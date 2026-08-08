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


# ===========================================================================
# _spawn_direction_merge_grade_task + record_direction_merge_grade — the
# DIRECTION-scoped blind objective grade (campaign path, dir d-4f944896)
# ===========================================================================


async def _make_direction_project(db_session):
    project = Project(
        name="DirGradeTest",
        user_id="u1",
        github_owner="acme",
        github_repo="repo",
        integration_branch="automain",
        promotion_target="main",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    return project


async def _seed_direction_landed_tasks(db_session, project_id, direction_id):
    """Seed landed + unlanded tasks tagged with the direction."""
    for pr, title in ((301, "landed child A"), (302, "landed child B")):
        db_session.add(
            Task(
                project_id=project_id,
                title=title,
                payloads=[{"type": "create", "pr_number": pr}],
                vm_instance_id=f"vm-{pr}",
                status=TaskStatus.COMPLETED,
                context={
                    "direction_id": direction_id,
                    "pr_number": pr,
                    "landed_on_integration": True,
                },
            )
        )
    # A direction task that did NOT land must be excluded from the window.
    db_session.add(
        Task(
            project_id=project_id,
            title="unlanded child",
            payloads=[{"type": "create", "pr_number": 303}],
            vm_instance_id="vm-303",
            status=TaskStatus.COMPLETED,
            context={"direction_id": direction_id, "pr_number": 303},
        )
    )
    # A landed task NOT tagged with the direction must be excluded too.
    db_session.add(
        Task(
            project_id=project_id,
            title="other direction",
            payloads=[{"type": "create", "pr_number": 304}],
            vm_instance_id="vm-304",
            status=TaskStatus.COMPLETED,
            context={
                "direction_id": "d-other",
                "pr_number": 304,
                "landed_on_integration": True,
            },
        )
    )
    await db_session.commit()


@pytest.mark.asyncio
async def test_direction_scoped_merge_grade_spawn_and_record(db_session):
    """A direction-scoped spawn carries direction_id in payload + task.context,
    derives the integrated merged-PR window, and its completed artifact records
    scoped verdicts/residual + objective_gap debt keyed on the direction."""
    from app import category_verdict_ledger as cvl
    from app import completion_debt
    from app.models import CompletionDebtEntry
    from app.release_qualifier_audit import (
        _spawn_direction_merge_grade_task,
        record_direction_merge_grade,
    )

    project = await _make_direction_project(db_session)
    direction_id = "d-1a2b3c4d5e6f"
    direction = {
        "id": direction_id,
        "text": "Scope the blind objective judge to directions",
        "status": "active",
        "completion_criteria": _GOOD_GOAL,
        "context": {"success_evidence": _GOOD_GOAL},
    }
    candidate_sha = "beef" * 10
    attempt_id = uuid.uuid4()

    await _seed_direction_landed_tasks(db_session, project.id, direction_id)

    # --- spawn: direction_id in payload + context, window derived from tasks --
    task = await _spawn_direction_merge_grade_task(
        db_session,
        project.id,
        direction=direction,
        candidate_sha=candidate_sha,
        qualification_attempt_id=attempt_id,
    )
    await db_session.commit()
    await db_session.refresh(task)

    assert task is not None
    assert task.status == TaskStatus.QUEUED
    payload = task.first_payload
    assert payload["type"] == "release_merge_grade"
    assert payload["direction_id"] == direction_id
    assert payload["version_goal"] == _GOOD_GOAL
    # The diff base defaults to the project's PROMOTION TARGET ("main"), NOT the
    # live integration branch ("automain"): the candidate_sha is the automain tip
    # on the terminal-rollup path, so an automain base would produce an EMPTY
    # three-dot range and the objective could never be graded.
    assert payload["base_branch"] == "main"
    assert payload["diff_range"] == f"main...{candidate_sha}"
    assert payload["candidate_sha"] == candidate_sha
    assert payload["merged_prs"] == [301, 302]
    assert payload["quality_tier"] == "opus"
    assert task.context["direction_id"] == direction_id
    # Blindness contract: running_update must NOT appear in any payload key.
    assert not any("running_update" in str(k).lower() for k in payload)

    # --- record: the completed artifact's verdicts/debt key on the direction --
    artifact = {
        "graded_candidate_sha": candidate_sha,
        "covers_goal": False,
        "coverage_score": 0.4,
        "biggest_gap": "Objective still missing the ledger wiring.",
        "rationale": "Read the merged diff.",
        "category_verdicts": [
            {
                "category_key": "c-dir-1",
                "status": "not_met",
                "gap": "Direction-scoped verdict not yet recorded.",
                "citations": [{"kind": "diff", "ref": "backend/app/a.py"}],
            }
        ],
        "residual_uncaptured_gap": (
            "The objective's acceptance criteria are not fully captured."
        ),
    }
    cert = await record_direction_merge_grade(
        db_session,
        direction_id=direction_id,
        project_id=project.id,
        goal=_GOOD_GOAL,
        artifact=artifact,
        active_categories=[{"category_key": "c-dir-1", "text": "objective met"}],
        qualification_attempt_id=attempt_id,
        grader_run_id=str(task.id),
    )
    await db_session.commit()

    assert cert["status"] == "fail"
    assert cert["blocked"] is True
    verdicts = await cvl.get_category_verdicts(
        db_session, None, candidate_sha, direction_id=direction_id
    )
    assert len(verdicts) == 1
    assert verdicts[0].direction_id == direction_id
    assert verdicts[0].category_key == "c-dir-1"
    assert verdicts[0].status == "not_met"
    assert verdicts[0].gap == "Direction-scoped verdict not yet recorded."
    residual = await cvl.get_residual(
        db_session, None, candidate_sha, direction_id=direction_id
    )
    assert residual == artifact["residual_uncaptured_gap"]

    # The not_met verdict opens the DIRECTION-keyed objective_gap debt.
    key = completion_debt.objective_gap_direction_dedupe_key(direction_id, "c-dir-1")
    entry = (
        await db_session.execute(
            select(CompletionDebtEntry).where(CompletionDebtEntry.dedupe_key == key)
        )
    ).scalar_one()
    assert entry.kind == completion_debt.KIND_OBJECTIVE_GAP
    assert entry.status == completion_debt.STATUS_OPEN
    assert entry.detail["direction_id"] == direction_id
    assert entry.detail["version_id"] is None
    assert entry.detail["category_key"] == "c-dir-1"
    assert entry.detail["status"] == "not_met"
    assert entry.detail["gap"] == "Direction-scoped verdict not yet recorded."
    assert entry.detail["candidate_sha"] == candidate_sha


@pytest.mark.asyncio
async def test_direction_spawn_resolves_objective_from_completion_criteria(db_session):
    """The direction objective is read from completion_criteria, then falls back
    to context.success_evidence."""
    from app.release_qualifier_audit import _spawn_direction_merge_grade_task

    project = await _make_direction_project(db_session)
    direction_id = "d-deadbeef1234"
    candidate_sha = "cafe" * 10
    attempt_id = uuid.uuid4()

    task = await _spawn_direction_merge_grade_task(
        db_session,
        project.id,
        direction={
            "id": direction_id,
            "completion_criteria": "Ship the scoped objective gate",
            "context": {"success_evidence": "ignored — completion_criteria wins"},
        },
        candidate_sha=candidate_sha,
        qualification_attempt_id=attempt_id,
    )
    await db_session.commit()
    await db_session.refresh(task)
    assert task.first_payload["version_goal"] == "Ship the scoped objective gate"

    task2 = await _spawn_direction_merge_grade_task(
        db_session,
        project.id,
        direction={
            "id": "d-2nd",
            "context": {"success_evidence": "Fallback success evidence text"},
        },
        candidate_sha=candidate_sha,
        qualification_attempt_id=attempt_id,
    )
    await db_session.commit()
    await db_session.refresh(task2)
    assert task2.first_payload["version_goal"] == "Fallback success evidence text"


@pytest.mark.asyncio
async def test_direction_spawn_terminal_rollup_diff_range_is_non_empty(db_session):
    """A terminal-rollup direction grade must NOT grade an EMPTY three-dot range.

    On the primary path the candidate_sha IS the integration-branch (automain)
    tip right after the rollup lands.  Pinning the diff base to the integration
    branch would yield ``automain...automain-tip`` — an empty symmetric
    difference that the agent pre-flight would accept as 'resolves' (rev-list
    prints 0, exits 0) and then grade a blank surface, so the objective could
    never be graded as met.  The spawner pins the base to the project's
    PROMOTION TARGET (a stable ancestor that always lags the candidate) — the
    same convention the version path uses — guaranteeing a non-empty range.
    """
    from app.release_qualifier_audit import _spawn_direction_merge_grade_task

    project = await _make_direction_project(db_session)  # integration=automain, promotion_target=main
    direction_id = "d-terminal-rollup"
    candidate_sha = "f00d" * 10  # the automain tip after the rollup merged
    attempt_id = uuid.uuid4()

    task = await _spawn_direction_merge_grade_task(
        db_session,
        project.id,
        direction={
            "id": direction_id,
            "completion_criteria": "A real objective to grade",
        },
        candidate_sha=candidate_sha,
        qualification_attempt_id=attempt_id,
    )
    await db_session.commit()
    await db_session.refresh(task)
    payload = task.first_payload

    # The base is the PROMOTION TARGET, never the integration branch the
    # candidate was resolved from — that branch equality is the empty-range
    # condition, and it must be structurally impossible on the terminal path.
    assert payload["base_branch"] == "main"
    assert payload["base_branch"] == project.promotion_target
    assert payload["base_branch"] != project.integration_branch
    # The graded range is the stable promotion-target...candidate window.
    assert payload["diff_range"] == f"main...{candidate_sha}"


async def _seed_direction_merge_grade_task(
    db_session, project, direction_id, candidate_sha, attempt_id, artifact_logs
):
    """Seed a COMPLETED direction-scoped release_merge_grade task + artifact."""
    mg_task = Task(
        project_id=project.id,
        title="dir grade",
        payloads=[{"type": "release_merge_grade", "base_branch": "main"}],
        vm_instance_id="vm-mg",
        status=TaskStatus.COMPLETED,
        context={
            "direction_id": direction_id,
            "candidate_sha": candidate_sha,
            "qualification_attempt_id": str(attempt_id),
        },
    )
    db_session.add(mg_task)
    await db_session.flush()
    db_session.add(
        Artifact(
            task_id=mg_task.id,
            artifact_type=ArtifactType.RELEASE_MERGE_GRADE,
            logs=json.dumps(artifact_logs),
        )
    )
    await db_session.commit()
    return mg_task


@pytest.mark.asyncio
async def test_consume_direction_merge_grade_false_when_no_ledger_write(db_session):
    """consume_direction_merge_grade_if_ready returns False when the artifact
    produced NO ledger writes (missing graded_candidate_sha / no per-category
    verdicts), so the lifecycle does not re-stamp the direction row every
    cycle just because a grade task is COMPLETED."""
    from app import category_verdict_ledger as cvl
    from app.strategic_completion import consume_direction_merge_grade_if_ready

    project = await _make_direction_project(db_session)
    direction_id = "d-consume-nograde"
    candidate_sha = "bead" * 10
    attempt_id = uuid.uuid4()

    # A usable HOLISTIC verdict but NO graded_candidate_sha and NO
    # category_verdicts → record_direction_merge_grade writes nothing.
    await _seed_direction_merge_grade_task(
        db_session,
        project,
        direction_id,
        candidate_sha,
        attempt_id,
        {
            "covers_goal": False,
            "coverage_score": 0.4,
            "biggest_gap": "Objective unmet.",
            "rationale": "Read the diff.",
        },
    )

    direction = {
        "id": direction_id,
        "completion_criteria": "Ship the objective.",
        "criterion_keys": ["c-consume-1"],
    }
    recorded = await consume_direction_merge_grade_if_ready(
        db_session, project, direction
    )
    assert recorded is False

    # Nothing was persisted in the direction-scoped ledger.
    verdicts = await cvl.get_category_verdicts(
        db_session, None, candidate_sha, direction_id=direction_id
    )
    assert verdicts == []


@pytest.mark.asyncio
async def test_consume_direction_merge_grade_true_when_ledger_written(db_session):
    """consume_direction_merge_grade_if_ready returns True ONLY when a real
    verdict/debt record was written, and the next call is a no-op (no re-stamp)."""
    from app import category_verdict_ledger as cvl
    from app.strategic_completion import consume_direction_merge_grade_if_ready

    project = await _make_direction_project(db_session)
    direction_id = "d-consume-yes"
    candidate_sha = "beef" * 10
    attempt_id = uuid.uuid4()

    await _seed_direction_merge_grade_task(
        db_session,
        project,
        direction_id,
        candidate_sha,
        attempt_id,
        {
            "graded_candidate_sha": candidate_sha,
            "covers_goal": False,
            "coverage_score": 0.4,
            "biggest_gap": "Objective unmet.",
            "rationale": "Read the diff.",
            "category_verdicts": [
                {
                    "category_key": "c-consume-1",
                    "status": "not_met",
                    "gap": "Not yet shipped.",
                    "citations": [{"kind": "diff", "ref": "backend/app/a.py"}],
                }
            ],
            "residual_uncaptured_gap": None,
        },
    )

    direction = {
        "id": direction_id,
        "completion_criteria": "Ship the objective.",
        "criterion_keys": ["c-consume-1"],
    }
    recorded = await consume_direction_merge_grade_if_ready(
        db_session, project, direction
    )
    assert recorded is True

    # The verdict actually landed in the direction-scoped ledger.
    verdicts = await cvl.get_category_verdicts(
        db_session, None, candidate_sha, direction_id=direction_id
    )
    assert len(verdicts) == 1
    assert verdicts[0].category_key == "c-consume-1"

    # Already recorded → the next consume is a no-op (returns False).
    recorded2 = await consume_direction_merge_grade_if_ready(
        db_session, project, direction
    )
    assert recorded2 is False


@pytest.mark.asyncio
async def test_direction_spawn_fails_closed_on_missing_or_contentless_objective(db_session):
    """No gradeable objective (missing or contentless) spawns NO task row."""
    from app.release_qualifier_audit import _spawn_direction_merge_grade_task

    project = await _make_direction_project(db_session)
    attempt_id = uuid.uuid4()
    candidate_sha = "dead" * 10

    for direction in (
        {"id": "d-missing"},  # no objective fields at all
        {"id": "d-contentless", "completion_criteria": "test"},  # placeholder word
        {"id": "d-blank", "completion_criteria": "   "},
    ):
        spawned = await _spawn_direction_merge_grade_task(
            db_session,
            project.id,
            direction=direction,
            candidate_sha=candidate_sha,
            qualification_attempt_id=attempt_id,
        )
        await db_session.commit()
        assert spawned is None, f"direction {direction['id']} should fail closed"


@pytest.mark.asyncio
async def test_direction_spawn_fails_closed_when_project_missing(db_session):
    """No project row → no integration branch to pin the diff range to — the
    spawner fails closed (never fabricates a hardcoded 'main' fallback)."""
    from app.release_qualifier_audit import _spawn_direction_merge_grade_task

    attempt_id = uuid.uuid4()
    candidate_sha = "face" * 10
    spawned = await _spawn_direction_merge_grade_task(
        db_session,
        uuid.uuid4(),  # a project id that has no Project row
        direction={"id": "d-orphan", "completion_criteria": "A real objective"},
        candidate_sha=candidate_sha,
        qualification_attempt_id=attempt_id,
    )
    await db_session.commit()
    assert spawned is None


@pytest.mark.asyncio
async def test_direction_spawn_keeps_payload_blind_to_running_update(db_session):
    """The direction-scoped payload never leaks running-update text."""
    from app.release_qualifier_audit import _spawn_direction_merge_grade_task

    project = await _make_direction_project(db_session)
    candidate_sha = "aced" * 10
    attempt_id = uuid.uuid4()
    # Even a direction whose context carries a strategist narrative must not
    # thread it through the grade payload.
    task = await _spawn_direction_merge_grade_task(
        db_session,
        project.id,
        direction={
            "id": "d-blind",
            "completion_criteria": "A real objective",
            "context": {
                "success_evidence": "ignored",
                "running_update": "the strategist says everything is fine",
            },
        },
        candidate_sha=candidate_sha,
        qualification_attempt_id=attempt_id,
    )
    await db_session.commit()
    await db_session.refresh(task)
    payload = task.first_payload
    assert not any("running_update" in str(k).lower() for k in payload)
    assert "running_update" not in json.dumps(payload)
