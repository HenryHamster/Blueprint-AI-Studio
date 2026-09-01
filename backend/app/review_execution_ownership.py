"""Canonical ownership contract for review/fix execution continuations."""

from __future__ import annotations

from sqlalchemy import and_, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models import Task, TaskStatus


POST_COMMIT_CONTINUATION_STATUSES: frozenset[TaskStatus] = frozenset(
    {
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.EVALUATING,
    }
)


def pr_execution_owner_clause():
    """SQL predicate for work that still owns review/fix execution."""
    from app.review_pipeline import _PIPELINE_ACTIVE_STATUSES

    return or_(
        Task.status.in_(_PIPELINE_ACTIVE_STATUSES),
        and_(
            Task.post_commit_pending.is_(True),
            Task.archived.is_(False),
            Task.status.in_(POST_COMMIT_CONTINUATION_STATUSES),
        ),
    )


def is_pr_execution_owner(task: Task) -> bool:
    """Return whether ``task`` still has durable execution ownership."""
    from app.review_pipeline import _PIPELINE_ACTIVE_STATUSES

    return bool(
        task.status in _PIPELINE_ACTIVE_STATUSES
        or (
            task.post_commit_pending
            and not task.archived
            and task.status in POST_COMMIT_CONTINUATION_STATUSES
        )
    )


def has_pending_pr_continuation(task: Task) -> bool:
    """Return whether a review/fix row has deferred post-commit work."""
    return bool(
        task.post_commit_pending
        and str(task.first_payload.get("type") or "").lower() in {"review", "fix"}
    )


def is_pending_pr_continuation_owner(task: Task) -> bool:
    """Return whether a review/fix continuation currently owns its PR."""
    return bool(
        has_pending_pr_continuation(task)
        and not task.archived
        and task.status in POST_COMMIT_CONTINUATION_STATUSES
    )


async def has_active_pr_repair_owner(task: Task, session: AsyncSession) -> bool:
    """Return whether the task's canonical PR chain still owns repair work.

    A failed originating task is not dependency-dead while an exact-chain
    review/fix task owns execution. This includes a terminal review/fix row with
    a durable post-commit continuation, which closes the request-changes -> fix
    handoff gap without inventing a second ownership marker.
    """
    from app.ci_merge_helpers import _pr_number_match, effective_pr_number

    try:
        pr_number = int(
            effective_pr_number(task) or task.first_payload.get("pr_number") or 0
        )
    except (TypeError, ValueError):
        return False
    if pr_number <= 0:
        return False
    owners = (
        (
            await session.execute(
                select(Task).where(
                    Task.project_id == task.project_id,
                    _pr_number_match(session, pr_number),
                    pr_execution_owner_clause(),
                )
            )
        )
        .scalars()
        .all()
    )
    original_task_id = str(task.id)
    for owner in owners:
        payload = owner.first_payload
        if payload.get("type") not in {"review", "fix"}:
            continue
        review_loop = (owner.context or {}).get("review_loop") or {}
        if str(
            payload.get("original_task_id")
            or review_loop.get("original_task_id")
            or ""
        ) != original_task_id:
            continue
        if is_pr_execution_owner(owner):
            return True
    return False


def pr_execution_owner_priority(task: Task) -> tuple[int, str]:
    """Order already-owned continuations ahead of newly claimable duplicates."""
    if is_pending_pr_continuation_owner(task):
        rank = 0
    elif task.status in {TaskStatus.IN_PROGRESS, TaskStatus.EVALUATING}:
        rank = 1
    elif task.status in {TaskStatus.BLOCKED, TaskStatus.PARKED}:
        rank = 2
    elif task.status == TaskStatus.PROVISIONING:
        rank = 3
    else:
        rank = 4
    return rank, str(task.id)
