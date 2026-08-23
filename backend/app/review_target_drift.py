"""Shared review-target drift checks for approval and change-request fences."""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Task

logger = logging.getLogger(__name__)
_GITHUB_PR_FILES_MAX = 3000


async def _resolve_live_review_target(
    task: Task,
    session: AsyncSession,
    pr_number: int,
) -> dict[str, str | int] | None:
    """Return the PR's current immutable review target, when GitHub is reachable."""
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
    except Exception as exc:  # noqa: BLE001 - target lookup is best-effort
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
    pr_number: int | None = None,
    reviewed_base_sha: str,
    reviewed_head_sha: str,
    live_base_sha: str,
    live_head_sha: str,
) -> bool:
    """Return whether moved-base traffic invalidates the reviewed guidance.

    A base move is material only when its changed files overlap the files in
    the reviewed PR diff.  The moved-base compare remains fail-closed because
    GitHub caps its inline compare file collection.  For the reviewed PR,
    use the paginated files endpoint whenever ``pr_number`` is available so a
    large rollup is not mistaken for an incomplete comparison.
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
        from app.github import get_pr_files
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
        if base_move is None:
            return True

        if pr_number is not None:
            reviewed_diff = await get_pr_files(
                token,
                owner,
                repo,
                int(pr_number),
                project_id=project_id,
            )
        else:
            reviewed_diff = await get_changed_files_with_patches_between_refs(
                token,
                owner,
                repo,
                reviewed_base_sha,
                reviewed_head_sha,
                project_id=project_id,
            )

        if (
            pr_number is not None
            and isinstance(reviewed_diff, list)
            and len(reviewed_diff) >= _GITHUB_PR_FILES_MAX
        ):
            # get_pr_files is bounded at GitHub's 30-page/3000-file limit and
            # does not expose whether that bound was reached.
            return True

        def _validated_filenames(rows: object) -> set[str] | None:
            if not isinstance(rows, list):
                return None
            names: set[str] = set()
            for row in rows:
                if not isinstance(row, dict) or not isinstance(
                    row.get("filename"), str
                ):
                    return None
                names.add(row["filename"])
            return names

        base_files = _validated_filenames(base_move)
        reviewed_files = _validated_filenames(reviewed_diff)
        if base_files is None or reviewed_files is None:
            return True
        return bool(base_files & reviewed_files)
    except Exception as exc:  # noqa: BLE001 - comparison must fail closed
        logger.warning(
            "[REVIEW PIPELINE] Could not compare moved base against reviewed "
            "PR diff for task %s — keeping review stale: %s",
            task.id,
            exc,
        )
        return True
