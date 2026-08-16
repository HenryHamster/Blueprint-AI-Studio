"""Event-driven qualification trigger resolver (Phase 0/1 — observe mode).

Pure decision module: ``resolve_qualification_trigger`` is a DB-free, side-effect-free
function that maps the current qualification state of an open version to a fire/hold
decision.  ``explain_qualification_trigger`` wraps it with an operator-facing explanation
dict for use in the admin lifecycle endpoint, strategic payload, and attention surfacing.

The resolver implements the ordered rule table from the design doc (efbe4182):

    T0 hold      — phase/in-flight/promotion guards
    T1 initial fire — first-ever (unconditional), or any not_promoted verdict
                      whose evaluated candidate was superseded by automain.
                      Candidate identity, not the stale blocker, owns re-entry.
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

# Maximum number of automatic relaunches for a ``status=rubric_changed`` /
# ``rubric_provenance_unknown`` version.  Mirrors
# ``release_qualifier_redrive._MAX_RUBRIC_REDRIVE_ATTEMPTS``: a mid-flight
# rubric change invalidates the in-flight attempt, so a fresh grade is the
# recovery path — but a rubric that keeps changing must not spin attempts
# indefinitely.  The dedicated re-driver
# (``release_qualifier_redrive._redrive_rubric_changed_qualification``) enforces
# the cap; this resolver mirror keeps the read surfaces ("capped", not "armed")
# consistent with that enforcement.
_MAX_RUBRIC_REDRIVE_ATTEMPTS: int = 2

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
        "candidate_advanced", "post_dry_run_advance", "blockers_cleared".
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
    promotion_check_wait: bool,
    consecutive_infra_errors: int = 0,
    rubric_redrive_count: int = 0,
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
        iff there is at least one ACTIVE acceptance category, every one is
        ``met``, and no residual gap — an empty (never-authored or fully
        retracted) rubric is NOT fully met.  Used ONLY to gate the
        Legacy ledger signal retained for read-surface compatibility. A
        ``not_promoted`` verdict is immutable for its evaluated SHA, so this
        signal no longer authorizes a same-candidate re-grade.
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
    promotion_check_wait:
        True when Blueprint owns a managed promotion check/merge wait.
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
    if promotion_check_wait:
        return TriggerDecision(
            fire=False,
            trigger="none",
            reason="promotion_check_wait",
            blocking=["a promotion PR is in flight — qualification locked"],
        )

    # T1 — initial fire (coverage gate retired). The pre-qualification
    # criterion-coverage precondition is gone: the acceptance contract is
    # enforced INSIDE qualification (the sha-keyed ledger gate in
    # release_scoring.compute_composite). A never-attempted version fires
    # unconditionally; a version sitting at ``error`` falls through to the T2
    # redrive path (which keeps the transient/readiness distinction and the
    # attempt cap — bounding a persistently-failing candidate).
    if status is None:
        return TriggerDecision(
            fire=True,
            trigger="coverage_complete",
            reason="armed",
            blocking=[],
        )
    # T1 candidate invalidation. A not_promoted record is a verdict about exactly
    # ``candidate_sha``. Its blocker/findings/ledger rows cannot veto evaluation of
    # a DIFFERENT automain SHA; doing so carries stale candidate authority forward
    # and permanently wedges blockers such as prior_operator_authorship_nonzero.
    # Conversely, no mutable readiness/ledger signal may re-grade the SAME SHA:
    # that has no new information and the sweep would churn forever.
    if status == "not_promoted":
        if not candidate_sha or not automain_sha:
            return TriggerDecision(
                fire=False,
                trigger="none",
                reason="candidate_identity_unavailable",
                blocking=[
                    "cannot prove that automain advanced past the evaluated candidate"
                ],
            )
        if automain_sha == candidate_sha:
            return TriggerDecision(
                fire=False,
                trigger="none",
                reason="same_candidate",
                blocking=[
                    "not_promoted already evaluated the current automain SHA"
                ],
            )
        if automain_ci_state == _CI_RED:
            return TriggerDecision(
                fire=False,
                trigger="none",
                reason="ci_red",
                blocking=[
                    "automain advanced past the not_promoted candidate but CI "
                    "for the new SHA is red — suppressing auto-fire"
                ],
            )
        return TriggerDecision(
            fire=True,
            trigger="candidate_advanced",
            reason="armed",
            blocking=[],
        )

    # T2 — rubric_redrive: these attempt-scoped outcomes prove no mutable
    # contract, so a fresh snapshot/grade is always the recovery path.  The
    # dedicated re-driver enforces the cap; the resolver mirrors it so read
    # surfaces report "capped" instead of "armed" once the bound is hit (the
    # sweep's auto-trigger executor never fires these statuses — the bespoke
    # re-driver owns them).
    if status in {"rubric_changed", "rubric_provenance_unknown"}:
        if rubric_redrive_count >= _MAX_RUBRIC_REDRIVE_ATTEMPTS:
            return TriggerDecision(
                fire=False,
                trigger="none",
                reason="rubric_redrive_cap",
                blocking=[
                    f"rubric redrive cap reached ({rubric_redrive_count}/"
                    f"{_MAX_RUBRIC_REDRIVE_ATTEMPTS} relaunches) — "
                    "stabilize the rubric, then request a fresh qualification"
                ],
            )
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

    # T5 — blockers_cleared: promotion failure remains a remediation-owned
    # re-drive. not_promoted is handled above solely by candidate identity.
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
    promotion_check_wait: bool,
    last_attempt: dict[str, Any] | None,
    knob: str,
    evaluated_at: str,
    qualification_provenance: dict[str, Any] | None = None,
    consecutive_infra_errors: int = 0,
    rubric_redrive_count: int = 0,
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
        promotion_check_wait=promotion_check_wait,
        consecutive_infra_errors=consecutive_infra_errors,
        rubric_redrive_count=rubric_redrive_count,
    )

    # Map decision to auto_trigger label
    if knob == "off":
        auto_trigger = "off"
    elif knob == "observe":
        auto_trigger = "observe"
    elif in_flight:
        auto_trigger = "in_flight"
    elif decision.reason in (
        "error_attempt_cap",
        "infra_failure_attempt_cap",
        "rubric_redrive_cap",
    ):
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
        "promotion_check_wait": "operator_or_strategist_only",
        # A not-met grade is frozen against its candidate sha — the realistic
        # unblock is new work landing (which re-grades), not the stale verdict
        # spontaneously flipping.
        "candidate_identity_unavailable": "candidate_identity_available",
        "same_candidate": "new_candidate_commit",
        "error_attempt_cap": "operator_or_strategist_only",
        "infra_failure_attempt_cap": "operator_or_strategist_only",
        "rubric_redrive_cap": "operator_or_strategist_only",
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
        "rubric_redrive_episode": {
            "count": rubric_redrive_count,
            "cap": _MAX_RUBRIC_REDRIVE_ATTEMPTS,
        },
        "knob": knob,
        "evaluated_at": evaluated_at,
        "decision": decision.as_dict(),
    }
    if qualification_provenance is not None:
        out["qualification_provenance"] = qualification_provenance
    return out
