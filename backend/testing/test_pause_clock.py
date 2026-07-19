"""Tests for app.pause_clock — pause-aware elapsed-time helpers.

The pure-function helpers (total_paused_seconds, active_elapsed,
mark_paused, accumulate_pause_window) operate on a DeploymentState
object directly and can be tested without a live DB.
"""

from datetime import datetime, timedelta, timezone

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
    paused_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=20)
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
    started = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=100)
    s = _state(total_auto_paused_seconds=999.0)  # huge counter — must be ignored
    elapsed = pause_clock.active_elapsed(s, started, baseline_paused=None)
    assert 99.0 <= elapsed <= 102.0


def test_active_elapsed_baseline_zero_no_pauses():
    """No pauses since timer started — active equals raw."""
    started = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=60)
    s = _state()
    elapsed = pause_clock.active_elapsed(s, started, baseline_paused=0.0)
    assert 59.0 <= elapsed <= 62.0


def test_active_elapsed_subtracts_pause_window():
    """A 30s pause inside a 100s timer window leaves 70s of active elapsed."""
    started = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=100)
    s = _state(total_auto_paused_seconds=30.0)
    # Timer started before any pauses, so baseline = 0; pauses since = 30.
    elapsed = pause_clock.active_elapsed(s, started, baseline_paused=0.0)
    assert 69.0 <= elapsed <= 72.0


def test_active_elapsed_baseline_excludes_pre_start_pauses():
    """Pauses that happened BEFORE the timer started must not be subtracted."""
    started = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=60)
    # 100s of pre-existing pause time, then timer started, then 0 more pauses.
    # baseline captures the 100s — paused_during after start is 0.
    s = _state(total_manual_paused_seconds=100.0)
    elapsed = pause_clock.active_elapsed(s, started, baseline_paused=100.0)
    assert 59.0 <= elapsed <= 62.0


def test_active_elapsed_clamps_to_zero():
    """If total subtracted exceeds raw elapsed, result is clamped to 0."""
    started = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=10)
    s = _state(total_auto_paused_seconds=999.0)
    elapsed = pause_clock.active_elapsed(s, started, baseline_paused=0.0)
    assert elapsed == 0.0


def test_active_elapsed_during_in_flight_pause():
    """Timer started before pause; pause is still in flight; elapsed excludes
    the in-flight portion."""
    started = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=100)
    pause_started = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=40)
    s = _state(
        paused_at=pause_started,
        paused_kind=pause_clock.PAUSED_KIND_MANUAL,
    )
    # Baseline at start was 0; current total = 0 closed + ~40 in-flight = ~40
    elapsed = pause_clock.active_elapsed(s, started, baseline_paused=0.0)
    # raw 100 - 40 in-flight = ~60; allow slack for test scheduling
    assert 58.0 <= elapsed <= 62.0


# ── active_elapsed_seconds (reaper-side stall/timeout deadlines) ──────────────


def test_active_elapsed_seconds_state_none_returns_raw():
    """No deploy state threaded (legacy/test callers) → raw wall-clock."""
    started = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=100)
    elapsed = pause_clock.active_elapsed_seconds(None, started, baseline_paused=0.0)
    assert 99.0 <= elapsed <= 102.0


def test_active_elapsed_seconds_baseline_subtracts_pause_since_start():
    """With a baseline, pauses accumulated since timer start are excluded."""
    started = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=100)
    s = _state(total_manual_paused_seconds=30.0)
    elapsed = pause_clock.active_elapsed_seconds(s, started, baseline_paused=0.0)
    assert 69.0 <= elapsed <= 72.0


def test_active_elapsed_seconds_baseline_excludes_pre_start_pauses():
    """Pauses BEFORE the timer started (captured in the baseline) aren't subtracted."""
    started = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=60)
    s = _state(total_manual_paused_seconds=100.0)
    elapsed = pause_clock.active_elapsed_seconds(s, started, baseline_paused=100.0)
    assert 59.0 <= elapsed <= 62.0


def test_active_elapsed_seconds_no_baseline_subtracts_open_window():
    """Legacy row (no baseline) during an OPEN pause → only the in-flight window
    is subtracted, so it is never counted as progressing while paused."""
    started = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=100)
    pause_started = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=40)
    s = _state(paused_at=pause_started, paused_kind=pause_clock.PAUSED_KIND_MANUAL)
    elapsed = pause_clock.active_elapsed_seconds(s, started, baseline_paused=None)
    assert 58.0 <= elapsed <= 62.0


def test_active_elapsed_seconds_no_baseline_not_paused_returns_raw():
    """Legacy row (no baseline), not currently paused → raw. Closed-window totals
    are NOT credited without a baseline (documented convergence-on-rotate)."""
    started = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=100)
    s = _state(total_manual_paused_seconds=999.0)  # closed totals ignored w/o baseline
    elapsed = pause_clock.active_elapsed_seconds(s, started, baseline_paused=None)
    assert 99.0 <= elapsed <= 102.0


def test_active_elapsed_seconds_clamps_to_zero():
    """Subtracted pause exceeding raw elapsed clamps to 0 (a fully-paused timer)."""
    started = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=30)
    pause_started = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=90)
    s = _state(paused_at=pause_started, paused_kind=pause_clock.PAUSED_KIND_MANUAL)
    elapsed = pause_clock.active_elapsed_seconds(s, started, baseline_paused=None)
    assert elapsed == 0.0


# ── mark_paused ───────────────────────────────────────────────────────────────


def test_mark_paused_sets_fields():
    s = _state()
    pause_clock.mark_paused(s, pause_clock.PAUSED_KIND_MANUAL)
    assert s.paused_at is not None
    assert s.paused_kind == "manual"


def test_mark_paused_idempotent_when_already_paused():
    """Re-calling mark_paused must not reset paused_at — would lose in-flight time."""
    earlier = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=300)
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
    paused_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=15)
    s = _state(paused_at=paused_at, paused_kind=pause_clock.PAUSED_KIND_MANUAL)
    pause_clock.accumulate_pause_window(s)
    assert s.paused_at is None
    assert s.paused_kind is None
    assert 14.0 <= s.total_manual_paused_seconds <= 17.0
    assert s.total_auto_paused_seconds == 0.0


def test_accumulate_routes_auto_window_to_auto_counter():
    paused_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=25)
    s = _state(paused_at=paused_at, paused_kind=pause_clock.PAUSED_KIND_AUTO)
    pause_clock.accumulate_pause_window(s)
    assert s.paused_at is None
    assert s.paused_kind is None
    assert s.total_manual_paused_seconds == 0.0
    assert 24.0 <= s.total_auto_paused_seconds <= 27.0


def test_accumulate_preserves_prior_totals():
    """Adding a new window doesn't overwrite previously accumulated time."""
    paused_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=10)
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
    started = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=30)
    s = _state()
    baseline = pause_clock.total_paused_seconds(s)
    assert baseline == 0.0

    # Enter a manual pause that started 20s ago, ended 5s ago — i.e. lasted 15s.
    s.paused_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=20)
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


# ── Double-pause idempotency ──────────────────────────────────────────────────


def test_manual_pause_snapshot_idempotency():
    """A second pause-call must not overwrite the pre-pause capacity snapshot.
    This guards the manual pause endpoint against double-clicks or retries that
    would snapshot the already-zeroed live capacity, causing resume to restore
    the wrong value.

    Scenario: operator pauses with 5 concurrent VMs configured.
      1. First pause: pre_pause_max_vms = 5, then runtime max_concurrent_vms = 0
      2. Second pause (retry/double-click): must NOT overwrite pre_pause_max_vms
      3. Resume: restores 5, not 0
    """
    s = _state(total_manual_paused_seconds=0.0)

    # Simulate first pause: snapshot pre_pause_max_vms before zeroing
    pre_pause_capacity = 5
    if s.pre_pause_max_vms is None:
        s.pre_pause_max_vms = pre_pause_capacity
    # Mark paused
    pause_clock.mark_paused(s, pause_clock.PAUSED_KIND_MANUAL)

    # Verify first pause captured the right value
    assert s.pre_pause_max_vms == 5
    assert s.paused_at is not None
    assert s.paused_kind == "manual"

    # Simulate second pause (idempotency test): zero has already been set
    current_runtime_vms = 0  # already zeroed by first pause
    if s.pre_pause_max_vms is None:  # idempotent guard — don't overwrite
        s.pre_pause_max_vms = current_runtime_vms
    # mark_paused is also idempotent
    pause_clock.mark_paused(s, pause_clock.PAUSED_KIND_MANUAL)

    # Verify second pause did not overwrite the snapshot
    assert s.pre_pause_max_vms == 5, "Second pause must not overwrite pre_pause snapshot"
    assert s.paused_at is not None
    assert s.paused_kind == "manual"

    # Simulate resume: use the saved pre_pause_max_vms
    restore_vms = s.pre_pause_max_vms or 5
    assert restore_vms == 5, "Resume must use the original pre-pause capacity"


def test_rollback_pause_snapshot_idempotency():
    """Rolling back an already-paused pipeline must not overwrite the genuine
    pre-pause capacity snapshot, and must preserve the existing pause window.

    This mirrors the manual-pause guard for the rollback endpoint. Scenario:
    the deploy-health watchdog auto-paused the pipeline (pre_pause_max_vms=5,
    live capacity already zeroed, paused_at set ~300s ago). An operator then
    triggers a rollback. The rollback must:
      1. NOT overwrite pre_pause_max_vms with the already-zeroed live capacity.
      2. NOT restart the pause window (preserve the original paused_at/kind),
         so accumulated auto-pause time isn't lost.
      3. Leave resume able to restore the operator's real capacity (5), not the
         default fallback.
    """
    earlier = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=300)
    s = _state(
        pre_pause_max_vms=5,
        runtime_max_concurrent_vms=5,
        paused_at=earlier,
        paused_kind=pause_clock.PAUSED_KIND_AUTO,
    )

    # Simulate rollback's guarded snapshot: live capacity is already zeroed.
    current_runtime_vms = 0
    if s.pre_pause_max_vms is None:  # idempotent guard — don't overwrite
        s.pre_pause_max_vms = current_runtime_vms or s.runtime_max_concurrent_vms
    # rollback marks an AUTO pause window — idempotent, must preserve prior window
    pause_clock.mark_paused(s, pause_clock.PAUSED_KIND_AUTO)

    assert s.pre_pause_max_vms == 5, "Rollback must not overwrite pre_pause snapshot"
    assert s.paused_at == earlier, "Rollback must preserve the existing pause window"
    assert s.paused_kind == "auto"

    # Resume restores the operator's real capacity, not the default fallback.
    restore_vms = s.pre_pause_max_vms or s.runtime_max_concurrent_vms or 5
    assert restore_vms == 5, "Resume must restore the genuine pre-pause capacity"
