"""Server-owned public-egress fences for typed-Fast Resident sessions."""

from __future__ import annotations

import uuid

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models import (
    MessageHandlingState,
    MessageSurface,
    ProjectMessage,
    Task,
)
from app.time_utils import _utcnow


def enforce_typed_fast_public_claim(
    *,
    task: Task,
    resident_context: dict,
    is_routine: bool,
    audience: object,
    operator_audiences: set[str],
    conversation_claim_id: uuid.UUID | None,
    covered_message_ids: list[uuid.UUID] | None,
) -> None:
    if (
        resident_context.get("_resident_session") is True
        and task.first_payload.get("typed_fast_enabled") is True
        and not is_routine
        and audience in operator_audiences
        and (conversation_claim_id is None or covered_message_ids is None)
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                "Typed-Fast Resident operator-visible messages require an exact "
                "legacy conversation claim and covered message receipt"
            ),
        )


async def has_exact_live_fast_public_claim(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    task_id: uuid.UUID,
    resident_session_id: str,
    worker_generation: int,
    worker_incarnation_id: uuid.UUID | None,
) -> bool:
    """Return whether this exact Resident process has a live Fast-owned turn."""
    if worker_incarnation_id is None:
        return False
    rows = (
        (
            await session.execute(
                select(ProjectMessage).where(
                    ProjectMessage.project_id == project_id,
                    ProjectMessage.from_type == "user",
                    ProjectMessage.surface == MessageSurface.CONVERSATION,
                    ProjectMessage.status == "open",
                    ProjectMessage.handling_state == MessageHandlingState.CLAIMED,
                    ProjectMessage.handled_by_task_id == task_id,
                    ProjectMessage.conversation_claim_generation == worker_generation,
                    ProjectMessage.conversation_claim_incarnation
                    == worker_incarnation_id,
                    ProjectMessage.conversation_claim_expires_at > _utcnow(),
                )
            )
        )
        .scalars()
        .all()
    )
    return any(
        row.resident_session_id in {None, resident_session_id}
        and isinstance(row.context, dict)
        and row.context.get("_conversation_public_reply_owner") == "fast"
        for row in rows
    )


__all__ = ["enforce_typed_fast_public_claim", "has_exact_live_fast_public_claim"]
