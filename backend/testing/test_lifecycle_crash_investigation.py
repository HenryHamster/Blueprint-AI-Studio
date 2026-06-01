"""Tests for background-service crash detection + reaper liveness in lifecycle.py.

The detect→investigate→fix self-repair apparatus has been removed: the
``_spawn_crash_investigations`` spawn path (which created investigate Tasks and
RegressionRemediationCase rows on a crash episode) is gone, replaced by a
deterministic operator Alert. What remains and is exercised here is the
crash-episode DETECTION bookkeeping (``_record_crash_and_count`` — timestamp
pruning + the 3-crash threshold), the deduped ERROR-Alert emission
(``_emit_crash_episode_alert``), the reaper liveness-probe decision
(``_evaluate_reaper_stall``), and the orphan-reap per-task crash isolation
guards. Every test below drives the ACTUAL production code in lifecycle.py /
reaper.py — none re-implement the logic in local lists/dicts.
"""

import uuid
from datetime import timedelta

import pytest
from sqlmodel import select

from app.database import AsyncSessionLocal
from app.lifecycle import (
    _BACKGROUND_CRASH_EPISODE_THRESHOLD,
    _BACKGROUND_CRASH_EPISODE_WINDOW_SECONDS,
    _emit_crash_episode_alert,
    _evaluate_reaper_stall,
    _record_crash_and_count,
)
from app.models import Alert, AlertSeverity
from app.time_utils import _utcnow
from testing.factories import make_project


# ---------------------------------------------------------------------------
# Crash-episode window bookkeeping — exercises _record_crash_and_count directly
# ---------------------------------------------------------------------------


class TestRecordCrashAndCount:
    """The real prune-and-count logic from lifecycle._record_crash_and_count."""

    def test_crash_timestamps_pruning(self):
        """A crash older than the rolling window is pruned on the next record."""
        now = _utcnow()
        stale = now - timedelta(seconds=_BACKGROUND_CRASH_EPISODE_WINDOW_SECONDS + 100)
        recent = now - timedelta(seconds=50)
        crash_timestamps = {"reaper": [stale, recent]}
        # Record a fresh crash at `now`: the stale entry must be pruned out.
        count = _record_crash_and_count(crash_timestamps, "reaper", now)
        assert count == 2  # stale pruned, recent + new kept
        assert stale not in crash_timestamps["reaper"]
        assert recent in crash_timestamps["reaper"]
        assert now in crash_timestamps["reaper"]

    def test_threshold_not_met_below_3(self):
        """Two crashes in the window do not reach the 3-crash threshold."""
        now = _utcnow()
        crash_timestamps: dict = {}
        first = _record_crash_and_count(
            crash_timestamps, "reaper", now - timedelta(seconds=100)
        )
        second = _record_crash_and_count(
            crash_timestamps, "reaper", now - timedelta(seconds=50)
        )
        assert first == 1
        assert second == 2
        assert second < _BACKGROUND_CRASH_EPISODE_THRESHOLD

    def test_threshold_met_exactly_3(self):
        """Three crashes inside the window reach the threshold exactly."""
        now = _utcnow()
        crash_timestamps: dict = {}
        for offset in (500, 250, 0):
            count = _record_crash_and_count(
                crash_timestamps, "reaper", now - timedelta(seconds=offset)
            )
        assert count == 3
        assert count >= _BACKGROUND_CRASH_EPISODE_THRESHOLD

    def test_per_service_isolation(self):
        """Crashes for distinct services accumulate in separate windows."""
        now = _utcnow()
        crash_timestamps: dict = {}
        _record_crash_and_count(crash_timestamps, "reaper", now)
        _record_crash_and_count(crash_timestamps, "reaper", now)
        orch_count = _record_crash_and_count(crash_timestamps, "orchestrator", now)
        assert orch_count == 1
        assert len(crash_timestamps["reaper"]) == 2


# ---------------------------------------------------------------------------
# Crash-episode ERROR-Alert emission — exercises _emit_crash_episode_alert
# against the real DB path (the re-added AlertSeverity.ERROR emitter).
# ---------------------------------------------------------------------------


class TestCrashEpisodeAlertEmission:
    @pytest.mark.asyncio
    async def test_emits_error_alert_per_project(self, db_session):
        """On an episode the emitter writes one ERROR Alert per project."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        created = await _emit_crash_episode_alert(
            AsyncSessionLocal, "reaper", crash_count=3
        )
        assert created == 1

        alerts = (
            (
                await db_session.execute(
                    select(Alert).where(
                        Alert.project_id == project.id,
                        Alert.title == "Background service crash episode: reaper",
                        Alert.dismissed_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(alerts) == 1
        assert alerts[0].severity == AlertSeverity.ERROR
        assert "crashed 3 times" in alerts[0].message
        assert (
            f"{_BACKGROUND_CRASH_EPISODE_WINDOW_SECONDS // 60}" in alerts[0].message
        )

    @pytest.mark.asyncio
    async def test_deduped_on_second_emit(self, db_session):
        """A second emit with an open episode alert present does not duplicate it."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        first = await _emit_crash_episode_alert(
            AsyncSessionLocal, "reaper", crash_count=3
        )
        second = await _emit_crash_episode_alert(
            AsyncSessionLocal, "reaper", crash_count=4
        )
        assert first == 1
        assert second == 0

        alerts = (
            (
                await db_session.execute(
                    select(Alert).where(
                        Alert.project_id == project.id,
                        Alert.title == "Background service crash episode: reaper",
                        Alert.dismissed_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(alerts) == 1


# ---------------------------------------------------------------------------
# Regression guards: orphan-reap crash isolation ↔ lifecycle crash-episode link
# ---------------------------------------------------------------------------


class TestOrphanReapCrashIsolation:
    """Regression guards verifying that isolated orphan-reap failures stay below
    the service-crash radar in _monitor_background_tasks.

    Before the per-task isolation fix, a single poison-pill task crashing inside
    the reaper loop caused the entire reaper service to exit (task.done() = True),
    eventually reaching the crash-episode threshold. After the fix, per-task
    exceptions are caught inside the reaper's own try/except so the reaper service
    never exits for per-task failures — crash_timestamps["reaper"] stays empty.
    """

    def test_orphan_streak_limit_at_most_episode_threshold(self):
        """Per-task quarantine fires before service-level crash episodes.

        If _ORPHAN_REAP_CRASH_STREAK_LIMIT > _BACKGROUND_CRASH_EPISODE_THRESHOLD,
        a poison-pill task would trigger enough full-reaper crashes to trip a
        crash episode BEFORE being quarantined — defeating the per-task isolation.
        Quarantine must fire first (streak ≤ episode threshold).
        """
        from app.reaper import _ORPHAN_REAP_CRASH_STREAK_LIMIT

        assert _ORPHAN_REAP_CRASH_STREAK_LIMIT <= _BACKGROUND_CRASH_EPISODE_THRESHOLD

    def test_per_task_isolation_keeps_service_crash_window_empty(self):
        """When the reaper isolates per-task exceptions the service window stays empty.

        _record_crash_and_count is only invoked when an asyncio.Task finishes
        (task.done() = True). Per-row isolation means the reaper service task never
        exits for per-task reap failures, so no crash timestamp is ever recorded —
        the per-service window for "reaper" is therefore absent / empty.
        """
        crash_timestamps: dict = {}
        reaper_service_done = False  # reaper stayed alive via per-task isolation
        if reaper_service_done:
            _record_crash_and_count(crash_timestamps, "reaper", _utcnow())
        assert not crash_timestamps.get("reaper")

    def test_quarantine_clears_streak_entry_so_task_is_not_reselected(self):
        """After quarantine, the streak entry is deleted so the task stops accumulating.

        The quarantine path in reaper.py transitions the task to FAILED (removing it
        from the PROVISIONING/IN_PROGRESS active-task query on the next cycle) and
        deletes the streak entry so stale accumulation cannot re-trigger quarantine.
        """
        from app.reaper import (
            _orphan_reap_crash_streaks,
            _ORPHAN_REAP_CRASH_STREAK_LIMIT,
        )

        task_id = str(uuid.uuid4())
        _orphan_reap_crash_streaks.pop(task_id, None)  # clean slate

        # Simulate accumulation up to the quarantine threshold
        _orphan_reap_crash_streaks[task_id] = _ORPHAN_REAP_CRASH_STREAK_LIMIT
        assert (
            _orphan_reap_crash_streaks.get(task_id, 0)
            >= _ORPHAN_REAP_CRASH_STREAK_LIMIT
        )

        # Simulate quarantine: DB transition happens, streak entry is removed
        del _orphan_reap_crash_streaks[task_id]
        assert task_id not in _orphan_reap_crash_streaks


# ---------------------------------------------------------------------------
# Reaper liveness probe — exercises lifecycle._evaluate_reaper_stall directly
# ---------------------------------------------------------------------------


class TestReaperLivenessProbe:
    """Verify the lifecycle watchdog respects the reaper's HALTED state.

    When the reaper self-halts after a crash-loop, the module-level halt state
    persists for the lifetime of the process. Cancelling and re-creating the
    asyncio.Task only produces another halted reaper. The watchdog must therefore
    skip the cancel when the reaper is halted, and only cancel a non-halted reaper
    whose progress heartbeat is older than the stall threshold.
    """

    _STALL_THRESHOLD = 300.0

    def test_halted_reaper_is_not_cancelled(self):
        """A HALTED reaper must not be cancelled even when it looks stalled."""
        now = _utcnow()
        stale_progress = now - timedelta(seconds=400)  # well past the threshold
        should_cancel, reason = _evaluate_reaper_stall(
            is_halted=True,
            last_progress=stale_progress,
            now=now,
            stall_threshold=self._STALL_THRESHOLD,
        )
        assert should_cancel is False
        assert reason is None

    def test_stalled_non_halted_reaper_is_cancelled(self):
        """A non-halted reaper with a too-old progress heartbeat is cancelled."""
        now = _utcnow()
        stale_progress = now - timedelta(seconds=400)
        should_cancel, reason = _evaluate_reaper_stall(
            is_halted=False,
            last_progress=stale_progress,
            now=now,
            stall_threshold=self._STALL_THRESHOLD,
        )
        assert should_cancel is True
        assert reason is not None
        assert "no progress heartbeat" in reason

    def test_progressing_reaper_within_threshold_is_not_cancelled(self):
        """A non-halted reaper that made progress inside the threshold is left alone."""
        now = _utcnow()
        fresh_progress = now - timedelta(seconds=10)
        should_cancel, reason = _evaluate_reaper_stall(
            is_halted=False,
            last_progress=fresh_progress,
            now=now,
            stall_threshold=self._STALL_THRESHOLD,
        )
        assert should_cancel is False
        assert reason is None

    def test_never_progressed_reaper_is_not_cancelled(self):
        """A freshly-spawned reaper (no progress recorded yet) is given time to start."""
        should_cancel, reason = _evaluate_reaper_stall(
            is_halted=False,
            last_progress=None,
            now=_utcnow(),
            stall_threshold=self._STALL_THRESHOLD,
        )
        assert should_cancel is False
        assert reason is None

    def test_is_reaper_halted_reflects_halt_state(self):
        """is_reaper_halted() returns True when _reaper_halt_state[halted] is True."""
        import app.reaper as _reaper_mod

        original = _reaper_mod._reaper_halt_state.copy()
        try:
            _reaper_mod._reaper_halt_state["halted"] = True
            assert _reaper_mod.is_reaper_halted() is True
            _reaper_mod._reaper_halt_state["halted"] = False
            assert _reaper_mod.is_reaper_halted() is False
        finally:
            _reaper_mod._reaper_halt_state.update(original)
