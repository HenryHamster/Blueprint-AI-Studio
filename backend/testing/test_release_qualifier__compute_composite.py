# AUTO-SPLIT from test_release_qualifier.py — see refactor: split mega backend test files.
# Behavior-preserving: shared preamble lives in _release_qualifier_testhelpers.py; this file
# holds one domain slice of the original test suite.
"""Domain slice: compute_composite — pure scoring function

Split from test_release_qualifier.py."""
from _release_qualifier_testhelpers import *  # noqa: F403


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


class TestComputeCompositeCiRequired:
    """ci_required gates only definitive absent candidate CI.

    A topology-safe integration-branch candidate with a definitive
    ``no_checks`` state may fall through to the score. Pending/API-error CI
    stays unresolved, and confirmed red still blocks regardless.
    """

    def test_no_checks_not_blocking_when_ci_not_required(self):
        """ci_required=False + no_checks → promotes on score."""
        result = rq.compute_composite(
            ci_green=False,
            audit_score=1.0,
            test_erosion=1.0,
            smoketest_score=1.0,
            policy=_POLICY,
            ci_state="no_checks",
            ci_required=False,
        )
        assert result["blocker"] is None
        # Sub-scores are perfect → confidence clears the 0.75 threshold.
        assert result["promote"] is True
        assert result["confidence"] >= _POLICY["threshold"]

    @pytest.mark.parametrize("ci_state", ["pending", "api_error"])
    def test_unresolved_still_blocks_when_ci_not_required(self, ci_state):
        """ci_required=False must not mask pending/API-error CI."""
        result = rq.compute_composite(
            ci_green=False,
            audit_score=1.0,
            test_erosion=1.0,
            smoketest_score=1.0,
            policy=_POLICY,
            ci_state=ci_state,
            ci_required=False,
        )
        assert result["confidence"] == 0.0
        assert result["promote"] is False
        assert result["blocker"] == "ci_unresolved"

    def test_red_still_blocks_even_when_ci_not_required(self):
        """A confirmed red build is ALWAYS a blocker, even with ci_required=False."""
        result = rq.compute_composite(
            ci_green=False,
            audit_score=1.0,
            test_erosion=1.0,
            smoketest_score=1.0,
            policy=_POLICY,
            ci_state="red",
            ci_required=False,
        )
        assert result["confidence"] == 0.0
        assert result["promote"] is False
        assert result["blocker"] == "ci_not_green"

    def test_unresolved_still_blocks_when_ci_required_default(self):
        """ci_required defaults True → unresolved still wedges (regression guard)."""
        result = rq.compute_composite(
            ci_green=False,
            audit_score=1.0,
            test_erosion=1.0,
            smoketest_score=1.0,
            policy=_POLICY,
            ci_state="pending",
        )
        assert result["blocker"] == "ci_unresolved"

    def test_ci_not_required_does_not_mask_other_blockers(self):
        """ci_required=False relaxes ONLY the CI dimension — a deleted test file
        (test_erosion is None) still hard-blocks via its own gate."""
        result = rq.compute_composite(
            ci_green=False,
            audit_score=1.0,
            test_erosion=None,
            smoketest_score=1.0,
            policy=_POLICY,
            ci_state="no_checks",
            ci_required=False,
        )
        assert result["promote"] is False
        assert result["blocker"] == "test_file_deleted"


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
        """With active 4/7 + 3/7 weights, 0.5 audit yields 5/7."""
        result = rq.compute_composite(
            ci_green=True,
            audit_score=0.5,
            test_erosion=1.0,
            smoketest_score=1.0,
            policy=_POLICY,
        )
        assert result["confidence"] == pytest.approx(round(5 / 7, 4))
        assert result["promote"] is False

    def test_weights_are_applied_per_dimension(self):
        """Audit carries 4/7 weight: a 0.0 audit caps confidence at 3/7."""
        result = rq.compute_composite(
            ci_green=True,
            audit_score=0.0,
            test_erosion=1.0,
            smoketest_score=1.0,
            policy=_POLICY,
        )
        assert result["confidence"] == pytest.approx(round(3 / 7, 4))
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


class TestComputeCompositeQualifierSmokeRetired:
    def test_legacy_smoke_arguments_are_inert(self):
        common = dict(ci_green=True, audit_score=1.0, test_erosion=1.0, policy=_POLICY)
        missing = rq.compute_composite(**common, smoketest_score=None, smoketest_enabled=True)
        failed = rq.compute_composite(**common, smoketest_score=0.0, smoketest_enabled=True)
        passed = rq.compute_composite(**common, smoketest_score=1.0, smoketest_enabled=False)
        assert missing == failed == passed
        assert "smoketest" not in passed["sub_scores"]

def test_blocker_labels_have_emitters():
    """Every ``_BLOCKER_LABELS`` key must be a blocker some code path can emit.

    A label for a blocker nothing emits is DEAD operator-facing vocabulary: it
    reads to an operator (and to the strategic agent, which is handed these
    labels) as a live gate that can fire, when in fact the gate behind it was
    retired.  ``uncorroborated_attestation`` was exactly that — its gate was
    replaced by the per-category goal-verification gate (which blocks as
    ``criterion_coverage_incomplete``) but the label outlived the emitter.

    The emitters are the ``blocker = "..."`` assignments in
    ``release_scoring`` plus the explicit ``{"blocker": "..."}`` product-probe
    verdicts in ``release_qualifier``. This is the REVERSE direction of
    ``test_every_compute_composite_blocker_code_is_classified`` in
    test_release_scoring.py, which pins emitter → classification; together they
    keep the vocabulary and the gates in lockstep in both directions.
    """
    import re
    from pathlib import Path

    scoring_src = Path(rs.__file__).read_text()
    qualifier_src = Path(rq.__file__).read_text()
    emitted = set(re.findall(r'blocker = "([a-z0-9_]+)"', scoring_src))
    emitted.update(
        re.findall(r'"blocker"\s*:\s*"([a-z0-9_]+)"', qualifier_src)
    )
    assert emitted, "expected to find blocker emitters in release code"

    dead = sorted(set(rq._BLOCKER_LABELS) - emitted)
    assert not dead, (
        f"_BLOCKER_LABELS has labels for blockers nothing emits: {dead}. "
        "Either wire up the emitter or delete the dead vocabulary."
    )


def test_boot_prereq_missing_has_operator_label():
    """The Slice-3 deployed-side env-preflight blocker carries an operator label."""
    label = rq._BLOCKER_LABELS.get("boot_prereq_missing")
    assert label is not None
    assert "deploy-target" in label
    # Presence-only framing: the label instructs binding secrets, not reading values.
    assert "bind" in label.lower()


def test_walkthrough_contract_failed_label_is_deployed_side():
    """The walkthrough report-contract blocker labels the fix as DEPLOYED-side.

    The strategist agent is handed these labels: a report-key validation failure
    is resolved by the DEPLOYED qualifier, so the label must direct the operator
    to a deploy rather than suggesting an automain fix clears it.
    """
    label = rq._BLOCKER_LABELS.get("walkthrough_e2e_contract_failed")
    assert label is not None
    assert "DEPLOYED" in label
    assert "inert until this version itself promotes" in label
    # Must NOT suggest the candidate-side "a fix on automain clears it" story.
    assert "clears it on the next" not in label
    assert "a fix on automain is inert" in label
    # The classification agrees with the label.
    assert (
        rq.blocker_execution_site("walkthrough_e2e_contract_failed")
        == rs.EXECUTION_SITE_DEPLOYED
    )
    assert rq.blocker_execution_site_label(
        "walkthrough_e2e_contract_failed"
    ) == rs.EXECUTION_SITE_LABELS[rs.EXECUTION_SITE_DEPLOYED]


def test_deployed_env_presence_snapshot_is_presence_only(monkeypatch):
    """The deploy-target snapshot reads ONLY bool(os.getenv) — never a value.

    Presence-only HARD RULE (design 245212a4): a candidate-declared secret's
    VALUE must never be read, compared, logged, or persisted.  The snapshot must
    be a ``{name: bool}`` map, and a real secret value bound in the process env
    must never leak into it.
    """
    SECRET = "super-secret-fernet-value-xyz"
    monkeypatch.setenv("GITHUB_PAT_ENCRYPTION_KEY", SECRET)
    monkeypatch.setenv("K_REVISION", "prod-rev-1")
    monkeypatch.delenv("SOME_ABSENT_VAR", raising=False)

    artifact = {
        "required_env": [
            {"name": "GITHUB_PAT_ENCRYPTION_KEY", "scope": "cloud_run"},
            {"name": "SOME_ABSENT_VAR", "scope": "always"},
        ]
    }
    snap = rq._deployed_env_presence_snapshot(artifact)

    # Every value is a bool — never the underlying env value.
    assert all(isinstance(v, bool) for v in snap.values())
    assert snap["GITHUB_PAT_ENCRYPTION_KEY"] is True
    assert snap["SOME_ABSENT_VAR"] is False
    # The Cloud Run signal is always snapshotted so the scope can be derived.
    assert snap["K_REVISION"] is True
    # The real secret value never appears anywhere in the snapshot.
    assert SECRET not in repr(snap)


def test_deployed_env_presence_snapshot_handles_absent_manifest():
    # None / malformed artifact still returns at least the Cloud Run signal,
    # never raises.
    from app.required_runtime_env import CLOUD_RUN_SIGNAL_ENV

    snap = rq._deployed_env_presence_snapshot(None)
    assert CLOUD_RUN_SIGNAL_ENV in snap
    assert all(isinstance(v, bool) for v in snap.values())


def test_uncorroborated_attestation_is_fully_retired():
    """The retired attestation gate leaves NO vocabulary behind.

    Named regression for the dead-blocker-vocabulary finding: the code must be
    absent from BOTH surfaces that described it — the operator-facing label map
    and the execution-site map — and it must stay un-emitted.
    """
    assert "uncorroborated_attestation" not in rq._BLOCKER_LABELS
    assert "uncorroborated_attestation" not in rs.BLOCKER_EXECUTION_SITE
    assert rs.blocker_execution_site("uncorroborated_attestation") is None

# ===========================================================================
# compute_composite — debt-pressure signal (observe mode)
# ===========================================================================


class TestComputeCompositeDebtPressure:
    """Observe-mode debt-pressure signal: debt_pressure appears in sub_scores
    without altering confidence or promote when no completion_debt weight is
    set in the policy (the default)."""

    def test_completion_debt_pressure_recorded_in_observe_mode(self):
        """Observe mode: debt_pressure_score is recorded in sub_scores but
        confidence and promote are identical to a call without the score."""
        # Baseline: same inputs, no debt_pressure_score passed.
        base = rq.compute_composite(
            ci_green=True,
            audit_score=0.8,
            test_erosion=1.0,
            smoketest_score=1.0,
            policy=_POLICY,
        )
        # With debt pressure — policy has no completion_debt weight → observe mode.
        with_debt = rq.compute_composite(
            ci_green=True,
            audit_score=0.8,
            test_erosion=1.0,
            smoketest_score=1.0,
            policy=_POLICY,
            debt_pressure_score=0.2,
        )
        # Observe mode: confidence and promote are unchanged.
        assert with_debt["confidence"] == base["confidence"]
        assert with_debt["promote"] == base["promote"]
        assert with_debt["blocker"] is None
        # debt_pressure sub-score is recorded for observability.
        assert "debt_pressure" in with_debt["sub_scores"]
        assert with_debt["sub_scores"]["debt_pressure"] == pytest.approx(0.2)
        # Baseline call without the score has no debt_pressure key.
        assert "debt_pressure" not in base["sub_scores"]

    def test_debt_pressure_none_omits_key_from_sub_scores(self):
        """When debt_pressure_score is None (debt module unavailable), the
        debt_pressure key is absent from sub_scores."""
        result = rq.compute_composite(
            ci_green=True,
            audit_score=1.0,
            test_erosion=1.0,
            smoketest_score=1.0,
            policy=_POLICY,
        )
        assert "debt_pressure" not in result["sub_scores"]

    def test_debt_pressure_recorded_even_when_blocker_fires(self):
        """debt_pressure_score appears in sub_scores even when a hard blocker
        forces confidence to zero — consistent with how other sub-scores work."""
        result = rq.compute_composite(
            ci_green=False,
            audit_score=1.0,
            test_erosion=1.0,
            smoketest_score=1.0,
            policy=_POLICY,
            debt_pressure_score=0.5,
        )
        assert result["blocker"] == "ci_not_green"
        assert result["confidence"] == 0.0
        assert "debt_pressure" in result["sub_scores"]
        assert result["sub_scores"]["debt_pressure"] == pytest.approx(0.5)

    def test_debt_pressure_blended_when_policy_weight_set(self):
        """When completion_debt_weight > 0 in policy, the score is blended:
        confidence = (1 - w) * existing + w * debt_pressure."""
        policy_with_debt = {
            "weights": {
                "audit": 0.40,
                "test_erosion": 0.30,
                "smoketest": 0.30,
                "completion_debt": 0.1,
            },
            "threshold": 0.75,
        }
        result = rq.compute_composite(
            ci_green=True,
            audit_score=1.0,
            test_erosion=1.0,
            smoketest_score=1.0,
            policy=policy_with_debt,
            debt_pressure_score=0.0,  # zero pressure (high debt)
        )
        # existing_confidence = 1.0; blended = 0.9 * 1.0 + 0.1 * 0.0 = 0.9
        assert result["confidence"] == pytest.approx(0.9)
        assert result["promote"] is True  # 0.9 >= 0.75

    def test_project_actionable_open_preferred_over_total_open(self):
        """Scoring-only project_actionable_open overrides deployment-wide total_open."""
        assert rq._score_completion_debt_pressure(
            {"total_open": 10, "project_actionable_open": 0}
        ) == pytest.approx(1.0)
        assert rq._score_completion_debt_pressure({"total_open": 10}) == pytest.approx(
            0.5
        )

    def test_global_standing_debt_does_not_block_passing_candidate_on_enforce(self):
        """NAMED ACCEPTANCE TEST — global debt does not drag enforce-mode promotion.

        A 0.92 candidate should remain above the 0.90 threshold when the only
        open debt is deployment-global standing debt.  The scoring snapshot may
        still report total_open=10 for visibility, but the blend must use the
        project-actionable count of zero.
        """
        policy_with_default_enforce_debt = {
            "weights": {
                "audit": 0.40,
                "test_erosion": 0.30,
                "smoketest": 0.30,
                "completion_debt": rq.DEFAULT_ENFORCE_COMPLETION_DEBT_WEIGHT,
            },
            "threshold": 0.90,
        }
        debt_pressure = rq._score_completion_debt_pressure(
            {"total_open": 10, "project_actionable_open": 0}
        )

        result = rq.compute_composite(
            ci_green=True,
            audit_score=0.92,
            test_erosion=0.92,
            smoketest_score=0.92,
            policy=policy_with_default_enforce_debt,
            debt_pressure_score=debt_pressure,
        )

        assert debt_pressure == pytest.approx(1.0)
        assert result["confidence"] == pytest.approx(0.932)
        assert result["promote"] is True


# ===========================================================================
# completion_debt_weight_mode — the graduation gate that makes the debt-pressure
# blend reachable end-to-end (Defect 1 + Defect 2).
# ===========================================================================


class TestCompletionDebtWeightModeGate:
    """The completion_debt weight is READ + PRESERVED through resolution and the
    completion_debt_weight_mode knob gates whether it reaches compute_composite."""

    @staticmethod
    def _seed_runtime_config(mocker, *, weights, mode):
        mocker.patch.dict(
            rq._runtime_config,
            {
                "promotion_threshold": 0.90,
                "promotion_weights": weights,
                "completion_debt_weight_mode": mode,
            },
            clear=False,
        )

    def test_completion_debt_weight_mode_enforce_reachable(self, mocker):
        """NAMED ACCEPTANCE TEST — end-to-end enforce reachability.

        With completion_debt_weight_mode='enforce' AND a completion_debt weight
        configured in runtime_config, the resolved policy MUST carry a nonzero
        completion_debt weight (Defect 1: the tier no longer strips it) and the
        enforce blend in compute_composite MUST actually be reached — confidence
        under enforce differs from confidence under observe for the same debt
        pressure (Defect 2: the mode gate arms the blend)."""
        weights = {
            "audit": 0.40,
            "test_erosion": 0.30,
            "smoketest": 0.30,
            "completion_debt": 0.2,
        }
        # --- enforce: the configured completion_debt weight survives resolution
        self._seed_runtime_config(mocker, weights=weights, mode="enforce")
        enforce_policy = rq._load_policy()
        assert enforce_policy["weights"].get("completion_debt") == pytest.approx(0.2), (
            "enforce mode must preserve the configured completion_debt weight so "
            "the compute_composite blend is reachable"
        )
        enforce_result = rq.compute_composite(
            ci_green=True,
            audit_score=1.0,
            test_erosion=1.0,
            smoketest_score=1.0,
            policy=enforce_policy,
            debt_pressure_score=0.0,  # maximum open debt
        )
        # existing_confidence = 1.0; blended = 0.8 * 1.0 + 0.2 * 0.0 = 0.8
        assert enforce_result["confidence"] == pytest.approx(0.8)

        # --- observe: the SAME configured weight is forced to 0.0 (dropped)
        self._seed_runtime_config(mocker, weights=weights, mode="observe")
        observe_policy = rq._load_policy()
        assert "completion_debt" not in observe_policy["weights"], (
            "observe mode must force the effective completion_debt weight to 0.0"
        )
        observe_result = rq.compute_composite(
            ci_green=True,
            audit_score=1.0,
            test_erosion=1.0,
            smoketest_score=1.0,
            policy=observe_policy,
            debt_pressure_score=0.0,
        )
        assert observe_result["confidence"] == pytest.approx(1.0)

        # Graduation actually changed the composite.
        assert enforce_result["confidence"] != pytest.approx(
            observe_result["confidence"]
        )

    def test_observe_and_off_force_effective_weight_zero(self, mocker):
        """observe/off both force the effective completion_debt weight to 0.0
        even when a nonzero weight is configured (behaviour-preserving)."""
        weights = {
            "audit": 0.40,
            "test_erosion": 0.30,
            "smoketest": 0.30,
            "completion_debt": 0.5,
        }
        for mode in ("observe", "off"):
            self._seed_runtime_config(mocker, weights=weights, mode=mode)
            policy = rq._load_policy()
            assert "completion_debt" not in policy["weights"], (
                f"mode={mode} must drop the completion_debt blend factor"
            )

    def test_enforce_with_unset_weight_falls_back_to_default(self, mocker):
        """Graduating to enforce with NO completion_debt weight configured falls
        back to the module default so graduation still changes the composite."""
        weights = {"audit": 0.40, "test_erosion": 0.30, "smoketest": 0.30}
        self._seed_runtime_config(mocker, weights=weights, mode="enforce")
        policy = rq._load_policy()
        assert policy["weights"]["completion_debt"] == pytest.approx(
            rq.DEFAULT_ENFORCE_COMPLETION_DEBT_WEIGHT
        )
        assert rq.DEFAULT_ENFORCE_COMPLETION_DEBT_WEIGHT > 0.0

    def test_enforce_weight_clamped_to_max(self, mocker):
        """A configured completion_debt weight above the cap is clamped."""
        weights = {
            "audit": 0.40,
            "test_erosion": 0.30,
            "smoketest": 0.30,
            "completion_debt": 0.9,
        }
        self._seed_runtime_config(mocker, weights=weights, mode="enforce")
        policy = rq._load_policy()
        assert policy["weights"]["completion_debt"] == pytest.approx(
            rq.MAX_COMPLETION_DEBT_WEIGHT
        )

    def test_legacy_three_key_weights_still_resolve(self, mocker):
        """Backward compatibility: a legacy 3-key promotion_weights (no
        completion_debt) resolves exactly as before, with an effective
        completion_debt weight of 0.0 (key absent) under an explicitly-pinned
        observe mode."""
        weights = {"audit": 0.40, "test_erosion": 0.30, "smoketest": 0.30}
        mocker.patch.dict(
            rq._runtime_config,
            {
                "promotion_threshold": 0.90,
                "promotion_weights": weights,
                # Pin observe explicitly (the shipped default is now "enforce").
                "completion_debt_weight_mode": "observe",
            },
            clear=False,
        )
        policy = rq._load_policy()
        assert policy["weights"] == pytest.approx(
            {"audit": 4 / 7, "test_erosion": 3 / 7}
        )
        assert policy["threshold"] == pytest.approx(0.90)


# ===========================================================================
# compute_enforce_confidence_preview — pure unit tests
# ===========================================================================


class TestComputeEnforceConfidencePreview:
    """compute_enforce_confidence_preview returns what enforce would have done."""

    def test_returns_enforce_confidence_and_effective_weight(self):
        """Basic: existing_confidence=0.9, debt=1.0, no configured weight."""
        result = rs.compute_enforce_confidence_preview(0.9, 1.0)
        w = rs.DEFAULT_ENFORCE_COMPLETION_DEBT_WEIGHT
        expected = round((1 - w) * 0.9 + w * 1.0, 4)
        assert result["enforce_confidence"] == pytest.approx(expected)
        assert result["effective_weight"] == pytest.approx(w)

    def test_configured_weight_used_when_positive(self):
        """When configured_weight > 0, it overrides the default."""
        result = rs.compute_enforce_confidence_preview(0.8, 0.5, configured_weight=0.2)
        assert result["effective_weight"] == pytest.approx(0.2)
        expected = round((1 - 0.2) * 0.8 + 0.2 * 0.5, 4)
        assert result["enforce_confidence"] == pytest.approx(expected)

    def test_zero_configured_weight_falls_back_to_default(self):
        """configured_weight=0 falls back to DEFAULT_ENFORCE_COMPLETION_DEBT_WEIGHT."""
        result_zero = rs.compute_enforce_confidence_preview(0.9, 0.5, configured_weight=0.0)
        result_none = rs.compute_enforce_confidence_preview(0.9, 0.5)
        assert result_zero["effective_weight"] == result_none["effective_weight"]
        assert result_zero["enforce_confidence"] == result_none["enforce_confidence"]

    def test_configured_weight_clamped_to_max(self):
        """Weights above MAX_COMPLETION_DEBT_WEIGHT are clamped."""
        result = rs.compute_enforce_confidence_preview(0.9, 0.5, configured_weight=0.9)
        assert result["effective_weight"] == pytest.approx(rs.MAX_COMPLETION_DEBT_WEIGHT)

    def test_zero_debt_pressure_lowers_confidence(self):
        """Zero debt pressure (max open debt) must lower enforce confidence vs observe."""
        observe_confidence = 0.95
        result = rs.compute_enforce_confidence_preview(observe_confidence, 0.0)
        assert result["enforce_confidence"] < observe_confidence

    def test_full_debt_pressure_raises_or_keeps_confidence(self):
        """Full debt pressure (no open debt) must not lower enforce confidence."""
        observe_confidence = 0.85
        result = rs.compute_enforce_confidence_preview(observe_confidence, 1.0)
        assert result["enforce_confidence"] >= observe_confidence

    def test_result_clamped_to_unit_interval(self):
        """Output is always in [0.0, 1.0]."""
        result = rs.compute_enforce_confidence_preview(1.5, -0.5)
        assert 0.0 <= result["enforce_confidence"] <= 1.0


class TestPolicyFromBuildConfig:
    """Per-project promotion rubric via ``Project.build_config`` (Fix 5): a repo
    that legitimately promotes at a different bar rides the existing build_config
    seam — no new setting, no migration — and cannot set a gate-disabling bar."""

    @staticmethod
    def _proj(build_config):
        from types import SimpleNamespace

        return SimpleNamespace(id="proj-1", build_config=build_config)

    def test_build_config_override_wins(self):
        proj = self._proj(
            {
                "promotion_threshold": 0.75,
                "promotion_weights": {
                    "audit": 0.5,
                    "test_erosion": 0.3,
                    "smoketest": 0.2,
                },
            }
        )
        policy, source, _ = rq._resolve_policy_with_provenance(proj)
        assert source == "build_config"
        assert policy["threshold"] == pytest.approx(0.75)
        assert policy["weights"]["audit"] == pytest.approx(0.625)
        assert policy["weights"]["test_erosion"] == pytest.approx(0.375)
        assert "smoketest" not in policy["weights"]
        # _load_policy threads the project through too.
        assert rq._load_policy(proj)["threshold"] == pytest.approx(0.75)

    def test_equivalent_policy_sources_produce_identical_weights_and_verdict(
        self, mocker, tmp_path
    ):
        """Legacy, YAML, runtime, and build-config 4:3 ratios are deterministic."""
        default_weights = rq._DEFAULT_POLICY["weights"]
        legacy_weights = {"audit": 0.4, "test_erosion": 0.3, "smoketest": 0.3}
        assert rq._normalize_active_promotion_weights(legacy_weights) == pytest.approx(default_weights)

        build_policy = rq._policy_from_build_config(
            self._proj({"promotion_weights": legacy_weights})
        )
        assert build_policy["weights"] == pytest.approx(default_weights)

        _point_policy_at(
            mocker,
            tmp_path,
            "weights:\n"
            "  audit: 0.5714285714285714\n"
            "  test_erosion: 0.4285714285714286\n"
            "threshold: 0.9\n",
        )
        mocker.patch.dict(
            rq._runtime_config,
            {"completion_debt_weight_mode": "observe"},
            clear=False,
        )
        yaml_policy = rq._load_policy()
        assert yaml_policy["weights"] == pytest.approx(default_weights)

        mocker.patch.dict(
            rq._runtime_config,
            {
                "promotion_threshold": 0.9,
                "promotion_weights": legacy_weights,
                "completion_debt_weight_mode": "observe",
            },
            clear=False,
        )
        runtime_policy = rq._load_policy()
        assert runtime_policy["weights"] == pytest.approx(default_weights)

        results = [
            rq.compute_composite(
                ci_green=True,
                audit_score=0.825,
                test_erosion=1.0,
                smoketest_score=smoke,
                smoketest_enabled=True,
                policy={"weights": weights, "threshold": 0.9},
            )
            for weights, smoke in (
                (legacy_weights, 0.0),
                (default_weights, None),
                (yaml_policy["weights"], 1.0),
                (runtime_policy["weights"], 0.5),
                (build_policy["weights"], None),
            )
        ]
        assert all(result == results[0] for result in results[1:])
        assert results[0]["confidence"] == pytest.approx(0.9)
        assert results[0]["promote"] is True

    def test_threshold_clamped_to_floor(self):
        # A project cannot set a near-zero bar. With default active weights (audit 4/7)
        # the threshold clamps up to the audit-veto floor (1 - audit_weight +
        # margin), which sits above the plain 0.5 safety floor.
        proj = self._proj({"promotion_threshold": 0.2})
        policy = rq._load_policy(proj)
        expected = max(
            rq._SAFE_PROMOTION_THRESHOLD_FLOOR,
            1.0 - rq._DEFAULT_POLICY["weights"]["audit"] + rq._AUDIT_VETO_MARGIN,
        )
        assert policy["threshold"] == pytest.approx(expected)
        assert policy["threshold"] >= rq._SAFE_PROMOTION_THRESHOLD_FLOOR

    def test_audit_can_always_veto(self):
        # Finding B: the audit-weight floor and threshold floor are independent,
        # so at their joint minimum a zero-audit candidate could promote. The
        # threshold clamp must guarantee that a present-but-terrible (0.0) audit
        # always fails: a 0-audit candidate scores at most (1 - audit_weight), so
        # the clamped threshold must strictly exceed that for any rubric.
        for weights in (
            {"audit": 0.2, "test_erosion": 0.4, "smoketest": 0.4},
            {"audit": 0.5, "test_erosion": 0.25, "smoketest": 0.25},
        ):
            proj = self._proj(
                {"promotion_threshold": 0.5, "promotion_weights": weights}
            )
            policy = rq._load_policy(proj)
            assert policy["threshold"] > 1.0 - policy["weights"]["audit"]

    def test_threshold_only_uses_default_weights(self, mocker):
        # Pin observe so this asserts the base two-dimension active rubric without
        # the enforce-default completion_debt overlay (covered separately).
        mocker.patch.dict(
            rq._runtime_config,
            {"completion_debt_weight_mode": "observe"},
            clear=False,
        )
        proj = self._proj({"promotion_threshold": 0.85})
        policy, source, _ = rq._resolve_policy_with_provenance(proj)
        assert source == "build_config"
        assert policy["threshold"] == pytest.approx(0.85)
        assert policy["weights"] == rq._DEFAULT_POLICY["weights"]

    def test_invalid_weights_fall_through(self):
        # Weights that don't sum to ~1.0 would break the gate math — ignored.
        proj = self._proj(
            {"promotion_weights": {"audit": 0.9, "test_erosion": 0.9, "smoketest": 0.9}}
        )
        assert rq._policy_from_build_config(proj) is None

    def test_no_override_falls_through(self):
        assert rq._policy_from_build_config(self._proj({})) is None
        assert rq._policy_from_build_config(self._proj(None)) is None
        assert rq._policy_from_build_config(None) is None

    def test_audit_weight_floor_rejects(self):
        # A rubric that zeroes/near-zeroes the audit dimension is rejected —
        # compute_composite only hard-blocks a MISSING audit, so a near-zero audit
        # weight would let a terrible-but-present audit promote.
        proj = self._proj(
            {
                "promotion_weights": {
                    "audit": 0.1,
                    "test_erosion": 0.45,
                    "smoketest": 0.45,
                }
            }
        )
        assert rq._policy_from_build_config(proj) is None

    def test_build_config_schema_persists_promotion_keys(self):
        # The review's HIGH finding: the keys must be DECLARED fields on the
        # BuildConfig schema, or model_dump (run on every save/PATCH/re-detect)
        # silently strips them and the override never persists.
        from app.schemas import BuildConfig

        dumped = BuildConfig(
            promotion_threshold=0.75,
            promotion_weights={"audit": 0.5, "test_erosion": 0.3, "smoketest": 0.2},
            smoketest_enabled=True,
        ).model_dump(exclude_none=True)
        assert dumped["promotion_threshold"] == 0.75
        assert dumped["promotion_weights"] == {
            "audit": 0.5,
            "test_erosion": 0.3,
            "smoketest": 0.2,
        }
        assert dumped["smoketest_enabled"] is True

    def test_redetect_preserves_promotion_keys(self):
        # A fresh re-detect can't recover these operator-owned keys, so
        # apply_operator_build_config_overrides must carry them forward or an
        # unrelated re-detect silently reverts the project to the fleet rubric.
        from app.build_config_detection import apply_operator_build_config_overrides

        existing = {
            "promotion_threshold": 0.7,
            "promotion_weights": {"audit": 0.5, "test_erosion": 0.3, "smoketest": 0.2},
            "smoketest_enabled": True,
        }
        merged = apply_operator_build_config_overrides({}, existing)
        assert merged["promotion_threshold"] == 0.7
        assert merged["promotion_weights"] == {
            "audit": 0.5,
            "test_erosion": 0.3,
            "smoketest": 0.2,
        }
        assert merged["smoketest_enabled"] is True

    def test_override_preserves_merge_grade_block_threshold(self, mocker):
        # A per-project threshold override must NOT drop fleet-only keys like
        # merge_grade_block_threshold (which arms the goal-coverage blocker) —
        # the override overlays the fleet policy, it does not replace it.
        mocker.patch.dict(
            rq._runtime_config,
            {
                "promotion_threshold": 0.90,
                "promotion_weights": {
                    "audit": 0.4,
                    "test_erosion": 0.3,
                    "smoketest": 0.3,
                },
                "merge_grade_block_threshold": 0.5,
            },
            clear=False,
        )
        proj = self._proj({"promotion_threshold": 0.75})
        policy, source, _ = rq._resolve_policy_with_provenance(proj)
        assert source == "build_config"
        assert policy["threshold"] == pytest.approx(0.75)
        # Inherited from the fleet policy, not dropped by the override.
        assert policy.get("merge_grade_block_threshold") == pytest.approx(0.5)


class TestLoadPolicy:
    def test_parses_real_policy_file(self, mocker):
        """The shipped promotion_policy.yaml parses to the documented values.

        Clears the live runtime_config rubric so the YAML-file tier is exercised
        directly (the seeded runtime_config holds the same values, but this test
        is specifically about the on-disk file)."""
        mocker.patch.dict(
            rq._runtime_config,
            {
                "promotion_threshold": None,
                "promotion_weights": None,
                # Pin observe: assert the on-disk active-weight shape without
                # the enforce-default completion_debt overlay.
                "completion_debt_weight_mode": "observe",
            },
            clear=False,
        )
        policy = rq._load_policy()
        assert {"weights", "threshold"} <= set(policy.keys())
        assert set(policy["weights"].keys()) == {"audit", "test_erosion"}
        # Values are coerced to float.
        for v in policy["weights"].values():
            assert isinstance(v, float)
        assert isinstance(policy["threshold"], float)
        # Matches the shipped defaults.
        assert policy["weights"]["audit"] == pytest.approx(4 / 7)
        assert policy["weights"]["test_erosion"] == pytest.approx(3 / 7)
        assert "smoketest" not in policy["weights"]
        assert policy["threshold"] == pytest.approx(0.90)

    def test_fallback_on_missing_file(self, mocker, tmp_path):
        """A missing policy file falls back to hardcoded defaults, no raise."""
        _point_policy_at(mocker, tmp_path, None)
        # Pin observe so the fallback asserts the base two-dimension active rubric without
        # the enforce-default completion_debt overlay.
        mocker.patch.dict(
            rq._runtime_config, {"completion_debt_weight_mode": "observe"}, clear=False
        )
        policy = rq._load_policy()
        assert policy["weights"] == pytest.approx(
            {"audit": 4 / 7, "test_erosion": 3 / 7}
        )
        assert policy["threshold"] == 0.90

    def test_fallback_on_malformed_yaml(self, mocker, tmp_path):
        """Malformed YAML falls back to defaults instead of raising."""
        _point_policy_at(mocker, tmp_path, "weights: {unterminated: [1, 2")
        policy = rq._load_policy()
        assert policy["threshold"] == 0.90
        assert policy["weights"]["audit"] == pytest.approx(4 / 7)

    def test_fallback_on_missing_weights_key(self, mocker, tmp_path):
        """A policy file with no 'weights' mapping falls back to defaults."""
        # Use a sentinel threshold that differs from the default so the
        # fallback (vs the file's parsed value) is unambiguous.
        _point_policy_at(mocker, tmp_path, "threshold: 0.55\n")
        policy = rq._load_policy()
        assert policy["weights"]["audit"] == pytest.approx(4 / 7)
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
        assert policy["weights"]["audit"] == pytest.approx(4 / 7)

    def test_fallback_on_scalar_yaml(self, mocker, tmp_path):
        """YAML that parses to a non-mapping (a bare scalar) falls back."""
        _point_policy_at(mocker, tmp_path, "just-a-string\n")
        policy = rq._load_policy()
        assert policy["weights"]["audit"] == pytest.approx(4 / 7)
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
        # Pin observe so the fallback asserts the base two-dimension active rubric without
        # the enforce-default completion_debt overlay.
        mocker.patch.dict(
            rq._runtime_config, {"completion_debt_weight_mode": "observe"}, clear=False
        )
        policy = rq._load_policy()
        assert policy["weights"] == pytest.approx(
            {"audit": 4 / 7, "test_erosion": 3 / 7}
        )
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
        assert policy["weights"]["audit"] == pytest.approx(4 / 7)
        assert policy["weights"]["test_erosion"] == pytest.approx(3 / 7)
        assert "smoketest" not in policy["weights"]

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
        assert policy["weights"]["audit"] == pytest.approx(2 / 3)
        assert policy["weights"]["test_erosion"] == pytest.approx(1 / 3)
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
        assert policy["weights"]["audit"] == pytest.approx(0.401 / 0.701)
        assert policy["weights"]["test_erosion"] == pytest.approx(0.3 / 0.701)

    def test_fallback_never_returns_module_constant(self, mocker, tmp_path):
        """The fallback returns a copy — mutating it must not corrupt the
        module-level _DEFAULT_POLICY for the next caller."""
        _point_policy_at(mocker, tmp_path, None)
        policy = rq._load_policy()
        policy["weights"]["audit"] = 999.0
        policy["threshold"] = 999.0
        # A fresh fallback must still see the real defaults.
        fresh = rq._load_policy()
        assert fresh["weights"]["audit"] == pytest.approx(4 / 7)
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
        assert policy["weights"]["audit"] == pytest.approx(2 / 3)
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
        assert policy["weights"]["audit"] == pytest.approx(2 / 3)

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


class TestLoadPolicyProvenance:
    def test_runtime_config_provenance_is_reported(self, mocker):
        """A valid runtime_config rubric is reported as runtime_config."""
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

        policy, source, error = rq._resolve_policy_with_provenance()
        assert source == "runtime_config"
        assert error is None
        assert policy["threshold"] == pytest.approx(0.75)

    def test_yaml_provenance_is_reported(self, mocker, tmp_path):
        """A valid YAML file is reported as yaml_file when runtime_config is unset."""
        _point_policy_at(
            mocker,
            tmp_path,
            "weights:\n  audit: 0.5\n  test_erosion: 0.25\n"
            "  smoketest: 0.25\nthreshold: 0.8\n",
        )

        policy, source, error = rq._resolve_policy_with_provenance()
        assert source == "yaml_file"
        assert error is None
        assert policy["weights"]["audit"] == pytest.approx(2 / 3)

    def test_hardcoded_defaults_provenance_includes_parse_error(self, mocker, tmp_path):
        """A broken YAML file falls back to hardcoded defaults with an error string."""
        _point_policy_at(mocker, tmp_path, "weights: {unterminated: [1, 2")
        # Pin observe so the hardcoded fallback asserts the base two-dimension active
        # rubric without the enforce-default completion_debt overlay.
        mocker.patch.dict(
            rq._runtime_config, {"completion_debt_weight_mode": "observe"}, clear=False
        )

        policy, source, error = rq._resolve_policy_with_provenance()
        assert source == "hardcoded_defaults"
        assert error is not None
        assert (
            "ScannerError" in error
            or "ParserError" in error
            or "while parsing" in error
        )
        assert policy["weights"] == pytest.approx(
            {"audit": 4 / 7, "test_erosion": 3 / 7}
        )


# ===========================================================================
# _score_audit — artifact → sub-score derivation
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

    def test_missing_findings_key_scores_unmeasured(self):
        """A report missing the findings key scores unmeasured (None), not clean.

        A report carrying NEITHER a "findings" nor an "issues" key is malformed —
        the deduction loop never runs — so scoring it a clean 1.0 fails GREEN over
        an audit that never actually surfaced findings.  It must null the score so
        compute_composite routes to the ``audit_missing`` hard blocker.  (Contrast
        ``empty_findings_list``: an explicit ``findings: []`` legitimately says the
        auditor found nothing and keeps its clean 1.0.)
        """
        report = {"audit_score": 0.5, "test_erosion": {"deleted_test_files": []}}
        audit, erosion = rq._score_audit(report)
        assert audit is None
        # test-erosion is scored independently and remains neutral.
        assert erosion == pytest.approx(1.0)

    def test_deleted_test_file_returns_none_erosion(self):
        """A deleted test file classified genuine_erosion is the None hard-blocker."""
        report = {
            "audit_score": 0.9,
            "test_erosion": {
                "deleted_test_files": ["testing/test_gone.py"],
                "assertion_delta": 0,
                "test_count_delta": -5,
            },
            "deleted_test_file_classifications": [
                {
                    "path": "testing/test_gone.py",
                    "classification": "genuine_erosion",
                    "rationale": "Subject module is present and unchanged; test removed to stop failing build.",
                }
            ],
            "findings": [],
        }
        audit, erosion = rq._score_audit(report)
        # No findings → derived audit = 1.0
        assert audit == pytest.approx(1.0)
        assert erosion is None

    def test_dead_code_classification_no_hard_block(self):
        """A dead_code_removal classification must NOT trigger the hard-blocker.

        Replaces the retired AUTHORIZED_TEST_DELETIONS allowlist: disposition is
        now driven by per-file classifications emitted by the release audit agent.
        A deleted test file classified ``dead_code_removal`` must return a
        non-None erosion score so the test_file_deleted hard-blocker does not fire.
        """
        report = {
            "audit_score": 0.9,
            "test_erosion": {
                "deleted_test_files": ["testing/test_foo.py"],
                "assertion_delta": 0,
                "test_count_delta": 0,
            },
            "deleted_test_file_classifications": [
                {
                    "path": "testing/test_foo.py",
                    "classification": "dead_code_removal",
                    "rationale": "Subject app/foo.py co-deleted in this diff; no live callers remain.",
                }
            ],
            "findings": [],
        }
        audit, erosion = rq._score_audit(report)
        # No findings → derived audit = 1.0
        assert audit == pytest.approx(1.0)
        assert erosion is not None
        assert erosion == pytest.approx(1.0)

    def test_genuine_erosion_classification_hard_blocks(self):
        """A genuine_erosion classification must produce the None hard-blocker."""
        report = {
            "audit_score": 0.9,
            "test_erosion": {
                "deleted_test_files": ["testing/test_foo.py"],
                "assertion_delta": 0,
                "test_count_delta": 0,
            },
            "deleted_test_file_classifications": [
                {
                    "path": "testing/test_foo.py",
                    "classification": "genuine_erosion",
                    "rationale": "Subject app/foo.py is present and unchanged; test removed to stop failing build.",
                }
            ],
            "findings": [],
        }
        _, erosion = rq._score_audit(report)
        assert erosion is None, (
            "An explicit genuine_erosion classification must hard-block"
        )

    def test_absent_classifications_fail_open(self):
        """A deleted test file with NO classifications entry must fail OPEN.

        Fail-open (operator steer): only an explicit ``genuine_erosion``
        classification hard-blocks.  An absent/ambiguous classification passes
        — a co-deleted/dead-code test removal must not block promotion just
        because the auditor didn't emit a tag.
        """
        report = {
            "audit_score": 0.9,
            "test_erosion": {
                "deleted_test_files": ["testing/test_foo.py"],
                "assertion_delta": 0,
                "test_count_delta": 0,
            },
            # No deleted_test_file_classifications key.
            "findings": [],
        }
        _, erosion = rq._score_audit(report)
        assert erosion is not None, (
            "An unclassified deleted test file must fail open (non-None erosion)"
        )

    def test_mixed_classifications_any_genuine_blocks(self):
        """Two deleted files where one is genuine_erosion must hard-block."""
        report = {
            "audit_score": 0.9,
            "test_erosion": {
                "deleted_test_files": [
                    "testing/test_dead.py",
                    "testing/test_alive.py",
                ],
                "assertion_delta": 0,
                "test_count_delta": 0,
            },
            "deleted_test_file_classifications": [
                {
                    "path": "testing/test_dead.py",
                    "classification": "dead_code_removal",
                    "rationale": "Subject app/dead.py co-deleted; no callers remain.",
                },
                {
                    "path": "testing/test_alive.py",
                    "classification": "genuine_erosion",
                    "rationale": "Subject app/alive.py present and unchanged.",
                },
            ],
            "findings": [],
        }
        _, erosion = rq._score_audit(report)
        assert erosion is None, (
            "Any single genuine_erosion among mixed classifications must hard-block"
        )

    def test_empty_classifications_list_fails_open(self):
        """An empty deleted_test_file_classifications list must fail OPEN.

        An empty list carries no explicit ``genuine_erosion`` tag, so under the
        fail-open disposition the deletion passes (erosion is non-None).
        """
        report = {
            "audit_score": 0.9,
            "test_erosion": {
                "deleted_test_files": ["testing/test_foo.py"],
                "assertion_delta": 0,
                "test_count_delta": 0,
            },
            "deleted_test_file_classifications": [],
            "findings": [],
        }
        _, erosion = rq._score_audit(report)
        assert erosion is not None, (
            "An empty classifications list must fail open (non-None erosion)"
        )

    def test_all_dead_code_removal_passes(self):
        """Multiple deleted test files all classified dead_code_removal must pass.

        Each file carries an explicit non-erosion authorisation, so the gate
        must return a non-None erosion score (no hard block).
        """
        report = {
            "audit_score": 0.9,
            "test_erosion": {
                "deleted_test_files": [
                    "testing/test_foo.py",
                    "testing/test_bar.py",
                ],
                "assertion_delta": 0,
                "test_count_delta": 0,
            },
            "deleted_test_file_classifications": [
                {
                    "path": "testing/test_foo.py",
                    "classification": "dead_code_removal",
                    "rationale": "app/foo.py co-deleted; no callers remain.",
                },
                {
                    "path": "testing/test_bar.py",
                    "classification": "dead_code_removal",
                    "rationale": "app/bar.py co-deleted; no callers remain.",
                },
            ],
            "findings": [],
        }
        _, erosion = rq._score_audit(report)
        assert erosion is not None, (
            "All dead_code_removal deletions must pass (non-None erosion)"
        )

    def test_allowlist_symbols_not_importable(self):
        """The retired allowlist symbols must not be re-exported from release_scoring."""
        assert not hasattr(rs, "AUTHORIZED_TEST_DELETIONS")
        assert not hasattr(rs, "_get_lifecycle_bounded_authorized_deletions")
        assert not hasattr(rs, "AuthorizedTestDeletion")

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

    def test_missing_audit_score_no_findings_scores_unmeasured(self):
        """A report with neither audit_score nor findings key scores unmeasured.

        No findings key AND no self-reported score is a malformed artifact, not a
        clean audit — it must null the score (fail closed) rather than derive a
        phantom-clean 1.0.
        """
        report = {"test_erosion": {"deleted_test_files": []}}
        audit, erosion = rq._score_audit(report)
        assert audit is None
        assert erosion == pytest.approx(1.0)

    def test_empty_findings_list_still_scores_clean(self):
        """An EXPLICIT empty findings list is a legitimately-clean audit (1.0).

        Guards the no_findings_key fix from over-reaching: an auditor that ran and
        found nothing emits ``findings: []`` and must keep its clean score, unlike
        a report that omits the findings key entirely.
        """
        report = {
            "audit_score": 1.0,
            "findings": [],
            "test_erosion": {"deleted_test_files": []},
        }
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
    """_score_audit must skip deductions for high/medium/critical findings that
    lack a substantive verification_note (≥ 20 non-whitespace chars) — and, when
    that leaves the report with NO scoreable higher-severity finding, must NULL
    the audit_score (fail-closed to the ``audit_missing`` blocker) rather than
    return a phantom-clean 1.0 over unsubstantiated criticals."""

    def test_unverified_high_finding_nulls_audit_score(self):
        """A lone high finding without verification_note is dropped, leaving the
        report unscoreable → audit_score is NULLED (was a false-clean 1.0)."""
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
        assert audit is None

    def test_unverified_medium_finding_nulls_audit_score(self):
        """A lone medium finding without verification_note → unscoreable → nulled."""
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
        assert audit is None

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
        """A verification_note with fewer than 20 non-whitespace chars is treated
        as absent — the lone critical is dropped → unscoreable → nulled."""
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
        assert audit is None

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
        """An empty verification_note is treated the same as absent — the lone
        high finding is dropped → unscoreable → nulled."""
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
        assert audit is None

    def test_all_findings_unverified_nulls_score_and_reason_fires(self):
        """An all-unverified-critical report must NOT read clean: _score_audit
        NULLs the score (fail-closed to audit_missing) AND _audit_missing_reason
        returns 'all_findings_unverified' for the observability row."""
        report = {
            "findings": [
                {
                    "severity": "critical",
                    "summary": "SQL injection.",
                    "file": "app/db.py",
                },
                {
                    "severity": "high",
                    "summary": "Auth bypass.",
                    "file": "app/auth.py",
                },
            ]
        }
        audit, _ = rq._score_audit(report)
        assert audit is None, "all-unverified criticals must not read as a clean 1.0"
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
        assert audit == pytest.approx(1.0), (
            "authorized revision-identity divergence must not deduct"
        )

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
        assert audit == pytest.approx(1.0 - 0.15), (
            "migration-body defect must still deduct"
        )

    def test_helper_matches_authorized_and_rejects_unrelated(self):
        """_finding_subject_is_authorized_divergence: matches a listed-revision
        renumber finding, rejects an unrelated finding and a same-file body defect."""
        auth = {
            "severity": "medium",
            "summary": "0143 revision-identity collision (renumber).",
            "verification_note": self._AUTH_VNOTE,
        }
        unrelated = {
            "severity": "medium",
            "summary": "Missing null check in app/api.py.",
        }
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
        """computed_test_erosion with a deleted file → None even when LLM omits it.

        The deletion is classified genuine_erosion so the hard-blocker fires off
        the computed (authoritative) deleted_test_files list, proving the gate
        reads the computed deletion even when the LLM's self-reported list missed
        it.
        """
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
            "deleted_test_file_classifications": [
                {
                    "path": "testing/test_gone.py",
                    "classification": "genuine_erosion",
                    "rationale": "Subject module present and unchanged; deletion is genuine erosion.",
                }
            ],
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
        """When computed_test_erosion is absent, the LLM test_erosion list drives the gate.

        The genuine_erosion classification confirms the deletion hard-blocks off
        the fallback (LLM-reported) deleted_test_files list.
        """
        report = {
            "test_erosion": {
                "deleted_test_files": ["testing/test_gone.py"],
                "assertion_delta": 0,
                "test_count_delta": 0,
            },
            "deleted_test_file_classifications": [
                {
                    "path": "testing/test_gone.py",
                    "classification": "genuine_erosion",
                    "rationale": "Subject module present and unchanged; deletion is genuine erosion.",
                }
            ],
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

    def test_authorized_deletion_with_zero_delta_does_not_penalize(self):
        """A dead_code_removal deletion whose removed lines are excluded from the
        delta (by the TS-side fix) yields a non-penalized test_erosion score.

        When the agent correctly excludes deleted-file lines from
        assertion_delta/test_count_delta, both deltas arrive at 0 even though
        the file itself had test content — the hard-blocker gate owns the file
        dimension, the graded penalty must see delta=0 and return 1.0.

        The deletion is explicitly authorized via a dead_code_removal
        classification — the audit agent must provide this for any non-erosion
        deletion to pass the fail-closed gate.
        """
        report = {
            "audit_score": 0.9,
            "computed_test_erosion": {
                "deleted_test_files": ["admin-ui/src/utils/viewportRegression.test.ts"],
                # TS fix: lines from the deleted file are excluded, so deltas stay 0.
                "assertion_delta": 0,
                "test_count_delta": 0,
            },
            "deleted_test_file_classifications": [
                {
                    "path": "admin-ui/src/utils/viewportRegression.test.ts",
                    "classification": "dead_code_removal",
                    "rationale": "viewport regression harness co-deleted; no callers remain.",
                }
            ],
            "findings": [],
        }
        _, erosion = rq._score_audit(report)
        # Authorized deletion — hard-blocker must NOT fire.
        assert erosion is not None, (
            "dead_code_removal classification must not produce the None hard-blocker"
        )
        # Zero deltas — graded penalty must not fire.
        assert erosion == pytest.approx(1.0), (
            "Zero deltas after authorized deletion must yield an unpenalized score of 1.0"
        )

    def test_genuine_erosion_deleted_root_level_test_file_hard_blocks(self):
        """A root-level test deletion classified genuine_erosion hard-blocks.

        Regression pin: 'testing/test_root_level.py' (no leading slash) is the
        path shape 'git diff --name-status' emits for a root-level testing/
        directory deletion.  When the release audit agent classifies it as
        genuine_erosion, _score_audit must return erosion=None regardless of the
        path shape.
        """
        report = {
            "audit_score": 0.9,
            "computed_test_erosion": {
                # Path with no leading slash — as emitted by 'git diff --name-status'
                # for a root-level testing/ directory.
                "deleted_test_files": ["testing/test_root_level.py"],
                "assertion_delta": 0,
                "test_count_delta": 0,
            },
            "deleted_test_file_classifications": [
                {
                    "path": "testing/test_root_level.py",
                    "classification": "genuine_erosion",
                    "rationale": "Subject module present and unchanged; deletion is genuine erosion.",
                }
            ],
            "findings": [],
        }
        _, erosion = rq._score_audit(report)
        assert erosion is None, (
            "A genuine_erosion-classified deleted test file must hard-block (erosion=None)"
        )


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

    def test_a_verified_low_does_not_rescue_unverified_higher_severity(self):
        """A trusted low finding does NOT make an all-unverified higher-severity
        report scoreable.

        The fail-closed rule is about the HIGHER-severity findings being
        unsubstantiated; a 0.02 style-nit deduction is orthogonal noise and must
        not launder an unverified medium/high into a near-clean score.  So the
        reason still fires and _score_audit still NULLs the score, even though the
        deduction loop did move ``derived`` off 1.0."""
        report = {
            "findings": [
                {"severity": "medium", "summary": "Row-vs-ORM false positive."},
                {"severity": "low", "summary": "Minor style issue."},
            ]
        }
        assert rq._audit_missing_reason(report) == "all_findings_unverified"
        audit_score, _ = rq._score_audit(report)
        assert audit_score is None, (
            "a lone verified low must not rescue an unverified higher-severity report"
        )


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
            {
                "severity": "low",
                "summary": "Private import.",
                "key": rq._audit_finding_key("Private import.", None),
            },
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
        assert out == [
            {
                "severity": "high",
                "summary": "Token baked into image.",
                "key": rq._audit_finding_key("Token baked into image.", None),
            }
        ]

    def test_gate_agrees_with_score_audit_for_unverified(self):
        """When _score_audit treats a finding as undeducted (unverified),
        _extract_audit_findings must also exclude it.

        The report carries a VERIFIED high so it stays scoreable: without it,
        every higher-severity finding would be unverified and _score_audit would
        fail-closed to None (``all_findings_unverified``), which is a different
        contract — pinned separately in TestAuditMissingReason — and would hide
        the extract/score agreement this test is about."""
        report = {
            "findings": [
                # Verified high: deducted by _score_audit, carried by extract.
                # Keeps the report scoreable (not all higher-sev are unverified).
                {
                    "severity": "high",
                    "summary": "Hardcoded credential in config.",
                    "verification_note": "Read app/config.py lines 10-25: API key hardcoded in a module-level constant.",
                },
                # Medium without verification_note: _score_audit skips deduction.
                {"severity": "medium", "summary": "Row-vs-ORM false positive."},
                # Low always counted by both functions.
                {"severity": "low", "summary": "Minor style issue."},
            ]
        }
        # _score_audit must NOT deduct for the unverified medium — only the
        # verified high and the (always-trusted) low.
        audit_score, _ = rq._score_audit(report)
        assert audit_score == pytest.approx(
            1.0 - rq._AUDIT_DEDUCTIONS["high"] - rq._AUDIT_DEDUCTIONS["low"]
        ), "_score_audit should only deduct for the verified high and the low finding"
        # _extract_audit_findings must agree: medium excluded, high + low present.
        out = rq._extract_audit_findings(report)
        severities = [f["severity"] for f in out]
        assert "medium" not in severities, (
            "unverified medium finding must not appear in carried findings"
        )
        assert "high" in severities, "verified high finding must be carried"
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
        assert out == [
            {
                "severity": "unknown",
                "summary": "a plain string",
                "key": rq._audit_finding_key("a plain string", None),
            }
        ]

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

    def test_extract_audit_findings_carries_file_and_stable_key(self):
        """Carried findings include file attribution and a deterministic,
        content-derived key that is stable across re-extractions.

        Acceptance guard for the stable-identity feature: a fix-first
        resolution ledger needs each carried finding to (a) name the file it
        targets and (b) carry a stable key so the same finding maps to the same
        ledger entry across re-extractions.
        """
        report = {
            "findings": [
                {
                    "severity": "high",
                    "summary": "SQL injection in claim handler.",
                    "file": "backend/app/claim_task_helpers.py",
                    "verification_note": (
                        "Read backend/app/claim_task_helpers.py lines 40-60: "
                        "raw f-string interpolated into the WHERE clause."
                    ),
                },
                {"severity": "low", "summary": "Unused import."},
            ]
        }
        out = rq._extract_audit_findings(report)
        by_summary = {f["summary"]: f for f in out}

        injected = by_summary["SQL injection in claim handler."]
        # (a) file attribution carried from the raw finding.
        assert injected["file"] == "backend/app/claim_task_helpers.py"
        # (b) every carried finding has a key.
        assert all("key" in f for f in out)
        # The keyed value is the deterministic content hash of file|summary.
        assert injected["key"] == rq._audit_finding_key(
            "SQL injection in claim handler.",
            "backend/app/claim_task_helpers.py",
        )
        # Findings without a raw file carry no file but still get a key.
        unused = by_summary["Unused import."]
        assert "file" not in unused
        assert unused["key"] == rq._audit_finding_key("Unused import.", None)

        # Key is STABLE across re-extractions of the same report.
        out2 = rq._extract_audit_findings(report)
        assert [f["key"] for f in out] == [f["key"] for f in out2]

        # Same summary, DIFFERENT file → different key (file participates).
        assert rq._audit_finding_key("dup summary", "a.py") != rq._audit_finding_key(
            "dup summary", "b.py"
        )
        # Same content → same key.
        assert rq._audit_finding_key("dup summary", "a.py") == rq._audit_finding_key(
            "dup summary", "a.py"
        )

    def test_extract_audit_findings_carries_blob_keyed_surface_identity(self):
        report = {
            "findings": [
                {
                    "severity": "low",
                    "summary": "Leaky cache lifetime.",
                    "file": "backend/app/cache.py",
                    "blob_sha": "b" * 40,
                }
            ]
        }
        finding = rq._extract_audit_findings(report)[0]
        assert finding["blob_sha"] == "b" * 40
        assert finding["surface_key"]
        changed_blob = {**report["findings"][0], "blob_sha": "c" * 40}
        changed = rq._extract_audit_findings({"findings": [changed_blob]})[0]
        assert changed["surface_key"] != finding["surface_key"]

    def test_path_field_used_as_file_fallback(self):
        """When the raw finding keys the location as 'path' (not 'file'), the
        carried finding still gets file attribution."""
        report = {"findings": [{"severity": "low", "summary": "nit", "path": "x/y.py"}]}
        out = rq._extract_audit_findings(report)
        assert out[0]["file"] == "x/y.py"
        assert out[0]["key"] == rq._audit_finding_key("nit", "x/y.py")


class TestAuditFindingsCarryMeta:
    """_audit_findings_carry_meta exposes pre-cap totals + truncation flags so
    consumers can tell whether the carried (capped) lists are complete."""

    def test_none_report_zeroed(self):
        meta = rq._audit_findings_carry_meta(None)
        assert meta == {
            "audit_findings_total": 0,
            "audit_findings_truncated": False,
            "excluded_audit_findings_total": 0,
            "excluded_audit_findings_truncated": False,
        }

    def test_totals_agree_with_capped_lists_below_cap(self):
        report = {
            "findings": [
                {"severity": "low", "summary": "low a"},
                {"severity": "low", "summary": "low b"},
                # Unverified high → excluded list only.
                {"severity": "high", "summary": "speculative high"},
            ]
        }
        meta = rq._audit_findings_carry_meta(report)
        verified = rq._extract_audit_findings(report)
        excluded = rq._extract_excluded_audit_findings(report)
        assert meta["audit_findings_total"] == len(verified) == 2
        assert meta["audit_findings_truncated"] is False
        assert meta["excluded_audit_findings_total"] == len(excluded) == 1
        assert meta["excluded_audit_findings_truncated"] is False

    def test_truncated_flag_at_and_above_cap(self):
        # Exactly at the cap → not truncated.
        at_cap = {
            "findings": [
                {"severity": "low", "summary": f"low {i}"}
                for i in range(rq._MAX_CARRIED_FINDINGS)
            ]
        }
        meta_at = rq._audit_findings_carry_meta(at_cap)
        assert meta_at["audit_findings_total"] == rq._MAX_CARRIED_FINDINGS
        assert meta_at["audit_findings_truncated"] is False
        assert len(rq._extract_audit_findings(at_cap)) == rq._MAX_CARRIED_FINDINGS

        # Above the cap → truncated, total reflects the PRE-cap count.
        over_cap = {
            "findings": [
                {"severity": "low", "summary": f"low {i}"}
                for i in range(rq._MAX_CARRIED_FINDINGS + 3)
            ]
        }
        meta_over = rq._audit_findings_carry_meta(over_cap)
        assert meta_over["audit_findings_total"] == rq._MAX_CARRIED_FINDINGS + 3
        assert meta_over["audit_findings_truncated"] is True
        # Capped list is still bounded.
        assert len(rq._extract_audit_findings(over_cap)) == rq._MAX_CARRIED_FINDINGS

    def test_excluded_truncated_flag_above_cap(self):
        over_cap = {
            "findings": [
                {"severity": "high", "summary": f"unverified high {i}"}
                for i in range(rq._MAX_CARRIED_FINDINGS + 2)
            ]
        }
        meta = rq._audit_findings_carry_meta(over_cap)
        assert meta["excluded_audit_findings_total"] == rq._MAX_CARRIED_FINDINGS + 2
        assert meta["excluded_audit_findings_truncated"] is True


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
                "key": rq._audit_finding_key("Speculative SQL injection.", None),
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
                "key": rq._audit_finding_key("an unverified issue", None),
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


class TestComputeAuditChallengeSynthesis:
    def test_agree_marks_primary_findings_as_both_flagged(self):
        out = rq._compute_audit_challenge_synthesis(
            {
                "findings": [
                    {
                        "severity": "high",
                        "summary": "Unsafe migration path.",
                        "file": "backend/app/models.py",
                    }
                ]
            },
            {
                "agreement": "agree",
                "overstated_findings": [],
                "challenge_severity": "low",
            },
        )

        assert out["status"] == "available"
        assert out["reported_agreement"] == "agree"
        assert out["challenge_severity"] == "low"
        assert out["agreement"]["count"] == 1
        assert out["agreement"]["findings_both_flagged"][0]["summary"] == (
            "Unsafe migration path."
        )
        assert out["disagreement"]["primary_only"] == []
        assert out["disagreement"]["challenger_only"] == []
        assert out["disagreement"]["overstated_findings"] == []
        assert out["challenge_findings_truncated"] is False

    def test_challenge_findings_truncated_flag_set_when_primary_exceeds_max(self):
        findings = [
            {"severity": "high", "summary": f"Finding number {i}"} for i in range(15)
        ]
        out = rq._compute_audit_challenge_synthesis(
            {"findings": findings},
            {"agreement": "agree", "challenge_severity": "low"},
        )
        assert out["challenge_findings_truncated"] is True
        assert (
            len(out["agreement"]["findings_both_flagged"]) == rq._CHALLENGE_FINDING_MAX
        )

    def test_challenge_findings_truncated_false_when_within_max(self):
        findings = [
            {"severity": "high", "summary": f"Finding number {i}"} for i in range(5)
        ]
        out = rq._compute_audit_challenge_synthesis(
            {"findings": findings},
            {"agreement": "agree", "challenge_severity": "low"},
        )
        assert out["challenge_findings_truncated"] is False


class TestReconcileAuditWithChallengeVerdictConsumedOnce:
    """Each challenge verdict may match at most one deductible finding.

    A single refuted_false_positive cannot cancel multiple distinct real
    findings, and a single confirmed verdict cannot satisfy multiple findings.
    """

    SHA = "abc123"

    def _report(self, findings: list) -> dict:
        return {"findings": findings}

    def test_single_refuted_verdict_cannot_exclude_multiple_findings(self):
        """Two verified findings, one refuted_false_positive verdict (no file).

        The verdict fuzzy-matches the first finding and is consumed; the second
        finding has no remaining verdict and triggers an audit_challenge_missing HOLD.
        """
        finding_a = _make_verified_finding("SQL injection in query builder module")
        finding_b = _make_verified_finding("Hardcoded credentials in config file")
        report = self._report([finding_a, finding_b])

        # Single verdict that omits `file` can fuzzy-match either finding by summary,
        # but after matching the first it should be consumed and unavailable for the second.
        verdicts = [
            {
                "verdict": "refuted_false_positive",
                "severity": "high",
                "summary": "SQL injection in query builder module",
                "reason": "not present in current HEAD",
            }
        ]
        envelope = _make_challenge_envelope(self.SHA, verdicts)

        hold, modified, refuted, _binding = rq._reconcile_audit_with_challenge(
            report, envelope, self.SHA
        )

        # The second finding has no verdict left → HOLD, not a silent double exclusion.
        assert hold == "audit_challenge_binding_incomplete"
        assert modified is None
        assert refuted == []

    def test_single_confirmed_verdict_consumed_by_first_finding(self):
        """Two findings, one confirmed verdict: second triggers HOLD."""
        finding_a = _make_verified_finding("Missing authentication on admin endpoint")
        finding_b = _make_verified_finding("Unsafe deserialization in upload handler")
        report = self._report([finding_a, finding_b])

        verdicts = [
            {
                "verdict": "confirmed",
                "summary": "Missing authentication on admin endpoint",
            }
        ]
        envelope = _make_challenge_envelope(self.SHA, verdicts)

        hold, modified, _refuted, _binding = rq._reconcile_audit_with_challenge(
            report, envelope, self.SHA
        )

        assert hold == "audit_challenge_binding_incomplete"
        assert modified is None

    def test_empty_verdicts_with_matching_sha_get_distinct_hold_code(self):
        """Correct challenge SHA with zero verdicts is not a SHA mismatch."""
        finding = _make_verified_finding("Missing authorization on release promotion")
        envelope = _make_challenge_envelope(self.SHA, [])

        hold, modified, refuted, binding = rq._reconcile_audit_with_challenge(
            self._report([finding]), envelope, self.SHA
        )

        assert hold == "audit_challenge_empty_verdicts"
        assert modified is None
        assert refuted == []
        assert binding == []

    def test_two_verdicts_two_findings_both_matched(self):
        """One-to-one verdict-to-finding matching still works correctly."""
        finding_a = _make_verified_finding("Race condition in cache invalidation")
        finding_b = _make_verified_finding("Insecure direct object reference in API")
        report = self._report([finding_a, finding_b])

        verdicts = [
            {
                "verdict": "confirmed",
                "summary": "Race condition in cache invalidation",
            },
            {
                "verdict": "confirmed",
                "summary": "Insecure direct object reference in API",
            },
        ]
        envelope = _make_challenge_envelope(self.SHA, verdicts)

        hold, modified, refuted, _binding = rq._reconcile_audit_with_challenge(
            report, envelope, self.SHA
        )

        # Both findings confirmed → no HOLD, report unchanged, no refuted.
        assert hold is None
        assert modified is report
        assert refuted == []

    def test_primary_finding_index_binds_retry_verdict_with_paraphrased_summary(self):
        medium = _make_verified_finding("Medium priority challenge binding issue")
        medium["severity"] = "medium"
        high = _make_verified_finding("High priority challenge binding issue")
        report = self._report([medium, high])

        envelope = _make_challenge_envelope(
            self.SHA,
            [
                {
                    "primary_finding_index": 1,
                    "verdict": "confirmed",
                    "severity": "high",
                    "summary": "severe retry concern",
                },
                {
                    "primary_finding_index": 2,
                    "verdict": "confirmed",
                    "severity": "medium",
                    "summary": "moderate retry concern",
                },
            ],
        )

        hold, modified, refuted, binding = rq._reconcile_audit_with_challenge(
            report, envelope, self.SHA
        )

        assert hold is None
        assert modified is report
        assert refuted == []
        assert [b["finding_summary"] for b in binding] == [
            "Medium priority challenge binding issue",
            "High priority challenge binding issue",
        ]
        assert [b["verdict_summary"] for b in binding] == [
            "moderate retry concern",
            "severe retry concern",
        ]

    def test_retry_primary_findings_cover_all_deductible_findings_past_display_cap(
        self,
    ):
        findings = [
            _make_verified_finding(
                f"Verified challenge binding issue {i}", f"app/{i}.py"
            )
            for i in range(rq._MAX_CARRIED_FINDINGS + 2)
        ]
        report = self._report(findings)

        carried = rq._primary_findings_for_challenge_retry(report)
        assert len(carried) == len(findings)

        envelope = _make_challenge_envelope(
            self.SHA,
            [
                {
                    "primary_finding_index": i + 1,
                    "verdict": "confirmed",
                    "severity": finding["severity"],
                    "summary": finding["summary"],
                    "reason": "confirmed by challenge review with supporting evidence",
                }
                for i, finding in enumerate(carried)
            ],
        )

        hold, modified, refuted, binding = rq._reconcile_audit_with_challenge(
            report, envelope, self.SHA
        )

        assert hold is None
        assert modified is report
        assert refuted == []
        assert binding[0]["finding_summary"] == findings[0]["summary"]

    def test_empty_summary_finding_excluded_from_index_domain(self):
        """Empty-summary findings are excluded from the indexed and must-bind domains."""
        a = {
            "severity": "high",
            "summary": "",
            "file": "a.py",
            "verification_note": "confirmed via full repo scan with supporting evidence",
        }
        b = _make_verified_finding("Security critical authentication bypass")
        b["file"] = "b.py"
        report = {"findings": [a, b]}

        envelope = _make_challenge_envelope(
            self.SHA,
            [
                {
                    "primary_finding_index": 1,
                    "verdict": "refuted_false_positive",
                    "severity": "high",
                    "summary": "Security critical authentication bypass",
                    "reason": "thorough code review confirmed no authentication bypass",
                },
            ],
        )

        hold, modified, refuted, binding = rq._reconcile_audit_with_challenge(
            report, envelope, self.SHA
        )

        assert hold is None
        assert len(refuted) == 1
        assert refuted[0]["summary"] == "Security critical authentication bypass"
        assert refuted[0]["severity"] == "high"

    def test_indexed_refuted_lower_severity_not_allowed(self):
        """An indexed refuted_false_positive with lower severity than the finding
        must be rejected by _indexed_verdict_can_bind.

        When rejected the verdict is NOT bound by index and also excluded from
        the fuzzy adjacency list (it has primary_finding_index set), leaving the
        finding without any verdict and producing audit_challenge_binding_incomplete.
        """
        finding = _make_verified_finding("SQL injection in query builder module")
        report = {"findings": [finding]}

        envelope = _make_challenge_envelope(
            self.SHA,
            [
                {
                    "primary_finding_index": 1,
                    "verdict": "refuted_false_positive",
                    "severity": "low",  # lower than finding's "high"
                    "summary": "SQL injection in query builder module",
                    "reason": "parameterized queries are used throughout",
                },
            ],
        )

        hold, modified, refuted, _binding = rq._reconcile_audit_with_challenge(
            report, envelope, self.SHA
        )

        # Severity ceiling blocks the refutation; no fuzzy fallback since
        # primary_finding_index is set → binding is incomplete.
        assert hold == "audit_challenge_binding_incomplete"
        assert refuted == []

    def test_indexed_refuted_equal_severity_allowed(self):
        """An indexed refuted_false_positive with equal severity is accepted and
        removes the finding from scoring; this is the backend side of agent
        canonicalization to the bound finding's severity.
        """
        finding = _make_verified_finding("SQL injection in query builder module")
        report = {"findings": [finding]}

        envelope = _make_challenge_envelope(
            self.SHA,
            [
                {
                    "primary_finding_index": 1,
                    "verdict": "refuted_false_positive",
                    "severity": "high",  # equal to finding's "high"
                    "summary": "SQL injection in query builder module",
                    "reason": "parameterized queries are used throughout the module",
                },
            ],
        )

        hold, modified, refuted, _binding = rq._reconcile_audit_with_challenge(
            report, envelope, self.SHA
        )

        # Severity ceiling passes; finding is removed.
        assert hold is None
        assert len(refuted) == 1
        assert refuted[0]["summary"] == "SQL injection in query builder module"

    def test_indexed_refuted_paraphrased_summary_binds_by_index(self):
        """Indexed refutation binds by index regardless of summary text mismatch.

        The backend binds via primary_finding_index; the verbatim requirement
        applies only to the agent side.  _indexed_verdict_can_bind only checks
        the severity ceiling, so a paraphrased summary must still remove the
        finding from scoring.
        """
        finding = _make_verified_finding("Memory corruption in packet parser")
        finding["severity"] = "high"
        report = {"findings": [finding]}

        envelope = _make_challenge_envelope(
            self.SHA,
            [
                {
                    "primary_finding_index": 1,
                    "verdict": "refuted_false_positive",
                    "severity": "high",
                    "summary": "Heap overflow during network packet parsing",  # paraphrase
                    "reason": "bounds-checked buffer slices prevent out-of-bounds writes",
                },
            ],
        )

        hold, modified, refuted, _binding = rq._reconcile_audit_with_challenge(
            report, envelope, self.SHA
        )

        # Index-based binding bypasses summary matching; finding is removed.
        assert hold is None
        assert len(refuted) == 1

    @pytest.mark.parametrize(
        ("finding_summary", "verdict_summary"),
        [
            (
                "Out of range index still has exact text",
                "Out of range index still has exact text",
            ),
            (
                "Release audit challenge binding retries lose primary verdict identity",
                "challenge binding retry loses primary verdict identity",
            ),
        ],
    )
    def test_out_of_range_index_falls_back_to_content_matching(
        self, finding_summary, verdict_summary
    ):
        finding = _make_verified_finding(finding_summary)
        report = {"findings": [finding]}

        envelope = _make_challenge_envelope(
            self.SHA,
            [
                {
                    "primary_finding_index": 99,
                    "verdict": "confirmed",
                    "severity": "high",
                    "summary": verdict_summary,
                },
            ],
        )

        hold, modified, refuted, binding = rq._reconcile_audit_with_challenge(
            report, envelope, self.SHA
        )

        assert hold is None
        assert modified is report
        assert refuted == []
        assert binding[0]["verdict_summary"] == verdict_summary

    def test_truncated_primary_summary_exact_match_binds(self):
        long_summary = "A" * (rq._FINDING_SUMMARY_MAX + 40)
        finding = _make_verified_finding(long_summary)
        report = {"findings": [finding]}
        shown_to_agent = long_summary[: rq._FINDING_SUMMARY_MAX]

        envelope = _make_challenge_envelope(
            self.SHA,
            [
                {
                    "verdict": "confirmed",
                    "severity": "high",
                    "summary": shown_to_agent,
                },
            ],
        )

        hold, modified, refuted, binding = rq._reconcile_audit_with_challenge(
            report, envelope, self.SHA
        )

        assert hold is None
        assert modified is report
        assert refuted == []
        assert binding[0]["finding_summary"] == shown_to_agent

    def test_refuted_verdict_consumed_second_finding_held(self):
        """One refuted verdict + one confirmed verdict match one finding each."""
        finding_a = _make_verified_finding(
            "Path traversal vulnerability in file server"
        )
        finding_b = _make_verified_finding("Weak password hashing algorithm used")
        report = self._report([finding_a, finding_b])

        verdicts = [
            {
                "verdict": "refuted_false_positive",
                "severity": "high",
                "summary": "Path traversal vulnerability in file server",
                "reason": "no user input reaches the file path",
            },
            {
                "verdict": "confirmed",
                "summary": "Weak password hashing algorithm used",
            },
        ]
        envelope = _make_challenge_envelope(self.SHA, verdicts)

        hold, modified, refuted, _binding = rq._reconcile_audit_with_challenge(
            report, envelope, self.SHA
        )

        # First finding refuted (removed), second confirmed (kept) → no HOLD.
        assert hold is None
        assert modified is not None
        assert len(modified["findings"]) == 1
        assert (
            modified["findings"][0]["summary"] == "Weak password hashing algorithm used"
        )
        assert len(refuted) == 1
        assert refuted[0]["summary"] == "Path traversal vulnerability in file server"

    def test_uncertain_verdict_consumed_triggers_hold(self):
        """An uncertain verdict immediately triggers HOLD (existing behavior preserved)."""
        finding_a = _make_verified_finding(
            "SSRF vulnerability in outbound request handler"
        )
        report = self._report([finding_a])

        verdicts = [
            {
                "verdict": "uncertain",
                "summary": "SSRF vulnerability in outbound request handler",
            }
        ]
        envelope = _make_challenge_envelope(self.SHA, verdicts)

        hold, modified, refuted, _binding = rq._reconcile_audit_with_challenge(
            report, envelope, self.SHA
        )

        assert hold == "audit_challenge_uncertain"
        assert modified is None
        assert refuted == []

    def test_match_finding_to_challenge_removed(self):
        """_match_finding_to_challenge is no longer exported — it was dead code."""
        assert not hasattr(rq, "_match_finding_to_challenge")


class TestReconcileAuditBestMatchAssignment:
    """Best-match pre-assignment: Jaccard denominator + global greedy assignment.

    These tests verify the hardened matching introduced to close two failure modes:
    (1) score inflation — a refutation mis-bound to a higher-severity finding cancels
        a larger deduction than warranted; and
    (2) spurious holds — a mis-bound verdict leaves the intended finding with no verdict.
    """

    SHA = "deadbeef"

    def _report(self, findings: list) -> dict:
        return {"findings": findings}

    def _verified(self, severity: str, summary: str, file: str = "") -> dict:
        return {
            "severity": severity,
            "summary": summary,
            "file": file,
            "verification_note": "confirmed via full repo scan with supporting evidence",
        }

    def test_refuted_verdict_cannot_cancel_higher_severity_finding(self):
        """Refutation of a low-overlap finding must not be bound to a CRITICAL finding.

        Finding A (CRITICAL) has a detailed summary with many tokens.
        Finding B (HIGH) has a short summary whose tokens are a strict subset of A's.
        The refutation verdict exactly matches B's summary with severity "high".

        The severity ceiling in _score_finding_to_challenge fires first: a "high"
        refutation has verdict_rank=1 > finding_rank=0 (critical) → returns None,
        so the refutation cannot score against A at all.  Jaccard is a secondary
        guard and would also reject it, but severity ceiling fires earlier.
        """
        finding_a = self._verified(
            "critical",
            "Unbounded file read allows arbitrary file disclosure via path traversal in upload module",
        )
        finding_b = self._verified(
            "high",
            "File read path traversal in upload module",
        )
        report = self._report([finding_a, finding_b])

        # Verdicts emitted in reverse-finding order: refutation (for B) first so the
        # old greedy code would have bound it to A (the first finding it matched).
        verdicts = [
            {
                "verdict": "refuted_false_positive",
                "severity": "high",
                "summary": "File read path traversal in upload module",
                "reason": "no actual arbitrary file access possible",
            },
            {
                "verdict": "confirmed",
                "summary": "Unbounded file read allows arbitrary file disclosure via path traversal in upload module",
            },
        ]
        envelope = _make_challenge_envelope(self.SHA, verdicts)

        hold, modified, refuted, _binding = rq._reconcile_audit_with_challenge(
            report, envelope, self.SHA
        )

        # A (CRITICAL) must be confirmed — it must NOT be the refuted one.
        assert hold is None
        assert modified is not None
        assert len(modified["findings"]) == 1
        assert "Unbounded file read" in modified["findings"][0]["summary"]
        assert len(refuted) == 1
        assert refuted[0]["severity"] == "high"

    def test_refuted_subset_summary_does_not_cancel_broader_finding(self):
        """A short subset refutation must remove the intended smaller finding only.

        The verdict summary exactly matches the lower-impact finding B, but its
        tokens are a strict subset of the broader high-severity finding A.  The
        matcher must bind the refutation to B so A stays in the report and its
        HIGH deduction still applies.
        """
        finding_a = self._verified(
            "high",
            "Unbounded file read allows arbitrary file disclosure via path traversal in upload module",
        )
        finding_b = self._verified("medium", "Path traversal in upload module")
        report = self._report([finding_a, finding_b])

        verdicts = [
            {
                "verdict": "refuted_false_positive",
                "severity": "high",
                "summary": "Path traversal in upload module",
                "reason": "path is normalized before file access",
            },
            {
                "verdict": "confirmed",
                "summary": "Unbounded file read allows arbitrary file disclosure via path traversal in upload module",
            },
        ]
        envelope = _make_challenge_envelope(self.SHA, verdicts)

        hold, modified, refuted, _binding = rq._reconcile_audit_with_challenge(
            report, envelope, self.SHA
        )

        assert hold is None
        assert modified is not None
        assert len(modified["findings"]) == 1
        assert modified["findings"][0]["severity"] == "high"
        assert "Unbounded file read" in modified["findings"][0]["summary"]
        assert len(refuted) == 1
        assert refuted[0]["severity"] == "medium"
        assert refuted[0]["summary"] == "Path traversal in upload module"

    def test_similar_findings_refutation_matches_correct_target(self):
        """Two near-identical higher-severity findings; refutation matches only one.

        Finding A: "SQL injection via query param in user endpoint"
        Finding B: "SQL injection via header param in user endpoint"

        The refutation summary exactly matches B. With best-match assignment,
        the refutation is bound to B (exact match score 1.0) and A is confirmed.
        """
        finding_a = self._verified(
            "high", "SQL injection via query param in user endpoint"
        )
        finding_b = self._verified(
            "high", "SQL injection via header param in user endpoint"
        )
        report = self._report([finding_a, finding_b])

        verdicts = [
            {
                "verdict": "confirmed",
                "summary": "SQL injection via query param in user endpoint",
            },
            {
                "verdict": "refuted_false_positive",
                "severity": "high",
                "summary": "SQL injection via header param in user endpoint",
                "reason": "header param is sanitized before use",
            },
        ]
        envelope = _make_challenge_envelope(self.SHA, verdicts)

        hold, modified, refuted, _binding = rq._reconcile_audit_with_challenge(
            report, envelope, self.SHA
        )

        assert hold is None
        assert modified is not None
        assert len(modified["findings"]) == 1
        assert "query param" in modified["findings"][0]["summary"]
        assert len(refuted) == 1
        assert "header param" in refuted[0]["summary"]

    def test_asymmetric_token_overlap_does_not_match_across_topics(self):
        """A verdict summary with low token overlap against a finding does not match.

        Verdict summary: "SQL injection endpoint"  (3 significant tokens)
        Finding A: "Cross-site request forgery bypass in admin dashboard endpoint"

        Only 1 token ("endpoint") is shared.  min-denominator: 1/3 = 0.33 < 0.6.
        No match → reconciliation returns audit_challenge_missing (HOLD, fail-closed).
        """
        finding_a = self._verified(
            "high",
            "Cross-site request forgery bypass in admin dashboard endpoint",
        )
        report = self._report([finding_a])

        verdicts = [
            {
                "verdict": "refuted_false_positive",
                "severity": "high",
                "summary": "SQL injection endpoint",
                "reason": "irrelevant finding",
            }
        ]
        envelope = _make_challenge_envelope(self.SHA, verdicts)

        hold, modified, _refuted, _binding = rq._reconcile_audit_with_challenge(
            report, envelope, self.SHA
        )

        # Verdict does not reach the 0.6 token-overlap threshold → HOLD (fail-closed).
        assert hold == "audit_challenge_binding_incomplete"
        assert modified is None

    def test_best_match_assignment_ignores_emission_order(self):
        """Verdict order must not determine which finding each verdict is bound to.

        Findings are emitted in order A, B; verdicts are emitted in order B, A (reversed).
        Each verdict exactly matches only its intended finding.  Best-match assignment
        must pair each verdict with its highest-scoring finding regardless of list order.
        """
        finding_a = self._verified("high", "Race condition in cache invalidation logic")
        finding_b = self._verified(
            "high", "Missing rate limit on password reset endpoint"
        )
        report = self._report([finding_a, finding_b])

        # Verdicts in REVERSE order: B's verdict first, then A's.
        verdicts = [
            {
                "verdict": "confirmed",
                "summary": "Missing rate limit on password reset endpoint",
            },
            {
                "verdict": "refuted_false_positive",
                "severity": "high",
                "summary": "Race condition in cache invalidation logic",
                "reason": "lock acquired before shared state access",
            },
        ]
        envelope = _make_challenge_envelope(self.SHA, verdicts)

        hold, modified, refuted, _binding = rq._reconcile_audit_with_challenge(
            report, envelope, self.SHA
        )

        # A is refuted (removed), B is confirmed (kept) — regardless of verdict order.
        assert hold is None
        assert modified is not None
        assert len(modified["findings"]) == 1
        assert "rate limit" in modified["findings"][0]["summary"]
        assert len(refuted) == 1
        assert "Race condition" in refuted[0]["summary"]

    def test_complete_assignment_found_when_greedy_order_would_strand_a_finding(self):
        """Augmenting-path matching finds a complete assignment greedy would miss.

        F1: medium "Race condition in cache invalidation logic"
        F2: medium "Race condition in session token refresh"
        V1 (refuted_false_positive, medium): "Race condition cache invalidation refresh"
            → Jaccard against F1 ≈ 4/6 = 0.667 (≥ 0.6 threshold) — matches F1 only.
            → Jaccard against F2 ≈ 3/7 ≈ 0.43 (< 0.6 threshold) — no match.
        V2 (confirmed): "race condition"
            → min-denominator against F1 = 2/2 = 1.0 — matches.
            → min-denominator against F2 = 2/2 = 1.0 — matches.

        Greedy assigns F1→V2 first (score 1.0), then cannot assign F2 (V2 used, V1
        only matches F1 which is taken) → spurious audit_challenge_missing hold.

        Augmenting path re-routes F1→V1, F2→V2 so the complete assignment is found.
        """
        finding_f1 = self._verified(
            "medium", "Race condition in cache invalidation logic"
        )
        finding_f2 = self._verified("medium", "Race condition in session token refresh")
        report = self._report([finding_f1, finding_f2])

        verdicts = [
            {
                "verdict": "refuted_false_positive",
                "severity": "medium",
                "summary": "Race condition cache invalidation refresh",
                "reason": "lock is held before cache state is read",
            },
            {
                "verdict": "confirmed",
                "summary": "race condition",
            },
        ]
        envelope = _make_challenge_envelope(self.SHA, verdicts)

        hold, modified, refuted, _binding = rq._reconcile_audit_with_challenge(
            report, envelope, self.SHA
        )

        # Complete assignment exists: no spurious hold.
        assert hold is None
        assert modified is not None
        # F1 is refuted (removed), F2 is confirmed (kept).
        assert len(modified["findings"]) == 1
        assert "session token" in modified["findings"][0]["summary"]
        assert len(refuted) == 1
        assert "cache invalidation" in refuted[0]["summary"]


class TestReconcileAuditChallengeRefutedSeverityCeiling:
    """Refuted verdict binding is severity-gated because it removes deductions."""

    SHA = "facefeed"

    def _report(self, findings: list) -> dict:
        return {"findings": findings}

    def _verified(self, severity: str, summary: str) -> dict:
        return {
            "severity": severity,
            "summary": summary,
            "verification_note": "confirmed via full repo scan with supporting evidence",
        }

    def test_low_refutation_does_not_cancel_critical_finding(self):
        finding = self._verified(
            "critical",
            "Remote code execution in artifact extraction path",
        )
        envelope = _make_challenge_envelope(
            self.SHA,
            [
                {
                    "verdict": "refuted_false_positive",
                    "severity": "low",
                    "summary": "Remote code execution in artifact extraction path",
                    "reason": "only assessed a low-risk extraction warning",
                }
            ],
        )

        hold, modified, refuted, _binding = rq._reconcile_audit_with_challenge(
            self._report([finding]), envelope, self.SHA
        )

        assert hold == "audit_challenge_binding_incomplete"
        assert modified is None
        assert refuted == []

    @pytest.mark.parametrize("verdict_severity", ["high", "critical"])
    def test_equal_or_higher_refutation_can_cancel_finding(self, verdict_severity):
        finding = self._verified(
            "high",
            "Privilege escalation through workspace role mutation",
        )
        envelope = _make_challenge_envelope(
            self.SHA,
            [
                {
                    "verdict": "refuted_false_positive",
                    "severity": verdict_severity,
                    "summary": "Privilege escalation through workspace role mutation",
                    "reason": "role mutation is guarded by an existing admin check",
                }
            ],
        )

        hold, modified, refuted, _binding = rq._reconcile_audit_with_challenge(
            self._report([finding]), envelope, self.SHA
        )

        assert hold is None
        assert modified is not None
        assert modified["findings"] == []
        assert len(refuted) == 1
        assert refuted[0]["severity"] == "high"

    def test_confirmed_verdict_still_ignores_lower_severity(self):
        finding = self._verified(
            "critical",
            "Tenant isolation bypass in project membership checks",
        )
        report = self._report([finding])
        envelope = _make_challenge_envelope(
            self.SHA,
            [
                {
                    "verdict": "confirmed",
                    "severity": "info",
                    "summary": "Tenant isolation bypass in project membership checks",
                }
            ],
        )

        hold, modified, refuted, _binding = rq._reconcile_audit_with_challenge(
            report, envelope, self.SHA
        )

        assert hold is None
        assert modified is report
        assert refuted == []


# ---------------------------------------------------------------------------
# Parametrized severity matrix for the refuted_false_positive ceiling gate
# ---------------------------------------------------------------------------


class TestRefutedSeverityMatrixScoreFinding:
    """Parametrized matrix for _score_finding_to_challenge severity ceiling.

    A refuted_false_positive verdict must have a severity rank <= the finding's
    rank (i.e. verdict severity must be at least as severe as the finding).
    Confirmed/uncertain verdicts are NOT gated by severity.
    """

    SUMMARY = "SQL injection in query builder"

    def _finding(self, severity: str) -> dict:
        return {"severity": severity, "summary": self.SUMMARY}

    def _verdict(self, verdict_type: str, severity: str) -> dict:
        return {"verdict": verdict_type, "severity": severity, "summary": self.SUMMARY}

    # --- refuted_false_positive: cases that must return None (no match) ---

    @pytest.mark.parametrize(
        "verdict_sev,finding_sev",
        [
            ("medium", "critical"),
            ("medium", "high"),
            ("low", "critical"),
            ("low", "high"),
            ("low", "medium"),
        ],
    )
    def test_refuted_lower_rank_cannot_bind(self, verdict_sev, finding_sev):
        """refuted_false_positive with lower-rank severity returns None — no binding."""
        score = rs._score_finding_to_challenge(
            self._finding(finding_sev),
            self._verdict("refuted_false_positive", verdict_sev),
        )
        assert score is None, (
            f"Expected no match: verdict_sev={verdict_sev!r} vs finding_sev={finding_sev!r}"
        )

    # --- refuted_false_positive: cases that must succeed (return a score) ---

    @pytest.mark.parametrize(
        "verdict_sev,finding_sev",
        [
            ("medium", "medium"),  # equal rank — passes ceiling
            ("high", "medium"),  # higher rank — passes ceiling
            ("critical", "medium"),  # higher rank — passes ceiling
            ("high", "high"),  # equal rank — passes ceiling
            ("critical", "high"),  # higher rank — passes ceiling
            ("critical", "critical"),  # equal rank — passes ceiling
        ],
    )
    def test_refuted_equal_or_higher_rank_can_bind(self, verdict_sev, finding_sev):
        """refuted_false_positive with equal or higher-rank severity returns a score."""
        score = rs._score_finding_to_challenge(
            self._finding(finding_sev),
            self._verdict("refuted_false_positive", verdict_sev),
        )
        assert score is not None, (
            f"Expected a match: verdict_sev={verdict_sev!r} vs finding_sev={finding_sev!r}"
        )
        assert score > 0

    # --- unknown/missing severity edge cases ---

    def test_refuted_missing_verdict_severity_treated_as_unknown(self):
        """A refuted verdict with no severity key falls back to rank 99 (unknown).

        Unknown verdict rank (99) > any real finding rank, so the ceiling fires
        and the verdict cannot bind to a finding with a known severity.
        """
        finding = self._finding("critical")
        verdict = {"verdict": "refuted_false_positive", "summary": self.SUMMARY}
        score = rs._score_finding_to_challenge(finding, verdict)
        assert score is None, (
            "Missing verdict severity should not bind to a critical finding"
        )

    def test_refuted_missing_finding_severity_treated_as_unknown(self):
        """A finding with no severity has rank 99; any verdict rank <= 99 can bind."""
        finding = {"summary": self.SUMMARY}  # no severity key
        verdict = self._verdict("refuted_false_positive", "low")
        score = rs._score_finding_to_challenge(finding, verdict)
        # low rank (3) <= unknown finding rank (99): ceiling passes, content matches
        assert score is not None, (
            "Low verdict should bind to a finding with unknown severity"
        )

    def test_confirmed_verdict_not_gated_by_severity(self):
        """confirmed verdicts are never filtered by severity — any severity combination passes."""
        finding = self._finding("critical")
        verdict = self._verdict("confirmed", "info")
        score = rs._score_finding_to_challenge(finding, verdict)
        assert score is not None, (
            "confirmed verdict should match regardless of severity"
        )

    def test_uncertain_verdict_not_gated_by_severity(self):
        """uncertain verdicts are never filtered by severity."""
        finding = self._finding("critical")
        verdict = self._verdict("uncertain", "low")
        score = rs._score_finding_to_challenge(finding, verdict)
        assert score is not None, (
            "uncertain verdict should match regardless of severity"
        )


class TestRefutedSeverityMatrixReconcile:
    """End-to-end parametrized matrix for _reconcile_audit_with_challenge.

    Verifies the severity ceiling gate causes the right hold code or binding
    outcome at the reconciliation level, not just the scoring level.
    """

    SHA = "deadbeef"

    def _report(self, findings: list) -> dict:
        return {"findings": findings}

    def _verified(self, severity: str, summary: str) -> dict:
        return {
            "severity": severity,
            "summary": summary,
            "verification_note": "confirmed via full repo scan with supporting evidence",
        }

    @pytest.mark.parametrize(
        "verdict_sev,finding_sev",
        [
            ("medium", "critical"),
            ("medium", "high"),
            ("low", "medium"),
        ],
    )
    def test_lower_rank_refutation_triggers_hold(self, verdict_sev, finding_sev):
        """A refuted verdict whose rank is lower than the finding's triggers hold."""
        finding = self._verified(finding_sev, "Unescaped user input reaches SQL query")
        envelope = _make_challenge_envelope(
            self.SHA,
            [
                {
                    "verdict": "refuted_false_positive",
                    "severity": verdict_sev,
                    "summary": "Unescaped user input reaches SQL query",
                    "reason": "assessed only the surface-level form validation",
                }
            ],
        )
        hold, modified, refuted, _binding = rq._reconcile_audit_with_challenge(
            self._report([finding]), envelope, self.SHA
        )
        assert hold == "audit_challenge_binding_incomplete", (
            f"verdict_sev={verdict_sev!r} vs finding_sev={finding_sev!r}: "
            f"expected hold='audit_challenge_binding_incomplete', got {hold!r}"
        )
        assert modified is None
        assert refuted == []

    @pytest.mark.parametrize(
        "verdict_sev,finding_sev",
        [
            ("medium", "medium"),
            ("high", "medium"),
            ("critical", "medium"),
        ],
    )
    def test_equal_or_higher_rank_refutation_removes_finding(
        self, verdict_sev, finding_sev
    ):
        """A refuted verdict with >= rank removes the finding from the report."""
        finding = self._verified(finding_sev, "Unescaped user input reaches SQL query")
        envelope = _make_challenge_envelope(
            self.SHA,
            [
                {
                    "verdict": "refuted_false_positive",
                    "severity": verdict_sev,
                    "summary": "Unescaped user input reaches SQL query",
                    "reason": "query uses parameterized statements throughout",
                }
            ],
        )
        hold, modified, refuted, _binding = rq._reconcile_audit_with_challenge(
            self._report([finding]), envelope, self.SHA
        )
        assert hold is None, (
            f"verdict_sev={verdict_sev!r} vs finding_sev={finding_sev!r}: "
            f"expected no hold, got {hold!r}"
        )
        assert modified is not None
        assert modified["findings"] == []
        assert len(refuted) == 1
        assert refuted[0]["severity"] == finding_sev


class TestChallengerIndependentBlocker:
    """Challenger-only confirmed >=HIGH verdicts must gate qualification.

    When the challenger identifies a >=HIGH severity issue with no matching
    primary finding (vi not in vi_assigned), the reconciliation must return
    the 'audit_challenge_independent_blocker' hold code.
    """

    SHA = "cafebabe"

    def _report(self, findings: list) -> dict:
        return {"findings": findings}

    def test_challenger_only_high_confirmed_verdict_holds(self):
        """Unmatched confirmed HIGH verdict with no primary findings returns hold."""
        envelope = _make_challenge_envelope(
            self.SHA,
            [
                {
                    "verdict": "confirmed",
                    "severity": "high",
                    "summary": "SQL injection in user search endpoint",
                    "reason": "no parameterized query",
                }
            ],
        )
        hold, modified, refuted, blocker_findings = rq._reconcile_audit_with_challenge(
            self._report([]), envelope, self.SHA
        )
        assert hold == "audit_challenge_independent_blocker"
        assert modified is None
        assert refuted == []
        assert len(blocker_findings) == 1
        assert blocker_findings[0]["severity"] == "high"
        assert "SQL injection" in blocker_findings[0]["summary"]
        assert blocker_findings[0]["source"] == "challenger_independent"

    def test_challenger_only_critical_confirmed_verdict_holds(self):
        """Unmatched confirmed CRITICAL verdict returns hold."""
        envelope = _make_challenge_envelope(
            self.SHA,
            [
                {
                    "verdict": "confirmed",
                    "severity": "critical",
                    "summary": "Remote code execution via deserialization",
                    "reason": "untrusted data deserialized without validation",
                }
            ],
        )
        hold, modified, refuted, blocker_findings = rq._reconcile_audit_with_challenge(
            self._report([]), envelope, self.SHA
        )
        assert hold == "audit_challenge_independent_blocker"
        assert modified is None
        assert refuted == []
        assert len(blocker_findings) == 1
        assert blocker_findings[0]["severity"] == "critical"
        assert blocker_findings[0]["source"] == "challenger_independent"

    def test_challenger_only_blocker_confirmed_verdict_holds(self):
        """Unmatched confirmed BLOCKER severity returns hold."""
        envelope = _make_challenge_envelope(
            self.SHA,
            [
                {
                    "verdict": "confirmed",
                    "severity": "blocker",
                    "summary": "Auth bypass in admin panel",
                    "reason": "session token not validated server-side",
                }
            ],
        )
        hold, modified, refuted, blocker_findings = rq._reconcile_audit_with_challenge(
            self._report([]), envelope, self.SHA
        )
        assert hold == "audit_challenge_independent_blocker"
        assert modified is None
        assert refuted == []
        assert len(blocker_findings) == 1
        assert blocker_findings[0]["severity"] == "blocker"
        assert blocker_findings[0]["source"] == "challenger_independent"

    def test_challenger_only_medium_confirmed_no_hold(self):
        """Unmatched confirmed MEDIUM verdict does not trigger independent blocker hold."""
        envelope = _make_challenge_envelope(
            self.SHA,
            [
                {
                    "verdict": "confirmed",
                    "severity": "medium",
                    "summary": "Information disclosure in error messages",
                    "reason": "stack traces exposed in non-production configs",
                }
            ],
        )
        hold, modified, refuted, _binding = rq._reconcile_audit_with_challenge(
            self._report([]), envelope, self.SHA
        )
        assert hold is None
        assert modified == self._report([])

    def test_challenger_only_unknown_severity_no_hold(self):
        """Unmatched confirmed verdict with missing/unknown severity does not hold."""
        envelope = _make_challenge_envelope(
            self.SHA,
            [
                {
                    "verdict": "confirmed",
                    "severity": "unknown_level",
                    "summary": "Something weird",
                    "reason": "indeterminate severity",
                }
            ],
        )
        hold, modified, refuted, _binding = rq._reconcile_audit_with_challenge(
            self._report([]), envelope, self.SHA
        )
        assert hold is None
        assert modified == self._report([])

    def test_challenger_only_missing_severity_no_hold(self):
        """Unmatched confirmed verdict with no severity field does not hold."""
        envelope = _make_challenge_envelope(
            self.SHA,
            [
                {
                    "verdict": "confirmed",
                    "summary": "Something without severity",
                }
            ],
        )
        hold, modified, refuted, _binding = rq._reconcile_audit_with_challenge(
            self._report([]), envelope, self.SHA
        )
        assert hold is None
        assert modified == self._report([])

    def test_challenger_only_refuted_high_no_hold(self):
        """Unmatched refuted/high verdict does not trigger independent blocker."""
        envelope = _make_challenge_envelope(
            self.SHA,
            [
                {
                    "verdict": "refuted_false_positive",
                    "severity": "high",
                    "summary": "XSS in comment field",
                    "reason": "input is properly sanitized before rendering",
                }
            ],
        )
        hold, modified, refuted, _binding = rq._reconcile_audit_with_challenge(
            self._report([]), envelope, self.SHA
        )
        # No primary findings -> no deductible -> no assignment needed -> no hold
        assert hold is None
        assert modified == self._report([])

    def test_matched_finding_plus_unmatched_high_still_holds(self):
        """One primary finding matched + one unmatched confirmed HIGH still holds."""
        verified_finding = {
            "severity": "medium",
            "summary": "Info leak in error response",
            "verification_note": "confirmed via full repo scan with supporting evidence",
        }
        report = self._report([verified_finding])
        envelope = _make_challenge_envelope(
            self.SHA,
            [
                {
                    "verdict": "refuted_false_positive",
                    "severity": "medium",
                    "summary": "Info leak in error response",
                    "reason": "the error response is behind an admin-only endpoint",
                },
                {
                    "verdict": "confirmed",
                    "severity": "high",
                    "summary": "Challenger-discovered SQL injection missed by primary",
                    "reason": "user-supplied data flows into raw query",
                },
            ],
        )
        hold, modified, refuted, blocker_findings = rq._reconcile_audit_with_challenge(
            report, envelope, self.SHA
        )
        assert hold == "audit_challenge_independent_blocker"
        assert modified is None
        assert refuted == []
        assert len(blocker_findings) == 1
        assert blocker_findings[0]["severity"] == "high"
        assert "SQL injection" in blocker_findings[0]["summary"]
        assert blocker_findings[0]["source"] == "challenger_independent"

    def test_all_matched_confirmed_high_no_independent_hold(self):
        """When vi_assigned covers the confirmed HIGH verdict, no independent hold fires."""
        verified_finding = {
            "severity": "high",
            "summary": "SQL injection in user search endpoint",
            "verification_note": "confirmed via full repo scan with supporting evidence",
        }
        report = self._report([verified_finding])
        envelope = _make_challenge_envelope(
            self.SHA,
            [
                {
                    "verdict": "confirmed",
                    "severity": "high",
                    "summary": "SQL injection in user search endpoint",
                    "reason": "confirmed the injection path",
                }
            ],
        )
        hold, modified, refuted, _binding = rq._reconcile_audit_with_challenge(
            report, envelope, self.SHA
        )
        # confirmed matched verdict keeps the finding, no independent blocker
        assert hold is None
        assert modified == report
        assert refuted == []


# ---------------------------------------------------------------------------
# _finalize_qualification — independent blocker hold routes to quality outcome
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_independent_blocker_hold_records_quality_outcome_not_infra_failure(
    db_session, mocker
):
    """Acceptance test: an independent-blocker hold is a quality finding.

    When the challenger confirms a >=HIGH severity issue not present in the
    primary audit findings, _finalize_qualification must:
    - return status='not_promoted' (not 'infra_failure')
    - set version.last_qualification["status"] == 'not_promoted'
    - record a QualificationVerdict row with terminal_status='not_promoted'
    - NOT set 'consecutive_infra_errors' in last_qualification
    - NOT raise an infra Alert row
    - carry the challenger finding in last_qualification['audit_findings']
    """
    from app import shared_state
    from app.models import Alert

    shared_state.runtime_config["promotion_enabled"] = True
    mocker.patch("app.release_qualifier.promote", new=AsyncMock())

    project, version = await _make_project_and_version(db_session)
    # Audit has no deductible (empty findings), so the independent-blocker branch
    # in _reconcile_audit_with_challenge fires on the challenger's confirmed HIGH.
    audit_task, _smoke = await _seed_qualification_children(
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
            "tests": [{"name": "a", "status": "pass"}],
            "suites": {"startup": {"passed": True}},
        },
    )
    # Challenge confirms an independent HIGH finding the primary audit missed.
    await _seed_challenge_artifact(
        db_session,
        project,
        version,
        audit_task=audit_task,
        verdicts=[
            {
                "verdict": "confirmed",
                "severity": "high",
                "summary": "SQL injection in admin search endpoint missed by primary audit",
                "reason": "user-supplied data flows into a raw SQL query without sanitisation",
            }
        ],
    )

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    # Must return quality-finding outcome, NOT infra_failure.
    assert result["status"] == "not_promoted", (
        f"expected not_promoted but got {result['status']!r} — "
        "independent blocker should be classified as a quality finding"
    )
    assert result["blocker"] == "audit_challenge_independent_blocker"

    # last_qualification reflects the quality outcome.
    await db_session.refresh(version)
    lq = version.last_qualification
    assert lq["status"] == "not_promoted"
    assert lq["blocker"] == "audit_challenge_independent_blocker"
    # Infra streak must not be set — this is not an infrastructure failure.
    assert (
        "consecutive_infra_errors" not in lq
        or lq.get("consecutive_infra_errors") is None
    )
    # Challenger finding detail must be persisted for the strategic loop.
    assert lq.get("audit_findings"), (
        "audit_findings must be populated with the blocker detail"
    )
    assert lq["audit_findings"][0]["severity"] == "high"
    assert "SQL injection" in lq["audit_findings"][0]["summary"]

    # A durable QualificationVerdict row must exist (unlike _handle_infra_failure).
    count = (
        await db_session.execute(
            select(sa_func.count()).where(
                QualificationVerdict.release_version_id == version.id
            )
        )
    ).scalar()
    assert count == 1, (
        f"expected 1 QualificationVerdict row but found {count} — "
        "independent blocker must record a durable quality verdict"
    )

    # No infra Alert should have been raised.
    alert_count = (
        await db_session.execute(
            select(sa_func.count()).where(Alert.project_id == project.id)
        )
    ).scalar()
    assert alert_count == 0, (
        f"expected 0 Alert rows but found {alert_count} — "
        "an independent quality blocker must not raise an infra alert"
    )

    # Version returned to stabilize, not promoted.
    assert version.phase == "stabilize"
    assert version.status == "open"


@pytest.mark.asyncio
async def test_independent_blocker_does_not_increment_infra_streak(db_session, mocker):
    """Repeated independent-blocker holds do not accumulate infra_error streak.

    Unlike infra failures, each independent-blocker hold is a quality verdict.
    The infra-error counter must not grow across multiple runs — otherwise the
    backstop alert fires and misleads the operator about the agent infrastructure.
    """
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = True
    mocker.patch("app.release_qualifier.promote", new=AsyncMock())

    project, version = await _make_project_and_version(db_session)

    async def _run_independent_blocker():
        audit_task, _smoke = await _seed_qualification_children(
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
                "tests": [{"name": "a", "status": "pass"}],
                "suites": {"startup": {"passed": True}},
            },
        )
        await _seed_challenge_artifact(
            db_session,
            project,
            version,
            audit_task=audit_task,
            verdicts=[
                {
                    "verdict": "confirmed",
                    "severity": "high",
                    "summary": "Auth bypass in admin panel",
                    "reason": "session token not validated server-side",
                }
            ],
        )
        result = await rq._finalize_qualification(db_session, version)
        await db_session.commit()
        await db_session.refresh(version)
        return result

    # Run three independent-blocker holds (matches _MIN_INFRA_ERROR_STREAK).
    for _ in range(3):
        result = await _run_independent_blocker()
        assert result["status"] == "not_promoted"
        lq = version.last_qualification
        assert (
            "consecutive_infra_errors" not in lq
            or lq.get("consecutive_infra_errors") is None
        )

    # No infra Alert should have been raised despite three runs.
    from app.models import Alert

    alert_count = (
        await db_session.execute(
            select(sa_func.count()).where(Alert.project_id == project.id)
        )
    ).scalar()
    assert alert_count == 0, (
        f"expected 0 Alert rows but found {alert_count} after 3 independent-blocker "
        "holds — must not trigger infra backstop alert"
    )


@pytest.mark.asyncio
async def test_finalize_tags_candidate_side_blocker_execution_site(db_session, mocker):
    """A candidate-side blocker (test_file_deleted) is surfaced execution_site='candidate'.

    Acceptance evidence (dir: tag promotion blockers inert-until-promote): a
    blocker whose responsible check runs against CANDIDATE content — here a
    deleted test file the audit surfaced — must be tagged as clearable by an
    automain fix (execution_site='candidate') on both the finalize return dict
    and the persisted last_qualification, and the per-dimension ledger detail.
    """
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = True
    mocker.patch("app.release_qualifier.promote", new=AsyncMock())

    project, version = await _make_project_and_version(db_session)
    # A deleted test file classified genuine_erosion nulls test_erosion, driving
    # compute_composite to the test_file_deleted hard blocker (candidate-side).
    await _seed_qualification_children(
        db_session,
        project,
        version,
        audit_logs={
            "audit_score": 1.0,
            "findings": [],
            "test_erosion": {
                "deleted_test_files": ["testing/test_gone.py"],
                "assertion_delta": 0,
                "test_count_delta": -5,
            },
            "deleted_test_file_classifications": [
                {
                    "path": "testing/test_gone.py",
                    "classification": "genuine_erosion",
                    "rationale": "Subject module present and unchanged; test removed to stop a failing build.",
                }
            ],
        },
        smoke_logs={
            "passed": True,
            "tests": [{"name": "a", "status": "pass"}],
            "suites": {"startup": {"passed": True}},
        },
    )

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()
    await db_session.refresh(version)

    assert result["blocker"] == "test_file_deleted"
    assert result["execution_site"] == "candidate"

    lq = version.last_qualification
    assert lq["blocker"] == "test_file_deleted"
    assert lq["execution_site"] == "candidate"


@pytest.mark.asyncio
async def test_ledger_stores_post_reconciliation_audit_score(db_session, mocker):
    """Finding 7c6c3428ff6afee2: the per-dimension audit ledger row must reflect
    the POST-challenge-reconciliation audit score, not the pre-reconciliation one.

    Scenario: the primary audit surfaces one verified HIGH finding (a 0.15
    deduction → pre-reconciliation audit_score == 0.85), and the challenge
    REFUTES it with a substantive reason.  Reconciliation removes the refuted
    finding, so the score the composite actually uses rises to 1.0.

    Before the fix, an eager upsert wrote the 0.85/"failed" row BEFORE the
    challenge gate ran; the skip-if-RESOLVED guard then froze it, so the durable
    ledger permanently stored 0.85 while last_qualification carried 1.0 — and the
    read-side sub_scores overlay clobbered the reconciled 1.0 back down to 0.85.
    The refresh must leave the ledger at the reconciled 1.0/"passed".
    """
    from app import shared_state

    # DRY-RUN: _finalize_qualification RETURNS the composite dict on this path.
    # With promotion_enabled=True it instead returns whatever the mocked promote()
    # returns — a bare AsyncMock — so `result["sub_scores"]["audit"]` read back a
    # MagicMock and the assertion could never be satisfied.  The reconciliation +
    # per-dimension ledger write this test exercises both run on the dry-run path.
    shared_state.runtime_config["promotion_enabled"] = False
    mocker.patch("app.release_qualifier.promote", new=AsyncMock())

    project, version = await _make_project_and_version(db_session)
    _high_summary = "SQL injection in report export query builder"
    await _seed_qualification_children(
        db_session,
        project,
        version,
        audit_logs={
            "audit_score": 0.85,
            "findings": [
                {
                    "severity": "high",
                    "summary": _high_summary,
                    "file": "app/reports.py",
                    "verification_note": "confirmed via full repo scan with supporting evidence",
                }
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
        # The challenger refutes the single HIGH finding, so reconciliation drops
        # it and the reconciled audit score rises from 0.85 back to 1.0.
        challenge_verdicts=[
            {
                "primary_finding_index": 1,
                "verdict": "refuted_false_positive",
                "severity": "high",
                "summary": _high_summary,
                "reason": "the export path uses parameterized queries throughout; no raw interpolation",
            }
        ],
    )

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()
    await db_session.refresh(version)

    # The composite must have scored the reconciled (refuted-removed) report: 1.0.
    # `_finalize_qualification`'s RETURN carries only status/confidence/blocker —
    # the per-dimension sub_scores are persisted on last_qualification, which is
    # where the reconciled audit score is actually observable.
    assert result["status"] == "dry_run", result
    _sub = (version.last_qualification or {}).get("sub_scores") or {}
    assert _sub.get("audit") == pytest.approx(1.0), (
        f"expected reconciled audit sub_score 1.0 but got {_sub.get('audit')!r}"
    )

    # The durable per-dimension ledger row must carry the POST-reconciliation
    # value, not the pre-reconciliation 0.85 the eager upsert wrote.
    audit_row = (
        await db_session.execute(
            select(QualificationDimensionResult).where(
                QualificationDimensionResult.release_version_id == version.id,
                QualificationDimensionResult.dimension == "audit",
            )
        )
    ).scalar_one()
    assert audit_row.sub_score == pytest.approx(1.0), (
        f"ledger audit sub_score is {audit_row.sub_score!r} (pre-reconciliation "
        "value leaked) — must be the post-reconciliation 1.0"
    )
    assert audit_row.status == "passed", (
        f"ledger audit status is {audit_row.status!r} — a refuted-away deduction "
        "must leave the dimension 'passed', not the pre-reconciliation 'failed'"
    )

    # And the read-side sub_scores overlay must now agree (it reads the ledger),
    # so the top-level state and the per-dimension breakdown tell one story.
    await db_session.refresh(version)
    lq = version.last_qualification
    assert lq["sub_scores"]["audit"] == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_finalize_discharges_hotfix_backfill_debt_on_passing_gate(
    db_session, mocker
):
    """A passing normal qualification gate discharges the write-only
    hotfix_deploy_pending_backfill debt the operator hotfix-deploy channel opened.

    The hotfix-deploy channel mints a hotfix_deploy_pending_backfill completion-debt
    row recording that a fast-tracked release-machinery promotion bypassed the
    audit/scoring gate and is owed re-qualification by the NORMAL gate.  Nothing
    used to resolve it, so it stayed open forever and permanently dragged the
    debt-pressure sub-score down.  When the normal gate composite clears
    (promote=True, here on the dry-run path so no real promote() is needed), the
    owed re-qualification has demonstrably happened and the row must flip to
    resolved with a reason.
    """
    from app import shared_state
    from app.completion_debt import STATUS_OPEN, STATUS_RESOLVED

    # Dry-run path: promote=True but promotion_enabled=False, so finalize returns
    # 'dry_run' without entering promote() — exactly the composite["promote"] gate
    # the discharge hooks onto.
    shared_state.runtime_config["promotion_enabled"] = False
    mocker.patch("app.release_qualifier.promote", new=AsyncMock())

    project, version = await _make_project_and_version(db_session)
    await _open_hotfix_backfill_debt(db_session, project, "abc12345" + "0" * 32)

    # Sanity: the debt is open before the gate runs.
    row = await _hotfix_backfill_row(db_session, project, "abc12345" + "0" * 32)
    assert row.status == STATUS_OPEN

    await _seed_qualification_children(
        db_session,
        project,
        version,
        audit_logs={"audit_score": 1.0, "findings": []},
        smoke_logs={
            "passed": True,
            "tests": [{"name": "a", "status": "pass"}],
            "suites": {"startup": {"passed": True}},
        },
    )

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    assert result["status"] == "dry_run", result

    row = await _hotfix_backfill_row(db_session, project, "abc12345" + "0" * 32)
    assert row.status == STATUS_RESOLVED, (
        f"expected hotfix-backfill debt resolved on passing gate, got {row.status!r}"
    )
    assert row.resolved_at is not None
    assert "re-qualified by normal gate" in (row.resolution_reason or ""), (
        f"resolution_reason must record the discharge, got {row.resolution_reason!r}"
    )


@pytest.mark.asyncio
async def test_finalize_leaves_hotfix_backfill_debt_open_when_not_promoted(
    db_session, mocker
):
    """A not-promoted (blocked) normal gate must NOT discharge the hotfix-backfill
    debt — the owed re-qualification has not succeeded, so the obligation stands."""
    from app import shared_state
    from app.completion_debt import STATUS_OPEN

    shared_state.runtime_config["promotion_enabled"] = True
    mocker.patch("app.release_qualifier.promote", new=AsyncMock())

    project, version = await _make_project_and_version(db_session)
    await _open_hotfix_backfill_debt(db_session, project, "deadbeef" + "0" * 32)

    # A deleted test file classified genuine_erosion drives compute_composite to
    # the test_file_deleted hard blocker → not_promoted, so composite["promote"]
    # is False and no discharge fires.
    await _seed_qualification_children(
        db_session,
        project,
        version,
        audit_logs={
            "audit_score": 1.0,
            "findings": [],
            "test_erosion": {
                "deleted_test_files": ["testing/test_gone.py"],
                "assertion_delta": 0,
                "test_count_delta": -5,
            },
            "deleted_test_file_classifications": [
                {
                    "path": "testing/test_gone.py",
                    "classification": "genuine_erosion",
                    "rationale": "Subject module present and unchanged; test removed to stop a failing build.",
                }
            ],
        },
        smoke_logs={
            "passed": True,
            "tests": [{"name": "a", "status": "pass"}],
            "suites": {"startup": {"passed": True}},
        },
    )

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    assert result["status"] == "not_promoted", result
    assert result["blocker"] == "test_file_deleted", result

    row = await _hotfix_backfill_row(db_session, project, "deadbeef" + "0" * 32)
    assert row.status == STATUS_OPEN, (
        f"a not-promoted gate must leave the hotfix-backfill debt OPEN, got {row.status!r}"
    )


@pytest.mark.asyncio
async def test_aresolve_hotfix_backfill_scopes_to_project_and_kind(db_session):
    """aresolve_hotfix_deploy_backfill_debts resolves ONLY open hotfix-deploy rows
    for the target project — skipping dismissed rows, other kinds, other projects,
    and returning the count resolved.  None inputs are a safe no-op (returns 0)."""
    from app import completion_debt as cd

    project, _ = await _make_project_and_version(db_session)
    other, _ = await _make_project_and_version(db_session)

    # Two open hotfix-backfill rows for the target project (a project can
    # accumulate several fast-tracks before the next normal gate runs).
    await cd.arecord_completion_debt(
        db_session,
        dedupe_key=f"hotfix_deploy:{project.id}:sha_one",
        kind=cd.KIND_HOTFIX_DEPLOY_PENDING_BACKFILL,
        title="hotfix one",
        project_id=project.id,
    )
    await cd.arecord_completion_debt(
        db_session,
        dedupe_key=f"hotfix_deploy:{project.id}:sha_two",
        kind=cd.KIND_HOTFIX_DEPLOY_PENDING_BACKFILL,
        title="hotfix two",
        project_id=project.id,
    )
    # A dismissed hotfix row for the same project — must NOT be reopened/resolved.
    await cd.arecord_completion_debt(
        db_session,
        dedupe_key=f"hotfix_deploy:{project.id}:sha_dismissed",
        kind=cd.KIND_HOTFIX_DEPLOY_PENDING_BACKFILL,
        title="hotfix dismissed",
        project_id=project.id,
    )
    # A different debt kind for the same project — out of scope.
    await cd.arecord_completion_debt(
        db_session,
        dedupe_key=f"no_ci_gate:{project.id}:x",
        kind=cd.KIND_NO_CI_GATE,
        title="unrelated",
        project_id=project.id,
    )
    # A hotfix row for a DIFFERENT project — out of scope.
    await cd.arecord_completion_debt(
        db_session,
        dedupe_key=f"hotfix_deploy:{other.id}:sha_other",
        kind=cd.KIND_HOTFIX_DEPLOY_PENDING_BACKFILL,
        title="other project hotfix",
        project_id=other.id,
    )
    await db_session.commit()

    dismissed_row = (
        await db_session.execute(
            select(CompletionDebtEntry).where(
                CompletionDebtEntry.dedupe_key
                == f"hotfix_deploy:{project.id}:sha_dismissed"
            )
        )
    ).scalar_one()
    dismissed_row.status = cd.STATUS_DISMISSED
    db_session.add(dismissed_row)
    await db_session.commit()

    resolved = await cd.aresolve_hotfix_deploy_backfill_debts(
        db_session, project.id, reason="unit-test discharge"
    )
    await db_session.commit()
    assert resolved == 2, f"expected exactly the 2 open target-project rows, got {resolved}"

    def _status(key):
        return (
            db_session.execute(
                select(CompletionDebtEntry.status).where(
                    CompletionDebtEntry.dedupe_key == key
                )
            )
        )

    assert (
        await _status(f"hotfix_deploy:{project.id}:sha_one")
    ).scalar_one() == cd.STATUS_RESOLVED
    assert (
        await _status(f"hotfix_deploy:{project.id}:sha_two")
    ).scalar_one() == cd.STATUS_RESOLVED
    # Dismissed stays dismissed; other-kind and other-project stay open.
    assert (
        await _status(f"hotfix_deploy:{project.id}:sha_dismissed")
    ).scalar_one() == cd.STATUS_DISMISSED
    assert (
        await _status(f"no_ci_gate:{project.id}:x")
    ).scalar_one() == cd.STATUS_OPEN
    assert (
        await _status(f"hotfix_deploy:{other.id}:sha_other")
    ).scalar_one() == cd.STATUS_OPEN

    # None inputs are a safe no-op.
    assert await cd.aresolve_hotfix_deploy_backfill_debts(None, project.id) == 0
    assert await cd.aresolve_hotfix_deploy_backfill_debts(db_session, None) == 0


class TestReconcileRefutedJustificationGate:
    """Backend reason-substantiveness gate for refuted_false_positive verdicts.

    A refuted_false_positive verdict without a substantive reason (< 20
    non-whitespace characters) must fail closed with audit_challenge_uncertain,
    mirroring the agent-side normalizeChallengeOutput contract.
    """

    SHA = "cafebabe"

    def _report(self, findings: list) -> dict:
        return {"findings": findings}

    def _verified(self, severity: str, summary: str) -> dict:
        return {
            "severity": severity,
            "summary": summary,
            "verification_note": "confirmed via full repo scan with supporting evidence",
        }

    def test_refuted_challenge_without_reason_cannot_cancel_finding(self):
        """A refuted_false_positive with no reason key must not remove the finding."""
        finding = self._verified(
            "high", "Unvalidated redirect allows open redirect attack"
        )
        envelope = _make_challenge_envelope(
            self.SHA,
            [
                {
                    "verdict": "refuted_false_positive",
                    "severity": "high",
                    "summary": "Unvalidated redirect allows open redirect attack",
                }
            ],
        )
        hold, modified, refuted, _binding = rq._reconcile_audit_with_challenge(
            self._report([finding]), envelope, self.SHA
        )
        assert hold == "audit_challenge_uncertain"
        assert modified is None
        assert refuted == []

    def test_refuted_missing_reason_fails_closed(self):
        """A matched refuted_false_positive with no reason key returns audit_challenge_uncertain."""
        finding = self._verified("medium", "SQL injection risk in query builder")
        envelope = _make_challenge_envelope(
            self.SHA,
            [
                {
                    "verdict": "refuted_false_positive",
                    "severity": "medium",
                    "summary": "SQL injection risk in query builder",
                }
            ],
        )
        hold, modified, refuted, _binding = rq._reconcile_audit_with_challenge(
            self._report([finding]), envelope, self.SHA
        )
        assert hold == "audit_challenge_uncertain"
        assert modified is None
        assert refuted == []

    def test_refuted_short_reason_fails_closed(self):
        """A reason with 19 non-whitespace chars is below the threshold and fails closed."""
        # "short: ok ok ok ok" → s(1)h(1)o(1)r(1)t(1):(1)o(1)k(1)o(1)k(1)o(1)k(1)o(1)k(1) = 14; pad to 19
        reason_19 = "a b c d e f g h i j k l m n o p q r s"  # 19 letters
        assert sum(1 for c in reason_19 if not c.isspace()) == 19
        finding = self._verified("high", "Hardcoded API key exposed in source")
        envelope = _make_challenge_envelope(
            self.SHA,
            [
                {
                    "verdict": "refuted_false_positive",
                    "severity": "high",
                    "summary": "Hardcoded API key exposed in source",
                    "reason": reason_19,
                }
            ],
        )
        hold, modified, refuted, _binding = rq._reconcile_audit_with_challenge(
            self._report([finding]), envelope, self.SHA
        )
        assert hold == "audit_challenge_uncertain"
        assert modified is None
        assert refuted == []

    def test_refuted_exactly_20_nw_chars_accepted(self):
        """A reason with exactly 20 non-whitespace chars meets the threshold and is accepted."""
        reason_20 = "a b c d e f g h i j k l m n o p q r s t"  # 20 letters
        assert sum(1 for c in reason_20 if not c.isspace()) == 20
        finding = self._verified(
            "medium", "Missing CSRF token on state-changing endpoint"
        )
        envelope = _make_challenge_envelope(
            self.SHA,
            [
                {
                    "verdict": "refuted_false_positive",
                    "severity": "medium",
                    "summary": "Missing CSRF token on state-changing endpoint",
                    "reason": reason_20,
                }
            ],
        )
        hold, modified, refuted, _binding = rq._reconcile_audit_with_challenge(
            self._report([finding]), envelope, self.SHA
        )
        assert hold is None
        assert modified is not None
        assert modified["findings"] == []
        assert len(refuted) == 1

    def test_refuted_substantive_reason_accepted(self):
        """A refuted_false_positive with a clearly substantive reason removes the finding."""
        finding = self._verified("high", "Path traversal in file upload handler")
        envelope = _make_challenge_envelope(
            self.SHA,
            [
                {
                    "verdict": "refuted_false_positive",
                    "severity": "high",
                    "summary": "Path traversal in file upload handler",
                    "reason": "all file paths are validated against an allowlist before use",
                }
            ],
        )
        hold, modified, refuted, _binding = rq._reconcile_audit_with_challenge(
            self._report([finding]), envelope, self.SHA
        )
        assert hold is None
        assert modified is not None
        assert modified["findings"] == []
        assert len(refuted) == 1
        assert refuted[0]["summary"] == "Path traversal in file upload handler"

    def test_refuted_false_positive_subset_summary_does_not_misbind_same_severity_finding(
        self,
    ):
        """A short subset-summary refutation must remove only its closest finding.

        The refutation tokens are a strict subset of both findings. Under the old
        min-denominator scorer it would score 1.0 against both and, because it is
        emitted before the fuzzy confirmation, bind to finding A first. Jaccard
        keeps finding A below threshold while allowing the closer finding B match.
        """
        finding_a = self._verified(
            "high",
            "Unbounded SQL query parameter injection via user input search endpoint without sanitization",
        )
        finding_b = self._verified(
            "high",
            "SQL injection via query parameter search endpoint",
        )
        report = self._report([finding_a, finding_b])

        verdicts = [
            {
                "verdict": "refuted_false_positive",
                "severity": "high",
                "summary": "SQL injection via query parameter",
                "reason": "query parameter is sanitized via ORM layer before database execution",
            },
            {
                "verdict": "confirmed",
                "summary": "Unbounded SQL user input checked",
            },
        ]
        envelope = _make_challenge_envelope(self.SHA, verdicts)

        hold, modified, refuted, _binding = rq._reconcile_audit_with_challenge(
            report, envelope, self.SHA
        )

        assert hold is None
        assert modified is not None
        assert [f["summary"] for f in modified["findings"]] == [
            "Unbounded SQL query parameter injection via user input search endpoint without sanitization"
        ]
        assert len(refuted) == 1
        assert refuted[0]["summary"] == (
            "SQL injection via query parameter search endpoint"
        )


class TestBindingRecordAndUnboundHoldDistinction:
    """Tests for binding record persistence and hold-code disambiguation.

    Requirement: audit_challenge_binding_incomplete (verdicts present but
    assignment incomplete) must be distinct from audit_challenge_missing
    (artifact absent).  A successful assignment must produce an auditable
    binding record with per-finding entries.
    """

    SHA = "bead1234"

    def _report(self, findings: list) -> dict:
        return {"findings": findings}

    def _verified(self, severity: str, summary: str, file: str = "") -> dict:
        entry: dict = {
            "severity": severity,
            "summary": summary,
            "verification_note": "confirmed via full repo scan with supporting evidence",
        }
        if file:
            entry["file"] = file
        return entry

    def test_binding_record_persisted_and_unbound_hold_distinguished_from_absent(self):
        """Acceptance test: binding record populated on merge-verdict; unbound hold
        uses audit_challenge_binding_incomplete (not audit_challenge_missing).

        Step A — successful reconciliation with one confirmed and one refuted finding:
        a binding_record entry exists for each deductible finding, with the correct
        verdict value and summary.

        Step B — one finding but no matching verdict (topic mismatch): the hold code
        is audit_challenge_binding_incomplete, not audit_challenge_missing (which is
        reserved for artifact-absent/SHA-mismatch cases).
        """
        # ---- Step A: full assignment, binding record populated ----
        finding_a = self._verified(
            "high", "SQL injection via unsanitized query parameter", "backend/db.py"
        )
        finding_b = self._verified(
            "high", "Hardcoded API secret in config file", "backend/config.py"
        )
        report = self._report([finding_a, finding_b])

        verdicts = [
            {
                "verdict": "confirmed",
                "severity": "high",
                "summary": "SQL injection via unsanitized query parameter",
            },
            {
                "verdict": "refuted_false_positive",
                "severity": "high",
                "summary": "Hardcoded API secret in config file",
                "reason": "secret is loaded from environment variable at runtime",
            },
        ]
        envelope = _make_challenge_envelope(self.SHA, verdicts)
        hold, modified, refuted, binding = rq._reconcile_audit_with_challenge(
            report, envelope, self.SHA
        )

        assert hold is None, f"expected no hold, got {hold!r}"
        assert modified is not None
        assert len(refuted) == 1

        # Binding record must have one entry per deductible finding.
        assert len(binding) == 2, f"expected 2 binding entries, got {len(binding)}"
        confirmed_entry = next(
            (e for e in binding if e["verdict"] == "confirmed"), None
        )
        refuted_entry = next(
            (e for e in binding if e["verdict"] == "refuted_false_positive"), None
        )
        assert confirmed_entry is not None
        assert confirmed_entry["finding_severity"] == "high"
        assert "SQL injection" in confirmed_entry["finding_summary"]
        assert refuted_entry is not None
        assert "Hardcoded API secret" in refuted_entry["finding_summary"]
        # verdict_summary is populated from the challenger's summary of the finding.
        assert "Hardcoded API secret" in refuted_entry["verdict_summary"]

        # ---- Step B: verdicts present but no matching verdict → binding_incomplete ----
        finding_unmatched = self._verified(
            "high", "Cross-site request forgery in admin panel"
        )
        report_b = self._report([finding_unmatched])
        # Verdict summary shares only one token ("admin") — below the 0.6 threshold.
        verdicts_b = [
            {
                "verdict": "confirmed",
                "severity": "high",
                "summary": "SQL injection admin endpoint",
            }
        ]
        envelope_b = _make_challenge_envelope(self.SHA, verdicts_b)
        hold_b, modified_b, _refuted_b, binding_b = rq._reconcile_audit_with_challenge(
            report_b, envelope_b, self.SHA
        )

        # Verdicts ARE present and SHA matched — must use binding_incomplete, not missing.
        assert hold_b == "audit_challenge_binding_incomplete", (
            f"expected 'audit_challenge_binding_incomplete', got {hold_b!r}"
        )
        assert modified_b is None
        assert binding_b == []

        # Artifact-absent case must still use audit_challenge_missing.
        hold_absent, _, _, _ = rq._reconcile_audit_with_challenge(
            report_b, None, self.SHA
        )
        assert hold_absent == "audit_challenge_missing", (
            f"artifact-absent should be 'audit_challenge_missing', got {hold_absent!r}"
        )

    def test_binding_incomplete_in_binding_holds_frozenset(self):
        """audit_challenge_binding_incomplete must trigger challenge respawn."""
        assert "audit_challenge_binding_incomplete" in rq._AUDIT_CHALLENGE_BINDING_HOLDS

    def test_empty_verdicts_in_binding_holds_frozenset(self):
        """audit_challenge_empty_verdicts must trigger challenge respawn."""
        assert "audit_challenge_empty_verdicts" in rq._AUDIT_CHALLENGE_BINDING_HOLDS

    def test_binding_record_capped_at_binding_record_cap(self):
        """Binding record must not exceed _BINDING_RECORD_CAP entries."""
        cap = rs._BINDING_RECORD_CAP
        findings = [
            self._verified(
                "high", f"Finding {i} with enough tokens to match verdict {i}"
            )
            for i in range(cap + 5)
        ]
        verdicts = [
            {
                "verdict": "confirmed",
                "severity": "high",
                "summary": f"Finding {i} with enough tokens to match verdict {i}",
            }
            for i in range(cap + 5)
        ]
        report = self._report(findings)
        envelope = _make_challenge_envelope(self.SHA, verdicts)
        _hold, _modified, _refuted, binding = rq._reconcile_audit_with_challenge(
            report, envelope, self.SHA
        )
        assert len(binding) <= cap, (
            f"binding record exceeds cap: {len(binding)} > {cap}"
        )


class TestBindingRecordBindingMethod:
    """binding_record entries must carry a binding_method field ('indexed' or 'fuzzy').

    'indexed' when the verdict was bound via primary_finding_index; 'fuzzy' when
    bound via the bipartite fuzzy-matching path.
    """

    SHA = "cafe1234"

    def _verified(self, severity: str, summary: str, file: str = "") -> dict:
        entry: dict = {
            "severity": severity,
            "summary": summary,
            "verification_note": "confirmed via full repo scan with supporting evidence",
        }
        if file:
            entry["file"] = file
        return entry

    def test_binding_method_indexed(self):
        """When verdict carries primary_finding_index, binding_method is 'indexed'."""
        finding = self._verified(
            "high", "SQL injection via unsanitized query parameter"
        )
        report = {"findings": [finding]}
        verdicts = [
            {
                "verdict": "confirmed",
                "severity": "high",
                "summary": "SQL injection via unsanitized query parameter",
                "primary_finding_index": 1,
            }
        ]
        envelope = _make_challenge_envelope(self.SHA, verdicts)
        hold, modified, _refuted, binding = rq._reconcile_audit_with_challenge(
            report, envelope, self.SHA
        )
        assert hold is None, f"unexpected hold: {hold!r}"
        assert len(binding) == 1
        assert binding[0]["binding_method"] == "indexed", (
            f"expected 'indexed', got {binding[0].get('binding_method')!r}"
        )

    def test_binding_method_fuzzy(self):
        """When verdict has no primary_finding_index, binding_method is 'fuzzy'."""
        finding = self._verified("high", "Hardcoded API secret in config file")
        report = {"findings": [finding]}
        verdicts = [
            {
                "verdict": "confirmed",
                "severity": "high",
                "summary": "Hardcoded API secret in config file",
                # no primary_finding_index
            }
        ]
        envelope = _make_challenge_envelope(self.SHA, verdicts)
        hold, modified, _refuted, binding = rq._reconcile_audit_with_challenge(
            report, envelope, self.SHA
        )
        assert hold is None, f"unexpected hold: {hold!r}"
        assert len(binding) == 1
        assert binding[0]["binding_method"] == "fuzzy", (
            f"expected 'fuzzy', got {binding[0].get('binding_method')!r}"
        )


class TestChallengedVsUnchallengedTelemetry:
    """Tests for compute_challenged_vs_unchallenged_telemetry (pure function)."""

    def _make_verdict(
        self,
        *,
        challenged: bool = False,
        candidate_sha: str = "feed" * 10,
        challenged_candidate_sha: str | None = None,
        reported_agreement: str | None = "agree",
        excluded_findings_count: int = 0,
        would_promote: bool = False,
        terminal_status: str = "not_promoted",
        post_promotion_outcome: str | None = None,
    ) -> dict:
        recon: dict = {
            "model_reported_score": 1.0,
            "findings_derived_score": 1.0,
            "score_delta": 0.0,
            "excluded_findings_count": excluded_findings_count,
        }
        if challenged:
            recon["audit_challenge"] = {
                "status": "available",
                "reported_agreement": reported_agreement,
                "challenge_severity": "low",
                "challenged_candidate_sha": challenged_candidate_sha or candidate_sha,
            }
        else:
            recon["audit_challenge"] = {
                "status": "unavailable",
                "reason": "challenge_absent",
                "reported_agreement": None,
            }
        return {
            "candidate_sha": candidate_sha,
            "audit_reconciliation": recon,
            "would_promote": would_promote,
            "terminal_status": terminal_status,
            "post_promotion_outcome": post_promotion_outcome,
            "excluded_audit_findings": None,
        }

    def test_empty_list_returns_insufficient_sample_both_cohorts(self):
        result = rq.compute_challenged_vs_unchallenged_telemetry([])
        assert result["total_verdicts"] == 0
        assert result["challenged"]["insufficient_sample"] is True
        assert result["challenged"]["sample_size"] == 0
        assert result["unchallenged"]["insufficient_sample"] is True
        assert result["unchallenged"]["sample_size"] == 0

    def test_single_challenged_verdict_insufficient_sample(self):
        verdicts = [self._make_verdict(challenged=True)]
        result = rq.compute_challenged_vs_unchallenged_telemetry(verdicts)
        assert result["challenged"]["insufficient_sample"] is True
        assert result["challenged"]["sample_size"] == 1
        # unchallenged cohort is empty
        assert result["unchallenged"]["insufficient_sample"] is True
        assert result["unchallenged"]["sample_size"] == 0

    def test_cohort_of_one_is_insufficient_sample(self):
        # Exactly one challenged, one unchallenged — both below _MIN_TELEMETRY_SAMPLE.
        verdicts = [
            self._make_verdict(challenged=True),
            self._make_verdict(challenged=False),
        ]
        result = rq.compute_challenged_vs_unchallenged_telemetry(verdicts)
        assert result["challenged"]["insufficient_sample"] is True
        assert result["unchallenged"]["insufficient_sample"] is True

    def test_disagreement_rate_challenged_cohort(self):
        # 3 challenged verdicts: 2 disagree, 1 agrees.
        # 3 unchallenged verdicts (no disagreement field).
        verdicts = [
            self._make_verdict(challenged=True, reported_agreement="disagree"),
            self._make_verdict(challenged=True, reported_agreement="partial"),
            self._make_verdict(challenged=True, reported_agreement="agree"),
            self._make_verdict(challenged=False),
            self._make_verdict(challenged=False),
            self._make_verdict(challenged=False),
        ]
        result = rq.compute_challenged_vs_unchallenged_telemetry(verdicts)
        ch = result["challenged"]
        assert ch["insufficient_sample"] is False
        assert ch["sample_size"] == 3
        assert ch["disagreement_count"] == 2
        assert ch["disagreement_rate"] == pytest.approx(2 / 3, abs=1e-4)
        # unchallenged cohort has no disagreement keys
        unch = result["unchallenged"]
        assert unch["insufficient_sample"] is False
        assert "disagreement_rate" not in unch
        assert "disagreement_count" not in unch

    def test_exclusion_rate_computation(self):
        # 2 of 3 challenged verdicts have excluded_findings_count > 0.
        verdicts = [
            self._make_verdict(challenged=True, excluded_findings_count=2),
            self._make_verdict(challenged=True, excluded_findings_count=1),
            self._make_verdict(challenged=True, excluded_findings_count=0),
            self._make_verdict(challenged=False, excluded_findings_count=0),
            self._make_verdict(challenged=False, excluded_findings_count=0),
            self._make_verdict(challenged=False, excluded_findings_count=3),
        ]
        result = rq.compute_challenged_vs_unchallenged_telemetry(verdicts)
        ch = result["challenged"]
        assert ch["exclusion_count"] == 2
        assert ch["exclusion_rate"] == pytest.approx(2 / 3, abs=1e-4)
        unch = result["unchallenged"]
        assert unch["exclusion_count"] == 1
        assert unch["exclusion_rate"] == pytest.approx(1 / 3, abs=1e-4)

    def test_wrong_decision_rate_deploy_failed(self):
        # 2 of 3 promoted challenged verdicts ended in deploy_failed.
        verdicts = [
            self._make_verdict(
                challenged=True,
                would_promote=True,
                terminal_status="promoted",
                post_promotion_outcome="deploy_failed",
            ),
            self._make_verdict(
                challenged=True,
                would_promote=True,
                terminal_status="promoted",
                post_promotion_outcome="deploy_failed",
            ),
            self._make_verdict(
                challenged=True,
                would_promote=True,
                terminal_status="promoted",
                post_promotion_outcome="serving_confirmed",
            ),
        ]
        result = rq.compute_challenged_vs_unchallenged_telemetry(verdicts)
        ch = result["challenged"]
        assert ch["insufficient_sample"] is False
        assert ch["wrong_decision_resolved_sample"] == 3
        assert ch["wrong_decision_rate"] == pytest.approx(2 / 3, abs=1e-4)

    def test_unresolved_outcomes_excluded_from_wrong_decision(self):
        # Verdicts with unconfirmed or None outcome are not in the resolved sample.
        verdicts = [
            self._make_verdict(
                challenged=True,
                terminal_status="promoted",
                post_promotion_outcome="unconfirmed",
            ),
            self._make_verdict(
                challenged=True,
                terminal_status="promoted",
                post_promotion_outcome=None,
            ),
            self._make_verdict(
                challenged=True,
                terminal_status="not_promoted",
                post_promotion_outcome=None,
            ),
        ]
        result = rq.compute_challenged_vs_unchallenged_telemetry(verdicts)
        ch = result["challenged"]
        assert ch["insufficient_sample"] is False
        # No resolved outcomes in the promoted subset
        assert ch["wrong_decision_resolved_sample"] == 0
        assert ch["wrong_decision_rate"] is None

    def test_partition_null_audit_reconciliation_goes_unchallenged(self):
        # A verdict with audit_reconciliation=None falls into the unchallenged cohort.
        verdicts = [
            {
                "audit_reconciliation": None,
                "would_promote": False,
                "terminal_status": "not_promoted",
                "post_promotion_outcome": None,
                "excluded_audit_findings": None,
            },
            {
                "audit_reconciliation": None,
                "would_promote": False,
                "terminal_status": "not_promoted",
                "post_promotion_outcome": None,
                "excluded_audit_findings": None,
            },
            {
                "audit_reconciliation": None,
                "would_promote": False,
                "terminal_status": "not_promoted",
                "post_promotion_outcome": None,
                "excluded_audit_findings": None,
            },
        ]
        result = rq.compute_challenged_vs_unchallenged_telemetry(verdicts)
        assert result["challenged"]["insufficient_sample"] is True
        assert result["challenged"]["sample_size"] == 0
        assert result["unchallenged"]["sample_size"] == 3
        assert result["unchallenged"]["insufficient_sample"] is False

    def test_total_verdicts_count(self):
        verdicts = [
            self._make_verdict(challenged=True),
            self._make_verdict(challenged=False),
            self._make_verdict(challenged=False),
        ]
        result = rq.compute_challenged_vs_unchallenged_telemetry(verdicts)
        assert result["total_verdicts"] == 3
        assert result["challenged"]["sample_size"] == 1
        assert result["unchallenged"]["sample_size"] == 2

    def test_no_division_by_zero_on_empty_resolved_subset(self):
        # All 3 verdicts are not_promoted — no promoted rows, resolved_sample=0.
        verdicts = [
            self._make_verdict(challenged=True, terminal_status="not_promoted"),
            self._make_verdict(challenged=True, terminal_status="not_promoted"),
            self._make_verdict(challenged=True, terminal_status="not_promoted"),
        ]
        result = rq.compute_challenged_vs_unchallenged_telemetry(verdicts)
        ch = result["challenged"]
        assert ch["wrong_decision_rate"] is None
        assert ch["wrong_decision_resolved_sample"] == 0

    def test_challenge_refuted_rate_distinct_from_exclusion(self):
        """challenge_refuted_count/rate aggregates challenger refuted_count from
        audit_challenge; it is distinct from exclusion_rate and does NOT perturb it.

        This is the required acceptance test: real merge-test verdict shape with
        refuted_count > 0, asserting challenge_refuted_rate is set correctly and
        exclusion_rate is unchanged.
        """

        # 3 challenged verdicts with binding synthesis shape (as produced by
        # _compute_challenge_synthesis_from_binding_verdicts after a real merge-test).
        # Verdict A: confirmed=1, refuted=1, uncertain=0, excluded=0
        # Verdict B: confirmed=1, refuted=2, uncertain=0, excluded=1
        # Verdict C: confirmed=1, refuted=0, uncertain=0, excluded=0
        # Total refuted = 3, total reviewed = (1+1+0)+(1+2+0)+(1+0+0) = 6
        # → challenge_refuted_rate = 3/6 = 0.5
        # exclusion_count = 1 (only B has excluded_findings_count > 0) → exclusion_rate = 1/3
        def _make_binding_verdict(
            refuted_count, excluded_findings_count, sha="abc1234"
        ):
            return {
                "candidate_sha": sha,
                "audit_reconciliation": {
                    "model_reported_score": 1.0,
                    "findings_derived_score": 1.0,
                    "score_delta": 0.0,
                    "excluded_findings_count": excluded_findings_count,
                    "audit_challenge": {
                        "status": "available",
                        "binding": True,
                        "confirmed_count": 1,
                        "refuted_count": refuted_count,
                        "uncertain_count": 0,
                        "reported_agreement": "agree"
                        if refuted_count == 0
                        else "partial",
                        "challenged_candidate_sha": sha,
                    },
                },
                "would_promote": True,
                "terminal_status": "not_promoted",
                "post_promotion_outcome": None,
                "excluded_audit_findings": None,
            }

        verdicts = [
            _make_binding_verdict(refuted_count=1, excluded_findings_count=0),
            _make_binding_verdict(refuted_count=2, excluded_findings_count=1),
            _make_binding_verdict(refuted_count=0, excluded_findings_count=0),
        ]
        result = rq.compute_challenged_vs_unchallenged_telemetry(verdicts)
        ch = result["challenged"]

        assert ch["insufficient_sample"] is False
        assert ch["sample_size"] == 3

        # challenge_refuted_count/rate: sum of refuted_count across challenged verdicts
        # divided by total findings reviewed (confirmed + refuted + uncertain)
        # Verdict A: 1+1+0=2 total reviewed
        # Verdict B: 1+2+0=3 total reviewed
        # Verdict C: 1+0+0=1 total reviewed
        # Rate = (1+2+0) / (2+3+1) = 3/6 = 0.5
        assert ch["challenge_refuted_count"] == 3
        assert ch["challenge_refuted_rate"] == pytest.approx(3 / 6, abs=1e-4)

        # exclusion_rate is driven by excluded_findings_count, not refuted_count
        assert ch["exclusion_count"] == 1
        assert ch["exclusion_rate"] == pytest.approx(1 / 3, abs=1e-4)

        # Rates are distinct — exclusion_rate != challenge_refuted_rate
        assert ch["exclusion_rate"] != ch["challenge_refuted_rate"]

        # Unchallenged cohort has no challenge_refuted fields
        assert "challenge_refuted_count" not in result["unchallenged"]
        assert "challenge_refuted_rate" not in result["unchallenged"]

    def test_challenge_refuted_rate_zero_when_no_refuted(self):
        """challenge_refuted_rate is 0.0 when all challenged verdicts have refuted_count=0."""
        verdicts = [
            {
                "candidate_sha": f"sha{i:04d}",
                "audit_reconciliation": {
                    "excluded_findings_count": 0,
                    "audit_challenge": {
                        "status": "available",
                        "confirmed_count": 1,
                        "refuted_count": 0,
                        "reported_agreement": "agree",
                        "challenged_candidate_sha": f"sha{i:04d}",
                    },
                },
                "would_promote": False,
                "terminal_status": "not_promoted",
                "post_promotion_outcome": None,
                "excluded_audit_findings": None,
            }
            for i in range(3)
        ]
        result = rq.compute_challenged_vs_unchallenged_telemetry(verdicts)
        ch = result["challenged"]
        assert ch["challenge_refuted_count"] == 0
        assert ch["challenge_refuted_rate"] == pytest.approx(0.0, abs=1e-4)

    def test_challenge_refuted_rate_absent_on_insufficient_sample(self):
        """challenge_refuted_count/rate are absent when cohort is insufficient_sample."""
        verdicts = [self._make_verdict(challenged=True)]  # only 1, below min sample
        result = rq.compute_challenged_vs_unchallenged_telemetry(verdicts)
        assert result["challenged"]["insufficient_sample"] is True
        assert "challenge_refuted_count" not in result["challenged"]
        assert "challenge_refuted_rate" not in result["challenged"]

    def test_available_challenge_with_mismatched_sha_goes_unchallenged(self):
        verdicts = [
            self._make_verdict(
                challenged=True,
                candidate_sha="feed" * 10,
                challenged_candidate_sha="beef" * 10,
            ),
            self._make_verdict(challenged=False),
            self._make_verdict(challenged=False),
        ]
        result = rq.compute_challenged_vs_unchallenged_telemetry(verdicts)
        assert result["challenged"]["sample_size"] == 0
        assert result["unchallenged"]["sample_size"] == 3

    def test_available_challenge_without_explicit_sha_stays_challenged(self):
        """Legacy binding challenge blobs without challenged_candidate_sha still count.

        The qualifier now treats an omitted challenged SHA as the current candidate
        rather than forcing a false mismatch, so older binding artifacts remain
        visible in the challenged cohort.
        """
        verdicts = [
            {
                "candidate_sha": "feed" * 10,
                "audit_reconciliation": {
                    "audit_challenge": {
                        "status": "available",
                        "reported_agreement": "agree",
                        "confirmed_count": 1,
                        "refuted_count": 0,
                        "uncertain_count": 0,
                    }
                },
                "would_promote": False,
                "terminal_status": "not_promoted",
                "post_promotion_outcome": None,
                "excluded_audit_findings": None,
            }
            for _ in range(3)
        ]
        result = rq.compute_challenged_vs_unchallenged_telemetry(verdicts)
        assert result["challenged"]["sample_size"] == 3
        assert result["unchallenged"]["sample_size"] == 0


def test_challenge_synthesis_rejects_mismatched_sha_and_counts_uncertain_as_non_agreement():
    candidate_sha = "feed" * 10
    mismatched = rq._compute_challenge_synthesis_from_binding_verdicts(
        {"findings": []},
        {
            "challenged_candidate_sha": "beef" * 10,
            "verdicts": [
                {
                    "finding_title": "Merge-test candidate mismatch",
                    "verdict": "confirmed",
                }
            ],
        },
        candidate_sha=candidate_sha,
    )
    assert mismatched["status"] == "unavailable"
    assert mismatched["reason"] == "audit_challenge_sha_mismatch"

    uncertain_only = rq._compute_challenge_synthesis_from_binding_verdicts(
        {"findings": []},
        {
            "challenged_candidate_sha": candidate_sha,
            "verdicts": [
                {
                    "finding_title": "Merge-test unresolved second opinion",
                    "verdict": "uncertain",
                }
            ],
        },
        candidate_sha=candidate_sha,
    )
    assert uncertain_only["status"] == "available"
    assert uncertain_only["uncertain_count"] == 1
    assert uncertain_only["reported_agreement"] != "agree"


def test_challenge_telemetry_counts_binding_challenge_as_challenged():
    """Binding challenge synthesis (status=available, binding=True) lands in challenged cohort.

    Acceptance proof: after _finalize_qualification where the binding
    RELEASE_AUDIT_CHALLENGE ran, the resulting QualificationVerdict is counted in
    the challenged cohort by compute_challenged_vs_unchallenged_telemetry
    (challenged.sample_size > 0).
    """
    # Build verdict dicts with the binding synthesis shape produced by
    # _compute_challenge_synthesis_from_binding_verdicts.
    binding_synthesis = {
        "status": "available",
        "binding": True,
        "reason": None,
        "confirmed_count": 2,
        "refuted_count": 1,
        "uncertain_count": 0,
        "reported_agreement": "partial",
        "challenged_candidate_sha": "feed" * 10,
    }
    verdicts = [
        {
            "candidate_sha": "feed" * 10,
            "audit_reconciliation": {
                "model_reported_score": 1.0,
                "findings_derived_score": 1.0,
                "score_delta": 0.0,
                "excluded_findings_count": 0,
                "audit_challenge": binding_synthesis,
            },
            "would_promote": False,
            "terminal_status": "not_promoted",
            "post_promotion_outcome": None,
            "excluded_audit_findings": None,
        }
        for _ in range(3)
    ]
    result = rq.compute_challenged_vs_unchallenged_telemetry(verdicts)
    assert result["total_verdicts"] == 3
    # All 3 verdicts carry a binding challenge with status=available → challenged cohort.
    assert result["challenged"]["sample_size"] == 3
    assert result["challenged"]["insufficient_sample"] is False
    assert result["unchallenged"]["sample_size"] == 0
    # Disagreement rate: reported_agreement="partial" ≠ "agree" → all 3 disagree.
    assert result["challenged"]["disagreement_count"] == 3
    assert result["challenged"]["disagreement_rate"] == pytest.approx(1.0)


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
        """Net-negative test/assertion deltas are carried (as before)."""
        report = {
            "test_erosion": {
                "deleted_test_files": [],
                "test_count_delta": -2,
                "assertion_delta": -5,
            }
        }
        detail = rq._extract_test_erosion_detail(report)
        assert detail == {"test_count_delta": -2, "assertion_delta": -5}

    def test_positive_deltas_preserved_for_cumulative(self):
        """Positive deltas are now carried (lossless for cumulative reconstruction)."""
        report = {
            "test_erosion": {
                "deleted_test_files": [],
                "test_count_delta": 3,
                "assertion_delta": 10,
            }
        }
        detail = rq._extract_test_erosion_detail(report)
        assert detail == {"test_count_delta": 3, "assertion_delta": 10}

    def test_zero_deltas_omitted(self):
        """Zero deltas (no noise) are omitted; no other signal → None."""
        report = {
            "test_erosion": {
                "deleted_test_files": [],
                "test_count_delta": 0,
                "assertion_delta": 0,
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
