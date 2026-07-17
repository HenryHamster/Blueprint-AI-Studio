"""Pre-lock Resident state before the conversation sequence critical section."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.database import resolve_dialect_name
from app.models import Project, StrategicResident, Task
from app.strategic_resident_turns import lock_shadow_resident_turn_state
from app.strategic_trigger import strategic_refresh_pending


@dataclass(frozen=True)
class ResidentMessagePrelock:
    resident: StrategicResident | None
    tasks: dict[uuid.UUID, Task] | None
    engine_owner: str | None
    error: Exception | None


async def lock_resident_message_owner_fence(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> tuple[Project, StrategicResident | None]:
    """Lock the ownership root once before choosing legacy or Resident wake."""

    project = (
        await session.execute(
            select(Project)
            .where(Project.id == project_id)
            .with_for_update(key_share=True)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    resident = (
        await session.execute(
            select(StrategicResident)
            .where(StrategicResident.project_id == project_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    return project, resident


async def lock_resident_message_owner_state(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> tuple[Project, bool, bool, bool, str | None]:
    """Lock the owner fence and return current owner and legacy session state."""

    project, resident = await lock_resident_message_owner_fence(session, project_id)
    strategic_owner = (
        resident is not None and resident.engine_owner == "strategic_resident"
    )
    refresh_pending = False if strategic_owner else strategic_refresh_pending(project.vision)
    from app.strategic_resident_session import (
        read_resident_session,
        resident_session_is_active,
    )

    resident_blob = read_resident_session(project.vision)
    resident_active = not strategic_owner and resident_session_is_active(resident_blob)
    session_id = resident_blob.get("session_id") if resident_active else None
    return project, strategic_owner, refresh_pending, resident_active, session_id


async def prelock_resident_message_turn_state(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    locked_project: Project,
    lock_timeout_ms: int = 1_500,
) -> ResidentMessagePrelock:
    """Bound all existing Resident row waits before reserving a sequence."""

    try:
        async with session.begin_nested():
            if resolve_dialect_name(session) == "postgresql":
                await session.execute(
                    text(
                        "SET LOCAL lock_timeout = "
                        f"'{max(1, int(lock_timeout_ms))}ms'"
                    )
                )
            resident, tasks = await lock_shadow_resident_turn_state(
                session,
                project_id=project_id,
                locked_project=locked_project,
            )
            if resolve_dialect_name(session) == "postgresql":
                await session.execute(text("SET LOCAL lock_timeout = DEFAULT"))
        return ResidentMessagePrelock(
            resident=resident,
            tasks=tasks,
            engine_owner=resident.engine_owner,
            error=None,
        )
    except Exception as exc:  # noqa: BLE001 - caller applies ownership fail policy
        owner = await session.scalar(
            select(StrategicResident.engine_owner).where(
                StrategicResident.project_id == project_id
            )
        )
        return ResidentMessagePrelock(
            resident=None,
            tasks=None,
            engine_owner=str(owner) if owner is not None else None,
            error=exc,
        )


__all__ = [
    "ResidentMessagePrelock",
    "lock_resident_message_owner_fence",
    "lock_resident_message_owner_state",
    "prelock_resident_message_turn_state",
]
