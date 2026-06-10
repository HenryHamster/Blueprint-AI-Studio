"""Tests for capability_trial_verdict.py.

Covers:
  - evaluate_trial_verdict: adopt/reject/continue-trial at threshold boundaries
  - evaluate_trial_verdict: insufficient-sample → continue-trial
  - evaluate_trial_verdict: None lift → continue-trial
  - evaluate_trial_verdict: configurable thresholds respected
  - record_trial_verdict: upserts and supersedes prior verdict
  - get_trial_verdicts: returns empty dict when column is absent/None
"""

from unittest.mock import MagicMock, patch

from app.capability_trial_verdict import (
    evaluate_trial_verdict,
    get_effective_holdback_fraction,
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
        project = _make_project()
        prior = {"verdict": "continue-trial", "lift": 0.01, "n_with": 10, "n_without": 10}
        project.capability_adoption_verdicts = {"repo_map": prior}
        newer = {"verdict": "adopt", "lift": 0.07, "n_with": 40, "n_without": 40}

        with patch("app.capability_trial_verdict.flag_modified"):
            record_trial_verdict(project, "repo_map", newer)

        assert project.capability_adoption_verdicts["repo_map"]["verdict"] == "adopt"
        assert project.capability_adoption_verdicts["repo_map"]["lift"] == 0.07

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
