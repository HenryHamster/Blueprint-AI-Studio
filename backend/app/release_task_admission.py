"""Project-locked automatic task admission for release qualification."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Project
from app.task_locking import (
    AUTOMATIC_TASK_CREATION_ALLOWED,
    AUTOMATIC_TASK_CREATION_PROJECT_PAUSED,
    automatic_task_creation_decision,
    project_lock,
)

_MODULE_SIZE_GOVERNANCE_CAP: int = 122

logger = logging.getLogger(__name__)


class ReleaseTaskAdmissionClosed(RuntimeError):
    """Typed control-flow signal for a denied qualifier child spawn.

    Callers that own a durable parent state can distinguish an expected
    pause/archive/contention denial from an actual finalizer crash without
    parsing an exception string.  It remains a ``RuntimeError`` subclass for
    compatibility with direct spawner callers that historically treated a
    closed admission fence as a runtime failure.
    """

    def __init__(self, project_id: uuid.UUID, reason: str) -> None:
        self.project_id = project_id
        self.reason = reason
        super().__init__(
            "automatic qualifier task admission is closed "
            f"for project {project_id} ({reason})"
        )


async def _acquire_release_task_creation_authority(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    skip_locked: bool = True,
) -> str:
    """Lock and retain a fully loaded Project for qualifier continuations.

    Release spawners read additional Project policy immediately after admission.
    Keeping only the canonical narrow projection in the identity map would make
    those synchronous attribute reads attempt async lazy I/O.  The full-row lock
    keeps those reads safe without weakening the same pause/archive authority or
    the Project -> child lock order.
    """

    async with project_lock(session, project_id, skip_locked=skip_locked) as project:
        return automatic_task_creation_decision(project)


async def require_release_task_admission(
    session: AsyncSession, project_id: uuid.UUID
) -> None:
    """Raise before a qualifier child spawn when Project admission is closed."""

    decision = await _acquire_release_task_creation_authority(session, project_id)
    if decision != AUTOMATIC_TASK_CREATION_ALLOWED:
        raise ReleaseTaskAdmissionClosed(project_id, decision)


async def peek_release_task_admission(
    session: AsyncSession, project_id: uuid.UUID
) -> str:
    """Read admission without a lock to skip external finalize gates when closed."""

    project = await session.get(Project, project_id, populate_existing=True)
    return automatic_task_creation_decision(project)


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
        if decision == AUTOMATIC_TASK_CREATION_PROJECT_PAUSED
        else "task_admission_deferred",
        "admission_reason": decision,
    }


async def finalize_admission_decision(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    skip_locked: bool = True,
) -> str:
    """Fence Project before Version and return the qualifier admission decision.

    Finalization may need to create any of several continuation children after
    it inspects terminal evidence.  It therefore uses the exact same
    pause/archive/unavailable authority as the spawners themselves.  The full
    row remains loaded so later synchronous policy reads cannot attempt async
    lazy I/O, and the transaction-scoped Project lock preserves the canonical
    Project -> ReleaseVersion order through the decision write.
    """

    return await _acquire_release_task_creation_authority(
        session, project_id, skip_locked=skip_locked
    )
