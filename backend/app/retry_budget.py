"""Retry-budget helpers for parent task re-evaluation.

Extracted from ``task_lifecycle.py`` to keep that module under its size
guardrail. Two pieces live here:

- :func:`compute_effective_max_requeues` shrinks the parent retry budget when a
  child has already failed twice in a row with the same non-infra failure
  class — that pattern usually means the retry is addressing the symptom, not
  the root cause. INFRA classes are exempt because transient failures can
  legitimately repeat.
- :func:`classify_override_reason` maps a failure-class + clone-context pair
  to a provenance label for scope overrides applied during retry. Used by
  ``direction_outcomes`` to distinguish tasks that were originally spawned at
  a given scope from those that reached it via retry escalation/downgrade.
"""
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.failure_classification import (
    AGENT_TIMEOUT,
    CODE_ERROR,
    INFRA_CLASSES,
    SCOPE_OVERFLOW,
    classify_failure,
)
from app.models import Task, TaskStatus


async def compute_effective_max_requeues(
    parent: Task, session: AsyncSession, max_requeues: int
) -> int:
    """Return the max requeue count, reduced by 1 if any failed child repeats
    its prior non-infra failure class."""
    children = (await session.execute(
        select(Task).where(
            Task.parent_task_id == parent.id,
            Task.status == TaskStatus.FAILED,
        )
    )).scalars().all()
    for child in children:
        ctx = child.context or {}
        prev_class = ctx.get("_previous_failure_class")
        if not prev_class:
            continue
        current_class = classify_failure(child, logs=ctx.get("last_error") or None)
        if current_class == prev_class and current_class not in INFRA_CLASSES:
            return max_requeues - 1
    return max_requeues


def classify_override_reason(failure_class: str, clone_ctx: dict) -> str:
    """Return a provenance label for a scope override applied during retry."""
    if failure_class == AGENT_TIMEOUT:
        return "timeout_downgrade"
    if failure_class == SCOPE_OVERFLOW or (
        (clone_ctx.get("failure_category") or "").lower() == "scope_overflow"
    ):
        return "scope_overflow_escalation"
    if failure_class == CODE_ERROR:
        return "code_error_downgrade"
    return "retry_override"
