"""Dispatch local-only Resident turns independently of cloud VM capacity."""

import uuid
from collections.abc import Awaitable, Callable

from sqlalchemy import or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models import Project, Task, TaskStatus
from app.orchestrator_runner_dispatch import (
    _hold_strategic_local_runner_task,
    _load_active_runners_cache,
    _try_runner_dispatch,
)
from app.strategic_resident_runner_policy import resident_turn_requires_local_runner


async def dispatch_local_required_resident_turns_without_cloud_slots(
    session: AsyncSession,
    *,
    lock_dispatchable_task: Callable[
        [AsyncSession, uuid.UUID], Awaitable[Task | None]
    ],
) -> tuple[int, int]:
    """Pin or truthfully hold authoritative local-only Resident turns."""

    payload = Task.payloads[0]
    policy = payload["resident_runner_policy"]
    candidate_ids = (
        await session.execute(
            select(Task.id)
            .join(Project, Task.project_id == Project.id)
            .where(
                Task.status == TaskStatus.QUEUED,
                Task.requires_approval.is_(False),
                Project.paused.is_(False),
                Project.not_archived(),
                payload["type"].as_string() == "strategic_resident_turn",
                payload["engine_owner"].as_string() == "strategic_resident",
                payload["shadow_only"].as_boolean().is_(False),
                Task.context["_resident_mutation_authorized"].as_boolean().is_(True),
                Task.context["_resident_input_frozen"].as_boolean().is_(True),
                or_(
                    policy["require_local_runner"].as_boolean().is_(True),
                    policy["mode"].as_string().in_(["local", "local_required"]),
                ),
            )
            .order_by(Task.priority.desc().nullslast(), Task.created_at.asc())
            .limit(64)
        )
    ).scalars().all()
    if not candidate_ids:
        return (0, 0)

    runners_by_project = await _load_active_runners_cache(session)
    pins_this_cycle: dict[uuid.UUID, int] = {}
    pinned = 0
    held = 0
    for task_id in candidate_ids:
        task = await lock_dispatchable_task(session, task_id)
        if task is None:
            continue
        if not resident_turn_requires_local_runner(task):
            await session.rollback()
            continue
        if await _try_runner_dispatch(
            session, task, runners_by_project, pins_this_cycle
        ):
            pinned += 1
            continue
        await _hold_strategic_local_runner_task(session, task)
        held += 1
    return (pinned, held)


__all__ = ["dispatch_local_required_resident_turns_without_cloud_slots"]
