"""Attempt-scoped reconciliation for operator requeue of rollup parents."""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.closed_pr_reconciliation import reset_pr_attempt_state
from app.models import Project, Task, TaskStatus
from app.time_utils import _utcnow

logger = logging.getLogger("blueprint")

_TERMINAL_STATUSES = frozenset(
    {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
)


async def _store_context(task: Task, session: AsyncSession, context: dict) -> None:
    task.context = context
    flag_modified(task, "context")
    session.add(task)


def _safe_identity_context(context: dict, review_loop: dict, pr_number: int) -> dict:
    """Keep a possibly-live PR identity while stripping stale attempt proof."""

    safe_loop = reset_pr_attempt_state(review_loop)
    safe_loop["pr_number"] = pr_number
    safe = {
        **context,
        "rollup_pr_number": pr_number,
        "review_loop": safe_loop,
    }
    safe.pop("blueprint_review_verdict", None)
    return safe


async def _bind_fresh_pr_attempt(
    *,
    task: Task,
    session: AsyncSession,
    context: dict,
    review_loop: dict,
    pr_data: dict,
) -> dict:
    """Bind ``context`` to one PR with fresh attempt-scoped latches/clocks."""

    from app import pause_clock
    from app.ci_merge_helpers import arm_ci_wait

    pr_number = pr_data.get("number")
    assert pr_number, "fresh PR attempt binding requires a PR number"
    head = pr_data.get("head") or {}
    base = pr_data.get("base") or {}
    head_branch = head.get("ref") or review_loop.get("branch") or ""
    base_branch = base.get("ref") or review_loop.get("base_branch") or ""
    fresh_loop = reset_pr_attempt_state(review_loop)
    fresh_loop.update(
        {
            "pr_number": pr_number,
            "branch": head_branch,
            "base_branch": base_branch,
        }
    )
    head_sha = head.get("sha") or ""
    if head_sha:
        fresh_loop["head_sha"] = head_sha

    project = await session.get(Project, task.project_id)
    is_intermediate = bool(
        project is not None and project.is_staging_branch(base_branch)
    )
    if not is_intermediate:
        arm_ci_wait(
            fresh_loop,
            pr_number=pr_number,
            head_sha=head_sha or None,
            base_branch=base_branch or None,
            now_iso=_utcnow().isoformat(),
            pause_baseline=await pause_clock.current_baseline(session),
        )

    rebound = {
        **context,
        "rollup_pr_number": pr_number,
        "_is_rollup_review": True,
        "review_loop": fresh_loop,
    }
    if pr_data.get("html_url"):
        rebound["rollup_pr_url"] = pr_data["html_url"]
    rebound.pop("blueprint_review_verdict", None)
    return rebound


async def reconcile_terminal_rollup_requeue(
    task: Task,
    session: AsyncSession,
) -> str:
    """Prepare a terminal rollup parent for an operator-requested fresh attempt.

    Open or merged PRs retain identity but receive fresh CI/review state. Closed
    PRs adopt a unique open same-head/base replacement, when one exists, or
    release volatile identity so rollup management can create a fresh PR. Lookup
    uncertainty preserves identity to prevent duplicates while still dropping
    stale approval/verifier/backstop state. Active tasks are never modified.
    """

    from app.ci_merge_helpers import effective_pr_number
    from app.github import get_open_pull_request_for_head_branch, get_pull_request
    from app.routers.agent import get_github_token_for_task

    if task.status not in _TERMINAL_STATUSES:
        return "active_unchanged"

    context = dict(task.context or {})
    old_loop = dict(context.get("review_loop") or {})
    # A manual parent-level rebind is authoritative for terminal recovery even
    # when the stale review loop still names the closed predecessor.
    pr_number = context.get("rollup_pr_number") or effective_pr_number(context)
    if not pr_number:
        cleaned = reset_pr_attempt_state(old_loop)
        context.pop("blueprint_review_verdict", None)
        context.pop("rollup_pr_url", None)
        context.pop("_is_rollup_review", None)
        if cleaned:
            context["review_loop"] = cleaned
        else:
            context.pop("review_loop", None)
        await _store_context(task, session, context)
        return "no_pr_cleared"

    try:
        token, owner, repo = await get_github_token_for_task(task.id, session)
        pr_data = await get_pull_request(
            token,
            owner,
            repo,
            int(pr_number),
            project_id=str(task.project_id),
        )
    except Exception as exc:  # noqa: BLE001 - uncertainty must preserve identity
        await _store_context(
            task,
            session,
            _safe_identity_context(context, old_loop, pr_number),
        )
        logger.warning(
            "[ADMIN] Terminal requeue could not verify rollup PR #%s for task %s; "
            "identity preserved, attempt proof cleared: %r",
            pr_number,
            task.id,
            exc,
        )
        return "lookup_uncertain_identity_preserved"

    if pr_data.get("merged") or pr_data.get("state") == "open":
        rebound = await _bind_fresh_pr_attempt(
            task=task,
            session=session,
            context=context,
            review_loop=old_loop,
            pr_data=pr_data,
        )
        await _store_context(task, session, rebound)
        return "merged_pr_rearmed" if pr_data.get("merged") else "open_pr_rearmed"

    head_branch = (pr_data.get("head") or {}).get("ref") or ""
    base_branch = (pr_data.get("base") or {}).get("ref") or ""
    replacement = None
    if head_branch and base_branch:
        try:
            replacement = await get_open_pull_request_for_head_branch(
                token,
                owner,
                repo,
                head_branch,
                base_branch,
                project_id=str(task.project_id),
            )
        except Exception as exc:  # noqa: BLE001 - uncertainty must preserve identity
            await _store_context(
                task,
                session,
                _safe_identity_context(context, old_loop, pr_number),
            )
            logger.warning(
                "[ADMIN] Terminal requeue replacement lookup unavailable for PR "
                "#%s on task %s; identity preserved, attempt proof cleared: %r",
                pr_number,
                task.id,
                exc,
            )
            return "replacement_lookup_uncertain_identity_preserved"

    if replacement:
        rebound = await _bind_fresh_pr_attempt(
            task=task,
            session=session,
            context=context,
            review_loop=old_loop,
            pr_data=replacement,
        )
        await _store_context(task, session, rebound)
        return "replacement_pr_rearmed"

    cleaned = reset_pr_attempt_state(old_loop)
    released = {
        key: value
        for key, value in context.items()
        if key
        not in {
            "blueprint_review_verdict",
            "rollup_pr_number",
            "rollup_pr_url",
            "_is_rollup_review",
        }
    }
    # The branch owner must remain eligible to recreate its rollup after the
    # volatile marker is released (same invariant as rollup_management).
    released["owns_rollup_branch"] = True
    if cleaned:
        released["review_loop"] = cleaned
    else:
        released.pop("review_loop", None)
    await _store_context(task, session, released)
    return "closed_pr_released"
