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


@dataclass(frozen=True)
class ResidentMessagePrelock:
    resident: StrategicResident | None
    tasks: dict[uuid.UUID, Task] | None
    engine_owner: str | None
    error: Exception | None


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
    "prelock_resident_message_turn_state",
]
