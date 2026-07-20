"""Message-claim fencing for Strategic Resident ownership cutover."""

from __future__ import annotations

import uuid
from collections.abc import Collection

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models import (
    MessageHandlingState,
    MessageSurface,
    ProjectMessage,
    Task,
    TaskStatus,
)
from app.strategic_resident_transition_locks import ResidentOwnershipStateError
from app.time_utils import _utcnow


async def reconcile_cutover_message_claims(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    cancelled_shadow_task_ids: Collection[uuid.UUID],
) -> None:
    """Release cut-over shadow claims and repair proven legacy responses."""

    rows = (
        (
            await session.execute(
                select(ProjectMessage)
                .where(
                    ProjectMessage.project_id == project_id,
                    ProjectMessage.surface == MessageSurface.CONVERSATION,
                    ProjectMessage.from_type == "user",
                    ProjectMessage.handling_state.in_(
                        (MessageHandlingState.PENDING, MessageHandlingState.CLAIMED)
                    ),
                    ProjectMessage.handled_by_task_id.is_not(None),
                )
                .order_by(ProjectMessage.sequence.asc(), ProjectMessage.id.asc())
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    owner_ids = {row.handled_by_task_id for row in rows}
    task_query = select(Task).where(
        Task.project_id == project_id,
        Task.id.in_(owner_ids),
    )
    tasks = (
        (await session.execute(task_query)).scalars().all() if owner_ids else []
    )
    tasks_by_id = {task.id: task for task in tasks}
    cancelled = set(cancelled_shadow_task_ids)
    legacy_responses = {
        row.id
        for row in rows
        if (task := tasks_by_id.get(row.handled_by_task_id)) is not None
        and task.project_id == project_id
        and task.status == TaskStatus.COMPLETED
        and task.first_payload.get("type") == "strategic_review"
        and row.status == "resolved"
        and isinstance(row.resolution, str)
        and bool(row.resolution.strip())
        and row.resolved_at is not None
    }
    foreign = [
        row
        for row in rows
        if row.handled_by_task_id not in cancelled and row.id not in legacy_responses
    ]
    if foreign:
        raise ResidentOwnershipStateError(
            "cutover found unresolved messages owned by non-shadow Tasks: "
            + ", ".join(str(row.id) for row in foreign)
        )
    now = _utcnow()
    for row in rows:
        if row.id in legacy_responses:
            row.handling_state = MessageHandlingState.RESPONDED
        else:
            row.handling_state = MessageHandlingState.PENDING
            row.handled_by_task_id = None
        row.updated_at = now
        session.add(row)


__all__ = ["reconcile_cutover_message_claims"]
