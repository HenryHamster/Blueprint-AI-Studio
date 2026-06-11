"""Tests for lane_baseline_snapshot module."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest

from app.lane_baseline_snapshot import (
    DESIGN_DOC_KEY,
    _compute_relay_medians,
    persist_version_lane_baseline,
    read_version_lane_baseline,
)
from app.models import TaskStatus
from app.time_utils import _utcnow
from testing.factories import make_project, make_task


# ---------------------------------------------------------------------------
# Pure-function tests (no DB)
# ---------------------------------------------------------------------------


class TestComputeRelayMedians:
    def test_empty_returns_none_medians(self):
        result = _compute_relay_medians([])
        assert result["wall_clock_seconds"] is None
        assert result["active_seconds"] is None
        assert result["gap_seconds"] is None
        assert result["task_count"] is None
        assert result["pr_count"] == 0

    def test_single_pr_returns_that_pr_values(self):
        breakdown = [
            {
                "pr_number": "101",
                "task_count": 3,
                "review_seconds": 600.0,
                "fix_seconds": 1200.0,
                "gap_seconds": 300.0,
                "total_seconds": 2100.0,
                "queue_seconds": 0.0,
                "provision_seconds": 0.0,
                "cold_start_seconds": 0.0,
                "execution_seconds": 0.0,
            }
        ]
        result = _compute_relay_medians(breakdown)
        assert result["wall_clock_seconds"] == 2100.0
        assert result["active_seconds"] == 1800.0  # 600 + 1200
        assert result["gap_seconds"] == 300.0
        assert result["task_count"] == 3.0
        assert result["pr_count"] == 1

    def test_odd_count_median(self):
        breakdown = [
            {
                "pr_number": str(i),
                "task_count": i,
                "review_seconds": float(i * 100),
                "fix_seconds": 0.0,
                "gap_seconds": float(i * 10),
                "total_seconds": float(i * 110),
                "queue_seconds": 0.0,
                "provision_seconds": 0.0,
                "cold_start_seconds": 0.0,
                "execution_seconds": 0.0,
            }
            for i in [1, 2, 3]
        ]
        result = _compute_relay_medians(breakdown)
        # Median of [1,2,3] total_seconds (sorted) * 110 => 220
        assert result["wall_clock_seconds"] == 220.0

    def test_even_count_median_averages_middle_two(self):
        breakdown = [
            {
                "pr_number": str(i),
                "task_count": 1,
                "review_seconds": float(i * 100),
                "fix_seconds": 0.0,
                "gap_seconds": 0.0,
                "total_seconds": float(i * 100),
                "queue_seconds": 0.0,
                "provision_seconds": 0.0,
                "cold_start_seconds": 0.0,
                "execution_seconds": 0.0,
            }
            for i in [1, 2, 3, 4]
        ]
        result = _compute_relay_medians(breakdown)
        # Sorted: 100, 200, 300, 400 → median = (200+300)/2 = 250
        assert result["wall_clock_seconds"] == 250.0
        assert result["pr_count"] == 4


class TestReadVersionLaneBaseline:
    def test_returns_none_when_no_design_doc(self):
        project = make_project()
        project.design_doc = None
        assert read_version_lane_baseline(project, 9) is None

    def test_returns_none_when_key_missing(self):
        project = make_project()
        project.design_doc = {"other_key": {}}
        assert read_version_lane_baseline(project, 9) is None

    def test_returns_none_for_different_version(self):
        project = make_project()
        project.design_doc = {DESIGN_DOC_KEY: {"8": {"version_number": 8}}}
        assert read_version_lane_baseline(project, 9) is None

    def test_returns_stored_baseline_for_matching_version(self):
        baseline = {"version_number": 9, "pr_count": 5}
        project = make_project()
        project.design_doc = {DESIGN_DOC_KEY: {"9": baseline}}
        result = read_version_lane_baseline(project, 9)
        assert result == baseline


# ---------------------------------------------------------------------------
# DB-backed tests
# ---------------------------------------------------------------------------


class TestPersistVersionLaneBaseline:
    @pytest.mark.asyncio
    async def test_persist_writes_baseline_to_design_doc(self, db_session):
        """persist_version_lane_baseline stores a non-None dict in design_doc."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()

        now = _utcnow()
        opened = now - timedelta(days=7)
        closed = now - timedelta(days=1)

        # Add a review-loop task in the window
        task = make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
            payloads=[{"type": "review", "prompt": "x"}],
            context={"review_loop": {"pr_number": "101"}},
            created_at=opened + timedelta(days=1),
            started_at=opened + timedelta(days=1),
            status_log=[
                {
                    "s": "COMPLETED",
                    "t": (opened + timedelta(days=1, hours=1)).isoformat(),
                }
            ],
        )
        db_session.add(task)
        await db_session.commit()

        result = await persist_version_lane_baseline(
            db_session,
            project_id=project.id,
            version_number=9,
            opened_at=opened,
            closed_at=closed,
            terminal_status="promoted",
        )
        await db_session.commit()

        assert result is not None
        assert result["version_number"] == 9
        assert result["terminal_status"] == "promoted"
        assert "lane_iteration_distribution" in result
        assert "relay_counts_by_lane" in result
        assert "lane_waste" in result
        assert "relay_medians" in result
        assert result["pr_count"] == 1

        # Verify it landed in design_doc
        await db_session.refresh(project)
        stored = read_version_lane_baseline(project, 9)
        assert stored is not None
        assert stored["pr_count"] == 1

    @pytest.mark.asyncio
    async def test_persist_first_write_wins(self, db_session):
        """A second persist call for the same version returns the original baseline."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()

        now = _utcnow()
        opened = now - timedelta(days=7)
        closed = now - timedelta(days=1)

        first = await persist_version_lane_baseline(
            db_session,
            project_id=project.id,
            version_number=9,
            opened_at=opened,
            closed_at=closed,
            terminal_status="promoted",
        )
        await db_session.commit()
        first_captured_at = first["captured_at"]

        # Second call should return the already-stored entry unchanged
        second = await persist_version_lane_baseline(
            db_session,
            project_id=project.id,
            version_number=9,
            opened_at=opened,
            closed_at=closed,
            terminal_status="promoted",
        )
        assert second is not None
        assert second["captured_at"] == first_captured_at

    @pytest.mark.asyncio
    async def test_persist_no_tasks_produces_zero_counts(self, db_session):
        """A version window with no tasks stores a valid zero-count baseline."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()

        now = _utcnow()

        result = await persist_version_lane_baseline(
            db_session,
            project_id=project.id,
            version_number=5,
            opened_at=now - timedelta(days=30),
            closed_at=now - timedelta(days=23),
            terminal_status="abandoned",
        )
        await db_session.commit()

        assert result is not None
        assert result["pr_count"] == 0
        assert result["task_count"] == 0
        assert result["terminal_status"] == "abandoned"
        relay_medians = result["relay_medians"]
        assert relay_medians["pr_count"] == 0
        assert relay_medians["wall_clock_seconds"] is None

    @pytest.mark.asyncio
    async def test_persist_history_cap_evicts_oldest(self, db_session):
        """Storing more than _MAX_BASELINES_STORED evicts the oldest entries."""
        from app.lane_baseline_snapshot import _MAX_BASELINES_STORED

        project = make_project()
        db_session.add(project)
        await db_session.commit()

        now = _utcnow()

        # Pre-populate with _MAX_BASELINES_STORED entries directly
        doc = {
            DESIGN_DOC_KEY: {
                str(i): {"version_number": i, "captured_at": now.isoformat()}
                for i in range(1, _MAX_BASELINES_STORED + 1)
            }
        }
        project.design_doc = doc
        db_session.add(project)
        await db_session.commit()

        # Adding one more should evict version 1
        new_version = _MAX_BASELINES_STORED + 1
        await persist_version_lane_baseline(
            db_session,
            project_id=project.id,
            version_number=new_version,
            opened_at=now - timedelta(days=2),
            closed_at=now - timedelta(days=1),
            terminal_status="promoted",
        )
        await db_session.commit()

        await db_session.refresh(project)
        baselines = project.design_doc.get(DESIGN_DOC_KEY, {})
        assert len(baselines) == _MAX_BASELINES_STORED
        assert "1" not in baselines
        assert str(new_version) in baselines

    @pytest.mark.asyncio
    async def test_persist_for_nonexistent_project_returns_none(self, db_session):
        """Persisting for a project that doesn't exist returns None without error."""
        now = _utcnow()
        result = await persist_version_lane_baseline(
            db_session,
            project_id=uuid.uuid4(),
            version_number=1,
            opened_at=now - timedelta(days=7),
            closed_at=now,
            terminal_status="promoted",
        )
        assert result is None
