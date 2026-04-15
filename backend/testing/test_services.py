"""
Tests for recovery functions in backend/app/services.py.

Covers:
  1. _recover_orphaned_tasks — COMPLETED tasks with no PR artifact / no review child
  2. _persist_circuit_breakers — serialize in-memory breaker state to DeploymentState
  3. _persist_zone_health — serialize zone health snapshot to DeploymentState

Uses isolated in-memory SQLite DB (same pattern as test_pipeline_reliability.py).
"""
import uuid
import pytest
import pytest_asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, select

import app.shared_state as _shared_state
from app.services import _recover_orphaned_tasks, _persist_circuit_breakers, _persist_zone_health
from app.models import (
    Project, Task, TaskStatus, Artifact, ArtifactType,
    DeploymentState,
)

# ── SQLite JSONB compatibility ────────────────────────────────────────────────

from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler as _SQLiteTypeCompiler
if not hasattr(_SQLiteTypeCompiler, "visit_JSONB"):
    _SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: self.visit_JSON(type_, **kw)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _naive_utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _naive_utc_ago(**kwargs) -> datetime:
    return (datetime.now(timezone.utc) - timedelta(**kwargs)).replace(tzinfo=None)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture()
async def svc_db(mocker):
    """Isolated in-memory SQLite DB for services tests.

    Patches app.services.AsyncSessionLocal so functions use this DB.
    """
    _engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with _engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    _Session = sessionmaker(
        bind=_engine, class_=AsyncSession,
        autocommit=False, autoflush=False, expire_on_commit=False,
    )
    mocker.patch("app.services.AsyncSessionLocal", _Session)

    # Suppress broadcast calls across all tests in this fixture
    mocker.patch("app.services.broadcast_to_project", new_callable=AsyncMock)

    async with _Session() as session:
        yield session

    async with _engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
    await _engine.dispose()


async def _make_project(session) -> Project:
    project = Project(name="Test Project", user_id="user1")
    session.add(project)
    await session.commit()
    await session.refresh(project)
    return project


async def _make_completed_task(session, project_id, task_type="create", minutes_ago=30) -> Task:
    """Create a COMPLETED, ORCHESTRATOR-created task of the given type."""
    task = Task(
        project_id=project_id,
        payloads=[{"type": task_type, "prompt": f"do {task_type}"}],
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        created_at=_naive_utc_ago(minutes=minutes_ago),
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task


# ══════════════════════════════════════════════════════════════════════════════
# _recover_orphaned_tasks
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_orphan_recovery_attempts_github_lookup(svc_db, mocker):
    """A COMPLETED ORCHESTRATOR create-task with no PR artifact and no review child
    should attempt a GitHub PR lookup."""
    project = await _make_project(svc_db)
    task = await _make_completed_task(svc_db, project.id, task_type="create")

    # Mock token fetch
    mock_get_token = AsyncMock(return_value=("gh_token", "owner", "repo"))
    mocker.patch("app.routers.agent.get_github_token_for_task", mock_get_token)

    # Mock GitHub PR lookup returning no open PRs (to avoid spawn logic)
    mock_gh_resp = MagicMock()
    mock_gh_resp.status_code = 200
    mock_gh_resp.json.return_value = []
    mock_github_request = AsyncMock(return_value=mock_gh_resp)
    mocker.patch("app.github._github_request", mock_github_request)

    await _recover_orphaned_tasks()

    mock_get_token.assert_called_once()
    mock_github_request.assert_called_once()


@pytest.mark.asyncio
async def test_orphan_recovery_skips_task_with_pr_artifact(svc_db, mocker):
    """A COMPLETED task that already has a PR artifact should be skipped."""
    project = await _make_project(svc_db)
    task = await _make_completed_task(svc_db, project.id, task_type="create")

    # Create a PR artifact
    artifact = Artifact(
        task_id=task.id,
        artifact_type=ArtifactType.PULL_REQUEST,
        pr_url="https://github.com/owner/repo/pull/42",
    )
    svc_db.add(artifact)
    await svc_db.commit()

    mock_get_token = AsyncMock(return_value=("gh_token", "owner", "repo"))
    mocker.patch("app.routers.agent.get_github_token_for_task", mock_get_token)

    await _recover_orphaned_tasks()

    # Token fetch should never be called — task was skipped
    mock_get_token.assert_not_called()


@pytest.mark.asyncio
async def test_orphan_recovery_skips_task_with_review_child(svc_db, mocker):
    """A COMPLETED task that already has a review child should be skipped."""
    project = await _make_project(svc_db)
    parent = await _make_completed_task(svc_db, project.id, task_type="create")

    child = Task(
        project_id=project.id,
        parent_task_id=parent.id,
        payloads=[{"type": "review", "pr_number": 1, "branch": "blueprint/task-abc"}],
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
    )
    svc_db.add(child)
    await svc_db.commit()

    mock_get_token = AsyncMock(return_value=("gh_token", "owner", "repo"))
    mocker.patch("app.routers.agent.get_github_token_for_task", mock_get_token)

    await _recover_orphaned_tasks()

    mock_get_token.assert_not_called()


@pytest.mark.asyncio
async def test_orphan_recovery_skips_research_type(svc_db, mocker):
    """Tasks with type='research' are not code-producing and must be skipped."""
    project = await _make_project(svc_db)
    await _make_completed_task(svc_db, project.id, task_type="research")

    mock_get_token = AsyncMock(return_value=("gh_token", "owner", "repo"))
    mocker.patch("app.routers.agent.get_github_token_for_task", mock_get_token)

    await _recover_orphaned_tasks()

    mock_get_token.assert_not_called()


@pytest.mark.asyncio
async def test_orphan_recovery_github_token_failure_skips_gracefully(svc_db, mocker):
    """If GitHub token fetch raises, the task should be logged+skipped without crashing."""
    project = await _make_project(svc_db)
    await _make_completed_task(svc_db, project.id, task_type="create")

    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        side_effect=Exception("token fetch failed"),
    )

    # Should not raise
    await _recover_orphaned_tasks()


@pytest.mark.asyncio
async def test_orphan_recovery_creates_artifact_and_review_when_pr_found(svc_db, mocker):
    """When GitHub returns an open PR, a PR artifact and review child must be created."""
    project = await _make_project(svc_db)
    task = await _make_completed_task(svc_db, project.id, task_type="create")

    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("gh_token", "owner", "repo"),
    )

    pr_url = "https://github.com/owner/repo/pull/99"
    mock_gh_resp = MagicMock()
    mock_gh_resp.status_code = 200
    mock_gh_resp.json.return_value = [{"html_url": pr_url, "number": 99}]
    mocker.patch("app.github._github_request", AsyncMock(return_value=mock_gh_resp))

    mock_review_task = Task(
        project_id=project.id,
        payloads=[{"type": "review", "pr_number": 99, "branch": "blueprint/task-abc"}],
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
    )
    mocker.patch(
        "app.review_pipeline._spawn_pipeline_task",
        new_callable=AsyncMock,
        return_value=(mock_review_task, True),
    )

    await _recover_orphaned_tasks()

    # Verify artifact was created
    artifact = (await svc_db.execute(
        select(Artifact).where(
            Artifact.task_id == task.id,
            Artifact.artifact_type == ArtifactType.PULL_REQUEST,
        )
    )).scalars().first()
    assert artifact is not None
    assert artifact.pr_url == pr_url


@pytest.mark.asyncio
async def test_orphan_recovery_only_checks_recent_tasks(svc_db, mocker):
    """Tasks older than 1 hour should not be included in the orphan sweep."""
    project = await _make_project(svc_db)
    # Create a task older than 1 hour
    await _make_completed_task(svc_db, project.id, task_type="create", minutes_ago=90)

    mock_get_token = AsyncMock(return_value=("gh_token", "owner", "repo"))
    mocker.patch("app.routers.agent.get_github_token_for_task", mock_get_token)

    await _recover_orphaned_tasks()

    # Task is outside the 1h window, so no GitHub call should occur
    mock_get_token.assert_not_called()


@pytest.mark.asyncio
async def test_orphan_recovery_skips_too_recent_tasks(svc_db, mocker):
    """Tasks created less than 5 minutes ago should not be swept (still completing)."""
    project = await _make_project(svc_db)
    # Create a task that is only 2 minutes old
    await _make_completed_task(svc_db, project.id, task_type="create", minutes_ago=2)

    mock_get_token = AsyncMock(return_value=("gh_token", "owner", "repo"))
    mocker.patch("app.routers.agent.get_github_token_for_task", mock_get_token)

    await _recover_orphaned_tasks()

    mock_get_token.assert_not_called()


# ══════════════════════════════════════════════════════════════════════════════
# _persist_circuit_breakers
# ══════════════════════════════════════════════════════════════════════════════

@pytest_asyncio.fixture()
async def deploy_db(mocker):
    """Isolated in-memory SQLite DB for _persist_circuit_breakers / _persist_zone_health tests."""
    _engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with _engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    _Session = sessionmaker(
        bind=_engine, class_=AsyncSession,
        autocommit=False, autoflush=False, expire_on_commit=False,
    )
    mocker.patch("app.services.AsyncSessionLocal", _Session)

    # Seed the singleton DeploymentState row
    async with _Session() as session:
        state = DeploymentState(id=1, current_revision="r1", current_sha="abc123")
        session.add(state)
        await session.commit()

    async with _Session() as session:
        yield session, _Session

    async with _engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
    await _engine.dispose()


@pytest.mark.asyncio
async def test_persist_circuit_breakers_saves_open_breaker(deploy_db, mocker):
    """An OPEN circuit breaker (failure_count > 0) must be persisted to DeploymentState."""
    session, _Session = deploy_db
    opened_at = _naive_utcnow()

    saved_breakers = _shared_state.circuit_breakers.copy()
    _shared_state.circuit_breakers.clear()
    _shared_state.circuit_breakers["proj-uuid-1"] = {
        "state": "OPEN",
        "failure_count": 5,
        "success_count": 0,
        "opened_at": opened_at,
        "cooldown_seconds": 60,
        "base_cooldown": 30,
        "half_open_task_id": None,
    }

    mock_get_state = AsyncMock()
    mock_state = MagicMock()
    mock_state.circuit_breaker_states = None
    mock_get_state.return_value = mock_state
    mocker.patch("app.deploy_health._get_deploy_state_for_update", mock_get_state)

    try:
        await _persist_circuit_breakers()
    finally:
        _shared_state.circuit_breakers.clear()
        _shared_state.circuit_breakers.update(saved_breakers)

    mock_get_state.assert_called_once()
    persisted = mock_state.circuit_breaker_states
    assert "proj-uuid-1" in persisted
    entry = persisted["proj-uuid-1"]
    assert entry["state"] == "OPEN"
    assert entry["failure_count"] == 5
    # opened_at must be serialized to ISO string
    assert entry["opened_at"] == opened_at.isoformat()


@pytest.mark.asyncio
async def test_persist_circuit_breakers_prunes_closed_with_no_failures(deploy_db, mocker):
    """CLOSED breakers with failure_count=0 are default state and must NOT be persisted."""
    session, _Session = deploy_db

    saved_breakers = _shared_state.circuit_breakers.copy()
    _shared_state.circuit_breakers.clear()
    _shared_state.circuit_breakers["proj-clean"] = {
        "state": "CLOSED",
        "failure_count": 0,
        "success_count": 0,
        "opened_at": None,
        "cooldown_seconds": 30,
        "base_cooldown": 30,
        "half_open_task_id": None,
    }

    mock_state = MagicMock()
    mock_state.circuit_breaker_states = None
    mocker.patch("app.deploy_health._get_deploy_state_for_update", AsyncMock(return_value=mock_state))

    try:
        await _persist_circuit_breakers()
    finally:
        _shared_state.circuit_breakers.clear()
        _shared_state.circuit_breakers.update(saved_breakers)

    persisted = mock_state.circuit_breaker_states
    assert "proj-clean" not in persisted


@pytest.mark.asyncio
async def test_persist_circuit_breakers_handles_missing_deploy_state(deploy_db, mocker):
    """If _get_deploy_state_for_update returns None, the function must not crash."""
    mocker.patch("app.deploy_health._get_deploy_state_for_update", AsyncMock(return_value=None))

    saved_breakers = _shared_state.circuit_breakers.copy()
    _shared_state.circuit_breakers.clear()
    _shared_state.circuit_breakers["some-proj"] = {
        "state": "OPEN",
        "failure_count": 1,
        "success_count": 0,
        "opened_at": _naive_utcnow(),
        "cooldown_seconds": 30,
        "base_cooldown": 30,
        "half_open_task_id": None,
    }

    try:
        await _persist_circuit_breakers()
    finally:
        _shared_state.circuit_breakers.clear()
        _shared_state.circuit_breakers.update(saved_breakers)
    # No exception == pass


# ══════════════════════════════════════════════════════════════════════════════
# _persist_zone_health
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_persist_zone_health_serializes_datetimes(deploy_db, mocker):
    """Zone health datetime fields (e.g. cooldown_expires_at) must be ISO-stringified."""
    session, _Session = deploy_db

    expires_at = _naive_utcnow() + timedelta(minutes=30)
    fake_zone_health = {
        "us-central1-f": {
            "consecutive_failures": 3,
            "cooldown_expires_at": expires_at,
        }
    }

    mocker.patch("app.compute._zone_health", fake_zone_health)
    # Provide a real non-async lock
    import threading
    mocker.patch("app.compute._zone_lock", threading.Lock())

    mocker.patch("app.deploy_health._pause_failure_streak", 2)
    mocker.patch("app.deploy_health._health_recovery_streak", 1)

    mock_state = MagicMock()
    mock_state.zone_health_state = None
    mocker.patch("app.deploy_health._get_deploy_state_for_update", AsyncMock(return_value=mock_state))

    await _persist_zone_health()

    snapshot = mock_state.zone_health_state
    assert "us-central1-f" in snapshot
    zone = snapshot["us-central1-f"]
    assert zone["consecutive_failures"] == 3
    # datetime must have been converted to ISO string
    assert isinstance(zone["cooldown_expires_at"], str)
    assert zone["cooldown_expires_at"] == expires_at.isoformat()


@pytest.mark.asyncio
async def test_persist_zone_health_includes_streak_counters(deploy_db, mocker):
    """Snapshot must include _pause_failure_streak and _health_recovery_streak."""
    mocker.patch("app.compute._zone_health", {})
    import threading
    mocker.patch("app.compute._zone_lock", threading.Lock())
    mocker.patch("app.deploy_health._pause_failure_streak", 4)
    mocker.patch("app.deploy_health._health_recovery_streak", 7)

    mock_state = MagicMock()
    mock_state.zone_health_state = None
    mocker.patch("app.deploy_health._get_deploy_state_for_update", AsyncMock(return_value=mock_state))

    await _persist_zone_health()

    snapshot = mock_state.zone_health_state
    assert snapshot["_pause_failure_streak"] == 4
    assert snapshot["_health_recovery_streak"] == 7


@pytest.mark.asyncio
async def test_persist_zone_health_handles_missing_deploy_state(mocker):
    """If _get_deploy_state_for_update returns None, the function must not crash."""
    mocker.patch("app.compute._zone_health", {})
    import threading
    mocker.patch("app.compute._zone_lock", threading.Lock())
    mocker.patch("app.deploy_health._pause_failure_streak", 0)
    mocker.patch("app.deploy_health._health_recovery_streak", 0)
    mocker.patch("app.deploy_health._get_deploy_state_for_update", AsyncMock(return_value=None))
    mocker.patch("app.services.AsyncSessionLocal", MagicMock())

    await _persist_zone_health()
    # No exception == pass
