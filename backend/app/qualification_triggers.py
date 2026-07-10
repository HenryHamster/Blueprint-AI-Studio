"""Event-driven qualification trigger resolver (Phase 0/1 — observe mode).

Pure decision module: ``resolve_qualification_trigger`` is a DB-free, side-effect-free
function that maps the current qualification state of an open version to a fire/hold
decision.  ``explain_qualification_trigger`` wraps it with an operator-facing explanation
dict for use in the admin lifecycle endpoint, strategic payload, and attention surfacing.

The resolver implements the ordered rule table from the design doc (efbe4182):

    T0 hold      — phase/in-flight/promotion guards
    T1 coverage_complete — first-ever or error state + coverage now complete
    T2 error_redrive — transient/non-transient error under attempt cap
    T3 post_dry_run_advance — dry_run + automain has advanced (+ CI-red gate)
    T4 blockers_cleared — not_promoted readiness / promotion_failed / ci_unresolved

All paths call the same ``run_qualification`` chokepoint (which is self-defending via
a FOR-UPDATE lock); this module only decides *whether* to call it.
"""

from __future__ import annotations

from typing import Any


# Error codes considered transient and always eligible for T2 redrive.
_TRANSIENT_ERROR_CODES: frozenset[str] = frozenset({"sha_error", "no_credentials"})

# Maximum number of error-based redrives before the version is considered capped.
_MAX_ERROR_REDRIVE_ATTEMPTS: int = 5

# CI state string representing a known-red build (from ci_resolution.py).
_CI_RED = "red"


class TriggerDecision:
    """Result of ``resolve_qualification_trigger``.

    Attributes
    ----------
    fire:
        Whether a qualification run should be initiated.
    trigger:
        Which rule matched: "none" when fire=False.  One of
        "none", "coverage_complete", "error_redrive",
        "post_dry_run_advance", "blockers_cleared".
    reason:
        Machine-readable code for the decision (e.g. "ci_red", "in_flight",
        "wrong_phase", "coverage_incomplete", "error_attempt_cap", "armed",
        "not_applicable").
    blocking:
        Human-readable list of reasons why fire=False (empty when fire=True).
    """

    __slots__ = ("blocking", "fire", "reason", "trigger")

    def __init__(
        self,
        *,
        fire: bool,
        trigger: str,
        reason: str,
        blocking: list[str],
    ) -> None:
        self.fire = fire
        self.trigger = trigger
        self.reason = reason
        self.blocking = blocking

    def as_dict(self) -> dict[str, Any]:
        return {
            "fire": self.fire,
            "trigger": self.trigger,
            "reason": self.reason,
            "blocking": self.blocking,
        }


def resolve_qualification_trigger(
    *,
    phase: str,
    status: str | None,
    error_code: str | None,
    qualification_attempts: int,
    coverage: dict[str, int] | None,
    requalification_ready: bool | None,
    candidate_sha: str | None,
    automain_sha: str | None,
    automain_ci_state: str | None,
    blocker: str | None,
    in_flight: bool,
    promotion_in_flight: bool,
) -> TriggerDecision:
    """Return a fire/hold decision for the open version.

    This function is **pure** — no I/O, no side effects.  All external state
    (coverage counts, CI state, automain SHA) must be resolved by the caller
    and passed in.

    Parameters
    ----------
    phase:
        The version's release phase ("stabilize" is required for any trigger to fire).
    status:
        The last_qualification status string, or None when no attempt exists.
    error_code:
        The error code from last_qualification when status="error", else None.
    qualification_attempts:
        Total qualification attempt count recorded on the version.
    coverage:
        Dict with integer "total" and "covered" keys, or None when unavailable.
    requalification_ready:
        True when all gating not_promoted findings are ledger-resolved/stale;
        False or None when not.
    candidate_sha:
        The candidate SHA stored in last_qualification (the SHA that was evaluated).
    automain_sha:
        The current automain HEAD SHA (may be None when unavailable / not fetched).
    automain_ci_state:
        CI state string for the automain SHA ("red"/"green"/"pending"/"no_checks"/None).
        When "red" the T3 post_dry_run_advance trigger is suppressed.
    blocker:
        The blocker code from the last_qualification record, or None.
    in_flight:
        True when a qualification is currently pending/in_progress.
    promotion_in_flight:
        True when a promotion PR is open and auto-merging.
    """
    # T0 — Hard hold guards (mirror run_qualification's self-defending phase/lock guards).
    # These are informational; the guards in run_qualification remain the authority.
    if phase != "stabilize":
        return TriggerDecision(
            fire=False,
            trigger="none",
            reason="wrong_phase",
            blocking=[f"version is in phase '{phase}', not 'stabilize'"],
        )
    if in_flight:
        return TriggerDecision(
            fire=False,
            trigger="none",
            reason="in_flight",
            blocking=["a qualification is already pending or in progress"],
        )
    if promotion_in_flight:
        return TriggerDecision(
            fire=False,
            trigger="none",
            reason="promotion_in_flight",
            blocking=["a promotion PR is in flight — qualification locked"],
        )

    # Convenience helpers
    total = int((coverage or {}).get("total") or 0)
    covered = int((coverage or {}).get("covered") or 0)
    coverage_complete = not (total > 0 and covered < total)

    # T1 — coverage_complete: fire when coverage gate is met AND the version has
    # no conclusive verdict (never attempted, sitting at error, or not_promoted
    # with a coverage-incomplete blocker).
    _T1_ELIGIBLE_STATUSES = {None, "error"}
    _T1_NOT_PROMOTED_BLOCKER = "criterion_coverage_incomplete"
    is_t1_eligible = status in _T1_ELIGIBLE_STATUSES or (
        status == "not_promoted" and blocker == _T1_NOT_PROMOTED_BLOCKER
    )
    if is_t1_eligible and coverage_complete:
        return TriggerDecision(
            fire=True,
            trigger="coverage_complete",
            reason="armed",
            blocking=[],
        )
    # T1 blocked — coverage incomplete and no T2 fallback available.
    # When status="error", fall through to T2 even if coverage is incomplete,
    # because the error path may still be eligible for redrive.
    if is_t1_eligible and not coverage_complete and status != "error":
        return TriggerDecision(
            fire=False,
            trigger="none",
            reason="coverage_incomplete",
            blocking=[
                f"criterion coverage incomplete: {covered}/{total} active criteria covered"
            ],
        )

    # T2 — error_redrive: transient errors always eligible; non-transient errors
    # eligible when requalification_ready is not False (i.e. True or None — unknown).
    if status == "error":
        is_transient = error_code in _TRANSIENT_ERROR_CODES
        is_non_transient_ready = not is_transient and requalification_ready is not False
        if is_transient or is_non_transient_ready:
            if qualification_attempts >= _MAX_ERROR_REDRIVE_ATTEMPTS:
                return TriggerDecision(
                    fire=False,
                    trigger="none",
                    reason="error_attempt_cap",
                    blocking=[
                        f"error redrive cap reached ({qualification_attempts}/"
                        f"{_MAX_ERROR_REDRIVE_ATTEMPTS} attempts)"
                    ],
                )
            return TriggerDecision(
                fire=True,
                trigger="error_redrive",
                reason="armed",
                blocking=[],
            )

    # T3 — post_dry_run_advance: fire when the dry_run candidate SHA has been
    # superseded by a new automain HEAD.  Blocked when CI for the new SHA is
    # definitively red (i.e. automain_ci_state == "red").
    if (
        status == "dry_run"
        and automain_sha is not None
        and candidate_sha is not None
        and automain_sha != candidate_sha
    ):
        if automain_ci_state == _CI_RED:
            return TriggerDecision(
                fire=False,
                trigger="none",
                reason="ci_red",
                blocking=[
                    "automain has advanced past the dry_run candidate but "
                    "CI for the new SHA is red — suppressing auto-fire"
                ],
            )
        return TriggerDecision(
            fire=True,
            trigger="post_dry_run_advance",
            reason="armed",
            blocking=[],
        )

    # T4 — blockers_cleared: existing re-drive conditions surfaced through the
    # same decision shape for observability.  These mirror the existing bespoke
    # redrive functions and will remain as separate paths until Phase 1 folding.
    if status == "not_promoted" and blocker != _T1_NOT_PROMOTED_BLOCKER:
        if requalification_ready is True:
            return TriggerDecision(
                fire=True,
                trigger="blockers_cleared",
                reason="armed",
                blocking=[],
            )
        return TriggerDecision(
            fire=False,
            trigger="none",
            reason="findings_unresolved",
            blocking=["gating findings not yet resolved — requalification not ready"],
        )

    if status == "promotion_failed":
        if requalification_ready is True:
            return TriggerDecision(
                fire=True,
                trigger="blockers_cleared",
                reason="armed",
                blocking=[],
            )
        return TriggerDecision(
            fire=False,
            trigger="none",
            reason="findings_unresolved",
            blocking=["promotion_failed — awaiting remediation before re-drive"],
        )

    # Default: no applicable rule matched (e.g. promoted, abandoned, dry_run with
    # same SHA, or a status the resolver does not yet handle).
    return TriggerDecision(
        fire=False,
        trigger="none",
        reason="not_applicable",
        blocking=["no auto-trigger rule applies to the current version state"],
    )


def explain_qualification_trigger(
    *,
    phase: str,
    status: str | None,
    error_code: str | None,
    qualification_attempts: int,
    coverage: dict[str, int] | None,
    requalification_ready: bool | None,
    candidate_sha: str | None,
    automain_sha: str | None,
    automain_ci_state: str | None,
    blocker: str | None,
    in_flight: bool,
    promotion_in_flight: bool,
    last_attempt: dict[str, Any] | None,
    knob: str,
    evaluated_at: str,
) -> dict[str, Any]:
    """Return an operator-facing explanation of the qualification trigger state.

    This wraps ``resolve_qualification_trigger`` and formats the result per the
    design doc section 4 shape::

        {
          "auto_trigger": "armed|waiting|in_flight|capped|settled|observe|off",
          "last_attempt": {...},
          "next_expected_trigger": "<event>",
          "blocking": [...],
          "attempts": {"count": N, "cap": 5},
          "knob": "on|observe|off",
          "evaluated_at": "...",
          "decision": {...}   # resolver output for debugging
        }

    Parameters
    ----------
    knob:
        The runtime-config ``auto_qualification_triggers`` value.
    evaluated_at:
        ISO timestamp of when this explanation was computed.
    last_attempt:
        The raw last_qualification dict (or None), surfaced verbatim.
    All other parameters:
        Passed through to ``resolve_qualification_trigger``.
    """
    decision = resolve_qualification_trigger(
        phase=phase,
        status=status,
        error_code=error_code,
        qualification_attempts=qualification_attempts,
        coverage=coverage,
        requalification_ready=requalification_ready,
        candidate_sha=candidate_sha,
        automain_sha=automain_sha,
        automain_ci_state=automain_ci_state,
        blocker=blocker,
        in_flight=in_flight,
        promotion_in_flight=promotion_in_flight,
    )

    # Map decision to auto_trigger label
    if knob == "off":
        auto_trigger = "off"
    elif knob == "observe":
        auto_trigger = "observe"
    elif in_flight:
        auto_trigger = "in_flight"
    elif decision.reason == "error_attempt_cap":
        auto_trigger = "capped"
    elif decision.reason == "not_applicable":
        auto_trigger = "settled"
    elif decision.fire:
        auto_trigger = "armed"
    else:
        auto_trigger = "waiting"

    # Derive next_expected_trigger from the blocking reason
    reason_to_next: dict[str, str] = {
        "wrong_phase": "operator_or_strategist_only",
        "in_flight": "in_flight",
        "promotion_in_flight": "operator_or_strategist_only",
        "coverage_incomplete": "coverage_complete",
        "error_attempt_cap": "operator_or_strategist_only",
        "ci_red": "new_candidate_commit",
        "findings_unresolved": "findings_resolved",
        "not_applicable": "operator_or_strategist_only",
        "armed": "sweep_retry",  # fire=True means next sweep will fire
    }
    next_expected = reason_to_next.get(decision.reason, "operator_or_strategist_only")

    return {
        "auto_trigger": auto_trigger,
        "last_attempt": last_attempt,
        "next_expected_trigger": next_expected,
        "blocking": decision.blocking,
        "attempts": {
            "count": qualification_attempts,
            "cap": _MAX_ERROR_REDRIVE_ATTEMPTS,
        },
        "knob": knob,
        "evaluated_at": evaluated_at,
        "decision": decision.as_dict(),
    }
