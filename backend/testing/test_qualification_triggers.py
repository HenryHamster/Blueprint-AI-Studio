"""Tests for the qualification_triggers module.

Covers:
  * resolve_qualification_trigger — all T0-T4 rule table rows, including the
    CI-readiness gate on T3 (red CI suppresses post_dry_run_advance fire).
  * explain_qualification_trigger — correct auto_trigger label and next_expected_trigger.
  * Table-driven parametrized cases for edge combinations.
"""

import pytest

from app.qualification_triggers import (
    _MAX_ERROR_REDRIVE_ATTEMPTS,
    _TRANSIENT_ERROR_CODES,
    explain_qualification_trigger,
    resolve_qualification_trigger,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve(**kwargs):
    """Call resolve_qualification_trigger with defaults suitable for a healthy
    stabilize-phase version, overridden by kwargs."""
    defaults = dict(
        phase="stabilize",
        status=None,
        error_code=None,
        qualification_attempts=0,
        coverage={"total": 1, "covered": 1, "attested": 0},
        requalification_ready=None,
        candidate_sha=None,
        automain_sha=None,
        automain_ci_state=None,
        blocker=None,
        in_flight=False,
        promotion_in_flight=False,
    )
    defaults.update(kwargs)
    return resolve_qualification_trigger(**defaults)


# ---------------------------------------------------------------------------
# T0 — hard hold guards
# ---------------------------------------------------------------------------

class TestT0HoldGuards:
    def test_wrong_phase_blocks(self):
        d = _resolve(phase="build")
        assert not d.fire
        assert d.reason == "wrong_phase"

    def test_in_flight_blocks(self):
        d = _resolve(in_flight=True)
        assert not d.fire
        assert d.reason == "in_flight"

    def test_promotion_in_flight_blocks(self):
        d = _resolve(promotion_in_flight=True)
        assert not d.fire
        assert d.reason == "promotion_in_flight"

    def test_wrong_phase_takes_priority_over_everything(self):
        # Even if everything else would fire, wrong phase wins.
        d = _resolve(phase="build", status=None, coverage={"total": 1, "covered": 1})
        assert not d.fire
        assert d.reason == "wrong_phase"


# ---------------------------------------------------------------------------
# T1 — coverage_complete
# ---------------------------------------------------------------------------

class TestT1CoverageComplete:
    def test_never_attempted_coverage_complete_fires(self):
        d = _resolve(status=None, coverage={"total": 1, "covered": 1})
        assert d.fire
        assert d.trigger == "coverage_complete"

    def test_never_attempted_no_criteria_fires(self):
        # total == 0 is not incomplete; coverage gate passes
        d = _resolve(status=None, coverage={"total": 0, "covered": 0})
        assert d.fire
        assert d.trigger == "coverage_complete"

    def test_error_status_coverage_complete_fires(self):
        d = _resolve(status="error", error_code="sha_error",
                     coverage={"total": 2, "covered": 2})
        # T2 would also match but T1 comes first
        assert d.fire
        assert d.trigger == "coverage_complete"

    def test_not_promoted_coverage_blocker_fires(self):
        d = _resolve(
            status="not_promoted",
            blocker="criterion_coverage_incomplete",
            coverage={"total": 1, "covered": 1, "attested": 0},
        )
        assert d.fire
        assert d.trigger == "coverage_complete"

    def test_coverage_incomplete_blocks(self):
        d = _resolve(status=None, coverage={"total": 2, "covered": 1})
        assert not d.fire
        assert d.reason == "coverage_incomplete"
        assert "1/2" in d.blocking[0]

    def test_coverage_none_treated_as_zero_total(self):
        # coverage=None → total=0 → not incomplete → fires
        d = _resolve(status=None, coverage=None)
        assert d.fire
        assert d.trigger == "coverage_complete"

    def test_attested_only_is_not_coverage_incomplete(self):
        # All criteria attested-only — coverage gate satisfied (matching scorer).
        d = _resolve(status=None, coverage={"total": 2, "covered": 0, "attested": 2})
        assert d.fire
        assert d.trigger == "coverage_complete"

    def test_mixed_attested_and_covered_fires(self):
        # Mix of covered + attested satisfies the gate.
        d = _resolve(status=None, coverage={"total": 3, "covered": 1, "attested": 2})
        assert d.fire
        assert d.trigger == "coverage_complete"

    def test_attested_not_enough_when_uncovered_remains(self):
        # Even with some attested, uncovered criteria still block.
        d = _resolve(
            status=None,
            coverage={"total": 2, "covered": 0, "attested": 1},
        )
        assert not d.fire
        assert d.reason == "coverage_incomplete"
        assert "0/2" in d.blocking[0]

    def test_backfilled_only_coverage_arms_trigger(self):
        # All remaining criteria covered solely by the 'backfilled' tier — the
        # authoritative release_scoring gate treats these as clearing the
        # coverage blocker, so the T1 trigger must arm (no strand). Regression
        # for: backfilled tier omitted from coverage-gate sibling predicates.
        d = _resolve(
            status=None,
            coverage={"total": 2, "covered": 0, "attested": 0, "backfilled": 2},
        )
        assert d.fire is True
        assert d.trigger == "coverage_complete"

    def test_mixed_backfilled_covered_and_attested_fires(self):
        # A mix spanning all three clearing tiers satisfies the gate.
        d = _resolve(
            status=None,
            coverage={"total": 3, "covered": 1, "attested": 1, "backfilled": 1},
        )
        assert d.fire is True
        assert d.trigger == "coverage_complete"

    def test_backfilled_not_enough_when_uncovered_remains(self):
        # Backfilled closes some but genuinely-uncovered criteria still block,
        # and the blocking message surfaces the backfilled count.
        d = _resolve(
            status=None,
            coverage={"total": 3, "covered": 0, "attested": 0, "backfilled": 1},
        )
        assert not d.fire
        assert d.reason == "coverage_incomplete"
        assert "0/3" in d.blocking[0]
        assert "1 backfilled" in d.blocking[0]


# ---------------------------------------------------------------------------
# T2 — error_redrive
# ---------------------------------------------------------------------------

class TestT2ErrorRedrive:
    def test_transient_error_fires(self):
        for code in _TRANSIENT_ERROR_CODES:
            # T1 would fire if coverage is complete with no criteria, so use incomplete
            # coverage to let T2 be the first matching rule.
            d = _resolve(
                status="error",
                error_code=code,
                qualification_attempts=1,
                coverage={"total": 2, "covered": 1},
            )
            assert d.fire
            assert d.trigger == "error_redrive"

    def test_non_transient_error_fires_when_readiness_not_false(self):
        d = _resolve(
            status="error",
            error_code="spawn_failed",
            qualification_attempts=2,
            coverage={"total": 2, "covered": 1},
            requalification_ready=None,  # not False → eligible
        )
        assert d.fire
        assert d.trigger == "error_redrive"

    def test_non_transient_error_blocked_when_readiness_false(self):
        d = _resolve(
            status="error",
            error_code="spawn_failed",
            qualification_attempts=2,
            coverage={"total": 2, "covered": 1},
            requalification_ready=False,
        )
        assert not d.fire
        assert d.reason == "not_applicable"

    def test_attempt_cap_blocks_redrive(self):
        d = _resolve(
            status="error",
            error_code="sha_error",
            qualification_attempts=_MAX_ERROR_REDRIVE_ATTEMPTS,
            coverage={"total": 2, "covered": 1},
        )
        assert not d.fire
        assert d.reason == "error_attempt_cap"
        assert str(_MAX_ERROR_REDRIVE_ATTEMPTS) in d.blocking[0]

    def test_attempt_just_below_cap_fires(self):
        d = _resolve(
            status="error",
            error_code="sha_error",
            qualification_attempts=_MAX_ERROR_REDRIVE_ATTEMPTS - 1,
            coverage={"total": 2, "covered": 1},
        )
        assert d.fire
        assert d.trigger == "error_redrive"


# ---------------------------------------------------------------------------
# T3 — post_dry_run_advance (with CI-readiness gate)
# ---------------------------------------------------------------------------

class TestT3PostDryRunAdvance:
    def test_fires_when_sha_advanced(self):
        d = _resolve(
            status="dry_run",
            candidate_sha="old_sha",
            automain_sha="new_sha",
            automain_ci_state=None,
        )
        assert d.fire
        assert d.trigger == "post_dry_run_advance"

    def test_fires_when_ci_green(self):
        d = _resolve(
            status="dry_run",
            candidate_sha="old_sha",
            automain_sha="new_sha",
            automain_ci_state="green",
        )
        assert d.fire
        assert d.trigger == "post_dry_run_advance"

    def test_fires_when_ci_pending(self):
        # Pending CI should NOT block (unknown timing is fine)
        d = _resolve(
            status="dry_run",
            candidate_sha="old_sha",
            automain_sha="new_sha",
            automain_ci_state="pending",
        )
        assert d.fire
        assert d.trigger == "post_dry_run_advance"

    def test_fires_when_ci_no_checks(self):
        d = _resolve(
            status="dry_run",
            candidate_sha="old_sha",
            automain_sha="new_sha",
            automain_ci_state="no_checks",
        )
        assert d.fire
        assert d.trigger == "post_dry_run_advance"

    def test_blocked_when_ci_red(self):
        """CRITICAL: known-red CI must suppress T3 auto-fire."""
        d = _resolve(
            status="dry_run",
            candidate_sha="old_sha",
            automain_sha="new_sha",
            automain_ci_state="red",
        )
        assert not d.fire
        assert d.reason == "ci_red"
        assert "ci_red" in " ".join(d.blocking).lower() or "red" in " ".join(d.blocking).lower()

    def test_no_fire_when_sha_unchanged(self):
        d = _resolve(
            status="dry_run",
            candidate_sha="same_sha",
            automain_sha="same_sha",
            automain_ci_state=None,
        )
        assert not d.fire
        assert d.reason == "not_applicable"

    def test_no_fire_when_automain_sha_none(self):
        d = _resolve(
            status="dry_run",
            candidate_sha="some_sha",
            automain_sha=None,
        )
        assert not d.fire
        assert d.reason == "not_applicable"


# ---------------------------------------------------------------------------
# T4 — blockers_cleared
# ---------------------------------------------------------------------------

class TestT4BlockersCleared:
    def test_not_promoted_readiness_fires(self):
        d = _resolve(
            status="not_promoted",
            blocker="high_audit_finding",
            requalification_ready=True,
            coverage={"total": 2, "covered": 1},  # incomplete → T1 blocked
        )
        assert d.fire
        assert d.trigger == "blockers_cleared"

    def test_not_promoted_readiness_false_blocks(self):
        d = _resolve(
            status="not_promoted",
            blocker="high_audit_finding",
            requalification_ready=False,
            coverage={"total": 2, "covered": 1},
        )
        assert not d.fire
        assert d.reason == "findings_unresolved"

    def test_promotion_failed_readiness_fires(self):
        d = _resolve(
            status="promotion_failed",
            requalification_ready=True,
            coverage={"total": 2, "covered": 1},
        )
        assert d.fire
        assert d.trigger == "blockers_cleared"

    def test_promotion_failed_readiness_false_blocks(self):
        d = _resolve(
            status="promotion_failed",
            requalification_ready=False,
            coverage={"total": 2, "covered": 1},
        )
        assert not d.fire
        assert d.reason == "findings_unresolved"


# ---------------------------------------------------------------------------
# Not-applicable fallback
# ---------------------------------------------------------------------------

class TestNotApplicable:
    def test_promoted_version_not_applicable(self):
        # A 'promoted' status has no auto-trigger rule
        d = _resolve(status="promoted")
        assert not d.fire
        assert d.reason == "not_applicable"

    def test_dry_run_same_sha_not_applicable(self):
        d = _resolve(
            status="dry_run",
            candidate_sha="abc",
            automain_sha="abc",
        )
        assert not d.fire
        assert d.reason == "not_applicable"


# ---------------------------------------------------------------------------
# CI-readiness gate — parametrized table
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ci_state,expect_fire", [
    ("red",      False),
    ("green",    True),
    ("pending",  True),
    ("no_checks", True),
    (None,       True),
])
def test_ci_gate_parametrized(ci_state, expect_fire):
    """Red CI must block T3; all other states (including unknown) allow fire."""
    d = _resolve(
        status="dry_run",
        candidate_sha="old",
        automain_sha="new",
        automain_ci_state=ci_state,
        coverage={"total": 0, "covered": 0},
    )
    assert d.fire == expect_fire, (
        f"ci_state={ci_state!r}: expected fire={expect_fire}, got {d.fire} "
        f"(reason={d.reason}, blocking={d.blocking})"
    )
    if expect_fire:
        assert d.trigger == "post_dry_run_advance"
    else:
        assert d.reason == "ci_red"


# ---------------------------------------------------------------------------
# explain_qualification_trigger
# ---------------------------------------------------------------------------

class TestExplainQualificationTrigger:
    def _explain(self, **kwargs):
        defaults = dict(
            phase="stabilize",
            status=None,
            error_code=None,
            qualification_attempts=0,
            coverage={"total": 1, "covered": 1, "attested": 0},
            requalification_ready=None,
            candidate_sha=None,
            automain_sha=None,
            automain_ci_state=None,
            blocker=None,
            in_flight=False,
            promotion_in_flight=False,
            last_attempt=None,
            knob="observe",
            evaluated_at="2026-07-10T00:00:00",
        )
        defaults.update(kwargs)
        return explain_qualification_trigger(**defaults)

    def test_observe_mode_returns_observe_auto_trigger(self):
        out = self._explain(knob="observe")
        assert out["auto_trigger"] == "observe"

    def test_off_mode_returns_off_auto_trigger(self):
        out = self._explain(knob="off")
        assert out["auto_trigger"] == "off"

    def test_on_mode_armed_returns_armed(self):
        out = self._explain(knob="on")
        assert out["auto_trigger"] == "armed"

    def test_on_mode_waiting_returns_waiting(self):
        out = self._explain(
            knob="on",
            status=None,
            coverage={"total": 2, "covered": 1},  # incomplete
        )
        assert out["auto_trigger"] == "waiting"

    def test_in_flight_returns_in_flight(self):
        out = self._explain(knob="on", in_flight=True)
        assert out["auto_trigger"] == "in_flight"

    def test_capped_returns_capped(self):
        out = self._explain(
            knob="on",
            status="error",
            error_code="sha_error",
            qualification_attempts=_MAX_ERROR_REDRIVE_ATTEMPTS,
            coverage={"total": 2, "covered": 1},
        )
        assert out["auto_trigger"] == "capped"

    def test_settled_returns_settled(self):
        # A promoted version has no rule → settled
        out = self._explain(knob="on", status="promoted")
        assert out["auto_trigger"] == "settled"

    def test_shape_keys_present(self):
        out = self._explain()
        assert "auto_trigger" in out
        assert "last_attempt" in out
        assert "next_expected_trigger" in out
        assert "blocking" in out
        assert "attempts" in out
        assert "knob" in out
        assert "evaluated_at" in out
        assert "decision" in out

    def test_ci_red_blocking_surfaced_in_explain(self):
        out = self._explain(
            knob="on",
            status="dry_run",
            candidate_sha="old",
            automain_sha="new",
            automain_ci_state="red",
        )
        assert out["auto_trigger"] == "waiting"
        assert out["decision"]["reason"] == "ci_red"
        assert len(out["blocking"]) > 0

    def test_next_expected_trigger_for_coverage_incomplete(self):
        out = self._explain(
            knob="on",
            status=None,
            coverage={"total": 2, "covered": 1},
        )
        assert out["next_expected_trigger"] == "coverage_complete"

    def test_next_expected_trigger_for_ci_red(self):
        out = self._explain(
            knob="on",
            status="dry_run",
            candidate_sha="old",
            automain_sha="new",
            automain_ci_state="red",
        )
        assert out["next_expected_trigger"] == "new_candidate_commit"

    def test_attempts_counter(self):
        out = self._explain(qualification_attempts=3)
        assert out["attempts"]["count"] == 3
        assert out["attempts"]["cap"] == _MAX_ERROR_REDRIVE_ATTEMPTS
