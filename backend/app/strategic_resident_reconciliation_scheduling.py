"""Meaningful deadline scheduling derived from reconciled Resident state."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from app.models import StrategicResident
from app.strategic_resident_reconciliation_validation import (
    ReconciliationResultInvalid,
)
from app.strategic_resident_turn_results import StrategicResidentTurnResult


def _parse_timestamp(value: Any, *, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ReconciliationResultInvalid(f"{field} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise ReconciliationResultInvalid(
            f"{field} must be an ISO-8601 timestamp"
        ) from None
    if parsed.tzinfo is not None:
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _deadline_candidates(
    body: Mapping[str, Any],
    result: StrategicResidentTurnResult,
) -> list[tuple[datetime, str]]:
    candidates: list[tuple[datetime, str]] = []
    if result.follow_up and result.follow_up.next_wakeup_at:
        candidates.append(
            (
                _parse_timestamp(
                    result.follow_up.next_wakeup_at,
                    field="follow_up.next_wakeup_at",
                ),
                "follow_up",
            )
        )
    strategy = body.get("working_strategy")
    if not isinstance(strategy, Mapping):
        raise ReconciliationResultInvalid("working_strategy is malformed")
    for index, item in enumerate(list(strategy.get("open_loops") or [])[:100]):
        if not isinstance(item, Mapping) or not item.get("due_at"):
            continue
        candidates.append(
            (
                _parse_timestamp(
                    item["due_at"],
                    field=f"working_strategy.open_loops[{index}].due_at",
                ),
                "open_loop_deadline",
            )
        )
    return candidates


def schedule_meaningful_follow_up(
    resident: StrategicResident,
    *,
    body: Mapping[str, Any],
    result: StrategicResidentTurnResult,
    turn_task_id: uuid.UUID,
    now: datetime,
) -> datetime | None:
    """Persist the earliest explicit follow-up or open-loop deadline."""

    candidates = _deadline_candidates(body, result)
    if not candidates:
        return None
    deadline, source = min(candidates, key=lambda item: (item[0], item[1]))
    effective = now if deadline <= now else deadline
    reason = "overdue_commitment" if deadline <= now else source
    scheduling = dict(resident.scheduling_state or {})
    scheduling.update(
        {
            "deadline_at": deadline.isoformat(),
            "deadline_reason": reason,
            "deadline_source": source,
            "deadline_source_turn_task_id": str(turn_task_id),
            "deadline_scheduled_at": now.isoformat(),
        }
    )
    resident.scheduling_state = scheduling
    if resident.background_policy == "active":
        existing = resident.next_wakeup_at
        resident.next_wakeup_at = (
            min(existing, effective) if existing is not None else effective
        )
    else:
        resident.next_wakeup_at = None
    return resident.next_wakeup_at


__all__ = ["schedule_meaningful_follow_up"]
