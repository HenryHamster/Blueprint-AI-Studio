"""Bounded health and retry state for Strategic Resident turn failures."""

from __future__ import annotations

import uuid
from typing import Any

from app.models import StrategicResident


def reset_resident_failure_recovery(
    resident: StrategicResident,
    *,
    recovered_at: Any,
) -> None:
    """Reset only failure-retry state after a clean reconciled turn."""
    health = dict(resident.health_state or {})
    health["consecutive_turn_failures"] = 0
    resident.health_state = health
    recovery = dict(resident.recovery_state or {})
    if "failure_count" in recovery:
        # A successful turn supersedes the failed turn's retry timer. Any
        # still-meaningful deadline is scheduled after this reset.
        resident.next_wakeup_at = None
        recovery.update(
            {
                "status": "recovered",
                "failure_count": 0,
                "recovered_at": recovered_at.isoformat(),
            }
        )
        resident.recovery_state = recovery
        if resident.health_status in {"recovering", "blocked"}:
            resident.health_status = "healthy"


def stored_promoted_task_id(failure: object) -> uuid.UUID | None:
    """Read the original promoted successor from an idempotency marker."""
    if not isinstance(failure, dict) or not failure.get("promoted_task_id"):
        return None
    try:
        return uuid.UUID(str(failure["promoted_task_id"]))
    except (TypeError, ValueError):
        return None


def failure_backoff_seconds(category: str, count: int) -> int:
    """Return the bounded retry delay for a consecutive failure."""
    base = 300 if category == "rate_limited" else 60
    return min(1_800, base * (2 ** min(max(count - 1, 0), 5)))


__all__ = [
    "failure_backoff_seconds",
    "reset_resident_failure_recovery",
    "stored_promoted_task_id",
]
