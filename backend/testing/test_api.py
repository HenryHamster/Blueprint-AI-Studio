import uuid
import pytest
from httpx import AsyncClient
from app.models import Project, ProjectMember, ProjectStatus, Task, TaskStatus
from app.auth import create_agent_token

@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_read_root(client: AsyncClient):
    """
    Tests the basic health check endpoint.
    """
    response = await client.get("/")
    assert response.status_code == 200
    assert response.json() == {"status": "online", "service": "Blueprint Orchestrator"}

@pytest.mark.asyncio
async def test_deploy_blueprint(client: AsyncClient, mocker):
    """
    Tests the POST /api/deploy endpoint.
    We mock the GCP Compute call so no real VM is spun up.
    """
    # Mock the background task so it doesn't actually try to provision a VM
    mocker.patch("app.main.provision_agent_vm", return_value=None)

    payload = {
        "name": "Test Project",
        "payloads": [{"command": "echo hello"}],
        "storage_uris": []
    }

    response = await client.post("/api/deploy", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "accepted"
    assert "project_id" in data

    # Verify it was saved in the isolated in-memory DB
    project_id = data["project_id"]
    get_response = await client.get(f"/api/projects/{project_id}")
    assert get_response.status_code == 200
    project_data = get_response.json()
    assert project_data["name"] == "Test Project"
    assert project_data["status"] == ProjectStatus.DEPLOYING

@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_list_projects_empty(client: AsyncClient):
    """
    Tests that a fresh database returns an empty list of projects.
    """
    response = await client.get("/api/projects")
    assert response.status_code == 200
    data = response.json()
    assert data["projects"] == []
    assert "pagination" in data

@pytest.mark.asyncio
async def test_get_project_tasks(client: AsyncClient, mocker):
    """
    Tests retrieving tasks and artifacts for a specific project.
    """
    mocker.patch("app.main.provision_agent_vm", return_value=None)

    # 1. Create project
    payload = {
        "name": "Test Project Tasks",
        "payloads": [{"command": "echo hello"}],
        "storage_uris": []
    }
    create_resp = await client.post("/api/deploy", json=payload)
    project_id = create_resp.json()["project_id"]

    # 2. Get tasks — deploy creates 1 parent task + 1 child task per payload
    resp = await client.get(f"/api/projects/{project_id}/tasks")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["tasks"]) == 2
    # The child task (has a parent_task_id) carries the original payload
    child_tasks = [t for t in data["tasks"] if t["parent_task_id"] is not None]
    assert len(child_tasks) == 1
    assert child_tasks[0]["payloads"] == [{"command": "echo hello"}]

@pytest.mark.asyncio
async def test_get_project_tasks_not_found(client: AsyncClient):
    """
    Tests that requesting tasks for a non-existent project returns 404.
    """
    import uuid
    fake_id = str(uuid.uuid4())
    resp = await client.get(f"/api/projects/{fake_id}/tasks")
    assert resp.status_code == 404


# --- Validation and Error Handling Tests ---

@pytest.mark.asyncio
async def test_deploy_missing_name_returns_422(client: AsyncClient):
    """
    POST /api/deploy without the required `name` field should return 422.
    """
    payload = {
        "payloads": [{"command": "echo hello"}]
    }
    response = await client.post("/api/deploy", json=payload)
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_deploy_null_name_returns_422(client: AsyncClient):
    """
    POST /api/deploy with `name` set to null should return 422 since name is required str.
    """
    payload = {
        "name": None,
        "payloads": [{"command": "echo hello"}]
    }
    response = await client.post("/api/deploy", json=payload)
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_deploy_extra_fields_are_ignored(client: AsyncClient, mocker):
    """
    POST /api/deploy with extra unexpected fields — DeployPayload has no extra="forbid",
    so Pydantic v2 silently ignores unknown fields and the request should succeed.
    """
    mocker.patch("app.main.provision_agent_vm", return_value=None)
    payload = {
        "name": "Test Extra Fields",
        "payloads": [{"command": "echo hello"}],
        "unexpected_field": "should be ignored",
        "another_extra": 42
    }
    response = await client.post("/api/deploy", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "accepted"
    assert "project_id" in data


@pytest.mark.asyncio
async def test_get_project_different_user_returns_404(client: AsyncClient, db_session):
    """
    GET /api/projects/{id} where the project belongs to a different user_id should
    return 404 (not 403), because the app filters by user_id and treats it as not found.
    """
    other_project = Project(
        name="Other User's Project",
        user_id="other-user-456",
        status=ProjectStatus.DEPLOYING
    )
    db_session.add(other_project)
    await db_session.commit()
    await db_session.refresh(other_project)

    response = await client.get(f"/api/projects/{other_project.id}")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_get_project_tasks_different_user_returns_404(client: AsyncClient, db_session):
    """
    GET /api/projects/{id}/tasks where the project belongs to a different user_id should
    return 404, since the app verifies project ownership before fetching tasks.
    """
    other_project = Project(
        name="Other User's Tasks Project",
        user_id="other-user-456",
        status=ProjectStatus.DEPLOYING
    )
    db_session.add(other_project)
    await db_session.commit()
    await db_session.refresh(other_project)

    response = await client.get(f"/api/projects/{other_project.id}/tasks")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_list_projects_pagination_beyond_results(client: AsyncClient, db_session):
    """
    GET /api/projects?limit=5&offset=10 when the DB only has 3 projects for the current
    user should return an empty projects list with correct pagination metadata.
    """
    for i in range(3):
        project = Project(
            name=f"Pagination Project {i}",
            user_id="test-user-123",
            status=ProjectStatus.DEPLOYING
        )
        db_session.add(project)
    await db_session.commit()

    response = await client.get("/api/projects?limit=5&offset=10")
    assert response.status_code == 200
    data = response.json()
    assert data["projects"] == []
    assert "pagination" in data
    assert data["pagination"]["limit"] == 5
    assert data["pagination"]["offset"] == 10
    assert data["pagination"]["count"] == 0


@pytest.mark.asyncio
async def test_list_projects_status_filter(client: AsyncClient, db_session):
    """
    GET /api/projects?status=COMPLETED should return only projects with COMPLETED status.
    """
    completed_project = Project(
        name="Completed Project",
        user_id="test-user-123",
        status=ProjectStatus.COMPLETED
    )
    deploying_project = Project(
        name="Deploying Project",
        user_id="test-user-123",
        status=ProjectStatus.DEPLOYING
    )
    failed_project = Project(
        name="Failed Project",
        user_id="test-user-123",
        status=ProjectStatus.FAILED
    )
    db_session.add(completed_project)
    db_session.add(deploying_project)
    db_session.add(failed_project)
    await db_session.commit()
    for p in [completed_project, deploying_project, failed_project]:
        await db_session.refresh(p)
        db_session.add(ProjectMember(project_id=p.id, user_id="test-user-123", role="owner"))
    await db_session.commit()

    response = await client.get("/api/projects?status=COMPLETED")
    assert response.status_code == 200
    data = response.json()
    assert len(data["projects"]) == 1
    assert data["projects"][0]["name"] == "Completed Project"
    assert data["projects"][0]["status"] == ProjectStatus.COMPLETED


@pytest.mark.asyncio
async def test_ws_ticket_nonexistent_project_returns_404(client: AsyncClient):
    """
    POST /api/projects/{id}/ws-ticket on a non-existent project should return 404.
    """
    fake_id = str(uuid.uuid4())
    response = await client.post(f"/api/projects/{fake_id}/ws-ticket")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_fix_task_receives_project_context(client: AsyncClient, db_session):
    """
    Claiming a fix-type task should include project_context in the response,
    the same as research/plan/create tasks.
    """
    project = Project(
        name="Fix Context Test Project",
        user_id="test-user-123",
        status=ProjectStatus.DEPLOYING,
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    vm_id = "test-vm-fix-001"
    task = Task(
        project_id=project.id,
        title="Fix a bug",
        status=TaskStatus.QUEUED,
        vm_instance_id=vm_id,
        payloads=[{"type": "fix", "prompt": "Fix the null pointer exception"}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), vm_id)
    response = await client.post(
        "/api/agent/claim",
        headers={"X-Agent-Token": token},
    )
    assert response.status_code == 200
    data = response.json()
    assert "project_context" in data
    assert "active_tasks" in data["project_context"]
    assert "sibling_tasks" in data["project_context"]
    assert "open_prs" in data["project_context"]


# --- Bulk Action Tests ---

async def _make_task(db_session, project, status, requires_approval=False):
    """Helper: create a task belonging to a project with test user as member."""
    task = Task(
        project_id=project.id,
        title="Test task",
        status=status,
        payloads=[{"type": "fix", "prompt": "do something"}],
        requires_approval=requires_approval,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    return task


async def _make_project_with_member(db_session):
    """Helper: create a project with test-user-123 as member."""
    project = Project(
        name="Bulk Test Project",
        user_id="test-user-123",
        status=ProjectStatus.DEPLOYING,
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(ProjectMember(project_id=project.id, user_id="test-user-123", role="owner"))
    await db_session.commit()
    return project


@pytest.mark.asyncio
async def test_bulk_action_invalid_action(client: AsyncClient):
    """POST /api/admin/tasks/bulk-action with unknown action returns 400."""
    response = await client.post(
        "/api/admin/tasks/bulk-action",
        json={"action": "delete", "task_ids": []},
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_bulk_action_retry_requeues_tasks(client: AsyncClient, db_session, mocker):
    """Bulk retry sets status=QUEUED, assigns new vm_instance_id, and creates TaskHistory."""
    mocker.patch("app.main._ensure_vm_deleted", return_value=None)

    project = await _make_project_with_member(db_session)
    task1 = await _make_task(db_session, project, TaskStatus.FAILED)
    task2 = await _make_task(db_session, project, TaskStatus.FAILED)

    response = await client.post(
        "/api/admin/tasks/bulk-action",
        json={"action": "retry", "task_ids": [str(task1.id), str(task2.id)]},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["success_count"] == 2
    assert data["error_count"] == 0
    assert all(r["status"] == "ok" for r in data["results"])

    # Verify task state via the admin GET endpoint (avoids SQLite session isolation)
    get_resp = await client.get(f"/api/admin/tasks/{task1.id}")
    assert get_resp.status_code == 200
    t1_data = get_resp.json()
    assert t1_data["status"] == TaskStatus.QUEUED


@pytest.mark.asyncio
async def test_bulk_action_approve_clears_approval_flag(client: AsyncClient, db_session):
    """Bulk approve clears requires_approval and creates TaskHistory."""
    project = await _make_project_with_member(db_session)
    task1 = await _make_task(db_session, project, TaskStatus.QUEUED, requires_approval=True)
    task2 = await _make_task(db_session, project, TaskStatus.QUEUED, requires_approval=True)

    response = await client.post(
        "/api/admin/tasks/bulk-action",
        json={"action": "approve", "task_ids": [str(task1.id), str(task2.id)]},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["success_count"] == 2
    assert data["error_count"] == 0

    # Verify via GET endpoint
    get_resp = await client.get(f"/api/admin/tasks/{task1.id}")
    assert get_resp.status_code == 200
    t1_data = get_resp.json()
    assert t1_data["requires_approval"] is False


@pytest.mark.asyncio
async def test_bulk_action_partial_errors(client: AsyncClient, db_session, mocker):
    """Invalid task IDs in the batch produce per-task errors without failing the whole request."""
    mocker.patch("app.main._ensure_vm_deleted", return_value=None)

    project = await _make_project_with_member(db_session)
    valid_task = await _make_task(db_session, project, TaskStatus.FAILED)

    response = await client.post(
        "/api/admin/tasks/bulk-action",
        json={
            "action": "retry",
            "task_ids": [str(valid_task.id), "not-a-uuid", str(uuid.uuid4())],
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["success_count"] == 1
    assert data["error_count"] == 2
    statuses = {r["task_id"]: r["status"] for r in data["results"]}
    assert statuses[str(valid_task.id)] == "ok"
    assert statuses["not-a-uuid"] == "error"


@pytest.mark.asyncio
async def test_bulk_action_cancel_sets_cancelled(client: AsyncClient, db_session, mocker):
    """Bulk cancel sets status=CANCELLED and creates TaskHistory."""
    mocker.patch("app.main._ensure_vm_deleted", return_value=None)
    mocker.patch("app.max_plan_pool.release_max_plan_account", return_value=None)

    project = await _make_project_with_member(db_session)
    task1 = await _make_task(db_session, project, TaskStatus.IN_PROGRESS)
    task2 = await _make_task(db_session, project, TaskStatus.QUEUED)

    response = await client.post(
        "/api/admin/tasks/bulk-action",
        json={"action": "cancel", "task_ids": [str(task1.id), str(task2.id)]},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["success_count"] == 2
    assert data["error_count"] == 0

    get_resp = await client.get(f"/api/admin/tasks/{task1.id}")
    assert get_resp.status_code == 200
    assert get_resp.json()["status"] == TaskStatus.CANCELLED


@pytest.mark.asyncio
async def test_bulk_action_cancel_skips_terminal_tasks(client: AsyncClient, db_session, mocker):
    """Bulk cancel returns error for already-terminal tasks."""
    mocker.patch("app.main._ensure_vm_deleted", return_value=None)

    project = await _make_project_with_member(db_session)
    task = await _make_task(db_session, project, TaskStatus.COMPLETED)

    response = await client.post(
        "/api/admin/tasks/bulk-action",
        json={"action": "cancel", "task_ids": [str(task.id)]},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["success_count"] == 0
    assert data["error_count"] == 1
    assert "terminal" in data["results"][0]["error"]
