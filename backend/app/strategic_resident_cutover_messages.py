"""Message-claim fencing for Strategic Resident ownership cutover."""

from __future__ import annotations

import uuid
from collections.abc import Collection

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models import MessageHandlingState, MessageSurface, ProjectMessage
from app.strategic_resident_transition_locks import ResidentOwnershipStateError
from app.time_utils import _utcnow


async def release_cancelled_shadow_message_claims(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    cancelled_shadow_task_ids: Collection[uuid.UUID],
) -> None:
    """Release only unresolved claims owned by shadow Tasks cut over here."""

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
    cancelled = set(cancelled_shadow_task_ids)
    foreign = [row for row in rows if row.handled_by_task_id not in cancelled]
    if foreign:
        raise ResidentOwnershipStateError(
            "cutover found unresolved messages owned by non-shadow Tasks: "
            + ", ".join(str(row.id) for row in foreign)
        )
    now = _utcnow()
    for row in rows:
        row.handling_state = MessageHandlingState.PENDING
        row.handled_by_task_id = None
        row.updated_at = now
        session.add(row)


__all__ = ["release_cancelled_shadow_message_claims"]
