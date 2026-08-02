"""Reconcile CI-waiting tasks whose tracked pull request was superseded."""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.models import Task
from app.time_utils import _utcnow

logger = logging.getLogger("blueprint")

_REPLACEMENT_PUBLICATION_GRACE_SECONDS = 15 * 60
_REPLACEMENT_WAIT_KEY = "closed_pr_replacement_wait_started"

# These fields describe evidence, latches, or retry budgets for one concrete
# pull-request attempt. A replacement PR may reuse the same branch while
# pointing at a different head, so carrying any of them across the PR-number
# boundary can merge without fresh proof or immediately exhaust a stale
# timeout/retry budget. Chain-level fields such as ``iteration``,
# ``original_task_id`` and ``fix_reason`` intentionally remain intact.
_REPLACEMENT_ATTEMPT_KEYS = frozenset(
    {
        "_behind_update_retries",
        "_ci_backstop_first_defer_at",
        "_conflict_deferred_count",
        "_conflict_deferred_state",
        "acceptance_test_targeted_descriptor",
        "acceptance_test_targeted_green",
        "ci_checks_seen",
        "ci_first_check_seen_at",
        "ci_fix_spawned",
        "ci_last_polled",
        "ci_passed",
        "ci_poll_interval",
        "conflict_fix_spawned",
        "escalated",
        "fetch_escalated",
        "last_merge_msg",
        "merge_error_streak",
        "merge_escalated",
        "merge_failures",
        "merge_in_progress_resets",
        "merge_last_error",
        "merge_pattern_alerted",
        "merge_pattern_paused",
        "merge_pattern_pr_count",
        "merge_rule_blocked",
        "mergeable_null_count",
        "mergeable_null_first_ts",
        "mergeable_null_reopen_attempts",
        "mergeable_state",
        "no_checks_escalated",
        "no_ci_degraded_block_reason",
        "no_ci_degraded_blocked",
        "no_ci_escalation_base",
        "no_ci_escalation_verify_gate",
        "no_ci_no_test_block_reason",
        "no_ci_no_test_blocked",
        "no_ci_runtime_recorded",
        "pending_reopen",
        "pr_fetch_failures",
        "recovered_test_command",
        "reopen_alert_fired",
        "reopen_attempts",
        "review_approved",
        "review_changes_requested",
        "review_history",
        "review_recovery_at",
        "review_recovery_attempted",
        "review_wait_escalated",
        "review_wait_respawn_count",
        "review_wait_respawn_task_id",
        "review_wait_respawned",
        "review_wait_started",
        "test_coverage_result",
        "unverified_merge_reason",
        "update_branch_failures",
        "verdict",
        "verify_tests_output",
        "verify_tests_retries",
        "verify_tests_spawn_deferrals",
        "verify_tests_task_id",
        "verify_tests_verdict",
    }
)

_PR_CHAIN_STATE_KEYS = frozenset(
    {
        "base_branch",
        "branch",
        "churn_detections",
        "drift_signal",
        "fix_agent_responses",
        "fix_reason",
        "hotfix",
        "issue_ledger",
        "iteration",
        "iteration_reset_at",
        "iteration_reset_by",
        "max_iterations",
        "original_task_id",
        "reviewer_model",
        "scope_overflow_warned",
    }
)


def reset_pr_attempt_state(review_loop: dict | None) -> dict:
    """Retain only reviewed chain metadata across a concrete PR boundary.

    This is intentionally an allowlist, not a list of known fields to delete:
    every new/unknown CI, review, verifier, merge, retry, or clock field defaults
    to attempt-scoped and is cleared.  The retained fields are the audited
    chain-level convergence budget/provenance, branch coordinates, reviewer pin,
    and issue/churn memory that must survive a replacement PR.

    ``_REPLACEMENT_ATTEMPT_KEYS`` remains the explicit documentation and test
    surface for today's known latches, while this default-deny copy prevents a
    future latch from silently crossing attempts before that list is updated.
    """

    loop = dict(review_loop or {})
    return {key: loop[key] for key in _PR_CHAIN_STATE_KEYS if key in loop}




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
    closed_head = (pr_data.get("head") or {}).get("ref") or ""
    closed_base = ((pr_data.get("base") or {}).get("ref") or "") or review_loop.get(
        "base_branch", ""
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
        # A fix agent can finish its bounded local gate seconds before the new
        # PR is visible. The old closed PR remains in review_loop during that
        # publication window; terminalizing immediately kills the publishing VM
        # and strands its corrected branch. Give the authoritative same-head/base
        # lookup a bounded grace period, persisted across monitor processes.
        now = _utcnow()
        started_raw = review_loop.get(_REPLACEMENT_WAIT_KEY)
        try:
            started = (
                datetime.fromisoformat(started_raw)
                if isinstance(started_raw, str) and started_raw
                else now
            )
        except (TypeError, ValueError):
            started = now
        elapsed = max(0.0, (now - started).total_seconds())
        if elapsed < _REPLACEMENT_PUBLICATION_GRACE_SECONDS:
            review_loop[_REPLACEMENT_WAIT_KEY] = started.isoformat()
            review_loop["polls_since_pr_fetch"] = 999
            remaining = int(_REPLACEMENT_PUBLICATION_GRACE_SECONDS - elapsed)
            task.latest_status_message = (
                f"PR #{pr_number} closed; waiting for replacement publication "
                f"on {closed_head} ({remaining}s grace remaining)"
            )[:500]
            task.context = {**(task.context or {}), "review_loop": review_loop}
            flag_modified(task, "context")
            session.add(task)
            await session.commit()
            return True
        review_loop.pop(_REPLACEMENT_WAIT_KEY, None)
        return False

    from app import pause_clock
    from app.ci_merge_helpers import arm_ci_wait

    replacement_head = (replacement_pr.get("head") or {}).get("sha") or ""
    replacement_base = (
        (replacement_pr.get("base") or {}).get("ref") or ""
    ) or closed_base
    chain_loop = reset_pr_attempt_state(review_loop)
    review_loop.clear()
    review_loop.update(chain_loop)
    review_loop["branch"] = (
        (replacement_pr.get("head") or {}).get("ref") or ""
    ) or closed_head
    arm_ci_wait(
        review_loop,
        pr_number=replacement_number,
        head_sha=replacement_head,
        base_branch=replacement_base,
        now_iso=_utcnow().isoformat(),
        pause_baseline=await pause_clock.current_baseline(session),
    )
    review_loop["polls_since_pr_fetch"] = 999
    review_loop.pop(_REPLACEMENT_WAIT_KEY, None)


    rebound_context = {**(task.context or {}), "review_loop": review_loop}
    # This legacy parent-level verdict is also PR-scoped. Leaving it behind
    # lets rollup convergence treat the replacement as already approved.
    rebound_context.pop("blueprint_review_verdict", None)
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
        "[CI] rebound task %s from closed PR #%s to open replacement PR #%s (%s -> %s)",
        task.id,
        pr_number,
        replacement_number,
        closed_head,
        closed_base,
    )
    return True
