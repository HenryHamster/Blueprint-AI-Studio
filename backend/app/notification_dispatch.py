"""
notification_dispatch.py — helpers to create system ProjectMessages for significant events.

Each function creates a ProjectMessage and adds it to the session but does NOT commit.
Callers own the transaction.

Also provides dispatch_external_notification() for fire-and-forget external webhook delivery.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import MessageType, ProjectMessage, _utcnow

logger = logging.getLogger(__name__)

# Module-level throttle tracking: event_type -> last dispatch monotonic time (seconds)
_last_dispatch_time: dict[str, float] = {}


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
    """
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

        # Per-event throttle: drop if last dispatch was too recent
        now = time.monotonic()
        last = _last_dispatch_time.get(event_type, 0.0)
        if throttle_seconds > 0 and (now - last) < throttle_seconds:
            return

        _last_dispatch_time[event_type] = now

        body = {
            "event_type": event_type,
            "project_id": str(project_id),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "payload": payload,
        }
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(webhook_url, json=body)
            if resp.status_code >= 400:
                logger.warning(
                    f"[NOTIFY] External webhook {event_type} returned {resp.status_code}"
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
) -> ProjectMessage:
    """Post a message for direction lifecycle milestones (auto-completed, auto-paused, auto-retired)."""
    summary = f"Direction {milestone}: {direction_text[:80]} — {reason}"
    return await post_event_message(
        session,
        project_id,
        event_type="direction_milestone",
        summary=summary,
        context={"milestone": milestone, "direction_text": direction_text[:120], "reason": reason},
        task_id=task_id,
    )


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


async def post_auto_fix_result_message(
    session: AsyncSession,
    project_id: uuid.UUID,
    branch: str,
    result: str,  # 'merged', 'failed', 'review_needed'
    pr_number: Optional[int] = None,
    task_id: Optional[uuid.UUID] = None,
) -> ProjectMessage:
    """Post a message when a CI auto-fix task completes."""
    if result == 'merged':
        summary = f'CI auto-fix merged: PR #{pr_number} fixed {branch}'
    elif result == 'failed':
        summary = f'CI auto-fix failed on {branch} — manual intervention needed'
    else:
        summary = f'CI auto-fix on {branch} needs review: PR #{pr_number}'
    return await post_event_message(
        session,
        project_id,
        event_type='auto_fix_result',
        summary=summary,
        context={'branch': branch, 'result': result, 'pr_number': pr_number},
        task_id=task_id,
    )


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
