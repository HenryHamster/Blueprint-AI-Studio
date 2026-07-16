"""Tests for the task-spawned blind merge-grade qualification gate.

The inline blind_merge_grade module was removed (#11716); goal-coverage grading
is now performed by a spawned ``release_merge_grade`` agent task.  These tests
cover the current implementation:

  * ``evaluate_merge_grade`` pure-function semantics — pass / fail /
    not_applicable / unparseable — the four status values the gate produces.
  * ``_spawn_release_merge_grade_task`` — payload shape and blindness contract.
  * ``merge_grade_task_id`` recording in ``run_qualification`` — spawned when
    goal is usable, skipped when goal is empty/contentless.
  * ``_finalize_qualification`` gate decisions — block on fail, hold on missing
    artifact, pass on covering grade.
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app import release_qualifier as rq
from app import release_scoring as rs
from app import release_version as rv
from app.models import (
    Artifact,
    ArtifactType,
    Project,
    Task,
    TaskStatus,
)


# ---------------------------------------------------------------------------
# Autouse fixture: default GitHub to green CI (mirrors test_release_qualifier.py)
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _default_green_ci(mocker):
    """Patch GitHub/migration helpers to green/ok defaults.

    Mirrors the autouse fixture in test_release_qualifier.py so that
    CI resolution, migration gates, and GitHub credential checks are all
    incidental (green) by default — tests that exercise those dimensions
    re-patch in their own body.
    """
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(
            return_value={
                "status": "completed",
                "conclusion": "success",
                "details": [],
                "failed_logs": "",
            }
        ),
    )
    mocker.patch(
        "app.github.list_pull_requests_for_commit",
        new=AsyncMock(return_value=[]),
    )
    mocker.patch(
        "app.release_qualifier._is_target_absorbed",
        new=AsyncMock(return_value=True),
    )
    mocker.patch(
        "app.github.get_pr_files",
        new=AsyncMock(return_value=[]),
    )
    mocker.patch(
        "app.github.list_versions_at_ref",
        new=AsyncMock(return_value=["0001_init.py", "0002_feature.py"]),
    )
    mocker.patch(
        "app.release_qualifier._resolve_migration_identity_at_finalize",
        new=AsyncMock(return_value=("ok", None)),
    )
    mocker.patch(
        "app.release_qualifier.run_migration_apply_gate",
        new=AsyncMock(return_value=("ok", None)),
    )

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_GOOD_GOAL = "Deliver the blind merge-grade qualification gate end-to-end"


async def _make_project_and_version(db_session, *, phase: str = "build"):
    project = Project(
        name="BMG-Test",
        user_id="bmg-user",
        github_owner="acme",
        github_repo="widget",
        integration_branch="automain",
        promotion_target="main",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    version = await rv.open_version(db_session, project.id, version_number=1)
    if phase != "build":
        rv.set_version_phase(version, phase, reasoning="test setup")
        db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)
    return project, version


async def _seed_for_finalize(
    db_session,
    project,
    version,
    *,
    merge_grade_logs: dict | None,
    seed_merge_grade: bool = True,
    goal: str = _GOOD_GOAL,
):
    """Seed audit + smoketest + optional merge-grade children and stamp
    last_qualification so _finalize_qualification can run."""
    _candidate_sha = "feed" * 10

    audit_task = Task(
        project_id=project.id,
        title="audit",
        payloads=[{"type": "release_audit", "base_branch": "main"}],
        vm_instance_id="vm-audit",
        status=TaskStatus.COMPLETED,
        context={"candidate_sha": _candidate_sha},
    )
    smoke_task = Task(
        project_id=project.id,
        title="smoke",
        payloads=[{"type": "smoketest", "base_branch": "automain"}],
        vm_instance_id="vm-smoke",
        status=TaskStatus.COMPLETED,
    )
    db_session.add(audit_task)
    db_session.add(smoke_task)
    await db_session.flush()

    db_session.add(
        Artifact(
            task_id=audit_task.id,
            artifact_type=ArtifactType.RELEASE_AUDIT_REPORT,
            logs=json.dumps(
                {
                    "audit_score": 1.0,
                    # A clean RELEASE_AUDIT_REPORT carries an explicit empty findings
                    # list — the real agent always emits the key. A non-empty report
                    # that OMITS it is the malformed `no_findings_key` shape that
                    # _score_audit scores as UNMEASURED, which hard-blocks on
                    # `audit_missing` before the merge-grade gate is ever reached —
                    # so every test in this file would assert the wrong blocker.
                    "findings": [],
                    "test_erosion": {
                        "deleted_test_files": [],
                        "assertion_delta": 0,
                        "test_count_delta": 0,
                    },
                }
            ),
        )
    )
    db_session.add(
        Artifact(
            task_id=smoke_task.id,
            artifact_type=ArtifactType.SMOKETEST_RESULT,
            logs=json.dumps(
                {
                    "passed": True,
                    "tests": [{"name": "startup", "status": "pass"}],
                    "suites": {"startup": {"passed": True}},
                }
            ),
        )
    )

    merge_grade_task_id: str | None = None
    if seed_merge_grade:
        mg_task = Task(
            project_id=project.id,
            title="merge-grade",
            payloads=[{"type": "release_merge_grade", "base_branch": "main"}],
            vm_instance_id="vm-merge-grade",
            status=TaskStatus.COMPLETED,
            context={"candidate_sha": _candidate_sha},
        )
        db_session.add(mg_task)
        await db_session.flush()
        if merge_grade_logs is not None:
            db_session.add(
                Artifact(
                    task_id=mg_task.id,
                    artifact_type=ArtifactType.RELEASE_MERGE_GRADE,
                    logs=json.dumps(merge_grade_logs),
                )
            )
        await db_session.flush()
        await db_session.refresh(mg_task)
        merge_grade_task_id = str(mg_task.id)

    version.goal = goal
    version.last_qualification = {
        "status": "pending",
        "candidate_sha": _candidate_sha,
        "ci_green": True,
        "smoketest_enabled": True,
        "started_at": "2026-05-19T00:00:00",
        "audit_task_id": str(audit_task.id),
        "smoketest_task_id": str(smoke_task.id),
        "challenge_task_id": None,
        "merge_grade_task_id": merge_grade_task_id,
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)


# ===========================================================================
# evaluate_merge_grade — pure function
# ===========================================================================


class TestEvaluateMergeGrade:
    """Unit tests for the pure evaluate_merge_grade scoring function."""

    def test_explicit_covers_goal_true_returns_pass(self):
        result = rs.evaluate_merge_grade(
            _GOOD_GOAL,
            {"covers_goal": True, "coverage_score": 0.9, "biggest_gap": None},
        )
        assert result["status"] == "pass"
        assert result["covers_goal"] is True
        assert result["blocked"] is False

    def test_explicit_covers_goal_false_returns_fail_blocked(self):
        result = rs.evaluate_merge_grade(
            _GOOD_GOAL,
            {
                "covers_goal": False,
                "coverage_score": 0.1,
                "biggest_gap": "Gate never wired into composite.",
            },
        )
        assert result["status"] == "fail"
        assert result["covers_goal"] is False
        assert result["blocked"] is True
        assert "never wired" in (result["biggest_gap"] or "")

    def test_empty_goal_returns_not_applicable_unblocked(self):
        result = rs.evaluate_merge_grade("", {"covers_goal": False})
        assert result["status"] == "not_applicable"
        assert result["blocked"] is False

    def test_contentless_goal_returns_not_applicable(self):
        for placeholder in ("test", "n/a", "tbd", "TODO", "None"):
            result = rs.evaluate_merge_grade(placeholder, {"covers_goal": False})
            assert result["status"] == "not_applicable", (
                f"placeholder {placeholder!r} should yield not_applicable"
            )

    def test_none_artifact_returns_unparseable_blocked(self):
        result = rs.evaluate_merge_grade(_GOOD_GOAL, None)
        assert result["status"] == "unparseable"
        assert result["blocked"] is True

    def test_artifact_missing_both_covers_and_score_returns_unparseable(self):
        result = rs.evaluate_merge_grade(_GOOD_GOAL, {"biggest_gap": "missing field"})
        assert result["status"] == "unparseable"
        assert result["blocked"] is True

    def test_numeric_score_above_threshold_passes(self):
        result = rs.evaluate_merge_grade(
            _GOOD_GOAL,
            {"coverage_score": 0.85},
        )
        assert result["status"] == "pass"
        assert result["blocked"] is False

    def test_numeric_score_below_threshold_fails(self):
        result = rs.evaluate_merge_grade(
            _GOOD_GOAL,
            {"coverage_score": 0.3},
        )
        assert result["status"] == "fail"
        assert result["blocked"] is True


# ===========================================================================
# _spawn_release_merge_grade_task — payload / blindness contract
# ===========================================================================


@pytest.mark.asyncio
async def test_spawn_merge_grade_task_creates_queued_task(db_session):
    """_spawn_release_merge_grade_task creates a QUEUED release_merge_grade task."""
    from app.release_qualifier_audit import _spawn_release_merge_grade_task

    project = Project(
        name="SpawnTest",
        user_id="u1",
        github_owner="acme",
        github_repo="repo",
        integration_branch="automain",
        promotion_target="main",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    version_id = uuid.uuid4()
    candidate_sha = "abcd" * 10
    attempt_id = uuid.uuid4()
    task = await _spawn_release_merge_grade_task(
        db_session,
        project.id,
        version_id=version_id,
        base_branch="main",
        diff_range=f"main...{candidate_sha}",
        candidate_sha=candidate_sha,
        qualification_attempt_id=attempt_id,
        version_goal=_GOOD_GOAL,
        merged_pr_numbers=[101, 102],
    )
    await db_session.commit()
    await db_session.refresh(task)

    assert task.status == TaskStatus.QUEUED
    payload = task.first_payload
    assert payload["type"] == "release_merge_grade"
    assert payload["version_goal"] == _GOOD_GOAL
    assert payload["quality_tier"] == "opus"
    assert payload["candidate_sha"] == candidate_sha
    assert payload["qualification_attempt_id"] == str(attempt_id)
    assert 101 in payload["merged_prs"]
    # Blindness contract: running_update must NOT appear in any payload key.
    assert not any("running_update" in str(k).lower() for k in payload)


# ===========================================================================
# run_qualification — merge_grade_task_id spawn / skip
# ===========================================================================


@pytest.mark.asyncio
async def test_run_qualification_spawns_merge_grade_when_goal_usable(
    db_session, mocker
):
    """A usable version goal causes run_qualification to spawn a merge_grade child
    and record its id in last_qualification."""
    project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.goal = _GOOD_GOAL
    db_session.add(version)
    await db_session.commit()

    candidate = "cafe" * 10
    mocker.patch("app.github.get_branch_sha", new=AsyncMock(return_value=candidate))

    result = await rq.run_qualification(db_session, project.id, version)
    await db_session.commit()

    assert result["status"] == "spawned"
    assert result.get("merge_grade_task_id"), (
        "merge_grade_task_id must be non-None when version.goal is usable"
    )
    await db_session.refresh(version)
    assert version.last_qualification["merge_grade_task_id"] is not None

    tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    types = [(t.first_payload or {}).get("type") for t in tasks]
    assert "release_merge_grade" in types


@pytest.mark.asyncio
async def test_run_qualification_skips_merge_grade_when_goal_empty(db_session, mocker):
    """An empty goal causes run_qualification to skip the merge_grade child."""
    project, version = await _make_project_and_version(db_session, phase="stabilize")
    # Leave version.goal as None.

    candidate = "dead" * 10
    mocker.patch("app.github.get_branch_sha", new=AsyncMock(return_value=candidate))

    result = await rq.run_qualification(db_session, project.id, version)
    await db_session.commit()

    assert result["status"] == "spawned"
    assert result.get("merge_grade_task_id") is None

    tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    types = [(t.first_payload or {}).get("type") for t in tasks]
    assert "release_merge_grade" not in types


# ===========================================================================
# _finalize_qualification — gate decisions (block / hold / pass)
# ===========================================================================


@pytest.mark.asyncio
async def test_finalize_blocks_failing_merge_grade(db_session, mocker):
    """A failing merge-grade verdict blocks promotion when the gate is armed."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = True
    # The merge-grade gate honours merge_grade_block_threshold (default 0.0 = off
    # since #12457 / 74055c4f2); arm it so a failing grade actually blocks — this
    # is the test's stated premise ("when the gate is armed").
    mocker.patch.dict(
        shared_state.runtime_config, {"merge_grade_block_threshold": 1.0}
    )
    # The armed + failing path consults the already-on-main ancestry exemption
    # (finding 0); this candidate is NOT on the promotion target, so pin
    # is_ancestor_of=False to keep the block load-bearing and the test hermetic.
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=False))
    promote_mock = mocker.patch(
        "app.release_qualifier.promote",
        new=AsyncMock(return_value={"status": "promoted", "confidence": 0.99}),
    )

    project, version = await _make_project_and_version(db_session)
    await _seed_for_finalize(
        db_session,
        project,
        version,
        merge_grade_logs={
            "covers_goal": False,
            "coverage_score": 0.1,
            "biggest_gap": "The gate was never wired into compute_composite.",
            "candidate_sha": "feed" * 10,
        },
    )

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    assert result["status"] == "not_promoted"
    assert result["blocker"] == "merge_grade_misses"
    promote_mock.assert_not_awaited()
    lq = version.last_qualification
    assert lq["merge_grade_status"] == "fail"
    assert lq["blocker"] == "merge_grade_misses"


@pytest.mark.asyncio
async def test_finalize_holds_when_merge_grade_artifact_missing(db_session, mocker):
    """A merge_grade child that completed with NO artifact routes to infra_failure
    (hold) — the gate cannot be silently skipped."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = True
    promote_mock = mocker.patch("app.release_qualifier.promote", new=AsyncMock())

    project, version = await _make_project_and_version(db_session)
    # seed_merge_grade=True but merge_grade_logs=None → COMPLETED child, no artifact.
    await _seed_for_finalize(
        db_session,
        project,
        version,
        seed_merge_grade=True,
        merge_grade_logs=None,
    )

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    assert result["status"] == "infra_failure", (
        f"expected infra_failure when artifact absent; got {result['status']!r}"
    )
    promote_mock.assert_not_called()


@pytest.mark.asyncio
async def test_finalize_passes_on_covering_merge_grade(db_session, mocker):
    """A PASSING merge-grade verdict does not block: the composite reaches the
    promote threshold and the gate status is 'pass'.

    Uses promotion_enabled=False (dry-run) so _finalize_qualification writes the
    final last_qualification before returning — the mocked promote() path would
    not write it.
    """
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = False  # dry-run path writes lq
    mocker.patch("app.release_qualifier.promote", new=AsyncMock())

    project, version = await _make_project_and_version(db_session)
    await _seed_for_finalize(
        db_session,
        project,
        version,
        merge_grade_logs={
            "covers_goal": True,
            "coverage_score": 0.95,
            "biggest_gap": None,
        },
    )

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    # Dry-run: would-promote path, no hard block.
    assert result["status"] == "dry_run", (
        f"expected dry_run (would promote); got {result['status']!r}"
    )
    assert result.get("would_promote") is True

    await db_session.refresh(version)
    lq = version.last_qualification
    # Merge-grade gate must be pass — not a blocker.
    assert lq.get("merge_grade_status") == "pass"
    assert lq.get("blocker") is None


@pytest.mark.asyncio
async def test_finalize_not_applicable_when_no_merge_grade_task(db_session, mocker):
    """When merge_grade_task_id is None (empty goal at spawn time), the gate is
    a no-op PASS: finalize reaches dry_run / promote without a merge-grade block.

    Uses promotion_enabled=False (dry-run) so last_qualification is written back
    with merge_grade_status='not_applicable'.
    """
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = False  # dry-run path writes lq
    mocker.patch("app.release_qualifier.promote", new=AsyncMock())

    project, version = await _make_project_and_version(db_session)
    # seed_merge_grade=False → no merge_grade child, merge_grade_task_id=None.
    await _seed_for_finalize(
        db_session,
        project,
        version,
        seed_merge_grade=False,
        merge_grade_logs=None,
        goal="",
    )

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    assert result["status"] == "dry_run", (
        f"absent merge_grade_task_id should not block; got {result['status']!r}"
    )
    await db_session.refresh(version)
    lq = version.last_qualification
    # Gate is not_applicable when no task was spawned.
    assert lq.get("merge_grade_status") == "not_applicable"
