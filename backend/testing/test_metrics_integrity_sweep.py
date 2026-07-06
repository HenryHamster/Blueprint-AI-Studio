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
    CriterionEvidence,
    EvergreenBacklogItem,
    Project,
    ReleaseVersion,
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


async def _open_version_with_criterion(db, project, *, text: str):
    from app import acceptance_criteria as ac

    version = ReleaseVersion(project_id=project.id, version_number=1, status="open")
    db.add(version)
    await db.commit()
    await db.refresh(version)
    criterion = await ac.create_criterion(
        db,
        project_id=project.id,
        release_version_id=version.id,
        text=text,
        created_by="strategist",
    )
    return version, criterion


async def _completed_landed_proven_task(db, project, criterion_key: str) -> Task:
    from app.acceptance_gate import CODE_ACCEPTANCE_VERDICT_KEY

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        status=TaskStatus.COMPLETED,
        context={
            "landed_on_integration": True,
            "pr_merged": True,
            CODE_ACCEPTANCE_VERDICT_KEY: {"proven": True},
        },
        criterion_ids=[criterion_key],
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)
    return task


# --- Registry ---------------------------------------------------------------


def test_registry_contains_initial_checks():
    names = {c.name for c in registered_checks()}
    assert {
        "landed_vs_reported_merges",
        "criterion_merge_evidence_gap",
        "playbook_confirmation_integrity",
        "noncode_outcome_window_accounting",
        "direction_acceptance_metric_invariant",
        "completion_debt_completeness",
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


# --- Check: criterion_merge_evidence_gap -----------------------------------


@pytest.mark.asyncio
async def test_criterion_merge_evidence_gap_flags_missing_evidence(db):
    project = await _project(db)
    _, criterion = await _open_version_with_criterion(
        db, project, text="Merged task should record evidence"
    )
    await _completed_landed_proven_task(db, project, criterion.criterion_key)

    ctx = SweepContext(
        session=db, now=_utcnow(), mode="observe", max_projects=25,
        sample_limit=200, window_days=14,
    )
    results = await mis.check_criterion_merge_evidence_gap(ctx)
    flagged = [r for r in results if r.flagged]
    assert len(flagged) == 1
    f = flagged[0]
    assert f.self_reported == 1
    assert f.ground_truth == 0
    assert f.delta == 1
    assert "no task_merge evidence" in f.detail


@pytest.mark.asyncio
async def test_criterion_merge_evidence_gap_ok_when_evidence_present(db):
    project = await _project(db)
    _, criterion = await _open_version_with_criterion(
        db, project, text="Merged task should record evidence"
    )
    task = await _completed_landed_proven_task(db, project, criterion.criterion_key)
    db.add(
        CriterionEvidence(
            project_id=project.id,
            criterion_id=criterion.id,
            source_kind="task_merge",
            source_id=str(task.id),
            detail={"proven": True},
        )
    )
    await db.commit()

    ctx = SweepContext(
        session=db, now=_utcnow(), mode="observe", max_projects=25,
        sample_limit=200, window_days=14,
    )
    results = await mis.check_criterion_merge_evidence_gap(ctx)
    assert all(not r.flagged for r in results)
    assert any(r.status == CheckStatus.OK and r.delta == 0 for r in results)


@pytest.mark.asyncio
async def test_criterion_merge_evidence_gap_skips_with_no_qualifying_tasks(db):
    project = await _project(db)
    _, criterion = await _open_version_with_criterion(
        db, project, text="Merged task should record evidence"
    )
    db.add(
        Task(
            project_id=project.id,
            payloads=[{"type": "create"}],
            status=TaskStatus.COMPLETED,
            context={"landed_on_integration": False, "pr_merged": False},
            criterion_ids=[criterion.criterion_key],
        )
    )
    await db.commit()

    ctx = SweepContext(
        session=db, now=_utcnow(), mode="observe", max_projects=25,
        sample_limit=200, window_days=14,
    )
    results = await mis.check_criterion_merge_evidence_gap(ctx)
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


@pytest.mark.asyncio
async def test_playbook_flags_confirmed_over_followed(db):
    project = await _project(db)
    project.design_doc = {
        # confirmed=7 > followed=5 with reference=10 — passes reference check, fails followed check
        "playbook/over_followed": {"confirmed_count": 7, "reference_count": 10, "followed_count": 5},
        # clean entry — should not be flagged
        "playbook/ok": {"confirmed_count": 2, "reference_count": 5, "followed_count": 3},
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
    assert f.delta >= 1
    assert "followed" in f.detail


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
    import uuid as _uuid
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
    # subject must be the owning project UUID — not "global" — so enforce-mode
    # enrollment can create a per-project EvergreenBacklogItem.
    assert flagged[0].subject != "global"
    assert str(_uuid.UUID(flagged[0].subject)) == flagged[0].subject


@pytest.mark.asyncio
async def test_noncode_window_enforce_enrolls_project_finding(db):
    """Enforce mode must create an EvergreenBacklogItem for the owning project.

    Previously subject="global" caused _enroll_flag_finding to return False
    unconditionally, so no backlog item was ever created even with real drift.
    """
    _rc["metrics_integrity_sweep_mode"] = "enforce"
    project = await _project(db)
    now = _utcnow()
    window_start = now - timedelta(days=14)
    old_created = window_start - timedelta(days=2)
    # A task created before but completed inside the window: produces a mismatch.
    db.add(
        Task(
            project_id=project.id,
            payloads=[{"type": "research"}],
            status=TaskStatus.COMPLETED,
            created_at=old_created,
            status_log=[{"t": (now - timedelta(days=1)).isoformat(), "s": "COMPLETED"}],
            context={},
        )
    )
    await db.commit()

    tick = await run_metrics_integrity_sweep(db, now=now)
    assert tick is not None
    assert tick["flagged"] >= 1
    assert tick["enrolled"] >= 1

    rows = (
        await db.execute(
            select(EvergreenBacklogItem).where(
                EvergreenBacklogItem.project_id == project.id
            )
        )
    ).scalars().all()
    assert any(
        r.finding_key.startswith("metrics-integrity:noncode_outcome_window_accounting:")
        for r in rows
    ), f"expected a noncode_outcome_window_accounting backlog item, got {[r.finding_key for r in rows]}"


@pytest.mark.asyncio
async def test_noncode_window_flags_long_running_task_created_before_load_cutoff(db):
    """A task created before load_cutoff but completed inside the window must
    be loaded and flagged as mis-windowed (the regression for the OR-widened
    WHERE clause fix)."""
    project = await _project(db)
    now = _utcnow()
    # created_at is beyond the old load_cutoff (2*window_days = 28 days ago),
    # so the previous created_at-only query would miss it entirely.
    very_old_created = now - timedelta(days=2 * 14 + 5)  # 33 days ago
    completed_inside_window = now - timedelta(days=1)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "research"}],
        status=TaskStatus.COMPLETED,
        created_at=very_old_created,
        completed_at=completed_inside_window,
        status_log=[{"t": completed_inside_window.isoformat(), "s": "COMPLETED"}],
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
async def test_noncode_window_flags_task_with_missing_completed_at_and_terminal_status_log(db):
    """Rows with missing completed_at still count if status_log shows an in-window terminal time."""
    project = await _project(db)
    now = _utcnow()
    very_old_created = now - timedelta(days=33)
    terminal_at = now - timedelta(days=1)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "research"}],
        status=TaskStatus.COMPLETED,
        created_at=very_old_created,
        completed_at=None,
        status_log=[{"t": terminal_at.isoformat(), "s": "COMPLETED"}],
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


# --- Check: completion_debt_completeness ------------------------------------


def _ctx(db, **overrides):
    base = {
        "session": db,
        "now": _utcnow(),
        "mode": "observe",
        "max_projects": 25,
        "sample_limit": 200,
        "window_days": 14,
    }
    base.update(overrides)
    return SweepContext(**base)


async def _debt_row(db, *, dedupe_key, status="open", kind="capability_trial", project_id=None):
    """Insert a CompletionDebtEntry directly (bypassing the recorders)."""
    from app.models import CompletionDebtEntry

    row = CompletionDebtEntry(
        project_id=project_id,
        kind=kind,
        dedupe_key=dedupe_key,
        status=status,
        title="test debt",
    )
    db.add(row)
    await db.commit()
    return row


def _flagged_for(results, subject):
    return [r for r in results if r.flagged and r.subject == subject]


@pytest.mark.asyncio
async def test_completion_debt_completeness_flags_missing_expected_obligations(db):
    """The named acceptance test: with no ledger rows at all, the standing global
    seed/dormant-knob obligations are recomputed and flagged as an under-count."""
    results = await mis.check_completion_debt_completeness(_ctx(db))
    global_flags = _flagged_for(results, "global")
    assert len(global_flags) == 1, f"expected one global flag, got {results}"
    f = global_flags[0]
    assert f.check == "completion_debt_completeness"
    assert f.delta >= 1  # at least the standing seed inventory is missing
    assert f.self_reported == 0  # no open rows exist
    assert f.ground_truth == f.delta  # every expected obligation is missing
    assert "under-count" in f.detail
    # The recomputed expected set covers the standing seed inventory: delta counts
    # at least the three seed:* rows plus whatever dormant knobs are currently off.
    from app.completion_debt import _iter_seed_debts

    seed_keys = {d["dedupe_key"] for d in _iter_seed_debts()}
    assert any(k.startswith("seed:") for k in seed_keys)
    assert f.delta >= len(seed_keys)


@pytest.mark.asyncio
async def test_completion_debt_missing_capability_trial_flagged(db):
    project = await _project(db)
    project.capability_adoption_verdicts = {"repo_map": {"verdict": "continue-trial"}}
    flag_modified(project, "capability_adoption_verdicts")
    db.add(project)
    await db.commit()

    results = await mis.check_completion_debt_completeness(_ctx(db))
    pid_flags = _flagged_for(results, str(project.id))
    assert len(pid_flags) == 1
    f = pid_flags[0]
    assert f.delta == 1
    assert f"capability_trial:{project.id}:repo_map" in f.detail


@pytest.mark.asyncio
async def test_completion_debt_missing_no_ci_gate_flagged(db):
    project = await _project(db)
    project.merge_gate_runtime = {"last_unverified_reason": "verify_setup_failed"}
    flag_modified(project, "merge_gate_runtime")
    db.add(project)
    await db.commit()

    results = await mis.check_completion_debt_completeness(_ctx(db))
    pid_flags = _flagged_for(results, str(project.id))
    assert len(pid_flags) == 1
    assert f"no_ci_gate:{project.id}" in pid_flags[0].detail


@pytest.mark.asyncio
async def test_completion_debt_open_row_produces_ok(db):
    project = await _project(db)
    project.capability_adoption_verdicts = {"repo_map": {"verdict": "continue-trial"}}
    flag_modified(project, "capability_adoption_verdicts")
    db.add(project)
    await db.commit()
    # The recorder-canonical open row exists — the obligation is satisfied.
    await _debt_row(
        db,
        dedupe_key=f"capability_trial:{project.id}:repo_map",
        status="open",
        project_id=project.id,
    )

    results = await mis.check_completion_debt_completeness(_ctx(db))
    assert not _flagged_for(results, str(project.id))
    ok = [
        r
        for r in results
        if r.subject == str(project.id) and r.status == CheckStatus.OK
    ]
    assert len(ok) == 1
    assert ok[0].self_reported == 1
    assert ok[0].delta == 0


@pytest.mark.asyncio
async def test_completion_debt_dismissed_row_ignored(db):
    project = await _project(db)
    project.capability_adoption_verdicts = {"repo_map": {"verdict": "continue-trial"}}
    flag_modified(project, "capability_adoption_verdicts")
    db.add(project)
    await db.commit()
    # Operator dismissed the obligation — an explicit disposition, not missing debt.
    await _debt_row(
        db,
        dedupe_key=f"capability_trial:{project.id}:repo_map",
        status="dismissed",
        project_id=project.id,
    )

    results = await mis.check_completion_debt_completeness(_ctx(db))
    # Neither flagged (not missing) nor OK-with-drift for this project.
    assert not _flagged_for(results, str(project.id))
    assert not [
        r
        for r in results
        if r.subject == str(project.id) and r.status == CheckStatus.OK
    ]


@pytest.mark.asyncio
async def test_completion_debt_resolved_row_flagged(db):
    """A live obligation whose only row is RESOLVED (recorder never re-opened it)
    is a non-open, non-dismissed under-count and must be flagged."""
    project = await _project(db)
    project.capability_adoption_verdicts = {"repo_map": {"verdict": "continue-trial"}}
    flag_modified(project, "capability_adoption_verdicts")
    db.add(project)
    await db.commit()
    await _debt_row(
        db,
        dedupe_key=f"capability_trial:{project.id}:repo_map",
        status="resolved",
        project_id=project.id,
    )

    results = await mis.check_completion_debt_completeness(_ctx(db))
    pid_flags = _flagged_for(results, str(project.id))
    assert len(pid_flags) == 1
    assert pid_flags[0].delta == 1


@pytest.mark.asyncio
async def test_completion_debt_inconclusive_over_bounds(db):
    for _ in range(3):
        await _project(db)
    results = await mis.check_completion_debt_completeness(_ctx(db, max_projects=2))
    assert len(results) == 1
    assert results[0].status == CheckStatus.INCONCLUSIVE


@pytest.mark.asyncio
async def test_completion_debt_check_is_flag_only(db):
    """Observe + enforce runs never create, reopen, resolve, or dismiss a
    CompletionDebtEntry row — the ledger is untouched by the audit."""
    from app.models import CompletionDebtEntry

    project = await _project(db)
    project.capability_adoption_verdicts = {"repo_map": {"verdict": "continue-trial"}}
    flag_modified(project, "capability_adoption_verdicts")
    db.add(project)
    await db.commit()
    # A dismissed row for a DIFFERENT obligation must survive both runs unchanged.
    await _debt_row(
        db,
        dedupe_key=f"capability_trial:{project.id}:other_cap",
        status="dismissed",
        project_id=project.id,
    )

    async def _snapshot():
        rows = (await db.execute(select(CompletionDebtEntry))).scalars().all()
        return sorted((r.dedupe_key, r.status) for r in rows)

    before = await _snapshot()

    now = _utcnow()
    _rc["metrics_integrity_sweep_mode"] = "observe"
    assert await run_metrics_integrity_sweep(db, now=now) is not None
    _rc["metrics_integrity_sweep_mode"] = "enforce"
    assert (
        await run_metrics_integrity_sweep(db, now=now + timedelta(minutes=61))
        is not None
    )

    assert await _snapshot() == before


@pytest.mark.asyncio
async def test_completion_debt_enforce_enrolls_for_project_obligation(db):
    """A missing per-project obligation enrolls exactly one deduped
    EvergreenBacklogItem (subject is a real project id); re-running enrolls none."""
    _rc["metrics_integrity_sweep_mode"] = "enforce"
    project = await _project(db)
    project.capability_adoption_verdicts = {"repo_map": {"verdict": "continue-trial"}}
    flag_modified(project, "capability_adoption_verdicts")
    db.add(project)
    await db.commit()

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
    matching = [
        r
        for r in rows
        if r.finding_key
        == f"metrics-integrity:completion_debt_completeness:{project.id}"
    ]
    assert len(matching) == 1
    before = len(rows)

    tick2 = await run_metrics_integrity_sweep(db, now=now + timedelta(minutes=61))
    assert tick2 is not None
    rows2 = (
        await db.execute(
            select(EvergreenBacklogItem).where(
                EvergreenBacklogItem.project_id == project.id
            )
        )
    ).scalars().all()
    assert len(rows2) == before  # deduped: no new enrollment


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
