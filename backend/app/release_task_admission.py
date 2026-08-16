"""Project-locked automatic task admission for release qualification."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.task_locking import (
    AUTOMATIC_TASK_CREATION_ALLOWED,
    AUTOMATIC_TASK_CREATION_PROJECT_ARCHIVED,
    AUTOMATIC_TASK_CREATION_PROJECT_PAUSED,
    AUTOMATIC_TASK_CREATION_PROJECT_UNAVAILABLE,
    project_lock,
)

logger = logging.getLogger(__name__)


async def _acquire_release_task_creation_authority(
    session: AsyncSession, project_id: uuid.UUID
) -> str:
    """Lock and retain a fully loaded Project for qualifier continuations.

    Release spawners read additional Project policy immediately after admission.
    Keeping only the canonical narrow projection in the identity map would make
    those synchronous attribute reads attempt async lazy I/O.  The full-row lock
    keeps those reads safe without weakening the same pause/archive authority or
    the Project -> child lock order.
    """

    async with project_lock(session, project_id, skip_locked=True) as project:
        if project is None:
            return AUTOMATIC_TASK_CREATION_PROJECT_UNAVAILABLE
        if project.archived:
            return AUTOMATIC_TASK_CREATION_PROJECT_ARCHIVED
        if project.paused:
            return AUTOMATIC_TASK_CREATION_PROJECT_PAUSED
        return AUTOMATIC_TASK_CREATION_ALLOWED


async def require_release_task_admission(
    session: AsyncSession, project_id: uuid.UUID
) -> None:
    """Raise before a qualifier child spawn when Project admission is closed."""

    decision = await _acquire_release_task_creation_authority(session, project_id)
    if decision != AUTOMATIC_TASK_CREATION_ALLOWED:
        raise RuntimeError(
            "automatic qualifier task admission is closed "
            f"for project {project_id} ({decision})"
        )


async def start_block(
    session: AsyncSession,
    project_id: uuid.UUID,
    version_number: int,
) -> dict[str, Any] | None:
    """Return the public no-spawn result, or None when qualification may start."""

    decision = await _acquire_release_task_creation_authority(session, project_id)
    if decision == AUTOMATIC_TASK_CREATION_ALLOWED:
        return None
    logger.info(
        "[RELEASE_QUALIFIER] v%d automatic task admission closed (%s)",
        version_number,
        decision,
    )
    return {
        "status": "project_paused"
        if decision == "project_paused"
        else "task_admission_deferred",
        "admission_reason": decision,
    }


async def finalize_admitted(
    session: AsyncSession, project_id: uuid.UUID
) -> bool:
    """Fence Project before Version while allowing terminal reconciliation.

    Finalization reads the complete Project policy after taking this fence.  A
    projected ``load_only`` lock would leave that same identity-mapped Project
    underloaded; a later synchronous policy attribute access can then attempt
    async lazy I/O and raise ``MissingGreenlet``.  Load the full row here while
    preserving the canonical Project -> ReleaseVersion lock order.
    """

    async with project_lock(session, project_id, skip_locked=True) as project:
        return project is not None
