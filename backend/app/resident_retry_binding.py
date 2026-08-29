"""Retry binding helpers for the durable Workday resident worker."""

import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

from app.models import Project, Task, TaskStatus
from app.time_utils import _coerce_naive_utc, _utcnow


logger = logging.getLogger(__name__)


async def restore_resident_retry_binding(
    task: Task,
    session: AsyncSession,
    prior_context: dict,
    *,
    excluded_runner_ids: set[str] | None = None,
) -> bool:
    """Restore the live Workday worker's hard runner pin after an attempt reset.

    ``excluded_runner_ids`` is the task-local decline fence.  A post-claim
    runtime incompatibility must not be undone by restoring the resident
    session's otherwise-authoritative runner pin.
    """
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
        _parse_iso_naive,
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
    excluded = {str(value) for value in (excluded_runner_ids or set())}
    if (
        not resident_session_is_active(blob)
        or bool((blob or {}).get("stop_requested"))
        or str((blob or {}).get("task_id")) != str(task.id)
        or str((blob or {}).get("session_id"))
        != str(prior_context.get("strategic_session_id"))
        or current_generation != task_generation
        or not runner_id
        or runner_id in excluded
    ):
        return False

    next_context = dict(task.context or {})
    for key, value in prior_context.items():
        if key.startswith("_resident_") or key == "strategic_session_id":
            next_context[key] = value
    next_context["runner_target_id"] = runner_id
    next_context["preferred_runner_target_id"] = runner_id
    task.context = next_context
    # A retry keeps the same durable session/generation but starts a new Agent
    # process with a fresh random incarnation id.  Do not dispatch that process
    # while the predecessor's Resident-wide incarnation lease is still live:
    # the initial admission heartbeat would correctly lose the CAS, exit 0, and
    # leave this attempt stranded IN_PROGRESS until the 25-minute orphan sweep.
    #
    # Preserve a longer provider/account hold when one already exists.  The
    # Task-column deadline is part of the pinned-runner claim guard, so this
    # closes the dispatch race without prematurely revoking an incarnation that
    # may still be finishing its terminal callback.
    incarnation_expires_at = _parse_iso_naive(
        (blob or {}).get("worker_incarnation_expires_at")
    )
    now = _utcnow()
    if incarnation_expires_at is not None and incarnation_expires_at > now:
        existing_retry_after = task.retry_after_until
        if existing_retry_after is not None:
            existing_retry_after = _coerce_naive_utc(existing_retry_after)
        if (
            existing_retry_after is None
            or existing_retry_after < incarnation_expires_at
        ):
            task.retry_after_until = incarnation_expires_at
    task.status = TaskStatus.PROVISIONING
    task.vm_instance_id = f"runner:{runner_id}"
    task.vm_provisioned_at = now
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
