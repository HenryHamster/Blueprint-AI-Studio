"""Tests for the agent task-status fetch endpoints
(app.routers.agent_tasks):
  GET /api/agent/projects/{project_id}/tasks/{task_id}
  GET /api/agent/projects/{project_id}/tasks

Covers: auth/project scoping, STRATEGIC_AGENT-only visibility,
status/outcome fields, PR URL and error-reason extraction, list ordering
and limit, and cross-project 404 semantics.
"""

import uuid

import pytest
from httpx import AsyncClient

from app.auth import create_agent_token
from app.models import Artifact, ArtifactType, Project, Task, TaskStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_project_and_task(db_session, *, vm: str = "agent-vm-tasks", name: str = "Tasks Test"):
    """Create a minimal project + calling task (used as the JWT subject)."""
    project = Project(name=name, user_id="test-user-tasks")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # The JWT subject task — represents the strategic session calling the endpoint.
    calling_task = Task(project_id=project.id, payloads=[{}], vm_instance_id=vm)
    db_session.add(calling_task)
    await db_session.commit()
    await db_session.refresh(calling_task)
    return project, calling_task


async def _make_strategic_task(
    db_session,
    project: Project,
    *,
    status: TaskStatus = TaskStatus.IN_PROGRESS,
    task_type: str = "research",
    title: str = "Test strategic task",
    latest_status_message: str | None = None,
    blocked_reason: str | None = None,
    park_reason: str | None = None,
    blocker_kind: str | None = None,
) -> Task:
    """Create a task with created_by=STRATEGIC_AGENT in the given project."""
    task = Task(
        project_id=project.id,
        payloads=[{"type": task_type}],
        title=title,
        status=status,
        created_by="STRATEGIC_AGENT",
        latest_status_message=latest_status_message,
        blocked_reason=blocked_reason,
        park_reason=park_reason,
        blocker_kind=blocker_kind,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    return task


async def _add_artifact(
    db_session,
    task: Task,
    *,
    artifact_type: ArtifactType,
    pr_url: str | None = None,
    logs: str | None = None,
) -> Artifact:
    a = Artifact(
        task_id=task.id,
        artifact_type=artifact_type,
        pr_url=pr_url,
        logs=logs,
    )
    db_session.add(a)
    await db_session.commit()
    await db_session.refresh(a)
    return a


# ---------------------------------------------------------------------------
# GET /api/agent/projects/{project_id}/tasks/{task_id}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_task_returns_status_and_fields(client: AsyncClient, db_session):
    """Basic happy path: status, task_type, title, and message returned."""
    project, calling_task = await _make_project_and_task(db_session)
    spawned = await _make_strategic_task(
        db_session,
        project,
        status=TaskStatus.IN_PROGRESS,
        task_type="research",
        title="My Research",
        latest_status_message="Running step 2",
    )

    token = create_agent_token(str(calling_task.id), "agent-vm-tasks")
    res = await client.get(
        f"/api/agent/projects/{project.id}/tasks/{spawned.id}",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "success"
    data = body["data"]
    assert data["id"] == str(spawned.id)
    assert data["status"] == "IN_PROGRESS"
    assert data["task_type"] == "research"
    assert data["title"] == "My Research"
    assert data["latest_status_message"] == "Running step 2"
    assert "created_at" in data
    assert "updated_at" in data


@pytest.mark.asyncio
async def test_get_task_completed_includes_pr_url(client: AsyncClient, db_session):
    """Completed task response includes pr_url from PULL_REQUEST artifact."""
    project, calling_task = await _make_project_and_task(db_session, name="PR Test")
    spawned = await _make_strategic_task(
        db_session, project, status=TaskStatus.COMPLETED, task_type="fix"
    )
    await _add_artifact(
        db_session,
        spawned,
        artifact_type=ArtifactType.PULL_REQUEST,
        pr_url="https://github.com/org/repo/pull/42",
    )

    token = create_agent_token(str(calling_task.id), "agent-vm-tasks")
    res = await client.get(
        f"/api/agent/projects/{project.id}/tasks/{spawned.id}",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    data = res.json()["data"]
    assert data["status"] == "COMPLETED"
    assert data["pr_url"] == "https://github.com/org/repo/pull/42"
    assert "completed_at" in data


@pytest.mark.asyncio
async def test_get_task_failed_includes_failure_reason_from_error_log(
    client: AsyncClient, db_session
):
    """Failed task with ERROR_LOG artifact: failure_reason is the log excerpt."""
    project, calling_task = await _make_project_and_task(db_session, name="Fail Test")
    spawned = await _make_strategic_task(
        db_session,
        project,
        status=TaskStatus.FAILED,
        latest_status_message="Agent terminated unexpectedly",
    )
    await _add_artifact(
        db_session,
        spawned,
        artifact_type=ArtifactType.ERROR_LOG,
        logs="Traceback: SyntaxError on line 42",
    )

    token = create_agent_token(str(calling_task.id), "agent-vm-tasks")
    res = await client.get(
        f"/api/agent/projects/{project.id}/tasks/{spawned.id}",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    data = res.json()["data"]
    assert data["status"] == "FAILED"
    assert "Traceback" in data["failure_reason"]


@pytest.mark.asyncio
async def test_get_task_failed_falls_back_to_status_message_when_no_error_log(
    client: AsyncClient, db_session
):
    """Failed task without ERROR_LOG: failure_reason falls back to latest_status_message."""
    project, calling_task = await _make_project_and_task(db_session, name="FallbackFail")
    spawned = await _make_strategic_task(
        db_session,
        project,
        status=TaskStatus.FAILED,
        latest_status_message="Build failed: test suite errors",
    )

    token = create_agent_token(str(calling_task.id), "agent-vm-tasks")
    res = await client.get(
        f"/api/agent/projects/{project.id}/tasks/{spawned.id}",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    data = res.json()["data"]
    assert data["failure_reason"] == "Build failed: test suite errors"


@pytest.mark.asyncio
async def test_get_task_blocked_includes_blocked_reason(client: AsyncClient, db_session):
    """BLOCKED task response includes blocked_reason and blocker_kind."""
    project, calling_task = await _make_project_and_task(db_session, name="Blocked Test")
    spawned = await _make_strategic_task(
        db_session,
        project,
        status=TaskStatus.BLOCKED,
        blocked_reason="Waiting on sibling PR to merge",
        blocker_kind="pending_merge",
    )

    token = create_agent_token(str(calling_task.id), "agent-vm-tasks")
    res = await client.get(
        f"/api/agent/projects/{project.id}/tasks/{spawned.id}",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    data = res.json()["data"]
    assert data["status"] == "BLOCKED"
    assert data["blocked_reason"] == "Waiting on sibling PR to merge"
    assert data["blocker_kind"] == "pending_merge"


@pytest.mark.asyncio
async def test_get_task_parked_includes_park_reason(client: AsyncClient, db_session):
    """PARKED task response includes park_reason."""
    project, calling_task = await _make_project_and_task(db_session, name="Parked Test")
    spawned = await _make_strategic_task(
        db_session,
        project,
        status=TaskStatus.PARKED,
        park_reason="Waiting for external dependency",
    )

    token = create_agent_token(str(calling_task.id), "agent-vm-tasks")
    res = await client.get(
        f"/api/agent/projects/{project.id}/tasks/{spawned.id}",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    data = res.json()["data"]
    assert data["status"] == "PARKED"
    assert data["park_reason"] == "Waiting for external dependency"


@pytest.mark.asyncio
async def test_get_task_404_for_non_strategic_agent_task(client: AsyncClient, db_session):
    """Task created by USER is not visible through this endpoint."""
    project, calling_task = await _make_project_and_task(db_session, name="Auth Test")
    user_task = Task(
        project_id=project.id,
        payloads=[{"type": "fix"}],
        created_by="USER",
    )
    db_session.add(user_task)
    await db_session.commit()
    await db_session.refresh(user_task)

    token = create_agent_token(str(calling_task.id), "agent-vm-tasks")
    res = await client.get(
        f"/api/agent/projects/{project.id}/tasks/{user_task.id}",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_get_task_404_for_cross_project_task(client: AsyncClient, db_session):
    """Task in another project returns 404 (not 403) to prevent enumeration."""
    project1, calling_task = await _make_project_and_task(db_session, name="Project1")
    project2 = Project(name="Project2", user_id="other-user")
    db_session.add(project2)
    await db_session.commit()
    await db_session.refresh(project2)

    other_task = await _make_strategic_task(db_session, project2)

    token = create_agent_token(str(calling_task.id), "agent-vm-tasks")
    # Request task from project2 but with project1 in path
    res = await client.get(
        f"/api/agent/projects/{project1.id}/tasks/{other_task.id}",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_get_task_404_for_unknown_task_id(client: AsyncClient, db_session):
    """Non-existent task ID returns 404."""
    project, calling_task = await _make_project_and_task(db_session, name="Missing Test")

    token = create_agent_token(str(calling_task.id), "agent-vm-tasks")
    res = await client.get(
        f"/api/agent/projects/{project.id}/tasks/{uuid.uuid4()}",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_get_task_400_for_invalid_uuid(client: AsyncClient, db_session):
    """Invalid UUID in task_id path returns 404."""
    project, calling_task = await _make_project_and_task(db_session, name="Invalid UUID")

    token = create_agent_token(str(calling_task.id), "agent-vm-tasks")
    res = await client.get(
        f"/api/agent/projects/{project.id}/tasks/not-a-uuid",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_get_task_rejects_invalid_token(client: AsyncClient, db_session):
    """Invalid token returns 401."""
    project, calling_task = await _make_project_and_task(db_session, name="NoAuth")
    spawned = await _make_strategic_task(db_session, project)

    res = await client.get(
        f"/api/agent/projects/{project.id}/tasks/{spawned.id}",
        headers={"x-agent-token": "not-a-valid-jwt"},
    )
    assert res.status_code == 401


# ---------------------------------------------------------------------------
# GET /api/agent/projects/{project_id}/tasks  (list)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_tasks_returns_strategic_agent_tasks(client: AsyncClient, db_session):
    """List returns only STRATEGIC_AGENT tasks for the project."""
    project, calling_task = await _make_project_and_task(db_session, name="List Test")

    t1 = await _make_strategic_task(db_session, project, title="Task 1", task_type="research")
    t2 = await _make_strategic_task(db_session, project, title="Task 2", task_type="plan")
    # Non-strategic task — should not appear
    user_task = Task(project_id=project.id, payloads=[{"type": "fix"}], created_by="USER")
    db_session.add(user_task)
    await db_session.commit()

    token = create_agent_token(str(calling_task.id), "agent-vm-tasks")
    res = await client.get(
        f"/api/agent/projects/{project.id}/tasks",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "success"
    ids = {item["id"] for item in body["data"]}
    assert str(t1.id) in ids
    assert str(t2.id) in ids
    assert str(user_task.id) not in ids
    assert body["count"] == 2


@pytest.mark.asyncio
async def test_list_tasks_ordered_most_recent_first(client: AsyncClient, db_session):
    """List is ordered by created_at descending."""
    project, calling_task = await _make_project_and_task(db_session, name="Order Test")

    # Create two tasks; t2 will have a later timestamp
    t1 = await _make_strategic_task(db_session, project, title="Older")
    t2 = await _make_strategic_task(db_session, project, title="Newer")

    token = create_agent_token(str(calling_task.id), "agent-vm-tasks")
    res = await client.get(
        f"/api/agent/projects/{project.id}/tasks",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    items = res.json()["data"]
    ids = [item["id"] for item in items]
    # Newer task (t2) should appear before older task (t1)
    assert ids.index(str(t2.id)) < ids.index(str(t1.id))


@pytest.mark.asyncio
async def test_list_tasks_limit_parameter(client: AsyncClient, db_session):
    """Limit query parameter caps the result count."""
    project, calling_task = await _make_project_and_task(db_session, name="Limit Test")

    for i in range(5):
        await _make_strategic_task(db_session, project, title=f"Task {i}")

    token = create_agent_token(str(calling_task.id), "agent-vm-tasks")
    res = await client.get(
        f"/api/agent/projects/{project.id}/tasks?limit=3",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["count"] == 3
    assert len(body["data"]) == 3


@pytest.mark.asyncio
async def test_list_tasks_includes_pr_url_for_completed(client: AsyncClient, db_session):
    """Completed tasks in the list response include pr_url from artifact."""
    project, calling_task = await _make_project_and_task(db_session, name="ListPR Test")
    done = await _make_strategic_task(
        db_session, project, status=TaskStatus.COMPLETED, task_type="fix"
    )
    await _add_artifact(
        db_session,
        done,
        artifact_type=ArtifactType.PULL_REQUEST,
        pr_url="https://github.com/org/repo/pull/99",
    )
    in_progress = await _make_strategic_task(
        db_session, project, status=TaskStatus.IN_PROGRESS, task_type="research"
    )

    token = create_agent_token(str(calling_task.id), "agent-vm-tasks")
    res = await client.get(
        f"/api/agent/projects/{project.id}/tasks",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    items = res.json()["data"]
    done_item = next(i for i in items if i["id"] == str(done.id))
    ip_item = next(i for i in items if i["id"] == str(in_progress.id))
    assert done_item["pr_url"] == "https://github.com/org/repo/pull/99"
    assert "pr_url" not in ip_item


@pytest.mark.asyncio
async def test_list_tasks_empty_for_project_with_no_strategic_tasks(
    client: AsyncClient, db_session
):
    """Project with no STRATEGIC_AGENT tasks returns empty list."""
    project, calling_task = await _make_project_and_task(db_session, name="Empty List")
    # Only the calling task exists (no strategic tasks)

    token = create_agent_token(str(calling_task.id), "agent-vm-tasks")
    res = await client.get(
        f"/api/agent/projects/{project.id}/tasks",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["data"] == []
    assert body["count"] == 0


@pytest.mark.asyncio
async def test_list_tasks_cross_project_isolation(client: AsyncClient, db_session):
    """Tasks from another project are not visible in this project's list."""
    project1, calling_task = await _make_project_and_task(db_session, name="Proj1 Isolation")
    project2 = Project(name="Proj2 Isolation", user_id="other-user-iso")
    db_session.add(project2)
    await db_session.commit()
    await db_session.refresh(project2)

    # Add a strategic task to project2 only
    await _make_strategic_task(db_session, project2, title="Other project task")
    # Add a strategic task to project1
    mine = await _make_strategic_task(db_session, project1, title="My task")

    token = create_agent_token(str(calling_task.id), "agent-vm-tasks")
    res = await client.get(
        f"/api/agent/projects/{project1.id}/tasks",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    ids = {item["id"] for item in res.json()["data"]}
    assert str(mine.id) in ids
    # The calling_task is not STRATEGIC_AGENT so won't appear, and proj2 task is isolated
    for item_id in ids:
        # None of the IDs should belong to project2
        task = await db_session.get(Task, uuid.UUID(item_id))
        assert task is not None
        assert task.project_id == project1.id


# ---------------------------------------------------------------------------
# Parent_task_id lineage visibility — GET single task
# ---------------------------------------------------------------------------


async def _make_child_task(
    db_session,
    project,
    parent_task: Task,
    *,
    created_by: str = "ORCHESTRATOR",
    title: str = "Child task",
    status: TaskStatus = TaskStatus.IN_PROGRESS,
) -> Task:
    """Create a task whose parent_task_id points to parent_task."""
    task = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        title=title,
        status=status,
        created_by=created_by,
        parent_task_id=parent_task.id,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    return task


@pytest.mark.asyncio
async def test_get_task_visible_for_direct_child_of_strategic_task(
    client: AsyncClient, db_session
):
    """Direct child of a STRATEGIC_AGENT task (created_by=ORCHESTRATOR) is visible."""
    project, calling_task = await _make_project_and_task(db_session, name="Child Visibility")
    strategic = await _make_strategic_task(db_session, project, task_type="plan")
    child = await _make_child_task(db_session, project, strategic, title="Impl task")

    token = create_agent_token(str(calling_task.id), "agent-vm-tasks")
    res = await client.get(
        f"/api/agent/projects/{project.id}/tasks/{child.id}",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    data = res.json()["data"]
    assert data["id"] == str(child.id)
    assert data["title"] == "Impl task"


@pytest.mark.asyncio
async def test_get_task_visible_for_grandchild_of_strategic_task(
    client: AsyncClient, db_session
):
    """Grandchild (strategist -> plan -> impl) is visible via multi-level ancestor walk."""
    project, calling_task = await _make_project_and_task(db_session, name="Grandchild Vis")
    strategic = await _make_strategic_task(db_session, project, task_type="plan")
    plan_child = await _make_child_task(
        db_session, project, strategic, created_by="ORCHESTRATOR", title="Plan child"
    )
    impl_grandchild = await _make_child_task(
        db_session, project, plan_child, created_by="ORCHESTRATOR", title="Impl grandchild"
    )

    token = create_agent_token(str(calling_task.id), "agent-vm-tasks")
    res = await client.get(
        f"/api/agent/projects/{project.id}/tasks/{impl_grandchild.id}",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    assert res.json()["data"]["id"] == str(impl_grandchild.id)


@pytest.mark.asyncio
async def test_get_task_404_for_unrelated_non_strategic_task(
    client: AsyncClient, db_session
):
    """Task with no parent_task_id and created_by=ORCHESTRATOR stays invisible (404)."""
    project, calling_task = await _make_project_and_task(db_session, name="Unrelated 404")
    orphan = Task(
        project_id=project.id,
        payloads=[{"type": "fix"}],
        title="Orphan task",
        created_by="ORCHESTRATOR",
    )
    db_session.add(orphan)
    await db_session.commit()
    await db_session.refresh(orphan)

    token = create_agent_token(str(calling_task.id), "agent-vm-tasks")
    res = await client.get(
        f"/api/agent/projects/{project.id}/tasks/{orphan.id}",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_get_task_404_for_descendant_of_cross_project_strategic_task(
    client: AsyncClient, db_session
):
    """Child of a strategist task in another project is not visible from this project."""
    project1, calling_task = await _make_project_and_task(db_session, name="XP Desc P1")
    project2 = Project(name="XP Desc P2", user_id="other-user-xp")
    db_session.add(project2)
    await db_session.commit()
    await db_session.refresh(project2)

    strategic2 = await _make_strategic_task(db_session, project2, task_type="plan")
    # Create a child that incorrectly claims to live in project1 but whose parent is in project2
    # In practice, parent must be in same project — this tests the project_id guard during walk.
    child_p2 = await _make_child_task(
        db_session, project2, strategic2, title="Child in project2"
    )

    token = create_agent_token(str(calling_task.id), "agent-vm-tasks")
    # Request child_p2 with project1 in path — must 404 (project_id mismatch).
    res = await client.get(
        f"/api/agent/projects/{project1.id}/tasks/{child_p2.id}",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 404


# ---------------------------------------------------------------------------
# Parent_task_id lineage visibility — list endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_tasks_includes_transitive_descendants(
    client: AsyncClient, db_session
):
    """List returns the strategist task AND its direct child and grandchild."""
    project, calling_task = await _make_project_and_task(db_session, name="List Descendants")
    strategic = await _make_strategic_task(db_session, project, title="Root strategic")
    child = await _make_child_task(db_session, project, strategic, title="Child task")
    grandchild = await _make_child_task(db_session, project, child, title="Grandchild task")
    # Unrelated non-strategic task — must not appear
    orphan = Task(
        project_id=project.id,
        payloads=[{"type": "fix"}],
        title="Orphan",
        created_by="ORCHESTRATOR",
    )
    db_session.add(orphan)
    await db_session.commit()

    token = create_agent_token(str(calling_task.id), "agent-vm-tasks")
    res = await client.get(
        f"/api/agent/projects/{project.id}/tasks",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    ids = {item["id"] for item in res.json()["data"]}
    assert str(strategic.id) in ids
    assert str(child.id) in ids
    assert str(grandchild.id) in ids
    assert str(orphan.id) not in ids


@pytest.mark.asyncio
async def test_list_tasks_descendants_do_not_leak_cross_project(
    client: AsyncClient, db_session
):
    """Descendants of a strategist task in another project are absent from this project's list."""
    project1, calling_task = await _make_project_and_task(db_session, name="XP List P1")
    project2 = Project(name="XP List P2", user_id="other-user-xplist")
    db_session.add(project2)
    await db_session.commit()
    await db_session.refresh(project2)

    strategic2 = await _make_strategic_task(db_session, project2, title="Other project root")
    child2 = await _make_child_task(db_session, project2, strategic2, title="Other project child")
    own_strategic = await _make_strategic_task(db_session, project1, title="My root")

    token = create_agent_token(str(calling_task.id), "agent-vm-tasks")
    res = await client.get(
        f"/api/agent/projects/{project1.id}/tasks",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    ids = {item["id"] for item in res.json()["data"]}
    assert str(own_strategic.id) in ids
    assert str(strategic2.id) not in ids
    assert str(child2.id) not in ids
