"""Post-commit integration for authoritative Resident terminal mutations."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import TypeVar

from sqlalchemy.ext.asyncio import AsyncSession

from app.broadcast import broadcast_to_project
from app.models import TaskStatus
from app.strategic_resident_slot_release import release_resident_turn_slot


logger = logging.getLogger(__name__)
_Outcome = TypeVar("_Outcome")


def release_slot_after_reconciliation(
    function: Callable[..., Awaitable[_Outcome]],
) -> Callable[..., Awaitable[_Outcome]]:
    """Release after the reconciliation context has committed its transaction."""

    @wraps(function)
    async def wrapped(session_factory, request, *args, **kwargs):
        outcome = await function(session_factory, request, *args, **kwargs)
        try:
            await release_resident_turn_slot(
                session_factory,
                task_id=request.turn_task_id,
                project_id=request.project_id,
            )
        except Exception:  # noqa: BLE001 - terminal truth is already committed.
            logger.warning(
                "Resident terminal slot release deferred for task %s",
                request.turn_task_id,
                exc_info=True,
            )
        return outcome

    return wrapped


async def commit_release_and_broadcast_failure(
    session: AsyncSession,
    *,
    task_id: uuid.UUID,
    project_id: uuid.UUID,
    suppress_broadcast_error: bool,
) -> None:
    """Commit terminal truth, release its slot, then publish realtime state."""

    await session.commit()
    try:
        await release_resident_turn_slot(
            session,
            task_id=task_id,
            project_id=project_id,
        )
    except Exception:  # noqa: BLE001 - terminal truth is already committed.
        logger.warning(
            "Resident failure slot release deferred for task %s",
            task_id,
            exc_info=True,
        )
    try:
        await broadcast_to_project(
            str(project_id),
            "task_updated",
            {"task_id": str(task_id), "status": TaskStatus.FAILED},
        )
    except Exception:  # Optional realtime delivery is post-commit.
        if not suppress_broadcast_error:
            raise
        logger.warning(
            "Resident failure committed but realtime broadcast failed for task %s",
            task_id,
            exc_info=True,
        )


async def commit_and_release_failure_replay(
    session: AsyncSession,
    *,
    task_id: uuid.UUID,
    project_id: uuid.UUID,
) -> None:
    """Release a terminal replay without emitting a duplicate broadcast."""

    await session.commit()
    try:
        await release_resident_turn_slot(
            session,
            task_id=task_id,
            project_id=project_id,
        )
    except Exception:  # noqa: BLE001 - terminal truth is already committed.
        logger.warning(
            "Resident replay slot release deferred for task %s",
            task_id,
            exc_info=True,
        )


__all__ = [
    "commit_and_release_failure_replay",
    "commit_release_and_broadcast_failure",
    "release_slot_after_reconciliation",
]
