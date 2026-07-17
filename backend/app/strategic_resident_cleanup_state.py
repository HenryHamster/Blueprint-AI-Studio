"""State helpers for durable Resident transition cleanup intents."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any

from app.models import StrategicResident


CLAIM_LEASE = timedelta(minutes=5)
SETTLED_CLEANUP_STATUSES = frozenset({"complete"})


def cleanup_items(resident: StrategicResident) -> list[dict[str, Any]]:
    state = resident.recovery_state if isinstance(resident.recovery_state, dict) else {}
    raw = state.get("task_cleanup")
    if not isinstance(raw, list):
        return []
    return [deepcopy(item) for item in raw if isinstance(item, dict)]


def replace_cleanup_items(
    resident: StrategicResident,
    items: list[dict[str, Any]],
    *,
    now: datetime,
) -> None:
    state = dict(resident.recovery_state or {})
    state["task_cleanup"] = items
    state["task_cleanup_updated_at"] = now.isoformat()
    resident.recovery_state = state
    statuses = {str(item.get("status") or "") for item in items}
    if "unknown_effect" in statuses:
        resident.health_status = "blocked"
    elif statuses & {"pending", "applying", "failed"}:
        resident.health_status = "recovering"
    elif items and statuses <= SETTLED_CLEANUP_STATUSES:
        resident.health_status = "unknown"
    resident.updated_at = now


def cleanup_claim_is_live(
    item: dict[str, Any],
    *,
    authority_generation: int,
    now: datetime,
) -> bool:
    if (
        not item.get("claim_id")
        or int(item.get("authority_generation") or -1) != authority_generation
    ):
        return False
    raw_claimed_at = item.get("claimed_at")
    if not isinstance(raw_claimed_at, str):
        return False
    try:
        claimed_at = datetime.fromisoformat(raw_claimed_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if claimed_at.tzinfo is not None:
        claimed_at = claimed_at.astimezone(timezone.utc).replace(tzinfo=None)
    return now - claimed_at <= CLAIM_LEASE


def unsettled_cleanup_descriptors(resident: StrategicResident) -> list[str]:
    recovery = resident.recovery_state if isinstance(resident.recovery_state, dict) else {}
    raw_items = recovery.get("task_cleanup")
    if raw_items is None:
        return []
    if not isinstance(raw_items, list):
        return ["state:malformed"]
    unsettled: list[str] = []
    for index, item in enumerate(raw_items):
        if not isinstance(item, dict):
            unsettled.append(f"item[{index}]:malformed")
            continue
        task_id = str(item.get("task_id") or f"item[{index}]")
        status = str(item.get("status") or "missing")
        if status not in SETTLED_CLEANUP_STATUSES:
            unsettled.append(f"{task_id}:{status}")
    return unsettled


__all__ = [
    "cleanup_claim_is_live",
    "cleanup_items",
    "replace_cleanup_items",
    "unsettled_cleanup_descriptors",
]
