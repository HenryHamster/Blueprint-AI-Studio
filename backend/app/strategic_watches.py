"""Durable, one-shot task watches for the Strategic Resident.

The Resident heartbeat already observes a project-wide work-state revision.
These helpers add the missing identity/agenda layer: which exact task Strategic
was waiting on, why it mattered, and whether its terminal receipt was handled.
They deliberately do not park or advance either task's lifecycle.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select

from app.models import (
    KnowledgeDocument,
    StrategicWatch,
    Task,
    TaskStatus,
    _utcnow,
)

WATCH_PENDING = "pending"
WATCH_FIRED = "fired"
WATCH_ACKNOWLEDGED = "acknowledged"
WATCH_CANCELLED = "cancelled"
WATCH_EXPIRED = "expired"

WATCH_ACTIVE_STATES = (WATCH_PENDING, WATCH_FIRED, WATCH_EXPIRED)
WATCH_ACK_DISPOSITIONS = frozenset(
    {"reported", "acted", "no_action", "superseded"}
)
WATCH_RECEIPT_LIMIT = 20


def _status_value(value: TaskStatus | str | None) -> str | None:
    if value is None:
        return None
    return value.value if isinstance(value, TaskStatus) else str(value)


def _is_terminal(value: TaskStatus | str | None) -> bool:
    return _status_value(value) in {
        TaskStatus.COMPLETED.value,
        TaskStatus.FAILED.value,
        TaskStatus.CANCELLED.value,
    }


async def _build_task_receipt(
    session,
    *,
    watch: StrategicWatch,
    target: Task,
) -> dict[str, Any]:
    doc_ids = (
        (
            await session.execute(
                select(KnowledgeDocument.id)
                .where(
                    KnowledgeDocument.project_id == watch.project_id,
                    KnowledgeDocument.source_task_id == target.id,
                )
                .order_by(KnowledgeDocument.created_at.desc())
                .limit(5)
            )
        )
        .scalars()
        .all()
    )
    payload = target.first_payload or {}
    return {
        "watch_id": str(watch.id),
        "target_kind": "task",
        "target_task_id": str(target.id),
        "title": str(target.title or "Untitled Task")[:500],
        "task_type": str(payload.get("type") or "")[:64],
        "terminal_status": _status_value(target.status),
        "latest_status_message": str(target.latest_status_message or "")[:2000],
        "knowledge_document_ids": [str(doc_id) for doc_id in doc_ids],
        "purpose": str(watch.purpose or "")[:1000],
        "fired_at": (watch.fired_at or _utcnow()).isoformat(),
    }


def serialize_watch(watch: StrategicWatch) -> dict[str, Any]:
    """Return the bounded heartbeat/API representation of a watch."""

    return {
        "id": str(watch.id),
        "project_id": str(watch.project_id),
        "created_by_task_id": (
            str(watch.created_by_task_id) if watch.created_by_task_id else None
        ),
        "strategic_session_id": watch.strategic_session_id,
        "target_kind": "task",
        "target_task_id": str(watch.target_task_id),
        "purpose": str(watch.purpose or "")[:1000],
        "state": watch.state,
        "terminal_status": watch.terminal_status,
        "receipt": dict(watch.receipt or {}),
        "deadline_at": watch.deadline_at.isoformat() if watch.deadline_at else None,
        "fired_at": watch.fired_at.isoformat() if watch.fired_at else None,
        "acknowledged_at": (
            watch.acknowledged_at.isoformat() if watch.acknowledged_at else None
        ),
        "acknowledgement_disposition": watch.acknowledgement_disposition,
    }


async def register_task_watch(
    session,
    *,
    project_id: uuid.UUID,
    created_by_task_id: uuid.UUID,
    target_task_id: uuid.UUID,
    purpose: str = "",
    strategic_session_id: str | None = None,
    deadline_at: datetime | None = None,
) -> tuple[StrategicWatch, bool]:
    """Register one idempotent terminal watch for a same-project task."""

    target = await session.get(Task, target_task_id)
    if target is None or target.project_id != project_id:
        raise ValueError("target task not found in project")

    existing = (
        await session.execute(
            select(StrategicWatch).where(
                StrategicWatch.created_by_task_id == created_by_task_id,
                StrategicWatch.target_task_id == target_task_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing, False

    now = _utcnow()
    watch = StrategicWatch(
        project_id=project_id,
        created_by_task_id=created_by_task_id,
        strategic_session_id=(strategic_session_id or "")[:128] or None,
        target_task_id=target_task_id,
        purpose=(purpose or "")[:1000],
        deadline_at=deadline_at,
    )
    if _is_terminal(target.status):
        watch.state = WATCH_FIRED
        watch.terminal_status = _status_value(target.status)
        watch.fired_at = now
        watch.receipt = await _build_task_receipt(
            session,
            watch=watch,
            target=target,
        )
    session.add(watch)
    await session.flush()
    return watch, True


async def resolve_project_task_watches(
    session,
    *,
    project_id: uuid.UUID,
    now: datetime | None = None,
    limit: int = WATCH_RECEIPT_LIMIT,
) -> list[StrategicWatch]:
    """Resolve terminal/expired watches and return unacknowledged receipts.

    The caller commits. Repeated calls are safe: only ``pending`` rows can
    transition, while fired/expired rows remain visible until acknowledgement.
    """

    now = now or _utcnow()
    rows = (
        (
            await session.execute(
                select(StrategicWatch, Task)
                .join(Task, Task.id == StrategicWatch.target_task_id)
                .where(
                    StrategicWatch.project_id == project_id,
                    StrategicWatch.state.in_(WATCH_ACTIVE_STATES),
                )
                .order_by(StrategicWatch.created_at.asc())
                .limit(max(1, min(int(limit), WATCH_RECEIPT_LIMIT)))
            )
        )
        .all()
    )
    visible: list[StrategicWatch] = []
    for watch, target in rows:
        if watch.state == WATCH_PENDING:
            if watch.deadline_at is not None and watch.deadline_at <= now:
                watch.state = WATCH_EXPIRED
                watch.fired_at = now
                watch.receipt = {
                    "watch_id": str(watch.id),
                    "target_kind": "task",
                    "target_task_id": str(target.id),
                    "title": str(target.title or "Untitled Task")[:500],
                    "task_status": _status_value(target.status),
                    "purpose": str(watch.purpose or "")[:1000],
                    "reason": "watch_deadline_elapsed",
                    "fired_at": now.isoformat(),
                }
                session.add(watch)
            elif _is_terminal(target.status):
                watch.state = WATCH_FIRED
                watch.terminal_status = _status_value(target.status)
                watch.fired_at = now
                watch.receipt = await _build_task_receipt(
                    session,
                    watch=watch,
                    target=target,
                )
                session.add(watch)
        if watch.state in {WATCH_FIRED, WATCH_EXPIRED}:
            visible.append(watch)
    return visible


async def acknowledge_watch(
    session,
    *,
    project_id: uuid.UUID,
    watch_id: uuid.UUID,
    disposition: str,
    note: str = "",
) -> StrategicWatch:
    """Acknowledge a fired/expired receipt idempotently."""

    if disposition not in WATCH_ACK_DISPOSITIONS:
        raise ValueError(
            f"disposition must be one of {sorted(WATCH_ACK_DISPOSITIONS)}"
        )
    watch = await session.get(StrategicWatch, watch_id)
    if watch is None or watch.project_id != project_id:
        raise ValueError("watch not found in project")
    if watch.state == WATCH_ACKNOWLEDGED:
        return watch
    if watch.state not in {WATCH_FIRED, WATCH_EXPIRED}:
        raise ValueError("watch has not fired")
    watch.state = WATCH_ACKNOWLEDGED
    watch.acknowledged_at = _utcnow()
    watch.acknowledgement_disposition = disposition
    watch.acknowledgement_note = (note or "")[:1000] or None
    session.add(watch)
    return watch


async def cancel_watch(
    session,
    *,
    project_id: uuid.UUID,
    watch_id: uuid.UUID,
) -> StrategicWatch:
    watch = await session.get(StrategicWatch, watch_id)
    if watch is None or watch.project_id != project_id:
        raise ValueError("watch not found in project")
    if watch.state in {WATCH_ACKNOWLEDGED, WATCH_CANCELLED}:
        return watch
    watch.state = WATCH_CANCELLED
    session.add(watch)
    return watch
