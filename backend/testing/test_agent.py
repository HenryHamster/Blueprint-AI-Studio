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
    mock_delete = mocker.patch("app.main.delete_agent_vm", return_value=True)

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


@pytest.mark.asyncio
async def test_agent_token_rejected_for_terminated_task(client: AsyncClient, db_session):
    """
    Tests that an agent cannot use a JWT for a task that has been terminated.
    The _terminated_task_ids set (populated by SQLAlchemy event on status transition)
    causes verify_agent_token to reject the request with 401.
    """
    from app.routers.agent import _terminated_task_ids

    project = Project(name="Token Revocation Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"cmd": "run"}],
        vm_instance_id="agent-vm-revoked",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    task_id_str = str(task.id)
    token = create_agent_token(task_id_str, "agent-vm-revoked")
    headers = {"x-agent-token": token}

    # Simulate task reaching terminal state (what the SQLAlchemy event does in production)
    _terminated_task_ids[task_id_str] = 9999999999.0  # far-future timestamp

    # Heartbeat should now be rejected
    resp = await client.post("/api/agent/heartbeat",
        json={"task_id": task_id_str, "status_message": "still running"},
        headers=headers)
    assert resp.status_code == 401

    # Clean up to avoid polluting other tests
    _terminated_task_ids.pop(task_id_str, None)


@pytest.mark.asyncio
async def test_agent_claim_active_tasks_include_related_files(client: AsyncClient, db_session):
    """
    Tests that the project_context returned from /api/agent/claim includes
    related_files in each active_tasks entry when the active task has related_files
    in its context.
    """
    project = Project(name="Related Files Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # An already-IN_PROGRESS task with related_files in its context
    active_task = Task(
        project_id=project.id,
        title="Active sibling task",
        payloads=[{"type": "create", "prompt": "Do something"}],
        vm_instance_id="agent-vm-active",
        status=TaskStatus.IN_PROGRESS,
        context={"related_files": ["backend/app/main.py", "agent/src/index.ts"]},
    )
    db_session.add(active_task)
    await db_session.commit()
    await db_session.refresh(active_task)

    # The task being claimed (research type so active_tasks are included)
    claim_task = Task(
        project_id=project.id,
        title="Claiming task",
        payloads=[{"type": "research", "prompt": "Investigate things"}],
        vm_instance_id="agent-vm-claimer",
    )
    db_session.add(claim_task)
    await db_session.commit()
    await db_session.refresh(claim_task)

    token = create_agent_token(str(claim_task.id), "agent-vm-claimer")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200

    data = resp.json()
    project_context = data.get("project_context", {})
    active_tasks = project_context.get("active_tasks", [])

    assert len(active_tasks) == 1
    entry = active_tasks[0]
    assert entry["title"] == "Active sibling task"
    assert entry["related_files"] == ["backend/app/main.py", "agent/src/index.ts"]


@pytest.mark.asyncio
async def test_agent_claim_active_tasks_related_files_defaults_to_empty(client: AsyncClient, db_session):
    """
    Tests that active_tasks entries have an empty related_files list when
    the active task has no related_files in its context.
    """
    project = Project(name="No Related Files Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    active_task = Task(
        project_id=project.id,
        title="Active task no files",
        payloads=[{"type": "create", "prompt": "Do stuff"}],
        vm_instance_id="agent-vm-nofiles",
        status=TaskStatus.IN_PROGRESS,
        context={},
    )
    db_session.add(active_task)
    await db_session.commit()
    await db_session.refresh(active_task)

    claim_task = Task(
        project_id=project.id,
        title="Claimer task",
        payloads=[{"type": "research", "prompt": "Research things"}],
        vm_instance_id="agent-vm-claimer2",
    )
    db_session.add(claim_task)
    await db_session.commit()
    await db_session.refresh(claim_task)

    token = create_agent_token(str(claim_task.id), "agent-vm-claimer2")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200

    data = resp.json()
    active_tasks = data.get("project_context", {}).get("active_tasks", [])
    assert len(active_tasks) == 1
    assert active_tasks[0]["related_files"] == []


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_playbook_set_and_get(client: AsyncClient, db_session):
    """Tests setting and retrieving playbook entries via POST /api/agent/playbook."""
    project = Project(name="Playbook Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "Do something"}],
        vm_instance_id="agent-vm-playbook1",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-playbook1")
    headers = {"x-agent-token": token}

    # Set two playbook entries
    resp = await client.post("/api/agent/playbook", json={
        "task_id": str(task.id), "op": "set", "key": "build-commands", "value": "npm run build"
    }, headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["op"] == "set"
    assert data["key"] == "build-commands"
    assert data["revision"] == 1

    resp2 = await client.post("/api/agent/playbook", json={
        "task_id": str(task.id), "op": "set", "key": "lint-rules", "value": "eslint --max-warnings 0"
    }, headers=headers)
    assert resp2.status_code == 200
    assert resp2.json()["revision"] == 2

    # Verify stored in design_doc
    await db_session.refresh(project)
    assert project.design_doc is not None
    assert "playbook/build-commands" in project.design_doc
    assert project.design_doc["playbook/build-commands"]["content"] == "npm run build"
    assert project.design_doc["playbook/build-commands"]["source_task_id"] == str(task.id)
    assert "playbook/lint-rules" in project.design_doc

    # Get all playbook entries
    resp3 = await client.post("/api/agent/playbook", json={
        "task_id": str(task.id), "op": "get"
    }, headers=headers)
    assert resp3.status_code == 200
    data3 = resp3.json()
    assert data3["status"] == "ok"
    assert data3["op"] == "get"
    entries = data3["entries"]
    assert "build-commands" in entries
    assert entries["build-commands"]["content"] == "npm run build"
    assert "lint-rules" in entries

    # List keys
    resp4 = await client.post("/api/agent/playbook", json={
        "task_id": str(task.id), "op": "list"
    }, headers=headers)
    assert resp4.status_code == 200
    data4 = resp4.json()
    assert set(data4["keys"]) == {"build-commands", "lint-rules"}

    # Delete one entry
    resp5 = await client.post("/api/agent/playbook", json={
        "task_id": str(task.id), "op": "delete", "key": "lint-rules"
    }, headers=headers)
    assert resp5.status_code == 200
    assert resp5.json()["status"] == "ok"

    await db_session.refresh(project)
    assert "playbook/lint-rules" not in project.design_doc
    assert "playbook/build-commands" in project.design_doc


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_playbook_size_limits(client: AsyncClient, db_session):
    """Tests that per-entry 2KB and total 30KB size limits are enforced."""
    project = Project(name="Playbook Size Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "fix", "prompt": "Fix it"}],
        vm_instance_id="agent-vm-playbook2",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-playbook2")
    headers = {"x-agent-token": token}

    # Per-entry limit: value > 2048 bytes should be rejected
    oversized_value = "x" * 2049
    resp = await client.post("/api/agent/playbook", json={
        "task_id": str(task.id), "op": "set", "key": "too-big", "value": oversized_value
    }, headers=headers)
    assert resp.status_code == 400, f"Expected 400, got: {resp.json()}"
    assert "2KB" in resp.json()["error"]["message"]

    # Total playbook limit: fill up to near 30KB then overflow
    # Each entry ~2000 chars; 16 entries = ~32KB total
    large_value = "y" * 2000
    for i in range(14):
        r = await client.post("/api/agent/playbook", json={
            "task_id": str(task.id), "op": "set", "key": f"entry-{i:02d}", "value": large_value
        }, headers=headers)
        assert r.status_code == 200, f"Entry {i} failed: {r.json()}"

    # The 15th entry should push past 30KB
    resp2 = await client.post("/api/agent/playbook", json={
        "task_id": str(task.id), "op": "set", "key": "overflow-entry", "value": large_value
    }, headers=headers)
    assert resp2.status_code == 400, f"Expected 400, got: {resp2.json()}"
    assert "30KB" in resp2.json()["error"]["message"]


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_playbook_any_task_type_allowed(client: AsyncClient, db_session):
    """Tests that any task type (not just design_doc_update) can use the playbook endpoint."""
    project = Project(name="Playbook Any Type Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Use task type "create" — unlike the design-doc endpoint which restricts by type
    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "Build something"}],
        vm_instance_id="agent-vm-playbook3",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-playbook3")
    headers = {"x-agent-token": token}

    resp = await client.post("/api/agent/playbook", json={
        "task_id": str(task.id), "op": "set", "key": "test-pattern", "value": "use pytest fixtures"
    }, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_claim_response_includes_playbook(client: AsyncClient, db_session):
    """Tests that claim endpoint injects playbook entries into the response."""
    project = Project(name="Claim Playbook Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Seed playbook entries directly in design_doc
    from sqlalchemy.orm.attributes import flag_modified
    project.design_doc = {
        "playbook/build-steps": {
            "content": "run npm run build",
            "source_task_id": "seed",
            "updated_at": "2026-01-01T00:00:00",
            "confirmed_count": 0,
        },
        "playbook/test-commands": {
            "content": "bash scripts/test-backend.sh",
            "source_task_id": "seed",
            "updated_at": "2026-01-01T00:00:00",
            "confirmed_count": 0,
        },
    }
    flag_modified(project, "design_doc")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "Do work"}],
        vm_instance_id="agent-vm-claim-playbook",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-claim-playbook")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200

    data = resp.json()
    assert "playbook" in data, f"Expected 'playbook' in claim response, got keys: {list(data.keys())}"
    playbook = data["playbook"]
    assert "build-steps" in playbook
    assert playbook["build-steps"] == "run npm run build"
    assert "test-commands" in playbook
    assert playbook["test-commands"] == "bash scripts/test-backend.sh"


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
    import app.lifecycle as lifecycle_module
    import app.orchestrator as orchestrator_module

    # Prevent startup coroutines from trying to connect to a real database
    mocker.patch.object(main_module, "load_max_plan_pool", new_callable=AsyncMock)
    mocker.patch.object(main_module, "_verify_schema_version", new_callable=AsyncMock)
    mocker.patch.object(lifecycle_module, "_seed_initial_admin", new_callable=AsyncMock)
    mocker.patch.object(main_module, "_init_deployment_state", new_callable=AsyncMock)
    # Background task coroutines must also be no-ops so the task loop doesn't
    # block the test event loop or attempt DB queries
    mocker.patch.object(main_module, "reaper_service", new_callable=AsyncMock)
    mocker.patch.object(main_module, "gcp_cleanup_service", new_callable=AsyncMock)
    mocker.patch.object(main_module, "secret_reconciliation_service", new_callable=AsyncMock)
    mocker.patch.object(orchestrator_module, "orchestrator_worker", new_callable=AsyncMock)

    task_id = uuid.uuid4()
    with TestClient(app=main_module.app) as tc:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with tc.websocket_connect(f"/api/agent/ws/{task_id}") as ws:
                ws.send_json({"token": "not-a-valid-jwt-token"})
                ws.receive_text()
        assert exc_info.value.code == 1008


@pytest.mark.asyncio
async def test_external_api_requeue_sets_exponential_backoff(client: AsyncClient, db_session, mocker):
    """
    Tests that EXTERNAL_API failures requeue with exponential backoff in retry_after.
    - 1st retry: ~2 min delay (2^1 * 120 = 240s)
    - 2nd retry: ~8 min delay (2^2 * 120 = 480s)
    - 3rd retry: ~30 min delay (2^3 * 120 = 960s, but capped at 1800s — not capped here)
    - After 3 retries, task is not requeued
    """
    mocker.patch("app.main.delete_agent_vm", return_value=True)
    mocker.patch("app.main._ensure_vm_deleted", new_callable=mocker.AsyncMock)
    mocker.patch("app.main.broadcast_to_project", return_value=None)

    project = Project(name="Backoff Test", user_id="test-user-backoff")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    for retry_num in range(1, 4):
        task = Task(
            project_id=project.id,
            payloads=[{"cmd": "test"}],
            vm_instance_id=f"agent-vm-backoff-{retry_num}",
            status=TaskStatus.IN_PROGRESS,
            context={"_api_failure_retries": retry_num - 1},
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        token = create_agent_token(str(task.id), f"agent-vm-backoff-{retry_num}")
        headers = {"x-agent-token": token}
        payload = {
            "task_id": str(task.id),
            "status": "FAILED",
            "failure_class": "EXTERNAL_API",
            "logs": "api.github.com returned 503",
        }
        before = datetime.utcnow()
        resp = await client.post("/api/agent/status", json=payload, headers=headers)
        after = datetime.utcnow()
        assert resp.status_code == 200

        await db_session.refresh(task)
        assert task.status == TaskStatus.QUEUED, f"retry {retry_num}: expected QUEUED"
        ctx = task.context or {}
        assert "retry_after" in ctx, f"retry {retry_num}: retry_after not set"

        retry_after_dt = datetime.fromisoformat(ctx["retry_after"])
        expected_delay = min(2 ** retry_num * 120, 1800)
        actual_delay = (retry_after_dt - before).total_seconds()
        assert actual_delay >= expected_delay - 2, f"retry {retry_num}: delay {actual_delay} < expected {expected_delay}"
        assert actual_delay <= expected_delay + 5, f"retry {retry_num}: delay {actual_delay} too large"

    # 4th attempt: api_retries=3, increments to 4 > 3 → EXTERNAL_API early-requeue path is skipped
    # Falls through to general retry (task.attempt_count < max_retries), so no retry_after is set
    task = Task(
        project_id=project.id,
        payloads=[{"cmd": "test"}],
        vm_instance_id="agent-vm-backoff-4",
        status=TaskStatus.IN_PROGRESS,
        context={"_api_failure_retries": 3},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-backoff-4")
    headers = {"x-agent-token": token}
    payload = {
        "task_id": str(task.id),
        "status": "FAILED",
        "failure_class": "EXTERNAL_API",
        "logs": "api.github.com returned 503",
    }
    resp = await client.post("/api/agent/status", json=payload, headers=headers)
    assert resp.status_code == 200

    await db_session.refresh(task)
    # Falls through to general retry path — no EXTERNAL_API-specific retry_after is set
    ctx = task.context or {}
    assert "retry_after" not in ctx, "after EXTERNAL_API cap exceeded, retry_after should not be set by EXTERNAL_API path"
