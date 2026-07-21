"""Meaningful deadline scheduling derived from reconciled Resident state."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from app.models import StrategicResident
from app.strategic_resident_reconciliation_cadence import (
    DEADLINE_STATE_KEYS,
    decide_cadence,
)
from app.strategic_resident_turn_results import StrategicResidentTurnResult


def _clear_deadline_state(resident: StrategicResident) -> None:
    scheduling = dict(resident.scheduling_state or {})
    for key in DEADLINE_STATE_KEYS:
        scheduling.pop(key, None)
    resident.scheduling_state = scheduling
    resident.next_wakeup_at = None


def schedule_meaningful_follow_up(
    resident: StrategicResident,
    *,
    body: Mapping[str, Any],
    result: StrategicResidentTurnResult,
    turn_task_id: uuid.UUID,
    now: datetime,
) -> datetime | None:
    """Persist the next cadence selected from the effective checkpoint."""

    scheduling = dict(resident.scheduling_state or {})
    decision = decide_cadence(
        body=body,
        result=result,
        scheduling_state=scheduling,
        now=now,
    )
    if decision is None:
        _clear_deadline_state(resident)
        return None

    # This turn's effective checkpoint supersedes a wake preserved at pause.
    scheduling.pop("paused_next_wakeup_at", None)
    scheduling.update(
        {
            "deadline_at": decision.effective_at.isoformat(),
            "deadline_reason": decision.reason,
            "deadline_source": decision.source,
            "deadline_source_turn_task_id": str(turn_task_id),
            "deadline_scheduled_at": now.isoformat(),
            "continuation_fingerprint": decision.fingerprint,
            "continuation_unchanged_count": decision.unchanged_count,
            "continuation_backoff_seconds": int(decision.backoff.total_seconds()),
        }
    )
    resident.scheduling_state = scheduling
    resident.next_wakeup_at = (
        decision.effective_at if resident.background_policy == "active" else None
    )
    return resident.next_wakeup_at


__all__ = ["schedule_meaningful_follow_up"]
