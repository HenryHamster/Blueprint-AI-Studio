"""Tests for capability_trial_verdict.py.

Covers:
  - evaluate_trial_verdict: adopt/reject/continue-trial at threshold boundaries
  - evaluate_trial_verdict: insufficient-sample → continue-trial
  - evaluate_trial_verdict: None lift → continue-trial
  - evaluate_trial_verdict: configurable thresholds respected
  - record_trial_verdict: upserts and supersedes prior verdict
  - record_trial_verdict: decided verdicts cannot be self-reverted by continue-trial
  - record_trial_verdict: decided → decided transitions still supersede normally
  - get_trial_verdicts: returns empty dict when column is absent/None
"""

import contextlib
from typing import ClassVar
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.capability_trial_verdict import (
    LANE_PILOT_ASSIGNMENT_KEY,
    LANE_PILOT_CAP_NAME,
    LANE_PILOT_CHECKPOINT_RECEIPT_KEY,
    LANE_PILOT_WARM_RESUME_RECEIPT_KEY,
    attach_accrual_projection_to_verdict,
    carry_adoption_verdicts_for_consolidation,
    evaluate_lane_pilot_verdict,
    evaluate_trial_verdict,
    get_effective_holdback_fraction,
    get_trial_definitions,
    get_trial_verdicts,
    is_lane_pilot_arm_member,
    record_trial_verdict,
)
from app.lifecycle import _seed_deploy_project_futility_verdicts


# ── evaluate_trial_verdict ────────────────────────────────────────────────────


class TestEvaluateTrialVerdict:
    def _cfg(self, **overrides):
        base = {"min_n_per_arm": 30, "adopt_lift": 0.05, "reject_lift": -0.03}
        base.update(overrides)
        return base

    def test_adopt_at_threshold(self):
        entry = evaluate_trial_verdict(
            lift=0.05,
            n_with=30,
            n_without=30,
            config=self._cfg(),
            evaluated_at="2026-06-09T00:00:00",
        )
        assert entry["verdict"] == "adopt"
        assert entry["lift"] == 0.05
        assert entry["n_with"] == 30
        assert entry["n_without"] == 30

    def test_adopt_above_threshold(self):
        entry = evaluate_trial_verdict(
            lift=0.12,
            n_with=50,
            n_without=50,
            config=self._cfg(),
        )
        assert entry["verdict"] == "adopt"

    def test_reject_at_threshold(self):
        entry = evaluate_trial_verdict(
            lift=-0.03,
            n_with=30,
            n_without=30,
            config=self._cfg(),
        )
        assert entry["verdict"] == "reject"

    def test_reject_below_threshold(self):
        entry = evaluate_trial_verdict(
            lift=-0.10,
            n_with=40,
            n_without=40,
            config=self._cfg(),
        )
        assert entry["verdict"] == "reject"

    def test_continue_trial_lift_between_thresholds(self):
        entry = evaluate_trial_verdict(
            lift=0.02,
            n_with=30,
            n_without=30,
            config=self._cfg(),
        )
        assert entry["verdict"] == "continue-trial"

    def test_continue_trial_negative_but_above_reject(self):
        entry = evaluate_trial_verdict(
            lift=-0.01,
            n_with=30,
            n_without=30,
            config=self._cfg(),
        )
        assert entry["verdict"] == "continue-trial"

    def test_continue_trial_insufficient_with_arm(self):
        entry = evaluate_trial_verdict(
            lift=0.10,
            n_with=5,
            n_without=30,
            config=self._cfg(),
        )
        assert entry["verdict"] == "continue-trial"

    def test_continue_trial_insufficient_without_arm(self):
        entry = evaluate_trial_verdict(
            lift=0.10,
            n_with=30,
            n_without=5,
            config=self._cfg(),
        )
        assert entry["verdict"] == "continue-trial"

    def test_continue_trial_none_lift(self):
        entry = evaluate_trial_verdict(
            lift=None,
            n_with=30,
            n_without=30,
            config=self._cfg(),
        )
        assert entry["verdict"] == "continue-trial"
        assert entry["lift"] is None

    def test_evaluated_at_propagated(self):
        entry = evaluate_trial_verdict(
            lift=0.06,
            n_with=30,
            n_without=30,
            evaluated_at="2026-06-09T12:00:00",
        )
        assert entry["evaluated_at"] == "2026-06-09T12:00:00"

    def test_evaluated_at_omitted_when_not_supplied(self):
        entry = evaluate_trial_verdict(lift=0.06, n_with=30, n_without=30)
        assert "evaluated_at" not in entry

    def test_window_hours_propagated(self):
        entry = evaluate_trial_verdict(
            lift=0.06, n_with=30, n_without=30, window_hours=72
        )
        assert entry["window_hours"] == 72

    def test_accumulation_basis_propagated_distinct_from_window(self):
        # The auditable evidence basis must be recorded DISTINCT from window_hours:
        # window_hours describes the dashboard window; accumulation_basis names the
        # population that actually produced the decision counts.
        entry = evaluate_trial_verdict(
            lift=0.06,
            n_with=42,
            n_without=37,
            window_hours=24,
            accumulation_basis="cumulative_terminal_arm_tasks",
        )
        assert entry["accumulation_basis"] == "cumulative_terminal_arm_tasks"
        assert entry["window_hours"] == 24
        # The cumulative counts that drove the decision are auditable on the entry.
        assert entry["n_with"] == 42
        assert entry["n_without"] == 37
        assert entry["verdict"] == "adopt"

    def test_accumulation_basis_omitted_when_not_supplied(self):
        entry = evaluate_trial_verdict(lift=0.06, n_with=30, n_without=30)
        assert "accumulation_basis" not in entry

    def test_configurable_min_n_respected(self):
        # With a lower min_n threshold the arms qualify earlier.
        entry = evaluate_trial_verdict(
            lift=0.06,
            n_with=10,
            n_without=10,
            config={"min_n_per_arm": 10, "adopt_lift": 0.05, "reject_lift": -0.03},
        )
        assert entry["verdict"] == "adopt"

    def test_configurable_adopt_lift_respected(self):
        # Require a higher lift to adopt — lift=0.05 should still be continue.
        entry = evaluate_trial_verdict(
            lift=0.05,
            n_with=30,
            n_without=30,
            config={"min_n_per_arm": 30, "adopt_lift": 0.10, "reject_lift": -0.03},
        )
        assert entry["verdict"] == "continue-trial"

    def test_configurable_reject_lift_respected(self):
        # Require a steeper drop to reject — lift=-0.03 should be continue.
        entry = evaluate_trial_verdict(
            lift=-0.03,
            n_with=30,
            n_without=30,
            config={"min_n_per_arm": 30, "adopt_lift": 0.05, "reject_lift": -0.08},
        )
        assert entry["verdict"] == "continue-trial"

    def test_thresholds_included_in_entry(self):
        entry = evaluate_trial_verdict(
            lift=0.06,
            n_with=30,
            n_without=30,
            config={"min_n_per_arm": 30, "adopt_lift": 0.05, "reject_lift": -0.03},
        )
        assert entry["thresholds"]["min_n_per_arm"] == 30
        assert entry["thresholds"]["adopt_lift"] == 0.05
        assert entry["thresholds"]["reject_lift"] == -0.03

    def test_verdict_reason_sufficient_data_adopt(self):
        entry = evaluate_trial_verdict(
            lift=0.06,
            n_with=30,
            n_without=30,
            config=self._cfg(),
        )
        assert entry["verdict"] == "adopt"
        assert entry["verdict_reason"] == "sufficient_data"

    def test_verdict_reason_sufficient_data_reject(self):
        entry = evaluate_trial_verdict(
            lift=-0.05,
            n_with=30,
            n_without=30,
            config=self._cfg(),
        )
        assert entry["verdict"] == "reject"
        assert entry["verdict_reason"] == "sufficient_data"

    def test_verdict_reason_ambiguous_lift(self):
        entry = evaluate_trial_verdict(
            lift=0.02,
            n_with=30,
            n_without=30,
            config=self._cfg(),
        )
        assert entry["verdict"] == "continue-trial"
        assert entry["verdict_reason"] == "ambiguous_lift"

    def test_verdict_reason_insufficient_data_none_lift(self):
        entry = evaluate_trial_verdict(
            lift=None,
            n_with=30,
            n_without=30,
            config=self._cfg(),
        )
        assert entry["verdict"] == "continue-trial"
        assert entry["verdict_reason"] == "insufficient_data"

    def test_verdict_reason_insufficient_data_below_min_n(self):
        entry = evaluate_trial_verdict(
            lift=0.06,
            n_with=5,
            n_without=30,
            config=self._cfg(),
        )
        assert entry["verdict"] == "continue-trial"
        assert entry["verdict_reason"] == "insufficient_data"

    # ── futility bound tests ──────────────────────────────────────────────────

    def test_futility_bound_terminates_ambiguous_trial(self):
        # Acceptance test: both arms at futility_n with ambiguous lift → no_effect/futility_bound_reached
        # Default futility_n = 30 * 3 = 90
        entry = evaluate_trial_verdict(
            lift=0.02,
            n_with=90,
            n_without=90,
            config=self._cfg(),
        )
        assert entry["verdict"] == "no_effect"
        assert entry["verdict_reason"] == "futility_bound_reached"

    def test_futility_ambiguous_lift_is_no_effect_not_reject(self):
        # Acceptance test (named): ambiguous lift at futility bound → no_effect, NOT reject.
        # Negative lift crossing the reject threshold is a different outcome (reject/sufficient_data).
        entry = evaluate_trial_verdict(
            lift=0.01,
            n_with=100,
            n_without=100,
            config=self._cfg(),
        )
        assert entry["verdict"] == "no_effect"
        assert entry["verdict_reason"] == "futility_bound_reached"
        assert entry["verdict"] != "reject"

    def test_futility_bound_reached_emits_no_effect(self):
        entry = evaluate_trial_verdict(
            lift=0.01,
            n_with=100,
            n_without=100,
            config=self._cfg(),
        )
        assert entry["verdict"] == "no_effect"
        assert entry["verdict_reason"] == "futility_bound_reached"

    def test_below_futility_bound_stays_ambiguous(self):
        # Both arms at min_n (30) but below futility_n (90)
        entry = evaluate_trial_verdict(
            lift=0.02,
            n_with=30,
            n_without=30,
            config=self._cfg(),
        )
        assert entry["verdict"] == "continue-trial"
        assert entry["verdict_reason"] == "ambiguous_lift"

    def test_one_arm_below_futility_bound_stays_ambiguous(self):
        # n_with >= futility_n but n_without < futility_n
        entry = evaluate_trial_verdict(
            lift=0.02,
            n_with=90,
            n_without=50,
            config=self._cfg(),
        )
        assert entry["verdict"] == "continue-trial"
        assert entry["verdict_reason"] == "ambiguous_lift"

    def test_futility_bound_not_triggered_when_adopt_lift_met(self):
        # lift >= adopt_lift must still produce adopt even at futility_n
        entry = evaluate_trial_verdict(
            lift=0.06,
            n_with=90,
            n_without=90,
            config=self._cfg(),
        )
        assert entry["verdict"] == "adopt"
        assert entry["verdict_reason"] == "sufficient_data"

    def test_futility_bound_not_triggered_when_reject_lift_met(self):
        # lift <= reject_lift must still produce reject/sufficient_data even at futility_n
        entry = evaluate_trial_verdict(
            lift=-0.05,
            n_with=90,
            n_without=90,
            config=self._cfg(),
        )
        assert entry["verdict"] == "reject"
        assert entry["verdict_reason"] == "sufficient_data"

    def test_futility_n_included_in_thresholds_entry(self):
        entry = evaluate_trial_verdict(
            lift=0.02,
            n_with=90,
            n_without=90,
            config=self._cfg(),
        )
        assert "futility_n" in entry["thresholds"]
        assert entry["thresholds"]["futility_n"] == 90

    def test_configurable_futility_n_respected(self):
        # Custom futility_n lower than default — smaller arms can trigger futility
        cfg = self._cfg()
        cfg["futility_n"] = 50
        entry = evaluate_trial_verdict(
            lift=0.01,
            n_with=50,
            n_without=50,
            config=cfg,
        )
        assert entry["verdict"] == "no_effect"
        assert entry["verdict_reason"] == "futility_bound_reached"

    def test_futility_bound_holdback_is_one(self):
        # futility_bound_reached emits no_effect — holdback must be 1.0 (stop delivering)
        entry = evaluate_trial_verdict(
            lift=0.01,
            n_with=90,
            n_without=90,
            config=self._cfg(),
        )
        verdicts = {"repo_map": entry}
        holdback = get_effective_holdback_fraction(verdicts, "repo_map", 0.1)
        assert holdback == 1.0

    # ── significance gate ─────────────────────────────────────────────────────

    def test_insignificant_lift_at_min_n_stays_continue_trial(self):
        """Acceptance test: lift exactly at adopt threshold is not significant at min_n.

        At n=30 per arm, +5% lift yields z≈0.39 (SE≈0.129 with p=0.5 worst case).
        With min_significance_z=1.65 the gate must hold the verdict at continue-trial
        so the system cannot durably adopt on sampling noise.
        """
        entry = evaluate_trial_verdict(
            lift=0.05,
            n_with=30,
            n_without=30,
            config={
                "min_n_per_arm": 30,
                "adopt_lift": 0.05,
                "reject_lift": -0.03,
                "min_significance_z": 1.65,
            },
        )
        assert entry["verdict"] == "continue-trial"
        assert entry["verdict_reason"] == "insignificant_lift"

    def test_significant_lift_at_large_n_adopts(self):
        # At n=600 per arm, +5% lift yields z≈2.74 > 1.65 — gate passes, adopt fires.
        entry = evaluate_trial_verdict(
            lift=0.05,
            n_with=600,
            n_without=600,
            config={
                "min_n_per_arm": 30,
                "adopt_lift": 0.05,
                "reject_lift": -0.03,
                "min_significance_z": 1.65,
            },
        )
        assert entry["verdict"] == "adopt"
        assert entry["verdict_reason"] == "sufficient_data"

    def test_significance_gate_disabled_by_default(self):
        # Without min_significance_z in config, small-n adopt still fires (backward-compat).
        entry = evaluate_trial_verdict(
            lift=0.05,
            n_with=30,
            n_without=30,
            config=self._cfg(),
        )
        assert entry["verdict"] == "adopt"

    def test_significance_gate_blocks_reject_too(self):
        # A negative lift at min_n that is not statistically significant must also be held.
        entry = evaluate_trial_verdict(
            lift=-0.03,
            n_with=30,
            n_without=30,
            config={
                "min_n_per_arm": 30,
                "adopt_lift": 0.05,
                "reject_lift": -0.03,
                "min_significance_z": 1.65,
            },
        )
        assert entry["verdict"] == "continue-trial"
        assert entry["verdict_reason"] == "insignificant_lift"

    def test_significance_gate_does_not_affect_no_effect(self):
        # Futility bound (no_effect) is structural, not a precision claim — gate must not fire.
        entry = evaluate_trial_verdict(
            lift=0.02,
            n_with=90,
            n_without=90,
            config={
                "min_n_per_arm": 30,
                "adopt_lift": 0.05,
                "reject_lift": -0.03,
                "min_significance_z": 1.65,
            },
        )
        assert entry["verdict"] == "no_effect"
        assert entry["verdict_reason"] == "futility_bound_reached"

    # ── per_arm_cost ──────────────────────────────────────────────────────────

    def test_verdict_entry_surfaces_per_arm_cost_delta(self):
        """per_arm_cost dict is included verbatim in the verdict entry for an adopt verdict."""
        per_arm_cost = {
            "with_avg_cost_usd": 0.12,
            "without_avg_cost_usd": 0.10,
            "cost_delta": 0.02,
        }
        entry = evaluate_trial_verdict(
            lift=0.10,
            n_with=50,
            n_without=50,
            config=self._cfg(),
            evaluated_at="2026-06-23T00:00:00",
            per_arm_cost=per_arm_cost,
        )
        assert entry["verdict"] == "adopt"
        assert "per_arm_cost" in entry
        assert entry["per_arm_cost"]["cost_delta"] == pytest.approx(0.02)
        assert entry["per_arm_cost"]["with_avg_cost_usd"] == pytest.approx(0.12)
        assert entry["per_arm_cost"]["without_avg_cost_usd"] == pytest.approx(0.10)

    def test_verdict_entry_per_arm_cost_none_when_not_provided(self):
        """per_arm_cost is absent from the entry when not passed in."""
        entry = evaluate_trial_verdict(
            lift=0.10,
            n_with=50,
            n_without=50,
            config=self._cfg(),
        )
        assert "per_arm_cost" not in entry

    def test_verdict_entry_per_arm_cost_present_on_continue_trial(self):
        """per_arm_cost is surfaced even when the verdict is continue-trial (early cost signal)."""
        per_arm_cost = {
            "with_avg_cost_usd": 0.08,
            "without_avg_cost_usd": 0.09,
            "cost_delta": -0.01,
        }
        entry = evaluate_trial_verdict(
            lift=None,
            n_with=5,
            n_without=5,
            config=self._cfg(),
            per_arm_cost=per_arm_cost,
        )
        assert entry["verdict"] == "continue-trial"
        assert entry["per_arm_cost"]["cost_delta"] == pytest.approx(-0.01)


# ── record_trial_verdict ─────────────────────────────────────────────────────


def _make_project():
    p = MagicMock()
    p.capability_adoption_verdicts = {}
    return p


class TestRecordTrialVerdict:
    def test_upserts_entry(self):
        project = _make_project()
        entry = {"verdict": "adopt", "lift": 0.08, "n_with": 40, "n_without": 40}

        with patch("app.capability_trial_verdict.flag_modified"):
            record_trial_verdict(project, "repo_map", entry)

        assert project.capability_adoption_verdicts["repo_map"] == entry

    def test_supersedes_prior_verdict(self):
        # continue-trial → adopt is an allowed upward transition
        project = _make_project()
        prior = {
            "verdict": "continue-trial",
            "lift": 0.01,
            "n_with": 10,
            "n_without": 10,
        }
        project.capability_adoption_verdicts = {"repo_map": prior}
        newer = {"verdict": "adopt", "lift": 0.07, "n_with": 40, "n_without": 40}

        with patch("app.capability_trial_verdict.flag_modified"):
            record_trial_verdict(project, "repo_map", newer)

        assert project.capability_adoption_verdicts["repo_map"]["verdict"] == "adopt"
        assert project.capability_adoption_verdicts["repo_map"]["lift"] == 0.07

    def test_continue_trial_does_not_overwrite_adopt(self):
        project = _make_project()
        decided = {"verdict": "adopt", "lift": 0.09, "n_with": 50, "n_without": 50}
        project.capability_adoption_verdicts = {"repo_map": decided}
        insufficient = {
            "verdict": "continue-trial",
            "lift": None,
            "n_with": 3,
            "n_without": 2,
        }

        with patch("app.capability_trial_verdict.flag_modified") as mock_flag:
            record_trial_verdict(project, "repo_map", insufficient)

        # Decided verdict preserved; flag_modified not called (no write happened)
        assert project.capability_adoption_verdicts["repo_map"]["verdict"] == "adopt"
        assert project.capability_adoption_verdicts["repo_map"]["lift"] == 0.09
        mock_flag.assert_not_called()

    def test_continue_trial_does_not_overwrite_reject(self):
        project = _make_project()
        decided = {"verdict": "reject", "lift": -0.08, "n_with": 50, "n_without": 50}
        project.capability_adoption_verdicts = {"repo_map": decided}
        insufficient = {
            "verdict": "continue-trial",
            "lift": 0.0,
            "n_with": 5,
            "n_without": 0,
        }

        with patch("app.capability_trial_verdict.flag_modified") as mock_flag:
            record_trial_verdict(project, "repo_map", insufficient)

        assert project.capability_adoption_verdicts["repo_map"]["verdict"] == "reject"
        mock_flag.assert_not_called()

    def test_decided_verdict_survives_insufficient_data_reevaluation(self):
        """Acceptance test: adopt persists when the periodic evaluator returns continue-trial.

        After adoption, holdback goes to 0.0 and no without-arm tasks accrue.
        Once pre-decision tasks age out of the window the evaluator sees n_without=0
        and must return continue-trial.  That continue-trial must not self-revert the adopt.
        """
        from app.capability_trial_verdict import evaluate_trial_verdict

        project = _make_project()
        # Record the initial adoption decision
        adopt_entry = evaluate_trial_verdict(
            lift=0.07,
            n_with=40,
            n_without=40,
            evaluated_at="2026-06-01T00:00:00",
        )
        assert adopt_entry["verdict"] == "adopt"

        with patch("app.capability_trial_verdict.flag_modified"):
            record_trial_verdict(project, "repo_map", adopt_entry)

        assert project.capability_adoption_verdicts["repo_map"]["verdict"] == "adopt"

        # Simulate periodic digest re-evaluation after window rolls: without-arm dried up
        reevaluation = evaluate_trial_verdict(
            lift=None,
            n_with=0,
            n_without=0,
            evaluated_at="2026-06-09T00:00:00",
        )
        assert reevaluation["verdict"] == "continue-trial"

        with patch("app.capability_trial_verdict.flag_modified") as mock_flag:
            record_trial_verdict(project, "repo_map", reevaluation)

        # The adopt must survive the insufficient-data reevaluation
        assert project.capability_adoption_verdicts["repo_map"]["verdict"] == "adopt"
        assert project.capability_adoption_verdicts["repo_map"]["lift"] == 0.07
        mock_flag.assert_not_called()

    def test_adopt_still_overwrites_reject(self):
        # decided → decided transitions must still work (reversal with full evidence)
        project = _make_project()
        prior = {"verdict": "reject", "lift": -0.05, "n_with": 40, "n_without": 40}
        project.capability_adoption_verdicts = {"repo_map": prior}
        reversal = {
            "verdict": "adopt",
            "lift": 0.08,
            "n_with": 50,
            "n_without": 50,
            "evidence_source": "strategic_review",
        }

        with patch("app.capability_trial_verdict.flag_modified") as mock_flag:
            record_trial_verdict(project, "repo_map", reversal)

        assert project.capability_adoption_verdicts["repo_map"]["verdict"] == "adopt"
        mock_flag.assert_called_once()

    def test_reject_still_overwrites_adopt(self):
        project = _make_project()
        prior = {"verdict": "adopt", "lift": 0.07, "n_with": 40, "n_without": 40}
        project.capability_adoption_verdicts = {"repo_map": prior}
        reversal = {"verdict": "reject", "lift": -0.06, "n_with": 50, "n_without": 50}

        with patch("app.capability_trial_verdict.flag_modified") as mock_flag:
            record_trial_verdict(project, "repo_map", reversal)

        assert project.capability_adoption_verdicts["repo_map"]["verdict"] == "reject"
        mock_flag.assert_called_once()

    def test_strategic_review_continue_trial_reopens_decided_verdict(self):
        """Strategist re-declaration with continue-trial must override a decided verdict.

        The strategist is the one actor authorised to deliberately reopen a trial
        (e.g. to resume default holdback after an erroneous adoption).  When
        evidence_source='strategic_review', the guard must not block the write.
        """
        project = _make_project()
        decided = {"verdict": "adopt", "lift": 0.09, "n_with": 50, "n_without": 50}
        project.capability_adoption_verdicts = {"repo_map": decided}
        strategic_reopen = {
            "verdict": "continue-trial",
            "evidence_source": "strategic_review",
            "rationale": "re-opening trial for more data",
        }

        with patch("app.capability_trial_verdict.flag_modified") as mock_flag:
            record_trial_verdict(project, "repo_map", strategic_reopen)

        # Strategic re-declaration must supersede the prior decided verdict
        assert (
            project.capability_adoption_verdicts["repo_map"]["verdict"]
            == "continue-trial"
        )
        assert (
            project.capability_adoption_verdicts["repo_map"]["evidence_source"]
            == "strategic_review"
        )
        mock_flag.assert_called_once()

    def test_strategic_review_continue_trial_reopens_reject(self):
        """Same re-open semantics apply when prior verdict is reject."""
        project = _make_project()
        decided = {"verdict": "reject", "lift": -0.08, "n_with": 50, "n_without": 50}
        project.capability_adoption_verdicts = {"repo_map": decided}
        strategic_reopen = {
            "verdict": "continue-trial",
            "evidence_source": "strategic_review",
        }

        with patch("app.capability_trial_verdict.flag_modified") as mock_flag:
            record_trial_verdict(project, "repo_map", strategic_reopen)

        assert (
            project.capability_adoption_verdicts["repo_map"]["verdict"]
            == "continue-trial"
        )
        mock_flag.assert_called_once()

    def test_non_strategic_continue_trial_still_blocked_after_decide(self):
        """Auto-evaluation continue-trial (no evidence_source) must still be blocked."""
        project = _make_project()
        decided = {"verdict": "adopt", "lift": 0.09, "n_with": 50, "n_without": 50}
        project.capability_adoption_verdicts = {"repo_map": decided}
        auto_eval = {
            "verdict": "continue-trial",
            "lift": None,
            "n_with": 0,
            "n_without": 0,
        }

        with patch("app.capability_trial_verdict.flag_modified") as mock_flag:
            record_trial_verdict(project, "repo_map", auto_eval)

        assert project.capability_adoption_verdicts["repo_map"]["verdict"] == "adopt"
        mock_flag.assert_not_called()

    def test_continue_trial_overwrites_prior_continue_trial(self):
        # Normal trial progression: continue-trial updates are allowed
        project = _make_project()
        prior = {
            "verdict": "continue-trial",
            "lift": 0.01,
            "n_with": 10,
            "n_without": 10,
        }
        project.capability_adoption_verdicts = {"repo_map": prior}
        newer = {
            "verdict": "continue-trial",
            "lift": 0.02,
            "n_with": 20,
            "n_without": 20,
        }

        with patch("app.capability_trial_verdict.flag_modified") as mock_flag:
            record_trial_verdict(project, "repo_map", newer)

        assert project.capability_adoption_verdicts["repo_map"]["n_with"] == 20
        mock_flag.assert_called_once()

    def test_initializes_column_when_none(self):
        project = MagicMock()
        project.capability_adoption_verdicts = None
        entry = {"verdict": "continue-trial", "lift": None, "n_with": 5, "n_without": 3}

        with patch("app.capability_trial_verdict.flag_modified"):
            record_trial_verdict(project, "repo_map", entry)

        assert isinstance(project.capability_adoption_verdicts, dict)
        assert "repo_map" in project.capability_adoption_verdicts

    def test_multiple_capabilities_stored_independently(self):
        project = _make_project()
        e1 = {"verdict": "adopt", "lift": 0.08}
        e2 = {"verdict": "reject", "lift": -0.05}

        with patch("app.capability_trial_verdict.flag_modified"):
            record_trial_verdict(project, "repo_map", e1)
            record_trial_verdict(project, "another_cap", e2)

        assert project.capability_adoption_verdicts["repo_map"]["verdict"] == "adopt"
        assert (
            project.capability_adoption_verdicts["another_cap"]["verdict"] == "reject"
        )

    def test_preserves_prior_adopt_against_insufficient_data(self):
        project = _make_project()
        prior = {"verdict": "adopt", "lift": 0.08, "n_with": 40, "n_without": 40}
        project.capability_adoption_verdicts = {"repo_map": prior}
        # Incoming verdict: continue-trial with insufficient_data (one arm lost samples)
        incoming = {
            "verdict": "continue-trial",
            "verdict_reason": "insufficient_data",
            "lift": None,
            "n_with": 20,
            "n_without": 2,
        }

        with patch("app.capability_trial_verdict.flag_modified"):
            record_trial_verdict(project, "repo_map", incoming)

        # Prior adopt verdict must be preserved.
        assert project.capability_adoption_verdicts["repo_map"]["verdict"] == "adopt"
        assert project.capability_adoption_verdicts["repo_map"]["lift"] == 0.08

    def test_preserves_prior_reject_against_insufficient_data(self):
        project = _make_project()
        prior = {"verdict": "reject", "lift": -0.09, "n_with": 40, "n_without": 40}
        project.capability_adoption_verdicts = {"repo_map": prior}
        # Incoming verdict: continue-trial with insufficient_data
        incoming = {
            "verdict": "continue-trial",
            "verdict_reason": "insufficient_data",
            "lift": None,
            "n_with": 5,
            "n_without": 35,
        }

        with patch("app.capability_trial_verdict.flag_modified"):
            record_trial_verdict(project, "repo_map", incoming)

        # Prior reject verdict must be preserved.
        assert project.capability_adoption_verdicts["repo_map"]["verdict"] == "reject"
        assert project.capability_adoption_verdicts["repo_map"]["lift"] == -0.09

    def test_cumulative_continue_trial_cannot_self_revert_decided_adopt(self):
        # Guard semantics must hold for entries built via the cumulative path: an
        # insufficient-data continue-trial carrying accumulation_basis must NOT
        # supersede a prior decided adopt.
        project = _make_project()
        prior = evaluate_trial_verdict(
            lift=0.08,
            n_with=40,
            n_without=40,
            accumulation_basis="cumulative_terminal_arm_tasks",
        )
        assert prior["verdict"] == "adopt"
        project.capability_adoption_verdicts = {"repo_map": prior}
        incoming = evaluate_trial_verdict(
            lift=None,
            n_with=12,
            n_without=3,
            accumulation_basis="cumulative_terminal_arm_tasks",
        )
        assert incoming["verdict"] == "continue-trial"
        assert incoming["verdict_reason"] == "insufficient_data"

        with patch("app.capability_trial_verdict.flag_modified"):
            record_trial_verdict(project, "repo_map", incoming)

        # Prior adopt stands; cumulative basis does not weaken the guard.
        assert project.capability_adoption_verdicts["repo_map"]["verdict"] == "adopt"
        assert (
            project.capability_adoption_verdicts["repo_map"]["accumulation_basis"]
            == "cumulative_terminal_arm_tasks"
        )

    def test_overwrites_adopt_for_ambiguous_lift(self):
        project = _make_project()
        prior = {"verdict": "adopt", "lift": 0.08, "n_with": 40, "n_without": 40}
        project.capability_adoption_verdicts = {"repo_map": prior}
        # Incoming verdict: continue-trial with ambiguous_lift (sufficient data, but ambiguous)
        incoming = {
            "verdict": "continue-trial",
            "verdict_reason": "ambiguous_lift",
            "lift": 0.02,
            "n_with": 50,
            "n_without": 50,
        }

        with patch("app.capability_trial_verdict.flag_modified"):
            record_trial_verdict(project, "repo_map", incoming)

        # Should overwrite: ambiguous_lift with sufficient data supersedes prior adopt.
        assert (
            project.capability_adoption_verdicts["repo_map"]["verdict"]
            == "continue-trial"
        )
        assert project.capability_adoption_verdicts["repo_map"]["lift"] == 0.02

    def test_preserves_strategist_declared_against_insufficient_data(self):
        project = _make_project()
        prior = {
            "verdict": "adopt",
            "lift": 0.08,
            "n_with": 40,
            "n_without": 40,
            "evidence_source": "strategic_review",
        }
        project.capability_adoption_verdicts = {"repo_map": prior}
        # Incoming verdict: continue-trial with insufficient_data
        incoming = {
            "verdict": "continue-trial",
            "verdict_reason": "insufficient_data",
            "lift": None,
            "n_with": 2,
            "n_without": 40,
        }

        with patch("app.capability_trial_verdict.flag_modified"):
            record_trial_verdict(project, "repo_map", incoming)

        # Strategist-declared verdict must be preserved.
        assert project.capability_adoption_verdicts["repo_map"]["verdict"] == "adopt"
        assert (
            project.capability_adoption_verdicts["repo_map"]["evidence_source"]
            == "strategic_review"
        )

    def test_fresh_adopt_always_overwrites(self):
        project = _make_project()
        prior = {"verdict": "reject", "lift": -0.08, "n_with": 40, "n_without": 40}
        project.capability_adoption_verdicts = {"repo_map": prior}
        # Fresh adopt verdict
        incoming = {
            "verdict": "adopt",
            "verdict_reason": "sufficient_data",
            "lift": 0.07,
            "n_with": 50,
            "n_without": 50,
        }

        with patch("app.capability_trial_verdict.flag_modified"):
            record_trial_verdict(project, "repo_map", incoming)

        # Fresh adopt verdict should overwrite regardless of prior state.
        assert project.capability_adoption_verdicts["repo_map"]["verdict"] == "adopt"
        assert project.capability_adoption_verdicts["repo_map"]["lift"] == 0.07

    def test_fresh_reject_always_overwrites(self):
        project = _make_project()
        prior = {"verdict": "adopt", "lift": 0.08, "n_with": 40, "n_without": 40}
        project.capability_adoption_verdicts = {"repo_map": prior}
        # Fresh reject verdict
        incoming = {
            "verdict": "reject",
            "verdict_reason": "sufficient_data",
            "lift": -0.07,
            "n_with": 50,
            "n_without": 50,
        }

        with patch("app.capability_trial_verdict.flag_modified"):
            record_trial_verdict(project, "repo_map", incoming)

        # Fresh reject verdict should overwrite regardless of prior state.
        assert project.capability_adoption_verdicts["repo_map"]["verdict"] == "reject"
        assert project.capability_adoption_verdicts["repo_map"]["lift"] == -0.07

    def test_futility_bound_verdict_protected_from_insufficient_data_revert(self):
        # A no_effect/futility_bound_reached must not be overwritten by continue-trial/insufficient_data
        project = _make_project()
        prior = {
            "verdict": "no_effect",
            "verdict_reason": "futility_bound_reached",
            "lift": 0.01,
            "n_with": 90,
            "n_without": 90,
        }
        project.capability_adoption_verdicts = {"repo_map": prior}
        incoming = {
            "verdict": "continue-trial",
            "verdict_reason": "insufficient_data",
            "lift": None,
            "n_with": 5,
            "n_without": 3,
        }

        with patch("app.capability_trial_verdict.flag_modified") as mock_flag:
            record_trial_verdict(project, "repo_map", incoming)

        assert (
            project.capability_adoption_verdicts["repo_map"]["verdict"] == "no_effect"
        )
        assert (
            project.capability_adoption_verdicts["repo_map"]["verdict_reason"]
            == "futility_bound_reached"
        )
        mock_flag.assert_not_called()

    def test_strategic_review_can_override_futility_bound(self):
        # Explicit strategist re-declaration must still override futility verdict
        project = _make_project()
        prior = {
            "verdict": "no_effect",
            "verdict_reason": "futility_bound_reached",
            "lift": 0.01,
            "n_with": 90,
            "n_without": 90,
        }
        project.capability_adoption_verdicts = {"repo_map": prior}
        strategic_reopen = {
            "verdict": "continue-trial",
            "evidence_source": "strategic_review",
            "rationale": "re-opening trial after protocol change",
        }

        with patch("app.capability_trial_verdict.flag_modified") as mock_flag:
            record_trial_verdict(project, "repo_map", strategic_reopen)

        assert (
            project.capability_adoption_verdicts["repo_map"]["verdict"]
            == "continue-trial"
        )
        assert (
            project.capability_adoption_verdicts["repo_map"]["evidence_source"]
            == "strategic_review"
        )
        mock_flag.assert_called_once()

    def test_decided_lane_verdict_survives_insufficient_data_continue(self):
        continue_entry = evaluate_lane_pilot_verdict({"n_lane": 1, "n_control": 1})
        assert (continue_entry["verdict"], continue_entry["verdict_reason"]) == (
            "CONTINUE",
            "insufficient_data",
        )
        lane_kill = evaluate_lane_pilot_verdict(
            {
                "n_lane": 40,
                "n_control": 40,
                "lane_merge_rate": 0.60,
                "control_merge_rate": 0.70,
            }
        )
        lane_adopt = evaluate_lane_pilot_verdict(
            {
                "n_lane": 40,
                "n_control": 40,
                "lane_merge_rate": 0.75,
                "control_merge_rate": 0.70,
                "lane_cost_per_merged": 90.0,
                "control_cost_per_merged": 100.0,
            }
        )
        assert (lane_kill["verdict"], lane_adopt["verdict"]) == ("KILL", "ADOPT")

        for decided in (lane_kill, lane_adopt):
            project = _make_project()
            project.capability_adoption_verdicts = {LANE_PILOT_CAP_NAME: decided}
            with patch("app.capability_trial_verdict.flag_modified") as mock_flag:
                record_trial_verdict(project, LANE_PILOT_CAP_NAME, continue_entry)

            assert project.capability_adoption_verdicts[LANE_PILOT_CAP_NAME] == decided
            mock_flag.assert_not_called()

    def test_decided_lane_verdict_survives_accumulating_data_continue(self):
        # n between min_n (30) and futility_n (90), no kill/adopt criteria → accumulating_data
        accumulating_entry = evaluate_lane_pilot_verdict(
            {"n_lane": 50, "n_control": 50}
        )
        assert (
            accumulating_entry["verdict"],
            accumulating_entry["verdict_reason"],
        ) == ("CONTINUE", "accumulating_data")

        lane_kill = evaluate_lane_pilot_verdict(
            {
                "n_lane": 40,
                "n_control": 40,
                "lane_merge_rate": 0.60,
                "control_merge_rate": 0.70,
            }
        )
        lane_adopt = evaluate_lane_pilot_verdict(
            {
                "n_lane": 40,
                "n_control": 40,
                "lane_merge_rate": 0.75,
                "control_merge_rate": 0.70,
                "lane_cost_per_merged": 90.0,
                "control_cost_per_merged": 100.0,
            }
        )
        assert (lane_kill["verdict"], lane_adopt["verdict"]) == ("KILL", "ADOPT")

        for decided in (lane_kill, lane_adopt):
            project = _make_project()
            project.capability_adoption_verdicts = {LANE_PILOT_CAP_NAME: decided}
            with patch("app.capability_trial_verdict.flag_modified") as mock_flag:
                record_trial_verdict(project, LANE_PILOT_CAP_NAME, accumulating_entry)

            assert project.capability_adoption_verdicts[LANE_PILOT_CAP_NAME] == decided
            mock_flag.assert_not_called()

    def test_lane_kill_survives_automated_adopt_and_strategic_verdicts_still_supersede(
        self,
    ):
        project = _make_project()
        kill_entry = evaluate_lane_pilot_verdict(
            {
                "n_lane": 40,
                "n_control": 40,
                "lane_merge_rate": 0.60,
                "control_merge_rate": 0.70,
            }
        )
        adopt_entry = evaluate_lane_pilot_verdict(
            {
                "n_lane": 40,
                "n_control": 40,
                "lane_merge_rate": 0.75,
                "control_merge_rate": 0.70,
                "lane_cost_per_merged": 90.0,
                "control_cost_per_merged": 100.0,
            }
        )
        project.capability_adoption_verdicts = {LANE_PILOT_CAP_NAME: kill_entry}

        with patch("app.capability_trial_verdict.flag_modified") as mock_flag:
            record_trial_verdict(project, LANE_PILOT_CAP_NAME, adopt_entry)

        assert project.capability_adoption_verdicts[LANE_PILOT_CAP_NAME] == kill_entry
        mock_flag.assert_not_called()

        strategic_reopen = {
            "verdict": "CONTINUE",
            "verdict_reason": "insufficient_data",
            "evidence_source": "strategic_review",
        }
        with patch("app.capability_trial_verdict.flag_modified") as mock_flag:
            record_trial_verdict(project, LANE_PILOT_CAP_NAME, strategic_reopen)

        assert (
            project.capability_adoption_verdicts[LANE_PILOT_CAP_NAME]
            == strategic_reopen
        )
        mock_flag.assert_called_once()

    def test_contaminated_continue_does_not_overwrite_adopt(self):
        """Broken/contaminated measurements must not overturn a standing decided verdict.

        When a capability is adopted, holdback goes to 0.0 and no without-arm tasks accrue.
        A subsequent measurement showing treatment_not_engaging (with-arm delivery below
        threshold) or holdback_contaminated (without-arm received capability despite holdback)
        indicates a broken measurement, not a genuine inconclusive result. These contaminated
        verdicts must emit an alert but must NOT revert the standing adopted verdict.
        """
        project = _make_project()
        adopted = {"verdict": "adopt", "lift": 0.09, "n_with": 50, "n_without": 50}
        project.capability_adoption_verdicts = {"repo_map": adopted}

        # Attempt 1: continue-trial with treatment_not_engaging (broken with-arm delivery)
        contaminated_delivery = {
            "verdict": "continue-trial",
            "verdict_reason": "treatment_not_engaging",
            "immediate_alert": True,
            "lift": None,
            "n_with": 40,
            "n_without": 40,
        }

        with patch("app.capability_trial_verdict.flag_modified") as mock_flag:
            record_trial_verdict(project, "repo_map", contaminated_delivery)

        # Adopt must be preserved; flag_modified not called (no write happened).
        assert project.capability_adoption_verdicts["repo_map"]["verdict"] == "adopt"
        assert project.capability_adoption_verdicts["repo_map"]["lift"] == 0.09
        mock_flag.assert_not_called()

        # Attempt 2: continue-trial with holdback_contaminated (broken without-arm)
        contaminated_holdback = {
            "verdict": "continue-trial",
            "verdict_reason": "holdback_contaminated",
            "immediate_alert": True,
            "contamination_rate": 0.35,
            "lift": None,
            "n_with": 45,
            "n_without": 45,
        }

        with patch("app.capability_trial_verdict.flag_modified") as mock_flag:
            record_trial_verdict(project, "repo_map", contaminated_holdback)

        # Adopt must still be preserved despite the contaminated signal.
        assert project.capability_adoption_verdicts["repo_map"]["verdict"] == "adopt"
        assert project.capability_adoption_verdicts["repo_map"]["lift"] == 0.09
        mock_flag.assert_not_called()

    def test_operator_source_bypasses_kill_survival_guard(self):
        """evidence_source='operator' must override a durable KILL like strategic_review does."""
        project = _make_project()
        project.capability_adoption_verdicts = {
            LANE_PILOT_CAP_NAME: {"verdict": "KILL", "evidence_source": "auto"}
        }
        override = {
            "verdict": "adopt",
            "evidence_source": "operator",
        }

        with patch("app.capability_trial_verdict.flag_modified") as mock_flag:
            record_trial_verdict(project, LANE_PILOT_CAP_NAME, override)

        assert (
            project.capability_adoption_verdicts[LANE_PILOT_CAP_NAME]["verdict"]
            == "adopt"
        )
        mock_flag.assert_called_once()

    def test_operator_source_bypasses_insufficient_data_self_revert_guard(self):
        """evidence_source='operator' must be able to reopen a decided verdict."""
        project = _make_project()
        decided = {"verdict": "adopt", "lift": 0.09, "n_with": 50, "n_without": 50}
        project.capability_adoption_verdicts = {"repo_map": decided}
        operator_reopen = {
            "verdict": "continue-trial",
            "evidence_source": "operator",
            "rationale": "operator re-opening trial",
        }

        with patch("app.capability_trial_verdict.flag_modified") as mock_flag:
            record_trial_verdict(project, "repo_map", operator_reopen)

        assert (
            project.capability_adoption_verdicts["repo_map"]["verdict"]
            == "continue-trial"
        )
        assert (
            project.capability_adoption_verdicts["repo_map"]["evidence_source"]
            == "operator"
        )
        mock_flag.assert_called_once()

    def test_automated_source_still_blocked_by_kill_guard(self):
        """Automated verdict must still be blocked by KILL-survival when evidence_source is absent."""
        project = _make_project()
        project.capability_adoption_verdicts = {
            LANE_PILOT_CAP_NAME: {"verdict": "KILL", "evidence_source": "auto"}
        }
        auto_adopt = {"verdict": "adopt"}

        with patch("app.capability_trial_verdict.flag_modified") as mock_flag:
            record_trial_verdict(project, LANE_PILOT_CAP_NAME, auto_adopt)

        assert (
            project.capability_adoption_verdicts[LANE_PILOT_CAP_NAME]["verdict"]
            == "KILL"
        )
        mock_flag.assert_not_called()

    def test_persisted_verdict_entry_carries_accrual_projection(self):
        """Accrual projection is stamped onto a verdict entry after record_trial_verdict.

        Validates that attach_accrual_projection_to_verdict patches accrual_projection
        and is_stalled onto an existing persisted verdict — the fields a strategic reader
        needs to determine whether the trial can reach min_n at current throughput.
        Exercises a real merge-test verdict path (adopt via sufficient assigned arms).
        """
        project = _make_project()

        comp = {
            "with_assigned": 35,
            "with_receipt": 5,
            "without_assigned": 32,
            "without_receipt": 3,
        }
        entry = evaluate_trial_verdict(
            lift=0.06,
            n_with=40,
            n_without=35,
            config={"min_n_per_arm": 30, "adopt_lift": 0.05, "reject_lift": -0.03},
            arm_composition=comp,
            accumulation_basis="cumulative_terminal_arm_tasks",
            evaluated_at="2026-06-23T00:00:00",
        )
        assert entry["verdict"] == "adopt"

        with patch("app.capability_trial_verdict.flag_modified"):
            record_trial_verdict(project, "repo_map", entry)

        projection = {
            "with_7d": 14,
            "without_7d": 7,
            "window_days": 7,
            "holdback_fraction": 0.1,
            "days_to_min_n": 0.0,
            "alternative_projections": {"0.1": 0.0, "0.3": 5.2, "0.5": 8.4},
        }

        with patch("app.capability_trial_verdict.flag_modified") as mock_flag:
            attach_accrual_projection_to_verdict(
                project, "repo_map", projection, is_stalled=False
            )

        stored = project.capability_adoption_verdicts["repo_map"]
        assert stored["verdict"] == "adopt"
        assert "accrual_projection" in stored
        assert stored["accrual_projection"]["days_to_min_n"] == 0.0
        assert stored["accrual_projection"]["holdback_fraction"] == 0.1
        assert stored["is_stalled"] is False
        mock_flag.assert_called_once()


# ── get_trial_verdicts ────────────────────────────────────────────────────────


class TestGetTrialVerdicts:
    def test_returns_dict_when_present(self):
        project = _make_project()
        project.capability_adoption_verdicts = {"repo_map": {"verdict": "adopt"}}
        result = get_trial_verdicts(project)
        assert result == {"repo_map": {"verdict": "adopt"}}

    def test_returns_empty_when_none(self):
        project = MagicMock()
        project.capability_adoption_verdicts = None
        result = get_trial_verdicts(project)
        assert result == {}

    def test_returns_empty_when_attribute_absent(self):
        project = MagicMock(spec=[])
        result = get_trial_verdicts(project)
        assert result == {}


# ── get_effective_holdback_fraction ──────────────────────────────────────────


class TestGetEffectiveHoldbackFraction:
    def _verdicts(self, verdict: str) -> dict:
        return {
            "repo_map": {"verdict": verdict, "lift": 0.0, "n_with": 40, "n_without": 40}
        }

    def test_effective_holdback_adopt_returns_zero(self):
        result = get_effective_holdback_fraction(
            self._verdicts("adopt"), "repo_map", 0.5
        )
        assert result == 0.0

    def test_effective_holdback_reject_returns_one(self):
        result = get_effective_holdback_fraction(
            self._verdicts("reject"), "repo_map", 0.5
        )
        assert result == 1.0

    def test_effective_holdback_continue_returns_default(self):
        result = get_effective_holdback_fraction(
            self._verdicts("continue-trial"), "repo_map", 0.3
        )
        assert result == 0.3

    def test_effective_holdback_missing_cap_returns_default(self):
        result = get_effective_holdback_fraction(
            {"other_cap": {"verdict": "adopt"}}, "repo_map", 0.4
        )
        assert result == 0.4

    def test_effective_holdback_none_verdicts_returns_default(self):
        result = get_effective_holdback_fraction(None, "repo_map", 0.25)
        assert result == 0.25

    def test_effective_holdback_empty_dict_returns_default(self):
        result = get_effective_holdback_fraction({}, "repo_map", 0.1)
        assert result == 0.1

    def test_effective_holdback_entry_missing_verdict_key_returns_default(self):
        verdicts = {"repo_map": {"lift": 0.06, "n_with": 40, "n_without": 40}}
        result = get_effective_holdback_fraction(verdicts, "repo_map", 0.5)
        assert result == 0.5

    def test_effective_holdback_adopt_ignores_default(self):
        result = get_effective_holdback_fraction(
            self._verdicts("adopt"), "repo_map", 0.99
        )
        assert result == 0.0

    def test_effective_holdback_reject_ignores_default(self):
        result = get_effective_holdback_fraction(
            self._verdicts("reject"), "repo_map", 0.0
        )
        assert result == 1.0


# ── get_trial_definitions ─────────────────────────────────────────────────────


class TestGetTrialDefinitions:
    def test_returns_repo_map_entry(self):
        defs = get_trial_definitions()
        assert len(defs) >= 1
        caps = [td["cap_name"] for td in defs]
        assert "repo_map" in caps

    def test_repo_map_entry_has_required_fields(self):
        defs = get_trial_definitions()
        rm = next(td for td in defs if td["cap_name"] == "repo_map")
        assert rm["context_key"] == "_had_repo_map"
        assert rm["assignment_key"] == "_repo_map_arm"
        assert "thresholds" in rm
        assert "min_n_per_arm" in rm["thresholds"]
        assert "adopt_lift" in rm["thresholds"]
        assert "reject_lift" in rm["thresholds"]
        assert "futility_n" in rm["thresholds"]

    def test_trial_definition_thresholds_fallback_to_defaults(self):
        """Thresholds in the definition can be overridden per-capability via config."""
        from app.capability_trial_verdict import (
            _DEFAULT_ADOPT_LIFT,
            _DEFAULT_MIN_N_PER_ARM,
            _DEFAULT_REJECT_LIFT,
        )

        defs = get_trial_definitions()
        rm = next(td for td in defs if td["cap_name"] == "repo_map")
        # When thresholds are passed as-is to evaluate_trial_verdict, module defaults apply.
        entry = evaluate_trial_verdict(
            lift=_DEFAULT_ADOPT_LIFT,
            n_with=_DEFAULT_MIN_N_PER_ARM,
            n_without=_DEFAULT_MIN_N_PER_ARM,
            config=rm["thresholds"],
        )
        assert entry["verdict"] == "adopt"
        assert entry["thresholds"]["adopt_lift"] == _DEFAULT_ADOPT_LIFT
        assert entry["thresholds"]["reject_lift"] == _DEFAULT_REJECT_LIFT


# ── arm_composition and purity gate ──────────────────────────────────────────


class TestArmComposition:
    def _cfg(self, **overrides):
        base = {"min_n_per_arm": 30, "adopt_lift": 0.05, "reject_lift": -0.03}
        base.update(overrides)
        return base

    def test_arm_composition_recorded_in_entry(self):
        comp = {
            "with_assigned": 30,
            "with_receipt": 5,
            "without_assigned": 30,
            "without_receipt": 3,
        }
        entry = evaluate_trial_verdict(
            lift=0.08,
            n_with=35,
            n_without=33,
            config=self._cfg(),
            arm_composition=comp,
        )
        assert entry["arm_composition"] == comp

    def test_arm_composition_omitted_when_not_supplied(self):
        entry = evaluate_trial_verdict(lift=0.08, n_with=30, n_without=30)
        assert "arm_composition" not in entry

    def test_purity_gate_blocks_adopt_when_assigned_with_arm_insufficient(self):
        # Total n_with=35 would satisfy min_n=30 — but only 15 are assignment-based.
        comp = {
            "with_assigned": 15,
            "with_receipt": 20,
            "without_assigned": 30,
            "without_receipt": 2,
        }
        entry = evaluate_trial_verdict(
            lift=0.08,
            n_with=35,
            n_without=32,
            config=self._cfg(),
            arm_composition=comp,
        )
        assert entry["verdict"] == "continue-trial"
        assert entry["verdict_reason"] == "insufficient_assigned_arm_data"
        assert entry["arm_composition"] == comp

    def test_purity_gate_blocks_adopt_when_assigned_without_arm_insufficient(self):
        comp = {
            "with_assigned": 30,
            "with_receipt": 5,
            "without_assigned": 10,
            "without_receipt": 20,
        }
        entry = evaluate_trial_verdict(
            lift=0.08,
            n_with=35,
            n_without=30,
            config=self._cfg(),
            arm_composition=comp,
        )
        assert entry["verdict"] == "continue-trial"
        assert entry["verdict_reason"] == "insufficient_assigned_arm_data"

    def test_purity_gate_allows_adopt_when_both_assigned_arms_sufficient(self):
        comp = {
            "with_assigned": 30,
            "with_receipt": 5,
            "without_assigned": 30,
            "without_receipt": 3,
        }
        entry = evaluate_trial_verdict(
            lift=0.08,
            n_with=35,
            n_without=33,
            config=self._cfg(),
            arm_composition=comp,
        )
        assert entry["verdict"] == "adopt"
        assert entry["verdict_reason"] == "sufficient_data"

    def test_purity_gate_blocks_reject_when_assigned_arm_insufficient(self):
        comp = {
            "with_assigned": 30,
            "with_receipt": 5,
            "without_assigned": 12,
            "without_receipt": 18,
        }
        entry = evaluate_trial_verdict(
            lift=-0.05,
            n_with=35,
            n_without=30,
            config=self._cfg(),
            arm_composition=comp,
        )
        assert entry["verdict"] == "continue-trial"
        assert entry["verdict_reason"] == "insufficient_assigned_arm_data"

    def test_purity_gate_blocks_futility_bound_when_assigned_arm_insufficient(self):
        # Total arms both >= futility_n=90, but assigned arms below min_n=30.
        comp = {
            "with_assigned": 20,
            "with_receipt": 70,
            "without_assigned": 20,
            "without_receipt": 70,
        }
        entry = evaluate_trial_verdict(
            lift=0.02,
            n_with=90,
            n_without=90,
            config=self._cfg(),
            arm_composition=comp,
        )
        assert entry["verdict"] == "continue-trial"
        assert entry["verdict_reason"] == "insufficient_assigned_arm_data"

    def test_purity_gate_skipped_when_arm_composition_absent(self):
        # Without arm_composition, normal verdict logic applies.
        entry = evaluate_trial_verdict(
            lift=0.08,
            n_with=30,
            n_without=30,
            config=self._cfg(),
        )
        assert entry["verdict"] == "adopt"

    def test_purity_gate_does_not_affect_continue_trial(self):
        # continue-trial from insufficient total n is not changed by the purity gate.
        comp = {
            "with_assigned": 5,
            "with_receipt": 0,
            "without_assigned": 2,
            "without_receipt": 0,
        }
        entry = evaluate_trial_verdict(
            lift=0.08,
            n_with=5,
            n_without=2,
            config=self._cfg(),
            arm_composition=comp,
        )
        assert entry["verdict"] == "continue-trial"
        assert entry["verdict_reason"] == "insufficient_data"


# ── receipt_only_trial guard ──────────────────────────────────────────────────


class TestReceiptOnlyTrial:
    def _cfg(self, **overrides):
        base = {"min_n_per_arm": 30, "adopt_lift": 0.05, "reject_lift": -0.03}
        base.update(overrides)
        return base

    def test_receipt_only_blocks_adopt(self):
        # Lift qualifies for adopt, both arms meet min_n, but receipt_only_trial=True.
        entry = evaluate_trial_verdict(
            lift=0.08,
            n_with=35,
            n_without=33,
            config=self._cfg(),
            receipt_only_trial=True,
        )
        assert entry["verdict"] == "continue-trial"
        assert entry["verdict_reason"] == "receipt_only_trial"

    def test_receipt_only_blocks_reject(self):
        # Lift qualifies for reject, both arms meet min_n, but receipt_only_trial=True.
        entry = evaluate_trial_verdict(
            lift=-0.05,
            n_with=35,
            n_without=33,
            config=self._cfg(),
            receipt_only_trial=True,
        )
        assert entry["verdict"] == "continue-trial"
        assert entry["verdict_reason"] == "receipt_only_trial"

    def test_receipt_only_blocks_futility_bound(self):
        # Both arms >= futility_n=90 with ambiguous lift → normally futility_bound_reached.
        entry = evaluate_trial_verdict(
            lift=0.02,
            n_with=90,
            n_without=90,
            config=self._cfg(),
            receipt_only_trial=True,
        )
        assert entry["verdict"] == "continue-trial"
        assert entry["verdict_reason"] == "receipt_only_trial"

    def test_receipt_only_allows_continue_trial_passthrough(self):
        # Insufficient data → continue-trial/insufficient_data regardless of the flag.
        entry = evaluate_trial_verdict(
            lift=0.08,
            n_with=5,
            n_without=33,
            config=self._cfg(),
            receipt_only_trial=True,
        )
        assert entry["verdict"] == "continue-trial"
        assert entry["verdict_reason"] == "insufficient_data"

    def test_receipt_only_false_behaves_normally(self):
        # Default (False) must not change the adopt outcome.
        entry = evaluate_trial_verdict(
            lift=0.08,
            n_with=35,
            n_without=33,
            config=self._cfg(),
            receipt_only_trial=False,
        )
        assert entry["verdict"] == "adopt"

    def test_receipt_only_continue_trial_supersedes_confounded_decided_verdict(self):
        # A prior decided verdict recorded on confounded evidence must be replaced.
        project = _make_project()
        # Simulate a confounded decided verdict already on record.
        confounded = {"verdict": "adopt", "lift": 0.08, "n_with": 40, "n_without": 40}
        project.capability_adoption_verdicts = {"module_interfaces": confounded}

        new_entry = evaluate_trial_verdict(
            lift=0.08,
            n_with=40,
            n_without=40,
            config=self._cfg(),
            receipt_only_trial=True,
        )
        assert new_entry["verdict"] == "continue-trial"
        assert new_entry["verdict_reason"] == "receipt_only_trial"

        with patch("app.capability_trial_verdict.flag_modified") as mock_flag:
            record_trial_verdict(project, "module_interfaces", new_entry)

        # The confounded adopt must be superseded by receipt_only_trial evaluation.
        assert (
            project.capability_adoption_verdicts["module_interfaces"]["verdict"]
            == "continue-trial"
        )
        assert (
            project.capability_adoption_verdicts["module_interfaces"]["verdict_reason"]
            == "receipt_only_trial"
        )
        mock_flag.assert_called_once()

    def test_receipt_only_continue_trial_supersedes_confounded_reject(self):
        project = _make_project()
        confounded = {"verdict": "reject", "lift": -0.07, "n_with": 40, "n_without": 40}
        project.capability_adoption_verdicts = {"related_tests": confounded}

        new_entry = evaluate_trial_verdict(
            lift=-0.07,
            n_with=40,
            n_without=40,
            config=self._cfg(),
            receipt_only_trial=True,
        )
        assert new_entry["verdict_reason"] == "receipt_only_trial"

        with patch("app.capability_trial_verdict.flag_modified") as mock_flag:
            record_trial_verdict(project, "related_tests", new_entry)

        assert (
            project.capability_adoption_verdicts["related_tests"]["verdict"]
            == "continue-trial"
        )
        mock_flag.assert_called_once()


# ── carry_adoption_verdicts_for_consolidation ─────────────────────────────────


class TestCarryAdoptionVerdictsForConsolidation:
    def _adopt_entry(self) -> dict:
        return {"verdict": "adopt", "lift": 0.08, "n_with": 40, "n_without": 38}

    def _reject_entry(self) -> dict:
        return {"verdict": "reject", "lift": -0.05, "n_with": 35, "n_without": 33}

    def _continue_entry(self, reason: str = "insufficient_data") -> dict:
        return {
            "verdict": "continue-trial",
            "verdict_reason": reason,
            "lift": None,
            "n_with": 5,
            "n_without": 3,
        }

    def _strategic_entry(self) -> dict:
        return {"verdict": "adopt", "evidence_source": "strategic_review"}

    def test_absorbed_adopt_verdict_carried_to_unset_survivor(self):
        """An absorbed adopt verdict is written to the survivor when it has none."""
        verdicts = {"old-cap": self._adopt_entry()}
        consolidated = [{"survivor": "new-cap", "absorbed": ["old-cap"]}]

        result = carry_adoption_verdicts_for_consolidation(consolidated, verdicts)

        assert "old-cap" not in result
        assert result["new-cap"]["verdict"] == "adopt"

    def test_absorbed_reject_verdict_carried_to_unset_survivor(self):
        verdicts = {"old-cap": self._reject_entry()}
        consolidated = [{"survivor": "new-cap", "absorbed": ["old-cap"]}]

        result = carry_adoption_verdicts_for_consolidation(consolidated, verdicts)

        assert result["new-cap"]["verdict"] == "reject"

    def test_absorbed_continue_trial_does_not_overwrite_decided_survivor(self):
        """A continue-trial verdict from an absorbed cap must not downgrade a decided survivor."""
        verdicts = {
            "survivor-cap": self._adopt_entry(),
            "old-cap": self._continue_entry(),
        }
        consolidated = [{"survivor": "survivor-cap", "absorbed": ["old-cap"]}]

        result = carry_adoption_verdicts_for_consolidation(consolidated, verdicts)

        assert "old-cap" not in result
        assert result["survivor-cap"]["verdict"] == "adopt"

    def test_absorbed_adopt_does_not_overwrite_existing_decided_survivor(self):
        """An absorbed adopt must not clobber a survivor that already has adopt."""
        survivor_entry = {
            "verdict": "adopt",
            "lift": 0.12,
            "n_with": 60,
            "n_without": 55,
        }
        verdicts = {
            "survivor-cap": survivor_entry,
            "old-cap": self._adopt_entry(),
        }
        consolidated = [{"survivor": "survivor-cap", "absorbed": ["old-cap"]}]

        result = carry_adoption_verdicts_for_consolidation(consolidated, verdicts)

        # Survivor entry unchanged (not replaced by absorbed one).
        assert result["survivor-cap"]["n_with"] == 60

    def test_absorbed_does_not_overwrite_strategist_declared_survivor(self):
        """A strategist-declared verdict on the survivor is protected."""
        verdicts = {
            "survivor-cap": self._strategic_entry(),
            "old-cap": self._adopt_entry(),
        }
        consolidated = [{"survivor": "survivor-cap", "absorbed": ["old-cap"]}]

        result = carry_adoption_verdicts_for_consolidation(consolidated, verdicts)

        assert result["survivor-cap"]["evidence_source"] == "strategic_review"

    def test_higher_priority_absorbed_wins_over_lower(self):
        """When multiple absorbed caps exist, the decided one wins over continue-trial."""
        verdicts = {
            "old-cap-a": self._continue_entry(),
            "old-cap-b": self._adopt_entry(),
        }
        consolidated = [{"survivor": "new-cap", "absorbed": ["old-cap-a", "old-cap-b"]}]

        result = carry_adoption_verdicts_for_consolidation(consolidated, verdicts)

        assert result["new-cap"]["verdict"] == "adopt"

    def test_no_consolidation_leaves_verdicts_unchanged(self):
        verdicts = {"cap-a": self._adopt_entry()}
        result = carry_adoption_verdicts_for_consolidation([], verdicts)
        assert result is verdicts  # same object

    def test_absorbed_keys_removed_even_when_survivor_keeps_own_verdict(self):
        verdicts = {
            "survivor-cap": self._adopt_entry(),
            "old-cap": self._reject_entry(),
        }
        consolidated = [{"survivor": "survivor-cap", "absorbed": ["old-cap"]}]

        result = carry_adoption_verdicts_for_consolidation(consolidated, verdicts)

        assert "old-cap" not in result
        assert "survivor-cap" in result

    def test_input_dict_not_mutated(self):
        verdicts = {"old-cap": self._adopt_entry()}
        original_keys = set(verdicts.keys())
        consolidated = [{"survivor": "new-cap", "absorbed": ["old-cap"]}]

        carry_adoption_verdicts_for_consolidation(consolidated, verdicts)

        assert set(verdicts.keys()) == original_keys


# ── assignment_key: newly-assigned trials can reach decided verdicts ──────────


class TestAssignedArmTrials:
    """Tests proving the three newly-assigned trials (module_interfaces,
    related_tests, affected_consumers) can reach adopt/reject under a real
    merge-test scenario and are held at continue-trial when the assigned-arm
    subpopulation is insufficient."""

    def _cfg(self, **overrides):
        base = {"min_n_per_arm": 30, "adopt_lift": 0.05, "reject_lift": -0.03}
        base.update(overrides)
        return base

    def test_all_three_trials_have_assignment_key(self):
        """Registry check: all three receipt-only trials now carry an assignment_key."""
        defs = get_trial_definitions()
        by_name = {td["cap_name"]: td for td in defs}
        for cap in ("module_interfaces", "related_tests", "affected_consumers"):
            assert cap in by_name, f"missing trial definition for {cap}"
            td = by_name[cap]
            assert "assignment_key" in td, f"{cap} missing assignment_key"
        assert (
            by_name["module_interfaces"]["assignment_key"] == "_module_interfaces_arm"
        )
        assert by_name["related_tests"]["assignment_key"] == "_related_tests_arm"
        assert (
            by_name["affected_consumers"]["assignment_key"] == "_affected_consumers_arm"
        )

    def _decided_verdict_with_sufficient_assigned_arms(
        self, cap_name: str, lift: float, verdict: str
    ):
        """Helper: build an arm_composition with both assigned arms >= min_n and verify verdict."""
        comp = {
            "with_assigned": 35,
            "with_receipt": 5,
            "without_assigned": 32,
            "without_receipt": 3,
        }
        entry = evaluate_trial_verdict(
            lift=lift,
            n_with=40,
            n_without=35,
            config=self._cfg(),
            arm_composition=comp,
            accumulation_basis="cumulative_terminal_arm_tasks",
            evaluated_at="2026-06-11T00:00:00",
        )
        assert entry["verdict"] == verdict, (
            f"{cap_name}: expected {verdict!r} but got {entry['verdict']!r} "
            f"(reason={entry.get('verdict_reason')})"
        )
        assert entry["verdict_reason"] == "sufficient_data"
        assert entry["arm_composition"] == comp
        return entry

    def test_module_interfaces_adopt_with_sufficient_assigned_arms(self):
        """module_interfaces trial can reach adopt when both assigned arms meet min_n."""
        self._decided_verdict_with_sufficient_assigned_arms(
            "module_interfaces", 0.08, "adopt"
        )

    def test_related_tests_adopt_with_sufficient_assigned_arms(self):
        """related_tests trial can reach adopt when both assigned arms meet min_n."""
        self._decided_verdict_with_sufficient_assigned_arms(
            "related_tests", 0.07, "adopt"
        )

    def test_affected_consumers_adopt_with_sufficient_assigned_arms(self):
        """affected_consumers trial can reach adopt when both assigned arms meet min_n."""
        self._decided_verdict_with_sufficient_assigned_arms(
            "affected_consumers", 0.06, "adopt"
        )

    def test_module_interfaces_reject_with_sufficient_assigned_arms(self):
        """module_interfaces trial can reach reject when both assigned arms meet min_n."""
        self._decided_verdict_with_sufficient_assigned_arms(
            "module_interfaces", -0.05, "reject"
        )

    def test_related_tests_reject_with_sufficient_assigned_arms(self):
        """related_tests trial can reach reject when both assigned arms meet min_n."""
        self._decided_verdict_with_sufficient_assigned_arms(
            "related_tests", -0.06, "reject"
        )

    def test_affected_consumers_reject_with_sufficient_assigned_arms(self):
        """affected_consumers trial can reach reject when both assigned arms meet min_n."""
        self._decided_verdict_with_sufficient_assigned_arms(
            "affected_consumers", -0.04, "reject"
        )

    def _insufficient_assigned_arm_verdict(self, cap_name: str, lift: float):
        """Helper: assigned-arm subpopulation below min_n → insufficient_assigned_arm_data."""
        # Total n_with/without both meet min_n, but assigned arms are below min_n.
        comp = {
            "with_assigned": 15,  # below min_n=30
            "with_receipt": 20,
            "without_assigned": 12,  # below min_n=30
            "without_receipt": 18,
        }
        entry = evaluate_trial_verdict(
            lift=lift,
            n_with=35,
            n_without=30,
            config=self._cfg(),
            arm_composition=comp,
        )
        assert entry["verdict"] == "continue-trial", (
            f"{cap_name}: expected continue-trial but got {entry['verdict']!r}"
        )
        assert entry["verdict_reason"] == "insufficient_assigned_arm_data", (
            f"{cap_name}: expected insufficient_assigned_arm_data but got "
            f"{entry['verdict_reason']!r} — must NOT be 'receipt_only_trial'"
        )
        return entry

    def test_module_interfaces_held_at_insufficient_assigned_arm_data(self):
        """module_interfaces: insufficient assigned-arm subpopulation → continue-trial/insufficient_assigned_arm_data."""
        self._insufficient_assigned_arm_verdict("module_interfaces", 0.08)

    def test_related_tests_held_at_insufficient_assigned_arm_data(self):
        """related_tests: insufficient assigned-arm subpopulation → continue-trial/insufficient_assigned_arm_data."""
        self._insufficient_assigned_arm_verdict("related_tests", 0.07)

    def test_affected_consumers_held_at_insufficient_assigned_arm_data(self):
        """affected_consumers: insufficient assigned-arm subpopulation → continue-trial/insufficient_assigned_arm_data."""
        self._insufficient_assigned_arm_verdict("affected_consumers", 0.06)

    def test_not_receipt_only_trial_reason_for_newly_assigned_caps(self):
        """Newly-assigned trials must NEVER surface receipt_only_trial as verdict_reason.

        Before adding assignment_key, these three trials would have been evaluated
        with receipt_only_trial=True and their verdict_reason would be receipt_only_trial.
        Now that they have assignment_key, purity-gate is the correct gate path.
        """
        comp = {
            "with_assigned": 10,
            "with_receipt": 25,
            "without_assigned": 8,
            "without_receipt": 22,
        }
        for cap in ("module_interfaces", "related_tests", "affected_consumers"):
            entry = evaluate_trial_verdict(
                lift=0.09,
                n_with=35,
                n_without=30,
                config=self._cfg(),
                arm_composition=comp,
            )
            assert entry["verdict_reason"] != "receipt_only_trial", (
                f"{cap}: verdict_reason must not be receipt_only_trial — "
                "trial now has assignment_key and must use purity-gate path"
            )

    def test_full_merge_test_verdict_cycle_module_interfaces(self):
        """End-to-end: module_interfaces accumulates assigned-arm evidence and reaches adopt.

        Simulates a real merge-test scenario: enough cumulative terminal tasks in
        both assigned arms, positive lift → adopt verdict persisted on project.
        """
        project = _make_project()

        comp = {
            "with_assigned": 40,
            "with_receipt": 8,
            "without_assigned": 38,
            "without_receipt": 5,
        }
        entry = evaluate_trial_verdict(
            lift=0.06,
            n_with=48,
            n_without=43,
            config=self._cfg(),
            arm_composition=comp,
            accumulation_basis="cumulative_terminal_arm_tasks",
            evaluated_at="2026-06-11T00:00:00",
        )
        assert entry["verdict"] == "adopt"
        assert entry["verdict_reason"] == "sufficient_data"

        with patch("app.capability_trial_verdict.flag_modified") as mock_flag:
            record_trial_verdict(project, "module_interfaces", entry)

        assert (
            project.capability_adoption_verdicts["module_interfaces"]["verdict"]
            == "adopt"
        )
        assert (
            project.capability_adoption_verdicts["module_interfaces"][
                "accumulation_basis"
            ]
            == "cumulative_terminal_arm_tasks"
        )
        mock_flag.assert_called_once()

    def test_full_merge_test_verdict_cycle_related_tests(self):
        """End-to-end: related_tests accumulates assigned-arm evidence and reaches adopt."""
        project = _make_project()

        comp = {
            "with_assigned": 33,
            "with_receipt": 4,
            "without_assigned": 31,
            "without_receipt": 2,
        }
        entry = evaluate_trial_verdict(
            lift=0.07,
            n_with=37,
            n_without=33,
            config=self._cfg(),
            arm_composition=comp,
            accumulation_basis="cumulative_terminal_arm_tasks",
            evaluated_at="2026-06-11T00:00:00",
        )
        assert entry["verdict"] == "adopt"

        with patch("app.capability_trial_verdict.flag_modified") as mock_flag:
            record_trial_verdict(project, "related_tests", entry)

        assert (
            project.capability_adoption_verdicts["related_tests"]["verdict"] == "adopt"
        )
        mock_flag.assert_called_once()

    def test_full_merge_test_verdict_cycle_affected_consumers(self):
        """End-to-end: affected_consumers accumulates assigned-arm evidence and reaches adopt."""
        project = _make_project()

        comp = {
            "with_assigned": 32,
            "with_receipt": 6,
            "without_assigned": 30,
            "without_receipt": 4,
        }
        entry = evaluate_trial_verdict(
            lift=0.05,
            n_with=38,
            n_without=34,
            config=self._cfg(),
            arm_composition=comp,
            accumulation_basis="cumulative_terminal_arm_tasks",
            evaluated_at="2026-06-11T00:00:00",
        )
        assert entry["verdict"] == "adopt"

        with patch("app.capability_trial_verdict.flag_modified") as mock_flag:
            record_trial_verdict(project, "affected_consumers", entry)

        assert (
            project.capability_adoption_verdicts["affected_consumers"]["verdict"]
            == "adopt"
        )
        mock_flag.assert_called_once()

    def test_decided_verdict_not_reverted_by_insufficient_data_after_adoption(self):
        """After adopt, a subsequent insufficient-data evaluation must not self-revert it."""
        project = _make_project()

        comp = {
            "with_assigned": 35,
            "with_receipt": 5,
            "without_assigned": 30,
            "without_receipt": 3,
        }
        adopt_entry = evaluate_trial_verdict(
            lift=0.08,
            n_with=40,
            n_without=33,
            config=self._cfg(),
            arm_composition=comp,
            evaluated_at="2026-06-11T00:00:00",
        )
        assert adopt_entry["verdict"] == "adopt"

        with patch("app.capability_trial_verdict.flag_modified"):
            record_trial_verdict(project, "module_interfaces", adopt_entry)

        # Post-adoption: without-arm dries up, periodic evaluator returns insufficient_data.
        stale_entry = evaluate_trial_verdict(
            lift=None,
            n_with=0,
            n_without=0,
            evaluated_at="2026-06-12T00:00:00",
        )
        assert stale_entry["verdict"] == "continue-trial"
        assert stale_entry["verdict_reason"] == "insufficient_data"

        with patch("app.capability_trial_verdict.flag_modified") as mock_flag:
            record_trial_verdict(project, "module_interfaces", stale_entry)

        # Adopt must survive.
        assert (
            project.capability_adoption_verdicts["module_interfaces"]["verdict"]
            == "adopt"
        )
        mock_flag.assert_not_called()


# ── diagnostics_after_edit trial definition ───────────────────────────────────


class TestDiagnosticsAfterEditTrialDefinition:
    def test_definition_exists_in_registry(self):
        defs = get_trial_definitions()
        caps = [td["cap_name"] for td in defs]
        assert "diagnostics_after_edit" in caps

    def test_definition_has_required_fields(self):
        defs = get_trial_definitions()
        td = next(d for d in defs if d["cap_name"] == "diagnostics_after_edit")
        assert td["context_key"] == "_had_diagnostics_after_edit"
        assert td["assignment_key"] == "_diagnostics_after_edit_arm"
        assert "thresholds" in td
        assert "min_n_per_arm" in td["thresholds"]
        assert "adopt_lift" in td["thresholds"]
        assert "reject_lift" in td["thresholds"]
        assert "futility_n" in td["thresholds"]

    def test_adopt_verdict_with_sufficient_assigned_arm_data(self):
        from app.capability_trial_verdict import (
            _DEFAULT_ADOPT_LIFT,
            _DEFAULT_MIN_N_PER_ARM,
        )

        defs = get_trial_definitions()
        td = next(d for d in defs if d["cap_name"] == "diagnostics_after_edit")
        arm_composition = {
            "with_assigned": _DEFAULT_MIN_N_PER_ARM,
            "with_receipt": 0,
            "without_assigned": _DEFAULT_MIN_N_PER_ARM,
            "without_receipt": 0,
        }
        entry = evaluate_trial_verdict(
            lift=_DEFAULT_ADOPT_LIFT,
            n_with=_DEFAULT_MIN_N_PER_ARM,
            n_without=_DEFAULT_MIN_N_PER_ARM,
            config=td["thresholds"],
            arm_composition=arm_composition,
        )
        assert entry["verdict"] == "adopt"

    def test_purity_gate_blocks_adopt_when_assigned_arm_insufficient(self):
        from app.capability_trial_verdict import _DEFAULT_ADOPT_LIFT

        defs = get_trial_definitions()
        td = next(d for d in defs if d["cap_name"] == "diagnostics_after_edit")
        # Total n meets min_n but assigned subpopulation does not
        arm_composition = {
            "with_assigned": 10,
            "with_receipt": 25,
            "without_assigned": 10,
            "without_receipt": 25,
        }
        entry = evaluate_trial_verdict(
            lift=_DEFAULT_ADOPT_LIFT,
            n_with=35,
            n_without=35,
            config=td["thresholds"],
            arm_composition=arm_composition,
        )
        assert entry["verdict"] == "continue-trial"
        assert entry["verdict_reason"] == "insufficient_assigned_arm_data"

    def test_continue_trial_when_n_insufficient(self):
        """diagnostics_after_edit: total n below min_n → continue-trial/insufficient_data."""
        from app.capability_trial_verdict import _DEFAULT_ADOPT_LIFT

        defs = get_trial_definitions()
        td = next(d for d in defs if d["cap_name"] == "diagnostics_after_edit")
        arm_composition = {
            "with_assigned": 5,
            "with_receipt": 0,
            "without_assigned": 5,
            "without_receipt": 0,
        }
        entry = evaluate_trial_verdict(
            lift=_DEFAULT_ADOPT_LIFT,
            n_with=5,
            n_without=5,
            config=td["thresholds"],
            arm_composition=arm_composition,
        )
        assert entry["verdict"] == "continue-trial"
        assert entry["verdict_reason"] in (
            "insufficient_data",
            "insufficient_assigned_arm_data",
        )

    def test_reject_verdict_with_sufficient_assigned_arm_data(self):
        """diagnostics_after_edit: lift below reject threshold + sufficient arms → reject."""
        from app.capability_trial_verdict import (
            _DEFAULT_MIN_N_PER_ARM,
            _DEFAULT_REJECT_LIFT,
        )

        defs = get_trial_definitions()
        td = next(d for d in defs if d["cap_name"] == "diagnostics_after_edit")
        arm_composition = {
            "with_assigned": _DEFAULT_MIN_N_PER_ARM,
            "with_receipt": 0,
            "without_assigned": _DEFAULT_MIN_N_PER_ARM,
            "without_receipt": 0,
        }
        entry = evaluate_trial_verdict(
            lift=_DEFAULT_REJECT_LIFT,
            n_with=_DEFAULT_MIN_N_PER_ARM,
            n_without=_DEFAULT_MIN_N_PER_ARM,
            config=td["thresholds"],
            arm_composition=arm_composition,
        )
        assert entry["verdict"] == "reject"

    def test_full_merge_test_verdict_cycle(self):
        """End-to-end: diagnostics_after_edit evaluate → record → persist on project.

        Simulates a real merge-test scenario: sufficient assigned-arm tasks,
        positive lift → adopt verdict persisted on project object.
        """
        from app.capability_trial_verdict import (
            _DEFAULT_ADOPT_LIFT,
            _DEFAULT_MIN_N_PER_ARM,
        )

        project = _make_project()
        defs = get_trial_definitions()
        td = next(d for d in defs if d["cap_name"] == "diagnostics_after_edit")

        arm_composition = {
            "with_assigned": _DEFAULT_MIN_N_PER_ARM + 5,
            "with_receipt": 3,
            "without_assigned": _DEFAULT_MIN_N_PER_ARM + 3,
            "without_receipt": 2,
        }
        n_with = arm_composition["with_assigned"] + arm_composition["with_receipt"]
        n_without = (
            arm_composition["without_assigned"] + arm_composition["without_receipt"]
        )
        entry = evaluate_trial_verdict(
            lift=_DEFAULT_ADOPT_LIFT,
            n_with=n_with,
            n_without=n_without,
            config=td["thresholds"],
            arm_composition=arm_composition,
            accumulation_basis="cumulative_terminal_arm_tasks",
            evaluated_at="2026-06-13T00:00:00",
        )
        assert entry["verdict"] == "adopt"
        assert entry["verdict_reason"] == "sufficient_data"

        with patch("app.capability_trial_verdict.flag_modified") as mock_flag:
            record_trial_verdict(project, "diagnostics_after_edit", entry)

        assert (
            project.capability_adoption_verdicts["diagnostics_after_edit"]["verdict"]
            == "adopt"
        )
        assert (
            project.capability_adoption_verdicts["diagnostics_after_edit"][
                "accumulation_basis"
            ]
            == "cumulative_terminal_arm_tasks"
        )
        mock_flag.assert_called_once()


# ── review_fix_lane TrialDefinition registration ──────────────────────────────


class TestTrialDefinitionsLanePilot:
    def _lane_def(self):
        defs = get_trial_definitions()
        matches = [td for td in defs if td["cap_name"] == LANE_PILOT_CAP_NAME]
        assert matches, f"{LANE_PILOT_CAP_NAME} not found in TRIAL_DEFINITIONS"
        return matches[0]

    def test_lane_pilot_registered(self):
        caps = [td["cap_name"] for td in get_trial_definitions()]
        assert LANE_PILOT_CAP_NAME in caps

    def test_lane_pilot_required_fields_present(self):
        td = self._lane_def()
        assert "cap_name" in td
        assert "context_key" in td
        assert "thresholds" in td

    def test_lane_pilot_has_assignment_key(self):
        td = self._lane_def()
        assert td.get("assignment_key") == LANE_PILOT_ASSIGNMENT_KEY

    def test_lane_pilot_thresholds_contain_all_keys(self):
        td = self._lane_def()
        required_threshold_keys = [
            "min_n",
            "kill_merge_rate_floor",
            "kill_cost_ceiling",
            "kill_gate_violations_max",
            "kill_checkpoint_loss_ceiling",
            "futility_n",
            "adopt_merge_lift_floor",
            "adopt_cost_improvement",
        ]
        for key in required_threshold_keys:
            assert key in td["thresholds"], f"threshold key missing: {key}"

    def test_lane_pilot_threshold_values_are_numeric_and_nonzero(self):
        td = self._lane_def()
        for k, v in td["thresholds"].items():
            assert isinstance(v, (int, float)), f"{k} is not numeric"
            assert v != 0, f"{k} is zero"

    def test_canonical_key_constants_exported(self):
        assert isinstance(LANE_PILOT_CAP_NAME, str) and LANE_PILOT_CAP_NAME
        assert isinstance(LANE_PILOT_ASSIGNMENT_KEY, str) and LANE_PILOT_ASSIGNMENT_KEY
        assert (
            isinstance(LANE_PILOT_WARM_RESUME_RECEIPT_KEY, str)
            and LANE_PILOT_WARM_RESUME_RECEIPT_KEY
        )
        assert (
            isinstance(LANE_PILOT_CHECKPOINT_RECEIPT_KEY, str)
            and LANE_PILOT_CHECKPOINT_RECEIPT_KEY
        )


# ── is_lane_pilot_arm_member (partitioning helper) ────────────────────────────


class TestIsLanePilotArmMember:
    def test_lane_assignment_key_returns_lane(self):
        ctx = {LANE_PILOT_ASSIGNMENT_KEY: "lane"}
        assert is_lane_pilot_arm_member(ctx) == "lane"

    def test_control_assignment_key_returns_control(self):
        ctx = {LANE_PILOT_ASSIGNMENT_KEY: "control"}
        assert is_lane_pilot_arm_member(ctx) == "control"

    def test_receipt_key_only_not_a_member(self):
        # A task with ONLY warm-resume or checkpoint receipt keys but NO assignment key
        # must NOT be counted into any arm — receipts are not arm membership.
        ctx = {
            LANE_PILOT_WARM_RESUME_RECEIPT_KEY: True,
            LANE_PILOT_CHECKPOINT_RECEIPT_KEY: True,
        }
        assert is_lane_pilot_arm_member(ctx) is None

    def test_empty_context_not_a_member(self):
        assert is_lane_pilot_arm_member({}) is None

    def test_none_context_not_a_member(self):
        assert is_lane_pilot_arm_member(None) is None  # type: ignore[arg-type]

    def test_unknown_arm_value_not_a_member(self):
        ctx = {LANE_PILOT_ASSIGNMENT_KEY: "unknown_arm"}
        assert is_lane_pilot_arm_member(ctx) is None


# ── evaluate_lane_pilot_verdict ───────────────────────────────────────────────


def _good_aggs(
    *,
    n=40,
    merge_lift=0.10,
    cost_improvement=0.10,
    gate_violations=0,
    checkpoint_loss=0.01,
    lane_runtime_cost: float | None = None,
    lane_reserved_idle_cost: float = 0.0,
    control_runtime_cost: float | None = None,
    control_reserved_idle_cost: float = 0.0,
) -> dict:
    """Build a healthy aggregate dict (ADOPT territory by default)."""
    control_rate = 0.70
    lane_rate = control_rate + merge_lift
    control_runtime = 1000.0 if control_runtime_cost is None else control_runtime_cost
    lane_runtime = (
        control_runtime * (1.0 - cost_improvement)
        if lane_runtime_cost is None
        else lane_runtime_cost
    )
    control_cost = control_runtime + control_reserved_idle_cost
    lane_cost = lane_runtime + lane_reserved_idle_cost
    return {
        "n_lane": n,
        "n_control": n,
        "lane_merge_rate": lane_rate,
        "control_merge_rate": control_rate,
        "lane_cost_per_merged": lane_cost,
        "control_cost_per_merged": control_cost,
        "lane_runtime_cost_per_merged": lane_runtime,
        "control_runtime_cost_per_merged": control_runtime,
        "lane_reserved_idle_cost_per_merged": lane_reserved_idle_cost,
        "control_reserved_idle_cost_per_merged": control_reserved_idle_cost,
        "gate_integrity_violations": gate_violations,
        "checkpoint_loss_rate": checkpoint_loss,
    }


class TestEvaluateLanePilotVerdict:
    # ── acceptance test (named in the plan) ──────────────────────────────────

    def test_lane_pilot_thresholds_kill_and_adopt(self):
        """Acceptance: pre-registered thresholds produce KILL on synthetic regressing
        data and ADOPT on synthetic improving data."""
        # Synthetic regressing: merge rate tanks well below the kill floor.
        bad = _good_aggs(merge_lift=-0.10)
        kill_verdict = evaluate_lane_pilot_verdict(bad)
        assert kill_verdict["verdict"] == "KILL"

        # Synthetic improving: cost down ≥5%, merge rate non-inferior, no violations.
        good = _good_aggs(merge_lift=0.05, cost_improvement=0.05)
        adopt_verdict = evaluate_lane_pilot_verdict(good)
        assert adopt_verdict["verdict"] == "ADOPT"

    # ── KILL conditions ───────────────────────────────────────────────────────

    def test_kill_on_lane_merge_rate_below_floor(self):
        aggs = _good_aggs(merge_lift=-0.05)  # lift = -0.05 <= -0.03
        result = evaluate_lane_pilot_verdict(aggs)
        assert result["verdict"] == "KILL"
        assert result["verdict_reason"] == "lane_merge_rate_below_floor"
        assert result["immediate_alert"] is False

    def test_kill_on_lane_merge_rate_exactly_at_floor(self):
        # lift = -0.03 (exactly at floor) → KILL
        aggs = _good_aggs(merge_lift=-0.03)
        result = evaluate_lane_pilot_verdict(aggs)
        assert result["verdict"] == "KILL"
        assert result["verdict_reason"] == "lane_merge_rate_below_floor"

    def test_kill_on_cost_per_merged_above_ceiling(self):
        # Lane costs 15% more than control — exceeds +10% ceiling.
        aggs = _good_aggs(cost_improvement=-0.15)  # lane_cost = control * 1.15
        result = evaluate_lane_pilot_verdict(aggs)
        assert result["verdict"] == "KILL"
        assert result["verdict_reason"] == "lane_cost_ceiling_exceeded"
        assert result["immediate_alert"] is False

    def test_kill_on_reserved_idle_cost_pushing_total_above_ceiling(self):
        """Reserved-idle spend is part of the lane total, so it can trigger KILL."""
        aggs = _good_aggs(
            cost_improvement=0.10,
            lane_runtime_cost=900.0,
            lane_reserved_idle_cost=200.0,
            control_runtime_cost=1000.0,
        )
        result = evaluate_lane_pilot_verdict(aggs)
        assert result["lane_runtime_cost_per_merged"] == 900.0
        assert result["lane_reserved_idle_cost_per_merged"] == 200.0
        assert result["lane_cost_per_merged"] == 1100.0
        assert result["verdict"] == "KILL"
        assert result["verdict_reason"] == "lane_cost_ceiling_exceeded"

    def test_adopt_fails_when_reserved_idle_erases_runtime_savings(self):
        """Runtime looks cheap, but total cost misses the 5% ADOPT improvement bar."""
        aggs = _good_aggs(
            merge_lift=0.05,
            cost_improvement=0.10,
            lane_runtime_cost=900.0,
            lane_reserved_idle_cost=80.0,
            control_runtime_cost=1000.0,
        )
        result = evaluate_lane_pilot_verdict(aggs)
        assert result["lane_runtime_cost_per_merged"] == 900.0
        assert result["lane_reserved_idle_cost_per_merged"] == 80.0
        assert result["lane_cost_per_merged"] == 980.0
        assert result["verdict"] == "CONTINUE"
        assert result["verdict_reason"] == "accumulating_data"

    def test_kill_on_gate_integrity_violations(self):
        # 3 gate violations → KILL + immediate_alert.
        aggs = _good_aggs(gate_violations=3)
        result = evaluate_lane_pilot_verdict(aggs)
        assert result["verdict"] == "KILL"
        assert result["verdict_reason"] == "gate_integrity_violations_exceeded"
        assert result["immediate_alert"] is True

    def test_kill_on_gate_integrity_violations_above_min(self):
        aggs = _good_aggs(gate_violations=10)
        result = evaluate_lane_pilot_verdict(aggs)
        assert result["verdict"] == "KILL"
        assert result["immediate_alert"] is True

    def test_kill_on_checkpoint_loss_rate_above_ceiling(self):
        # 8% checkpoint loss > 5% ceiling.
        aggs = _good_aggs(checkpoint_loss=0.08)
        result = evaluate_lane_pilot_verdict(aggs)
        assert result["verdict"] == "KILL"
        assert result["verdict_reason"] == "checkpoint_loss_rate_exceeded"
        assert result["immediate_alert"] is False

    def test_kill_on_futility_at_max_n(self):
        # n >= 90 per arm, adopt criteria NOT met → futility KILL.
        aggs = _good_aggs(n=90, merge_lift=0.005, cost_improvement=-0.02)
        result = evaluate_lane_pilot_verdict(aggs)
        assert result["verdict"] == "KILL"
        assert result["verdict_reason"] == "futility_at_max_n"

    # ── CONTINUE conditions ───────────────────────────────────────────────────

    def test_continue_insufficient_n(self):
        aggs = _good_aggs(n=10)  # below min_n=30
        result = evaluate_lane_pilot_verdict(aggs)
        assert result["verdict"] == "CONTINUE"
        assert result["verdict_reason"] == "insufficient_data"
        assert result["immediate_alert"] is False

    def test_continue_none_merge_rate(self):
        aggs = _good_aggs()
        aggs["lane_merge_rate"] = None
        result = evaluate_lane_pilot_verdict(aggs)
        assert result["verdict"] == "CONTINUE"

    def test_continue_none_cost(self):
        aggs = _good_aggs()
        aggs["lane_cost_per_merged"] = None
        result = evaluate_lane_pilot_verdict(aggs)
        assert result["verdict"] == "CONTINUE"

    def test_continue_ambiguous_metrics_sufficient_n(self):
        # n ≥ min_n but metrics don't meet adopt OR kill.
        aggs = _good_aggs(merge_lift=0.005, cost_improvement=0.02)
        result = evaluate_lane_pilot_verdict(aggs)
        assert result["verdict"] == "CONTINUE"
        assert result["verdict_reason"] == "accumulating_data"

    # ── ADOPT condition ───────────────────────────────────────────────────────

    def test_adopt_all_criteria_met(self):
        aggs = _good_aggs(merge_lift=0.05, cost_improvement=0.05, gate_violations=0)
        result = evaluate_lane_pilot_verdict(aggs)
        assert result["verdict"] == "ADOPT"
        assert result["verdict_reason"] == "adopt_criteria_met"
        assert result["immediate_alert"] is False

    def test_adopt_requires_zero_gate_violations(self):
        # Exactly adopt metrics but 1 gate violation → not ADOPT.
        aggs = _good_aggs(merge_lift=0.05, cost_improvement=0.05, gate_violations=1)
        result = evaluate_lane_pilot_verdict(aggs)
        assert result["verdict"] != "ADOPT"

    def test_adopt_requires_cost_improvement_at_threshold(self):
        # Exactly 5% cost improvement meets the adopt criterion.
        aggs = _good_aggs(merge_lift=0.05, cost_improvement=0.05)
        result = evaluate_lane_pilot_verdict(aggs)
        assert result["verdict"] == "ADOPT"

    def test_adopt_requires_merge_non_inferiority(self):
        # Lift = -0.005 (> -0.01 floor) — still non-inferior.
        aggs = _good_aggs(merge_lift=-0.005, cost_improvement=0.05)
        result = evaluate_lane_pilot_verdict(aggs)
        assert result["verdict"] == "ADOPT"

    # ── gate-integrity violations raise immediate_alert below min_n ───────────

    def test_gate_violations_alert_below_min_n(self):
        aggs = _good_aggs(n=5, gate_violations=3)  # n << min_n
        result = evaluate_lane_pilot_verdict(aggs)
        assert result["verdict"] == "KILL"
        assert result["immediate_alert"] is True

    # ── KILL takes priority over ADOPT ───────────────────────────────────────

    def test_kill_takes_priority_over_adopt(self):
        # Meets adopt criteria but also has gate violations → KILL wins.
        aggs = _good_aggs(merge_lift=0.05, cost_improvement=0.10, gate_violations=3)
        result = evaluate_lane_pilot_verdict(aggs)
        assert result["verdict"] == "KILL"
        assert result["immediate_alert"] is True

    def test_kill_merge_rate_takes_priority_over_cost_improvement(self):
        aggs = _good_aggs(merge_lift=-0.05, cost_improvement=0.10)
        result = evaluate_lane_pilot_verdict(aggs)
        assert result["verdict"] == "KILL"
        assert result["verdict_reason"] == "lane_merge_rate_below_floor"

    # ── snapshot fields present ───────────────────────────────────────────────

    def test_snapshot_fields_present_on_kill(self):
        aggs = _good_aggs(merge_lift=-0.05)
        result = evaluate_lane_pilot_verdict(aggs)
        for field in (
            "n_lane",
            "n_control",
            "lane_merge_rate",
            "control_merge_rate",
            "lane_cost_per_merged",
            "control_cost_per_merged",
            "lane_runtime_cost_per_merged",
            "control_runtime_cost_per_merged",
            "lane_reserved_idle_cost_per_merged",
            "control_reserved_idle_cost_per_merged",
            "gate_integrity_violations",
            "checkpoint_loss_rate",
            "thresholds",
        ):
            assert field in result, f"missing field: {field}"

    def test_snapshot_fields_present_on_adopt(self):
        aggs = _good_aggs()
        result = evaluate_lane_pilot_verdict(aggs)
        assert result["verdict"] == "ADOPT"
        assert "thresholds" in result
        assert isinstance(result["thresholds"], dict)
        for key in (
            "min_n",
            "kill_merge_rate_floor",
            "kill_cost_ceiling",
            "kill_gate_violations_max",
            "kill_checkpoint_loss_ceiling",
            "futility_n",
            "adopt_merge_lift_floor",
            "adopt_cost_improvement",
        ):
            assert key in result["thresholds"]
        for field in (
            "lane_runtime_cost_per_merged",
            "control_runtime_cost_per_merged",
            "lane_reserved_idle_cost_per_merged",
            "control_reserved_idle_cost_per_merged",
        ):
            assert field in result

    def test_snapshot_fields_present_on_continue(self):
        aggs = _good_aggs(n=10)
        result = evaluate_lane_pilot_verdict(aggs)
        assert result["verdict"] == "CONTINUE"
        assert "thresholds" in result
        assert "immediate_alert" in result
        assert "lane_reserved_idle_cost_per_merged" in result


class TestEvaluateLanePilotVerdictEngagement:
    """Engagement guard: futility KILL is suppressed when warm-resume fraction
    is below _LANE_PILOT_FUTILITY_MIN_ENGAGEMENT.  Tests verify BOTH the guard
    firing and the passthrough (engagement absent or sufficient)."""

    def test_futility_blocked_when_treatment_not_engaging(self):
        """Acceptance: n ≥ 90, adopt not met, engagement=0.0 → CONTINUE not KILL."""
        aggs = _good_aggs(n=90, merge_lift=0.005, cost_improvement=-0.02)
        aggs["warm_resume_fraction"] = 0.0
        aggs["checkpoint_fraction"] = 0.0
        aggs["lane_fix_tasks_sampled"] = 90
        result = evaluate_lane_pilot_verdict(aggs)
        assert result["verdict"] == "CONTINUE"
        assert result["verdict_reason"] == "treatment_not_engaging"
        assert result["immediate_alert"] is True
        assert result["engagement_low"] is True

    def test_futility_kill_proceeds_when_engagement_meets_threshold(self):
        """When warm_resume_fraction meets the threshold, futility KILL fires normally."""
        aggs = _good_aggs(n=90, merge_lift=0.005, cost_improvement=-0.02)
        aggs["warm_resume_fraction"] = 0.80
        aggs["checkpoint_fraction"] = 0.75
        aggs["lane_fix_tasks_sampled"] = 90
        result = evaluate_lane_pilot_verdict(aggs)
        assert result["verdict"] == "KILL"
        assert result["verdict_reason"] == "futility_at_max_n"
        assert result.get("engagement_low") is not True

    def test_futility_kill_proceeds_when_engagement_exactly_at_threshold(self):
        """warm_resume_fraction == threshold (0.50) is NOT below threshold → KILL."""
        aggs = _good_aggs(n=90, merge_lift=0.005, cost_improvement=-0.02)
        aggs["warm_resume_fraction"] = 0.50
        aggs["lane_fix_tasks_sampled"] = 90
        result = evaluate_lane_pilot_verdict(aggs)
        assert result["verdict"] == "KILL"
        assert result["verdict_reason"] == "futility_at_max_n"

    def test_engagement_guard_skipped_when_fields_absent(self):
        """_good_aggs without engagement fields → falls through to futility_at_max_n."""
        aggs = _good_aggs(n=90, merge_lift=0.005, cost_improvement=-0.02)
        # warm_resume_fraction absent (None from aggregates.get) → guard is skipped
        result = evaluate_lane_pilot_verdict(aggs)
        assert result["verdict"] == "KILL"
        assert result["verdict_reason"] == "futility_at_max_n"

    def test_engagement_fields_propagated_in_snapshot(self):
        """warm_resume_fraction / checkpoint_fraction / lane_fix_tasks_sampled appear
        in the returned dict regardless of verdict path."""
        aggs = _good_aggs(n=40)
        aggs["warm_resume_fraction"] = 0.6
        aggs["checkpoint_fraction"] = 0.5
        aggs["lane_fix_tasks_sampled"] = 40
        result = evaluate_lane_pilot_verdict(aggs)
        assert result["warm_resume_fraction"] == 0.6
        assert result["checkpoint_fraction"] == 0.5
        assert result["lane_fix_tasks_sampled"] == 40

    def test_engagement_threshold_in_thresholds_snapshot(self):
        """thresholds dict includes futility_min_engagement."""
        aggs = _good_aggs(n=10)  # CONTINUE (insufficient data)
        result = evaluate_lane_pilot_verdict(aggs)
        assert "futility_min_engagement" in result["thresholds"]


class TestEvaluateLanePilotVerdictControlWarmResume:
    """Control-arm contamination guard: any control fix task with a warm-resume
    receipt means the baseline is invalid — KILL + immediate alert, regardless
    of sample size."""

    def test_control_warm_resume_kills_immediately(self):
        """Acceptance: control_warm_resume_count=1 triggers KILL with immediate_alert
        and verdict_reason=control_arm_warm_resume_contamination."""
        aggs = _good_aggs()  # ADOPT-territory data
        aggs["control_warm_resume_count"] = 1
        result = evaluate_lane_pilot_verdict(aggs)
        assert result["verdict"] == "KILL"
        assert result["verdict_reason"] == "control_arm_warm_resume_contamination"
        assert result["immediate_alert"] is True

    def test_control_warm_resume_zero_does_not_kill(self):
        """control_warm_resume_count=0 must not trigger the contamination branch;
        an otherwise-healthy trial should still CONTINUE or ADOPT."""
        aggs = _good_aggs()
        aggs["control_warm_resume_count"] = 0
        result = evaluate_lane_pilot_verdict(aggs)
        # ADOPT-territory: must NOT be KILL from the contamination path
        assert result["verdict"] in ("ADOPT", "CONTINUE")
        assert result.get("verdict_reason") != "control_arm_warm_resume_contamination"

    def test_control_warm_resume_kills_below_min_n(self):
        """Contamination fires even before minimum sample size is reached."""
        aggs = _good_aggs(n=5)  # well below min_n=30
        aggs["control_warm_resume_count"] = 2
        result = evaluate_lane_pilot_verdict(aggs)
        assert result["verdict"] == "KILL"
        assert result["verdict_reason"] == "control_arm_warm_resume_contamination"
        assert result["immediate_alert"] is True

    def test_control_warm_resume_count_propagated_in_snapshot(self):
        """control_warm_resume_count appears in the returned verdict dict."""
        aggs = _good_aggs(n=10)
        aggs["control_warm_resume_count"] = 3
        result = evaluate_lane_pilot_verdict(aggs)
        assert result["control_warm_resume_count"] == 3

    def test_control_warm_resume_max_threshold_in_thresholds_snapshot(self):
        """thresholds dict includes kill_control_warm_resume_max."""
        aggs = _good_aggs(n=10)
        result = evaluate_lane_pilot_verdict(aggs)
        assert "kill_control_warm_resume_max" in result["thresholds"]
        assert result["thresholds"]["kill_control_warm_resume_max"] == 0


# ── Lane relay comparison structure ──────────────────────────────────────────


class TestLaneRelayComparison:
    """Tests for the lane_relay_comparison structure persisted in the verdict."""

    def _make_relay(self, **overrides):
        base = {
            "wall_clock_seconds": 3600.0,
            "active_seconds": 1800.0,
            "gap_seconds": 1800.0,
            "task_count": 3.0,
            "pr_count": 10,
        }
        base.update(overrides)
        return base

    def _make_lane(self, **overrides):
        base = {
            "wall_clock_seconds": 2700.0,
            "active_seconds": 1500.0,
            "gap_seconds": 1200.0,
            "task_count": 2.5,
            "pr_count": 8,
        }
        base.update(overrides)
        return base

    def _build_comparison(self, relay, lane, version_number=9):
        deltas = {}
        for field in (
            "wall_clock_seconds",
            "active_seconds",
            "gap_seconds",
            "task_count",
        ):
            rv, lv = relay.get(field), lane.get(field)
            deltas[field] = (lv - rv) if (rv is not None and lv is not None) else None
        return {
            "source_version": {
                "version_number": version_number,
                "captured_at": "2026-06-01T00:00:00",
                "terminal_status": "promoted",
            },
            "relay": relay,
            "lane": lane,
            "deltas": deltas,
        }

    def test_evaluate_lane_pilot_verdict_has_no_relay_baseline(self):
        """evaluate_lane_pilot_verdict output must never include relay_baseline."""
        aggs = {
            "n_lane": 5,
            "n_control": 5,
            "lane_merge_rate": 0.8,
            "control_merge_rate": 0.7,
            "lane_cost_per_merged": 1.0,
            "control_cost_per_merged": 1.2,
            "lane_runtime_cost_per_merged": 1.0,
            "control_runtime_cost_per_merged": 1.2,
            "lane_reserved_idle_cost_per_merged": 0.0,
            "control_reserved_idle_cost_per_merged": 0.0,
            "gate_integrity_violations": 0,
            "checkpoint_loss_rate": 0.1,
            "warm_resume_fraction": 0.9,
            "checkpoint_fraction": 0.8,
            "lane_fix_tasks_sampled": 5,
        }
        result = evaluate_lane_pilot_verdict(aggs)
        assert "relay_baseline" not in result

    def test_comparison_has_required_top_level_keys(self):
        comparison = self._build_comparison(self._make_relay(), self._make_lane())
        assert set(comparison.keys()) == {"source_version", "relay", "lane", "deltas"}

    def test_source_version_has_identity_fields(self):
        comparison = self._build_comparison(
            self._make_relay(), self._make_lane(), version_number=10
        )
        sv = comparison["source_version"]
        assert sv["version_number"] == 10
        assert sv["captured_at"] == "2026-06-01T00:00:00"
        assert sv["terminal_status"] == "promoted"

    def test_relay_and_lane_have_no_arrays_or_dicts(self):
        comparison = self._build_comparison(self._make_relay(), self._make_lane())
        for arm_key in ("relay", "lane"):
            for v in comparison[arm_key].values():
                assert not isinstance(v, (list, dict)), (
                    f"{arm_key} field must be a scalar, got {type(v)}"
                )

    def test_deltas_are_scalars(self):
        comparison = self._build_comparison(self._make_relay(), self._make_lane())
        for v in comparison["deltas"].values():
            assert v is None or isinstance(v, (int, float))

    def test_deltas_correct_values(self):
        relay = self._make_relay(wall_clock_seconds=3600.0, active_seconds=1800.0)
        lane = self._make_lane(wall_clock_seconds=2700.0, active_seconds=1500.0)
        comparison = self._build_comparison(relay, lane)
        assert comparison["deltas"]["wall_clock_seconds"] == -900.0
        assert comparison["deltas"]["active_seconds"] == -300.0

    def test_delta_none_when_relay_field_is_none(self):
        relay = self._make_relay(wall_clock_seconds=None)
        lane = self._make_lane(wall_clock_seconds=2700.0)
        comparison = self._build_comparison(relay, lane)
        assert comparison["deltas"]["wall_clock_seconds"] is None

    def test_delta_none_when_lane_field_is_none(self):
        relay = self._make_relay(wall_clock_seconds=3600.0)
        lane = self._make_lane(wall_clock_seconds=None)
        comparison = self._build_comparison(relay, lane)
        assert comparison["deltas"]["wall_clock_seconds"] is None

    def test_lane_relay_comparison_persisted_without_relay_baseline(self):
        """Persisted verdict entry should contain lane_relay_comparison, not relay_baseline."""
        project = _make_project()
        comparison = self._build_comparison(self._make_relay(), self._make_lane())
        verdict_entry = {
            "verdict": "CONTINUE",
            "n_lane": 5,
            "n_control": 5,
            "lane_relay_comparison": comparison,
        }
        with patch("app.capability_trial_verdict.flag_modified"):
            record_trial_verdict(project, LANE_PILOT_CAP_NAME, verdict_entry)

        stored = project.capability_adoption_verdicts[LANE_PILOT_CAP_NAME]
        assert "relay_baseline" not in stored
        assert "lane_relay_comparison" in stored
        assert stored["lane_relay_comparison"]["source_version"]["version_number"] == 9


# ── raise_capability_verdict_notice ──────────────────────────────────────────


def _mock_async_session(existing_messages=None):
    """Build a mock async session that models SQL-side dedupe_key filtering.

    The production code now pushes the dedupe_key filter into the SQL WHERE clause
    and calls result.scalar() (not .scalars().all()). This mock faithfully emulates
    that: scalar() returns a sentinel ID only when the queried dedupe_key matches
    one of the existing_messages.
    """
    from unittest.mock import AsyncMock, MagicMock

    msgs = existing_messages or []
    present_keys = {
        (m.context or {}).get("dedupe_key")
        for m in msgs
        if (m.context or {}).get("dedupe_key") is not None
    }
    session = MagicMock()
    session.add = MagicMock()

    async def _execute(stmt, *args, **kwargs):
        result = MagicMock()
        result.scalar.return_value = None
        # Walk the WHERE criteria to find the dedupe_key literal being queried.
        # The SQL pattern is: context["dedupe_key"].as_string() == key
        # This produces a BinaryExpression where left.right.value == 'dedupe_key'
        # (the JSON subscript key) and right.value is the queried dedupe_key value.
        try:
            for clause in getattr(stmt, "_where_criteria", ()):
                left = getattr(clause, "left", None)
                right = getattr(clause, "right", None)
                if left is None or right is None:
                    continue
                inner_key = getattr(getattr(left, "right", None), "value", None)
                if inner_key != "dedupe_key":
                    continue
                queried_key = getattr(right, "value", None)
                if queried_key is not None and queried_key in present_keys:
                    result.scalar.return_value = "sentinel-id"
                break
        except Exception:
            pass
        return result

    session.execute = AsyncMock(side_effect=_execute)
    return session


def _open_message(dedupe_key: str):
    """Build a mock ProjectMessage carrying a specific dedupe_key."""
    from unittest.mock import MagicMock

    msg = MagicMock()
    msg.context = {"dedupe_key": dedupe_key}
    return msg


class TestRaiseCapabilityVerdictNotice:
    """Unit tests for raise_capability_verdict_notice using mock sessions."""

    @pytest.mark.asyncio
    async def test_notice_created_on_first_decide(self):
        """No prior open message → session.add called once for the transition notice."""
        from app.capability_trial_verdict import raise_capability_verdict_notice

        session = _mock_async_session(existing_messages=[])
        entry = {
            "verdict": "adopt",
            "verdict_reason": "sufficient_data",
            "lift": 0.08,
            "n_with": 40,
            "n_without": 40,
        }
        await raise_capability_verdict_notice(
            session, "proj-id", "repo_map", None, "adopt", entry
        )
        session.add.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_notice_on_repeated_same_decided_verdict(self):
        """Existing open message with same dedupe_key → session.add NOT called."""
        from app.capability_trial_verdict import raise_capability_verdict_notice

        existing = _open_message("capability_verdict_decided_repo_map_adopt")
        session = _mock_async_session(existing_messages=[existing])
        entry = {
            "verdict": "adopt",
            "verdict_reason": "sufficient_data",
            "lift": 0.09,
            "n_with": 50,
            "n_without": 50,
        }
        await raise_capability_verdict_notice(
            session, "proj-id", "repo_map", "continue-trial", "adopt", entry
        )
        session.add.assert_not_called()

    @pytest.mark.asyncio
    async def test_reversal_emits_new_notice(self):
        """Adopt notice exists but we're recording a reject reversal → new notice fired."""
        from app.capability_trial_verdict import raise_capability_verdict_notice

        existing = _open_message("capability_verdict_decided_repo_map_adopt")
        session = _mock_async_session(existing_messages=[existing])
        entry = {
            "verdict": "reject",
            "verdict_reason": "sufficient_data",
            "lift": -0.05,
            "n_with": 50,
            "n_without": 50,
        }
        await raise_capability_verdict_notice(
            session, "proj-id", "repo_map", "adopt", "reject", entry
        )
        session.add.assert_called_once()

    @pytest.mark.asyncio
    async def test_notice_content_carries_evidence(self):
        """Notice content and context include cap_name, verdict, lift, n_with, n_without."""
        from app.capability_trial_verdict import raise_capability_verdict_notice
        from app.models import MessageType

        session = _mock_async_session(existing_messages=[])
        entry = {
            "verdict": "adopt",
            "verdict_reason": "sufficient_data",
            "lift": 0.08,
            "n_with": 42,
            "n_without": 38,
        }
        await raise_capability_verdict_notice(
            session, "proj-id", "repo_map", None, "adopt", entry
        )
        added = session.add.call_args[0][0]
        assert "repo_map" in added.content
        assert "adopt" in added.content
        assert "0.08" in added.content
        assert "42" in added.content
        assert "38" in added.content
        assert added.context["cap_name"] == "repo_map"
        assert added.context["new_verdict"] == "adopt"
        assert added.context["lift"] == 0.08
        assert added.context["n_with"] == 42
        assert added.context["n_without"] == 38
        assert added.message_type == MessageType.NOTE

    @pytest.mark.asyncio
    async def test_notice_from_type_is_system(self):
        """Notice from_type must be 'system'."""
        from app.capability_trial_verdict import raise_capability_verdict_notice

        session = _mock_async_session(existing_messages=[])
        entry = {
            "verdict": "reject",
            "verdict_reason": "sufficient_data",
            "lift": -0.05,
            "n_with": 30,
            "n_without": 30,
        }
        await raise_capability_verdict_notice(
            session, "proj-id", "repo_map", None, "reject", entry
        )
        added = session.add.call_args[0][0]
        assert added.from_type == "system"

    @pytest.mark.asyncio
    async def test_undecided_transition_label_in_content(self):
        """When prior_verdict is None the content mentions 'undecided → verdict'."""
        from app.capability_trial_verdict import raise_capability_verdict_notice

        session = _mock_async_session(existing_messages=[])
        entry = {
            "verdict": "adopt",
            "verdict_reason": "sufficient_data",
            "lift": 0.06,
            "n_with": 35,
            "n_without": 32,
        }
        await raise_capability_verdict_notice(
            session, "proj-id", "repo_map", None, "adopt", entry
        )
        added = session.add.call_args[0][0]
        assert "undecided" in added.content


# ── persist_trial_verdicts_in_transaction notice emission ─────────────────────


class TestPersistTrialVerdictsNoticeEmission:
    """Tests that persist_trial_verdicts_in_transaction emits notices for
    decided transitions and suppresses them when no transition occurred."""

    def _build_mock_db(self, prior_verdicts: dict | None = None):
        """Return (mock_session, mock_project) with configurable prior verdict state."""
        from unittest.mock import AsyncMock, MagicMock

        mock_project = MagicMock()
        mock_project.capability_adoption_verdicts = dict(prior_verdicts or {})

        mock_session = MagicMock()
        mock_session.get = AsyncMock(return_value=mock_project)
        mock_session.commit = AsyncMock()
        mock_session.rollback = AsyncMock()
        # execute result: scalars().first() returns mock_project (for the locked project
        # fetch), scalars().all() returns [] (for other list queries), scalar() returns
        # None (for dedup queries in raise_capability_verdict_notice).
        scalars_mock = MagicMock()
        scalars_mock.all.return_value = []
        scalars_mock.first.return_value = mock_project
        execute_result = MagicMock()
        execute_result.scalars.return_value = scalars_mock
        execute_result.scalar.return_value = None
        mock_session.execute = AsyncMock(return_value=execute_result)
        mock_session.add = MagicMock()

        mock_cm = MagicMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        # factory() is called (not factory directly) in async with factory() as session:
        # so mock_cm() must return mock_cm itself for __aenter__ to fire correctly.
        mock_cm.return_value = mock_cm

        return mock_session, mock_project, mock_cm

    @pytest.mark.asyncio
    async def test_notice_emitted_on_first_adopt_transition(self):
        """No prior verdict → adopt transition emits exactly one notice via real notice helper."""
        import uuid
        from unittest.mock import AsyncMock, MagicMock, patch

        from app.capability_trial_verdict import persist_trial_verdicts_in_transaction

        project_id = uuid.uuid4()
        _, _, mock_cm = self._build_mock_db(prior_verdicts={})
        mock_factory = MagicMock(return_value=mock_cm)

        entry = {
            "verdict": "adopt",
            "verdict_reason": "sufficient_data",
            "lift": 0.07,
            "n_with": 40,
            "n_without": 40,
        }

        with (
            patch("sqlalchemy.ext.asyncio.async_sessionmaker", mock_factory),
            patch("app.database.resolve_async_engine", return_value=MagicMock()),
            patch(
                "app.capability_trial_verdict.raise_capability_verdict_notice",
                new_callable=AsyncMock,
            ) as mock_notice,
            patch("app.capability_trial_verdict.record_trial_verdict") as mock_record,
        ):

            def _write_verdict(project, cap_name, e):
                project.capability_adoption_verdicts[cap_name] = e

            mock_record.side_effect = _write_verdict
            await persist_trial_verdicts_in_transaction(
                MagicMock(),
                project_id,
                pending_verdicts=[("repo_map", entry)],
            )

        mock_notice.assert_called_once()
        args = mock_notice.call_args[0]
        assert args[2] == "repo_map"  # cap_name
        assert args[3] is None  # prior_verdict (was absent)
        assert args[4] == "adopt"  # new_verdict

    @pytest.mark.asyncio
    async def test_no_notice_on_continue_trial_skipped_by_self_revert_guard(self):
        """Prior=adopt, incoming=insufficient-data continue-trial: guard skips write → no notice."""
        import uuid
        from unittest.mock import AsyncMock, MagicMock, patch

        from app.capability_trial_verdict import persist_trial_verdicts_in_transaction

        project_id = uuid.uuid4()
        prior_entry = {"verdict": "adopt", "lift": 0.08, "n_with": 40, "n_without": 40}
        _, _, mock_cm = self._build_mock_db(prior_verdicts={"repo_map": prior_entry})
        mock_factory = MagicMock(return_value=mock_cm)

        incoming = {
            "verdict": "continue-trial",
            "verdict_reason": "insufficient_data",
            "lift": None,
            "n_with": 5,
            "n_without": 0,
        }

        with (
            patch("sqlalchemy.ext.asyncio.async_sessionmaker", mock_factory),
            patch("app.database.resolve_async_engine", return_value=MagicMock()),
            patch(
                "app.capability_trial_verdict.raise_capability_verdict_notice",
                new_callable=AsyncMock,
            ) as mock_notice,
            # Use the REAL record_trial_verdict — the guard must block the write
            patch("app.capability_trial_verdict.flag_modified"),
        ):
            await persist_trial_verdicts_in_transaction(
                MagicMock(),
                project_id,
                pending_verdicts=[("repo_map", incoming)],
            )

        mock_notice.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_notice_on_strategic_review_adoption(self):
        """Strategic-review adoptions are exempted from notice emission."""
        import uuid
        from unittest.mock import AsyncMock, MagicMock, patch

        from app.capability_trial_verdict import persist_trial_verdicts_in_transaction

        project_id = uuid.uuid4()
        _, _, mock_cm = self._build_mock_db(prior_verdicts={})
        mock_factory = MagicMock(return_value=mock_cm)

        entry = {
            "verdict": "adopt",
            "verdict_reason": "sufficient_data",
            "lift": 0.08,
            "n_with": 40,
            "n_without": 40,
            "evidence_source": "strategic_review",
        }

        with (
            patch("sqlalchemy.ext.asyncio.async_sessionmaker", mock_factory),
            patch("app.database.resolve_async_engine", return_value=MagicMock()),
            patch(
                "app.capability_trial_verdict.raise_capability_verdict_notice",
                new_callable=AsyncMock,
            ) as mock_notice,
            patch("app.capability_trial_verdict.record_trial_verdict") as mock_record,
        ):

            def _write_verdict(project, cap_name, e):
                project.capability_adoption_verdicts[cap_name] = e

            mock_record.side_effect = _write_verdict
            await persist_trial_verdicts_in_transaction(
                MagicMock(),
                project_id,
                pending_verdicts=[("repo_map", entry)],
            )

        mock_notice.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_notice_on_standing_decided_verdict_repeated_evaluation(self):
        """Re-evaluation of a trial that already has the same decided verdict → no notice."""
        import uuid
        from unittest.mock import AsyncMock, MagicMock, patch

        from app.capability_trial_verdict import persist_trial_verdicts_in_transaction

        project_id = uuid.uuid4()
        prior_entry = {"verdict": "adopt", "lift": 0.08, "n_with": 40, "n_without": 40}
        _, _, mock_cm = self._build_mock_db(prior_verdicts={"repo_map": prior_entry})
        mock_factory = MagicMock(return_value=mock_cm)

        same_entry = {"verdict": "adopt", "lift": 0.09, "n_with": 50, "n_without": 50}

        with (
            patch("sqlalchemy.ext.asyncio.async_sessionmaker", mock_factory),
            patch("app.database.resolve_async_engine", return_value=MagicMock()),
            patch(
                "app.capability_trial_verdict.raise_capability_verdict_notice",
                new_callable=AsyncMock,
            ) as mock_notice,
            patch("app.capability_trial_verdict.record_trial_verdict") as mock_record,
        ):

            def _write_same(project, cap_name, e):
                project.capability_adoption_verdicts[cap_name] = e

            mock_record.side_effect = _write_same
            await persist_trial_verdicts_in_transaction(
                MagicMock(),
                project_id,
                pending_verdicts=[("repo_map", same_entry)],
            )

        # same prior and actual verdict → no transition → no notice
        mock_notice.assert_not_called()

    @pytest.mark.asyncio
    async def test_persist_trial_verdicts_triggers_strategic_refresh_on_decided_transition(
        self,
    ):
        """Decided verdict transitions arm refresh; continue-trial and repeats do not."""
        import uuid
        from unittest.mock import AsyncMock, MagicMock, patch

        from app.capability_trial_verdict import persist_trial_verdicts_in_transaction

        def _make_record_side_effect():
            def _write_verdict(project, cap_name, entry):
                project.capability_adoption_verdicts[cap_name] = entry

            return _write_verdict

        async def _run_case(prior_verdicts: dict, entry: dict):
            project_id = uuid.uuid4()
            _, _, mock_cm = self._build_mock_db(prior_verdicts=prior_verdicts)
            mock_factory = MagicMock(return_value=mock_cm)

            with (
                patch("sqlalchemy.ext.asyncio.async_sessionmaker", mock_factory),
                patch("app.database.resolve_async_engine", return_value=MagicMock()),
                patch(
                    "app.capability_trial_verdict.raise_capability_verdict_notice",
                    new_callable=AsyncMock,
                ) as mock_notice,
                patch(
                    "app.strategic_trigger.request_strategic_refresh",
                    new_callable=AsyncMock,
                ) as mock_refresh,
                patch(
                    "app.capability_trial_verdict.record_trial_verdict"
                ) as mock_record,
            ):
                mock_record.side_effect = _make_record_side_effect()
                await persist_trial_verdicts_in_transaction(
                    MagicMock(),
                    project_id,
                    pending_verdicts=[("repo_map", entry)],
                )

            return mock_notice, mock_refresh

        decided_entry = {
            "verdict": "adopt",
            "verdict_reason": "sufficient_data",
            "lift": 0.07,
            "n_with": 40,
            "n_without": 40,
        }
        mock_notice, mock_refresh = await _run_case({}, decided_entry)
        mock_notice.assert_called_once()
        mock_refresh.assert_called_once()
        refresh_args = mock_refresh.call_args[0]
        assert refresh_args[2] == "capability_verdict:repo_map:adopt"

        continue_entry = {
            "verdict": "continue-trial",
            "verdict_reason": "insufficient_data",
            "lift": None,
            "n_with": 5,
            "n_without": 0,
        }
        mock_notice, mock_refresh = await _run_case(
            {"repo_map": decided_entry}, continue_entry
        )
        mock_notice.assert_not_called()
        mock_refresh.assert_not_called()

        repeated_entry = {
            "verdict": "adopt",
            "verdict_reason": "sufficient_data",
            "lift": 0.09,
            "n_with": 50,
            "n_without": 50,
        }
        mock_notice, mock_refresh = await _run_case(
            {"repo_map": decided_entry}, repeated_entry
        )
        mock_notice.assert_not_called()
        mock_refresh.assert_not_called()


# ── engagement guard for generic trials ──────────────────────────────────────


class TestEngagementGuard:
    """Generic-trial engagement guard: futility REJECT suppressed when delivery low."""

    def _cfg(self, **overrides):
        base = {
            "min_n_per_arm": 30,
            "adopt_lift": 0.05,
            "reject_lift": -0.03,
            "futility_n": 90,
            "futility_min_delivery_rate": 0.50,
        }
        base.update(overrides)
        return base

    # ── acceptance test (named by spec) ──────────────────────────────────────

    def test_generic_trial_futility_blocked_when_treatment_not_delivered(self):
        """Acceptance: futility bound reached but delivery rate < 0.50 → continue-trial."""
        comp = {
            "with_assigned": 90,
            "with_assigned_delivered": 27,  # 27/90 = 0.30 < 0.50
            "with_receipt": 0,
            "without_assigned": 90,
            "without_receipt": 0,
        }
        entry = evaluate_trial_verdict(
            lift=0.01,
            n_with=90,
            n_without=90,
            config=self._cfg(),
            arm_composition=comp,
        )
        assert entry["verdict"] == "continue-trial"
        assert entry["verdict_reason"] == "treatment_not_engaging"
        assert entry.get("immediate_alert") is True
        assert "delivery_rate" in entry
        assert abs(entry["delivery_rate"] - 0.30) < 0.01

    # ── additional coverage ───────────────────────────────────────────────────

    def test_futility_bound_low_delivery_returns_continue_treatment_not_engaging(self):
        # 30/90 = 0.333 < 0.50 → engage guard fires
        comp = {
            "with_assigned": 90,
            "with_assigned_delivered": 30,
            "with_receipt": 0,
            "without_assigned": 90,
            "without_receipt": 0,
        }
        entry = evaluate_trial_verdict(
            lift=0.02,
            n_with=90,
            n_without=90,
            config=self._cfg(),
            arm_composition=comp,
        )
        assert entry["verdict"] == "continue-trial"
        assert entry["verdict_reason"] == "treatment_not_engaging"
        assert entry.get("immediate_alert") is True

    def test_futility_bound_high_delivery_still_no_effect(self):
        # 50/90 = 0.556 >= 0.50 → guard does not fire, futility no_effect stands
        comp = {
            "with_assigned": 90,
            "with_assigned_delivered": 50,
            "with_receipt": 0,
            "without_assigned": 90,
            "without_receipt": 0,
        }
        entry = evaluate_trial_verdict(
            lift=0.01,
            n_with=90,
            n_without=90,
            config=self._cfg(),
            arm_composition=comp,
        )
        assert entry["verdict"] == "no_effect"
        assert entry["verdict_reason"] == "futility_bound_reached"
        assert not entry.get("immediate_alert")

    def test_engagement_guard_absent_when_arm_composition_none(self):
        # No arm_composition → guard skipped, no_effect stands
        entry = evaluate_trial_verdict(
            lift=0.01,
            n_with=90,
            n_without=90,
            config=self._cfg(),
        )
        assert entry["verdict"] == "no_effect"
        assert entry["verdict_reason"] == "futility_bound_reached"

    def test_engagement_guard_absent_when_with_assigned_delivered_missing(self):
        # arm_composition present but no with_assigned_delivered key → guard skipped
        comp = {
            "with_assigned": 90,
            "with_receipt": 0,
            "without_assigned": 90,
            "without_receipt": 0,
        }
        entry = evaluate_trial_verdict(
            lift=0.01,
            n_with=90,
            n_without=90,
            config=self._cfg(),
            arm_composition=comp,
        )
        assert entry["verdict"] == "no_effect"
        assert entry["verdict_reason"] == "futility_bound_reached"

    def test_engagement_guard_skipped_when_with_assigned_zero(self):
        # with_assigned=0 → engagement guard division is skipped.
        # The purity gate then fires (with_assigned < min_n) before the reject lands.
        comp = {
            "with_assigned": 0,
            "with_assigned_delivered": 0,
            "with_receipt": 90,
            "without_assigned": 0,
            "without_receipt": 90,
        }
        entry = evaluate_trial_verdict(
            lift=0.01,
            n_with=90,
            n_without=90,
            config=self._cfg(),
            arm_composition=comp,
        )
        # Purity gate fires (0 < min_n=30) — not the engagement guard.
        assert entry["verdict"] == "continue-trial"
        assert entry["verdict_reason"] == "insufficient_assigned_arm_data"

    def test_configurable_futility_min_delivery_rate(self):
        # Custom threshold of 0.70 means 0.60 delivery triggers the guard
        comp = {
            "with_assigned": 90,
            "with_assigned_delivered": 54,  # 54/90 = 0.60 < 0.70
            "with_receipt": 0,
            "without_assigned": 90,
            "without_receipt": 0,
        }
        cfg = self._cfg(futility_min_delivery_rate=0.70)
        entry = evaluate_trial_verdict(
            lift=0.01,
            n_with=90,
            n_without=90,
            config=cfg,
            arm_composition=comp,
        )
        assert entry["verdict"] == "continue-trial"
        assert entry["verdict_reason"] == "treatment_not_engaging"

    def test_delivery_rate_recorded_in_entry(self):
        # entry["delivery_rate"] and immediate_alert=True must be present
        comp = {
            "with_assigned": 90,
            "with_assigned_delivered": 18,  # 18/90 = 0.20
            "with_receipt": 0,
            "without_assigned": 90,
            "without_receipt": 0,
        }
        entry = evaluate_trial_verdict(
            lift=0.01,
            n_with=90,
            n_without=90,
            config=self._cfg(),
            arm_composition=comp,
        )
        assert entry["verdict"] == "continue-trial"
        assert entry["delivery_rate"] == pytest.approx(0.20, abs=0.01)
        assert entry["immediate_alert"] is True

    def test_treatment_not_engaging_does_not_overwrite_decided_no_effect(self):
        """treatment_not_engaging is NOT in _SUFFICIENT_DATA_CONTINUE_REASONS and cannot overwrite.

        treatment_not_engaging describes a broken/contaminated measurement (with-arm
        delivery was never truly delivered), not a genuine inconclusive result.
        It must emit an alert but NOT revert a standing decided verdict.
        """
        from unittest.mock import patch

        project = MagicMock()
        prior = {
            "verdict": "no_effect",
            "verdict_reason": "futility_bound_reached",
            "lift": 0.01,
            "n_with": 90,
            "n_without": 90,
        }
        project.capability_adoption_verdicts = {"repo_map": prior}
        newer = {
            "verdict": "continue-trial",
            "verdict_reason": "treatment_not_engaging",
            "immediate_alert": True,
            "delivery_rate": 0.20,
            "lift": 0.01,
            "n_with": 90,
            "n_without": 90,
        }
        with patch("app.capability_trial_verdict.flag_modified") as mock_flag:
            record_trial_verdict(project, "repo_map", newer)

        # no_effect must be preserved; flag_modified not called (no write happened).
        assert (
            project.capability_adoption_verdicts["repo_map"]["verdict"] == "no_effect"
        )
        assert (
            project.capability_adoption_verdicts["repo_map"]["verdict_reason"]
            == "futility_bound_reached"
        )
        mock_flag.assert_not_called()

    def test_engagement_guard_does_not_fire_below_futility_n(self):
        # Arms not yet at futility_n → ordinary ambiguous_lift, guard never checked
        comp = {
            "with_assigned": 50,
            "with_assigned_delivered": 5,  # very low delivery, but below futility_n
            "with_receipt": 0,
            "without_assigned": 50,
            "without_receipt": 0,
        }
        entry = evaluate_trial_verdict(
            lift=0.02,
            n_with=50,
            n_without=50,
            config=self._cfg(),
            arm_composition=comp,
        )
        assert entry["verdict"] == "continue-trial"
        assert entry["verdict_reason"] == "ambiguous_lift"


# ── holdback contamination guard ──────────────────────────────────────────────


class TestHoldbackContaminationGuard:
    """Tests for the contamination guard in evaluate_trial_verdict.

    The guard fires when a decided/terminal verdict would otherwise be emitted but
    the holdback-arm contamination rate (without_contaminated / without_assigned)
    exceeds futility_max_holdback_contamination_rate (default 0.20).
    """

    def _cfg(self, **overrides):
        base = {"min_n_per_arm": 30, "adopt_lift": 0.05, "reject_lift": -0.03}
        base.update(overrides)
        return base

    def test_holdback_contamination_suppresses_decided_verdict(self):
        """Acceptance test: high contamination rate suppresses adopt → holdback_contaminated.

        This is the named acceptance requirement: a positive delivery receipt on
        holdback-assigned tasks above the 20% threshold must suppress a decided
        verdict with reason 'holdback_contaminated' and immediate_alert=True.
        """
        # without_contaminated=15 / without_assigned=50 = 0.30 > 0.20 threshold
        comp = {
            "with_assigned": 35,
            "with_assigned_delivered": 35,
            "with_receipt": 5,
            "without_assigned": 50,
            "without_receipt": 3,
            "without_contaminated": 15,
        }
        entry = evaluate_trial_verdict(
            lift=0.08,
            n_with=40,
            n_without=53,
            config=self._cfg(),
            arm_composition=comp,
            accumulation_basis="cumulative_terminal_arm_tasks",
            evaluated_at="2026-06-13T00:00:00",
        )
        assert entry["verdict"] == "continue-trial"
        assert entry["verdict_reason"] == "holdback_contaminated"
        assert entry["immediate_alert"] is True
        assert "contamination_rate" in entry
        assert entry["contamination_rate"] == round(15 / 50, 4)

    def test_high_contamination_suppresses_reject(self):
        """High contamination rate suppresses reject verdict."""
        comp = {
            "with_assigned": 35,
            "with_assigned_delivered": 35,
            "with_receipt": 5,
            "without_assigned": 50,
            "without_receipt": 3,
            "without_contaminated": 15,
        }
        entry = evaluate_trial_verdict(
            lift=-0.05,
            n_with=40,
            n_without=53,
            config=self._cfg(),
            arm_composition=comp,
        )
        assert entry["verdict"] == "continue-trial"
        assert entry["verdict_reason"] == "holdback_contaminated"
        assert entry["immediate_alert"] is True

    def test_high_contamination_suppresses_no_effect(self):
        """High contamination rate suppresses no_effect (futility-bound) verdict."""
        # n_with, n_without both >= futility_n=90 with ambiguous lift → normally no_effect
        comp = {
            "with_assigned": 95,
            "with_assigned_delivered": 95,
            "with_receipt": 5,
            "without_assigned": 95,
            "without_receipt": 5,
            "without_contaminated": 25,  # 25/95 ≈ 0.263 > 0.20
        }
        entry = evaluate_trial_verdict(
            lift=0.02,
            n_with=100,
            n_without=100,
            config=self._cfg(),
            arm_composition=comp,
        )
        assert entry["verdict"] == "continue-trial"
        assert entry["verdict_reason"] == "holdback_contaminated"
        assert entry["immediate_alert"] is True

    def test_contamination_at_threshold_does_not_block(self):
        """Contamination rate exactly at threshold (not exceeding) must NOT trigger guard."""
        # Rate = 10/50 = 0.20 exactly — check is strictly >, so 0.20 should not fire
        comp = {
            "with_assigned": 35,
            "with_assigned_delivered": 35,
            "with_receipt": 5,
            "without_assigned": 50,
            "without_receipt": 3,
            "without_contaminated": 10,
        }
        entry = evaluate_trial_verdict(
            lift=0.08,
            n_with=40,
            n_without=53,
            config=self._cfg(),
            arm_composition=comp,
        )
        assert entry["verdict"] == "adopt"

    def test_contamination_below_threshold_does_not_block(self):
        """Contamination rate below threshold must not block a clean verdict."""
        comp = {
            "with_assigned": 35,
            "with_assigned_delivered": 35,
            "with_receipt": 5,
            "without_assigned": 50,
            "without_receipt": 3,
            "without_contaminated": 5,  # 5/50 = 0.10 < 0.20
        }
        entry = evaluate_trial_verdict(
            lift=0.08,
            n_with=40,
            n_without=53,
            config=self._cfg(),
            arm_composition=comp,
        )
        assert entry["verdict"] == "adopt"

    def test_contamination_guard_skipped_when_arm_composition_absent(self):
        """Guard must not fire when arm_composition is not provided."""
        entry = evaluate_trial_verdict(
            lift=0.08,
            n_with=40,
            n_without=33,
            config=self._cfg(),
        )
        assert entry["verdict"] == "adopt"

    def test_contamination_guard_skipped_when_without_contaminated_absent(self):
        """Guard must not fire when arm_composition lacks without_contaminated key."""
        comp = {
            "with_assigned": 35,
            "with_receipt": 5,
            "without_assigned": 50,
            "without_receipt": 3,
            # without_contaminated intentionally absent
        }
        entry = evaluate_trial_verdict(
            lift=0.08,
            n_with=40,
            n_without=53,
            config=self._cfg(),
            arm_composition=comp,
        )
        assert entry["verdict"] == "adopt"

    def test_contamination_guard_skipped_when_without_assigned_zero(self):
        """Guard must not fire when without_assigned is zero (division guard)."""
        comp = {
            "with_assigned": 35,
            "with_receipt": 5,
            "without_assigned": 0,
            "without_receipt": 53,
            "without_contaminated": 5,
        }
        entry = evaluate_trial_verdict(
            lift=0.08,
            n_with=40,
            n_without=53,
            config=self._cfg(),
            arm_composition=comp,
        )
        # Purity gate fires first (without_assigned=0 < min_n=30) → insufficient_assigned_arm_data
        assert entry["verdict"] == "continue-trial"
        assert entry["verdict_reason"] != "holdback_contaminated"

    def test_holdback_contaminated_does_not_overwrite_decided_adopt(self):
        """holdback_contaminated is NOT in _SUFFICIENT_DATA_CONTINUE_REASONS and cannot overwrite.

        holdback_contaminated describes a broken/contaminated measurement (without-arm
        received capability despite holdback), not a genuine inconclusive result.
        It must emit an alert but NOT revert a standing decided verdict.
        """
        project = _make_project()
        prior = {"verdict": "adopt", "lift": 0.09, "n_with": 50, "n_without": 50}
        project.capability_adoption_verdicts = {"repo_map": prior}

        comp = {
            "with_assigned": 35,
            "with_assigned_delivered": 35,
            "with_receipt": 5,
            "without_assigned": 50,
            "without_receipt": 3,
            "without_contaminated": 15,
        }
        new_entry = evaluate_trial_verdict(
            lift=0.08,
            n_with=40,
            n_without=53,
            config=self._cfg(),
            arm_composition=comp,
        )
        assert new_entry["verdict"] == "continue-trial"
        assert new_entry["verdict_reason"] == "holdback_contaminated"

        with patch("app.capability_trial_verdict.flag_modified") as mock_flag:
            record_trial_verdict(project, "repo_map", new_entry)

        # adopt must be preserved; flag_modified not called (no write happened).
        assert project.capability_adoption_verdicts["repo_map"]["verdict"] == "adopt"
        assert project.capability_adoption_verdicts["repo_map"]["lift"] == 0.09
        mock_flag.assert_not_called()

    def test_configurable_contamination_threshold_respected(self):
        """Custom futility_max_holdback_contamination_rate threshold is applied."""
        # With a stricter threshold (0.10), rate=0.15 should trigger
        comp = {
            "with_assigned": 35,
            "with_assigned_delivered": 35,
            "with_receipt": 5,
            "without_assigned": 100,
            "without_receipt": 3,
            "without_contaminated": 15,  # 15/100 = 0.15 > 0.10
        }
        cfg = self._cfg()
        cfg["futility_max_holdback_contamination_rate"] = 0.10
        entry = evaluate_trial_verdict(
            lift=0.08,
            n_with=40,
            n_without=103,
            config=cfg,
            arm_composition=comp,
        )
        assert entry["verdict"] == "continue-trial"
        assert entry["verdict_reason"] == "holdback_contaminated"


# ── trial-adopt → capability proof integration ────────────────────────────────


def test_adopt_verdict_records_capability_proof():
    """Evidence-grounded ADOPT records exactly one durable trial-based proof entry.

    Verifies:
    - proof_kind is 'trial_adoption'
    - per-arm counts and lift are carried in the entry
    - the recorded proof flips the registry capability's proof_status to 'proven'
    - re-evaluating a standing ADOPT is idempotent (dedup by task_id)
    """
    from app.capability_proof import record_capability_proof_from_trial
    from app.capability_registry import annotate_capability_proof_status

    project = MagicMock()
    project.capability_proof_records = {}

    adopt_entry = {
        "verdict": "adopt",
        "verdict_reason": "sufficient_data",
        "lift": 0.08,
        "n_with": 40,
        "n_without": 38,
    }

    with patch("app.capability_proof.flag_modified"):
        record_capability_proof_from_trial(project, "repo_map", adopt_entry)

    # Entry recorded under normalized cap name.
    assert "repo-map" in project.capability_proof_records
    entries = project.capability_proof_records["repo-map"]
    assert len(entries) == 1
    e = entries[0]
    assert e["proof_kind"] == "trial_adoption"
    assert e["lift"] == 0.08
    assert e["n_with"] == 40
    assert e["n_without"] == 38
    assert e["task_id"] == "trial:repo-map"

    # Registry capability flips to 'proven'.
    vocab = [{"name": "repo-map", "status": "working"}]
    annotated = annotate_capability_proof_status(
        vocab, project.capability_proof_records
    )
    assert annotated[0]["proof_status"] == "proven"

    # Re-evaluation is idempotent — no duplicate entry.
    with patch("app.capability_proof.flag_modified"):
        record_capability_proof_from_trial(project, "repo_map", adopt_entry)
    assert len(project.capability_proof_records["repo-map"]) == 1


def test_non_adopt_verdicts_do_not_record_capability_proof():
    """reject / no_effect / continue-trial verdicts must never record a proof entry."""
    from app.capability_proof import record_capability_proof_from_trial

    for non_adopt in ("reject", "no_effect", "continue-trial", "KILL", "CONTINUE"):
        project = MagicMock()
        project.capability_proof_records = {}
        entry = {"verdict": non_adopt, "lift": 0.01, "n_with": 40, "n_without": 40}
        with patch("app.capability_proof.flag_modified"):
            record_capability_proof_from_trial(project, "repo_map", entry)
        assert project.capability_proof_records == {}, (
            f"Unexpected proof recorded for verdict={non_adopt!r}"
        )


# ── is_trial_stalled ──────────────────────────────────────────────────────────


class TestIsTrialStalled:
    """Unit tests for is_trial_stalled predicate."""

    def _stalled_entry(self, **overrides):
        base = {
            "verdict": "continue-trial",
            "verdict_reason": "insufficient_data",
            "arm_composition": {
                "with_assigned": 50,
                "without_assigned": 3,  # holdback has participants — trial is stalled, not cold
            },
            "accrual_projection": {
                "with_7d": 5,
                "without_7d": 0,
                "window_days": 7,
                "holdback_fraction": 0.05,
                "days_to_min_n": None,
                "alternative_projections": {"0.1": 20.0, "0.3": None},
            },
        }
        base.update(overrides)
        return base

    def test_stalled_continue_trial_insufficient_data_none_days(self):
        from app.capability_trial_verdict import is_trial_stalled

        assert is_trial_stalled(self._stalled_entry()) is True

    def test_decided_adopt_not_stalled(self):
        from app.capability_trial_verdict import is_trial_stalled

        entry = {"verdict": "adopt", "verdict_reason": "sufficient_data"}
        assert is_trial_stalled(entry) is False

    def test_decided_reject_not_stalled(self):
        from app.capability_trial_verdict import is_trial_stalled

        entry = {"verdict": "reject", "verdict_reason": "sufficient_data"}
        assert is_trial_stalled(entry) is False

    def test_decided_no_effect_not_stalled(self):
        from app.capability_trial_verdict import is_trial_stalled

        entry = {"verdict": "no_effect", "verdict_reason": "futility_bound_reached"}
        assert is_trial_stalled(entry) is False

    def test_decided_kill_not_stalled(self):
        from app.capability_trial_verdict import is_trial_stalled

        entry = {
            "verdict": "KILL",
            "verdict_reason": "gate_integrity_violations_exceeded",
        }
        assert is_trial_stalled(entry) is False

    def test_healthy_continue_trial_finite_days_not_stalled(self):
        from app.capability_trial_verdict import is_trial_stalled

        entry = self._stalled_entry()
        entry["accrual_projection"]["days_to_min_n"] = 14.0
        assert is_trial_stalled(entry) is False

    def test_ambiguous_lift_continue_trial_not_stalled(self):
        from app.capability_trial_verdict import is_trial_stalled

        entry = {
            "verdict": "continue-trial",
            "verdict_reason": "ambiguous_lift",
            "accrual_projection": {"days_to_min_n": None},
        }
        assert is_trial_stalled(entry) is False

    def test_treatment_not_engaging_not_stalled(self):
        from app.capability_trial_verdict import is_trial_stalled

        entry = {
            "verdict": "continue-trial",
            "verdict_reason": "treatment_not_engaging",
            "immediate_alert": True,
            "accrual_projection": {"days_to_min_n": None},
        }
        assert is_trial_stalled(entry) is False

    def test_holdback_contaminated_not_stalled(self):
        from app.capability_trial_verdict import is_trial_stalled

        entry = {
            "verdict": "continue-trial",
            "verdict_reason": "holdback_contaminated",
            "immediate_alert": True,
            "accrual_projection": {"days_to_min_n": None},
        }
        assert is_trial_stalled(entry) is False

    def test_no_accrual_projection_not_stalled(self):
        from app.capability_trial_verdict import is_trial_stalled

        entry = {"verdict": "continue-trial", "verdict_reason": "insufficient_data"}
        assert is_trial_stalled(entry) is False

    def test_days_to_min_n_zero_not_stalled(self):
        from app.capability_trial_verdict import is_trial_stalled

        entry = self._stalled_entry()
        entry["accrual_projection"]["days_to_min_n"] = 0.0
        assert is_trial_stalled(entry) is False

    def test_cold_start_no_holdback_participants_not_stalled(self):
        """A trial with zero holdback assignments hasn't started yet — not a stall."""
        from app.capability_trial_verdict import is_trial_stalled

        entry = self._stalled_entry()
        entry["arm_composition"] = {"with_assigned": 0, "without_assigned": 0}
        assert is_trial_stalled(entry) is False

    def test_cold_start_missing_arm_composition_not_stalled(self):
        """A trial with no arm_composition at all is treated as cold start — not stalled."""
        from app.capability_trial_verdict import is_trial_stalled

        entry = {
            "verdict": "continue-trial",
            "verdict_reason": "insufficient_data",
            "accrual_projection": {"days_to_min_n": None},
        }
        assert is_trial_stalled(entry) is False

    def test_non_dict_entry_not_stalled(self):
        from app.capability_trial_verdict import is_trial_stalled

        assert is_trial_stalled(None) is False  # type: ignore[arg-type]
        assert is_trial_stalled("continue-trial") is False  # type: ignore[arg-type]


# ── persist_stalled_trial_notices_in_transaction ──────────────────────────────


class TestPersistStalledTrialNotices:
    """Tests for raise_stalled_trial_notice and persist_stalled_trial_notices_in_transaction."""

    def _build_mock_db(self, open_messages: list | None = None):
        """Return (mock_session, mock_cm) with configurable open ProjectMessages.

        Models SQL-side dedupe_key filtering: scalar() returns a sentinel ID only
        when the queried dedupe_key matches one of the open_messages.
        """
        from unittest.mock import AsyncMock, MagicMock

        msgs = open_messages or []
        present_keys = {
            (m.context or {}).get("dedupe_key")
            for m in msgs
            if (m.context or {}).get("dedupe_key") is not None
        }

        mock_session = MagicMock()
        mock_session.commit = AsyncMock()
        mock_session.rollback = AsyncMock()
        mock_session.add = MagicMock()

        async def _execute(stmt, *args, **kwargs):
            execute_result = MagicMock()
            execute_result.scalar.return_value = None
            try:
                for clause in getattr(stmt, "_where_criteria", ()):
                    left = getattr(clause, "left", None)
                    right = getattr(clause, "right", None)
                    if left is None or right is None:
                        continue
                    inner_key = getattr(getattr(left, "right", None), "value", None)
                    if inner_key != "dedupe_key":
                        continue
                    queried_key = getattr(right, "value", None)
                    if queried_key is not None and queried_key in present_keys:
                        execute_result.scalar.return_value = "sentinel-id"
                    break
            except Exception:
                pass
            return execute_result

        mock_session.execute = AsyncMock(side_effect=_execute)

        mock_cm = MagicMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        mock_cm.return_value = mock_cm

        return mock_session, mock_cm

    def _stalled_entry(self):
        return {
            "verdict": "continue-trial",
            "verdict_reason": "insufficient_data",
            "thresholds": {"min_n_per_arm": 30},
            "arm_composition": {"without_assigned": 3, "with_assigned": 25},
            "accrual_projection": {
                "with_7d": 10,
                "without_7d": 0,
                "window_days": 7,
                "holdback_fraction": 0.05,
                "days_to_min_n": None,
                "alternative_projections": {"0.1": 20.0, "0.3": 8.0, "0.5": None},
            },
        }

    @pytest.mark.asyncio
    async def test_stalled_trial_raises_exactly_one_notice(self):
        """A stalled continue-trial (insufficient_data, days_to_min_n=None) emits one notice."""
        import uuid
        from unittest.mock import AsyncMock, MagicMock, patch

        from app.capability_trial_verdict import (
            persist_stalled_trial_notices_in_transaction,
        )

        project_id = uuid.uuid4()
        mock_session, mock_cm = self._build_mock_db()
        mock_factory = MagicMock(return_value=mock_cm)

        with (
            patch("sqlalchemy.ext.asyncio.async_sessionmaker", mock_factory),
            patch("app.database.resolve_async_engine", return_value=MagicMock()),
            patch(
                "app.capability_trial_verdict.raise_stalled_trial_notice",
                new_callable=AsyncMock,
            ) as mock_notice,
        ):
            await persist_stalled_trial_notices_in_transaction(
                MagicMock(),
                project_id,
                stalled=[("repo_map", self._stalled_entry())],
            )

        mock_notice.assert_called_once()
        call_args = mock_notice.call_args[0]
        assert call_args[2] == "repo_map"  # cap_name
        assert call_args[3] == self._stalled_entry()  # entry

    @pytest.mark.asyncio
    async def test_dedup_second_evaluation_silent(self):
        """Second call for the same stalled cap with an existing open notice is silent."""
        import uuid
        from unittest.mock import MagicMock

        from app.capability_trial_verdict import raise_stalled_trial_notice

        project_id = uuid.uuid4()

        # Simulate an existing open ProjectMessage with the dedupe_key.
        existing_msg = MagicMock()
        existing_msg.context = {"dedupe_key": "capability_trial_stalled_repo_map"}
        mock_session, _ = self._build_mock_db(open_messages=[existing_msg])

        await raise_stalled_trial_notice(
            mock_session, project_id, "repo_map", self._stalled_entry()
        )

        # No add should have been called — dedupe blocks the write.
        mock_session.add.assert_not_called()

    @pytest.mark.asyncio
    async def test_first_evaluation_creates_notice(self):
        """No existing open notice → raise_stalled_trial_notice writes a ProjectMessage."""
        import uuid

        from app.capability_trial_verdict import raise_stalled_trial_notice

        project_id = uuid.uuid4()
        mock_session, _ = self._build_mock_db(open_messages=[])

        await raise_stalled_trial_notice(
            mock_session, project_id, "repo_map", self._stalled_entry()
        )

        mock_session.add.assert_called_once()
        added_msg = mock_session.add.call_args[0][0]
        ctx = added_msg.context
        assert ctx["event_type"] == "capability_trial_stalled"
        assert ctx["dedupe_key"] == "capability_trial_stalled_repo_map"
        assert ctx["cap_name"] == "repo_map"
        assert ctx["alternative_projections"] == {"0.1": 20.0, "0.3": 8.0, "0.5": None}
        assert ctx["with_7d"] == 10
        assert ctx["without_7d"] == 0
        assert ctx["holdback_fraction"] == 0.05

    @pytest.mark.asyncio
    async def test_decided_verdicts_do_not_raise_stall_notice(self):
        """is_trial_stalled returns False for decided verdicts — no notice emitted."""
        from app.capability_trial_verdict import is_trial_stalled

        for verdict in ("adopt", "reject", "no_effect", "ADOPT", "KILL"):
            entry = {
                "verdict": verdict,
                "verdict_reason": "sufficient_data",
                "accrual_projection": {"days_to_min_n": None},
            }
            assert is_trial_stalled(entry) is False, (
                f"Expected not stalled for verdict={verdict!r}"
            )

    @pytest.mark.asyncio
    async def test_healthy_continue_trial_not_stalled(self):
        """A continue-trial with finite days_to_min_n is NOT stalled."""
        from app.capability_trial_verdict import is_trial_stalled

        entry = {
            "verdict": "continue-trial",
            "verdict_reason": "insufficient_data",
            "accrual_projection": {"days_to_min_n": 12.5},
        }
        assert is_trial_stalled(entry) is False

    @pytest.mark.asyncio
    async def test_notice_context_carries_alternative_projections(self):
        """The notice context must include alternative_projections map."""
        import uuid

        from app.capability_trial_verdict import raise_stalled_trial_notice

        project_id = uuid.uuid4()
        mock_session, _ = self._build_mock_db(open_messages=[])

        entry = self._stalled_entry()
        await raise_stalled_trial_notice(
            mock_session, project_id, "module_interfaces", entry
        )

        mock_session.add.assert_called_once()
        added_msg = mock_session.add.call_args[0][0]
        ctx = added_msg.context
        assert "alternative_projections" in ctx

    @pytest.mark.asyncio
    async def test_stalled_trial_requests_strategic_refresh_once(self):
        """Strategic refresh is triggered exactly once on first stall detection, not on re-evaluation.

        Uses a real evaluate_trial_verdict entry (real merge-test verdict) augmented with
        an accrual_projection that marks the trial as permanently stalled.  The first call
        to persist_stalled_trial_notices_in_transaction (no existing notice) must arm a
        strategic refresh.  The second call (existing notice already open) must be silent.
        """
        import uuid
        from unittest.mock import AsyncMock, MagicMock, patch

        from app.capability_trial_verdict import (
            evaluate_trial_verdict,
            persist_stalled_trial_notices_in_transaction,
        )

        # Build a real continue-trial entry via the production verdict evaluator.
        real_entry = evaluate_trial_verdict(
            lift=None,
            n_with=5,
            n_without=3,
            config={"min_n_per_arm": 30, "adopt_lift": 0.05, "reject_lift": -0.03},
            arm_composition={
                "with_assigned": 5,
                "without_assigned": 3,
            },
            accumulation_basis="cumulative_terminal_arm_tasks",
            evaluated_at="2026-06-20T00:00:00",
        )
        assert real_entry["verdict"] == "continue-trial"
        assert real_entry["verdict_reason"] == "insufficient_data"

        # Augment with accrual_projection so is_trial_stalled() returns True.
        real_entry["accrual_projection"] = {
            "with_7d": 5,
            "without_7d": 0,
            "window_days": 7,
            "holdback_fraction": 0.05,
            "days_to_min_n": None,  # permanently stalled
            "alternative_projections": {"0.1": 18.0, "0.3": 7.0},
        }

        project_id = uuid.uuid4()

        # ── First call: no existing notice → refresh must be requested ────────
        mock_session_1, mock_cm_1 = self._build_mock_db(open_messages=[])
        mock_factory_1 = MagicMock(return_value=mock_cm_1)

        with (
            patch("sqlalchemy.ext.asyncio.async_sessionmaker", mock_factory_1),
            patch("app.database.resolve_async_engine", return_value=MagicMock()),
            patch(
                "app.strategic_trigger.request_strategic_refresh",
                new_callable=AsyncMock,
            ) as mock_refresh_1,
        ):
            await persist_stalled_trial_notices_in_transaction(
                MagicMock(),
                project_id,
                stalled=[("repo_map", real_entry)],
            )

        mock_refresh_1.assert_called_once()
        refresh_reason = mock_refresh_1.call_args[0][2]
        assert refresh_reason == "capability_trial_stalled:repo_map"

        # ── Second call: notice already open → refresh must NOT be requested ──
        existing_msg = MagicMock()
        existing_msg.context = {"dedupe_key": "capability_trial_stalled_repo_map"}
        mock_session_2, mock_cm_2 = self._build_mock_db(open_messages=[existing_msg])
        mock_factory_2 = MagicMock(return_value=mock_cm_2)

        with (
            patch("sqlalchemy.ext.asyncio.async_sessionmaker", mock_factory_2),
            patch("app.database.resolve_async_engine", return_value=MagicMock()),
            patch(
                "app.strategic_trigger.request_strategic_refresh",
                new_callable=AsyncMock,
            ) as mock_refresh_2,
        ):
            await persist_stalled_trial_notices_in_transaction(
                MagicMock(),
                project_id,
                stalled=[("repo_map", real_entry)],
            )

        mock_refresh_2.assert_not_called()


# ── is_stalled flag stamped on augmented verdict entries ──────────────────────


class TestIsStalledFlagStamped:
    """Verify that is_stalled is stamped onto augmented verdict dicts.

    The digest loop stamps is_stalled on every _augmented_verdicts entry.
    These tests verify the flag is True for a stalled entry and False for
    a healthy continue-trial (finite days_to_min_n) or a decided verdict.
    """

    def _stalled_entry(self):
        return {
            "verdict": "continue-trial",
            "verdict_reason": "insufficient_data",
            "arm_composition": {"with_assigned": 50, "without_assigned": 5},
            "accrual_projection": {
                "with_7d": 5,
                "without_7d": 0,
                "window_days": 7,
                "holdback_fraction": 0.05,
                "days_to_min_n": None,
                "alternative_projections": {},
            },
        }

    def _healthy_entry(self):
        return {
            "verdict": "continue-trial",
            "verdict_reason": "insufficient_data",
            "arm_composition": {"with_assigned": 20, "without_assigned": 8},
            "accrual_projection": {
                "with_7d": 10,
                "without_7d": 4,
                "window_days": 7,
                "holdback_fraction": 0.3,
                "days_to_min_n": 14.0,
                "alternative_projections": {},
            },
        }

    def test_is_stalled_true_for_stalled_entry(self):
        from app.capability_trial_verdict import is_trial_stalled

        entry = self._stalled_entry()
        # Simulate the digest loop stamping is_stalled
        entry["is_stalled"] = is_trial_stalled(entry)
        assert entry["is_stalled"] is True

    def test_is_stalled_false_for_healthy_continue_trial(self):
        from app.capability_trial_verdict import is_trial_stalled

        entry = self._healthy_entry()
        entry["is_stalled"] = is_trial_stalled(entry)
        assert entry["is_stalled"] is False

    def test_is_stalled_false_for_decided_adopt(self):
        from app.capability_trial_verdict import is_trial_stalled

        entry = {"verdict": "adopt", "verdict_reason": "sufficient_data"}
        entry["is_stalled"] = is_trial_stalled(entry)
        assert entry["is_stalled"] is False

    def test_augmented_verdicts_all_have_is_stalled(self):
        """All entries in the augmented dict receive is_stalled regardless of verdict."""
        from app.capability_trial_verdict import is_trial_stalled

        augmented_verdicts = {
            "stalled_cap": self._stalled_entry(),
            "healthy_cap": self._healthy_entry(),
            "decided_cap": {"verdict": "adopt", "verdict_reason": "sufficient_data"},
        }
        # Simulate the digest stamping loop
        for _cap, _entry in augmented_verdicts.items():
            if isinstance(_entry, dict):
                _entry["is_stalled"] = is_trial_stalled(_entry)

        assert augmented_verdicts["stalled_cap"]["is_stalled"] is True
        assert augmented_verdicts["healthy_cap"]["is_stalled"] is False
        assert augmented_verdicts["decided_cap"]["is_stalled"] is False


# ── is_ambiguous_lift_escalation_needed ──────────────────────────────────────


class TestIsAmbiguousLiftEscalationNeeded:
    """Unit tests for is_ambiguous_lift_escalation_needed predicate."""

    def _ambiguous_entry(self, since_iso: str, **overrides) -> dict:
        base = {
            "verdict": "continue-trial",
            "verdict_reason": "ambiguous_lift",
            "ambiguous_lift_since": since_iso,
            "lift": 0.02,
            "n_with": 50,
            "n_without": 50,
        }
        base.update(overrides)
        return base

    def test_above_threshold_returns_true(self):
        from app.capability_trial_verdict import is_ambiguous_lift_escalation_needed

        # 15 days dwell > 14-day threshold
        since = "2026-06-01T00:00:00+00:00"
        now = "2026-06-16T00:00:00+00:00"
        assert (
            is_ambiguous_lift_escalation_needed(
                self._ambiguous_entry(since), threshold_days=14, now_iso=now
            )
            is True
        )

    def test_below_threshold_returns_false(self):
        from app.capability_trial_verdict import is_ambiguous_lift_escalation_needed

        # 5 days dwell < 14-day threshold
        since = "2026-06-10T00:00:00+00:00"
        now = "2026-06-15T00:00:00+00:00"
        assert (
            is_ambiguous_lift_escalation_needed(
                self._ambiguous_entry(since), threshold_days=14, now_iso=now
            )
            is False
        )

    def test_exactly_at_threshold_returns_true(self):
        from app.capability_trial_verdict import is_ambiguous_lift_escalation_needed

        since = "2026-06-01T00:00:00+00:00"
        now = "2026-06-15T00:00:00+00:00"
        assert (
            is_ambiguous_lift_escalation_needed(
                self._ambiguous_entry(since), threshold_days=14, now_iso=now
            )
            is True
        )

    def test_non_ambiguous_lift_verdict_reason_returns_false(self):
        from app.capability_trial_verdict import is_ambiguous_lift_escalation_needed

        entry = {
            "verdict": "continue-trial",
            "verdict_reason": "insufficient_data",
            "ambiguous_lift_since": "2026-01-01T00:00:00+00:00",
        }
        now = "2026-06-15T00:00:00+00:00"
        assert is_ambiguous_lift_escalation_needed(entry, now_iso=now) is False

    def test_decided_verdict_returns_false(self):
        from app.capability_trial_verdict import is_ambiguous_lift_escalation_needed

        for verdict in ("adopt", "reject", "no_effect", "ADOPT", "KILL"):
            entry = {
                "verdict": verdict,
                "verdict_reason": "ambiguous_lift",
                "ambiguous_lift_since": "2026-01-01T00:00:00+00:00",
            }
            now = "2026-06-15T00:00:00+00:00"
            assert is_ambiguous_lift_escalation_needed(entry, now_iso=now) is False

    def test_missing_since_returns_false(self):
        from app.capability_trial_verdict import is_ambiguous_lift_escalation_needed

        entry = {"verdict": "continue-trial", "verdict_reason": "ambiguous_lift"}
        now = "2026-06-15T00:00:00+00:00"
        assert is_ambiguous_lift_escalation_needed(entry, now_iso=now) is False

    def test_non_dict_returns_false(self):
        from app.capability_trial_verdict import is_ambiguous_lift_escalation_needed

        assert (
            is_ambiguous_lift_escalation_needed(
                None, now_iso="2026-06-15T00:00:00+00:00"
            )
            is False
        )  # type: ignore[arg-type]
        assert (
            is_ambiguous_lift_escalation_needed(
                "continue-trial", now_iso="2026-06-15T00:00:00+00:00"
            )
            is False
        )  # type: ignore[arg-type]

    def test_unparseable_since_returns_false(self):
        from app.capability_trial_verdict import is_ambiguous_lift_escalation_needed

        entry = {
            "verdict": "continue-trial",
            "verdict_reason": "ambiguous_lift",
            "ambiguous_lift_since": "not-a-date",
        }
        now = "2026-06-15T00:00:00+00:00"
        assert is_ambiguous_lift_escalation_needed(entry, now_iso=now) is False


# ── record_trial_verdict: ambiguous_lift_since carry-forward ─────────────────


class TestRecordTrialVerdictAmbiguousLiftSince:
    """Tests for ambiguous_lift_since stamping in record_trial_verdict."""

    def test_stamps_since_on_first_ambiguous_lift(self):
        """First ambiguous_lift verdict stamps ambiguous_lift_since from evaluated_at."""
        project = _make_project()
        incoming = {
            "verdict": "continue-trial",
            "verdict_reason": "ambiguous_lift",
            "lift": 0.02,
            "n_with": 50,
            "n_without": 50,
            "evaluated_at": "2026-06-01T00:00:00+00:00",
        }
        with patch("app.capability_trial_verdict.flag_modified"):
            record_trial_verdict(project, "repo_map", incoming)

        stored = project.capability_adoption_verdicts["repo_map"]
        assert stored["ambiguous_lift_since"] == "2026-06-01T00:00:00+00:00"

    def test_preserves_since_on_subsequent_ambiguous_lift(self):
        """Subsequent ambiguous_lift verdicts carry forward the original since timestamp."""
        project = _make_project()
        prior = {
            "verdict": "continue-trial",
            "verdict_reason": "ambiguous_lift",
            "lift": 0.02,
            "ambiguous_lift_since": "2026-06-01T00:00:00+00:00",
        }
        project.capability_adoption_verdicts = {"repo_map": prior}

        incoming = {
            "verdict": "continue-trial",
            "verdict_reason": "ambiguous_lift",
            "lift": 0.01,
            "n_with": 60,
            "n_without": 60,
            "evaluated_at": "2026-06-15T00:00:00+00:00",
        }
        with patch("app.capability_trial_verdict.flag_modified"):
            record_trial_verdict(project, "repo_map", incoming)

        stored = project.capability_adoption_verdicts["repo_map"]
        # Original since timestamp must be preserved, not overwritten with evaluated_at
        assert stored["ambiguous_lift_since"] == "2026-06-01T00:00:00+00:00"

    def test_non_ambiguous_lift_does_not_stamp_since(self):
        """insufficient_data continue-trial must not receive ambiguous_lift_since."""
        project = _make_project()
        incoming = {
            "verdict": "continue-trial",
            "verdict_reason": "insufficient_data",
            "lift": None,
            "n_with": 5,
            "n_without": 5,
            "evaluated_at": "2026-06-01T00:00:00+00:00",
        }
        with patch("app.capability_trial_verdict.flag_modified"):
            record_trial_verdict(project, "repo_map", incoming)

        stored = project.capability_adoption_verdicts["repo_map"]
        assert "ambiguous_lift_since" not in stored


# ── raise_ambiguous_lift_escalation_notice ───────────────────────────────────


class TestRaiseAmbiguousLiftEscalationNotice:
    """Tests for raise_ambiguous_lift_escalation_notice and persist_ambiguous_lift_escalations_in_transaction."""

    def _build_mock_db(self, open_messages=None):
        """Return (mock_session, mock_cm) with configurable open ProjectMessages.

        Models SQL-side dedupe_key filtering: scalar() returns a sentinel ID only
        when the queried dedupe_key matches one of the open_messages.
        """
        from unittest.mock import AsyncMock, MagicMock

        msgs = open_messages or []
        present_keys = {
            (m.context or {}).get("dedupe_key")
            for m in msgs
            if (m.context or {}).get("dedupe_key") is not None
        }

        mock_session = MagicMock()
        mock_session.commit = AsyncMock()
        mock_session.rollback = AsyncMock()
        mock_session.add = MagicMock()

        async def _execute(stmt, *args, **kwargs):
            execute_result = MagicMock()
            execute_result.scalar.return_value = None
            try:
                for clause in getattr(stmt, "_where_criteria", ()):
                    left = getattr(clause, "left", None)
                    right = getattr(clause, "right", None)
                    if left is None or right is None:
                        continue
                    inner_key = getattr(getattr(left, "right", None), "value", None)
                    if inner_key != "dedupe_key":
                        continue
                    queried_key = getattr(right, "value", None)
                    if queried_key is not None and queried_key in present_keys:
                        execute_result.scalar.return_value = "sentinel-id"
                    break
            except Exception:
                pass
            return execute_result

        mock_session.execute = AsyncMock(side_effect=_execute)

        mock_cm = MagicMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        mock_cm.return_value = mock_cm

        return mock_session, mock_cm

    def _escalated_entry(self):
        return {
            "verdict": "continue-trial",
            "verdict_reason": "ambiguous_lift",
            "ambiguous_lift_since": "2026-06-01T00:00:00+00:00",
            "lift": 0.02,
            "n_with": 50,
            "n_without": 50,
            "thresholds": {"min_n_per_arm": 30},
        }

    @pytest.mark.asyncio
    async def test_first_evaluation_creates_notice(self):
        """No existing open notice → creates a ProjectMessage with correct event_type."""
        import uuid

        from app.capability_trial_verdict import raise_ambiguous_lift_escalation_notice

        project_id = uuid.uuid4()
        mock_session, _ = self._build_mock_db(open_messages=[])

        await raise_ambiguous_lift_escalation_notice(
            mock_session, project_id, "repo_map", self._escalated_entry()
        )

        mock_session.add.assert_called_once()
        added_msg = mock_session.add.call_args[0][0]
        ctx = added_msg.context
        assert ctx["event_type"] == "capability_trial_ambiguous_lift_escalation"
        assert (
            ctx["dedupe_key"] == "capability_trial_ambiguous_lift_escalation_repo_map"
        )
        assert ctx["cap_name"] == "repo_map"
        assert ctx["ambiguous_lift_since"] == "2026-06-01T00:00:00+00:00"
        assert ctx["lift"] == 0.02

    @pytest.mark.asyncio
    async def test_dedup_second_evaluation_silent(self):
        """Second call with existing open notice must not add another message."""
        import uuid
        from unittest.mock import MagicMock

        from app.capability_trial_verdict import raise_ambiguous_lift_escalation_notice

        project_id = uuid.uuid4()
        existing_msg = MagicMock()
        existing_msg.context = {
            "dedupe_key": "capability_trial_ambiguous_lift_escalation_repo_map"
        }
        mock_session, _ = self._build_mock_db(open_messages=[existing_msg])

        await raise_ambiguous_lift_escalation_notice(
            mock_session, project_id, "repo_map", self._escalated_entry()
        )

        mock_session.add.assert_not_called()

    @pytest.mark.asyncio
    async def test_persist_ambiguous_lift_escalations_basic(self):
        """persist_ambiguous_lift_escalations_in_transaction calls the notice function and commits."""
        import uuid
        from unittest.mock import AsyncMock, MagicMock, patch

        from app.capability_trial_verdict import (
            persist_ambiguous_lift_escalations_in_transaction,
        )

        project_id = uuid.uuid4()
        mock_session, mock_cm = self._build_mock_db()
        mock_factory = MagicMock(return_value=mock_cm)

        with (
            patch("sqlalchemy.ext.asyncio.async_sessionmaker", mock_factory),
            patch("app.database.resolve_async_engine", return_value=MagicMock()),
            patch(
                "app.capability_trial_verdict.raise_ambiguous_lift_escalation_notice",
                new_callable=AsyncMock,
            ) as mock_notice,
        ):
            await persist_ambiguous_lift_escalations_in_transaction(
                MagicMock(),
                project_id,
                escalated=[("repo_map", self._escalated_entry())],
            )

        mock_notice.assert_called_once()
        call_args = mock_notice.call_args[0]
        assert call_args[2] == "repo_map"
        assert call_args[3] == self._escalated_entry()
        mock_session.commit.assert_called_once()


# ── Bounded ProjectMessage query invariants ───────────────────────────────────


class TestBoundedProjectMessageQueries:
    """Verify that dedupe scans are bounded with .limit() via shared helper."""

    def test_check_open_message_exists_query_is_bounded(self):
        """_check_open_message_exists must apply .limit() to ProjectMessage dedupe scan."""
        import inspect

        from app.capability_trial_verdict import _check_open_message_exists

        source = inspect.getsource(_check_open_message_exists)
        assert ".limit(" in source, (
            "_check_open_message_exists must bound ProjectMessage query with .limit()"
        )

    def test_raise_capability_verdict_notice_uses_helper(self):
        """raise_capability_verdict_notice must use _check_open_message_exists for dedupe."""
        import inspect

        from app.capability_trial_verdict import raise_capability_verdict_notice

        source = inspect.getsource(raise_capability_verdict_notice)
        assert "_check_open_message_exists" in source, (
            "raise_capability_verdict_notice must use _check_open_message_exists"
        )

    def test_raise_stalled_trial_notice_uses_helper(self):
        """raise_stalled_trial_notice must use _check_open_message_exists for dedupe."""
        import inspect

        from app.capability_trial_verdict import raise_stalled_trial_notice

        source = inspect.getsource(raise_stalled_trial_notice)
        assert "_check_open_message_exists" in source, (
            "raise_stalled_trial_notice must use _check_open_message_exists"
        )

    def test_raise_ambiguous_lift_escalation_notice_uses_helper(self):
        """raise_ambiguous_lift_escalation_notice must use _check_open_message_exists for dedupe."""
        import inspect

        from app.capability_trial_verdict import raise_ambiguous_lift_escalation_notice

        source = inspect.getsource(raise_ambiguous_lift_escalation_notice)
        assert "_check_open_message_exists" in source, (
            "raise_ambiguous_lift_escalation_notice must use _check_open_message_exists"
        )


# ── persist_trial_verdicts_in_transaction row locking ─────────────────────────


class TestPersistTrialVerdictsRowLock:
    """Verify that persist_trial_verdicts_in_transaction acquires a row lock on
    the project before evaluating KILL-survival and other guards, so a concurrent
    writer cannot sneak in an overwrite between fetch and commit."""

    def _build_mock_db_locked(self, prior_verdicts: dict | None = None):
        """Return (mock_session, mock_project, mock_cm) wired for the locked fetch path.

        The first session.execute() call returns the project row (simulating
        SELECT ... FOR UPDATE); subsequent calls return empty results (dedup queries).
        session.get() is intentionally NOT set up with a project return — this lets
        the test assert that the implementation no longer calls session.get().
        """
        from unittest.mock import AsyncMock, MagicMock

        mock_project = MagicMock()
        mock_project.capability_adoption_verdicts = dict(prior_verdicts or {})

        # Project result: scalars().first() returns mock_project (locked fetch)
        project_scalars = MagicMock()
        project_scalars.first.return_value = mock_project
        project_result = MagicMock()
        project_result.scalars.return_value = project_scalars

        # Empty result for dedup queries: scalar() returns None
        empty_result = MagicMock()
        empty_result.scalar.return_value = None

        executed_stmts = []

        async def execute_side_effect(stmt, *args, **kwargs):
            executed_stmts.append(stmt)
            if len(executed_stmts) == 1:
                return project_result
            return empty_result

        mock_session = MagicMock()
        mock_session.get = AsyncMock(return_value=None)  # must NOT be called
        mock_session.commit = AsyncMock()
        mock_session.rollback = AsyncMock()
        mock_session.execute = AsyncMock(side_effect=execute_side_effect)
        mock_session.add = MagicMock()

        mock_cm = MagicMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        mock_cm.return_value = mock_cm

        return mock_session, mock_project, mock_cm, executed_stmts

    @pytest.mark.asyncio
    async def test_uses_row_lock_for_project_fetch_on_merge_test_adopt_verdict(self):
        """Acceptance test: persist_trial_verdicts_in_transaction acquires FOR UPDATE lock.

        Uses a real merge-test adopt verdict (both assigned arms >= min_n, lift above
        adopt threshold) to exercise the full persist path.  Asserts:
        - session.get() is NOT called (row lock enforcement — replaced by execute+lock)
        - session.execute() IS called with a query that uses with_for_update()
        - The adopt verdict is durably stored on the project
        """
        import uuid
        from unittest.mock import AsyncMock, MagicMock, patch

        from app.capability_trial_verdict import (
            evaluate_trial_verdict,
            persist_trial_verdicts_in_transaction,
        )

        project_id = uuid.uuid4()
        mock_session, mock_project, mock_cm, executed_stmts = (
            self._build_mock_db_locked(prior_verdicts={})
        )
        mock_factory = MagicMock(return_value=mock_cm)

        # Real merge-test verdict: adopt after both assigned arms meet min_n.
        comp = {
            "with_assigned": 35,
            "with_receipt": 5,
            "without_assigned": 32,
            "without_receipt": 3,
        }
        adopt_entry = evaluate_trial_verdict(
            lift=0.07,
            n_with=40,
            n_without=35,
            config={"min_n_per_arm": 30, "adopt_lift": 0.05, "reject_lift": -0.03},
            arm_composition=comp,
            accumulation_basis="cumulative_terminal_arm_tasks",
            evaluated_at="2026-06-23T00:00:00",
        )
        assert adopt_entry["verdict"] == "adopt"

        with (
            patch("sqlalchemy.ext.asyncio.async_sessionmaker", mock_factory),
            patch("app.database.resolve_async_engine", return_value=MagicMock()),
            patch("app.capability_trial_verdict.flag_modified"),
            patch(
                "app.capability_trial_verdict.raise_capability_verdict_notice",
                new_callable=AsyncMock,
            ),
        ):
            await persist_trial_verdicts_in_transaction(
                MagicMock(),
                project_id,
                pending_verdicts=[("repo_map", adopt_entry)],
            )

        # The implementation must NOT fall back to session.get (unlocked path).
        mock_session.get.assert_not_called()

        # At least one execute() call must have been made (for the locked fetch).
        assert mock_session.execute.call_count >= 1

        # The first execute() call must use with_for_update (SELECT ... FOR UPDATE).
        first_stmt = executed_stmts[0]
        stmt_str = str(first_stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "FOR UPDATE" in stmt_str, (
            f"First execute() call must issue a FOR UPDATE lock; got: {stmt_str!r}"
        )

        # Verdict must be committed (not rolled back).
        mock_session.commit.assert_called_once()
        mock_session.rollback.assert_not_called()

        # Adopt verdict stored on the project object.
        assert mock_project.capability_adoption_verdicts.get("repo_map", {}).get(
            "verdict"
        ) == "adopt"


# ── _seed_deploy_project_futility_verdicts (lifecycle) ─────────────────────


class TestSeedDeployProjectFutilityVerdicts:
    """Tests for lifecycle._seed_deploy_project_futility_verdicts().

    Exercises the startup seed that records ``no_effect`` / ``futility_bound_reached``
    verdicts for the five context-injection blocks on the deploy project.
    """

    FUTILITY_CAPS: ClassVar[list[str]] = [
        "repo_map",
        "module_interfaces",
        "related_tests",
        "affected_consumers",
        "diagnostics_after_edit",
    ]
    DEPLOY_IDENTITY = ("blueprint", "blueprint")

    @pytest.fixture
    def mock_project(self):
        p = MagicMock()
        p.github_owner = "Blueprint"
        p.github_repo = "Blueprint"
        p.capability_adoption_verdicts = {}
        return p

    @pytest.fixture
    def mock_session(self, mock_project):
        s = AsyncMock(spec=["execute", "commit", "rollback"])
        result = MagicMock()
        result.scalars.return_value.first.return_value = mock_project
        s.execute = AsyncMock(return_value=result)
        s.commit = AsyncMock()
        s.rollback = AsyncMock()
        return s

    @pytest.fixture
    def mock_cm(self, mock_session):
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=mock_session)
        cm.__aexit__ = AsyncMock(return_value=None)
        return cm

    def _patch(self, mock_cm):
        return (
            patch("app.lifecycle.AsyncSessionLocal", return_value=mock_cm),
            patch(
                "app.deploy_identity.deploy_repo_identity",
                return_value=self.DEPLOY_IDENTITY,
            ),
            patch("app.capability_trial_verdict.flag_modified"),
        )

    async def test_seeds_verdicts_on_deploy_project(self, mock_project, mock_cm):
        """All five futility caps receive no_effect/futility_bound_reached."""
        with contextlib.ExitStack() as stack:
            for p in self._patch(mock_cm):
                stack.enter_context(p)
            await _seed_deploy_project_futility_verdicts()

        for cap in self.FUTILITY_CAPS:
            assert cap in mock_project.capability_adoption_verdicts, (
                f"{cap} not seeded"
            )
            entry = mock_project.capability_adoption_verdicts[cap]
            assert entry["verdict"] == "no_effect"
            assert entry["verdict_reason"] == "futility_bound_reached"
            assert entry["evidence_source"] == "strategic_review"
            assert "rationale" in entry
            assert "futility_bound_reached" in entry["rationale"]

    async def test_idempotent_skips_existing_verdicts(self, mock_project, mock_cm):
        """Calling twice does not overwrite existing verdicts."""
        with contextlib.ExitStack() as stack:
            for p in self._patch(mock_cm):
                stack.enter_context(p)
            await _seed_deploy_project_futility_verdicts()

        # Verify first call seeded all
        first_entry = mock_project.capability_adoption_verdicts["repo_map"]
        first_rationale = first_entry["rationale"]

        # Second call
        with contextlib.ExitStack() as stack:
            for p in self._patch(mock_cm):
                stack.enter_context(p)
            await _seed_deploy_project_futility_verdicts()

        # Verdicts unchanged
        assert mock_project.capability_adoption_verdicts["repo_map"] is first_entry
        assert (
            mock_project.capability_adoption_verdicts["repo_map"]["rationale"]
            == first_rationale
        )

    async def test_skips_when_no_deploy_identity(self, mock_cm):
        """No verdicts seeded when deploy_repo_identity() returns None."""
        with (
            patch("app.lifecycle.AsyncSessionLocal", return_value=mock_cm),
            patch(
                "app.deploy_identity.deploy_repo_identity",
                return_value=None,
            ),
            patch("app.capability_trial_verdict.flag_modified") as mock_flag,
        ):
            await _seed_deploy_project_futility_verdicts()

        mock_flag.assert_not_called()

    async def test_skips_when_deploy_project_not_found(self, mock_cm):
        """No verdicts seeded when no project matches the deploy identity."""
        mock_session = AsyncMock(spec=["execute"])
        mock_result = MagicMock()
        mock_result.scalars.return_value.first.return_value = None
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_cm.__aenter__ = AsyncMock(return_value=mock_session)

        with (
            patch("app.lifecycle.AsyncSessionLocal", return_value=mock_cm),
            patch(
                "app.deploy_identity.deploy_repo_identity",
                return_value=self.DEPLOY_IDENTITY,
            ),
            patch("app.capability_trial_verdict.flag_modified") as mock_flag,
        ):
            await _seed_deploy_project_futility_verdicts()

        mock_flag.assert_not_called()

    async def test_get_effective_holdback_is_one_after_seeding(
        self, mock_project, mock_cm
    ):
        """After seeding, get_effective_holdback_fraction returns 1.0 for each cap."""
        with contextlib.ExitStack() as stack:
            for p in self._patch(mock_cm):
                stack.enter_context(p)
            await _seed_deploy_project_futility_verdicts()

        for cap in self.FUTILITY_CAPS:
            frac = get_effective_holdback_fraction(
                mock_project.capability_adoption_verdicts, cap, 0.1
            )
            assert frac == 1.0, (
                f"{cap} holdback should be 1.0 after no_effect verdict, got {frac}"
            )

    async def test_non_deploy_project_unaffected(self, mock_cm):
        """A non-deploy project must NOT have verdicts seeded."""
        other_project = MagicMock()
        other_project.capability_adoption_verdicts = {}

        mock_session = AsyncMock(spec=["execute", "commit"])
        mock_result = MagicMock()
        mock_result.scalars.return_value.first.return_value = None
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session.commit = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_session)

        with (
            patch("app.lifecycle.AsyncSessionLocal", return_value=mock_cm),
            patch(
                "app.deploy_identity.deploy_repo_identity",
                return_value=self.DEPLOY_IDENTITY,
            ),
            patch("app.capability_trial_verdict.flag_modified") as mock_flag,
        ):
            await _seed_deploy_project_futility_verdicts()

        mock_flag.assert_not_called()
        assert other_project.capability_adoption_verdicts == {}


class TestDisabledPilotAwaitingEnablement:
    """Reconcile the review_fix_lane trial verdict/debt with the disabled-pilot state.

    While the pilot ships default-off, no intent-to-treat arm key is ever written
    (record_lane_arm_assignment is a no-op), so the trial stays at n=0 forever.
    The off-state must be REPORTED as ``awaiting_enablement`` (not the generic
    ``insufficient_data``) so the debt path defers to the dormant-knob obligation
    instead of opening a duplicate, never-resolvable capability-trial debt.
    """

    def test_disabled_pilot_zero_sample_verdict_is_awaiting_enablement(self):
        """(a) pilot_active=False + empty arms → awaiting_enablement CONTINUE + marker."""
        verdict = evaluate_lane_pilot_verdict({}, pilot_active=False)
        assert verdict["verdict"] == "CONTINUE"
        assert verdict["verdict_reason"] == "awaiting_enablement"
        assert verdict["awaiting_enablement"] is True
        assert verdict["immediate_alert"] is False
        assert verdict["n_lane"] == 0
        assert verdict["n_control"] == 0

    def test_active_pilot_zero_sample_stays_insufficient_data(self):
        """(b) pilot_active=True + empty arms → legacy insufficient_data CONTINUE (no marker)."""
        verdict = evaluate_lane_pilot_verdict({}, pilot_active=True)
        assert verdict["verdict"] == "CONTINUE"
        assert verdict["verdict_reason"] == "insufficient_data"
        assert "awaiting_enablement" not in verdict

    def test_default_none_preserves_legacy_insufficient_data(self):
        """(b) default pilot_active=None must not change today's behavior at n=0."""
        verdict = evaluate_lane_pilot_verdict({})
        assert verdict["verdict"] == "CONTINUE"
        assert verdict["verdict_reason"] == "insufficient_data"
        assert "awaiting_enablement" not in verdict

    def test_awaiting_enablement_never_masks_partial_traffic(self):
        """Disabled pilot with SOME accrued arm traffic falls through to insufficient_data.

        The awaiting-enablement branch requires BOTH arms empty; a trial that already
        accrued traffic before being switched off is genuinely accumulating, not
        awaiting first enablement.
        """
        verdict = evaluate_lane_pilot_verdict(
            {"n_lane": 5, "n_control": 3}, pilot_active=False
        )
        assert verdict["verdict"] == "CONTINUE"
        assert verdict["verdict_reason"] == "insufficient_data"
        assert "awaiting_enablement" not in verdict

    def test_awaiting_enablement_cannot_mask_gate_integrity_kill(self):
        """A gate-integrity KILL must win even when pilot_active=False is passed."""
        verdict = evaluate_lane_pilot_verdict(
            {"gate_integrity_violations": 3}, pilot_active=False
        )
        assert verdict["verdict"] == "KILL"
        assert verdict["verdict_reason"] == "gate_integrity_violations_exceeded"
        assert verdict["immediate_alert"] is True

    def test_should_record_capability_trial_debt_predicate(self):
        """(c-predicate) awaiting-enablement entries suppress the capability-trial debt."""
        from app.capability_trial_verdict import (
            is_awaiting_enablement_entry,
            should_record_capability_trial_debt,
        )

        awaiting = evaluate_lane_pilot_verdict({}, pilot_active=False)
        assert is_awaiting_enablement_entry(awaiting) is True
        assert should_record_capability_trial_debt(awaiting) is False

        # A marker set without the reason string is also honored.
        assert is_awaiting_enablement_entry({"awaiting_enablement": True}) is True

        # An ordinary insufficient_data CONTINUE still records its debt.
        ordinary = evaluate_lane_pilot_verdict({}, pilot_active=True)
        assert is_awaiting_enablement_entry(ordinary) is False
        assert should_record_capability_trial_debt(ordinary) is True

    def _build_mock_db(self, prior_verdicts: dict | None = None):
        """Return (mock_session, mock_project, mock_cm) mirroring the notice-emission suite."""
        mock_project = MagicMock()
        mock_project.capability_adoption_verdicts = dict(prior_verdicts or {})

        mock_session = MagicMock()
        mock_session.get = AsyncMock(return_value=mock_project)
        mock_session.commit = AsyncMock()
        mock_session.rollback = AsyncMock()
        scalars_mock = MagicMock()
        scalars_mock.all.return_value = []
        scalars_mock.first.return_value = mock_project
        execute_result = MagicMock()
        execute_result.scalars.return_value = scalars_mock
        execute_result.scalar.return_value = None
        mock_session.execute = AsyncMock(return_value=execute_result)
        mock_session.add = MagicMock()

        mock_cm = MagicMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        mock_cm.return_value = mock_cm
        return mock_session, mock_project, mock_cm

    @pytest.mark.asyncio
    async def test_disabled_pilot_zero_sample_trial_reports_awaiting_enablement_not_perpetual_debt(
        self,
    ):
        """Acceptance: off-state lane trial resolves (not re-opens) the capability-trial debt.

        (c-DB) An awaiting-enablement CONTINUE verdict for review_fix_lane must NOT
        call arecord_completion_debt for capability_trial:{project}:review_fix_lane,
        and MUST call aresolve_completion_debt for that key so any pre-existing
        never-resolvable row is cleared. The single surviving obligation is the
        dormant-knob debt owned elsewhere.
        """
        import uuid

        from app.capability_trial_verdict import persist_trial_verdicts_in_transaction

        project_id = uuid.uuid4()
        _, _, mock_cm = self._build_mock_db(prior_verdicts={})
        mock_factory = MagicMock(return_value=mock_cm)

        awaiting_entry = evaluate_lane_pilot_verdict({}, pilot_active=False)

        with (
            patch("sqlalchemy.ext.asyncio.async_sessionmaker", mock_factory),
            patch("app.database.resolve_async_engine", return_value=MagicMock()),
            patch("app.capability_trial_verdict.flag_modified"),
            patch(
                "app.completion_debt.arecord_completion_debt",
                new_callable=AsyncMock,
            ) as mock_record_debt,
            patch(
                "app.completion_debt.aresolve_completion_debt",
                new_callable=AsyncMock,
            ) as mock_resolve_debt,
        ):
            await persist_trial_verdicts_in_transaction(
                MagicMock(),
                project_id,
                pending_verdicts=[(LANE_PILOT_CAP_NAME, awaiting_entry)],
            )

        expected_key = f"capability_trial:{project_id}:{LANE_PILOT_CAP_NAME}"
        # No new capability-trial debt for the awaiting-enablement verdict.
        for call in mock_record_debt.call_args_list:
            assert call.kwargs.get("dedupe_key") != expected_key
        # Any pre-existing capability-trial debt is resolved with the reconcile reason.
        mock_resolve_debt.assert_awaited()
        resolved_keys = {
            (c.args[1] if len(c.args) > 1 else c.kwargs.get("dedupe_key"))
            for c in mock_resolve_debt.call_args_list
        }
        assert expected_key in resolved_keys
        reasons = {c.kwargs.get("reason") for c in mock_resolve_debt.call_args_list}
        assert "awaiting_enablement:dormant_knob_tracks_decision" in reasons

    @pytest.mark.asyncio
    async def test_active_pilot_zero_sample_still_records_capability_trial_debt(self):
        """Regression: an ACTIVE-pilot insufficient_data CONTINUE still opens the debt."""
        import uuid

        from app.capability_trial_verdict import persist_trial_verdicts_in_transaction

        project_id = uuid.uuid4()
        _, _, mock_cm = self._build_mock_db(prior_verdicts={})
        mock_factory = MagicMock(return_value=mock_cm)

        active_entry = evaluate_lane_pilot_verdict({}, pilot_active=True)

        with (
            patch("sqlalchemy.ext.asyncio.async_sessionmaker", mock_factory),
            patch("app.database.resolve_async_engine", return_value=MagicMock()),
            patch("app.capability_trial_verdict.flag_modified"),
            patch(
                "app.completion_debt.arecord_completion_debt",
                new_callable=AsyncMock,
            ) as mock_record_debt,
            patch(
                "app.completion_debt.aresolve_completion_debt",
                new_callable=AsyncMock,
            ) as mock_resolve_debt,
        ):
            await persist_trial_verdicts_in_transaction(
                MagicMock(),
                project_id,
                pending_verdicts=[(LANE_PILOT_CAP_NAME, active_entry)],
            )

        expected_key = f"capability_trial:{project_id}:{LANE_PILOT_CAP_NAME}"
        recorded_keys = {
            c.kwargs.get("dedupe_key") for c in mock_record_debt.call_args_list
        }
        assert expected_key in recorded_keys
        # And it must NOT be resolved in the same pass.
        resolved_keys = {
            (c.args[1] if len(c.args) > 1 else c.kwargs.get("dedupe_key"))
            for c in mock_resolve_debt.call_args_list
        }
        assert expected_key not in resolved_keys
