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
    _MAX_INFRA_FAILURE_REDRIVE_ATTEMPTS,
    _MAX_RUBRIC_REDRIVE_ATTEMPTS,
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
        error_redrive_count=0,
        is_candidate_fully_met=True,
        requalification_ready=None,
        candidate_sha=None,
        automain_sha=None,
        automain_ci_state=None,
        blocker=None,
        in_flight=False,
        promotion_check_wait=False,
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

    def test_promotion_check_wait_blocks(self):
        d = _resolve(promotion_check_wait=True)
        assert not d.fire
        assert d.reason == "promotion_check_wait"

    def test_wrong_phase_takes_priority_over_everything(self):
        # Even if everything else would fire, wrong phase wins.
        d = _resolve(phase="build", status=None)
        assert not d.fire
        assert d.reason == "wrong_phase"


# ---------------------------------------------------------------------------
# T1 — initial fire (coverage gate retired)
# ---------------------------------------------------------------------------

class TestT1InitialFire:
    def test_never_attempted_fires_unconditionally(self):
        # No pre-qualification coverage gate: a never-attempted stabilize-phase
        # version arms regardless of any per-criterion signal. The acceptance
        # contract is graded INSIDE qualification, not here.
        d = _resolve(status=None)
        assert d.fire
        assert d.trigger == "coverage_complete"

    def test_never_attempted_fires_even_when_ledger_not_met(self):
        # is_candidate_fully_met is irrelevant to the initial (status=None) fire.
        d = _resolve(status=None, is_candidate_fully_met=False)
        assert d.fire
        assert d.trigger == "coverage_complete"

    def test_not_promoted_same_sha_holds_even_when_ledger_met(self):
        # A mutable ledger signal cannot authorize re-grading an immutable
        # not_promoted verdict for the same candidate.
        d = _resolve(
            status="not_promoted",
            blocker="criterion_coverage_incomplete",
            is_candidate_fully_met=True,
            candidate_sha="aaaa1111",
            automain_sha="aaaa1111",
        )
        assert not d.fire
        assert d.reason == "same_candidate"

    def test_not_promoted_coverage_blocker_blocks_when_ledger_not_met(self):
        # Ledger not met AND the candidate has not advanced → hold (no new
        # information; re-grading the same sha would spin).
        d = _resolve(
            status="not_promoted",
            blocker="criterion_coverage_incomplete",
            is_candidate_fully_met=False,
        )
        assert not d.fire
        assert d.reason == "candidate_identity_unavailable"

    def test_not_promoted_not_met_refires_when_candidate_advanced(self):
        # REGRESSION (permanent-wedge guard): CategoryVerdict rows are SHA-KEYED
        # and nothing re-grades a PAST candidate, so a not-met verdict is frozen
        # against its sha.  T4 excludes this blocker and T3 only handles dry_run,
        # so if the advanced candidate did not re-fire here, the version could
        # NEVER re-qualify — and only a re-qualification could produce the verdict
        # that would clear it.  New work landing MUST re-arm the grade.
        d = _resolve(
            status="not_promoted",
            blocker="criterion_coverage_incomplete",
            is_candidate_fully_met=False,
            candidate_sha="aaaa1111",
            automain_sha="bbbb2222",
        )
        assert d.fire
        assert d.trigger == "candidate_advanced"
        assert d.reason == "armed"

    def test_not_promoted_not_met_same_sha_does_not_spin(self):
        # The candidate has NOT advanced — re-grading the identical sha carries no
        # new information and there is no cooldown in the sweep, so it must hold.
        d = _resolve(
            status="not_promoted",
            blocker="criterion_coverage_incomplete",
            is_candidate_fully_met=False,
            candidate_sha="aaaa1111",
            automain_sha="aaaa1111",
        )
        assert not d.fire
        assert d.reason == "same_candidate"

    def test_not_promoted_not_met_advanced_candidate_suppressed_on_red_ci(self):
        # Bounded like every other redrive leg: a red new candidate does not fire.
        d = _resolve(
            status="not_promoted",
            blocker="criterion_coverage_incomplete",
            is_candidate_fully_met=False,
            candidate_sha="aaaa1111",
            automain_sha="bbbb2222",
            automain_ci_state="red",
        )
        assert not d.fire
        assert d.reason == "ci_red"

    def test_not_promoted_advanced_candidate_invalidates_unresolved_old_findings(self):
        # Findings belong to the evaluated SHA. They cannot veto grading a new
        # candidate, or stale findings permanently wedge the version.
        d = _resolve(
            status="not_promoted",
            blocker="criterion_coverage_incomplete",
            is_candidate_fully_met=False,
            candidate_sha="aaaa1111",
            automain_sha="bbbb2222",
            requalification_ready=False,
        )
        assert d.fire
        assert d.trigger == "candidate_advanced"

    @pytest.mark.parametrize(
        "blocker",
        [
            "prior_operator_authorship_nonzero",
            "high_audit_finding",
            "below_threshold",
            "criterion_coverage_incomplete",
        ],
    )
    def test_new_candidate_redrives_not_promoted_regardless_stale_blocker(self, blocker):
        d = _resolve(
            status="not_promoted",
            blocker=blocker,
            candidate_sha="old-candidate",
            automain_sha="new-candidate",
            requalification_ready=False,
        )
        assert d.fire
        assert d.trigger == "candidate_advanced"

    def test_prior_operator_authorship_same_sha_does_not_churn(self):
        d = _resolve(
            status="not_promoted",
            blocker="prior_operator_authorship_nonzero",
            candidate_sha="same-candidate",
            automain_sha="same-candidate",
            requalification_ready=True,
        )
        assert not d.fire
        assert d.reason == "same_candidate"


# ---------------------------------------------------------------------------
# T2 — error_redrive
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["rubric_changed", "rubric_provenance_unknown"])
def test_rubric_outcomes_redrive_with_a_fresh_snapshot(status):
    d = _resolve(status=status)
    assert d.fire
    assert d.trigger == "rubric_redrive"


@pytest.mark.parametrize("status", ["rubric_changed", "rubric_provenance_unknown"])
def test_rubric_redrive_cap_holds(status):
    """A rubric that keeps changing cannot spin attempts indefinitely: once the
    per-episode ``rubric_redrive_count`` reaches the cap, T2 holds (read
    surfaces report "capped", not "armed").  The dedicated re-driver enforces
    the same bound."""
    d = _resolve(status=status, rubric_redrive_count=_MAX_RUBRIC_REDRIVE_ATTEMPTS)
    assert not d.fire
    assert d.reason == "rubric_redrive_cap"


class TestT2ErrorRedrive:
    # With the coverage gate retired, a version sitting at ``error`` no longer
    # short-circuits at T1 — it falls through to T2, which keeps the
    # transient/readiness distinction and the attempt cap (bounding a
    # persistently-failing candidate).
    def test_transient_error_fires(self):
        for code in _TRANSIENT_ERROR_CODES:
            d = _resolve(
                status="error",
                error_code=code,
                qualification_attempts=1,
            )
            assert d.fire
            assert d.trigger == "error_redrive"

    def test_non_transient_error_fires_when_readiness_not_false(self):
        d = _resolve(
            status="error",
            error_code="spawn_failed",
            qualification_attempts=2,
            requalification_ready=None,  # not False → eligible
        )
        assert d.fire
        assert d.trigger == "error_redrive"

    def test_non_transient_error_blocked_when_readiness_false(self):
        d = _resolve(
            status="error",
            error_code="spawn_failed",
            qualification_attempts=2,
            requalification_ready=False,
        )
        assert not d.fire
        assert d.reason == "not_applicable"

    def test_attempt_cap_blocks_redrive(self):
        d = _resolve(
            status="error",
            error_code="sha_error",
            error_redrive_count=_MAX_ERROR_REDRIVE_ATTEMPTS,
        )
        assert not d.fire
        assert d.reason == "error_attempt_cap"
        assert str(_MAX_ERROR_REDRIVE_ATTEMPTS) in d.blocking[0]

    def test_attempt_just_below_cap_fires(self):
        d = _resolve(
            status="error",
            error_code="sha_error",
            error_redrive_count=_MAX_ERROR_REDRIVE_ATTEMPTS - 1,
        )
        assert d.fire
        assert d.trigger == "error_redrive"

    def test_lifetime_attempts_do_not_cap_error_redrive(self):
        # REGRESSION: the cap must read the PER-EPISODE error_redrive_count, NOT the
        # lifetime qualification_attempts column. release_qualifier_redrive documents
        # that capping on the lifetime counter was a bug — any version with 5+ prior
        # qualifications of ANY kind would be barred from every transient-error
        # redrive and stranded at status=error forever. A version re-qualifies on
        # each candidate advance, so a high lifetime count is NORMAL and healthy.
        d = _resolve(
            status="error",
            error_code="sha_error",
            qualification_attempts=99,
            error_redrive_count=0,
        )
        assert d.fire
        assert d.trigger == "error_redrive"

    def test_non_transient_error_at_high_lifetime_count_still_redrives(self):
        # The exact wedge: non-transient error + many lifetime attempts. The sibling
        # redrive path refuses non-transient errors, so if T2 also capped this out,
        # the version would sit at status=error with no autonomous exit.
        d = _resolve(
            status="error",
            error_code="spawn_failed",
            qualification_attempts=42,
            error_redrive_count=1,
            requalification_ready=True,
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
    def test_not_promoted_readiness_cannot_regrade_same_sha(self):
        d = _resolve(
            status="not_promoted",
            blocker="high_audit_finding",
            requalification_ready=True,
            candidate_sha="same",
            automain_sha="same",
        )
        assert not d.fire
        assert d.reason == "same_candidate"

    def test_not_promoted_readiness_false_blocks(self):
        d = _resolve(
            status="not_promoted",
            blocker="high_audit_finding",
            requalification_ready=False,
            candidate_sha="same",
            automain_sha="same",
        )
        assert not d.fire
        assert d.reason == "same_candidate"

    def test_promotion_failed_readiness_fires(self):
        d = _resolve(
            status="promotion_failed",
            requalification_ready=True,
        )
        assert d.fire
        assert d.trigger == "blockers_cleared"

    def test_promotion_failed_readiness_false_blocks(self):
        d = _resolve(
            status="promotion_failed",
            requalification_ready=False,
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
            error_redrive_count=0,
            is_candidate_fully_met=True,
            requalification_ready=None,
            candidate_sha=None,
            automain_sha=None,
            automain_ci_state=None,
            blocker=None,
            in_flight=False,
            promotion_check_wait=False,
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
        # A not_promoted/criterion_coverage version whose ledger is NOT met holds.
        out = self._explain(
            knob="on",
            status="not_promoted",
            blocker="criterion_coverage_incomplete",
            is_candidate_fully_met=False,
        )
        assert out["auto_trigger"] == "waiting"

    def test_in_flight_returns_in_flight(self):
        out = self._explain(knob="on", in_flight=True)
        assert out["auto_trigger"] == "in_flight"

    def test_capped_returns_capped(self):
        # The cap counts the ERROR EPISODE, not the version's lifetime attempts.
        out = self._explain(
            knob="on",
            status="error",
            error_code="sha_error",
            error_redrive_count=_MAX_ERROR_REDRIVE_ATTEMPTS,
        )
        assert out["auto_trigger"] == "capped"

    def test_high_lifetime_attempts_alone_does_not_cap(self):
        # Regression: a version re-qualifies on every candidate advance, so a large
        # lifetime count is normal. Capping on it stranded versions at status=error.
        out = self._explain(
            knob="on",
            status="error",
            error_code="sha_error",
            qualification_attempts=_MAX_ERROR_REDRIVE_ATTEMPTS * 10,
            error_redrive_count=0,
        )
        assert out["auto_trigger"] != "capped"

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
        assert "lifetime_qualification_attempts" in out
        assert "error_retry_episode" in out
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

    def test_next_expected_trigger_for_same_candidate(self):
        out = self._explain(
            knob="on",
            status="not_promoted",
            blocker="criterion_coverage_incomplete",
            is_candidate_fully_met=False,
            candidate_sha="same",
            automain_sha="same",
        )
        assert out["decision"]["reason"] == "same_candidate"
        # The not-met verdict is frozen against its candidate sha, so the honest
        # next-expected unblock is NEW WORK landing (which re-grades), not the
        # stale verdict spontaneously flipping to met.
        assert out["next_expected_trigger"] == "new_candidate_commit"

    def test_next_expected_trigger_for_ci_red(self):
        out = self._explain(
            knob="on",
            status="dry_run",
            candidate_sha="old",
            automain_sha="new",
            automain_ci_state="red",
        )
        assert out["next_expected_trigger"] == "new_candidate_commit"

    def test_lifetime_attempts_and_error_episode_are_independent(self):
        out = self._explain(
            status="error",
            error_code="sha_error",
            qualification_attempts=11,
            error_redrive_count=0,
        )
        assert out["lifetime_qualification_attempts"] == 11
        assert out["error_retry_episode"] == {
            "count": 0,
            "cap": _MAX_ERROR_REDRIVE_ATTEMPTS,
        }
        assert out["auto_trigger"] != "capped"
        assert out["decision"]["fire"] is True

    def test_error_episode_rollover_uses_the_new_episode_count(self):
        out = self._explain(
            status="error",
            error_code="sha_error",
            qualification_attempts=12,
            error_redrive_count=1,
        )
        assert out["lifetime_qualification_attempts"] == 12
        assert out["error_retry_episode"]["count"] == 1
        assert out["auto_trigger"] != "capped"
        assert out["decision"]["fire"] is True


# ---------------------------------------------------------------------------
# T3b — infra_failure_redrive
# ---------------------------------------------------------------------------


class TestInfraFailureRedrive:
    def test_infra_failure_fires_when_below_cap(self):
        d = _resolve(
            status="infra_failure",
            consecutive_infra_errors=0,
        )
        assert d.fire
        assert d.trigger == "infra_failure_redrive"

    def test_infra_failure_fires_one_below_cap(self):
        d = _resolve(
            status="infra_failure",
            consecutive_infra_errors=_MAX_INFRA_FAILURE_REDRIVE_ATTEMPTS - 1,
        )
        assert d.fire
        assert d.trigger == "infra_failure_redrive"

    def test_infra_failure_blocked_at_cap(self):
        d = _resolve(
            status="infra_failure",
            consecutive_infra_errors=_MAX_INFRA_FAILURE_REDRIVE_ATTEMPTS,
        )
        assert not d.fire
        assert d.reason == "infra_failure_attempt_cap"
        assert str(_MAX_INFRA_FAILURE_REDRIVE_ATTEMPTS) in d.blocking[0]

    def test_infra_failure_blocked_above_cap(self):
        d = _resolve(
            status="infra_failure",
            consecutive_infra_errors=_MAX_INFRA_FAILURE_REDRIVE_ATTEMPTS + 5,
        )
        assert not d.fire
        assert d.reason == "infra_failure_attempt_cap"


# ---------------------------------------------------------------------------
# T5b — deferred_unsynced_redrive
# ---------------------------------------------------------------------------


class TestDeferredUnsyncedRedrive:
    def test_deferred_unsynced_fires_unconditionally(self):
        """The sync gate is inside run_qualification — the resolver always arms."""
        d = _resolve(status="deferred_unsynced")
        assert d.fire
        assert d.trigger == "deferred_unsynced_redrive"


# ---------------------------------------------------------------------------
# resolver-matches-every-automatic-redrive-state
# ---------------------------------------------------------------------------


# Every qualification status the sweep autonomously re-drives MUST have a
# matching rule in the resolver that returns fire=True under eligible
# conditions.  A missing status falls through to "not_applicable" (fire=False),
# which the explain-surface reports as "settled" — but the sweep is actively
# re-driving it, so the read surface lies to the operator.
_AUTOMATIC_REDRIVE_STATUSES: dict[str, dict] = {
    "deferred_unsynced": {
        "params": {},
        "trigger": "deferred_unsynced_redrive",
    },
    "error": {
        "params": {"error_code": "sha_error"},
        "trigger": "error_redrive",
    },
    "infra_failure": {
        "params": {},
        "trigger": "infra_failure_redrive",
    },
    "rubric_changed": {
        "params": {},
        "trigger": "rubric_redrive",
    },
    "rubric_provenance_unknown": {
        "params": {},
        "trigger": "rubric_redrive",
    },
    "not_promoted": {
        "params": {"blocker": "high_audit_finding", "candidate_sha": "old", "automain_sha": "new"},
        "trigger": "candidate_advanced",
    },
    "promotion_failed": {
        "params": {"requalification_ready": True},
        "trigger": "blockers_cleared",
    },
    "dry_run": {
        "params": {"candidate_sha": "old", "automain_sha": "new"},
        "trigger": "post_dry_run_advance",
    },
}


@pytest.mark.parametrize(
    "status, case",
    [(s, c) for s, c in _AUTOMATIC_REDRIVE_STATUSES.items()],
    ids=lambda val: val if isinstance(val, str) else "",
)
def test_resolver_matches_every_automatic_redrive_state(status, case):
    """The resolver MUST return fire=True for every status the sweep re-drives.

    Each entry in _AUTOMATIC_REDRIVE_STATUSES is a qualification status that
    the sweep_stuck_qualifications loops autonomously re-enter.  If any of
    these falls through to the default not_applicable rule, the operator and
    strategic read surfaces report the version as ``settled`` while the cycle
    is actually re-driving it — a correctness gap in the trigger truth.
    """
    d = _resolve(status=status, **case["params"])
    assert d.fire, (
        f"status={status!r}: resolver returned fire=False "
        f"(reason={d.reason}, trigger={d.trigger}) — "
        f"expected fire=True with trigger={case['trigger']!r}"
    )
    assert d.trigger == case["trigger"], (
        f"status={status!r}: expected trigger={case['trigger']!r}, "
        f"got trigger={d.trigger!r}"
    )


# ---------------------------------------------------------------------------
# explain — infra_failure and deferred_unsynced states
# ---------------------------------------------------------------------------


class TestExplainNewStates:
    def _explain(self, **kwargs):
        defaults = dict(
            phase="stabilize",
            status=None,
            error_code=None,
            qualification_attempts=0,
            error_redrive_count=0,
            is_candidate_fully_met=True,
            requalification_ready=None,
            candidate_sha=None,
            automain_sha=None,
            automain_ci_state=None,
            blocker=None,
            in_flight=False,
            promotion_check_wait=False,
            last_attempt=None,
            knob="on",
            evaluated_at="2026-07-16T00:00:00",
        )
        defaults.update(kwargs)
        return explain_qualification_trigger(**defaults)

    def test_infra_failure_armed(self):
        out = self._explain(status="infra_failure", consecutive_infra_errors=0)
        assert out["auto_trigger"] == "armed"
        assert out["decision"]["trigger"] == "infra_failure_redrive"

    def test_infra_failure_capped(self):
        out = self._explain(
            status="infra_failure",
            consecutive_infra_errors=_MAX_INFRA_FAILURE_REDRIVE_ATTEMPTS,
        )
        assert out["auto_trigger"] == "capped"
        assert out["decision"]["reason"] == "infra_failure_attempt_cap"

    def test_infra_failure_capped_respects_knob(self):
        # Knob dominates: observe/off modes suppress armed/capped labels.
        out = self._explain(
            status="infra_failure",
            consecutive_infra_errors=_MAX_INFRA_FAILURE_REDRIVE_ATTEMPTS,
            knob="observe",
        )
        assert out["auto_trigger"] == "observe"

    def test_deferred_unsynced_armed(self):
        out = self._explain(status="deferred_unsynced")
        assert out["auto_trigger"] == "armed"
        assert out["decision"]["trigger"] == "deferred_unsynced_redrive"

    def test_infra_failure_next_expected_when_capped(self):
        out = self._explain(
            status="infra_failure",
            consecutive_infra_errors=_MAX_INFRA_FAILURE_REDRIVE_ATTEMPTS,
        )
        assert out["next_expected_trigger"] == "operator_or_strategist_only"

    def test_deferred_unsynced_next_expected_is_sweep_retry(self):
        out = self._explain(status="deferred_unsynced")
        assert out["next_expected_trigger"] == "sweep_retry"
