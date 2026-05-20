"""Tests for the release qualifier (automain LTS Phase 3).

Covers:
  * compute_composite — each hard blocker forces confidence 0.0; the
    policy-weighted sum; the promote threshold boundary; the audit-missing vs
    test_file_deleted blocker-label ordering; the smoketest-disabled mode
    (dimension dropped + weights renormalized — audit H3).
  * _load_policy — parses promotion_policy.yaml; falls back to hardcoded
    defaults on any read/parse failure (never raises), including a weight-sum
    that does not total ~1.0.
  * _score_audit / _score_smoketest — artifact → sub-score derivation,
    including the deleted-test-file and startup-failure hard-blocker sentinels.
  * run_qualification — phase + in-flight + already-dry-run guards (audit H1);
    the FOR UPDATE row-lock serialization (audit C3); the candidate_sha-pinned
    child payloads + main...<candidate_sha> audit diff (audit C4); the
    smoketest-skipped spawn path when smoketest_enabled is False (audit H3).
  * _finalize_qualification — promote / dry-run / not-promoted decision paths;
    the smoketest-disabled drop-dimension path.
  * promote — success path (create release branch at candidate_sha, open +
    merge release-v{N} → main PR, advance version — audit C4); automain
    advancing is now harmless (no staleness guard); the stale-release-branch
    recreate path; the real-merge-SHA recording; the non-merged result →
    FAILED-promotion path; and the project_not_found → failure path.
  * sweep_stuck_qualifications — defense-in-depth reaper for qualifications
    stuck pending; the qualification-timeout force-finalize (audit H2); the
    per-version commit isolation (audit H5).
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
        """A None test_erosion (deleted test file) hard-blocks — reported as
        test_file_deleted only when the audit report itself was present."""
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

    def test_failed_audit_both_none_reports_audit_missing_not_deletion(self):
        """A fully-FAILED release_audit posts no report, so _score_audit(None)
        yields BOTH audit_score=None and test_erosion=None.  The blocker must
        be reported as 'audit_missing' (the truthful cause) and NOT mislabelled
        'test_file_deleted' — no test file was actually deleted."""
        # This is exactly the (None, None) pair _score_audit returns for a
        # missing report — the regression the ordering fix addresses.
        audit_score, test_erosion = rq._score_audit(None)
        assert audit_score is None and test_erosion is None

        result = rq.compute_composite(
            ci_green=True,
            audit_score=audit_score,
            test_erosion=test_erosion,
            smoketest_score=1.0,
            policy=_POLICY,
        )
        assert result["confidence"] == 0.0
        assert result["promote"] is False
        # audit_missing — NOT test_file_deleted.
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

    def test_blocker_precedence_audit_before_test_deleted(self):
        """With CI green + smoketest OK but BOTH audit and test_erosion None,
        audit_missing is reported ahead of test_file_deleted."""
        result = rq.compute_composite(
            ci_green=True,
            audit_score=None,
            test_erosion=None,
            smoketest_score=0.9,
            policy=_POLICY,
        )
        assert result["blocker"] == "audit_missing"

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


class TestComputeCompositeSmoketestDisabled:
    """Audit H3: with smoketest_enabled=False the smoketest dimension is
    dropped, the audit + test_erosion weights renormalize to sum 1.0, and a
    None smoketest_score is NOT a hard blocker (the subsystem simply didn't
    run)."""

    def test_none_smoketest_is_not_a_blocker_when_disabled(self):
        """A None smoketest_score does NOT hard-block when smoketest_enabled is
        False — without this, no candidate could ever promote with the
        subsystem off."""
        result = rq.compute_composite(
            ci_green=True,
            audit_score=1.0,
            test_erosion=1.0,
            smoketest_score=None,
            policy=_POLICY,
            smoketest_enabled=False,
        )
        assert result["blocker"] is None
        assert result["promote"] is True

    def test_weights_renormalized_to_audit_plus_test(self):
        """With the default 0.40/0.30/0.30 policy, dropping smoketest
        renormalizes to audit≈0.5714 / test≈0.4286 (0.40/0.70, 0.30/0.70).
        A perfect audit + perfect test_erosion therefore scores exactly 1.0."""
        result = rq.compute_composite(
            ci_green=True,
            audit_score=1.0,
            test_erosion=1.0,
            smoketest_score=None,
            policy=_POLICY,
            smoketest_enabled=False,
        )
        assert result["confidence"] == pytest.approx(1.0)

    def test_renormalized_weight_split_is_proportional(self):
        """The renormalized split keeps the original audit:test_erosion ratio.
        0.40:0.30 → 0.5714:0.4286.  A 0.0 audit alone caps confidence at the
        renormalized test_erosion weight (0.30/0.70 ≈ 0.4286)."""
        result = rq.compute_composite(
            ci_green=True,
            audit_score=0.0,
            test_erosion=1.0,
            smoketest_score=None,
            policy=_POLICY,
            smoketest_enabled=False,
        )
        assert result["confidence"] == pytest.approx(0.30 / 0.70)

    def test_ci_red_still_blocks_when_smoketest_disabled(self):
        """CI red is still a hard blocker regardless of smoketest_enabled."""
        result = rq.compute_composite(
            ci_green=False,
            audit_score=1.0,
            test_erosion=1.0,
            smoketest_score=None,
            policy=_POLICY,
            smoketest_enabled=False,
        )
        assert result["blocker"] == "ci_not_green"
        assert result["promote"] is False

    def test_audit_missing_still_blocks_when_smoketest_disabled(self):
        """A missing audit report still hard-blocks with smoketest disabled —
        the audit dimension is still mandatory."""
        result = rq.compute_composite(
            ci_green=True,
            audit_score=None,
            test_erosion=1.0,
            smoketest_score=None,
            policy=_POLICY,
            smoketest_enabled=False,
        )
        assert result["blocker"] == "audit_missing"

    def test_deleted_test_file_still_blocks_when_smoketest_disabled(self):
        """A deleted test file still hard-blocks with smoketest disabled."""
        result = rq.compute_composite(
            ci_green=True,
            audit_score=1.0,
            test_erosion=None,
            smoketest_score=None,
            policy=_POLICY,
            smoketest_enabled=False,
        )
        assert result["blocker"] == "test_file_deleted"

    def test_enabled_mode_unchanged_is_the_default(self):
        """smoketest_enabled defaults to True — the existing rubric is the
        default behaviour and a None smoketest_score still hard-blocks."""
        result = rq.compute_composite(
            ci_green=True,
            audit_score=1.0,
            test_erosion=1.0,
            smoketest_score=None,
            policy=_POLICY,
        )
        assert result["blocker"] == "smoketest_startup_failed"


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

    def test_fallback_when_weights_sum_above_one(self, mocker, tmp_path):
        """Weights summing to >1.0 saturate confidence and disable the gate —
        a mis-weighted policy must fall back to the safe default rubric."""
        _point_policy_at(
            mocker,
            tmp_path,
            # 0.6 + 0.6 + 0.6 = 1.8 — well outside the 0.01 tolerance.
            "weights:\n  audit: 0.6\n  test_erosion: 0.6\n"
            "  smoketest: 0.6\nthreshold: 0.75\n",
        )
        policy = rq._load_policy()
        assert policy["weights"] == {
            "audit": 0.40,
            "test_erosion": 0.30,
            "smoketest": 0.30,
        }
        assert policy["threshold"] == 0.75

    def test_fallback_when_weights_sum_below_one(self, mocker, tmp_path):
        """Weights summing to <1.0 mean nothing can clear the threshold — that
        mis-weighted policy must fall back to the safe default rubric."""
        _point_policy_at(
            mocker,
            tmp_path,
            # 0.1 + 0.1 + 0.1 = 0.3 — well below 1.0.
            "weights:\n  audit: 0.1\n  test_erosion: 0.1\n"
            "  smoketest: 0.1\nthreshold: 0.75\n",
        )
        policy = rq._load_policy()
        assert policy["weights"]["audit"] == 0.40
        assert policy["weights"]["test_erosion"] == 0.30
        assert policy["weights"]["smoketest"] == 0.30

    def test_weights_summing_to_one_within_tolerance_accepted(self, mocker, tmp_path):
        """A custom policy whose weights sum to exactly 1.0 is honoured."""
        _point_policy_at(
            mocker,
            tmp_path,
            "weights:\n  audit: 0.5\n  test_erosion: 0.25\n"
            "  smoketest: 0.25\nthreshold: 0.8\n",
        )
        policy = rq._load_policy()
        # Sum is 1.0 exactly — parsed, not the fallback.
        assert policy["weights"]["audit"] == pytest.approx(0.5)
        assert policy["weights"]["test_erosion"] == pytest.approx(0.25)
        assert policy["threshold"] == pytest.approx(0.8)

    def test_weights_within_tolerance_band_accepted(self, mocker, tmp_path):
        """A weight sum within the 0.01 tolerance of 1.0 is still accepted —
        small floating-point drift must not trip the fallback."""
        _point_policy_at(
            mocker,
            tmp_path,
            # 0.401 + 0.30 + 0.30 = 1.001 — inside the 0.01 band.
            "weights:\n  audit: 0.401\n  test_erosion: 0.30\n"
            "  smoketest: 0.30\nthreshold: 0.75\n",
        )
        policy = rq._load_policy()
        assert policy["weights"]["audit"] == pytest.approx(0.401)

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

    def test_skipped_tests_count_as_zero(self):
        """A 'skip' status counts as 0 (conservative) — not as a pass.  Two
        passing + two skipped tests score 0.5, not 1.0."""
        result = {
            "passed": True,
            "tests": [
                {"name": "a", "status": "pass"},
                {"name": "b", "status": "pass"},
                {"name": "c", "status": "skip"},
                {"name": "d", "status": "skip"},
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


async def _make_project_and_version(db_session, *, phase: str = "build"):
    """Create a self-repo project with an authorized GitHub repo + open v1.

    ``phase`` defaults to "build" (open_version's default).  Pass
    phase="stabilize" for tests that exercise run_qualification, whose phase
    guard only admits a stabilize-phase candidate.
    """
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
    if phase != "build":
        rv.set_version_phase(version, phase, reasoning="test setup")
        db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)
    return project, version


def _branch_sha_mock(*, automain: str = "automn0" * 5 + "00000", main: str = "main00" * 6 + "0000"):
    """An AsyncMock for github.get_branch_sha keyed on the requested branch.

    After the audit-C4 fix promote() pins the PR to the candidate commit and
    no longer re-resolves automain (the staleness guard is gone) — its only
    get_branch_sha call is for "main" (the post-merge HEAD).  run_qualification
    still resolves "automain" to snapshot the candidate.  This mock answers
    both branches so the same helper serves run_qualification and promote
    tests; ``main`` lets a promote test assert promoted_sha is the new main
    HEAD (the real merge commit, not the candidate).
    """

    async def _impl(token, owner, repo, branch):
        if branch == "automain":
            return automain
        if branch == "main":
            return main
        raise AssertionError(f"unexpected branch {branch!r}")

    return AsyncMock(side_effect=_impl)


def _create_branch_mock():
    """An AsyncMock for github.create_branch that echoes the requested SHA.

    After the audit-C4 fix promote() creates a release-v{N} branch pinned to
    the candidate commit before opening the PR.  github.create_branch returns
    ``{"ref": ..., "object": {"sha": <sha>}}``; this mock echoes the SHA it was
    asked to create so promote()'s "branch already exists at a stale SHA"
    delete+recreate path is NOT triggered (the branch lands exactly where
    asked).  Tests that exercise the stale-branch path mock create_branch
    explicitly.
    """

    async def _impl(token, owner, repo, branch_name, sha):
        return {"ref": f"refs/heads/{branch_name}", "object": {"sha": sha}}

    return AsyncMock(side_effect=_impl)


# ===========================================================================
# run_qualification — phase 1: guards + snapshot + spawn
# ===========================================================================


@pytest.mark.asyncio
async def test_run_qualification_spawns_both_children_when_smoketest_enabled(
    db_session, mocker
):
    """With smoketest_enabled=True, run_qualification snapshots the SHA, spawns
    release_audit + qualifier-smoketest, and stamps version.last_qualification
    = {status: 'pending', ...}.  Both child payloads carry the exact 40-char
    candidate_sha (audit C4 contract), and the audit diff_range is pinned to
    main...<candidate_sha> (NOT main...automain)."""
    from app import shared_state

    shared_state.runtime_config["smoketest_enabled"] = True

    project, version = await _make_project_and_version(
        db_session, phase="stabilize"
    )

    candidate = "cafe1234" * 5  # 40-char placeholder SHA
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=AsyncMock(return_value=candidate),
    )
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(return_value={"conclusion": "success"}),
    )

    result = await rq.run_qualification(db_session, project.id, version)
    await db_session.commit()

    assert result["status"] == "spawned"
    assert result["ci_green"] is True
    assert result["candidate_sha"] == candidate
    assert result["smoketest_enabled"] is True

    # last_qualification stamped pending, attempts bumped.
    await db_session.refresh(version)
    assert version.last_qualification["status"] == "pending"
    assert version.last_qualification["candidate_sha"] == candidate
    assert version.last_qualification["ci_green"] is True
    assert version.last_qualification["smoketest_enabled"] is True
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

    # release_audit clones main and audits the main...<candidate_sha> diff —
    # pinned to the exact snapshot commit, NOT the live automain branch.
    assert audit_task.first_payload["base_branch"] == "main"
    assert audit_task.first_payload["diff_range"] == f"main...{candidate}"
    # Audit C4 contract: candidate_sha is in the PAYLOAD, named exactly
    # candidate_sha, the full 40-char SHA.
    assert audit_task.first_payload["candidate_sha"] == candidate
    assert audit_task.context["release_version_id"] == str(version.id)

    # The qualifier smoketest carries candidate_sha in its payload too and is
    # flagged so its failure does not spawn a fix task.
    assert smoke_task.first_payload["candidate_sha"] == candidate
    assert smoke_task.context["qualifier_smoketest"] is True
    assert smoke_task.context["release_version_id"] == str(version.id)


@pytest.mark.asyncio
async def test_run_qualification_skips_smoketest_when_disabled(db_session, mocker):
    """Audit H3: with smoketest_enabled=False (the shipped default), only the
    release_audit child is spawned — NO qualifier-smoketest task — and
    last_qualification records smoketest_enabled=False with smoketest_task_id
    None."""
    from app import shared_state

    # Explicit for clarity — this is also the autouse-reset default.
    shared_state.runtime_config["smoketest_enabled"] = False

    project, version = await _make_project_and_version(
        db_session, phase="stabilize"
    )

    candidate = "d15ab1ed" * 5  # 40-char placeholder SHA
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=AsyncMock(return_value=candidate),
    )
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(return_value={"conclusion": "success"}),
    )

    result = await rq.run_qualification(db_session, project.id, version)
    await db_session.commit()

    assert result["status"] == "spawned"
    assert result["smoketest_enabled"] is False
    assert result["smoketest_task_id"] is None

    await db_session.refresh(version)
    assert version.last_qualification["smoketest_enabled"] is False
    assert version.last_qualification["smoketest_task_id"] is None
    assert version.last_qualification["audit_task_id"] is not None

    # Exactly one child task — the release_audit — and NO smoketest.
    from sqlmodel import select

    tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    types = sorted((t.first_payload or {}).get("type") for t in tasks)
    assert types == ["release_audit"]
    # The audit child still carries the pinned candidate_sha.
    assert tasks[0].first_payload["candidate_sha"] == candidate
    assert tasks[0].first_payload["diff_range"] == f"main...{candidate}"


@pytest.mark.asyncio
async def test_run_qualification_records_ci_red(db_session, mocker):
    """A red CI conclusion is recorded as ci_green=False (not acted on here —
    the composite applies it as a hard blocker at finalize time)."""
    project, version = await _make_project_and_version(
        db_session, phase="stabilize"
    )

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
    rv.set_version_phase(version, "stabilize", reasoning="test setup")
    db_session.add(version)
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


@pytest.mark.asyncio
async def test_run_qualification_wrong_phase_is_noop(db_session, mocker):
    """run_qualification on a build-phase version early-returns wrong_phase:
    no children spawned, last_qualification untouched, attempts not bumped."""
    # Default phase is "build" — not stabilize.
    project, version = await _make_project_and_version(db_session)
    assert version.phase == "build"

    token_mock = mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    sha_mock = mocker.patch(
        "app.github.get_branch_sha",
        new=AsyncMock(return_value="cafe" * 10),
    )

    result = await rq.run_qualification(db_session, project.id, version)
    await db_session.commit()

    assert result["status"] == "wrong_phase"
    # The guard fires before any GitHub I/O.
    token_mock.assert_not_called()
    sha_mock.assert_not_called()

    await db_session.refresh(version)
    # No side effects whatsoever.
    assert version.last_qualification is None
    assert version.qualification_attempts == 0

    from sqlmodel import select

    tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    assert len(tasks) == 0


@pytest.mark.asyncio
async def test_run_qualification_already_pending_is_noop(db_session, mocker):
    """A second run_qualification while a qualification is in flight (pending
    with a still-running child) early-returns already_pending and spawns
    nothing — no duplicate agent VMs, no orphaned prior children."""
    project, version = await _make_project_and_version(
        db_session, phase="stabilize"
    )

    # Seed a pending qualification whose audit child is still IN_PROGRESS.
    audit_task = Task(
        project_id=project.id,
        title="audit-child",
        payloads=[{"type": "release_audit", "base_branch": "main"}],
        vm_instance_id="vm-audit-inflight",
        status=TaskStatus.IN_PROGRESS,
        context={"release_version_id": str(version.id)},
    )
    smoke_task = Task(
        project_id=project.id,
        title="smoke-child",
        payloads=[{"type": "smoketest", "base_branch": "automain"}],
        vm_instance_id="vm-smoke-inflight",
        status=TaskStatus.COMPLETED,
        context={"release_version_id": str(version.id)},
    )
    db_session.add(audit_task)
    db_session.add(smoke_task)
    await db_session.commit()
    await db_session.refresh(audit_task)
    await db_session.refresh(smoke_task)

    version.last_qualification = {
        "status": "pending",
        "candidate_sha": "feed" * 10,
        "ci_green": True,
        "started_at": "2026-05-20T00:00:00",
        "audit_task_id": str(audit_task.id),
        "smoketest_task_id": str(smoke_task.id),
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)
    attempts_before = version.qualification_attempts

    sha_mock = mocker.patch(
        "app.github.get_branch_sha",
        new=AsyncMock(return_value="cafe" * 10),
    )

    result = await rq.run_qualification(db_session, project.id, version)
    await db_session.commit()

    assert result["status"] == "already_pending"
    # No new SHA snapshot — the guard fired before any GitHub I/O.
    sha_mock.assert_not_called()

    await db_session.refresh(version)
    # The original pending record is untouched; attempts NOT bumped.
    assert version.last_qualification["candidate_sha"] == "feed" * 10
    assert version.qualification_attempts == attempts_before

    # Still exactly the two original children — no third/fourth spawned.
    from sqlmodel import select

    tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    assert len(tasks) == 2


@pytest.mark.asyncio
async def test_run_qualification_pending_but_children_terminal_runs_fresh(
    db_session, mocker
):
    """A 'pending' record whose children have ALL terminated is NOT in flight
    — it is merely awaiting finalize.  A fresh run_qualification is allowed
    (the stale record gets overwritten), so a new qualification spawns."""
    project, version = await _make_project_and_version(
        db_session, phase="stabilize"
    )

    # Both children already terminal, yet last_qualification still 'pending'.
    audit_task = Task(
        project_id=project.id,
        title="audit-old",
        payloads=[{"type": "release_audit", "base_branch": "main"}],
        vm_instance_id="vm-audit-old",
        status=TaskStatus.COMPLETED,
        context={"release_version_id": str(version.id)},
    )
    smoke_task = Task(
        project_id=project.id,
        title="smoke-old",
        payloads=[{"type": "smoketest", "base_branch": "automain"}],
        vm_instance_id="vm-smoke-old",
        status=TaskStatus.FAILED,
        context={"release_version_id": str(version.id)},
    )
    db_session.add(audit_task)
    db_session.add(smoke_task)
    await db_session.commit()
    await db_session.refresh(audit_task)
    await db_session.refresh(smoke_task)

    version.last_qualification = {
        "status": "pending",
        "candidate_sha": "0ld0" * 10,
        "ci_green": True,
        "started_at": "2026-05-18T00:00:00",
        "audit_task_id": str(audit_task.id),
        "smoketest_task_id": str(smoke_task.id),
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=AsyncMock(return_value="newc" * 10),
    )
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(return_value={"conclusion": "success"}),
    )

    result = await rq.run_qualification(db_session, project.id, version)
    await db_session.commit()

    # A fresh qualification ran — the stale pending record did not block it.
    assert result["status"] == "spawned"
    assert result["candidate_sha"] == "newc" * 10

    await db_session.refresh(version)
    assert version.last_qualification["candidate_sha"] == "newc" * 10


@pytest.mark.asyncio
async def test_run_qualification_second_sequential_call_does_not_double_spawn(
    db_session, mocker
):
    """Audit C3: run_qualification serializes via a FOR UPDATE lock on the
    ReleaseVersion row and re-checks the in-flight guard under the lock.  A
    second call after the first has stamped a pending record (its audit child
    still QUEUED → in flight) early-returns already_pending and spawns nothing
    — so two callers (strategic qualify_request + admin POST /qualify) can
    never produce a duplicate audit/smoketest pair."""
    project, version = await _make_project_and_version(
        db_session, phase="stabilize"
    )

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=AsyncMock(return_value="5e91a1ed" * 5),
    )
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(return_value={"conclusion": "success"}),
    )

    # First call — spawns and stamps a pending record.
    first = await rq.run_qualification(db_session, project.id, version)
    await db_session.commit()
    assert first["status"] == "spawned"

    # Second call on the same version — the audit child is still QUEUED
    # (non-terminal) so the under-lock in-flight guard fires.
    second = await rq.run_qualification(db_session, project.id, version)
    await db_session.commit()
    assert second["status"] == "already_pending"

    # Exactly one child task total — no duplicate pair from the second call.
    from sqlmodel import select

    tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    # smoketest_enabled defaults False → one release_audit child only.
    assert len(tasks) == 1
    # qualification_attempts bumped exactly once (the second call no-op'd).
    await db_session.refresh(version)
    assert version.qualification_attempts == 1


@pytest.mark.asyncio
async def test_run_qualification_version_vanished_before_lock(db_session, mocker):
    """Audit C3: if the ReleaseVersion row is gone by the time run_qualification
    takes its FOR UPDATE lock, it returns version_not_found rather than
    crashing or spawning orphan children."""
    project, version = await _make_project_and_version(
        db_session, phase="stabilize"
    )

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=AsyncMock(return_value="90111111" * 5),
    )
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(return_value={"conclusion": "success"}),
    )

    # Delete the version row so the FOR UPDATE lock select finds nothing.
    await db_session.delete(version)
    await db_session.commit()

    result = await rq.run_qualification(db_session, project.id, version)

    assert result["status"] == "version_not_found"

    # No child tasks spawned.
    from sqlmodel import select

    tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    assert len(tasks) == 0


@pytest.mark.asyncio
async def test_run_qualification_already_dry_run_same_sha_is_noop(
    db_session, mocker
):
    """Audit H1: when the prior qualification finalized as a dry_run and
    automain HEAD still equals the recorded candidate_sha, run_qualification
    early-returns already_dry_run and spawns nothing — the strategic agent
    re-emitting qualify_request every cycle must not re-spawn 2 agent VMs for
    an identical, already-scored commit while promotion ships dark."""
    project, version = await _make_project_and_version(
        db_session, phase="stabilize"
    )

    dry_run_sha = "drc0de00" * 5  # 40-char placeholder SHA
    version.last_qualification = {
        "status": "dry_run",
        "candidate_sha": dry_run_sha,
        "ci_green": True,
        "would_promote": True,
        "confidence": 0.92,
        "started_at": "2026-05-19T00:00:00",
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)
    attempts_before = version.qualification_attempts

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    # automain HEAD still equals the dry-run candidate — automain has NOT moved.
    mocker.patch(
        "app.github.get_branch_sha",
        new=AsyncMock(return_value=dry_run_sha),
    )
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(return_value={"conclusion": "success"}),
    )

    result = await rq.run_qualification(db_session, project.id, version)
    await db_session.commit()

    assert result["status"] == "already_dry_run"

    await db_session.refresh(version)
    # The dry_run record is untouched, attempts NOT bumped.
    assert version.last_qualification["status"] == "dry_run"
    assert version.qualification_attempts == attempts_before

    # No child tasks spawned.
    from sqlmodel import select

    tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    assert len(tasks) == 0


@pytest.mark.asyncio
async def test_run_qualification_dry_run_but_automain_moved_runs_fresh(
    db_session, mocker
):
    """Audit H1: a prior dry_run does NOT block a fresh qualification once
    automain has advanced — the new automain HEAD is a different commit that
    has not been scored, so run_qualification spawns again."""
    project, version = await _make_project_and_version(
        db_session, phase="stabilize"
    )

    version.last_qualification = {
        "status": "dry_run",
        "candidate_sha": "01dc0de0" * 5,  # the old, already-scored commit
        "ci_green": True,
        "would_promote": True,
        "started_at": "2026-05-18T00:00:00",
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    # automain HEAD has moved on to a NEW commit since the dry_run.
    new_sha = "freshc0d" * 5
    mocker.patch(
        "app.github.get_branch_sha",
        new=AsyncMock(return_value=new_sha),
    )
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(return_value={"conclusion": "success"}),
    )

    result = await rq.run_qualification(db_session, project.id, version)
    await db_session.commit()

    # A fresh qualification ran — the new commit is unscored.
    assert result["status"] == "spawned"
    assert result["candidate_sha"] == new_sha

    await db_session.refresh(version)
    assert version.last_qualification["status"] == "pending"
    assert version.last_qualification["candidate_sha"] == new_sha


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
        "smoketest_enabled": True,
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
    # candidate_sha recorded by _seed_qualification_children is "feed" * 10;
    # promote() pins the PR to it (no staleness guard) and the post-merge main
    # HEAD is the new merge commit.
    merge_sha = "merged0" * 5 + "00000"
    mocker.patch(
        "app.github.get_branch_sha",
        new=_branch_sha_mock(main=merge_sha),
    )
    branch_mock = mocker.patch(
        "app.github.create_branch",
        new=_create_branch_mock(),
    )
    create_mock = mocker.patch(
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
    # The recorded promoted_sha is the REAL merge commit, not the candidate.
    assert result["promoted_sha"] == merge_sha
    assert result["next_version_number"] == 2

    # merge_pull_request must use merge_method="merge" (preserve history).
    assert merge_mock.call_args.kwargs["merge_method"] == "merge"

    # Audit C4: a release branch was created at the EXACT candidate SHA, and
    # the PR is opened from that branch (not live automain) into main.
    branch_mock.assert_called_once()
    assert branch_mock.call_args.args[3] == "release-v1"   # branch name
    assert branch_mock.call_args.args[4] == "feed" * 10    # pinned to candidate
    assert create_mock.call_args.kwargs["head"] == "release-v1"
    assert create_mock.call_args.kwargs["base"] == "main"

    # Version v1 is promoted with the merge SHA stamped.
    await db_session.refresh(version)
    assert version.status == "promoted"
    assert version.promoted_sha == merge_sha
    assert version.promoted_at is not None

    # A fresh open v2 was created in the build phase, based on the merge SHA.
    next_version = await rv.current_open_version(db_session, project.id)
    assert next_version is not None
    assert next_version.version_number == 2
    assert next_version.phase == "build"
    assert next_version.base_sha == merge_sha


# ===========================================================================
# promote — staleness guard, real-merge-SHA, failure paths
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
        "app.github.get_branch_sha",
        new=_branch_sha_mock(),
    )
    mocker.patch(
        "app.github.create_branch",
        new=_create_branch_mock(),
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
    """A successful merge marks v1 promoted and opens v2 (build phase).  The PR
    is opened from the release-v{N} branch (pinned to the candidate) into
    main (audit C4)."""
    _project, version = await _make_project_and_version(db_session)

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    merge_sha = "mainnew" * 5 + "00000"
    mocker.patch(
        "app.github.get_branch_sha",
        new=_branch_sha_mock(main=merge_sha),
    )
    branch_mock = mocker.patch(
        "app.github.create_branch",
        new=_create_branch_mock(),
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

    # Audit C4: a release-v1 branch was created at the candidate SHA, and the
    # PR is opened release-v1 → main (NOT live automain → main).
    branch_mock.assert_called_once()
    assert branch_mock.call_args.args[3] == "release-v1"
    assert branch_mock.call_args.args[4] == "1234" * 10
    assert create_mock.call_args.kwargs["head"] == "release-v1"
    assert create_mock.call_args.kwargs["base"] == "main"

    await db_session.refresh(version)
    assert version.status == "promoted"
    # promoted_sha is the re-resolved main HEAD (the merge commit), not the
    # candidate — merge_method="merge" produced a new commit.
    assert version.promoted_sha == merge_sha


@pytest.mark.asyncio
async def test_promote_empty_diff_is_noop_success(db_session, mocker):
    """An empty diff (release branch == main) is a degenerate but valid success
    — create_pull_request returns {empty_diff: True}; the version still
    advances and the candidate SHA is recorded (no new merge commit exists)."""
    _project, version = await _make_project_and_version(db_session)

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=_branch_sha_mock(),
    )
    mocker.patch(
        "app.github.create_branch",
        new=_create_branch_mock(),
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
    # Empty-diff path keeps the candidate SHA (automain == main).
    assert version.promoted_sha == "0000" * 10


@pytest.mark.asyncio
async def test_promote_succeeds_even_when_automain_advanced(db_session, mocker):
    """Audit C4: automain advancing after qualification is now HARMLESS — there
    is no staleness guard.  promote() pins the PR to a release-v{N} branch
    created at the exact candidate SHA, so even with automain far ahead the
    promotion still succeeds and the qualified commit (not live automain) is
    what lands."""
    _project, version = await _make_project_and_version(db_session)

    candidate = "qua11f1e" * 5  # the commit the children actually scored
    merge_sha = "mergedup" * 5

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    # automain HEAD has moved far past the candidate — irrelevant now, since
    # promote() never reads automain (the staleness guard is gone).  The mock
    # answers automain only to prove promote() does not consult it.
    automain_mock = _branch_sha_mock(
        automain="movedon0" * 5, main=merge_sha
    )
    mocker.patch("app.github.get_branch_sha", new=automain_mock)
    branch_mock = mocker.patch(
        "app.github.create_branch",
        new=_create_branch_mock(),
    )
    create_mock = mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"number": 1, "html_url": "http://pr/1"}),
    )
    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(return_value={"merged": True, "message": "Merged"}),
    )

    result = await rq.promote(
        db_session,
        version,
        candidate,  # the candidate snapshot — automain has since moved
        composite={"confidence": 0.95},
    )
    await db_session.commit()

    # Promotion SUCCEEDS — automain moving is expected and harmless.
    assert result["status"] == "promoted"

    # The release branch was created at the EXACT candidate SHA.
    branch_mock.assert_called_once()
    assert branch_mock.call_args.args[4] == candidate
    # The PR head is the pinned release branch, not automain.
    assert create_mock.call_args.kwargs["head"] == "release-v1"
    merge_mock.assert_called_once()

    # promote() never resolves the automain branch — only main (post-merge).
    resolved_branches = {
        call.args[3] for call in automain_mock.call_args_list
    }
    assert "automain" not in resolved_branches

    await db_session.refresh(version)
    assert version.status == "promoted"
    # last_qualification records the scored candidate; promoted_sha is the
    # post-merge main HEAD.
    assert version.last_qualification["candidate_sha"] == candidate
    assert version.promoted_sha == merge_sha


@pytest.mark.asyncio
async def test_promote_recreates_stale_release_branch(db_session, mocker):
    """Audit C4: if the release-v{N} branch already exists from a prior failed
    attempt and points at a DIFFERENT (stale) SHA, promote() deletes and
    recreates it at the current candidate SHA so the PR always carries exactly
    the qualified commit."""
    _project, version = await _make_project_and_version(db_session)

    candidate = "freshca0" * 5    # the commit we want to promote now
    stale_sha = "0ldbra0c" * 5    # what an existing release branch points at

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=_branch_sha_mock(main="merged00" * 5),
    )

    # First create_branch call: the branch already exists at the STALE sha
    # (GitHub's 422 idempotent path returns the existing ref).  Second call
    # (after delete) creates it cleanly at the candidate.
    create_calls: list[str] = []

    async def _create_branch(token, owner, repo, branch_name, sha):
        create_calls.append(sha)
        if len(create_calls) == 1:
            # Simulate "branch already exists, pointing somewhere stale".
            return {
                "ref": f"refs/heads/{branch_name}",
                "object": {"sha": stale_sha},
            }
        return {"ref": f"refs/heads/{branch_name}", "object": {"sha": sha}}

    branch_mock = mocker.patch(
        "app.github.create_branch", new=AsyncMock(side_effect=_create_branch)
    )
    delete_mock = mocker.patch(
        "app.github.delete_branch", new=AsyncMock(return_value=True)
    )
    mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"number": 3, "html_url": "http://pr/3"}),
    )
    mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(return_value={"merged": True, "message": "Merged"}),
    )

    result = await rq.promote(
        db_session, version, candidate, composite={"confidence": 0.9}
    )
    await db_session.commit()

    assert result["status"] == "promoted"

    # The stale branch was deleted, then recreated at the candidate SHA.
    delete_mock.assert_called_once()
    assert delete_mock.call_args.args[3] == "release-v1"
    assert branch_mock.call_count == 2
    # Both create attempts targeted the candidate SHA — never the stale one.
    assert create_calls == [candidate, candidate]


@pytest.mark.asyncio
async def test_promote_release_branch_creation_failure_is_blocked(
    db_session, mocker
):
    """Audit C4: a failure creating the release branch blocks the promotion —
    no PR is opened, the version returns to stabilize, and main is untouched."""
    project, version = await _make_project_and_version(db_session)

    from fastapi import HTTPException

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch(
        "app.github.create_branch",
        new=AsyncMock(
            side_effect=HTTPException(status_code=502, detail="GitHub 502")
        ),
    )
    create_mock = mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"number": 1, "html_url": "http://pr/1"}),
    )
    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(return_value={"merged": True}),
    )

    result = await rq.promote(
        db_session, version, "abcd" * 10, composite={"confidence": 0.9}
    )
    await db_session.commit()

    assert result["status"] == "promotion_failed"
    assert result["error"] == "release_branch_failed"

    # No PR opened, no merge — the branch failure blocks before either.
    create_mock.assert_not_called()
    merge_mock.assert_not_called()

    await db_session.refresh(version)
    # Version stays open and returns to stabilize — a failed promotion blocks.
    assert version.status == "open"
    assert version.phase == "stabilize"
    assert version.last_qualification["status"] == "promotion_failed"
    assert version.last_qualification["error"] == "release_branch_failed"

    # No next version opened.
    open_v = await rv.current_open_version(db_session, project.id)
    assert open_v is not None
    assert open_v.version_number == 1


@pytest.mark.asyncio
async def test_promote_records_real_merge_sha_not_candidate(db_session, mocker):
    """After a real merge, promoted_sha + the v{N} tag use the NEW merge
    commit (re-resolved main HEAD), not the candidate SHA which is only a
    parent of that merge commit."""
    _project, version = await _make_project_and_version(db_session)

    candidate = "cand0000" * 5      # 40-char placeholder SHA
    merge_commit = "mergecmt" * 5   # 40-char placeholder SHA

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=_branch_sha_mock(main=merge_commit),
    )
    mocker.patch(
        "app.github.create_branch",
        new=_create_branch_mock(),
    )
    mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"number": 8, "html_url": "http://pr/8"}),
    )
    mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(return_value={"merged": True, "message": "Merged"}),
    )
    tag_mock = mocker.patch(
        "app.github.create_tag",
        new=AsyncMock(return_value={"ref": "refs/tags/v1"}),
    )

    result = await rq.promote(
        db_session, version, candidate, composite={"confidence": 0.9}
    )
    await db_session.commit()

    assert result["status"] == "promoted"
    assert result["promoted_sha"] == merge_commit

    await db_session.refresh(version)
    assert version.promoted_sha == merge_commit
    # last_qualification keeps BOTH: the candidate for traceability and the
    # real promoted_sha.
    assert version.last_qualification["candidate_sha"] == candidate
    assert version.last_qualification["promoted_sha"] == merge_commit

    # The v1 tag is created on the merge commit, NOT the candidate.
    tag_mock.assert_called_once()
    assert tag_mock.call_args.args[3] == "v1"          # tag name
    assert tag_mock.call_args.args[4] == merge_commit  # sha — the merge commit

    # The next version's base_sha is the merge commit too.
    next_version = await rv.current_open_version(db_session, version.project_id)
    assert next_version is not None
    assert next_version.base_sha == merge_commit


@pytest.mark.asyncio
async def test_promote_merged_but_main_sha_unresolvable_degrades_to_candidate(
    db_session, mocker
):
    """If main HEAD cannot be re-resolved AFTER a successful merge, the
    promotion still succeeds — promoted_sha degrades to the candidate (a known
    parent) rather than failing a merge that already landed."""
    from fastapi import HTTPException

    _project, version = await _make_project_and_version(db_session)
    candidate = "fallback" * 5

    async def _sha(token, owner, repo, branch):
        if branch == "main":
            # Post-merge resolution fails (e.g. transient GitHub 502).
            raise HTTPException(status_code=502, detail="GitHub 502")
        raise AssertionError(f"unexpected branch {branch!r}")

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch("app.github.get_branch_sha", new=AsyncMock(side_effect=_sha))
    mocker.patch(
        "app.github.create_branch",
        new=_create_branch_mock(),
    )
    mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"number": 9, "html_url": "http://pr/9"}),
    )
    mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(return_value={"merged": True, "message": "Merged"}),
    )
    mocker.patch(
        "app.github.create_tag",
        new=AsyncMock(return_value={"ref": "refs/tags/v1"}),
    )

    result = await rq.promote(
        db_session, version, candidate, composite={"confidence": 0.9}
    )
    await db_session.commit()

    # The merge landed — promotion succeeds despite the SHA-read failure.
    assert result["status"] == "promoted"
    assert result["promoted_sha"] == candidate

    await db_session.refresh(version)
    assert version.status == "promoted"
    assert version.promoted_sha == candidate


@pytest.mark.asyncio
async def test_promote_project_not_found_records_failure(db_session, mocker):
    """When promote() cannot load the Project row, it records a promotion
    failure (NOT a bare error return) so last_qualification reaches a terminal
    stabilize state — otherwise it would stay 'pending' and re-loop forever."""
    _project, version = await _make_project_and_version(db_session)

    # Force session.get(Project, ...) to return None even though the row
    # exists — simulates a project deleted between finalize and promote.
    real_get = db_session.get

    async def _fake_get(model, ident, *args, **kwargs):
        if model is Project:
            return None
        return await real_get(model, ident, *args, **kwargs)

    mocker.patch.object(db_session, "get", new=AsyncMock(side_effect=_fake_get))

    result = await rq.promote(
        db_session,
        version,
        "abcd" * 10,
        composite={"confidence": 0.9},
        base_record={"candidate_sha": "abcd" * 10},
    )
    await db_session.commit()

    # A real promotion-failure record, not a bare {status: error}.
    assert result["status"] == "promotion_failed"
    assert result["error"] == "project_not_found"

    await db_session.refresh(version)
    # The version reached a terminal stabilize state — NOT stuck pending.
    assert version.status == "open"
    assert version.phase == "stabilize"
    assert version.last_qualification is not None
    assert version.last_qualification["status"] == "promotion_failed"
    assert version.last_qualification["error"] == "project_not_found"
    # Crucially the record is no longer 'pending' — the re-loop is broken.
    assert version.last_qualification["status"] != "pending"


@pytest.mark.asyncio
async def test_finalize_project_not_found_breaks_pending_loop(db_session, mocker):
    """End-to-end: when _finalize_qualification reaches promote() and the
    project is gone, last_qualification must NOT be left 'pending' — a later
    finalize would otherwise re-loop forever."""
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

    # _finalize_qualification resolves credentials via session.get(Project) and
    # then promote() does too.  Make every Project lookup return None.
    real_get = db_session.get

    async def _fake_get(model, ident, *args, **kwargs):
        if model is Project:
            return None
        return await real_get(model, ident, *args, **kwargs)

    mocker.patch.object(db_session, "get", new=AsyncMock(side_effect=_fake_get))

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    assert result["status"] == "promotion_failed"
    assert result["error"] == "project_not_found"

    await db_session.refresh(version)
    # The pending loop is broken — status is terminal.
    assert version.last_qualification["status"] == "promotion_failed"
    assert version.last_qualification["status"] != "pending"


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
    started_at: str | None = None,
) -> tuple[Task, Task]:
    """Create the two child tasks in the requested statuses and stamp
    version.last_qualification as 'pending' pointing to them.

    Both child tasks carry ``context["release_version_id"]`` so that
    maybe_finalize_qualification recognises them as qualifier children —
    matching the real spawn path in run_qualification.

    ``started_at`` defaults to *now* so the qualification is well within the
    qualification-timeout window — sweep tests for the both-children-terminal
    path must not accidentally trip the age-bound timeout.  Pass an explicit
    old timestamp to exercise the timeout path.
    """
    from app.time_utils import _utcnow

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
        "smoketest_enabled": True,
        "started_at": started_at or _utcnow().isoformat(),
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


# ===========================================================================
# Release tagging — Phase 4: v{N} tag on promotion, failure is non-fatal
# ===========================================================================


@pytest.mark.asyncio
async def test_promote_success_creates_release_tag(db_session, mocker):
    """A successful promotion creates a git tag v{version_number} on the REAL
    merged commit (re-resolved main HEAD) via github.create_tag."""
    _project, version = await _make_project_and_version(db_session)

    merge_sha = "tagmerge" * 5
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=_branch_sha_mock(main=merge_sha),
    )
    mocker.patch(
        "app.github.create_branch",
        new=_create_branch_mock(),
    )
    mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"number": 42, "html_url": "http://pr/42"}),
    )
    mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(return_value={"merged": True, "message": "Merged"}),
    )
    tag_mock = mocker.patch(
        "app.github.create_tag",
        new=AsyncMock(
            return_value={"ref": "refs/tags/v1", "object": {"sha": "abc" * 13 + "a"}}
        ),
    )

    result = await rq.promote(
        db_session,
        version,
        "abcd" * 10,
        composite={"confidence": 0.9},
    )
    await db_session.commit()

    assert result["status"] == "promoted"

    # create_tag was called once with the v1 tag on the merge commit.
    tag_mock.assert_called_once()
    call_kwargs = tag_mock.call_args
    assert call_kwargs.args[3] == "v1"        # tag name
    assert call_kwargs.args[4] == merge_sha   # sha — the real merge commit


@pytest.mark.asyncio
async def test_promote_success_tag_failure_does_not_abort_promotion(db_session, mocker):
    """A create_tag failure (any exception) is non-fatal: the promotion still
    succeeds (status='promoted') and the next version is still opened."""
    _project, version = await _make_project_and_version(db_session)

    merge_sha = "beefmerg" * 5
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=_branch_sha_mock(main=merge_sha),
    )
    mocker.patch(
        "app.github.create_branch",
        new=_create_branch_mock(),
    )
    mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"number": 99, "html_url": "http://pr/99"}),
    )
    mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(return_value={"merged": True, "message": "Merged"}),
    )
    # Simulate a tag creation failure (e.g. GitHub 500, network error, etc.)
    from fastapi import HTTPException as _HTTPException

    mocker.patch(
        "app.github.create_tag",
        new=AsyncMock(side_effect=_HTTPException(status_code=500, detail="GitHub 500")),
    )

    result = await rq.promote(
        db_session,
        version,
        "beef" * 10,
        composite={"confidence": 0.95},
    )
    await db_session.commit()

    # The promotion itself must succeed despite the tag failure.
    assert result["status"] == "promoted"
    assert result["promoted_sha"] == merge_sha
    assert result["next_version_number"] == 2

    # The version row must be promoted.
    await db_session.refresh(version)
    assert version.status == "promoted"
    assert version.promoted_sha == merge_sha

    # The next version was opened normally.
    from app import release_version as rv_mod

    next_version = await rv_mod.current_open_version(db_session, version.project_id)
    assert next_version is not None
    assert next_version.version_number == 2


# ===========================================================================
# sweep_stuck_qualifications — defense-in-depth reaper
# ===========================================================================


@pytest.mark.asyncio
async def test_sweep_finalizes_version_stuck_pending(db_session):
    """A version stuck 'pending' with BOTH children terminal is finalized by
    the sweep — the count reflects exactly that one finalization."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = False

    project, version = await _make_project_and_version(db_session)

    # Both children COMPLETED but the version was never finalized (its
    # completion handler died) — last_qualification still says 'pending'.
    audit_task, smoke_task = await _seed_pending_qualification(
        db_session,
        project,
        version,
        audit_status=TaskStatus.COMPLETED,
        smoke_status=TaskStatus.COMPLETED,
    )
    db_session.add(
        Artifact(
            task_id=audit_task.id,
            artifact_type=ArtifactType.RELEASE_AUDIT_REPORT,
            logs=json.dumps(
                {
                    "audit_score": 1.0,
                    "test_erosion": {
                        "deleted_test_files": [],
                        "assertion_delta": 3,
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

    count = await rq.sweep_stuck_qualifications(db_session)
    await db_session.commit()

    assert count == 1

    # The stuck version is now finalized (dry_run — promotion disabled).
    await db_session.refresh(version)
    assert version.last_qualification["status"] == "dry_run"


@pytest.mark.asyncio
async def test_sweep_skips_version_with_running_child(db_session):
    """A 'pending' version with a still-running child is NOT swept — the
    normal completion path will (or already does) handle it."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = False

    project, version = await _make_project_and_version(db_session)

    # Audit COMPLETED but smoketest still IN_PROGRESS — not stuck, just running.
    await _seed_pending_qualification(
        db_session,
        project,
        version,
        audit_status=TaskStatus.COMPLETED,
        smoke_status=TaskStatus.IN_PROGRESS,
    )

    count = await rq.sweep_stuck_qualifications(db_session)
    await db_session.commit()

    assert count == 0

    # Version untouched — still pending.
    await db_session.refresh(version)
    assert version.last_qualification["status"] == "pending"


@pytest.mark.asyncio
async def test_sweep_skips_already_finalized_version(db_session):
    """A version whose last_qualification is not 'pending' is skipped — the
    sweep only acts on stuck pending records."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = False

    project, version = await _make_project_and_version(db_session)
    await _seed_pending_qualification(
        db_session,
        project,
        version,
        audit_status=TaskStatus.COMPLETED,
        smoke_status=TaskStatus.COMPLETED,
    )
    # Mark it already finalized.
    version.last_qualification = {
        **version.last_qualification,
        "status": "not_promoted",
        "blocker": "ci_not_green",
    }
    db_session.add(version)
    await db_session.commit()

    count = await rq.sweep_stuck_qualifications(db_session)
    await db_session.commit()

    assert count == 0

    await db_session.refresh(version)
    # Unchanged.
    assert version.last_qualification["status"] == "not_promoted"


@pytest.mark.asyncio
async def test_sweep_is_idempotent(db_session):
    """Calling the sweep twice finalizes the stuck version once; the second
    call is a safe no-op that finalizes nothing."""
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
    db_session.add(
        Artifact(
            task_id=audit_task.id,
            artifact_type=ArtifactType.RELEASE_AUDIT_REPORT,
            logs=json.dumps(
                {
                    "audit_score": 1.0,
                    "test_erosion": {"deleted_test_files": []},
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

    first = await rq.sweep_stuck_qualifications(db_session)
    await db_session.commit()
    second = await rq.sweep_stuck_qualifications(db_session)
    await db_session.commit()

    assert first == 1
    # Second sweep finds nothing left to finalize.
    assert second == 0


@pytest.mark.asyncio
async def test_sweep_no_pending_versions_returns_zero(db_session):
    """With no pending qualifications anywhere, the sweep returns 0."""
    # An open version with no qualification at all.
    await _make_project_and_version(db_session)

    count = await rq.sweep_stuck_qualifications(db_session)
    await db_session.commit()

    assert count == 0


# ===========================================================================
# Audit H3 — smoketest-disabled finalization (audit-only qualification)
# ===========================================================================


async def _seed_audit_only_pending_qualification(
    db_session,
    project,
    version,
    *,
    audit_status: TaskStatus = TaskStatus.COMPLETED,
    audit_logs: dict | None = None,
    started_at: str | None = None,
) -> Task:
    """Seed a smoketest-disabled pending qualification — only a release_audit
    child, smoketest_enabled=False, smoketest_task_id None — matching what
    run_qualification produces when runtime_config['smoketest_enabled'] is
    False."""
    from app.time_utils import _utcnow

    audit_task = Task(
        project_id=project.id,
        title="audit-child",
        payloads=[{"type": "release_audit", "base_branch": "main"}],
        vm_instance_id="vm-audit-only",
        status=audit_status,
        context={"release_version_id": str(version.id)},
    )
    db_session.add(audit_task)
    await db_session.commit()
    await db_session.refresh(audit_task)

    if audit_logs is not None:
        db_session.add(
            Artifact(
                task_id=audit_task.id,
                artifact_type=ArtifactType.RELEASE_AUDIT_REPORT,
                logs=json.dumps(audit_logs),
            )
        )

    version.last_qualification = {
        "status": "pending",
        "candidate_sha": "abcd" * 10,
        "ci_green": True,
        "smoketest_enabled": False,
        "started_at": started_at or _utcnow().isoformat(),
        "audit_task_id": str(audit_task.id),
        "smoketest_task_id": None,
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)
    return audit_task


@pytest.mark.asyncio
async def test_finalize_smoketest_disabled_drops_dimension(db_session):
    """Audit H3: a smoketest-disabled qualification (no smoketest child, no
    SMOKETEST_RESULT) finalizes on audit + test_erosion alone — the smoketest
    dimension is dropped, no smoketest_startup_failed blocker fires, and a
    confident audit-only candidate reaches a dry_run (promotion off)."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = False

    project, version = await _make_project_and_version(db_session)
    await _seed_audit_only_pending_qualification(
        db_session,
        project,
        version,
        audit_logs={
            "audit_score": 1.0,
            "test_erosion": {
                "deleted_test_files": [],
                "assertion_delta": 4,
                "test_count_delta": 2,
            },
        },
    )

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    # No smoketest_startup_failed blocker — the dimension was dropped.
    assert result["status"] == "dry_run"
    assert result["would_promote"] is True
    # Perfect audit + perfect test_erosion renormalize to a 1.0 composite.
    assert result["confidence"] == pytest.approx(1.0)

    await db_session.refresh(version)
    rec = version.last_qualification
    assert rec["status"] == "dry_run"
    assert rec["smoketest_enabled"] is False
    # The smoketest sub-score is reported as 0.0 but carries 0 weight.
    assert rec["sub_scores"]["smoketest"] == 0.0


@pytest.mark.asyncio
async def test_maybe_finalize_smoketest_disabled_audit_only(db_session):
    """Audit H3: maybe_finalize_qualification finalizes a smoketest-disabled
    qualification once the SOLE audit child is terminal — it does not wedge
    waiting for a non-existent smoketest child."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = False

    project, version = await _make_project_and_version(db_session)
    audit_task = await _seed_audit_only_pending_qualification(
        db_session,
        project,
        version,
        audit_status=TaskStatus.COMPLETED,
        audit_logs={
            "audit_score": 1.0,
            "test_erosion": {"deleted_test_files": []},
        },
    )

    result = await rq.maybe_finalize_qualification(db_session, audit_task)
    await db_session.commit()

    # Finalized on the audit child alone — no smoketest child to wait for.
    assert result is not None
    assert result["status"] == "dry_run"

    await db_session.refresh(version)
    assert version.last_qualification["status"] == "dry_run"


@pytest.mark.asyncio
async def test_sweep_smoketest_disabled_audit_only(db_session):
    """Audit H3: the sweep finalizes a stuck smoketest-disabled qualification
    once its sole audit child is terminal — it does not require a smoketest
    child that never existed."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = False

    project, version = await _make_project_and_version(db_session)
    await _seed_audit_only_pending_qualification(
        db_session,
        project,
        version,
        audit_status=TaskStatus.COMPLETED,
        audit_logs={
            "audit_score": 1.0,
            "test_erosion": {"deleted_test_files": []},
        },
    )

    count = await rq.sweep_stuck_qualifications(db_session)

    assert count == 1
    await db_session.refresh(version)
    assert version.last_qualification["status"] == "dry_run"


# ===========================================================================
# Audit H2 — qualification-timeout force-finalize
# ===========================================================================


@pytest.mark.asyncio
async def test_sweep_force_finalizes_timed_out_qualification(db_session):
    """Audit H2: a qualification pending longer than the qualification-timeout
    is force-finalized as a blocked one (blocker=qualification_timeout) EVEN
    when a child is still non-terminal — the escape hatch for a child that
    heartbeats-but-hangs."""
    from datetime import timedelta

    from app.models import Alert
    from app.time_utils import _utcnow
    from sqlmodel import select as _select

    project, version = await _make_project_and_version(db_session)

    # The smoketest child is still IN_PROGRESS — normally the sweep would skip
    # this version. started_at is well past the timeout.
    old_started = (
        _utcnow()
        - timedelta(seconds=rq._MAX_QUALIFICATION_DURATION_SECONDS + 3600)
    ).isoformat()
    await _seed_pending_qualification(
        db_session,
        project,
        version,
        audit_status=TaskStatus.COMPLETED,
        smoke_status=TaskStatus.IN_PROGRESS,  # hung child, never terminal
        started_at=old_started,
    )

    count = await rq.sweep_stuck_qualifications(db_session)

    assert count == 1

    await db_session.refresh(version)
    # Force-finalized as a blocked qualification.
    assert version.last_qualification["status"] == "not_promoted"
    assert version.last_qualification["blocker"] == "qualification_timeout"
    # Version stays open and returns to stabilize for the next strategic cycle.
    assert version.status == "open"
    assert version.phase == "stabilize"

    # An operator-visible Alert was raised.
    alerts = (
        (
            await db_session.execute(
                _select(Alert).where(Alert.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 1
    assert "timed out" in alerts[0].title.lower()


@pytest.mark.asyncio
async def test_sweep_does_not_time_out_a_fresh_qualification(db_session):
    """Audit H2: a qualification still within the timeout window with a
    running child is NOT force-finalized — the timeout is a backstop, not an
    aggressive killer."""
    project, version = await _make_project_and_version(db_session)

    # Fresh started_at (default = now), one child still running.
    await _seed_pending_qualification(
        db_session,
        project,
        version,
        audit_status=TaskStatus.COMPLETED,
        smoke_status=TaskStatus.IN_PROGRESS,
    )

    count = await rq.sweep_stuck_qualifications(db_session)

    assert count == 0
    await db_session.refresh(version)
    # Untouched — still pending, no timeout blocker.
    assert version.last_qualification["status"] == "pending"


@pytest.mark.asyncio
async def test_sweep_timeout_takes_priority_over_terminal_children(db_session):
    """Audit H2: when a qualification is BOTH past the timeout AND has all
    children terminal, the timeout path wins — it is checked first and is
    child-state-independent (a stuck qualification this old should surface as
    a timeout, not silently finalize)."""
    from datetime import timedelta

    from app.time_utils import _utcnow

    project, version = await _make_project_and_version(db_session)

    old_started = (
        _utcnow()
        - timedelta(seconds=rq._MAX_QUALIFICATION_DURATION_SECONDS + 600)
    ).isoformat()
    await _seed_pending_qualification(
        db_session,
        project,
        version,
        audit_status=TaskStatus.COMPLETED,
        smoke_status=TaskStatus.COMPLETED,
        started_at=old_started,
    )

    count = await rq.sweep_stuck_qualifications(db_session)

    assert count == 1
    await db_session.refresh(version)
    assert version.last_qualification["blocker"] == "qualification_timeout"


# ===========================================================================
# Audit H5 — per-version commit isolation in the sweep
# ===========================================================================


@pytest.mark.asyncio
async def test_sweep_commits_each_version_independently(db_session, mocker):
    """Audit H5: the sweep commits after EACH finalized version, so a failure
    finalizing one version cannot roll back another version's already-committed
    finalization.  Here version A finalizes cleanly; version B's finalize is
    forced to raise — A's finalized record must survive."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = False

    # Two independent projects, each with a stuck pending qualification whose
    # children are both terminal.
    project_a, version_a = await _make_project_and_version(db_session)
    audit_a, smoke_a = await _seed_pending_qualification(
        db_session,
        project_a,
        version_a,
        audit_status=TaskStatus.COMPLETED,
        smoke_status=TaskStatus.COMPLETED,
    )
    db_session.add(
        Artifact(
            task_id=audit_a.id,
            artifact_type=ArtifactType.RELEASE_AUDIT_REPORT,
            logs=json.dumps(
                {"audit_score": 1.0, "test_erosion": {"deleted_test_files": []}}
            ),
        )
    )
    db_session.add(
        Artifact(
            task_id=smoke_a.id,
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

    project_b, version_b = await _make_project_and_version(db_session)
    await _seed_pending_qualification(
        db_session,
        project_b,
        version_b,
        audit_status=TaskStatus.COMPLETED,
        smoke_status=TaskStatus.COMPLETED,
    )
    await db_session.commit()

    # Make _finalize_qualification blow up ONLY for version B.  version A
    # finalizes normally; the sweep processes each version in its own
    # try/except + commit, so whichever order the open-versions query returns
    # them in, A's commit lands and B's raise is isolated + rolled back.
    real_finalize = rq._finalize_qualification

    async def _flaky_finalize(session, version):
        if version.id == version_b.id:
            raise RuntimeError("simulated finalize failure for version B")
        return await real_finalize(session, version)

    mocker.patch.object(
        rq, "_finalize_qualification", new=AsyncMock(side_effect=_flaky_finalize)
    )

    # The sweep must not propagate version B's error — it isolates per version.
    count = await rq.sweep_stuck_qualifications(db_session)

    # Exactly one version finalized (A); B's failure was caught and isolated.
    assert count == 1

    # CRITICAL (audit H5): version A's finalization is committed and durable —
    # B's raise did NOT roll it back.
    await db_session.refresh(version_a)
    assert version_a.last_qualification["status"] == "dry_run"

    # Version B stays pending — its finalize failed and rolled back cleanly.
    await db_session.refresh(version_b)
    assert version_b.last_qualification["status"] == "pending"
