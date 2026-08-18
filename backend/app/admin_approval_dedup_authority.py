"""Locked exact-target authority for operator-approval review DEDUP."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models import Project, Task, TaskStatus

if TYPE_CHECKING:
    from app.admin_approval_authority import ApprovalDecision


_ACTIVE_REVIEW_STATUSES = frozenset(
    {
        TaskStatus.QUEUED,
        TaskStatus.PROVISIONING,
        TaskStatus.IN_PROGRESS,
        TaskStatus.EVALUATING,
        TaskStatus.BLOCKED,
        TaskStatus.PARKED,
    }
)


async def consume_approval_followup_after_dedup(
    decision: ApprovalDecision,
    review_task_id: uuid.UUID,
    session: AsyncSession,
) -> bool:
    """Consume only while the exact child stays active through source commit.

    Project -> PR -> child -> source matches review completion's Task-row order.
    Locking the child closes the status-check/approval-consumption race: a
    terminal transition either wins before this transaction and is rejected,
    or waits until the already-active review owns follow-up authority.
    """

    from app.admin_approval_authority import (
        _claim_matches_authoritative_task,
        approval_child_spawn_publication,
    )
    from app.task_locking import acquire_pr_spawn_lock

    project_id = decision.project_id
    await session.rollback()
    project = (
        (
            await session.execute(
                select(Project)
                .where(Project.id == project_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .first()
    )
    if project is None or not decision.pr_number:
        await session.rollback()
        return False
    repo_identity = (
        f"{project.github_owner}/{project.github_repo}"
        if project.github_owner and project.github_repo
        else None
    )
    await acquire_pr_spawn_lock(
        session,
        decision.project_id,
        int(decision.pr_number),
        repo_identity=repo_identity,
    )
    review_task = (
        (
            await session.execute(
                select(Task)
                .where(Task.id == review_task_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .first()
    )
    target = (
        ((review_task.context or {}).get("review_loop") or {}).get("review_target")
        if review_task is not None
        else None
    )
    expected_target = {
        "project_id": str(decision.project_id),
        "pr_number": int(decision.pr_number),
        "base_sha": str((decision.claim or {}).get("base_sha") or ""),
        "head_sha": str((decision.claim or {}).get("head_sha") or ""),
    }
    if (
        review_task is None
        or review_task.project_id != decision.project_id
        or review_task.archived is True
        or review_task.status not in _ACTIVE_REVIEW_STATUSES
        or review_task.first_payload.get("type") != "review"
        or target != expected_target
    ):
        await session.rollback()
        return False
    source = (
        (
            await session.execute(
                select(Task)
                .where(Task.id == decision.task_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .first()
    )
    if source is None or not await _claim_matches_authoritative_task(
        source,
        decision,
        session,
        allowed_states=frozenset({"pending"}),
    ):
        await session.rollback()
        return False
    await approval_child_spawn_publication(decision)(source, session)
    await session.commit()
    return True
