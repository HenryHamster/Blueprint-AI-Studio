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
    FAILED-promotion path; and the project_not_found → failure path.  Round 7:
    promote() does NOT commit a caller-supplied unrelated transaction; an
    already-merged PR returning {merged: False} is recorded as success not
    failure (disambiguated via the PR's own merged boolean); the early-return
    failure path does not expire() un-flushed mutations.
  * maybe_finalize_qualification — Round 7: opens its OWN dedicated session, so
    a caller's unrelated dirty work is never committed by promote(); concurrent
    (lock-serialized) finalize calls never double-promote.
  * sweep_stuck_qualifications — defense-in-depth reaper for qualifications
    stuck pending; the qualification-timeout force-finalize (audit H2); the
    per-version session isolation (audit H5).
"""

from __future__ import annotations

import contextlib
import json
import uuid
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func as sa_func
from sqlalchemy import select

from app import release_qualifier as rq
from app import release_version as rv
from app.models import (
    Alert,
    Artifact,
    ArtifactType,
    Project,
    ProjectMessage,
    QualificationVerdict,
    Task,
    TaskStatus,
)

# Fixed test policy for the compute_composite scoring-math tests.  Weights
# mirror promotion_policy.yaml; the threshold is pinned at 0.75 here so the
# threshold-boundary tests stay deterministic regardless of the shipped value.
_POLICY = {
    "weights": {"audit": 0.40, "test_erosion": 0.30, "smoketest": 0.30},
    "threshold": 0.75,
}


def _shared_session_factory(db_session):
    """Return a session-factory that hands back the test's shared ``db_session``.

    Round 7: the release qualifier's finalize→promote chain
    (``maybe_finalize_qualification`` / ``sweep_stuck_qualifications`` →
    ``_finalize_in_dedicated_session``) opens its OWN session via an injected
    factory — in production that is ``AsyncSessionLocal`` (a genuine second DB
    connection), which fully decouples ``promote()``'s commits from the
    completion handler's transaction.

    Under the test harness the SQLite in-memory DB is a single
    ``StaticPool`` connection, so the "dedicated" session must BE the test
    session.  This factory yields ``db_session`` inside an async context
    manager whose ``__aexit__`` is a deliberate no-op — it does NOT close or
    roll back the session, so the test still owns its lifecycle and can run
    assertions after the function under test returns.

    Used as ``session_factory=_shared_session_factory(db_session)``.
    """

    @contextlib.asynccontextmanager
    async def _factory():
        # Yield the shared session; on exit do nothing (the test owns close()).
        yield db_session

    return _factory


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

    def test_parseable_but_unscoreable_audit_forces_zero(self):
        """Malformed findings are blocked the same way as an unusable audit."""
        audit_score, test_erosion = rq._score_audit(
            {
                "audit_score": 0.95,
                "findings": [{"description": "missing severity"}],
                "test_erosion": {
                    "deleted_test_files": [],
                    "assertion_delta": 0,
                    "test_count_delta": 0,
                },
            }
        )
        assert audit_score is None
        result = rq.compute_composite(
            ci_green=True,
            audit_score=audit_score,
            test_erosion=test_erosion,
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


class TestComputeCompositeCiState:
    """ci_state parameter distinguishes confirmed red build from unresolved CI."""

    def test_ci_unresolved_when_state_pending(self):
        """not ci_green + ci_state='pending' → ci_unresolved (not ci_not_green)."""
        result = rq.compute_composite(
            ci_green=False,
            audit_score=1.0,
            test_erosion=1.0,
            smoketest_score=1.0,
            policy=_POLICY,
            ci_state="pending",
        )
        assert result["confidence"] == 0.0
        assert result["promote"] is False
        assert result["blocker"] == "ci_unresolved"

    def test_ci_unresolved_when_state_no_checks(self):
        """not ci_green + ci_state='no_checks' → ci_unresolved."""
        result = rq.compute_composite(
            ci_green=False,
            audit_score=1.0,
            test_erosion=1.0,
            smoketest_score=1.0,
            policy=_POLICY,
            ci_state="no_checks",
        )
        assert result["confidence"] == 0.0
        assert result["promote"] is False
        assert result["blocker"] == "ci_unresolved"

    def test_ci_not_green_only_when_state_red(self):
        """not ci_green + ci_state='red' (default) → ci_not_green (real red build)."""
        result = rq.compute_composite(
            ci_green=False,
            audit_score=1.0,
            test_erosion=1.0,
            smoketest_score=1.0,
            policy=_POLICY,
            ci_state="red",
        )
        assert result["confidence"] == 0.0
        assert result["promote"] is False
        assert result["blocker"] == "ci_not_green"

    def test_ci_state_default_preserves_existing_ci_not_green_behavior(self):
        """ci_state defaults to 'red' so existing call sites are unaffected."""
        result = rq.compute_composite(
            ci_green=False,
            audit_score=1.0,
            test_erosion=1.0,
            smoketest_score=1.0,
            policy=_POLICY,
        )
        assert result["blocker"] == "ci_not_green"

    def test_ci_green_true_ignores_ci_state(self):
        """When ci_green is True, ci_state is irrelevant — no blocker fires."""
        for state in ("green", "red", "pending", "no_checks"):
            result = rq.compute_composite(
                ci_green=True,
                audit_score=1.0,
                test_erosion=1.0,
                smoketest_score=1.0,
                policy=_POLICY,
                ci_state=state,
            )
            assert result["blocker"] is None, (
                f"unexpected blocker for ci_state={state!r}"
            )


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
        renormalized test_erosion weight (0.30/0.70 ≈ 0.4286).

        compute_composite rounds the returned confidence to 4 decimal places
        (see its docstring + the ``round(_clamp01(confidence), 4)`` final
        step), so the expected value is rounded the same way — 0.30/0.70 in
        full precision (0.428571…) differs from the stored 0.4286 by more
        than pytest.approx's default 1e-6 relative tolerance."""
        result = rq.compute_composite(
            ci_green=True,
            audit_score=0.0,
            test_erosion=1.0,
            smoketest_score=None,
            policy=_POLICY,
            smoketest_enabled=False,
        )
        # The proportional split is exact; only the 4-dp rounding is applied.
        assert result["confidence"] == pytest.approx(round(0.30 / 0.70, 4))

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

    ``_load_policy`` now resolves the live runtime_config rubric FIRST (it is
    seeded from the YAML at startup and is admin-tunable), so to exercise the
    YAML-file tier these tests must clear that in-memory override.  The
    runtime_config promotion values are temporarily set to None for the test.
    """
    # Clear the live runtime_config rubric so _load_policy falls through to the
    # YAML-file tier this helper is testing.
    mocker.patch.dict(
        rq._runtime_config,
        {"promotion_threshold": None, "promotion_weights": None},
        clear=False,
    )
    target = tmp_path / "promotion_policy.yaml"
    if content is not None:
        target.write_text(content, encoding="utf-8")
    # else: leave the file absent so read_text raises FileNotFoundError.
    mocker.patch.object(rq, "_POLICY_PATH", target)
    return target


class TestLoadPolicy:
    def test_parses_real_policy_file(self, mocker):
        """The shipped promotion_policy.yaml parses to the documented values.

        Clears the live runtime_config rubric so the YAML-file tier is exercised
        directly (the seeded runtime_config holds the same values, but this test
        is specifically about the on-disk file)."""
        mocker.patch.dict(
            rq._runtime_config,
            {"promotion_threshold": None, "promotion_weights": None},
            clear=False,
        )
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
        assert policy["threshold"] == pytest.approx(0.90)

    def test_fallback_on_missing_file(self, mocker, tmp_path):
        """A missing policy file falls back to hardcoded defaults, no raise."""
        _point_policy_at(mocker, tmp_path, None)
        policy = rq._load_policy()
        assert policy["weights"] == {
            "audit": 0.40,
            "test_erosion": 0.30,
            "smoketest": 0.30,
        }
        assert policy["threshold"] == 0.90

    def test_fallback_on_malformed_yaml(self, mocker, tmp_path):
        """Malformed YAML falls back to defaults instead of raising."""
        _point_policy_at(mocker, tmp_path, "weights: {unterminated: [1, 2")
        policy = rq._load_policy()
        assert policy["threshold"] == 0.90
        assert policy["weights"]["audit"] == 0.40

    def test_fallback_on_missing_weights_key(self, mocker, tmp_path):
        """A policy file with no 'weights' mapping falls back to defaults."""
        # Use a sentinel threshold that differs from the default so the
        # fallback (vs the file's parsed value) is unambiguous.
        _point_policy_at(mocker, tmp_path, "threshold: 0.55\n")
        policy = rq._load_policy()
        assert policy["weights"]["audit"] == 0.40
        assert policy["threshold"] == 0.90

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
        assert policy["threshold"] == 0.90

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
        assert policy["threshold"] == 0.90

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
        assert fresh["threshold"] == 0.90

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

    def test_runtime_config_override_wins_over_yaml(self, mocker, tmp_path):
        """The live runtime_config rubric takes precedence over the YAML file.

        A calibrated threshold/weights set in runtime_config (via the admin
        PATCH) must be what the gate reads, with no redeploy — even when the
        on-disk file says something else."""
        # File says 0.90 / default weights...
        target = tmp_path / "promotion_policy.yaml"
        target.write_text(
            "weights:\n  audit: 0.40\n  test_erosion: 0.30\n"
            "  smoketest: 0.30\nthreshold: 0.90\n",
            encoding="utf-8",
        )
        mocker.patch.object(rq, "_POLICY_PATH", target)
        # ...but runtime_config holds a calibrated 0.75 with custom weights.
        mocker.patch.dict(
            rq._runtime_config,
            {
                "promotion_threshold": 0.75,
                "promotion_weights": {
                    "audit": 0.5,
                    "test_erosion": 0.25,
                    "smoketest": 0.25,
                },
            },
            clear=False,
        )
        policy = rq._load_policy()
        assert policy["threshold"] == pytest.approx(0.75)
        assert policy["weights"]["audit"] == pytest.approx(0.5)

    def test_runtime_config_falls_back_to_yaml_when_unset(self, mocker, tmp_path):
        """When runtime_config has no rubric, _load_policy reads the YAML file."""
        _point_policy_at(
            mocker,
            tmp_path,
            "weights:\n  audit: 0.5\n  test_erosion: 0.25\n"
            "  smoketest: 0.25\nthreshold: 0.8\n",
        )
        # _point_policy_at already cleared the runtime_config rubric.
        policy = rq._load_policy()
        assert policy["threshold"] == pytest.approx(0.8)

    def test_runtime_config_malformed_weights_falls_back(self, mocker, tmp_path):
        """A mis-weighted runtime_config rubric (sum != 1.0) is rejected and the
        YAML/default tier is used instead — a bad in-memory value never reaches
        the gate."""
        target = tmp_path / "promotion_policy.yaml"
        target.write_text(
            "weights:\n  audit: 0.40\n  test_erosion: 0.30\n"
            "  smoketest: 0.30\nthreshold: 0.88\n",
            encoding="utf-8",
        )
        mocker.patch.object(rq, "_POLICY_PATH", target)
        mocker.patch.dict(
            rq._runtime_config,
            {
                "promotion_threshold": 0.5,
                # Sums to 0.3 — mis-weighted, must be rejected.
                "promotion_weights": {
                    "audit": 0.1,
                    "test_erosion": 0.1,
                    "smoketest": 0.1,
                },
            },
            clear=False,
        )
        policy = rq._load_policy()
        # Fell through to the YAML file, not the broken runtime_config value.
        assert policy["threshold"] == pytest.approx(0.88)


# ===========================================================================
# _score_audit / _score_smoketest — artifact → sub-score derivation
# ===========================================================================


class TestScoreAudit:
    def test_clean_audit_report(self):
        """An empty findings list yields derived audit_score=1.0 and 1.0 erosion."""
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
        # derived from findings (empty) → 1.0; reported 0.85 is observability only
        assert audit == pytest.approx(1.0)
        assert erosion == pytest.approx(1.0)

    def test_parseable_but_unscoreable_returns_none_audit_score(self):
        """Findings with no severity/level are treated as an unusable audit signal."""
        report = {
            "audit_score": 0.95,
            "findings": [
                {"description": "Some issue without severity."},
                "plain-text finding with no structured severity",
            ],
            "test_erosion": {
                "deleted_test_files": [],
                "assertion_delta": 0,
                "test_count_delta": 0,
            },
        }
        audit, erosion = rq._score_audit(report)
        assert audit is None
        assert erosion == pytest.approx(1.0)

    def test_findings_deducted_correctly(self):
        """1 high + 1 medium finding → 1.0 - 0.15 - 0.07 = 0.78."""
        report = {
            "audit_score": 0.78,
            "findings": [
                {
                    "severity": "high",
                    "summary": "Token in source.",
                    "file": "app/auth.py",
                    "verification_note": "Read app/auth.py lines 10-30: secret hardcoded in plaintext, reaches external HTTP call.",
                },
                {
                    "severity": "medium",
                    "summary": "Missing null check.",
                    "file": "app/api.py",
                    "verification_note": "Read app/api.py lines 55-70: payload.user may be None, dereferenced without guard.",
                },
            ],
        }
        audit, erosion = rq._score_audit(report)
        assert audit == pytest.approx(0.78)
        assert erosion == pytest.approx(1.0)

    def test_critical_finding_deducted(self):
        """A critical finding deducts 0.30 → 1.0 - 0.30 = 0.70."""
        report = {
            "findings": [
                {
                    "severity": "critical",
                    "summary": "SQL injection.",
                    "file": "app/db.py",
                    "verification_note": "Read app/db.py lines 88-95: user_id from request headers reaches cursor.execute f-string with no parameterisation.",
                },
            ],
        }
        audit, _ = rq._score_audit(report)
        assert audit == pytest.approx(0.70)

    def test_clamped_below_zero(self):
        """Many critical findings clamp to 0.0."""
        report = {
            "findings": [
                {
                    "severity": "critical",
                    "summary": f"Issue {i}",
                    "file": "",
                    "verification_note": f"Read src/module.py line {i * 10 + 1}: confirmed real defect in the diff with no mitigation.",
                }
                for i in range(10)
            ],
        }
        audit, _ = rq._score_audit(report)
        assert audit == pytest.approx(0.0)

    def test_missing_findings_key_scores_1_0(self):
        """A report with no findings key derives score 1.0 (no deductions)."""
        report = {"audit_score": 0.5, "test_erosion": {"deleted_test_files": []}}
        audit, erosion = rq._score_audit(report)
        assert audit == pytest.approx(1.0)
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
            "findings": [],
        }
        audit, erosion = rq._score_audit(report)
        # No findings → derived audit = 1.0
        assert audit == pytest.approx(1.0)
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

    def test_missing_audit_score_no_findings_scores_1_0(self):
        """An absent self-reported audit_score still yields derived 1.0 (no findings)."""
        report = {"test_erosion": {"deleted_test_files": []}}
        audit, erosion = rq._score_audit(report)
        assert audit == pytest.approx(1.0)
        assert erosion == pytest.approx(1.0)

    def test_none_report_returns_none_none(self):
        """A None report (no artifact) yields (None, None)."""
        assert rq._score_audit(None) == (None, None)

    def test_no_erosion_block_assumes_neutral(self):
        """A report with no test_erosion block assumes neutral (1.0)."""
        audit, erosion = rq._score_audit({"audit_score": 0.7, "findings": []})
        assert audit == pytest.approx(1.0)
        assert erosion == pytest.approx(1.0)

    def test_divergence_log_emitted(self, caplog):
        """When derived and reported scores diverge by >0.10 a warning is logged."""
        import logging

        _NOTE = (
            "Read src/module.py lines 5-20: confirmed real defect with no mitigation."
        )
        report = {
            "audit_score": 0.90,  # reported
            # derived = 1.0 - 0.30 - 0.30 = 0.40 → delta 0.50
            "findings": [
                {
                    "severity": "critical",
                    "summary": "Issue A",
                    "file": "",
                    "verification_note": _NOTE,
                },
                {
                    "severity": "critical",
                    "summary": "Issue B",
                    "file": "",
                    "verification_note": _NOTE,
                },
            ],
        }
        with caplog.at_level(logging.WARNING, logger="backend.app.release_qualifier"):
            audit, _ = rq._score_audit(report)
        assert audit == pytest.approx(0.40)
        assert any("audit_score drift" in r.message for r in caplog.records)

    def test_moderate_severity_treated_as_medium(self):
        """'moderate' is an alias for 'medium' (same 0.07 deduction)."""
        report = {
            "findings": [
                {
                    "severity": "moderate",
                    "summary": "Style issue",
                    "file": "",
                    "verification_note": "Read app/utils.py lines 12-20: pattern inconsistent with rest of module, confirmed present in diff.",
                }
            ]
        }
        audit, _ = rq._score_audit(report)
        assert audit == pytest.approx(1.0 - 0.07)

    def test_findings_under_issues_key_applies_deductions(self):
        """When 'findings' is absent, fall back to 'issues' and apply deductions."""
        report = {
            "issues": [
                {
                    "severity": "high",
                    "summary": "Token in source.",
                    "file": "app/auth.py",
                    "verification_note": "Read app/auth.py lines 10-30: API key hardcoded in source, reaches outbound request.",
                },
            ]
        }
        audit, _ = rq._score_audit(report)
        # Should deduct 0.15 for high, not produce 1.0 (fail-open)
        assert audit == pytest.approx(1.0 - 0.15)

    def test_severity_via_level_field_applies_deductions(self):
        """Finding with 'level' instead of 'severity' must apply the correct deduction."""
        report = {
            "findings": [
                {
                    "level": "critical",
                    "summary": "SQL injection",
                    "file": "app/db.py",
                    "verification_note": "Read app/db.py lines 88-95: user input reaches cursor.execute without parameterisation.",
                },
            ]
        }
        audit, _ = rq._score_audit(report)
        assert audit == pytest.approx(1.0 - 0.30)

    def test_unrecognized_severity_applies_nonzero_deduction(self):
        """Off-list severity labels ('blocker', 'warning', 'info', 'unknown') must not score 0."""
        _NOTE = "Read src/module.py lines 5-20: confirmed real defect with no mitigation in the diff."
        for sev, expected_deduction, needs_note in [
            ("blocker", 0.30, True),  # in _SEVERITIES_REQUIRING_VERIFICATION
            ("warning", 0.07, True),  # in _SEVERITIES_REQUIRING_VERIFICATION
            ("info", 0.02, False),  # not in set; no verification required
            (
                "custom_label",
                rq._AUDIT_DEDUCTION_UNKNOWN,
                False,
            ),  # unknown; no verification required
        ]:
            finding: dict = {"severity": sev, "summary": "Issue", "file": ""}
            if needs_note:
                finding["verification_note"] = _NOTE
            report = {"findings": [finding]}
            audit, _ = rq._score_audit(report)
            assert audit == pytest.approx(1.0 - expected_deduction), (
                f"severity={sev!r} should deduct {expected_deduction} but got {1.0 - audit:.4f}"
            )

    def test_score_audit_and_extract_findings_agree_on_source(self):
        """Both _score_audit and _extract_audit_findings must read from the same key/field.

        Regression guard: a report whose findings live under 'issues' with 'level'
        severity must produce a sub-1.0 score AND non-empty findings — not 1.0 / [].
        """
        report = {
            "issues": [
                {
                    "level": "high",
                    "summary": "Token baked into source.",
                    "file": "app/secret.py",
                    "verification_note": "Read app/secret.py lines 5-15: API key hardcoded in module-level constant, reaches outbound HTTP call.",
                },
            ]
        }
        audit, _ = rq._score_audit(report)
        findings = rq._extract_audit_findings(report)
        # Gate must not pass 1.0 while findings are surfaced
        assert audit < 1.0, "score should reflect the finding, not fail open to 1.0"
        assert len(findings) > 0, "findings should be surfaced for the same report"
        assert findings[0]["severity"] == "high"


class TestScoreAuditVerification:
    """_score_audit must skip deductions for high/medium/critical findings
    that lack a substantive verification_note (≥ 20 non-whitespace chars)."""

    def test_unverified_high_finding_not_deducted(self):
        """A high finding without verification_note is excluded → score stays 1.0."""
        report = {
            "findings": [
                {
                    "severity": "high",
                    "summary": "Potential issue.",
                    "file": "app/foo.py",
                },
            ]
        }
        audit, _ = rq._score_audit(report)
        assert audit == pytest.approx(1.0)

    def test_unverified_medium_finding_not_deducted(self):
        """A medium finding without verification_note is excluded → score stays 1.0."""
        report = {
            "findings": [
                {
                    "severity": "medium",
                    "summary": "Missing guard.",
                    "file": "app/bar.py",
                },
            ]
        }
        audit, _ = rq._score_audit(report)
        assert audit == pytest.approx(1.0)

    def test_low_finding_always_deducted_without_verification_note(self):
        """Low findings do not require verification_note — deduction applies."""
        report = {
            "findings": [
                {"severity": "low", "summary": "Nit: inconsistent style.", "file": ""},
            ]
        }
        audit, _ = rq._score_audit(report)
        assert audit == pytest.approx(1.0 - 0.02)

    def test_short_verification_note_not_counted(self):
        """A verification_note with fewer than 20 non-whitespace chars is treated as absent."""
        report = {
            "findings": [
                {
                    "severity": "critical",
                    "summary": "SQL injection.",
                    "file": "app/db.py",
                    "verification_note": "too short",  # only 9 non-whitespace chars
                },
            ]
        }
        audit, _ = rq._score_audit(report)
        assert audit == pytest.approx(1.0)

    def test_verified_critical_finding_deducted(self):
        """A critical finding WITH a substantive verification_note is deducted normally."""
        report = {
            "findings": [
                {
                    "severity": "critical",
                    "summary": "SQL injection.",
                    "file": "app/db.py",
                    "verification_note": "Read app/db.py lines 88-95: user input from request body reaches cursor.execute without parameterisation.",
                },
            ]
        }
        audit, _ = rq._score_audit(report)
        assert audit == pytest.approx(1.0 - 0.30)

    def test_mixed_verified_and_unverified_findings(self):
        """Only verified findings count — one verified high + one unverified critical."""
        report = {
            "findings": [
                {
                    "severity": "high",
                    "summary": "Real issue.",
                    "file": "app/auth.py",
                    "verification_note": "Read app/auth.py lines 10-30: API key hardcoded in source, reaches outbound request.",
                },
                {
                    "severity": "critical",
                    "summary": "Speculative injection.",
                    "file": "app/db.py",
                    # no verification_note — should be excluded
                },
            ]
        }
        audit, _ = rq._score_audit(report)
        # Only the verified high finding deducts: 1.0 - 0.15 = 0.85
        assert audit == pytest.approx(1.0 - 0.15)

    def test_empty_string_verification_note_not_counted(self):
        """An empty verification_note is treated the same as absent."""
        report = {
            "findings": [
                {
                    "severity": "high",
                    "summary": "Missing guard.",
                    "file": "app/api.py",
                    "verification_note": "",
                },
            ]
        }
        audit, _ = rq._score_audit(report)
        assert audit == pytest.approx(1.0)

    def test_all_findings_unverified_audit_missing_reason_fires(self):
        """When _score_audit returns 1.0 due to all-unverified findings,
        _audit_missing_reason must return 'all_findings_unverified'."""
        report = {
            "findings": [
                {
                    "severity": "high",
                    "summary": "Potential issue.",
                    "file": "app/foo.py",
                },
            ]
        }
        audit, _ = rq._score_audit(report)
        assert audit == pytest.approx(1.0), "unverified high must not be deducted"
        assert rq._audit_missing_reason(report) == "all_findings_unverified"


class TestScoreAuditAuthorizedDivergence:
    """An authorized, registry-listed Alembic revision-identity divergence is
    owned by the deterministic migration-identity gate, so the release audit
    scorer must NOT deduct for it (no double-counting). Unrelated findings and
    genuine migration-body defects still deduct."""

    _AUTH_VNOTE = (
        "Read git diff main...candidate: the old 0143 migration is replaced by a "
        "new 0143 plus a renumbered 0144 — revision-identity collision vs deployed main."
    )

    def test_authorized_renumber_finding_not_deducted(self):
        """A medium finding whose subject is the authorized 0143 renumber does
        not deduct — the gate owns it."""
        report = {
            "findings": [
                {
                    "severity": "medium",
                    "summary": "The 0143 back-merge renumber reuses revision id 0143 and renumbers 0144.",
                    "file": "backend/alembic/versions/0143_add_compute_job_persistence.py",
                    "verification_note": self._AUTH_VNOTE,
                },
            ]
        }
        audit, _ = rq._score_audit(report)
        assert audit == pytest.approx(1.0), "authorized revision-identity divergence must not deduct"

    def test_authorized_divergence_excluded_but_unrelated_finding_still_deducts(self):
        """The authorized renumber is delegated to the gate; an unrelated medium
        finding in the same report still deducts normally."""
        report = {
            "findings": [
                {
                    "severity": "medium",
                    "summary": "0143 revision-identity collision: revision id reused on the back-merge.",
                    "file": "backend/alembic/versions/0143_add_compute_job_persistence.py",
                    "verification_note": self._AUTH_VNOTE,
                },
                {
                    "severity": "medium",
                    "summary": "Missing null check.",
                    "file": "app/api.py",
                    "verification_note": "Read app/api.py lines 55-70: payload.user may be None, dereferenced without guard.",
                },
            ]
        }
        audit, _ = rq._score_audit(report)
        # Only the unrelated medium deducts: 1.0 - 0.07
        assert audit == pytest.approx(1.0 - 0.07)

    def test_genuine_migration_body_defect_still_deducts(self):
        """A real migration-BODY defect on the same 0143 file (missing downgrade)
        is NOT a revision-identity concern and must still deduct."""
        report = {
            "findings": [
                {
                    "severity": "high",
                    "summary": "0143 add_compute_job_persistence has no downgrade — irreversible, data-loss on rollback.",
                    "file": "backend/alembic/versions/0143_add_compute_job_persistence.py",
                    "verification_note": "Read 0143: downgrade() is a bare pass so the computejob table is never dropped — destructive rollback risk.",
                },
            ]
        }
        audit, _ = rq._score_audit(report)
        assert audit == pytest.approx(1.0 - 0.15), "migration-body defect must still deduct"

    def test_helper_matches_authorized_and_rejects_unrelated(self):
        """_finding_subject_is_authorized_divergence: matches a listed-revision
        renumber finding, rejects an unrelated finding and a same-file body defect."""
        auth = {
            "severity": "medium",
            "summary": "0143 revision-identity collision (renumber).",
            "verification_note": self._AUTH_VNOTE,
        }
        unrelated = {"severity": "medium", "summary": "Missing null check in app/api.py."}
        body_defect = {
            "severity": "high",
            "summary": "0143 add_compute_job_persistence has no downgrade().",
            "verification_note": "downgrade() is a bare pass — destructive.",
        }
        assert rq._finding_subject_is_authorized_divergence(auth) is True
        assert rq._finding_subject_is_authorized_divergence(unrelated) is False
        assert rq._finding_subject_is_authorized_divergence(body_defect) is False


class TestScoreAuditComputedErosion:
    """_score_audit prefers computed_test_erosion (authoritative, deterministic
    from git diff) over the LLM's self-reported test_erosion when the key is present."""

    def test_computed_erosion_authoritative_over_llm(self):
        """computed_test_erosion with a deleted file → None even when LLM omits it."""
        report = {
            "audit_score": 0.9,
            "computed_test_erosion": {
                "deleted_test_files": ["testing/test_gone.py"],
                "assertion_delta": 0,
                "test_count_delta": 0,
            },
            "test_erosion": {
                "deleted_test_files": [],  # LLM missed the deletion
                "assertion_delta": 0,
                "test_count_delta": 0,
            },
            "findings": [],
        }
        _, erosion = rq._score_audit(report)
        assert erosion is None

    def test_llm_reports_deletion_but_computed_does_not(self):
        """computed wins: no deletions → score=1.0 even when LLM hallucinates one."""
        report = {
            "computed_test_erosion": {
                "deleted_test_files": [],
                "assertion_delta": 0,
                "test_count_delta": 0,
            },
            "test_erosion": {
                "deleted_test_files": ["testing/test_hallucinated.py"],
                "assertion_delta": 0,
                "test_count_delta": 0,
            },
            "findings": [],
        }
        _, erosion = rq._score_audit(report)
        assert erosion == pytest.approx(1.0)

    def test_computed_assertion_delta_penalizes(self):
        """Negative assertion_delta in computed_test_erosion yields a penalty."""
        report = {
            "computed_test_erosion": {
                "deleted_test_files": [],
                "assertion_delta": -10,
                "test_count_delta": 0,
            },
            "findings": [],
        }
        _, erosion = rq._score_audit(report)
        assert erosion is not None
        assert erosion < 1.0

    def test_fallback_to_llm_when_no_computed_key(self):
        """When computed_test_erosion is absent, existing test_erosion logic is unchanged."""
        report = {
            "test_erosion": {
                "deleted_test_files": ["testing/test_gone.py"],
                "assertion_delta": 0,
                "test_count_delta": 0,
            },
            "findings": [],
        }
        _, erosion = rq._score_audit(report)
        assert erosion is None

    def test_divergence_warning_logged(self, caplog):
        """When computed and LLM deleted_test_files disagree, a warning is logged."""
        import logging

        report = {
            "computed_test_erosion": {
                "deleted_test_files": ["testing/test_gone.py"],
                "assertion_delta": 0,
                "test_count_delta": 0,
            },
            "test_erosion": {
                "deleted_test_files": [],  # LLM missed it
                "assertion_delta": 0,
                "test_count_delta": 0,
            },
            "findings": [],
        }
        with caplog.at_level(logging.WARNING, logger="backend.app.release_qualifier"):
            rq._score_audit(report)
        assert any("test_erosion divergence" in r.message for r in caplog.records)


class TestAuditMissingReason:
    """_audit_missing_reason returns a structured sub-status string for parseable
    reports that yield no scoring signal, and None when the report is scoreable."""

    def test_none_report_returns_none(self):
        assert rq._audit_missing_reason(None) is None

    def test_non_dict_returns_none(self):
        assert rq._audit_missing_reason("not-a-dict") is None  # type: ignore[arg-type]

    def test_no_findings_key(self):
        """A report with neither 'findings' nor 'issues' key → 'no_findings_key'."""
        report: dict = {"audit_score": 0.8}
        assert rq._audit_missing_reason(report) == "no_findings_key"

    def test_empty_findings_list_with_score(self):
        """findings=[], audit_score present → 'empty_findings_list'."""
        report = {"findings": [], "audit_score": 0.9}
        assert rq._audit_missing_reason(report) == "empty_findings_list"

    def test_audit_score_null_no_findings(self):
        """findings=[], no self-reported audit_score → 'audit_score_null_no_findings'."""
        report: dict = {"findings": []}
        assert rq._audit_missing_reason(report) == "audit_score_null_no_findings"

    def test_parseable_but_unscoreable(self):
        """findings list with entries that have no severity/level → 'parseable_but_unscoreable'."""
        report = {"findings": [{"description": "some issue"}, {"title": "another"}]}
        assert rq._audit_missing_reason(report) == "parseable_but_unscoreable"

    def test_scoreable_report_returns_none(self):
        """A report with a verified high finding → None (genuinely scoreable)."""
        report = {
            "findings": [
                {
                    "severity": "high",
                    "summary": "Token baked into image.",
                    "verification_note": "Read infra/packer/agent.pkr.hcl lines 20-35: API token hardcoded in provisioner.",
                }
            ],
            "audit_score": 0.85,
        }
        assert rq._audit_missing_reason(report) is None

    def test_issues_key_fallback_empty(self):
        """'issues' key with empty list and no audit_score → 'audit_score_null_no_findings'."""
        report: dict = {"issues": []}
        assert rq._audit_missing_reason(report) == "audit_score_null_no_findings"

    def test_issues_key_with_severity_returns_none(self):
        """Scoreable findings under 'issues' key with verified finding → None."""
        report = {
            "issues": [
                {
                    "severity": "medium",
                    "summary": "dep pinning risk",
                    "verification_note": "Read requirements.txt and pyproject.toml: unpinned transitive deps confirmed.",
                }
            ]
        }
        assert rq._audit_missing_reason(report) is None

    def test_all_findings_unverified_single_high(self):
        """Single unverified high finding → 'all_findings_unverified'."""
        report = {
            "findings": [{"severity": "high", "summary": "Potential issue."}],
        }
        assert rq._audit_missing_reason(report) == "all_findings_unverified"

    def test_all_findings_unverified_multiple_mixed_severity(self):
        """Multiple unverified high/medium/critical findings → 'all_findings_unverified'."""
        report = {
            "findings": [
                {"severity": "critical", "summary": "SQL injection."},
                {"severity": "high", "summary": "Leaked secret."},
                {"severity": "medium", "summary": "Weak cipher."},
            ]
        }
        assert rq._audit_missing_reason(report) == "all_findings_unverified"

    def test_one_verified_one_unverified_returns_none(self):
        """One verified high + one unverified medium → None (scoreable)."""
        report = {
            "findings": [
                {
                    "severity": "high",
                    "summary": "Hardcoded credential.",
                    "verification_note": "Read app/config.py lines 10-25: API key hardcoded in module-level constant.",
                },
                {
                    "severity": "medium",
                    "summary": "Missing rate limit.",
                    "file": "app/api.py",
                },
            ]
        }
        assert rq._audit_missing_reason(report) is None

    def test_all_findings_unverified_only_low_findings_returns_none(self):
        """Only low/info findings (not in _SEVERITIES_REQUIRING_VERIFICATION) → None."""
        report = {
            "findings": [
                {"severity": "low", "summary": "Minor style nit."},
                {"severity": "info", "summary": "Informational note."},
            ]
        }
        # No higher-severity findings to check → does not fire the new reason
        assert rq._audit_missing_reason(report) is None


class TestExtractAuditFindings:
    """_extract_audit_findings carries the findings (not just the score) into
    last_qualification so the strategic agent can target them — see the
    findings → strategic-payload wiring."""

    def test_none_report_returns_empty(self):
        assert rq._extract_audit_findings(None) == []

    def test_no_findings_key_returns_empty(self):
        assert rq._extract_audit_findings({"audit_score": 0.8}) == []

    def test_dict_findings_extracted_with_severity_and_summary(self):
        report = {
            "audit_score": 0.74,
            "findings": [
                # High without verification_note: excluded by the scoring gate.
                {"severity": "high", "summary": "Token baked into VM image."},
                # Low never requires verification.
                {"severity": "low", "description": "Private import."},
            ],
        }
        out = rq._extract_audit_findings(report)
        assert out == [
            {"severity": "low", "summary": "Private import."},
        ]

    def test_sorted_most_severe_first(self):
        report = {
            "findings": [
                {"severity": "low", "summary": "low one"},
                # High and medium lack verification_note → excluded by gate.
                {"severity": "high", "summary": "high one"},
                {"severity": "medium", "summary": "medium one"},
            ]
        }
        out = rq._extract_audit_findings(report)
        # Only the low finding survives the verification gate.
        assert [f["severity"] for f in out] == ["low"]

    def test_issues_key_fallback(self):
        """Some audits key the list as 'issues' rather than 'findings'.
        Medium without verification_note → excluded by the scoring gate."""
        report = {"issues": [{"level": "medium", "title": "an issue"}]}
        out = rq._extract_audit_findings(report)
        assert out == []

    def test_unverified_high_finding_excluded(self):
        """An unsubstantiated high finding must NOT appear in the carried list.

        Regression guard: the scoring gate (_finding_is_verified) must be
        applied inside _extract_audit_findings so unverified findings that
        do not affect the audit sub-score are not surfaced as actionable blockers.
        """
        report = {
            "findings": [
                {"severity": "high", "summary": "Speculative SQL injection."},
            ]
        }
        out = rq._extract_audit_findings(report)
        assert out == [], (
            "unverified high finding must be excluded from carried findings"
        )

    def test_verified_high_finding_included(self):
        """A high finding with a substantive verification_note IS carried."""
        report = {
            "findings": [
                {
                    "severity": "high",
                    "summary": "Token baked into image.",
                    "verification_note": (
                        "Read infra/packer/agent.pkr.hcl lines 20-35: "
                        "API token hardcoded in provisioner shell command."
                    ),
                }
            ]
        }
        out = rq._extract_audit_findings(report)
        assert out == [{"severity": "high", "summary": "Token baked into image."}]

    def test_gate_agrees_with_score_audit_for_unverified(self):
        """When _score_audit treats a finding as undeducted (unverified),
        _extract_audit_findings must also exclude it."""
        report = {
            "findings": [
                # Medium without verification_note: _score_audit skips deduction.
                {"severity": "medium", "summary": "Row-vs-ORM false positive."},
                # Low always counted by both functions.
                {"severity": "low", "summary": "Minor style issue."},
            ]
        }
        # _score_audit should NOT deduct for the medium finding → score stays 1.0.
        audit_score, _ = rq._score_audit(report)
        assert audit_score == pytest.approx(1.0 - rq._AUDIT_DEDUCTIONS["low"]), (
            "_score_audit should only deduct for the verified low finding"
        )
        # _extract_audit_findings must agree: medium excluded, low present.
        out = rq._extract_audit_findings(report)
        severities = [f["severity"] for f in out]
        assert "medium" not in severities, (
            "unverified medium finding must not appear in carried findings"
        )
        assert "low" in severities, "low finding must be carried"

    def test_caps_count_and_truncates_summary(self):
        report = {
            "findings": [
                {"severity": "low", "summary": "x" * 500}
                for _ in range(rq._MAX_CARRIED_FINDINGS + 5)
            ]
        }
        out = rq._extract_audit_findings(report)
        assert len(out) == rq._MAX_CARRIED_FINDINGS
        assert all(len(f["summary"]) <= rq._FINDING_SUMMARY_MAX for f in out)

    def test_string_findings_and_blank_summaries_handled(self):
        report = {"findings": ["a plain string", {"severity": "high", "summary": ""}]}
        out = rq._extract_audit_findings(report)
        # The blank-summary finding is dropped; the string one is kept.
        assert out == [{"severity": "unknown", "summary": "a plain string"}]

    def test_blocker_warning_severities_sort_correctly_and_survive_cap(self):
        """Blocker and warning severities must sort before lower-severity findings.

        Regression guard: blocker (and warning) severities were missing from
        _SEVERITY_RANK, causing them to sort last instead of first, and thus
        be dropped by _MAX_CARRIED_FINDINGS cap before less-severe findings.
        """
        # Create a report with many low findings to trigger the cap, plus
        # one blocker and one warning to verify they sort ahead and survive.
        report = {
            "findings": [
                {"severity": "low", "summary": f"low finding {i}"}
                for i in range(rq._MAX_CARRIED_FINDINGS)
            ]
            + [
                {
                    "severity": "blocker",
                    "summary": "Critical blocker that must be fixed",
                    "verification_note": "Read src/main.py lines 1-10: confirmed.",
                },
                {
                    "severity": "warning",
                    "summary": "Warning that should be visible",
                    "verification_note": "Read src/config.py lines 20-30: confirmed.",
                },
            ]
        }
        out = rq._extract_audit_findings(report)
        severities = [f["severity"] for f in out]

        # Blocker should be first, warning should appear before any low findings
        assert severities[0] == "blocker", (
            "blocker finding must sort first (most severe)"
        )
        assert "warning" in severities, (
            "warning finding must survive the cap and appear in carried findings"
        )
        blocker_idx = severities.index("blocker")
        warning_idx = severities.index("warning")
        low_indices = [i for i, s in enumerate(severities) if s == "low"]
        assert blocker_idx < min(low_indices), (
            "blocker must sort before all low findings"
        )
        assert warning_idx < min(low_indices), (
            "warning must sort before all low findings"
        )


class TestExtractExcludedAuditFindings:
    """_extract_excluded_audit_findings is the inverse of _extract_audit_findings:
    it carries the higher-severity findings the verification gate DROPPED from
    scoring, marked unverified, so an inflated audit sub-score is rendered
    honestly instead of reading as a clean audit."""

    def test_none_report_returns_empty(self):
        assert rq._extract_excluded_audit_findings(None) == []

    def test_no_findings_key_returns_empty(self):
        assert rq._extract_excluded_audit_findings({"audit_score": 0.9}) == []

    def test_unverified_high_finding_carried_marked_unverified(self):
        """A high finding lacking a verification_note is excluded from scoring
        AND from audit_findings — but IS carried here, marked unverified."""
        report = {
            "audit_score": 0.95,
            "findings": [
                {"severity": "high", "summary": "Speculative SQL injection."},
            ],
        }
        out = rq._extract_excluded_audit_findings(report)
        assert out == [
            {
                "severity": "high",
                "summary": "Speculative SQL injection.",
                "unverified": "true",
            }
        ]
        # And it must NOT appear in the verified-findings list.
        assert rq._extract_audit_findings(report) == []

    def test_verified_high_finding_not_carried(self):
        """A high finding WITH a substantive verification_note is scored (carried
        by _extract_audit_findings) and so must NOT appear in the excluded list."""
        report = {
            "findings": [
                {
                    "severity": "high",
                    "summary": "Token baked into image.",
                    "verification_note": (
                        "Read infra/packer/agent.pkr.hcl lines 20-35: "
                        "API token hardcoded in provisioner shell command."
                    ),
                }
            ]
        }
        assert rq._extract_excluded_audit_findings(report) == []

    def test_low_severity_never_excluded(self):
        """Low/info severities never require verification, so they are never in
        the excluded list even without a verification_note."""
        report = {"findings": [{"severity": "low", "summary": "Minor style nit."}]}
        assert rq._extract_excluded_audit_findings(report) == []

    def test_all_verified_returns_empty(self):
        report = {
            "findings": [
                {
                    "severity": "medium",
                    "summary": "Real medium issue.",
                    "verification_note": "Read app/foo.py lines 1-20: confirmed.",
                },
                {"severity": "low", "summary": "Minor nit."},
            ]
        }
        assert rq._extract_excluded_audit_findings(report) == []

    def test_issues_key_fallback(self):
        report = {"issues": [{"level": "medium", "title": "an unverified issue"}]}
        out = rq._extract_excluded_audit_findings(report)
        assert out == [
            {
                "severity": "medium",
                "summary": "an unverified issue",
                "unverified": "true",
            }
        ]

    def test_sorted_most_severe_first(self):
        report = {
            "findings": [
                {"severity": "medium", "summary": "medium one"},
                {"severity": "critical", "summary": "critical one"},
                {"severity": "high", "summary": "high one"},
            ]
        }
        out = rq._extract_excluded_audit_findings(report)
        assert [f["severity"] for f in out] == ["critical", "high", "medium"]

    def test_caps_count(self):
        report = {
            "findings": [
                {"severity": "high", "summary": f"high finding {i}"}
                for i in range(rq._MAX_CARRIED_FINDINGS + 5)
            ]
        }
        out = rq._extract_excluded_audit_findings(report)
        assert len(out) == rq._MAX_CARRIED_FINDINGS
        assert all(len(f["summary"]) <= rq._FINDING_SUMMARY_MAX for f in out)

    def test_reconciliation_count_matches_excluded_list_length(self):
        """The excluded list length must agree with the reconciliation count when
        none are truncated by the cap."""
        report = {
            "audit_score": 0.9,
            "findings": [
                {"severity": "high", "summary": "unverified high"},
                {"severity": "medium", "summary": "unverified medium"},
                {"severity": "low", "summary": "low nit"},
            ],
        }
        out = rq._extract_excluded_audit_findings(report)
        recon = rq._compute_audit_reconciliation(report)
        assert len(out) == recon["excluded_findings_count"] == 2


class TestComputeAuditReconciliation:
    """_compute_audit_reconciliation surfaces divergence between the model's
    self-reported audit_score and the deterministic findings-derived score,
    and counts findings excluded for lacking a substantive verification_note."""

    def test_none_report_returns_empty_structure(self):
        out = rq._compute_audit_reconciliation(None)
        assert out == {
            "model_reported_score": None,
            "findings_derived_score": None,
            "score_delta": None,
            "excluded_findings_count": 0,
        }

    def test_model_score_only_no_findings(self):
        """When findings key is absent, findings_derived_score and delta are None."""
        out = rq._compute_audit_reconciliation({"audit_score": 0.8})
        assert out["model_reported_score"] == pytest.approx(0.8)
        assert out["findings_derived_score"] is None
        assert out["score_delta"] is None
        assert out["excluded_findings_count"] == 0

    def test_agreement_yields_small_delta(self):
        """No findings → derived=1.0; model-reported=1.0 → delta=0."""
        out = rq._compute_audit_reconciliation({"audit_score": 1.0, "findings": []})
        assert out["model_reported_score"] == pytest.approx(1.0)
        assert out["findings_derived_score"] == pytest.approx(1.0)
        assert out["score_delta"] == pytest.approx(0.0)

    def test_divergence_delta_populated(self):
        """A high finding (verified) reduces derived score; delta reflects gap."""
        report = {
            "audit_score": 1.0,
            "findings": [
                {
                    "severity": "high",
                    "summary": "leaked secret",
                    "verification_note": "confirmed in full repo scan with evidence",
                }
            ],
        }
        out = rq._compute_audit_reconciliation(report)
        # findings-derived = 1.0 - 0.15 = 0.85; model_reported = 1.0
        assert out["findings_derived_score"] == pytest.approx(0.85)
        assert out["score_delta"] == pytest.approx(0.15)
        assert out["excluded_findings_count"] == 0

    def test_excluded_findings_counted_for_missing_note(self):
        """High finding without a substantive note is excluded and counted."""
        report = {
            "audit_score": 0.9,
            "findings": [
                {"severity": "high", "summary": "risk", "verification_note": ""}
            ],
        }
        out = rq._compute_audit_reconciliation(report)
        assert out["excluded_findings_count"] == 1
        # Not deducted, so derived stays 1.0
        assert out["findings_derived_score"] == pytest.approx(1.0)

    def test_low_finding_never_excluded(self):
        """Low-severity findings are always counted without needing a note."""
        report = {
            "findings": [{"severity": "low", "summary": "minor"}],
        }
        out = rq._compute_audit_reconciliation(report)
        assert out["excluded_findings_count"] == 0
        assert out["findings_derived_score"] == pytest.approx(0.98)  # 1.0 - 0.02

    def test_excluded_findings_count_coincides_with_all_findings_unverified(self):
        """When all higher-severity findings are unverified, excluded_findings_count > 0
        and _audit_missing_reason returns 'all_findings_unverified'."""
        report = {
            "audit_score": 0.9,
            "findings": [
                {"severity": "high", "summary": "Leaked token.", "file": "app/auth.py"},
                {
                    "severity": "medium",
                    "summary": "Weak cipher.",
                    "file": "app/crypto.py",
                },
            ],
        }
        out = rq._compute_audit_reconciliation(report)
        assert out["excluded_findings_count"] == 2
        assert rq._audit_missing_reason(report) == "all_findings_unverified"


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


class TestExtractTestErosionDetail:
    def test_deleted_test_file_present(self):
        """A deleted test file is carried as actionable detail."""
        report = {
            "test_erosion": {
                "deleted_test_files": ["testing/test_foo.py"],
                "test_count_delta": 0,
                "assertion_delta": 0,
            }
        }
        detail = rq._extract_test_erosion_detail(report)
        assert detail == {"deleted_test_files": ["testing/test_foo.py"]}

    def test_negative_deltas_carried(self):
        """Net-negative test/assertion deltas are carried; zero/positive ignored."""
        report = {
            "test_erosion": {
                "deleted_test_files": [],
                "test_count_delta": -2,
                "assertion_delta": -5,
            }
        }
        detail = rq._extract_test_erosion_detail(report)
        assert detail == {"test_count_delta": -2, "assertion_delta": -5}

    def test_clean_erosion_returns_none(self):
        """No deletions and no negative delta yields None (nothing to carry)."""
        report = {
            "test_erosion": {
                "deleted_test_files": [],
                "test_count_delta": 3,
                "assertion_delta": 10,
            }
        }
        assert rq._extract_test_erosion_detail(report) is None

    def test_computed_preferred_over_llm(self):
        """computed_test_erosion is authoritative over the LLM's test_erosion."""
        report = {
            "computed_test_erosion": {"deleted_test_files": ["testing/test_real.py"]},
            "test_erosion": {"deleted_test_files": ["testing/test_hallucinated.py"]},
        }
        detail = rq._extract_test_erosion_detail(report)
        assert detail == {"deleted_test_files": ["testing/test_real.py"]}

    def test_deleted_files_capped(self):
        """Deleted-file list is capped with a truncation marker."""
        files = [f"testing/test_{i}.py" for i in range(12)]
        report = {"test_erosion": {"deleted_test_files": files}}
        detail = rq._extract_test_erosion_detail(report)
        assert len(detail["deleted_test_files"]) == rq._MAX_DELETED_TEST_FILES
        assert detail["deleted_test_files_truncated"] == 12 - rq._MAX_DELETED_TEST_FILES

    def test_non_dict_returns_none(self):
        assert rq._extract_test_erosion_detail(None) is None
        assert rq._extract_test_erosion_detail("nope") is None
        assert rq._extract_test_erosion_detail({"test_erosion": "bad"}) is None


class TestExtractSmoketestDetail:
    def test_startup_suite_failure(self):
        """A failed startup suite is named explicitly."""
        result = {
            "passed": False,
            "tests": [{"name": "a", "status": "pass"}],
            "suites": {"startup": {"passed": False}},
        }
        detail = rq._extract_smoketest_detail(result)
        assert detail["startup_failed"] is True
        assert detail["failed_suite"] == "startup"

    def test_failed_named_tests(self):
        """Failing tests are carried by name with pass/total counts."""
        result = {
            "passed": False,
            "tests": [
                {"name": "a", "status": "pass"},
                {"name": "b", "status": "fail"},
                {"name": "c", "status": "fail"},
            ],
            "suites": {"startup": {"passed": True}},
        }
        detail = rq._extract_smoketest_detail(result)
        assert detail["failed_tests"] == ["b", "c"]
        assert detail["passed"] == 1
        assert detail["total"] == 3

    def test_failed_tests_capped(self):
        """The failing-test list is capped with a truncation marker."""
        tests = [{"name": f"t{i}", "status": "fail"} for i in range(12)]
        result = {"tests": tests, "suites": {"startup": {"passed": True}}}
        detail = rq._extract_smoketest_detail(result)
        assert len(detail["failed_tests"]) == rq._MAX_FAILED_SMOKE_TESTS
        assert detail["failed_tests_truncated"] == 12 - rq._MAX_FAILED_SMOKE_TESTS

    def test_all_pass_returns_none(self):
        """A clean smoketest yields None (nothing to carry)."""
        result = {
            "passed": True,
            "tests": [{"name": "a", "status": "pass"}],
            "suites": {"startup": {"passed": True}},
        }
        assert rq._extract_smoketest_detail(result) is None

    def test_non_dict_returns_none(self):
        assert rq._extract_smoketest_detail(None) is None
        assert rq._extract_smoketest_detail("nope") is None


# ===========================================================================
# _classify_check_runs — CI state mapping
# ===========================================================================


class TestClassifyCheckRuns:
    def test_none_is_pending(self):
        assert rq._classify_check_runs(None) == rq._CI_PENDING

    def test_non_dict_is_pending(self):
        assert rq._classify_check_runs("bad") == rq._CI_PENDING  # type: ignore[arg-type]

    def test_no_checks_status(self):
        assert rq._classify_check_runs({"status": "no_checks"}) == rq._CI_NO_CHECKS

    def test_completed_success_is_green(self):
        assert (
            rq._classify_check_runs({"status": "completed", "conclusion": "success"})
            == rq._CI_GREEN
        )

    def test_completed_failure_is_red(self):
        assert (
            rq._classify_check_runs({"status": "completed", "conclusion": "failure"})
            == rq._CI_RED
        )

    def test_completed_neutral_conclusion_is_pending(self):
        assert (
            rq._classify_check_runs({"status": "completed", "conclusion": "neutral"})
            == rq._CI_PENDING
        )

    def test_in_progress_is_pending(self):
        assert rq._classify_check_runs({"status": "in_progress"}) == rq._CI_PENDING

    def test_conclusion_without_status_is_pending_not_red(self):
        """A conclusion with NO status falls through to the safe default
        (pending), never silently RED/GREEN. This partial shape is never
        produced by github_checks.get_check_runs — which always emits a
        "completed" status alongside a "failure"/"success" conclusion — so the
        defensive fall-through is correct, not a misroute. (The old CI-red
        run_qualification test relied on this exact shape and so passed for the
        wrong reason; it now sends the faithful completed+failure shape.)"""
        assert rq._classify_check_runs({"conclusion": "failure"}) == rq._CI_PENDING
        assert rq._classify_check_runs({"conclusion": "success"}) == rq._CI_PENDING


# ===========================================================================
# _resolve_candidate_ci — qualifier-scoped strict CI resolver
# ===========================================================================


@pytest.mark.asyncio
async def test_resolve_candidate_ci_no_sha_returns_pending(mocker):
    """Missing candidate SHA → pending state without any API call."""
    result = await rq._resolve_candidate_ci("tok", "owner", "repo", "")
    assert result["ci_green"] is False
    assert result["state"] == rq._CI_PENDING


@pytest.mark.asyncio
async def test_resolve_candidate_ci_direct_green(mocker):
    """Candidate commit carries a completed green build → ci_green=True."""
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(
            return_value={"status": "completed", "conclusion": "success", "details": []}
        ),
    )
    mocker.patch(
        "app.github.list_pull_requests_for_commit", new=AsyncMock(return_value=[])
    )
    result = await rq._resolve_candidate_ci("tok", "owner", "repo", "abc123")
    assert result["ci_green"] is True
    assert result["state"] == rq._CI_GREEN
    assert result["resolved_sha"] == "abc123"


@pytest.mark.asyncio
async def test_resolve_candidate_ci_direct_red(mocker):
    """Candidate commit carries a completed failing build → ci_green=False."""
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(
            return_value={"status": "completed", "conclusion": "failure", "details": []}
        ),
    )
    result = await rq._resolve_candidate_ci("tok", "owner", "repo", "abc123")
    assert result["ci_green"] is False
    assert result["state"] == rq._CI_RED


@pytest.mark.asyncio
async def test_resolve_candidate_ci_direct_pending(mocker):
    """Candidate commit has in-progress checks → ci_green=False (pending)."""
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(
            return_value={"status": "in_progress", "conclusion": None, "details": []}
        ),
    )
    result = await rq._resolve_candidate_ci("tok", "owner", "repo", "abc123")
    assert result["ci_green"] is False
    assert result["state"] == rq._CI_PENDING


@pytest.mark.asyncio
async def test_resolve_candidate_ci_no_checks_resolves_via_pr_head(mocker):
    """Candidate has no check-runs; CI ran on the PR head commit — resolves green."""
    pr_head_sha = "pr_head_deadbeef"
    call_count = {"n": 0}

    async def _check_runs_side_effect(token, owner, repo, sha, **kwargs):
        call_count["n"] += 1
        if sha == "candidate000":
            return {"status": "no_checks", "conclusion": None, "details": []}
        # PR head commit
        return {"status": "completed", "conclusion": "success", "details": []}

    mocker.patch(
        "app.github.get_check_runs_cached", side_effect=_check_runs_side_effect
    )
    mocker.patch(
        "app.github.list_pull_requests_for_commit",
        new=AsyncMock(
            return_value=[
                {
                    "number": 42,
                    "merged_at": "2026-01-01T00:00:00Z",
                    "head": {"sha": pr_head_sha},
                },
            ]
        ),
    )
    result = await rq._resolve_candidate_ci("tok", "owner", "repo", "candidate000")
    assert result["ci_green"] is True
    assert result["state"] == rq._CI_GREEN
    assert result["resolved_sha"] == pr_head_sha


@pytest.mark.asyncio
async def test_resolve_candidate_ci_no_checks_no_prs_returns_pending(mocker):
    """Candidate has no check-runs and no associated PRs → pending (not-green)."""
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(
            return_value={"status": "no_checks", "conclusion": None, "details": []}
        ),
    )
    mocker.patch(
        "app.github.list_pull_requests_for_commit",
        new=AsyncMock(return_value=[]),
    )
    result = await rq._resolve_candidate_ci("tok", "owner", "repo", "candidate111")
    assert result["ci_green"] is False
    assert result["state"] == rq._CI_PENDING


@pytest.mark.asyncio
async def test_resolve_candidate_ci_check_runs_fetch_error_returns_pending(mocker):
    """Any exception from get_check_runs_cached → pending (never raises)."""
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(side_effect=RuntimeError("transient network error")),
    )
    result = await rq._resolve_candidate_ci("tok", "owner", "repo", "candidate222")
    assert result["ci_green"] is False
    assert result["state"] == rq._CI_PENDING


@pytest.mark.asyncio
async def test_resolve_candidate_ci_no_checks_pr_head_red_returns_red(mocker):
    """Candidate has no check-runs; CI ran on the PR head and is RED → ci_green=False."""
    pr_head_sha = "pr_head_red0001"

    async def _check_runs_side_effect(token, owner, repo, sha, **kwargs):
        if sha == "candidate_red":
            return {"status": "no_checks", "conclusion": None, "details": []}
        return {"status": "completed", "conclusion": "failure", "details": []}

    mocker.patch(
        "app.github.get_check_runs_cached", side_effect=_check_runs_side_effect
    )
    mocker.patch(
        "app.github.list_pull_requests_for_commit",
        new=AsyncMock(
            return_value=[
                {
                    "number": 7,
                    "merged_at": "2026-01-01T00:00:00Z",
                    "head": {"sha": pr_head_sha},
                }
            ]
        ),
    )
    result = await rq._resolve_candidate_ci("tok", "owner", "repo", "candidate_red")
    assert result["ci_green"] is False
    assert result["state"] == rq._CI_RED
    assert result["resolved_sha"] == pr_head_sha


@pytest.mark.asyncio
async def test_resolve_candidate_ci_no_checks_pr_head_pending_returns_pending(mocker):
    """Candidate has no check-runs; PR head CI still in progress → pending (not-green)."""
    pr_head_sha = "pr_head_pending01"

    async def _check_runs_side_effect(token, owner, repo, sha, **kwargs):
        if sha == "candidate_pend":
            return {"status": "no_checks", "conclusion": None, "details": []}
        return {"status": "in_progress", "conclusion": None, "details": []}

    mocker.patch(
        "app.github.get_check_runs_cached", side_effect=_check_runs_side_effect
    )
    mocker.patch(
        "app.github.list_pull_requests_for_commit",
        new=AsyncMock(
            return_value=[
                {
                    "number": 8,
                    "merged_at": "2026-01-01T00:00:00Z",
                    "head": {"sha": pr_head_sha},
                }
            ]
        ),
    )
    result = await rq._resolve_candidate_ci("tok", "owner", "repo", "candidate_pend")
    assert result["ci_green"] is False
    assert result["state"] == rq._CI_PENDING


@pytest.mark.asyncio
async def test_resolve_candidate_ci_red_beats_green_across_multiple_pr_heads(mocker):
    """With multiple PR heads, a red result on any head must win over a green on
    another head — regardless of which order GitHub returns them.  The resolver
    is exhaustive: it inspects all PR heads and returns red if any is red."""
    green_sha = "pr_head_green01"
    red_sha = "pr_head_red0002"

    async def _check_runs_side_effect(token, owner, repo, sha, **kwargs):
        if sha == "candidate_multi":
            return {"status": "no_checks", "conclusion": None, "details": []}
        if sha == green_sha:
            return {"status": "completed", "conclusion": "success", "details": []}
        if sha == red_sha:
            return {"status": "completed", "conclusion": "failure", "details": []}
        return {"status": "no_checks", "conclusion": None, "details": []}

    mocker.patch(
        "app.github.get_check_runs_cached", side_effect=_check_runs_side_effect
    )
    # Green head is listed FIRST — the resolver must not short-circuit on it.
    mocker.patch(
        "app.github.list_pull_requests_for_commit",
        new=AsyncMock(
            return_value=[
                {
                    "number": 10,
                    "merged_at": "2026-01-01T00:00:00Z",
                    "head": {"sha": green_sha},
                },
                {
                    "number": 11,
                    "merged_at": "2026-01-02T00:00:00Z",
                    "head": {"sha": red_sha},
                },
            ]
        ),
    )
    result = await rq._resolve_candidate_ci("tok", "owner", "repo", "candidate_multi")
    # Red must win despite being listed after the green PR.
    assert result["ci_green"] is False
    assert result["state"] == rq._CI_RED
    assert result["resolved_sha"] == red_sha


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
        # Distinct integration/promotion pair (the multi-branch LTS model these
        # promote tests intend — head=release-vN, base="main"). Without an
        # explicit pair a non-self repo resolves to main/main, which is the
        # single-branch case promote() now short-circuits as lts_inactive.
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


def _branch_sha_mock(
    *, automain: str = "automn0" * 5 + "00000", main: str = "main00" * 6 + "0000"
):
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


def _patch_promote_github_defaults(mocker, *, already_on_main: bool = False):
    """Patch the GitHub helpers promote() calls that are NOT the focus of a test.

    promote() makes several GitHub calls beyond branch/PR/merge:

      * ``github.is_ancestor_of`` — the idempotency ancestry check (R4-C1/H1).
        Defaults to ``already_on_main=False`` (candidate NOT yet on main) so a
        normal-flow test proceeds to actually open + merge the PR.  Pass
        ``already_on_main=True`` to exercise the already-landed idempotent
        path.
      * ``github.get_pull_request_merge_state`` — Round 7 concern 3: the
        authoritative PR-state check promote() runs to disambiguate a
        ``merge_pull_request`` ``{"merged": False}`` result.  Defaulted CONSISTENT
        with ``already_on_main`` (an already-landed candidate's PR is merged;
        a not-landed candidate's PR is not) so a test that does not specifically
        exercise the merged=False disambiguation behaves coherently.
      * ``github.delete_branch`` — the best-effort release-branch cleanup
        (R4-H2).  Defaulted to a no-op success so a test that does not
        specifically assert on branch deletion does not make a real network
        call.

    Returns ``(is_ancestor_mock, delete_branch_mock, pr_state_mock)`` so a test
    can assert on any of them when it cares.  A test that specifically exercises
    one of these paths should patch that helper itself (this default patch is
    only for the helpers a given test treats as incidental).
    """
    is_ancestor_mock = mocker.patch(
        "app.github.is_ancestor_of",
        new=AsyncMock(return_value=already_on_main),
    )
    delete_branch_mock = mocker.patch(
        "app.github.delete_branch",
        new=AsyncMock(return_value=True),
    )
    pr_state_mock = mocker.patch(
        "app.github.get_pull_request_merge_state",
        new=AsyncMock(return_value=already_on_main),
    )
    return is_ancestor_mock, delete_branch_mock, pr_state_mock


@pytest.fixture(autouse=True)
def _qualifier_ci_resolves_green(mocker):
    """Default the qualifier's CI resolution to a real completed green build.

    Both ``run_qualification`` and ``_finalize_qualification`` resolve CI for
    the candidate commit through ``_resolve_candidate_ci``, which calls the
    GitHub check-runs + PR-for-commit + installation-token helpers.  The
    overwhelming majority of coordinator tests exercise guards, scoring,
    promote paths or notifications where CI is incidental and the candidate is
    assumed green.

    This autouse fixture patches those GitHub helpers so ``_resolve_candidate_ci``
    runs for real and resolves GREEN by default — the candidate commit reports
    a completed, all-passing build.  Tests that exercise the CI gate itself
    re-patch ``get_check_runs_cached`` / ``list_pull_requests_for_commit`` in
    the test body; the later patch wins.  Pure-function tests (compute_composite
    etc.) never reach these helpers, so the patches are harmless there.
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
    # Default the qualify-on-synced gate to SYNCED so the overwhelming majority
    # of run_qualification tests (which assume the candidate is promotable)
    # proceed without a real compare call. Patches the gate's OWN seam (not the
    # shared github.is_ancestor_of, which promote-idempotency + TestIsAncestorOf
    # depend on). Tests that exercise the gate re-patch it; the later patch wins.
    mocker.patch(
        "app.release_qualifier._is_target_absorbed",
        new=AsyncMock(return_value=True),
    )
    # Default the pre-merge Alembic dual-head guard to a PR that touches NO
    # migration files, so promote()'s enforce_premerge_migration_guard returns
    # ("ok") and the merge proceeds unchanged for the majority of promote tests
    # where migration safety is incidental. Tests that exercise the guard
    # re-patch get_pr_files (or the diff / version-listing helpers) in their own
    # body; the later patch wins.
    mocker.patch(
        "app.github.get_pr_files",
        new=AsyncMock(return_value=[]),
    )
    # Default the finalize-time deterministic Alembic revision-identity gate to
    # CLEAN: _finalize_qualification lists the candidate's and the promotion
    # target's migration filenames via github.list_versions_at_ref and runs
    # find_content_divergent_revisions over them. Returning the SAME list for
    # every ref means no revision id diverges, so the gate is "ok" and the
    # finalize proceeds to the quality composite for the majority of tests where
    # migration safety is incidental. Tests exercising the gate re-patch this in
    # their own body (branch-aware / raising); the later patch wins.
    mocker.patch(
        "app.github.list_versions_at_ref",
        new=AsyncMock(return_value=["0001_init.py", "0002_feature.py"]),
    )
    # Default the finalize-time scratch-DB migration-APPLY gate to CLEAN.
    # _finalize_qualification calls run_migration_apply_gate, which in production
    # reproduces the candidate's upgrade against production's recorded state on
    # an isolated Postgres scratch DB. The test session is SQLite, so the real
    # gate would 'defer' (non-Postgres bind) and HOLD every finalize as an infra
    # failure. Defaulting it to ("ok", None) keeps migration-apply safety
    # incidental for the majority of tests; tests exercising the gate re-patch
    # this in their own body (block / defer); the later patch wins.
    mocker.patch(
        "app.release_qualifier.run_migration_apply_gate",
        new=AsyncMock(return_value=("ok", None)),
    )


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

    project, version = await _make_project_and_version(db_session, phase="stabilize")

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
        new=AsyncMock(return_value={"status": "completed", "conclusion": "success"}),
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
    assert audit_task.context["scope"] == "medium"

    from datetime import datetime, timezone

    from app import reaper

    hb_cutoff = reaper._heartbeat_stale_cutoff(
        audit_task, datetime(2026, 1, 1, tzinfo=timezone.utc)
    )
    assert (
        datetime(2026, 1, 1, tzinfo=timezone.utc) - hb_cutoff
    ).total_seconds() == 420

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

    project, version = await _make_project_and_version(db_session, phase="stabilize")

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
    project, version = await _make_project_and_version(db_session, phase="stabilize")

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=AsyncMock(return_value="dead" * 10),
    )
    # The real get_check_runs_cached always emits ``status`` alongside
    # ``conclusion`` (a "failure" conclusion is only ever produced with
    # status="completed" — see github_checks.get_check_runs). Without the
    # status key the prod classifier (_classify_check_runs) falls through to
    # _CI_PENDING, so the old {"conclusion": "failure"} mock passed for the
    # wrong reason (ci_green is False under BOTH pending and red). The faithful
    # shape exercises the actual red-build branch.
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(return_value={"status": "completed", "conclusion": "failure"}),
    )

    result = await rq.run_qualification(db_session, project.id, version)
    await db_session.commit()

    assert result["status"] == "spawned"
    assert result["ci_green"] is False
    assert result["ci_state"] == "red"
    await db_session.refresh(version)
    assert version.last_qualification["ci_green"] is False
    assert version.last_qualification["ci_state"] == "red"


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


# ===========================================================================
# run_qualification — qualify-on-synced gate (LTS split)
# ===========================================================================


async def _audit_tasks(db_session, project_id):
    rows = (
        (await db_session.execute(select(Task).where(Task.project_id == project_id)))
        .scalars()
        .all()
    )
    return [t for t in rows if t.first_payload.get("type") == "release_audit"]


@pytest.mark.asyncio
async def test_run_qualification_proceeds_when_synced(db_session, mocker):
    """When the promotion target is already absorbed into the candidate (synced),
    the gate is a no-op and qualification proceeds normally (pending verdict)."""
    project, version = await _make_project_and_version(db_session, phase="stabilize")
    mocker.patch("app.github.get_branch_sha", new=_branch_sha_mock())
    mocker.patch("app.release_qualifier._is_target_absorbed", new=AsyncMock(return_value=True))

    result = await rq.run_qualification(db_session, project.id, version)
    await db_session.commit()

    assert result["status"] == "spawned"
    await db_session.refresh(version)
    assert version.last_qualification["status"] == "pending"
    assert len(await _audit_tasks(db_session, project.id)) == 1


@pytest.mark.asyncio
async def test_run_qualification_defers_when_candidate_behind_main(db_session, mocker):
    """A candidate the promotion target is NOT yet absorbed into is DEFERRED, not
    scored: no audit child spawns, and the version records a retryable
    deferred_unsynced state (regression for the 2026-06-02 stale-candidate
    promotion break)."""
    project, version = await _make_project_and_version(db_session, phase="stabilize")
    mocker.patch("app.github.get_branch_sha", new=_branch_sha_mock())
    # main NOT absorbed into the candidate.
    mocker.patch("app.release_qualifier._is_target_absorbed", new=AsyncMock(return_value=False))

    result = await rq.run_qualification(db_session, project.id, version)
    await db_session.commit()

    assert result["status"] == "deferred_unsynced"
    await db_session.refresh(version)
    assert version.last_qualification["status"] == "deferred_unsynced"
    assert version.last_qualification["first_deferred_at"]
    # A deferral is not a qualification attempt and spawns nothing.
    assert version.qualification_attempts == 0
    assert await _audit_tasks(db_session, project.id) == []


@pytest.mark.asyncio
async def test_run_qualification_fall_through_when_unsynced_too_long(
    db_session, mocker
):
    """Bounded fall-through: a candidate behind main for >= the fall-through
    window qualifies ANYWAY — a wedged back-merge must never starve promotion."""
    from datetime import datetime, timedelta

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    # Pre-seed a stale deferral so the accumulated wait exceeds the 30-min window.
    version.last_qualification = {
        "status": "deferred_unsynced",
        "first_deferred_at": (datetime.utcnow() - timedelta(minutes=45)).isoformat(),
    }
    db_session.add(version)
    await db_session.commit()
    mocker.patch("app.github.get_branch_sha", new=_branch_sha_mock())
    mocker.patch("app.release_qualifier._is_target_absorbed", new=AsyncMock(return_value=False))

    result = await rq.run_qualification(db_session, project.id, version)
    await db_session.commit()

    # Fell through → scored anyway.
    assert result["status"] == "spawned"
    assert len(await _audit_tasks(db_session, project.id)) == 1


@pytest.mark.asyncio
async def test_run_qualification_no_sync_gate_for_single_branch(db_session, mocker):
    """Single-branch projects (integration == promotion) have no back-merge
    concept — the sync gate is skipped entirely and never runs the compare, even
    when is_ancestor_of would report not-absorbed."""
    project, version = await _make_project_and_version(db_session, phase="stabilize")
    project.integration_branch = "main"
    project.promotion_target = "main"
    db_session.add(project)
    await db_session.commit()
    mocker.patch(
        "app.github.get_branch_sha", new=AsyncMock(return_value="cafe1234" * 5)
    )
    anc = mocker.patch(
        "app.release_qualifier._is_target_absorbed", new=AsyncMock(return_value=False)
    )

    result = await rq.run_qualification(db_session, project.id, version)
    await db_session.commit()

    assert result["status"] == "spawned"
    anc.assert_not_called()  # the gate is short-circuited before any compare


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
    project, version = await _make_project_and_version(db_session, phase="stabilize")

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
async def test_run_qualification_promotion_in_flight_is_noop(db_session, mocker):
    """A version whose audit PASSED and now has an open promotion PR
    auto-merging (status='pending' + a promotion_pending sub-key, children
    terminal) must NOT be re-qualified: doing so would spawn a fresh heavy
    audit/smoketest pair and clobber the promotion-pending provenance.
    run_qualification refuses with promotion_in_flight, spawns nothing, and
    leaves the record untouched — consistent with the version-cut lever."""
    project, version = await _make_project_and_version(db_session, phase="stabilize")

    # Both children already terminal — by the child-task definition this is NOT
    # "in flight", and 'pending' is not a conclusive verdict, so only the
    # promotion-in-flight guard catches it.
    audit_task = Task(
        project_id=project.id,
        title="audit-done",
        payloads=[{"type": "release_audit", "base_branch": "main"}],
        vm_instance_id="vm-audit-promo",
        status=TaskStatus.COMPLETED,
        context={"release_version_id": str(version.id)},
    )
    db_session.add(audit_task)
    await db_session.commit()
    await db_session.refresh(audit_task)

    version.last_qualification = {
        "status": "pending",
        "candidate_sha": "feed" * 10,
        "ci_green": True,
        "started_at": "2026-05-20T00:00:00",
        "audit_task_id": str(audit_task.id),
        "smoketest_task_id": None,
        "smoketest_enabled": False,
        "promotion_pending": {
            "pr_number": 4242,
            "pr_url": "https://github.com/o/r/pull/4242",
            "auto_merge_enabled": True,
        },
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

    assert result["status"] == "promotion_in_flight"
    assert result["promotion_pr_number"] == 4242
    assert result["promotion_pr_url"] == "https://github.com/o/r/pull/4242"
    # The pre-lock guard fired before any GitHub I/O (no SHA snapshot).
    sha_mock.assert_not_called()

    await db_session.refresh(version)
    # The promotion-pending record is untouched; attempts NOT bumped.
    assert version.last_qualification["promotion_pending"]["pr_number"] == 4242
    assert version.last_qualification["candidate_sha"] == "feed" * 10
    assert version.qualification_attempts == attempts_before

    # Still exactly the one original child — no fresh audit/smoketest pair.
    from sqlmodel import select

    tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    assert len(tasks) == 1


@pytest.mark.asyncio
async def test_run_qualification_pending_but_children_terminal_runs_fresh(
    db_session, mocker
):
    """A 'pending' record whose children have ALL terminated is NOT in flight
    — it is merely awaiting finalize.  A fresh run_qualification is allowed
    (the stale record gets overwritten), so a new qualification spawns."""
    project, version = await _make_project_and_version(db_session, phase="stabilize")

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
    project, version = await _make_project_and_version(db_session, phase="stabilize")

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
    project, version = await _make_project_and_version(db_session, phase="stabilize")

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
async def test_run_qualification_cached_dry_run_same_sha_is_noop(db_session, mocker):
    """Idempotent qualification: when a conclusive dry_run verdict exists for
    this candidate_sha and automain HEAD still equals it, run_qualification
    early-returns cached_verdict and spawns nothing — the strategic agent
    re-emitting qualify_request every cycle must not re-spawn 2 agent VMs for
    an identical, already-scored commit. Relying on lucky re-rolls is not a
    sound gate; the next automain advance will trigger a fresh qualification."""
    project, version = await _make_project_and_version(db_session, phase="stabilize")

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

    assert result["status"] == "cached_verdict"
    assert result["cached_verdict_status"] == "dry_run"

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
    project, version = await _make_project_and_version(db_session, phase="stabilize")

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


@pytest.mark.asyncio
async def test_run_qualification_cached_not_promoted_same_sha_is_noop(db_session, mocker):
    """Idempotent qualification: when a conclusive not_promoted verdict exists
    for this candidate_sha and automain HEAD still equals it, run_qualification
    early-returns cached_verdict and spawns nothing — a candidate that scored
    below the promotion bar is not re-audited on the same commit."""
    project, version = await _make_project_and_version(db_session, phase="stabilize")

    candidate_sha = "notprom0" * 5  # 40-char placeholder SHA
    version.last_qualification = {
        "status": "not_promoted",
        "candidate_sha": candidate_sha,
        "ci_green": True,
        "confidence": 0.82,
        "threshold": 0.90,
        "started_at": "2026-05-19T00:00:00",
        "finalized_at": "2026-05-19T00:05:00",
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)
    attempts_before = version.qualification_attempts

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    # automain HEAD still equals the not_promoted candidate.
    mocker.patch(
        "app.github.get_branch_sha",
        new=AsyncMock(return_value=candidate_sha),
    )
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(return_value={"conclusion": "success"}),
    )

    result = await rq.run_qualification(db_session, project.id, version)
    await db_session.commit()

    assert result["status"] == "cached_verdict"
    assert result["cached_verdict_status"] == "not_promoted"

    await db_session.refresh(version)
    assert version.last_qualification["status"] == "not_promoted"
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
async def test_run_qualification_cached_promoted_same_sha_is_noop(db_session, mocker):
    """Idempotent qualification: when a conclusive promoted verdict exists
    for this candidate_sha and automain HEAD still equals it, run_qualification
    early-returns cached_verdict and spawns nothing — a successfully promoted
    commit is not re-audited."""
    project, version = await _make_project_and_version(db_session, phase="stabilize")

    candidate_sha = "promot00" * 5  # 40-char placeholder SHA
    version.last_qualification = {
        "status": "promoted",
        "candidate_sha": candidate_sha,
        "ci_green": True,
        "confidence": 0.95,
        "threshold": 0.90,
        "started_at": "2026-05-19T00:00:00",
        "finalized_at": "2026-05-19T00:05:00",
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)
    attempts_before = version.qualification_attempts

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    # automain HEAD still equals the promoted candidate.
    mocker.patch(
        "app.github.get_branch_sha",
        new=AsyncMock(return_value=candidate_sha),
    )
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(return_value={"conclusion": "success"}),
    )

    result = await rq.run_qualification(db_session, project.id, version)
    await db_session.commit()

    assert result["status"] == "cached_verdict"
    assert result["cached_verdict_status"] == "promoted"

    await db_session.refresh(version)
    assert version.last_qualification["status"] == "promoted"
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
async def test_run_qualification_non_conclusive_error_always_retries(db_session, mocker):
    """Non-idempotent infra/error verdicts: when a prior qualification
    finalized with an error status (not a conclusive verdict) and the
    candidate_sha is unchanged, run_qualification proceeds with a fresh run
    (not cached) because the error produced no real quality verdict."""
    project, version = await _make_project_and_version(db_session, phase="stabilize")

    candidate_sha = "errorsh0" * 5  # 40-char placeholder SHA
    version.last_qualification = {
        "status": "error",
        "error": "spawn_failed",
        "candidate_sha": candidate_sha,
        "started_at": "2026-05-19T00:00:00",
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    # Create a mock audit task to return from spawn.
    audit_task = Task(
        project_id=project.id,
        title="audit-retry",
        payloads=[{"type": "release_audit", "base_branch": "main"}],
        vm_instance_id="vm-audit-retry",
        status=TaskStatus.QUEUED,
        context={"release_version_id": str(version.id)},
    )
    db_session.add(audit_task)
    await db_session.commit()
    await db_session.refresh(audit_task)

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    # automain HEAD still equals the error candidate.
    mocker.patch(
        "app.github.get_branch_sha",
        new=AsyncMock(return_value=candidate_sha),
    )
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(return_value={"conclusion": "success"}),
    )
    mocker.patch(
        "app.release_qualifier._spawn_release_audit_task",
        new=AsyncMock(return_value=audit_task),
    )

    result = await rq.run_qualification(db_session, project.id, version)
    await db_session.commit()

    # Not cached — a fresh qualification spawned despite unchanged sha.
    assert result["status"] == "spawned"


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
async def test_finalize_records_excluded_audit_findings(db_session, mocker):
    """_finalize_qualification carries excluded (unverified higher-severity)
    findings into last_qualification for display, while audit_findings still
    omits them and the reconciliation count agrees."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = False

    project, version = await _make_project_and_version(db_session)
    await _seed_qualification_children(
        db_session,
        project,
        version,
        audit_logs={
            # Model self-reports a clean score; the only finding is an
            # unverified high — excluded from scoring and audit_findings, but
            # surfaced in excluded_audit_findings.
            "audit_score": 1.0,
            "findings": [
                {"severity": "high", "summary": "Unverified injection risk."},
            ],
            "test_erosion": {
                "deleted_test_files": [],
                "assertion_delta": 0,
                "test_count_delta": 0,
            },
        },
        smoke_logs={
            "passed": True,
            "tests": [{"name": "a", "status": "pass"}],
            "suites": {"startup": {"passed": True}},
        },
    )

    mocker.patch("app.release_qualifier.promote", new=AsyncMock())

    await rq._finalize_qualification(db_session, version)
    await db_session.commit()
    await db_session.refresh(version)

    lq = version.last_qualification
    # The unverified high finding is carried for display, marked unverified.
    assert lq["excluded_audit_findings"] == [
        {
            "severity": "high",
            "summary": "Unverified injection risk.",
            "unverified": "true",
        }
    ]
    # ...but it must NOT appear in the scored/actionable findings list.
    assert lq["audit_findings"] == []
    # ...and the reconciliation count agrees.
    assert lq["audit_reconciliation"]["excluded_findings_count"] == 1


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


def _versions_by_ref(*, main_list: list[str], candidate_list: list[str]) -> AsyncMock:
    """Branch-keyed ``list_versions_at_ref``: deployed promotion target vs candidate.

    The finalize-time migration-identity gate lists the candidate's migrations
    at ``candidate_sha`` and the deployed promotion target's at ``"main"`` — so
    a ``ref == "main"`` request answers with the deployed list and everything
    else (the candidate SHA) answers with the candidate list. ``ref`` is the 4th
    positional arg of ``list_versions_at_ref``.
    """

    async def _side(token, owner, repo, ref, project_id=None):
        return list(main_list) if ref == "main" else list(candidate_list)

    return AsyncMock(side_effect=_side)


@pytest.mark.asyncio
async def test_finalize_blocks_on_migration_identity_collision(db_session, mocker):
    """An UNAUTHORIZED revision-identity collision (a revision id reused for
    different content, NOT on the authorized-reconciliation registry) must be a
    HARD qualification blocker end-to-end: deployed main keeps revision 0138 ==
    digest_cache_table, the candidate reassigned 0138 to task_blocked_state_fields.
    An otherwise-clean candidate (audit 1.0, smoketest pass, CI green) must be
    HARD-blocked at qualification: confidence forced to 0, promote False, and the
    blocker names the diverging revision — never a soft audit deduction.

    (The revision-0143 renumber is the ONE authorized, deploy-time-reconciled
    divergence — its non-blocking path is covered by
    ``test_qualification_gate_authorizes_reconciled_0143_divergence`` in
    test_migration_divergence.py; using it here would no longer block.)"""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = True

    project, version = await _make_project_and_version(db_session)
    await _seed_qualification_children(
        db_session,
        project,
        version,
        # A pristine quality profile — without the migration gate this candidate
        # would promote, isolating the migration collision as the sole blocker.
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

    # Deployed main keeps 0138 == digest_cache_table; the candidate reused 0138
    # for a DIFFERENT migration (task_blocked_state_fields) — an unrelated
    # revision-identity collision the deterministic detector flags, and one NOT
    # on the authorized-reconciliation registry (so it still hard-blocks).
    mocker.patch(
        "app.github.list_versions_at_ref",
        new=_versions_by_ref(
            main_list=["0138_digest_cache_table.py"],
            candidate_list=["0138_task_blocked_state_fields.py"],
        ),
    )

    # A real collision must NEVER promote, even with promotion enabled.
    promote_spy = mocker.patch("app.release_qualifier.promote", new=AsyncMock())

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    assert result["status"] == "not_promoted"
    assert result["blocker"] == "migration_identity_collision"
    assert result["confidence"] == 0.0
    promote_spy.assert_not_called()

    await db_session.refresh(version)
    lq = version.last_qualification
    assert lq["status"] == "not_promoted"
    assert lq["blocker"] == "migration_identity_collision"
    assert lq["confidence"] == 0.0
    assert version.phase == "stabilize"
    # The detail names the specific diverging revision (0138) so the operator
    # sees WHICH revision collides, not just a generic blocker label.
    assert lq["migration_safety"] == "block"
    detail = lq["migration_safety_detail"]
    assert "0138" in detail
    assert "digest_cache_table" in detail


@pytest.mark.asyncio
async def test_finalize_holds_when_migration_listing_fetch_fails(db_session, mocker):
    """Fail-safe: when the candidate's / target's migration list cannot be
    fetched, migration safety is UNKNOWN — the release is HELD (deferred to the
    next cycle) rather than waved through. A fetch error is not 'no collision'."""
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

    # The migration listing fetch fails transiently — uncertainty must HOLD.
    mocker.patch(
        "app.github.list_versions_at_ref",
        new=AsyncMock(side_effect=RuntimeError("GitHub 502")),
    )
    promote_spy = mocker.patch("app.release_qualifier.promote", new=AsyncMock())

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    # Held as an infra failure — no quality verdict, never promoted.
    assert result["status"] == "infra_failure"
    assert result["error"] == "migration_identity_check_unavailable"
    promote_spy.assert_not_called()

    await db_session.refresh(version)
    assert version.status == "open"
    assert version.phase == "stabilize"
    assert version.last_qualification["status"] == "infra_failure"


@pytest.mark.asyncio
async def test_finalize_blocks_on_migration_apply_drift(db_session, mocker):
    """The scratch-DB migration-apply gate is a SECOND deterministic dimension:
    when it returns 'block' (the candidate's chain, replayed against production's
    real recorded schema, leaves a required object uncreated — the v5 computejob
    renumber-skip class) an otherwise-clean candidate is HARD-blocked. Confidence
    forced to 0, promote False, blocker migration_apply_drift, and the verdict
    record carries migration_apply_safety='block' with the specific detail.

    The filename-identity gate stays CLEAN here (autouse default), isolating the
    apply gate as the sole blocker — proving the two dimensions are distinct."""
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

    # The apply gate reports deploy-fatal drift: a required table is uncreated
    # when the candidate's chain is applied against production's recorded state.
    apply_detail = (
        "candidate migrations applied against production's recorded revision "
        "'0375' leave the schema missing required object(s) (deploy-fatal "
        "drift): table 'computejob'"
    )
    mocker.patch(
        "app.release_qualifier.run_migration_apply_gate",
        new=AsyncMock(return_value=("block", apply_detail)),
    )
    promote_spy = mocker.patch("app.release_qualifier.promote", new=AsyncMock())

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    assert result["status"] == "not_promoted"
    assert result["blocker"] == "migration_apply_drift"
    assert result["confidence"] == 0.0
    promote_spy.assert_not_called()

    await db_session.refresh(version)
    lq = version.last_qualification
    assert lq["status"] == "not_promoted"
    assert lq["blocker"] == "migration_apply_drift"
    assert lq["confidence"] == 0.0
    assert version.phase == "stabilize"
    # The new fields are recorded alongside (not replacing) the existing
    # migration-identity fields — the identity gate stayed clean here.
    assert lq["migration_safety"] == "ok"
    assert lq["migration_apply_safety"] == "block"
    assert lq["migration_apply_safety_detail"] == apply_detail
    assert "computejob" in lq["migration_apply_safety_detail"]


@pytest.mark.asyncio
async def test_finalize_holds_when_migration_apply_gate_defers(db_session, mocker):
    """Fail-safe: when the apply gate cannot determine safety (prod state
    unreadable, candidate fetch failed, scratch DB unprovisionable) it returns
    'defer'. Migration-apply safety is UNKNOWN, so the release is HELD as an
    infra failure (no quality verdict, returned to stabilize) — never waved
    through, mirroring the identity gate's defer path."""
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

    mocker.patch(
        "app.release_qualifier.run_migration_apply_gate",
        new=AsyncMock(
            return_value=("defer", "could not read production's recorded state")
        ),
    )
    promote_spy = mocker.patch("app.release_qualifier.promote", new=AsyncMock())

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    assert result["status"] == "infra_failure"
    assert result["error"] == "migration_apply_check_unavailable"
    promote_spy.assert_not_called()

    await db_session.refresh(version)
    assert version.status == "open"
    assert version.phase == "stabilize"
    assert version.last_qualification["status"] == "infra_failure"


@pytest.mark.asyncio
async def test_finalize_apply_gate_ok_does_not_block_promotion(db_session, mocker):
    """When the apply gate returns 'ok' (the autouse default) qualification
    proceeds normally — the new dimension is inert on a clean candidate and the
    verdict records migration_apply_safety='ok'. This is the complement of the
    block/defer cases above: an 'ok' apply gate never perturbs the promotion
    path."""
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

    # Leave the autouse ("ok", None) apply-gate default in place. The candidate
    # is otherwise pristine, so it clears every dimension and reaches promote();
    # the apply gate never blocks it.
    promote_spy = mocker.patch(
        "app.release_qualifier.promote",
        new=AsyncMock(return_value={"status": "promoted"}),
    )

    await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    # Reaching promote() at all proves the apply gate did not block; the verdict
    # record threaded into promote carries the clean apply-safety fields.
    promote_spy.assert_called_once()
    base_record = promote_spy.call_args.kwargs["base_record"]
    assert base_record["migration_apply_safety"] == "ok"
    assert base_record["migration_apply_safety_detail"] is None
    assert base_record["blocker"] is None


@pytest.mark.asyncio
async def test_finalize_threads_snapshotted_candidate_sha_into_apply_gate(
    db_session, mocker
):
    """The finalize path must drive migration-apply from last_qualification."""
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

    candidate_sha = "cafe" * 10
    version.last_qualification = {
        **(version.last_qualification or {}),
        "candidate_sha": candidate_sha,
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    observed = {}

    async def _fake_apply_gate(**kwargs):
        observed["candidate_ref"] = kwargs["candidate_ref"]
        return ("ok", None)

    mocker.patch(
        "app.release_qualifier.run_migration_apply_gate",
        new=AsyncMock(side_effect=_fake_apply_gate),
    )
    promote_spy = mocker.patch(
        "app.release_qualifier.promote",
        new=AsyncMock(return_value={"status": "promoted"}),
    )

    await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    assert observed["candidate_ref"] == candidate_sha
    promote_spy.assert_called_once()


@pytest.mark.asyncio
async def test_finalize_carries_audit_findings_into_last_qualification(
    db_session, mocker
):
    """A below-threshold qualification stores the audit findings on the version
    so the strategic agent's payload (via to_phase_block) can target them —
    not just the composite score."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = True

    project, version = await _make_project_and_version(db_session)
    await _seed_qualification_children(
        db_session,
        project,
        version,
        # Three findings derive audit 0.68; with erosion 1.0 and smoketest 0.5
        # (half-passing), the composite lands at 0.722, under the 0.90
        # bar and on the soft-miss path.
        audit_logs={
            "audit_score": 0.35,
            "test_erosion": {
                "deleted_test_files": [],
                "assertion_delta": 840,
                "test_count_delta": 373,
            },
            "findings": [
                {"severity": "low", "summary": "Private cross-module import."},
                {
                    "severity": "high",
                    "summary": "Token baked into VM image.",
                    "verification_note": "Read infra/packer/agent.pkr.hcl lines 20-35: API token hardcoded in provisioner shell command, present in diff with no mitigation.",
                },
                {
                    "severity": "high",
                    "summary": "Unsafe dependency pinning.",
                    "verification_note": "Read agent/package.json: dependency version unpinned (uses ^), confirmed change in diff allows semver drift.",
                },
            ],
        },
        smoke_logs={
            "passed": True,
            "tests": [
                {"name": "a", "status": "pass"},
                {"name": "b", "status": "fail"},
            ],
            "suites": {"startup": {"passed": True}},
        },
    )

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    assert result["status"] == "not_promoted"
    assert result["blocker"] is None

    await db_session.refresh(version)
    findings = version.last_qualification["audit_findings"]
    # Most-severe first, all three findings carried with severity + summary.
    assert findings == [
        {"severity": "high", "summary": "Token baked into VM image."},
        {"severity": "high", "summary": "Unsafe dependency pinning."},
        {"severity": "low", "summary": "Private cross-module import."},
    ]
    # Test-erosion detail is carried too: this fixture has positive deltas and
    # no deleted files, so the clean dimension carries None (nothing to fix).
    assert "test_erosion_detail" in version.last_qualification
    assert version.last_qualification["test_erosion_detail"] is None
    # Smoketest detail names the specific failing test so the agent can target it.
    smoketest_detail = version.last_qualification["smoketest_detail"]
    assert smoketest_detail["failed_tests"] == ["b"]
    assert smoketest_detail["passed"] == 1
    assert smoketest_detail["total"] == 2


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
    _patch_promote_github_defaults(mocker)
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
    assert branch_mock.call_args.args[3] == "release-v1"  # branch name
    assert branch_mock.call_args.args[4] == "feed" * 10  # pinned to candidate
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
    # is_ancestor_of=False on BOTH the pre-check and the post-merge re-check:
    # the candidate is genuinely NOT on main, so a 409-conflict non-merge is a
    # real failure (not an idempotent already-merged retry).
    _patch_promote_github_defaults(mocker)
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
    _patch_promote_github_defaults(mocker)
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
async def test_promote_blocks_on_dual_alembic_head(db_session, mocker):
    """The pre-merge Alembic dual-head guard HOLDS a promotion whose added
    migration reuses a revision id already on the promotion target.

    A hotfix migration that landed on ``main`` (the promotion target) AFTER
    qualification but BEFORE this merge, plus a feature migration on the
    integration branch that independently reused the same ``NNNN`` slot, would
    produce a dual head on ``main``. promote() must NOT call the merge API; it
    records a retryable ``migration_guard_blocked`` failure (version stays
    open + stabilize, main untouched, no v2 opened) and best-effort deletes the
    release branch."""
    project, version = await _make_project_and_version(db_session)

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch("app.github.get_branch_sha", new=_branch_sha_mock())
    _is_anc, delete_branch_mock, _state = _patch_promote_github_defaults(mocker)
    mocker.patch("app.github.create_branch", new=_create_branch_mock())
    mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"number": 77, "html_url": "http://pr/77"}),
    )
    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(return_value={"merged": True}),
    )

    # Drive the REAL combined-chain validator: the promotion PR adds migration
    # revision 0042, which already exists on the promotion target → collision.
    migration_path = "backend/alembic/versions/0042_feature.py"
    mocker.patch(
        "app.github.get_pr_files",
        new=AsyncMock(return_value=[{"filename": migration_path}]),
    )
    mocker.patch(
        "app.github.get_pull_request_diff",
        new=AsyncMock(
            return_value=(
                f"diff --git a/{migration_path} b/{migration_path}\n"
                f"--- /dev/null\n"
                f"+++ b/{migration_path}\n"
                "+revision = '0042'\n"
                "+down_revision = '0041'\n"
            )
        ),
    )
    mocker.patch(
        "app.github.list_versions_at_ref",
        new=AsyncMock(return_value=["0041_base.py", "0042_hotfix.py"]),
    )

    result = await rq.promote(
        db_session,
        version,
        "abcd" * 10,
        composite={"confidence": 0.95},
        base_record={"candidate_sha": "abcd" * 10},
    )
    await db_session.commit()

    # The merge API was NEVER called — the guard held the promotion.
    merge_mock.assert_not_called()
    assert result["status"] == "promotion_failed"
    assert result["error"] == "migration_guard_blocked"

    await db_session.refresh(version)
    # Retryable hold: version stays open + stabilize, main untouched.
    assert version.status == "open"
    assert version.phase == "stabilize"
    assert version.last_qualification["status"] == "promotion_failed"
    assert version.last_qualification["error"] == "migration_guard_blocked"
    # The reason names the colliding revision.
    assert "0042" in version.last_qualification["detail"]
    # The release branch was cleaned up since the merge never ran.
    delete_branch_mock.assert_awaited()

    # No next version was opened — the qualified version was NOT discarded.
    open_version = await rv.current_open_version(db_session, project.id)
    assert open_version is not None
    assert open_version.version_number == 1


@pytest.mark.asyncio
async def test_promote_guard_block_skips_auto_merge(db_session, mocker):
    """A blocking guard verdict prevents promote() from reaching auto-merge."""
    project, version = await _make_project_and_version(db_session)

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch("app.github.get_branch_sha", new=_branch_sha_mock())
    _patch_promote_github_defaults(mocker)
    mocker.patch("app.github.create_branch", new=_create_branch_mock())
    mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"number": 77, "html_url": "http://pr/77"}),
    )
    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(return_value={"merged": True}),
    )
    enable_auto_merge_mock = mocker.patch(
        "app.github.enable_auto_merge",
        new=AsyncMock(return_value={"enabled": True}),
    )
    mocker.patch(
        "app.rollup_migration_validation.enforce_premerge_migration_guard",
        new=AsyncMock(return_value=(False, "dual-head: duplicate revision")),
    )

    result = await rq.promote(
        db_session,
        version,
        "abcd" * 10,
        composite={"confidence": 0.95},
        base_record={"candidate_sha": "abcd" * 10},
    )
    await db_session.commit()

    merge_mock.assert_not_called()
    enable_auto_merge_mock.assert_not_called()
    assert result["status"] == "promotion_failed"
    assert result["error"] == "migration_guard_blocked"

    await db_session.refresh(version)
    assert version.status == "open"
    assert version.phase == "stabilize"
    assert version.last_qualification["status"] == "promotion_failed"
    assert version.last_qualification["error"] == "migration_guard_blocked"


@pytest.mark.asyncio
async def test_promote_defers_on_migration_metadata_unfetchable(db_session, mocker):
    """A transient inability to fetch the migration metadata HOLDS the promotion
    (retryable) rather than merging blind — fail-safe, mirroring the other
    pre-merge guard paths."""
    _project, version = await _make_project_and_version(db_session)

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch("app.github.get_branch_sha", new=_branch_sha_mock())
    _is_anc, delete_branch_mock, _state = _patch_promote_github_defaults(mocker)
    mocker.patch("app.github.create_branch", new=_create_branch_mock())
    mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"number": 78, "html_url": "http://pr/78"}),
    )
    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(return_value={"merged": True}),
    )
    # The PR touches a migration file, but the diff fetch fails → guard "defer".
    mocker.patch(
        "app.github.get_pr_files",
        new=AsyncMock(
            return_value=[{"filename": "backend/alembic/versions/0042_x.py"}]
        ),
    )
    mocker.patch(
        "app.github.get_pull_request_diff",
        new=AsyncMock(side_effect=RuntimeError("502 from GitHub")),
    )

    result = await rq.promote(
        db_session,
        version,
        "abcd" * 10,
        composite={"confidence": 0.91},
        base_record={"candidate_sha": "abcd" * 10},
    )
    await db_session.commit()

    merge_mock.assert_not_called()
    assert result["status"] == "promotion_failed"
    assert result["error"] == "migration_guard_blocked"

    await db_session.refresh(version)
    assert version.status == "open"
    assert version.phase == "stabilize"
    assert version.last_qualification["error"] == "migration_guard_blocked"
    delete_branch_mock.assert_awaited()


@pytest.mark.asyncio
async def test_promote_proceeds_when_no_migrations_touched(db_session, mocker):
    """A healthy promotion whose PR touches NO migration files passes the guard
    (the default ``get_pr_files`` → []) and merges exactly as before."""
    _project, version = await _make_project_and_version(db_session)

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    merge_sha = "mainnew" * 5 + "00000"
    mocker.patch("app.github.get_branch_sha", new=_branch_sha_mock(main=merge_sha))
    _patch_promote_github_defaults(mocker)
    mocker.patch("app.github.create_branch", new=_create_branch_mock())
    mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"number": 79, "html_url": "http://pr/79"}),
    )
    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(return_value={"merged": True, "message": "Merged"}),
    )

    result = await rq.promote(
        db_session,
        version,
        "1234" * 10,
        composite={"confidence": 0.9},
    )
    await db_session.commit()

    # The guard passed (no migrations) — the merge ran and the promotion landed.
    merge_mock.assert_awaited_once()
    assert result["status"] == "promoted"
    assert result["next_version_number"] == 2

    await db_session.refresh(version)
    assert version.status == "promoted"
    assert version.promoted_sha == merge_sha


@pytest.mark.asyncio
async def test_promote_single_branch_repo_is_inert_lts_inactive(db_session, mocker):
    """On a single-branch repo (integration branch == promotion target) promote()
    must NOT open or merge a release PR into the very branch work lands on. It
    mirrors the back-merge ``lts_inactive`` guard: record an inert, already-landed
    success (the candidate is already live) and advance the version, with zero
    GitHub I/O."""
    project = Project(
        name="RQ-SingleBranch",
        user_id="rq-user",
        github_owner="acme",
        github_repo="solo",
        # Single-branch model: the integration branch IS the promotion target.
        integration_branch="trunk",
        promotion_target="trunk",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    version = await rv.open_version(db_session, project.id, version_number=1)
    await db_session.commit()
    await db_session.refresh(version)

    branch_mock = mocker.patch("app.github.create_branch", new=AsyncMock())
    pr_mock = mocker.patch("app.github.create_pull_request", new=AsyncMock())
    merge_mock = mocker.patch("app.github.merge_pull_request", new=AsyncMock())

    result = await rq.promote(
        db_session,
        version,
        "1234" * 10,
        composite={"confidence": 0.91},
    )
    await db_session.commit()

    assert result["status"] == "promoted"
    assert result["next_version_number"] == 2
    # No GitHub I/O whatsoever — the guard short-circuits before any branch/PR work.
    branch_mock.assert_not_called()
    pr_mock.assert_not_called()
    merge_mock.assert_not_called()

    await db_session.refresh(version)
    assert version.status == "promoted"
    assert version.promoted_sha == "1234" * 10
    assert version.last_qualification.get("lts_inactive") is True
    assert version.last_qualification.get("already_landed") is True


@pytest.mark.asyncio
async def test_promote_end_to_end_over_custom_branch_pair(db_session, mocker):
    """End-to-end qualify->promote->merge for a NON-self project whose
    integration/promotion branches are a CUSTOM pair (trunk -> release), not the
    self-repo automain/main fixture every other promote success test uses.

    Per-project branch resolution (audit C4): the release PR must open
    release-v{N} -> the project's promotion_target ('release', NOT 'main'); the
    idempotency ancestry check and the post-merge HEAD re-resolution must both
    use 'release'; and the release tag message must reference the custom
    integration -> promotion pair.  This proves the promote tail honours the
    per-project branch resolver rather than hardcoded literals."""
    project = Project(
        name="RQ-CustomPair",
        user_id="rq-user",
        github_owner="acme",
        github_repo="ext",
        # Custom multi-branch pair — distinct from the automain/main fixture.
        integration_branch="trunk",
        promotion_target="release",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    version = await rv.open_version(db_session, project.id, version_number=1)
    await db_session.commit()
    await db_session.refresh(version)

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    merge_sha = "release" * 5 + "00000"

    async def _branch_sha(token, owner, repo, branch):
        # promote() re-resolves the promotion target's HEAD after the merge —
        # it MUST ask for 'release', never the hardcoded 'main'.
        assert branch == "release", f"unexpected branch {branch!r}"
        return merge_sha

    mocker.patch("app.github.get_branch_sha", new=AsyncMock(side_effect=_branch_sha))
    is_ancestor_mock, _del_mock, _state_mock = _patch_promote_github_defaults(mocker)
    mocker.patch("app.github.create_branch", new=_create_branch_mock())
    create_mock = mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"number": 9, "html_url": "http://pr/9"}),
    )
    mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(return_value={"merged": True, "message": "Merged"}),
    )
    tag_mock = mocker.patch("app.github.create_tag", new=AsyncMock(return_value={}))

    result = await rq.promote(
        db_session, version, "abcd" * 10, composite={"confidence": 0.93}
    )
    await db_session.commit()

    assert result["status"] == "promoted"
    assert result["next_version_number"] == 2
    # The PR opens release-v1 -> the per-project promotion_target, NOT 'main'.
    assert create_mock.call_args.kwargs["head"] == "release-v1"
    assert create_mock.call_args.kwargs["base"] == "release"
    # The idempotency ancestry pre-check used the custom promotion target
    # (is_ancestor_of(token, owner, repo, candidate_sha, promotion_target, ...)).
    assert is_ancestor_mock.call_args.args[4] == "release"

    await db_session.refresh(version)
    assert version.status == "promoted"
    # promoted_sha is the re-resolved 'release' HEAD (asserted in _branch_sha).
    assert version.promoted_sha == merge_sha
    # The release tag message references the custom integration -> promotion pair.
    assert "trunk → release" in tag_mock.call_args.kwargs["message"]


@pytest.mark.asyncio
async def test_promote_success_snapshots_current_focus_as_next_goal(db_session, mocker):
    """The next version (N+1) opened by a successful promotion snapshots the
    project's current_focus into its ``goal``.

    Regression: ``_record_promotion_success`` referenced an undefined ``project``
    when reading current_focus, so the NameError was always swallowed and every
    promotion opened v(N+1) with goal=None instead of the real focus.
    """
    project, version = await _make_project_and_version(db_session)
    focus = "Promote v1 and stabilize the qualifier loop."
    project.problem_model = {"intent": {"current_focus": focus}}
    db_session.add(project)
    await db_session.commit()

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch("app.github.get_branch_sha", new=_branch_sha_mock())
    _patch_promote_github_defaults(mocker)
    mocker.patch("app.github.create_branch", new=_create_branch_mock())
    mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"number": 7, "html_url": "http://pr/7"}),
    )
    mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(return_value={"merged": True, "message": "Merged"}),
    )

    result = await rq.promote(
        db_session, version, "1234" * 10, composite={"confidence": 0.95}
    )
    await db_session.commit()

    assert result["status"] == "promoted"
    next_version = await rv.current_open_version(db_session, project.id)
    assert next_version is not None
    assert next_version.version_number == 2
    assert next_version.goal == focus


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
    _patch_promote_github_defaults(mocker)
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
    automain_mock = _branch_sha_mock(automain="movedon0" * 5, main=merge_sha)
    mocker.patch("app.github.get_branch_sha", new=automain_mock)
    _patch_promote_github_defaults(mocker)
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
    resolved_branches = {call.args[3] for call in automain_mock.call_args_list}
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

    candidate = "freshca0" * 5  # the commit we want to promote now
    stale_sha = "0ldbra0c" * 5  # what an existing release branch points at

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=_branch_sha_mock(main="merged00" * 5),
    )
    # Candidate not yet on main — a real promotion runs.
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=False))

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

    # delete_branch is called twice on this path: once to drop the STALE branch
    # before recreate, and once as the best-effort post-merge cleanup (R4-H2).
    assert delete_mock.call_count == 2
    # Every delete targeted the release-v1 branch.
    assert all(c.args[3] == "release-v1" for c in delete_mock.call_args_list)
    assert branch_mock.call_count == 2
    # Both create attempts targeted the candidate SHA — never the stale one.
    assert create_calls == [candidate, candidate]


@pytest.mark.asyncio
async def test_promote_release_branch_creation_failure_is_blocked(db_session, mocker):
    """Audit C4: a failure creating the release branch blocks the promotion —
    no PR is opened, the version returns to stabilize, and main is untouched."""
    project, version = await _make_project_and_version(db_session)

    from fastapi import HTTPException

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    # Candidate not on main (idempotency pre-check returns False) so promote()
    # proceeds to the branch-create step, which fails.
    _patch_promote_github_defaults(mocker)
    mocker.patch(
        "app.github.create_branch",
        new=AsyncMock(side_effect=HTTPException(status_code=502, detail="GitHub 502")),
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

    candidate = "cand0000" * 5  # 40-char placeholder SHA
    merge_commit = "mergecmt" * 5  # 40-char placeholder SHA

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=_branch_sha_mock(main=merge_commit),
    )
    _patch_promote_github_defaults(mocker)
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
    assert tag_mock.call_args.args[3] == "v1"  # tag name
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
    # is_ancestor_of=False: candidate not on main, real merge runs.  The
    # post-merge main-HEAD re-resolve then fails (the _sha mock above), and
    # promote() degrades promoted_sha to the candidate.
    _patch_promote_github_defaults(mocker)
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
    """End-to-end: when _finalize_qualification cannot load the Project row,
    last_qualification must NOT be left 'pending' — a later finalize would
    otherwise re-loop forever.

    _finalize_qualification re-resolves CI before scoring; that re-resolution
    needs the Project row to resolve credentials.  With the project gone CI
    cannot be re-resolved, so it degrades to not-green (graceful — never
    crashes), the composite hard-blocks on ci_not_green, and the version is
    finalized as not_promoted.  The pending loop is still broken: the record
    reaches a terminal status rather than staying 'pending'."""
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

    # Make every Project lookup return None — simulates the project deleted
    # between qualification spawn and finalize.
    real_get = db_session.get

    async def _fake_get(model, ident, *args, **kwargs):
        if model is Project:
            return None
        return await real_get(model, ident, *args, **kwargs)

    mocker.patch.object(db_session, "get", new=AsyncMock(side_effect=_fake_get))

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    # _resolve_ci_at_finalize degrades to not-green + state=_CI_PENDING when the
    # project is gone, so the composite hard-blocks on ci_unresolved (not
    # ci_not_green — the build was never evaluated, CI merely could not be read).
    assert result["status"] == "not_promoted"
    assert result["blocker"] == "ci_unresolved"

    await db_session.refresh(version)
    # The pending loop is broken — status is terminal.
    assert version.last_qualification["status"] == "not_promoted"
    assert version.last_qualification["status"] != "pending"


# ===========================================================================
# _finalize_qualification — infra-failure detection (FAILED/CANCELLED children)
# ===========================================================================


async def _seed_infra_failure_children(
    db_session,
    project,
    version,
    *,
    audit_status: TaskStatus,
    smoke_status: TaskStatus = TaskStatus.COMPLETED,
    smoketest_enabled: bool = True,
    prior_infra_streak: int = 0,
) -> tuple[Task, Task | None]:
    """Create qualifier child tasks in requested statuses (no artifacts).

    Use for infra-failure scenarios where children did not produce artifacts.
    Stamps version.last_qualification as pending pointing to the children.
    """
    audit_task = Task(
        project_id=project.id,
        title="audit",
        payloads=[{"type": "release_audit", "base_branch": "main"}],
        vm_instance_id="vm-audit-infra",
        status=audit_status,
        context={"release_version_id": str(version.id)},
    )
    db_session.add(audit_task)
    smoke_task: Task | None = None
    if smoketest_enabled:
        smoke_task = Task(
            project_id=project.id,
            title="smoke",
            payloads=[{"type": "smoketest", "base_branch": "automain"}],
            vm_instance_id="vm-smoke-infra",
            status=smoke_status,
            context={"release_version_id": str(version.id)},
        )
        db_session.add(smoke_task)
    await db_session.commit()
    await db_session.refresh(audit_task)
    if smoke_task is not None:
        await db_session.refresh(smoke_task)

    version.last_qualification = {
        "status": "pending",
        "candidate_sha": "dead" * 10,
        "ci_green": True,
        "smoketest_enabled": smoketest_enabled,
        "started_at": "2026-05-19T00:00:00",
        "audit_task_id": str(audit_task.id),
        "smoketest_task_id": str(smoke_task.id) if smoke_task else None,
        "prior_infra_streak": prior_infra_streak,
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)
    return audit_task, smoke_task


class TestFinalizeInfraFailure:
    """_finalize_qualification correctly distinguishes infra failures from verdicts."""

    @pytest.mark.asyncio
    async def test_audit_task_failed_records_infra_failure_not_verdict(
        self, db_session
    ):
        """Audit FAILED → status='infra_failure', NO QualificationVerdict row.
        Version returns to stabilize but is NOT blocked on a quality finding."""
        project, version = await _make_project_and_version(db_session)
        await _seed_infra_failure_children(
            db_session,
            project,
            version,
            audit_status=TaskStatus.FAILED,
            smoke_status=TaskStatus.COMPLETED,
        )

        result = await rq._finalize_qualification(db_session, version)
        await db_session.commit()

        assert result["status"] == "infra_failure"
        assert "audit_task_failed" in result["error"]

        # Version back to stabilize — no quality judgement recorded.
        await db_session.refresh(version)
        assert version.status == "open"
        assert version.phase == "stabilize"
        assert version.last_qualification["status"] == "infra_failure"
        assert version.last_qualification["error"] == "audit_task_failed"

        # No QualificationVerdict row — the qualifier never rendered a verdict.
        verdicts = (
            (
                await db_session.execute(
                    select(QualificationVerdict).where(
                        QualificationVerdict.release_version_id == version.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(verdicts) == 0

        # An operator notification was posted with release_infra_failure event.
        msgs = (
            (
                await db_session.execute(
                    select(ProjectMessage).where(
                        ProjectMessage.project_id == project.id,
                        ProjectMessage.context["event_type"].as_string()
                        == "release_infra_failure",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(msgs) == 1

    @pytest.mark.asyncio
    async def test_audit_task_cancelled_records_infra_failure(self, db_session):
        """Audit CANCELLED also routes to the infra-failure path."""
        project, version = await _make_project_and_version(db_session)
        await _seed_infra_failure_children(
            db_session,
            project,
            version,
            audit_status=TaskStatus.CANCELLED,
            smoke_status=TaskStatus.COMPLETED,
        )

        result = await rq._finalize_qualification(db_session, version)
        await db_session.commit()

        assert result["status"] == "infra_failure"
        assert "audit_task_cancelled" in result["error"]

        await db_session.refresh(version)
        assert version.last_qualification["status"] == "infra_failure"
        assert version.last_qualification["error"] == "audit_task_cancelled"

        # No verdict row.
        count = (
            await db_session.execute(
                select(sa_func.count()).where(
                    QualificationVerdict.release_version_id == version.id
                )
            )
        ).scalar()
        assert count == 0

    @pytest.mark.asyncio
    async def test_smoketest_task_failed_records_infra_failure_when_enabled(
        self, db_session
    ):
        """When smoketest_enabled=True and smoketest FAILED, infra_failure is
        recorded — NOT a quality verdict (even though audit may be COMPLETED)."""
        project, version = await _make_project_and_version(db_session)
        # Audit completed (no quality issue), smoketest FAILED (infra failure).
        await _seed_infra_failure_children(
            db_session,
            project,
            version,
            audit_status=TaskStatus.COMPLETED,
            smoke_status=TaskStatus.FAILED,
            smoketest_enabled=True,
        )

        result = await rq._finalize_qualification(db_session, version)
        await db_session.commit()

        assert result["status"] == "infra_failure"
        assert "smoketest_task_failed" in result["error"]

        await db_session.refresh(version)
        assert version.last_qualification["status"] == "infra_failure"

        # No verdict row even though audit completed.
        count = (
            await db_session.execute(
                select(sa_func.count()).where(
                    QualificationVerdict.release_version_id == version.id
                )
            )
        ).scalar()
        assert count == 0

    @pytest.mark.asyncio
    async def test_infra_failure_does_not_appear_in_promotion_readiness(
        self, db_session
    ):
        """An infra-failure finalization writes no QualificationVerdict row, so
        compute_promotion_readiness sees verdicts_evaluated == 0 (the infra
        failure does not pollute the dry-run track record)."""
        project, version = await _make_project_and_version(db_session)
        await _seed_infra_failure_children(
            db_session,
            project,
            version,
            audit_status=TaskStatus.FAILED,
            smoke_status=TaskStatus.COMPLETED,
        )

        await rq._finalize_qualification(db_session, version)
        await db_session.commit()

        readiness = await rq.compute_promotion_readiness(db_session, project.id)
        assert readiness["verdicts_evaluated"] == 0

    @pytest.mark.asyncio
    async def test_audit_completed_no_report_reclassified_as_infra_failure(
        self, db_session
    ):
        """A COMPLETED audit task whose RELEASE_AUDIT_REPORT artifact is absent
        is now reclassified as infra_failure — the agent ran but emitted no
        usable output.  No QualificationVerdict row is written; the operator
        notification is 'release_infra_failure', not 'release_blocked'."""
        project, version = await _make_project_and_version(db_session)
        # Both children COMPLETED but NO artifacts seeded — audit produced
        # no report, which is an infra failure (defense-in-depth path).
        audit_task, _ = await _seed_infra_failure_children(
            db_session,
            project,
            version,
            audit_status=TaskStatus.COMPLETED,
            smoke_status=TaskStatus.COMPLETED,
            smoketest_enabled=True,
        )

        result = await rq._finalize_qualification(db_session, version)
        await db_session.commit()

        # Reclassified as infra_failure, not a quality verdict.
        assert result["status"] == "infra_failure"
        assert result["error"] == "audit_report_missing"

        # NO QualificationVerdict row written — infra failure, not quality blocker.
        count = (
            await db_session.execute(
                select(sa_func.count()).where(
                    QualificationVerdict.release_version_id == version.id
                )
            )
        ).scalar()
        assert count == 0

    @pytest.mark.asyncio
    async def test_audit_completed_empty_artifact_reclassified_as_infra_failure(
        self, db_session
    ):
        """A COMPLETED audit task with an empty JSON artifact is infra_failure."""
        project, version = await _make_project_and_version(db_session)
        audit_task, _ = await _seed_infra_failure_children(
            db_session,
            project,
            version,
            audit_status=TaskStatus.COMPLETED,
            smoke_status=TaskStatus.COMPLETED,
            smoketest_enabled=True,
        )
        db_session.add(
            Artifact(
                task_id=audit_task.id,
                artifact_type=ArtifactType.RELEASE_AUDIT_REPORT,
                logs="{}",
            )
        )
        await db_session.commit()
        await db_session.refresh(version)

        result = await rq._finalize_qualification(db_session, version)
        await db_session.commit()

        assert result["status"] == "infra_failure"
        assert result["error"] == "audit_report_empty_dict"

        count = (
            await db_session.execute(
                select(sa_func.count()).where(
                    QualificationVerdict.release_version_id == version.id
                )
            )
        ).scalar()
        assert count == 0

    @pytest.mark.asyncio
    async def test_audit_completed_unparseable_artifact_reclassified_as_infra_failure(
        self, db_session
    ):
        """A COMPLETED audit task whose RELEASE_AUDIT_REPORT artifact contains
        non-JSON logs (unparseable) is reclassified as infra_failure — distinct
        regression test from the 'no artifact row' sub-case above."""
        project, version = await _make_project_and_version(db_session)
        audit_task, _ = await _seed_infra_failure_children(
            db_session,
            project,
            version,
            audit_status=TaskStatus.COMPLETED,
            smoke_status=TaskStatus.COMPLETED,
            smoketest_enabled=True,
        )
        # Seed a RELEASE_AUDIT_REPORT artifact with non-JSON content.
        db_session.add(
            Artifact(
                task_id=audit_task.id,
                artifact_type=ArtifactType.RELEASE_AUDIT_REPORT,
                logs="this is not valid json {{{{",
            )
        )
        await db_session.commit()
        await db_session.refresh(version)

        result = await rq._finalize_qualification(db_session, version)
        await db_session.commit()

        assert result["status"] == "infra_failure"
        assert result["error"] == "audit_report_missing"

        count = (
            await db_session.execute(
                select(sa_func.count()).where(
                    QualificationVerdict.release_version_id == version.id
                )
            )
        ).scalar()
        assert count == 0

    @pytest.mark.asyncio
    async def test_audit_completed_parseable_but_unscoreable_reclassified_as_infra_failure(
        self, db_session
    ):
        """Structured but unscorable audit findings are infra_failure, not a verdict."""
        project, version = await _make_project_and_version(db_session)
        await _seed_qualification_children(
            db_session,
            project,
            version,
            audit_logs={
                "audit_score": 0.95,
                "findings": [
                    {"description": "missing severity"},
                    "plain-text finding with no structured severity",
                ],
                "test_erosion": {
                    "deleted_test_files": [],
                    "assertion_delta": 0,
                    "test_count_delta": 0,
                },
            },
            smoke_logs={
                "passed": True,
                "tests": [{"name": "smoke-c", "status": "pass"}],
                "suites": {"startup": {"passed": True}},
            },
        )

        result = await rq._finalize_qualification(db_session, version)
        await db_session.commit()

        assert result["status"] == "infra_failure"
        assert result["error"] == "audit_report_parseable_but_unscoreable"

        await db_session.refresh(version)
        assert version.last_qualification["status"] == "infra_failure"
        assert (
            version.last_qualification["error"]
            == "audit_report_parseable_but_unscoreable"
        )

        count = (
            await db_session.execute(
                select(sa_func.count()).where(
                    QualificationVerdict.release_version_id == version.id
                )
            )
        ).scalar()
        assert count == 0

    @pytest.mark.asyncio
    async def test_legitimate_empty_findings_audit_reaches_verdict_not_infra_failure(
        self, db_session
    ):
        """An audit report with findings=[] and a valid audit_score reaches a real verdict.

        Regression guard: if _audit_missing_reason() were changed to return
        'parseable_but_unscoreable' for empty-findings-with-score reports, the
        finalize path would silently mis-route a clean audit as infra_failure.
        This test ensures that path stays open — a QualificationVerdict row is
        written and the audit sub-score reflects the clean signal (1.0).
        """
        from app import shared_state

        shared_state.runtime_config["promotion_enabled"] = False

        project, version = await _make_project_and_version(db_session)
        await _seed_qualification_children(
            db_session,
            project,
            version,
            audit_logs={
                "audit_score": 1.0,
                "findings": [],
                "test_erosion": {
                    "deleted_test_files": [],
                    "assertion_delta": 0,
                    "test_count_delta": 0,
                },
            },
            smoke_logs={
                "passed": True,
                "tests": [{"name": "smoke-ok", "status": "pass"}],
                "suites": {"startup": {"passed": True}},
            },
        )

        result = await rq._finalize_qualification(db_session, version)
        await db_session.commit()

        # Primary guard: legitimate empty findings must NOT route to infra_failure.
        assert result["status"] != "infra_failure", (
            f"empty-findings clean audit mis-routed as infra_failure: {result}"
        )
        # A real QualificationVerdict row should be written.
        count = (
            await db_session.execute(
                select(sa_func.count()).where(
                    QualificationVerdict.release_version_id == version.id
                )
            )
        ).scalar()
        assert count == 1, f"expected 1 QualificationVerdict, got {count}"

        # Audit sub-score must reflect the clean 1.0 signal, not collapse.
        await db_session.refresh(version)
        qual = version.last_qualification
        assert qual["sub_scores"]["audit"] == 1.0, (
            f"audit sub-score collapsed: {qual['sub_scores']['audit']}, expected 1.0"
        )
        # A clean audit is not a hard-blocking dimension.
        assert result["would_promote"] is True, (
            f"would_promote should be True for a clean audit, got {result}"
        )

    @pytest.mark.asyncio
    async def test_smoketest_completed_no_report_reclassified_as_infra_failure(
        self, db_session
    ):
        """A COMPLETED smoketest with no SMOKETEST_RESULT is infra_failure,
        not a smoketest_startup_failed verdict."""
        project, version = await _make_project_and_version(db_session)
        await _seed_infra_failure_children(
            db_session,
            project,
            version,
            audit_status=TaskStatus.COMPLETED,
            smoke_status=TaskStatus.COMPLETED,
            smoketest_enabled=True,
        )

        db_session.add(
            Artifact(
                task_id=version.last_qualification["audit_task_id"],
                artifact_type=ArtifactType.RELEASE_AUDIT_REPORT,
                logs=json.dumps(
                    {
                        "audit_score": 1.0,
                        "test_erosion": {"deleted_test_files": []},
                    }
                ),
            )
        )
        await db_session.commit()
        await db_session.refresh(version)

        result = await rq._finalize_qualification(db_session, version)
        await db_session.commit()

        assert result["status"] == "infra_failure"
        assert result["error"] == "smoketest_report_missing"

        count = (
            await db_session.execute(
                select(sa_func.count()).where(
                    QualificationVerdict.release_version_id == version.id
                )
            )
        ).scalar()
        assert count == 0

    @pytest.mark.asyncio
    async def test_smoketest_completed_unparseable_artifact_reclassified_as_infra_failure(
        self, db_session
    ):
        """A COMPLETED smoketest with an unparseable SMOKETEST_RESULT is infra_failure."""
        project, version = await _make_project_and_version(db_session)
        audit_task, smoke_task = await _seed_infra_failure_children(
            db_session,
            project,
            version,
            audit_status=TaskStatus.COMPLETED,
            smoke_status=TaskStatus.COMPLETED,
            smoketest_enabled=True,
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
                logs="not json {{{",
            )
        )
        await db_session.commit()
        await db_session.refresh(version)

        result = await rq._finalize_qualification(db_session, version)
        await db_session.commit()

        assert result["status"] == "infra_failure"
        assert result["error"] == "smoketest_report_missing"

        count = (
            await db_session.execute(
                select(sa_func.count()).where(
                    QualificationVerdict.release_version_id == version.id
                )
            )
        ).scalar()
        assert count == 0

    @pytest.mark.asyncio
    async def test_consecutive_infra_errors_backstop_fires_alert(self, db_session):
        """After _MIN_INFRA_ERROR_STREAK consecutive infra failures, an Alert is
        raised — the calcification guard is silent for infra failures, so this
        backstop ensures a persistently broken qualifier pipeline surfaces."""
        project, version = await _make_project_and_version(db_session)

        # Simulate two prior infra failures already recorded (streak = 2).
        # The third failure (current_streak = 3 == _MIN_INFRA_ERROR_STREAK)
        # should trigger the alert.
        await _seed_infra_failure_children(
            db_session,
            project,
            version,
            audit_status=TaskStatus.FAILED,
            smoke_status=TaskStatus.COMPLETED,
            prior_infra_streak=rq._MIN_INFRA_ERROR_STREAK - 1,
        )

        result = await rq._finalize_qualification(db_session, version)
        await db_session.commit()

        assert result["status"] == "infra_failure"
        assert result["consecutive_infra_errors"] == rq._MIN_INFRA_ERROR_STREAK

        # An Alert was raised for the consecutive infra-failure backstop.
        alerts = (
            (
                await db_session.execute(
                    select(Alert).where(Alert.project_id == project.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(alerts) == 1
        assert "infrastructure" in alerts[0].title.lower()
        assert str(rq._MIN_INFRA_ERROR_STREAK) in alerts[0].title

    @pytest.mark.asyncio
    async def test_infra_failure_streak_resets_after_quality_verdict(self, db_session):
        """The infra-failure streak resets to 0 when a non-infra finalization
        (a real quality verdict) occurs between infra failures."""
        from app import shared_state

        shared_state.runtime_config["promotion_enabled"] = False

        project, version = await _make_project_and_version(db_session)

        # Run a real quality verdict (not_promoted) to reset the streak.
        await _seed_qualification_children(
            db_session,
            project,
            version,
            audit_logs={
                "audit_score": 0.5,
                "test_erosion": {"deleted_test_files": [], "assertion_delta": 0},
            },
            smoke_logs={
                "passed": False,
                "tests": [{"name": "a", "status": "fail"}],
                "suites": {"startup": {"passed": True}},
            },
        )
        await rq._finalize_qualification(db_session, version)
        await db_session.commit()

        # Re-open for another qualification cycle (simulate new run_qualification).
        # After a non-infra verdict, run_qualification should carry prior_infra_streak=0.
        await db_session.refresh(version)
        assert version.last_qualification["status"] in ("not_promoted", "dry_run")

        # Now manually simulate the next run_qualification stamping prior_infra_streak=0
        # (because the prior status was not infra_failure).
        prior_qual = version.last_qualification or {}
        prior_streak = (
            int(prior_qual.get("consecutive_infra_errors") or 0)
            if prior_qual.get("status") == "infra_failure"
            else 0
        )
        assert prior_streak == 0  # streak reset after a real verdict

    @pytest.mark.asyncio
    async def test_smoketest_task_failed_when_disabled_does_not_route_infra_failure(
        self, db_session
    ):
        """When smoketest_enabled=False, the smoketest child is not required.
        A COMPLETED audit (even with an absent smoketest artifact) routes through
        the normal compute_composite path — NOT the infra-failure path."""
        from app import shared_state

        shared_state.runtime_config["promotion_enabled"] = False

        project, version = await _make_project_and_version(db_session)
        # Audit COMPLETED with a valid report; smoketest disabled.
        audit_task = Task(
            project_id=project.id,
            title="audit",
            payloads=[{"type": "release_audit", "base_branch": "main"}],
            vm_instance_id="vm-audit-smoke-disabled",
            status=TaskStatus.COMPLETED,
            context={"release_version_id": str(version.id)},
        )
        db_session.add(audit_task)
        await db_session.commit()
        await db_session.refresh(audit_task)

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
        version.last_qualification = {
            "status": "pending",
            "candidate_sha": "cafe" * 10,
            "ci_green": True,
            "smoketest_enabled": False,
            "started_at": "2026-05-19T00:00:00",
            "audit_task_id": str(audit_task.id),
            "smoketest_task_id": None,
            "prior_infra_streak": 0,
        }
        db_session.add(version)
        await db_session.commit()
        await db_session.refresh(version)

        result = await rq._finalize_qualification(db_session, version)
        await db_session.commit()

        # Took the normal quality-verdict path (dry_run since promotion disabled).
        assert result["status"] == "dry_run"
        assert result["status"] != "infra_failure"

    @pytest.mark.asyncio
    async def test_audit_task_failed_enriches_error_with_failure_category(
        self, db_session
    ):
        """FAILED audit with agent_failure_category_raw → enriched error label.

        Ensures operators can distinguish audit infra failures by the specific
        agent failure category (e.g. rate_limited vs. claude_code_failed) when
        inspecting consecutive infra-failure alerts.
        """
        project, version = await _make_project_and_version(db_session)
        audit_task = Task(
            project_id=project.id,
            title="audit",
            payloads=[{"type": "release_audit", "base_branch": "main"}],
            vm_instance_id="vm-audit-enriched",
            status=TaskStatus.FAILED,
            context={
                "release_version_id": str(version.id),
                "agent_failure_category_raw": "release_audit_report_unparseable",
            },
        )
        db_session.add(audit_task)
        await db_session.commit()
        await db_session.refresh(audit_task)

        version.last_qualification = {
            "status": "pending",
            "candidate_sha": "cafe" * 10,
            "ci_green": True,
            "smoketest_enabled": False,
            "started_at": "2026-05-19T00:00:00",
            "audit_task_id": str(audit_task.id),
            "smoketest_task_id": None,
            "prior_infra_streak": 0,
        }
        db_session.add(version)
        await db_session.commit()
        await db_session.refresh(version)

        result = await rq._finalize_qualification(db_session, version)
        await db_session.commit()

        assert result["status"] == "infra_failure"
        assert result["error"] == "audit_task_failed_release_audit_report_unparseable"

        await db_session.refresh(version)
        assert version.last_qualification["status"] == "infra_failure"
        assert (
            version.last_qualification["error"]
            == "audit_task_failed_release_audit_report_unparseable"
        )

    @pytest.mark.asyncio
    async def test_audit_task_failed_no_failure_category_falls_back_to_generic(
        self, db_session
    ):
        """FAILED audit with no agent_failure_category_raw → generic label.

        Ensures backward compatibility when older agents didn't record the
        failure category — the label stays 'audit_task_failed'.
        """
        project, version = await _make_project_and_version(db_session)
        await _seed_infra_failure_children(
            db_session,
            project,
            version,
            audit_status=TaskStatus.FAILED,
            smoke_status=TaskStatus.COMPLETED,
            smoketest_enabled=False,
        )

        result = await rq._finalize_qualification(db_session, version)
        await db_session.commit()

        assert result["status"] == "infra_failure"
        assert result["error"] == "audit_task_failed"

        await db_session.refresh(version)
        assert version.last_qualification["error"] == "audit_task_failed"

    @pytest.mark.asyncio
    async def test_smoketest_task_failed_enriches_error_with_failure_category(
        self, db_session
    ):
        """FAILED smoketest with agent_failure_category_raw → enriched error label."""
        project, version = await _make_project_and_version(db_session)
        audit_task, smoke_task = await _seed_infra_failure_children(
            db_session,
            project,
            version,
            audit_status=TaskStatus.COMPLETED,
            smoke_status=TaskStatus.FAILED,
            smoketest_enabled=True,
        )
        # Patch the smoketest task's context to include a failure category.
        smoke_task.context = {
            **dict(smoke_task.context or {}),
            "agent_failure_category_raw": "rate_limited",
        }
        db_session.add(smoke_task)
        await db_session.commit()
        await db_session.refresh(smoke_task)
        await db_session.refresh(version)

        result = await rq._finalize_qualification(db_session, version)
        await db_session.commit()

        assert result["status"] == "infra_failure"
        assert result["error"] == "smoketest_task_failed_rate_limited"

        await db_session.refresh(version)
        assert (
            version.last_qualification["error"] == "smoketest_task_failed_rate_limited"
        )

    @pytest.mark.asyncio
    async def test_infra_failure_dispatches_external_webhook(self, db_session, mocker):
        """_handle_infra_failure fires an external webhook with RELEASE_INFRA_FAILURE."""
        import asyncio

        dispatch_mock = AsyncMock()
        mocker.patch(
            "app.release_qualifier.dispatch_external_notification",
            dispatch_mock,
        )

        project, version = await _make_project_and_version(db_session)
        await _seed_infra_failure_children(
            db_session,
            project,
            version,
            audit_status=TaskStatus.FAILED,
            smoke_status=TaskStatus.COMPLETED,
        )

        await rq._finalize_qualification(db_session, version)
        await db_session.commit()
        # Drain ensure_future'd coroutine.
        await asyncio.sleep(0)

        dispatch_mock.assert_called_once()
        call_args = dispatch_mock.call_args
        assert call_args[0][1] == "RELEASE_INFRA_FAILURE"
        payload = call_args[0][2]
        assert payload["consecutive_infra_errors"] >= 1
        assert "version_number" in payload
        assert "infra_error" in payload

    @pytest.mark.asyncio
    async def test_infra_failure_streak_in_webhook_payload(self, db_session, mocker):
        """Webhook payload carries the correct consecutive_infra_errors streak count."""
        import asyncio

        dispatch_mock = AsyncMock()
        mocker.patch(
            "app.release_qualifier.dispatch_external_notification",
            dispatch_mock,
        )

        project, version = await _make_project_and_version(db_session)
        await _seed_infra_failure_children(
            db_session,
            project,
            version,
            audit_status=TaskStatus.FAILED,
            smoke_status=TaskStatus.COMPLETED,
            prior_infra_streak=1,
        )

        await rq._finalize_qualification(db_session, version)
        await db_session.commit()
        await asyncio.sleep(0)

        dispatch_mock.assert_called_once()
        payload = dispatch_mock.call_args[0][2]
        assert payload["consecutive_infra_errors"] == 2

    @pytest.mark.asyncio
    async def test_audit_rate_limited_respawns_new_task(self, db_session):
        """FAILED audit classified RATE_LIMIT → respawn, not infra_failure.

        A transient rate-limit failure must not pollute the consecutive_infra_errors
        streak or waste the candidate_sha snapshot.  The qualifier re-spawns a new
        audit task carrying the same candidate_sha and returns
        status='audit_rate_limit_respawned' instead of routing to _handle_infra_failure.
        """
        from app.failure_classification import RATE_LIMIT, persist_failure_category

        project, version = await _make_project_and_version(db_session)
        audit_task = Task(
            project_id=project.id,
            title="audit",
            payloads=[
                {
                    "type": "release_audit",
                    "base_branch": "main",
                    "diff_range": "main...abcd1234",
                    "candidate_sha": "abcd1234" * 5,
                }
            ],
            vm_instance_id="vm-audit-rate-limited",
            status=TaskStatus.FAILED,
            context={"release_version_id": str(version.id)},
        )
        db_session.add(audit_task)
        await db_session.commit()
        await db_session.refresh(audit_task)

        persist_failure_category(audit_task, RATE_LIMIT)
        db_session.add(audit_task)
        await db_session.commit()
        await db_session.refresh(audit_task)

        original_audit_id = str(audit_task.id)
        candidate = "abcd1234" * 5

        version.last_qualification = {
            "status": "pending",
            "candidate_sha": candidate,
            "ci_green": True,
            "smoketest_enabled": False,
            "started_at": "2026-05-19T00:00:00",
            "audit_task_id": original_audit_id,
            "smoketest_task_id": None,
            "prior_infra_streak": 0,
        }
        db_session.add(version)
        await db_session.commit()
        await db_session.refresh(version)

        result = await rq._finalize_qualification(db_session, version)
        await db_session.commit()

        # Returns the respawn status, not infra_failure.
        assert result["status"] == "audit_rate_limit_respawned"
        assert result["respawn_count"] == 1

        # last_qualification carries the new audit_task_id and the counter.
        await db_session.refresh(version)
        new_qual = version.last_qualification
        assert new_qual["audit_rl_respawn_count"] == 1
        assert new_qual["audit_task_id"] != original_audit_id

        # No QualificationVerdict row — verdict was not rendered.
        count = (
            await db_session.execute(
                select(sa_func.count()).where(
                    QualificationVerdict.release_version_id == version.id
                )
            )
        ).scalar()
        assert count == 0

        # No infra_failure ProjectMessage — this is a transient respawn, not a
        # hard pipeline failure.
        infra_msg_count = (
            await db_session.execute(
                select(sa_func.count()).where(
                    ProjectMessage.project_id == project.id,
                    ProjectMessage.context["event_type"].as_string()
                    == "release_infra_failure",
                )
            )
        ).scalar()
        assert infra_msg_count == 0

    @pytest.mark.asyncio
    async def test_audit_rate_limited_cap_exceeded_routes_to_infra_failure(
        self, db_session
    ):
        """When audit_rl_respawn_count hits _MAX_AUDIT_RL_RESPAWNS, fall through to
        infra_failure with label 'audit_rate_limit_respawn_cap_exceeded'."""
        from app.failure_classification import RATE_LIMIT, persist_failure_category
        from app.release_qualifier import _MAX_AUDIT_RL_RESPAWNS

        project, version = await _make_project_and_version(db_session)
        audit_task = Task(
            project_id=project.id,
            title="audit",
            payloads=[
                {
                    "type": "release_audit",
                    "base_branch": "main",
                    "diff_range": "main...cafe5678",
                    "candidate_sha": "cafe5678" * 5,
                }
            ],
            vm_instance_id="vm-audit-rl-cap",
            status=TaskStatus.FAILED,
            context={"release_version_id": str(version.id)},
        )
        db_session.add(audit_task)
        await db_session.commit()
        await db_session.refresh(audit_task)

        persist_failure_category(audit_task, RATE_LIMIT)
        db_session.add(audit_task)
        await db_session.commit()
        await db_session.refresh(audit_task)

        version.last_qualification = {
            "status": "pending",
            "candidate_sha": "cafe5678" * 5,
            "ci_green": True,
            "smoketest_enabled": False,
            "started_at": "2026-05-19T00:00:00",
            "audit_task_id": str(audit_task.id),
            "smoketest_task_id": None,
            "prior_infra_streak": 0,
            # At the cap — next failure must route to infra_failure.
            "audit_rl_respawn_count": _MAX_AUDIT_RL_RESPAWNS,
        }
        db_session.add(version)
        await db_session.commit()
        await db_session.refresh(version)

        result = await rq._finalize_qualification(db_session, version)
        await db_session.commit()

        assert result["status"] == "infra_failure"
        assert result["error"] == "audit_rate_limit_respawn_cap_exceeded"

        await db_session.refresh(version)
        assert version.phase == "stabilize"
        assert version.last_qualification["status"] == "infra_failure"
        assert (
            version.last_qualification["error"]
            == "audit_rate_limit_respawn_cap_exceeded"
        )

    # -----------------------------------------------------------------------
    # Malformed audit artifact shapes — each must produce either a real verdict
    # OR an infra_failure; never both, never neither, never a silent clean score.
    # -----------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_empty_dict_audit_report_routes_to_infra_failure(self, db_session):
        """audit_report = {} (empty dict) must route to infra_failure.

        Without the empty-dict guard _score_audit() returns (1.0, 1.0) with no
        deductions applied — a silent clean score.  Verify infra_failure is
        returned and no QualificationVerdict row is written."""
        project, version = await _make_project_and_version(db_session)
        await _seed_qualification_children(
            db_session,
            project,
            version,
            audit_logs={},
            smoke_logs={
                "passed": True,
                "tests": [{"name": "smoke-ok", "status": "pass"}],
                "suites": {"startup": {"passed": True}},
            },
        )

        result = await rq._finalize_qualification(db_session, version)
        await db_session.commit()

        assert result["status"] == "infra_failure"
        assert result["error"] == "audit_report_empty_dict"

        await db_session.refresh(version)
        assert version.last_qualification["status"] == "infra_failure"
        assert version.last_qualification["error"] == "audit_report_empty_dict"

        count = (
            await db_session.execute(
                select(sa_func.count()).where(
                    QualificationVerdict.release_version_id == version.id
                )
            )
        ).scalar()
        assert count == 0

    @pytest.mark.asyncio
    async def test_findings_list_no_audit_score_field_reaches_verdict(self, db_session):
        """audit_report = {"findings": []} with no audit_score field reaches a verdict.

        An empty findings list with no self-reported score is a legitimately
        clean audit — _audit_missing_reason returns "audit_score_null_no_findings"
        which does NOT trigger infra_failure.  A QualificationVerdict row is
        written and the audit sub-score is 1.0 (no deductions applied)."""
        from app import shared_state

        shared_state.runtime_config["promotion_enabled"] = False

        project, version = await _make_project_and_version(db_session)
        await _seed_qualification_children(
            db_session,
            project,
            version,
            audit_logs={
                # Deliberately omit "audit_score" — this is the variant under test.
                "findings": [],
                "test_erosion": {
                    "deleted_test_files": [],
                    "assertion_delta": 0,
                    "test_count_delta": 0,
                },
            },
            smoke_logs={
                "passed": True,
                "tests": [{"name": "smoke-ok", "status": "pass"}],
                "suites": {"startup": {"passed": True}},
            },
        )

        result = await rq._finalize_qualification(db_session, version)
        await db_session.commit()

        assert result["status"] != "infra_failure", (
            f"empty-findings-no-score mis-routed as infra_failure: {result}"
        )

        await db_session.refresh(version)
        qual = version.last_qualification
        assert qual.get("audit_missing_reason") == "audit_score_null_no_findings"
        assert qual["sub_scores"]["audit"] == 1.0

        count = (
            await db_session.execute(
                select(sa_func.count()).where(
                    QualificationVerdict.release_version_id == version.id
                )
            )
        ).scalar()
        assert count == 1, f"expected 1 QualificationVerdict row, got {count}"

    @pytest.mark.asyncio
    async def test_findings_entries_no_severity_routes_to_infra_failure(
        self, db_session
    ):
        """audit_report with findings that have no severity/level field → infra_failure.

        A single finding dict missing severity/level is "parseable_but_unscoreable" —
        the agent produced structured output but _score_audit() cannot apply any
        deduction.  No QualificationVerdict row is written."""
        project, version = await _make_project_and_version(db_session)
        await _seed_qualification_children(
            db_session,
            project,
            version,
            audit_logs={
                "findings": [{"description": "finding with no severity field"}],
                "test_erosion": {"deleted_test_files": [], "assertion_delta": 0},
            },
            smoke_logs={
                "passed": True,
                "tests": [{"name": "smoke-ok", "status": "pass"}],
                "suites": {"startup": {"passed": True}},
            },
        )

        result = await rq._finalize_qualification(db_session, version)
        await db_session.commit()

        assert result["status"] == "infra_failure"
        assert result["error"] == "audit_report_parseable_but_unscoreable"

        count = (
            await db_session.execute(
                select(sa_func.count()).where(
                    QualificationVerdict.release_version_id == version.id
                )
            )
        ).scalar()
        assert count == 0

    @pytest.mark.asyncio
    async def test_issues_key_equivalent_to_findings_reaches_verdict(self, db_session):
        """audit_report with "issues" key (not "findings") reaches a real verdict.

        The "issues" fallback is recognized by _score_audit and _audit_missing_reason.
        A low-severity finding applies a deduction so audit_score < 1.0 (not a
        silent clean score).  A QualificationVerdict row is written."""
        from app import shared_state

        shared_state.runtime_config["promotion_enabled"] = False

        project, version = await _make_project_and_version(db_session)
        await _seed_qualification_children(
            db_session,
            project,
            version,
            audit_logs={
                "issues": [{"severity": "low", "description": "minor style nit"}],
                "test_erosion": {
                    "deleted_test_files": [],
                    "assertion_delta": 0,
                    "test_count_delta": 0,
                },
            },
            smoke_logs={
                "passed": True,
                "tests": [{"name": "smoke-ok", "status": "pass"}],
                "suites": {"startup": {"passed": True}},
            },
        )

        result = await rq._finalize_qualification(db_session, version)
        await db_session.commit()

        assert result["status"] != "infra_failure", (
            f"issues-key audit incorrectly routed as infra_failure: {result}"
        )

        await db_session.refresh(version)
        qual = version.last_qualification
        # Low-severity finding deducts 0.02 → audit score 0.98 < 1.0.
        assert qual["sub_scores"]["audit"] < 1.0, (
            f"expected deduction from low-severity finding, "
            f"got {qual['sub_scores']['audit']}"
        )

        count = (
            await db_session.execute(
                select(sa_func.count()).where(
                    QualificationVerdict.release_version_id == version.id
                )
            )
        ).scalar()
        assert count == 1, f"expected 1 QualificationVerdict row, got {count}"

    @pytest.mark.asyncio
    async def test_findings_wrong_type_routes_to_infra_failure(self, db_session):
        """audit_report = {"findings": "not-a-list"} routes to infra_failure.

        A "findings" key whose value is not a list causes _score_audit() to fall
        through to the "issues" fallback (also absent here), yielding a silent
        clean 1.0 without the wrong-type guard.  Verify infra_failure is returned
        and no QualificationVerdict row is written."""
        project, version = await _make_project_and_version(db_session)
        await _seed_qualification_children(
            db_session,
            project,
            version,
            audit_logs={
                "findings": "not-a-list",
                "test_erosion": {"deleted_test_files": [], "assertion_delta": 0},
            },
            smoke_logs={
                "passed": True,
                "tests": [{"name": "smoke-ok", "status": "pass"}],
                "suites": {"startup": {"passed": True}},
            },
        )

        result = await rq._finalize_qualification(db_session, version)
        await db_session.commit()

        assert result["status"] == "infra_failure"
        assert result["error"] == "audit_report_findings_wrong_type"

        await db_session.refresh(version)
        assert version.last_qualification["status"] == "infra_failure"
        assert version.last_qualification["error"] == "audit_report_findings_wrong_type"

        count = (
            await db_session.execute(
                select(sa_func.count()).where(
                    QualificationVerdict.release_version_id == version.id
                )
            )
        ).scalar()
        assert count == 0


# ===========================================================================
# _finalize_qualification — reaper-induced heartbeat timeout
# ===========================================================================


class TestReaperInducedAuditHeartbeatTimeout:
    """Regression guard: reaper-reaped release_audit routes to infra_failure.

    Exercises the full reaper → finalize pipeline to catch regressions where
    a reaped audit task silently creates a QualificationVerdict with
    sub_scores.audit=0 (the 0.00 collapse) or crashes during finalization.
    """

    @pytest.mark.asyncio
    async def test_reaper_heartbeat_timeout_routes_to_infra_failure(self, db_session):
        """Heartbeat-stale audit reaped as FAILED → infra_failure, no verdict.

        Five assertions (matching the task contract):
          (a) status is infra_failure, not not_promoted / dry_run.
          (b) No QualificationVerdict row is created.
          (c) Version remains open with phase=stabilize.
          (d) Error label carries the heartbeat_timeout enrichment so the
              operator can distinguish a VM heartbeat death from a code-defect
              failure.
          (e) consecutive_infra_errors is incremented (0 prior → 1).
        """
        from datetime import timedelta

        from app.failure_classification import persist_failure_category
        from app.reaper import _heartbeat_stale_cutoff
        from app.time_utils import _utcnow

        project, version = await _make_project_and_version(
            db_session, phase="stabilize"
        )

        now = _utcnow()
        # 400 s exceeds the default small-scope heartbeat-stale cutoff (300 s).
        stale_heartbeat = now - timedelta(seconds=400)

        # Seed an IN_PROGRESS release_audit task with a stale last_heartbeat,
        # simulating an agent VM that stopped heartbeating before the reaper swept.
        audit_task = Task(
            project_id=project.id,
            title="audit",
            payloads=[
                {"type": "release_audit", "base_branch": "main", "scope": "small"}
            ],
            vm_instance_id="vm-audit-heartbeat-timeout",
            status=TaskStatus.IN_PROGRESS,
            last_heartbeat=stale_heartbeat,
            context={
                "release_version_id": str(version.id),
                # Simulates an agent that reported its failure category before dying
                # (e.g. via the agent_status endpoint before the VM was killed).
                "agent_failure_category_raw": "heartbeat_timeout",
            },
        )
        db_session.add(audit_task)
        await db_session.commit()
        await db_session.refresh(audit_task)

        # Confirm the seeded heartbeat is stale per the reaper's own cutoff logic.
        cutoff = _heartbeat_stale_cutoff(audit_task, now)
        assert stale_heartbeat < cutoff, (
            f"test precondition: heartbeat {stale_heartbeat} must be before "
            f"stale cutoff {cutoff} — adjust timedelta if thresholds changed"
        )

        version.last_qualification = {
            "status": "pending",
            "candidate_sha": "dead" * 10,
            "ci_green": True,
            "smoketest_enabled": False,
            "started_at": "2026-05-19T00:00:00",
            "audit_task_id": str(audit_task.id),
            "smoketest_task_id": None,
            "prior_infra_streak": 0,
        }
        db_session.add(version)
        await db_session.commit()
        await db_session.refresh(version)

        # Simulate the reaper sweep: mark the task FAILED and record the
        # failure category (AGENT_TIMEOUT is what the reaper classifies
        # heartbeat_timeout_post_claim failures as).
        audit_task.status = TaskStatus.FAILED
        persist_failure_category(audit_task, "AGENT_TIMEOUT")
        db_session.add(audit_task)
        await db_session.commit()
        await db_session.refresh(audit_task)
        await db_session.refresh(version)

        result = await rq._finalize_qualification(db_session, version)
        await db_session.commit()

        # (a) status is infra_failure, not not_promoted / dry_run.
        assert result["status"] == "infra_failure", (
            f"expected infra_failure, got {result['status']}: {result}"
        )

        # (b) No QualificationVerdict row — the qualifier never rendered a verdict.
        count = (
            await db_session.execute(
                select(sa_func.count()).where(
                    QualificationVerdict.release_version_id == version.id
                )
            )
        ).scalar()
        assert count == 0, f"expected 0 QualificationVerdict rows, got {count}"

        # (c) Version remains open with phase=stabilize.
        await db_session.refresh(version)
        assert version.status == "open"
        assert version.phase == "stabilize"

        # (d) Error label carries the heartbeat_timeout enrichment from
        #     agent_failure_category_raw — distinguishes VM heartbeat death
        #     from code-defect failures in operator notifications.
        assert "audit_task_failed" in result["error"], (
            f"expected 'audit_task_failed' prefix, got {result['error']!r}"
        )
        assert "heartbeat_timeout" in result["error"], (
            f"expected 'heartbeat_timeout' enrichment in error label, "
            f"got {result['error']!r}"
        )

        # (e) consecutive_infra_errors is incremented (0 prior → 1).
        assert result["consecutive_infra_errors"] == 1


# ===========================================================================
# _finalize_qualification — happy-path audit_score regression
# ===========================================================================


@pytest.mark.asyncio
async def test_finalize_happy_path_audit_score_high(db_session, mocker):
    """Happy path: a COMPLETED audit with clean findings yields audit sub-score ≥ 0.7.

    Regression guard for the 0.00-collapse finding: ensures that a real
    COMPLETED audit task whose RELEASE_AUDIT_REPORT artifact carries a small
    number of low-severity findings produces a high composite with no blocker.
    """
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = False

    project, version = await _make_project_and_version(db_session)
    await _seed_qualification_children(
        db_session,
        project,
        version,
        audit_logs={
            "audit_score": 0.9,
            "findings": [
                {
                    "severity": "low",
                    "summary": "Minor style issue.",
                    "file": "app/foo.py",
                },
            ],
            "test_erosion": {
                "deleted_test_files": [],
                "assertion_delta": 3,
                "test_count_delta": 1,
            },
        },
        smoke_logs={
            "passed": True,
            "tests": [{"name": "smoke-a", "status": "pass"}],
            "suites": {"startup": {"passed": True}},
        },
    )

    mocker.patch("app.release_qualifier.promote", new=AsyncMock())

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    assert result["status"] == "dry_run", f"expected dry_run, got {result['status']}"
    assert result["would_promote"] is True

    # sub_scores are stored in the persisted last_qualification record.
    await db_session.refresh(version)
    qual = version.last_qualification
    assert qual["sub_scores"]["audit"] >= 0.7, (
        f"audit sub-score collapsed to {qual['sub_scores']['audit']}, expected ≥ 0.7"
    )
    assert qual.get("blocker") is None, f"unexpected blocker: {qual.get('blocker')}"


@pytest.mark.asyncio
async def test_finalize_quality_blocker_four_critical_findings(db_session, mocker):
    """Quality blocker: 4 critical findings yield a quality verdict (not infra_failure).

    Ensures that a COMPLETED audit task with a 0.00 audit_score from real
    findings is recorded as a quality verdict and the strategic agent can target
    the findings list — not mis-routed as infra_failure.
    """
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = False

    project, version = await _make_project_and_version(db_session)
    await _seed_qualification_children(
        db_session,
        project,
        version,
        audit_logs={
            "audit_score": 0.0,
            "findings": [
                {
                    "severity": "critical",
                    "summary": f"SQL injection {i}.",
                    "file": "app/db.py",
                    "verification_note": f"Read app/db.py lines {i * 10 + 5}-{i * 10 + 15}: user input from request body reaches cursor.execute f-string at line {i * 10 + 12} without parameterisation.",
                }
                for i in range(4)
            ],
            "test_erosion": {
                "deleted_test_files": [],
                "assertion_delta": 0,
                "test_count_delta": 0,
            },
        },
        smoke_logs={
            "passed": True,
            "tests": [{"name": "smoke-b", "status": "pass"}],
            "suites": {"startup": {"passed": True}},
        },
    )

    mocker.patch("app.release_qualifier.promote", new=AsyncMock())

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    # Must be a quality verdict (not_promoted), NOT an infra_failure.
    assert result["status"] == "not_promoted", (
        f"expected not_promoted quality verdict, got {result['status']}"
    )
    assert result["status"] != "infra_failure"

    # A QualificationVerdict row must be written (quality path, not infra path).
    count = (
        await db_session.execute(
            select(sa_func.count()).where(
                QualificationVerdict.release_version_id == version.id
            )
        )
    ).scalar()
    assert count == 1, f"expected 1 QualificationVerdict row, got {count}"

    # sub_scores and findings are persisted in last_qualification.
    await db_session.refresh(version)
    qual = version.last_qualification
    assert qual["sub_scores"]["audit"] == pytest.approx(0.0), (
        f"expected audit sub-score 0.0 (4 critical findings), got {qual['sub_scores']['audit']}"
    )
    # Findings must be captured so the strategic agent can target them.
    findings = qual.get("audit_findings", [])
    assert len(findings) > 0, (
        "expected audit_findings in last_qualification for strategic agent"
    )


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

    result = await rq.maybe_finalize_qualification(
        task, session_factory=_shared_session_factory(db_session)
    )

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

    result = await rq.maybe_finalize_qualification(
        audit_task, session_factory=_shared_session_factory(db_session)
    )

    assert result is None

    # Version must still be pending — finalize must NOT have run.
    await db_session.refresh(version)
    assert version.last_qualification["status"] == "pending"


@pytest.mark.asyncio
async def test_maybe_finalize_both_children_terminal_calls_finalize(db_session, mocker):
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
    result = await rq.maybe_finalize_qualification(
        smoke_task, session_factory=_shared_session_factory(db_session)
    )
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

    result = await rq.maybe_finalize_qualification(
        audit_task, session_factory=_shared_session_factory(db_session)
    )

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
    _patch_promote_github_defaults(mocker)
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
    assert call_kwargs.args[3] == "v1"  # tag name
    assert call_kwargs.args[4] == merge_sha  # sha — the real merge commit


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
    _patch_promote_github_defaults(mocker)
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

    count = await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )
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

    count = await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )
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

    count = await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )
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

    first = await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )
    await db_session.commit()
    second = await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )
    await db_session.commit()

    assert first == 1
    # Second sweep finds nothing left to finalize.
    assert second == 0


@pytest.mark.asyncio
async def test_sweep_no_pending_versions_returns_zero(db_session):
    """With no pending qualifications anywhere, the sweep returns 0."""
    # An open version with no qualification at all.
    await _make_project_and_version(db_session)

    count = await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )
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

    result = await rq.maybe_finalize_qualification(
        audit_task, session_factory=_shared_session_factory(db_session)
    )
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

    count = await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )

    assert count == 1
    await db_session.refresh(version)
    assert version.last_qualification["status"] == "dry_run"


# ===========================================================================
# Audit H2 — qualification-timeout force-finalize
# ===========================================================================


@pytest.mark.asyncio
async def test_sweep_force_finalizes_timed_out_qualification(db_session, mocker):
    """Audit H2: a qualification pending longer than the qualification-timeout
    is force-finalized as a blocked one (blocker=qualification_timeout) EVEN
    when a child is still non-terminal — the escape hatch for a child that
    heartbeats-but-hangs."""
    # Isolate this sweep/timeout test from the post-commit strategic re-trigger
    # (covered by its own tests); a resolved verdict would otherwise spawn a
    # real strategic_review whose branch-resolution emits extra alerts.
    mocker.patch(
        "app.release_qualifier._maybe_trigger_strategic_after_qualification",
        new=AsyncMock(return_value=None),
    )
    from datetime import timedelta

    from app.time_utils import _utcnow
    from sqlmodel import select as _select

    project, version = await _make_project_and_version(db_session)

    # The smoketest child is still IN_PROGRESS — normally the sweep would skip
    # this version. started_at is well past the timeout.
    old_started = (
        _utcnow() - timedelta(seconds=rq._MAX_QUALIFICATION_DURATION_SECONDS + 3600)
    ).isoformat()
    await _seed_pending_qualification(
        db_session,
        project,
        version,
        audit_status=TaskStatus.COMPLETED,
        smoke_status=TaskStatus.IN_PROGRESS,  # hung child, never terminal
        started_at=old_started,
    )

    count = await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )

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
        (await db_session.execute(_select(Alert).where(Alert.project_id == project.id)))
        .scalars()
        .all()
    )
    assert len(alerts) == 1
    assert "timed out" in alerts[0].title.lower()


# ===========================================================================
# _maybe_reconcile_after_promotion_conflict — promotion-conflict self-heal
# ===========================================================================


@pytest.mark.asyncio
async def test_promotion_conflict_triggers_reconciliation(db_session, mocker):
    """A promotion that failed on a merge CONFLICT kicks off a main -> automain
    reconciliation so the version self-heals instead of dead-ending in
    stabilize until an operator notices."""
    project, version = await _make_project_and_version(db_session)
    recon_mock = mocker.patch(
        "app.hotfix_backmerge.reconcile_main_into_automain",
        new=AsyncMock(return_value={"status": "merged"}),
    )

    await rq._maybe_reconcile_after_promotion_conflict(
        version.id,
        {
            "status": "promotion_failed",
            "error": "merge_blocked",
            "conflict": True,
        },
        session_factory=_shared_session_factory(db_session),
    )

    recon_mock.assert_awaited_once()
    # Reconciliation runs for the conflicted version's own project.
    assert recon_mock.call_args.args[1] == project.id


@pytest.mark.asyncio
async def test_promotion_failure_without_conflict_does_not_reconcile(
    db_session, mocker
):
    """A non-conflict promotion failure (e.g. a 405 checks-block) must NOT
    trigger a reconciliation — there is no branch divergence to reconcile."""
    _project, version = await _make_project_and_version(db_session)
    recon_mock = mocker.patch(
        "app.hotfix_backmerge.reconcile_main_into_automain",
        new=AsyncMock(return_value={"status": "merged"}),
    )

    await rq._maybe_reconcile_after_promotion_conflict(
        version.id,
        {
            "status": "promotion_failed",
            "error": "merge_blocked",
            "conflict": False,
        },
        session_factory=_shared_session_factory(db_session),
    )

    recon_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_promotion_failure_result_does_not_reconcile(db_session, mocker):
    """A dry_run / not_promoted / None finalize result never triggers a
    reconciliation — only a conflicted promotion failure does."""
    _project, version = await _make_project_and_version(db_session)
    recon_mock = mocker.patch(
        "app.hotfix_backmerge.reconcile_main_into_automain",
        new=AsyncMock(return_value={"status": "merged"}),
    )

    for result in (
        {"status": "dry_run", "confidence": 0.9},
        {"status": "not_promoted", "blocker": "below_threshold"},
        None,
    ):
        await rq._maybe_reconcile_after_promotion_conflict(
            version.id,
            result,
            session_factory=_shared_session_factory(db_session),
        )

    recon_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_finalize_in_dedicated_session_reconciles_on_promotion_conflict(
    db_session, mocker
):
    """End-to-end wiring: when the finalize chain produces a conflicted
    promotion failure, _finalize_in_dedicated_session triggers the
    main -> automain reconciliation after its dedicated session has committed
    and closed."""
    _project, version = await _make_project_and_version(db_session)
    # Drive _finalize_locked_version to return a conflicted promotion failure.
    mocker.patch(
        "app.release_qualifier._finalize_locked_version",
        new=AsyncMock(
            return_value={
                "status": "promotion_failed",
                "error": "merge_blocked",
                "conflict": True,
            }
        ),
    )
    recon_mock = mocker.patch(
        "app.hotfix_backmerge.reconcile_main_into_automain",
        new=AsyncMock(return_value={"status": "conflict"}),
    )

    result = await rq._finalize_in_dedicated_session(
        version.id, session_factory=_shared_session_factory(db_session)
    )

    assert result is not None
    assert result["status"] == "promotion_failed"
    recon_mock.assert_awaited_once()


# ===========================================================================
# _maybe_trigger_strategic_after_qualification — re-plan on a resolved verdict
# ===========================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["not_promoted", "dry_run", "promoted", "error"])
async def test_resolved_qualification_triggers_strategic_review(
    db_session, mocker, status
):
    """Every RESOLVED qualification verdict — a promote or any flavour of miss —
    re-triggers the strategic agent so it re-plans (opens the next wave on a
    promote, works the blocker on a miss) instead of leaving a dry frontier
    idle until the wall-clock backstop fires."""
    project, version = await _make_project_and_version(db_session)
    spawn_mock = mocker.patch(
        "app.strategic_spawning._spawn_strategic_review",
        new=AsyncMock(return_value=True),
    )

    await rq._maybe_trigger_strategic_after_qualification(
        version.id,
        {"status": status, "confidence": 0.8},
        session_factory=_shared_session_factory(db_session),
    )

    spawn_mock.assert_awaited_once()
    assert spawn_mock.call_args.args[0].id == project.id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result",
    [
        {"status": "pending_auto_merge"},  # promotion still in flight
        {"status": "audit_rate_limit_respawned", "respawn_count": 1},  # transient
        {"status": "wrong_phase"},  # no-op
        None,  # read-only no-op finalize
    ],
)
async def test_unresolved_qualification_does_not_trigger_strategic(
    db_session, mocker, result
):
    """In-flight / transient / no-op finalize results must NOT re-trigger the
    strategic agent — only a settled verdict warrants a re-plan."""
    _project, version = await _make_project_and_version(db_session)
    spawn_mock = mocker.patch(
        "app.strategic_spawning._spawn_strategic_review",
        new=AsyncMock(return_value=True),
    )

    await rq._maybe_trigger_strategic_after_qualification(
        version.id,
        result,
        session_factory=_shared_session_factory(db_session),
    )

    spawn_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_strategic_trigger_skipped_when_autonomy_disabled(db_session, mocker):
    """A project with autonomy disabled is never auto-driven into a strategic
    cycle by a qualification verdict."""
    project, version = await _make_project_and_version(db_session)
    project.autonomous_enabled = False
    db_session.add(project)
    await db_session.commit()
    spawn_mock = mocker.patch(
        "app.strategic_spawning._spawn_strategic_review",
        new=AsyncMock(return_value=True),
    )

    await rq._maybe_trigger_strategic_after_qualification(
        version.id,
        {"status": "not_promoted"},
        session_factory=_shared_session_factory(db_session),
    )

    spawn_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_strategic_trigger_swallows_spawn_failure(db_session, mocker):
    """The trigger is best-effort: a spawn failure (e.g. payload too large) is
    logged but never propagates to break the finalize chain."""
    _project, version = await _make_project_and_version(db_session)
    mocker.patch(
        "app.strategic_spawning._spawn_strategic_review",
        new=AsyncMock(side_effect=RuntimeError("boom")),
    )

    # Must not raise.
    await rq._maybe_trigger_strategic_after_qualification(
        version.id,
        {"status": "not_promoted"},
        session_factory=_shared_session_factory(db_session),
    )


@pytest.mark.asyncio
async def test_strategic_trigger_queues_refresh_when_cycle_active(db_session, mocker):
    """When a strategic cycle is already active the spawn no-ops (returns False) —
    the verdict trigger must NOT be dropped. It coalesces into a durable refresh
    marker the autonomous loop consumes (regression for the 2026-06-02 dropped-
    trigger starvation: v3 promoted while a cycle ran and directions stayed 0)."""
    project, version = await _make_project_and_version(db_session)
    mocker.patch(
        "app.strategic_spawning._spawn_strategic_review",
        new=AsyncMock(return_value=False),  # a cycle is already active
    )

    await rq._maybe_trigger_strategic_after_qualification(
        version.id,
        {"status": "promoted"},
        session_factory=_shared_session_factory(db_session),
    )

    await db_session.refresh(project)
    from app.strategic_trigger import strategic_refresh_pending

    assert strategic_refresh_pending(project.vision) is True
    assert (
        "post_qualification:promoted"
        in (project.vision or {})["_strategic_refresh"]["reason"]
    )


@pytest.mark.asyncio
async def test_finalize_in_dedicated_session_triggers_strategic_on_miss(
    db_session, mocker
):
    """End-to-end wiring: a finalize that resolves to a miss drives
    _finalize_in_dedicated_session to re-trigger a strategic review after its
    dedicated session has committed and closed."""
    _project, version = await _make_project_and_version(db_session)
    mocker.patch(
        "app.release_qualifier._finalize_locked_version",
        new=AsyncMock(
            return_value={"status": "not_promoted", "blocker": "below_threshold"}
        ),
    )
    spawn_mock = mocker.patch(
        "app.strategic_spawning._spawn_strategic_review",
        new=AsyncMock(return_value=True),
    )

    result = await rq._finalize_in_dedicated_session(
        version.id, session_factory=_shared_session_factory(db_session)
    )

    assert result is not None and result["status"] == "not_promoted"
    spawn_mock.assert_awaited_once()


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

    count = await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )

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
        _utcnow() - timedelta(seconds=rq._MAX_QUALIFICATION_DURATION_SECONDS + 600)
    ).isoformat()
    await _seed_pending_qualification(
        db_session,
        project,
        version,
        audit_status=TaskStatus.COMPLETED,
        smoke_status=TaskStatus.COMPLETED,
        started_at=old_started,
    )

    count = await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )

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
    count = await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )

    # Exactly one version finalized (A); B's failure was caught and isolated.
    assert count == 1

    # CRITICAL (audit H5): version A's finalization is committed and durable —
    # B's raise did NOT roll it back.
    await db_session.refresh(version_a)
    assert version_a.last_qualification["status"] == "dry_run"

    # Version B stays pending — its finalize failed and rolled back cleanly.
    await db_session.refresh(version_b)
    assert version_b.last_qualification["status"] == "pending"


@pytest.mark.asyncio
async def test_sweep_finalize_crash_increments_streak(db_session, mocker):
    """A single finalize crash increments sweep_crash_streak by 1 but does NOT
    force-finalize — self-recovery fires only after _MAX_SWEEP_CRASH_STREAK
    consecutive crashes.  The version stays pending and will be retried."""
    project, version = await _make_project_and_version(db_session)
    await _seed_pending_qualification(
        db_session,
        project,
        version,
        audit_status=TaskStatus.COMPLETED,
        smoke_status=TaskStatus.COMPLETED,
    )

    mocker.patch.object(
        rq,
        "_finalize_qualification",
        new=AsyncMock(side_effect=RuntimeError("simulated finalize crash")),
    )

    count = await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )

    assert count == 0  # crash was caught; nothing finalized on this sweep

    await db_session.refresh(version)
    qual = version.last_qualification
    assert qual is not None
    assert qual.get("status") == "pending"  # NOT force-finalized yet
    assert qual.get("sweep_crash_streak") == 1  # streak incremented once


@pytest.mark.asyncio
async def test_sweep_finalize_crash_loop_force_finalizes_and_alerts(db_session, mocker):
    """After _MAX_SWEEP_CRASH_STREAK consecutive finalize crashes the sweep
    force-finalizes the version as not_promoted/sweep_finalize_crash_loop, resets
    it to the stabilize phase, posts a WARNING Alert, and sends an operator
    notification — the crash-loop is never invisible to operators."""
    project, version = await _make_project_and_version(db_session)
    await _seed_pending_qualification(
        db_session,
        project,
        version,
        audit_status=TaskStatus.COMPLETED,
        smoke_status=TaskStatus.COMPLETED,
    )

    # Simulate (MAX - 1) prior crashes already recorded.
    await db_session.refresh(version)
    from sqlalchemy.orm.attributes import flag_modified as _flag_modified

    version.last_qualification = {
        **version.last_qualification,
        "sweep_crash_streak": rq._MAX_SWEEP_CRASH_STREAK - 1,
    }
    _flag_modified(version, "last_qualification")
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    mocker.patch.object(
        rq,
        "_finalize_qualification",
        new=AsyncMock(side_effect=RuntimeError("persistent finalize crash")),
    )

    count = await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )

    # force-finalize path does not count as a normal finalization
    assert count == 0

    await db_session.refresh(version)
    qual = version.last_qualification
    assert qual is not None
    assert qual["status"] == "not_promoted"
    assert qual["blocker"] == "sweep_finalize_crash_loop"
    assert qual.get("sweep_crash_streak") == 0  # reset after force-finalize
    assert version.phase == "stabilize"  # returned to stabilize for re-qualification

    # A WARNING Alert must be raised so the crash-loop is visible to operators.
    alerts = (
        (await db_session.execute(select(Alert).where(Alert.project_id == project.id)))
        .scalars()
        .all()
    )
    assert len(alerts) == 1
    assert "crash" in alerts[0].title.lower()

    # An operator ProjectMessage must be posted.
    messages = (
        (
            await db_session.execute(
                select(ProjectMessage).where(ProjectMessage.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    assert any("crash" in (m.content or "").lower() for m in messages)


# ===========================================================================
# Round 5 — R4-C1 / R4-H1: promote() idempotency on a crash-retry
# ===========================================================================


@pytest.mark.asyncio
async def test_promote_idempotent_when_candidate_already_on_main(db_session, mocker):
    """R4-C1: a retried promote() whose candidate_sha is ALREADY reachable from
    main (a prior attempt landed the merge but crashed before the DB commit)
    records SUCCESS without re-merging — it must NOT open a second PR, re-merge,
    or record a promotion_failed.  This is the crash-between-merge-and-commit
    divergence fix."""
    _project, version = await _make_project_and_version(db_session)
    candidate = "alr3ady0" * 5  # already merged to main by a prior attempt

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    # The idempotency pre-check sees the candidate is already on main.
    ancestor_mock = mocker.patch(
        "app.github.is_ancestor_of", new=AsyncMock(return_value=True)
    )
    # These must NOT be called — the promotion already landed.
    branch_mock = mocker.patch("app.github.create_branch", new=_create_branch_mock())
    create_mock = mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"number": 1, "html_url": "http://pr/1"}),
    )
    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(return_value={"merged": True}),
    )
    delete_mock = mocker.patch(
        "app.github.delete_branch", new=AsyncMock(return_value=True)
    )
    mocker.patch(
        "app.github.create_tag",
        new=AsyncMock(return_value={"ref": "refs/tags/v1"}),
    )

    result = await rq.promote(
        db_session,
        version,
        candidate,
        composite={"confidence": 0.9},
        base_record={"candidate_sha": candidate},
    )
    await db_session.commit()

    # Recorded as a SUCCESS — not a failure.
    assert result["status"] == "promoted"

    # The idempotency check ran; no PR was opened, nothing was re-merged.
    ancestor_mock.assert_awaited()
    branch_mock.assert_not_called()
    create_mock.assert_not_called()
    merge_mock.assert_not_called()
    # The release branch a prior attempt may have left behind is cleaned up.
    delete_mock.assert_awaited()

    await db_session.refresh(version)
    assert version.status == "promoted"
    assert version.last_qualification["status"] == "promoted"
    # The record is flagged as an already-landed idempotent retry.
    assert version.last_qualification.get("already_landed") is True
    # The next version was still opened — the DB now matches GitHub.
    next_version = await rv.current_open_version(db_session, version.project_id)
    assert next_version is not None
    assert next_version.version_number == 2


@pytest.mark.asyncio
async def test_promote_open_pr_already_merged_recorded_as_success(db_session, mocker):
    """R4-H1: on a retry the release PR is still open, merge_pull_request runs
    again, GitHub returns 405 (already merged) → {"merged": False}.  Without the
    post-merge disambiguation this would be misclassified as merge_blocked.
    Round 7 concern 3: promote() disambiguates via the PR's OWN merged boolean
    (get_pull_request_merge_state) — the PR reports merged=True, so promote()
    records SUCCESS instead."""
    _project, version = await _make_project_and_version(db_session)
    candidate = "0penpr00" * 5
    merge_sha = "realmerg" * 5

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )

    # is_ancestor_of: False — only the idempotency PRE-check consults it.  The
    # merged=False disambiguation no longer needs the (lagging) ancestry API:
    # the authoritative PR-state check below settles it.
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=False))
    # Round 7 concern 3: the PR's own resource reports merged=True.
    pr_state_mock = mocker.patch(
        "app.github.get_pull_request_merge_state",
        new=AsyncMock(return_value=True),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=_branch_sha_mock(main=merge_sha),
    )
    mocker.patch("app.github.create_branch", new=_create_branch_mock())
    mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"number": 7, "html_url": "http://pr/7"}),
    )
    # merge returns 405-style {"merged": False} — the PR was already merged.
    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(
            return_value={"merged": False, "message": "Pull Request is not mergeable"}
        ),
    )
    delete_mock = mocker.patch(
        "app.github.delete_branch", new=AsyncMock(return_value=True)
    )
    mocker.patch(
        "app.github.create_tag",
        new=AsyncMock(return_value={"ref": "refs/tags/v1"}),
    )

    result = await rq.promote(
        db_session,
        version,
        candidate,
        composite={"confidence": 0.9},
        base_record={"candidate_sha": candidate},
    )
    await db_session.commit()

    # A merged=False result + PR-state merged=True = SUCCESS, not merge_blocked.
    assert result["status"] == "promoted"
    assert result["status"] != "promotion_failed"
    merge_mock.assert_awaited()  # the merge WAS attempted
    pr_state_mock.assert_awaited()  # the PR's own merge state WAS consulted
    delete_mock.assert_awaited()  # branch cleaned up

    await db_session.refresh(version)
    assert version.status == "promoted"
    assert version.last_qualification["status"] == "promoted"
    # The post-merge re-resolve of main HEAD becomes promoted_sha.
    assert version.promoted_sha == merge_sha
    assert version.last_qualification.get("already_landed") is True


@pytest.mark.asyncio
async def test_promote_pr_creation_fails_on_retry_but_candidate_merged(
    db_session, mocker
):
    """R4-H1: on a retry the prior PR merged then closed, so create_pull_request
    finds nothing open and raises.  Because the candidate is already on main,
    promote() re-checks idempotency and records SUCCESS rather than
    pr_creation_failed."""
    _project, version = await _make_project_and_version(db_session)
    candidate = "prret000" * 5
    merge_sha = "mergedrt" * 5

    from fastapi import HTTPException

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    # Pre-check False (proceed), post-PR-failure re-check True (already merged).
    ancestry_calls = {"n": 0}

    async def _ancestor(token, owner, repo, anc, desc, project_id=None):
        ancestry_calls["n"] += 1
        return ancestry_calls["n"] >= 2

    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(side_effect=_ancestor))
    mocker.patch(
        "app.github.get_branch_sha",
        new=_branch_sha_mock(main=merge_sha),
    )
    mocker.patch("app.github.create_branch", new=_create_branch_mock())
    # PR creation raises — the prior attempt's PR is already merged + closed.
    create_mock = mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(
            side_effect=HTTPException(
                status_code=422, detail="Failed to create pull request: head ref"
            )
        ),
    )
    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(return_value={"merged": True}),
    )
    delete_mock = mocker.patch(
        "app.github.delete_branch", new=AsyncMock(return_value=True)
    )
    mocker.patch(
        "app.github.create_tag",
        new=AsyncMock(return_value={"ref": "refs/tags/v1"}),
    )

    result = await rq.promote(
        db_session,
        version,
        candidate,
        composite={"confidence": 0.9},
        base_record={"candidate_sha": candidate},
    )
    await db_session.commit()

    # PR creation failed but the candidate is on main → SUCCESS.
    assert result["status"] == "promoted"
    create_mock.assert_awaited()
    # No merge was attempted — PR creation failed before the merge step.
    merge_mock.assert_not_called()
    delete_mock.assert_awaited()

    await db_session.refresh(version)
    assert version.status == "promoted"
    assert version.last_qualification.get("already_landed") is True


@pytest.mark.asyncio
async def test_promote_indeterminate_ancestry_does_not_false_succeed(
    db_session, mocker
):
    """R4-C1 safety: when the idempotency ancestry check is INDETERMINATE
    (is_ancestor_of returns None — a transient GitHub error), promote() must
    NOT treat that as 'already merged'.  It proceeds with a real promotion —
    a transient failure must never silently skip a merge."""
    _project, version = await _make_project_and_version(db_session)
    candidate = "1nd3t000" * 5
    merge_sha = "freshmrg" * 5

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    # is_ancestor_of returns None — the comparison could not be made.
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=None))
    mocker.patch(
        "app.github.get_branch_sha",
        new=_branch_sha_mock(main=merge_sha),
    )
    branch_mock = mocker.patch("app.github.create_branch", new=_create_branch_mock())
    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(return_value={"merged": True, "message": "Merged"}),
    )
    mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"number": 2, "html_url": "http://pr/2"}),
    )
    mocker.patch("app.github.delete_branch", new=AsyncMock(return_value=True))
    mocker.patch(
        "app.github.create_tag",
        new=AsyncMock(return_value={"ref": "refs/tags/v1"}),
    )

    result = await rq.promote(
        db_session, version, candidate, composite={"confidence": 0.9}
    )
    await db_session.commit()

    assert result["status"] == "promoted"
    # A real promotion ran — the indeterminate check did NOT short-circuit it.
    branch_mock.assert_awaited()
    merge_mock.assert_awaited()


@pytest.mark.asyncio
async def test_promote_genuine_merge_block_still_fails(db_session, mocker):
    """R4-H1 boundary: a genuine non-merge (a real checks/reviews block — the PR
    genuinely did not merge) is still recorded as merge_blocked.  The Round-7
    concern-3 disambiguation only converts a non-merge to success when the PR
    actually merged; it must not paper over real failures."""
    _project, version = await _make_project_and_version(db_session)
    candidate = "blocked0" * 5

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    # is_ancestor_of False — only the idempotency pre-check consults it.
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=False))
    # Round 7 concern 3: the PR's own resource reports merged=False — a genuine,
    # authoritative not-merged.  The merged=False result is a real failure.
    mocker.patch(
        "app.github.get_pull_request_merge_state",
        new=AsyncMock(return_value=False),
    )
    mocker.patch("app.github.get_branch_sha", new=_branch_sha_mock())
    mocker.patch("app.github.create_branch", new=_create_branch_mock())
    mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"number": 4, "html_url": "http://pr/4"}),
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
    mocker.patch("app.github.delete_branch", new=AsyncMock(return_value=True))

    result = await rq.promote(
        db_session, version, candidate, composite={"confidence": 0.9}
    )
    await db_session.commit()

    # A real merge block — recorded as a failure, not papered over.
    assert result["status"] == "promotion_failed"
    assert result["error"] == "merge_blocked"

    await db_session.refresh(version)
    assert version.status == "open"
    assert version.phase == "stabilize"


# ===========================================================================
# Round 5 — R4-H2: release-branch deletion after a successful promotion
# ===========================================================================


@pytest.mark.asyncio
async def test_promote_deletes_release_branch_after_merge(db_session, mocker):
    """R4-H2: after a confirmed merge, promote() best-effort deletes the
    release-v{N} branch so dangling release branches do not accumulate on the
    repo forever."""
    _project, version = await _make_project_and_version(db_session)
    merge_sha = "delbr000" * 5

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=False))
    mocker.patch(
        "app.github.get_branch_sha",
        new=_branch_sha_mock(main=merge_sha),
    )
    mocker.patch("app.github.create_branch", new=_create_branch_mock())
    mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"number": 6, "html_url": "http://pr/6"}),
    )
    mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(return_value={"merged": True, "message": "Merged"}),
    )
    delete_mock = mocker.patch(
        "app.github.delete_branch", new=AsyncMock(return_value=True)
    )
    mocker.patch(
        "app.github.create_tag",
        new=AsyncMock(return_value={"ref": "refs/tags/v1"}),
    )

    result = await rq.promote(
        db_session, version, "ca0d" * 10, composite={"confidence": 0.9}
    )
    await db_session.commit()

    assert result["status"] == "promoted"
    # The release-v1 branch was deleted exactly once after the merge.
    delete_mock.assert_called_once()
    assert delete_mock.call_args.args[3] == "release-v1"


@pytest.mark.asyncio
async def test_promote_branch_delete_failure_does_not_abort_promotion(
    db_session, mocker
):
    """R4-H2: a delete_branch failure is non-fatal — the promotion still
    succeeds (the merge already landed; a dangling branch is cosmetic)."""
    _project, version = await _make_project_and_version(db_session)
    merge_sha = "keptbr00" * 5

    from fastapi import HTTPException

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=False))
    mocker.patch(
        "app.github.get_branch_sha",
        new=_branch_sha_mock(main=merge_sha),
    )
    mocker.patch("app.github.create_branch", new=_create_branch_mock())
    mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"number": 10, "html_url": "http://pr/10"}),
    )
    mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(return_value={"merged": True, "message": "Merged"}),
    )
    # delete_branch raises — must not abort the promotion.
    mocker.patch(
        "app.github.delete_branch",
        new=AsyncMock(side_effect=HTTPException(status_code=500, detail="boom")),
    )
    mocker.patch(
        "app.github.create_tag",
        new=AsyncMock(return_value={"ref": "refs/tags/v1"}),
    )

    result = await rq.promote(
        db_session, version, "beef" * 10, composite={"confidence": 0.95}
    )
    await db_session.commit()

    # The promotion succeeds despite the branch-delete failure.
    assert result["status"] == "promoted"
    assert result["next_version_number"] == 2
    await db_session.refresh(version)
    assert version.status == "promoted"


# ===========================================================================
# Round 5 — R4-H3 / R4-C1: lock-safe re-check guards against double-promote
# ===========================================================================


@pytest.mark.asyncio
async def test_promote_bails_if_version_already_promoted_under_relock(
    db_session, mocker
):
    """R4-H3 / R4-C1: promote() releases the ReleaseVersion row lock for its
    GitHub I/O, then re-acquires it for the final write and RE-CHECKS
    version.status == "open".  If a concurrent finalize / crash-retry already
    promoted the version while the GitHub I/O ran, the re-locked row reads
    status="promoted" and promote() bails WITHOUT opening a duplicate vN+1."""
    _project, version = await _make_project_and_version(db_session)
    candidate = "racec0de" * 5
    merge_sha = "racemerg" * 5

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=False))
    mocker.patch(
        "app.github.get_branch_sha",
        new=_branch_sha_mock(main=merge_sha),
    )
    mocker.patch("app.github.create_branch", new=_create_branch_mock())
    mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"number": 11, "html_url": "http://pr/11"}),
    )

    # Simulate a concurrent finalize winning the race: while promote()'s merge
    # I/O "runs", another path flips the version to promoted in the DB.  We
    # hook merge_pull_request to perform that out-of-band mutation via a direct
    # SQL UPDATE (no ORM identity-map interaction with promote()'s own
    # already-committed/expired instance).
    from sqlalchemy import update as _sa_update

    async def _merge_then_concurrent_promote(*args, **kwargs):
        # A parallel promotion lands first: mark the version promoted directly.
        await db_session.execute(
            _sa_update(rv.ReleaseVersion)
            .where(rv.ReleaseVersion.id == version.id)
            .values(status="promoted", promoted_sha="wonrace0" * 5)
        )
        await db_session.commit()
        return {"merged": True, "message": "Merged"}

    mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(side_effect=_merge_then_concurrent_promote),
    )
    mocker.patch("app.github.delete_branch", new=AsyncMock(return_value=True))
    mocker.patch(
        "app.github.create_tag",
        new=AsyncMock(return_value={"ref": "refs/tags/v1"}),
    )

    result = await rq.promote(
        db_session, version, candidate, composite={"confidence": 0.9}
    )
    await db_session.commit()

    # promote() detected the version is already promoted under the re-lock and
    # bailed — it reports success-as-already-promoted, NOT a fresh promotion.
    assert result["status"] == "promoted"
    assert result.get("already_promoted") is True
    # CRITICAL: no duplicate vN+1 was opened — only the concurrent winner's
    # state stands.  current_open_version must not find a v2.
    open_v = await rv.current_open_version(db_session, _project.id)
    assert open_v is None or open_v.version_number != 2

    # All ReleaseVersion rows for the project: exactly one (v1, promoted) —
    # no duplicate v2 row was created by the losing promote() call.
    from sqlmodel import select as _select

    all_versions = (
        (
            await db_session.execute(
                _select(rv.ReleaseVersion).where(
                    rv.ReleaseVersion.project_id == _project.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(all_versions) == 1
    assert all_versions[0].version_number == 1


# ===========================================================================
# Round 5 — R4 MEDIUM: qualification_attempts increment is lost-update-safe
# ===========================================================================


@pytest.mark.asyncio
async def test_qualification_attempts_increments_atomically_on_no_credentials(
    db_session,
):
    """R4 MEDIUM: the no_credentials early-return path increments
    qualification_attempts with an atomic SQL UPDATE (not a read-modify-write).
    Two sequential calls bump the counter to exactly 2 — the atomic increment
    is relative to the row's current DB value."""
    project = Project(name="RQ-AtomicAttempts", user_id="rq-user")  # no repo
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    version = await rv.open_version(db_session, project.id, version_number=1)
    rv.set_version_phase(version, "stabilize", reasoning="test setup")
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    r1 = await rq.run_qualification(db_session, project.id, version)
    await db_session.commit()
    assert r1["status"] == "no_credentials"
    await db_session.refresh(version)
    assert version.qualification_attempts == 1

    r2 = await rq.run_qualification(db_session, project.id, version)
    await db_session.commit()
    assert r2["status"] == "no_credentials"
    await db_session.refresh(version)
    # The atomic increment bumped it relative to the DB value → 2, not 1.
    assert version.qualification_attempts == 2


@pytest.mark.asyncio
async def test_qualification_attempts_increments_atomically_on_sha_error(
    db_session, mocker
):
    """R4 MEDIUM: the sha_error early-return path also increments
    qualification_attempts with an atomic SQL UPDATE."""
    from fastapi import HTTPException

    project, version = await _make_project_and_version(db_session, phase="stabilize")

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    # automain SHA snapshot fails — the sha_error path.
    mocker.patch(
        "app.github.get_branch_sha",
        new=AsyncMock(side_effect=HTTPException(status_code=404, detail="no automain")),
    )

    result = await rq.run_qualification(db_session, project.id, version)
    await db_session.commit()

    assert result["status"] == "sha_error"
    await db_session.refresh(version)
    assert version.last_qualification["status"] == "error"
    assert version.last_qualification["error"] == "sha_error"
    # The atomic increment ran.
    assert version.qualification_attempts == 1


# ===========================================================================
# Round 5 — R4 MEDIUM: status!="open" guard before promoting
# ===========================================================================


@pytest.mark.asyncio
async def test_maybe_finalize_skips_non_open_version(db_session):
    """R4 MEDIUM: maybe_finalize_qualification skips a version whose status is
    not 'open' (e.g. already promoted/abandoned) — defensive insurance against
    a future abandon path racing a late child completion."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = False

    project, version = await _make_project_and_version(db_session)
    audit_task, _smoke_task = await _seed_pending_qualification(
        db_session,
        project,
        version,
        audit_status=TaskStatus.COMPLETED,
        smoke_status=TaskStatus.COMPLETED,
    )

    # Flip the version to a terminal status while last_qualification still
    # says 'pending' (a future abandon path could do this).
    version.status = "abandoned"
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    result = await rq.maybe_finalize_qualification(
        audit_task, session_factory=_shared_session_factory(db_session)
    )

    # The status!="open" guard fired — finalize did not run.
    assert result is None
    await db_session.refresh(version)
    # last_qualification is untouched — still pending, never finalized.
    assert version.last_qualification["status"] == "pending"
    assert version.status == "abandoned"


@pytest.mark.asyncio
async def test_sweep_skips_non_open_version(db_session):
    """R4 MEDIUM: the sweep skips a non-open version even when it is past the
    qualification timeout — the timeout force-finalize must never flip a
    terminal (promoted/abandoned) version back to stabilize."""
    from datetime import timedelta

    from app.time_utils import _utcnow

    project, version = await _make_project_and_version(db_session)
    # Past the timeout, one child still running — would normally force-finalize.
    old_started = (
        _utcnow() - timedelta(seconds=rq._MAX_QUALIFICATION_DURATION_SECONDS + 600)
    ).isoformat()
    await _seed_pending_qualification(
        db_session,
        project,
        version,
        audit_status=TaskStatus.COMPLETED,
        smoke_status=TaskStatus.IN_PROGRESS,
        started_at=old_started,
    )

    # Flip to abandoned after seeding the (timed-out) pending qualification.
    version.status = "abandoned"
    db_session.add(version)
    await db_session.commit()

    count = await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )
    await db_session.commit()

    # The sweep's open-versions query never even returns an abandoned version,
    # and _sweep_one_version's status!="open" guard is the belt-and-suspenders
    # backstop — either way, nothing is finalized.
    assert count == 0
    await db_session.refresh(version)
    # The timed-out qualification was NOT force-finalized.
    assert version.last_qualification["status"] == "pending"
    assert version.status == "abandoned"


# ===========================================================================
# Round 5 — R4-H5: timeout backstop survives an unparseable started_at
# ===========================================================================


@pytest.mark.asyncio
async def test_sweep_timeout_fires_when_started_at_unparseable(db_session, mocker):
    """R4-H5: when started_at is corrupt/unparseable, the qualification-timeout
    backstop must STILL fire — it falls back to version.updated_at as the
    timeout clock.  A bad timestamp string must never wedge a qualification
    forever."""
    # Isolate this sweep/timeout test from the post-commit strategic re-trigger
    # (covered by its own tests); a resolved verdict would otherwise spawn a
    # real strategic_review whose branch-resolution emits extra alerts.
    mocker.patch(
        "app.release_qualifier._maybe_trigger_strategic_after_qualification",
        new=AsyncMock(return_value=None),
    )
    from datetime import timedelta

    from app.models import ReleaseVersion as _RV
    from app.time_utils import _utcnow
    from sqlmodel import select as _select

    project, version = await _make_project_and_version(db_session)

    # Seed a pending qualification with a hung child and a CORRUPT started_at.
    await _seed_pending_qualification(
        db_session,
        project,
        version,
        audit_status=TaskStatus.COMPLETED,
        smoke_status=TaskStatus.IN_PROGRESS,  # hung child, never terminal
        started_at="not-a-real-timestamp",  # unparseable
    )

    # Force the row's updated_at far into the past so the updated_at fallback
    # clock exceeds the qualification timeout.  An explicit UPDATE bypasses the
    # onupdate hook (which would otherwise stamp it to now).
    old_ts = _utcnow() - timedelta(
        seconds=rq._MAX_QUALIFICATION_DURATION_SECONDS + 3600
    )
    from sqlalchemy import update as _update

    await db_session.execute(
        _update(_RV).where(_RV.id == version.id).values(updated_at=old_ts)
    )
    await db_session.commit()

    count = await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )
    await db_session.commit()

    # The timeout fired via the updated_at fallback, even with a bad started_at.
    assert count == 1
    await db_session.refresh(version)
    assert version.last_qualification["status"] == "not_promoted"
    assert version.last_qualification["blocker"] == "qualification_timeout"
    assert version.phase == "stabilize"

    # An operator-visible Alert was raised.
    from app.models import Alert as _Alert

    alerts = (
        (
            await db_session.execute(
                _select(_Alert).where(_Alert.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 1


@pytest.mark.asyncio
async def test_sweep_no_timeout_when_started_at_unparseable_but_row_fresh(
    db_session,
):
    """R4-H5: the unparseable-started_at fallback does NOT make the timeout
    over-aggressive — when updated_at is recent, a corrupt started_at does not
    cause a premature force-finalize.  A still-running child keeps the version
    pending."""
    project, version = await _make_project_and_version(db_session)

    # Corrupt started_at, but the row was just written (updated_at = now).
    await _seed_pending_qualification(
        db_session,
        project,
        version,
        audit_status=TaskStatus.COMPLETED,
        smoke_status=TaskStatus.IN_PROGRESS,
        started_at="garbage",
    )

    count = await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )
    await db_session.commit()

    # updated_at is fresh → the fallback clock is well within the timeout.
    assert count == 0
    await db_session.refresh(version)
    assert version.last_qualification["status"] == "pending"


# ===========================================================================
# Round 5 — github.is_ancestor_of: the idempotency primitive
# ===========================================================================


class TestIsAncestorOf:
    """github.is_ancestor_of wraps the compare API to answer 'is ancestor_sha
    reachable from descendant_ref'."""

    @pytest.mark.asyncio
    async def test_status_ahead_means_ancestor(self, mocker):
        """compare status 'ahead' → the descendant strictly contains the
        ancestor → True."""
        from app import github

        resp = mocker.Mock()
        resp.status_code = 200
        resp.json = mocker.Mock(return_value={"status": "ahead"})
        mocker.patch("app.github._github_request", new=AsyncMock(return_value=resp))
        result = await github.is_ancestor_of("tok", "o", "r", "cafe" * 10, "main")
        assert result is True

    @pytest.mark.asyncio
    async def test_status_identical_means_ancestor(self, mocker):
        """compare status 'identical' (same commit) → True."""
        from app import github

        resp = mocker.Mock()
        resp.status_code = 200
        resp.json = mocker.Mock(return_value={"status": "identical"})
        mocker.patch("app.github._github_request", new=AsyncMock(return_value=resp))
        result = await github.is_ancestor_of("tok", "o", "r", "cafe" * 10, "main")
        assert result is True

    @pytest.mark.asyncio
    async def test_status_diverged_means_not_ancestor(self, mocker):
        """compare status 'diverged' → the descendant does NOT contain the
        ancestor → False."""
        from app import github

        resp = mocker.Mock()
        resp.status_code = 200
        resp.json = mocker.Mock(return_value={"status": "diverged"})
        mocker.patch("app.github._github_request", new=AsyncMock(return_value=resp))
        result = await github.is_ancestor_of("tok", "o", "r", "cafe" * 10, "main")
        assert result is False

    @pytest.mark.asyncio
    async def test_status_behind_means_not_ancestor(self, mocker):
        """compare status 'behind' → False."""
        from app import github

        resp = mocker.Mock()
        resp.status_code = 200
        resp.json = mocker.Mock(return_value={"status": "behind"})
        mocker.patch("app.github._github_request", new=AsyncMock(return_value=resp))
        result = await github.is_ancestor_of("tok", "o", "r", "cafe" * 10, "main")
        assert result is False

    @pytest.mark.asyncio
    async def test_non_200_returns_none(self, mocker):
        """A non-200 response → None (indeterminate, NOT False) so the caller
        can distinguish 'definitely not an ancestor' from 'could not tell'."""
        from app import github

        resp = mocker.Mock()
        resp.status_code = 404
        resp.json = mocker.Mock(return_value={})
        mocker.patch("app.github._github_request", new=AsyncMock(return_value=resp))
        result = await github.is_ancestor_of("tok", "o", "r", "cafe" * 10, "main")
        assert result is None

    @pytest.mark.asyncio
    async def test_unparseable_body_returns_none(self, mocker):
        """An unparseable 200 body → None (indeterminate)."""
        from app import github

        resp = mocker.Mock()
        resp.status_code = 200
        resp.json = mocker.Mock(side_effect=ValueError("bad json"))
        mocker.patch("app.github._github_request", new=AsyncMock(return_value=resp))
        result = await github.is_ancestor_of("tok", "o", "r", "cafe" * 10, "main")
        assert result is None


# ===========================================================================
# Round 7 — github.get_pull_request_merge_state: robust merged disambiguation
# ===========================================================================


class TestGetPullRequestMergeState:
    """github.get_pull_request_merge_state queries the PR resource itself for
    its authoritative ``merged`` boolean — the primitive that lets promote()
    distinguish an already-merged PR from a genuine merge block (concern 3)."""

    @pytest.mark.asyncio
    async def test_merged_true(self, mocker):
        """A PR whose resource reports merged=True → True."""
        from app import github

        resp = mocker.Mock()
        resp.status_code = 200
        resp.json = mocker.Mock(return_value={"number": 7, "merged": True})
        mocker.patch("app.github._github_request", new=AsyncMock(return_value=resp))
        result = await github.get_pull_request_merge_state("tok", "o", "r", 7)
        assert result is True

    @pytest.mark.asyncio
    async def test_merged_false(self, mocker):
        """A PR whose resource reports merged=False → False (authoritative —
        the PR genuinely has not merged)."""
        from app import github

        resp = mocker.Mock()
        resp.status_code = 200
        resp.json = mocker.Mock(return_value={"number": 7, "merged": False})
        mocker.patch("app.github._github_request", new=AsyncMock(return_value=resp))
        result = await github.get_pull_request_merge_state("tok", "o", "r", 7)
        assert result is False

    @pytest.mark.asyncio
    async def test_non_200_returns_none(self, mocker):
        """A non-200 (404 / API error) → None (indeterminate, NOT False) so the
        caller can fall back rather than misread a transient failure as
        'definitely not merged'."""
        from app import github

        resp = mocker.Mock()
        resp.status_code = 404
        resp.json = mocker.Mock(return_value={})
        mocker.patch("app.github._github_request", new=AsyncMock(return_value=resp))
        result = await github.get_pull_request_merge_state("tok", "o", "r", 7)
        assert result is None

    @pytest.mark.asyncio
    async def test_unparseable_body_returns_none(self, mocker):
        """An unparseable 200 body → None (indeterminate)."""
        from app import github

        resp = mocker.Mock()
        resp.status_code = 200
        resp.json = mocker.Mock(side_effect=ValueError("bad json"))
        mocker.patch("app.github._github_request", new=AsyncMock(return_value=resp))
        result = await github.get_pull_request_merge_state("tok", "o", "r", 7)
        assert result is None

    @pytest.mark.asyncio
    async def test_missing_merged_field_returns_none(self, mocker):
        """A 200 body with no boolean ``merged`` field → None (indeterminate) —
        the helper never guesses."""
        from app import github

        resp = mocker.Mock()
        resp.status_code = 200
        resp.json = mocker.Mock(return_value={"number": 7})  # no 'merged'
        mocker.patch("app.github._github_request", new=AsyncMock(return_value=resp))
        result = await github.get_pull_request_merge_state("tok", "o", "r", 7)
        assert result is None


# ===========================================================================
# Round 7 — promote(): merged=False disambiguation via the PR's own state
# (concern 3 — an already-merged PR must never be recorded as merge_blocked)
# ===========================================================================


@pytest.mark.asyncio
async def test_promote_merged_false_but_pr_state_merged_is_success(db_session, mocker):
    """Concern 3: merge_pull_request returns {"merged": False} (GitHub's merge
    endpoint returns 405 once a PR is already merged), AND the eventually-
    consistent compare/ancestry API still lags (is_ancestor_of → False).  The
    PR's OWN merged boolean is True, so promote() records SUCCESS — a genuinely
    successful promotion is NOT misclassified as merge_blocked just because the
    ancestry API has not caught up yet."""
    _project, version = await _make_project_and_version(db_session)
    candidate = "pr5tate0" * 5
    merge_sha = "pr5merg0" * 5

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=_branch_sha_mock(main=merge_sha),
    )
    # is_ancestor_of: False on BOTH the pre-check and any later call — the
    # compare API is lagging right after the merge.  Without the PR-state
    # check this candidate would be misrecorded as merge_blocked.
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=False))
    # The PR resource itself reports merged=True — the authoritative signal.
    pr_state_mock = mocker.patch(
        "app.github.get_pull_request_merge_state",
        new=AsyncMock(return_value=True),
    )
    mocker.patch("app.github.create_branch", new=_create_branch_mock())
    mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"number": 31, "html_url": "http://pr/31"}),
    )
    # merge returns 405-style {"merged": False} — the PR was already merged.
    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(return_value={"merged": False, "message": "not mergeable"}),
    )
    delete_mock = mocker.patch(
        "app.github.delete_branch", new=AsyncMock(return_value=True)
    )
    mocker.patch(
        "app.github.create_tag",
        new=AsyncMock(return_value={"ref": "refs/tags/v1"}),
    )

    result = await rq.promote(
        db_session,
        version,
        candidate,
        composite={"confidence": 0.9},
        base_record={"candidate_sha": candidate},
    )
    await db_session.commit()

    # Recorded as a SUCCESS — NOT merge_blocked — purely on the PR-state signal.
    assert result["status"] == "promoted"
    assert result["status"] != "promotion_failed"
    merge_mock.assert_awaited()  # the merge WAS attempted
    pr_state_mock.assert_awaited()  # the PR's own merge state WAS consulted
    delete_mock.assert_awaited()  # branch cleaned up

    await db_session.refresh(version)
    assert version.status == "promoted"
    assert version.last_qualification["status"] == "promoted"
    assert version.last_qualification.get("already_landed") is True
    # The post-merge re-resolve of main HEAD becomes promoted_sha.
    assert version.promoted_sha == merge_sha


@pytest.mark.asyncio
async def test_promote_merged_false_and_pr_state_not_merged_is_failure(
    db_session, mocker
):
    """Concern 3 boundary: merge_pull_request returns {"merged": False} AND the
    PR's own state reports merged=False — the PR genuinely did not merge (a real
    checks/reviews block).  This is recorded as merge_blocked.  The authoritative
    PR-state False is trusted directly; the lagging ancestry API is NOT consulted
    to second-guess a definitive 'not merged'."""
    _project, version = await _make_project_and_version(db_session)
    candidate = "n0tm3rg0" * 5

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch("app.github.get_branch_sha", new=_branch_sha_mock())
    # Pre-check ancestry False so promote() proceeds to the merge step.
    ancestor_mock = mocker.patch(
        "app.github.is_ancestor_of", new=AsyncMock(return_value=False)
    )
    # The PR resource itself reports merged=False — authoritative not-merged.
    pr_state_mock = mocker.patch(
        "app.github.get_pull_request_merge_state",
        new=AsyncMock(return_value=False),
    )
    mocker.patch("app.github.create_branch", new=_create_branch_mock())
    mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"number": 32, "html_url": "http://pr/32"}),
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
    mocker.patch("app.github.delete_branch", new=AsyncMock(return_value=True))

    result = await rq.promote(
        db_session, version, candidate, composite={"confidence": 0.9}
    )
    await db_session.commit()

    # A genuine merge block (conflict=True) — recorded as a failure, not papered over.
    assert result["status"] == "promotion_failed"
    assert result["error"] == "merge_blocked"
    pr_state_mock.assert_awaited()
    # The ancestry API was consulted ONLY for the pre-check (call #1); the
    # merged=False path trusted the authoritative PR-state and did NOT make a
    # second ancestry call to second-guess it.
    assert ancestor_mock.await_count == 1

    await db_session.refresh(version)
    assert version.status == "open"
    assert version.phase == "stabilize"
    assert version.last_qualification["status"] == "promotion_failed"


@pytest.mark.asyncio
async def test_promote_merged_false_pr_state_indeterminate_falls_back_to_ancestry(
    db_session, mocker
):
    """Concern 3 fallback: merge_pull_request returns {"merged": False} and the
    PR-state query is indeterminate (API error → None).  promote() falls back to
    the candidate-on-main ancestry check; with the candidate on main it records
    SUCCESS."""
    _project, version = await _make_project_and_version(db_session)
    candidate = "fa11bck0" * 5
    merge_sha = "fa11mrg0" * 5

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=_branch_sha_mock(main=merge_sha),
    )
    # Pre-check ancestry False (proceed to merge); the post-merge fallback
    # ancestry check returns True (the candidate did in fact land).
    ancestry_calls = {"n": 0}

    async def _ancestor(token, owner, repo, anc, desc, project_id=None):
        ancestry_calls["n"] += 1
        return ancestry_calls["n"] >= 2

    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(side_effect=_ancestor))
    # PR-state query is indeterminate — None — so promote() must fall back.
    pr_state_mock = mocker.patch(
        "app.github.get_pull_request_merge_state",
        new=AsyncMock(return_value=None),
    )
    mocker.patch("app.github.create_branch", new=_create_branch_mock())
    mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"number": 33, "html_url": "http://pr/33"}),
    )
    mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(return_value={"merged": False, "message": "not mergeable"}),
    )
    mocker.patch("app.github.delete_branch", new=AsyncMock(return_value=True))
    mocker.patch(
        "app.github.create_tag",
        new=AsyncMock(return_value={"ref": "refs/tags/v1"}),
    )

    result = await rq.promote(
        db_session, version, candidate, composite={"confidence": 0.9}
    )
    await db_session.commit()

    # PR-state was indeterminate → ancestry fallback → candidate on main →
    # SUCCESS.
    assert result["status"] == "promoted"
    pr_state_mock.assert_awaited()
    # The fallback ancestry check ran (pre-check + post-merge fallback).
    assert ancestry_calls["n"] >= 2

    await db_session.refresh(version)
    assert version.status == "promoted"
    assert version.last_qualification.get("already_landed") is True


# ===========================================================================
# Round 7 — concern 1: promote() must NOT commit a caller's unrelated
# transaction.  maybe_finalize_qualification opens its OWN dedicated session.
# ===========================================================================


@pytest.mark.asyncio
async def test_maybe_finalize_takes_no_caller_session(db_session, mocker):
    """Concern 1 (structural, both dialects): maybe_finalize_qualification has
    NO ``session`` parameter — it cannot, by construction, operate on or commit
    a caller's session.  It opens its OWN session via the injected factory; the
    factory IS invoked exactly once for a real qualifier child."""
    # Isolate the factory-count invariant from the post-commit strategic
    # re-trigger (covered by its own tests), which opens its OWN session via the
    # same factory — so the finalize chain's single dedicated session stays the
    # only factory call this assertion counts.
    mocker.patch(
        "app.release_qualifier._maybe_trigger_strategic_after_qualification",
        new=AsyncMock(return_value=None),
    )
    import inspect

    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = False

    # The public signature must not accept a session — passing one is an error.
    params = list(inspect.signature(rq.maybe_finalize_qualification).parameters)
    assert "session" not in params
    assert params[0] == "completed_task"

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
                {"audit_score": 1.0, "test_erosion": {"deleted_test_files": []}}
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

    # Wrap the shared-session factory so we can assert it was the source of the
    # session the finalize chain ran on.
    factory_calls = {"n": 0}
    base_factory = _shared_session_factory(db_session)

    def _counting_factory():
        factory_calls["n"] += 1
        return base_factory()

    result = await rq.maybe_finalize_qualification(
        smoke_task, session_factory=_counting_factory
    )
    await db_session.commit()

    # The dedicated session was opened via the injected factory — exactly once.
    assert factory_calls["n"] == 1
    # And the finalize actually ran on it.
    assert result is not None
    assert result["status"] == "dry_run"
    await db_session.refresh(version)
    assert version.last_qualification["status"] == "dry_run"


# ===========================================================================
# Round 8 — promote(): pending-CI block → enable auto-merge, no failure verdict
# ===========================================================================


@pytest.mark.asyncio
async def test_promote_pending_ci_enables_auto_merge_no_failure(db_session, mocker):
    """When merge_pull_request returns merged=False with no conflict flag
    (GitHub 405 — required checks still pending), promote() must NOT record a
    failure verdict.  Instead it enables GitHub auto-merge so the PR merges
    automatically once the checks pass.  last_qualification stays "pending"
    so the reaper can confirm success when auto-merge lands."""
    _project, version = await _make_project_and_version(db_session)
    candidate = "p3nd1ng0" * 5

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=False))
    # PR not yet merged (pending CI, not a conflict).
    mocker.patch(
        "app.github.get_pull_request_merge_state",
        new=AsyncMock(return_value=False),
    )
    mocker.patch("app.github.get_branch_sha", new=_branch_sha_mock())
    mocker.patch("app.github.create_branch", new=_create_branch_mock())
    mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"number": 99, "html_url": "http://pr/99"}),
    )
    # 405-style: not merged, no conflict flag — pending required checks.
    mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(
            return_value={"merged": False, "message": "required checks pending"}
        ),
    )
    am_mock = mocker.patch(
        "app.github.enable_auto_merge",
        new=AsyncMock(return_value={"enabled": True, "message": "auto-merge enabled"}),
    )
    mocker.patch("app.github.delete_branch", new=AsyncMock(return_value=True))

    result = await rq.promote(
        db_session,
        version,
        candidate,
        composite={"confidence": 0.9657},
        base_record={"confidence": 0.9657, "sub_scores": {"ci": 1.0}},
    )
    await db_session.commit()

    # Pending CI → auto-merge enabled, NOT a promotion failure.
    assert result["status"] == "pending_auto_merge"
    assert result["auto_merge_enabled"] is True
    am_mock.assert_awaited_once()

    # No failure recorded — status is not promotion_failed.
    await db_session.refresh(version)
    lq = version.last_qualification or {}
    assert lq.get("status") != "promotion_failed"
    assert "error" not in lq

    # Durable promotion_pending sub-state is now written so the reaper can
    # distinguish this version (qualification PASSED) from a hung qualifier child.
    pending = lq.get("promotion_pending") or {}
    assert pending.get("pr_number") == 99
    assert pending.get("pr_url") == "http://pr/99"
    assert pending.get("auto_merge_enabled") is True
    assert "enabled_at" in pending

    # The spawn-time "pending" record carried NO confidence (the composite is
    # only computed at finalize), so promote() must merge base_record's composite
    # into the durable promotion_pending record.  Otherwise the later reconcile
    # path records the promotion at "confidence 0.00" (the v6 prod regression).
    assert lq.get("confidence") == 0.9657
    assert lq.get("sub_scores") == {"ci": 1.0}
    # The merge must NOT clobber the "pending" status the substate relies on.
    assert lq.get("status") == "pending"


@pytest.mark.asyncio
async def test_promote_pending_ci_migration_pr_skips_auto_merge(db_session, mocker):
    """A migration-carrying promotion PR that returns pending-CI (GitHub 405,
    no conflict) must NOT be handed to GitHub native auto-merge — that would
    reopen the dual-head TOCTOU window (the promotion target can advance while
    checks are pending, so the eventual native merge could land against a
    different migration chain than the one Blueprint validated). Parity with the
    task-PR and rollup auto-merge sites: promote() holds the version in its
    existing retryable state (open + stabilize, no promotion_pending sub-state)
    so the next strategic cycle re-runs the pre-merge guard against the live
    promotion target before merging."""
    _project, version = await _make_project_and_version(db_session)
    candidate = "m1grat10" * 5

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=False))
    mocker.patch(
        "app.github.get_pull_request_merge_state",
        new=AsyncMock(return_value=False),
    )
    mocker.patch("app.github.get_branch_sha", new=_branch_sha_mock())
    mocker.patch("app.github.create_branch", new=_create_branch_mock())
    mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"number": 99, "html_url": "http://pr/99"}),
    )
    # 405-style: not merged, no conflict flag — pending required checks.
    mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(
            return_value={"merged": False, "message": "required checks pending"}
        ),
    )
    am_mock = mocker.patch(
        "app.github.enable_auto_merge",
        new=AsyncMock(return_value={"enabled": True, "message": "auto-merge enabled"}),
    )
    delete_branch_mock = mocker.patch(
        "app.github.delete_branch", new=AsyncMock(return_value=True)
    )
    # The pre-merge guard passes (validates clean against the target AT check
    # time) so execution reaches the pending-CI auto-merge fallback; the new
    # exclusion then diverts a migration-carrying PR away from native auto-merge.
    mocker.patch(
        "app.rollup_migration_validation.enforce_premerge_migration_guard",
        new=AsyncMock(return_value=(True, None)),
    )
    # PR touches an Alembic migration → pr_touches_migrations returns True.
    mocker.patch(
        "app.github.get_pr_files",
        new=AsyncMock(
            return_value=[{"filename": "backend/alembic/versions/0042_feature.py"}]
        ),
    )

    result = await rq.promote(
        db_session,
        version,
        candidate,
        composite={"confidence": 0.9},
        base_record={"candidate_sha": candidate},
    )
    await db_session.commit()

    # Native auto-merge was NEVER delegated.
    am_mock.assert_not_called()
    # Retryable hold recorded instead of pending_auto_merge.
    assert result["status"] == "promotion_failed"
    assert result["error"] == "migration_pending_no_automerge"

    await db_session.refresh(version)
    # Version stays open + stabilize so it re-qualifies next cycle.
    assert version.status == "open"
    assert version.phase == "stabilize"
    assert version.last_qualification["status"] == "promotion_failed"
    assert version.last_qualification["error"] == "migration_pending_no_automerge"
    # No promotion_pending sub-state was written — there is no native auto-merge
    # in flight for the reaper to reconcile.
    assert "promotion_pending" not in version.last_qualification
    # The release branch was cleaned up since the merge never ran.
    delete_branch_mock.assert_awaited()


@pytest.mark.asyncio
async def test_promote_does_not_commit_caller_unrelated_transaction(db_session, mocker):
    """Concern 1 (behavioral): promote()'s intermediate commit (which releases
    the row lock before its GitHub I/O) must NOT commit a caller's UNRELATED
    open transaction — the completion handler's terminal-status / TaskHistory /
    Artifact / remediation work.

    The fix is that the finalize→promote chain runs on its OWN dedicated
    session, so promote()'s commit only ever flushes qualifier state.  This
    test proves it: a genuinely-separate caller session holds an uncommitted
    unrelated Task; after the dedicated-session promote chain runs (intermediate
    commit included), the caller's Task is STILL uncommitted — rolling the
    caller session back discards it.

    Requires genuinely independent DB transactions (a real second connection),
    which the SQLite in-memory StaticPool harness cannot provide — so it is
    skipped there and runs under Postgres (the test-backend.sh default + CI)."""
    if db_session.bind.dialect.name == "sqlite":
        pytest.skip(
            "needs two independent DB transactions — SQLite in-memory shares "
            "one connection; runs under Postgres"
        )

    from app import shared_state
    from app.database import AsyncSessionLocal

    shared_state.runtime_config["promotion_enabled"] = True

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
                    "test_erosion": {
                        "deleted_test_files": [],
                        "assertion_delta": 5,
                        "test_count_delta": 2,
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

    # promote() will run a full merge — patch the GitHub surface.
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=_branch_sha_mock(main="caller00" * 5),
    )
    _patch_promote_github_defaults(mocker)
    mocker.patch("app.github.create_branch", new=_create_branch_mock())
    mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"number": 50, "html_url": "http://pr/50"}),
    )
    mocker.patch(
        "app.github.create_tag",
        new=AsyncMock(return_value={"ref": "refs/tags/v1"}),
    )

    # A genuinely separate "caller" session with UNRELATED uncommitted work —
    # this stands in for the task-completion handler's open transaction.
    caller_session = AsyncSessionLocal()
    try:
        unrelated = Task(
            project_id=project.id,
            title="UNRELATED-caller-work",
            payloads=[{"type": "fix"}],
            vm_instance_id="vm-unrelated-caller",
            status=TaskStatus.IN_PROGRESS,
        )
        caller_session.add(unrelated)
        await caller_session.flush()  # in the caller txn, NOT committed
        unrelated_id = unrelated.id

        # The merge hook fires DURING promote() — at that point promote() has
        # already issued its intermediate commit.  If that commit had run on
        # the caller's session it would have committed `unrelated`.  Assert the
        # caller's row is still invisible to a fresh connection at this moment.
        async def _merge_and_probe(*args, **kwargs):
            async with AsyncSessionLocal() as probe:
                leaked = await probe.get(Task, unrelated_id)
            assert leaked is None, (
                "promote()'s intermediate commit leaked the caller's "
                "unrelated transaction"
            )
            return {"merged": True, "message": "Merged"}

        mocker.patch(
            "app.github.merge_pull_request",
            new=AsyncMock(side_effect=_merge_and_probe),
        )

        # Run the real finalize→promote chain on its own dedicated session.
        result = await rq.maybe_finalize_qualification(smoke_task)
        assert result["status"] == "promoted"

        # The dedicated-session promotion committed qualifier state...
        async with AsyncSessionLocal() as verify:
            v = await verify.get(rv.ReleaseVersion, version.id)
            assert v is not None
            assert v.status == "promoted"
            # ...but the caller's unrelated Task is STILL uncommitted.
            leaked_after = await verify.get(Task, unrelated_id)
            assert leaked_after is None, (
                "the caller's unrelated transaction was committed by the finalize chain"
            )

        # The caller still owns its transaction: rolling back discards the
        # unrelated work cleanly (it was never committed by promote()).
        await caller_session.rollback()
        async with AsyncSessionLocal() as final:
            assert await final.get(Task, unrelated_id) is None
    finally:
        await caller_session.close()


@pytest.mark.asyncio
async def test_concurrent_finalize_does_not_double_promote(db_session, mocker):
    """Concern 4(d): two finalize attempts on the same promote-eligible version
    must promote it exactly ONCE.

    The dedicated session FOR UPDATE-locks the version row at the top of the
    critical section, so two concurrent callers are serialized — concurrent
    becomes sequential.  The first finalizes (promote → status='promoted'); the
    second, on acquiring the lock, sees ``last_qualification.status`` is no
    longer 'pending' (and ``status`` no longer 'open') and bails.  Driving the
    finalize path twice in sequence is exactly that serialized resolution: the
    structural guard must yield exactly one promotion — one v2 row, merge
    called once."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = True

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
                    "test_erosion": {
                        "deleted_test_files": [],
                        "assertion_delta": 8,
                        "test_count_delta": 3,
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

    merge_sha = "dbl3prom" * 5
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=_branch_sha_mock(main=merge_sha),
    )
    _patch_promote_github_defaults(mocker)
    mocker.patch("app.github.create_branch", new=_create_branch_mock())
    mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"number": 60, "html_url": "http://pr/60"}),
    )
    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(return_value={"merged": True, "message": "Merged"}),
    )
    mocker.patch(
        "app.github.create_tag",
        new=AsyncMock(return_value={"ref": "refs/tags/v1"}),
    )

    factory = _shared_session_factory(db_session)

    # First finalize — promotes v1, opens v2.
    first = await rq.maybe_finalize_qualification(smoke_task, session_factory=factory)
    await db_session.commit()
    assert first is not None
    assert first["status"] == "promoted"

    # Second finalize (the serialized "concurrent" caller) — the version is now
    # promoted; the under-lock guard must make this a no-op.
    second = await rq.maybe_finalize_qualification(smoke_task, session_factory=factory)
    await db_session.commit()
    # No second finalize ran — the pending guard short-circuited it.
    assert second is None

    # The merge was attempted exactly ONCE — no double-merge.
    assert merge_mock.await_count == 1

    # Exactly TWO ReleaseVersion rows exist for the project: v1 (promoted) and
    # the single v2 the first finalize opened — NO duplicate v2.
    from sqlmodel import select as _select

    all_versions = (
        (
            await db_session.execute(
                _select(rv.ReleaseVersion).where(
                    rv.ReleaseVersion.project_id == project.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(all_versions) == 2
    numbers = sorted(v.version_number for v in all_versions)
    assert numbers == [1, 2]
    v1 = next(v for v in all_versions if v.version_number == 1)
    assert v1.status == "promoted"


@pytest.mark.asyncio
async def test_overlapping_promote_does_not_double_promote(db_session, mocker):
    """Round 9 (concurrency, genuine overlap): the SECOND finalize is launched
    from INSIDE the first finalize's merge hook — a true overlapping promote,
    not the post-promotion sequential case ``test_concurrent_finalize_does_not_
    double_promote`` covers.

    promote() issues its intermediate commit (releasing the ReleaseVersion row
    lock) BEFORE its GitHub I/O, with ``last_qualification.status`` still
    'pending'.  So a concurrent caller that fires mid-merge genuinely passes the
    pending guard and enters its own promote() — the pending guard is NOT what
    serializes this.  The real safety net is the idempotency machinery
    (``_candidate_already_on_main``) plus the ``status != "open"`` re-check in
    ``_record_promotion_success`` / ``_record_promotion_failure``.

    This mirrors the mid-merge-callback pattern of
    ``test_promote_does_not_commit_caller_unrelated_transaction``: the merge
    hook reentrantly runs the second finalize on its OWN genuinely-independent
    dedicated session.  Like that test it needs two real independent DB
    transactions (the first finalize's intermediate commit must release a row
    lock the second can then acquire), which the single-connection SQLite
    in-memory harness cannot provide — so it is skipped there and runs under
    Postgres (the test-backend.sh default + CI).  The dedicated sessions are
    built from the test engine (``db_session.bind``) rather than the global
    ``app.database.AsyncSessionLocal`` — that engine's pool is loop-bound and
    reused across pytest-asyncio's per-test event loops, so opening sessions on
    it here would trip an 'attached to a different loop' error; the test engine
    is the one already on this test's loop.  Despite the genuine overlap the
    result must be exactly ONE merge call and exactly ONE v2 ReleaseVersion
    row."""
    if db_session.bind.dialect.name == "sqlite":
        pytest.skip(
            "needs two independent DB transactions for a genuine overlapping "
            "promote — SQLite in-memory shares one connection; runs under "
            "Postgres"
        )

    from contextlib import asynccontextmanager

    from sqlalchemy.ext.asyncio import AsyncSession as _AsyncSession

    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = True

    # A dedicated-session factory bound to the SAME engine the test fixture
    # uses (db_session.bind) — genuinely independent transactions / row locks,
    # but on this test's event loop (avoiding the global AsyncSessionLocal
    # cross-loop pool trap).  Each session it yields IS closed on exit, so the
    # finalize chain owns its own lifecycle exactly as it would in production.
    _test_engine = db_session.bind

    @asynccontextmanager
    async def _dedicated_factory():
        async with _AsyncSession(_test_engine, expire_on_commit=False) as s:
            yield s

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
                    "test_erosion": {
                        "deleted_test_files": [],
                        "assertion_delta": 8,
                        "test_count_delta": 3,
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

    merge_sha = "0v3rlap0" * 5
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=_branch_sha_mock(main=merge_sha),
    )
    # is_ancestor_of is the idempotency primitive.  It is False before the first
    # promotion's merge lands and True after — so the SECOND finalize, launched
    # mid-merge once the first merge has been marked landed, sees the candidate
    # already on main and routes straight to the idempotent-success path
    # WITHOUT a second merge.
    merge_landed = {"done": False}

    async def _is_ancestor(token, owner, repo, anc, desc, project_id=None):
        return merge_landed["done"]

    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(side_effect=_is_ancestor))
    mocker.patch(
        "app.github.get_pull_request_merge_state",
        new=AsyncMock(return_value=False),
    )
    mocker.patch("app.github.delete_branch", new=AsyncMock(return_value=True))
    mocker.patch("app.github.create_branch", new=_create_branch_mock())
    mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"number": 70, "html_url": "http://pr/70"}),
    )
    mocker.patch(
        "app.github.create_tag",
        new=AsyncMock(return_value={"ref": "refs/tags/v1"}),
    )

    second_result = {}

    async def _merge_then_reentrant_finalize(*args, **kwargs):
        # The first promote() is mid-merge here — its intermediate commit has
        # already run, so the ReleaseVersion row lock is released and
        # last_qualification is still 'pending'.  Launch the
        # genuinely-overlapping second finalize NOW, on its OWN independent
        # dedicated session (a real second connection from the test engine, NOT
        # the shared test session) so it is a true concurrent promote and not a
        # reentrant commit on this transaction.  The merge hook is THIS
        # function, so we mark merge_landed BEFORE the reentrant call: the inner
        # finalize's idempotency pre-check (is_ancestor_of) then sees the
        # candidate already on main and takes the no-merge idempotent-success
        # path — which is what avoids a recursive merge.
        if not merge_landed["done"]:
            merge_landed["done"] = True
            second_result["value"] = await rq.maybe_finalize_qualification(
                smoke_task, session_factory=_dedicated_factory
            )
        return {"merged": True, "message": "Merged"}

    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(side_effect=_merge_then_reentrant_finalize),
    )

    # The first finalize runs on its own dedicated session (test-engine factory)
    # — fully independent of this test's db_session.
    first = await rq.maybe_finalize_qualification(
        smoke_task, session_factory=_dedicated_factory
    )

    # The first finalize promoted v1.
    assert first is not None
    assert first["status"] == "promoted"

    # The genuinely-overlapping second finalize ran its own promote() but the
    # idempotency pre-check short-circuited it — it recorded success WITHOUT a
    # second merge (status 'promoted', possibly flagged already_promoted /
    # already_landed depending on which call won the status!="open" re-check).
    assert second_result["value"] is not None
    assert second_result["value"]["status"] == "promoted"

    # merge_pull_request was called EXACTLY once despite the genuine overlap —
    # the second promote() took the candidate-already-on-main idempotent path.
    assert merge_mock.await_count == 1

    # Exactly ONE v2 ReleaseVersion row — no duplicate from the overlapping
    # promote.  Read through a fresh dedicated connection: the committed
    # cross-session state is what matters (db_session would not see the
    # separate-connection commits without an expire/re-read).
    from sqlmodel import select as _select

    async with _dedicated_factory() as verify:
        all_versions = (
            (
                await verify.execute(
                    _select(rv.ReleaseVersion).where(
                        rv.ReleaseVersion.project_id == project.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(all_versions) == 2
        assert sorted(v.version_number for v in all_versions) == [1, 2]
        v1 = next(v for v in all_versions if v.version_number == 1)
        assert v1.status == "promoted"
        # v1's terminal record is the promoted record — never overwritten with
        # a contradictory promotion_failed.
        assert v1.last_qualification["status"] == "promoted"


@pytest.mark.asyncio
async def test_record_promotion_failure_does_not_overwrite_promoted_version(
    db_session, mocker
):
    """Round 9 (data integrity): when two finalize/promote calls race and one
    PROMOTES successfully while the loser's merge fails transiently, the loser's
    _record_promotion_failure re-locks the row, sees status='promoted', and must
    NOT overwrite last_qualification with a {status:'promotion_failed'} record —
    that would leave a self-contradictory terminal state (version.status=
    'promoted' yet last_qualification.status='promotion_failed').

    Mirrors ``test_promote_bails_if_version_already_promoted_under_relock`` (the
    SUCCESS-path counterpart) but drives the FAILURE path: the merge hook flips
    the version to 'promoted' out-of-band (a parallel promotion landing first)
    AND returns {merged: False} so the losing promote() routes to
    _record_promotion_failure.  With the guard in place the loser bails as
    already-promoted and the promoted record stands intact."""
    _project, version = await _make_project_and_version(db_session)
    candidate = "l0s3rrac" * 5

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    # is_ancestor_of False — the idempotency pre-check does not short-circuit;
    # this promote() proceeds to open + merge the PR (and the merge fails).
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=False))
    # The PR's own merge state reports False — so the merged=False result is
    # treated as a genuine merge block, routing promote() to
    # _record_promotion_failure (NOT the already-merged success path).
    mocker.patch(
        "app.github.get_pull_request_merge_state",
        new=AsyncMock(return_value=False),
    )
    mocker.patch("app.github.get_branch_sha", new=_branch_sha_mock())
    mocker.patch("app.github.create_branch", new=_create_branch_mock())
    mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"number": 80, "html_url": "http://pr/80"}),
    )
    mocker.patch("app.github.delete_branch", new=AsyncMock(return_value=True))

    # The merge hook: a PARALLEL promotion lands first (direct SQL UPDATE flips
    # the row to 'promoted' with a real promoted record), then this call's own
    # merge returns merged=False — a transient block.  This promote() then runs
    # _record_promotion_failure, which must see status='promoted' under the
    # re-lock and bail.
    from sqlalchemy import update as _sa_update

    winner_record = {
        "status": "promoted",
        "candidate_sha": candidate,
        "promoted_sha": "winn3rsh" * 5,
        "pr_url": "http://pr/winner",
    }

    async def _merge_loses_race(*args, **kwargs):
        await db_session.execute(
            _sa_update(rv.ReleaseVersion)
            .where(rv.ReleaseVersion.id == version.id)
            .values(
                status="promoted",
                promoted_sha="winn3rsh" * 5,
                last_qualification=winner_record,
            )
        )
        await db_session.commit()
        return {"merged": False, "conflict": True, "message": "merge conflict"}

    mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(side_effect=_merge_loses_race),
    )

    result = await rq.promote(
        db_session,
        version,
        candidate,
        composite={"confidence": 0.9},
        base_record={"candidate_sha": candidate},
    )
    await db_session.commit()

    # The losing promote() detected the version is already promoted under the
    # re-lock and bailed as already-promoted — it did NOT record a failure.
    assert result["status"] != "promotion_failed"
    assert result.get("already_promoted") is True

    await db_session.refresh(version)
    # CRITICAL: the terminal state is self-consistent — the promoted record set
    # by the winner stands, NOT a contradictory promotion_failed record.
    assert version.status == "promoted"
    assert version.last_qualification["status"] == "promoted"
    assert version.last_qualification["status"] != "promotion_failed"
    # The winner's record is intact — not clobbered by the loser.
    assert version.last_qualification["promoted_sha"] == "winn3rsh" * 5
    assert version.last_qualification["pr_url"] == "http://pr/winner"
    assert "error" not in version.last_qualification


@pytest.mark.asyncio
async def test_record_promotion_failure_still_records_on_open_version(
    db_session, mocker
):
    """Round 9 (data integrity — boundary): the new status!='open' guard in
    _record_promotion_failure must NOT suppress a genuine failure record when
    the version is legitimately still 'open'.  A real merge block on an
    un-raced version is still recorded as promotion_failed and the version goes
    back to stabilize — the guard converts only the already-promoted race, not
    real failures."""
    _project, version = await _make_project_and_version(db_session)

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=False))
    # The PR's own resource reports merged=False — a genuine, authoritative
    # not-merged.  The version is never raced, so it stays 'open'.
    mocker.patch(
        "app.github.get_pull_request_merge_state",
        new=AsyncMock(return_value=False),
    )
    mocker.patch("app.github.get_branch_sha", new=_branch_sha_mock())
    mocker.patch("app.github.create_branch", new=_create_branch_mock())
    mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"number": 81, "html_url": "http://pr/81"}),
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
    mocker.patch("app.github.delete_branch", new=AsyncMock(return_value=True))

    result = await rq.promote(
        db_session,
        version,
        "0p3nfa1l" * 5,
        composite={"confidence": 0.9},
        base_record={"candidate_sha": "0p3nfa1l" * 5},
    )
    await db_session.commit()

    # The version was open — a real merge conflict is recorded as a failure.
    assert result["status"] == "promotion_failed"
    assert result["error"] == "merge_blocked"

    await db_session.refresh(version)
    assert version.status == "open"
    assert version.phase == "stabilize"
    assert version.last_qualification["status"] == "promotion_failed"
    assert version.last_qualification["error"] == "merge_blocked"


# ===========================================================================
# Round 7 — concern 4: _relock_version_for_promotion_write must not expire()
# un-flushed in-memory mutations on the early-return (lock-not-released) path.
# ===========================================================================


@pytest.mark.asyncio
async def test_relock_lock_not_released_preserves_unflushed_mutation(db_session):
    """Concern 4: on an early-return failure path BEFORE promote()'s
    intermediate commit, the version row is still locked by this transaction.
    _relock_version_for_promotion_write(lock_released=False) must return the
    LIVE instance WITHOUT session.expire() — an expire here would silently
    discard un-flushed in-memory mutations on the version."""
    _project, version = await _make_project_and_version(db_session)

    # Mutate the in-memory instance but do NOT flush/commit — exactly the state
    # an early-return failure path could be in.
    version.last_qualification = {"status": "pending", "sentinel": "unflushed"}
    from sqlalchemy.orm.attributes import flag_modified

    flag_modified(version, "last_qualification")

    resolved = await rq._relock_version_for_promotion_write(
        db_session, version, lock_released=False
    )

    # The SAME live instance is returned, and its un-flushed mutation survives —
    # it was NOT discarded by an expire().
    assert resolved is version
    assert resolved.last_qualification == {
        "status": "pending",
        "sentinel": "unflushed",
    }


@pytest.mark.asyncio
async def test_relock_lock_released_rereads_authoritative_db_state(db_session):
    """Concern 4 counterpart: when the lock HAS been released (lock_released=
    True — promote()'s intermediate commit ran + the unlocked GitHub I/O is
    done), the helper re-reads + re-locks the row from AUTHORITATIVE DB state.

    A concurrent finalize / crash-retry could have changed the row while the
    lock was released; the re-read must reflect that committed change rather
    than the now-stale in-memory instance.  This is what makes the
    ``status != "open"`` re-check in _record_promotion_success able to detect a
    concurrent promotion.  Simulated with a direct SQL UPDATE (a stand-in for
    the concurrent writer) — the re-locked instance must reflect it."""
    from sqlalchemy import update as _sa_update

    _project, version = await _make_project_and_version(db_session)

    # Commit a known starting state; the in-memory instance is clean (exactly
    # promote()'s state right after its intermediate commit).
    version.status = "open"
    db_session.add(version)
    await db_session.commit()
    assert version.status == "open"

    # A concurrent writer flips the row in the DB out-of-band (direct SQL — no
    # ORM identity-map interaction), exactly as a parallel promotion would.
    await db_session.execute(
        _sa_update(rv.ReleaseVersion)
        .where(rv.ReleaseVersion.id == version.id)
        .values(status="promoted")
    )
    await db_session.commit()

    resolved = await rq._relock_version_for_promotion_write(
        db_session, version, lock_released=True
    )

    # The re-read reflects the authoritative committed DB state ("promoted"),
    # not the now-stale in-memory instance — so the under-relock status re-check
    # can correctly detect the concurrent promotion.
    assert resolved is not None
    assert resolved.status == "promoted"


# ===========================================================================
# Round 7 — concern 5: dangling-branch cleanup + labeled pr_url
# ===========================================================================


@pytest.mark.asyncio
async def test_promote_pr_missing_number_cleans_up_release_branch(db_session, mocker):
    """Concern 5: when create_pull_request returns a PR with no number, the
    release-v{N} branch (already created) cannot be merged — promote() must
    best-effort delete it so it does not dangle, then record the failure."""
    _project, version = await _make_project_and_version(db_session)

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch("app.github.get_branch_sha", new=_branch_sha_mock())
    _patch_promote_github_defaults(mocker)
    mocker.patch("app.github.create_branch", new=_create_branch_mock())
    # PR object with NO 'number' field (and not an empty_diff).
    mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"html_url": "http://pr/nonumber"}),
    )
    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(return_value={"merged": True}),
    )
    delete_mock = mocker.patch(
        "app.github.delete_branch", new=AsyncMock(return_value=True)
    )

    result = await rq.promote(
        db_session, version, "n0num000" * 5, composite={"confidence": 0.9}
    )
    await db_session.commit()

    assert result["status"] == "promotion_failed"
    assert result["error"] == "pr_missing_number"
    # No merge attempted — there is no PR number to merge.
    merge_mock.assert_not_called()
    # The dangling release-v1 branch was cleaned up.
    delete_mock.assert_awaited()
    assert any(c.args[3] == "release-v1" for c in delete_mock.call_args_list)

    await db_session.refresh(version)
    assert version.status == "open"
    assert version.phase == "stabilize"
    assert version.last_qualification["status"] == "promotion_failed"


@pytest.mark.asyncio
async def test_promote_idempotent_success_records_meaningful_pr_url(db_session, mocker):
    """Concern 5: the idempotency-success path (candidate already on main) has
    no live PR of its own.  Rather than recording a bare "" pr_url, it carries
    forward a pr_url the caller threaded through base_record when present, and
    otherwise records an explicit human-readable label — never a blank string
    that reads like 'there was no PR'."""
    _project, version = await _make_project_and_version(db_session)
    candidate = "1d3mp0t0" * 5

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    # The idempotency pre-check sees the candidate already on main.
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=True))
    mocker.patch("app.github.delete_branch", new=AsyncMock(return_value=True))
    mocker.patch(
        "app.github.create_tag",
        new=AsyncMock(return_value={"ref": "refs/tags/v1"}),
    )

    # Case A: a prior promotion attempt's PR url is carried in base_record —
    # the idempotent-success record reuses that real url.
    result_a = await rq.promote(
        db_session,
        version,
        candidate,
        composite={"confidence": 0.9},
        base_record={"candidate_sha": candidate, "pr_url": "http://pr/prior"},
    )
    await db_session.commit()
    assert result_a["status"] == "promoted"
    await db_session.refresh(version)
    assert version.last_qualification["pr_url"] == "http://pr/prior"

    # Case B: no carried pr_url — the record gets an explicit label, not "".
    _project_b, version_b = await _make_project_and_version(db_session)
    result_b = await rq.promote(
        db_session,
        version_b,
        candidate,
        composite={"confidence": 0.9},
        base_record={"candidate_sha": candidate},
    )
    await db_session.commit()
    assert result_b["status"] == "promoted"
    await db_session.refresh(version_b)
    recorded = version_b.last_qualification["pr_url"]
    # Not a bare empty string — an explicit, human-readable label.
    assert recorded != ""
    assert "already merged" in recorded.lower()


# ===========================================================================
# Operator notifications — the qualifier posts a ProjectMessage at every verdict
# (promoted / blocked / below-threshold / dry-run / promotion-failed) so the
# operator sees release outcomes without polling.
# ===========================================================================


async def _messages_for(db_session, project_id, event_type=None):
    """ProjectMessage rows for a project, optionally filtered by the context
    event_type the qualifier stamps."""
    result = await db_session.execute(
        select(ProjectMessage).where(ProjectMessage.project_id == project_id)
    )
    rows = list(result.scalars().all())
    if event_type is not None:
        rows = [m for m in rows if (m.context or {}).get("event_type") == event_type]
    return rows


class TestFmtSubScores:
    """_fmt_sub_scores renders a composite's sub-scores for operator messages."""

    def test_renders_all_three_dimensions(self):
        out = rq._fmt_sub_scores({"audit": 0.95, "test_erosion": 1.0, "smoketest": 0.8})
        assert out == "audit 0.95 · test 1.00 · smoke 0.80"

    def test_omits_smoke_when_subsystem_disabled(self):
        out = rq._fmt_sub_scores(
            {"audit": 0.95, "test_erosion": 1.0, "smoketest": 0.0},
            smoketest_enabled=False,
        )
        assert out == "audit 0.95 · test 1.00"
        assert "smoke" not in out

    def test_empty_or_none_returns_na(self):
        assert rq._fmt_sub_scores(None) == "n/a"
        assert rq._fmt_sub_scores({}) == "n/a"


@pytest.mark.asyncio
async def test_finalize_dry_run_posts_operator_message(db_session, mocker):
    """A dry-run verdict (promotion disabled) posts a release_dry_run message."""
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
    mocker.patch("app.release_qualifier.promote", new=AsyncMock())

    await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    msgs = await _messages_for(db_session, project.id, "release_dry_run")
    assert len(msgs) == 1
    assert msgs[0].from_type == "system"
    assert "would promote" in msgs[0].content
    assert msgs[0].context["version_number"] == version.version_number


@pytest.mark.asyncio
async def test_finalize_blocked_posts_operator_message(db_session):
    """A hard blocker posts a release_blocked message naming the blocker."""
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
                "deleted_test_files": ["testing/test_gone.py"],
                "assertion_delta": 0,
                "test_count_delta": -2,
            },
        },
        smoke_logs={
            "passed": True,
            "tests": [{"name": "a", "status": "pass"}],
            "suites": {"startup": {"passed": True}},
        },
    )

    await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    msgs = await _messages_for(db_session, project.id, "release_blocked")
    assert len(msgs) == 1
    assert "blocked from promotion" in msgs[0].content
    assert "deleted a test file" in msgs[0].content
    assert msgs[0].context["blocker"] == "test_file_deleted"


@pytest.mark.asyncio
async def test_finalize_below_threshold_posts_operator_message(db_session):
    """A non-blocked candidate scoring under the threshold posts a
    release_below_threshold message carrying the sub-scores."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = True

    project, version = await _make_project_and_version(db_session)
    # 2 high + 1 low findings → derived audit 0.68, erosion 1.0, smoketest 0.5
    # (half-passing) → composite 0.722, well under the 0.90 threshold, but no
    # hard blocker (audit present, no test deletion, CI green).
    await _seed_qualification_children(
        db_session,
        project,
        version,
        audit_logs={
            "audit_score": 0.5,
            "test_erosion": {
                "deleted_test_files": [],
                "assertion_delta": 0,
                "test_count_delta": 0,
            },
            "findings": [
                {"severity": "low", "summary": "Stale comment in release_scoring."},
                {
                    "severity": "high",
                    "summary": "Unverified retry path.",
                    "verification_note": "Read app/release_promotion.py lines 40-60: retry loop swallows transient errors with no telemetry, confirmed in diff.",
                },
                {
                    "severity": "high",
                    "summary": "Missing null check.",
                    "verification_note": "Read app/release_scoring.py lines 120-135: payload.audit may be None and is dereferenced without guard, confirmed in diff.",
                },
            ],
        },
        smoke_logs={
            "passed": True,
            "tests": [
                {"name": "a", "status": "pass"},
                {"name": "b", "status": "fail"},
            ],
            "suites": {"startup": {"passed": True}},
        },
    )

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    assert result["status"] == "not_promoted"
    assert result["blocker"] is None
    msgs = await _messages_for(db_session, project.id, "release_below_threshold")
    assert len(msgs) == 1
    assert "below the" in msgs[0].content
    assert "Sub-scores:" in msgs[0].content


@pytest.mark.asyncio
async def test_promote_success_posts_operator_message(db_session, mocker):
    """A successful promotion posts a release_promoted message."""
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
    mocker.patch("app.github.get_branch_sha", new=_branch_sha_mock())
    _patch_promote_github_defaults(mocker)
    mocker.patch("app.github.create_branch", new=_create_branch_mock())
    mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"number": 55, "html_url": "http://pr/55"}),
    )
    mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(return_value={"merged": True, "message": "Merged"}),
    )
    mocker.patch(
        "app.github.create_tag",
        new=AsyncMock(return_value={"ref": "refs/tags/v1"}),
    )

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    assert result["status"] == "promoted"
    msgs = await _messages_for(db_session, project.id, "release_promoted")
    assert len(msgs) == 1
    assert "promoted" in msgs[0].content.lower()
    assert msgs[0].context["next_version_number"] == 2


@pytest.mark.asyncio
async def test_promote_failure_posts_operator_message(db_session, mocker):
    """A failed promotion (non-merged merge result) posts a
    release_promotion_failed message."""
    project, version = await _make_project_and_version(db_session)

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch("app.github.get_branch_sha", new=_branch_sha_mock())
    _patch_promote_github_defaults(mocker)
    mocker.patch("app.github.create_branch", new=_create_branch_mock())
    mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"number": 92, "html_url": "http://pr/92"}),
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
        composite={"confidence": 0.95},
        base_record={"candidate_sha": "abcd" * 10},
    )
    await db_session.commit()

    assert result["status"] == "promotion_failed"
    msgs = await _messages_for(db_session, project.id, "release_promotion_failed")
    assert len(msgs) == 1
    assert "merge failed" in msgs[0].content
    assert msgs[0].context["error"] == "merge_blocked"


@pytest.mark.asyncio
async def test_notify_operator_swallows_failure(db_session, mocker):
    """A messaging failure inside _notify_operator must never propagate — the
    release verdict is already decided by the time it runs."""
    _project, version = await _make_project_and_version(db_session)
    mocker.patch(
        "app.release_promotion.post_event_message",
        new=AsyncMock(side_effect=RuntimeError("messaging is down")),
    )
    # Must return normally despite post_event_message raising.
    await rq._notify_operator(db_session, version, "release_promoted", "summary text")


# ===========================================================================
# Calcification alerting — _check_version_calcification
# ===========================================================================


@pytest.mark.asyncio
async def test_calcification_alert_fires_for_repeated_hard_blockers(db_session):
    """A version in stabilize with >= _MIN_CALCIFICATION_ATTEMPTS hard-blocker
    failures posts one Alert and one release_calcifying message."""
    from app.models import Alert as _Alert

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.qualification_attempts = rq._MIN_CALCIFICATION_ATTEMPTS
    version.last_qualification = {
        "status": "not_promoted",
        "blocker": "ci_not_green",
        "candidate_sha": "abc" * 13 + "a",
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    alerted = await rq._check_version_calcification(db_session, version)
    await db_session.commit()

    assert alerted is True
    alerts = (
        (
            await db_session.execute(
                select(_Alert).where(_Alert.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 1
    assert "calcifying" in alerts[0].title
    assert "ci_not_green" in alerts[0].title
    assert "CI is not green" in alerts[0].message

    msgs = await _messages_for(db_session, project.id, "release_calcifying")
    assert len(msgs) == 1


@pytest.mark.asyncio
async def test_calcification_alert_not_fired_for_dry_run_hold(db_session):
    """A version in the dry-run hold (passed qualifier but promotion disabled)
    must NOT trigger a calcification alert — that is healthy, not calcification."""
    from app.models import Alert as _Alert

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.qualification_attempts = rq._MIN_CALCIFICATION_ATTEMPTS + 5
    version.last_qualification = {
        "status": "dry_run",
        "would_promote": True,
        "confidence": 0.95,
        "blocker": None,
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    alerted = await rq._check_version_calcification(db_session, version)
    await db_session.commit()

    assert alerted is False
    alerts = (
        (
            await db_session.execute(
                select(_Alert).where(_Alert.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 0


@pytest.mark.asyncio
async def test_calcification_alert_deduplicated(db_session):
    """A second call with the same version+blocker is a no-op — the alert is
    posted only once per distinct stall."""
    from app.models import Alert as _Alert

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.qualification_attempts = rq._MIN_CALCIFICATION_ATTEMPTS
    version.last_qualification = {
        "status": "not_promoted",
        "blocker": "audit_missing",
        "candidate_sha": "def" * 13 + "d",
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    first = await rq._check_version_calcification(db_session, version)
    await db_session.commit()
    second = await rq._check_version_calcification(db_session, version)
    await db_session.commit()

    assert first is True
    assert second is False
    alerts = (
        (
            await db_session.execute(
                select(_Alert).where(_Alert.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 1


@pytest.mark.asyncio
async def test_calcification_alert_refires_on_blocker_change(db_session):
    """When the blocker changes (e.g. ci_not_green → audit_missing) a new alert
    is posted — the old one is for a different stall."""
    from app.models import Alert as _Alert

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.qualification_attempts = rq._MIN_CALCIFICATION_ATTEMPTS
    version.last_qualification = {
        "status": "not_promoted",
        "blocker": "ci_not_green",
        "candidate_sha": "aaa" * 13 + "b",
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    first = await rq._check_version_calcification(db_session, version)
    await db_session.commit()
    assert first is True

    version.last_qualification = {
        "status": "not_promoted",
        "blocker": "audit_missing",
        "candidate_sha": "bbb" * 13 + "b",
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    second = await rq._check_version_calcification(db_session, version)
    await db_session.commit()
    assert second is True

    alerts = (
        (
            await db_session.execute(
                select(_Alert).where(_Alert.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 2


@pytest.mark.asyncio
async def test_calcification_not_fired_below_attempt_threshold(db_session):
    """Fewer than _MIN_CALCIFICATION_ATTEMPTS hard-blocker failures do not
    trigger an alert — the stall may still be transient."""
    from app.models import Alert as _Alert

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.qualification_attempts = rq._MIN_CALCIFICATION_ATTEMPTS - 1
    version.last_qualification = {
        "status": "not_promoted",
        "blocker": "ci_not_green",
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    alerted = await rq._check_version_calcification(db_session, version)
    assert alerted is False
    alerts = (
        (
            await db_session.execute(
                select(_Alert).where(_Alert.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 0


@pytest.mark.asyncio
async def test_sweep_triggers_calcification_check(db_session):
    """The sweep runs the calcification check after finalizing stuck versions —
    a version that already has enough hard-blocker failures gets an alert."""
    from app.models import Alert as _Alert

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    # No pending qualification — the sweep won't try to finalize this version.
    # But it should still check for calcification.
    version.qualification_attempts = rq._MIN_CALCIFICATION_ATTEMPTS
    version.last_qualification = {
        "status": "not_promoted",
        "blocker": "ci_not_green",
    }
    db_session.add(version)
    await db_session.commit()

    await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )
    await db_session.commit()

    alerts = (
        (
            await db_session.execute(
                select(_Alert).where(_Alert.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 1
    assert "calcifying" in alerts[0].title


# ===========================================================================
# ci_unresolved blocker — calcification + readiness guards
# ===========================================================================


@pytest.mark.asyncio
async def test_calcification_skips_ci_unresolved_blocker(db_session):
    """ci_unresolved must NOT trigger the quality calcification alert — it is an
    infrastructure / resolution concern, not a quality regression."""
    from app.models import Alert as _Alert

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.qualification_attempts = rq._MIN_CALCIFICATION_ATTEMPTS
    version.last_qualification = {
        "status": "not_promoted",
        "blocker": "ci_unresolved",
    }
    db_session.add(version)
    await db_session.commit()

    fired = await rq._check_version_calcification(db_session, version)
    await db_session.commit()

    assert fired is False
    alerts = (
        (
            await db_session.execute(
                select(_Alert).where(_Alert.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 0


@pytest.mark.asyncio
async def test_readiness_excludes_ci_unresolved_from_recurring_blockers(db_session):
    """A window full of ci_unresolved verdicts must NOT produce a recurring-blocker
    block in the promotion-readiness summary."""
    project, _version = await _make_project_and_version(db_session)

    # Seed enough ci_unresolved verdicts to exceed the recurring-blocker threshold
    # if they were counted.
    for _ in range(rq._READINESS_RECURRING_BLOCKER_MIN + 1):
        db_session.add(
            QualificationVerdict(
                project_id=project.id,
                release_version_id=_version.id,
                candidate_sha="abc1234",
                confidence=0.0,
                sub_scores={},
                blocker="ci_unresolved",
                would_promote=False,
                promotion_enabled=False,
                terminal_status="not_promoted",
                threshold=0.9,
            )
        )
    await db_session.commit()

    result = await rq.compute_promotion_readiness(db_session, project.id)

    # ci_unresolved must not appear in blocker_counts or recurring_blockers.
    assert "ci_unresolved" not in result["blocker_counts"]
    assert "ci_unresolved" not in result["recurring_blockers"]


# ===========================================================================
# Build-phase stall alerting — _check_version_build_stall
# ===========================================================================


async def _make_merged_task(db_session, project_id, *, updated_at):
    """Create a COMPLETED + pr_merged task so count_merged_since counts it."""
    task = Task(
        project_id=project_id,
        title="merged-task",
        payloads=[{"type": "create", "prompt": "x"}],
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        updated_at=updated_at,
    )
    db_session.add(task)
    await db_session.commit()
    return task


@pytest.mark.asyncio
async def test_build_stall_alert_fires_for_old_build_version(db_session):
    """An open build-phase version older than _BUILD_STALL_AGE_DAYS posts one
    Alert and one release_build_stalled message."""
    from datetime import timedelta

    from app.models import Alert as _Alert

    project, version = await _make_project_and_version(db_session, phase="build")
    # Build phase started well beyond the healthy window; phase_changed_at is
    # None for a freshly-opened build version, so the age derives from opened_at.
    version.opened_at = rq._utcnow() - timedelta(days=rq._BUILD_STALL_AGE_DAYS + 6)
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    alerted = await rq._check_version_build_stall(db_session, version)
    await db_session.commit()

    assert alerted is True
    alerts = (
        (
            await db_session.execute(
                select(_Alert).where(_Alert.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 1
    assert "build phase" in alerts[0].title
    assert "stabilize" in alerts[0].message

    msgs = await _messages_for(db_session, project.id, "release_build_stalled")
    assert len(msgs) == 1


@pytest.mark.asyncio
async def test_build_stall_alert_fires_for_high_merged_count(db_session, mocker):
    """An open build-phase version that has accumulated merged tasks past the
    threshold fires even when the phase age is young."""
    from app.models import Alert as _Alert

    # Keep age well below threshold so only the merged-count trigger can fire,
    # and lower the merged-count threshold so the test stays fast.
    mocker.patch.object(rq, "_BUILD_STALL_AGE_DAYS", 365)
    mocker.patch.object(rq, "_BUILD_STALL_MERGED_TASKS", 2)

    project, version = await _make_project_and_version(db_session, phase="build")
    now = rq._utcnow()
    await _make_merged_task(db_session, project.id, updated_at=now)
    await _make_merged_task(db_session, project.id, updated_at=now)
    await db_session.refresh(version)

    alerted = await rq._check_version_build_stall(db_session, version)
    await db_session.commit()

    assert alerted is True
    alerts = (
        (
            await db_session.execute(
                select(_Alert).where(_Alert.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 1
    assert "build phase" in alerts[0].title


@pytest.mark.asyncio
async def test_build_stall_alert_not_fired_for_young_build_version(db_session):
    """A freshly-opened build version with no merged tasks does not alert —
    a normally-progressing build must not be nagged."""
    from app.models import Alert as _Alert

    project, version = await _make_project_and_version(db_session, phase="build")
    # opened_at defaults to now; no merged tasks.
    alerted = await rq._check_version_build_stall(db_session, version)
    assert alerted is False
    alerts = (
        (
            await db_session.execute(
                select(_Alert).where(_Alert.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 0


@pytest.mark.asyncio
async def test_build_stall_alert_not_fired_for_stabilize_version(db_session):
    """A stabilize-phase version is never a build stall — wrong phase, no-op,
    even when old."""
    from datetime import timedelta

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.opened_at = rq._utcnow() - timedelta(days=rq._BUILD_STALL_AGE_DAYS + 30)
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    alerted = await rq._check_version_build_stall(db_session, version)
    assert alerted is False


@pytest.mark.asyncio
async def test_build_stall_alert_deduplicated(db_session):
    """A second call for the same build-phase stall is a no-op — the alert is
    posted only once per distinct build phase."""
    from datetime import timedelta

    from app.models import Alert as _Alert

    project, version = await _make_project_and_version(db_session, phase="build")
    version.opened_at = rq._utcnow() - timedelta(days=rq._BUILD_STALL_AGE_DAYS + 6)
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    first = await rq._check_version_build_stall(db_session, version)
    await db_session.commit()
    second = await rq._check_version_build_stall(db_session, version)
    await db_session.commit()

    assert first is True
    assert second is False
    alerts = (
        (
            await db_session.execute(
                select(_Alert).where(_Alert.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 1


@pytest.mark.asyncio
async def test_build_stall_alert_refires_after_build_phase_restarts(db_session):
    """When the version re-enters build later (phase_changed_at re-stamped to a
    new reference), the dedup resets and a fresh stall can alert again."""
    from datetime import timedelta

    from app.models import Alert as _Alert

    project, version = await _make_project_and_version(db_session, phase="build")
    version.opened_at = rq._utcnow() - timedelta(days=rq._BUILD_STALL_AGE_DAYS + 16)
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    first = await rq._check_version_build_stall(db_session, version)
    await db_session.commit()
    assert first is True

    # Simulate a build → stabilize → build round-trip: phase_changed_at is
    # re-stamped to a newer (but still stalled) reference time.
    version.phase_changed_at = rq._utcnow() - timedelta(
        days=rq._BUILD_STALL_AGE_DAYS + 1
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    second = await rq._check_version_build_stall(db_session, version)
    await db_session.commit()
    assert second is True

    alerts = (
        (
            await db_session.execute(
                select(_Alert).where(_Alert.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 2


@pytest.mark.asyncio
async def test_sweep_triggers_build_stall_check(db_session):
    """The sweep runs the build-stall check — an open build version old enough
    to be stalled gets an alert."""
    from datetime import timedelta

    from app.models import Alert as _Alert

    project, version = await _make_project_and_version(db_session, phase="build")
    version.opened_at = rq._utcnow() - timedelta(days=rq._BUILD_STALL_AGE_DAYS + 6)
    db_session.add(version)
    await db_session.commit()

    await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )
    await db_session.commit()

    alerts = (
        (
            await db_session.execute(
                select(_Alert).where(_Alert.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 1
    assert "build phase" in alerts[0].title


@pytest.mark.asyncio
async def test_stabilize_stall_alert_fires_for_old_stabilize_version(db_session):
    """An open stabilize-phase version older than _STABILIZE_STALL_AGE_DAYS posts
    one Alert and one release_stabilize_stalled message."""
    from datetime import timedelta

    from app.models import Alert as _Alert

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    # Phase started well beyond the healthy window.
    version.phase_changed_at = rq._utcnow() - timedelta(
        days=rq._STABILIZE_STALL_AGE_DAYS + 6
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    alerted = await rq._check_version_stabilize_stall(db_session, version)
    await db_session.commit()

    assert alerted is True
    alerts = (
        (
            await db_session.execute(
                select(_Alert).where(_Alert.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 1
    assert "stabilize phase" in alerts[0].title
    assert "qualify" in alerts[0].message

    msgs = await _messages_for(db_session, project.id, "release_stabilize_stalled")
    assert len(msgs) == 1


@pytest.mark.asyncio
async def test_stabilize_stall_alert_fires_for_high_merged_count(db_session, mocker):
    """An open stabilize-phase version that has accumulated merged tasks past the
    threshold fires even when the phase age is young."""
    from app.models import Alert as _Alert

    mocker.patch.object(rq, "_STABILIZE_STALL_AGE_DAYS", 365)
    mocker.patch.object(rq, "_STABILIZE_STALL_MERGED_TASKS", 2)

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    now = rq._utcnow()
    await _make_merged_task(db_session, project.id, updated_at=now)
    await _make_merged_task(db_session, project.id, updated_at=now)
    await db_session.refresh(version)

    alerted = await rq._check_version_stabilize_stall(db_session, version)
    await db_session.commit()

    assert alerted is True
    alerts = (
        (
            await db_session.execute(
                select(_Alert).where(_Alert.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 1
    assert "stabilize phase" in alerts[0].title


@pytest.mark.asyncio
async def test_stabilize_stall_alert_not_fired_for_young_stabilize_version(db_session):
    """A freshly-entered stabilize version with no merged tasks does not alert —
    a normally-progressing stabilize must not be nagged."""
    from app.models import Alert as _Alert

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    alerted = await rq._check_version_stabilize_stall(db_session, version)
    assert alerted is False
    alerts = (
        (
            await db_session.execute(
                select(_Alert).where(_Alert.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 0


@pytest.mark.asyncio
async def test_stabilize_stall_alert_not_fired_for_build_version(db_session):
    """A build-phase version is never a stabilize stall — wrong phase, no-op."""
    from datetime import timedelta

    project, version = await _make_project_and_version(db_session, phase="build")
    version.phase_changed_at = rq._utcnow() - timedelta(
        days=rq._STABILIZE_STALL_AGE_DAYS + 30
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    alerted = await rq._check_version_stabilize_stall(db_session, version)
    assert alerted is False


@pytest.mark.asyncio
async def test_stabilize_stall_not_fired_for_dry_run_hold(db_session):
    """A version that passed qualification but is held by promotion_enabled=False
    (dry_run) must NOT trigger a stabilize-stall alert — it is healthy."""
    from datetime import timedelta

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.phase_changed_at = rq._utcnow() - timedelta(
        days=rq._STABILIZE_STALL_AGE_DAYS + 10
    )
    version.last_qualification = {"status": "dry_run", "candidate_sha": "abc123"}
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    alerted = await rq._check_version_stabilize_stall(db_session, version)
    assert alerted is False


@pytest.mark.asyncio
async def test_stabilize_stall_not_fired_when_calcification_covers_it(db_session):
    """A version with a hard-blocker not_promoted status is already covered by
    the calcification alert — stabilize-stall must not double-alert."""
    from datetime import timedelta

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.phase_changed_at = rq._utcnow() - timedelta(
        days=rq._STABILIZE_STALL_AGE_DAYS + 10
    )
    version.last_qualification = {
        "status": "not_promoted",
        "blocker": "ci_not_green",
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    alerted = await rq._check_version_stabilize_stall(db_session, version)
    assert alerted is False


@pytest.mark.asyncio
async def test_stabilize_stall_alert_deduplicated(db_session):
    """A second call for the same stabilize-phase stall is a no-op — the alert
    is posted only once per distinct stabilize phase."""
    from datetime import timedelta

    from app.models import Alert as _Alert

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.phase_changed_at = rq._utcnow() - timedelta(
        days=rq._STABILIZE_STALL_AGE_DAYS + 6
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    first = await rq._check_version_stabilize_stall(db_session, version)
    await db_session.commit()
    second = await rq._check_version_stabilize_stall(db_session, version)
    await db_session.commit()

    assert first is True
    assert second is False
    alerts = (
        (
            await db_session.execute(
                select(_Alert).where(_Alert.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 1


@pytest.mark.asyncio
async def test_stabilize_stall_alert_refires_after_stabilize_phase_restarts(db_session):
    """When the version re-enters stabilize (phase_changed_at re-stamped), the
    dedup resets and a fresh stall can alert again."""
    from datetime import timedelta

    from app.models import Alert as _Alert

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.phase_changed_at = rq._utcnow() - timedelta(
        days=rq._STABILIZE_STALL_AGE_DAYS + 16
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    first = await rq._check_version_stabilize_stall(db_session, version)
    await db_session.commit()
    assert first is True

    # Simulate a stabilize → qualify → stabilize round-trip: phase_changed_at
    # is re-stamped to a newer (but still stalled) reference time.
    version.phase_changed_at = rq._utcnow() - timedelta(
        days=rq._STABILIZE_STALL_AGE_DAYS + 1
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    second = await rq._check_version_stabilize_stall(db_session, version)
    await db_session.commit()
    assert second is True

    alerts = (
        (
            await db_session.execute(
                select(_Alert).where(_Alert.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 2


@pytest.mark.asyncio
async def test_sweep_triggers_stabilize_stall_check(db_session):
    """The sweep runs the stabilize-stall check — an open stabilize version old
    enough to be stalled gets an alert."""
    from datetime import timedelta

    from app.models import Alert as _Alert

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.phase_changed_at = rq._utcnow() - timedelta(
        days=rq._STABILIZE_STALL_AGE_DAYS + 6
    )
    db_session.add(version)
    await db_session.commit()

    await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )
    await db_session.commit()

    alerts = (
        (
            await db_session.execute(
                select(_Alert).where(_Alert.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 1
    assert "stabilize phase" in alerts[0].title


class TestFormatQualificationForApi:
    """format_qualification_for_api returns a normalized, operator-facing dict."""

    def test_promoted_status_has_correct_outcome(self):
        qual = {"status": "promoted", "confidence": 0.95, "threshold": 0.90}
        out = rq.format_qualification_for_api(qual)
        assert out["status"] == "promoted"
        assert out["outcome"] == "Promoted"

    def test_dry_run_outcome_label(self):
        qual = {"status": "dry_run", "confidence": 0.92}
        out = rq.format_qualification_for_api(qual)
        assert out["outcome"] == "Dry-run (would promote)"

    def test_not_promoted_outcome_label(self):
        qual = {"status": "not_promoted", "confidence": 0.75}
        out = rq.format_qualification_for_api(qual)
        assert out["outcome"] == "Below threshold"

    def test_pending_outcome_label(self):
        out = rq.format_qualification_for_api({"status": "pending"})
        assert out["outcome"] == "In progress"

    def test_error_outcome_label(self):
        out = rq.format_qualification_for_api({"status": "error"})
        assert out["outcome"] == "Error"

    def test_unknown_status_falls_back_to_status_string(self):
        out = rq.format_qualification_for_api({"status": "unexpected_value"})
        assert out["outcome"] == "unexpected_value"

    def test_empty_dict_returns_none_fields(self):
        out = rq.format_qualification_for_api({})
        assert out["status"] is None
        assert out["outcome"] is None
        assert out["confidence"] is None
        assert out["threshold"] is None
        assert out["sub_scores"] is None
        assert out["blocker"] is None
        assert out["started_at"] is None
        assert out["finalized_at"] is None
        assert out["migration_safety"] is None
        assert out["audit_findings"] is None
        assert out["promotion_conflict"] is None

    def test_all_fields_passed_through(self):
        qual = {
            "status": "not_promoted",
            "confidence": 0.82,
            "threshold": 0.90,
            "sub_scores": {"audit": 0.85, "test_erosion": 0.80, "smoketest": 0.78},
            "blocker": "ci_not_green",
            "started_at": "2026-05-21T10:00:00",
            "finalized_at": "2026-05-21T10:30:00",
            "migration_safety": "block",
            "migration_safety_detail": "table rename pending",
            "migration_apply_safety": "warn",
            "migration_apply_safety_detail": "apply path has drift",
            "audit_findings": [{"severity": "high", "summary": "stale report"}],
            "excluded_audit_findings": [{"severity": "low", "summary": "ignored"}],
            "test_erosion_detail": {"deleted_test_files": 1},
            "smoketest_detail": {"passed": False},
            "audit_reconciliation": {"excluded_findings_count": 1},
            "promotion_pr_url": "https://example.com/pr/123",
            "promotion_conflict": True,
            "promotion_error_detail": "merge conflict",
        }
        out = rq.format_qualification_for_api(qual)
        assert out["confidence"] == 0.82
        assert out["threshold"] == 0.90
        assert out["sub_scores"] == qual["sub_scores"]
        assert out["blocker"] == "ci_not_green"
        assert out["started_at"] == "2026-05-21T10:00:00"
        assert out["finalized_at"] == "2026-05-21T10:30:00"
        assert out["migration_safety"] == "block"
        assert out["migration_safety_detail"] == "table rename pending"
        assert out["migration_apply_safety"] == "warn"
        assert out["migration_apply_safety_detail"] == "apply path has drift"
        assert out["audit_findings"] == qual["audit_findings"]
        assert out["excluded_audit_findings"] == qual["excluded_audit_findings"]
        assert out["test_erosion_detail"] == {"deleted_test_files": 1}
        assert out["smoketest_detail"] == {"passed": False}
        assert out["audit_reconciliation"] == {"excluded_findings_count": 1}
        assert out["promotion_pr_url"] == "https://example.com/pr/123"
        assert out["promotion_conflict"] is True
        assert out["promotion_error_detail"] == "merge conflict"


# ===========================================================================
# QualificationVerdict recording — the durable per-verdict history (Phase 4)
# ===========================================================================


async def _ensure_verdict_table(db_session):
    """Create the ``qualificationverdict`` table on the test DB if absent.

    The Alembic migration for this table is authored by a sibling task and is
    not yet applied, so on the Postgres CI engine (whose schema is built from
    migrations) the table does not exist.  Create it idempotently so the
    verdict-history and readiness tests have a real table to exercise; on SQLite
    the per-test ``create_all`` already built it and ``checkfirst`` makes this a
    no-op there (and once the real migration lands).
    """
    from app.models import QualificationVerdict as _QV

    conn = await db_session.connection()
    await conn.run_sync(lambda c: _QV.__table__.create(c, checkfirst=True))
    await db_session.commit()


async def _verdict_rows(db_session, project_id):
    """Return all QualificationVerdict rows for a project, oldest-first."""
    rows = (
        (
            await db_session.execute(
                select(QualificationVerdict)
                .where(QualificationVerdict.project_id == project_id)
                .order_by(QualificationVerdict.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


@pytest.mark.asyncio
async def test_finalize_dry_run_records_verdict_row(db_session, mocker):
    """A dry-run finalize appends ONE immutable verdict-history row capturing
    the would-promote outcome, confidence, sub-scores and the promotion_enabled
    state at the time — additive history alongside last_qualification."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = False

    project, version = await _make_project_and_version(db_session)
    await _ensure_verdict_table(db_session)
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

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    assert result["status"] == "dry_run"

    rows = await _verdict_rows(db_session, project.id)
    assert len(rows) == 1
    row = rows[0]
    assert row.terminal_status == "dry_run"
    assert row.would_promote is True
    assert row.promotion_enabled is False
    assert row.release_version_id == version.id
    assert row.confidence == pytest.approx(1.0)
    assert row.blocker is None
    assert row.sub_scores is not None and "audit" in row.sub_scores


@pytest.mark.asyncio
async def test_finalize_not_promoted_records_blocker_verdict_row(db_session):
    """A blocked (not-promoted) finalize records a would_promote=False verdict
    row carrying the hard-blocker code."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = False

    project, version = await _make_project_and_version(db_session)
    await _ensure_verdict_table(db_session)
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

    rows = await _verdict_rows(db_session, project.id)
    assert len(rows) == 1
    row = rows[0]
    assert row.terminal_status == "not_promoted"
    assert row.would_promote is False
    assert row.blocker == "test_file_deleted"


@pytest.mark.asyncio
async def test_verdict_recording_failure_does_not_propagate(db_session, mocker):
    """Recording is best-effort: a failure constructing/staging the verdict row
    must NEVER break or roll back the qualification — the dry-run still finalizes
    and last_qualification is written, just with no history row."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = False

    project, version = await _make_project_and_version(db_session)
    await _ensure_verdict_table(db_session)
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

    # Make the verdict-row construction blow up — recording must swallow it.
    mocker.patch(
        "app.release_promotion.QualificationVerdict",
        side_effect=RuntimeError("boom — cannot build verdict row"),
    )

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    # The qualification itself is unaffected: it still reports dry_run and
    # last_qualification persists.
    assert result["status"] == "dry_run"
    await db_session.refresh(version)
    assert version.last_qualification["status"] == "dry_run"
    assert version.status == "open"

    # ...but no history row was written (recording failed and was swallowed).
    rows = await _verdict_rows(db_session, project.id)
    assert rows == []


@pytest.mark.asyncio
async def test_finalize_records_ci_resolution_provenance_in_verdict(db_session, mocker):
    """CI resolution provenance (state, resolved_sha, detail) is persisted in
    the QualificationVerdict sub_scores as ci_resolution for both green and
    non-green paths."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = False

    project, version = await _make_project_and_version(db_session)
    await _ensure_verdict_table(db_session)
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

    # Mock CI resolution to return explicit provenance: green via PR head.
    async def mock_resolve_ci(*args, **kwargs):
        return (
            True,
            {
                "state": rq._CI_GREEN,
                "resolved_sha": "0123456789abcdef0123456789abcdef01234567",
                "detail": "PR head check-runs passed",
            },
        )

    mocker.patch.object(rq, "_resolve_ci_at_finalize", side_effect=mock_resolve_ci)

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    assert result["status"] == "dry_run"

    rows = await _verdict_rows(db_session, project.id)
    assert len(rows) == 1
    row = rows[0]
    assert row.sub_scores is not None
    assert "ci_resolution" in row.sub_scores
    ci_res = row.sub_scores["ci_resolution"]
    assert ci_res["state"] == rq._CI_GREEN
    assert ci_res["resolved_sha"] == "0123456789abcdef0123456789abcdef01234567"
    assert ci_res["detail"] == "PR head check-runs passed"


@pytest.mark.asyncio
async def test_finalize_records_ci_resolution_on_not_promoted(db_session, mocker):
    """CI resolution provenance is also recorded when a verdict is not promoted,
    including degenerate states like pending."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = False

    project, version = await _make_project_and_version(db_session)
    await _ensure_verdict_table(db_session)
    await _seed_qualification_children(
        db_session,
        project,
        version,
        audit_logs={
            "audit_score": 0.5,
            "test_erosion": {
                "deleted_test_files": [],
                "assertion_delta": 0,
                "test_count_delta": 0,
            },
        },
        smoke_logs={
            "passed": True,
            "tests": [{"name": "a", "status": "pass"}],
            "suites": {"startup": {"passed": True}},
        },
    )

    # Mock CI resolution to return pending state (no checks found).
    async def mock_resolve_ci(*args, **kwargs):
        return (
            False,
            {
                "state": rq._CI_PENDING,
                "resolved_sha": None,
                "detail": "no check-runs on candidate and no PR head with CI",
            },
        )

    mocker.patch.object(rq, "_resolve_ci_at_finalize", side_effect=mock_resolve_ci)

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    assert result["status"] == "not_promoted"

    rows = await _verdict_rows(db_session, project.id)
    assert len(rows) == 1
    row = rows[0]
    assert row.sub_scores is not None
    assert "ci_resolution" in row.sub_scores
    ci_res = row.sub_scores["ci_resolution"]
    assert ci_res["state"] == rq._CI_PENDING
    assert ci_res["resolved_sha"] is None
    assert ci_res["detail"] == "no check-runs on candidate and no PR head with CI"


# ===========================================================================
# compute_promotion_readiness — the calibration aggregation (Phase 4)
# ===========================================================================


async def _seed_verdict(
    db_session,
    project_id,
    version_id,
    *,
    seq: int,
    would_promote: bool,
    blocker: str | None = None,
    confidence: float = 0.95,
    promotion_enabled: bool = False,
):
    """Insert ONE QualificationVerdict row with a deterministic created_at.

    ``seq`` orders rows in time (higher seq = more recent) so the newest-first
    consecutive-clean-streak walk in compute_promotion_readiness is
    deterministic regardless of insert timing.
    """
    from datetime import timedelta

    from app.time_utils import _utcnow

    row = QualificationVerdict(
        project_id=project_id,
        release_version_id=version_id,
        candidate_sha="ab" * 20,
        confidence=confidence,
        sub_scores={"audit": 1.0, "test_erosion": 1.0, "smoketest": 0.9},
        blocker=blocker,
        would_promote=would_promote,
        promotion_enabled=promotion_enabled,
        terminal_status="dry_run" if would_promote else "not_promoted",
        threshold=0.9,
        created_at=_utcnow() + timedelta(minutes=seq),
    )
    db_session.add(row)
    await db_session.commit()
    return row


@pytest.mark.asyncio
async def test_readiness_empty_history_not_ready(db_session):
    """No verdict history → not ready, with an explanatory reason."""
    project, version = await _make_project_and_version(db_session)
    await _ensure_verdict_table(db_session)

    out = await rq.compute_promotion_readiness(db_session, project.id)

    assert out["ready"] is False
    assert out["verdicts_evaluated"] == 0
    assert "nothing to evaluate" in out["reason"]
    assert out["confidence"]["mean"] is None


@pytest.mark.asyncio
async def test_readiness_insufficient_dry_runs_not_ready(db_session):
    """Fewer than the minimum evaluated verdicts → not ready even when every
    one would have promoted."""
    project, version = await _make_project_and_version(db_session)
    await _ensure_verdict_table(db_session)
    # Two clean would-promote verdicts — below _READINESS_MIN_VERDICTS (5).
    for i in range(2):
        await _seed_verdict(
            db_session, project.id, version.id, seq=i, would_promote=True
        )

    out = await rq.compute_promotion_readiness(db_session, project.id)

    assert out["ready"] is False
    assert out["verdicts_evaluated"] == 2
    assert out["consecutive_clean_passes"] == 2
    assert "dry-run verdict" in out["reason"]


@pytest.mark.asyncio
async def test_readiness_healthy_streak_ready(db_session):
    """A long consecutive-clean streak of high-confidence would-promote verdicts
    above all thresholds → ready."""
    project, version = await _make_project_and_version(db_session)
    await _ensure_verdict_table(db_session)
    # Six clean, high-confidence, low-variance would-promote verdicts.
    confidences = [0.95, 0.96, 0.94, 0.97, 0.95, 0.96]
    for i, conf in enumerate(confidences):
        await _seed_verdict(
            db_session,
            project.id,
            version.id,
            seq=i,
            would_promote=True,
            confidence=conf,
        )

    out = await rq.compute_promotion_readiness(db_session, project.id)

    assert out["ready"] is True, out["reason"]
    assert out["verdicts_evaluated"] == 6
    assert out["would_promote_count"] == 6
    assert out["would_promote_rate"] == pytest.approx(1.0)
    assert out["consecutive_clean_passes"] == 6
    assert out["recurring_blockers"] == {}
    assert out["confidence"]["stable"] is True


@pytest.mark.asyncio
async def test_readiness_is_framed_as_self_approval_not_trustworthy(db_session):
    """The ready signal must NOT claim the qualifier is 'trustworthy' — there is
    no recorded post-promotion ground truth, so it is a self-approval rate only.
    The verdict reason and the structured caveat fields must say so."""
    project, version = await _make_project_and_version(db_session)
    await _ensure_verdict_table(db_session)
    for i in range(6):
        await _seed_verdict(
            db_session, project.id, version.id, seq=i, would_promote=True, confidence=0.95
        )

    out = await rq.compute_promotion_readiness(db_session, project.id)

    assert out["ready"] is True, out["reason"]
    # No unvalidated "trustworthy" claim; framed honestly as self-approval.
    assert "trustworthy" not in out["reason"].lower()
    assert "self-approval" in out["reason"].lower()
    # Structured caveat fields are present so every consumer states it honestly.
    assert out["ground_truth_validated"] is False
    assert "self-approval only" in out["caveat"]


@pytest.mark.asyncio
async def test_readiness_empty_carries_ground_truth_caveat(db_session):
    """Even the no-verdicts (not-ready) summary carries the ground-truth caveat
    fields so the UI/strategic payloads never imply a validated signal."""
    project, _version = await _make_project_and_version(db_session)
    await _ensure_verdict_table(db_session)

    out = await rq.compute_promotion_readiness(db_session, project.id)

    assert out["verdicts_evaluated"] == 0
    assert out["ground_truth_validated"] is False
    assert "self-approval only" in out["caveat"]


async def _seed_promoted_verdict(
    db_session,
    project_id,
    version_id,
    *,
    seq: int,
    post_promotion_outcome: str | None,
):
    """Insert ONE promoted QualificationVerdict with a resolved (or NULL/unconfirmed)
    post-promotion serving outcome, for ground-truth precision tests."""
    from datetime import timedelta

    from app.time_utils import _utcnow

    row = QualificationVerdict(
        project_id=project_id,
        release_version_id=version_id,
        candidate_sha="cd" * 20,
        confidence=0.95,
        sub_scores={"audit": 1.0},
        blocker=None,
        would_promote=True,
        promotion_enabled=True,
        terminal_status="promoted",
        threshold=0.9,
        post_promotion_outcome=post_promotion_outcome,
        created_at=_utcnow() + timedelta(minutes=seq),
    )
    db_session.add(row)
    await db_session.commit()
    return row


@pytest.mark.asyncio
async def test_validated_precision_none_when_no_resolved_outcomes(db_session):
    """Only dry-run verdicts (no resolved promoted outcomes) → validated_precision
    null, sample 0, ground_truth_validated False; self-approval framing intact."""
    project, version = await _make_project_and_version(db_session)
    await _ensure_verdict_table(db_session)
    for i in range(6):
        await _seed_verdict(
            db_session, project.id, version.id, seq=i, would_promote=True
        )

    out = await rq.compute_promotion_readiness(db_session, project.id)

    assert out["validated_precision"] is None
    assert out["resolved_outcome_sample"] == 0
    assert out["serving_confirmed_count"] == 0
    assert out["deploy_failed_count"] == 0
    assert out["ground_truth_validated"] is False
    assert "self-approval only" in out["caveat"]
    # Self-approval rate is still reported independently.
    assert out["would_promote_rate"] == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_validated_precision_from_resolved_outcomes(db_session):
    """A mixed serving_confirmed/deploy_failed sample at/above the minimum computes
    the correct precision and flips ground_truth_validated True; unconfirmed/NULL
    rows are ignored. The self-approval rate is still reported independently."""
    project, version = await _make_project_and_version(db_session)
    await _ensure_verdict_table(db_session)
    # Self-approval window (dry-run regime) — separate signal.
    for i in range(6):
        await _seed_verdict(
            db_session, project.id, version.id, seq=i, would_promote=True
        )
    # Resolved promoted outcomes: 4 serving_confirmed, 1 deploy_failed → 0.80.
    outcomes = (
        ["serving_confirmed"] * 4
        + ["deploy_failed"]
        + ["unconfirmed", None]  # ignored
    )
    for j, outcome in enumerate(outcomes):
        await _seed_promoted_verdict(
            db_session, project.id, version.id, seq=100 + j,
            post_promotion_outcome=outcome,
        )

    out = await rq.compute_promotion_readiness(db_session, project.id)

    assert out["serving_confirmed_count"] == 4
    assert out["deploy_failed_count"] == 1
    assert out["resolved_outcome_sample"] == 5
    assert out["validated_precision"] == pytest.approx(0.8)
    assert out["ground_truth_validated"] is True
    assert "validated precision" in out["caveat"]
    # The self-approval rate is augmented, never replaced.
    assert out["would_promote_rate"] == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_validated_precision_below_min_keeps_unvalidated(db_session):
    """A resolved sample below the minimum reports the precision but keeps
    ground_truth_validated False and the self-approval-only caveat."""
    project, version = await _make_project_and_version(db_session)
    await _ensure_verdict_table(db_session)
    # Only 2 resolved outcomes — below _READINESS_MIN_RESOLVED_OUTCOMES (5).
    await _seed_promoted_verdict(
        db_session, project.id, version.id, seq=100,
        post_promotion_outcome="serving_confirmed",
    )
    await _seed_promoted_verdict(
        db_session, project.id, version.id, seq=101,
        post_promotion_outcome="deploy_failed",
    )

    out = await rq.compute_promotion_readiness(db_session, project.id)

    assert out["resolved_outcome_sample"] == 2
    assert out["validated_precision"] == pytest.approx(0.5)
    assert out["ground_truth_validated"] is False
    assert "self-approval only" in out["caveat"]


@pytest.mark.asyncio
async def test_validated_precision_self_corrects_on_deploy_failed_recovery(db_session):
    """(c) When a deploy_failed verdict is later recovered (its
    post_promotion_outcome flips deploy_failed -> serving_confirmed because the
    promoted SHA finally served), the readiness validated_precision sample
    self-corrects on the next compute — the false-negative deploy_failed no longer
    depresses precision. release_readiness needs no logic change: it derives the
    precision directly from QualificationVerdict.post_promotion_outcome."""
    project, version = await _make_project_and_version(db_session)
    await _ensure_verdict_table(db_session)
    # 4 serving_confirmed + 1 deploy_failed → precision 0.80 over 5 resolved.
    confirmed_rows = [
        await _seed_promoted_verdict(
            db_session, project.id, version.id, seq=100 + i,
            post_promotion_outcome="serving_confirmed",
        )
        for i in range(4)
    ]
    failed_row = await _seed_promoted_verdict(
        db_session, project.id, version.id, seq=200,
        post_promotion_outcome="deploy_failed",
    )

    before = await rq.compute_promotion_readiness(db_session, project.id)
    assert before["serving_confirmed_count"] == 4
    assert before["deploy_failed_count"] == 1
    assert before["validated_precision"] == pytest.approx(0.8)

    # Recovery flips the verdict outcome exactly as
    # _recover_serving_confirmed does (deploy_failed -> serving_confirmed).
    failed_row.post_promotion_outcome = "serving_confirmed"
    db_session.add(failed_row)
    await db_session.commit()

    after = await rq.compute_promotion_readiness(db_session, project.id)
    assert after["serving_confirmed_count"] == 5
    assert after["deploy_failed_count"] == 0
    assert after["validated_precision"] == pytest.approx(1.0)
    # The false-negative no longer depresses the precision sample.
    assert after["validated_precision"] > before["validated_precision"]
    assert len(confirmed_rows) == 4


@pytest.mark.asyncio
async def test_readiness_recurring_blocker_not_ready(db_session):
    """A recurring hard blocker in the history blocks readiness even when the
    most-recent streak, evaluated count and would-promote rate all clear their
    thresholds."""
    project, version = await _make_project_and_version(db_session)
    await _ensure_verdict_table(db_session)
    # Two older not-promoted verdicts on the SAME blocker (recurring), then a
    # run of eight recent clean would-promote passes.  evaluated=10,
    # streak=8, rate=0.80 (boundary, passes) — only the recurring blocker fails.
    await _seed_verdict(
        db_session,
        project.id,
        version.id,
        seq=0,
        would_promote=False,
        blocker="audit_missing",
        confidence=0.4,
    )
    await _seed_verdict(
        db_session,
        project.id,
        version.id,
        seq=1,
        would_promote=False,
        blocker="audit_missing",
        confidence=0.4,
    )
    for i in range(8):
        await _seed_verdict(
            db_session,
            project.id,
            version.id,
            seq=2 + i,
            would_promote=True,
            confidence=0.95,
        )

    out = await rq.compute_promotion_readiness(db_session, project.id)

    assert out["ready"] is False
    assert out["verdicts_evaluated"] == 10
    assert out["consecutive_clean_passes"] == 8
    assert out["would_promote_rate"] == pytest.approx(0.8)
    assert out["recurring_blockers"] == {"audit_missing": 2}
    assert "recurring hard blocker" in out["reason"]


@pytest.mark.asyncio
async def test_readiness_excludes_promotion_enabled_verdicts(db_session):
    """Verdicts recorded after promotion was enabled are post-decision and must
    NOT count toward the dry-run readiness evidence."""
    project, version = await _make_project_and_version(db_session)
    await _ensure_verdict_table(db_session)
    # Five clean dry-run-regime verdicts (would qualify as ready)...
    for i in range(5):
        await _seed_verdict(
            db_session, project.id, version.id, seq=i, would_promote=True
        )
    # ...plus a promotion-enabled verdict that must be ignored.
    await _seed_verdict(
        db_session,
        project.id,
        version.id,
        seq=99,
        would_promote=True,
        promotion_enabled=True,
    )

    out = await rq.compute_promotion_readiness(db_session, project.id)

    assert out["verdicts_evaluated"] == 5
    assert out["ready"] is True, out["reason"]
    # Pre-go-live (promotion disabled): this IS current readiness.
    assert out["regime"] == "pre_go_live_calibration"
    assert out["is_current_readiness"] is True


@pytest.mark.asyncio
async def test_readiness_post_go_live_is_historical_not_current(db_session):
    """Once promotion is effectively enabled, the dry-run window stops receiving
    verdicts and is frozen pre-go-live history. The summary must relabel itself as
    a historical calibration window (regime='live', is_current_readiness=False)
    and NOT present a stale 'ready' streak as current readiness — otherwise the
    panel freezes on aging dry-runs while claiming to be current."""
    project, version = await _make_project_and_version(db_session)
    await _ensure_verdict_table(db_session)
    # A healthy dry-run streak that WOULD read as ready pre-go-live...
    for i in range(6):
        await _seed_verdict(
            db_session, project.id, version.id, seq=i, would_promote=True, confidence=0.95
        )

    # ...but promotion is now live for this project.
    out = await rq.compute_promotion_readiness(
        db_session, project.id, promotion_enabled=True
    )

    assert out["regime"] == "live"
    assert out["is_current_readiness"] is False
    assert out["promotion_enabled"] is True
    # The stale dry-run streak must NOT surface as a live 'ready' signal.
    assert out["ready"] is False
    assert "pre-go-live" in out["reason"]
    # The underlying dry-run evidence is still carried (now labeled historical).
    assert out["verdicts_evaluated"] == 6


@pytest.mark.asyncio
async def test_readiness_post_go_live_empty_window_is_historical(db_session):
    """The 'collapsed to no verdicts' post-go-live state is also relabeled as
    historical rather than presenting a bare not-ready current-readiness number."""
    project, _version = await _make_project_and_version(db_session)
    await _ensure_verdict_table(db_session)

    out = await rq.compute_promotion_readiness(
        db_session, project.id, promotion_enabled=True
    )

    assert out["verdicts_evaluated"] == 0
    assert out["regime"] == "live"
    assert out["is_current_readiness"] is False
    assert out["ready"] is False


@pytest.mark.asyncio
async def test_readiness_resolves_promotion_enabled_from_runtime_config(db_session):
    """When promotion_enabled is not passed, the regime is resolved from the
    effective per-project promotion gate (so the panel endpoint, which omits the
    arg, gets the live relabeling automatically once promotion is enabled)."""
    from app import shared_state

    project, version = await _make_project_and_version(db_session)
    await _ensure_verdict_table(db_session)
    for i in range(6):
        await _seed_verdict(
            db_session, project.id, version.id, seq=i, would_promote=True, confidence=0.95
        )

    shared_state.runtime_config["promotion_enabled"] = True
    try:
        out = await rq.compute_promotion_readiness(db_session, project.id)
    finally:
        shared_state.runtime_config["promotion_enabled"] = False

    assert out["regime"] == "live"
    assert out["is_current_readiness"] is False
    assert out["ready"] is False


@pytest.mark.asyncio
async def test_readiness_surfaces_infra_failure_count_and_reason(db_session):
    """When the only recent qualification is an infra_failure, the readiness
    summary surfaces infra_failure_count >= 1, consecutive_infra_errors >= 1,
    and a reason that distinguishes 'broken pipeline' from 'never ran'.

    This is the anti-silent-collapse guard: operators must be able to tell the
    difference between 'qualifier has not run' and 'qualifier is thrashing on
    infrastructure'."""
    project, version = await _make_project_and_version(db_session)
    await _ensure_verdict_table(db_session)

    # Stamp the version's last_qualification as an infra_failure with a streak.
    from sqlalchemy.orm.attributes import flag_modified

    version.last_qualification = {
        "status": "infra_failure",
        "consecutive_infra_errors": 3,
        "reason": "audit_report_missing",
    }
    flag_modified(version, "last_qualification")
    db_session.add(version)
    await db_session.commit()

    out = await rq.compute_promotion_readiness(db_session, project.id)

    # No QualificationVerdict rows were written — infra failures don't write them.
    assert out["verdicts_evaluated"] == 0
    assert out["infra_failure_count"] == 1
    assert out["consecutive_infra_errors"] == 3
    assert "infrastructure failures" in out["reason"]
    assert out["ready"] is False


@pytest.mark.asyncio
async def test_readiness_consecutive_infra_errors_from_pending_prior_streak(db_session):
    """When the latest last_qualification is 'pending' (a new run just started
    after a streak of infra failures), consecutive_infra_errors is sourced from
    prior_infra_streak on the pending record rather than from the previous infra
    failure record."""
    project, version = await _make_project_and_version(db_session)
    await _ensure_verdict_table(db_session)

    from sqlalchemy.orm.attributes import flag_modified

    version.last_qualification = {
        "status": "pending",
        "prior_infra_streak": 2,
    }
    flag_modified(version, "last_qualification")
    db_session.add(version)
    await db_session.commit()

    out = await rq.compute_promotion_readiness(db_session, project.id)

    assert out["consecutive_infra_errors"] == 2
    # infra_failure_count counts only infra_failure status rows, not pending.
    assert out["infra_failure_count"] == 0


# ===========================================================================
# _check_version_promotion_readiness — once-per-transition notification
# ===========================================================================


@pytest.mark.asyncio
async def test_check_promotion_readiness_skips_non_open_version(db_session):
    """A promoted (or otherwise closed) version is skipped immediately — no
    message posted, no state change."""
    project, version = await _make_project_and_version(db_session)
    version.status = "promoted"
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    result = await rq._check_version_promotion_readiness(db_session, version)

    assert result is False
    assert version.promotion_ready_notified is False


@pytest.mark.asyncio
async def test_check_promotion_readiness_skips_when_promotion_already_enabled(
    db_session,
):
    """When promotion_enabled is True, the operator has already acted — no
    notification is needed and the function returns False."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = True

    project, version = await _make_project_and_version(db_session)

    result = await rq._check_version_promotion_readiness(db_session, version)

    assert result is False
    assert version.promotion_ready_notified is False


@pytest.mark.asyncio
async def test_check_promotion_readiness_fires_on_first_ready_transition(db_session):
    """On the first not-ready→ready transition the function posts a
    promotion_readiness_reached message, sets promotion_ready_notified=True,
    and returns True."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = False

    project, version = await _make_project_and_version(db_session)
    await _ensure_verdict_table(db_session)
    for i in range(5):
        await _seed_verdict(
            db_session, project.id, version.id, seq=i, would_promote=True
        )
    assert version.promotion_ready_notified is False

    result = await rq._check_version_promotion_readiness(db_session, version)
    await db_session.commit()

    assert result is True
    await db_session.refresh(version)
    assert version.promotion_ready_notified is True

    msgs = await _messages_for(
        db_session, project.id, event_type="promotion_readiness_reached"
    )
    assert len(msgs) == 1
    assert (msgs[0].context or {}).get("version_number") == version.version_number


@pytest.mark.asyncio
async def test_check_promotion_readiness_deduplicates_within_ready_window(db_session):
    """Once promotion_ready_notified is True, a second call while readiness still
    holds returns False without posting a duplicate message."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = False

    project, version = await _make_project_and_version(db_session)
    await _ensure_verdict_table(db_session)
    for i in range(5):
        await _seed_verdict(
            db_session, project.id, version.id, seq=i, would_promote=True
        )

    version.promotion_ready_notified = True
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    result = await rq._check_version_promotion_readiness(db_session, version)

    assert result is False
    msgs = await _messages_for(
        db_session, project.id, event_type="promotion_readiness_reached"
    )
    assert len(msgs) == 0


@pytest.mark.asyncio
async def test_check_promotion_readiness_resets_dedup_flag_on_lapse(db_session):
    """When readiness lapses (no verdicts → not-ready) and promotion_ready_notified
    was True, the flag is reset to False so the next ready transition can notify."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = False

    project, version = await _make_project_and_version(db_session)
    await _ensure_verdict_table(db_session)
    # No verdicts → not ready.
    version.promotion_ready_notified = True
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    # The reset branch returns True so the sweep — which commits only on a True
    # return — persists the reset. Mirror that caller contract here: commit only
    # because the function signalled it changed state.
    result = await rq._check_version_promotion_readiness(db_session, version)

    assert result is True
    await db_session.commit()
    await db_session.refresh(version)
    assert version.promotion_ready_notified is False


@pytest.mark.asyncio
async def test_check_promotion_readiness_messaging_failure_is_nonfatal(
    db_session, mocker
):
    """A failure in post_version_promotion_readiness_message is swallowed — the
    function returns False without raising and leaves promotion_ready_notified
    unset so a later cycle can retry."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = False

    project, version = await _make_project_and_version(db_session)
    await _ensure_verdict_table(db_session)
    for i in range(5):
        await _seed_verdict(
            db_session, project.id, version.id, seq=i, would_promote=True
        )

    mocker.patch(
        "app.release_qualifier.post_version_promotion_readiness_message",
        new=AsyncMock(side_effect=RuntimeError("messaging down")),
    )

    result = await rq._check_version_promotion_readiness(db_session, version)

    assert result is False
    assert version.promotion_ready_notified is False


# ===========================================================================
# Connected promotion lifecycle integration tests
# ===========================================================================
# The unit tests above verify individual helpers in isolation.  These two
# integration tests drive the FULL connected path — _finalize_qualification
# → promote → v(N+1) opens — so that regressions in the wiring (not just the
# helpers) are caught together.


@pytest.mark.asyncio
async def test_connected_success_promotion_lifecycle_includes_goal_snapshot(
    db_session, mocker
):
    """Success branch: _finalize_qualification with a confident composite
    promotes, records v1 as promoted, opens v2 with base_sha = the real merge
    commit AND goal snapshotted from the project's current_focus.

    The goal regression (goal=None on every promotion — a NameError in
    _record_promotion_success that was always swallowed) is only caught in the
    CONNECTED flow because the isolated promote() unit test passes the project
    in directly, bypassing the load path that previously referenced an undefined
    variable.  This test exercises the connected chain.
    """
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = True

    project, version = await _make_project_and_version(db_session)

    focus = "Stabilise the qualifier loop and ship the promotion gate."
    project.problem_model = {"intent": {"current_focus": focus}}
    db_session.add(project)
    await db_session.commit()

    await _seed_qualification_children(
        db_session,
        project,
        version,
        audit_logs={
            "audit_score": 1.0,
            "test_erosion": {
                "deleted_test_files": [],
                "assertion_delta": 8,
                "test_count_delta": 3,
            },
        },
        smoke_logs={
            "passed": True,
            "tests": [{"name": "smoke-a", "status": "pass"}],
            "suites": {"startup": {"passed": True}},
        },
    )

    merge_sha = "merged01" * 5
    mocker.patch("app.github.get_branch_sha", new=_branch_sha_mock(main=merge_sha))
    _patch_promote_github_defaults(mocker)
    mocker.patch("app.github.create_branch", new=_create_branch_mock())
    mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"number": 301, "html_url": "http://pr/301"}),
    )
    mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(return_value={"merged": True, "message": "Merged"}),
    )
    mocker.patch(
        "app.github.create_tag",
        new=AsyncMock(return_value={"ref": "refs/tags/v1"}),
    )

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    # The connected finalize → promote chain produced a promoted verdict.
    assert result["status"] == "promoted"
    assert result["promoted_sha"] == merge_sha
    assert result["next_version_number"] == 2

    # v1 is closed as promoted.
    await db_session.refresh(version)
    assert version.status == "promoted"
    assert version.promoted_sha == merge_sha

    # v2 was opened: base_sha = the REAL merge commit AND goal = current_focus.
    next_version = await rv.current_open_version(db_session, project.id)
    assert next_version is not None
    assert next_version.version_number == 2
    assert next_version.phase == "build"
    assert next_version.base_sha == merge_sha
    assert next_version.goal == focus


@pytest.mark.asyncio
async def test_connected_conflict_promotion_lifecycle(db_session, mocker):
    """Conflict branch: _finalize_qualification with a confident composite but a
    conflicting GitHub merge records promotion_failed with conflict=True, keeps
    v1 open in stabilize, emits the release_promotion_failed operator message,
    and triggers the post-conflict main->automain reconciliation via
    _maybe_reconcile_after_promotion_conflict.

    This is the operator's ONLY proactive signal that promotion is stuck on a
    conflict — and the reconciliation is the self-healing trigger.  Neither was
    covered by any previous test in the connected form.
    """
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
                "assertion_delta": 4,
                "test_count_delta": 1,
            },
        },
        smoke_logs={
            "passed": True,
            "tests": [{"name": "smoke-a", "status": "pass"}],
            "suites": {"startup": {"passed": True}},
        },
    )

    mocker.patch("app.github.get_branch_sha", new=_branch_sha_mock())
    _patch_promote_github_defaults(mocker)
    mocker.patch("app.github.create_branch", new=_create_branch_mock())
    mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"number": 302, "html_url": "http://pr/302"}),
    )
    # GitHub returns a merge conflict — the promotion cannot land.
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
    recon_mock = mocker.patch(
        "app.hotfix_backmerge.reconcile_main_into_automain",
        new=AsyncMock(return_value={"status": "merged"}),
    )

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    # The finalize chain saw a conflict → promotion_failed with conflict flag.
    assert result["status"] == "promotion_failed"
    assert result.get("conflict") is True

    # v1 stays open — a failed promotion never closes the version.
    await db_session.refresh(version)
    assert version.status == "open"
    assert version.phase == "stabilize"
    assert version.last_qualification["status"] == "promotion_failed"

    # No v2 was opened — promotion did not succeed.
    next_version = await rv.current_open_version(db_session, project.id)
    assert next_version is None or next_version.version_number == 1

    # The operator received a release_promotion_failed message.
    msgs = await _messages_for(db_session, project.id, "release_promotion_failed")
    assert len(msgs) == 1
    assert msgs[0].context["error"] == "merge_blocked"

    # Verify the post-conflict reconciliation wiring: _maybe_reconcile_after_
    # promotion_conflict fires when result has status=promotion_failed + conflict=True.
    # In production this is triggered by _finalize_in_dedicated_session after
    # its session commits; here we call it directly to assert the wiring is in
    # place and that the reconciler is invoked exactly once for a conflict result.
    await rq._maybe_reconcile_after_promotion_conflict(
        version.id,
        result,
        session_factory=_shared_session_factory(db_session),
    )
    recon_mock.assert_awaited_once()


# ===========================================================================
# CI-unresolved stall alerting — _check_version_ci_unresolved_stall
# ===========================================================================


@pytest.mark.asyncio
async def test_ci_unresolved_stall_alert_fires_after_threshold(db_session):
    """_check_version_ci_unresolved_stall posts an Alert and a
    release_ci_unresolved_stall message after _MIN_CALCIFICATION_ATTEMPTS
    ci_unresolved verdicts."""
    from app.models import Alert as _Alert

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.qualification_attempts = rq._MIN_CALCIFICATION_ATTEMPTS
    version.last_qualification = {
        "status": "not_promoted",
        "blocker": "ci_unresolved",
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    alerted = await rq._check_version_ci_unresolved_stall(db_session, version)
    await db_session.commit()

    assert alerted is True
    alerts = (
        (
            await db_session.execute(
                select(_Alert).where(_Alert.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 1
    assert "CI state unresolvable" in alerts[0].title
    assert "CI infrastructure" in alerts[0].message

    msgs = await _messages_for(db_session, project.id, "release_ci_unresolved_stall")
    assert len(msgs) == 1
    assert version.calcification_alerted_blocker == "ci_unresolved"


@pytest.mark.asyncio
async def test_ci_unresolved_stall_dedup_suppresses_repeat(db_session):
    """A second sweep call with the same ci_unresolved stall is a no-op —
    the alert fires only once per distinct stall."""
    from app.models import Alert as _Alert

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.qualification_attempts = rq._MIN_CALCIFICATION_ATTEMPTS
    version.last_qualification = {
        "status": "not_promoted",
        "blocker": "ci_unresolved",
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    first = await rq._check_version_ci_unresolved_stall(db_session, version)
    await db_session.commit()
    second = await rq._check_version_ci_unresolved_stall(db_session, version)
    await db_session.commit()

    assert first is True
    assert second is False
    alerts = (
        (
            await db_session.execute(
                select(_Alert).where(_Alert.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 1


@pytest.mark.asyncio
async def test_ci_unresolved_stall_does_not_fire_below_threshold(db_session):
    """Fewer than _MIN_CALCIFICATION_ATTEMPTS ci_unresolved verdicts do not
    trigger the stall alert — the unresolved state may be transient."""
    from app.models import Alert as _Alert

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.qualification_attempts = rq._MIN_CALCIFICATION_ATTEMPTS - 1
    version.last_qualification = {
        "status": "not_promoted",
        "blocker": "ci_unresolved",
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    alerted = await rq._check_version_ci_unresolved_stall(db_session, version)

    assert alerted is False
    alerts = (
        (
            await db_session.execute(
                select(_Alert).where(_Alert.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 0


@pytest.mark.asyncio
async def test_ci_unresolved_stall_distinct_from_calcification_alert(db_session):
    """ci_not_green triggers calcification but NOT ci_unresolved stall, and
    ci_unresolved triggers ci_unresolved stall but NOT calcification."""
    # ci_not_green: calcification fires, ci_unresolved stall does not.
    project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.qualification_attempts = rq._MIN_CALCIFICATION_ATTEMPTS
    version.last_qualification = {
        "status": "not_promoted",
        "blocker": "ci_not_green",
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    calc_fired = await rq._check_version_calcification(db_session, version)
    ci_stall_fired = await rq._check_version_ci_unresolved_stall(db_session, version)
    await db_session.commit()

    assert calc_fired is True
    assert ci_stall_fired is False

    # ci_unresolved: ci_unresolved stall fires, calcification does not.
    project2, version2 = await _make_project_and_version(db_session, phase="stabilize")
    version2.qualification_attempts = rq._MIN_CALCIFICATION_ATTEMPTS
    version2.last_qualification = {
        "status": "not_promoted",
        "blocker": "ci_unresolved",
    }
    db_session.add(version2)
    await db_session.commit()
    await db_session.refresh(version2)

    calc_fired2 = await rq._check_version_calcification(db_session, version2)
    ci_stall_fired2 = await rq._check_version_ci_unresolved_stall(db_session, version2)
    await db_session.commit()

    assert calc_fired2 is False
    assert ci_stall_fired2 is True


# ===========================================================================
# pending-auto-merge: timeout bypass and stall alert
# ===========================================================================


@pytest.mark.asyncio
async def test_sweep_skips_timeout_for_version_awaiting_auto_merge(db_session):
    """Audit H2 exemption: the qualification-timeout backstop must NOT
    misclassify a version that is already awaiting promotion auto-merge.
    Qualification completed and passed; the pending clock runs on the
    promotion PR's CI, not on qualifier children."""
    from datetime import timedelta

    from app.time_utils import _utcnow

    project, version = await _make_project_and_version(db_session)

    # Stamp a very old started_at that would normally trigger the timeout,
    # but include a promotion_pending sub-state indicating qualification passed.
    old_started = (
        _utcnow() - timedelta(seconds=rq._MAX_QUALIFICATION_DURATION_SECONDS + 3600)
    ).isoformat()
    await _seed_pending_qualification(
        db_session,
        project,
        version,
        audit_status=TaskStatus.COMPLETED,
        smoke_status=TaskStatus.COMPLETED,
        started_at=old_started,
    )
    # Inject the promotion_pending sub-state directly (simulating what promote()
    # writes after enabling auto-merge).
    version.last_qualification = {
        **version.last_qualification,
        "promotion_pending": {
            "pr_number": 42,
            "pr_url": "http://github.com/pr/42",
            "auto_merge_enabled": True,
            "enabled_at": _utcnow().isoformat(),
        },
    }
    db_session.add(version)
    await db_session.commit()

    count = await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )

    # Sweep did NOT force-finalize this version (count=0) — it is not a
    # hung-qualifier-child timeout; it is awaiting the promotion PR.
    assert count == 0
    await db_session.refresh(version)
    assert version.last_qualification["status"] == "pending"
    assert "blocker" not in version.last_qualification


@pytest.mark.asyncio
async def test_auto_merge_stall_alert_fires_when_stuck(db_session):
    """_check_version_auto_merge_stall fires once and records a dedup anchor
    when a promotion_pending sub-state has been waiting past the stall threshold.
    """
    from datetime import timedelta

    from app.time_utils import _utcnow
    from sqlmodel import select as _select

    project, version = await _make_project_and_version(db_session)

    # Seed a pending qualification with an old auto-merge enabled_at.
    await _seed_pending_qualification(
        db_session,
        project,
        version,
        audit_status=TaskStatus.COMPLETED,
        smoke_status=TaskStatus.COMPLETED,
    )
    old_enabled = (
        _utcnow() - timedelta(seconds=rq._AUTO_MERGE_STALL_SECONDS + 600)
    ).isoformat()
    version.last_qualification = {
        **version.last_qualification,
        "promotion_pending": {
            "pr_number": 77,
            "pr_url": "http://github.com/pr/77",
            "auto_merge_enabled": True,
            "enabled_at": old_enabled,
        },
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    fired = await rq._check_version_auto_merge_stall(db_session, version)
    await db_session.commit()

    assert fired is True

    # An Alert was created.
    alerts = (
        (await db_session.execute(_select(Alert).where(Alert.project_id == project.id)))
        .scalars()
        .all()
    )
    assert len(alerts) == 1
    assert "auto-merge" in alerts[0].title.lower()

    # Dedup anchor is set — a second call returns False.
    await db_session.refresh(version)
    assert version.last_qualification["promotion_pending"].get("alerted_at") is not None
    fired_again = await rq._check_version_auto_merge_stall(db_session, version)
    assert fired_again is False


@pytest.mark.asyncio
async def test_auto_merge_stall_does_not_fire_before_threshold(db_session):
    """_check_version_auto_merge_stall does NOT fire when auto-merge was enabled
    recently (under the stall threshold)."""
    from app.time_utils import _utcnow

    project, version = await _make_project_and_version(db_session)

    await _seed_pending_qualification(
        db_session,
        project,
        version,
        audit_status=TaskStatus.COMPLETED,
        smoke_status=TaskStatus.COMPLETED,
    )
    # enabled_at is recent — under the stall threshold.
    version.last_qualification = {
        **version.last_qualification,
        "promotion_pending": {
            "pr_number": 55,
            "pr_url": "http://github.com/pr/55",
            "auto_merge_enabled": True,
            "enabled_at": _utcnow().isoformat(),
        },
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    fired = await rq._check_version_auto_merge_stall(db_session, version)
    assert fired is False


# ===========================================================================
# sweep reconcile — promotion_failed candidate already on main
# ===========================================================================


@pytest.mark.asyncio
async def test_promotion_failed_candidate_on_main_reconciled(db_session, mocker):
    """An open version with last_qualification.status='promotion_failed' whose
    candidate_sha is already reachable from main is reconciled to 'promoted' by
    the sweep, and the next version is opened (count=1)."""
    from app.models import ReleaseVersion as _RV

    project, version = await _make_project_and_version(db_session)

    candidate_sha = "reconcile" * 4 + "1234"  # 40 chars
    main_head_sha = "mainhead" * 5  # 40 chars
    version.last_qualification = {
        "status": "promotion_failed",
        "candidate_sha": candidate_sha,
        "error": "merge_blocked",
        "conflict": True,
        "confidence": 0.95,
        "sub_scores": {},
        "pr_url": "https://github.com/acme/widget/pull/42",
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    # Candidate IS on main now (operator merged it out-of-band).
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch(
        "app.github.is_ancestor_of",
        new=AsyncMock(return_value=True),
    )
    # PR has no migration files — post-merge check returns "ok" immediately.
    mocker.patch(
        "app.github.get_pr_files",
        new=AsyncMock(return_value=[{"filename": "backend/app/foo.py"}]),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=AsyncMock(return_value=main_head_sha),
    )
    mocker.patch("app.github.create_tag", new=AsyncMock(return_value=None))

    count = await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )
    await db_session.commit()

    # Sweep counted this as a finalization.
    assert count == 1

    # Version is now promoted.
    await db_session.refresh(version)
    assert version.status == "promoted"
    assert version.last_qualification["status"] == "promoted"
    assert version.last_qualification["already_landed"] is True
    assert version.promoted_sha == main_head_sha

    # Next version was opened.
    next_versions = (
        (
            await db_session.execute(
                select(_RV).where(
                    _RV.project_id == project.id,
                    _RV.version_number == 2,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(next_versions) == 1
    assert next_versions[0].status == "open"


@pytest.mark.asyncio
async def test_promotion_failed_candidate_not_on_main_skipped(db_session, mocker):
    """An open version with last_qualification.status='promotion_failed' whose
    candidate_sha is NOT on main is left untouched by the sweep (count=0)."""
    project, version = await _make_project_and_version(db_session)

    candidate_sha = "notlanded" * 4 + "abcd"  # 40 chars
    version.last_qualification = {
        "status": "promotion_failed",
        "candidate_sha": candidate_sha,
        "error": "merge_blocked",
        "conflict": True,
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    # Candidate is NOT on main yet.
    mocker.patch(
        "app.github.is_ancestor_of",
        new=AsyncMock(return_value=False),
    )

    count = await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )
    await db_session.commit()

    assert count == 0

    # Version untouched — still open with promotion_failed record.
    await db_session.refresh(version)
    assert version.status == "open"
    assert version.last_qualification["status"] == "promotion_failed"


@pytest.mark.asyncio
async def test_promotion_failed_reconcile_idempotent(db_session, mocker):
    """Calling sweep twice on a version that was reconciled on the first call
    does NOT double-promote or open a duplicate next version (count=0 on the
    second call)."""
    from app.models import ReleaseVersion as _RV

    project, version = await _make_project_and_version(db_session)

    candidate_sha = "idemptest" * 4 + "12ab"  # 40 chars
    main_head_sha = "mainhead" * 5  # 40 chars
    version.last_qualification = {
        "status": "promotion_failed",
        "candidate_sha": candidate_sha,
        "error": "merge_blocked",
        "pr_url": "https://github.com/acme/widget/pull/42",
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch(
        "app.github.is_ancestor_of",
        new=AsyncMock(return_value=True),
    )
    # PR has no migration files — post-merge check returns "ok" immediately.
    mocker.patch(
        "app.github.get_pr_files",
        new=AsyncMock(return_value=[{"filename": "backend/app/foo.py"}]),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=AsyncMock(return_value=main_head_sha),
    )
    mocker.patch("app.github.create_tag", new=AsyncMock(return_value=None))

    first_count = await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )
    await db_session.commit()
    assert first_count == 1

    # Second sweep — version is now promoted (not open), reconcile is a no-op.
    second_count = await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )
    await db_session.commit()
    assert second_count == 0

    # Exactly one next version was opened.
    next_versions = (
        (
            await db_session.execute(
                select(_RV).where(
                    _RV.project_id == project.id,
                    _RV.version_number == 2,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(next_versions) == 1


@pytest.mark.asyncio
async def test_non_promotion_failed_status_not_reconciled(db_session, mocker):
    """A version with last_qualification.status='not_promoted' is NOT touched by
    the reconcile loop — only 'promotion_failed' versions are candidates."""
    project, version = await _make_project_and_version(db_session)

    version.last_qualification = {
        "status": "not_promoted",
        "candidate_sha": "aabbcc" * 6 + "aabb",
        "blocker": "ci_not_green",
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    # is_ancestor_of would return True if called, but it should NOT be called.
    is_ancestor_mock = mocker.patch(
        "app.github.is_ancestor_of",
        new=AsyncMock(return_value=True),
    )

    count = await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )
    await db_session.commit()

    assert count == 0
    assert version.status == "open"
    assert version.last_qualification["status"] == "not_promoted"
    # The ancestry check must NOT have been invoked for a non-promotion_failed version.
    is_ancestor_mock.assert_not_called()


# ===========================================================================
# sweep reconcile — pending_auto_merge: candidate landed / PR closed
# ===========================================================================


@pytest.mark.asyncio
async def test_pending_auto_merge_landed_reconciled_to_promoted(db_session, mocker):
    """An open version with status='pending' + promotion_pending sub-state whose
    candidate_sha is on main is reconciled to 'promoted' by the sweep, and the
    next version is opened (count=1)."""
    from app.models import ReleaseVersion as _RV
    from app.time_utils import _utcnow

    project, version = await _make_project_and_version(db_session)

    candidate_sha = "pendlnd0" * 5  # 40 chars
    main_head_sha = "mainhead" * 5  # 40 chars
    await _seed_pending_qualification(
        db_session,
        project,
        version,
        audit_status=TaskStatus.COMPLETED,
        smoke_status=TaskStatus.COMPLETED,
    )
    # Inject the promotion_pending sub-state (simulating what promote() writes).
    version.last_qualification = {
        **version.last_qualification,
        "candidate_sha": candidate_sha,
        "promotion_pending": {
            "pr_number": 55,
            "pr_url": "https://github.com/acme/widget/pull/55",
            "auto_merge_enabled": True,
            "enabled_at": _utcnow().isoformat(),
        },
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch(
        "app.github.is_ancestor_of",
        new=AsyncMock(return_value=True),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=AsyncMock(return_value=main_head_sha),
    )
    mocker.patch("app.github.create_tag", new=AsyncMock(return_value=None))

    count = await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )
    await db_session.commit()

    assert count == 1

    await db_session.refresh(version)
    assert version.status == "promoted"
    assert version.last_qualification["status"] == "promoted"
    assert version.last_qualification["already_landed"] is True
    assert version.promoted_sha == main_head_sha

    # Next version was opened.
    next_versions = (
        (
            await db_session.execute(
                select(_RV).where(
                    _RV.project_id == project.id,
                    _RV.version_number == 2,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(next_versions) == 1
    assert next_versions[0].status == "open"


@pytest.mark.asyncio
async def test_pending_auto_merge_not_yet_merged_not_reconciled(db_session, mocker):
    """An open version with status='pending' + promotion_pending whose candidate
    is NOT yet on main (PR CI still pending) is left untouched (count=0)."""
    from app.time_utils import _utcnow

    project, version = await _make_project_and_version(db_session)

    candidate_sha = "notmerged" * 4 + "abcd"  # 40 chars
    await _seed_pending_qualification(
        db_session,
        project,
        version,
        audit_status=TaskStatus.COMPLETED,
        smoke_status=TaskStatus.COMPLETED,
    )
    version.last_qualification = {
        **version.last_qualification,
        "candidate_sha": candidate_sha,
        "promotion_pending": {
            "pr_number": 66,
            "pr_url": "https://github.com/acme/widget/pull/66",
            "auto_merge_enabled": True,
            "enabled_at": _utcnow().isoformat(),
        },
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    # Candidate NOT on main yet (CI still running).
    mocker.patch(
        "app.github.is_ancestor_of",
        new=AsyncMock(return_value=False),
    )
    # PR is still open — not closed without merge.
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(return_value={"state": "open", "merged": False}),
    )

    count = await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )
    await db_session.commit()

    assert count == 0

    await db_session.refresh(version)
    assert version.status == "open"
    assert version.last_qualification["status"] == "pending"
    assert "promotion_pending" in version.last_qualification


@pytest.mark.asyncio
async def test_pending_auto_merge_reconcile_idempotent(db_session, mocker):
    """Calling sweep twice after candidate lands does NOT double-promote or open
    a duplicate next version (count=0 on the second call)."""
    from app.models import ReleaseVersion as _RV
    from app.time_utils import _utcnow

    project, version = await _make_project_and_version(db_session)

    candidate_sha = "idmptpnd" * 5  # 40 chars
    main_head_sha = "mnhdidem" * 5  # 40 chars
    await _seed_pending_qualification(
        db_session,
        project,
        version,
        audit_status=TaskStatus.COMPLETED,
        smoke_status=TaskStatus.COMPLETED,
    )
    version.last_qualification = {
        **version.last_qualification,
        "candidate_sha": candidate_sha,
        "promotion_pending": {
            "pr_number": 77,
            "pr_url": "https://github.com/acme/widget/pull/77",
            "auto_merge_enabled": True,
            "enabled_at": _utcnow().isoformat(),
        },
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch(
        "app.github.is_ancestor_of",
        new=AsyncMock(return_value=True),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=AsyncMock(return_value=main_head_sha),
    )
    mocker.patch("app.github.create_tag", new=AsyncMock(return_value=None))

    first_count = await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )
    await db_session.commit()
    assert first_count == 1

    # Second sweep — version is now promoted (not open), reconcile is a no-op.
    second_count = await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )
    await db_session.commit()
    assert second_count == 0

    # Exactly one next version was opened.
    next_versions = (
        (
            await db_session.execute(
                select(_RV).where(
                    _RV.project_id == project.id,
                    _RV.version_number == 2,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(next_versions) == 1


@pytest.mark.asyncio
async def test_pending_status_without_promotion_pending_not_reconciled(
    db_session, mocker
):
    """A version with status='pending' but NO promotion_pending sub-state is left
    alone by the pending_auto_merge reconcile loop (normal in-flight qualification
    with children still running)."""
    project, version = await _make_project_and_version(db_session)

    await _seed_pending_qualification(
        db_session,
        project,
        version,
        audit_status=TaskStatus.IN_PROGRESS,
        smoke_status=TaskStatus.IN_PROGRESS,
    )
    # Ensure no promotion_pending key is present.
    assert "promotion_pending" not in (version.last_qualification or {})

    is_ancestor_mock = mocker.patch(
        "app.github.is_ancestor_of",
        new=AsyncMock(return_value=True),
    )

    count = await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )
    await db_session.commit()

    assert count == 0
    await db_session.refresh(version)
    assert version.status == "open"
    assert version.last_qualification["status"] == "pending"
    # Ancestry check must NOT have fired — no promotion_pending means no reconcile.
    is_ancestor_mock.assert_not_called()


@pytest.mark.asyncio
async def test_pending_auto_merge_pr_closed_without_merge_drives_to_promotion_failed(
    db_session, mocker
):
    """When the promotion PR is closed without merging the reconcile loop drives
    the version to promotion_failed so the retry machinery can re-attempt."""
    from app.time_utils import _utcnow

    project, version = await _make_project_and_version(db_session)

    candidate_sha = "closedpr" * 5  # 40 chars
    await _seed_pending_qualification(
        db_session,
        project,
        version,
        audit_status=TaskStatus.COMPLETED,
        smoke_status=TaskStatus.COMPLETED,
    )
    version.last_qualification = {
        **version.last_qualification,
        "candidate_sha": candidate_sha,
        "promotion_pending": {
            "pr_number": 88,
            "pr_url": "https://github.com/acme/widget/pull/88",
            "auto_merge_enabled": True,
            "enabled_at": _utcnow().isoformat(),
        },
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    # Candidate is NOT on main (PR was closed, not merged).
    mocker.patch(
        "app.github.is_ancestor_of",
        new=AsyncMock(return_value=False),
    )
    # PR is closed without merging.
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(return_value={"state": "closed", "merged": False}),
    )
    mocker.patch("app.github.create_tag", new=AsyncMock(return_value=None))

    count = await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )
    await db_session.commit()

    # Sweep counted the state change as a finalization.
    assert count == 1

    await db_session.refresh(version)
    assert version.status == "open"  # stays open — promotion_failed keeps it open
    assert version.last_qualification["status"] == "promotion_failed"
    assert version.last_qualification["error"] == "pr_closed_without_merge"


# ===========================================================================
# Audit C9 — promotion_pending wall-clock deadline + escalating stall alert
#
# The promotion_pending sub-state intentionally bypasses the 6h qualification
# timeout, but had NO replacement backstop: if the promotion PR stays open with
# pending/absent CI (or GitHub calls fail) the terminal-state reconcile is a
# no-op and the version strands indefinitely, blocking the self-improve loop and
# (because the stall alert fired exactly once) becoming operator-invisible.
# ===========================================================================


@pytest.mark.asyncio
async def test_promotion_pending_timeout_force_fails_stranded_version(
    db_session, mocker
):
    """A version stranded in promotion_pending past the wall-clock cap is
    force-failed as 'promotion_pending_timeout', clearing the sub-state so the
    version returns to stabilize and the normal backstop re-arms.

    WITHOUT the C9 fix this version stays pending forever — no GitHub I/O is
    even attempted here (PR state cannot be resolved), which is exactly the
    no-op-reconcile case the old code could not recover from.
    """
    from datetime import timedelta

    from app.release_reconciliation import (
        _reconcile_if_promotion_pending_timed_out,
        _promotion_pending_timeout_seconds,
    )
    from app.time_utils import _utcnow

    project, version = await _make_project_and_version(db_session)

    await _seed_pending_qualification(
        db_session,
        project,
        version,
        audit_status=TaskStatus.COMPLETED,
        smoke_status=TaskStatus.COMPLETED,
    )
    # enabled_at is older than the cap — the promotion has stranded.
    stranded_enabled = (
        _utcnow()
        - timedelta(seconds=_promotion_pending_timeout_seconds() + 3600)
    ).isoformat()
    version.last_qualification = {
        **version.last_qualification,
        "candidate_sha": "strandpp" * 5,  # 40 chars
        "promotion_pending": {
            "pr_number": 99,
            "pr_url": "https://github.com/acme/widget/pull/99",
            "auto_merge_enabled": True,
            "enabled_at": stranded_enabled,
        },
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    timed_out = await _reconcile_if_promotion_pending_timed_out(
        version.id, session_factory=_shared_session_factory(db_session)
    )
    await db_session.commit()

    assert timed_out is True

    await db_session.refresh(version)
    # Stays open (promotion failure never touches main) and returns to stabilize.
    assert version.status == "open"
    assert version.phase == "stabilize"
    assert version.last_qualification["status"] == "promotion_failed"
    assert version.last_qualification["error"] == "promotion_pending_timeout"
    # The sub-state is CLEARED so the qualification-timeout backstop re-arms.
    assert "promotion_pending" not in version.last_qualification


@pytest.mark.asyncio
async def test_promotion_pending_timeout_does_not_fire_before_cap(db_session):
    """A promotion_pending sub-state younger than the cap is left untouched —
    a slow-but-progressing promotion PR must not be force-failed."""
    from app.release_reconciliation import _reconcile_if_promotion_pending_timed_out
    from app.time_utils import _utcnow

    project, version = await _make_project_and_version(db_session)

    await _seed_pending_qualification(
        db_session,
        project,
        version,
        audit_status=TaskStatus.COMPLETED,
        smoke_status=TaskStatus.COMPLETED,
    )
    version.last_qualification = {
        **version.last_qualification,
        "candidate_sha": "freshpp0" * 5,  # 40 chars
        "promotion_pending": {
            "pr_number": 100,
            "pr_url": "https://github.com/acme/widget/pull/100",
            "auto_merge_enabled": True,
            "enabled_at": _utcnow().isoformat(),
        },
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    timed_out = await _reconcile_if_promotion_pending_timed_out(
        version.id, session_factory=_shared_session_factory(db_session)
    )
    await db_session.commit()

    assert timed_out is False

    await db_session.refresh(version)
    assert version.status == "open"
    assert version.last_qualification["status"] == "pending"
    assert "promotion_pending" in version.last_qualification


@pytest.mark.asyncio
async def test_promotion_pending_timeout_wired_into_sweep(db_session):
    """The wall-clock deadline is reachable through the public sweep entry point
    (sweep_stuck_qualifications), proving the wiring — not just the helper."""
    from datetime import timedelta

    from app import shared_state
    from app.release_reconciliation import _promotion_pending_timeout_seconds
    from app.time_utils import _utcnow

    # Disable promotion so the readiness check in the same sweep is a no-op.
    shared_state.runtime_config["promotion_enabled"] = False

    project, version = await _make_project_and_version(db_session)

    await _seed_pending_qualification(
        db_session,
        project,
        version,
        audit_status=TaskStatus.COMPLETED,
        smoke_status=TaskStatus.COMPLETED,
    )
    stranded_enabled = (
        _utcnow()
        - timedelta(seconds=_promotion_pending_timeout_seconds() + 3600)
    ).isoformat()
    version.last_qualification = {
        **version.last_qualification,
        "candidate_sha": "sweepstr" * 5,  # 40 chars
        "promotion_pending": {
            "pr_number": 101,
            "pr_url": "https://github.com/acme/widget/pull/101",
            "auto_merge_enabled": True,
            "enabled_at": stranded_enabled,
        },
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    count = await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )
    await db_session.commit()

    assert count == 1

    await db_session.refresh(version)
    assert version.status == "open"
    assert version.last_qualification["status"] == "promotion_failed"
    assert version.last_qualification["error"] == "promotion_pending_timeout"
    assert "promotion_pending" not in version.last_qualification


@pytest.mark.asyncio
async def test_promotion_pending_timeout_respects_runtime_config_override(db_session):
    """The cap is configurable via runtime_config; a positive override changes
    when the deadline fires (and a non-positive override is ignored)."""
    from app import shared_state
    from app.release_reconciliation import (
        _PROMOTION_PENDING_TIMEOUT_SECONDS_DEFAULT,
        _promotion_pending_timeout_seconds,
    )

    # Positive override is honored.
    shared_state.runtime_config["promotion_pending_timeout_seconds"] = 3600
    try:
        assert _promotion_pending_timeout_seconds() == 3600
        # Non-positive / bogus overrides fall back to the default (never disable
        # the backstop).
        shared_state.runtime_config["promotion_pending_timeout_seconds"] = 0
        assert (
            _promotion_pending_timeout_seconds()
            == _PROMOTION_PENDING_TIMEOUT_SECONDS_DEFAULT
        )
        shared_state.runtime_config["promotion_pending_timeout_seconds"] = "garbage"
        assert (
            _promotion_pending_timeout_seconds()
            == _PROMOTION_PENDING_TIMEOUT_SECONDS_DEFAULT
        )
    finally:
        shared_state.runtime_config.pop("promotion_pending_timeout_seconds", None)


@pytest.mark.asyncio
async def test_auto_merge_stall_alert_refires_and_escalates(db_session):
    """The auto-merge stall alert is NO LONGER single-fire: it re-fires once per
    re-alert interval and escalates from WARNING to ERROR (the highest
    AlertSeverity) past the critical threshold, so a stranded promotion stays
    operator-visible.

    WITHOUT the C9 fix the second call returns False permanently (the old code
    bailed once ``alerted_at`` was set), so the CRITICAL escalation never fires.
    """
    from datetime import timedelta

    from app import release_stall_alerts as rsa
    from app.models import AlertSeverity
    from app.time_utils import _utcnow
    from sqlmodel import select as _select

    project, version = await _make_project_and_version(db_session)

    await _seed_pending_qualification(
        db_session,
        project,
        version,
        audit_status=TaskStatus.COMPLETED,
        smoke_status=TaskStatus.COMPLETED,
    )

    # First fire: stalled past the WARNING threshold but under critical.
    first_enabled = _utcnow() - timedelta(seconds=rq._AUTO_MERGE_STALL_SECONDS + 600)
    version.last_qualification = {
        **version.last_qualification,
        "promotion_pending": {
            "pr_number": 202,
            "pr_url": "https://github.com/acme/widget/pull/202",
            "auto_merge_enabled": True,
            "enabled_at": first_enabled.isoformat(),
        },
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    fired = await rq._check_version_auto_merge_stall(db_session, version)
    await db_session.commit()
    assert fired is True

    await db_session.refresh(version)
    pending = version.last_qualification["promotion_pending"]
    assert pending["alert_count"] == 1
    assert pending.get("last_alerted_at") is not None

    # An immediate second call is suppressed by the re-alert interval gate.
    fired_again = await rq._check_version_auto_merge_stall(db_session, version)
    assert fired_again is False

    # Now simulate time passing PAST both the re-alert interval and the critical
    # threshold: push enabled_at + last_alerted_at far enough back.
    crit_enabled = _utcnow() - timedelta(
        seconds=rsa._AUTO_MERGE_STALL_CRITICAL_SECONDS + 600
    )
    old_last_alert = _utcnow() - timedelta(
        seconds=rsa._AUTO_MERGE_REALERT_INTERVAL_SECONDS + 600
    )
    pend = dict(version.last_qualification["promotion_pending"])
    pend["enabled_at"] = crit_enabled.isoformat()
    pend["last_alerted_at"] = old_last_alert.isoformat()
    version.last_qualification = {
        **version.last_qualification,
        "promotion_pending": pend,
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    fired_critical = await rq._check_version_auto_merge_stall(db_session, version)
    await db_session.commit()
    assert fired_critical is True

    await db_session.refresh(version)
    pending = version.last_qualification["promotion_pending"]
    # Re-fired (count incremented) — proves it is no longer single-fire.
    assert pending["alert_count"] == 2

    # The most recent alert escalated to ERROR (the highest severity tier).
    alerts = (
        (
            await db_session.execute(
                _select(Alert)
                .where(Alert.project_id == project.id)
                .order_by(Alert.created_at)
            )
        )
        .scalars()
        .all()
    )
    assert any(a.severity == AlertSeverity.ERROR for a in alerts)
    assert any(a.severity == AlertSeverity.WARNING for a in alerts)


# ---------------------------------------------------------------------------
# get_latest_release_audit_report — observability helper
#
# A qualification verdict only persists the composite + sub-scores, and the
# task API strips artifact logs, so operators are blind to WHY a candidate
# scored what it did.  This helper surfaces the latest release_audit task's
# RELEASE_AUDIT_REPORT (audit_score, findings, test_erosion) server-side.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_latest_release_audit_report_none_when_no_audit_task(db_session):
    """No release_audit task → None (a fresh project has no audit history)."""
    project, _version = await _make_project_and_version(db_session)

    result = await rq.get_latest_release_audit_report(
        db_session, project.id, candidate_sha="feed" * 10
    )

    assert result is None


@pytest.mark.asyncio
async def test_get_latest_release_audit_report_returns_parsed_report(db_session):
    """A release_audit task with a RELEASE_AUDIT_REPORT artifact → parsed
    report plus task metadata (id, status, candidate_sha, created_at)."""
    project, version = await _make_project_and_version(db_session)

    report_logs = {
        "audit_score": 0.74,
        "test_erosion": {
            "deleted_test_files": [],
            "assertion_delta": 12,
            "test_count_delta": 3,
        },
        "findings": [
            {"severity": "high", "description": "Diff is unusually large."},
        ],
    }
    audit_task = Task(
        project_id=project.id,
        title="release audit",
        payloads=[{"type": "release_audit", "base_branch": "main"}],
        status=TaskStatus.COMPLETED,
        context={
            "release_version_id": str(version.id),
            "candidate_sha": "feed" * 10,
        },
    )
    db_session.add(audit_task)
    await db_session.commit()
    await db_session.refresh(audit_task)
    db_session.add(
        Artifact(
            task_id=audit_task.id,
            artifact_type=ArtifactType.RELEASE_AUDIT_REPORT,
            logs=json.dumps(report_logs),
        )
    )
    await db_session.commit()

    result = await rq.get_latest_release_audit_report(
        db_session, project.id, candidate_sha="feed" * 10
    )

    assert result is not None
    assert result["audit_task_id"] == str(audit_task.id)
    assert result["task_status"] == "COMPLETED"
    assert result["candidate_sha"] == "feed" * 10
    assert result["created_at"] is not None
    assert result["report"] == report_logs


@pytest.mark.asyncio
async def test_get_latest_release_audit_report_report_none_when_no_artifact(db_session):
    """A release_audit task that crashed without posting a report → the task
    metadata is returned but ``report`` is None (not an error)."""
    project, _version = await _make_project_and_version(db_session)

    audit_task = Task(
        project_id=project.id,
        title="crashed audit",
        payloads=[{"type": "release_audit", "base_branch": "main"}],
        status=TaskStatus.FAILED,
        context={"candidate_sha": "feed" * 10},
    )
    db_session.add(audit_task)
    await db_session.commit()
    await db_session.refresh(audit_task)

    result = await rq.get_latest_release_audit_report(
        db_session, project.id, candidate_sha="feed" * 10
    )

    assert result is not None
    assert result["audit_task_id"] == str(audit_task.id)
    assert result["task_status"] == "FAILED"
    assert result["report"] is None


@pytest.mark.asyncio
async def test_get_latest_release_audit_report_scopes_to_candidate_sha(db_session):
    """With several release_audit tasks, the latest matching candidate SHA wins."""
    from datetime import timedelta

    from app.time_utils import _utcnow

    project, _version = await _make_project_and_version(db_session)
    base = _utcnow()
    candidate_sha = "feed" * 10

    old_task = Task(
        project_id=project.id,
        title="old audit",
        payloads=[{"type": "release_audit", "base_branch": "main"}],
        status=TaskStatus.COMPLETED,
        created_at=base - timedelta(hours=2),
        context={"candidate_sha": candidate_sha},
    )
    new_task = Task(
        project_id=project.id,
        title="new audit",
        payloads=[{"type": "release_audit", "base_branch": "main"}],
        status=TaskStatus.COMPLETED,
        created_at=base,
        context={"candidate_sha": "beef" * 10},
    )
    # A non-audit task that must be ignored even though it is the newest.
    other_task = Task(
        project_id=project.id,
        title="some smoketest",
        payloads=[{"type": "smoketest", "base_branch": "automain"}],
        status=TaskStatus.COMPLETED,
        created_at=base + timedelta(hours=1),
    )
    db_session.add_all([old_task, new_task, other_task])
    await db_session.commit()
    await db_session.refresh(old_task)
    db_session.add(
        Artifact(
            task_id=old_task.id,
            artifact_type=ArtifactType.RELEASE_AUDIT_REPORT,
            logs=json.dumps({"audit_score": 0.91}),
        )
    )
    await db_session.commit()

    result = await rq.get_latest_release_audit_report(
        db_session, project.id, candidate_sha=candidate_sha
    )

    assert result is not None
    assert result["audit_task_id"] == str(old_task.id)
    assert result["report"] == {"audit_score": 0.91}


# ---------------------------------------------------------------------------
# resolve_lifecycle_audit_evidence / _select_lifecycle_audit — version-scoped
# audit evidence
#
# The lifecycle view must only show audit evidence belonging to the open
# version's evaluated candidate.  These cover the pure selector's classification
# (current / stale / none) plus the version-scoped DB resolver.
# ---------------------------------------------------------------------------


def test_select_lifecycle_audit_matching_candidate_is_current():
    """The newest row whose candidate_sha equals the evaluated SHA → current."""
    rows = [
        {"candidate_sha": "newc" * 10},
        {"candidate_sha": "0ld0" * 10},
    ]
    sel = rq._select_lifecycle_audit(rows, "newc" * 10)
    assert sel["state"] == "current"
    assert sel["current"] is rows[0]
    assert sel["stale"] is None


def test_select_lifecycle_audit_only_prior_candidate_is_stale():
    """Audits exist but none match the evaluated SHA → newest is stale."""
    rows = [
        {"candidate_sha": "0ld0" * 10},
        {"candidate_sha": "evnoldr" * 5 + "00000"},
    ]
    sel = rq._select_lifecycle_audit(rows, "newc" * 10)
    assert sel["state"] == "stale"
    assert sel["current"] is None
    assert sel["stale"] is rows[0]


def test_select_lifecycle_audit_empty_is_none():
    """No audit rows → none, regardless of evaluated SHA."""
    sel = rq._select_lifecycle_audit([], "newc" * 10)
    assert sel == {"state": "none", "current": None, "stale": None}


def test_select_lifecycle_audit_no_evaluated_sha_is_stale():
    """Rows present but no evaluated SHA to match → newest treated as stale."""
    rows = [{"candidate_sha": "0ld0" * 10}]
    sel = rq._select_lifecycle_audit(rows, None)
    assert sel["state"] == "stale"
    assert sel["stale"] is rows[0]


async def _add_release_audit_task(
    db_session,
    project,
    *,
    version_id,
    candidate_sha,
    created_at=None,
    report=None,
    status=TaskStatus.COMPLETED,
):
    """Insert a release_audit Task scoped to a version (+ optional report)."""
    kwargs = {}
    if created_at is not None:
        kwargs["created_at"] = created_at
    task = Task(
        project_id=project.id,
        title="release audit",
        payloads=[{"type": "release_audit", "base_branch": "main"}],
        status=status,
        context={
            "release_version_id": str(version_id),
            "candidate_sha": candidate_sha,
        },
        **kwargs,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    if report is not None:
        db_session.add(
            Artifact(
                task_id=task.id,
                artifact_type=ArtifactType.RELEASE_AUDIT_REPORT,
                logs=json.dumps(report),
            )
        )
        await db_session.commit()
    return task


@pytest.mark.asyncio
async def test_resolve_lifecycle_audit_evidence_matching_candidate(db_session):
    """An audit pinned to the open version's evaluated candidate → current."""
    project, version = await _make_project_and_version(db_session)
    candidate = "feed" * 10
    version.last_qualification = {"status": "pending", "candidate_sha": candidate}
    db_session.add(version)
    await db_session.commit()

    report = {"audit_score": 0.8, "findings": []}
    task = await _add_release_audit_task(
        db_session, project, version_id=version.id, candidate_sha=candidate, report=report
    )

    result = await rq.resolve_lifecycle_audit_evidence(db_session, version)

    assert result["state"] == "current"
    assert result["stale_audit"] is None
    assert result["latest_audit"] is not None
    assert result["latest_audit"]["audit_task_id"] == str(task.id)
    assert result["latest_audit"]["candidate_sha"] == candidate
    assert result["latest_audit"]["report"] == report


@pytest.mark.asyncio
async def test_resolve_lifecycle_audit_evidence_stale_prior_candidate(db_session):
    """Only an audit against an OLDER candidate exists → stale provenance, no
    current report (the prior findings must not be presented as current)."""
    project, version = await _make_project_and_version(db_session)
    version.last_qualification = {"status": "pending", "candidate_sha": "newc" * 10}
    db_session.add(version)
    await db_session.commit()

    report = {"audit_score": 0.5, "findings": [{"severity": "high"}]}
    task = await _add_release_audit_task(
        db_session,
        project,
        version_id=version.id,
        candidate_sha="0ld0" * 10,
        report=report,
    )

    result = await rq.resolve_lifecycle_audit_evidence(db_session, version)

    assert result["state"] == "stale"
    assert result["latest_audit"] is None
    assert result["stale_audit"] is not None
    assert result["stale_audit"]["audit_task_id"] == str(task.id)
    assert result["stale_audit"]["candidate_sha"] == "0ld0" * 10
    assert result["stale_audit"]["report"] == report


@pytest.mark.asyncio
async def test_resolve_lifecycle_audit_evidence_excludes_other_version(db_session):
    """An audit belonging to a DIFFERENT release version is never attached —
    a fresh version with no audit of its own returns the explicit none state."""
    project, version = await _make_project_and_version(db_session)
    version.last_qualification = {"status": "pending", "candidate_sha": "feed" * 10}
    db_session.add(version)
    await db_session.commit()

    other_version_id = uuid.uuid4()
    await _add_release_audit_task(
        db_session,
        project,
        version_id=other_version_id,
        candidate_sha="feed" * 10,
        report={"audit_score": 0.9},
    )

    result = await rq.resolve_lifecycle_audit_evidence(db_session, version)

    assert result["state"] == "none"
    assert result["latest_audit"] is None
    assert result["stale_audit"] is None


@pytest.mark.asyncio
async def test_resolve_lifecycle_audit_evidence_none_when_no_audit(db_session):
    """No release_audit task for the version → explicit none state."""
    project, version = await _make_project_and_version(db_session)
    version.last_qualification = {"status": "pending", "candidate_sha": "feed" * 10}
    db_session.add(version)
    await db_session.commit()

    result = await rq.resolve_lifecycle_audit_evidence(db_session, version)

    assert result == {"state": "none", "latest_audit": None, "stale_audit": None}


@pytest.mark.asyncio
async def test_resolve_lifecycle_audit_evidence_current_report_none_no_artifact(
    db_session,
):
    """A matching audit that crashed without a report → current with a null
    report body but provenance present (state stays current, not none)."""
    project, version = await _make_project_and_version(db_session)
    candidate = "feed" * 10
    version.last_qualification = {"status": "pending", "candidate_sha": candidate}
    db_session.add(version)
    await db_session.commit()

    task = await _add_release_audit_task(
        db_session,
        project,
        version_id=version.id,
        candidate_sha=candidate,
        status=TaskStatus.FAILED,
    )

    result = await rq.resolve_lifecycle_audit_evidence(db_session, version)

    assert result["state"] == "current"
    assert result["latest_audit"] is not None
    assert result["latest_audit"]["audit_task_id"] == str(task.id)
    assert result["latest_audit"]["task_status"] == "FAILED"
    assert result["latest_audit"]["report"] is None


@pytest.mark.asyncio
async def test_get_latest_release_audit_report_none_when_candidate_missing(db_session):
    """No open-version candidate SHA means there is no comparable audit scope."""
    project, _version = await _make_project_and_version(db_session)

    audit_task = Task(
        project_id=project.id,
        title="candidate-scoped audit",
        payloads=[{"type": "release_audit", "base_branch": "main"}],
        status=TaskStatus.COMPLETED,
        context={"candidate_sha": "feed" * 10},
    )
    db_session.add(audit_task)
    await db_session.commit()

    result = await rq.get_latest_release_audit_report(db_session, project.id)

    assert result is None


@pytest.mark.asyncio
async def test_pending_auto_merge_pr_ci_red_drives_to_promotion_failed(
    db_session, mocker
):
    """When the promotion PR is still open but its CI is conclusively red, the
    reconcile loop drives the version to promotion_failed so the retry machinery
    can re-attempt on the next strategic cycle."""
    from app.time_utils import _utcnow

    project, version = await _make_project_and_version(db_session)

    candidate_sha = "redcired" * 5  # 40 chars
    pr_head_sha = "prheadci" * 5  # 40 chars
    await _seed_pending_qualification(
        db_session,
        project,
        version,
        audit_status=TaskStatus.COMPLETED,
        smoke_status=TaskStatus.COMPLETED,
    )
    version.last_qualification = {
        **version.last_qualification,
        "candidate_sha": candidate_sha,
        "promotion_pending": {
            "pr_number": 99,
            "pr_url": "https://github.com/acme/widget/pull/99",
            "auto_merge_enabled": True,
            "enabled_at": _utcnow().isoformat(),
        },
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    # Candidate NOT on main (CI failed, auto-merge never fired).
    mocker.patch(
        "app.github.is_ancestor_of",
        new=AsyncMock(return_value=False),
    )
    # PR is still open but CI on the PR head SHA is conclusively red.
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={
                "state": "open",
                "merged": False,
                "head": {"sha": pr_head_sha},
            }
        ),
    )
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(
            return_value={
                "status": "completed",
                "conclusion": "failure",
                "details": [],
                "failed_logs": "",
            }
        ),
    )
    mocker.patch("app.github.create_tag", new=AsyncMock(return_value=None))

    count = await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )
    await db_session.commit()

    assert count == 1

    await db_session.refresh(version)
    assert version.status == "open"  # stays open — promotion_failed keeps it open
    assert version.last_qualification["status"] == "promotion_failed"
    assert version.last_qualification["error"] == "pr_ci_red"


@pytest.mark.asyncio
async def test_pending_auto_merge_pr_ci_pending_not_reconciled(db_session, mocker):
    """When the promotion PR is still open and CI is still running (unresolved),
    the reconcile loop leaves the version pending — never aborts a healthy
    in-flight promotion."""
    from app.time_utils import _utcnow

    project, version = await _make_project_and_version(db_session)

    candidate_sha = "pendingc" * 5  # 40 chars
    pr_head_sha = "prheadpd" * 5  # 40 chars
    await _seed_pending_qualification(
        db_session,
        project,
        version,
        audit_status=TaskStatus.COMPLETED,
        smoke_status=TaskStatus.COMPLETED,
    )
    version.last_qualification = {
        **version.last_qualification,
        "candidate_sha": candidate_sha,
        "promotion_pending": {
            "pr_number": 101,
            "pr_url": "https://github.com/acme/widget/pull/101",
            "auto_merge_enabled": True,
            "enabled_at": _utcnow().isoformat(),
        },
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    # Candidate NOT on main (CI still running).
    mocker.patch(
        "app.github.is_ancestor_of",
        new=AsyncMock(return_value=False),
    )
    # PR is open, CI is still in progress (unresolved).
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={
                "state": "open",
                "merged": False,
                "head": {"sha": pr_head_sha},
            }
        ),
    )
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(
            return_value={
                "status": "in_progress",
                "conclusion": None,
                "details": [],
            }
        ),
    )

    count = await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )
    await db_session.commit()

    assert count == 0

    await db_session.refresh(version)
    assert version.status == "open"
    assert version.last_qualification["status"] == "pending"
    assert "promotion_pending" in version.last_qualification


# ===========================================================================
# Per-project branch resolution — _lts_branch_names must use project columns
# ===========================================================================


async def _make_project_and_version_custom_branches(
    db_session,
    *,
    integration_branch: str,
    promotion_target: str,
    phase: str = "stabilize",
):
    """Create a project with non-default branch columns and an open version."""
    project = Project(
        name="custom-branch-project",
        user_id="cb-user",
        github_owner="acme",
        github_repo="custom",
        integration_branch=integration_branch,
        promotion_target=promotion_target,
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    from app import release_version as _rv

    version = await _rv.open_version(db_session, project.id, version_number=1)
    if phase != "build":
        _rv.set_version_phase(version, phase, reasoning="test setup")
        db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)
    return project, version


@pytest.mark.asyncio
async def test_run_qualification_uses_project_integration_branch(db_session, mocker):
    """run_qualification must snapshot the candidate SHA from the project's
    configured integration_branch, not the global runtime_config default.

    A project with integration_branch='trunk' should call github.get_branch_sha
    with 'trunk', never with 'main' (the global default).
    """
    from app import shared_state

    shared_state.runtime_config["smoketest_enabled"] = False

    project, version = await _make_project_and_version_custom_branches(
        db_session,
        integration_branch="trunk",
        promotion_target="release",
        phase="stabilize",
    )

    candidate = "trunk1ca" * 5  # 40-char placeholder SHA

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )

    branch_sha_calls: list[str] = []

    async def _branch_sha(token, owner, repo, branch, **kwargs):
        branch_sha_calls.append(branch)
        return candidate

    mocker.patch("app.github.get_branch_sha", new=AsyncMock(side_effect=_branch_sha))
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(return_value={"status": "completed", "conclusion": "success"}),
    )

    result = await rq.run_qualification(db_session, project.id, version)
    await db_session.commit()

    assert result["status"] == "spawned"
    assert result["candidate_sha"] == candidate

    # The integration_branch snapshot call must use 'trunk', not 'main'.
    assert "trunk" in branch_sha_calls, (
        f"Expected get_branch_sha call for 'trunk' but got calls: {branch_sha_calls}"
    )
    assert "main" not in branch_sha_calls, (
        "Got unexpected call for 'main' — should use per-project integration_branch"
    )


@pytest.mark.asyncio
async def test_smoketest_payload_uses_project_integration_branch(db_session, mocker):
    """The qualifier-smoketest payload's base_branch must equal the project's
    configured integration_branch, not the global runtime_config default.

    A project with integration_branch='trunk' should produce a smoketest task
    whose payload.base_branch == 'trunk'.
    """
    from app import shared_state

    shared_state.runtime_config["smoketest_enabled"] = True

    project, version = await _make_project_and_version_custom_branches(
        db_session,
        integration_branch="trunk",
        promotion_target="release",
        phase="stabilize",
    )

    candidate = "smktrunk" * 5  # 40-char SHA

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
        new=AsyncMock(return_value={"status": "completed", "conclusion": "success"}),
    )

    result = await rq.run_qualification(db_session, project.id, version)
    await db_session.commit()

    assert result["status"] == "spawned"
    assert result["smoketest_enabled"] is True

    tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    by_type = {(t.first_payload or {}).get("type"): t for t in tasks}
    smoke_task = by_type.get("smoketest")
    assert smoke_task is not None, "Smoketest task was not spawned"

    # base_branch must be the per-project integration_branch, not 'main'.
    assert smoke_task.first_payload["base_branch"] == "trunk", (
        f"Expected base_branch='trunk' but got {smoke_task.first_payload['base_branch']!r}"
    )


@pytest.mark.asyncio
async def test_candidate_already_on_main_uses_project_promotion_target(
    db_session, mocker
):
    """_candidate_already_on_main must check ancestry against the project's
    configured promotion_target, not the global runtime_config default.

    A project with promotion_target='release' should call github.is_ancestor_of
    with 'release' as the descendant branch, not 'main'.
    """
    project, version = await _make_project_and_version_custom_branches(
        db_session,
        integration_branch="trunk",
        promotion_target="release",
        phase="build",
    )

    candidate = "chkancst" * 5  # 40-char SHA

    ancestor_calls: list[str] = []

    async def _is_ancestor(token, owner, repo, sha, branch, **kwargs):
        ancestor_calls.append(branch)
        return True

    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(side_effect=_is_ancestor))

    result = await rq._candidate_already_on_main(
        "gh-token",
        "acme",
        "custom",
        candidate,
        version,
        project=project,
    )

    assert result is True
    assert ancestor_calls == ["release"], (
        f"Expected is_ancestor_of called with 'release' but got: {ancestor_calls}"
    )


@pytest.mark.asyncio
async def test_reconcile_promotion_failed_uses_project_promotion_target(
    db_session, mocker
):
    """When a promotion_failed version is reconciled (candidate landed on main
    out-of-band), promoted_sha is resolved from the project's promotion_target
    branch, not the global runtime_config default.

    A project with promotion_target='release' should call get_branch_sha with
    'release' to resolve the promoted merge commit SHA.
    """
    project, version = await _make_project_and_version_custom_branches(
        db_session,
        integration_branch="trunk",
        promotion_target="release",
        phase="build",
    )

    candidate_sha = "rcnclfld" * 5  # 40 chars
    release_head_sha = "rlsehead" * 5  # 40 chars

    version.last_qualification = {
        "status": "promotion_failed",
        "candidate_sha": candidate_sha,
        "pr_url": "https://github.com/acme/custom/pull/42",
        "error": "merge_blocked",
        "confidence": 0.9,
        "sub_scores": {},
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    # Candidate is now on the promotion_target branch (operator merged it).
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=True))
    # PR has no migration files — post-merge check returns "ok" immediately.
    mocker.patch(
        "app.github.get_pr_files",
        new=AsyncMock(return_value=[{"filename": "backend/app/foo.py"}]),
    )

    branch_sha_calls: list[str] = []

    async def _branch_sha(token, owner, repo, branch, **kwargs):
        branch_sha_calls.append(branch)
        return release_head_sha

    mocker.patch("app.github.get_branch_sha", new=AsyncMock(side_effect=_branch_sha))
    mocker.patch("app.github.create_tag", new=AsyncMock(return_value=None))

    count = await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )
    await db_session.commit()

    assert count == 1

    await db_session.refresh(version)
    assert version.status == "promoted"
    assert version.promoted_sha == release_head_sha

    # promoted_sha must be resolved from the per-project promotion_target.
    assert "release" in branch_sha_calls, (
        f"Expected get_branch_sha call for 'release' but got: {branch_sha_calls}"
    )
    assert "main" not in branch_sha_calls, (
        "Got unexpected 'main' branch call — should use per-project promotion_target"
    )


# ===========================================================================
# Non-self qualify -> promote -> reconcile tail — the promotion MERGE step and
# the pending-auto-merge reconciliation path over a custom branch pair.
# ===========================================================================


@pytest.mark.asyncio
async def test_finalize_promote_merges_over_custom_branch_pair(db_session, mocker):
    """End-to-end qualify -> promote -> merge for a non-self project with a
    custom integration/promotion branch pair.

    The other custom-branch tests cover the candidate snapshot and the ancestry
    check, but the actual promotion MERGE step (open + merge the
    release-branch -> promotion-target PR) was only ever exercised with the
    hardcoded automain/main fixture.  This drives a confident candidate through
    ``_finalize_qualification`` -> ``promote`` and asserts the PR is opened from
    the release branch into the project's configured ``promotion_target``
    (resolved via the per-project branch resolver, NOT a hardcoded 'main'),
    merged with ``merge_method='merge'``, and the version is promoted.
    """
    from app import shared_state
    from app.branch_resolution import get_project_promotion_target

    shared_state.runtime_config["promotion_enabled"] = True

    project, version = await _make_project_and_version_custom_branches(
        db_session,
        integration_branch="develop",
        promotion_target="staging",
        phase="stabilize",
    )
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
    merge_sha = "stgmerge" * 5  # 40-char post-merge promotion-target HEAD

    async def _branch_sha(token, owner, repo, branch, **kwargs):
        return merge_sha

    mocker.patch("app.github.get_branch_sha", new=AsyncMock(side_effect=_branch_sha))
    # is_ancestor_of=False on the idempotency pre-check: the candidate is NOT yet
    # on the promotion target, so promote() proceeds to open + merge a real PR.
    _patch_promote_github_defaults(mocker)
    mocker.patch("app.github.create_branch", new=_create_branch_mock())
    create_mock = mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"number": 42, "html_url": "http://pr/42"}),
    )
    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(return_value={"merged": True, "message": "Merged"}),
    )

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    assert result["status"] == "promoted"
    # The PR head is the release branch; its base is the per-project promotion
    # target — resolved through the branch resolver, never a hardcoded 'main'.
    assert create_mock.call_args.kwargs["head"] == "release-v1"
    assert (
        create_mock.call_args.kwargs["base"]
        == get_project_promotion_target(project)
        == "staging"
    )
    merge_mock.assert_awaited_once()
    assert merge_mock.call_args.kwargs["merge_method"] == "merge"

    await db_session.refresh(version)
    assert version.status == "promoted"
    assert version.promoted_sha == merge_sha


@pytest.mark.asyncio
async def test_promote_inert_when_integration_equals_promotion_target(
    db_session, mocker
):
    """Single-branch external repo (integration_branch == promotion_target).

    This is the common default when onboarding an external repo.  promote() has
    an explicit single-branch guard (mirroring the back-merge guard in
    hotfix_backmerge.reconcile_main_into_automain): when the integration branch
    IS the promotion target it records an inert, already-landed success and
    returns BEFORE any GitHub I/O — without opening a release branch, opening a
    PR, merging, or even running the idempotency ancestry check.  Documents that
    the promote tail is genuinely inert for this configuration via that explicit
    guard, not merely by accident of the ancestry short-circuit.
    """
    project, version = await _make_project_and_version_custom_branches(
        db_session,
        integration_branch="main",
        promotion_target="main",
        phase="stabilize",
    )
    candidate = "snglbrnc" * 5  # already on the shared single branch

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    # integration == promotion: the explicit single-branch guard fires first, so
    # the ancestry check must never be reached.  Track is_ancestor_of calls to
    # prove the guard short-circuits ahead of any GitHub I/O.
    ancestor_calls: list[str] = []

    async def _is_ancestor(token, owner, repo, sha, branch, **kwargs):
        ancestor_calls.append(branch)
        return True

    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(side_effect=_is_ancestor))
    mocker.patch("app.github.delete_branch", new=AsyncMock(return_value=True))
    mocker.patch(
        "app.github.create_tag", new=AsyncMock(return_value={"ref": "refs/tags/v1"})
    )
    branch_mock = mocker.patch("app.github.create_branch", new=_create_branch_mock())
    create_mock = mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"number": 1, "html_url": "http://pr/1"}),
    )
    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(return_value={"merged": True}),
    )

    result = await rq.promote(
        db_session,
        version,
        candidate,
        composite={"confidence": 0.95},
        base_record={"candidate_sha": candidate},
    )
    await db_session.commit()

    # Inert promote tail: recorded as an already-landed success with NO branch,
    # PR, or merge against the shared single branch.
    assert result["status"] == "promoted"
    branch_mock.assert_not_called()
    create_mock.assert_not_called()
    merge_mock.assert_not_called()
    # The explicit single-branch guard returns before any GitHub I/O, so the
    # idempotency ancestry check is never reached.
    assert ancestor_calls == []

    await db_session.refresh(version)
    assert version.status == "promoted"
    assert version.last_qualification["status"] == "promoted"
    assert version.last_qualification.get("already_landed") is True
    # The guard marks the LTS model inactive for this single-branch repo.
    assert version.last_qualification.get("lts_inactive") is True


@pytest.mark.asyncio
async def test_reconcile_pending_auto_merge_pr_closed_without_merge(db_session, mocker):
    """_reconcile_if_pending_auto_merge_landed drives a promotion_pending version
    to promotion_failed when its auto-merge PR was closed WITHOUT merging.

    This out-of-band reconciliation path (and its PR-closed-without-merge
    sub-branch) had no test coverage.  With the candidate NOT on the promotion
    target and the PR closed-unmerged, the version must move to
    promotion_failed — clearing the promotion_pending sub-state so the normal
    qualification retry machinery re-arms instead of stranding indefinitely.
    """
    project, version = await _make_project_and_version_custom_branches(
        db_session,
        integration_branch="develop",
        promotion_target="staging",
        phase="build",
    )
    candidate_sha = "pendclos" * 5  # 40 chars

    version.last_qualification = {
        "status": "pending",
        "candidate_sha": candidate_sha,
        "promotion_pending": {
            "pr_number": 55,
            "pr_url": "http://pr/55",
            "enabled_at": "2026-05-19T00:00:00",
        },
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    # Candidate is NOT on the promotion target — the PR did not land.
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=False))
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(return_value={"state": "closed", "merged": False}),
    )

    reconciled = await rq._reconcile_if_pending_auto_merge_landed(
        version.id, session_factory=_shared_session_factory(db_session)
    )

    assert reconciled is True
    await db_session.refresh(version)
    assert version.last_qualification["status"] == "promotion_failed"
    assert version.last_qualification["error"] == "pr_closed_without_merge"
    # The sub-state is cleared so the qualification backstop / retry re-arms.
    assert "promotion_pending" not in version.last_qualification


@pytest.mark.asyncio
async def test_reconcile_pending_auto_merge_pr_ci_red(db_session, mocker):
    """_reconcile_if_pending_auto_merge_landed drives a promotion_pending version
    to promotion_failed when its still-open auto-merge PR has conclusively
    failed CI.

    The CI-red sub-branch of the reconciliation path was untested.  An open PR
    whose head commit reports a completed failing build means auto-merge will
    never fire, so the version must be force-failed (clearing the
    promotion_pending sub-state) rather than left waiting forever.
    """
    project, version = await _make_project_and_version_custom_branches(
        db_session,
        integration_branch="develop",
        promotion_target="staging",
        phase="build",
    )
    candidate_sha = "pendcird" * 5  # 40 chars
    pr_head_sha = "prheadci" * 5  # 40 chars

    version.last_qualification = {
        "status": "pending",
        "candidate_sha": candidate_sha,
        "promotion_pending": {
            "pr_number": 66,
            "pr_url": "http://pr/66",
            "enabled_at": "2026-05-19T00:00:00",
        },
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=False))
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(return_value={"state": "open", "head": {"sha": pr_head_sha}}),
    )
    # Override the autouse green default: the PR head's build conclusively failed.
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(return_value={"status": "completed", "conclusion": "failure"}),
    )

    reconciled = await rq._reconcile_if_pending_auto_merge_landed(
        version.id, session_factory=_shared_session_factory(db_session)
    )

    assert reconciled is True
    await db_session.refresh(version)
    assert version.last_qualification["status"] == "promotion_failed"
    assert version.last_qualification["error"] == "pr_ci_red"
    assert "promotion_pending" not in version.last_qualification


@pytest.mark.asyncio
async def test_reconcile_pending_auto_merge_uses_project_promotion_target(
    db_session, mocker
):
    """When a pending-auto-merge version is reconciled (candidate landed on the
    promotion_target out-of-band), promoted_sha is resolved from the project's
    promotion_target branch, not the global runtime_config default.

    A project with promotion_target='release' should call get_branch_sha with
    'release' to resolve the promoted merge commit SHA, not 'main'.
    """
    project, version = await _make_project_and_version_custom_branches(
        db_session,
        integration_branch="trunk",
        promotion_target="release",
        phase="build",
    )

    candidate_sha = "pendauto" * 5  # 40 chars
    release_head_sha = "relhead2" * 5  # 40 chars

    version.last_qualification = {
        "status": "pending",
        "promotion_pending": {
            "pr_number": 42,
            "pr_url": "https://github.com/acme/custom/pull/42",
            "enabled_at": "2025-01-01T00:00:00",
        },
        "candidate_sha": candidate_sha,
        "confidence": 0.9,
        "sub_scores": {},
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    # Candidate is now on the promotion_target branch (auto-merge landed).
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=True))
    # PR has no migration files — post-merge check returns "ok" immediately.
    mocker.patch(
        "app.github.get_pr_files",
        new=AsyncMock(return_value=[{"filename": "backend/app/foo.py"}]),
    )

    branch_sha_calls: list[str] = []

    async def _branch_sha(token, owner, repo, branch, **kwargs):
        branch_sha_calls.append(branch)
        return release_head_sha

    mocker.patch("app.github.get_branch_sha", new=AsyncMock(side_effect=_branch_sha))
    mocker.patch("app.github.create_tag", new=AsyncMock(return_value=None))

    count = await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )
    await db_session.commit()

    assert count == 1

    await db_session.refresh(version)
    assert version.status == "promoted"
    assert version.promoted_sha == release_head_sha

    # promoted_sha must be resolved from the per-project promotion_target.
    assert "release" in branch_sha_calls, (
        f"Expected get_branch_sha call for 'release' but got: {branch_sha_calls}"
    )
    assert "main" not in branch_sha_calls, (
        "Got unexpected 'main' branch call — should use per-project promotion_target"
    )


def test_reaper_sweep_seam_reexported_from_release_qualifier():
    """The reaper-sweep seam lives in app.release_qualifier_sweep but its public
    symbols must stay importable/patchable as ``app.release_qualifier.<name>``.

    Guards the split-and-re-export contract: the reaper imports
    ``sweep_stuck_qualifications`` via release_qualifier, and tests patch the
    constants / helpers as ``rq.*``. A future ``ruff --fix`` dropping a re-export
    pin would silently break those callers — this test catches it.
    """
    from app import release_qualifier_sweep as rqs

    for name in (
        "sweep_stuck_qualifications",
        "_force_finalize_timed_out_qualification",
        "_force_finalize_sweep_crash_loop",
        "_parse_started_at",
        "_MAX_QUALIFICATION_DURATION_SECONDS",
        "_MAX_SWEEP_CRASH_STREAK",
    ):
        assert hasattr(rq, name), f"release_qualifier must re-export {name}"
        assert getattr(rq, name) is getattr(rqs, name), (
            f"{name} re-export must be identical to the canonical impl"
        )


# ===========================================================================
# Post-promotion serving confirmation — promotion-time stamping.
# ===========================================================================


@pytest.mark.asyncio
async def test_record_promotion_success_stamps_unconfirmed_for_self_repo(db_session):
    """A self-repo promotion is PROVISIONAL: serving_status='unconfirmed' and a
    bounded deploy_confirm_deadline are stamped at promotion time."""
    from app.release_promotion import (
        _DEPLOY_CONFIRM_WINDOW_SECONDS,
        _record_promotion_success,
    )
    from app.time_utils import _utcnow

    project = Project(
        name="Self-Repo",
        user_id="sr-user",
        github_owner="acme",
        github_repo="self",
        integration_branch="automain",
        promotion_target="main",
        is_self_repo=True,
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    version = await rv.open_version(db_session, project.id, version_number=1)
    await db_session.commit()
    await db_session.refresh(version)

    before = _utcnow()
    result = await _record_promotion_success(
        db_session,
        version,
        candidate_sha="c" * 40,
        base_record={"confidence": 0.9, "status": "promoted"},
        pr_url="http://pr/1",
        promoted_sha="p" * 40,
        lock_released=True,
    )
    await db_session.commit()

    assert result["status"] == "promoted"
    await db_session.refresh(version)
    assert version.status == "promoted"
    assert version.serving_status == "unconfirmed"
    assert version.served_sha is None
    assert version.deploy_confirm_deadline is not None
    elapsed = (version.deploy_confirm_deadline - before).total_seconds()
    # Deadline lands roughly one window from now (allow generous slack).
    assert 0 < elapsed <= _DEPLOY_CONFIRM_WINDOW_SECONDS + 120


@pytest.mark.asyncio
async def test_record_promotion_success_leaves_serving_null_for_non_self_repo(
    db_session,
):
    """A non-self-repo promotion leaves serving_status NULL (NA) — the deploy is
    not observable via the DeploymentState singleton, so the sweep never strands
    it waiting on a signal it cannot read."""
    from app.release_promotion import _record_promotion_success

    project = Project(
        name="Other-Repo",
        user_id="or-user",
        github_owner="acme",
        github_repo="other",
        integration_branch="automain",
        promotion_target="main",
        is_self_repo=False,
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    version = await rv.open_version(db_session, project.id, version_number=1)
    await db_session.commit()
    await db_session.refresh(version)

    result = await _record_promotion_success(
        db_session,
        version,
        candidate_sha="c" * 40,
        base_record={"confidence": 0.9, "status": "promoted"},
        pr_url="http://pr/1",
        promoted_sha="p" * 40,
        lock_released=True,
    )
    await db_session.commit()

    assert result["status"] == "promoted"
    await db_session.refresh(version)
    assert version.status == "promoted"
    assert version.serving_status is None
    assert version.deploy_confirm_deadline is None


@pytest.mark.asyncio
async def test_sweep_skips_non_self_repo_promotions_without_artifact_rows(
    db_session, mocker
):
    """Non-self-repo promotions stay out of the serving-confirmation sweep when
    they have NULL serving scalars and no artifact registry participation."""
    from app.time_utils import _utcnow

    project = Project(
        name="Other-Repo",
        user_id="or-user-2",
        github_owner="acme",
        github_repo="other-2",
        integration_branch="automain",
        promotion_target="main",
        is_self_repo=False,
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    version = await rv.open_version(db_session, project.id, version_number=1)
    version.status = "promoted"
    version.promoted_at = _utcnow()
    version.promoted_sha = "p" * 40
    version.last_qualification = {"status": "promoted", "promoted_sha": "p" * 40}
    db_session.add(version)
    await db_session.commit()

    confirm = mocker.patch(
        "app.release_qualifier_sweep._confirm_serving_or_fail",
        new=AsyncMock(return_value=False),
    )

    count = await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )

    assert count == 0
    confirm.assert_not_called()
    await db_session.refresh(version)
    assert version.serving_status is None
    assert version.deploy_confirm_deadline is None


# ---------------------------------------------------------------------------
# Per-attempt evidence mirroring onto QualificationVerdict (durable history).
#
# release_qualifier overwrites ReleaseVersion.last_qualification on every
# re-qualification, so the richer per-attempt evidence (migration-safety
# verdicts, findings, gate detail, promotion-failure context) was lost there.
# These tests assert that evidence is durably mirrored onto the immutable
# QualificationVerdict row and survives a subsequent last_qualification
# overwrite.
# ---------------------------------------------------------------------------


async def _load_only_verdict(db_session, version):
    """Return the single QualificationVerdict row for ``version`` (asserts one)."""
    rows = (
        (
            await db_session.execute(
                select(QualificationVerdict)
                .where(QualificationVerdict.release_version_id == version.id)
                .order_by(QualificationVerdict.created_at)
            )
        )
        .scalars()
        .all()
    )
    return rows


@pytest.mark.asyncio
async def test_blocked_attempt_persists_evidence_on_verdict_row(db_session):
    """A not_promoted (blocked) attempt mirrors migration-safety + findings onto
    the immutable verdict row, and that evidence survives a later
    re-qualification that overwrites last_qualification."""
    from app import release_promotion as rp

    project, version = await _make_project_and_version(db_session)

    base_record = {
        "candidate_sha": "abc123",
        "confidence": 0.42,
        "threshold": 0.75,
        "blocker": "migration_unsafe",
        "status": "not_promoted",
        "migration_safety": "block",
        "migration_safety_detail": "candidate renumber-skip leaves table uncreated",
        "migration_apply_safety": "block",
        "migration_apply_safety_detail": "chain does not apply on prod schema",
        "audit_findings": [{"id": "f1", "severity": "high"}],
        "excluded_audit_findings": [{"id": "f2", "reason": "stale"}],
        "test_erosion_detail": {"deleted_test_files": ["t.py"]},
        "smoketest_detail": {"passed": False},
        "audit_reconciliation": {"reconciled": False},
    }
    version.last_qualification = dict(base_record)
    db_session.add(version)

    await rp._record_qualification_verdict(
        db_session,
        version,
        base_record,
        terminal_status="not_promoted",
        would_promote=False,
        promotion_enabled=False,
    )
    await db_session.commit()

    rows = await _load_only_verdict(db_session, version)
    assert len(rows) == 1
    row = rows[0]
    assert row.terminal_status == "not_promoted"
    assert row.migration_safety == "block"
    assert row.migration_safety_detail == (
        "candidate renumber-skip leaves table uncreated"
    )
    assert row.migration_apply_safety == "block"
    assert row.migration_apply_safety_detail == "chain does not apply on prod schema"
    assert row.audit_findings == [{"id": "f1", "severity": "high"}]
    assert row.excluded_audit_findings == [{"id": "f2", "reason": "stale"}]
    assert row.test_erosion_detail == {"deleted_test_files": ["t.py"]}
    assert row.smoketest_detail == {"passed": False}
    assert row.audit_reconciliation == {"reconciled": False}
    # Promotion-attempt columns stay NULL on a non-promotion attempt.
    assert row.promotion_pr_url is None
    assert row.promotion_conflict is None
    assert row.promotion_error_detail is None

    # A subsequent re-qualification overwrites last_qualification with a NEW
    # (e.g. now-passing) evidence set — the first verdict row must keep its own.
    next_record = {
        "candidate_sha": "def456",
        "confidence": 0.91,
        "threshold": 0.75,
        "status": "not_promoted",
        "migration_safety": "pass",
        "migration_safety_detail": "clean",
    }
    version.last_qualification = dict(next_record)
    db_session.add(version)
    await rp._record_qualification_verdict(
        db_session,
        version,
        next_record,
        terminal_status="not_promoted",
        would_promote=True,
        promotion_enabled=False,
    )
    await db_session.commit()

    rows = await _load_only_verdict(db_session, version)
    assert len(rows) == 2
    first, second = rows
    # The original blocked attempt's evidence is untouched by the overwrite.
    assert first.migration_safety == "block"
    assert first.migration_safety_detail == (
        "candidate renumber-skip leaves table uncreated"
    )
    assert first.audit_findings == [{"id": "f1", "severity": "high"}]
    # The new attempt carries its own (different) evidence.
    assert second.migration_safety == "pass"
    assert second.migration_safety_detail == "clean"


@pytest.mark.asyncio
async def test_promotion_failed_attempt_persists_promotion_context(db_session):
    """A promotion_failed attempt mirrors the merge PR url / conflict / error
    context onto the immutable verdict row alongside migration-safety."""
    from app import release_promotion as rp

    project, version = await _make_project_and_version(db_session)

    base_record = {
        "candidate_sha": "abc123",
        "confidence": 0.88,
        "threshold": 0.75,
        "status": "promotion_failed",
        "migration_safety": "pass",
        "migration_safety_detail": "clean",
        "audit_findings": [],
        # Promotion-failure context folded in by _record_promotion_failure.
        "pr_url": "https://github.com/acme/widget/pull/42",
        "conflict": True,
        "error": "merge_conflict",
        "detail": "automain → main had conflicts",
    }
    version.last_qualification = dict(base_record)
    db_session.add(version)

    await rp._record_qualification_verdict(
        db_session,
        version,
        base_record,
        terminal_status="promotion_failed",
        would_promote=True,
        promotion_enabled=True,
    )
    await db_session.commit()

    rows = await _load_only_verdict(db_session, version)
    assert len(rows) == 1
    row = rows[0]
    assert row.terminal_status == "promotion_failed"
    assert row.migration_safety == "pass"
    assert row.promotion_pr_url == "https://github.com/acme/widget/pull/42"
    assert row.promotion_conflict is True
    assert row.promotion_error_detail == "merge_conflict: automain → main had conflicts"


def test_format_verdict_for_api_surfaces_durable_evidence():
    """format_verdict_for_api exposes the per-attempt evidence columns so
    operators read the attempt's own migration-safety/findings, not just the
    latest last_qualification overwrite."""
    import uuid

    verdict = QualificationVerdict(
        project_id=uuid.uuid4(),
        release_version_id=uuid.uuid4(),
        candidate_sha="abc123",
        confidence=0.1,
        blocker="migration_unsafe",
        audit_missing_reason=None,
        would_promote=False,
        promotion_enabled=False,
        terminal_status="not_promoted",
        threshold=0.9,
        migration_safety="block",
        migration_safety_detail="renumber-skip leaves table uncreated",
        migration_apply_safety="block",
        migration_apply_safety_detail="diverged from prod schema",
        audit_findings=[{"severity": "high", "summary": "x"}],
        excluded_audit_findings=[{"severity": "low", "summary": "y"}],
        test_erosion_detail={"removed": 2},
        smoketest_detail={"passed": False},
        audit_reconciliation={"reconciled": True},
        promotion_pr_url="https://example.com/pr/7",
        promotion_conflict=True,
        promotion_error_detail="merge conflict",
    )

    out = rq.format_verdict_for_api(verdict)

    # Existing keys remain.
    assert out["candidate_sha"] == "abc123"
    assert out["blocker"] == "migration_unsafe"
    assert out["audit_missing_reason"] is None
    # New durable evidence keys.
    assert out["migration_safety"] == "block"
    assert out["migration_safety_detail"] == "renumber-skip leaves table uncreated"
    assert out["migration_apply_safety"] == "block"
    assert out["migration_apply_safety_detail"] == "diverged from prod schema"
    assert out["audit_findings"] == [{"severity": "high", "summary": "x"}]
    assert out["excluded_audit_findings"] == [{"severity": "low", "summary": "y"}]
    assert out["test_erosion_detail"] == {"removed": 2}
    assert out["smoketest_detail"] == {"passed": False}
    assert out["audit_reconciliation"] == {"reconciled": True}
    assert out["promotion_pr_url"] == "https://example.com/pr/7"
    assert out["promotion_conflict"] is True
    assert out["promotion_error_detail"] == "merge conflict"


def test_get_latest_qualification_audit_findings_prefers_verdict_row():
    """The findings helper prefers the durable verdict row's audit_findings and
    falls back to last_qualification only when no verdict is supplied."""
    import uuid

    version = rv.ReleaseVersion(
        project_id=uuid.uuid4(),
        version_number=1,
        phase="stabilize",
        status="open",
        last_qualification={"audit_findings": [{"severity": "low", "summary": "stale"}]},
    )
    verdict = QualificationVerdict(
        project_id=version.project_id,
        release_version_id=uuid.uuid4(),
        candidate_sha="abc123",
        confidence=0.1,
        would_promote=False,
        promotion_enabled=False,
        terminal_status="not_promoted",
        audit_findings=[{"severity": "high", "summary": "durable"}],
    )

    # Verdict row takes precedence.
    from_verdict = rq.get_latest_qualification_audit_findings(version, verdict)
    assert from_verdict == [{"severity": "high", "summary": "durable"}]

    # Fallback to last_qualification when no verdict carries findings.
    fallback = rq.get_latest_qualification_audit_findings(version)
    assert fallback == [{"severity": "low", "summary": "stale"}]
