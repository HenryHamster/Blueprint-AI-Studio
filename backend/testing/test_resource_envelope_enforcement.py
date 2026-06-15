"""Regression tests for project resource-envelope enforcement seams."""

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

import app.shared_state as _shared_state
from app.autonomous_research import _spawn_periodic_task, _spawn_research_task
from app.max_plan_pool import _account_by_id, _max_plan_pools
from app.models import Task
from app.orchestrator import (
    _filter_project_resource_dispatch_eligible,
    _project_resource_dispatch_defer_message,
)
from testing.factories import make_project, make_task


@pytest.fixture(autouse=True)
def _available_account_pool():
    uid = "test-user-123"
    entry = {
        "id": "resource-envelope-test-acct",
        "user_id": uid,
        "name": "resource envelope test",
        "enabled": True,
        "active_tasks": 0,
        "oauth_token": "sk-ant-test",
        "total_tasks": 0,
        "token_created_at": None,
        "claude_config_json": None,
    }
    _max_plan_pools.setdefault(uid, []).append(entry)
    _account_by_id[entry["id"]] = entry
    yield
    _max_plan_pools.get(uid, [])[:] = [
        acct for acct in _max_plan_pools.get(uid, []) if acct["id"] != entry["id"]
    ]
    _account_by_id.pop(entry["id"], None)


@pytest.mark.asyncio
async def test_spend_ceiling_blocks_autonomous_research_spawn(db_session, mocker):
    project = make_project(
        autonomous_enabled=True,
        directions=[{"id": "d1", "status": "active", "text": "reduce cost drift"}],
        vision={"purpose": "test"},
        build_config={
            "resource_envelope": {
                "spend_ceiling_usd": 10.0,
                "spend_window_days": 7,
            }
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    spend_mock = mocker.patch(
        "app.autonomous_research.get_project_recent_spend_usd",
        new=AsyncMock(return_value=10.0),
    )

    await _spawn_research_task(project, db_session)

    spawned = (
        (
            await db_session.execute(
                select(Task).where(
                    Task.project_id == project.id,
                    Task.created_by == "ORCHESTRATOR",
                )
            )
        )
        .scalars()
        .all()
    )
    assert spawned == []
    spend_mock.assert_awaited_once_with(
        db_session,
        project_id=project.id,
        window_days=7,
    )


@pytest.mark.asyncio
async def test_spend_ceiling_blocks_autonomous_periodic_spawn(db_session, mocker):
    project = make_project(
        autonomous_enabled=True,
        build_config={
            "resource_envelope": {
                "spend_ceiling_usd": 5.0,
                "spend_window_days": 3,
            }
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    spend_mock = mocker.patch(
        "app.autonomous_research.get_project_recent_spend_usd",
        new=AsyncMock(return_value=7.5),
    )
    previous_periodic = _shared_state.runtime_config.get("autonomous_periodic_tasks")
    _shared_state.runtime_config["autonomous_periodic_tasks"] = {
        "security_audit": {"enabled": True, "interval_hours": 0}
    }
    try:
        await _spawn_periodic_task(project, "security_audit", db_session)
    finally:
        if previous_periodic is None:
            _shared_state.runtime_config.pop("autonomous_periodic_tasks", None)
        else:
            _shared_state.runtime_config["autonomous_periodic_tasks"] = (
                previous_periodic
            )

    spawned = (
        (
            await db_session.execute(
                select(Task).where(
                    Task.project_id == project.id,
                    Task.created_by == "ORCHESTRATOR",
                )
            )
        )
        .scalars()
        .all()
    )
    assert spawned == []
    spend_mock.assert_awaited_once_with(
        db_session,
        project_id=project.id,
        window_days=3,
    )


@pytest.mark.asyncio
async def test_spend_ceiling_blocks_residual_remediation_spawn(db_session, mocker):
    """_check_idle_research must NOT call _spawn_residual_fingerprint_remediation
    when the project is at or over its spend ceiling."""
    import contextlib
    from app.autonomous_research import _check_idle_research

    project = make_project(
        autonomous_enabled=True,
        directions=[{"id": "d1", "status": "active", "text": "reduce cost drift"}],
        vision={"purpose": "test"},
        build_config={
            "resource_envelope": {
                "spend_ceiling_usd": 10.0,
                "spend_window_days": 7,
            }
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Bind the module's AsyncSessionLocal to the test session so it sees
    # committed rows without touching the production DB engine.
    @contextlib.asynccontextmanager
    async def _session_cm():
        yield db_session

    mocker.patch("app.autonomous_research.AsyncSessionLocal", lambda: _session_cm())
    mocker.patch(
        "app.anomaly_detection._expire_stale_proposals", new_callable=AsyncMock
    )

    # Simulate over-budget spend
    mocker.patch(
        "app.autonomous_research.get_project_recent_spend_usd",
        new=AsyncMock(return_value=15.0),
    )
    # Stub other spawn functions to avoid side-effects
    mocker.patch("app.autonomous_research._spawn_research_task", new_callable=AsyncMock)
    mocker.patch("app.autonomous_research._spawn_periodic_task", new_callable=AsyncMock)
    residual_mock = mocker.patch(
        "app.autonomous_research._spawn_residual_fingerprint_remediation",
        new_callable=AsyncMock,
        return_value=0,
    )

    _shared_state.last_idle_check = None
    try:
        await _check_idle_research()
    finally:
        _shared_state.last_idle_check = None

    residual_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Dispatch-time resource-envelope enforcement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_defers_task_at_project_vm_cap(db_session, mocker):
    """Project at its concurrent-VM cap gets a VM-capacity deferral message."""
    project = make_project(
        build_config={"resource_envelope": {"max_concurrent_vms": 2}}
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = make_task(project_id=project.id)
    db_session.add(task)
    await db_session.commit()

    # Simulate 2 already-active tasks (at cap)
    active_counts = {project.id: 2}
    msg = await _project_resource_dispatch_defer_message(
        db_session, task, active_counts
    )

    assert msg is not None
    assert "VM capacity" in msg
    assert "2/2" in msg


@pytest.mark.asyncio
async def test_dispatch_passes_task_under_vm_cap(db_session):
    """Project with active tasks below cap returns None (no deferral)."""
    project = make_project(
        build_config={"resource_envelope": {"max_concurrent_vms": 3}}
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = make_task(project_id=project.id)
    db_session.add(task)
    await db_session.commit()

    active_counts = {project.id: 1}
    msg = await _project_resource_dispatch_defer_message(
        db_session, task, active_counts
    )

    assert msg is None


@pytest.mark.asyncio
async def test_dispatch_projected_count_stops_double_admit(db_session, mocker):
    """With cap=1 and active=0, only the first of two queued tasks is admitted."""
    project = make_project(
        build_config={"resource_envelope": {"max_concurrent_vms": 1}}
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task1 = make_task(project_id=project.id)
    task2 = make_task(project_id=project.id)
    db_session.add(task1)
    db_session.add(task2)
    await db_session.commit()

    broadcast = AsyncMock()
    active_counts = {project.id: 0}
    result = await _filter_project_resource_dispatch_eligible(
        db_session,
        [task1.id, task2.id],
        [task1, task2],
        active_counts,
        broadcast,
    )

    # Only the first task is admitted; second is deferred by projected count
    assert result == [task1.id]


@pytest.mark.asyncio
async def test_dispatch_defers_task_at_spend_ceiling(db_session, mocker):
    """Project at or over its spend ceiling is deferred with a spend-budget message."""
    project = make_project(
        build_config={
            "resource_envelope": {
                "spend_ceiling_usd": 10.0,
                "spend_window_days": 7,
            }
        }
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = make_task(project_id=project.id)
    db_session.add(task)
    await db_session.commit()

    mocker.patch(
        "app.orchestrator.get_project_recent_spend_usd",
        new=AsyncMock(return_value=10.0),
    )

    msg = await _project_resource_dispatch_defer_message(db_session, task, {})

    assert msg is not None
    assert "spend budget" in msg
    assert "$10.00/$10.00" in msg


@pytest.mark.asyncio
async def test_dispatch_passes_task_under_spend_ceiling(db_session, mocker):
    """Project below its spend ceiling returns None (no deferral)."""
    project = make_project(
        build_config={
            "resource_envelope": {
                "spend_ceiling_usd": 10.0,
                "spend_window_days": 7,
            }
        }
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = make_task(project_id=project.id)
    db_session.add(task)
    await db_session.commit()

    mocker.patch(
        "app.orchestrator.get_project_recent_spend_usd",
        new=AsyncMock(return_value=9.99),
    )

    msg = await _project_resource_dispatch_defer_message(db_session, task, {})

    assert msg is None


@pytest.mark.asyncio
async def test_dispatch_passes_task_with_no_envelope(db_session):
    """Project with no resource envelope configured passes through unaffected."""
    project = make_project(build_config={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = make_task(project_id=project.id)
    db_session.add(task)
    await db_session.commit()

    broadcast = AsyncMock()
    result = await _filter_project_resource_dispatch_eligible(
        db_session,
        [task.id],
        [task],
        {},
        broadcast,
    )

    assert result == [task.id]
    broadcast.assert_not_called()


@pytest.mark.asyncio
async def test_dispatch_missing_project_returns_sentinel(db_session):
    """If the project row is absent from the DB, a sentinel string is returned."""
    import uuid as _uuid

    task = make_task(project_id=_uuid.uuid4())
    # Do not persist the task or project — project_id is unknown to the DB

    msg = await _project_resource_dispatch_defer_message(db_session, task, {})

    assert msg is not None
    assert "project record unavailable" in msg


@pytest.mark.asyncio
async def test_filter_broadcasts_on_new_defer_message(db_session, mocker):
    """Deferred task with a new status message triggers broadcast_to_project."""
    project = make_project(
        build_config={"resource_envelope": {"max_concurrent_vms": 1}}
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task1 = make_task(project_id=project.id)
    task2 = make_task(project_id=project.id, latest_status_message=None)
    db_session.add(task1)
    db_session.add(task2)
    await db_session.commit()

    broadcast = AsyncMock()
    await _filter_project_resource_dispatch_eligible(
        db_session,
        [task1.id, task2.id],
        [task1, task2],
        {project.id: 0},
        broadcast,
    )

    # task2 is deferred (projected count from task1 fills the cap)
    # broadcast should be called for task2's new deferral message
    broadcast.assert_called_once()
    call_args = broadcast.call_args
    assert str(project.id) == call_args[0][0]
    assert call_args[0][1] == "task_updated"


@pytest.mark.asyncio
async def test_filter_no_broadcast_when_message_unchanged(db_session, mocker):
    """No broadcast when the deferred task already has the same status message."""
    project = make_project(
        build_config={"resource_envelope": {"max_concurrent_vms": 1}}
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task1 = make_task(project_id=project.id)
    # Pre-set task2's message to match the deferral message it will receive
    expected_msg = (
        "Waiting for project VM capacity — 1/1 project VM slots are already in use"
    )
    task2 = make_task(project_id=project.id, latest_status_message=expected_msg)
    db_session.add(task1)
    db_session.add(task2)
    await db_session.commit()

    broadcast = AsyncMock()
    await _filter_project_resource_dispatch_eligible(
        db_session,
        [task1.id, task2.id],
        [task1, task2],
        {project.id: 0},
        broadcast,
    )

    # task2 is deferred but message is unchanged — no broadcast
    broadcast.assert_not_called()


@pytest.mark.asyncio
async def test_dispatch_defers_when_idle_cost_pushes_over_ceiling(db_session):
    """Task/compute spend alone is below ceiling, but pool idle cost pushes total over — task must defer."""
    from app.models import PoolIdleSpan
    from datetime import timedelta

    # Ceiling: $10. Task/compute spend will be $0 (no tasks or jobs seeded).
    # Pool idle span: $12 — this alone pushes the total over the ceiling.
    project = make_project(
        build_config={
            "resource_envelope": {
                "spend_ceiling_usd": 10.0,
                "spend_window_days": 30,
            }
        }
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Seed a PoolIdleSpan within the window for this project.
    from app.time_utils import _utcnow as _now

    now = _now()
    span = PoolIdleSpan(
        project_id=project.id,
        vm_instance_id="idle-vm-over-ceiling",
        started_at=now - timedelta(days=2, hours=1),
        settled_at=now - timedelta(days=2),
        cost_usd=12.0,
    )
    db_session.add(span)
    await db_session.commit()

    task = make_task(project_id=project.id)
    db_session.add(task)
    await db_session.commit()

    msg = await _project_resource_dispatch_defer_message(db_session, task, {})

    assert msg is not None
    assert "spend budget" in msg
