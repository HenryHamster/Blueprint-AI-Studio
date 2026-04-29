"""Task lifecycle service facade."""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Mapping

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.models import Task, TaskStatus
from app.task_lifecycle_service.commands import (
    CreateTaskCommand,
    SpawnTaskCommand,
    TransitionTaskCommand,
)
from app.task_lifecycle_service.context_patch import ContextPatch, apply_context_patch
from app.task_lifecycle_service.events import append_task_history
from app.task_lifecycle_service.transitions import can_transition, coerce_task_status
from app.task_locking import task_lock


class InvalidTaskTransition(ValueError):
    """Raised when a requested generic status transition is invalid."""


class TaskLifecycleService:
    """Centralized write API for task lifecycle state.

    Methods mutate the provided SQLAlchemy session but do not commit. Callers
    keep control of transaction boundaries so existing workflows can batch
    domain side effects with the lifecycle write.
    """

    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_task(self, command: CreateTaskCommand) -> Task:
        """Create a task and append a creation history row.

        `task_fields` is passed through to the Task model so future columns can
        be adopted without redesigning this command shape.
        """

        task = Task(
            **{
                "project_id": command.project_id,
                "title": command.title,
                "description": command.description,
                "payloads": [dict(payload) for payload in command.payloads],
                "context": dict(command.context),
                "status": _coerce_required_status(command.status),
                "created_by": command.created_by,
                "launched_by_user_id": command.launched_by_user_id,
                "requires_approval": command.requires_approval,
                "priority": command.priority,
                "depends_on": list(command.depends_on),
                "idempotency_key": command.idempotency_key,
                **dict(command.task_fields),
            }
        )
        self.session.add(task)
        if command.record_history:
            append_task_history(
                self.session,
                task,
                action="CREATED",
                actor=command.actor,
                reason=command.reason,
                details=command.details,
            )
        return task

    async def spawn_child(self, command: SpawnTaskCommand) -> Task:
        """Spawn a child/follow-up task and append a spawn history row."""

        task_fields = {
            "parent_task_id": command.parent_task_id,
            **dict(command.task_fields),
        }
        task = Task(
            **{
                "project_id": command.project_id,
                "title": command.title,
                "description": command.description,
                "payloads": [dict(payload) for payload in command.payloads],
                "context": dict(command.context),
                "status": _coerce_required_status(command.status),
                "created_by": command.created_by,
                "requires_approval": command.requires_approval,
                "priority": command.priority,
                "depends_on": list(command.depends_on),
                "idempotency_key": command.idempotency_key,
                **task_fields,
            }
        )
        self.session.add(task)
        if command.record_history:
            details = {
                "parent_task_id": str(command.parent_task_id),
                **dict(command.details or {}),
            }
            append_task_history(
                self.session,
                task,
                action="SPAWNED",
                actor=command.actor,
                reason=command.reason,
                details=details,
            )
        return task

    async def patch_context(
        self,
        task_id: Any,
        patch: ContextPatch,
        *,
        actor: str,
        reason: str,
        details: Mapping[str, Any] | None = None,
        allowed_statuses: Iterable[TaskStatus] | None = None,
        skip_locked: bool = True,
        timeout: float = 30.0,
        record_history: bool = True,
    ) -> Task | None:
        """Patch `Task.context` under a fresh row lock.

        Unknown keys are preserved. The patch operation itself defines how to
        update a key; it is not a whitelist of allowed lifecycle fields.
        """

        async with task_lock(
            self.session,
            task_id,
            skip_locked=skip_locked,
            timeout=timeout,
        ) as task:
            if task is None:
                return None
            if not _status_allowed(task.status, allowed_statuses):
                return None
            if patch.is_empty:
                return task

            task.context = apply_context_patch(task.context or {}, patch)
            flag_modified(task, "context")
            self.session.add(task)

            if record_history:
                history_details = dict(details or {})
                history_details.setdefault("keys", sorted(_patch_keys(patch)))
                append_task_history(
                    self.session,
                    task,
                    action="CONTEXT_PATCH",
                    actor=actor,
                    reason=reason,
                    details=history_details,
                )
            return task

    async def transition(
        self,
        task_id: Any,
        command: TransitionTaskCommand,
        *,
        allowed_statuses: Iterable[TaskStatus] | None = None,
        skip_locked: bool = True,
        timeout: float = 30.0,
    ) -> Task | None:
        """Transition task status under a fresh row lock."""

        async with task_lock(
            self.session,
            task_id,
            skip_locked=skip_locked,
            timeout=timeout,
        ) as task:
            if task is None:
                return None
            if not _status_allowed(task.status, allowed_statuses):
                return None

            previous_status = coerce_task_status(task.status)
            target_status = coerce_task_status(command.status)
            if previous_status is None or target_status is None:
                raise InvalidTaskTransition(
                    f"cannot transition task {task.id} from {task.status} to {command.status}"
                )
            if not can_transition(
                previous_status,
                target_status,
                allow_terminal_requeue=command.allow_terminal_requeue,
            ):
                raise InvalidTaskTransition(
                    f"cannot transition task {task.id} from {previous_status} to {command.status}"
                )

            if command.context_patch and not command.context_patch.is_empty:
                task.context = apply_context_patch(task.context or {}, command.context_patch)
                flag_modified(task, "context")

            if command.message is not None:
                task.latest_status_message = command.message

            task.status = target_status
            self.session.add(task)

            if command.record_history:
                history_details = {
                    "from_status": previous_status.value,
                    "to_status": target_status.value,
                    **dict(command.details or {}),
                }
                append_task_history(
                    self.session,
                    task,
                    action="STATUS_CHANGE",
                    actor=command.actor,
                    reason=command.reason,
                    details=history_details,
                )
            return task


def _patch_keys(patch: ContextPatch) -> set[str]:
    return (
        set(patch.set)
        | set(patch.delete)
        | set(patch.increment)
        | set(patch.append_unique)
    )


def _coerce_required_status(status: TaskStatus | str) -> TaskStatus:
    coerced = coerce_task_status(status)
    if coerced is None:
        raise ValueError(f"unknown task status: {status!r}")
    return coerced


def _status_allowed(
    status: TaskStatus | str,
    allowed_statuses: Iterable[TaskStatus] | None,
) -> bool:
    if allowed_statuses is None:
        return True
    current = coerce_task_status(status)
    allowed = {coerce_task_status(s) for s in allowed_statuses}
    return current in allowed
