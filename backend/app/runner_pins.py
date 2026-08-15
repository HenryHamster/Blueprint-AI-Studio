"""Shared helpers for releasing work pinned to a runner that cannot pull it."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Project, Task, TaskStatus
from app.task_locking import project_lock, task_lock


def _is_exact_active_resident_pin(
    task: Task,
    project_vision: dict | None,
    runner_id: uuid.UUID,
) -> bool:
    """Return whether ``task`` is the exact live Resident pin for this runner.

    A spawned Resident task is committed before the start transaction can stamp
    its id into the Project session blob.  During that bounded hand-off the blob
    is an active ``worker_state=starting`` claim with ``task_id=None`` while the
    born Task already carries the same session/generation/runner identity.  That
    exact provisional pair is protected alongside the finalized canonical pair;
    a generic Resident-shaped marker is not.
    """
    context = task.context if isinstance(task.context, dict) else {}
    session_blob = (project_vision or {}).get("_resident_strategic_session")
    if not isinstance(session_blob, dict):
        return False
    try:
        blob_generation = int(session_blob.get("worker_generation") or 1)
        task_generation = int(context.get("_resident_worker_generation") or 1)
    except (TypeError, ValueError):
        return False
    runner_text = str(runner_id)
    blob_task_id = str(session_blob.get("task_id") or "").strip()
    exact_finalized_task = blob_task_id == str(task.id)
    exact_provisional_claim = (
        not blob_task_id and session_blob.get("worker_state") == "starting"
    )
    return (
        context.get("_resident_session") is True
        and session_blob.get("status") == "active"
        and not bool(session_blob.get("stop_requested"))
        and (exact_finalized_task or exact_provisional_claim)
        and str(session_blob.get("session_id") or "")
        == str(context.get("strategic_session_id") or "")
        and blob_generation == task_generation
        and str(session_blob.get("runner_id") or "") == runner_text
        and str(context.get("runner_target_id") or "") == runner_text
    )


async def release_unpulled_runner_pins(
    session: AsyncSession,
    runner_id: uuid.UUID,
    *,
    preserve_active_resident: bool = False,
) -> int:
    """Return releasable unstarted runner pins to the dispatch queue.

    Automatic runner transitions may opt in to preserving the exact canonical
    active Resident pin.  Stale task markers are still released, and explicit
    admin pause/delete callers retain their existing generic-release behavior.
    """
    total = 0
    batch_size = 500
    considered_task_ids: set[uuid.UUID] = set()
    while True:
        # This is only a bounded candidate hint.  Never load Task/vision ORM
        # authority here: a Resident finalizer can commit between this scan and
        # our decision.  Every mutation below re-reads Project then Task under
        # the canonical lock order with ``populate_existing=True``.
        query = (
            select(Task.id, Task.project_id)
            .where(
                Task.status == TaskStatus.PROVISIONING,
                Task.started_at.is_(None),
                Task.vm_instance_id == f"runner:{runner_id}",
            )
        )
        if considered_task_ids:
            query = query.where(Task.id.notin_(considered_task_ids))
        pinned = (
            await session.execute(
                query.order_by(Task.project_id.asc(), Task.id.asc()).limit(batch_size)
            )
        ).all()
        if not pinned:
            break
        released = 0
        for task_id, project_id in pinned:
            considered_task_ids.add(task_id)
            # Canonical cross-table order is Project -> Task.  Automatic
            # preserve-mode sweeps/heartbeats may safely defer a contended pin
            # until their next recurring invocation. Explicit admin pause/delete
            # callers are one-shot and retain their complete-release semantics,
            # so they block (in the same canonical order) instead of silently
            # returning with a pin left behind.
            skip_contended = preserve_active_resident
            async with project_lock(
                session,
                project_id,
                skip_locked=skip_contended,
                load_columns=[Project.id, Project.vision],
            ) as locked_project:
                if locked_project is None:
                    continue
                async with task_lock(
                    session, task_id, skip_locked=skip_contended
                ) as locked_task:
                    if locked_task is None:
                        continue
                    # Revalidate every candidate predicate from the locked,
                    # freshly-populated row.  A runner pull/requeue may have won
                    # after the hint scan and must never be overwritten here.
                    if (
                        locked_task.project_id != project_id
                        or locked_task.status != TaskStatus.PROVISIONING
                        or locked_task.started_at is not None
                        or locked_task.vm_instance_id != f"runner:{runner_id}"
                    ):
                        continue
                    if preserve_active_resident and _is_exact_active_resident_pin(
                        locked_task, locked_project.vision, runner_id
                    ):
                        continue
                    locked_task.status = TaskStatus.QUEUED
                    locked_task.vm_instance_id = None
                    locked_task.vm_provisioned_at = None
                    new_ctx = dict(locked_task.context or {})
                    new_ctx.pop("runner_target_id", None)
                    locked_task.context = new_ctx
                    session.add(locked_task)
                    released += 1
        total += released
        await session.flush()
    return total
