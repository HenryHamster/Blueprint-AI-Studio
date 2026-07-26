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


def direction_has_attempt_evidence(direction: dict[str, Any]) -> bool:
    """Return whether a direction has any durable evidence of an attempted cycle."""
    counters = (
        "total_cycles",
        "tasks_created",
        "prs_merged",
        "prs_rejected",
        "tasks_failed",
    )
    for key in counters:
        try:
            if int(direction.get(key) or 0) > 0:
                return True
        except (TypeError, ValueError):
            continue
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
