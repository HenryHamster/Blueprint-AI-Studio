"""Post-commit realtime projection for reconciled Resident turns."""

from __future__ import annotations

import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

from app.broadcast import broadcast_to_project
from app.models import ProjectMessage, Task
from app.project_message_projection import (
    message_event_payload,
    message_updated_event_payload,
)
from app.strategic_resident_reconciliation import StrategicResidentReconcileOutcome


logger = logging.getLogger(__name__)


async def _best_effort_broadcast(
    project_id: uuid.UUID,
    event_type: str,
    payload: dict,
) -> None:
    try:
        await broadcast_to_project(str(project_id), event_type, payload)
    except Exception:  # noqa: BLE001 - reconciliation is already committed
        logger.warning(
            "Resident reconciliation broadcast failed (project=%s, event=%s)",
            project_id,
            event_type,
            exc_info=True,
        )


async def broadcast_resident_reconciliation(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    project_id: uuid.UUID,
    turn_task_id: uuid.UUID,
    outcome: StrategicResidentReconcileOutcome,
) -> None:
    """Refetch committed rows and emit each canonical event exactly once."""

    if outcome.idempotent_replay:
        return
    try:
        async with session_factory() as session:
            updated_messages = []
            if outcome.responded_message_ids:
                updated_messages = (
                    (
                        await session.execute(
                            select(ProjectMessage)
                            .where(
                                ProjectMessage.project_id == project_id,
                                ProjectMessage.id.in_(
                                    outcome.responded_message_ids
                                ),
                            )
                            .order_by(
                                ProjectMessage.sequence.asc(),
                                ProjectMessage.id.asc(),
                            )
                        )
                    ).scalars().all()
                )
            reply = (
                await session.get(ProjectMessage, outcome.reply_message_id)
                if outcome.reply_message_id is not None
                else None
            )
            task = await session.get(Task, turn_task_id)
            updated_payloads = [
                message_updated_event_payload(message)
                for message in updated_messages
            ]
            reply_payload = (
                message_event_payload(reply)
                if reply is not None and reply.project_id == project_id
                else None
            )
            task_payload = (
                {"task_id": str(task.id), "status": task.status}
                if task is not None and task.project_id == project_id
                else None
            )
    except Exception:  # noqa: BLE001 - reconciliation is already committed
        logger.warning(
            "Resident realtime projection failed after commit (project=%s)",
            project_id,
            exc_info=True,
        )
        return
    for payload in updated_payloads:
        await _best_effort_broadcast(project_id, "message_updated", payload)
    if reply_payload is not None:
        await _best_effort_broadcast(project_id, "message_created", reply_payload)
    if task_payload is not None:
        await _best_effort_broadcast(project_id, "task_updated", task_payload)


__all__ = ["broadcast_resident_reconciliation"]
