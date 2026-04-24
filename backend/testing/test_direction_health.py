"""Tests for direction-level failure streak detection and dispatch hold.

Covers:
- streak accumulation
- hold triggering at DIRECTION_HOLD_THRESHOLD consecutive CODE_ERROR failures
- auto-clear on success
- auto-clear on timeout
- infra failures NOT counting toward streak
- tasks without direction_id pass through filter unaffected
- tasks with a held direction are deferred by filter_held_directions
- clear_direction_hold resets streak
- get_pipeline_health_snapshot includes held_directions
"""
import uuid
import pytest
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import app.shared_state as _ss
from app.shared_state import (
    record_direction_result,
    clear_direction_hold,
    is_direction_held,
    get_direction_hold_info,
    get_held_directions,
    get_pipeline_health_snapshot,
    DIRECTION_HOLD_THRESHOLD,
    DIRECTION_HOLD_TIMEOUT_HOURS,
)
from app.orchestrator_filters import filter_held_directions
from testing.factories import make_task


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fresh_direction_id() -> str:
    """Return a unique direction id so tests don't bleed into each other."""
    return str(uuid.uuid4())


def _setup():
    """Clear streak state before each test."""
    _ss.direction_failure_streaks.clear()


# ---------------------------------------------------------------------------
# Unit tests — record_direction_result
# ---------------------------------------------------------------------------

class TestRecordDirectionResult:
    def setup_method(self):
        _setup()

    def test_infra_failures_are_ignored_entirely(self):
        """Infra failures (RATE_LIMIT, PROVISION_FAILURE, AGENT_TIMEOUT,
        EXTERNAL_API, SCOPE_OVERFLOW) are platform noise, not the direction's
        fault — they must NOT count toward any hold streak, ever."""
        did = _fresh_direction_id()
        for fc in ("PROVISION_FAILURE", "RATE_LIMIT", "AGENT_TIMEOUT", "EXTERNAL_API", "SCOPE_OVERFLOW"):
            for _ in range(20):  # way past any threshold
                record_direction_result(did, success=False, failure_class=fc)
        # Direction state may or may not exist depending on init path; either
        # way it must not be held.
        streak = _ss.direction_failure_streaks.get(did, {})
        assert not streak.get("held")
        assert streak.get("consecutive_failures", 0) == 0

    def test_code_error_increments_streak(self):
        did = _fresh_direction_id()
        record_direction_result(did, success=False, failure_class="CODE_ERROR")
        streak = _ss.direction_failure_streaks[did]
        assert streak["consecutive_failures"] == 1
        assert not streak["held"]

    def test_hold_triggered_at_threshold(self):
        did = _fresh_direction_id()
        for _ in range(DIRECTION_HOLD_THRESHOLD):
            record_direction_result(did, success=False, failure_class="CODE_ERROR")
        held, reason = is_direction_held(did)
        assert held
        assert "consecutive" in reason
        assert str(DIRECTION_HOLD_THRESHOLD) in reason

    def test_success_clears_streak_and_hold(self):
        did = _fresh_direction_id()
        for _ in range(DIRECTION_HOLD_THRESHOLD):
            record_direction_result(did, success=False, failure_class="CODE_ERROR")
        assert is_direction_held(did)[0]

        record_direction_result(did, success=True)
        held, _ = is_direction_held(did)
        assert not held
        streak = _ss.direction_failure_streaks[did]
        assert streak["consecutive_failures"] == 0

    def test_no_direction_id_is_noop(self):
        record_direction_result("", success=False, failure_class="CODE_ERROR")
        assert "" not in _ss.direction_failure_streaks

    def test_mixed_infra_and_code_errors(self):
        """Only code errors count; infra failures are ignored."""
        did = _fresh_direction_id()
        for _ in range(2):
            record_direction_result(did, success=False, failure_class="CODE_ERROR")
        for _ in range(50):  # infra failures should have no effect
            record_direction_result(did, success=False, failure_class="PROVISION_FAILURE")
        streak = _ss.direction_failure_streaks[did]
        assert streak["consecutive_failures"] == 2
        assert not streak["held"]

    def test_interleaved_infra_does_not_reset_code_streak(self):
        """Code streak must not be reset by interleaved infra noise."""
        did = _fresh_direction_id()
        record_direction_result(did, success=False, failure_class="CODE_ERROR")
        record_direction_result(did, success=False, failure_class="RATE_LIMIT")
        record_direction_result(did, success=False, failure_class="CODE_ERROR")
        record_direction_result(did, success=False, failure_class="PROVISION_FAILURE")
        record_direction_result(did, success=False, failure_class="CODE_ERROR")
        streak = _ss.direction_failure_streaks[did]
        assert streak["consecutive_failures"] == 3
        assert streak["held"]
        _, reason = is_direction_held(did)
        assert "CODE_ERROR" in reason

    def test_project_id_stored(self):
        did = _fresh_direction_id()
        pid = str(uuid.uuid4())
        record_direction_result(did, success=False, failure_class="CODE_ERROR", project_id=pid)
        assert _ss.direction_failure_streaks[did]["project_id"] == pid


# ---------------------------------------------------------------------------
# Unit tests — clear_direction_hold
# ---------------------------------------------------------------------------

class TestClearDirectionHold:
    def setup_method(self):
        _setup()

    def test_clears_hold_and_resets_count(self):
        did = _fresh_direction_id()
        for _ in range(DIRECTION_HOLD_THRESHOLD):
            record_direction_result(did, success=False, failure_class="CODE_ERROR")
        assert is_direction_held(did)[0]

        clear_direction_hold(did)
        held, _ = is_direction_held(did)
        assert not held
        assert _ss.direction_failure_streaks[did]["consecutive_failures"] == 0

    def test_clear_on_unknown_direction_is_noop(self):
        # Should not raise
        clear_direction_hold("non-existent-direction")


# ---------------------------------------------------------------------------
# Unit tests — is_direction_held timeout
# ---------------------------------------------------------------------------

class TestIsDirectionHeldTimeout:
    def setup_method(self):
        _setup()

    def test_hold_expires_after_timeout(self):
        did = _fresh_direction_id()
        for _ in range(DIRECTION_HOLD_THRESHOLD):
            record_direction_result(did, success=False, failure_class="CODE_ERROR")
        assert is_direction_held(did)[0]

        # Manually backdate held_at past the timeout window
        _ss.direction_failure_streaks[did]["held_at"] = (
            _ss._utcnow() - timedelta(hours=DIRECTION_HOLD_TIMEOUT_HOURS + 1)
        )
        held, _ = is_direction_held(did)
        assert not held
        # streak entry should be un-held in-memory after the call
        assert not _ss.direction_failure_streaks[did]["held"]

    def test_hold_within_timeout_stays_held(self):
        did = _fresh_direction_id()
        for _ in range(DIRECTION_HOLD_THRESHOLD):
            record_direction_result(did, success=False, failure_class="CODE_ERROR")
        held, _ = is_direction_held(did)
        assert held


# ---------------------------------------------------------------------------
# Unit tests — get_direction_hold_info
# ---------------------------------------------------------------------------

class TestGetDirectionHoldInfo:
    def setup_method(self):
        _setup()

    def test_returns_held_at_and_held_until_while_held(self):
        did = _fresh_direction_id()
        for _ in range(DIRECTION_HOLD_THRESHOLD):
            record_direction_result(did, success=False, failure_class="CODE_ERROR")

        info = get_direction_hold_info(did)
        assert info["held"] is True
        assert info["held_at"] is not None
        assert info["held_until"] is not None
        delta = info["held_until"] - info["held_at"]
        assert int(delta.total_seconds()) == DIRECTION_HOLD_TIMEOUT_HOURS * 3600

    def test_timeout_returns_unheld_info_and_clears_streak(self):
        did = _fresh_direction_id()
        for _ in range(DIRECTION_HOLD_THRESHOLD):
            record_direction_result(did, success=False, failure_class="CODE_ERROR")
        _ss.direction_failure_streaks[did]["held_at"] = (
            _ss._utcnow() - timedelta(hours=DIRECTION_HOLD_TIMEOUT_HOURS + 1)
        )

        info = get_direction_hold_info(did)
        assert info == {"held": False, "reason": "", "held_at": None, "held_until": None}
        assert _ss.direction_failure_streaks[did]["held"] is False


# ---------------------------------------------------------------------------
# Unit tests — get_held_directions
# ---------------------------------------------------------------------------

class TestGetHeldDirections:
    def setup_method(self):
        _setup()

    def test_returns_held_directions(self):
        did1 = _fresh_direction_id()
        did2 = _fresh_direction_id()
        for _ in range(DIRECTION_HOLD_THRESHOLD):
            record_direction_result(did1, success=False, failure_class="CODE_ERROR")
        record_direction_result(did2, success=False, failure_class="CODE_ERROR")

        held = get_held_directions()
        assert did1 in held
        assert did2 not in held  # only 1 failure, below threshold

    def test_expired_hold_not_returned(self):
        did = _fresh_direction_id()
        for _ in range(DIRECTION_HOLD_THRESHOLD):
            record_direction_result(did, success=False, failure_class="CODE_ERROR")
        _ss.direction_failure_streaks[did]["held_at"] = (
            _ss._utcnow() - timedelta(hours=DIRECTION_HOLD_TIMEOUT_HOURS + 1)
        )
        held = get_held_directions()
        assert did not in held

    def test_held_entry_has_expected_fields(self):
        did = _fresh_direction_id()
        pid = str(uuid.uuid4())
        for _ in range(DIRECTION_HOLD_THRESHOLD):
            record_direction_result(did, success=False, failure_class="CODE_ERROR", project_id=pid)
        held = get_held_directions()
        entry = held[did]
        assert entry["consecutive_failures"] == DIRECTION_HOLD_THRESHOLD
        assert entry["last_failure_class"] == "CODE_ERROR"
        assert entry["project_id"] == pid
        assert "reason" in entry


# ---------------------------------------------------------------------------
# Unit tests — get_pipeline_health_snapshot includes held_directions
# ---------------------------------------------------------------------------

class TestPipelineHealthSnapshot:
    def setup_method(self):
        _setup()

    def test_snapshot_includes_held_directions_key(self):
        snapshot = get_pipeline_health_snapshot()
        assert "held_directions" in snapshot

    def test_snapshot_held_directions_populated(self):
        did = _fresh_direction_id()
        for _ in range(DIRECTION_HOLD_THRESHOLD):
            record_direction_result(did, success=False, failure_class="CODE_ERROR")
        snapshot = get_pipeline_health_snapshot()
        assert did in snapshot["held_directions"]

    def test_snapshot_empty_when_no_holds(self):
        snapshot = get_pipeline_health_snapshot()
        assert snapshot["held_directions"] == {}


# ---------------------------------------------------------------------------
# Integration tests — filter_held_directions
# ---------------------------------------------------------------------------

class TestFilterHeldDirections:
    def setup_method(self):
        _setup()

    @pytest.mark.asyncio
    async def test_task_without_direction_id_passes_through(self):
        project_id = uuid.uuid4()
        task = make_task(project_id=project_id, context={})
        session = MagicMock()
        broadcast_fn = AsyncMock()

        eligible = [task.id]
        result = await filter_held_directions(eligible, [task], session, broadcast_fn)
        assert result == eligible

    @pytest.mark.asyncio
    async def test_task_with_unheld_direction_passes_through(self):
        did = _fresh_direction_id()
        project_id = uuid.uuid4()
        task = make_task(project_id=project_id, context={"direction_id": did})
        # Only 1 failure — below threshold
        record_direction_result(did, success=False, failure_class="CODE_ERROR")
        session = MagicMock()
        broadcast_fn = AsyncMock()

        eligible = [task.id]
        result = await filter_held_directions(eligible, [task], session, broadcast_fn)
        assert result == eligible

    @pytest.mark.asyncio
    async def test_task_with_held_direction_deferred(self):
        did = _fresh_direction_id()
        project_id = uuid.uuid4()
        task = make_task(project_id=project_id, context={"direction_id": did})
        for _ in range(DIRECTION_HOLD_THRESHOLD):
            record_direction_result(did, success=False, failure_class="CODE_ERROR")
        session = MagicMock()
        broadcast_fn = AsyncMock()

        eligible = [task.id]
        result = await filter_held_directions(eligible, [task], session, broadcast_fn)
        assert result == []
        # Status message should be updated
        assert task.latest_status_message is not None
        assert "Held" in task.latest_status_message

    @pytest.mark.asyncio
    async def test_empty_eligible_ids_returns_empty(self):
        session = MagicMock()
        broadcast_fn = AsyncMock()
        result = await filter_held_directions([], [], session, broadcast_fn)
        assert result == []

    @pytest.mark.asyncio
    async def test_mixed_tasks_only_held_deferred(self):
        did_held = _fresh_direction_id()
        did_ok = _fresh_direction_id()
        project_id = uuid.uuid4()

        task_held = make_task(project_id=project_id, context={"direction_id": did_held})
        task_ok = make_task(project_id=project_id, context={"direction_id": did_ok})
        task_no_dir = make_task(project_id=project_id, context={})

        for _ in range(DIRECTION_HOLD_THRESHOLD):
            record_direction_result(did_held, success=False, failure_class="CODE_ERROR")
        record_direction_result(did_ok, success=False, failure_class="CODE_ERROR")  # only 1

        session = MagicMock()
        broadcast_fn = AsyncMock()

        eligible = [task_held.id, task_ok.id, task_no_dir.id]
        result = await filter_held_directions(
            eligible, [task_held, task_ok, task_no_dir], session, broadcast_fn
        )
        assert task_held.id not in result
        assert task_ok.id in result
        assert task_no_dir.id in result

    @pytest.mark.asyncio
    async def test_broadcast_called_on_deferral(self):
        did = _fresh_direction_id()
        project_id = uuid.uuid4()
        task = make_task(project_id=project_id, context={"direction_id": did})
        for _ in range(DIRECTION_HOLD_THRESHOLD):
            record_direction_result(did, success=False, failure_class="CODE_ERROR")
        session = MagicMock()
        broadcast_fn = AsyncMock()

        await filter_held_directions([task.id], [task], session, broadcast_fn)
        broadcast_fn.assert_called_once()

    @pytest.mark.asyncio
    async def test_investigate_task_exempt_from_hold(self):
        """Investigation tasks must pass through — they're the escape hatch for held directions."""
        did = _fresh_direction_id()
        project_id = uuid.uuid4()
        task = make_task(
            project_id=project_id,
            context={"direction_id": did},
            payloads=[{"type": "investigate"}],
        )
        for _ in range(DIRECTION_HOLD_THRESHOLD):
            record_direction_result(did, success=False, failure_class="CODE_ERROR")
        session = MagicMock()
        broadcast_fn = AsyncMock()

        eligible = [task.id]
        result = await filter_held_directions(eligible, [task], session, broadcast_fn)
        assert result == eligible
        broadcast_fn.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_duplicate_broadcast_when_message_unchanged(self):
        """If the status message is already set to the hold reason, don't re-broadcast."""
        did = _fresh_direction_id()
        project_id = uuid.uuid4()
        task = make_task(project_id=project_id, context={"direction_id": did})
        for _ in range(DIRECTION_HOLD_THRESHOLD):
            record_direction_result(did, success=False, failure_class="CODE_ERROR")
        _, reason = is_direction_held(did)
        task.latest_status_message = reason  # already set

        session = MagicMock()
        broadcast_fn = AsyncMock()

        await filter_held_directions([task.id], [task], session, broadcast_fn)
        broadcast_fn.assert_not_called()
