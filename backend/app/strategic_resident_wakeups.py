"""Meaningful background wakeups for the authoritative Resident."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

from app.models import StrategicResident
from app.strategic_resident_runtime import coalesce_authoritative_resident_turn
from app.strategic_resident_turns import (
    ResidentTurnWake,
    _ensure_resident_locked,
    _merge_project_change_cursor,
)
from app.time_utils import _utcnow


async def coalesce_meaningful_background_wakeup(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    trigger_kind: str,
    now: datetime | None = None,
    project_change_cursor: dict[str, Any] | None = None,
) -> ResidentTurnWake | None:
    """Enqueue only a changed cursor or a genuinely due commitment."""

    resident = await _ensure_resident_locked(session, project_id)
    if resident.engine_owner != "strategic_resident":
        return None
    if trigger_kind in {"deadline", "follow_up"}:
        due_at = resident.next_wakeup_at
        if due_at is None or due_at > (now or _utcnow()):
            return None
    elif trigger_kind == "project_change":
        incoming = _merge_project_change_cursor({}, project_change_cursor)
        if not incoming:
            return None
        represented = _merge_project_change_cursor(
            {}, resident.handled_project_change_cursor
        )
        if _merge_project_change_cursor(represented, incoming) == represented:
            return None
    else:
        raise ValueError("background wake must be project_change, deadline, or follow_up")
    wake = await coalesce_authoritative_resident_turn(
        session,
        project_id=project_id,
        lane="background",
        trigger_kind=trigger_kind,
        project_change_cursor=project_change_cursor,
        locked_resident=resident,
    )
    if trigger_kind in {"deadline", "follow_up"} and wake.state != "paused":
        resident.next_wakeup_at = None
    return wake


async def sweep_due_authoritative_resident_wakeups(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    now: datetime | None = None,
    batch_size: int = 32,
) -> int:
    """Reaper hook: unchanged projects make zero Tasks and zero model calls."""

    observed_at = now or _utcnow()
    async with session_factory() as scan:
        project_ids = list(
            (
                await scan.execute(
                    select(StrategicResident.project_id)
                    .where(
                        StrategicResident.engine_owner == "strategic_resident",
                        StrategicResident.background_policy == "active",
                        StrategicResident.next_wakeup_at.is_not(None),
                        StrategicResident.next_wakeup_at <= observed_at,
                    )
                    .order_by(StrategicResident.next_wakeup_at.asc())
                    .limit(max(1, min(batch_size, 200)))
                )
            ).scalars()
        )
    queued = 0
    for project_id in project_ids:
        async with session_factory() as session, session.begin():
            wake = await coalesce_meaningful_background_wakeup(
                session,
                project_id=project_id,
                trigger_kind="deadline",
                now=observed_at,
            )
            if wake is not None and wake.task_id is not None:
                queued += 1
    return queued


__all__ = [
    "coalesce_meaningful_background_wakeup",
    "sweep_due_authoritative_resident_wakeups",
]
