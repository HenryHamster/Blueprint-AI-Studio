"""Integration tests for compute_lane_arm_time_medians in review_fix_lane_pilot."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.capability_trial_verdict import LANE_PILOT_ASSIGNMENT_KEY
from app.models import TaskStatus
from app.review_fix_lane_pilot import compute_lane_arm_time_medians
from testing.factories import make_project, make_task


def _dt(base: datetime, **delta_kwargs) -> datetime:
    """Return a timezone-naive datetime offset from base."""
    dt = base + timedelta(**delta_kwargs)
    return dt.replace(tzinfo=None)


class TestComputeLaneArmTimeMedians:
    @pytest.mark.asyncio
    async def test_empty_returns_pr_count_zero(self, db_session):
        """No lane-arm tasks → all medians None, pr_count=0."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()

        result = await compute_lane_arm_time_medians(db_session, project.id)

        assert result["pr_count"] == 0
        assert result["wall_clock_seconds"] is None
        assert result["active_seconds"] is None
        assert result["gap_seconds"] is None
        assert result["task_count"] is None

    @pytest.mark.asyncio
    async def test_single_pr_two_tasks_correct_medians(self, db_session):
        """Two completed lane-arm tasks on one PR produce correct per-PR medians."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()

        base = datetime(2024, 1, 1, 12, 0, 0)

        # Task 1: created at base, started +10s, finished +70s → active=60s
        t1 = make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            context={
                LANE_PILOT_ASSIGNMENT_KEY: "lane",
                "review_loop": {"pr_number": "42"},
            },
            created_at=_dt(base),
            started_at=_dt(base, seconds=10),
            updated_at=_dt(base, seconds=70),
        )
        # Task 2: created at base+100s, started +110s, finished +150s → active=40s
        t2 = make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            context={
                LANE_PILOT_ASSIGNMENT_KEY: "lane",
                "review_loop": {"pr_number": "42"},
            },
            created_at=_dt(base, seconds=100),
            started_at=_dt(base, seconds=110),
            updated_at=_dt(base, seconds=150),
        )
        db_session.add(t1)
        db_session.add(t2)
        await db_session.commit()

        result = await compute_lane_arm_time_medians(db_session, project.id)

        assert result["pr_count"] == 1
        # wall_clock = max(updated) - min(created) = 150 - 0 = 150s
        assert result["wall_clock_seconds"] == 150.0
        # active = (70-10) + (150-110) = 60 + 40 = 100s
        assert result["active_seconds"] == 100.0
        # gap = wall_clock - active = 150 - 100 = 50s
        assert result["gap_seconds"] == 50.0
        # task_count median for a single PR with 2 tasks = 2.0
        assert result["task_count"] == 2.0

    @pytest.mark.asyncio
    async def test_multiple_prs_median_across_prs(self, db_session):
        """Three PRs produce the median of per-PR wall_clock values."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()

        base = datetime(2024, 1, 1, 12, 0, 0)

        # PR 1: wall_clock = 100s
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                context={
                    LANE_PILOT_ASSIGNMENT_KEY: "lane",
                    "review_loop": {"pr_number": "1"},
                },
                created_at=_dt(base),
                started_at=_dt(base, seconds=5),
                updated_at=_dt(base, seconds=100),
            )
        )
        # PR 2: wall_clock = 200s
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                context={
                    LANE_PILOT_ASSIGNMENT_KEY: "lane",
                    "review_loop": {"pr_number": "2"},
                },
                created_at=_dt(base),
                started_at=_dt(base, seconds=5),
                updated_at=_dt(base, seconds=200),
            )
        )
        # PR 3: wall_clock = 300s
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                context={
                    LANE_PILOT_ASSIGNMENT_KEY: "lane",
                    "review_loop": {"pr_number": "3"},
                },
                created_at=_dt(base),
                started_at=_dt(base, seconds=5),
                updated_at=_dt(base, seconds=300),
            )
        )
        await db_session.commit()

        result = await compute_lane_arm_time_medians(db_session, project.id)

        assert result["pr_count"] == 3
        # Sorted wall_clocks: [100, 200, 300] → median = 200
        assert result["wall_clock_seconds"] == 200.0

    @pytest.mark.asyncio
    async def test_ignores_non_lane_arm_tasks(self, db_session):
        """Tasks assigned to 'relay' arm are excluded from the result."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()

        base = datetime(2024, 1, 1, 12, 0, 0)

        # Relay-arm task (should be ignored)
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                context={
                    LANE_PILOT_ASSIGNMENT_KEY: "relay",
                    "review_loop": {"pr_number": "99"},
                },
                created_at=_dt(base),
                started_at=_dt(base, seconds=5),
                updated_at=_dt(base, seconds=500),
            )
        )
        await db_session.commit()

        result = await compute_lane_arm_time_medians(db_session, project.id)
        assert result["pr_count"] == 0
        assert result["wall_clock_seconds"] is None

    @pytest.mark.asyncio
    async def test_ignores_non_terminal_tasks(self, db_session):
        """IN_PROGRESS / QUEUED tasks are excluded; only terminal statuses count."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()

        base = datetime(2024, 1, 1, 12, 0, 0)

        # QUEUED lane task — should be excluded
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.QUEUED,
                context={
                    LANE_PILOT_ASSIGNMENT_KEY: "lane",
                    "review_loop": {"pr_number": "7"},
                },
                created_at=_dt(base),
                started_at=None,
                updated_at=_dt(base, seconds=10),
            )
        )
        # Completed lane task on a different PR — should be included
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                context={
                    LANE_PILOT_ASSIGNMENT_KEY: "lane",
                    "review_loop": {"pr_number": "8"},
                },
                created_at=_dt(base),
                started_at=_dt(base, seconds=5),
                updated_at=_dt(base, seconds=120),
            )
        )
        await db_session.commit()

        result = await compute_lane_arm_time_medians(db_session, project.id)
        assert result["pr_count"] == 1
        assert result["wall_clock_seconds"] == 120.0

    @pytest.mark.asyncio
    async def test_isolates_by_project(self, db_session):
        """Tasks from a different project are not counted."""
        project = make_project()
        other_project = make_project()
        db_session.add(project)
        db_session.add(other_project)
        await db_session.commit()

        base = datetime(2024, 1, 1, 12, 0, 0)

        # Lane task belonging to other_project
        db_session.add(
            make_task(
                project_id=other_project.id,
                status=TaskStatus.COMPLETED,
                context={
                    LANE_PILOT_ASSIGNMENT_KEY: "lane",
                    "review_loop": {"pr_number": "55"},
                },
                created_at=_dt(base),
                started_at=_dt(base, seconds=5),
                updated_at=_dt(base, seconds=300),
            )
        )
        await db_session.commit()

        result = await compute_lane_arm_time_medians(db_session, project.id)
        assert result["pr_count"] == 0

    @pytest.mark.asyncio
    async def test_failed_and_cancelled_tasks_included(self, db_session):
        """FAILED and CANCELLED are terminal statuses and should be included."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()

        base = datetime(2024, 1, 1, 12, 0, 0)

        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.FAILED,
                context={
                    LANE_PILOT_ASSIGNMENT_KEY: "lane",
                    "review_loop": {"pr_number": "10"},
                },
                created_at=_dt(base),
                started_at=_dt(base, seconds=5),
                updated_at=_dt(base, seconds=80),
            )
        )
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.CANCELLED,
                context={
                    LANE_PILOT_ASSIGNMENT_KEY: "lane",
                    "review_loop": {"pr_number": "11"},
                },
                created_at=_dt(base),
                started_at=_dt(base, seconds=3),
                updated_at=_dt(base, seconds=60),
            )
        )
        await db_session.commit()

        result = await compute_lane_arm_time_medians(db_session, project.id)
        assert result["pr_count"] == 2
        # Sorted wall_clocks: [60, 80] → median = (60+80)/2 = 70
        assert result["wall_clock_seconds"] == 70.0
