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
from app.models import Alert, AlertSeverity, MessageType, Project, ProjectMessage, Task, TaskStatus
from app.autonomous_research import _spawn_research_task
from app.research_completion import _handle_research_completion
from app.direction_outcomes import _update_direction_research_outcomes
from app.max_plan_pool import _max_plan_pools, _account_by_id


@pytest.fixture(autouse=True)
def _ensure_test_account_pool():
    """Ensure _spawn_research_task finds an available account for the test user.
    Also clears the task_results ring buffer so the _assess_spawn_health failure-rate
    gate doesn't trigger from results left by other test modules."""
    uid = "test-user-123"
    entry = {"id": "test-acct", "user_id": uid, "name": "test", "enabled": True, "active_tasks": 0,
             "oauth_token": "sk-ant-test", "total_tasks": 0, "token_created_at": None, "claude_config_json": None}
    _max_plan_pools.setdefault(uid, []).append(entry)
    _account_by_id["test-acct"] = entry
    _shared_state.deploy_monitoring["task_results"].clear()
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
    """POST a direction and return the created direction dict."""
    resp = await client.post(
        f"/api/admin/projects/{project_id}/directions",
        json={"text": text, "priority": priority, "paths": paths or []},
    )
    assert resp.status_code == 200
    return resp.json()["direction"]


def _patch_research_externals(mocker):
    """Patch all external calls needed by _handle_research_completion."""
    mocker.patch("app.research_completion._ensure_branch", new=AsyncMock(return_value=True))
    mocker.patch("app.broadcast.broadcast_to_project", new=AsyncMock(return_value=None))
    mocker.patch("app.research_completion._validate_plan_dependencies", return_value=True)
    mocker.patch("app.research_completion._log_file_overlaps", return_value=[])
    # Simulate no pool configured so capacity-aware batch cap doesn't constrain unit tests
    mocker.patch("app.max_plan_pool._all_accounts", return_value=[])


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
        user_id="test-user-123",
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
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[_active_direction("d-cbhalf01")],
        vision={"purpose": "test"},  # Non-empty vision so force_strategic doesn't override
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
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[],
        vision={"purpose": "test", "_strategic_failures": 3},  # At failure cap so force_strategic doesn't fire
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
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[
            {**_active_direction("d-paused01", "Paused A"), "status": "paused"},
            {**_active_direction("d-done0001", "Completed B"), "status": "completed"},
        ],
        vision={"purpose": "test", "_strategic_failures": 3},  # At failure cap so force_strategic doesn't fire
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
    """_spawn_research_task does not spawn beyond the parallel limit when one is already QUEUED."""
    from app.shared_state import runtime_config as _runtime_config
    old_val = _runtime_config.get("max_parallel_research_per_project", 2)
    _runtime_config["max_parallel_research_per_project"] = 1
    try:
        project = Project(
            name="Already Running Research",
            user_id="test-user-123",
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
    finally:
        _runtime_config["max_parallel_research_per_project"] = old_val


@pytest.mark.asyncio
async def test_spawn_skipped_when_research_task_in_progress(db_session):
    """_spawn_research_task does not spawn beyond the parallel limit when a research task is IN_PROGRESS."""
    from app.shared_state import runtime_config as _runtime_config
    old_val = _runtime_config.get("max_parallel_research_per_project", 2)
    _runtime_config["max_parallel_research_per_project"] = 1
    try:
        project = Project(
            name="In-Progress Research",
            user_id="test-user-123",
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
    finally:
        _runtime_config["max_parallel_research_per_project"] = old_val


# ─────────────────────────────────────────────────────────────────────────────
# 4. _spawn_research_task — Rate Limits
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_spawn_skipped_at_per_project_rate_limit(db_session):
    """_spawn_research_task does not spawn when the per-project hourly limit is saturated."""
    project = Project(
        name="Per-Project Rate Limit",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[_active_direction("d-ratelm01")],
        vision={"purpose": "test"},
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
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[_active_direction("d-globrl01")],
        vision={"purpose": "test"},
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
        name="Depth Limit", user_id="test-user-123", autonomous_enabled=True, directions=[direction]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # depth=3 is at the limit — no children should be spawned
    task = await _make_research_task(project, direction, db_session, depth=3)

    mocker.patch(
        "app.research_completion._parse_research_findings",
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
        name="Hash Dedup Test", user_id="test-user-123", autonomous_enabled=True, directions=[direction]
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
        "app.research_completion._parse_research_findings",
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
    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED, "Research task must be auto-completed when all findings are duplicates"


@pytest.mark.asyncio
async def test_word_overlap_deduplication_skips_similar_title(db_session, mocker):
    """A finding with 4+ meaningful words overlapping an active task title is skipped."""
    direction = _active_direction("d-worddup1", "Word overlap direction")
    project = Project(
        name="Word Overlap Dedup", user_id="test-user-123", autonomous_enabled=True, directions=[direction]
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
        "app.research_completion._parse_research_findings",
        return_value=[{
            # "database", "connection", "pooling", "configuration" overlap existing → ≥4 words
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
    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED, "Research task must be auto-completed when all findings are duplicates"


@pytest.mark.asyncio
async def test_word_overlap_deduplication_allows_common_word_findings(db_session, mocker):
    """Findings sharing only common stopwords/prepositions are NOT falsely deduplicated."""
    direction = _active_direction("d-worddup2", "Stopword false-positive direction")
    project = Project(
        name="Stopword False Positive Test", user_id="test-user-123", autonomous_enabled=True, directions=[direction]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Active task whose title shares only stopwords with the new finding
    existing = Task(
        project_id=project.id,
        title="inject context into agent prompt for better results",
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
        "app.research_completion._parse_research_findings",
        return_value=[{
            # Shares only stopwords ("inject", "into", "for") with existing — should NOT be deduplicated
            "title": "inject rate limiting into api gateway for security",
            "description": "Add rate limiting to protect the API",
            "prompt": "Add rate limiting middleware",
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
    child_tasks = result.scalars().all()
    assert len(child_tasks) == 1, "Finding with only stopword overlap must NOT be deduplicated"


@pytest.mark.asyncio
async def test_cross_direction_word_overlap_not_deduplicated(db_session, mocker):
    """A finding with 4-5 word overlap against a task from a DIFFERENT direction must NOT be deduped."""
    dir_a = _active_direction("d-cross-a01", "Direction A: database performance")
    dir_b = _active_direction("d-cross-b01", "Direction B: query optimization")
    project = Project(
        name="Cross Direction Dedup Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[dir_a, dir_b],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Active task belonging to direction A
    existing = Task(
        project_id=project.id,
        title="optimize database query execution performance plan",
        payloads=[{"type": "create", "prompt": "...", "base_branch": "main"}],
        context={"direction_id": "d-cross-a01"},
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(existing)
    await db_session.commit()

    # Research task belongs to direction B
    task = await _make_research_task(project, dir_b, db_session)

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[{
            # "optimize", "database", "query", "execution", "performance" overlap existing (5 words)
            # Same-direction threshold (4) would falsely dedup this; cross-direction threshold (6) must not
            "title": "optimize database query execution performance strategy",
            "description": "Improve query performance for direction B",
            "prompt": "Optimize query execution",
            "priority": "medium",
            "scope": "small",
            "risk": "low",
            "type": "improvement",
            "direction_id": "d-cross-b01",
        }],
    )

    await _handle_research_completion(task, "", db_session)

    result = await db_session.execute(
        select(Task).where(
            Task.project_id == project.id, Task.parent_task_id == task.id
        )
    )
    child_tasks = result.scalars().all()
    assert len(child_tasks) == 1, (
        "Cross-direction finding with 5-word overlap must NOT be falsely deduped against a "
        "task from a different direction"
    )


@pytest.mark.asyncio
async def test_all_findings_deduplicated_auto_completes_task(db_session, mocker):
    """When every finding is deduplicated (zero children), the research task is auto-completed."""
    direction = _active_direction("d-autocomp1", "Auto-complete direction")
    project = Project(
        name="Auto Complete Test", user_id="test-user-123", autonomous_enabled=True, directions=[direction]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    dup_title = "Refactor authentication middleware"
    dup_desc = "Middleware needs cleanup"
    fhash = hashlib.sha256((dup_title + dup_desc).encode()).hexdigest()[:16]

    # Existing task with matching hash — causes deduplication
    existing = Task(
        project_id=project.id,
        title=dup_title,
        payloads=[{"type": "create", "prompt": "refactor it", "base_branch": "main"}],
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
        "app.research_completion._parse_research_findings",
        return_value=[{
            "title": dup_title,
            "description": dup_desc,
            "prompt": "Refactor auth middleware",
            "priority": "medium",
            "scope": "small",
            "risk": "low",
            "type": "improvement",
            "direction_id": direction["id"],
        }],
    )

    await _handle_research_completion(task, "", db_session)

    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED, (
        "Research task must be auto-completed when all findings are deduplicated"
    )

    result = await db_session.execute(
        select(Task).where(
            Task.project_id == project.id, Task.parent_task_id == task.id
        )
    )
    assert len(result.scalars().all()) == 0, "No child tasks should be created when all findings are duplicates"


@pytest.mark.asyncio
async def test_ghost_completion_does_not_block_rededup(db_session, mocker):
    """A COMPLETED task without pr_merged=True (ghost completion) must NOT block re-proposal."""
    direction = _active_direction("d-ghost01", "Ghost completion direction")
    project = Project(
        name="Ghost Dedup Test", user_id="test-user-123", autonomous_enabled=True, directions=[direction]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    dup_title = "Fix authentication token expiry handling"
    dup_desc = "Auth tokens expire without proper notification"
    fhash = hashlib.sha256((dup_title + dup_desc).encode()).hexdigest()[:16]

    # Ghost completion: COMPLETED but no pr_merged in context
    ghost = Task(
        project_id=project.id,
        title=dup_title,
        payloads=[{"type": "create", "prompt": "fix it", "base_branch": "main"}],
        context={"finding_hash": fhash},
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(ghost)
    await db_session.commit()

    task = await _make_research_task(project, direction, db_session)

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
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
    child_tasks = result.scalars().all()
    assert len(child_tasks) == 1, "Ghost completion must NOT block re-proposal — child task should be created"


@pytest.mark.asyncio
async def test_confirmed_merge_still_blocks_rededup(db_session, mocker):
    """A COMPLETED task WITH pr_merged=True must still block re-proposal."""
    direction = _active_direction("d-merged01", "Merged completion direction")
    project = Project(
        name="Merged Dedup Test", user_id="test-user-123", autonomous_enabled=True, directions=[direction]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    dup_title = "Fix authentication token expiry handling"
    dup_desc = "Auth tokens expire without proper notification"
    fhash = hashlib.sha256((dup_title + dup_desc).encode()).hexdigest()[:16]

    # Confirmed merge: COMPLETED with pr_merged=True
    merged = Task(
        project_id=project.id,
        title=dup_title,
        payloads=[{"type": "create", "prompt": "fix it", "base_branch": "main"}],
        context={"finding_hash": fhash, "pr_merged": True},
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(merged)
    await db_session.commit()

    task = await _make_research_task(project, direction, db_session)

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
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
    child_tasks = result.scalars().all()
    assert len(child_tasks) == 0, "Confirmed merge (pr_merged=True) must still block re-proposal"


@pytest.mark.asyncio
async def test_ghost_merge_does_not_block_rededup(db_session, mocker):
    """A COMPLETED+pr_merged task whose parent rollup failed must NOT block re-proposal."""
    direction = _active_direction("d-ghostmrg", "Ghost merge direction")
    project = Project(
        name="Ghost Merge Dedup Test", user_id="test-user-123", autonomous_enabled=True, directions=[direction]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    dup_title = "Fix authentication token expiry handling"
    dup_desc = "Auth tokens expire without proper notification"
    fhash = hashlib.sha256((dup_title + dup_desc).encode()).hexdigest()[:16]

    # Parent rollup task that FAILED (e.g. staging branch invalid)
    parent = Task(
        project_id=project.id,
        title="rollup: research batch",
        payloads=[{"type": "create", "prompt": "rollup", "base_branch": "main"}],
        context={},
        status=TaskStatus.FAILED,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(parent)
    await db_session.commit()

    # Ghost merge: child is COMPLETED+pr_merged=True but its parent rollup failed
    ghost_merge = Task(
        project_id=project.id,
        title=dup_title,
        payloads=[{"type": "create", "prompt": "fix it", "base_branch": "main"}],
        context={"finding_hash": fhash, "pr_merged": True},
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
        parent_task_id=parent.id,
    )
    db_session.add(ghost_merge)
    await db_session.commit()

    task = await _make_research_task(project, direction, db_session)

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
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
    child_tasks = result.scalars().all()
    assert len(child_tasks) == 1, "Ghost merge (failed rollup parent) must NOT block re-proposal"


@pytest.mark.asyncio
async def test_infra_failure_does_not_block_rededup(db_session, mocker):
    """A FAILED task due to infrastructure issues must NOT block re-proposal."""
    direction = _active_direction("d-infrafail", "Infra failure direction")
    project = Project(
        name="Infra Failure Dedup Test", user_id="test-user-123", autonomous_enabled=True, directions=[direction]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    infra_title = "Add rate limiting to API endpoints"
    infra_desc = "API endpoints lack rate limiting protection"
    infra_hash = hashlib.sha256((infra_title + infra_desc).encode()).hexdigest()[:16]

    # FAILED task due to AGENT_TIMEOUT (no heartbeat = transient infra issue)
    infra_failed = Task(
        project_id=project.id,
        title=infra_title,
        payloads=[{"type": "create", "prompt": "add rate limiting", "base_branch": "main"}],
        context={"finding_hash": infra_hash},
        status=TaskStatus.FAILED,
        latest_status_message="Reaped (attempt 1/3): No heartbeat for 314s",
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(infra_failed)
    await db_session.commit()

    task = await _make_research_task(project, direction, db_session)

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[{
            "title": infra_title,
            "description": infra_desc,
            "prompt": "Add rate limiting",
            "priority": "high",
            "scope": "small",
            "risk": "medium",
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
    child_tasks = result.scalars().all()
    assert len(child_tasks) == 1, "Infrastructure failure must NOT block re-proposal"


@pytest.mark.asyncio
async def test_code_error_failure_still_blocks_rededup(db_session, mocker):
    """A FAILED task due to a code error must still block re-proposal."""
    direction = _active_direction("d-codefail", "Code failure direction")
    project = Project(
        name="Code Failure Dedup Test", user_id="test-user-123", autonomous_enabled=True, directions=[direction]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    code_title = "Refactor database connection pooling"
    code_desc = "Connection pool is not properly configured"
    code_hash = hashlib.sha256((code_title + code_desc).encode()).hexdigest()[:16]

    # FAILED task due to a code error (non-infra failure)
    code_failed = Task(
        project_id=project.id,
        title=code_title,
        payloads=[{"type": "create", "prompt": "refactor pool", "base_branch": "main"}],
        context={"finding_hash": code_hash},
        status=TaskStatus.FAILED,
        latest_status_message="Tests failed: AssertionError in test_db_pool.py line 42",
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(code_failed)
    await db_session.commit()

    task = await _make_research_task(project, direction, db_session)

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[{
            "title": code_title,
            "description": code_desc,
            "prompt": "Refactor connection pool",
            "priority": "medium",
            "scope": "small",
            "risk": "medium",
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
    child_tasks = result.scalars().all()
    assert len(child_tasks) == 0, "Code error failure must still block re-proposal"


@pytest.mark.asyncio
async def test_unique_findings_are_not_deduplicated(db_session, mocker):
    """Distinct findings that share fewer than 3 meaningful words each get a child task."""
    direction = _active_direction("d-unique01", "Unique findings direction")
    project = Project(
        name="Unique Findings", user_id="test-user-123", autonomous_enabled=True, directions=[direction]
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
        "app.research_completion._parse_research_findings",
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
        name="Child Context Test", user_id="test-user-123", autonomous_enabled=True, directions=[direction]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_research_task(project, direction, db_session)

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
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
        name="Parent Task ID Test", user_id="test-user-123", autonomous_enabled=True, directions=[direction]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_research_task(project, direction, db_session)

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
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
        name="EVALUATING Status Test", user_id="test-user-123", autonomous_enabled=True, directions=[direction]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_research_task(project, direction, db_session)

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
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
        name="Multiple Findings", user_id="test-user-123", autonomous_enabled=True, directions=[direction]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_research_task(project, direction, db_session)

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
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
        name="Depth Increment Test", user_id="test-user-123", autonomous_enabled=True, directions=[direction]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Parent at depth 1
    task = await _make_research_task(project, direction, db_session, depth=1)

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
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
        name="Sub-Research Test", user_id="test-user-123", autonomous_enabled=True, directions=[direction]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_research_task(project, direction, db_session)

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
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
        name="Large Scope Plan Test", user_id="test-user-123", autonomous_enabled=True, directions=[direction]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_research_task(project, direction, db_session)

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
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
        name="Small Scope Create Test", user_id="test-user-123", autonomous_enabled=True, directions=[direction]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_research_task(project, direction, db_session)

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
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
        name="Security Audit Test", user_id="test-user-123", autonomous_enabled=True, directions=[direction]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_research_task(project, direction, db_session)

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
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
    """research_history gets a new entry with timestamp, task_id, tasks_created, summary."""
    direction_id = "d-history1"
    direction = {
        **_active_direction(direction_id, "History fields direction"),
        "research_history": [],
    }
    project = Project(
        name="History Fields Test", user_id="test-user-123", autonomous_enabled=True, directions=[direction]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_research_task(project, direction, db_session)
    finding_title = "Add structured logging to all service methods"

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
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
    assert finding_title[:60] in entry.get("summary", "")
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
        name="History Cap Test", user_id="test-user-123", autonomous_enabled=True, directions=[direction]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_research_task(project, direction, db_session)

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
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
        name="Selective History Test", user_id="test-user-123", autonomous_enabled=True, directions=directions
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_research_task(project, directions[0], db_session)

    _patch_research_externals(mocker)
    # Only direction A produces a finding
    mocker.patch(
        "app.research_completion._parse_research_findings",
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
        name="Proposal Happy Path", user_id="test-user-123", autonomous_enabled=True, directions=[direction]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_research_task(project, direction, db_session)
    proposal_prompt = "Investigate database index improvements"
    proposal_rationale = "Query times are increasing under load"

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
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
        name="Proposal No Task", user_id="test-user-123", autonomous_enabled=True, directions=[direction]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_research_task(project, direction, db_session)

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
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
        name="Proposal Per-Call Cap", user_id="test-user-123", autonomous_enabled=True, directions=[direction]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_research_task(project, direction, db_session)

    proposals = [_make_direction_proposal(f"Proposal {i}") for i in range(8)]
    _patch_research_externals(mocker)
    mocker.patch("app.research_completion._parse_research_findings", return_value=proposals)

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
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction] + existing_proposed,
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_research_task(project, direction, db_session)

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
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


# ─────────────────────────────────────────────────────────────────────────────
# research_context accumulation
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_research_context_accumulates_across_cycles(db_session, mocker):
    """research_context compounds across cycles, accumulating finding titles."""
    direction_id = "d-ctx0001"
    direction = _active_direction(direction_id, "Accumulation test")
    project = Project(
        name="Context Accumulate",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    _patch_research_externals(mocker)
    mocker.patch("app.research_completion._parse_research_insights", return_value=[])
    mocker.patch(
        "app.research_completion._parse_research_findings",
        side_effect=[
            [_make_finding(title="Finding Alpha", direction_id=direction_id)],
            [_make_finding(title="Finding Beta", direction_id=direction_id)],
            [_make_finding(title="Finding Gamma", direction_id=direction_id)],
        ],
    )

    expected_titles = ["Finding Alpha", "Finding Beta", "Finding Gamma"]
    for i in range(3):
        await db_session.refresh(project)
        dirs = project.directions or []
        current_dir = next((x for x in dirs if x.get("id") == direction_id), direction)
        task = await _make_research_task(project, current_dir, db_session)
        await _handle_research_completion(task, "", db_session)

        await db_session.refresh(project)
        dirs = project.directions or []
        d = next((x for x in dirs if x.get("id") == direction_id), None)
        assert d is not None, f"Direction not found after cycle {i + 1}"
        ctx = d.get("research_context", "")
        assert ctx, f"research_context should exist after cycle {i + 1}"

        # All findings up to this cycle should be present (with scope tag)
        for title in expected_titles[: i + 1]:
            assert f"{title} [small] (proposed)" in ctx, (
                f"'{title} [small] (proposed)' should be in research_context after cycle {i + 1}"
            )

    # Final assertions after all 3 cycles
    assert "## Established Findings" in ctx
    assert "Finding Alpha [small] (proposed)" in ctx
    assert "Finding Beta [small] (proposed)" in ctx
    assert "Finding Gamma [small] (proposed)" in ctx


@pytest.mark.asyncio
async def test_research_context_truncated_under_8000_chars(db_session, mocker):
    """research_context stays under 8000 chars even with many findings."""
    direction_id = "d-ctx0002"
    direction = _active_direction(direction_id, "Truncation test")
    project = Project(
        name="Context Truncation",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    _patch_research_externals(mocker)
    mocker.patch("app.research_completion._parse_research_insights", return_value=[])

    # Build findings with long descriptions to generate large research_context entries
    findings_per_cycle = []
    for cycle in range(6):
        cycle_findings = []
        for i in range(5):
            title = f"unique-{cycle}-{i}"
            cycle_findings.append({
                "title": title,
                "description": f"Long description for {title}: " + "x" * 150,
                "prompt": f"Implement: {title}",
                "priority": "medium",
                "scope": "small",
                "risk": "low",
                "type": "improvement",
                "direction_id": direction_id,
            })
        findings_per_cycle.append(cycle_findings)

    mocker.patch("app.research_completion._parse_research_findings", side_effect=findings_per_cycle)

    for i in range(6):
        await db_session.refresh(project)
        dirs = project.directions or []
        current_dir = next((x for x in dirs if x.get("id") == direction_id), direction)
        task = await _make_research_task(project, current_dir, db_session)
        await _handle_research_completion(task, "", db_session)

    await db_session.refresh(project)
    dirs = project.directions or []
    d = next((x for x in dirs if x.get("id") == direction_id), None)
    assert d is not None
    ctx = d.get("research_context", "")
    assert ctx, "research_context should exist after 6 cycles"
    assert len(ctx) <= 8000, f"research_context should be ≤8000 chars, got {len(ctx)}"


@pytest.mark.asyncio
async def test_research_context_updates_on_task_failure(db_session, mocker):
    """Failed tasks move their proposed finding to Failed Approaches; merged PRs get confirmed."""
    direction_id = "d-ctx0003"
    initial_ctx = (
        "## Established Findings\n"
        "- Add retry logic to API client (proposed)\n"
        "- Improve caching layer (proposed)\n"
        "\n"
        "## Failed Approaches\n"
        "\n"
        "## Open Questions\n"
        "- How many retries are optimal?\n"
    )

    # Create child tasks directly so we control their titles and statuses
    fail_task = Task(
        project_id=uuid.uuid4(),  # placeholder, overridden after project creation
        title="Add retry logic to API client",
        payloads=[{"type": "create", "prompt": "add retry logic"}],
        status=TaskStatus.FAILED,
        latest_status_message="Timed out after 30 minutes",
        created_by="ORCHESTRATOR",
    )
    merge_task = Task(
        project_id=uuid.uuid4(),  # placeholder
        title="Improve caching layer",
        payloads=[{"type": "create", "prompt": "improve caching"}],
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        created_by="ORCHESTRATOR",
    )

    direction = _active_direction(direction_id, "Outcome update test")
    direction["research_context"] = initial_ctx

    project = Project(
        name="Context Outcome Update",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Fix the project_id on child tasks now that we have it
    fail_task.project_id = project.id
    merge_task.project_id = project.id
    db_session.add(fail_task)
    db_session.add(merge_task)
    await db_session.commit()
    await db_session.refresh(fail_task)
    await db_session.refresh(merge_task)

    # Create a research task and manually add a history entry (compact format, no child_task_ids)
    research_task = Task(
        project_id=project.id,
        title="Research: Outcome update test",
        payloads=[{"type": "research", "directions": [direction], "base_branch": "main"}],
        context={"research_depth": 0},
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
    )
    db_session.add(research_task)
    await db_session.commit()
    await db_session.refresh(research_task)

    # Set parent_task_id on child tasks so they can be found by parent_task_id query
    fail_task.parent_task_id = research_task.id
    merge_task.parent_task_id = research_task.id
    db_session.add(fail_task)
    db_session.add(merge_task)

    # Inject research_history entry without child_task_ids (compact format)
    dirs = list(project.directions or [])
    for d in dirs:
        if d.get("id") == direction_id:
            d["research_history"] = [{
                "timestamp": "2026-04-07T00:00:00",
                "task_id": str(research_task.id),
                "tasks_created": 2,
                "finding_titles": ["Add retry logic to API client", "Improve caching layer"],
            }]
            break
    project.directions = dirs
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(project, "directions")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(fail_task)
    await db_session.refresh(merge_task)

    # Trigger outcome update
    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=str(research_task.id),
        session=db_session,
    )

    await db_session.refresh(project)
    dirs = project.directions or []
    d = next(x for x in dirs if x.get("id") == direction_id)
    ctx = d.get("research_context", "")

    # Failed task's finding should be moved to Failed Approaches
    assert "## Failed Approaches" in ctx
    assert "Add retry logic to API client" in ctx
    assert "Timed out after 30 minutes" in ctx, "Failure reason should appear in Failed Approaches"
    # Should no longer be in Established as (proposed)
    assert "Add retry logic to API client (proposed)" not in ctx

    # Merged task's finding should be promoted from (proposed) to (confirmed)
    assert "Improve caching layer (confirmed)" in ctx
    assert "Improve caching layer (proposed)" not in ctx


@pytest.mark.asyncio
async def test_type_distribution_and_product_ratio(db_session):
    """type_distribution and product_ratio are correctly populated in the history entry."""
    direction_id = "d-typedist01"
    direction = _active_direction(direction_id, "Type distribution test")

    project = Project(
        name="Type Distribution Project",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Create child tasks: 2 product types (create), 2 structural (fix, refactor)
    create_task1 = Task(
        project_id=project.id,
        title="Add dashboard widget",
        payloads=[{"type": "create", "prompt": "add widget"}],
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        created_by="ORCHESTRATOR",
    )
    create_task2 = Task(
        project_id=project.id,
        title="Add export feature",
        payloads=[{"type": "create", "prompt": "add export"}],
        status=TaskStatus.COMPLETED,
        context={"pr_merged": False},
        created_by="ORCHESTRATOR",
    )
    fix_task = Task(
        project_id=project.id,
        title="Fix null pointer in parser",
        payloads=[{"type": "fix", "prompt": "fix null"}],
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        created_by="ORCHESTRATOR",
    )
    refactor_task = Task(
        project_id=project.id,
        title="Refactor database layer",
        payloads=[{"type": "refactor", "prompt": "refactor db"}],
        status=TaskStatus.FAILED,
        latest_status_message="Build failed",
        created_by="ORCHESTRATOR",
    )
    for t in (create_task1, create_task2, fix_task, refactor_task):
        db_session.add(t)
    await db_session.commit()
    for t in (create_task1, create_task2, fix_task, refactor_task):
        await db_session.refresh(t)

    # Create a research task so we have a real UUID for parent_task_id lookup
    research_task = Task(
        project_id=project.id,
        title="Research: Type distribution test",
        payloads=[{"type": "research", "directions": [direction], "base_branch": "main"}],
        context={"research_depth": 0},
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
    )
    db_session.add(research_task)
    await db_session.commit()
    await db_session.refresh(research_task)

    # Link child tasks to the research task via parent_task_id
    for t in (create_task1, create_task2, fix_task, refactor_task):
        t.parent_task_id = research_task.id
        db_session.add(t)
    await db_session.commit()

    # Inject research_history entry using the real research task UUID
    dirs = list(project.directions or [])
    for d in dirs:
        if d.get("id") == direction_id:
            d["research_history"] = [{
                "timestamp": "2026-04-07T00:00:00",
                "task_id": str(research_task.id),
                "tasks_created": 4,
                "finding_titles": [],
            }]
            break
    project.directions = dirs
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(project, "directions")
    db_session.add(project)
    await db_session.commit()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=str(research_task.id),
        session=db_session,
    )

    await db_session.refresh(project)
    dirs = project.directions or []
    d = next(x for x in dirs if x.get("id") == direction_id)
    entry = d["research_history"][0]

    # type_distribution should count all 4 tasks by type
    type_dist = entry.get("type_distribution", {})
    assert type_dist.get("create") == 2, f"Expected 2 create tasks, got {type_dist}"
    assert type_dist.get("fix") == 1, f"Expected 1 fix task, got {type_dist}"
    assert type_dist.get("refactor") == 1, f"Expected 1 refactor task, got {type_dist}"

    # product_ratio: 2 product (create) out of 4 total = 0.5
    product_ratio = entry.get("product_ratio")
    assert product_ratio == 0.5, f"Expected product_ratio=0.5, got {product_ratio}"


@pytest.mark.asyncio
async def test_stage_dropout_breakdown(db_session):
    """stage_dropout is populated with per-category failure counts."""
    direction_id = "d-stagedrop01"
    direction = _active_direction(direction_id, "Stage dropout test")

    project = Project(
        name="Stage Dropout Project",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # code_error_task: FAILED with CODE_ERROR message
    code_error_task = Task(
        project_id=project.id,
        title="Fix broken parser",
        payloads=[{"type": "fix", "prompt": "fix parser"}],
        status=TaskStatus.FAILED,
        latest_status_message="Build failed: syntax error in main.py",
        created_by="ORCHESTRATOR",
    )
    # review_capped_task: requires_approval=True (hit iteration cap)
    review_capped_task = Task(
        project_id=project.id,
        title="Add feature flag",
        payloads=[{"type": "create", "prompt": "add flag"}],
        status=TaskStatus.FAILED,
        latest_status_message="Review-fix cap reached for PR #42 (10 total iterations)",
        requires_approval=True,
        created_by="ORCHESTRATOR",
    )
    # merged_task: COMPLETED with pr_merged=True
    merged_task = Task(
        project_id=project.id,
        title="Add caching layer",
        payloads=[{"type": "create", "prompt": "add cache"}],
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        created_by="ORCHESTRATOR",
    )
    for t in (code_error_task, review_capped_task, merged_task):
        db_session.add(t)
    await db_session.commit()
    for t in (code_error_task, review_capped_task, merged_task):
        await db_session.refresh(t)

    # Research task (parent) in COMPLETED state — rollup succeeded
    research_task = Task(
        project_id=project.id,
        title="Research: Stage dropout test",
        payloads=[{"type": "research", "directions": [direction], "base_branch": "main"}],
        context={"research_depth": 0},
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
    )
    db_session.add(research_task)
    await db_session.commit()
    await db_session.refresh(research_task)

    for t in (code_error_task, review_capped_task, merged_task):
        t.parent_task_id = research_task.id
        db_session.add(t)
    await db_session.commit()

    dirs = list(project.directions or [])
    for d in dirs:
        if d.get("id") == direction_id:
            d["research_history"] = [{
                "timestamp": "2026-04-07T00:00:00",
                "task_id": str(research_task.id),
                "tasks_created": 3,
                "finding_titles": [],
            }]
            break
    project.directions = dirs
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(project, "directions")
    db_session.add(project)
    await db_session.commit()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=str(research_task.id),
        session=db_session,
    )

    await db_session.refresh(project)
    dirs = project.directions or []
    d = next(x for x in dirs if x.get("id") == direction_id)
    entry = d["research_history"][0]
    dropout = entry.get("stage_dropout", {})

    assert dropout.get("tasks_failed_code_error") == 1, f"Expected 1 code error, got {dropout}"
    assert dropout.get("tasks_required_approval") == 1, f"Expected 1 required approval, got {dropout}"
    # rollup succeeded (research task COMPLETED) — no staging-only merges
    assert dropout.get("tasks_merged_staging_only") == 0, f"Expected 0 staging-only, got {dropout}"


# ─────────────────────────────────────────────────────────────────────────────
# _strategic_failures auto-decay
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_strategic_failures_auto_decay_triggers_force_strategic(db_session):
    """When _strategic_failures >= 3 but last failure was >48h ago, force_strategic fires."""
    old_timestamp = (datetime.utcnow() - timedelta(hours=72)).isoformat()
    project = Project(
        name="Decay Test Project",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[],  # No active directions — needs force_strategic to spawn anything
        vision={
            "purpose": "test purpose",
            "_strategic_failures": 5,
            "_strategic_failures_at": old_timestamp,
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _spawn_research_task(project, db_session)

    # force_strategic should have fired — a strategic task should be created.
    # Depending on the cycle count, this is either design_doc_update (architecture
    # cycle) or strategic_review (strategy-only cycle).
    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    tasks = result.scalars().all()
    assert len(tasks) == 1, "Expected one strategic task to be spawned after decay"
    assert tasks[0].payloads[0].get("type") in ("design_doc_update", "strategic_review")


@pytest.mark.asyncio
async def test_strategic_failures_no_decay_when_recent(db_session):
    """When _strategic_failures >= 3 and last failure was recent (<48h), force_strategic stays blocked."""
    recent_timestamp = (datetime.utcnow() - timedelta(hours=12)).isoformat()
    project = Project(
        name="No Decay Test Project",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[],
        vision={
            "purpose": "test purpose",
            "_strategic_failures": 5,
            "_strategic_failures_at": recent_timestamp,
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _spawn_research_task(project, db_session)

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    tasks = result.scalars().all()
    assert len(tasks) == 0, "Expected no task: decay should not apply for recent failure"


@pytest.mark.asyncio
async def test_all_saturated_directions_triggers_force_strategic(db_session):
    """When all active directions are saturated, force_strategic fires regardless of spawn count."""
    project = Project(
        name="All Saturated Test Project",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[
            {"id": "d-111", "status": "active", "possibly_saturated": True, "title": "Dir 1"},
            {"id": "d-222", "status": "active", "possibly_saturated": True, "title": "Dir 2"},
            {"id": "d-333", "status": "active", "possibly_saturated": True, "title": "Dir 3"},
        ],
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _spawn_research_task(project, db_session)

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    tasks = result.scalars().all()
    task_types = [t.payloads[0].get("type") for t in tasks]
    strategic_types = {"design_doc_update", "strategic_review"}
    assert any(t in strategic_types for t in task_types), "Expected a strategic task when all directions are saturated"
    assert "research" not in task_types, "Expected no research tasks when all directions are saturated"


@pytest.mark.asyncio
async def test_partially_saturated_directions_no_force_strategic(db_session):
    """When only some active directions are saturated, force_strategic does NOT fire."""
    project = Project(
        name="Partial Saturated Test Project",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[
            {"id": "d-111", "status": "active", "possibly_saturated": True, "title": "Dir 1"},
            {"id": "d-222", "status": "active", "possibly_saturated": False, "title": "Dir 2"},
        ],
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _spawn_research_task(project, db_session)

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    tasks = result.scalars().all()
    # A research task for the non-saturated direction should spawn, not a strategic task
    strategic_tasks = [t for t in tasks if t.payloads[0].get("type") == "design_doc_update"]
    assert len(strategic_tasks) == 0, "Expected no strategic task when only some directions are saturated"


# ─────────────────────────────────────────────────────────────────────────────
# Dedup hole tests — failed parent and infra failures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_failed_parent_child_not_blocking_dedup(db_session, mocker):
    """A COMPLETED child with pr_merged=True whose parent rollup FAILED must NOT block re-proposal."""
    direction = _active_direction("d-fpdedup01", "Failed parent dedup test")
    project = Project(
        name="Failed Parent Dedup Test", user_id="test-user-123", autonomous_enabled=True, directions=[direction]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    dup_title = "Fix caching layer for session tokens"
    dup_desc = "Session tokens not cached properly causing re-auth on every request"
    fhash = hashlib.sha256((dup_title + dup_desc).encode()).hexdigest()[:16]

    # Parent rollup task that failed
    failed_parent = Task(
        project_id=project.id,
        title="Rollup PR for batch-7",
        payloads=[{"type": "create", "prompt": "rollup", "base_branch": "main"}],
        context={},
        status=TaskStatus.FAILED,
        latest_status_message="Rollup PR creation failed: staging branch invalid",
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(failed_parent)
    await db_session.commit()
    await db_session.refresh(failed_parent)

    # Child task: COMPLETED+pr_merged but parent failed, so code never reached main
    ghost_child = Task(
        project_id=project.id,
        title=dup_title,
        payloads=[{"type": "create", "prompt": "fix it", "base_branch": "main"}],
        context={"finding_hash": fhash, "pr_merged": True},
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        parent_task_id=failed_parent.id,
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(ghost_child)
    await db_session.commit()

    task = await _make_research_task(project, direction, db_session)

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[{
            "title": dup_title,
            "description": dup_desc,
            "prompt": "Fix session token caching",
            "priority": "high",
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
    child_tasks = result.scalars().all()
    assert len(child_tasks) == 1, "Ghost child (failed parent) must NOT block re-proposal"


@pytest.mark.asyncio
async def test_infra_failure_not_blocking_dedup(db_session, mocker):
    """A FAILED task with AGENT_TIMEOUT classification must NOT block re-proposal."""
    direction = _active_direction("d-infraded01", "Infra failure dedup test")
    project = Project(
        name="Infra Failure Dedup Test", user_id="test-user-123", autonomous_enabled=True, directions=[direction]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    dup_title = "Add retry logic to payment processor"
    dup_desc = "Payment processor calls fail with no retry on transient errors"
    fhash = hashlib.sha256((dup_title + dup_desc).encode()).hexdigest()[:16]

    # Infra failure — agent timeout (no heartbeat message triggers AGENT_TIMEOUT)
    infra_failed = Task(
        project_id=project.id,
        title=dup_title,
        payloads=[{"type": "create", "prompt": "fix it", "base_branch": "main"}],
        context={"finding_hash": fhash},
        status=TaskStatus.FAILED,
        latest_status_message="Reaped: No heartbeat for 320s (last: 2026-04-13T08:10:00)",
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(infra_failed)
    await db_session.commit()

    task = await _make_research_task(project, direction, db_session)

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[{
            "title": dup_title,
            "description": dup_desc,
            "prompt": "Add retry logic",
            "priority": "high",
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
    child_tasks = result.scalars().all()
    assert len(child_tasks) == 1, "Infra-failed task must NOT block re-proposal"


@pytest.mark.asyncio
async def test_code_error_still_blocks_dedup(db_session, mocker):
    """A FAILED task with CODE_ERROR classification must still block re-proposal."""
    direction = _active_direction("d-codedeup01", "Code error dedup test")
    project = Project(
        name="Code Error Dedup Test", user_id="test-user-123", autonomous_enabled=True, directions=[direction]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    dup_title = "Refactor authentication middleware"
    dup_desc = "Middleware has circular dependency causing startup failures"
    fhash = hashlib.sha256((dup_title + dup_desc).encode()).hexdigest()[:16]

    # Code error failure — should remain in dedup set
    code_failed = Task(
        project_id=project.id,
        title=dup_title,
        payloads=[{"type": "create", "prompt": "fix it", "base_branch": "main"}],
        context={"finding_hash": fhash},
        status=TaskStatus.FAILED,
        latest_status_message="Tests failed: ImportError in auth module",
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(code_failed)
    await db_session.commit()

    task = await _make_research_task(project, direction, db_session)

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[{
            "title": dup_title,
            "description": dup_desc,
            "prompt": "Refactor middleware",
            "priority": "high",
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
    child_tasks = result.scalars().all()
    assert len(child_tasks) == 0, "CODE_ERROR failure must still block re-proposal"


@pytest.mark.asyncio
async def test_code_error_after_cooldown_allows_reproposal(db_session, mocker):
    """A CODE_ERROR failure older than 48 hours must NOT block re-proposal."""
    direction = _active_direction("d-ceold01", "Code error cooldown test")
    project = Project(
        name="Code Error Cooldown Allow", user_id="test-user-123", autonomous_enabled=True, directions=[direction]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    old_title = "Add input validation to user registration"
    old_desc = "Registration endpoint missing email format validation"
    fhash = hashlib.sha256((old_title + old_desc).encode()).hexdigest()[:16]

    # CODE_ERROR failure older than 48h — should be excluded from dedup set
    old_code_failed = Task(
        project_id=project.id,
        title=old_title,
        payloads=[{"type": "create", "prompt": "add validation", "base_branch": "main"}],
        context={"finding_hash": fhash},
        status=TaskStatus.FAILED,
        latest_status_message="Tests failed: AssertionError in test_registration.py line 15",
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
        created_at=datetime.utcnow() - timedelta(hours=72),
    )
    db_session.add(old_code_failed)
    await db_session.commit()

    task = await _make_research_task(project, direction, db_session)

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[{
            "title": old_title,
            "description": old_desc,
            "prompt": "Add input validation",
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
    child_tasks = result.scalars().all()
    assert len(child_tasks) == 1, "CODE_ERROR failure older than 48h must NOT block re-proposal"


@pytest.mark.asyncio
async def test_code_error_within_cooldown_blocks_reproposal(db_session, mocker):
    """A CODE_ERROR failure within the 48-hour cooldown must still block re-proposal."""
    direction = _active_direction("d-cewarm01", "Code error within cooldown test")
    project = Project(
        name="Code Error Cooldown Block", user_id="test-user-123", autonomous_enabled=True, directions=[direction]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    recent_title = "Fix null pointer in order processing"
    recent_desc = "Order processor crashes on missing customer_id field"
    fhash = hashlib.sha256((recent_title + recent_desc).encode()).hexdigest()[:16]

    # CODE_ERROR failure within 48h — must remain in dedup set
    recent_code_failed = Task(
        project_id=project.id,
        title=recent_title,
        payloads=[{"type": "create", "prompt": "fix null pointer", "base_branch": "main"}],
        context={"finding_hash": fhash},
        status=TaskStatus.FAILED,
        latest_status_message="Build failed: NullPointerException in OrderProcessor.java:88",
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
        created_at=datetime.utcnow() - timedelta(hours=24),
    )
    db_session.add(recent_code_failed)
    await db_session.commit()

    task = await _make_research_task(project, direction, db_session)

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[{
            "title": recent_title,
            "description": recent_desc,
            "prompt": "Fix null pointer error",
            "priority": "high",
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
    child_tasks = result.scalars().all()
    assert len(child_tasks) == 0, "CODE_ERROR failure within 48h cooldown must still block re-proposal"


@pytest.mark.asyncio
async def test_merged_pr_blocks_reproposal_permanently(db_session, mocker):
    """A successfully merged PR must block re-proposal even after the cooldown period."""
    direction = _active_direction("d-mergedold01", "Merged PR permanent block test")
    project = Project(
        name="Merged PR Permanent Block", user_id="test-user-123", autonomous_enabled=True, directions=[direction]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    merged_title = "Migrate legacy auth to OAuth2"
    merged_desc = "Replace custom auth with OAuth2 provider integration"
    fhash = hashlib.sha256((merged_title + merged_desc).encode()).hexdigest()[:16]

    # COMPLETED+pr_merged task that is very old — must still block
    old_merged = Task(
        project_id=project.id,
        title=merged_title,
        payloads=[{"type": "create", "prompt": "migrate auth", "base_branch": "main"}],
        context={"finding_hash": fhash, "pr_merged": True},
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
        created_at=datetime.utcnow() - timedelta(days=30),
    )
    db_session.add(old_merged)
    await db_session.commit()

    task = await _make_research_task(project, direction, db_session)

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[{
            "title": merged_title,
            "description": merged_desc,
            "prompt": "Migrate auth to OAuth2",
            "priority": "high",
            "scope": "medium",
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
    child_tasks = result.scalars().all()
    assert len(child_tasks) == 0, "Successfully merged PR must block re-proposal permanently"


# ─────────────────────────────────────────────────────────────────────────────
# Deadlock Detection
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_dedup_deadlock_triggers_alert_after_3_consecutive_empty_cycles(db_session, mocker):
    """3 consecutive 0-task cycles driven by hash dedup creates a WARNING Alert and BUG_REPORT message."""
    direction_id = "d-deadlk01"
    direction = {
        **_active_direction(direction_id, "Fix performance bottlenecks"),
        # Pre-populate 2 prior empty cycles with non-zero hash_dedup counts
        "research_history": [
            {
                "timestamp": "2026-01-10T00:00:00",
                "task_id": "00000000-0000-0000-0000-000000000001",
                "tasks_created": 0,
                "summary": "no findings",
                "finding_titles": [],
                "key_finding_descriptions": [],
                "filter_breakdown": {
                    "hash_dedup": 3,
                    "word_overlap_dedup": 0,
                    "file_overlap_dedup": 0,
                    "type_filtered": 0,
                    "empty_prompt": 0,
                },
            },
            {
                "timestamp": "2026-01-11T00:00:00",
                "task_id": "00000000-0000-0000-0000-000000000002",
                "tasks_created": 0,
                "summary": "no findings",
                "finding_titles": [],
                "key_finding_descriptions": [],
                "filter_breakdown": {
                    "hash_dedup": 2,
                    "word_overlap_dedup": 0,
                    "file_overlap_dedup": 0,
                    "type_filtered": 0,
                    "empty_prompt": 0,
                },
            },
        ],
    }
    project = Project(
        name="Deadlock Detection Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Existing task whose hash will block the new finding (causes hash_dedup in 3rd cycle)
    dup_title = "Fix database connection leak in worker pool"
    dup_desc = "Connection leak causes OOM after sustained load"
    fhash = hashlib.sha256((dup_title + dup_desc).encode()).hexdigest()[:16]
    existing = Task(
        project_id=project.id,
        title=dup_title,
        payloads=[{"type": "create", "prompt": "fix it", "base_branch": "main"}],
        context={"finding_hash": fhash, "direction_id": direction_id},
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(existing)
    await db_session.commit()

    task = Task(
        project_id=project.id,
        title="Research: Fix performance bottlenecks",
        payloads=[{"type": "research", "directions": [direction], "base_branch": "main"}],
        context={"research_depth": 0, "direction_id": direction_id},
        status=TaskStatus.IN_PROGRESS,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[{
            "title": dup_title,
            "description": dup_desc,
            "prompt": "Fix the connection leak",
            "priority": "high",
            "scope": "small",
            "risk": "low",
            "type": "improvement",
            "direction_id": direction_id,
        }],
    )

    await _handle_research_completion(task, "", db_session)

    # A WARNING-severity Alert should have been created for the deadlock
    alerts = (await db_session.execute(
        select(Alert).where(Alert.project_id == project.id)
    )).scalars().all()
    deadlock_alerts = [
        a for a in alerts
        if a.severity == AlertSeverity.WARNING and "deadlock" in a.title.lower()
    ]
    assert len(deadlock_alerts) >= 1, "Expected a WARNING Alert for dedup deadlock"
    assert direction_id in deadlock_alerts[0].message

    # A BUG_REPORT ProjectMessage should also have been created
    messages = (await db_session.execute(
        select(ProjectMessage).where(ProjectMessage.project_id == project.id)
    )).scalars().all()
    bug_reports = [m for m in messages if m.message_type == MessageType.BUG_REPORT]
    assert len(bug_reports) >= 1, "Expected a BUG_REPORT ProjectMessage for dedup deadlock"
    assert bug_reports[0].context.get("source") == "dedup_deadlock"
    assert bug_reports[0].context.get("consecutive_empty_cycles") == 3


# ─────────────────────────────────────────────────────────────────────────────
# 15. _handle_research_completion — Spawn-Time Health Gate
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_health_gate_normal_no_cap(db_session, mocker):
    """When pipeline is healthy, all findings create tasks without any cap."""
    direction_id = "d-hgate001"
    direction = _active_direction(direction_id, "Health gate normal")
    project = Project(
        name="Health Gate Normal", user_id="test-user-123", autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_research_task(project, direction, db_session)

    _patch_research_externals(mocker)
    # 5 findings — all should spawn tasks when healthy
    findings = [_make_finding(f"Improvement {i}", direction_id) for i in range(5)]
    mocker.patch("app.research_completion._parse_research_findings", return_value=findings)
    # Healthy pipeline: zone available, no degradation
    mocker.patch("app.research_completion._assess_spawn_health", return_value=(False, None))

    await _handle_research_completion(task, "", db_session)

    result = await db_session.execute(
        select(Task).where(Task.project_id == project.id, Task.id != task.id)
    )
    child_tasks = result.scalars().all()
    assert len(child_tasks) == 5, "All 5 findings should spawn tasks when healthy"

    await db_session.refresh(task)
    assert "deferred_findings" not in (task.context or {}), "No deferral when healthy"


@pytest.mark.asyncio
async def test_health_gate_zone_exhaustion_caps_spawn(db_session, mocker):
    """When all zones are in cooldown, child task creation is capped at _DEGRADED_SPAWN_CAP."""
    from app.research_completion import _DEGRADED_SPAWN_CAP

    direction_id = "d-hgate002"
    direction = _active_direction(direction_id, "Zone exhaustion cap")
    project = Project(
        name="Zone Exhaustion", user_id="test-user-123", autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_research_task(project, direction, db_session)

    _patch_research_externals(mocker)
    # 6 findings — only _DEGRADED_SPAWN_CAP should be created
    findings = [_make_finding(f"Zone cap finding {i}", direction_id) for i in range(6)]
    mocker.patch("app.research_completion._parse_research_findings", return_value=findings)
    mocker.patch(
        "app.research_completion._assess_spawn_health",
        return_value=(True, "all zones in cooldown"),
    )

    await _handle_research_completion(task, "", db_session)

    result = await db_session.execute(
        select(Task).where(Task.project_id == project.id, Task.id != task.id)
    )
    child_tasks = result.scalars().all()
    assert len(child_tasks) == _DEGRADED_SPAWN_CAP, (
        f"Should spawn only {_DEGRADED_SPAWN_CAP} tasks when zones exhausted"
    )

    await db_session.refresh(task)
    deferred = (task.context or {}).get("deferred_findings", [])
    assert len(deferred) == 6 - _DEGRADED_SPAWN_CAP, (
        f"Remaining {6 - _DEGRADED_SPAWN_CAP} findings should be deferred"
    )


@pytest.mark.asyncio
async def test_health_gate_account_unavailable_caps_spawn(db_session, mocker):
    """When no accounts are available, child task creation is capped at _DEGRADED_SPAWN_CAP."""
    from app.research_completion import _DEGRADED_SPAWN_CAP

    direction_id = "d-hgate003"
    direction = _active_direction(direction_id, "Account unavailable cap")
    project = Project(
        name="Account Unavailable", user_id="test-user-123", autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_research_task(project, direction, db_session)

    _patch_research_externals(mocker)
    findings = [_make_finding(f"Account cap finding {i}", direction_id) for i in range(5)]
    mocker.patch("app.research_completion._parse_research_findings", return_value=findings)
    mocker.patch(
        "app.research_completion._assess_spawn_health",
        return_value=(True, "no accounts available"),
    )

    await _handle_research_completion(task, "", db_session)

    result = await db_session.execute(
        select(Task).where(Task.project_id == project.id, Task.id != task.id)
    )
    child_tasks = result.scalars().all()
    assert len(child_tasks) == _DEGRADED_SPAWN_CAP

    await db_session.refresh(task)
    deferred = (task.context or {}).get("deferred_findings", [])
    assert len(deferred) == 5 - _DEGRADED_SPAWN_CAP


@pytest.mark.asyncio
async def test_health_gate_deferred_findings_stored_in_context(db_session, mocker):
    """Deferred findings are stored in task.context['deferred_findings']."""
    from app.research_completion import _DEGRADED_SPAWN_CAP

    direction_id = "d-hgate004"
    direction = _active_direction(direction_id, "Deferred findings storage")
    project = Project(
        name="Deferred Storage Test", user_id="test-user-123", autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_research_task(project, direction, db_session)

    _patch_research_externals(mocker)
    findings = [
        _make_finding(f"Finding {i}", direction_id) for i in range(4)
    ]
    mocker.patch("app.research_completion._parse_research_findings", return_value=findings)
    mocker.patch(
        "app.research_completion._assess_spawn_health",
        return_value=(True, "high failure rate (8/10 recent tasks failed)"),
    )

    await _handle_research_completion(task, "", db_session)

    await db_session.refresh(task)
    ctx = task.context or {}
    deferred = ctx.get("deferred_findings", [])
    assert len(deferred) == 4 - _DEGRADED_SPAWN_CAP
    # Verify deferred findings preserve title
    deferred_titles = {f["title"] for f in deferred}
    assert all(t.startswith("Finding") for t in deferred_titles)


@pytest.mark.asyncio
async def test_health_gate_metadata_findings_bypass_cap(db_session, mocker):
    """Metadata finding types (design_doc_observation, direction_update) bypass the health gate."""
    from app.research_completion import _DEGRADED_SPAWN_CAP

    direction_id = "d-hgate005"
    direction = _active_direction(direction_id, "Metadata bypass test")
    project = Project(
        name="Metadata Bypass", user_id="test-user-123", autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_research_task(project, direction, db_session)

    _patch_research_externals(mocker)
    # 2 implementation findings + 2 metadata findings
    findings = [
        _make_finding("Impl finding 0", direction_id),
        _make_finding("Impl finding 1", direction_id),
        {
            "title": "Design doc observation",
            "description": "Some design note",
            "prompt": "",
            "priority": "low",
            "scope": "small",
            "risk": "low",
            "type": "design_doc_observation",
            "direction_id": direction_id,
            "section": "general",
        },
        {
            "title": "Direction update proposal",
            "description": "Update direction priority",
            "prompt": "Update direction",
            "priority": "low",
            "scope": "small",
            "risk": "low",
            "type": "direction_update",
            "direction_id": direction_id,
        },
    ]
    mocker.patch("app.research_completion._parse_research_findings", return_value=findings)
    # Pipeline is degraded — but metadata findings must still be processed
    mocker.patch(
        "app.research_completion._assess_spawn_health",
        return_value=(True, "all zones in cooldown"),
    )

    await _handle_research_completion(task, "", db_session)

    result = await db_session.execute(
        select(Task).where(Task.project_id == project.id, Task.id != task.id)
    )
    child_tasks = result.scalars().all()
    # Both impl findings are at or below the cap (cap=2), so both should be created
    assert len(child_tasks) == _DEGRADED_SPAWN_CAP, (
        "Implementation findings up to cap should spawn; metadata findings never create tasks"
    )

    await db_session.refresh(task)
    # No findings should be deferred (impl count == cap, metadata never deferred)
    deferred = (task.context or {}).get("deferred_findings", [])
    assert len(deferred) == 0, "No deferral when impl count exactly equals cap"


# ─────────────────────────────────────────────────────────────────────────────
# 16. _handle_research_completion — Prior Deferred Findings Recovery
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_prior_deferred_findings_processed_in_next_cycle(db_session, mocker):
    """Deferred findings from a previous cycle are prepended and spawned in the next cycle."""
    from app.research_completion import _DEGRADED_SPAWN_CAP

    direction_id = "d-deferred-recovery01"
    direction = _active_direction(direction_id, "Deferred recovery test")
    project = Project(
        name="Deferred Recovery", user_id="test-user-123", autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Create task with prior_deferred_findings injected (as autonomous_research.py would do)
    deferred_from_prev_cycle = [
        _make_finding("Deferred finding A", direction_id),
        _make_finding("Deferred finding B", direction_id),
    ]
    task = Task(
        project_id=project.id,
        title=f"Research: {direction['text']}",
        payloads=[{"type": "research", "directions": [direction], "base_branch": "main"}],
        context={"research_depth": 0, "prior_deferred_findings": deferred_from_prev_cycle},
        status=TaskStatus.IN_PROGRESS,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    _patch_research_externals(mocker)
    # New cycle produces 1 fresh finding — plus 2 deferred = 3 total
    new_findings = [_make_finding("Fresh finding C", direction_id)]
    mocker.patch("app.research_completion._parse_research_findings", return_value=new_findings)
    # Healthy pipeline: no cap
    mocker.patch("app.research_completion._assess_spawn_health", return_value=(False, None))

    await _handle_research_completion(task, "", db_session)

    result = await db_session.execute(
        select(Task).where(Task.project_id == project.id, Task.id != task.id)
    )
    child_tasks = result.scalars().all()
    child_titles = {ct.title for ct in child_tasks}
    assert len(child_tasks) == 3, (
        f"Both deferred findings + fresh finding should spawn tasks, got: {child_titles}"
    )
    assert "Deferred finding A" in child_titles
    assert "Deferred finding B" in child_titles
    assert "Fresh finding C" in child_titles


@pytest.mark.asyncio
async def test_prior_deferred_findings_still_capped_if_pipeline_degraded(db_session, mocker):
    """Deferred findings from previous cycle are still subject to the health gate cap."""
    from app.research_completion import _DEGRADED_SPAWN_CAP

    direction_id = "d-deferred-recovery02"
    direction = _active_direction(direction_id, "Deferred still capped")
    project = Project(
        name="Deferred Still Capped", user_id="test-user-123", autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # 3 prior deferred findings
    deferred_from_prev_cycle = [
        _make_finding(f"Old deferred {i}", direction_id) for i in range(3)
    ]
    task = Task(
        project_id=project.id,
        title=f"Research: {direction['text']}",
        payloads=[{"type": "research", "directions": [direction], "base_branch": "main"}],
        context={"research_depth": 0, "prior_deferred_findings": deferred_from_prev_cycle},
        status=TaskStatus.IN_PROGRESS,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    _patch_research_externals(mocker)
    # 2 new findings + 3 deferred = 5 total — but pipeline still degraded
    new_findings = [_make_finding(f"New finding {i}", direction_id) for i in range(2)]
    mocker.patch("app.research_completion._parse_research_findings", return_value=new_findings)
    mocker.patch(
        "app.research_completion._assess_spawn_health",
        return_value=(True, "all zones in cooldown"),
    )

    await _handle_research_completion(task, "", db_session)

    result = await db_session.execute(
        select(Task).where(Task.project_id == project.id, Task.id != task.id)
    )
    child_tasks = result.scalars().all()
    assert len(child_tasks) == _DEGRADED_SPAWN_CAP, (
        f"Cap still applies even with prior deferred findings: expected {_DEGRADED_SPAWN_CAP}, got {len(child_tasks)}"
    )

    await db_session.refresh(task)
    # The un-spawned findings should be deferred again for next cycle
    still_deferred = (task.context or {}).get("deferred_findings", [])
    assert len(still_deferred) == 5 - _DEGRADED_SPAWN_CAP, (
        f"Excess findings re-deferred: expected {5 - _DEGRADED_SPAWN_CAP}, got {len(still_deferred)}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Anomaly adjustment alert creation
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_anomaly_adjustment_creates_warning_alert(db_session, mocker):
    """WARNING alert created in DB when anomaly adjustments first activate."""
    direction = _active_direction("d-anomaly01", "Improve stability")
    project = Project(
        name="Anomaly Alert Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    mocker.patch(
        "app.anomaly_detection.check_anomaly_actions",
        new=AsyncMock(return_value={
            "action": "adjusted",
            "is_new": True,
            "max_concurrent_vms": 4,
            "failure_gate_threshold": 0.6,
            "trigger_anomalies": ["PROVISION_FAILURE"],
            "pre_adjustment_max_vms": 8,
        }),
    )
    mocker.patch("app.broadcast.broadcast_to_project", new=AsyncMock())
    mocker.patch("app.notification_dispatch.dispatch_external_notification", new=AsyncMock())

    await _spawn_research_task(project, db_session)

    result = await db_session.execute(
        select(Alert).where(Alert.project_id == project.id)
    )
    alerts = result.scalars().all()
    adjustment_alerts = [a for a in alerts if "auto-adjusted" in a.title]
    assert len(adjustment_alerts) == 1
    alert = adjustment_alerts[0]
    assert alert.severity == AlertSeverity.WARNING
    assert "4" in alert.message  # new max_concurrent_vms
    assert "8" in alert.message  # pre_adjustment value
    assert "PROVISION_FAILURE" in alert.message


@pytest.mark.asyncio
async def test_anomaly_already_active_does_not_duplicate_alert(db_session, mocker):
    """No duplicate alert when adjustments are already active (is_new=False)."""
    direction = _active_direction("d-anomaly02", "Reduce failures")
    project = Project(
        name="Anomaly No Dupe",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    mocker.patch(
        "app.anomaly_detection.check_anomaly_actions",
        new=AsyncMock(return_value={
            "action": "adjusted",
            "is_new": False,  # Already active — no new alert
            "max_concurrent_vms": 4,
            "failure_gate_threshold": 0.6,
            "trigger_anomalies": ["PROVISION_FAILURE"],
            "pre_adjustment_max_vms": 8,
        }),
    )
    mocker.patch("app.broadcast.broadcast_to_project", new=AsyncMock())
    mocker.patch("app.notification_dispatch.dispatch_external_notification", new=AsyncMock())

    await _spawn_research_task(project, db_session)

    result = await db_session.execute(
        select(Alert).where(Alert.project_id == project.id)
    )
    alerts = result.scalars().all()
    adjustment_alerts = [a for a in alerts if "auto-adjusted" in a.title]
    assert len(adjustment_alerts) == 0, "No alert when adjustment already active"


@pytest.mark.asyncio
async def test_anomaly_restoration_creates_info_alert(db_session, mocker):
    """INFO alert created in DB when anomaly adjustments are restored."""
    direction = _active_direction("d-anomaly03", "Monitor health")
    project = Project(
        name="Anomaly Restore Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    mocker.patch(
        "app.anomaly_detection.check_anomaly_actions",
        new=AsyncMock(return_value={
            "action": "restored",
            "max_concurrent_vms": 8,
            "failure_gate_threshold": 0.8,
        }),
    )
    mocker.patch("app.broadcast.broadcast_to_project", new=AsyncMock())
    mocker.patch("app.notification_dispatch.dispatch_external_notification", new=AsyncMock())

    await _spawn_research_task(project, db_session)

    result = await db_session.execute(
        select(Alert).where(Alert.project_id == project.id)
    )
    alerts = result.scalars().all()
    restore_alerts = [a for a in alerts if "restored" in a.title]
    assert len(restore_alerts) == 1
    alert = restore_alerts[0]
    assert alert.severity == AlertSeverity.INFO
    assert "8" in alert.message  # restored max_concurrent_vms
    assert "0.8" in alert.message  # restored failure gate


@pytest.mark.asyncio
async def test_respawn_after_cooldown_injects_prior_failure_context(db_session, mocker):
    """A finding re-spawned after CODE_ERROR cooldown receives _previous_* retry context
    from the prior failed task so buildRetryContext renders prior-attempt guidance."""
    direction = _active_direction("d-respawn1", "Re-spawn context direction")
    project = Project(
        name="Respawn Prior Context Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    title = "Add retry logic to webhook dispatcher"
    desc = "Webhook dispatcher fails silently on transient 5xx responses"
    fhash = hashlib.sha256((title + desc).encode()).hexdigest()[:16]

    prior_failed = Task(
        project_id=project.id,
        title=title,
        payloads=[{"type": "create", "prompt": "add retry", "base_branch": "main"}],
        context={
            "finding_hash": fhash,
            "_analysis_plan": "Planned to wrap dispatcher in tenacity retry decorator",
            "_pr_changed_files": ["app/webhooks.py", "testing/test_webhooks.py"],
            "_pr_diff_summary": [{"file": "app/webhooks.py", "added": 12, "removed": 2}],
            "_pr_url": "https://github.com/example/repo/pull/999",
            "_test_failure_summary": "1 failed: test_webhooks.py::test_retry_on_5xx",
            "_self_review_outcome": {"status": "failed", "notes": "missing retry count assertion"},
            "_file_change_details": [{"file": "app/webhooks.py", "summary": "added retry decorator"}],
        },
        status=TaskStatus.FAILED,
        latest_status_message="Tests failed: AssertionError in test_webhooks.py line 42",
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
        created_at=datetime.utcnow() - timedelta(hours=72),
    )
    db_session.add(prior_failed)
    await db_session.commit()

    task = await _make_research_task(project, direction, db_session)

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[{
            "title": title,
            "description": desc,
            "prompt": "Add retry logic",
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
    children = result.scalars().all()
    assert len(children) == 1, "Re-spawn after cooldown should produce one child task"
    ctx = children[0].context or {}
    assert ctx.get("_attempt_number") == 2
    assert "AssertionError" in (ctx.get("_previous_error") or "")
    assert ctx.get("_previous_failure_class") == "CODE_ERROR"
    assert ctx.get("_previous_analysis_plan") == "Planned to wrap dispatcher in tenacity retry decorator"
    assert ctx.get("_previous_changed_files") == ["app/webhooks.py", "testing/test_webhooks.py"]
    assert ctx.get("_previous_diff_summary") == [{"file": "app/webhooks.py", "added": 12, "removed": 2}]
    assert ctx.get("_previous_pr_url") == "https://github.com/example/repo/pull/999"
    assert ctx.get("_previous_test_failure_summary") == "1 failed: test_webhooks.py::test_retry_on_5xx"
    assert ctx.get("_previous_self_review_outcome") == {"status": "failed", "notes": "missing retry count assertion"}
    assert ctx.get("_previous_file_change_details") == [{"file": "app/webhooks.py", "summary": "added retry decorator"}]
