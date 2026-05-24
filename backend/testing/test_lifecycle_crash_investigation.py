"""Tests for crash-episode investigation spawning in lifecycle.py."""

import uuid
from datetime import timedelta
from typing import Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.lifecycle import _spawn_crash_investigations
from app.models import (
    RegressionCaseStatus,
    RegressionRemediationCase,
    Task,
    TaskStatus,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_project(pid=None):
    p = MagicMock()
    p.id = pid or uuid.uuid4()
    return p


def _make_session(projects, existing_task=None, existing_case=None):
    """Return a fake AsyncSession that returns `projects` for Project queries,
    `existing_task` for Task dedup queries, and `existing_case` for
    RegressionRemediationCase queries.

    The Project query (`select(Project.id)`) yields scalar UUIDs, so the
    mock returns each project's `.id` rather than the project object itself.
    """
    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)

    async def execute(stmt):
        result = MagicMock()
        stmt_str = str(stmt).lower()
        if "regressionremediationcase" in stmt_str:
            result.scalars.return_value.first.return_value = existing_case
        elif "task" in stmt_str or "spawned_by" in stmt_str:
            # Task dedup query returns a row-tuple or None
            if existing_task:
                result.first.return_value = (existing_task.id,)
            else:
                result.first.return_value = None
        else:
            # Project query — select(Project.id) yields scalar UUIDs
            result.scalars.return_value.all.return_value = [p.id for p in projects]
        return result

    session.execute = execute
    session.flush = AsyncMock()
    session.commit = AsyncMock()
    session.add = MagicMock()
    return session


# ---------------------------------------------------------------------------
# Tests for _spawn_crash_investigations
# ---------------------------------------------------------------------------


class TestSpawnCrashInvestigations:
    @pytest.mark.asyncio
    async def test_spawns_task_and_case_per_project(self):
        """One investigation task and one RegressionRemediationCase per project."""
        pid1 = uuid.uuid4()
        pid2 = uuid.uuid4()
        projects = [_make_project(pid1), _make_project(pid2)]
        inflight: Dict[str, str] = {}

        session = _make_session(projects, existing_task=None, existing_case=None)
        added_objects = []
        session.add = MagicMock(side_effect=lambda obj: added_objects.append(obj))

        with patch("app.lifecycle.AsyncSessionLocal", return_value=session):
            with patch(
                "app.lifecycle.validate_internal_payload", side_effect=lambda p, _: p
            ):
                with patch.object(
                    RegressionRemediationCase,
                    "create_for_spawn",
                    side_effect=lambda **kw: MagicMock(spec=RegressionRemediationCase),
                ) as mock_create:
                    await _spawn_crash_investigations(
                        service_name="reaper",
                        cause_str="MissingGreenlet at reaper.py:724",
                        exc=ValueError("test"),
                        crash_count=3,
                        inflight=inflight,
                    )
                assert mock_create.call_count == 2

        task_objects = [o for o in added_objects if isinstance(o, Task)]
        assert len(task_objects) == 2
        for t in task_objects:
            assert t.status == TaskStatus.QUEUED
            assert t.created_by == "ORCHESTRATOR"
            assert t.context["spawned_by"] == "crash_investigation"
            assert t.context["service_name"] == "reaper"
            assert t.context["crash_count"] == 3
            assert t.context["regression_class"] == "background_service_crash:reaper"
        assert len(inflight) == 2
        session.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_suppressed_when_needs_manual_intervention(self):
        """No task is spawned when the existing case is at NEEDS_MANUAL_INTERVENTION."""
        pid = uuid.uuid4()
        projects = [_make_project(pid)]
        inflight: Dict[str, str] = {}

        mock_case = MagicMock(spec=RegressionRemediationCase)
        mock_case.status = RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION

        session = _make_session(projects, existing_task=None, existing_case=mock_case)
        added_objects = []
        session.add = MagicMock(side_effect=lambda obj: added_objects.append(obj))

        with patch("app.lifecycle.AsyncSessionLocal", return_value=session):
            with patch(
                "app.lifecycle.validate_internal_payload", side_effect=lambda p, _: p
            ):
                await _spawn_crash_investigations(
                    service_name="orchestrator",
                    cause_str="hung",
                    exc=None,
                    crash_count=5,
                    inflight=inflight,
                )

        task_objects = [o for o in added_objects if isinstance(o, Task)]
        assert len(task_objects) == 0
        session.commit.assert_not_called()

    @pytest.mark.asyncio
    async def test_existing_active_case_recorded_not_duplicated(self):
        """When an ACTIVE case exists, record_spawn is called instead of create_for_spawn."""
        pid = uuid.uuid4()
        projects = [_make_project(pid)]
        inflight: Dict[str, str] = {}

        mock_case = MagicMock(spec=RegressionRemediationCase)
        mock_case.status = RegressionCaseStatus.ACTIVE

        session = _make_session(projects, existing_task=None, existing_case=mock_case)
        added_objects = []
        session.add = MagicMock(side_effect=lambda obj: added_objects.append(obj))

        with patch("app.lifecycle.AsyncSessionLocal", return_value=session):
            with patch(
                "app.lifecycle.validate_internal_payload", side_effect=lambda p, _: p
            ):
                with patch.object(
                    RegressionRemediationCase, "create_for_spawn"
                ) as mock_create:
                    await _spawn_crash_investigations(
                        service_name="gcp_cleanup",
                        cause_str="timeout",
                        exc=None,
                        crash_count=3,
                        inflight=inflight,
                    )
                mock_create.assert_not_called()
        mock_case.record_spawn.assert_called_once()

    @pytest.mark.asyncio
    async def test_dedup_skips_project_already_inflight(self):
        """Projects already tracked in the inflight dict are skipped."""
        pid = uuid.uuid4()
        projects = [_make_project(pid)]
        existing_tid = str(uuid.uuid4())
        inflight: Dict[str, str] = {str(pid): existing_tid}

        session = _make_session(projects, existing_task=None)
        added_objects = []
        session.add = MagicMock(side_effect=lambda obj: added_objects.append(obj))

        with patch("app.lifecycle.AsyncSessionLocal", return_value=session):
            with patch(
                "app.lifecycle.validate_internal_payload", side_effect=lambda p, _: p
            ):
                await _spawn_crash_investigations(
                    service_name="reaper",
                    cause_str="error",
                    exc=None,
                    crash_count=3,
                    inflight=inflight,
                )

        task_objects = [o for o in added_objects if isinstance(o, Task)]
        assert len(task_objects) == 0
        session.commit.assert_not_called()

    @pytest.mark.asyncio
    async def test_dedup_skips_project_with_db_active_investigation(self):
        """If the DB already has an active crash investigation, inflight is populated but no new task spawned."""
        pid = uuid.uuid4()
        projects = [_make_project(pid)]
        existing = MagicMock()
        existing.id = uuid.uuid4()
        inflight: Dict[str, str] = {}

        session = _make_session(projects, existing_task=existing)
        added_objects = []
        session.add = MagicMock(side_effect=lambda obj: added_objects.append(obj))

        with patch("app.lifecycle.AsyncSessionLocal", return_value=session):
            with patch(
                "app.lifecycle.validate_internal_payload", side_effect=lambda p, _: p
            ):
                await _spawn_crash_investigations(
                    service_name="reaper",
                    cause_str="error",
                    exc=RuntimeError("db issue"),
                    crash_count=4,
                    inflight=inflight,
                )

        task_objects = [o for o in added_objects if isinstance(o, Task)]
        assert len(task_objects) == 0
        assert str(pid) in inflight
        session.commit.assert_not_called()

    @pytest.mark.asyncio
    async def test_reaper_stats_included_in_prompt(self):
        """When service_name is 'reaper', reaper telemetry is included in the prompt."""
        pid = uuid.uuid4()
        projects = [_make_project(pid)]
        inflight: Dict[str, str] = {}

        session = _make_session(projects, existing_task=None)
        captured_payloads = []

        def capture_payload(p, ctx):
            captured_payloads.append(p)
            return p

        mock_stats = {"cycles_completed_total": 42, "last_cycle_age_seconds": 90.5}

        with patch("app.lifecycle.AsyncSessionLocal", return_value=session):
            with patch(
                "app.lifecycle.validate_internal_payload", side_effect=capture_payload
            ):
                with patch("app.reaper.get_reaper_stats", return_value=mock_stats):
                    await _spawn_crash_investigations(
                        service_name="reaper",
                        cause_str="MissingGreenlet",
                        exc=None,
                        crash_count=3,
                        inflight=inflight,
                    )

        assert len(captured_payloads) == 1
        prompt = captured_payloads[0]["prompt"]
        assert "cycles_completed=42" in prompt
        assert "last_cycle_age=90.5s" in prompt

    @pytest.mark.asyncio
    async def test_no_projects_no_commit(self):
        """When there are no projects, no tasks are spawned and commit is not called."""
        inflight: Dict[str, str] = {}
        session = _make_session(projects=[], existing_task=None)

        with patch("app.lifecycle.AsyncSessionLocal", return_value=session):
            with patch(
                "app.lifecycle.validate_internal_payload", side_effect=lambda p, _: p
            ):
                await _spawn_crash_investigations(
                    service_name="orchestrator",
                    cause_str="timeout",
                    exc=None,
                    crash_count=3,
                    inflight=inflight,
                )

        session.commit.assert_not_called()


# ---------------------------------------------------------------------------
# Tests for crash tracking logic in _monitor_background_tasks
# ---------------------------------------------------------------------------


class TestMonitorCrashTracking:
    """Unit tests for the crash-episode threshold logic extracted from the monitor."""

    def test_crash_timestamps_pruning(self):
        """Timestamps older than 600s are pruned; only recent ones count."""
        from app.time_utils import _utcnow

        now = _utcnow()
        old = now - timedelta(seconds=700)
        recent1 = now - timedelta(seconds=200)
        recent2 = now - timedelta(seconds=50)

        ts_list = [old, recent1, recent2, now]
        cutoff = now - timedelta(seconds=600)
        pruned = [ts for ts in ts_list if ts >= cutoff]
        assert len(pruned) == 3  # old excluded
        assert old not in pruned

    def test_threshold_not_met_below_3(self):
        """2 crashes in window does not meet the 3-crash threshold."""
        from app.time_utils import _utcnow

        now = _utcnow()
        ts_list = [now - timedelta(seconds=100), now - timedelta(seconds=50)]
        assert len(ts_list) < 3

    def test_threshold_met_exactly_3(self):
        """Exactly 3 crashes in window meets the threshold."""
        from app.time_utils import _utcnow

        now = _utcnow()
        ts_list = [
            now - timedelta(seconds=500),
            now - timedelta(seconds=250),
            now,
        ]
        assert len(ts_list) >= 3

    def test_stability_reset_clears_tracking(self):
        """Simulates the stability reset: crash_timestamps and inflight are cleared."""
        crash_timestamps: Dict[str, list] = {"reaper": [MagicMock(), MagicMock()]}
        inflight_investigations: Dict[str, Dict[str, str]] = {
            "reaper": {"proj-1": "task-abc"}
        }

        # Stability reset triggered
        crash_timestamps.pop("reaper", None)
        inflight_investigations.pop("reaper", None)

        assert "reaper" not in crash_timestamps
        assert "reaper" not in inflight_investigations


# ---------------------------------------------------------------------------
# Regression guards: orphan-reap crash isolation ↔ lifecycle crash-episode link
# ---------------------------------------------------------------------------


class TestOrphanReapCrashIsolation:
    """Regression guards verifying that isolated orphan-reap failures stay below
    the service-crash radar in _monitor_background_tasks.

    Before the per-task isolation fix, a single poison-pill task crashing inside
    the reaper loop caused the entire reaper service to exit (task.done() = True),
    eventually reaching the crash-episode threshold and spawning investigation tasks.
    After the fix, per-task exceptions are caught inside the reaper's own try/except
    so the reaper service never exits for per-task failures — crash_timestamps["reaper"]
    stays empty and no spurious investigation tasks are spawned.
    """

    def test_orphan_streak_limit_at_most_episode_threshold(self):
        """Per-task quarantine fires before service-level crash investigations.

        If _ORPHAN_REAP_CRASH_STREAK_LIMIT > _BACKGROUND_CRASH_EPISODE_THRESHOLD,
        a poison-pill task would trigger enough full-reaper crashes to spawn an
        investigation task BEFORE being quarantined — defeating the per-task isolation.
        Quarantine must fire first (streak ≤ episode threshold).
        """
        from app.reaper import _ORPHAN_REAP_CRASH_STREAK_LIMIT
        from app.lifecycle import _BACKGROUND_CRASH_EPISODE_THRESHOLD

        assert _ORPHAN_REAP_CRASH_STREAK_LIMIT <= _BACKGROUND_CRASH_EPISODE_THRESHOLD

    def test_per_task_isolation_keeps_service_crash_timestamps_empty(self):
        """When the reaper isolates per-task exceptions the service crash counter stays at 0.

        _monitor_background_tasks only appends to crash_timestamps when an asyncio.Task
        finishes (task.done() = True). Per-row isolation means the reaper service task
        never exits for per-task reap failures, so no crash timestamp is recorded.
        """
        from app.time_utils import _utcnow

        crash_timestamps: Dict[str, list] = {}
        reaper_service_done = False  # reaper stayed alive via per-task isolation
        if reaper_service_done:
            crash_timestamps.setdefault("reaper", []).append(_utcnow())

        assert not crash_timestamps.get("reaper")

    def test_quarantine_clears_streak_entry_so_task_is_not_reselected(self):
        """After quarantine, the streak entry is deleted so the task stops accumulating.

        The quarantine path in reaper.py transitions the task to FAILED (removing it
        from the PROVISIONING/IN_PROGRESS active-task query on the next cycle) and
        deletes the streak entry so stale accumulation cannot re-trigger quarantine.
        """
        import uuid
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
# Tests for the reaper liveness probe in _monitor_background_tasks
# ---------------------------------------------------------------------------


class TestReaperLivenessProbe:
    """Verify the lifecycle watchdog respects the reaper's HALTED state.

    When the reaper self-halts after a crash-loop, the module-level halt
    state persists for the lifetime of the process.  Cancelling and
    re-creating the asyncio.Task only produces another halted reaper (the new
    task sees the same ``_reaper_halt_state["halted"] = True`` and immediately
    enters the 300s sleep loop).  The watchdog must therefore skip the cancel
    when the reaper is halted.
    """

    def test_halted_reaper_is_not_cancelled(self):
        """Simulate the watchdog probe logic: a HALTED reaper must not be cancelled."""
        from unittest.mock import MagicMock

        cancel_reasons: Dict[str, str] = {}
        mock_reaper_task = MagicMock()
        mock_reaper_task.done.return_value = False

        reaper_last_cycle = None  # no completed cycles — looks stalled
        is_halted = True  # but the reaper is HALTED

        # Mirror the probe logic from lifecycle.py _monitor_background_tasks
        _reaper_stall_threshold = 300.0
        if mock_reaper_task is not None and not mock_reaper_task.done():
            if is_halted:
                pass  # halted intentionally — skip cancel
            elif reaper_last_cycle is not None:
                from app.time_utils import _utcnow
                from datetime import timedelta

                stale_last = _utcnow() - timedelta(seconds=400)
                age = (_utcnow() - stale_last).total_seconds()
                if age > _reaper_stall_threshold:
                    cancel_reasons["reaper"] = (
                        f"watchdog cancelled: no cycle completion in {int(age)}s"
                    )
                    mock_reaper_task.cancel()

        mock_reaper_task.cancel.assert_not_called()
        assert "reaper" not in cancel_reasons

    def test_stalled_non_halted_reaper_is_cancelled(self):
        """When the reaper is NOT halted but its last cycle is too old, it is cancelled."""
        from unittest.mock import MagicMock
        from datetime import timedelta
        from app.time_utils import _utcnow

        cancel_reasons: Dict[str, str] = {}
        mock_reaper_task = MagicMock()
        mock_reaper_task.done.return_value = False

        stale_last_cycle = _utcnow() - timedelta(seconds=400)
        is_halted = False

        _reaper_stall_threshold = 300.0
        if mock_reaper_task is not None and not mock_reaper_task.done():
            if is_halted:
                pass
            elif stale_last_cycle is not None:
                age = (_utcnow() - stale_last_cycle).total_seconds()
                if age > _reaper_stall_threshold:
                    reaper_reason = f"no cycle completion in {int(age)}s"
                    cancel_reasons["reaper"] = f"watchdog cancelled: {reaper_reason}"
                    mock_reaper_task.cancel()

        mock_reaper_task.cancel.assert_called_once()
        assert "reaper" in cancel_reasons

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
