"""Unit tests for the standing metrics-integrity sweep registry.

Covers: registry wiring, config resolution + gating, per-check ground-truth
recompute (landed-vs-reported merges, playbook confirmation integrity, non-code
outcome window accounting), enforce-mode deduped enrollment, error isolation,
the skipped/inconclusive contract, and the bounded observability snapshot.
"""

import pytest
import pytest_asyncio
from datetime import timedelta

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, select

import app.metrics_integrity_sweep as mis
from app.metrics_integrity_sweep import (
    CheckResult,
    CheckStatus,
    SweepContext,
    register_check,
    registered_checks,
    run_metrics_integrity_sweep,
    get_metrics_integrity_observability,
)
from app.models import (
    Artifact,
    ArtifactType,
    EvergreenBacklogItem,
    Project,
    Task,
    TaskStatus,
)
from app.shared_state import runtime_config as _rc
from app.time_utils import _utcnow


@pytest_asyncio.fixture()
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    Session = sessionmaker(
        bind=engine,
        class_=AsyncSession,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
    )
    async with Session() as s:
        yield s
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
    await engine.dispose()


@pytest.fixture(autouse=True)
def _clean_observability_and_config():
    """Reset the process-local cadence gate + observability and restore config."""
    mis._reset_observability_for_tests()
    saved = {k: v for k, v in _rc.items() if k.startswith("metrics_integrity_sweep_")}
    _rc["metrics_integrity_sweep_mode"] = "observe"
    _rc["metrics_integrity_sweep_interval_minutes"] = 60
    yield
    for k in [k for k in _rc if k.startswith("metrics_integrity_sweep_")]:
        _rc.pop(k, None)
    _rc.update(saved)
    mis._reset_observability_for_tests()


async def _project(db) -> Project:
    p = Project(name="mi-unit", user_id="u1")
    db.add(p)
    await db.commit()
    await db.refresh(p)
    return p


async def _pr_task(db, project, *, landed: bool) -> Task:
    """A COMPLETED task with a PR artifact; landed controls the canonical signal."""
    ctx = {}
    if landed:
        ctx["landed_on_integration"] = True
        ctx["pr_merged"] = True
    else:
        # PR artifact present but explicitly did NOT land (a ghost merge).
        ctx["landed_on_integration"] = False
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        status=TaskStatus.COMPLETED,
        context=ctx,
    )
    db.add(t)
    await db.commit()
    await db.refresh(t)
    art = Artifact(task_id=t.id, artifact_type=ArtifactType.PULL_REQUEST)
    db.add(art)
    await db.commit()
    return t


# --- Registry ---------------------------------------------------------------


def test_registry_contains_initial_checks():
    names = {c.name for c in registered_checks()}
    assert {
        "landed_vs_reported_merges",
        "playbook_confirmation_integrity",
        "noncode_outcome_window_accounting",
        "direction_acceptance_metric_invariant",
    } <= names


def test_check_result_finding_key_is_stable_and_scoped():
    r = CheckResult(
        check="landed_vs_reported_merges", status=CheckStatus.FLAGGED, subject="proj-1"
    )
    assert r.finding_key() == "metrics-integrity:landed_vs_reported_merges:proj-1"
    r2 = CheckResult(check="x", status=CheckStatus.FLAGGED, subject=None)
    assert r2.finding_key() == "metrics-integrity:x:global"


# --- Config + gating --------------------------------------------------------


def test_config_falls_back_on_bad_values():
    _rc["metrics_integrity_sweep_mode"] = "nonsense"
    _rc["metrics_integrity_sweep_interval_minutes"] = "not-an-int"
    _rc["metrics_integrity_sweep_max_projects"] = -5
    cfg = mis._resolve_config()
    assert cfg["mode"] == "observe"
    assert cfg["interval_minutes"] == mis.DEFAULT_INTERVAL_MINUTES
    assert cfg["max_projects"] == mis.DEFAULT_MAX_PROJECTS_PER_CHECK


@pytest.mark.asyncio
async def test_mode_off_gate_returns_none(db):
    _rc["metrics_integrity_sweep_mode"] = "off"
    assert await run_metrics_integrity_sweep(db, now=_utcnow()) is None
    assert get_metrics_integrity_observability()["ticks"] == 0


@pytest.mark.asyncio
async def test_cadence_gate_blocks_second_call(db):
    await _project(db)
    now = _utcnow()
    assert await run_metrics_integrity_sweep(db, now=now) is not None
    assert (
        await run_metrics_integrity_sweep(db, now=now + timedelta(minutes=30)) is None
    )
    # Past the interval it runs again.
    assert (
        await run_metrics_integrity_sweep(db, now=now + timedelta(minutes=61))
        is not None
    )


# --- Check: landed_vs_reported_merges ---------------------------------------


@pytest.mark.asyncio
async def test_landed_vs_reported_flags_ghost_merges(db):
    project = await _project(db)
    await _pr_task(db, project, landed=True)
    await _pr_task(db, project, landed=False)
    await _pr_task(db, project, landed=False)

    ctx = SweepContext(
        session=db,
        now=_utcnow(),
        mode="observe",
        max_projects=25,
        sample_limit=200,
        window_days=14,
    )
    results = await mis.check_landed_vs_reported_merges(ctx)
    flagged = [r for r in results if r.flagged]
    assert len(flagged) == 1
    f = flagged[0]
    assert f.self_reported == 3  # three PR-artifact completed tasks
    assert f.ground_truth == 1  # only one landed
    assert f.delta == 2  # two ghost merges


@pytest.mark.asyncio
async def test_landed_vs_reported_ok_when_all_landed(db):
    project = await _project(db)
    await _pr_task(db, project, landed=True)
    await _pr_task(db, project, landed=True)
    ctx = SweepContext(
        session=db, now=_utcnow(), mode="observe", max_projects=25,
        sample_limit=200, window_days=14,
    )
    results = await mis.check_landed_vs_reported_merges(ctx)
    assert all(not r.flagged for r in results)
    assert any(r.status == CheckStatus.OK and r.delta == 0 for r in results)


@pytest.mark.asyncio
async def test_landed_vs_reported_skips_with_no_pr_tasks(db):
    ctx = SweepContext(
        session=db, now=_utcnow(), mode="observe", max_projects=25,
        sample_limit=200, window_days=14,
    )
    results = await mis.check_landed_vs_reported_merges(ctx)
    assert len(results) == 1
    assert results[0].status == CheckStatus.SKIPPED


# --- Check: playbook_confirmation_integrity ---------------------------------


@pytest.mark.asyncio
async def test_playbook_flags_over_credit_and_negative(db):
    project = await _project(db)
    project.design_doc = {
        "playbook/over": {"confirmed_count": 9, "reference_count": 2},
        "playbook/neg": {"confirmed_count": -1, "reference_count": 3},
        "playbook/ok": {"confirmed_count": 1, "reference_count": 5},
    }
    flag_modified(project, "design_doc")
    db.add(project)
    await db.commit()

    ctx = SweepContext(
        session=db, now=_utcnow(), mode="observe", max_projects=25,
        sample_limit=200, window_days=14,
    )
    results = await mis.check_playbook_confirmation_integrity(ctx)
    flagged = [r for r in results if r.flagged]
    assert len(flagged) == 1
    f = flagged[0]
    assert f.delta == 2  # one over-credit + one negative
    assert "over" in f.detail


@pytest.mark.asyncio
async def test_playbook_ok_when_consistent(db):
    project = await _project(db)
    project.design_doc = {
        "playbook/a": {"confirmed_count": 2, "reference_count": 5},
        "playbook/b": {"confirmed_count": 0, "reference_count": 0},
    }
    flag_modified(project, "design_doc")
    db.add(project)
    await db.commit()
    ctx = SweepContext(
        session=db, now=_utcnow(), mode="observe", max_projects=25,
        sample_limit=200, window_days=14,
    )
    results = await mis.check_playbook_confirmation_integrity(ctx)
    assert all(not r.flagged for r in results)
    assert any(r.status == CheckStatus.OK for r in results)


@pytest.mark.asyncio
async def test_playbook_does_not_mutate_source_counter(db):
    project = await _project(db)
    project.design_doc = {"playbook/x": {"confirmed_count": 7, "reference_count": 1}}
    flag_modified(project, "design_doc")
    db.add(project)
    await db.commit()
    ctx = SweepContext(
        session=db, now=_utcnow(), mode="observe", max_projects=25,
        sample_limit=200, window_days=14,
    )
    await mis.check_playbook_confirmation_integrity(ctx)
    await db.refresh(project)
    assert project.design_doc["playbook/x"]["confirmed_count"] == 7
    assert project.design_doc["playbook/x"]["reference_count"] == 1


# --- Check: direction_acceptance_metric_invariant ---------------------------


@pytest.mark.asyncio
async def test_direction_acceptance_metric_check_flags_violating_entry(db):
    """A research_history entry where proven > declared must be flagged.

    This exercises the real acceptance_metric_invariant_violations path via the
    registered sweep check — not a mock — so the test validates that the check
    is wired up and running against live data correctly.
    """
    project = await _project(db)
    # Build a violating entry: proven=3 exceeds declared=2 (invariant: proven ≤ declared).
    violating_entry = {
        "task_id": "aaaaaaaa-0000-0000-0000-000000000001",
        "prs_merged": 5,
        "verified_prs_merged": 4,
        "acceptance_proven_merged": 3,   # violates: proven (3) > declared (2)
        "acceptance_declared_merged": 2,
        "acceptance_merged_write_total": 4,
        "acceptance_declaration_coverage": 0.5,  # 2/4
    }
    project.directions = [
        {
            "id": "d-test-001",
            "research_history": [violating_entry],
        }
    ]
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(project, "directions")
    db.add(project)
    await db.commit()

    ctx = SweepContext(
        session=db,
        now=_utcnow(),
        mode="observe",
        max_projects=25,
        sample_limit=200,
        window_days=14,
    )
    results = await mis.check_direction_acceptance_metric_invariant(ctx)
    flagged = [r for r in results if r.flagged]
    assert len(flagged) == 1, f"expected 1 flagged result, got {results}"
    f = flagged[0]
    assert f.check == "direction_acceptance_metric_invariant"
    assert f.delta >= 1  # at least one invariant violated
    assert "proven" in f.detail  # the violation message names the field


@pytest.mark.asyncio
async def test_direction_acceptance_metric_check_ok_when_consistent(db):
    project = await _project(db)
    good_entry = {
        "task_id": "aaaaaaaa-0000-0000-0000-000000000002",
        "prs_merged": 5,
        "verified_prs_merged": 4,
        "acceptance_proven_merged": 2,
        "acceptance_declared_merged": 3,
        "acceptance_merged_write_total": 4,
        "acceptance_declaration_coverage": 0.75,  # 3/4
    }
    project.directions = [{"id": "d-test-002", "research_history": [good_entry]}]
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(project, "directions")
    db.add(project)
    await db.commit()

    ctx = SweepContext(
        session=db, now=_utcnow(), mode="observe", max_projects=25,
        sample_limit=200, window_days=14,
    )
    results = await mis.check_direction_acceptance_metric_invariant(ctx)
    assert all(not r.flagged for r in results)
    assert any(r.status == CheckStatus.OK for r in results)


@pytest.mark.asyncio
async def test_direction_acceptance_metric_check_skips_legacy_entries(db):
    """Entries without any acceptance fields (pre-acceptance-layer) must be skipped."""
    project = await _project(db)
    legacy_entry = {
        "task_id": "aaaaaaaa-0000-0000-0000-000000000003",
        "prs_merged": 3,
    }
    project.directions = [{"id": "d-legacy", "research_history": [legacy_entry]}]
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(project, "directions")
    db.add(project)
    await db.commit()

    ctx = SweepContext(
        session=db, now=_utcnow(), mode="observe", max_projects=25,
        sample_limit=200, window_days=14,
    )
    results = await mis.check_direction_acceptance_metric_invariant(ctx)
    assert len(results) == 1
    assert results[0].status == CheckStatus.SKIPPED


# --- Check: noncode_outcome_window_accounting -------------------------------


@pytest.mark.asyncio
async def test_noncode_window_flags_misclassified_task(db):
    project = await _project(db)
    now = _utcnow()
    window_start = now - timedelta(days=14)
    # Created BEFORE the window but completed INSIDE it: naive(created_at) would
    # exclude it, terminal-time includes it => a mismatch.
    old_created = window_start - timedelta(days=2)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "research"}],
        status=TaskStatus.COMPLETED,
        created_at=old_created,
        status_log=[{"t": (now - timedelta(days=1)).isoformat(), "s": "COMPLETED"}],
        context={},
    )
    db.add(t)
    await db.commit()

    ctx = SweepContext(
        session=db, now=now, mode="observe", max_projects=25,
        sample_limit=200, window_days=14,
    )
    results = await mis.check_noncode_outcome_window_accounting(ctx)
    flagged = [r for r in results if r.flagged]
    assert len(flagged) == 1
    assert flagged[0].delta == 1


@pytest.mark.asyncio
async def test_noncode_window_inconclusive_when_over_sample_limit(db):
    project = await _project(db)
    now = _utcnow()
    for _ in range(3):
        db.add(
            Task(
                project_id=project.id,
                payloads=[{"type": "research"}],
                status=TaskStatus.COMPLETED,
                created_at=now - timedelta(days=1),
                context={},
            )
        )
    await db.commit()
    ctx = SweepContext(
        session=db, now=now, mode="observe", max_projects=25,
        sample_limit=2, window_days=14,
    )
    results = await mis.check_noncode_outcome_window_accounting(ctx)
    assert len(results) == 1
    assert results[0].status == CheckStatus.INCONCLUSIVE


@pytest.mark.asyncio
async def test_noncode_window_skips_review_chain_tasks(db):
    project = await _project(db)
    now = _utcnow()
    # A review-chain task (has review_loop context) is a PR-chain task, not
    # non-code — it must be excluded from this check.
    db.add(
        Task(
            project_id=project.id,
            payloads=[{"type": "create"}],
            status=TaskStatus.COMPLETED,
            created_at=now - timedelta(days=1),
            context={"review_loop": {"iteration": 1}},
        )
    )
    await db.commit()
    ctx = SweepContext(
        session=db, now=now, mode="observe", max_projects=25,
        sample_limit=200, window_days=14,
    )
    results = await mis.check_noncode_outcome_window_accounting(ctx)
    assert len(results) == 1
    assert results[0].status == CheckStatus.SKIPPED


# --- Enforce-mode enrollment (deduped; never touches source counter) --------


@pytest.mark.asyncio
async def test_enforce_mode_enrolls_deduped_finding(db):
    _rc["metrics_integrity_sweep_mode"] = "enforce"
    project = await _project(db)
    await _pr_task(db, project, landed=False)  # a ghost merge -> flag

    now = _utcnow()
    tick = await run_metrics_integrity_sweep(db, now=now)
    assert tick is not None
    assert tick["enrolled"] >= 1

    rows = (
        await db.execute(
            select(EvergreenBacklogItem).where(
                EvergreenBacklogItem.project_id == project.id
            )
        )
    ).scalars().all()
    assert any(
        r.finding_key.startswith("metrics-integrity:landed_vs_reported_merges:")
        for r in rows
    )
    before = len(rows)

    # Second run past cadence re-detects the same drift but enrolls nothing new.
    tick2 = await run_metrics_integrity_sweep(db, now=now + timedelta(minutes=61))
    assert tick2 is not None
    assert tick2["enrolled"] == 0
    rows2 = (
        await db.execute(
            select(EvergreenBacklogItem).where(
                EvergreenBacklogItem.project_id == project.id
            )
        )
    ).scalars().all()
    assert len(rows2) == before


# --- Error isolation --------------------------------------------------------


@pytest.mark.asyncio
async def test_one_crashing_check_does_not_abort_the_tick(db):
    await _project(db)

    @register_check("boom_test_check", "always raises")
    async def _boom(ctx):
        raise RuntimeError("intentional")

    try:
        now = _utcnow()
        tick = await run_metrics_integrity_sweep(db, now=now)
        assert tick is not None
        assert tick["errors"] >= 1
        # The other checks still ran (results include non-error statuses).
        snap = get_metrics_integrity_observability()
        assert snap["errors_total"] >= 1
        assert snap["checks_run"] > 1
    finally:
        mis._REGISTRY.pop("boom_test_check", None)
