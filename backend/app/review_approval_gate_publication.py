"""Exact-authority publication for a completed review behind an approval hold."""

from __future__ import annotations

import uuid
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.models import Task, TaskStatus

logger = logging.getLogger(__name__)


async def handle_review_approval_gate(
    task: Task,
    session: AsyncSession,
    *,
    original_task_id: object,
    pr_number: int,
    target: dict[str, str | int],
    branch: str,
    base_branch: str,
    approved_review_loop: dict[str, Any],
    comment_call: Any,
    gh_token: str,
    owner: str,
    repo: str,
) -> bool:
    """Return true when a hold was handled or lost to fresher authority."""

    try:
        source_id = uuid.UUID(str(original_task_id))
    except (TypeError, ValueError):
        return True
    with session.no_autoflush:
        approval_required = (
            await session.execute(
                select(Task.requires_approval).where(Task.id == source_id)
            )
        ).scalar_one_or_none()
    if approval_required is not True:
        return False

    project_id = task.project_id
    review_task_id = task.id
    child_context_patch = {
        key: value
        for key, value in (task.context or {}).items()
        if key
        in {
            "blueprint_review_verdict",
            "review_was_parallelized",
            "review_result",
        }
    }
    receipt = await publish_review_approval_gate(
        session,
        project_id=project_id,
        pr_number=pr_number,
        review_task_id=review_task_id,
        original_task_id=source_id,
        target=target,
        branch=branch,
        base_branch=base_branch,
        approved_review_loop=approved_review_loop,
        child_context_patch=child_context_patch,
    )
    if receipt is None:
        return True

    from app.broadcast import broadcast_to_project
    from app.task_lifecycle_github_helpers import _comment_with_retry

    await broadcast_to_project(
        receipt["project_id"],
        "task_updated",
        {"task_id": receipt["task_id"], "status": receipt["status"]},
    )
    await _comment_with_retry(
        comment_call,
        gh_token,
        owner,
        repo,
        pr_number,
        "**Blueprint Review Pipeline**: Review approved, but merge requires "
        f"human approval — {receipt['reason']}",
    )
    logger.info(
        "[PIPELINE] PR #%s review approved but merge blocked — "
        "requires_approval set on %s",
        pr_number,
        str(original_task_id)[:8],
    )
    return True


async def publish_review_approval_gate(
    session: AsyncSession,
    *,
    project_id: object,
    pr_number: int,
    review_task_id: object,
    original_task_id: object,
    target: dict[str, str | int],
    branch: str,
    base_branch: str,
    approved_review_loop: dict[str, Any],
    child_context_patch: dict[str, Any],
) -> dict[str, str] | None:
    """Publish exact review proof without reviving a terminal/replaced source."""

    from app.task_lifecycle import (
        _lock_exact_review_project_pr,
        _publish_exact_review_target,
    )

    await session.rollback()
    project = await _lock_exact_review_project_pr(
        session,
        project_id=project_id,
        pr_number=pr_number,
    )
    if project is None:
        await session.rollback()
        return None

    try:
        child_id = uuid.UUID(str(review_task_id))
        source_id = uuid.UUID(str(original_task_id))
    except (TypeError, ValueError):
        await session.rollback()
        return None

    child = (
        (
            await session.execute(
                select(Task)
                .where(Task.id == child_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .first()
    )
    child_loop = (
        dict((child.context or {}).get("review_loop") or {})
        if child is not None
        else {}
    )
    if (
        child is None
        or child.project_id is None
        or str(child.project_id) != str(project_id)
        or child.status != TaskStatus.COMPLETED
        or child.archived is True
        or child.first_payload.get("type") != "review"
        or child_loop.get("review_target") != target
        or str(
            child_loop.get("original_task_id")
            or child.first_payload.get("original_task_id")
            or ""
        )
        != str(source_id)
    ):
        await session.rollback()
        return None

    source = (
        (
            await session.execute(
                select(Task)
                .where(Task.id == source_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .first()
    )
    if (
        source is None
        or source.requires_approval is not True
        or not _publish_exact_review_target(
            source,
            target=target,
            branch=branch,
            base_branch=base_branch,
            review_history=list(approved_review_loop.get("review_history") or []),
            issue_ledger=approved_review_loop.get("issue_ledger"),
            review_approved=True,
        )
    ):
        await session.rollback()
        return None

    reason = source.latest_status_message or "requires manual approval"
    source.latest_status_message = (
        f"Review approved — awaiting human approval to merge ({reason})"
    )
    child_context = dict(child.context or {})
    for key, value in child_context_patch.items():
        child_context[key] = value
    child_context["review_loop"] = {**child_loop, **approved_review_loop}
    child.context = child_context
    flag_modified(child, "context")
    session.add(source)
    session.add(child)
    await session.commit()
    return {
        "project_id": str(source.project_id),
        "task_id": str(source.id),
        "status": str(source.status.value),
        "reason": reason,
    }
