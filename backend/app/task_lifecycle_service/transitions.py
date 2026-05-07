"""Task status grouping and transition policy helpers."""

from __future__ import annotations

from app.models import TaskStatus

QUEUED_STATUSES: frozenset[TaskStatus] = frozenset({TaskStatus.QUEUED})
ACTIVE_STATUSES: frozenset[TaskStatus] = frozenset(
    {
        TaskStatus.PROVISIONING,
        TaskStatus.IN_PROGRESS,
        TaskStatus.EVALUATING,
    }
)
TERMINAL_STATUSES: frozenset[TaskStatus] = frozenset(
    {
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    }
)
NON_TERMINAL_STATUSES: frozenset[TaskStatus] = QUEUED_STATUSES | ACTIVE_STATUSES


def coerce_task_status(status: TaskStatus | str) -> TaskStatus | None:
    if isinstance(status, TaskStatus):
        return status
    try:
        return TaskStatus(status)
    except ValueError:
        return None


def is_terminal_status(status: TaskStatus | str) -> bool:
    coerced = coerce_task_status(status)
    return coerced in TERMINAL_STATUSES if coerced is not None else False


def is_active_status(status: TaskStatus | str) -> bool:
    coerced = coerce_task_status(status)
    return coerced in ACTIVE_STATUSES if coerced is not None else False


def is_retryable_status(status: TaskStatus | str) -> bool:
    coerced = coerce_task_status(status)
    return coerced == TaskStatus.FAILED


def is_archiveable_status(status: TaskStatus | str) -> bool:
    return is_terminal_status(status)


def can_transition(
    current: TaskStatus | str,
    target: TaskStatus | str,
    *,
    allow_terminal_requeue: bool = False,
) -> bool:
    """Return whether a lifecycle transition is valid for the generic service.

    Domain-specific approval/merge flows may layer stricter checks on top.
    The generic rule blocks terminal-state mutation except idempotent repeats
    and explicit administrative requeue to QUEUED.
    """

    current_status = coerce_task_status(current)
    target_status = coerce_task_status(target)
    if current_status is None or target_status is None:
        return False
    if current_status == target_status:
        return True
    if current_status in TERMINAL_STATUSES:
        return allow_terminal_requeue and target_status == TaskStatus.QUEUED
    return True
