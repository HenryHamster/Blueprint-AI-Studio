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


@pytest.mark.asyncio
async def test_recently_completed_tasks_include_outcome_context(client: AsyncClient, db_session):
    """
    Recently completed tasks in the claim response carry prompt, related_files,
    direction_id, and (for failed tasks) changed_files so the next agent can
    understand what the prior task tried and what it touched.
    """
    project = Project(name="Recent Outcome Context", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # A failed task with changed_files and a direction
    long_prompt = "Describe the change in detail. " * 20  # ~620 chars
    failed_task = Task(
        project_id=project.id,
        title="Failed feature attempt",
        description="desc",
        payloads=[{"type": "create", "prompt": long_prompt}],
        vm_instance_id="agent-vm-recent-failed",
        status=TaskStatus.FAILED,
        context={
            "direction_id": "dir-abc",
            "related_files": [f"file{i}.py" for i in range(15)],
            "_pr_changed_files": [f"changed{i}.py" for i in range(15)],
        },
        latest_status_message="Tests failed on CI",
    )
    db_session.add(failed_task)
    # A completed task with changed_files (should also be exposed)
    completed_task = Task(
        project_id=project.id,
        title="Completed task",
        payloads=[{"type": "create", "prompt": "Short prompt"}],
        vm_instance_id="agent-vm-recent-done",
        status=TaskStatus.COMPLETED,
        context={
            "direction_id": "dir-xyz",
            "related_files": ["a.py", "b.py"],
            "_pr_changed_files": ["modded1.py", "modded2.py"],
        },
    )
    db_session.add(completed_task)
    await db_session.commit()

    claim_task = Task(
        project_id=project.id,
        title="Claimer",
        payloads=[{"type": "research", "prompt": "Research"}],
        vm_instance_id="agent-vm-recent-claimer",
    )
    db_session.add(claim_task)
    await db_session.commit()
    await db_session.refresh(claim_task)

    token = create_agent_token(str(claim_task.id), "agent-vm-recent-claimer")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200
    recent = resp.json()["project_context"]["recently_completed_tasks"]

    failed_entry = next(e for e in recent if e["title"] == "Failed feature attempt")
    assert failed_entry["direction_id"] == "dir-abc"
    assert len(failed_entry["related_files"]) == 10  # capped
    assert failed_entry["related_files"][0] == "file0.py"
    assert len(failed_entry["changed_files"]) == 10  # capped
    assert failed_entry["prompt"] is not None
    assert len(failed_entry["prompt"]) <= 300  # truncated
    assert failed_entry["failure_reason"] == "Tests failed on CI"

    done_entry = next(e for e in recent if e["title"] == "Completed task")
    assert done_entry["direction_id"] == "dir-xyz"
    assert done_entry["related_files"] == ["a.py", "b.py"]
    assert done_entry["changed_files"] == ["modded1.py", "modded2.py"]
    assert done_entry["prompt"] == "Short prompt"


@pytest.mark.asyncio
async def test_lessons_include_first_feedback_when_distinct(client: AsyncClient, db_session):
    """
    Lessons from high-iteration tasks include first_feedback (iteration 0) in
    addition to last_feedback so the initial rejection reason is visible.
    """
    project = Project(name="Lessons First Feedback", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    lesson_task = Task(
        project_id=project.id,
        title="Lesson source",
        payloads=[{"type": "create", "prompt": "Build stuff"}],
        vm_instance_id="agent-vm-lesson",
        status=TaskStatus.COMPLETED,
        context={
            "review_loop": {
                "iteration": 2,
                "review_history": [
                    {"iteration": 0, "feedback": "Initial rejection: wrong approach"},
                    {"iteration": 1, "feedback": "Still failing: missing tests"},
                ],
            }
        },
    )
    db_session.add(lesson_task)
    await db_session.commit()

    claim_task = Task(
        project_id=project.id,
        title="Claimer",
        payloads=[{"type": "research", "prompt": "Research"}],
        vm_instance_id="agent-vm-lesson-claimer",
    )
    db_session.add(claim_task)
    await db_session.commit()
    await db_session.refresh(claim_task)

    token = create_agent_token(str(claim_task.id), "agent-vm-lesson-claimer")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200
    lessons = resp.json()["project_context"]["lessons"]
    assert len(lessons) == 1
    lesson = lessons[0]
    assert lesson["first_feedback"] == "Initial rejection: wrong approach"
    assert lesson["last_feedback"] == "Still failing: missing tests"


@pytest.mark.asyncio
async def test_agent_claim_sibling_tasks_include_terminal_outcomes(client: AsyncClient, db_session):
    """
    Tests that sibling_tasks in the claim response includes both active and terminal
    siblings. Terminal entries carry status, failure_class (if FAILED), pr_url (if any),
    and an active=False flag so the agent can distinguish in-progress conflicts from
    historical outcomes.
    """
    project = Project(name="Sibling Outcomes Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    parent = Task(
        project_id=project.id,
        title="Parent task",
        payloads=[{"type": "research", "prompt": "investigate"}],
        vm_instance_id="agent-vm-sib-parent",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(parent)
    await db_session.commit()
    await db_session.refresh(parent)

    completed_sib = Task(
        project_id=project.id,
        title="Completed sibling",
        payloads=[{"type": "create", "prompt": "ship feature A"}],
        vm_instance_id="agent-vm-sib-completed",
        status=TaskStatus.COMPLETED,
        parent_task_id=parent.id,
        context={"related_files": ["backend/app/foo.py"]},
    )
    failed_sib = Task(
        project_id=project.id,
        title="Failed sibling",
        payloads=[{"type": "create", "prompt": "ship feature B"}],
        vm_instance_id="agent-vm-sib-failed",
        status=TaskStatus.FAILED,
        parent_task_id=parent.id,
        latest_status_message="ImportError in module xyz: cannot find symbol foo across 3 modules",
        context={
            "related_files": ["backend/app/bar.py"],
            "failure_category": "CODE_ERROR",
            "_pr_changed_files": [
                "backend/app/bar.py",
                "backend/app/baz.py",
                "backend/app/qux.py",
                "backend/app/quux.py",
                "backend/app/corge.py",
                "backend/app/grault.py",
            ],
        },
    )
    in_progress_sib = Task(
        project_id=project.id,
        title="In-progress sibling",
        payloads=[{"type": "create", "prompt": "ship feature C"}],
        vm_instance_id="agent-vm-sib-active",
        status=TaskStatus.IN_PROGRESS,
        parent_task_id=parent.id,
        context={"related_files": ["backend/app/baz.py"]},
    )
    db_session.add_all([completed_sib, failed_sib, in_progress_sib])
    await db_session.commit()
    await db_session.refresh(completed_sib)

    pr_artifact = Artifact(
        task_id=completed_sib.id,
        artifact_type=ArtifactType.PULL_REQUEST,
        pr_url="https://github.com/example/repo/pull/42",
    )
    db_session.add(pr_artifact)

    claim_task = Task(
        project_id=project.id,
        title="Claiming sibling",
        payloads=[{"type": "create", "prompt": "ship feature D"}],
        vm_instance_id="agent-vm-sib-claimer",
        status=TaskStatus.QUEUED,
        parent_task_id=parent.id,
    )
    db_session.add(claim_task)
    await db_session.commit()
    await db_session.refresh(claim_task)

    token = create_agent_token(str(claim_task.id), "agent-vm-sib-claimer")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200

    siblings = resp.json().get("project_context", {}).get("sibling_tasks", [])
    by_title = {s["title"]: s for s in siblings}
    assert set(by_title.keys()) == {"Completed sibling", "Failed sibling", "In-progress sibling"}

    active_entry = by_title["In-progress sibling"]
    assert active_entry["active"] is True
    assert active_entry["related_files"] == ["backend/app/baz.py"]

    completed_entry = by_title["Completed sibling"]
    assert completed_entry["active"] is False
    assert completed_entry["status"] == TaskStatus.COMPLETED.value
    assert completed_entry["pr_url"] == "https://github.com/example/repo/pull/42"
    assert "failure_class" not in completed_entry

    failed_entry = by_title["Failed sibling"]
    assert failed_entry["active"] is False
    assert failed_entry["status"] == TaskStatus.FAILED.value
    assert failed_entry["failure_class"] == "CODE_ERROR"
    assert failed_entry["pr_url"] is None
    # New: failure surfaces an error excerpt and capped changed files list
    assert failed_entry["error_excerpt"].startswith("ImportError in module xyz")
    assert len(failed_entry["error_excerpt"]) <= 150
    assert failed_entry["changed_files"] == [
        "backend/app/bar.py",
        "backend/app/baz.py",
        "backend/app/qux.py",
        "backend/app/quux.py",
        "backend/app/corge.py",
    ]
    # Completed sibling does not get error_excerpt / changed_files
    assert "error_excerpt" not in completed_entry
    assert "changed_files" not in completed_entry


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
async def test_design_doc_file_to_system_index(client: AsyncClient, db_session):
    """Setting systems/* keys rebuilds _file_to_system reverse index; index hidden from list."""
    project = Project(name="Design Doc Index Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "design_doc_update"}],
        vm_instance_id="agent-vm-ddi-test",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-ddi-test", task_type="design_doc_update")
    headers = {"x-agent-token": token}

    # Set first system
    resp = await client.post("/api/agent/design-doc", json={
        "task_id": str(task.id),
        "op": "set",
        "key": "systems/auth",
        "value": {"name": "Auth", "modules": ["backend/app/auth.py", "backend/app/models.py"]},
    }, headers=headers)
    assert resp.status_code == 200

    await db_session.refresh(project)
    idx = project.design_doc["_file_to_system"]
    assert idx["backend/app/auth.py"] == "auth"
    assert idx["backend/app/models.py"] == "auth"

    # Set second system — index should include files from both systems
    resp2 = await client.post("/api/agent/design-doc", json={
        "task_id": str(task.id),
        "op": "set",
        "key": "systems/database",
        "value": {"name": "Database", "modules": ["backend/app/db.py", "backend/app/models.py"]},
    }, headers=headers)
    assert resp2.status_code == 200

    await db_session.refresh(project)
    idx = project.design_doc["_file_to_system"]
    assert idx["backend/app/auth.py"] == "auth"
    assert idx["backend/app/db.py"] == "database"
    # last-write-wins for shared file: database set second
    assert idx["backend/app/models.py"] == "database"

    # _file_to_system must NOT appear in list
    resp3 = await client.post("/api/agent/design-doc", json={
        "task_id": str(task.id), "op": "list",
    }, headers=headers)
    assert resp3.status_code == 200
    assert "_file_to_system" not in resp3.json()["keys"]

    # Delete a system — index should no longer contain its files
    resp4 = await client.post("/api/agent/design-doc", json={
        "task_id": str(task.id), "op": "delete", "key": "systems/auth",
    }, headers=headers)
    assert resp4.status_code == 200

    await db_session.refresh(project)
    idx = project.design_doc["_file_to_system"]
    assert "backend/app/auth.py" not in idx
    assert idx["backend/app/db.py"] == "database"


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
    entry = playbook["build-steps"]
    assert isinstance(entry, dict), f"Expected playbook entry to be a dict, got: {type(entry)}"
    assert entry["content"] == "run npm run build"
    assert "effectiveness_score" in entry
    assert "confirmed_count" in entry
    assert "age_days" in entry
    assert "stale" in entry
    assert "test-commands" in playbook
    assert playbook["test-commands"]["content"] == "bash scripts/test-backend.sh"


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_claim_playbook_demotes_ineffective_entries(client: AsyncClient, db_session):
    """Entries with low effectiveness score (confirmed/referenced ratio) sort after effective entries."""
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
async def test_claim_skips_stale_playbook_entries(client: AsyncClient, db_session):
    """Playbook entries with stale=True are not shown to agents (auto-archived)."""
    from sqlalchemy.orm.attributes import flag_modified

    project = Project(name="Stale Playbook Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    project.design_doc = {
        "playbook/stale-entry": {
            "content": "outdated guidance that no longer helps",
            "source_task_id": "seed",
            "updated_at": "2026-01-01T00:00:00",
            "confirmed_count": 0,
            "reference_count": 15,
            "stale": True,
            "effectiveness_score": 0.0,
        },
        "playbook/active-entry": {
            "content": "current helpful guidance",
            "source_task_id": "seed",
            "updated_at": "2026-01-01T00:00:00",
            "confirmed_count": 2,
            "reference_count": 5,
        },
    }
    flag_modified(project, "design_doc")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "Do work"}],
        vm_instance_id="agent-vm-stale-test",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-stale-test")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200

    data = resp.json()
    playbook = data.get("playbook", {})
    assert "active-entry" in playbook, "Active entry should be shown"
    assert "stale-entry" not in playbook, "Stale entry should be excluded"


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_claim_appends_recent_outcomes_on_reference(client: AsyncClient, db_session):
    """Claim appends False to recent_outcomes for each shown entry and stores _shown_playbook_keys."""
    from sqlalchemy.orm.attributes import flag_modified

    project = Project(name="Recent Outcomes Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    project.design_doc = {
        "playbook/my-entry": {
            "content": "some guidance",
            "source_task_id": "seed",
            "updated_at": "2026-01-01T00:00:00",
            "confirmed_count": 1,
            "reference_count": 3,
            "recent_outcomes": [True, False, True],
        },
    }
    flag_modified(project, "design_doc")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "Do work"}],
        vm_instance_id="agent-vm-outcomes-test",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-outcomes-test")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200

    # Verify recent_outcomes got a False appended
    await db_session.refresh(project)
    entry = project.design_doc["playbook/my-entry"]
    outcomes = entry.get("recent_outcomes", [])
    assert outcomes == [True, False, True, False], f"Expected [..., False] appended, got {outcomes}"

    # Verify task context stores shown keys
    await db_session.refresh(task)
    shown_keys = (task.context or {}).get("_shown_playbook_keys", [])
    assert "my-entry" in shown_keys, f"Expected 'my-entry' in _shown_playbook_keys, got {shown_keys}"


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_claim_records_review_measurement_attribution(client: AsyncClient, db_session):
    """Claim stores _had_review_measurements=True and _top_review_issue_categories when review data exists."""
    project = Project(name="Review Measurement Attribution Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "Do work"}],
        vm_instance_id="agent-vm-review-attr",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # Seed completed tasks with review_loop history containing >=5 categorizable issues
    review_issue = {"description": "Missing unit test coverage for this function", "severity": "MAJOR"}
    review_history_entry = {"issues": [review_issue] * 5, "iteration": 1}
    completed_task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "prev"}],
        status=TaskStatus.COMPLETED,
        attempt_count=2,
        context={"review_loop": {"iteration": 2, "review_history": [review_history_entry]}},
    )
    db_session.add(completed_task)
    await db_session.commit()

    token = create_agent_token(str(task.id), "agent-vm-review-attr")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200

    await db_session.refresh(task)
    ctx = task.context or {}
    assert ctx.get("_had_review_measurements") is True, f"Expected _had_review_measurements=True, got {ctx}"
    categories = ctx.get("_top_review_issue_categories", [])
    assert len(categories) > 0, f"Expected non-empty _top_review_issue_categories, got {categories}"
    assert "test-coverage" in categories, f"Expected test-coverage in categories, got {categories}"


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_claim_review_measurement_attribution_false_when_no_data(client: AsyncClient, db_session):
    """Claim stores _had_review_measurements=False when no review data is available."""
    project = Project(name="No Review Data Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "Do work"}],
        vm_instance_id="agent-vm-no-review",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-no-review")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200

    await db_session.refresh(task)
    ctx = task.context or {}
    assert ctx.get("_had_review_measurements") is False, f"Expected _had_review_measurements=False, got {ctx}"
    assert "_top_review_issue_categories" not in ctx, "Should not store categories when no measurements"


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_claim_recent_outcomes_sorts_by_effectiveness(client: AsyncClient, db_session):
    """Claim sorts entries by recent_outcomes effectiveness score (sum/len) descending."""
    from sqlalchemy.orm.attributes import flag_modified

    project = Project(name="Effectiveness Sort Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    project.design_doc = {
        # Low effectiveness: 1/5 = 0.2 score
        "playbook/low-effectiveness": {
            "content": "rarely helps",
            "source_task_id": "seed",
            "updated_at": "2026-01-01T00:00:00",
            "confirmed_count": 1,
            "reference_count": 5,
            "recent_outcomes": [False, False, False, True, False],  # score = 0.2
        },
        # High effectiveness: 4/5 = 0.8 score
        "playbook/high-effectiveness": {
            "content": "usually helps",
            "source_task_id": "seed",
            "updated_at": "2026-01-01T00:00:00",
            "confirmed_count": 4,
            "reference_count": 5,
            "recent_outcomes": [True, True, False, True, True],  # score = 0.8
        },
    }
    flag_modified(project, "design_doc")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "Do work"}],
        vm_instance_id="agent-vm-effectiveness-sort",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-effectiveness-sort")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200

    data = resp.json()
    playbook = data.get("playbook", {})
    keys = list(playbook.keys())
    assert keys.index("high-effectiveness") < keys.index("low-effectiveness"), (
        f"Expected high-effectiveness before low-effectiveness, got: {keys}"
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
    assert {
        "text": "JWT in cookies caused CSRF issues",
        "failure_type": "",
        "failed_at": "",
        "failure_stage": "",
    } in dc["failed_approaches"]
    assert {
        "text": "Session-based auth had scaling problems",
        "failure_type": "",
        "failed_at": "",
        "failure_stage": "",
    } in dc["failed_approaches"]
    # recent_outcomes: last 2 history entries with tasks_created > 0
    assert len(dc["recent_outcomes"]) == 2
    assert dc["recent_outcomes"][0]["prs_merged"] == 2
    assert dc["recent_outcomes"][1]["prs_merged"] == 1


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_claim_injects_scope_success_rates_in_direction_context(client: AsyncClient, db_session):
    """direction_context.scope_success_rates aggregates scope_breakdown over last 3 productive cycles."""
    from sqlalchemy.orm.attributes import flag_modified

    direction_id = str(uuid.uuid4())
    project = Project(name="Scope Rates Test", user_id="test-user-xyz")
    project.directions = [
        {
            "id": direction_id,
            "title": "d",
            "priority": "medium",
            "research_context": "",
            "research_history": [
                # non-productive cycle should be excluded
                {"tasks_created": 0, "prs_merged": 0, "scope_breakdown": {"small": {"created": 0, "merged": 0, "failed": 0}}},
                {"tasks_created": 2, "prs_merged": 2, "scope_breakdown": {"small": {"created": 2, "merged": 2, "failed": 0}}},
                {"tasks_created": 2, "prs_merged": 1, "scope_breakdown": {"small": {"created": 2, "merged": 1, "failed": 1}, "medium": {"created": 1, "merged": 0, "failed": 1}}},
                {"tasks_created": 1, "prs_merged": 0, "scope_breakdown": {"empty": {"created": 0, "merged": 0, "failed": 0}}},
            ],
        }
    ]
    flag_modified(project, "directions")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "do"}],
        vm_instance_id="agent-vm-scope-rates",
        context={"direction_id": direction_id},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-scope-rates")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200
    dc = resp.json()["direction_context"]
    rates = dc["scope_success_rates"]
    assert rates["small"] == {"created": 4, "merged": 3, "failed": 1}
    assert rates["medium"] == {"created": 1, "merged": 0, "failed": 1}
    assert "empty" not in rates


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


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_claim_injects_saturation_signals_for_research_task(client: AsyncClient, db_session):
    """Claim endpoint injects possibly_saturated + deadlock_detected for research tasks."""
    from sqlalchemy.orm.attributes import flag_modified

    direction_id = str(uuid.uuid4())
    project = Project(name="Research Saturation Test", user_id="test-user-123")
    # 3 consecutive 0-task cycles with dedup activity → deadlock_detected True
    project.directions = [
        {
            "id": direction_id,
            "title": "Saturated direction",
            "priority": "low",
            "possibly_saturated": True,
            "research_history": [
                {"tasks_created": 0, "filter_breakdown": {"hash_dedup": 2}},
                {"tasks_created": 0, "filter_breakdown": {"hash_dedup": 1}},
                {"tasks_created": 0, "filter_breakdown": {"word_overlap_dedup": 3}},
            ],
        }
    ]
    flag_modified(project, "directions")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "research", "prompt": "Research this direction"}],
        vm_instance_id="agent-vm-research-sat",
        context={"direction_id": direction_id},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-research-sat")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200
    dc = resp.json().get("direction_context")
    assert dc is not None
    assert dc["possibly_saturated"] is True
    assert dc["deadlock_detected"] is True
    # Research tasks should not get implementer-specific fields
    assert "failed_approaches" not in dc


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


@pytest.mark.asyncio
async def test_compute_failure_stage_pattern_staging_only_dominant():
    """Returns staging_only pattern when 60%+ of non-infra failures are staging-only dropouts."""
    from app.routers.agent import _compute_failure_stage_pattern

    history = [
        {"stage_dropout": {"tasks_failed_code_error": 0, "tasks_failed_infra": 1, "tasks_required_approval": 1, "tasks_merged_staging_only": 3}},
        {"stage_dropout": {"tasks_failed_code_error": 1, "tasks_failed_infra": 0, "tasks_required_approval": 0, "tasks_merged_staging_only": 2}},
    ]
    # Non-infra totals: code_error=1, approval=1, staging_only=5, total=7 → rate=5/7≈0.714
    result = _compute_failure_stage_pattern(history)
    assert result is not None
    assert result["stage"] == "staging_only"
    assert result["rate"] >= 0.6
    assert "integration" in result["message"].lower() or "rollup" in result["message"].lower() or "ci" in result["message"].lower()


def test_parse_failed_approach_extracts_known_category():
    """Known failure categories in parens are extracted into failure_type."""
    from app.routers.agent import _parse_failed_approach

    result = _parse_failed_approach("Use jwt in cookies (CODE_ERROR) — CSRF vulnerability")
    assert result["failure_type"] == "CODE_ERROR"
    assert "CODE_ERROR" not in result["text"]
    assert "Use jwt in cookies" in result["text"]
    assert "CSRF" in result["text"]


def test_parse_failed_approach_without_category_passes_through():
    """Lines without a parenthesised category return text unchanged and empty failure_type."""
    from app.routers.agent import _parse_failed_approach

    result = _parse_failed_approach("Session auth had scaling issues")
    assert result["failure_type"] == ""
    assert result["text"] == "Session auth had scaling issues"


def test_parse_failed_approach_ignores_unknown_parens():
    """Parens whose contents aren't in the known-category set are kept as prose."""
    from app.routers.agent import _parse_failed_approach

    result = _parse_failed_approach("Try batching (see issue #42) to reduce round trips")
    assert result["failure_type"] == ""
    # Prose parens must be preserved — they're part of the description, not metadata
    assert "(see issue #42)" in result["text"]


def test_parse_failed_approach_truncates_long_text():
    """Text is capped at 200 chars to avoid bloat in agent prompts."""
    from app.routers.agent import _parse_failed_approach

    long_line = "x" * 300
    result = _parse_failed_approach(long_line)
    assert len(result["text"]) == 200


def test_parse_failed_approach_extracts_failed_at_and_stage_metadata():
    """``[at=... stage=...]`` metadata after the category is parsed and removed."""
    from app.routers.agent import _parse_failed_approach

    line = "Use jwt in cookies (CODE_ERROR) [at=2026-04-15 stage=test] — CSRF vulnerability"
    result = _parse_failed_approach(line)
    assert result["failure_type"] == "CODE_ERROR"
    assert result["failed_at"] == "2026-04-15"
    assert result["failure_stage"] == "test"
    # Metadata block should not leak into text
    assert "at=" not in result["text"]
    assert "stage=" not in result["text"]
    assert "Use jwt in cookies" in result["text"]
    assert "CSRF" in result["text"]


def test_parse_failed_approach_stage_only_metadata():
    """Metadata block may contain only one of the two fields."""
    from app.routers.agent import _parse_failed_approach

    result = _parse_failed_approach("Approach X (CODE_ERROR) [stage=lint] — ruff flagged style")
    assert result["failure_type"] == "CODE_ERROR"
    assert result["failed_at"] == ""
    assert result["failure_stage"] == "lint"


def test_parse_failed_approach_without_metadata_returns_empty_fields():
    """Legacy entries without a metadata block still parse cleanly."""
    from app.routers.agent import _parse_failed_approach

    result = _parse_failed_approach("Legacy (CODE_ERROR) — some reason")
    assert result["failure_type"] == "CODE_ERROR"
    assert result["failed_at"] == ""
    assert result["failure_stage"] == ""


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


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_claim_injects_direction_prior_analysis(client: AsyncClient, db_session):
    """Claim endpoint surfaces condensed prior analysis from recent failed/cancelled sibling tasks."""
    from sqlalchemy.orm.attributes import flag_modified

    direction_id = str(uuid.uuid4())
    project = Project(name="Prior Analysis Test", user_id="test-user-123")
    project.directions = [
        {
            "id": direction_id,
            "title": "Refactor module X",
            "priority": "medium",
            "research_context": "",
            "research_history": [],
        }
    ]
    flag_modified(project, "directions")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    long_plan = "A" * 800  # will be truncated to 500
    long_tfs = "B" * 600  # will be truncated to 300
    prior = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "prior"}],
        vm_instance_id="vm-prior-1",
        context={
            "direction_id": direction_id,
            "_analysis_plan": long_plan,
            "_test_failure_summary": long_tfs,
            "_pr_changed_files": ["foo.py", "bar.ts"],
            "_pr_diff_summary": [
                {"file": "foo.py", "additions": 10, "deletions": 5, "change_type": "modified"},
                {"file": "bar.ts", "additions": 3, "deletions": 2, "change_type": "added"},
            ],
            "_previous_error_fingerprint": "abc123def456",
            "_self_review_outcome": {
                "verdict": "FAIL",
                "notes": "lint errors remain",
                "check_results": {
                    "lint": {"passed": False, "issue": "E501"},
                    "tests": {"passed": True},
                    "types": {"passed": False},
                },
            },
            "review_loop": {"iteration": 3},
        },
        status=TaskStatus.FAILED,
        title="Prior failed task",
        latest_status_message="TypeError: cannot read property x of undefined",
    )
    # Prior with no analysis artifacts and no status message — should be skipped
    prior_no_artifacts = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "no artifacts"}],
        vm_instance_id="vm-prior-2",
        context={"direction_id": direction_id},
        status=TaskStatus.FAILED,
        title="No artifacts task",
        latest_status_message=None,
    )
    db_session.add_all([prior, prior_no_artifacts])
    await db_session.commit()

    current = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "current"}],
        vm_instance_id="vm-current",
        context={"direction_id": direction_id},
    )
    db_session.add(current)
    await db_session.commit()
    await db_session.refresh(current)

    token = create_agent_token(str(current.id), "vm-current")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200
    dc = resp.json()["direction_context"]
    assert "direction_prior_analysis" in dc
    dpa = dc["direction_prior_analysis"]
    assert len(dpa) == 1
    entry = dpa[0]
    assert entry["title"] == "Prior failed task"
    assert "failure_category" in entry
    assert len(entry["analysis_plan"]) == 500
    assert len(entry["test_failure_summary"]) == 300
    assert entry["pr_changed_files"] == ["foo.py", "bar.ts"]
    assert entry["pr_diff_summary"] == [
        {"file": "foo.py", "added": 10, "removed": 5, "change_type": "modified"},
        {"file": "bar.ts", "added": 3, "removed": 2, "change_type": "added"},
    ]
    assert entry["previous_error_fingerprint"] == "abc123def456"
    assert entry["latest_status_message"] == "TypeError: cannot read property x of undefined"
    assert entry["self_review_outcome"]["verdict"] == "FAIL"
    assert entry["self_review_outcome"]["notes"] == "lint errors remain"
    assert set(entry["self_review_outcome"]["failed_checks"]) == {"lint", "types"}
    assert entry["review_iterations"] == 3
    # Total payload stays under 2000-char cap
    import json as _json
    assert len(_json.dumps(dpa)) <= 2000


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_claim_includes_structured_review_feedback(client: AsyncClient, db_session):
    """Failed tasks with structured review issues surface top critical/major items."""
    from sqlalchemy.orm.attributes import flag_modified

    direction_id = str(uuid.uuid4())
    project = Project(name="Review Feedback Test", user_id="test-user-123")
    project.directions = [
        {
            "id": direction_id,
            "title": "Refactor module Z",
            "priority": "medium",
            "research_context": "",
            "research_history": [],
        }
    ]
    flag_modified(project, "directions")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    prior = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "prior"}],
        vm_instance_id="vm-rf-1",
        context={
            "direction_id": direction_id,
            "_analysis_plan": "approach plan",
            "review_loop": {
                "iteration": 2,
                "review_history": [
                    {
                        "iteration": 0,
                        "feedback": "first round",
                        "issues": [
                            {"severity": "minor", "description": "old style"},
                        ],
                    },
                    {
                        "iteration": 1,
                        "feedback": "second round",
                        "issues": [
                            {"severity": "minor", "description": "nit: rename var"},
                            {"severity": "critical", "description": "missing null check in handler", "file": "foo.py", "line": 42},
                            {"severity": "major", "description": "no test for edge case", "file": "test_foo.py"},
                            {"severity": "critical", "description": "race condition in cache update"},
                            {"severity": "major", "description": "should be a single bullet but text long " * 10},
                        ],
                    },
                ],
            },
        },
        status=TaskStatus.FAILED,
        title="Prior with review feedback",
        latest_status_message="failed self-review",
    )
    db_session.add(prior)
    await db_session.commit()

    current = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "current"}],
        vm_instance_id="vm-rf-current",
        context={"direction_id": direction_id},
    )
    db_session.add(current)
    await db_session.commit()
    await db_session.refresh(current)

    token = create_agent_token(str(current.id), "vm-rf-current")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200
    dpa = resp.json()["direction_context"]["direction_prior_analysis"]
    assert len(dpa) == 1
    rf = dpa[0]["review_feedback"]
    # Top 3 critical/major from the LAST review entry, critical first
    assert isinstance(rf, list)
    assert len(rf) == 3
    assert rf[0]["severity"] == "CRITICAL"
    assert rf[0]["description"] == "missing null check in handler"
    assert rf[0]["file"] == "foo.py"
    assert rf[0]["line"] == 42
    assert rf[1]["severity"] == "CRITICAL"
    assert rf[1]["description"] == "race condition in cache update"
    assert "file" not in rf[1]
    assert rf[2]["severity"] == "MAJOR"
    # Long description truncated to 150 chars
    assert len(rf[2]["description"]) <= 150


@pytest.mark.asyncio
async def test_claim_review_feedback_falls_back_to_text(client: AsyncClient, db_session):
    """When review history lacks structured issues, fall back to truncated feedback string."""
    from sqlalchemy.orm.attributes import flag_modified

    direction_id = str(uuid.uuid4())
    project = Project(name="Review Feedback Fallback", user_id="test-user-123")
    project.directions = [
        {
            "id": direction_id,
            "title": "Refactor module W",
            "priority": "medium",
            "research_context": "",
            "research_history": [],
        }
    ]
    flag_modified(project, "directions")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    long_feedback = "F" * 500  # truncated to 200
    prior = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "prior"}],
        vm_instance_id="vm-rf-fb-1",
        context={
            "direction_id": direction_id,
            "_analysis_plan": "approach",
            "review_loop": {
                "iteration": 1,
                "review_history": [
                    {"iteration": 0, "feedback": long_feedback},
                ],
            },
        },
        status=TaskStatus.FAILED,
        title="Prior text feedback",
        latest_status_message="failed",
    )
    db_session.add(prior)
    await db_session.commit()

    current = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "current"}],
        vm_instance_id="vm-rf-fb-current",
        context={"direction_id": direction_id},
    )
    db_session.add(current)
    await db_session.commit()
    await db_session.refresh(current)

    token = create_agent_token(str(current.id), "vm-rf-fb-current")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200
    dpa = resp.json()["direction_context"]["direction_prior_analysis"]
    assert len(dpa) == 1
    rf = dpa[0]["review_feedback"]
    assert isinstance(rf, str)
    assert len(rf) == 200
    assert rf == "F" * 200


@pytest.mark.asyncio
async def test_claim_injects_direction_successful_analysis(client: AsyncClient, db_session):
    """Claim endpoint surfaces analysis_plan from recent successful sibling tasks."""
    from sqlalchemy.orm.attributes import flag_modified

    direction_id = str(uuid.uuid4())
    project = Project(name="Success Analysis Test", user_id="test-user-123")
    project.directions = [
        {
            "id": direction_id,
            "title": "Refactor module Y",
            "priority": "medium",
            "research_context": "",
            "research_history": [],
        }
    ]
    flag_modified(project, "directions")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    long_plan = "P" * 800  # truncated to 500
    success = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "success"}],
        vm_instance_id="vm-success-1",
        context={
            "direction_id": direction_id,
            "_analysis_plan": long_plan,
            "_pr_changed_files": ["svc.py", "route.py"],
            "scope": "medium",
            "review_loop": {"iteration": 2},
        },
        status=TaskStatus.COMPLETED,
        title="Successful task",
    )
    # Small-scope task without analysis plan but with pr_changed_files — should be included
    success_small = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "small"}],
        vm_instance_id="vm-success-2",
        context={
            "direction_id": direction_id,
            "_pr_changed_files": ["small.py"],
            "scope": "small",
        },
        status=TaskStatus.COMPLETED,
        title="Successful small-scope task",
    )
    # Successful task with no analysis plan AND no pr_changed_files — should be skipped
    success_empty = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "empty"}],
        vm_instance_id="vm-success-3",
        context={"direction_id": direction_id},
        status=TaskStatus.COMPLETED,
        title="Successful empty task",
    )
    db_session.add_all([success, success_small, success_empty])
    await db_session.commit()

    current = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "current"}],
        vm_instance_id="vm-current",
        context={"direction_id": direction_id},
    )
    db_session.add(current)
    await db_session.commit()
    await db_session.refresh(current)

    token = create_agent_token(str(current.id), "vm-current")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200
    dc = resp.json()["direction_context"]
    assert "direction_successful_analysis" in dc
    dsa = dc["direction_successful_analysis"]
    assert len(dsa) == 2
    titles = {e["title"] for e in dsa}
    assert titles == {"Successful task", "Successful small-scope task"}
    by_title = {e["title"]: e for e in dsa}
    full = by_title["Successful task"]
    assert len(full["analysis_plan"]) == 500
    assert full["pr_changed_files"] == ["svc.py", "route.py"]
    assert full["scope"] == "medium"
    assert full["review_iterations"] == 2
    small = by_title["Successful small-scope task"]
    assert "analysis_plan" not in small
    assert small["pr_changed_files"] == ["small.py"]
    assert small["scope"] == "small"
    assert "review_iterations" not in small
    import json as _json
    assert len(_json.dumps(dsa)) <= 1500


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
async def test_claim_injects_cochange_hints_for_plan_task(client: AsyncClient, db_session):
    """Claim endpoint injects cochange_hints in direction_context for plan tasks restricted to related_files."""
    from sqlalchemy.orm.attributes import flag_modified

    cochange_index = {
        "backend/app/models.py": {
            "backend/app/schemas.py": 3.0,
            "backend/testing/test_models.py": 2.5,
            "backend/app/orchestrator.py": 0.5,  # below 1.0 threshold
        },
        "backend/app/schemas.py": {
            "backend/app/models.py": 3.0,
            "backend/testing/test_schemas.py": 1.2,  # not in related_files
        },
        "backend/testing/test_models.py": {
            "backend/app/models.py": 2.5,
        },
    }
    project = Project(name="Cochange Plan Test", user_id="test-user-123", design_doc={"cochange_index": cochange_index})
    flag_modified(project, "design_doc")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "plan", "prompt": "Decompose model refactor"}],
        vm_instance_id="agent-vm-cochange-plan",
        context={
            "related_files": [
                "backend/app/models.py",
                "backend/app/schemas.py",
                "backend/testing/test_models.py",
            ],
        },
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-cochange-plan")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200

    data = resp.json()
    direction_ctx = data.get("direction_context", {})
    assert "cochange_hints" in direction_ctx, f"Expected cochange_hints in direction_context, got: {list(direction_ctx.keys())}"
    hints = direction_ctx["cochange_hints"]
    # models.py paired with both schemas.py (3.0) and test_models.py (2.5) — both in related_files, both >= 1.0
    assert hints["backend/app/models.py"]["backend/app/schemas.py"] == 3.0
    assert hints["backend/app/models.py"]["backend/testing/test_models.py"] == 2.5
    # orchestrator.py is in the index but NOT in related_files — must be excluded
    assert "backend/app/orchestrator.py" not in hints["backend/app/models.py"]
    # schemas.py only pairs with models.py from related_files; test_schemas.py is not in related_files
    assert hints["backend/app/schemas.py"] == {"backend/app/models.py": 3.0}


@pytest.mark.asyncio
async def test_claim_omits_cochange_hints_when_below_threshold(client: AsyncClient, db_session):
    """Claim endpoint omits cochange_hints when no within-set pairs meet the >= 1.0 threshold."""
    from sqlalchemy.orm.attributes import flag_modified

    cochange_index = {
        "backend/app/a.py": {"backend/app/b.py": 0.7},  # below threshold
        "backend/app/b.py": {"backend/app/a.py": 0.7},
    }
    project = Project(name="Cochange Plan Threshold Test", user_id="test-user-123", design_doc={"cochange_index": cochange_index})
    flag_modified(project, "design_doc")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "plan", "prompt": "Decompose"}],
        vm_instance_id="agent-vm-cochange-plan-thresh",
        context={"related_files": ["backend/app/a.py", "backend/app/b.py"]},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-cochange-plan-thresh")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200

    data = resp.json()
    direction_ctx = data.get("direction_context", {})
    assert "cochange_hints" not in direction_ctx


@pytest.mark.asyncio
async def test_claim_does_not_inject_cochange_hints_for_create_task(client: AsyncClient, db_session):
    """Claim endpoint does NOT inject cochange_hints (a plan-only signal) into direction_context for create tasks."""
    from sqlalchemy.orm.attributes import flag_modified

    cochange_index = {
        "backend/app/models.py": {"backend/app/schemas.py": 3.0},
        "backend/app/schemas.py": {"backend/app/models.py": 3.0},
    }
    project = Project(name="Cochange Hint Skip Test", user_id="test-user-123", design_doc={"cochange_index": cochange_index})
    flag_modified(project, "design_doc")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "Add field"}],
        vm_instance_id="agent-vm-cochange-create-skip",
        context={"related_files": ["backend/app/models.py", "backend/app/schemas.py"]},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-cochange-create-skip")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200

    data = resp.json()
    direction_ctx = data.get("direction_context", {}) or {}
    assert "cochange_hints" not in direction_ctx, (
        f"cochange_hints should be plan-only, got direction_context keys: {list(direction_ctx.keys())}"
    )


@pytest.mark.asyncio
async def test_claim_injects_decomposition_outcomes_for_plan_task(client: AsyncClient, db_session):
    """Claim endpoint summarizes decomposition_quality from last 3 productive research_history entries for plan tasks."""
    from sqlalchemy.orm.attributes import flag_modified

    direction_id = str(uuid.uuid4())
    project = Project(name="Decomp Plan Test", user_id="test-user-decomp-plan")
    project.directions = [
        {
            "id": direction_id,
            "title": "Refactor models",
            "priority": "medium",
            "research_context": "",
            "research_history": [
                # Non-productive cycle → excluded
                {"tasks_created": 0, "prs_merged": 0, "decomposition_quality": [
                    {"plan_task_id": "ignore-me", "subtask_count": 1, "subtask_merge_rate": 0.0,
                     "avg_files_per_subtask": 0.0, "max_files_in_subtask": 0,
                     "dependency_depth": 0, "cross_module_count": 0},
                ]},
                {"tasks_created": 2, "prs_merged": 2, "decomposition_quality": [
                    {"plan_task_id": "p1", "subtask_count": 3, "subtask_merge_rate": 0.9,
                     "avg_files_per_subtask": 2.0, "max_files_in_subtask": 3,
                     "dependency_depth": 1, "cross_module_count": 0},
                ]},
                {"tasks_created": 2, "prs_merged": 1, "decomposition_quality": [
                    {"plan_task_id": "p2", "subtask_count": 4, "subtask_merge_rate": 0.2,
                     "avg_files_per_subtask": 7.0, "max_files_in_subtask": 10,
                     "dependency_depth": 4, "cross_module_count": 3},
                    {"plan_task_id": "p3", "subtask_count": 2, "subtask_merge_rate": 0.8,
                     "avg_files_per_subtask": 3.0, "max_files_in_subtask": 4,
                     "dependency_depth": 2, "cross_module_count": 0},
                ]},
            ],
        }
    ]
    flag_modified(project, "directions")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "plan", "prompt": "Decompose refactor"}],
        vm_instance_id="agent-vm-decomp-plan",
        context={"direction_id": direction_id},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-decomp-plan")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200

    data = resp.json()
    dc = data.get("direction_context", {}) or {}
    assert "decomposition_outcomes" in dc, f"Expected decomposition_outcomes, got: {list(dc.keys())}"
    summary = dc["decomposition_outcomes"]
    # 3 entries flattened from the 2 productive cycles (non-productive excluded)
    assert summary["prior_decomposition_count"] == 3
    # avg merge rate = (0.9 + 0.2 + 0.8) / 3 = 0.633
    assert summary["avg_subtask_merge_rate"] == round((0.9 + 0.2 + 0.8) / 3, 3)
    # avg files per subtask = (2 + 7 + 3) / 3 = 4.0
    assert summary["avg_files_per_subtask"] == 4.0
    assert summary["max_files_in_subtask"] == 10
    assert summary["avg_dependency_depth"] == round((1 + 4 + 2) / 3, 2)
    assert summary["max_dependency_depth"] == 4
    # high cross_module (>=2) merge rate = 0.2; low = (0.9+0.8)/2 = 0.85; gap >= 0.1 → true
    assert summary["high_cross_module_correlates_with_low_merge"] is True


@pytest.mark.asyncio
async def test_claim_omits_decomposition_outcomes_when_no_history(client: AsyncClient, db_session):
    """Claim endpoint omits decomposition_outcomes when research_history has no decomposition_quality data."""
    from sqlalchemy.orm.attributes import flag_modified

    direction_id = str(uuid.uuid4())
    project = Project(name="Decomp Empty Test", user_id="test-user-decomp-empty")
    project.directions = [
        {
            "id": direction_id,
            "title": "No decomp data",
            "priority": "low",
            "research_context": "",
            "research_history": [
                {"tasks_created": 2, "prs_merged": 1},
                {"tasks_created": 3, "prs_merged": 2, "decomposition_quality": []},
            ],
        }
    ]
    flag_modified(project, "directions")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "plan", "prompt": "Decompose nothing"}],
        vm_instance_id="agent-vm-decomp-empty",
        context={"direction_id": direction_id},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-decomp-empty")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200

    dc = resp.json().get("direction_context", {}) or {}
    assert "decomposition_outcomes" not in dc


@pytest.mark.asyncio
async def test_claim_does_not_inject_decomposition_outcomes_for_create_task(client: AsyncClient, db_session):
    """Claim endpoint omits decomposition_outcomes (plan-only signal) for create tasks."""
    from sqlalchemy.orm.attributes import flag_modified

    direction_id = str(uuid.uuid4())
    project = Project(name="Decomp Create Skip Test", user_id="test-user-decomp-skip")
    project.directions = [
        {
            "id": direction_id,
            "title": "Has decomp data",
            "priority": "high",
            "research_context": "",
            "research_history": [
                {"tasks_created": 2, "prs_merged": 2, "decomposition_quality": [
                    {"plan_task_id": "p1", "subtask_count": 2, "subtask_merge_rate": 1.0,
                     "avg_files_per_subtask": 2.0, "max_files_in_subtask": 3,
                     "dependency_depth": 1, "cross_module_count": 0},
                ]},
            ],
        }
    ]
    flag_modified(project, "directions")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "Implement change"}],
        vm_instance_id="agent-vm-decomp-create-skip",
        context={"direction_id": direction_id},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-decomp-create-skip")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200

    dc = resp.json().get("direction_context", {}) or {}
    assert "decomposition_outcomes" not in dc, (
        f"decomposition_outcomes should be plan-only, got direction_context keys: {list(dc.keys())}"
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
async def test_claim_injects_architecture_context_for_create_task(client: AsyncClient, db_session):
    """Claim endpoint injects architecture_context in project_context for create tasks when related_files match _file_to_system."""
    from sqlalchemy.orm.attributes import flag_modified

    design_doc = {
        "_file_to_system": {
            "backend/app/auth.py": "auth",
            "backend/app/models.py": "database",
        },
        "systems/auth": {
            "name": "Auth",
            "status": "functional",
            "ready_for_features": True,
            "description": "Handles authentication and JWT tokens.",
            "modules": ["backend/app/auth.py"],
        },
        "systems/database": {
            "name": "Database",
            "status": "mature",
            "ready_for_features": True,
            "description": "SQLModel ORM layer and migrations.",
            "modules": ["backend/app/models.py"],
        },
    }
    project = Project(name="Arch Context Test", user_id="test-user-123", design_doc=design_doc)
    flag_modified(project, "design_doc")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "Add OAuth provider"}],
        vm_instance_id="agent-vm-arch-ctx-create",
        context={"related_files": ["backend/app/auth.py", "backend/app/models.py"]},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-arch-ctx-create")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200

    data = resp.json()
    project_ctx = data.get("project_context", {})
    assert "architecture_context" in project_ctx, (
        f"Expected architecture_context in project_context, got: {list(project_ctx.keys())}"
    )
    arch_ctx = project_ctx["architecture_context"]
    assert len(arch_ctx) == 2
    names = {s["name"] for s in arch_ctx}
    assert names == {"Auth", "Database"}
    auth_entry = next(s for s in arch_ctx if s["name"] == "Auth")
    assert auth_entry["status"] == "functional"
    assert auth_entry["ready_for_features"] is True
    assert "authentication" in auth_entry["description"]


@pytest.mark.asyncio
async def test_claim_does_not_inject_architecture_context_for_review_task(client: AsyncClient, db_session):
    """Claim endpoint does NOT inject architecture_context for review tasks."""
    from sqlalchemy.orm.attributes import flag_modified

    design_doc = {
        "_file_to_system": {"backend/app/auth.py": "auth"},
        "systems/auth": {
            "name": "Auth",
            "status": "functional",
            "ready_for_features": True,
            "description": "Auth system.",
            "modules": ["backend/app/auth.py"],
        },
    }
    project = Project(name="Arch Context Review Test", user_id="test-user-123", design_doc=design_doc)
    flag_modified(project, "design_doc")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "review", "pr_number": 99, "branch": "feat/review-branch"}],
        vm_instance_id="agent-vm-arch-ctx-review",
        context={"related_files": ["backend/app/auth.py"]},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-arch-ctx-review")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200

    data = resp.json()
    project_ctx = data.get("project_context", {})
    assert "architecture_context" not in project_ctx, (
        f"architecture_context should be absent for review tasks, got: {project_ctx.get('architecture_context')}"
    )


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_claim_tracks_arch_context_in_task_context(client: AsyncClient, db_session):
    """When arch context is injected at claim time, _had_arch_context=True and
    _shown_arch_system_names are persisted in task.context for effectiveness tracking."""
    from sqlalchemy.orm.attributes import flag_modified

    design_doc = {
        "_file_to_system": {
            "backend/app/auth.py": "auth",
            "backend/app/models.py": "database",
        },
        "systems/auth": {
            "name": "Auth",
            "status": "functional",
            "ready_for_features": True,
            "description": "Handles authentication.",
            "modules": ["backend/app/auth.py"],
        },
        "systems/database": {
            "name": "Database",
            "status": "mature",
            "ready_for_features": True,
            "description": "ORM layer.",
            "modules": ["backend/app/models.py"],
        },
    }
    project = Project(name="Arch Context Tracking Test", user_id="test-user-123", design_doc=design_doc)
    flag_modified(project, "design_doc")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "Add new feature"}],
        vm_instance_id="agent-vm-arch-track",
        context={"related_files": ["backend/app/auth.py", "backend/app/models.py"]},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-arch-track")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200

    await db_session.refresh(task)
    ctx = task.context or {}
    assert ctx.get("_had_arch_context") is True, (
        f"Expected _had_arch_context=True in task.context, got: {ctx.get('_had_arch_context')}"
    )
    shown = ctx.get("_shown_arch_system_names", [])
    assert set(shown) == {"auth", "database"}, (
        f"Expected system names auth, database in _shown_arch_system_names, got: {shown}"
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
    - 3rd retry: ~16 min delay (2^3 * 120 = 960s)
    - After 5 retries, task is not requeued (budget exhausted)
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
            # immediately exhaust the retry budget on the next dispatch.
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

    # 6th attempt: api_retries=5, increments to 6 > 5 → EXTERNAL_API requeue path is skipped
    # Falls through to general retry (task.attempt_count < max_retries), so no retry_after is set
    task = Task(
        project_id=project.id,
        payloads=[{"cmd": "test"}],
        vm_instance_id="agent-vm-backoff-6",
        status=TaskStatus.IN_PROGRESS,
        context={"_api_failure_retries": 5},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-backoff-6")
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
async def test_external_api_permanent_auth_failure_does_not_requeue(client: AsyncClient, db_session, mocker):
    """
    EXTERNAL_API failures with permanent auth indicators (token expired, 401, bad credentials)
    must fail immediately without consuming any retry budget. Requeuing only wastes 30+ minutes
    of exponential backoff before the task permanently fails with the same error.
    """
    mocker.patch("app.main.delete_agent_vm", return_value=True)
    mocker.patch("app.main._ensure_vm_deleted", new_callable=mocker.AsyncMock)
    mocker.patch("app.broadcast.broadcast_to_project", return_value=None)

    project = Project(name="Auth Failure Test", user_id="test-user-auth-fail")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    permanent_auth_cases = [
        "GitHub API returned 401 unauthorized",
        "token expired: please re-authenticate",
        "token revoked by organization admin",
        "bad credentials — check your PAT",
        "401 Unauthorized: access denied",
    ]

    for i, logs_msg in enumerate(permanent_auth_cases):
        task = Task(
            project_id=project.id,
            payloads=[{"type": "create", "prompt": "do work"}],
            vm_instance_id=f"agent-vm-auth-{i}",
            status=TaskStatus.IN_PROGRESS,
            context={"_api_failure_retries": 0},
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        token = create_agent_token(str(task.id), f"agent-vm-auth-{i}")
        headers = {"x-agent-token": token}
        payload = {
            "task_id": str(task.id),
            "status": "FAILED",
            "failure_class": "EXTERNAL_API",
            "logs": logs_msg,
        }
        resp = await client.post("/api/agent/status", json=payload, headers=headers)
        assert resp.status_code == 200, f"case {i}: unexpected status {resp.status_code}"

        await db_session.refresh(task)
        assert task.status == TaskStatus.FAILED, (
            f"case {i} ({logs_msg!r}): expected FAILED (immediate), got {task.status}"
        )
        # retry_after must not be set — permanent failures should not schedule a retry
        ctx = task.context or {}
        assert "retry_after" not in ctx, (
            f"case {i}: retry_after should not be set for permanent auth failure"
        )
        # Operator-facing message must direct to re-authentication
        assert task.latest_status_message is not None
        assert "auth" in task.latest_status_message.lower() or "re-authenticate" in task.latest_status_message.lower(), (
            f"case {i}: status message should mention auth/re-authenticate, got: {task.latest_status_message!r}"
        )


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
async def test_agent_claim_pipeline_health_includes_scope_conversion_rates(client: AsyncClient, db_session, mocker):
    """Claim response includes scope_conversion_rates and medium_scope_review_iterations in pipeline_health."""
    project = Project(name="Scope Merge Rate Test", user_id="test-user-smr")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "docs", "cmd": "write docs"}],
        vm_instance_id="agent-vm-smr-1",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    fake_health = {
        "success_rate": 0.92,
        "trend": "stable",
        "first_attempt_success_rate": {
            "overall": 0.88,
            "trend": "stable",
            "sample_size": 50,
            "by_type": {"create": 0.88},
        },
        "failure_distribution": {},
        "direction_stats": [],
        "pipeline_conversion_rate": None,
        "review_accuracy": None,
        "persistent_review_issues": None,
        "scope_conversion_rates": {
            "medium": {"current_rate": 0.75, "prev_rate": 0.5, "delta": 0.25, "trend": "improving"},
            "small": {"current_rate": 0.9, "prev_rate": 0.85, "delta": 0.05, "trend": "stable"},
        },
        "medium_scope_review_iterations": 2.5,
    }
    mocker.patch(
        "app.routers.admin_analytics.compute_pipeline_health_summary",
        return_value=fake_health,
    )

    token = create_agent_token(str(task.id), "agent-vm-smr-1")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200
    data = resp.json()
    assert "project_context" in data
    ph = data["project_context"]["pipeline_health"]
    assert ph is not None
    assert "scope_conversion_rates" in ph
    assert isinstance(ph["scope_conversion_rates"], dict)
    assert ph["scope_conversion_rates"]["medium"]["current_rate"] == 0.75
    assert ph["scope_conversion_rates"]["medium"]["trend"] == "improving"
    assert ph["medium_scope_review_iterations"] == 2.5


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
    assert data2["playbook"]["untagged"]["content"] == "untagged operational entry"


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
    assert data["playbook"]["build-hint"]["content"] == "build instruction"


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
    Playbook entries returned by claim are sorted by effectiveness_score descending.
    For entries without recent_outcomes data, the score falls back to confirmed/referenced ratio.
    Entries with higher confirmation rate per reference sort first.
    """
    project = Project(name="Playbook Order Test", user_id="test-user-order")
    project.design_doc = {
        "playbook/low-ratio": {
            "content": "low confirmation rate",
            "source_task_id": None,
            "updated_at": "2026-01-01T00:00:00",
            "confirmed_count": 1,
            "reference_count": 10,  # ratio = 0.1
        },
        "playbook/high-ratio": {
            "content": "high confirmation rate",
            "source_task_id": None,
            "updated_at": "2026-01-01T00:00:00",
            "confirmed_count": 5,
            "reference_count": 2,  # ratio = 2.5 (more confirmations than references — possible since all applicable tasks increment confirmed_count)
        },
        "playbook/mid-ratio": {
            "content": "medium confirmation rate",
            "source_task_id": None,
            "updated_at": "2026-01-01T00:00:00",
            "confirmed_count": 5,
            "reference_count": 8,  # ratio = 0.625
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
    # Sort by effectiveness score descending: high-ratio (2.5) → mid-ratio (0.625) → low-ratio (0.1)
    assert keys.index("high-ratio") < keys.index("mid-ratio"), f"Order: {keys}"
    assert keys.index("mid-ratio") < keys.index("low-ratio"), f"Order: {keys}"


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
async def test_playbook_recent_outcomes_flipped_on_first_attempt_success(
    client: AsyncClient, db_session, mocker
):
    """
    When a task with _shown_playbook_keys completes on first attempt, the most recent False
    in recent_outcomes is flipped to True and effectiveness_score is computed.
    Entries not shown to this task do not get their recent_outcomes updated.
    """
    mocker.patch("app.main.delete_agent_vm", return_value=True)

    project = Project(name="Outcomes Flip Test", user_id="test-user-outcomes")
    project.design_doc = {
        "playbook/shown-entry": {
            "content": "guidance shown to this task",
            "applicable_types": ["create"],
            "source_task_id": None,
            "updated_at": "2026-01-01T00:00:00",
            "confirmed_count": 1,
            "reference_count": 3,
            "recent_outcomes": [True, False, False],  # last False should be flipped
        },
        "playbook/not-shown-entry": {
            "content": "guidance not shown to this task",
            "applicable_types": ["create"],
            "source_task_id": None,
            "updated_at": "2026-01-01T00:00:00",
            "confirmed_count": 0,
            "reference_count": 2,
            "recent_outcomes": [False, False],  # should NOT be changed
        },
    }
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "do something"}],
        vm_instance_id="agent-vm-outcomes-flip",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=0,
        # Simulate that only 'shown-entry' was shown at claim time
        context={"_shown_playbook_keys": ["shown-entry"]},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-outcomes-flip")
    resp = await client.post(
        "/api/agent/status",
        json={"task_id": str(task.id), "status": "COMPLETED", "logs": "done"},
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 200

    await db_session.refresh(project)
    shown = project.design_doc["playbook/shown-entry"]
    not_shown = project.design_doc["playbook/not-shown-entry"]

    # Shown entry: last False flipped to True → [True, False, True]
    assert shown["recent_outcomes"] == [True, False, True], (
        f"Expected [True, False, True], got {shown['recent_outcomes']}"
    )
    # effectiveness_score computed: 2/3 ≈ 0.6667
    assert shown.get("effectiveness_score") is not None
    assert abs(shown["effectiveness_score"] - round(2/3, 4)) < 0.001
    # last_confirmed_at set
    assert shown.get("last_confirmed_at") is not None

    # Not-shown entry: recent_outcomes unchanged
    assert not_shown["recent_outcomes"] == [False, False], (
        f"Not-shown entry should not be updated, got {not_shown['recent_outcomes']}"
    )
    assert not_shown.get("effectiveness_score") is None


@pytest.mark.asyncio
async def test_playbook_auto_archive_after_10_unconfirmed_references(client: AsyncClient, db_session):
    """
    After 10 references with no confirmations, an entry accumulates 10 Falses in recent_outcomes
    (score=0.0 < 0.1 threshold) and is auto-archived (stale=True) during reference tracking.
    Subsequent claims skip the stale entry.
    """
    from sqlalchemy.orm.attributes import flag_modified

    project = Project(name="Auto Archive Ref Test", user_id="test-user-autoarchive")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Entry pre-loaded with 9 Falses — one more reference will fill the window (10 Falses → stale)
    project.design_doc = {
        "playbook/zero-effectiveness": {
            "content": "guidance that is never confirmed",
            "source_task_id": "seed",
            "updated_at": "2026-01-01T00:00:00",
            "confirmed_count": 0,
            "reference_count": 9,
            "recent_outcomes": [False] * 9,  # 9 unconfirmed references so far
        },
        "playbook/active-entry": {
            "content": "currently active guidance",
            "source_task_id": "seed",
            "updated_at": "2026-01-01T00:00:00",
            "confirmed_count": 2,
            "reference_count": 3,
        },
    }
    flag_modified(project, "design_doc")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # First claim: shows both entries, appends False to zero-effectiveness → 10 Falses → stale=True
    task1 = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "First task"}],
        vm_instance_id="agent-vm-autoarchive-1",
    )
    db_session.add(task1)
    await db_session.commit()
    await db_session.refresh(task1)

    token1 = create_agent_token(str(task1.id), "agent-vm-autoarchive-1")
    resp1 = await client.post("/api/agent/claim", headers={"x-agent-token": token1})
    assert resp1.status_code == 200

    # After first claim: zero-effectiveness should now have stale=True in DB
    await db_session.refresh(project)
    entry = project.design_doc["playbook/zero-effectiveness"]
    assert entry.get("stale") is True, f"Expected stale=True after 10 Falses, got: {entry}"
    assert entry.get("effectiveness_score") == 0.0

    # Second claim: zero-effectiveness should be excluded (stale=True)
    task2 = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "Second task"}],
        vm_instance_id="agent-vm-autoarchive-2",
    )
    db_session.add(task2)
    await db_session.commit()
    await db_session.refresh(task2)

    token2 = create_agent_token(str(task2.id), "agent-vm-autoarchive-2")
    resp2 = await client.post("/api/agent/claim", headers={"x-agent-token": token2})
    assert resp2.status_code == 200

    data2 = resp2.json()
    playbook2 = data2.get("playbook", {})
    assert "zero-effectiveness" not in playbook2, "Auto-archived entry should not appear in subsequent claims"
    assert "active-entry" in playbook2, "Active entry should still appear"


@pytest.mark.asyncio
async def test_playbook_admin_list_stale_flag(client: AsyncClient, db_session):
    """
    Admin playbook list endpoint returns reference_count, last_referenced_at, stale flag,
    effectiveness_score, and last_confirmed_at.
    Stale is True if stored stale=True (auto-archived) OR no reference in 30+ days.
    Auth is mocked by conftest to test-user-123 (admin).
    """
    from app.models import ProjectMember

    project = Project(name="Playbook Stale Test", user_id="test-user-123")
    project.design_doc = {
        "playbook/fresh-entry": {
            "content": "fresh content",
            "updated_at": "2026-04-10T00:00:00",
            "confirmed_count": 3,
            "reference_count": 5,
            "last_referenced_at": "2026-04-13T00:00:00",  # 1 day ago — not stale
            "last_confirmed_at": "2026-04-12T00:00:00",
            "effectiveness_score": 0.6,
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
        "playbook/auto-archived-entry": {
            "content": "persistently ineffective",
            "updated_at": "2026-04-01T00:00:00",
            "confirmed_count": 0,
            "reference_count": 10,
            "last_referenced_at": "2026-04-13T00:00:00",  # recent but stale=True stored
            "stale": True,
            "effectiveness_score": 0.0,
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
    assert fresh["last_confirmed_at"] == "2026-04-12T00:00:00"
    assert fresh["effectiveness_score"] == 0.6
    assert fresh["stale"] is False

    stale_entry = entries["stale-entry"]
    assert stale_entry["reference_count"] == 0
    assert stale_entry["last_referenced_at"] is None
    assert stale_entry["last_confirmed_at"] is None
    assert stale_entry["effectiveness_score"] is None
    assert stale_entry["stale"] is True

    stale_ref = entries["stale-ref-entry"]
    assert stale_ref["reference_count"] == 3
    assert stale_ref["stale"] is True

    # Auto-archived entry: stored stale=True takes precedence even though last_referenced_at is recent
    auto_archived = entries["auto-archived-entry"]
    assert auto_archived["stale"] is True
    assert auto_archived["effectiveness_score"] == 0.0


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


@pytest.mark.asyncio
async def test_claim_top_first_review_issues_populated(client: AsyncClient, db_session):
    """top_first_review_issues is populated when review_history contains issues with descriptions at iteration 0."""
    from sqlalchemy.orm.attributes import flag_modified

    project = Project(name="Top Review Issues Test", user_id="test-user-tri")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Create two completed tasks with review_history entries that have issues at iteration 0
    common_issue = "Missing test coverage for the new function"
    for i in range(2):
        ht = Task(
            project_id=project.id,
            payloads=[{"type": "create", "prompt": f"Some task {i}"}],
            vm_instance_id=f"agent-vm-tri-{i}",
            status=TaskStatus.COMPLETED,
            context={
                "review_loop": {
                    "iteration": 2,
                    "review_history": [
                        {
                            "iteration": 0,
                            "feedback": "Needs work",
                            "issues": [
                                {"severity": "high", "description": common_issue, "file": "app/foo.py", "line": 10},
                            ],
                        }
                    ],
                }
            },
        )
        flag_modified(ht, "context")
        db_session.add(ht)
    await db_session.commit()

    # Create the task to claim
    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "New feature"}],
        vm_instance_id="agent-vm-tri-claim",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-tri-claim")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200

    project_ctx = resp.json().get("project_context", {})
    top_issues = project_ctx.get("top_first_review_issues")
    assert top_issues is not None, "top_first_review_issues should be populated"
    assert len(top_issues) >= 1
    descriptions = [issue["description"] for issue in top_issues]
    assert common_issue.lower()[:120] in descriptions


@pytest.mark.asyncio
async def test_claim_direction_top_first_review_issues_populated(client: AsyncClient, db_session):
    """direction_context.top_first_review_issues is populated when a direction has 3+ tasks with review history and a recurring issue."""
    from sqlalchemy.orm.attributes import flag_modified

    direction_id = str(uuid.uuid4())
    project = Project(name="Direction Top Review Issues Test", user_id="test-user-dtri")
    project.directions = [
        {
            "id": direction_id,
            "title": "Refactor auth",
            "priority": "medium",
            "research_context": "",
            "research_history": [],
        }
    ]
    flag_modified(project, "directions")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    direction_issue = "Direction-scoped missing validation on inputs"
    other_issue = "Unique issue seen only once"
    # 3 tasks in this direction with review history; 2 share the recurring issue.
    for i in range(3):
        issues = [{"severity": "high", "description": direction_issue, "file": "a.py", "line": 1}]
        if i == 2:
            issues = [{"severity": "low", "description": other_issue, "file": "b.py", "line": 2}]
        ht = Task(
            project_id=project.id,
            payloads=[{"type": "create", "prompt": f"dir task {i}"}],
            vm_instance_id=f"agent-vm-dtri-{i}",
            status=TaskStatus.COMPLETED,
            context={
                "direction_id": direction_id,
                "review_loop": {
                    "iteration": 2,
                    "review_history": [
                        {"iteration": 0, "feedback": "fix", "issues": issues},
                    ],
                },
            },
        )
        flag_modified(ht, "context")
        db_session.add(ht)
    await db_session.commit()

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "new in direction"}],
        vm_instance_id="agent-vm-dtri-claim",
        context={"direction_id": direction_id},
    )
    flag_modified(task, "context")
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-dtri-claim")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200

    dc = resp.json().get("direction_context", {})
    dir_top = dc.get("top_first_review_issues")
    assert dir_top, "direction_context.top_first_review_issues should be populated"
    descriptions = [issue["description"] for issue in dir_top]
    assert direction_issue.lower()[:120] in descriptions
    assert other_issue.lower()[:120] not in descriptions


@pytest.mark.asyncio
async def test_claim_direction_top_first_review_issues_gated_by_min_tasks(client: AsyncClient, db_session):
    """direction_context.top_first_review_issues stays None when fewer than 3 direction-scoped tasks have review history."""
    from sqlalchemy.orm.attributes import flag_modified

    direction_id = str(uuid.uuid4())
    project = Project(name="Direction Top Review Issues Gated", user_id="test-user-dtri-gate")
    project.directions = [
        {
            "id": direction_id,
            "title": "Refactor auth",
            "priority": "medium",
            "research_context": "",
            "research_history": [],
        }
    ]
    flag_modified(project, "directions")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    shared_issue = "Recurring direction issue"
    # Only 2 direction-scoped tasks with review history — below the 3-task threshold.
    for i in range(2):
        ht = Task(
            project_id=project.id,
            payloads=[{"type": "create", "prompt": f"dir task {i}"}],
            vm_instance_id=f"agent-vm-dtri-gate-{i}",
            status=TaskStatus.COMPLETED,
            context={
                "direction_id": direction_id,
                "review_loop": {
                    "iteration": 2,
                    "review_history": [
                        {
                            "iteration": 0,
                            "feedback": "fix",
                            "issues": [{"severity": "high", "description": shared_issue, "file": "a.py", "line": 1}],
                        },
                    ],
                },
            },
        )
        flag_modified(ht, "context")
        db_session.add(ht)
    await db_session.commit()

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "new in direction"}],
        vm_instance_id="agent-vm-dtri-gate-claim",
        context={"direction_id": direction_id},
    )
    flag_modified(task, "context")
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-dtri-gate-claim")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200

    dc = resp.json().get("direction_context", {})
    assert dc.get("top_first_review_issues") is None


async def _setup_smoketest_task(db_session, vm_id: str, base_branch: str = "main"):
    """Create a project + IN_PROGRESS smoketest task.

    Leaving attempt_count at 0 keeps the task on the normal-retry path on FAILED,
    so the permanent-failure auto-debug spawn does not also fire — isolating the
    SMOKETEST plan-task spawn behavior under test.
    """
    project = Project(name=f"Smoketest {vm_id}", user_id="test-user-smoketest")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "smoketest", "base_branch": base_branch}],
        vm_instance_id=vm_id,
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    return project, task


@pytest.mark.asyncio
async def test_smoketest_startup_failure_spawns_plan_task_priority_90(client: AsyncClient, db_session, mocker):
    """SMOKETEST_RESULT with a startup-suite failure spawns a plan task at priority 90."""
    mocker.patch("app.main.delete_agent_vm", return_value=None)
    mocker.patch("app.main._ensure_vm_deleted", new_callable=mocker.AsyncMock)
    mocker.patch("app.broadcast.broadcast_to_project", return_value=None)

    _, task = await _setup_smoketest_task(db_session, "agent-vm-smoke-startup")

    import json as _json  # noqa: PLC0415
    smoketest_payload = {
        "passed": False,
        "tests": [
            {"name": "startup_probe", "status": "fail", "message": "port unreachable"},
        ],
        "suites": {
            "startup": {"passed": False, "tests": [{"name": "startup_probe", "status": "fail"}]},
            "curated": {"passed": True, "tests": []},
        },
    }
    token = create_agent_token(str(task.id), "agent-vm-smoke-startup")
    payload = {
        "task_id": str(task.id),
        "status": "FAILED",
        "artifact_type": "SMOKETEST_RESULT",
        "logs": _json.dumps(smoketest_payload),
    }
    resp = await client.post("/api/agent/status", json=payload, headers={"x-agent-token": token})
    assert resp.status_code == 200

    result = await db_session.execute(select(Task).where(Task.parent_task_id == task.id))
    spawned = result.scalars().all()
    assert len(spawned) == 1, f"Expected 1 spawned plan task, got {len(spawned)}"
    plan_task = spawned[0]
    assert plan_task.priority == 90
    assert plan_task.first_payload.get("type") == "plan"
    assert plan_task.status == TaskStatus.QUEUED


@pytest.mark.asyncio
async def test_smoketest_curated_failure_spawns_plan_task_priority_70(client: AsyncClient, db_session, mocker):
    """SMOKETEST_RESULT with a curated-suite failure (startup ok) spawns plan task at priority 70."""
    mocker.patch("app.main.delete_agent_vm", return_value=None)
    mocker.patch("app.main._ensure_vm_deleted", new_callable=mocker.AsyncMock)
    mocker.patch("app.broadcast.broadcast_to_project", return_value=None)

    _, task = await _setup_smoketest_task(db_session, "agent-vm-smoke-curated")

    import json as _json  # noqa: PLC0415
    smoketest_payload = {
        "passed": False,
        "tests": [{"name": "login_flow", "status": "fail", "message": "assertion failed"}],
        "suites": {
            "startup": {"passed": True, "tests": []},
            "curated": {"passed": False, "tests": [{"name": "login_flow", "status": "fail"}]},
        },
    }
    token = create_agent_token(str(task.id), "agent-vm-smoke-curated")
    payload = {
        "task_id": str(task.id),
        "status": "FAILED",
        "artifact_type": "SMOKETEST_RESULT",
        "logs": _json.dumps(smoketest_payload),
    }
    resp = await client.post("/api/agent/status", json=payload, headers={"x-agent-token": token})
    assert resp.status_code == 200

    result = await db_session.execute(select(Task).where(Task.parent_task_id == task.id))
    spawned = result.scalars().all()
    assert len(spawned) == 1
    assert spawned[0].priority == 70
    assert spawned[0].first_payload.get("type") == "plan"


@pytest.mark.asyncio
async def test_smoketest_passing_does_not_spawn_plan_task(client: AsyncClient, db_session, mocker):
    """SMOKETEST_RESULT with passed=true (no failures) does not spawn any child task."""
    mocker.patch("app.main.delete_agent_vm", return_value=None)
    mocker.patch("app.main._ensure_vm_deleted", new_callable=mocker.AsyncMock)
    mocker.patch("app.broadcast.broadcast_to_project", return_value=None)

    _, task = await _setup_smoketest_task(db_session, "agent-vm-smoke-ok")

    import json as _json  # noqa: PLC0415
    # Even when status=FAILED is reported, a passed=true smoketest payload must not spawn.
    smoketest_payload = {
        "passed": True,
        "tests": [{"name": "startup_probe", "status": "pass"}],
        "suites": {"startup": {"passed": True, "tests": []}},
    }
    token = create_agent_token(str(task.id), "agent-vm-smoke-ok")
    payload = {
        "task_id": str(task.id),
        "status": "FAILED",
        "artifact_type": "SMOKETEST_RESULT",
        "logs": _json.dumps(smoketest_payload),
    }
    resp = await client.post("/api/agent/status", json=payload, headers={"x-agent-token": token})
    assert resp.status_code == 200

    result = await db_session.execute(select(Task).where(Task.parent_task_id == task.id))
    assert result.scalars().all() == []


@pytest.mark.asyncio
async def test_debug_result_high_confidence_spawns_fix_task(client: AsyncClient, db_session, mocker):
    """DEBUG_RESULT with confidence='high' and fix_prompt spawns a create-type fix task."""
    mocker.patch("app.main.delete_agent_vm", return_value=None)
    mocker.patch("app.main._ensure_vm_deleted", new_callable=mocker.AsyncMock)
    mocker.patch("app.broadcast.broadcast_to_project", return_value=None)

    project = Project(name="Debug High", user_id="test-user-debug-high")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Debug: flaky login test",
        payloads=[{"type": "debug", "base_branch": "main"}],
        vm_instance_id="agent-vm-debug-high",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=3,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    import json as _json  # noqa: PLC0415
    debug_payload = {
        "confidence": "high",
        "fix_prompt": "Update login handler to normalize email before lookup",
        "root_cause": "Case-sensitive email comparison",
    }
    token = create_agent_token(str(task.id), "agent-vm-debug-high")
    payload = {
        "task_id": str(task.id),
        "status": "COMPLETED",
        "artifact_type": "DEBUG_RESULT",
        "logs": _json.dumps(debug_payload),
    }
    resp = await client.post("/api/agent/status", json=payload, headers={"x-agent-token": token})
    assert resp.status_code == 200

    result = await db_session.execute(
        select(Task).where(Task.project_id == project.id, Task.id != task.id)
    )
    spawned = result.scalars().all()
    assert len(spawned) == 1
    fix_task = spawned[0]
    assert fix_task.first_payload.get("type") == "create"
    assert fix_task.first_payload.get("prompt") == debug_payload["fix_prompt"]
    assert fix_task.status == TaskStatus.QUEUED
    assert fix_task.title == "Fix: flaky login test"


@pytest.mark.asyncio
async def test_debug_result_low_confidence_does_not_spawn_fix_task(client: AsyncClient, db_session, mocker):
    """DEBUG_RESULT with confidence='low' records plan_output but does not spawn a fix task."""
    mocker.patch("app.main.delete_agent_vm", return_value=None)
    mocker.patch("app.main._ensure_vm_deleted", new_callable=mocker.AsyncMock)
    mocker.patch("app.broadcast.broadcast_to_project", return_value=None)

    project = Project(name="Debug Low", user_id="test-user-debug-low")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Debug: intermittent 500 error",
        payloads=[{"type": "debug", "base_branch": "main"}],
        vm_instance_id="agent-vm-debug-low",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=3,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    import json as _json  # noqa: PLC0415
    debug_payload = {
        "confidence": "low",
        "fix_prompt": "Possibly add a retry, but unclear",
        "root_cause": "Unknown — multiple candidate causes",
    }
    token = create_agent_token(str(task.id), "agent-vm-debug-low")
    payload = {
        "task_id": str(task.id),
        "status": "COMPLETED",
        "artifact_type": "DEBUG_RESULT",
        "logs": _json.dumps(debug_payload),
    }
    resp = await client.post("/api/agent/status", json=payload, headers={"x-agent-token": token})
    assert resp.status_code == 200

    result = await db_session.execute(
        select(Task).where(Task.project_id == project.id, Task.id != task.id)
    )
    assert result.scalars().all() == []

    await db_session.refresh(task)
    assert task.plan_output == debug_payload
