"""Auto-review: spawn review tasks when pull requests are opened.

When a GitHub pull_request 'opened' (or 'synchronize') event arrives for a
registered project with auto-review enabled, this module applies guard rails
(rate limit, duplicate detection, concurrent limit) and spawns a review-type
task.
"""

import logging
import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.database import AsyncSessionLocal
from app.models import Project, Task, TaskStatus
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
    webhook_pr: dict[str, Any] | None = None,
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
                webhook_pr=webhook_pr,
                session=session,
            )
    except Exception as exc:
        logger.error(
            "[AUTO-REVIEW] Error handling PR #%d on %s/%s: %s",
            pr_number,
            owner,
            repo,
            exc,
            exc_info=True,
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
    webhook_pr: dict[str, Any] | None = None,
    session: AsyncSession,
) -> None:
    """Inner implementation, called with a live session."""
    max_per_hour = config.get("max_per_hour", 5)
    max_concurrent = config.get("max_concurrent", 3)
    now = _utcnow()

    # The webhook is signature-verified event context, but the background job
    # may run after another synchronize/close/merge. Resolve current GitHub
    # state inside canonical admission's repo/PR lock so stale events do not
    # provision needless reviewers.
    if not isinstance(webhook_pr, dict):
        logger.warning(
            "[AUTO-REVIEW] Refusing unbound review for PR #%d on %s/%s",
            pr_number,
            owner,
            repo,
        )
        return
    from app.review_admission import prepare_direct_review_admission
    from app.branch_cleanup import _get_project_github_token
    from app.github import get_pull_request

    project = await session.get(Project, project_id)
    if project is None:
        logger.warning("[AUTO-REVIEW] Project %s disappeared", project_id)
        return
    gh_token = await _get_project_github_token(project, session)

    # Direct review admission takes same-PR Task locks.  Acquire the universal
    # Project authority first so the complete order remains Project -> Task and
    # retain it through the eventual INSERT/commit.
    from app.task_locking import (
        AUTOMATIC_TASK_CREATION_ALLOWED,
        acquire_automatic_task_creation_authority,
    )

    creation_admission = await acquire_automatic_task_creation_authority(
        session, project_id
    )
    if creation_admission != AUTOMATIC_TASK_CREATION_ALLOWED:
        logger.info(
            "[AUTO-REVIEW] Automatic task creation held for PR #%d: %s",
            pr_number,
            creation_admission,
        )
        await session.rollback()
        return

    async def _current_pr_target() -> dict:
        return await get_pull_request(
            gh_token,
            owner,
            repo,
            pr_number,
            project_id=str(project_id),
        )

    admission = await prepare_direct_review_admission(
        session,
        project_id=project_id,
        pr_number=pr_number,
        fetch_live_pr=_current_pr_target,
    )
    if not admission.allowed:
        logger.info(
            "[AUTO-REVIEW] Admission declined for PR #%d: %s",
            pr_number,
            admission.reason,
        )
        return

    # ── Guard: rate limit ──
    one_hour_ago = now - timedelta(hours=1)
    recent_tasks = (
        (
            await session.execute(
                select(Task).where(
                    Task.project_id == project_id,
                    Task.created_at >= one_hour_ago,
                )
            )
        )
        .scalars()
        .all()
    )
    recent_review_count = sum(
        1 for t in recent_tasks if (t.context or {}).get("auto_review") is True
    )
    if recent_review_count >= max_per_hour:
        logger.info(
            "[AUTO-REVIEW] Rate limit hit (%d/%d in last hour) for project %s",
            recent_review_count,
            max_per_hour,
            project_id,
        )
        return

    # ── Guard: concurrent limit ──
    from app.review_pipeline import _PIPELINE_ACTIVE_STATUSES

    active_tasks = (
        (
            await session.execute(
                select(Task).where(
                    Task.project_id == project_id,
                    Task.status.in_(_PIPELINE_ACTIVE_STATUSES),
                )
            )
        )
        .scalars()
        .all()
    )
    active_review_count = sum(
        1 for t in active_tasks if (t.context or {}).get("auto_review") is True
    )
    if active_review_count >= max_concurrent:
        logger.info(
            "[AUTO-REVIEW] Concurrent limit hit (%d/%d) for project %s",
            active_review_count,
            max_concurrent,
            project_id,
        )
        return

    # ── Create the review task ──
    task = Task(
        project_id=project_id,
        title=f"Review PR #{pr_number}: {pr_title[:80]}",
        payloads=[
            {
                "type": "review",
                "pr_number": pr_number,
                "branch": branch,
                "base_branch": base_branch,
            }
        ],
        context={
            "auto_review": True,
            "pr_number": pr_number,
            "pr_title": pr_title,
            "pr_url": f"https://github.com/{owner}/{repo}/pull/{pr_number}",
            "review_loop": {
                "pr_number": pr_number,
                "branch": branch,
                "base_branch": base_branch,
                "review_target": admission.review_target,
                "target_assigned_by_backend": True,
            },
        },
        status=TaskStatus.QUEUED,
        created_by="SYSTEM_AUTO_REVIEW",
    )
    session.add(task)
    await session.commit()
    logger.info(
        "[AUTO-REVIEW] Spawned task %s for PR #%d on %s/%s",
        task.id,
        pr_number,
        owner,
        repo,
    )
