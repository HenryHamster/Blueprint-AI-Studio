"""Canonical ownership contract for review/fix execution continuations."""

from __future__ import annotations

from sqlalchemy import and_, or_

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
