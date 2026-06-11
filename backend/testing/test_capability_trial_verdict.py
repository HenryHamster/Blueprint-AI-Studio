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

from unittest.mock import MagicMock, patch

from app.capability_trial_verdict import (
    LANE_PILOT_ASSIGNMENT_KEY,
    LANE_PILOT_CAP_NAME,
    LANE_PILOT_CHECKPOINT_RECEIPT_KEY,
    LANE_PILOT_WARM_RESUME_RECEIPT_KEY,
    carry_adoption_verdicts_for_consolidation,
    evaluate_lane_pilot_verdict,
    evaluate_trial_verdict,
    get_effective_holdback_fraction,
    get_trial_definitions,
    get_trial_verdicts,
    is_lane_pilot_arm_member,
    record_trial_verdict,
)


# ── evaluate_trial_verdict ────────────────────────────────────────────────────


class TestEvaluateTrialVerdict:
    def _cfg(self, **overrides):
        base = {"min_n_per_arm": 30, "adopt_lift": 0.05, "reject_lift": -0.03}
        base.update(overrides)
        return base

    def test_adopt_at_threshold(self):
        entry = evaluate_trial_verdict(
            lift=0.05, n_with=30, n_without=30,
            config=self._cfg(), evaluated_at="2026-06-09T00:00:00"
        )
        assert entry["verdict"] == "adopt"
        assert entry["lift"] == 0.05
        assert entry["n_with"] == 30
        assert entry["n_without"] == 30

    def test_adopt_above_threshold(self):
        entry = evaluate_trial_verdict(
            lift=0.12, n_with=50, n_without=50,
            config=self._cfg(),
        )
        assert entry["verdict"] == "adopt"

    def test_reject_at_threshold(self):
        entry = evaluate_trial_verdict(
            lift=-0.03, n_with=30, n_without=30,
            config=self._cfg(),
        )
        assert entry["verdict"] == "reject"

    def test_reject_below_threshold(self):
        entry = evaluate_trial_verdict(
            lift=-0.10, n_with=40, n_without=40,
            config=self._cfg(),
        )
        assert entry["verdict"] == "reject"

    def test_continue_trial_lift_between_thresholds(self):
        entry = evaluate_trial_verdict(
            lift=0.02, n_with=30, n_without=30,
            config=self._cfg(),
        )
        assert entry["verdict"] == "continue-trial"

    def test_continue_trial_negative_but_above_reject(self):
        entry = evaluate_trial_verdict(
            lift=-0.01, n_with=30, n_without=30,
            config=self._cfg(),
        )
        assert entry["verdict"] == "continue-trial"

    def test_continue_trial_insufficient_with_arm(self):
        entry = evaluate_trial_verdict(
            lift=0.10, n_with=5, n_without=30,
            config=self._cfg(),
        )
        assert entry["verdict"] == "continue-trial"

    def test_continue_trial_insufficient_without_arm(self):
        entry = evaluate_trial_verdict(
            lift=0.10, n_with=30, n_without=5,
            config=self._cfg(),
        )
        assert entry["verdict"] == "continue-trial"

    def test_continue_trial_none_lift(self):
        entry = evaluate_trial_verdict(
            lift=None, n_with=30, n_without=30,
            config=self._cfg(),
        )
        assert entry["verdict"] == "continue-trial"
        assert entry["lift"] is None

    def test_evaluated_at_propagated(self):
        entry = evaluate_trial_verdict(
            lift=0.06, n_with=30, n_without=30,
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
            lift=0.06, n_with=42, n_without=37,
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
            lift=0.06, n_with=10, n_without=10,
            config={"min_n_per_arm": 10, "adopt_lift": 0.05, "reject_lift": -0.03},
        )
        assert entry["verdict"] == "adopt"

    def test_configurable_adopt_lift_respected(self):
        # Require a higher lift to adopt — lift=0.05 should still be continue.
        entry = evaluate_trial_verdict(
            lift=0.05, n_with=30, n_without=30,
            config={"min_n_per_arm": 30, "adopt_lift": 0.10, "reject_lift": -0.03},
        )
        assert entry["verdict"] == "continue-trial"

    def test_configurable_reject_lift_respected(self):
        # Require a steeper drop to reject — lift=-0.03 should be continue.
        entry = evaluate_trial_verdict(
            lift=-0.03, n_with=30, n_without=30,
            config={"min_n_per_arm": 30, "adopt_lift": 0.05, "reject_lift": -0.08},
        )
        assert entry["verdict"] == "continue-trial"

    def test_thresholds_included_in_entry(self):
        entry = evaluate_trial_verdict(
            lift=0.06, n_with=30, n_without=30,
            config={"min_n_per_arm": 30, "adopt_lift": 0.05, "reject_lift": -0.03},
        )
        assert entry["thresholds"]["min_n_per_arm"] == 30
        assert entry["thresholds"]["adopt_lift"] == 0.05
        assert entry["thresholds"]["reject_lift"] == -0.03

    def test_verdict_reason_sufficient_data_adopt(self):
        entry = evaluate_trial_verdict(
            lift=0.06, n_with=30, n_without=30,
            config=self._cfg(),
        )
        assert entry["verdict"] == "adopt"
        assert entry["verdict_reason"] == "sufficient_data"

    def test_verdict_reason_sufficient_data_reject(self):
        entry = evaluate_trial_verdict(
            lift=-0.05, n_with=30, n_without=30,
            config=self._cfg(),
        )
        assert entry["verdict"] == "reject"
        assert entry["verdict_reason"] == "sufficient_data"

    def test_verdict_reason_ambiguous_lift(self):
        entry = evaluate_trial_verdict(
            lift=0.02, n_with=30, n_without=30,
            config=self._cfg(),
        )
        assert entry["verdict"] == "continue-trial"
        assert entry["verdict_reason"] == "ambiguous_lift"

    def test_verdict_reason_insufficient_data_none_lift(self):
        entry = evaluate_trial_verdict(
            lift=None, n_with=30, n_without=30,
            config=self._cfg(),
        )
        assert entry["verdict"] == "continue-trial"
        assert entry["verdict_reason"] == "insufficient_data"

    def test_verdict_reason_insufficient_data_below_min_n(self):
        entry = evaluate_trial_verdict(
            lift=0.06, n_with=5, n_without=30,
            config=self._cfg(),
        )
        assert entry["verdict"] == "continue-trial"
        assert entry["verdict_reason"] == "insufficient_data"

    # ── futility bound tests ──────────────────────────────────────────────────

    def test_futility_bound_terminates_ambiguous_trial(self):
        # Acceptance test: both arms at futility_n with ambiguous lift → reject/futility_bound_reached
        # Default futility_n = 30 * 3 = 90
        entry = evaluate_trial_verdict(
            lift=0.02, n_with=90, n_without=90,
            config=self._cfg(),
        )
        assert entry["verdict"] == "reject"
        assert entry["verdict_reason"] == "futility_bound_reached"

    def test_futility_bound_reached_emits_reject(self):
        entry = evaluate_trial_verdict(
            lift=0.01, n_with=100, n_without=100,
            config=self._cfg(),
        )
        assert entry["verdict"] == "reject"
        assert entry["verdict_reason"] == "futility_bound_reached"

    def test_below_futility_bound_stays_ambiguous(self):
        # Both arms at min_n (30) but below futility_n (90)
        entry = evaluate_trial_verdict(
            lift=0.02, n_with=30, n_without=30,
            config=self._cfg(),
        )
        assert entry["verdict"] == "continue-trial"
        assert entry["verdict_reason"] == "ambiguous_lift"

    def test_one_arm_below_futility_bound_stays_ambiguous(self):
        # n_with >= futility_n but n_without < futility_n
        entry = evaluate_trial_verdict(
            lift=0.02, n_with=90, n_without=50,
            config=self._cfg(),
        )
        assert entry["verdict"] == "continue-trial"
        assert entry["verdict_reason"] == "ambiguous_lift"

    def test_futility_bound_not_triggered_when_adopt_lift_met(self):
        # lift >= adopt_lift must still produce adopt even at futility_n
        entry = evaluate_trial_verdict(
            lift=0.06, n_with=90, n_without=90,
            config=self._cfg(),
        )
        assert entry["verdict"] == "adopt"
        assert entry["verdict_reason"] == "sufficient_data"

    def test_futility_bound_not_triggered_when_reject_lift_met(self):
        # lift <= reject_lift must still produce reject/sufficient_data even at futility_n
        entry = evaluate_trial_verdict(
            lift=-0.05, n_with=90, n_without=90,
            config=self._cfg(),
        )
        assert entry["verdict"] == "reject"
        assert entry["verdict_reason"] == "sufficient_data"

    def test_futility_n_included_in_thresholds_entry(self):
        entry = evaluate_trial_verdict(
            lift=0.02, n_with=90, n_without=90,
            config=self._cfg(),
        )
        assert "futility_n" in entry["thresholds"]
        assert entry["thresholds"]["futility_n"] == 90

    def test_configurable_futility_n_respected(self):
        # Custom futility_n lower than default — smaller arms can trigger futility
        cfg = self._cfg()
        cfg["futility_n"] = 50
        entry = evaluate_trial_verdict(
            lift=0.01, n_with=50, n_without=50,
            config=cfg,
        )
        assert entry["verdict"] == "reject"
        assert entry["verdict_reason"] == "futility_bound_reached"

    def test_futility_bound_holdback_is_one(self):
        # futility_bound_reached reuses reject verdict — holdback must be 1.0
        entry = evaluate_trial_verdict(
            lift=0.01, n_with=90, n_without=90,
            config=self._cfg(),
        )
        verdicts = {"repo_map": entry}
        holdback = get_effective_holdback_fraction(verdicts, "repo_map", 0.1)
        assert holdback == 1.0


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
        prior = {"verdict": "continue-trial", "lift": 0.01, "n_with": 10, "n_without": 10}
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
        insufficient = {"verdict": "continue-trial", "lift": None, "n_with": 3, "n_without": 2}

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
        insufficient = {"verdict": "continue-trial", "lift": 0.0, "n_with": 5, "n_without": 0}

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
            lift=0.07, n_with=40, n_without=40,
            evaluated_at="2026-06-01T00:00:00",
        )
        assert adopt_entry["verdict"] == "adopt"

        with patch("app.capability_trial_verdict.flag_modified"):
            record_trial_verdict(project, "repo_map", adopt_entry)

        assert project.capability_adoption_verdicts["repo_map"]["verdict"] == "adopt"

        # Simulate periodic digest re-evaluation after window rolls: without-arm dried up
        reevaluation = evaluate_trial_verdict(
            lift=None, n_with=0, n_without=0,
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
        reversal = {"verdict": "adopt", "lift": 0.08, "n_with": 50, "n_without": 50, "evidence_source": "strategic_review"}

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
        assert project.capability_adoption_verdicts["repo_map"]["verdict"] == "continue-trial"
        assert project.capability_adoption_verdicts["repo_map"]["evidence_source"] == "strategic_review"
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

        assert project.capability_adoption_verdicts["repo_map"]["verdict"] == "continue-trial"
        mock_flag.assert_called_once()

    def test_non_strategic_continue_trial_still_blocked_after_decide(self):
        """Auto-evaluation continue-trial (no evidence_source) must still be blocked."""
        project = _make_project()
        decided = {"verdict": "adopt", "lift": 0.09, "n_with": 50, "n_without": 50}
        project.capability_adoption_verdicts = {"repo_map": decided}
        auto_eval = {"verdict": "continue-trial", "lift": None, "n_with": 0, "n_without": 0}

        with patch("app.capability_trial_verdict.flag_modified") as mock_flag:
            record_trial_verdict(project, "repo_map", auto_eval)

        assert project.capability_adoption_verdicts["repo_map"]["verdict"] == "adopt"
        mock_flag.assert_not_called()

    def test_continue_trial_overwrites_prior_continue_trial(self):
        # Normal trial progression: continue-trial updates are allowed
        project = _make_project()
        prior = {"verdict": "continue-trial", "lift": 0.01, "n_with": 10, "n_without": 10}
        project.capability_adoption_verdicts = {"repo_map": prior}
        newer = {"verdict": "continue-trial", "lift": 0.02, "n_with": 20, "n_without": 20}

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
        assert project.capability_adoption_verdicts["another_cap"]["verdict"] == "reject"

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
            lift=0.08, n_with=40, n_without=40,
            accumulation_basis="cumulative_terminal_arm_tasks",
        )
        assert prior["verdict"] == "adopt"
        project.capability_adoption_verdicts = {"repo_map": prior}
        incoming = evaluate_trial_verdict(
            lift=None, n_with=12, n_without=3,
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
        assert project.capability_adoption_verdicts["repo_map"]["verdict"] == "continue-trial"
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
        assert project.capability_adoption_verdicts["repo_map"]["evidence_source"] == "strategic_review"

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
        # A reject/futility_bound_reached must not be overwritten by continue-trial/insufficient_data
        project = _make_project()
        prior = {
            "verdict": "reject",
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

        assert project.capability_adoption_verdicts["repo_map"]["verdict"] == "reject"
        assert project.capability_adoption_verdicts["repo_map"]["verdict_reason"] == "futility_bound_reached"
        mock_flag.assert_not_called()

    def test_strategic_review_can_override_futility_bound(self):
        # Explicit strategist re-declaration must still override futility verdict
        project = _make_project()
        prior = {
            "verdict": "reject",
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

        assert project.capability_adoption_verdicts["repo_map"]["verdict"] == "continue-trial"
        assert project.capability_adoption_verdicts["repo_map"]["evidence_source"] == "strategic_review"
        mock_flag.assert_called_once()

    def test_decided_lane_verdict_survives_insufficient_data_continue(self):
        continue_entry = evaluate_lane_pilot_verdict({"n_lane": 1, "n_control": 1})
        assert (continue_entry["verdict"], continue_entry["verdict_reason"]) == ("CONTINUE", "insufficient_data")
        lane_kill = evaluate_lane_pilot_verdict({"n_lane": 40, "n_control": 40, "lane_merge_rate": 0.60, "control_merge_rate": 0.70})
        lane_adopt = evaluate_lane_pilot_verdict(
            {"n_lane": 40, "n_control": 40, "lane_merge_rate": 0.75, "control_merge_rate": 0.70,
             "lane_cost_per_merged": 90.0, "control_cost_per_merged": 100.0}
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
        accumulating_entry = evaluate_lane_pilot_verdict({"n_lane": 50, "n_control": 50})
        assert (accumulating_entry["verdict"], accumulating_entry["verdict_reason"]) == ("CONTINUE", "accumulating_data")

        lane_kill = evaluate_lane_pilot_verdict({"n_lane": 40, "n_control": 40, "lane_merge_rate": 0.60, "control_merge_rate": 0.70})
        lane_adopt = evaluate_lane_pilot_verdict(
            {"n_lane": 40, "n_control": 40, "lane_merge_rate": 0.75, "control_merge_rate": 0.70,
             "lane_cost_per_merged": 90.0, "control_cost_per_merged": 100.0}
        )
        assert (lane_kill["verdict"], lane_adopt["verdict"]) == ("KILL", "ADOPT")

        for decided in (lane_kill, lane_adopt):
            project = _make_project()
            project.capability_adoption_verdicts = {LANE_PILOT_CAP_NAME: decided}
            with patch("app.capability_trial_verdict.flag_modified") as mock_flag:
                record_trial_verdict(project, LANE_PILOT_CAP_NAME, accumulating_entry)

            assert project.capability_adoption_verdicts[LANE_PILOT_CAP_NAME] == decided
            mock_flag.assert_not_called()

    def test_lane_kill_survives_automated_adopt_and_strategic_verdicts_still_supersede(self):
        project = _make_project()
        kill_entry = evaluate_lane_pilot_verdict({"n_lane": 40, "n_control": 40, "lane_merge_rate": 0.60, "control_merge_rate": 0.70})
        adopt_entry = evaluate_lane_pilot_verdict(
            {"n_lane": 40, "n_control": 40, "lane_merge_rate": 0.75, "control_merge_rate": 0.70,
             "lane_cost_per_merged": 90.0, "control_cost_per_merged": 100.0}
        )
        project.capability_adoption_verdicts = {LANE_PILOT_CAP_NAME: kill_entry}

        with patch("app.capability_trial_verdict.flag_modified") as mock_flag:
            record_trial_verdict(project, LANE_PILOT_CAP_NAME, adopt_entry)

        assert project.capability_adoption_verdicts[LANE_PILOT_CAP_NAME] == kill_entry
        mock_flag.assert_not_called()

        strategic_reopen = {"verdict": "CONTINUE", "verdict_reason": "insufficient_data", "evidence_source": "strategic_review"}
        with patch("app.capability_trial_verdict.flag_modified") as mock_flag:
            record_trial_verdict(project, LANE_PILOT_CAP_NAME, strategic_reopen)

        assert project.capability_adoption_verdicts[LANE_PILOT_CAP_NAME] == strategic_reopen
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
        return {"repo_map": {"verdict": verdict, "lift": 0.0, "n_with": 40, "n_without": 40}}

    def test_effective_holdback_adopt_returns_zero(self):
        result = get_effective_holdback_fraction(self._verdicts("adopt"), "repo_map", 0.5)
        assert result == 0.0

    def test_effective_holdback_reject_returns_one(self):
        result = get_effective_holdback_fraction(self._verdicts("reject"), "repo_map", 0.5)
        assert result == 1.0

    def test_effective_holdback_continue_returns_default(self):
        result = get_effective_holdback_fraction(self._verdicts("continue-trial"), "repo_map", 0.3)
        assert result == 0.3

    def test_effective_holdback_missing_cap_returns_default(self):
        result = get_effective_holdback_fraction({"other_cap": {"verdict": "adopt"}}, "repo_map", 0.4)
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
        result = get_effective_holdback_fraction(self._verdicts("adopt"), "repo_map", 0.99)
        assert result == 0.0

    def test_effective_holdback_reject_ignores_default(self):
        result = get_effective_holdback_fraction(self._verdicts("reject"), "repo_map", 0.0)
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
        comp = {"with_assigned": 30, "with_receipt": 5, "without_assigned": 30, "without_receipt": 3}
        entry = evaluate_trial_verdict(
            lift=0.08, n_with=35, n_without=33,
            config=self._cfg(), arm_composition=comp,
        )
        assert entry["arm_composition"] == comp

    def test_arm_composition_omitted_when_not_supplied(self):
        entry = evaluate_trial_verdict(lift=0.08, n_with=30, n_without=30)
        assert "arm_composition" not in entry

    def test_purity_gate_blocks_adopt_when_assigned_with_arm_insufficient(self):
        # Total n_with=35 would satisfy min_n=30 — but only 15 are assignment-based.
        comp = {"with_assigned": 15, "with_receipt": 20, "without_assigned": 30, "without_receipt": 2}
        entry = evaluate_trial_verdict(
            lift=0.08, n_with=35, n_without=32,
            config=self._cfg(), arm_composition=comp,
        )
        assert entry["verdict"] == "continue-trial"
        assert entry["verdict_reason"] == "insufficient_assigned_arm_data"
        assert entry["arm_composition"] == comp

    def test_purity_gate_blocks_adopt_when_assigned_without_arm_insufficient(self):
        comp = {"with_assigned": 30, "with_receipt": 5, "without_assigned": 10, "without_receipt": 20}
        entry = evaluate_trial_verdict(
            lift=0.08, n_with=35, n_without=30,
            config=self._cfg(), arm_composition=comp,
        )
        assert entry["verdict"] == "continue-trial"
        assert entry["verdict_reason"] == "insufficient_assigned_arm_data"

    def test_purity_gate_allows_adopt_when_both_assigned_arms_sufficient(self):
        comp = {"with_assigned": 30, "with_receipt": 5, "without_assigned": 30, "without_receipt": 3}
        entry = evaluate_trial_verdict(
            lift=0.08, n_with=35, n_without=33,
            config=self._cfg(), arm_composition=comp,
        )
        assert entry["verdict"] == "adopt"
        assert entry["verdict_reason"] == "sufficient_data"

    def test_purity_gate_blocks_reject_when_assigned_arm_insufficient(self):
        comp = {"with_assigned": 30, "with_receipt": 5, "without_assigned": 12, "without_receipt": 18}
        entry = evaluate_trial_verdict(
            lift=-0.05, n_with=35, n_without=30,
            config=self._cfg(), arm_composition=comp,
        )
        assert entry["verdict"] == "continue-trial"
        assert entry["verdict_reason"] == "insufficient_assigned_arm_data"

    def test_purity_gate_blocks_futility_bound_when_assigned_arm_insufficient(self):
        # Total arms both >= futility_n=90, but assigned arms below min_n=30.
        comp = {"with_assigned": 20, "with_receipt": 70, "without_assigned": 20, "without_receipt": 70}
        entry = evaluate_trial_verdict(
            lift=0.02, n_with=90, n_without=90,
            config=self._cfg(), arm_composition=comp,
        )
        assert entry["verdict"] == "continue-trial"
        assert entry["verdict_reason"] == "insufficient_assigned_arm_data"

    def test_purity_gate_skipped_when_arm_composition_absent(self):
        # Without arm_composition, normal verdict logic applies.
        entry = evaluate_trial_verdict(
            lift=0.08, n_with=30, n_without=30,
            config=self._cfg(),
        )
        assert entry["verdict"] == "adopt"

    def test_purity_gate_does_not_affect_continue_trial(self):
        # continue-trial from insufficient total n is not changed by the purity gate.
        comp = {"with_assigned": 5, "with_receipt": 0, "without_assigned": 2, "without_receipt": 0}
        entry = evaluate_trial_verdict(
            lift=0.08, n_with=5, n_without=2,
            config=self._cfg(), arm_composition=comp,
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
            lift=0.08, n_with=35, n_without=33,
            config=self._cfg(), receipt_only_trial=True,
        )
        assert entry["verdict"] == "continue-trial"
        assert entry["verdict_reason"] == "receipt_only_trial"

    def test_receipt_only_blocks_reject(self):
        # Lift qualifies for reject, both arms meet min_n, but receipt_only_trial=True.
        entry = evaluate_trial_verdict(
            lift=-0.05, n_with=35, n_without=33,
            config=self._cfg(), receipt_only_trial=True,
        )
        assert entry["verdict"] == "continue-trial"
        assert entry["verdict_reason"] == "receipt_only_trial"

    def test_receipt_only_blocks_futility_bound(self):
        # Both arms >= futility_n=90 with ambiguous lift → normally futility_bound_reached.
        entry = evaluate_trial_verdict(
            lift=0.02, n_with=90, n_without=90,
            config=self._cfg(), receipt_only_trial=True,
        )
        assert entry["verdict"] == "continue-trial"
        assert entry["verdict_reason"] == "receipt_only_trial"

    def test_receipt_only_allows_continue_trial_passthrough(self):
        # Insufficient data → continue-trial/insufficient_data regardless of the flag.
        entry = evaluate_trial_verdict(
            lift=0.08, n_with=5, n_without=33,
            config=self._cfg(), receipt_only_trial=True,
        )
        assert entry["verdict"] == "continue-trial"
        assert entry["verdict_reason"] == "insufficient_data"

    def test_receipt_only_false_behaves_normally(self):
        # Default (False) must not change the adopt outcome.
        entry = evaluate_trial_verdict(
            lift=0.08, n_with=35, n_without=33,
            config=self._cfg(), receipt_only_trial=False,
        )
        assert entry["verdict"] == "adopt"

    def test_receipt_only_continue_trial_supersedes_confounded_decided_verdict(self):
        # A prior decided verdict recorded on confounded evidence must be replaced.
        project = _make_project()
        # Simulate a confounded decided verdict already on record.
        confounded = {"verdict": "adopt", "lift": 0.08, "n_with": 40, "n_without": 40}
        project.capability_adoption_verdicts = {"module_interfaces": confounded}

        new_entry = evaluate_trial_verdict(
            lift=0.08, n_with=40, n_without=40,
            config=self._cfg(), receipt_only_trial=True,
        )
        assert new_entry["verdict"] == "continue-trial"
        assert new_entry["verdict_reason"] == "receipt_only_trial"

        with patch("app.capability_trial_verdict.flag_modified") as mock_flag:
            record_trial_verdict(project, "module_interfaces", new_entry)

        # The confounded adopt must be superseded by receipt_only_trial evaluation.
        assert project.capability_adoption_verdicts["module_interfaces"]["verdict"] == "continue-trial"
        assert project.capability_adoption_verdicts["module_interfaces"]["verdict_reason"] == "receipt_only_trial"
        mock_flag.assert_called_once()

    def test_receipt_only_continue_trial_supersedes_confounded_reject(self):
        project = _make_project()
        confounded = {"verdict": "reject", "lift": -0.07, "n_with": 40, "n_without": 40}
        project.capability_adoption_verdicts = {"related_tests": confounded}

        new_entry = evaluate_trial_verdict(
            lift=-0.07, n_with=40, n_without=40,
            config=self._cfg(), receipt_only_trial=True,
        )
        assert new_entry["verdict_reason"] == "receipt_only_trial"

        with patch("app.capability_trial_verdict.flag_modified") as mock_flag:
            record_trial_verdict(project, "related_tests", new_entry)

        assert project.capability_adoption_verdicts["related_tests"]["verdict"] == "continue-trial"
        mock_flag.assert_called_once()


# ── carry_adoption_verdicts_for_consolidation ─────────────────────────────────


class TestCarryAdoptionVerdictsForConsolidation:
    def _adopt_entry(self) -> dict:
        return {"verdict": "adopt", "lift": 0.08, "n_with": 40, "n_without": 38}

    def _reject_entry(self) -> dict:
        return {"verdict": "reject", "lift": -0.05, "n_with": 35, "n_without": 33}

    def _continue_entry(self, reason: str = "insufficient_data") -> dict:
        return {"verdict": "continue-trial", "verdict_reason": reason, "lift": None, "n_with": 5, "n_without": 3}

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
        survivor_entry = {"verdict": "adopt", "lift": 0.12, "n_with": 60, "n_without": 55}
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
        assert by_name["module_interfaces"]["assignment_key"] == "_module_interfaces_arm"
        assert by_name["related_tests"]["assignment_key"] == "_related_tests_arm"
        assert by_name["affected_consumers"]["assignment_key"] == "_affected_consumers_arm"

    def _decided_verdict_with_sufficient_assigned_arms(self, cap_name: str, lift: float, verdict: str):
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
        self._decided_verdict_with_sufficient_assigned_arms("module_interfaces", 0.08, "adopt")

    def test_related_tests_adopt_with_sufficient_assigned_arms(self):
        """related_tests trial can reach adopt when both assigned arms meet min_n."""
        self._decided_verdict_with_sufficient_assigned_arms("related_tests", 0.07, "adopt")

    def test_affected_consumers_adopt_with_sufficient_assigned_arms(self):
        """affected_consumers trial can reach adopt when both assigned arms meet min_n."""
        self._decided_verdict_with_sufficient_assigned_arms("affected_consumers", 0.06, "adopt")

    def test_module_interfaces_reject_with_sufficient_assigned_arms(self):
        """module_interfaces trial can reach reject when both assigned arms meet min_n."""
        self._decided_verdict_with_sufficient_assigned_arms("module_interfaces", -0.05, "reject")

    def test_related_tests_reject_with_sufficient_assigned_arms(self):
        """related_tests trial can reach reject when both assigned arms meet min_n."""
        self._decided_verdict_with_sufficient_assigned_arms("related_tests", -0.06, "reject")

    def test_affected_consumers_reject_with_sufficient_assigned_arms(self):
        """affected_consumers trial can reach reject when both assigned arms meet min_n."""
        self._decided_verdict_with_sufficient_assigned_arms("affected_consumers", -0.04, "reject")

    def _insufficient_assigned_arm_verdict(self, cap_name: str, lift: float):
        """Helper: assigned-arm subpopulation below min_n → insufficient_assigned_arm_data."""
        # Total n_with/without both meet min_n, but assigned arms are below min_n.
        comp = {
            "with_assigned": 15,   # below min_n=30
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

        assert project.capability_adoption_verdicts["module_interfaces"]["verdict"] == "adopt"
        assert project.capability_adoption_verdicts["module_interfaces"]["accumulation_basis"] == "cumulative_terminal_arm_tasks"
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

        assert project.capability_adoption_verdicts["related_tests"]["verdict"] == "adopt"
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

        assert project.capability_adoption_verdicts["affected_consumers"]["verdict"] == "adopt"
        mock_flag.assert_called_once()

    def test_decided_verdict_not_reverted_by_insufficient_data_after_adoption(self):
        """After adopt, a subsequent insufficient-data evaluation must not self-revert it."""
        project = _make_project()

        comp = {"with_assigned": 35, "with_receipt": 5, "without_assigned": 30, "without_receipt": 3}
        adopt_entry = evaluate_trial_verdict(
            lift=0.08, n_with=40, n_without=33,
            config=self._cfg(), arm_composition=comp,
            evaluated_at="2026-06-11T00:00:00",
        )
        assert adopt_entry["verdict"] == "adopt"

        with patch("app.capability_trial_verdict.flag_modified"):
            record_trial_verdict(project, "module_interfaces", adopt_entry)

        # Post-adoption: without-arm dries up, periodic evaluator returns insufficient_data.
        stale_entry = evaluate_trial_verdict(
            lift=None, n_with=0, n_without=0,
            evaluated_at="2026-06-12T00:00:00",
        )
        assert stale_entry["verdict"] == "continue-trial"
        assert stale_entry["verdict_reason"] == "insufficient_data"

        with patch("app.capability_trial_verdict.flag_modified") as mock_flag:
            record_trial_verdict(project, "module_interfaces", stale_entry)

        # Adopt must survive.
        assert project.capability_adoption_verdicts["module_interfaces"]["verdict"] == "adopt"
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
        from app.capability_trial_verdict import _DEFAULT_ADOPT_LIFT, _DEFAULT_MIN_N_PER_ARM
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
        assert isinstance(LANE_PILOT_WARM_RESUME_RECEIPT_KEY, str) and LANE_PILOT_WARM_RESUME_RECEIPT_KEY
        assert isinstance(LANE_PILOT_CHECKPOINT_RECEIPT_KEY, str) and LANE_PILOT_CHECKPOINT_RECEIPT_KEY


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


def _good_aggs(*, n=40, merge_lift=0.10, cost_improvement=0.10, gate_violations=0, checkpoint_loss=0.01) -> dict:
    """Build a healthy aggregate dict (ADOPT territory by default)."""
    control_rate = 0.70
    lane_rate = control_rate + merge_lift
    control_cost = 1000.0
    lane_cost = control_cost * (1.0 - cost_improvement)
    return {
        "n_lane": n,
        "n_control": n,
        "lane_merge_rate": lane_rate,
        "control_merge_rate": control_rate,
        "lane_cost_per_merged": lane_cost,
        "control_cost_per_merged": control_cost,
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
        for field in ("n_lane", "n_control", "lane_merge_rate", "control_merge_rate",
                      "lane_cost_per_merged", "control_cost_per_merged",
                      "gate_integrity_violations", "checkpoint_loss_rate", "thresholds"):
            assert field in result, f"missing field: {field}"

    def test_snapshot_fields_present_on_adopt(self):
        aggs = _good_aggs()
        result = evaluate_lane_pilot_verdict(aggs)
        assert result["verdict"] == "ADOPT"
        assert "thresholds" in result
        assert isinstance(result["thresholds"], dict)
        for key in ("min_n", "kill_merge_rate_floor", "kill_cost_ceiling",
                    "kill_gate_violations_max", "kill_checkpoint_loss_ceiling",
                    "futility_n", "adopt_merge_lift_floor", "adopt_cost_improvement"):
            assert key in result["thresholds"]

    def test_snapshot_fields_present_on_continue(self):
        aggs = _good_aggs(n=10)
        result = evaluate_lane_pilot_verdict(aggs)
        assert result["verdict"] == "CONTINUE"
        assert "thresholds" in result
        assert "immediate_alert" in result
