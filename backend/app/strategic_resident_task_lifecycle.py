"""Resident task-lifecycle mutations routed through the operator's own path.

A Resident cancel/requeue/retry/unblock is exactly the operator ``PATCH
/tasks/{id}`` status change — so instead of re-implementing the cascade
(child cancellation, fleet-slot release, VM teardown, direction outcomes), these
adapters run the SAME ``modify_task_apply`` the operator UI uses, inside the
two-phase external executor's ``perform`` phase (authority-fenced + receipted).
The Resident's only added authority is: the target must be in its own project and
must not be a Resident turn task.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.strategic_operation_authority_validation import RESIDENT_TURN_TASK_TYPE
from app.strategic_operation_external_effect import ExternalEffectNotPerformed


# capability_id -> (receipt operation_kind, PATCH-status the operator path applies).
# QUEUED covers requeue / retry / unblock; CANCELLED covers cancel. Both are the
# only operator-allowed status transitions (see admin_task_mutations
# `_apply_status_mutation` `admin_allowed = {QUEUED, CANCELLED}`).
_TASK_LIFECYCLE_OPS = {
    "task.cancel": ("cancel_task", "CANCELLED"),
    "task.requeue": ("requeue_task", "QUEUED"),
    "task.retry": ("retry_task", "QUEUED"),
    "task.unblock": ("unblock_task", "QUEUED"),
}


def task_lifecycle_operation_kind(capability_id: str) -> str | None:
    entry = _TASK_LIFECYCLE_OPS.get(capability_id)
    return entry[0] if entry is not None else None


def _lifecycle_effect(
    capability_id: str,
    project_id: uuid.UUID,
    parameters: dict[str, Any],
    *,
    expected_target_updated_at: str | None,
) -> Callable[[AsyncSession], Awaitable[dict[str, Any]]]:
    _kind, new_status = _TASK_LIFECYCLE_OPS[capability_id]
    target_task_id = uuid.UUID(str(parameters["task_id"]))

    async def perform(session: AsyncSession) -> dict[str, Any]:
        # Deferred imports keep this module importable without the admin stack.
        from fastapi import BackgroundTasks

        from app.admin_task_mutations import modify_task_apply
        from app.models import Task

        task = (
            await session.execute(
                select(Task)
                .where(Task.id == target_task_id, Task.project_id == project_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if task is None:
            raise ValueError("target task not found in project")
        if (
            str((task.first_payload or {}).get("type") or "") == RESIDENT_TURN_TASK_TYPE
        ):
            raise ValueError("cannot lifecycle-mutate a Resident turn task")
        observed_updated_at = (
            task.updated_at.isoformat() if task.updated_at is not None else None
        )
        if observed_updated_at != expected_target_updated_at:
            raise ExternalEffectNotPerformed(
                "target task changed after the lifecycle operation was authorized"
            )

        background_tasks = BackgroundTasks()
        result = await modify_task_apply(
            task, {"status": new_status}, background_tasks, session
        )
        # Run the integrated post-mutation work (terminal cleanup / VM teardown).
        await background_tasks()
        return {"task_id": str(task.id), "status": str(result.get("status"))}

    return perform


def build_task_lifecycle_effect(
    capability_id: str,
    *,
    project_id: uuid.UUID,
    parameters: dict[str, Any],
    expected_target_updated_at: str | None = None,
) -> Callable[[AsyncSession], Awaitable[dict[str, Any]]] | None:
    """Return the perform coroutine for a task-lifecycle capability, or None."""

    if capability_id in _TASK_LIFECYCLE_OPS:
        return _lifecycle_effect(
            capability_id,
            project_id,
            parameters,
            expected_target_updated_at=expected_target_updated_at,
        )
    return None


__all__ = [
    "build_task_lifecycle_effect",
    "task_lifecycle_operation_kind",
]
