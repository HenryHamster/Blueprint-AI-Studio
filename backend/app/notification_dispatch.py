"""
notification_dispatch.py — helpers to create system ProjectMessages for significant events.

Each function creates a ProjectMessage and adds it to the session but does NOT commit.
Callers own the transaction.

Also provides dispatch_external_notification() for fire-and-forget external webhook delivery.
"""
from __future__ import annotations

import asyncio
import collections
import dataclasses
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import MessageType, Project, ProjectMessage, Task, _utcnow

logger = logging.getLogger(__name__)

# Module-level throttle tracking: event_type -> last dispatch monotonic time (seconds)
_last_dispatch_time: dict[str, float] = {}

# Delivery stats — module-level counters
total_dispatched: int = 0
total_delivered: int = 0
total_failed: int = 0
last_failure_reason: str = ""

# HTTP status codes that indicate a permanent failure — don't retry these
_PERMANENT_FAILURE_CODES: frozenset[int] = frozenset({401, 403, 404})

# Retry delays in seconds: attempt 1 → 30s, attempt 2 → 120s, attempt 3 → 600s
_RETRY_DELAYS: tuple[int, ...] = (30, 120, 600)


@dataclasses.dataclass
class _RetryEntry:
    project_id: Any
    event_type: str
    payload: dict
    body: dict  # pre-built JSON body
    attempts: int  # number of attempts already made
    next_retry_at: float  # monotonic time


# Bounded in-memory retry queue (max 50 entries; oldest are dropped when full)
_retry_queue: collections.deque[_RetryEntry] = collections.deque(maxlen=50)


def get_notification_stats() -> dict:
    """Return current delivery stats and retry queue depth."""
    return {
        "total_dispatched": total_dispatched,
        "total_delivered": total_delivered,
        "total_failed": total_failed,
        "last_failure_reason": last_failure_reason,
        "retry_queue_depth": len(_retry_queue),
    }


async def _attempt_delivery(webhook_url: str, body: dict) -> int:
    """POST body to webhook_url. Returns HTTP status code, or 0 on network/timeout error."""
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.post(webhook_url, json=body)
        return resp.status_code


async def _drain_retry_queue(webhook_url: str) -> None:
    """Process due retry entries, piggybacking on the next dispatch call."""
    global total_delivered, total_failed, last_failure_reason

    now = time.monotonic()
    # Collect due entries (leave non-due entries in place)
    due: list[_RetryEntry] = []
    not_due: list[_RetryEntry] = []
    for entry in _retry_queue:
        if entry.next_retry_at <= now:
            due.append(entry)
        else:
            not_due.append(entry)

    if not due:
        return

    _retry_queue.clear()
    for entry in not_due:
        _retry_queue.append(entry)

    for entry in due:
        try:
            status = await _attempt_delivery(webhook_url, entry.body)
            if 200 <= status < 300:
                total_delivered += 1
                logger.info(
                    f"[NOTIFY] Retry succeeded for {entry.event_type} "
                    f"(attempt {entry.attempts + 1})"
                )
            elif status in _PERMANENT_FAILURE_CODES:
                total_failed += 1
                last_failure_reason = f"permanent {status}"
                logger.warning(
                    f"[NOTIFY] Retry permanently failed for {entry.event_type}: {status}"
                )
            elif entry.attempts + 1 < len(_RETRY_DELAYS):
                # Schedule another retry
                delay = _RETRY_DELAYS[entry.attempts + 1]
                _retry_queue.append(dataclasses.replace(
                    entry,
                    attempts=entry.attempts + 1,
                    next_retry_at=time.monotonic() + delay,
                ))
                logger.warning(
                    f"[NOTIFY] Retry {entry.attempts + 1} failed for {entry.event_type} "
                    f"({status}), next retry in {delay}s"
                )
            else:
                total_failed += 1
                last_failure_reason = f"max retries exceeded (last status: {status})"
                logger.warning(
                    f"[NOTIFY] Exhausted retries for {entry.event_type} after "
                    f"{entry.attempts + 1} attempts"
                )
        except Exception as exc:
            if entry.attempts + 1 < len(_RETRY_DELAYS):
                delay = _RETRY_DELAYS[entry.attempts + 1]
                _retry_queue.append(dataclasses.replace(
                    entry,
                    attempts=entry.attempts + 1,
                    next_retry_at=time.monotonic() + delay,
                ))
                logger.warning(
                    f"[NOTIFY] Retry {entry.attempts + 1} network error for "
                    f"{entry.event_type}: {exc}"
                )
            else:
                total_failed += 1
                last_failure_reason = str(exc)
                logger.warning(
                    f"[NOTIFY] Exhausted retries for {entry.event_type}: {exc}"
                )


async def dispatch_external_notification(
    project_id: Any,
    event_type: str,
    payload: dict,
    session: Optional[AsyncSession] = None,  # kept for call-site uniformity; not used
) -> None:
    """Fire-and-forget external webhook dispatch.

    Reads notification_config from runtime_config and POSTs a structured JSON
    body to the configured webhook_url when event_type is in enabled_events.

    Never raises — callers must not fail because notification dispatch failed.
    Use asyncio.ensure_future() / asyncio.create_task() at call sites to make
    this truly non-blocking.

    On 5xx or network/timeout failures, the notification is enqueued for retry
    with exponential backoff (30s, 2min, 10min — 3 attempts max). 4xx permanent
    errors (401, 403, 404) are not retried. The retry queue is bounded at 50
    entries to cap memory use; oldest entries are dropped when full.
    """
    global total_dispatched, total_delivered, total_failed, last_failure_reason

    try:
        from app.shared_state import runtime_config  # noqa: PLC0415
        nc = runtime_config.get("notification_config", {})
        webhook_url = nc.get("webhook_url", "")
        enabled_events = nc.get("enabled_events", [])
        throttle_seconds = float(nc.get("throttle_seconds", 30))

        if not webhook_url:
            return
        if event_type not in enabled_events:
            return
        # SSRF guard: only allow http(s) and block internal/metadata IPs
        from urllib.parse import urlparse  # noqa: PLC0415
        parsed_url = urlparse(webhook_url)
        if parsed_url.scheme not in ("http", "https"):
            logger.warning(f"[NOTIFY] Rejecting non-http(s) webhook URL: {parsed_url.scheme}")
            return
        _host = parsed_url.hostname or ""
        if _host in ("169.254.169.254", "metadata.google.internal", "localhost", "127.0.0.1"):
            logger.warning(f"[NOTIFY] Rejecting internal webhook URL host: {_host}")
            return

        # Per-event throttle: drop if last dispatch was too recent
        now = time.monotonic()
        last = _last_dispatch_time.get(event_type, 0.0)
        if throttle_seconds > 0 and (now - last) < throttle_seconds:
            return

        _last_dispatch_time[event_type] = now

        # Drain any due retries before making the new request
        await _drain_retry_queue(webhook_url)

        body = {
            "event_type": event_type,
            "project_id": str(project_id),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "payload": payload,
        }

        total_dispatched += 1

        try:
            status = await _attempt_delivery(webhook_url, body)
            if 200 <= status < 300:
                total_delivered += 1
            elif status in _PERMANENT_FAILURE_CODES:
                total_failed += 1
                last_failure_reason = f"permanent {status}"
                logger.warning(
                    f"[NOTIFY] External webhook {event_type} returned {status} (permanent)"
                )
            elif status >= 400:
                # 5xx or unexpected 4xx — schedule first retry
                delay = _RETRY_DELAYS[0]
                _retry_queue.append(_RetryEntry(
                    project_id=project_id,
                    event_type=event_type,
                    payload=payload,
                    body=body,
                    attempts=0,
                    next_retry_at=time.monotonic() + delay,
                ))
                logger.warning(
                    f"[NOTIFY] External webhook {event_type} returned {status}, "
                    f"scheduled retry in {delay}s"
                )
            else:
                # Unexpected status in 1xx/3xx range — treat as delivered
                total_delivered += 1
        except Exception as exc:
            # Network/timeout error — schedule first retry
            delay = _RETRY_DELAYS[0]
            _retry_queue.append(_RetryEntry(
                project_id=project_id,
                event_type=event_type,
                payload=payload,
                body=body,
                attempts=0,
                next_retry_at=time.monotonic() + delay,
            ))
            logger.warning(
                f"[NOTIFY] External webhook dispatch failed for {event_type}: {exc}, "
                f"scheduled retry in {delay}s"
            )
    except Exception as e:
        logger.warning(f"[NOTIFY] External webhook dispatch failed for {event_type}: {e}")


async def post_event_message(
    session: AsyncSession,
    project_id: uuid.UUID,
    event_type: str,
    summary: str,
    context: Optional[dict[str, Any]] = None,
    task_id: Optional[uuid.UUID] = None,
) -> ProjectMessage:
    """Core helper — creates a system ProjectMessage with the given event_type in context."""
    ctx: dict[str, Any] = {"event_type": event_type}
    if context:
        ctx.update(context)
    msg = ProjectMessage(
        project_id=project_id,
        message_type=MessageType.NOTE,
        from_type="system",
        from_task_id=task_id,
        content=summary,
        context=ctx,
    )
    session.add(msg)
    return msg


async def post_pr_merged_message(
    session: AsyncSession,
    project_id: uuid.UUID,
    pr_number: int,
    direction_text: str,
    task_count: int,
    task_id: Optional[uuid.UUID] = None,
) -> ProjectMessage:
    """Post a message when a rollup PR is auto-merged."""
    summary = (
        f"PR #{pr_number} merged — {task_count} subtask(s) shipped for direction: {direction_text}"
    )
    return await post_event_message(
        session,
        project_id,
        event_type="pr_merged",
        summary=summary,
        context={"pr_number": pr_number, "task_count": task_count, "direction_text": direction_text[:120]},
        task_id=task_id,
    )


async def post_direction_milestone_message(
    session: AsyncSession,
    project_id: uuid.UUID,
    direction_text: str,
    milestone: str,
    reason: str,
    task_id: Optional[uuid.UUID] = None,
    direction_id: Optional[str] = None,
    evidence: Optional[dict[str, Any]] = None,
) -> ProjectMessage:
    """Post a message for direction lifecycle milestones (auto-completed, auto-paused, auto-retired)."""
    reason_l = reason.lower()
    reason_code: Optional[str] = None
    if milestone == "auto-paused":
        if "budget" in reason_l and ("exceed" in reason_l or "over" in reason_l):
            reason_code = "budget_exceeded"
        elif "cost-per-merge" in reason_l or "cost per merge" in reason_l:
            reason_code = "cost_per_merge_high"
        elif "low yield" in reason_l or "yield" in reason_l:
            reason_code = "low_yield"

    context: dict[str, Any] = {
        "milestone": milestone,
        "direction_text": direction_text[:120],
        "reason": reason,
    }
    payload: dict[str, Any] = {
        "direction_text": direction_text[:120],
        "milestone": milestone,
        "reason": reason,
        "direction_id": direction_id,
    }
    if reason_code:
        context["reason_code"] = reason_code
        payload["reason_code"] = reason_code
    if evidence:
        context["evidence"] = evidence
        payload["evidence"] = evidence

    summary = f"Direction {milestone}: {direction_text[:80]} — {reason}"
    msg = await post_event_message(
        session,
        project_id,
        event_type="direction_milestone",
        summary=summary,
        context=context,
        task_id=task_id,
    )
    asyncio.ensure_future(dispatch_external_notification(
        project_id,
        "DIRECTION_MILESTONE",
        payload,
    ))
    return msg


async def post_cycle_summary_message(
    session: AsyncSession,
    project_id: uuid.UUID,
    vision_rev: Any,
    dirs_created: int,
    dirs_retired: int,
    ops_applied: int,
    msgs_created: int,
    task_id: Optional[uuid.UUID] = None,
) -> ProjectMessage:
    """Post a message when a strategic cycle completes."""
    summary = (
        f"Strategic cycle complete — vision rev {vision_rev}, "
        f"{dirs_created} directions created, {dirs_retired} retired"
    )
    return await post_event_message(
        session,
        project_id,
        event_type="cycle_summary",
        summary=summary,
        context={
            "vision_rev": vision_rev,
            "dirs_created": dirs_created,
            "dirs_retired": dirs_retired,
            "ops_applied": ops_applied,
            "msgs_created": msgs_created,
        },
        task_id=task_id,
    )


async def post_failure_alert_message(
    session: AsyncSession,
    project_id: uuid.UUID,
    description: str,
    task_id: Optional[uuid.UUID] = None,
) -> ProjectMessage:
    """Post a message for failure pattern alerts (e.g. circuit breaker opened)."""
    summary = f"Failure alert: {description}"
    return await post_event_message(
        session,
        project_id,
        event_type="failure_alert",
        summary=summary,
        context={"description": description},
        task_id=task_id,
    )


async def post_auto_fix_spawned_message(
    session: AsyncSession,
    project_id: uuid.UUID,
    branch: str,
    failed_checks: list[str],
    task_id: Optional[uuid.UUID] = None,
) -> ProjectMessage:
    """Post a message when CI auto-remediation spawns a fix task."""
    check_names = ', '.join(failed_checks[:3])
    suffix = f' +{len(failed_checks) - 3} more' if len(failed_checks) > 3 else ''
    summary = f'CI auto-fix spawned: {check_names}{suffix} failed on {branch}'
    return await post_event_message(
        session,
        project_id,
        event_type='auto_fix_spawned',
        summary=summary,
        context={'branch': branch, 'failed_checks': failed_checks[:10], 'task_id': str(task_id) if task_id else None},
        task_id=task_id,
    )


_AUTO_REMEDIATION_ESCALATION_THRESHOLD = 3
_AUTO_REMEDIATION_REGRESSION_ESCALATION_THRESHOLD = 1


async def post_auto_fix_result_message(
    session: AsyncSession,
    project_id: uuid.UUID,
    branch: str,
    result: str,  # 'merged', 'failed', 'review_needed'
    pr_number: Optional[int] = None,
    task_id: Optional[uuid.UUID] = None,
) -> ProjectMessage:
    """Post a message when a CI auto-fix task completes.

    For result='failed': increments the consecutive failure counter for this
    branch in the project's auto_remediation config.  When the counter reaches
    _AUTO_REMEDIATION_ESCALATION_THRESHOLD (3), the branch is suspended and an
    escalation message + webhook are fired.

    For result='merged': resets the counter and clears any suspension for this
    branch.
    """
    if result == 'merged':
        summary = f'CI auto-fix merged: PR #{pr_number} fixed {branch}'
    elif result == 'failed':
        summary = f'CI auto-fix failed on {branch} — manual intervention needed'
    else:
        summary = f'CI auto-fix on {branch} needs review: PR #{pr_number}'

    msg = await post_event_message(
        session,
        project_id,
        event_type='auto_fix_result',
        summary=summary,
        context={'branch': branch, 'result': result, 'pr_number': pr_number},
        task_id=task_id,
    )

    # Update per-branch failure tracking in the project's auto_remediation config
    if result in ('failed', 'merged'):
        try:
            from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415
            project = await session.get(Project, project_id)
            if project is not None:
                design_doc = dict(project.design_doc or {})
                ar_config = dict(design_doc.get('_auto_remediation', {}))
                failure_counts: dict[str, int] = dict(ar_config.get('branch_failure_counts', {}))
                suspended: list[str] = list(ar_config.get('suspended_branches', []))
                suspension_meta: dict[str, dict] = dict(ar_config.get('suspension_metadata', {}))

                if result == 'merged':
                    # Reset on success, but preserve recovery_attempts so that
                    # a branch which keeps failing-and-fixing eventually escalates
                    # to permanent suspension via exponential backoff.
                    failure_counts.pop(branch, None)
                    if branch in suspended:
                        suspended.remove(branch)
                    prior = suspension_meta.get(branch) or {}
                    prior_attempts = int(prior.get('recovery_attempts', 0) or 0)
                    if prior_attempts > 0:
                        suspension_meta[branch] = {'recovery_attempts': prior_attempts}
                    else:
                        suspension_meta.pop(branch, None)
                else:
                    # Increment consecutive failure counter
                    failure_counts[branch] = failure_counts.get(branch, 0) + 1
                    count = failure_counts[branch]
                    # If the triggering task was flagged as a regression of a
                    # recently-merged fix, suspend earlier — a straightforward
                    # fix has already been tried and didn't hold.
                    threshold = _AUTO_REMEDIATION_ESCALATION_THRESHOLD
                    if task_id is not None:
                        triggering = await session.get(Task, task_id)
                        if triggering is not None and (triggering.context or {}).get("regression_of_task_id"):
                            threshold = _AUTO_REMEDIATION_REGRESSION_ESCALATION_THRESHOLD
                    if count >= threshold and branch not in suspended:
                        suspended.append(branch)
                        # Preserve recovery_attempts across re-suspensions so
                        # exponential backoff applies on repeated failures.
                        prior = suspension_meta.get(branch) or {}
                        suspension_meta[branch] = {
                            'suspended_at': _utcnow().isoformat(),
                            'recovery_attempts': int(prior.get('recovery_attempts', 0) or 0),
                        }
                        ar_config['branch_failure_counts'] = failure_counts
                        ar_config['suspended_branches'] = suspended
                        ar_config['suspension_metadata'] = suspension_meta
                        design_doc['_auto_remediation'] = ar_config
                        project.design_doc = design_doc
                        flag_modified(project, 'design_doc')
                        session.add(project)
                        # Fire escalation message + webhook
                        await post_auto_fix_escalation_message(
                            session, project_id, branch=branch, failure_count=count,
                        )
                        asyncio.ensure_future(dispatch_external_notification(
                            project_id,
                            'AUTO_FIX_ESCALATED',
                            {
                                'branch': branch,
                                'consecutive_failures': count,
                                'threshold': threshold,
                                'suspended_at': suspension_meta[branch]['suspended_at'],
                                'recovery_attempts': suspension_meta[branch]['recovery_attempts'],
                                'severity': 'blocker',
                                'manual_intervention_required': True,
                                'summary': summary,
                            },
                        ))
                        return msg

                ar_config['branch_failure_counts'] = failure_counts
                ar_config['suspended_branches'] = suspended
                ar_config['suspension_metadata'] = suspension_meta
                design_doc['_auto_remediation'] = ar_config
                project.design_doc = design_doc
                flag_modified(project, 'design_doc')
                session.add(project)
        except Exception as exc:
            logger.warning('[AUTO-REMEDIATION] Failed to update failure tracking for %s: %s', branch, exc)

    return msg


async def post_auto_fix_escalation_message(
    session: AsyncSession,
    project_id: uuid.UUID,
    branch: str,
    failure_count: int,
    task_id: Optional[uuid.UUID] = None,
) -> ProjectMessage:
    """Post a BLOCKER-severity escalation message when auto-remediation is suspended.

    Called after _AUTO_REMEDIATION_ESCALATION_THRESHOLD consecutive failures for
    the same branch.  Documents why auto-remediation was suspended so operators
    know manual action is required.
    """
    summary = (
        f'Auto-remediation suspended for {branch} after {failure_count} consecutive failures. '
        f'Manual intervention required. Re-enable via the auto-remediation settings.'
    )
    ctx: dict[str, Any] = {
        'event_type': 'auto_fix_escalated',
        'branch': branch,
        'consecutive_failures': failure_count,
    }
    msg = ProjectMessage(
        project_id=project_id,
        message_type=MessageType.BLOCKER,
        from_type='system',
        from_task_id=task_id,
        content=summary,
        context=ctx,
    )
    session.add(msg)
    return msg


async def post_remediation_escalation_message(
    session: AsyncSession,
    project_id: uuid.UUID,
    direction_text: str,
    failure_pattern: str,
    attempts: int,
    remediation_summary: str,
    task_id: Optional[uuid.UUID] = None,
) -> ProjectMessage:
    """Post a BLOCKER-severity message when remediation has repeatedly failed for a direction.

    Fired after attempts >= 3 with the same error fingerprint still recurring. Callers should
    also set needs_manual_intervention on the direction so the diagnostics API surfaces it.
    """
    summary = (
        f"Remediation escalation: direction '{direction_text[:80]}' still failing after "
        f"{attempts} remediation attempts. Manual intervention required."
    )
    ctx: dict[str, Any] = {
        "event_type": "remediation_escalated",
        "direction_text": direction_text[:120],
        "failure_pattern": failure_pattern[:200],
        "attempts": attempts,
        "remediation_summary": remediation_summary[:240],
    }
    msg = ProjectMessage(
        project_id=project_id,
        message_type=MessageType.BLOCKER,
        from_type="system",
        from_task_id=task_id,
        content=summary,
        context=ctx,
    )
    session.add(msg)
    asyncio.ensure_future(dispatch_external_notification(
        project_id,
        "REMEDIATION_ESCALATED",
        {
            "direction_text": direction_text[:120],
            "failure_pattern": failure_pattern[:200],
            "attempts": attempts,
            "remediation_summary": remediation_summary[:240],
            "severity": "blocker",
            "manual_intervention_required": True,
            "summary": summary,
        },
    ))
    return msg


async def post_issue_implementation_spawned_message(
    session: AsyncSession,
    project_id: uuid.UUID,
    issue_number: int,
    issue_title: str,
    task_id: Optional[uuid.UUID] = None,
) -> ProjectMessage:
    """Post a message when an issue-to-implementation task is spawned."""
    summary = f"Blueprint is implementing issue #{issue_number}: {issue_title}"
    return await post_event_message(
        session,
        project_id,
        event_type="issue_implementation_spawned",
        summary=summary,
        context={
            "issue_number": issue_number,
            "issue_title": issue_title,
            "task_id": str(task_id) if task_id else None,
        },
        task_id=task_id,
    )


async def post_review_feedback_message(
    session: AsyncSession,
    project_id: uuid.UUID,
    pr_number: int,
    signal: str,  # 'review_dismissed', 'user_overrode_review', 'user_caught_issues'
    detail: Optional[str] = None,
    task_id: Optional[uuid.UUID] = None,
) -> ProjectMessage:
    """Post a message when a review quality signal is received from GitHub."""
    if signal == "review_dismissed":
        summary = f"Blueprint review dismissed on PR #{pr_number}" + (f": {detail}" if detail else "")
    elif signal == "user_overrode_review":
        summary = f"User approved PR #{pr_number} after Blueprint requested changes"
    elif signal == "user_caught_issues":
        summary = f"User requested changes on PR #{pr_number} that Blueprint had approved"
    else:
        summary = f"Review feedback on PR #{pr_number}: {signal}"
    return await post_event_message(
        session,
        project_id,
        event_type="review_feedback",
        summary=summary,
        context={"pr_number": pr_number, "signal": signal, "detail": detail},
        task_id=task_id,
    )


async def post_rollback_recommendation_message(
    session: AsyncSession,
    project_id: uuid.UUID,
    reason: str,
) -> ProjectMessage:
    """Post a message when a rollback is recommended after deploy monitoring."""
    summary = f"Rollback recommended: {reason}"
    return await post_event_message(
        session,
        project_id,
        event_type="rollback_recommended",
        summary=summary,
        context={"reason": reason},
    )


async def post_feedback_acknowledgment(
    session: AsyncSession,
    project_id: uuid.UUID,
    original_msg_id: uuid.UUID,
    resolution: str,
    task_id: Optional[uuid.UUID] = None,
) -> ProjectMessage:
    """Create a reply message acknowledging a resolved FEEDBACK message."""
    summary = f"Feedback addressed: {resolution}"
    return await post_event_message(
        session,
        project_id,
        event_type="feedback_acknowledgment",
        summary=summary,
        context={"reply_to": str(original_msg_id), "resolution": resolution},
        task_id=task_id,
    )
