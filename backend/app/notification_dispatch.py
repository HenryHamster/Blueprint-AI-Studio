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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.url_security import _resolve_safe_external_url
from app.field_encryption import encrypt_field, decrypt_field
from app.shared_state import get_project_promotion_target

from app.message_audience import (
    PIPELINE_LOG,
    is_external_webhook_operator_visible,
    route_event,
    stamp_message_audience,
)

from app.models import (
    MessageHandlingState,
    MessageSurface,
    MessageType,
    NotificationDeadLetter,
    Project,
    ProjectMessage,
    ResidualCaseStatus,
    ResidualRemediationCase,
    Task,
    _utcnow,
)
from app.project_message_creation import (
    build_project_message,
    lock_project_and_next_message_sequence,
)

logger = logging.getLogger(__name__)

# Module-level throttle tracking: (project_id, event_type) -> last dispatch monotonic time (seconds)
_last_dispatch_time: dict[tuple[str, str], float] = {}

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

# System-emitted NOTE event types classified as high_signal in event_type_snapshot.json.
# These must always survive paging regardless of routine message volume (see
# admin_notifications.list_messages guaranteed-query logic). This includes the
# operator-steering receipt/confirmation pair so the interim queued receipt is
# visible while it waits behind an active cycle. Escalation types
# (auto_fix_escalated, residual_fix_escalated, etc.) are already safe because they emit
# as BLOCKER messages — not NOTE — so they don't belong here.
# Source of truth: backend/testing/event_type_snapshot.json (classification=high_signal).
HIGH_SIGNAL_NOTE_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "failure_alert",  # post_failure_alert_message
        "rollback_recommended",  # post_rollback_recommendation_message
        "fix_loop_divergence_regressed",  # post_fix_loop_divergence_regression_message
        "direction_outcome_stalled",  # post_direction_outcome_stalled_message
        "direction_staged_activated",  # post_staged_directions_activated_message
        "promotion_readiness_reached",  # post_version_promotion_readiness_message
        "release_calcifying",  # post_version_calcification_message
        "release_build_stalled",  # post_version_build_stalled_message
        "release_stabilize_stalled",  # post_version_stabilize_stalled_message
        "release_ci_unresolved_stall",  # post_version_ci_unresolved_stall_message
        "release_version_abandoned",  # post_release_version_abandoned_message
        "strategic_steering_deferred",  # post_strategic_steering_deferred_message
        "strategic_steering_acted",  # post_strategic_steering_acted_message
        "capability_trial_ambiguous_lift_escalation",  # raise_ambiguous_lift_escalation_notice
        "criterion_retracted",  # post_criterion_retracted_message
        "criterion_superseded",  # post_criterion_superseded_message
        "question_answer",  # post_question_answer
        "capability_proof_gate_graduated_observe",  # post_capability_proof_gate_graduated_message
        "capability_proof_gate_graduated_enforce",  # post_capability_proof_gate_graduated_message
        "mode_knob_graduated_observe",  # post_mode_knob_graduated_message
        "mode_knob_graduated_enforce",  # post_mode_knob_graduated_message
        "mode_knob_cut_recommended",  # post_mode_knob_cut_recommendation_message
    }
)


@dataclasses.dataclass
class _RetryEntry:
    project_id: Any
    event_type: str
    payload: dict
    body: dict  # pre-built JSON body
    attempts: int  # number of attempts already made
    next_retry_at: float  # monotonic time
    # Snapshot of the destination at the time the retry was scheduled. Stored
    # per-entry so a retry delivers to the URL the notification was originally
    # destined for — NOT whatever webhook_url runtime_config happens to hold
    # when the drain fires (which can change between dispatch and retry, causing
    # a retry to leak to the wrong endpoint). Defaulted for backward-compat with
    # call sites/tests that construct entries positionally.
    webhook_url: str = ""


# Bounded in-memory retry queue (max 50 entries). When full, the OLDEST entry
# is evicted on append; ``_enqueue_retry`` logs that eviction so a silently
# dropped alert-retry is at least visible in the logs (cluster C12 — bounded
# maxlen silently evicts). The independent drainer (``notification_retry_drain_service``)
# flushes due entries on its own schedule regardless of new dispatches.
_RETRY_QUEUE_MAXLEN = 50
_retry_queue: collections.deque[_RetryEntry] = collections.deque(
    maxlen=_RETRY_QUEUE_MAXLEN
)

# Independent retry-drainer cadence. The queue used to be drained ONLY as a
# prefix to a new dispatch, so during quiet periods (exactly when an alert
# retry matters most) due retries stalled indefinitely. This background service
# flushes them on its own ~30s schedule. Per-entry webhook_url means the drainer
# needs no ambient URL.
_RETRY_DRAIN_INTERVAL_SECONDS = 30


def _enqueue_retry(entry: _RetryEntry) -> Optional[_RetryEntry]:
    """Append a retry entry, returning the entry evicted to make room (if any).

    ``collections.deque(maxlen=...)`` silently discards the oldest element on
    append once full. For an alert-delivery retry queue that silent loss is a
    real signal disappearing, so we detect the at-capacity case, log the
    evicted entry, and return it so the caller can durably dead-letter it (see
    ``_enqueue_retry_durable``). Returns ``None`` when nothing was evicted.
    """
    evicted: Optional[_RetryEntry] = None
    if len(_retry_queue) >= _RETRY_QUEUE_MAXLEN:
        evicted = _retry_queue[0]
        logger.warning(
            "[NOTIFY] Retry queue full (maxlen=%d); evicting oldest undelivered "
            "retry (event=%s, project=%s, attempts=%d) to make room",
            _RETRY_QUEUE_MAXLEN,
            evicted.event_type,
            evicted.project_id,
            evicted.attempts,
        )
    _retry_queue.append(entry)
    return evicted


async def _write_dead_letter(
    entry: _RetryEntry, reason: str, last_status: Optional[int] = None
) -> None:
    """Durably record a notification that will never be delivered.

    Called when a retry is ``"exhausted"`` (all attempts used up) or
    ``"evicted"`` (dropped from the full in-memory queue). Persists a
    ``NotificationDeadLetter`` row so the failed escalation survives restarts
    and stays operator-visible/re-drivable instead of vanishing. Best-effort:
    a persistence failure is logged and swallowed — dead-lettering must never
    break the dispatch/drain path.
    """
    from app.database import closing_session

    try:
        async with closing_session() as session:
            session.add(
                NotificationDeadLetter(
                    project_id=str(entry.project_id),
                    event_type=entry.event_type,
                    reason=reason,
                    attempts=entry.attempts + 1,
                    last_status=last_status,
                    webhook_url=encrypt_field(entry.webhook_url) or "",
                    payload=entry.payload or {},
                    body=entry.body or {},
                )
            )
            await session.commit()
    except Exception as exc:
        logger.exception(
            "[NOTIFY] Dead-letter write FAILED for %s/%s (%s) — notification unrecoverable",
            entry.project_id,
            entry.event_type,
            reason,
        )
        # Best-effort: surface the double-failure (delivery failed + dead-letter
        # write failed) as an operator-visible Alert so it is not silently lost.
        try:
            from app.database import closing_session as _cs
            from app.models import Alert as _Alert, AlertSeverity as _Sev
            async with _cs() as _alert_session:
                _alert_session.add(
                    _Alert(
                        project_id=str(entry.project_id),
                        severity=_Sev.WARNING,
                        title="Notification delivery permanently lost",
                        message=(
                            f"A {entry.event_type!r} notification exhausted retries "
                            f"({reason}) but the dead-letter record could not be "
                            f"persisted: {exc}. The notification is unrecoverable."
                        )[:1000],
                        event_type="notification.dead_letter_lost",
                    )
                )
                await _alert_session.commit()
        except Exception:
            logger.exception(
                "[NOTIFY] Could not surface dead-letter loss as alert for %s/%s",
                entry.project_id,
                entry.event_type,
            )


async def _enqueue_retry_durable(entry: _RetryEntry) -> None:
    """Enqueue a retry, dead-lettering any entry evicted to make room.

    Wraps the synchronous ``_enqueue_retry`` so callers in async contexts get
    durable eviction handling without duplicating the write. The evicted entry
    would otherwise disappear silently once the bounded deque is full.
    """
    evicted = _enqueue_retry(entry)
    if evicted is not None:
        await _write_dead_letter(evicted, reason="evicted")


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


async def list_dead_letters(
    session: AsyncSession,
    project_id: Any,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[dict], int]:
    """List NotificationDeadLetter rows for a project, newest first, with pagination.

    Returns a (items, total) tuple where items is a list of dicts with id,
    project_id, event_type, reason, attempts, last_status, webhook_url, payload,
    body, and created_at for each row, and total is the unpaginated count.
    """
    from sqlalchemy import func as _func
    from sqlmodel import select as _dl_select

    base_where = NotificationDeadLetter.project_id == str(project_id)
    total_result = await session.execute(
        _dl_select(_func.count()).select_from(NotificationDeadLetter).where(base_where)
    )
    total = total_result.scalar_one()

    result = await session.execute(
        _dl_select(NotificationDeadLetter)
        .where(base_where)
        .order_by(NotificationDeadLetter.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    rows = result.scalars().all()
    return [
        {
            "id": str(r.id),
            "project_id": r.project_id,
            "event_type": r.event_type,
            "reason": r.reason,
            "attempts": r.attempts,
            "last_status": r.last_status,
            "webhook_url": decrypt_field(r.webhook_url) or "",
            "payload": r.payload,
            "body": r.body,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ], total


async def redrive_dead_letter(session: AsyncSession, dead_letter_id: str, project_id: str) -> dict:
    """Re-enqueue a dead-lettered notification and delete the row.

    Loads the NotificationDeadLetter row, reconstructs a _RetryEntry from its
    preserved payload/body, schedules it for immediate retry via
    ``_enqueue_retry_durable``, then deletes the row so it is not replayed twice.

    Validates that the row belongs to ``project_id`` — raises ``ValueError``
    (caller should translate to 404) when the row does not exist or belongs to a
    different project, so an attacker who knows a dead-letter UUID cannot redrive
    (and delete) a row from a project they are not a member of.
    """
    row = await session.get(NotificationDeadLetter, uuid.UUID(dead_letter_id))
    if row is None:
        raise ValueError("dead letter not found")
    if str(row.project_id) != str(project_id):
        raise ValueError("dead letter not found")
    entry = _RetryEntry(
        project_id=row.project_id,
        event_type=row.event_type,
        payload=row.payload or {},
        body=row.body or {},
        attempts=0,
        next_retry_at=time.monotonic(),
        webhook_url=decrypt_field(row.webhook_url) or "",
    )
    await _enqueue_retry_durable(entry)
    await session.delete(row)
    await session.commit()
    return {"status": "redriven", "id": dead_letter_id}


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


async def _drain_retry_queue(webhook_url: str = "") -> None:
    """Process due retry entries.

    Called both as a prefix to a new dispatch AND independently from
    ``notification_retry_drain_service`` on a fixed schedule, so retries are no
    longer stranded during quiet periods (when no new dispatch ever fires).

    Each retry is delivered to the URL stored ON THE ENTRY (``entry.webhook_url``)
    — the destination snapshotted when the notification was first scheduled —
    NOT the ambient ``webhook_url`` argument, which may have changed in
    runtime_config between dispatch and retry. The argument is kept only as a
    fallback for entries with no stored URL (legacy / positionally-constructed
    entries). An entry with neither is dropped (cannot deliver to an unknown
    destination).
    """
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
        target_url = entry.webhook_url or webhook_url
        if not target_url:
            total_failed += 1
            last_failure_reason = "no webhook_url on retry entry"
            _record_project_stat(entry.project_id, "failed", last_failure_reason)
            logger.warning(
                "[NOTIFY] Dropping retry for %s — no target webhook_url recorded",
                entry.event_type,
            )
            continue
        try:
            status = await _attempt_delivery(target_url, entry.body)
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
                await _enqueue_retry_durable(
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
                await _write_dead_letter(entry, reason="exhausted", last_status=status)
                logger.warning(
                    f"[NOTIFY] Exhausted retries for {entry.event_type} after "
                    f"{entry.attempts + 1} attempts"
                )
        except Exception as exc:
            if entry.attempts + 1 < len(_RETRY_DELAYS):
                delay = _RETRY_DELAYS[entry.attempts + 1]
                await _enqueue_retry_durable(
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
                await _write_dead_letter(entry, reason="exhausted")
                logger.warning(
                    f"[NOTIFY] Exhausted retries for {entry.event_type}: {exc}"
                )


async def notification_retry_drain_service() -> None:
    """Background service: flush the webhook retry queue on a fixed schedule.

    Independent of new dispatches. Previously the retry queue was drained ONLY
    as a prefix to a fresh ``dispatch_external_notification`` call, so during
    quiet periods — exactly when a backlog of alert retries matters most — due
    retries sat undelivered indefinitely. This loop wakes every
    ``_RETRY_DRAIN_INTERVAL_SECONDS`` and drains using each entry's stored
    ``webhook_url``, so it needs no ambient configuration. Best-effort: a drain
    failure is logged and the loop continues.
    """
    while True:
        try:
            await asyncio.sleep(_RETRY_DRAIN_INTERVAL_SECONDS)
            if _retry_queue:
                await _drain_retry_queue()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("[NOTIFY] Retry drain loop error: %s", exc)


_DEFERRED_EXTERNAL_NOTIFICATIONS_KEY = "_deferred_external_notifications"


def begin_deferred_external_notifications(session: AsyncSession) -> None:
    """Defer session-scoped webhooks until an authority-fenced commit succeeds."""
    session.info.setdefault(_DEFERRED_EXTERNAL_NOTIFICATIONS_KEY, [])


def _schedule_external_notification(
    session: AsyncSession,
    project_id: Any,
    event_type: str,
    payload: dict,
) -> None:
    pending = session.info.get(_DEFERRED_EXTERNAL_NOTIFICATIONS_KEY)
    if pending is not None:
        pending.append((project_id, event_type, dict(payload)))
        return
    asyncio.ensure_future(
        dispatch_external_notification(project_id, event_type, payload)
    )


def flush_deferred_external_notifications(session: AsyncSession) -> None:
    """Schedule webhooks only after the associated transaction has committed."""
    pending = session.info.pop(_DEFERRED_EXTERNAL_NOTIFICATIONS_KEY, None)
    if pending is None:
        return
    for project_id, event_type, payload in pending:
        asyncio.ensure_future(
            dispatch_external_notification(project_id, event_type, payload)
        )


def discard_deferred_external_notifications(session: AsyncSession) -> None:
    """Drop effects staged by a transaction that failed its authority fence."""
    session.info.pop(_DEFERRED_EXTERNAL_NOTIFICATIONS_KEY, None)


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
        # Unified audience gate: the external webhook is a third operator-reaching
        # channel and must obey the same fail-closed policy as the ledger and the
        # alert channel. A strategist-internal / pipeline-log event (e.g. PR_MERGED,
        # DIRECTION_MILESTONE, AUTO_FIX_SPAWNED) is dropped here even when the
        # operator's enabled_events (legacy config) still lists it — internal
        # bookkeeping never leaks to an external system.
        if not is_external_webhook_operator_visible(event_type):
            logger.debug(
                "[NOTIFY] Dropping non-operator external webhook event_type=%s "
                "(strategist-internal/pipeline-log — audience gate)",
                event_type,
            )
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

        # Per-project/event throttle: one noisy project must not suppress another.
        now = time.monotonic()
        throttle_key = (str(project_id), event_type)
        last = _last_dispatch_time.get(throttle_key, 0.0)
        if throttle_seconds > 0 and (now - last) < throttle_seconds:
            return

        _last_dispatch_time[throttle_key] = now

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
                await _enqueue_retry_durable(
                    _RetryEntry(
                        project_id=project_id,
                        event_type=event_type,
                        payload=payload,
                        body=body,
                        attempts=0,
                        next_retry_at=time.monotonic() + delay,
                        webhook_url=webhook_url,
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
            await _enqueue_retry_durable(
                _RetryEntry(
                    project_id=project_id,
                    event_type=event_type,
                    payload=payload,
                    body=body,
                    attempts=0,
                    next_retry_at=time.monotonic() + delay,
                    webhook_url=webhook_url,
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
    idempotency_key: str | None = None,
) -> Optional[ProjectMessage]:
    """Core helper — creates a system ProjectMessage with the given event_type in context.

    Every created row is stamped with ``context["audience"]`` resolved from the
    unified audience registry (:func:`app.message_audience.route_event`), so both
    the ledger read paths and downstream surfaces can tell an operator-visible
    message from a strategist-feed one:

    * ``operator-action-needed`` / ``operator-fyi`` → operator-visible NOTE (as before).
    * ``strategist-internal`` → a non-operator strategist-feed row is still created
      (bookkeeping the strategist may want), but tagged so it never surfaces to the
      operator. Unregistered event types fail closed to this class.
    * ``pipeline-log`` → pure telemetry; **no** ledger row is created and ``None``
      is returned.

    Callers that forward the return value MUST handle ``None`` for the pipeline-log
    case.
    """
    audience = route_event(event_type)
    if audience == PIPELINE_LOG:
        # Pure telemetry — no ledger row at all, logs/observability only.
        logger.debug(
            "[NOTIFY] Skipping ledger row for pipeline-log event_type=%s",
            event_type,
        )
        return None
    normalized_idempotency_key = (idempotency_key or "").strip()[:200] or None
    if normalized_idempotency_key is not None:
        existing = await session.scalar(
            select(ProjectMessage)
            .where(
                ProjectMessage.project_id == project_id,
                ProjectMessage.idempotency_key == normalized_idempotency_key,
            )
            .limit(1)
        )
        if existing is not None:
            return existing
    # Stamp the resolved audience via the shared helper so the message carries
    # a fail-closed audience classification consistent with every other
    # operator-reaching emitter. ``stamp_message_audience`` sets event_type
    # (preserving any existing value via setdefault) and always overwrites
    # audience with the canonical registry resolution — a caller-supplied
    # audience in ``context`` never wins over the registry.
    ctx = stamp_message_audience(context, event_type, message_type=MessageType.NOTE.value)
    msg = await build_project_message(
        session,
        project_id=project_id,
        message_type=MessageType.NOTE,
        from_type="system",
        from_task_id=task_id,
        content=summary,
        context=ctx,
        idempotency_key=normalized_idempotency_key,
    )
    session.add(msg)
    return msg


async def post_criterion_retracted_message(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    criterion_key: str,
    text: str,
    reason: str,
    release_version_id: Any,
    retracted_by: str,
    task_id: Optional[uuid.UUID] = None,
) -> ProjectMessage:
    """Post a HIGH-SIGNAL system NOTE when an active acceptance criterion is retracted.

    Fired by the autonomous safety valve (and any loop-driven retraction) when a
    previously-active criterion transitions to ``retracted``. Operators must see
    that a quality gate was removed without their action, so the event type is
    registered in ``HIGH_SIGNAL_NOTE_EVENT_TYPES`` and survives paging.
    """
    summary = (
        f"Acceptance criterion {criterion_key} retracted by {retracted_by} — "
        f"{(reason or '').strip()[:160]}"
    )
    return await post_event_message(
        session,
        project_id,
        event_type="criterion_retracted",
        summary=summary,
        context={
            "criterion_key": criterion_key,
            "text": (text or "")[:200],
            "reason": (reason or "")[:400],
            "release_version_id": str(release_version_id),
            "retracted_by": retracted_by,
        },
        task_id=task_id,
    )


async def post_criterion_superseded_message(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    criterion_key: str,
    text: str,
    reason: str,
    release_version_id: Any,
    superseded_by_key: str,
    retracted_by: str,
    task_id: Optional[uuid.UUID] = None,
) -> ProjectMessage:
    """Post a HIGH-SIGNAL system NOTE when an active acceptance criterion is superseded.

    Symmetric to :func:`post_criterion_retracted_message` but for the
    supersession case, where the criterion is replaced by ``superseded_by_key``
    rather than simply retracted. Registered in ``HIGH_SIGNAL_NOTE_EVENT_TYPES``.
    """
    summary = (
        f"Acceptance criterion {criterion_key} superseded by {superseded_by_key} "
        f"({retracted_by}) — {(reason or '').strip()[:160]}"
    )
    return await post_event_message(
        session,
        project_id,
        event_type="criterion_superseded",
        summary=summary,
        context={
            "criterion_key": criterion_key,
            "text": (text or "")[:200],
            "reason": (reason or "")[:400],
            "release_version_id": str(release_version_id),
            "superseded_by_key": superseded_by_key,
            "retracted_by": retracted_by,
        },
        task_id=task_id,
    )


async def post_capability_proof_gate_graduated_message(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    from_mode: str,
    to_mode: str,
    coverage: float,
    threshold: float,
    projects_contributing: int,
    total_non_exempt: int,
    total_proven: int,
    task_id: Optional[uuid.UUID] = None,
) -> Optional[ProjectMessage]:
    """Post a HIGH-SIGNAL system NOTE when the capability-proof gate auto-graduates.

    The metrics-integrity sweep advances the gate mode ``off → observe → enforce``
    autonomously once fleet-wide proof coverage crosses the configured thresholds.
    Previously this significant behavior change was recorded only in an INFO log —
    the operator learned about it by reading logs or noticing the settings mode
    flipped. This routes an operator-visible message through the unified audience
    system on every advance.

    The event type (and hence audience) is split by target mode:

    * ``→ observe`` → ``capability_proof_gate_graduated_observe`` (``operator-fyi``):
      the gate now records unproven-capability blocks but does not enforce them.
    * ``→ enforce`` → ``capability_proof_gate_graduated_enforce``
      (``operator-action-needed``): the gate now BLOCKS task spawning and release
      promotion for capabilities lacking proof — a change the operator should act
      on (raise proof coverage or relax the thresholds).

    Both event types are registered in ``HIGH_SIGNAL_NOTE_EVENT_TYPES`` so the
    NOTE survives paging. Best-effort: the in-session message rides the caller's
    commit.
    """
    to_mode_norm = (to_mode or "").strip().lower()
    if to_mode_norm == "enforce":
        event_type = "capability_proof_gate_graduated_enforce"
        impact = (
            "gate now BLOCKS task spawning and release promotion for "
            "capabilities lacking proof"
        )
    else:
        event_type = "capability_proof_gate_graduated_observe"
        impact = "gate now records unproven-capability blocks (no blocking yet)"

    summary = (
        f"Capability-proof gate auto-graduated {from_mode} → {to_mode_norm} "
        f"(fleet coverage={coverage:.1%} ≥ threshold={threshold:.1%}, "
        f"{total_proven}/{total_non_exempt} proven across "
        f"{projects_contributing} project(s)) — {impact}."
    )
    return await post_event_message(
        session,
        project_id,
        event_type=event_type,
        summary=summary,
        context={
            "from_mode": from_mode,
            "to_mode": to_mode_norm,
            "coverage": round(float(coverage), 4),
            "threshold": round(float(threshold), 4),
            "projects_contributing": projects_contributing,
            "total_non_exempt": total_non_exempt,
            "total_proven": total_proven,
        },
        task_id=task_id,
    )


async def post_mode_knob_graduated_message(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    config_key: str,
    from_mode: str,
    to_mode: str,
    signal: float,
    threshold: float,
    measurements: dict[str, Any],
    task_id: Optional[uuid.UUID] = None,
) -> Optional[ProjectMessage]:
    """Post a HIGH-SIGNAL system NOTE when a dormant mode-knob auto-graduates.

    The generalization of :func:`post_capability_proof_gate_graduated_message` to
    every knob driven by the shared mode-knob graduation harness
    (:mod:`app.mode_knob_graduation`). The knob advances ``off → observe → enforce``
    autonomously once its measured counterfactual signal crosses the configured
    threshold. The event type (and hence audience) is split by target mode:

    * ``→ observe`` → ``mode_knob_graduated_observe`` (``operator-fyi``): the knob
      now records its would-be decisions but does not enforce them.
    * ``→ enforce`` → ``mode_knob_graduated_enforce`` (``operator-action-needed``):
      the knob's real blocking branch is now live.
    """
    to_mode_norm = (to_mode or "").strip().lower()
    if to_mode_norm == "enforce":
        event_type = "mode_knob_graduated_enforce"
        impact = "the knob's enforce branch is now live and can block"
    else:
        event_type = "mode_knob_graduated_observe"
        impact = "the knob now records its would-be decisions (no blocking yet)"

    summary = (
        f"Mode-knob {config_key} auto-graduated {from_mode} → {to_mode_norm} "
        f"(measured signal={signal:.3f} ≥ threshold={threshold:.3f}) — {impact}."
    )
    return await post_event_message(
        session,
        project_id,
        event_type=event_type,
        summary=summary,
        context={
            "config_key": config_key,
            "from_mode": from_mode,
            "to_mode": to_mode_norm,
            "signal": round(float(signal), 4),
            "threshold": round(float(threshold), 4),
            "measurements": measurements,
        },
        task_id=task_id,
    )


async def post_mode_knob_cut_recommendation_message(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    config_key: str,
    signal: float,
    measurements: dict[str, Any],
    task_id: Optional[uuid.UUID] = None,
    idempotency_key: str | None = None,
) -> Optional[ProjectMessage]:
    """Post an activity NOTE recommending a dormant mode-knob be CUT.

    Fired by the shared mode-knob graduation harness when a knob's measured
    counterfactual shows its enforce branch would never bind (no lift) over a
    representative window — the default-off PRINCIPLE's "cut, with numbers"
    disposition. Carries the measured signal so the operator can act (remove the
    knob) rather than let it age as completion debt indefinitely. FYI-class: it
    changes nothing on its own. The completion-debt ledger is the durable
    operator surface; this event is explicitly not part of Strategic Chat.
    """
    would_have_blocked = measurements.get("would_have_blocked")
    summary = (
        f"Mode-knob {config_key} shows NO measured lift "
        f"(signal={signal:.3f}, would-have-blocked={would_have_blocked}) — "
        "recommend CUT: the enforce branch would never bind. Remove the knob "
        "rather than carry it as dormant completion debt."
    )
    return await post_event_message(
        session,
        project_id,
        event_type="mode_knob_cut_recommended",
        summary=summary,
        context={
            "config_key": config_key,
            "signal": round(float(signal), 4),
            "measurements": measurements,
        },
        task_id=task_id,
        idempotency_key=idempotency_key,
    )


async def post_strategic_steering_deferred_message(
    session: AsyncSession,
    project_id: uuid.UUID,
    reason: str,
) -> ProjectMessage:
    """Post a visible system message when an operator steering request is deferred.

    An operator steering message routes through the coalesced strategic-refresh
    marker. When it cannot schedule the strategist immediately (a strategic
    cycle is already running, or a residual gate holds it), the operator must be
    told *why* rather than left with silence. ``reason`` is a short
    human-readable explanation (e.g. "queued behind an active strategic cycle").
    """
    return await post_event_message(
        session,
        project_id,
        event_type="strategic_steering_deferred",
        summary=f"Strategic steering request deferred — {reason}",
        context={"reason": reason},
    )


async def post_strategic_steering_acted_message(
    session: AsyncSession,
    project_id: uuid.UUID,
    reason: str = "the strategist is now acting on your steering",
) -> ProjectMessage:
    """Post a HIGH-SIGNAL system message when operator steering reaches the strategist.

    The symmetric success counterpart to
    ``post_strategic_steering_deferred_message``. An operator steering message
    routes through the coalesced strategic-refresh marker; when the autonomous
    loop actually consumes an operator-origin marker and spawns the strategist,
    the operator must be told their steering landed rather than left to guess
    from silence. Classified ``high_signal`` (NOT critical/escalation) so it
    surfaces in the high-signal feed and fires an info-severity toast — higher
    signal than the routine deferral notice, without the error styling reserved
    for failures.
    """
    return await post_event_message(
        session,
        project_id,
        event_type="strategic_steering_acted",
        summary=f"Strategic steering reached the strategist — {reason}",
        context={"reason": reason},
    )


async def post_pr_merged_message(
    session: AsyncSession,
    project_id: uuid.UUID,
    pr_number: int,
    direction_text: str,
    task_count: int,
    task_id: Optional[uuid.UUID] = None,
    idempotency_key: str | None = None,
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
        idempotency_key=idempotency_key,
    )


async def post_operator_decision_capture_low_message(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    total: int,
    captured: int,
    missed: int,
    capture_rate: int,
    session_id: str,
    task_id: Optional[uuid.UUID] = None,
) -> Optional[ProjectMessage]:
    """Strategist-internal notice that operator-decision capture is chronically low.

    Fired by the strategic-session heartbeat when a session accumulates enough
    operator turns and its capture rate stays below the low-rate floor — a signal
    that the OPERATOR-DECISION marker is being systematically forgotten and that
    operator corrections may not be reaching the durable decisions register. The
    event routes ``strategist-internal`` so it is bookkeeping the strategist can
    act on (re-emit the marker) WITHOUT surfacing to the operator chat or the
    alert channel. The caller de-dupes so it fires at most once per session per
    low-capture crossing.
    """
    summary = (
        f"Operator-decision capture chronically low this session: "
        f"{captured}/{total} captured ({capture_rate}%) — {missed} operator "
        f"correction(s) may not have reached the durable decisions register. "
        f"End a turn that settles or corrects direction with the literal line "
        f"`OPERATOR-DECISION: <settled fact>` so it survives compaction and "
        f"restart. Routine chat needs no decision marker."
    )
    return await post_event_message(
        session,
        project_id,
        event_type="operator_decision.capture_rate_low",
        summary=summary,
        context={
            "session_id": session_id,
            "total": total,
            "captured": captured,
            "missed": missed,
            "capture_rate": capture_rate,
        },
        task_id=task_id,
    )


async def post_operator_decision_summary_absent_message(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    last_summary_at: str,
    session_id: str,
    task_id: Optional[uuid.UUID] = None,
) -> Optional[ProjectMessage]:
    """Strategist-internal notice that operator_decision_summary stopped arriving.

    Fired by the strategic-session heartbeat when heartbeats keep arriving but the
    operator_decision_summary telemetry blob has stopped refreshing for longer than
    the staleness window, despite the session having enough historical data to judge
    it. Routes strategist-internal so the strategist can investigate without
    creating operator-visible noise. The caller de-dupes so it fires at most once
    per absent crossing; recovery (agent resumes sending) clears the dedup flag.
    """
    summary = (
        f"operator_decision_summary absent this session: heartbeats are arriving "
        f"but the telemetry blob stopped refreshing (last received: {last_summary_at}). "
        f"Check the agent's decision-capture bookkeeping path."
    )
    return await post_event_message(
        session,
        project_id,
        event_type="operator_decision.summary_absent",
        summary=summary,
        context={
            "session_id": session_id,
            "last_summary_at": last_summary_at,
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
    _schedule_external_notification(
        session, project_id, "DIRECTION_MILESTONE", payload
    )
    return msg


async def post_staged_directions_activated_message(
    session: AsyncSession,
    project_id: uuid.UUID,
    activated: list[dict[str, Any]],
    promoted_version_number: int,
    task_id: Optional[uuid.UUID] = None,
    *,
    terminal_status: str = "promoted",
) -> Optional[ProjectMessage]:
    """Post a HIGH-SIGNAL message when staged directions auto-activate on version open.

    Symmetric counterpart to the staging-IN note/notice: when the version a
    direction was staged behind promotes, the shared activation helper flips it
    back to ``active`` (making it researchable and able to consume VM capacity
    again). This is the one direction lifecycle transition that was previously
    silent — every other transition (auto-pause, auto-retire, churn/burn pause,
    outcome-stalled) posts an operator milestone. ``activated`` is a list of
    ``{"id", "text"}`` dicts for the directions that flipped; classified
    ``high_signal`` so it survives paging and fires an info toast.

    Returns ``None`` (posts nothing) when ``activated`` is empty so callers need
    no guard. Best-effort: the external webhook is fire-and-forget and the
    in-session message rides the caller's commit.
    """
    if not activated:
        return None
    resolved_status = str(terminal_status or "").strip().lower()
    if resolved_status not in {"promoted", "abandoned"}:
        raise ValueError(
            "terminal_status must be either 'promoted' or 'abandoned'"
        )
    texts = [str(d.get("text") or d.get("id") or "?")[:80] for d in activated]
    count = len(activated)
    joined = "; ".join(texts)
    summary = (
        f"{count} staged direction(s) auto-activated after v"
        f"{promoted_version_number} {resolved_status} — staged work resumed: {joined}"
    )
    context: dict[str, Any] = {
        "terminal_version_number": promoted_version_number,
        "terminal_status": resolved_status,
        "count": count,
        "direction_ids": [str(d.get("id")) for d in activated if d.get("id")],
        "direction_texts": texts,
    }
    msg = await post_event_message(
        session,
        project_id,
        event_type="direction_staged_activated",
        summary=summary,
        context=context,
        task_id=task_id,
    )
    _schedule_external_notification(
        session,
        project_id,
        "DIRECTION_STAGED_ACTIVATED",
        {
            "terminal_version_number": promoted_version_number,
            "terminal_status": resolved_status,
            "count": count,
            "direction_ids": context["direction_ids"],
            "direction_texts": texts,
        },
    )
    return msg


async def post_release_version_abandoned_message(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    version_id: uuid.UUID,
    version_number: int,
    successor_version_number: int,
    operator_display: str,
    operator_id: str,
    reason: str | None,
) -> ProjectMessage:
    """Persist the operator-visible, idempotent receipt for an exact cut."""

    summary = (
        f"v{version_number} was abandoned by operator {operator_display}"
        + (f" ({reason})" if reason else "")
        + f". v{successor_version_number} is now open for build."
    )
    return await post_event_message(
        session,
        project_id,
        event_type="release_version_abandoned",
        summary=summary,
        context={
            "version_number": version_number,
            "version_id": str(version_id),
            "reason": reason,
            "operator": operator_display,
            "operator_id": operator_id,
            "next_version_number": successor_version_number,
        },
        idempotency_key=f"release:{version_id}:release_version_abandoned",
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

    # Update per-branch failure tracking in the project's auto_remediation config.
    # Pre-read the Task row outside the lock (pure read, no mutation).
    if result in ("failed", "merged"):
        try:
            from app.task_locking import with_design_doc_locked

            threshold = _AUTO_REMEDIATION_ESCALATION_THRESHOLD
            if result == "failed" and task_id is not None:
                triggering = await session.get(Task, task_id)
                if triggering is not None and (triggering.context or {}).get(
                    "regression_of_task_id"
                ):
                    threshold = _AUTO_REMEDIATION_REGRESSION_ESCALATION_THRESHOLD

            # Mutable cell to capture side-effects from inside the lock closure.
            _escalation: dict = {}
            # Captures the final config for the project_telemetry dual-write.
            _persisted: dict = {}

            def _mutate(doc: dict) -> None:
                ar_config = dict(doc.get("_auto_remediation", {}))
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
                        _escalation["count"] = count
                        _escalation["meta"] = suspension_meta[branch]

                ar_config["branch_failure_counts"] = failure_counts
                ar_config["suspended_branches"] = suspended
                ar_config["suspension_metadata"] = suspension_meta
                doc["_auto_remediation"] = ar_config
                _persisted["config"] = ar_config

            # expire_on_commit=False means an already-identity-mapped Project
            # carries a stale design_doc; expire it so the lock's SELECT FOR
            # UPDATE re-reads the freshly-committed row (matches the canonical
            # pattern in direction_outcomes.py / research_direction_state.py).
            _proj = await session.get(Project, project_id)
            if _proj is not None:
                session.expire(_proj, ["design_doc"])
            await with_design_doc_locked(session, project_id, _mutate)

            if _persisted.get("config") is not None:
                # Dual-write to project_telemetry (design_doc copy kept for
                # rollback compatibility with older serving engines).
                from app.project_telemetry import (
                    set_project_telemetry,
                )

                await set_project_telemetry(
                    session, project_id, "_auto_remediation", _persisted["config"]
                )

            if _escalation:
                count = _escalation["count"]
                meta = _escalation["meta"]
                # Fire escalation message + webhook outside the lock
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
                            "suspended_at": meta["suspended_at"],
                            "recovery_attempts": meta["recovery_attempts"],
                            "severity": "blocker",
                            "manual_intervention_required": True,
                            "summary": summary,
                        },
                    )
                )
                return msg
        except Exception as exc:
            logger.warning(
                "[AUTO-REMEDIATION] Failed to update failure tracking for %s: %s",
                branch,
                exc,
            )

    return msg


# Authoring channels treated as "system-generated" for de-dup purposes. User
# messages, FEEDBACK, and QUESTION are NEVER de-duped on create.
_DEDUPED_SYSTEM_FROM_TYPES: frozenset[str] = frozenset({"system", "agent"})


async def open_system_bug_or_blocker_exists(
    session: AsyncSession,
    project_id: uuid.UUID,
    dedupe_key: str,
) -> Optional[ProjectMessage]:
    """Return an already-open system/agent BUG_REPORT/BLOCKER carrying the same
    ``context["dedupe_key"]``, or ``None`` when no duplicate is open.

    Shared, query-only de-dup primitive for the recurring-condition creators
    that auto-raise a bug/blocker (auto-fix branch escalation, residual-fix
    fingerprint escalation, direction burn/churn pause, …). Each creator stamps
    a stable ``dedupe_key`` into the message ``context`` and consults this helper
    before posting, so one underlying problem maps to at most one OPEN system
    message instead of an ever-growing pile.

    The bug-class eligibility predicate is shared with the convergence layer via
    ``message_convergence.BUG_CLASS_TYPES`` (defined once there). Conservative by
    construction: only ``status == "open"`` messages of a bug-class type authored
    by ``from_type in ("system", "agent")`` are considered. An empty/falsy
    ``dedupe_key`` never matches, so an un-keyed creator stays a byte-identical
    no-op rather than collapsing unrelated messages. Query-only and fail-open:
    does NOT commit (callers own their transaction) and any lookup error returns
    ``None`` so a legitimately new message is never blocked.
    """
    if not dedupe_key:
        return None

    from sqlmodel import select

    from app.message_convergence import BUG_CLASS_TYPES

    try:
        eligible_types = tuple(MessageType(v) for v in BUG_CLASS_TYPES)
        result = await session.execute(
            select(ProjectMessage)
            .where(
                ProjectMessage.project_id == project_id,
                ProjectMessage.status == "open",
                ProjectMessage.message_type.in_(eligible_types),
                ProjectMessage.from_type.in_(tuple(_DEDUPED_SYSTEM_FROM_TYPES)),
                # Push dedupe_key match to the database to avoid a full-table scan
                # in Python; NULL contexts produce NULL from the path accessor and
                # never match, so this is safe with nullable context columns.
                ProjectMessage.context["dedupe_key"].as_string() == dedupe_key,
            )
            .limit(500)
        )
        msg = result.scalars().first()
        if msg is not None:
            return msg
    except Exception as exc:  # fail-open: never block a legitimately new message
        logger.warning(
            "[DEDUPE] open_system_bug_or_blocker_exists lookup failed for %s: %s",
            dedupe_key,
            exc,
        )
    return None


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
    dedupe_key = f"auto_fix_escalated:{branch}"
    existing = await open_system_bug_or_blocker_exists(session, project_id, dedupe_key)
    if existing is not None:
        # Refresh the latest detail on the still-open escalation rather than
        # opening a duplicate BLOCKER for the same branch.
        _ctx = dict(existing.context or {})
        _ctx["consecutive_failures"] = failure_count
        existing.context = _ctx
        existing.content = summary
        session.add(existing)
        return existing
    ctx: dict[str, Any] = {
        "event_type": "auto_fix_escalated",
        "branch": branch,
        "consecutive_failures": failure_count,
        "dedupe_key": dedupe_key,
    }
    ctx = stamp_message_audience(ctx, "auto_fix_escalated")
    msg = await build_project_message(
        session,
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
    with_lock: bool = False,
) -> ResidualRemediationCase:
    """Return the case row for (project_id, fingerprint), creating it if
    missing. The UNIQUE(project_id, fingerprint) added in 0054 means one
    row per fingerprint. Caller is responsible for the surrounding commit.

    When with_lock=True, acquires a row-level write lock to prevent
    concurrent read-modify-write races on counters (e.g., audit_defer_count).
    """
    from sqlmodel import select

    stmt = select(ResidualRemediationCase).where(
        ResidualRemediationCase.project_id == project_id,
        ResidualRemediationCase.fingerprint == fingerprint,
    )
    if with_lock:
        stmt = stmt.with_for_update()

    result = await session.execute(stmt)
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
    case = await _get_or_create_residual_case(session, project_id, fingerprint, with_lock=True)
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
    _schedule_external_notification(
        session,
        project_id,
        "RESIDUAL_FIX_SPAWNED",
        {
            "fingerprint": fingerprint,
            "hit_count": case.hit_count,
            "task_id": str(task_id) if task_id else None,
        },
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
            case = await _get_or_create_residual_case(session, project_id, fingerprint, with_lock=True)
            case.last_spawned_pr_number = pr_number
            case.updated_at = _utcnow()
            session.add(case)
        return msg

    try:
        case = await _get_or_create_residual_case(session, project_id, fingerprint, with_lock=True)
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
    _project = await session.get(Project, task.project_id) if task.project_id else None
    _ci_failure_branch = (task.context or {}).get("ci_failure_branch")
    if _ci_failure_branch:
        branch = _ci_failure_branch
    elif _project:
        branch = get_project_promotion_target(_project)
    else:
        logger.warning(
            "[AUTO-REMEDIATION] post_remediation_result_message: "
            "project not found for task %s (project_id=%s) and no ci_failure_branch "
            "in context — aborting",
            task.id,
            task.project_id,
        )
        raise ValueError(
            f"post_remediation_result_message: project not found for task {task.id} "
            f"and no ci_failure_branch in context"
        )
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
    dedupe_key = f"residual_fix_escalated:{fingerprint}"
    existing = await open_system_bug_or_blocker_exists(session, project_id, dedupe_key)
    if existing is not None:
        # Refresh the still-open escalation instead of opening a duplicate
        # BLOCKER for the same fingerprint.
        _ctx = dict(existing.context or {})
        _ctx["consecutive_failures"] = failure_count
        existing.context = _ctx
        existing.content = summary
        session.add(existing)
        return existing
    ctx: dict[str, Any] = {
        "event_type": "residual_fix_escalated",
        "fingerprint": fingerprint,
        "consecutive_failures": failure_count,
        "dedupe_key": dedupe_key,
    }
    ctx = stamp_message_audience(ctx, "residual_fix_escalated")
    msg = await build_project_message(
        session,
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


async def post_residual_fix_auto_recovered_message(
    session: AsyncSession,
    project_id: uuid.UUID,
    fingerprint: str,
    recovery_attempts: int,
    task_id: Optional[uuid.UUID] = None,
) -> ProjectMessage:
    """Post a NOTE-severity message when a suspended residual fingerprint
    auto-recovers after its exponential-backoff cooldown elapses.

    Paired with the BLOCKER escalation message so the message stream shows
    suspend → auto-recover as a matched pair. Fires RESIDUAL_FIX_UNSUSPENDED
    external webhook (same event type as manual unsuspend, different context).
    """
    recovered_at = datetime.now(timezone.utc).isoformat()
    summary = (
        f"Residual auto-fix re-enabled for fingerprint {fingerprint[:32]}. "
        f"Case auto-recovered after cooldown (attempt {recovery_attempts}). "
        f"Spawn will resume."
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
            "auto_recovered": True,
            "recovery_attempts": recovery_attempts,
        },
        task_id=task_id,
    )
    _schedule_external_notification(
        session,
        project_id,
        "RESIDUAL_FIX_UNSUSPENDED",
        {
            "fingerprint": fingerprint,
            "recovered_at": recovered_at,
            "manual_intervention_required": False,
            "auto_recovered": True,
            "recovery_attempts": recovery_attempts,
            "summary": summary,
        },
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
    ctx = stamp_message_audience(ctx, "residual_fix_audit_deferred")
    msg = await build_project_message(
        session,
        project_id=project_id,
        message_type=MessageType.BLOCKER,
        from_type="system",
        from_task_id=task_id,
        content=summary,
        context=ctx,
    )
    session.add(msg)
    _schedule_external_notification(
        session,
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
    case = await _get_or_create_residual_case(session, project_id, fingerprint, with_lock=True)
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
    ctx = stamp_message_audience(ctx, "remediation_escalated")
    msg = await build_project_message(
        session,
        project_id=project_id,
        message_type=MessageType.BLOCKER,
        from_type="system",
        from_task_id=task_id,
        content=summary,
        context=ctx,
    )
    session.add(msg)
    _schedule_external_notification(
        session,
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
    # Stable key only when we have a direction_id; an un-keyed pause stays a
    # byte-identical no-op (creates as before).
    dedupe_key = f"direction_burn_pause:{direction_id}" if direction_id else ""
    existing = await open_system_bug_or_blocker_exists(session, project_id, dedupe_key)
    if existing is not None:
        return existing
    ctx: dict[str, Any] = {
        "event_type": "direction_burn_pause",
        "direction_text": direction_text[:120],
        "reason": reason,
        "direction_id": direction_id,
        "manual_intervention_required": True,
    }
    ctx = stamp_message_audience(ctx, "direction_burn_pause")
    if dedupe_key:
        ctx["dedupe_key"] = dedupe_key
    if evidence:
        ctx["evidence"] = evidence
    msg = await build_project_message(
        session,
        project_id=project_id,
        message_type=MessageType.BLOCKER,
        from_type="system",
        from_task_id=task_id,
        content=summary,
        context=ctx,
    )
    session.add(msg)
    _schedule_external_notification(
        session,
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
    return msg


async def post_direction_churn_pause_message(
    session: AsyncSession,
    project_id: uuid.UUID,
    direction_text: str,
    reason: str,
    direction_id: Optional[str] = None,
    evidence: Optional[dict[str, Any]] = None,
    task_id: Optional[uuid.UUID] = None,
) -> ProjectMessage:
    """Post a BLOCKER-severity message when a direction is auto-paused by the churn guardrail.

    Unlike the lifetime-burn pause, churn pauses auto-recover after a cooldown period and
    do NOT require manual operator intervention. Still surfaced as BLOCKER so it reaches
    the high-signal feed rather than being digested as routine ops noise.
    """
    ev = evidence or {}
    ev_parts: list[str] = []
    if "non_merge_finished" in ev:
        ev_parts.append(f"non-merging={ev['non_merge_finished']}")
    if "merged" in ev:
        ev_parts.append(f"merged={ev['merged']}")
    if "merge_rate" in ev:
        rate = ev["merge_rate"]
        ev_parts.append(
            f"merge_rate={rate:.0%}"
            if isinstance(rate, float)
            else f"merge_rate={rate}"
        )
    if "window_cycles" in ev:
        ev_parts.append(f"window={ev['window_cycles']} cycles")
    ev_str = f" ({', '.join(ev_parts)})" if ev_parts else ""
    summary = (
        f"Direction auto-paused by churn guardrail — will auto-recover after cooldown: "
        f"'{direction_text[:80]}'{ev_str} — {reason}"
    )
    dedupe_key = f"direction_churn_pause:{direction_id}" if direction_id else ""
    existing = await open_system_bug_or_blocker_exists(session, project_id, dedupe_key)
    if existing is not None:
        return existing
    ctx: dict[str, Any] = {
        "event_type": "direction_churn_pause",
        "direction_text": direction_text[:120],
        "reason": reason,
        "direction_id": direction_id,
    }
    ctx = stamp_message_audience(ctx, "direction_churn_pause")
    if dedupe_key:
        ctx["dedupe_key"] = dedupe_key
    if evidence:
        ctx["evidence"] = evidence
    msg = await build_project_message(
        session,
        project_id=project_id,
        message_type=MessageType.BLOCKER,
        from_type="system",
        from_task_id=task_id,
        content=summary,
        context=ctx,
    )
    session.add(msg)
    _schedule_external_notification(
        session,
        project_id,
        "DIRECTION_CHURN_PAUSE",
        {
            "direction_text": direction_text[:120],
            "reason": reason,
            "direction_id": direction_id,
            "evidence": evidence or {},
            "severity": "blocker",
            "summary": summary,
        },
    )
    return msg


async def post_direction_outcome_stalled_message(
    session: AsyncSession,
    project_id: uuid.UUID,
    direction_text: str,
    evidence: Optional[dict[str, Any]] = None,
    direction_id: Optional[str] = None,
    task_id: Optional[uuid.UUID] = None,
) -> ProjectMessage:
    """Post a HIGH-SIGNAL NOTE when a direction is flagged as outcome-less scaffolding.

    Advisory signal — NOT a pause: the direction keeps merging PRs at a healthy
    rate but its outcome-progress signal has been flat for a sustained run of
    cycles and it has never met its success_evidence. Emitted once per transition
    into the flagged state (the caller dedups via the direction's
    outcome_scaffolding_flagged flag), mirroring the direction burn-pause /
    milestone one-shot pattern. High-signal NOTE so it survives routine paging.
    """
    ev = evidence or {}
    parts: list[str] = []
    if "total_cycles" in ev:
        parts.append(f"cycles={ev['total_cycles']}")
    if "lifetime_merged" in ev:
        parts.append(f"merged={ev['lifetime_merged']}")
    if "lifetime_merge_rate" in ev:
        rate = ev["lifetime_merge_rate"]
        parts.append(
            f"merge_rate={rate:.0%}"
            if isinstance(rate, float)
            else f"merge_rate={rate}"
        )
    if "stall_run_cycles" in ev:
        parts.append(f"no_progress_for={ev['stall_run_cycles']} cycles")
    ev_str = f" ({', '.join(parts)})" if parts else ""
    summary = (
        f"Direction shipping merges without outcome progress: "
        f"'{direction_text[:80]}'{ev_str} — review whether to retire, redirect, "
        f"or redefine its success_evidence."
    )
    context: dict[str, Any] = {
        "direction_text": direction_text[:120],
        "direction_id": direction_id,
    }
    if evidence:
        context["evidence"] = evidence
    return await post_event_message(
        session,
        project_id,
        event_type="direction_outcome_stalled",
        summary=summary,
        context=context,
        task_id=task_id,
    )


async def post_direction_outcome_scaffolding_escalation_message(
    session: AsyncSession,
    project_id: uuid.UUID,
    direction_text: str,
    flagged_cycles: int,
    evidence: Optional[dict[str, Any]] = None,
    direction_id: Optional[str] = None,
    task_id: Optional[uuid.UUID] = None,
) -> ProjectMessage:
    """Post a BLOCKER-severity message when outcome-less scaffolding persists past the escalation threshold.

    Fires once when outcome_scaffolding_flagged_cycles crosses escalation_cycles,
    signalling that the agent/operator has had multiple cycles to act and hasn't.
    Mirrored after post_direction_burn_pause_message — BLOCKER so it surfaces in
    the operator alert feed.
    """
    ev = evidence or {}
    parts: list[str] = []
    if "total_cycles" in ev:
        parts.append(f"cycles={ev['total_cycles']}")
    if "lifetime_merged" in ev:
        parts.append(f"merged={ev['lifetime_merged']}")
    if "stall_run_cycles" in ev:
        parts.append(f"no_progress_for={ev['stall_run_cycles']} cycles")
    parts.append(f"flagged_for={flagged_cycles} cycles")
    ev_str = f" ({', '.join(parts)})" if parts else ""
    summary = (
        f"Direction outcome-scaffolding flag unresolved after {flagged_cycles} cycles: "
        f"'{direction_text[:80]}'{ev_str} — manual retirement or success_evidence update required."
    )
    ctx: dict[str, Any] = {
        "event_type": "direction_outcome_scaffolding_escalation",
        "direction_text": direction_text[:120],
        "direction_id": direction_id,
        "flagged_cycles": flagged_cycles,
        "manual_intervention_required": True,
    }
    ctx = stamp_message_audience(ctx, "direction_outcome_scaffolding_escalation")
    if evidence:
        ctx["evidence"] = evidence
    msg = await build_project_message(
        session,
        project_id=project_id,
        message_type=MessageType.BLOCKER,
        from_type="system",
        from_task_id=task_id,
        content=summary,
        context=ctx,
    )
    session.add(msg)
    _schedule_external_notification(
        session,
        project_id,
        "DIRECTION_OUTCOME_SCAFFOLDING_ESCALATION",
        {
            "direction_text": direction_text[:120],
            "direction_id": direction_id,
            "flagged_cycles": flagged_cycles,
            "evidence": evidence or {},
            "severity": "blocker",
            "manual_intervention_required": True,
            "summary": summary,
        },
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
    *,
    from_type: str = "strategic",
    resident_session_id: Optional[str] = None,
    actor_user_id: Optional[str] = None,
    event_type: str = "feedback_acknowledgment",
    content_override: str | None = None,
    sequence: int | None = None,
) -> ProjectMessage:
    """Create a reply message acknowledging a resolved FEEDBACK message."""
    summary = content_override if content_override is not None else f"Feedback addressed: {resolution}"
    if sequence is None:
        sequence = await lock_project_and_next_message_sequence(session, project_id)
    context = stamp_message_audience(
        {
            "event_type": event_type,
            "message_role": "operator_reply",
            "reply_to": str(original_msg_id),
            "resolution": resolution,
        },
        event_type,
    )
    msg = await build_project_message(
        session,
        project_id=project_id,
        sequence=sequence,
        surface=MessageSurface.CONVERSATION,
        handling_state=MessageHandlingState.RESPONDED,
        handled_by_task_id=task_id,
        reply_to_message_id=original_msg_id,
        message_type=MessageType.NOTE,
        from_type=from_type,
        actor_user_id=actor_user_id,
        from_task_id=task_id,
        content=summary,
        context=context,
    )
    session.add(msg)
    return msg


async def post_question_answer(
    session: AsyncSession,
    project_id: uuid.UUID,
    original_msg_id: uuid.UUID,
    answer: str,
    task_id: Optional[uuid.UUID] = None,
    from_type: str = "strategic",
    resident_session_id: Optional[str] = None,
    actor_user_id: Optional[str] = None,
    content_override: str | None = None,
    sequence: int | None = None,
) -> ProjectMessage:
    """Create a reply message answering a resolved QUESTION message.

    Uses from_type="strategic" by default (NOT "system") so the answer reads as
    a first-class strategist response; pass from_type="user" for operator replies.
    The event_type "question_answer" is a cross-component contract consumed
    by the frontend threading logic — use it verbatim.
    """
    # Route the context through the shared audience gate so the reply carries a
    # fail-closed audience classification instead of an unstamped context dict.
    # ``from_type`` defaults to the operator-reaching "strategic", so an unstamped
    # row here would stay operator-visible even if the registry later reclassifies
    # ``question_answer`` as strategist-internal. Stamping binds visibility to the
    # registry resolution (currently OPERATOR_FYI) regardless of from_type.
    context = stamp_message_audience(
        {
            "event_type": "question_answer",
            "message_role": "operator_reply",
            "reply_to": str(original_msg_id),
            "answer": answer,
        },
        "question_answer",
    )
    if sequence is None:
        sequence = await lock_project_and_next_message_sequence(session, project_id)
    msg = await build_project_message(
        session,
        project_id=project_id,
        sequence=sequence,
        surface=MessageSurface.CONVERSATION,
        handling_state=MessageHandlingState.RESPONDED,
        handled_by_task_id=task_id,
        reply_to_message_id=original_msg_id,
        message_type=MessageType.NOTE,
        from_type=from_type,
        actor_user_id=actor_user_id,
        from_task_id=task_id,
        content=content_override if content_override is not None else f"Answer: {answer}",
        context=context,
        resident_session_id=resident_session_id,
    )
    session.add(msg)
    return msg


async def post_version_calcification_message(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    version_number: int,
    version_id: str,
    qualification_attempts: int,
    blocker: str,
    blocker_label: str,
    age_days: int,
) -> ProjectMessage:
    """Post a calcification alert when a release version is repeatedly stuck.

    Called when a version in the stabilize phase has accumulated enough
    hard-blocker failures to be considered calcifying (promotion is stalled).
    Also fires RELEASE_VERSION_CALCIFYING external webhook so operators can
    route the alert to their incident channel.

    Does NOT commit — the caller owns the transaction.
    """
    summary = (
        f"v{version_number} is calcifying — {qualification_attempts} "
        f"qualification attempt(s) blocked by {blocker_label} over "
        f"{age_days} day(s). Resolve the blocker to restore promotion."
    )
    msg = await post_event_message(
        session,
        project_id,
        event_type="release_calcifying",
        summary=summary,
        context={
            "version_number": version_number,
            "version_id": version_id,
            "qualification_attempts": qualification_attempts,
            "blocker": blocker,
            "age_days": age_days,
        },
    )
    asyncio.ensure_future(
        dispatch_external_notification(
            project_id,
            "RELEASE_VERSION_CALCIFYING",
            {
                "version_number": version_number,
                "version_id": version_id,
                "qualification_attempts": qualification_attempts,
                "blocker": blocker,
                "age_days": age_days,
                "summary": summary,
            },
        )
    )
    return msg


async def post_version_ci_unresolved_stall_message(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    version_number: int,
    version_id: str,
    qualification_attempts: int,
    age_days: int,
) -> ProjectMessage:
    """Post a CI-resolution stall alert when CI state cannot be evaluated.

    Called when a version in the stabilize phase has repeatedly finalized as
    not_promoted with blocker ci_unresolved — meaning CI checks are pending,
    no check-runs exist for the candidate, or GitHub I/O is degraded. This is
    a CI infrastructure or configuration concern, not a quality regression about
    the release candidate itself.

    Does NOT commit — the caller owns the transaction.
    """
    summary = (
        f"v{version_number} CI cannot be resolved — {qualification_attempts} "
        f"qualification attempt(s) could not evaluate CI state (checks pending, "
        f"no check-runs, or GitHub I/O degraded) over {age_days} day(s). "
        f"Investigate CI configuration or GitHub connectivity, not the release candidate."
    )
    msg = await post_event_message(
        session,
        project_id,
        event_type="release_ci_unresolved_stall",
        summary=summary,
        context={
            "version_number": version_number,
            "version_id": version_id,
            "qualification_attempts": qualification_attempts,
            "age_days": age_days,
        },
    )
    asyncio.ensure_future(
        dispatch_external_notification(
            project_id,
            "RELEASE_VERSION_CI_UNRESOLVED_STALL",
            {
                "version_number": version_number,
                "version_id": version_id,
                "qualification_attempts": qualification_attempts,
                "age_days": age_days,
                "summary": summary,
            },
        )
    )
    return msg


async def post_version_build_stalled_message(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    version_number: int,
    version_id: str,
    age_days: int,
    tasks_merged: int,
) -> ProjectMessage:
    """Post a build-stall alert when a release version sits too long in build.

    Called when an open version has been in the ``build`` phase well past a
    healthy window without advancing to ``stabilize`` (``build → stabilize`` is
    agent-driven and has no automatic cadence, so a version can accumulate
    merged tasks in build indefinitely without ever reaching qualify/promote).
    Also fires the RELEASE_VERSION_BUILD_STALLED external webhook so operators
    can route the alert to their incident channel.

    Does NOT commit — the caller owns the transaction.
    """
    summary = (
        f"v{version_number} has been building for {age_days} day(s) with "
        f"{tasks_merged} merged task(s) and has not advanced to stabilize. "
        f"Consider moving it to the stabilize phase to drive it toward "
        f"qualification."
    )
    msg = await post_event_message(
        session,
        project_id,
        event_type="release_build_stalled",
        summary=summary,
        context={
            "version_number": version_number,
            "version_id": version_id,
            "age_days": age_days,
            "tasks_merged": tasks_merged,
        },
    )
    asyncio.ensure_future(
        dispatch_external_notification(
            project_id,
            "RELEASE_VERSION_BUILD_STALLED",
            {
                "version_number": version_number,
                "version_id": version_id,
                "age_days": age_days,
                "tasks_merged": tasks_merged,
                "summary": summary,
            },
        )
    )
    return msg


async def post_version_stabilize_stalled_message(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    version_number: int,
    version_id: str,
    age_days: int,
    tasks_merged: int,
    blocker: str | None = None,
    blocker_class: str | None = None,
    remedy: str | None = None,
) -> ProjectMessage:
    """Post a stabilize-stall alert when a release version sits too long in stabilize.

    Called when an open version has been in the ``stabilize`` phase well past a
    healthy window without making qualification progress (stabilize→qualify is
    agent-driven and has no automatic cadence, so a version can sit in stabilize
    indefinitely without ever reaching qualify/promote).  Also fires the
    RELEASE_VERSION_STABILIZE_STALLED external webhook so operators can route
    the alert to their incident channel.

    Does NOT commit — the caller owns the transaction.
    """
    external_delivery_blocked = (
        blocker == "promotion_disabled" and blocker_class == "external_delivery"
    )
    if external_delivery_blocked:
        summary = (
            f"v{version_number} has been in the stabilize phase for {age_days} "
            f"day(s) with {tasks_merged} merged task(s), but qualification is "
            f"blocked because project promotion is disabled. Enable project "
            f"promotion before requesting qualification."
        )
    else:
        summary = (
            f"v{version_number} has been in the stabilize phase for {age_days} day(s) "
            f"with {tasks_merged} merged task(s) and has not been qualified. "
            f"Consider requesting qualification now or addressing what is preventing "
            f"readiness."
        )
    context = {
        "version_number": version_number,
        "version_id": version_id,
        "age_days": age_days,
        "tasks_merged": tasks_merged,
    }
    if blocker is not None:
        context["blocker"] = blocker
    if blocker_class is not None:
        context["blocker_class"] = blocker_class
    if remedy is not None:
        context["remedy"] = remedy
    msg = await post_event_message(
        session,
        project_id,
        event_type="release_stabilize_stalled",
        summary=summary,
        context=context,
    )
    asyncio.ensure_future(
        dispatch_external_notification(
            project_id,
            "RELEASE_VERSION_STABILIZE_STALLED",
            {
                "version_number": version_number,
                "version_id": version_id,
                "age_days": age_days,
                "tasks_merged": tasks_merged,
                "summary": summary,
                **(
                    {
                        "blocker": blocker,
                        "blocker_class": blocker_class,
                        "remedy": remedy,
                    }
                    if external_delivery_blocked
                    else {}
                ),
            },
        )
    )
    return msg


async def post_version_promotion_readiness_message(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    version_number: int,
    version_id: str,
    verdicts_evaluated: int,
    would_promote_rate: float,
    consecutive_clean_passes: int,
    reason: str,
    validated_precision: float | None = None,
    resolved_outcome_sample: int = 0,
    ground_truth_validated: bool = False,
) -> ProjectMessage:
    """Notify operator when the dry-run track record first reaches promotion-readiness.

    Fires once per not-ready→ready transition while promotion is disabled.
    Enable promotion_enabled in settings to act on this signal.

    ``validated_precision`` / ``resolved_outcome_sample`` / ``ground_truth_validated``
    carry the ground-truth post-promotion precision derived from resolved promoted
    verdicts. When ``ground_truth_validated`` is True the message states the
    measured precision; otherwise it states that no resolved outcome sample exists
    yet (rather than claiming no outcome is ever recorded).
    Does NOT commit — the caller owns the transaction.
    """
    if ground_truth_validated and validated_precision is not None:
        ground_truth_note = (
            f"GROUND TRUTH: validated post-promotion precision "
            f"{validated_precision:.0%} over {resolved_outcome_sample} resolved "
            f"promotion(s)."
        )
    elif resolved_outcome_sample > 0:
        ground_truth_note = (
            f"NOTE: this is the qualifier's own approval rate; only "
            f"{resolved_outcome_sample} post-promotion outcome(s) have resolved "
            f"so far — not yet enough to validate precision."
        )
    else:
        ground_truth_note = (
            "NOTE: this is the qualifier's own approval rate with no resolved "
            "post-promotion outcome yet."
        )
    summary = (
        f"v{version_number} qualifier self-approval streak clears thresholds: "
        f"{would_promote_rate:.0%} self-approval rate ({verdicts_evaluated} dry-run verdict(s)), "
        f"{consecutive_clean_passes} consecutive clean passes. "
        f"{ground_truth_note} "
        f"Enable promotion in settings to unlock automain→main autonomy."
    )
    msg = await post_event_message(
        session,
        project_id,
        event_type="promotion_readiness_reached",
        summary=summary,
        context={
            "version_number": version_number,
            "version_id": version_id,
            "verdicts_evaluated": verdicts_evaluated,
            "would_promote_rate": would_promote_rate,
            "consecutive_clean_passes": consecutive_clean_passes,
            "validated_precision": validated_precision,
            "resolved_outcome_sample": resolved_outcome_sample,
            "ground_truth_validated": ground_truth_validated,
            "reason": reason,
        },
    )
    asyncio.ensure_future(
        dispatch_external_notification(
            project_id,
            "PROMOTION_READINESS_REACHED",
            {
                "version_number": version_number,
                "version_id": version_id,
                "verdicts_evaluated": verdicts_evaluated,
                "would_promote_rate": would_promote_rate,
                "consecutive_clean_passes": consecutive_clean_passes,
                "validated_precision": validated_precision,
                "resolved_outcome_sample": resolved_outcome_sample,
                "ground_truth_validated": ground_truth_validated,
                "reason": reason,
                "summary": summary,
            },
        )
    )
    return msg


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
