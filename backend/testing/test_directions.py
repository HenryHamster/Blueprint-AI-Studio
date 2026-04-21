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
from app.direction_lifecycle import _process_direction_lifecycle
from app.research_completion import _handle_research_completion, _calibrate_scope, predict_spawn_success, _poor_decomp_history
from app.direction_metrics import _compute_acceptance_rate, _compute_direction_quality_score, _compute_momentum_factor, _compute_productivity_score, _compute_rolling_median_review_iterations
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
async def test_create_direction_with_cost_budget(client: AsyncClient, mocker):
    """POST accepts and stores cost_budget_usd."""
    project_id = await _deploy_project(client, mocker)
    resp = await client.post(
        f"/api/admin/projects/{project_id}/directions",
        json={"text": "budgeted", "priority": "low", "paths": ["src/"], "cost_budget_usd": 12.5},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["direction"]["cost_budget_usd"] == 12.5


@pytest.mark.asyncio
async def test_create_direction_rejects_negative_cost_budget(client: AsyncClient, mocker):
    """POST rejects negative cost_budget_usd via pydantic ge=0 validator."""
    project_id = await _deploy_project(client, mocker)
    resp = await client.post(
        f"/api/admin/projects/{project_id}/directions",
        json={"text": "x", "priority": "low", "paths": ["src/"], "cost_budget_usd": -1},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_update_direction_cost_budget_set_and_clear(client: AsyncClient, mocker):
    """PATCH sets cost_budget_usd and clears it when set to null."""
    project_id = await _deploy_project(client, mocker)
    direction = await _add_direction(client, project_id)

    resp = await client.patch(
        f"/api/admin/projects/{project_id}/directions/{direction['id']}",
        json={"cost_budget_usd": 5},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["direction"]["cost_budget_usd"] == 5.0

    resp = await client.patch(
        f"/api/admin/projects/{project_id}/directions/{direction['id']}",
        json={"cost_budget_usd": None},
    )
    assert resp.status_code == 200
    assert "cost_budget_usd" not in resp.json()["direction"]


@pytest.mark.asyncio
async def test_update_direction_rejects_invalid_cost_budget(client: AsyncClient, mocker):
    """PATCH rejects non-numeric and negative cost_budget_usd."""
    project_id = await _deploy_project(client, mocker)
    direction = await _add_direction(client, project_id)

    resp = await client.patch(
        f"/api/admin/projects/{project_id}/directions/{direction['id']}",
        json={"cost_budget_usd": "lots"},
    )
    assert resp.status_code == 400

    resp = await client.patch(
        f"/api/admin/projects/{project_id}/directions/{direction['id']}",
        json={"cost_budget_usd": -3},
    )
    assert resp.status_code == 400


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
async def test_task_counts_includes_non_research_tasks_with_direction_id(
    client: AsyncClient, db_session, mocker
):
    """Non-research tasks with a direction_id in context ARE counted, and
    appear under their own type bucket in counts_by_type."""
    project_id = await _deploy_project(client, mocker)
    direction = await _add_direction(client, project_id, text="Direction C")
    direction_id = direction["id"]

    db_session.add(
        Task(
            project_id=uuid.UUID(project_id),
            title="feat: something",
            payloads=[{"type": "create", "base_branch": "main"}],
            context={"direction_id": direction_id},
            status=TaskStatus.IN_PROGRESS,
            created_by="ORCHESTRATOR",
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
        )
    )
    await db_session.commit()

    resp = await client.get(f"/api/admin/projects/{project_id}/directions/task-counts")
    assert resp.status_code == 200
    body = resp.json()
    assert body["counts"].get(direction_id) == 1
    assert body["counts_by_type"][direction_id]["create"] == 1


@pytest.mark.asyncio
async def test_task_counts_context_direction_id_preferred_over_payload(
    client: AsyncClient, db_session, mocker
):
    """context.direction_id takes precedence over payload-derived direction ids."""
    project_id = await _deploy_project(client, mocker)
    dir_ctx = await _add_direction(client, project_id, text="Ctx direction")
    dir_payload = await _add_direction(client, project_id, text="Payload direction")

    db_session.add(
        Task(
            project_id=uuid.UUID(project_id),
            title="Research: mixed",
            payloads=[{
                "type": "research",
                "current_direction_id": dir_payload["id"],
                "directions": [dir_payload],
                "base_branch": "main",
            }],
            context={"direction_id": dir_ctx["id"]},
            status=TaskStatus.IN_PROGRESS,
            created_by="ORCHESTRATOR",
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
        )
    )
    await db_session.commit()

    resp = await client.get(f"/api/admin/projects/{project_id}/directions/task-counts")
    assert resp.status_code == 200
    counts = resp.json()["counts"]
    assert counts.get(dir_ctx["id"]) == 1
    assert counts.get(dir_payload["id"], 0) == 0


@pytest.mark.asyncio
async def test_task_counts_payload_fallback_when_context_missing(
    client: AsyncClient, db_session, mocker
):
    """Older tasks without context.direction_id are still counted via payload."""
    project_id = await _deploy_project(client, mocker)
    direction = await _add_direction(client, project_id, text="Fallback direction")
    direction_id = direction["id"]

    db_session.add(
        Task(
            project_id=uuid.UUID(project_id),
            title="Research: legacy",
            payloads=[{
                "type": "research",
                "current_direction_id": direction_id,
                "base_branch": "main",
            }],
            context={},
            status=TaskStatus.IN_PROGRESS,
            created_by="ORCHESTRATOR",
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
        )
    )
    await db_session.commit()

    resp = await client.get(f"/api/admin/projects/{project_id}/directions/task-counts")
    assert resp.status_code == 200
    assert resp.json()["counts"].get(direction_id) == 1


@pytest.mark.asyncio
async def test_task_counts_by_type_breakdown(
    client: AsyncClient, db_session, mocker
):
    """counts_by_type reports per-type counts for mixed task types on a direction."""
    project_id = await _deploy_project(client, mocker)
    direction = await _add_direction(client, project_id, text="Mixed types")
    direction_id = direction["id"]

    for payload_type in ("research", "create", "create", "review"):
        db_session.add(
            Task(
                project_id=uuid.UUID(project_id),
                title=f"{payload_type} task",
                payloads=[{"type": payload_type, "base_branch": "main"}],
                context={"direction_id": direction_id},
                status=TaskStatus.IN_PROGRESS,
                created_by="ORCHESTRATOR",
                vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
            )
        )
    await db_session.commit()

    resp = await client.get(f"/api/admin/projects/{project_id}/directions/task-counts")
    assert resp.status_code == 200
    body = resp.json()
    assert body["counts"][direction_id] == 4
    by_type = body["counts_by_type"][direction_id]
    assert by_type == {"research": 1, "create": 2, "review": 1}


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
    """GET /api/admin/settings returns max_parallel_research_per_project with default 6."""
    resp = await client.get("/api/admin/settings")
    assert resp.status_code == 200
    data = resp.json()
    assert "max_parallel_research_per_project" in data
    assert data["max_parallel_research_per_project"] == 6


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
# Standalone tests for _compute_productivity_score (no DB required)
# ─────────────────────────────────────────────────────────────────────────────

class TestComputeMergeRate:
    """Unit tests for _compute_productivity_score — pure function, no DB."""

    def test_never_researched_returns_neutral(self):
        """A direction with no history returns 0.5 (neutral/unknown)."""
        d = {"id": "d1", "priority": "medium"}
        assert _compute_productivity_score(d) == 0.5

    def test_empty_history_returns_neutral(self):
        """Explicit empty lists also return 0.5."""
        d = {"id": "d1", "research_history": [], "history_aggregate": {}}
        assert _compute_productivity_score(d) == 0.5

    def test_history_with_no_merges(self):
        """Cycles present but zero merges → rate = 0.0."""
        d = {
            "research_history": [
                {"prs_merged": 0},
                {"prs_merged": 0},
            ]
        }
        assert _compute_productivity_score(d) == 0.0

    def test_history_with_merges(self):
        """3 cycles, 6 total merges → rate = 2.0."""
        d = {
            "research_history": [
                {"prs_merged": 1},
                {"prs_merged": 3},
                {"prs_merged": 2},
            ]
        }
        assert _compute_productivity_score(d) == pytest.approx(6 / 3)

    def test_aggregate_counted(self):
        """history_aggregate cycles and merges are included in the total."""
        d = {
            "research_history": [{"prs_merged": 1}],
            "history_aggregate": {"total_cycles": 4, "total_prs_merged": 8},
        }
        # lifetime: 1 live + 4 agg = 5 cycles, 1+8=9 merged → 1.8
        # recent (last 3 live): [{"prs_merged":1}] → rate=1.0
        # weighted: (1.0*3 + 1.8) / 4 = 1.2
        assert _compute_productivity_score(d) == pytest.approx(1.2)

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
            -_compute_productivity_score(d),
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
            -_compute_productivity_score(d),
            d.get("last_researched_at") or "",
        ))
        assert directions[0]["id"] == "unsat"


class TestComputeAcceptanceRate:
    """Unit tests for _compute_acceptance_rate — pure function, no DB."""

    def test_no_data_returns_none(self):
        assert _compute_acceptance_rate({}) is None
        assert _compute_acceptance_rate({"research_history": [], "history_aggregate": {}}) is None

    def test_only_merges_returns_one(self):
        d = {"research_history": [{"prs_merged": 2, "prs_rejected": 0}]}
        assert _compute_acceptance_rate(d) == 1.0

    def test_only_rejections_returns_zero(self):
        d = {"research_history": [{"prs_merged": 0, "prs_rejected": 3}]}
        assert _compute_acceptance_rate(d) == 0.0

    def test_empty_recent_falls_back_to_lifetime(self):
        """No live history but aggregate has data → recent is empty → lifetime rate."""
        d = {
            "research_history": [],
            "history_aggregate": {"total_prs_merged": 3, "total_prs_rejected": 1},
        }
        assert _compute_acceptance_rate(d) == pytest.approx(0.75)

    def test_aggregate_contributes_to_lifetime(self):
        """history_aggregate merged/rejected totals roll into the lifetime computation."""
        d = {
            "research_history": [{"prs_merged": 1, "prs_rejected": 0}],
            "history_aggregate": {"total_prs_merged": 1, "total_prs_rejected": 2},
        }
        # lifetime: merged=2, rejected=2 → 0.5; recent (last 3 live)=1 cycle, recent_rate=1.0
        # weighted: (1.0*3 + 0.5)/4 = 0.875
        assert _compute_acceptance_rate(d) == pytest.approx(0.875)

    def test_recent_dominates_lifetime(self):
        """3:1 weighting: recent (1.0) over lifetime (≈0.23) yields ~0.808."""
        d = {
            "research_history": [
                {"prs_merged": 1, "prs_rejected": 0},
                {"prs_merged": 1, "prs_rejected": 0},
                {"prs_merged": 1, "prs_rejected": 0},
            ],
            "history_aggregate": {"total_prs_merged": 0, "total_prs_rejected": 10},
        }
        lifetime = 3 / 13
        expected = (1.0 * 3 + lifetime) / 4
        assert _compute_acceptance_rate(d) == pytest.approx(expected)

    def test_recent_zero_prs_falls_back_to_lifetime(self):
        """When the recent window has cycles but no PRs, lifetime rate is returned."""
        d = {
            "research_history": [
                {"prs_merged": 0, "prs_rejected": 0},
                {"prs_merged": 0, "prs_rejected": 0},
                {"prs_merged": 0, "prs_rejected": 0},
            ],
            "history_aggregate": {"total_prs_merged": 4, "total_prs_rejected": 1},
        }
        assert _compute_acceptance_rate(d) == pytest.approx(0.8)


class TestComputeDirectionQualityScore:
    """Unit tests for _compute_direction_quality_score — pure function, no DB."""

    def test_no_history_returns_neutral(self):
        assert _compute_direction_quality_score({}) == 0.5
        assert _compute_direction_quality_score({"research_history": []}) == 0.5

    def test_all_actionable_taxonomy_high_score(self):
        """All failures actionable, tasks merging → score near 1.0."""
        d = {
            "research_history": [
                {
                    "tasks_created": 2, "prs_merged": 1,
                    "taxonomy_summary": {"code_defect": 2, "scope_related": 1,
                                         "infra_related": 0, "review_quality": 0},
                }
            ]
        }
        # taxonomy_health=1.0, productivity=1.0 (active and merged), scope=1.0 (33% <= 50%)
        assert _compute_direction_quality_score(d) == pytest.approx(1.0)

    def test_all_structural_failures_low_taxonomy_health(self):
        """All failures structural (infra/review) → taxonomy_health=0, productivity=0, scope=1 → 1/3."""
        d = {
            "research_history": [
                {
                    "tasks_created": 2, "prs_merged": 0,
                    "taxonomy_summary": {"code_defect": 0, "scope_related": 0,
                                         "infra_related": 2, "review_quality": 1},
                }
            ]
        }
        assert _compute_direction_quality_score(d) == pytest.approx(1 / 3)

    def test_scope_dominant_triggers_zero_scope_signal(self):
        """scope_related > 50% of taxonomy → scope_signal = 0."""
        d = {
            "research_history": [
                {
                    "tasks_created": 1, "prs_merged": 1,
                    "taxonomy_summary": {"code_defect": 1, "scope_related": 5,
                                         "infra_related": 0, "review_quality": 0},
                }
            ]
        }
        # taxonomy_health=1.0 (all actionable), productivity=1.0, scope=0 → 2/3
        assert _compute_direction_quality_score(d) == pytest.approx(2 / 3)

    def test_recency_weighting_favors_recent(self):
        """Recent cycle weighted 3x: new healthy cycle dominates stale unhealthy ones."""
        d_recent_good = {
            "research_history": [
                {"tasks_created": 1, "prs_merged": 0,
                 "taxonomy_summary": {"code_defect": 0, "scope_related": 0,
                                      "infra_related": 3, "review_quality": 0}},
                {"tasks_created": 1, "prs_merged": 1,
                 "taxonomy_summary": {"code_defect": 1, "scope_related": 0,
                                      "infra_related": 0, "review_quality": 0}},
            ]
        }
        d_recent_bad = {
            "research_history": [
                {"tasks_created": 1, "prs_merged": 1,
                 "taxonomy_summary": {"code_defect": 1, "scope_related": 0,
                                      "infra_related": 0, "review_quality": 0}},
                {"tasks_created": 1, "prs_merged": 0,
                 "taxonomy_summary": {"code_defect": 0, "scope_related": 0,
                                      "infra_related": 3, "review_quality": 0}},
            ]
        }
        assert _compute_direction_quality_score(d_recent_good) > _compute_direction_quality_score(d_recent_bad)

    def test_direction_sort_respects_quality_tiebreak(self):
        """Two directions with equal productivity: higher-quality sorts first."""
        from app.failure_classification import (
            TAXONOMY_CODE_DEFECT, TAXONOMY_INFRA_RELATED,
        )
        now = "2026-01-01T00:00:00"
        high_q = {
            "id": "high_q", "priority": "medium", "last_researched_at": now,
            "research_history": [
                {"prs_merged": 1, "tasks_created": 1,
                 "taxonomy_summary": {TAXONOMY_CODE_DEFECT: 1}},
            ],
        }
        low_q = {
            "id": "low_q", "priority": "medium", "last_researched_at": now,
            "research_history": [
                {"prs_merged": 1, "tasks_created": 2,
                 "taxonomy_summary": {TAXONOMY_INFRA_RELATED: 3}},
            ],
        }
        # Equivalent productivity (both have prs_merged=1 over 1 cycle)
        assert _compute_productivity_score(high_q) == pytest.approx(_compute_productivity_score(low_q))
        # Quality score should differentiate
        assert _compute_direction_quality_score(high_q) > _compute_direction_quality_score(low_q)
        directions = [low_q, high_q]
        directions.sort(key=lambda d: (
            -_compute_productivity_score(d),
            -_compute_direction_quality_score(d),
        ))
        assert directions[0]["id"] == "high_q"


class TestRollingMedianReviewIterations:
    """Unit tests for _compute_rolling_median_review_iterations — pure function."""

    def test_no_history_returns_none(self):
        assert _compute_rolling_median_review_iterations({}) is None
        assert _compute_rolling_median_review_iterations({"research_history": []}) is None

    def test_no_recorded_values_returns_none(self):
        d = {"research_history": [{"cycle_id": "c1"}, {"cycle_id": "c2"}]}
        assert _compute_rolling_median_review_iterations(d) is None

    def test_single_value(self):
        d = {"research_history": [{"median_review_iterations": 4}]}
        assert _compute_rolling_median_review_iterations(d) == 4.0

    def test_odd_count_returns_middle(self):
        d = {"research_history": [
            {"median_review_iterations": 2},
            {"median_review_iterations": 8},
            {"median_review_iterations": 5},
        ]}
        assert _compute_rolling_median_review_iterations(d) == 5.0

    def test_even_count_averages_middle(self):
        # Only last 3 considered; with just 2 values present in window, average them.
        d = {"research_history": [
            {"cycle_id": "c1"},  # no iteration recorded
            {"median_review_iterations": 3},
            {"median_review_iterations": 7},
        ]}
        assert _compute_rolling_median_review_iterations(d) == 5.0

    def test_only_last_three_cycles_considered(self):
        d = {"research_history": [
            {"median_review_iterations": 100},  # older, ignored
            {"median_review_iterations": 1},
            {"median_review_iterations": 2},
            {"median_review_iterations": 3},
        ]}
        assert _compute_rolling_median_review_iterations(d) == 2.0


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
async def test_reactivation_clears_manual_intervention_state(client: AsyncClient, db_session, mocker):
    """Operator reactivating a flagged direction clears escalation/intervention state."""
    from sqlalchemy.orm.attributes import flag_modified

    project_id = await _deploy_project(client, mocker)
    direction = await _add_direction(client, project_id, text="Flagged direction")
    direction_id = direction["id"]

    # Simulate an escalated / flagged state (as set by remediation monitoring).
    project = (await db_session.execute(
        select(Project).where(Project.id == uuid.UUID(project_id))
    )).scalar_one()
    for d in project.directions:
        if d["id"] == direction_id:
            d["status"] = "paused"
            d["auto_paused"] = True
            d["paused_reason"] = "Remediation attempts exhausted"
            d["paused_at"] = datetime.utcnow().isoformat()
            d["needs_manual_intervention"] = True
            d["escalation_notification_pending"] = True
            d["escalation_notification_retries"] = 2
            d["remediation_attempts"] = 3
    flag_modified(project, "directions")
    await db_session.commit()

    resp = await client.patch(
        f"/api/admin/projects/{project_id}/directions/{direction_id}",
        json={"status": "active"},
    )
    assert resp.status_code == 200
    d = resp.json()["direction"]
    assert d["status"] == "active"
    assert "auto_paused" not in d
    assert "paused_reason" not in d
    assert "paused_at" not in d
    assert "needs_manual_intervention" not in d
    assert "escalation_notification_pending" not in d
    assert "escalation_notification_retries" not in d
    assert d["remediation_attempts"] == 0


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


@pytest.mark.asyncio
async def test_auto_recovery_starts_remediation_monitoring(db_session):
    """Auto-recovery of a direction with a merged remediation starts an effectiveness window."""
    direction = _make_paused_direction("d-remed001", hours_ago=50, healthy_after_cooldown=True)
    direction["last_remediation_result"] = "merged"
    direction["remediation_attempts"] = 1
    direction["error_fingerprint"] = "abc123def456"
    project = Project(
        name="Remediation Monitor Test",
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
    d = next(x for x in project.directions if x["id"] == "d-remed001")
    assert d["status"] == "active"
    assert d.get("remediation_monitor_started_at"), "monitoring window should be set on recovery"
    # Tracking fields from the prior remediation must be preserved for fingerprint matching
    assert d.get("last_remediation_result") == "merged"
    assert d.get("error_fingerprint") == "abc123def456"


@pytest.mark.asyncio
async def test_re_pause_during_monitoring_flags_manual_intervention(db_session):
    """Re-pause while remediation monitoring is active and attempts ≥ 3 triggers escalation flag."""
    direction = {
        "id": "d-escal001",
        "text": "Repeatedly failing direction",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "research_history": [{"tasks_created": 0}] * 5,  # unhealthy → will re-pause
        "possibly_saturated": True,
        "remediation_monitor_started_at": (datetime.utcnow() - timedelta(hours=1)).isoformat(),
        "remediation_attempts": 3,
        "last_remediation_result": "merged",
    }
    project = Project(
        name="Escalation Test",
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
    d = next(x for x in project.directions if x["id"] == "d-escal001")
    assert d["status"] == "paused"
    assert d.get("needs_manual_intervention") is True, "should flag for operator when attempts ≥ 3"


@pytest.mark.asyncio
async def test_re_pause_without_monitoring_does_not_flag_intervention(db_session):
    """Re-pause without active remediation monitoring does not set needs_manual_intervention."""
    direction = {
        "id": "d-nomon001",
        "text": "Unhealthy direction with no remediation history",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "research_history": [{"tasks_created": 0}] * 5,
        "possibly_saturated": True,
    }
    project = Project(
        name="No Escalation Test",
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
    d = next(x for x in project.directions if x["id"] == "d-nomon001")
    assert d["status"] == "paused"
    assert "needs_manual_intervention" not in d


@pytest.mark.asyncio
async def test_stuck_manual_intervention_cleared_after_ttl(db_session):
    """needs_manual_intervention auto-clears via TTL when no remediation monitor ever started."""
    flagged_at = (datetime.utcnow() - timedelta(hours=80)).isoformat()
    direction = {
        "id": "d-stuck001",
        "text": "Stuck flagged direction",
        "status": "paused",
        "auto_paused": True,
        "paused_reason": "Saturated",
        "paused_at": flagged_at,
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "research_history": [],
        "needs_manual_intervention": True,
        "needs_manual_intervention_at": flagged_at,
    }
    project = Project(
        name="Stuck Intervention Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _process_direction_lifecycle(
        project, db_session, cooldown_minutes=60.0,
        busy_direction_ids=set(), now=datetime.utcnow(),
    )

    await db_session.refresh(project)
    d = next(x for x in project.directions if x["id"] == "d-stuck001")
    assert "needs_manual_intervention" not in d
    assert "needs_manual_intervention_at" not in d


@pytest.mark.asyncio
async def test_stuck_manual_intervention_preserved_when_monitor_active(db_session):
    """TTL clear does not fire when a remediation_monitor_started_at is present."""
    flagged_at = (datetime.utcnow() - timedelta(hours=80)).isoformat()
    direction = {
        "id": "d-stuck002",
        "text": "Flagged with monitor",
        "status": "paused",
        "auto_paused": True,
        "paused_reason": "Saturated",
        "paused_at": flagged_at,
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "research_history": [],
        "needs_manual_intervention": True,
        "needs_manual_intervention_at": flagged_at,
        "remediation_monitor_started_at": (datetime.utcnow() - timedelta(hours=1)).isoformat(),
    }
    project = Project(
        name="Stuck Intervention Monitor Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _process_direction_lifecycle(
        project, db_session, cooldown_minutes=60.0,
        busy_direction_ids=set(), now=datetime.utcnow(),
    )

    await db_session.refresh(project)
    d = next(x for x in project.directions if x["id"] == "d-stuck002")
    assert d.get("needs_manual_intervention") is True


# ─────────────────────────────────────────────────────────────────────────────
# Diagnosis research on auto-pause
# ─────────────────────────────────────────────────────────────────────────────


def _make_unhealthy_active_direction(did: str, auto_pause_count: int = 0) -> dict:
    """Build an ACTIVE direction whose history trips Rule 2 (zero merge rate).

    5 cycles, all finished, all with 0 prs_merged → _check_direction_health
    returns a pause reason, forcing an auto-pause transition.
    """
    history = [
        {"tasks_created": 1, "tasks_completed": 0, "tasks_failed": 1, "prs_merged": 0}
        for _ in range(5)
    ]
    return {
        "id": did,
        "text": "Direction failing repeatedly",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "auto_pause_count": auto_pause_count,
        "research_history": history,
    }


@pytest.mark.asyncio
async def test_auto_pause_spawns_diagnosis_research(db_session):
    """Auto-pause transition spawns exactly one research task in diagnosis mode."""
    direction = _make_unhealthy_active_direction("d-diag001")
    project = Project(
        name="Diagnosis Spawn Test",
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
    d = next(x for x in project.directions if x["id"] == "d-diag001")
    assert d["status"] == "paused"
    assert d.get("auto_paused") is True
    assert d.get("diagnosis_spawned_at")

    result = await db_session.execute(
        select(Task).where(
            Task.project_id == project.id,
            Task.context["diagnosis_for_direction"].as_string() == "d-diag001",
        )
    )
    diagnosis_tasks = result.scalars().all()
    assert len(diagnosis_tasks) == 1
    dtask = diagnosis_tasks[0]
    payload = dtask.first_payload
    assert payload["type"] == "research"
    assert payload["research_mode"] == "diagnosis"
    assert payload["current_direction_id"] == "d-diag001"
    assert payload["pause_reason"]
    assert dtask.context["direction_id"] == "d-diag001"


@pytest.mark.asyncio
async def test_diagnosis_not_respawned_when_active(db_session):
    """An existing active diagnosis task for the direction suppresses a duplicate spawn."""
    from app.autonomous_research import _spawn_direction_diagnosis

    did = "d-diag002"
    direction = _make_unhealthy_active_direction(did)
    project = Project(
        name="Diagnosis Dedup Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    existing = Task(
        project_id=project.id,
        title="Diagnosis: existing",
        payloads=[{
            "type": "research",
            "research_mode": "diagnosis",
            "current_direction_id": did,
            "base_branch": "main",
        }],
        context={"diagnosis_for_direction": did, "direction_id": did},
        status=TaskStatus.IN_PROGRESS,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(existing)
    await db_session.commit()

    spawned = await _spawn_direction_diagnosis(
        project, direction, "Unproductive: 0 PRs merged", db_session
    )
    assert spawned is False, "spawner must skip when an active diagnosis exists"

    result = await db_session.execute(
        select(Task).where(
            Task.project_id == project.id,
            Task.context["diagnosis_for_direction"].as_string() == did,
        )
    )
    diagnosis_tasks = result.scalars().all()
    assert len(diagnosis_tasks) == 1, "duplicate diagnosis must not be spawned"


@pytest.mark.asyncio
async def test_auto_retire_skips_diagnosis_spawn(db_session):
    """When the code-failure pause count escalates to retirement, diagnosis is NOT spawned."""
    did = "d-diag003"
    # With code_failure_pause_count=2 and a Rule-3 (code-failure) history, the
    # incremented counter hits MAX_AUTO_PAUSE_CYCLES=3 and the direction is
    # auto-retired instead of paused.
    direction = _make_unhealthy_active_direction(did, auto_pause_count=2)
    direction["code_failure_pause_count"] = 2
    # Replace Rule-2 "Unproductive" history with Rule-3 "High code failure rate"
    # so the new pause counts toward retirement.
    direction["research_history"] = [
        {"tasks_created": 4, "tasks_failed": 4, "tasks_failed_infra": 0, "prs_merged": 0}
        for _ in range(3)
    ]
    project = Project(
        name="Diagnosis Retire Skip Test",
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
    d = next(x for x in project.directions if x["id"] == did)
    assert d["status"] == "completed"
    assert d.get("auto_completed") is True
    assert "diagnosis_spawned_at" not in d

    result = await db_session.execute(
        select(Task).where(
            Task.project_id == project.id,
            Task.context["diagnosis_for_direction"].as_string() == did,
        )
    )
    diagnosis_tasks = result.scalars().all()
    assert len(diagnosis_tasks) == 0, "retired directions must not spawn diagnosis"


@pytest.mark.asyncio
async def test_diagnosis_payload_includes_failure_context(db_session):
    """Diagnosis payload carries pause_reason, failed-task signatures, and taxonomy counts."""
    did = "d-diag004"
    direction = _make_unhealthy_active_direction(did)
    project = Project(
        name="Diagnosis Context Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test"},
        design_doc={
            "playbook/test-gotcha": {
                "content": "When tests fail, first diagnose if the bug is in CODE or TEST.",
            },
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Seed a couple of FAILED tasks attributed to this direction so the spawner
    # has real signatures to include.
    for i in range(2):
        failed = Task(
            project_id=project.id,
            title=f"Failed task {i}",
            payloads=[{"type": "create", "base_branch": "main"}],
            context={
                "direction_id": did,
                "failure_category": "CODE_ERROR",
                "failure_taxonomy": "review_quality",
            },
            status=TaskStatus.FAILED,
            created_by="ORCHESTRATOR",
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
            latest_status_message=f"pytest failed in module {i}",
        )
        db_session.add(failed)
    await db_session.commit()

    await _spawn_research_task(project, db_session)

    result = await db_session.execute(
        select(Task).where(
            Task.project_id == project.id,
            Task.context["diagnosis_for_direction"].as_string() == did,
        )
    )
    diagnosis_tasks = result.scalars().all()
    assert len(diagnosis_tasks) == 1
    payload = diagnosis_tasks[0].first_payload
    assert payload["research_mode"] == "diagnosis"
    assert payload["pause_reason"]
    sigs = payload.get("failed_task_signatures") or []
    assert len(sigs) == 2
    # Signatures carry the failure context for the agent.
    assert all(s["failure_category"] == "CODE_ERROR" for s in sigs)
    assert all(s["failure_taxonomy"] == "review_quality" for s in sigs)
    assert payload["failure_taxonomy_counts"].get("review_quality") == 2
    # Playbook entry is forwarded through the design_doc summary.
    assert "playbook/test-gotcha" in payload["design_doc"]


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


@pytest.mark.asyncio
async def test_strategic_api_accumulates_direction_ids(db_session, mocker):
    """create_direction and retire_direction should append IDs to task.context lists."""
    from httpx import AsyncClient, ASGITransport
    from app.main import app
    from app.database import get_session
    from app.routers.agent import verify_agent_token

    mocker.patch("app.main.provision_agent_vm", return_value=None)

    existing = [
        {"id": "d-retire-me", "text": "Old direction", "status": "active", "priority": "medium",
         "notes": [], "research_history": []}
    ]
    project = Project(
        name="P-sapi-ids", user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=existing,
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id, user_id="u1", task_type="strategic_review",
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
            r1 = await ac.post("/api/agent/strategic", json={
                "op": "create_direction", "task_id": task_id,
                "data": {"text": "New direction alpha", "priority": "medium"}})
            assert r1.status_code == 200, r1.text
            new_id_1 = r1.json()["direction_id"]

            r2 = await ac.post("/api/agent/strategic", json={
                "op": "create_direction", "task_id": task_id,
                "data": {"text": "New direction beta", "priority": "low"}})
            assert r2.status_code == 200, r2.text
            new_id_2 = r2.json()["direction_id"]

            r3 = await ac.post("/api/agent/strategic", json={
                "op": "retire_direction", "task_id": task_id,
                "data": {"direction_id": "d-retire-me", "rationale": "obsolete"}})
            assert r3.status_code == 200, r3.text

        await db_session.refresh(task)
        ctx = task.context or {}
        assert ctx.get("_strategic_created_ids") == [new_id_1, new_id_2]
        assert ctx.get("_strategic_retired_ids") == ["d-retire-me"]
        assert ctx.get("_strategic_dirs_created") == 2
        assert ctx.get("_strategic_dirs_retired") == 1
    finally:
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(verify_agent_token, None)


# ─────────────────────────────────────────────────────────────────────────────
# Strategic API — new problem_model / running_update / reflect / pending_confirm ops
# ─────────────────────────────────────────────────────────────────────────────


async def _make_strategic_project_and_task(db_session):
    """Shared setup: one project + one strategic_review task, returns (project, task_id)."""
    from app.main import app
    from app.database import get_session
    from app.routers.agent import verify_agent_token

    project = Project(name="P-strategic-new", user_id="u1")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id, user_id="u1", task_type="strategic_review",
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
    app.dependency_overrides[verify_agent_token] = lambda: {
        "sub": task_id, "task_type": "strategic_review",
    }
    return project, task_id


def _cleanup_strategic_overrides():
    from app.main import app
    from app.database import get_session
    from app.routers.agent import verify_agent_token

    app.dependency_overrides.pop(get_session, None)
    app.dependency_overrides.pop(verify_agent_token, None)


@pytest.mark.asyncio
async def test_strategic_api_set_problem_model_happy_path(db_session):
    """op=set_problem_model validates, bumps revision, persists."""
    from httpx import AsyncClient, ASGITransport
    from app.main import app

    project, task_id = await _make_strategic_project_and_task(db_session)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            resp = await ac.post("/api/agent/strategic", json={
                "op": "set_problem_model", "task_id": task_id,
                "data": {
                    "intent": {
                        "purpose": "Build an autonomous dev studio.",
                        "current_focus": "Stabilize the review loop.",
                    },
                    "gaps": [
                        {"id": "g-mem", "name": "No cross-cycle memory",
                         "size": "large", "priority": "high", "status": "active"},
                    ],
                },
            })
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["op"] == "set_problem_model"
        assert body["revision"] == 1

        await db_session.refresh(project)
        pm = project.problem_model
        assert pm["intent"]["purpose"] == "Build an autonomous dev studio."
        assert len(pm["gaps"]) == 1
        assert pm["gaps"][0]["id"] == "g-mem"
        assert pm["revision"] == 1
    finally:
        _cleanup_strategic_overrides()


@pytest.mark.asyncio
async def test_strategic_api_set_problem_model_rejects_malformed(db_session):
    """Invalid schema fields return 400, don't mutate state."""
    from httpx import AsyncClient, ASGITransport
    from app.main import app

    project, task_id = await _make_strategic_project_and_task(db_session)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            # size="enormous" is not a valid GapSize literal
            resp = await ac.post("/api/agent/strategic", json={
                "op": "set_problem_model", "task_id": task_id,
                "data": {"gaps": [{"id": "g-bad", "name": "x", "size": "enormous"}]},
            })
        assert resp.status_code == 400
        body = resp.json()
        msg = body.get("detail") or body.get("error", {}).get("message", "")
        assert "Invalid problem_model" in msg

        await db_session.refresh(project)
        # Model unchanged (still empty dict)
        assert project.problem_model in (None, {}, {"revision": 0})
    finally:
        _cleanup_strategic_overrides()


@pytest.mark.asyncio
async def test_strategic_api_append_running_update(db_session):
    """op=append_running_update validates entry and appends to project.running_update."""
    from httpx import AsyncClient, ASGITransport
    from app.main import app

    project, task_id = await _make_strategic_project_and_task(db_session)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            resp = await ac.post("/api/agent/strategic", json={
                "op": "append_running_update", "task_id": task_id,
                "data": {
                    "whats_changed": "Opened a gap for cross-cycle memory (your ask).",
                    "whats_working_on": "- Strategic memory — direction in review",
                    "next_up": "Merge the schema migration.",
                },
            })
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["op"] == "append_running_update"
        assert body["log_size"] == 1

        await db_session.refresh(project)
        assert len(project.running_update) == 1
        entry = project.running_update[0]
        assert entry["whats_changed"].startswith("Opened a gap")
        assert entry["cycle_task_id"] == task_id
        assert entry["ts"]  # server-stamped
    finally:
        _cleanup_strategic_overrides()


@pytest.mark.asyncio
async def test_strategic_api_reflect(db_session):
    """op=reflect appends to project.self_reflection_log."""
    from httpx import AsyncClient, ASGITransport
    from app.main import app

    project, task_id = await _make_strategic_project_and_task(db_session)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            resp = await ac.post("/api/agent/strategic", json={
                "op": "reflect", "task_id": task_id,
                "data": {
                    "what_was_hard": "Couldn't tell two near-duplicate directions apart.",
                    "missing_context": "Direction outcome doesn't include user rejection reason.",
                    "proposed_self_improvement": "Add rejection_reason to direction outcomes.",
                },
            })
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["op"] == "reflect"
        assert body["log_size"] == 1

        await db_session.refresh(project)
        assert len(project.self_reflection_log) == 1
        entry = project.self_reflection_log[0]
        assert entry["what_was_hard"].startswith("Couldn't tell")
        assert entry["cycle_task_id"] == task_id
    finally:
        _cleanup_strategic_overrides()


@pytest.mark.asyncio
async def test_strategic_api_pending_confirm(db_session):
    """op=pending_confirm adds a pending_user_confirmation item to problem_model."""
    from httpx import AsyncClient, ASGITransport
    from app.main import app

    project, task_id = await _make_strategic_project_and_task(db_session)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            resp = await ac.post("/api/agent/strategic", json={
                "op": "pending_confirm", "task_id": task_id,
                "data": {
                    "type": "credentials_needed",
                    "proposal": "Need OpenAI key with higher rate limit.",
                    "rationale": "Strategic cycles hitting 429s consistently.",
                },
            })
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["op"] == "pending_confirm"
        assert body["id"].startswith("p-")

        await db_session.refresh(project)
        pm = project.problem_model
        pending = pm.get("pending_user_confirmation", [])
        assert len(pending) == 1
        item = pending[0]
        assert item["type"] == "credentials_needed"
        assert item["proposal"].startswith("Need OpenAI")
        assert item["id"] == body["id"]
    finally:
        _cleanup_strategic_overrides()


@pytest.mark.asyncio
async def test_strategic_api_pending_confirm_rejects_invalid_type(db_session):
    """Invalid pending_confirmation type returns 400."""
    from httpx import AsyncClient, ASGITransport
    from app.main import app

    _project, task_id = await _make_strategic_project_and_task(db_session)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            resp = await ac.post("/api/agent/strategic", json={
                "op": "pending_confirm", "task_id": task_id,
                "data": {"type": "some_made_up_type", "proposal": "x"},
            })
        assert resp.status_code == 400
        body = resp.json()
        msg = body.get("detail") or body.get("error", {}).get("message", "")
        assert "Invalid pending_confirmation" in msg
    finally:
        _cleanup_strategic_overrides()


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
            {"small": {"created": 1, "merged": 0, "failed": 1}},
            {"small": {"created": 1, "merged": 0, "failed": 1}},
        ])
        # total_data_points = 2 < 3
        assert _calibrate_scope("small", d) == ("small", None)

    def test_acceptable_rate_returns_unchanged(self):
        """Proposed scope at >= 40% merge rate → no downgrade."""
        d = self._direction([
            {"medium": {"created": 5, "merged": 2, "failed": 3}},  # 40%
        ])
        assert _calibrate_scope("medium", d) == ("medium", None)

    def test_exactly_40_percent_returns_unchanged(self):
        """Exactly 40% merge rate → not downgraded (rate >= 0.4 threshold)."""
        d = self._direction([
            {"medium": {"created": 5, "merged": 2, "failed": 3}},  # 40% exactly
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
            {"large": {"created": 10, "merged": 3, "failed": 7}},   # 30% < 40%
            {"medium": {"created": 5, "merged": 3, "failed": 2}},   # 60% >= 40%
        ])
        result_scope, calibrated_from = _calibrate_scope("large", d)
        assert result_scope == "medium"
        assert calibrated_from == "large"

    def test_smaller_scope_also_bad_returns_unchanged(self):
        """Proposed scope below 40% but no smaller scope has acceptable rate → no downgrade."""
        d = self._direction([
            {"medium": {"created": 10, "merged": 2, "failed": 8}},  # 20%
            {"small": {"created": 5, "merged": 1, "failed": 4}},    # 20%
        ])
        result_scope, calibrated_from = _calibrate_scope("medium", d)
        assert result_scope == "medium"
        assert calibrated_from is None

    def test_smaller_scope_with_no_data_is_acceptable(self):
        """Smaller scope with no historical data (rate=None) is treated as acceptable."""
        d = self._direction([
            {"medium": {"created": 10, "merged": 2, "failed": 8}},  # 20% < 40%
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
            {"large": {"created": 10, "merged": 3, "failed": 7}},   # 30% — triggers downgrade search
            {"medium": {"created": 5, "merged": 2, "failed": 3}},   # 40% — should be accepted (>= 0.4)
        ])
        result_scope, calibrated_from = _calibrate_scope("large", d)
        assert result_scope == "medium"
        assert calibrated_from == "large"

    def test_large_scope_downgrades_through_medium_to_small(self):
        """Large scope below threshold downgrades: picks first smaller scope with acceptable rate."""
        d = self._direction([
            {"large": {"created": 10, "merged": 2, "failed": 8}},   # 20%
            {"medium": {"created": 10, "merged": 2, "failed": 8}},  # 20%
            {"small": {"created": 5, "merged": 3, "failed": 2}},    # 60%
        ])
        result_scope, calibrated_from = _calibrate_scope("large", d)
        assert result_scope == "small"
        assert calibrated_from == "large"

    def test_small_scope_cannot_be_downgraded(self):
        """Small is the smallest scope — even with low rate, no downgrade possible."""
        d = self._direction([
            {"small": {"created": 10, "merged": 1, "failed": 9}},  # 10%
        ])
        result_scope, calibrated_from = _calibrate_scope("small", d)
        assert result_scope == "small"
        assert calibrated_from is None

    def test_uses_only_last_3_history_entries(self):
        """Only the last 3 history entries are considered; older entries are ignored."""
        # Older entries show bad rate for small, recent entries show good rate for medium
        older_bad = {"small": {"created": 100, "merged": 0, "failed": 100}}   # very bad
        recent_good = {"medium": {"created": 5, "merged": 4, "failed": 1}}  # 80% good
        d = self._direction([older_bad, older_bad, older_bad, older_bad, recent_good, recent_good, recent_good])
        # Only last 3 (recent_good x3) used → medium rate 80%, no downgrade
        assert _calibrate_scope("medium", d) == ("medium", None)

    def test_high_overflow_rate_escalates_even_with_acceptable_merge_rate(self):
        """High failed_scope_overflow rate escalates to larger scope despite acceptable merge rate."""
        # medium: 10 created, 6 merged (60%), 4 failed incl. 4 overflow → 40% overflow
        # Merge rate alone would not trigger downgrade/upgrade, but overflow rate does.
        d = self._direction([
            {"medium": {
                "created": 10, "merged": 6, "failed": 4,
                "failed_infra": 4, "failed_scope_overflow": 4,
            }},
        ])
        result_scope, calibrated_from = _calibrate_scope("medium", d)
        assert result_scope == "large"
        assert calibrated_from == "medium"

    def test_low_overflow_rate_does_not_escalate(self):
        """Overflow rate below 30% does not trigger escalation."""
        d = self._direction([
            {"medium": {
                "created": 10, "merged": 7, "failed": 3,
                "failed_infra": 1, "failed_scope_overflow": 1,
            }},
        ])
        assert _calibrate_scope("medium", d) == ("medium", None)

    def test_overflow_escalation_requires_3_data_points(self):
        """Fewer than 3 created at scope → overflow escalation does not fire."""
        # Only 2 medium tasks; project fallback also insufficient → cold-start path
        d = self._direction([
            {"medium": {
                "created": 2, "merged": 0, "failed": 2,
                "failed_infra": 2, "failed_scope_overflow": 2,
            },
             "small": {"created": 3, "merged": 2, "failed": 1}},
        ])
        # 2 medium created < 3 → overflow escalation skipped; medium effective
        # denominator also < 3 so merge rate is None → returns unchanged.
        assert _calibrate_scope("medium", d) == ("medium", None)

    def test_analysis_overflow_dominance_escalates_medium(self):
        """50%+ analysis_overflow rate at medium → hard-escalate medium→large."""
        # 4 medium created, 2 analysis_overflow failures (50%), merge rate 50%
        # (acceptable), scope_overflow below 30% → only analysis signal triggers.
        d = self._direction([
            {"medium": {
                "created": 4, "merged": 2, "failed": 2,
                "failed_infra": 2, "failed_scope_overflow": 0,
                "failed_analysis_overflow": 2,
            }},
        ])
        result_scope, calibrated_from = _calibrate_scope("medium", d)
        assert result_scope == "large"
        assert calibrated_from == "medium"

    def test_overflow_escalation_skipped_for_large_scope(self):
        """Large scope has no larger scope to escalate to → no-op."""
        d = self._direction([
            {"large": {
                "created": 10, "merged": 6, "failed": 4,
                "failed_infra": 4, "failed_scope_overflow": 4,
            }},
        ])
        # 60% merge rate, 40% overflow, but no scope larger than "large"
        assert _calibrate_scope("large", d) == ("large", None)

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
            [{"large": {"created": 5, "merged": 0, "failed": 5}},
             {"medium": {"created": 3, "merged": 3, "failed": 0}}],
            [{"large": {"created": 5, "merged": 1, "failed": 4}},
             {"medium": {"created": 2, "merged": 1, "failed": 1}}],
        ])
        result_scope, calibrated_from = _calibrate_scope("large", d, project)
        assert result_scope == "medium"
        assert calibrated_from == "large"

    def test_project_fallback_softer_threshold_30_percent(self):
        """Project-wide rate at exactly 30% → not downgraded (threshold >= 0.3)."""
        d = self._direction([])
        # Project-wide medium: 10 created / 3 merged = 30% (not below threshold)
        project = self._project([
            [{"medium": {"created": 10, "merged": 3, "failed": 7}}],
        ])
        result_scope, calibrated_from = _calibrate_scope("medium", d, project)
        assert result_scope == "medium"
        assert calibrated_from is None

    def test_project_fallback_skipped_when_direction_has_enough_data(self):
        """Direction has >= 3 data points → use direction-specific 40% threshold,
        ignore project-wide data."""
        # Direction medium: 5 created / 2 merged = 40% — acceptable at direction threshold
        d = self._direction([
            {"medium": {"created": 5, "merged": 2, "failed": 3}},
        ])
        # Project-wide medium would be 0% (very bad), but should be ignored
        project = self._project([
            [{"medium": {"created": 100, "merged": 0, "failed": 100}}],
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
            [{"medium": {"created": 1, "merged": 0, "failed": 1}}],
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
            [{"medium": {"created": 1, "merged": 0, "failed": 1}}],
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
            {"medium": {"created": 10, "merged": 2, "failed": 8}},  # 20% < 40%
            {"small": {"created": 5, "merged": 4, "failed": 1}},    # 80% >= 40% (healthy)
        ])
        result_scope, calibrated_from = _calibrate_scope("medium", d)
        assert result_scope == "large"
        assert calibrated_from == "medium"

    def test_medium_no_upgrade_when_small_also_failing(self):
        """Medium below threshold + small below threshold → systemic issue, no upgrade.

        Falls through to downgrade loop; since small also fails, returns unchanged.
        """
        d = self._direction([
            {"medium": {"created": 10, "merged": 2, "failed": 8}},  # 20%
            {"small": {"created": 5, "merged": 1, "failed": 4}},    # 20% < 40%
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
            {"medium": {"created": 10, "merged": 2, "failed": 8}},  # 20%
            # no "small" data
        ])
        result_scope, calibrated_from = _calibrate_scope("medium", d)
        assert result_scope == "small"
        assert calibrated_from == "medium"

    def test_medium_downgrades_to_small_when_decomp_history_poor(self):
        """Medium below threshold + small healthy + poor decomposition history →
        downgrade to small rather than upgrading to large or staying at medium.

        Guards the dead-zone bug where _poor_decomp_history suppressed the
        medium→large upgrade and the function returned early at medium, skipping
        the downgrade loop entirely.
        """
        d = self._direction([
            {"medium": {"created": 10, "merged": 2, "failed": 8}},  # 20% < 40%
            {"small": {"created": 5, "merged": 4, "failed": 1}},    # 80% healthy
        ])
        d["research_history"][-1]["decomposition_quality"] = [
            {"subtask_merge_rate": 0.1, "subtask_count": 5},
        ]
        result_scope, calibrated_from = _calibrate_scope("medium", d)
        assert result_scope == "small"
        assert calibrated_from == "medium"

    def test_large_does_not_upgrade(self):
        """Upgrade path only fires for medium — large below threshold still downgrades normally."""
        d = self._direction([
            {"large": {"created": 10, "merged": 2, "failed": 8}},   # 20%
            {"medium": {"created": 5, "merged": 1, "failed": 4}},   # 20% — bad, skipped
            {"small": {"created": 5, "merged": 4, "failed": 1}},    # 80% — healthy
        ])
        result_scope, calibrated_from = _calibrate_scope("large", d)
        # Despite small being healthy (the trigger condition for upgrade), large
        # only ever downgrades — calibrated_from is the bare scope, not a "→...(decompose)" marker.
        assert result_scope == "small"
        assert calibrated_from == "large"

    def test_in_flight_tasks_excluded_from_denominator(self):
        """16 medium created, only 4 terminal (1 merged + 3 failed) → rate reads 25%, not 6%.

        Guards against in-flight bias: the older formula (created - true_infra)
        would read 1/16 = 6% because still-QUEUED/IN_PROGRESS tasks inflate
        `created`. Terminal-outcome denominator (merged + failed - true_infra)
        restores the honest 1/4 = 25% rate.
        """
        d = self._direction([
            # 16 medium spawned, 12 still in-flight (not in merged/failed counters);
            # 5 small with 4 merged (healthy) to trigger the decompose-upgrade path.
            {
                "medium": {"created": 16, "merged": 1, "failed": 3},
                "small": {"created": 5, "merged": 4, "failed": 1},
            },
        ])
        # With the old formula, medium rate = 1/16 = 6% < 40% (in-flight bias).
        # With the terminal denominator, rate = 1/4 = 25% < 40%. Medium still
        # downgrades here, but the *reason* is the honest terminal rate, not
        # the artificial in-flight skew. Small at 4/5 = 80% triggers the
        # medium→large(decompose) upgrade path.
        result_scope, calibrated_from = _calibrate_scope("medium", d)
        assert result_scope == "large"
        assert calibrated_from == "medium"

    def test_in_flight_bias_does_not_force_downgrade(self):
        """Early-cycle medium task (many in-flight, few terminal) is not downgraded
        prematurely by an artificially low rate."""
        d = self._direction([
            # 10 spawned, only 2 terminal: both merged (100% true rate).
            # Old formula: rate = 2/10 = 20% → would downgrade.
            # New formula: rate = 2/(2+0) = 100% but effective=2 < 3 → rate=None → unchanged.
            {"medium": {"created": 10, "merged": 2, "failed": 0}},
        ])
        # direction_data_points uses created (10 >= 3), so we enter calibration.
        # effective denominator = 2 + 0 - 0 = 2 < 3 → rate=None → unchanged.
        result_scope, calibrated_from = _calibrate_scope("medium", d)
        assert result_scope == "medium"
        assert calibrated_from is None


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
                    {"scope_breakdown": {"medium": {"created": 5, "merged": 5, "failed": 0}}},
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


class TestPredictSpawnSuccessInfraFailureExclusion:
    """Infra failures (provision/rate-limit/timeout) must not reduce spawn confidence."""

    def test_infra_failures_excluded_from_failure_rate(self):
        """All failures are infra → code-attributable failure rate is 0 → signal contributes 1.0."""
        d = {
            "research_history": [
                {"tasks_created": 3, "tasks_failed": 3, "tasks_failed_infra": 3},
            ],
        }
        # Only Signal 3 present: 1.0 - 0/3 = 1.0
        assert predict_spawn_success("medium", d) == pytest.approx(1.0)

    def test_code_failures_still_reduce_confidence(self):
        """Non-infra failures still count against the direction."""
        d = {
            "research_history": [
                {"tasks_created": 4, "tasks_failed": 3, "tasks_failed_infra": 1},
            ],
        }
        # Signal 3: code_failed=2, failure_rate=0.5 → 0.5
        assert predict_spawn_success("medium", d) == pytest.approx(0.5)


class TestPredictSpawnSuccessDecompositionSignal:
    """Unit tests for the large-scope decomposition_quality signal."""

    def test_decomposition_signal_only_applies_to_large_scope(self):
        d = {
            "research_history": [
                {"decomposition_quality": [{"subtask_merge_rate": 0.8}]},
            ],
        }
        # medium scope: decomposition signal omitted → no signals → neutral 0.5
        assert predict_spawn_success("medium", d) == 0.5

    def test_decomposition_signal_averages_recent_plans(self):
        d = {
            "research_history": [
                {"decomposition_quality": [
                    {"subtask_merge_rate": 0.4},
                    {"subtask_merge_rate": 0.8},
                ]},
            ],
        }
        # Only signal 5: average = 0.6
        assert predict_spawn_success("large", d) == pytest.approx(0.6)

    def test_decomposition_signal_omitted_when_no_data(self):
        """No decomposition data → signal omitted, not defaulted to 0.5."""
        d = {"research_history": [{"decomposition_quality": []}]}
        assert predict_spawn_success("large", d) == 0.5

    def test_decomposition_signal_skips_none_rates(self):
        d = {
            "research_history": [
                {"decomposition_quality": [
                    {"subtask_merge_rate": None},
                    {"subtask_merge_rate": 0.5},
                ]},
            ],
        }
        assert predict_spawn_success("large", d) == pytest.approx(0.5)

    def test_decomposition_signal_combines_with_direction_signal(self):
        d = {
            "research_history": [
                {
                    "scope_breakdown": {"large": {"created": 2, "merged": 2, "failed": 0}},
                    "decomposition_quality": [{"subtask_merge_rate": 0.0}],
                },
            ],
        }
        # Signal 1 (weight 2.0): 1.0; Signal 5 (weight 1.5): 0.0
        # (2.0*1.0 + 1.5*0.0) / 3.5 = 0.5714...
        assert predict_spawn_success("large", d) == pytest.approx(2.0 / 3.5)


class TestPredictSpawnSuccessDiagnosisSignal:
    """Unit tests for the diagnosis-state penalty signal (Signal 6)."""

    def test_no_diagnosis_state_omits_signal(self):
        """A direction with zero diagnosis indicators is unaffected — Signal 6
        contributes nothing, so the result equals the no-data neutral 0.5.
        """
        assert predict_spawn_success("medium", {}) == 0.5

    def test_needs_manual_intervention_applies_low_score(self):
        """needs_manual_intervention is the strongest indicator: contributes 0.1
        regardless of other diagnosis fields.
        """
        d = {"needs_manual_intervention": True}
        # Only Signal 6 present at weight 1.0 → 0.1
        assert predict_spawn_success("medium", d) == pytest.approx(0.1)

    def test_needs_manual_intervention_overrides_other_diagnosis_fields(self):
        """When needs_manual_intervention is True, weaker indicators are ignored."""
        d = {
            "needs_manual_intervention": True,
            "diagnosis_in_progress": True,
            "remediation_attempts": 5,
            "last_remediation_result": "failed",
        }
        assert predict_spawn_success("medium", d) == pytest.approx(0.1)

    def test_diagnosis_in_progress_applies_moderate_penalty(self):
        d = {"diagnosis_in_progress": True}
        # Only Signal 6 present at weight 1.0 → 0.3
        assert predict_spawn_success("medium", d) == pytest.approx(0.3)

    def test_failed_remediations_proportional_penalty(self):
        """rem_attempts=2 with last_result=failed → 0.4; rem_attempts=4 → 0.2."""
        d2 = {"remediation_attempts": 2, "last_remediation_result": "failed"}
        assert predict_spawn_success("medium", d2) == pytest.approx(0.4)
        d4 = {"remediation_attempts": 4, "last_remediation_result": "failed"}
        assert predict_spawn_success("medium", d4) == pytest.approx(0.2)
        d10 = {"remediation_attempts": 10, "last_remediation_result": "failed"}
        # Floored at 0.1.
        assert predict_spawn_success("medium", d10) == pytest.approx(0.1)

    def test_failed_remediations_below_threshold_omits_signal(self):
        """rem_attempts=1 — below the 2-attempt threshold — leaves direction
        unaffected even with last_result=failed.
        """
        d = {"remediation_attempts": 1, "last_remediation_result": "failed"}
        assert predict_spawn_success("medium", d) == 0.5

    def test_merged_remediation_omits_signal(self):
        """A successful (merged) remediation is not a penalty signal even with
        many attempts — the direction has already been fixed.
        """
        d = {"remediation_attempts": 5, "last_remediation_result": "merged"}
        assert predict_spawn_success("medium", d) == 0.5

    def test_diagnosis_signal_combines_with_direction_signal(self):
        """Signal 1 (weight 2.0) at 1.0 combined with Signal 6 (weight 1.0) at 0.3
        → (2.0*1.0 + 1.0*0.3) / 3.0 = 0.7666...
        """
        d = {
            "research_history": [
                {"scope_breakdown": {"medium": {"created": 5, "merged": 5, "failed": 0}}},
            ],
            "diagnosis_in_progress": True,
        }
        assert predict_spawn_success("medium", d) == pytest.approx(2.3 / 3.0)


class TestPredictSpawnSuccessAcceptanceSignal:
    """Unit tests for the operator acceptance-rate signal (Signal 7)."""

    def test_no_pr_data_omits_signal(self):
        """A direction with zero PRs merged/rejected contributes nothing."""
        d = {"research_history": [{"decomposition_quality": []}]}
        assert predict_spawn_success("medium", d) == 0.5

    def test_below_threshold_omits_signal(self):
        """Fewer than 3 total PRs — signal omitted to avoid single-PR noise."""
        d = {"research_history": [{"prs_merged": 1, "prs_rejected": 1}]}
        assert predict_spawn_success("medium", d) == 0.5

    def test_low_acceptance_reduces_confidence(self):
        """3+ PRs with majority rejected meaningfully reduces P(success)."""
        d = {
            "research_history": [
                {"prs_merged": 1, "prs_rejected": 4},
            ],
        }
        # Only Signal 7 present at weight 1.5: acceptance = 1/5 = 0.2
        assert predict_spawn_success("medium", d) == pytest.approx(0.2)

    def test_high_acceptance_supports_confidence(self):
        d = {
            "research_history": [
                {"prs_merged": 4, "prs_rejected": 1},
            ],
        }
        assert predict_spawn_success("medium", d) == pytest.approx(0.8)

    def test_aggregate_counts_toward_threshold(self):
        """history_aggregate PRs count toward the 3-PR minimum."""
        d = {
            "research_history": [{"prs_merged": 1, "prs_rejected": 0}],
            "history_aggregate": {"total_prs_merged": 0, "total_prs_rejected": 5},
        }
        # total PRs = 6 >= 3; acceptance via _compute_acceptance_rate combines
        # recent (1/1=1.0) with lifetime (1/6≈0.1667): (1.0*3 + 0.1667)/4
        expected = (1.0 * 3 + 1.0 / 6) / 4
        assert predict_spawn_success("medium", d) == pytest.approx(expected)

    def test_related_files_small_count_no_penalty(self):
        """1-2 related_files emit no file-count signal."""
        assert predict_spawn_success("small", {}, related_files=["backend/app/a.py"]) == 0.5
        assert predict_spawn_success(
            "small", {}, related_files=["backend/app/a.py", "backend/app/b.py"],
        ) == 0.5

    def test_related_files_moderate_penalty(self):
        """3-4 files → moderate 0.6 penalty at weight 1.0."""
        files = [f"backend/app/f{i}.py" for i in range(3)]
        assert predict_spawn_success("small", {}, related_files=files) == pytest.approx(0.6)

    def test_related_files_strong_penalty(self):
        """5+ files → strong 0.3 penalty at weight 1.0."""
        files = [f"backend/app/f{i}.py" for i in range(5)]
        assert predict_spawn_success("small", {}, related_files=files) == pytest.approx(0.3)

    def test_cross_module_span_penalty(self):
        """Cross-module span is intentionally NOT a separate spawn signal — the
        composite complexity score already folds module span into its factor.
        Only the file-count penalty (0.6 for 3 files at small scope) fires.
        """
        files = ["backend/app/a.py", "agent/src/b.ts", "admin-ui/src/c.ts"]
        assert predict_spawn_success("small", {}, related_files=files) == pytest.approx(0.6)

    def test_two_module_span_no_cross_module_penalty(self):
        """2-module findings stay at medium scope (per escalation threshold), so
        the cross-module spawn penalty should not fire below 3 top-level dirs."""
        files = ["backend/app/a.py", "agent/src/b.ts"]
        assert predict_spawn_success("small", {}, related_files=files) == 0.5

    def test_single_top_level_no_cross_module_penalty(self):
        """All files under one top-level dir → no cross-module signal."""
        files = ["backend/app/a.py", "backend/app/b.py"]
        assert predict_spawn_success("small", {}, related_files=files) == 0.5

    def test_related_files_scope_aware_thresholds(self):
        """Medium scope uses shifted thresholds (7+/5+) so 5 files gets
        the moderate penalty (0.6) rather than small's strong 0.3.
        Small scope is unchanged — 5 files still gets 0.3."""
        files = [f"backend/app/f{i}.py" for i in range(5)]
        medium_score = predict_spawn_success("medium", {}, related_files=files)
        small_score = predict_spawn_success("small", {}, related_files=files)
        assert small_score == pytest.approx(0.3)
        assert medium_score == pytest.approx(0.6)
        assert medium_score > small_score
        # Medium with 7 files hits the strong 0.3 penalty.
        files7 = [f"backend/app/f{i}.py" for i in range(7)]
        assert predict_spawn_success("medium", {}, related_files=files7) == pytest.approx(0.3)

    def test_analysis_overflow_dominance_penalizes_medium(self):
        """50%+ of medium failures being analysis_overflow triggers 0.2 penalty."""
        # 2 medium failures, both analysis_overflow — scope merge rate = 0/2 = 0.0
        # with weight 2.0, plus analysis_overflow signal 0.2 at weight 1.5.
        d = {
            "research_history": [
                {
                    "scope_breakdown": {
                        "medium": {
                            "created": 2, "merged": 0, "failed": 2,
                            "failed_infra": 2, "failed_scope_overflow": 2,
                            "failed_analysis_overflow": 2,
                        },
                    },
                },
            ],
        }
        # Signal 1: 0/2 = 0.0 weight 2.0
        # Signal 10: 0.2 weight 1.5
        # Signal 3 omitted (tasks_created=0 in history entry)
        expected = (0.0 * 2.0 + 0.2 * 1.5) / (2.0 + 1.5)
        assert predict_spawn_success("medium", d) == pytest.approx(expected)

    def test_analysis_overflow_below_threshold_omitted(self):
        """<50% analysis_overflow share of failures does not trigger signal 10."""
        d = {
            "research_history": [
                {
                    "scope_breakdown": {
                        "medium": {
                            "created": 5, "merged": 1, "failed": 4,
                            "failed_infra": 4, "failed_scope_overflow": 4,
                            "failed_analysis_overflow": 1,
                        },
                    },
                },
            ],
        }
        # Only signal 1: merge rate = 1/5 = 0.2
        assert predict_spawn_success("medium", d) == pytest.approx(0.2)

    def test_analysis_overflow_sample_too_small_omitted(self):
        """Single failure (sample < 2) does not trigger signal 10 even at 100%."""
        d = {
            "research_history": [
                {
                    "scope_breakdown": {
                        "medium": {
                            "created": 1, "merged": 0, "failed": 1,
                            "failed_infra": 1, "failed_scope_overflow": 1,
                            "failed_analysis_overflow": 1,
                        },
                    },
                },
            ],
        }
        # effective = 1 < 2 → signal 1 omitted; signal 10 also requires failed>=2
        assert predict_spawn_success("medium", d) == 0.5

    def test_analysis_overflow_small_scope_not_affected(self):
        """Signal 10 is medium-only."""
        d = {
            "research_history": [
                {
                    "scope_breakdown": {
                        "small": {
                            "created": 2, "merged": 0, "failed": 2,
                            "failed_infra": 2, "failed_scope_overflow": 2,
                            "failed_analysis_overflow": 2,
                        },
                    },
                },
            ],
        }
        # Only signal 1: 0/2 = 0.0
        assert predict_spawn_success("small", d) == pytest.approx(0.0)

    def test_complexity_score_signal_lowers_medium_prediction(self):
        """A medium finding with a low complexity_score should receive a lower
        predicted P(success) than one with a high score (weight 1.5)."""
        low = predict_spawn_success("medium", {}, complexity_score=0.2)
        high = predict_spawn_success("medium", {}, complexity_score=0.9)
        assert low < high
        # With no other signals, the weighted average equals the complexity
        # score itself (single signal, any weight).
        assert low == pytest.approx(0.2)
        assert high == pytest.approx(0.9)
        # Absent/None preserves the neutral fallback.
        assert predict_spawn_success("medium", {}) == 0.5

    def test_review_iteration_signal_graduated_penalty(self):
        """Rolling median review-iteration count applies a graduated penalty:
        1-2 iters → no effect, 3-4 → 0.5 (moderate), 5+ → 0.2 (strong)."""
        # No effect at 1-2 iterations (normal churn).
        d_low = {"research_history": [{"median_review_iterations": 2}]}
        assert predict_spawn_success("small", d_low) == 0.5
        # Moderate penalty at 3-4 iterations: combine with complexity 0.9
        # (weight 1.5) so the review-iter signal (0.5, weight 1.0) is observable.
        d_mid = {"research_history": [{"median_review_iterations": 3}]}
        # (0.9*1.5 + 0.5*1.0) / 2.5 = 1.85/2.5 = 0.74
        assert predict_spawn_success("small", d_mid, complexity_score=0.9) == pytest.approx(0.74)
        d_mid2 = {"research_history": [{"median_review_iterations": 4}]}
        assert predict_spawn_success("small", d_mid2, complexity_score=0.9) == pytest.approx(0.74)
        # Without the review-iter penalty, the same complexity would yield 0.9.
        assert predict_spawn_success("small", d_low, complexity_score=0.9) == pytest.approx(0.9)
        # Strong penalty at 5+ iterations.
        d_high = {"research_history": [{"median_review_iterations": 6}]}
        assert predict_spawn_success("small", d_high) == pytest.approx(0.2)


# ─────────────────────────────────────────────────────────────────────────────
# _poor_decomp_history helper unit tests (suppression signal)
# ─────────────────────────────────────────────────────────────────────────────

class TestPoorDecompHistory:
    """Unit tests for _poor_decomp_history: suppression signal shared by
    _calibrate_scope and the low-confidence auto-escalation gate.
    """

    def test_no_history_returns_false(self):
        assert _poor_decomp_history({}) is False

    def test_insufficient_subtask_count_returns_false(self):
        d = {"research_history": [{"decomposition_quality": [
            {"subtask_merge_rate": 0.0, "subtask_count": 2},
        ]}]}
        assert _poor_decomp_history(d) is False

    def test_poor_weighted_avg_returns_true(self):
        d = {"research_history": [{"decomposition_quality": [
            {"subtask_merge_rate": 0.1, "subtask_count": 4},
        ]}]}
        assert _poor_decomp_history(d) is True

    def test_healthy_weighted_avg_returns_false(self):
        d = {"research_history": [{"decomposition_quality": [
            {"subtask_merge_rate": 0.5, "subtask_count": 4},
        ]}]}
        assert _poor_decomp_history(d) is False

    def test_weighted_averaging_across_entries(self):
        # 2*0.8 + 3*0.1 = 1.9 over 5 subtasks = 0.38 >= 0.3 → healthy
        d = {"research_history": [{"decomposition_quality": [
            {"subtask_merge_rate": 0.8, "subtask_count": 2},
            {"subtask_merge_rate": 0.1, "subtask_count": 3},
        ]}]}
        assert _poor_decomp_history(d) is False

    def test_skips_non_numeric_rates(self):
        d = {"research_history": [{"decomposition_quality": [
            {"subtask_merge_rate": None, "subtask_count": 10},
            {"subtask_merge_rate": 0.1, "subtask_count": 3},
        ]}]}
        assert _poor_decomp_history(d) is True


# ─────────────────────────────────────────────────────────────────────────────
# Low-confidence medium auto-escalation gate (integration via _handle_research_completion)
# ─────────────────────────────────────────────────────────────────────────────

async def _setup_escalation_project(db_session, direction_id, decomp_quality):
    # Empty research_history keeps _calibrate_scope a no-op (cold-start falls through);
    # low-confidence is simulated via mocker.patch on predict_spawn_success in the test.
    direction = {
        "id": direction_id,
        "text": "Low-confidence medium",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "research_history": (
            [{"decomposition_quality": decomp_quality}] if decomp_quality else []
        ),
    }
    project = Project(name="Escalation test", user_id="test-user-123",
                      autonomous_enabled=True, directions=[direction])
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    task = Task(
        project_id=project.id,
        title="Research: low-conf",
        payloads=[{"type": "research", "directions": [direction], "base_branch": "main"}],
        context={"research_depth": 0},
        status=TaskStatus.IN_PROGRESS,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    return project, task, direction


def _mock_escalation_externals(mocker, direction_id):
    mocker.patch("app.task_completion.get_github_token_for_task",
                 new=AsyncMock(return_value=("fake-token", "owner", "repo")))
    mocker.patch("app.task_completion.get_branch_sha", new=AsyncMock(return_value="abc123sha"))
    mocker.patch("app.task_completion.create_branch", new=AsyncMock(return_value=None))
    mocker.patch("app.broadcast.broadcast_to_project", new=AsyncMock(return_value=None))
    mocker.patch("app.research_completion._validate_plan_dependencies", return_value=True)
    mocker.patch("app.research_completion._log_file_overlaps", return_value=[])
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[{
            "title": "Medium improvement",
            "description": "Some work",
            "prompt": "Do the work",
            "priority": "medium",
            "scope": "medium",
            "risk": "low",
            "type": "improvement",
            "direction_id": direction_id,
        }],
    )


@pytest.mark.asyncio
async def test_low_confidence_medium_auto_escalates_to_large(db_session, mocker):
    """Medium-scope low-confidence finding is escalated to large+plan; the
    low_confidence_spawn flag is preserved in context for tracking."""
    direction_id = "d-lowconf1"
    _, task, _ = await _setup_escalation_project(db_session, direction_id, None)
    _mock_escalation_externals(mocker, direction_id)
    mocker.patch("app.research_completion.predict_spawn_success", return_value=0.15)
    # Disable exploration-bypass randomness so the escalation path is deterministic.
    mocker.patch.dict("app.shared_state.runtime_config", {"medium_exploration_fraction": 0})

    await _handle_research_completion(task, "", db_session)

    result = await db_session.execute(select(Task).where(Task.parent_task_id == task.id))
    children = result.scalars().all()
    assert len(children) == 1
    child = children[0]
    assert child.context["scope"] == "large"
    assert child.context["scope_calibrated_from"] == "medium"
    assert child.context["low_confidence_spawn"] is True
    assert child.first_payload["type"] == "plan"


@pytest.mark.asyncio
async def test_exploration_floor_prevents_zero_collapse(db_session, mocker):
    """Directions with exploration_created>0 but exploration_merged=0 still get
    the exploration floor instead of collapsing to fraction=0 (death spiral).

    Uses exploration_created<3 so the _exploration_proven_unviable branch is
    NOT triggered — the floor should still apply in the early-data regime.
    """
    direction_id = "d-floor-1"
    direction = {
        "id": direction_id,
        "text": "Zero-merge medium exploration",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "research_history": [{
            "scope_breakdown": {"medium": {"exploration_created": 2, "exploration_merged": 0}},
        }],
    }
    project = Project(name="Floor test", user_id="test-user-123",
                      autonomous_enabled=True, directions=[direction])
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    task = Task(
        project_id=project.id,
        title="Research: floor",
        payloads=[{"type": "research", "directions": [direction], "base_branch": "main"}],
        context={"research_depth": 0},
        status=TaskStatus.IN_PROGRESS,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    _mock_escalation_externals(mocker, direction_id)
    mocker.patch("app.research_completion.predict_spawn_success", return_value=0.15)
    # Force the random draw below the floor so the exploration path is taken
    # iff the floor is applied; without the floor, _explore_frac would be 0
    # and random.random() < 0 is always False regardless of the draw.
    mocker.patch("app.research_completion.random.random", return_value=0.01)

    await _handle_research_completion(task, "", db_session)

    result = await db_session.execute(select(Task).where(Task.parent_task_id == task.id))
    children = result.scalars().all()
    assert len(children) == 1
    assert children[0].context["scope"] == "medium"


@pytest.mark.asyncio
async def test_exploration_floor_skipped_when_proven_unviable(db_session, mocker):
    """Once a direction has 3+ exploration attempts with 0 merges, the
    exploration floor is skipped and the low-confidence scope escalates to
    large — continued exploration is waste once the data is informative."""
    direction_id = "d-unviable-1"
    direction = {
        "id": direction_id,
        "text": "Proven-unviable medium exploration",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "research_history": [{
            "scope_breakdown": {"medium": {"exploration_created": 5, "exploration_merged": 0}},
        }],
    }
    project = Project(name="Unviable test", user_id="test-user-123",
                      autonomous_enabled=True, directions=[direction])
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    task = Task(
        project_id=project.id,
        title="Research: unviable",
        payloads=[{"type": "research", "directions": [direction], "base_branch": "main"}],
        context={"research_depth": 0},
        status=TaskStatus.IN_PROGRESS,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    _mock_escalation_externals(mocker, direction_id)
    mocker.patch("app.research_completion.predict_spawn_success", return_value=0.15)
    # Even with the random draw forced below the floor, the floor must be
    # skipped (proven-unviable branch) so escalation to large occurs.
    mocker.patch("app.research_completion.random.random", return_value=0.01)

    await _handle_research_completion(task, "", db_session)

    result = await db_session.execute(select(Task).where(Task.parent_task_id == task.id))
    children = result.scalars().all()
    assert len(children) == 1
    child = children[0]
    assert child.context["scope"] == "large"
    assert child.context["scope_calibrated_from"] == "medium"
    assert child.context["low_confidence_spawn"] is True
    assert child.first_payload["type"] == "plan"


@pytest.mark.asyncio
async def test_exploration_floor_skipped_when_pipeline_unviable(db_session, mocker):
    """When pipeline-wide medium merge rate is at/below the floor, the
    exploration floor is skipped even for a new direction with no history —
    pipeline evidence is sufficient proof of unviability."""
    from app.shared_state import runtime_config
    direction_id = "d-pipeline-unviable-1"
    direction = {
        "id": direction_id,
        "text": "New direction, no exploration history",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "research_history": [],
    }
    project = Project(name="Pipeline unviable test", user_id="test-user-123",
                      autonomous_enabled=True, directions=[direction])
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    task = Task(
        project_id=project.id,
        title="Research: pipeline unviable",
        payloads=[{"type": "research", "directions": [direction], "base_branch": "main"}],
        context={"research_depth": 0},
        status=TaskStatus.IN_PROGRESS,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    _mock_escalation_externals(mocker, direction_id)
    mocker.patch("app.research_completion.predict_spawn_success", return_value=0.15)
    mocker.patch("app.research_completion.random.random", return_value=0.01)
    runtime_config["pipeline_scope_merge_rates"] = {"medium": 0.02}
    try:
        await _handle_research_completion(task, "", db_session)
    finally:
        runtime_config["pipeline_scope_merge_rates"] = {}

    result = await db_session.execute(select(Task).where(Task.parent_task_id == task.id))
    children = result.scalars().all()
    assert len(children) == 1
    child = children[0]
    assert child.context["scope"] == "large"
    assert child.context["scope_calibrated_from"] == "medium:pipeline_floor"


@pytest.mark.asyncio
async def test_complexity_modulates_medium_exploration_fraction(db_session, mocker):
    """A low-complexity medium finding gets a halved exploration fraction
    compared to a high-complexity finding from the same direction. With
    base explore_frac=0.2 and random draw=0.15, the high-complexity finding
    stays medium (0.15 < 0.20) while the low-complexity finding (modulated
    to 0.10) escalates to large (0.15 >= 0.10)."""
    direction_id = "d-complexity-mod-1"
    direction = {
        "id": direction_id,
        "text": "Complexity modulation test",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "research_history": [],
    }
    project = Project(name="Complexity mod test", user_id="test-user-123",
                      autonomous_enabled=True, directions=[direction])
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    task = Task(
        project_id=project.id,
        title="Research: complexity mod",
        payloads=[{"type": "research", "directions": [direction], "base_branch": "main"}],
        context={"research_depth": 0},
        status=TaskStatus.IN_PROGRESS,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    mocker.patch("app.task_completion.get_github_token_for_task",
                 new=AsyncMock(return_value=("fake-token", "owner", "repo")))
    mocker.patch("app.task_completion.get_branch_sha", new=AsyncMock(return_value="abc123sha"))
    mocker.patch("app.task_completion.create_branch", new=AsyncMock(return_value=None))
    mocker.patch("app.broadcast.broadcast_to_project", new=AsyncMock(return_value=None))
    mocker.patch("app.research_completion._validate_plan_dependencies", return_value=True)
    mocker.patch("app.research_completion._log_file_overlaps", return_value=[])
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[
            {
                "title": "Low-complexity finding",
                "description": "simple",
                "prompt": "do it",
                "priority": "medium",
                "scope": "medium",
                "risk": "low",
                "type": "improvement",
                "direction_id": direction_id,
            },
            {
                "title": "High-complexity finding",
                "description": "hairy",
                "prompt": "do it",
                "priority": "medium",
                "scope": "medium",
                "risk": "low",
                "type": "improvement",
                "direction_id": direction_id,
            },
        ],
    )

    def _fake_calibrate(finding, scope, direction, cochange_index, all_directions=None):
        if "Low-complexity" in finding.get("title", ""):
            finding["_complexity_score"] = 0.20
        else:
            finding["_complexity_score"] = 0.50
        finding["_complexity_signals"] = {}
        return "medium", None

    mocker.patch(
        "app.research_completion._calibrate_finding_scope",
        side_effect=_fake_calibrate,
    )
    mocker.patch("app.research_completion.predict_spawn_success", return_value=0.15)
    mocker.patch.dict(
        "app.shared_state.runtime_config",
        {"medium_exploration_fraction": 0.2},
    )
    # Draw 0.15: passes for high-complexity (unmodulated 0.20), fails for
    # low-complexity (modulated 0.10).
    mocker.patch("app.research_completion.random.random", return_value=0.15)

    await _handle_research_completion(task, "", db_session)

    result = await db_session.execute(select(Task).where(Task.parent_task_id == task.id))
    children = result.scalars().all()
    by_title = {c.title: c for c in children}
    high = next(c for t, c in by_title.items() if "High-complexity" in t)
    low = next(c for t, c in by_title.items() if "Low-complexity" in t)
    assert high.context["scope"] == "medium"
    assert low.context["scope"] == "large"
    assert low.context["scope_calibrated_from"] == "medium"


@pytest.mark.asyncio
async def test_hard_skip_below_threshold_drops_finding(db_session, mocker):
    """Medium-scope finding with P(success) below medium_hard_skip_threshold is
    dropped entirely (no child task spawned) and recorded in deferred_findings
    with skip_reason='ultra_low_confidence'."""
    from app.shared_state import runtime_config
    direction_id = "d-hardskip-1"
    project, task, _ = await _setup_escalation_project(db_session, direction_id, None)
    _mock_escalation_externals(mocker, direction_id)
    mocker.patch("app.research_completion.predict_spawn_success", return_value=0.05)
    runtime_config["medium_hard_skip_threshold"] = 0.10
    # Seed medium history so the direction is not data-starved (which would
    # bypass the hard-skip gate).
    project.directions[0]["research_history"] = [
        {"scope_breakdown": {"medium": {"tasks_created": 1, "prs_merged": 0}}}
    ]
    from sqlalchemy.orm.attributes import flag_modified as _fm1
    _fm1(project, "directions")
    await db_session.commit()
    try:
        await _handle_research_completion(task, "", db_session)
    finally:
        runtime_config.pop("medium_hard_skip_threshold", None)

    result = await db_session.execute(select(Task).where(Task.parent_task_id == task.id))
    children = result.scalars().all()
    assert len(children) == 0
    await db_session.refresh(task)
    deferred = (task.context or {}).get("deferred_findings") or []
    assert len(deferred) == 1
    assert deferred[0]["skip_reason"] == "ultra_low_confidence"
    assert deferred[0]["deferred_reason"] == "ultra_low_confidence"
    skip_counts = (task.context or {}).get("skipped_low_confidence_by_scope") or {}
    assert skip_counts.get("medium") == 1


@pytest.mark.asyncio
async def test_hard_skip_above_threshold_runs_normal_path(db_session, mocker):
    """Finding above hard_skip threshold but below confidence_threshold still
    follows the normal exploration/escalation path."""
    from app.shared_state import runtime_config
    direction_id = "d-hardskip-2"
    project, task, _ = await _setup_escalation_project(db_session, direction_id, None)
    _mock_escalation_externals(mocker, direction_id)
    # 0.15 is above hard_skip (0.10) but below confidence_threshold (default 0.25)
    mocker.patch("app.research_completion.predict_spawn_success", return_value=0.15)
    mocker.patch.dict("app.shared_state.runtime_config", {"medium_exploration_fraction": 0})
    runtime_config["medium_hard_skip_threshold"] = 0.10
    project.directions[0]["research_history"] = [
        {"scope_breakdown": {"medium": {"tasks_created": 1, "prs_merged": 0}}}
    ]
    from sqlalchemy.orm.attributes import flag_modified as _fm2
    _fm2(project, "directions")
    await db_session.commit()
    try:
        await _handle_research_completion(task, "", db_session)
    finally:
        runtime_config.pop("medium_hard_skip_threshold", None)

    result = await db_session.execute(select(Task).where(Task.parent_task_id == task.id))
    children = result.scalars().all()
    assert len(children) == 1
    # Escalated to large+plan via the normal low-confidence path.
    assert children[0].context["scope"] == "large"
    assert children[0].context["scope_calibrated_from"] == "medium"
    await db_session.refresh(task)
    assert not (task.context or {}).get("skipped_low_confidence_by_scope")


@pytest.mark.asyncio
async def test_hard_skip_bypassed_for_data_starved_direction(db_session, mocker):
    """A direction with no medium scope_breakdown in its recent research_history
    is data-starved — the hard-skip gate is bypassed so the exploration lottery
    can run, breaking the self-reinforcing loop where medium is never attempted."""
    from app.shared_state import runtime_config
    direction_id = "d-hardskip-starved"
    _, task, _ = await _setup_escalation_project(db_session, direction_id, None)
    _mock_escalation_externals(mocker, direction_id)
    mocker.patch("app.research_completion.predict_spawn_success", return_value=0.05)
    # Force exploration to fire so a child is spawned (otherwise the finding
    # still falls through to escalation, which also spawns a child).
    mocker.patch.dict(
        "app.shared_state.runtime_config",
        {"medium_exploration_fraction": 1.0},
    )
    mocker.patch("app.research_completion.random.random", return_value=0.0)
    runtime_config["medium_hard_skip_threshold"] = 0.10
    try:
        await _handle_research_completion(task, "", db_session)
    finally:
        runtime_config.pop("medium_hard_skip_threshold", None)

    result = await db_session.execute(select(Task).where(Task.parent_task_id == task.id))
    children = result.scalars().all()
    # A child was spawned (hard-skip was bypassed) and no deferral was recorded.
    assert len(children) == 1
    await db_session.refresh(task)
    skip_counts = (task.context or {}).get("skipped_low_confidence_by_scope") or {}
    assert skip_counts.get("medium", 0) == 0


@pytest.mark.asyncio
async def test_hard_skip_counter_in_scope_breakdown(db_session, mocker):
    """skipped_low_confidence counter from the parent research task surfaces in
    the per-direction scope_breakdown after _update_direction_research_outcomes
    runs."""
    from app.shared_state import runtime_config
    from app.direction_outcomes import _update_direction_research_outcomes
    direction_id = "d-hardskip-3"
    project, task, _ = await _setup_escalation_project(db_session, direction_id, None)
    _mock_escalation_externals(mocker, direction_id)
    mocker.patch("app.research_completion.predict_spawn_success", return_value=0.05)
    runtime_config["medium_hard_skip_threshold"] = 0.10
    # Seed a research_history entry so _update_direction_research_outcomes finds it.
    # Includes medium scope_breakdown so the hard-skip gate is not bypassed for
    # data-starved directions.
    project.directions[0]["research_history"] = [{
        "task_id": str(task.id),
        "tasks_created": 0,
        "scope_breakdown": {"medium": {"tasks_created": 1, "prs_merged": 0}},
    }]
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(project, "directions")
    db_session.add(project)
    await db_session.commit()

    try:
        await _handle_research_completion(task, "", db_session)
    finally:
        runtime_config.pop("medium_hard_skip_threshold", None)

    # Spawn at least one dummy child so the rebuild logic engages.
    dummy_child = Task(
        project_id=project.id,
        title="dummy",
        parent_task_id=task.id,
        payloads=[{"type": "create"}],
        context={"scope": "small", "direction_id": direction_id},
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
    )
    db_session.add(dummy_child)
    await db_session.commit()

    await _update_direction_research_outcomes(project.id, direction_id, str(task.id), db_session)
    await db_session.refresh(project)
    sb = project.directions[0]["research_history"][0]["scope_breakdown"]
    assert sb["medium"]["skipped_low_confidence"] == 1


@pytest.mark.asyncio
async def test_auto_escalate_suppressed_by_poor_decomp_history(db_session, mocker):
    """Medium-scope low-confidence finding stays at medium when prior plan
    decompositions merged poorly (shares suppression with _calibrate_scope)."""
    direction_id = "d-suppressed"
    _, task, _ = await _setup_escalation_project(
        db_session, direction_id,
        [{"subtask_merge_rate": 0.1, "subtask_count": 5}],
    )
    _mock_escalation_externals(mocker, direction_id)
    mocker.patch("app.research_completion.predict_spawn_success", return_value=0.15)

    await _handle_research_completion(task, "", db_session)

    result = await db_session.execute(select(Task).where(Task.parent_task_id == task.id))
    children = result.scalars().all()
    assert len(children) == 1
    child = children[0]
    assert child.context["scope"] == "medium"
    assert child.context["low_confidence_spawn"] is True
    assert child.first_payload["type"] == "create"


@pytest.mark.asyncio
async def test_completion_suggestion_toggles_off_when_no_longer_complete(db_session):
    """A previously possibly_complete direction clears both flags when
    _check_direction_completion later returns None, and the cleared state
    is committed."""
    direction = {
        "id": "d-toggle-off-1",
        "text": "Direction that looked complete earlier",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        # Empty history → _check_direction_completion returns None (needs ≥4 entries)
        # and _check_direction_health returns None (no rules trip).
        "research_history": [],
        "possibly_complete": True,
        "completion_suggestion": (
            "Direction appears complete: 3 PRs merged across 4 cycles, "
            "last 2 cycles low-yield"
        ),
    }
    project = Project(
        name="Completion Toggle-Off",
        user_id="u-toggle",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "t"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    active = await _process_direction_lifecycle(
        project, db_session, cooldown_minutes=60.0,
        busy_direction_ids=set(), now=datetime.utcnow(),
    )

    # Direction stays active and eligible.
    assert len(active) == 1
    assert active[0]["id"] == "d-toggle-off-1"
    assert active[0]["status"] == "active"

    # Both flags removed from the direction dict.
    d = project.directions[0]
    assert "possibly_complete" not in d
    assert "completion_suggestion" not in d

    # directions_modified triggered a commit — the cleared state is persisted.
    await db_session.refresh(project)
    persisted = project.directions[0]
    assert "possibly_complete" not in persisted
    assert "completion_suggestion" not in persisted


@pytest.mark.asyncio
async def test_tiered_unproductive_cooldown_escalates_with_consecutive_failures(db_session):
    """1st unproductive cycle waits 0.375x base; 3rd+ waits full base."""
    base_cooldown = 240.0
    now = datetime.utcnow()

    def _make_direction(did: str, consecutive: int, minutes_ago: float) -> dict:
        return {
            "id": did,
            "text": f"dir-{did}",
            "status": "active",
            "priority": "medium",
            "paths": [],
            "added_at": "2026-01-01T00:00:00",
            "last_researched_at": (now - timedelta(minutes=minutes_ago)).isoformat(),
            "last_cycle_productive": False,
            "consecutive_unproductive_cycles": consecutive,
            "research_history": [],
        }

    # 1st consecutive (multiplier 0.375 → 90m): 100m ago passes, 80m ago does not.
    # 3rd+ consecutive (multiplier 1.0 → 240m): 100m ago does NOT pass.
    project = Project(
        name="Tiered Cooldown Test",
        user_id="u-tiered",
        autonomous_enabled=True,
        directions=[
            _make_direction("d-tier-1st-pass", consecutive=1, minutes_ago=100),
            _make_direction("d-tier-1st-fail", consecutive=1, minutes_ago=80),
            _make_direction("d-tier-3rd-fail", consecutive=3, minutes_ago=100),
        ],
        vision={"purpose": "t"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    active = await _process_direction_lifecycle(
        project, db_session, cooldown_minutes=base_cooldown,
        busy_direction_ids=set(), now=now,
    )
    active_ids = {d["id"] for d in active}
    assert "d-tier-1st-pass" in active_ids, "1 consecutive unproductive cycle should use 0.375x cooldown"
    assert "d-tier-1st-fail" not in active_ids, "80m < 90m (0.375 * 240) — still cooling down"
    assert "d-tier-3rd-fail" not in active_ids, "3+ consecutive unproductive cycles should use full base cooldown"
