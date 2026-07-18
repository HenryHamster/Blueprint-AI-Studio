"""Meaningful background wakeups for the authoritative Resident."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

from app.models import StrategicResident
from app.strategic_resident_failure_state import failure_backoff_seconds
from app.strategic_resident_runtime import (
    coalesce_authoritative_resident_turn,
    is_authoritative_resident_turn,
)
from app.strategic_resident_turns import (
    ResidentTurnWake,
    _ensure_resident_locked,
    _load_pointer_tasks_locked,
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
    locked_resident: StrategicResident | None = None,
) -> ResidentTurnWake | None:
    """Enqueue only a changed cursor or a genuinely due commitment."""

    resident = locked_resident or await _ensure_resident_locked(session, project_id)
    if resident.project_id != project_id:
        raise ValueError("locked Resident belongs to another project")
    if resident.engine_owner != "strategic_resident":
        return None
    prelocked_tasks = None
    if trigger_kind in {"deadline", "follow_up"}:
        due_at = resident.next_wakeup_at
        if due_at is None or due_at > (now or _utcnow()):
            return None
    elif trigger_kind == "project_change":
        incoming = _merge_project_change_cursor({}, project_change_cursor)
        if not incoming:
            return None
        scheduling = dict(resident.scheduling_state or {})
        raw_pending = scheduling.get("pending_project_change_cursor")
        pending = _merge_project_change_cursor(
            {}, raw_pending if isinstance(raw_pending, dict) else None
        )
        incoming = _merge_project_change_cursor(pending, incoming)
        prelocked_tasks = await _load_pointer_tasks_locked(session, resident)
        represented = _merge_project_change_cursor(
            {}, resident.handled_project_change_cursor
        )
        for task in prelocked_tasks.values():
            if not is_authoritative_resident_turn(
                task, project_id=project_id
            ):
                continue
            payload = task.first_payload if isinstance(task.first_payload, dict) else {}
            raw_cursor = payload.get("project_change_cursor")
            represented = _merge_project_change_cursor(
                represented,
                raw_cursor if isinstance(raw_cursor, dict) else None,
            )
        if _merge_project_change_cursor(represented, incoming) == represented:
            if pending:
                scheduling.pop("pending_project_change_cursor", None)
                resident.scheduling_state = scheduling
                session.add(resident)
            return None
        if resident.background_policy == "paused":
            scheduling.update(
                {
                    "pending_project_change_cursor": incoming,
                    "background_suppressed_at": (now or _utcnow()).isoformat(),
                    "background_suppressed_trigger": trigger_kind,
                }
            )
            resident.scheduling_state = scheduling
            session.add(resident)
            return ResidentTurnWake(
                task_id=None,
                state="paused",
                lane="background",
                active_task_id=resident.active_turn_task_id,
                successor_task_id=resident.background_successor_task_id,
            )
        scheduling["pending_project_change_cursor"] = incoming
        resident.scheduling_state = scheduling
        session.add(resident)
        project_change_cursor = incoming
    else:
        raise ValueError("background wake must be project_change, deadline, or follow_up")
    wake = await coalesce_authoritative_resident_turn(
        session,
        project_id=project_id,
        lane="background",
        trigger_kind=trigger_kind,
        project_change_cursor=project_change_cursor,
        locked_resident=resident,
        prelocked_tasks=prelocked_tasks,
    )
    if trigger_kind in {"deadline", "follow_up"} and wake.state != "paused":
        resident.next_wakeup_at = None
    elif trigger_kind == "project_change" and wake.task_id is not None:
        scheduling = dict(resident.scheduling_state or {})
        scheduling.pop("pending_project_change_cursor", None)
        resident.scheduling_state = scheduling
        session.add(resident)
    return wake


def _parse_scheduled_at(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _restored_recovery_retry(
    resident: StrategicResident,
    *,
    now: datetime,
) -> datetime | None:
    recovery = resident.recovery_state if isinstance(resident.recovery_state, dict) else {}
    if recovery.get("status") != "retrying" or recovery.get(
        "unknown_operation_receipt_ids"
    ):
        return None
    failure_count = recovery.get("failure_count")
    retry_limit = recovery.get("retry_limit")
    if (
        not isinstance(failure_count, int)
        or not isinstance(retry_limit, int)
        or failure_count >= retry_limit
    ):
        return None
    updated_at = _parse_scheduled_at(recovery.get("updated_at"))
    category = recovery.get("failure_category")
    if updated_at is None or not isinstance(category, str):
        return None
    retry_at = updated_at + timedelta(
        seconds=failure_backoff_seconds(category, failure_count)
    )
    return now if retry_at <= now else retry_at


async def sync_resident_background_policy(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    paused: bool,
    now: datetime | None = None,
) -> ResidentTurnWake | None:
    """Mirror the operator project pause into Resident scheduling atomically."""

    resident = (
        await session.execute(
            select(StrategicResident)
            .where(StrategicResident.project_id == project_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if resident is None:
        return None
    observed_at = now or _utcnow()
    resident.background_policy = "paused" if paused else "active"
    scheduling = dict(resident.scheduling_state or {})
    scheduling.update(
        {
            "background_policy_changed_at": observed_at.isoformat(),
            "background_policy_source": "project_pause",
        }
    )
    resident.scheduling_state = scheduling
    if paused:
        if resident.next_wakeup_at is not None:
            scheduling["paused_next_wakeup_at"] = resident.next_wakeup_at.isoformat()
            resident.scheduling_state = scheduling
        resident.next_wakeup_at = None
        session.add(resident)
        return None

    raw_pending = scheduling.get("pending_project_change_cursor")
    pending = _merge_project_change_cursor(
        {}, raw_pending if isinstance(raw_pending, dict) else None
    )
    pending_wake: ResidentTurnWake | None = None
    if resident.engine_owner == "strategic_resident" and pending:
        pending_wake = await coalesce_meaningful_background_wakeup(
            session,
            project_id=project_id,
            trigger_kind="project_change",
            now=observed_at,
            project_change_cursor=pending,
            locked_resident=resident,
        )
        scheduling = dict(resident.scheduling_state or {})
    paused_wakeup = _parse_scheduled_at(
        scheduling.pop("paused_next_wakeup_at", None)
    )
    restored_retry = (
        None
        if paused_wakeup is not None
        else _restored_recovery_retry(resident, now=observed_at)
    )
    candidates = (
        _parse_scheduled_at(scheduling.get("deadline_at")),
        paused_wakeup,
        restored_retry,
    )
    due = [value for value in candidates if value is not None]
    resident.next_wakeup_at = max(observed_at, min(due)) if due else None
    # A failure recorded while background work was paused sets health blocked
    # because it could not arm a retry then. Reconstructing a valid retry on
    # resume proves there are no unknown effects and the retry budget remains;
    # restore the truthful recovery state. `_restored_recovery_retry` rejects
    # genuine blockers (unknown effects, exhausted budget, non-retrying state).
    if restored_retry is not None:
        resident.health_status = "recovering"
    resident.scheduling_state = scheduling
    session.add(resident)
    return pending_wake


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
    from app.strategic_resident_worker_proposal_intake import (
        pending_worker_proposal_cursor,
    )

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
            pending_cursor = await pending_worker_proposal_cursor(
                count_session,
                project_id=project_id,
            )
        cursor = {
            "worker_proposal_count": int(total),
            **pending_cursor,
        }
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
    "sync_resident_background_policy",
]
