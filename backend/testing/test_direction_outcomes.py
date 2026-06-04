"""Tests for _update_direction_research_outcomes in direction_outcomes.py.

Covers:
1. Guard conditions (missing project, direction not found, invalid UUID, no children, task_id not in history)
2. Status aggregation (tasks_completed, tasks_failed, tasks_cancelled)
3. PR metrics (prs_merged, prs_rejected, conversion_rate)
4. Time-to-merge calculation (avg_time_to_merge_hours)
5. Failure categorization (infra vs non-infra, failed_task_details cap, reason truncation)
6. Scope breakdown per-scope created/merged/failed counters
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.direction_outcomes import (
    _counts_as_failure,
    _is_cascade_cancelled,
    _is_non_genuine_cancelled,
    _longest_chain_length,
    _refresh_pipeline_scope_merge_rates,
    _update_cochange_index,
    _update_direction_research_outcomes,
    refresh_direction_outcomes_after_merge,
)
from app.models import Artifact, ArtifactType, Project, Task, TaskStatus
from app.shared_state import runtime_config


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _create_project(
    session: AsyncSession, directions: list | None = None
) -> Project:
    """Create a minimal project with optional directions list."""
    project = Project(
        name="Test Project",
        user_id="test-user-123",
        directions=directions or [],
    )
    session.add(project)
    await session.flush()
    await session.refresh(project)
    return project


def _make_direction(
    direction_id: str, research_task_id: str, tasks_created: int = 3
) -> dict:
    """Build a minimal direction with one research_history entry."""
    return {
        "id": direction_id,
        "text": "Improve test coverage",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "research_history": [
            {
                "task_id": research_task_id,
                "tasks_created": tasks_created,
                "cycle": 1,
            }
        ],
    }


async def _create_child_task(
    session: AsyncSession,
    parent_id: uuid.UUID,
    project_id: uuid.UUID,
    *,
    status: TaskStatus = TaskStatus.COMPLETED,
    context: dict | None = None,
    payloads: list | None = None,
    title: str = "Child task",
    latest_status_message: str | None = None,
    created_at: datetime | None = None,
    vm_stopped_at: datetime | None = None,
    created_by: str = "ORCHESTRATOR",
) -> Task:
    task = Task(
        project_id=project_id,
        parent_task_id=parent_id,
        title=title,
        payloads=payloads or [{"type": "create", "scope": "small"}],
        context=context or {},
        status=status,
        created_by=created_by,
        latest_status_message=latest_status_message,
    )
    if created_at is not None:
        task.created_at = created_at
    if vm_stopped_at is not None:
        task.vm_stopped_at = vm_stopped_at
    session.add(task)
    await session.flush()
    return task


async def _make_research_task(session: AsyncSession, project_id: uuid.UUID) -> Task:
    """Create a placeholder research task row (parent)."""
    task = Task(
        project_id=project_id,
        title="Research: improve things",
        payloads=[{"type": "research"}],
        context={},
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
    )
    session.add(task)
    await session.flush()
    return task


# ─────────────────────────────────────────────────────────────────────────────
# 1. Guard conditions — early return / break without committing
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_guard_missing_project(db_session: AsyncSession):
    """Returns early when project_id does not exist."""
    # Should not raise; just returns None
    await _update_direction_research_outcomes(
        project_id=uuid.uuid4(),
        direction_id="d-missing",
        research_task_id=str(uuid.uuid4()),
        session=db_session,
    )


@pytest.mark.asyncio
async def test_guard_direction_not_found(db_session: AsyncSession):
    """Returns early when no direction matches direction_id."""
    direction_id = "d-exists"
    research_task_id = str(uuid.uuid4())
    project = await _create_project(
        db_session, directions=[_make_direction(direction_id, research_task_id)]
    )

    # Call with a non-existent direction_id
    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id="d-does-not-exist",
        research_task_id=research_task_id,
        session=db_session,
    )
    # Project should be unchanged (no commit happened) — we just verify no error
    await db_session.refresh(project)
    assert project.directions[0]["id"] == direction_id


@pytest.mark.asyncio
async def test_guard_research_task_id_not_in_history(db_session: AsyncSession):
    """Breaks without updating when task_id not in research_history."""
    direction_id = "d-history"
    history_task_id = str(uuid.uuid4())
    project = await _create_project(
        db_session, directions=[_make_direction(direction_id, history_task_id)]
    )

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=str(uuid.uuid4()),  # different from history entry
        session=db_session,
    )
    await db_session.refresh(project)
    # No tasks_completed key should have been added
    assert "tasks_completed" not in project.directions[0]["research_history"][0]


@pytest.mark.asyncio
async def test_guard_invalid_uuid_in_history(db_session: AsyncSession):
    """Breaks when task_id in history entry is not a valid UUID."""
    direction_id = "d-baduuid"
    bad_task_id = "not-a-uuid"
    project = await _create_project(
        db_session,
        directions=[
            {
                "id": direction_id,
                "text": "Test",
                "status": "active",
                "priority": "medium",
                "paths": [],
                "research_history": [{"task_id": bad_task_id, "tasks_created": 1}],
            }
        ],
    )

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=bad_task_id,
        session=db_session,
    )
    await db_session.refresh(project)
    assert "tasks_completed" not in project.directions[0]["research_history"][0]


@pytest.mark.asyncio
async def test_guard_no_child_tasks(db_session: AsyncSession):
    """Breaks without updating when research task has no child tasks."""
    direction_id = "d-nochildren"
    project = await _create_project(
        db_session, directions=[_make_direction(direction_id, str(uuid.uuid4()))]
    )
    # Use a valid UUID that has no child tasks
    fresh_task_id = str(uuid.uuid4())
    project.directions[0]["research_history"][0]["task_id"] = fresh_task_id
    session_project = await db_session.get(Project, project.id)
    session_project.directions = project.directions
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=fresh_task_id,
        session=db_session,
    )
    await db_session.refresh(project)
    assert "tasks_completed" not in project.directions[0]["research_history"][0]


# ─────────────────────────────────────────────────────────────────────────────
# 2. Status aggregation
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_status_aggregation_mixed(db_session: AsyncSession):
    """tasks_completed/failed/cancelled are counted from mixed-status children."""
    direction_id = "d-mixed"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)

    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=5)
    ]
    await db_session.flush()

    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        title="c1",
    )
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        title="c2",
    )
    await _create_child_task(
        db_session, research_task.id, project.id, status=TaskStatus.FAILED, title="c3"
    )
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.CANCELLED,
        title="c4",
    )
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.IN_PROGRESS,
        title="c5",
    )
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.BLOCKED,
        title="c6",
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]
    assert entry["tasks_completed"] == 2
    assert entry["tasks_failed"] == 1
    assert entry["tasks_cancelled"] == 1
    # BLOCKED is counted in its own bucket — NOT folded into the failure tally.
    assert entry["tasks_blocked"] == 1


def test_blocked_status_does_not_count_as_failure():
    """A BLOCKED task is non-terminal — it must never count as a direction failure
    (which would inflate failure rates and falsely consume retry budget)."""
    blocked = Task(
        project_id=uuid.uuid4(),
        title="t",
        description="d",
        status=TaskStatus.BLOCKED,
        created_by="u",
        payloads=[{"type": "fix"}],
        context={},
    )
    assert _counts_as_failure(blocked) is False
    failed = Task(
        project_id=uuid.uuid4(),
        title="t",
        description="d",
        status=TaskStatus.FAILED,
        created_by="u",
        payloads=[{"type": "fix"}],
        context={},
    )
    assert _counts_as_failure(failed) is True


# ─────────────────────────────────────────────────────────────────────────────
# 3. PR metrics
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pr_metrics_basic(db_session: AsyncSession):
    """prs_merged and prs_rejected counted from context flags."""
    direction_id = "d-prs"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)

    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=4)
    ]
    await db_session.flush()

    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        title="merged1",
    )
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        title="merged2",
    )
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={"pr_rejected": True},
        title="rejected1",
    )
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={},
        title="no-pr",
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]
    assert entry["prs_merged"] == 2
    assert entry["prs_rejected"] == 1
    # conversion_rate = 2 merged / 4 tasks_created
    assert entry["conversion_rate"] == 0.5


@pytest.mark.asyncio
async def test_pr_metrics_exclude_non_merge_unblocks(db_session: AsyncSession):
    """Staging/no-op success paths should not inflate prs_merged analytics."""
    direction_id = "d-prs-non-merge"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)

    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=2)
    ]
    await db_session.flush()

    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True, "landed_on_integration": True},
        title="landed",
    )
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True, "landed_on_integration": False},
        title="staging-only-success",
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]
    assert entry["prs_merged"] == 1
    assert entry["conversion_rate"] == 0.5


@pytest.mark.asyncio
async def test_conversion_rate_division_by_zero(db_session: AsyncSession):
    """conversion_rate is None when tasks_created is 0."""
    direction_id = "d-zerodiv"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)

    # tasks_created = 0
    direction = _make_direction(direction_id, research_task_id, tasks_created=0)
    project.directions = [direction]
    await db_session.flush()

    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        title="merged",
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]
    assert entry["conversion_rate"] is None


@pytest.mark.asyncio
async def test_rejected_and_review_counts_require_completed_status(
    db_session: AsyncSession,
):
    """prs_rejected, reviews_dismissed, reviews_overridden exclude non-COMPLETED tasks."""
    direction_id = "d-status-filter"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)

    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=6)
    ]
    await db_session.flush()

    # COMPLETED — should count
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={
            "pr_rejected": True,
            "review_dismissed": True,
            "user_overrode_review": True,
        },
        title="ok",
    )
    # FAILED with the same flags — should NOT count
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.FAILED,
        context={
            "pr_rejected": True,
            "review_dismissed": True,
            "user_overrode_review": True,
        },
        title="failed",
    )
    # CANCELLED with flags — should NOT count
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.CANCELLED,
        context={
            "pr_rejected": True,
            "review_dismissed": True,
            "user_overrode_review": True,
        },
        title="cancelled",
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]
    assert entry["prs_rejected"] == 1
    assert entry["reviews_dismissed"] == 1
    assert entry["reviews_overridden"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# 4. Time-to-merge calculation
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_avg_time_to_merge_hours(db_session: AsyncSession):
    """avg_time_to_merge_hours is mean of (vm_stopped_at - created_at) for merged tasks."""
    direction_id = "d-ttm"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)

    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=2)
    ]
    await db_session.flush()

    base = _utcnow()
    # Task 1: 2 hours
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        created_at=base,
        vm_stopped_at=base + timedelta(hours=2),
        title="t1",
    )
    # Task 2: 4 hours
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        created_at=base,
        vm_stopped_at=base + timedelta(hours=4),
        title="t2",
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]
    assert entry["avg_time_to_merge_hours"] == 3.0


@pytest.mark.asyncio
async def test_avg_time_to_merge_excludes_null_timestamps(db_session: AsyncSession):
    """Tasks with null vm_stopped_at or created_at are excluded from merge time calc."""
    direction_id = "d-nullts"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)

    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=2)
    ]
    await db_session.flush()

    base = _utcnow()
    # One valid merged task: 3 hours
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        created_at=base,
        vm_stopped_at=base + timedelta(hours=3),
        title="valid",
    )
    # One merged task with no vm_stopped_at
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        title="no-stop-time",
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]
    assert entry["avg_time_to_merge_hours"] == 3.0


@pytest.mark.asyncio
async def test_avg_time_to_merge_none_when_no_merged(db_session: AsyncSession):
    """avg_time_to_merge_hours is None when no tasks have pr_merged."""
    direction_id = "d-nomerge"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)

    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=1)
    ]
    await db_session.flush()

    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.FAILED,
        context={},
        title="failed",
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]
    assert entry["avg_time_to_merge_hours"] is None


@pytest.mark.asyncio
async def test_implementation_and_review_timing_split(db_session: AsyncSession):
    """avg_implementation_hours covers all completed; avg_review_hours uses review child's vm_stopped_at."""
    direction_id = "d-split"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)

    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=2)
    ]
    await db_session.flush()

    base = _utcnow()
    # Merged create task: 2h implementation
    merged = await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True, "pr_number": 42},
        created_at=base,
        vm_stopped_at=base + timedelta(hours=2),
        title="impl",
    )
    # Completed-but-unmerged create task: 4h implementation, contributes only to impl avg
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={},
        created_at=base,
        vm_stopped_at=base + timedelta(hours=4),
        title="impl-only",
    )
    # Review child for PR 42: vm_stopped 1h after merged.vm_stopped_at
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "review", "pr_number": 42}],
        context={"pr_number": 42},
        created_at=base + timedelta(hours=2),
        vm_stopped_at=base + timedelta(hours=3),
        title="review",
    )
    await db_session.flush()
    # Ensure merged's updated_at doesn't interfere
    merged.updated_at = base + timedelta(hours=10)
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]
    # Mean across all completed children: impl=2h, impl-only=4h, review=1h
    assert entry["avg_implementation_hours"] == 2.33
    assert entry["avg_review_hours"] == 1.0  # 3h - 2h via review child


@pytest.mark.asyncio
async def test_avg_review_hours_falls_back_to_updated_at(db_session: AsyncSession):
    """When no review child exists, avg_review_hours uses merged task's updated_at."""
    direction_id = "d-rev-fallback"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)

    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=1)
    ]
    await db_session.flush()

    base = _utcnow()
    merged = await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True, "pr_number": 99},
        created_at=base,
        vm_stopped_at=base + timedelta(hours=1),
        title="merged-no-review",
    )
    await db_session.flush()
    merged.updated_at = base + timedelta(hours=2, minutes=30)
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]
    assert entry["avg_implementation_hours"] == 1.0
    assert entry["avg_review_hours"] == 1.5


@pytest.mark.asyncio
async def test_timing_fields_none_when_no_data(db_session: AsyncSession):
    """Both new timing fields are None when no qualifying tasks exist."""
    direction_id = "d-no-timing"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)

    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=1)
    ]
    await db_session.flush()

    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.FAILED,
        context={},
        title="failed",
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]
    assert entry["avg_implementation_hours"] is None
    assert entry["avg_review_hours"] is None


# ─────────────────────────────────────────────────────────────────────────────
# 5. Failure categorization
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_infra_vs_non_infra_failure_counting(db_session: AsyncSession):
    """tasks_failed_infra counts only INFRA_CATEGORIES; failed_task_details includes all failed/cancelled."""
    direction_id = "d-infra"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)

    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=4)
    ]
    await db_session.flush()

    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.FAILED,
        context={"failure_category": "PROVISION_FAILURE"},
        title="infra1",
        latest_status_message="VM failed",
    )
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.FAILED,
        context={"failure_category": "RATE_LIMIT"},
        title="infra2",
        latest_status_message="Rate limited",
    )
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.FAILED,
        context={"failure_category": "CODE_ERROR"},
        title="noinfra1",
        latest_status_message="Code error",
    )
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.CANCELLED,
        context={"failure_category": "AGENT_TIMEOUT"},
        title="infra3",
        latest_status_message="Timed out",
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]
    # PROVISION_FAILURE, RATE_LIMIT, AGENT_TIMEOUT are infra; CODE_ERROR is not
    assert entry["tasks_failed_infra"] == 3
    assert len(entry["failed_task_details"]) == 4


@pytest.mark.asyncio
async def test_failed_task_details_capped_at_5(db_session: AsyncSession):
    """failed_task_details is capped at 5 entries even when more tasks fail."""
    direction_id = "d-cap"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)

    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=8)
    ]
    await db_session.flush()

    for i in range(8):
        await _create_child_task(
            db_session,
            research_task.id,
            project.id,
            status=TaskStatus.FAILED,
            context={},
            title=f"failed-{i}",
            latest_status_message=f"Error {i}",
        )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]
    assert len(entry["failed_task_details"]) == 5


@pytest.mark.asyncio
async def test_failed_task_reason_truncated_to_120(db_session: AsyncSession):
    """reason in failed_task_details is truncated to 120 characters."""
    direction_id = "d-truncate"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)

    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=1)
    ]
    await db_session.flush()

    long_message = "X" * 200
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.FAILED,
        context={},
        title="long-fail",
        latest_status_message=long_message,
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]
    assert len(entry["failed_task_details"][0]["reason"]) == 120


@pytest.mark.asyncio
async def test_taxonomy_fields_in_outcome_entries(db_session: AsyncSession):
    """Each failed_task_details entry has a 'taxonomy' label; taxonomy_summary counts
    all failed tasks across the 4 categories (not just the capped 5)."""
    direction_id = "d-taxonomy"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)

    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=8)
    ]
    await db_session.flush()

    # infra_related: PROVISION_FAILURE
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.FAILED,
        context={"failure_category": "PROVISION_FAILURE"},
        title="infra1",
        latest_status_message="VM failed",
    )
    # infra_related: AGENT_TIMEOUT
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.CANCELLED,
        context={"failure_category": "AGENT_TIMEOUT"},
        title="infra2",
        latest_status_message="Timed out",
    )
    # review_quality: CODE_ERROR with pytest signal
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.FAILED,
        context={"failure_category": "CODE_ERROR", "scope": "small"},
        title="rq1",
        latest_status_message="pytest failed in tests",
    )
    # scope_related: CODE_ERROR with positive scope evidence (status-msg keyword)
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.FAILED,
        context={
            "failure_category": "CODE_ERROR",
            "scope": "medium",
            "scope_calibrated_from": "small",
        },
        title="sc1",
        latest_status_message="task exceeds scope budget",
    )
    # code_defect: CODE_ERROR small with no classifying signal
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.FAILED,
        context={"failure_category": "CODE_ERROR", "scope": "small"},
        title="cd1",
        latest_status_message="unknown failure",
    )
    # Extra failures to push total above the cap of 5
    for i in range(3):
        await _create_child_task(
            db_session,
            research_task.id,
            project.id,
            status=TaskStatus.FAILED,
            context={"failure_category": "PROVISION_FAILURE"},
            title=f"extra-{i}",
            latest_status_message="vm crashed",
        )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]
    # failed_task_details remains capped at 5 with taxonomy on each
    assert len(entry["failed_task_details"]) == 5
    for d in entry["failed_task_details"]:
        assert "taxonomy" in d
        assert d["taxonomy"] in {
            "scope_related",
            "review_quality",
            "infra_related",
            "code_defect",
        }
    # taxonomy_summary counts ALL 8 failed tasks, not just capped 5
    ts = entry["taxonomy_summary"]
    assert ts["infra_related"] == 5  # 1 provision + 1 agent_timeout + 3 extra provision
    assert ts["review_quality"] == 1
    assert ts["scope_related"] == 1
    assert ts["code_defect"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# 6. Scope breakdown
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_scope_breakdown_per_scope_counters(db_session: AsyncSession):
    """scope_breakdown tracks created/merged/failed per scope."""
    direction_id = "d-scope"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)

    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=4)
    ]
    await db_session.flush()

    # small: 2 created, 1 merged, 0 failed
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True, "scope": "small"},
        payloads=[{"type": "create"}],
        title="s1",
    )
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={"scope": "small"},
        payloads=[{"type": "create"}],
        title="s2",
    )
    # medium: 1 created, 0 merged, 1 failed
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.FAILED,
        context={"scope": "medium"},
        payloads=[{"type": "create"}],
        title="m1",
    )
    # no scope → unknown
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={},  # no scope key
        payloads=[{"type": "create"}],
        title="u1",
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]
    sb = entry["scope_breakdown"]

    assert sb["small"]["created"] == 2
    assert sb["small"]["merged"] == 1
    assert sb["small"]["failed"] == 0

    assert sb["medium"]["created"] == 1
    assert sb["medium"]["merged"] == 0
    assert sb["medium"]["failed"] == 1

    assert sb["unknown"]["created"] == 1


@pytest.mark.asyncio
async def test_scope_breakdown_tracks_calibration(db_session: AsyncSession):
    """A task with scope=small + scope_calibrated_from=medium increments small.calibrated_in
    and medium.calibrated_out so consumers see scope ambition vs effective scope."""
    direction_id = "d-calibrate"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)

    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=2)
    ]
    await db_session.flush()

    # Calibrated task: proposed medium, ran as small, merged
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={
            "pr_merged": True,
            "scope": "small",
            "scope_calibrated_from": "medium",
        },
        payloads=[{"type": "create"}],
        title="calibrated",
    )
    # Non-calibrated small task
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={"scope": "small"},
        payloads=[{"type": "create"}],
        title="plain-small",
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    sb = project.directions[0]["research_history"][0]["scope_breakdown"]
    assert sb["small"]["created"] == 2
    assert sb["small"]["calibrated_in"] == 1
    assert sb["small"]["calibrated_out"] == 0
    # medium gets a calibrated_out entry even though no task ran AT medium
    assert sb["medium"]["created"] == 0
    assert sb["medium"]["calibrated_in"] == 0
    assert sb["medium"]["calibrated_out"] == 1


@pytest.mark.asyncio
async def test_escalation_outcomes_tracks_attempt_merge_fail(db_session: AsyncSession):
    """escalation_outcomes buckets tasks whose effective scope exceeds the
    original (calibrated_from), keyed ``{from}_to_{to}``, with attempted/
    merged/failed counters. Downsizing (e.g. medium → small) and same-scope
    calibration must NOT show up in escalation_outcomes."""
    direction_id = "d-escalate"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)

    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=4)
    ]
    await db_session.flush()

    # Escalation small → medium, merged
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={
            "pr_merged": True,
            "scope": "medium",
            "scope_calibrated_from": "small",
        },
        payloads=[{"type": "create"}],
        title="esc-sm-md-merged",
    )
    # Escalation small → medium, failed
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.FAILED,
        context={"scope": "medium", "scope_calibrated_from": "small"},
        payloads=[{"type": "create"}],
        title="esc-sm-md-failed",
    )
    # Escalation medium → large, attempted only (still running)
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.IN_PROGRESS,
        context={"scope": "large", "scope_calibrated_from": "medium"},
        payloads=[{"type": "create"}],
        title="esc-md-lg-running",
    )
    # Downsize medium → small — must NOT appear in escalation_outcomes
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={
            "pr_merged": True,
            "scope": "small",
            "scope_calibrated_from": "medium",
        },
        payloads=[{"type": "create"}],
        title="downsize-md-sm",
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]
    eo = entry["escalation_outcomes"]

    assert set(eo.keys()) == {"small_to_medium", "medium_to_large"}
    assert eo["small_to_medium"] == {"attempted": 2, "merged": 1, "failed": 1}
    assert eo["medium_to_large"] == {"attempted": 1, "merged": 0, "failed": 0}


@pytest.mark.asyncio
async def test_escalation_outcomes_empty_when_no_escalations(db_session: AsyncSession):
    """escalation_outcomes is an empty dict when no child is calibrated upward."""
    direction_id = "d-no-escalate"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)

    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=1)
    ]
    await db_session.flush()

    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True, "scope": "small"},
        payloads=[{"type": "create"}],
        title="plain-small",
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]
    assert entry["escalation_outcomes"] == {}


@pytest.mark.asyncio
async def test_scope_breakdown_avg_expansion_ratio(db_session: AsyncSession):
    """avg_expansion_ratio per scope is mean of len(_pr_changed_files)/len(related_files)
    for completed tasks that have both fields populated."""
    direction_id = "d-expand"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)

    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=5)
    ]
    await db_session.flush()

    # small #1: 2 expected, 4 changed → ratio 2.0
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={
            "pr_merged": True,
            "scope": "small",
            "related_files": ["a.py", "b.py"],
            "_pr_changed_files": ["a.py", "b.py", "c.py", "d.py"],
        },
        payloads=[{"type": "create"}],
        title="s1",
    )
    # small #2: 1 expected, 1 changed → ratio 1.0  → avg small = 1.5
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={
            "pr_merged": True,
            "scope": "small",
            "related_files": ["x.py"],
            "_pr_changed_files": ["x.py"],
        },
        payloads=[{"type": "create"}],
        title="s2",
    )
    # medium: 2 expected, 6 changed → ratio 3.0
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={
            "pr_merged": True,
            "scope": "medium",
            "related_files": ["m1.py", "m2.py"],
            "_pr_changed_files": ["m1.py", "m2.py", "m3.py", "m4.py", "m5.py", "m6.py"],
        },
        payloads=[{"type": "create"}],
        title="m1",
    )
    # small with empty related_files: should be excluded (division by zero guard)
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={
            "pr_merged": True,
            "scope": "small",
            "related_files": [],
            "_pr_changed_files": ["y.py"],
        },
        payloads=[{"type": "create"}],
        title="s-no-related",
    )
    # failed task: contributes to avg_expansion_ratio_failed (separate from merged bucket)
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.FAILED,
        context={
            "scope": "medium",
            "related_files": ["z.py"],
            "_pr_changed_files": ["z.py", "q.py", "r.py"],
        },
        payloads=[{"type": "create"}],
        title="failed-m",
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    sb = project.directions[0]["research_history"][0]["scope_breakdown"]
    assert sb["small"]["avg_expansion_ratio"] == 1.5
    assert sb["small"]["avg_expansion_ratio_merged"] == 1.5
    assert sb["medium"]["avg_expansion_ratio"] == 3.0
    assert sb["medium"]["avg_expansion_ratio_merged"] == 3.0
    # Failed medium task with 1 related_file and 3 changed files → ratio 3.0
    assert sb["medium"]["avg_expansion_ratio_failed"] == 3.0
    assert "avg_expansion_ratio_failed" not in sb["small"]


@pytest.mark.asyncio
async def test_expansion_ratio_excludes_test_files(db_session: AsyncSession):
    """Test files in _pr_changed_files must not inflate the expansion ratio.

    Tasks that declare production files in related_files and then ship both
    those production files plus paired test files should have an expansion
    ratio of 1.0 (production-only), not 2.0.
    """
    direction_id = "d-expand-tests"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)

    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=2)
    ]
    await db_session.flush()

    # 2 production related_files, PR touches the same 2 plus 2 test files →
    # production-only ratio 1.0 (not inflated to 2.0).
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={
            "pr_merged": True,
            "scope": "small",
            "related_files": ["backend/app/foo.py", "backend/app/bar.py"],
            "_pr_changed_files": [
                "backend/app/foo.py",
                "backend/app/bar.py",
                "backend/testing/test_foo.py",
                "backend/testing/test_bar.py",
            ],
        },
        payloads=[{"type": "create"}],
        title="s-with-tests",
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    sb = project.directions[0]["research_history"][0]["scope_breakdown"]
    assert sb["small"]["avg_expansion_ratio"] == 1.0
    assert sb["small"]["avg_expansion_ratio_merged"] == 1.0


@pytest.mark.asyncio
async def test_scope_breakdown_failed_scope_overflow_counter(db_session: AsyncSession):
    """SCOPE_OVERFLOW failures increment both failed_infra and failed_scope_overflow.

    _effective_scope_denominator subtracts (failed_infra - failed_scope_overflow)
    from created to exclude true-infra failures while keeping scope-overflow
    signal in the denominator.
    """
    direction_id = "d-scope-overflow"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)

    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=3)
    ]
    await db_session.flush()

    # medium task that hit scope overflow
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.FAILED,
        context={"scope": "medium", "failure_category": "SCOPE_OVERFLOW"},
        payloads=[{"type": "create"}],
        title="m-overflow",
        latest_status_message="scope overflow",
    )
    # medium task with a pure infra failure (no scope overflow)
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.FAILED,
        context={"scope": "medium", "failure_category": "RATE_LIMIT"},
        payloads=[{"type": "create"}],
        title="m-rate",
        latest_status_message="rate limited",
    )
    # small task with CODE_ERROR — should not touch either infra counter
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.FAILED,
        context={"scope": "small", "failure_category": "CODE_ERROR"},
        payloads=[{"type": "create"}],
        title="s-code",
        latest_status_message="test failed",
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    sb = project.directions[0]["research_history"][0]["scope_breakdown"]
    # SCOPE_OVERFLOW is in INFRA_CLASSES, so it bumps both counters
    assert sb["medium"]["failed"] == 2
    assert sb["medium"]["failed_infra"] == 2
    assert sb["medium"]["failed_scope_overflow"] == 1
    # CODE_ERROR does not touch either infra counter
    assert sb["small"]["failed"] == 1
    assert sb["small"]["failed_infra"] == 0
    assert sb["small"]["failed_scope_overflow"] == 0


@pytest.mark.asyncio
async def test_scope_breakdown_failed_analysis_overflow_counter(
    db_session: AsyncSession,
):
    """scope_gate_triggered='analysis_overflow' increments failed_analysis_overflow.

    Other scope_gate values (post_review_overflow) and non-analysis failures
    leave the counter untouched — a stronger mismatch signal than generic
    SCOPE_OVERFLOW, used by spawn_calibration to detect systemic scope
    misestimation.
    """
    direction_id = "d-analysis-overflow"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)

    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=3)
    ]
    await db_session.flush()

    # medium + analysis_overflow
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.FAILED,
        context={
            "scope": "medium",
            "failure_category": "SCOPE_OVERFLOW",
            "scope_gate_triggered": "analysis_overflow",
        },
        payloads=[{"type": "create"}],
        title="m-ao",
        latest_status_message="analysis overflow",
    )
    # medium + post_review_overflow — must NOT increment
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.FAILED,
        context={
            "scope": "medium",
            "failure_category": "SCOPE_OVERFLOW",
            "scope_gate_triggered": "post_review_overflow",
        },
        payloads=[{"type": "create"}],
        title="m-pro",
        latest_status_message="post review overflow",
    )
    # small + no scope_gate
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.FAILED,
        context={"scope": "small", "failure_category": "CODE_ERROR"},
        payloads=[{"type": "create"}],
        title="s-code",
        latest_status_message="test failed",
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    sb = project.directions[0]["research_history"][0]["scope_breakdown"]
    assert sb["medium"]["failed_analysis_overflow"] == 1
    assert sb["small"]["failed_analysis_overflow"] == 0


@pytest.mark.asyncio
async def test_scope_breakdown_failure_stages_per_scope(db_session: AsyncSession):
    """failure_stages counts per-stage failures within each scope bucket."""
    direction_id = "d-stages"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)

    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=4)
    ]
    await db_session.flush()

    # small — test failure
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.FAILED,
        context={"scope": "small", "failure_category": "CODE_ERROR"},
        payloads=[{"type": "create"}],
        title="s-test",
        latest_status_message="pytest assert failed",
    )
    # small — lint failure
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.FAILED,
        context={"scope": "small", "failure_category": "CODE_ERROR"},
        payloads=[{"type": "create"}],
        title="s-lint",
        latest_status_message="ruff E501",
    )
    # medium — merge failure via _previous_failure_stage
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.FAILED,
        context={
            "scope": "medium",
            "failure_category": "CODE_ERROR",
            "_previous_failure_stage": "merge",
        },
        payloads=[{"type": "create"}],
        title="m-merge",
        latest_status_message="something else",
    )
    # medium — unknown stage (AGENT_TIMEOUT, generic message)
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.FAILED,
        context={"scope": "medium", "failure_category": "AGENT_TIMEOUT"},
        payloads=[{"type": "create"}],
        title="m-unknown",
        latest_status_message="No heartbeat for 300s",
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    sb = project.directions[0]["research_history"][0]["scope_breakdown"]
    assert sb["small"]["failure_stages"] == {"test": 1, "lint": 1}
    assert sb["medium"]["failure_stages"] == {"merge": 1, "unknown": 1}


@pytest.mark.asyncio
async def test_scope_breakdown_merged_cross_module(db_session: AsyncSession):
    """merged_cross_module counts merged tasks whose PR touches 2+ top-level dirs."""
    direction_id = "d-xmod"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)
    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=3)
    ]
    await db_session.flush()

    # merged, cross-module (backend + admin-ui)
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={
            "scope": "medium",
            "pr_merged": True,
            "_pr_changed_files": ["backend/app/a.py", "admin-ui/src/b.ts"],
        },
        payloads=[{"type": "create"}],
        title="x1",
    )
    # merged, single-module
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={
            "scope": "medium",
            "pr_merged": True,
            "_pr_changed_files": ["backend/app/x.py", "backend/app/y.py"],
        },
        payloads=[{"type": "create"}],
        title="x2",
    )
    # merged, no changed files recorded
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={"scope": "small", "pr_merged": True},
        payloads=[{"type": "create"}],
        title="x3",
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )
    await db_session.refresh(project)
    sb = project.directions[0]["research_history"][0]["scope_breakdown"]
    assert sb["medium"]["merged"] == 2
    assert sb["medium"]["merged_cross_module"] == 1
    assert sb["small"]["merged"] == 1
    assert sb["small"]["merged_cross_module"] == 0


def test_aggregate_scope_breakdown_sums_failure_stages():
    """_aggregate_scope_breakdown sums failure_stages counts across history entries."""
    from app.spawn_calibration import _aggregate_scope_breakdown

    entries = [
        {
            "scope_breakdown": {
                "small": {
                    "created": 2,
                    "merged": 1,
                    "failed": 1,
                    "failed_infra": 0,
                    "failed_scope_overflow": 0,
                    "failure_stages": {"test": 1},
                },
            }
        },
        {
            "scope_breakdown": {
                "small": {
                    "created": 3,
                    "merged": 1,
                    "failed": 2,
                    "failed_infra": 0,
                    "failed_scope_overflow": 0,
                    "failure_stages": {"test": 1, "lint": 1},
                },
                "medium": {
                    "created": 1,
                    "merged": 0,
                    "failed": 1,
                    "failed_infra": 0,
                    "failed_scope_overflow": 0,
                    "failure_stages": {"merge": 1},
                },
            }
        },
    ]
    agg = _aggregate_scope_breakdown(entries)
    assert agg["small"]["failure_stages"] == {"test": 2, "lint": 1}
    assert agg["medium"]["failure_stages"] == {"merge": 1}


@pytest.mark.asyncio
async def test_scope_breakdown_no_expansion_ratio_when_no_data(
    db_session: AsyncSession,
):
    """avg_expansion_ratio is omitted when no qualifying tasks exist for a scope."""
    direction_id = "d-no-expand"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)

    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=1)
    ]
    await db_session.flush()

    # Task missing _pr_changed_files entirely
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True, "scope": "small", "related_files": ["a.py"]},
        payloads=[{"type": "create"}],
        title="no-changed",
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    sb = project.directions[0]["research_history"][0]["scope_breakdown"]
    assert "avg_expansion_ratio" not in sb["small"]


# ─────────────────────────────────────────────────────────────────────────────
# 7. Co-change affinity index
# ─────────────────────────────────────────────────────────────────────────────


def _make_project_with_design_doc(design_doc: dict | None = None) -> Project:
    """Create an in-memory Project with the given design_doc (no DB flush needed)."""
    return Project(
        name="Co-change Test",
        user_id="test-user",
        design_doc=design_doc or {},
    )


def test_cochange_index_basic_pairs():
    """Co-changed files are recorded bidirectionally with score 1."""
    project = _make_project_with_design_doc()
    _update_cochange_index(project, [["a.py", "b.py", "c.py"]])
    idx = project.design_doc["cochange_index"]
    assert idx["a.py"]["b.py"] == 1.0
    assert idx["a.py"]["c.py"] == 1.0
    assert idx["b.py"]["a.py"] == 1.0
    assert idx["c.py"]["a.py"] == 1.0


def test_cochange_index_accumulates_scores():
    """Scores increment across separate calls (decay applied once per call)."""
    project = _make_project_with_design_doc()
    # First call: a↔b = 1.0
    _update_cochange_index(project, [["a.py", "b.py"]])
    # Second call: existing 1.0 decays to 0.9, then +1.0 = 1.9
    _update_cochange_index(project, [["a.py", "b.py"]])
    idx = project.design_doc["cochange_index"]
    assert idx["a.py"]["b.py"] == pytest.approx(1.9, abs=0.01)


def test_cochange_index_decay_fades_old_scores():
    """Repeated updates decay old scores toward zero."""
    project = _make_project_with_design_doc({"cochange_index": {"a.py": {"b.py": 1.0}}})
    # Apply 10 rounds of decay without adding new data (empty list → early return)
    for _ in range(10):
        # Use a single-file list (no pairs generated) to trigger decay only
        _update_cochange_index(project, [["a.py"]])
    idx = project.design_doc.get("cochange_index", {})
    # After enough decay cycles, score should either be pruned or very small
    score = (idx.get("a.py") or {}).get("b.py", 0.0)
    assert score < 0.35  # decayed below MIN_SCORE threshold (0.3)


def test_cochange_index_prunes_to_top_5():
    """Only the top-5 co-changed files are kept per file."""
    project = _make_project_with_design_doc()
    # Create 7 files that all co-change with a.py
    files = ["a.py"] + [f"file{i}.py" for i in range(7)]
    _update_cochange_index(project, [files])
    idx = project.design_doc["cochange_index"]
    assert len(idx["a.py"]) == 5


def test_cochange_index_skips_single_file_prs():
    """PRs with only one file produce no pairs."""
    project = _make_project_with_design_doc()
    _update_cochange_index(project, [["solo.py"]])
    idx = project.design_doc.get("cochange_index", {})
    assert idx == {}


def test_cochange_index_no_op_on_empty_input():
    """Empty merged_files_lists returns immediately with no design_doc change."""
    project = _make_project_with_design_doc({"cochange_index": {"a.py": {"b.py": 2.0}}})
    _update_cochange_index(project, [])
    # design_doc must be unchanged
    assert project.design_doc["cochange_index"]["a.py"]["b.py"] == 2.0


def test_cochange_index_caps_total_files():
    """Index is capped at MAX_FILES (200) tracked files."""
    # Build a large index with 210 files
    large_index: dict = {
        f"file{i}.py": {f"file{i + 1}.py": float(i)} for i in range(210)
    }
    project = _make_project_with_design_doc({"cochange_index": large_index})
    # Trigger an update with a small new PR
    _update_cochange_index(project, [["x.py", "y.py"]])
    idx = project.design_doc["cochange_index"]
    assert len(idx) <= 200


@pytest.mark.asyncio
async def test_cochange_index_updated_on_merged_pr(db_session: AsyncSession):
    """_update_direction_research_outcomes populates cochange_index from _pr_changed_files."""
    direction_id = "d-cochange"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)

    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=1)
    ]
    await db_session.flush()

    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={
            "pr_merged": True,
            "_pr_changed_files": [
                "app/models.py",
                "app/schemas.py",
                "tests/test_models.py",
            ],
        },
        title="merged-with-files",
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    idx = (project.design_doc or {}).get("cochange_index", {})
    assert "app/models.py" in idx
    assert "app/schemas.py" in idx["app/models.py"]
    assert "tests/test_models.py" in idx["app/models.py"]


@pytest.mark.asyncio
async def test_cochange_index_not_updated_when_no_changed_files(
    db_session: AsyncSession,
):
    """No cochange_index entry when merged PR has no _pr_changed_files."""
    direction_id = "d-nofiles"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)

    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=1)
    ]
    await db_session.flush()

    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},  # no _pr_changed_files
        title="merged-no-files",
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    # cochange_index should not exist or be empty
    idx = (project.design_doc or {}).get("cochange_index", {})
    assert idx == {}


@pytest.mark.asyncio
async def test_scope_breakdown_unknown_default(db_session: AsyncSession):
    """Tasks with scope in context are categorized correctly; tasks missing scope default to 'unknown'."""
    direction_id = "d-unknown-scope"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)

    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=2)
    ]
    await db_session.flush()

    # Task with scope in context — should be categorized as "small"
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={"scope": "small"},
        payloads=[{"type": "fix"}],
        title="with-scope",
    )
    # Task with no scope in context — should default to "unknown"
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={},
        payloads=[{"type": "fix"}],
        title="no-scope",
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]
    assert "small" in entry["scope_breakdown"]
    assert entry["scope_breakdown"]["small"]["created"] == 1
    assert "unknown" in entry["scope_breakdown"]
    assert entry["scope_breakdown"]["unknown"]["created"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# 8. Decomposition quality (plan parent metrics)
# ─────────────────────────────────────────────────────────────────────────────


def test_longest_chain_length_empty_and_linear():
    assert _longest_chain_length({}) == 0
    # A -> B -> C (depth 3)
    assert _longest_chain_length({"a": [], "b": ["a"], "c": ["b"]}) == 3


def test_longest_chain_length_breaks_cycles_and_drops_external():
    # Cycle a→b→a is treated as depth 0 for the cycling node; external IDs ignored.
    chain = _longest_chain_length({"a": ["b"], "b": ["a", "zzz-external"]})
    assert chain >= 1


@pytest.mark.asyncio
async def test_decomposition_quality_basic(db_session: AsyncSession):
    """For a terminal plan child with subtasks, metrics are captured."""
    direction_id = "d-decomp"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)
    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=3)
    ]
    await db_session.flush()

    plan = await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={},
        payloads=[{"type": "plan"}],
        title="plan-a",
    )
    # Subtask 1: merged, 2 files, cross-module (backend + tests)
    s1 = await _create_child_task(
        db_session,
        plan.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={
            "pr_merged": True,
            "_pr_changed_files": ["backend/app/x.py", "tests/test_x.py"],
        },
        title="s1",
    )
    # Subtask 2: merged, 1 file (single module)
    s2 = await _create_child_task(
        db_session,
        plan.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True, "_pr_changed_files": ["backend/app/y.py"]},
        title="s2",
    )
    # Subtask 3: failed with non-infra error; depends on s1 and s2
    s3 = await _create_child_task(
        db_session,
        plan.id,
        project.id,
        status=TaskStatus.FAILED,
        context={"failure_category": "CODE_ERROR"},
        title="s3",
        latest_status_message="unit test failed",
    )
    s3.depends_on = [str(s1.id), str(s2.id)]
    db_session.add(s3)
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )
    await db_session.refresh(project)

    dq = project.directions[0]["research_history"][0]["decomposition_quality"]
    assert len(dq) == 1
    q = dq[0]
    assert q["plan_task_id"] == str(plan.id)
    assert q["subtask_count"] == 3
    # merged=2, failed non-infra=1 → 2/3
    assert q["subtask_merge_rate"] == round(2 / 3, 3)
    # file counts: 2, 1 → avg 1.5, max 2
    assert q["avg_files_per_subtask"] == 1.5
    assert q["max_files_in_subtask"] == 2
    # dependency depth: s3 -> (s1|s2) = 2
    assert q["dependency_depth"] == 2
    # cross-module: only s1 touches >=2 top-level dirs
    assert q["cross_module_count"] == 1


@pytest.mark.asyncio
async def test_decomposition_quality_ignores_infra_failures_in_merge_rate(
    db_session: AsyncSession,
):
    """Infra-class failures are excluded from the merge rate denominator."""
    direction_id = "d-decomp-infra"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)
    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=2)
    ]
    await db_session.flush()

    plan = await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "plan"}],
        title="plan-b",
    )
    await _create_child_task(
        db_session,
        plan.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True, "_pr_changed_files": ["a.py"]},
        title="m",
    )
    await _create_child_task(
        db_session,
        plan.id,
        project.id,
        status=TaskStatus.FAILED,
        context={"failure_category": "RATE_LIMIT"},
        title="infra",
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )
    await db_session.refresh(project)
    dq = project.directions[0]["research_history"][0]["decomposition_quality"]
    # merged=1, failed_non_infra=0 → 1/1
    assert dq[0]["subtask_merge_rate"] == 1.0


@pytest.mark.asyncio
async def test_decomposition_quality_records_failed_plan_with_zero_subtasks(
    db_session: AsyncSession,
):
    """A terminal plan that produced no subtasks gets a sentinel entry with
    subtask_merge_rate=0.0 so _poor_decomp_history can count the failure."""
    direction_id = "d-decomp-zero"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)
    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=1)
    ]
    await db_session.flush()

    plan = await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.FAILED,
        context={"failure_category": "CODE_ERROR"},
        payloads=[{"type": "plan"}],
        title="plan-zero-subtasks",
        latest_status_message="plan handler crashed before spawning",
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )
    await db_session.refresh(project)
    dq = project.directions[0]["research_history"][0]["decomposition_quality"]
    assert len(dq) == 1
    entry = dq[0]
    assert entry["plan_task_id"] == str(plan.id)
    assert entry["subtask_count"] == 0
    assert entry["subtask_merge_rate"] == 0.0


@pytest.mark.asyncio
async def test_decomposition_quality_skips_non_terminal_plans(db_session: AsyncSession):
    """Plan tasks still EVALUATING/running are not included."""
    direction_id = "d-decomp-skip"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)
    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=1)
    ]
    await db_session.flush()

    plan = await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "plan"}],
        title="plan-open",
    )
    await _create_child_task(
        db_session,
        plan.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True, "_pr_changed_files": ["a.py"]},
        title="s",
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )
    await db_session.refresh(project)
    dq = project.directions[0]["research_history"][0]["decomposition_quality"]
    assert dq == []


# ─────────────────────────────────────────────────────────────────────────────
# 9. Pipeline scope merge rate auto-population
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def _reset_pipeline_scope_rates():
    """Snapshot/restore runtime_config['pipeline_scope_merge_rates'] per test."""
    original = dict(runtime_config.get("pipeline_scope_merge_rates") or {})
    runtime_config["pipeline_scope_merge_rates"] = {}
    yield
    runtime_config["pipeline_scope_merge_rates"] = original


def _direction_with_scope_breakdown(direction_id: str, scope_breakdown: dict) -> dict:
    """Build a direction with one research_history entry carrying scope_breakdown."""
    return {
        "id": direction_id,
        "text": "t",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "research_history": [
            {"task_id": str(uuid.uuid4()), "scope_breakdown": scope_breakdown}
        ],
    }


def test_refresh_pipeline_scope_merge_rates_aggregates_and_publishes(
    _reset_pipeline_scope_rates,
):
    """Rates are aggregated across all directions and written to runtime_config."""
    project = Project(
        name="p",
        user_id="u",
        directions=[
            _direction_with_scope_breakdown(
                "d1",
                {
                    "small": {
                        "created": 4,
                        "merged": 3,
                        "failed": 1,
                        "failed_infra": 0,
                        "failed_scope_overflow": 0,
                    },
                },
            ),
            _direction_with_scope_breakdown(
                "d2",
                {
                    "small": {
                        "created": 3,
                        "merged": 2,
                        "failed": 1,
                        "failed_infra": 0,
                        "failed_scope_overflow": 0,
                    },
                },
            ),
        ],
    )
    _refresh_pipeline_scope_merge_rates(project)
    # small effective = (3+2) merged + (1+1) failed - 0 infra = 7 >= 5
    # rate = 5 / 7 = 0.714
    assert runtime_config["pipeline_scope_merge_rates"] == {"small": round(5 / 7, 3)}


def test_refresh_pipeline_scope_merge_rates_omits_sparse_scopes(
    _reset_pipeline_scope_rates,
):
    """Scopes with < 5 effective data points are excluded from the published dict."""
    project = Project(
        name="p",
        user_id="u",
        directions=[
            _direction_with_scope_breakdown(
                "d1",
                {
                    "small": {
                        "created": 6,
                        "merged": 4,
                        "failed": 2,
                        "failed_infra": 0,
                        "failed_scope_overflow": 0,
                    },
                    "medium": {
                        "created": 3,
                        "merged": 1,
                        "failed": 2,
                        "failed_infra": 0,
                        "failed_scope_overflow": 0,
                    },
                },
            ),
        ],
    )
    _refresh_pipeline_scope_merge_rates(project)
    rates = runtime_config["pipeline_scope_merge_rates"]
    assert "small" in rates  # effective = 6, included
    assert "medium" not in rates  # effective = 3, excluded


def test_refresh_pipeline_scope_merge_rates_excludes_infra_from_denominator(
    _reset_pipeline_scope_rates,
):
    """Non-overflow infra failures are excluded from the effective denominator."""
    project = Project(
        name="p",
        user_id="u",
        directions=[
            _direction_with_scope_breakdown(
                "d1",
                {
                    "medium": {
                        "created": 10,
                        "merged": 3,
                        "failed": 7,
                        "failed_infra": 5,
                        "failed_scope_overflow": 1,
                    },
                },
            ),
        ],
    )
    _refresh_pipeline_scope_merge_rates(project)
    # effective = 3 merged + 7 failed - (5 infra - 1 overflow) = 3 + 7 - 4 = 6
    # rate = 3 / 6 = 0.5
    assert runtime_config["pipeline_scope_merge_rates"] == {"medium": 0.5}


def test_refresh_pipeline_scope_merge_rates_no_directions(_reset_pipeline_scope_rates):
    """Empty directions produces an empty rates dict."""
    project = Project(name="p", user_id="u", directions=[])
    _refresh_pipeline_scope_merge_rates(project)
    assert runtime_config["pipeline_scope_merge_rates"] == {}


def test_pipeline_scope_expansion_ratios_fallback(_reset_pipeline_scope_rates):
    """Pipeline-wide expansion ratio is published when samples >= 5; fallback chain
    uses the pipeline signal only when direction and project history lack data.
    """
    from app.research_completion import _avg_medium_expansion_ratio

    # Pipeline signal: 5 medium history entries with avg_expansion_ratio.
    entries = [
        {
            "task_id": str(uuid.uuid4()),
            "scope_breakdown": {
                "medium": {
                    "created": 1,
                    "merged": 1,
                    "failed": 0,
                    "failed_infra": 0,
                    "failed_scope_overflow": 0,
                    "avg_expansion_ratio": 3.0,
                    "avg_expansion_ratio_merged": 3.0,
                },
            },
        }
        for _ in range(5)
    ]
    seeded = {
        "id": "d-seed",
        "text": "t",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "research_history": entries,
    }
    # A brand-new direction with no research_history — only benefits from the
    # pipeline-wide fallback.
    fresh = {
        "id": "d-new",
        "text": "t",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "research_history": [],
    }
    project = Project(name="p", user_id="u", directions=[seeded, fresh])

    _refresh_pipeline_scope_merge_rates(project)
    assert runtime_config["pipeline_scope_expansion_ratios"] == {"medium": 3.0}

    # Direction/project helper returns None for the fresh direction alone —
    # confirms the fallback chain would reach the pipeline tier.
    assert _avg_medium_expansion_ratio([fresh]) is None

    # Sparse signal (< 5 samples) → no publication, fallback falls through to 2.0.
    project_sparse = Project(
        name="p",
        user_id="u",
        directions=[
            _direction_with_scope_breakdown(
                "d1",
                {
                    "medium": {
                        "created": 1,
                        "merged": 1,
                        "failed": 0,
                        "failed_infra": 0,
                        "failed_scope_overflow": 0,
                        "avg_expansion_ratio": 3.0,
                        "avg_expansion_ratio_merged": 3.0,
                    },
                },
            ),
        ],
    )
    _refresh_pipeline_scope_merge_rates(project_sparse)
    assert runtime_config["pipeline_scope_expansion_ratios"] == {}


@pytest.mark.asyncio
async def test_update_outcomes_publishes_pipeline_scope_rates(
    db_session: AsyncSession, _reset_pipeline_scope_rates
):
    """_update_direction_research_outcomes writes rates after updating a direction."""
    direction_id = "d-rates"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)

    # Seed another direction with history so the cross-direction aggregation reaches
    # the 5-effective threshold after this cycle's 2 small tasks land.
    seeded_direction = _direction_with_scope_breakdown(
        "d-seed",
        {
            "small": {
                "created": 3,
                "merged": 3,
                "failed": 0,
                "failed_infra": 0,
                "failed_scope_overflow": 0,
            },
        },
    )
    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=2),
        seeded_direction,
    ]
    await db_session.flush()

    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True, "scope": "small"},
        payloads=[{"type": "create"}],
        title="s1",
    )
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.FAILED,
        context={"scope": "small", "failure_category": "CODE_ERROR"},
        payloads=[{"type": "create"}],
        title="s2",
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    # Combined small: 1 merged + 3 seeded merged = 4; 1 failed; infra 0.
    # With seeded 3 merged + this cycle 1 merged + 1 failed = effective 5 → 4/5 = 0.8
    rates = runtime_config["pipeline_scope_merge_rates"]
    assert rates == {"small": 0.8}


@pytest.mark.asyncio
async def test_update_outcomes_writes_cost_by_provider_model(db_session: AsyncSession):
    """_update_direction_research_outcomes records per-(provider, model) cost on
    the research_history entry, keyed by f"{provider}|{model}". Tasks lacking
    provider/model fall under "unknown|unknown" so legacy rows still contribute."""
    direction_id = "d-pm-rollup"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)
    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=3)
    ]
    await db_session.flush()

    now = _utcnow()
    # Two child tasks across distinct (provider, model) pairs; one legacy without
    # provider/model. Each runs for a known VM duration so cost is deterministic.
    t_anth = Task(
        project_id=project.id,
        parent_task_id=research_task.id,
        title="anth",
        payloads=[{"type": "create", "scope": "small"}],
        context={},
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        provider="anthropic",
        model="claude-opus-4-7",
        vm_provisioned_at=now - timedelta(hours=1),
        vm_stopped_at=now,
        vm_spot=False,
    )
    t_oai = Task(
        project_id=project.id,
        parent_task_id=research_task.id,
        title="oai",
        payloads=[{"type": "create", "scope": "small"}],
        context={},
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        provider="openai",
        model="gpt-5.4",
        vm_provisioned_at=now - timedelta(minutes=30),
        vm_stopped_at=now,
        vm_spot=True,
    )
    t_legacy = Task(
        project_id=project.id,
        parent_task_id=research_task.id,
        title="legacy",
        payloads=[{"type": "create", "scope": "small"}],
        context={},
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        # provider/model omitted — legacy row.
        vm_provisioned_at=now - timedelta(minutes=10),
        vm_stopped_at=now,
        vm_spot=True,
    )
    db_session.add_all([t_anth, t_oai, t_legacy])
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )
    await db_session.refresh(project)

    entry = project.directions[0]["research_history"][0]
    by_pm = entry.get("cost_by_provider_model")
    assert isinstance(by_pm, dict) and by_pm, "cost_by_provider_model must be populated"
    assert "anthropic|claude-opus-4-7" in by_pm
    assert "openai|gpt-5.4" in by_pm
    assert "unknown|unknown" in by_pm
    for v in by_pm.values():
        assert v > 0
    # The aggregate matches the entry-level estimated_cost_usd within rounding.
    assert abs(sum(by_pm.values()) - entry["estimated_cost_usd"]) < 1e-3


# ─────────────────────────────────────────────────────────────────────────────
# 10. Cascade-cancellation exclusion from failure metrics
# ─────────────────────────────────────────────────────────────────────────────


def test_is_cascade_cancelled_detects_signals():
    """All four cascade signals are detected; genuine CANCELLED is not."""
    # Requeue replacement via _replaced_by_task_id in context
    t = Task(
        project_id=uuid.uuid4(),
        title="t",
        payloads=[{"type": "create"}],
        status=TaskStatus.CANCELLED,
        context={"_replaced_by_task_id": "x"},
        created_by="ORCHESTRATOR",
    )
    assert _is_cascade_cancelled(t)
    # Requeue message
    t.context = {}
    t.latest_status_message = "Parent requeued — replaced by retry"
    assert _is_cascade_cancelled(t)
    # Parent cascade
    t.latest_status_message = "Cancelled: parent evaluation timeout after 6h"
    assert _is_cascade_cancelled(t)
    # Dependency cascade
    t.latest_status_message = "Cancelled: dependency task failed"
    assert _is_cascade_cancelled(t)
    # Review-pipeline supersession ("Superseded by fix on PR #N: ...")
    t.latest_status_message = (
        "Superseded by fix on PR #5317: review was scoped to a pre-conflict snapshot"
    )
    assert _is_cascade_cancelled(t)
    # Stale-heartbeat supersession ("Superseded by new review for PR #N: ...")
    t.latest_status_message = (
        "Superseded by new review for PR #42: no heartbeat for >30min"
    )
    assert _is_cascade_cancelled(t)
    # Genuine cancellation (operator action with no cascade marker)
    t.latest_status_message = "Cancelled by admin"
    assert not _is_cascade_cancelled(t)
    # FAILED is never a cascade cancellation
    t.status = TaskStatus.FAILED
    t.latest_status_message = "Parent requeued — replaced by retry"
    assert not _is_cascade_cancelled(t)


def test_counts_as_failure_excludes_cascade_cancelled():
    t = Task(
        project_id=uuid.uuid4(),
        title="t",
        payloads=[{"type": "create"}],
        status=TaskStatus.CANCELLED,
        context={"_replaced_by_task_id": "x"},
        created_by="ORCHESTRATOR",
    )
    assert not _counts_as_failure(t)
    t.context = {}
    t.latest_status_message = "Cancelled by admin"
    assert _counts_as_failure(t)
    t.status = TaskStatus.FAILED
    assert _counts_as_failure(t)


def _cancelled_task(context):
    return Task(
        project_id=uuid.uuid4(),
        title="t",
        payloads=[{"type": "create"}],
        status=TaskStatus.CANCELLED,
        context=context,
        created_by="ORCHESTRATOR",
    )


def test_non_genuine_cancelled_excludes_new_markers():
    """CROSS-FILE CONSISTENCY: the FIX A / FIX D / FIX H non-genuine CANCELLED
    outcomes must NOT count toward direction failure metrics."""
    # FIX A: pr_closed_non_rejection
    assert _is_non_genuine_cancelled(
        _cancelled_task({"failure_detail": "pr_closed_non_rejection"})
    )
    assert not _counts_as_failure(
        _cancelled_task({"failure_detail": "pr_closed_non_rejection"})
    )
    # FIX D: cap_exhausted_after_approve
    assert _is_non_genuine_cancelled(
        _cancelled_task({"failure_detail": "cap_exhausted_after_approve"})
    )
    assert not _counts_as_failure(
        _cancelled_task({"failure_detail": "cap_exhausted_after_approve"})
    )
    # FIX H: REASSIGNED handoff (and the SCOPE_EMPTY no-op) lands CANCELLED.
    assert _is_non_genuine_cancelled(
        _cancelled_task({"failure_category": "REASSIGNED"})
    )
    assert not _counts_as_failure(
        _cancelled_task({"failure_category": "REASSIGNED"})
    )
    assert not _counts_as_failure(
        _cancelled_task({"failure_category": "SCOPE_EMPTY"})
    )


def test_genuine_cancelled_and_failed_still_count():
    """GUARDRAIL: a genuine CANCELLED (operator-cancelled with no non-genuine
    marker) and a genuine FAILED still count toward failure metrics."""
    genuine = _cancelled_task({})
    genuine.latest_status_message = "Cancelled by admin"
    assert not _is_non_genuine_cancelled(genuine)
    assert _counts_as_failure(genuine)
    # A real code failure that landed CANCELLED with a CODE_ERROR category is
    # NOT excluded (only REASSIGNED/SCOPE_EMPTY categories are non-genuine).
    code_err = _cancelled_task({"failure_category": "CODE_ERROR"})
    code_err.latest_status_message = "Cancelled by admin"
    assert not _is_non_genuine_cancelled(code_err)
    assert _counts_as_failure(code_err)
    # FAILED is always a genuine failure.
    failed = _cancelled_task({"failure_detail": "pr_closed_non_rejection"})
    failed.status = TaskStatus.FAILED
    assert _counts_as_failure(failed)


@pytest.mark.asyncio
async def test_cascade_cancelled_excluded_from_failure_buckets(
    db_session: AsyncSession,
):
    """A cascade-cancelled task stays in tasks_cancelled but is excluded from
    taxonomy_summary, scope_breakdown.failed, type_breakdown.failed,
    stage_dropout, and failed_task_details."""
    direction_id = "d-cascade"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)

    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=3)
    ]
    await db_session.flush()

    # Genuine FAILED — should count everywhere
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.FAILED,
        context={"scope": "small", "failure_category": "CODE_ERROR"},
        payloads=[{"type": "create"}],
        title="real-failure",
        latest_status_message="pytest failed",
    )
    # Cascade-cancelled via requeue clone — should NOT count as failure
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.CANCELLED,
        context={"scope": "small", "_replaced_by_task_id": str(uuid.uuid4())},
        payloads=[{"type": "create"}],
        title="requeued",
        latest_status_message="Parent requeued — replaced by retry",
    )
    # Cascade-cancelled via parent dependency failure — should NOT count
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.CANCELLED,
        context={"scope": "small"},
        payloads=[{"type": "create"}],
        title="dep-cascade",
        latest_status_message="Cancelled: parent dependency failed",
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]

    # tasks_cancelled still reflects BOTH cancelled tasks — raw status count is unchanged
    assert entry["tasks_cancelled"] == 2
    assert entry["tasks_failed"] == 1

    # Only the genuine CODE_ERROR should appear in taxonomy / failed_task_details
    assert len(entry["failed_task_details"]) == 1
    assert entry["failed_task_details"][0]["title"] == "real-failure"
    ts = entry["taxonomy_summary"]
    assert sum(ts.values()) == 1  # cascade entries not counted

    # scope_breakdown.small.failed should only include the real failure
    sb = entry["scope_breakdown"]
    assert sb["small"]["failed"] == 1
    assert sb["small"]["failed_code_error"] == 1

    # type_breakdown.create.failed excludes cascade cancellations
    assert entry["type_breakdown"]["create"]["failed"] == 1

    # stage_dropout.code_error only counts the real failure
    assert entry["stage_dropout"]["tasks_failed_code_error"] == 1


@pytest.mark.asyncio
async def test_genuine_cancelled_still_counts_as_failure(db_session: AsyncSession):
    """A CANCELLED task without cascade markers still counts toward failure metrics."""
    direction_id = "d-genuine-cancel"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)

    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=1)
    ]
    await db_session.flush()

    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.CANCELLED,
        context={"scope": "small", "failure_category": "AGENT_TIMEOUT"},
        payloads=[{"type": "create"}],
        title="genuine-cancel",
        latest_status_message="Task timed out on VM",
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]
    assert entry["scope_breakdown"]["small"]["failed"] == 1
    assert len(entry["failed_task_details"]) == 1


# ─────────────────────────────────────────────────────────────────────────────
# Preventive context tracking
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_preventive_context_counts(db_session: AsyncSession):
    """preventive_context_tasks_{created,merged,failed} counted from child context."""
    direction_id = "d-prev"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)

    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=5)
    ]
    await db_session.flush()

    # 3 preventive-context: 1 merged, 1 failed, 1 in-progress/other
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={"has_preventive_context": True, "pr_merged": True},
        title="prev-merged",
    )
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.FAILED,
        context={"has_preventive_context": True},
        title="prev-failed",
    )
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={"has_preventive_context": True},  # completed, not merged
        title="prev-completed-no-pr",
    )
    # Non-preventive: shouldn't count
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        title="no-prev-merged",
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]
    assert entry["preventive_context_tasks_created"] == 3
    assert entry["preventive_context_tasks_merged"] == 1
    assert entry["preventive_context_tasks_failed"] == 1


@pytest.mark.asyncio
async def test_remediation_task_counts(db_session: AsyncSession):
    """remediation_tasks_{created,merged,failed} counted via diagnosis_task_id/auto_remediation."""
    direction_id = "d-rem"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)

    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=5)
    ]
    await db_session.flush()

    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={"diagnosis_task_id": "diag-1", "pr_merged": True},
        title="rem-diag-merged",
    )
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.FAILED,
        context={"auto_remediation": True},
        title="rem-auto-failed",
    )
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={"auto_remediation": True},  # completed, not merged
        title="rem-auto-no-pr",
    )
    # Non-remediation: shouldn't count
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        title="regular-merged",
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]
    assert entry["remediation_tasks_created"] == 3
    assert entry["remediation_tasks_merged"] == 1
    assert entry["remediation_tasks_failed"] == 1


def test_synthesize_prompt_quality_patterns_emits_when_signal_meaningful():
    from app.direction_outcomes import synthesize_prompt_quality_patterns

    history = [
        {
            "prompt_telemetry": {
                "merged": {
                    "count": 3,
                    "avg_char_count": 1200,
                    "avg_related_file_count": 4.0,
                    "frac_mentions_tests": 0.8,
                    "frac_mentions_symbols": 0.7,
                },
                "failed": {
                    "count": 2,
                    "avg_char_count": 800,
                    "avg_related_file_count": 2.0,
                    "frac_mentions_tests": 0.3,
                    "frac_mentions_symbols": 0.2,
                },
            }
        },
        {
            "prompt_telemetry": {
                "merged": {
                    "count": 2,
                    "avg_char_count": 1300,
                    "avg_related_file_count": 5.0,
                    "frac_mentions_tests": 0.9,
                    "frac_mentions_symbols": 0.8,
                },
                "failed": {
                    "count": 2,
                    "avg_char_count": 700,
                    "avg_related_file_count": 1.5,
                    "frac_mentions_tests": 0.2,
                    "frac_mentions_symbols": 0.1,
                },
            }
        },
    ]
    notes = synthesize_prompt_quality_patterns(history)
    assert len(notes) >= 3
    joined = " ".join(notes)
    assert "chars" in joined
    assert "tests" in joined


def test_synthesize_prompt_quality_patterns_skips_when_too_few_cycles():
    from app.direction_outcomes import synthesize_prompt_quality_patterns

    history = [
        {
            "prompt_telemetry": {
                "merged": {
                    "count": 2,
                    "avg_char_count": 1200,
                    "frac_mentions_tests": 0.8,
                },
                "failed": {
                    "count": 2,
                    "avg_char_count": 600,
                    "frac_mentions_tests": 0.2,
                },
            }
        },
    ]
    assert synthesize_prompt_quality_patterns(history) == []


def test_synthesize_prompt_quality_patterns_skips_when_differences_are_noise():
    from app.direction_outcomes import synthesize_prompt_quality_patterns

    history = [
        {
            "prompt_telemetry": {
                "merged": {
                    "count": 2,
                    "avg_char_count": 1000,
                    "avg_related_file_count": 3.0,
                    "frac_mentions_tests": 0.5,
                    "frac_mentions_symbols": 0.5,
                },
                "failed": {
                    "count": 2,
                    "avg_char_count": 1010,
                    "avg_related_file_count": 3.1,
                    "frac_mentions_tests": 0.55,
                    "frac_mentions_symbols": 0.5,
                },
            }
        },
        {
            "prompt_telemetry": {
                "merged": {
                    "count": 2,
                    "avg_char_count": 990,
                    "avg_related_file_count": 2.9,
                    "frac_mentions_tests": 0.5,
                    "frac_mentions_symbols": 0.5,
                },
                "failed": {
                    "count": 2,
                    "avg_char_count": 1000,
                    "avg_related_file_count": 3.0,
                    "frac_mentions_tests": 0.55,
                    "frac_mentions_symbols": 0.5,
                },
            }
        },
    ]
    assert synthesize_prompt_quality_patterns(history) == []


def test_synthesize_prompt_quality_patterns_emits_scope_segmented_note():
    from app.direction_outcomes import synthesize_prompt_quality_patterns

    history = [
        {
            "prompt_telemetry": {
                "merged": {
                    "count": 3,
                    "avg_char_count": 600,
                    "avg_related_file_count": 2.0,
                    "frac_mentions_tests": 0.5,
                    "frac_mentions_symbols": 0.5,
                },
                "failed": {
                    "count": 2,
                    "avg_char_count": 600,
                    "avg_related_file_count": 2.0,
                    "frac_mentions_tests": 0.5,
                    "frac_mentions_symbols": 0.5,
                },
                "merged_small": {"count": 2, "avg_char_count": 300},
                "failed_small": {"count": 2, "avg_char_count": 900},
            }
        },
        {
            "prompt_telemetry": {
                "merged": {
                    "count": 3,
                    "avg_char_count": 600,
                    "avg_related_file_count": 2.0,
                    "frac_mentions_tests": 0.5,
                    "frac_mentions_symbols": 0.5,
                },
                "failed": {
                    "count": 2,
                    "avg_char_count": 600,
                    "avg_related_file_count": 2.0,
                    "frac_mentions_tests": 0.5,
                    "frac_mentions_symbols": 0.5,
                },
                "merged_small": {"count": 2, "avg_char_count": 320},
                "failed_small": {"count": 2, "avg_char_count": 880},
            }
        },
    ]
    notes = synthesize_prompt_quality_patterns(history)
    assert any("Small-scope" in n for n in notes)


def test_summarize_review_quality_feedback_includes_top_categories():
    from app.direction_outcomes import summarize_review_quality_feedback

    history = [{"reviews_dismissed": 1, "tasks_completed": 1}]
    dfa = {
        "missing_tests": 4,
        "style": 2,
        "naming": 1,
        "obscure": 1,
        "total": 8,
        "last_updated": "2026-04-20T00:00:00",
    }
    result = summarize_review_quality_feedback(history, dfa)
    assert result is not None
    assert result["top_categories"] == {"missing_tests": 4, "style": 2, "naming": 1}


def test_summarize_review_quality_feedback_skips_top_categories_when_sparse():
    from app.direction_outcomes import summarize_review_quality_feedback

    history = [{"reviews_dismissed": 1, "tasks_completed": 1}]
    dfa = {"missing_tests": 1, "style": 1, "total": 2}
    result = summarize_review_quality_feedback(history, dfa)
    assert result is not None
    assert "top_categories" not in result


def test_summarize_review_quality_feedback_no_dfa_unchanged():
    from app.direction_outcomes import summarize_review_quality_feedback

    history = [{"reviews_dismissed": 2, "tasks_completed": 1}]
    result = summarize_review_quality_feedback(history)
    assert result is not None
    assert "top_categories" not in result


# ─────────────────────────────────────────────────────────────────────────────
# Autonomous / operator-assisted classification
# ─────────────────────────────────────────────────────────────────────────────


class TestIsAutonomousMerge:
    """Unit tests for is_autonomous_merge — pure function, no DB required."""

    def _make_task(self, created_by="ORCHESTRATOR", context=None) -> Task:
        t = Task(
            project_id=uuid.uuid4(),
            title="test",
            payloads=[{"type": "create"}],
            context=context or {},
            status=TaskStatus.COMPLETED,
            created_by=created_by,
        )
        return t

    def test_orchestrator_created_no_override_is_autonomous(self):
        from app.direction_outcomes import is_autonomous_merge

        task = self._make_task(created_by="ORCHESTRATOR")
        assert is_autonomous_merge(task) is True

    def test_user_created_is_operator_assisted(self):
        from app.direction_outcomes import is_autonomous_merge

        task = self._make_task(created_by="USER")
        assert is_autonomous_merge(task) is False

    def test_review_dismissed_is_operator_assisted(self):
        from app.direction_outcomes import is_autonomous_merge

        task = self._make_task(context={"review_dismissed": True})
        assert is_autonomous_merge(task) is False

    def test_user_overrode_review_is_operator_assisted(self):
        from app.direction_outcomes import is_autonomous_merge

        task = self._make_task(context={"user_overrode_review": True})
        assert is_autonomous_merge(task) is False

    def test_orchestrator_with_pr_merged_and_no_override_is_autonomous(self):
        from app.direction_outcomes import is_autonomous_merge

        task = self._make_task(context={"pr_merged": True})
        assert is_autonomous_merge(task) is True

    def test_user_created_even_with_pr_merged_is_operator_assisted(self):
        from app.direction_outcomes import is_autonomous_merge

        task = self._make_task(created_by="USER", context={"pr_merged": True})
        assert is_autonomous_merge(task) is False


# ─────────────────────────────────────────────────────────────────────────────
# Aggregation: autonomous_prs_merged + assisted_prs_merged sum invariant
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_autonomous_assisted_sum_equals_prs_merged(db_session: AsyncSession):
    """autonomous_prs_merged + assisted_prs_merged == prs_merged for any mix."""
    direction_id = "d-attribution"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)

    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=4)
    ]
    await db_session.flush()

    # 2 autonomous merges, 1 operator-assisted merge, 1 rejected
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        context={"pr_merged": True},
        created_by="ORCHESTRATOR",
    )
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        context={"pr_merged": True},
        created_by="ORCHESTRATOR",
    )
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        context={"pr_merged": True},
        created_by="USER",
    )
    await _create_child_task(
        db_session, research_task.id, project.id, context={"pr_rejected": True}
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]
    assert entry["prs_merged"] == 3
    assert entry["autonomous_prs_merged"] == 2
    assert entry["assisted_prs_merged"] == 1
    assert (
        entry["autonomous_prs_merged"] + entry["assisted_prs_merged"]
        == entry["prs_merged"]
    )


@pytest.mark.asyncio
async def test_all_autonomous_direction_counts(db_session: AsyncSession):
    """All merges autonomous → assisted==0 and autonomous==prs_merged."""
    direction_id = "d-all-auto"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)

    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=2)
    ]
    await db_session.flush()

    for _ in range(2):
        await _create_child_task(
            db_session,
            research_task.id,
            project.id,
            context={"pr_merged": True},
            created_by="ORCHESTRATOR",
        )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]
    assert entry["autonomous_prs_merged"] == 2
    assert entry["assisted_prs_merged"] == 0
    assert entry["operator_action_count"] == 0


@pytest.mark.asyncio
async def test_zero_autonomous_all_assisted(db_session: AsyncSession):
    """All merges operator-assisted → autonomous==0."""
    direction_id = "d-all-assisted"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)

    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=2)
    ]
    await db_session.flush()

    for _ in range(2):
        await _create_child_task(
            db_session,
            research_task.id,
            project.id,
            context={"pr_merged": True, "review_dismissed": True},
        )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]
    assert entry["autonomous_prs_merged"] == 0
    assert entry["assisted_prs_merged"] == 2
    assert entry["prs_merged"] == 2


# ─────────────────────────────────────────────────────────────────────────────
# _fold_evicted_into_aggregate: attribution rollup
# ─────────────────────────────────────────────────────────────────────────────


def test_fold_evicted_attribution_fields():
    """total_autonomous/assisted/operator aggregate correctly; legacy entries fall back to prs_merged."""
    from app.direction_metrics import _fold_evicted_into_aggregate

    # One pre-attribution entry (no autonomous_prs_merged → fallback to prs_merged)
    # One post-attribution entry with split counts
    evicted = [
        {"timestamp": "2025-01-01T00:00:00", "tasks_created": 3, "prs_merged": 3},
        {
            "timestamp": "2025-02-01T00:00:00",
            "tasks_created": 5,
            "prs_merged": 3,
            "autonomous_prs_merged": 2,
            "assisted_prs_merged": 1,
            "operator_action_count": 2,
        },
    ]
    agg = _fold_evicted_into_aggregate(None, evicted)

    # Legacy entry contributes prs_merged=3 to autonomous (conservative fallback)
    # Post-attribution entry contributes autonomous=2
    assert agg["total_autonomous_prs_merged"] == 5  # 3 + 2
    assert agg["total_assisted_prs_merged"] == 1
    assert agg["total_operator_actions"] == 2


def test_fold_evicted_attribution_accumulates_on_prior_agg():
    """New evictions accumulate on top of an existing aggregate with attribution fields."""
    from app.direction_metrics import _fold_evicted_into_aggregate

    prior = {
        "total_cycles": 2,
        "total_autonomous_prs_merged": 4,
        "total_assisted_prs_merged": 1,
        "total_operator_actions": 1,
    }
    evicted = [
        {
            "timestamp": "2025-03-01T00:00:00",
            "tasks_created": 2,
            "prs_merged": 2,
            "autonomous_prs_merged": 1,
            "assisted_prs_merged": 1,
            "operator_action_count": 1,
        }
    ]
    agg = _fold_evicted_into_aggregate(prior, evicted)

    assert agg["total_autonomous_prs_merged"] == 5  # 4 + 1
    assert agg["total_assisted_prs_merged"] == 2  # 1 + 1
    assert agg["total_operator_actions"] == 2  # 1 + 1


# ─────────────────────────────────────────────────────────────────────────────
# compute_direction_autonomy_state
# ─────────────────────────────────────────────────────────────────────────────


def test_autonomy_state_below_threshold():
    from app.direction_outcomes import compute_direction_autonomy_state

    d = {
        "research_history": [
            {"autonomous_prs_merged": 3, "operator_action_count": 0},
            {"autonomous_prs_merged": 5, "operator_action_count": 0},
        ],
    }
    result = compute_direction_autonomy_state(d)
    assert result["lifetime_autonomous_prs_merged"] == 8
    assert result["lifetime_operator_actions"] == 0
    assert result["retirement_eligible"] is False


def test_autonomy_state_at_threshold():
    from app.direction_outcomes import compute_direction_autonomy_state

    d = {
        "research_history": [{"autonomous_prs_merged": 10, "operator_action_count": 0}]
    }
    result = compute_direction_autonomy_state(d)
    assert result["lifetime_autonomous_prs_merged"] == 10
    assert result["lifetime_operator_actions"] == 0
    assert result["retirement_eligible"] is True


def test_autonomy_state_above_threshold_from_aggregate():
    from app.direction_outcomes import compute_direction_autonomy_state

    d = {
        "research_history": [],
        "history_aggregate": {
            "total_autonomous_prs_merged": 12,
            "total_operator_actions": 0,
        },
    }
    result = compute_direction_autonomy_state(d)
    assert result["lifetime_autonomous_prs_merged"] == 12
    assert result["retirement_eligible"] is True


def test_autonomy_state_operator_action_blocks_eligibility():
    """12 autonomous merges but 1 operator action — must NOT be retirement-eligible."""
    from app.direction_outcomes import compute_direction_autonomy_state

    d = {
        "research_history": [{"autonomous_prs_merged": 12, "operator_action_count": 1}]
    }
    result = compute_direction_autonomy_state(d)
    assert result["lifetime_autonomous_prs_merged"] == 12
    assert result["lifetime_operator_actions"] == 1
    assert result["retirement_eligible"] is False


def test_autonomy_state_legacy_fallback_treats_prs_merged_as_autonomous():
    """Pre-attribution entries without autonomous_prs_merged fall back to prs_merged."""
    from app.direction_outcomes import compute_direction_autonomy_state

    d = {"research_history": [{"prs_merged": 5}]}  # no autonomous_prs_merged key
    result = compute_direction_autonomy_state(d)
    assert result["lifetime_autonomous_prs_merged"] == 5


def test_autonomy_state_combines_history_and_aggregate():
    from app.direction_outcomes import compute_direction_autonomy_state

    d = {
        "research_history": [{"autonomous_prs_merged": 3, "operator_action_count": 0}],
        "history_aggregate": {
            "total_autonomous_prs_merged": 7,
            "total_operator_actions": 0,
        },
    }
    result = compute_direction_autonomy_state(d)
    assert result["lifetime_autonomous_prs_merged"] == 10
    assert result["retirement_eligible"] is True


# ─────────────────────────────────────────────────────────────────────────────
# refresh_direction_outcomes_after_merge — heal stale prs_merged after a late merge
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_refresh_after_merge_heals_stale_prs_merged(db_session: AsyncSession):
    """Reproduces the churn-guard false positive and its fix.

    prs_merged is snapshotted when each child reaches COMPLETED — at which point
    its PR has only just opened, so the count is 0. The PR lands asynchronously
    later; refreshing after the landed signal is stamped recomputes the real
    count instead of leaving a stale 0 that trips the churn guardrail.
    """
    direction_id = "d-merge-refresh"
    project = await _create_project(db_session)
    research_task = await _make_research_task(db_session, project.id)
    rtid = str(research_task.id)
    project.directions = [_make_direction(direction_id, rtid, tasks_created=2)]
    db_session.add(project)
    await db_session.flush()

    child_ctx = {"direction_id": direction_id, "research_task_id": rtid}
    children = [
        await _create_child_task(
            db_session,
            research_task.id,
            project.id,
            status=TaskStatus.COMPLETED,
            context=dict(child_ctx),
            title=f"create {i}",
        )
        for i in range(2)
    ]
    await db_session.flush()

    # Phase 1 — children COMPLETED but PRs only just opened (no landed signal).
    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=rtid,
        session=db_session,
    )
    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]
    assert entry["tasks_completed"] == 2
    assert entry["prs_merged"] == 0  # the stale snapshot that trips churn

    # Phase 2 — both PRs land on the integration branch (landed signal stamped),
    # then a single child's merge fires the refresh, healing the whole cycle.
    for c in children:
        c.context = {**c.context, "pr_merged": True, "landed_on_integration": True}
        db_session.add(c)
    await db_session.flush()

    await refresh_direction_outcomes_after_merge(children[0], db_session)
    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]
    assert entry["prs_merged"] == 2  # both siblings counted, not just the trigger


@pytest.mark.asyncio
async def test_refresh_after_merge_noop_without_direction_context(
    db_session: AsyncSession,
):
    """A merged task that isn't a research-cycle child is a no-op (no crash)."""
    project = await _create_project(db_session)
    research_task = await _make_research_task(db_session, project.id)
    task = await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True, "landed_on_integration": True},  # no direction_id
    )
    await db_session.flush()
    # Must not raise and must not touch any direction state.
    await refresh_direction_outcomes_after_merge(task, db_session)


@pytest.mark.asyncio
async def test_refresh_after_out_of_band_merge_path_heals_prs_merged(
    db_session: AsyncSession,
):
    """A child whose PR lands via a newly-wired out-of-band path heals prs_merged.

    Models the gap fixed in Task 1: a child reported terminal (here FAILED, as in
    the agent_status / reaper out-of-band recovery) while its PR merged later. The
    recovery path stamps the landed signal and COMPLETED, then calls
    ``refresh_direction_outcomes_after_merge``. Before the refresh the cycle's
    prs_merged is the stale 0 that trips the zero-merge/churn guard; after it the
    landed child is counted (alongside a sibling that landed in the same window),
    proving the whole-cycle recompute the wired paths rely on.
    """
    direction_id = "d-oob-merge"
    project = await _create_project(db_session)
    research_task = await _make_research_task(db_session, project.id)
    rtid = str(research_task.id)
    project.directions = [_make_direction(direction_id, rtid, tasks_created=2)]
    db_session.add(project)
    await db_session.flush()

    child_ctx = {"direction_id": direction_id, "research_task_id": rtid}
    # The trigger child reported FAILED with an open PR (agent died after merge);
    # the sibling already COMPLETED. Neither yet carries the landed signal.
    trigger = await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.FAILED,
        context=dict(child_ctx),
        title="out-of-band create",
    )
    sibling = await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context=dict(child_ctx),
        title="sibling create",
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=rtid,
        session=db_session,
    )
    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]
    assert entry["prs_merged"] == 0  # stale: PRs not yet recorded as landed

    # The out-of-band recovery path lands BOTH PRs on the integration branch and
    # flips the trigger COMPLETED, then fires the refresh on the trigger child.
    trigger.status = TaskStatus.COMPLETED
    trigger.context = {
        **trigger.context,
        "pr_merged": True,
        "landed_on_integration": True,
    }
    sibling.context = {
        **sibling.context,
        "pr_merged": True,
        "landed_on_integration": True,
    }
    db_session.add(trigger)
    db_session.add(sibling)
    await db_session.flush()

    await refresh_direction_outcomes_after_merge(trigger, db_session)
    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]
    # Both the trigger AND the sibling are counted — the recompute re-counts every
    # child of the cycle, not just the one that fired the refresh.
    assert entry["prs_merged"] == 2


@pytest.mark.asyncio
async def test_tasks_completed_no_pr_counts_only_genuine_no_pr(
    db_session: AsyncSession,
):
    """tasks_completed_no_pr counts a COMPLETED-no-PR child, excluding others.

    "No changes needed — work already exists" completions are a healthy outcome,
    not churn. The count must include a COMPLETED child that opened no PR and
    exclude: a merged completion, a pr_rejected completion, a COMPLETED child that
    DID open a PR (PULL_REQUEST artifact / pr_number signal), and any in-flight /
    failed child.
    """
    direction_id = "d-no-pr"
    project = await _create_project(db_session)
    research_task = await _make_research_task(db_session, project.id)
    rtid = str(research_task.id)
    project.directions = [_make_direction(direction_id, rtid, tasks_created=6)]
    db_session.add(project)
    await db_session.flush()

    # (1) COMPLETED, never opened a PR — the genuine no-PR completion to count.
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={},
        title="no-changes-needed",
    )
    # (2) Merged completion — excluded (it landed a PR).
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True, "landed_on_integration": True},
        title="merged",
    )
    # (3) pr_rejected completion — excluded.
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={"pr_rejected": True},
        title="rejected",
    )
    # (4) COMPLETED but DID open a PR (artifact present, no landed/rejected
    #     signal) — excluded: it produced a PR, so it is not a no-PR completion.
    opened_pr = await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={"pr_number": 99},
        title="opened-pr-not-yet-landed",
    )
    db_session.add(
        Artifact(
            task_id=opened_pr.id,
            artifact_type=ArtifactType.PULL_REQUEST,
            pr_url="https://github.com/owner/repo/pull/99",
        )
    )
    # (5) In-flight (IN_PROGRESS) — excluded (not COMPLETED).
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.IN_PROGRESS,
        context={},
        title="in-flight",
    )
    # (6) FAILED — excluded (not COMPLETED).
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.FAILED,
        context={},
        title="failed",
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=rtid,
        session=db_session,
    )
    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]
    # Only child (1) qualifies.
    assert entry["tasks_completed_no_pr"] == 1
    # Sanity: the other completed children are still reflected in their buckets.
    assert entry["prs_merged"] == 1
    assert entry["prs_rejected"] == 1
    assert entry["tasks_completed"] == 4


# ─────────────────────────────────────────────────────────────────────────────
# Generalized (mixed-portfolio) outcome aggregates — ADDITIVE to PR metrics.
# A direction whose cycle merged 0 PRs but produced successful research +
# audit + memo outcomes must show positive generalized health, while code-only
# portfolios keep identical merge metrics.
# ─────────────────────────────────────────────────────────────────────────────


async def _set_plan_output(session: AsyncSession, task: Task, plan_output) -> None:
    """Attach a plan_output payload to an already-flushed child task."""
    task.plan_output = plan_output
    session.add(task)
    await session.flush()


@pytest.mark.asyncio
async def test_generalized_health_non_code_portfolio(db_session: AsyncSession):
    """A 0-PR cycle of research/audit/memo successes shows positive health."""
    direction_id = "d-noncode"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    rtid = str(research_task.id)
    project.directions = [_make_direction(direction_id, rtid, tasks_created=3)]
    await db_session.flush()

    # Successful research: a titled, graded finding.
    research_child = await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "research"}],
        title="research",
    )
    await _set_plan_output(
        db_session,
        research_child,
        [{"title": "Investigate X", "actionability_grade": "HIGH"}],
    )
    # Successful audit: structured findings present.
    audit_child = await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "security_audit"}],
        title="audit",
    )
    await _set_plan_output(
        db_session, audit_child, {"findings": [{"severity": "high"}]}
    )
    # Successful memo: a reasoning artifact mirrored onto plan_output.
    memo_child = await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "memo"}],
        title="memo",
    )
    await _set_plan_output(db_session, memo_child, {"memo": "Reasoning text"})
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=rtid,
        session=db_session,
    )
    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]

    # Existing merge metrics are intact and honestly zero (no PR-merging task).
    assert entry["prs_merged"] == 0
    assert entry["conversion_rate"] == 0.0
    # Generalized signal: all three read-only successes counted.
    assert entry["generalized_successes"] == 3
    assert entry["generalized_health_rate"] == 1.0
    breakdown = entry["outcome_kind_breakdown"]
    assert breakdown["research_quality"]["success"] == 1
    assert breakdown["audit_findings"]["success"] == 1
    assert breakdown["memo_artifact"]["success"] == 1


@pytest.mark.asyncio
async def test_generalized_health_code_only_unchanged(db_session: AsyncSession):
    """Code-only portfolio: merge metrics AND generalized successes both equal
    the merged count (a code merge is a generalized success of kind code_merge)."""
    direction_id = "d-codeonly"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    rtid = str(research_task.id)
    project.directions = [_make_direction(direction_id, rtid, tasks_created=2)]
    await db_session.flush()

    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        payloads=[{"type": "create", "scope": "small"}],
        title="merged",
    )
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.FAILED,
        payloads=[{"type": "create", "scope": "small"}],
        title="failed",
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=rtid,
        session=db_session,
    )
    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]

    assert entry["prs_merged"] == 1
    assert entry["conversion_rate"] == 0.5
    # One code merge → one generalized success of kind code_merge.
    assert entry["generalized_successes"] == 1
    assert entry["outcome_kind_breakdown"]["code_merge"]["success"] == 1
    assert entry["outcome_kind_breakdown"]["code_merge"]["failure"] == 1
    # 1 success / 2 terminal children.
    assert entry["generalized_health_rate"] == 0.5


@pytest.mark.asyncio
async def test_product_vs_structural_classification_registry_driven(
    db_session: AsyncSession,
):
    """product_ratio / structural_count classify child tasks via the task-type
    registry (PRODUCT_TASK_TYPES), not the stale {create, feature, improvement}
    set. create + fix + migrate count as product; refactor + research count as
    structural; an unknown/future type defaults to structural."""
    direction_id = "d-product-split"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    rtid = str(research_task.id)
    project.directions = [_make_direction(direction_id, rtid, tasks_created=6)]
    await db_session.flush()

    # Product types (3): create, fix, migrate.
    for ttype in ("create", "fix", "migrate"):
        await _create_child_task(
            db_session,
            research_task.id,
            project.id,
            payloads=[{"type": ttype, "scope": "small"}],
            title=f"product-{ttype}",
        )
    # Structural types (3): refactor (write but overhead), research (read-only),
    # and an unknown/future type that defaults to structural.
    for ttype in ("refactor", "research", "future_unknown_type"):
        await _create_child_task(
            db_session,
            research_task.id,
            project.id,
            payloads=[{"type": ttype, "scope": "small"}],
            title=f"structural-{ttype}",
        )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=rtid,
        session=db_session,
    )
    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]
    # 3 product / 6 total = 0.5, 3 structural.
    assert entry["product_ratio"] == 0.5
    assert entry["structural_count"] == 3
