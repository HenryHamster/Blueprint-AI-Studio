"""Tests for sweep_stale_in_memory_state.

The orchestrator/reaper populate several module-level state dicts keyed by a
growing identifier space (direction_id, project_id, normalized error). Entries
were never removed once their condition resolved, so long-lived Cloud Run
instances leaked memory toward the 4GB ceiling. sweep_stale_in_memory_state
reclaims them; these tests pin the retention semantics.
"""

from collections import deque
from datetime import timedelta

import pytest

import app.shared_state as _ss
from app.shared_state import sweep_stale_in_memory_state
from app.time_utils import _utcnow


def _reset_state():
    _ss.direction_failure_streaks.clear()
    _ss.circuit_breakers.clear()
    _ss.autonomous_failure_counts.clear()
    _ss.merge_error_occurrences.clear()
    _ss.user_debug_spawn_times.clear()


@pytest.fixture(autouse=True)
def _autoclean_shared_state():
    """Reset all in-memory state dicts before every test in this module."""
    _reset_state()


class TestDirectionFailureStreaks:
    def test_idle_streak_pruned_when_last_failure_older_than_retention(self):
        now = _utcnow()
        _ss.direction_failure_streaks["d1"] = {
            "consecutive_failures": 0,
            "consecutive_infra_failures": 0,
            "last_failure_class": "",
            "last_failure_at": now - timedelta(hours=48),
            "held": False,
            "held_at": None,
            "held_reason": "",
            "held_failure_class": "",
            "project_id": "p1",
        }
        pruned = sweep_stale_in_memory_state()
        assert "d1" not in _ss.direction_failure_streaks
        assert pruned["direction_failure_streaks"] == 1

    def test_held_streak_kept_even_when_old(self):
        now = _utcnow()
        _ss.direction_failure_streaks["d1"] = {
            "consecutive_failures": 3,
            "consecutive_infra_failures": 0,
            "last_failure_class": "CODE_ERROR",
            "last_failure_at": now - timedelta(days=30),
            "held": True,
            "held_at": now - timedelta(days=30),
            "held_reason": "code",
            "held_failure_class": "CODE_ERROR",
            "project_id": "p1",
        }
        sweep_stale_in_memory_state()
        assert "d1" in _ss.direction_failure_streaks

    def test_streak_with_active_counters_kept(self):
        now = _utcnow()
        _ss.direction_failure_streaks["d1"] = {
            "consecutive_failures": 2,
            "consecutive_infra_failures": 0,
            "last_failure_class": "CODE_ERROR",
            "last_failure_at": now - timedelta(hours=48),
            "held": False,
            "held_at": None,
            "held_reason": "",
            "held_failure_class": "",
            "project_id": "p1",
        }
        sweep_stale_in_memory_state()
        assert "d1" in _ss.direction_failure_streaks

    def test_recent_streak_with_zero_counters_kept(self):
        now = _utcnow()
        _ss.direction_failure_streaks["d1"] = {
            "consecutive_failures": 0,
            "consecutive_infra_failures": 0,
            "last_failure_class": "",
            "last_failure_at": now - timedelta(minutes=5),
            "held": False,
            "held_at": None,
            "held_reason": "",
            "held_failure_class": "",
            "project_id": "p1",
        }
        sweep_stale_in_memory_state()
        assert "d1" in _ss.direction_failure_streaks


class TestCircuitBreakers:
    def test_closed_breaker_with_no_recent_activity_pruned(self):
        now = _utcnow()
        _ss.circuit_breakers["p1"] = {
            "state": "CLOSED",
            "failure_count": 0,
            "infra_failure_count": 0,
            "success_count": 5,
            "opened_at": now - timedelta(hours=48),
            "cooldown_seconds": 1800,
            "base_cooldown": 1800,
            "half_open_task_id": None,
            "open_reason": None,
        }
        pruned = sweep_stale_in_memory_state()
        assert "p1" not in _ss.circuit_breakers
        assert pruned["circuit_breakers"] == 1

    def test_open_breaker_kept(self):
        now = _utcnow()
        _ss.circuit_breakers["p1"] = {
            "state": "OPEN",
            "failure_count": 5,
            "infra_failure_count": 0,
            "success_count": 0,
            "opened_at": now - timedelta(hours=48),
            "cooldown_seconds": 1800,
            "base_cooldown": 1800,
            "half_open_task_id": None,
            "open_reason": "code",
        }
        sweep_stale_in_memory_state()
        assert "p1" in _ss.circuit_breakers

    def test_breaker_with_active_failure_count_kept(self):
        now = _utcnow()
        _ss.circuit_breakers["p1"] = {
            "state": "CLOSED",
            "failure_count": 2,
            "infra_failure_count": 0,
            "success_count": 0,
            "opened_at": now - timedelta(hours=48),
            "cooldown_seconds": 1800,
            "base_cooldown": 1800,
            "half_open_task_id": None,
            "open_reason": None,
        }
        sweep_stale_in_memory_state()
        assert "p1" in _ss.circuit_breakers


class TestAutonomousFailureCounts:
    def test_zero_counter_pruned(self):
        _ss.autonomous_failure_counts["p1"] = 0
        _ss.autonomous_failure_counts["p2"] = 2
        pruned = sweep_stale_in_memory_state()
        assert "p1" not in _ss.autonomous_failure_counts
        assert _ss.autonomous_failure_counts["p2"] == 2
        assert pruned["autonomous_failure_counts"] == 1


class TestMergeErrorOccurrences:
    def test_empty_deque_key_pruned_after_timestamps_age_out(self):
        now = _utcnow()
        stale = now - timedelta(seconds=_ss.MERGE_PATTERN_WINDOW_SECONDS * 3)
        _ss.merge_error_occurrences[("p1", "conflict")] = deque([(1, stale)])
        _ss.merge_error_occurrences[("p1", "lint")] = deque([(2, now)])
        pruned = sweep_stale_in_memory_state()
        assert ("p1", "conflict") not in _ss.merge_error_occurrences
        assert ("p1", "lint") in _ss.merge_error_occurrences
        assert pruned["merge_error_occurrences"] == 1


class TestUserDebugSpawnTimes:
    def test_empty_deque_pruned(self):
        now = _utcnow()
        stale = now - timedelta(hours=5)
        _ss.user_debug_spawn_times["p1"] = deque([stale])
        _ss.user_debug_spawn_times["p2"] = deque([now])
        pruned = sweep_stale_in_memory_state()
        assert "p1" not in _ss.user_debug_spawn_times
        assert "p2" in _ss.user_debug_spawn_times
        assert pruned["user_debug_spawn_times"] == 1


class TestSweepSummary:
    def test_empty_state_returns_zero_counts(self):
        pruned = sweep_stale_in_memory_state()
        assert all(v == 0 for v in pruned.values())
        assert set(pruned.keys()) == {
            "direction_failure_streaks",
            "circuit_breakers",
            "autonomous_failure_counts",
            "merge_error_occurrences",
            "user_debug_spawn_times",
            "vm_last_heartbeat",
            "warm_pin_clear_events",
            "warm_backlog_first_refusal",
            "warm_backlog_reuse_failure_events",
        }
