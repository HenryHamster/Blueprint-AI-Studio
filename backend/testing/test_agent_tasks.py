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
from test_agent_task_contract import assert_task_list_response_contract


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
    """List returns only STRATEGIC_AGENT tasks for the project, with flat+children contract."""
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

    # Validate against the shared flat+children contract
    assert_task_list_response_contract(body)

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
async def test_get_task_includes_parent_task_id(client: AsyncClient, db_session):
    """Single-task GET returns parent_task_id: populated for a child, null for root."""
    project, calling_task = await _make_project_and_task(db_session, name="ParentId GET")
    root = await _make_strategic_task(db_session, project, title="Root strategic")
    child = await _make_child_task(db_session, project, root, title="Child task")

    token = create_agent_token(str(calling_task.id), "agent-vm-tasks")

    # Root strategic task: parent_task_id must be null
    res = await client.get(
        f"/api/agent/projects/{project.id}/tasks/{root.id}",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    assert res.json()["data"]["parent_task_id"] is None

    # Child task: parent_task_id must equal the root's id
    res = await client.get(
        f"/api/agent/projects/{project.id}/tasks/{child.id}",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    assert res.json()["data"]["parent_task_id"] == str(root.id)


@pytest.mark.asyncio
async def test_list_tasks_descendants_carry_parent_task_id(
    client: AsyncClient, db_session
):
    """List items carry parent_task_id: null for strategist roots, populated for descendants."""
    project, calling_task = await _make_project_and_task(db_session, name="List ParentId")
    root = await _make_strategic_task(db_session, project, title="Root strategic")
    child = await _make_child_task(db_session, project, root, title="Child task")
    grandchild = await _make_child_task(db_session, project, child, title="Grandchild task")

    token = create_agent_token(str(calling_task.id), "agent-vm-tasks")
    res = await client.get(
        f"/api/agent/projects/{project.id}/tasks",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    items = {item["id"]: item for item in res.json()["data"]}

    # Root has no parent
    assert items[str(root.id)]["parent_task_id"] is None
    # Child points to root
    assert items[str(child.id)]["parent_task_id"] == str(root.id)
    # Grandchild points to child
    assert items[str(grandchild.id)]["parent_task_id"] == str(child.id)


@pytest.mark.asyncio
async def test_list_tasks_depth_bound_does_not_recurse_past_max(
    client: AsyncClient, db_session
):
    """A chain deeper than _ANCESTOR_WALK_MAX_DEPTH (20) is safely truncated."""
    from app.routers.agent_tasks import _ANCESTOR_WALK_MAX_DEPTH

    project, calling_task = await _make_project_and_task(db_session, name="Depth Bound")
    root = await _make_strategic_task(db_session, project, title="Root")

    # Build a chain of 25 tasks (exceeds _ANCESTOR_WALK_MAX_DEPTH=20)
    current = root
    for i in range(25):
        current = await _make_child_task(
            db_session, project, current, title=f"Depth {i + 1}"
        )

    token = create_agent_token(str(calling_task.id), "agent-vm-tasks")
    res = await client.get(
        f"/api/agent/projects/{project.id}/tasks?limit=100",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    # At most max_depth + 1 generations (root + max_depth children)
    assert res.json()["count"] <= _ANCESTOR_WALK_MAX_DEPTH + 1


@pytest.mark.asyncio
async def test_list_tasks_limit_truncates_mixed_tree(client: AsyncClient, db_session):
    """limit=4 returns exactly 4 items from two strategist roots each with 3 children."""
    project, calling_task = await _make_project_and_task(db_session, name="Limit Mixed")
    root1 = await _make_strategic_task(db_session, project, title="Root1")
    root2 = await _make_strategic_task(db_session, project, title="Root2")
    for i in range(3):
        await _make_child_task(db_session, project, root1, title=f"R1-child-{i}")
        await _make_child_task(db_session, project, root2, title=f"R2-child-{i}")

    token = create_agent_token(str(calling_task.id), "agent-vm-tasks")
    res = await client.get(
        f"/api/agent/projects/{project.id}/tasks?limit=4",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["count"] == 4
    assert len(body["data"]) == 4


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


# ---------------------------------------------------------------------------
# GET /api/agent/projects/{project_id}/task-census
#
# The lineage list above seeds on created_by="STRATEGIC_AGENT". Nearly all
# pipeline work is authored as "ORCHESTRATOR", so the census exists to show the
# agent what the lineage view structurally cannot.
# ---------------------------------------------------------------------------


async def _make_census_task(
    db_session,
    project: Project,
    *,
    created_by: str,
    status: TaskStatus = TaskStatus.QUEUED,
    task_type: str = "research",
    title: str = "census task",
    created_at=None,
    latest_status_message: str | None = None,
    blocked_reason: str | None = None,
    blocker_kind: str | None = None,
    park_reason: str | None = None,
) -> Task:
    kwargs = {
        "project_id": project.id,
        "payloads": [{"type": task_type}],
        "title": title,
        "status": status,
        "created_by": created_by,
        "latest_status_message": latest_status_message,
        "blocked_reason": blocked_reason,
        "blocker_kind": blocker_kind,
        "park_reason": park_reason,
    }
    if created_at is not None:
        kwargs["created_at"] = created_at
    task = Task(**kwargs)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    return task


@pytest.mark.asyncio
async def test_task_census_sees_orchestrator_work_the_lineage_list_cannot(
    client: AsyncClient, db_session
):
    """The census counts every author; the lineage list still counts only its own.

    Both endpoints are asserted in one test on purpose: the census is only
    valuable because the lineage list omits exactly these rows, and pinning the
    omission here means a future widening of the lineage filter cannot make this
    test pass for the wrong reason.
    """
    project, calling_task = await _make_project_and_task(db_session, name="Census Authors")
    orchestrated = await _make_census_task(
        db_session,
        project,
        created_by="ORCHESTRATOR",
        status=TaskStatus.IN_PROGRESS,
        task_type="direction_lifecycle",
        title="orchestrator-authored work",
    )
    strategist = await _make_strategic_task(
        db_session, project, status=TaskStatus.QUEUED, task_type="research"
    )

    token = create_agent_token(str(calling_task.id), "agent-vm-tasks")
    census = await client.get(
        f"/api/agent/projects/{project.id}/task-census",
        headers={"x-agent-token": token},
    )
    assert census.status_code == 200, census.text
    data = census.json()["data"]

    by_author = data["non_terminal_by_created_by"]
    # The calling task itself is a real queued row authored "USER"; it is
    # counted like any other, which is the point — no author is filtered out.
    assert by_author.get("ORCHESTRATOR") == 1
    assert by_author.get("STRATEGIC_AGENT") == 1
    assert by_author.get("USER") == 1
    assert data["non_terminal_by_status"]["IN_PROGRESS"] == 1
    assert data["non_terminal_by_type"]["direction_lifecycle"] == {"IN_PROGRESS": 1}
    census_ids = {row["id"] for row in data["oldest_non_terminal"]}
    assert str(orchestrated.id) in census_ids
    assert str(strategist.id) in census_ids

    # The lineage list is unchanged and still cannot see the orchestrator row.
    lineage = await client.get(
        f"/api/agent/projects/{project.id}/tasks",
        headers={"x-agent-token": token},
    )
    assert lineage.status_code == 200, lineage.text
    lineage_ids = {item["id"] for item in lineage.json()["data"]}
    assert str(strategist.id) in lineage_ids
    assert str(orchestrated.id) not in lineage_ids


@pytest.mark.asyncio
async def test_task_census_orders_oldest_first_and_carries_block_reasons(
    client: AsyncClient, db_session
):
    """Oldest non-terminal first, with age and the blocked/parked reason."""
    from datetime import datetime, timedelta, timezone

    project, calling_task = await _make_project_and_task(db_session, name="Census Ordering")
    now = datetime.now(timezone.utc)
    stranded = await _make_census_task(
        db_session,
        project,
        created_by="ORCHESTRATOR",
        status=TaskStatus.BLOCKED,
        title="stranded for days",
        created_at=now - timedelta(days=4),
        blocked_reason="waiting on an unresolved upstream prerequisite",
        blocker_kind="dependency",
    )
    parked = await _make_census_task(
        db_session,
        project,
        created_by="ORCHESTRATOR",
        status=TaskStatus.PARKED,
        title="parked yesterday",
        created_at=now - timedelta(days=1),
        park_reason="review loop parked",
    )
    fresh = await _make_census_task(
        db_session,
        project,
        created_by="ORCHESTRATOR",
        status=TaskStatus.QUEUED,
        title="queued just now",
        created_at=now,
    )

    token = create_agent_token(str(calling_task.id), "agent-vm-tasks")
    res = await client.get(
        f"/api/agent/projects/{project.id}/task-census",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    rows = res.json()["data"]["oldest_non_terminal"]
    by_id = {row["id"]: row for row in rows}
    mine = [row["id"] for row in rows if row["id"] in {
        str(stranded.id), str(parked.id), str(fresh.id)
    }]
    assert mine == [str(stranded.id), str(parked.id), str(fresh.id)]

    assert by_id[str(stranded.id)]["blocked_reason"] == (
        "waiting on an unresolved upstream prerequisite"
    )
    assert by_id[str(stranded.id)]["blocker_kind"] == "dependency"
    # ~4 days old; the point is that it reads as stranded, not as fresh.
    assert by_id[str(stranded.id)]["age_hours"] >= 90
    assert by_id[str(parked.id)]["park_reason"] == "review loop parked"
    # Reason fields are attached per status, not blanket-emitted.
    assert "blocked_reason" not in by_id[str(fresh.id)]
    assert "park_reason" not in by_id[str(stranded.id)]


@pytest.mark.asyncio
async def test_task_census_counts_recent_terminal_work(client: AsyncClient, db_session):
    """A 24h terminal breakdown separates a standing queue from a stalled one."""
    project, calling_task = await _make_project_and_task(db_session, name="Census Terminal")
    done_ids = set()
    for status in (TaskStatus.COMPLETED, TaskStatus.COMPLETED, TaskStatus.FAILED):
        task = await _make_census_task(
            db_session, project, created_by="ORCHESTRATOR", status=status
        )
        done_ids.add(str(task.id))

    token = create_agent_token(str(calling_task.id), "agent-vm-tasks")
    res = await client.get(
        f"/api/agent/projects/{project.id}/task-census",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    data = res.json()["data"]
    assert data["recent_terminal"]["window_hours"] == 24
    assert data["recent_terminal"]["by_status"] == {"COMPLETED": 2, "FAILED": 1}
    # Terminal rows are counted, never listed: only the queued calling task is.
    assert data["non_terminal_total"] == 1
    assert data["non_terminal_by_status"] == {"QUEUED": 1}
    listed = {row["id"] for row in data["oldest_non_terminal"]}
    assert listed == {str(calling_task.id)}
    assert not (listed & done_ids)


@pytest.mark.asyncio
async def test_task_census_is_hard_capped_and_sheds_rows_not_counts(
    client: AsyncClient, db_session
):
    """The byte ceiling is load-bearing, so it is pinned twice over.

    A response larger than a quarter of MAX_CANONICAL_ENVELOPE_BYTES can push the
    strategic envelope past the coreless-stub threshold and livelock the reclaim
    loop (PR #17424). This asserts the literal ceiling, asserts the transmitted
    body actually fits under it with worst-case free text, and asserts that what
    got shed was detail rows — never the aggregate counts.
    """
    import json as _json

    from app.agent_read_budget import AGENT_READ_MAX_RESPONSE_BYTES

    # Literal pin. If MAX_CANONICAL_ENVELOPE_BYTES moves, this must be re-derived
    # deliberately rather than tracking it silently.
    assert AGENT_READ_MAX_RESPONSE_BYTES == 16384

    project, calling_task = await _make_project_and_task(db_session, name="Census Cap")
    long_title = "T" * 4000
    long_message = "M" * 4000
    for index in range(40):
        await _make_census_task(
            db_session,
            project,
            created_by="ORCHESTRATOR",
            status=TaskStatus.BLOCKED,
            task_type=f"type_{index % 3}",
            title=long_title,
            latest_status_message=long_message,
            blocked_reason=long_message,
        )

    token = create_agent_token(str(calling_task.id), "agent-vm-tasks")
    res = await client.get(
        f"/api/agent/projects/{project.id}/task-census?oldest=100",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    data = res.json()["data"]

    body_bytes = len(
        _json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    )
    assert body_bytes <= AGENT_READ_MAX_RESPONSE_BYTES, body_bytes
    assert data["bounds"]["max_response_bytes"] == AGENT_READ_MAX_RESPONSE_BYTES
    assert data["bounds"]["truncated"] is True
    assert data["bounds"]["truncated_sections"] == ["oldest_non_terminal"]

    # Counts describe every row (40 blocked + the queued calling task) even
    # though far fewer rows were returned.
    assert data["non_terminal_total"] == 41
    assert data["non_terminal_by_status"]["BLOCKED"] == 40
    assert sum(data["non_terminal_by_status"].values()) == 41
    assert data["bounds"]["oldest_returned"] == len(data["oldest_non_terminal"])
    assert data["bounds"]["oldest_returned"] < 40

    # Per-field clipping happened before the whole-response clamp.
    clipped = 0
    for row in data["oldest_non_terminal"]:
        for field in ("title", "latest_status_message", "blocked_reason"):
            value = row.get(field)
            if value is None:
                continue
            assert len(value.encode("utf-8")) <= 244, (field, len(value))
            clipped += 1
    assert clipped, "expected at least one clipped free-text field"


@pytest.mark.asyncio
async def test_task_census_survives_unbounded_task_type_cardinality(
    client: AsyncClient, db_session
):
    """Regression: the aggregate maps alone used to blow through the ceiling.

    ``clamp_read_response`` can only shed rows from the keys named in its trim
    order, and the census names exactly one: ``oldest_non_terminal``. The three
    aggregate maps are untrimmable by design — a census that has lost its
    counts is worthless. That was fine for ``non_terminal_by_status`` (a
    TaskStatus enum) and for ``non_terminal_by_created_by`` (``chk_task_created_by``,
    alembic 0067, bounds it to 11 values), but NOT for ``non_terminal_by_type``:
    the type is read straight out of payload JSONB, where the only constraint
    is ``chk_task_payloads_size`` on total bytes. Nothing bounds the key
    vocabulary, so a project with a few hundred distinct in-flight types
    returned multiples of the cap with ``oldest_non_terminal`` already emptied
    and ``truncated: True`` as the only signal — a bound that reported success
    while missing by 2x.

    The construction is asserted to genuinely breach before the response is
    checked, so removing ``bound_counts`` from the endpoint fails this test
    rather than merely making it less meaningful.
    """
    import json as _json

    from app.agent_read_budget import (
        AGENT_READ_AGGREGATE_TOP_N,
        AGENT_READ_MAX_KEY_BYTES,
        AGENT_READ_MAX_RESPONSE_BYTES,
        serialized_bytes,
    )

    project, calling_task = await _make_project_and_task(
        db_session, name="Census Cardinality"
    )

    distinct = 400
    type_names = [f"pipeline_maintenance_followup_stage_{i:03d}" for i in range(distinct)]
    # The unfolded map, with ZERO detail rows, does not fit. This is the defect.
    assert (
        serialized_bytes({name: {"QUEUED": 1} for name in type_names})
        > AGENT_READ_MAX_RESPONSE_BYTES
    )

    # A single free-string key is also unbounded in length, so one type is long
    # enough to matter on its own. Three tasks so it ranks into the kept set and
    # the emitted key can be inspected rather than folded away.
    long_type = "L" * 4000
    for name in type_names + [long_type] * 3:
        db_session.add(
            Task(
                project_id=project.id,
                payloads=[{"type": name}],
                title="census cardinality",
                status=TaskStatus.QUEUED,
                created_by="ORCHESTRATOR",
            )
        )
    await db_session.commit()

    token = create_agent_token(str(calling_task.id), "agent-vm-tasks")
    res = await client.get(
        f"/api/agent/projects/{project.id}/task-census?oldest=100",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    data = res.json()["data"]

    body_bytes = len(
        _json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    )
    assert body_bytes <= AGENT_READ_MAX_RESPONSE_BYTES, body_bytes
    # The positive statement: the ceiling was reached, not merely aimed at.
    assert data["bounds"]["over_budget"] is False

    by_type = data["non_terminal_by_type"]
    assert len(by_type) <= AGENT_READ_AGGREGATE_TOP_N + 1
    assert "other" in by_type
    # Every key is bounded in length as well as in number.
    assert max(len(key.encode("utf-8")) for key in by_type) <= (
        AGENT_READ_MAX_KEY_BYTES + 3
    )

    # 400 named types + the long one + the calling task's typeless payload.
    assert data["bounds"]["distinct_types"] == distinct + 2
    assert data["bounds"]["aggregate_top_n"] == AGENT_READ_AGGREGATE_TOP_N

    # The fold is arithmetically exact: hiding the vocabulary must not lose a
    # single task, or the census stops being a census.
    assert data["non_terminal_total"] == distinct + 4
    folded_total = sum(
        sum(bucket.values()) if isinstance(bucket, dict) else bucket
        for bucket in by_type.values()
    )
    assert folded_total == data["non_terminal_total"]
    assert sum(data["non_terminal_by_created_by"].values()) == data["non_terminal_total"]
    assert sum(data["non_terminal_by_status"].values()) == data["non_terminal_total"]


def test_task_census_route_is_read_only():
    """Asserted on the route, not the router — the router-wide sweep is vacuous here.

    ``test_agent_pipeline_visibility.test_every_visibility_route_is_read_only``
    sweeps ``visibility_router``, which is read-only end to end. The census is
    registered on the ``agent_tasks`` router instead, which already carries
    POST/PATCH task verbs, so the same sweep applied here would pass on a
    mutating census. The route is located by path and its existence asserted,
    so a rename cannot make this pass by absence.
    """
    from app.routers.agent_tasks import router as agent_tasks_router

    census_path = "/api/agent/projects/{project_id}/task-census"
    matches = [
        route
        for route in agent_tasks_router.routes
        if getattr(route, "path", None) == census_path
    ]
    assert len(matches) == 1, sorted(
        getattr(route, "path", "") for route in agent_tasks_router.routes
    )
    methods = set(getattr(matches[0], "methods", set()) or set())
    assert methods <= {"GET", "HEAD"}, methods


@pytest.mark.asyncio
async def test_task_census_cross_project_is_404(client: AsyncClient, db_session):
    """Cross-project probing is hidden the same way the other agent reads hide it."""
    _project1, calling_task = await _make_project_and_task(db_session, name="Census P1")
    project2 = Project(name="Census P2", user_id="other-user-census")
    db_session.add(project2)
    await db_session.commit()
    await db_session.refresh(project2)

    token = create_agent_token(str(calling_task.id), "agent-vm-tasks")
    res = await client.get(
        f"/api/agent/projects/{project2.id}/task-census",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 404, res.text


@pytest.mark.asyncio
async def test_task_census_requires_agent_token(client: AsyncClient, db_session):
    project, _ = await _make_project_and_task(db_session, name="Census Auth")

    # A forged token is rejected outright.
    res = await client.get(
        f"/api/agent/projects/{project.id}/task-census",
        headers={"x-agent-token": "not-a-real-token"},
    )
    assert res.status_code == 401, res.text

    # No token at all never reaches the handler either.
    res = await client.get(f"/api/agent/projects/{project.id}/task-census")
    assert res.status_code in (401, 403, 422), res.text


# ---------------------------------------------------------------------------
# Terminal rows carry a REASON, not just a state
#
# The list surface used to return FAILED with nothing attached: no
# failure_reason, no class, no detail. A reader saw the state and had to
# re-fetch every row one at a time to learn anything about it. These pin the
# three fields and, just as importantly, the RESOLUTION ORDER between them --
# reading the sparse verdict cache instead of the canonical category is what
# made properly classified failures display as UNKNOWN.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_tasks_failed_item_carries_failure_reason_and_class(
    client: AsyncClient, db_session
):
    """A FAILED row in the LIST reports prose, class and detail -- not bare state."""
    project, calling_task = await _make_project_and_task(db_session, name="ListWhy")
    failed = await _make_strategic_task(
        db_session,
        project,
        status=TaskStatus.FAILED,
        title="Broken",
        latest_status_message="Agent terminated unexpectedly",
    )
    # EXTERNAL_API is deliberately NOT what classify_failure() would derive from
    # this status message, so this pins the PERSISTED canonical field rather
    # than accidentally agreeing with the read-time derivation.
    failed.context = {
        "failure_category": "EXTERNAL_API",
        "failure_detail": "evaluation_gate_failed",
    }
    db_session.add(failed)
    await db_session.commit()
    await _add_artifact(
        db_session,
        failed,
        artifact_type=ArtifactType.ERROR_LOG,
        logs="Traceback: SyntaxError on line 42",
    )

    token = create_agent_token(str(calling_task.id), "agent-vm-tasks")
    res = await client.get(
        f"/api/agent/projects/{project.id}/tasks",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert_task_list_response_contract(body)
    item = next(i for i in body["data"] if i["id"] == str(failed.id))

    assert "Traceback" in (item["failure_reason"] or "")
    assert item["failure_class"] == "EXTERNAL_API"
    assert item["failure_detail"] == "evaluation_gate_failed"


@pytest.mark.asyncio
async def test_list_tasks_failed_item_falls_back_to_status_message(
    client: AsyncClient, db_session
):
    """No ERROR_LOG artifact: the list still answers why, from the status message."""
    project, calling_task = await _make_project_and_task(db_session, name="ListWhyFB")
    failed = await _make_strategic_task(
        db_session,
        project,
        status=TaskStatus.FAILED,
        title="Broken no artifact",
        latest_status_message="Build failed: test suite errors",
    )

    token = create_agent_token(str(calling_task.id), "agent-vm-tasks")
    res = await client.get(
        f"/api/agent/projects/{project.id}/tasks",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    item = next(i for i in res.json()["data"] if i["id"] == str(failed.id))
    assert item["failure_reason"] == "Build failed: test suite errors"


@pytest.mark.asyncio
async def test_list_tasks_prefers_canonical_category_over_sparse_cache(
    client: AsyncClient, db_session
):
    """failure_category (canonical) WINS over failure_class (sparse reaper cache).

    Reading the sparse field first is the exact bug PR #17982 fixed in the
    operator CLI; pinning the order here is what stops the CLI and the agent
    read surface from disagreeing about the same task.
    """
    project, calling_task = await _make_project_and_task(db_session, name="ListPrec")
    failed = await _make_strategic_task(
        db_session,
        project,
        status=TaskStatus.FAILED,
        title="Two classes",
        latest_status_message="something went wrong",
    )
    failed.context = {
        "failure_category": "RATE_LIMIT",
        "failure_class": "CODE_ERROR",
    }
    db_session.add(failed)
    await db_session.commit()

    token = create_agent_token(str(calling_task.id), "agent-vm-tasks")
    res = await client.get(
        f"/api/agent/projects/{project.id}/tasks",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    item = next(i for i in res.json()["data"] if i["id"] == str(failed.id))
    assert item["failure_class"] == "RATE_LIMIT"


@pytest.mark.asyncio
async def test_list_tasks_completed_item_has_no_failure_class(
    client: AsyncClient, db_session
):
    """A COMPLETED row must never acquire a failure class from the classifier."""
    project, calling_task = await _make_project_and_task(db_session, name="ListDone")
    done = await _make_strategic_task(
        db_session,
        project,
        status=TaskStatus.COMPLETED,
        title="Fine",
        latest_status_message="All good",
    )

    token = create_agent_token(str(calling_task.id), "agent-vm-tasks")
    res = await client.get(
        f"/api/agent/projects/{project.id}/tasks",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    item = next(i for i in res.json()["data"] if i["id"] == str(done.id))
    assert "failure_class" not in item
    assert "failure_reason" not in item


@pytest.mark.asyncio
async def test_list_tasks_failed_descendant_carries_its_own_reason(
    client: AsyncClient, db_session
):
    """A failed CHILD in the inline children array reports why it failed too."""
    project, calling_task = await _make_project_and_task(db_session, name="ChildWhy")
    root = await _make_strategic_task(db_session, project, title="Root")
    child = Task(
        project_id=project.id,
        payloads=[{"type": "fix"}],
        title="Failed child",
        status=TaskStatus.FAILED,
        created_by="ORCHESTRATOR",
        parent_task_id=root.id,
        latest_status_message="child blew up",
        context={
            "failure_category": "BLOCKED_PRECONDITION",
            "failure_detail": "dependency_task_failed",
        },
    )
    db_session.add(child)
    await db_session.commit()

    token = create_agent_token(str(calling_task.id), "agent-vm-tasks")
    res = await client.get(
        f"/api/agent/projects/{project.id}/tasks",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    data = res.json()["data"]
    root_item = next(i for i in data if i["id"] == str(root.id))
    child_item = next(c for c in root_item["children"] if c["id"] == str(child.id))
    assert child_item["failure_reason"] == "child blew up"
    assert child_item["failure_class"] == "BLOCKED_PRECONDITION"
    assert child_item["failure_detail"] == "dependency_task_failed"


@pytest.mark.asyncio
async def test_get_task_failed_also_carries_class_and_detail(
    client: AsyncClient, db_session
):
    """The single-task view gains the same machine-readable pair."""
    project, calling_task = await _make_project_and_task(db_session, name="ShowWhy")
    failed = await _make_strategic_task(
        db_session,
        project,
        status=TaskStatus.FAILED,
        latest_status_message="died",
    )
    failed.context = {
        "failure_category": "PROVISION_FAILURE",
        "failure_detail": "pr_execution_fence_denied",
    }
    db_session.add(failed)
    await db_session.commit()

    token = create_agent_token(str(calling_task.id), "agent-vm-tasks")
    res = await client.get(
        f"/api/agent/projects/{project.id}/tasks/{failed.id}",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    data = res.json()["data"]
    assert data["failure_class"] == "PROVISION_FAILURE"
    assert data["failure_detail"] == "pr_execution_fence_denied"
