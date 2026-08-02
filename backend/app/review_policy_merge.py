"""Backend-owned review-policy stamping at automatic merge call sites."""

from __future__ import annotations

from typing import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.models import Task
from app.review_merge_authority import (
    MergeAuthorityDecision,
    attempt_merge_with_authority,
)
from app.ci_merge_helpers import _fetch_live_review_target


async def attempt_policy_bound_merge(
    task: Task,
    session: AsyncSession,
    *,
    merge_call: Callable[[str], Awaitable[dict]],
    gh_token: str,
    owner: str,
    repo: str,
    pr_number: int,
    branch: str,
    base_branch: str,
    checked_head_sha: str,
    checked_base_sha: str,
    review_not_required_reason: str | None,
    retry_nonterminal: bool = False,
) -> tuple[MergeAuthorityDecision, dict | None]:
    """Stamp an exact backend policy and submit through merge authority.

    A no-review reason is valid only before any review history exists. Once a
    chain enters review, its binding ledger/approval protocol cannot be replaced
    by a later cheap-policy marker.
    """
    target: dict[str, str | int] = {
        "project_id": str(task.project_id),
        "pr_number": int(pr_number),
        "base_sha": str(checked_base_sha),
        "head_sha": str(checked_head_sha),
    }
    loop = dict((task.context or {}).get("review_loop") or {})
    loop.update(
        {
            "pr_number": int(pr_number),
            "branch": branch,
            "base_branch": base_branch,
            "ci_passed": True,
            "ci_waiting": False,
            "ci_checked_head_sha": str(checked_head_sha),
            "ci_checked_base_sha": str(checked_base_sha),
            "head_sha": str(checked_head_sha),
            "base_sha": str(checked_base_sha),
            "review_target": target,
        }
    )
    history = loop.get("review_history")
    pristine_review_state = bool(
        (history is None or history == [])
        and loop.get("issue_ledger") is None
        and not any(
            loop.get(key)
            for key in (
                "authoritative_review_required",
                "authoritative_review_task_id",
                "review_wait_started",
                "review_approved",
            )
        )
    )
    if review_not_required_reason and pristine_review_state:
        loop["review_not_required"] = True
        loop["review_policy_reason"] = review_not_required_reason
    else:
        loop.pop("review_not_required", None)
        loop.pop("review_policy_reason", None)
    task.context = {**(task.context or {}), "review_loop": loop}
    flag_modified(task, "context")
    session.add(task)
    return await attempt_merge_with_authority(
        task,
        session,
        merge_call=merge_call,
        gh_token=gh_token,
        owner=owner,
        repo=repo,
        ci_loop=loop,
        pr_number=pr_number,
        branch=branch,
        base_branch=base_branch,
        original_task_id=str(task.id),
        checked_head_sha=checked_head_sha,
        checked_base_sha=checked_base_sha,
        retry_nonterminal=retry_nonterminal,
    )


async def attempt_explicit_human_merge(
    task: Task,
    session: AsyncSession,
    *,
    merge_call: Callable[[str], Awaitable[dict]],
    gh_token: str,
    owner: str,
    repo: str,
    pr_number: int,
    branch: str,
    base_branch: str,
) -> tuple[MergeAuthorityDecision, dict | None]:
    """Bypass model-review policy, but never target/lock/fix safety."""
    target = await _fetch_live_review_target(
        gh_token,
        owner,
        repo,
        pr_number,
        project_id=str(task.project_id),
    )
    checked_head = str(target.get("head_sha") or "")
    checked_base = str(target.get("base_sha") or "")
    if not checked_head or not checked_base:
        raise ValueError("explicit merge target is unresolved")
    return await attempt_merge_with_authority(
        task,
        session,
        merge_call=merge_call,
        gh_token=gh_token,
        owner=owner,
        repo=repo,
        ci_loop=dict((task.context or {}).get("review_loop") or {}),
        pr_number=pr_number,
        branch=branch,
        base_branch=base_branch,
        original_task_id=str(task.id),
        checked_head_sha=checked_head,
        checked_base_sha=checked_base,
        explicit_review_override=True,
    )
