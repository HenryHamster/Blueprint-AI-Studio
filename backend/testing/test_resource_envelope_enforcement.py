"""Regression tests for project resource-envelope enforcement seams."""

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

import app.shared_state as _shared_state
from app.autonomous_research import _spawn_periodic_task, _spawn_research_task
from app.max_plan_pool import _account_by_id, _max_plan_pools
from app.models import Task
from testing.factories import make_project


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
        await db_session.execute(
            select(Task).where(
                Task.project_id == project.id,
                Task.created_by == "ORCHESTRATOR",
            )
        )
    ).scalars().all()
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
            _shared_state.runtime_config["autonomous_periodic_tasks"] = previous_periodic

    spawned = (
        await db_session.execute(
            select(Task).where(
                Task.project_id == project.id,
                Task.created_by == "ORCHESTRATOR",
            )
        )
    ).scalars().all()
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

    mocker.patch(
        "app.autonomous_research.AsyncSessionLocal", lambda: _session_cm()
    )
    mocker.patch(
        "app.anomaly_detection._expire_stale_proposals", new_callable=AsyncMock
    )

    # Simulate over-budget spend
    mocker.patch(
        "app.autonomous_research.get_project_recent_spend_usd",
        new=AsyncMock(return_value=15.0),
    )
    # Stub other spawn functions to avoid side-effects
    mocker.patch(
        "app.autonomous_research._spawn_research_task", new_callable=AsyncMock
    )
    mocker.patch(
        "app.autonomous_research._spawn_periodic_task", new_callable=AsyncMock
    )
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
