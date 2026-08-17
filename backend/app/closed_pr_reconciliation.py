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
_REPLACEMENT_RETRY_INTERVAL_SECONDS = 120

# ``requires_approval`` is shared by several independent safety gates.  The
# review-wait backstop may release only the hold it created for one immutable PR
# attempt, so it records an exact, timer-bound ownership marker beside that
# attempt's review-loop state.
REVIEW_WAIT_APPROVAL_HOLD_KEY = "review_wait_approval_hold"
REVIEW_WAIT_APPROVAL_HOLD_OWNER = "ci_review_wait_backstop"


def revoke_review_wait_approval_hold(task: Task) -> bool:
    """Remove automatic hold provenance without disturbing an independent timer.

    Approval releases and explicit operator holds both end ownership by the
    review-wait backstop.  The timer is removed only when it is the timer named
    by that marker; a later spend/manual hold may have installed a new one.
    """

    context = dict(task.context or {})
    review_loop = dict(context.get("review_loop") or {})
    marker = review_loop.pop(REVIEW_WAIT_APPROVAL_HOLD_KEY, None)
    if not isinstance(marker, dict):
        return False
    if context.get("_approval_set_at") == marker.get("set_at"):
        context.pop("_approval_set_at", None)
    task.context = {**context, "review_loop": review_loop}
    flag_modified(task, "context")
    return True


def _stamp_replacement_retry_backoff(review_loop: dict) -> None:
    """Throttle unchanged closed-PR lookup/grace retries without losing state."""

    try:
        current_interval = int(review_loop.get("ci_poll_interval", 30) or 30)
    except (TypeError, ValueError):
        current_interval = 30
    review_loop["ci_last_polled"] = _utcnow().isoformat()
    review_loop["ci_poll_interval"] = max(
        current_interval, _REPLACEMENT_RETRY_INTERVAL_SECONDS
    )

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
        REVIEW_WAIT_APPROVAL_HOLD_KEY,
        "review_wait_respawn_count",
        "review_wait_respawn_task_id",
        "review_wait_respawned",
        "review_wait_deferred_at",
        "review_wait_spawn_claim",
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


def _owns_superseded_review_wait_approval_hold(
    task: Task,
    review_loop: dict,
    *,
    pr_number: int,
    pr_data: dict,
) -> bool:
    """Whether the current gate is exactly the old PR attempt's backstop hold."""

    closed_head_sha = str(((pr_data.get("head") or {}).get("sha")) or "")
    closed_base_sha = str(((pr_data.get("base") or {}).get("sha")) or "")
    return owns_review_wait_approval_hold(
        task,
        review_loop,
        pr_number=pr_number,
        head_sha=closed_head_sha,
        base_sha=closed_base_sha,
    )


def owns_review_wait_approval_hold(
    task: Task,
    review_loop: dict,
    *,
    pr_number: int,
    head_sha: str,
    base_sha: str,
) -> bool:
    """Whether only this exact review-wait backstop owns the approval gate."""

    if task.requires_approval is not True:
        return False
    marker = review_loop.get(REVIEW_WAIT_APPROVAL_HOLD_KEY)
    if not isinstance(marker, dict):
        return False
    set_at = marker.get("set_at")
    context = dict(task.context or {})
    if not head_sha or not base_sha or not isinstance(set_at, str):
        return False
    # The timer equality makes ownership exclusive: if a later hold replaced
    # the approval episode, replacement reconciliation leaves it untouched.
    if context.get("_approval_set_at") != set_at:
        return False
    # Spend, critical-file, and other explicitly-labelled holds always outrank
    # this marker even if they arrived while the backstop flag was already true.
    if context.get("approval_hold_reason") or context.get("touched_critical"):
        return False
    terminal_outcome = context.get("review_loop_terminal_outcome")
    if isinstance(terminal_outcome, dict) and terminal_outcome.get("resolved") is not True:
        return False
    try:
        marker_pr = int(marker.get("pr_number"))
    except (TypeError, ValueError):
        return False
    return (
        marker.get("owner") == REVIEW_WAIT_APPROVAL_HOLD_OWNER
        and str(marker.get("project_id") or "") == str(task.project_id)
        and marker_pr == int(pr_number)
        and str(marker.get("base_sha") or "") == str(base_sha)
        and str(marker.get("head_sha") or "") == str(head_sha)
    )


async def lookup_closed_pr_replacement(
    *,
    project_id: str,
    closed_head: str,
    closed_base: str,
    github_token: str,
    github_owner: str,
    github_repo: str,
) -> dict:
    """Perform the replacement GitHub lookup without any ORM row lock."""

    from app.github import get_open_pull_request_for_head_branch

    try:
        replacement_pr = await get_open_pull_request_for_head_branch(
            github_token,
            github_owner,
            github_repo,
            closed_head,
            closed_base,
            project_id=project_id,
        )
    except HTTPException as exc:
        return {"status": "error", "detail": str(exc.detail)}
    except Exception as exc:  # noqa: BLE001 - retry receipt is fail-closed
        return {
            "status": "error",
            "detail": f"{type(exc).__name__}: {exc}",
        }
    return {"status": "ok", "replacement_pr": replacement_pr}


async def reconcile_closed_pr_replacement(
    *,
    task: Task,
    session: AsyncSession,
    review_loop: dict,
    pr_number: int,
    pr_data: dict,
    replacement_lookup: dict | None,
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

    if replacement_lookup is None:
        return False
    if replacement_lookup.get("status") == "error":
        review_loop["polls_since_pr_fetch"] = 999
        _stamp_replacement_retry_backoff(review_loop)
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
            replacement_lookup.get("detail") or "unknown GitHub error",
        )
        return True

    replacement_pr = replacement_lookup.get("replacement_pr")

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
            _stamp_replacement_retry_backoff(review_loop)
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
    release_owned_review_wait_hold = _owns_superseded_review_wait_approval_hold(
        task,
        review_loop,
        pr_number=pr_number,
        pr_data=pr_data,
    )
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
    from app.admin_approval_authority import APPROVAL_FOLLOWUP_CLAIM_KEY

    # Any operator follow-up receipt is bound to the superseded PR/base/head.
    # Revoke it, but never infer that the human approval hold itself is safe to
    # clear; only the exact backstop-owned marker below may release the flag.
    rebound_context.pop(APPROVAL_FOLLOWUP_CLAIM_KEY, None)
    if release_owned_review_wait_hold:
        task.requires_approval = False
        rebound_context.pop("_approval_set_at", None)
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
