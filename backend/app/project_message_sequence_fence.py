"""Non-allocating transaction fence for a project's message sequence."""

from __future__ import annotations

import uuid

from sqlalchemy import func, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.database import resolve_dialect_name
from app.models import Project, ProjectMessage, ProjectMessageSequence


async def lock_project_message_sequence_boundary(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> int:
    """Lock the canonical sequence row without consuming a sequence value."""

    locked_project_id = await session.scalar(
        select(Project.id)
        .where(Project.id == project_id)
        .with_for_update(read=True, key_share=True)
    )
    if locked_project_id is None:
        raise RuntimeError("project not found while fencing message sequence")
    if resolve_dialect_name(session) == "postgresql":
        value = await session.scalar(
            text(
                """
                INSERT INTO projectmessagesequence AS counter
                  (project_id, last_sequence)
                SELECT :project_id, coalesce(max(message.sequence), 0)
                FROM projectmessage AS message
                WHERE message.project_id = :project_id
                ON CONFLICT (project_id) DO UPDATE
                SET last_sequence = greatest(
                  counter.last_sequence,
                  EXCLUDED.last_sequence
                )
                RETURNING last_sequence
                """
            ),
            {"project_id": project_id},
        )
        if value is None:
            raise RuntimeError("message sequence boundary lock failed")
        return int(value)

    counter = await session.get(
        ProjectMessageSequence,
        project_id,
        with_for_update=True,
    )
    if counter is None:
        max_sequence = await session.scalar(
            select(func.max(ProjectMessage.sequence)).where(
                ProjectMessage.project_id == project_id
            )
        )
        counter = ProjectMessageSequence(
            project_id=project_id,
            last_sequence=int(max_sequence or 0),
        )
        session.add(counter)
        await session.flush()
    return int(counter.last_sequence)


__all__ = ["lock_project_message_sequence_boundary"]
