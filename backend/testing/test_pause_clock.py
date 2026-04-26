"""Tests for app.pause_clock — pause-aware elapsed-time helpers.

The pure-function helpers (total_paused_seconds, active_elapsed,
mark_paused, accumulate_pause_window) operate on a DeploymentState
object directly and can be tested without a live DB.
"""
from datetime import datetime, timedelta

import pytest

from app import pause_clock
from app.models import DeploymentState


def _state(**overrides):
    """Build a fresh DeploymentState for in-memory tests."""
    defaults = dict(
        id=1,
        total_manual_paused_seconds=0.0,
        total_auto_paused_seconds=0.0,
        paused_at=None,
        paused_kind=None,
    )
    defaults.update(overrides)
    return DeploymentState(**defaults)


# ── total_paused_seconds + breakdown ──────────────────────────────────────────

def test_total_zero_on_fresh_state():
    s = _state()
    assert pause_clock.total_paused_seconds(s) == 0.0
    assert pause_clock.total_paused_seconds_breakdown(s) == (0.0, 0.0)


def test_total_sums_both_kinds():
    s = _state(total_manual_paused_seconds=30.0, total_auto_paused_seconds=70.0)
    assert pause_clock.total_paused_seconds(s) == 100.0
    assert pause_clock.total_paused_seconds_breakdown(s) == (30.0, 70.0)


def test_total_includes_in_flight_window():
    """While paused, total includes the live window even before resume."""
    paused_at = datetime.utcnow() - timedelta(seconds=20)
    s = _state(
        total_auto_paused_seconds=50.0,
        paused_at=paused_at,
        paused_kind=pause_clock.PAUSED_KIND_AUTO,
    )
    total = pause_clock.total_paused_seconds(s)
    # 50 closed + ~20 in-flight; small slack for test latency
    assert 69.0 <= total <= 72.0
    # Breakdown reflects only closed totals (in-flight is unattributed until resume)
    assert pause_clock.total_paused_seconds_breakdown(s) == (0.0, 50.0)


# ── active_elapsed ────────────────────────────────────────────────────────────

def test_active_elapsed_no_baseline_returns_raw():
    """Legacy timers with no stashed baseline get raw wall-clock elapsed."""
    started = datetime.utcnow() - timedelta(seconds=100)
    s = _state(total_auto_paused_seconds=999.0)  # huge counter — must be ignored
    elapsed = pause_clock.active_elapsed(s, started, baseline_paused=None)
    assert 99.0 <= elapsed <= 102.0


def test_active_elapsed_baseline_zero_no_pauses():
    """No pauses since timer started — active equals raw."""
    started = datetime.utcnow() - timedelta(seconds=60)
    s = _state()
    elapsed = pause_clock.active_elapsed(s, started, baseline_paused=0.0)
    assert 59.0 <= elapsed <= 62.0


def test_active_elapsed_subtracts_pause_window():
    """A 30s pause inside a 100s timer window leaves 70s of active elapsed."""
    started = datetime.utcnow() - timedelta(seconds=100)
    s = _state(total_auto_paused_seconds=30.0)
    # Timer started before any pauses, so baseline = 0; pauses since = 30.
    elapsed = pause_clock.active_elapsed(s, started, baseline_paused=0.0)
    assert 69.0 <= elapsed <= 72.0


def test_active_elapsed_baseline_excludes_pre_start_pauses():
    """Pauses that happened BEFORE the timer started must not be subtracted."""
    started = datetime.utcnow() - timedelta(seconds=60)
    # 100s of pre-existing pause time, then timer started, then 0 more pauses.
    # baseline captures the 100s — paused_during after start is 0.
    s = _state(total_manual_paused_seconds=100.0)
    elapsed = pause_clock.active_elapsed(s, started, baseline_paused=100.0)
    assert 59.0 <= elapsed <= 62.0


def test_active_elapsed_clamps_to_zero():
    """If total subtracted exceeds raw elapsed, result is clamped to 0."""
    started = datetime.utcnow() - timedelta(seconds=10)
    s = _state(total_auto_paused_seconds=999.0)
    elapsed = pause_clock.active_elapsed(s, started, baseline_paused=0.0)
    assert elapsed == 0.0


def test_active_elapsed_during_in_flight_pause():
    """Timer started before pause; pause is still in flight; elapsed excludes
    the in-flight portion."""
    started = datetime.utcnow() - timedelta(seconds=100)
    pause_started = datetime.utcnow() - timedelta(seconds=40)
    s = _state(
        paused_at=pause_started,
        paused_kind=pause_clock.PAUSED_KIND_MANUAL,
    )
    # Baseline at start was 0; current total = 0 closed + ~40 in-flight = ~40
    elapsed = pause_clock.active_elapsed(s, started, baseline_paused=0.0)
    # raw 100 - 40 in-flight = ~60; allow slack for test scheduling
    assert 58.0 <= elapsed <= 62.0


# ── mark_paused ───────────────────────────────────────────────────────────────

def test_mark_paused_sets_fields():
    s = _state()
    pause_clock.mark_paused(s, pause_clock.PAUSED_KIND_MANUAL)
    assert s.paused_at is not None
    assert s.paused_kind == "manual"


def test_mark_paused_idempotent_when_already_paused():
    """Re-calling mark_paused must not reset paused_at — would lose in-flight time."""
    earlier = datetime.utcnow() - timedelta(seconds=300)
    s = _state(paused_at=earlier, paused_kind=pause_clock.PAUSED_KIND_AUTO)
    pause_clock.mark_paused(s, pause_clock.PAUSED_KIND_MANUAL)
    # paused_at unchanged — still the original auto-pause start
    assert s.paused_at == earlier
    assert s.paused_kind == "auto"


def test_mark_paused_rejects_unknown_kind():
    s = _state()
    with pytest.raises(ValueError):
        pause_clock.mark_paused(s, "weird")


# ── accumulate_pause_window ───────────────────────────────────────────────────

def test_accumulate_noop_when_not_paused():
    s = _state(total_manual_paused_seconds=10.0, total_auto_paused_seconds=20.0)
    pause_clock.accumulate_pause_window(s)
    assert s.total_manual_paused_seconds == 10.0
    assert s.total_auto_paused_seconds == 20.0


def test_accumulate_routes_manual_window_to_manual_counter():
    paused_at = datetime.utcnow() - timedelta(seconds=15)
    s = _state(paused_at=paused_at, paused_kind=pause_clock.PAUSED_KIND_MANUAL)
    pause_clock.accumulate_pause_window(s)
    assert s.paused_at is None
    assert s.paused_kind is None
    assert 14.0 <= s.total_manual_paused_seconds <= 17.0
    assert s.total_auto_paused_seconds == 0.0


def test_accumulate_routes_auto_window_to_auto_counter():
    paused_at = datetime.utcnow() - timedelta(seconds=25)
    s = _state(paused_at=paused_at, paused_kind=pause_clock.PAUSED_KIND_AUTO)
    pause_clock.accumulate_pause_window(s)
    assert s.paused_at is None
    assert s.paused_kind is None
    assert s.total_manual_paused_seconds == 0.0
    assert 24.0 <= s.total_auto_paused_seconds <= 27.0


def test_accumulate_preserves_prior_totals():
    """Adding a new window doesn't overwrite previously accumulated time."""
    paused_at = datetime.utcnow() - timedelta(seconds=10)
    s = _state(
        total_manual_paused_seconds=100.0,
        total_auto_paused_seconds=200.0,
        paused_at=paused_at,
        paused_kind=pause_clock.PAUSED_KIND_MANUAL,
    )
    pause_clock.accumulate_pause_window(s)
    assert 109.0 <= s.total_manual_paused_seconds <= 112.0
    assert s.total_auto_paused_seconds == 200.0


# ── End-to-end: pause → measure → resume → measure ────────────────────────────

def test_full_cycle_active_elapsed_excludes_completed_pause():
    """Simulate: start timer, pause for ~5s, resume, check active_elapsed
    excludes the pause window."""
    started = datetime.utcnow() - timedelta(seconds=30)
    s = _state()
    baseline = pause_clock.total_paused_seconds(s)
    assert baseline == 0.0

    # Enter a manual pause that started 20s ago, ended 5s ago — i.e. lasted 15s.
    s.paused_at = datetime.utcnow() - timedelta(seconds=20)
    s.paused_kind = pause_clock.PAUSED_KIND_MANUAL
    # Then jump forward 15s in pause-time, then resume:
    # accumulate_pause_window uses real wall-clock, so simulate by setting
    # paused_at backward and immediately accumulating.
    pause_clock.accumulate_pause_window(s)
    assert s.paused_at is None
    # Total pause-time is ~20s
    assert 19.0 <= s.total_manual_paused_seconds <= 22.0

    # Active elapsed: 30s raw - 20s paused = 10s
    elapsed = pause_clock.active_elapsed(s, started, baseline_paused=baseline)
    assert 8.0 <= elapsed <= 12.0
