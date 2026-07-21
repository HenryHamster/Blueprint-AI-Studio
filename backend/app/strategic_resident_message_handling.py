"""Atomic ownership updates for messages represented by a Resident turn."""

from __future__ import annotations

import uuid

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models import MessageHandlingState, MessageSurface, ProjectMessage
from app.strategic_resident_message_tail import ReconciliationCursorError
from app.time_utils import _utcnow


async def respond_to_represented_user_messages(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    turn_task_id: uuid.UUID,
    represented_start: int,
    represented_end: int,
) -> tuple[uuid.UUID, ...]:
    """Own and respond to every unresolved user message in a frozen range.

    Already-terminal messages retain their original handler. An unresolved row
    owned by another Task is an authority conflict, not a row this turn may
    silently steal. Returned IDs preserve sequence order so the caller can
    attach its reply to the newest handled input and project each update.
    """

    if represented_start < 1 or represented_end < represented_start:
        raise ReconciliationCursorError("represented user-message range is malformed")
    rows = (
        (
            await session.execute(
                select(ProjectMessage)
                .where(
                    ProjectMessage.project_id == project_id,
                    ProjectMessage.surface == MessageSurface.CONVERSATION,
                    ProjectMessage.from_type == "user",
                    ProjectMessage.handling_state.in_(
                        (
                            MessageHandlingState.PENDING,
                            MessageHandlingState.CLAIMED,
                        )
                    ),
                    ProjectMessage.sequence >= represented_start,
                    ProjectMessage.sequence <= represented_end,
                )
                .order_by(ProjectMessage.sequence.asc(), ProjectMessage.id.asc())
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    responded_ids: list[uuid.UUID] = []
    now = _utcnow()
    unresolved = {
        MessageHandlingState.PENDING.value,
        MessageHandlingState.CLAIMED.value,
    }
    for message in rows:
        state = str(getattr(message.handling_state, "value", message.handling_state))
        if state not in unresolved:
            continue
        if (
            message.handled_by_task_id is not None
            and message.handled_by_task_id != turn_task_id
        ):
            raise ReconciliationCursorError(
                "represented user message is owned by another Task"
            )
        message.handled_by_task_id = turn_task_id
        message.updated_at = now
        session.add(message)
        responded_ids.append(message.id)
    if responded_ids:
        # Persist the ownership pointer first, then set handling_state in a
        # SEPARATE UPDATE that touches no column watched by the projectmessage
        # 0567-compat trigger (which fires BEFORE UPDATE OF handled_by_task_id and
        # unconditionally re-derives handling_state — 'pending' for user
        # conversation rows). Writing handling_state in the same UPDATE as
        # handled_by_task_id would let that trigger clobber the response back to
        # 'pending' on PostgreSQL (SQLite has no such trigger). synchronize_session
        # keeps the loaded ORM rows consistent for the caller's projections.
        await session.flush()
        await session.execute(
            update(ProjectMessage)
            .where(ProjectMessage.id.in_(responded_ids))
            .values(handling_state=MessageHandlingState.RESPONDED)
            .execution_options(synchronize_session="fetch")
        )
    return tuple(responded_ids)


__all__ = ["respond_to_represented_user_messages"]
