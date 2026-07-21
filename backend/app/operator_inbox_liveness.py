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
from datetime import datetime
from typing import Any, Deque, Dict, List, NamedTuple

from sqlalchemy import func
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

# ── Persistence / escalation constants ────────────────────────────────────

_PERSISTENCE_THRESHOLD = 3
"""Number of consecutive hourly flagged ticks required to treat a leak as persistent."""

_DEDUPE_KEY_PREFIX = "operator_inbox_leak:"
"""Stable dedupe-key prefix for once-per-episode strategist escalation."""

_ESCALATION_EVENT_TYPE = "operator_inbox_leak_persistent"
"""Event type registered as STRATEGIST_INTERNAL — never reaches the operator channel."""


class LivenessResult(NamedTuple):
    """One per-project result from a liveness check run."""

    project_id: str
    status: str  # "ok" | "flagged" | "skipped"
    forward_leaked: int
    badge_invisible: int
    info_alert_noise: int
    detail: str


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
    episode boundaries when a project goes [flagged×3, clean, flagged].
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
    global _total_forward_leaked, _total_badge_invisible, _total_info_alert_noise

    if now is None:
        now = _utcnow()

    if not _force and _last_liveness_check is not None:
        elapsed = (now - _last_liveness_check).total_seconds()
        if elapsed < _LIVENESS_INTERVAL_SECONDS:
            return None

    _last_liveness_check = now

    from app.message_audience import (
        OPERATOR_FYI,
        operator_visible_alert_filter,
        operator_visible_audience_filter,
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
                    operator_visible_audience_filter(ProjectMessage.context),
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

        if not messages and info_alert_count == 0:
            results.append(
                LivenessResult(
                    project_id=pid,
                    status="skipped",
                    forward_leaked=0,
                    badge_invisible=0,
                    info_alert_noise=0,
                    detail="no open/linked operator-visible messages or INFO alerts",
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

        # badge_invisible is always 0: the badge SQL now uses
        # status IN ('open', 'linked') for needs-action queries, so linked
        # BLOCKER/QUESTION messages are correctly included in the count.
        n_forward = len(forward_leaked_msgs)
        n_badge = 0
        delta = n_forward + n_badge + info_alert_count

        parts: List[str] = []
        if n_forward:
            parts.append(
                f"{n_forward}/{len(messages)} non-fyi operator-visible "
                "message(s) not genuinely needs-action (forward leak)"
            )
        if info_alert_count:
            parts.append(
                f"{info_alert_count} INFO alert(s) in operator-visible "
                "alert channel (non-actionable alert noise)"
            )

        status = "flagged" if delta > 0 else "ok"
        detail = (
            "; ".join(parts)
            if parts
            else (
                f"all {len(messages)} open/linked operator-visible message(s) "
                "correctly handled (no forward leaks, no INFO alert noise)"
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
            )
        )

        _total_forward_leaked += n_forward
        _total_badge_invisible += n_badge
        _total_info_alert_noise += info_alert_count

    _total_ticks += 1
    tick_forward = sum(r.forward_leaked for r in results)
    tick_badge = sum(r.badge_invisible for r in results)
    tick_alerts = sum(r.info_alert_noise for r in results)
    flagged = [r for r in results if r.status == "flagged"]
    tick_summary: Dict[str, Any] = {
        "tick": _total_ticks,
        "projects": len(results),
        "flagged": len(flagged),
        "forward_leaked": tick_forward,
        "badge_invisible": tick_badge,
        "info_alert_noise": tick_alerts,
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
        "recent_ticks": list(_observability_ring),
    }


def _reset_liveness_for_tests() -> None:
    """Test helper — clear process-local counters and cadence gate."""
    global _last_liveness_check, _total_ticks
    global _total_forward_leaked, _total_badge_invisible, _total_info_alert_noise
    _last_liveness_check = None
    _total_ticks = 0
    _total_forward_leaked = 0
    _total_badge_invisible = 0
    _total_info_alert_noise = 0
    _observability_ring.clear()
