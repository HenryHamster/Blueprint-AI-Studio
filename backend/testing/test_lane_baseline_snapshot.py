"""Tests for lane_baseline_snapshot module."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest

from app.lane_baseline_snapshot import (
    DESIGN_DOC_KEY,
    _MAX_BASELINES_STORED,
    _compute_checkpoint_recovery_waste,
    _compute_relay_medians,
    backfill_missing_lane_baselines,
    persist_version_lane_baseline,
    read_version_lane_baseline,
)
from app.models import ReleaseVersion, TaskStatus
from app.review_fix_lane_pilot import LANE_RECOVERY_REASON_KEY
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


# ---------------------------------------------------------------------------
# Backfill tests
# ---------------------------------------------------------------------------


class TestBackfillMissingLaneBaselines:
    @pytest.mark.asyncio
    async def test_backfill_persists_missing_baselines(self, db_session):
        """backfill_missing_lane_baselines writes baselines for closed versions."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()

        now = _utcnow()
        v9_opened = now - timedelta(days=14)
        v9_promoted = now - timedelta(days=7)

        version = ReleaseVersion(
            project_id=project.id,
            version_number=9,
            status="promoted",
            opened_at=v9_opened,
            promoted_at=v9_promoted,
        )
        db_session.add(version)
        await db_session.commit()

        backfilled = await backfill_missing_lane_baselines(project_id=project.id)

        assert backfilled == [9]

        await db_session.refresh(project)
        stored = read_version_lane_baseline(project, 9)
        assert stored is not None
        assert stored["version_number"] == 9
        assert stored["terminal_status"] == "promoted"
        assert "relay_medians" in stored

    @pytest.mark.asyncio
    async def test_backfill_skips_already_stored_version(self, db_session):
        """backfill_missing_lane_baselines is a no-op for versions with existing baselines."""
        project = make_project()
        project.design_doc = {
            DESIGN_DOC_KEY: {
                "9": {"version_number": 9, "captured_at": "2026-01-01T00:00:00"}
            }
        }
        db_session.add(project)
        await db_session.commit()

        now = _utcnow()
        version = ReleaseVersion(
            project_id=project.id,
            version_number=9,
            status="promoted",
            opened_at=now - timedelta(days=14),
            promoted_at=now - timedelta(days=7),
        )
        db_session.add(version)
        await db_session.commit()

        backfilled = await backfill_missing_lane_baselines(project_id=project.id)

        assert backfilled == []
        # Original entry must be unchanged (first-write-wins)
        await db_session.refresh(project)
        stored = read_version_lane_baseline(project, 9)
        assert stored["captured_at"] == "2026-01-01T00:00:00"

    @pytest.mark.asyncio
    async def test_backfill_respects_max_versions_cap(self, db_session):
        """backfill only processes the most recent max_versions closed versions."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()

        now = _utcnow()
        for vn in range(1, 6):  # 5 closed versions
            v = ReleaseVersion(
                project_id=project.id,
                version_number=vn,
                status="promoted",
                opened_at=now - timedelta(days=10 + vn),
                promoted_at=now - timedelta(days=vn),
            )
            db_session.add(v)
        await db_session.commit()

        # Backfill with max_versions=3 — should only process the 3 most recent
        backfilled = await backfill_missing_lane_baselines(
            project_id=project.id, max_versions=3
        )

        assert len(backfilled) == 3
        assert set(backfilled) == {3, 4, 5}

    @pytest.mark.asyncio
    async def test_backfill_handles_abandoned_version_closed_at(self, db_session):
        """backfill derives closed_at from last_qualification["abandoned_at"] for abandoned versions."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()

        now = _utcnow()
        abandoned_iso = (now - timedelta(days=3)).isoformat()
        version = ReleaseVersion(
            project_id=project.id,
            version_number=7,
            status="abandoned",
            opened_at=now - timedelta(days=10),
            last_qualification={"abandoned_at": abandoned_iso, "reason": "operator"},
        )
        db_session.add(version)
        await db_session.commit()

        backfilled = await backfill_missing_lane_baselines(project_id=project.id)

        assert backfilled == [7]
        await db_session.refresh(project)
        stored = read_version_lane_baseline(project, 7)
        assert stored is not None
        assert stored["terminal_status"] == "abandoned"

    @pytest.mark.asyncio
    async def test_backfill_no_closed_versions_is_noop(self, db_session):
        """backfill returns an empty list when the project has no closed versions."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()

        # Only an open version
        now = _utcnow()
        version = ReleaseVersion(
            project_id=project.id,
            version_number=1,
            status="open",
            opened_at=now - timedelta(days=3),
        )
        db_session.add(version)
        await db_session.commit()

        backfilled = await backfill_missing_lane_baselines(project_id=project.id)

        assert backfilled == []

    @pytest.mark.asyncio
    async def test_backfill_unknown_project_returns_empty(self, db_session):
        """backfill returns an empty list without error for a nonexistent project."""
        backfilled = await backfill_missing_lane_baselines(project_id=uuid.uuid4())
        assert backfilled == []


# ---------------------------------------------------------------------------
# Checkpoint recovery waste tests
# ---------------------------------------------------------------------------


class TestComputeCheckpointRecoveryWaste:
    def test_no_recovery_tasks_returns_zero_totals(self):
        """A version with no recovery tasks produces zero counts."""
        tasks = [make_task(context={"review_loop": {"pr_number": "1"}})]
        result = _compute_checkpoint_recovery_waste(tasks)
        assert result == {"by_reason": {}, "total": 0, "exhausted_count": 0}

    def test_single_recovery_task_counted(self):
        """A single recovery task is counted under its reason."""
        tasks = [
            make_task(context={LANE_RECOVERY_REASON_KEY: "checkpoint_missing"}),
        ]
        result = _compute_checkpoint_recovery_waste(tasks)
        assert result["total"] == 1
        assert result["by_reason"] == {"checkpoint_missing": 1}
        assert result["exhausted_count"] == 0

    def test_multiple_tasks_same_reason_aggregated(self):
        """Multiple recovery tasks with the same reason are summed."""
        tasks = [
            make_task(context={LANE_RECOVERY_REASON_KEY: "checkpoint_missing"}),
            make_task(context={LANE_RECOVERY_REASON_KEY: "checkpoint_missing"}),
            make_task(context={"review_loop": {"pr_number": "99"}}),  # non-recovery
        ]
        result = _compute_checkpoint_recovery_waste(tasks)
        assert result["total"] == 2
        assert result["by_reason"] == {"checkpoint_missing": 2}

    def test_multiple_reasons_grouped_separately(self):
        """Tasks with distinct reason values form separate by_reason buckets."""
        tasks = [
            make_task(context={LANE_RECOVERY_REASON_KEY: "checkpoint_missing"}),
            make_task(context={LANE_RECOVERY_REASON_KEY: "some_other_reason"}),
        ]
        result = _compute_checkpoint_recovery_waste(tasks)
        assert result["total"] == 2
        assert result["by_reason"]["checkpoint_missing"] == 1
        assert result["by_reason"]["some_other_reason"] == 1

    def test_empty_task_list_returns_zero(self):
        """Empty task list returns all-zero result."""
        result = _compute_checkpoint_recovery_waste([])
        assert result == {"by_reason": {}, "total": 0, "exhausted_count": 0}

    def test_task_with_none_context_skipped(self):
        """Tasks with None context are skipped without error."""
        task = make_task()
        task.context = None
        result = _compute_checkpoint_recovery_waste([task])
        assert result["total"] == 0


class TestPersistCheckpointRecoveryWasteInBaseline:
    @pytest.mark.asyncio
    async def test_persist_includes_checkpoint_recovery_waste(self, db_session):
        """persist_version_lane_baseline folds checkpoint_recovery_waste into baseline."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()

        now = _utcnow()
        opened = now - timedelta(days=7)
        closed = now - timedelta(days=1)

        # Two recovery tasks in the version window
        for _ in range(2):
            task = make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                created_by="ORCHESTRATOR",
                payloads=[{"type": "fix", "prompt": "recover"}],
                context={LANE_RECOVERY_REASON_KEY: "checkpoint_missing"},
                created_at=opened + timedelta(hours=2),
                started_at=opened + timedelta(hours=2),
                status_log=[
                    {
                        "s": "COMPLETED",
                        "t": (opened + timedelta(hours=3)).isoformat(),
                    }
                ],
            )
            db_session.add(task)
        await db_session.commit()

        result = await persist_version_lane_baseline(
            db_session,
            project_id=project.id,
            version_number=10,
            opened_at=opened,
            closed_at=closed,
            terminal_status="promoted",
        )
        await db_session.commit()

        assert result is not None
        crw = result.get("checkpoint_recovery_waste")
        assert crw is not None
        assert crw["total"] == 2
        assert crw["by_reason"] == {"checkpoint_missing": 2}
        assert crw["exhausted_count"] == 0

    @pytest.mark.asyncio
    async def test_persist_no_recovery_tasks_zero_waste(self, db_session):
        """A version window with no recovery tasks produces a zero-waste block."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()

        now = _utcnow()

        result = await persist_version_lane_baseline(
            db_session,
            project_id=project.id,
            version_number=11,
            opened_at=now - timedelta(days=14),
            closed_at=now - timedelta(days=7),
            terminal_status="promoted",
        )
        await db_session.commit()

        assert result is not None
        crw = result.get("checkpoint_recovery_waste")
        assert crw is not None
        assert crw["total"] == 0
        assert crw["by_reason"] == {}
        assert crw["exhausted_count"] == 0
