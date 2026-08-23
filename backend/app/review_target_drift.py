"""Review-target drift helpers for the approval fence.

Extracted from ``task_lifecycle`` verbatim. Both functions answer the same
question from different angles -- what is this PR's *current* immutable review
target, and does a move in that target actually invalidate an existing
approval -- and both are defence in depth around the review lifecycle rather
than part of it, so they read better beside each other than buried in the
lifecycle module.

Behaviour is unchanged, including the deliberate fail-closed posture: any
missing provenance, compare failure, or unreachable GitHub keeps the existing
stale-approval outcome rather than silently accepting a stale review.
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Task

logger = logging.getLogger(__name__)


async def _resolve_live_review_target(
    task: Task,
    session: AsyncSession,
    pr_number: int,
) -> dict[str, str | int] | None:
    """Return the PR's current immutable review target, when GitHub is reachable.

    Target-aware review deduplication is defence in depth, so a transient
    metadata lookup must not block the normal review lifecycle.  Callers pass
    ``None`` on failure and retain the legacy same-chain guard in that case.
    """
    try:
        from app.github import get_pull_request
        from app.routers.agent import get_github_token_for_task

        token, owner, repo = await get_github_token_for_task(task.id, session)
        pr_data = await get_pull_request(
            token,
            owner,
            repo,
            pr_number,
            project_id=str(task.project_id),
        )
        base_sha = str(pr_data.get("base", {}).get("sha", ""))
        head_sha = str(pr_data.get("head", {}).get("sha", ""))
        if base_sha and head_sha:
            return {
                "project_id": str(task.project_id),
                "pr_number": int(pr_number),
                "base_sha": base_sha,
                "head_sha": head_sha,
            }
    except Exception as exc:
        logger.info(
            "[REVIEW PIPELINE] Could not resolve immutable review target for "
            "PR #%s (task %s): %s — using legacy same-chain dedup",
            pr_number,
            task.id,
            exc,
        )
    return None


async def _base_move_invalidates_approval(
    task: Task,
    session: AsyncSession,
    *,
    reviewed_base_sha: str,
    reviewed_head_sha: str,
    live_base_sha: str,
    live_head_sha: str,
) -> bool:
    """Return whether a moved base still makes an approval stale.

    A base move is material only when the files changed by that move overlap
    the files in the reviewed ``reviewed_base_sha..reviewed_head_sha`` diff.
    File-level overlap is deliberately conservative: it may re-review two
    independent edits in one file, but it cannot prove that line-level edits
    compose safely.  Missing provenance, a compare failure, malformed output,
    or GitHub's capped compare response all return ``True`` so an indeterminate
    result keeps the existing fail-closed stale-approval behavior.

    This is not a replacement for the immutable head/base fence.  It only
    prevents unrelated base traffic from invalidating an otherwise exact
    approval; a changed head remains stale at the caller.
    """
    if reviewed_base_sha == live_base_sha:
        return False
    if (
        not reviewed_base_sha
        or not reviewed_head_sha
        or not live_base_sha
        or reviewed_head_sha != live_head_sha
    ):
        return True

    try:
        from app.github_refs import get_changed_files_with_patches_between_refs
        from app.routers.agent import get_github_token_for_task

        token, owner, repo = await get_github_token_for_task(task.id, session)
        project_id = str(task.project_id) if task.project_id else None
        base_move = await get_changed_files_with_patches_between_refs(
            token,
            owner,
            repo,
            reviewed_base_sha,
            live_base_sha,
            project_id=project_id,
        )
        reviewed_diff = await get_changed_files_with_patches_between_refs(
            token,
            owner,
            repo,
            reviewed_base_sha,
            reviewed_head_sha,
            project_id=project_id,
        )
        if base_move is None or reviewed_diff is None:
            return True

        base_files = {row["filename"] for row in base_move}
        reviewed_files = {row["filename"] for row in reviewed_diff}
        return bool(base_files & reviewed_files)
    except Exception as exc:
        logger.warning(
            "[REVIEW PIPELINE] Could not compare moved base against reviewed "
            "PR diff for task %s — keeping approval stale: %s",
            task.id,
            exc,
        )
        return True
