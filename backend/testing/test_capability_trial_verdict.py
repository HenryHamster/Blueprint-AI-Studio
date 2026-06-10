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
    evaluate_trial_verdict,
    get_effective_holdback_fraction,
    get_trial_definitions,
    get_trial_verdicts,
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
        assert "thresholds" in rm
        assert "min_n_per_arm" in rm["thresholds"]
        assert "adopt_lift" in rm["thresholds"]
        assert "reject_lift" in rm["thresholds"]

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
