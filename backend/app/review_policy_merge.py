"""Backend-owned review-policy stamping at automatic merge call sites."""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.models import Task
from app.review_merge_authority import (
    MergeAuthorityDecision,
    MergePreSubmissionRefused,
    MergeSubmissionIndeterminate,
    attempt_merge_with_authority,
)
from app.ci_merge_helpers import (
    _fetch_live_review_target,
    _merge_commit_parent_matches_checked_base,
)


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
    source_authority: Callable[[AsyncSession], Awaitable[Task | None]] | None = None,
    submission_intent: Callable[[AsyncSession, dict], Awaitable[dict | None]]
    | None = None,
    submission_start: Callable[
        [AsyncSession, MergeAuthorityDecision],
        Awaitable[Any | None],
    ]
    | None = None,
    submission_receipt: Callable[
        [AsyncSession, MergeAuthorityDecision, dict, str], Awaitable[None]
    ]
    | None = None,
) -> tuple[MergeAuthorityDecision, dict | None]:
    """Bypass model-review policy, but never target/lock/fix safety."""
    task_id = task.id
    fresh_task = await session.get(Task, task_id, populate_existing=True)
    if fresh_task is None:
        raise ValueError("explicit merge source task is unavailable")
    project_id = str(fresh_task.project_id)
    review_loop_snapshot = dict((fresh_task.context or {}).get("review_loop") or {})
    # Credential resolution may commit the caller's transaction.  Snapshot the
    # source through an awaited refresh, then release the read transaction before
    # the live GitHub lookup; later callbacks revalidate the durable claim.
    await session.commit()
    target = await _fetch_live_review_target(
        gh_token,
        owner,
        repo,
        pr_number,
        project_id=project_id,
    )
    checked_head = str(target.get("head_sha") or "")
    checked_base = str(target.get("base_sha") or "")
    if not checked_head or not checked_base:
        raise ValueError("explicit merge target is unresolved")
    if submission_intent is not None:
        ledger = await submission_intent(session, target)
        if ledger is None:
            raise MergePreSubmissionRefused(
                "the exact approved target or guarded project policy changed"
            )
        checked_head = str(ledger.get("head_sha") or "")
        checked_base = str(ledger.get("base_sha") or "")
        merge_decision = MergeAuthorityDecision(
            True,
            checked_head,
            checked_base,
            review_loop_snapshot,
        )
        if ledger.get("state") == "merged":
            fact = dict(ledger.get("merge_fact") or {})
            return merge_decision, {
                "merged": True,
                "sha": str(fact.get("merge_sha") or ""),
                "message": str(fact.get("message") or "merged"),
                "resumed": True,
            }
        observed_exact_merge = bool(
            target.get("merged") is True
            and str(target.get("head_sha") or "") == checked_head
            and str(target.get("head_ref") or "") == str(ledger.get("head_ref") or "")
            and str(target.get("base_ref") or "") == str(ledger.get("base_ref") or "")
            and str(target.get("merge_commit_sha") or "")
        )
        if observed_exact_merge:
            observed_exact_merge = await _merge_commit_parent_matches_checked_base(
                gh_token,
                owner,
                repo,
                str(target.get("merge_commit_sha") or ""),
                checked_base,
                project_id=project_id,
            )
        if observed_exact_merge:
            if submission_start is None or submission_receipt is None:
                raise RuntimeError("observed approval merge lacks durable protocol")
            started_authority = await submission_start(session, merge_decision)
            if started_authority is None:
                raise MergeSubmissionIndeterminate(
                    "observed approval merge could not linearize its receipt"
                )
            authority_token = await started_authority.acquire_send_lease(session)
            if not authority_token:
                raise MergeSubmissionIndeterminate(
                    "observed approval merge lost its exact receipt authority"
                )
            observed_result = {
                "merged": True,
                "sha": str(target.get("merge_commit_sha") or ""),
                "message": "exact merged PR observed during reconciliation",
                "observed_merged": True,
                "checked_base_parent_verified": True,
            }
            await submission_receipt(
                session, merge_decision, observed_result, authority_token
            )
            return merge_decision, observed_result
        if ledger.get("state") == "ambiguous":
            raise MergeSubmissionIndeterminate(
                "approval merge submission remains ambiguous; no PUT replayed"
            )
    task = await session.get(Task, task_id, populate_existing=True)
    if task is None:
        return (
            MergeAuthorityDecision(False, checked_head, checked_base, review_loop_snapshot),
            None,
        )
    return await attempt_merge_with_authority(
        task,
        session,
        merge_call=merge_call,
        gh_token=gh_token,
        owner=owner,
        repo=repo,
        ci_loop=review_loop_snapshot,
        pr_number=pr_number,
        branch=branch,
        base_branch=base_branch,
        original_task_id=str(task_id),
        checked_head_sha=checked_head,
        checked_base_sha=checked_base,
        explicit_review_override=True,
        source_authority=source_authority,
        submission_start=submission_start,
        submission_receipt=submission_receipt,
    )
