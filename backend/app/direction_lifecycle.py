"""Direction lifecycle management for autonomous research.

Handles dead direction pruning, auto-recovery from auto-pause, and active
direction filtering (cooldown enforcement, auto-pause, completion detection).
Extracted from autonomous_research.py to create headroom in that module.
"""

import logging
import uuid
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.models import Project, TASK_ORIGINATION_RESEARCH, Task, TaskStatus
from app.direction_lineage_spend import (
    DirectionLineageSpend,
    load_direction_lineage_spend,
)
from app.direction_terminal_guard import mark_auto_direction_terminal
from app.payload_schemas import validate_internal_payload
from app.shared_state import (
    runtime_config as _runtime_config,
)
from app.direction_refractory import get_direction_refractory_info, resolve_direction_refractory_minutes
from app.strategic_completion import _is_code_failure_pause_reason
from app.time_utils import _utcnow
from app.strategic_authority_locks import commit_legacy_authorized

logger = logging.getLogger(__name__)

_MAX_AUTO_PAUSE_CYCLES = 3

# Zombie-direction churn ladder (4th ladder, ref knowledge bf6d8788; redesign
# d-73f03cc0). A direction the health guardrail keeps churn/unproductive/
# saturated-pausing (Rules 1, 2, 5) accumulates churn_family_pause_count; once it
# has been paused this many times for a churn-family reason it crosses the
# retirement threshold. This NO LONGER auto-retires the direction — it records a
# flagged, strategist-reviewable retirement RECOMMENDATION and keeps the
# direction paused/non-terminal. Terminal retirement requires an explicit
# strategist/operator retire action.
_MAX_CHURN_PAUSE_CYCLES = 2

# Churn-retirement DECISION WINDOW (dud-culling fence).
#
# The recommendation above holds the direction paused and non-terminal pending an
# explicit keep/retire decision. Nothing bounded that hold: a recommended
# direction is explicitly skipped by BOTH short clocks in this module — the 72h
# needs_manual_intervention TTL fallback and the 48h auto-recovery cooldown — and
# the ladder counter is frozen while it is held. With no decider, the direction
# stayed paused forever, kept its gap staffing, and re-surfaced in every
# strategist payload: nursed, never culled. That is the exact failure the
# lifetime-burn ladder already fixed for itself ("eventually auto-retires with
# zero operator action instead of thrashing in pause/extend forever"); the churn
# ladder was the only one left without a terminal clock.
#
# Window derivation (NOT an arbitrary number) — two independent in-repo anchors
# that agree on 7 days:
#   1. Guaranteed strategist opportunities. The recommendation + its bounded
#      evidence are surfaced into the strategist payload on every strategic cycle
#      (strategic_spawning, direction stat_entry), and a strategic cycle is
#      guaranteed to run at least once per `strategic_max_interval_minutes`
#      (default 1440 = 24h) by the wall-clock backstop in autonomous_research.
#      Seven days is therefore SEVEN guaranteed looks at a one-question decision.
#   2. This module's existing "a signal this old is presumed dead" horizon,
#      _REMEDIATION_MONITOR_EXPIRY_DAYS (7 days), below.
#
# After that many guaranteed opportunities, the absence of a decision IS the
# decision, and the ladder's OWN recommendation (retire) is applied. The window is
# deliberately far longer than either short clock so this fence can never be
# confused with them: it does not clear flags or reactivate by time alone (the
# behaviour those guards correctly reject) — it records a decision, with
# machine-readable evidence, through the same two-tick designated-retire gate the
# lifetime-burn ladder uses. Any sanctioned reactivation (operator PATCH /
# strategist KEEP) clears auto_retire_pending via
# _clear_direction_reactivation_state, so a decision made during the final tick
# still wins.
_CHURN_RECOMMENDATION_DECISION_DAYS = 7
# auto_retire_pending_reason written when the window above expires undecided.
CHURN_UNDECIDED_RETIRE_REASON = "churn_recommendation_undecided"

# Lifetime-burn auto-retire ladder (5th ladder). A direction that repeatedly
# trips the lifetime burn backstop (Rule 6) accumulates lifetime_burn_pause_count.
# Once it reaches this threshold it is auto-retired instead of left open for
# indefinite operator intervention — the burn pattern is structural and the slot
# can be reused. The burn-pause notification fires before retirement on each cycle.
_MAX_LIFETIME_BURN_PAUSE_CYCLES = 2
_ZOMBIE_FAMILY_PREFIXES = (
    "Unproductive:",
    "Churn guardrail:",
    "Saturated:",
)

# Remediation monitor window: if a merged remediation happened longer ago than this,
# a new failure is presumed unrelated and the stale window is cleared instead of
# triggering escalation.
_REMEDIATION_MONITOR_EXPIRY_DAYS = 7

# Max time an investigation task may hold diagnosis_in_progress before flags are considered stale.
# Investigations typically finish in minutes; 4h is a safety ceiling to recover from reaped/timed-out tasks.
_DIAGNOSIS_STALE_HOURS = 4

# Budget-pause evidence basis used when the lineage-spend query finds no task
# rows for a direction (e.g. archived/pruned lineage). The research-history +
# history_aggregate sum is an EXPLICIT labelled fallback — it is never silently
# conflated with the canonical lifetime lineage meter the badge reads, so an
# operator can always tell which dollars a pause was computed from.
_BUDGET_BASIS_RESEARCH_HISTORY = "research_history+history_aggregate"

# Recent failed tasks to scan when computing the dominant error fingerprint
_FINGERPRINT_SAMPLE_SIZE = 10
# Min matching failures required for a fingerprint to count as "dominant" for the direction
_FINGERPRINT_MIN_RECURRENCE = 2
# Only consider failures from within this window so stale patterns don't dominate
# after the current failure mode has shifted. Falls back to the unfiltered query
# when fewer than _FINGERPRINT_MIN_RECURRENCE recent failures exist.
_FINGERPRINT_RECENCY_DAYS = 7

# Max retries for a BLOCKER escalation notification whose initial delivery failed.
# After this many failed retries, the pending flag is cleared and an ERROR is logged
# with structured fields so monitoring can catch a silently-stuck direction.
_MAX_ESCALATION_NOTIFICATION_RETRIES = 3

# Minimum fractional drop in CODE_ERROR failure rate (pre- vs post-remediation)
# that counts as partial success rather than a failed fix. 0.5 == 50% reduction.
_PARTIAL_SUCCESS_RATE_DROP = 0.5

# TTL fallback for clearing needs_manual_intervention when no remediation monitor
# ever started. The monitor only starts on a successful remediation merge; if
# remediation keeps failing, the flag (and its operator alert) would otherwise
# persist indefinitely.
_STUCK_INTERVENTION_TTL_HOURS = 72

_OUTCOME_DETECTION_KEYS = (
    "outcome_progress_history",
    "outcome_progress_ever_met",
    "evidence_progress",
    "stall_advisory",
    "possibly_complete",
    "completion_suggestion",
    "outcome_scaffolding_flagged",
    "outcome_scaffolding_flagged_at",
    "outcome_scaffolding_flagged_cycles",
)


def _direction_budget_usd(direction: dict) -> float | None:
    """Positive float envelope for a direction, or None when not configured.

    Mirrors the budget gate's parsing so the batched lineage query covers
    exactly the directions the gate could pause. A zero/negative/unparseable
    ``cost_budget_usd`` means no envelope and needs no spend query.
    """
    budget = direction.get("cost_budget_usd")
    if budget is None:
        return None
    try:
        budget_f = float(budget)
    except (TypeError, ValueError):
        return None
    return budget_f if budget_f and budget_f > 0 else None


def _reset_outcome_detection_state(d: dict) -> None:
    """Clear per-direction outcome-detection state after a contract field changes.

    Called whenever text, paths, completion_criteria, success_evidence, or
    retirement_condition is rewritten so the next research cycle measures
    against the new contract rather than poisoned snapshots from the old one.
    """
    d["outcome_progress_history"] = []
    for key in _OUTCOME_DETECTION_KEYS[1:]:
        d.pop(key, None)


def _clear_direction_reactivation_state(direction: dict) -> None:
    """Drop transient status residue so active/staged transitions start clean.

    Import-safe home for the residue-clear used by the operator PATCH and
    strategist HTTP/fallback reactivation surfaces (via
    :func:`reactivate_paused_direction`). Kept as pure dict mutation so it can
    live in this module without pulling in strategic_completion (which imports
    this module lazily). NOTE: cooldown auto-recovery intentionally does NOT use
    this full clear — it preserves ``last_researched_at`` (research-cooldown
    eligibility) and ``remediation_attempts`` (escalation ladder), popping only
    the pause markers directly.
    """
    for key in (
        "paused_reason",
        "paused_at",
        "auto_paused",
        "expired_at",
        "needs_manual_intervention",
        "needs_manual_intervention_at",
        "escalation_notification_pending",
        "escalation_notification_retries",
        "last_researched_at",
        "staged_activated_at",
        "staged_for_version",
        # Cancel any pending auto-retirement: operator reactivation / resurrection
        # must not be silently overridden by the designated retire pass on the
        # very next lifecycle tick (the d-21efb120 incident class).
        "auto_retire_pending",
        "auto_retire_pending_reason",
        "auto_retire_pending_at",
        # Clear the churn-retirement recommendation: a sanctioned reactivation
        # (operator PATCH / strategist reactivate) is the explicit keep decision
        # the hold was waiting for, so the recommendation must not survive it and
        # silently re-hold the direction.
        "churn_retirement_recommended",
        "churn_retirement_recommended_reason",
        "churn_retirement_recommended_at",
    ):
        direction.pop(key, None)
    direction["remediation_attempts"] = 0


def reactivate_paused_direction(direction: dict, *, allow_user_paused: bool) -> bool:
    """Transition a paused/staged/proposed direction back to ``active`` cleanly.

    Centralizes the paused→active transition so every accepted reactivation of
    an auto-paused direction gets the same lifecycle grace that cooldown
    auto-recovery relies on:

    * Enforces the user-paused block: when the prior status is ``paused`` and
      the direction was NOT auto-paused, a reactivation is only allowed when
      ``allow_user_paused`` is True (operators may resume their own paused
      directions; the autonomous strategist may not). Returns ``False`` WITHOUT
      mutating the direction so the caller can emit its own block response.
    * Clears pause/intervention residue via
      :func:`_clear_direction_reactivation_state` so no stale ``auto_paused`` /
      ``paused_at`` / intervention flags survive the transition.
    * Sets ``status`` to ``active``.
    * Stamps ``auto_recovered_at`` — the grace marker consumed by
      ``_check_direction_health`` — ONLY when the prior status was ``paused``.
      This is what prevents the frozen ``research_history`` that originally
      triggered the pause from immediately re-pausing or zombie-retiring the
      direction before a new research cycle is appended. Ordinary
      proposed/staged→active activation is left grace-free.

    Counters that must survive a reactivation (``churn_family_pause_count``,
    ``lifetime_burn_pause_count``, ``code_failure_pause_count``) are NOT touched.

    Returns ``True`` when the direction was reactivated, ``False`` when the
    user-paused block prevented it.
    """
    prior_status = direction.get("status")
    if (
        prior_status == "paused"
        and not direction.get("auto_paused")
        and not allow_user_paused
    ):
        return False
    _clear_direction_reactivation_state(direction)
    direction["status"] = "active"
    if prior_status == "paused":
        # Grace period: stamp recovery time so _check_direction_health skips
        # history-based health rules until at least one new cycle is appended.
        direction["auto_recovered_at"] = _utcnow().isoformat()
    return True


# Auto-retire ladder counters reset when a direction is resurrected. Keeping the
# tuple next to the ladder constants above makes the reset<->accumulate pairing
# explicit: every counter incremented by _process_direction_lifecycle is zeroed
# here on the sanctioned undo path.
_AUTO_RETIRE_LADDER_COUNTERS = (
    "churn_family_pause_count",
    "lifetime_burn_pause_count",
    "code_failure_pause_count",
    "auto_pause_count",
)
_AUTO_RETIRE_LADDER_EVIDENCE = (
    "churn_pause_evidence",
    "lifetime_burn_pause_evidence",
)
# Terminal residue stamped by the auto-retire ladder / lifetime-burn retire path.
# auto_retire_pending* are included here so a direction resurrected from a
# DirectionEvent snapshot (which may have been snapshotted while the flag was
# set) does not carry the pending-retire into its new life and get re-retired on
# the very next designated-retire pass.
_TERMINAL_RESIDUE_KEYS = (
    "auto_completed",
    "auto_completed_reason",
    "completed_at",
    "auto_retire_pending",
    "auto_retire_pending_reason",
    "auto_retire_pending_at",
    # Churn-retirement recommendation residue: a direction resurrected from a
    # DirectionEvent snapshot (possibly captured while the recommendation was
    # set) must not carry the flag into its new life and immediately re-hold /
    # re-recommend. resurrect_direction also resets the churn ladder counters, so
    # the resurrected direction leaves with the recommendation gone AND the
    # ladder at zero.
    "churn_retirement_recommended",
    "churn_retirement_recommended_reason",
    "churn_retirement_recommended_at",
    # The decision record of a PREVIOUS churn-undecided retirement. A resurrected
    # direction is starting a new life with the ladder counters zeroed, so a stale
    # "this was culled for going undecided" record must not travel with it.
    "churn_retirement_decision",
)
# Cap on the audit-note rationale so a resurrected direction can't bloat the
# directions JSONB with an unbounded operator string.
_RESURRECTION_RATIONALE_MAX = 512
_RESURRECTION_NOTES_MAX = 50


def resurrect_direction(
    direction: dict,
    *,
    now_iso: str | None = None,
    actor: str | None = None,
    rationale: str | None = None,
) -> dict:
    """Mutate ``direction`` into a CLEAN, resurrectable active direction.

    Sanctioned undo for a wrongly auto-retired direction (e.g. d-21efb120). A raw
    completed->active status flip re-retires almost immediately because the
    auto-retire ladder counters stay primed, terminal residue stays stamped, and
    the health rules re-fire on the very next lifecycle tick. This primitive fixes
    all of that in one place:

      * ``status`` -> ``"active"``
      * drops transient pause/intervention residue (via
        :func:`_clear_direction_reactivation_state`)
      * resets EVERY auto-retire ladder counter to 0 and drops the counters'
        evidence blobs
      * strips terminal residue (``auto_completed`` / ``auto_completed_reason`` /
        ``completed_at``)
      * grants ONE grace window by stamping ``auto_recovered_at`` — reusing the
        EXISTING grace machinery: ``strategic_completion._check_direction_health``
        skips the history-based pause rules while ``auto_recovered_at`` is set and
        no ``research_history`` entry post-dates it, and the next re-pause consumes
        the grace (``direction_lifecycle`` pops ``auto_recovered_at``). This
        guarantees one clean cycle before the direction re-enters the ladder from
        zero rather than re-retiring on the next tick.

    Also stamps ``resurrected_at`` and appends an audit note recording who brought
    the direction back and why. Gap re-staffing is the caller's responsibility (it
    needs the problem_model, which lives outside the direction dict).

    Returns the same ``direction`` object (mutated in place) for convenience.
    """
    now = now_iso or _utcnow().isoformat()
    notes = direction.get("resurrection_notes")
    if direction.get("status") == "active" and isinstance(notes, list):
        last_note = notes[-1] if notes else None
        if (
            isinstance(last_note, dict)
            and last_note.get("action") == "resurrected"
            and last_note.get("actor") == (actor or "operator")[:64]
            and last_note.get("rationale") == (str(rationale)[:_RESURRECTION_RATIONALE_MAX] if rationale else None)
        ):
            return direction

    direction["status"] = "active"

    _clear_direction_reactivation_state(direction)

    for counter in _AUTO_RETIRE_LADDER_COUNTERS:
        direction[counter] = 0
    for evidence in _AUTO_RETIRE_LADDER_EVIDENCE:
        direction.pop(evidence, None)

    for key in _TERMINAL_RESIDUE_KEYS:
        direction.pop(key, None)

    # Reset outcome-detection state: a resurrected direction's stale completion
    # snapshots must not immediately re-flag it complete.
    _reset_outcome_detection_state(direction)

    # Grant one clean grace window (existing machinery — see docstring).
    direction["auto_recovered_at"] = now
    direction["resurrected_at"] = now

    note = {
        "at": now,
        "actor": (actor or "operator")[:64],
        "action": "resurrected",
    }
    if rationale:
        note["rationale"] = str(rationale)[:_RESURRECTION_RATIONALE_MAX]
    notes = direction.get("resurrection_notes")
    if not isinstance(notes, list):
        notes = []
    notes.append(note)
    direction["resurrection_notes"] = notes[-_RESURRECTION_NOTES_MAX:]

    return direction


# Fields stamped by the churn/zombie retire-RECOMMENDATION ladder. Both keep and
# retire decisions clear them (KEEP via _clear_direction_reactivation_state, which
# lists the same keys; RETIRE explicitly below), so a decided direction never
# carries a stale recommendation that would silently re-hold it.
_CHURN_RETIREMENT_RECOMMENDATION_KEYS = (
    "churn_retirement_recommended",
    "churn_retirement_recommended_reason",
    "churn_retirement_recommended_at",
)


def direction_has_churn_retirement_recommendation(direction: dict) -> bool:
    """True when a direction is held paused pending an explicit keep/retire decision."""
    return isinstance(direction, dict) and bool(
        direction.get("churn_retirement_recommended")
    )


def apply_churn_retirement_keep(direction: dict, *, allow_user_paused: bool) -> bool:
    """KEEP decision on a churn-retirement-recommended direction.

    Reactivates the held-paused direction back to ``active`` via
    :func:`reactivate_paused_direction` — which clears the recommendation fields
    and pause/intervention residue (through
    :func:`_clear_direction_reactivation_state`) and stamps the
    ``auto_recovered_at`` grace marker so frozen ``research_history`` cannot
    immediately re-pause it — and then gives the churn ladder a GENUINE fresh
    start by zeroing ``churn_family_pause_count`` and dropping its evidence blob,
    so the same zombie ladder does not immediately re-recommend retirement on the
    next pause.

    Returns ``False`` WITHOUT mutating when the user-paused block applies
    (mirrors :func:`reactivate_paused_direction`), so the caller can emit its own
    block response and the user-paused reactivation guard is never weakened.
    """
    if not reactivate_paused_direction(direction, allow_user_paused=allow_user_paused):
        return False
    # Fresh start scoped to the churn family — the ladder that produced the
    # recommendation. Other ladder counters are intentionally left untouched.
    direction["churn_family_pause_count"] = 0
    direction.pop("churn_pause_evidence", None)
    return True


def _churn_recommendation_decision_overdue(direction: dict) -> dict | None:
    """Machine-readable decision record when the churn hold has gone unanswered.

    Returns ``None`` while the direction is still inside its decision window (or
    when the recommendation carries no usable ``churn_retirement_recommended_at``
    stamp — an unstamped recommendation is never culled on a guess; it keeps the
    old indefinite-hold behaviour until a decision or a re-recommendation stamps
    it).

    When the window HAS elapsed, returns the full ``churn_retirement_decision``
    blob that is persisted onto the direction, so an operator reading the retired
    direction never has to re-derive why it was culled: it carries the decision,
    who/what made it, when the recommendation was raised, how long it went
    unanswered, the window and its unit of derivation (guaranteed strategist
    opportunities), and the ladder evidence summary that produced the
    recommendation in the first place.
    """
    if not direction.get("churn_retirement_recommended"):
        return None
    recommended_at = direction.get("churn_retirement_recommended_at")
    if not recommended_at:
        return None
    try:
        recommended_dt = datetime.fromisoformat(str(recommended_at))
    except (TypeError, ValueError):
        logger.warning(
            "[LIFECYCLE] Direction %s has malformed churn_retirement_recommended_at "
            "%r; leaving the recommendation held rather than culling on a guess",
            direction.get("id"),
            recommended_at,
        )
        return None
    window_days = _CHURN_RECOMMENDATION_DECISION_DAYS
    undecided_hours = (_utcnow() - recommended_dt).total_seconds() / 3600
    if undecided_hours < window_days * 24:
        return None
    evidence = direction.get("churn_retirement_evidence")
    return {
        "decision": "retired",
        "decided_by": "decision_window_expiry",
        "reason_code": CHURN_UNDECIDED_RETIRE_REASON,
        "recommended_at": str(recommended_at),
        "decided_at": _utcnow().isoformat(),
        "window_days": window_days,
        "undecided_hours": round(undecided_hours, 1),
        # The window's unit of derivation: a strategic cycle is guaranteed at
        # least once per strategic_max_interval_minutes (default 24h), and the
        # recommendation is in every strategic payload, so this is the number of
        # guaranteed opportunities the strategist had to answer.
        "strategist_opportunities": int(undecided_hours // 24),
        "churn_family_pause_count": int(
            direction.get("churn_family_pause_count", 0) or 0
        ),
        "recommendation_summary": (
            evidence.get("summary") if isinstance(evidence, dict) else None
        )
        or direction.get("churn_retirement_recommended_reason"),
    }


def apply_churn_retirement_retire(
    direction: dict, *, reason: str, now_iso: str | None = None
) -> None:
    """RETIRE decision on a churn-retirement-recommended direction.

    Confirms the terminal outcome the recommendation was proposing: marks the
    direction retired with the explicit ``stopped`` outcome and a reason
    attributing the retirement to an explicit keep/retire decision, then clears the recommendation
    fields plus the manual-hold flags the recommendation set (retirement
    supersedes them — mirrors :func:`_retire_lifetime_burn`). Gap reconciliation
    and retired-direction memory are the caller's responsibility (they need the
    problem_model / vision, which live outside the direction dict).
    """
    now = now_iso or _utcnow().isoformat()
    mark_auto_direction_terminal(
        direction,
        outcome="stopped",
        reason=reason,
        completed_at=now,
    )
    for key in _CHURN_RETIREMENT_RECOMMENDATION_KEYS:
        direction.pop(key, None)
    direction.pop("needs_manual_intervention", None)
    direction.pop("needs_manual_intervention_at", None)


def _detect_partial_remediation_success(direction: dict) -> bool | None:
    """Return True if post-remediation code-failure rate is materially lower than pre-remediation.

    Returns None when research_history lacks entries on either side of
    ``last_remediation_at`` — the data is insufficient to decide, which
    the caller must distinguish from a confirmed failure (False) so a
    newly-created direction or one whose cycles haven't completed yet is
    not prematurely escalated to manual intervention.
    """
    last_rem_at = direction.get("last_remediation_at")
    if not last_rem_at:
        # No remediation recorded — nothing to evaluate; proceed to escalation.
        return False
    try:
        boundary = datetime.fromisoformat(last_rem_at)
    except (ValueError, TypeError):
        return False
    pre: list[dict] = []
    post: list[dict] = []
    for entry in direction.get("research_history") or []:
        ts = entry.get("timestamp")
        if not ts:
            continue
        try:
            entry_at = datetime.fromisoformat(ts)
        except (ValueError, TypeError):
            continue
        (post if entry_at >= boundary else pre).append(entry)
    if not pre or not post:
        return None

    def _code_failure_rate(entries: list[dict]) -> float:
        created = sum(e.get("tasks_created", 0) for e in entries)
        if created <= 0:
            return 0.0
        failed = sum(e.get("tasks_failed", 0) for e in entries)
        infra = sum(e.get("tasks_failed_infra", 0) for e in entries)
        return max(0, failed - infra) / created

    pre_rate = _code_failure_rate(pre[-3:])
    post_rate = _code_failure_rate(post)
    if pre_rate <= 0:
        return None
    return post_rate <= pre_rate * (1.0 - _PARTIAL_SUCCESS_RATE_DROP)


def _build_escalation_args(direction: dict) -> dict:
    """Assemble kwargs for post_remediation_escalation_message from direction state.

    Shared by the initial auto-pause escalation path and the retry loop so both
    produce identical message bodies when re-derived from the direction.
    """
    from app.strategic_completion import _build_direction_failure_summary

    summary = _build_direction_failure_summary(direction)
    rem_summary = (
        f"cycles_analyzed={summary['cycles_analyzed']} "
        f"tasks_created={summary['tasks_created']} "
        f"code_failed={summary['tasks_failed_code']} "
        f"merged={summary['prs_merged']} "
        f"code_failure_rate={summary['code_failure_rate']}"
    )
    did = direction.get("id")
    return {
        "direction_text": direction.get("text") or (str(did) if did else ""),
        "failure_pattern": (
            direction.get("error_fingerprint")
            or direction.get("paused_reason")
            or "(unknown)"
        ),
        "attempts": int(direction.get("remediation_attempts", 0)),
        "remediation_summary": rem_summary,
    }


async def _compute_direction_error_fingerprint(
    project_id: uuid.UUID,
    direction_id: str,
    session: AsyncSession,
) -> str | None:
    """Return the dominant error fingerprint across this direction's recent failed tasks.

    Pulls the most recent ``_FINGERPRINT_SAMPLE_SIZE`` failures for ``direction_id``
    within the last ``_FINGERPRINT_RECENCY_DAYS`` days and uses
    ``count_matching_fingerprints`` to find the fingerprint that recurs most,
    returning it only if it matches at least ``_FINGERPRINT_MIN_RECURRENCE``
    failures. Falls back to the unfiltered query when too few failures exist
    inside the window so low-volume directions still get a fingerprint. The
    result is stored on the direction so operators see the specific error
    pattern and the escalation path can compare it against the last
    remediation's fingerprint.
    """
    from app.anomaly_detection import (
        _task_error_fingerprint,
        count_matching_fingerprints,
    )

    base_query = (
        select(Task)
        .where(
            Task.project_id == project_id,
            Task.status == TaskStatus.FAILED,
            Task.context["direction_id"].as_string() == direction_id,
        )
        .order_by(Task.created_at.desc())
        .limit(_FINGERPRINT_SAMPLE_SIZE)
    )
    window_start = _utcnow() - timedelta(days=_FINGERPRINT_RECENCY_DAYS)
    failed_tasks = (
        (await session.execute(base_query.where(Task.created_at >= window_start)))
        .scalars()
        .all()
    )
    if len(failed_tasks) < _FINGERPRINT_MIN_RECURRENCE:
        failed_tasks = (await session.execute(base_query)).scalars().all()
    best_fp: str | None = None
    best_count = 0
    seen: set[str] = set()
    for t in failed_tasks:
        fp = _task_error_fingerprint(t)
        if not fp or fp in seen:
            continue
        seen.add(fp)
        count = count_matching_fingerprints(fp, failed_tasks)
        if count > best_count:
            best_fp, best_count = fp, count
    return best_fp if best_count >= _FINGERPRINT_MIN_RECURRENCE else None


def _synthesize_outcome_scaffolding_memo(
    direction_id: str,
    direction_topic: str,
    scaffolding_ev: dict,
) -> dict:
    """Build a memo dict from scaffolding evidence available at spawn time.

    All fields come from structured evidence the backend already holds,
    so no model call is needed — the result is substantive and citable.
    """
    total_cycles = scaffolding_ev.get("total_cycles", 0)
    lifetime_merged = scaffolding_ev.get("lifetime_merged", 0)
    merge_rate = scaffolding_ev.get("lifetime_merge_rate", 0.0)
    stall_cycles = scaffolding_ev.get("stall_run_cycles", 0)

    summary = (
        f"Direction '{direction_topic[:120]}' has shipped {lifetime_merged} merged PRs "
        f"across {total_cycles} research cycles ({merge_rate:.0%} merge rate) but its "
        f"outcome signal has stalled for {stall_cycles} consecutive cycles."
    )
    parts = [
        f"Direction: {direction_topic}",
        (
            f"Scaffolding evidence: {total_cycles} total cycles, "
            f"{lifetime_merged} lifetime merged PRs, "
            f"{merge_rate:.1%} lifetime merge rate, "
            f"{stall_cycles} consecutive stall cycles without outcome advancement."
        ),
        (
            "Assessment: The direction is generating consistent engineering activity "
            "(merges) but not advancing its declared success evidence. "
            "This indicates outcome-less scaffolding — work that builds structure "
            "without demonstrating measurable progress toward the stated goal."
        ),
    ]
    return {
        "summary": summary[:500],
        "reasoning": "\n\n".join(parts),
        "citations": [f"direction:{direction_id}"],
        "tags": ["outcome-scaffolding"],
    }


async def _directed_research_recently_completed(
    project_id, direction_id: str, session, direction: dict | None = None
) -> bool:
    """True when a directed research task for this direction COMPLETED within the
    cooldown window.

    Crash guard: protects against re-commissioning when ``_handle_research_completion``
    dies before it can update ``refractory_until`` on the direction — the refractory
    gate then sees no hold and allows a re-spawn (incident 2026-06-10).

    Mirrors ``_undirected_research_recently_active`` for the directed case; the
    undirected guard already covers all statuses, so this companion only checks
    COMPLETED. Set ``completed_research_cooldown_minutes`` <= 0 to disable.

    Absent an explicit ``completed_research_cooldown_minutes`` value the window
    derives from the direction's effective refractory window (via
    ``resolve_direction_refractory_minutes``) when a direction dict is supplied,
    or falls back to ``autonomous_direction_cooldown_minutes`` (default 240) —
    matching the refractory gate's fallback so both gates agree on the cooldown window.
    """
    from app.routers.admin_analytics import _derive_completed_at

    explicit = _runtime_config.get("completed_research_cooldown_minutes")
    if isinstance(explicit, (int, float)) and not isinstance(explicit, bool):
        cooldown_minutes = float(explicit)
    elif direction is not None:
        cooldown_minutes = resolve_direction_refractory_minutes(direction)
    else:
        base = _runtime_config.get("autonomous_direction_cooldown_minutes", 240)
        if isinstance(base, (int, float)) and not isinstance(base, bool):
            cooldown_minutes = float(base)
        else:
            cooldown_minutes = 240.0
    if cooldown_minutes <= 0:
        return False
    cutoff = _utcnow() - timedelta(minutes=cooldown_minutes)
    completed = (
        await session.execute(
            select(Task).where(
                Task.project_id == project_id,
                Task.status == TaskStatus.COMPLETED,
                Task.payloads[0]["type"].as_string() == "research",
                Task.payloads[0]["current_direction_id"].as_string()
                == str(direction_id),
            )
        )
    ).scalars()
    return any(_derive_completed_at(task) >= cutoff for task in completed)


def _build_churn_retirement_evidence(
    churn_count: int,
    threshold: int,
    pause_reason: str | None,
    metrics: dict | None,
) -> dict:
    """Bounded structured evidence for a churn-family retirement recommendation.

    Recorded on the direction (``churn_retirement_evidence``) when the zombie
    churn ladder crosses the configured recommendation threshold. Kept flat and
    small so it survives strategic-payload compaction and is cheap to render.
    ``metrics`` is the ``_compute_churn_metrics`` result (or None when history is
    insufficient).
    """
    evidence: dict = {
        "churn_family_pause_count": int(churn_count),
        "threshold": int(threshold),
        "last_pause_reason": (pause_reason or "")[:200],
        "summary": (
            f"Paused {churn_count}x for churn/unproductive/saturated with no "
            f"escape; retirement recommended — requires an explicit "
            f"strategist/operator retire action (no longer auto-retired)."
        ),
    }
    if metrics:
        # Flat scalars only (merge_rate/non_merge_finished/window_cycles) so the
        # scalar-mapping compactor keeps them.
        for key in ("merge_rate", "non_merge_finished", "window_cycles"):
            if metrics.get(key) is not None:
                evidence[key] = metrics[key]
    return evidence


def _record_auto_retired_memory(
    pending_retired_memory: list[tuple[dict, str]], d: dict, reason: str
) -> None:
    """Queue a compact retired-direction entry for cross-cycle dedup.

    Called at every lifecycle-driven terminal retirement so the strategist dedup
    check can block same-gap or text-overlap re-spawns even after direction pruning.
    The outer lifecycle wrapper applies the queued entries via with_vision_locked()
    after releasing the directions/project lock.
    """
    pending_retired_memory.append((dict(d), reason))


async def _process_direction_lifecycle(
    project: Project,
    session: AsyncSession,
    busy_direction_ids: set,
    now: datetime,
    *,
    expected_legacy_generation: int | None = None,
) -> list:
    """Acquire project-row lock, then delegate to the inner lifecycle worker.

    Uses skip_locked=True so a concurrent operator write causes this background
    tick to be skipped rather than queue behind a 30s lock timeout.  The next
    scheduled cycle will pick up any directions that were not processed.
    """
    from app.task_locking import project_lock

    async with project_lock(session, project.id, skip_locked=True) as _locked:
        if _locked is None:
            logger.debug(
                "[LIFECYCLE] Skipping direction lifecycle for %s: project row held by concurrent writer",
                project.id,
            )
            return []
        if expected_legacy_generation is not None:
            from app.notification_dispatch import begin_deferred_external_notifications

            begin_deferred_external_notifications(session)
        pending_retired_memory: list[tuple[dict, str]] = []
        # Reload directions inside the lock so we read the latest committed
        # state and cannot overwrite a concurrent write that committed between
        # the caller's outer project load and this lock acquisition.
        await session.refresh(project, attribute_names=["directions"])
        active = await _process_direction_lifecycle_locked(
            project,
            session,
            busy_direction_ids,
            now,
            pending_retired_memory=pending_retired_memory,
            expected_legacy_generation=expected_legacy_generation,
        )
    if pending_retired_memory:
        from app.strategic_completion import record_retired_direction_memory
        from app.task_locking import with_vision_locked
        from sqlalchemy.exc import OperationalError

        dropped_ids = [d.get("id") for d, _ in pending_retired_memory]

        def _apply_retired_memory(vision: dict) -> None:
            for direction, reason in pending_retired_memory:
                record_retired_direction_memory(vision, direction, reason=reason)

        try:
            locked = await with_vision_locked(session, project.id, _apply_retired_memory)
        except OperationalError:
            logger.error(  # noqa: TRY400
                "[LIFECYCLE] Vision lock timed out while flushing retired-memory entries; "
                "entries NOT persisted — direction IDs: %s",
                dropped_ids,
            )
            raise
        if locked:
            if expected_legacy_generation is None:
                await session.commit()
            else:
                await commit_legacy_authorized(
                    session, project.id, expected_legacy_generation
                )
        else:
            logger.warning(
                "[LIFECYCLE] Vision lock not acquired (project missing?) while flushing "
                "retired-memory entries; entries silently dropped — direction IDs: %s",
                dropped_ids,
            )
    return active


async def _process_direction_lifecycle_locked(
    project: Project,
    session: AsyncSession,
    busy_direction_ids: set,
    now: datetime,
    *,
    pending_retired_memory: list[tuple[dict, str]] | None = None,
    expected_legacy_generation: int | None = None,
) -> list:
    """Prune dead directions, auto-recover paused ones, and return eligible active directions.

    Always called under the project-row lock (via _process_direction_lifecycle).
    Mutates project.directions in place for auto-pause and recovery state changes.
    Commits the session if any direction state was modified.

    Returns a list of active, non-cooling directions eligible for research.
    """
    from statistics import median

    from app.strategic_completion import (
        _check_direction_health,
        _check_direction_completion,
        _check_outcome_scaffolding,
        _compute_churn_metrics,
        _compute_lifetime_burn_metrics,
        _evidence_progress_suppresses_scaffolding,
        _outcome_progress_config,
        already_built_completion_suggestion,
        consume_direction_merge_grade_if_ready,
        direction_objective_completion_verdict,
    )

    directions_modified = False
    if pending_retired_memory is None:
        pending_retired_memory = []

    def _reconcile_retired_gap(retired_id: str) -> None:
        """Prune a terminally-retired direction out of every problem-model gap's
        ``active_moves`` (archiving it in ``past_moves`` and recording a
        ``GapDeferral`` when the retire empties a gap), so a completed direction
        can never linger in a gap's active staffing list.

        Authoritative on direction id: it matches across ALL gaps regardless of
        the direction's possibly-stale ``gap_id`` hint (closing the latent hole
        where a missing/wrong ``gap_id`` made the old per-gap reconcile a no-op).
        Runs under the already-held project-row lock, mutating
        ``project.problem_model`` in place. Fail-open: a malformed/empty problem
        model never raises and never blocks the retire.
        """
        raw = project.problem_model
        if not isinstance(raw, dict):
            return
        gaps = raw.get("gaps")
        if not isinstance(gaps, list):
            return
        # Only write when the id is actually staffing some gap — keeps the
        # durable problem model byte-identical on the common no-op case.
        in_active = any(
            isinstance(g, dict)
            and isinstance(g.get("active_moves"), list)
            and str(retired_id) in {str(m) for m in g["active_moves"]}
            for g in gaps
        )
        if not in_active:
            return
        from app.problem_model import (
            reconcile_gaps_for_retired_directions,
            sanitize_problem_model_raw,
        )

        try:
            _reconciled, _ = reconcile_gaps_for_retired_directions(
                sanitize_problem_model_raw(raw), [retired_id], _utcnow().isoformat()
            )
        except Exception as _reconcile_exc:  # fail-open — never block the retire  # noqa: BLE001
            logger.warning(
                f"[AUTONOMOUS] Gap reconcile failed for retired direction "
                f"{retired_id}: {_reconcile_exc}"
            )
            return
        project.problem_model = _reconciled
        flag_modified(project, "problem_model")

    def _reconcile_gap_status(
        direction: dict,
        *,
        new_status: str,
        prior_status: str | None,
        rationale: str | None = None,
    ) -> None:
        """Keep problem-model gap staffing aligned with lifecycle status flips."""
        did = direction.get("id")
        gap_id = direction.get("gap_id")
        if not did or not gap_id:
            return
        from app.problem_model import reconcile_gap_staffing_raw_result

        result = reconcile_gap_staffing_raw_result(
            project.problem_model,
            str(did),
            str(gap_id),
            str(gap_id),
            new_status=new_status,
            prior_status=prior_status,
            now_iso=_utcnow().isoformat(),
            rationale=rationale,
        )
        if result.get("problem_model") is not None:
            project.problem_model = result["problem_model"]
            flag_modified(project, "problem_model")
        details = result.get("result") or {}
        if details.get("malformed_model") or details.get("unresolved_gap_ids"):
            logger.warning(
                "[AUTONOMOUS] Gap staffing reconciliation classified drift for "
                "direction %s: %s",
                did,
                details,
            )

    async def _retire_lifetime_burn(d: dict, did, lb_pause_count: int) -> None:
        """Apply the terminal lifetime-burn retirement to ``d`` in place.

        Single source of truth for the lifetime-burn retire side effects so the
        active-directions retire ladder and the auto-recovery extend-pause path
        produce a functionally identical retirement: mark the direction with the
        explicit ``failed`` outcome, clear the manual-hold flags, post the milestone + Alert, and
        reconcile any gap the direction was staffing. Callers set
        ``directions_modified = True`` and ``continue`` past the
        active_directions append.
        """
        _reason = (
            f"Auto-retired: direction tripped lifetime burn backstop "
            f"{lb_pause_count} times with no recovery. "
            f"Slot freed for new directions."
        )
        mark_auto_direction_terminal(
            d,
            outcome="failed",
            reason=_reason,
            completed_at=_utcnow().isoformat(),
        )
        # Clear the manual-hold flags — retirement supersedes them.
        d.pop("needs_manual_intervention", None)
        d.pop("needs_manual_intervention_at", None)
        logger.info(
            f"[AUTONOMOUS] Auto-retired lifetime-burn direction {did} after "
            f"{lb_pause_count} burn-pause cycles: {d.get('text', '')[:60]}"
        )
        from app.alert_policy import create_project_alert_if_allowed
        from app.models import AlertSeverity

        # Routed through the policy gate, not a raw ``Alert(...)`` insert.
        # ``research.direction_auto_retired`` is TIMELINE_ONLY: the milestone
        # message below IS the delivery. The raw insert also wrote a durable
        # operator row that nothing could ever resolve -- "direction X was
        # retired at T" has no future state in which it becomes false.
        await create_project_alert_if_allowed(
            session,
            project_id=project.id,
            event_type="research.direction_auto_retired",
            severity=AlertSeverity.INFO,
            title="Direction auto-retired (lifetime burn)",
            message=(
                f"Direction '{d.get('text', '')[:80]}' was auto-retired after "
                f"{lb_pause_count} lifetime burn backstop pauses. "
                f"Slot freed for strategic agent to create a replacement."
            ),
        )
        try:
            from app.notification_dispatch import (
                post_direction_milestone_message,
            )

            await post_direction_milestone_message(
                session,
                project.id,
                direction_text=d.get("text", ""),
                milestone="auto-retired-lifetime-burn",
                reason=d["auto_completed_reason"],
                direction_id=did,
            )
        except Exception as _notify_exc:  # noqa: BLE001
            logger.warning(
                f"[NOTIFY] Failed to post lifetime-burn retire milestone for {did}: "
                f"{_notify_exc}"
            )
        # Gap reconciliation: prune this direction from every gap's active_moves
        # by id — authoritative helper so a missing/stale gap_id cannot leave the
        # direction as phantom active staff.
        _reconcile_retired_gap(did)
        _record_auto_retired_memory(
            pending_retired_memory, d, "auto_retired_lifetime_burn"
        )

    async def _retire_churn_undecided(d: dict, did) -> None:
        """Apply the churn ladder's own recommendation after an undecided window.

        Runs only from the designated retire pass, one tick after the fence
        flagged the direction, and only for a direction still in a live status
        (the coherence guard above already dropped anything that went terminal or
        was reactivated in between). Delegates the terminal mutation to
        :func:`apply_churn_retirement_retire` — the same sanctioned helper the
        explicit operator/strategist RETIRE decision uses — so an expiry-applied
        retirement and an explicitly-decided one are byte-identical in outcome
        (``stopped``) and residue-clearing. The distinguishing record is the
        durable ``churn_retirement_decision`` blob, which survives the clear.
        """
        decision = d.get("churn_retirement_decision") or {}
        _undecided_h = decision.get("undecided_hours")
        _window_days = decision.get("window_days", _CHURN_RECOMMENDATION_DECISION_DAYS)
        _summary = decision.get("recommendation_summary") or "churn/unproductive loop"
        _reason = (
            f"Auto-retired: churn-retirement recommendation went undecided for "
            f"{_undecided_h}h (decision window {_window_days}d, "
            f"~{decision.get('strategist_opportunities', '?')} guaranteed strategist "
            f"opportunities). Applying the recommended retirement — {_summary}. "
            f"Slot freed for new directions."
        )
        apply_churn_retirement_retire(d, reason=_reason, now_iso=_utcnow().isoformat())
        logger.info(
            "[AUTONOMOUS] Auto-retired churn-recommended direction %s after an "
            "undecided %sd decision window: %s",
            did,
            _window_days,
            (d.get("text", "") or "")[:60],
        )
        from app.alert_policy import create_project_alert_if_allowed
        from app.models import AlertSeverity

        # TIMELINE_ONLY via the policy gate; see the lifetime-burn site above.
        await create_project_alert_if_allowed(
            session,
            project_id=project.id,
            event_type="research.direction_auto_retired",
            severity=AlertSeverity.INFO,
            title="Direction auto-retired (churn recommendation undecided)",
            message=(
                f"Direction '{(d.get('text', '') or '')[:80]}' was recommended for "
                f"churn retirement and went undecided for {_undecided_h}h "
                f"(window {_window_days}d). The recommended retirement was applied "
                f"automatically. Slot freed for the strategic agent to create a "
                f"replacement; the direction remains resurrectable."
            ),
        )
        try:
            from app.notification_dispatch import (
                post_direction_milestone_message,
            )

            await post_direction_milestone_message(
                session,
                project.id,
                direction_text=d.get("text", ""),
                milestone="auto-retired-churn-undecided",
                reason=d.get("auto_completed_reason", _reason),
                direction_id=did,
                evidence=decision or None,
            )
        except Exception as _notify_exc:  # noqa: BLE001
            logger.warning(
                f"[NOTIFY] Failed to post churn-undecided retire milestone for {did}: "
                f"{_notify_exc}"
            )
        _reconcile_retired_gap(did)
        _record_auto_retired_memory(
            pending_retired_memory, d, "auto_retired_churn_undecided"
        )

    # Designated retire pass: consume auto_retire_pending flags set in the prior
    # lifecycle tick. Separates the DECISION to retire (flagged by an outlier
    # auto-retire site) from EXECUTION (here), so every retirement passes through
    # a single auditable gate and is visible in the strategist payload for at
    # least one tick before the direction is removed by the sweep prune below.
    for _d in project.directions or []:
        if not _d.get("auto_retire_pending"):
            continue
        _retire_did = _d.get("id")
        _retire_reason = _d.get("auto_retire_pending_reason", "")
        # Outcome-scaffolding is an advisory detector, not retirement
        # authority.  Older releases persisted a two-tick retirement flag for
        # this reason; consume that legacy state by clearing the flag without
        # changing the direction's current status. Terminal directions are
        # left terminal because their status may reflect a subsequent explicit
        # strategist/operator decision rather than the legacy detector.
        if _retire_reason == "scaffolding":
            _current_status = _d.get("status")
            _d.pop("auto_retire_pending", None)
            _d.pop("auto_retire_pending_reason", None)
            _d.pop("auto_retire_pending_at", None)
            if _current_status in ("active", "paused", "proposed", "staged"):
                logger.info(
                    "[LIFECYCLE] Cleared legacy scaffolding retirement flag for "
                    "live direction %s without changing status=%r",
                    _retire_did,
                    _current_status,
                )
            else:
                logger.info(
                    "[LIFECYCLE] Cleared legacy scaffolding retirement flag for "
                    "terminal direction %s without changing status=%r",
                    _retire_did,
                    _current_status,
                )
            directions_modified = True
            continue
        # Coherence guard: skip (and clear) the flag when the direction is no
        # longer in the state that produced it.  This collapses two failure modes:
        # (a) operator reactivated the direction between the flag tick and this
        #     consume tick — auto_retire_pending was already cleared by
        #     _clear_direction_reactivation_state, so this branch is dead for
        #     that path, but a belt-and-suspenders status check catches any gap;
        # (b) the direction reached terminal status via another path (e.g.
        #     operator PATCH completed_at, zombie-retire in the same tick) while
        #     the flag was still set — retiring a second time would overwrite
        #     auto_completed_reason and fire duplicate Alert + milestone calls.
        _current_status = _d.get("status")
        if _current_status not in ("active", "paused", "proposed", "staged"):
            logger.info(
                f"[LIFECYCLE] Skipping designated retire for direction {_retire_did}: "
                f"status={_current_status!r} is already terminal; clearing flag"
            )
            _d.pop("auto_retire_pending", None)
            _d.pop("auto_retire_pending_reason", None)
            _d.pop("auto_retire_pending_at", None)
            directions_modified = True
            continue
        if _retire_reason == "lifetime_burn":
            _lb_count = _d.get("lifetime_burn_pause_count", _MAX_LIFETIME_BURN_PAUSE_CYCLES)
            await _retire_lifetime_burn(_d, _retire_did, _lb_count)
        elif _retire_reason == CHURN_UNDECIDED_RETIRE_REASON:
            await _retire_churn_undecided(_d, _retire_did)
        else:
            logger.warning(
                f"[LIFECYCLE] Unknown auto_retire_pending_reason {_retire_reason!r} "
                f"for direction {_retire_did}; clearing flag without retiring"
            )
        _d.pop("auto_retire_pending", None)
        _d.pop("auto_retire_pending_reason", None)
        _d.pop("auto_retire_pending_at", None)
        directions_modified = True

    # Prune dead directions (expired/completed/rejected) to prevent JSON bloat.
    # "staged" directions are inert-by-design (begin-after-current-version-
    # promotes) and MUST survive the prune so they remain to be auto-activated
    # at the next version-open transition — they are not dead.
    live_dirs = [
        d
        for d in (project.directions or [])
        if d.get("status") in ("active", "proposed", "paused", "staged")
    ]
    if len(live_dirs) < len(project.directions or []):
        pruned_dirs = [
            d
            for d in (project.directions or [])
            if d.get("status") not in ("active", "proposed", "paused", "staged")
        ]
        # Record tombstone memory for each pruned direction not already covered
        # by an explicit auto-retire call earlier in this lifecycle pass.
        already_recorded_ids = {d.get("id") for d, _ in (pending_retired_memory or [])}
        for d in pruned_dirs:
            if d.get("id") not in already_recorded_ids:
                reason = d.get("auto_completed_reason") or d.get("status") or "pruned"
                _record_auto_retired_memory(pending_retired_memory, d, reason)
        logger.info(
            f"[AUTONOMOUS] Pruned {len(project.directions or []) - len(live_dirs)} dead directions from {project.id}"
        )
        project.directions = live_dirs
        flag_modified(project, "directions")
        directions_modified = True
    # Retry BLOCKER escalation notifications whose initial delivery failed.
    # Runs before auto-recovery so a direction that recovers this cycle does
    # not silently lose its pending notification.
    for d in project.directions or []:
        if not d.get("escalation_notification_pending"):
            continue
        retries = int(d.get("escalation_notification_retries", 0))
        if retries >= _MAX_ESCALATION_NOTIFICATION_RETRIES:
            logger.error(
                "[NOTIFY] Escalation notification retries exhausted",
                extra={
                    "event": "escalation_retries_exhausted",
                    "project_id": str(project.id),
                    "direction_id": d.get("id"),
                    "retries": retries,
                    "remediation_attempts": int(d.get("remediation_attempts", 0)),
                },
            )
            from app.alert_policy import create_project_alert_if_allowed
            from app.models import AlertSeverity

            # Routed through the policy gate, and DELIBERATELY still durable:
            # message_audience classifies this event OPERATOR_ACTION_NEEDED, so
            # suppressing it would delete an operator signal rather than move
            # it to another surface. Its retirement mode is MANUAL -- honest,
            # because nothing in the codebase resolves it -- and the dedupe_key
            # is what stops a direction that keeps failing delivery from
            # minting one indistinguishable ERROR row per lifecycle pass:
            # repeats now bump occurrence_count on the single open row.
            await create_project_alert_if_allowed(
                session,
                project_id=project.id,
                event_type="research.escalation_delivery_failed",
                severity=AlertSeverity.ERROR,
                title="Escalation notification delivery failed",
                dedupe_key=f"escalation_delivery_failed:{d.get('id')}",
                message=(
                    f"Direction {d.get('id')} ('{(d.get('text') or '')[:80]}') needs manual "
                    f"intervention but the BLOCKER notification could not be delivered after "
                    f"{retries} retries. Fingerprint={d.get('error_fingerprint') or '(unknown)'}, "
                    f"remediation_attempts={int(d.get('remediation_attempts', 0))}."
                ),
            )
            d.pop("escalation_notification_pending", None)
            directions_modified = True
            continue
        try:
            from app.notification_dispatch import post_remediation_escalation_message

            await post_remediation_escalation_message(
                session,
                project.id,
                **_build_escalation_args(d),
            )
            d.pop("escalation_notification_pending", None)
            d.pop("escalation_notification_retries", None)
            directions_modified = True
            logger.info(
                f"[NOTIFY] Escalation retry succeeded for direction {d.get('id')} "
                f"after {retries} prior retries"
            )
        except Exception as _retry_exc:  # noqa: BLE001
            d["escalation_notification_retries"] = retries + 1
            directions_modified = True
            logger.warning(
                f"[NOTIFY] Escalation retry {retries + 1} failed for {d.get('id')}: {_retry_exc}"
            )
    # TTL fallback for stuck needs_manual_intervention. The flag is normally cleared
    # when the remediation monitor expires (line ~561) or when the fingerprint
    # changes on recovery, but both paths require remediation_monitor_started_at,
    # which only gets set on a successful remediation merge. If remediation keeps
    # failing, the flag (and operator alert) persists forever — clear it here
    # once enough time has passed without a monitor ever starting.
    #
    # EXCEPTION: lifetime-burn pauses set needs_manual_intervention but never set
    # a remediation monitor (they are not code-failure remediation cases). They require
    # manual operator action (retire or reset) and should persist until resolved,
    # not expire via this TTL safety net designed for remediation-stuck scenarios.
    for d in project.directions or []:
        if not d.get("needs_manual_intervention"):
            continue
        if d.get("remediation_monitor_started_at"):
            continue
        if d.get("churn_retirement_recommended"):
            continue
        # Skip TTL clear for lifetime-burn pauses — they require manual operator
        # intervention and should not auto-expire.
        if (d.get("paused_reason") or "").startswith("Lifetime burn backstop:"):
            continue
        # Skip TTL clear for churn-retirement-recommended directions — the
        # recommendation holds the direction paused pending an explicit
        # keep/retire decision. Silently clearing needs_manual_intervention here
        # (after 72h) would let the auto-recovery pass reactivate it with no
        # decision made. It must survive indefinitely until a sanctioned
        # reactivation/undo path clears churn_retirement_recommended.
        if d.get("churn_retirement_recommended"):
            continue
        flagged_at = d.get("needs_manual_intervention_at")
        if not flagged_at:
            continue
        try:
            age_h = (
                _utcnow() - datetime.fromisoformat(flagged_at)
            ).total_seconds() / 3600
        except (ValueError, TypeError):
            continue
        if age_h < _STUCK_INTERVENTION_TTL_HOURS:
            continue
        d.pop("needs_manual_intervention", None)
        d.pop("needs_manual_intervention_at", None)
        directions_modified = True
        logger.info(
            f"[AUTONOMOUS] Cleared stuck needs_manual_intervention on {d.get('id')} "
            f"after {age_h:.1f}h with no remediation monitor started"
        )
    # Auto-recovery: reactivate auto-paused directions past cooldown
    default_recovery_hours = _runtime_config.get("direction_auto_recovery_hours", 48)
    for d in project.directions or []:
        if (
            d.get("status") != "paused"
            or not d.get("auto_paused")
            or not d.get("paused_at")
        ):
            continue
        # Churn-retirement-recommended directions are held paused pending an
        # explicit keep/retire decision — never silently reactivate them by time
        # alone. This guard is defensive-in-depth alongside the needs_manual_
        # intervention flag (which the branch below already respects): even if
        # that flag were cleared elsewhere, the recommendation itself keeps the
        # direction paused until a sanctioned undo path clears the flag.
        #
        # The hold is NOT unbounded, though: once the decision window has elapsed
        # with no keep/retire answer, apply the ladder's own recommendation rather
        # than nursing the dud forever. This never reactivates and never clears a
        # flag by time alone — it flags the two-tick designated retire, exactly as
        # the lifetime-burn ladder does below, so the direction stays visible in
        # the strategist payload for one more tick and any sanctioned reactivation
        # in the meantime cancels it.
        if d.get("churn_retirement_recommended"):
            _decided = _churn_recommendation_decision_overdue(d)
            if _decided is not None and not d.get("auto_retire_pending"):
                d["churn_retirement_decision"] = _decided
                d["auto_retire_pending"] = True
                d["auto_retire_pending_reason"] = CHURN_UNDECIDED_RETIRE_REASON
                d["auto_retire_pending_at"] = now.isoformat()
                directions_modified = True
                logger.warning(
                    "[AUTONOMOUS] Churn-retirement recommendation for direction %s "
                    "went undecided for %.1fh (window %dd, ~%d guaranteed strategist "
                    "opportunities); applying the recommended retirement on the next "
                    "lifecycle tick",
                    d.get("id"),
                    _decided["undecided_hours"],
                    _decided["window_days"],
                    _decided["strategist_opportunities"],
                )
            continue
        recovery_hours = default_recovery_hours
        try:
            paused_dt = datetime.fromisoformat(d["paused_at"])
        except (ValueError, TypeError):
            did = d.get("id")
            logger.warning(
                f"[LIFECYCLE] Skipping auto-recovery for {did}: malformed paused_at {d['paused_at']!r}"
            )
            continue
        hours = (_utcnow() - paused_dt).total_seconds() / 3600
        if hours < recovery_hours:
            continue
        if not d.get("needs_manual_intervention") and not d.get(
            "churn_retirement_recommended"
        ):
            prior_status = d.get("status")
            d["status"] = "active"
            for k in ("auto_paused", "paused_reason", "paused_at"):
                d.pop(k, None)
            # Grace period: stamp recovery time so _check_direction_health can skip
            # history-based health rules until at least one new cycle is appended.
            # Without this, the same frozen research_history that triggered the pause
            # immediately re-triggers it on the next lifecycle tick.
            d["auto_recovered_at"] = _utcnow().isoformat()
            # Start remediation-effectiveness monitoring when recovering a direction whose
            # remediation task merged. Subsequent failures with the same error fingerprint
            # during this window signal the fix did not hold; escalation is triggered below
            # when the direction is re-paused while monitoring is active.
            if d.get("last_remediation_result") in ("merged", "partial_success"):
                d["remediation_monitor_started_at"] = _utcnow().isoformat()
            logger.info(
                f"[AUTONOMOUS] Auto-recovered direction {d.get('id')} after {hours:.0f}h cooldown"
            )
            _reconcile_gap_status(
                d,
                new_status="active",
                prior_status=prior_status,
            )
            directions_modified = True
        else:
            # Lifetime-burn backstop pauses require manual operator action (retire or reset)
            # because the cumulative metric won't drop on its own. The auto-recovery branch
            # above is blocked for these (needs_manual_intervention is set), so this extend
            # path is the ONLY place a manual-held burn pause is re-touched. Advance the
            # burn ladder here so a structurally-stuck direction eventually auto-retires
            # with zero operator action instead of thrashing in pause/extend forever —
            # mirroring the active-loop ladder threshold (_MAX_LIFETIME_BURN_PAUSE_CYCLES).
            if (d.get("paused_reason") or "").startswith("Lifetime burn backstop:"):
                d["lifetime_burn_pause_count"] = (
                    d.get("lifetime_burn_pause_count", 0) + 1
                )
                lb_pause_count = d.get("lifetime_burn_pause_count", 0)
                if lb_pause_count >= _MAX_LIFETIME_BURN_PAUSE_CYCLES:
                    # Flag for retirement rather than executing immediately; the
                    # designated retire pass (above) will complete the retirement
                    # on the next lifecycle tick so the direction remains visible
                    # in the strategist payload before it is removed.
                    d["auto_retire_pending"] = True
                    d["auto_retire_pending_reason"] = "lifetime_burn"
                    d["auto_retire_pending_at"] = now.isoformat()
                    directions_modified = True
                    continue
            # Otherwise keep the pause active until the operator reactivates the
            # direction or the TTL fallback clears the flag.
            d["paused_at"] = _utcnow().isoformat()
            logger.info(
                f"[AUTONOMOUS] Direction {d.get('id')} requires manual intervention, extending pause"
            )
            directions_modified = True
    active_directions = []
    auto_paused_any = False

    def _cost_per_merge(direction: dict) -> float | None:
        history = list(direction.get("research_history") or [])
        if cpm_window > 0:
            history = history[-cpm_window:]
        spend = 0.0
        autonomous = 0
        for entry in history:
            try:
                spend += max(0.0, float(entry.get("estimated_cost_usd") or 0.0))
                # Legacy fallback: pre-attribution entries lack autonomous_prs_merged;
                # treat all merges as autonomous so CPM is conservative for old data.
                autonomous += max(
                    0,
                    int(
                        entry.get("autonomous_prs_merged", entry.get("prs_merged")) or 0
                    ),
                )
            except (TypeError, ValueError):  # noqa: PERF203
                continue
        return (spend / autonomous) if autonomous > 0 else None

    def _cost_by_provider_model_window(
        direction: dict,
    ) -> tuple[dict[str, float], float]:
        """Sum per-(provider, model) cost across the same rolling window.

        Returns (totals_by_key, total_window_cost). Legacy entries lacking
        ``cost_by_provider_model`` contribute 0 to the breakdown — the caller
        falls back to omitting the split for those directions.
        """
        history = list(direction.get("research_history") or [])
        if cpm_window > 0:
            history = history[-cpm_window:]
        totals: dict[str, float] = {}
        for entry in history:
            for key, val in (entry.get("cost_by_provider_model") or {}).items():
                try:
                    totals[key] = totals.get(key, 0.0) + max(0.0, float(val))
                except (TypeError, ValueError):  # noqa: PERF203
                    continue
        return totals, sum(totals.values())

    try:
        cpm_window = int(_runtime_config.get("direction_cost_per_merge_window", 6) or 6)
    except (TypeError, ValueError):
        cpm_window = 6
    try:
        cpm_multiplier = float(
            _runtime_config.get("direction_cost_per_merge_multiplier", 2.0) or 2.0
        )
    except (TypeError, ValueError):
        cpm_multiplier = 2.0
    project_cpms = [
        cpm
        for cpm in (_cost_per_merge(x) for x in (project.directions or []))
        if cpm is not None and cpm > 0
    ]
    project_cpm_median = median(project_cpms) if project_cpms else None
    # ONE canonical lifetime lineage-spend read per tick, covering every
    # direction that declares a cost envelope. This is the SAME meter the
    # operator-visible budget badge divides by (app.direction_lineage_spend),
    # so the auto-pause gate and the card agree on what "spend" means. Batched
    # once for all budgeted directions — a tick never fans out into per-direction
    # round trips.
    budgeted_ids = [
        str(d.get("id") or d.get("direction_id") or "")
        for d in (project.directions or [])
        if _direction_budget_usd(d) is not None
    ]
    lineage_spend_by_direction: dict[str, DirectionLineageSpend] = {}
    if budgeted_ids:
        lineage_spend_by_direction = await load_direction_lineage_spend(
            session, project_id=project.id, direction_ids=budgeted_ids
        )
    for d in project.directions or []:
        if d.get("status") != "active":
            continue
        did = d.get("id")
        # Skip directions with an unrolled research cycle still in progress
        if did and did in busy_direction_ids:
            logger.debug(
                f"[RESEARCH] Skipping direction {did}: research cycle already active"
            )
            continue
        # Clear stale diagnosis flags so a re-paused direction can be diagnosed again.
        # If the investigation task was reaped/timed out without reaching the completion handler,
        # diagnosis_in_progress stays True and permanently blocks future diagnosis spawning.
        if d.get("diagnosis_in_progress"):
            started_at = d.get("diagnosis_started_at")
            is_stale = True
            if started_at:
                try:
                    age_h = (
                        _utcnow() - datetime.fromisoformat(started_at)
                    ).total_seconds() / 3600
                    is_stale = age_h >= _DIAGNOSIS_STALE_HOURS
                except (ValueError, TypeError):
                    is_stale = True
            if is_stale:
                prior_task_id = d.get("diagnosis_task_id")
                logger.warning(
                    f"[DIAGNOSIS] Clearing stale diagnosis flags for direction {did} "
                    f"(started_at={started_at})"
                )
                d.pop("diagnosis_in_progress", None)
                d.pop("diagnosis_started_at", None)
                d.pop("diagnosis_task_id", None)
                d["diagnosis_completed_at"] = _utcnow().isoformat()
                directions_modified = True
                # The auto-diagnosis re-spawn lane was removed with the
                # detect->investigate->fix apparatus strip. Surface the stalled
                # diagnosis as a deterministic operator Alert instead so the
                # signal (a direction whose investigation never produced a
                # finding) is preserved for manual follow-up.
                from app.models import Alert, AlertSeverity

                session.add(
                    Alert(
                        project_id=project.id,
                        event_type="research.diagnosis_stalled",
                        severity=AlertSeverity.WARNING,
                        title="Direction diagnosis stalled",
                        message=(
                            f"Direction '{(d.get('text') or '')[:80]}' ({did}) had a "
                            f"diagnosis investigation time out without producing a "
                            f"finding (prior task {prior_task_id}). Manual "
                            f"investigation required."
                        ),
                    )
                )
                logger.warning(
                    f"[DIAGNOSIS] Stalled diagnosis for {did}; "
                    f"escalating to operator alert"
                )
        # If a remediation monitor has expired while the direction stayed active,
        # the fix held — drop the monitor and clear any lingering manual-intervention
        # alert so the UI stops showing a permanent red banner after recovery.
        monitor_started_at = d.get("remediation_monitor_started_at")
        if monitor_started_at:
            expired = False
            age_desc = ""
            try:
                age_days = (
                    _utcnow() - datetime.fromisoformat(monitor_started_at)
                ).total_seconds() / 86400
                if age_days > _REMEDIATION_MONITOR_EXPIRY_DAYS:
                    expired = True
                    age_desc = f"age {age_days:.1f}d"
            except (TypeError, ValueError):
                # Unparseable timestamp — safer to treat the monitor as expired
                # than to keep it active indefinitely and risk a false escalation.
                expired = True
                age_desc = f"unparseable timestamp {monitor_started_at!r}"
            if expired:
                d.pop("remediation_monitor_started_at", None)
                cleared = d.pop("needs_manual_intervention", None)
                d.pop("needs_manual_intervention_at", None)
                # Stamp "held" on the remediation_history entry that started this monitor
                # so the digest funnel can count fixes that survived the full window.
                held_iso = _utcnow().isoformat()
                history = list(d.get("remediation_history") or [])
                for entry in reversed(history):
                    if entry.get("result") in ("merged", "partial_success"):
                        entry["result"] = "held"
                        entry["held_at"] = held_iso
                        d["remediation_history"] = history
                        break
                d["last_remediation_held_at"] = held_iso
                if d.get("last_remediation_result") in ("merged", "partial_success"):
                    d["last_remediation_result"] = "held"
                logger.info(
                    f"[AUTONOMOUS] Remediation monitor expired for {did} "
                    f"({age_desc}); fix held; "
                    f"{'cleared needs_manual_intervention' if cleared else 'no intervention flag to clear'}"
                )
                directions_modified = True
        # Refractory check: direction is held ineligible until refractory_until elapses.
        # Set by research completion (standard window) or no-findings saturation (longer
        # window). Strategist/operator can clear refractory_until to re-open the direction.
        refractory_info = get_direction_refractory_info(d, now=now)
        remaining_m = refractory_info["refractory_remaining_minutes"]
        if remaining_m is not None and remaining_m > 0:
            logger.debug(
                "[RESEARCH] Skipping direction %s: %s hold for %.1fm more",
                did,
                refractory_info["refractory_reason"],
                remaining_m,
            )
            continue
        # Cost efficiency escalation: flag expensive directions for retirement consideration.
        direction_cpm = _cost_per_merge(d)
        threshold = (
            (project_cpm_median * cpm_multiplier) if project_cpm_median else None
        )
        if direction_cpm and threshold and direction_cpm > threshold:
            # Raise once per breach episode. The pre-existing flag value gates
            # re-firing so the alert below is not re-emitted on every lifecycle
            # tick while the direction stays over threshold (mirrors the churn
            # ladder's ``churn_retirement_recommended`` gate).
            _already_flagged = bool(d.get("retirement_consideration_needed"))
            d["retirement_consideration_needed"] = True
            d["retirement_consideration_at"] = _utcnow().isoformat()
            d["retirement_consideration_reason"] = (
                f"Recent cost/merge ${direction_cpm:.2f} exceeded threshold ${threshold:.2f}."
            )
            evidence: dict = {
                "window_cycles": cpm_window,
                "direction_cost_per_merge_usd": round(direction_cpm, 4),
                "threshold_usd": round(threshold, 4),
            }
            split_totals, split_total = _cost_by_provider_model_window(d)
            if split_totals and split_total > 0:
                evidence["cost_by_provider_model"] = {
                    key: {
                        "cost_usd": round(cost, 4),
                        "share_pct": round(cost / split_total, 4),
                    }
                    for key, cost in split_totals.items()
                }
            else:
                evidence["cost_by_provider_model"] = {}
            d["retirement_consideration_evidence"] = evidence
            directions_modified = True
            logger.warning(
                f"[AUTONOMOUS] Direction {did} flagged for retirement consideration: "
                f"cost/merge ${direction_cpm:.2f} > ${threshold:.2f}"
            )
            if not _already_flagged:
                # Deliver the recommendation to a decider. Before this, the flag
                # was written to the direction dict and read by nothing but the
                # admin-ui card, so a cost-outlier direction kept being selected
                # for research indefinitely and no keep-or-retire decision was
                # ever owed by anyone.
                from app.models import Alert, AlertSeverity

                session.add(
                    Alert(
                        project_id=project.id,
                        event_type="research.retirement_consideration_needed",
                        severity=AlertSeverity.INFO,
                        title="Direction flagged for retirement consideration",
                        message=(
                            f"Direction '{(d.get('text') or '')[:80]}' ({did}) has a "
                            f"{cpm_window}-cycle cost/merge of ${direction_cpm:.2f}, over the "
                            f"${threshold:.2f} threshold ({cpm_multiplier:g}x the project "
                            f"median). A keep-or-retire decision is owed. The direction stays "
                            f"ACTIVE and eligible for research until a strategist/operator "
                            f"decides — a cost breach alone is not evidence of low value."
                        ),
                    )
                )
        elif d.get("retirement_consideration_needed"):
            # Self-clear. The recommendation used to be a one-way latch: nothing
            # in the backend ever removed these keys, so a direction whose
            # cost/merge recovered kept carrying a stale retire-me badge forever
            # and the strategist could not tell a live breach from an old one.
            # Clear it and record WHY, so the signal read downstream is current.
            if direction_cpm is None or threshold is None:
                _clear_reason = (
                    "cost/merge is no longer measurable for this direction "
                    f"(cost_per_merge={direction_cpm!r}, threshold={threshold!r})"
                )
            else:
                _clear_reason = (
                    f"cost/merge ${direction_cpm:.2f} recovered to at-or-below the "
                    f"${threshold:.2f} threshold"
                )
            for _key in (
                "retirement_consideration_needed",
                "retirement_consideration_at",
                "retirement_consideration_reason",
                "retirement_consideration_evidence",
            ):
                d.pop(_key, None)
            d["retirement_consideration_cleared_at"] = _utcnow().isoformat()
            d["retirement_consideration_cleared_reason"] = _clear_reason
            directions_modified = True
            logger.info(
                "[AUTONOMOUS] Direction %s retirement consideration cleared: %s",
                did,
                _clear_reason,
            )
        # Auto-pause check — must come after cooldown to only evaluate directions eligible for research
        pause_reason = _check_direction_health(d)
        cumulative_cost = 0.0
        budget_f = _direction_budget_usd(d)
        spend_basis = ""
        spend_terms: dict[str, object] = {}
        # Spend actually attributed to the direction's task lineage. Under the
        # canonical basis this IS the whole envelope; under the labelled
        # research-history fallback no lineage rows were found, so it is 0.
        lineage_spend = 0.0
        if budget_f is not None:
            # Canonical basis: the SAME lifetime lineage spend the badge reads.
            # A direction with real lineage rows is measured on that meter; the
            # research-history sum below is only an explicit labelled fallback.
            lineage = lineage_spend_by_direction.get(
                str(d.get("id") or d.get("direction_id") or "")
            )
            if lineage is not None and lineage.task_count > 0:
                cumulative_cost = lineage.total_usd
                lineage_spend = lineage.total_usd
                spend_basis = lineage.basis
                spend_terms = {
                    "lineage_vm_cost_usd": round(lineage.vm_usd, 4),
                    "lineage_llm_cost_usd": round(lineage.llm_usd, 4),
                    "lineage_compute_cost_usd": round(lineage.compute_usd, 4),
                    "lineage_task_count": lineage.task_count,
                    "lineage_llm_unpriced": lineage.llm_unpriced,
                }
            else:
                # Fallback basis: research_history + history_aggregate, used only
                # when the lineage query found no task rows for this direction
                # (e.g. archived/pruned lineage).
                for entry in d.get("research_history") or []:
                    try:
                        cumulative_cost += max(
                            0.0, float(entry.get("estimated_cost_usd") or 0.0)
                        )
                    except (TypeError, ValueError):  # noqa: PERF203
                        continue
                # Include evicted cycles folded into history_aggregate so the
                # budget check fires correctly for long-lived directions past
                # the 10-cycle eviction window.
                try:  # noqa: SIM105
                    cumulative_cost += max(
                        0.0,
                        float(
                            (d.get("history_aggregate") or {}).get(
                                "total_estimated_cost_usd"
                            )
                            or 0.0
                        ),
                    )
                except (TypeError, ValueError):
                    pass
                spend_basis = _BUDGET_BASIS_RESEARCH_HISTORY
                spend_terms = {
                    "lineage_vm_cost_usd": 0.0,
                    "lineage_llm_cost_usd": 0.0,
                    "lineage_compute_cost_usd": 0.0,
                    "lineage_task_count": 0,
                    "lineage_llm_unpriced": False,
                }
        if budget_f is not None and cumulative_cost > budget_f:
            pause_reason = (
                f"Budget exceeded: cumulative estimated cost ${cumulative_cost:.2f} "
                f"> cost budget ${budget_f:.2f}"
            )
            d["cost_pause_evidence"] = {
                "attributed_lineage_spend_usd": round(lineage_spend, 4),
                "cumulative_estimated_cost_usd": round(cumulative_cost, 4),
                "cost_budget_usd": round(budget_f, 4),
                "spend_basis": spend_basis,
                **spend_terms,
            }
        # Fix #3: route "already-built" directions to completion instead of a
        # churn / zero-merge auto-pause. When the pending pause is a cumulative
        # merge-rate signal (Rule 2 Unproductive, Rule 5 Churn, Rule 6 Lifetime burn)
        # AND the direction's recent cycles are dominated by correct no-op
        # "already-present" completions (no merges, no code failures), the goal is
        # effectively already met. Suppress the pause here; the possibly_complete
        # flag itself is set authoritatively by the positive-completion block below
        # (which also owns clearing it), so the two paths cannot fight over the flag.
        # Budget and code-failure pauses are deliberately excluded — those are not
        # "already built".
        _CHURN_FAMILY_PREFIXES = (
            "Unproductive:",
            "Churn guardrail:",
            "Lifetime burn backstop:",
        )
        if (
            pause_reason
            and pause_reason.startswith(_CHURN_FAMILY_PREFIXES)
            and already_built_completion_suggestion(d)
        ):
            logger.info(
                f"[AUTONOMOUS] Direction {did} suppressing churn-pause "
                f"({pause_reason!r}); routing to possibly_complete (already built)"
            )
            pause_reason = None
        if pause_reason:
            prior_status = d.get("status")
            d["status"] = "paused"
            d["paused_reason"] = pause_reason
            d["paused_at"] = _utcnow().isoformat()
            d["auto_paused"] = True
            d["auto_pause_count"] = d.get("auto_pause_count", 0) + 1
            # Zombie ladder: count churn/unproductive/saturated re-pauses so a
            # direction that oscillates between paused and auto-recovered without
            # ever shipping work can be auto-retired below (ref bf6d8788).
            if pause_reason.startswith(_ZOMBIE_FAMILY_PREFIXES):
                d["churn_family_pause_count"] = (
                    d.get("churn_family_pause_count", 0) + 1
                )
            d.pop("auto_recovered_at", None)  # Grace period consumed on re-pause
            d.pop(
                "diagnosis_stall_retry_count", None
            )  # Reset retries for this pause event
            # Store structured evidence for churn guardrail pauses so the operator
            # can see the windowed counts (not just the reason string).
            if pause_reason.startswith("Churn guardrail:"):
                churn_ev = _compute_churn_metrics(d)
                if churn_ev:
                    d["churn_pause_evidence"] = churn_ev
            # Lifetime burn backstop: store evidence, increment ladder counter, and
            # flag for manual intervention. The lifetime metric won't drop on its own
            # after auto-recovery, so the direction would thrash through repeated
            # pause/extend cycles. On the first few pauses surface it for operator
            # decision; once the counter hits _MAX_LIFETIME_BURN_PAUSE_CYCLES the
            # lifetime-burn retire ladder (below) will auto-retire it instead.
            if pause_reason.startswith("Lifetime burn backstop:"):
                lb_ev = _compute_lifetime_burn_metrics(d)
                if lb_ev:
                    d["lifetime_burn_pause_evidence"] = lb_ev
                d["lifetime_burn_pause_count"] = (
                    d.get("lifetime_burn_pause_count", 0) + 1
                )
                if not d.get("needs_manual_intervention"):
                    d["needs_manual_intervention"] = True
                    d["needs_manual_intervention_at"] = _utcnow().isoformat()
                    logger.warning(
                        f"[AUTONOMOUS] Direction {did} flagged needs_manual_intervention "
                        f"(lifetime burn backstop): operator must retire or reset."
                    )
            # Only code-failure pauses (Rules 3/4) count toward auto-retirement.
            # Infrastructure-driven pauses (saturation, unproductive) shouldn't
            # permanently retire an otherwise healthy direction.
            if _is_code_failure_pause_reason(pause_reason):
                d["code_failure_pause_count"] = d.get("code_failure_pause_count", 0) + 1
            # Stamp the dominant error fingerprint from the direction's recent failures
            # so the diagnostics API can surface it and escalation can verify that the
            # same root cause keeps recurring after remediation.
            if _is_code_failure_pause_reason(pause_reason):
                try:
                    fingerprint = await _compute_direction_error_fingerprint(
                        project.id, did, session
                    )
                    if fingerprint:
                        d["error_fingerprint"] = fingerprint
                except Exception as _fp_exc:  # noqa: BLE001
                    logger.warning(
                        f"[DIAGNOSIS] Failed to compute error fingerprint for {did}: {_fp_exc!r}"
                    )
            # Re-pause while remediation monitoring is active means the fix did not hold.
            # Flag for manual intervention once attempts reach the escalation threshold so
            # the diagnostics API and notification dispatcher can surface it to operators.
            monitor_started_at = d.get("remediation_monitor_started_at")
            monitor_expired = False
            if monitor_started_at:
                try:
                    age_days = (
                        _utcnow() - datetime.fromisoformat(monitor_started_at)
                    ).total_seconds() / 86400
                    if age_days > _REMEDIATION_MONITOR_EXPIRY_DAYS:
                        monitor_expired = True
                        d.pop("remediation_monitor_started_at", None)
                        logger.info(
                            f"[AUTONOMOUS] Cleared stale remediation monitor on {did} "
                            f"(age {age_days:.1f}d > {_REMEDIATION_MONITOR_EXPIRY_DAYS}d)"
                        )
                except (TypeError, ValueError):
                    # Unparseable timestamp — safe default is to treat the monitor
                    # as expired so escalation logic does not fire off a bad value.
                    monitor_expired = True
                    d.pop("remediation_monitor_started_at", None)
                    logger.warning(
                        f"[AUTONOMOUS] Cleared unparseable remediation monitor on {did}: "
                        f"{monitor_started_at!r}"
                    )
            # Only escalate when the monitor is active AND the recurring failure matches
            # the fingerprint we already remediated (or fingerprint data is unavailable —
            # conservative fallback so we don't silently drop escalations).
            current_fp = d.get("error_fingerprint")
            last_rem_fp = d.get("last_remediation_fingerprint")
            fingerprints_match = (
                not current_fp or not last_rem_fp or str(current_fp) == str(last_rem_fp)
            )
            if (
                monitor_started_at
                and not monitor_expired
                and d.get("remediation_attempts", 0) >= 3
                and fingerprints_match
            ):
                partial_result = _detect_partial_remediation_success(d)
                if partial_result is True:
                    # Fix materially reduced failure rate even though the
                    # dominant fingerprint is still present — treat as partial
                    # progress so the direction is not escalated to manual
                    # intervention on the next remediation cycle.
                    d["last_remediation_result"] = "partial_success"
                    # Update the most recent merged remediation_history entry so
                    # follow-up investigations and spawn calibration can read the
                    # nuanced outcome ("partial" rather than "fully merged").
                    partial_iso = _utcnow().isoformat()
                    history = list(d.get("remediation_history") or [])
                    for entry in reversed(history):
                        if entry.get("result") == "merged":
                            entry["result"] = "partial_success"
                            entry["partial_success_at"] = partial_iso
                            d["remediation_history"] = history
                            break
                    logger.info(
                        f"[AUTONOMOUS] Direction {did} showed partial remediation success "
                        f"(code-failure rate dropped >= {int(_PARTIAL_SUCCESS_RATE_DROP * 100)}%); "
                        f"skipping manual-intervention escalation"
                    )
                elif partial_result is None:
                    logger.info(
                        f"[AUTONOMOUS] Direction {did} has insufficient research_history "
                        f"to evaluate remediation success; deferring manual-intervention escalation"
                    )
                else:
                    d["needs_manual_intervention"] = True
                    d["needs_manual_intervention_at"] = _utcnow().isoformat()
                    logger.warning(
                        f"[AUTONOMOUS] Direction {did} flagged needs_manual_intervention "
                        f"after {d.get('remediation_attempts', 0)} remediation attempts"
                    )
                    try:
                        from app.notification_dispatch import (
                            post_remediation_escalation_message,
                        )

                        await post_remediation_escalation_message(
                            session,
                            project.id,
                            **_build_escalation_args(d),
                        )
                        # Clear any pending retry stamps from a prior failed escalation
                        d.pop("escalation_notification_pending", None)
                        d.pop("escalation_notification_retries", None)
                    except Exception as _esc_exc:  # noqa: BLE001
                        logger.warning(
                            f"[NOTIFY] Failed to post remediation escalation message: {_esc_exc}"
                        )
                        # Stamp for retry on the next lifecycle cycle. Retry count is
                        # preserved if an earlier escalation on this direction was also
                        # pending; the retry loop enforces _MAX_ESCALATION_NOTIFICATION_RETRIES.
                        d["escalation_notification_pending"] = True
                        d["escalation_notification_retries"] = int(
                            d.get("escalation_notification_retries", 0)
                        )
            logger.info(f"[AUTONOMOUS] Auto-paused direction {did}: {pause_reason}")
            _reconcile_gap_status(
                d,
                new_status="paused",
                prior_status=prior_status,
                rationale=pause_reason,
            )
            auto_paused_any = True
            try:
                if pause_reason.startswith("Lifetime burn backstop:"):
                    from app.notification_dispatch import (
                        post_direction_burn_pause_message,
                    )

                    await post_direction_burn_pause_message(
                        session,
                        project.id,
                        direction_text=d.get("text", ""),
                        reason=pause_reason,
                        direction_id=did,
                        evidence=d.get("lifetime_burn_pause_evidence"),
                    )
                elif pause_reason.startswith("Churn guardrail:"):
                    from app.notification_dispatch import (
                        post_direction_churn_pause_message,
                    )

                    await post_direction_churn_pause_message(
                        session,
                        project.id,
                        direction_text=d.get("text", ""),
                        reason=pause_reason,
                        direction_id=did,
                        evidence=d.get("churn_pause_evidence"),
                    )
                else:
                    from app.notification_dispatch import (
                        post_direction_milestone_message,
                    )

                    await post_direction_milestone_message(
                        session,
                        project.id,
                        direction_text=d.get("text", ""),
                        milestone="auto-paused",
                        reason=pause_reason,
                        direction_id=did,
                    )
            except Exception as _notify_exc:  # noqa: BLE001
                logger.warning(
                    f"[NOTIFY] Failed to post direction pause notification: {_notify_exc}"
                )
            # Zombie churn-family retirement RECOMMENDATION (4th ladder, ref
            # bf6d8788; churn-guardrail redesign d-73f03cc0). A direction that has
            # now been churn/unproductive/saturated-paused >= the threshold has
            # never escaped the recycling-zombie loop. Historically this ladder
            # silently auto-executed a terminal completed/auto_completed state,
            # freeing the slot with zero review. That auto-retire is replaced with
            # a flagged, strategist-reviewable recommendation: the direction stays
            # PAUSED and NON-TERMINAL, its staffing (gap) and memory state are
            # preserved, bounded structured evidence is recorded, and terminal
            # retirement now requires an explicit strategist/operator retire action
            # through the existing retirement paths. Fires once (the flag gates
            # re-firing); the auto-pause notification/evidence above are untouched.
            churn_count = d.get("churn_family_pause_count", 0)
            _churn_recommend_threshold = int(
                _runtime_config.get("direction_review_recommendation_threshold", _MAX_CHURN_PAUSE_CYCLES)
            )
            if churn_count >= _churn_recommend_threshold and not d.get(
                "churn_retirement_recommended"
            ):
                churn_metrics = _compute_churn_metrics(d)
                d["churn_retirement_recommended"] = True
                d["churn_retirement_recommended_at"] = _utcnow().isoformat()
                d["churn_retirement_evidence"] = _build_churn_retirement_evidence(
                    churn_count,
                    _churn_recommend_threshold,
                    pause_reason,
                    churn_metrics,
                )
                # Mirror the structured evidence summary into a human-readable
                # reason so the strategist decision endpoints, operator-attention
                # surface, and DirectionCard (which read
                # churn_retirement_recommended_reason) show a meaningful
                # explanation. Cleared alongside the other recommendation fields
                # on a keep/retire decision (_CHURN_RETIREMENT_RECOMMENDATION_KEYS).
                d["churn_retirement_recommended_reason"] = d[
                    "churn_retirement_evidence"
                ]["summary"]
                # Flag for manual intervention so operator-facing surfaces treat it
                # like the lifetime-burn recommendation (a retire/reset decision is
                # owed), without touching the auto-recovery/reactivation-grace path
                # owned by the separate paused-to-active task.
                if not d.get("needs_manual_intervention"):
                    d["needs_manual_intervention"] = True
                    d["needs_manual_intervention_at"] = _utcnow().isoformat()
                logger.warning(
                    f"[AUTONOMOUS] Direction {did} recommended for churn retirement "
                    f"after {churn_count} churn-family auto-pause cycles — kept paused "
                    f"and non-terminal pending explicit strategist/operator retire "
                    f"action: {d.get('text', '')[:60]}"
                )
                from app.models import Alert, AlertSeverity

                session.add(
                    Alert(
                        project_id=project.id,
                        event_type="research.churn_retirement_recommended",
                        severity=AlertSeverity.INFO,
                        title="Direction churn-retirement recommended",
                        message=(
                            f"Direction '{d.get('text', '')[:80]}' has been "
                            f"{churn_count}x churn/unproductive/saturated auto-paused "
                            f"and is recommended for retirement. It stays paused "
                            f"pending a strategist/operator retire decision — it is "
                            f"no longer auto-retired."
                        ),
                    )
                )
                try:
                    from app.notification_dispatch import (
                        post_direction_milestone_message,
                    )

                    await post_direction_milestone_message(
                        session,
                        project.id,
                        direction_text=d.get("text", ""),
                        milestone="churn-retirement-recommended",
                        reason=d["churn_retirement_evidence"]["summary"],
                        direction_id=did,
                        evidence=d["churn_retirement_evidence"],
                    )
                except Exception as _notify_exc:  # noqa: BLE001
                    logger.warning(
                        f"[NOTIFY] Failed to post churn-retirement recommendation for "
                        f"{did}: {_notify_exc}"
                    )
                directions_modified = True
                # NON-TERMINAL: staffing (gap) and retired-memory state are
                # deliberately preserved — no _reconcile_retired_gap, no
                # _record_auto_retired_memory, no `continue`. Fall through to the
                # paused-direction handling so the direction remains paused.
            # Lifetime-burn auto-retire ladder (5th ladder): a direction that has
            # repeatedly tripped the lifetime burn backstop is structurally stuck —
            # the cumulative non-merge ratio will not improve without a history reset.
            # Rather than holding it open indefinitely for manual action, auto-retire
            # it so the slot can be reused. The burn-pause notification already fired
            # above; post the retirement milestone and clear the manual-hold flags.
            lb_pause_count = d.get("lifetime_burn_pause_count", 0)
            if lb_pause_count >= _MAX_LIFETIME_BURN_PAUSE_CYCLES:
                await _retire_lifetime_burn(d, did, lb_pause_count)
                directions_modified = True
                continue  # terminal — skip active_directions.append
            MAX_AUTO_PAUSE_CYCLES = _MAX_AUTO_PAUSE_CYCLES
            code_pause_count = d.get("code_failure_pause_count", 0)
            if code_pause_count >= MAX_AUTO_PAUSE_CYCLES:
                _reason = (
                    f"Auto-retired: direction hit {code_pause_count} code-failure "
                    f"auto-pauses without producing results. Slot freed for new directions."
                )
                mark_auto_direction_terminal(
                    d,
                    outcome="failed",
                    reason=_reason,
                    completed_at=_utcnow().isoformat(),
                )
                logger.info(
                    f"[AUTONOMOUS] Auto-retired direction {did} after "
                    f"{code_pause_count} code-failure auto-pause cycles: {d.get('text', '')[:60]}"
                )
                from app.alert_policy import create_project_alert_if_allowed
                from app.models import AlertSeverity

                # TIMELINE_ONLY via the policy gate; the milestone message below
                # is the delivery.
                await create_project_alert_if_allowed(
                    session,
                    project_id=project.id,
                    event_type="research.direction_auto_retired",
                    severity=AlertSeverity.INFO,
                    title="Direction auto-retired",
                    message=(
                        f"Direction '{d.get('text', '')[:80]}' was auto-retired after "
                        f"{code_pause_count} consecutive code-failure auto-pause cycles. "
                        f"Slot freed for strategic agent to create a replacement."
                    ),
                )
                try:
                    from app.notification_dispatch import (
                        post_direction_milestone_message,
                    )

                    await post_direction_milestone_message(
                        session,
                        project.id,
                        direction_text=d.get("text", ""),
                        milestone="auto-retired",
                        reason=d["auto_completed_reason"],
                        direction_id=did,
                    )
                except Exception as _notify_exc:  # noqa: BLE001
                    logger.warning(
                        f"[NOTIFY] Failed to post direction milestone message: {_notify_exc}"
                    )
                # Prune the now-completed direction out of any gap it was
                # staffing — same terminal invariant the zombie/strategic
                # retires preserve. Fail-open.
                _reconcile_retired_gap(did)
                _record_auto_retired_memory(
                    pending_retired_memory, d, "auto_retired_code_failure"
                )
                directions_modified = True
            # The auto-paused direction's diagnosis-research spawn was removed
            # with the detect->investigate->fix apparatus strip. The pause itself
            # (and its operator notification, posted above) is the preserved
            # signal; the strategic agent / operator decides any follow-up.
            continue
        # Positive completion detection — mark for user review but don't pause.
        # Scoped objective verdict (campaign path): when the
        # direction's integrated merged diff has been graded against its declared
        # objective, the graded verdict is AUTHORITATIVE over the merge-count
        # heuristic below — a graded not-met objective must not let the campaign
        # 'complete' on child-merge counts, and a graded fully-met objective marks
        # it complete regardless of recent cycle yield.
        try:
            if await consume_direction_merge_grade_if_ready(session, project, d):
                directions_modified = True
        except Exception:  # noqa: BLE001 — best-effort consumption
            logger.warning(
                f"[AUTONOMOUS] Failed to consume direction merge grade for {did}",
                exc_info=True,
            )
        _objective_verdict = await direction_objective_completion_verdict(
            session, project, d
        )
        if _objective_verdict is True:
            if not d.get("possibly_complete"):
                d["possibly_complete"] = True
                d["completion_suggestion"] = (
                    "Direction objective graded fully met on the integrated "
                    "merged diff."
                )
                logger.info(
                    f"[AUTONOMOUS] Direction {did} objective graded fully met — "
                    "marking possibly_complete"
                )
                directions_modified = True
        elif _objective_verdict is False:
            # A graded not-met objective is authoritative: suppress any heuristic
            # completion signal so the campaign does not 'complete' while its
            # objective remains unmet.
            if d.get("possibly_complete"):
                d.pop("possibly_complete", None)
                d.pop("completion_suggestion", None)
                directions_modified = True
        else:
            # Two signals feed possibly_complete: the merge-based heuristic
            # (_check_direction_completion) and the Fix #3 "already-built" signature
            # (no-op-dominated completions). The already-built signal is the reason a
            # churn/zero-merge pause was just suppressed above, so it must set the flag
            # here; otherwise the else-branch below would immediately pop it.
            completion_suggestion = _check_direction_completion(
                d
            ) or already_built_completion_suggestion(d)
            if completion_suggestion:
                if not d.get("possibly_complete"):
                    d["possibly_complete"] = True
                    d["completion_suggestion"] = completion_suggestion
                    logger.info(
                        f"[AUTONOMOUS] Direction {did} possibly complete: {completion_suggestion}"
                    )
                    directions_modified = True
            else:
                if d.get("possibly_complete"):
                    d.pop("possibly_complete", None)
                    d.pop("completion_suggestion", None)
                    directions_modified = True
        # Outcome-progress (scaffolding) detection — advisory signal.
        # Flags a long-running healthy-merge direction whose outcome signal has
        # stalled. It records evidence and notifies on entry/escalation, but it
        # never changes status, suppresses research, or authorizes retirement.
        # An advancing/met evidence_progress verdict suppresses the counter.
        scaffolding_ev = _check_outcome_scaffolding(d)
        if scaffolding_ev:
            if not d.get("outcome_scaffolding_flagged"):
                d["outcome_scaffolding_flagged"] = True
                d["outcome_scaffolding_flagged_at"] = _utcnow().isoformat()
                d["outcome_scaffolding_evidence"] = scaffolding_ev
                d["outcome_scaffolding_last_cycle_count"] = scaffolding_ev[
                    "total_cycles"
                ]
                directions_modified = True
                logger.warning(
                    f"[AUTONOMOUS] Direction {did} flagged outcome-less scaffolding: "
                    f"{scaffolding_ev['total_cycles']} cycles, "
                    f"{scaffolding_ev['lifetime_merged']} merged "
                    f"(rate {scaffolding_ev['lifetime_merge_rate']:.0%}), "
                    f"progress stalled {scaffolding_ev['stall_run_cycles']} cycles"
                )
                try:
                    from app.notification_dispatch import (
                        post_direction_outcome_stalled_message,
                    )

                    await post_direction_outcome_stalled_message(
                        session,
                        project.id,
                        direction_text=d.get("text", ""),
                        evidence=scaffolding_ev,
                        direction_id=did,
                    )
                except Exception as _notify_exc:  # noqa: BLE001
                    logger.warning(
                        f"[NOTIFY] Failed to post outcome-stalled message for {did}: "
                        f"{_notify_exc}"
                    )
                # Spawn one memo task to capture this signal as a citable artifact.
                # The flag already gates first-detection only so no dedup query needed.
                from app.task_locking import (
                    AUTOMATIC_TASK_CREATION_ALLOWED,
                    automatic_task_creation_decision,
                )

                creation_admission = automatic_task_creation_decision(project)
                if creation_admission != AUTOMATIC_TASK_CREATION_ALLOWED:
                    logger.info(
                        "[AUTONOMOUS] Outcome-scaffolding memo held for direction %s: %s",
                        did,
                        creation_admission,
                    )
                else:
                    try:
                        from app.plan_completion import TASK_PRIORITY_MAP
                        from app.task_completion import _apply_synthesized_memo

                        _scaffolding_topic = (d.get("text") or "")[
                            :120
                        ] or f"Direction {did}"
                        _scaffolding_payload = validate_internal_payload(
                            {
                                "type": "memo",
                                "topic": f"Outcome-less scaffolding: {_scaffolding_topic}",
                                "reasoning_kind": "outcome_scaffolding",
                                "direction_id": did,
                                "scaffolding_evidence": scaffolding_ev,
                            },
                            "direction_lifecycle:memo:outcome_scaffolding",
                        )
                        _scaffolding_memo = Task(
                            project_id=project.id,
                            title=f"Memo: outcome-less scaffolding — {_scaffolding_topic[:80]}",
                            description="Reasoning memo for outcome-less scaffolding detection",
                            payloads=[_scaffolding_payload],
                            context={
                                "memo_reasoning_kind": "outcome_scaffolding",
                                "memo_source_direction_id": did,
                                "outcome_scaffolding_evidence": scaffolding_ev,
                            },
                            status=TaskStatus.QUEUED,
                            created_by="ORCHESTRATOR",
                            origination=TASK_ORIGINATION_RESEARCH,
                            priority=TASK_PRIORITY_MAP.get("low", 0),
                        )
                        session.add(_scaffolding_memo)
                        _synth_memo = _synthesize_outcome_scaffolding_memo(
                            did, _scaffolding_topic, scaffolding_ev
                        )
                        _apply_synthesized_memo(_scaffolding_memo, _synth_memo, session)
                        logger.info(
                            "[AUTONOMOUS] Spawned outcome-scaffolding memo task %s for direction %s",
                            _scaffolding_memo.id,
                            did,
                        )
                    except Exception:  # noqa: BLE001
                        try:  # noqa: SIM105
                            _scaffolding_memo.status = TaskStatus.FAILED
                        except Exception:  # noqa: BLE001, S110
                            pass
                        logger.warning(
                            f"[AUTONOMOUS] Failed to spawn scaffolding memo for direction {did}",
                            exc_info=True,
                        )
            else:
                # Keep the evidence fresh while the flag remains set.
                d["outcome_scaffolding_evidence"] = scaffolding_ev
                # Advisory persistence counter: increment each cycle the flag holds.
                _scaffolding_cfg = _outcome_progress_config()
                _esc_threshold = _scaffolding_cfg["escalation_cycles"]
                # A RECENT advancing/met evidence_progress verdict suppresses the
                # counter increment — uses the same recency-aware helper as the
                # detector so stale verdicts grant no credit (consistent semantics).
                _ep_suppresses = _evidence_progress_suppresses_scaffolding(
                    d,
                    scaffolding_ev["total_cycles"],
                    scaffolding_ev["stall_run_cycles"],
                )
                if not _ep_suppresses:
                    _advanced_flag_cycle = False
                    _current_cycles = scaffolding_ev["total_cycles"]
                    _last_counted = d.get("outcome_scaffolding_last_cycle_count")
                    if _last_counted is None:
                        # Legacy flagged records predate the watermark field.
                        # Backfill from the current completed-cycle count so
                        # this tick does not miscount a stale cycle as new work.
                        d["outcome_scaffolding_last_cycle_count"] = _current_cycles
                        _last_counted = _current_cycles
                        directions_modified = True
                    if _current_cycles <= _last_counted:
                        # No new completed research cycle — skip increment.
                        pass
                    else:
                        _flag_cycles = (
                            d.get("outcome_scaffolding_flagged_cycles", 0) + 1
                        )
                        d["outcome_scaffolding_flagged_cycles"] = _flag_cycles
                        d["outcome_scaffolding_last_cycle_count"] = _current_cycles
                        _advanced_flag_cycle = True
                        directions_modified = True
                    _flag_cycles = d.get("outcome_scaffolding_flagged_cycles", 0)
                    if _advanced_flag_cycle and _flag_cycles == _esc_threshold:
                        # Escalation notification at mid-threshold.
                        try:
                            from app.notification_dispatch import (
                                post_direction_outcome_scaffolding_escalation_message,
                            )

                            await post_direction_outcome_scaffolding_escalation_message(
                                session,
                                project.id,
                                direction_text=d.get("text", ""),
                                flagged_cycles=_flag_cycles,
                                evidence=scaffolding_ev,
                                direction_id=did,
                            )
                        except Exception as _esc_notify_exc:  # noqa: BLE001
                            logger.warning(
                                f"[NOTIFY] Failed to post scaffolding escalation for {did}: {_esc_notify_exc}"
                            )
        elif d.get("outcome_scaffolding_flagged"):
            d.pop("outcome_scaffolding_flagged", None)
            d.pop("outcome_scaffolding_flagged_at", None)
            d.pop("outcome_scaffolding_evidence", None)
            d.pop("outcome_scaffolding_flagged_cycles", None)
            d.pop("outcome_scaffolding_last_cycle_count", None)
            directions_modified = True
            logger.info(
                f"[AUTONOMOUS] Cleared outcome-scaffolding flag on {did}: "
                f"condition no longer holds"
            )
        # Crash guard: skip if a research task for this direction completed within the
        # cooldown window — covers the gap when _handle_research_completion dies before
        # stamping refractory_until (incident 2026-06-10).
        if did and await _directed_research_recently_completed(
            project.id, did, session, direction=d
        ):
            logger.info(
                "[RESEARCH] Skipping direction %s: a research task completed within the "
                "cooldown window (crash guard)",
                did,
            )
            continue
        active_directions.append(d)
    if auto_paused_any or directions_modified:
        flag_modified(project, "directions")
        session.add(project)
        # P2 (p2-direction-table): dual-write the normalised ``direction`` table
        # so it stays consistent with the authoritative blob after lifecycle
        # mutations (auto-pause, auto-retire, prune). Best-effort — a sync
        # failure must never break the blob write.
        try:
            from app.directions_repo import sync_directions

            await sync_directions(
                session, project.id, list(project.directions or [])
            )
        except Exception as exc:  # noqa: BLE001  # pragma: no cover - defensive
            logger.warning(
                "[direction_lifecycle] direction-table dual-write sync failed "
                "for project %s (blob write unaffected): %s",
                project.id,
                exc,
            )
        if expected_legacy_generation is None:
            await session.commit()
        else:
            await commit_legacy_authorized(
                session, project.id, expected_legacy_generation
            )
    return active_directions
