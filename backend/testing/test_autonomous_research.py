"""
Integration tests for the autonomous research pipeline and direction management.

Focuses on gaps not covered by test_directions.py:
1. _spawn_research_task() guards: circuit breaker, rate limits, active-research guard,
   no-directions guard
2. _handle_research_completion(): depth limit, deduplication (hash + word overlap),
   direction_id tracking, research_history JSONB updates, task type selection
3. Direction management API endpoints: PUT (replace all), PATCH reorder,
   POST /research with/without bypass_cooldown
"""
import hashlib
import uuid
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient
from sqlalchemy import select

import app.shared_state as _shared_state
from app.models import Project, Task, TaskStatus
from app.main import _spawn_research_task, _handle_research_completion


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

async def _deploy_project(client: AsyncClient, mocker, name: str = "Test Project") -> str:
    """Create a project via the deploy API and return its project_id string."""
    mocker.patch("app.main.provision_agent_vm", return_value=None)
    resp = await client.post(
        "/api/deploy",
        json={"name": name, "payloads": [{"command": "setup"}], "storage_uris": []},
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
    """POST a direction and return the created direction dict."""
    resp = await client.post(
        f"/api/admin/projects/{project_id}/directions",
        json={"text": text, "priority": priority, "paths": paths or []},
    )
    assert resp.status_code == 200
    return resp.json()["direction"]


def _patch_research_externals(mocker):
    """Patch all external calls needed by _handle_research_completion."""
    mocker.patch("app.main._ensure_branch", new=AsyncMock(return_value=True))
    mocker.patch("app.main.broadcast_to_project", new=AsyncMock(return_value=None))
    mocker.patch("app.main._validate_plan_dependencies", return_value=True)
    mocker.patch("app.main._log_file_overlaps", return_value=None)


def _make_finding(
    title: str = "Fix a bug",
    direction_id: str = "d-test0001",
    scope: str = "small",
    finding_type: str = "improvement",
) -> dict:
    return {
        "title": title,
        "description": f"Description: {title}",
        "prompt": f"Implement: {title}",
        "priority": "medium",
        "scope": scope,
        "risk": "low",
        "type": finding_type,
        "direction_id": direction_id,
    }


def _active_direction(direction_id: str, text: str = "Test direction") -> dict:
    return {
        "id": direction_id,
        "text": text,
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
    }


async def _make_research_task(
    project: Project, direction: dict, db_session, depth: int = 0
) -> Task:
    task = Task(
        project_id=project.id,
        title=f"Research: {direction['text']}",
        payloads=[{"type": "research", "directions": [direction], "base_branch": "main"}],
        context={"research_depth": depth},
        status=TaskStatus.IN_PROGRESS,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    return task


# ─────────────────────────────────────────────────────────────────────────────
# 1. _spawn_research_task — Circuit Breaker Guards
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_spawn_blocked_by_open_circuit_breaker(db_session):
    """Circuit breaker in OPEN state (cooldown not elapsed) prevents spawning."""
    project = Project(
        name="CB Open Block",
        autonomous_enabled=True,
        directions=[_active_direction("d-cbopen01")],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Trip breaker — opened just now, cooldown (30 min) has NOT elapsed
    _shared_state.circuit_breakers[str(project.id)] = {
        "state": "OPEN",
        "failure_count": 3,
        "success_count": 0,
        "opened_at": datetime.utcnow(),
        "cooldown_seconds": 1800,
        "base_cooldown": 1800,
        "half_open_task_id": None,
    }
    try:
        await _spawn_research_task(project, db_session)

        result = await db_session.execute(select(Task).where(Task.project_id == project.id))
        assert len(result.scalars().all()) == 0, "No task spawned when circuit breaker OPEN"
    finally:
        _shared_state.circuit_breakers.pop(str(project.id), None)


@pytest.mark.asyncio
async def test_spawn_allowed_when_circuit_breaker_cooldown_elapsed(db_session):
    """Circuit breaker transitions OPEN → HALF-OPEN when cooldown has elapsed, one task spawned."""
    project = Project(
        name="CB Cooldown Elapsed",
        autonomous_enabled=True,
        directions=[_active_direction("d-cbhalf01")],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Opened 2 hours ago — 30-min cooldown has long elapsed
    _shared_state.circuit_breakers[str(project.id)] = {
        "state": "OPEN",
        "failure_count": 3,
        "success_count": 0,
        "opened_at": datetime.utcnow() - timedelta(hours=2),
        "cooldown_seconds": 1800,
        "base_cooldown": 1800,
        "half_open_task_id": None,
    }
    try:
        await _spawn_research_task(project, db_session)

        result = await db_session.execute(select(Task).where(Task.project_id == project.id))
        tasks = result.scalars().all()
        research = [t for t in tasks if (t.payloads or [{}])[0].get("type") == "research"]
        assert len(research) == 1, "One test task should be spawned in HALF-OPEN state"

        # Breaker should have transitioned to HALF-OPEN
        breaker = _shared_state.circuit_breakers[str(project.id)]
        assert breaker["state"] == "HALF_OPEN"
        assert breaker["half_open_task_id"] == str(research[0].id)
    finally:
        _shared_state.circuit_breakers.pop(str(project.id), None)


# ─────────────────────────────────────────────────────────────────────────────
# 2. _spawn_research_task — No-Directions Guards
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_spawn_skipped_when_directions_empty(db_session):
    """_spawn_research_task does nothing when the directions list is empty."""
    project = Project(
        name="Empty Directions",
        autonomous_enabled=True,
        directions=[],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _spawn_research_task(project, db_session)

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    assert len(result.scalars().all()) == 0


@pytest.mark.asyncio
async def test_spawn_skipped_when_all_directions_non_active(db_session):
    """_spawn_research_task does nothing when all directions are paused or completed."""
    project = Project(
        name="All Non-Active Directions",
        autonomous_enabled=True,
        directions=[
            {**_active_direction("d-paused01", "Paused A"), "status": "paused"},
            {**_active_direction("d-done0001", "Completed B"), "status": "completed"},
        ],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _spawn_research_task(project, db_session)

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    assert len(result.scalars().all()) == 0


# ─────────────────────────────────────────────────────────────────────────────
# 3. _spawn_research_task — Active Research Guard
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_spawn_skipped_when_research_task_already_queued(db_session):
    """_spawn_research_task does not spawn a second task if one is already QUEUED."""
    project = Project(
        name="Already Running Research",
        autonomous_enabled=True,
        directions=[_active_direction("d-running1")],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Existing QUEUED research task for this project
    existing = Task(
        project_id=project.id,
        title="Research: existing",
        payloads=[{"type": "research", "directions": [], "base_branch": "main"}],
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(existing)
    await db_session.commit()

    await _spawn_research_task(project, db_session)

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    all_tasks = result.scalars().all()
    research = [t for t in all_tasks if (t.payloads or [{}])[0].get("type") == "research"]
    assert len(research) == 1, "Must not spawn a second research task"


@pytest.mark.asyncio
async def test_spawn_skipped_when_research_task_in_progress(db_session):
    """_spawn_research_task does not spawn when a research task is IN_PROGRESS."""
    project = Project(
        name="In-Progress Research",
        autonomous_enabled=True,
        directions=[_active_direction("d-inprog01")],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    existing = Task(
        project_id=project.id,
        title="Research: in progress",
        payloads=[{"type": "research", "directions": [], "base_branch": "main"}],
        status=TaskStatus.IN_PROGRESS,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(existing)
    await db_session.commit()

    await _spawn_research_task(project, db_session)

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    all_tasks = result.scalars().all()
    research = [t for t in all_tasks if (t.payloads or [{}])[0].get("type") == "research"]
    assert len(research) == 1, "Must not spawn when research already IN_PROGRESS"


# ─────────────────────────────────────────────────────────────────────────────
# 4. _spawn_research_task — Rate Limits
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_spawn_skipped_at_per_project_rate_limit(db_session):
    """_spawn_research_task does not spawn when the per-project hourly limit is saturated."""
    project = Project(
        name="Per-Project Rate Limit",
        autonomous_enabled=True,
        directions=[_active_direction("d-ratelm01")],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Lower per-project limit to 1, then create one qualifying ORCHESTRATOR research task
    original = _shared_state.runtime_config["max_autonomous_tasks_per_hour"]
    _shared_state.runtime_config["max_autonomous_tasks_per_hour"] = 1

    saturating_task = Task(
        project_id=project.id,
        title="Research: saturating",
        payloads=[{"type": "research", "directions": [], "base_branch": "main"}],
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(saturating_task)
    await db_session.commit()

    try:
        await _spawn_research_task(project, db_session)

        result = await db_session.execute(
            select(Task).where(
                Task.project_id == project.id,
                Task.status == TaskStatus.QUEUED,
            )
        )
        assert len(result.scalars().all()) == 0, "No QUEUED task should be spawned at rate limit"
    finally:
        _shared_state.runtime_config["max_autonomous_tasks_per_hour"] = original


@pytest.mark.asyncio
async def test_spawn_skipped_at_global_rate_limit(db_session):
    """_spawn_research_task does not spawn when the global hourly limit is at zero."""
    project = Project(
        name="Global Rate Limit",
        autonomous_enabled=True,
        directions=[_active_direction("d-globrl01")],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    original = _shared_state.runtime_config["max_global_autonomous_tasks_per_hour"]
    _shared_state.runtime_config["max_global_autonomous_tasks_per_hour"] = 0
    try:
        await _spawn_research_task(project, db_session)

        result = await db_session.execute(select(Task).where(Task.project_id == project.id))
        assert len(result.scalars().all()) == 0, "No task should be spawned when global limit is 0"
    finally:
        _shared_state.runtime_config["max_global_autonomous_tasks_per_hour"] = original


# ─────────────────────────────────────────────────────────────────────────────
# 5. _handle_research_completion — Depth Guard
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_depth_limit_prevents_child_tasks(db_session, mocker):
    """Research at MAX_RESEARCH_DEPTH=3 skips child task creation and sets a status message."""
    direction = _active_direction("d-depth001", "Deep research")
    project = Project(
        name="Depth Limit", autonomous_enabled=True, directions=[direction]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # depth=3 is at the limit — no children should be spawned
    task = await _make_research_task(project, direction, db_session, depth=3)

    mocker.patch(
        "app.main._parse_research_findings",
        return_value=[_make_finding(direction_id=direction["id"])],
    )

    await _handle_research_completion(task, "", db_session)

    result = await db_session.execute(
        select(Task).where(
            Task.project_id == project.id, Task.id != task.id
        )
    )
    assert len(result.scalars().all()) == 0, "No children at max depth"
    assert "depth limit" in (task.latest_status_message or "").lower()


# ─────────────────────────────────────────────────────────────────────────────
# 6. _handle_research_completion — Deduplication
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_hash_deduplication_skips_finding_with_matching_hash(db_session, mocker):
    """A finding whose hash matches a recent task's finding_hash context key is skipped."""
    direction = _active_direction("d-hashdup1", "Hash dedup direction")
    project = Project(
        name="Hash Dedup Test", autonomous_enabled=True, directions=[direction]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    dup_title = "Fix authentication token expiry handling"
    dup_desc = "Auth tokens expire without proper notification"
    fhash = hashlib.sha256((dup_title + dup_desc).encode()).hexdigest()[:16]

    # Existing task from within the last 30 days with this finding hash
    existing = Task(
        project_id=project.id,
        title=dup_title,
        payloads=[{"type": "create", "prompt": "fix it", "base_branch": "main"}],
        context={"finding_hash": fhash},
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(existing)
    await db_session.commit()

    task = await _make_research_task(project, direction, db_session)

    _patch_research_externals(mocker)
    mocker.patch(
        "app.main._parse_research_findings",
        return_value=[{
            "title": dup_title,
            "description": dup_desc,
            "prompt": "Fix auth token expiry",
            "priority": "high",
            "scope": "small",
            "risk": "high",
            "type": "improvement",
            "direction_id": direction["id"],
        }],
    )

    await _handle_research_completion(task, "", db_session)

    result = await db_session.execute(
        select(Task).where(
            Task.project_id == project.id, Task.parent_task_id == task.id
        )
    )
    assert len(result.scalars().all()) == 0, "Hash-duplicate finding must not create a task"


@pytest.mark.asyncio
async def test_word_overlap_deduplication_skips_similar_title(db_session, mocker):
    """A finding with 3+ meaningful words overlapping an active task title is skipped."""
    direction = _active_direction("d-worddup1", "Word overlap direction")
    project = Project(
        name="Word Overlap Dedup", autonomous_enabled=True, directions=[direction]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Active task with a title that shares key words
    existing = Task(
        project_id=project.id,
        title="optimize database connection pooling configuration",
        payloads=[{"type": "create", "prompt": "...", "base_branch": "main"}],
        context={},
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(existing)
    await db_session.commit()

    task = await _make_research_task(project, direction, db_session)

    _patch_research_externals(mocker)
    mocker.patch(
        "app.main._parse_research_findings",
        return_value=[{
            # "database", "connection", "pooling", "configuration" overlap existing → ≥3 words
            "title": "tune database connection pooling configuration settings",
            "description": "Improve pooling",
            "prompt": "Tune the connection pool",
            "priority": "medium",
            "scope": "small",
            "risk": "low",
            "type": "improvement",
            "direction_id": direction["id"],
        }],
    )

    await _handle_research_completion(task, "", db_session)

    result = await db_session.execute(
        select(Task).where(
            Task.project_id == project.id, Task.parent_task_id == task.id
        )
    )
    assert len(result.scalars().all()) == 0, "Word-overlap duplicate must not create a task"


@pytest.mark.asyncio
async def test_unique_findings_are_not_deduplicated(db_session, mocker):
    """Distinct findings that share fewer than 3 meaningful words each get a child task."""
    direction = _active_direction("d-unique01", "Unique findings direction")
    project = Project(
        name="Unique Findings", autonomous_enabled=True, directions=[direction]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Existing task with unrelated title
    existing = Task(
        project_id=project.id,
        title="add prometheus metrics endpoint",
        payloads=[{"type": "create", "prompt": "...", "base_branch": "main"}],
        context={},
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(existing)
    await db_session.commit()

    task = await _make_research_task(project, direction, db_session)

    _patch_research_externals(mocker)
    mocker.patch(
        "app.main._parse_research_findings",
        return_value=[
            _make_finding("Upgrade authentication to OAuth2", direction["id"]),
            _make_finding("Add input validation to signup form", direction["id"]),
        ],
    )

    await _handle_research_completion(task, "", db_session)

    result = await db_session.execute(
        select(Task).where(
            Task.project_id == project.id, Task.parent_task_id == task.id
        )
    )
    child_tasks = result.scalars().all()
    assert len(child_tasks) == 2, "Both unique findings should produce child tasks"


# ─────────────────────────────────────────────────────────────────────────────
# 7. _handle_research_completion — Child Task Properties
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_child_task_direction_id_set_in_context(db_session, mocker):
    """Child tasks created from findings carry direction_id in their context."""
    direction_id = "d-dirctx01"
    direction = _active_direction(direction_id, "Direction context test")
    project = Project(
        name="Child Context Test", autonomous_enabled=True, directions=[direction]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_research_task(project, direction, db_session)

    _patch_research_externals(mocker)
    mocker.patch(
        "app.main._parse_research_findings",
        return_value=[_make_finding(direction_id=direction_id)],
    )

    await _handle_research_completion(task, "", db_session)

    result = await db_session.execute(
        select(Task).where(
            Task.project_id == project.id, Task.parent_task_id == task.id
        )
    )
    child_tasks = result.scalars().all()
    assert len(child_tasks) == 1
    assert child_tasks[0].context.get("direction_id") == direction_id


@pytest.mark.asyncio
async def test_child_task_has_research_task_as_parent(db_session, mocker):
    """Child tasks have parent_task_id pointing to the originating research task."""
    direction = _active_direction("d-parent01", "Parent tracking direction")
    project = Project(
        name="Parent Task ID Test", autonomous_enabled=True, directions=[direction]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_research_task(project, direction, db_session)

    _patch_research_externals(mocker)
    mocker.patch(
        "app.main._parse_research_findings",
        return_value=[_make_finding(direction_id=direction["id"])],
    )

    await _handle_research_completion(task, "", db_session)

    result = await db_session.execute(
        select(Task).where(
            Task.project_id == project.id, Task.parent_task_id == task.id
        )
    )
    child_tasks = result.scalars().all()
    assert len(child_tasks) == 1
    assert child_tasks[0].parent_task_id == task.id


@pytest.mark.asyncio
async def test_research_task_status_becomes_evaluating(db_session, mocker):
    """After _handle_research_completion, the research task's status is EVALUATING."""
    direction = _active_direction("d-evalsts1", "Evaluating status direction")
    project = Project(
        name="EVALUATING Status Test", autonomous_enabled=True, directions=[direction]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_research_task(project, direction, db_session)

    _patch_research_externals(mocker)
    mocker.patch(
        "app.main._parse_research_findings",
        return_value=[_make_finding(direction_id=direction["id"])],
    )

    await _handle_research_completion(task, "", db_session)

    await db_session.refresh(task)
    assert task.status == TaskStatus.EVALUATING


@pytest.mark.asyncio
async def test_multiple_findings_each_create_a_child_task(db_session, mocker):
    """Three distinct findings produce three child tasks."""
    direction = _active_direction("d-multi001", "Multi-finding direction")
    project = Project(
        name="Multiple Findings", autonomous_enabled=True, directions=[direction]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_research_task(project, direction, db_session)

    _patch_research_externals(mocker)
    mocker.patch(
        "app.main._parse_research_findings",
        return_value=[
            _make_finding("Add request tracing middleware to all routes", direction["id"]),
            _make_finding("Implement exponential backoff on external API calls", direction["id"]),
            _make_finding("Replace print statements with structured logging", direction["id"]),
        ],
    )

    await _handle_research_completion(task, "", db_session)

    result = await db_session.execute(
        select(Task).where(
            Task.project_id == project.id, Task.parent_task_id == task.id
        )
    )
    assert len(result.scalars().all()) == 3


@pytest.mark.asyncio
async def test_child_task_carries_research_depth_plus_one(db_session, mocker):
    """Child tasks have research_depth = parent research_depth + 1."""
    direction = _active_direction("d-depth2x1", "Depth tracking direction")
    project = Project(
        name="Depth Increment Test", autonomous_enabled=True, directions=[direction]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Parent at depth 1
    task = await _make_research_task(project, direction, db_session, depth=1)

    _patch_research_externals(mocker)
    mocker.patch(
        "app.main._parse_research_findings",
        return_value=[_make_finding(direction_id=direction["id"])],
    )

    await _handle_research_completion(task, "", db_session)

    result = await db_session.execute(
        select(Task).where(
            Task.project_id == project.id, Task.parent_task_id == task.id
        )
    )
    child_tasks = result.scalars().all()
    assert len(child_tasks) == 1
    assert child_tasks[0].context.get("research_depth") == 2


# ─────────────────────────────────────────────────────────────────────────────
# 8. _handle_research_completion — Task Type Selection
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_finding_type_research_spawns_research_subtask(db_session, mocker):
    """A finding with type='research' spawns a child research task."""
    direction = _active_direction("d-typeres1", "Type research")
    project = Project(
        name="Sub-Research Test", autonomous_enabled=True, directions=[direction]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_research_task(project, direction, db_session)

    _patch_research_externals(mocker)
    mocker.patch(
        "app.main._parse_research_findings",
        return_value=[_make_finding(
            "Investigate query performance bottlenecks in depth",
            direction["id"],
            finding_type="research",
        )],
    )

    await _handle_research_completion(task, "", db_session)

    result = await db_session.execute(
        select(Task).where(
            Task.project_id == project.id, Task.parent_task_id == task.id
        )
    )
    child_tasks = result.scalars().all()
    assert len(child_tasks) == 1
    assert child_tasks[0].payloads[0]["type"] == "research"


@pytest.mark.asyncio
async def test_finding_scope_large_spawns_plan_task(db_session, mocker):
    """A finding with scope='large' spawns a 'plan' task for decomposition."""
    direction = _active_direction("d-planscop1", "Large scope direction")
    project = Project(
        name="Large Scope Plan Test", autonomous_enabled=True, directions=[direction]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_research_task(project, direction, db_session)

    _patch_research_externals(mocker)
    mocker.patch(
        "app.main._parse_research_findings",
        return_value=[_make_finding(
            "Migrate entire payment processing system to new provider",
            direction["id"],
            scope="large",
        )],
    )

    await _handle_research_completion(task, "", db_session)

    result = await db_session.execute(
        select(Task).where(
            Task.project_id == project.id, Task.parent_task_id == task.id
        )
    )
    child_tasks = result.scalars().all()
    assert len(child_tasks) == 1
    assert child_tasks[0].payloads[0]["type"] == "plan"


@pytest.mark.asyncio
async def test_finding_scope_small_spawns_create_task(db_session, mocker):
    """A finding with scope='small' spawns a 'create' task for single-PR work."""
    direction = _active_direction("d-createsc1", "Small scope direction")
    project = Project(
        name="Small Scope Create Test", autonomous_enabled=True, directions=[direction]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_research_task(project, direction, db_session)

    _patch_research_externals(mocker)
    mocker.patch(
        "app.main._parse_research_findings",
        return_value=[_make_finding(
            "Add null check to user profile endpoint handler",
            direction["id"],
            scope="small",
        )],
    )

    await _handle_research_completion(task, "", db_session)

    result = await db_session.execute(
        select(Task).where(
            Task.project_id == project.id, Task.parent_task_id == task.id
        )
    )
    child_tasks = result.scalars().all()
    assert len(child_tasks) == 1
    assert child_tasks[0].payloads[0]["type"] == "create"


@pytest.mark.asyncio
async def test_finding_type_security_audit_spawns_security_audit_task(db_session, mocker):
    """A finding with type='security_audit' spawns a specialized security_audit task."""
    direction = _active_direction("d-secaud01", "Security audit direction")
    project = Project(
        name="Security Audit Test", autonomous_enabled=True, directions=[direction]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_research_task(project, direction, db_session)

    _patch_research_externals(mocker)
    mocker.patch(
        "app.main._parse_research_findings",
        return_value=[_make_finding(
            "Audit authentication module for injection vulnerabilities",
            direction["id"],
            finding_type="security_audit",
        )],
    )

    await _handle_research_completion(task, "", db_session)

    result = await db_session.execute(
        select(Task).where(
            Task.project_id == project.id, Task.parent_task_id == task.id
        )
    )
    child_tasks = result.scalars().all()
    assert len(child_tasks) == 1
    assert child_tasks[0].payloads[0]["type"] == "security_audit"


# ─────────────────────────────────────────────────────────────────────────────
# 9. _handle_research_completion — JSONB research_history Updates
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_research_history_entry_has_correct_fields(db_session, mocker):
    """research_history gets a new entry with timestamp, task_id, tasks_created, finding_titles."""
    direction_id = "d-history1"
    direction = {
        **_active_direction(direction_id, "History fields direction"),
        "research_history": [],
    }
    project = Project(
        name="History Fields Test", autonomous_enabled=True, directions=[direction]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_research_task(project, direction, db_session)
    finding_title = "Add structured logging to all service methods"

    _patch_research_externals(mocker)
    mocker.patch(
        "app.main._parse_research_findings",
        return_value=[_make_finding(finding_title, direction_id)],
    )

    before = datetime.utcnow()
    await _handle_research_completion(task, "", db_session)

    await db_session.refresh(project)
    updated = next(d for d in project.directions if d["id"] == direction_id)
    history = updated.get("research_history", [])

    assert len(history) == 1
    entry = history[0]
    assert entry["task_id"] == str(task.id)
    assert entry["tasks_created"] == 1
    assert finding_title in entry.get("finding_titles", [])
    assert datetime.fromisoformat(entry["timestamp"]) >= before


@pytest.mark.asyncio
async def test_research_history_capped_at_10_entries(db_session, mocker):
    """research_history is truncated to 10 entries — oldest entry is dropped."""
    direction_id = "d-histcap1"
    # Seed with exactly 10 existing entries
    existing_history = [
        {
            "timestamp": f"2025-01-{i:02d}T00:00:00",
            "task_id": str(uuid.uuid4()),
            "tasks_created": 1,
            "finding_titles": [f"Old finding {i}"],
        }
        for i in range(1, 11)
    ]
    direction = {
        **_active_direction(direction_id, "History cap direction"),
        "research_history": existing_history,
    }
    project = Project(
        name="History Cap Test", autonomous_enabled=True, directions=[direction]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_research_task(project, direction, db_session)

    _patch_research_externals(mocker)
    mocker.patch(
        "app.main._parse_research_findings",
        return_value=[_make_finding("Brand new finding for cap test", direction_id)],
    )

    await _handle_research_completion(task, "", db_session)

    await db_session.refresh(project)
    updated = next(d for d in project.directions if d["id"] == direction_id)
    history = updated.get("research_history", [])

    assert len(history) == 10, "research_history must stay ≤ 10 entries"
    # Newest entry (just added) should be at the end
    assert history[-1]["task_id"] == str(task.id)
    # Oldest entry (index 0 from seed) should have been dropped
    oldest_id = existing_history[0]["task_id"]
    assert oldest_id not in [e["task_id"] for e in history], "Oldest entry should be evicted"


@pytest.mark.asyncio
async def test_research_history_only_updated_for_directions_with_findings(db_session, mocker):
    """Only directions that produced at least one child task get a research_history entry."""
    dir_a_id = "d-histdra1"
    dir_b_id = "d-histdrb1"
    directions = [
        {**_active_direction(dir_a_id, "Direction A with findings"), "research_history": []},
        {**_active_direction(dir_b_id, "Direction B no findings"), "research_history": []},
    ]
    project = Project(
        name="Selective History Test", autonomous_enabled=True, directions=directions
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_research_task(project, directions[0], db_session)

    _patch_research_externals(mocker)
    # Only direction A produces a finding
    mocker.patch(
        "app.main._parse_research_findings",
        return_value=[_make_finding(direction_id=dir_a_id)],
    )

    await _handle_research_completion(task, "", db_session)

    await db_session.refresh(project)
    dir_a = next(d for d in project.directions if d["id"] == dir_a_id)
    dir_b = next(d for d in project.directions if d["id"] == dir_b_id)

    assert len(dir_a.get("research_history", [])) == 1
    assert len(dir_b.get("research_history", [])) == 0


# ─────────────────────────────────────────────────────────────────────────────
# 10. _handle_research_completion — Direction Proposal Handling
# ─────────────────────────────────────────────────────────────────────────────

def _make_direction_proposal(
    prompt: str = "Explore new caching strategy",
    description: str = "Rationale for this proposal",
    priority: str = "medium",
) -> dict:
    return {
        "type": "direction_proposal",
        "title": prompt,
        "prompt": prompt,
        "description": description,
        "priority": priority,
        "scope": "small",
        "risk": "low",
        "direction_id": None,
    }


@pytest.mark.asyncio
async def test_research_completion_creates_direction_proposals(db_session, mocker):
    """direction_proposal findings are stored in project.directions with status='proposed'."""
    direction_id = "d-prop0001"
    direction = _active_direction(direction_id, "Active direction")
    project = Project(
        name="Proposal Happy Path", autonomous_enabled=True, directions=[direction]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_research_task(project, direction, db_session)
    proposal_prompt = "Investigate database index improvements"
    proposal_rationale = "Query times are increasing under load"

    _patch_research_externals(mocker)
    mocker.patch(
        "app.main._parse_research_findings",
        return_value=[_make_direction_proposal(proposal_prompt, proposal_rationale, "high")],
    )

    await _handle_research_completion(task, "", db_session)

    await db_session.refresh(project)
    proposed = [d for d in project.directions if d.get("status") == "proposed"]
    assert len(proposed) == 1

    p = proposed[0]
    assert p["text"] == proposal_prompt
    assert p["status"] == "proposed"
    assert p["priority"] == "high"
    assert p["proposed_by"] == str(task.id)
    assert p["proposal_rationale"] == proposal_rationale
    assert p["proposed_at"] is not None
    assert p["id"].startswith("d-")


@pytest.mark.asyncio
async def test_direction_proposals_not_converted_to_tasks(db_session, mocker):
    """direction_proposal findings must not create child tasks."""
    direction_id = "d-prop0002"
    direction = _active_direction(direction_id, "Active direction")
    project = Project(
        name="Proposal No Task", autonomous_enabled=True, directions=[direction]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_research_task(project, direction, db_session)

    _patch_research_externals(mocker)
    mocker.patch(
        "app.main._parse_research_findings",
        return_value=[
            _make_direction_proposal("Add observability layer"),
            _make_finding("A real improvement finding", direction_id),
        ],
    )

    before_ids = set(
        (await db_session.execute(select(Task.id))).scalars().all()
    )
    await _handle_research_completion(task, "", db_session)
    after_ids = set(
        (await db_session.execute(select(Task.id))).scalars().all()
    )

    new_task_ids = after_ids - before_ids
    # Only the real improvement finding should produce a child task, not the proposal
    assert len(new_task_ids) == 1

    await db_session.refresh(project)
    proposed = [d for d in project.directions if d.get("status") == "proposed"]
    assert len(proposed) == 1
    assert proposed[0]["text"] == "Add observability layer"


@pytest.mark.asyncio
async def test_direction_proposals_capped_at_5_per_call(db_session, mocker):
    """At most 5 direction_proposal findings are stored per research call."""
    direction_id = "d-prop0003"
    direction = _active_direction(direction_id, "Active direction")
    project = Project(
        name="Proposal Per-Call Cap", autonomous_enabled=True, directions=[direction]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_research_task(project, direction, db_session)

    proposals = [_make_direction_proposal(f"Proposal {i}") for i in range(8)]
    _patch_research_externals(mocker)
    mocker.patch("app.main._parse_research_findings", return_value=proposals)

    await _handle_research_completion(task, "", db_session)

    await db_session.refresh(project)
    proposed = [d for d in project.directions if d.get("status") == "proposed"]
    assert len(proposed) == 5, "Only 5 proposals should be created per call, not 8"


@pytest.mark.asyncio
async def test_direction_proposals_capped_at_10_pending_total(db_session, mocker):
    """No new proposals are added when 10 pending proposals already exist."""
    direction_id = "d-prop0004"
    direction = _active_direction(direction_id, "Active direction")
    existing_proposed = [
        {
            "id": f"d-existing{i:02d}",
            "text": f"Existing proposal {i}",
            "status": "proposed",
            "priority": "medium",
            "paths": [],
            "added_at": "2026-01-01T00:00:00",
            "proposed_by": "some-task-id",
            "proposal_rationale": "",
            "proposed_at": "2026-01-01T00:00:00",
        }
        for i in range(10)
    ]
    project = Project(
        name="Proposal Global Cap",
        autonomous_enabled=True,
        directions=[direction] + existing_proposed,
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_research_task(project, direction, db_session)

    _patch_research_externals(mocker)
    mocker.patch(
        "app.main._parse_research_findings",
        return_value=[_make_direction_proposal("New proposal that should be blocked")],
    )

    await _handle_research_completion(task, "", db_session)

    await db_session.refresh(project)
    proposed = [d for d in project.directions if d.get("status") == "proposed"]
    assert len(proposed) == 10, "Must not exceed 10 pending proposals"


# ─────────────────────────────────────────────────────────────────────────────
# 11. Direction API — PUT (replace all directions)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_put_directions_replaces_entire_list(client: AsyncClient, mocker):
    """PUT /directions discards existing directions and installs the provided list."""
    project_id = await _deploy_project(client, mocker)
    await _add_direction(client, project_id, text="Original A")
    await _add_direction(client, project_id, text="Original B")

    resp = await client.put(
        f"/api/admin/projects/{project_id}/directions",
        json=[
            {"text": "Replacement X", "priority": "high", "status": "active"},
            {"text": "Replacement Y", "priority": "low", "status": "active"},
        ],
    )
    assert resp.status_code == 200
    directions = resp.json()["directions"]
    texts = {d["text"] for d in directions}
    assert texts == {"Replacement X", "Replacement Y"}
    assert "Original A" not in texts
    assert "Original B" not in texts


@pytest.mark.asyncio
async def test_put_directions_auto_generates_missing_ids(client: AsyncClient, mocker):
    """PUT /directions auto-generates IDs for entries that don't have one."""
    project_id = await _deploy_project(client, mocker)

    resp = await client.put(
        f"/api/admin/projects/{project_id}/directions",
        json=[{"text": "No ID provided", "priority": "medium", "status": "active"}],
    )
    assert resp.status_code == 200
    directions = resp.json()["directions"]
    assert len(directions) == 1
    assert directions[0]["id"].startswith("d-")


@pytest.mark.asyncio
async def test_put_directions_preserves_provided_id(client: AsyncClient, mocker):
    """PUT /directions keeps explicitly provided IDs."""
    project_id = await _deploy_project(client, mocker)
    existing_id = "d-keepthis"

    resp = await client.put(
        f"/api/admin/projects/{project_id}/directions",
        json=[{"id": existing_id, "text": "Keep my ID", "priority": "medium", "status": "active"}],
    )
    assert resp.status_code == 200
    directions = resp.json()["directions"]
    assert directions[0]["id"] == existing_id


@pytest.mark.asyncio
async def test_put_directions_invalid_priority_returns_400(client: AsyncClient, mocker):
    """PUT /directions with an unrecognised priority returns 400."""
    project_id = await _deploy_project(client, mocker)

    resp = await client.put(
        f"/api/admin/projects/{project_id}/directions",
        json=[{"text": "Valid text", "priority": "critical", "status": "active"}],
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_put_directions_empty_text_returns_400(client: AsyncClient, mocker):
    """PUT /directions with empty text returns 400."""
    project_id = await _deploy_project(client, mocker)

    resp = await client.put(
        f"/api/admin/projects/{project_id}/directions",
        json=[{"text": "", "priority": "medium", "status": "active"}],
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_put_directions_project_not_found_returns_404(client: AsyncClient):
    """PUT /directions for nonexistent project returns 404."""
    resp = await client.put(
        f"/api/admin/projects/{uuid.uuid4()}/directions",
        json=[{"text": "Some direction", "priority": "medium", "status": "active"}],
    )
    assert resp.status_code == 404


# ─────────────────────────────────────────────────────────────────────────────
# 12. Direction API — PATCH /reorder
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_reorder_directions_changes_order(client: AsyncClient, mocker):
    """PATCH /directions/reorder returns directions in the requested order."""
    project_id = await _deploy_project(client, mocker)
    dir_a = await _add_direction(client, project_id, text="Direction A")
    dir_b = await _add_direction(client, project_id, text="Direction B")
    dir_c = await _add_direction(client, project_id, text="Direction C")

    resp = await client.patch(
        f"/api/admin/projects/{project_id}/directions/reorder",
        json={"direction_ids": [dir_c["id"], dir_a["id"], dir_b["id"]]},
    )
    assert resp.status_code == 200
    order = [d["id"] for d in resp.json()["directions"]]
    assert order == [dir_c["id"], dir_a["id"], dir_b["id"]]


@pytest.mark.asyncio
async def test_reorder_directions_persists_in_get(client: AsyncClient, mocker):
    """After reorder, GET /directions returns them in the new order."""
    project_id = await _deploy_project(client, mocker)
    dir_a = await _add_direction(client, project_id, text="Direction A")
    dir_b = await _add_direction(client, project_id, text="Direction B")

    await client.patch(
        f"/api/admin/projects/{project_id}/directions/reorder",
        json={"direction_ids": [dir_b["id"], dir_a["id"]]},
    )

    get_resp = await client.get(f"/api/admin/projects/{project_id}/directions")
    assert get_resp.status_code == 200
    order = [d["id"] for d in get_resp.json()["directions"]]
    assert order == [dir_b["id"], dir_a["id"]]


@pytest.mark.asyncio
async def test_reorder_with_missing_id_returns_400(client: AsyncClient, mocker):
    """PATCH /directions/reorder with a subset of direction IDs returns 400."""
    project_id = await _deploy_project(client, mocker)
    dir_a = await _add_direction(client, project_id, text="Direction A")
    await _add_direction(client, project_id, text="Direction B")

    # Provide only one ID when two exist
    resp = await client.patch(
        f"/api/admin/projects/{project_id}/directions/reorder",
        json={"direction_ids": [dir_a["id"]]},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_reorder_with_extra_id_returns_400(client: AsyncClient, mocker):
    """PATCH /directions/reorder with extra (unknown) IDs returns 400."""
    project_id = await _deploy_project(client, mocker)
    dir_a = await _add_direction(client, project_id, text="Direction A")

    resp = await client.patch(
        f"/api/admin/projects/{project_id}/directions/reorder",
        json={"direction_ids": [dir_a["id"], "d-doesnotexist"]},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_reorder_directions_project_not_found(client: AsyncClient):
    """PATCH /directions/reorder for nonexistent project returns 404."""
    resp = await client.patch(
        f"/api/admin/projects/{uuid.uuid4()}/directions/reorder",
        json={"direction_ids": []},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_reorder_non_list_payload_returns_400(client: AsyncClient, mocker):
    """PATCH /directions/reorder with non-list direction_ids returns 400."""
    project_id = await _deploy_project(client, mocker)

    resp = await client.patch(
        f"/api/admin/projects/{project_id}/directions/reorder",
        json={"direction_ids": "not-a-list"},
    )
    assert resp.status_code == 400


# ─────────────────────────────────────────────────────────────────────────────
# 13. Direction API — POST /{direction_id}/research (cooldown behavior)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_research_endpoint_returns_429_when_direction_in_cooldown(
    client: AsyncClient, mocker
):
    """POST /research without bypass returns 429 when direction was recently researched."""
    project_id = await _deploy_project(client, mocker)
    direction = await _add_direction(client, project_id, text="Recently researched direction")
    direction_id = direction["id"]

    # Inject a last_researched_at 1 hour ago via PUT (within the 4-hour default cooldown)
    recent_ts = (datetime.utcnow() - timedelta(hours=1)).isoformat()
    put_resp = await client.put(
        f"/api/admin/projects/{project_id}/directions",
        json=[{
            **direction,
            "last_researched_at": recent_ts,
        }],
    )
    assert put_resp.status_code == 200

    resp = await client.post(
        f"/api/admin/projects/{project_id}/directions/{direction_id}/research"
    )
    assert resp.status_code == 429
    assert "cooldown" in resp.json()["error"]["message"].lower()


@pytest.mark.asyncio
async def test_research_endpoint_bypass_cooldown_creates_task(client: AsyncClient, mocker):
    """POST /research?bypass_cooldown=true creates a QUEUED task even if in cooldown."""
    project_id = await _deploy_project(client, mocker)
    direction = await _add_direction(client, project_id, text="Cooldown bypass direction")
    direction_id = direction["id"]

    # Put it in cooldown
    recent_ts = (datetime.utcnow() - timedelta(hours=1)).isoformat()
    await client.put(
        f"/api/admin/projects/{project_id}/directions",
        json=[{**direction, "last_researched_at": recent_ts}],
    )

    resp = await client.post(
        f"/api/admin/projects/{project_id}/directions/{direction_id}/research"
        "?bypass_cooldown=true"
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "QUEUED"
    assert "id" in data


@pytest.mark.asyncio
async def test_research_endpoint_no_cooldown_creates_task(client: AsyncClient, mocker):
    """POST /research for a direction with no last_researched_at creates a task immediately."""
    project_id = await _deploy_project(client, mocker)
    direction = await _add_direction(client, project_id, text="Fresh direction")
    direction_id = direction["id"]

    resp = await client.post(
        f"/api/admin/projects/{project_id}/directions/{direction_id}/research"
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "QUEUED"
    assert data["title"].startswith("Research:")


@pytest.mark.asyncio
async def test_research_endpoint_direction_not_found_returns_404(
    client: AsyncClient, mocker
):
    """POST /research for a nonexistent direction returns 404."""
    project_id = await _deploy_project(client, mocker)

    resp = await client.post(
        f"/api/admin/projects/{project_id}/directions/d-notexist/research"
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_research_endpoint_project_not_found_returns_404(client: AsyncClient):
    """POST /research for a nonexistent project returns 404."""
    resp = await client.post(
        f"/api/admin/projects/{uuid.uuid4()}/directions/d-any/research"
    )
    assert resp.status_code == 404
