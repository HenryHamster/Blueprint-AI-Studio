"""Unit tests for the standing metrics-integrity sweep registry.

Covers: registry wiring, config resolution + gating, per-check ground-truth
recompute (landed-vs-reported merges, playbook confirmation integrity, non-code
outcome window accounting), enforce-mode deduped enrollment, error isolation,
the skipped/inconclusive contract, and the bounded observability snapshot.
"""

import json
import os
import tempfile
from unittest.mock import patch

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
        "rollup_child_landing_credit",
        "near_cap_size_warning",
        "problem_model_duplicate_id_sweep",
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


# --- Check: rollup_child_landing_credit -------------------------------------


def _rollup_ctx(**kwargs):
    return SweepContext(
        session=None,
        now=_utcnow(),
        mode="observe",
        max_projects=25,
        sample_limit=200,
        window_days=14,
        **kwargs,
    )


async def _rollup_parent_task(
    db, project, *, rollup_pr_number, landed: bool
) -> Task:
    """A COMPLETED rollup parent task."""
    ctx = {
        "_is_rollup_review": True,
        "rollup_pr_number": rollup_pr_number,
    }
    if landed:
        ctx["landed_on_integration"] = True
        ctx["pr_merged"] = True
    else:
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
    return t


async def _rollup_child_task(
    db,
    project,
    *,
    rollup_pr_number,
    pr_merged: bool = True,
    landed: bool | None = None,
    force_dropped: bool = False,
) -> Task:
    """A COMPLETED rollup child task (has rollup_pr_number but is NOT a parent)."""
    ctx: dict = {"rollup_pr_number": rollup_pr_number}
    if pr_merged:
        ctx["pr_merged"] = True
    if landed is True:
        ctx["landed_on_integration"] = True
    elif landed is False:
        ctx["landed_on_integration"] = False
    if force_dropped:
        ctx["_non_merge_unblock_reason"] = "force_dropped"
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        status=TaskStatus.COMPLETED,
        context=ctx,
    )
    db.add(t)
    await db.commit()
    await db.refresh(t)
    return t


@pytest.mark.asyncio
async def test_registry_contains_rollup_child_landing_credit():
    names = {c.name for c in registered_checks()}
    assert "rollup_child_landing_credit" in names


@pytest.mark.asyncio
async def test_rollup_child_landing_credit_flags_under_credited_child(db):
    """Parent landed, child has pr_merged=True but landed_on_integration=False → FLAGGED.

    The constituent child PR merged into the rollup staging branch (setting pr_merged=True)
    but landed_on_integration was stamped False (not on the integration branch yet). When
    the rollup parent lands, the child should be backfilled to landed_on_integration=True.
    Without the backfill, has_landed_signal(child) returns False (explicit False wins over
    the pr_merged legacy fallback), representing the under-credit.
    """
    project = await _project(db)
    rollup_pr = 42
    await _rollup_parent_task(db, project, rollup_pr_number=rollup_pr, landed=True)
    # landed=False: explicitly NOT stamped on integration (the staging-merge state)
    await _rollup_child_task(
        db, project, rollup_pr_number=rollup_pr, pr_merged=True, landed=False
    )

    ctx = SweepContext(
        session=db, now=_utcnow(), mode="observe",
        max_projects=25, sample_limit=200, window_days=14,
    )
    results = await mis.check_rollup_child_landing_credit(ctx)
    flagged = [r for r in results if r.flagged]
    assert len(flagged) == 1, f"expected 1 flagged, got {results}"
    f = flagged[0]
    assert f.check == "rollup_child_landing_credit"
    assert f.delta == 1
    assert f.ground_truth == 1
    assert f.self_reported == 0
    assert "under-credited" in f.detail


@pytest.mark.asyncio
async def test_rollup_child_landing_credit_ok_when_child_is_stamped(db):
    """Parent landed, child also carries landed_on_integration=True → OK."""
    project = await _project(db)
    rollup_pr = 99
    await _rollup_parent_task(db, project, rollup_pr_number=rollup_pr, landed=True)
    await _rollup_child_task(
        db, project, rollup_pr_number=rollup_pr, pr_merged=True, landed=True
    )

    ctx = SweepContext(
        session=db, now=_utcnow(), mode="observe",
        max_projects=25, sample_limit=200, window_days=14,
    )
    results = await mis.check_rollup_child_landing_credit(ctx)
    assert all(not r.flagged for r in results)
    assert any(r.status == CheckStatus.OK and r.delta == 0 for r in results)


@pytest.mark.asyncio
async def test_rollup_child_landing_credit_skips_force_dropped_child(db):
    """A force-dropped child (has _non_merge_unblock_reason) must not be counted."""
    project = await _project(db)
    rollup_pr = 77
    await _rollup_parent_task(db, project, rollup_pr_number=rollup_pr, landed=True)
    await _rollup_child_task(
        db, project, rollup_pr_number=rollup_pr,
        pr_merged=True, landed=None, force_dropped=True
    )

    ctx = SweepContext(
        session=db, now=_utcnow(), mode="observe",
        max_projects=25, sample_limit=200, window_days=14,
    )
    results = await mis.check_rollup_child_landing_credit(ctx)
    # Force-dropped child excluded → OK (no under-credited children)
    assert all(not r.flagged for r in results)
    ok = [r for r in results if r.status == CheckStatus.OK]
    assert len(ok) == 1
    assert ok[0].delta == 0


@pytest.mark.asyncio
async def test_rollup_child_landing_credit_ignores_unmerged_rollup_parent(db):
    """If the rollup parent has NOT landed, the child should not be flagged."""
    project = await _project(db)
    rollup_pr = 55
    await _rollup_parent_task(db, project, rollup_pr_number=rollup_pr, landed=False)
    await _rollup_child_task(
        db, project, rollup_pr_number=rollup_pr, pr_merged=True, landed=None
    )

    ctx = SweepContext(
        session=db, now=_utcnow(), mode="observe",
        max_projects=25, sample_limit=200, window_days=14,
    )
    results = await mis.check_rollup_child_landing_credit(ctx)
    # No landed parent → SKIPPED (no landed rollup_pr_numbers to match against)
    assert all(not r.flagged for r in results)
    statuses = {r.status for r in results}
    assert CheckStatus.FLAGGED not in statuses


@pytest.mark.asyncio
async def test_rollup_child_landing_credit_counts_multiple_under_credited(db):
    """Delta equals the number of under-credited children when multiple exist."""
    project = await _project(db)
    rollup_pr = 11
    await _rollup_parent_task(db, project, rollup_pr_number=rollup_pr, landed=True)
    for _ in range(3):
        await _rollup_child_task(
            db, project, rollup_pr_number=rollup_pr, pr_merged=True, landed=False
        )

    ctx = SweepContext(
        session=db, now=_utcnow(), mode="observe",
        max_projects=25, sample_limit=200, window_days=14,
    )
    results = await mis.check_rollup_child_landing_credit(ctx)
    flagged = [r for r in results if r.flagged]
    assert len(flagged) == 1
    assert flagged[0].delta == 3


# --- Check: near_cap_size_warning -------------------------------------------


def _near_cap_ctx(db=None):
    return SweepContext(
        session=db,
        now=_utcnow(),
        mode="observe",
        max_projects=25,
        sample_limit=200,
        window_days=14,
    )


def _write_lines(path: str, n: int) -> None:
    """Write a file with exactly n lines."""
    with open(path, "w") as f:
        f.writelines(f"x = {i}\n" for i in range(n))


@pytest.fixture()
def near_cap_env():
    """Temporary source tree + limits JSON for near-cap tests.

    Yields (tmpdir, json_path, set_caps_fn) where set_caps_fn(caps_dict)
    rewrites the JSON to use the given caps and returns the root.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        json_path = os.path.join(tmpdir, "module_size_limits.json")

        def set_caps(caps: dict):
            with open(json_path, "w") as f:
                json.dump({"backend": caps}, f)
            return tmpdir

        yield tmpdir, json_path, set_caps


def test_registry_contains_near_cap_check():
    names = {c.name for c in registered_checks()}
    assert "near_cap_size_warning" in names


@pytest.mark.asyncio
async def test_near_cap_check_flags_module_at_threshold(near_cap_env):
    """A module at 95% of its cap is flagged with severity='warn'."""
    tmpdir, json_path, set_caps = near_cap_env
    cap = 100
    count = 95  # exactly 95% of cap
    fname = "mymodule.py"
    _write_lines(os.path.join(tmpdir, fname), count)
    set_caps({fname: cap})

    ctx = _near_cap_ctx()
    with (
        patch.object(mis, "_NEAR_CAP_LIMITS_PATH", json_path),
        patch.object(mis, "_NEAR_CAP_BACKEND_ROOT", tmpdir),
        patch("app.size_limit_policy.parse_bump_history", return_value=[]),
        patch("app.size_limit_policy._is_chronic_offender", return_value=False),
    ):
        results = await mis.check_near_cap_size_warning(ctx)

    flagged = [r for r in results if r.flagged]
    assert len(flagged) == 1
    f = flagged[0]
    assert f.check == "near_cap_size_warning"
    assert f.self_reported == count
    assert f.ground_truth == cap
    assert f.delta == cap - count
    assert f.severity == "warn"
    assert f.subject == fname
    assert fname in f.detail
    assert "95%" in f.detail


@pytest.mark.asyncio
async def test_near_cap_check_flags_chronic_as_error(near_cap_env):
    """A near-cap module flagged as a chronic offender uses severity='error'."""
    tmpdir, json_path, set_caps = near_cap_env
    cap = 200
    count = 195  # ~97.5% of cap
    fname = "chronic.py"
    _write_lines(os.path.join(tmpdir, fname), count)
    set_caps({fname: cap})

    ctx = _near_cap_ctx()
    fake_history = [
        {"commit_sha": "a" * 40, "commit_date": "2026-01-01T00:00:00Z", "old_cap": 100, "new_cap": 150},
        {"commit_sha": "b" * 40, "commit_date": "2026-02-01T00:00:00Z", "old_cap": 150, "new_cap": 180},
        {"commit_sha": "c" * 40, "commit_date": "2026-03-01T00:00:00Z", "old_cap": 180, "new_cap": 200},
    ]
    with (
        patch.object(mis, "_NEAR_CAP_LIMITS_PATH", json_path),
        patch.object(mis, "_NEAR_CAP_BACKEND_ROOT", tmpdir),
        patch("app.size_limit_policy.parse_bump_history", return_value=fake_history),
        patch("app.size_limit_policy._is_chronic_offender", return_value=True),
    ):
        results = await mis.check_near_cap_size_warning(ctx)

    flagged = [r for r in results if r.flagged]
    assert len(flagged) == 1
    f = flagged[0]
    assert f.severity == "error"
    assert "[chronic offender]" in f.detail


@pytest.mark.asyncio
async def test_near_cap_check_ok_when_below_threshold(near_cap_env):
    """A module at 90% of its cap is not flagged; returns OK."""
    tmpdir, json_path, set_caps = near_cap_env
    cap = 100
    count = 90  # 90% — below the 95% threshold
    fname = "under.py"
    _write_lines(os.path.join(tmpdir, fname), count)
    set_caps({fname: cap})

    ctx = _near_cap_ctx()
    with (
        patch.object(mis, "_NEAR_CAP_LIMITS_PATH", json_path),
        patch.object(mis, "_NEAR_CAP_BACKEND_ROOT", tmpdir),
    ):
        results = await mis.check_near_cap_size_warning(ctx)

    assert not any(r.flagged for r in results)
    assert any(r.status == CheckStatus.OK for r in results)


@pytest.mark.asyncio
async def test_near_cap_check_skips_when_no_limits_file():
    """A missing limits JSON returns a single SKIPPED result."""
    ctx = _near_cap_ctx()
    with (
        patch.object(mis, "_NEAR_CAP_LIMITS_PATH", "/nonexistent/path/limits.json"),
        patch.object(mis, "_NEAR_CAP_BACKEND_ROOT", "/nonexistent/root"),
    ):
        results = await mis.check_near_cap_size_warning(ctx)

    assert len(results) == 1
    assert results[0].status == CheckStatus.SKIPPED
    assert "absent or unreadable" in results[0].detail


@pytest.mark.asyncio
async def test_near_cap_check_skips_when_section_root_absent(near_cap_env):
    """A valid limits JSON but missing backend root returns SKIPPED."""
    tmpdir, json_path, set_caps = near_cap_env
    set_caps({"somemodule.py": 100})

    ctx = _near_cap_ctx()
    with (
        patch.object(mis, "_NEAR_CAP_LIMITS_PATH", json_path),
        patch.object(mis, "_NEAR_CAP_BACKEND_ROOT", "/nonexistent/backend/app"),
    ):
        results = await mis.check_near_cap_size_warning(ctx)

    assert len(results) == 1
    assert results[0].status == CheckStatus.SKIPPED
    assert "absent" in results[0].detail


# --- Check: problem_model_duplicate_id_sweep --------------------------------


@pytest.mark.asyncio
async def test_problem_model_dup_id_ok_when_unique(db):
    """A problem_model with all-unique ids in every collection → OK."""
    project = await _project(db)
    project.problem_model = {
        "pending_user_confirmation": [
            {"id": "pc-1", "type": "credentials", "proposal": "a", "rationale": ""},
            {"id": "pc-2", "type": "credentials", "proposal": "b", "rationale": ""},
        ],
        "gaps": [
            {"id": "g-1", "title": "gap1"},
            {"id": "g-2", "title": "gap2"},
        ],
    }
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(project, "problem_model")
    db.add(project)
    await db.commit()

    ctx = SweepContext(
        session=db, now=_utcnow(), mode="observe",
        max_projects=25, sample_limit=200, window_days=14,
    )
    results = await mis.check_problem_model_duplicate_id_sweep(ctx)
    flagged = [r for r in results if r.flagged]
    assert len(flagged) == 0
    ok = [r for r in results if r.status == CheckStatus.OK]
    assert len(ok) >= 1
    assert ok[0].subject == str(project.id)
    assert ok[0].delta == 0


@pytest.mark.asyncio
async def test_problem_model_dup_id_flagged_on_duplicate(db):
    """A duplicate id in pending_user_confirmation → FLAGGED with correct delta and per-project subject."""
    project = await _project(db)
    project.problem_model = {
        "pending_user_confirmation": [
            {"id": "pc-dup", "type": "credentials", "proposal": "first", "rationale": ""},
            {"id": "pc-dup", "type": "credentials", "proposal": "replay", "rationale": ""},
            {"id": "pc-3", "type": "credentials", "proposal": "unique", "rationale": ""},
        ],
    }
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(project, "problem_model")
    db.add(project)
    await db.commit()

    ctx = SweepContext(
        session=db, now=_utcnow(), mode="observe",
        max_projects=25, sample_limit=200, window_days=14,
    )
    results = await mis.check_problem_model_duplicate_id_sweep(ctx)
    flagged = [r for r in results if r.flagged]
    assert len(flagged) == 1, f"expected 1 flagged, got {results}"
    f = flagged[0]
    assert f.check == "problem_model_duplicate_id_sweep"
    assert f.subject == str(project.id)  # per-project, not "global"
    assert f.subject != "global"
    assert f.self_reported == 3   # raw length
    assert f.ground_truth == 2   # distinct ids: pc-dup + pc-3
    assert f.delta == 1           # one duplicate
    assert "pending_user_confirmation" in f.detail


@pytest.mark.asyncio
async def test_problem_model_dup_id_skipped_when_empty(db):
    """A project with an empty or absent problem_model → SKIPPED."""
    await _project(db)
    # problem_model stays None/default

    ctx = SweepContext(
        session=db, now=_utcnow(), mode="observe",
        max_projects=25, sample_limit=200, window_days=14,
    )
    results = await mis.check_problem_model_duplicate_id_sweep(ctx)
    assert all(r.status in (CheckStatus.SKIPPED,) for r in results)


@pytest.mark.asyncio
async def test_problem_model_dup_id_is_flag_only(db):
    """The check never mutates the problem_model collections it audits."""
    project = await _project(db)
    pm = {
        "pending_user_confirmation": [
            {"id": "dup", "type": "credentials", "proposal": "a", "rationale": ""},
            {"id": "dup", "type": "credentials", "proposal": "b", "rationale": ""},
        ],
    }
    project.problem_model = pm
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(project, "problem_model")
    db.add(project)
    await db.commit()

    ctx = SweepContext(
        session=db, now=_utcnow(), mode="observe",
        max_projects=25, sample_limit=200, window_days=14,
    )
    await mis.check_problem_model_duplicate_id_sweep(ctx)

    await db.refresh(project)
    # Collection unchanged — both duplicates still present.
    assert len(project.problem_model["pending_user_confirmation"]) == 2
