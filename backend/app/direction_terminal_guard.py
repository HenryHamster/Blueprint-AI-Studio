"""Shared guardrails for terminal direction transitions."""

from __future__ import annotations

from typing import Any


ZERO_ATTEMPT_TERMINAL_REASONS: frozenset[str] = frozenset(
    {
        "duplicate",
        "superseded",
        "invalidated",
        "explicit_abandonment",
    }
)

# Existing direction terminal-outcome field vocabulary.  ``status`` answers
# whether work is active; this value answers *how it ended* so exhausted or
# failed work is never mistaken for successful completion.
DIRECTION_TERMINAL_OUTCOMES: frozenset[str] = frozenset(
    {
        "success_evidence_met",
        "stopped",
        "failed",
        "exhausted",
        "obsolete",
        "pivoted",
    }
)
SUCCESS_DIRECTION_TERMINAL_OUTCOME = "success_evidence_met"


def direction_success_evidence_met(direction: dict[str, Any]) -> bool:
    """Return whether the direction's declared success evidence was met.

    Merge/activity counters are deliberately absent.  The existing Strategic
    evidence-progress decision is the authoritative judgment over the current
    ``context.success_evidence`` declaration, and a ``met`` judgment counts only
    when its independent challenge approved that exact assessment.  This avoids
    introducing a second proof ledger while preventing a bare merge threshold or
    an unchallenged self-rating from claiming success.
    """
    context = direction.get("context")
    if not isinstance(context, dict):
        return False
    if not str(context.get("success_evidence") or "").strip():
        return False
    progress = direction.get("evidence_progress")
    return bool(
        isinstance(progress, dict)
        and str(progress.get("status") or "").strip().lower() == "met"
        and progress.get("challenge_approved") is True
    )


def mark_auto_direction_terminal(
    direction: dict[str, Any],
    *,
    outcome: str,
    reason: str,
    completed_at: str,
) -> None:
    """Stamp one explicit automatic terminal outcome on a direction.

    Successful completion retains ``status='completed'``.  Every other outcome
    uses neutral terminal ``status='retired'`` and remains distinguishable via
    the existing ``retirement_outcome`` field.  ``auto_completed`` is retained
    as the established automatic-transition marker consumed by lifecycle/UI
    code; it no longer determines whether the outcome was successful.
    """
    normalized = str(outcome or "").strip().lower()
    if normalized not in DIRECTION_TERMINAL_OUTCOMES:
        raise ValueError(f"invalid direction terminal outcome: {outcome!r}")
    direction["status"] = (
        "completed"
        if normalized == SUCCESS_DIRECTION_TERMINAL_OUTCOME
        else "retired"
    )
    direction["retirement_outcome"] = normalized
    direction["completed_at"] = completed_at
    direction["auto_completed"] = True
    direction["auto_completed_reason"] = reason


def direction_has_attempt_evidence(direction: dict[str, Any]) -> bool:
    """Return whether a direction has any durable evidence of an attempted cycle."""
    def _is_positive_count(value: Any) -> bool:
        try:
            return int(value or 0) > 0
        except (TypeError, ValueError):
            return False

    counters = (
        "total_cycles",
        "tasks_created",
        "prs_merged",
        "prs_rejected",
        "tasks_failed",
    )
    if any(_is_positive_count(direction.get(key)) for key in counters):
        return True
    history = direction.get("research_history")
    return isinstance(history, list) and len(history) > 0


def validate_direction_terminal_transition(
    direction: dict[str, Any],
    *,
    rationale: str | None,
    terminal_reason: str | None,
) -> tuple[bool, str | None]:
    """Require an outcome after attempts, or a typed reason before any attempt."""
    rationale_text = str(rationale or "").strip()
    reason = str(terminal_reason or "").strip().lower()

    if direction_has_attempt_evidence(direction):
        if rationale_text:
            return True, None
        return False, "attempted_direction_requires_terminal_outcome"

    if reason not in ZERO_ATTEMPT_TERMINAL_REASONS:
        return False, "zero_attempt_direction_requires_typed_terminal_reason"
    if not rationale_text:
        return False, "terminal_reason_requires_rationale"
    return True, None
