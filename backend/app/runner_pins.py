"""Shared helpers for releasing work pinned to a runner that cannot pull it."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Task, TaskStatus


async def release_unpulled_runner_pins(
    session: AsyncSession,
    runner_id: uuid.UUID,
) -> int:
    """Return unstarted runner-pinned tasks to the dispatch queue."""
    total = 0
    batch_size = 500
    while True:
        pinned = (
            await session.execute(
                select(Task).where(
                    Task.status == TaskStatus.PROVISIONING,
                    Task.started_at.is_(None),
                    Task.vm_instance_id == f"runner:{runner_id}",
                ).limit(batch_size)
            )
        ).scalars().all()
        if not pinned:
            break
        for task in pinned:
            task.status = TaskStatus.QUEUED
            task.vm_instance_id = None
            task.vm_provisioned_at = None
            new_ctx = dict(task.context or {})
            new_ctx.pop("runner_target_id", None)
            task.context = new_ctx
            session.add(task)
        total += len(pinned)
        await session.flush()
    return total
