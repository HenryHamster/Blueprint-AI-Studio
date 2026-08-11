"""PR-owned admission boundary for administrative review/fix retries."""

from __future__ import annotations

import uuid

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.models import Task, TaskStatus
from app.rollup_lineage_fence import (
    invalidate_ancestor_rollups_for_requeue,
    lock_ancestor_rollups_for_requeue,
)
from app.task_locking import acquire_pr_spawn_lock


async def prepare_bulk_pr_retry_boundary(
    session: AsyncSession,
    task: Task,
) -> tuple[dict | None, dict | None, list[Task], uuid.UUID | None]:
    """Admit one review/fix retry in PR -> ancestor -> child lock order.

    Canonical review/fix admission locks the same-PR task rows.  A retry beneath
    a plan/research parent must first lock that ancestor chain, because parent
    cancellation owns ancestor -> descendants and the reverse hierarchy order
    deadlocks.  The returned parent id is a stale-edge receipt: the caller must
    definitively re-read the target child, compare its parent edge, and roll
    back on mismatch before applying the already-locked ancestor invalidation.
    """

    payload = task.first_payload or {}
    task_type = str(payload.get("type") or "").lower()
    if task_type not in {"review", "fix"}:
        return None, None, [], task.parent_task_id

    pr_number = payload.get("pr_number")
    if pr_number is None:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot retry {task_type} without PR",
        )

    # The repository PR lock stays outermost. Its second acquisition inside
    # prepare_direct_*_admission is transaction-reentrant.
    await acquire_pr_spawn_lock(session, task.project_id, int(pr_number))
    ancestors = await lock_ancestor_rollups_for_requeue(task, session)

    from app.github import get_pull_request
    from app.routers.agent import get_github_token_for_task

    gh_token, owner, repo = await get_github_token_for_task(task.id, session)

    async def _fetch_live_pr() -> dict:
        return await get_pull_request(
            gh_token,
            owner,
            repo,
            int(pr_number),
            project_id=str(task.project_id),
        )

    fix_target: dict | None = None
    review_target: dict | None = None
    if task_type == "fix":
        from app.fix_admission import prepare_direct_fix_admission

        admission = await prepare_direct_fix_admission(
            session,
            project_id=task.project_id,
            pr_number=int(pr_number),
            fix_task_id=task.id,
            fetch_live_pr=_fetch_live_pr,
        )
        if admission.allowed:
            fix_target = dict(admission.fix_target or {})
    else:
        from app.review_admission import prepare_direct_review_admission

        admission = await prepare_direct_review_admission(
            session,
            project_id=task.project_id,
            pr_number=int(pr_number),
            fetch_live_pr=_fetch_live_pr,
            review_task_id=task.id,
        )
        if admission.allowed:
            review_target = dict(admission.review_target or {})

    if not admission.allowed:
        await session.rollback()
        raise HTTPException(status_code=409, detail=admission.reason)
    return fix_target, review_target, ancestors, task.parent_task_id


async def apply_bulk_pr_retry_boundary(
    session: AsyncSession,
    task: Task,
    *,
    expected_parent_id: uuid.UUID | None,
    prelocked_ancestors: list[Task],
    fix_target: dict | None,
    review_target: dict | None,
) -> list[str]:
    """Validate the locked child and apply its prelocked retry state."""

    if task.parent_task_id != expected_parent_id:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail=(
                "Task ancestry changed during review/fix retry admission; "
                "retry the operation"
            ),
        )

    from app.review_execution_ownership import has_pending_pr_continuation

    if has_pending_pr_continuation(task):
        task_type = str((task.first_payload or {}).get("type") or "review/fix")
        raise HTTPException(
            status_code=409,
            detail=(
                f"Cannot retry {task_type} task while its post-commit "
                "continuation is pending; wait for recovery or cancel the "
                "task first"
            ),
        )

    if fix_target is not None or review_target is not None:
        review_loop = dict((task.context or {}).get("review_loop") or {})
        if fix_target is not None:
            review_loop["fix_target"] = fix_target
        if review_target is not None:
            review_loop["review_target"] = review_target
        task.context = {**(task.context or {}), "review_loop": review_loop}
        flag_modified(task, "context")

    if (
        task.status in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
        and task.parent_task_id is not None
    ):
        return await invalidate_ancestor_rollups_for_requeue(
            task,
            session,
            prelocked_ancestors=prelocked_ancestors,
        )
    return []


async def apply_bulk_non_pr_retry_boundary(
    session: AsyncSession,
    task: Task,
) -> list[str]:
    """Revalidate and invalidate a canonically Project->Task-locked retry."""

    from app.review_execution_ownership import has_pending_pr_continuation

    if has_pending_pr_continuation(task):
        task_type = str((task.first_payload or {}).get("type") or "review/fix")
        raise HTTPException(
            status_code=409,
            detail=(
                f"Cannot retry {task_type} task while its post-commit "
                "continuation is pending; wait for recovery or cancel the "
                "task first"
            ),
        )
    if (
        task.status in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
        and task.parent_task_id is not None
    ):
        return await invalidate_ancestor_rollups_for_requeue(task, session)
    return []
