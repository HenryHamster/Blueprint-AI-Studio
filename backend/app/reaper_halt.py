"""Reaper halt circuit-breaker state, liveness helpers, and alert functions.

Extracted from reaper.py so the halt/observability cluster is independent of
orphan detection and the main reaper service loop.
"""

import logging
import uuid

from sqlalchemy import insert
from sqlmodel import select

from app.database import AsyncSessionLocal
from app.models import Project
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)


# Cycle-level circuit breaker. After N consecutive whole-cycle exceptions the
# reaper enters HALTED state, stops cycling, and posts an ERROR alert. Resets
# automatically on process restart (in-memory); tunable via runtime_config key
# "reaper_cycle_halt_threshold".
_REAPER_CYCLE_HALT_THRESHOLD: int = 5
# Half-open probe limit: after this many consecutive failed probes the reaper
# escalates to a hard halt that requires operator reset (like the original behavior).
# Tunable via runtime_config key "reaper_half_open_probe_limit".
_REAPER_HALF_OPEN_PROBE_LIMIT: int = 3
_reaper_halt_state: dict = {
    "halted": False,
    "halt_timestamp": None,
    "last_error": None,
    "crash_count": 0,
    # hard_halted=True → terminal; requires operator reset (not auto-recovered).
    # hard_halted=False + halted=True → soft halt; auto-recovers after cooldown.
    "hard_halted": False,
    "half_open_failures": 0,  # consecutive failed half-open probes
}


def get_reaper_stats() -> dict:
    """Snapshot of reaper liveness for /metrics and admin observability.

    ``last_cycle_age_seconds`` is the wall-clock seconds since the last
    fully-successful cycle. -1 means the reaper has never completed a cycle
    (process just started, or every cycle has crashed). Alerts should fire
    if this exceeds the reaper sleep interval by a meaningful margin
    (e.g. >300s — five expected cycles missed).
    """
    import app.reaper as _reaper_mod

    age_seconds: float | int = -1
    if _reaper_mod._reaper_last_cycle_completed_at is not None:
        age_seconds = max(
            0, (_utcnow() - _reaper_mod._reaper_last_cycle_completed_at).total_seconds()
        )
    # In-cycle progress age: seconds since the last _mark_reaper_progress()
    # heartbeat, -1 if never marked. This is the freshest liveness signal under
    # a long cycle (the watchdog judges on it before falling back to cycle age).
    progress_age_seconds: float | int = -1
    if _reaper_mod._reaper_last_progress is not None:
        progress_age_seconds = max(
            0, (_utcnow() - _reaper_mod._reaper_last_progress).total_seconds()
        )
    return {
        "cycles_completed_total": _reaper_mod._reaper_cycles_completed_count,
        "last_cycle_age_seconds": age_seconds,
        "last_progress_age_seconds": progress_age_seconds,
        "halted": _reaper_halt_state["halted"],
        "halt_timestamp": _reaper_halt_state["halt_timestamp"].isoformat()
        if _reaper_halt_state["halt_timestamp"]
        else None,
        "halt_crash_count": _reaper_halt_state["crash_count"],
        "hard_halted": _reaper_halt_state.get("hard_halted", False),
        "half_open_failures": _reaper_halt_state.get("half_open_failures", 0),
    }


def is_reaper_halted() -> bool:
    """Liveness probe: True iff the reaper has self-halted after a crash-loop."""
    return _reaper_halt_state["halted"]


def _mark_reaper_progress() -> None:
    """Heartbeat that the reaper worker is actively progressing within a cycle.

    Liveness (the lifecycle watchdog) is gated on the freshness of this
    timestamp, not on full-cycle completion, so a legitimately long cycle (large
    orphan backlog → many sequential GitHub/VM calls) is not mistaken for a hung
    process and killed. Kept cheap: a single timestamp write, no I/O. Mirrors
    orchestrator._mark_orchestrator_progress (PR #6678)."""
    import app.reaper as _reaper_mod

    _reaper_mod._reaper_last_progress = _utcnow()


async def _post_reaper_halt_alert(crash_count: int, last_error: str) -> None:
    """Post an ERROR-severity alert to every project when the reaper self-halts."""
    try:
        from app.models import Alert, AlertSeverity

        async with AsyncSessionLocal() as s:
            # Bound the fan-out scan and fan-out insert: a single capped SELECT
            # plus one bulk INSERT instead of an unbounded full-table scan and
            # one ORM add() per project in the error-recovery hot path.
            proj_ids = (await s.execute(select(Project.id).limit(1000))).scalars().all()
            if not proj_ids:
                return
            message = (
                f"Reaper crash-looped {crash_count}x and self-halted. "
                f"Last error: {last_error[:300]}. "
                "No new reap cycles until process restart or operator reset."
            )
            await s.execute(
                insert(Alert),
                [
                    {
                        "id": uuid.uuid4(),
                        "project_id": pid,
                        "severity": AlertSeverity.ERROR,
                        "title": "Reaper service HALTED after crash-loop",
                        "message": message,
                    }
                    for pid in proj_ids
                ],
            )
            await s.commit()
    except Exception:
        logger.exception("[REAPER] Failed to post halt alert")


async def _post_reaper_recovery_alert(probe_failures: int) -> None:
    """Post an INFO alert to every project when the reaper autonomously recovers."""
    try:
        from app.models import Alert, AlertSeverity

        async with AsyncSessionLocal() as s:
            # Bound the fan-out scan and fan-out insert (see _post_reaper_halt_alert).
            proj_ids = (await s.execute(select(Project.id).limit(1000))).scalars().all()
            if not proj_ids:
                return
            message = (
                f"Reaper self-recovered after {probe_failures} failed "
                "probe(s). Normal reap cycles have resumed."
            )
            await s.execute(
                insert(Alert),
                [
                    {
                        "id": uuid.uuid4(),
                        "project_id": pid,
                        "severity": AlertSeverity.INFO,
                        "title": "Reaper service recovered autonomously",
                        "message": message,
                    }
                    for pid in proj_ids
                ],
            )
            await s.commit()
    except Exception:
        logger.exception("[REAPER] Failed to post recovery alert")
