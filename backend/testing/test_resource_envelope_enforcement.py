"""Regression tests for project resource-envelope enforcement seams."""

from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient
from sqlalchemy import select

import app.shared_state as _shared_state
from app.alert_dispatch import (
    _SPEND_APPROACHING_ALERT_TITLE,
    _SPEND_CEILING_ALERT_TITLE,
)
from app.autonomous_research import _spawn_periodic_task, _spawn_research_task
from app.autonomous_research import _project_spend_ceiling_spawn_defer_reason
from app.max_plan_pool import _account_by_id, _max_plan_pools
from app.models import Alert, Project, Task
from app.orchestrator import (
    _filter_project_resource_dispatch_eligible,
    _project_resource_dispatch_defer_message,
)
from app.workbench_state import (
    STRATEGIC_LOCAL_RUNNER_REASON_CONTEXT_KEY,
    STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY,
)
from testing.factories import make_membership, make_project, make_task


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
        new=AsyncMock(
            return_value={"total": 10.0, "compute": 10.0, "agent_vm": 0.0, "idle": 0.0}
        ),
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
        new=AsyncMock(
            return_value={"total": 7.5, "compute": 7.5, "agent_vm": 0.0, "idle": 0.0}
        ),
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
        new=AsyncMock(
            return_value={"total": 15.0, "compute": 15.0, "agent_vm": 0.0, "idle": 0.0}
        ),
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
async def test_dispatch_resource_cache_refreshes_project_expired_by_rollback(db_session):
    """A rollback-expired cached Project must not lazy-load in async context."""
    project = make_project(
        build_config={"resource_envelope": {"max_concurrent_vms": 3}}
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = make_task(project_id=project.id)
    db_session.add(task)
    await db_session.commit()

    project_cache = {}
    active_counts = {project.id: 1}
    assert (
        await _project_resource_dispatch_defer_message(
            db_session,
            task,
            active_counts,
            project_cache=project_cache,
        )
        is None
    )

    cached_project = project_cache[project.id]
    db_session.expire(cached_project, ["build_config"])

    # Before the fix this second call synchronously lazy-loaded build_config
    # from the expired cache entry and raised sqlalchemy.exc.MissingGreenlet.
    assert (
        await _project_resource_dispatch_defer_message(
            db_session,
            task,
            active_counts,
            project_cache=project_cache,
        )
        is None
    )


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
        new=AsyncMock(
            return_value={"total": 10.0, "compute": 10.0, "agent_vm": 0.0, "idle": 0.0}
        ),
    )

    msg = await _project_resource_dispatch_defer_message(db_session, task, {})

    assert msg is not None
    assert "spend budget" in msg
    assert "$10.00/$10.00" in msg


@pytest.mark.asyncio
async def test_dispatch_filter_keeps_local_required_strategic_at_spend_ceiling(
    db_session, mocker
):
    """Local-required Strategic work is handled by runner pin/hold, not cloud spend deferral."""
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

    task = make_task(
        project_id=project.id,
        payloads=[{"type": "strategic_review"}],
        context={
            STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY: True,
            STRATEGIC_LOCAL_RUNNER_REASON_CONTEXT_KEY: "workbench_continuous",
        },
    )
    db_session.add(task)
    await db_session.commit()

    spend_mock = mocker.patch(
        "app.orchestrator.get_project_recent_spend_usd",
        new=AsyncMock(
            return_value={"total": 10.0, "compute": 10.0, "agent_vm": 0.0, "idle": 0.0}
        ),
    )
    broadcast = AsyncMock()

    result = await _filter_project_resource_dispatch_eligible(
        db_session,
        [task.id],
        [task],
        {},
        broadcast,
    )

    assert result == [task.id]
    spend_mock.assert_not_called()
    broadcast.assert_not_called()


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
        new=AsyncMock(
            return_value={"total": 9.99, "compute": 9.99, "agent_vm": 0.0, "idle": 0.0}
        ),
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


# ---------------------------------------------------------------------------
# Hard-pause (pause_on_ceiling_exceeded) enforcement tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hard_pause_triggers_on_ceiling_exceeded(db_session, mocker):
    """pause_on_ceiling_exceeded=True + spend at ceiling -> project.paused=True + broadcast."""
    project = make_project(
        build_config={
            "resource_envelope": {
                "spend_ceiling_usd": 10.0,
                "spend_window_days": 7,
                "pause_on_ceiling_exceeded": True,
            }
        }
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    assert project.paused is False

    task = make_task(project_id=project.id)
    db_session.add(task)
    await db_session.commit()

    mocker.patch(
        "app.orchestrator.get_project_recent_spend_usd",
        new=AsyncMock(
            return_value={"total": 10.0, "compute": 10.0, "agent_vm": 0.0, "idle": 0.0}
        ),
    )
    broadcast = AsyncMock()

    result = await _filter_project_resource_dispatch_eligible(
        db_session,
        [task.id],
        [task],
        {},
        broadcast,
    )

    # Task was deferred (spend at ceiling)
    assert result == []

    # Project is now hard-paused
    await db_session.refresh(project)
    assert project.paused is True

    # Broadcast for the pause event must have been sent
    broadcast.assert_any_call(
        str(project.id),
        "project_spend_ceiling_exceeded",
        {"project_id": str(project.id)},
    )


@pytest.mark.asyncio
async def test_hard_pause_not_triggers_when_flag_not_set(db_session, mocker):
    """Without pause_on_ceiling_exceeded, project is NOT paused even at ceiling."""
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
    assert project.paused is False

    task = make_task(project_id=project.id)
    db_session.add(task)
    await db_session.commit()

    mocker.patch(
        "app.orchestrator.get_project_recent_spend_usd",
        new=AsyncMock(
            return_value={"total": 12.0, "compute": 12.0, "agent_vm": 0.0, "idle": 0.0}
        ),
    )
    broadcast = AsyncMock()

    result = await _filter_project_resource_dispatch_eligible(
        db_session,
        [task.id],
        [task],
        {},
        broadcast,
    )

    assert result == []

    # Project must NOT be paused
    await db_session.refresh(project)
    assert project.paused is False

    # No pause broadcast was sent
    pause_calls = [
        call
        for call in broadcast.call_args_list
        if call[0][1] == "project_spend_ceiling_exceeded"
    ]
    assert pause_calls == []


@pytest.mark.asyncio
async def test_hard_pause_no_refire_when_already_paused(db_session, mocker):
    """Already-paused project should not re-broadcast spend_ceiling_exceeded."""
    project = make_project(
        build_config={
            "resource_envelope": {
                "spend_ceiling_usd": 10.0,
                "spend_window_days": 7,
                "pause_on_ceiling_exceeded": True,
            }
        },
        # Pre-pause the project
        paused=True,
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    assert project.paused is True

    task = make_task(project_id=project.id)
    db_session.add(task)
    await db_session.commit()

    mocker.patch(
        "app.orchestrator.get_project_recent_spend_usd",
        new=AsyncMock(
            return_value={"total": 15.0, "compute": 15.0, "agent_vm": 0.0, "idle": 0.0}
        ),
    )
    broadcast = AsyncMock()

    result = await _filter_project_resource_dispatch_eligible(
        db_session,
        [task.id],
        [task],
        {},
        broadcast,
    )

    assert result == []

    # Project must still be paused
    await db_session.refresh(project)
    assert project.paused is True

    # No SPEND-CEILING-EXCEEDED broadcast was sent (no re-fire)
    pause_calls = [
        call
        for call in broadcast.call_args_list
        if call[0][1] == "project_spend_ceiling_exceeded"
    ]
    assert pause_calls == []


# ---------------------------------------------------------------------------
# Approaching-ceiling proactive warning alert tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approaching_ceiling_warning_fires_in_configured_band(db_session, mocker):
    """Approaching-ceiling alert fires when spend >= ceiling * warning_fraction but < ceiling."""
    project = make_project(
        autonomous_enabled=True,
        directions=[{"id": "d1", "status": "active", "text": "reduce cost"}],
        vision={"purpose": "test"},
        build_config={
            "resource_envelope": {
                "spend_ceiling_usd": 10.0,
                "spend_window_days": 7,
                "spend_warning_fraction": 0.8,
            }
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Spend at 85% of ceiling — above the 0.8 threshold but below the ceiling
    mocker.patch(
        "app.autonomous_research.get_project_recent_spend_usd",
        new=AsyncMock(
            return_value={"total": 8.5, "compute": 8.5, "agent_vm": 0.0, "idle": 0.0}
        ),
    )

    result = await _project_spend_ceiling_spawn_defer_reason(project, db_session)

    # Should NOT block spawning
    assert result is None

    # Approaching-ceiling alert must have been created
    alerts = (
        (
            await db_session.execute(
                select(Alert).where(
                    Alert.project_id == project.id,
                    Alert.title == _SPEND_APPROACHING_ALERT_TITLE,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 1
    assert "80%" in alerts[0].message


@pytest.mark.asyncio
async def test_approaching_ceiling_warning_not_fired_below_threshold(
    db_session, mocker
):
    """No approaching-ceiling alert when spend is below the warning threshold."""
    project = make_project(
        autonomous_enabled=True,
        build_config={
            "resource_envelope": {
                "spend_ceiling_usd": 10.0,
                "spend_window_days": 7,
                "spend_warning_fraction": 0.8,
            }
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Spend at 70% of ceiling — below the 0.8 threshold
    mocker.patch(
        "app.autonomous_research.get_project_recent_spend_usd",
        new=AsyncMock(
            return_value={"total": 7.0, "compute": 7.0, "agent_vm": 0.0, "idle": 0.0}
        ),
    )

    result = await _project_spend_ceiling_spawn_defer_reason(project, db_session)

    assert result is None

    alerts = (
        (
            await db_session.execute(
                select(Alert).where(
                    Alert.project_id == project.id,
                    Alert.title == _SPEND_APPROACHING_ALERT_TITLE,
                )
            )
        )
        .scalars()
        .all()
    )
    assert alerts == []


@pytest.mark.asyncio
async def test_at_ceiling_creates_ceiling_alert_not_approaching(db_session, mocker):
    """When spend is at/above the ceiling, the AT-CEILING alert fires, not the approaching alert."""
    project = make_project(
        autonomous_enabled=True,
        build_config={
            "resource_envelope": {
                "spend_ceiling_usd": 10.0,
                "spend_window_days": 7,
                "spend_warning_fraction": 0.8,
            }
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Spend exactly at the ceiling
    mocker.patch(
        "app.autonomous_research.get_project_recent_spend_usd",
        new=AsyncMock(
            return_value={"total": 10.0, "compute": 10.0, "agent_vm": 0.0, "idle": 0.0}
        ),
    )

    result = await _project_spend_ceiling_spawn_defer_reason(project, db_session)

    # Spawning must be blocked
    assert result is not None
    assert "spend ceiling reached" in result

    ceiling_alerts = (
        (
            await db_session.execute(
                select(Alert).where(
                    Alert.project_id == project.id,
                    Alert.title == _SPEND_CEILING_ALERT_TITLE,
                )
            )
        )
        .scalars()
        .all()
    )
    approaching_alerts = (
        (
            await db_session.execute(
                select(Alert).where(
                    Alert.project_id == project.id,
                    Alert.title == _SPEND_APPROACHING_ALERT_TITLE,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(ceiling_alerts) == 1
    assert approaching_alerts == []


@pytest.mark.asyncio
async def test_spend_ceiling_alert_contains_window_days(db_session, mocker):
    """Spend ceiling alert message contains the configured window days, not hardcoded '24h'."""
    project = make_project(
        autonomous_enabled=True,
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

    # Spend at the ceiling
    mocker.patch(
        "app.autonomous_research.get_project_recent_spend_usd",
        new=AsyncMock(return_value={"total": 10.0, "compute": 10.0, "agent_vm": 0.0, "idle": 0.0}),
    )

    result = await _project_spend_ceiling_spawn_defer_reason(project, db_session)

    # Alert must be created
    assert result is not None

    alerts = (
        (
            await db_session.execute(
                select(Alert).where(
                    Alert.project_id == project.id,
                    Alert.title == _SPEND_CEILING_ALERT_TITLE,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 1
    assert "7-day" in alerts[0].message
    assert "24h" not in alerts[0].message


@pytest.mark.asyncio
async def test_approaching_ceiling_alert_contains_window_days(db_session, mocker):
    """Approaching ceiling alert message contains the configured window days."""
    project = make_project(
        autonomous_enabled=True,
        build_config={
            "resource_envelope": {
                "spend_ceiling_usd": 10.0,
                "spend_window_days": 14,
                "spend_warning_fraction": 0.8,
            }
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Spend at 85% of ceiling — above the 0.8 threshold but below the ceiling
    mocker.patch(
        "app.autonomous_research.get_project_recent_spend_usd",
        new=AsyncMock(return_value={"total": 8.5, "compute": 8.5, "agent_vm": 0.0, "idle": 0.0}),
    )

    result = await _project_spend_ceiling_spawn_defer_reason(project, db_session)

    # Should NOT block spawning
    assert result is None

    # Approaching-ceiling alert must have been created
    alerts = (
        (
            await db_session.execute(
                select(Alert).where(
                    Alert.project_id == project.id,
                    Alert.title == _SPEND_APPROACHING_ALERT_TITLE,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 1
    assert "14-day" in alerts[0].message


# ---------------------------------------------------------------------------
# spend_warning_fraction API round-trip
# ---------------------------------------------------------------------------


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_spend_warning_fraction_patch_round_trip(client: AsyncClient, db_session):
    """PATCH spend_warning_fraction=0.7, then GET returns 0.7; clearing returns default 0.8."""
    project = Project(
        name="Warning Fraction Test", user_id="test-user-123", is_self_repo=True
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    member = make_membership(project.id, user_id="test-user-123", role="owner")
    db_session.add(member)
    await db_session.commit()

    # PATCH with spend_warning_fraction=0.7
    resp = await client.patch(
        f"/api/admin/projects/{project.id}/resource-envelope",
        json={"spend_warning_fraction": 0.7},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["spend_warning_fraction"] == pytest.approx(0.7)

    # GET returns the persisted value
    resp2 = await client.get(f"/api/admin/projects/{project.id}/resource-envelope")
    assert resp2.status_code == 200, resp2.text
    assert resp2.json()["spend_warning_fraction"] == pytest.approx(0.7)

    # PATCH with null clears the override; GET returns the default (0.8)
    resp3 = await client.patch(
        f"/api/admin/projects/{project.id}/resource-envelope",
        json={"spend_warning_fraction": None},
    )
    assert resp3.status_code == 200, resp3.text
    assert resp3.json()["spend_warning_fraction"] == pytest.approx(0.8)

    resp4 = await client.get(f"/api/admin/projects/{project.id}/resource-envelope")
    assert resp4.status_code == 200, resp4.text
    assert resp4.json()["spend_warning_fraction"] == pytest.approx(0.8)


@pytest.mark.asyncio
async def test_patch_resource_envelope_rejects_out_of_range_spend_warning_fraction(
    client: AsyncClient, db_session
):
    """PATCH /resource-envelope rejects spend_warning_fraction outside (0, 1] with 422."""
    project = Project(name="Envelope Warning Reject Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    member = make_membership(project.id, user_id="test-user-123", role="owner")
    db_session.add(member)
    await db_session.commit()

    resp = await client.patch(
        f"/api/admin/projects/{project.id}/resource-envelope",
        json={"spend_warning_fraction": 1.5},
    )
    assert resp.status_code == 422, resp.text


# ---------------------------------------------------------------------------
# pause_on_ceiling_exceeded auto-pause enforcement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_defers_at_ceiling_via_defer_message(
    db_session, mocker
):
    """When spend exceeds ceiling, _filter_project_resource_dispatch_eligible
    filters out the task via a deferral message AND hard-pauses the project
    when pause_on_ceiling_exceeded is set (both mechanisms are active:
    the task gets a defer message, and the project is paused)."""
    project = make_project(
        build_config={
            "resource_envelope": {
                "spend_ceiling_usd": 10.0,
                "spend_window_days": 7,
                "pause_on_ceiling_exceeded": True,
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
        new=AsyncMock(
            return_value={"total": 10.0, "compute": 10.0, "agent_vm": 0.0, "idle": 0.0}
        ),
    )
    broadcast = AsyncMock()

    result = await _filter_project_resource_dispatch_eligible(
        db_session,
        [task.id],
        [task],
        {},
        broadcast,
    )

    # Task is deferred because spend ceiling is exceeded
    assert result == []

    # The defer message is set on the task (the "via defer message" behavior)
    assert task.latest_status_message is not None
    assert "spend budget" in task.latest_status_message
    assert "$10.00/$10.00" in task.latest_status_message

    # Project IS hard-paused because pause_on_ceiling_exceeded is True
    await db_session.refresh(project)
    assert project.paused is True


@pytest.mark.asyncio
async def test_dispatch_defer_message_contains_spend_details(
    db_session, mocker
):
    """The defer message should include the current spend and ceiling values
    for operator visibility."""
    project = make_project(
        build_config={
            "resource_envelope": {
                "spend_ceiling_usd": 50.0,
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
        new=AsyncMock(
            return_value={"total": 50.0, "compute": 50.0, "agent_vm": 0.0, "idle": 0.0}
        ),
    )
    broadcast = AsyncMock()

    # Patch _project_resource_dispatch_defer_message to capture the reason
    import app.orchestrator as _orch

    orig_fn = _orch._project_resource_dispatch_defer_message
    captured_reason = None

    async def capturing_fn(*args, **kwargs):
        nonlocal captured_reason
        captured_reason = await orig_fn(*args, **kwargs)
        return captured_reason

    mocker.patch.object(
        _orch,
        "_project_resource_dispatch_defer_message",
        capturing_fn,
    )

    result = await _filter_project_resource_dispatch_eligible(
        db_session,
        [task.id],
        [task],
        {},
        broadcast,
    )

    assert result == []
    assert captured_reason is not None
    assert "50.00" in captured_reason
    assert "spend budget" in captured_reason
