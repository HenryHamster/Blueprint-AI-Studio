"""Meaningful background wakeups for the authoritative Resident."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import func
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


async def sweep_pending_worker_proposal_wakeups(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    now: datetime | None = None,
    batch_size: int = 32,
) -> int:
    """Wake the background Resident for projects with pending worker proposals.

    A concrete production trigger for the ``project_change`` lane: when a bounded
    worker has submitted a strategic-memory proposal (see
    ``strategic_resident_worker_proposal_intake``), the owning Resident should wake
    and reconcile it. The cursor is the monotonic count of ALL proposals for the
    project, so it advances on every new submission (no same-timestamp stranding)
    and, once the woken turn reconciles/drains them, an unchanged count is a
    zero-model no-op via the coalescer's unchanged-cursor guard.
    """

    from app.models import StrategicResident as _Resident
    from app.models import StrategicWorkerMemoryProposal as _Proposal

    async with session_factory() as scan:
        project_ids = list(
            (
                await scan.execute(
                    select(_Proposal.project_id)
                    .join(
                        _Resident,
                        _Resident.project_id == _Proposal.project_id,
                    )
                    .where(
                        _Proposal.status == "pending",
                        _Resident.engine_owner == "strategic_resident",
                        _Resident.background_policy == "active",
                    )
                    .group_by(_Proposal.project_id)
                    .limit(max(1, min(batch_size, 200)))
                )
            ).scalars()
        )
    queued = 0
    for project_id in project_ids:
        async with session_factory() as count_session:
            total = (
                await count_session.execute(
                    select(func.count())
                    .select_from(_Proposal)
                    .where(_Proposal.project_id == project_id)
                )
            ).scalar_one()
        cursor = {"worker_proposal_count": int(total)}
        wake = await signal_project_change(
            session_factory,
            project_id=project_id,
            project_change_cursor=cursor,
            now=now,
        )
        if wake is not None:
            queued += 1
    return queued


async def signal_project_change(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    project_id: uuid.UUID,
    project_change_cursor: dict[str, Any],
    now: datetime | None = None,
) -> uuid.UUID | None:
    """Wake the background Resident on a MEANINGFUL project change.

    The production entrypoint for the ``project_change`` coalescing lane (the
    deadline/follow_up lanes are driven by ``sweep_due_authoritative_resident_wakeups``).
    A trigger — a completed StrategicCycle, a direction/pipeline transition, any
    non-conversational project mutation — calls this with a bounded cursor of the
    changed high-waters. Coalescing guarantees:

    * an unchanged project (cursor does not advance the resident's handled cursor)
      makes ZERO Tasks and ZERO model calls — this returns ``None`` cheaply;
    * a paused background policy is respected inside the coalescer;
    * rapid changes coalesce into at most one active turn plus one successor.

    Returns the enqueued/coalesced turn task id, or ``None`` when nothing was due.
    Owns its own transaction so a caller's session/lock is never held across it.
    """

    async with session_factory() as session, session.begin():
        wake = await coalesce_meaningful_background_wakeup(
            session,
            project_id=project_id,
            trigger_kind="project_change",
            now=now,
            project_change_cursor=project_change_cursor,
        )
    if wake is None:
        return None
    return wake.task_id


__all__ = [
    "coalesce_meaningful_background_wakeup",
    "signal_project_change",
    "sweep_due_authoritative_resident_wakeups",
    "sweep_pending_worker_proposal_wakeups",
]
