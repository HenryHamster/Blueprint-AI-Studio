"""Binding issue-ledger and exact-review proof gate for PR publication."""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.context_bounds import REVIEW_HISTORY_MAX_ENTRIES
from app.issue_ledger import (
    build_issue_ledger,
    issue_ledger_complete_for_history,
)
from app.models import Task, _utcnow

logger = logging.getLogger(__name__)


def review_target_matches_merge_identity(
    review_target: dict[str, str | int] | None,
    *,
    project_id: str,
    pr_number: int,
) -> bool:
    """Return whether a purported immutable target belongs to this merge."""
    if not isinstance(review_target, dict):
        return False
    try:
        target_pr_number = int(review_target.get("pr_number") or 0)
    except (TypeError, ValueError):
        return False
    return bool(
        project_id
        and str(review_target.get("project_id") or "") == project_id
        and target_pr_number == int(pr_number)
        and str(review_target.get("base_sha") or "")
        and str(review_target.get("head_sha") or "")
    )


async def _enforce_authoritative_review_merge_gate(
    task: Task,
    session: AsyncSession,
    *,
    ci_loop: dict,
    pr_number: int,
    branch: str,
    base_branch: str,
    original_task_id: str,
    live_review_target: dict[str, str | int] | None,
) -> bool:
    """Require empty binding blockers and latest exact-target APPROVE."""
    raw_history = ci_loop.get("review_history") or []
    history = raw_history if isinstance(raw_history, list) else []
    persisted_ledger = ci_loop.get("issue_ledger")
    try:
        ledger = build_issue_ledger(
            history,
            **(
                {"seed_ledger": persisted_ledger}
                if persisted_ledger is not None
                else {}
            ),
            stamp_complete=True,
        )
    except ValueError:
        ledger = build_issue_ledger(history, stamp_complete=True)
    ledger_proof_complete = issue_ledger_complete_for_history(ledger, history)
    if not ledger_proof_complete and len(history) >= REVIEW_HISTORY_MAX_ENTRIES:
        ledger["open"].append(
            {
                "issue_id": "BP-ledger-proof-incomplete",
                "severity": "major",
                "description": "durable blocker ledger missing after history compaction",
            }
        )
    open_blockers = list(ledger.get("open") or [])

    # Every dictionary here is review evidence.  A trailing COMMENT-only,
    # feedback-only, or malformed row invalidates an older APPROVE.
    latest_review = next(
        (entry for entry in reversed(history) if isinstance(entry, dict)),
        {},
    )
    latest_is_approval = (
        str(latest_review.get("verdict") or "").upper() == "APPROVE"
    )
    approved_head = str(
        latest_review.get("reviewed_head_sha")
        or latest_review.get("head_sha")
        or ""
    )
    approved_base = str(
        latest_review.get("reviewed_base_sha")
        or latest_review.get("base_sha")
        or ""
    )
    live_head = str((live_review_target or {}).get("head_sha") or "")
    live_base = str((live_review_target or {}).get("base_sha") or "")
    project_id = str(task.project_id) if task.project_id else ""
    target_identity_is_exact = review_target_matches_merge_identity(
        live_review_target,
        project_id=project_id,
        pr_number=pr_number,
    )
    approval_is_exact = bool(
        latest_is_approval
        and latest_review.get("target_verified_by_backend") is True
        and str(latest_review.get("review_task_id") or "").strip()
        and ledger_proof_complete
        and target_identity_is_exact
        and approved_head
        and approved_base
        and approved_head == live_head
        and approved_base == live_base
    )
    if not open_blockers and approval_is_exact:
        for transient_key in (
            "authoritative_review_required",
            "authoritative_review_target_unresolved_at",
            "authoritative_review_task_id",
            "review_not_required",
            "review_policy_reason",
        ):
            ci_loop.pop(transient_key, None)
        ci_loop["review_approved"] = True
        ci_loop["review_target"] = dict(live_review_target or {})
        ci_loop["head_sha"] = live_head
        ci_loop["issue_ledger"] = ledger
        task.context = {**(task.context or {}), "review_loop": ci_loop}
        flag_modified(task, "context")
        return True

    from app.review_pipeline import _spawn_pipeline_task

    # Spawn helpers may commit/rollback and expire ORM instances, so snapshot
    # everything needed to reconstruct the fail-closed gate first.
    source_task_id = task.id
    source_context = dict(task.context or {})
    source_prompt = task.first_payload.get("prompt", "") or (task.description or "")
    gated_loop = dict(ci_loop)
    for stale_key in (
        "review_not_required",
        "review_policy_reason",
        "verdict",
        "authoritative_review_task_id",
    ):
        gated_loop.pop(stale_key, None)
    gated_loop["review_approved"] = False
    gated_loop["authoritative_review_required"] = True
    gated_loop["ci_waiting"] = True
    gated_loop["issue_ledger"] = ledger
    if not gated_loop.get("review_wait_started"):
        gated_loop["review_wait_started"] = _utcnow().isoformat()
    if target_identity_is_exact:
        gated_loop["authoritative_review_target"] = dict(live_review_target or {})
        gated_loop.pop("authoritative_review_target_unresolved_at", None)
    else:
        gated_loop.pop("authoritative_review_target", None)
        gated_loop["authoritative_review_target_unresolved_at"] = (
            _utcnow().isoformat()
        )
    task.context = {**source_context, "review_loop": gated_loop}
    flag_modified(task, "context")

    review_task_id = ""
    if target_identity_is_exact:
        review_task, _created = await _spawn_pipeline_task(
            task,
            "review",
            session,
            pr_number,
            branch,
            base_branch,
            original_prompt=source_prompt,
            original_task_id=original_task_id,
            review_target=live_review_target,
        )
        if review_task is not None:
            review_task_id = str(review_task.id)

    post_spawn_context = task.__dict__.get("context")
    refreshed_task = (
        task
        if isinstance(post_spawn_context, dict)
        else await session.get(Task, source_task_id, populate_existing=True)
    )
    if refreshed_task is None:
        logger.error(
            "[CI] Merge held for PR #%s but source task %s disappeared after "
            "the authoritative review spawn attempt",
            pr_number,
            source_task_id,
        )
        return False

    refreshed_context = dict(refreshed_task.context or {})
    refreshed_loop = dict(refreshed_context.get("review_loop") or {})
    refreshed_loop.update(gated_loop)
    for stale_key in ("verdict", "review_not_required", "review_policy_reason"):
        refreshed_loop.pop(stale_key, None)
    if review_task_id:
        refreshed_loop["authoritative_review_task_id"] = review_task_id
        refreshed_task.latest_status_message = (
            f"Merge held for PR #{pr_number}: authoritative blockers/proof "
            f"require exact-target review {review_task_id}"
        )
    elif not target_identity_is_exact:
        refreshed_loop.pop("authoritative_review_task_id", None)
        refreshed_loop.pop("authoritative_review_target", None)
        refreshed_task.latest_status_message = (
            f"Merge held for PR #{pr_number}: could not resolve the live "
            "review target; retrying before merge"
        )
    else:
        refreshed_loop.pop("authoritative_review_task_id", None)
        refreshed_task.latest_status_message = (
            f"Merge held for PR #{pr_number}: authoritative blockers/proof "
            "require an exact-target review"
        )
    refreshed_task.context = {
        **refreshed_context,
        "review_loop": refreshed_loop,
    }
    flag_modified(refreshed_task, "context")
    session.add(refreshed_task)
    logger.warning(
        "[CI] Merge held for PR #%s: %d authoritative blocker(s), "
        "exact approval=%s; exact-target review required",
        pr_number,
        len(open_blockers),
        approval_is_exact,
    )
    return False
