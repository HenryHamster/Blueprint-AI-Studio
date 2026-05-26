"""
Tests for recovery functions in backend/app/services.py.

Covers:
  1. _recover_orphaned_tasks — COMPLETED tasks with no PR artifact / no review child
  2. _persist_circuit_breakers — serialize in-memory breaker state to DeploymentState
  3. _persist_zone_health — serialize zone health snapshot to DeploymentState
  4. gcp_cleanup_service — orphaned VM detection and deletion
  5. _check_daily_cost_ceiling — daily compute budget enforcement (in agent_status.py)

Uses isolated in-memory SQLite DB (same pattern as test_pipeline_reliability.py).
"""

import asyncio
import uuid as _uuid
import pytest
import pytest_asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, select

import app.shared_state as _shared_state
from app.models import (
    Project,
    Task,
    TaskStatus,
    Artifact,
    ArtifactType,
    Alert,
    AlertSeverity,
    DeploymentState,
)
from app.routers.agent_status import _check_daily_cost_ceiling
from app.services import (
    _derive_task_branch,
    _recover_orphaned_tasks,
    _run_auto_archive_cycle,
    _sweep_post_commit_pending,
    gcp_cleanup_service,
)
from app.state_persistence import (
    _persist_circuit_breakers,
    _persist_zone_health,
)
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler as _SQLiteTypeCompiler

# ── SQLite JSONB compatibility ────────────────────────────────────────────────

if not hasattr(_SQLiteTypeCompiler, "visit_JSONB"):
    _SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: self.visit_JSON(
        type_, **kw
    )


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
        bind=_engine,
        class_=AsyncSession,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
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


async def _make_completed_task(
    session, project_id, task_type="create", minutes_ago=30
) -> Task:
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
    await _make_completed_task(svc_db, project.id, task_type="create")

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
async def test_orphan_recovery_creates_artifact_and_review_when_pr_found(
    svc_db, mocker
):
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
    artifact = (
        (
            await svc_db.execute(
                select(Artifact).where(
                    Artifact.task_id == task.id,
                    Artifact.artifact_type == ArtifactType.PULL_REQUEST,
                )
            )
        )
        .scalars()
        .first()
    )
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


@pytest.mark.asyncio
async def test_orphan_recovery_uses_project_namespace(svc_db, mocker):
    """review-respawn derives branches under the project's task_branch_namespace, not hardcoded 'blueprint'."""
    project = Project(
        name="AutoProj",
        user_id="user1",
        build_config={"task_branch_namespace": "autoproj"},
    )
    svc_db.add(project)
    await svc_db.commit()
    await svc_db.refresh(project)

    await _make_completed_task(svc_db, project.id, task_type="create")

    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("gh_token", "owner", "repo"),
    )

    pr_url = "https://github.com/owner/repo/pull/42"
    mock_gh_resp = MagicMock()
    mock_gh_resp.status_code = 200
    mock_gh_resp.json.return_value = [{"html_url": pr_url, "number": 42}]
    mocker.patch("app.github._github_request", AsyncMock(return_value=mock_gh_resp))

    mock_review_task = Task(
        project_id=project.id,
        payloads=[{"type": "review"}],
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
    )
    mock_spawn = mocker.patch(
        "app.review_pipeline._spawn_pipeline_task",
        new_callable=AsyncMock,
        return_value=(mock_review_task, True),
    )

    await _recover_orphaned_tasks()

    mock_spawn.assert_called_once()
    _spawned_branch = mock_spawn.call_args[0][4]
    assert _spawned_branch.startswith("autoproj/"), (
        f"expected autoproj/* branch, got {_spawned_branch!r}"
    )


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
        bind=_engine,
        class_=AsyncSession,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
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
    _session, _Session = deploy_db
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
async def test_persist_circuit_breakers_prunes_closed_with_no_failures(
    deploy_db, mocker
):
    """CLOSED breakers with failure_count=0 are default state and must NOT be persisted."""
    _session, _Session = deploy_db

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
    mocker.patch(
        "app.deploy_health._get_deploy_state_for_update",
        AsyncMock(return_value=mock_state),
    )

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
    mocker.patch(
        "app.deploy_health._get_deploy_state_for_update", AsyncMock(return_value=None)
    )

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
    _session, _Session = deploy_db

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
    from collections import deque as _deque

    mocker.patch("app.deploy_health._health_recovery_history", _deque([True], maxlen=5))

    mock_state = MagicMock()
    mock_state.zone_health_state = None
    mocker.patch(
        "app.deploy_health._get_deploy_state_for_update",
        AsyncMock(return_value=mock_state),
    )

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
    """Snapshot must include _pause_failure_streak and _health_recovery_history."""
    mocker.patch("app.compute._zone_health", {})
    import threading

    mocker.patch("app.compute._zone_lock", threading.Lock())
    mocker.patch("app.deploy_health._pause_failure_streak", 4)
    from collections import deque as _deque

    mocker.patch(
        "app.deploy_health._health_recovery_history",
        _deque([True, False, True], maxlen=5),
    )
    from datetime import datetime as _dt

    resume_at = _dt(2026, 4, 17, 12, 0, 0)
    mocker.patch("app.deploy_health._last_auto_resume_at", resume_at)

    mock_state = MagicMock()
    mock_state.zone_health_state = None
    mocker.patch(
        "app.deploy_health._get_deploy_state_for_update",
        AsyncMock(return_value=mock_state),
    )

    await _persist_zone_health()

    snapshot = mock_state.zone_health_state
    assert snapshot["_pause_failure_streak"] == 4
    assert snapshot["_health_recovery_history"] == [True, False, True]
    assert snapshot["_last_auto_resume_at"] == resume_at.isoformat()


@pytest.mark.asyncio
async def test_persist_zone_health_handles_missing_deploy_state(mocker):
    """If _get_deploy_state_for_update returns None, the function must not crash."""
    mocker.patch("app.compute._zone_health", {})
    import threading

    mocker.patch("app.compute._zone_lock", threading.Lock())
    mocker.patch("app.deploy_health._pause_failure_streak", 0)
    from collections import deque as _deque

    mocker.patch("app.deploy_health._health_recovery_history", _deque(maxlen=5))
    mocker.patch(
        "app.deploy_health._get_deploy_state_for_update", AsyncMock(return_value=None)
    )
    mocker.patch("app.services.AsyncSessionLocal", MagicMock())

    await _persist_zone_health()
    # No exception == pass


# ══════════════════════════════════════════════════════════════════════════════
# gcp_cleanup_service
# ══════════════════════════════════════════════════════════════════════════════


@pytest_asyncio.fixture()
async def gcp_db(mocker):
    """Isolated in-memory SQLite DB for gcp_cleanup_service tests."""
    _engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with _engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    _Session = sessionmaker(
        bind=_engine,
        class_=AsyncSession,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
    )
    mocker.patch("app.services.AsyncSessionLocal", _Session)

    async with _Session() as session:
        yield session

    async with _engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
    await _engine.dispose()


async def _make_task_with_vm(session, project_id, status, vm_instance_id=None) -> Task:
    task = Task(
        project_id=project_id,
        payloads=[{"type": "create", "prompt": "test"}],
        status=status,
        created_by="ORCHESTRATOR",
        vm_instance_id=vm_instance_id,
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task


@pytest.mark.asyncio
async def test_gcp_cleanup_passes_active_vm_ids_to_cleanup(gcp_db, mocker):
    """Active (PROVISIONING/IN_PROGRESS) VM IDs should be passed to cleanup_orphaned_vms.
    Completed/failed task VMs should NOT be included."""
    project = await _make_project(gcp_db)

    await _make_task_with_vm(
        gcp_db, project.id, TaskStatus.PROVISIONING, "agent-vm-prov-1"
    )
    await _make_task_with_vm(
        gcp_db, project.id, TaskStatus.IN_PROGRESS, "agent-vm-ip-1"
    )
    # These should NOT be included
    await _make_task_with_vm(
        gcp_db, project.id, TaskStatus.COMPLETED, "agent-vm-done-1"
    )
    await _make_task_with_vm(gcp_db, project.id, TaskStatus.FAILED, "agent-vm-fail-1")
    # Task with no vm_instance_id
    await _make_task_with_vm(gcp_db, project.id, TaskStatus.IN_PROGRESS, None)

    mock_cleanup = AsyncMock(
        return_value=MagicMock(deleted=[], failed=[], total_gcp_vms=2)
    )
    mocker.patch("app.compute.cleanup_orphaned_vms", mock_cleanup)

    # Exit after first cleanup iteration by raising CancelledError on sleep
    mocker.patch(
        "app.services.asyncio.sleep", AsyncMock(side_effect=asyncio.CancelledError)
    )

    await gcp_cleanup_service()

    mock_cleanup.assert_called_once()
    called_vm_ids = mock_cleanup.call_args[0][0]
    assert "agent-vm-prov-1" in called_vm_ids
    assert "agent-vm-ip-1" in called_vm_ids
    assert "agent-vm-done-1" not in called_vm_ids
    assert "agent-vm-fail-1" not in called_vm_ids
    assert None not in called_vm_ids


@pytest.mark.asyncio
async def test_gcp_cleanup_handles_api_failure_gracefully(gcp_db, mocker):
    """If cleanup_orphaned_vms raises an exception, gcp_cleanup_service should log and continue."""
    project = await _make_project(gcp_db)
    await _make_task_with_vm(gcp_db, project.id, TaskStatus.IN_PROGRESS, "agent-vm-1")

    mocker.patch(
        "app.compute.cleanup_orphaned_vms", side_effect=RuntimeError("GCP API down")
    )
    mocker.patch(
        "app.services.asyncio.sleep", AsyncMock(side_effect=asyncio.CancelledError)
    )

    # Should not raise
    await gcp_cleanup_service()


@pytest.mark.asyncio
async def test_gcp_cleanup_handles_timeout_gracefully(gcp_db, mocker):
    """If cleanup_orphaned_vms times out, the service should log and continue without crashing."""
    project = await _make_project(gcp_db)
    await _make_task_with_vm(gcp_db, project.id, TaskStatus.IN_PROGRESS, "agent-vm-1")

    async def _slow_cleanup(_ids):
        raise asyncio.TimeoutError()

    mocker.patch("app.compute.cleanup_orphaned_vms", side_effect=_slow_cleanup)
    mocker.patch(
        "app.services.asyncio.sleep", AsyncMock(side_effect=asyncio.CancelledError)
    )

    # Should not raise
    await gcp_cleanup_service()


@pytest.mark.asyncio
async def test_gcp_cleanup_updates_vm_count_cache(gcp_db, mocker):
    """When cleanup returns total_gcp_vms, the global VM count cache should be updated."""
    project = await _make_project(gcp_db)
    await _make_task_with_vm(gcp_db, project.id, TaskStatus.IN_PROGRESS, "agent-vm-1")

    mock_result = MagicMock()
    mock_result.deleted = ["agent-vm-orphan"]
    mock_result.failed = []
    mock_result.total_gcp_vms = 5
    mocker.patch(
        "app.compute.cleanup_orphaned_vms", AsyncMock(return_value=mock_result)
    )
    mocker.patch(
        "app.services.asyncio.sleep", AsyncMock(side_effect=asyncio.CancelledError)
    )

    import app.main as _app_main

    await gcp_cleanup_service()

    assert _app_main._last_gcp_vm_count == 5
    assert hasattr(_app_main, "_last_gcp_vm_check")


@pytest.mark.asyncio
async def test_gcp_cleanup_logs_partial_failures(gcp_db, mocker):
    """If cleanup_result.failed is non-empty, the service should still run without crashing."""
    project = await _make_project(gcp_db)
    await _make_task_with_vm(gcp_db, project.id, TaskStatus.IN_PROGRESS, "agent-vm-1")

    mock_result = MagicMock()
    mock_result.deleted = []
    mock_result.failed = ["agent-vm-bad-1", "agent-vm-bad-2"]
    mock_result.total_gcp_vms = 3
    mocker.patch(
        "app.compute.cleanup_orphaned_vms", AsyncMock(return_value=mock_result)
    )
    mocker.patch(
        "app.services.asyncio.sleep", AsyncMock(side_effect=asyncio.CancelledError)
    )

    # Should not raise despite partial failures
    await gcp_cleanup_service()


# ══════════════════════════════════════════════════════════════════════════════
# _check_daily_cost_ceiling (agent_status.py)
# ══════════════════════════════════════════════════════════════════════════════


@pytest_asyncio.fixture()
async def cost_db(mocker):
    """Isolated in-memory SQLite DB for _check_daily_cost_ceiling tests."""
    _engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with _engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    _Session = sessionmaker(
        bind=_engine,
        class_=AsyncSession,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
    )

    # Suppress broadcasts
    mocker.patch(
        "app.routers.agent_status.broadcast_to_project", new_callable=AsyncMock
    )
    # Suppress external notification dispatch (patching where defined so ensure_future gets a real coroutine)
    mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )

    async with _Session() as session:
        yield session

    async with _engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
    await _engine.dispose()


def _make_cost_task(
    project_id, vm_secs=3600, is_spot=False, stopped_today=True
) -> Task:
    """Build a completed Task with cost data (not yet committed)."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    stop_at = now if stopped_today else today_start - timedelta(seconds=1)
    prov_at = stop_at - timedelta(seconds=vm_secs)
    return Task(
        project_id=project_id,
        payloads=[{"type": "create", "prompt": "test"}],
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        vm_provisioned_at=prov_at,
        vm_stopped_at=stop_at,
        vm_spot=is_spot,
    )


@pytest.mark.asyncio
async def test_cost_ceiling_no_alert_when_disabled(cost_db, mocker):
    """When daily_cost_ceiling_usd == 0 (disabled), no alert should be created."""
    project = await _make_project(cost_db)

    task = _make_cost_task(project.id, vm_secs=36000)  # 10 hours, very expensive
    cost_db.add(task)
    await cost_db.commit()
    await cost_db.refresh(task)

    with patch.dict(_shared_state.runtime_config, {"daily_cost_ceiling_usd": 0}):
        await _check_daily_cost_ceiling(task, cost_db)

    alert_count = (
        (await cost_db.execute(select(Alert).where(Alert.project_id == project.id)))
        .scalars()
        .all()
    )
    assert len(alert_count) == 0


@pytest.mark.asyncio
async def test_cost_ceiling_no_alert_when_under_ceiling(cost_db, mocker):
    """When cost < ceiling, no alert should be created."""
    project = await _make_project(cost_db)

    # 1 hour on-demand: cost = 3600 * ON_DEMAND_RATE ≈ $0.067
    task = _make_cost_task(project.id, vm_secs=3600, is_spot=False)
    cost_db.add(task)
    await cost_db.commit()
    await cost_db.refresh(task)

    # Set ceiling above actual cost ($1 >> $0.067)
    with patch.dict(_shared_state.runtime_config, {"daily_cost_ceiling_usd": 1.0}):
        await _check_daily_cost_ceiling(task, cost_db)

    alerts = (
        (await cost_db.execute(select(Alert).where(Alert.project_id == project.id)))
        .scalars()
        .all()
    )
    assert len(alerts) == 0


@pytest.mark.asyncio
async def test_cost_ceiling_alert_created_when_over_ceiling(cost_db, mocker):
    """When total daily cost > ceiling, a WARNING alert should be created."""
    project = await _make_project(cost_db)

    # 1 hour on-demand: cost ≈ $0.067
    task = _make_cost_task(project.id, vm_secs=3600, is_spot=False)
    cost_db.add(task)
    await cost_db.commit()
    await cost_db.refresh(task)

    # Set ceiling below actual cost ($0.01 < $0.067)
    with patch.dict(_shared_state.runtime_config, {"daily_cost_ceiling_usd": 0.01}):
        await _check_daily_cost_ceiling(task, cost_db)

    # Function adds alert with session.add() but doesn't commit; flush to make it visible.
    await cost_db.flush()
    alerts = (
        (await cost_db.execute(select(Alert).where(Alert.project_id == project.id)))
        .scalars()
        .all()
    )
    assert len(alerts) == 1
    assert alerts[0].severity == AlertSeverity.WARNING
    assert alerts[0].title == "Daily cost ceiling exceeded"
    # task_id must be NULL (project-level alert, not task-specific)
    assert alerts[0].task_id is None


@pytest.mark.asyncio
async def test_cost_ceiling_deduplicates_alert(cost_db, mocker):
    """If an undismissed cost ceiling alert already exists, no second alert should be created."""
    project = await _make_project(cost_db)

    # Pre-existing undismissed alert
    existing_alert = Alert(
        project_id=project.id,
        severity=AlertSeverity.WARNING,
        title="Daily cost ceiling exceeded",
        message="Pre-existing alert.",
    )
    cost_db.add(existing_alert)
    await cost_db.commit()

    # Task that pushes cost over ceiling
    task = _make_cost_task(project.id, vm_secs=3600, is_spot=False)
    cost_db.add(task)
    await cost_db.commit()
    await cost_db.refresh(task)

    with patch.dict(_shared_state.runtime_config, {"daily_cost_ceiling_usd": 0.01}):
        await _check_daily_cost_ceiling(task, cost_db)

    alerts = (
        (await cost_db.execute(select(Alert).where(Alert.project_id == project.id)))
        .scalars()
        .all()
    )
    # Still only the one pre-existing alert, no duplicate
    assert len(alerts) == 1


@pytest.mark.asyncio
async def test_cost_ceiling_excludes_other_day_tasks(cost_db, mocker):
    """Tasks whose vm_stopped_at is before today should not count toward the daily total."""
    project = await _make_project(cost_db)

    # Yesterday's task — very expensive but outside today's window
    yesterday_task = _make_cost_task(project.id, vm_secs=86400, stopped_today=False)
    cost_db.add(yesterday_task)
    await cost_db.commit()

    # Today's task — cheap (10 min on-demand)
    today_task = _make_cost_task(project.id, vm_secs=600, is_spot=False)
    cost_db.add(today_task)
    await cost_db.commit()
    await cost_db.refresh(today_task)

    # Ceiling is $0.05 — above today-only cost ($0.011) but below yesterday+today ($1.67)
    with patch.dict(_shared_state.runtime_config, {"daily_cost_ceiling_usd": 0.05}):
        await _check_daily_cost_ceiling(today_task, cost_db)

    alerts = (
        (await cost_db.execute(select(Alert).where(Alert.project_id == project.id)))
        .scalars()
        .all()
    )
    assert len(alerts) == 0


@pytest.mark.asyncio
async def test_cost_ceiling_spot_rate_used_for_spot_vms(cost_db, mocker):
    """Spot VMs should be costed at SPOT_RATE (cheaper than ON_DEMAND_RATE)."""
    project = await _make_project(cost_db)

    # 1 hour spot: cost = 3600 * SPOT_RATE ≈ $0.020
    spot_task = _make_cost_task(project.id, vm_secs=3600, is_spot=True)
    cost_db.add(spot_task)
    await cost_db.commit()
    await cost_db.refresh(spot_task)

    # Ceiling between spot and on-demand cost ($0.04 > spot $0.020, < on-demand $0.067)
    # If on-demand rate were used, alert would fire. It should NOT fire here.
    with patch.dict(_shared_state.runtime_config, {"daily_cost_ceiling_usd": 0.04}):
        await _check_daily_cost_ceiling(spot_task, cost_db)

    alerts = (
        (await cost_db.execute(select(Alert).where(Alert.project_id == project.id)))
        .scalars()
        .all()
    )
    assert len(alerts) == 0


@pytest.mark.asyncio
async def test_cost_ceiling_includes_current_task_cost(cost_db, mocker):
    """The in-memory current task's own cost must be included even if not yet in DB."""
    project = await _make_project(cost_db)

    # Create a task but do NOT commit it to DB — simulates in-memory state
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "test"}],
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        # In-memory cost data only (not yet in DB)
        vm_provisioned_at=now - timedelta(hours=1),
        vm_stopped_at=now,
        vm_spot=False,
    )
    cost_db.add(task)
    # Flush to get an ID but keep out of the query (the function excludes Task.id != task.id)
    await cost_db.flush()

    # Ceiling below 1-hour on-demand cost
    with patch.dict(_shared_state.runtime_config, {"daily_cost_ceiling_usd": 0.01}):
        await _check_daily_cost_ceiling(task, cost_db)

    # Function adds alert with session.add() but doesn't commit; flush to make it visible.
    await cost_db.flush()
    alerts = (
        (await cost_db.execute(select(Alert).where(Alert.project_id == project.id)))
        .scalars()
        .all()
    )
    assert len(alerts) == 1
    assert "Daily cost ceiling exceeded" in alerts[0].title


@pytest.mark.asyncio
async def test_cost_ceiling_budget_exceeded_webhook_enriched(cost_db, mocker):
    """BUDGET_EXCEEDED webhook payload includes direction_text and cost_by_failure_class."""
    project = await _make_project(cost_db)

    dispatch_mock = AsyncMock()
    mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        dispatch_mock,
    )

    task = _make_cost_task(project.id, vm_secs=3600, is_spot=False)
    task.context = {
        "direction_text": "Improve pipeline throughput",
        "direction_id": "d-x",
    }
    cost_db.add(task)
    await cost_db.commit()
    await cost_db.refresh(task)

    with patch.dict(_shared_state.runtime_config, {"daily_cost_ceiling_usd": 0.01}):
        await _check_daily_cost_ceiling(task, cost_db)

    for _ in range(5):
        await asyncio.sleep(0)
    assert dispatch_mock.call_count >= 1
    args, _kwargs = dispatch_mock.call_args
    assert args[1] == "BUDGET_EXCEEDED"
    payload = args[2]
    assert payload["direction_text"] == "Improve pipeline throughput"
    assert payload["ceiling_usd"] == 0.01
    assert "daily_total_usd" in payload
    assert isinstance(payload["cost_by_failure_class"], dict)
    # Pin the bucket total: 1h on-demand ≈ $0.067. If the DB query included the
    # current task (identity map double-count), this bucket would be ~$0.134.
    assert "COMPLETED" in payload["cost_by_failure_class"]
    completed_cost = payload["cost_by_failure_class"]["COMPLETED"]
    assert 0.06 < completed_cost < 0.08, (
        f"expected ~$0.067 single-count, got {completed_cost} (possible double-count)"
    )
    # Internal consistency: sum of buckets should equal daily_total_usd.
    assert (
        abs(sum(payload["cost_by_failure_class"].values()) - payload["daily_total_usd"])
        < 0.01
    )


# ══════════════════════════════════════════════════════════════════════════════
# _run_auto_archive_cycle — age cap + per-project count cap
# ══════════════════════════════════════════════════════════════════════════════


async def _make_terminal_task(
    session, project_id, status=TaskStatus.COMPLETED, days_ago=0
) -> Task:
    """Create a terminal task with updated_at set days_ago days in the past."""
    when = _naive_utc_ago(days=days_ago)
    task = Task(
        project_id=project_id,
        payloads=[{"type": "create"}],
        status=status,
        created_by="ORCHESTRATOR",
        created_at=when,
        updated_at=when,
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task


@pytest.mark.asyncio
async def test_auto_archive_age_pass_archives_old_terminal(svc_db):
    """Terminal tasks older than auto_archive_terminal_after_days get archived."""
    project = await _make_project(svc_db)
    old = await _make_terminal_task(svc_db, project.id, days_ago=30)
    fresh = await _make_terminal_task(svc_db, project.id, days_ago=1)

    with patch.dict(
        _shared_state.runtime_config,
        {
            "auto_archive_terminal_after_days": 7,
            "auto_archive_max_terminal_tasks_per_project": 0,
        },
    ):
        result = await _run_auto_archive_cycle()

    assert result["age"] == 1
    assert result["count"] == 0
    await svc_db.refresh(old)
    await svc_db.refresh(fresh)
    assert old.archived is True
    assert fresh.archived is False


@pytest.mark.asyncio
async def test_auto_archive_count_pass_archives_beyond_cap(svc_db):
    """Per-project cap keeps the N most-recent terminal tasks; rest archive."""
    project = await _make_project(svc_db)
    # days_ago=0 is newest; days_ago=4 is oldest
    tasks = [
        await _make_terminal_task(svc_db, project.id, days_ago=i) for i in range(5)
    ]

    with patch.dict(
        _shared_state.runtime_config,
        {
            "auto_archive_terminal_after_days": 90,
            "auto_archive_max_terminal_tasks_per_project": 3,
        },
    ):
        result = await _run_auto_archive_cycle()

    assert result["age"] == 0
    assert result["count"] == 2
    for t in tasks:
        await svc_db.refresh(t)
    # 3 newest (index 0,1,2 = days_ago 0,1,2) stay; 2 oldest (3,4) archived.
    # Don't re-derive age from updated_at — the UPDATE's onupdate=_utcnow hook
    # resets updated_at on archived rows, so we check by creation order.
    assert [t.archived for t in tasks] == [False, False, False, True, True]


@pytest.mark.asyncio
async def test_auto_archive_count_pass_partitions_by_project(svc_db):
    """Cap is applied per-project independently (projects don't cross-count)."""
    p1 = await _make_project(svc_db)
    p2 = Project(name="P2", user_id="user1")
    svc_db.add(p2)
    await svc_db.commit()
    await svc_db.refresh(p2)

    for i in range(4):
        await _make_terminal_task(svc_db, p1.id, days_ago=i)
    await _make_terminal_task(svc_db, p2.id, days_ago=0)

    with patch.dict(
        _shared_state.runtime_config,
        {
            "auto_archive_terminal_after_days": 90,
            "auto_archive_max_terminal_tasks_per_project": 2,
        },
    ):
        result = await _run_auto_archive_cycle()

    assert result["count"] == 2
    p1_archived = (
        (
            await svc_db.execute(
                select(Task).where(Task.project_id == p1.id, Task.archived.is_(True))
            )
        )
        .scalars()
        .all()
    )
    p2_archived = (
        (
            await svc_db.execute(
                select(Task).where(Task.project_id == p2.id, Task.archived.is_(True))
            )
        )
        .scalars()
        .all()
    )
    assert len(p1_archived) == 2
    assert len(p2_archived) == 0


@pytest.mark.asyncio
async def test_auto_archive_count_pass_disabled_when_cap_zero(svc_db):
    """cap=0 disables the count pass entirely."""
    project = await _make_project(svc_db)
    for i in range(10):
        await _make_terminal_task(svc_db, project.id, days_ago=i)

    with patch.dict(
        _shared_state.runtime_config,
        {
            "auto_archive_terminal_after_days": 90,
            "auto_archive_max_terminal_tasks_per_project": 0,
        },
    ):
        result = await _run_auto_archive_cycle()

    assert result["age"] == 0
    assert result["count"] == 0
    archived = (
        (await svc_db.execute(select(Task).where(Task.archived.is_(True))))
        .scalars()
        .all()
    )
    assert len(archived) == 0


@pytest.mark.asyncio
async def test_auto_archive_skips_non_terminal(svc_db):
    """Non-terminal tasks (QUEUED, IN_PROGRESS) are never archived, regardless of age."""
    project = await _make_project(svc_db)
    queued = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        status=TaskStatus.QUEUED,
        created_by="USER",
        created_at=_naive_utc_ago(days=60),
        updated_at=_naive_utc_ago(days=60),
    )
    svc_db.add(queued)
    await svc_db.commit()
    await svc_db.refresh(queued)

    with patch.dict(
        _shared_state.runtime_config,
        {
            "auto_archive_terminal_after_days": 7,
            "auto_archive_max_terminal_tasks_per_project": 1,
        },
    ):
        await _run_auto_archive_cycle()

    await svc_db.refresh(queued)
    assert queued.archived is False


# ─────────────────────────────────────────────────────────────────────────────
# cleanup_old_images — image family pruner
# ─────────────────────────────────────────────────────────────────────────────


def _mk_image(name: str, ts: str):
    """Lightweight stand-in for google.cloud.compute_v1.Image — just needs .name/.creation_timestamp."""
    im = MagicMock()
    im.name = name
    im.creation_timestamp = ts
    return im


@pytest.mark.asyncio
async def test_cleanup_old_images_deletes_oldest_beyond_keep(mocker):
    """cleanup_old_images should delete only images past the keep-N boundary, newest-first sort."""
    from app import compute as _compute

    images = [
        _mk_image("img-2026-04-15", "2026-04-15T10:00:00.000-00:00"),
        _mk_image("img-2026-04-10", "2026-04-10T10:00:00.000-00:00"),
        _mk_image("img-2026-04-05", "2026-04-05T10:00:00.000-00:00"),
        _mk_image("img-2026-04-01", "2026-04-01T10:00:00.000-00:00"),
        _mk_image("img-2026-03-25", "2026-03-25T10:00:00.000-00:00"),
    ]
    mock_client = MagicMock()
    mock_client.list.return_value = iter(images)
    mock_client.delete = MagicMock(return_value=None)
    mocker.patch("google.cloud.compute_v1.ImagesClient", return_value=mock_client)
    mocker.patch.object(_compute, "LOCAL_MODE", False)

    result = await _compute.cleanup_old_images(
        keep=2, family="blueprint-agent", project="my-proj"
    )

    assert result["total"] == 5
    assert result["kept"] == 2
    assert result["deleted"] == 3
    assert result["failed"] == []
    deleted_names = sorted(
        [c.kwargs["image"] for c in mock_client.delete.call_args_list]
    )
    assert deleted_names == ["img-2026-03-25", "img-2026-04-01", "img-2026-04-05"]


@pytest.mark.asyncio
async def test_cleanup_old_images_noop_when_at_or_below_keep(mocker):
    """If total images <= keep, no delete calls should fire."""
    from app import compute as _compute

    mock_client = MagicMock()
    mock_client.list.return_value = iter(
        [
            _mk_image("img-1", "2026-04-15T10:00:00.000-00:00"),
            _mk_image("img-2", "2026-04-10T10:00:00.000-00:00"),
        ]
    )
    mock_client.delete = MagicMock()
    mocker.patch("google.cloud.compute_v1.ImagesClient", return_value=mock_client)
    mocker.patch.object(_compute, "LOCAL_MODE", False)

    result = await _compute.cleanup_old_images(
        keep=5, family="blueprint-agent", project="my-proj"
    )

    assert result["deleted"] == 0
    assert result["kept"] == 2
    mock_client.delete.assert_not_called()


@pytest.mark.asyncio
async def test_cleanup_old_images_dry_run_does_not_delete(mocker):
    """dry_run=True must return the planned list but never call delete."""
    from app import compute as _compute

    images = [
        _mk_image(f"img-{i}", f"2026-04-{i:02d}T10:00:00.000-00:00")
        for i in range(1, 6)
    ]
    mock_client = MagicMock()
    mock_client.list.return_value = iter(images)
    mock_client.delete = MagicMock()
    mocker.patch("google.cloud.compute_v1.ImagesClient", return_value=mock_client)
    mocker.patch.object(_compute, "LOCAL_MODE", False)

    result = await _compute.cleanup_old_images(
        keep=2, family="blueprint-agent", project="my-proj", dry_run=True
    )

    assert result["dry_run"] is True
    assert result["deleted"] == 0
    assert len(result["would_delete"]) == 3
    mock_client.delete.assert_not_called()


@pytest.mark.asyncio
async def test_cleanup_old_images_rejects_keep_less_than_one():
    """keep < 1 must raise — we must retain at least one image for VM provisioning."""
    from app import compute as _compute

    with pytest.raises(ValueError):
        await _compute.cleanup_old_images(
            keep=0, family="blueprint-agent", project="my-proj"
        )


@pytest.mark.asyncio
async def test_cleanup_old_images_collects_per_image_failures(mocker):
    """A delete failure on one image must not abort the sweep; failures are aggregated."""
    from app import compute as _compute

    images = [
        _mk_image(f"img-{i}", f"2026-04-{i:02d}T10:00:00.000-00:00")
        for i in range(1, 6)
    ]
    mock_client = MagicMock()
    mock_client.list.return_value = iter(images)

    def _delete_side_effect(**kwargs):
        if kwargs["image"] == "img-2":  # one of the images we expect to delete
            raise RuntimeError("permissionDenied")
        return None

    mock_client.delete = MagicMock(side_effect=_delete_side_effect)
    mocker.patch("google.cloud.compute_v1.ImagesClient", return_value=mock_client)
    mocker.patch.object(_compute, "LOCAL_MODE", False)

    result = await _compute.cleanup_old_images(
        keep=2, family="blueprint-agent", project="my-proj"
    )

    assert result["deleted"] == 2
    assert len(result["failed"]) == 1
    assert result["failed"][0]["name"] == "img-2"
    assert "permissionDenied" in result["failed"][0]["error"]


# ══════════════════════════════════════════════════════════════════════════════
# _sweep_post_commit_pending — admin-cancel must be sticky (regression 2026-05-04)
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_post_commit_sweep_skips_cancelled_tasks_and_clears_pending(
    svc_db, mocker
):
    """Regression for incident 2026-05-04: an admin-cancelled plan task
    (b9456e05) was zombied 2 hours later when the post-commit sweep retried
    `_handle_plan_completion`. The handler unconditionally set status back to
    EVALUATING and spawned a child (b403b76a), silently overriding the
    admin's cancel decision.

    The sweep must NOT call `_process_post_commit` on CANCELLED tasks.
    Instead it should clear `post_commit_pending` so the row stops appearing
    in future sweeps."""
    project = await _make_project(svc_db)
    cancelled_task = Task(
        project_id=project.id,
        payloads=[{"type": "plan", "prompt": "decompose"}],
        status=TaskStatus.CANCELLED,
        created_by="ORCHESTRATOR",
        post_commit_pending=True,
        context={
            "_post_commit_pending_meta": {
                "at": _naive_utcnow().isoformat(),
                "reason": "post_commit_error",
                "payload": {
                    "task_id": "irrelevant",
                    "status": "COMPLETED",
                    "logs": "{}",
                },
                "attempts": 1,
            }
        },
    )
    svc_db.add(cancelled_task)
    await svc_db.commit()
    await svc_db.refresh(cancelled_task)

    # If `_process_post_commit` is called, the test fails — it should never run.
    mock_process = AsyncMock(
        side_effect=AssertionError(
            "_process_post_commit must not run on CANCELLED tasks"
        )
    )
    mocker.patch("app.routers.agent_status._process_post_commit", mock_process)

    await _sweep_post_commit_pending()

    await svc_db.refresh(cancelled_task)
    assert cancelled_task.status == TaskStatus.CANCELLED, "status must stay CANCELLED"
    assert cancelled_task.post_commit_pending is False, (
        "pending flag must be cleared so the row stops appearing in sweeps"
    )
    assert "_post_commit_pending_meta" not in (cancelled_task.context or {}), (
        "pending meta must be cleared alongside the flag"
    )
    mock_process.assert_not_called()


@pytest.mark.asyncio
async def test_post_commit_sweep_still_retries_completed_and_failed(svc_db, mocker):
    """The sweep continues to retry COMPLETED and FAILED tasks — only
    CANCELLED is special-cased."""
    import uuid as _uuid

    project = await _make_project(svc_db)
    completed_id = _uuid.uuid4()
    failed_id = _uuid.uuid4()
    completed_task = Task(
        id=completed_id,
        project_id=project.id,
        payloads=[{"type": "plan", "prompt": "decompose"}],
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        post_commit_pending=True,
        context={
            "_post_commit_pending_meta": {
                "at": _naive_utcnow().isoformat(),
                "reason": "post_commit_error",
                "payload": {
                    "task_id": str(completed_id),
                    "status": "COMPLETED",
                    "logs": "{}",
                },
                "attempts": 1,
            }
        },
    )
    failed_task = Task(
        id=failed_id,
        project_id=project.id,
        payloads=[{"type": "plan", "prompt": "decompose"}],
        status=TaskStatus.FAILED,
        created_by="ORCHESTRATOR",
        post_commit_pending=True,
        context={
            "_post_commit_pending_meta": {
                "at": _naive_utcnow().isoformat(),
                "reason": "post_commit_error",
                "payload": {
                    "task_id": str(failed_id),
                    "status": "FAILED",
                    "logs": "{}",
                },
                "attempts": 1,
            }
        },
    )
    svc_db.add_all([completed_task, failed_task])
    await svc_db.commit()

    mock_process = AsyncMock()  # succeed silently
    mocker.patch("app.routers.agent_status._process_post_commit", mock_process)

    await _sweep_post_commit_pending()

    # Both retried.
    assert mock_process.call_count == 2


# ══════════════════════════════════════════════════════════════════════════════
# _sweep_post_commit_pending — write-ahead marker age-gate (incident 2026-05-21)
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_post_commit_sweep_skips_recent_inline_running_marker(svc_db, mocker):
    """A reason="inline_running" marker is written-ahead BEFORE
    _process_post_commit runs. A *recent* one may still be a live inline pass,
    so the sweep must NOT retry it — retrying would double-process the
    post-commit (double review-spawn / double merge)."""
    project = await _make_project(svc_db)
    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "x"}],
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        post_commit_pending=True,
        context={
            "_post_commit_pending_meta": {
                "at": _naive_utcnow().isoformat(),  # just now — run still live
                "reason": "inline_running",
                "payload": {"task_id": "x", "status": "COMPLETED", "logs": "{}"},
                "attempts": 0,
            }
        },
    )
    svc_db.add(task)
    await svc_db.commit()

    mock_process = AsyncMock(
        side_effect=AssertionError("must not retry a live inline_running marker")
    )
    mocker.patch("app.routers.agent_status._process_post_commit", mock_process)

    await _sweep_post_commit_pending()

    mock_process.assert_not_called()
    await svc_db.refresh(task)
    assert task.post_commit_pending is True, "marker must persist — run in flight"


@pytest.mark.asyncio
async def test_post_commit_sweep_retries_stale_inline_running_marker(svc_db, mocker):
    """Orphan recovery for incident 2026-05-21: when _process_post_commit is
    killed mid-flight by an instance shutdown, the except handler never runs,
    so the write-ahead marker stays reason="inline_running" forever. Once it
    ages past the grace window the sweep MUST retry it — without this the task
    orphans permanently (cluster PR #4775 / be15c811 stranded eefbed05)."""
    import uuid as _uuid

    project = await _make_project(svc_db)
    task_id = _uuid.uuid4()
    task = Task(
        id=task_id,
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "x"}],
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        post_commit_pending=True,
        context={
            "_post_commit_pending_meta": {
                # 20 min old — well past the 600s grace; the inline pass that
                # set this is provably dead.
                "at": (_naive_utcnow() - timedelta(minutes=20)).isoformat(),
                "reason": "inline_running",
                "payload": {
                    "task_id": str(task_id),
                    "status": "COMPLETED",
                    "logs": "{}",
                },
                "attempts": 0,
            }
        },
    )
    svc_db.add(task)
    await svc_db.commit()

    mock_process = AsyncMock()  # succeeds
    mocker.patch("app.routers.agent_status._process_post_commit", mock_process)

    await _sweep_post_commit_pending()

    mock_process.assert_called_once()
    await svc_db.refresh(task)
    assert task.post_commit_pending is False, "recovered marker must be cleared"
    assert "_post_commit_pending_meta" not in (task.context or {})


@pytest.mark.asyncio
async def test_clear_post_commit_pending_clears_write_ahead_marker(svc_db):
    """_clear_post_commit_pending is the happy-path counterpart to the sweep:
    after a clean _process_post_commit run, update_agent_status calls it to
    drop the write-ahead marker so the sweep never re-runs a finished
    post-commit."""
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.routers.agent_status import _clear_post_commit_pending

    project = await _make_project(svc_db)
    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "x"}],
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        post_commit_pending=True,
        context={
            "keep_me": "untouched",
            "_post_commit_pending_meta": {
                "at": _naive_utcnow().isoformat(),
                "reason": "inline_running",
                "payload": {"task_id": "x", "status": "COMPLETED", "logs": "{}"},
                "attempts": 0,
            },
        },
    )
    svc_db.add(task)
    await svc_db.commit()

    factory = async_sessionmaker(
        svc_db.bind, class_=AsyncSession, expire_on_commit=False
    )
    await _clear_post_commit_pending(factory, task.id)

    await svc_db.refresh(task)
    assert task.post_commit_pending is False
    assert "_post_commit_pending_meta" not in (task.context or {})
    assert (task.context or {}).get("keep_me") == "untouched", (
        "unrelated context keys must survive the clear"
    )


# ══════════════════════════════════════════════════════════════════════════════
# _sweep_post_commit_pending — bump-failure isolation (regression 2026-05-07)
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_post_commit_sweep_bump_failure_does_not_poison_batch(svc_db, mocker):
    """When the attempt-bump fails on one task (e.g. row-lock contention →
    Postgres LockNotAvailableError on the bump UPDATE), the sweep must
    continue and bump the OTHER tasks in the batch instead of aborting.
    Regression for 2026-05-07 incident: a single stuck task's bump failure
    propagated to the outer try/except, leaving subsequent pending tasks
    unprocessed AND letting the same bad task burn ~30s of orchestrator
    cycle time per sweep (lock_timeout) → /health 503 → instance kill loop.
    """
    import uuid as _uuid

    project = await _make_project(svc_db)
    bad_id = _uuid.uuid4()
    good_id = _uuid.uuid4()
    bad_task = Task(
        id=bad_id,
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "bad"}],
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        post_commit_pending=True,
        context={
            "_post_commit_pending_meta": {
                "at": _naive_utcnow().isoformat(),
                "reason": "post_commit_error",
                "payload": {
                    "task_id": str(bad_id),
                    "status": "COMPLETED",
                    "logs": "{}",
                },
                "attempts": 4,
            }
        },
    )
    good_task = Task(
        id=good_id,
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "good"}],
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        post_commit_pending=True,
        context={
            "_post_commit_pending_meta": {
                "at": _naive_utcnow().isoformat(),
                "reason": "post_commit_error",
                "payload": {
                    "task_id": str(good_id),
                    "status": "COMPLETED",
                    "logs": "{}",
                },
                "attempts": 1,
            }
        },
    )
    svc_db.add_all([bad_task, good_task])
    await svc_db.commit()

    # _process_post_commit raises for BOTH tasks → bump path runs for both.
    # This isolates the test from sweep iteration order: the regression is
    # about whether one task's bump failure blocks the others' bumps.
    call_log: list[str] = []

    async def fake_process(task_id, _payload):
        call_log.append(str(task_id))
        raise RuntimeError("simulated _process_post_commit retry exhaustion")

    mocker.patch(
        "app.routers.agent_status._process_post_commit", side_effect=fake_process
    )

    # Make ONLY the bad task's bump raise. flag_modified is called inside
    # the bump block before commit; raising there mimics the production
    # LockNotAvailableError on bump_session.commit() (single-point injection
    # since patching commit() across multiple async sessions is awkward).
    from sqlalchemy.orm import attributes as _sa_attrs

    real_flag_modified = _sa_attrs.flag_modified

    def fake_flag_modified(obj, key):
        if isinstance(obj, Task) and str(obj.id) == str(bad_id) and key == "context":
            raise RuntimeError("simulated bump lock_timeout")
        return real_flag_modified(obj, key)

    mocker.patch("sqlalchemy.orm.attributes.flag_modified", fake_flag_modified)

    await _sweep_post_commit_pending()

    # Both tasks reached _process_post_commit (the regression was that the
    # outer except aborted the loop after the first failure).
    assert str(bad_id) in call_log
    assert str(good_id) in call_log

    # Good task: bump succeeded → attempts advanced 1 → 2.
    await svc_db.refresh(good_task)
    good_attempts = (
        (good_task.context or {}).get("_post_commit_pending_meta", {}).get("attempts")
    )
    assert good_attempts == 2, (
        f"good task's bump should have advanced attempts to 2, got {good_attempts}. "
        f"This means the bad task's bump failure poisoned the batch — the regression is back."
    )

    # Bad task: bump failed → attempts stays at 4. The sweep must NOT have
    # raised (otherwise the good task wouldn't have been processed).
    await svc_db.refresh(bad_task)
    bad_attempts = (
        (bad_task.context or {}).get("_post_commit_pending_meta", {}).get("attempts")
    )
    assert bad_attempts == 4, f"bad task attempts should stay at 4, got {bad_attempts}"


# ══════════════════════════════════════════════════════════════════════════════
# stale_branch_prefixes (project-driven branch cleanup)
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_stale_branch_prefixes_default_when_not_configured(svc_db):
    """Project without build_config returns the default Blueprint prefixes."""
    project = await _make_project(svc_db)
    # Project has no build_config set
    assert project.build_config is None
    # Should return the Blueprint defaults
    assert project.stale_branch_prefixes == ("blueprint/", "research/", "plan/")


@pytest.mark.asyncio
async def test_stale_branch_prefixes_follows_namespace_override(svc_db):
    """Project with task_branch_namespace override picks up the new prefix."""
    project = Project(
        name="Autoproj Project",
        user_id="user1",
        build_config={"task_branch_namespace": "autoproj"},
    )
    svc_db.add(project)
    await svc_db.commit()
    await svc_db.refresh(project)
    assert project.task_branch_namespace == "autoproj"
    # Without an explicit stale_branch_prefixes override, the namespace flows
    # into the default cleanup-prefix tuple so per-project work branches stay
    # eligible for sweep.
    assert project.stale_branch_prefixes == ("autoproj/", "research/", "plan/")


@pytest.mark.asyncio
async def test_stale_branch_prefixes_custom_from_build_config(svc_db):
    """Project with custom prefixes in build_config uses them."""
    project = Project(
        name="Custom Project",
        user_id="user1",
        build_config={"stale_branch_prefixes": ["feature/", "hotfix/"]},
    )
    svc_db.add(project)
    await svc_db.commit()
    await svc_db.refresh(project)
    assert project.stale_branch_prefixes == ("feature/", "hotfix/")


@pytest.mark.asyncio
async def test_stale_branch_prefixes_empty_list_disables_cleanup(svc_db):
    """Project with empty prefix list in build_config disables cleanup."""
    project = Project(
        name="No Cleanup Project",
        user_id="user1",
        build_config={"stale_branch_prefixes": []},
    )
    svc_db.add(project)
    await svc_db.commit()
    await svc_db.refresh(project)
    # Empty list should be returned as-is (cleanup disabled for this project)
    assert project.stale_branch_prefixes == ()


@pytest.mark.asyncio
async def test_stale_branch_prefixes_preserves_single_prefix(svc_db):
    """Project with single custom prefix works correctly."""
    project = Project(
        name="Single Prefix Project",
        user_id="user1",
        build_config={"stale_branch_prefixes": ["task/"]},
    )
    svc_db.add(project)
    await svc_db.commit()
    await svc_db.refresh(project)
    assert project.stale_branch_prefixes == ("task/",)


# ══════════════════════════════════════════════════════════════════════════════
# protected_branches (project-driven cleanup safety)
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_protected_branches_default_when_not_configured(svc_db):
    """Project without build_config returns the default protected branch set."""
    project = await _make_project(svc_db)
    assert project.build_config is None
    protected = project.protected_branches
    assert "main" in protected
    assert "master" in protected
    assert "develop" in protected
    assert "dev" in protected


@pytest.mark.asyncio
async def test_protected_branches_custom_from_build_config(svc_db):
    """Project with custom protected_branches in build_config uses them."""
    project = Project(
        name="Custom Protected Project",
        user_id="user1",
        build_config={"protected_branches": ["trunk", "stable"]},
    )
    svc_db.add(project)
    await svc_db.commit()
    await svc_db.refresh(project)
    protected = project.protected_branches
    assert "trunk" in protected
    assert "stable" in protected


@pytest.mark.asyncio
async def test_protected_branches_always_includes_integration_branch(svc_db):
    """integration_branch is always in the protected set regardless of config."""
    project = Project(
        name="Integration Branch Project",
        user_id="user1",
        integration_branch="develop-lts",
        build_config={"protected_branches": ["trunk"]},
    )
    svc_db.add(project)
    await svc_db.commit()
    await svc_db.refresh(project)
    assert "develop-lts" in project.protected_branches


@pytest.mark.asyncio
async def test_protected_branches_always_includes_promotion_target(svc_db):
    """promotion_target is always in the protected set regardless of config."""
    project = Project(
        name="Promotion Target Project",
        user_id="user1",
        promotion_target="release-lts",
        build_config={"protected_branches": ["trunk"]},
    )
    svc_db.add(project)
    await svc_db.commit()
    await svc_db.refresh(project)
    assert "release-lts" in project.protected_branches


# ── _derive_task_branch: task-type -> branch prefix mapping ──────────────────


def test_derive_task_branch_known_prefixes():
    """Each task type with a non-default prefix maps correctly."""
    task_id = _uuid.UUID("12345678-abcd-ef01-2345-678abcdef012")
    short = "12345678"
    assert _derive_task_branch(task_id, "refactor") == f"blueprint/refactor-{short}"
    assert _derive_task_branch(task_id, "docs") == f"blueprint/docs-{short}"
    assert _derive_task_branch(task_id, "deps") == f"blueprint/deps-{short}"
    assert _derive_task_branch(task_id, "migrate") == f"blueprint/migrate-{short}"


def test_derive_task_branch_default_prefix():
    """Task types that map to 'task' all yield blueprint/task-{short_id}."""
    task_id = _uuid.UUID("12345678-abcd-ef01-2345-678abcdef012")
    short = "12345678"
    for task_type in (
        "create",
        "qa",
        "fix",
        "perf",
        "security_audit",
        "debug",
        "smoketest",
        None,
        "",
    ):
        assert _derive_task_branch(task_id, task_type) == f"blueprint/task-{short}", (
            task_type
        )


def test_derive_task_branch_short_id_derivation():
    """short_id is exactly the first 8 hex chars of the UUID with dashes stripped."""
    task_id = _uuid.UUID("aabbccdd-1122-3344-5566-778899aabbcc")
    branch = _derive_task_branch(task_id, "create")
    assert branch == "blueprint/task-aabbccdd"


# ── derive_task_branch: per-project namespace override ───────────────────────


def test_derive_task_branch_custom_namespace():
    """Passing namespace= overrides the leading 'blueprint/' segment."""
    task_id = _uuid.UUID("12345678-abcd-ef01-2345-678abcdef012")
    short = "12345678"
    assert (
        _derive_task_branch(task_id, "refactor", "autoproj")
        == f"autoproj/refactor-{short}"
    )
    assert _derive_task_branch(task_id, "docs", "bot") == f"bot/docs-{short}"
    # Default kw matches pre-namespace call sites byte-for-byte.
    assert _derive_task_branch(task_id, "refactor") == f"blueprint/refactor-{short}"


def test_get_project_task_branch_namespace_fallback_chain():
    """Resolver returns project override → runtime_config → 'blueprint'."""
    from app.shared_state import (
        get_project_task_branch_namespace,
        runtime_config,
    )

    class _StubProject:
        def __init__(self, build_config):
            self.build_config = build_config

    # Per-project build_config wins
    assert (
        get_project_task_branch_namespace(
            _StubProject({"task_branch_namespace": "autoproj"})
        )
        == "autoproj"
    )
    # Empty build_config falls through to runtime_config default
    assert get_project_task_branch_namespace(_StubProject({})) == runtime_config.get(
        "task_branch_namespace", "blueprint"
    )
    # No project at all returns runtime_config default
    assert get_project_task_branch_namespace(None) == runtime_config.get(
        "task_branch_namespace", "blueprint"
    )


def test_build_branch_short_id_re_custom_namespace():
    """Per-project regex recognizes branches under a custom namespace."""
    from app.services import _build_branch_short_id_re

    pattern = _build_branch_short_id_re("autoproj")
    assert pattern.match("autoproj/refactor-abcdef12").group(1) == "abcdef12"
    assert pattern.match("autoproj/fix-12345678").group(1) == "12345678"
    # Default staging prefixes still recognized
    assert pattern.match("research/abcdef12").group(1) == "abcdef12"
    assert pattern.match("plan/abcdef12").group(1) == "abcdef12"
    # The default Blueprint namespace is NOT matched by an autoproj regex.
    assert pattern.match("blueprint/refactor-abcdef12") is None


def test_build_branch_short_id_re_custom_staging_prefixes():
    """Custom staging prefixes replace the default research/plan pair."""
    from app.services import _build_branch_short_id_re

    pattern = _build_branch_short_id_re(
        "autoproj", staging_prefixes=("feature/", "experiment/")
    )
    # Custom staging prefixes are matched
    assert pattern.match("feature/abcdef12").group(1) == "abcdef12"
    assert pattern.match("experiment/12345678").group(1) == "12345678"
    # Task namespace branches still matched
    assert pattern.match("autoproj/fix-abcdef12").group(1) == "abcdef12"
    # Blueprint defaults NOT matched when overridden
    assert pattern.match("research/abcdef12") is None
    assert pattern.match("plan/abcdef12") is None


def test_build_branch_short_id_re_default_staging_prefixes_preserved():
    """Blueprint default staging prefixes work without explicit override."""
    from app.services import _build_branch_short_id_re, _DEFAULT_STAGING_PREFIXES

    assert _DEFAULT_STAGING_PREFIXES == ("research/", "plan/")
    pattern = _build_branch_short_id_re("blueprint")
    assert pattern.match("research/abcdef12").group(1) == "abcdef12"
    assert pattern.match("plan/abcdef12").group(1) == "abcdef12"
    assert pattern.match("blueprint/task-abcdef12").group(1) == "abcdef12"
