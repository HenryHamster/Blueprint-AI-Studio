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
    Alert,
    AlertSeverity,
    Artifact,
    ArtifactType,
    CriterionEvidence,
    Direction,
    EvergreenBacklogItem,
    MessageType,
    Project,
    ProjectMessage,
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
        "direction_cumulative_merge_invariant",
        "first_attempt_rate_denominator_coverage",
        "sr_catch_rate_cache_freshness",
        "acceptance_window_sub_scores_invariant",
        "undeclared_test_cochange_coverage_split",
        "acceptance_window_fraction_recompute",
        "evidence_claim_reconciliation",
        "saturated_direction_with_nonmerge_productivity",
        "capability_claim_reconciliation",
        "merged_pr_count_consistency",
        "criterion_evidence_count_integrity",
        "criterion_claim_reconciliation",
        "capability_proof_gate_gap",
        "capability_proof_trial_stall",
        "unmapped_acceptance_criteria",
        "research_quality_grader_pass_rate",
        "encrypted_field_plaintext_detector",
        "audience_routing_attestation",
        "operator_inbox_needs_action_liveness",
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
async def test_landed_vs_reported_samples_newest_by_created_at(db):
    """Verify the sweep samples by created_at (newest first), not by uuid (random).

    Creates MORE PR artifacts than sample_limit, spreads them across distinct
    created_at times, and places ghost merges among the newest. Asserts the
    sweep flags the newest ghost merges, proving created_at ordering is used.
    """
    from datetime import timedelta as td

    project = await _project(db)
    now = _utcnow()
    sample_limit = 5

    # Create 10 PR-artifact tasks spread across different created_at times
    # (twice the sample limit).
    # Older tasks (created_at earlier) are all landed.
    for i in range(6):
        t = Task(
            project_id=project.id,
            payloads=[{"type": "create"}],
            status=TaskStatus.COMPLETED,
            created_at=now - td(hours=100 - i),  # older: hours 100, 99, ..., 95
            context={"landed_on_integration": True, "pr_merged": True},
        )
        db.add(t)
        await db.commit()
        await db.refresh(t)
        art = Artifact(task_id=t.id, artifact_type=ArtifactType.PULL_REQUEST, created_at=t.created_at)
        db.add(art)
        await db.commit()

    # Newest tasks (created_at later) are ghost merges: PR artifact but NOT landed.
    # These should be sampled because they are newest (by created_at).
    for i in range(4):
        t = Task(
            project_id=project.id,
            payloads=[{"type": "create"}],
            status=TaskStatus.COMPLETED,
            created_at=now - td(hours=10 - i),  # newer: hours 10, 9, 8, 7
            context={"landed_on_integration": False},  # NOT landed
        )
        db.add(t)
        await db.commit()
        await db.refresh(t)
        art = Artifact(task_id=t.id, artifact_type=ArtifactType.PULL_REQUEST, created_at=t.created_at)
        db.add(art)
        await db.commit()

    # Run the sweep with sample_limit=5. If it orders by created_at correctly,
    # it should sample the 4 newest ghost merges + 1 older landed task (newest of that batch).
    # The result should flag the 4 ghost merges.
    ctx = SweepContext(
        session=db,
        now=now,
        mode="observe",
        max_projects=25,
        sample_limit=sample_limit,
        window_days=14,
    )
    results = await mis.check_landed_vs_reported_merges(ctx)
    flagged = [r for r in results if r.flagged]

    # Should find at least one FLAGGED result indicating ghost merges.
    assert len(flagged) >= 1, f"expected flagged result for ghost merges, got {results}"
    f = flagged[0]
    # Delta should reflect the 4 ghost merges (the newest ones that were sampled).
    assert f.delta >= 4, f"expected delta >= 4 ghost merges, got delta={f.delta} in {f}"


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


# --- Check: undeclared_test_cochange_coverage_split --------------------------


@pytest.mark.asyncio
async def test_undeclared_test_cochange_coverage_split():
    """Report the split of undeclared merges: inferred test co-change vs bare.

    A real merge-test verdict gating a CI-verified merge with both test and
    production files changed represents inferred test co-change (agents shipping
    tests). A bare-undeclared merge has either no test files or no production
    files. The check observes and reports the split per project.
    """
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
    async with Session() as db:
        project = await _project(db)

        # Case 1: Verified merge with both test and production files → inferred.
        t_inferred = Task(
            project_id=project.id,
            payloads=[{"type": "create"}],
            status=TaskStatus.COMPLETED,
            context={
                "landed_on_integration": True,
                "pr_merged": True,
                # CI-verified (no unverified_merge_reason): passes the verified check.
                # Both test and production files changed.
                "_pr_changed_files": [
                    "backend/testing/test_foo.py",  # test file
                    "backend/app/foo.py",            # production file
                ],
            },
        )
        db.add(t_inferred)
        await db.commit()
        await db.refresh(t_inferred)
        art_inferred = Artifact(
            task_id=t_inferred.id, artifact_type=ArtifactType.PULL_REQUEST
        )
        db.add(art_inferred)
        await db.commit()

        # Case 2: Verified merge but only test files (no production) → bare.
        t_only_test = Task(
            project_id=project.id,
            payloads=[{"type": "create"}],
            status=TaskStatus.COMPLETED,
            context={
                "landed_on_integration": True,
                "pr_merged": True,
                "_pr_changed_files": ["backend/testing/test_bar.py"],  # only test
            },
        )
        db.add(t_only_test)
        await db.commit()
        await db.refresh(t_only_test)
        art_only_test = Artifact(
            task_id=t_only_test.id, artifact_type=ArtifactType.PULL_REQUEST
        )
        db.add(art_only_test)
        await db.commit()

        # Case 3: Verified merge but only production files (no test) → bare.
        t_only_prod = Task(
            project_id=project.id,
            payloads=[{"type": "create"}],
            status=TaskStatus.COMPLETED,
            context={
                "landed_on_integration": True,
                "pr_merged": True,
                "_pr_changed_files": ["backend/app/baz.py"],  # only production
            },
        )
        db.add(t_only_prod)
        await db.commit()
        await db.refresh(t_only_prod)
        art_only_prod = Artifact(
            task_id=t_only_prod.id, artifact_type=ArtifactType.PULL_REQUEST
        )
        db.add(art_only_prod)
        await db.commit()

        # Case 4: No-CI gate fall-open (unverified merge) → bare (cannot infer).
        t_unverified = Task(
            project_id=project.id,
            payloads=[{"type": "create"}],
            status=TaskStatus.COMPLETED,
            context={
                "landed_on_integration": True,
                "pr_merged": True,
                # Unverified merge: has unverified_merge_reason in review_loop.
                "review_loop": {"unverified_merge_reason": "ci_setup_failed"},
                "_pr_changed_files": [
                    "backend/testing/test_fallopen.py",
                    "backend/app/fallopen.py",
                ],
            },
        )
        db.add(t_unverified)
        await db.commit()
        await db.refresh(t_unverified)
        art_unverified = Artifact(
            task_id=t_unverified.id, artifact_type=ArtifactType.PULL_REQUEST
        )
        db.add(art_unverified)
        await db.commit()

        # Run the check.
        ctx = SweepContext(
            session=db,
            now=_utcnow(),
            mode="observe",
            max_projects=25,
            sample_limit=200,
            window_days=14,
        )
        results = await mis.check_undeclared_test_cochange_coverage_split(ctx)

        # Expect exactly one result per project (since all tasks are in one project).
        assert len(results) >= 1, f"expected at least one result, got {results}"
        pid = str(project.id)
        per_project_results = [r for r in results if r.subject == pid]
        assert len(per_project_results) == 1, f"expected one result for project {pid}"

        result = per_project_results[0]
        assert result.check == "undeclared_test_cochange_coverage_split"
        assert result.status == CheckStatus.OK  # Observe-only, never flagged

        # Result metrics:
        # - self_reported = count with inferred test co-change (1: t_inferred)
        # - ground_truth = count bare-undeclared (3: t_only_test, t_only_prod, t_unverified)
        # - delta = total (4)
        assert result.self_reported == 1, f"expected 1 inferred, got {result.self_reported}"
        assert result.ground_truth == 3, f"expected 3 bare, got {result.ground_truth}"
        assert result.delta == 4, f"expected delta=4 total, got {result.delta}"
        assert "inferred test co-change" in result.detail
        assert "bare-undeclared" in result.detail

    await engine.dispose()


@pytest.mark.asyncio
async def test_undeclared_test_cochange_coverage_split_truncated_pr():
    """The sweep check classifies truncated PRs correctly given only the stored list.

    When ``_pr_changed_files`` is the truncated list (capped at 50 entries) and the
    test path falls beyond the truncation window, the sweep's current
    implementation scans the stored list directly without consulting the non-lossy
    summary fields (``_pr_changed_files_has_test`` / ``_pr_changed_files_has_prod``).
    This test documents the current behaviour: a >50-file PR whose test paths are
    beyond index 50 is classified as ``bare_undeclared`` even when the non-lossy
    summary is present.

    The acceptance_gate ``classify_merged_acceptance_context`` already prefers the
    summary fields for the same classification. When the sweep is updated to do the
    same, this test's assertion should be flipped: with summary fields present and
    ``_pr_changed_files_has_test=True, _pr_changed_files_has_prod=True``, the
    sweep should classify the merge as ``inferred_test_cochange`` (self_reported=1)
    instead of ``bare_undeclared`` (ground_truth=1).
    """
    from app.models import Artifact, ArtifactType

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
    async with Session() as db:
        project = await _project(db)

        # --- Case 1: Truncated PR (50 prod files), summary says test co-change ---
        # The stored list has only production files (the test file is beyond
        # index 50), so the current sweep implementation — which scans
        # _pr_changed_files directly — sees no test files and classifies as
        # bare_undeclared, despite _pr_changed_files_has_test being True.
        prod_50 = [f"backend/app/module_{i}.py" for i in range(50)]
        t_trunc = Task(
            project_id=project.id,
            payloads=[{"type": "create"}],
            status=TaskStatus.COMPLETED,
            context={
                "landed_on_integration": True,
                "pr_merged": True,
                "_pr_changed_files": prod_50,
                "_pr_changed_files_has_test": True,
                "_pr_changed_files_has_prod": True,
                "_pr_changed_files_truncated": True,
            },
        )
        db.add(t_trunc)
        await db.commit()
        await db.refresh(t_trunc)
        db.add(Artifact(task_id=t_trunc.id, artifact_type=ArtifactType.PULL_REQUEST))
        await db.commit()

        # --- Case 2: Truncated PR with NO summary fields (legacy shape) ---
        # The stored list has only production files, and no summary fields
        # exist to help. Also classified as bare_undeclared.
        prod_50_b = [f"backend/app/module_{i}.py" for i in range(50)]
        t_legacy = Task(
            project_id=project.id,
            payloads=[{"type": "create"}],
            status=TaskStatus.COMPLETED,
            context={
                "landed_on_integration": True,
                "pr_merged": True,
                "_pr_changed_files": prod_50_b,
            },
        )
        db.add(t_legacy)
        await db.commit()
        await db.refresh(t_legacy)
        db.add(Artifact(task_id=t_legacy.id, artifact_type=ArtifactType.PULL_REQUEST))
        await db.commit()

        # --- Case 3: Small PR (non-truncated) with summary fields — control ---
        # With both test and prod files in the stored list, the sweep correctly
        # classifies as inferred_test_cochange regardless of summary fields.
        t_small = Task(
            project_id=project.id,
            payloads=[{"type": "create"}],
            status=TaskStatus.COMPLETED,
            context={
                "landed_on_integration": True,
                "pr_merged": True,
                "_pr_changed_files": [
                    "backend/app/foo.py",
                    "backend/testing/test_foo.py",
                ],
                "_pr_changed_files_has_test": True,
                "_pr_changed_files_has_prod": True,
            },
        )
        db.add(t_small)
        await db.commit()
        await db.refresh(t_small)
        db.add(Artifact(task_id=t_small.id, artifact_type=ArtifactType.PULL_REQUEST))
        await db.commit()

        ctx = SweepContext(
            session=db,
            now=_utcnow(),
            mode="observe",
            max_projects=25,
            sample_limit=200,
            window_days=14,
        )
        results = await mis.check_undeclared_test_cochange_coverage_split(ctx)

        pid = str(project.id)
        per_project = [r for r in results if r.subject == pid]
        assert len(per_project) == 1
        result = per_project[0]
        assert result.check == "undeclared_test_cochange_coverage_split"
        assert result.status == CheckStatus.OK

        # Current behaviour: the sweep scans _pr_changed_files directly.
        # - t_trunc: 50 prod files, no test file in list → bare_undeclared
        # - t_legacy: same, no summary → bare_undeclared
        # - t_small: test + prod files in list → inferred_test_cochange
        #
        # When the sweep is updated to prefer the non-lossy summary fields
        # (mirroring classify_merged_acceptance_context), the assertion should
        # become: self_reported=2 (t_trunc + t_small), ground_truth=1 (t_legacy).
        assert result.self_reported == 1, (
            f"small PR with test+prod in stored list should be inferred; "
            f"got self_reported={result.self_reported}"
        )
        assert result.ground_truth == 2, (
            f"both truncated PRs should be bare_undeclared in current behaviour; "
            f"got ground_truth={result.ground_truth}"
        )
        assert result.delta == 3

    await engine.dispose()


# --- Check: acceptance_window_fraction_recompute -----------------------------


@pytest.mark.asyncio
async def test_acceptance_window_fraction_recompute_reports_nonzero(db):
    """Observe-only recompute reports a non-zero fraction + reason split on real data."""
    project = await _project(db)
    version, _criterion = await _open_version_with_criterion(
        db, project, text="acceptance window recompute"
    )

    # One inferred_test_cochange (verified) + two undeclared reasons in the window.
    inferred = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        status=TaskStatus.COMPLETED,
        context={
            "landed_on_integration": True,
            "pr_merged": True,
            "_pr_changed_files": [
                "backend/app/release_qualifier.py",
                "backend/testing/test_release_qualifier.py",
            ],
        },
    )
    prod_only = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        status=TaskStatus.COMPLETED,
        context={
            "landed_on_integration": True,
            "pr_merged": True,
            "_pr_changed_files": ["backend/app/release_qualifier.py"],
        },
    )
    no_files = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        status=TaskStatus.COMPLETED,
        context={"landed_on_integration": True, "pr_merged": True},
    )
    db.add_all([inferred, prod_only, no_files])
    await db.commit()

    ctx = SweepContext(
        session=db, now=_utcnow(), mode="observe", max_projects=25,
        sample_limit=200, window_days=14,
    )
    results = await mis.check_acceptance_window_fraction_recompute(ctx)

    pid = str(project.id)
    mine = [r for r in results if r.subject == pid]
    assert len(mine) == 1, f"expected one result for {pid}, got {results}"
    result = mine[0]
    assert result.check == "acceptance_window_fraction_recompute"
    assert result.status == CheckStatus.OK  # observe-only, never flagged
    # verified=1 numerator, write_total=3 denominator, undeclared (bare) delta=2.
    assert result.self_reported == 1
    assert result.ground_truth == 3
    assert result.delta == 2
    # The recomputed fraction is surfaced non-zero, with the reason split visible.
    assert "acceptance_window_fraction=0.333" in result.detail
    assert "inferred_test_cochange=1" in result.detail
    assert "no_test_prod_cochange=1" in result.detail
    assert "no_changed_files_context=1" in result.detail


@pytest.mark.asyncio
async def test_acceptance_window_fraction_recompute_skips_without_open_version(db):
    """No open version → SKIPPED, never a flag."""
    project = await _project(db)
    ctx = SweepContext(
        session=db, now=_utcnow(), mode="observe", max_projects=25,
        sample_limit=200, window_days=14,
    )
    results = await mis.check_acceptance_window_fraction_recompute(ctx)
    mine = [r for r in results if r.subject == str(project.id)]
    assert len(mine) == 1
    assert mine[0].status == CheckStatus.SKIPPED
    assert not mine[0].flagged


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
async def test_completion_debt_no_ci_gate_not_flagged_when_recent_verified_run(db):
    """last_unverified_reason is a permanent stamp (never cleared by verified runs).
    When the most recent gate outcome in recent_outcomes is verified, the obligation
    is no longer live and must NOT be flagged as an under-count."""
    project = await _project(db)
    project.merge_gate_runtime = {
        "last_unverified_reason": "verify_setup_failed",
        "last_unverified_at": "2026-01-01T00:00:00",
        "recent_outcomes": [
            {"at": "2026-01-01T00:00:00", "verified": False, "reason": "verify_setup_failed"},
            # Most recent: verified run — gate is now functioning, obligation not live.
            {"at": "2026-06-01T00:00:00", "verified": True, "reason": None},
        ],
    }
    flag_modified(project, "merge_gate_runtime")
    db.add(project)
    await db.commit()

    results = await mis.check_completion_debt_completeness(_ctx(db))
    # Must NOT be flagged — the most recent gate outcome was verified.
    assert not _flagged_for(results, str(project.id))


@pytest.mark.asyncio
async def test_completion_debt_no_ci_gate_still_flagged_when_last_outcome_unverified(db):
    """When the most recent recent_outcomes entry is still unverified, the obligation
    is live and must be flagged even if there was an earlier verified run."""
    project = await _project(db)
    project.merge_gate_runtime = {
        "last_unverified_reason": "verify_setup_failed",
        "recent_outcomes": [
            {"at": "2026-01-01T00:00:00", "verified": True, "reason": None},
            # Most recent: unverified — obligation is still live.
            {"at": "2026-06-01T00:00:00", "verified": False, "reason": "verify_setup_failed"},
        ],
    }
    flag_modified(project, "merge_gate_runtime")
    db.add(project)
    await db.commit()

    results = await mis.check_completion_debt_completeness(_ctx(db))
    pid_flags = _flagged_for(results, str(project.id))
    assert len(pid_flags) == 1
    assert f"no_ci_gate:{project.id}" in pid_flags[0].detail


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
async def test_rollup_child_landing_credit_samples_newest_by_created_at(db):
    """Verify the sweep samples by created_at (newest first), not by Task.id (uuid-random).

    Creates more rollup tasks than sample_limit. The oldest rollup pairs are properly
    credited (OK). The newest pair has a landed parent but an un-credited child (FLAGGED).
    With created_at ordering the newest tasks are sampled and the flag is detected.
    """
    from datetime import timedelta as td

    project = await _project(db)
    now = _utcnow()
    sample_limit = 4

    # 3 old OK rollup pairs (6 tasks): parent landed, child stamped — no flag.
    for i in range(3):
        old_pr = 100 + i
        parent_old = Task(
            project_id=project.id,
            payloads=[{"type": "create"}],
            status=TaskStatus.COMPLETED,
            created_at=now - td(hours=200 - i * 4),
            context={
                "_is_rollup_review": True,
                "rollup_pr_number": old_pr,
                "landed_on_integration": True,
                "pr_merged": True,
            },
        )
        db.add(parent_old)
        await db.commit()
        await db.refresh(parent_old)
        child_old = Task(
            project_id=project.id,
            payloads=[{"type": "create"}],
            status=TaskStatus.COMPLETED,
            created_at=now - td(hours=200 - i * 4 - 1),
            context={
                "rollup_pr_number": old_pr,
                "pr_merged": True,
                "landed_on_integration": True,
            },
        )
        db.add(child_old)
        await db.commit()

    # 1 new FLAGGED pair (2 tasks): parent landed, child missing landing stamp.
    # Most recent created_at — must be picked by created_at-ordered sampling.
    new_rollup_pr = 200
    parent_new = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        status=TaskStatus.COMPLETED,
        created_at=now - td(hours=5),
        context={
            "_is_rollup_review": True,
            "rollup_pr_number": new_rollup_pr,
            "landed_on_integration": True,
            "pr_merged": True,
        },
    )
    db.add(parent_new)
    await db.commit()
    await db.refresh(parent_new)
    child_new = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        status=TaskStatus.COMPLETED,
        created_at=now - td(hours=4),
        context={
            "rollup_pr_number": new_rollup_pr,
            "pr_merged": True,
            "landed_on_integration": False,  # under-credited
        },
    )
    db.add(child_new)
    await db.commit()

    # 8 tasks total; sample_limit=4 picks the 4 newest by created_at:
    # child_new (now-4h), parent_new (now-5h), and the 2 newest old tasks.
    # The new FLAGGED pair is guaranteed in the sample → FLAGGED.
    ctx = SweepContext(
        session=db,
        now=now,
        mode="observe",
        max_projects=25,
        sample_limit=sample_limit,
        window_days=14,
    )
    results = await mis.check_rollup_child_landing_credit(ctx)
    flagged = [r for r in results if r.flagged]
    assert len(flagged) >= 1, (
        f"expected flagged result for newest under-credited child, got {results}"
    )
    assert flagged[0].delta >= 1


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


# --- Check: direction_cumulative_merge_invariant ----------------------------


@pytest.mark.asyncio
async def test_direction_cumulative_merge_invariant_flags_verified_exceeds_merged(db):
    """A direction where lifetime verified > merged must be flagged."""
    project = await _project(db)
    project.directions = [
        {
            "id": "d-inv-001",
            "research_history": [
                {
                    "task_id": "bbbbbbbb-0000-0000-0000-000000000001",
                    "prs_merged": 2,
                    "verified_prs_merged": 3,  # violates: verified (3) > merged (2)
                }
            ],
        }
    ]
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(project, "directions")
    db.add(project)
    await db.commit()

    ctx = SweepContext(
        session=db, now=_utcnow(), mode="observe",
        max_projects=25, sample_limit=200, window_days=14,
    )
    results = await mis.check_direction_cumulative_merge_invariant(ctx)
    flagged = [r for r in results if r.flagged]
    assert len(flagged) == 1, f"expected 1 flagged, got {results}"
    f = flagged[0]
    assert f.check == "direction_cumulative_merge_invariant"
    assert f.delta >= 1
    assert "verified" in f.detail


@pytest.mark.asyncio
async def test_direction_cumulative_merge_invariant_ok_when_consistent(db):
    """A direction with internally-consistent lifetime totals returns OK."""
    project = await _project(db)
    project.directions = [
        {
            "id": "d-ok-001",
            "research_history": [
                {
                    "task_id": "cccccccc-0000-0000-0000-000000000001",
                    "prs_merged": 5,
                    "verified_prs_merged": 4,
                    "acceptance_proven_merged": 2,
                    "acceptance_declared_merged": 3,
                }
            ],
            "history_aggregate": {
                "total_prs_merged": 10,
                "total_verified_prs_merged": 9,
                "total_acceptance_proven_merged": 3,
                "total_acceptance_declared_merged": 5,
            },
        }
    ]
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(project, "directions")
    db.add(project)
    await db.commit()

    ctx = SweepContext(
        session=db, now=_utcnow(), mode="observe",
        max_projects=25, sample_limit=200, window_days=14,
    )
    results = await mis.check_direction_cumulative_merge_invariant(ctx)
    assert all(not r.flagged for r in results)
    assert any(r.status == CheckStatus.OK for r in results)


@pytest.mark.asyncio
async def test_direction_cumulative_merge_invariant_skips_no_directions(db):
    """A project with no directions or empty research_history returns SKIPPED."""
    await _project(db)
    ctx = SweepContext(
        session=db, now=_utcnow(), mode="observe",
        max_projects=25, sample_limit=200, window_days=14,
    )
    results = await mis.check_direction_cumulative_merge_invariant(ctx)
    assert len(results) == 1
    assert results[0].status == CheckStatus.SKIPPED


# --- Check: first_attempt_rate_denominator_coverage -------------------------


@pytest.mark.asyncio
async def test_fa_rate_denominator_flags_low_coverage(db):
    """When <50% of eligible tasks have _first_attempt_succeeded, FLAGGED."""
    project = await _project(db)
    now = _utcnow()
    # 8 eligible tasks, only 2 have the flag → 25% coverage < 50% threshold
    for i in range(8):
        ctx_data = {"_first_attempt_succeeded": True} if i < 2 else {}
        db.add(Task(
            project_id=project.id,
            payloads=[{"type": "create"}],
            status=TaskStatus.COMPLETED,
            created_at=now - timedelta(days=1),
            context=ctx_data,
        ))
    await db.commit()

    ctx = SweepContext(
        session=db, now=now, mode="observe",
        max_projects=25, sample_limit=200, window_days=14,
    )
    results = await mis.check_first_attempt_rate_denominator_coverage(ctx)
    flagged = [r for r in results if r.flagged]
    assert len(flagged) == 1, f"expected 1 flagged, got {results}"
    f = flagged[0]
    assert f.check == "first_attempt_rate_denominator_coverage"
    assert f.self_reported == 2
    assert f.ground_truth == 8
    assert f.delta == 6
    assert "_first_attempt_succeeded" in f.detail


@pytest.mark.asyncio
async def test_fa_rate_denominator_ok_when_high_coverage(db):
    """When >=50% of eligible tasks have _first_attempt_succeeded, OK."""
    project = await _project(db)
    now = _utcnow()
    for i in range(6):
        ctx_data = {"_first_attempt_succeeded": i < 5}  # 5/6 = 83%
        db.add(Task(
            project_id=project.id,
            payloads=[{"type": "create"}],
            status=TaskStatus.COMPLETED,
            created_at=now - timedelta(days=1),
            context=ctx_data,
        ))
    await db.commit()

    ctx = SweepContext(
        session=db, now=now, mode="observe",
        max_projects=25, sample_limit=200, window_days=14,
    )
    results = await mis.check_first_attempt_rate_denominator_coverage(ctx)
    assert all(not r.flagged for r in results)
    assert any(r.status == CheckStatus.OK for r in results)


@pytest.mark.asyncio
async def test_fa_rate_denominator_skips_non_eligible_types(db):
    """research/review/strategic tasks do not count toward FA coverage."""
    project = await _project(db)
    now = _utcnow()
    for ttype in ("research", "review", "strategic"):
        db.add(Task(
            project_id=project.id,
            payloads=[{"type": ttype}],
            status=TaskStatus.COMPLETED,
            created_at=now - timedelta(days=1),
            context={},
        ))
    await db.commit()

    ctx = SweepContext(
        session=db, now=now, mode="observe",
        max_projects=25, sample_limit=200, window_days=14,
    )
    results = await mis.check_first_attempt_rate_denominator_coverage(ctx)
    assert len(results) == 1
    assert results[0].status == CheckStatus.SKIPPED


# --- Check: sr_catch_rate_cache_freshness -----------------------------------


@pytest.mark.asyncio
async def test_sr_cache_freshness_flags_stale_cache(db):
    """A sr_catch_rate_trends cache older than _SR_TREND_STALENESS_DAYS must be flagged."""
    from app.sr_review_quality_metrics import _SR_TREND_STALENESS_DAYS
    project = await _project(db)
    stale_date = (_utcnow() - timedelta(days=_SR_TREND_STALENESS_DAYS + 2)).isoformat()
    project.design_doc = {"sr_catch_rate_trends": {"computed_at": stale_date, "categories": {}}}
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(project, "design_doc")
    db.add(project)
    await db.commit()

    ctx = SweepContext(
        session=db, now=_utcnow(), mode="observe",
        max_projects=25, sample_limit=200, window_days=14,
    )
    results = await mis.check_sr_catch_rate_cache_freshness(ctx)
    flagged = [r for r in results if r.flagged]
    assert len(flagged) == 1, f"expected 1 flagged, got {results}"
    f = flagged[0]
    assert f.check == "sr_catch_rate_cache_freshness"
    assert "stale" in f.detail or "old" in f.detail


@pytest.mark.asyncio
async def test_sr_cache_freshness_ok_when_fresh(db):
    """A recently-computed sr_catch_rate_trends cache returns OK."""
    project = await _project(db)
    fresh_date = (_utcnow() - timedelta(hours=12)).isoformat()
    project.design_doc = {"sr_catch_rate_trends": {"computed_at": fresh_date, "categories": {}}}
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(project, "design_doc")
    db.add(project)
    await db.commit()

    ctx = SweepContext(
        session=db, now=_utcnow(), mode="observe",
        max_projects=25, sample_limit=200, window_days=14,
    )
    results = await mis.check_sr_catch_rate_cache_freshness(ctx)
    assert all(not r.flagged for r in results)
    assert any(r.status == CheckStatus.OK for r in results)


@pytest.mark.asyncio
async def test_sr_cache_freshness_skips_when_no_cache(db):
    """A project without sr_catch_rate_trends returns SKIPPED."""
    await _project(db)
    ctx = SweepContext(
        session=db, now=_utcnow(), mode="observe",
        max_projects=25, sample_limit=200, window_days=14,
    )
    results = await mis.check_sr_catch_rate_cache_freshness(ctx)
    assert all(r.status == CheckStatus.SKIPPED for r in results)


# --- Pure helper: acceptance_window_sub_scores_violations -------------------


def test_acceptance_window_violations_empty_dict_returns_no_violations():
    """An empty sub_scores dict has no acceptance_window fields — fail-safe: []."""
    from app.acceptance_gate import acceptance_window_sub_scores_violations

    assert acceptance_window_sub_scores_violations({}) == []


def test_acceptance_window_violations_legacy_sub_scores_no_violations():
    """A sub_scores dict with no acceptance_window_* keys is a legacy verdict: []."""
    from app.acceptance_gate import acceptance_window_sub_scores_violations

    legacy = {"audit_score": 0.9, "test_erosion": 0.8}
    assert acceptance_window_sub_scores_violations(legacy) == []


def test_acceptance_window_violations_three_key_intermediate_schema_no_violations():
    """Pre-fix 3-key schema (total/verified/fraction without write_total/total_all_merges)
    must not produce violations — the new population keys are absent so the check skips."""
    from app.acceptance_gate import acceptance_window_sub_scores_violations

    # This is the schema written by pre-fix code: healthy values but no population keys.
    three_key = {
        "acceptance_window_total": 346,
        "acceptance_window_verified": 40,
        "acceptance_window_fraction": 40 / 346,
    }
    assert acceptance_window_sub_scores_violations(three_key) == []


def test_acceptance_window_violations_consistent_fields_no_violations():
    """Consistent acceptance_window fields produce no violations."""
    from app.acceptance_gate import acceptance_window_sub_scores_violations

    sub_scores = {
        "acceptance_window_verified": 2,
        "acceptance_window_write_total": 4,
        "acceptance_window_total_all_merges": 6,
        "acceptance_window_fraction": 2 / 4,  # 0.5
        "acceptance_window_total": 4,
    }
    assert acceptance_window_sub_scores_violations(sub_scores) == []


def test_acceptance_window_violations_ignores_additive_breakdown_keys():
    """The additive nested breakdown keys never trip the sub-scores invariant."""
    from app.acceptance_gate import acceptance_window_sub_scores_violations

    sub_scores = {
        "acceptance_window_verified": 1,
        "acceptance_window_write_total": 4,
        "acceptance_window_total_all_merges": 4,
        "acceptance_window_fraction": 1 / 4,
        "acceptance_window_total": 4,
        # New additive nested keys — must be inert to the invariant guard.
        "acceptance_window_buckets": {
            "proven": 0,
            "satisfied_unproven": 0,
            "inferred_test_cochange": 1,
            "declared_unsatisfied": 0,
            "undeclared": 3,
            "merged_unverified_degraded": 0,
            "malformed": 0,
        },
        "acceptance_window_undeclared_reasons": {
            "not_ci_verified": 1,
            "no_changed_files_context": 1,
            "no_test_prod_cochange": 1,
        },
    }
    assert acceptance_window_sub_scores_violations(sub_scores) == []


def test_acceptance_window_violations_verified_exceeds_write_total():
    """verified > write_total is a violation (the mixed-population bug pattern)."""
    from app.acceptance_gate import acceptance_window_sub_scores_violations

    sub_scores = {
        "acceptance_window_verified": 5,
        "acceptance_window_write_total": 3,
        "acceptance_window_total_all_merges": 10,
        "acceptance_window_fraction": 5 / 3,
    }
    viols = acceptance_window_sub_scores_violations(sub_scores)
    assert any("verified" in v and "write_total" in v for v in viols), viols


def test_acceptance_window_violations_write_total_exceeds_all_merges():
    """write_total > total_all_merges is a violation."""
    from app.acceptance_gate import acceptance_window_sub_scores_violations

    sub_scores = {
        "acceptance_window_verified": 2,
        "acceptance_window_write_total": 10,
        "acceptance_window_total_all_merges": 5,
        "acceptance_window_fraction": 2 / 10,
    }
    viols = acceptance_window_sub_scores_violations(sub_scores)
    assert any("write_total" in v and "total_all_merges" in v for v in viols), viols


def test_acceptance_window_violations_fraction_mismatch():
    """A stored fraction that doesn't match verified/write_total is a violation."""
    from app.acceptance_gate import acceptance_window_sub_scores_violations

    sub_scores = {
        "acceptance_window_verified": 2,
        "acceptance_window_write_total": 4,
        "acceptance_window_total_all_merges": 6,
        "acceptance_window_fraction": 0.99,  # should be 0.5
    }
    viols = acceptance_window_sub_scores_violations(sub_scores)
    assert any("fraction" in v for v in viols), viols


def test_acceptance_window_violations_fraction_none_but_write_total_nonzero():
    """fraction=None with write_total > 0 is a violation."""
    from app.acceptance_gate import acceptance_window_sub_scores_violations

    sub_scores = {
        "acceptance_window_verified": 0,
        "acceptance_window_write_total": 3,
        "acceptance_window_total_all_merges": 5,
        "acceptance_window_fraction": None,
    }
    viols = acceptance_window_sub_scores_violations(sub_scores)
    assert any("None" in v or "fraction" in v for v in viols), viols


def test_acceptance_window_violations_fraction_not_none_but_write_total_zero():
    """fraction is not None with write_total=0 is a violation."""
    from app.acceptance_gate import acceptance_window_sub_scores_violations

    sub_scores = {
        "acceptance_window_verified": 0,
        "acceptance_window_write_total": 0,
        "acceptance_window_total_all_merges": 5,
        "acceptance_window_fraction": 0.0,
    }
    viols = acceptance_window_sub_scores_violations(sub_scores)
    assert any("fraction" in v and "None" in v for v in viols), viols


def test_acceptance_window_violations_write_total_zero_fraction_none_ok():
    """write_total=0 with fraction=None is the correct None state — no violation."""
    from app.acceptance_gate import acceptance_window_sub_scores_violations

    sub_scores = {
        "acceptance_window_verified": 0,
        "acceptance_window_write_total": 0,
        "acceptance_window_total_all_merges": 5,
        "acceptance_window_fraction": None,
    }
    assert acceptance_window_sub_scores_violations(sub_scores) == []


# --- Check: acceptance_window_sub_scores_invariant --------------------------


async def _version_with_sub_scores(db, project, sub_scores: dict) -> ReleaseVersion:
    """Create an open ReleaseVersion with the given last_qualification sub_scores."""
    version = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        status="open",
        last_qualification={"status": "not_promoted", "sub_scores": sub_scores},
    )
    db.add(version)
    await db.commit()
    await db.refresh(version)
    return version


@pytest.mark.asyncio
async def test_acceptance_window_sub_scores_invariant_ok_when_consistent(db):
    """A version with consistent acceptance_window sub_scores returns OK."""
    project = await _project(db)
    await _version_with_sub_scores(
        db,
        project,
        {
            "acceptance_window_verified": 2,
            "acceptance_window_write_total": 4,
            "acceptance_window_total_all_merges": 6,
            "acceptance_window_fraction": 2 / 4,
            "acceptance_window_total": 4,
        },
    )
    ctx = SweepContext(
        session=db, now=_utcnow(), mode="observe",
        max_projects=25, sample_limit=200, window_days=14,
    )
    results = await mis.check_acceptance_window_sub_scores_invariant(ctx)
    assert any(r.status == CheckStatus.OK for r in results), results
    assert not any(r.flagged for r in results)


@pytest.mark.asyncio
async def test_acceptance_window_sub_scores_invariant_flags_verified_exceeds_write_total(db):
    """A version where verified > write_total is flagged (mixed-population bug)."""
    project = await _project(db)
    await _version_with_sub_scores(
        db,
        project,
        {
            "acceptance_window_verified": 100,
            "acceptance_window_write_total": 5,
            "acceptance_window_total_all_merges": 346,
            "acceptance_window_fraction": 100 / 5,
            "acceptance_window_total": 5,
        },
    )
    ctx = SweepContext(
        session=db, now=_utcnow(), mode="observe",
        max_projects=25, sample_limit=200, window_days=14,
    )
    results = await mis.check_acceptance_window_sub_scores_invariant(ctx)
    flagged = [r for r in results if r.flagged]
    assert len(flagged) == 1, results
    assert "verified" in flagged[0].detail or "write_total" in flagged[0].detail


@pytest.mark.asyncio
async def test_acceptance_window_sub_scores_invariant_skips_three_key_intermediate_schema(db):
    """A version with the pre-fix 3-key schema (no write_total/total_all_merges) must be
    SKIPPED rather than flagged — otherwise healthy in-flight ReleaseVersions emit false
    FLAGGED warn results until they are re-qualified with post-fix code."""
    project = await _project(db)
    await _version_with_sub_scores(
        db,
        project,
        {
            "acceptance_window_total": 346,
            "acceptance_window_verified": 40,
            "acceptance_window_fraction": 40 / 346,
        },
    )
    ctx = SweepContext(
        session=db, now=_utcnow(), mode="observe",
        max_projects=25, sample_limit=200, window_days=14,
    )
    results = await mis.check_acceptance_window_sub_scores_invariant(ctx)
    assert not any(r.flagged for r in results), results
    assert any(r.status == CheckStatus.SKIPPED for r in results), results


@pytest.mark.asyncio
async def test_acceptance_window_sub_scores_invariant_flags_write_total_exceeds_all_merges(db):
    """A version where write_total > total_all_merges is flagged."""
    project = await _project(db)
    await _version_with_sub_scores(
        db,
        project,
        {
            "acceptance_window_verified": 2,
            "acceptance_window_write_total": 50,
            "acceptance_window_total_all_merges": 10,
            "acceptance_window_fraction": 2 / 50,
            "acceptance_window_total": 50,
        },
    )
    ctx = SweepContext(
        session=db, now=_utcnow(), mode="observe",
        max_projects=25, sample_limit=200, window_days=14,
    )
    results = await mis.check_acceptance_window_sub_scores_invariant(ctx)
    flagged = [r for r in results if r.flagged]
    assert len(flagged) == 1, results
    assert "write_total" in flagged[0].detail or "total_all_merges" in flagged[0].detail


@pytest.mark.asyncio
async def test_acceptance_window_sub_scores_invariant_flags_fraction_mismatch(db):
    """A stored fraction that doesn't match verified/write_total is flagged."""
    project = await _project(db)
    await _version_with_sub_scores(
        db,
        project,
        {
            "acceptance_window_verified": 2,
            "acceptance_window_write_total": 4,
            "acceptance_window_total_all_merges": 6,
            "acceptance_window_fraction": 0.99,  # should be 0.5
            "acceptance_window_total": 4,
        },
    )
    ctx = SweepContext(
        session=db, now=_utcnow(), mode="observe",
        max_projects=25, sample_limit=200, window_days=14,
    )
    results = await mis.check_acceptance_window_sub_scores_invariant(ctx)
    flagged = [r for r in results if r.flagged]
    assert len(flagged) == 1, results
    assert "fraction" in flagged[0].detail


@pytest.mark.asyncio
async def test_acceptance_window_sub_scores_invariant_skips_legacy_verdict(db):
    """A version with no acceptance_window fields in sub_scores → SKIPPED."""
    project = await _project(db)
    version = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        status="open",
        last_qualification={
            "status": "not_promoted",
            "sub_scores": {"audit_score": 0.9, "test_erosion": 0.8},
        },
    )
    db.add(version)
    await db.commit()

    ctx = SweepContext(
        session=db, now=_utcnow(), mode="observe",
        max_projects=25, sample_limit=200, window_days=14,
    )
    results = await mis.check_acceptance_window_sub_scores_invariant(ctx)
    assert all(r.status == CheckStatus.SKIPPED for r in results), results


@pytest.mark.asyncio
async def test_acceptance_window_sub_scores_invariant_skips_no_last_qualification(db):
    """A version with no last_qualification at all → SKIPPED (no acceptance_window keys)."""
    project = await _project(db)
    version = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        status="open",
        last_qualification=None,
    )
    db.add(version)
    await db.commit()

    ctx = SweepContext(
        session=db, now=_utcnow(), mode="observe",
        max_projects=25, sample_limit=200, window_days=14,
    )
    results = await mis.check_acceptance_window_sub_scores_invariant(ctx)
    assert all(r.status == CheckStatus.SKIPPED for r in results), results


@pytest.mark.asyncio
async def test_acceptance_window_sub_scores_invariant_skips_no_open_versions(db):
    """When there are no open ReleaseVersion rows → global SKIPPED."""
    await _project(db)
    # No ReleaseVersion row added.
    ctx = SweepContext(
        session=db, now=_utcnow(), mode="observe",
        max_projects=25, sample_limit=200, window_days=14,
    )
    results = await mis.check_acceptance_window_sub_scores_invariant(ctx)
    assert len(results) == 1
    assert results[0].status == CheckStatus.SKIPPED
    assert results[0].subject == "global"


# --- evidence_claim_reconciliation ------------------------------------------


async def _project_with_gap_evidence_ref(
    db, *, ref, proven=True, kind="named_test", status="active"
) -> Project:
    """A project whose problem_model has one active gap carrying one evidence_ref."""
    pm = {
        "gaps": [
            {
                "id": "g-ev-1",
                "name": "evidence gap",
                "status": status,
                "evidence_refs": [{"kind": kind, "ref": ref, "proven": proven}],
            }
        ]
    }
    p = Project(name="mi-ev", user_id="u1", problem_model=pm)
    db.add(p)
    await db.commit()
    await db.refresh(p)
    return p


def _ev_ctx(db) -> SweepContext:
    return SweepContext(
        session=db,
        now=_utcnow(),
        mode="observe",
        max_projects=25,
        sample_limit=200,
        window_days=14,
    )


@pytest.mark.asyncio
async def test_evidence_claim_reconciliation_flags_stale_gap_evidence(db, monkeypatch):
    """A proven, path-backed evidence_ref whose file is REMOVED at HEAD (the path
    helper returns a definite False) flags the gap on the observability surface —
    WITHOUT mutating the durable gap — while an indeterminate (None) result does
    not (companion assertion below)."""
    import app.source_ref_validation as srv

    project = await _project_with_gap_evidence_ref(
        db, ref="backend/testing/test_foo.py"
    )

    async def _removed(path, *args, **kwargs):
        return False  # path definitely removed at HEAD → proof contradicted

    monkeypatch.setattr(srv, "_path_exists_at_head", _removed)

    # Run in observe mode via the full sweep so we exercise the observability
    # surface (recent_flags), not just the check function in isolation.
    _rc["metrics_integrity_sweep_mode"] = "observe"
    tick = await run_metrics_integrity_sweep(db, now=_utcnow())
    assert tick is not None

    snap = get_metrics_integrity_observability()
    ev_flags = [
        f
        for f in snap["recent_flags"]
        if f["check"] == "evidence_claim_reconciliation"
    ]
    assert len(ev_flags) == 1, snap["recent_flags"]
    flag = ev_flags[0]
    assert flag["subject"] == str(project.id)
    assert flag["self_reported"] == 1  # one proven path-backed ref
    assert flag["ground_truth"] == 0  # none still present at HEAD
    assert flag["delta"] == 1
    assert "removed at HEAD" in flag["detail"]

    # Observe mode is side-effect-free: durable gap untouched, nothing enrolled.
    await db.refresh(project)
    gap = project.problem_model["gaps"][0]
    assert gap["evidence_refs"][0]["proven"] is True
    assert gap["status"] == "active"
    enrolled = (await db.execute(select(EvergreenBacklogItem))).scalars().all()
    assert enrolled == []


@pytest.mark.asyncio
async def test_evidence_claim_reconciliation_indeterminate_not_flagged(db, monkeypatch):
    """CRITICAL HONESTY RULE: a None (git/API indeterminate) path check must NEVER
    be laundered into 'removed' — the gap is not flagged."""
    import app.source_ref_validation as srv

    await _project_with_gap_evidence_ref(db, ref="backend/testing/test_foo.py")

    async def _indeterminate(path, *args, **kwargs):
        return None  # no checkout / timeout / rate limit — indeterminate

    monkeypatch.setattr(srv, "_path_exists_at_head", _indeterminate)

    results = await mis.check_evidence_claim_reconciliation(_ev_ctx(db))
    assert all(not r.flagged for r in results), results
    ok = [r for r in results if r.status == CheckStatus.OK]
    assert ok, results
    # Proven ref still counts as present ground truth (never laundered to stale).
    assert ok[0].ground_truth == 1


@pytest.mark.asyncio
async def test_evidence_claim_reconciliation_ok_when_ref_present(db, monkeypatch):
    """A proven ref whose backing file still exists (True) reconciles OK."""
    import app.source_ref_validation as srv

    await _project_with_gap_evidence_ref(db, ref="backend/testing/test_foo.py")

    async def _present(path, *args, **kwargs):
        return True

    monkeypatch.setattr(srv, "_path_exists_at_head", _present)

    results = await mis.check_evidence_claim_reconciliation(_ev_ctx(db))
    assert all(not r.flagged for r in results)
    ok = [r for r in results if r.status == CheckStatus.OK]
    assert ok and ok[0].self_reported == 1 and ok[0].ground_truth == 1


@pytest.mark.asyncio
async def test_evidence_claim_reconciliation_never_mutates_durable_gap(db, monkeypatch):
    """The flag-only guarantee: even the check function in isolation performs
    zero writes to the durable problem_model gap."""
    import app.source_ref_validation as srv

    project = await _project_with_gap_evidence_ref(
        db, ref="backend/testing/test_foo.py"
    )
    before = project.problem_model["gaps"][0]["evidence_refs"][0].copy()

    async def _removed(path, *args, **kwargs):
        return False

    monkeypatch.setattr(srv, "_path_exists_at_head", _removed)

    await mis.check_evidence_claim_reconciliation(_ev_ctx(db))

    await db.refresh(project)
    after = project.problem_model["gaps"][0]["evidence_refs"][0]
    assert after == before  # ref, proven flag, kind all untouched


@pytest.mark.asyncio
async def test_evidence_claim_reconciliation_closed_gap_unproven_flagged(db, monkeypatch):
    """A gap that CLAIMS closure but carries only unproven evidence is a
    'closed but unproven' contradiction — flagged even with no path removal."""
    import app.source_ref_validation as srv

    await _project_with_gap_evidence_ref(
        db, ref="backend/testing/test_foo.py", proven=False, status="closed"
    )

    async def _present(path, *args, **kwargs):
        return True  # nothing removed — the flag comes from the claim contradiction

    monkeypatch.setattr(srv, "_path_exists_at_head", _present)

    results = await mis.check_evidence_claim_reconciliation(_ev_ctx(db))
    flagged = [r for r in results if r.flagged]
    assert len(flagged) == 1, results
    assert "claims closed" in flagged[0].detail


@pytest.mark.asyncio
async def test_evidence_claim_reconciliation_skips_gaps_without_refs(db):
    """A gap carrying no structured evidence_refs is skipped (nothing to
    reconcile) — no flag, no error."""
    pm = {"gaps": [{"id": "g-1", "name": "plain gap", "status": "active"}]}
    p = Project(name="mi-ev-none", user_id="u1", problem_model=pm)
    db.add(p)
    await db.commit()

    results = await mis.check_evidence_claim_reconciliation(_ev_ctx(db))
    assert all(not r.flagged for r in results)
    assert any(
        r.status == CheckStatus.SKIPPED and "no gaps carry" in r.detail
        for r in results
    )


# --- Check: saturated_direction_with_nonmerge_productivity -------------------


@pytest.mark.asyncio
async def test_flags_saturated_direction_with_recorded_nonmerge_productivity(db):
    """A direction marked possibly_saturated but with recent non-merge productive
    outcomes (verified merges, acceptance evidence) is flagged — the saturation
    marker is stale or productivity was not properly credited.

    This is the named acceptance test validating that the guard check detects
    when the self-reported saturation state diverges from actual recorded
    productivity, the regression class that the fix (#13378) closes.
    """
    project = await _project(db)

    # Create a direction marked possibly_saturated with recent non-merge productivity.
    # Non-merge productivity: verified merges + acceptance evidence without new tasks.
    project.directions = [
        {
            "id": "d-saturated-productive",
            "possibly_saturated": True,
            "last_cycle_productive": False,  # marked unproductive
            "consecutive_unproductive_cycles": 4,  # multiple consecutive unproductive
            "research_history": [
                {
                    "task_id": "xxxxxxxx-0000-0000-0000-000000000001",
                    "verified_prs_merged": 1,  # CI-verified merge verdict
                    "acceptance_proven_merged": 1,  # acceptance evidence recorded
                    "tasks_created": 0,  # but no child tasks spawned (non-merge productivity)
                },
                {
                    "task_id": "xxxxxxxx-0000-0000-0000-000000000002",
                    "verified_prs_merged": 0,
                    "tasks_created": 0,
                },
            ],
        }
    ]
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(project, "directions")
    db.add(project)
    await db.commit()

    # Run the check.
    ctx = SweepContext(
        session=db,
        now=_utcnow(),
        mode="observe",
        max_projects=25,
        sample_limit=200,
        window_days=14,
    )
    results = await mis.check_saturated_direction_with_nonmerge_productivity(ctx)

    # Verify the check flags this contradiction.
    flagged = [r for r in results if r.flagged]
    assert len(flagged) == 1, f"expected 1 flagged result, got {results}"
    f = flagged[0]
    assert f.check == "saturated_direction_with_nonmerge_productivity"
    assert f.status == CheckStatus.FLAGGED
    assert f.subject == str(project.id)
    assert f.delta == 1  # one flagged direction
    assert "possibly_saturated" in f.detail
    assert "verified" in f.detail or "proven" in f.detail
    assert "d-saturated-productive" in f.detail


@pytest.mark.asyncio
async def test_saturated_direction_with_nonmerge_productivity_ok_when_no_productivity(db):
    """A direction marked possibly_saturated with no recorded productivity → OK.

    The saturation flag is consistent with the recorded state when no
    non-merge outcomes exist in the recent history.
    """
    project = await _project(db)
    project.directions = [
        {
            "id": "d-truly-saturated",
            "possibly_saturated": True,
            "research_history": [
                {
                    "task_id": "yyyyyyyy-0000-0000-0000-000000000001",
                    "verified_prs_merged": 0,  # no verification
                    "acceptance_proven_merged": 0,  # no acceptance evidence
                    "tasks_created": 0,  # no child tasks
                },
            ],
        }
    ]
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(project, "directions")
    db.add(project)
    await db.commit()

    ctx = SweepContext(
        session=db, now=_utcnow(), mode="observe",
        max_projects=25, sample_limit=200, window_days=14,
    )
    results = await mis.check_saturated_direction_with_nonmerge_productivity(ctx)

    # No contradiction → not flagged.
    assert all(not r.flagged for r in results)
    # Should be SKIPPED since the direction is saturated but has no productivity.
    skipped = [r for r in results if r.status == CheckStatus.SKIPPED]
    assert len(skipped) == 1


@pytest.mark.asyncio
async def test_saturated_direction_with_nonmerge_productivity_skips_non_saturated(db):
    """Directions not marked possibly_saturated are skipped — they are not audited."""
    project = await _project(db)
    project.directions = [
        {
            "id": "d-active",
            "possibly_saturated": False,  # not saturated
            "research_history": [
                {
                    "verified_prs_merged": 3,  # has productivity
                    "acceptance_proven_merged": 2,
                    "tasks_created": 0,
                },
            ],
        }
    ]
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(project, "directions")
    db.add(project)
    await db.commit()

    ctx = SweepContext(
        session=db, now=_utcnow(), mode="observe",
        max_projects=25, sample_limit=200, window_days=14,
    )
    results = await mis.check_saturated_direction_with_nonmerge_productivity(ctx)

    # Non-saturated directions are not audited — result is SKIPPED.
    assert len(results) == 1
    assert results[0].status == CheckStatus.SKIPPED
    assert "no saturated directions" in results[0].detail


@pytest.mark.asyncio
async def test_saturated_direction_with_nonmerge_productivity_checks_acceptance_verified(db):
    """A saturated direction with acceptance_verified_merged > 0 is flagged.

    Acceptance evidence (both proven and verified) counts as non-merge
    productivity that contradicts the saturation flag.
    """
    project = await _project(db)
    project.directions = [
        {
            "id": "d-saturated-verified-acceptance",
            "possibly_saturated": True,
            "research_history": [
                {
                    "task_id": "zzzzzzzz-0000-0000-0000-000000000001",
                    "verified_prs_merged": 0,  # no merge verification
                    "acceptance_verified_merged": 2,  # verified acceptance evidence
                    "tasks_created": 0,
                },
            ],
        }
    ]
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(project, "directions")
    db.add(project)
    await db.commit()

    ctx = SweepContext(
        session=db, now=_utcnow(), mode="observe",
        max_projects=25, sample_limit=200, window_days=14,
    )
    results = await mis.check_saturated_direction_with_nonmerge_productivity(ctx)

    flagged = [r for r in results if r.flagged]
    assert len(flagged) == 1
    f = flagged[0]
    assert "possibly_saturated" in f.detail
    assert "verified_evidence" in f.detail or "acceptance" in f.detail.lower()


# --- Check: capability_claim_reconciliation ---------------------------------


async def _project_with_capabilities(db, capabilities: list) -> Project:
    """A project whose problem_model has the given capabilities list."""
    pm = {"capabilities": capabilities}
    p = Project(name="mi-cap", user_id="u1", problem_model=pm)
    db.add(p)
    await db.commit()
    await db.refresh(p)
    return p


def _cap_ctx(db) -> SweepContext:
    return SweepContext(
        session=db,
        now=_utcnow(),
        mode="observe",
        max_projects=25,
        sample_limit=200,
        window_days=14,
    )


@pytest.mark.asyncio
async def test_capability_claim_reconciliation_flags_removed_backing_paths(
    db, monkeypatch
):
    """REQUIRED acceptance test: a working/partial capability whose every declared
    backing_path is DEFINITELY removed at HEAD (False) must be FLAGGED — the
    self-reported 'working' claim is no longer backed by code."""
    import app.source_ref_validation as srv

    project = await _project_with_capabilities(
        db,
        [
            {
                "name": "repo_map",
                "status": "working",
                "backing_paths": ["backend/app/repo_map.py"],
            }
        ],
    )

    async def _removed(path, *args, **kwargs):
        return False  # definite removal

    monkeypatch.setattr(srv, "_path_exists_at_head", _removed)

    results = await mis.check_capability_claim_reconciliation(_cap_ctx(db))
    flagged = [r for r in results if r.flagged]
    assert len(flagged) == 1, f"expected 1 flagged, got {results}"
    f = flagged[0]
    assert f.check == "capability_claim_reconciliation"
    assert f.status == CheckStatus.FLAGGED
    assert f.subject == str(project.id)
    assert f.delta == 1
    assert "repo_map" in f.detail
    assert "removed at HEAD" in f.detail


@pytest.mark.asyncio
async def test_capability_claim_reconciliation_indeterminate_not_flagged(
    db, monkeypatch
):
    """CRITICAL HONESTY RULE: a None (indeterminate) path check must NEVER be
    laundered into 'removed' — the capability is NOT flagged."""
    import app.source_ref_validation as srv

    await _project_with_capabilities(
        db,
        [
            {
                "name": "repo_map",
                "status": "working",
                "backing_paths": ["backend/app/repo_map.py"],
            }
        ],
    )

    async def _indeterminate(path, *args, **kwargs):
        return None

    monkeypatch.setattr(srv, "_path_exists_at_head", _indeterminate)

    results = await mis.check_capability_claim_reconciliation(_cap_ctx(db))
    assert all(not r.flagged for r in results), results
    ok = [r for r in results if r.status == CheckStatus.OK]
    assert ok, results


@pytest.mark.asyncio
async def test_capability_claim_reconciliation_ok_when_all_paths_present(
    db, monkeypatch
):
    """All backing_paths still present at HEAD → OK, not flagged."""
    import app.source_ref_validation as srv

    await _project_with_capabilities(
        db,
        [
            {
                "name": "repo_map",
                "status": "working",
                "backing_paths": ["backend/app/repo_map.py", "backend/app/foo.py"],
            }
        ],
    )

    async def _present(path, *args, **kwargs):
        return True

    monkeypatch.setattr(srv, "_path_exists_at_head", _present)

    results = await mis.check_capability_claim_reconciliation(_cap_ctx(db))
    assert all(not r.flagged for r in results), results
    ok = [r for r in results if r.status == CheckStatus.OK]
    assert ok and ok[0].delta == 0


@pytest.mark.asyncio
async def test_capability_claim_reconciliation_mixed_presence_not_flagged(
    db, monkeypatch
):
    """If SOME paths are removed but at least one is still present, no flag is raised."""
    import app.source_ref_validation as srv

    await _project_with_capabilities(
        db,
        [
            {
                "name": "repo_map",
                "status": "partial",
                "backing_paths": [
                    "backend/app/gone.py",  # removed
                    "backend/app/present.py",  # still present
                ],
            }
        ],
    )

    async def _mixed(path, *args, **kwargs):
        return path == "backend/app/present.py"  # True for present, False for gone

    monkeypatch.setattr(srv, "_path_exists_at_head", _mixed)

    results = await mis.check_capability_claim_reconciliation(_cap_ctx(db))
    assert all(not r.flagged for r in results), results


@pytest.mark.asyncio
async def test_capability_claim_reconciliation_planned_never_flagged(
    db, monkeypatch
):
    """planned capabilities must NEVER be flagged for absent backing paths."""
    import app.source_ref_validation as srv

    await _project_with_capabilities(
        db,
        [
            {
                "name": "future_feature",
                "status": "planned",
                "backing_paths": ["backend/app/future.py"],
            }
        ],
    )

    async def _removed(path, *args, **kwargs):
        return False

    monkeypatch.setattr(srv, "_path_exists_at_head", _removed)

    results = await mis.check_capability_claim_reconciliation(_cap_ctx(db))
    assert all(not r.flagged for r in results), results
    # planned caps don't count as eligible → SKIPPED
    skipped = [r for r in results if r.status == CheckStatus.SKIPPED]
    assert skipped


@pytest.mark.asyncio
async def test_capability_claim_reconciliation_empty_backing_paths_no_evidence_flagged(db):
    """A working capability with empty backing_paths and no evidence_refs is
    flagged by the free-claim path (claim_contradicts_evidence returns True)."""
    await _project_with_capabilities(
        db,
        [{"name": "repo_map", "status": "working", "backing_paths": []}],
    )

    results = await mis.check_capability_claim_reconciliation(_cap_ctx(db))
    flagged = [r for r in results if r.flagged]
    assert len(flagged) == 1, f"expected 1 flagged, got {results}"
    assert "repo_map" in flagged[0].detail


@pytest.mark.asyncio
async def test_capability_claim_reconciliation_is_flag_only(db, monkeypatch):
    """The check must never mutate the durable problem_model — flag-only guarantee."""
    import app.source_ref_validation as srv

    project = await _project_with_capabilities(
        db,
        [
            {
                "name": "repo_map",
                "status": "working",
                "backing_paths": ["backend/app/repo_map.py"],
            }
        ],
    )
    before = project.problem_model["capabilities"][0].copy()

    async def _removed(path, *args, **kwargs):
        return False

    monkeypatch.setattr(srv, "_path_exists_at_head", _removed)

    await mis.check_capability_claim_reconciliation(_cap_ctx(db))

    await db.refresh(project)
    after = project.problem_model["capabilities"][0]
    assert after == before  # name, status, backing_paths all untouched


def test_registry_contains_capability_claim_reconciliation():
    """The new check must be registered in the global registry."""
    names = {c.name for c in registered_checks()}
    assert "capability_claim_reconciliation" in names


@pytest.mark.asyncio
async def test_capability_claim_reconciliation_flags_contradicted_evidence(
    db, monkeypatch
):
    """A working capability with a proven evidence_ref whose backing path is
    definitively removed at HEAD should be flagged (evidence is contradicted)."""
    import app.source_ref_validation as srv

    project = await _project_with_capabilities(
        db,
        [
            {
                "name": "repo_map",
                "status": "working",
                "backing_paths": ["backend/app/present.py"],
                "evidence_refs": [
                    {
                        "kind": "named_test",
                        "ref": "backend/testing/test_repo_map.py",
                        "proven": True,
                        "source_id": "task-abc",
                    }
                ],
            }
        ],
    )

    async def _mixed(path, *args, **kwargs):
        # backing path is present; evidence ref path is removed
        if path == "backend/app/present.py":
            return True
        return False  # evidence ref path removed

    monkeypatch.setattr(srv, "_path_exists_at_head", _mixed)

    results = await mis.check_capability_claim_reconciliation(_cap_ctx(db))
    flagged = [r for r in results if r.flagged]
    assert len(flagged) == 1, f"expected 1 flagged, got {results}"
    f = flagged[0]
    assert f.subject == str(project.id)
    assert "repo_map" in f.detail


@pytest.mark.asyncio
async def test_capability_claim_reconciliation_flags_working_unproven(
    db, monkeypatch
):
    """A working capability with empty evidence_refs (proven=False, no evidence)
    should be flagged via claim_contradicts_evidence."""
    import app.source_ref_validation as srv

    await _project_with_capabilities(
        db,
        [
            {
                "name": "repo_map",
                "status": "working",
                "backing_paths": ["backend/app/repo_map.py"],
                "evidence_refs": [],  # explicitly empty — claim contradicts evidence
            }
        ],
    )

    async def _present(path, *args, **kwargs):
        return True  # backing_path still exists — not flagged by backing-path check

    monkeypatch.setattr(srv, "_path_exists_at_head", _present)

    results = await mis.check_capability_claim_reconciliation(_cap_ctx(db))
    flagged = [r for r in results if r.flagged]
    assert len(flagged) == 1, f"expected 1 flagged, got {results}"
    assert "repo_map" in flagged[0].detail


@pytest.mark.asyncio
async def test_capability_claim_reconciliation_ok_when_proven_ref_present(
    db, monkeypatch
):
    """A working capability with a proven evidence_ref whose path still exists
    at HEAD should NOT be flagged."""
    import app.source_ref_validation as srv

    await _project_with_capabilities(
        db,
        [
            {
                "name": "repo_map",
                "status": "working",
                "backing_paths": ["backend/app/repo_map.py"],
                "evidence_refs": [
                    {
                        "kind": "named_test",
                        "ref": "backend/testing/test_repo_map.py",
                        "proven": True,
                        "source_id": "task-abc",
                    }
                ],
            }
        ],
    )

    async def _present(path, *args, **kwargs):
        return True  # all paths present

    monkeypatch.setattr(srv, "_path_exists_at_head", _present)

    results = await mis.check_capability_claim_reconciliation(_cap_ctx(db))
    assert all(not r.flagged for r in results), results
    ok = [r for r in results if r.status == CheckStatus.OK]
    assert ok


# --- Check: capability_claim_reconciliation (free-claim path) ----------------


@pytest.mark.asyncio
async def test_capability_claim_reconciliation_free_claim_working_no_evidence_flagged(
    db,
):
    """A working capability with no backing_paths and empty evidence_refs IS flagged."""
    await _project_with_capabilities(
        db,
        [
            {
                "name": "free_working",
                "status": "working",
                # No backing_paths — free-claim path applies.
                "evidence_refs": [],
            }
        ],
    )

    results = await mis.check_capability_claim_reconciliation(_cap_ctx(db))
    flagged = [r for r in results if r.flagged]
    assert len(flagged) == 1, f"expected 1 flagged, got {results}"
    f = flagged[0]
    assert f.check == "capability_claim_reconciliation"
    assert "free_working" in f.detail
    assert f.severity == "warn"


@pytest.mark.asyncio
async def test_capability_claim_reconciliation_free_claim_working_with_proven_ref_not_flagged(
    db,
):
    """A working capability with no backing_paths but a proven evidence_ref IS NOT flagged."""
    await _project_with_capabilities(
        db,
        [
            {
                "name": "free_working_proven",
                "status": "working",
                "evidence_refs": [
                    {
                        "kind": "named_test",
                        "ref": "backend/testing/test_something.py",
                        "proven": True,
                        "source_id": "task-xyz",
                    }
                ],
            }
        ],
    )

    results = await mis.check_capability_claim_reconciliation(_cap_ctx(db))
    assert all(not r.flagged for r in results), f"expected no flags, got {results}"
    ok = [r for r in results if r.status == CheckStatus.OK]
    assert ok


@pytest.mark.asyncio
async def test_capability_claim_reconciliation_free_claim_partial_no_evidence_not_flagged(
    db,
):
    """A partial capability with no backing_paths and empty evidence_refs is NOT flagged."""
    await _project_with_capabilities(
        db,
        [
            {
                "name": "free_partial",
                "status": "partial",
                "evidence_refs": [],
            }
        ],
    )

    results = await mis.check_capability_claim_reconciliation(_cap_ctx(db))
    assert all(not r.flagged for r in results), f"expected no flags for partial, got {results}"


@pytest.mark.asyncio
async def test_capability_claim_reconciliation_backing_path_check_still_works_with_free_claim(
    db, monkeypatch
):
    """The existing backing_path reconciliation path is unchanged when a free-claim
    cap is also present in the same project."""
    import app.source_ref_validation as srv

    await _project_with_capabilities(
        db,
        [
            {
                "name": "path_backed",
                "status": "working",
                "backing_paths": ["backend/app/removed.py"],
                "evidence_refs": [],
            },
            {
                "name": "free_working",
                "status": "working",
                "evidence_refs": [
                    {"kind": "named_test", "ref": "backend/testing/test_ok.py", "proven": True}
                ],
            },
        ],
    )

    async def _removed(path, *args, **kwargs):
        return False  # all paths removed — backing_path cap should be flagged

    monkeypatch.setattr(srv, "_path_exists_at_head", _removed)

    results = await mis.check_capability_claim_reconciliation(_cap_ctx(db))
    flagged = [r for r in results if r.flagged]
    # path_backed has all backing_paths removed → flagged; free_working has a proven ref → not flagged.
    assert len(flagged) == 1, f"expected 1 flagged (path_backed only), got {results}"
    assert "path_backed" in flagged[0].detail


# --- Check: merged_pr_count_consistency --------------------------------------


def _mpc_ctx(db, **kwargs):
    return SweepContext(
        session=db,
        now=_utcnow(),
        mode=kwargs.get("mode", "observe"),
        max_projects=kwargs.get("max_projects", 25),
        sample_limit=kwargs.get("sample_limit", 200),
        window_days=kwargs.get("window_days", 14),
    )


async def _merged_task(db, project, *, parent_task_id=None) -> Task:
    """A COMPLETED merged task; optionally a rollup parent (has children pointing at it)."""
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        status=TaskStatus.COMPLETED,
        context={"landed_on_integration": True, "pr_merged": True},
        parent_task_id=parent_task_id,
    )
    db.add(t)
    await db.commit()
    await db.refresh(t)
    return t


async def _unmerged_task(db, project) -> Task:
    """A COMPLETED but unmerged task."""
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        status=TaskStatus.COMPLETED,
        context={},
    )
    db.add(t)
    await db.commit()
    await db.refresh(t)
    return t


def test_registry_contains_merged_pr_count_consistency():
    names = {c.name for c in registered_checks()}
    assert "merged_pr_count_consistency" in names


@pytest.mark.asyncio
async def test_merged_pr_count_consistency_flags_rollup_double_count(db):
    """Rollup parent + children all merged → naive=3, canonical=2, delta=1."""
    project = await _project(db)

    # Seed a rollup parent and two children. The parent task will be identified
    # as a parent because other tasks carry parent_task_id pointing at it.
    parent = await _merged_task(db, project)
    await _merged_task(db, project, parent_task_id=parent.id)
    await _merged_task(db, project, parent_task_id=parent.id)

    results = await mis.check_merged_pr_count_consistency(_mpc_ctx(db))
    flagged = [r for r in results if r.flagged]
    assert len(flagged) == 1, results
    f = flagged[0]
    assert f.check == "merged_pr_count_consistency"
    # naive = 3 (parent + 2 children), canonical = 2 (children only)
    assert f.self_reported == 3
    assert f.ground_truth == 2
    assert f.delta == 1
    assert f.severity == "warn"
    assert f.subject == str(project.id)


@pytest.mark.asyncio
async def test_merged_pr_count_consistency_ok_when_no_rollup_parents(db):
    """Two independent merged tasks with no parent links → OK, delta=0."""
    project = await _project(db)
    await _merged_task(db, project)
    await _merged_task(db, project)

    results = await mis.check_merged_pr_count_consistency(_mpc_ctx(db))
    assert all(not r.flagged for r in results), results
    ok = [r for r in results if r.status == CheckStatus.OK]
    assert len(ok) == 1
    assert ok[0].delta == 0
    assert ok[0].self_reported == 2
    assert ok[0].ground_truth == 2


@pytest.mark.asyncio
async def test_merged_pr_count_consistency_skips_when_no_merged_tasks(db):
    """Completed but unmerged tasks → SKIPPED."""
    project = await _project(db)
    await _unmerged_task(db, project)
    await _unmerged_task(db, project)

    results = await mis.check_merged_pr_count_consistency(_mpc_ctx(db))
    assert all(not r.flagged for r in results), results
    assert any(r.status == CheckStatus.SKIPPED for r in results)


@pytest.mark.asyncio
async def test_merged_pr_count_consistency_is_flag_only(db):
    """The check must never mutate any audited task row."""
    project = await _project(db)
    parent = await _merged_task(db, project)
    child = await _merged_task(db, project, parent_task_id=parent.id)

    before_parent_ctx = dict(parent.context or {})
    before_child_ctx = dict(child.context or {})

    await mis.check_merged_pr_count_consistency(_mpc_ctx(db))

    await db.refresh(parent)
    await db.refresh(child)
    assert parent.context == before_parent_ctx
    assert child.context == before_child_ctx


@pytest.mark.asyncio
async def test_merged_pr_count_consistency_multiple_parents_inflates_delta(db):
    """Two rollup parents each with children → delta=2."""
    project = await _project(db)

    parent1 = await _merged_task(db, project)
    await _merged_task(db, project, parent_task_id=parent1.id)

    parent2 = await _merged_task(db, project)
    await _merged_task(db, project, parent_task_id=parent2.id)

    results = await mis.check_merged_pr_count_consistency(_mpc_ctx(db))
    flagged = [r for r in results if r.flagged]
    assert len(flagged) == 1, results
    f = flagged[0]
    # naive=4 (2 parents + 2 children), canonical=2 (children only)
    assert f.self_reported == 4
    assert f.ground_truth == 2
    assert f.delta == 2


# --- Check: criterion_evidence_count_integrity --------------------------------


@pytest.mark.asyncio
async def test_new_integrity_check_flags_counter_divergence(db):
    """Named acceptance evidence: seed a divergence and assert FLAGGED.

    Creates a criterion with CriterionEvidence rows, mocks
    get_version_coverage_summary to return a wrong evidence_count, and
    verifies the check flags the divergence between the mock self-reported
    count and the independent COUNT(*) ground truth.
    """
    from unittest.mock import patch

    project = await _project(db)
    version, criterion = await _open_version_with_criterion(
        db, project, text="Evidence count integrity divergence test"
    )
    # Insert 3 evidence rows (ground truth will be 3).
    for i in range(3):
        db.add(
            CriterionEvidence(
                project_id=project.id,
                criterion_id=criterion.id,
                source_kind="operator_attestation",
                detail={"note": f"evidence {i}"},
            )
        )
    await db.commit()

    # Mock the summary to report evidence_count=1 (WRONG — actual is 3).
    async def _mock_summary(*args, **kwargs):
        return {
            "total": 1,
            "covered": 0,
            "partial": 0,
            "uncovered": 1,
            "retracted": 0,
            "superseded": 0,
            "inactive": 0,
            "stuck_count": 0,
            "criteria": [
                {
                    "criterion_key": criterion.criterion_key,
                    "text": "Evidence count integrity divergence test",
                    "coverage": "partial",
                    "status": "active",
                    "evidence_count": 1,  # WRONG: says 1, but 3 rows exist
                    "evidence_truncated": False,
                    "stuck_reason": None,
                    "evidence": [],
                }
            ],
        }

    ctx = SweepContext(
        session=db, now=_utcnow(), mode="observe",
        max_projects=25, sample_limit=200, window_days=14,
    )
    with patch(
        "app.acceptance_criteria.get_version_coverage_summary",
        side_effect=_mock_summary,
    ):
        results = await mis.check_criterion_evidence_count_integrity(ctx)

    flagged = [r for r in results if r.flagged]
    assert len(flagged) == 1, f"expected 1 flagged, got {results}"
    f = flagged[0]
    assert f.check == "criterion_evidence_count_integrity"
    assert f.self_reported == 1   # mocked summary says 1
    assert f.ground_truth == 3    # actual CriterionEvidence row count is 3
    assert f.delta == 2           # |3 - 1|
    assert criterion.criterion_key in f.detail


@pytest.mark.asyncio
async def test_criterion_evidence_count_integrity_ok_when_counts_match(db):
    """When the independent COUNT(*) matches summary evidence_count, OK with delta=0."""
    project = await _project(db)
    version, criterion = await _open_version_with_criterion(
        db, project, text="Evidence count integrity match test"
    )
    # Insert evidence rows.
    for i in range(3):
        db.add(
            CriterionEvidence(
                project_id=project.id,
                criterion_id=criterion.id,
                source_kind="operator_attestation",
                detail={"note": f"evidence {i}"},
            )
        )
    await db.commit()

    ctx = SweepContext(
        session=db, now=_utcnow(), mode="observe",
        max_projects=25, sample_limit=200, window_days=14,
    )
    results = await mis.check_criterion_evidence_count_integrity(ctx)

    # No flagged results expected — counts match.
    assert all(not r.flagged for r in results), f"unexpected flagged: {results}"
    ok_results = [r for r in results if r.status == CheckStatus.OK]
    assert len(ok_results) >= 1
    ok = ok_results[0]
    assert ok.delta == 0
    assert ok.subject == str(project.id)
    assert "matching evidence counts" in ok.detail


@pytest.mark.asyncio
async def test_criterion_evidence_count_integrity_skips_no_open_version(db):
    """Project with no open version → SKIPPED."""
    project = await _project(db)

    ctx = SweepContext(
        session=db, now=_utcnow(), mode="observe",
        max_projects=25, sample_limit=200, window_days=14,
    )
    results = await mis.check_criterion_evidence_count_integrity(ctx)
    mine = [r for r in results if r.subject == str(project.id)]
    assert len(mine) == 1
    assert mine[0].status == CheckStatus.SKIPPED
    assert "no open version" in mine[0].detail


@pytest.mark.asyncio
async def test_criterion_evidence_count_integrity_skips_no_criteria(db):
    """Open version with no criteria → SKIPPED."""
    project = await _project(db)
    version = ReleaseVersion(project_id=project.id, version_number=1, status="open")
    db.add(version)
    await db.commit()

    ctx = SweepContext(
        session=db, now=_utcnow(), mode="observe",
        max_projects=25, sample_limit=200, window_days=14,
    )
    results = await mis.check_criterion_evidence_count_integrity(ctx)
    mine = [r for r in results if r.subject == str(project.id)]
    assert len(mine) == 1
    assert mine[0].status == CheckStatus.SKIPPED
    assert "no criteria" in mine[0].detail


@pytest.mark.asyncio
async def test_criterion_evidence_count_integrity_is_flag_only(db):
    """The check never mutates CriterionEvidence rows in any mode."""
    project = await _project(db)
    version, criterion = await _open_version_with_criterion(
        db, project, text="Flag-only integrity test"
    )
    db.add(
        CriterionEvidence(
            project_id=project.id,
            criterion_id=criterion.id,
            source_kind="operator_attestation",
            detail={"note": "flag-only test"},
        )
    )
    await db.commit()

    # Count before.
    before_count = (
        await db.execute(
            select(CriterionEvidence).where(
                CriterionEvidence.criterion_id == criterion.id
            )
        )
    ).scalars().all()
    before = len([r for r in before_count])

    ctx = SweepContext(
        session=db, now=_utcnow(), mode="observe",
        max_projects=25, sample_limit=200, window_days=14,
    )
    await mis.check_criterion_evidence_count_integrity(ctx)

    # Count after must be identical.
    after_count = (
        await db.execute(
            select(CriterionEvidence).where(
                CriterionEvidence.criterion_id == criterion.id
            )
        )
    ).scalars().all()
    after = len([r for r in after_count])
    assert after == before, f"CriterionEvidence rows changed: {before} → {after}"


@pytest.mark.asyncio
async def test_criterion_evidence_count_integrity_runs_in_sweep_tick(db):
    """The new check is auto-discovered and executed in a full sweep tick."""
    project = await _project(db)
    version, criterion = await _open_version_with_criterion(
        db, project, text="Sweep tick integration test"
    )
    db.add(
        CriterionEvidence(
            project_id=project.id,
            criterion_id=criterion.id,
            source_kind="operator_attestation",
            detail={"note": "sweep tick test"},
        )
    )
    await db.commit()

    now = _utcnow()
    tick = await run_metrics_integrity_sweep(db, now=now)
    assert tick is not None
    # The check must have run and produced a result.
    # Tick flags are check-name-keyed; the check is registered and the sweep
    # picks it up. The runs flag count depends on data, but the observability
    # snapshot must record the check ran.
    snap = get_metrics_integrity_observability()
    assert snap["checks_run"] > 0
    # The check name must appear in the registered set.
    names = {c.name for c in registered_checks()}
    assert "criterion_evidence_count_integrity" in names


# --- Check: criterion_claim_reconciliation -----------------------------------


@pytest.mark.asyncio
async def test_criterion_claim_reconciliation_flags_uncovered_active_criterion(db):
    """An active criterion with no covering evidence is flagged as unbacked."""
    project = await _project(db)
    version, criterion = await _open_version_with_criterion(
        db, project, text="Uncovered active criterion"
    )  # no evidence rows → uncovered → unproven

    ctx = SweepContext(
        session=db, now=_utcnow(), mode="observe",
        max_projects=25, sample_limit=200, window_days=14,
    )
    results = await mis.check_criterion_claim_reconciliation(ctx)
    flagged = [r for r in results if r.flagged]
    assert len(flagged) == 1, f"expected 1 flagged, got {results}"
    f = flagged[0]
    assert f.check == "criterion_claim_reconciliation"
    assert f.self_reported == 1
    assert f.ground_truth == 0
    assert f.delta == 1
    assert criterion.criterion_key in f.detail
    assert "unproven" in f.detail


@pytest.mark.asyncio
async def test_criterion_claim_reconciliation_ok_when_covered(db):
    """A criterion with proven task_merge evidence (covered → proven) is not flagged."""
    project = await _project(db)
    version, criterion = await _open_version_with_criterion(
        db, project, text="Covered active criterion"
    )
    db.add(
        CriterionEvidence(
            project_id=project.id,
            criterion_id=criterion.id,
            source_kind="reachability",  # covers on its own → proven
            detail={"note": "reachable"},
        )
    )
    await db.commit()

    ctx = SweepContext(
        session=db, now=_utcnow(), mode="observe",
        max_projects=25, sample_limit=200, window_days=14,
    )
    results = await mis.check_criterion_claim_reconciliation(ctx)
    assert all(not r.flagged for r in results), f"unexpected flagged: {results}"
    ok = [r for r in results if r.status == CheckStatus.OK]
    assert len(ok) >= 1
    assert ok[0].delta == 0
    assert "reconciled" in ok[0].detail


@pytest.mark.asyncio
async def test_criterion_claim_reconciliation_skips_no_open_version(db):
    """Project with no open version → SKIPPED for that project."""
    project = await _project(db)
    ctx = SweepContext(
        session=db, now=_utcnow(), mode="observe",
        max_projects=25, sample_limit=200, window_days=14,
    )
    results = await mis.check_criterion_claim_reconciliation(ctx)
    mine = [r for r in results if r.subject == str(project.id)]
    assert len(mine) == 1
    assert mine[0].status == CheckStatus.SKIPPED
    assert "no open version" in mine[0].detail


@pytest.mark.asyncio
async def test_criterion_claim_reconciliation_skips_no_active_criteria(db):
    """Open version with no active criteria → SKIPPED for that project."""
    project = await _project(db)
    version = ReleaseVersion(project_id=project.id, version_number=1, status="open")
    db.add(version)
    await db.commit()
    ctx = SweepContext(
        session=db, now=_utcnow(), mode="observe",
        max_projects=25, sample_limit=200, window_days=14,
    )
    results = await mis.check_criterion_claim_reconciliation(ctx)
    mine = [r for r in results if r.subject == str(project.id)]
    assert len(mine) == 1
    assert mine[0].status == CheckStatus.SKIPPED
    assert "no active criteria" in mine[0].detail


@pytest.mark.asyncio
async def test_criterion_claim_reconciliation_is_flag_only(db):
    """The check never mutates CriterionEvidence rows in any mode."""
    project = await _project(db)
    version, criterion = await _open_version_with_criterion(
        db, project, text="Flag-only reconciliation test"
    )
    db.add(
        CriterionEvidence(
            project_id=project.id,
            criterion_id=criterion.id,
            source_kind="task_merge",
            detail={"proven": False},  # present but not covering → partial
        )
    )
    await db.commit()
    before = len((
        await db.execute(
            select(CriterionEvidence).where(
                CriterionEvidence.criterion_id == criterion.id
            )
        )
    ).scalars().all())

    ctx = SweepContext(
        session=db, now=_utcnow(), mode="observe",
        max_projects=25, sample_limit=200, window_days=14,
    )
    results = await mis.check_criterion_claim_reconciliation(ctx)
    # partial evidence does not back a "proven" claim → flagged.
    assert any(r.flagged for r in results)

    after = len((
        await db.execute(
            select(CriterionEvidence).where(
                CriterionEvidence.criterion_id == criterion.id
            )
        )
    ).scalars().all())
    assert after == before, f"CriterionEvidence rows changed: {before} → {after}"


@pytest.mark.asyncio
async def test_criterion_claim_reconciliation_runs_in_sweep_tick(db):
    """The new check is auto-discovered and executed in a full sweep tick."""
    project = await _project(db)
    await _open_version_with_criterion(
        db, project, text="Sweep tick reconciliation test"
    )
    now = _utcnow()
    tick = await run_metrics_integrity_sweep(db, now=now)
    assert tick is not None
    names = {c.name for c in registered_checks()}
    assert "criterion_claim_reconciliation" in names


# --- Check: capability_proof_gate_gap ----------------------------------------


@pytest.mark.asyncio
async def test_capability_proof_gate_gap_flags_unproven_working(db):
    """A project with working capabilities that carry no proven evidence is flagged.

    Caps with no backing_paths cannot be auto-resolved: they remain in delta and
    are surfaced with ``needs_backing_paths`` in the detail string. The problem_model
    is never mutated when no auto-resolution is possible.

    Uses a real merge-test verdict context (working capability + no evidence) so
    the assertion is concrete and non-trivially true.
    """
    from sqlalchemy.orm.attributes import flag_modified

    project = await _project(db)
    # Two working capabilities: one unproven (no evidence_refs, no backing_paths),
    # one proven via both evidence_refs and the platform ledger.
    project.problem_model = {
        "capabilities": [
            {
                "name": "repo-map",
                "status": "working",
                "evidence_refs": [],  # no proof → unproven; no backing_paths either
            },
            {
                "name": "ci-integration",
                "status": "working",
                "evidence_refs": [{"proven": True, "ref": "backend/app/ci.py"}],
            },
            {
                "name": "future-feature",
                "status": "planned",  # planned → not included in working set
                "evidence_refs": [],
            },
        ]
    }
    # Seed a platform ledger entry so consumer_gate_proof_status corroborates it.
    project.capability_proof_records = {
        "ci-integration": [
            {"proof_kind": "named_test", "proven": True, "ref": "backend/app/ci.py"},
        ]
    }
    flag_modified(project, "problem_model")
    flag_modified(project, "capability_proof_records")
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
    results = await mis.check_capability_proof_gate_gap(ctx)

    pid = str(project.id)
    flagged = [r for r in results if r.flagged and r.subject == pid]
    assert len(flagged) == 1, f"expected 1 flagged result for project, got {results}"
    f = flagged[0]
    assert f.check == "capability_proof_gate_gap"
    # self_reported = total working capabilities (2: repo-map + ci-integration)
    assert f.self_reported == 2
    # ground_truth = proven count (1: ci-integration); no auto-resolution possible
    assert f.ground_truth == 1
    # delta = unresolved gap (1: repo-map has no backing_paths → needs_backing_paths)
    assert f.delta == 1
    assert "repo-map" in f.detail
    # No backing_paths → detail uses needs_backing_paths marker, not "proven"
    assert "needs_backing_paths" in f.detail

    # Flag-only for caps without backing_paths: problem_model must be unchanged
    await db.refresh(project)
    caps = (project.problem_model or {}).get("capabilities", [])
    repo_map = next((c for c in caps if c.get("name") == "repo-map"), None)
    assert repo_map is not None
    assert repo_map["evidence_refs"] == []


@pytest.mark.asyncio
async def test_capability_proof_gate_gap_triggers_self_heal(db):
    """An unproven working capability with a backing_path present at HEAD is
    auto-resolved: proof is recorded in capability_proof_records, the check
    reports delta == 0 (fully self-healed), and ground_truth increases by 1.

    This is the named acceptance test for the self-heal feature.
    """
    from unittest.mock import AsyncMock, patch
    from sqlalchemy.orm.attributes import flag_modified

    project = await _project(db)
    # One unproven working capability with a backing_path.
    # backend/app/models.py is a real file in the repo.
    project.problem_model = {
        "capabilities": [
            {
                "name": "repo-map",
                "status": "working",
                "evidence_refs": [],  # no proof → unproven
                "backing_paths": ["backend/app/models.py"],
            },
        ]
    }
    flag_modified(project, "problem_model")
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

    # Mock _path_exists_at_head to return True for the backing path so the test
    # doesn't depend on filesystem state. Patch at the source module since the
    # check imports it with a local `from app.source_ref_validation import`.
    # Also force named-test discovery to find no covering test (read_head → None)
    # so this test deterministically exercises the ``backing_path`` fallback tier
    # rather than depending on whether a checkout exists at HEAD.
    with patch(
        "app.source_ref_validation._path_exists_at_head",
        new=AsyncMock(return_value=True),
    ), patch(
        "app.capability_proof._read_file_at_head",
        new=AsyncMock(return_value=None),
    ):
        results = await mis.check_capability_proof_gate_gap(ctx)

    pid = str(project.id)
    # Fully self-healed → no flagged result, one OK result.
    flagged = [r for r in results if r.flagged and r.subject == pid]
    assert flagged == [], f"expected no flagged results after self-heal, got {results}"
    ok = [r for r in results if r.subject == pid and r.status == CheckStatus.OK]
    assert len(ok) == 1, f"expected 1 OK result, got {results}"
    assert ok[0].delta == 0
    # ground_truth is only the genuine ``proven`` count — auto-resolved caps are
    # path_proven (backing_path tier), NOT proven, so they are NOT included.
    assert ok[0].ground_truth == 0
    assert "path_proven" in ok[0].detail
    # Self-heal throughput is surfaced structured (not just in the detail prose)
    # so the observability snapshot / dashboards can report it machine-readably.
    assert ok[0].auto_resolved == 1
    assert ok[0].as_dict()["auto_resolved"] == 1
    # This cap self-healed via the backing_path fallback tier (read_head is forced
    # to None above so no covering test is discovered), so the structured tier
    # breakdown must attribute the single auto-resolution to backing_path — NOT
    # named_test_present — and the two tiers must sum to auto_resolved.
    assert ok[0].auto_resolved_backing_path == 1
    assert ok[0].auto_resolved_named_test_present == 0
    d = ok[0].as_dict()
    assert d["auto_resolved_backing_path"] == 1
    assert d["auto_resolved_named_test_present"] == 0
    assert (
        d["auto_resolved_backing_path"] + d["auto_resolved_named_test_present"]
        == d["auto_resolved"]
    )

    # Proof must have been recorded in capability_proof_records.
    await db.refresh(project)
    proof_records = project.capability_proof_records or {}
    # normalize_capability_name("repo-map") == "repo-map"
    assert "repo-map" in proof_records, (
        f"expected proof entry for 'repo-map' in capability_proof_records, got {proof_records}"
    )
    entries = proof_records["repo-map"]
    assert len(entries) >= 1
    assert entries[0].get("proof_kind") == "backing_path"

    # evidence_refs should now carry a proven backing_path ref (written by
    # _append_evidence_refs_to_capabilities inside record_capability_backing_path_proof).
    caps = (project.problem_model or {}).get("capabilities", [])
    repo_map = next((c for c in caps if c.get("name") == "repo-map"), None)
    assert repo_map is not None
    evidence_refs = repo_map.get("evidence_refs", [])
    assert any(
        e.get("proven") is True and e.get("kind") == "backing_path"
        for e in evidence_refs
    ), f"expected a proven backing_path evidence_ref, got {evidence_refs}"


def test_record_tick_aggregates_auto_resolved_throughput():
    """The observability aggregator must sum per-result ``auto_resolved`` (and its
    tier split) into the tick summary and the cumulative snapshot totals so
    self-heal throughput is machine-readable — it previously tracked only
    flags/errors/enrolled, leaving self-heal invisible to operators.
    """
    mis._reset_observability_for_tests()
    snap0 = get_metrics_integrity_observability()
    assert snap0["auto_resolved_total"] == 0
    assert snap0["auto_resolved_backing_path_total"] == 0
    assert snap0["auto_resolved_named_test_present_total"] == 0

    results = [
        CheckResult(
            check="capability_proof_gate_gap",
            status=CheckStatus.OK,
            subject="p1",
            auto_resolved=3,
            auto_resolved_backing_path=2,
            auto_resolved_named_test_present=1,
        ),
        # A flagged result that still healed some caps this tick.
        CheckResult(
            check="capability_proof_gate_gap",
            status=CheckStatus.FLAGGED,
            severity="warn",
            subject="p2",
            delta=1,
            auto_resolved=1,
            auto_resolved_backing_path=1,
            auto_resolved_named_test_present=0,
        ),
        # A non-mutating check contributes nothing (auto_resolved is None).
        CheckResult(
            check="landed_vs_reported",
            status=CheckStatus.OK,
            subject="global",
        ),
    ]
    tick = mis._OBSERVABILITY.record_tick(
        now=_utcnow(), mode="observe", results=results, enrolled=0
    )
    # Per-tick sums across all results.
    assert tick["auto_resolved"] == 4
    assert tick["auto_resolved_backing_path"] == 3
    assert tick["auto_resolved_named_test_present"] == 1

    snap1 = get_metrics_integrity_observability()
    assert snap1["auto_resolved_total"] == 4
    assert snap1["auto_resolved_backing_path_total"] == 3
    assert snap1["auto_resolved_named_test_present_total"] == 1

    # A second tick accumulates on top of the first (monotonic totals).
    mis._OBSERVABILITY.record_tick(
        now=_utcnow(),
        mode="observe",
        results=[
            CheckResult(
                check="capability_proof_gate_gap",
                status=CheckStatus.OK,
                subject="p3",
                auto_resolved=2,
                auto_resolved_backing_path=0,
                auto_resolved_named_test_present=2,
            )
        ],
        enrolled=0,
    )
    snap2 = get_metrics_integrity_observability()
    assert snap2["auto_resolved_total"] == 6
    assert snap2["auto_resolved_backing_path_total"] == 3
    assert snap2["auto_resolved_named_test_present_total"] == 3


@pytest.mark.asyncio
async def test_capability_proof_gate_gap_mints_named_test_present_for_covered_cap(db):
    """The fleet-wide self-heal mints a ``named_test_present`` proof when a working
    capability's backing path has a conventionally-named test file present at HEAD
    that imports the module.

    That discovery is STATIC — file existence plus an import regex; the test is
    NEVER executed — so the recorded proof is graded ``path_proven``, NOT
    ``proven``: it cannot clear the enforce gate. Only a real green merge-test
    acceptance verdict mints the executed ``proven`` ``named_test`` tier.
    """
    from unittest.mock import AsyncMock, patch
    from sqlalchemy.orm.attributes import flag_modified

    project = await _project(db)
    # Working capability backed by a source path whose conventional test file
    # (backend/testing/test_local_runner.py) exists and imports the module.
    project.problem_model = {
        "capabilities": [
            {
                "name": "runner-dispatch",
                "status": "working",
                "evidence_refs": [],  # no proof → unproven
                "backing_paths": ["backend/app/local_runner.py"],
            },
        ]
    }
    flag_modified(project, "problem_model")
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

    # _path_exists_at_head → True (both the backing path and its derived test path
    # resolve at HEAD). _read_file_at_head → a test body that imports the module,
    # so _named_tests_covering_paths returns the covering test ref and the sweep
    # takes the named_test_present tier instead of the bare backing_path fallback.
    test_body = "from app.local_runner import LocalRunner\n\ndef test_it():\n    assert LocalRunner\n"
    with patch(
        "app.source_ref_validation._path_exists_at_head",
        new=AsyncMock(return_value=True),
    ), patch(
        "app.capability_proof._read_file_at_head",
        new=AsyncMock(return_value=test_body),
    ), patch(
        "app.capability_proof._maybe_enroll_proof_trial",
        new=AsyncMock(return_value=None),
    ):
        results = await mis.check_capability_proof_gate_gap(ctx)

    pid = str(project.id)
    # Self-heal recorded a proof for the cap → the sweep's own accounting reports
    # it resolved (one OK result, delta 0). The recorded tier is path_proven.
    flagged = [r for r in results if r.flagged and r.subject == pid]
    assert flagged == [], f"expected no flagged results after self-heal, got {results}"
    ok = [r for r in results if r.subject == pid and r.status == CheckStatus.OK]
    assert len(ok) == 1, f"expected 1 OK result, got {results}"
    assert ok[0].delta == 0
    # ground_truth is only genuine ``proven`` caps — named_test_present is
    # path_proven tier, so ground_truth stays 0 while auto_resolved == 1.
    assert ok[0].ground_truth == 0
    assert "named_test_present" in ok[0].detail, (
        f"expected detail to name the named_test_present tier, got {ok[0].detail!r}"
    )

    # The recorded proof must be the STATIC ``named_test_present`` tier (graded
    # ``path_proven``) — NOT the executed ``named_test`` tier (graded ``proven``).
    await db.refresh(project)
    proof_records = project.capability_proof_records or {}
    assert "runner-dispatch" in proof_records, (
        f"expected proof entry for 'runner-dispatch', got {proof_records}"
    )
    entries = proof_records["runner-dispatch"]
    assert len(entries) >= 1
    assert entries[0].get("proof_kind") == "named_test_present", (
        f"expected named_test_present proof_kind, got {entries[0].get('proof_kind')!r}"
    )

    # evidence_refs should carry a named_test_present ref (proven flag set, but
    # tiered path_proven by the classifier — it does not clear the enforce gate).
    caps = (project.problem_model or {}).get("capabilities", [])
    cap = next((c for c in caps if c.get("name") == "runner-dispatch"), None)
    assert cap is not None
    evidence_refs = cap.get("evidence_refs", [])
    assert any(
        e.get("proven") is True and e.get("kind") == "named_test_present"
        for e in evidence_refs
    ), f"expected a named_test_present evidence_ref, got {evidence_refs}"

    # The capability is graded path_proven — the never-executed test file does
    # NOT graduate it to the enforce-gate-clearing ``proven`` status.
    from app.capability_registry import working_capability_proof_status
    status = working_capability_proof_status(project.problem_model)
    assert status.get("runner-dispatch") == "path_proven", (
        f"expected path_proven, got {status.get('runner-dispatch')!r}"
    )


@pytest.mark.asyncio
async def test_capability_proof_gate_gap_enrolls_trial_when_flag_on(db):
    """Slice 3 (design 84021e2d): with ``capability_proof_execution_trials_enabled``
    ON, the self-heal ENROLLS a bounded ``capability_proof_trial`` for a
    discovered-covered capability (earning the real ``proven`` tier via an actual
    test run) instead of minting the STATIC ``named_test_present`` proof."""
    from unittest.mock import AsyncMock, patch

    from sqlalchemy import select
    from sqlalchemy.orm.attributes import flag_modified

    from app.models import Task
    from app.shared_state import runtime_config

    project = await _project(db)
    project.problem_model = {
        "capabilities": [
            {
                "name": "runner-dispatch",
                "status": "working",
                "evidence_refs": [],  # no proof → unproven
                "backing_paths": ["backend/app/local_runner.py"],
            },
        ]
    }
    flag_modified(project, "problem_model")
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

    test_body = "from app.local_runner import LocalRunner\n\ndef test_it():\n    assert LocalRunner\n"
    _prev = runtime_config.get("capability_proof_execution_trials_enabled")
    runtime_config["capability_proof_execution_trials_enabled"] = True
    try:
        with patch(
            "app.source_ref_validation._path_exists_at_head",
            new=AsyncMock(return_value=True),
        ), patch(
            "app.capability_proof._read_file_at_head",
            new=AsyncMock(return_value=test_body),
        ), patch(
            "app.capability_proof._default_read_head_sha",
            new=AsyncMock(return_value="cafef00d"),
        ):
            results = await mis.check_capability_proof_gate_gap(ctx)
    finally:
        if _prev is None:
            runtime_config.pop("capability_proof_execution_trials_enabled", None)
        else:
            runtime_config["capability_proof_execution_trials_enabled"] = _prev

    pid = str(project.id)
    # No STATIC named_test_present proof was minted — the cap is handled by a
    # real execution trial instead.
    await db.refresh(project)
    proof_records = project.capability_proof_records or {}
    assert "runner-dispatch" not in proof_records, (
        f"flag-on self-heal must NOT mint a static proof, got {proof_records}"
    )

    # A bounded capability_proof_trial task was enrolled for the cap.
    tasks = (
        await db.execute(select(Task).where(Task.project_id == project.id))
    ).scalars().all()
    trial_tasks = [
        t for t in tasks
        if (t.first_payload or {}).get("type") == "capability_proof_trial"
    ]
    assert len(trial_tasks) == 1, (
        f"expected exactly 1 enrolled trial, got {[t.title for t in tasks]}"
    )
    assert trial_tasks[0].first_payload.get("cap") == "runner-dispatch"

    # The cap remains a pending gap this sweep (proof lands only when the trial
    # runs green) and is reported distinctly as an enrolled execution trial.
    flagged = [r for r in results if r.flagged and r.subject == pid]
    assert len(flagged) == 1, (
        f"expected the enrolled cap to remain a pending gap, got {results}"
    )
    assert "execution trial" in flagged[0].detail, (
        f"expected detail to name the enrolled execution trial, got {flagged[0].detail!r}"
    )


@pytest.mark.asyncio
async def test_capability_proof_gate_gap_falls_back_to_backing_path_without_test(db):
    """When the backing path resolves at HEAD but no covering test is discoverable
    (read_head → None), the self-heal falls back to the structural ``backing_path``
    tier — preserving the prior behavior for uncovered-but-present capabilities.
    """
    from unittest.mock import AsyncMock, patch
    from sqlalchemy.orm.attributes import flag_modified

    project = await _project(db)
    project.problem_model = {
        "capabilities": [
            {
                "name": "repo-map",
                "status": "working",
                "evidence_refs": [],
                "backing_paths": ["backend/app/models.py"],
            },
        ]
    }
    flag_modified(project, "problem_model")
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

    with patch(
        "app.source_ref_validation._path_exists_at_head",
        new=AsyncMock(return_value=True),
    ), patch(
        "app.capability_proof._read_file_at_head",
        new=AsyncMock(return_value=None),
    ):
        results = await mis.check_capability_proof_gate_gap(ctx)

    pid = str(project.id)
    ok = [r for r in results if r.subject == pid and r.status == CheckStatus.OK]
    assert len(ok) == 1, f"expected 1 OK result, got {results}"
    assert ok[0].delta == 0

    await db.refresh(project)
    proof_records = project.capability_proof_records or {}
    entries = proof_records.get("repo-map", [])
    assert len(entries) >= 1
    assert entries[0].get("proof_kind") == "backing_path", (
        f"expected backing_path fallback, got {entries[0].get('proof_kind')!r}"
    )


@pytest.mark.asyncio
async def test_capability_proof_gate_gap_stale_backing_path_enrolls_research(db):
    """A working cap whose declared file-like backing_path does NOT resolve at
    HEAD (stale/moved path) is a genuine gap self-heal cannot close. It must be:
      * honestly labeled "backing_paths failed verification" in the detail
        (NOT the path_proven-dedup wording),
      * counted in delta with auto_resolved == 0 (nothing was healed), and
      * enrolled for proof-earn research in enforce mode so a stale backing path
        is investigated rather than flagged forever.
    """
    from unittest.mock import AsyncMock, patch
    from sqlalchemy.orm.attributes import flag_modified

    project = await _project(db)
    project.problem_model = {
        "capabilities": [
            {
                "name": "repo-map",
                "status": "working",
                "evidence_refs": [],  # unproven
                "backing_paths": ["backend/app/does_not_exist_stale.py"],
            },
        ]
    }
    flag_modified(project, "problem_model")
    db.add(project)
    await db.commit()

    ctx = SweepContext(
        session=db,
        now=_utcnow(),
        mode="enforce",
        max_projects=25,
        sample_limit=200,
        window_days=14,
    )

    # Force HEAD verification to fail for the declared backing path.
    with patch(
        "app.source_ref_validation._path_exists_at_head",
        new=AsyncMock(return_value=False),
    ):
        results = await mis.check_capability_proof_gate_gap(ctx)

    pid = str(project.id)
    flagged = [r for r in results if r.flagged and r.subject == pid]
    assert len(flagged) == 1, f"expected 1 flagged result, got {results}"
    detail = flagged[0].detail
    assert "backing_paths failed verification" in detail, detail
    # Not mislabeled as an already-recorded path_proven dedup.
    assert "already carry a path_proven proof" not in detail, detail
    assert flagged[0].delta == 1
    assert flagged[0].auto_resolved == 0

    # The stale-backing-path cap enrolled exactly one proof-earn research row.
    rows = (
        await db.execute(
            select(EvergreenBacklogItem).where(
                EvergreenBacklogItem.project_id == project.id,
                EvergreenBacklogItem.finding_key.startswith(
                    mis.CAPABILITY_PROOF_RESEARCH_KEY_PREFIX
                ),
            )
        )
    ).scalars().all()
    assert len(rows) == 1, (
        f"expected one research obligation for the stale-path cap, got "
        f"{[r.finding_key for r in rows]}"
    )
    assert rows[0].finding_key == mis._capability_proof_research_finding_key(
        pid, "repo-map"
    )
    assert rows[0].provenance == mis.CAPABILITY_PROOF_RESEARCH_PROVENANCE


@pytest.mark.asyncio
async def test_capability_proof_gate_gap_proposes_backing_paths(db):
    """A working capability with NO declared backing_paths whose name-derived
    discovery surfaces candidate files resolves along the SAME two-tier fallback
    as the declared-backing_paths self-heal:

    * A discovered candidate with a convention test file present at HEAD is
      auto-resolved to a ``named_test_present`` (path_proven) proof.
    * A discovered candidate present at HEAD but NOT test-covered is auto-resolved
      to a ``backing_path`` (path_proven) proof — the discovery pass now records
      the backing_path fallback rather than merely folding the candidate into a
      research obligation.
    * A discovered candidate that already carries a path_proven ``backing_path``
      proof records a dedup no-op, so the cap stays flagged and its candidate is
      folded into the enrolled research obligation's ``detail``.
    * A capability whose discovery surfaces NO candidate at all stays flagged with
      NO proof recorded and a blank (candidate-less) research obligation.

    This exercises the enforce-gated production path in
    ``check_capability_proof_gate_gap`` end-to-end against a real
    EvergreenBacklogItem ledger, mocking only the IO-injectable
    discovery/verification seams.
    """
    from unittest.mock import AsyncMock, patch
    from sqlalchemy.orm.attributes import flag_modified

    project = await _project(db)
    project.problem_model = {
        "capabilities": [
            # Discovery finds a test-covered candidate → auto-resolve
            # (named_test_present, path_proven tier).
            {"name": "runner-dispatch", "status": "working", "evidence_refs": []},
            # Discovery finds a candidate present at HEAD but NOT test-covered →
            # backing_path proof recorded, auto-resolve (the behavior under test).
            {"name": "audit-sweep", "status": "working", "evidence_refs": []},
            # Discovery finds a candidate but the cap already carries a path_proven
            # backing_path proof → dedup no-op, stays flagged with the suggestion.
            {"name": "dedup-sweep", "status": "working", "evidence_refs": []},
            # Discovery finds NO candidate → stays flagged, no proof recorded.
            {"name": "orphan-cap", "status": "working", "evidence_refs": []},
        ]
    }
    flag_modified(project, "problem_model")
    # Pre-seed a path_proven backing_path proof for dedup-sweep so the discovery
    # pass's record_capability_backing_path_proof is a dedup no-op (path_proven
    # already present) and the cap stays in the gap.
    project.capability_proof_records = {
        "dedup-sweep": [
            {
                "task_id": "backing:dedup-sweep",
                "proved_at": _utcnow().isoformat(),
                "proof_kind": "backing_path",
                "proof_ref": "dedup-sweep",
                "commit_sha": "",
            }
        ]
    }
    flag_modified(project, "capability_proof_records")
    db.add(project)
    await db.commit()

    covered_candidate = "backend/app/local_runner.py"
    uncovered_candidate = "backend/app/some_audit_sweep_module.py"
    dedup_candidate = "backend/app/some_dedup_sweep_module.py"

    async def _fake_discover(cap_name, **_kwargs):
        if cap_name == "runner-dispatch":
            return [covered_candidate]
        if cap_name == "audit-sweep":
            return [uncovered_candidate]
        if cap_name == "dedup-sweep":
            return [dedup_candidate]
        return []

    # The covered candidate's conventional test resolves at HEAD and imports the
    # module → named_test_present tier. The other candidates' test reads return
    # None → no coverage → fall to the backing_path fallback. We drive this purely
    # through _read_file_at_head keyed by path (test file for the covered candidate
    # only).
    covered_test_path = "backend/testing/test_local_runner.py"
    test_body = "from app.local_runner import LocalRunner\n\ndef test_it():\n    assert LocalRunner\n"

    async def _fake_read(path):
        return test_body if path == covered_test_path else None

    ctx = SweepContext(
        session=db,
        now=_utcnow(),
        mode="enforce",
        max_projects=25,
        sample_limit=200,
        window_days=14,
    )
    with patch(
        "app.capability_proof._discover_candidate_backing_paths",
        new=_fake_discover,
    ), patch(
        "app.source_ref_validation._path_exists_at_head",
        new=AsyncMock(return_value=True),
    ), patch(
        "app.capability_proof._read_file_at_head",
        new=_fake_read,
    ), patch(
        "app.capability_proof._maybe_enroll_proof_trial",
        new=AsyncMock(return_value=None),
    ):
        results = await mis.check_capability_proof_gate_gap(ctx)

    pid = str(project.id)

    # runner-dispatch auto-resolved to a named_test_present (path_proven) proof.
    await db.refresh(project)
    proof_records = project.capability_proof_records or {}
    assert "runner-dispatch" in proof_records, (
        f"expected discovery-driven named_test_present proof for 'runner-dispatch', "
        f"got {proof_records}"
    )
    rd_entries = proof_records["runner-dispatch"]
    assert rd_entries[0].get("proof_kind") == "named_test_present", (
        f"expected named_test_present proof_kind from discovery, got "
        f"{rd_entries[0].get('proof_kind')!r}"
    )

    # audit-sweep: present-but-not-test-covered candidate now records a
    # backing_path (path_proven) proof and auto-resolves.
    assert "audit-sweep" in proof_records, (
        "audit-sweep must be auto-resolved with a backing_path proof for a "
        f"discovered candidate present at HEAD, got {proof_records}"
    )
    as_entries = proof_records["audit-sweep"]
    assert as_entries[0].get("proof_kind") == "backing_path", (
        f"expected backing_path proof_kind from discovery fallback, got "
        f"{as_entries[0].get('proof_kind')!r}"
    )

    # orphan-cap: no candidate found → no proof recorded.
    assert "orphan-cap" not in proof_records, (
        "orphan-cap must NOT be auto-proven when discovery surfaces no candidate"
    )

    # Still flagged: dedup-sweep (dedup no-op) and orphan-cap (no candidate)
    # remain unresolved gaps; audit-sweep and runner-dispatch auto-resolved.
    flagged = [r for r in results if r.flagged and r.subject == pid]
    assert len(flagged) == 1, f"expected 1 flagged result, got {results}"
    assert "dedup-sweep" in flagged[0].detail
    assert "orphan-cap" in flagged[0].detail
    # audit-sweep is reported as auto-resolved (via backing_paths), not as an
    # unresolved gap needing backing_paths.
    assert "1 via backing_paths" in flagged[0].detail, (
        f"expected audit-sweep counted as a backing_paths auto-resolve, got "
        f"{flagged[0].detail!r}"
    )
    assert "discovered candidate" in flagged[0].detail, (
        f"expected detail to mention discovered candidate suggestions, got "
        f"{flagged[0].detail!r}"
    )

    # Research obligations: dedup-sweep (dedup no-op, keeps the candidate
    # suggestion) and orphan-cap (no candidate) each enroll one; audit-sweep and
    # runner-dispatch auto-resolved and enroll none.
    rows = (
        await db.execute(
            select(EvergreenBacklogItem).where(
                EvergreenBacklogItem.project_id == project.id,
                EvergreenBacklogItem.finding_key.startswith(
                    mis.CAPABILITY_PROOF_RESEARCH_KEY_PREFIX
                ),
            )
        )
    ).scalars().all()
    keys = {r.finding_key for r in rows}
    assert keys == {
        mis._capability_proof_research_finding_key(pid, "dedup-sweep"),
        mis._capability_proof_research_finding_key(pid, "orphan-cap"),
    }, f"expected dedup-sweep + orphan-cap research obligations, got {keys}"

    # The dedup-sweep obligation carries its discovered candidate path (the full
    # discovery→suggestion pipeline); orphan-cap's obligation has no candidate.
    by_key = {r.finding_key: r for r in rows}
    dedup_row = by_key[mis._capability_proof_research_finding_key(pid, "dedup-sweep")]
    assert dedup_candidate in dedup_row.detail, (
        f"expected discovered candidate path folded into obligation detail, got "
        f"{dedup_row.detail!r}"
    )
    orphan_row = by_key[mis._capability_proof_research_finding_key(pid, "orphan-cap")]
    assert dedup_candidate not in orphan_row.detail


@pytest.mark.asyncio
async def test_capability_proof_gate_gap_ok_when_all_proven(db):
    """All working capabilities with corroborated proof produce an OK result."""
    from sqlalchemy.orm.attributes import flag_modified

    project = await _project(db)
    project.problem_model = {
        "capabilities": [
            {
                "name": "repo-map",
                "status": "working",
                "evidence_refs": [{"proven": True, "ref": "backend/app/x.py"}],
            },
        ]
    }
    # Seed a platform ledger entry so consumer_gate_proof_status corroborates it.
    project.capability_proof_records = {
        "repo-map": [
            {"proof_kind": "named_test", "proven": True, "ref": "backend/app/x.py"},
        ]
    }
    flag_modified(project, "problem_model")
    flag_modified(project, "capability_proof_records")
    db.add(project)
    await db.commit()

    ctx = SweepContext(
        session=db, now=_utcnow(), mode="observe", max_projects=25,
        sample_limit=200, window_days=14,
    )
    results = await mis.check_capability_proof_gate_gap(ctx)
    pid = str(project.id)
    assert not any(r.flagged for r in results if r.subject == pid)
    ok = [r for r in results if r.subject == pid and r.status == CheckStatus.OK]
    assert len(ok) == 1
    assert ok[0].delta == 0


@pytest.mark.asyncio
async def test_capability_proof_gate_gap_exempt_not_flagged(db):
    """A working capability with an operator-recorded platform exemption is NOT
    counted in the gap delta — the enforce gate would clear it."""
    from sqlalchemy.orm.attributes import flag_modified

    project = await _project(db)
    project.problem_model = {
        "capabilities": [
            {
                "name": "exempt-cap",
                "status": "working",
                "evidence_refs": [],  # no machine proof
            },
            {
                "name": "non-exempt-cap",
                "status": "working",
                "evidence_refs": [],  # genuinely unproven
            },
        ]
    }
    # Operator-recorded exemption in the platform ledger.
    project.capability_proof_records = {
        "exempt-cap": [
            {
                "task_id": "operator:exempt-cap",
                "proved_at": _utcnow().isoformat(),
                "proof_kind": "operator_exemption",
                "proof_ref": "exempt-cap",
                "commit_sha": "",
            }
        ]
    }
    flag_modified(project, "problem_model")
    flag_modified(project, "capability_proof_records")
    db.add(project)
    await db.commit()

    ctx = SweepContext(
        session=db, now=_utcnow(), mode="observe", max_projects=25,
        sample_limit=200, window_days=14,
    )
    results = await mis.check_capability_proof_gate_gap(ctx)
    pid = str(project.id)
    flagged = [r for r in results if r.flagged and r.subject == pid]
    assert len(flagged) == 1, f"expected 1 flagged result, got {results}"
    f = flagged[0]
    # The exempt cap is excluded from both total_working and delta.
    # Only non-exempt-cap remains in the gap.
    assert f.self_reported == 2  # total working including exempt
    # exempt-cap grades proven via platform exemption → ground_truth = 1
    assert f.ground_truth == 1
    # delta = non-exempt-cap still needs proof
    assert f.delta == 1
    assert "non-exempt-cap" in f.detail
    # Exempt must be mentioned in the detail.
    assert "exempt" in f.detail


@pytest.mark.asyncio
async def test_capability_proof_gate_gap_ledger_only_proof_not_flagged(db):
    """A working capability whose platform ledger holds a proof entry but whose
    problem_model evidence_refs append was lost is NOT flagged as a gap —
    consumer_gate_proof_status folds the ledger proof up to proven."""
    from sqlalchemy.orm.attributes import flag_modified

    project = await _project(db)
    project.problem_model = {
        "capabilities": [
            {
                "name": "ledger-only-cap",
                "status": "working",
                "evidence_refs": [],  # problem_model evidence was lost
            },
        ]
    }
    # Platform ledger still holds the proof.
    project.capability_proof_records = {
        "ledger-only-cap": [
            {
                "task_id": "test:ledger-only-cap",
                "proved_at": _utcnow().isoformat(),
                "proof_kind": "named_test",
                "proof_ref": "testing/test_foo.py",
                "commit_sha": "a" * 40,
            }
        ]
    }
    flag_modified(project, "problem_model")
    flag_modified(project, "capability_proof_records")
    db.add(project)
    await db.commit()

    ctx = SweepContext(
        session=db, now=_utcnow(), mode="observe", max_projects=25,
        sample_limit=200, window_days=14,
    )
    results = await mis.check_capability_proof_gate_gap(ctx)
    pid = str(project.id)
    # No flagged results — the ledger-only proof is recognized.
    assert not any(r.flagged for r in results if r.subject == pid), (
        f"expected no flagged result for ledger-only proof, got {results}"
    )
    ok = [r for r in results if r.subject == pid and r.status == CheckStatus.OK]
    assert len(ok) == 1
    assert ok[0].delta == 0
    assert ok[0].ground_truth == 1  # ledger-only cap is proven
    assert ok[0].self_reported == 1


@pytest.mark.asyncio
async def test_capability_proof_gate_gap_unverified_proven_is_flagged(db):
    """A working capability that self-declares ``proven`` via problem_model
    evidence_refs but has NO platform ledger corroboration is demoted to
    ``unverified`` by consumer_gate_proof_status and correctly counted in the
    gap delta — the enforce gate would reject it."""
    from sqlalchemy.orm.attributes import flag_modified

    project = await _project(db)
    project.problem_model = {
        "capabilities": [
            {
                "name": "self-declared-cap",
                "status": "working",
                "evidence_refs": [{"proven": True, "ref": "backend/app/x.py"}],
            },
        ]
    }
    # NO capability_proof_records — the self-declared claim cannot be corroborated.
    flag_modified(project, "problem_model")
    db.add(project)
    await db.commit()

    ctx = SweepContext(
        session=db, now=_utcnow(), mode="observe", max_projects=25,
        sample_limit=200, window_days=14,
    )
    results = await mis.check_capability_proof_gate_gap(ctx)
    pid = str(project.id)
    flagged = [r for r in results if r.flagged and r.subject == pid]
    assert len(flagged) == 1, (
        f"expected 1 flagged result for self-declared unverified cap, got {results}"
    )
    f = flagged[0]
    assert f.self_reported == 1
    # ground_truth = 0 because the self-declared claim is NOT corroborated
    assert f.ground_truth == 0, (
        f"ground_truth should be 0 (self-declared without ledger is unverified), "
        f"got {f.ground_truth}"
    )
    assert f.delta == 1
    # The detail should mention the capability name.
    assert "self-declared-cap" in f.detail


@pytest.mark.asyncio
async def test_capability_proof_gate_gap_skips_with_no_capabilities(db):
    """A project with an empty capabilities list produces a SKIPPED result."""
    project = await _project(db)
    # No problem_model set → SKIPPED

    ctx = SweepContext(
        session=db, now=_utcnow(), mode="observe", max_projects=25,
        sample_limit=200, window_days=14,
    )
    results = await mis.check_capability_proof_gate_gap(ctx)
    pid = str(project.id)
    skipped = [r for r in results if r.subject in (pid, "global") and r.status == CheckStatus.SKIPPED]
    assert len(skipped) >= 1


@pytest.mark.asyncio
async def test_capability_proof_gate_gap_enrolls_research_for_no_backing_paths(db):
    """ENFORCE mode: each unproven working capability with NO file-like
    backing_paths and no proof (the ``needs_backing_paths`` bucket) enrolls
    exactly one deduped per-capability proof-earn research obligation, and
    re-running the check enrolls nothing new (idempotent across ticks).

    This is the named acceptance test for the gate-gap research-enrollment
    feature. It exercises the enforce-gated production path in
    ``check_capability_proof_gate_gap`` end-to-end against a real
    EvergreenBacklogItem ledger.
    """
    from sqlalchemy.orm.attributes import flag_modified

    project = await _project(db)
    # Two working capabilities, both unproven with NO backing_paths → both land
    # in needs_backing_paths and must each get a research obligation.
    project.problem_model = {
        "capabilities": [
            {"name": "repo-map", "status": "working", "evidence_refs": []},
            {"name": "ci-integration", "status": "working", "evidence_refs": []},
        ]
    }
    flag_modified(project, "problem_model")
    db.add(project)
    await db.commit()

    ctx = SweepContext(
        session=db,
        now=_utcnow(),
        mode="enforce",
        max_projects=25,
        sample_limit=200,
        window_days=14,
    )
    results = await mis.check_capability_proof_gate_gap(ctx)

    pid = str(project.id)
    flagged = [r for r in results if r.flagged and r.subject == pid]
    assert len(flagged) == 1, f"expected 1 flagged result, got {results}"

    rows = (
        await db.execute(
            select(EvergreenBacklogItem).where(
                EvergreenBacklogItem.project_id == project.id
            )
        )
    ).scalars().all()
    research_rows = [
        r
        for r in rows
        if r.finding_key.startswith(mis.CAPABILITY_PROOF_RESEARCH_KEY_PREFIX)
    ]
    # Exactly one deduped item per unproven capability that needs proof research.
    assert len(research_rows) == 2, (
        f"expected 2 per-capability research obligations, got "
        f"{[r.finding_key for r in research_rows]}"
    )
    keys = {r.finding_key for r in research_rows}
    assert keys == {
        mis._capability_proof_research_finding_key(pid, "repo-map"),
        mis._capability_proof_research_finding_key(pid, "ci-integration"),
    }
    # Provenance is the dedicated proof-research value, distinct from the generic
    # metrics-integrity drift provenance.
    for r in research_rows:
        assert r.provenance == mis.CAPABILITY_PROOF_RESEARCH_PROVENANCE
        assert r.provenance != mis.METRICS_INTEGRITY_PROVENANCE
        assert r.status == "enrolled"
        assert "repo-map" in r.detail or "ci-integration" in r.detail
    # finding_key distinct from the generic per-check drift finding key.
    generic_key = flagged[0].finding_key()
    assert generic_key not in keys
    assert generic_key == f"metrics-integrity:capability_proof_gate_gap:{pid}"

    # Idempotency: a second tick creates NO new rows (deduped on finding_key).
    ctx2 = SweepContext(
        session=db,
        now=_utcnow(),
        mode="enforce",
        max_projects=25,
        sample_limit=200,
        window_days=14,
    )
    await mis.check_capability_proof_gate_gap(ctx2)
    rows_after = (
        await db.execute(
            select(EvergreenBacklogItem).where(
                EvergreenBacklogItem.project_id == project.id,
                EvergreenBacklogItem.finding_key.startswith(
                    mis.CAPABILITY_PROOF_RESEARCH_KEY_PREFIX
                ),
            )
        )
    ).scalars().all()
    assert len(rows_after) == 2, (
        "second enforce tick must not create duplicate research obligations, "
        f"got {[r.finding_key for r in rows_after]}"
    )


@pytest.mark.asyncio
async def test_capability_proof_gate_gap_observe_does_not_enroll(db):
    """OBSERVE mode: the same no-backing-paths capability enrolls NO backlog item
    (enrollment is enforce-gated), while the always-on backing-path self-heal
    still records a proof for a cap whose backing_path is present at HEAD.
    """
    from unittest.mock import AsyncMock, patch
    from sqlalchemy.orm.attributes import flag_modified

    project = await _project(db)
    project.problem_model = {
        "capabilities": [
            # No backing_paths → would enroll research in enforce mode only.
            {"name": "repo-map", "status": "working", "evidence_refs": []},
            # Backing path present at HEAD → self-heals in observe mode unchanged.
            {
                "name": "ci-integration",
                "status": "working",
                "evidence_refs": [],
                "backing_paths": ["backend/app/models.py"],
            },
        ]
    }
    flag_modified(project, "problem_model")
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
    with patch(
        "app.source_ref_validation._path_exists_at_head",
        new=AsyncMock(return_value=True),
    ):
        results = await mis.check_capability_proof_gate_gap(ctx)

    # No enrollment in observe mode: zero research obligations created.
    research_rows = (
        await db.execute(
            select(EvergreenBacklogItem).where(
                EvergreenBacklogItem.project_id == project.id,
                EvergreenBacklogItem.finding_key.startswith(
                    mis.CAPABILITY_PROOF_RESEARCH_KEY_PREFIX
                ),
            )
        )
    ).scalars().all()
    assert research_rows == [], (
        f"observe mode must enroll no research obligations, got "
        f"{[r.finding_key for r in research_rows]}"
    )

    # Backing-path self-heal still fires in observe mode: ci-integration proven.
    await db.refresh(project)
    proof_records = project.capability_proof_records or {}
    assert "ci-integration" in proof_records, (
        f"expected observe-mode self-heal proof for 'ci-integration', got "
        f"{proof_records}"
    )
    pid = str(project.id)
    flagged = [r for r in results if r.flagged and r.subject == pid]
    # repo-map remains an unresolved gap (needs_backing_paths), so still flagged.
    assert len(flagged) == 1
    assert "repo-map" in flagged[0].detail



@pytest.mark.asyncio
async def test_capability_proof_gate_gap_path_proven_not_counted_as_proven(db):
    """A working capability auto-resolved to the ``path_proven`` tier (via
    backing_path or static named_test_present self-heal) must NOT appear in
    ``ground_truth`` — that counter is reserved for true machine-verified
    ``proven`` status earned by a real green merge-test verdict.

    Scenario: one working cap with a present backing_path is self-healed to
    ``path_proven``.  A second working cap already carries a real ``proven``
    evidence_ref.  After the check runs:
      - ground_truth == 1 (only the truly proven cap)
      - auto_resolved == 1 (the path_proven self-heal is counted as throughput)
    """
    from unittest.mock import AsyncMock, patch
    from sqlalchemy.orm.attributes import flag_modified

    project = await _project(db)
    project.problem_model = {
        "capabilities": [
            {
                "name": "path-healed-cap",
                "status": "working",
                # No proof yet — will be auto-resolved to path_proven tier.
                "evidence_refs": [],
                "backing_paths": ["backend/app/models.py"],
            },
            {
                "name": "truly-proven-cap",
                "status": "working",
                # A real proven evidence_ref so working_capability_proof_status
                # returns "proven" for this capability.
                "evidence_refs": [
                    {"kind": "named_test", "ref": "test_foo", "proven": True}
                ],
            },
        ]
    }
    # Seed a real proof record so working_capability_proof_status grades it proven.
    project.capability_proof_records = {
        "truly-proven-cap": [
            {"proof_kind": "named_test", "proven": True}
        ]
    }
    flag_modified(project, "problem_model")
    flag_modified(project, "capability_proof_records")
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

    with patch(
        "app.source_ref_validation._path_exists_at_head",
        new=AsyncMock(return_value=True),
    ), patch(
        "app.capability_proof._read_file_at_head",
        new=AsyncMock(return_value=None),
    ):
        results = await mis.check_capability_proof_gate_gap(ctx)

    pid = str(project.id)
    proj_results = [r for r in results if r.subject == pid]
    assert proj_results, f"expected a result for project {pid}, got {results}"
    r = proj_results[0]

    # path_proven self-heals must NOT inflate ground_truth — only the one truly
    # proven cap contributes to the machine-verified coverage counter.
    assert r.ground_truth == 1, (
        f"ground_truth should be 1 (only truly-proven-cap), got {r.ground_truth}; "
        "path_proven self-heals must not be counted as proven"
    )
    # The self-heal throughput counter (auto_resolved) is still reported correctly.
    assert r.auto_resolved == 1, (
        f"auto_resolved should be 1 (path-healed-cap), got {r.auto_resolved}"
    )


# --- earn obligations for trial-ineligible (path_proven, no covering test) ---


@pytest.mark.asyncio
async def test_capability_proof_gate_gap_enrolls_earn_obligation_for_no_test_cap(db):
    """ENFORCE mode: a working cap that already has a backing_path proof but NO
    covering test (trial-ineligible) gets a CompletionDebtEntry enrolled with
    kind='capability_earn_obligation' and the stable dedupe key.

    This test exercises the production path added in
    _enroll_trial_ineligible_earn_obligation (metrics_integrity_sweep.py) and
    capability_earn_obligation_dedupe_key (completion_debt.py).
    """
    from unittest.mock import AsyncMock, patch
    from sqlalchemy.orm.attributes import flag_modified
    from app.completion_debt import (
        KIND_CAPABILITY_EARN_OBLIGATION,
        STATUS_OPEN,
        capability_earn_obligation_dedupe_key,
    )
    from app.models import CompletionDebtEntry

    project = await _project(db)
    project.problem_model = {
        "capabilities": [
            {
                "name": "repo-map",
                "status": "working",
                "evidence_refs": [],
                "backing_paths": ["backend/app/models.py"],
            },
        ]
    }
    flag_modified(project, "problem_model")
    db.add(project)
    await db.commit()

    ctx = SweepContext(
        session=db,
        now=_utcnow(),
        mode="enforce",
        max_projects=25,
        sample_limit=200,
        window_days=14,
    )

    # First tick: path exists but no covering test file (read_head → None so
    # _named_tests_covering_paths returns []).  The backing_path proof is NOT
    # yet recorded — it will be recorded on this tick and auto-resolved.
    with patch(
        "app.source_ref_validation._path_exists_at_head",
        new=AsyncMock(return_value=True),
    ), patch(
        "app.capability_proof._read_file_at_head",
        new=AsyncMock(return_value=None),
    ):
        results = await mis.check_capability_proof_gate_gap(ctx)

    pid = str(project.id)
    # First tick auto-resolves the cap to path_proven (backing_path tier).
    # The cap is NOT yet in already_recorded_no_test — it was auto-resolved.
    assert any(r.subject == pid for r in results)

    # Second tick: now the proof exists (dedup no-op → already_recorded_no_test).
    ctx2 = SweepContext(
        session=db,
        now=_utcnow(),
        mode="enforce",
        max_projects=25,
        sample_limit=200,
        window_days=14,
    )
    with patch(
        "app.source_ref_validation._path_exists_at_head",
        new=AsyncMock(return_value=True),
    ), patch(
        "app.capability_proof._read_file_at_head",
        new=AsyncMock(return_value=None),
    ):
        results2 = await mis.check_capability_proof_gate_gap(ctx2)

    # The earn obligation must now exist.
    project_uuid = project.id
    expected_key = capability_earn_obligation_dedupe_key(str(project_uuid), "repo-map")
    debt_rows = (
        await db.execute(
            select(CompletionDebtEntry).where(
                CompletionDebtEntry.project_id == project_uuid,
                CompletionDebtEntry.kind == KIND_CAPABILITY_EARN_OBLIGATION,
            )
        )
    ).scalars().all()
    assert len(debt_rows) == 1, (
        f"expected 1 earn obligation, got {[r.dedupe_key for r in debt_rows]}"
    )
    row = debt_rows[0]
    assert row.dedupe_key == expected_key
    assert row.status == STATUS_OPEN
    assert "backing_path" in (row.detail or {}).get("description", "")

    # The flagged result must mention the no-test wording.
    flagged2 = [r for r in results2 if r.flagged and r.subject == pid]
    assert len(flagged2) == 1
    assert "trial-ineligible" in flagged2[0].detail or "no covering test" in flagged2[0].detail

    # Idempotency: a third tick must NOT create a duplicate obligation.
    ctx3 = SweepContext(
        session=db,
        now=_utcnow(),
        mode="enforce",
        max_projects=25,
        sample_limit=200,
        window_days=14,
    )
    with patch(
        "app.source_ref_validation._path_exists_at_head",
        new=AsyncMock(return_value=True),
    ), patch(
        "app.capability_proof._read_file_at_head",
        new=AsyncMock(return_value=None),
    ):
        await mis.check_capability_proof_gate_gap(ctx3)

    debt_rows_after = (
        await db.execute(
            select(CompletionDebtEntry).where(
                CompletionDebtEntry.project_id == project_uuid,
                CompletionDebtEntry.kind == KIND_CAPABILITY_EARN_OBLIGATION,
            )
        )
    ).scalars().all()
    assert len(debt_rows_after) == 1, (
        "third tick must not create duplicate earn obligations"
    )


@pytest.mark.asyncio
async def test_capability_proof_gate_gap_observe_does_not_enroll_earn_obligation(db):
    """OBSERVE mode: the earn obligation for a trial-ineligible cap is NOT enrolled
    (obligations are enforce-mode only, matching the research-obligation policy).
    """
    from unittest.mock import AsyncMock, patch
    from sqlalchemy.orm.attributes import flag_modified
    from app.completion_debt import KIND_CAPABILITY_EARN_OBLIGATION
    from app.models import CompletionDebtEntry

    project = await _project(db)
    project.problem_model = {
        "capabilities": [
            {
                "name": "repo-map",
                "status": "working",
                "evidence_refs": [],
                "backing_paths": ["backend/app/models.py"],
            },
        ]
    }
    flag_modified(project, "problem_model")
    db.add(project)
    await db.commit()

    # Run twice so the second tick hits already_recorded_no_test.
    for _ in range(2):
        ctx = SweepContext(
            session=db,
            now=_utcnow(),
            mode="observe",
            max_projects=25,
            sample_limit=200,
            window_days=14,
        )
        with patch(
            "app.source_ref_validation._path_exists_at_head",
            new=AsyncMock(return_value=True),
        ), patch(
            "app.capability_proof._read_file_at_head",
            new=AsyncMock(return_value=None),
        ):
            await mis.check_capability_proof_gate_gap(ctx)

    debt_rows = (
        await db.execute(
            select(CompletionDebtEntry).where(
                CompletionDebtEntry.project_id == project.id,
                CompletionDebtEntry.kind == KIND_CAPABILITY_EARN_OBLIGATION,
            )
        )
    ).scalars().all()
    assert len(debt_rows) == 0, (
        "observe mode must not enroll earn obligations for trial-ineligible caps"
    )


@pytest.mark.asyncio
async def test_capability_proof_gate_gap_has_test_does_not_enroll_earn_obligation(db):
    """A cap with a named_test_present proof (trial-ELIGIBLE) must NOT get an
    earn obligation — it already has test coverage and is just awaiting trial
    execution.
    """
    from unittest.mock import AsyncMock, patch
    from sqlalchemy.orm.attributes import flag_modified
    from app.completion_debt import KIND_CAPABILITY_EARN_OBLIGATION
    from app.models import CompletionDebtEntry

    project = await _project(db)
    project.problem_model = {
        "capabilities": [
            {
                "name": "repo-map",
                "status": "working",
                "evidence_refs": [],
                "backing_paths": ["backend/app/models.py"],
            },
        ]
    }
    flag_modified(project, "problem_model")
    db.add(project)
    await db.commit()

    FAKE_TEST_CONTENT = "import backend.app.models\n"

    # Run twice so the second tick hits already_recorded_has_test.
    for _ in range(2):
        ctx = SweepContext(
            session=db,
            now=_utcnow(),
            mode="enforce",
            max_projects=25,
            sample_limit=200,
            window_days=14,
        )
        with patch(
            "app.source_ref_validation._path_exists_at_head",
            new=AsyncMock(return_value=True),
        ), patch(
            "app.capability_proof._read_file_at_head",
            new=AsyncMock(return_value=FAKE_TEST_CONTENT),
        ):
            await mis.check_capability_proof_gate_gap(ctx)

    debt_rows = (
        await db.execute(
            select(CompletionDebtEntry).where(
                CompletionDebtEntry.project_id == project.id,
                CompletionDebtEntry.kind == KIND_CAPABILITY_EARN_OBLIGATION,
            )
        )
    ).scalars().all()
    assert len(debt_rows) == 0, (
        "trial-eligible cap (has covering test) must not get an earn obligation"
    )


@pytest.mark.asyncio
async def test_capability_proof_gate_gap_resolves_earn_obligation_on_remediation(db):
    """Regression: when a trial-ineligible cap gains test coverage, its earn
    obligation is explicitly RESOLVED without duplicate open debt.

    Lifecycle:
    1. Tick 1 (no test coverage) → auto-resolves backing_path proof.
    2. Tick 2 (no test coverage, dedup) → cap is already_recorded_no_test;
       earn obligation ENROLLED.
    3. Tick 3 (test coverage appears) → cap auto-resolves named_test_present;
       the earn obligation must transition to RESOLVED.
    4. Tick 4 (test coverage still present, dedup) → already_recorded_has_test;
       NO new earn obligation enrolled, prior row stays RESOLVED.
    """
    from unittest.mock import AsyncMock, patch
    from sqlalchemy.orm.attributes import flag_modified
    from app.completion_debt import (
        KIND_CAPABILITY_EARN_OBLIGATION,
        STATUS_OPEN,
        STATUS_RESOLVED,
        capability_earn_obligation_dedupe_key,
    )
    from app.models import CompletionDebtEntry

    project = await _project(db)
    project.problem_model = {
        "capabilities": [
            {
                "name": "repo-map",
                "status": "working",
                "evidence_refs": [],
                "backing_paths": ["backend/app/models.py"],
            },
        ]
    }
    flag_modified(project, "problem_model")
    db.add(project)
    await db.commit()

    project_uuid = project.id
    expected_key = capability_earn_obligation_dedupe_key(str(project_uuid), "repo-map")

    FAKE_TEST_CONTENT = "from app.models import Something\n"

    # ── Tick 1: no test coverage → backing_path proof auto-resolved ──────────
    ctx1 = SweepContext(
        session=db,
        now=_utcnow(),
        mode="enforce",
        max_projects=25,
        sample_limit=200,
        window_days=14,
    )
    with patch(
        "app.source_ref_validation._path_exists_at_head",
        new=AsyncMock(return_value=True),
    ), patch(
        "app.capability_proof._read_file_at_head",
        new=AsyncMock(return_value=None),
    ):
        await mis.check_capability_proof_gate_gap(ctx1)

    # ── Tick 2: no test coverage, dedup → earn obligation enrolled ───────────
    ctx2 = SweepContext(
        session=db,
        now=_utcnow(),
        mode="enforce",
        max_projects=25,
        sample_limit=200,
        window_days=14,
    )
    with patch(
        "app.source_ref_validation._path_exists_at_head",
        new=AsyncMock(return_value=True),
    ), patch(
        "app.capability_proof._read_file_at_head",
        new=AsyncMock(return_value=None),
    ):
        await mis.check_capability_proof_gate_gap(ctx2)

    # Confirm the obligation was enrolled and is OPEN.
    debt_rows = (
        await db.execute(
            select(CompletionDebtEntry).where(
                CompletionDebtEntry.project_id == project_uuid,
                CompletionDebtEntry.kind == KIND_CAPABILITY_EARN_OBLIGATION,
            )
        )
    ).scalars().all()
    assert len(debt_rows) == 1
    row = debt_rows[0]
    assert row.dedupe_key == expected_key
    assert row.status == STATUS_OPEN

    # ── Tick 3: test coverage appears → auto-resolves named_test_present ─────
    # The earn obligation must transition to RESOLVED.
    ctx3 = SweepContext(
        session=db,
        now=_utcnow(),
        mode="enforce",
        max_projects=25,
        sample_limit=200,
        window_days=14,
    )
    with patch(
        "app.source_ref_validation._path_exists_at_head",
        new=AsyncMock(return_value=True),
    ), patch(
        "app.capability_proof._read_file_at_head",
        new=AsyncMock(return_value=FAKE_TEST_CONTENT),
    ):
        await mis.check_capability_proof_gate_gap(ctx3)

    # Verify the obligation is now RESOLVED.
    debt_rows_after = (
        await db.execute(
            select(CompletionDebtEntry).where(
                CompletionDebtEntry.project_id == project_uuid,
                CompletionDebtEntry.kind == KIND_CAPABILITY_EARN_OBLIGATION,
            )
        )
    ).scalars().all()
    assert len(debt_rows_after) == 1, (
        "remediation must not create duplicate debt rows"
    )
    resolved_row = debt_rows_after[0]
    assert resolved_row.dedupe_key == expected_key
    assert resolved_row.status == STATUS_RESOLVED, (
        f"expected RESOLVED after gaining test coverage, got {resolved_row.status}"
    )

    # ── Tick 4: test coverage still present, dedup → already_recorded_has_test ──
    # Must NOT re-open or create a new obligation.
    ctx4 = SweepContext(
        session=db,
        now=_utcnow(),
        mode="enforce",
        max_projects=25,
        sample_limit=200,
        window_days=14,
    )
    with patch(
        "app.source_ref_validation._path_exists_at_head",
        new=AsyncMock(return_value=True),
    ), patch(
        "app.capability_proof._read_file_at_head",
        new=AsyncMock(return_value=FAKE_TEST_CONTENT),
    ):
        await mis.check_capability_proof_gate_gap(ctx4)

    # The row must STILL be RESOLVED (not reopened) and there must be exactly
    # one row — no duplicate.
    debt_rows_final = (
        await db.execute(
            select(CompletionDebtEntry).where(
                CompletionDebtEntry.project_id == project_uuid,
                CompletionDebtEntry.kind == KIND_CAPABILITY_EARN_OBLIGATION,
            )
        )
    ).scalars().all()
    assert len(debt_rows_final) == 1, (
        "subsequent ticks must not create duplicate earn obligations"
    )
    final_row = debt_rows_final[0]
    assert final_row.status == STATUS_RESOLVED, (
        f"earn obligation must stay RESOLVED across subsequent ticks, "
        f"got {final_row.status}"
    )


# --- Check: capability_proof_gate_auto_graduation ----------------------------


_GRADUATION_RC_KEYS = (
    "capability_proof_gate_auto_graduation_enabled",
    "capability_proof_gate_mode",
    "capability_proof_gate_auto_graduation_observe_threshold",
    "capability_proof_gate_auto_graduation_enforce_threshold",
)


def _set_auto_graduation_config(
    enabled: bool = True,
    mode: str = "off",
    observe_threshold: float = 0.5,
    enforce_threshold: float = 0.95,
):
    """Set runtime_config keys for the auto-graduation check.

    Clears all graduation-related keys first so tests that call this helper
    start with a clean slate regardless of what prior tests left behind.
    """
    for k in _GRADUATION_RC_KEYS:
        _rc.pop(k, None)
    _rc["capability_proof_gate_auto_graduation_enabled"] = enabled
    _rc["capability_proof_gate_mode"] = mode
    _rc["capability_proof_gate_auto_graduation_observe_threshold"] = observe_threshold
    _rc["capability_proof_gate_auto_graduation_enforce_threshold"] = enforce_threshold


async def _seed_auto_graduation_deployment_state(db) -> None:
    """Create the durable singleton required before a gate mode can advance."""
    from app.models import DeploymentState

    if await db.get(DeploymentState, 1) is None:
        db.add(DeploymentState())
        await db.commit()


def _proven_ref(recorded_at: str | None = None) -> dict:
    """A minimal proven evidence_ref for a named_test."""
    ref = {"kind": "named_test", "ref": "testing/test_foo.py::test_bar", "proven": True}
    if recorded_at is not None:
        ref["recorded_at"] = recorded_at
    return ref


def _set_machine_proof_records(
    project, *names: str, proved_at: str | None = None
) -> None:
    """Corroborate test capabilities through the platform proof ledger."""
    from app.capability_registry import normalize_capability_name

    timestamp = proved_at or _utcnow().isoformat()
    records = dict(project.capability_proof_records or {})
    for raw_name in names:
        name = normalize_capability_name(raw_name)
        assert name is not None
        records[name] = [
            {
                "task_id": f"proof:{name}",
                "proof_kind": "named_test",
                "proof_ref": f"testing/test_{name}.py::test_{name}",
                "proved_at": timestamp,
                "commit_sha": "a" * 40,
            }
        ]
    project.capability_proof_records = records
    flag_modified(project, "capability_proof_records")


@pytest.mark.asyncio
async def test_auto_graduation_check_disabled_by_default(db):
    """The check is a no-op when the feature flag is not enabled."""
    # Ensure no leftover graduation keys from prior tests.
    for k in _GRADUATION_RC_KEYS:
        _rc.pop(k, None)
    ctx = SweepContext(
        session=db, now=_utcnow(), mode="observe", max_projects=25,
        sample_limit=200, window_days=14,
    )
    results = await mis.check_capability_proof_gate_auto_graduation(ctx)
    assert len(results) == 1
    r = results[0]
    assert r.status == CheckStatus.OK
    assert "disabled" in r.detail
    # Gate mode must be unchanged: the check must not set or advance the mode.
    assert "capability_proof_gate_mode" not in _rc


@pytest.mark.asyncio
async def test_auto_graduation_already_at_enforce_is_noop(db):
    """When the gate is already at enforce, the check returns OK without advancing."""
    await _seed_auto_graduation_deployment_state(db)
    _set_auto_graduation_config(mode="enforce")
    ctx = SweepContext(
        session=db, now=_utcnow(), mode="observe", max_projects=25,
        sample_limit=200, window_days=14,
    )
    results = await mis.check_capability_proof_gate_auto_graduation(ctx)
    assert len(results) == 1
    assert results[0].status == CheckStatus.OK
    assert "enforce" in results[0].detail
    # Still at enforce.
    assert _rc.get("capability_proof_gate_mode") == "enforce"


@pytest.mark.asyncio
async def test_auto_graduation_skipped_when_no_projects(db):
    """When there are no projects, the check is SKIPPED."""
    await _seed_auto_graduation_deployment_state(db)
    _set_auto_graduation_config(mode="off")
    ctx = SweepContext(
        session=db, now=_utcnow(), mode="observe", max_projects=25,
        sample_limit=200, window_days=14,
    )
    results = await mis.check_capability_proof_gate_auto_graduation(ctx)
    assert len(results) == 1
    assert results[0].status == CheckStatus.SKIPPED


@pytest.mark.asyncio
async def test_auto_graduation_off_to_observe(db):
    """When coverage crosses the observe threshold, gate advances from off → observe.

    Creates one project with 100% proven coverage. With observe_threshold=0.5
    and current mode=off, the check should advance to observe and return FLAGGED.
    """
    from datetime import timedelta

    project = await _project(db)
    now = _utcnow()
    fresh = (now - timedelta(days=1)).isoformat()

    project.problem_model = {
        "capabilities": [
            {
                "name": "repo-map",
                "status": "working",
                "evidence_refs": [_proven_ref(fresh)],
            },
        ]
    }
    _set_machine_proof_records(project, "repo-map", proved_at=fresh)
    flag_modified(project, "problem_model")
    db.add(project)
    await db.commit()

    await _seed_auto_graduation_deployment_state(db)
    _set_auto_graduation_config(mode="off", observe_threshold=0.5)

    ctx = SweepContext(
        session=db, now=now, mode="observe", max_projects=25,
        sample_limit=200, window_days=14,
    )
    results = await mis.check_capability_proof_gate_auto_graduation(ctx)

    assert len(results) == 1
    r = results[0]
    assert r.status == CheckStatus.FLAGGED
    assert "off" in r.detail
    assert "observe" in r.detail
    # In-memory config must be updated.
    assert _rc.get("capability_proof_gate_mode") == "observe"


@pytest.mark.asyncio
async def test_auto_graduation_observe_to_enforce(db):
    """When coverage crosses the enforce threshold, gate advances from observe → enforce.

    All working capabilities proven → coverage = 1.0. With enforce_threshold=0.95
    and current mode=observe, the check should advance to enforce.
    """
    from datetime import timedelta

    project = await _project(db)
    now = _utcnow()
    fresh = (now - timedelta(days=1)).isoformat()

    project.problem_model = {
        "capabilities": [
            {
                "name": "repo-map",
                "status": "working",
                "evidence_refs": [_proven_ref(fresh)],
            },
            {
                "name": "ci-integration",
                "status": "working",
                "evidence_refs": [_proven_ref(fresh)],
            },
        ]
    }
    _set_machine_proof_records(
        project, "repo-map", "ci-integration", proved_at=fresh
    )
    flag_modified(project, "problem_model")
    db.add(project)
    await db.commit()

    await _seed_auto_graduation_deployment_state(db)
    _set_auto_graduation_config(mode="observe", enforce_threshold=0.95)

    ctx = SweepContext(
        session=db, now=now, mode="observe", max_projects=25,
        sample_limit=200, window_days=14,
    )
    results = await mis.check_capability_proof_gate_auto_graduation(ctx)

    assert len(results) == 1
    r = results[0]
    assert r.status == CheckStatus.FLAGGED
    assert "observe" in r.detail
    assert "enforce" in r.detail
    assert _rc.get("capability_proof_gate_mode") == "enforce"


@pytest.mark.asyncio
async def test_auto_graduation_no_advancement_below_threshold(db):
    """When coverage is below the threshold, no advancement occurs.

    Two working caps, one proven → coverage = 0.5. With observe_threshold=0.6
    (mode=off), the check returns OK without advancing.
    """
    from datetime import timedelta

    project = await _project(db)
    now = _utcnow()
    fresh = (now - timedelta(days=1)).isoformat()

    project.problem_model = {
        "capabilities": [
            {
                "name": "repo-map",
                "status": "working",
                "evidence_refs": [_proven_ref(fresh)],  # proven
            },
            {
                "name": "ci-integration",
                "status": "working",
                "evidence_refs": [],  # unproven
            },
        ]
    }
    flag_modified(project, "problem_model")
    db.add(project)
    await db.commit()

    await _seed_auto_graduation_deployment_state(db)
    _set_auto_graduation_config(mode="off", observe_threshold=0.6)

    ctx = SweepContext(
        session=db, now=now, mode="observe", max_projects=25,
        sample_limit=200, window_days=14,
    )
    results = await mis.check_capability_proof_gate_auto_graduation(ctx)

    assert len(results) == 1
    r = results[0]
    assert r.status == CheckStatus.OK
    assert "below threshold" in r.detail
    # Gate must NOT advance.
    assert _rc.get("capability_proof_gate_mode") == "off"


@pytest.mark.asyncio
async def test_self_declared_proof_empty_ledger_cannot_advance_global_mode(db):
    """A problem-model proof claim cannot inflate ledger-backed fleet coverage."""
    project = await _project(db)
    now = _utcnow()
    project.problem_model = {
        "capabilities": [
            {
                "name": "repo-map",
                "status": "working",
                "evidence_refs": [_proven_ref(now.isoformat())],
            }
        ]
    }
    project.capability_proof_records = {}
    flag_modified(project, "problem_model")
    flag_modified(project, "capability_proof_records")
    db.add(project)
    await db.commit()

    coverage = await mis.compute_fleet_proof_coverage(db, max_projects=25, now=now)
    assert coverage["total_non_exempt_working"] == 1
    assert coverage["total_proven"] == 0
    assert coverage["coverage_ratio"] == 0.0

    await _seed_auto_graduation_deployment_state(db)
    _set_auto_graduation_config(mode="off", observe_threshold=0.5)
    ctx = SweepContext(
        session=db,
        now=now,
        mode="observe",
        max_projects=25,
        sample_limit=200,
        window_days=14,
    )
    results = await mis.check_capability_proof_gate_auto_graduation(ctx)
    assert results[0].status == CheckStatus.OK
    assert _rc["capability_proof_gate_mode"] == "off"


@pytest.mark.asyncio
async def test_auto_graduation_posts_operator_notification(db):
    """On graduation, post_capability_proof_gate_graduated_message is called
    for each contributing project so the operator learns about the gate change
    through the standard notification surface (not just an INFO log).
    """
    from datetime import timedelta
    from unittest.mock import AsyncMock, patch

    project = await _project(db)
    now = _utcnow()
    fresh = (now - timedelta(days=1)).isoformat()

    project.problem_model = {
        "capabilities": [
            {
                "name": "repo-map",
                "status": "working",
                "evidence_refs": [_proven_ref(fresh)],
            },
        ]
    }
    _set_machine_proof_records(project, "repo-map", proved_at=fresh)
    flag_modified(project, "problem_model")
    db.add(project)
    await db.commit()

    await _seed_auto_graduation_deployment_state(db)
    _set_auto_graduation_config(mode="off", observe_threshold=0.5)

    ctx = SweepContext(
        session=db, now=now, mode="observe", max_projects=25,
        sample_limit=200, window_days=14,
    )

    mock_notify = AsyncMock(return_value=None)
    with patch(
        "app.notification_dispatch.post_capability_proof_gate_graduated_message",
        new=mock_notify,
    ):
        results = await mis.check_capability_proof_gate_auto_graduation(ctx)

    # The graduation must have happened.
    assert any(r.status == CheckStatus.FLAGGED for r in results)

    # The operator notification must have been dispatched.
    mock_notify.assert_awaited_once()
    call_kwargs = mock_notify.call_args.kwargs
    assert call_kwargs["from_mode"] == "off"
    assert call_kwargs["to_mode"] == "observe"
    assert call_kwargs["coverage"] == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_auto_graduation_notification_sent_for_each_contributing_project(db):
    """Each contributing project receives a separate operator notification.

    Two projects, each with proven working capabilities → two notification calls.
    """
    from datetime import timedelta
    from unittest.mock import AsyncMock, patch

    now = _utcnow()
    fresh = (now - timedelta(days=1)).isoformat()
    cap = {"name": "repo-map", "status": "working", "evidence_refs": [_proven_ref(fresh)]}

    for _ in range(2):
        p = await _project(db)
        p.problem_model = {"capabilities": [cap]}
        _set_machine_proof_records(p, "repo-map", proved_at=fresh)
        flag_modified(p, "problem_model")
        db.add(p)
    await db.commit()

    await _seed_auto_graduation_deployment_state(db)
    _set_auto_graduation_config(mode="off", observe_threshold=0.5)

    ctx = SweepContext(
        session=db, now=now, mode="observe", max_projects=25,
        sample_limit=200, window_days=14,
    )

    mock_notify = AsyncMock(return_value=None)
    with patch(
        "app.notification_dispatch.post_capability_proof_gate_graduated_message",
        new=mock_notify,
    ):
        await mis.check_capability_proof_gate_auto_graduation(ctx)

    assert mock_notify.await_count == 2


@pytest.mark.asyncio
async def test_auto_graduation_notification_skipped_when_no_advancement(db):
    """When coverage is below the threshold and no advancement occurs, no
    notification is posted — the notification is graduation-gated.
    """
    from datetime import timedelta
    from unittest.mock import AsyncMock, patch

    project = await _project(db)
    now = _utcnow()
    fresh = (now - timedelta(days=1)).isoformat()

    project.problem_model = {
        "capabilities": [
            {"name": "repo-map", "status": "working", "evidence_refs": [_proven_ref(fresh)]},
            {"name": "ci-integration", "status": "working", "evidence_refs": []},
        ]
    }
    flag_modified(project, "problem_model")
    db.add(project)
    await db.commit()

    # Coverage = 0.5, threshold = 0.8 → below threshold, no advancement.
    await _seed_auto_graduation_deployment_state(db)
    _set_auto_graduation_config(mode="off", observe_threshold=0.8)

    ctx = SweepContext(
        session=db, now=now, mode="observe", max_projects=25,
        sample_limit=200, window_days=14,
    )

    mock_notify = AsyncMock(return_value=None)
    with patch(
        "app.notification_dispatch.post_capability_proof_gate_graduated_message",
        new=mock_notify,
    ):
        results = await mis.check_capability_proof_gate_auto_graduation(ctx)

    assert all(r.status == CheckStatus.OK for r in results)
    mock_notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_auto_graduation_refuses_incomplete_fleet_coverage(db):
    """Auto-graduation to enforce is blocked when the fleet exceeds the sample cap.

    If there are more projects than max_projects, the coverage calculation only
    sees the first N projects. Projects beyond the cap may have unproven
    capabilities, so graduating the global gate to enforce based on incomplete
    data could block those projects' tasks without warning. The check must
    return SKIPPED when fleet_capped is True and the next step would be enforce.

    Verifies that even 100% coverage on the visible slice is not enough to
    graduate when unseen projects exist.
    """
    from datetime import timedelta

    now = _utcnow()
    fresh = (now - timedelta(days=1)).isoformat()
    cap = {"name": "repo-map", "status": "working", "evidence_refs": [_proven_ref(fresh)]}

    # Create 3 projects with 100% proven coverage — all within the sample cap of 2.
    for _ in range(3):
        p = await _project(db)
        p.problem_model = {"capabilities": [cap]}
        _set_machine_proof_records(p, "repo-map", proved_at=fresh)
        flag_modified(p, "problem_model")
        db.add(p)
    await db.commit()

    # Confirm fleet_capped is detected: 3 projects, cap=2.
    coverage = await mis.compute_fleet_proof_coverage(db, max_projects=2, now=now)
    assert coverage["fleet_total"] == 3
    assert coverage["num_projects"] == 2
    assert coverage["fleet_capped"] is True
    # The visible 2/2 are proven → 100% coverage on the sample.
    assert coverage["coverage_ratio"] == pytest.approx(1.0)

    # The graduation check must REFUSE to advance to enforce due to incomplete fleet.
    await _seed_auto_graduation_deployment_state(db)
    _set_auto_graduation_config(mode="observe", enforce_threshold=0.95)
    ctx = SweepContext(
        session=db,
        now=now,
        mode="observe",
        max_projects=2,  # cap below fleet size
        sample_limit=200,
        window_days=14,
    )
    results = await mis.check_capability_proof_gate_auto_graduation(ctx)
    assert len(results) == 1
    r = results[0]
    assert r.status == CheckStatus.SKIPPED, (
        f"expected SKIPPED (incomplete fleet), got {r.status}: {r.detail}"
    )
    assert "enforce" in r.detail
    assert "cap" in r.detail or "max_projects" in r.detail or "fleet" in r.detail
    # Gate must NOT have advanced.
    assert _rc.get("capability_proof_gate_mode") == "observe"


@pytest.mark.asyncio
async def test_auto_graduation_off_to_observe_on_capped_fleet(db):
    """Auto-graduation off->observe is blocked when the fleet exceeds the sample cap.

    Even if 100% of the visible slice is proven, the off->observe advance must
    return SKIPPED when fleet_capped is True so that unseen projects aren't
    silently subjected to observe-mode logging on incomplete fleet data.
    """
    from datetime import timedelta

    now = _utcnow()
    fresh = (now - timedelta(days=1)).isoformat()
    cap = {"name": "repo-map", "status": "working", "evidence_refs": [_proven_ref(fresh)]}

    # Create 3 projects with 100% proven coverage, but cap=2 so fleet_capped=True.
    for _ in range(3):
        p = await _project(db)
        p.problem_model = {"capabilities": [cap]}
        _set_machine_proof_records(p, "repo-map", proved_at=fresh)
        flag_modified(p, "problem_model")
        db.add(p)
    await db.commit()

    # Confirm fleet_capped is detected with a cap of 2.
    coverage = await mis.compute_fleet_proof_coverage(db, max_projects=2, now=now)
    assert coverage["fleet_total"] == 3
    assert coverage["fleet_capped"] is True
    assert coverage["coverage_ratio"] == pytest.approx(1.0)

    await _seed_auto_graduation_deployment_state(db)
    _set_auto_graduation_config(mode="off", observe_threshold=0.5)
    ctx = SweepContext(
        session=db,
        now=now,
        mode="observe",
        max_projects=2,  # cap below fleet size
        sample_limit=200,
        window_days=14,
    )
    results = await mis.check_capability_proof_gate_auto_graduation(ctx)
    assert len(results) == 1
    r = results[0]
    assert r.status == CheckStatus.SKIPPED, (
        f"expected SKIPPED (incomplete fleet), got {r.status}: {r.detail}"
    )
    assert "observe" in r.detail
    assert "cap" in r.detail or "max_projects" in r.detail or "fleet" in r.detail
    # Gate must NOT have advanced from off.
    assert _rc.get("capability_proof_gate_mode") == "off"


@pytest.mark.asyncio
async def test_fleet_coverage_summary_exposes_fleet_capped(db):
    """The coverage summary returned by compute_fleet_proof_coverage includes
    fleet_capped / fleet_total so the operator-facing settings endpoint can
    surface whether the coverage ratio was computed on a truncated sample.

    This is the named-test acceptance evidence for the fix: surface
    fleet_capped to operator capability-proof coverage PR: the settings
    endpoint forwards these fields from the sweep function to the operator
    surface, and the auto-graduation guard already reads them to skip the
    enforce step.
    """
    from datetime import timedelta

    now = _utcnow()
    fresh = (now - timedelta(days=1)).isoformat()
    cap = {"name": "repo-map", "status": "working", "evidence_refs": [_proven_ref(fresh)]}

    # === Uncapped - fewer projects than max_projects ===
    p = await _project(db)
    p.problem_model = {"capabilities": [cap]}
    _set_machine_proof_records(p, "repo-map", proved_at=fresh)
    flag_modified(p, "problem_model")
    await db.commit()

    coverage = await mis.compute_fleet_proof_coverage(db, max_projects=10, now=now)
    assert coverage["fleet_total"] == 1
    assert coverage["num_projects"] == 1
    assert coverage["fleet_capped"] is False, (
        "should not be capped when fleet (1) is below sample cap (10)"
    )

    # === Capped - more projects than max_projects ===
    for _ in range(4):
        p2 = await _project(db)
        p2.problem_model = {"capabilities": [cap]}
        _set_machine_proof_records(p2, "repo-map", proved_at=fresh)
        flag_modified(p2, "problem_model")
    await db.commit()

    coverage = await mis.compute_fleet_proof_coverage(db, max_projects=3, now=now)
    assert coverage["fleet_total"] == 5  # 1 from above + 4 new
    assert coverage["num_projects"] == 3  # capped at max_projects
    assert coverage["fleet_capped"] is True, (
        "fleet_total (5) > max_projects (3) so the sample is truncated"
    )
    # coverage_ratio is still valid within the sample
    assert coverage["coverage_ratio"] == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_auto_graduation_stale_proof_is_not_counted_by_working_status(db):
    """Regression: a proof older than PROOF_FRESHNESS_TTL demotes from 'proven'
    when working_capability_proof_status is called with now=.

    This pins the TTL contract that the auto-graduation path MUST honour:
    compute_fleet_proof_coverage must pass now= to working_capability_proof_status
    so stale proofs do not inflate graduation coverage. The underlying function
    correctly applies TTL when now= is supplied — this test documents that behaviour
    at the function level (the fix in compute_fleet_proof_coverage threads now= through).
    """
    from datetime import datetime, timedelta

    from app.evidence_primitive import PROOF_FRESHNESS_TTL
    from app.capability_registry import working_capability_proof_status

    now = datetime(2026, 7, 14, 0, 0, 0)
    stale_ts = (now - PROOF_FRESHNESS_TTL - timedelta(days=1)).isoformat()
    fresh_ts = (now - timedelta(days=1)).isoformat()

    stale_model = {
        "capabilities": [
            {
                "name": "repo-map",
                "status": "working",
                "evidence_refs": [
                    {"kind": "named_test", "ref": "t", "proven": True, "recorded_at": stale_ts}
                ],
            }
        ]
    }
    fresh_model = {
        "capabilities": [
            {
                "name": "repo-map",
                "status": "working",
                "evidence_refs": [
                    {"kind": "named_test", "ref": "t", "proven": True, "recorded_at": fresh_ts}
                ],
            }
        ]
    }

    # Stale proof: when now= is passed, TTL is applied and the proof is demoted.
    status_stale = working_capability_proof_status(stale_model, now=now)
    assert status_stale.get("repo-map") != "proven", (
        "A proof older than PROOF_FRESHNESS_TTL must not count as 'proven' "
        "when now= is supplied — stale proofs must not inflate graduation coverage."
    )

    # Fresh proof: same capability with a recent timestamp remains proven.
    status_fresh = working_capability_proof_status(fresh_model, now=now)
    assert status_fresh.get("repo-map") == "proven", (
        "A fresh proof must still count as 'proven'."
    )

    # Default path: without an explicit now=, working_capability_proof_status
    # defaults now to _utcnow() (capability_registry.py:585-588), so PROOF_FRESHNESS_TTL
    # is STILL applied and a proof older than the TTL is demoted. There is no "legacy
    # gap" that preserves stale proofs as 'proven' when now= is omitted.
    status_no_now = working_capability_proof_status(stale_model)
    assert status_no_now.get("repo-map") != "proven", (
        "Without an explicit now=, working_capability_proof_status still defaults "
        "now to _utcnow() and applies PROOF_FRESHNESS_TTL, so a proof older than the "
        "TTL must not count as 'proven'."
    )


@pytest.mark.asyncio
async def test_auto_graduation_fleet_coverage_stale_proof_counts_without_now(db):
    """A proof older than PROOF_FRESHNESS_TTL is NOT counted by compute_fleet_proof_coverage.

    compute_fleet_proof_coverage snapshots ``now`` once and threads it explicitly into
    working_capability_proof_status, which applies PROOF_FRESHNESS_TTL. A stale proof is
    therefore demoted and excluded from ``total_proven`` (the graduation numerator) while
    still counting toward ``total_non_exempt_working`` (the denominator). This pins that
    stale proofs cannot silently inflate graduation coverage.
    """
    from datetime import timedelta

    from app.evidence_primitive import PROOF_FRESHNESS_TTL
    from app.metrics_integrity_sweep import compute_fleet_proof_coverage

    project = await _project(db)

    now = _utcnow()
    stale_ts = (now - PROOF_FRESHNESS_TTL - timedelta(days=1)).isoformat()

    project.problem_model = {
        "capabilities": [
            {
                "name": "repo-map",
                "status": "working",
                "evidence_refs": [
                    {"kind": "named_test", "ref": "t", "proven": True, "recorded_at": stale_ts}
                ],
            }
        ]
    }
    _set_machine_proof_records(project, "repo-map", proved_at=stale_ts)
    flag_modified(project, "problem_model")
    db.add(project)
    await db.commit()

    # compute_fleet_proof_coverage snapshots now (defaulting to _utcnow()) and threads
    # it into working_capability_proof_status, so PROOF_FRESHNESS_TTL IS applied.
    # The stale proof is demoted and excluded from the numerator.
    coverage = await compute_fleet_proof_coverage(db, max_projects=25)
    # The stale working capability still counts toward the denominator.
    assert coverage["total_non_exempt_working"] == 1
    # But the stale proof is demoted by the TTL, so it does NOT count as proven.
    assert coverage["total_proven"] == 0, (
        "compute_fleet_proof_coverage must exclude proofs older than "
        "PROOF_FRESHNESS_TTL from total_proven — a stale proof cannot inflate "
        "graduation coverage."
    )
    # Coverage ratio reflects the demotion: 0 proven / 1 working.
    assert coverage["coverage_ratio"] == 0.0

    # Passing an explicit now= before the proof went stale counts it as proven,
    # proving the demotion is driven by the freshness cutoff and not something else.
    fresh_cutoff = now - PROOF_FRESHNESS_TTL + timedelta(days=1)
    coverage_fresh = await compute_fleet_proof_coverage(
        db, max_projects=25, now=fresh_cutoff
    )
    assert coverage_fresh["total_proven"] == 1


@pytest.mark.asyncio
async def test_compute_fleet_proof_coverage_trial_eligible_unproven(db):
    """``trial_eligible_unproven`` counts unproven working caps with backing_paths.

    This is the projection for finding 1: it answers "how many unproven caps
    could earn a trial if capability_proof_execution_trials_enabled were ON?"
    A cap is trial-eligible when it is (a) non-exempt, (b) not yet proven, and
    (c) declares at least one backing_path. Proven caps and caps with no
    backing_paths are excluded from the count.
    """
    from sqlalchemy.orm.attributes import flag_modified
    from app.metrics_integrity_sweep import compute_fleet_proof_coverage

    project = await _project(db)
    project.problem_model = {
        "capabilities": [
            {
                "name": "Cap Proven",
                "status": "working",
                "evidence_refs": [
                    {"kind": "named_test", "ref": "test_x.py::test_x", "proven": True}
                ],
            },
            {
                "name": "Cap Unproven With Backing",
                "status": "working",
                "evidence_refs": [],
                "backing_paths": ["backend/app/some_module.py"],
            },
            {
                "name": "Cap Unproven No Backing",
                "status": "working",
                "evidence_refs": [],
            },
            {
                "name": "Cap Planned",
                "status": "planned",
                "evidence_refs": [],
                "backing_paths": ["backend/app/planned.py"],
            },
        ]
    }
    _set_machine_proof_records(project, "Cap Proven")
    flag_modified(project, "problem_model")
    db.add(project)
    await db.commit()

    coverage = await compute_fleet_proof_coverage(db, max_projects=25)
    # 3 working (non-planned) caps, 1 proven → 1 trial-eligible unproven with backing_paths.
    assert coverage["total_non_exempt_working"] == 3
    assert coverage["total_proven"] == 1
    assert coverage["trial_eligible_unproven"] == 1, (
        "only the unproven working cap with backing_paths is trial-eligible"
    )


@pytest.mark.asyncio
async def test_auto_graduation_advances_by_one_step_per_run(db):
    """Graduation advances by at most one step per check invocation.

    With mode=off and 100% proven coverage (>= both thresholds), the first run
    advances only to 'observe' (the off→observe condition fires first; the
    observe→enforce elif is not evaluated in the same run). The second run then
    advances observe→enforce.
    """
    from datetime import timedelta

    project = await _project(db)
    now = _utcnow()
    fresh = (now - timedelta(days=1)).isoformat()

    project.problem_model = {
        "capabilities": [
            {"name": "repo-map", "status": "working", "evidence_refs": [_proven_ref(fresh)]},
        ]
    }
    _set_machine_proof_records(project, "repo-map", proved_at=fresh)
    flag_modified(project, "problem_model")
    db.add(project)
    await db.commit()

    # Both thresholds at 0.5 so 100% coverage triggers both — verifies the
    # code uses if/elif (one step) not two separate ifs (two steps).
    await _seed_auto_graduation_deployment_state(db)
    _set_auto_graduation_config(mode="off", observe_threshold=0.5, enforce_threshold=0.5)

    ctx = SweepContext(
        session=db, now=now, mode="observe", max_projects=25,
        sample_limit=200, window_days=14,
    )

    # First run: off → observe (not off → enforce in one jump).
    results1 = await mis.check_capability_proof_gate_auto_graduation(ctx)
    assert any(r.status == CheckStatus.FLAGGED for r in results1)
    assert _rc.get("capability_proof_gate_mode") == "observe"
    assert "off" in results1[0].detail
    assert "observe" in results1[0].detail

    # Second run: observe → enforce (one more step).
    results2 = await mis.check_capability_proof_gate_auto_graduation(ctx)
    assert any(r.status == CheckStatus.FLAGGED for r in results2)
    assert _rc.get("capability_proof_gate_mode") == "enforce"
    assert "observe" in results2[0].detail
    assert "enforce" in results2[0].detail


@pytest.mark.asyncio
async def test_auto_graduation_db_failure_leaves_in_memory_mode_unchanged(db):
    """When the DB commit fails, in-memory mode must not be advanced.

    The check should abort with an ERROR result and leave _rc unchanged so the
    running process does not enforce a mode that was never durably committed.
    """
    from datetime import timedelta

    from app.models import DeploymentState

    project = await _project(db)
    now = _utcnow()
    fresh = (now - timedelta(days=1)).isoformat()

    project.problem_model = {
        "capabilities": [
            {
                "name": "repo-map",
                "status": "working",
                "evidence_refs": [_proven_ref(fresh)],
            },
        ]
    }
    _set_machine_proof_records(project, "repo-map", proved_at=fresh)
    flag_modified(project, "problem_model")
    db.add(project)
    # Create a DeploymentState row so the graduation code has a row to update.
    dep_state = DeploymentState()
    db.add(dep_state)
    await db.commit()

    await _seed_auto_graduation_deployment_state(db)
    _set_auto_graduation_config(mode="off", observe_threshold=0.5)

    # Use a proxy that delegates all ops to the real session but raises on the
    # first commit call, avoiding AsyncMock/AsyncSession interaction quirks.
    class _FailOnFirstCommit:
        _commits = 0

        def __init__(self, real):
            object.__setattr__(self, "_real", real)
            object.__setattr__(self, "_commits", 0)

        def __getattr__(self, name):
            return getattr(object.__getattribute__(self, "_real"), name)

        async def commit(self):
            c = object.__getattribute__(self, "_commits")
            object.__setattr__(self, "_commits", c + 1)
            if c == 0:
                raise RuntimeError("simulated DB failure")
            return await object.__getattribute__(self, "_real").commit()

    fail_db = _FailOnFirstCommit(db)
    ctx = SweepContext(
        session=fail_db, now=now, mode="observe", max_projects=25,
        sample_limit=200, window_days=14,
    )

    results = await mis.check_capability_proof_gate_auto_graduation(ctx)

    assert len(results) == 1
    r = results[0]
    # Must return ERROR, not FLAGGED, so the tick shows the failed advancement.
    assert r.status == CheckStatus.ERROR
    assert "off" in r.detail or "persist" in r.detail
    # Critical invariant: in-memory mode must remain unchanged.
    assert _rc.get("capability_proof_gate_mode") == "off"


@pytest.mark.asyncio
async def test_auto_graduation_missing_state_leaves_in_memory_mode_unchanged(db):
    """A missing durable singleton must abort graduation without changing memory."""
    from datetime import timedelta

    project = await _project(db)
    now = _utcnow()
    fresh = (now - timedelta(days=1)).isoformat()
    project.problem_model = {
        "capabilities": [
            {
                "name": "repo-map",
                "status": "working",
                "evidence_refs": [_proven_ref(fresh)],
            },
        ]
    }
    _set_machine_proof_records(project, "repo-map", proved_at=fresh)
    flag_modified(project, "problem_model")
    db.add(project)
    await db.commit()

    _set_auto_graduation_config(mode="off", observe_threshold=0.5)
    ctx = SweepContext(
        session=db, now=now, mode="observe", max_projects=25,
        sample_limit=200, window_days=14,
    )

    results = await mis.check_capability_proof_gate_auto_graduation(ctx)

    assert len(results) == 1
    assert results[0].status == CheckStatus.ERROR
    assert "deployment state is missing" in results[0].detail
    assert _rc.get("capability_proof_gate_mode") == "off"


@pytest.mark.asyncio
async def test_auto_graduation_notification_failure_does_not_rollback_mode(db):
    """When notification posting fails, the already-committed mode must persist.

    The gate advancement (DB + in-memory) is already durable; a notification
    error must not undo the mode or report failure to the tick.
    """
    from datetime import timedelta
    from unittest.mock import patch

    project = await _project(db)
    now = _utcnow()
    fresh = (now - timedelta(days=1)).isoformat()

    project.problem_model = {
        "capabilities": [
            {
                "name": "repo-map",
                "status": "working",
                "evidence_refs": [_proven_ref(fresh)],
            },
        ]
    }
    _set_machine_proof_records(project, "repo-map", proved_at=fresh)
    flag_modified(project, "problem_model")
    db.add(project)
    await db.commit()

    await _seed_auto_graduation_deployment_state(db)
    _set_auto_graduation_config(mode="off", observe_threshold=0.5)

    ctx = SweepContext(
        session=db, now=now, mode="observe", max_projects=25,
        sample_limit=200, window_days=14,
    )

    with patch(
        "app.notification_dispatch.post_capability_proof_gate_graduated_message",
        side_effect=RuntimeError("notification service down"),
    ):
        results = await mis.check_capability_proof_gate_auto_graduation(ctx)

    assert len(results) == 1
    r = results[0]
    # Advancement still reported as FLAGGED (info).
    assert r.status == CheckStatus.FLAGGED
    assert "observe" in r.detail
    # In-memory mode must still be advanced despite notification failure.
    assert _rc.get("capability_proof_gate_mode") == "observe"


# --- observe_mode_unverified_resolution ---------------------------------------


@pytest.mark.asyncio
async def test_observe_mode_unverified_resolution_flags_stamped_tasks(db):
    """Tasks carrying SPAWN_CAPABILITY_PROOF_UNVERIFIED_KEY are surfaced as FLAGGED."""
    from app.research_completion import SPAWN_CAPABILITY_PROOF_UNVERIFIED_KEY

    project = await _project(db)
    now = _utcnow()

    stamps = [
        {"kind": "capability", "ref": "test-runner", "proof_status": "unverified"},
        {"kind": "capability", "ref": "test-runner", "proof_status": "unverified"},
    ]
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        status=TaskStatus.COMPLETED,
        context={SPAWN_CAPABILITY_PROOF_UNVERIFIED_KEY: stamps},
    )
    db.add(t)
    await db.commit()

    ctx = SweepContext(
        session=db, now=now, mode="observe", max_projects=25,
        sample_limit=200, window_days=14,
    )
    results = await mis.check_observe_mode_unverified_resolution(ctx)

    flagged = [r for r in results if r.status == CheckStatus.FLAGGED]
    assert len(flagged) == 1
    assert flagged[0].subject == str(project.id)
    assert flagged[0].delta == 1
    assert flagged[0].self_reported == 1
    assert flagged[0].ground_truth == 0
    assert "test-runner" in flagged[0].detail


@pytest.mark.asyncio
async def test_observe_mode_unverified_resolution_skipped_when_no_stamps(db):
    """No stamped tasks → all projects SKIPPED and a global OK summary."""
    project = await _project(db)
    now = _utcnow()

    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        status=TaskStatus.COMPLETED,
        context={"some_other_key": "value"},
    )
    db.add(t)
    await db.commit()

    ctx = SweepContext(
        session=db, now=now, mode="observe", max_projects=25,
        sample_limit=200, window_days=14,
    )
    results = await mis.check_observe_mode_unverified_resolution(ctx)

    assert not any(r.status == CheckStatus.FLAGGED for r in results)
    ok_or_skipped = [r for r in results if r.status in (CheckStatus.OK, CheckStatus.SKIPPED)]
    assert ok_or_skipped


@pytest.mark.asyncio
async def test_observe_mode_unverified_resolution_aggregates_multiple_tasks(db):
    """Multiple stamped tasks in the same project increment delta correctly."""
    from app.research_completion import SPAWN_CAPABILITY_PROOF_UNVERIFIED_KEY

    project = await _project(db)
    now = _utcnow()

    for ref in ("cap-a", "cap-b", "cap-a"):
        t = Task(
            project_id=project.id,
            payloads=[{"type": "create"}],
            status=TaskStatus.COMPLETED,
            context={
                SPAWN_CAPABILITY_PROOF_UNVERIFIED_KEY: [
                    {"kind": "capability", "ref": ref, "proof_status": "unverified"}
                ]
            },
        )
        db.add(t)
    await db.commit()

    ctx = SweepContext(
        session=db, now=now, mode="observe", max_projects=25,
        sample_limit=200, window_days=14,
    )
    results = await mis.check_observe_mode_unverified_resolution(ctx)

    flagged = [r for r in results if r.status == CheckStatus.FLAGGED]
    assert len(flagged) == 1
    assert flagged[0].delta == 3
    assert "cap-a" in flagged[0].detail


# --- capability_proof_trial_stall ---------------------------------------------


def _make_trial_task(project_id, cap, *, ran=False, passed=False, could_not_run=False):
    """Helper: a COMPLETED capability_proof_trial task with a contradiction stamp."""
    contradiction = {
        "cap": cap,
        "head_sha": "abc123",
        "test_refs": [],
        "ran": ran,
        "passed": passed,
        "could_not_run": could_not_run,
        "proof_revoked": False,
        "recorded_at": _utcnow().isoformat(),
    }
    return Task(
        project_id=project_id,
        payloads=[{"type": "capability_proof_trial", "cap": cap}],
        status=TaskStatus.COMPLETED,
        context={"capability_proof_trial_contradiction": contradiction},
    )


@pytest.mark.asyncio
async def test_capability_proof_trial_stall_flags_all_could_not_run(db):
    """All trials could_not_run → FLAGGED with severity=warn."""
    project = await _project(db)
    now = _utcnow()

    for _ in range(2):
        db.add(_make_trial_task(project.id, "cap-a", could_not_run=True))
    await db.commit()

    ctx = SweepContext(
        session=db, now=now, mode="observe", max_projects=25,
        sample_limit=200, window_days=14,
    )
    results = await mis.check_capability_proof_trial_stall(ctx)

    flagged = [r for r in results if r.status == CheckStatus.FLAGGED]
    assert len(flagged) == 1
    f = flagged[0]
    assert f.subject == str(project.id)
    assert f.severity == "warn"
    assert f.delta == 2
    assert f.ground_truth == 0
    assert "could_not_run=2" in f.detail
    assert "cap-a" in f.detail


@pytest.mark.asyncio
async def test_capability_proof_trial_stall_flags_all_red(db):
    """All trials ran + failed (red) → FLAGGED with severity=error."""
    project = await _project(db)
    now = _utcnow()

    db.add(_make_trial_task(project.id, "cap-b", ran=True, passed=False))
    await db.commit()

    ctx = SweepContext(
        session=db, now=now, mode="observe", max_projects=25,
        sample_limit=200, window_days=14,
    )
    results = await mis.check_capability_proof_trial_stall(ctx)

    flagged = [r for r in results if r.status == CheckStatus.FLAGGED]
    assert len(flagged) == 1
    f = flagged[0]
    assert f.severity == "error"
    assert f.delta == 1
    assert "red=1" in f.detail
    assert "cap-b" in f.detail


@pytest.mark.asyncio
async def test_capability_proof_trial_stall_ok_when_any_green(db):
    """A green trial (no contradiction stamp) for a cap → that cap is NOT flagged."""
    project = await _project(db)
    now = _utcnow()

    # Green trial: completed but NO contradiction stamp.
    green_task = Task(
        project_id=project.id,
        payloads=[{"type": "capability_proof_trial", "cap": "cap-c"}],
        status=TaskStatus.COMPLETED,
        context={},
    )
    db.add(green_task)
    await db.commit()

    ctx = SweepContext(
        session=db, now=now, mode="observe", max_projects=25,
        sample_limit=200, window_days=14,
    )
    results = await mis.check_capability_proof_trial_stall(ctx)

    assert not any(r.status == CheckStatus.FLAGGED for r in results)
    ok_or_skipped = [
        r for r in results if r.status in (CheckStatus.OK, CheckStatus.SKIPPED)
    ]
    assert ok_or_skipped


@pytest.mark.asyncio
async def test_capability_proof_trial_stall_ok_when_mixed_green_and_red(db):
    """Green trial + red trial for the same cap → cap is NOT flagged (has a passing outcome)."""
    project = await _project(db)
    now = _utcnow()

    # Red (non-green) trial.
    db.add(_make_trial_task(project.id, "cap-d", ran=True, passed=False))
    # Green trial for the same cap — no contradiction stamp.
    db.add(Task(
        project_id=project.id,
        payloads=[{"type": "capability_proof_trial", "cap": "cap-d"}],
        status=TaskStatus.COMPLETED,
        context={},
    ))
    await db.commit()

    ctx = SweepContext(
        session=db, now=now, mode="observe", max_projects=25,
        sample_limit=200, window_days=14,
    )
    results = await mis.check_capability_proof_trial_stall(ctx)

    flagged = [r for r in results if r.status == CheckStatus.FLAGGED]
    assert not flagged, (
        f"cap-d should not be flagged — it has a green trial; got: {flagged}"
    )


@pytest.mark.asyncio
async def test_capability_proof_trial_stall_ok_when_mixed_green_and_could_not_run(db):
    """Green trial + could_not_run trial for the same cap → cap is NOT flagged."""
    project = await _project(db)
    now = _utcnow()

    # could_not_run (non-green) trial.
    db.add(_make_trial_task(project.id, "cap-e", could_not_run=True))
    # Green trial for the same cap — no contradiction stamp.
    db.add(Task(
        project_id=project.id,
        payloads=[{"type": "capability_proof_trial", "cap": "cap-e"}],
        status=TaskStatus.COMPLETED,
        context={},
    ))
    await db.commit()

    ctx = SweepContext(
        session=db, now=now, mode="observe", max_projects=25,
        sample_limit=200, window_days=14,
    )
    results = await mis.check_capability_proof_trial_stall(ctx)

    flagged = [r for r in results if r.status == CheckStatus.FLAGGED]
    assert not flagged, (
        f"cap-e should not be flagged — it has a green trial; got: {flagged}"
    )


@pytest.mark.asyncio
async def test_capability_proof_trial_stall_skips_empty(db):
    """No trial tasks at all → SKIPPED / global OK (no FLAGGED)."""
    project = await _project(db)
    now = _utcnow()
    db.add(Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        status=TaskStatus.COMPLETED,
        context={},
    ))
    await db.commit()

    ctx = SweepContext(
        session=db, now=now, mode="observe", max_projects=25,
        sample_limit=200, window_days=14,
    )
    results = await mis.check_capability_proof_trial_stall(ctx)

    assert not any(r.status == CheckStatus.FLAGGED for r in results)
    skipped = [r for r in results if r.status == CheckStatus.SKIPPED]
    assert skipped


@pytest.mark.asyncio
async def test_capability_proof_trial_stall_mixed_caps(db):
    """One cap all-red, another all-could_not_run → two FLAGGED results."""
    project = await _project(db)
    now = _utcnow()

    db.add(_make_trial_task(project.id, "cap-alpha", ran=True, passed=False))
    db.add(_make_trial_task(project.id, "cap-beta", could_not_run=True))
    await db.commit()

    ctx = SweepContext(
        session=db, now=now, mode="observe", max_projects=25,
        sample_limit=200, window_days=14,
    )
    results = await mis.check_capability_proof_trial_stall(ctx)

    flagged = [r for r in results if r.status == CheckStatus.FLAGGED]
    assert len(flagged) == 2
    severities = {f.detail.split("cap=")[1].split("'")[1]: f.severity for f in flagged}
    assert severities["cap-alpha"] == "error"
    assert severities["cap-beta"] == "warn"


# --- check_unmapped_acceptance_criteria -------------------------------------


@pytest.mark.asyncio
async def test_unmapped_acceptance_criteria_flags_criterion_with_no_direction(db):
    """An active criterion not linked to any Direction is FLAGGED."""
    project = await _project(db)
    version, criterion = await _open_version_with_criterion(
        db, project, text="Unmapped criterion"
    )
    # No Direction row → criterion is unmapped.

    ctx = SweepContext(
        session=db, now=_utcnow(), mode="observe",
        max_projects=25, sample_limit=200, window_days=14,
    )
    results = await mis.check_unmapped_acceptance_criteria(ctx)
    flagged = [r for r in results if r.status == CheckStatus.FLAGGED]
    assert len(flagged) == 1, f"expected 1 flagged, got {results}"
    f = flagged[0]
    assert f.check == "unmapped_acceptance_criteria"
    assert f.self_reported == 1
    assert f.ground_truth == 0
    assert f.delta == 1
    assert criterion.criterion_key in f.detail


@pytest.mark.asyncio
async def test_unmapped_acceptance_criteria_ok_when_criterion_mapped(db):
    """A criterion linked to a Direction is NOT flagged."""
    project = await _project(db)
    version, criterion = await _open_version_with_criterion(
        db, project, text="Mapped criterion"
    )
    # Create a Direction that lists this criterion_key.
    direction = Direction(
        project_id=project.id,
        direction_key="d-test-mapped",
        status="active",
        criterion_keys=[criterion.criterion_key],
    )
    db.add(direction)
    await db.commit()

    ctx = SweepContext(
        session=db, now=_utcnow(), mode="observe",
        max_projects=25, sample_limit=200, window_days=14,
    )
    results = await mis.check_unmapped_acceptance_criteria(ctx)
    assert all(not r.flagged for r in results), f"unexpected flagged: {results}"
    ok = [r for r in results if r.status == CheckStatus.OK]
    assert len(ok) >= 1
    assert ok[0].delta == 0


@pytest.mark.asyncio
async def test_unmapped_acceptance_criteria_skips_no_open_version(db):
    """Project with no open version → SKIPPED for that project."""
    project = await _project(db)
    ctx = SweepContext(
        session=db, now=_utcnow(), mode="observe",
        max_projects=25, sample_limit=200, window_days=14,
    )
    results = await mis.check_unmapped_acceptance_criteria(ctx)
    mine = [r for r in results if r.subject == str(project.id)]
    assert len(mine) == 1
    assert mine[0].status == CheckStatus.SKIPPED
    assert "no open version" in mine[0].detail


@pytest.mark.asyncio
async def test_unmapped_acceptance_criteria_skips_no_active_criteria(db):
    """Open version with no active criteria → SKIPPED."""
    project = await _project(db)
    version = ReleaseVersion(project_id=project.id, version_number=1, status="open")
    db.add(version)
    await db.commit()
    ctx = SweepContext(
        session=db, now=_utcnow(), mode="observe",
        max_projects=25, sample_limit=200, window_days=14,
    )
    results = await mis.check_unmapped_acceptance_criteria(ctx)
    mine = [r for r in results if r.subject == str(project.id)]
    assert len(mine) == 1
    assert mine[0].status == CheckStatus.SKIPPED
    assert "no active criteria" in mine[0].detail


# --- check_research_quality_grader_pass_rate --------------------------------


@pytest.mark.asyncio
async def test_research_quality_grader_pass_rate_flags_all_failed(db):
    """Direction whose recent cycles all have quality_grader_verdict passed=False is FLAGGED."""
    project = await _project(db)
    project.directions = [
        {
            "id": "d-failing-qg",
            "status": "active",
            "text": "Test direction",
            "research_history": [
                {"task_id": "t1", "quality_grader_verdict": {"passed": False, "reason": "bad"}},
                {"task_id": "t2", "quality_grader_verdict": {"passed": False, "reason": "bad"}},
            ],
        }
    ]
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(project, "directions")
    db.add(project)
    await db.commit()

    ctx = SweepContext(
        session=db, now=_utcnow(), mode="observe",
        max_projects=25, sample_limit=200, window_days=14,
    )
    results = await mis.check_research_quality_grader_pass_rate(ctx)
    flagged = [r for r in results if r.status == CheckStatus.FLAGGED]
    assert len(flagged) == 1, f"expected 1 flagged, got {results}"
    f = flagged[0]
    assert f.check == "research_quality_grader_pass_rate"
    assert f.self_reported == 2
    assert f.ground_truth == 0
    assert f.delta == 2


@pytest.mark.asyncio
async def test_research_quality_grader_pass_rate_ok_when_passing(db):
    """Direction with at least one passing quality_grader_verdict is NOT flagged."""
    project = await _project(db)
    project.directions = [
        {
            "id": "d-passing-qg",
            "status": "active",
            "research_history": [
                {"task_id": "t1", "quality_grader_verdict": {"passed": False, "reason": "bad"}},
                {"task_id": "t2", "quality_grader_verdict": {"passed": True, "reason": "ok"}},
            ],
        }
    ]
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(project, "directions")
    db.add(project)
    await db.commit()

    ctx = SweepContext(
        session=db, now=_utcnow(), mode="observe",
        max_projects=25, sample_limit=200, window_days=14,
    )
    results = await mis.check_research_quality_grader_pass_rate(ctx)
    assert all(not r.flagged for r in results), f"unexpected flagged: {results}"
    ok = [r for r in results if r.status == CheckStatus.OK]
    assert len(ok) >= 1


@pytest.mark.asyncio
async def test_research_quality_grader_pass_rate_skips_legacy_entries(db):
    """Directions with no quality_grader_verdict in history are skipped (not flagged)."""
    project = await _project(db)
    project.directions = [
        {
            "id": "d-legacy",
            "status": "active",
            "research_history": [
                {"task_id": "t1", "tasks_created": 3},
                {"task_id": "t2", "tasks_created": 2},
            ],
        }
    ]
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(project, "directions")
    db.add(project)
    await db.commit()

    ctx = SweepContext(
        session=db, now=_utcnow(), mode="observe",
        max_projects=25, sample_limit=200, window_days=14,
    )
    results = await mis.check_research_quality_grader_pass_rate(ctx)
    assert all(not r.flagged for r in results), f"unexpected flagged: {results}"
    ok = [r for r in results if r.status == CheckStatus.OK]
    assert len(ok) >= 1
    assert "no directions" in ok[0].detail


# --- Check: encrypted_field_plaintext_detector --------------------------------


@pytest.mark.asyncio
async def test_encrypted_field_flags_plaintext_when_encryption_active(db):
    """When encryption is active and a column has plaintext values, FLAGGED.
    Uses MaxPlanAccount.oauth_token as the test subject.
    """
    from app.models import MaxPlanAccount

    with (
        patch("app.field_encryption.is_encryption_active", return_value=True),
        patch(
            "app.field_encryption.decrypt_field", side_effect=lambda v: v
        ),  # identity → plaintext signal
    ):
        account = MaxPlanAccount(
            user_id="u-enc-test", name="test-acc", oauth_token="plaintext-token"
        )
        db.add(account)
        await db.commit()

        ctx = SweepContext(
            session=db,
            now=_utcnow(),
            mode="observe",
            max_projects=25,
            sample_limit=200,
            window_days=14,
        )
        results = await mis.check_encrypted_field_plaintext_detector(ctx)

    oauth_results = [
        r for r in results if r.subject == "maxplanaccount.oauth_token"
    ]
    assert len(oauth_results) == 1, f"expected 1 oauth_token result, got {results}"
    f = oauth_results[0]
    assert f.status == CheckStatus.FLAGGED, f"expected FLAGGED, got {f.status}"
    assert f.delta == 1, f"expected delta=1, got {f.delta}"
    assert f.self_reported == 1
    assert f.ground_truth == 0


@pytest.mark.asyncio
async def test_encrypted_field_ok_when_values_are_ciphertext(db):
    """When encryption is active and values decrypt to something different,
    they are ciphertext → OK."""
    from app.models import MaxPlanAccount

    with (
        patch("app.field_encryption.is_encryption_active", return_value=True),
        patch(
            "app.field_encryption.decrypt_field",
            side_effect=lambda v: v + "_decrypted",
        ),  # decrypt changes value → encrypted
    ):
        account = MaxPlanAccount(
            user_id="u-enc-ok", name="test-ok", oauth_token="ciphertext-value"
        )
        db.add(account)
        await db.commit()

        ctx = SweepContext(
            session=db,
            now=_utcnow(),
            mode="observe",
            max_projects=25,
            sample_limit=200,
            window_days=14,
        )
        results = await mis.check_encrypted_field_plaintext_detector(ctx)

    oauth_results = [
        r for r in results if r.subject == "maxplanaccount.oauth_token"
    ]
    assert len(oauth_results) == 1
    r = oauth_results[0]
    assert r.status == CheckStatus.OK, f"expected OK, got {r.status}"
    assert r.delta == 0
    assert r.self_reported == 1
    assert r.ground_truth == 1


@pytest.mark.asyncio
async def test_encrypted_field_plaintext_detector_skips_when_encryption_inactive(
    db,
):
    """When encryption is not active, the check returns a single global SKIPPED."""
    with patch(
        "app.field_encryption.is_encryption_active", return_value=False
    ):
        ctx = SweepContext(
            session=db,
            now=_utcnow(),
            mode="observe",
            max_projects=25,
            sample_limit=200,
            window_days=14,
        )
        results = await mis.check_encrypted_field_plaintext_detector(ctx)

    assert len(results) == 1
    assert results[0].status == CheckStatus.SKIPPED
    assert results[0].subject == "global"
    assert "encryption not active" in results[0].detail


@pytest.mark.asyncio
async def test_plaintext_in_encrypted_column_is_flagged(db):
    """Acceptance-named test: a plaintext secret in an encrypted column is
    flagged when encryption is active.

    Uses MaxPlanAccount.claude_config_json with a real-like plaintext value.
    mocks decrypt_field to return its input unchanged (simulating the real
    InvalidToken fallback when decrypting plaintext).
    """
    from app.models import MaxPlanAccount

    with (
        patch("app.field_encryption.is_encryption_active", return_value=True),
        patch("app.field_encryption.decrypt_field", side_effect=lambda v: v),
    ):
        account = MaxPlanAccount(
            user_id="u-plain-ci",
            name="plain-ci",
            claude_config_json='{"key": "sk-ant-this-is-plaintext"}',
        )
        db.add(account)
        await db.commit()

        ctx = SweepContext(
            session=db,
            now=_utcnow(),
            mode="observe",
            max_projects=25,
            sample_limit=200,
            window_days=14,
        )
        results = await mis.check_encrypted_field_plaintext_detector(ctx)

    # The check should flag at least one column (claude_config_json has plaintext)
    flagged = [r for r in results if r.flagged]
    assert len(flagged) >= 1, f"expected at least one FLAGGED, got {results}"
    # At least one FLAGGED result should identify the claude_config_json column
    claude_flagged = [r for r in flagged if "claude_config_json" in r.subject]
    assert len(claude_flagged) == 1
    cf = claude_flagged[0]
    assert cf.delta >= 1  # at least one row in that column is plaintext
    assert "plaintext" in cf.detail


# --- Check: audience_routing_attestation ----------------------------------


def _message(
    db,
    project,
    *,
    event_type: str,
    audience: str | None = None,
    message_type: MessageType = MessageType.NOTE,
) -> ProjectMessage:
    """Insert a ProjectMessage with the given event_type and audience in context."""
    ctx: dict = {"event_type": event_type}
    if audience is not None:
        ctx["audience"] = audience
    msg = ProjectMessage(
        project_id=project.id,
        context=ctx,
        message_type=message_type,
    )
    db.add(msg)
    return msg


@pytest.mark.asyncio
async def test_audience_routing_attestation_ok_for_operator_facing_message_type(db):
    """A strategist-authored BLOCKER/BUG_REPORT/QUESTION whose event_type resolves
    to strategist-internal but is stamped operator-action-needed (via the
    message_type upgrade in route_event) must NOT be flagged: the ground-truth
    recompute must pass message_type so it mirrors the stamping logic."""
    project = await _project(db)
    from app.message_audience import route_event

    # strategic_message alone -> strategist-internal, but the operator-facing
    # message_type upgrades the audience to operator-action-needed.
    for mt in (MessageType.BLOCKER, MessageType.BUG_REPORT, MessageType.QUESTION):
        canonical = route_event("strategic_message", message_type=mt.value)
        assert canonical == "operator-action-needed"
        _message(db, project, event_type="strategic_message",
                 audience=canonical, message_type=mt)
    await db.commit()

    ctx = SweepContext(session=db, now=_utcnow(), mode="observe",
                       max_projects=25, sample_limit=200, window_days=14)
    results = await mis.check_audience_routing_attestation(ctx)

    pid = str(project.id)
    project_results = [r for r in results if r.subject == pid]
    assert len(project_results) == 1, f"expected 1 result, got {results}"
    r = project_results[0]
    assert r.status == CheckStatus.OK, f"expected OK, got {r.status}: {r.detail}"
    assert r.self_reported == 3
    assert r.ground_truth == 3
    assert r.delta == 0


@pytest.mark.asyncio
async def test_audience_routing_attestation_ok_when_stamps_match(db):
    """Messages whose stamped audience matches route_event() produce OK."""
    project = await _project(db)
    from app.message_audience import route_event

    for et in ("pr_merged", "auto_fix_escalated", "failure_alert"):
        canonical = route_event(et)
        _message(db, project, event_type=et, audience=canonical)
    await db.commit()

    ctx = SweepContext(session=db, now=_utcnow(), mode="observe",
                       max_projects=25, sample_limit=200, window_days=14)
    results = await mis.check_audience_routing_attestation(ctx)

    pid = str(project.id)
    ok_results = [r for r in results if r.subject == pid]
    assert len(ok_results) == 1, f"expected 1 result for project, got {results}"
    r = ok_results[0]
    assert r.status == CheckStatus.OK, f"expected OK, got {r.status}"
    assert r.self_reported == 3
    assert r.ground_truth == 3
    assert r.delta == 0


@pytest.mark.asyncio
async def test_audience_routing_attestation_flags_mismatched_stamp(db):
    """Messages with a stamped audience != route_event() produce FLAGGED."""
    project = await _project(db)
    _message(db, project, event_type="pr_merged", audience="operator-action-needed")
    _message(db, project, event_type="auto_fix_escalated", audience="strategist-internal")
    await db.commit()

    ctx = SweepContext(session=db, now=_utcnow(), mode="observe",
                       max_projects=25, sample_limit=200, window_days=14)
    results = await mis.check_audience_routing_attestation(ctx)

    pid = str(project.id)
    flagged = [r for r in results if r.flagged and r.subject == pid]
    assert len(flagged) == 1, f"expected 1 FLAGGED for project, got {results}"
    f = flagged[0]
    assert f.self_reported == 0  # neither message is correctly stamped
    assert f.ground_truth == 2   # both had a known event_type
    assert f.delta == 2


@pytest.mark.asyncio
async def test_audience_routing_attestation_flags_null_audience(db):
    """Messages with a NULL audience (legacy pass-through) are FLAGGED."""
    project = await _project(db)
    # pr_merged -> strategist-internal, but NULL audience means the read-side
    # filter retains it as operator-visible — a latent leak.
    _message(db, project, event_type="pr_merged", audience=None)
    await db.commit()

    ctx = SweepContext(session=db, now=_utcnow(), mode="observe",
                       max_projects=25, sample_limit=200, window_days=14)
    results = await mis.check_audience_routing_attestation(ctx)

    pid = str(project.id)
    flagged = [r for r in results if r.flagged and r.subject == pid]
    assert len(flagged) == 1, f"expected 1 FLAGGED for project, got {results}"
    f = flagged[0]
    assert f.self_reported == 0  # NULL != canonical audience
    assert f.ground_truth == 1
    assert f.delta == 1


@pytest.mark.asyncio
async def test_audience_routing_attestation_skipped_when_no_messages(db):
    """When no ProjectMessage rows exist, the check returns SKIPPED."""
    ctx = SweepContext(session=db, now=_utcnow(), mode="observe",
                       max_projects=25, sample_limit=200, window_days=14)
    results = await mis.check_audience_routing_attestation(ctx)

    assert len(results) == 1
    assert results[0].status == CheckStatus.SKIPPED
    assert results[0].subject == "global"


@pytest.mark.asyncio
async def test_audience_routing_attestation_skipped_when_no_event_types(db):
    """Messages exist but none carry an event_type => SKIPPED."""
    project = await _project(db)
    _message(db, project, event_type="", audience="strategist-internal")
    _message(db, project, event_type="pr_merged", audience="strategist-internal")
    # The second message has an event_type, so it IS counted. Rewrite without
    # an event_type key at all for a proper skip.
    await db.commit()

    # Overwrite context to remove event_type entirely.
    from app.models import ProjectMessage as PM
    rows = (await db.execute(select(PM))).scalars().all()
    for r in rows:
        r.context = {"audience": "strategist-internal"}
    flag_modified(rows[-1], "context")
    await db.commit()

    ctx = SweepContext(session=db, now=_utcnow(), mode="observe",
                       max_projects=25, sample_limit=200, window_days=14)
    results = await mis.check_audience_routing_attestation(ctx)

    assert len(results) == 1
    assert results[0].status == CheckStatus.SKIPPED
    assert "no ProjectMessage rows with a known event_type" in results[0].detail


@pytest.mark.asyncio
async def test_audience_routing_attestation_bounded_sample(db):
    """More messages than sample_limit => only sample_limit are processed."""
    project = await _project(db)
    from app.message_audience import route_event

    # Insert more than sample_limit messages with known event_type+audience.
    limit = 200
    over = limit + 10
    for i in range(over):
        _message(db, project, event_type="pr_merged", audience=route_event("pr_merged"))
    await db.commit()

    ctx = SweepContext(session=db, now=_utcnow(), mode="observe",
                       max_projects=25, sample_limit=limit, window_days=14)
    results = await mis.check_audience_routing_attestation(ctx)

    pid = str(project.id)
    ok_results = [r for r in results if r.subject == pid]
    assert len(ok_results) == 1
    r = ok_results[0]
    assert r.status == CheckStatus.OK
    assert r.ground_truth == limit  # capped by sample_limit, not total
    assert r.self_reported == limit


@pytest.mark.asyncio
async def test_audience_routing_attestation_enforce_enrolls_finding(db):
    """In enforce mode, a mismatched audience enrolls an EvergreenBacklogItem."""
    _rc["metrics_integrity_sweep_mode"] = "enforce"
    project = await _project(db)
    _message(db, project, event_type="pr_merged", audience="operator-fyi")
    await db.commit()

    now = _utcnow()
    tick = await run_metrics_integrity_sweep(db, now=now)

    assert tick is not None
    # Find the audience_routing_attestation flag in the tick flags.
    attestations = [f for f in tick["flags"] if f["check"] == "audience_routing_attestation"]
    assert len(attestations) == 1, f"expected 1 flag, got {tick['flags']}"
    assert tick["enrolled"] >= 1

    rows = (
        await db.execute(
            select(EvergreenBacklogItem).where(
                EvergreenBacklogItem.project_id == project.id
            )
        )
    ).scalars().all()
    assert any(
        "audience_routing_attestation" in r.finding_key for r in rows
    )


# --- Check: operator_inbox_needs_action_liveness ---------------------------


def _inbox_message(
    db,
    project,
    *,
    message_type: MessageType = MessageType.NOTE,
    audience: str | None = "operator-fyi",
    status: str = "open",
    from_type: str = "agent",
) -> ProjectMessage:
    """Insert an open operator-visible ProjectMessage for inbox liveness tests."""
    ctx: dict = {}
    if audience is not None:
        ctx["audience"] = audience
    msg = ProjectMessage(
        project_id=project.id,
        context=ctx,
        message_type=message_type,
        status=status,
        from_type=from_type,
    )
    db.add(msg)
    return msg


@pytest.mark.asyncio
async def test_operator_inbox_needs_action_liveness_ok_when_all_actionable(db):
    """Open operator-visible QUESTION and BLOCKER messages are all needs-action → OK."""
    project = await _project(db)
    _inbox_message(db, project, message_type=MessageType.QUESTION, audience="operator-fyi")
    _inbox_message(db, project, message_type=MessageType.BLOCKER, audience="operator-action-needed")
    await db.commit()

    ctx = SweepContext(session=db, now=_utcnow(), mode="observe",
                       max_projects=25, sample_limit=200, window_days=14)
    results = await mis.check_operator_inbox_needs_action_liveness(ctx)

    pid = str(project.id)
    project_results = [r for r in results if r.subject == pid]
    assert len(project_results) == 1
    r = project_results[0]
    assert r.status == CheckStatus.OK
    assert r.self_reported == 2
    assert r.ground_truth == 2
    assert r.delta == 0


@pytest.mark.asyncio
async def test_operator_inbox_needs_action_liveness_flags_nonactionable_notice(db):
    """An open NULL-audience NOTE (non-actionable, non-fyi) triggers FLAGGED with delta=1."""
    project = await _project(db)
    # NOTE with no audience (NULL) is operator-visible (legacy fallthrough) but not
    # needs-action and not operator-fyi — should be flagged as a forward leak.
    _inbox_message(db, project, message_type=MessageType.NOTE, audience=None)
    await db.commit()

    ctx = SweepContext(session=db, now=_utcnow(), mode="observe",
                       max_projects=25, sample_limit=200, window_days=14)
    results = await mis.check_operator_inbox_needs_action_liveness(ctx)

    pid = str(project.id)
    flagged = [r for r in results if r.flagged and r.subject == pid]
    assert len(flagged) == 1, f"expected FLAGGED, got {results}"
    f = flagged[0]
    assert f.self_reported == 1
    assert f.ground_truth == 0
    assert f.delta == 1
    assert "non-actionable" in f.detail or "not genuinely needs-action" in f.detail


@pytest.mark.asyncio
async def test_operator_inbox_needs_action_liveness_skipped_when_no_messages(db):
    """No open operator-visible messages → SKIPPED per project."""
    await _project(db)
    await db.commit()

    ctx = SweepContext(session=db, now=_utcnow(), mode="observe",
                       max_projects=25, sample_limit=200, window_days=14)
    results = await mis.check_operator_inbox_needs_action_liveness(ctx)

    assert any(r.status == CheckStatus.SKIPPED for r in results)


@pytest.mark.asyncio
async def test_operator_inbox_needs_action_liveness_ignores_resolved(db):
    """Resolved messages are excluded and do not cause a flag."""
    project = await _project(db)
    # Resolved NOTE — should not count as open
    _inbox_message(db, project, message_type=MessageType.NOTE, audience="operator-fyi",
                   status="resolved")
    await db.commit()

    ctx = SweepContext(session=db, now=_utcnow(), mode="observe",
                       max_projects=25, sample_limit=200, window_days=14)
    results = await mis.check_operator_inbox_needs_action_liveness(ctx)

    # No open messages → SKIPPED (resolved messages are not open)
    assert all(r.status == CheckStatus.SKIPPED for r in results)


@pytest.mark.asyncio
async def test_operator_inbox_needs_action_liveness_ignores_user_messages(db):
    """User-authored messages are excluded from the operator inbox check."""
    project = await _project(db)
    # User-authored message should not be counted (from_type == "user")
    _inbox_message(db, project, message_type=MessageType.NOTE, audience="operator-fyi",
                   from_type="user")
    await db.commit()

    ctx = SweepContext(session=db, now=_utcnow(), mode="observe",
                       max_projects=25, sample_limit=200, window_days=14)
    results = await mis.check_operator_inbox_needs_action_liveness(ctx)

    # User messages filtered out → SKIPPED
    assert all(r.status == CheckStatus.SKIPPED for r in results)


@pytest.mark.asyncio
async def test_operator_inbox_needs_action_liveness_enforce_enrolls_finding(db):
    """In enforce mode, a non-actionable NULL-audience open message enrolls an EvergreenBacklogItem."""
    _rc["metrics_integrity_sweep_mode"] = "enforce"
    project = await _project(db)
    # NULL-audience NOTE: operator-visible (legacy fallthrough) but not needs-action
    # and not operator-fyi → forward leak → FLAGGED → enrolled in enforce mode.
    _inbox_message(db, project, message_type=MessageType.NOTE, audience=None)
    await db.commit()

    now = _utcnow()
    tick = await run_metrics_integrity_sweep(db, now=now)

    assert tick is not None
    inbox_flags = [f for f in tick["flags"] if f["check"] == "operator_inbox_needs_action_liveness"]
    assert len(inbox_flags) >= 1, f"expected inbox liveness flag, got {tick['flags']}"
    assert tick["enrolled"] >= 1

    rows = (
        await db.execute(
            select(EvergreenBacklogItem).where(
                EvergreenBacklogItem.project_id == project.id
            )
        )
    ).scalars().all()
    assert any(
        "operator_inbox_needs_action_liveness" in r.finding_key for r in rows
    )


def test_registry_contains_operator_inbox_check():
    """operator_inbox_needs_action_liveness is registered in the check registry."""
    names = {c.name for c in registered_checks()}
    assert "operator_inbox_needs_action_liveness" in names


@pytest.mark.asyncio
async def test_operator_inbox_liveness_covers_alerts_and_bidirectional_drift(db):
    """Verify the check exercises both drift directions and the alert channel.

    Forward drift: operator-fyi NOTEs are legitimate informational notices and
    are NOT counted as leaked chatter. Only non-fyi messages that fail
    derive_needs_action() are forward leaks.

    Reverse drift: a linked BLOCKER (needs-action=True per derive_needs_action,
    status='linked') is now included in the sample and flagged as badge-invisible
    — the badge SQL reads status='open' only and would miss it.

    Alert coverage: an undismissed INFO-severity, operator-visible Alert is
    flagged as non-actionable alert noise, demonstrating the check covers the
    alert channel in addition to the message ledger.
    """
    project = await _project(db)

    # Linked BLOCKER: needs-action=True (derive_needs_action), but status='linked'
    # → badge SQL (status='open') misses it → reverse drift.
    _inbox_message(
        db, project,
        message_type=MessageType.BLOCKER,
        audience="operator-action-needed",
        status="linked",
    )

    # operator-fyi NOTE: legitimate informational notice — NOT a forward leak.
    _inbox_message(
        db, project,
        message_type=MessageType.NOTE,
        audience="operator-fyi",
    )

    # INFO-severity alert with NULL event_type: operator-visible (fails open on NULL)
    # but non-actionable → alert channel noise detected by the check.
    db.add(Alert(
        project_id=project.id,
        severity=AlertSeverity.INFO,
        title="Build completed",
        message="Image build finished successfully",
        event_type=None,
    ))
    await db.commit()

    ctx = SweepContext(
        session=db, now=_utcnow(), mode="observe",
        max_projects=25, sample_limit=200, window_days=14,
    )
    results = await mis.check_operator_inbox_needs_action_liveness(ctx)

    pid = str(project.id)
    project_results = [r for r in results if r.subject == pid]
    assert len(project_results) == 1
    r = project_results[0]

    # FLAGGED because of reverse drift (1 badge-invisible linked BLOCKER)
    # and alert channel noise (1 INFO alert).
    # The operator-fyi NOTE is NOT counted as a forward leak.
    assert r.status == CheckStatus.FLAGGED, (
        f"expected FLAGGED, got {r.status}: {r.detail}"
    )
    # delta = 0 forward_leaked + 1 badge_invisible + 1 info_alert = 2
    assert r.delta == 2, (
        f"expected delta=2 (1 badge-invisible + 1 INFO alert), got {r.delta}: {r.detail}"
    )
    # Reverse drift detail mentions badge-invisible / linked status
    assert "linked" in r.detail or "badge" in r.detail or "invisible" in r.detail, (
        f"expected reverse-drift mention in detail: {r.detail}"
    )
    # Alert channel detail mentions INFO alerts
    assert "INFO" in r.detail or "alert" in r.detail.lower(), (
        f"expected alert-channel mention in detail: {r.detail}"
    )


# --- Standalone operator-inbox liveness (always-on, sweep-mode-independent) ---


@pytest.fixture(autouse=False)
def _clean_liveness():
    """Reset the standalone liveness module's process-local state between tests."""
    from app.operator_inbox_liveness import _reset_liveness_for_tests
    _reset_liveness_for_tests()
    yield
    _reset_liveness_for_tests()


@pytest.mark.asyncio
async def test_operator_inbox_leak_signal_counts_null_audience_noise(db, _clean_liveness):
    """Standalone liveness check counts NULL-audience non-actionable messages as
    forward-leak noise and fires even when metrics_integrity_sweep_mode is 'off'.

    This is the acceptance test for the always-on operator-inbox liveness signal:
    a NULL-audience open NOTE (operator-visible by legacy fail-open, but not
    needs-action and not operator-fyi) must appear in ``forward_leaked`` regardless
    of the sweep mode, proving the signal is independent of the cuttable sweep.
    """
    from app.operator_inbox_liveness import run_operator_inbox_liveness_check

    # Disable the metrics-integrity sweep entirely — the standalone signal must
    # still fire and count the noise.
    _rc["metrics_integrity_sweep_mode"] = "off"

    project = await _project(db)
    # NULL-audience NOTE: legacy fail-open makes it operator-visible, but it is
    # not needs-action (not BLOCKER/QUESTION) and not operator-fyi.  It should be
    # counted as forward-leaked noise by the standalone liveness signal.
    msg = ProjectMessage(
        project_id=project.id,
        context={},  # NULL audience (no "audience" key)
        message_type=MessageType.NOTE,
        status="open",
        from_type="agent",
    )
    db.add(msg)
    await db.commit()

    results = await run_operator_inbox_liveness_check(db, _utcnow(), _force=True)

    assert results is not None, "expected results (not gated out)"
    pid = str(project.id)
    project_results = [r for r in results if r.project_id == pid]
    assert len(project_results) == 1, f"expected one result for project, got {project_results}"
    r = project_results[0]

    assert r.status == "flagged", (
        f"expected 'flagged' for NULL-audience NOTE, got '{r.status}': {r.detail}"
    )
    assert r.forward_leaked == 1, (
        f"expected forward_leaked=1, got {r.forward_leaked}: {r.detail}"
    )
    assert r.badge_invisible == 0
    assert r.info_alert_noise == 0
    assert "forward leak" in r.detail or "not genuinely needs-action" in r.detail, (
        f"expected forward-leak mention in detail: {r.detail}"
    )
