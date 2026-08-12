"""Pre-spawn effort classifier for strategic_review cycles.

Every strategic_review cycle historically ran at effort 'max' (gpt-5.6-sol / xhigh
via the 'strategic' category default). Measured baseline (see
docs/strategic-cycle-cost-baseline.md): mean cost ~$7.52 per cycle; strategic_review
is approximately 47% of total LLM spend. This module decides, from signals
available BEFORE the LLM call, whether a cycle is ROUTINE (a cheaper tier suffices)
or CONSEQUENTIAL (stays at 'max').

Fail-closed: any unknown/missing signal, or any high-stakes condition, resolves to
CONSEQUENTIAL/'max'. ROUTINE requires ALL low-stakes conditions to hold.
"""

from __future__ import annotations

from typing import Any, Mapping

# The cheaper effort tier routine cycles run at. 'medium' resolves through
# provider_settings_by_effort to a materially cheaper model cell than 'max'
# (e.g. openai gpt-5.6-terra/medium vs gpt-5.6-sol/xhigh).
ROUTINE_EFFORT = "medium"
CONSEQUENTIAL_EFFORT = "max"

ROUTINE_TIER = "routine"
CONSEQUENTIAL_TIER = "consequential"

# Version qualification states that still need a strategist decision this cycle.
# Any state outside the settled/in-flight vocabulary is treated as
# decision-pending too (fail-closed), so a novel or unrecognized state can never
# slip a consequential cycle onto a cheap tier.
_DECISION_PENDING_QUAL_STATES = frozenset(
    {
        "audit_crashed",
        "promotion_failed",
        "required_check_failed",
        "protection_blocked_review",
        "not_promoted",
        "promotion_deploy_failed",
        "promotion_confirmation_timed_out",
        "deferred_unsynced",
        "error",
        "infra_failure",
        "rubric_changed",
        "rubric_provenance_unknown",
    }
)

# States that do NOT force consequential by themselves: the version is settled
# (promoted / dry_run) or its machinery is actively in flight (qualifying /
# promotion_check_wait) — neither needs a strategist decision THIS cycle.
_SETTLED_OR_IN_FLIGHT_QUAL_STATES = frozenset(
    {
        "promoted",
        "promoted_unconfirmed",
        "dry_run",
        "qualifying",
        "promotion_check_wait",
    }
)

# Integrity-conflict classes counted at cycle start (mirrors
# ``_build_cycle_start_reconciliation`` counts).
_INTEGRITY_CONFLICT_KEYS = (
    "orphaned_gaps",
    "ungrounded_directions",
    "stale_active_moves",
    "staffed_but_deferred",
)


def classify_strategic_review_effort(
    signals: Mapping[str, Any],
) -> tuple[str, str, str]:
    """Return ``(effort_level, tier, reason)`` for a strategic_review cycle.

    ``signals`` must carry the pre-spawn signals the cadence spawn path computes
    before the payload pack. ROUTINE requires ALL of:

      * no open operator messages (``open_message_count`` == 0)
      * not operator-steered (``operator_steered`` falsy)
      * no version decision pending (``version_phase_block`` absent, or goal set,
        no active qualification, and ``qualification_state`` settled/in-flight)
      * zero integrity conflicts at cycle start (``integrity_conflict_counts``
        all zero or absent)
      * the prior completed cycle was clean (``prior_cycle_dirty`` falsy)
      * no recent cycle failures (``recent_cycle_failures`` falsy)

    Any signal missing/unknown, or any condition unmet, resolves to
    CONSEQUENTIAL (fail-closed) so a cheapened cycle is never trusted on
    incomplete context.
    """
    if not isinstance(signals, Mapping):
        return CONSEQUENTIAL_EFFORT, CONSEQUENTIAL_TIER, "signals missing"

    reasons: list[str] = []

    # 1. Open operator messages — an operator is waiting on a decision.
    open_count = signals.get("open_message_count")
    if open_count is None:
        reasons.append("open_message_count unknown")
    elif open_count:
        reasons.append(f"{open_count} open messages")

    # 2. Operator-steered wake — explicit operator attention.
    if signals.get("operator_steered"):
        reasons.append("operator_steered")

    # 3. Version decision pending.
    version_phase = signals.get("version_phase_block")
    if version_phase is None:
        pass  # No LTS split / no version surfaced — not a consequential signal.
    elif not isinstance(version_phase, Mapping):
        reasons.append("version_phase malformed")
    else:
        if version_phase.get("build_unavailable"):
            reasons.append("version_phase build unavailable")
        if version_phase.get("goal_needed"):
            reasons.append("version goal needed")
        if version_phase.get("active_qualification_attempt_id"):
            reasons.append("active qualification in flight")
        raw_qual_state = version_phase.get("qualification_state")
        # The version-phase block carries the full ``summarize_qualification_state``
        # dict (``{"state", "summary", ...}``); normalize to the state string so a
        # string form (other callers) and the dict form classify identically.
        qual_state = (
            raw_qual_state.get("state")
            if isinstance(raw_qual_state, Mapping)
            else raw_qual_state
        )
        if isinstance(qual_state, str):
            if qual_state in _DECISION_PENDING_QUAL_STATES:
                reasons.append(f"qualification_state={qual_state}")
            elif qual_state not in _SETTLED_OR_IN_FLIGHT_QUAL_STATES:
                # Unknown/novel state — fail closed rather than trusting it.
                reasons.append(f"qualification_state={qual_state}")
        elif qual_state is not None:
            # Unrecognized shape — fail closed rather than trusting it.
            reasons.append("qualification_state malformed")

    # 4. Integrity conflicts at cycle start.
    counts = signals.get("integrity_conflict_counts")
    if counts is not None:
        if not isinstance(counts, Mapping):
            reasons.append("integrity_conflict_counts malformed")
        else:
            total = sum(int(counts.get(k) or 0) for k in _INTEGRITY_CONFLICT_KEYS)
            if total:
                reasons.append(f"{total} integrity conflicts")

    # 5. Prior completed cycle was dirty — this cycle is a recovery decision.
    if signals.get("prior_cycle_dirty"):
        reasons.append("prior cycle dirty")

    # 6. Recent cycle failures — this cycle is a recovery decision.
    if signals.get("recent_cycle_failures"):
        reasons.append("recent cycle failures")

    if reasons:
        return CONSEQUENTIAL_EFFORT, CONSEQUENTIAL_TIER, "; ".join(reasons)
    return ROUTINE_EFFORT, ROUTINE_TIER, "all routine signals clean"
