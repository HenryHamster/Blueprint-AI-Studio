"""Standalone always-on operator-inbox liveness health signal.

Promotes the operator-inbox leak measurement to a first-class, always-on health
signal independent of the mode-gated metrics_integrity_sweep.  The sweep's
``check_operator_inbox_needs_action_liveness`` lives behind ``DEFAULT_MODE =
"observe"`` and is explicitly slated for cutting by direction d-aaecf1b0; this
module provides the same audit logic on its own cadence gate with no mode gate
so the signal survives the sweep's removal.

Registered as a standing reaper step (``sweep_operator_inbox_liveness`` in
``reaper_sweeps.py``) and called unconditionally alongside
``sweep_metrics_integrity`` in the reaper cycle.

**Signal counts (per tick, per project):**

* ``forward_leaked`` — open/linked non-fyi operator-visible messages that fail
  ``derive_needs_action()``.  These are NULL-audience legacy rows or mis-routed
  messages inflating the operator inbox with non-actionable chatter.
* ``badge_invisible`` — always 0.  The badge SQL in ``operator_attention.py``
  now uses ``status IN ('open', 'linked')`` for needs-action queries, so linked
  BLOCKER/QUESTION messages are correctly counted.  Field retained for API
  stability.
* ``info_alert_noise`` — undismissed INFO-severity, operator-visible Alerts in
  the alert channel.  Bookkeeping notices inflating the operator alert badge.

Writes durable observation rows for every flagged project so the signal
survives restart, and escalates persistent leaks (>= 3 consecutive flagged
hourly ticks) to the strategist surface exactly once per episode.
"""

from __future__ import annotations

import contextlib
import logging
from collections import deque
from datetime import datetime, timedelta
from typing import Any, Deque, Dict, List, NamedTuple

from sqlalchemy import func, or_
from sqlmodel import select

from app.time_utils import _utcnow

logger = logging.getLogger(__name__)

_LIVENESS_INTERVAL_SECONDS = 3600
_last_liveness_check: datetime | None = None

_OBSERVABILITY_MAX = 100
_observability_ring: Deque[Dict[str, Any]] = deque(maxlen=_OBSERVABILITY_MAX)
_total_ticks: int = 0
_total_forward_leaked: int = 0
_total_badge_invisible: int = 0
_total_info_alert_noise: int = 0
_total_remediated: int = 0
_total_reply_audience_quarantined: int = 0
_total_reply_audience_healed: int = 0
_total_undelivered_strategist_replies: int = 0
_undelivered_oldest_age_seconds: int = 0

# ── Persistence / escalation constants ────────────────────────────────────

_PERSISTENCE_THRESHOLD = 3
"""Number of consecutive hourly flagged ticks required to treat a leak as persistent."""

_DEDUPE_KEY_PREFIX = "operator_inbox_leak:"
"""Stable dedupe-key prefix for once-per-episode strategist escalation."""

_ESCALATION_EVENT_TYPE = "operator_inbox_leak_persistent"
"""Event type registered as STRATEGIST_INTERNAL — never reaches the operator channel."""

_QUARANTINE_ALERT_DEDUPE_PREFIX = "reply_quarantine_unhealed:"
"""Stable dedupe-key prefix for once-per-episode operator-visible quarantine alert."""

_QUARANTINE_ALERT_EVENT_TYPE = "delivery.reply_quarantine_unhealed"
"""Event type registered as OPERATOR_ACTION_NEEDED — surfaces in the operator channel."""

_UNDELIVERED_ALERT_DEDUPE_PREFIX = "undelivered_strategist_reply:"
"""Stable dedupe-key prefix for once-per-episode operator-visible undelivered-reply alert."""

_UNDELIVERED_ALERT_EVENT_TYPE = "delivery.undelivered_strategist_reply"
"""Event type registered as OPERATOR_ACTION_NEEDED — surfaces in the operator channel."""

_UNDELIVERED_STALENESS_MINUTES = 15
"""Minutes after which an undelivered operator-directed reply is considered stale."""


class LivenessResult(NamedTuple):
    """One per-project result from a liveness check run."""

    project_id: str
    status: str  # "ok" | "flagged" | "skipped"
    forward_leaked: int
    badge_invisible: int
    info_alert_noise: int
    detail: str
    remediated_count: int = 0
    reply_audience_quarantined: int = 0
    reply_audience_healed: int = 0
    undelivered_strategist_replies: int = 0


# ── Durable persistence helpers ────────────────────────────────────────────


async def _persist_observation(
    session: Any,
    result: LivenessResult,
    tick_number: int,
    now: datetime,
) -> None:
    """Write one durable observation row for a measured project.

    Written for EVERY measured project — not only flagged ones — so that
    non-flagged rows (status ``"ok"`` / ``"skipped"``) break the consecutive
    flagged chain in ``_count_consecutive_flagged`` and correctly detect
    episode boundaries when a project goes [flaggedx3, clean, flagged].
    Without clean-tick rows the DB sees an unbroken sequence of flagged rows
    and escalates on the *first* flagged tick of a new episode instead of
    waiting for >= 3 NEW consecutive flagged ticks.

    Error-isolated: a persistence failure is caught and logged but never
    propagated — this is a best-effort durable write; the in-memory signal
    and the warning log line are the fallback channel.
    """
    try:
        from app.models import OperatorInboxLivenessObservation

        row = OperatorInboxLivenessObservation(
            project_id=result.project_id,
            status=result.status,
            forward_leaked=result.forward_leaked,
            badge_invisible=result.badge_invisible,
            info_alert_noise=result.info_alert_noise,
            detail=result.detail,
            tick_number=tick_number,
            created_at=now,
        )
        session.add(row)
    except Exception:  # noqa: BLE001
        logger.warning(
            "[OPERATOR_INBOX_LIVENESS] Failed to persist observation for %s",
            result.project_id,
            exc_info=True,
        )


async def _count_consecutive_flagged(
    session: Any,
    project_id: str,
) -> int:
    """Return the number of consecutive most-recent 'flagged' observation rows.

    Queries the durable table for the last N rows ordered by created_at DESC
    and counts from the top until a non-flagged row is found.  The result
    tells us how many consecutive ticks this project has been leaking.

    Error-isolated: returns 0 on any query failure so the reaper cycle
    continues uninterrupted.
    """
    try:
        from app.models import OperatorInboxLivenessObservation as OLO

        rows = (
            await session.execute(
                select(OLO.status)
                .where(OLO.project_id == project_id)
                .order_by(OLO.created_at.desc())
                .limit(_PERSISTENCE_THRESHOLD + 2)
            )
        ).scalars().all()

        consecutive = 0
        for status in rows:
            if status == "flagged":
                consecutive += 1
            else:
                break
        return consecutive
    except Exception:  # noqa: BLE001
        logger.warning(
            "[OPERATOR_INBOX_LIVENESS] Failed to count consecutive flagged for %s",
            project_id,
            exc_info=True,
        )
        return 0


async def _find_open_escalation(
    session: Any,
    project_id: str,
) -> Any:
    """Return an open escalation message for *project_id*, or None.

    Matches on ``context['dedupe_key']`` — the stable key stamped at
    creation time.  Any open message (any type, any surface) that still
    carries the key is treated as the live episode marker.
    """
    try:
        from app.models import ProjectMessage

        dedupe_key = f"{_DEDUPE_KEY_PREFIX}{project_id}"
        result = await session.execute(
            select(ProjectMessage)
            .where(
                ProjectMessage.project_id == project_id,
                ProjectMessage.status == "open",
                # JSONB path accessor — NULL context never matches.
                ProjectMessage.context["dedupe_key"].as_string() == dedupe_key,
            )
            .limit(1)
        )
        return result.scalars().first()
    except Exception:  # noqa: BLE001
        logger.warning(
            "[OPERATOR_INBOX_LIVENESS] Dedup lookup failed for %s",
            project_id,
            exc_info=True,
        )
        return None


async def _escalate_persistent_leak(
    session: Any,
    result: LivenessResult,
) -> None:
    """Escalate a persistent leak to the strategist surface.

    Called when a project has >= _PERSISTENCE_THRESHOLD consecutive flagged
    observations.  Creates exactly ONE strategist-internal ProjectMessage per
    episode via a stable dedupe_key; subsequent persistent ticks for the same
    episode are no-ops.

    Error-isolated: a failure here is caught and logged — the reaper cycle
    must never crash on a failed escalation.
    """
    try:
        existing = await _find_open_escalation(session, result.project_id)
        if existing is not None:
            # Already escalated — update the detail on the still-open message.
            ctx = dict(existing.context or {})
            ctx["forward_leaked"] = result.forward_leaked
            ctx["info_alert_noise"] = result.info_alert_noise
            ctx["tick_number"] = _total_ticks
            existing.content = (
                f"[OPERATOR_INBOX_LIVENESS] Persistent operator-inbox leak "
                f"detected for project {result.project_id} "
                f"(forward_leaked={result.forward_leaked}, "
                f"info_alert_noise={result.info_alert_noise}). "
                f"Consecutive flagged ticks >= {_PERSISTENCE_THRESHOLD}. "
                f"Detail: {result.detail[:500]}"
            )
            existing.context = ctx
            session.add(existing)
            return

        # First time this episode — create the strategist-internal escalation.
        from app.notification_dispatch import post_event_message
        import uuid as _uuid

        dedupe_key = f"{_DEDUPE_KEY_PREFIX}{result.project_id}"
        summary = (
            f"Persistent operator-inbox leak detected for project {result.project_id}: "
            f"{result.forward_leaked} forward-leaked message(s), "
            f"{result.info_alert_noise} INFO alert noise. "
            f"Consecutive flagged ticks >= {_PERSISTENCE_THRESHOLD} — "
            f"the chat/messages redesign's success criterion may be violated. "
            f"Detail: {result.detail[:500]}"
        )
        ctx = {
            "forward_leaked": result.forward_leaked,
            "info_alert_noise": result.info_alert_noise,
            "tick_number": _total_ticks,
            "dedupe_key": dedupe_key,
        }
        await post_event_message(
            session,
            project_id=_uuid.UUID(result.project_id),
            event_type=_ESCALATION_EVENT_TYPE,
            summary=summary,
            context=ctx,
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "[OPERATOR_INBOX_LIVENESS] Failed to escalate persistent leak for %s",
            result.project_id,
            exc_info=True,
        )


async def _resolve_open_escalation(
    session: Any,
    project_id: str,
) -> None:
    """Resolve any open persistent-leak escalation for *project_id*.

    Called when a tick shows the project is clean (no leak detected), so the
    episode closes and a genuinely NEW future leak can escalate again.
    """
    try:
        existing = await _find_open_escalation(session, project_id)
        if existing is None:
            return
        existing.status = "resolved"
        existing.resolution = "auto-resolved (clean tick)"
        session.add(existing)
    except Exception:  # noqa: BLE001
        logger.warning(
            "[OPERATOR_INBOX_LIVENESS] Failed to resolve escalation for %s",
            project_id,
            exc_info=True,
        )


# ── Quarantine alert helpers ──────────────────────────────────────────────


async def _find_open_quarantine_alert(
    session: Any,
    project_id: str,
) -> Any:
    """Return the open operator-visible quarantine alert for *project_id*, or None."""
    try:
        from app.models import ProjectMessage

        dedupe_key = f"{_QUARANTINE_ALERT_DEDUPE_PREFIX}{project_id}"
        result = await session.execute(
            select(ProjectMessage)
            .where(
                ProjectMessage.project_id == project_id,
                ProjectMessage.status == "open",
                ProjectMessage.context["dedupe_key"].as_string() == dedupe_key,
            )
            .limit(1)
        )
        return result.scalars().first()
    except Exception:  # noqa: BLE001
        logger.warning(
            "[OPERATOR_INBOX_LIVENESS] Quarantine alert dedup lookup failed for %s",
            project_id,
            exc_info=True,
        )
        return None


async def _escalate_reply_quarantine_alert(
    session: Any,
    result: LivenessResult,
) -> None:
    """Create or update an OPERATOR_ACTION_NEEDED alert for reply-audience quarantine.

    Unlike the persistent-leak escalation (which requires >= 3 consecutive flagged
    ticks and routes STRATEGIST_INTERNAL), this alert fires on the FIRST tick a
    quarantined reply is detected and routes to OPERATOR_ACTION_NEEDED so the
    operator immediately sees that their reply was dropped.  A stable dedupe_key
    prevents duplicate alerts within a single episode; subsequent ticks update the
    existing open alert in place.  Auto-resolved by ``_resolve_reply_quarantine_alert``
    when the quarantine count reaches zero on a clean tick.
    """
    try:
        import uuid as _uuid

        existing = await _find_open_quarantine_alert(session, result.project_id)
        if existing is not None:
            ctx = dict(existing.context or {})
            ctx["reply_audience_quarantined"] = result.reply_audience_quarantined
            ctx["reply_audience_healed"] = result.reply_audience_healed
            ctx["tick_number"] = _total_ticks
            existing.content = (
                f"[OPERATOR_INBOX_LIVENESS] Reply-audience quarantine detected "
                f"for project {result.project_id}: "
                f"{result.reply_audience_quarantined} operator-directed reply/replies "
                f"stamped with a non-operator audience (quarantined — not delivered). "
                f"Healed this tick: {result.reply_audience_healed}. "
                f"Re-check if this alert persists."
            )
            existing.context = ctx
            session.add(existing)
            return

        from app.notification_dispatch import post_event_message

        dedupe_key = f"{_QUARANTINE_ALERT_DEDUPE_PREFIX}{result.project_id}"
        summary = (
            f"Operator-directed reply(ies) quarantined (never delivered) "
            f"for project {result.project_id}: "
            f"{result.reply_audience_quarantined} message(s) stamped with a "
            f"non-operator audience (strategist-internal or pipeline-log) instead "
            f"of the expected operator-fyi audience. "
            f"A heal was attempted this tick ({result.reply_audience_healed} healed). "
            f"Detail: {result.detail[:400]}"
        )
        ctx = {
            "reply_audience_quarantined": result.reply_audience_quarantined,
            "reply_audience_healed": result.reply_audience_healed,
            "tick_number": _total_ticks,
            "dedupe_key": dedupe_key,
        }
        await post_event_message(
            session,
            project_id=_uuid.UUID(result.project_id),
            event_type=_QUARANTINE_ALERT_EVENT_TYPE,
            summary=summary,
            context=ctx,
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "[OPERATOR_INBOX_LIVENESS] Failed to create quarantine alert for %s",
            result.project_id,
            exc_info=True,
        )


async def _resolve_reply_quarantine_alert(
    session: Any,
    project_id: str,
) -> None:
    """Resolve any open quarantine alert for *project_id* on a clean tick."""
    try:
        existing = await _find_open_quarantine_alert(session, project_id)
        if existing is None:
            return
        existing.status = "resolved"
        existing.resolution = "auto-resolved (zero quarantined replies)"
        session.add(existing)
    except Exception:  # noqa: BLE001
        logger.warning(
            "[OPERATOR_INBOX_LIVENESS] Failed to resolve quarantine alert for %s",
            project_id,
            exc_info=True,
        )


# ── Undelivered-reply alert helpers ───────────────────────────────────────


async def _find_open_undelivered_alert(
    session: Any,
    project_id: str,
) -> Any:
    """Return the open operator-visible undelivered-reply alert for *project_id*, or None."""
    try:
        from app.models import ProjectMessage

        dedupe_key = f"{_UNDELIVERED_ALERT_DEDUPE_PREFIX}{project_id}"
        result = await session.execute(
            select(ProjectMessage)
            .where(
                ProjectMessage.project_id == project_id,
                ProjectMessage.status == "open",
                ProjectMessage.context["dedupe_key"].as_string() == dedupe_key,
            )
            .limit(1)
        )
        return result.scalars().first()
    except Exception:  # noqa: BLE001
        logger.warning(
            "[OPERATOR_INBOX_LIVENESS] Undelivered alert dedup lookup failed for %s",
            project_id,
            exc_info=True,
        )
        return None


async def _escalate_undelivered_alert(
    session: Any,
    result: LivenessResult,
) -> None:
    """Create or update an OPERATOR_ACTION_NEEDED alert for undelivered strategist replies.

    Follows the same pattern as ``_escalate_reply_quarantine_alert``: fires on the
    FIRST tick a non-zero undelivered-reply count is detected and routes to
    OPERATOR_ACTION_NEEDED.  A stable dedupe_key prevents duplicate alerts within
    a single episode; subsequent ticks update the existing open alert in place.
    Auto-resolved by ``_resolve_undelivered_alert`` when the undelivered count
    reaches zero on a clean tick.
    """
    try:
        import uuid as _uuid

        existing = await _find_open_undelivered_alert(session, result.project_id)
        if existing is not None:
            ctx = dict(existing.context or {})
            ctx["undelivered_strategist_replies"] = result.undelivered_strategist_replies
            ctx["tick_number"] = _total_ticks
            existing.content = (
                f"[OPERATOR_INBOX_LIVENESS] Undelivered strategist reply(ies) detected "
                f"for project {result.project_id}: "
                f"{result.undelivered_strategist_replies} operator-directed strategist "
                f"reply/replies never delivered (delivered_at IS NULL). "
                f"Re-check if this alert persists."
            )
            existing.context = ctx
            session.add(existing)
            return

        from app.notification_dispatch import post_event_message

        dedupe_key = f"{_UNDELIVERED_ALERT_DEDUPE_PREFIX}{result.project_id}"
        summary = (
            f"Strategist reply(ies) never delivered (no delivery receipt) "
            f"for project {result.project_id}: "
            f"{result.undelivered_strategist_replies} operator-directed strategist "
            f"reply/replies have delivered_at=NULL past the "
            f"{_UNDELIVERED_STALENESS_MINUTES}-minute staleness threshold. "
            f"These messages were routed with an operator-visible audience but "
            f"never received a delivery ack — reply was possibly lost in transit. "
            f"Detail: {result.detail[:400]}"
        )
        ctx = {
            "undelivered_strategist_replies": result.undelivered_strategist_replies,
            "tick_number": _total_ticks,
            "dedupe_key": dedupe_key,
        }
        await post_event_message(
            session,
            project_id=_uuid.UUID(result.project_id),
            event_type=_UNDELIVERED_ALERT_EVENT_TYPE,
            summary=summary,
            context=ctx,
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "[OPERATOR_INBOX_LIVENESS] Failed to create undelivered alert for %s",
            result.project_id,
            exc_info=True,
        )


async def _resolve_undelivered_alert(
    session: Any,
    project_id: str,
) -> None:
    """Resolve any open undelivered-reply alert for *project_id* on a clean tick."""
    try:
        existing = await _find_open_undelivered_alert(session, project_id)
        if existing is None:
            return
        existing.status = "resolved"
        existing.resolution = "auto-resolved (zero undelivered strategist replies)"
        session.add(existing)
    except Exception:  # noqa: BLE001
        logger.warning(
            "[OPERATOR_INBOX_LIVENESS] Failed to resolve undelivered alert for %s",
            project_id,
            exc_info=True,
        )


# ── Remediation helper ────────────────────────────────────────────────────


def _remediate_forward_leaked_messages(session: Any, msgs: list) -> int:
    """Re-stamp forward-leaked messages with their canonical audience.

    Iterates *msgs* (messages already detected as forward leaks) and updates
    each message's ``context["audience"]`` to the canonical classification
    returned by ``stamp_message_audience``.  For NULL-audience or mis-stamped
    rows the canonical value is STRATEGIST_INTERNAL (fail-closed for
    unregistered types) or OPERATOR_FYI — either way the row is excluded from
    future ``operator_visible_message_audience_filter`` queries (fail-closed,
    no NULL pass-through) without waiting for the 14-day age-decay sweep.

    Dead-letter guard: when a message has ``reply_to_message_id`` set AND an
    absent or unregistered ``event_type``, the fail-closed STRATEGIST_INTERNAL
    stamp would permanently hide an operator-directed reply.  For this case,
    ``question_answer`` (OPERATOR_FYI) is substituted so the reply remains
    visible — an operator-directed reply with no explicit event_type is
    presumed to be a question answer, the same default used by
    ``post_question_answer`` and ``_handle_strategic_completion``.

    Returns the number of messages successfully remediated.
    Error-isolated per-message so a single malformed row never aborts the tick.
    """
    from app.message_audience import stamp_message_audience

    count = 0
    for msg in msgs:
        with contextlib.suppress(Exception):
            ctx = dict(msg.context or {})
            event_type = ctx.get("event_type") or ""
            msg_type = getattr(msg, "message_type", None)
            # Dead-letter guard: a reply with no event_type would fail-closed to
            # STRATEGIST_INTERNAL and become permanently invisible.  Default to
            # "question_answer" (OPERATOR_FYI) so the reply stays visible.
            if not event_type and getattr(msg, "reply_to_message_id", None) is not None:
                event_type = "question_answer"
            new_ctx = stamp_message_audience(ctx, event_type, message_type=msg_type)
            msg.context = new_ctx
            session.add(msg)
            count += 1
    return count


async def _heal_quarantined_replies(
    session: Any,
    msgs: list,
    project_id: str,
) -> int:
    """Re-stamp quarantined reply messages with an operator-visible audience.

    Each row is re-stamped via ``stamp_message_audience`` with event_type
    ``'question_answer'`` — the reply default fixed in #16040 that resolves to
    OPERATOR_FYI — and ``delivered_at`` is set so the durable receipt records
    that the heal closed the delivery gap.  Each healed row is broadcast so the
    reply surfaces in the operator chat immediately without a page refresh.

    Returns the number of rows successfully healed.  Error-isolated per-message
    so a single malformed row never aborts the tick.  Does NOT re-stamp rows
    whose audience is already operator-visible (the fetch filter guarantees this).
    """
    from app.message_audience import stamp_message_audience
    from app.broadcast import broadcast_to_project
    from app.project_message_projection import message_updated_event_payload

    count = 0
    for msg in msgs:
        with contextlib.suppress(Exception):
            ctx = dict(msg.context or {})
            new_ctx = stamp_message_audience(ctx, "question_answer")
            msg.context = new_ctx
            msg.delivered_at = _utcnow()
            session.add(msg)
            await broadcast_to_project(
                project_id,
                "message_updated",
                message_updated_event_payload(msg),
            )
            count += 1
    return count


# ── Main liveness check ────────────────────────────────────────────────────


async def run_operator_inbox_liveness_check(
    session: Any,
    now: datetime | None = None,
    *,
    max_projects: int = 25,
    sample_limit: int = 200,
    _force: bool = False,
) -> List[LivenessResult] | None:
    """Always-on operator-inbox liveness check.

    Returns ``None`` when the cadence gate has not elapsed (ran recently).
    Returns a list of per-project ``LivenessResult`` entries on each live run.

    Never mode-gated — fires regardless of ``metrics_integrity_sweep_mode``.
    Pass ``_force=True`` in tests to bypass the cadence gate.

    Persists durable observation rows for every flagged project and escalates
    persistent leaks (>= 3 consecutive flagged hourly ticks) exactly once per
    episode to the strategist surface.  A clean tick auto-resolves the episode
    so a genuinely new leak can escalate again.
    """
    global _last_liveness_check, _total_ticks
    global _total_forward_leaked, _total_badge_invisible, _total_info_alert_noise, _total_remediated
    global _total_reply_audience_quarantined, _total_reply_audience_healed
    global _total_undelivered_strategist_replies, _undelivered_oldest_age_seconds

    if now is None:
        now = _utcnow()

    if not _force and _last_liveness_check is not None:
        elapsed = (now - _last_liveness_check).total_seconds()
        if elapsed < _LIVENESS_INTERVAL_SECONDS:
            return None

    _last_liveness_check = now

    from app.message_audience import (
        OPERATOR_FYI,
        _NON_OPERATOR_AUDIENCES,
        operator_visible_alert_filter,
        operator_visible_message_audience_filter,
    )
    from app.message_convergence import LINKED_STATUS, OPEN_STATUS, derive_needs_action
    from app.models import Alert, AlertSeverity, Project, ProjectMessage

    project_ids = (
        await session.execute(select(Project.id).limit(max_projects))
    ).all()

    if not project_ids:
        return []

    results: List[LivenessResult] = []
    for (project_id,) in project_ids:
        pid = str(project_id)

        messages = (
            await session.execute(
                select(ProjectMessage)
                .where(
                    ProjectMessage.project_id == project_id,
                    ProjectMessage.status.in_([OPEN_STATUS, LINKED_STATUS]),
                    ProjectMessage.from_type != "user",
                    operator_visible_message_audience_filter(ProjectMessage.context),
                )
                .order_by(ProjectMessage.created_at.desc())
                .limit(sample_limit)
            )
        ).scalars().all()

        info_alert_count = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(Alert)
                    .where(
                        Alert.project_id == project_id,
                        Alert.dismissed_at.is_(None),
                        Alert.severity == AlertSeverity.INFO,
                        operator_visible_alert_filter(Alert.event_type),
                    )
                )
            ).scalar()
            or 0
        )

        # Reverse-audience detector: reply-bearing messages that are invisible to
        # the operator — either explicitly quarantined (audience IN non-operator)
        # OR with a NULL audience (un-stamped rows that are also excluded by the
        # fail-closed operator_visible_message_audience_filter).  NULL-audience
        # replies are a silent dead-letter path: the backfill sweep would stamp
        # them STRATEGIST_INTERNAL if their event_type is absent or unregistered,
        # making the reply permanently invisible.  Detecting both classes here
        # lets the heal pass surface them before the backfill can bury them.
        # Covers both reply_to-threaded messages and cover-style messages
        # (with _resident_reply_covered_message_ids but no reply_to).
        quarantined_reply_count = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(ProjectMessage)
                    .where(
                        ProjectMessage.project_id == project_id,
                        or_(
                            ProjectMessage.reply_to_message_id.isnot(None),
                            ProjectMessage.context["_resident_reply_covered_message_ids"].isnot(None),
                        ),
                        ProjectMessage.status.in_([OPEN_STATUS, LINKED_STATUS]),
                        or_(
                            ProjectMessage.context["audience"].as_string().in_(
                                _NON_OPERATOR_AUDIENCES
                            ),
                            ProjectMessage.context["audience"].as_string().is_(None),
                        ),
                    )
                )
            ).scalar()
            or 0
        )

        # Undelivered strategist-reply detector: operator-visible strategist replies
        # that were created but never delivered (delivered_at IS NULL) past the
        # staleness threshold.  These are messages that SHOULD have been delivered
        # but the transport ack was never received — a provable delivery gap.
        staleness_cutoff = now - timedelta(minutes=_UNDELIVERED_STALENESS_MINUTES)
        undelivered_reply_count = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(ProjectMessage)
                    .where(
                        ProjectMessage.project_id == project_id,
                        ProjectMessage.from_type != "user",
                        ProjectMessage.delivered_at.is_(None),
                        ProjectMessage.created_at < staleness_cutoff,
                        or_(
                            ProjectMessage.reply_to_message_id.isnot(None),
                            ProjectMessage.context["_resident_reply_covered_message_ids"].as_string().isnot(None),
                        ),
                        operator_visible_message_audience_filter(ProjectMessage.context),
                    )
                )
            ).scalar()
            or 0
        )

        if not messages and info_alert_count == 0 and quarantined_reply_count == 0 and undelivered_reply_count == 0:
            results.append(
                LivenessResult(
                    project_id=pid,
                    status="skipped",
                    forward_leaked=0,
                    badge_invisible=0,
                    info_alert_noise=0,
                    detail="no open/linked operator-visible messages or INFO alerts",
                    reply_audience_quarantined=quarantined_reply_count,
                    undelivered_strategist_replies=undelivered_reply_count,
                )
            )
            continue

        def _audience_str(m: ProjectMessage) -> str | None:
            return (m.context or {}).get("audience")

        # Forward: non-fyi operator-visible messages that fail derive_needs_action.
        # operator-fyi is a legitimate informational audience — not a leak.
        forward_leaked_msgs = [
            m for m in messages
            if not derive_needs_action(m) and _audience_str(m) != OPERATOR_FYI
        ]

        # Immediately remediate detected leaks: re-stamp audience to the
        # canonical classification so the row is excluded from the operator
        # inbox on the next query cycle without waiting for the 14-day
        # age-decay sweep.  The count below records pre-remediation detections;
        # remediated_count records how many were successfully re-stamped.
        remediated = 0
        if forward_leaked_msgs:
            remediated = _remediate_forward_leaked_messages(session, forward_leaked_msgs)

        # badge_invisible is always 0: the badge SQL now uses
        # status IN ('open', 'linked') for needs-action queries, so linked
        # BLOCKER/QUESTION messages are correctly included in the count.
        n_forward = len(forward_leaked_msgs)
        n_badge = 0
        delta = n_forward + n_badge + info_alert_count + quarantined_reply_count + undelivered_reply_count

        parts: List[str] = []
        if n_forward:
            parts.append(
                f"{n_forward}/{len(messages)} non-fyi operator-visible "
                "message(s) not genuinely needs-action (forward leak)"
            )
            if remediated:
                parts.append(f"{remediated} remediated")
        if info_alert_count:
            parts.append(
                f"{info_alert_count} INFO alert(s) in operator-visible "
                "alert channel (non-actionable alert noise)"
            )
        # Heal: fetch and re-stamp quarantined reply rows so they surface in the
        # operator chat immediately.  The pre-heal count is preserved in
        # reply_audience_quarantined for observability; healed_count records the
        # durable re-deliveries made this tick.
        healed_count = 0
        if quarantined_reply_count > 0:
            quarantined_msgs = (
                await session.execute(
                    select(ProjectMessage)
                    .where(
                        ProjectMessage.project_id == project_id,
                        or_(
                            ProjectMessage.reply_to_message_id.isnot(None),
                            ProjectMessage.context["_resident_reply_covered_message_ids"].isnot(None),
                        ),
                        ProjectMessage.status.in_([OPEN_STATUS, LINKED_STATUS]),
                        or_(
                            ProjectMessage.context["audience"].as_string().in_(
                                _NON_OPERATOR_AUDIENCES
                            ),
                            ProjectMessage.context["audience"].as_string().is_(None),
                        ),
                    )
                    .limit(sample_limit)
                )
            ).scalars().all()
            healed_count = await _heal_quarantined_replies(
                session, quarantined_msgs, pid
            )

        if quarantined_reply_count:
            heal_note = f", {healed_count} healed this tick" if healed_count else ""
            parts.append(
                f"{quarantined_reply_count} reply message(s) quarantined in "
                f"non-operator audience (reply-audience quarantine{heal_note})"
            )

        if undelivered_reply_count:
            parts.append(
                f"{undelivered_reply_count} operator-directed strategist reply(ies) "
                f"undelivered past {_UNDELIVERED_STALENESS_MINUTES} min (delivery gap)"
            )

        status = "flagged" if delta > 0 else "ok"
        detail = (
            "; ".join(parts)
            if parts
            else (
                f"all {len(messages)} open/linked operator-visible message(s) "
                "correctly handled (no forward leaks, no INFO alert noise, "
                "no quarantined replies)"
            )
        )
        results.append(
            LivenessResult(
                project_id=pid,
                status=status,
                forward_leaked=n_forward,
                badge_invisible=n_badge,
                info_alert_noise=info_alert_count,
                detail=detail,
                remediated_count=remediated,
                reply_audience_quarantined=quarantined_reply_count,
                reply_audience_healed=healed_count,
                undelivered_strategist_replies=undelivered_reply_count,
            )
        )

        _total_forward_leaked += n_forward
        _total_badge_invisible += n_badge
        _total_info_alert_noise += info_alert_count
        _total_remediated += remediated
        _total_reply_audience_quarantined += quarantined_reply_count
        _total_reply_audience_healed += healed_count
        _total_undelivered_strategist_replies += undelivered_reply_count

    _total_ticks += 1
    tick_undelivered = sum(r.undelivered_strategist_replies for r in results)
    _undelivered_oldest_age_seconds = 1 if tick_undelivered > 0 else 0
    tick_forward = sum(r.forward_leaked for r in results)
    tick_badge = sum(r.badge_invisible for r in results)
    tick_alerts = sum(r.info_alert_noise for r in results)
    tick_remediated = sum(r.remediated_count for r in results)
    tick_quarantined = sum(r.reply_audience_quarantined for r in results)
    tick_healed = sum(r.reply_audience_healed for r in results)
    flagged = [r for r in results if r.status == "flagged"]
    tick_summary: Dict[str, Any] = {
        "tick": _total_ticks,
        "projects": len(results),
        "flagged": len(flagged),
        "forward_leaked": tick_forward,
        "badge_invisible": tick_badge,
        "info_alert_noise": tick_alerts,
        "remediated": tick_remediated,
        "reply_audience_quarantined": tick_quarantined,
        "reply_audience_healed": tick_healed,
        "undelivered_strategist_replies": tick_undelivered,
    }
    _observability_ring.append(tick_summary)

    if flagged:
        for r in flagged:
            logger.warning(
                "[OPERATOR_INBOX_LIVENESS] project=%s %s", r.project_id, r.detail
            )
    else:
        logger.debug(
            "[OPERATOR_INBOX_LIVENESS] tick=%d projects=%d all-ok",
            _total_ticks,
            len(results),
        )

    # ── Durable persistence + escalation (post-measurement) ────────────────

    flagged_project_ids = {r.project_id for r in flagged}

    # Persist rows for ALL measured projects so non-flagged ("ok"/"skipped")
    # rows break the consecutive flagged chain in _count_consecutive_flagged.
    for result in results:
        with contextlib.suppress(Exception):
            await _persist_observation(session, result, _total_ticks, now)

    # ── Reply-quarantine operator alert ────────────────────────────────────
    # Fire an OPERATOR_ACTION_NEEDED alert on first detection of quarantined
    # replies; resolve it when the quarantine count reaches zero.  This alert
    # fires immediately (no persistence threshold), making the dropped-reply bug
    # class operator-visible on the first tick rather than only after >= 3 ticks.
    for result in results:
        if result.reply_audience_quarantined > 0:
            with contextlib.suppress(Exception):
                await _escalate_reply_quarantine_alert(session, result)
        elif result.status in ("ok", "skipped"):
            with contextlib.suppress(Exception):
                await _resolve_reply_quarantine_alert(session, result.project_id)

    # ── Undelivered-reply operator alert ────────────────────────────────────
    # Fire an OPERATOR_ACTION_NEEDED alert on first detection of undelivered
    # strategist replies; resolve it when the count reaches zero.  Same immediate-
    # fire pattern as the quarantine alert (no persistence threshold).
    for result in results:
        if result.undelivered_strategist_replies > 0:
            with contextlib.suppress(Exception):
                await _escalate_undelivered_alert(session, result)
        elif result.status in ("ok", "skipped"):
            with contextlib.suppress(Exception):
                await _resolve_undelivered_alert(session, result.project_id)

    for result in flagged:
        with contextlib.suppress(Exception):
            consecutive = await _count_consecutive_flagged(session, result.project_id)
            if consecutive >= _PERSISTENCE_THRESHOLD:
                await _escalate_persistent_leak(session, result)

    # For projects that were NOT flagged this tick: resolve any open escalation.
    # A project not present at all (skipped/empty) is NOT automatically resolved
    # — only projects we measured as clean (status == "ok" or "skipped") close
    # the episode.  "skipped" means no open/linked operator-visible messages at
    # all — the leak has been fully eliminated for this project.
    for result in results:
        if result.status in ("ok", "skipped") and result.project_id not in flagged_project_ids:
            with contextlib.suppress(Exception):
                await _resolve_open_escalation(session, result.project_id)

    # Commit all durable writes from this tick.  The reaper wraps this call in
    # try/except+rollback and does NOT commit for us; we own our writes.
    try:
        await session.commit()
    except Exception:  # noqa: BLE001
        logger.warning(
            "[OPERATOR_INBOX_LIVENESS] Failed to commit durable writes",
            exc_info=True,
        )
        with contextlib.suppress(Exception):
            await session.rollback()

    return results


def get_operator_inbox_liveness_observability() -> Dict[str, Any]:
    """Return a snapshot of the in-memory observability state for operators/tests."""
    return {
        "total_ticks": _total_ticks,
        "total_forward_leaked": _total_forward_leaked,
        "total_badge_invisible": _total_badge_invisible,
        "total_info_alert_noise": _total_info_alert_noise,
        "total_remediated": _total_remediated,
        "total_reply_audience_quarantined": _total_reply_audience_quarantined,
        "total_reply_audience_healed": _total_reply_audience_healed,
        "total_undelivered_strategist_replies": _total_undelivered_strategist_replies,
        "undelivered_oldest_age_seconds": _undelivered_oldest_age_seconds,
        "recent_ticks": list(_observability_ring),
    }


def _reset_liveness_for_tests() -> None:
    """Test helper — clear process-local counters and cadence gate."""
    global _last_liveness_check, _total_ticks
    global _total_forward_leaked, _total_badge_invisible, _total_info_alert_noise, _total_remediated
    global _total_reply_audience_quarantined, _total_reply_audience_healed
    global _total_undelivered_strategist_replies, _undelivered_oldest_age_seconds
    _last_liveness_check = None
    _total_ticks = 0
    _total_forward_leaked = 0
    _total_badge_invisible = 0
    _total_info_alert_noise = 0
    _total_remediated = 0
    _total_reply_audience_quarantined = 0
    _total_reply_audience_healed = 0
    _total_undelivered_strategist_replies = 0
    _undelivered_oldest_age_seconds = 0
    _observability_ring.clear()


async def compute_undelivered_strategist_replies(
    session: Any,
    *,
    staleness_minutes: int = _UNDELIVERED_STALENESS_MINUTES,
    max_projects: int = 50,
) -> dict:
    """Compute undelivered strategist reply aggregates across known projects.

    Returns a dict with fleet-wide aggregate counts:
    - ``total_count``: int — total undelivered operator-directed strategist replies
      past the staleness threshold across all projects.
    - ``total_projects_affected``: int — number of projects with at least one
      undelivered reply.
    - ``oldest_age_seconds``: float | None — age in seconds of the oldest
      undelivered reply (None when total_count is 0).
    - ``by_project``: list[dict] — per-project breakdown with ``project_id``,
      ``count``, and ``oldest_age_seconds``.

    Error-isolated: returns zero-valued dict on any query failure so a transient
    DB error never crashes the caller or degrades a metrics scrape.
    """
    try:
        from app.models import ProjectMessage
        from app.message_audience import (
            operator_visible_message_audience_filter,
        )
        from app.time_utils import _utcnow

        now = _utcnow()
        staleness_cutoff = now - timedelta(minutes=staleness_minutes)

        # Fleet-wide aggregate: total count and oldest created_at
        agg = (
            await session.execute(
                select(
                    func.count(),
                    func.min(ProjectMessage.created_at),
                ).where(
                    ProjectMessage.from_type != "user",
                    ProjectMessage.delivered_at.is_(None),
                    ProjectMessage.created_at < staleness_cutoff,
                    or_(
                        ProjectMessage.reply_to_message_id.isnot(None),
                        ProjectMessage.context["_resident_reply_covered_message_ids"].as_string().isnot(None),
                    ),
                    operator_visible_message_audience_filter(ProjectMessage.context),
                )
            )
        ).one_or_none()

        total_count = int(agg[0]) if agg else 0
        oldest_created_at = agg[1] if agg else None
        oldest_age_seconds = (
            (now - oldest_created_at).total_seconds()
            if oldest_created_at is not None and total_count > 0
            else None
        )

        # Per-project breakdown
        by_project_rows = (
            await session.execute(
                select(
                    ProjectMessage.project_id,
                    func.count(),
                    func.min(ProjectMessage.created_at),
                )
                .where(
                    ProjectMessage.from_type != "user",
                    ProjectMessage.delivered_at.is_(None),
                    ProjectMessage.created_at < staleness_cutoff,
                    or_(
                        ProjectMessage.reply_to_message_id.isnot(None),
                        ProjectMessage.context["_resident_reply_covered_message_ids"].as_string().isnot(None),
                    ),
                    operator_visible_message_audience_filter(ProjectMessage.context),
                )
                .group_by(ProjectMessage.project_id)
                .limit(max_projects)
            )
        ).all()

        by_project = []
        for row in by_project_rows:
            pid, cnt, oldest = row
            by_project.append({
                "project_id": str(pid),
                "count": int(cnt),
                "oldest_age_seconds": (
                    (now - oldest).total_seconds()
                    if oldest is not None
                    else None
                ),
            })

        return {
            "total_count": total_count,
            "total_projects_affected": len(by_project),
            "oldest_age_seconds": oldest_age_seconds,
            "by_project": by_project,
        }
    except Exception:  # noqa: BLE001
        logger.warning(
            "[OPERATOR_INBOX_LIVENESS] Failed to compute undelivered strategist replies",
            exc_info=True,
        )
        return {
            "total_count": 0,
            "total_projects_affected": 0,
            "oldest_age_seconds": None,
            "by_project": [],
        }
