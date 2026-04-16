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
async def test_agent_heartbeat_accepts_pre_claim_provisioning_task(client: AsyncClient, db_session):
    """
    Tests that the heartbeat endpoint accepts a heartbeat from an agent whose task
    is still in PROVISIONING state. This is the pre-claim failure reporting path:
    an agent that dies during token validation or claim needs to post a final
    PRE_CLAIM_FAILED heartbeat so the reaper has forensic evidence instead of a
    silent provision failure.
    """
    project = Project(name="Pre-claim Heartbeat Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Task is in PROVISIONING — matches the state during the agent's pre-claim window.
    task = Task(
        project_id=project.id,
        payloads=[{"cmd": "run"}],
        vm_instance_id="agent-vm-preclaim",
        status=TaskStatus.PROVISIONING,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-preclaim")
    headers = {"x-agent-token": token}
    payload = {
        "task_id": str(task.id),
        "status_message": "PRE_CLAIM_FAILED: token validation rate-limited",
    }

    resp = await client.post("/api/agent/heartbeat", json=payload, headers=headers)

    assert resp.status_code == 200
    await db_session.refresh(task)
    assert task.status == TaskStatus.PROVISIONING, "heartbeat must not mutate status"
    assert task.latest_status_message == "PRE_CLAIM_FAILED: token validation rate-limited"
    assert task.last_heartbeat is not None


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
async def test_agent_claim_includes_project_metadata(client: AsyncClient, db_session):
    """
    Tests that the claim response includes project_metadata with build_config.
    Agents need this to adapt lint/build/test behavior to different project structures.
    """
    project = Project(
        name="Build Config Test",
        user_id="test-user-123",
        github_owner="acme",
        github_repo="myapp",
        build_config={"directories": [{"path": "frontend", "type": "node"}], "test_command": "pytest"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(project_id=project.id, payloads=[{"type": "create", "prompt": "do something"}], vm_instance_id="agent-vm-meta-1")
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-meta-1")
    headers = {"x-agent-token": token}
    resp = await client.post("/api/agent/claim", headers=headers)

    assert resp.status_code == 200
    data = resp.json()
    assert "project_metadata" in data
    pm = data["project_metadata"]
    assert pm["project_id"] == str(project.id)
    assert pm["project_name"] == "Build Config Test"
    assert pm["github_owner"] == "acme"
    assert pm["github_repo"] == "myapp"
    assert pm["is_self_repo"] is False
    assert pm["build_config"]["test_command"] == "pytest"


@pytest.mark.asyncio
async def test_agent_claim_project_metadata_empty_build_config(client: AsyncClient, db_session):
    """
    Tests that agents get an empty build_config dict (not an error) when the project
    has no build_config configured.
    """
    project = Project(name="No Build Config Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(project_id=project.id, payloads=[{"type": "create", "prompt": "do something"}], vm_instance_id="agent-vm-meta-2")
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-meta-2")
    headers = {"x-agent-token": token}
    resp = await client.post("/api/agent/claim", headers=headers)

    assert resp.status_code == 200
    data = resp.json()
    assert "project_metadata" in data
    assert data["project_metadata"]["build_config"] == {}


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


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_claim_playbook_demotes_ineffective_entries(client: AsyncClient, db_session):
    """Entries with reference_count>=10 and confirmed_count==0 are sorted after effective entries."""
    from sqlalchemy.orm.attributes import flag_modified

    project = Project(name="Playbook Demotion Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    project.design_doc = {
        # Ineffective: high reference count, zero confirmations — should be demoted
        "playbook/zzz-ineffective": {
            "content": "ineffective guidance",
            "source_task_id": "seed",
            "updated_at": "2026-01-01T00:00:00",
            "confirmed_count": 0,
            "reference_count": 15,
        },
        # Effective: has confirmations — should sort before the ineffective one
        "playbook/aaa-effective": {
            "content": "effective guidance",
            "source_task_id": "seed",
            "updated_at": "2026-01-01T00:00:00",
            "confirmed_count": 3,
            "reference_count": 10,
        },
    }
    flag_modified(project, "design_doc")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "Do work"}],
        vm_instance_id="agent-vm-demotion-test",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-demotion-test")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200

    data = resp.json()
    playbook = data["playbook"]
    assert "zzz-ineffective" in playbook
    assert "aaa-effective" in playbook

    # The effective entry must appear before the ineffective one in insertion order
    keys = list(playbook.keys())
    assert keys.index("aaa-effective") < keys.index("zzz-ineffective"), (
        f"Expected effective entry before ineffective, got order: {keys}"
    )


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_claim_injects_direction_context_for_create_task(client: AsyncClient, db_session):
    """Claim endpoint injects direction_context for create/fix/refactor tasks when direction_id is set."""
    from sqlalchemy.orm.attributes import flag_modified

    direction_id = str(uuid.uuid4())
    research_context = (
        "## Overview\nImprove auth flow for users.\n\n"
        "## Failed Approaches\n"
        "- JWT in cookies caused CSRF issues\n"
        "- Session-based auth had scaling problems\n"
        "## Next Steps\nTry token refresh pattern.\n"
    )
    project = Project(name="Direction Context Test", user_id="test-user-123")
    project.directions = [
        {
            "id": direction_id,
            "title": "Improve auth flow",
            "priority": "high",
            "research_context": research_context,
            "research_history": [
                {"tasks_created": 3, "prs_merged": 2, "prs_rejected": 1},
                {"tasks_created": 4, "prs_merged": 1, "prs_rejected": 0},
            ],
        }
    ]
    flag_modified(project, "directions")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "Implement auth improvements"}],
        vm_instance_id="agent-vm-dir-ctx-create",
        context={"direction_id": direction_id},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-dir-ctx-create")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200

    data = resp.json()
    assert "direction_context" in data, f"Expected direction_context in claim response, got keys: {list(data.keys())}"
    dc = data["direction_context"]
    assert dc["title"] == "Improve auth flow"
    assert dc["priority"] == "high"
    assert "JWT in cookies caused CSRF issues" in dc["failed_approaches"]
    assert "Session-based auth had scaling problems" in dc["failed_approaches"]
    # recent_outcomes: last 2 history entries with tasks_created > 0
    assert len(dc["recent_outcomes"]) == 2
    assert dc["recent_outcomes"][0]["prs_merged"] == 2
    assert dc["recent_outcomes"][1]["prs_merged"] == 1


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_claim_does_not_inject_direction_context_for_review_task(client: AsyncClient, db_session):
    """Claim endpoint does NOT inject direction_context for review tasks (only create/fix/refactor)."""
    from sqlalchemy.orm.attributes import flag_modified

    direction_id = str(uuid.uuid4())
    project = Project(name="Direction Context Review Test", user_id="test-user-123")
    project.directions = [
        {
            "id": direction_id,
            "title": "Fix bug in login",
            "priority": "medium",
            "research_context": "## Failed Approaches\n- Tried patching the validator\n",
            "research_history": [],
        }
    ]
    flag_modified(project, "directions")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "review", "pr_number": 42, "branch": "fix/login"}],
        vm_instance_id="agent-vm-dir-ctx-review",
        context={"direction_id": direction_id},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-dir-ctx-review")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200

    data = resp.json()
    assert "direction_context" not in data, (
        f"direction_context should be absent for review tasks, got: {data.get('direction_context')}"
    )


@pytest.mark.asyncio
async def test_compute_failure_stage_pattern_code_error_dominant():
    """Returns code_error pattern when 60%+ of non-infra failures are code errors."""
    from app.routers.agent import _compute_failure_stage_pattern

    history = [
        {"stage_dropout": {"tasks_failed_code_error": 3, "tasks_failed_infra": 1, "tasks_required_approval": 1, "tasks_merged_staging_only": 0}},
        {"stage_dropout": {"tasks_failed_code_error": 2, "tasks_failed_infra": 0, "tasks_required_approval": 0, "tasks_merged_staging_only": 0}},
        {"stage_dropout": {"tasks_failed_code_error": 1, "tasks_failed_infra": 2, "tasks_required_approval": 0, "tasks_merged_staging_only": 0}},
    ]
    # Non-infra totals: code_error=6, approval=1, total=7 → rate=6/7≈0.857
    result = _compute_failure_stage_pattern(history)
    assert result is not None
    assert result["stage"] == "code_error"
    assert result["rate"] >= 0.6
    assert "test" in result["message"].lower() or "code" in result["message"].lower()


@pytest.mark.asyncio
async def test_compute_failure_stage_pattern_approval_dominant():
    """Returns approval pattern when 60%+ of non-infra failures are approval stalls."""
    from app.routers.agent import _compute_failure_stage_pattern

    history = [
        {"stage_dropout": {"tasks_failed_code_error": 0, "tasks_failed_infra": 1, "tasks_required_approval": 4, "tasks_merged_staging_only": 0}},
        {"stage_dropout": {"tasks_failed_code_error": 1, "tasks_failed_infra": 0, "tasks_required_approval": 3, "tasks_merged_staging_only": 0}},
    ]
    # Non-infra totals: code_error=1, approval=7, total=8 → rate=7/8=0.875
    result = _compute_failure_stage_pattern(history)
    assert result is not None
    assert result["stage"] == "approval"
    assert result["rate"] >= 0.6
    assert "approval" in result["message"].lower() or "review" in result["message"].lower()


@pytest.mark.asyncio
async def test_compute_failure_stage_pattern_no_pattern():
    """Returns None when no dominant pattern or insufficient data."""
    from app.routers.agent import _compute_failure_stage_pattern

    # Insufficient data (total < 3)
    assert _compute_failure_stage_pattern([
        {"stage_dropout": {"tasks_failed_code_error": 1, "tasks_failed_infra": 0, "tasks_required_approval": 1}},
    ]) is None

    # No clear pattern (roughly even split)
    mixed_history = [
        {"stage_dropout": {"tasks_failed_code_error": 2, "tasks_failed_infra": 0, "tasks_required_approval": 2}},
        {"stage_dropout": {"tasks_failed_code_error": 1, "tasks_failed_infra": 0, "tasks_required_approval": 1}},
    ]
    # code_error=3, approval=3, total=6 → neither reaches 60%
    assert _compute_failure_stage_pattern(mixed_history) is None

    # Empty history
    assert _compute_failure_stage_pattern([]) is None


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_claim_injects_failure_stage_pattern_in_direction_context(client: AsyncClient, db_session):
    """Claim endpoint injects failure_stage_pattern into direction_context when pattern is clear."""
    from sqlalchemy.orm.attributes import flag_modified

    direction_id = str(uuid.uuid4())
    project = Project(name="Stage Dropout Pattern Test", user_id="test-user-123")
    project.directions = [
        {
            "id": direction_id,
            "title": "Fix test flakiness",
            "priority": "high",
            "research_context": "",
            "research_history": [
                {"tasks_created": 4, "prs_merged": 1, "stage_dropout": {"tasks_failed_code_error": 3, "tasks_failed_infra": 0, "tasks_required_approval": 0, "tasks_merged_staging_only": 0}},
                {"tasks_created": 3, "prs_merged": 1, "stage_dropout": {"tasks_failed_code_error": 2, "tasks_failed_infra": 1, "tasks_required_approval": 0, "tasks_merged_staging_only": 0}},
                {"tasks_created": 5, "prs_merged": 2, "stage_dropout": {"tasks_failed_code_error": 3, "tasks_failed_infra": 0, "tasks_required_approval": 0, "tasks_merged_staging_only": 0}},
            ],
        }
    ]
    flag_modified(project, "directions")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "Fix the flaky tests"}],
        vm_instance_id="agent-vm-stage-dropout",
        context={"direction_id": direction_id},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-stage-dropout")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200

    data = resp.json()
    assert "direction_context" in data
    dc = data["direction_context"]
    assert "failure_stage_pattern" in dc
    fsp = dc["failure_stage_pattern"]
    assert fsp is not None
    assert fsp["stage"] == "code_error"
    assert "message" in fsp
    assert fsp["rate"] >= 0.6


@pytest.mark.asyncio
async def test_claim_injects_cochange_files_for_create_task(client: AsyncClient, db_session):
    """Claim endpoint injects cochange_files in project_context for create tasks when related_files match index."""
    from sqlalchemy.orm.attributes import flag_modified

    cochange_index = {
        "backend/app/models.py": {"backend/app/schemas.py": 3.0, "backend/testing/test_models.py": 2.5},
        "backend/app/schemas.py": {"backend/app/models.py": 3.0},
    }
    project = Project(name="Cochange Test", user_id="test-user-123", design_doc={"cochange_index": cochange_index})
    flag_modified(project, "design_doc")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "Add new model field"}],
        vm_instance_id="agent-vm-cochange-create",
        context={"related_files": ["backend/app/models.py"]},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-cochange-create")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200

    data = resp.json()
    project_ctx = data.get("project_context", {})
    assert "cochange_files" in project_ctx, f"Expected cochange_files in project_context, got: {list(project_ctx.keys())}"
    cochange_files = project_ctx["cochange_files"]
    assert "backend/app/schemas.py" in cochange_files
    assert "backend/testing/test_models.py" in cochange_files
    # related_files themselves should not appear in cochange_files
    assert "backend/app/models.py" not in cochange_files


@pytest.mark.asyncio
async def test_claim_does_not_inject_cochange_files_for_review_task(client: AsyncClient, db_session):
    """Claim endpoint does NOT inject cochange_files for review tasks."""
    from sqlalchemy.orm.attributes import flag_modified

    cochange_index = {
        "backend/app/models.py": {"backend/app/schemas.py": 3.0},
    }
    project = Project(name="Cochange Review Test", user_id="test-user-123", design_doc={"cochange_index": cochange_index})
    flag_modified(project, "design_doc")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "review", "pr_number": 99, "branch": "fix/something"}],
        vm_instance_id="agent-vm-cochange-review",
        context={"related_files": ["backend/app/models.py"]},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-cochange-review")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200

    data = resp.json()
    project_ctx = data.get("project_context", {})
    assert "cochange_files" not in project_ctx, (
        f"cochange_files should be absent for review tasks, got: {project_ctx.get('cochange_files')}"
    )


@pytest.mark.asyncio
async def test_claim_injects_module_feedback_warnings_for_create_task(client: AsyncClient, db_session):
    """Claim endpoint injects module_feedback_warnings for create tasks when related_files have profiles."""
    from sqlalchemy.orm.attributes import flag_modified

    module_feedback = {
        "backend/app/orchestrator.py": {
            "test-coverage": 6,
            "error-handling": 2,
            "total": 8,
            "last_updated": "2026-01-01T00:00:00",
        },
        "backend/app/models.py": {
            "type-safety": 1,
            "total": 2,  # below threshold of 3 — should be excluded
            "last_updated": "2026-01-01T00:00:00",
        },
    }
    project = Project(
        name="Module Feedback Test",
        user_id="test-user-123",
        design_doc={"module_feedback": module_feedback},
    )
    flag_modified(project, "design_doc")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "Add feature"}],
        vm_instance_id="agent-vm-module-feedback",
        context={"related_files": ["backend/app/orchestrator.py", "backend/app/models.py"]},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-module-feedback")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200

    data = resp.json()
    project_ctx = data.get("project_context", {})
    assert "module_feedback_warnings" in project_ctx, (
        f"Expected module_feedback_warnings in project_context, got: {list(project_ctx.keys())}"
    )
    warnings = project_ctx["module_feedback_warnings"]
    assert len(warnings) == 1, f"Expected 1 warning (models.py below threshold), got {len(warnings)}"
    w = warnings[0]
    assert w["file"] == "backend/app/orchestrator.py"
    assert "test-coverage" in w["top_categories"]
    assert w["sample_size"] == 8
    # models.py should not appear — total < 3
    files_in_warnings = [x["file"] for x in warnings]
    assert "backend/app/models.py" not in files_in_warnings


@pytest.mark.asyncio
async def test_claim_does_not_inject_module_feedback_warnings_for_review_task(client: AsyncClient, db_session):
    """Claim endpoint does NOT inject module_feedback_warnings for review tasks."""
    from sqlalchemy.orm.attributes import flag_modified

    module_feedback = {
        "backend/app/orchestrator.py": {
            "test-coverage": 5,
            "total": 5,
            "last_updated": "2026-01-01T00:00:00",
        },
    }
    project = Project(
        name="Module Feedback Review Test",
        user_id="test-user-123",
        design_doc={"module_feedback": module_feedback},
    )
    flag_modified(project, "design_doc")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "review", "pr_number": 42, "branch": "fix/something"}],
        vm_instance_id="agent-vm-module-feedback-review",
        context={"related_files": ["backend/app/orchestrator.py"]},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-module-feedback-review")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200

    data = resp.json()
    project_ctx = data.get("project_context", {})
    assert "module_feedback_warnings" not in project_ctx, (
        f"module_feedback_warnings should be absent for review tasks, got: {project_ctx.get('module_feedback_warnings')}"
    )


@pytest.mark.asyncio
async def test_safe_ws_close_swallows_double_close_runtime_error():
    """Production cascade: receive_json raised WebSocketDisconnect (already
    sent a close frame), then our explicit close tries to send a second one
    and starlette raises RuntimeError. _safe_ws_close must swallow that
    specific race without breaking."""
    from unittest.mock import AsyncMock
    from app.routers.projects import _safe_ws_close
    ws = AsyncMock()
    ws.close.side_effect = RuntimeError(
        'Unexpected ASGI message "websocket.close", after sending "websocket.close".'
    )
    await _safe_ws_close(ws, code=1008, reason="Authentication failed")
    ws.close.assert_awaited_once_with(code=1008, reason="Authentication failed")


@pytest.mark.asyncio
async def test_safe_ws_close_reraises_other_runtime_errors():
    """Only the double-close race is swallowed; other RuntimeErrors propagate
    so real bugs don't get hidden."""
    from unittest.mock import AsyncMock
    from app.routers.projects import _safe_ws_close
    ws = AsyncMock()
    ws.close.side_effect = RuntimeError("connection closed during handshake")
    with pytest.raises(RuntimeError, match="handshake"):
        await _safe_ws_close(ws, code=1008, reason="boom")


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
    mocker.patch("app.max_plan_pool.load_max_plan_pool", new_callable=AsyncMock)
    mocker.patch.object(main_module, "_verify_schema_version", new_callable=AsyncMock)
    mocker.patch.object(lifecycle_module, "_seed_initial_admin", new_callable=AsyncMock)
    mocker.patch.object(main_module, "_init_deployment_state", new_callable=AsyncMock)
    # Background task coroutines must also be no-ops so the task loop doesn't
    # block the test event loop or attempt DB queries
    mocker.patch("app.reaper.reaper_service", new_callable=AsyncMock)
    mocker.patch("app.services.gcp_cleanup_service", new_callable=AsyncMock)
    mocker.patch("app.services.secret_reconciliation_service", new_callable=AsyncMock)
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
    mocker.patch("app.broadcast.broadcast_to_project", return_value=None)

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
            # Pre-seed provision_failures to verify it gets cleared on graceful requeue —
            # otherwise a single provision hiccup on a previously-successful task would
            # immediately exhaust the 3-retry budget on the next dispatch.
            context={"_api_failure_retries": retry_num - 1, "provision_failures": 2},
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
        assert "provision_failures" not in ctx, (
            f"retry {retry_num}: provision_failures should be cleared on graceful requeue"
        )

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


@pytest.mark.asyncio
async def test_agent_claim_non_research_includes_pipeline_health(client: AsyncClient, db_session, mocker):
    """Non-gated task type (docs) receives pipeline_health via the else-branch."""
    project = Project(name="Pipeline Health Test", user_id="test-user-ph")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "docs", "cmd": "write docs"}],
        vm_instance_id="agent-vm-ph-1",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    fake_health = {
        "success_rate": 0.9,
        "trend": "improving",
        "first_attempt_success_rate": {
            "overall": 0.85,
            "trend": "stable",
            "sample_size": 20,
            "by_type": {"create": 0.85, "fix": 0.62},
        },
        "failure_distribution": {},
        "direction_stats": [],
    }
    mocker.patch(
        "app.routers.admin_analytics.compute_pipeline_health_summary",
        return_value=fake_health,
    )

    token = create_agent_token(str(task.id), "agent-vm-ph-1")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200
    data = resp.json()
    assert "project_context" in data
    assert data["project_context"]["pipeline_health"] is not None
    assert data["project_context"]["pipeline_health"]["first_attempt_success_rate"]["by_type"]["create"] == 0.85


@pytest.mark.asyncio
async def test_playbook_claim_filters_by_task_type(client: AsyncClient, db_session):
    """
    Claim endpoint filters playbook entries by applicable_types:
    - entry with applicable_types=["create","fix"] is excluded for 'research' tasks
    - entry with applicable_types=None is excluded for 'research' (not in _PLAYBOOK_DEFAULT_TYPES)
    - entry with applicable_types=None IS included for 'fix' (in _PLAYBOOK_DEFAULT_TYPES)
    """
    project = Project(name="Playbook Filter Test", user_id="test-user-pbf")
    project.design_doc = {
        "playbook/write-only": {
            "content": "only for write tasks",
            "applicable_types": ["create", "fix"],
            "source_task_id": None,
            "updated_at": "2026-01-01T00:00:00",
            "confirmed_count": 0,
        },
        "playbook/untagged": {
            "content": "untagged operational entry",
            "source_task_id": None,
            "updated_at": "2026-01-01T00:00:00",
            "confirmed_count": 0,
        },
    }
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # research task: both entries should be excluded
    task_research = Task(
        project_id=project.id,
        payloads=[{"type": "research", "prompt": "investigate something"}],
        vm_instance_id="agent-vm-pbf-1",
    )
    db_session.add(task_research)
    await db_session.commit()
    await db_session.refresh(task_research)

    token = create_agent_token(str(task_research.id), "agent-vm-pbf-1")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200
    data = resp.json()

    # No playbook entries should be present for research task
    playbook = data.get("playbook", {})
    assert "write-only" not in playbook
    assert "untagged" not in playbook

    # fix task: untagged entry should be included
    task_fix = Task(
        project_id=project.id,
        payloads=[{"type": "fix", "prompt": "fix the bug"}],
        vm_instance_id="agent-vm-pbf-2",
    )
    db_session.add(task_fix)
    await db_session.commit()
    await db_session.refresh(task_fix)

    token2 = create_agent_token(str(task_fix.id), "agent-vm-pbf-2")
    resp2 = await client.post("/api/agent/claim", headers={"x-agent-token": token2})
    assert resp2.status_code == 200
    data2 = resp2.json()

    assert "playbook" in data2
    # write-only entry has applicable_types=["create","fix"] so included for fix
    assert "write-only" in data2["playbook"]
    # untagged entry is included for fix (in _PLAYBOOK_DEFAULT_TYPES)
    assert "untagged" in data2["playbook"]
    assert data2["playbook"]["untagged"] == "untagged operational entry"


@pytest.mark.asyncio
async def test_playbook_claim_includes_entry_when_type_matches(client: AsyncClient, db_session):
    """
    Claim endpoint includes playbook entries when task type is in applicable_types.
    """
    project = Project(name="Playbook Match Test", user_id="test-user-pbm")
    project.design_doc = {
        "playbook/build-hint": {
            "content": "build instruction",
            "applicable_types": ["create", "fix", "refactor"],
            "source_task_id": None,
            "updated_at": "2026-01-01T00:00:00",
            "confirmed_count": 0,
        },
    }
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "fix", "prompt": "fix a bug"}],
        vm_instance_id="agent-vm-pbm-1",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-pbm-1")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200
    data = resp.json()

    assert "playbook" in data
    assert "build-hint" in data["playbook"]
    assert data["playbook"]["build-hint"] == "build instruction"


@pytest.mark.asyncio
async def test_playbook_claim_increments_reference_count(client: AsyncClient, db_session):
    """
    After claim, reference_count is incremented and last_referenced_at is set
    for served playbook entries. Non-served entries (filtered by applicable_types) are NOT incremented.
    """
    project = Project(name="Playbook RefCount Test", user_id="test-user-prc")
    project.design_doc = {
        "playbook/served-entry": {
            "content": "served content",
            "applicable_types": ["create"],
            "source_task_id": None,
            "updated_at": "2026-01-01T00:00:00",
            "confirmed_count": 0,
            "reference_count": 2,
        },
        "playbook/unserved-entry": {
            "content": "write-only content",
            "applicable_types": ["review"],  # won't match 'create' task
            "source_task_id": None,
            "updated_at": "2026-01-01T00:00:00",
            "confirmed_count": 0,
            "reference_count": 0,
        },
    }
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "build something"}],
        vm_instance_id="agent-vm-prc-1",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-prc-1")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200
    data = resp.json()
    assert "playbook" in data
    assert "served-entry" in data["playbook"]
    assert "unserved-entry" not in data["playbook"]

    # Verify reference_count incremented for served entry
    await db_session.refresh(project)
    served = project.design_doc["playbook/served-entry"]
    assert served["reference_count"] == 3
    assert served["last_referenced_at"] is not None

    # Verify unserved entry unchanged
    unserved = project.design_doc["playbook/unserved-entry"]
    assert unserved["reference_count"] == 0
    assert "last_referenced_at" not in unserved


@pytest.mark.asyncio
async def test_playbook_claim_ordering_confirmed_count(client: AsyncClient, db_session):
    """
    Playbook entries returned by claim are sorted by confirmed_count descending,
    then reference_count descending as tiebreaker. Most-validated entries come first.
    """
    project = Project(name="Playbook Order Test", user_id="test-user-order")
    project.design_doc = {
        "playbook/low-count": {
            "content": "low confirmed",
            "source_task_id": None,
            "updated_at": "2026-01-01T00:00:00",
            "confirmed_count": 1,
            "reference_count": 10,
        },
        "playbook/high-count": {
            "content": "high confirmed",
            "source_task_id": None,
            "updated_at": "2026-01-01T00:00:00",
            "confirmed_count": 5,
            "reference_count": 2,
        },
        "playbook/tiebreak": {
            "content": "same confirmed, more references",
            "source_task_id": None,
            "updated_at": "2026-01-01T00:00:00",
            "confirmed_count": 5,
            "reference_count": 8,
        },
    }
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "build something"}],
        vm_instance_id="agent-vm-order-1",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-order-1")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200
    data = resp.json()

    assert "playbook" in data
    keys = list(data["playbook"].keys())
    # confirmed_count=5 entries first; among those, reference_count=8 before reference_count=2
    assert keys.index("tiebreak") < keys.index("high-count")
    assert keys.index("high-count") < keys.index("low-count")


@pytest.mark.asyncio
async def test_playbook_claim_age_penalty_deprioritises_old_entries(client: AsyncClient, db_session):
    """
    Old entries (>30 days, confirmed_count < 3) are deprioritised in claim ordering.
    A recent unconfirmed entry should appear before an older unconfirmed entry.
    """
    old_date = (datetime.utcnow() - timedelta(days=40)).isoformat()
    recent_date = datetime.utcnow().isoformat()

    project = Project(name="Age Penalty Test", user_id="test-user-age-penalty")
    project.design_doc = {
        "playbook/old-unconfirmed": {
            "content": "old tip with few confirmations",
            "confirmed_count": 0,
            "reference_count": 0,
            "updated_at": old_date,
        },
        "playbook/recent-unconfirmed": {
            "content": "recent tip with few confirmations",
            "confirmed_count": 0,
            "reference_count": 0,
            "updated_at": recent_date,
        },
    }
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "build something"}],
        vm_instance_id="agent-vm-age-penalty-1",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-age-penalty-1")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200
    data = resp.json()

    assert "playbook" in data
    keys = list(data["playbook"].keys())
    # Recent entry should appear before the age-penalised old entry
    assert keys.index("recent-unconfirmed") < keys.index("old-unconfirmed"), (
        f"Expected recent-unconfirmed before old-unconfirmed, got order: {keys}"
    )


@pytest.mark.asyncio
async def test_playbook_claim_stale_file_refs_deprioritised(client: AsyncClient, db_session):
    """
    Entries with stale_file_refs=True are deprioritised — a clean entry with the same
    confirmed_count should come before a stale entry.
    """
    now = datetime.utcnow().isoformat()

    project = Project(name="Stale Refs Test", user_id="test-user-stale-refs")
    project.design_doc = {
        "playbook/stale-entry": {
            "content": "tip referencing a deleted file",
            "confirmed_count": 3,
            "reference_count": 5,
            "updated_at": now,
            "stale_file_refs": True,
        },
        "playbook/clean-entry": {
            "content": "tip with valid file refs",
            "confirmed_count": 3,
            "reference_count": 5,
            "updated_at": now,
            "stale_file_refs": False,
        },
    }
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "build something"}],
        vm_instance_id="agent-vm-stale-refs-1",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-stale-refs-1")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200
    data = resp.json()

    assert "playbook" in data
    keys = list(data["playbook"].keys())
    # Clean entry should come before stale entry
    assert keys.index("clean-entry") < keys.index("stale-entry"), (
        f"Expected clean-entry before stale-entry, got order: {keys}"
    )


@pytest.mark.asyncio
async def test_playbook_claim_old_confirmed_entry_not_penalised(client: AsyncClient, db_session):
    """
    An old entry with confirmed_count >= 3 should NOT receive the age penalty.
    It should still be ordered before a less-confirmed recent entry.
    """
    old_date = (datetime.utcnow() - timedelta(days=40)).isoformat()
    recent_date = datetime.utcnow().isoformat()

    project = Project(name="Confirmed Old Entry Test", user_id="test-user-conf-old")
    project.design_doc = {
        "playbook/old-confirmed": {
            "content": "old but well-confirmed tip",
            "confirmed_count": 5,
            "reference_count": 10,
            "updated_at": old_date,
        },
        "playbook/recent-low-confirmed": {
            "content": "recent tip with low confirmations",
            "confirmed_count": 1,
            "reference_count": 1,
            "updated_at": recent_date,
        },
    }
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "build something"}],
        vm_instance_id="agent-vm-conf-old-1",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-conf-old-1")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200
    data = resp.json()

    assert "playbook" in data
    keys = list(data["playbook"].keys())
    # Old but confirmed entry (no age penalty) should come before less-confirmed recent entry
    assert keys.index("old-confirmed") < keys.index("recent-low-confirmed"), (
        f"Expected old-confirmed before recent-low-confirmed, got order: {keys}"
    )


@pytest.mark.asyncio
async def test_playbook_confirmed_count_incremented_on_first_attempt_success(
    client: AsyncClient, db_session, mocker
):
    """
    When a task completes on first attempt and the project has playbook entries,
    confirmed_count is incremented for entries applicable to that task type.
    Entries not applicable to the task type are NOT incremented.
    """
    mocker.patch("app.main.delete_agent_vm", return_value=True)

    project = Project(name="Playbook Confirm Test", user_id="test-user-confirm")
    project.design_doc = {
        "playbook/create-entry": {
            "content": "create guidance",
            "applicable_types": ["create", "fix"],
            "source_task_id": None,
            "updated_at": "2026-01-01T00:00:00",
            "confirmed_count": 2,
        },
        "playbook/review-only": {
            "content": "review guidance",
            "applicable_types": ["review"],
            "source_task_id": None,
            "updated_at": "2026-01-01T00:00:00",
            "confirmed_count": 0,
        },
    }
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "do something"}],
        vm_instance_id="agent-vm-confirm-1",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=0,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-confirm-1")
    resp = await client.post(
        "/api/agent/status",
        json={"task_id": str(task.id), "status": "COMPLETED", "logs": "done"},
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 200

    await db_session.refresh(project)
    # create-entry is applicable to 'create' tasks — confirmed_count should be incremented
    assert project.design_doc["playbook/create-entry"]["confirmed_count"] == 3
    # review-only is NOT applicable to 'create' tasks — confirmed_count unchanged
    assert project.design_doc["playbook/review-only"]["confirmed_count"] == 0


@pytest.mark.asyncio
async def test_playbook_confirmed_count_not_incremented_on_retry_success(
    client: AsyncClient, db_session, mocker
):
    """
    When a task completes after retries (attempt_count >= 2), confirmed_count
    is NOT incremented — only first-attempt successes count as confirmation.
    """
    mocker.patch("app.main.delete_agent_vm", return_value=True)

    project = Project(name="Playbook No Confirm Retry", user_id="test-user-no-confirm")
    project.design_doc = {
        "playbook/some-entry": {
            "content": "some guidance",
            "source_task_id": None,
            "updated_at": "2026-01-01T00:00:00",
            "confirmed_count": 1,
        },
    }
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "do something"}],
        vm_instance_id="agent-vm-no-confirm-1",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=2,  # not first attempt
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-no-confirm-1")
    resp = await client.post(
        "/api/agent/status",
        json={"task_id": str(task.id), "status": "COMPLETED", "logs": "done"},
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 200

    await db_session.refresh(project)
    # attempt_count=2 → _first_attempt_succeeded=False → confirmed_count unchanged
    assert project.design_doc["playbook/some-entry"]["confirmed_count"] == 1


@pytest.mark.asyncio
async def test_playbook_admin_list_stale_flag(client: AsyncClient, db_session):
    """
    Admin playbook list endpoint returns reference_count, last_referenced_at, and stale flag.
    Stale is True if no reference in 30+ days; False if recently referenced.
    Auth is mocked by conftest to test-user-123 (admin).
    """
    from app.models import ProjectMember

    project = Project(name="Playbook Stale Test", user_id="test-user-123")
    project.design_doc = {
        "playbook/fresh-entry": {
            "content": "fresh content",
            "updated_at": "2026-04-10T00:00:00",
            "confirmed_count": 1,
            "reference_count": 5,
            "last_referenced_at": "2026-04-13T00:00:00",  # 1 day ago — not stale
        },
        "playbook/stale-entry": {
            "content": "old content",
            "updated_at": "2025-12-01T00:00:00",  # >30 days ago, never referenced
            "confirmed_count": 0,
            "reference_count": 0,
        },
        "playbook/stale-ref-entry": {
            "content": "referenced long ago",
            "updated_at": "2025-12-01T00:00:00",
            "confirmed_count": 0,
            "reference_count": 3,
            "last_referenced_at": "2025-12-15T00:00:00",  # >30 days ago
        },
    }
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Add project member matching the mocked current_user (test-user-123)
    member = ProjectMember(project_id=project.id, user_id="test-user-123", role="owner")
    db_session.add(member)
    await db_session.commit()

    # No auth header needed — conftest mocks get_current_user to test-user-123
    resp = await client.get(f"/api/admin/projects/{project.id}/playbook")
    assert resp.status_code == 200
    entries = resp.json()["entries"]

    fresh = entries["fresh-entry"]
    assert fresh["reference_count"] == 5
    assert fresh["last_referenced_at"] == "2026-04-13T00:00:00"
    assert fresh["stale"] is False

    stale_entry = entries["stale-entry"]
    assert stale_entry["reference_count"] == 0
    assert stale_entry["last_referenced_at"] is None
    assert stale_entry["stale"] is True

    stale_ref = entries["stale-ref-entry"]
    assert stale_ref["reference_count"] == 3
    assert stale_ref["stale"] is True

    stale_ref = entries["stale-ref-entry"]
    assert stale_ref["reference_count"] == 3
    assert stale_ref["stale"] is True


@pytest.mark.asyncio
async def test_agent_create_pr_recreates_when_cached_pr_closed(client: AsyncClient, db_session, mocker):
    """If the cached PR artifact points at a closed PR (e.g. branch was deleted
    on requeue, auto-closing the PR), the endpoint must NOT return the stale URL.
    It should delete the stale artifact and create a fresh PR.

    Regression for task 512c4514: the idempotency check returned a closed PR's
    URL to the agent, which then reported COMPLETED on a phantom PR.
    """
    from unittest.mock import AsyncMock

    project = Project(name="Stale PR Recreate", user_id="test-user-stale-pr")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "do thing"}],
        vm_instance_id="agent-vm-stale-pr",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    db_session.add(Artifact(
        task_id=task.id,
        artifact_type=ArtifactType.PULL_REQUEST,
        pr_url="https://github.com/foo/bar/pull/42",
    ))
    await db_session.commit()

    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new=AsyncMock(return_value=("tok", "foo", "bar")),
    )
    mocker.patch(
        "app.routers.agent.get_pull_request",
        new=AsyncMock(return_value={"state": "closed", "merged": False}),
    )
    mock_create = AsyncMock(return_value={"html_url": "https://github.com/foo/bar/pull/99", "number": 99})
    mocker.patch("app.routers.agent.create_pull_request", new=mock_create)

    token = create_agent_token(str(task.id), "agent-vm-stale-pr")
    headers = {"x-agent-token": token}
    resp = await client.post(
        "/api/agent/github/pull-requests",
        json={
            "task_id": str(task.id),
            "owner": "foo",
            "repo": "bar",
            "title": "fresh PR",
            "head": "blueprint/task-deadbeef",
            "base": "main",
            "body": "body",
        },
        headers=headers,
    )

    assert resp.status_code == 200
    assert resp.json()["data"]["html_url"] == "https://github.com/foo/bar/pull/99"
    mock_create.assert_called_once()

    artifacts = (await db_session.execute(
        select(Artifact).where(Artifact.task_id == task.id, Artifact.artifact_type == ArtifactType.PULL_REQUEST)
    )).scalars().all()
    pr_urls = sorted(a.pr_url for a in artifacts)
    assert "https://github.com/foo/bar/pull/99" in pr_urls
    assert "https://github.com/foo/bar/pull/42" not in pr_urls, (
        "Stale closed-PR artifact should have been removed"
    )


@pytest.mark.asyncio
async def test_agent_create_pr_returns_cached_when_open(client: AsyncClient, db_session, mocker):
    """If the cached PR artifact points at an OPEN PR, the endpoint should
    short-circuit and return its URL without calling create_pull_request."""
    from unittest.mock import AsyncMock

    project = Project(name="Open PR Cached", user_id="test-user-open-pr")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "do thing"}],
        vm_instance_id="agent-vm-open-pr",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    db_session.add(Artifact(
        task_id=task.id,
        artifact_type=ArtifactType.PULL_REQUEST,
        pr_url="https://github.com/foo/bar/pull/7",
    ))
    await db_session.commit()

    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new=AsyncMock(return_value=("tok", "foo", "bar")),
    )
    mocker.patch(
        "app.routers.agent.get_pull_request",
        new=AsyncMock(return_value={"state": "open", "merged": False}),
    )
    mock_create = AsyncMock()
    mocker.patch("app.routers.agent.create_pull_request", new=mock_create)

    token = create_agent_token(str(task.id), "agent-vm-open-pr")
    headers = {"x-agent-token": token}
    resp = await client.post(
        "/api/agent/github/pull-requests",
        json={
            "task_id": str(task.id),
            "owner": "foo",
            "repo": "bar",
            "title": "should not be created",
            "head": "blueprint/task-cafef00d",
            "base": "main",
            "body": "body",
        },
        headers=headers,
    )

    assert resp.status_code == 200
    assert resp.json()["data"]["html_url"] == "https://github.com/foo/bar/pull/7"
    mock_create.assert_not_called()


@pytest.mark.asyncio
async def test_rate_limit_requeue_clears_terminated_cache(client: AsyncClient, db_session, mocker):
    """
    Regression test for the 401 cascade bug.

    When an agent reports FAILED with a rate-limit failure, agent_status.py:359
    sets task.status = FAILED, firing the SQLAlchemy set event that adds task.id
    to _terminated_task_ids. Then the RATE_LIMIT requeue path overwrites the
    status back to QUEUED. Without the fix, the cache entry persists — every
    subsequent claim for the requeued task returns 401 until the 9-hour TTL
    expires.

    This test pins the fix: after a rate-limit requeue, _terminated_task_ids
    must NOT contain the task_id.
    """
    from app.routers.agent import _terminated_task_ids

    mocker.patch("app.main.delete_agent_vm", return_value=True)
    mocker.patch("app.main._ensure_vm_deleted", new_callable=mocker.AsyncMock)
    mocker.patch("app.broadcast.broadcast_to_project", return_value=None)

    project = Project(name="Rate Limit Requeue Test", user_id="test-user-rl-cache")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"cmd": "test"}],
        vm_instance_id="agent-vm-rl-cache",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    task_id_str = str(task.id)

    # Make sure no stale cache entry exists from a prior test
    _terminated_task_ids.pop(task_id_str, None)

    token = create_agent_token(task_id_str, "agent-vm-rl-cache")
    headers = {"x-agent-token": token}
    payload = {
        "task_id": task_id_str,
        "status": "FAILED",
        "failure_category": "rate_limited",
        "logs": "You've hit your usage limit - View usage",
    }
    resp = await client.post("/api/agent/status", json=payload, headers=headers)
    assert resp.status_code == 200

    await db_session.refresh(task)
    # Task must be requeued, not left in a terminal state
    assert task.status == TaskStatus.QUEUED, (
        f"Rate-limited task should be requeued to QUEUED, got {task.status}"
    )
    # The fix: cache entry must be cleared so the next VM can claim the task
    assert task_id_str not in _terminated_task_ids, (
        f"Task {task_id_str} must not be in _terminated_task_ids after rate-limit "
        f"requeue — entry would block every subsequent claim with 401 until the "
        f"9-hour TTL expires."
    )

    # Clean up to avoid polluting other tests
    _terminated_task_ids.pop(task_id_str, None)


@pytest.mark.asyncio
async def test_completed_terminal_populates_terminated_cache(client: AsyncClient, db_session, mocker):
    """
    Inverse of test_rate_limit_requeue_clears_terminated_cache: a genuinely
    terminal COMPLETED status MUST populate _terminated_task_ids so the agent's
    JWT is revoked and a second process can't reuse it.

    Pins the intended semantics of the SQLAlchemy terminal callback.
    """
    from app.routers.agent import _terminated_task_ids

    mocker.patch("app.main.delete_agent_vm", return_value=True)
    mocker.patch("app.main._ensure_vm_deleted", new_callable=mocker.AsyncMock)
    mocker.patch("app.broadcast.broadcast_to_project", return_value=None)

    project = Project(name="Terminal Cache Test", user_id="test-user-terminal-cache")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"cmd": "test"}],
        vm_instance_id="agent-vm-terminal-cache",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    task_id_str = str(task.id)

    _terminated_task_ids.pop(task_id_str, None)

    token = create_agent_token(task_id_str, "agent-vm-terminal-cache")
    headers = {"x-agent-token": token}
    payload = {
        "task_id": task_id_str,
        "status": "COMPLETED",
        "logs": "task finished",
    }
    resp = await client.post("/api/agent/status", json=payload, headers=headers)
    assert resp.status_code == 200

    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED
    # The SQLAlchemy set event must have populated the cache for this truly
    # terminal transition — otherwise verify_agent_token would accept the
    # agent's old JWT for continued heartbeats.
    assert task_id_str in _terminated_task_ids, (
        f"Task {task_id_str} must be in _terminated_task_ids after COMPLETED so "
        f"verify_agent_token revokes the agent's JWT."
    )

    _terminated_task_ids.pop(task_id_str, None)


@pytest.mark.asyncio
async def test_rate_limit_requeue_allows_subsequent_claim(client: AsyncClient, db_session, mocker):
    """
    End-to-end regression: after a rate-limit requeue, a fresh VM must be able
    to re-claim the task without hitting the 401 cascade. Before the fix, the
    verify_agent_token dependency rejected every request for the requeued
    task because _terminated_task_ids still contained the task_id.

    We exercise the non-/api/agent/status code path (/api/agent/heartbeat) so
    the cache check in verify_agent_token is actually reached.
    """
    from app.routers.agent import _terminated_task_ids

    mocker.patch("app.main.delete_agent_vm", return_value=True)
    mocker.patch("app.main._ensure_vm_deleted", new_callable=mocker.AsyncMock)
    mocker.patch("app.broadcast.broadcast_to_project", return_value=None)

    project = Project(name="Rate Limit E2E Test", user_id="test-user-rl-e2e")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"cmd": "test"}],
        vm_instance_id="agent-vm-rl-e2e-old",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    task_id_str = str(task.id)

    _terminated_task_ids.pop(task_id_str, None)

    # Step 1: old agent reports rate-limit FAILED → task requeued to QUEUED
    old_token = create_agent_token(task_id_str, "agent-vm-rl-e2e-old")
    payload = {
        "task_id": task_id_str,
        "status": "FAILED",
        "failure_category": "rate_limited",
        "logs": "You've hit your usage limit - View usage",
    }
    resp = await client.post(
        "/api/agent/status",
        json=payload,
        headers={"x-agent-token": old_token},
    )
    assert resp.status_code == 200

    await db_session.refresh(task)
    assert task.status == TaskStatus.QUEUED

    # Step 2: orchestrator will later provision a new VM for this task. Simulate
    # that VM's agent heartbeating with a JWT bound to the new vm_instance_id
    # (which agent_status.py rewrote on requeue). The request must NOT be
    # rejected with 401 — that would reproduce the cascade.
    new_vm_id = task.vm_instance_id
    assert new_vm_id != "agent-vm-rl-e2e-old", (
        "Requeue path should rotate vm_instance_id so the next VM has its own identity"
    )
    new_token = create_agent_token(task_id_str, new_vm_id)
    hb_resp = await client.post(
        "/api/agent/heartbeat",
        json={"task_id": task_id_str, "status_message": "new VM started"},
        headers={"x-agent-token": new_token},
    )
    assert hb_resp.status_code != 401, (
        f"Heartbeat from fresh VM after rate-limit requeue must not return 401. "
        f"Got {hb_resp.status_code}: {hb_resp.text}. This is the 401 cascade bug."
    )


@pytest.mark.asyncio
async def test_file_change_details_stored_in_task_context(client: AsyncClient, db_session, mocker):
    """When an agent reports status with file_change_details, the field is stored
    in task.context['_file_change_details'] so _requeue_parent_task can propagate
    it to the retry clone as _previous_file_change_details."""
    from app.routers.agent import _terminated_task_ids

    mocker.patch("app.main.delete_agent_vm", return_value=None)
    mocker.patch("app.main._ensure_vm_deleted", new_callable=mocker.AsyncMock)

    project = Project(name="File Change Details Test", user_id="test-user-fcd")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "add feature"}],
        vm_instance_id="agent-vm-fcd",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    task_id_str = str(task.id)

    _terminated_task_ids.pop(task_id_str, None)

    file_details = [
        {"file": "backend/app/models.py", "additions": 45, "deletions": 12},
        {"file": "backend/testing/test_models.py", "additions": 8, "deletions": 3},
    ]
    token = create_agent_token(str(task.id), "agent-vm-fcd")
    payload = {
        "task_id": str(task.id),
        "status": "FAILED",
        "artifact_type": "ERROR_LOG",
        "logs": "Test suite failed",
        "failure_category": "test_failed",
        "file_change_details": file_details,
    }
    resp = await client.post("/api/agent/status", json=payload, headers={"x-agent-token": token})
    assert resp.status_code == 200

    await db_session.refresh(task)
    ctx = task.context or {}
    assert ctx.get("_file_change_details") == file_details, (
        f"Expected _file_change_details in task.context, got: {ctx}"
    )

    _terminated_task_ids.pop(task_id_str, None)


@pytest.mark.asyncio
async def test_agent_status_pr_artifact_stores_enriched_context(client: AsyncClient, db_session, mocker):
    """COMPLETED+PULL_REQUEST status with diff_summary, self_review_outcome, and
    test_failure_summary stores them in task context as _pr_diff_summary,
    _self_review_outcome, and _test_failure_summary respectively."""
    mocker.patch("app.main.delete_agent_vm", return_value=None)
    mocker.patch("app.main._ensure_vm_deleted", new_callable=mocker.AsyncMock)
    mocker.patch("app.broadcast.broadcast_to_project", return_value=None)

    project = Project(name="Enriched Context PR Test", user_id="test-user-enriched-pr")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "do thing"}],
        vm_instance_id="agent-vm-enriched-pr",
        status=TaskStatus.IN_PROGRESS,
    )
    # Sibling stays IN_PROGRESS so project aggregation path is not triggered
    sibling = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "sibling"}],
        vm_instance_id="agent-vm-enriched-pr-sibling",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    db_session.add(sibling)
    await db_session.commit()
    await db_session.refresh(task)

    diff_summary = [
        {"file": "backend/app/models.py", "additions": 45, "deletions": 12, "change_type": "modified"},
        {"file": "backend/testing/test_models.py", "additions": 30, "deletions": 0, "change_type": "added"},
    ]
    self_review_outcome = {"issues_found": False, "fixes_applied": False, "categories": []}
    test_failure_summary = "FAILED backend/testing/test_models.py::test_create"

    token = create_agent_token(str(task.id), "agent-vm-enriched-pr")
    headers = {"x-agent-token": token}
    payload = {
        "task_id": str(task.id),
        "status": "COMPLETED",
        "artifact_type": "PULL_REQUEST",
        "pr_url": "https://github.com/foo/bar/pull/10",
        "changed_files": ["backend/app/models.py", "backend/testing/test_models.py"],
        "diff_summary": diff_summary,
        "self_review_outcome": self_review_outcome,
        "test_failure_summary": test_failure_summary,
    }
    resp = await client.post("/api/agent/status", json=payload, headers=headers)
    assert resp.status_code == 200

    await db_session.refresh(task)
    ctx = task.context or {}
    assert ctx.get("_pr_diff_summary") == diff_summary
    assert ctx.get("_self_review_outcome") == self_review_outcome
    assert ctx.get("_test_failure_summary") == test_failure_summary
    assert ctx.get("_pr_url") == "https://github.com/foo/bar/pull/10"


@pytest.mark.asyncio
async def test_agent_status_failed_artifact_stores_diff_summary_and_test_failure(client: AsyncClient, db_session, mocker):
    """FAILED+ERROR_LOG status with diff_summary and test_failure_summary stores them
    in task context as _pr_diff_summary and _test_failure_summary."""
    mocker.patch("app.main.delete_agent_vm", return_value=None)
    mocker.patch("app.main._ensure_vm_deleted", new_callable=mocker.AsyncMock)
    mocker.patch("app.broadcast.broadcast_to_project", return_value=None)

    project = Project(name="Enriched Context Failed Test", user_id="test-user-enriched-failed")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "do thing"}],
        vm_instance_id="agent-vm-enriched-failed",
        status=TaskStatus.IN_PROGRESS,
        # Exhaust retries so the FAILED path terminates without requeueing
        attempt_count=3,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    diff_summary = [
        {"file": "backend/app/models.py", "additions": 10, "deletions": 2, "change_type": "modified"},
    ]
    test_failure_summary = "FAILED backend/testing/test_models.py::test_create - AssertionError"

    token = create_agent_token(str(task.id), "agent-vm-enriched-failed")
    headers = {"x-agent-token": token}
    payload = {
        "task_id": str(task.id),
        "status": "FAILED",
        "artifact_type": "ERROR_LOG",
        "logs": "Test suite failed after max attempts",
        "changed_files": ["backend/app/models.py"],
        "diff_summary": diff_summary,
        "test_failure_summary": test_failure_summary,
    }
    resp = await client.post("/api/agent/status", json=payload, headers=headers)
    assert resp.status_code == 200

    await db_session.refresh(task)
    ctx = task.context or {}
    assert ctx.get("_pr_diff_summary") == diff_summary
    assert ctx.get("_test_failure_summary") == test_failure_summary
