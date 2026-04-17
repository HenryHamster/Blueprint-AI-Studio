"""
Integration tests for the directions system.

Tests cover:
- CRUD: create, update (priority/status/text/paths), delete (cancels QUEUED tasks)
- Cooldown filter: directions within/past the cooldown window in _spawn_research_task
- last_researched_at update: after _handle_research_completion runs
- task-counts endpoint: counts research tasks per direction
- Status/priority validation: invalid values return 400
"""
import uuid
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.models import Alert, AlertSeverity, Project, Task, TaskStatus
from app.autonomous_research import _spawn_research_task
from app.research_completion import _handle_research_completion, _compute_momentum_factor, _compute_merge_rate, _calibrate_scope, predict_spawn_success
from app.max_plan_pool import _max_plan_pools, _account_by_id


@pytest.fixture(autouse=True)
def _ensure_test_account_pool():
    """Ensure _spawn_research_task finds an available account for the test user."""
    uid = "test-user-123"
    entry = {"id": "test-acct", "user_id": uid, "name": "test", "enabled": True, "active_tasks": 0,
             "oauth_token": "sk-ant-test", "total_tasks": 0, "token_created_at": None, "claude_config_json": None}
    _max_plan_pools.setdefault(uid, []).append(entry)
    _account_by_id["test-acct"] = entry
    yield
    _max_plan_pools.get(uid, [])[:] = [a for a in _max_plan_pools.get(uid, []) if a["id"] != "test-acct"]
    _account_by_id.pop("test-acct", None)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

async def _deploy_project(client: AsyncClient, mocker, name: str = "Test Project") -> str:
    """Create a project via the deploy API and return its project_id string."""
    mocker.patch("app.main.provision_agent_vm", return_value=None)
    resp = await client.post(
        "/api/deploy",
        json={"name": name, "payloads": [{"type": "smoketest"}], "storage_uris": []},
    )
    assert resp.status_code == 200
    return resp.json()["project_id"]


async def _add_direction(
    client: AsyncClient,
    project_id: str,
    *,
    text: str = "Improve test coverage",
    priority: str = "medium",
    paths: list | None = None,
) -> dict:
    """POST a direction to a project and return the created direction dict."""
    resp = await client.post(
        f"/api/admin/projects/{project_id}/directions",
        json={"text": text, "priority": priority, "paths": paths or []},
    )
    assert resp.status_code == 200
    return resp.json()["direction"]


# ─────────────────────────────────────────────────────────────────────────────
# 1. CRUD
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_directions_empty(client: AsyncClient, mocker):
    """GET on a fresh project returns an empty directions list."""
    project_id = await _deploy_project(client, mocker)
    resp = await client.get(f"/api/admin/projects/{project_id}/directions")
    assert resp.status_code == 200
    data = resp.json()
    assert data["directions"] == []
    assert "autonomous_enabled" in data


@pytest.mark.asyncio
async def test_create_direction(client: AsyncClient, mocker):
    """POST creates a direction with an auto-generated id and default active status."""
    project_id = await _deploy_project(client, mocker)
    resp = await client.post(
        f"/api/admin/projects/{project_id}/directions",
        json={"text": "Improve test coverage", "priority": "high", "paths": ["src/"]},
    )
    assert resp.status_code == 200
    d = resp.json()["direction"]
    assert d["text"] == "Improve test coverage"
    assert d["priority"] == "high"
    assert d["paths"] == ["src/"]
    assert d["status"] == "active"
    assert d["id"].startswith("d-")
    assert "added_at" in d


@pytest.mark.asyncio
async def test_get_directions_returns_created(client: AsyncClient, mocker):
    """GET returns directions that were previously POSTed."""
    project_id = await _deploy_project(client, mocker)
    direction = await _add_direction(client, project_id, text="Direction A", priority="low")

    resp = await client.get(f"/api/admin/projects/{project_id}/directions")
    assert resp.status_code == 200
    directions = resp.json()["directions"]
    assert len(directions) == 1
    assert directions[0]["id"] == direction["id"]


@pytest.mark.asyncio
async def test_unhold_direction_clears_hold(client: AsyncClient, mocker):
    """POST /unhold clears an existing direction hold and reports was_held=True."""
    import app.shared_state as _ss
    project_id = await _deploy_project(client, mocker)
    direction = await _add_direction(client, project_id)
    did = direction["id"]

    # Drive the direction into a held state
    _ss.direction_failure_streaks.clear()
    for _ in range(_ss.DIRECTION_HOLD_THRESHOLD):
        _ss.record_direction_result(did, success=False, failure_class="CODE_ERROR")
    assert _ss.is_direction_held(did)[0]

    try:
        resp = await client.post(
            f"/api/admin/projects/{project_id}/directions/{did}/unhold",
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "unheld"
        assert body["direction_id"] == did
        assert body["was_held"] is True
        assert not _ss.is_direction_held(did)[0]
    finally:
        _ss.direction_failure_streaks.clear()


@pytest.mark.asyncio
async def test_unhold_direction_idempotent_when_not_held(client: AsyncClient, mocker):
    """POST /unhold on a healthy direction returns 200 with was_held=False."""
    import app.shared_state as _ss
    project_id = await _deploy_project(client, mocker)
    direction = await _add_direction(client, project_id)

    _ss.direction_failure_streaks.clear()
    resp = await client.post(
        f"/api/admin/projects/{project_id}/directions/{direction['id']}/unhold",
    )
    assert resp.status_code == 200
    assert resp.json()["was_held"] is False


@pytest.mark.asyncio
async def test_unhold_direction_not_found_returns_404(client: AsyncClient, mocker):
    """POST /unhold on an unknown direction id returns 404."""
    project_id = await _deploy_project(client, mocker)
    resp = await client.post(
        f"/api/admin/projects/{project_id}/directions/d-does-not-exist/unhold",
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_update_direction_priority(client: AsyncClient, mocker):
    """PATCH updates direction priority."""
    project_id = await _deploy_project(client, mocker)
    direction = await _add_direction(client, project_id, priority="low")

    resp = await client.patch(
        f"/api/admin/projects/{project_id}/directions/{direction['id']}",
        json={"priority": "high"},
    )
    assert resp.status_code == 200
    assert resp.json()["direction"]["priority"] == "high"


@pytest.mark.asyncio
async def test_update_direction_status(client: AsyncClient, mocker):
    """PATCH updates direction status to paused."""
    project_id = await _deploy_project(client, mocker)
    direction = await _add_direction(client, project_id)

    resp = await client.patch(
        f"/api/admin/projects/{project_id}/directions/{direction['id']}",
        json={"status": "paused"},
    )
    assert resp.status_code == 200
    assert resp.json()["direction"]["status"] == "paused"


@pytest.mark.asyncio
async def test_update_direction_text(client: AsyncClient, mocker):
    """PATCH updates direction text."""
    project_id = await _deploy_project(client, mocker)
    direction = await _add_direction(client, project_id, text="Original text")

    resp = await client.patch(
        f"/api/admin/projects/{project_id}/directions/{direction['id']}",
        json={"text": "Updated text"},
    )
    assert resp.status_code == 200
    assert resp.json()["direction"]["text"] == "Updated text"


@pytest.mark.asyncio
async def test_update_direction_paths(client: AsyncClient, mocker):
    """PATCH updates direction paths."""
    project_id = await _deploy_project(client, mocker)
    direction = await _add_direction(client, project_id, paths=["old/path"])

    resp = await client.patch(
        f"/api/admin/projects/{project_id}/directions/{direction['id']}",
        json={"paths": ["new/path1", "new/path2"]},
    )
    assert resp.status_code == 200
    assert resp.json()["direction"]["paths"] == ["new/path1", "new/path2"]


@pytest.mark.asyncio
async def test_delete_direction_removes_it(client: AsyncClient, mocker):
    """DELETE removes the direction from the project."""
    project_id = await _deploy_project(client, mocker)
    direction = await _add_direction(client, project_id)

    resp = await client.delete(
        f"/api/admin/projects/{project_id}/directions/{direction['id']}"
    )
    assert resp.status_code == 200
    assert resp.json()["message"] == "Direction removed"

    get_resp = await client.get(f"/api/admin/projects/{project_id}/directions")
    ids = [d["id"] for d in get_resp.json()["directions"]]
    assert direction["id"] not in ids


@pytest.mark.asyncio
async def test_delete_direction_cancels_queued_tasks(client: AsyncClient, db_session, mocker):
    """DELETE cancels QUEUED tasks whose context.direction_id matches the deleted direction."""
    project_id = await _deploy_project(client, mocker)
    direction = await _add_direction(client, project_id, text="Direction to delete")
    direction_id = direction["id"]

    # Create a QUEUED task that references this direction in its context
    task = Task(
        project_id=uuid.UUID(project_id),
        title="Autonomous research task",
        payloads=[{"type": "research", "directions": [direction], "base_branch": "main"}],
        context={"direction_id": direction_id},
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(task)
    await db_session.commit()

    resp = await client.delete(
        f"/api/admin/projects/{project_id}/directions/{direction_id}"
    )
    assert resp.status_code == 200
    assert resp.json()["cancelled_tasks"] == 1

    # Verify the task status is now CANCELLED
    await db_session.refresh(task)
    assert task.status == TaskStatus.CANCELLED


# ─────────────────────────────────────────────────────────────────────────────
# 2. Cooldown Filter (_spawn_research_task)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cooldown_filter_excludes_recently_researched_direction(db_session):
    """A direction researched 1 hour ago (within 4-hour cooldown) should not trigger a task."""
    project = Project(
        name="Cooldown Test — Recent",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[{
            "id": "d-recent01",
            "text": "Researched recently",
            "status": "active",
            "priority": "medium",
            "paths": [],
            "added_at": "2026-01-01T00:00:00",
            "last_researched_at": (datetime.utcnow() - timedelta(hours=1)).isoformat(),
        }],
        vision={"purpose": "test"},  # Non-empty vision so force_strategic doesn't bypass
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _spawn_research_task(project, db_session)

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    tasks = result.scalars().all()
    assert len(tasks) == 0, "No task should be spawned for a recently-researched direction"


@pytest.mark.asyncio
async def test_cooldown_filter_includes_direction_past_cooldown(db_session):
    """A direction researched 5 hours ago (past 4-hour cooldown) should trigger a task."""
    project = Project(
        name="Cooldown Test — Past",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[{
            "id": "d-past0001",
            "text": "Researched long ago",
            "status": "active",
            "priority": "medium",
            "paths": [],
            "added_at": "2026-01-01T00:00:00",
            "last_researched_at": (datetime.utcnow() - timedelta(hours=5)).isoformat(),
        }],
        vision={"purpose": "test"},  # Non-empty vision so force_strategic doesn't bypass
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _spawn_research_task(project, db_session)

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    tasks = result.scalars().all()
    research_tasks = [t for t in tasks if (t.payloads or [{}])[0].get("type") == "research"]
    assert len(research_tasks) == 1, "A research task should be spawned when past cooldown"


@pytest.mark.asyncio
async def test_cooldown_filter_includes_direction_never_researched(db_session):
    """A direction with no last_researched_at is always eligible for research."""
    project = Project(
        name="Cooldown Test — Never",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[{
            "id": "d-never001",
            "text": "Never been researched",
            "status": "active",
            "priority": "high",
            "paths": [],
            "added_at": "2026-01-01T00:00:00",
            # no last_researched_at key
        }],
        vision={"purpose": "test"},  # Non-empty vision so force_strategic doesn't bypass
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _spawn_research_task(project, db_session)

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    tasks = result.scalars().all()
    research_tasks = [t for t in tasks if (t.payloads or [{}])[0].get("type") == "research"]
    assert len(research_tasks) == 1, "A research task should be spawned for a never-researched direction"


@pytest.mark.asyncio
async def test_spawn_skipped_when_autonomous_disabled(db_session):
    """_spawn_research_task does nothing when autonomous_enabled is False."""
    project = Project(
        name="Autonomous Disabled",
        user_id="test-user-123",
        autonomous_enabled=False,
        directions=[{
            "id": "d-dis00001",
            "text": "Should not be researched",
            "status": "active",
            "priority": "high",
            "paths": [],
            "added_at": "2026-01-01T00:00:00",
        }],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _spawn_research_task(project, db_session)

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    tasks = result.scalars().all()
    assert len(tasks) == 0


# ─────────────────────────────────────────────────────────────────────────────
# 3. last_researched_at Update (_handle_research_completion)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_handle_research_completion_updates_last_researched_at(db_session, mocker):
    """After _handle_research_completion runs, the researched direction gets last_researched_at set."""
    direction_id = "d-resrch01"
    direction = {
        "id": direction_id,
        "text": "Improve logging",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
    }

    project = Project(
        name="Research Completion Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Research: Improve logging",
        payloads=[{
            "type": "research",
            "directions": [direction],
            "base_branch": "main",
        }],
        context={"research_depth": 0},
        status=TaskStatus.IN_PROGRESS,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # Mock all external calls made inside _handle_research_completion
    mocker.patch(
        "app.task_completion.get_github_token_for_task",
        new=AsyncMock(return_value=("fake-token", "owner", "repo")),
    )
    mocker.patch("app.task_completion.get_branch_sha", new=AsyncMock(return_value="abc123sha"))
    mocker.patch("app.task_completion.create_branch", new=AsyncMock(return_value=None))
    mocker.patch("app.broadcast.broadcast_to_project", new=AsyncMock(return_value=None))
    mocker.patch("app.research_completion._validate_plan_dependencies", return_value=True)
    mocker.patch("app.research_completion._log_file_overlaps", return_value=[])
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[{
            "title": "Add unit tests for directions",
            "description": "Improve coverage of the directions module",
            "prompt": "Write unit tests for the directions CRUD endpoints",
            "priority": "medium",
            "scope": "small",
            "risk": "low",
            "type": "improvement",
            "direction_id": direction_id,
        }],
    )

    before = datetime.utcnow()
    await _handle_research_completion(task, "", db_session)

    # Reload project from DB to get the updated directions
    await db_session.refresh(project)
    updated_directions = project.directions
    assert updated_directions is not None

    researched = next((d for d in updated_directions if d["id"] == direction_id), None)
    assert researched is not None, "Direction should still exist after research completion"
    assert "last_researched_at" in researched, "last_researched_at should be set after completion"

    last_ts = datetime.fromisoformat(researched["last_researched_at"])
    assert last_ts >= before, "last_researched_at should be set to a time after the test started"


@pytest.mark.asyncio
async def test_handle_research_completion_no_findings_skips_update(db_session, mocker):
    """When there are no findings, last_researched_at is NOT updated and no tasks are created."""
    direction_id = "d-nofind01"
    direction = {
        "id": direction_id,
        "text": "Direction with no findings",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
    }

    project = Project(
        name="No Findings Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Research: Direction with no findings",
        payloads=[{"type": "research", "directions": [direction], "base_branch": "main"}],
        context={"research_depth": 0},
        status=TaskStatus.IN_PROGRESS,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    mocker.patch("app.research_completion._parse_research_findings", return_value=[])  # no findings

    await _handle_research_completion(task, "", db_session)

    await db_session.refresh(project)
    directions = project.directions
    researched = next(d for d in directions if d["id"] == direction_id)
    assert "last_researched_at" not in researched, (
        "last_researched_at should NOT be set when there are no findings"
    )

    # No child tasks should have been created
    result = await db_session.execute(
        select(Task).where(Task.project_id == project.id, Task.id != task.id)
    )
    child_tasks = result.scalars().all()
    assert len(child_tasks) == 0


@pytest.mark.asyncio
async def test_handle_research_completion_direction_update_sets_pending_update(db_session, mocker):
    """A direction_update finding stores pending_update on the target direction and creates no task."""
    direction_id = "d-dupdate1"
    direction = {
        "id": direction_id,
        "text": "Old direction text",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
    }

    project = Project(
        name="Direction Update Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Research: Old direction text",
        payloads=[{"type": "research", "directions": [direction], "base_branch": "main"}],
        context={"research_depth": 0},
        status=TaskStatus.IN_PROGRESS,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    mocker.patch("app.task_completion.get_github_token_for_task", new=AsyncMock(return_value=("tok", "o", "r")))
    mocker.patch("app.task_completion.get_branch_sha", new=AsyncMock(return_value="abc123sha"))
    mocker.patch("app.task_completion.create_branch", new=AsyncMock(return_value=None))
    mocker.patch("app.broadcast.broadcast_to_project", new=AsyncMock(return_value=None))
    mocker.patch("app.research_completion._validate_plan_dependencies", return_value=True)
    mocker.patch("app.research_completion._log_file_overlaps", return_value=[])
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[{
            "type": "direction_update",
            "direction_id": direction_id,
            "title": "Refine direction scope",
            "description": "The direction is too broad; narrow it to authentication only.",
            "suggested_text": "Focus on authentication module",
            "suggested_priority": "high",
            "suggested_paths": ["backend/app/auth.py"],
            "prompt": "",  # direction_update findings have no task prompt
        }],
    )

    await _handle_research_completion(task, "", db_session)

    # Reload from DB
    await db_session.refresh(project)
    updated_directions = project.directions
    target = next((d for d in updated_directions if d["id"] == direction_id), None)
    assert target is not None

    # pending_update should be set
    assert "pending_update" in target, "pending_update should be written to the direction"
    pu = target["pending_update"]
    assert pu["suggested_text"] == "Focus on authentication module"
    assert pu["suggested_priority"] == "high"
    assert pu["suggested_paths"] == ["backend/app/auth.py"]
    assert "rationale" in pu
    assert "proposed_at" in pu

    # No child task should have been created
    result = await db_session.execute(
        select(Task).where(Task.project_id == project.id, Task.id != task.id)
    )
    child_tasks = result.scalars().all()
    assert len(child_tasks) == 0, "direction_update findings must not create child tasks"



@pytest.mark.asyncio
async def test_handle_research_completion_direction_note_appends_note(db_session, mocker):
    """A direction_note finding appends a note to the target direction and creates no task."""
    direction_id = "d-dnote01"
    direction = {
        "id": direction_id,
        "text": "Improve logging coverage",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
    }

    project = Project(
        name="Direction Note Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Research: Improve logging coverage",
        payloads=[{"type": "research", "directions": [direction], "base_branch": "main"}],
        context={"research_depth": 0},
        status=TaskStatus.IN_PROGRESS,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    mocker.patch("app.task_completion.get_github_token_for_task", new=AsyncMock(return_value=("tok", "o", "r")))
    mocker.patch("app.task_completion.get_branch_sha", new=AsyncMock(return_value="abc123sha"))
    mocker.patch("app.task_completion.create_branch", new=AsyncMock(return_value=None))
    mocker.patch("app.broadcast.broadcast_to_project", new=AsyncMock(return_value=None))
    mocker.patch("app.research_completion._validate_plan_dependencies", return_value=True)
    mocker.patch("app.research_completion._log_file_overlaps", return_value=[])
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[{
            "type": "direction_note",
            "direction_id": direction_id,
            "title": "Logging observation",
            "description": "Found that request errors are not logged at the middleware level.",
            "prompt": "",  # direction_note findings have no task prompt
        }],
    )

    await _handle_research_completion(task, "", db_session)

    # Reload from DB
    await db_session.refresh(project)
    updated_directions = project.directions
    target = next((d for d in updated_directions if d["id"] == direction_id), None)
    assert target is not None

    # Note should be appended
    assert "notes" in target, "notes list should be written to the direction"
    assert len(target["notes"]) == 1
    note = target["notes"][0]
    assert note["text"] == "Found that request errors are not logged at the middleware level."
    assert "added_at" in note
    assert "added_by_task" in note

    # No child task should have been created
    result = await db_session.execute(
        select(Task).where(Task.project_id == project.id, Task.id != task.id)
    )
    child_tasks = result.scalars().all()
    assert len(child_tasks) == 0, "direction_note findings must not create child tasks"


# ─────────────────────────────────────────────────────────────────────────────
# 4. Task-Counts Endpoint
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_task_counts_empty(client: AsyncClient, mocker):
    """task-counts returns an empty dict when no research tasks exist."""
    project_id = await _deploy_project(client, mocker)

    resp = await client.get(f"/api/admin/projects/{project_id}/directions/task-counts")
    assert resp.status_code == 200
    assert resp.json()["counts"] == {}


@pytest.mark.asyncio
async def test_task_counts_single_direction(client: AsyncClient, mocker):
    """task-counts returns count=1 after one research task is triggered for a direction."""
    project_id = await _deploy_project(client, mocker)
    direction = await _add_direction(client, project_id, text="Direction for research")
    direction_id = direction["id"]

    research_resp = await client.post(
        f"/api/admin/projects/{project_id}/directions/{direction_id}/research"
    )
    assert research_resp.status_code == 200

    resp = await client.get(f"/api/admin/projects/{project_id}/directions/task-counts")
    assert resp.status_code == 200
    counts = resp.json()["counts"]
    assert counts.get(direction_id, 0) == 1


@pytest.mark.asyncio
async def test_task_counts_multiple_directions(client: AsyncClient, mocker):
    """task-counts tracks counts independently per direction."""
    project_id = await _deploy_project(client, mocker)
    dir_a = await _add_direction(client, project_id, text="Direction A")
    dir_b = await _add_direction(client, project_id, text="Direction B")

    await client.post(f"/api/admin/projects/{project_id}/directions/{dir_a['id']}/research")
    await client.post(f"/api/admin/projects/{project_id}/directions/{dir_b['id']}/research")

    resp = await client.get(f"/api/admin/projects/{project_id}/directions/task-counts")
    assert resp.status_code == 200
    counts = resp.json()["counts"]
    assert counts[dir_a["id"]] == 1
    assert counts[dir_b["id"]] == 1


@pytest.mark.asyncio
async def test_task_counts_ignores_non_research_tasks(client: AsyncClient, mocker):
    """task-counts does not count tasks whose payload type is not 'research'."""
    project_id = await _deploy_project(client, mocker)
    direction = await _add_direction(client, project_id, text="Direction C")
    direction_id = direction["id"]

    # The initial deploy task has type != "research" — it should not be counted
    resp = await client.get(f"/api/admin/projects/{project_id}/directions/task-counts")
    assert resp.status_code == 200
    counts = resp.json()["counts"]
    assert counts.get(direction_id, 0) == 0


@pytest.mark.asyncio
async def test_task_counts_excludes_cancelled_and_failed(client: AsyncClient, db_session, mocker):
    """task-counts must not count CANCELLED or FAILED research tasks."""
    project_id = await _deploy_project(client, mocker)
    direction = await _add_direction(client, project_id, text="Direction for cancelled test")
    direction_id = direction["id"]

    # Insert a CANCELLED research task directly into the DB
    cancelled_task = Task(
        project_id=uuid.UUID(project_id),
        title="Research: cancelled",
        payloads=[{"type": "research", "directions": [direction], "base_branch": "main"}],
        context={"direction_id": direction_id},
        status=TaskStatus.CANCELLED,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    # Insert a FAILED research task directly into the DB
    failed_task = Task(
        project_id=uuid.UUID(project_id),
        title="Research: failed",
        payloads=[{"type": "research", "directions": [direction], "base_branch": "main"}],
        context={"direction_id": direction_id},
        status=TaskStatus.FAILED,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(cancelled_task)
    db_session.add(failed_task)
    await db_session.commit()

    resp = await client.get(f"/api/admin/projects/{project_id}/directions/task-counts")
    assert resp.status_code == 200
    counts = resp.json()["counts"]
    assert counts.get(direction_id, 0) == 0, (
        "CANCELLED and FAILED tasks must not be included in task-counts"
    )


@pytest.mark.asyncio
async def test_task_counts_project_not_found(client: AsyncClient):
    """task-counts returns 404 for a nonexistent project."""
    fake_id = str(uuid.uuid4())
    resp = await client.get(f"/api/admin/projects/{fake_id}/directions/task-counts")
    assert resp.status_code == 404


# ─────────────────────────────────────────────────────────────────────────────
# 5. Status and Priority Validation
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_update_direction_invalid_status_returns_400(client: AsyncClient, mocker):
    """PATCH with an unrecognised status value returns 400."""
    project_id = await _deploy_project(client, mocker)
    direction = await _add_direction(client, project_id)

    resp = await client.patch(
        f"/api/admin/projects/{project_id}/directions/{direction['id']}",
        json={"status": "invalid_status"},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_update_direction_invalid_priority_returns_400(client: AsyncClient, mocker):
    """PATCH with an unrecognised priority value returns 400."""
    project_id = await _deploy_project(client, mocker)
    direction = await _add_direction(client, project_id)

    resp = await client.patch(
        f"/api/admin/projects/{project_id}/directions/{direction['id']}",
        json={"priority": "urgent"},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_update_direction_empty_text_returns_400(client: AsyncClient, mocker):
    """PATCH with empty text returns 400."""
    project_id = await _deploy_project(client, mocker)
    direction = await _add_direction(client, project_id)

    resp = await client.patch(
        f"/api/admin/projects/{project_id}/directions/{direction['id']}",
        json={"text": ""},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_update_direction_not_found_returns_404(client: AsyncClient, mocker):
    """PATCH on a nonexistent direction_id returns 404."""
    project_id = await _deploy_project(client, mocker)

    resp = await client.patch(
        f"/api/admin/projects/{project_id}/directions/d-notexist",
        json={"priority": "low"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_directions_project_not_found(client: AsyncClient):
    """GET directions for a nonexistent project returns 404."""
    resp = await client.get(f"/api/admin/projects/{uuid.uuid4()}/directions")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_nonexistent_direction_returns_404(client: AsyncClient, mocker):
    """DELETE a direction that does not exist returns 404."""
    project_id = await _deploy_project(client, mocker)
    resp = await client.delete(
        f"/api/admin/projects/{project_id}/directions/d-notexist"
    )
    assert resp.status_code == 404


# ─────────────────────────────────────────────────────────────────────────────
# 7. max_parallel_research_per_project settings
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_settings_returns_max_parallel_default(client: AsyncClient):
    """GET /api/admin/settings returns max_parallel_research_per_project with default 3."""
    resp = await client.get("/api/admin/settings")
    assert resp.status_code == 200
    data = resp.json()
    assert "max_parallel_research_per_project" in data
    assert data["max_parallel_research_per_project"] == 3


@pytest.mark.asyncio
async def test_patch_settings_max_parallel_persists(client: AsyncClient):
    """PATCH /api/admin/settings with max_parallel_research_per_project updates the value."""
    resp = await client.patch("/api/admin/settings", json={"max_parallel_research_per_project": 5})
    assert resp.status_code == 200

    resp2 = await client.get("/api/admin/settings")
    assert resp2.status_code == 200
    assert resp2.json()["max_parallel_research_per_project"] == 5


@pytest.mark.asyncio
async def test_patch_settings_max_parallel_invalid_returns_400(client: AsyncClient):
    """PATCH /api/admin/settings with out-of-range max_parallel_research_per_project returns 400."""
    resp = await client.patch("/api/admin/settings", json={"max_parallel_research_per_project": 0})
    assert resp.status_code == 400

    resp2 = await client.patch("/api/admin/settings", json={"max_parallel_research_per_project": 21})
    assert resp2.status_code == 400


# ─────────────────────────────────────────────────────────────────────────────
# Standalone tests for _compute_merge_rate (no DB required)
# ─────────────────────────────────────────────────────────────────────────────

class TestComputeMergeRate:
    """Unit tests for _compute_merge_rate — pure function, no DB."""

    def test_never_researched_returns_neutral(self):
        """A direction with no history returns 0.5 (neutral/unknown)."""
        d = {"id": "d1", "priority": "medium"}
        assert _compute_merge_rate(d) == 0.5

    def test_empty_history_returns_neutral(self):
        """Explicit empty lists also return 0.5."""
        d = {"id": "d1", "research_history": [], "history_aggregate": {}}
        assert _compute_merge_rate(d) == 0.5

    def test_history_with_no_merges(self):
        """Cycles present but zero merges → rate = 0.0."""
        d = {
            "research_history": [
                {"prs_merged": 0},
                {"prs_merged": 0},
            ]
        }
        assert _compute_merge_rate(d) == 0.0

    def test_history_with_merges(self):
        """3 cycles, 6 total merges → rate = 2.0."""
        d = {
            "research_history": [
                {"prs_merged": 1},
                {"prs_merged": 3},
                {"prs_merged": 2},
            ]
        }
        assert _compute_merge_rate(d) == pytest.approx(6 / 3)

    def test_aggregate_counted(self):
        """history_aggregate cycles and merges are included in the total."""
        d = {
            "research_history": [{"prs_merged": 1}],
            "history_aggregate": {"total_cycles": 4, "total_prs_merged": 8},
        }
        # lifetime: 1 live + 4 agg = 5 cycles, 1+8=9 merged → 1.8
        # recent (last 3 live): [{"prs_merged":1}] → rate=1.0
        # weighted: (1.0*3 + 1.8) / 4 = 1.2
        assert _compute_merge_rate(d) == pytest.approx(1.2)

    def test_higher_merge_rate_sorted_first(self):
        """Within the same priority tier, higher merge_rate direction is selected first."""
        priority_rank = {"high": 0, "medium": 1, "low": 2}
        now = "2026-01-01T00:00:00"

        # Both medium priority, both researched — differ only by merge rate
        low_rate = {
            "id": "low",
            "priority": "medium",
            "possibly_saturated": False,
            "last_researched_at": now,
            "research_history": [{"prs_merged": 0}, {"prs_merged": 0}],
        }
        high_rate = {
            "id": "high",
            "priority": "medium",
            "possibly_saturated": False,
            "last_researched_at": now,
            "research_history": [{"prs_merged": 1}, {"prs_merged": 1}],
        }

        directions = [low_rate, high_rate]
        directions.sort(key=lambda d: (
            priority_rank.get(d.get("priority", "medium"), 1),
            1 if d.get("possibly_saturated") else 0,
            -_compute_merge_rate(d),
            d.get("last_researched_at") or "",
        ))
        assert directions[0]["id"] == "high"

    def test_never_researched_takes_precedence_over_merge_rate(self):
        """Directions with no last_researched_at are picked first (never_researched path)."""
        import random
        random.seed(42)

        # Simulate the never_researched branch: a direction with high merge rate
        # that has been researched should NOT beat a never-researched direction.
        never = {"id": "never", "priority": "medium", "last_researched_at": None}
        productive = {
            "id": "productive",
            "priority": "medium",
            "last_researched_at": "2026-01-01T00:00:00",
            "research_history": [{"prs_merged": 5}],
        }

        never_researched = [d for d in [never, productive] if not d.get("last_researched_at")]
        assert never_researched == [never], "Only the never-researched direction qualifies"

    def test_saturated_deprioritized_regardless_of_merge_rate(self):
        """Saturated directions sort after non-saturated ones even with higher merge rate."""
        priority_rank = {"high": 0, "medium": 1, "low": 2}
        now = "2026-01-01T00:00:00"

        saturated_high = {
            "id": "sat",
            "priority": "medium",
            "possibly_saturated": True,
            "last_researched_at": now,
            "research_history": [{"prs_merged": 10}],
        }
        unsaturated_low = {
            "id": "unsat",
            "priority": "medium",
            "possibly_saturated": False,
            "last_researched_at": now,
            "research_history": [{"prs_merged": 0}],
        }

        directions = [saturated_high, unsaturated_low]
        directions.sort(key=lambda d: (
            priority_rank.get(d.get("priority", "medium"), 1),
            1 if d.get("possibly_saturated") else 0,
            -_compute_merge_rate(d),
            d.get("last_researched_at") or "",
        ))
        assert directions[0]["id"] == "unsat"


# ─────────────────────────────────────────────────────────────────────────────
# 8. Direction Cap, completion_criteria, and merge_rate in list
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_direction_cap_blocks_excess_active(client: AsyncClient, mocker):
    """Creating an 11th active direction returns 409 when cap is 10."""
    project_id = await _deploy_project(client, mocker)
    for i in range(10):
        resp = await client.post(
            f"/api/admin/projects/{project_id}/directions",
            json={"text": f"Direction {i + 1}", "priority": "medium"},
        )
        assert resp.status_code == 200

    resp = await client.post(
        f"/api/admin/projects/{project_id}/directions",
        json={"text": "Direction 11 - should be blocked", "priority": "medium"},
    )
    assert resp.status_code == 409
    assert "Direction cap reached" in resp.json()["error"]["message"]


@pytest.mark.asyncio
async def test_direction_cap_allows_non_active(client: AsyncClient, mocker):
    """proposed and paused directions do not count toward the cap."""
    project_id = await _deploy_project(client, mocker)
    # Create 5 active directions to hit the cap
    for i in range(5):
        await client.post(
            f"/api/admin/projects/{project_id}/directions",
            json={"text": f"Active {i + 1}", "priority": "medium"},
        )
    # Pause one to make room
    resp = await client.get(f"/api/admin/projects/{project_id}/directions")
    directions = resp.json()["directions"]
    first_id = directions[0]["id"]
    await client.patch(
        f"/api/admin/projects/{project_id}/directions/{first_id}",
        json={"status": "paused"},
    )
    # Now we have 4 active, 1 paused — creating another active should succeed
    resp = await client.post(
        f"/api/admin/projects/{project_id}/directions",
        json={"text": "Direction 6 - now allowed", "priority": "medium"},
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_direction_cap_on_reactivation(client: AsyncClient, mocker):
    """PATCH setting status to 'active' returns 409 when cap is already at 10."""
    project_id = await _deploy_project(client, mocker)
    # Create 9 active, then one more to reach cap of 10
    for i in range(9):
        await client.post(
            f"/api/admin/projects/{project_id}/directions",
            json={"text": f"Active {i + 1}", "priority": "medium"},
        )
    paused_resp = await client.post(
        f"/api/admin/projects/{project_id}/directions",
        json={"text": "To be paused", "priority": "low"},
    )
    # Now at cap (10 active). Pause one to allow creating an 11th, then re-activate to trigger 409.
    resp = await client.get(f"/api/admin/projects/{project_id}/directions")
    directions = resp.json()["directions"]
    # Pause one to allow creating an 11th
    first_id = directions[0]["id"]
    await client.patch(
        f"/api/admin/projects/{project_id}/directions/{first_id}",
        json={"status": "paused"},
    )
    # Now create 10th active direction (9 active + 1 paused = OK)
    tenth_resp = await client.post(
        f"/api/admin/projects/{project_id}/directions",
        json={"text": "Tenth direction", "priority": "medium"},
    )
    assert tenth_resp.status_code == 200
    # Now we have 10 active + 1 paused. Trying to reactivate the paused one should fail
    resp = await client.patch(
        f"/api/admin/projects/{project_id}/directions/{first_id}",
        json={"status": "active"},
    )
    assert resp.status_code == 409
    assert "Direction cap reached" in resp.json()["error"]["message"]


@pytest.mark.asyncio
async def test_merge_rate_in_list(client: AsyncClient, db_session, mocker):
    """GET directions returns merge_rate for each direction based on research_history."""
    project_id = await _deploy_project(client, mocker)
    direction = await _add_direction(client, project_id, text="Direction with history")
    direction_id = direction["id"]

    # Directly set research_history on the direction in the DB
    from app.models import Project as ProjectModel
    from sqlmodel import select as sa_select
    result = await db_session.execute(
        sa_select(ProjectModel).where(ProjectModel.id == uuid.UUID(project_id))
    )
    project = result.scalars().first()
    updated_directions = list(project.directions or [])
    for d in updated_directions:
        if d["id"] == direction_id:
            d["research_history"] = [{"prs_merged": 1}, {"prs_merged": 0}]
    from sqlalchemy.orm.attributes import flag_modified
    project.directions = updated_directions
    flag_modified(project, "directions")
    db_session.add(project)
    await db_session.commit()

    resp = await client.get(f"/api/admin/projects/{project_id}/directions")
    assert resp.status_code == 200
    dirs = resp.json()["directions"]
    target = next(d for d in dirs if d["id"] == direction_id)
    assert "merge_rate" in target
    assert abs(target["merge_rate"] - 0.5) < 0.001  # 1 merged / 2 cycles = 0.5


@pytest.mark.asyncio
async def test_completion_criteria_stored(client: AsyncClient, mocker):
    """POST direction with completion_criteria stores and returns it."""
    project_id = await _deploy_project(client, mocker)
    resp = await client.post(
        f"/api/admin/projects/{project_id}/directions",
        json={"text": "Direction with criteria", "priority": "medium", "completion_criteria": "All auth tests pass"},
    )
    assert resp.status_code == 200
    d = resp.json()["direction"]
    assert d["completion_criteria"] == "All auth tests pass"

    # Verify it's returned in the GET list too
    get_resp = await client.get(f"/api/admin/projects/{project_id}/directions")
    dirs = get_resp.json()["directions"]
    stored = next(x for x in dirs if x["id"] == d["id"])
    assert stored["completion_criteria"] == "All auth tests pass"


@pytest.mark.asyncio
async def test_completion_criteria_optional(client: AsyncClient, mocker):
    """POST direction without completion_criteria succeeds."""
    project_id = await _deploy_project(client, mocker)
    resp = await client.post(
        f"/api/admin/projects/{project_id}/directions",
        json={"text": "Direction without criteria", "priority": "low"},
    )
    assert resp.status_code == 200
    d = resp.json()["direction"]
    assert "completion_criteria" not in d or d.get("completion_criteria") is None


@pytest.mark.asyncio
async def test_put_directions_with_valid_completion_criteria(client: AsyncClient, mocker):
    """PUT directions with valid completion_criteria succeeds."""
    project_id = await _deploy_project(client, mocker)
    resp = await client.put(
        f"/api/admin/projects/{project_id}/directions",
        json=[{"text": "Direction A", "priority": "medium", "completion_criteria": "All tests pass"}],
    )
    assert resp.status_code == 200
    dirs = resp.json()["directions"]
    assert dirs[0]["completion_criteria"] == "All tests pass"


@pytest.mark.asyncio
async def test_put_directions_completion_criteria_too_long(client: AsyncClient, mocker):
    """PUT directions with completion_criteria exceeding 500 chars returns 400."""
    project_id = await _deploy_project(client, mocker)
    resp = await client.put(
        f"/api/admin/projects/{project_id}/directions",
        json=[{"text": "Direction A", "priority": "medium", "completion_criteria": "x" * 501}],
    )
    assert resp.status_code == 400


# ─────────────────────────────────────────────────────────────────────────────
# Auto-completion
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_auto_completion_triggers(db_session, mocker):
    """Direction auto-completes when saturated with >= 3 total merged PRs and current cycle has findings."""
    direction_id = "d-autocomp"
    direction = {
        "id": direction_id,
        "text": "Autocompletion target",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "research_history": [
            {"timestamp": "2026-01-01T01:00:00", "task_id": "t1", "tasks_created": 2, "prs_merged": 1},
            {"timestamp": "2026-01-02T01:00:00", "task_id": "t2", "tasks_created": 1, "prs_merged": 1},
            {"timestamp": "2026-01-03T01:00:00", "task_id": "t3", "tasks_created": 1, "prs_merged": 1},
        ],
    }

    project = Project(
        name="Auto-completion Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Research: Autocompletion target",
        payloads=[{"type": "research", "directions": [direction], "base_branch": "main"}],
        context={"research_depth": 0},
        status=TaskStatus.IN_PROGRESS,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    mocker.patch(
        "app.task_completion.get_github_token_for_task",
        new=AsyncMock(return_value=("fake-token", "owner", "repo")),
    )
    mocker.patch("app.task_completion.get_branch_sha", new=AsyncMock(return_value="abc123sha"))
    mocker.patch("app.task_completion.create_branch", new=AsyncMock(return_value=None))
    mocker.patch("app.broadcast.broadcast_to_project", new=AsyncMock(return_value=None))
    mocker.patch("app.research_completion._validate_plan_dependencies", return_value=True)
    mocker.patch("app.research_completion._log_file_overlaps", return_value=[])
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[{
            "title": "Final improvement",
            "description": "Last polish",
            "prompt": "Do the thing",
            "priority": "medium",
            "scope": "small",
            "risk": "low",
            "type": "improvement",
            "direction_id": direction_id,
        }],
    )

    await _handle_research_completion(task, "", db_session)

    await db_session.refresh(project)
    d = next(x for x in project.directions if x["id"] == direction_id)
    assert d["status"] == "completed"
    assert d["auto_completed"] is True
    assert "auto_completed_reason" in d
    assert "3" in d["auto_completed_reason"]  # mentions the PR count

    # Verify an INFO alert was created
    result = await db_session.execute(
        select(Alert).where(
            Alert.project_id == project.id,
            Alert.severity == AlertSeverity.INFO,
        )
    )
    alerts = result.scalars().all()
    assert len(alerts) == 1
    assert "auto-completed" in alerts[0].title.lower()


@pytest.mark.asyncio
async def test_auto_completion_skips_unsaturated(db_session, mocker):
    """Direction stays active when not saturated, even with enough PRs."""
    direction_id = "d-nosat01"
    direction = {
        "id": direction_id,
        "text": "Unsaturated direction",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "research_history": [
            {"timestamp": "2026-01-01T01:00:00", "task_id": "t1", "tasks_created": 5, "prs_merged": 2},
            {"timestamp": "2026-01-02T01:00:00", "task_id": "t2", "tasks_created": 4, "prs_merged": 2},
        ],
    }

    project = Project(
        name="Unsaturated Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Research: Unsaturated direction",
        payloads=[{"type": "research", "directions": [direction], "base_branch": "main"}],
        context={"research_depth": 0},
        status=TaskStatus.IN_PROGRESS,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    mocker.patch(
        "app.task_completion.get_github_token_for_task",
        new=AsyncMock(return_value=("fake-token", "owner", "repo")),
    )
    mocker.patch("app.task_completion.get_branch_sha", new=AsyncMock(return_value="abc123sha"))
    mocker.patch("app.task_completion.create_branch", new=AsyncMock(return_value=None))
    mocker.patch("app.broadcast.broadcast_to_project", new=AsyncMock(return_value=None))
    mocker.patch("app.research_completion._validate_plan_dependencies", return_value=True)
    mocker.patch("app.research_completion._log_file_overlaps", return_value=[])
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[{
            "title": "Active finding",
            "description": "Lots to do",
            "prompt": "Keep going",
            "priority": "medium",
            "scope": "medium",
            "risk": "low",
            "type": "improvement",
            "direction_id": direction_id,
        }],
    )

    await _handle_research_completion(task, "", db_session)

    await db_session.refresh(project)
    d = next(x for x in project.directions if x["id"] == direction_id)
    assert d["status"] == "active"
    assert d.get("auto_completed") is not True


@pytest.mark.asyncio
async def test_auto_completion_skips_insufficient_prs(db_session, mocker):
    """Direction stays active when saturated but with fewer than 3 total merged PRs."""
    direction_id = "d-fewprs1"
    direction = {
        "id": direction_id,
        "text": "Few PRs direction",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        # 3 cycles with <=1 tasks each triggers saturation, but only 2 PRs merged
        "research_history": [
            {"timestamp": "2026-01-01T01:00:00", "task_id": "t1", "tasks_created": 1, "prs_merged": 1},
            {"timestamp": "2026-01-02T01:00:00", "task_id": "t2", "tasks_created": 1, "prs_merged": 1},
            {"timestamp": "2026-01-03T01:00:00", "task_id": "t3", "tasks_created": 0, "prs_merged": 0},
        ],
    }

    project = Project(
        name="Few PRs Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Research: Few PRs direction",
        payloads=[{"type": "research", "directions": [direction], "base_branch": "main"}],
        context={"research_depth": 0},
        status=TaskStatus.IN_PROGRESS,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    mocker.patch(
        "app.task_completion.get_github_token_for_task",
        new=AsyncMock(return_value=("fake-token", "owner", "repo")),
    )
    mocker.patch("app.task_completion.get_branch_sha", new=AsyncMock(return_value="abc123sha"))
    mocker.patch("app.task_completion.create_branch", new=AsyncMock(return_value=None))
    mocker.patch("app.broadcast.broadcast_to_project", new=AsyncMock(return_value=None))
    mocker.patch("app.research_completion._validate_plan_dependencies", return_value=True)
    mocker.patch("app.research_completion._log_file_overlaps", return_value=[])
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[{
            "title": "Small finding",
            "description": "Minor tweak",
            "prompt": "Fix the thing",
            "priority": "low",
            "scope": "small",
            "risk": "low",
            "type": "improvement",
            "direction_id": direction_id,
        }],
    )

    await _handle_research_completion(task, "", db_session)

    await db_session.refresh(project)
    d = next(x for x in project.directions if x["id"] == direction_id)
    assert d["status"] == "active"
    assert d.get("auto_completed") is not True


# ─────────────────────────────────────────────────────────────────────────────
# Momentum factor
# ─────────────────────────────────────────────────────────────────────────────

class TestComputeMomentumFactor:
    """Unit tests for _compute_momentum_factor — pure function, no DB needed."""

    def test_momentum_factor_hot_streak(self):
        """Last 2 cycles both productive → 2.0 multiplier."""
        d = {"research_history": [
            {"tasks_created": 3},
            {"tasks_created": 2},
        ]}
        assert _compute_momentum_factor(d) == 2.0

    def test_momentum_factor_hot_streak_longer_history(self):
        """Hot streak detected even with older stalled entries."""
        d = {"research_history": [
            {"tasks_created": 0},
            {"tasks_created": 0},
            {"tasks_created": 5},
            {"tasks_created": 1},
        ]}
        assert _compute_momentum_factor(d) == 2.0

    def test_momentum_factor_stalled(self):
        """Last 3 cycles all empty → 0.5 multiplier."""
        d = {"research_history": [
            {"tasks_created": 0},
            {"tasks_created": 0},
            {"tasks_created": 0},
        ]}
        assert _compute_momentum_factor(d) == 0.5

    def test_momentum_factor_neutral_mixed(self):
        """Mixed history → 1.0 multiplier."""
        d = {"research_history": [
            {"tasks_created": 3},
            {"tasks_created": 0},
            {"tasks_created": 2},
        ]}
        assert _compute_momentum_factor(d) == 1.0

    def test_momentum_factor_neutral_short_history(self):
        """Only 1 entry — too short for either streak → 1.0."""
        d = {"research_history": [
            {"tasks_created": 0},
        ]}
        assert _compute_momentum_factor(d) == 1.0

    def test_momentum_factor_empty_history(self):
        """No history at all → 1.0."""
        d = {"research_history": []}
        assert _compute_momentum_factor(d) == 1.0

    def test_momentum_factor_no_history_key(self):
        """Missing research_history key → 1.0."""
        assert _compute_momentum_factor({}) == 1.0


# ─────────────────────────────────────────────────────────────────────────────
# Auto-recovery of auto-paused directions
# ─────────────────────────────────────────────────────────────────────────────

def _make_paused_direction(did: str, hours_ago: float, healthy_after_cooldown: bool = True) -> dict:
    """Build a direction that is auto-paused at the given age."""
    paused_at = (datetime.utcnow() - timedelta(hours=hours_ago)).isoformat()
    history = []
    if not healthy_after_cooldown:
        # Add saturated history so _check_direction_health still returns a reason
        history = [{"tasks_created": 0}] * 5
    return {
        "id": did,
        "text": "Paused direction",
        "status": "paused",
        "auto_paused": True,
        "paused_reason": "Saturated: last 5 research cycles produced no actionable findings",
        "paused_at": paused_at,
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "possibly_saturated": not healthy_after_cooldown,
        "research_history": history,
    }


@pytest.mark.asyncio
async def test_auto_recovery_after_cooldown(db_session):
    """Auto-paused direction recovers when health improves after the cooldown."""
    direction = _make_paused_direction("d-recov001", hours_ago=50, healthy_after_cooldown=True)
    project = Project(
        name="Auto-Recovery Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _spawn_research_task(project, db_session)

    await db_session.refresh(project)
    recovered = next(d for d in project.directions if d["id"] == "d-recov001")
    assert recovered["status"] == "active", "Direction should be reactivated after cooldown"
    assert "auto_paused" not in recovered, "auto_paused flag should be removed"
    assert "paused_reason" not in recovered, "paused_reason should be cleared"
    assert "paused_at" not in recovered, "paused_at should be cleared"


@pytest.mark.asyncio
async def test_auto_recovery_stays_paused_when_still_unhealthy(db_session):
    """Auto-paused direction stays paused when still unhealthy at cooldown."""
    direction = _make_paused_direction("d-unhlt001", hours_ago=50, healthy_after_cooldown=False)
    project = Project(
        name="Still Unhealthy Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _spawn_research_task(project, db_session)

    await db_session.refresh(project)
    d = next(x for x in project.directions if x["id"] == "d-unhlt001")
    assert d["status"] == "paused", "Still-unhealthy direction must remain paused"
    assert d.get("auto_paused") is True, "auto_paused flag must be preserved"


@pytest.mark.asyncio
async def test_auto_recovery_before_cooldown_stays_paused(db_session):
    """Auto-paused direction is not recovered before the cooldown expires."""
    direction = _make_paused_direction("d-early001", hours_ago=10, healthy_after_cooldown=True)
    project = Project(
        name="Early Recovery Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _spawn_research_task(project, db_session)

    await db_session.refresh(project)
    d = next(x for x in project.directions if x["id"] == "d-early001")
    assert d["status"] == "paused", "Direction should stay paused before cooldown expires"


@pytest.mark.asyncio
async def test_user_paused_direction_never_auto_recovered(db_session):
    """Directions paused by the user (auto_paused not set) are never auto-recovered."""
    direction = {
        "id": "d-usrpaus1",
        "text": "User-paused direction",
        "status": "paused",
        # auto_paused intentionally NOT set
        "paused_reason": "User manually paused",
        "paused_at": (datetime.utcnow() - timedelta(hours=100)).isoformat(),
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
    }
    project = Project(
        name="User Pause Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _spawn_research_task(project, db_session)

    await db_session.refresh(project)
    d = next(x for x in project.directions if x["id"] == "d-usrpaus1")
    assert d["status"] == "paused", "User-paused direction must NOT be auto-recovered"


@pytest.mark.asyncio
async def test_auto_recovery_respects_custom_cooldown(db_session, monkeypatch):
    """direction_auto_recovery_hours runtime config is respected."""
    import app.shared_state as _ss
    monkeypatch.setitem(_ss.runtime_config, "direction_auto_recovery_hours", 100)

    # Paused 50 hours ago — within the custom 100h cooldown
    direction = _make_paused_direction("d-cstmcld1", hours_ago=50, healthy_after_cooldown=True)
    project = Project(
        name="Custom Cooldown Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _spawn_research_task(project, db_session)

    await db_session.refresh(project)
    d = next(x for x in project.directions if x["id"] == "d-cstmcld1")
    assert d["status"] == "paused", "Direction should stay paused within the custom 100h cooldown"


# ─────────────────────────────────────────────────────────────────────────────
# Direction cap enforcement in autonomous creation paths
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_strategic_batch_create_respects_cap(db_session, mocker):
    """_handle_strategic_completion batch creates should not exceed max_active_directions."""
    from unittest.mock import AsyncMock
    from app.strategic_completion import _handle_strategic_completion
    from app.shared_state import runtime_config as _runtime_config

    mocker.patch("app.main._create_strategic_alert", new_callable=AsyncMock)

    # Seed runtime config cap to 3 to make the test fast
    original_cap = _runtime_config.get("max_active_directions")
    _runtime_config["max_active_directions"] = 3

    try:
        # Project already has 3 active directions (at cap)
        existing_dirs = [
            {"id": f"d-exist{i}", "text": f"Existing {i}", "status": "active", "priority": "medium",
             "notes": [], "research_history": []}
            for i in range(3)
        ]
        project = Project(
            name="P",
            user_id="u1",
            vision={"purpose": "test", "revision": 1},
            directions=existing_dirs,
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        task = Task(
            project_id=project.id,
            payloads=[{"type": "strategic_review", "prompt": "review",
                       "vision": {"revision": 1}}],
            context={},
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        # Log output tries to create 2 new directions
        import json
        logs = json.dumps({
            "vision": {"purpose": "test", "revision": 2},
            "direction_operations": [
                {"op": "create", "text": "New direction A", "priority": "medium", "rationale": "needed"},
                {"op": "create", "text": "New direction B", "priority": "low", "rationale": "needed"},
            ],
        })

        await _handle_strategic_completion(task, logs, db_session)

        await db_session.refresh(project)
        active_count = sum(1 for d in (project.directions or []) if d.get("status") == "active")
        assert active_count <= 3, f"Expected at most 3 active directions, got {active_count}"
    finally:
        if original_cap is None:
            _runtime_config.pop("max_active_directions", None)
        else:
            _runtime_config["max_active_directions"] = original_cap


@pytest.mark.asyncio
async def test_strategic_api_create_direction_respects_cap(db_session, mocker):
    """POST /api/agent/strategic with op=create_direction should skip when cap is reached."""
    from httpx import AsyncClient, ASGITransport
    from app.main import app
    from app.database import get_session
    from app.routers.agent import verify_agent_token
    from app.shared_state import runtime_config as _runtime_config

    mocker.patch("app.main.provision_agent_vm", return_value=None)

    original_cap = _runtime_config.get("max_active_directions")
    _runtime_config["max_active_directions"] = 2

    try:
        # Project with 2 active directions (at cap)
        existing_dirs = [
            {"id": f"d-sapi{i}", "text": f"Strategic API Dir {i}", "status": "active", "priority": "medium",
             "notes": [], "research_history": []}
            for i in range(2)
        ]
        project = Project(
            name="P-strategic-api",
            user_id="u1",
            vision={"purpose": "test", "revision": 1},
            directions=existing_dirs,
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        task = Task(
            project_id=project.id,
            user_id="u1",
            task_type="strategic_review",
            status=TaskStatus.IN_PROGRESS,
            payloads=[{"command": "review", "vision": {"revision": 1}}],
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        task_id = str(task.id)

        async def _override_session():
            yield db_session

        app.dependency_overrides[get_session] = _override_session
        app.dependency_overrides[verify_agent_token] = lambda: {"sub": task_id, "task_type": "strategic_review"}

        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                resp = await ac.post(
                    "/api/agent/strategic",
                    json={"op": "create_direction", "task_id": task_id,
                          "data": {"text": "Overcap direction", "priority": "medium"}},
                )
            assert resp.status_code == 409, resp.text
            body = resp.json()
            assert body["status"] == "skipped"
            assert "cap" in body.get("reason", "").lower()
        finally:
            app.dependency_overrides.pop(get_session, None)
            app.dependency_overrides.pop(verify_agent_token, None)

        # Confirm no new direction was added
        await db_session.refresh(project)
        active_count = sum(1 for d in (project.directions or []) if d.get("status") == "active")
        assert active_count == 2, f"Expected 2 active directions, got {active_count}"
    finally:
        if original_cap is None:
            _runtime_config.pop("max_active_directions", None)
        else:
            _runtime_config["max_active_directions"] = original_cap


@pytest.mark.asyncio
async def test_strategic_api_create_direction_skips_duplicate(db_session, mocker):
    """POST /api/agent/strategic with op=create_direction returns 409 for duplicate text."""
    from httpx import AsyncClient, ASGITransport
    from app.main import app
    from app.database import get_session
    from app.routers.agent import verify_agent_token

    mocker.patch("app.main.provision_agent_vm", return_value=None)

    existing_dirs = [
        {"id": "d-dup001", "text": "Improve test coverage", "status": "active", "priority": "medium",
         "notes": [], "research_history": []}
    ]
    project = Project(
        name="P-strategic-dup",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=existing_dirs,
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        user_id="u1",
        task_type="strategic_review",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"command": "review", "vision": {"revision": 1}}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    task_id = str(task.id)

    async def _override_session():
        yield db_session

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[verify_agent_token] = lambda: {"sub": task_id, "task_type": "strategic_review"}

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.post(
                "/api/agent/strategic",
                json={"op": "create_direction", "task_id": task_id,
                      "data": {"text": "Improve test coverage", "priority": "high"}},
            )
        assert resp.status_code == 409, resp.text
        body = resp.json()
        assert body["status"] == "skipped"
        assert "duplicate" in body.get("reason", "").lower()

        await db_session.refresh(task)
        assert task.context is not None
        assert task.context.get("_strategic_api_called") is True
    finally:
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(verify_agent_token, None)


# ─────────────────────────────────────────────────────────────────────────────
# Standalone tests for _calibrate_scope (no DB required)
# ─────────────────────────────────────────────────────────────────────────────

class TestCalibrateScope:
    """Unit tests for _calibrate_scope — pure function, no DB."""

    def _direction(self, scope_breakdown_per_entry: list[dict]) -> dict:
        """Build a minimal direction dict with research_history entries."""
        return {
            "id": "d-test",
            "title": "Test direction",
            "research_history": [
                {"scope_breakdown": bd} for bd in scope_breakdown_per_entry
            ],
        }

    def test_no_history_returns_unchanged(self, monkeypatch):
        """Direction with no research_history + no baselines → scope unchanged."""
        from app.shared_state import runtime_config as _rc
        monkeypatch.setitem(_rc, "pipeline_scope_baseline_rates", {})
        d = {"id": "d1"}
        assert _calibrate_scope("medium", d) == ("medium", None)

    def test_empty_history_returns_unchanged(self, monkeypatch):
        """Explicit empty research_history + no baselines → scope unchanged."""
        from app.shared_state import runtime_config as _rc
        monkeypatch.setitem(_rc, "pipeline_scope_baseline_rates", {})
        d = {"id": "d1", "research_history": []}
        assert _calibrate_scope("medium", d) == ("medium", None)

    def test_insufficient_data_points_returns_unchanged(self):
        """Less than 3 total tasks across history → no calibration (insufficient signal)."""
        d = self._direction([
            {"small": {"created": 1, "merged": 0}},
            {"small": {"created": 1, "merged": 0}},
        ])
        # total_data_points = 2 < 3
        assert _calibrate_scope("small", d) == ("small", None)

    def test_acceptable_rate_returns_unchanged(self):
        """Proposed scope at >= 40% merge rate → no downgrade."""
        d = self._direction([
            {"medium": {"created": 5, "merged": 2}},  # 40%
        ])
        assert _calibrate_scope("medium", d) == ("medium", None)

    def test_exactly_40_percent_returns_unchanged(self):
        """Exactly 40% merge rate → not downgraded (rate >= 0.4 threshold)."""
        d = self._direction([
            {"medium": {"created": 5, "merged": 2}},  # 40% exactly
        ])
        result_scope, calibrated_from = _calibrate_scope("medium", d)
        assert result_scope == "medium"
        assert calibrated_from is None

    def test_below_threshold_downgrades_to_smaller_scope(self):
        """Proposed scope below 40% → downgraded to next smaller scope with acceptable rate.

        Uses 'large' scope to exercise the downgrade path without triggering the
        medium→large(decompose) upgrade.
        """
        d = self._direction([
            {"large": {"created": 10, "merged": 3}},   # 30% < 40%
            {"medium": {"created": 5, "merged": 3}},   # 60% >= 40%
        ])
        result_scope, calibrated_from = _calibrate_scope("large", d)
        assert result_scope == "medium"
        assert calibrated_from == "large"

    def test_smaller_scope_also_bad_returns_unchanged(self):
        """Proposed scope below 40% but no smaller scope has acceptable rate → no downgrade."""
        d = self._direction([
            {"medium": {"created": 10, "merged": 2}},  # 20%
            {"small": {"created": 5, "merged": 1}},    # 20%
        ])
        result_scope, calibrated_from = _calibrate_scope("medium", d)
        assert result_scope == "medium"
        assert calibrated_from is None

    def test_smaller_scope_with_no_data_is_acceptable(self):
        """Smaller scope with no historical data (rate=None) is treated as acceptable."""
        d = self._direction([
            {"medium": {"created": 10, "merged": 2}},  # 20% < 40%
            # no "small" data
        ])
        result_scope, calibrated_from = _calibrate_scope("medium", d)
        assert result_scope == "small"
        assert calibrated_from == "medium"

    def test_boundary_40_percent_smaller_scope_is_accepted(self):
        """Smaller scope at exactly 40% merge rate should be accepted as downgrade target.

        Uses 'large' scope to bypass the medium→large(decompose) upgrade path.
        """
        d = self._direction([
            {"large": {"created": 10, "merged": 3}},   # 30% — triggers downgrade search
            {"medium": {"created": 5, "merged": 2}},   # 40% — should be accepted (>= 0.4)
        ])
        result_scope, calibrated_from = _calibrate_scope("large", d)
        assert result_scope == "medium"
        assert calibrated_from == "large"

    def test_large_scope_downgrades_through_medium_to_small(self):
        """Large scope below threshold downgrades: picks first smaller scope with acceptable rate."""
        d = self._direction([
            {"large": {"created": 10, "merged": 2}},   # 20%
            {"medium": {"created": 10, "merged": 2}},  # 20%
            {"small": {"created": 5, "merged": 3}},    # 60%
        ])
        result_scope, calibrated_from = _calibrate_scope("large", d)
        assert result_scope == "small"
        assert calibrated_from == "large"

    def test_small_scope_cannot_be_downgraded(self):
        """Small is the smallest scope — even with low rate, no downgrade possible."""
        d = self._direction([
            {"small": {"created": 10, "merged": 1}},  # 10%
        ])
        result_scope, calibrated_from = _calibrate_scope("small", d)
        assert result_scope == "small"
        assert calibrated_from is None

    def test_uses_only_last_3_history_entries(self):
        """Only the last 3 history entries are considered; older entries are ignored."""
        # Older entries show bad rate for small, recent entries show good rate for medium
        older_bad = {"small": {"created": 100, "merged": 0}}   # very bad
        recent_good = {"medium": {"created": 5, "merged": 4}}  # 80% good
        d = self._direction([older_bad, older_bad, older_bad, older_bad, recent_good, recent_good, recent_good])
        # Only last 3 (recent_good x3) used → medium rate 80%, no downgrade
        assert _calibrate_scope("medium", d) == ("medium", None)

    def test_unknown_scope_returns_unchanged(self):
        """Unrecognized scope string → no downgrade (not in SCOPE_ORDER)."""
        d = self._direction([
            {"xlarge": {"created": 10, "merged": 0}},
        ])
        assert _calibrate_scope("xlarge", d) == ("xlarge", None)

    def _project(self, dirs_history: list[list[dict]]):
        """Build a minimal project stub with .directions populated."""
        class _P:
            pass
        p = _P()
        p.directions = [
            {"id": f"d{i}", "research_history": [{"scope_breakdown": bd} for bd in hist]}
            for i, hist in enumerate(dirs_history)
        ]
        return p

    def test_project_fallback_downgrades_when_direction_data_insufficient(self):
        """Direction has < 3 data points; project-wide rate < 30% → downgrade.

        Uses 'large' scope so the project-fallback downgrade path is exercised
        without triggering the medium→large(decompose) upgrade.
        """
        d = self._direction([])  # no direction history
        # Project-wide large scope: 10 created / 1 merged = 10% < 30%
        # Project-wide medium scope: 5 created / 4 merged = 80% — acceptable target
        project = self._project([
            [{"large": {"created": 5, "merged": 0}}, {"medium": {"created": 3, "merged": 3}}],
            [{"large": {"created": 5, "merged": 1}}, {"medium": {"created": 2, "merged": 1}}],
        ])
        result_scope, calibrated_from = _calibrate_scope("large", d, project)
        assert result_scope == "medium"
        assert calibrated_from == "large"

    def test_project_fallback_softer_threshold_30_percent(self):
        """Project-wide rate at exactly 30% → not downgraded (threshold >= 0.3)."""
        d = self._direction([])
        # Project-wide medium: 10 created / 3 merged = 30% (not below threshold)
        project = self._project([
            [{"medium": {"created": 10, "merged": 3}}],
        ])
        result_scope, calibrated_from = _calibrate_scope("medium", d, project)
        assert result_scope == "medium"
        assert calibrated_from is None

    def test_project_fallback_skipped_when_direction_has_enough_data(self):
        """Direction has >= 3 data points → use direction-specific 40% threshold,
        ignore project-wide data."""
        # Direction medium: 5 created / 2 merged = 40% — acceptable at direction threshold
        d = self._direction([
            {"medium": {"created": 5, "merged": 2}},
        ])
        # Project-wide medium would be 0% (very bad), but should be ignored
        project = self._project([
            [{"medium": {"created": 100, "merged": 0}}],
        ])
        result_scope, calibrated_from = _calibrate_scope("medium", d, project)
        assert result_scope == "medium"
        assert calibrated_from is None

    def test_project_fallback_returns_unchanged_when_project_data_also_thin(self, monkeypatch):
        """Both direction and project-wide data insufficient + no baselines → no calibration."""
        from app.shared_state import runtime_config as _rc
        monkeypatch.setitem(_rc, "pipeline_scope_baseline_rates", {})
        d = self._direction([])
        project = self._project([
            [{"medium": {"created": 1, "merged": 0}}],
        ])  # project-wide total = 1 < 3
        result_scope, calibrated_from = _calibrate_scope("medium", d, project)
        assert result_scope == "medium"
        assert calibrated_from is None

    def test_project_fallback_omitted_returns_unchanged(self, monkeypatch):
        """No project passed AND direction insufficient + no baselines → unchanged (back-compat)."""
        from app.shared_state import runtime_config as _rc
        monkeypatch.setitem(_rc, "pipeline_scope_baseline_rates", {})
        d = self._direction([])
        assert _calibrate_scope("medium", d) == ("medium", None)

    def test_cold_start_upgrades_medium_when_pipeline_baseline_below_threshold(self, monkeypatch):
        """No direction/project history + baseline medium < 40% + small healthy → upgrade medium→large."""
        from app.shared_state import runtime_config as _rc
        monkeypatch.setitem(
            _rc, "pipeline_scope_baseline_rates", {"small": 0.75, "medium": 0.04, "large": 0.50},
        )
        d = self._direction([])
        result_scope, calibrated_from = _calibrate_scope("medium", d)
        assert result_scope == "large"
        assert calibrated_from == "medium"

    def test_cold_start_upgrades_medium_when_project_data_thin(self, monkeypatch):
        """Direction empty + project < 3 data points + baseline says medium failing → upgrade."""
        from app.shared_state import runtime_config as _rc
        monkeypatch.setitem(
            _rc, "pipeline_scope_baseline_rates", {"small": 0.75, "medium": 0.04, "large": 0.50},
        )
        d = self._direction([])
        project = self._project([
            [{"medium": {"created": 1, "merged": 0}}],
        ])
        result_scope, calibrated_from = _calibrate_scope("medium", d, project)
        assert result_scope == "large"
        assert calibrated_from == "medium"

    def test_cold_start_no_upgrade_when_baseline_medium_healthy(self, monkeypatch):
        """Cold-start + baseline medium >= 40% → no upgrade (pipeline-wide medium is fine)."""
        from app.shared_state import runtime_config as _rc
        monkeypatch.setitem(
            _rc, "pipeline_scope_baseline_rates", {"small": 0.75, "medium": 0.50, "large": 0.50},
        )
        d = self._direction([])
        result_scope, calibrated_from = _calibrate_scope("medium", d)
        assert result_scope == "medium"
        assert calibrated_from is None

    def test_cold_start_no_upgrade_when_baseline_small_unhealthy(self, monkeypatch):
        """Cold-start + baseline small < 40% → no upgrade (decompose target is unproven)."""
        from app.shared_state import runtime_config as _rc
        monkeypatch.setitem(
            _rc, "pipeline_scope_baseline_rates", {"small": 0.20, "medium": 0.04, "large": 0.50},
        )
        d = self._direction([])
        result_scope, calibrated_from = _calibrate_scope("medium", d)
        assert result_scope == "medium"
        assert calibrated_from is None

    def test_cold_start_no_upgrade_for_non_medium_scope(self, monkeypatch):
        """Cold-start upgrade only fires for medium — small/large pass through unchanged."""
        from app.shared_state import runtime_config as _rc
        monkeypatch.setitem(
            _rc, "pipeline_scope_baseline_rates", {"small": 0.75, "medium": 0.04, "large": 0.50},
        )
        d = self._direction([])
        assert _calibrate_scope("small", d) == ("small", None)
        assert _calibrate_scope("large", d) == ("large", None)

    def test_cold_start_no_upgrade_when_baselines_missing(self, monkeypatch):
        """Cold-start + no baseline configured → returns unchanged (no implicit defaults applied)."""
        from app.shared_state import runtime_config as _rc
        monkeypatch.setitem(_rc, "pipeline_scope_baseline_rates", {"small": 0.75})
        d = self._direction([])
        result_scope, calibrated_from = _calibrate_scope("medium", d)
        assert result_scope == "medium"
        assert calibrated_from is None

    def test_medium_upgrades_to_large_when_small_is_healthy(self):
        """Medium below threshold + small healthy → upgrade to large for plan-agent decomposition.

        calibrated_from is the bare proposed scope ('medium') — the "(decompose)"
        detail is logged, not persisted, so downstream consumers that treat
        calibrated_from as a scope key (direction_outcomes bookkeeping,
        prompt.ts calibration note, admin analytics) receive a valid scope.
        """
        d = self._direction([
            {"medium": {"created": 10, "merged": 2}},  # 20% < 40%
            {"small": {"created": 5, "merged": 4}},    # 80% >= 40% (healthy)
        ])
        result_scope, calibrated_from = _calibrate_scope("medium", d)
        assert result_scope == "large"
        assert calibrated_from == "medium"

    def test_medium_no_upgrade_when_small_also_failing(self):
        """Medium below threshold + small below threshold → systemic issue, no upgrade.

        Falls through to downgrade loop; since small also fails, returns unchanged.
        """
        d = self._direction([
            {"medium": {"created": 10, "merged": 2}},  # 20%
            {"small": {"created": 5, "merged": 1}},    # 20% < 40%
        ])
        result_scope, calibrated_from = _calibrate_scope("medium", d)
        assert result_scope == "medium"
        assert calibrated_from is None

    def test_medium_no_upgrade_when_small_lacks_data(self):
        """Medium below threshold + small has insufficient data → no upgrade.

        Without proven small-scope success, decomposition is not justified. Falls
        through to existing downgrade behavior (which treats no-data as acceptable).
        """
        d = self._direction([
            {"medium": {"created": 10, "merged": 2}},  # 20%
            # no "small" data
        ])
        result_scope, calibrated_from = _calibrate_scope("medium", d)
        assert result_scope == "small"
        assert calibrated_from == "medium"

    def test_large_does_not_upgrade(self):
        """Upgrade path only fires for medium — large below threshold still downgrades normally."""
        d = self._direction([
            {"large": {"created": 10, "merged": 2}},   # 20%
            {"medium": {"created": 5, "merged": 1}},   # 20% — bad, skipped
            {"small": {"created": 5, "merged": 4}},    # 80% — healthy
        ])
        result_scope, calibrated_from = _calibrate_scope("large", d)
        # Despite small being healthy (the trigger condition for upgrade), large
        # only ever downgrades — calibrated_from is the bare scope, not a "→...(decompose)" marker.
        assert result_scope == "small"
        assert calibrated_from == "large"


# ─────────────────────────────────────────────────────────────────────────────
# Pipeline awareness: stalled-cycle detection and research failure recording
# ─────────────────────────────────────────────────────────────────────────────

class TestDirectionHealthRule4:
    """Rule 4 pauses a direction after 3 consecutive failing cycles."""

    def _make_entry(self, *, research_task_failed: bool = False,
                    tasks_failed: int = 0, prs_merged: int = 0,
                    tasks_created: int = 0) -> dict:
        return {
            "timestamp": "2026-04-15T00:00:00",
            "tasks_created": tasks_created,
            "tasks_failed": tasks_failed,
            "prs_merged": prs_merged,
            "research_task_failed": research_task_failed,
        }

    def test_rule4_three_research_task_failures_trips(self):
        from app.strategic_completion import _check_direction_health
        d = {"research_history": [self._make_entry(research_task_failed=True) for _ in range(3)]}
        reason = _check_direction_health(d)
        assert reason is not None
        assert "Recent stall" in reason

    def test_rule4_three_failed_children_trips(self):
        """3 cycles each with ≥1 failed child and zero merges pauses the direction.

        Keep the failure ratio under 70% so Rule 3 does not fire first — we are
        specifically testing that Rule 4 catches the 'any failures, no merges'
        pattern that Rule 3 would miss.
        """
        from app.strategic_completion import _check_direction_health
        d = {"research_history": [
            # 5 created, 1 failed = 20% — below Rule 3's 70% — but 0 merged triggers Rule 4
            self._make_entry(tasks_created=5, tasks_failed=1, prs_merged=0) for _ in range(3)
        ]}
        reason = _check_direction_health(d)
        assert reason is not None
        assert "Recent stall" in reason

    def test_rule4_mixed_with_merge_does_not_trip(self):
        """If any of the last 3 cycles has a merge, Rule 4 does not trip."""
        from app.strategic_completion import _check_direction_health
        d = {"research_history": [
            self._make_entry(tasks_created=5, tasks_failed=1, prs_merged=0),
            self._make_entry(tasks_created=5, tasks_failed=1, prs_merged=1),  # has a merge
            self._make_entry(tasks_created=5, tasks_failed=1, prs_merged=0),
        ]}
        reason = _check_direction_health(d)
        # Rules 1-3 should not trip either on this short history
        assert reason is None

    def test_rule4_fewer_than_three_entries_does_not_trip(self):
        from app.strategic_completion import _check_direction_health
        d = {"research_history": [self._make_entry(research_task_failed=True) for _ in range(2)]}
        assert _check_direction_health(d) is None

    def test_rule4_mixed_failure_modes_trip(self):
        """Mixed research-task failures and failed-children count as failing cycles.

        Middle cycle keeps failure ratio under Rule 3's 70% threshold so Rule 4
        is the check that actually catches the pattern.
        """
        from app.strategic_completion import _check_direction_health
        d = {"research_history": [
            self._make_entry(research_task_failed=True),
            self._make_entry(tasks_created=5, tasks_failed=1, prs_merged=0),
            self._make_entry(research_task_failed=True),
        ]}
        reason = _check_direction_health(d)
        assert reason is not None
        assert "Recent stall" in reason


@pytest.mark.asyncio
async def test_record_research_task_failure_appends_history(db_session):
    """FAILED research task records a research_task_failed entry on its direction."""
    from app.research_completion import _record_research_task_failure

    direction = {
        "id": "d-failrec1",
        "text": "Failure recording test",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "research_history": [],
    }
    project = Project(
        name="Failure Recording Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Failed research",
        description="research failed at provisioning",
        payloads=[{"type": "research", "prompt": "find things"}],
        context={"direction_id": "d-failrec1"},
        status=TaskStatus.FAILED,
        latest_status_message="provisioning failed: zone exhausted",
        created_by="ORCHESTRATOR",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    await _record_research_task_failure(task, db_session)
    await db_session.commit()
    await db_session.refresh(project)

    d = next(x for x in project.directions if x["id"] == "d-failrec1")
    history = d.get("research_history", [])
    assert len(history) == 1
    entry = history[0]
    assert entry.get("research_task_failed") is True
    assert entry.get("tasks_created") == 0
    assert entry.get("task_id") == str(task.id)
    assert "failure_category" in entry
    assert d.get("last_researched_at") is not None


@pytest.mark.asyncio
async def test_record_research_task_failure_no_direction_is_noop(db_session):
    """Task without direction_id does not crash _record_research_task_failure."""
    from app.research_completion import _record_research_task_failure

    project = Project(
        name="No Direction Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Failed research",
        description="no direction attached",
        payloads=[{"type": "research", "prompt": "find things"}],
        context={},
        status=TaskStatus.FAILED,
        created_by="ORCHESTRATOR",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # Should be a silent no-op, no exception
    await _record_research_task_failure(task, db_session)


# ─────────────────────────────────────────────────────────────────────────────
# Standalone tests for predict_spawn_success (no DB required)
# ─────────────────────────────────────────────────────────────────────────────

class TestPredictSpawnSuccessPipelineSignal:
    """Unit tests for the pipeline-wide scope merge rate fallback signal."""

    def test_no_data_no_pipeline_rate_returns_neutral(self):
        """With no signals at all, returns 0.5 — preserves prior fallback behavior."""
        from app.shared_state import runtime_config
        runtime_config["pipeline_scope_merge_rates"] = {}
        try:
            assert predict_spawn_success("medium", {}) == 0.5
        finally:
            runtime_config["pipeline_scope_merge_rates"] = {}

    def test_only_pipeline_rate_dominates_when_others_absent(self):
        """When only signal #4 is available (sparse direction), it sets P(success)
        to the global rate so a 4% medium rate triggers the low_confidence gate
        instead of collapsing to a neutral 0.5 that always passes."""
        from app.shared_state import runtime_config
        runtime_config["pipeline_scope_merge_rates"] = {"medium": 0.04}
        try:
            p = predict_spawn_success("medium", {})
            assert p == pytest.approx(0.04)
        finally:
            runtime_config["pipeline_scope_merge_rates"] = {}

    def test_pipeline_rate_does_not_apply_to_other_scopes(self):
        """A medium pipeline rate must not influence small-scope predictions."""
        from app.shared_state import runtime_config
        runtime_config["pipeline_scope_merge_rates"] = {"medium": 0.04}
        try:
            assert predict_spawn_success("small", {}) == 0.5
        finally:
            runtime_config["pipeline_scope_merge_rates"] = {}

    def test_pipeline_rate_weighted_below_direction_signal(self):
        """Direction-level signal (weight 2.0) must dominate the pipeline-wide
        fallback (weight 0.5) when both are present."""
        from app.shared_state import runtime_config
        runtime_config["pipeline_scope_merge_rates"] = {"medium": 0.0}
        try:
            d = {
                "research_history": [
                    {"scope_breakdown": {"medium": {"created": 5, "merged": 5}}},
                ],
            }
            # Signal 1: 1.0 (weight 2.0); Signal 4: 0.0 (weight 0.5).
            # Weighted: (1.0*2.0 + 0.0*0.5) / 2.5 = 0.8
            p = predict_spawn_success("medium", d)
            assert p == pytest.approx(0.8)
        finally:
            runtime_config["pipeline_scope_merge_rates"] = {}

    def test_pipeline_rate_clamped_to_unit_interval(self):
        """Out-of-range pipeline rates are clamped to [0.0, 1.0]."""
        from app.shared_state import runtime_config
        runtime_config["pipeline_scope_merge_rates"] = {"medium": 1.5}
        try:
            assert predict_spawn_success("medium", {}) == pytest.approx(1.0)
            runtime_config["pipeline_scope_merge_rates"] = {"medium": -0.2}
            assert predict_spawn_success("medium", {}) == pytest.approx(0.0)
        finally:
            runtime_config["pipeline_scope_merge_rates"] = {}

    def test_pipeline_rate_ignores_non_numeric(self):
        """Non-numeric values (e.g. None, strings) are ignored — falls back to neutral."""
        from app.shared_state import runtime_config
        runtime_config["pipeline_scope_merge_rates"] = {"medium": None}
        try:
            assert predict_spawn_success("medium", {}) == 0.5
            runtime_config["pipeline_scope_merge_rates"] = {"medium": "0.04"}
            assert predict_spawn_success("medium", {}) == 0.5
        finally:
            runtime_config["pipeline_scope_merge_rates"] = {}
