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

import urllib.parse

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.url_security import _resolve_safe_external_url

from app.models import (
    MessageType,
    Project,
    ProjectMessage,
    ResidualCaseStatus,
    ResidualRemediationCase,
    Task,
    _utcnow,
)

logger = logging.getLogger(__name__)

# Module-level throttle tracking: event_type -> last dispatch monotonic time (seconds)
_last_dispatch_time: dict[str, float] = {}

# Delivery stats — module-level counters
total_dispatched: int = 0
total_delivered: int = 0
total_failed: int = 0
last_failure_reason: str = ""

# Per-project delivery stats: str(project_id) -> {dispatched, delivered, failed, last_failure_reason, last_failure_at}
_per_project_stats: dict[str, dict] = {}

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


def _record_project_stat(project_id: Any, outcome: str, reason: str = "") -> None:
    """Update per-project delivery counters. outcome: 'dispatched'|'delivered'|'failed'."""
    pid = str(project_id)
    if pid not in _per_project_stats:
        _per_project_stats[pid] = {
            "dispatched": 0,
            "delivered": 0,
            "failed": 0,
            "last_failure_reason": "",
            "last_failure_at": None,
        }
    ps = _per_project_stats[pid]
    ps[outcome] = ps.get(outcome, 0) + 1
    if outcome == "failed":
        ps["last_failure_reason"] = reason
        ps["last_failure_at"] = datetime.now(timezone.utc).isoformat()


def get_notification_stats(project_id: Any = None) -> dict:
    """Return current delivery stats and retry queue depth.

    If project_id is provided, returns stats scoped to that project.
    """
    if project_id is not None:
        pid = str(project_id)
        ps = _per_project_stats.get(pid, {})
        queue_depth = sum(1 for e in _retry_queue if str(e.project_id) == pid)
        return {
            "total_dispatched": ps.get("dispatched", 0),
            "total_delivered": ps.get("delivered", 0),
            "total_failed": ps.get("failed", 0),
            "last_failure_reason": ps.get("last_failure_reason", ""),
            "last_failure_at": ps.get("last_failure_at"),
            "retry_queue_depth": queue_depth,
        }
    return {
        "total_dispatched": total_dispatched,
        "total_delivered": total_delivered,
        "total_failed": total_failed,
        "last_failure_reason": last_failure_reason,
        "retry_queue_depth": len(_retry_queue),
    }


async def _attempt_delivery(webhook_url: str, body: dict) -> int:
    """POST body to webhook_url. Returns HTTP status code, or 0 on network/timeout error.

    Defeats DNS rebinding by resolving the hostname ourselves, validating the IP
    is public, and then pinning the connection to that exact IP — substituting
    the resolved address into the URL while preserving the original Host header
    and TLS SNI hostname so cert validation still works against the hostname.
    httpx's separate getaddrinfo call cannot be redirected to a private IP by a
    rebinding attacker because the connection target is now a literal address.
    """
    safe, reason, resolved_ip = await asyncio.to_thread(
        _resolve_safe_external_url, webhook_url
    )
    if not safe or resolved_ip is None:
        logger.warning(
            "[NOTIFY] SSRF check blocked dispatch to %r: %s", webhook_url, reason
        )
        return 0

    parsed = urllib.parse.urlparse(webhook_url)
    original_host = parsed.hostname or ""
    # IPv6 literals need bracketing in URL netloc
    ip_in_netloc = f"[{resolved_ip}]" if ":" in resolved_ip else resolved_ip
    netloc = f"{ip_in_netloc}:{parsed.port}" if parsed.port else ip_in_netloc
    pinned_url = urllib.parse.urlunparse(parsed._replace(netloc=netloc))

    # Preserve original Host header (for HTTP virtual hosting) and TLS SNI
    # (for cert validation against the hostname, not the IP).
    request_host_header = (
        f"{original_host}:{parsed.port}" if parsed.port else original_host
    )
    headers = {"Host": request_host_header}
    extensions = {"sni_hostname": original_host}

    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.post(
            pinned_url, json=body, headers=headers, extensions=extensions
        )
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
                _record_project_stat(entry.project_id, "delivered")
                logger.info(
                    f"[NOTIFY] Retry succeeded for {entry.event_type} "
                    f"(attempt {entry.attempts + 1})"
                )
            elif status in _PERMANENT_FAILURE_CODES:
                total_failed += 1
                last_failure_reason = f"permanent {status}"
                _record_project_stat(entry.project_id, "failed", f"permanent {status}")
                logger.warning(
                    f"[NOTIFY] Retry permanently failed for {entry.event_type}: {status}"
                )
            elif entry.attempts + 1 < len(_RETRY_DELAYS):
                # Schedule another retry
                delay = _RETRY_DELAYS[entry.attempts + 1]
                _retry_queue.append(
                    dataclasses.replace(
                        entry,
                        attempts=entry.attempts + 1,
                        next_retry_at=time.monotonic() + delay,
                    )
                )
                logger.warning(
                    f"[NOTIFY] Retry {entry.attempts + 1} failed for {entry.event_type} "
                    f"({status}), next retry in {delay}s"
                )
            else:
                total_failed += 1
                last_failure_reason = f"max retries exceeded (last status: {status})"
                _record_project_stat(entry.project_id, "failed", last_failure_reason)
                logger.warning(
                    f"[NOTIFY] Exhausted retries for {entry.event_type} after "
                    f"{entry.attempts + 1} attempts"
                )
        except Exception as exc:
            if entry.attempts + 1 < len(_RETRY_DELAYS):
                delay = _RETRY_DELAYS[entry.attempts + 1]
                _retry_queue.append(
                    dataclasses.replace(
                        entry,
                        attempts=entry.attempts + 1,
                        next_retry_at=time.monotonic() + delay,
                    )
                )
                logger.warning(
                    f"[NOTIFY] Retry {entry.attempts + 1} network error for "
                    f"{entry.event_type}: {exc}"
                )
            else:
                total_failed += 1
                last_failure_reason = str(exc)
                _record_project_stat(entry.project_id, "failed", str(exc))
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
        from app.shared_state import runtime_config

        nc = runtime_config.get("notification_config", {})
        webhook_url = nc.get("webhook_url", "")
        enabled_events = nc.get("enabled_events", [])
        throttle_seconds = float(nc.get("throttle_seconds", 30))

        if not webhook_url:
            return
        if event_type not in enabled_events:
            return
        # SSRF guard: only allow http(s) and block internal/metadata IPs
        from urllib.parse import urlparse

        parsed_url = urlparse(webhook_url)
        if parsed_url.scheme not in ("http", "https"):
            logger.warning(
                f"[NOTIFY] Rejecting non-http(s) webhook URL: {parsed_url.scheme}"
            )
            return
        _host = parsed_url.hostname or ""
        if _host in (
            "169.254.169.254",
            "metadata.google.internal",
            "localhost",
            "127.0.0.1",
        ):
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
        _record_project_stat(project_id, "dispatched")

        try:
            status = await _attempt_delivery(webhook_url, body)
            if 200 <= status < 300:
                total_delivered += 1
                _record_project_stat(project_id, "delivered")
            elif status in _PERMANENT_FAILURE_CODES:
                total_failed += 1
                last_failure_reason = f"permanent {status}"
                _record_project_stat(project_id, "failed", f"permanent {status}")
                logger.warning(
                    f"[NOTIFY] External webhook {event_type} returned {status} (permanent)"
                )
            elif status >= 400:
                # 5xx or unexpected 4xx — schedule first retry
                delay = _RETRY_DELAYS[0]
                _retry_queue.append(
                    _RetryEntry(
                        project_id=project_id,
                        event_type=event_type,
                        payload=payload,
                        body=body,
                        attempts=0,
                        next_retry_at=time.monotonic() + delay,
                    )
                )
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
            _retry_queue.append(
                _RetryEntry(
                    project_id=project_id,
                    event_type=event_type,
                    payload=payload,
                    body=body,
                    attempts=0,
                    next_retry_at=time.monotonic() + delay,
                )
            )
            logger.warning(
                f"[NOTIFY] External webhook dispatch failed for {event_type}: {exc}, "
                f"scheduled retry in {delay}s"
            )
    except Exception as e:
        logger.warning(
            f"[NOTIFY] External webhook dispatch failed for {event_type}: {e}"
        )


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
    summary = f"PR #{pr_number} merged — {task_count} subtask(s) shipped for direction: {direction_text}"
    return await post_event_message(
        session,
        project_id,
        event_type="pr_merged",
        summary=summary,
        context={
            "pr_number": pr_number,
            "task_count": task_count,
            "direction_text": direction_text[:120],
        },
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
    asyncio.ensure_future(
        dispatch_external_notification(
            project_id,
            "DIRECTION_MILESTONE",
            payload,
        )
    )
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


async def post_account_model_regression_message(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    account_id: str,
    account_label: str,
    provider: str,
    model: str,
    account_rate: float,
    project_median: float,
    sample_size: int,
    distinct_accounts: int,
    started_at: str,
) -> ProjectMessage:
    """Post a message when a (provider, model) combination is auto-quarantined
    on an account due to a success-rate regression vs the project median.

    Mirrors the post_failure_alert_message shape — same NOTE/system surface
    operators already watch for circuit-breaker events. The richer numeric
    detail (account_rate, project_median, sample_size) is preserved on
    ``context`` so the UI can surface a structured drill-in alongside the
    plain-text summary.
    """
    summary = (
        f"Account model regression-quarantined: {account_label} {provider}/{model} "
        f"({account_rate:.0%} success vs project median {project_median:.0%} on n={sample_size})"
    )
    return await post_event_message(
        session,
        project_id,
        event_type="account_model_regression_quarantined",
        summary=summary,
        context={
            "account_id": account_id,
            "account_label": account_label,
            "provider": provider,
            "model": model,
            "account_rate": account_rate,
            "project_median": project_median,
            "sample_size": sample_size,
            "distinct_accounts": distinct_accounts,
            "started_at": started_at,
        },
    )


async def post_account_model_regression_recovered_message(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    account_id: str,
    account_label: str,
    provider: str,
    model: str,
    account_rate: float | None = None,
    project_median: float | None = None,
    sample_size: int | None = None,
) -> ProjectMessage:
    """Post a message when a regression-quarantined (provider, model) auto-recovers
    after a successful probe task completes post-cooldown."""
    rate_detail = (
        f" ({account_rate:.0%} vs median {project_median:.0%} on n={sample_size})"
        if account_rate is not None
        and project_median is not None
        and sample_size is not None
        else ""
    )
    summary = (
        f"Account model regression-recovered: {account_label} {provider}/{model} "
        f"restored after probe success{rate_detail}"
    )
    return await post_event_message(
        session,
        project_id,
        event_type="account_model_regression_recovered",
        summary=summary,
        context={
            "account_id": account_id,
            "account_label": account_label,
            "provider": provider,
            "model": model,
            "account_rate": account_rate,
            "project_median": project_median,
            "sample_size": sample_size,
        },
    )


async def post_auto_fix_spawned_message(
    session: AsyncSession,
    project_id: uuid.UUID,
    branch: str,
    failed_checks: list[str],
    task_id: Optional[uuid.UUID] = None,
) -> ProjectMessage:
    """Post a message when CI auto-remediation spawns a fix task."""
    check_names = ", ".join(failed_checks[:3])
    suffix = f" +{len(failed_checks) - 3} more" if len(failed_checks) > 3 else ""
    summary = f"CI auto-fix spawned: {check_names}{suffix} failed on {branch}"
    return await post_event_message(
        session,
        project_id,
        event_type="auto_fix_spawned",
        summary=summary,
        context={
            "branch": branch,
            "failed_checks": failed_checks[:10],
            "task_id": str(task_id) if task_id else None,
        },
        task_id=task_id,
    )


_AUTO_REMEDIATION_ESCALATION_THRESHOLD = 3
_AUTO_REMEDIATION_REGRESSION_ESCALATION_THRESHOLD = 1
_RESIDUAL_AUDIT_DEFER_ESCALATION_THRESHOLD = 5


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
    if result == "merged":
        summary = f"CI auto-fix merged: PR #{pr_number} fixed {branch}"
    elif result == "failed":
        summary = f"CI auto-fix failed on {branch} — manual intervention needed"
    else:
        summary = f"CI auto-fix on {branch} needs review: PR #{pr_number}"

    msg = await post_event_message(
        session,
        project_id,
        event_type="auto_fix_result",
        summary=summary,
        context={"branch": branch, "result": result, "pr_number": pr_number},
        task_id=task_id,
    )

    # Update per-branch failure tracking in the project's auto_remediation config
    if result in ("failed", "merged"):
        try:
            from sqlalchemy.orm.attributes import flag_modified

            project = await session.get(Project, project_id)
            if project is not None:
                design_doc = dict(project.design_doc or {})
                ar_config = dict(design_doc.get("_auto_remediation", {}))
                failure_counts: dict[str, int] = dict(
                    ar_config.get("branch_failure_counts", {})
                )
                suspended: list[str] = list(ar_config.get("suspended_branches", []))
                suspension_meta: dict[str, dict] = dict(
                    ar_config.get("suspension_metadata", {})
                )

                if result == "merged":
                    # Reset on success, but preserve recovery_attempts so that
                    # a branch which keeps failing-and-fixing eventually escalates
                    # to permanent suspension via exponential backoff.
                    failure_counts.pop(branch, None)
                    if branch in suspended:
                        suspended.remove(branch)
                    prior = suspension_meta.get(branch) or {}
                    prior_attempts = int(prior.get("recovery_attempts", 0) or 0)
                    if prior_attempts > 0:
                        suspension_meta[branch] = {"recovery_attempts": prior_attempts}
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
                        if triggering is not None and (triggering.context or {}).get(
                            "regression_of_task_id"
                        ):
                            threshold = (
                                _AUTO_REMEDIATION_REGRESSION_ESCALATION_THRESHOLD
                            )
                    if count >= threshold and branch not in suspended:
                        suspended.append(branch)
                        # Preserve recovery_attempts across re-suspensions so
                        # exponential backoff applies on repeated failures.
                        prior = suspension_meta.get(branch) or {}
                        suspension_meta[branch] = {
                            "suspended_at": _utcnow().isoformat(),
                            "recovery_attempts": int(
                                prior.get("recovery_attempts", 0) or 0
                            ),
                        }
                        ar_config["branch_failure_counts"] = failure_counts
                        ar_config["suspended_branches"] = suspended
                        ar_config["suspension_metadata"] = suspension_meta
                        design_doc["_auto_remediation"] = ar_config
                        project.design_doc = design_doc
                        flag_modified(project, "design_doc")
                        session.add(project)
                        # Fire escalation message + webhook
                        await post_auto_fix_escalation_message(
                            session,
                            project_id,
                            branch=branch,
                            failure_count=count,
                        )
                        asyncio.ensure_future(
                            dispatch_external_notification(
                                project_id,
                                "AUTO_FIX_ESCALATED",
                                {
                                    "branch": branch,
                                    "consecutive_failures": count,
                                    "threshold": threshold,
                                    "suspended_at": suspension_meta[branch][
                                        "suspended_at"
                                    ],
                                    "recovery_attempts": suspension_meta[branch][
                                        "recovery_attempts"
                                    ],
                                    "severity": "blocker",
                                    "manual_intervention_required": True,
                                    "summary": summary,
                                },
                            )
                        )
                        return msg

                ar_config["branch_failure_counts"] = failure_counts
                ar_config["suspended_branches"] = suspended
                ar_config["suspension_metadata"] = suspension_meta
                design_doc["_auto_remediation"] = ar_config
                project.design_doc = design_doc
                flag_modified(project, "design_doc")
                session.add(project)
        except Exception as exc:
            logger.warning(
                "[AUTO-REMEDIATION] Failed to update failure tracking for %s: %s",
                branch,
                exc,
            )

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
        f"Auto-remediation suspended for {branch} after {failure_count} consecutive failures. "
        f"Manual intervention required. Re-enable via the auto-remediation settings."
    )
    ctx: dict[str, Any] = {
        "event_type": "auto_fix_escalated",
        "branch": branch,
        "consecutive_failures": failure_count,
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
    return msg


# ─── Residual remediation lifecycle (mirror of CI auto-fix) ──────────────────
# Lives on ResidualRemediationCase rows (per-fingerprint), not on
# project.design_doc._auto_remediation (per-branch). Otherwise the lifecycle
# is the same: spawned → result(merged|failed|review_needed) →
# suspended after _AUTO_REMEDIATION_ESCALATION_THRESHOLD consecutive failures.


async def _get_or_create_residual_case(
    session: AsyncSession,
    project_id: uuid.UUID,
    fingerprint: str,
) -> ResidualRemediationCase:
    """Return the case row for (project_id, fingerprint), creating it if
    missing. The UNIQUE(project_id, fingerprint) added in 0054 means one
    row per fingerprint. Caller is responsible for the surrounding commit.
    """
    from sqlmodel import select

    result = await session.execute(
        select(ResidualRemediationCase).where(
            ResidualRemediationCase.project_id == project_id,
            ResidualRemediationCase.fingerprint == fingerprint,
        )
    )
    case = result.scalars().first()
    if case is None:
        case = ResidualRemediationCase(
            project_id=project_id,
            fingerprint=fingerprint,
        )
        session.add(case)
    return case


async def post_residual_fix_spawned_message(
    session: AsyncSession,
    project_id: uuid.UUID,
    fingerprint: str,
    sample_message: str,
    task_id: Optional[uuid.UUID] = None,
) -> ProjectMessage:
    """Post a message when residual-fingerprint remediation spawns a fix task.

    Also transitions the case row to REMEDIATING and stamps last_spawned_*
    so the next idle-tick spawn-dedup query sees the in-flight attempt.
    """
    case = await _get_or_create_residual_case(session, project_id, fingerprint)
    case.status = ResidualCaseStatus.REMEDIATING
    case.last_spawned_task_id = task_id
    case.last_spawned_at = _utcnow()
    case.updated_at = _utcnow()
    # Reset audit-deferral streak so a future audit cycle can re-escalate.
    case.audit_defer_count = 0
    case.audit_deferred_system_id = None
    session.add(case)

    summary = f"Residual auto-fix spawned for fingerprint {fingerprint[:32]}"
    msg = await post_event_message(
        session,
        project_id,
        event_type="residual_fix_spawned",
        summary=summary,
        context={
            "fingerprint": fingerprint,
            "sample_message": (sample_message or "")[:400],
            "task_id": str(task_id) if task_id else None,
        },
        task_id=task_id,
    )
    asyncio.ensure_future(
        dispatch_external_notification(
            project_id,
            "RESIDUAL_FIX_SPAWNED",
            {
                "fingerprint": fingerprint,
                "hit_count": case.hit_count,
                "task_id": str(task_id) if task_id else None,
            },
        )
    )
    return msg


async def post_residual_fix_result_message(
    session: AsyncSession,
    project_id: uuid.UUID,
    fingerprint: str,
    result: str,  # 'merged', 'failed', 'review_needed'
    pr_number: Optional[int] = None,
    task_id: Optional[uuid.UUID] = None,
) -> ProjectMessage:
    """Post a message when a residual auto-fix task completes.

    For result='failed': increments consecutive_failure_count on the case
    row, transitions to FAILED. When the count crosses
    _AUTO_REMEDIATION_ESCALATION_THRESHOLD, the row goes SUSPENDED, an
    escalation message + RESIDUAL_FIX_ESCALATED webhook fire.
    For result='merged': resets the counter, transitions to MERGED.
    For result='review_needed': leaves the row in REMEDIATING with a
    result message but does not change the failure counter — a human
    review is the next gate.
    """
    if result == "merged":
        summary = (
            f"Residual auto-fix merged: PR #{pr_number} resolved {fingerprint[:32]}"
        )
    elif result == "failed":
        summary = f"Residual auto-fix failed for {fingerprint[:32]} — manual intervention may be needed"
    else:
        summary = (
            f"Residual auto-fix needs review: PR #{pr_number} for {fingerprint[:32]}"
        )

    msg = await post_event_message(
        session,
        project_id,
        event_type="residual_fix_result",
        summary=summary,
        context={
            "fingerprint": fingerprint,
            "result": result,
            "pr_number": pr_number,
        },
        task_id=task_id,
    )

    if result == "review_needed":
        # Don't touch the failure counter or status — wait for the human
        # gate. The case stays REMEDIATING.
        if pr_number is not None:
            case = await _get_or_create_residual_case(session, project_id, fingerprint)
            case.last_spawned_pr_number = pr_number
            case.updated_at = _utcnow()
            session.add(case)
        return msg

    try:
        case = await _get_or_create_residual_case(session, project_id, fingerprint)
        if pr_number is not None:
            case.last_spawned_pr_number = pr_number
        case.updated_at = _utcnow()

        if result == "merged":
            case.consecutive_failure_count = 0
            case.status = ResidualCaseStatus.MERGED
            case.suspended_at = None
            session.add(case)
            return msg

        # result == 'failed'
        case.consecutive_failure_count = (case.consecutive_failure_count or 0) + 1
        count = case.consecutive_failure_count

        if (
            count >= _AUTO_REMEDIATION_ESCALATION_THRESHOLD
            and case.status != ResidualCaseStatus.SUSPENDED
        ):
            case.status = ResidualCaseStatus.SUSPENDED
            case.suspended_at = _utcnow()
            session.add(case)
            await post_residual_fix_escalation_message(
                session,
                project_id,
                fingerprint=fingerprint,
                failure_count=count,
                task_id=task_id,
            )
            asyncio.ensure_future(
                dispatch_external_notification(
                    project_id,
                    "RESIDUAL_FIX_ESCALATED",
                    {
                        "fingerprint": fingerprint,
                        "consecutive_failures": count,
                        "threshold": _AUTO_REMEDIATION_ESCALATION_THRESHOLD,
                        "suspended_at": case.suspended_at.isoformat()
                        if case.suspended_at
                        else None,
                        "severity": "blocker",
                        "manual_intervention_required": True,
                        "summary": summary,
                    },
                )
            )
            return msg

        case.status = ResidualCaseStatus.FAILED
        session.add(case)
    except Exception as exc:
        logger.warning(
            "[RESIDUAL-REMEDIATION] Failed to update case row for %s: %s",
            fingerprint,
            exc,
        )

    return msg


def is_residual_remediation_task(task: Task) -> bool:
    """True if `task` was spawned by autonomous_research's residual-fingerprint
    remediation path. Used by outcome wire-ups to discriminate between CI
    auto-fix events (post_auto_fix_*) and residual events (post_residual_fix_*)
    so a residual task does NOT fire the CI events as well."""
    return (task.context or {}).get("spawned_by") == "residual_fingerprint_remediation"


async def post_remediation_result_message(
    session: AsyncSession,
    task: Task,
    result: str,  # 'merged' | 'failed' | 'review_needed'
    pr_number: Optional[int] = None,
) -> ProjectMessage:
    """Discriminator: routes to post_residual_fix_result_message for tasks
    spawned by residual fingerprint remediation, otherwise to
    post_auto_fix_result_message for CI auto-fix tasks. Lets call sites
    treat the two lifecycles uniformly without each one re-implementing
    the spawned_by check."""
    if is_residual_remediation_task(task):
        return await post_residual_fix_result_message(
            session,
            task.project_id,
            fingerprint=(task.context or {}).get("error_fingerprint", ""),
            result=result,
            pr_number=pr_number,
            task_id=task.id,
        )
    branch = (task.context or {}).get("ci_failure_branch", "main")
    return await post_auto_fix_result_message(
        session,
        task.project_id,
        branch=branch,
        result=result,
        pr_number=pr_number,
        task_id=task.id,
    )


async def post_residual_fix_escalation_message(
    session: AsyncSession,
    project_id: uuid.UUID,
    fingerprint: str,
    failure_count: int,
    task_id: Optional[uuid.UUID] = None,
) -> ProjectMessage:
    """Post a BLOCKER-severity escalation message when residual auto-fix
    is suspended for a fingerprint after the threshold of consecutive
    failures. Mirrors post_auto_fix_escalation_message but per-fingerprint.
    """
    summary = (
        f"Residual auto-fix suspended for fingerprint {fingerprint[:32]} "
        f"after {failure_count} consecutive failures. Manual intervention "
        f"required. Re-enable via the Unsuspend button in the dashboard "
        f"or POST /api/admin/residual-cases/{fingerprint}/unsuspend."
    )
    ctx: dict[str, Any] = {
        "event_type": "residual_fix_escalated",
        "fingerprint": fingerprint,
        "consecutive_failures": failure_count,
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
    return msg


async def post_residual_fix_unsuspend_message(
    session: AsyncSession,
    project_id: uuid.UUID,
    fingerprint: str,
    task_id: Optional[uuid.UUID] = None,
) -> ProjectMessage:
    """Post a NOTE-severity message when a suspended residual fingerprint is
    manually unsuspended by an operator. Paired with the BLOCKER escalation
    message so the message stream shows suspend → unsuspend as a matched pair.
    Also fires RESIDUAL_FIX_UNSUSPENDED external webhook (opt-in).
    """
    recovered_at = datetime.now(timezone.utc).isoformat()
    summary = (
        f"Residual auto-fix re-enabled for fingerprint {fingerprint[:32]}. "
        f"Case manually unsuspended by operator. Spawn will resume after cooldown."
    )
    msg = await post_event_message(
        session,
        project_id,
        event_type="residual_fix_unsuspended",
        summary=summary,
        context={
            "fingerprint": fingerprint,
            "recovered_at": recovered_at,
            "manual_intervention_required": False,
        },
        task_id=task_id,
    )
    asyncio.ensure_future(
        dispatch_external_notification(
            project_id,
            "RESIDUAL_FIX_UNSUSPENDED",
            {
                "fingerprint": fingerprint,
                "recovered_at": recovered_at,
                "manual_intervention_required": False,
                "summary": summary,
            },
        )
    )
    return msg


async def post_residual_fix_audit_deferred_escalation_message(
    session: AsyncSession,
    project_id: uuid.UUID,
    fingerprint: str,
    defer_count: int,
    task_id: Optional[uuid.UUID] = None,
) -> ProjectMessage:
    """Post a BLOCKER-severity escalation when a residual case is repeatedly
    blocked by an in-progress audit.

    Called when audit_defer_count first crosses
    _RESIDUAL_AUDIT_DEFER_ESCALATION_THRESHOLD (default 5, configurable via
    runtime_config["residual_audit_defer_escalation_threshold"]). The case
    status stays ACTIVE — the case is legitimately still open, just waiting
    for the audit to complete.

    Callers are responsible for the idempotency guard: fire only when
    audit_defer_count == threshold (not >= threshold) so the message fires
    once per deferral streak. post_residual_fix_spawned_message resets
    audit_defer_count to 0 when a fix spawns so future audit cycles can
    re-escalate.
    """
    try:
        from app.shared_state import runtime_config

        threshold = int(
            runtime_config.get(
                "residual_audit_defer_escalation_threshold",
                _RESIDUAL_AUDIT_DEFER_ESCALATION_THRESHOLD,
            )
        )
    except Exception:
        threshold = _RESIDUAL_AUDIT_DEFER_ESCALATION_THRESHOLD

    summary = (
        f"Residual case for fingerprint {fingerprint[:32]} has been deferred by audit "
        f"{defer_count} times (threshold: {threshold}). No fix has been spawned yet — "
        f"the case is blocked pending audit completion."
    )
    ctx: dict[str, Any] = {
        "event_type": "residual_fix_audit_deferred",
        "fingerprint": fingerprint,
        "audit_defer_count": defer_count,
        "threshold": threshold,
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
    asyncio.ensure_future(
        dispatch_external_notification(
            project_id,
            "RESIDUAL_FIX_AUDIT_DEFERRED",
            {
                "fingerprint": fingerprint,
                "audit_defer_count": defer_count,
                "threshold": threshold,
                "severity": "blocker",
                "manual_intervention_required": False,
                "summary": summary,
            },
        )
    )
    return msg


async def close_residual_case_resolved(
    session: AsyncSession,
    project_id: uuid.UUID,
    fingerprint: str,
    reason: str = "fingerprint_went_silent",
) -> ProjectMessage:
    """Transition a ResidualRemediationCase to RESOLVED and emit a notification.

    Called by the reaper when a REMEDIATING case has been silent past the
    auto-resolve threshold. Distinct from MERGED (PR-driven close) and from
    CLOSED_ORGANICALLY (legacy reaper close of ACTIVE rows).
    """
    case = await _get_or_create_residual_case(session, project_id, fingerprint)
    case.status = ResidualCaseStatus.RESOLVED
    case.updated_at = _utcnow()
    session.add(case)

    summary = f"Residual case auto-resolved: fingerprint {fingerprint[:32]} went silent"
    msg = await post_event_message(
        session,
        project_id,
        event_type="residual_case_auto_resolved",
        summary=summary,
        context={"fingerprint": fingerprint, "reason": reason},
    )
    asyncio.ensure_future(
        dispatch_external_notification(
            project_id,
            "RESIDUAL_CASE_AUTO_RESOLVED",
            {"fingerprint": fingerprint, "reason": reason},
        )
    )
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
    asyncio.ensure_future(
        dispatch_external_notification(
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
        )
    )
    return msg


async def post_direction_burn_pause_message(
    session: AsyncSession,
    project_id: uuid.UUID,
    direction_text: str,
    reason: str,
    direction_id: Optional[str] = None,
    evidence: Optional[dict[str, Any]] = None,
    task_id: Optional[uuid.UUID] = None,
) -> ProjectMessage:
    """Post a BLOCKER-severity message when a direction is auto-paused for manual intervention.

    Distinct from the routine direction_milestone event — this fires as BLOCKER so it
    surfaces in the high-signal feed. Includes burn evidence (counts + merge rate) so
    the operator sees the numbers without drilling into direction details.
    """
    ev = evidence or {}
    ev_parts: list[str] = []
    if "lifetime_non_merge_finished" in ev:
        ev_parts.append(f"non-merging={ev['lifetime_non_merge_finished']}")
    if "lifetime_merged" in ev:
        ev_parts.append(f"merged={ev['lifetime_merged']}")
    if "lifetime_merge_rate" in ev:
        rate = ev["lifetime_merge_rate"]
        ev_parts.append(
            f"merge_rate={rate:.0%}"
            if isinstance(rate, float)
            else f"merge_rate={rate}"
        )
    ev_str = f" ({', '.join(ev_parts)})" if ev_parts else ""
    summary = (
        f"Direction auto-paused — manual intervention required: "
        f"'{direction_text[:80]}'{ev_str} — {reason}"
    )
    ctx: dict[str, Any] = {
        "event_type": "direction_burn_pause",
        "direction_text": direction_text[:120],
        "reason": reason,
        "direction_id": direction_id,
        "manual_intervention_required": True,
    }
    if evidence:
        ctx["evidence"] = evidence
    msg = ProjectMessage(
        project_id=project_id,
        message_type=MessageType.BLOCKER,
        from_type="system",
        from_task_id=task_id,
        content=summary,
        context=ctx,
    )
    session.add(msg)
    asyncio.ensure_future(
        dispatch_external_notification(
            project_id,
            "DIRECTION_BURN_PAUSE",
            {
                "direction_text": direction_text[:120],
                "reason": reason,
                "direction_id": direction_id,
                "evidence": evidence or {},
                "severity": "blocker",
                "manual_intervention_required": True,
                "summary": summary,
            },
        )
    )
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
        summary = f"Blueprint review dismissed on PR #{pr_number}" + (
            f": {detail}" if detail else ""
        )
    elif signal == "user_overrode_review":
        summary = f"User approved PR #{pr_number} after Blueprint requested changes"
    elif signal == "user_caught_issues":
        summary = (
            f"User requested changes on PR #{pr_number} that Blueprint had approved"
        )
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


async def post_fix_loop_divergence_regression_message(
    session: AsyncSession,
    project_id: uuid.UUID,
    regressions: list[dict[str, Any]],
    window_days: int = 7,
) -> ProjectMessage:
    """Alert operators when fix-loop divergence rate regresses across consecutive windows.

    Mirrors post_account_model_regression_message — creates a NOTE/system message
    and fires an external webhook. Each regression entry contains loop, prior_rate,
    current_rate, prior_count, current_count.
    """
    lines = ", ".join(
        f"{r['loop']} ({r['prior_rate']:.0%}→{r['current_rate']:.0%}, "
        f"n={r['prior_count']}→{r['current_count']})"
        for r in regressions
    )
    summary = f"Fix-loop divergence rate regressed: {lines} — check /admin#fix-loop"
    msg = await post_event_message(
        session,
        project_id,
        event_type="fix_loop_divergence_regressed",
        summary=summary,
        context={"regressions": regressions, "window_days": window_days},
    )
    asyncio.ensure_future(
        dispatch_external_notification(
            project_id,
            "FIX_LOOP_DIVERGENCE_REGRESSED",
            {
                "regressions": regressions,
                "window_days": window_days,
                "summary": summary,
            },
        )
    )
    return msg
