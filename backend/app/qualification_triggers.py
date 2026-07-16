"""Event-driven qualification trigger resolver (Phase 0/1 — observe mode).

Pure decision module: ``resolve_qualification_trigger`` is a DB-free, side-effect-free
function that maps the current qualification state of an open version to a fire/hold
decision.  ``explain_qualification_trigger`` wraps it with an operator-facing explanation
dict for use in the admin lifecycle endpoint, strategic payload, and attention surfacing.

The resolver implements the ordered rule table from the design doc (efbe4182):

    T0 hold      — phase/in-flight/promotion guards
    T1 initial fire — first-ever (unconditional), or not_promoted whose only
                      blocker was the acceptance grade, once EITHER the ledger
                      reports the candidate fully met OR the candidate advanced
                      (new work to re-grade).  No pre-qualification coverage gate.
    T2 rubric_redrive — frozen rubric changed or could not be reconstructed
    T3 error_redrive — transient/non-transient error under attempt cap
    T3b infra_failure_redrive — transient infrastructure failure under per-candidate
                                streak cap (separate from error-redrive cap)
    T4 post_dry_run_advance — dry_run + automain has advanced (+ CI-red gate)
    T5 blockers_cleared — not_promoted readiness / promotion_failed / ci_unresolved
    T5b deferred_unsynced_redrive — deferred deploy-sync re-entry (unconditional;
                                   the sync gate is inside run_qualification)

All paths call the same ``run_qualification`` chokepoint (which is self-defending via
a FOR-UPDATE lock); this module only decides *whether* to call it.
"""

from __future__ import annotations

from typing import Any


# Error codes considered transient and always eligible for T2 redrive.
_TRANSIENT_ERROR_CODES: frozenset[str] = frozenset({"sha_error", "no_credentials"})

# Maximum number of error-based redrives before the version is considered capped.
_MAX_ERROR_REDRIVE_ATTEMPTS: int = 5

# Maximum number of sweep-driven re-entries for a ``status=infra_failure`` version.
# Mirrors ``release_qualifier_redrive._MAX_INFRA_FAILURE_REDRIVE_ATTEMPTS``:
# infra_failure is non-conclusive (records a transient infrastructure problem) and
# re-entering run_qualification is always the recovery path.  Cap at 3 so a
# persistently broken infra path does not retry forever.
_MAX_INFRA_FAILURE_REDRIVE_ATTEMPTS: int = 3

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
        "none", "coverage_complete", "rubric_redrive", "error_redrive",
        "infra_failure_redrive", "deferred_unsynced_redrive",
        "post_dry_run_advance", "blockers_cleared".
    reason:
        Machine-readable code for the decision (e.g. "ci_red", "in_flight",
        "wrong_phase", "criterion_not_met", "error_attempt_cap",
        "infra_failure_attempt_cap", "armed", "not_applicable").
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
    error_redrive_count: int,
    is_candidate_fully_met: bool,
    requalification_ready: bool | None,
    candidate_sha: str | None,
    automain_sha: str | None,
    automain_ci_state: str | None,
    blocker: str | None,
    in_flight: bool,
    promotion_in_flight: bool,
    consecutive_infra_errors: int = 0,
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
        Total LIFETIME qualification attempt count recorded on the version.  NOT
        used for the error cap (see ``error_redrive_count``) — kept for
        observability and any rule that legitimately wants the lifetime figure.
    error_redrive_count:
        The PER-ERROR-EPISODE redrive counter from
        ``last_qualification["error_redrive_count"]`` (reset when an error episode
        clears).  This is what T2's cap reads — capping on the lifetime counter is
        a known bug that strands a version at status=error forever.
    is_candidate_fully_met:
        The sha-keyed acceptance-ledger verdict for the last-evaluated candidate
        (from :func:`app.category_verdict_ledger.is_candidate_fully_met`): True
        iff every active acceptance category is ``met`` and no residual gap.
        Used ONLY to gate the ``not_promoted``/``criterion_coverage_incomplete``
        redrive leg — the initial/error T1 fire does not depend on it.
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
    consecutive_infra_errors:
        The per-candidate infra-failure streak from
        ``last_qualification["consecutive_infra_errors"]`` (reset when the
        candidate SHA changes or a non-infra verdict lands).  Read by the
        infra_failure_redrive tier against ``_MAX_INFRA_FAILURE_REDRIVE_ATTEMPTS``.
        Defaults to 0 so callers that don't resolve the streak still get a
        safe (fire-eligible) decision.
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

    # T1 — initial fire (coverage gate retired). The pre-qualification
    # criterion-coverage precondition is gone: the acceptance contract is
    # enforced INSIDE qualification (the sha-keyed ledger gate in
    # release_scoring.compute_composite). A never-attempted version fires
    # unconditionally; a version sitting at ``error`` falls through to the T2
    # redrive path (which keeps the transient/readiness distinction and the
    # attempt cap — bounding a persistently-failing candidate).
    _T1_NOT_PROMOTED_BLOCKERS = {"criterion_coverage_incomplete"}
    if status is None:
        return TriggerDecision(
            fire=True,
            trigger="coverage_complete",
            reason="armed",
            blocking=[],
        )
    # T1 redrive leg — a ``not_promoted`` version whose ONLY blocker was the
    # acceptance grade.  Two DISTINCT things can unblock it, and the ledger
    # verdict alone covers only the first:
    #
    #   (a) the ledger now reports the candidate fully met — e.g. a criterion was
    #       retracted/superseded (it leaves the ACTIVE set, so the remaining
    #       categories are all ``met``), or a re-grade flipped the last category.
    #
    #   (b) the CANDIDATE ADVANCED — new work landed since the not-met grade.
    #       This leg is load-bearing: ``CategoryVerdict`` rows are SHA-KEYED and
    #       nothing ever re-grades a PAST candidate, so the verdict for the
    #       already-graded sha is frozen at not-met forever.  T4 excludes this
    #       blocker and T3 only handles ``dry_run`` — so without (b) a version
    #       that graded not-met could NEVER re-qualify, and only a re-qualification
    #       can produce the verdict that would clear it.  That is a PERMANENT
    #       wedge (and the exact circularity the coverage gate died of).
    #
    # Firing on (b) is bounded by the SAME guards the other redrive legs use —
    # CI must not be definitively red, and gating audit findings must not be
    # unresolved — and by the fact that only a genuinely NEW candidate re-fires.
    # A re-grade of the SAME sha is suppressed: it carries no new information and
    # would spin (there is no cooldown in the sweep to catch it).
    if status == "not_promoted" and blocker in _T1_NOT_PROMOTED_BLOCKERS:
        if is_candidate_fully_met:
            return TriggerDecision(
                fire=True,
                trigger="coverage_complete",
                reason="armed",
                blocking=[],
            )
        candidate_superseded = (
            automain_sha is not None
            and candidate_sha is not None
            and automain_sha != candidate_sha
        )
        if candidate_superseded:
            if automain_ci_state == _CI_RED:
                return TriggerDecision(
                    fire=False,
                    trigger="none",
                    reason="ci_red",
                    blocking=[
                        "new work landed since the not-met acceptance grade but "
                        "CI for the new SHA is red — suppressing auto-fire"
                    ],
                )
            if requalification_ready is False:
                return TriggerDecision(
                    fire=False,
                    trigger="none",
                    reason="findings_unresolved",
                    blocking=[
                        "new work landed since the not-met acceptance grade but "
                        "gating findings are not yet resolved"
                    ],
                )
            return TriggerDecision(
                fire=True,
                trigger="candidate_advanced",
                reason="armed",
                blocking=[],
            )
        return TriggerDecision(
            fire=False,
            trigger="none",
            reason="criterion_not_met",
            blocking=[
                "acceptance criteria not yet met for this candidate per the "
                "category verdict ledger — awaiting new work to re-grade"
            ],
        )

    # T2 — rubric_redrive: these attempt-scoped outcomes prove no mutable
    # contract, so a fresh snapshot/grade is always the recovery path.
    if status in {"rubric_changed", "rubric_provenance_unknown"}:
        return TriggerDecision(
            fire=True,
            trigger="rubric_redrive",
            reason="armed",
            blocking=[],
        )

    # T3 — error_redrive: transient errors always eligible; non-transient errors
    # eligible when requalification_ready is not False (i.e. True or None — unknown).
    #
    # The cap reads ``error_redrive_count`` — the PER-ERROR-EPISODE counter carried
    # on ``last_qualification`` — NOT the lifetime ``qualification_attempts`` column.
    # This mirrors ``release_qualifier_redrive._redrive_error_qualification``, whose
    # docstring records that capping on the lifetime counter WAS A BUG: any version
    # with 5+ prior qualifications of ANY kind would be barred from every
    # transient-error re-drive, stranding it at status=error forever.  That bug is
    # newly reachable here — pre-retirement an errored version short-circuited at T1
    # whenever coverage was "complete" (always, for a 0-criteria version), so it
    # never met this cap.  Routing ``error`` to T2 exposes it, so the counter must
    # be the right one.
    if status == "error":
        is_transient = error_code in _TRANSIENT_ERROR_CODES
        is_non_transient_ready = not is_transient and requalification_ready is not False
        if is_transient or is_non_transient_ready:
            if error_redrive_count >= _MAX_ERROR_REDRIVE_ATTEMPTS:
                return TriggerDecision(
                    fire=False,
                    trigger="none",
                    reason="error_attempt_cap",
                    blocking=[
                        f"error redrive cap reached ({error_redrive_count}/"
                        f"{_MAX_ERROR_REDRIVE_ATTEMPTS} attempts)"
                    ],
                )
            return TriggerDecision(
                fire=True,
                trigger="error_redrive",
                reason="armed",
                blocking=[],
            )

    # T3b — infra_failure_redrive: transient infrastructure failures are
    # non-conclusive (child timeout, unavailable check, GitHub API unreachable)
    # and re-entering run_qualification on the current integration-branch HEAD
    # is the recovery path.  Bounded by a per-candidate streak cap (separate
    # from the error-redrive cap) so a persistently broken infra path does not
    # retry forever.  The streak is SHA-scoped: a new candidate resets it to 0,
    # so the cap trips only when the SAME candidate fails infra repeatedly.
    if status == "infra_failure":
        if consecutive_infra_errors >= _MAX_INFRA_FAILURE_REDRIVE_ATTEMPTS:
            return TriggerDecision(
                fire=False,
                trigger="none",
                reason="infra_failure_attempt_cap",
                blocking=[
                    f"infra failure redrive cap reached "
                    f"({consecutive_infra_errors}/"
                    f"{_MAX_INFRA_FAILURE_REDRIVE_ATTEMPTS} attempts on this "
                    f"candidate)"
                ],
            )
        return TriggerDecision(
            fire=True,
            trigger="infra_failure_redrive",
            reason="armed",
            blocking=[],
        )

    # T4 — post_dry_run_advance: fire when the dry_run candidate SHA has been
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

    # T5 — blockers_cleared: existing re-drive conditions surfaced through the
    # same decision shape for observability.  These mirror the existing bespoke
    # redrive functions and will remain as separate paths until Phase 1 folding.
    if status == "not_promoted" and blocker not in _T1_NOT_PROMOTED_BLOCKERS:
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

    # T5b — deferred_unsynced_redrive: the qualify-on-synced gate DEFERS a
    # candidate the promotion target has not yet absorbed (a main→integration
    # back-merge is still pending).  run_qualification re-snapshots the
    # integration-branch HEAD and re-evaluates the sync gate on every re-entry,
    # so a standing deferral is always eligible for re-entry — the real gate is
    # inside run_qualification, not here.  Unconditional fire (no cap needed:
    # each sweep costs one cheap run_qualification re-entry that either proceeds
    # or re-defers).
    if status == "deferred_unsynced":
        return TriggerDecision(
            fire=True,
            trigger="deferred_unsynced_redrive",
            reason="armed",
            blocking=[],
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
    error_redrive_count: int,
    is_candidate_fully_met: bool,
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
    qualification_provenance: dict[str, Any] | None = None,
    consecutive_infra_errors: int = 0,
) -> dict[str, Any]:
    """Return an operator-facing explanation of the qualification trigger state.

    This wraps ``resolve_qualification_trigger`` and formats the result per the
    design doc section 4 shape::

        {
          "auto_trigger": "armed|waiting|in_flight|capped|settled|observe|off",
          "last_attempt": {...},
          "next_expected_trigger": "<event>",
          "blocking": [...],
          "lifetime_qualification_attempts": N,
          "error_retry_episode": {"count": M, "cap": 5},
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
        error_redrive_count=error_redrive_count,
        is_candidate_fully_met=is_candidate_fully_met,
        requalification_ready=requalification_ready,
        candidate_sha=candidate_sha,
        automain_sha=automain_sha,
        automain_ci_state=automain_ci_state,
        blocker=blocker,
        in_flight=in_flight,
        promotion_in_flight=promotion_in_flight,
        consecutive_infra_errors=consecutive_infra_errors,
    )

    # Map decision to auto_trigger label
    if knob == "off":
        auto_trigger = "off"
    elif knob == "observe":
        auto_trigger = "observe"
    elif in_flight:
        auto_trigger = "in_flight"
    elif decision.reason in ("error_attempt_cap", "infra_failure_attempt_cap"):
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
        # A not-met grade is frozen against its candidate sha — the realistic
        # unblock is new work landing (which re-grades), not the stale verdict
        # spontaneously flipping.
        "criterion_not_met": "new_candidate_commit",
        "error_attempt_cap": "operator_or_strategist_only",
        "infra_failure_attempt_cap": "operator_or_strategist_only",
        "ci_red": "new_candidate_commit",
        "findings_unresolved": "findings_resolved",
        "not_applicable": "operator_or_strategist_only",
        "armed": "sweep_retry",  # fire=True means next sweep will fire
    }
    next_expected = reason_to_next.get(decision.reason, "operator_or_strategist_only")

    out = {
        "auto_trigger": auto_trigger,
        "last_attempt": last_attempt,
        "next_expected_trigger": next_expected,
        "blocking": decision.blocking,
        # These are deliberately separate: lifetime attempts are diagnostic
        # history, while the cap applies only to retries in this error episode.
        "lifetime_qualification_attempts": qualification_attempts,
        "error_retry_episode": {
            "count": error_redrive_count,
            "cap": _MAX_ERROR_REDRIVE_ATTEMPTS,
        },
        "knob": knob,
        "evaluated_at": evaluated_at,
        "decision": decision.as_dict(),
    }
    if qualification_provenance is not None:
        out["qualification_provenance"] = qualification_provenance
    return out
