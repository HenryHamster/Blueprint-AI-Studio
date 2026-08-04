"""Tests for holistic goal grading and the bounded legacy drain.

Current qualifications obtain goal/category grading from the single holistic
``release_audit`` task. The separate ``release_merge_grade`` spawn helper is
retained only to drain or retry legacy attempts that already own that child.
These tests cover:

  * ``evaluate_merge_grade`` pure-function semantics — pass / fail /
    not_applicable / unparseable — the four status values the gate produces.
  * ``_spawn_release_merge_grade_task`` — legacy-drain payload and blindness.
  * New attempts do not create a legacy merge-grade child when no grade applies.
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


@pytest.fixture(autouse=True)
def _authorized_qualification_has_a_delivery_target(monkeypatch):
    monkeypatch.setattr(rq, "resolve_project_promotion_enabled", lambda _id: True)


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
    # Patch the SHA map so the pre-spawn migration-identity check (which uses
    # list_version_shas_at_ref) sees identical lists on both refs and returns
    # "ok" — mirrors the test_release_qualifier autouse fixture.
    mocker.patch(
        "app.github.list_version_shas_at_ref",
        new=AsyncMock(
            return_value={"0001_init.py": "sha1", "0002_feature.py": "sha2"}
        ),
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
            payloads=[{"type": "release_merge_grade", "base_branch": "main", "categories": []}],
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
        # Empty rubric snapshot so _resolve_launched_rubric succeeds in tests
        # that don't seed a merge_grade task payload with categories.
        "rubric_snapshot": [],
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
async def test_run_qualification_skips_merge_grade_when_goal_empty(db_session, mocker):
    """An empty goal causes run_qualification to skip the merge_grade child."""
    project, version = await _make_project_and_version(db_session, phase="stabilize")
    # Leave version.goal as None.

    candidate = "dead" * 10
    mocker.patch("app.github.get_branch_sha", new=AsyncMock(return_value=candidate))

    result = await rq.run_qualification(
        db_session, project.id, version, explicit_request=True
    )
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
