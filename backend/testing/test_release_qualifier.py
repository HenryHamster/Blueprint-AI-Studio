"""Tests for the release qualifier (automain LTS Phase 3).

Covers:
  * compute_composite — each hard blocker forces confidence 0.0; the
    policy-weighted sum; the promote threshold boundary.
  * _load_policy — parses promotion_policy.yaml; falls back to hardcoded
    defaults on any read/parse failure (never raises).
  * _score_audit / _score_smoketest — artifact → sub-score derivation,
    including the deleted-test-file and startup-failure hard-blocker sentinels.
  * run_qualification — spawns the release_audit + qualifier-smoketest tasks
    and stamps version.last_qualification = {status: "pending", ...}.
  * _finalize_qualification — promote / dry-run / not-promoted decision paths.
  * promote — success path (open + merge PR, advance version) and the
    non-merged result → FAILED-promotion path.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from app import release_qualifier as rq
from app import release_version as rv
from app.models import (
    Artifact,
    ArtifactType,
    Project,
    Task,
    TaskStatus,
)

# Canonical default policy used throughout — mirrors promotion_policy.yaml.
_POLICY = {
    "weights": {"audit": 0.40, "test_erosion": 0.30, "smoketest": 0.30},
    "threshold": 0.75,
}


# ===========================================================================
# compute_composite — pure scoring function
# ===========================================================================


class TestComputeCompositeHardBlockers:
    """Each hard blocker must force confidence to 0.0 and promote to False."""

    def test_ci_not_green_forces_zero(self):
        """CI red is a hard blocker even with perfect sub-scores."""
        result = rq.compute_composite(
            ci_green=False,
            audit_score=1.0,
            test_erosion=1.0,
            smoketest_score=1.0,
            policy=_POLICY,
        )
        assert result["confidence"] == 0.0
        assert result["promote"] is False
        assert result["blocker"] == "ci_not_green"

    def test_smoketest_startup_failure_forces_zero(self):
        """A None smoketest_score (startup-suite failure) hard-blocks."""
        result = rq.compute_composite(
            ci_green=True,
            audit_score=1.0,
            test_erosion=1.0,
            smoketest_score=None,
            policy=_POLICY,
        )
        assert result["confidence"] == 0.0
        assert result["promote"] is False
        assert result["blocker"] == "smoketest_startup_failed"

    def test_deleted_test_file_forces_zero(self):
        """A None test_erosion (deleted test file) hard-blocks."""
        result = rq.compute_composite(
            ci_green=True,
            audit_score=1.0,
            test_erosion=None,
            smoketest_score=1.0,
            policy=_POLICY,
        )
        assert result["confidence"] == 0.0
        assert result["promote"] is False
        assert result["blocker"] == "test_file_deleted"

    def test_missing_audit_forces_zero(self):
        """A None audit_score hard-blocks — the composite is meaningless
        without it, so smoketest+test_erosion alone can never promote."""
        result = rq.compute_composite(
            ci_green=True,
            audit_score=None,
            test_erosion=1.0,
            smoketest_score=1.0,
            policy=_POLICY,
        )
        assert result["confidence"] == 0.0
        assert result["promote"] is False
        assert result["blocker"] == "audit_missing"

    def test_blocker_precedence_ci_first(self):
        """When multiple blockers fire, ci_not_green is reported first."""
        result = rq.compute_composite(
            ci_green=False,
            audit_score=None,
            test_erosion=None,
            smoketest_score=None,
            policy=_POLICY,
        )
        assert result["blocker"] == "ci_not_green"

    def test_blocked_result_still_reports_sub_scores(self):
        """Even when a blocker fires, sub_scores are reported for observability."""
        result = rq.compute_composite(
            ci_green=False,
            audit_score=0.8,
            test_erosion=0.9,
            smoketest_score=0.7,
            policy=_POLICY,
        )
        assert result["sub_scores"] == {
            "audit": 0.8,
            "test_erosion": 0.9,
            "smoketest": 0.7,
        }


class TestComputeCompositeWeightedSum:
    """When no blocker fires, confidence is the policy-weighted sum."""

    def test_all_perfect_scores_confidence_one(self):
        """Perfect sub-scores sum to 1.0 (weights total 1.0)."""
        result = rq.compute_composite(
            ci_green=True,
            audit_score=1.0,
            test_erosion=1.0,
            smoketest_score=1.0,
            policy=_POLICY,
        )
        assert result["confidence"] == pytest.approx(1.0)
        assert result["promote"] is True
        assert result["blocker"] is None

    def test_weighted_sum_arithmetic(self):
        """0.40*0.5 + 0.30*1.0 + 0.30*1.0 = 0.80."""
        result = rq.compute_composite(
            ci_green=True,
            audit_score=0.5,
            test_erosion=1.0,
            smoketest_score=1.0,
            policy=_POLICY,
        )
        assert result["confidence"] == pytest.approx(0.80)
        assert result["promote"] is True

    def test_weights_are_applied_per_dimension(self):
        """Audit carries 0.40 weight: a 0.0 audit alone caps confidence at 0.60."""
        result = rq.compute_composite(
            ci_green=True,
            audit_score=0.0,
            test_erosion=1.0,
            smoketest_score=1.0,
            policy=_POLICY,
        )
        assert result["confidence"] == pytest.approx(0.60)
        assert result["promote"] is False

    def test_sub_scores_clamped_above_one(self):
        """Sub-scores above 1.0 are clamped before weighting."""
        result = rq.compute_composite(
            ci_green=True,
            audit_score=5.0,
            test_erosion=3.0,
            smoketest_score=2.0,
            policy=_POLICY,
        )
        assert result["confidence"] == pytest.approx(1.0)
        assert result["sub_scores"]["audit"] == 1.0

    def test_sub_scores_clamped_below_zero(self):
        """Negative sub-scores are clamped to 0.0 before weighting."""
        result = rq.compute_composite(
            ci_green=True,
            audit_score=-1.0,
            test_erosion=-0.5,
            smoketest_score=-2.0,
            policy=_POLICY,
        )
        assert result["confidence"] == 0.0
        assert result["promote"] is False
        # No blocker — clamped-to-zero is a low score, not a hard blocker.
        assert result["blocker"] is None


class TestComputeCompositeThreshold:
    """promote is True iff confidence >= threshold."""

    def test_exactly_at_threshold_promotes(self):
        """confidence == threshold promotes (inclusive boundary)."""
        # audit=0.75, test=0.75, smoke=0.75 → 0.75 composite.
        result = rq.compute_composite(
            ci_green=True,
            audit_score=0.75,
            test_erosion=0.75,
            smoketest_score=0.75,
            policy=_POLICY,
        )
        assert result["confidence"] == pytest.approx(0.75)
        assert result["promote"] is True

    def test_just_below_threshold_does_not_promote(self):
        """confidence just under threshold does not promote."""
        result = rq.compute_composite(
            ci_green=True,
            audit_score=0.74,
            test_erosion=0.74,
            smoketest_score=0.74,
            policy=_POLICY,
        )
        assert result["confidence"] < 0.75
        assert result["promote"] is False

    def test_custom_threshold_from_policy(self):
        """The threshold comes from the policy dict, not a hardcoded constant."""
        strict = {"weights": _POLICY["weights"], "threshold": 0.95}
        result = rq.compute_composite(
            ci_green=True,
            audit_score=0.9,
            test_erosion=0.9,
            smoketest_score=0.9,
            policy=strict,
        )
        assert result["confidence"] == pytest.approx(0.90)
        # 0.90 < 0.95 strict threshold.
        assert result["promote"] is False


# ===========================================================================
# _load_policy — read promotion_policy.yaml, fall back on failure
# ===========================================================================


def _point_policy_at(mocker, tmp_path, content: str | None):
    """Repoint rq._POLICY_PATH at a temp file (or a nonexistent path).

    Path instances are immutable, so the read_text method cannot be patched on
    the instance — instead the module attribute is swapped for a real Path that
    either holds ``content`` or does not exist (when ``content`` is None).  This
    exercises the genuine read_text + yaml.safe_load code path.
    """
    target = tmp_path / "promotion_policy.yaml"
    if content is not None:
        target.write_text(content, encoding="utf-8")
    # else: leave the file absent so read_text raises FileNotFoundError.
    mocker.patch.object(rq, "_POLICY_PATH", target)
    return target


class TestLoadPolicy:
    def test_parses_real_policy_file(self):
        """The shipped promotion_policy.yaml parses to the documented values."""
        policy = rq._load_policy()
        assert set(policy.keys()) == {"weights", "threshold"}
        assert set(policy["weights"].keys()) == {
            "audit",
            "test_erosion",
            "smoketest",
        }
        # Values are coerced to float.
        for v in policy["weights"].values():
            assert isinstance(v, float)
        assert isinstance(policy["threshold"], float)
        # Matches the shipped defaults.
        assert policy["weights"]["audit"] == pytest.approx(0.40)
        assert policy["weights"]["test_erosion"] == pytest.approx(0.30)
        assert policy["weights"]["smoketest"] == pytest.approx(0.30)
        assert policy["threshold"] == pytest.approx(0.75)

    def test_fallback_on_missing_file(self, mocker, tmp_path):
        """A missing policy file falls back to hardcoded defaults, no raise."""
        _point_policy_at(mocker, tmp_path, None)
        policy = rq._load_policy()
        assert policy["weights"] == {
            "audit": 0.40,
            "test_erosion": 0.30,
            "smoketest": 0.30,
        }
        assert policy["threshold"] == 0.75

    def test_fallback_on_malformed_yaml(self, mocker, tmp_path):
        """Malformed YAML falls back to defaults instead of raising."""
        _point_policy_at(mocker, tmp_path, "weights: {unterminated: [1, 2")
        policy = rq._load_policy()
        assert policy["threshold"] == 0.75
        assert policy["weights"]["audit"] == 0.40

    def test_fallback_on_missing_weights_key(self, mocker, tmp_path):
        """A policy file with no 'weights' mapping falls back to defaults."""
        _point_policy_at(mocker, tmp_path, "threshold: 0.9\n")
        policy = rq._load_policy()
        assert policy["weights"]["audit"] == 0.40
        assert policy["threshold"] == 0.75

    def test_fallback_on_non_numeric_weight(self, mocker, tmp_path):
        """A non-numeric weight value triggers the fallback."""
        _point_policy_at(
            mocker,
            tmp_path,
            "weights:\n  audit: not-a-number\n  test_erosion: 0.3\n"
            "  smoketest: 0.3\nthreshold: 0.75\n",
        )
        policy = rq._load_policy()
        # Coercion failure → full fallback.
        assert policy["weights"]["audit"] == 0.40

    def test_fallback_on_scalar_yaml(self, mocker, tmp_path):
        """YAML that parses to a non-mapping (a bare scalar) falls back."""
        _point_policy_at(mocker, tmp_path, "just-a-string\n")
        policy = rq._load_policy()
        assert policy["weights"]["audit"] == 0.40
        assert policy["threshold"] == 0.75

    def test_fallback_never_returns_module_constant(self, mocker, tmp_path):
        """The fallback returns a copy — mutating it must not corrupt the
        module-level _DEFAULT_POLICY for the next caller."""
        _point_policy_at(mocker, tmp_path, None)
        policy = rq._load_policy()
        policy["weights"]["audit"] = 999.0
        policy["threshold"] = 999.0
        # A fresh fallback must still see the real defaults.
        fresh = rq._load_policy()
        assert fresh["weights"]["audit"] == 0.40
        assert fresh["threshold"] == 0.75

    def test_parses_custom_valid_yaml(self, mocker, tmp_path):
        """A well-formed custom policy is parsed (not just the fallback)."""
        _point_policy_at(
            mocker,
            tmp_path,
            "weights:\n  audit: 0.5\n  test_erosion: 0.25\n"
            "  smoketest: 0.25\nthreshold: 0.8\n",
        )
        policy = rq._load_policy()
        assert policy["weights"]["audit"] == pytest.approx(0.5)
        assert policy["threshold"] == pytest.approx(0.8)


# ===========================================================================
# _score_audit / _score_smoketest — artifact → sub-score derivation
# ===========================================================================


class TestScoreAudit:
    def test_clean_audit_report(self):
        """A clean report yields the audit_score and a 1.0 erosion score."""
        report = {
            "audit_score": 0.85,
            "test_erosion": {
                "deleted_test_files": [],
                "assertion_delta": 12,
                "test_count_delta": 3,
            },
            "findings": [],
        }
        audit, erosion = rq._score_audit(report)
        assert audit == pytest.approx(0.85)
        assert erosion == pytest.approx(1.0)

    def test_deleted_test_file_returns_none_erosion(self):
        """A deleted test file makes the erosion score the None hard-blocker."""
        report = {
            "audit_score": 0.9,
            "test_erosion": {
                "deleted_test_files": ["testing/test_gone.py"],
                "assertion_delta": 0,
                "test_count_delta": -5,
            },
        }
        audit, erosion = rq._score_audit(report)
        assert audit == pytest.approx(0.9)
        assert erosion is None

    def test_net_test_count_reduction_penalized(self):
        """A negative test_count_delta drops the erosion score below 1.0."""
        report = {
            "audit_score": 1.0,
            "test_erosion": {
                "deleted_test_files": [],
                "assertion_delta": 0,
                "test_count_delta": -2,
            },
        }
        _, erosion = rq._score_audit(report)
        assert erosion is not None
        assert erosion < 1.0

    def test_missing_audit_score_returns_none(self):
        """An absent audit_score yields None (a hard blocker downstream)."""
        report = {"test_erosion": {"deleted_test_files": []}}
        audit, erosion = rq._score_audit(report)
        assert audit is None
        assert erosion == pytest.approx(1.0)

    def test_none_report_returns_none_none(self):
        """A None report (no artifact) yields (None, None)."""
        assert rq._score_audit(None) == (None, None)

    def test_no_erosion_block_assumes_neutral(self):
        """A report with no test_erosion block assumes neutral (1.0)."""
        audit, erosion = rq._score_audit({"audit_score": 0.7})
        assert audit == pytest.approx(0.7)
        assert erosion == pytest.approx(1.0)


class TestScoreSmoketest:
    def test_all_tests_pass(self):
        """A fully-passing smoketest yields a 1.0 score."""
        result = {
            "passed": True,
            "tests": [
                {"name": "a", "status": "pass"},
                {"name": "b", "status": "pass"},
            ],
            "suites": {"startup": {"passed": True}},
        }
        assert rq._score_smoketest(result) == pytest.approx(1.0)

    def test_partial_pass_ratio(self):
        """The score is the pass ratio (passed / total)."""
        result = {
            "passed": False,
            "tests": [
                {"name": "a", "status": "pass"},
                {"name": "b", "status": "pass"},
                {"name": "c", "status": "fail"},
                {"name": "d", "status": "fail"},
            ],
            "suites": {"startup": {"passed": True}},
        }
        assert rq._score_smoketest(result) == pytest.approx(0.5)

    def test_startup_suite_failure_returns_none(self):
        """A failed startup suite returns the None hard-blocker sentinel."""
        result = {
            "passed": False,
            "tests": [{"name": "a", "status": "pass"}],
            "suites": {"startup": {"passed": False}},
        }
        assert rq._score_smoketest(result) is None

    def test_none_result_returns_none(self):
        """A None result (no artifact) is treated as a startup failure."""
        assert rq._score_smoketest(None) is None

    def test_no_tests_but_passed_scores_one(self):
        """A passing result with no individual tests scores 1.0."""
        result = {"passed": True, "tests": [], "suites": {}}
        assert rq._score_smoketest(result) == pytest.approx(1.0)


# ===========================================================================
# Shared fixtures for the DB-backed coordinator tests
# ===========================================================================


async def _make_project_and_version(db_session):
    """Create a self-repo project with an authorized GitHub repo + open v1."""
    project = Project(
        name="RQ-Test",
        user_id="rq-user",
        github_owner="acme",
        github_repo="widget",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    version = await rv.open_version(db_session, project.id, version_number=1)
    await db_session.commit()
    await db_session.refresh(version)
    return project, version


# ===========================================================================
# run_qualification — phase 1: snapshot + spawn
# ===========================================================================


@pytest.mark.asyncio
async def test_run_qualification_spawns_both_children(db_session, mocker):
    """run_qualification snapshots the SHA, spawns release_audit + smoketest,
    and stamps version.last_qualification = {status: 'pending', ...}."""
    project, version = await _make_project_and_version(db_session)

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=AsyncMock(return_value="cafe1234" * 5),
    )
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(return_value={"conclusion": "success"}),
    )

    result = await rq.run_qualification(db_session, project.id, version)
    await db_session.commit()

    assert result["status"] == "spawned"
    assert result["ci_green"] is True
    assert result["candidate_sha"] == "cafe1234" * 5

    # last_qualification stamped pending, attempts bumped.
    await db_session.refresh(version)
    assert version.last_qualification["status"] == "pending"
    assert version.last_qualification["candidate_sha"] == "cafe1234" * 5
    assert version.last_qualification["ci_green"] is True
    assert version.qualification_attempts == 1

    # Two child tasks exist: a release_audit and a qualifier smoketest.
    from sqlmodel import select

    tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    types = sorted((t.first_payload or {}).get("type") for t in tasks)
    assert types == ["release_audit", "smoketest"]

    by_type = {(t.first_payload or {}).get("type"): t for t in tasks}
    audit_task = by_type["release_audit"]
    smoke_task = by_type["smoketest"]

    # release_audit clones main and audits the main...automain diff.
    assert audit_task.first_payload["base_branch"] == "main"
    assert audit_task.first_payload["diff_range"] == "main...automain"
    assert audit_task.context["release_version_id"] == str(version.id)

    # The qualifier smoketest runs against automain and is flagged so its
    # failure does not spawn a fix task.
    assert smoke_task.first_payload["base_branch"] == "automain"
    assert smoke_task.context["qualifier_smoketest"] is True
    assert smoke_task.context["release_version_id"] == str(version.id)


@pytest.mark.asyncio
async def test_run_qualification_records_ci_red(db_session, mocker):
    """A red CI conclusion is recorded as ci_green=False (not acted on here —
    the composite applies it as a hard blocker at finalize time)."""
    project, version = await _make_project_and_version(db_session)

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=AsyncMock(return_value="dead" * 10),
    )
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(return_value={"conclusion": "failure"}),
    )

    result = await rq.run_qualification(db_session, project.id, version)
    await db_session.commit()

    assert result["status"] == "spawned"
    assert result["ci_green"] is False
    await db_session.refresh(version)
    assert version.last_qualification["ci_green"] is False


@pytest.mark.asyncio
async def test_run_qualification_no_credentials(db_session, mocker):
    """With no resolvable GitHub token, run_qualification records an error and
    does not spawn children."""
    project = Project(name="RQ-NoCreds", user_id="rq-user")  # no github_owner/repo
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    version = await rv.open_version(db_session, project.id, version_number=1)
    await db_session.commit()
    await db_session.refresh(version)

    result = await rq.run_qualification(db_session, project.id, version)
    await db_session.commit()

    assert result["status"] == "no_credentials"
    await db_session.refresh(version)
    assert version.last_qualification["status"] == "error"
    assert version.last_qualification["error"] == "no_credentials"
    assert version.qualification_attempts == 1

    # No child tasks spawned.
    from sqlmodel import select

    tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    assert len(tasks) == 0


# ===========================================================================
# _finalize_qualification — phase 2: collect + decide
# ===========================================================================


async def _seed_qualification_children(
    db_session,
    project,
    version,
    *,
    audit_logs: dict | None,
    smoke_logs: dict | None,
    ci_green: bool = True,
):
    """Create the two child tasks + their report artifacts, and stamp
    version.last_qualification with their ids (pending state)."""
    audit_task = Task(
        project_id=project.id,
        title="audit",
        payloads=[{"type": "release_audit", "base_branch": "main"}],
        vm_instance_id="vm-audit",
        status=TaskStatus.COMPLETED,
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
    await db_session.commit()
    await db_session.refresh(audit_task)
    await db_session.refresh(smoke_task)

    if audit_logs is not None:
        db_session.add(
            Artifact(
                task_id=audit_task.id,
                artifact_type=ArtifactType.RELEASE_AUDIT_REPORT,
                logs=json.dumps(audit_logs),
            )
        )
    if smoke_logs is not None:
        db_session.add(
            Artifact(
                task_id=smoke_task.id,
                artifact_type=ArtifactType.SMOKETEST_RESULT,
                logs=json.dumps(smoke_logs),
            )
        )

    version.last_qualification = {
        "status": "pending",
        "candidate_sha": "feed" * 10,
        "ci_green": ci_green,
        "started_at": "2026-05-19T00:00:00",
        "audit_task_id": str(audit_task.id),
        "smoketest_task_id": str(smoke_task.id),
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)
    return audit_task, smoke_task


@pytest.mark.asyncio
async def test_finalize_dry_run_when_promotion_disabled(db_session, mocker):
    """A high-confidence candidate is NOT promoted while promotion_enabled is
    False — the composite is recorded as a dry-run and the version stays open."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = False

    project, version = await _make_project_and_version(db_session)
    await _seed_qualification_children(
        db_session,
        project,
        version,
        audit_logs={
            "audit_score": 1.0,
            "test_erosion": {
                "deleted_test_files": [],
                "assertion_delta": 5,
                "test_count_delta": 2,
            },
        },
        smoke_logs={
            "passed": True,
            "tests": [{"name": "a", "status": "pass"}],
            "suites": {"startup": {"passed": True}},
        },
    )

    # promote() must NOT be called in dry-run mode.
    promote_spy = mocker.patch(
        "app.release_qualifier.promote",
        new=AsyncMock(),
    )

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    assert result["status"] == "dry_run"
    assert result["would_promote"] is True
    assert result["confidence"] == pytest.approx(1.0)
    promote_spy.assert_not_called()

    await db_session.refresh(version)
    assert version.status == "open"
    assert version.last_qualification["status"] == "dry_run"


@pytest.mark.asyncio
async def test_finalize_not_promoted_on_blocker_sets_stabilize(db_session):
    """A hard blocker (deleted test file) keeps the version open and flips it
    back to the stabilize phase."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = True

    project, version = await _make_project_and_version(db_session)
    # Version starts in build; finalize on a blocker must move it to stabilize.
    assert version.phase == "build"
    await _seed_qualification_children(
        db_session,
        project,
        version,
        audit_logs={
            "audit_score": 1.0,
            "test_erosion": {
                "deleted_test_files": ["testing/test_deleted.py"],
                "assertion_delta": 0,
                "test_count_delta": -3,
            },
        },
        smoke_logs={
            "passed": True,
            "tests": [{"name": "a", "status": "pass"}],
            "suites": {"startup": {"passed": True}},
        },
    )

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    assert result["status"] == "not_promoted"
    assert result["blocker"] == "test_file_deleted"

    await db_session.refresh(version)
    assert version.status == "open"
    assert version.phase == "stabilize"
    assert version.last_qualification["status"] == "not_promoted"
    assert version.last_qualification["blocker"] == "test_file_deleted"


@pytest.mark.asyncio
async def test_finalize_promotes_when_enabled_and_confident(db_session, mocker):
    """With promotion_enabled=True and a confident candidate, finalize calls
    promote(), which opens + merges the PR and advances the version."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = True

    project, version = await _make_project_and_version(db_session)
    await _seed_qualification_children(
        db_session,
        project,
        version,
        audit_logs={
            "audit_score": 1.0,
            "test_erosion": {
                "deleted_test_files": [],
                "assertion_delta": 10,
                "test_count_delta": 4,
            },
        },
        smoke_logs={
            "passed": True,
            "tests": [{"name": "a", "status": "pass"}],
            "suites": {"startup": {"passed": True}},
        },
    )

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"number": 77, "html_url": "http://pr/77"}),
    )
    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(return_value={"merged": True, "message": "Merged"}),
    )

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    assert result["status"] == "promoted"
    assert result["promoted_sha"] == "feed" * 10
    assert result["next_version_number"] == 2

    # merge_pull_request must use merge_method="merge" (preserve history).
    assert merge_mock.call_args.kwargs["merge_method"] == "merge"

    # Version v1 is promoted with SHAs stamped.
    await db_session.refresh(version)
    assert version.status == "promoted"
    assert version.promoted_sha == "feed" * 10
    assert version.promoted_at is not None

    # A fresh open v2 was created in the build phase.
    next_version = await rv.current_open_version(db_session, project.id)
    assert next_version is not None
    assert next_version.version_number == 2
    assert next_version.phase == "build"
    assert next_version.base_sha == "feed" * 10


# ===========================================================================
# promote — non-merged result is a FAILED promotion
# ===========================================================================


@pytest.mark.asyncio
async def test_promote_non_merged_result_is_failed_promotion(db_session, mocker):
    """merge_pull_request returning {merged: False} (405/409) is a FAILED
    promotion — the version stays open and goes back to stabilize, and the
    next version is NOT opened."""
    project, version = await _make_project_and_version(db_session)

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"number": 91, "html_url": "http://pr/91"}),
    )
    mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(
            return_value={
                "merged": False,
                "conflict": True,
                "message": "merge conflict",
            }
        ),
    )

    result = await rq.promote(
        db_session,
        version,
        "abcd" * 10,
        composite={"confidence": 0.9},
        base_record={"candidate_sha": "abcd" * 10},
    )
    await db_session.commit()

    assert result["status"] == "promotion_failed"
    assert result["error"] == "merge_blocked"

    await db_session.refresh(version)
    # Version stays open — a failed promotion only ever blocks.
    assert version.status == "open"
    assert version.phase == "stabilize"
    assert version.last_qualification["status"] == "promotion_failed"
    assert version.last_qualification["error"] == "merge_blocked"
    assert version.last_qualification["conflict"] is True

    # No next version was opened.
    versions = await rv.current_open_version(db_session, project.id)
    assert versions is not None
    assert versions.version_number == 1


@pytest.mark.asyncio
async def test_promote_success_opens_next_version(db_session, mocker):
    """A successful merge marks v1 promoted and opens v2 (build phase)."""
    _project, version = await _make_project_and_version(db_session)

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    create_mock = mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"number": 5, "html_url": "http://pr/5"}),
    )
    mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(return_value={"merged": True, "message": "Merged"}),
    )

    result = await rq.promote(
        db_session,
        version,
        "1234" * 10,
        composite={"confidence": 0.88},
    )
    await db_session.commit()

    assert result["status"] == "promoted"
    assert result["next_version_number"] == 2

    # PR opened automain → main.
    assert create_mock.call_args.kwargs["head"] == "automain"
    assert create_mock.call_args.kwargs["base"] == "main"

    await db_session.refresh(version)
    assert version.status == "promoted"
    assert version.promoted_sha == "1234" * 10


@pytest.mark.asyncio
async def test_promote_empty_diff_is_noop_success(db_session, mocker):
    """An empty diff (automain == main) is a degenerate but valid success —
    create_pull_request returns {empty_diff: True}; the version still advances."""
    _project, version = await _make_project_and_version(db_session)

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(
            return_value={"empty_diff": True, "merged": True, "html_url": ""}
        ),
    )
    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(return_value={"merged": True}),
    )

    result = await rq.promote(db_session, version, "0000" * 10)
    await db_session.commit()

    assert result["status"] == "promoted"
    # No merge attempted on an empty diff.
    merge_mock.assert_not_called()
    await db_session.refresh(version)
    assert version.status == "promoted"


# ===========================================================================
# maybe_finalize_qualification — task-completion coordination glue
# ===========================================================================


async def _make_task_with_context(
    db_session,
    project,
    *,
    status: TaskStatus,
    context: dict,
) -> Task:
    """Create a minimal Task with the given status and context."""
    task = Task(
        project_id=project.id,
        title="test-task",
        payloads=[{"type": "release_audit", "base_branch": "main"}],
        vm_instance_id=f"vm-{id(context)}",
        status=status,
        context=context,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    return task


async def _seed_pending_qualification(
    db_session,
    project,
    version,
    *,
    audit_status: TaskStatus = TaskStatus.COMPLETED,
    smoke_status: TaskStatus = TaskStatus.COMPLETED,
) -> tuple[Task, Task]:
    """Create the two child tasks in the requested statuses and stamp
    version.last_qualification as 'pending' pointing to them.

    Both child tasks carry ``context["release_version_id"]`` so that
    maybe_finalize_qualification recognises them as qualifier children —
    matching the real spawn path in run_qualification.
    """
    audit_task = Task(
        project_id=project.id,
        title="audit-child",
        payloads=[{"type": "release_audit", "base_branch": "main"}],
        vm_instance_id="vm-audit-child",
        status=audit_status,
        context={"release_version_id": str(version.id)},
    )
    smoke_task = Task(
        project_id=project.id,
        title="smoke-child",
        payloads=[{"type": "smoketest", "base_branch": "automain"}],
        vm_instance_id="vm-smoke-child",
        status=smoke_status,
        context={"release_version_id": str(version.id)},
    )
    db_session.add(audit_task)
    db_session.add(smoke_task)
    await db_session.commit()
    await db_session.refresh(audit_task)
    await db_session.refresh(smoke_task)

    version.last_qualification = {
        "status": "pending",
        "candidate_sha": "abcd" * 10,
        "ci_green": True,
        "started_at": "2026-05-20T00:00:00",
        "audit_task_id": str(audit_task.id),
        "smoketest_task_id": str(smoke_task.id),
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)
    return audit_task, smoke_task


@pytest.mark.asyncio
async def test_maybe_finalize_non_qualifier_task_returns_none(db_session):
    """A task with no release_version_id in its context must return None
    immediately — cheap fast path for the overwhelmingly common non-qualifier
    case."""
    project, _version = await _make_project_and_version(db_session)
    task = await _make_task_with_context(
        db_session,
        project,
        status=TaskStatus.COMPLETED,
        context={"some_other_key": "value"},
    )

    result = await rq.maybe_finalize_qualification(db_session, task)

    assert result is None


@pytest.mark.asyncio
async def test_maybe_finalize_only_one_child_terminal_returns_none(db_session):
    """When only one of the two qualifier children has terminated,
    maybe_finalize_qualification returns None and does NOT call
    _finalize_qualification."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = False

    project, version = await _make_project_and_version(db_session)

    # audit child is COMPLETED, smoketest is still IN_PROGRESS.
    audit_task, _smoke_task = await _seed_pending_qualification(
        db_session,
        project,
        version,
        audit_status=TaskStatus.COMPLETED,
        smoke_status=TaskStatus.IN_PROGRESS,
    )

    result = await rq.maybe_finalize_qualification(db_session, audit_task)

    assert result is None

    # Version must still be pending — finalize must NOT have run.
    await db_session.refresh(version)
    assert version.last_qualification["status"] == "pending"


@pytest.mark.asyncio
async def test_maybe_finalize_both_children_terminal_calls_finalize(
    db_session, mocker
):
    """When both children are in terminal states, maybe_finalize_qualification
    calls _finalize_qualification and returns its result dict."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = False

    project, version = await _make_project_and_version(db_session)

    audit_task, smoke_task = await _seed_pending_qualification(
        db_session,
        project,
        version,
        audit_status=TaskStatus.COMPLETED,
        smoke_status=TaskStatus.COMPLETED,
    )

    # Seed RELEASE_AUDIT_REPORT and SMOKETEST_RESULT artifacts so that
    # _finalize_qualification can compute a real composite.
    db_session.add(
        Artifact(
            task_id=audit_task.id,
            artifact_type=ArtifactType.RELEASE_AUDIT_REPORT,
            logs=json.dumps(
                {
                    "audit_score": 1.0,
                    "test_erosion": {
                        "deleted_test_files": [],
                        "assertion_delta": 2,
                        "test_count_delta": 1,
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
                    "tests": [{"name": "a", "status": "pass"}],
                    "suites": {"startup": {"passed": True}},
                }
            ),
        )
    )
    await db_session.commit()

    # Use the smoke_task as the "just completed" trigger.
    result = await rq.maybe_finalize_qualification(db_session, smoke_task)
    await db_session.commit()

    # _finalize_qualification ran and returned a result dict.
    assert result is not None
    # With promotion_enabled=False and a high-confidence candidate: dry_run.
    assert result["status"] == "dry_run"
    assert result["would_promote"] is True

    # Version is no longer pending.
    await db_session.refresh(version)
    assert version.last_qualification["status"] == "dry_run"


@pytest.mark.asyncio
async def test_maybe_finalize_already_finalized_version_returns_none(db_session):
    """If the version's last_qualification.status is not 'pending' (e.g.
    'dry_run', 'promoted', 'not_promoted'), a second call returns None
    without re-running _finalize_qualification (idempotent guard)."""
    project, version = await _make_project_and_version(db_session)

    audit_task, _smoke_task = await _seed_pending_qualification(
        db_session,
        project,
        version,
        audit_status=TaskStatus.COMPLETED,
        smoke_status=TaskStatus.COMPLETED,
    )

    # Simulate the version already having been finalized.
    version.last_qualification = {
        **version.last_qualification,
        "status": "dry_run",
        "confidence": 0.9,
        "would_promote": True,
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    result = await rq.maybe_finalize_qualification(db_session, audit_task)

    assert result is None

    # Status must not have changed — still dry_run.
    await db_session.refresh(version)
    assert version.last_qualification["status"] == "dry_run"
