"""Pause-aware elapsed-time helpers.

Pipeline timers (CI backstop, queued-age alerts, pre-claim death reaper)
shouldn't fail tasks just because the pipeline was paused. This module
maintains a cumulative counter of pause-time on ``DeploymentState`` so
any timer can subtract pause windows from its elapsed calculation.

Two counters are kept, split by origin:
  * ``total_manual_paused_seconds`` — admin-initiated pauses
  * ``total_auto_paused_seconds``   — auto-pause from external dep failures

For timer math, callers always sum the two via :func:`total_paused_seconds`.
The split is for observability (self-inflicted vs external downtime).

## Usage

At timer start, capture a baseline alongside ``started_at``::

    from app import pause_clock
    state = await session.get(DeploymentState, 1)
    baseline = pause_clock.total_paused_seconds(state)
    review_loop["ci_wait_started"] = utcnow().isoformat()
    review_loop["ci_wait_pause_baseline"] = baseline

Later, compute pause-aware elapsed::

    elapsed = pause_clock.active_elapsed(state, started_at, baseline)

If ``baseline`` is None (legacy timer started before this was deployed),
:func:`active_elapsed` falls back to raw elapsed — preserving prior
behavior for in-flight tasks until they rotate.

At pause/resume transitions, hooks call :func:`mark_paused` and
:func:`accumulate_pause_window`. See ``routers/deploy.py`` and
``deploy_health.py`` for the entry points.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional, Tuple

from app.models import DeploymentState
from app.time_utils import _utcnow

PAUSED_KIND_MANUAL = "manual"
PAUSED_KIND_AUTO = "auto"


async def current_baseline(session) -> float:
    """Async helper: fetch DeploymentState and return current cumulative
    pause-seconds. Use at timer-start sites that don't already have a
    state object in scope. Returns 0.0 if no state row exists.
    """
    state = await session.get(DeploymentState, 1)
    return total_paused_seconds(state) if state else 0.0


def _in_flight_seconds(state: DeploymentState) -> float:
    """Seconds elapsed in the currently-active pause window, or 0 if not paused."""
    if not state.paused_at:
        return 0.0
    paused_at = state.paused_at
    if paused_at.tzinfo is not None:
        paused_at = paused_at.replace(tzinfo=None)
    return max(0.0, (_utcnow() - paused_at).total_seconds())


def total_paused_seconds(state: DeploymentState) -> float:
    """Cumulative pause-seconds including the in-flight window if any.

    This is the value timers consume — sum of both kinds.
    """
    closed = (state.total_manual_paused_seconds or 0.0) + (
        state.total_auto_paused_seconds or 0.0
    )
    return closed + _in_flight_seconds(state)


def total_paused_seconds_breakdown(state: DeploymentState) -> Tuple[float, float]:
    """Return (manual, auto) closed totals. The in-flight window is unattributed
    until resume; observers that need it can read ``state.paused_kind``.
    """
    return (
        state.total_manual_paused_seconds or 0.0,
        state.total_auto_paused_seconds or 0.0,
    )


def active_elapsed(
    state: DeploymentState,
    started_at: datetime,
    baseline_paused: Optional[float],
) -> float:
    """Return elapsed seconds since ``started_at``, excluding pause windows
    that occurred between then and now.

    ``baseline_paused`` is the value of :func:`total_paused_seconds` at the
    time the timer started. ``None`` is treated as legacy/no-adjustment —
    raw wall-clock elapsed is returned (matches pre-PauseClock behavior for
    timers that were started before this module was deployed).
    """
    if started_at.tzinfo is not None:
        started_at = started_at.replace(tzinfo=None)
    raw = (_utcnow() - started_at).total_seconds()
    if baseline_paused is None:
        return max(0.0, raw)
    paused_during = total_paused_seconds(state) - baseline_paused
    return max(0.0, raw - paused_during)


def active_elapsed_seconds(
    state: Optional[DeploymentState],
    started_at: datetime,
    baseline_paused: Optional[float] = None,
) -> float:
    """Seconds since ``started_at`` excluding pause windows — the reaper-side
    analogue of :func:`active_elapsed` for wall-clock stall/timeout reapers
    that penalize a task/cycle for lack of *progress*. A paused pipeline can't
    make progress, so these must not count pause time toward their deadline.

    Three modes, degrading safely:

    * ``state is None`` → raw elapsed (no pause data available — preserves
      pre-PauseClock behavior for callers/tests that don't thread state).
    * ``baseline_paused`` given (the value of :func:`total_paused_seconds` at
      timer start) → EXACT across every pause window since then.
    * ``baseline_paused is None`` but ``state`` present → subtract only the
      currently-OPEN pause window. Safe for the common single-pause case: a
      still-running timer that predates the open pause has its whole overlap
      inside that one window, so active time is never over-counted *during* a
      pause. It cannot retroactively credit already-closed windows for a legacy
      row that carries no baseline — such rows converge to correct once they
      rotate (cycles/qualifications are short-lived) and are never falsely
      reaped while the pipeline is actually paused.
    """
    if started_at.tzinfo is not None:
        started_at = started_at.replace(tzinfo=None)
    raw = (_utcnow() - started_at).total_seconds()
    if state is None:
        return max(0.0, raw)
    if baseline_paused is not None:
        return max(0.0, raw - (total_paused_seconds(state) - baseline_paused))
    return max(0.0, raw - _in_flight_seconds(state))


def mark_paused(state: DeploymentState, kind: str) -> None:
    """Record entry into a pause window. Idempotent: if already paused,
    keeps the existing ``paused_at`` and ``paused_kind`` so we don't
    lose accumulated in-flight time.

    Caller is responsible for setting ``state.status``, ``state.pause_reason``,
    and any pre_pause_max_vms / runtime_max_concurrent_vms snapshotting.
    """
    if kind not in (PAUSED_KIND_MANUAL, PAUSED_KIND_AUTO):
        raise ValueError(f"pause kind must be 'manual' or 'auto', got {kind!r}")
    if state.paused_at is not None:
        return
    state.paused_at = _utcnow()
    state.paused_kind = kind


def accumulate_pause_window(state: DeploymentState) -> None:
    """Close the in-flight pause window: add its duration to the appropriate
    counter and clear ``paused_at``/``paused_kind``. No-op if not paused.

    Call this BEFORE clearing ``state.paused_at`` in resume paths.
    """
    if not state.paused_at:
        return
    delta = _in_flight_seconds(state)
    kind = state.paused_kind or PAUSED_KIND_AUTO
    if kind == PAUSED_KIND_MANUAL:
        state.total_manual_paused_seconds = (
            state.total_manual_paused_seconds or 0.0
        ) + delta
    else:
        state.total_auto_paused_seconds = (
            state.total_auto_paused_seconds or 0.0
        ) + delta
    state.paused_at = None
    state.paused_kind = None
