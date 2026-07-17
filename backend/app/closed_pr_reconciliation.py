"""Reconcile CI-waiting tasks whose tracked pull request was superseded."""

from __future__ import annotations

import logging

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.models import Task

logger = logging.getLogger("blueprint")


async def reconcile_closed_pr_replacement(
    *,
    task: Task,
    session: AsyncSession,
    review_loop: dict,
    pr_number: int,
    pr_data: dict,
    github_token: str,
    github_owner: str,
    github_repo: str,
) -> bool:
    """Rebind ``task`` to its unique open same-head/base replacement.

    Returns ``True`` when the caller must stop processing the stale PR for this
    sweep. Lookup uncertainty persists retry state and also returns ``True`` so
    an outage cannot terminalize work that may have a live replacement.
    """
    closed_head = ((pr_data.get("head") or {}).get("ref") or "")
    closed_base = (
        ((pr_data.get("base") or {}).get("ref") or "")
        or review_loop.get("base_branch", "")
    )
    if not closed_head or not closed_base:
        return False

    # Import lazily to preserve the existing test/runtime patch point.
    from app.github import get_open_pull_request_for_head_branch

    try:
        replacement_pr = await get_open_pull_request_for_head_branch(
            github_token,
            github_owner,
            github_repo,
            closed_head,
            closed_base,
            project_id=str(task.project_id),
        )
    except HTTPException as exc:
        review_loop["polls_since_pr_fetch"] = 999
        task.latest_status_message = (
            f"PR #{pr_number} closed; replacement lookup "
            "unavailable — retrying before terminalizing"
        )
        task.context = {**(task.context or {}), "review_loop": review_loop}
        flag_modified(task, "context")
        session.add(task)
        await session.commit()
        logger.warning(
            "[CI] replacement lookup for closed PR #%s failed: %s",
            pr_number,
            exc,
        )
        return True

    replacement_number = (
        (replacement_pr or {}).get("number")
        if isinstance(replacement_pr, dict)
        else None
    )
    if not replacement_number or replacement_number == pr_number:
        return False

    replacement_head = ((replacement_pr.get("head") or {}).get("sha") or "")
    review_loop["pr_number"] = replacement_number
    review_loop["branch"] = (
        ((replacement_pr.get("head") or {}).get("ref") or "") or closed_head
    )
    review_loop["base_branch"] = (
        ((replacement_pr.get("base") or {}).get("ref") or "") or closed_base
    )
    review_loop["ci_waiting"] = True
    review_loop["polls_since_pr_fetch"] = 999
    if replacement_head:
        review_loop["head_sha"] = replacement_head

    rebound_context = {**(task.context or {}), "review_loop": review_loop}
    if rebound_context.get("rollup_pr_number") == pr_number:
        rebound_context["rollup_pr_number"] = replacement_number
    task.context = rebound_context
    task.latest_status_message = (
        f"PR #{pr_number} was superseded by open PR "
        f"#{replacement_number}; resumed CI monitoring"
    )
    flag_modified(task, "context")
    session.add(task)
    await session.commit()
    logger.info(
        "[CI] rebound task %s from closed PR #%s to open "
        "replacement PR #%s (%s -> %s)",
        task.id,
        pr_number,
        replacement_number,
        closed_head,
        closed_base,
    )
    return True
