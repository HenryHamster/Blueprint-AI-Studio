"""Retry binding helpers for the durable Workday resident worker."""

import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

from app.models import Project, Task, TaskStatus
from app.time_utils import _utcnow


logger = logging.getLogger(__name__)


async def restore_resident_retry_binding(
    task: Task,
    session: AsyncSession,
    prior_context: dict,
) -> bool:
    """Restore the live Workday worker's hard runner pin after an attempt reset."""
    if prior_context.get("_resident_session") is not True:
        return False
    project = await session.scalar(
        select(Project)
        .where(Project.id == task.project_id)
        .execution_options(populate_existing=True)
    )
    if project is None:
        return False
    from app.strategic_resident_session import (
        read_resident_session,
        resident_session_is_active,
    )

    blob = read_resident_session(project.vision)
    try:
        task_generation = int(prior_context.get("_resident_worker_generation") or 1)
        current_generation = int((blob or {}).get("worker_generation") or 1)
    except (TypeError, ValueError):
        return False
    runner_id = str((blob or {}).get("runner_id") or "").strip()
    if (
        not resident_session_is_active(blob)
        or bool((blob or {}).get("stop_requested"))
        or str((blob or {}).get("task_id")) != str(task.id)
        or str((blob or {}).get("session_id"))
        != str(prior_context.get("strategic_session_id"))
        or current_generation != task_generation
        or not runner_id
    ):
        return False

    next_context = dict(task.context or {})
    for key, value in prior_context.items():
        if key.startswith("_resident_") or key == "strategic_session_id":
            next_context[key] = value
    next_context["runner_target_id"] = runner_id
    next_context["preferred_runner_target_id"] = runner_id
    task.context = next_context
    task.status = TaskStatus.PROVISIONING
    task.vm_instance_id = f"runner:{runner_id}"
    task.vm_provisioned_at = _utcnow()
    return True


async def sync_resident_retry_wait_after_commit(
    task: Task,
    session: AsyncSession,
    prior_context: dict,
) -> bool:
    """Best-effort projection sync after the durable retry transition commits."""
    if prior_context.get("_resident_session") is not True:
        return False
    session_id = str(prior_context.get("strategic_session_id") or "")
    try:
        worker_generation = int(
            prior_context.get("_resident_worker_generation") or 1
        )
    except (TypeError, ValueError):
        return False
    if not session_id or session.bind is None:
        return False
    project_id = task.project_id
    task_id = task.id
    from app.strategic_resident_session import sync_resident_worker_retry_wait

    factory = async_sessionmaker(bind=session.bind, expire_on_commit=False)
    try:
        async with factory() as mirror_session:
            return await sync_resident_worker_retry_wait(
                mirror_session,
                project_id=project_id,
                task_id=task_id,
                session_id=session_id,
                worker_generation=worker_generation,
            )
    except Exception:
        logger.warning(
            "[RESIDENT_SESSION] retry-wait projection sync failed for task %s",
            task_id,
            exc_info=True,
        )
        return False
