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
* ``badge_invisible`` — messages with ``status='linked'`` that pass
  ``derive_needs_action()`` but are invisible to the badge SQL that reads only
  ``status='open'``.  Genuinely actionable items silently missed by the badge.
* ``info_alert_noise`` — undismissed INFO-severity, operator-visible Alerts in
  the alert channel.  Bookkeeping notices inflating the operator alert badge.

Pure read-only: this module never writes to ``ProjectMessage`` or ``Alert``.
"""

from __future__ import annotations

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


class LivenessResult(NamedTuple):
    """One per-project result from a liveness check run."""

    project_id: str
    status: str  # "ok" | "flagged" | "skipped"
    forward_leaked: int
    badge_invisible: int
    info_alert_noise: int
    detail: str


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

        def _status_str(m: ProjectMessage) -> str:
            s = m.status
            return s.value if hasattr(s, "value") else str(s)

        # Forward: non-fyi operator-visible messages that fail derive_needs_action.
        # operator-fyi is a legitimate informational audience — not a leak.
        forward_leaked_msgs = [
            m for m in messages
            if not derive_needs_action(m) and _audience_str(m) != OPERATOR_FYI
        ]

        # Reverse: needs-action messages with linked status invisible to badge SQL.
        badge_invisible_msgs = [
            m for m in messages
            if derive_needs_action(m) and _status_str(m) == LINKED_STATUS
        ]

        n_forward = len(forward_leaked_msgs)
        n_badge = len(badge_invisible_msgs)
        delta = n_forward + n_badge + info_alert_count

        parts: List[str] = []
        if n_forward:
            parts.append(
                f"{n_forward}/{len(messages)} non-fyi operator-visible "
                "message(s) not genuinely needs-action (forward leak)"
            )
        if n_badge:
            parts.append(
                f"{n_badge} needs-action message(s) with linked status "
                "invisible to badge SQL (reverse drift)"
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
                "correctly handled (no forward leaks, no badge-invisible "
                "items, no INFO alert noise)"
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
