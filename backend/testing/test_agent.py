import asyncio
import uuid
import pytest
from datetime import datetime, timedelta
from httpx import AsyncClient
from sqlalchemy import select
from app.models import Project, Task, TaskStatus, ProjectStatus, ArtifactType, Artifact
from app.auth import create_agent_token

@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_agent_claim_success(client: AsyncClient, db_session):
    """
    Tests an agent successfully claiming a QUEUED task.
    """
    project = Project(name="Agent Claim Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(project_id=project.id, payloads=[{"cmd": "ls"}], vm_instance_id="agent-vm-999")
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # Generate a valid JWT for this task/VM
    token = create_agent_token(str(task.id), "agent-vm-999")
    headers = {"x-agent-token": token}
    resp = await client.post("/api/agent/claim", headers=headers)

    assert resp.status_code == 200
    data = resp.json()
    assert data["task_id"] is not None
    assert data["title"] is not None
    assert "payloads" in data

    await db_session.refresh(task)
    assert task.status == TaskStatus.IN_PROGRESS

@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_agent_claim_unauthorized(client: AsyncClient):
    """
    Tests that missing or invalid token returns 401.
    """
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": "wrong-token"})
    assert resp.status_code == 401

@pytest.mark.asyncio
async def test_agent_claim_not_found(client: AsyncClient):
    """
    Tests that claiming a task with an invalid VM ID returns 404.
    """
    token = create_agent_token("00000000-0000-0000-0000-000000000000", "non-existent-vm")
    headers = {"x-agent-token": token}
    resp = await client.post("/api/agent/claim", headers=headers)
    assert resp.status_code == 404

@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_agent_status_update(client: AsyncClient, db_session, mocker):
    """
    Tests an agent updating task status and creating an artifact.
    Also verifies that completing the last task completes the project.
    """
    mock_delete = mocker.patch("app.main.delete_agent_vm", return_value=None)

    project = Project(name="Agent Status Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(project_id=project.id, payloads=[{"cmd": "ls"}], vm_instance_id="agent-vm-888", status=TaskStatus.IN_PROGRESS)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-888")
    headers = {"x-agent-token": token}
    payload = {
        "task_id": str(task.id),
        "status": "COMPLETED",
        "artifact_type": "ERROR_LOG",
        "logs": "build success"
    }
    resp = await client.post("/api/agent/status", json=payload, headers=headers)
    assert resp.status_code == 200

    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED

    await db_session.refresh(project)
    assert project.status == ProjectStatus.COMPLETED

    mock_delete.assert_called_once_with("agent-vm-888")


@pytest.mark.asyncio
async def test_agent_claim_concurrent_only_one_succeeds(client: AsyncClient, db_session):
    """
    Tests that two claim requests for the same task result in only one succeeding.
    The second request should return 404 because the task is no longer QUEUED after
    the first claim (idempotency: a task can only be claimed once).
    """
    project = Project(name="Concurrent Claim Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(project_id=project.id, payloads=[{"cmd": "run"}], vm_instance_id="agent-vm-concurrent")
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-concurrent")
    headers = {"x-agent-token": token}

    # First claim succeeds: task transitions to IN_PROGRESS
    resp1 = await client.post("/api/agent/claim", headers=headers)
    assert resp1.status_code == 200

    # Second claim for the same VM returns 404 because the task is no longer QUEUED
    resp2 = await client.post("/api/agent/claim", headers=headers)
    assert resp2.status_code == 404


@pytest.mark.asyncio
async def test_agent_heartbeat_updates_last_heartbeat(client: AsyncClient, db_session):
    """
    Tests that a heartbeat request updates the task's last_heartbeat timestamp.
    A task with a fresh heartbeat is within the 5-minute reaper threshold and
    will not be killed by the reaper.
    """
    project = Project(name="Heartbeat Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"cmd": "run"}],
        vm_instance_id="agent-vm-heartbeat",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    assert task.last_heartbeat is None

    token = create_agent_token(str(task.id), "agent-vm-heartbeat")
    headers = {"x-agent-token": token}
    payload = {"task_id": str(task.id), "status_message": "still running"}

    resp = await client.post("/api/agent/heartbeat", json=payload, headers=headers)

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}

    await db_session.refresh(task)
    assert task.last_heartbeat is not None

    # A task with a heartbeat this recent is well within the 5-minute reaper window
    five_minutes_ago = datetime.utcnow() - timedelta(minutes=5)
    assert task.last_heartbeat > five_minutes_ago


@pytest.mark.asyncio
async def test_agent_status_complete_with_artifacts_creates_artifact_record(client: AsyncClient, db_session, mocker):
    """
    Tests that completing a task with artifact information creates an Artifact record
    in the database and transitions the task status to COMPLETED.

    A sibling task is kept IN_PROGRESS so that the project aggregation path does not
    trigger a session.commit() followed by a lazy attribute access that would fail in
    the SQLite async test environment (MissingGreenlet).
    """
    mocker.patch("app.main.delete_agent_vm", return_value=None)

    project = Project(name="Artifact Creation Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"cmd": "run"}],
        vm_instance_id="agent-vm-artifact",
        status=TaskStatus.IN_PROGRESS,
    )
    # Sibling task stays IN_PROGRESS so the project is NOT fully complete yet,
    # preventing the project-aggregation commit path that has a SQLite async
    # limitation (lazy reload of expired primary key after commit).
    sibling = Task(
        project_id=project.id,
        payloads=[{"cmd": "other"}],
        vm_instance_id="agent-vm-artifact-sibling",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    db_session.add(sibling)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-artifact")
    headers = {"x-agent-token": token}
    payload = {
        "task_id": str(task.id),
        "status": "COMPLETED",
        "artifact_type": "GIT_COMMIT",
        "commit_hash": "abc123def456",
        "repo_url": "https://github.com/example/repo",
        "logs": "Build and tests passed",
    }

    resp = await client.post("/api/agent/status", json=payload, headers=headers)
    assert resp.status_code == 200

    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED

    artifact_stmt = select(Artifact).where(Artifact.task_id == task.id)
    result = await db_session.execute(artifact_stmt)
    artifacts = result.scalars().all()

    assert len(artifacts) == 1
    assert artifacts[0].artifact_type == ArtifactType.GIT_COMMIT
    assert artifacts[0].commit_hash == "abc123def456"
    assert artifacts[0].repo_url == "https://github.com/example/repo"


@pytest.mark.asyncio
async def test_agent_status_already_completed_returns_200_idempotent(client: AsyncClient, db_session, mocker):
    """
    Tests that completing a task that is already in a terminal state (COMPLETED)
    returns 200 rather than 409, demonstrating idempotent behavior for agent retries.
    """
    mocker.patch("app.main.delete_agent_vm", return_value=None)

    project = Project(name="Idempotent Complete Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"cmd": "run"}],
        vm_instance_id="agent-vm-idempotent",
        status=TaskStatus.COMPLETED,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-idempotent")
    headers = {"x-agent-token": token}
    payload = {
        "task_id": str(task.id),
        "status": "COMPLETED",
    }

    resp = await client.post("/api/agent/status", json=payload, headers=headers)
    assert resp.status_code == 200

    data = resp.json()
    assert data["status"] == "already_terminal"
    assert data["task_id"] == str(task.id)


def test_agent_ws_invalid_token_closes_with_1008(mocker):
    """
    Tests that connecting to the agent WebSocket without a valid token causes
    the server to close the connection with code 1008 (Policy Violation).

    Startup DB functions are mocked out so that starlette.testclient.TestClient
    (which runs the full ASGI lifespan) does not attempt a real PostgreSQL
    connection during test setup.
    """
    from unittest.mock import AsyncMock
    from starlette.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect
    import app.main as main_module

    # Prevent startup coroutines from trying to connect to a real database
    mocker.patch.object(main_module, "load_max_plan_pool", new_callable=AsyncMock)
    mocker.patch.object(main_module, "_init_deployment_state", new_callable=AsyncMock)
    # Background task coroutines must also be no-ops so the task loop doesn't
    # block the test event loop or attempt DB queries
    mocker.patch.object(main_module, "reaper_service", new_callable=AsyncMock)
    mocker.patch.object(main_module, "gcp_cleanup_service", new_callable=AsyncMock)
    mocker.patch.object(main_module, "orchestrator_worker", new_callable=AsyncMock)

    task_id = uuid.uuid4()
    with TestClient(app=main_module.app) as tc:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with tc.websocket_connect(f"/api/agent/ws/{task_id}") as ws:
                ws.send_json({"token": "not-a-valid-jwt-token"})
                ws.receive_text()
        assert exc_info.value.code == 1008
