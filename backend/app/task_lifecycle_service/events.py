"""Task lifecycle event/history helpers."""

from __future__ import annotations

from typing import Any, Mapping

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Task, TaskHistory


def append_task_history(
    session: AsyncSession,
    task: Task,
    *,
    action: str,
    actor: str,
    reason: str,
    details: Mapping[str, Any] | None = None,
) -> TaskHistory:
    """Append a TaskHistory row with stable actor/reason metadata."""

    history_details = dict(details or {})
    history_details.setdefault("actor", actor)
    history_details.setdefault("reason", reason)

    history = TaskHistory(
        task_id=task.id,
        action=action,
        details=history_details,
    )
    session.add(history)
    return history
