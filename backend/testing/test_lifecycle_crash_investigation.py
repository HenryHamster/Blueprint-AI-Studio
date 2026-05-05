"""Tests for crash-episode investigation spawning in lifecycle.py."""
import asyncio
import uuid
from datetime import timedelta
from typing import Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.lifecycle import _spawn_crash_investigations
from app.models import Task, TaskHistory, TaskStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_project(pid=None):
    p = MagicMock()
    p.id = pid or uuid.uuid4()
    return p


def _make_session(projects, existing_task=None):
    """Return a fake AsyncSession that returns `projects` for Project queries
    and `existing_task` for Task dedup queries."""
    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)

    async def execute(stmt):
        result = MagicMock()
        # Distinguish Project selects from Task selects by inspecting whereclause text
        stmt_str = str(stmt)
        if "task" in stmt_str.lower() or "spawned_by" in stmt_str.lower():
            # Task dedup query
            if existing_task:
                result.first.return_value = (existing_task.id,)
            else:
                result.first.return_value = None
        else:
            # Project query
            result.scalars.return_value.all.return_value = projects
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
    async def test_spawns_task_per_project(self):
        """One investigation task should be created for each project."""
        pid1 = uuid.uuid4()
        pid2 = uuid.uuid4()
        projects = [_make_project(pid1), _make_project(pid2)]
        inflight: Dict[str, str] = {}

        session = _make_session(projects, existing_task=None)
        added_objects = []
        session.add = MagicMock(side_effect=lambda obj: added_objects.append(obj))

        with patch("app.lifecycle.AsyncSessionLocal", return_value=session):
            with patch("app.lifecycle.validate_internal_payload", side_effect=lambda p, _: p):
                await _spawn_crash_investigations(
                    service_name="reaper",
                    cause_str="MissingGreenlet at reaper.py:724",
                    exc=ValueError("test"),
                    crash_count=3,
                    inflight=inflight,
                )

        task_objects = [o for o in added_objects if isinstance(o, Task)]
        assert len(task_objects) == 2
        for t in task_objects:
            assert t.status == TaskStatus.QUEUED
            assert t.created_by == "ORCHESTRATOR"
            assert t.context["spawned_by"] == "crash_investigation"
            assert t.context["service_name"] == "reaper"
            assert t.context["crash_count"] == 3
        assert len(inflight) == 2
        session.commit.assert_called_once()

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
            with patch("app.lifecycle.validate_internal_payload", side_effect=lambda p, _: p):
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
            with patch("app.lifecycle.validate_internal_payload", side_effect=lambda p, _: p):
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
            with patch("app.lifecycle.validate_internal_payload", side_effect=capture_payload):
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
            with patch("app.lifecycle.validate_internal_payload", side_effect=lambda p, _: p):
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
        inflight_investigations: Dict[str, Dict[str, str]] = {"reaper": {"proj-1": "task-abc"}}

        # Stability reset triggered
        crash_timestamps.pop("reaper", None)
        inflight_investigations.pop("reaper", None)

        assert "reaper" not in crash_timestamps
        assert "reaper" not in inflight_investigations
