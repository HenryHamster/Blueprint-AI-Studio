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

from app.direction_outcomes import _update_direction_research_outcomes
from app.models import Project, Task, TaskStatus


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _create_project(session: AsyncSession, directions: list | None = None) -> Project:
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


def _make_direction(direction_id: str, research_task_id: str, tasks_created: int = 3) -> dict:
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
) -> Task:
    task = Task(
        project_id=project_id,
        parent_task_id=parent_id,
        title=title,
        payloads=payloads or [{"type": "create", "scope": "small"}],
        context=context or {},
        status=status,
        created_by="ORCHESTRATOR",
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
    project = await _create_project(db_session, directions=[
        _make_direction(direction_id, research_task_id)
    ])

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
    project = await _create_project(db_session, directions=[
        _make_direction(direction_id, history_task_id)
    ])

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
    project = await _create_project(db_session, directions=[{
        "id": direction_id,
        "text": "Test",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "research_history": [{"task_id": bad_task_id, "tasks_created": 1}],
    }])

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
    research_task = await _make_research_task.__wrapped__ if hasattr(_make_research_task, "__wrapped__") else None
    direction_id = "d-nochildren"
    project = await _create_project(db_session, directions=[
        _make_direction(direction_id, str(uuid.uuid4()))
    ])
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

    project.directions = [_make_direction(direction_id, research_task_id, tasks_created=5)]
    await db_session.flush()

    await _create_child_task(db_session, research_task.id, project.id, status=TaskStatus.COMPLETED, title="c1")
    await _create_child_task(db_session, research_task.id, project.id, status=TaskStatus.COMPLETED, title="c2")
    await _create_child_task(db_session, research_task.id, project.id, status=TaskStatus.FAILED, title="c3")
    await _create_child_task(db_session, research_task.id, project.id, status=TaskStatus.CANCELLED, title="c4")
    await _create_child_task(db_session, research_task.id, project.id, status=TaskStatus.IN_PROGRESS, title="c5")
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

    project.directions = [_make_direction(direction_id, research_task_id, tasks_created=4)]
    await db_session.flush()

    await _create_child_task(db_session, research_task.id, project.id,
                              status=TaskStatus.COMPLETED,
                              context={"pr_merged": True}, title="merged1")
    await _create_child_task(db_session, research_task.id, project.id,
                              status=TaskStatus.COMPLETED,
                              context={"pr_merged": True}, title="merged2")
    await _create_child_task(db_session, research_task.id, project.id,
                              status=TaskStatus.FAILED,
                              context={"pr_rejected": True}, title="rejected1")
    await _create_child_task(db_session, research_task.id, project.id,
                              status=TaskStatus.COMPLETED,
                              context={}, title="no-pr")
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

    await _create_child_task(db_session, research_task.id, project.id,
                              status=TaskStatus.COMPLETED,
                              context={"pr_merged": True}, title="merged")
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

    project.directions = [_make_direction(direction_id, research_task_id, tasks_created=2)]
    await db_session.flush()

    base = _utcnow()
    # Task 1: 2 hours
    await _create_child_task(db_session, research_task.id, project.id,
                              status=TaskStatus.COMPLETED,
                              context={"pr_merged": True},
                              created_at=base,
                              vm_stopped_at=base + timedelta(hours=2),
                              title="t1")
    # Task 2: 4 hours
    await _create_child_task(db_session, research_task.id, project.id,
                              status=TaskStatus.COMPLETED,
                              context={"pr_merged": True},
                              created_at=base,
                              vm_stopped_at=base + timedelta(hours=4),
                              title="t2")
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

    project.directions = [_make_direction(direction_id, research_task_id, tasks_created=2)]
    await db_session.flush()

    base = _utcnow()
    # One valid merged task: 3 hours
    await _create_child_task(db_session, research_task.id, project.id,
                              status=TaskStatus.COMPLETED,
                              context={"pr_merged": True},
                              created_at=base,
                              vm_stopped_at=base + timedelta(hours=3),
                              title="valid")
    # One merged task with no vm_stopped_at
    await _create_child_task(db_session, research_task.id, project.id,
                              status=TaskStatus.COMPLETED,
                              context={"pr_merged": True},
                              title="no-stop-time")
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

    project.directions = [_make_direction(direction_id, research_task_id, tasks_created=1)]
    await db_session.flush()

    await _create_child_task(db_session, research_task.id, project.id,
                              status=TaskStatus.FAILED,
                              context={}, title="failed")
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

    project.directions = [_make_direction(direction_id, research_task_id, tasks_created=4)]
    await db_session.flush()

    await _create_child_task(db_session, research_task.id, project.id,
                              status=TaskStatus.FAILED,
                              context={"failure_category": "PROVISION_FAILURE"},
                              title="infra1", latest_status_message="VM failed")
    await _create_child_task(db_session, research_task.id, project.id,
                              status=TaskStatus.FAILED,
                              context={"failure_category": "RATE_LIMIT"},
                              title="infra2", latest_status_message="Rate limited")
    await _create_child_task(db_session, research_task.id, project.id,
                              status=TaskStatus.FAILED,
                              context={"failure_category": "CODE_ERROR"},
                              title="noinfra1", latest_status_message="Code error")
    await _create_child_task(db_session, research_task.id, project.id,
                              status=TaskStatus.CANCELLED,
                              context={"failure_category": "AGENT_TIMEOUT"},
                              title="infra3", latest_status_message="Timed out")
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

    project.directions = [_make_direction(direction_id, research_task_id, tasks_created=8)]
    await db_session.flush()

    for i in range(8):
        await _create_child_task(db_session, research_task.id, project.id,
                                  status=TaskStatus.FAILED,
                                  context={},
                                  title=f"failed-{i}", latest_status_message=f"Error {i}")
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

    project.directions = [_make_direction(direction_id, research_task_id, tasks_created=1)]
    await db_session.flush()

    long_message = "X" * 200
    await _create_child_task(db_session, research_task.id, project.id,
                              status=TaskStatus.FAILED,
                              context={},
                              title="long-fail", latest_status_message=long_message)
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

    project.directions = [_make_direction(direction_id, research_task_id, tasks_created=4)]
    await db_session.flush()

    # small: 2 created, 1 merged, 0 failed
    await _create_child_task(db_session, research_task.id, project.id,
                              status=TaskStatus.COMPLETED,
                              context={"pr_merged": True},
                              payloads=[{"type": "create", "scope": "small"}],
                              title="s1")
    await _create_child_task(db_session, research_task.id, project.id,
                              status=TaskStatus.COMPLETED,
                              context={},
                              payloads=[{"type": "create", "scope": "small"}],
                              title="s2")
    # medium: 1 created, 0 merged, 1 failed
    await _create_child_task(db_session, research_task.id, project.id,
                              status=TaskStatus.FAILED,
                              context={},
                              payloads=[{"type": "create", "scope": "medium"}],
                              title="m1")
    # no scope → unknown
    await _create_child_task(db_session, research_task.id, project.id,
                              status=TaskStatus.COMPLETED,
                              context={},
                              payloads=[{"type": "create"}],  # no scope key
                              title="u1")
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
async def test_scope_breakdown_unknown_default(db_session: AsyncSession):
    """Tasks with no scope field in first_payload default to 'unknown' scope."""
    direction_id = "d-unknown-scope"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)

    project.directions = [_make_direction(direction_id, research_task_id, tasks_created=1)]
    await db_session.flush()

    await _create_child_task(db_session, research_task.id, project.id,
                              status=TaskStatus.COMPLETED,
                              context={},
                              payloads=[{"type": "fix"}],  # no scope
                              title="no-scope")
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]
    assert "unknown" in entry["scope_breakdown"]
    assert entry["scope_breakdown"]["unknown"]["created"] == 1
