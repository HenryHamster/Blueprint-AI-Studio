"""Operator-facing explanations for why a task is moving or waiting."""

from __future__ import annotations

from typing import Any

from app.models import TaskStatus


def _status_value(value: Any) -> str:
    if hasattr(value, "value"):
        return str(value.value)
    return str(value or "")


def _attr(task: Any, name: str) -> Any:
    values = getattr(task, "__dict__", None)
    if isinstance(values, dict):
        return values.get(name)
    return getattr(task, name, None)


def _compact_text(value: Any, *, max_chars: int) -> str | None:
    text = " ".join(str(value or "").strip().split())
    if not text:
        return None
    if len(text) <= max_chars:
        return text
    return f"{text[: max_chars - 3]}..."


def _dependency_count(task: Any) -> int:
    depends_on = _attr(task, "depends_on")
    if isinstance(depends_on, (list, tuple, set)):
        return len(depends_on)
    return 0


def build_task_movement_explanation(
    task: Any,
    *,
    max_chars: int = 220,
) -> str | None:
    """Return one bounded sentence suitable for task cards and focus rows."""

    status = _status_value(_attr(task, "status"))
    latest_status = _compact_text(_attr(task, "latest_status_message"), max_chars=max_chars)
    blocked_reason = _compact_text(_attr(task, "blocked_reason"), max_chars=max_chars)
    park_reason = _compact_text(_attr(task, "park_reason"), max_chars=max_chars)

    if _attr(task, "requires_approval") is True and status not in {
        TaskStatus.COMPLETED.value,
        TaskStatus.FAILED.value,
        TaskStatus.CANCELLED.value,
    }:
        return "Waiting for operator approval."

    if status == TaskStatus.BLOCKED.value or blocked_reason:
        if blocked_reason:
            return blocked_reason
        blocker_kind = _compact_text(_attr(task, "blocker_kind"), max_chars=80)
        if blocker_kind:
            return f"Blocked on {blocker_kind.replace('_', ' ')}."
        return "Blocked on an unmet precondition."

    if status == TaskStatus.PARKED.value or park_reason:
        return park_reason or "Parked until its wake condition fires."

    if status == TaskStatus.FAILED.value:
        return latest_status or "Failed and needs review before follow-up work continues."

    if status == TaskStatus.QUEUED.value:
        dependency_count = _dependency_count(task)
        if dependency_count > 0:
            plural = "" if dependency_count == 1 else "s"
            return f"Waiting for {dependency_count} upstream task{plural} to finish."
        return latest_status or "Queued for the next available pipeline slot."

    if status == TaskStatus.PROVISIONING.value:
        return latest_status or "Provisioning runtime before work starts."

    if status == TaskStatus.IN_PROGRESS.value:
        return latest_status or "Running now."

    if status == TaskStatus.EVALUATING.value:
        return latest_status or "Evaluating the result before follow-up work continues."

    return latest_status
