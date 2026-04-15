"""Auto-review: spawn review tasks when pull requests are opened.

When a GitHub pull_request 'opened' (or 'synchronize') event arrives for a
registered project with auto-review enabled, this module applies guard rails
(rate limit, duplicate detection, concurrent limit) and spawns a review-type
task.
"""
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.database import AsyncSessionLocal
from app.models import Task, TaskStatus
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)


async def _handle_auto_review(
    *,
    owner: str,
    repo: str,
    pr_number: int,
    branch: str,
    base_branch: str,
    pr_title: str,
    project_id: uuid.UUID,
    config: dict[str, Any],
) -> None:
    """Background handler: spawn a review task for a newly opened PR.

    Opens its own DB session (runs outside the request lifecycle).
    """
    try:
        async with AsyncSessionLocal() as session:
            await _handle_auto_review_inner(
                owner=owner,
                repo=repo,
                pr_number=pr_number,
                branch=branch,
                base_branch=base_branch,
                pr_title=pr_title,
                project_id=project_id,
                config=config,
                session=session,
            )
    except Exception as exc:
        logger.error(
            "[AUTO-REVIEW] Error handling PR #%d on %s/%s: %s",
            pr_number, owner, repo, exc, exc_info=True,
        )


async def _handle_auto_review_inner(
    *,
    owner: str,
    repo: str,
    pr_number: int,
    branch: str,
    base_branch: str,
    pr_title: str,
    project_id: uuid.UUID,
    config: dict[str, Any],
    session: AsyncSession,
) -> None:
    """Inner implementation, called with a live session."""
    max_per_hour = config.get("max_per_hour", 5)
    max_concurrent = config.get("max_concurrent", 3)
    now = _utcnow()

    # ── Guard: rate limit ──
    one_hour_ago = now - timedelta(hours=1)
    recent_tasks = (await session.execute(
        select(Task).where(
            Task.project_id == project_id,
            Task.created_at >= one_hour_ago,
        )
    )).scalars().all()
    recent_review_count = sum(
        1 for t in recent_tasks
        if (t.context or {}).get("auto_review") is True
    )
    if recent_review_count >= max_per_hour:
        logger.info(
            "[AUTO-REVIEW] Rate limit hit (%d/%d in last hour) for project %s",
            recent_review_count, max_per_hour, project_id,
        )
        return

    # ── Guard: duplicate detection (same PR number, active review task) ──
    active_statuses = [
        TaskStatus.QUEUED, TaskStatus.PROVISIONING,
        TaskStatus.IN_PROGRESS, TaskStatus.EVALUATING,
    ]
    active_tasks = (await session.execute(
        select(Task).where(
            Task.project_id == project_id,
            Task.status.in_(active_statuses),
        )
    )).scalars().all()
    for t in active_tasks:
        ctx = t.context or {}
        if (
            ctx.get("auto_review") is True
            and ctx.get("pr_number") == pr_number
        ):
            logger.info(
                "[AUTO-REVIEW] Duplicate: active task %s already covers PR #%d",
                t.id, pr_number,
            )
            return

    # ── Guard: concurrent limit ──
    active_review_count = sum(
        1 for t in active_tasks
        if (t.context or {}).get("auto_review") is True
    )
    if active_review_count >= max_concurrent:
        logger.info(
            "[AUTO-REVIEW] Concurrent limit hit (%d/%d) for project %s",
            active_review_count, max_concurrent, project_id,
        )
        return

    # ── Create the review task ──
    task = Task(
        project_id=project_id,
        title=f"Review PR #{pr_number}: {pr_title[:80]}",
        payloads=[{
            "type": "review",
            "pr_number": pr_number,
            "branch": branch,
            "base_branch": base_branch,
        }],
        context={
            "auto_review": True,
            "pr_number": pr_number,
            "pr_title": pr_title,
            "pr_url": f"https://github.com/{owner}/{repo}/pull/{pr_number}",
        },
        status=TaskStatus.QUEUED,
        created_by="SYSTEM_AUTO_REVIEW",
    )
    session.add(task)
    await session.commit()
    logger.info(
        "[AUTO-REVIEW] Spawned task %s for PR #%d on %s/%s",
        task.id, pr_number, owner, repo,
    )
