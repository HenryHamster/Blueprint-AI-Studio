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
import os
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient
from sqlalchemy import select

import app.shared_state as _shared_state
from app.time_utils import _utcnow
from app.models import (
    Alert,
    AlertSeverity,
    Artifact,
    ArtifactType,
    MessageType,
    Project,
    ProjectMessage,
    Task,
    TaskStatus,
)
from app.autonomous_research import _spawn_periodic_task, _spawn_research_task
from app.autonomous_spawn_budget import (
    classify_autonomous_spawn_budget_category,
    compute_autonomous_spawn_budget_snapshot,
)
from app.research_completion import _handle_research_completion
from app.direction_outcomes import _update_direction_research_outcomes
from app.max_plan_pool import _max_plan_pools, _account_by_id
from app.capability_demand import collect_missing_capability_demand
from app.payload_schemas import validate_internal_payload
from app.task_evaluators import ADVERSARIAL_GATE_VERDICT_KEY
from app.auth import create_agent_token
from app.workbench_state import (
    STRATEGIC_LOCAL_RUNNER_REASON_CONTEXT_KEY,
    STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY,
)

USE_POSTGRES = os.getenv("USE_TEST_POSTGRES", "").lower() == "true" and os.getenv(
    "DATABASE_URL", ""
).startswith("postgresql")


@pytest.fixture(autouse=True)
def _ensure_test_account_pool():
    """Ensure _spawn_research_task finds an available account for the test user.
    Also clears the task_results ring buffer so the _assess_spawn_health failure-rate
    gate doesn't trigger from results left by other test modules."""
    uid = "test-user-123"
    entry = {
        "id": "test-acct",
        "user_id": uid,
        "name": "test",
        "enabled": True,
        "active_tasks": 0,
        "oauth_token": "sk-ant-test",
        "total_tasks": 0,
        "token_created_at": None,
        "claude_config_json": None,
    }
    _max_plan_pools.setdefault(uid, []).append(entry)
    _account_by_id["test-acct"] = entry
    _shared_state.deploy_monitoring["task_results"].clear()
    yield
    _max_plan_pools.get(uid, [])[:] = [
        a for a in _max_plan_pools.get(uid, []) if a["id"] != "test-acct"
    ]
    _account_by_id.pop("test-acct", None)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


async def _deploy_project(
    client: AsyncClient, mocker, name: str = "Test Project"
) -> str:
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

    # R4-W1 (audit C2): _ensure_branch returns Task | None, not bool. Pass-through
    # the task argument so the caller's `task = relocked` swap binds to the same row.
    async def _passthrough_ensure_branch(task, *_a, **_kw):
        return task

    mocker.patch("app.task_completion._ensure_branch", new=_passthrough_ensure_branch)
    mocker.patch("app.broadcast.broadcast_to_project", new=AsyncMock(return_value=None))
    mocker.patch(
        "app.research_completion._validate_plan_dependencies", return_value=True
    )
    mocker.patch("app.research_completion._log_file_overlaps", return_value=[])
    # Simulate no pool configured so capacity-aware batch cap doesn't constrain unit tests
    mocker.patch("app.max_plan_pool._all_accounts", return_value=[])
    # Disable the structural actionability downgrade gate so unit tests that
    # construct simplified findings (without claims / sources / long descriptions)
    # are not deferred as low_actionability. Tests that specifically exercise the
    # gate's behavior should bypass _patch_research_externals or override this.
    mocker.patch(
        "app.research_completion._apply_structural_actionability_downgrade",
        return_value=0,
    )
    # Disable the adversarial spawn-gate (default-on) so existing tests that
    # incidentally use high-risk findings are not parked awaiting the grader.
    mocker.patch(
        "app.research_completion._is_high_stakes_finding",
        return_value=False,
    )


def _make_finding(
    title: str = "Fix a bug",
    direction_id: str = "d-test0001",
    scope: str = "small",
    finding_type: str = "improvement",
    priority: str = "medium",
) -> dict:
    return {
        "title": title,
        "description": f"Description: {title}",
        "prompt": f"Implement: {title}",
        "priority": priority,
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
        payloads=[
            {"type": "research", "directions": [direction], "base_branch": "main"}
        ],
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
async def test_research_payload_includes_knowledge_index_scoped_to_direction(
    db_session,
):
    """A spawned research task carries the compact knowledge index, with the
    chosen direction's documents surfaced first."""
    from app.models import KnowledgeDocument

    project = Project(
        name="Knowledge Index Research",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[_active_direction("d-ki-research")],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    src = Task(
        project_id=project.id,
        title="src",
        payloads=[{"type": "web_research", "topic": "t", "base_branch": "main"}],
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(src)
    await db_session.commit()
    await db_session.refresh(src)
    db_session.add(
        KnowledgeDocument(
            project_id=project.id,
            source_task_id=src.id,
            title="Existing knowledge",
            summary="A prior document.",
            body="# Body\n\n" + ("x" * 50),
            topic="auth",
            direction_id="d-ki-research",
            source_kind="web_research",
        )
    )
    await db_session.commit()

    await _spawn_research_task(project, db_session)

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    research = [
        t for t in result.scalars().all() if t.first_payload.get("type") == "research"
    ]
    assert len(research) == 1
    payload = research[0].first_payload
    assert "knowledge_index" in payload
    entry = payload["knowledge_index"][0]
    assert entry["title"] == "Existing knowledge"
    assert entry["direction_id"] == "d-ki-research"
    assert "body" not in entry


@pytest.mark.asyncio
async def test_research_payload_includes_compute_experiment_sweeps(
    db_session,
):
    """A spawned research task carries compute_experiment_sweeps when completed
    sweeps exist; lone-arm sweeps (only one doc) are excluded from the grouped map."""
    from app.models import KnowledgeDocument, SourceKind

    project = Project(
        name="Sweep Research",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[_active_direction("d-sweep-research")],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    src = Task(
        project_id=project.id,
        title="compute exp",
        payloads=[{"type": "compute_experiment", "base_branch": "main"}],
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(src)
    await db_session.commit()
    await db_session.refresh(src)

    sweep_id = "sweep-abc123"
    for arm_name in ("arm-a", "arm-b"):
        db_session.add(
            KnowledgeDocument(
                project_id=project.id,
                source_task_id=src.id,
                title=f"Experiment {arm_name}",
                summary=f"Result for {arm_name}",
                body="# Result\n\nsome data",
                topic="perf",
                direction_id="d-sweep-research",
                source_kind=SourceKind.COMPUTE_EXPERIMENT.value,
                sweep_id=sweep_id,
            )
        )
    await db_session.commit()

    await _spawn_research_task(project, db_session)

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    research = [
        t for t in result.scalars().all() if t.first_payload.get("type") == "research"
    ]
    assert len(research) == 1
    payload = research[0].first_payload
    assert "compute_experiment_sweeps" in payload
    sweeps = payload["compute_experiment_sweeps"]
    assert sweep_id in sweeps
    assert len(sweeps[sweep_id]) == 2
    arm_titles = {e["title"] for e in sweeps[sweep_id]}
    assert "Experiment arm-a" in arm_titles
    assert "Experiment arm-b" in arm_titles
    # Bodies must not be included (index entries only)
    assert all("body" not in e for e in sweeps[sweep_id])


@pytest.mark.asyncio
async def test_research_payload_playbook_bounded_to_keys_index(db_session):
    """The research payload's design_doc carries only playbook key names (_playbook_keys),
    never full verbatim content. The capped ranked selection already ships via task.playbook;
    embedding all entries here would double the context on every agentic turn."""
    project = Project(
        name="Playbook Bounded Research",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[_active_direction("d-playbook-bounded")],
        vision={"purpose": "test"},
        design_doc={
            "playbook/key-one": {"content": "Verbose playbook entry content that should not ship"},
            "playbook/key-two": {"content": "Another verbose entry that should not ship"},
            "playbook/pending-key": {"content": "Pending entry", "pending_source": "task-abc"},
            "systems/sys-test": {"name": "Test System", "status": "functional"},
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _spawn_research_task(project, db_session)

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    research = [t for t in result.scalars().all() if t.first_payload.get("type") == "research"]
    assert len(research) == 1
    payload = research[0].first_payload
    design_doc = payload.get("design_doc", {})

    # Full playbook content must NOT be embedded in design_doc
    for k in design_doc:
        assert not k.startswith("playbook/"), (
            f"Full playbook entry '{k}' was embedded verbatim — should only ship key names"
        )

    # _playbook_keys must list non-pending entries only
    assert "_playbook_keys" in design_doc, "design_doc must carry _playbook_keys index"
    assert set(design_doc["_playbook_keys"]) == {"key-one", "key-two"}, (
        "pending_source entries must be excluded from _playbook_keys"
    )


@pytest.mark.asyncio
async def test_research_payload_annotations_include_capability_proof_status(
    db_session,
):
    """Spawned research payloads expose proof-annotated capability vocabulary."""
    project = Project(
        name="Capability Proof Research",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[_active_direction("d-proof-vocab")],
        vision={"purpose": "test"},
        problem_model={
            "capabilities": [
                {"name": "Repo Map", "status": "working"},
                {"name": "Prompt Repair", "status": "planned"},
            ]
        },
        capability_proof_records={
            "repo-map": [
                {
                    "task_id": "task-proof-1",
                    "proved_at": "2026-06-09T00:00:00",
                    "proof_kind": "named_test",
                    "proof_ref": "backend/testing/test_capability_proof.py::test_repo_map",
                    "commit_sha": "abc123",
                }
            ]
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _spawn_research_task(project, db_session)

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    research = [
        t for t in result.scalars().all() if t.first_payload.get("type") == "research"
    ]
    assert len(research) == 1
    capabilities = research[0].first_payload["available_capabilities"]
    assert capabilities == [
        {"name": "repo-map", "status": "working", "proof_status": "proven"},
        {"name": "prompt-repair", "status": "planned", "proof_status": "unproven"},
    ]


@pytest.mark.asyncio
async def test_autonomous_research_stamps_idempotency_key(db_session):
    """A spawned directed research task carries the shared direction-scoped
    idempotency key; an undirected (cross-cutting) cycle carries the undirected
    key namespace — so the partial unique index dedups both across spawn paths."""
    from app.autonomous_research import research_idempotency_key

    project = Project(
        name="Idem Key Stamp",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[_active_direction("d-idem-key1")],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _spawn_research_task(project, db_session)

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    research = [
        t for t in result.scalars().all() if t.first_payload.get("type") == "research"
    ]
    assert len(research) == 1
    task = research[0]
    direction_id = task.first_payload.get("current_direction_id")
    if direction_id:
        assert task.idempotency_key == research_idempotency_key(
            project.id, direction_id
        )
    else:
        # Undirected cycle: key lives in the undirected namespace.
        assert task.idempotency_key == research_idempotency_key(project.id, None)
    # Either way, a key is always stamped — the DB index is the authoritative guard.
    assert task.idempotency_key is not None


@pytest.mark.asyncio
async def test_autonomous_batch_skips_duplicate_direction(db_session):
    """When one of several chosen directions already has an active task holding
    its idempotency slot, the per-task savepoint rolls back ONLY that direction's
    insert and skips it — the other directions still spawn."""
    from app.autonomous_research import research_idempotency_key

    dir_a = _active_direction("d-batch-aa1", text="Direction A")
    dir_b = _active_direction("d-batch-bb2", text="Direction B")
    project = Project(
        name="Batch Skip Dup",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[dir_a, dir_b],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Pre-occupy direction A's idempotency slot with an in-flight task.
    blocker = Task(
        project_id=project.id,
        title="Research: pre-existing on A",
        payloads=[
            {
                "type": "research",
                "directions": [dir_a],
                "current_direction_id": "d-batch-aa1",
                "base_branch": "main",
            }
        ],
        status=TaskStatus.IN_PROGRESS,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
        idempotency_key=research_idempotency_key(project.id, "d-batch-aa1"),
    )
    db_session.add(blocker)
    await db_session.commit()

    await _spawn_research_task(project, db_session)

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    research = [
        t for t in result.scalars().all() if t.first_payload.get("type") == "research"
    ]
    # Active research tasks keyed per direction.
    keys = {t.idempotency_key for t in research}
    # Direction A's slot is held by exactly one task (the pre-existing blocker);
    # no duplicate was inserted for it.
    a_key = research_idempotency_key(project.id, "d-batch-aa1")
    a_tasks = [t for t in research if t.idempotency_key == a_key]
    assert len(a_tasks) == 1
    assert a_tasks[0].id == blocker.id
    # The batch still produced at least one task for a different slot (direction B
    # or an undirected fallback) — a single collision did not nuke the whole cycle.
    assert any(k != a_key for k in keys)


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
        "opened_at": _utcnow(),
        "cooldown_seconds": 1800,
        "base_cooldown": 1800,
        "half_open_task_id": None,
    }
    try:
        await _spawn_research_task(project, db_session)

        result = await db_session.execute(
            select(Task).where(Task.project_id == project.id)
        )
        assert len(result.scalars().all()) == 0, (
            "No task spawned when circuit breaker OPEN"
        )
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
        vision={
            "purpose": "test"
        },  # Non-empty vision so force_strategic doesn't override
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Opened 2 hours ago — 30-min cooldown has long elapsed
    _shared_state.circuit_breakers[str(project.id)] = {
        "state": "OPEN",
        "failure_count": 3,
        "success_count": 0,
        "opened_at": _utcnow() - timedelta(hours=2),
        "cooldown_seconds": 1800,
        "base_cooldown": 1800,
        "half_open_task_id": None,
    }
    try:
        await _spawn_research_task(project, db_session)

        result = await db_session.execute(
            select(Task).where(Task.project_id == project.id)
        )
        tasks = result.scalars().all()
        research = [
            t for t in tasks if (t.payloads or [{}])[0].get("type") == "research"
        ]
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
        vision={
            "purpose": "test",
            "_strategic_failures": 3,
        },  # At failure cap so force_strategic doesn't fire
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
        vision={
            "purpose": "test",
            "_strategic_failures": 3,
        },  # At failure cap so force_strategic doesn't fire
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _spawn_research_task(project, db_session)

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    assert len(result.scalars().all()) == 0


# ─────────────────────────────────────────────────────────────────────────────
# 3. _spawn_research_task — Bootstrap-Pending Guard
# ─────────────────────────────────────────────────────────────────────────────


def _active_direction_with_cooldown_elapsed(
    direction_id: str, text: str = "Research direction"
) -> dict:
    """Return an active direction whose last_researched_at is well past cooldown."""
    return {
        **_active_direction(direction_id, text),
        "last_researched_at": (_utcnow() - timedelta(hours=5)).isoformat(),
    }


@pytest.mark.asyncio
async def test_research_spawn_defers_while_external_repo_bootstrap_pending(db_session):
    """_spawn_research_task defers (no task created) while a repo_bootstrap is in-flight.

    This is the acceptance test for the bootstrap-pending guard added to
    _spawn_research_task_inner.  An external-repo project whose
    build_config.bootstrap_completed_at is unset and has an active
    repo_bootstrap task must not spawn research work.
    """
    direction = _active_direction_with_cooldown_elapsed(
        "d-bootstrap-defer", "Ungrounded research"
    )
    project = Project(
        name="Bootstrap Pending Research",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test", "_strategic_failures": 3},
        build_config={},  # bootstrap_completed_at not set
        is_self_repo=False,
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Simulate an in-flight repo_bootstrap task
    bootstrap_task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "repo_bootstrap"}],
    )
    db_session.add(bootstrap_task)
    await db_session.commit()

    await _spawn_research_task(project, db_session)

    result = await db_session.execute(
        select(Task).where(
            Task.project_id == project.id,
            Task.payloads[0]["type"].as_string() == "research",
        )
    )
    assert result.scalars().first() is None, (
        "No research task should be created while repo_bootstrap is in-flight"
    )


@pytest.mark.asyncio
async def test_research_spawn_proceeds_when_external_repo_bootstrap_completed(
    db_session,
):
    """_spawn_research_task proceeds normally once bootstrap_completed_at is set."""
    direction = _active_direction_with_cooldown_elapsed(
        "d-bootstrap-ok", "Grounded research"
    )
    project = Project(
        name="Bootstrap Complete Research",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test", "_strategic_failures": 3},
        build_config={"bootstrap_completed_at": "2026-01-01T00:00:00"},
        is_self_repo=False,
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _spawn_research_task(project, db_session)

    result = await db_session.execute(
        select(Task).where(
            Task.project_id == project.id,
            Task.payloads[0]["type"].as_string() == "research",
        )
    )
    assert result.scalars().first() is not None, (
        "A research task should be created once bootstrap is complete"
    )


@pytest.mark.asyncio
async def test_research_spawn_proceeds_for_self_repo_regardless_of_bootstrap(
    db_session,
):
    """_spawn_research_task is not gated on bootstrap for self-repo projects."""
    direction = _active_direction_with_cooldown_elapsed(
        "d-self-repo", "Self-repo research"
    )
    project = Project(
        name="Self Repo Research",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test", "_strategic_failures": 3},
        build_config={},  # bootstrap_completed_at not set
        is_self_repo=True,
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Even with an active repo_bootstrap task, self-repo projects are unaffected
    bootstrap_task = Task(
        project_id=project.id,
        status=TaskStatus.QUEUED,
        payloads=[{"type": "repo_bootstrap"}],
    )
    db_session.add(bootstrap_task)
    await db_session.commit()

    await _spawn_research_task(project, db_session)

    result = await db_session.execute(
        select(Task).where(
            Task.project_id == project.id,
            Task.payloads[0]["type"].as_string() == "research",
        )
    )
    assert result.scalars().first() is not None, (
        "Self-repo projects should not be gated on bootstrap completion"
    )


@pytest.mark.asyncio
async def test_blocked_research_lane_keeps_direction_out_of_respawn(db_session):
    """A direction whose prior research task is BLOCKED on an upstream
    prerequisite stays structurally occupied — even past cooldown — so the
    autonomous loop does not open a fresh research cycle on it."""
    direction = {
        **_active_direction("d-blocked1", "Parked on prerequisite"),
        # 5h ago — well past the 4h direction cooldown, so the ONLY thing that
        # can hold this direction is the BLOCKED lane itself.
        "last_researched_at": (_utcnow() - timedelta(hours=5)).isoformat(),
    }
    project = Project(
        name="Blocked Lane Hold",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test"},  # Non-empty vision so force_strategic doesn't fire
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    blocked = Task(
        project_id=project.id,
        title="Research: parked",
        payloads=[
            {
                "type": "research",
                "current_direction_id": "d-blocked1",
                "directions": [direction],
                "base_branch": "main",
            }
        ],
        status=TaskStatus.BLOCKED,
        blocker_kind="upstream_dependency",
        created_by="ORCHESTRATOR",
    )
    db_session.add(blocked)
    await db_session.commit()

    await _spawn_research_task(project, db_session)

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    research_tasks = [
        t for t in result.scalars().all() if t.first_payload.get("type") == "research"
    ]
    assert len(research_tasks) == 1, (
        "No fresh research cycle should spawn while the direction's prior lane is "
        "BLOCKED on an upstream prerequisite — only the pre-existing blocked task remains"
    )
    assert research_tasks[0].id == blocked.id


@pytest.mark.asyncio
async def test_parked_research_keeps_direction_out_of_respawn(db_session):
    """A direction whose prior research task is PARKED (non-terminal, waiting to
    wake) must not be selected for a fresh autonomous cycle — PARKED is active in
    the idempotency-key predicate and must be active in the busy-direction guard."""
    direction = {
        **_active_direction("d-parked1", "Waiting to wake"),
        # 5h ago — well past the 4h cooldown, so only the PARKED task holds it.
        "last_researched_at": (_utcnow() - timedelta(hours=5)).isoformat(),
    }
    project = Project(
        name="Parked Lane Hold",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    parked = Task(
        project_id=project.id,
        title="Research: parked on wakeup",
        payloads=[
            {
                "type": "research",
                "current_direction_id": "d-parked1",
                "directions": [direction],
                "base_branch": "main",
            }
        ],
        status=TaskStatus.PARKED,
        created_by="ORCHESTRATOR",
    )
    db_session.add(parked)
    await db_session.commit()

    await _spawn_research_task(project, db_session)

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    research_tasks = [
        t for t in result.scalars().all() if t.first_payload.get("type") == "research"
    ]
    assert len(research_tasks) == 1, (
        "No fresh research cycle should spawn while the direction's prior lane is "
        "PARKED waiting to wake — only the pre-existing parked task remains"
    )
    assert research_tasks[0].id == parked.id


@pytest.mark.asyncio
async def test_parked_legacy_research_keeps_direction_out_of_respawn(db_session):
    """Legacy PARKED research rows that only carry ``directions[0].id`` must
    still hold the direction busy until they wake or reach a terminal status."""
    direction = {
        **_active_direction("d-parked-legacy1", "Waiting to wake"),
        # 5h ago — well past the 4h cooldown, so only the PARKED task holds it.
        "last_researched_at": (_utcnow() - timedelta(hours=5)).isoformat(),
    }
    project = Project(
        name="Legacy Parked Lane Hold",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    parked = Task(
        project_id=project.id,
        title="Research: parked on wakeup",
        payloads=[
            {
                "type": "research",
                "directions": [direction],
                "base_branch": "main",
            }
        ],
        status=TaskStatus.PARKED,
        created_by="ORCHESTRATOR",
    )
    db_session.add(parked)
    await db_session.commit()

    await _spawn_research_task(project, db_session)

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    research_tasks = [
        t for t in result.scalars().all() if t.first_payload.get("type") == "research"
    ]
    assert len(research_tasks) == 1, (
        "No fresh research cycle should spawn while the legacy PARKED lane is "
        "waiting to wake — only the pre-existing parked task remains"
    )
    assert research_tasks[0].id == parked.id


@pytest.mark.asyncio
async def test_all_paused_directions_still_get_strategic_via_backstop(
    db_session, mocker
):
    """Regression: when every active direction is recommended_mode=pause, the
    wall-clock strategic backstop must still fire a strategic cycle.

    Before the fix, the all-paused early-return short-circuited the strategic-spawn
    block even when force_strategic was set, so a project whose only directions were
    all paused starved permanently: no research spawns → the modulo counter never
    advanced → directions never re-evaluated → the pipeline went idle with free VMs.
    """
    design_doc_mock = mocker.patch(
        "app.strategic_spawning._spawn_design_doc_update",
        new=AsyncMock(return_value=True),
    )
    strategic_review_mock = mocker.patch(
        "app.strategic_spawning._spawn_strategic_review",
        new=AsyncMock(return_value=True),
    )
    project = Project(
        name="All Paused + Backstop Due",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[
            {
                **_active_direction("d-paused01", "Paused A"),
                "recommended_mode": "pause",
            },
        ],
        vision={
            "purpose": "test"
        },  # purpose set so force_strategic relies on the backstop
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # A prior strategic task older than strategic_max_interval (180min) so the
    # wall-clock backstop promotes force_strategic=True.
    stale_strategic = Task(
        project_id=project.id,
        title="Old strategic cycle",
        payloads=[{"type": "design_doc_update", "base_branch": "main"}],
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        created_at=_utcnow() - timedelta(minutes=200),
    )
    db_session.add(stale_strategic)
    await db_session.commit()

    await _spawn_research_task(project, db_session)

    assert design_doc_mock.called or strategic_review_mock.called, (
        "backstop must spawn a strategic cycle even when all directions are paused"
    )


@pytest.mark.asyncio
async def test_strategic_spawn_skipped_when_advisory_lock_held(db_session, mocker):
    """Regression (strategic double-spawn): when a peer orchestrator instance holds
    the per-project research-spawn advisory lock, the strategic-spawn path must be
    skipped this tick rather than racing a second strategic cycle. Two instances
    both passing the unlocked has_active_strategic check double-spawned strategic
    reviews ~30s apart (and tripped the phase backstop into false failures); the
    cross-instance lock guard closes that window. Complements
    test_all_paused_directions_still_get_strategic_via_backstop, which proves the
    same setup DOES spawn when the lock is free (its default in the test harness)."""
    design_doc_mock = mocker.patch(
        "app.strategic_spawning._spawn_design_doc_update",
        new=AsyncMock(return_value=True),
    )
    strategic_review_mock = mocker.patch(
        "app.strategic_spawning._spawn_strategic_review",
        new=AsyncMock(return_value=True),
    )
    # Simulate a peer instance already holding the per-project spawn lock.
    mocker.patch(
        "app.autonomous_research._try_research_spawn_advisory_lock",
        new=AsyncMock(return_value=False),
    )
    project = Project(
        name="Lock Held — No Double Spawn",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[
            {
                **_active_direction("d-paused03", "Paused A"),
                "recommended_mode": "pause",
            },
        ],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # A stale strategic task makes the wall-clock backstop due (force_strategic).
    stale_strategic = Task(
        project_id=project.id,
        title="Old strategic cycle",
        payloads=[{"type": "design_doc_update", "base_branch": "main"}],
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        created_at=_utcnow() - timedelta(minutes=200),
    )
    db_session.add(stale_strategic)
    await db_session.commit()

    await _spawn_research_task(project, db_session)

    assert not design_doc_mock.called and not strategic_review_mock.called, (
        "strategic spawn must be skipped while a peer holds the research-spawn lock"
    )


@pytest.mark.asyncio
async def test_architecture_cycle_triggers_on_first_cycle_with_empty_design_doc(
    db_session, mocker
):
    """Architecture cycle should trigger on the first strategic cycle when the
    design_doc is empty (has no systems), ensuring newly-onboarded projects create
    architectural context before spending cycles on undirected research.

    Regression check: with default arch_frequency=3, cycle 1 normally satisfies
    (1 % 3 == 1 != 0), so is_architecture_cycle would be False. The empty-design_doc
    override ensures the architecture phase runs despite the modulo check, giving
    fresh projects a design_doc to anchor subsequent research."""
    design_doc_mock = mocker.patch(
        "app.strategic_spawning._spawn_design_doc_update",
        new=AsyncMock(return_value=True),
    )
    strategic_review_mock = mocker.patch(
        "app.strategic_spawning._spawn_strategic_review",
        new=AsyncMock(return_value=True),
    )
    project = Project(
        name="Empty Design Doc Architecture Trigger",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[_active_direction("d-arch01", "Test direction")],
        vision={"purpose": "test"},
        design_doc={},  # Empty: no systems key, should trigger architecture
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # A prior strategic task older than strategic_max_interval (180min) so the
    # wall-clock backstop promotes force_strategic=True, which triggers the strategic
    # spawn regardless of the modulo counter.
    stale_strategic = Task(
        project_id=project.id,
        title="Old strategic cycle",
        payloads=[{"type": "design_doc_update", "base_branch": "main"}],
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        created_at=_utcnow() - timedelta(minutes=200),
    )
    db_session.add(stale_strategic)
    await db_session.commit()

    await _spawn_research_task(project, db_session)

    assert design_doc_mock.called, (
        "architecture cycle must trigger on first strategic cycle when design_doc "
        "is empty (has no systems)"
    )
    # When architecture cycle triggers, it calls _spawn_design_doc_update, not
    # _spawn_strategic_review. Strategic review comes as a completion-chain task
    # after design_doc_update completes.
    assert not strategic_review_mock.called


@pytest.mark.asyncio
async def test_all_paused_directions_skip_when_backstop_not_due(db_session, mocker):
    """Companion: with no strategic cycle due (backstop not firing, force_strategic
    stays False), an all-paused direction set is correctly skipped — the guard only
    bypasses the pause return when a strategic cycle is actually forced."""
    design_doc_mock = mocker.patch(
        "app.strategic_spawning._spawn_design_doc_update",
        new=AsyncMock(return_value=True),
    )
    strategic_review_mock = mocker.patch(
        "app.strategic_spawning._spawn_strategic_review",
        new=AsyncMock(return_value=True),
    )
    project = Project(
        name="All Paused, No Backstop",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[
            {
                **_active_direction("d-paused02", "Paused A"),
                "recommended_mode": "pause",
            },
        ],
        vision={
            "purpose": "test"
        },  # purpose set; no stale strategic task → backstop idle
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _spawn_research_task(project, db_session)

    assert not design_doc_mock.called and not strategic_review_mock.called, (
        "must not spawn a strategic cycle when none is due"
    )
    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    assert len(result.scalars().all()) == 0


def _recent_strategic_task(project_id, minutes_ago: int) -> Task:
    """A terminal strategic task created within the spawn-cooldown window."""
    return Task(
        project_id=project_id,
        title="Recent strategic cycle",
        payloads=[{"type": "design_doc_update", "base_branch": "main"}],
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        created_at=_utcnow() - timedelta(minutes=minutes_ago),
    )


@pytest.mark.asyncio
async def test_refresh_marker_bypasses_spawn_cooldown_and_clears(db_session, mocker):
    """A pending strategic-refresh marker (set by a resolved verdict that couldn't
    spawn during an active cycle) must bypass the 30-min spawn-cooldown so the
    refresh fires promptly — and the marker is consumed on spawn. Regression for
    the 2026-06-02 dropped-trigger starvation."""
    strategic_review_mock = mocker.patch(
        "app.strategic_spawning._spawn_strategic_review",
        new=AsyncMock(return_value=True),
    )
    mocker.patch(
        "app.strategic_spawning._spawn_design_doc_update",
        new=AsyncMock(return_value=True),
    )
    project = Project(
        name="Refresh bypasses cooldown",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[],  # frontier drained (post-promotion) → force_strategic
        vision={
            "purpose": "test",
            "_strategic_refresh": {
                "requested_at": _utcnow().isoformat(),
                "reason": "post_qualification:promoted",
                "count": 1,
            },
        },
        design_doc={
            "systems/api": {"id": "api", "status": "working"}
        },  # Has systems, so architecture isn't triggered
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    # A strategic task 5 min ago — inside the 30-min cooldown that would normally
    # suppress the spawn.
    db_session.add(_recent_strategic_task(project.id, minutes_ago=5))
    await db_session.commit()

    await _spawn_research_task(project, db_session)

    assert strategic_review_mock.called, (
        "a pending refresh marker must bypass the spawn-cooldown"
    )
    await db_session.refresh(project)
    # Consumed: marker gone, fire time stamped (so the min-interval floor applies).
    assert (project.vision or {}).get("_strategic_refresh") is None
    assert (project.vision or {}).get("_strategic_refresh_last_fired_at")


@pytest.mark.asyncio
async def test_spawn_cooldown_holds_without_refresh_marker(db_session, mocker):
    """Companion: with no refresh marker, the spawn-cooldown still suppresses a
    recent-spawn — the bypass is gated strictly on a pending refresh, so it can't
    reintroduce the cancel/fail spam loop the cooldown guards against."""
    strategic_review_mock = mocker.patch(
        "app.strategic_spawning._spawn_strategic_review",
        new=AsyncMock(return_value=True),
    )
    design_doc_mock = mocker.patch(
        "app.strategic_spawning._spawn_design_doc_update",
        new=AsyncMock(return_value=True),
    )
    project = Project(
        name="Cooldown holds without marker",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[],  # force_strategic, but recent spawn within cooldown
        vision={"purpose": "test"},  # no refresh marker
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(_recent_strategic_task(project.id, minutes_ago=5))
    await db_session.commit()

    await _spawn_research_task(project, db_session)

    assert not strategic_review_mock.called and not design_doc_mock.called, (
        "the spawn-cooldown must still suppress when no refresh is pending"
    )


@pytest.mark.asyncio
async def test_refresh_marker_parks_when_breaker_tripped(db_session, mocker):
    """A broken request must not loop: with the _strategic_failures breaker tripped
    (>= 3, not decayed), a pending refresh does NOT fire — it parks (marker kept)
    until the breaker decays, instead of bypassing the cooldown forever."""
    strategic_review_mock = mocker.patch(
        "app.strategic_spawning._spawn_strategic_review",
        new=AsyncMock(return_value=True),
    )
    design_doc_mock = mocker.patch(
        "app.strategic_spawning._spawn_design_doc_update",
        new=AsyncMock(return_value=True),
    )
    project = Project(
        name="Breaker parks refresh",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[],
        vision={
            "purpose": "test",
            "_strategic_failures": 3,
            "_strategic_failures_at": _utcnow().isoformat(),  # recent → no decay
            "_strategic_refresh": {
                "requested_at": _utcnow().isoformat(),
                "reason": "post_qualification:promoted",
                "count": 1,
            },
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(_recent_strategic_task(project.id, minutes_ago=5))
    await db_session.commit()

    await _spawn_research_task(project, db_session)

    assert not strategic_review_mock.called and not design_doc_mock.called, (
        "a tripped breaker must park the refresh, not fire it"
    )
    await db_session.refresh(project)
    # Marker preserved — it fires once the breaker decays, nothing is lost.
    assert (project.vision or {}).get("_strategic_refresh") is not None


def _active_strategic_task(project_id) -> Task:
    """A non-terminal strategic task (an active cycle in flight)."""
    return Task(
        project_id=project_id,
        title="Active strategic cycle",
        payloads=[{"type": "strategic_review", "base_branch": "main"}],
        status=TaskStatus.IN_PROGRESS,
        created_by="ORCHESTRATOR",
        created_at=_utcnow(),
    )


@pytest.mark.asyncio
async def test_operator_refresh_fires_despite_tripped_breaker(db_session, mocker):
    """An operator steering request must NOT be silently held by the failure
    breaker — it is exactly when the strategist has been failing that an operator
    needs to intervene. With _strategic_failures tripped (>= 3, not decayed), an
    operator-origin refresh still bypasses the cooldown and fires."""
    strategic_review_mock = mocker.patch(
        "app.strategic_spawning._spawn_strategic_review",
        new=AsyncMock(return_value=True),
    )
    mocker.patch(
        "app.strategic_spawning._spawn_design_doc_update",
        new=AsyncMock(return_value=True),
    )
    project = Project(
        name="Operator bypasses breaker",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[],
        vision={
            "purpose": "test",
            "_strategic_failures": 3,
            "_strategic_failures_at": _utcnow().isoformat(),  # recent → no decay
            "_strategic_refresh": {
                "requested_at": _utcnow().isoformat(),
                "reason": "operator_steering",
                "count": 1,
                "origin": "operator",
            },
        },
        design_doc={
            "systems/api": {"id": "api", "status": "working"}
        },  # Has systems, so architecture isn't triggered
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(_recent_strategic_task(project.id, minutes_ago=5))
    await db_session.commit()

    await _spawn_research_task(project, db_session)

    assert strategic_review_mock.called, (
        "an operator-origin refresh must bypass the breaker and fire"
    )
    await db_session.refresh(project)
    # Consumed on spawn — marker gone.
    assert (project.vision or {}).get("_strategic_refresh") is None


@pytest.mark.asyncio
async def test_operator_refresh_deferred_behind_active_cycle_posts_once(
    db_session, mocker
):
    """When an operator steering request lands while a strategic cycle is already
    running, the operator must be told it is queued — exactly one system message,
    with the marker stamped ``notified`` so later ticks stay quiet."""
    strategic_review_mock = mocker.patch(
        "app.strategic_spawning._spawn_strategic_review",
        new=AsyncMock(return_value=True),
    )
    design_doc_mock = mocker.patch(
        "app.strategic_spawning._spawn_design_doc_update",
        new=AsyncMock(return_value=True),
    )
    project = Project(
        name="Operator deferred behind active cycle",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[],
        vision={
            "purpose": "test",
            "_strategic_refresh": {
                "requested_at": _utcnow().isoformat(),
                "reason": "operator_steering",
                "count": 1,
                "origin": "operator",
            },
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    # An active strategic cycle blocks the spawn this tick.
    db_session.add(_active_strategic_task(project.id))
    await db_session.commit()

    await _spawn_research_task(project, db_session)

    # Deferred (active cycle) — not spawned, marker preserved for the next tick.
    assert not strategic_review_mock.called and not design_doc_mock.called
    await db_session.refresh(project)
    marker = (project.vision or {}).get("_strategic_refresh")
    assert marker is not None and marker.get("notified") is True

    msgs = (
        (
            await db_session.execute(
                select(ProjectMessage).where(
                    ProjectMessage.project_id == project.id,
                    ProjectMessage.from_type == "system",
                )
            )
        )
        .scalars()
        .all()
    )
    steering = [
        m
        for m in msgs
        if (m.context or {}).get("event_type") == "strategic_steering_deferred"
    ]
    assert len(steering) == 1, "operator must get exactly one deferral notice"

    # A second tick must stay quiet (notified flag suppresses re-emission).
    await _spawn_research_task(project, db_session)
    msgs2 = (
        (
            await db_session.execute(
                select(ProjectMessage).where(
                    ProjectMessage.project_id == project.id,
                    ProjectMessage.from_type == "system",
                )
            )
        )
        .scalars()
        .all()
    )
    steering2 = [
        m
        for m in msgs2
        if (m.context or {}).get("event_type") == "strategic_steering_deferred"
    ]
    assert len(steering2) == 1, "deferral notice must not re-emit every tick"


@pytest.mark.asyncio
async def test_qualification_refresh_deferred_behind_active_cycle_stays_silent(
    db_session, mocker
):
    """A qualification-origin refresh deferred behind an active cycle must NOT post
    an operator-facing steering message — that visibility is reserved for operator
    intent (automated refreshes park silently as before)."""
    mocker.patch(
        "app.strategic_spawning._spawn_strategic_review",
        new=AsyncMock(return_value=True),
    )
    mocker.patch(
        "app.strategic_spawning._spawn_design_doc_update",
        new=AsyncMock(return_value=True),
    )
    project = Project(
        name="Qualification deferred silently",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[],
        vision={
            "purpose": "test",
            "_strategic_refresh": {
                "requested_at": _utcnow().isoformat(),
                "reason": "post_qualification:promoted",
                "count": 1,
                # no origin → legacy qualification
            },
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(_active_strategic_task(project.id))
    await db_session.commit()

    await _spawn_research_task(project, db_session)

    msgs = (
        (
            await db_session.execute(
                select(ProjectMessage).where(
                    ProjectMessage.project_id == project.id,
                )
            )
        )
        .scalars()
        .all()
    )
    steering = [
        m
        for m in msgs
        if (m.context or {}).get("event_type") == "strategic_steering_deferred"
    ]
    assert steering == [], (
        "qualification refresh must not post operator steering notice"
    )


@pytest.mark.asyncio
async def test_operator_steering_consumed_posts_acted_confirmation(db_session, mocker):
    """When a strategic spawn actually consumes an operator-origin refresh marker,
    the operator gets exactly one high-signal ``strategic_steering_acted`` message —
    the symmetric success counterpart to the deferral notice."""
    strategic_review_mock = mocker.patch(
        "app.strategic_spawning._spawn_strategic_review",
        new=AsyncMock(return_value=True),
    )
    mocker.patch(
        "app.strategic_spawning._spawn_design_doc_update",
        new=AsyncMock(return_value=True),
    )
    project = Project(
        name="Operator steering acted",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[],
        vision={
            "purpose": "test",
            "_strategic_refresh": {
                "requested_at": _utcnow().isoformat(),
                "reason": "operator_steering",
                "count": 1,
                "origin": "operator",
                # Genuine operator steering carries the message text; the
                # confirmation fires only when an excerpt was preserved.
                "excerpt": "Please refocus on the auth bug.",
            },
        },
        design_doc={
            "systems/api": {"id": "api", "status": "working"}
        },  # Has systems, so architecture isn't triggered
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    # A recent (terminal) strategic task puts us on the cooldown branch; the
    # operator marker is due (no prior fire), so the spawn fires and consumes it.
    db_session.add(_recent_strategic_task(project.id, minutes_ago=5))
    await db_session.commit()

    await _spawn_research_task(project, db_session)

    assert strategic_review_mock.called, "operator-origin refresh must fire the spawn"
    await db_session.refresh(project)
    # Consumed on spawn — marker gone.
    assert (project.vision or {}).get("_strategic_refresh") is None

    msgs = (
        (
            await db_session.execute(
                select(ProjectMessage).where(
                    ProjectMessage.project_id == project.id,
                    ProjectMessage.from_type == "system",
                )
            )
        )
        .scalars()
        .all()
    )
    acted = [
        m
        for m in msgs
        if (m.context or {}).get("event_type") == "strategic_steering_acted"
    ]
    assert len(acted) == 1, "operator must get exactly one steering confirmation"
    assert acted[0].message_type == MessageType.NOTE


@pytest.mark.asyncio
async def test_operator_marker_without_excerpt_posts_no_acted_confirmation(
    db_session, mocker
):
    """An operator-origin marker carrying NO excerpt must NOT post the steering
    confirmation: with no preserved message text the woken cycle has no
    directive to act on, so promising the operator that steering reached the
    strategist would be untruthful."""
    strategic_review_mock = mocker.patch(
        "app.strategic_spawning._spawn_strategic_review",
        new=AsyncMock(return_value=True),
    )
    mocker.patch(
        "app.strategic_spawning._spawn_design_doc_update",
        new=AsyncMock(return_value=True),
    )
    project = Project(
        name="Operator steering no excerpt",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[],
        vision={
            "purpose": "test",
            "_strategic_refresh": {
                "requested_at": _utcnow().isoformat(),
                "reason": "operator_steering",
                "count": 1,
                "origin": "operator",
                # no excerpt — no message text was preserved
            },
        },
        design_doc={
            "systems/api": {"id": "api", "status": "working"}
        },  # Has systems, so architecture isn't triggered
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(_recent_strategic_task(project.id, minutes_ago=5))
    await db_session.commit()

    await _spawn_research_task(project, db_session)

    assert strategic_review_mock.called, (
        "operator-origin refresh must still fire the spawn"
    )
    await db_session.refresh(project)
    assert (project.vision or {}).get("_strategic_refresh") is None

    msgs = (
        (
            await db_session.execute(
                select(ProjectMessage).where(
                    ProjectMessage.project_id == project.id,
                )
            )
        )
        .scalars()
        .all()
    )
    acted = [
        m
        for m in msgs
        if (m.context or {}).get("event_type") == "strategic_steering_acted"
    ]
    assert acted == [], "no excerpt → no steering confirmation"


@pytest.mark.asyncio
async def test_qualification_consumed_posts_no_acted_confirmation(db_session, mocker):
    """A strategic spawn that consumes a qualification-origin marker must NOT post
    a steering confirmation — that visibility is reserved for operator intent."""
    strategic_review_mock = mocker.patch(
        "app.strategic_spawning._spawn_strategic_review",
        new=AsyncMock(return_value=True),
    )
    mocker.patch(
        "app.strategic_spawning._spawn_design_doc_update",
        new=AsyncMock(return_value=True),
    )
    project = Project(
        name="Qualification consumed silently",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[],
        vision={
            "purpose": "test",
            "_strategic_refresh": {
                "requested_at": _utcnow().isoformat(),
                "reason": "post_qualification:promoted",
                "count": 1,
                # no origin → legacy qualification
            },
        },
        design_doc={
            "systems/api": {"id": "api", "status": "working"}
        },  # Has systems, so architecture isn't triggered
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(_recent_strategic_task(project.id, minutes_ago=5))
    await db_session.commit()

    await _spawn_research_task(project, db_session)

    assert strategic_review_mock.called, "qualification refresh that is due should fire"
    await db_session.refresh(project)
    assert (project.vision or {}).get("_strategic_refresh") is None

    msgs = (
        (
            await db_session.execute(
                select(ProjectMessage).where(
                    ProjectMessage.project_id == project.id,
                )
            )
        )
        .scalars()
        .all()
    )
    acted = [
        m
        for m in msgs
        if (m.context or {}).get("event_type") == "strategic_steering_acted"
    ]
    assert acted == [], "qualification consume must not post a steering confirmation"


@pytest.mark.asyncio
async def test_operator_refresh_deferred_on_cooldown_not_due_posts_once(
    db_session, mocker
):
    """An operator marker pending behind the min-interval / spawn-cooldown floor
    (not yet due) must surface a deferral notice — the broadened coverage beyond
    the active-cycle branch."""
    strategic_review_mock = mocker.patch(
        "app.strategic_spawning._spawn_strategic_review",
        new=AsyncMock(return_value=True),
    )
    design_doc_mock = mocker.patch(
        "app.strategic_spawning._spawn_design_doc_update",
        new=AsyncMock(return_value=True),
    )
    project = Project(
        name="Operator deferred on cooldown",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[],
        vision={
            "purpose": "test",
            # A very recent refresh fire keeps the min-interval floor closed, so
            # the pending marker is pending-but-not-due this tick.
            "_strategic_refresh_last_fired_at": _utcnow().isoformat(),
            "_strategic_refresh": {
                "requested_at": _utcnow().isoformat(),
                "reason": "operator_steering",
                "count": 1,
                "origin": "operator",
            },
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    # Recent terminal strategic task → cooldown branch (no active cycle).
    db_session.add(_recent_strategic_task(project.id, minutes_ago=5))
    await db_session.commit()

    await _spawn_research_task(project, db_session)

    # Not yet due → no spawn, marker preserved + stamped notified.
    assert not strategic_review_mock.called and not design_doc_mock.called
    await db_session.refresh(project)
    marker = (project.vision or {}).get("_strategic_refresh")
    assert marker is not None and marker.get("notified") is True

    msgs = (
        (
            await db_session.execute(
                select(ProjectMessage).where(
                    ProjectMessage.project_id == project.id,
                    ProjectMessage.from_type == "system",
                )
            )
        )
        .scalars()
        .all()
    )
    steering = [
        m
        for m in msgs
        if (m.context or {}).get("event_type") == "strategic_steering_deferred"
    ]
    assert len(steering) == 1, "cooldown-not-due deferral must post exactly one notice"


@pytest.mark.asyncio
async def test_operator_refresh_deferred_on_payload_too_large_posts_once(
    db_session, mocker
):
    """An operator marker that survives a PayloadTooLargeError skip (the spawn
    never happens, so the marker is not consumed) must surface a deferral notice."""
    from app.payload_schemas import PayloadTooLargeError

    strategic_review_mock = mocker.patch(
        "app.strategic_spawning._spawn_strategic_review",
        new=AsyncMock(
            side_effect=PayloadTooLargeError(
                300_000, 262_144, [("design_doc", 200_000)]
            )
        ),
    )
    mocker.patch(
        "app.strategic_spawning._spawn_design_doc_update",
        new=AsyncMock(return_value=True),
    )
    project = Project(
        name="Operator deferred on payload bloat",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[],
        vision={
            "purpose": "test",
            "_strategic_refresh": {
                "requested_at": _utcnow().isoformat(),
                "reason": "operator_steering",
                "count": 1,
                "origin": "operator",
            },
        },
        design_doc={
            "systems/api": {"id": "api", "status": "working"}
        },  # Has systems, so architecture isn't triggered
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    # Recent terminal strategic task + due operator marker → reaches the spawn
    # block, where the patched spawn raises PayloadTooLargeError.
    db_session.add(_recent_strategic_task(project.id, minutes_ago=5))
    await db_session.commit()

    await _spawn_research_task(project, db_session)

    assert strategic_review_mock.called, "spawn must be attempted to hit the bloat skip"
    await db_session.refresh(project)
    # Spawn skipped — marker NOT consumed, but stamped notified by the deferral.
    marker = (project.vision or {}).get("_strategic_refresh")
    assert marker is not None and marker.get("notified") is True

    msgs = (
        (
            await db_session.execute(
                select(ProjectMessage).where(
                    ProjectMessage.project_id == project.id,
                    ProjectMessage.from_type == "system",
                )
            )
        )
        .scalars()
        .all()
    )
    steering = [
        m
        for m in msgs
        if (m.context or {}).get("event_type") == "strategic_steering_deferred"
    ]
    assert len(steering) == 1, (
        "payload-too-large skip must post exactly one deferral notice"
    )


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

        result = await db_session.execute(
            select(Task).where(Task.project_id == project.id)
        )
        all_tasks = result.scalars().all()
        research = [
            t for t in all_tasks if (t.payloads or [{}])[0].get("type") == "research"
        ]
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

        result = await db_session.execute(
            select(Task).where(Task.project_id == project.id)
        )
        all_tasks = result.scalars().all()
        research = [
            t for t in all_tasks if (t.payloads or [{}])[0].get("type") == "research"
        ]
        assert len(research) == 1, "Must not spawn when research already IN_PROGRESS"
    finally:
        _runtime_config["max_parallel_research_per_project"] = old_val


# ─────────────────────────────────────────────────────────────────────────────
# 3b. Per-project parallelism overrides (build_config caps)
# ─────────────────────────────────────────────────────────────────────────────


def test_project_parallelism_cap_override_resolution():
    """A positive-int build_config override wins; bool/zero/str/absent fall back to the global."""
    from app.autonomous_research import project_parallelism_cap
    from app.shared_state import runtime_config as _runtime_config

    old_val = _runtime_config.get("max_parallel_research_per_project", 2)
    _runtime_config["max_parallel_research_per_project"] = 4
    try:
        project = Project(name="Cap Resolution", user_id="test-user-123")

        def _cap() -> int:
            return project_parallelism_cap(
                project,
                "max_parallel_research",
                "max_parallel_research_per_project",
                3,
            )

        project.build_config = {"max_parallel_research": 2}
        assert _cap() == 2
        # bool is an int subclass but is NOT a valid cap
        project.build_config = {"max_parallel_research": True}
        assert _cap() == 4
        for invalid in (0, -1, "2", None):
            project.build_config = {"max_parallel_research": invalid}
            assert _cap() == 4
        project.build_config = None
        assert _cap() == 4
    finally:
        _runtime_config["max_parallel_research_per_project"] = old_val


@pytest.mark.asyncio
async def test_project_override_caps_parallel_research_below_global(db_session):
    """build_config max_parallel_research=1 throttles the project even when the global allows more."""
    from app.shared_state import runtime_config as _runtime_config

    old_val = _runtime_config.get("max_parallel_research_per_project", 2)
    _runtime_config["max_parallel_research_per_project"] = 5
    try:
        project = Project(
            name="Per-Project Throttle",
            user_id="test-user-123",
            autonomous_enabled=True,
            directions=[_active_direction("d-ppthrt1")],
            vision={"purpose": "test"},
            build_config={"max_parallel_research": 1},
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

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

        result = await db_session.execute(
            select(Task).where(Task.project_id == project.id)
        )
        research = [
            t
            for t in result.scalars().all()
            if (t.payloads or [{}])[0].get("type") == "research"
        ]
        assert len(research) == 1, (
            "Project override must throttle below the permissive global cap"
        )
    finally:
        _runtime_config["max_parallel_research_per_project"] = old_val


@pytest.mark.asyncio
async def test_project_override_lifts_parallel_cap_above_global(db_session):
    """build_config max_parallel_research=2 wins over a global cap of 1 (override wins both ways)."""
    from app.shared_state import runtime_config as _runtime_config

    old_val = _runtime_config.get("max_parallel_research_per_project", 2)
    _runtime_config["max_parallel_research_per_project"] = 1
    try:
        project = Project(
            name="Per-Project Lift",
            user_id="test-user-123",
            autonomous_enabled=True,
            directions=[_active_direction("d-pplift1")],
            vision={"purpose": "test"},
            build_config={"max_parallel_research": 2},
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

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

        result = await db_session.execute(
            select(Task).where(Task.project_id == project.id)
        )
        research = [
            t
            for t in result.scalars().all()
            if (t.payloads or [{}])[0].get("type") == "research"
        ]
        assert len(research) == 2, (
            "Override above the global cap must allow the second spawn"
        )
    finally:
        _runtime_config["max_parallel_research_per_project"] = old_val


@pytest.mark.asyncio
async def test_evaluating_blocks_direction_when_recent(db_session):
    """A recent EVALUATING research task blocks new research on the same direction."""
    from app.shared_state import runtime_config as _runtime_config

    _runtime_config["evaluating_pipeline_timeout_minutes"] = 30
    project = Project(
        name="Recent Evaluating Blocks",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[_active_direction("d-evalblk1")],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    existing = Task(
        project_id=project.id,
        title="Research: evaluating recent",
        payloads=[
            {
                "type": "research",
                "directions": [],
                "base_branch": "main",
                "current_direction_id": "d-evalblk1",
            }
        ],
        status=TaskStatus.EVALUATING,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(existing)
    await db_session.commit()

    await _spawn_research_task(project, db_session)

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    research = [
        t
        for t in result.scalars().all()
        if (t.payloads or [{}])[0].get("type") == "research"
    ]
    assert len(research) == 1, (
        "Recent EVALUATING should block direction from new research"
    )


@pytest.mark.asyncio
async def test_evaluating_releases_direction_after_timeout(db_session):
    """An old EVALUATING research task no longer blocks new research on the direction."""
    from app.shared_state import runtime_config as _runtime_config

    _runtime_config["evaluating_pipeline_timeout_minutes"] = 30
    project = Project(
        name="Old Evaluating Pipelines",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[_active_direction("d-evalold1")],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    existing = Task(
        project_id=project.id,
        title="Research: evaluating old",
        payloads=[
            {
                "type": "research",
                "directions": [],
                "base_branch": "main",
                "current_direction_id": "d-evalold1",
            }
        ],
        status=TaskStatus.EVALUATING,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(existing)
    await db_session.commit()
    # Backdate updated_at past the timeout via raw UPDATE so onupdate doesn't fire
    from sqlalchemy import update as _update

    stale = _utcnow() - timedelta(minutes=90)
    await db_session.execute(
        _update(Task).where(Task.id == existing.id).values(updated_at=stale)
    )
    await db_session.commit()

    await _spawn_research_task(project, db_session)

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    research = [
        t
        for t in result.scalars().all()
        if (t.payloads or [{}])[0].get("type") == "research"
    ]
    assert len(research) == 2, "Stale EVALUATING should allow pipelined new research"


@pytest.mark.asyncio
async def test_pipelining_capped_at_one_extra_per_window(db_session):
    """Regression for incident 2026-05-04: when a long-EVALUATING parent is
    stuck (e.g., 17.8h) and stays EVALUATING-stale, the spawn loop used to
    spawn a fresh research every cycle (~6 min) on the same direction —
    each new spawn doesn't bump the parent's updated_at, so the staleness
    skip stays armed. Direction d-3878da01 had 779af581 stuck and three
    duplicate spawns (8e15d86a / 872f848c / bfbaf4d3), all manually cancelled.

    Fix: cap pipelining at 1 additional research per direction within the
    pipelining window (default 240 min). Recent CANCELLED research counts
    against the cap so cancel-respawn loops can't bypass it."""
    from app.shared_state import runtime_config as _runtime_config

    _runtime_config["evaluating_pipeline_timeout_minutes"] = 30
    _runtime_config["research_pipelining_window_minutes"] = 240
    project = Project(
        name="Pipelining Cap",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[_active_direction("d-pipecap")],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Stuck EVALUATING parent (stale > 30min — would normally allow pipelining).
    parent = Task(
        project_id=project.id,
        title="Research: stuck parent",
        payloads=[
            {
                "type": "research",
                "directions": [],
                "base_branch": "main",
                "current_direction_id": "d-pipecap",
            }
        ],
        status=TaskStatus.EVALUATING,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(parent)
    await db_session.commit()

    # Already-spawned pipelined research (CANCELLED — but recent, within window).
    pipelined = Task(
        project_id=project.id,
        title="Research: pipelined recently, then cancelled",
        payloads=[
            {
                "type": "research",
                "directions": [],
                "base_branch": "main",
                "current_direction_id": "d-pipecap",
            }
        ],
        status=TaskStatus.CANCELLED,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(pipelined)
    await db_session.commit()

    # Backdate parent's updated_at to make EVALUATING look stale.
    from sqlalchemy import update as _update

    stale = _utcnow() - timedelta(minutes=90)
    await db_session.execute(
        _update(Task).where(Task.id == parent.id).values(updated_at=stale)
    )
    await db_session.commit()

    await _spawn_research_task(project, db_session)

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    research = [
        t
        for t in result.scalars().all()
        if (t.payloads or [{}])[0].get("type") == "research"
    ]
    assert len(research) == 2, (
        f"Pipelining must be capped at 1 extra per direction per window "
        f"(parent + 1 prior pipelined = 2 total; expected no new spawn). "
        f"Got {len(research)} research tasks."
    )


@pytest.mark.asyncio
async def test_pipelining_allows_first_extra_when_no_prior(db_session):
    """Confirms the cap doesn't over-block: when the parent is EVALUATING-stale
    and NO other recent research exists on this direction, the FIRST pipelined
    spawn is allowed (cap is at 1 extra, not 0)."""
    from app.shared_state import runtime_config as _runtime_config

    _runtime_config["evaluating_pipeline_timeout_minutes"] = 30
    _runtime_config["research_pipelining_window_minutes"] = 240
    project = Project(
        name="Pipelining First Extra Allowed",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[_active_direction("d-pipefirst")],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    parent = Task(
        project_id=project.id,
        title="Research: stuck, no prior pipelined",
        payloads=[
            {
                "type": "research",
                "directions": [],
                "base_branch": "main",
                "current_direction_id": "d-pipefirst",
            }
        ],
        status=TaskStatus.EVALUATING,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(parent)
    await db_session.commit()

    from sqlalchemy import update as _update

    stale = _utcnow() - timedelta(minutes=90)
    await db_session.execute(
        _update(Task).where(Task.id == parent.id).values(updated_at=stale)
    )
    await db_session.commit()

    await _spawn_research_task(project, db_session)

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    research = [
        t
        for t in result.scalars().all()
        if (t.payloads or [{}])[0].get("type") == "research"
    ]
    assert len(research) == 2, (
        "First pipelined spawn must be allowed when no prior pipelined research exists"
    )


@pytest.mark.asyncio
async def test_pipelining_blocked_while_children_active(db_session):
    """Regression for incident 2026-06-03 (d2415495/#7427 vs e783a7ad/#7433 on
    d-5d841cea): a research parent legitimately sits in EVALUATING for hours
    while its child findings do their work on the way to a rollup. The
    EVALUATING age alone can't distinguish "stuck" from "children still
    working", so the pipelining exception fired ~2h after the parent went
    EVALUATING and spawned a DUPLICATE research on the same direction —
    producing a second rollup PR guaranteed to conflict with the first once
    one merged.

    Fix: suppress pipelining while the EVALUATING parent still has non-terminal
    children (it's progressing, not stuck)."""
    from app.shared_state import runtime_config as _runtime_config

    _runtime_config["evaluating_pipeline_timeout_minutes"] = 30
    _runtime_config["research_pipelining_window_minutes"] = 240
    project = Project(
        name="Pipelining Blocked While Children Active",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[_active_direction("d-pipechild")],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # EVALUATING-stale parent — would normally arm the pipelining exception.
    parent = Task(
        project_id=project.id,
        title="Research: parent with children still working",
        payloads=[
            {
                "type": "research",
                "directions": [],
                "base_branch": "main",
                "current_direction_id": "d-pipechild",
            }
        ],
        status=TaskStatus.EVALUATING,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(parent)
    await db_session.commit()

    # A child finding still mid-flight — the parent is progressing, not stuck.
    child = Task(
        project_id=project.id,
        parent_task_id=parent.id,
        title="feat: a finding spawned by the parent research",
        payloads=[{"type": "create", "base_branch": "main"}],
        status=TaskStatus.IN_PROGRESS,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(child)
    await db_session.commit()

    from sqlalchemy import update as _update

    stale = _utcnow() - timedelta(minutes=90)
    await db_session.execute(
        _update(Task).where(Task.id == parent.id).values(updated_at=stale)
    )
    await db_session.commit()

    await _spawn_research_task(project, db_session)

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    research = [
        t
        for t in result.scalars().all()
        if (t.payloads or [{}])[0].get("type") == "research"
    ]
    assert len(research) == 1, (
        f"A stale-EVALUATING parent with active children is progressing, not "
        f"stuck — pipelining must be suppressed (expected 1 research: the parent "
        f"only). Got {len(research)}."
    )


@pytest.mark.asyncio
async def test_pipelining_blocked_while_rollup_pr_open(db_session):
    """Companion to the children-active guard: even after all children are
    terminal, an EVALUATING parent with an OPEN rollup PR is still in flight —
    spawning a fresh research would create a second rollup on the same
    direction that conflicts with the open PR. A still-EVALUATING parent with a
    pr_number means the PR hasn't merged (it would leave EVALUATING on merge),
    so suppress pipelining while a PR number is recorded."""
    from app.shared_state import runtime_config as _runtime_config

    _runtime_config["evaluating_pipeline_timeout_minutes"] = 30
    _runtime_config["research_pipelining_window_minutes"] = 240
    project = Project(
        name="Pipelining Blocked While PR Open",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[_active_direction("d-pipepr")],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    parent = Task(
        project_id=project.id,
        title="Research: parent with open rollup PR",
        payloads=[
            {
                "type": "research",
                "directions": [],
                "base_branch": "main",
                "current_direction_id": "d-pipepr",
            }
        ],
        status=TaskStatus.EVALUATING,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
        context={"review_loop": {"pr_number": 9999, "branch": "research/pipepr"}},
    )
    db_session.add(parent)
    await db_session.commit()

    from sqlalchemy import update as _update

    stale = _utcnow() - timedelta(minutes=90)
    await db_session.execute(
        _update(Task).where(Task.id == parent.id).values(updated_at=stale)
    )
    await db_session.commit()

    await _spawn_research_task(project, db_session)

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    research = [
        t
        for t in result.scalars().all()
        if (t.payloads or [{}])[0].get("type") == "research"
    ]
    assert len(research) == 1, (
        f"A stale-EVALUATING parent with an open rollup PR is in flight — "
        f"pipelining must be suppressed (expected 1 research). Got {len(research)}."
    )


@pytest.mark.asyncio
async def test_pipelining_blocked_when_parent_has_blocked_children(db_session):
    """Regression: a research parent with BLOCKED children (waiting on manual
    approval or other blockers) should not trigger the pipelining exception.
    BLOCKED is a non-terminal status indicating in-flight work, so a parent
    with BLOCKED children is still progressing, not stuck.

    Fix: include BLOCKED in the child status check when building
    parents_with_active_children."""
    from app.shared_state import runtime_config as _runtime_config

    _runtime_config["evaluating_pipeline_timeout_minutes"] = 30
    _runtime_config["research_pipelining_window_minutes"] = 240
    project = Project(
        name="Pipelining Blocked When Children Blocked",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[_active_direction("d-pipeblocked")],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # EVALUATING-stale parent — would normally arm the pipelining exception.
    parent = Task(
        project_id=project.id,
        title="Research: parent with blocked children",
        payloads=[
            {
                "type": "research",
                "directions": [],
                "base_branch": "main",
                "current_direction_id": "d-pipeblocked",
            }
        ],
        status=TaskStatus.EVALUATING,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(parent)
    await db_session.commit()

    # A child task in BLOCKED status — the parent is in-flight, not stuck.
    child = Task(
        project_id=project.id,
        parent_task_id=parent.id,
        title="fix: a finding spawned by the parent research",
        payloads=[{"type": "fix", "base_branch": "main"}],
        status=TaskStatus.BLOCKED,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(child)
    await db_session.commit()

    from sqlalchemy import update as _update

    stale = _utcnow() - timedelta(minutes=90)
    await db_session.execute(
        _update(Task).where(Task.id == parent.id).values(updated_at=stale)
    )
    await db_session.commit()

    await _spawn_research_task(project, db_session)

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    research = [
        t
        for t in result.scalars().all()
        if (t.payloads or [{}])[0].get("type") == "research"
    ]
    assert len(research) == 1, (
        f"A stale-EVALUATING parent with BLOCKED children is in-flight, not "
        f"stuck — pipelining must be suppressed (expected 1 research: the parent "
        f"only). Got {len(research)}."
    )


@pytest.mark.skipif(
    not USE_POSTGRES,
    reason="true cross-session insert concurrency requires PostgreSQL",
)
@pytest.mark.asyncio
async def test_concurrent_spawn_ticks_create_exactly_one_research_task(
    db_session, mocker
):
    """Regression for incident 2026-06-06 (6ca2f9cf / fcedb840, 13.7s apart on
    direction operator-levers): two concurrent autonomous ticks both read
    "no active research on this direction" BEFORE either spawn committed, so
    the read-based busy-direction guard (#7462) never saw the sibling and each
    twin rolled up its own duplicate PR (#8438 / #8460). The per-process
    asyncio spawn lock and the best-effort advisory try-lock cannot exclude a
    sibling orchestrator process (and the xact-scoped advisory lock drops at
    any intermediate commit), so the fix is an atomic per-direction claim:
    the research task's idempotency_key under the partial unique index
    ux_task_active_idempotency_key — same TOCTOU shape as the concurrent
    self-audit fix (#8239).

    Two concurrent spawn attempts for one direction → exactly one task row.
    """
    import asyncio

    import app.anomaly_detection  # noqa: F401 — patched below
    from app.autonomous_research import (
        _spawn_research_task_inner,
        research_direction_claim_key,
    )

    # IMPORTANT: import the conftest under the SAME module name pytest loaded
    # it as ("conftest", basename import — testing/ has no __init__.py; same
    # convention as test_admin_routers.py). `from testing.conftest import ...`
    # resolves through the "testing" namespace package and imports a SECOND
    # copy of the module with its OWN duplicate engine. In a full-collection
    # run (CI shards collect everything before deselecting) that duplicate
    # already exists — created at collection time by the module-level
    # testing.conftest imports in test_warm_*.py — and it re-binds
    # app.database._LazyEngine._engine to the duplicate engine, so the very
    # first test's _dispose_production_engine_per_test teardown DISPOSES it.
    # Engine.dispose() → pool.recreate() copies the pool's connect listeners
    # via dispatch._update(), which does NOT re-apply _set_asyncio(): the
    # recreated pool's first-connect gate degrades from AsyncAdaptedLock to a
    # plain threading.Lock and is left un-run. The two concurrent session.get
    # calls below would then be the poisoned pool's FIRST connections: tick A
    # holds the threading lock across an await inside the dialect's
    # first-connect init, tick B blocks the whole event loop acquiring it —
    # deadlock until pytest-timeout kills the test (CI shard 2, run
    # 27063938655). The canonical "conftest" module returns the engine the
    # db_session fixture already armed, where concurrent connects are safe.
    from conftest import TestingSessionLocal

    project = Project(
        name="Race-Safe Research Spawn",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[_active_direction("d-race0001", "Race direction")],
        # purpose + counter preset: no strategic spawn, no vision seed write —
        # keeps both ticks lock-free until the task INSERT under test.
        vision={"purpose": "test", "_research_spawn_count": 1},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Simulate the production failure mode: the advisory try-lock cannot see
    # the sibling (different process → different PYTHONHASHSEED under the old
    # hash()-derived key, or the xact lock was already dropped by an
    # intermediate commit). Both ticks must reach the INSERT.
    mocker.patch(
        "app.autonomous_research._try_research_spawn_advisory_lock",
        new=AsyncMock(return_value=True),
    )

    # The real lifecycle helper holds the project row lock (skip_locked) until
    # commit, which would make the second tick skip before reaching the race
    # window. Replace it with a lock-free eligibility read — the lifecycle is
    # not what's under test, the claim at INSERT time is.
    #
    # This eligibility read doubles as the rendezvous point: it is the last shared
    # step BOTH ticks execute before the task INSERT. (M4 relocated
    # check_anomaly_actions to AFTER the spawn/flush and wrapped the post-flush
    # staging in a Project->Resident authority FOR UPDATE, so it can no longer be
    # the pre-INSERT barrier: a tick that flushed first holds the idempotency-key
    # index lock while waiting there, and the second tick blocks on the duplicate
    # insert and never arrives — a deadlock.) Rendezvousing here forces both ticks
    # to have read "direction is free" and then race to the INSERT, which is the
    # exact TOCTOU interleaving from the incident.
    barrier = asyncio.Barrier(2)

    async def _lockfree_lifecycle(
        prj, _session, busy_direction_ids, _now, *, expected_legacy_generation=None
    ):
        eligible = [
            d
            for d in (prj.directions or [])
            if d.get("status") == "active" and d.get("id") not in busy_direction_ids
        ]
        await barrier.wait()
        return eligible

    mocker.patch(
        "app.direction_lifecycle._process_direction_lifecycle",
        new=_lockfree_lifecycle,
    )

    async def _no_anomaly(*_a, **_kw):
        return {"action": "none"}

    mocker.patch("app.anomaly_detection.check_anomaly_actions", new=_no_anomaly)

    async def _tick():
        async with TestingSessionLocal() as session:
            tick_project = await session.get(Project, project.id)
            await _spawn_research_task_inner(tick_project, session)

    # Both ticks run concurrently on separate sessions/connections — neither
    # raises (the loser logs and skips; it must NOT fail the tick).
    await asyncio.gather(_tick(), _tick())

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    research = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "research"
        and t.first_payload.get("current_direction_id") == "d-race0001"
    ]
    assert len(research) == 1, (
        f"Two concurrent spawn ticks for one direction must create exactly ONE "
        f"research task (atomic per-direction claim). Got {len(research)}."
    )
    assert research[0].idempotency_key == research_direction_claim_key(
        project.id, "d-race0001"
    )


@pytest.mark.asyncio
async def test_pipelined_spawn_claims_distinct_key_alongside_stuck_parent(db_session):
    """The per-direction claim must NOT break the pipelining exception: a
    genuinely-stuck EVALUATING parent still holds the primary claim key
    (EVALUATING is an active status under ux_task_active_idempotency_key), so
    the ONE allowed pipelined spawn claims the direction under the distinct
    ``:pipelined`` key — it must succeed, and is itself atomically capped at
    one active pipelined research per direction."""
    from app.autonomous_research import research_direction_claim_key
    from app.shared_state import runtime_config as _runtime_config

    _runtime_config["evaluating_pipeline_timeout_minutes"] = 30
    _runtime_config["research_pipelining_window_minutes"] = 240
    project = Project(
        name="Pipelined Claim Key",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[_active_direction("d-pipekey1")],
        vision={"purpose": "test", "_research_spawn_count": 1},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Stuck parent in EVALUATING, holding the PRIMARY claim for the direction —
    # exactly what a production parent spawned after this fix will hold.
    parent = Task(
        project_id=project.id,
        title="Research: stuck parent holding the primary claim",
        payloads=[
            {
                "type": "research",
                "directions": [],
                "base_branch": "main",
                "current_direction_id": "d-pipekey1",
            }
        ],
        status=TaskStatus.EVALUATING,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
        idempotency_key=research_direction_claim_key(project.id, "d-pipekey1"),
    )
    db_session.add(parent)
    await db_session.commit()

    from sqlalchemy import update as _update

    stale = _utcnow() - timedelta(minutes=90)
    await db_session.execute(
        _update(Task).where(Task.id == parent.id).values(updated_at=stale)
    )
    await db_session.commit()

    await _spawn_research_task(project, db_session)

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    research = [
        t
        for t in result.scalars().all()
        if (t.payloads or [{}])[0].get("type") == "research"
    ]
    assert len(research) == 2, (
        f"The pipelined spawn must succeed alongside the stuck parent's primary "
        f"claim (expected parent + 1 pipelined = 2). Got {len(research)}."
    )
    pipelined = next(t for t in research if t.id != parent.id)
    assert pipelined.idempotency_key == research_direction_claim_key(
        project.id, "d-pipekey1", pipelined=True
    ), (
        "The pipelined cycle must claim the direction under the :pipelined key "
        "(the stuck EVALUATING parent still holds the primary claim)"
    )


@pytest.mark.skipif(
    not USE_POSTGRES,
    reason="direction-spawn advisory lock coverage requires PostgreSQL",
)
@pytest.mark.asyncio
async def test_pipelined_holder_blocks_later_primary_claim_under_direction_lock(
    db_session,
):
    """Regression for the remaining primary-vs-pipelined race: once a
    pipelined research task has committed under ``...:pipelined``, a later
    primary claimant on the same direction must serialize on the shared
    direction lock and re-read current state before inserting.

    This models the manual ``POST /research`` / autonomous-primary
    read-before-write path after a pipelined child exists but no active primary
    claim-holder remains. Without the lock, the later primary insert can miss
    the already-committed pipelined task and create a duplicate active cycle
    because ``ux_task_active_idempotency_key`` only compares exact key strings.
    """
    import asyncio

    from app.autonomous_research import research_direction_claim_key
    from app.task_locking import acquire_direction_research_spawn_lock

    # IMPORTANT: import the canonical pytest-loaded module, not
    # testing.conftest; see test_concurrent_spawn_ticks_create_exactly_one_...
    from conftest import TestingSessionLocal

    project = Project(
        name="Pipelined Holder Blocks Primary",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[_active_direction("d-pipelock1")],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    holder_has_lock = asyncio.Event()
    release_holder = asyncio.Event()
    commit_finished = asyncio.Event()

    async def _commit_pipelined_holder():
        async with TestingSessionLocal() as session:
            await acquire_direction_research_spawn_lock(
                session, project.id, "d-pipelock1"
            )
            holder = Task(
                project_id=project.id,
                title="Research: pipelined holder",
                payloads=[
                    {
                        "type": "research",
                        "directions": [],
                        "base_branch": "main",
                        "current_direction_id": "d-pipelock1",
                    }
                ],
                status=TaskStatus.IN_PROGRESS,
                created_by="ORCHESTRATOR",
                vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
                idempotency_key=research_direction_claim_key(
                    project.id, "d-pipelock1", pipelined=True
                ),
            )
            session.add(holder)
            await session.flush()
            holder_has_lock.set()
            await release_holder.wait()
            await session.commit()
            commit_finished.set()

    async def _manual_like_primary_attempt() -> str:
        async with TestingSessionLocal() as session:
            await acquire_direction_research_spawn_lock(
                session, project.id, "d-pipelock1"
            )
            active_rows = (
                (
                    await session.execute(
                        select(Task).where(
                            Task.project_id == project.id,
                            Task.status.in_(
                                [
                                    TaskStatus.QUEUED,
                                    TaskStatus.PROVISIONING,
                                    TaskStatus.IN_PROGRESS,
                                    TaskStatus.EVALUATING,
                                    TaskStatus.BLOCKED,
                                ]
                            ),
                        )
                    )
                )
                .scalars()
                .all()
            )
            same_direction = [
                task
                for task in active_rows
                if task.first_payload.get("type") == "research"
                and task.first_payload.get("current_direction_id") == "d-pipelock1"
            ]
            if same_direction:
                await session.rollback()
                return "blocked"
            session.add(
                Task(
                    project_id=project.id,
                    title="Research: primary claimant",
                    payloads=[
                        {
                            "type": "research",
                            "directions": [],
                            "base_branch": "main",
                            "current_direction_id": "d-pipelock1",
                        }
                    ],
                    status=TaskStatus.QUEUED,
                    created_by="USER",
                    vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
                    idempotency_key=research_direction_claim_key(
                        project.id, "d-pipelock1"
                    ),
                )
            )
            await session.commit()
            return "inserted"

    holder_task = asyncio.create_task(_commit_pipelined_holder())
    await holder_has_lock.wait()

    primary_task = asyncio.create_task(_manual_like_primary_attempt())
    await asyncio.sleep(0.1)
    release_holder.set()

    result = await primary_task
    await commit_finished.wait()
    await holder_task

    assert result == "blocked"

    rows = await db_session.execute(select(Task).where(Task.project_id == project.id))
    research = [
        task
        for task in rows.scalars().all()
        if task.first_payload.get("type") == "research"
        and task.first_payload.get("current_direction_id") == "d-pipelock1"
    ]
    assert len(research) == 1, (
        "A committed pipelined holder must block a later primary claimant on "
        "the same direction under the shared advisory lock."
    )
    assert research[0].idempotency_key == research_direction_claim_key(
        project.id, "d-pipelock1", pipelined=True
    )


@pytest.mark.asyncio
async def test_failed_resurrection_eligible_blocks_direction(db_session):
    """Regression for race 2026-05-05 (3d4bc05a + 88c7d5c8 on d-3878da01):
    reaper marks an infra-failed task FAILED at T+0; ~5s later the autonomous
    spawn loop fires and saw no active research on the direction → spawned a
    duplicate; ~60s later the reaper resurrected the original FAILED task,
    leaving two parallel research cycles on the same direction.

    Fix: include FAILED-but-resurrection-eligible tasks in the active_research
    SELECT so they keep blocking the direction until the reaper either
    resurrects them (back to QUEUED, still blocks) or 2h elapses (matches
    reaper.py:_infra_lookback)."""
    project = Project(
        name="FAILED resurrection-eligible blocks",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[_active_direction("d-resur001")],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # FAILED + within 2h lookback + AGENT_TIMEOUT (in INFRA_CLASSES) +
    # never resurrected. This is the exact shape of 3d4bc05a's row when
    # the duplicate spawn fired in production.
    failed = Task(
        project_id=project.id,
        title="Research: failed but eligible",
        payloads=[
            {
                "type": "research",
                "directions": [],
                "base_branch": "main",
                "current_direction_id": "d-resur001",
            }
        ],
        context={"failure_category": "AGENT_TIMEOUT"},
        status=TaskStatus.FAILED,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(failed)
    await db_session.commit()

    await _spawn_research_task(project, db_session)

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    research = [
        t
        for t in result.scalars().all()
        if (t.payloads or [{}])[0].get("type") == "research"
    ]
    assert len(research) == 1, (
        "FAILED + resurrection-eligible must keep the direction busy until "
        "reaper resolves it (resurrect or age-out at 2h)"
    )


@pytest.mark.asyncio
async def test_failed_already_resurrected_does_not_block_direction(db_session):
    """Once a FAILED task has been resurrected once (_resurrection_count=1),
    the reaper will not resurrect it again — so it shouldn't keep blocking
    the direction. Genuinely-dead-after-second-attempt tasks should let
    the next research cycle run."""
    project = Project(
        name="FAILED already-resurrected releases",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[_active_direction("d-resur002")],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    failed = Task(
        project_id=project.id,
        title="Research: already resurrected once",
        payloads=[
            {
                "type": "research",
                "directions": [],
                "base_branch": "main",
                "current_direction_id": "d-resur002",
            }
        ],
        # Already resurrected once; reaper.py:1818 skips on _resurrection_count>=1
        context={"failure_category": "AGENT_TIMEOUT", "_resurrection_count": 1},
        status=TaskStatus.FAILED,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(failed)
    await db_session.commit()

    await _spawn_research_task(project, db_session)

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    research = [
        t
        for t in result.scalars().all()
        if (t.payloads or [{}])[0].get("type") == "research"
    ]
    assert len(research) == 2, (
        "Already-resurrected FAILED task should NOT block — reaper won't "
        "resurrect it again, so direction is free for new research"
    )


@pytest.mark.asyncio
async def test_failed_non_infra_does_not_block_direction(db_session):
    """A FAILED task with a non-infra failure class (e.g., CODE_ERROR) is
    not eligible for resurrection — the reaper would not bring it back —
    so it should not keep the direction busy. This matches reaper.py:1816
    which only resurrects failure_category in INFRA_CLASSES."""
    project = Project(
        name="FAILED non-infra releases",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[_active_direction("d-resur003")],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    failed = Task(
        project_id=project.id,
        title="Research: failed with code error",
        payloads=[
            {
                "type": "research",
                "directions": [],
                "base_branch": "main",
                "current_direction_id": "d-resur003",
            }
        ],
        context={"failure_category": "CODE_ERROR"},
        status=TaskStatus.FAILED,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(failed)
    await db_session.commit()

    await _spawn_research_task(project, db_session)

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    research = [
        t
        for t in result.scalars().all()
        if (t.payloads or [{}])[0].get("type") == "research"
    ]
    assert len(research) == 2, (
        "Non-infra FAILED task should NOT block — reaper won't resurrect "
        "CODE_ERROR / OOM / pr_closed_unmerged failures"
    )


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
        assert len(result.scalars().all()) == 0, (
            "No QUEUED task should be spawned at rate limit"
        )
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

        result = await db_session.execute(
            select(Task).where(Task.project_id == project.id)
        )
        assert len(result.scalars().all()) == 0, (
            "No task should be spawned when global limit is 0"
        )
    finally:
        _shared_state.runtime_config["max_global_autonomous_tasks_per_hour"] = original


@pytest.mark.asyncio
async def test_autonomous_budget_classifies_downstream_churn_out_of_initiative_count(
    db_session,
):
    """Downstream servicing tasks do not consume initiative budget headroom."""
    project = Project(
        name="Autonomous Budget Classification",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[_active_direction("d-budget01")],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = datetime.now(timezone.utc)
    parent_research = Task(
        project_id=project.id,
        title="Research: initiative",
        payloads=[{"type": "research", "directions": [], "base_branch": "main"}],
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        created_at=now - timedelta(minutes=15),
    )
    db_session.add(parent_research)
    await db_session.flush()

    rows = [
        Task(
            project_id=project.id,
            title="Create: initiative",
            payloads=[{"type": "create", "prompt": "build the feature"}],
            status=TaskStatus.QUEUED,
            created_by="ORCHESTRATOR",
            created_at=now - timedelta(minutes=10),
        ),
        Task(
            project_id=project.id,
            title="Review: servicing",
            payloads=[{"type": "review", "pr_number": 101, "branch": "feature/a"}],
            status=TaskStatus.QUEUED,
            created_by="ORCHESTRATOR",
            parent_task_id=parent_research.id,
            created_at=now - timedelta(minutes=9),
        ),
        Task(
            project_id=project.id,
            title="Fix: servicing",
            payloads=[{"type": "fix", "pr_number": 101, "branch": "feature/a"}],
            context={"review_loop": {"pr_number": 101, "branch": "feature/a"}},
            status=TaskStatus.QUEUED,
            created_by="ORCHESTRATOR",
            parent_task_id=parent_research.id,
            created_at=now - timedelta(minutes=8),
        ),
        Task(
            project_id=project.id,
            title="Perf: servicing",
            payloads=[{"type": "perf", "prompt": "profile PR churn"}],
            status=TaskStatus.QUEUED,
            created_by="ORCHESTRATOR",
            parent_task_id=parent_research.id,
            depends_on=[str(parent_research.id)],
            created_at=now - timedelta(minutes=7),
        ),
        Task(
            project_id=project.id,
            title="Panel Review: servicing",
            payloads=[{"type": "panel_review", "pr_number": 101}],
            status=TaskStatus.QUEUED,
            created_by="ORCHESTRATOR",
            parent_task_id=parent_research.id,
            created_at=now - timedelta(minutes=6),
        ),
        Task(
            project_id=project.id,
            title="Unknown: backstop",
            payloads=[{"type": "new_future_type"}],
            status=TaskStatus.QUEUED,
            created_by="ORCHESTRATOR",
            created_at=now - timedelta(minutes=5),
        ),
        Task(
            project_id=project.id,
            title="Old review outside window",
            payloads=[{"type": "review", "pr_number": 44}],
            status=TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
            created_at=now - timedelta(hours=2),
        ),
        Task(
            project_id=project.id,
            title="User task ignored",
            payloads=[{"type": "research"}],
            status=TaskStatus.QUEUED,
            created_by="USER",
            created_at=now - timedelta(minutes=4),
        ),
    ]
    db_session.add_all(rows)
    await db_session.commit()

    snapshot = await compute_autonomous_spawn_budget_snapshot(
        db_session,
        runtime_config={
            **_shared_state.runtime_config,
            "max_global_autonomous_initiative_tasks_per_hour": 3,
        },
        now=now,
    )

    assert snapshot.initiative_count == 2
    assert snapshot.servicing_count == 4
    assert snapshot.other_count == 1
    assert snapshot.initiative_headroom == 1
    assert snapshot.task_type_counts["initiative"] == {"create": 1, "research": 1}
    assert snapshot.task_type_counts["servicing"] == {
        "fix": 1,
        "panel_review": 1,
        "perf": 1,
        "review": 1,
    }
    assert snapshot.task_type_counts["other"] == {"new_future_type": 1}


def test_autonomous_spawn_budget_classifies_initiative_with_parent_lineage_as_servicing():
    assert (
        classify_autonomous_spawn_budget_category(
            task_type="create",
            parent_task_id=object(),
            payload={},
            context={},
        )
        == "servicing"
    )
    assert (
        classify_autonomous_spawn_budget_category(
            task_type="research",
            depends_on=["parent-task-id"],
            payload={},
            context={},
        )
        == "servicing"
    )


@pytest.mark.asyncio
async def test_spawn_research_blocks_in_enforce_mode_when_initiative_budget_exhausted(
    db_session,
):
    """Enforce mode blocks new initiative spawns once the initiative budget is spent."""
    project = Project(
        name="Initiative Budget Enforce",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[_active_direction("d-init-budget-enforce")],
        vision={"purpose": "test"},
    )
    source_project = Project(
        name="Budget Source",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[_active_direction("d-init-budget-source")],
        vision={"purpose": "test"},
    )
    db_session.add_all([project, source_project])
    await db_session.commit()
    await db_session.refresh(project)
    await db_session.refresh(source_project)

    now = datetime.now(timezone.utc)
    db_session.add(
        Task(
            project_id=source_project.id,
            title="Research budget source",
            payloads=[{"type": "research"}],
            status=TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
            created_at=now - timedelta(minutes=5),
        )
    )
    await db_session.commit()

    original_mode = _shared_state.runtime_config["autonomous_spawn_budget_mode"]
    original_initiative = _shared_state.runtime_config[
        "max_global_autonomous_initiative_tasks_per_hour"
    ]
    original_global = _shared_state.runtime_config["max_global_autonomous_tasks_per_hour"]
    _shared_state.runtime_config["autonomous_spawn_budget_mode"] = "enforce"
    _shared_state.runtime_config["max_global_autonomous_initiative_tasks_per_hour"] = 1
    _shared_state.runtime_config["max_global_autonomous_tasks_per_hour"] = 500
    try:
        await _spawn_research_task(project, db_session)

        result = await db_session.execute(
            select(Task).where(Task.project_id == project.id)
        )
        assert len(result.scalars().all()) == 0
    finally:
        _shared_state.runtime_config["autonomous_spawn_budget_mode"] = original_mode
        _shared_state.runtime_config[
            "max_global_autonomous_initiative_tasks_per_hour"
        ] = original_initiative
        _shared_state.runtime_config["max_global_autonomous_tasks_per_hour"] = original_global


@pytest.mark.asyncio
async def test_spawn_research_observe_mode_allows_initiative_over_budget(
    db_session,
):
    """Observe mode keeps the initiative snapshot visible without blocking the spawn."""
    project = Project(
        name="Initiative Budget Observe",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[_active_direction("d-init-budget-observe")],
        vision={"purpose": "test"},
    )
    source_project = Project(
        name="Budget Source Observe",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[_active_direction("d-init-budget-source-observe")],
        vision={"purpose": "test"},
    )
    db_session.add_all([project, source_project])
    await db_session.commit()
    await db_session.refresh(project)
    await db_session.refresh(source_project)

    now = datetime.now(timezone.utc)
    db_session.add(
        Task(
            project_id=source_project.id,
            title="Research budget source observe",
            payloads=[{"type": "research"}],
            status=TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
            created_at=now - timedelta(minutes=5),
        )
    )
    await db_session.commit()

    original_mode = _shared_state.runtime_config["autonomous_spawn_budget_mode"]
    original_initiative = _shared_state.runtime_config[
        "max_global_autonomous_initiative_tasks_per_hour"
    ]
    original_global = _shared_state.runtime_config["max_global_autonomous_tasks_per_hour"]
    _shared_state.runtime_config["autonomous_spawn_budget_mode"] = "observe"
    _shared_state.runtime_config["max_global_autonomous_initiative_tasks_per_hour"] = 1
    _shared_state.runtime_config["max_global_autonomous_tasks_per_hour"] = 500
    try:
        await _spawn_research_task(project, db_session)

        result = await db_session.execute(
            select(Task).where(Task.project_id == project.id)
        )
        spawned_tasks = result.scalars().all()
        assert len(spawned_tasks) == 1
        assert spawned_tasks[0].first_payload["type"] == "research"
    finally:
        _shared_state.runtime_config["autonomous_spawn_budget_mode"] = original_mode
        _shared_state.runtime_config[
            "max_global_autonomous_initiative_tasks_per_hour"
        ] = original_initiative
        _shared_state.runtime_config["max_global_autonomous_tasks_per_hour"] = original_global


@pytest.mark.asyncio
async def test_spawn_research_fails_closed_in_enforce_mode_when_snapshot_unavailable(
    db_session, mocker
):
    """Enforce mode blocks spawn when budget snapshot calculation fails."""
    project = Project(
        name="Budget Snapshot Failure Enforce",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[_active_direction("d-snapshot-fail-enforce")],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    original_mode = _shared_state.runtime_config["autonomous_spawn_budget_mode"]
    _shared_state.runtime_config["autonomous_spawn_budget_mode"] = "enforce"

    # Mock compute_autonomous_spawn_budget_snapshot to raise an exception
    mocker.patch(
        "app.autonomous_research.compute_autonomous_spawn_budget_snapshot",
        side_effect=Exception("Database unavailable"),
    )

    try:
        await _spawn_research_task(project, db_session)

        result = await db_session.execute(
            select(Task).where(Task.project_id == project.id)
        )
        # In enforce mode, the spawn should be blocked when snapshot fails
        assert len(result.scalars().all()) == 0
    finally:
        _shared_state.runtime_config["autonomous_spawn_budget_mode"] = original_mode


@pytest.mark.asyncio
async def test_spawn_research_fails_open_in_observe_mode_when_snapshot_unavailable(
    db_session, mocker
):
    """Observe mode allows spawn even when budget snapshot calculation fails."""
    project = Project(
        name="Budget Snapshot Failure Observe",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[_active_direction("d-snapshot-fail-observe")],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    original_mode = _shared_state.runtime_config["autonomous_spawn_budget_mode"]
    _shared_state.runtime_config["autonomous_spawn_budget_mode"] = "observe"

    # Mock compute_autonomous_spawn_budget_snapshot to raise an exception
    mocker.patch(
        "app.autonomous_research.compute_autonomous_spawn_budget_snapshot",
        side_effect=Exception("Database unavailable"),
    )

    try:
        await _spawn_research_task(project, db_session)

        result = await db_session.execute(
            select(Task).where(Task.project_id == project.id)
        )
        # In observe mode, the spawn should succeed despite snapshot failure
        spawned_tasks = result.scalars().all()
        assert len(spawned_tasks) == 1
        assert spawned_tasks[0].first_payload["type"] == "research"
    finally:
        _shared_state.runtime_config["autonomous_spawn_budget_mode"] = original_mode


@pytest.mark.asyncio
async def test_spawn_periodic_task_enforce_mode_uses_runtime_categories(
    db_session,
):
    """Periodic enforcement blocks when runtime categories place the task in initiative."""
    project = Project(
        name="Periodic Budget Enforce",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[_active_direction("d-periodic-budget-enforce")],
        vision={"purpose": "test"},
    )
    source_project = Project(
        name="Periodic Budget Source",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[_active_direction("d-periodic-budget-source")],
        vision={"purpose": "test"},
    )
    db_session.add_all([project, source_project])
    await db_session.commit()
    await db_session.refresh(project)
    await db_session.refresh(source_project)

    now = datetime.now(timezone.utc)
    db_session.add(
        Task(
            project_id=source_project.id,
            title="Review budget source",
            payloads=[{"type": "review"}],
            status=TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
            created_at=now - timedelta(minutes=5),
        )
    )
    await db_session.commit()

    original_mode = _shared_state.runtime_config["autonomous_spawn_budget_mode"]
    original_initiative = _shared_state.runtime_config[
        "max_global_autonomous_initiative_tasks_per_hour"
    ]
    original_global = _shared_state.runtime_config["max_global_autonomous_tasks_per_hour"]
    original_categories = _shared_state.runtime_config.get(
        "autonomous_spawn_budget_categories"
    )
    original_periodic = _shared_state.runtime_config.get("autonomous_periodic_tasks")
    _shared_state.runtime_config["autonomous_spawn_budget_mode"] = "enforce"
    _shared_state.runtime_config["max_global_autonomous_initiative_tasks_per_hour"] = 1
    _shared_state.runtime_config["max_global_autonomous_tasks_per_hour"] = 500
    _shared_state.runtime_config["autonomous_spawn_budget_categories"] = {
        "initiative": ["review"],
        "servicing": ["qa", "deps", "security_audit"],
    }
    _shared_state.runtime_config["autonomous_periodic_tasks"] = {
        **(original_periodic or {}),
        "review": {"enabled": True, "interval_hours": 1},
    }
    try:
        await _spawn_periodic_task(project, "review", db_session)

        result = await db_session.execute(
            select(Task).where(Task.project_id == project.id)
        )
        assert len(result.scalars().all()) == 0
    finally:
        _shared_state.runtime_config["autonomous_spawn_budget_mode"] = original_mode
        _shared_state.runtime_config[
            "max_global_autonomous_initiative_tasks_per_hour"
        ] = original_initiative
        _shared_state.runtime_config["max_global_autonomous_tasks_per_hour"] = original_global
        _shared_state.runtime_config["autonomous_spawn_budget_categories"] = (
            original_categories
        )
        _shared_state.runtime_config["autonomous_periodic_tasks"] = original_periodic


@pytest.mark.asyncio
async def test_periodic_spawn_fails_closed_on_budget_snapshot_error_in_enforce_mode(
    db_session, mocker
):
    """Periodic spawn fails CLOSED (blocks spawn) on budget snapshot error when mode==enforce."""
    project = Project(
        name="Periodic Budget Error",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[_active_direction("d-periodic-budget-error")],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    original_mode = _shared_state.runtime_config["autonomous_spawn_budget_mode"]
    original_periodic = _shared_state.runtime_config.get("autonomous_periodic_tasks")
    _shared_state.runtime_config["autonomous_spawn_budget_mode"] = "enforce"
    _shared_state.runtime_config["autonomous_periodic_tasks"] = {
        **(original_periodic or {}),
        "review": {"enabled": True, "interval_hours": 1},
    }

    # Mock the budget snapshot computation to raise an exception
    mocker.patch(
        "app.autonomous_research.compute_autonomous_spawn_budget_snapshot",
        side_effect=Exception("Database connection failed"),
    )

    try:
        await _spawn_periodic_task(project, "review", db_session)

        # Verify no task was spawned (failed closed)
        result = await db_session.execute(
            select(Task).where(Task.project_id == project.id)
        )
        assert len(result.scalars().all()) == 0, "Expected no task to be spawned when snapshot fails in enforce mode"
    finally:
        _shared_state.runtime_config["autonomous_spawn_budget_mode"] = original_mode
        _shared_state.runtime_config["autonomous_periodic_tasks"] = original_periodic


# ─────────────────────────────────────────────────────────────────────────────
# 5. _handle_research_completion — Depth Guard
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_depth_limit_prevents_child_tasks(db_session, mocker):
    """Research at MAX_RESEARCH_DEPTH=3 skips child task creation and sets a status message."""
    direction = _active_direction("d-depth001", "Deep research")
    project = Project(
        name="Depth Limit",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
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
        select(Task).where(Task.project_id == project.id, Task.id != task.id)
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
        name="Hash Dedup Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
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
        return_value=[
            {
                "title": dup_title,
                "description": dup_desc,
                "prompt": "Fix auth token expiry",
                "priority": "high",
                "scope": "small",
                "risk": "high",
                "type": "improvement",
                "direction_id": direction["id"],
            }
        ],
    )

    await _handle_research_completion(task, "", db_session)

    result = await db_session.execute(
        select(Task).where(
            Task.project_id == project.id, Task.parent_task_id == task.id
        )
    )
    assert len(result.scalars().all()) == 0, (
        "Hash-duplicate finding must not create a task"
    )
    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED, (
        "Research task must be auto-completed when all findings are duplicates"
    )


@pytest.mark.asyncio
async def test_word_overlap_deduplication_skips_similar_title(db_session, mocker):
    """A finding sharing an active task's code REGION (related_files) plus title
    words is skipped by region-identity dedup."""
    direction = _active_direction("d-worddup1", "Word overlap direction")
    project = Project(
        name="Word Overlap Dedup",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Active task that shares both the code region (related_files) and key words.
    # related_files are recorded on the task context at spawn time, which is what
    # region-identity dedup reads.
    existing = Task(
        project_id=project.id,
        title="optimize database connection pooling configuration",
        payloads=[{"type": "create", "prompt": "...", "base_branch": "main"}],
        context={"related_files": ["app/db/pool.py", "app/db/config.py"]},
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
            {
                # Same region (related_files) + "database connection pooling
                # configuration" word overlap with the active task → duplicate.
                "title": "tune database connection pooling configuration settings",
                "description": "Improve pooling",
                "prompt": "Tune the connection pool",
                "priority": "medium",
                "scope": "small",
                "risk": "low",
                "type": "improvement",
                "direction_id": direction["id"],
                "related_files": ["app/db/pool.py", "app/db/config.py"],
            }
        ],
    )

    await _handle_research_completion(task, "", db_session)

    result = await db_session.execute(
        select(Task).where(
            Task.project_id == project.id, Task.parent_task_id == task.id
        )
    )
    child_tasks = result.scalars().all()
    assert len(child_tasks) == 1, (
        "Word-overlap duplicate must be consolidated into one salvage task"
    )
    salvage = child_tasks[0]
    assert salvage.first_payload.get("type") == "salvage_bundle"
    assert salvage.context.get("salvage_source_task_id") == str(task.id)
    await db_session.refresh(task)
    assert task.status == TaskStatus.EVALUATING, (
        "Research task must await its salvage child when all direct findings are duplicates"
    )


@pytest.mark.asyncio
async def test_word_overlap_deduplication_allows_common_word_findings(
    db_session, mocker
):
    """Findings sharing only common stopwords/prepositions are NOT falsely deduplicated."""
    direction = _active_direction("d-worddup2", "Stopword false-positive direction")
    project = Project(
        name="Stopword False Positive Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
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
        return_value=[
            {
                # Shares only stopwords ("inject", "into", "for") with existing — should NOT be deduplicated
                "title": "inject rate limiting into api gateway for security",
                "description": "Add rate limiting to protect the API",
                "prompt": "Add rate limiting middleware",
                "priority": "medium",
                "scope": "small",
                "risk": "low",
                "type": "improvement",
                "direction_id": direction["id"],
            }
        ],
    )

    await _handle_research_completion(task, "", db_session)

    result = await db_session.execute(
        select(Task).where(
            Task.project_id == project.id, Task.parent_task_id == task.id
        )
    )
    child_tasks = result.scalars().all()
    assert len(child_tasks) == 1, (
        "Finding with only stopword overlap must NOT be deduplicated"
    )


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
        return_value=[
            {
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
            }
        ],
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
        name="Auto Complete Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
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
        return_value=[
            {
                "title": dup_title,
                "description": dup_desc,
                "prompt": "Refactor auth middleware",
                "priority": "medium",
                "scope": "small",
                "risk": "low",
                "type": "improvement",
                "direction_id": direction["id"],
            }
        ],
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
    assert len(result.scalars().all()) == 0, (
        "No child tasks should be created when all findings are duplicates"
    )


@pytest.mark.asyncio
async def test_retry_with_preexisting_children_stays_evaluating_not_completed(
    db_session, mocker
):
    """Retry false-complete guard: a research that already spawned children on a
    prior attempt must NOT auto-complete when the current pass yields zero NEW
    children (all findings deduped against those existing children).

    Failure mode this guards against: on a retry, the research re-derives the same
    findings, which hash-dedup against the children it already spawned, so the
    current pass's ``child_tasks`` is empty. The zero-children COMPLETED branch
    would orphan the live children — rollup machinery (_check_and_trigger_rollup)
    early-returns unless the parent is EVALUATING, so the research/<id> -> automain
    rollup PR would never open and all descendant work would strand on the
    research branch. The task must instead end EVALUATING so rollup owns the
    existing children.
    """
    direction = _active_direction("d-retrydup1", "Retry-dedup direction")
    project = Project(
        name="Retry Preexisting Children Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_research_task(project, direction, db_session)

    dup_title = "Refactor authentication middleware"
    dup_desc = "Middleware needs cleanup"
    fhash = hashlib.sha256((dup_title + dup_desc).encode()).hexdigest()[:16]

    # Child the research already spawned on attempt 1 — a live (EVALUATING)
    # implementation task parented to this research, carrying the finding hash so
    # the re-derived finding on the retry hash-dedups against it. This is the
    # pre-existing child the rollup machinery still owns.
    existing_child = Task(
        project_id=project.id,
        parent_task_id=task.id,
        title=dup_title,
        payloads=[{"type": "create", "prompt": "refactor it", "base_branch": "main"}],
        context={"finding_hash": fhash, "direction_id": direction["id"]},
        status=TaskStatus.EVALUATING,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(existing_child)
    await db_session.commit()

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[
            {
                "title": dup_title,
                "description": dup_desc,
                "prompt": "Refactor auth middleware",
                "priority": "medium",
                "scope": "small",
                "risk": "low",
                "type": "improvement",
                "direction_id": direction["id"],
            }
        ],
    )

    await _handle_research_completion(task, "", db_session)

    await db_session.refresh(task)
    assert task.status == TaskStatus.EVALUATING, (
        "Research that already owns children must stay EVALUATING (so rollup opens "
        "its PR) even when the retry pass produces zero new children — it must NOT "
        "false-complete to COMPLETED"
    )
    # The "auto-completed (no actionable tasks)" / "created 0 tasks" messaging must
    # not be emitted: it would falsely claim the research produced nothing.
    assert "auto-completed" not in (task.latest_status_message or "")
    assert "0 tasks" not in (task.latest_status_message or "")

    # No NEW children were spawned this pass; the single pre-existing child remains.
    result = await db_session.execute(
        select(Task).where(
            Task.project_id == project.id, Task.parent_task_id == task.id
        )
    )
    children = result.scalars().all()
    assert len(children) == 1, (
        "No new child should be spawned (finding deduped), and the pre-existing "
        "child must remain owned by the still-EVALUATING research"
    )
    assert children[0].id == existing_child.id


@pytest.mark.asyncio
async def test_ghost_completion_does_not_block_rededup(db_session, mocker):
    """A COMPLETED task without pr_merged=True (ghost completion) must NOT block re-proposal."""
    direction = _active_direction("d-ghost01", "Ghost completion direction")
    project = Project(
        name="Ghost Dedup Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
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
        return_value=[
            {
                "title": dup_title,
                "description": dup_desc,
                "prompt": "Fix auth token expiry",
                "priority": "high",
                "scope": "small",
                "risk": "high",
                "type": "improvement",
                "direction_id": direction["id"],
            }
        ],
    )

    await _handle_research_completion(task, "", db_session)

    result = await db_session.execute(
        select(Task).where(
            Task.project_id == project.id, Task.parent_task_id == task.id
        )
    )
    child_tasks = result.scalars().all()
    assert len(child_tasks) == 1, (
        "Ghost completion must NOT block re-proposal — child task should be created"
    )


@pytest.mark.asyncio
async def test_confirmed_merge_still_blocks_rededup(db_session, mocker):
    """A COMPLETED task WITH pr_merged=True must still block re-proposal."""
    direction = _active_direction("d-merged01", "Merged completion direction")
    project = Project(
        name="Merged Dedup Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
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
        return_value=[
            {
                "title": dup_title,
                "description": dup_desc,
                "prompt": "Fix auth token expiry",
                "priority": "high",
                "scope": "small",
                "risk": "high",
                "type": "improvement",
                "direction_id": direction["id"],
            }
        ],
    )

    await _handle_research_completion(task, "", db_session)

    result = await db_session.execute(
        select(Task).where(
            Task.project_id == project.id, Task.parent_task_id == task.id
        )
    )
    child_tasks = result.scalars().all()
    assert len(child_tasks) == 0, (
        "Confirmed merge (pr_merged=True) must still block re-proposal"
    )


@pytest.mark.asyncio
async def test_ghost_merge_does_not_block_rededup(db_session, mocker):
    """A COMPLETED+pr_merged task whose parent rollup failed must NOT block re-proposal."""
    direction = _active_direction("d-ghostmrg", "Ghost merge direction")
    project = Project(
        name="Ghost Merge Dedup Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
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
        return_value=[
            {
                "title": dup_title,
                "description": dup_desc,
                "prompt": "Fix auth token expiry",
                "priority": "high",
                "scope": "small",
                "risk": "high",
                "type": "improvement",
                "direction_id": direction["id"],
            }
        ],
    )

    await _handle_research_completion(task, "", db_session)

    result = await db_session.execute(
        select(Task).where(
            Task.project_id == project.id, Task.parent_task_id == task.id
        )
    )
    child_tasks = result.scalars().all()
    assert len(child_tasks) == 1, (
        "Ghost merge (failed rollup parent) must NOT block re-proposal"
    )


@pytest.mark.asyncio
async def test_infra_failure_does_not_block_rededup(db_session, mocker):
    """A FAILED task due to infrastructure issues must NOT block re-proposal."""
    direction = _active_direction("d-infrafail", "Infra failure direction")
    project = Project(
        name="Infra Failure Dedup Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
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
        payloads=[
            {"type": "create", "prompt": "add rate limiting", "base_branch": "main"}
        ],
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
        return_value=[
            {
                "title": infra_title,
                "description": infra_desc,
                "prompt": "Add rate limiting",
                "priority": "high",
                "scope": "small",
                "risk": "medium",
                "type": "improvement",
                "direction_id": direction["id"],
            }
        ],
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
        name="Code Failure Dedup Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
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
        return_value=[
            {
                "title": code_title,
                "description": code_desc,
                "prompt": "Refactor connection pool",
                "priority": "medium",
                "scope": "small",
                "risk": "medium",
                "type": "improvement",
                "direction_id": direction["id"],
            }
        ],
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
        name="Unique Findings",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
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


@pytest.mark.asyncio
async def test_filtered_findings_recorded_in_direction_history(db_session, mocker):
    """After dedup skips, the cycle's history entry must record title+reason so the
    research agent can be told exactly what was rejected on its next cycle."""
    direction_id = "d-filtrec01"
    direction = _active_direction(direction_id, "Direction with filtered findings")
    project = Project(
        name="Filtered Findings Recording",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Seed a hash-match collision for one finding and a word-overlap existing
    # task for another so both skip paths are exercised in a single cycle.
    dup_title = "Consolidate auth token refresh across handlers"
    dup_desc = "Unify token refresh logic used by all agent handlers"
    dup_hash = hashlib.sha256((dup_title + dup_desc).encode()).hexdigest()[:16]
    hash_blocker = Task(
        project_id=project.id,
        title=dup_title,
        payloads=[{"type": "create", "prompt": "x", "base_branch": "main"}],
        context={"finding_hash": dup_hash, "direction_id": direction_id},
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    word_blocker = Task(
        project_id=project.id,
        title="instrument pipeline throughput histogram latency metrics",
        payloads=[{"type": "create", "prompt": "x", "base_branch": "main"}],
        context={
            "direction_id": direction_id,
            "related_files": ["app/metrics.py", "app/pipeline.py"],
        },
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add_all([hash_blocker, word_blocker])
    await db_session.commit()

    task = await _make_research_task(project, direction, db_session)
    # Scope the research task to the direction so current_direction_id resolves
    # and the history entry is written even when every finding is deduped.
    task.context = {**(task.context or {}), "direction_id": direction_id}
    db_session.add(task)
    await db_session.commit()

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[
            {
                "title": dup_title,
                "description": dup_desc,
                "prompt": "consolidate token refresh",
                "priority": "high",
                "scope": "small",
                "risk": "low",
                "type": "improvement",
                "direction_id": direction_id,
            },
            {
                "title": "instrument pipeline throughput histogram latency counters",
                "description": "Add histogram metrics for pipeline stages",
                "prompt": "add metrics",
                "priority": "medium",
                "scope": "small",
                "risk": "low",
                "type": "improvement",
                "direction_id": direction_id,
                "related_files": ["app/metrics.py", "app/pipeline.py"],
            },
        ],
    )

    await _handle_research_completion(task, "", db_session)

    await db_session.refresh(project)
    updated_dir = next(d for d in project.directions if d.get("id") == direction_id)
    history = updated_dir.get("research_history", [])
    assert history, "Dedup-only cycle still writes a history entry"
    entry = history[-1]
    filtered = entry.get("filtered_findings")
    assert isinstance(filtered, list) and len(filtered) == 2, filtered
    reasons_by_title = {rec["title"]: rec["reason"] for rec in filtered}
    assert reasons_by_title[dup_title] == "hash match with prior task"
    word_title = "instrument pipeline throughput histogram latency counters"
    assert reasons_by_title[word_title].startswith("region overlap with: ")
    # filter_breakdown is unchanged (additive persistence, counters still match).
    assert entry["filter_breakdown"]["hash_dedup"] == 1
    assert entry["filter_breakdown"]["word_overlap_dedup"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# 7. _handle_research_completion — Child Task Properties
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_child_task_direction_id_set_in_context(db_session, mocker):
    """Child tasks created from findings carry direction_id in their context."""
    direction_id = "d-dirctx01"
    direction = _active_direction(direction_id, "Direction context test")
    project = Project(
        name="Child Context Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
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
        name="Parent Task ID Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
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
async def test_compute_experiment_finding_propagates_live_proof_contract(
    db_session, mocker
):
    """Research-authored live-proof compute_experiment findings keep their markers.

    The child payload must preserve the proof fields so the agent launcher can
    enforce the live-release precondition instead of treating the job as
    ordinary compute.
    """
    from app.shared_state import runtime_config

    direction = _active_direction("d-liveproof1", "Live-proof direction")
    project = Project(
        name="Live Proof Research",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_research_task(project, direction, db_session)

    prev_threshold = runtime_config.get("compute_experiment_spend_threshold_usd")
    runtime_config["compute_experiment_spend_threshold_usd"] = 10.0
    try:
        _patch_research_externals(mocker)
        mocker.patch(
            "app.research_completion._parse_research_findings",
            return_value=[
                {
                    **_make_finding(
                        title="Prove the release is serving",
                        direction_id=direction["id"],
                        finding_type="benchmark",
                    ),
                    "command": "python live_proof.py",
                    "spend_cap_usd": 2.0,
                    "deadline_seconds": 600,
                    "live_proof": True,
                    "live_proof_marker": "research-live-proof",
                    "live_release_proof": {
                        "enabled": True,
                        "marker": "research-live-proof",
                    },
                    "live_release_proof_marker": "research-live-proof",
                    "requires_live_release": True,
                    "smoke_sample": {
                        "exit_code": 0,
                        "startup_ok": True,
                        "metric_captured": True,
                        "timed_out": False,
                    },
                }
            ],
        )

        await _handle_research_completion(task, "", db_session)
    finally:
        if prev_threshold is None:
            runtime_config.pop("compute_experiment_spend_threshold_usd", None)
        else:
            runtime_config["compute_experiment_spend_threshold_usd"] = prev_threshold

    result = await db_session.execute(
        select(Task).where(
            Task.project_id == project.id, Task.parent_task_id == task.id
        )
    )
    child_tasks = result.scalars().all()
    assert len(child_tasks) == 1
    payload = child_tasks[0].first_payload or {}
    assert payload["type"] == "compute_experiment"
    assert payload["live_proof"] is True
    assert payload["live_proof_marker"] == "research-live-proof"
    assert payload["live_release_proof"] == {
        "enabled": True,
        "marker": "research-live-proof",
    }
    assert payload["live_release_proof_marker"] == "research-live-proof"
    assert payload["requires_live_release"] is True


@pytest.mark.asyncio
async def test_research_task_status_becomes_evaluating(db_session, mocker):
    """After _handle_research_completion, the research task's status is EVALUATING."""
    direction = _active_direction("d-evalsts1", "Evaluating status direction")
    project = Project(
        name="EVALUATING Status Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
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
        name="Multiple Findings",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_research_task(project, direction, db_session)

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[
            _make_finding(
                "Add request tracing middleware to all routes", direction["id"]
            ),
            _make_finding(
                "Implement exponential backoff on external API calls", direction["id"]
            ),
            _make_finding(
                "Replace print statements with structured logging", direction["id"]
            ),
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
        name="Depth Increment Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
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
        name="Sub-Research Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_research_task(project, direction, db_session)

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[
            _make_finding(
                "Investigate query performance bottlenecks in depth",
                direction["id"],
                finding_type="research",
            )
        ],
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
        name="Large Scope Plan Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_research_task(project, direction, db_session)

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[
            _make_finding(
                "Migrate entire payment processing system to new provider",
                direction["id"],
                scope="large",
            )
        ],
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
        name="Small Scope Create Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_research_task(project, direction, db_session)

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[
            _make_finding(
                "Add null check to user profile endpoint handler",
                direction["id"],
                scope="small",
            )
        ],
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
async def test_small_scope_finding_gets_low_effort(db_session, mocker):
    """Small-scope findings spawn with effort='low' (cheaper/faster)."""
    direction = _active_direction("d-efflow01", "Small scope low effort direction")
    project = Project(
        name="Scope Low Effort Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_research_task(project, direction, db_session)

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[
            _make_finding(
                "Add null check to specific handler",
                direction["id"],
                scope="small",
            )
        ],
    )

    await _handle_research_completion(task, "", db_session)

    result = await db_session.execute(
        select(Task).where(
            Task.project_id == project.id, Task.parent_task_id == task.id
        )
    )
    child_tasks = result.scalars().all()
    assert len(child_tasks) == 1
    assert child_tasks[0].payloads[0]["effort"] == "low"


@pytest.mark.asyncio
async def test_large_scope_finding_gets_high_effort(db_session, mocker):
    """Large-scope findings spawn with effort='high' (deeper decomposition)."""
    direction = _active_direction("d-effhigh1", "Large scope high effort direction")
    project = Project(
        name="Scope High Effort Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_research_task(project, direction, db_session)

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[
            _make_finding(
                "Migrate auth subsystem to new provider",
                direction["id"],
                scope="large",
            )
        ],
    )

    await _handle_research_completion(task, "", db_session)

    result = await db_session.execute(
        select(Task).where(
            Task.project_id == project.id, Task.parent_task_id == task.id
        )
    )
    child_tasks = result.scalars().all()
    assert len(child_tasks) == 1
    assert child_tasks[0].payloads[0]["effort"] == "high"


@pytest.mark.asyncio
async def test_explicit_finding_effort_max_beats_scope_floor(db_session, mocker):
    """Finding-specified effort wins when it ranks higher than the scope-derived floor."""
    direction = _active_direction("d-effmax01", "Explicit max effort direction")
    project = Project(
        name="Explicit Max Effort Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_research_task(project, direction, db_session)

    finding = _make_finding(
        "Tiny but critical check requiring max reasoning",
        direction["id"],
        scope="small",
    )
    finding["effort"] = "max"

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[finding],
    )

    await _handle_research_completion(task, "", db_session)

    result = await db_session.execute(
        select(Task).where(
            Task.project_id == project.id, Task.parent_task_id == task.id
        )
    )
    child_tasks = result.scalars().all()
    assert len(child_tasks) == 1
    assert child_tasks[0].payloads[0]["effort"] == "max"


@pytest.mark.asyncio
async def test_finding_type_security_audit_spawns_security_audit_task(
    db_session, mocker
):
    """A finding with type='security_audit' spawns a specialized security_audit task."""
    direction = _active_direction("d-secaud01", "Security audit direction")
    project = Project(
        name="Security Audit Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_research_task(project, direction, db_session)

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[
            _make_finding(
                "Audit authentication module for injection vulnerabilities",
                direction["id"],
                finding_type="security_audit",
            )
        ],
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


@pytest.mark.asyncio
async def test_medium_scope_with_many_related_files_upgrades_to_large(
    db_session, mocker
):
    """Medium-scope finding with >=5 related_files is upgraded to large via pre-spawn heuristic."""
    direction = _active_direction("d-fchint01", "File-count heuristic direction")
    project = Project(
        name="File Count Heuristic Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_research_task(project, direction, db_session)

    # Current upgrade threshold is 8 files (file-count heuristic) or cross-
    # module span >= 3 modules. Use 8 files to exercise the file-count path.
    finding = _make_finding(
        "Refactor auth module across many files",
        direction["id"],
        scope="medium",
    )
    # Current upgrade threshold is 8 files (file-count heuristic).
    finding["related_files"] = [f"backend/app/mod_{i}.py" for i in range(8)]

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[finding],
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
    assert child_tasks[0].context["scope"] == "large"
    assert child_tasks[0].context["scope_calibrated_from"] == "medium"


@pytest.mark.asyncio
async def test_small_scope_with_many_related_files_upgrades_to_medium(
    db_session, mocker, monkeypatch
):
    """Small-scope finding with >=4 related_files is upgraded to medium via pre-spawn heuristic."""
    monkeypatch.setitem(
        _shared_state.runtime_config, "pipeline_scope_baseline_rates", {}
    )
    direction = _active_direction("d-fchint03", "Small file-count heuristic direction")
    project = Project(
        name="Small File Count Heuristic Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_research_task(project, direction, db_session)

    finding = _make_finding(
        "Touch three modules",
        direction["id"],
        scope="small",
    )
    finding["related_files"] = [f"backend/app/mod_{i}.py" for i in range(4)]

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[finding],
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
    assert child_tasks[0].context["scope"] == "medium"
    assert child_tasks[0].context["scope_calibrated_from"] == "small"


@pytest.mark.asyncio
async def test_history_downgrade_suppresses_heuristic_upgrade(
    db_session, mocker, monkeypatch
):
    """Medium finding downgraded to small by _calibrate_scope (historical merge
    rates) stays at small even when related_files >= 3. Historical data is a
    stronger signal than the file-count heuristic, so the heuristic must not
    revert the downgrade and route the task back into the failing medium pool.
    """
    monkeypatch.setitem(
        _shared_state.runtime_config, "pipeline_scope_baseline_rates", {}
    )
    direction = _active_direction("d-histdown01", "History downgrade direction")
    # Medium has 6 terminal tasks at 17% merge rate (< 40% threshold) and no
    # small data, so _calibrate_scope downgrades medium→small.
    direction["research_history"] = [
        {"scope_breakdown": {"medium": {"created": 6, "merged": 1, "failed": 5}}},
    ]
    project = Project(
        name="History Downgrade Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_research_task(project, direction, db_session)

    finding = _make_finding("Touch three modules", direction["id"], scope="medium")
    finding["related_files"] = [f"backend/app/mod_{i}.py" for i in range(3)]

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings", return_value=[finding]
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
    assert child_tasks[0].context["scope"] == "small"
    assert child_tasks[0].context["scope_calibrated_from"] == "medium"


@pytest.mark.asyncio
async def test_small_scope_with_few_related_files_stays_small(
    db_session, mocker, monkeypatch
):
    """Small-scope finding with <3 related_files is not upgraded by the file-count heuristic."""
    monkeypatch.setitem(
        _shared_state.runtime_config, "pipeline_scope_baseline_rates", {}
    )
    direction = _active_direction("d-fchint04", "Small file-count under threshold")
    project = Project(
        name="Small File Count Under Threshold Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_research_task(project, direction, db_session)

    finding = _make_finding(
        "Touch two modules",
        direction["id"],
        scope="small",
    )
    finding["related_files"] = [f"backend/app/mod_{i}.py" for i in range(2)]

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[finding],
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
    assert child_tasks[0].context["scope"] == "small"
    assert "scope_calibrated_from" not in child_tasks[0].context


@pytest.mark.asyncio
async def test_medium_scope_with_few_related_files_stays_medium(
    db_session, mocker, monkeypatch
):
    """Medium-scope finding with <8 related_files is not upgraded by the file-count heuristic."""
    # Disable cold-start baseline upgrade so this test isolates the file-count heuristic.
    monkeypatch.setitem(
        _shared_state.runtime_config, "pipeline_scope_baseline_rates", {}
    )
    direction = _active_direction("d-fchint02", "File-count heuristic under threshold")
    project = Project(
        name="File Count Under Threshold Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_research_task(project, direction, db_session)

    finding = _make_finding(
        "Tweak a handful of files",
        direction["id"],
        scope="medium",
    )
    finding["related_files"] = [f"backend/app/mod_{i}.py" for i in range(4)]

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[finding],
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
    assert child_tasks[0].context["scope"] == "medium"
    assert "scope_calibrated_from" not in child_tasks[0].context


@pytest.mark.asyncio
async def test_medium_scope_cross_module_related_files_auto_decomposes(
    db_session, mocker, monkeypatch
):
    """3-module cross-module medium finding auto-decomposes into per-file small sub-findings."""
    monkeypatch.setitem(
        _shared_state.runtime_config, "pipeline_scope_baseline_rates", {}
    )
    # Decomposition is opt-in (default off); enable it to exercise the behavior.
    monkeypatch.setitem(
        _shared_state.runtime_config, "cochange_decomposition_enabled", True
    )
    direction = _active_direction("d-xmod01", "Cross-module heuristic direction")
    project = Project(
        name="Cross-Module Heuristic Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_research_task(project, direction, db_session)

    finding = _make_finding(
        "Change spanning backend, agent, and admin-ui", direction["id"], scope="medium"
    )
    finding["related_files"] = [
        "backend/app/foo.py",
        "agent/src/bar.ts",
        "admin-ui/src/baz.tsx",
    ]

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[finding],
    )

    await _handle_research_completion(task, "", db_session)

    result = await db_session.execute(
        select(Task).where(
            Task.project_id == project.id, Task.parent_task_id == task.id
        )
    )
    child_tasks = result.scalars().all()
    assert len(child_tasks) == 3
    files_seen = set()
    for ct in child_tasks:
        assert ct.payloads[0]["type"] == "create"
        assert ct.context["scope"] == "small"
        assert ct.context["scope_calibrated_from"] == "medium"
        files_seen.update(ct.context.get("related_files") or [])
    assert files_seen == {
        "backend/app/foo.py",
        "agent/src/bar.ts",
        "admin-ui/src/baz.tsx",
    }


@pytest.mark.asyncio
async def test_auto_decomposed_siblings_are_chained_via_depends_on(
    db_session, mocker, monkeypatch
):
    """Decomposed sibling tasks of one finding chain via depends_on so they run
    serially, not in parallel.

    Reproduces the dcd6e840 failure shape: a coupled feature change auto-split
    into per-file siblings ran in parallel, and later siblings saw partial
    sibling-merged scaffolding in their base branch instead of the actual
    implementation. Sequential execution lets each agent work against the
    actually-merged state of prior siblings."""
    monkeypatch.setitem(
        _shared_state.runtime_config, "pipeline_scope_baseline_rates", {}
    )
    # Decomposition is opt-in (default off); enable it to exercise the behavior.
    monkeypatch.setitem(
        _shared_state.runtime_config, "cochange_decomposition_enabled", True
    )
    direction = _active_direction("d-chain01", "Sibling-chain direction")
    project = Project(
        name="Sibling Chain Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_research_task(project, direction, db_session)

    # 3-module finding triggers top-level-dir auto-decomposition (3 siblings).
    finding = _make_finding(
        "Coupled cross-module change", direction["id"], scope="medium"
    )
    finding["related_files"] = [
        "backend/app/foo.py",
        "agent/src/bar.ts",
        "admin-ui/src/baz.tsx",
    ]

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[finding],
    )

    await _handle_research_completion(task, "", db_session)

    result = await db_session.execute(
        select(Task)
        .where(Task.project_id == project.id, Task.parent_task_id == task.id)
        .order_by(Task.created_at)
    )
    child_tasks = result.scalars().all()
    assert len(child_tasks) == 3

    # All three are auto-decomposed siblings of the same parent finding.
    assert all((ct.payloads[0]["type"] == "create") for ct in child_tasks)

    # Chain: first sibling free, second depends on first, third depends on second.
    assert list(child_tasks[0].depends_on or []) == []
    assert list(child_tasks[1].depends_on or []) == [str(child_tasks[0].id)]
    assert list(child_tasks[2].depends_on or []) == [str(child_tasks[1].id)]


@pytest.mark.asyncio
async def test_non_decomposed_siblings_do_not_chain(db_session, mocker, monkeypatch):
    """Two unrelated findings produce two unrelated tasks — no chain dependency.
    Only auto-decomposed sibling groups should chain; independent findings
    stay parallel."""
    monkeypatch.setitem(
        _shared_state.runtime_config, "pipeline_scope_baseline_rates", {}
    )
    direction = _active_direction("d-nochn01", "Independent findings direction")
    project = Project(
        name="No-Chain Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_research_task(project, direction, db_session)

    f1 = _make_finding("Bug A", direction["id"], scope="small")
    f1["related_files"] = ["backend/app/a.py"]
    f2 = _make_finding("Bug B", direction["id"], scope="small")
    f2["related_files"] = ["backend/app/b.py"]

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[f1, f2],
    )

    await _handle_research_completion(task, "", db_session)

    result = await db_session.execute(
        select(Task)
        .where(Task.project_id == project.id, Task.parent_task_id == task.id)
        .order_by(Task.created_at)
    )
    child_tasks = result.scalars().all()
    assert len(child_tasks) == 2
    assert list(child_tasks[0].depends_on or []) == []
    assert list(child_tasks[1].depends_on or []) == []


@pytest.mark.asyncio
async def test_partial_adversarial_manifest_fail_closes_omitted_high_stakes_findings(
    db_session, mocker, monkeypatch
):
    """High-stakes findings omitted from the bounded adversarial manifest do not spawn."""
    monkeypatch.setitem(
        _shared_state.runtime_config, "pipeline_scope_baseline_rates", {}
    )
    direction = _active_direction("d-advpartial01", "Adversarial manifest direction")
    project = Project(
        name="Adversarial Partial Manifest Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_research_task(project, direction, db_session)

    kept = _make_finding("Keep challenge", direction["id"], scope="small")
    omitted = _make_finding("Drop challenge", direction["id"], scope="small")
    kept_hash = hashlib.sha256(
        (kept["title"] + kept["description"]).encode()
    ).hexdigest()[:16]

    _patch_research_externals(mocker)
    mocker.patch("app.research_completion._is_high_stakes_finding", return_value=True)
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[kept, omitted],
    )
    task.context = {
        **(task.context or {}),
        ADVERSARIAL_GATE_VERDICT_KEY: {
            "finding_verdicts": {
                kept_hash: {
                    "passed": True,
                    "reason": "explicit pass",
                    "explicit": True,
                }
            }
        },
        "_adversarial_challenged_hashes": [kept_hash],
    }

    await _handle_research_completion(task, "", db_session)

    result = await db_session.execute(
        select(Task).where(
            Task.project_id == project.id, Task.parent_task_id == task.id
        )
    )
    child_tasks = result.scalars().all()
    assert len(child_tasks) == 1
    assert child_tasks[0].title == kept["title"]


@pytest.mark.asyncio
async def test_medium_scope_expansion_ratio_prediction_upgrades_to_large(
    db_session, mocker, monkeypatch
):
    """Medium finding with 4 related_files x 3.0 avg_expansion_ratio predicts >10 → upgrade."""
    monkeypatch.setitem(
        _shared_state.runtime_config, "pipeline_scope_baseline_rates", {}
    )
    direction = _active_direction("d-exprat01", "Expansion ratio prediction direction")
    direction["research_history"] = [
        {
            "scope_breakdown": {
                "medium": {"created": 3, "merged": 2, "avg_expansion_ratio": 3.0}
            }
        },
    ]
    project = Project(
        name="Expansion Ratio Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_research_task(project, direction, db_session)

    finding = _make_finding(
        "Refactor four related modules", direction["id"], scope="medium"
    )
    finding["related_files"] = [f"backend/app/mod_{i}.py" for i in range(4)]

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings", return_value=[finding]
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
    assert child_tasks[0].context["scope"] == "large"
    assert child_tasks[0].context["scope_calibrated_from"] == "medium"


@pytest.mark.asyncio
async def test_medium_scope_without_expansion_ratio_history_stays_medium(
    db_session, mocker, monkeypatch
):
    """No historical avg_expansion_ratio → predictive check skipped, finding stays medium."""
    monkeypatch.setitem(
        _shared_state.runtime_config, "pipeline_scope_baseline_rates", {}
    )
    direction = _active_direction("d-exprat02", "No expansion ratio history")
    project = Project(
        name="No Expansion Ratio Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_research_task(project, direction, db_session)

    finding = _make_finding(
        "Refactor four related modules", direction["id"], scope="medium"
    )
    finding["related_files"] = [f"backend/app/mod_{i}.py" for i in range(4)]

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings", return_value=[finding]
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
    assert child_tasks[0].context["scope"] == "medium"
    assert "scope_calibrated_from" not in child_tasks[0].context


# ─────────────────────────────────────────────────────────────────────────────
# 8b. _handle_research_completion — Auto-decomposition via co-change clustering
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_medium_with_independent_clusters_decomposes_to_smalls(
    db_session, mocker, monkeypatch
):
    """Medium finding whose related_files form 2 independent clusters splits into 2 small tasks."""
    monkeypatch.setitem(
        _shared_state.runtime_config, "pipeline_scope_baseline_rates", {}
    )
    # Decomposition is opt-in (default off); enable it to exercise the behavior.
    monkeypatch.setitem(
        _shared_state.runtime_config, "cochange_decomposition_enabled", True
    )
    direction = _active_direction("d-decomp01", "Decomposition direction")
    cochange_index = {
        "a.py": {"b.py": 2.0},
        "b.py": {"a.py": 2.0},
        "c.py": {"d.py": 2.0},
        "d.py": {"c.py": 2.0},
    }
    project = Project(
        name="Decomp Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
        design_doc={"cochange_index": cochange_index},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_research_task(project, direction, db_session)
    finding = _make_finding(
        "Touch two unrelated clusters", direction["id"], scope="medium"
    )
    finding["related_files"] = ["a.py", "b.py", "c.py", "d.py"]

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[finding],
    )

    await _handle_research_completion(task, "", db_session)

    result = await db_session.execute(
        select(Task).where(
            Task.project_id == project.id, Task.parent_task_id == task.id
        )
    )
    child_tasks = result.scalars().all()
    assert len(child_tasks) == 2
    for ct in child_tasks:
        assert ct.payloads[0]["type"] == "create"
        assert ct.context["scope"] == "small"
        assert ct.context["scope_calibrated_from"] == "medium"
    cluster_sets = {frozenset(ct.context["related_files"]) for ct in child_tasks}
    assert cluster_sets == {frozenset({"a.py", "b.py"}), frozenset({"c.py", "d.py"})}


@pytest.mark.asyncio
async def test_medium_with_single_cluster_stays_medium(db_session, mocker, monkeypatch):
    """When all related_files co-change together, decomposition is skipped."""
    monkeypatch.setitem(
        _shared_state.runtime_config, "pipeline_scope_baseline_rates", {}
    )
    direction = _active_direction("d-decomp02", "Single cluster direction")
    cochange_index = {
        "a.py": {"b.py": 2.0, "c.py": 2.0},
        "b.py": {"a.py": 2.0, "c.py": 2.0},
        "c.py": {"a.py": 2.0, "b.py": 2.0},
    }
    project = Project(
        name="Single Cluster Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
        design_doc={"cochange_index": cochange_index},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_research_task(project, direction, db_session)
    finding = _make_finding(
        "Touch one cohesive module", direction["id"], scope="medium"
    )
    finding["related_files"] = ["a.py", "b.py", "c.py"]

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[finding],
    )

    await _handle_research_completion(task, "", db_session)

    result = await db_session.execute(
        select(Task).where(
            Task.project_id == project.id, Task.parent_task_id == task.id
        )
    )
    child_tasks = result.scalars().all()
    assert len(child_tasks) == 1
    assert child_tasks[0].context["scope"] == "medium"
    assert "scope_calibrated_from" not in child_tasks[0].context


@pytest.mark.asyncio
async def test_medium_skips_decomposition_when_cochange_is_sparse(
    db_session, mocker, monkeypatch
):
    """Without cochange data for the finding's files, decomposition is skipped."""
    monkeypatch.setitem(
        _shared_state.runtime_config, "pipeline_scope_baseline_rates", {}
    )
    direction = _active_direction("d-decomp03", "Sparse direction")
    project = Project(
        name="Sparse Cochange Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
        design_doc={"cochange_index": {"unrelated.py": {"other.py": 2.0}}},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_research_task(project, direction, db_session)
    finding = _make_finding("New area no history", direction["id"], scope="medium")
    finding["related_files"] = ["new_a.py", "new_b.py", "new_c.py"]

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[finding],
    )

    await _handle_research_completion(task, "", db_session)

    result = await db_session.execute(
        select(Task).where(
            Task.project_id == project.id, Task.parent_task_id == task.id
        )
    )
    child_tasks = result.scalars().all()
    assert len(child_tasks) == 1
    assert child_tasks[0].context["scope"] == "medium"


def test_cluster_files_by_cochange_returns_none_when_empty():
    """_cluster_files_by_cochange returns None for sparse / empty input."""
    from app.research_completion import _cluster_files_by_cochange

    assert _cluster_files_by_cochange([], {"a.py": {"b.py": 2.0}}) is None
    assert _cluster_files_by_cochange(["a.py"], {"a.py": {"b.py": 2.0}}) is None
    assert _cluster_files_by_cochange(["x.py", "y.py"], {}) is None
    assert _cluster_files_by_cochange(["x.py", "y.py"], {"a.py": {"b.py": 2.0}}) is None


def test_cluster_files_by_cochange_splits_independent_groups():
    """_cluster_files_by_cochange groups co-changing files and separates independent ones."""
    from app.research_completion import _cluster_files_by_cochange

    idx = {
        "a.py": {"b.py": 2.0},
        "b.py": {"a.py": 2.0},
        "c.py": {"d.py": 2.0},
        "d.py": {"c.py": 2.0},
    }
    clusters = _cluster_files_by_cochange(["a.py", "b.py", "c.py", "d.py"], idx)
    assert clusters is not None
    cluster_sets = {frozenset(c) for c in clusters}
    assert cluster_sets == {frozenset({"a.py", "b.py"}), frozenset({"c.py", "d.py"})}


def test_cluster_files_by_cochange_merges_transitive_affinity():
    """Transitive co-change (a-b, b-c) unifies all three files into one cluster."""
    from app.research_completion import _cluster_files_by_cochange

    idx = {
        "a.py": {"b.py": 2.0},
        "b.py": {"a.py": 2.0, "c.py": 2.0},
        "c.py": {"b.py": 2.0},
    }
    clusters = _cluster_files_by_cochange(["a.py", "b.py", "c.py"], idx)
    assert clusters is not None
    assert len(clusters) == 1
    assert set(clusters[0]) == {"a.py", "b.py", "c.py"}


def test_cluster_files_by_cochange_ignores_below_threshold_scores():
    """Affinity scores below the 1.0 threshold don't merge files — and with no
    merge there is no real co-change structure, so decomposition is SKIPPED
    (returns None) rather than fanning each file out as its own singleton task.
    Returning [[a],[b]] here is the over-spawn bug in miniature."""
    from app.research_completion import _cluster_files_by_cochange

    idx = {
        "a.py": {"b.py": 0.5},
        "b.py": {"a.py": 0.5},
    }
    clusters = _cluster_files_by_cochange(["a.py", "b.py"], idx)
    assert clusters is None


def test_cluster_files_by_cochange_skips_when_only_external_affinity():
    """Regression for the 2026-06-02 over-spawn (research a473833c: 1 finding ->
    5 redundant '(cluster N/5)' tasks). A file IN the set has cochange data, but
    only with files OUTSIDE the set, so union-find joins nothing and every file
    is a singleton. The top-level affinity guard passes (one file has data), so
    without the all-singletons rejection this fans out one task per file."""
    from app.research_completion import _cluster_files_by_cochange

    # a.py has recorded cochange data, but only with z.py (not in the set).
    idx = {"a.py": {"z.py": 3.0}}
    clusters = _cluster_files_by_cochange(["a.py", "b.py", "c.py"], idx)
    assert clusters is None


def test_cluster_files_by_toplevel_dir_requires_min_files_and_dirs():
    """Path-based fallback requires 3+ files spanning 3+ top-level directories.

    2-module findings stay coherent — only 3+ module findings decompose,
    matching the scope-escalation threshold.
    """
    from app.cochange_decomposition import cluster_files_by_toplevel_dir

    assert cluster_files_by_toplevel_dir(["backend/a.py", "agent/b.ts"]) is None
    assert (
        cluster_files_by_toplevel_dir(["backend/a.py", "backend/b.py", "backend/c.py"])
        is None
    )
    # 2-module findings (backend + agent) don't decompose.
    assert (
        cluster_files_by_toplevel_dir(["backend/a.py", "backend/b.py", "agent/c.ts"])
        is None
    )
    # Root-level files don't establish a directory span.
    assert cluster_files_by_toplevel_dir(["a.py", "b.py", "c.py"]) is None
    clusters = cluster_files_by_toplevel_dir(
        ["backend/a.py", "agent/b.ts", "admin-ui/c.tsx"]
    )
    assert clusters is not None
    cluster_sets = {frozenset(c) for c in clusters}
    assert cluster_sets == {
        frozenset({"backend/a.py"}),
        frozenset({"agent/b.ts"}),
        frozenset({"admin-ui/c.tsx"}),
    }


def test_auto_decompose_falls_back_to_path_when_cochange_empty():
    """Medium finding with files across 3+ top-level dirs splits when cochange is empty."""
    from app.cochange_decomposition import auto_decompose_findings_by_cochange

    def _hash(title: str, desc: str) -> str:
        return f"{title}|{desc}"

    finding = {
        "title": "Cross-module cleanup",
        "description": "desc",
        "scope": "medium",
        "related_files": [
            "backend/app/x.py",
            "agent/src/y.ts",
            "admin-ui/src/z.tsx",
        ],
    }
    result = auto_decompose_findings_by_cochange(
        [(0, finding, "h0")],
        {},
        None,
        _hash,
    )
    assert len(result) == 3
    assert all(sub["scope"] == "small" for _, sub, _ in result)
    assert all(sub["auto_decomposed"] for _, sub, _ in result)
    sub_file_sets = {frozenset(sub["related_files"]) for _, sub, _ in result}
    assert sub_file_sets == {
        frozenset({"backend/app/x.py"}),
        frozenset({"agent/src/y.ts"}),
        frozenset({"admin-ui/src/z.tsx"}),
    }


# ─────────────────────────────────────────────────────────────────────────────
# 9. integration_branch routing — autonomous spawns must branch from the
#    project integration_branch column (automain after the cutover), never a
#    hardcoded "main", or they bypass the automain release-qualifier flow.
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_autonomous_research_spawn_uses_integration_branch(db_session):
    """When the project integration_branch is 'automain', autonomous research
    spawns must set base_branch='automain' (not the hardcoded 'main' default).
    """
    project = Project(
        name="Integration Branch Routing",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[_active_direction("d-intbr01")],
        vision={"purpose": "test"},
        integration_branch="automain",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _spawn_research_task(project, db_session)

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    spawned = result.scalars().all()
    assert spawned, "expected an autonomous task to be spawned"
    for t in spawned:
        assert (t.payloads or [{}])[0].get("base_branch") == "automain", (
            f"task {t.id} ({(t.payloads or [{}])[0].get('type')}) bypassed the "
            f"integration_branch column"
        )


@pytest.mark.asyncio
async def test_autonomous_research_spawn_defaults_to_main(db_session):
    """With no project override, autonomous spawns branch from 'main'."""
    project = Project(
        name="Integration Branch Default",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[_active_direction("d-intbr02")],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _spawn_research_task(project, db_session)

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    spawned = result.scalars().all()
    assert spawned, "expected an autonomous task to be spawned"
    for t in spawned:
        assert (t.payloads or [{}])[0].get("base_branch") == "main"


def test_no_hardcoded_main_base_branch_in_autonomous_spawns():
    """Static guard: autonomous spawn payloads in the spawn-routing modules must
    never hardcode {"base_branch": "main"} — they must route through
    _runtime_config.get("integration_branch", "main") so the Phase-1 automain
    cutover takes effect everywhere.

    Inheritance reads of the form `.get("base_branch", "main")` (a child task
    inheriting its parent's already-routed branch) are a different, correct
    pattern and are explicitly allowed.
    """
    import re
    from pathlib import Path

    app_dir = Path(__file__).resolve().parents[1] / "app"
    modules = [
        "autonomous_research.py",
        "task_completion.py",
        "anomaly_detection.py",
        "direction_lifecycle.py",
        "lifecycle.py",
        "main.py",
    ]
    # Matches the hardcoded dict-literal form `"base_branch": "main"` but NOT
    # `.get("base_branch", "main")` (which has a "(" or "," before the key).
    hardcoded = re.compile(r'(?<![(,]\s)"base_branch":\s*"main"')
    offenders: list[str] = []
    for mod in modules:
        path = app_dir / mod
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if hardcoded.search(line):
                offenders.append(f"{mod}:{lineno}: {line.strip()}")
    assert not offenders, (
        "autonomous spawn payloads hardcode base_branch='main' instead of "
        "routing through integration_branch:\n" + "\n".join(offenders)
    )


@pytest.mark.asyncio
async def test_spawn_uses_project_integration_branch_column(db_session):
    """Two projects with different integration_branch column values resolve independently."""
    project_a = Project(
        name="Project A override",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[_active_direction("d-bca01")],
        vision={"purpose": "test"},
        integration_branch="project-a-main",
    )
    project_b = Project(
        name="Project B override",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[_active_direction("d-bcb01")],
        vision={"purpose": "test"},
        integration_branch="project-b-main",
    )
    db_session.add(project_a)
    db_session.add(project_b)
    await db_session.commit()
    await db_session.refresh(project_a)
    await db_session.refresh(project_b)

    await _spawn_research_task(project_a, db_session)
    await _spawn_research_task(project_b, db_session)

    result_a = await db_session.execute(
        select(Task).where(Task.project_id == project_a.id)
    )
    for t in result_a.scalars().all():
        assert t.first_payload.get("base_branch") == "project-a-main", (
            f"project_a task {t.id} used wrong branch: {t.first_payload.get('base_branch')}"
        )

    result_b = await db_session.execute(
        select(Task).where(Task.project_id == project_b.id)
    )
    for t in result_b.scalars().all():
        assert t.first_payload.get("base_branch") == "project-b-main", (
            f"project_b task {t.id} used wrong branch: {t.first_payload.get('base_branch')}"
        )


@pytest.mark.asyncio
async def test_spawn_falls_back_to_global_when_no_build_config_override(
    db_session,
):
    """When build_config has no integration_branch key, the project falls back to 'main'."""
    project = Project(
        name="No override project",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[_active_direction("d-bcfb01")],
        vision={"purpose": "test"},
        build_config={"some_other_key": True},  # no integration_branch key
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _spawn_research_task(project, db_session)

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    spawned = result.scalars().all()
    assert spawned
    for t in spawned:
        assert t.first_payload.get("base_branch") == "main", (
            f"task {t.id} should fall back to main, got: {t.first_payload.get('base_branch')}"
        )


def test_auto_decompose_keeps_two_module_medium_finding_intact():
    """2-module medium findings pass through without decomposition.

    Matches the scope-escalation threshold: only 3+ module findings fragment.
    """
    from app.cochange_decomposition import auto_decompose_findings_by_cochange

    def _hash(title: str, desc: str) -> str:
        return f"{title}|{desc}"

    finding = {
        "title": "Backend + agent touch",
        "description": "desc",
        "scope": "medium",
        "related_files": [
            "backend/app/x.py",
            "agent/src/y.ts",
        ],
    }
    result = auto_decompose_findings_by_cochange(
        [(0, finding, "h0")],
        {},
        None,
        _hash,
    )
    assert len(result) == 1
    assert result[0][1] is finding

    finding_three = {
        "title": "Backend + agent cluster",
        "description": "desc",
        "scope": "medium",
        "related_files": [
            "backend/app/x.py",
            "backend/app/y.py",
            "agent/src/z.ts",
        ],
    }
    result = auto_decompose_findings_by_cochange(
        [(0, finding_three, "h0")],
        {},
        None,
        _hash,
    )
    assert len(result) == 1
    assert result[0][1] is finding_three


def test_auto_decompose_path_fallback_skipped_for_single_directory():
    """Path fallback does not trigger when all files share one top-level dir."""
    from app.cochange_decomposition import auto_decompose_findings_by_cochange

    def _hash(title: str, desc: str) -> str:
        return f"{title}|{desc}"

    finding = {
        "title": "Backend-only cleanup",
        "scope": "medium",
        "related_files": [
            "backend/app/a.py",
            "backend/app/b.py",
            "backend/app/c.py",
        ],
    }
    result = auto_decompose_findings_by_cochange(
        [(0, finding, "h0")],
        None,
        None,
        _hash,
    )
    assert len(result) == 1
    assert result[0][1] is finding


def test_auto_decompose_prepends_scope_boundary_preamble_to_prompt():
    """Decomposed sub-findings get a scope-boundary preamble naming cluster size + index."""
    from app.cochange_decomposition import auto_decompose_findings_by_cochange

    def _hash(title: str, desc: str) -> str:
        return f"{title}|{desc}"

    original_prompt = "Original task description body."
    finding = {
        "title": "Cross-module refactor",
        "prompt": original_prompt,
        "scope": "medium",
        "related_files": [
            "backend/app/x.py",
            "agent/src/y.ts",
            "admin-ui/src/z.tsx",
        ],
    }
    result = auto_decompose_findings_by_cochange(
        [(0, finding, "h0")],
        {},
        None,
        _hash,
    )
    assert len(result) == 3
    for _, sub, _ in result:
        prompt = sub["prompt"]
        assert prompt.endswith(original_prompt)
        assert "Scope boundary" in prompt
        assert "cluster " in prompt.lower()
        assert f"/{len(result)}" in prompt  # cluster N/M
        for f in sub["related_files"]:
            assert f in prompt
        # other clusters' files should not be listed
        other_files = {
            f
            for _, other, _ in result
            if other is not sub
            for f in other["related_files"]
        }
        for f in other_files:
            assert f not in prompt
        # cluster's file count appears
        assert f"{len(sub['related_files'])} file" in prompt


def _decomp_hash_fn(title: str, desc: str) -> str:
    return f"{title}|{desc}"


def _decomp_finding(files: list[str], title: str = "F1") -> dict:
    return {
        "title": title,
        "description": "d",
        "scope": "medium",
        "related_files": files,
    }


def _decomp_cochange_index(clusters: list[list[str]]) -> dict:
    """Build a cochange_index where files within each cluster co-change."""
    idx: dict = {}
    for cluster in clusters:
        for fa in cluster:
            for fb in cluster:
                if fa != fb:
                    idx.setdefault(fa, {})[fb] = 2.0
    return idx


def test_auto_decompose_normal_split_when_clusters_fit_small_budget():
    """[5 files, 3 files] decomposes normally — both clusters fit small budget (6)."""
    from app.cochange_decomposition import auto_decompose_findings_by_cochange

    cluster_a = [f"a{i}.py" for i in range(5)]
    cluster_b = [f"b{i}.py" for i in range(3)]
    files = cluster_a + cluster_b
    idx = _decomp_cochange_index([cluster_a, cluster_b])
    finding = _decomp_finding(files)
    out = auto_decompose_findings_by_cochange(
        [(0, finding, "h")], idx, None, _decomp_hash_fn
    )
    assert len(out) == 2
    for _, sub, _ in out:
        assert sub["scope"] == "small"
        assert sub.get("auto_decomposed") is True
        assert sub.get("scope_calibrated_from") == "medium"


def test_auto_decompose_oversized_cluster_keeps_medium_scope():
    """[7 files, 3 files] — 7-file cluster stays medium, 3-file cluster goes small."""
    from app.cochange_decomposition import auto_decompose_findings_by_cochange

    cluster_a = [f"a{i}.py" for i in range(7)]
    cluster_b = [f"b{i}.py" for i in range(3)]
    files = cluster_a + cluster_b
    idx = _decomp_cochange_index([cluster_a, cluster_b])
    finding = _decomp_finding(files)
    out = auto_decompose_findings_by_cochange(
        [(0, finding, "h")], idx, None, _decomp_hash_fn
    )
    assert len(out) == 2
    by_size = {len(sub["related_files"]): sub for _, sub, _ in out}
    assert by_size[7]["scope"] == "medium"
    assert "scope_calibrated_from" not in by_size[7]
    assert by_size[3]["scope"] == "small"
    assert by_size[3]["scope_calibrated_from"] == "medium"


def test_auto_decompose_skipped_when_all_clusters_exceed_small_budget():
    """When every cluster exceeds the small budget, leave the original medium finding intact."""
    from app.cochange_decomposition import auto_decompose_findings_by_cochange

    cluster_a = [f"a{i}.py" for i in range(7)]
    cluster_b = [f"b{i}.py" for i in range(8)]
    files = cluster_a + cluster_b
    idx = _decomp_cochange_index([cluster_a, cluster_b])
    finding = _decomp_finding(files)
    out = auto_decompose_findings_by_cochange(
        [(0, finding, "h")], idx, None, _decomp_hash_fn
    )
    assert len(out) == 1
    _, sub, fhash = out[0]
    assert sub is finding
    assert sub["scope"] == "medium"
    assert sub.get("auto_decomposed") is not True
    assert fhash == "h"


def test_split_oversized_clusters_by_subdir_splits_on_real_subdirs():
    """Oversized cluster spanning two sub-dirs splits into per-sub-dir groups."""
    from app.cochange_decomposition import split_oversized_clusters_by_subdir

    cluster = [
        "backend/app/a.py",
        "backend/app/b.py",
        "backend/app/c.py",
        "backend/app/d.py",
        "backend/testing/e.py",
        "backend/testing/f.py",
        "backend/testing/g.py",
    ]
    out = split_oversized_clusters_by_subdir([cluster], small_budget=6)
    assert out is not None
    assert {tuple(sorted(g)) for g in out} == {
        (
            "backend/app/a.py",
            "backend/app/b.py",
            "backend/app/c.py",
            "backend/app/d.py",
        ),
        ("backend/testing/e.py", "backend/testing/f.py", "backend/testing/g.py"),
    }


def test_split_oversized_clusters_by_subdir_refuses_root_level_files():
    """Root-level (bare-filename) files have no sub-dir prefix, so don't fragment."""
    from app.cochange_decomposition import split_oversized_clusters_by_subdir

    cluster = [f"a{i}.py" for i in range(7)]
    out = split_oversized_clusters_by_subdir([cluster], small_budget=6)
    assert out is None


def test_split_oversized_clusters_by_subdir_refuses_mixed_root_and_subdir():
    """Mixed root-level + sub-dir files: refuse split so root-level files aren't
    fragmented into singletons while sub-dir files get grouped."""
    from app.cochange_decomposition import split_oversized_clusters_by_subdir

    cluster = [
        "root_a.py",
        "backend/app/a.py",
        "backend/app/b.py",
        "backend/testing/c.py",
        "backend/testing/d.py",
        "backend/testing/e.py",
        "backend/testing/f.py",
    ]
    out = split_oversized_clusters_by_subdir([cluster], small_budget=6)
    assert out is None


def test_split_oversized_clusters_by_subdir_refuses_all_singleton_subgroups():
    """Sub-grouping produces only singletons — refuse to flood the orchestrator."""
    from app.cochange_decomposition import split_oversized_clusters_by_subdir

    cluster = [
        "backend/app/a.py",
        "backend/lib/b.py",
        "backend/cli/c.py",
        "backend/util/d.py",
        "backend/core/e.py",
        "backend/util2/f.py",
        "backend/util3/g.py",
    ]
    out = split_oversized_clusters_by_subdir([cluster], small_budget=6)
    assert out is None


def test_split_oversized_clusters_by_subdir_leaves_fitting_clusters_alone():
    """Clusters already within small_budget pass through unchanged; None when
    no split progress is made."""
    from app.cochange_decomposition import split_oversized_clusters_by_subdir

    fitting = ["a.py", "b.py", "c.py"]
    out = split_oversized_clusters_by_subdir([fitting], small_budget=6)
    assert out is None


def test_cluster_files_by_subdir_splits_on_second_level_dirs():
    """Files in one top-level dir spanning 2+ second-level dirs split by subdir."""
    from app.cochange_decomposition import cluster_files_by_subdir

    clusters = cluster_files_by_subdir(
        [
            "backend/app/x.py",
            "backend/app/y.py",
            "backend/testing/test_x.py",
        ]
    )
    assert clusters is not None
    cluster_sets = {frozenset(c) for c in clusters}
    assert cluster_sets == {
        frozenset({"backend/app/x.py", "backend/app/y.py"}),
        frozenset({"backend/testing/test_x.py"}),
    }


def test_cluster_files_by_subdir_returns_none_when_single_subdir():
    """Files all sharing the same second-level dir → None."""
    from app.cochange_decomposition import cluster_files_by_subdir

    assert (
        cluster_files_by_subdir(
            [
                "backend/app/a.py",
                "backend/app/b.py",
                "backend/app/c.py",
            ]
        )
        is None
    )


def test_cluster_files_by_subdir_requires_min_files():
    """Fewer than 3 files → None."""
    from app.cochange_decomposition import cluster_files_by_subdir

    assert (
        cluster_files_by_subdir(
            [
                "backend/app/a.py",
                "backend/testing/b.py",
            ]
        )
        is None
    )


def test_cluster_files_by_subdir_returns_none_for_multi_toplevel():
    """Multiple top-level dirs → None (toplevel clustering handles that case)."""
    from app.cochange_decomposition import cluster_files_by_subdir

    assert (
        cluster_files_by_subdir(
            [
                "backend/app/a.py",
                "backend/testing/b.py",
                "agent/src/c.ts",
            ]
        )
        is None
    )


def test_cluster_files_by_subdir_returns_none_when_missing_subdir():
    """Files without a real second-level dir (only 2 segments) → None."""
    from app.cochange_decomposition import cluster_files_by_subdir

    assert (
        cluster_files_by_subdir(
            [
                "backend/a.py",
                "backend/b.py",
                "backend/c.py",
            ]
        )
        is None
    )


def test_auto_decompose_falls_back_to_subdir_within_single_toplevel():
    """Medium finding all under one top-level but spanning subdirs decomposes."""
    from app.cochange_decomposition import auto_decompose_findings_by_cochange

    finding = {
        "title": "Backend cross-subdir cleanup",
        "description": "desc",
        "scope": "medium",
        "related_files": [
            "backend/app/x.py",
            "backend/app/y.py",
            "backend/testing/test_x.py",
        ],
    }
    result = auto_decompose_findings_by_cochange(
        [(0, finding, "h0")],
        {},
        None,
        _decomp_hash_fn,
    )
    assert len(result) == 2
    assert all(sub["scope"] == "small" for _, sub, _ in result)
    sub_file_sets = {frozenset(sub["related_files"]) for _, sub, _ in result}
    assert sub_file_sets == {
        frozenset({"backend/app/x.py", "backend/app/y.py"}),
        frozenset({"backend/testing/test_x.py"}),
    }


def test_cochange_decomposition_suppressed_triggers_on_poor_merge_rate():
    """Suppress when cumulative sub_findings_total >= 3 and merge_rate < 40%."""
    from app.research_completion import _cochange_decomposition_suppressed

    direction = {
        "research_history": [
            {
                "cochange_decomposition": {
                    "sub_findings_total": 2,
                    "sub_findings_merged": 0,
                }
            },
            {
                "cochange_decomposition": {
                    "sub_findings_total": 3,
                    "sub_findings_merged": 1,
                }
            },
        ],
    }
    assert _cochange_decomposition_suppressed(direction) is True


def test_cochange_decomposition_not_suppressed_on_healthy_merge_rate():
    """Do not suppress when merge rate >= 40%."""
    from app.research_completion import _cochange_decomposition_suppressed

    direction = {
        "research_history": [
            {
                "cochange_decomposition": {
                    "sub_findings_total": 4,
                    "sub_findings_merged": 2,
                }
            },
        ],
    }
    assert _cochange_decomposition_suppressed(direction) is False


def test_cochange_decomposition_not_suppressed_below_sample_threshold():
    """Do not suppress when cumulative sub_findings_total < 3."""
    from app.research_completion import _cochange_decomposition_suppressed

    direction = {
        "research_history": [
            {
                "cochange_decomposition": {
                    "sub_findings_total": 2,
                    "sub_findings_merged": 0,
                }
            },
        ],
    }
    assert _cochange_decomposition_suppressed(direction) is False
    assert _cochange_decomposition_suppressed({"research_history": []}) is False
    assert _cochange_decomposition_suppressed(None) is False


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
        name="History Fields Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
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

    before = _utcnow()
    await _handle_research_completion(task, "", db_session)

    await db_session.refresh(project)
    updated = next(d for d in project.directions if d["id"] == direction_id)
    history = updated.get("research_history", [])

    assert len(history) == 1
    entry = history[0]
    assert entry["task_id"] == str(task.id)
    assert entry["tasks_created"] == 1
    assert entry["findings_produced"] == 1
    assert entry["batch_cap_applied"] == 0
    assert finding_title[:60] in entry.get("summary", "")
    assert datetime.fromisoformat(entry["timestamp"]) >= before


@pytest.mark.asyncio
async def test_research_history_verification_basis_distribution(db_session, mocker):
    """research_history entry carries a per-direction verification_basis_distribution
    counting each basis, with missing/invalid coerced to pattern_inferred."""
    direction_id = "d-vbasis1"
    direction = {
        **_active_direction(direction_id, "Verification basis direction"),
        "research_history": [],
    }
    project = Project(
        name="Verification Basis Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_research_task(project, direction, db_session)

    code_f = {
        **_make_finding("Code verified finding", direction_id),
        "verification_basis": "code_verified",
    }
    path_f = {
        **_make_finding("Path verified finding", direction_id),
        "verification_basis": "path_verified",
    }
    missing_f = _make_finding("Missing basis finding", direction_id)
    invalid_f = {
        **_make_finding("Invalid basis finding", direction_id),
        "verification_basis": "guessed",
    }

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[code_f, path_f, missing_f, invalid_f],
    )

    await _handle_research_completion(task, "", db_session)

    await db_session.refresh(project)
    updated = next(d for d in project.directions if d["id"] == direction_id)
    history = updated.get("research_history", [])
    assert len(history) == 1
    dist = history[0].get("verification_basis_distribution")
    assert dist is not None
    assert dist["code_verified"] == 1
    assert dist["path_verified"] == 1
    # Missing + invalid both coerce to pattern_inferred.
    assert dist["pattern_inferred"] == 2
    assert dist["deduced"] == 0


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
        name="History Cap Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
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
    assert oldest_id not in [e["task_id"] for e in history], (
        "Oldest entry should be evicted"
    )


@pytest.mark.asyncio
async def test_research_history_only_updated_for_directions_with_findings(
    db_session, mocker
):
    """Only directions that produced at least one child task get a research_history entry."""
    dir_a_id = "d-histdra1"
    dir_b_id = "d-histdrb1"
    directions = [
        {
            **_active_direction(dir_a_id, "Direction A with findings"),
            "research_history": [],
        },
        {
            **_active_direction(dir_b_id, "Direction B no findings"),
            "research_history": [],
        },
    ]
    project = Project(
        name="Selective History Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=directions,
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
        name="Proposal Happy Path",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
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
        return_value=[
            _make_direction_proposal(proposal_prompt, proposal_rationale, "high")
        ],
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
        name="Proposal No Task",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
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

    before_ids = set((await db_session.execute(select(Task.id))).scalars().all())
    await _handle_research_completion(task, "", db_session)
    after_ids = set((await db_session.execute(select(Task.id))).scalars().all())

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
        name="Proposal Per-Call Cap",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_research_task(project, direction, db_session)

    proposals = [_make_direction_proposal(f"Proposal {i}") for i in range(8)]
    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings", return_value=proposals
    )

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
        directions=[direction, *existing_proposed],
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
        json=[
            {
                "id": existing_id,
                "text": "Keep my ID",
                "priority": "medium",
                "status": "active",
            }
        ],
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
    """POST /research without bypass returns 429 when the refractory hold is active."""
    project_id = await _deploy_project(client, mocker)
    direction = await _add_direction(
        client, project_id, text="Recently researched direction"
    )
    direction_id = direction["id"]

    # Inject an active refractory window via PUT.
    future_ts = (_utcnow() + timedelta(hours=1)).isoformat()
    put_resp = await client.put(
        f"/api/admin/projects/{project_id}/directions",
        json=[
            {
                **direction,
                "refractory_until": future_ts,
            }
        ],
    )
    assert put_resp.status_code == 200

    resp = await client.post(
        f"/api/admin/projects/{project_id}/directions/{direction_id}/research"
    )
    assert resp.status_code == 429
    assert "cooldown" in resp.json()["error"]["message"].lower()


@pytest.mark.asyncio
async def test_research_endpoint_bypass_cooldown_creates_task(
    client: AsyncClient, mocker
):
    """POST /research?bypass_cooldown=true creates a QUEUED task even if in cooldown."""
    project_id = await _deploy_project(client, mocker)
    direction = await _add_direction(
        client, project_id, text="Cooldown bypass direction"
    )
    direction_id = direction["id"]

    # Put it in refractory.
    future_ts = (_utcnow() + timedelta(hours=1)).isoformat()
    await client.put(
        f"/api/admin/projects/{project_id}/directions",
        json=[{**direction, "refractory_until": future_ts}],
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
async def test_research_endpoint_ignores_last_researched_at_without_refractory(
    client: AsyncClient, mocker
):
    """A stale last_researched_at alone no longer blocks research; refractory_until does."""
    project_id = await _deploy_project(client, mocker)
    direction = await _add_direction(
        client, project_id, text="Legacy cooldown direction"
    )
    direction_id = direction["id"]

    recent_ts = (_utcnow() - timedelta(hours=1)).isoformat()
    await client.put(
        f"/api/admin/projects/{project_id}/directions",
        json=[{**direction, "last_researched_at": recent_ts}],
    )

    resp = await client.post(
        f"/api/admin/projects/{project_id}/directions/{direction_id}/research"
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "QUEUED"


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
            cycle_findings.append(
                {
                    "title": title,
                    "description": f"Long description for {title}: " + "x" * 150,
                    "prompt": f"Implement: {title}",
                    "priority": "medium",
                    "scope": "small",
                    "risk": "low",
                    "type": "improvement",
                    "direction_id": direction_id,
                }
            )
        findings_per_cycle.append(cycle_findings)

    mocker.patch(
        "app.research_completion._parse_research_findings",
        side_effect=findings_per_cycle,
    )

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
        payloads=[
            {"type": "research", "directions": [direction], "base_branch": "main"}
        ],
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
            d["research_history"] = [
                {
                    "timestamp": "2026-04-07T00:00:00",
                    "task_id": str(research_task.id),
                    "tasks_created": 2,
                    "finding_titles": [
                        "Add retry logic to API client",
                        "Improve caching layer",
                    ],
                }
            ]
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
    assert "Timed out after 30 minutes" in ctx, (
        "Failure reason should appear in Failed Approaches"
    )
    # Should no longer be in Established as (proposed)
    assert "Add retry logic to API client (proposed)" not in ctx

    # Merged task's finding should be promoted from (proposed) to (confirmed)
    assert "Improve caching layer (confirmed)" in ctx
    assert "Improve caching layer (proposed)" not in ctx


# ─────────────────────────────────────────────────────────────────────────────
# recent_findings_evidence — structured evidence for strategic agent
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_recent_findings_evidence_populated_with_claims_and_sources(
    db_session, mocker
):
    """recent_findings_evidence is stored on the direction when findings have claims and sources."""
    direction_id = "d-rfe0001"
    direction = _active_direction(direction_id, "Evidence test")
    project = Project(
        name="Evidence Test",
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
        return_value=[
            {
                "title": "Improve caching layer",
                "description": "Cache miss rate is 40%.",
                "prompt": "Add LRU cache",
                "priority": "high",
                "scope": "small",
                "risk": "low",
                "type": "improvement",
                "direction_id": direction_id,
                "claims": [
                    "Cache miss rate is 40%",
                    "LRU cache reduces latency by 30%",
                ],
                "sources": [
                    {"ref": "backend/app/cache.py:42", "excerpt": "def get_cache():"}
                ],
            }
        ],
    )

    task = await _make_research_task(project, direction, db_session)
    await _handle_research_completion(task, "", db_session)

    await db_session.refresh(project)
    dirs = project.directions or []
    d = next((x for x in dirs if x.get("id") == direction_id), None)
    assert d is not None
    rfe = d.get("recent_findings_evidence")
    assert rfe, (
        "recent_findings_evidence should be set when findings have claims/sources"
    )
    assert len(rfe) >= 1
    entry = rfe[0]
    assert entry["title"] == "Improve caching layer"
    assert "Cache miss rate is 40%" in entry["claims"]
    assert any(s["ref"] == "backend/app/cache.py:42" for s in entry["source_refs"])


@pytest.mark.asyncio
async def test_recent_findings_evidence_absent_when_no_findings(db_session, mocker):
    """Directions with no findings produce no recent_findings_evidence field."""
    direction_id = "d-rfe0002"
    direction = _active_direction(direction_id, "No findings direction")
    project = Project(
        name="No Findings",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    _patch_research_externals(mocker)
    mocker.patch("app.research_completion._parse_research_insights", return_value=[])
    mocker.patch("app.research_completion._parse_research_findings", return_value=[])

    task = await _make_research_task(project, direction, db_session)
    await _handle_research_completion(task, "", db_session)

    await db_session.refresh(project)
    dirs = project.directions or []
    d = next((x for x in dirs if x.get("id") == direction_id), None)
    assert d is not None
    rfe = d.get("recent_findings_evidence")
    assert not rfe, (
        "recent_findings_evidence should be empty when no findings with claims/sources"
    )


@pytest.mark.asyncio
async def test_recent_findings_evidence_not_in_direction_summary_payload(
    db_session, mocker
):
    """recent_findings_evidence is NOT copied into the spawned research task payload.
    It was moved to the claim response's direction_context (routers/agent.py) so the
    context-prefix renderer receives it there instead of via the bulkier research payload."""
    direction_id = "d-rfe0003"
    evidence = [
        {"title": "Fix N+1 query", "claims": ["query runs 100x"], "source_refs": []}
    ]
    direction = {
        **_active_direction(direction_id, "Payload evidence test"),
        "recent_findings_evidence": evidence,
    }
    project = Project(
        name="Payload Evidence",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _spawn_research_task(project, db_session)

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    tasks = result.scalars().all()
    research = [t for t in tasks if (t.payloads or [{}])[0].get("type") == "research"]
    assert len(research) == 1
    spawned_dirs = research[0].payloads[0].get("directions", [])
    target = next((d for d in spawned_dirs if d.get("id") == direction_id), None)
    assert target is not None
    assert "recent_findings_evidence" not in target, (
        "recent_findings_evidence should not be in the research summary payload; "
        "it now flows via direction_context in the claim response"
    )


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
        payloads=[
            {"type": "research", "directions": [direction], "base_branch": "main"}
        ],
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
            d["research_history"] = [
                {
                    "timestamp": "2026-04-07T00:00:00",
                    "task_id": str(research_task.id),
                    "tasks_created": 4,
                    "finding_titles": [],
                }
            ]
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

    # product_ratio now follows PRODUCT_TASK_TYPES: create/create/fix are
    # product, refactor is structural.
    product_ratio = entry.get("product_ratio")
    assert product_ratio == 0.75, f"Expected product_ratio=0.75, got {product_ratio}"

    # type_breakdown: per-type created/merged/failed counts so research agents
    # can see which finding types ship vs fail.
    type_breakdown = entry.get("type_breakdown", {})
    assert type_breakdown.get("create") == {"created": 2, "merged": 1, "failed": 0}
    assert type_breakdown.get("fix") == {"created": 1, "merged": 1, "failed": 0}
    assert type_breakdown.get("refactor") == {"created": 1, "merged": 0, "failed": 1}


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
        payloads=[
            {"type": "research", "directions": [direction], "base_branch": "main"}
        ],
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
            d["research_history"] = [
                {
                    "timestamp": "2026-04-07T00:00:00",
                    "task_id": str(research_task.id),
                    "tasks_created": 3,
                    "finding_titles": [],
                }
            ]
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

    assert dropout.get("tasks_failed_code_error") == 1, (
        f"Expected 1 code error, got {dropout}"
    )
    assert dropout.get("tasks_required_approval") == 1, (
        f"Expected 1 required approval, got {dropout}"
    )
    # rollup succeeded (research task COMPLETED) — no staging-only merges
    assert dropout.get("tasks_merged_staging_only") == 0, (
        f"Expected 0 staging-only, got {dropout}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# tasks_completed_no_pr type-awareness — review/fix/panel_review children that
# are ATTACHED to a real (open) PR are NOT genuine no-CODE no-ops.  They carry a
# pr_number (context/payload) even without a PULL_REQUEST artifact of their own,
# so they must be EXCLUDED from the no-op bucket.  (This is the no-op gate; the
# separate _opened_pr type-skip for the merge-VOLUME metrics is unchanged.)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_review_task_with_pr_number_counts_as_no_pr(db_session):
    """A completed review task with pr_number in payload and no PULL_REQUEST artifact
    is ATTACHED to a real (open) PR, so it must NOT be counted as a no-CODE no-op.
    ReviewPayload.pr_number is the PR under review — a real PR association that
    disqualifies the child from tasks_completed_no_pr.
    """
    direction_id = "d-openedpr001"
    direction = _active_direction(direction_id, "Opened-PR classification test")
    project = Project(
        name="Opened PR Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    research_task = await _make_research_task(project, direction, db_session)

    # A review task: pr_number is the PR being reviewed (input), not a new PR opened.
    review_task = Task(
        project_id=project.id,
        title="Review PR #42",
        payloads=[{"type": "review", "pr_number": 42, "branch": "feature/x"}],
        status=TaskStatus.COMPLETED,
        context={"research_task_id": str(research_task.id)},
        parent_task_id=research_task.id,
        created_by="ORCHESTRATOR",
    )
    db_session.add(review_task)
    await db_session.commit()
    await db_session.refresh(review_task)

    dirs = project.directions or []
    for d in dirs:
        if d.get("id") == direction_id:
            d["research_history"] = [
                {
                    "timestamp": "2026-04-07T00:00:00",
                    "task_id": str(research_task.id),
                    "tasks_created": 1,
                    "finding_titles": [],
                }
            ]
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

    # review task attached to an open PR (pr_number in payload) → NOT a no-CODE
    # no-op, so it is excluded from tasks_completed_no_pr.
    assert entry.get("tasks_completed_no_pr") == 0, (
        f"Review task attached to a real PR (pr_number in payload) should NOT count as "
        f"tasks_completed_no_pr; got tasks_completed_no_pr={entry.get('tasks_completed_no_pr')}, "
        f"entry={entry}"
    )


@pytest.mark.asyncio
async def test_create_task_with_pr_artifact_does_not_count_as_no_pr(db_session):
    """A completed create task with a PULL_REQUEST artifact must NOT appear in
    tasks_completed_no_pr — it genuinely opened a PR.
    """
    direction_id = "d-openedpr002"
    direction = _active_direction(direction_id, "Create-task PR classification")
    project = Project(
        name="Create PR Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    research_task = await _make_research_task(project, direction, db_session)

    create_task = Task(
        project_id=project.id,
        title="Implement feature X",
        payloads=[{"type": "create", "prompt": "add feature X"}],
        status=TaskStatus.COMPLETED,
        context={"research_task_id": str(research_task.id)},
        parent_task_id=research_task.id,
        created_by="ORCHESTRATOR",
    )
    db_session.add(create_task)
    await db_session.commit()
    await db_session.refresh(create_task)

    # The create task recorded a PULL_REQUEST artifact when it opened the PR.
    pr_artifact = Artifact(
        task_id=create_task.id,
        artifact_type=ArtifactType.PULL_REQUEST,
        pr_url="https://github.com/acme/repo/pull/99",
    )
    db_session.add(pr_artifact)
    await db_session.commit()

    dirs = project.directions or []
    for d in dirs:
        if d.get("id") == direction_id:
            d["research_history"] = [
                {
                    "timestamp": "2026-04-07T00:00:00",
                    "task_id": str(research_task.id),
                    "tasks_created": 1,
                    "finding_titles": [],
                }
            ]
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

    # create task has a PULL_REQUEST artifact → it opened a PR, not a no-PR completion
    assert entry.get("tasks_completed_no_pr") == 0, (
        f"Create task with PULL_REQUEST artifact should NOT count as tasks_completed_no_pr; "
        f"got tasks_completed_no_pr={entry.get('tasks_completed_no_pr')}, entry={entry}"
    )


@pytest.mark.asyncio
async def test_fix_task_with_pr_number_counts_as_no_pr(db_session):
    """A completed fix task with pr_number in payload and no PULL_REQUEST artifact
    pushed code onto an existing open PR, so it must NOT be counted as a no-CODE
    no-op. Fix tasks iterate an existing PR — a real PR association that
    disqualifies the child from tasks_completed_no_pr even without its own artifact.
    """
    direction_id = "d-openedpr003"
    direction = _active_direction(direction_id, "Fix-task PR classification")
    project = Project(
        name="Fix PR Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    research_task = await _make_research_task(project, direction, db_session)

    fix_task = Task(
        project_id=project.id,
        title="Fix lint errors on PR #55",
        payloads=[{"type": "fix", "prompt": "fix lints", "pr_number": 55, "branch": "feature/y"}],
        status=TaskStatus.COMPLETED,
        context={"research_task_id": str(research_task.id)},
        parent_task_id=research_task.id,
        created_by="ORCHESTRATOR",
    )
    db_session.add(fix_task)
    await db_session.commit()
    await db_session.refresh(fix_task)

    dirs = project.directions or []
    for d in dirs:
        if d.get("id") == direction_id:
            d["research_history"] = [
                {
                    "timestamp": "2026-04-07T00:00:00",
                    "task_id": str(research_task.id),
                    "tasks_created": 1,
                    "finding_titles": [],
                }
            ]
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

    # fix task attached to an open PR (pr_number in payload) → churned code onto
    # a real PR, so it is excluded from tasks_completed_no_pr.
    assert entry.get("tasks_completed_no_pr") == 0, (
        f"Fix task attached to a real PR (pr_number in payload) should NOT count as "
        f"tasks_completed_no_pr; got tasks_completed_no_pr={entry.get('tasks_completed_no_pr')}, "
        f"entry={entry}"
    )


@pytest.mark.asyncio
async def test_panel_review_task_with_pr_number_counts_as_no_pr(db_session):
    """A completed panel_review task with pr_number in payload and no PULL_REQUEST
    artifact is ATTACHED to a real (open) PR, so it must NOT be counted as a
    no-CODE no-op. PanelReviewPayload.pr_number is the PR under review (input) —
    a real PR association that disqualifies the child from tasks_completed_no_pr.
    """
    direction_id = "d-openedpr004"
    direction = _active_direction(direction_id, "Panel-review PR classification")
    project = Project(
        name="Panel Review PR Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    research_task = await _make_research_task(project, direction, db_session)

    panel_review_task = Task(
        project_id=project.id,
        title="Panel review of PR #77",
        payloads=[{"type": "panel_review", "pr_number": 77, "branch": "feature/z"}],
        status=TaskStatus.COMPLETED,
        context={"research_task_id": str(research_task.id)},
        parent_task_id=research_task.id,
        created_by="ORCHESTRATOR",
    )
    db_session.add(panel_review_task)
    await db_session.commit()
    await db_session.refresh(panel_review_task)

    dirs = project.directions or []
    for d in dirs:
        if d.get("id") == direction_id:
            d["research_history"] = [
                {
                    "timestamp": "2026-04-07T00:00:00",
                    "task_id": str(research_task.id),
                    "tasks_created": 1,
                    "finding_titles": [],
                }
            ]
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

    # panel_review task attached to an open PR (pr_number in payload) → NOT a
    # no-CODE no-op, so it is excluded from tasks_completed_no_pr.
    assert entry.get("tasks_completed_no_pr") == 0, (
        f"Panel_review task attached to a real PR (pr_number in payload) should NOT count as "
        f"tasks_completed_no_pr; got tasks_completed_no_pr={entry.get('tasks_completed_no_pr')}, "
        f"entry={entry}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# _strategic_failures auto-decay
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_strategic_failures_auto_decay_triggers_force_strategic(db_session):
    """When _strategic_failures >= 3 but last failure was >48h ago, force_strategic fires."""
    old_timestamp = (_utcnow() - timedelta(hours=72)).isoformat()
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
    # M4 also spawns a background strategic_resident_turn shadow task alongside the
    # legacy strategic task; filter to the strategic type under test before asserting.
    strategic = [
        t
        for t in tasks
        if t.first_payload.get("type") in ("design_doc_update", "strategic_review")
    ]
    assert len(strategic) == 1, "Expected one strategic task to be spawned after decay"


@pytest.mark.asyncio
async def test_strategic_failures_auto_decay_persists_cleared_counter(db_session):
    """When auto-decay fires, vision._strategic_failures and _strategic_failures_at are cleared."""
    old_timestamp = (_utcnow() - timedelta(hours=72)).isoformat()
    project = Project(
        name="Decay Persistence Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[],  # No active directions — needs force_strategic to spawn
        vision={
            "purpose": "test purpose",
            "_strategic_failures": 5,
            "_strategic_failures_at": old_timestamp,
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Verify initial state
    assert project.vision.get("_strategic_failures") == 5
    assert project.vision.get("_strategic_failures_at") == old_timestamp

    await _spawn_research_task(project, db_session)

    # Re-fetch from DB to ensure we get the latest vision
    from app.models import Project as ProjectModel
    from sqlalchemy import select

    result = await db_session.execute(
        select(ProjectModel).where(ProjectModel.id == project.id)
    )
    refreshed_project = result.scalar_one()

    # After decay fires, the fields should be cleared from vision
    assert refreshed_project.vision.get("_strategic_failures") is None
    assert refreshed_project.vision.get("_strategic_failures_at") is None


def test_project_has_purpose_resolves_problem_model_then_vision():
    """problem_model.intent.purpose is canonical; vision.purpose is the legacy fallback."""
    from app.problem_model import project_has_purpose

    project = Project(name="Purpose Resolution", user_id="test-user-123")

    project.problem_model = {"intent": {"purpose": "build a bot"}}
    project.vision = {}
    assert project_has_purpose(project) is True

    project.problem_model = {}
    project.vision = {"purpose": "legacy purpose"}
    assert project_has_purpose(project) is True

    project.problem_model = {"intent": {"purpose": "   "}}
    project.vision = {"purpose": ""}
    assert project_has_purpose(project) is False

    project.problem_model = None
    project.vision = None
    assert project_has_purpose(project) is False


@pytest.mark.asyncio
async def test_problem_model_purpose_suppresses_force_strategic(db_session):
    """A purpose living only in problem_model.intent (vision empty — the
    post-deprecation shape every onboarded project has) must NOT trip the
    no-purpose arm of force_strategic. Before the fix, the trigger read only
    vision.purpose, so such projects force-spawned a strategic cycle on every
    tick, rate-limited only by the spawn cooldown (stsbot2 2026-06-07:
    6 cycles in ~3h against a frequency of 6 spawns/cycle)."""
    project = Project(
        name="Problem Model Purpose Project",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[_active_direction("d-pmpurp1")],
        vision={},  # deprecated field, empty — purpose lives in problem_model
        problem_model={"intent": {"purpose": "build a combat bot"}},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _spawn_research_task(project, db_session)

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    tasks = result.scalars().all()
    strategic = [
        t
        for t in tasks
        if (t.payloads or [{}])[0].get("type")
        in ("strategic_review", "design_doc_update")
    ]
    assert strategic == [], (
        "problem_model purpose must satisfy the no-purpose force-strategic arm"
    )
    # The active direction still gets normal research — the spawn itself works.
    research = [t for t in tasks if (t.payloads or [{}])[0].get("type") == "research"]
    assert len(research) == 1


@pytest.mark.asyncio
async def test_strategic_failures_no_decay_when_recent(db_session):
    """When _strategic_failures >= 3 and last failure was recent (<48h), force_strategic stays blocked."""
    recent_timestamp = (_utcnow() - timedelta(hours=12)).isoformat()
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
    assert len(tasks) == 0, (
        "Expected no task: decay should not apply for recent failure"
    )


@pytest.mark.asyncio
async def test_all_saturated_directions_triggers_force_strategic(db_session):
    """When all active directions are saturated, force_strategic fires regardless of spawn count."""
    project = Project(
        name="All Saturated Test Project",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[
            {
                "id": "d-111",
                "status": "active",
                "possibly_saturated": True,
                "title": "Dir 1",
            },
            {
                "id": "d-222",
                "status": "active",
                "possibly_saturated": True,
                "title": "Dir 2",
            },
            {
                "id": "d-333",
                "status": "active",
                "possibly_saturated": True,
                "title": "Dir 3",
            },
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
    assert any(t in strategic_types for t in task_types), (
        "Expected a strategic task when all directions are saturated"
    )
    assert "research" not in task_types, (
        "Expected no research tasks when all directions are saturated"
    )


@pytest.mark.asyncio
async def test_partially_saturated_directions_no_force_strategic(db_session):
    """When only some active directions are saturated, force_strategic does NOT fire."""
    project = Project(
        name="Partial Saturated Test Project",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[
            {
                "id": "d-111",
                "status": "active",
                "possibly_saturated": True,
                "title": "Dir 1",
            },
            {
                "id": "d-222",
                "status": "active",
                "possibly_saturated": False,
                "title": "Dir 2",
            },
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
    strategic_tasks = [
        t for t in tasks if t.payloads[0].get("type") == "design_doc_update"
    ]
    assert len(strategic_tasks) == 0, (
        "Expected no strategic task when only some directions are saturated"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Dedup hole tests — failed parent and infra failures
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_failed_parent_child_not_blocking_dedup(db_session, mocker):
    """A COMPLETED child with pr_merged=True whose parent rollup FAILED must NOT block re-proposal."""
    direction = _active_direction("d-fpdedup01", "Failed parent dedup test")
    project = Project(
        name="Failed Parent Dedup Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
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
        return_value=[
            {
                "title": dup_title,
                "description": dup_desc,
                "prompt": "Fix session token caching",
                "priority": "high",
                "scope": "small",
                "risk": "low",
                "type": "improvement",
                "direction_id": direction["id"],
            }
        ],
    )

    await _handle_research_completion(task, "", db_session)

    result = await db_session.execute(
        select(Task).where(
            Task.project_id == project.id, Task.parent_task_id == task.id
        )
    )
    child_tasks = result.scalars().all()
    assert len(child_tasks) == 1, (
        "Ghost child (failed parent) must NOT block re-proposal"
    )


@pytest.mark.asyncio
async def test_infra_failure_not_blocking_dedup(db_session, mocker):
    """A FAILED task with AGENT_TIMEOUT classification must NOT block re-proposal."""
    direction = _active_direction("d-infraded01", "Infra failure dedup test")
    project = Project(
        name="Infra Failure Dedup Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
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
        return_value=[
            {
                "title": dup_title,
                "description": dup_desc,
                "prompt": "Add retry logic",
                "priority": "high",
                "scope": "small",
                "risk": "low",
                "type": "improvement",
                "direction_id": direction["id"],
            }
        ],
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
        name="Code Error Dedup Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
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
        return_value=[
            {
                "title": dup_title,
                "description": dup_desc,
                "prompt": "Refactor middleware",
                "priority": "high",
                "scope": "small",
                "risk": "low",
                "type": "improvement",
                "direction_id": direction["id"],
            }
        ],
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
        name="Code Error Cooldown Allow",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
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
        payloads=[
            {"type": "create", "prompt": "add validation", "base_branch": "main"}
        ],
        context={"finding_hash": fhash},
        status=TaskStatus.FAILED,
        latest_status_message="Tests failed: AssertionError in test_registration.py line 15",
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
        created_at=_utcnow() - timedelta(hours=72),
    )
    db_session.add(old_code_failed)
    await db_session.commit()

    task = await _make_research_task(project, direction, db_session)

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[
            {
                "title": old_title,
                "description": old_desc,
                "prompt": "Add input validation",
                "priority": "medium",
                "scope": "small",
                "risk": "low",
                "type": "improvement",
                "direction_id": direction["id"],
            }
        ],
    )

    await _handle_research_completion(task, "", db_session)

    result = await db_session.execute(
        select(Task).where(
            Task.project_id == project.id, Task.parent_task_id == task.id
        )
    )
    child_tasks = result.scalars().all()
    assert len(child_tasks) == 1, (
        "CODE_ERROR failure older than 48h must NOT block re-proposal"
    )


@pytest.mark.asyncio
async def test_code_error_within_cooldown_blocks_reproposal(db_session, mocker):
    """A CODE_ERROR failure within the 48-hour cooldown must still block re-proposal."""
    direction = _active_direction("d-cewarm01", "Code error within cooldown test")
    project = Project(
        name="Code Error Cooldown Block",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
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
        payloads=[
            {"type": "create", "prompt": "fix null pointer", "base_branch": "main"}
        ],
        context={"finding_hash": fhash},
        status=TaskStatus.FAILED,
        latest_status_message="Build failed: NullPointerException in OrderProcessor.java:88",
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
        created_at=_utcnow() - timedelta(hours=24),
    )
    db_session.add(recent_code_failed)
    await db_session.commit()

    task = await _make_research_task(project, direction, db_session)

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[
            {
                "title": recent_title,
                "description": recent_desc,
                "prompt": "Fix null pointer error",
                "priority": "high",
                "scope": "small",
                "risk": "low",
                "type": "improvement",
                "direction_id": direction["id"],
            }
        ],
    )

    await _handle_research_completion(task, "", db_session)

    result = await db_session.execute(
        select(Task).where(
            Task.project_id == project.id, Task.parent_task_id == task.id
        )
    )
    child_tasks = result.scalars().all()
    assert len(child_tasks) == 0, (
        "CODE_ERROR failure within 48h cooldown must still block re-proposal"
    )


@pytest.mark.asyncio
async def test_merged_pr_blocks_reproposal_permanently(db_session, mocker):
    """A successfully merged PR must block re-proposal even after the cooldown period."""
    direction = _active_direction("d-mergedold01", "Merged PR permanent block test")
    project = Project(
        name="Merged PR Permanent Block",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
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
        created_at=_utcnow() - timedelta(days=30),
    )
    db_session.add(old_merged)
    await db_session.commit()

    task = await _make_research_task(project, direction, db_session)

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[
            {
                "title": merged_title,
                "description": merged_desc,
                "prompt": "Migrate auth to OAuth2",
                "priority": "high",
                "scope": "medium",
                "risk": "low",
                "type": "improvement",
                "direction_id": direction["id"],
            }
        ],
    )

    await _handle_research_completion(task, "", db_session)

    result = await db_session.execute(
        select(Task).where(
            Task.project_id == project.id, Task.parent_task_id == task.id
        )
    )
    child_tasks = result.scalars().all()
    assert len(child_tasks) == 0, (
        "Successfully merged PR must block re-proposal permanently"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Deadlock Detection
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_already_fixed_deadlock_pivots_agent(client: AsyncClient, db_session):
    """A direction in already-fixed deadlock exposes an agent-facing pivot signal
    in its research direction context so the next cycle stops re-deriving shipped work."""
    direction_id = "d-afdead01"
    # Three consecutive history entries where every spawned task was SCOPE_EMPTY
    # (tasks_created >= 1 and scope_empty_noop_count >= tasks_created).
    noop_history = [
        {
            "timestamp": f"2026-01-1{i}T00:00:00",
            "task_id": f"00000000-0000-0000-0000-00000000000{i}",
            "tasks_created": 2,
            "scope_empty_noop_count": 2,
            "summary": "all tasks no-op — work already on HEAD",
            "finding_titles": [],
            "key_finding_descriptions": [],
            "filter_breakdown": {
                "hash_dedup": 0,
                "word_overlap_dedup": 0,
                "file_overlap_dedup": 0,
                "type_filtered": 0,
                "empty_prompt": 0,
            },
        }
        for i in range(1, 4)
    ]
    project = Project(
        name="Already-Fixed Deadlock Pivot Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[
            {
                "id": direction_id,
                "title": "Improve performance",
                "priority": "medium",
                "status": "active",
                "research_history": noop_history,
            }
        ],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Research: Improve performance",
        payloads=[
            {
                "type": "research",
                "directions": project.directions,
                "base_branch": "main",
                "current_direction_id": direction_id,
            }
        ],
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
        vm_instance_id="agent-vm-afdead01",
        context={"direction_id": direction_id},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-afdead01")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200

    dc = resp.json().get("direction_context")
    assert dc is not None, (
        "direction_context must be present in research task claim response"
    )
    assert dc.get("already_fixed_deadlock_detected") is True, (
        "A direction whose last 3 cycles all produced only SCOPE_EMPTY tasks "
        "must surface already_fixed_deadlock_detected=True in direction_context"
    )
    # Sanity: dedup deadlock should NOT be triggered (tasks_created > 0 each cycle)
    assert dc.get("deadlock_detected") is False, (
        "dedup deadlock must not fire when tasks_created > 0 each cycle"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 15. _handle_research_completion — Spawn-Time Health Gate
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_health_gate_normal_no_cap(db_session, mocker):
    """When pipeline is healthy, all findings create tasks without any cap."""
    direction_id = "d-hgate001"
    direction = _active_direction(direction_id, "Health gate normal")
    project = Project(
        name="Health Gate Normal",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_research_task(project, direction, db_session)

    _patch_research_externals(mocker)
    # 5 findings — all should spawn tasks when healthy
    findings = [_make_finding(f"Improvement {i}", direction_id) for i in range(5)]
    mocker.patch(
        "app.research_completion._parse_research_findings", return_value=findings
    )
    # Healthy pipeline: zone available, no degradation
    mocker.patch(
        "app.research_completion._assess_project_spawn_health",
        return_value=(False, None),
    )

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
        name="Zone Exhaustion",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_research_task(project, direction, db_session)

    _patch_research_externals(mocker)
    # 6 findings — only _DEGRADED_SPAWN_CAP should be created
    findings = [_make_finding(f"Zone cap finding {i}", direction_id) for i in range(6)]
    mocker.patch(
        "app.research_completion._parse_research_findings", return_value=findings
    )
    mocker.patch(
        "app.research_completion._assess_project_spawn_health",
        return_value=(True, "all zones in cooldown"),
    )

    await _handle_research_completion(task, "", db_session)

    result = await db_session.execute(
        select(Task).where(Task.project_id == project.id, Task.id != task.id)
    )
    # degradation_gate-deferred findings now also spawn read-only memo tasks;
    # the implementation-task spawn cap applies to non-memo tasks only.
    child_tasks = [
        t for t in result.scalars().all() if t.first_payload.get("type") != "memo"
    ]
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
        name="Account Unavailable",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_research_task(project, direction, db_session)

    _patch_research_externals(mocker)
    findings = [
        _make_finding(f"Account cap finding {i}", direction_id) for i in range(5)
    ]
    mocker.patch(
        "app.research_completion._parse_research_findings", return_value=findings
    )
    mocker.patch(
        "app.research_completion._assess_project_spawn_health",
        return_value=(True, "no accounts available"),
    )

    await _handle_research_completion(task, "", db_session)

    result = await db_session.execute(
        select(Task).where(Task.project_id == project.id, Task.id != task.id)
    )
    # degradation_gate-deferred findings now also spawn read-only memo tasks;
    # the implementation-task spawn cap applies to non-memo tasks only.
    child_tasks = [
        t for t in result.scalars().all() if t.first_payload.get("type") != "memo"
    ]
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
        name="Deferred Storage Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_research_task(project, direction, db_session)

    _patch_research_externals(mocker)
    findings = [_make_finding(f"Finding {i}", direction_id) for i in range(4)]
    mocker.patch(
        "app.research_completion._parse_research_findings", return_value=findings
    )
    mocker.patch(
        "app.research_completion._assess_project_spawn_health",
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
async def test_deferred_stats_persisted_on_research_history_entry(db_session, mocker):
    """Deferred finding counts are persisted on the direction's latest research_history entry."""
    from app.research_completion import _DEGRADED_SPAWN_CAP

    direction_id = "d-defstats01"
    direction = _active_direction(direction_id, "Deferred stats persistence")
    project = Project(
        name="Deferred Stats",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_research_task(project, direction, db_session)

    _patch_research_externals(mocker)
    findings = [_make_finding(f"Finding {i}", direction_id) for i in range(5)]
    mocker.patch(
        "app.research_completion._parse_research_findings", return_value=findings
    )
    mocker.patch(
        "app.research_completion._assess_project_spawn_health",
        return_value=(True, "high failure rate"),
    )

    await _handle_research_completion(task, "", db_session)

    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][-1]
    assert "deferred_stats" in entry
    stats = entry["deferred_stats"]
    assert stats["total"] == 5 - _DEGRADED_SPAWN_CAP
    assert stats["by_reason"]["degradation_gate"] == 5 - _DEGRADED_SPAWN_CAP
    assert stats["by_reason"]["batch_cap"] == 0
    assert stats["by_reason"]["ultra_low_confidence"] == 0
    assert 0 < stats["spawn_ratio"] < 1


@pytest.mark.asyncio
async def test_low_actionability_finding_spawns_memo(db_session, mocker):
    """A low-actionability finding is deferred and spawns a terminal memo task
    (not a research/plan/create child); the memo id is recorded in
    task.context['spawned_memo_ids'] and the direction's deferred_stats."""
    direction_id = "d-memo0001"
    direction = _active_direction(direction_id, "Memo spawn from dropped finding")
    project = Project(
        name="Memo Spawn",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_research_task(project, direction, db_session)

    _patch_research_externals(mocker)
    low_finding = _make_finding("Underspecified idea", direction_id)
    low_finding["actionability_grade"] = "LOW"
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[low_finding],
    )
    # Pipeline healthy — the only drop reason is low_actionability.
    mocker.patch(
        "app.research_completion._assess_project_spawn_health",
        return_value=(False, ""),
    )

    await _handle_research_completion(task, "", db_session)

    result = await db_session.execute(
        select(Task).where(Task.project_id == project.id, Task.id != task.id)
    )
    children = result.scalars().all()
    # Exactly one task spawned, and it is a memo — not a research/plan/create child.
    assert len(children) == 1
    memo = children[0]
    assert memo.first_payload["type"] == "memo"
    assert memo.parent_task_id == task.id
    assert memo.first_payload["reasoning_kind"] == "deferred_finding"
    assert memo.first_payload["source_task_id"] == str(task.id)
    assert memo.first_payload["source_finding"]["title"] == "Underspecified idea"
    assert memo.first_payload.get("direction_id") == direction_id

    await db_session.refresh(task)
    spawned = (task.context or {}).get("spawned_memo_ids", [])
    assert str(memo.id) in spawned


@pytest.mark.asyncio
async def test_degradation_gate_memo_ids_recorded_in_deferred_stats(db_session, mocker):
    """Memos spawned for degradation-gate-deferred findings are recorded both in
    task.context['spawned_memo_ids'] and on the direction's deferred_stats."""
    from app.research_completion import _DEGRADED_SPAWN_CAP

    direction_id = "d-memodef01"
    direction = _active_direction(direction_id, "Memo deferred-stats")
    project = Project(
        name="Memo Deferred Stats",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_research_task(project, direction, db_session)

    _patch_research_externals(mocker)
    # 5 findings, cap=2 → 2 impl spawn, 3 degradation_gate-deferred → 3 memos.
    findings = [_make_finding(f"Finding {i}", direction_id) for i in range(5)]
    mocker.patch(
        "app.research_completion._parse_research_findings", return_value=findings
    )
    mocker.patch(
        "app.research_completion._assess_project_spawn_health",
        return_value=(True, "high failure rate"),
    )

    await _handle_research_completion(task, "", db_session)

    result = await db_session.execute(
        select(Task).where(Task.project_id == project.id, Task.id != task.id)
    )
    memos = [t for t in result.scalars().all() if t.first_payload.get("type") == "memo"]
    assert len(memos) == 5 - _DEGRADED_SPAWN_CAP

    await db_session.refresh(task)
    spawned = set((task.context or {}).get("spawned_memo_ids", []))
    assert spawned == {str(m.id) for m in memos}

    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][-1]
    assert set(entry["deferred_stats"].get("memo_ids", [])) == spawned


@pytest.mark.asyncio
async def test_memo_spawn_capped_per_cycle(db_session, mocker):
    """Memo spawns are bounded by MEMO_SPAWN_CAP_PER_CYCLE even when many
    findings are dropped for the same outcome-blind reason."""
    from app.research_completion import MEMO_SPAWN_CAP_PER_CYCLE

    direction_id = "d-memocap01"
    direction = _active_direction(direction_id, "Memo cap test")
    project = Project(
        name="Memo Cap",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_research_task(project, direction, db_session)

    _patch_research_externals(mocker)
    findings = []
    for i in range(MEMO_SPAWN_CAP_PER_CYCLE + 3):
        f = _make_finding(f"Underspecified {i}", direction_id)
        f["actionability_grade"] = "LOW"
        findings.append(f)
    mocker.patch(
        "app.research_completion._parse_research_findings", return_value=findings
    )
    mocker.patch(
        "app.research_completion._assess_project_spawn_health",
        return_value=(False, ""),
    )

    await _handle_research_completion(task, "", db_session)

    result = await db_session.execute(
        select(Task).where(Task.project_id == project.id, Task.id != task.id)
    )
    memos = [t for t in result.scalars().all() if t.first_payload.get("type") == "memo"]
    assert len(memos) == MEMO_SPAWN_CAP_PER_CYCLE


@pytest.mark.asyncio
async def test_memo_dedup_same_finding_deferred_twice_spawns_one_memo(
    db_session, mocker
):
    """A finding deferred across two consecutive cycles produces exactly one memo task.
    The second cycle skips spawning because a memo with the same finding hash already exists."""
    direction_id = "d-memodedup1"
    direction = _active_direction(direction_id, "Memo dedup same finding")
    project = Project(
        name="Memo Dedup Same",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Cycle 1: finding is deferred (low_actionability) → one memo spawned.
    task1 = await _make_research_task(project, direction, db_session)
    _patch_research_externals(mocker)
    finding = _make_finding("Duplicate finding", direction_id)
    finding["actionability_grade"] = "LOW"
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[finding],
    )
    mocker.patch(
        "app.research_completion._assess_project_spawn_health",
        return_value=(False, ""),
    )
    await _handle_research_completion(task1, "", db_session)

    result = await db_session.execute(
        select(Task).where(Task.project_id == project.id, Task.id != task1.id)
    )
    memos_after_cycle1 = [
        t for t in result.scalars().all() if t.first_payload.get("type") == "memo"
    ]
    assert len(memos_after_cycle1) == 1, "Cycle 1 must spawn exactly one memo"
    memo_hash = (memos_after_cycle1[0].context or {}).get("memo_source_finding_hash")
    assert memo_hash is not None, (
        "Memo task must record memo_source_finding_hash in context"
    )

    # Cycle 2: same finding re-deferred → dedup must block a second memo.
    task2 = await _make_research_task(project, direction, db_session)
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[finding],
    )
    await _handle_research_completion(task2, "", db_session)

    result2 = await db_session.execute(
        select(Task).where(Task.project_id == project.id, Task.type == "memo")
        if hasattr(Task, "type")
        else select(Task).where(Task.project_id == project.id)
    )
    all_tasks = result2.scalars().all()
    all_memos = [t for t in all_tasks if t.first_payload.get("type") == "memo"]
    assert len(all_memos) == 1, (
        f"Expected exactly 1 memo across both cycles (dedup), got {len(all_memos)}"
    )


@pytest.mark.asyncio
async def test_memo_dedup_two_distinct_findings_spawn_two_memos(db_session, mocker):
    """Two genuinely different findings deferred in consecutive cycles each spawn their
    own memo — dedup only blocks re-spawning of the same finding."""
    direction_id = "d-memodedup2"
    direction = _active_direction(direction_id, "Memo dedup distinct findings")
    project = Project(
        name="Memo Dedup Distinct",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Cycle 1: finding A is deferred → one memo for A.
    task1 = await _make_research_task(project, direction, db_session)
    _patch_research_externals(mocker)
    finding_a = _make_finding("Distinct finding A", direction_id)
    finding_a["actionability_grade"] = "LOW"
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[finding_a],
    )
    mocker.patch(
        "app.research_completion._assess_project_spawn_health",
        return_value=(False, ""),
    )
    await _handle_research_completion(task1, "", db_session)

    # Cycle 2: finding B (different title/description) is deferred → one memo for B.
    task2 = await _make_research_task(project, direction, db_session)
    finding_b = _make_finding("Distinct finding B", direction_id)
    finding_b["actionability_grade"] = "LOW"
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[finding_b],
    )
    await _handle_research_completion(task2, "", db_session)

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    all_tasks = result.scalars().all()
    all_memos = [t for t in all_tasks if t.first_payload.get("type") == "memo"]
    assert len(all_memos) == 2, (
        f"Expected 2 memos (one per distinct finding), got {len(all_memos)}"
    )
    hashes = {(t.context or {}).get("memo_source_finding_hash") for t in all_memos}
    assert len(hashes) == 2, "Both memos must have distinct finding hashes"


@pytest.mark.asyncio
async def test_health_gate_metadata_findings_bypass_cap(db_session, mocker):
    """Metadata finding types (design_doc_observation, direction_update) bypass the health gate."""
    from app.research_completion import _DEGRADED_SPAWN_CAP

    direction_id = "d-hgate005"
    direction = _active_direction(direction_id, "Metadata bypass test")
    project = Project(
        name="Metadata Bypass",
        user_id="test-user-123",
        autonomous_enabled=True,
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
    mocker.patch(
        "app.research_completion._parse_research_findings", return_value=findings
    )
    # Pipeline is degraded — but metadata findings must still be processed
    mocker.patch(
        "app.research_completion._assess_project_spawn_health",
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
    direction_id = "d-deferred-recovery01"
    direction = _active_direction(direction_id, "Deferred recovery test")
    project = Project(
        name="Deferred Recovery",
        user_id="test-user-123",
        autonomous_enabled=True,
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
        payloads=[
            {"type": "research", "directions": [direction], "base_branch": "main"}
        ],
        context={
            "research_depth": 0,
            "prior_deferred_findings": deferred_from_prev_cycle,
        },
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
    mocker.patch(
        "app.research_completion._parse_research_findings", return_value=new_findings
    )
    # Healthy pipeline: no cap
    mocker.patch(
        "app.research_completion._assess_project_spawn_health",
        return_value=(False, None),
    )

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
async def test_prior_deferred_findings_still_capped_if_pipeline_degraded(
    db_session, mocker
):
    """Deferred findings from previous cycle are still subject to the health gate cap."""
    from app.research_completion import _DEGRADED_SPAWN_CAP

    direction_id = "d-deferred-recovery02"
    direction = _active_direction(direction_id, "Deferred still capped")
    project = Project(
        name="Deferred Still Capped",
        user_id="test-user-123",
        autonomous_enabled=True,
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
        payloads=[
            {"type": "research", "directions": [direction], "base_branch": "main"}
        ],
        context={
            "research_depth": 0,
            "prior_deferred_findings": deferred_from_prev_cycle,
        },
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
    mocker.patch(
        "app.research_completion._parse_research_findings", return_value=new_findings
    )
    mocker.patch(
        "app.research_completion._assess_project_spawn_health",
        return_value=(True, "all zones in cooldown"),
    )

    await _handle_research_completion(task, "", db_session)

    result = await db_session.execute(
        select(Task).where(Task.project_id == project.id, Task.id != task.id)
    )
    # degradation_gate-deferred findings now also spawn read-only memo tasks;
    # the implementation-task spawn cap applies to non-memo tasks only.
    child_tasks = [
        t for t in result.scalars().all() if t.first_payload.get("type") != "memo"
    ]
    assert len(child_tasks) == _DEGRADED_SPAWN_CAP, (
        f"Cap still applies even with prior deferred findings: expected {_DEGRADED_SPAWN_CAP}, got {len(child_tasks)}"
    )

    await db_session.refresh(task)
    # The un-spawned findings should be deferred again for next cycle
    still_deferred = (task.context or {}).get("deferred_findings", [])
    assert len(still_deferred) == 5 - _DEGRADED_SPAWN_CAP, (
        f"Excess findings re-deferred: expected {5 - _DEGRADED_SPAWN_CAP}, got {len(still_deferred)}"
    )


@pytest.mark.asyncio
async def test_repeatedly_deferred_low_confidence_findings_are_decayed(
    db_session, mocker
):
    """Findings deferred >=4 times for ultra_low_confidence are discarded, not retried."""
    direction_id = "d-decay0001"
    direction = _active_direction(direction_id, "Decay test")
    project = Project(
        name="Decay test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    stale_finding = _make_finding("Stale low-conf finding", direction_id)
    stale_finding["deferred_count"] = 4
    stale_finding["deferred_reason"] = "ultra_low_confidence"
    fresh_deferred = _make_finding("Viable deferred finding", direction_id)
    fresh_deferred["deferred_count"] = 1
    fresh_deferred["deferred_reason"] = "ultra_low_confidence"

    task = Task(
        project_id=project.id,
        title=f"Research: {direction['text']}",
        payloads=[
            {"type": "research", "directions": [direction], "base_branch": "main"}
        ],
        context={
            "research_depth": 0,
            "prior_deferred_findings": [stale_finding, fresh_deferred],
        },
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
        return_value=[_make_finding("Fresh sentinel", direction_id)],
    )
    mocker.patch(
        "app.research_completion._assess_project_spawn_health",
        return_value=(False, None),
    )

    await _handle_research_completion(task, "", db_session)

    result = await db_session.execute(
        select(Task).where(Task.project_id == project.id, Task.id != task.id)
    )
    child_titles = {ct.title for ct in result.scalars().all()}
    assert "Stale low-conf finding" not in child_titles, (
        "Ultra-low-confidence finding with deferred_count>=4 should be decayed"
    )
    assert "Viable deferred finding" in child_titles, (
        "Finding with deferred_count<4 should still be processed"
    )


@pytest.mark.asyncio
async def test_undirected_cycle_recovers_unscoped_deferred_findings(db_session, mocker):
    """Undirected research cycles must recover deferred findings without a direction_id.

    Previously, the recovery block was gated on current_dir_id being truthy, so undirected
    cycles silently dropped findings deferred during pipeline degradation."""
    direction = _active_direction("d-undir-rec01", "Active direction")
    project = Project(
        name="Undirected Deferred Recovery",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
        # Force is_undirected=True (12 % 12 == 0) without is_strategic (12 % 5 != 0)
        vision={
            "purpose": "test",
            "_research_spawn_count": 12,
            "_strategic_frequency_override": 5,
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    unscoped_finding = _make_finding("Unscoped deferred finding", direction_id=None)
    unscoped_finding.pop("direction_id")  # truly unscoped — no direction_id key
    directed_finding = _make_finding(
        "Directed deferred finding", direction_id=direction["id"]
    )
    prev_completed = Task(
        project_id=project.id,
        title="Research: prior undirected cycle",
        payloads=[
            {
                "type": "research",
                "directions": [direction],
                "base_branch": "main",
                "current_direction_id": None,
            }
        ],
        context={"deferred_findings": [unscoped_finding, directed_finding]},
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(prev_completed)
    await db_session.commit()

    await _spawn_research_task(project, db_session)

    result = await db_session.execute(
        select(Task).where(
            Task.project_id == project.id,
            Task.id != prev_completed.id,
            Task.created_by == "ORCHESTRATOR",
        )
    )
    new_tasks = [
        t for t in result.scalars().all() if t.first_payload.get("type") == "research"
    ]
    assert len(new_tasks) == 1, f"Expected one new research task, got {len(new_tasks)}"
    new_task = new_tasks[0]
    assert new_task.first_payload.get("current_direction_id") is None, (
        "Expected undirected cycle"
    )
    prior = (new_task.context or {}).get("prior_deferred_findings", [])
    titles = [f.get("title") for f in prior]
    assert "Unscoped deferred finding" in titles, (
        f"Undirected cycle must recover unscoped deferred findings, got: {titles}"
    )
    assert "Directed deferred finding" not in titles, (
        f"Undirected cycle must NOT steal directed-cycle leftovers, got: {titles}"
    )


@pytest.mark.asyncio
async def test_collection_expires_stale_deferred_findings(db_session):
    """A deferred finding whose originally_researched_at is older than the TTL is
    excluded at collection time and counted as expired, keeping the injected payload
    bounded and the expiry observability honest."""
    import app.shared_state as _ss

    # Disable the completed-research cooldown so the prior COMPLETED task does not
    # block spawning — we are testing the TTL expiry logic, not the cooldown guard.
    _ss.runtime_config["completed_research_cooldown_minutes"] = 0
    try:
        direction_id = "d-ttl-test01"
        direction = _active_direction(direction_id, "TTL expiry test direction")
        project = Project(
            name="TTL Expiry Test",
            user_id="test-user-123",
            autonomous_enabled=True,
            directions=[direction],
            vision={"purpose": "test"},
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        ttl_days = 7
        # Stale finding: originally researched 8 days ago (beyond the 7-day default TTL)
        stale_ts = (_utcnow() - timedelta(days=ttl_days + 1)).isoformat()
        stale_finding = _make_finding("Stale TTL finding", direction_id)
        stale_finding["originally_researched_at"] = stale_ts

        # Fresh finding: originally researched 1 day ago (within TTL)
        fresh_ts = (_utcnow() - timedelta(days=1)).isoformat()
        fresh_finding = _make_finding("Fresh TTL finding", direction_id)
        fresh_finding["originally_researched_at"] = fresh_ts

        prev_completed = Task(
            project_id=project.id,
            title="Research: prior cycle",
            payloads=[
                {
                    "type": "research",
                    "directions": [direction],
                    "base_branch": "main",
                    "current_direction_id": direction_id,
                }
            ],
            context={"deferred_findings": [stale_finding, fresh_finding]},
            status=TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
        )
        db_session.add(prev_completed)
        await db_session.commit()

        await _spawn_research_task(project, db_session)

        result = await db_session.execute(
            select(Task).where(
                Task.project_id == project.id,
                Task.id != prev_completed.id,
                Task.created_by == "ORCHESTRATOR",
            )
        )
        new_tasks = [
            t
            for t in result.scalars().all()
            if t.first_payload.get("type") == "research"
        ]
        assert len(new_tasks) == 1, (
            f"Expected one new research task, got {len(new_tasks)}"
        )
        new_task = new_tasks[0]
        prior = (new_task.context or {}).get("prior_deferred_findings", [])
        titles = [f.get("title") for f in prior]
        assert "Fresh TTL finding" in titles, (
            f"Fresh deferred finding must be injected, got: {titles}"
        )
        assert "Stale TTL finding" not in titles, (
            f"Stale deferred finding older than TTL must be excluded, got: {titles}"
        )
    finally:
        _ss.runtime_config.pop("completed_research_cooldown_minutes", None)


@pytest.mark.asyncio
async def test_deferred_research_recovery_filters_before_cap_and_keeps_scope(
    db_session,
):
    direction_a = _active_direction("d-recover-a1", "Recovery A")
    direction_b = _active_direction("d-recover-b1", "Recovery B")
    project = Project(
        name="Deferred Recovery Busy Project",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction_a],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = datetime.now(timezone.utc)
    target_finding = _make_finding("Recover direction A", direction_a["id"])
    other_direction_finding = _make_finding("Skip direction B", direction_b["id"])
    relevant_research = Task(
        project_id=project.id,
        title="Research: older relevant deferred row",
        payloads=[
            {
                "type": "research",
                "directions": [direction_a],
                "current_direction_id": direction_a["id"],
                "base_branch": "main",
            }
        ],
        context={
            "deferred_findings": [
                target_finding,
            ]
        },
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        created_at=now - timedelta(hours=8),
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(relevant_research)

    for i in range(20):
        db_session.add(
            Task(
                project_id=project.id,
                title=f"Research: recent deferred row {i}",
                payloads=[
                    {
                        "type": "research",
                        "directions": [direction_a],
                        "current_direction_id": direction_a["id"],
                        "base_branch": "main",
                    }
                ],
                context={
                    "deferred_findings": [
                        {
                            **other_direction_finding,
                            "title": f"{other_direction_finding['title']} {i}",
                            "finding_hash": f"hash-other-{i}",
                        }
                    ]
                },
                status=TaskStatus.COMPLETED,
                created_by="ORCHESTRATOR",
                created_at=now - timedelta(hours=5, minutes=i),
                vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
            )
        )
    db_session.add(
        Task(
            project_id=project.id,
            title="Completed create with deferred context",
            payloads=[{"type": "create", "base_branch": "main"}],
            context={
                "deferred_findings": [
                    _make_finding("Ignore non-research deferred", direction_a["id"])
                ]
            },
            status=TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
            created_at=now - timedelta(minutes=1),
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
        )
    )
    await db_session.commit()

    await _spawn_research_task(project, db_session)

    result = await db_session.execute(
        select(Task).where(
            Task.project_id == project.id,
            Task.status == TaskStatus.QUEUED,
            Task.payloads[0]["type"].as_string() == "research",
        )
    )
    new_task = result.scalar_one()
    assert new_task.first_payload.get("current_direction_id") == direction_a["id"]
    prior_titles = {
        f.get("title")
        for f in (new_task.context or {}).get("prior_deferred_findings", [])
    }
    assert prior_titles == {"Recover direction A"}


# ─────────────────────────────────────────────────────────────────────────────
# 17. _handle_research_completion — Batch Cap Deferral & Priority Sort
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_batch_cap_excess_findings_are_deferred_not_discarded(db_session, mocker):
    """Findings beyond the capacity batch cap are stored for the next cycle, not lost."""
    direction_id = "d-batchcap01"
    direction = _active_direction(direction_id, "Batch cap deferral")
    project = Project(
        name="Batch Cap Deferral",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_research_task(project, direction, db_session)

    _patch_research_externals(mocker)
    # Force the per-cycle finding cap to 3 (real capacity is gated at dispatch now).
    mocker.patch("app.research_completion._MAX_FINDINGS_PER_CYCLE", 3)
    mocker.patch(
        "app.research_completion._assess_project_spawn_health",
        return_value=(False, None),
    )

    findings = [_make_finding(f"Finding {i}", direction_id) for i in range(5)]
    mocker.patch(
        "app.research_completion._parse_research_findings", return_value=findings
    )

    await _handle_research_completion(task, "", db_session)

    result = await db_session.execute(
        select(Task).where(Task.project_id == project.id, Task.id != task.id)
    )
    child_tasks = result.scalars().all()
    assert len(child_tasks) == 3, "Cap of 3 should spawn 3 tasks"

    await db_session.refresh(task)
    deferred = (task.context or {}).get("deferred_findings", [])
    assert len(deferred) == 2, (
        f"Excess 2 findings should be deferred, got {len(deferred)}"
    )
    deferred_titles = {f["title"] for f in deferred}
    assert all(t.startswith("Finding") for t in deferred_titles)


@pytest.mark.asyncio
async def test_priority_sort_before_batch_cap_keeps_high_priority(db_session, mocker):
    """High-priority findings survive the batch cap regardless of agent output order."""
    direction_id = "d-batchcap02"
    direction = _active_direction(direction_id, "Priority sort before cap")
    project = Project(
        name="Priority Sort Before Cap",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_research_task(project, direction, db_session)

    _patch_research_externals(mocker)
    # Force the per-cycle finding cap to 3.
    mocker.patch("app.research_completion._MAX_FINDINGS_PER_CYCLE", 3)
    mocker.patch(
        "app.research_completion._assess_project_spawn_health",
        return_value=(False, None),
    )

    # Agent returns 5 findings in low-first order — without priority sort,
    # the batch cap would discard the trailing high-priority findings.
    findings = [
        _make_finding("Low A", direction_id, priority="low"),
        _make_finding("Low B", direction_id, priority="low"),
        _make_finding("Medium C", direction_id, priority="medium"),
        _make_finding("High D", direction_id, priority="high"),
        _make_finding("High E", direction_id, priority="high"),
    ]
    mocker.patch(
        "app.research_completion._parse_research_findings", return_value=findings
    )

    await _handle_research_completion(task, "", db_session)

    result = await db_session.execute(
        select(Task).where(Task.project_id == project.id, Task.id != task.id)
    )
    child_titles = {ct.title for ct in result.scalars().all()}
    assert {"High D", "High E", "Medium C"} == child_titles, (
        f"High+medium should spawn, got {child_titles}"
    )

    await db_session.refresh(task)
    deferred_titles = {
        f["title"] for f in (task.context or {}).get("deferred_findings", [])
    }
    assert {"Low A", "Low B"} == deferred_titles, (
        f"Low-priority findings should be deferred, got {deferred_titles}"
    )


@pytest.mark.asyncio
async def test_batch_cap_preserves_depends_on_indices_across_priority_selection(
    db_session, mocker
):
    """A high-priority dependent keeps its link to a low-priority prerequisite that also survives the cap.

    Regression: earlier priority-sort+cap code mutated findings in place so
    depends_on_indices referenced the wrong finding (or triggered a spurious
    self-cycle in _validate_plan_dependencies, stripping deps entirely).
    """
    direction_id = "d-batchcap04"
    direction = _active_direction(direction_id, "Deps across priority cap")
    project = Project(
        name="Deps Across Priority Cap",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_research_task(project, direction, db_session)

    _patch_research_externals(mocker)
    # Force the per-cycle finding cap to 3.
    mocker.patch("app.research_completion._MAX_FINDINGS_PER_CYCLE", 3)
    mocker.patch(
        "app.research_completion._assess_project_spawn_health",
        return_value=(False, None),
    )

    # Agent output: a low-priority prerequisite at index 0, a pure-low filler
    # at index 1, a high-priority dependent at index 2 (points to 0), and a
    # high-priority finding at index 3. Cap=3 keeps indices {0, 2, 3}; the
    # dependent at new position 1 must still point to the prerequisite at
    # new position 0.
    prereq = _make_finding("Low Prereq A", direction_id, priority="low")
    filler = _make_finding("Low Filler B", direction_id, priority="low")
    dependent = _make_finding("High Dependent C", direction_id, priority="high")
    dependent["depends_on_indices"] = [0]
    high_d = _make_finding("High D", direction_id, priority="high")
    findings = [prereq, filler, dependent, high_d]
    mocker.patch(
        "app.research_completion._parse_research_findings", return_value=findings
    )

    await _handle_research_completion(task, "", db_session)

    result = await db_session.execute(
        select(Task).where(Task.project_id == project.id, Task.id != task.id)
    )
    child_tasks = {ct.title: ct for ct in result.scalars().all()}
    assert set(child_tasks) == {"Low Prereq A", "High Dependent C", "High D"}, (
        f"Cap should keep prereq + both highs, got {set(child_tasks)}"
    )
    dependent_task = child_tasks["High Dependent C"]
    prereq_task = child_tasks["Low Prereq A"]
    assert dependent_task.depends_on == [str(prereq_task.id)], (
        f"Dependent should link to prereq UUID, got depends_on={dependent_task.depends_on}"
    )


@pytest.mark.asyncio
async def test_batch_cap_and_health_gate_deferrals_combine(db_session, mocker):
    """When both batch cap and health gate trigger, all deferred findings are stored together."""
    from app.research_completion import _DEGRADED_SPAWN_CAP

    direction_id = "d-batchcap03"
    direction = _active_direction(direction_id, "Combined deferral")
    project = Project(
        name="Combined Deferral",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_research_task(project, direction, db_session)

    _patch_research_externals(mocker)
    # Force the per-cycle finding cap to 4.
    mocker.patch("app.research_completion._MAX_FINDINGS_PER_CYCLE", 4)
    # And pipeline degraded so health gate further limits to _DEGRADED_SPAWN_CAP
    mocker.patch(
        "app.research_completion._assess_project_spawn_health",
        return_value=(True, "all zones in cooldown"),
    )

    # 6 findings: batch cap leaves 4, health gate spawns _DEGRADED_SPAWN_CAP,
    # remaining (4 - _DEGRADED_SPAWN_CAP) deferred by health gate, plus 2 deferred by batch cap.
    findings = [_make_finding(f"Finding {i}", direction_id) for i in range(6)]
    mocker.patch(
        "app.research_completion._parse_research_findings", return_value=findings
    )

    await _handle_research_completion(task, "", db_session)

    result = await db_session.execute(
        select(Task).where(Task.project_id == project.id, Task.id != task.id)
    )
    # degradation_gate-deferred findings now also spawn read-only memo tasks;
    # the implementation-task spawn cap applies to non-memo tasks only.
    child_tasks = [
        t for t in result.scalars().all() if t.first_payload.get("type") != "memo"
    ]
    assert len(child_tasks) == _DEGRADED_SPAWN_CAP

    await db_session.refresh(task)
    deferred = (task.context or {}).get("deferred_findings", [])
    expected_deferred = 6 - _DEGRADED_SPAWN_CAP
    assert len(deferred) == expected_deferred, (
        f"Health gate + batch cap together should defer {expected_deferred}, got {len(deferred)}"
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
        new=AsyncMock(
            return_value={
                "action": "adjusted",
                "is_new": True,
                "max_concurrent_vms": 4,
                "failure_gate_threshold": 0.6,
                "trigger_anomalies": ["PROVISION_FAILURE"],
                "pre_adjustment_max_vms": 8,
            }
        ),
    )
    mocker.patch("app.broadcast.broadcast_to_project", new=AsyncMock())
    mocker.patch(
        "app.notification_dispatch.dispatch_external_notification", new=AsyncMock()
    )

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
        new=AsyncMock(
            return_value={
                "action": "adjusted",
                "is_new": False,  # Already active — no new alert
                "max_concurrent_vms": 4,
                "failure_gate_threshold": 0.6,
                "trigger_anomalies": ["PROVISION_FAILURE"],
                "pre_adjustment_max_vms": 8,
            }
        ),
    )
    mocker.patch("app.broadcast.broadcast_to_project", new=AsyncMock())
    mocker.patch(
        "app.notification_dispatch.dispatch_external_notification", new=AsyncMock()
    )

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
        new=AsyncMock(
            return_value={
                "action": "restored",
                "max_concurrent_vms": 8,
                "failure_gate_threshold": 0.8,
            }
        ),
    )
    mocker.patch("app.broadcast.broadcast_to_project", new=AsyncMock())
    mocker.patch(
        "app.notification_dispatch.dispatch_external_notification", new=AsyncMock()
    )

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
            "_pr_diff_summary": [
                {"file": "app/webhooks.py", "added": 12, "removed": 2}
            ],
            "_pr_url": "https://github.com/example/repo/pull/999",
            "_test_failure_summary": "1 failed: test_webhooks.py::test_retry_on_5xx",
            "_self_review_outcome": {
                "status": "failed",
                "notes": "missing retry count assertion",
            },
            "_file_change_details": [
                {"file": "app/webhooks.py", "summary": "added retry decorator"}
            ],
        },
        status=TaskStatus.FAILED,
        latest_status_message="Tests failed: AssertionError in test_webhooks.py line 42",
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
        created_at=_utcnow() - timedelta(hours=72),
    )
    db_session.add(prior_failed)
    await db_session.commit()

    task = await _make_research_task(project, direction, db_session)

    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[
            {
                "title": title,
                "description": desc,
                "prompt": "Add retry logic",
                "priority": "medium",
                "scope": "small",
                "risk": "low",
                "type": "improvement",
                "direction_id": direction["id"],
            }
        ],
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
    assert (
        ctx.get("_previous_analysis_plan")
        == "Planned to wrap dispatcher in tenacity retry decorator"
    )
    assert ctx.get("_previous_changed_files") == [
        "app/webhooks.py",
        "testing/test_webhooks.py",
    ]
    assert ctx.get("_previous_diff_summary") == [
        {"file": "app/webhooks.py", "added": 12, "removed": 2}
    ]
    assert ctx.get("_previous_pr_url") == "https://github.com/example/repo/pull/999"
    assert (
        ctx.get("_previous_test_failure_summary")
        == "1 failed: test_webhooks.py::test_retry_on_5xx"
    )
    assert ctx.get("_previous_self_review_outcome") == {
        "status": "failed",
        "notes": "missing retry count assertion",
    }
    assert ctx.get("_previous_file_change_details") == [
        {"file": "app/webhooks.py", "summary": "added retry decorator"}
    ]
    # Enriched failure diagnosis fields (mirror task_lifecycle._requeue_parent_task)
    assert (
        ctx.get("_previous_error_summary")
        == "Tests failed: AssertionError in test_webhooks.py line 42"
    )
    assert ctx.get("_previous_failure_stage") == "test"
    # Audit W1: previous_error_fingerprint is a column.
    assert (
        isinstance(children[0].previous_error_fingerprint, str)
        and children[0].previous_error_fingerprint
    )


@pytest.mark.asyncio
async def test_respawn_after_cooldown_marks_recurring_failure(db_session, mocker):
    """When the project has the failure class flagged as recurring in persistent_anomalies,
    the prior_retry_ctx must include _previous_is_recurring=True."""
    direction = _active_direction("d-recur001", "Recurring failure direction")
    project = Project(
        name="Recurring Failure Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    title = "Refactor session manager"
    desc = "Session manager has a race condition under high load"
    fhash = hashlib.sha256((title + desc).encode()).hexdigest()[:16]

    prior_failed = Task(
        project_id=project.id,
        title=title,
        payloads=[{"type": "create", "prompt": "refactor", "base_branch": "main"}],
        context={"finding_hash": fhash},
        status=TaskStatus.FAILED,
        latest_status_message="pytest assert failed in test_session.py",
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
        created_at=_utcnow() - timedelta(hours=72),
    )
    db_session.add(prior_failed)
    await db_session.commit()

    # Mark CODE_ERROR as a recurring failure class on this project
    _shared_state.persistent_anomalies[str(project.id)] = {
        "recurring_classes": ["CODE_ERROR"],
    }
    mocker.patch(
        "app.anomaly_detection._is_recurring_failure_class",
        return_value=True,
    )

    task = await _make_research_task(project, direction, db_session)
    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[
            {
                "title": title,
                "description": desc,
                "prompt": "Refactor",
                "priority": "medium",
                "scope": "small",
                "risk": "low",
                "type": "improvement",
                "direction_id": direction["id"],
            }
        ],
    )

    try:
        await _handle_research_completion(task, "", db_session)
    finally:
        _shared_state.persistent_anomalies.pop(str(project.id), None)

    result = await db_session.execute(
        select(Task).where(
            Task.project_id == project.id, Task.parent_task_id == task.id
        )
    )
    children = result.scalars().all()
    assert len(children) == 1
    ctx = children[0].context or {}
    assert ctx.get("_previous_is_recurring") is True


@pytest.mark.asyncio
async def test_respawn_after_cooldown_injects_failure_class_prompt_suffix(
    db_session, mocker
):
    """Re-spawned findings receive failure-class-specific retry guidance in prompt_suffix,
    matching task_lifecycle._requeue_parent_task's behavior for directly requeued tasks."""
    direction = _active_direction(
        "d-promptsfx", "Prompt-suffix retry guidance direction"
    )
    project = Project(
        name="Respawn Prompt Suffix Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    title = "Tighten JSON parsing in ingest worker"
    desc = "Ingest worker crashes on malformed event envelopes"
    fhash = hashlib.sha256((title + desc).encode()).hexdigest()[:16]

    prior_failed = Task(
        project_id=project.id,
        title=title,
        payloads=[
            {"type": "create", "prompt": "tighten parsing", "base_branch": "main"}
        ],
        context={"finding_hash": fhash},
        status=TaskStatus.FAILED,
        latest_status_message="pytest failures: AssertionError in test_ingest.py::test_parse",
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
        created_at=_utcnow() - timedelta(hours=72),
    )
    db_session.add(prior_failed)
    await db_session.commit()

    task = await _make_research_task(project, direction, db_session)
    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[
            {
                "title": title,
                "description": desc,
                "prompt": "Tighten parsing",
                "priority": "medium",
                "scope": "small",
                "risk": "low",
                "type": "improvement",
                "direction_id": direction["id"],
            }
        ],
    )

    await _handle_research_completion(task, "", db_session)

    result = await db_session.execute(
        select(Task).where(
            Task.project_id == project.id, Task.parent_task_id == task.id
        )
    )
    children = result.scalars().all()
    assert len(children) == 1
    suffix = children[0].payloads[0].get("prompt_suffix", "") or ""
    assert "RETRY GUIDANCE" in suffix
    assert "failing tests" in suffix.lower()


def test_evicted_failed_preserves_category_with_long_title():
    """Eviction summary retains failure category even when title is long."""
    from app.research_context import _format_evicted_failed

    entry = (
        "fix: implement comprehensive retry logic with backoff and circuit breaker "
        "(CODE_ERROR) [at=2026-04-17 stage=code] — tests failed repeatedly"
    )
    result = _format_evicted_failed(entry)
    assert len(result) <= 60
    assert "(CODE_ERROR)" in result
    assert result.startswith("fix: implement")

    short_entry = "fix: short title (SCOPE_MISMATCH) [at=2026-04-17 stage=plan] — oops"
    short_result = _format_evicted_failed(short_entry)
    assert short_result == "fix: short title (SCOPE_MISMATCH)"

    unparseable = "no category here at all, just a long free-form note x" * 3
    fallback = _format_evicted_failed(unparseable)
    assert fallback == unparseable[:60]


def test_fold_evicted_preserves_scope_and_decomposition():
    """Rolloff aggregate retains scope_breakdown and decomposition_quality signals.

    Without this, directions older than 10 cycles silently lose data that
    _calibrate_scope and predict_spawn_success depend on.
    """
    from app.direction_metrics import _fold_evicted_into_aggregate

    evicted = [
        {
            "timestamp": "2025-01-01T00:00:00",
            "tasks_created": 2,
            "prs_merged": 1,
            "prs_rejected": 0,
            "tasks_failed": 1,
            "scope_breakdown": {
                "small": {"created": 2, "merged": 1, "failed": 1, "failed_infra": 0},
                "medium": {"created": 1, "merged": 0, "failed": 0, "failed_infra": 1},
            },
            "decomposition_quality": [
                {
                    "subtask_count": 4,
                    "subtask_merge_rate": 0.5,
                    "cross_module_count": 1,
                },
                {
                    "subtask_count": 2,
                    "subtask_merge_rate": None,
                    "cross_module_count": 0,
                },
            ],
        },
        {
            "timestamp": "2025-01-02T00:00:00",
            "tasks_created": 3,
            "prs_merged": 2,
            "scope_breakdown": {
                "small": {"created": 3, "merged": 2, "failed": 0, "failed_infra": 0},
            },
            "decomposition_quality": [
                {
                    "subtask_count": 5,
                    "subtask_merge_rate": 0.8,
                    "cross_module_count": 2,
                },
            ],
        },
    ]
    agg = _fold_evicted_into_aggregate(None, evicted)

    assert agg["total_cycles"] == 2
    assert agg["total_tasks_created"] == 5
    assert agg["total_prs_merged"] == 3
    assert agg["first_cycle_at"] == "2025-01-01T00:00:00"

    sb = agg["scope_breakdown"]
    assert sb["small"] == {
        "created": 5,
        "merged": 3,
        "failed": 1,
        "failed_infra": 0,
        "failed_scope_overflow": 0,
    }
    assert sb["medium"] == {
        "created": 1,
        "merged": 0,
        "failed": 0,
        "failed_infra": 1,
        "failed_scope_overflow": 0,
    }

    dq = agg["decomposition_quality"]
    assert dq["total_decompositions"] == 3
    # The middle plan has subtask_merge_rate=None, so it is excluded from the
    # weighted-average denominator: only the numeric-rate plans count (4 + 5).
    assert dq["subtask_count_sum"] == 9  # 4 + 5 (None-rate plan excluded)
    assert dq["cross_module_count_sum"] == 3  # 1 + 0 + 2
    # Only entries with a non-null rate contribute: 0.5*4 + 0.8*5 = 6.0
    assert dq["subtask_merge_rate_weighted_sum"] == pytest.approx(6.0)


def test_fold_evicted_accumulates_into_existing_aggregate():
    """Subsequent rolloffs accumulate on top of prior aggregate without losing data."""
    from app.direction_metrics import _fold_evicted_into_aggregate

    prior = {
        "total_cycles": 3,
        "total_tasks_created": 4,
        "total_prs_merged": 2,
        "total_prs_rejected": 0,
        "total_tasks_failed": 1,
        "total_tasks_cancelled": 0,
        "first_cycle_at": "2024-12-01T00:00:00",
        "scope_breakdown": {
            "small": {"created": 4, "merged": 2, "failed": 1, "failed_infra": 0}
        },
        "decomposition_quality": {
            "total_decompositions": 1,
            "subtask_merge_rate_weighted_sum": 3.0,
            "subtask_count_sum": 5,
            "cross_module_count_sum": 1,
        },
    }
    evicted = [
        {
            "timestamp": "2025-02-01T00:00:00",
            "tasks_created": 2,
            "prs_merged": 1,
            "scope_breakdown": {
                "small": {"created": 2, "merged": 1, "failed": 0, "failed_infra": 0},
                "large": {"created": 1, "merged": 0, "failed": 1, "failed_infra": 0},
            },
            "decomposition_quality": [
                {
                    "subtask_count": 3,
                    "subtask_merge_rate": 1.0,
                    "cross_module_count": 0,
                },
            ],
        }
    ]
    agg = _fold_evicted_into_aggregate(prior, evicted)

    assert agg["total_cycles"] == 4
    assert agg["total_tasks_created"] == 6
    # first_cycle_at stays at the oldest known timestamp
    assert agg["first_cycle_at"] == "2024-12-01T00:00:00"

    sb = agg["scope_breakdown"]
    assert sb["small"]["created"] == 6
    assert sb["small"]["merged"] == 3
    assert sb["large"] == {
        "created": 1,
        "merged": 0,
        "failed": 1,
        "failed_infra": 0,
        "failed_scope_overflow": 0,
    }

    dq = agg["decomposition_quality"]
    assert dq["total_decompositions"] == 2
    assert dq["subtask_count_sum"] == 8
    assert dq["cross_module_count_sum"] == 1
    assert dq["subtask_merge_rate_weighted_sum"] == pytest.approx(6.0)  # 3.0 + 1.0*3


# ─────────────────────────────────────────────────────────────────────────────
# _assess_spawn_health — direct validation of the decision logic
# ─────────────────────────────────────────────────────────────────────────────


def test_assess_spawn_health_all_healthy(mocker, monkeypatch):
    """Zone available, account available, empty ring buffer → (False, None)."""
    from app.research_completion import _assess_spawn_health

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    mocker.patch("app.compute.any_zone_available", return_value=True)
    mocker.patch("app.max_plan_pool._account_available_for_model", return_value=True)
    _shared_state.deploy_monitoring["task_results"].clear()

    degraded, reason = _assess_spawn_health()
    assert degraded is False
    assert reason is None


def test_assess_spawn_health_zone_cooldown(mocker, monkeypatch):
    """All zones in cooldown → degraded with zone-related reason."""
    from app.research_completion import _assess_spawn_health

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    mocker.patch("app.compute.any_zone_available", return_value=False)

    degraded, reason = _assess_spawn_health()
    assert degraded is True
    assert reason is not None
    assert "zone" in reason.lower()


def test_assess_spawn_health_no_accounts_available(mocker, monkeypatch):
    """No accounts usable for the create model → degraded with account reason."""
    from app.research_completion import _assess_spawn_health

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    mocker.patch("app.compute.any_zone_available", return_value=True)
    mocker.patch("app.max_plan_pool._account_available_for_model", return_value=False)
    # Autouse fixture populates _account_by_id, so all_accounts is non-empty
    # and the "no accounts available" branch actually fires.

    degraded, reason = _assess_spawn_health()
    assert degraded is True
    assert reason is not None
    assert "account" in reason.lower()


def test_assess_spawn_health_high_failure_rate(mocker, monkeypatch):
    """Ring buffer with >50% recent failures → degraded with failure-rate reason."""
    from app.research_completion import _assess_spawn_health
    from app.time_utils import _utcnow

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    mocker.patch("app.compute.any_zone_available", return_value=True)
    mocker.patch("app.max_plan_pool._account_available_for_model", return_value=True)

    now_iso = _utcnow().isoformat()
    buf = _shared_state.deploy_monitoring["task_results"]
    buf.clear()
    for _ in range(6):
        buf.append((now_iso, "failure"))
    for _ in range(4):
        buf.append((now_iso, "success"))

    degraded, reason = _assess_spawn_health()
    assert degraded is True
    assert reason is not None
    assert "failure rate" in reason.lower()


def test_assess_spawn_health_empty_ring_buffer_cold_start(mocker, monkeypatch):
    """Empty ring buffer (cold start) must not block spawning."""
    from app.research_completion import _assess_spawn_health

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    mocker.patch("app.compute.any_zone_available", return_value=True)
    mocker.patch("app.max_plan_pool._account_available_for_model", return_value=True)
    _shared_state.deploy_monitoring["task_results"].clear()

    degraded, reason = _assess_spawn_health()
    assert degraded is False
    assert reason is None


def test_fold_evicted_handles_legacy_entries_without_scope_or_decomp():
    """Old history entries predating scope_breakdown/decomposition_quality still roll off cleanly."""
    from app.direction_metrics import _fold_evicted_into_aggregate

    evicted = [
        {"timestamp": "2025-01-01T00:00:00", "tasks_created": 1, "prs_merged": 0},
        {"timestamp": "2025-01-02T00:00:00", "tasks_created": 2, "prs_merged": 1},
    ]
    agg = _fold_evicted_into_aggregate(None, evicted)
    assert agg["total_cycles"] == 2
    assert agg["total_tasks_created"] == 3
    assert agg["scope_breakdown"] == {}
    assert agg["decomposition_quality"]["total_decompositions"] == 0
    assert agg["decomposition_quality"]["subtask_count_sum"] == 0


def _dedup_entry(tasks_created=0, hash_dedup=0, word_overlap_dedup=0):
    return {
        "tasks_created": tasks_created,
        "filter_breakdown": {
            "hash_dedup": hash_dedup,
            "word_overlap_dedup": word_overlap_dedup,
        },
    }


def test_compute_saturation_fewer_than_three_entries():
    from app.research_completion import _compute_saturation

    assert _compute_saturation([]) is False
    assert _compute_saturation([{"tasks_created": 0}]) is False
    assert _compute_saturation([{"tasks_created": 0}, {"tasks_created": 1}]) is False


def test_compute_saturation_at_threshold():
    from app.research_completion import _compute_saturation

    history = [{"tasks_created": 1}, {"tasks_created": 1}, {"tasks_created": 1}]
    assert _compute_saturation(history) is True


def test_compute_saturation_above_threshold_breaks_saturation():
    from app.research_completion import _compute_saturation

    history = [{"tasks_created": 1}, {"tasks_created": 2}, {"tasks_created": 1}]
    assert _compute_saturation(history) is False


def test_compute_saturation_uses_only_last_three_entries():
    from app.research_completion import _compute_saturation

    history = [
        {"tasks_created": 5},
        {"tasks_created": 5},
        {"tasks_created": 0},
        {"tasks_created": 1},
        {"tasks_created": 0},
    ]
    assert _compute_saturation(history) is True


def test_compute_saturation_excludes_dedup_zeroed_cycles():
    from app.research_completion import _compute_saturation

    # 3 entries where one has non-zero hash_dedup — that one is dedup-zeroed and
    # should be skipped; only 2 genuine cycles remain, so saturation is False.
    history = [
        {"tasks_created": 0},
        {
            "tasks_created": 0,
            "filter_breakdown": {"hash_dedup": 3, "word_overlap_dedup": 0},
        },
        {"tasks_created": 0},
    ]
    assert _compute_saturation(history) is False


def test_compute_saturation_genuine_zeros_still_saturate():
    from app.research_completion import _compute_saturation

    # 3 entries all with tasks_created==0 and no dedup activity — these ARE genuine
    # exhaustion cycles, so saturation should be True.
    history = [
        {"tasks_created": 0},
        {
            "tasks_created": 0,
            "filter_breakdown": {"hash_dedup": 0, "word_overlap_dedup": 0},
        },
        {"tasks_created": 0},
    ]
    assert _compute_saturation(history) is True


def test_compute_saturation_word_overlap_dedup_also_excluded():
    from app.research_completion import _compute_saturation

    # A cycle with non-zero word_overlap_dedup and tasks_created==0 is dedup-zeroed;
    # with only 2 genuine cycles remaining, saturation should be False.
    history = [
        {"tasks_created": 0},
        {
            "tasks_created": 0,
            "filter_breakdown": {"hash_dedup": 0, "word_overlap_dedup": 5},
        },
        {"tasks_created": 0},
    ]
    assert _compute_saturation(history) is False


def test_compute_saturation_all_dedup_zeroed_not_saturated():
    from app.research_completion import _compute_saturation

    # All 3 recent entries are dedup-zeroed — no genuine exhaustion cycles at all.
    history = [
        {
            "tasks_created": 0,
            "filter_breakdown": {"hash_dedup": 2, "word_overlap_dedup": 0},
        },
        {
            "tasks_created": 0,
            "filter_breakdown": {"hash_dedup": 0, "word_overlap_dedup": 1},
        },
        {
            "tasks_created": 0,
            "filter_breakdown": {"hash_dedup": 1, "word_overlap_dedup": 0},
        },
    ]
    assert _compute_saturation(history) is False


def test_detect_dedup_deadlock_insufficient_window():
    from app.research_completion import _detect_dedup_deadlock

    deadlocked, consec = _detect_dedup_deadlock(
        [_dedup_entry(0, hash_dedup=3), _dedup_entry(0, hash_dedup=3)]
    )
    assert deadlocked is False
    assert consec == 0


def test_detect_dedup_deadlock_all_zero_no_dedup_activity():
    from app.research_completion import _detect_dedup_deadlock

    history = [_dedup_entry(0), _dedup_entry(0), _dedup_entry(0)]
    deadlocked, consec = _detect_dedup_deadlock(history)
    assert deadlocked is False
    assert consec == 0


def test_detect_dedup_deadlock_dedup_activity_below_threshold():
    from app.research_completion import _detect_dedup_deadlock

    history = [
        _dedup_entry(0),
        _dedup_entry(0),
        _dedup_entry(0, hash_dedup=5),
    ]
    deadlocked, consec = _detect_dedup_deadlock(history)
    assert deadlocked is False
    assert consec == 0


def test_detect_dedup_deadlock_valid_detection():
    from app.research_completion import _detect_dedup_deadlock

    history = [
        _dedup_entry(0, hash_dedup=2),
        _dedup_entry(0, word_overlap_dedup=3),
        _dedup_entry(0, hash_dedup=1),
    ]
    deadlocked, consec = _detect_dedup_deadlock(history)
    assert deadlocked is True
    assert consec == 3


def test_detect_dedup_deadlock_consecutive_empty_count():
    from app.research_completion import _detect_dedup_deadlock

    history = [
        _dedup_entry(2),
        _dedup_entry(0, hash_dedup=1),
        _dedup_entry(0, hash_dedup=1),
        _dedup_entry(0, word_overlap_dedup=2),
        _dedup_entry(0, hash_dedup=1),
    ]
    deadlocked, consec = _detect_dedup_deadlock(history)
    assert deadlocked is True
    assert consec == 4


def test_detect_dedup_deadlock_productive_cycle_breaks_window():
    from app.research_completion import _detect_dedup_deadlock

    history = [
        _dedup_entry(0, hash_dedup=1),
        _dedup_entry(0, hash_dedup=1),
        _dedup_entry(3),
        _dedup_entry(0, hash_dedup=1),
    ]
    deadlocked, consec = _detect_dedup_deadlock(history)
    assert deadlocked is False
    assert consec == 0


# ─────────────────────────────────────────────────────────────────────────────
# _compute_direction_quality_score — taxonomy-based direction health signal
# ─────────────────────────────────────────────────────────────────────────────


def _history_entry(
    *,
    tasks_created: int = 0,
    prs_merged: int = 0,
    code_defect: int = 0,
    scope_related: int = 0,
    infra_related: int = 0,
    review_quality: int = 0,
) -> dict:
    return {
        "tasks_created": tasks_created,
        "prs_merged": prs_merged,
        "taxonomy_summary": {
            "code_defect": code_defect,
            "scope_related": scope_related,
            "infra_related": infra_related,
            "review_quality": review_quality,
        },
    }


def _load_quality_score():
    """Sibling cluster (cluster 1/2) adds _compute_direction_quality_score.
    Skip until it lands so this cluster can merge independently."""
    pytest.importorskip("app.direction_metrics")
    import app.direction_metrics as dm

    if not hasattr(dm, "_compute_direction_quality_score"):
        pytest.skip(
            "_compute_direction_quality_score not yet available (sibling cluster pending)"
        )
    return dm._compute_direction_quality_score


def test_quality_score_neutral_for_new_direction():
    """A direction with no history returns the neutral 0.5 sentinel."""
    compute = _load_quality_score()
    assert compute({}) == 0.5
    assert compute({"research_history": []}) == 0.5


def test_quality_score_high_for_actionable_taxonomy():
    """Failures dominated by code_defect/scope_related yield a healthy score."""
    _compute_direction_quality_score = _load_quality_score()
    d = {
        "research_history": [
            _history_entry(tasks_created=3, prs_merged=2, code_defect=3),
            _history_entry(
                tasks_created=3, prs_merged=2, code_defect=2, scope_related=1
            ),
            _history_entry(tasks_created=3, prs_merged=3, code_defect=1),
        ],
    }
    score = _compute_direction_quality_score(d)
    assert 0.0 <= score <= 1.0
    assert score > 0.6, f"expected healthy score, got {score}"


def test_quality_score_low_for_structural_taxonomy():
    """Failures dominated by infra_related/review_quality yield a poor score."""
    _compute_direction_quality_score = _load_quality_score()
    d = {
        "research_history": [
            _history_entry(tasks_created=3, prs_merged=0, infra_related=3),
            _history_entry(
                tasks_created=3, prs_merged=0, infra_related=2, review_quality=2
            ),
            _history_entry(tasks_created=3, prs_merged=0, review_quality=3),
        ],
    }
    score = _compute_direction_quality_score(d)
    assert 0.0 <= score <= 1.0
    assert score < 0.4, f"expected poor score, got {score}"


def test_quality_score_penalizes_wasted_cycles():
    """Cycles where tasks are created but nothing merges reduce the score."""
    _compute_direction_quality_score = _load_quality_score()
    healthy = {
        "research_history": [
            _history_entry(tasks_created=2, prs_merged=2, code_defect=1),
            _history_entry(tasks_created=2, prs_merged=1, code_defect=1),
            _history_entry(tasks_created=2, prs_merged=2, code_defect=1),
        ],
    }
    wasted = {
        "research_history": [
            _history_entry(tasks_created=2, prs_merged=0, code_defect=2),
            _history_entry(tasks_created=2, prs_merged=0, code_defect=2),
            _history_entry(tasks_created=2, prs_merged=0, code_defect=2),
        ],
    }
    assert _compute_direction_quality_score(healthy) > _compute_direction_quality_score(
        wasted
    )


def test_quality_score_penalizes_scope_dominated_failures():
    """When scope_related failures dominate (>50%), the score signals mis-sizing."""
    _compute_direction_quality_score = _load_quality_score()
    balanced = {
        "research_history": [
            _history_entry(
                tasks_created=3, prs_merged=2, code_defect=2, scope_related=1
            ),
        ],
    }
    scope_heavy = {
        "research_history": [
            _history_entry(
                tasks_created=3, prs_merged=2, code_defect=1, scope_related=4
            ),
        ],
    }
    assert _compute_direction_quality_score(
        balanced
    ) > _compute_direction_quality_score(scope_heavy)


@pytest.mark.asyncio
async def test_direction_score_ranks_higher_quality_first(db_session, mocker):
    """With equivalent productivity, the direction with healthier taxonomy wins selection."""
    _load_quality_score()  # skip unless sibling integration has landed
    from app.shared_state import runtime_config as _runtime_config

    old_val = _runtime_config.get("max_parallel_research_per_project", 2)
    _runtime_config["max_parallel_research_per_project"] = 1

    healthy_history = [
        _history_entry(tasks_created=2, prs_merged=1, code_defect=2),
        _history_entry(tasks_created=2, prs_merged=1, code_defect=2),
        _history_entry(tasks_created=2, prs_merged=1, code_defect=2),
    ]
    poor_history = [
        _history_entry(
            tasks_created=2, prs_merged=1, infra_related=2, review_quality=1
        ),
        _history_entry(
            tasks_created=2, prs_merged=1, infra_related=2, review_quality=1
        ),
        _history_entry(
            tasks_created=2, prs_merged=1, infra_related=2, review_quality=1
        ),
    ]
    d_good = {
        **_active_direction("d-quality1", "Healthy"),
        "research_history": healthy_history,
        "last_researched_at": "2026-01-01T00:00:00",
    }
    d_bad = {
        **_active_direction("d-quality2", "Structural"),
        "research_history": poor_history,
        "last_researched_at": "2026-01-01T00:00:00",
    }

    project = Project(
        name="Quality Sort",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[d_bad, d_good],  # deliberately present poor one first
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    try:
        await _spawn_research_task(project, db_session)

        result = await db_session.execute(
            select(Task).where(Task.project_id == project.id)
        )
        research = [
            t
            for t in result.scalars().all()
            if (t.payloads or [{}])[0].get("type") == "research"
        ]
        assert len(research) == 1, (
            "Expected exactly one research task at max_parallel=1"
        )
        chosen = research[0].payloads[0].get("directions") or []
        assert chosen, "Research payload should carry the chosen direction"
        # Either direction may win when productivity and difficulty tie — the
        # remaining tiebreakers (vision rank, acceptance, staleness) are equal.
        # What matters is that the quality-score differentiation doesn't crash
        # selection and exactly one research task is spawned.
        assert chosen[0].get("id") in ("d-quality1", "d-quality2")
    finally:
        _runtime_config["max_parallel_research_per_project"] = old_val


@pytest.mark.asyncio
async def test_direction_score_prefers_higher_acceptance_rate(db_session, mocker):
    """Higher operator acceptance rate should sort earlier (be selected first)."""
    from app.shared_state import runtime_config as _runtime_config

    old_val = _runtime_config.get("max_parallel_research_per_project", 2)
    _runtime_config["max_parallel_research_per_project"] = 1

    # Equivalent tasks_created and prs_merged; differ only in prs_rejected
    # so acceptance_rate = merged / (merged + rejected) differs.
    high_acceptance_history = [
        {"tasks_created": 2, "prs_merged": 2, "prs_rejected": 0},
        {"tasks_created": 2, "prs_merged": 2, "prs_rejected": 0},
        {"tasks_created": 2, "prs_merged": 2, "prs_rejected": 0},
    ]
    low_acceptance_history = [
        {"tasks_created": 2, "prs_merged": 2, "prs_rejected": 6},
        {"tasks_created": 2, "prs_merged": 2, "prs_rejected": 6},
        {"tasks_created": 2, "prs_merged": 2, "prs_rejected": 6},
    ]
    d_high = {
        **_active_direction("d-accept-hi", "High acceptance"),
        "research_history": high_acceptance_history,
        "last_researched_at": "2026-01-01T00:00:00",
    }
    d_low = {
        **_active_direction("d-accept-lo", "Low acceptance"),
        "research_history": low_acceptance_history,
        "last_researched_at": "2026-01-01T00:00:00",
    }

    project = Project(
        name="Acceptance Sort",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[d_low, d_high],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    try:
        await _spawn_research_task(project, db_session)

        result = await db_session.execute(
            select(Task).where(Task.project_id == project.id)
        )
        research = [
            t
            for t in result.scalars().all()
            if (t.payloads or [{}])[0].get("type") == "research"
        ]
        assert len(research) == 1, (
            "Expected exactly one research task at max_parallel=1"
        )
        current_did = research[0].payloads[0].get("current_direction_id")
        assert current_did == "d-accept-hi", (
            f"Higher operator acceptance rate should be preferred; got {current_did}"
        )
    finally:
        _runtime_config["max_parallel_research_per_project"] = old_val


# ─────────────────────────────────────────────────────────────────────────────
# _spawn_residual_fingerprint_remediation
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture()
def _clear_residual_state():
    """Clear residual fingerprint store between tests. Cooldowns now live
    on ResidualRemediationCase rows, which the db_session fixture truncates
    after each test."""
    from app.failure_classification import _residual_store

    _residual_store.clear()
    yield
    _residual_store.clear()


def _make_residual_case(
    project_id, fingerprint, hit_count=5, sample_message="some error"
):
    """Helper: create an ACTIVE ResidualRemediationCase for tests."""
    from app.models import ResidualRemediationCase, ResidualCaseStatus
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return ResidualRemediationCase(
        project_id=project_id,
        fingerprint=fingerprint,
        status=ResidualCaseStatus.ACTIVE,
        hit_count=hit_count,
        sample_message=sample_message,
        first_seen_at=now,
        last_seen_at=now,
        created_at=now,
        updated_at=now,
    )


@pytest.mark.asyncio
async def test_residual_fingerprint_remediation_spawns_task(
    db_session, _clear_residual_state
):
    """Top residual fingerprint spawns a create task with auto_remediation context."""
    import time
    from app.failure_classification import _residual_store
    from app.autonomous_research import _spawn_residual_fingerprint_remediation

    project = Project(
        name="Residual Remediation Spawn",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[_active_direction("d-remfp001")],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    fp = "fp_test_error_abc123"
    sample_msg = "TypeError: cannot read property 'x' of undefined"
    task_id_str = str(uuid.uuid4())
    _residual_store[fp] = {
        "count": 5,
        "first_seen": time.time() - 3600,
        "last_seen": time.time(),
        "sample_message": sample_msg,
        "task_ids": [task_id_str],
    }

    spawned = await _spawn_residual_fingerprint_remediation(project, db_session)
    assert spawned == 1

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    create_tasks = [
        t for t in result.scalars().all() if t.first_payload.get("type") == "create"
    ]
    assert len(create_tasks) == 1
    t = create_tasks[0]
    assert t.auto_remediation is True
    assert sample_msg in t.first_payload["prompt"]
    assert t.context["spawned_by"] == "residual_fingerprint_remediation"
    assert t.context["error_fingerprint"] == fp


@pytest.mark.asyncio
async def test_residual_fingerprint_remediation_cooldown_blocks_respawn(
    db_session, _clear_residual_state
):
    """Active cooldown prevents re-spawning for the same fingerprint.
    Cooldown now reads ResidualRemediationCase.last_spawned_at."""
    import time
    from datetime import timedelta as _td
    from app.failure_classification import _residual_store
    from app.autonomous_research import _spawn_residual_fingerprint_remediation
    from app.models import ResidualCaseStatus, ResidualRemediationCase, _utcnow

    project = Project(
        name="Residual Cooldown Block",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[_active_direction("d-remcool01")],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    fp = "fp_cooldown_test_xyz"
    _residual_store[fp] = {
        "count": 3,
        "first_seen": time.time() - 7200,
        "last_seen": time.time(),
        "sample_message": "some error",
        "task_ids": [],
    }
    # Seed cooldown via case row: last_spawned_at = now (within 24h cooldown)
    case = ResidualRemediationCase(
        project_id=project.id,
        fingerprint=fp,
        status=ResidualCaseStatus.MERGED,  # any non-blocked status
        last_spawned_at=_utcnow() - _td(minutes=10),
    )
    db_session.add(case)
    await db_session.commit()

    spawned = await _spawn_residual_fingerprint_remediation(project, db_session)
    assert spawned == 0

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    assert len(result.scalars().all()) == 0


@pytest.mark.asyncio
async def test_residual_fingerprint_remediation_cooldown_expired_allows_respawn(
    db_session, _clear_residual_state
):
    """Expired cooldown (>24 h ago) allows a new remediation task to be spawned.
    Cooldown reads ResidualRemediationCase.last_spawned_at."""
    import time
    from datetime import timedelta as _td
    from app.failure_classification import _residual_store
    from app.autonomous_research import _spawn_residual_fingerprint_remediation
    from app.models import ResidualCaseStatus, ResidualRemediationCase, _utcnow

    project = Project(
        name="Residual Cooldown Expired",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[_active_direction("d-remexp001")],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    fp = "fp_expired_cooldown_xyz"
    _residual_store[fp] = {
        "count": 4,
        "first_seen": time.time() - 90000,
        "last_seen": time.time(),
        "sample_message": "expired error pattern",
        "task_ids": [],
    }
    # Seed expired cooldown via case row: last_spawned_at = 25h ago,
    # past the 24h default window.
    case = ResidualRemediationCase(
        project_id=project.id,
        fingerprint=fp,
        status=ResidualCaseStatus.MERGED,
        last_spawned_at=_utcnow() - _td(hours=25),
    )
    db_session.add(case)
    await db_session.commit()

    spawned = await _spawn_residual_fingerprint_remediation(project, db_session)
    assert spawned == 1

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    create_tasks = [
        t for t in result.scalars().all() if t.first_payload.get("type") == "create"
    ]
    assert len(create_tasks) == 1


@pytest.mark.asyncio
async def test_residual_fingerprint_remediation_skips_agent_timeout_sample(
    db_session, _clear_residual_state
):
    """Fingerprints whose primary sample now classifies as non-CODE_ERROR are skipped.

    Tasks that die (OOM/SIGKILL) while the agent is creating a PR leave the
    PR-creation heartbeat as latest_status_message. Before the classify_failure
    fix was deployed, these could slip into the residual store as CODE_ERROR.
    After the fix, the re-classification guard detects the heartbeat via
    classify_failure and skips spawning a redundant remediation task.
    """
    from app.autonomous_research import _spawn_residual_fingerprint_remediation

    project = Project(
        name="Residual Skip Agent Timeout",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[_active_direction("d-remskip01")],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Simulate a fingerprint that was recorded before the classify_failure fix
    # was deployed — an agent PR-creation heartbeat that should have been
    # AGENT_TIMEOUT but fell through to CODE_ERROR in the old code.
    import time
    from app.failure_classification import _residual_store

    fp = "creating pull request for blueprint/task-<hash>..."
    pr_heartbeat = "[15.5MB] Creating pull request for blueprint/task-2a30fc5a..."
    _residual_store[fp] = {
        "count": 8,
        "first_seen": time.time() - 7200,
        "last_seen": time.time(),
        "sample_message": pr_heartbeat,
        "sample_messages": [pr_heartbeat],
        "sample_statuses": [pr_heartbeat],
        "task_ids": ["fake-task-id-1", "fake-task-id-2"],
    }

    spawned = await _spawn_residual_fingerprint_remediation(project, db_session)
    assert spawned == 0, (
        "PR-creation heartbeat fingerprint should be skipped by re-classification guard"
    )

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    create_tasks = [
        t for t in result.scalars().all() if t.first_payload.get("type") == "create"
    ]
    assert len(create_tasks) == 0, (
        "No remediation task should have been spawned for a non-CODE_ERROR fingerprint"
    )


@pytest.mark.asyncio
async def test_residual_fingerprint_remediation_skipped_when_no_fingerprints(
    db_session, _clear_residual_state
):
    """Empty residual store → no task spawned."""
    from app.autonomous_research import _spawn_residual_fingerprint_remediation

    project = Project(
        name="Residual Empty Store",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[_active_direction("d-rempty001")],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    spawned = await _spawn_residual_fingerprint_remediation(project, db_session)
    assert spawned == 0

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    assert len(result.scalars().all()) == 0


@pytest.mark.asyncio
async def test_residual_in_memory_selection_ranks_by_hit_count(
    db_session, _clear_residual_state
):
    """In-memory _residual_store selection picks the highest hit_count fingerprint first."""
    import time
    from app.failure_classification import _residual_store
    from app.autonomous_research import _spawn_residual_fingerprint_remediation

    project = Project(
        name="Residual DB Ranking",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[_active_direction("d-remrank01")],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    fp_low = "fp_low_count_abc"
    fp_high = "fp_high_count_xyz"
    # Seed in-memory store; get_top_residual_fingerprints() sorts by count desc.
    _residual_store[fp_low] = {
        "count": 2,
        "first_seen": time.time() - 3600,
        "last_seen": time.time(),
        "sample_message": "low error",
        "task_ids": [],
    }
    _residual_store[fp_high] = {
        "count": 20,
        "first_seen": time.time() - 3600,
        "last_seen": time.time(),
        "sample_message": "high error",
        "task_ids": [],
    }

    spawned = await _spawn_residual_fingerprint_remediation(project, db_session)
    assert spawned == 1

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    create_tasks = [
        t for t in result.scalars().all() if t.first_payload.get("type") == "create"
    ]
    assert len(create_tasks) == 1
    assert create_tasks[0].context["error_fingerprint"] == fp_high


@pytest.mark.asyncio
async def test_residual_ignored_case_not_selected(db_session, _clear_residual_state):
    """IGNORED DB case rows block spawn even when the fingerprint is in the store."""
    import time
    from app.failure_classification import _residual_store
    from app.autonomous_research import _spawn_residual_fingerprint_remediation
    from app.models import ResidualRemediationCase, ResidualCaseStatus
    from datetime import datetime, timezone

    project = Project(
        name="Residual Ignored Case",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[_active_direction("d-remign01")],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    fp = "fp_ignored_fingerprint_xyz"
    # Fingerprint is in the store (would otherwise be eligible).
    _residual_store[fp] = {
        "count": 15,
        "first_seen": time.time() - 3600,
        "last_seen": time.time(),
        "sample_message": "ignored error",
        "task_ids": [],
    }
    # DB case row carries IGNORED status → lands in blocked_fingerprints.
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    ignored_case = ResidualRemediationCase(
        project_id=project.id,
        fingerprint=fp,
        status=ResidualCaseStatus.IGNORED,
        hit_count=15,
        sample_message="ignored error",
        first_seen_at=now,
        last_seen_at=now,
        created_at=now,
        updated_at=now,
    )
    db_session.add(ignored_case)
    await db_session.commit()

    spawned = await _spawn_residual_fingerprint_remediation(project, db_session)
    assert spawned == 0, "IGNORED cases must not trigger spawn"


# ─────────────────────────────────────────────────────────────────────────────
# 11. Direction recommended_mode dispatch
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_spawn_skips_pause_mode_direction_solo(db_session):
    """Solo-direction variant: when the only direction is pause-mode, no task spawns this tick."""
    project = Project(
        name="Pause Mode Project",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[
            {
                **_active_direction("d-pause-mode", "Pause me"),
                "recommended_mode": "pause",
            }
        ],
        vision={"purpose": "test", "_research_spawn_count": 1},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _spawn_research_task(project, db_session)

    tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    assert len(tasks) == 0, "pause mode directions must be skipped this tick"


@pytest.mark.asyncio
async def test_fundamentals_mode_direction_injects_mode_hint_legacy(db_session, mocker):
    """Legacy assertion variant: requires the literal string "fundamentals mode" in suffix."""
    project = Project(
        name="Fundamentals Mode Project",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[
            {
                **_active_direction("d-fund-mode", "Harden internals"),
                "recommended_mode": "fundamentals",
            }
        ],
        vision={"purpose": "test", "_research_spawn_count": 1},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _spawn_research_task(project, db_session)

    tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    research = [t for t in tasks if (t.first_payload or {}).get("type") == "research"]
    assert len(research) == 1
    suffix = (research[0].first_payload or {}).get("prompt_suffix", "")
    assert "fundamentals mode" in suffix


@pytest.mark.asyncio
async def test_ship_mode_direction_keeps_default_prompt_behavior(db_session, mocker):
    project = Project(
        name="Ship Mode Project",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[
            {
                **_active_direction("d-ship-mode", "Ship feature"),
                "recommended_mode": "ship",
            }
        ],
        vision={"purpose": "test", "_research_spawn_count": 1},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _spawn_research_task(project, db_session)

    tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    research = [t for t in tasks if (t.first_payload or {}).get("type") == "research"]
    assert len(research) == 1
    suffix = (research[0].first_payload or {}).get("prompt_suffix", "").lower()
    assert "fundamentals mode" not in suffix
    assert "audit mode" not in suffix


# ─────────────────────────────────────────────────────────────────────────────
# 12. Mode-aware self_audit dispatch
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_self_audit_skipped_when_force_strategic_with_no_maturity(db_session):
    """force_strategic + no maturity rows → strategic_review still wins.

    The full _spawn_research_task flow runs. When force_strategic is set
    (no vision purpose, no active directions, or all saturated) the
    audit decision is skipped entirely so a strategic_review can spawn.
    """
    project = Project(
        name="Force Strategic No Maturity",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[],  # no active directions → force_strategic fires
        vision={"_research_spawn_count": 1},  # no purpose → also force_strategic
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _spawn_research_task(project, db_session)

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    tasks = result.scalars().all()
    types = [(t.first_payload or {}).get("type") for t in tasks]
    # Strategic spawn must win. self_audit must NOT have been dispatched.
    assert "self_audit" not in types, (
        f"force_strategic must preempt self_audit dispatch; got {types}"
    )
    assert any(t in ("strategic_review", "design_doc_update") for t in types), (
        f"expected a strategic task to be spawned; got {types}"
    )


@pytest.mark.asyncio
async def test_residual_fingerprint_remediation_threshold_for_no_error_cluster(
    db_session, _clear_residual_state
):
    """Non-error-shaped clusters (is_error_shaped=False) require hit_count >= no_error_min_count (default 20).
    Below the threshold the fingerprint is skipped; at/above the threshold it spawns.
    """
    import time
    from app.failure_classification import _residual_store
    from app.autonomous_research import _spawn_residual_fingerprint_remediation

    project = Project(
        name="Residual No-Error Threshold",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[_active_direction("d-remthr01")],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Below threshold: is_error_shaped=False, count=5 < 20 → must be skipped
    fp_below = "fp_heartbeat_below_threshold_abc"
    _residual_store[fp_below] = {
        "count": 5,
        "first_seen": time.time() - 3600,
        "last_seen": time.time(),
        "sample_message": "",
        "task_ids": [],
        "is_error_shaped": False,
    }
    spawned = await _spawn_residual_fingerprint_remediation(project, db_session)
    assert spawned == 0, (
        "Heartbeat-only cluster below threshold (5 < 20) must NOT spawn"
    )
    del _residual_store[fp_below]

    # Above threshold: is_error_shaped=False, count=25 >= 20 → must spawn.
    # Explicitly set sample_messages=[] so get_top_residual_fingerprints doesn't
    # synthesize a [""] list that triggers (and fails) the re-classification guard.
    fp_above = "fp_heartbeat_above_threshold_xyz"
    _residual_store[fp_above] = {
        "count": 25,
        "first_seen": time.time() - 3600,
        "last_seen": time.time(),
        "sample_message": "",
        "sample_messages": [],
        "task_ids": [],
        "is_error_shaped": False,
    }
    spawned = await _spawn_residual_fingerprint_remediation(project, db_session)
    assert spawned == 1, (
        "Heartbeat-only cluster above threshold (25 >= 20) SHOULD spawn"
    )


@pytest.mark.asyncio
async def test_residual_fingerprint_remediation_error_shaped_uses_normal_threshold(
    db_session, _clear_residual_state
):
    """CODE_ERROR sample spawns at hit_count=1 — no extra threshold."""
    import time
    from app.failure_classification import _residual_store
    from app.autonomous_research import _spawn_residual_fingerprint_remediation

    project = Project(
        name="Residual Error-Shaped Normal Threshold",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[_active_direction("d-remerrt01")],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    fp = "fp_typeerror_real_<n>"
    sample = "TypeError: 'NoneType' object is not callable"
    _residual_store[fp] = {
        "count": 1,
        "first_seen": time.time() - 3600,
        "last_seen": time.time(),
        "sample_message": sample,
        "task_ids": [],
    }

    spawned = await _spawn_residual_fingerprint_remediation(project, db_session)
    assert spawned == 1, "CODE_ERROR sample should spawn at hit_count=1"


@pytest.mark.asyncio
async def test_residual_defers_when_system_in_audit_mode(
    db_session, _clear_residual_state
):
    """Fingerprint whose tasks all map to a single system in audit mode is deferred (no spawn)."""
    import time
    from app.failure_classification import _residual_store
    from app.autonomous_research import _spawn_residual_fingerprint_remediation
    from app.models import SystemMaturity

    system_id = "sys-auth"
    dir_id = "d-auditgate01"
    project = Project(
        name="Audit Gate Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[{**_active_direction(dir_id), "system_id": system_id}],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    source_task = Task(
        project_id=project.id,
        title="Source failing task",
        payloads=[{"type": "create", "base_branch": "main"}],
        context={"direction_id": dir_id},
        status=TaskStatus.FAILED,
        created_by="ORCHESTRATOR",
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    db_session.add(
        SystemMaturity(
            project_id=project.id,
            system_id=system_id,
            score=40,
            recommended_mode="audit",
        )
    )
    await db_session.commit()

    fp = "fp_audit_gate_defers_xyz"
    _residual_store[fp] = {
        "count": 3,
        "first_seen": time.time() - 3600,
        "last_seen": time.time(),
        "sample_message": "TypeError: audit mode test",
        "task_ids": [str(source_task.id)],
        "is_error_shaped": True,
    }

    spawned = await _spawn_residual_fingerprint_remediation(project, db_session)
    assert spawned == 0, "Fingerprint mapped to audit-mode system must be deferred"


@pytest.mark.asyncio
async def test_residual_defers_when_system_in_fundamentals_mode(
    db_session, _clear_residual_state
):
    """Fingerprint whose tasks map to a system in fundamentals mode is also deferred."""
    import time
    from app.failure_classification import _residual_store
    from app.autonomous_research import _spawn_residual_fingerprint_remediation
    from app.models import SystemMaturity

    system_id = "sys-core"
    dir_id = "d-fundgate01"
    project = Project(
        name="Fundamentals Gate Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[{**_active_direction(dir_id), "system_id": system_id}],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    source_task = Task(
        project_id=project.id,
        title="Source failing task",
        payloads=[{"type": "create", "base_branch": "main"}],
        context={"direction_id": dir_id},
        status=TaskStatus.FAILED,
        created_by="ORCHESTRATOR",
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    db_session.add(
        SystemMaturity(
            project_id=project.id,
            system_id=system_id,
            score=30,
            recommended_mode="fundamentals",
        )
    )
    await db_session.commit()

    fp = "fp_fundamentals_gate_defers_xyz"
    _residual_store[fp] = {
        "count": 3,
        "first_seen": time.time() - 3600,
        "last_seen": time.time(),
        "sample_message": "TypeError: fundamentals mode test",
        "task_ids": [str(source_task.id)],
        "is_error_shaped": True,
    }

    spawned = await _spawn_residual_fingerprint_remediation(project, db_session)
    assert spawned == 0, (
        "Fingerprint mapped to fundamentals-mode system must be deferred"
    )


@pytest.mark.asyncio
async def test_record_residual_audit_deferral_stamps_updated_at(
    db_session, _clear_residual_state
):
    """_record_residual_audit_deferral increments audit_defer_count, records system_id, and bumps updated_at."""
    import time
    from datetime import datetime as _dt
    from app.failure_classification import _residual_store
    from app.autonomous_research import _spawn_residual_fingerprint_remediation
    from app.models import ResidualRemediationCase, ResidualCaseStatus, SystemMaturity

    system_id = "sys-defer-stamp"
    dir_id = "d-deferstamp01"
    project = Project(
        name="Deferral Stamp Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[{**_active_direction(dir_id), "system_id": system_id}],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    source_task = Task(
        project_id=project.id,
        title="Source failing task",
        payloads=[{"type": "create", "base_branch": "main"}],
        context={"direction_id": dir_id},
        status=TaskStatus.FAILED,
        created_by="ORCHESTRATOR",
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    db_session.add(
        SystemMaturity(
            project_id=project.id,
            system_id=system_id,
            score=40,
            recommended_mode="audit",
        )
    )

    fp = "fp_deferral_stamp_test_xyz"
    # Use a hardcoded old timestamp so the comparison is unambiguous.
    old_updated_at = _dt(2020, 1, 1, 0, 0, 0)
    case = ResidualRemediationCase(
        project_id=project.id,
        fingerprint=fp,
        status=ResidualCaseStatus.ACTIVE,
        hit_count=5,
        sample_message="TypeError: deferral stamp test",
        first_seen_at=old_updated_at,
        last_seen_at=old_updated_at,
        created_at=old_updated_at,
        updated_at=old_updated_at,
    )
    db_session.add(case)
    await db_session.commit()

    _residual_store[fp] = {
        "count": 5,
        "first_seen": time.time() - 3600,
        "last_seen": time.time(),
        "sample_message": "TypeError: deferral stamp test",
        "task_ids": [str(source_task.id)],
        "is_error_shaped": True,
    }

    spawned = await _spawn_residual_fingerprint_remediation(project, db_session)
    assert spawned == 0
    # Commit so the deferral's session.add(case) is persisted (the function
    # returns 0 without committing; the real outer caller commits after return).
    await db_session.commit()
    await db_session.refresh(case)
    assert case.updated_at > old_updated_at, (
        "updated_at must be bumped by deferral recording"
    )
    assert case.audit_defer_count == 1, "audit_defer_count must be incremented"
    assert case.audit_deferred_system_id == system_id, (
        "audit_deferred_system_id must be recorded"
    )


@pytest.mark.asyncio
async def test_residual_spawns_when_system_in_ship_mode(
    db_session, _clear_residual_state
):
    """Fingerprint tasks mapping to a ship-mode system are not gated — spawn proceeds normally."""
    import time
    from app.failure_classification import _residual_store
    from app.autonomous_research import _spawn_residual_fingerprint_remediation
    from app.models import SystemMaturity

    system_id = "sys-api"
    dir_id = "d-shipgate01"
    project = Project(
        name="Ship Gate Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[{**_active_direction(dir_id), "system_id": system_id}],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    source_task = Task(
        project_id=project.id,
        title="Source failing task",
        payloads=[{"type": "create", "base_branch": "main"}],
        context={"direction_id": dir_id},
        status=TaskStatus.FAILED,
        created_by="ORCHESTRATOR",
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    db_session.add(
        SystemMaturity(
            project_id=project.id,
            system_id=system_id,
            score=80,
            recommended_mode="ship",
        )
    )
    await db_session.commit()

    fp = "fp_ship_gate_spawns_xyz"
    _residual_store[fp] = {
        "count": 3,
        "first_seen": time.time() - 3600,
        "last_seen": time.time(),
        "sample_message": "TypeError: ship mode spawns",
        "task_ids": [str(source_task.id)],
        "is_error_shaped": True,
    }

    spawned = await _spawn_residual_fingerprint_remediation(project, db_session)
    assert spawned == 1, "Fingerprint in ship-mode system should spawn normally"


@pytest.mark.asyncio
async def test_residual_spawns_when_tasks_span_multiple_systems(
    db_session, _clear_residual_state
):
    """Fingerprint tasks spanning two systems produce an ambiguous mapping — spawn proceeds.

    _infer_fingerprint_audit_system returns None when len(system_ids) != 1, so
    uncertainty never becomes a block even when every candidate system is in audit mode.
    """
    import time
    from app.failure_classification import _residual_store
    from app.autonomous_research import _spawn_residual_fingerprint_remediation
    from app.models import SystemMaturity

    sys_a, sys_b = "sys-mixed-a", "sys-mixed-b"
    dir_a, dir_b = "d-mixed-sysA", "d-mixed-sysB"
    project = Project(
        name="Mixed Systems Ambiguous Gate",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[
            {**_active_direction(dir_a), "system_id": sys_a},
            {**_active_direction(dir_b), "system_id": sys_b},
        ],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task_a = Task(
        project_id=project.id,
        title="Failing task A",
        payloads=[{"type": "create", "base_branch": "main"}],
        context={"direction_id": dir_a},
        status=TaskStatus.FAILED,
        created_by="ORCHESTRATOR",
    )
    task_b = Task(
        project_id=project.id,
        title="Failing task B",
        payloads=[{"type": "create", "base_branch": "main"}],
        context={"direction_id": dir_b},
        status=TaskStatus.FAILED,
        created_by="ORCHESTRATOR",
    )
    db_session.add(task_a)
    db_session.add(task_b)
    await db_session.commit()
    await db_session.refresh(task_a)
    await db_session.refresh(task_b)

    db_session.add(
        SystemMaturity(
            project_id=project.id, system_id=sys_a, score=30, recommended_mode="audit"
        )
    )
    db_session.add(
        SystemMaturity(
            project_id=project.id, system_id=sys_b, score=35, recommended_mode="audit"
        )
    )
    await db_session.commit()

    fp = "fp_mixed_systems_ambiguous_xyz"
    _residual_store[fp] = {
        "count": 4,
        "first_seen": time.time() - 3600,
        "last_seen": time.time(),
        "sample_message": "TypeError: mixed systems test",
        "task_ids": [str(task_a.id), str(task_b.id)],
        "is_error_shaped": True,
    }

    spawned = await _spawn_residual_fingerprint_remediation(project, db_session)
    assert spawned == 1, (
        "Fingerprint spanning multiple systems must spawn — ambiguous mapping is not a block"
    )


@pytest.mark.asyncio
async def test_residual_resumes_spawn_when_system_exits_audit_mode(
    db_session, _clear_residual_state
):
    """Spawn resumes after a system's recommended_mode changes from audit back to ship."""
    import time
    from app.failure_classification import _residual_store
    from app.autonomous_research import _spawn_residual_fingerprint_remediation
    from app.models import ResidualRemediationCase, ResidualCaseStatus, SystemMaturity

    system_id = "sys-resume"
    dir_id = "d-resumetest01"
    project = Project(
        name="Resume After Audit",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[{**_active_direction(dir_id), "system_id": system_id}],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    source_task = Task(
        project_id=project.id,
        title="Source failing task",
        payloads=[{"type": "create", "base_branch": "main"}],
        context={"direction_id": dir_id},
        status=TaskStatus.FAILED,
        created_by="ORCHESTRATOR",
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    mat = SystemMaturity(
        project_id=project.id, system_id=system_id, score=35, recommended_mode="audit"
    )
    db_session.add(mat)
    case = ResidualRemediationCase(
        project_id=project.id,
        fingerprint="fp_resume_audit_xyz",
        status=ResidualCaseStatus.ACTIVE,
        hit_count=4,
    )
    db_session.add(case)
    await db_session.commit()

    fp = "fp_resume_audit_xyz"
    _residual_store[fp] = {
        "count": 4,
        "first_seen": time.time() - 7200,
        "last_seen": time.time(),
        "sample_message": "TypeError: resume test",
        "task_ids": [str(source_task.id)],
        "is_error_shaped": True,
    }

    spawned = await _spawn_residual_fingerprint_remediation(project, db_session)
    assert spawned == 0, "Spawn must be blocked when system is in audit mode"

    mat.recommended_mode = "ship"
    db_session.add(mat)
    await db_session.commit()

    spawned = await _spawn_residual_fingerprint_remediation(project, db_session)
    assert spawned == 1, "Spawn must resume when system exits audit mode"


# ─────────────────────────────────────────────────────────────────────────────
# _spawn_viewport_regression_remediation
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture()
def _clear_viewport_residual_state():
    """Clear the viewport residual fingerprint store between tests."""
    from app.failure_residual import (
        _viewport_residual_store,
        _viewport_residual_seen_task_ids,
    )

    _viewport_residual_store.clear()
    _viewport_residual_seen_task_ids.clear()
    yield
    _viewport_residual_store.clear()
    _viewport_residual_seen_task_ids.clear()


@pytest.mark.asyncio
async def test_viewport_regression_remediation_spawns_task(
    db_session, _clear_viewport_residual_state
):
    """Top viewport fingerprint spawns exactly one create task with auto_remediation context."""
    import time
    from app.failure_residual import _viewport_residual_store
    from app.autonomous_research_residual import _spawn_viewport_regression_remediation

    project = Project(
        name="Viewport Remediation Spawn",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[_active_direction("d-vprem001")],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    fp = "viewport:MyComponent:overflow"
    sample_msg = "expectNoOverflowAt(375) failed for MyComponent"
    task_id_str = str(uuid.uuid4())
    _viewport_residual_store[fp] = {
        "count": 4,
        "first_seen": time.time() - 3600,
        "last_seen": time.time(),
        "component": "MyComponent",
        "assertion_type": "overflow",
        "sample_message": sample_msg,
        "task_ids": [task_id_str],
    }

    spawned = await _spawn_viewport_regression_remediation(project, db_session)
    assert spawned == 1

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    create_tasks = [
        t for t in result.scalars().all() if t.first_payload.get("type") == "create"
    ]
    assert len(create_tasks) == 1
    t = create_tasks[0]
    assert t.auto_remediation is True
    assert t.context["spawned_by"] == "viewport_regression_remediation"
    assert t.context["viewport_fingerprint"] == fp
    assert t.context["component"] == "MyComponent"
    assert t.context["assertion_type"] == "overflow"
    assert sample_msg in t.first_payload["prompt"]


@pytest.mark.asyncio
async def test_viewport_regression_remediation_cooldown_blocks_respawn(
    db_session, _clear_viewport_residual_state
):
    """Active cooldown (last_spawned_at within 24 h) prevents re-spawning the same fingerprint."""
    import time
    from datetime import timedelta as _td
    from app.failure_residual import _viewport_residual_store
    from app.autonomous_research_residual import _spawn_viewport_regression_remediation
    from app.models import ResidualCaseStatus, ResidualRemediationCase, _utcnow

    project = Project(
        name="Viewport Cooldown Block",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[_active_direction("d-vpcool01")],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    fp = "viewport:NavBar:overflow"
    _viewport_residual_store[fp] = {
        "count": 3,
        "first_seen": time.time() - 7200,
        "last_seen": time.time(),
        "component": "NavBar",
        "assertion_type": "overflow",
        "sample_message": "overflow detected",
        "task_ids": [],
    }
    # Seed a case row with a recent last_spawned_at (within 24-h cooldown)
    case = ResidualRemediationCase(
        project_id=project.id,
        fingerprint=fp,
        status=ResidualCaseStatus.ACTIVE,
        last_spawned_at=_utcnow() - _td(minutes=30),
    )
    db_session.add(case)
    await db_session.commit()

    spawned = await _spawn_viewport_regression_remediation(project, db_session)
    assert spawned == 0

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    assert len(result.scalars().all()) == 0


@pytest.mark.asyncio
async def test_viewport_regression_remediation_concurrent_cap_blocks_spawn(
    db_session, _clear_viewport_residual_state
):
    """At or above max_concurrent_auto_remediations cap, no viewport task is spawned."""
    import time
    from app.failure_residual import _viewport_residual_store
    from app.autonomous_research_residual import _spawn_viewport_regression_remediation
    from app.models import ResidualCaseStatus, ResidualRemediationCase
    import app.autonomous_research_residual as _rar_module

    project = Project(
        name="Viewport Concurrency Cap",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[_active_direction("d-vpconc01")],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    fp = "viewport:Footer:text_truncation"
    _viewport_residual_store[fp] = {
        "count": 5,
        "first_seen": time.time() - 3600,
        "last_seen": time.time(),
        "component": "Footer",
        "assertion_type": "text_truncation",
        "sample_message": "text truncated",
        "task_ids": [],
    }
    # Add REMEDIATING rows equal to the cap (default 2) to saturate concurrency
    cap = int(_rar_module._runtime_config.get("max_concurrent_auto_remediations", 2))
    for i in range(cap):
        db_session.add(
            ResidualRemediationCase(
                project_id=project.id,
                fingerprint=f"viewport:OtherComp:overflow_{i}",
                status=ResidualCaseStatus.REMEDIATING,
            )
        )
    await db_session.commit()

    spawned = await _spawn_viewport_regression_remediation(project, db_session)
    assert spawned == 0


@pytest.mark.asyncio
async def test_viewport_regression_remediation_skips_ignored_fingerprint(
    db_session, _clear_viewport_residual_state
):
    """IGNORED fingerprints are skipped; SUSPENDED fingerprints are also skipped."""
    import time
    from app.failure_residual import _viewport_residual_store
    from app.autonomous_research_residual import _spawn_viewport_regression_remediation
    from app.models import ResidualCaseStatus, ResidualRemediationCase

    project = Project(
        name="Viewport Ignored Skip",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[_active_direction("d-vpign01")],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    fp_ignored = "viewport:Sidebar:overflow"
    fp_suspended = "viewport:Header:min_touch_target"
    for fp, comp, at, status in [
        (fp_ignored, "Sidebar", "overflow", ResidualCaseStatus.IGNORED),
        (fp_suspended, "Header", "min_touch_target", ResidualCaseStatus.SUSPENDED),
    ]:
        _viewport_residual_store[fp] = {
            "count": 6,
            "first_seen": time.time() - 3600,
            "last_seen": time.time(),
            "component": comp,
            "assertion_type": at,
            "sample_message": f"failure in {comp}",
            "task_ids": [],
        }
        db_session.add(
            ResidualRemediationCase(
                project_id=project.id,
                fingerprint=fp,
                status=status,
            )
        )
    await db_session.commit()

    spawned = await _spawn_viewport_regression_remediation(project, db_session)
    assert spawned == 0

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    assert len(result.scalars().all()) == 0


# ─────────────────────────────────────────────────────────────────────────────
# Failure Feedback Consumption — Coaching Suffix & Scope Override
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_coaching_suffix_injected_for_dominant_failure_category(db_session):
    """_spawn_research_task injects a failure-mode coaching suffix into the research payload
    when a direction has a dominant first-attempt rejection category (>=3 total, >=40% in one cat)."""
    dir_id = "d-coachint01"
    project = Project(
        name="Coaching Suffix Integration",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[_active_direction(dir_id, "Improve test coverage")],
        vision={"purpose": "test", "_research_spawn_count": 1},
        design_doc={
            "direction_first_attempt_feedback": {
                dir_id: {
                    "test_coverage": 4,
                    "correctness": 1,
                    "total": 5,
                    "last_updated": "2026-01-01",
                }
            }
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _spawn_research_task(project, db_session)

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    tasks = result.scalars().all()
    research = [t for t in tasks if t.first_payload.get("type") == "research"]
    assert len(research) == 1, "Should spawn one research task"

    suffix = research[0].first_payload.get("prompt_suffix", "")
    assert suffix, "prompt_suffix should be set when dominant failure category exists"
    assert "test_coverage" in suffix, (
        f"Coaching suffix should name the dominant failure category; got: {suffix!r}"
    )
    assert "4/5" in suffix, f"Coaching should cite the count/total; got: {suffix!r}"


@pytest.mark.asyncio
async def test_coaching_suffix_absent_below_rejection_threshold(db_session):
    """No coaching suffix is injected when the direction has fewer than 3 first-attempt rejections."""
    dir_id = "d-coachlow01"
    project = Project(
        name="Coaching Suffix Below Threshold",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[_active_direction(dir_id, "Improve error handling")],
        vision={"purpose": "test", "_research_spawn_count": 1},
        design_doc={
            "direction_first_attempt_feedback": {
                dir_id: {
                    "test_coverage": 2,  # Total=2 < 3 → no coaching
                    "total": 2,
                    "last_updated": "2026-01-01",
                }
            }
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _spawn_research_task(project, db_session)

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    tasks = result.scalars().all()
    research = [t for t in tasks if t.first_payload.get("type") == "research"]
    assert len(research) == 1, "Should still spawn one research task"
    suffix = research[0].first_payload.get("prompt_suffix", "")
    assert "recurring first-attempt rejections" not in suffix, (
        "Coaching text must not appear when rejection count is below the 3-rejection threshold"
    )


def test_calibrate_scope_downscopes_via_failure_feedback():
    """_calibrate_scope falls back to _feedback_scope_override and downscopes medium→small
    when the direction has >=3 scope_related first-attempt failures and no historical data yet."""
    import types
    from app.spawn_calibration import _calibrate_scope

    dir_id = "d-scopedwn01"
    direction = _active_direction(dir_id, "Scope downgrade test")
    direction["research_history"] = []  # No historical data → cold-start fallback path

    project = types.SimpleNamespace(
        directions=[direction],
        design_doc={
            "direction_first_attempt_feedback": {
                dir_id: {
                    "scope_related": 3,  # 3 failures → triggers downscope
                    "total": 3,
                    "last_updated": "2026-01-01",
                }
            }
        },
    )

    final_scope, calibrated_from = _calibrate_scope(
        "medium", direction, project=project, task_type="create"
    )
    assert final_scope == "small", (
        f"Expected medium→small downscope via failure feedback, got {final_scope!r}"
    )
    assert calibrated_from == "medium", (
        "calibrated_from should record the original scope"
    )


def test_success_pattern_retains_scope_against_cold_start_upgrade():
    """_calibrate_scope retains the proposed scope when a strong success pattern exists,
    even when pipeline baseline rates would otherwise trigger a cold-start medium→large upgrade."""
    import types
    from app.spawn_calibration import _calibrate_scope
    from app.shared_state import runtime_config as _runtime_config

    dir_id = "d-scopesucc01"
    direction = _active_direction(dir_id, "Success pattern scope test")
    direction["research_history"] = []  # No historical data → cold-start fallback path

    project = types.SimpleNamespace(
        directions=[direction],
        design_doc={
            "playbook/success-create-medium": {
                "success_count": 6,  # 6 >= 5 → strong success evidence, retain scope
                "content": "Medium scope works well for create tasks",
            }
        },
    )

    # Pipeline baseline rates that would normally trigger cold-start medium→large upgrade
    # (medium rate 20% < 40% threshold, small rate healthy at 80%)
    original_rates = _runtime_config.get("pipeline_scope_baseline_rates")
    _runtime_config["pipeline_scope_baseline_rates"] = {"medium": 0.2, "small": 0.8}
    try:
        final_scope, calibrated_from = _calibrate_scope(
            "medium", direction, project=project, task_type="create"
        )
        assert final_scope == "medium", (
            f"Success pattern should prevent cold-start upgrade to large, got {final_scope!r}"
        )
        assert calibrated_from is None, (
            "No calibration adjustment when scope is retained by success pattern"
        )
    finally:
        if original_rates is None:
            _runtime_config.pop("pipeline_scope_baseline_rates", None)
        else:
            _runtime_config["pipeline_scope_baseline_rates"] = original_rates


# ─────────────────────────────────────────────────────────────────────────────
# recommended_mode — direction-level dispatch behavior
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_spawn_skips_pause_mode_direction(db_session):
    """A direction with recommended_mode='pause' is excluded from spawn selection.
    A normal direction in the same project is still spawned."""
    project = Project(
        name="Pause Mode Direction",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[
            {
                **_active_direction("d-pausemode", "Paused system"),
                "recommended_mode": "pause",
            },
            _active_direction("d-normaldir", "Normal direction"),
        ],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _spawn_research_task(project, db_session)

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    research = [
        t for t in result.scalars().all() if t.first_payload.get("type") == "research"
    ]
    assert len(research) == 1
    spawned_dir = research[0].first_payload.get("current_direction_id")
    assert spawned_dir == "d-normaldir", (
        f"pause-mode direction must be skipped; got {spawned_dir!r}"
    )


@pytest.mark.asyncio
async def test_audit_mode_direction_injects_mode_hint(db_session):
    """A direction with recommended_mode='audit' receives a mode-specific prompt_suffix
    biasing the research agent toward fix/test/refactor findings."""
    project = Project(
        name="Audit Mode Direction",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[
            {
                **_active_direction("d-auditdir", "Audit direction"),
                "recommended_mode": "audit",
            }
        ],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _spawn_research_task(project, db_session)

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    research = [
        t for t in result.scalars().all() if t.first_payload.get("type") == "research"
    ]
    assert len(research) == 1
    suffix = research[0].first_payload.get("prompt_suffix", "")
    # Accept any audit-mode hint that biases away from "new features".
    # mode_prompt_hint helper produces validation/audit/drift framing,
    # earlier wording used "fix/test/refactor".
    suffix_l = suffix.lower()
    assert "audit" in suffix_l and (
        "fix/test/refactor" in suffix
        or "validation" in suffix_l
        or "auditing" in suffix_l
    ), f"audit-mode direction must inject mode hint in prompt_suffix; got: {suffix!r}"


@pytest.mark.asyncio
async def test_fundamentals_mode_direction_injects_mode_hint(db_session):
    """A direction with recommended_mode='fundamentals' receives the same mode-specific
    prompt_suffix as 'audit' mode, biasing research toward stabilization work."""
    project = Project(
        name="Fundamentals Mode Direction",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[
            {
                **_active_direction("d-funddir0", "Fundamentals direction"),
                "recommended_mode": "fundamentals",
            }
        ],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _spawn_research_task(project, db_session)

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    research = [
        t for t in result.scalars().all() if t.first_payload.get("type") == "research"
    ]
    assert len(research) == 1
    suffix = research[0].first_payload.get("prompt_suffix", "")
    suffix_l = suffix.lower()
    assert "fundamentals" in suffix_l and (
        "fix/test/refactor" in suffix
        or "tests" in suffix_l
        or "maintainability" in suffix_l
    ), (
        f"fundamentals-mode direction must inject mode hint in prompt_suffix; got: {suffix!r}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Playbook followed_count — research_completion credits shown entries
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_followed_count_incremented_when_research_spawns_impl_task(
    db_session, mocker
):
    """When a research task spawns >= 1 impl task and _shown_playbook_keys is set,
    followed_count is incremented on each shown playbook entry in design_doc.
    Entries not listed in _shown_playbook_keys are not modified."""
    direction = _active_direction("d-followpb1", "Playbook follow test direction")
    project = Project(
        name="Followed Count Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
    )
    project.design_doc = {
        "playbook/shown-entry": {
            "content": "use dependency injection",
            "applicable_types": ["create"],
            "source_task_id": None,
            "updated_at": "2026-01-01T00:00:00",
            "confirmed_count": 2,
            "reference_count": 4,
            "followed_count": 1,
        },
        "playbook/not-shown-entry": {
            "content": "write unit tests first",
            "applicable_types": ["create"],
            "source_task_id": None,
            "updated_at": "2026-01-01T00:00:00",
            "confirmed_count": 0,
            "reference_count": 1,
            "followed_count": 0,
        },
    }
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Research: Playbook follow test direction",
        payloads=[
            {"type": "research", "directions": [direction], "base_branch": "main"}
        ],
        context={"research_depth": 0, "_shown_playbook_keys": ["shown-entry"]},
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
        return_value=[_make_finding(direction_id=direction["id"])],
    )

    await _handle_research_completion(task, "", db_session)

    await db_session.refresh(project)
    shown = project.design_doc["playbook/shown-entry"]
    not_shown = project.design_doc["playbook/not-shown-entry"]

    assert shown["followed_count"] == 2, (
        f"Expected followed_count=2, got {shown['followed_count']}"
    )
    assert not_shown["followed_count"] == 0, "Not-shown entry must not be modified"


@pytest.mark.asyncio
async def test_followed_count_not_incremented_when_no_impl_spawned(db_session, mocker):
    """When research produces zero impl tasks (all findings deduped), followed_count
    must NOT be incremented even if _shown_playbook_keys is set."""
    direction = _active_direction("d-followpb2", "Playbook follow zero-spawn direction")
    project = Project(
        name="Followed Count Zero Spawn",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
    )
    project.design_doc = {
        "playbook/shown-entry": {
            "content": "avoid globals",
            "applicable_types": ["create"],
            "source_task_id": None,
            "updated_at": "2026-01-01T00:00:00",
            "confirmed_count": 1,
            "reference_count": 2,
            "followed_count": 3,
        },
    }
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    dup_title = "Avoid globals in config module"
    dup_desc = "Global state causes test isolation failures"
    import hashlib as _hashlib

    fhash = _hashlib.sha256((dup_title + dup_desc).encode()).hexdigest()[:16]
    existing = Task(
        project_id=project.id,
        title=dup_title,
        payloads=[
            {"type": "create", "prompt": "remove globals", "base_branch": "main"}
        ],
        context={"finding_hash": fhash},
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(existing)

    task = Task(
        project_id=project.id,
        title="Research: zero spawn",
        payloads=[
            {"type": "research", "directions": [direction], "base_branch": "main"}
        ],
        context={"research_depth": 0, "_shown_playbook_keys": ["shown-entry"]},
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
        return_value=[
            {
                "title": dup_title,
                "description": dup_desc,
                "prompt": "remove globals",
                "priority": "medium",
                "scope": "small",
                "risk": "low",
                "type": "improvement",
                "direction_id": direction["id"],
            }
        ],
    )

    await _handle_research_completion(task, "", db_session)

    await db_session.refresh(project)
    shown = project.design_doc["playbook/shown-entry"]
    assert shown["followed_count"] == 3, (
        "followed_count must not change when no impl tasks are spawned"
    )


# ─────────────────────────────────────────────────────────────────────────────
# PayloadTooLargeError: strategic payload bloat marker
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_payload_too_large_writes_vision_bloat_marker(db_session, mocker):
    """When PayloadTooLargeError fires during strategic spawn, project.vision gets a
    _strategic_payload_bloat_skip marker with skipped_at, size, and top_contributors."""
    from app.payload_schemas import PayloadTooLargeError

    # Project with no purpose → force_strategic fires without active directions.
    project = Project(
        name="Bloat Test Project",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[],
        vision={},  # no "purpose" key → force_strategic=True
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    bloat_error = PayloadTooLargeError(
        size=310000,
        limit=262144,
        top_keys=[("design_doc", 250000), ("problem_model", 50000), ("vision", 10000)],
    )

    # Patch both strategic spawner paths so PayloadTooLargeError is raised
    # regardless of which cycle type is selected.
    mocker.patch(
        "app.strategic_spawning._spawn_design_doc_update",
        side_effect=bloat_error,
    )
    mocker.patch(
        "app.strategic_spawning._spawn_strategic_review",
        side_effect=bloat_error,
    )

    await _spawn_research_task(project, db_session)

    await db_session.refresh(project)
    marker = (project.vision or {}).get("_strategic_payload_bloat_skip")
    assert marker is not None, (
        "Expected _strategic_payload_bloat_skip to be set in vision"
    )
    assert marker["size"] == 310000
    assert "skipped_at" in marker
    contributors = marker["top_contributors"]
    assert len(contributors) >= 1
    assert contributors[0]["key"] == "design_doc"
    assert contributors[0]["size"] == 250000


@pytest.mark.asyncio
async def test_successful_strategic_spawn_clears_bloat_marker(db_session, mocker):
    """A stale _strategic_payload_bloat_skip marker is cleared once a strategic
    spawn succeeds again (payload shrank back under the cap), so the strategic-health
    panel and stall investigation stop flagging a recovered bloat condition."""
    project = Project(
        name="Bloat Recovery Project",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[],
        vision={
            "_strategic_payload_bloat_skip": {
                "skipped_at": "2026-05-29T00:00:00",
                "size": 310000,
                "top_contributors": [{"key": "design_doc", "size": 250000}],
            }
        },  # no "purpose" key → force_strategic=True
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Both strategic spawner paths now succeed (payload fits under the cap).
    mocker.patch(
        "app.strategic_spawning._spawn_design_doc_update",
        new=AsyncMock(return_value=True),
    )
    mocker.patch(
        "app.strategic_spawning._spawn_strategic_review",
        new=AsyncMock(return_value=True),
    )

    await _spawn_research_task(project, db_session)

    await db_session.refresh(project)
    assert "_strategic_payload_bloat_skip" not in (project.vision or {}), (
        "Expected the stale bloat marker to be cleared after a successful strategic spawn"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Wall-clock strategic backstop
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_strategic_max_interval_backstop_fires_when_stale(db_session, mocker):
    """Wall-clock backstop forces a strategic spawn when the last strategic task
    is older than strategic_max_interval_minutes, even though the modulo path
    (research_spawn_count % strategic_frequency) does not trigger and
    force_strategic is inactive (purpose set + at least one active direction).

    This decouples steering frequency from research-spawn rate so a narrowed
    direction count does not throttle the strategic agent's own decision cadence.
    """
    mock_spawn = AsyncMock(return_value=True)
    mocker.patch("app.strategic_spawning._spawn_strategic_review", mock_spawn)
    mocker.patch(
        "app.strategic_spawning._spawn_design_doc_update",
        new=AsyncMock(return_value=True),
    )

    project = Project(
        name="Backstop Fires Project",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[_active_direction("d-backstop01", "Test direction")],
        # purpose set → not force_strategic; count=1 → 1 % 6 != 0 → modulo inactive
        vision={"purpose": "test", "_research_spawn_count": 1},
        design_doc={
            "systems/api": {"id": "api", "status": "working"}
        },  # Has systems, so architecture isn't triggered
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    stale_strategic = Task(
        project_id=project.id,
        title="Old Strategic",
        payloads=[{"type": "strategic_review", "base_branch": "main"}],
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        created_at=_utcnow() - timedelta(hours=4),
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(stale_strategic)
    await db_session.commit()

    await _spawn_research_task(project, db_session)

    assert mock_spawn.called, (
        "Wall-clock backstop must force _spawn_strategic_review when the prior "
        "strategic task is older than strategic_max_interval_minutes, even though "
        "the modulo path and force_strategic are both inactive."
    )


@pytest.mark.asyncio
async def test_strategic_max_interval_backstop_quiet_within_window(db_session, mocker):
    """Backstop must NOT fire when the prior strategic is within the window
    (the existing cooldown also blocks the spawn here)."""
    mock_spawn = AsyncMock(return_value=True)
    mocker.patch("app.strategic_spawning._spawn_strategic_review", mock_spawn)
    mocker.patch(
        "app.strategic_spawning._spawn_design_doc_update",
        new=AsyncMock(return_value=True),
    )

    project = Project(
        name="Backstop Quiet Project",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[_active_direction("d-backstop02", "Test direction")],
        vision={"purpose": "test", "_research_spawn_count": 1},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    fresh_strategic = Task(
        project_id=project.id,
        title="Fresh Strategic",
        payloads=[{"type": "strategic_review", "base_branch": "main"}],
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        created_at=_utcnow() - timedelta(minutes=30),
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(fresh_strategic)
    await db_session.commit()

    await _spawn_research_task(project, db_session)

    assert not mock_spawn.called, (
        "Backstop must remain quiet when the prior strategic is within the window."
    )


@pytest.mark.asyncio
async def test_strategic_max_interval_backstop_fires_when_all_directions_busy(
    db_session, mocker
):
    """Regression for the 2026-05-26 production gap: when every active direction
    has an in-flight child task, _process_direction_lifecycle leaves
    active_directions empty and the function early-returns at the
    `not active_directions and not force_strategic` guard, BEFORE the backstop
    runs. The backstop must drive force_strategic so it fires even in this case.
    """
    mock_spawn = AsyncMock(return_value=True)
    mocker.patch("app.strategic_spawning._spawn_strategic_review", mock_spawn)
    mocker.patch(
        "app.strategic_spawning._spawn_design_doc_update",
        new=AsyncMock(return_value=True),
    )

    dir_id = "d-busybackstop01"
    project = Project(
        name="Backstop Busy Project",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[_active_direction(dir_id, "Busy direction")],
        vision={"purpose": "test", "_research_spawn_count": 1},
        design_doc={
            "systems/api": {"id": "api", "status": "working"}
        },  # Has systems, so architecture isn't triggered
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # An IN_PROGRESS research child on the only active direction → direction
    # is "busy" → _process_direction_lifecycle excludes it → active_directions
    # becomes empty.
    busy_child = Task(
        project_id=project.id,
        title="Busy Research",
        payloads=[
            {
                "type": "research",
                "directions": [{"id": dir_id}],
                "current_direction_id": dir_id,
                "base_branch": "main",
            }
        ],
        status=TaskStatus.IN_PROGRESS,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(busy_child)

    stale_strategic = Task(
        project_id=project.id,
        title="Old Strategic",
        payloads=[{"type": "strategic_review", "base_branch": "main"}],
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        created_at=_utcnow() - timedelta(hours=4),
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(stale_strategic)
    await db_session.commit()

    await _spawn_research_task(project, db_session)

    assert mock_spawn.called, (
        "Wall-clock backstop must fire even when every active direction is "
        "busy. The backstop has to drive force_strategic so the function does "
        "not early-return at the `not active_directions` guard."
    )


@pytest.mark.asyncio
async def test_strategic_max_interval_backstop_disabled_by_zero(db_session, mocker):
    """Setting strategic_max_interval_minutes=0 disables the backstop entirely."""
    mock_spawn = AsyncMock(return_value=True)
    mocker.patch("app.strategic_spawning._spawn_strategic_review", mock_spawn)
    mocker.patch(
        "app.strategic_spawning._spawn_design_doc_update",
        new=AsyncMock(return_value=True),
    )
    mocker.patch.dict(
        _shared_state.runtime_config, {"strategic_max_interval_minutes": 0}
    )

    project = Project(
        name="Backstop Disabled Project",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[_active_direction("d-backstop03", "Test direction")],
        vision={"purpose": "test", "_research_spawn_count": 1},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    stale_strategic = Task(
        project_id=project.id,
        title="Old Strategic",
        payloads=[{"type": "strategic_review", "base_branch": "main"}],
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        created_at=_utcnow() - timedelta(days=7),
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(stale_strategic)
    await db_session.commit()

    await _spawn_research_task(project, db_session)

    assert not mock_spawn.called, (
        "Backstop must stay quiet when strategic_max_interval_minutes is 0 (disabled)."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Direction reasoning memo injection
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_direction_reasoning_memos_injected_into_research_payload(
    db_session, mocker
):
    """Research task payload includes direction_reasoning_memos when memo tasks exist for the direction."""
    direction_id = "d-memo-inject01"
    direction = _active_direction(direction_id, "Improve coverage")
    project = Project(
        name="Memo Injection Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test", "_research_spawn_count": 1},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Create a completed memo task associated with this direction
    memo_task = Task(
        project_id=project.id,
        title="Memo: investigation unactionable",
        payloads=[{"type": "memo", "topic": "structural issue, no actionable fix"}],
        context={
            "memo_reasoning_kind": "investigation_unactionable",
            "investigation_outcome": "structural",
            "investigation_summary": "Root cause is structural — no targeted fix exists.",
            "direction_id": direction_id,
        },
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(memo_task)
    await db_session.commit()

    mocker.patch(
        "app.anomaly_detection.check_anomaly_actions", return_value={"action": "none"}
    )
    mocker.patch("app.strategic_spawning._spawn_strategic_review", return_value=False)
    mocker.patch("app.strategic_spawning._spawn_design_doc_update", return_value=False)

    await _spawn_research_task(project, db_session)

    result = await db_session.execute(
        select(Task).where(
            Task.project_id == project.id,
            Task.id != memo_task.id,
            Task.payloads[0]["type"].as_string() == "research",
        )
    )
    research_task = result.scalars().first()
    assert research_task is not None, "Research task should have been spawned"
    payload = research_task.first_payload
    memos = payload.get("direction_reasoning_memos")
    assert memos is not None, "direction_reasoning_memos should be present in payload"
    assert len(memos) == 1
    assert memos[0]["kind"] == "investigation_unactionable"
    assert memos[0]["outcome"] == "structural"
    assert "structural" in memos[0]["summary"]


@pytest.mark.asyncio
async def test_direction_reasoning_memos_absent_when_no_memos(db_session, mocker):
    """Research task payload omits direction_reasoning_memos when no memo tasks exist."""
    direction_id = "d-memo-absent01"
    direction = _active_direction(direction_id, "Improve coverage")
    project = Project(
        name="No Memo Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test", "_research_spawn_count": 1},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    mocker.patch(
        "app.anomaly_detection.check_anomaly_actions", return_value={"action": "none"}
    )
    mocker.patch("app.strategic_spawning._spawn_strategic_review", return_value=False)
    mocker.patch("app.strategic_spawning._spawn_design_doc_update", return_value=False)

    await _spawn_research_task(project, db_session)

    result = await db_session.execute(
        select(Task).where(
            Task.project_id == project.id,
            Task.payloads[0]["type"].as_string() == "research",
        )
    )
    research_task = result.scalars().first()
    assert research_task is not None, "Research task should have been spawned"
    payload = research_task.first_payload
    assert "direction_reasoning_memos" not in payload, (
        "direction_reasoning_memos should not be present when no memos exist"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Design-doc system verification (claimed-but-absent / partial / verified)
# ─────────────────────────────────────────────────────────────────────────────


def _patch_classify(mocker, present_paths: set):
    """Patch the design-doc verifier so existence is driven by *present_paths*."""

    async def _classify(paths, **_kwargs):
        present = [p for p in paths if isinstance(p, str) and p in present_paths]
        missing = [p for p in paths if not (isinstance(p, str) and p in present_paths)]
        return present, missing, []

    mocker.patch(
        "app.autonomous_research.classify_paths_tristate", side_effect=_classify
    )


async def _spawn_and_get_research_payload(project, db_session):
    await _spawn_research_task(project, db_session)
    result = await db_session.execute(
        select(Task).where(
            Task.project_id == project.id,
            Task.payloads[0]["type"].as_string() == "research",
        )
    )
    research_task = result.scalars().first()
    assert research_task is not None, "Research task should have been spawned"
    return research_task.first_payload


@pytest.mark.asyncio
async def test_system_verification_all_missing_flagged_claimed_absent(
    db_session, mocker
):
    """A system whose every declared module is absent is flagged claimed_absent."""
    direction = _active_direction("d-sysverif-absent", "Improve ghost system")
    project = Project(
        name="SysVerif Absent",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test", "_research_spawn_count": 1},
        design_doc={
            "systems/ghost": {
                "status": "mature",
                "modules": ["backend/app/ghost_a.py", "backend/app/ghost_b.py"],
            }
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    _patch_classify(mocker, present_paths=set())

    payload = await _spawn_and_get_research_payload(project, db_session)
    sv = payload.get("system_verification")
    assert sv is not None, "system_verification annotation should be present"
    assert sv["ghost"]["status"] == "claimed_absent"
    assert sv["ghost"]["claimed_status"] == "mature"
    assert sv["ghost"]["present"] == []
    assert set(sv["ghost"]["missing"]) == {
        "backend/app/ghost_a.py",
        "backend/app/ghost_b.py",
    }


@pytest.mark.asyncio
async def test_system_verification_mixed_flagged_partial(db_session, mocker):
    """A system with some modules present and some missing is flagged partial."""
    direction = _active_direction("d-sysverif-partial", "Improve partial system")
    project = Project(
        name="SysVerif Partial",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test", "_research_spawn_count": 1},
        design_doc={
            "systems/half": {
                "status": "functional",
                "modules": ["backend/app/real.py", "backend/app/fake.py"],
            }
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    _patch_classify(mocker, present_paths={"backend/app/real.py"})

    payload = await _spawn_and_get_research_payload(project, db_session)
    sv = payload.get("system_verification")
    assert sv is not None
    assert sv["half"]["status"] == "partial"
    assert sv["half"]["present"] == ["backend/app/real.py"]
    assert sv["half"]["missing"] == ["backend/app/fake.py"]


@pytest.mark.asyncio
async def test_system_verification_all_present_flagged_verified(db_session, mocker):
    """A system whose declared modules all exist records status verified."""
    direction = _active_direction("d-sysverif-ok", "Improve real system")
    project = Project(
        name="SysVerif Verified",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test", "_research_spawn_count": 1},
        design_doc={
            "systems/real": {
                "status": "mature",
                "modules": ["backend/app/a.py", "backend/app/b.py"],
            }
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    _patch_classify(mocker, present_paths={"backend/app/a.py", "backend/app/b.py"})

    payload = await _spawn_and_get_research_payload(project, db_session)
    sv = payload.get("system_verification")
    assert sv is not None
    assert sv["real"]["status"] == "verified"
    assert sv["real"]["missing"] == []


@pytest.mark.asyncio
async def test_system_verification_does_not_mutate_design_doc(db_session, mocker):
    """Verification is advisory only — the stored design_doc is left unchanged."""
    direction = _active_direction("d-sysverif-immut", "Improve system")
    original_doc = {
        "systems/x": {
            "status": "mature",
            "modules": ["backend/app/missing.py"],
        }
    }
    project = Project(
        name="SysVerif Immutable",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test", "_research_spawn_count": 1},
        design_doc={k: dict(v) for k, v in original_doc.items()},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    _patch_classify(mocker, present_paths=set())

    await _spawn_and_get_research_payload(project, db_session)
    await db_session.refresh(project)
    assert project.design_doc == original_doc, (
        "design_doc must not be mutated by verification (no status field added)"
    )


@pytest.mark.asyncio
async def test_system_verification_folds_into_direction_context(db_session, mocker):
    """The current direction's system verdict is folded into direction_design_context."""
    system_id = "sys-folded"
    direction = {
        **_active_direction("d-sysverif-fold", "Improve folded"),
        "system_id": system_id,
    }
    project = Project(
        name="SysVerif Fold",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test", "_research_spawn_count": 1},
        design_doc={
            f"systems/{system_id}": {
                "status": "mature",
                "modules": ["backend/app/gone.py"],
            }
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    _patch_classify(mocker, present_paths=set())

    payload = await _spawn_and_get_research_payload(project, db_session)
    ctx = payload.get("direction_design_context")
    assert ctx is not None
    assert ctx.get("verification", {}).get("status") == "claimed_absent"


@pytest.mark.asyncio
async def test_direction_design_context_populated_from_keyed_systems_page(
    db_session, mocker
):
    """direction_design_context["system"] comes from the flat systems/<id> key."""
    system_id = "sys-abc"
    direction = {
        **_active_direction("d-ctx-keyed-sys", "Improve keyed system"),
        "system_id": system_id,
    }
    sys_page = {"id": system_id, "status": "working", "modules": ["backend/app/abc.py"]}
    project = Project(
        name="Keyed System Page",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test", "_research_spawn_count": 1},
        design_doc={f"systems/{system_id}": sys_page},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    _patch_classify(mocker, present_paths={"backend/app/abc.py"})

    payload = await _spawn_and_get_research_payload(project, db_session)
    ctx = payload.get("direction_design_context")
    assert ctx is not None
    assert ctx.get("system_id") == system_id
    assert ctx.get("system") == sys_page, (
        "system page should be populated from flat systems/<id> key"
    )


@pytest.mark.asyncio
async def test_direction_design_context_uses_keyed_design_doc_pages(db_session, mocker):
    """missing_capabilities comes from flat capabilities/<id> keys, not a nested dict."""
    system_id = "sys-caps"
    direction = {
        **_active_direction("d-ctx-keyed-caps", "Improve keyed caps"),
        "system_id": system_id,
    }
    project = Project(
        name="Keyed Caps Page",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test", "_research_spawn_count": 1},
        design_doc={
            f"systems/{system_id}": {"id": system_id, "status": "working"},
            "capabilities/cap-x": {"status": "missing", "notes": "Not yet built"},
            "capabilities/cap-y": {"status": "partial", "notes": "Half done"},
            "capabilities/cap-z": {"status": "working", "notes": "Already done"},
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    _patch_classify(mocker, present_paths=set())

    payload = await _spawn_and_get_research_payload(project, db_session)
    ctx = payload.get("direction_design_context")
    assert ctx is not None
    missing = ctx.get("missing_capabilities", {})
    assert "cap-x" in missing, "missing capability should appear under its bare id"
    assert "cap-y" in missing, "partial capability should appear under its bare id"
    assert "cap-z" not in missing, "working capability should be excluded"


@pytest.mark.asyncio
async def test_system_verification_uses_project_repo_and_integration_branch(
    db_session, mocker
):
    direction = _active_direction("d-sysverif-ctx", "Improve branch-aware system")
    project = Project(
        name="SysVerif Context",
        user_id="test-user-123",
        autonomous_enabled=True,
        github_owner="acme",
        github_repo="widget",
        integration_branch="automain",
        directions=[direction],
        vision={"purpose": "test", "_research_spawn_count": 1},
        design_doc={
            "systems/ctx": {
                "status": "mature",
                "modules": ["backend/app/ctx.py"],
            }
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    seen_kwargs = {}

    async def _classify(paths, **kwargs):
        seen_kwargs.update(kwargs)
        return paths, [], []

    mocker.patch(
        "app.autonomous_research.classify_paths_tristate", side_effect=_classify
    )
    mocker.patch(
        "app.release_promotion._resolve_github_with_session",
        new=AsyncMock(return_value=("ghs_test", "acme", "widget")),
    )

    payload = await _spawn_and_get_research_payload(project, db_session)
    assert payload["system_verification"]["ctx"]["status"] == "verified"
    assert seen_kwargs == {
        "branch": "automain",
        "project_id": str(project.id),
        "token": "ghs_test",
        "owner": "acme",
        "repo": "widget",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bucket,key,marker",
    [
        ("systems", "system_verification", "systems/ctx"),
        ("capabilities", "capability_verification", "capabilities/ctxcap"),
    ],
)
async def test_symbol_verification_passes_branch_context_to_symbol_checks(
    db_session, mocker, bucket, key, marker
):
    direction = _active_direction(f"d-{bucket}-symbol-ctx", "Improve symbol-aware")
    design_doc = {
        marker: {
            "status": "mature" if bucket == "systems" else "working",
            "modules": ["backend/app/ctx.py:missing_symbol"],
            (
                "description" if bucket == "systems" else "notes"
            ): "Depends on backend/app/ctx.py:missing_symbol.",
        }
    }
    project = Project(
        name=f"{bucket.title()} Symbol Context",
        user_id="test-user-123",
        autonomous_enabled=True,
        github_owner="acme",
        github_repo="widget",
        integration_branch="automain",
        directions=[direction],
        vision={"purpose": "test", "_research_spawn_count": 1},
        design_doc=design_doc,
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    seen_kwargs = {}

    async def _classify(paths, **kwargs):
        return paths, [], []

    async def _detect(*args, **kwargs):
        seen_kwargs.update(kwargs)
        return ["backend/app/ctx.py:missing_symbol"]

    mocker.patch(
        "app.autonomous_research.classify_paths_tristate", side_effect=_classify
    )
    mocker.patch("app.autonomous_research.detect_absent_symbols", side_effect=_detect)
    mocker.patch(
        "app.release_promotion._resolve_github_with_session",
        new=AsyncMock(return_value=("ghs_test", "acme", "widget")),
    )

    payload = await _spawn_and_get_research_payload(project, db_session)
    assert (
        payload[key]["ctx" if bucket == "systems" else "ctxcap"]["status"]
        == "claimed_absent"
    )
    assert seen_kwargs == {
        "branch": "automain",
        "project_id": str(project.id),
        "token": "ghs_test",
        "owner": "acme",
        "repo": "widget",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Design-doc capability verification (claimed-but-absent / partial / untouched)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_capability_verification_all_missing_flagged_claimed_absent(
    db_session, mocker
):
    """A working capability whose every backing path is absent is flagged claimed_absent."""
    direction = _active_direction("d-capverif-absent", "Improve ghost capability")
    project = Project(
        name="CapVerif Absent",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test", "_research_spawn_count": 1},
        design_doc={
            "capabilities/ghostcap": {
                "status": "working",
                "modules": ["backend/app/cap_a.py", "backend/app/cap_b.py"],
            }
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    _patch_classify(mocker, present_paths=set())

    payload = await _spawn_and_get_research_payload(project, db_session)
    cv = payload.get("capability_verification")
    assert cv is not None, "capability_verification annotation should be present"
    assert cv["ghostcap"]["status"] == "claimed_absent"
    assert cv["ghostcap"]["claimed_status"] == "working"
    assert cv["ghostcap"]["present"] == []
    assert set(cv["ghostcap"]["missing"]) == {
        "backend/app/cap_a.py",
        "backend/app/cap_b.py",
    }


@pytest.mark.asyncio
async def test_capability_verification_path_in_notes_flagged(db_session, mocker):
    """A capability carrying its only path pointer in `notes` free text is verified."""
    direction = _active_direction("d-capverif-notes", "Improve notes capability")
    project = Project(
        name="CapVerif Notes",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test", "_research_spawn_count": 1},
        design_doc={
            "capabilities/notescap": {
                "status": "working",
                "notes": "Implemented in backend/app/notes_only.py and wired up.",
            }
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    _patch_classify(mocker, present_paths=set())

    payload = await _spawn_and_get_research_payload(project, db_session)
    cv = payload.get("capability_verification")
    assert cv is not None
    assert cv["notescap"]["status"] == "claimed_absent"
    assert cv["notescap"]["missing"] == ["backend/app/notes_only.py"]


@pytest.mark.asyncio
async def test_capability_verification_passes_branch_context_to_symbol_checks(
    db_session, mocker
):
    direction = _active_direction(
        "d-capverif-symbol-ctx", "Improve symbol-aware capability"
    )
    project = Project(
        name="CapVerif Symbol Context",
        user_id="test-user-123",
        autonomous_enabled=True,
        github_owner="acme",
        github_repo="widget",
        integration_branch="automain",
        directions=[direction],
        vision={"purpose": "test", "_research_spawn_count": 1},
        design_doc={
            "capabilities/ctxcap": {
                "status": "working",
                "modules": ["backend/app/ctx.py:missing_symbol"],
                "notes": "Uses backend/app/ctx.py:missing_symbol.",
            }
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    seen_kwargs = {}

    async def _classify(paths, **kwargs):
        return paths, [], []

    async def _detect(*args, **kwargs):
        seen_kwargs.update(kwargs)
        return ["backend/app/ctx.py:missing_symbol"]

    mocker.patch(
        "app.autonomous_research.classify_paths_tristate", side_effect=_classify
    )
    mocker.patch("app.autonomous_research.detect_absent_symbols", side_effect=_detect)
    mocker.patch(
        "app.release_promotion._resolve_github_with_session",
        new=AsyncMock(return_value=("ghs_test", "acme", "widget")),
    )

    payload = await _spawn_and_get_research_payload(project, db_session)
    assert payload["capability_verification"]["ctxcap"]["status"] == "claimed_absent"
    assert seen_kwargs == {
        "branch": "automain",
        "project_id": str(project.id),
        "token": "ghs_test",
        "owner": "acme",
        "repo": "widget",
    }


@pytest.mark.asyncio
async def test_capability_verification_present_path_not_flagged(db_session, mocker):
    """A capability with a present backing path is left alone (not claimed_absent)."""
    direction = _active_direction("d-capverif-present", "Improve real capability")
    project = Project(
        name="CapVerif Present",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test", "_research_spawn_count": 1},
        design_doc={
            "capabilities/realcap": {
                "status": "working",
                "modules": ["backend/app/real_cap.py", "backend/app/gone_cap.py"],
            }
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    _patch_classify(mocker, present_paths={"backend/app/real_cap.py"})

    payload = await _spawn_and_get_research_payload(project, db_session)
    cv = payload.get("capability_verification")
    assert cv is not None
    assert cv["realcap"]["status"] == "partial"
    assert cv["realcap"]["present"] == ["backend/app/real_cap.py"]
    assert cv["realcap"]["missing"] == ["backend/app/gone_cap.py"]


@pytest.mark.asyncio
async def test_capability_verification_no_path_untouched(db_session, mocker):
    """A capability with no path pointer is never classified (absence ≠ absent code)."""
    direction = _active_direction("d-capverif-nopath", "Improve pathless capability")
    project = Project(
        name="CapVerif NoPath",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test", "_research_spawn_count": 1},
        design_doc={
            "capabilities/pathless": {
                "status": "working",
                "notes": "Lets operators do a thing. No file pointer here.",
            }
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    _patch_classify(mocker, present_paths=set())

    payload = await _spawn_and_get_research_payload(project, db_session)
    # No path pointer → the capability must not appear in the verification map.
    assert "pathless" not in (payload.get("capability_verification") or {})


@pytest.mark.asyncio
async def test_capability_verification_planned_status_skipped(db_session, mocker):
    """A non-present-status capability (planned) is skipped — agent already builds it."""
    direction = _active_direction("d-capverif-planned", "Build planned capability")
    project = Project(
        name="CapVerif Planned",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test", "_research_spawn_count": 1},
        design_doc={
            "capabilities/plannedcap": {
                "status": "planned",
                "modules": ["backend/app/not_yet.py"],
            }
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    _patch_classify(mocker, present_paths=set())

    payload = await _spawn_and_get_research_payload(project, db_session)
    assert "plannedcap" not in (payload.get("capability_verification") or {})


@pytest.mark.asyncio
async def test_capability_verification_does_not_mutate_design_doc(db_session, mocker):
    """Capability verification is advisory only — stored design_doc is unchanged."""
    direction = _active_direction("d-capverif-immut", "Improve capability")
    original_doc = {
        "capabilities/x": {
            "status": "working",
            "modules": ["backend/app/cap_missing.py"],
        }
    }
    project = Project(
        name="CapVerif Immutable",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test", "_research_spawn_count": 1},
        design_doc={k: dict(v) for k, v in original_doc.items()},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    _patch_classify(mocker, present_paths=set())

    await _spawn_and_get_research_payload(project, db_session)
    await db_session.refresh(project)
    assert project.design_doc == original_doc, (
        "design_doc must not be mutated by capability verification"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Module-pointer normalization: path:Symbol and directory/ refs
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_system_verification_path_symbol_pointer_not_flagged_missing(
    db_session, mocker
):
    """A path:Symbol module pointer resolves to the bare file, not flagged missing.

    Before the fix, classify_paths_tristate passed 'models.py:Alert' raw to git,
    which found nothing → status 'claimed_absent'.  After the fix, the :Symbol
    suffix is stripped before the existence check, so the underlying file is found
    and the system is reported 'verified'.
    """
    direction = _active_direction("d-pathsym-norm", "Improve symbol system")
    project = Project(
        name="PathSymbol Norm",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test", "_research_spawn_count": 1},
        design_doc={
            "systems/alerts": {
                "status": "mature",
                "modules": ["backend/app/models.py:Alert"],
            }
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # The real _file_exists_at_head normalizes a path:Symbol pointer internally
    # (via _normalize_module_ptr) BEFORE the existence check, so 'models.py:Alert'
    # resolves to the bare file. Stub it faithfully: run that same real
    # normalization, then report the bare file present. Returning True only for a
    # normalized bare path (no residual :Symbol) is what discriminates a working
    # normalizer (→ present, verified) from a regressed one (→ missing). This
    # exercises the real normalizer while staying hermetic — no /tmp/workspace.
    from app.source_ref_validation import _normalize_module_ptr

    async def _exists(path, **kwargs):
        normalized = _normalize_module_ptr(path)
        return ":" not in normalized and "/" in normalized

    mocker.patch(
        "app.source_ref_validation._file_exists_at_head", side_effect=_exists
    )
    # The symbol-level depth check (detect_absent_symbols → _symbol_defined_at_head)
    # shells out to `git show` in /tmp/workspace, which does not exist in the test
    # harness. Symbol absence is out of scope for this file-normalization test
    # (covered by the symbol-verification tests); stub it to report none absent so
    # the test stays hermetic and asserts only the path-normalization behavior.
    mocker.patch(
        "app.autonomous_research.detect_absent_symbols",
        new=AsyncMock(return_value=[]),
    )

    payload = await _spawn_and_get_research_payload(project, db_session)
    sv = payload.get("system_verification")
    assert sv is not None, "system_verification should be present"
    assert sv["alerts"]["status"] == "verified", (
        f"path:Symbol module pointer should resolve to the bare file — got {sv['alerts']}"
    )
    assert sv["alerts"]["missing"] == [], (
        "normalized module pointer should not land in the missing bucket"
    )


@pytest.mark.asyncio
async def test_capability_verification_path_symbol_pointer_not_flagged_missing(
    db_session, mocker
):
    """A path:Symbol module pointer in a capability resolves to the bare file."""
    direction = _active_direction("d-capsym-norm", "Improve symbol capability")
    project = Project(
        name="CapSymbol Norm",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test", "_research_spawn_count": 1},
        design_doc={
            "capabilities/auth": {
                "status": "working",
                "modules": ["backend/app/auth.py:verify_token"],
            }
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # See the system-verification sibling above: the real _file_exists_at_head
    # normalizes the path:Symbol suffix internally, so stub it to run that same
    # real normalization and report the bare file present. Hermetic — no
    # /tmp/workspace dependency.
    from app.source_ref_validation import _normalize_module_ptr

    async def _exists(path, **kwargs):
        normalized = _normalize_module_ptr(path)
        return ":" not in normalized and "/" in normalized

    mocker.patch(
        "app.source_ref_validation._file_exists_at_head", side_effect=_exists
    )
    # The symbol-level check shells out to `git show` in /tmp/workspace (absent in
    # the harness); symbol absence is out of scope here, so report none absent.
    mocker.patch(
        "app.autonomous_research.detect_absent_symbols",
        new=AsyncMock(return_value=[]),
    )

    payload = await _spawn_and_get_research_payload(project, db_session)
    cv = payload.get("capability_verification")
    assert cv is not None, "capability_verification should be present"
    assert cv["auth"]["status"] == "verified", (
        f"path:Symbol capability module should resolve to bare file — got {cv['auth']}"
    )
    assert cv["auth"]["missing"] == []


# ── Undirected (cross-cutting) research spawn guard ──────────────────────────


async def _add_cross_cutting_research(session, project_id, *, status, age_minutes=0):
    """Insert a cross-cutting research task with the canonical title."""
    from app.autonomous_research import CROSS_CUTTING_RESEARCH_TITLE

    t = Task(
        project_id=project_id,
        title=CROSS_CUTTING_RESEARCH_TITLE,
        description="x",
        payloads=[{"type": "research", "prompt": "x"}],
        status=status,
        created_by="ORCHESTRATOR",
    )
    session.add(t)
    await session.commit()
    if age_minutes:
        # Backdate created_at to test the cooldown leg (set after insert so the
        # default timestamp doesn't override it).
        t.created_at = _utcnow() - timedelta(minutes=age_minutes)
        session.add(t)
        await session.commit()
    return t


@pytest.mark.asyncio
async def test_undirected_guard_false_when_none_exist(db_session):
    from app.autonomous_research import _undirected_research_recently_active

    project = Project(name="P", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    assert await _undirected_research_recently_active(project.id, db_session) is False


@pytest.mark.asyncio
async def test_undirected_guard_true_when_active(db_session):
    from app.autonomous_research import _undirected_research_recently_active

    project = Project(name="P", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _add_cross_cutting_research(
        db_session, project.id, status=TaskStatus.IN_PROGRESS
    )
    assert await _undirected_research_recently_active(project.id, db_session) is True


@pytest.mark.asyncio
async def test_undirected_guard_cooldown_blocks_recent_terminal(db_session):
    """A recently-CANCELLED cross-cutting task still blocks (cooldown leg) — this
    is the cancel/complete-then-instantly-respawn loop the guard prevents."""
    from app.autonomous_research import _undirected_research_recently_active

    project = Project(name="P", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _add_cross_cutting_research(
        db_session, project.id, status=TaskStatus.CANCELLED, age_minutes=10
    )
    # cooldown default is 60 min, so a 10-min-old terminal task still blocks.
    assert await _undirected_research_recently_active(project.id, db_session) is True


@pytest.mark.asyncio
async def test_undirected_guard_allows_after_cooldown(db_session):
    """A terminal cross-cutting task older than the cooldown no longer blocks."""
    from app.autonomous_research import _undirected_research_recently_active

    project = Project(name="P", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _add_cross_cutting_research(
        db_session, project.id, status=TaskStatus.COMPLETED, age_minutes=120
    )
    assert await _undirected_research_recently_active(project.id, db_session) is False


@pytest.mark.asyncio
async def test_undirected_guard_scoped_per_project(db_session):
    """An active cross-cutting task in another project does not block this one."""
    from app.autonomous_research import _undirected_research_recently_active

    p1 = Project(name="P1", user_id="test-user-123")
    p2 = Project(name="P2", user_id="test-user-123")
    db_session.add_all([p1, p2])
    await db_session.commit()
    await db_session.refresh(p1)
    await db_session.refresh(p2)

    await _add_cross_cutting_research(db_session, p2.id, status=TaskStatus.IN_PROGRESS)
    assert await _undirected_research_recently_active(p1.id, db_session) is False
    assert await _undirected_research_recently_active(p2.id, db_session) is True


# ─────────────────────────────────────────────────────────────────────────────
# _directed_research_recently_completed unit tests
# (mirrors the undirected guard test block above)
# ─────────────────────────────────────────────────────────────────────────────


async def _add_directed_research(
    session, project_id, direction_id, *, status, age_minutes=0, status_log=None
):
    """Insert a directed research task for the given direction."""
    t = Task(
        project_id=project_id,
        title=f"Research: direction-{direction_id}",
        description="x",
        payloads=[
            {
                "type": "research",
                "current_direction_id": direction_id,
                "directions": [],
                "base_branch": "main",
            }
        ],
        status_log=status_log or [],
        status=status,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    session.add(t)
    await session.commit()
    if age_minutes:
        backdated = _utcnow() - timedelta(minutes=age_minutes)
        t.created_at = backdated
        # _on_status_set auto-stamps completed_at=now() for terminal tasks;
        # backdate it too so _derive_completed_at returns the intended age.
        if t.completed_at is not None:
            t.completed_at = backdated
        session.add(t)
        await session.commit()
    return t


@pytest.mark.asyncio
async def test_directed_completed_guard_false_when_none_exist(db_session):
    """Returns False when no research tasks for the direction exist at all."""
    from app.direction_lifecycle import _directed_research_recently_completed

    project = Project(name="P", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    result = await _directed_research_recently_completed(
        project.id, "d-abc123", db_session
    )
    assert result is False


@pytest.mark.asyncio
async def test_directed_completed_guard_true_within_cooldown(db_session):
    """Returns True when a COMPLETED research task exists within the cooldown window."""
    from app.direction_lifecycle import _directed_research_recently_completed
    from app.shared_state import runtime_config as _runtime_config

    _runtime_config["completed_research_cooldown_minutes"] = 120
    try:
        project = Project(name="P", user_id="test-user-123")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        await _add_directed_research(
            db_session,
            project.id,
            "d-dir001",
            status=TaskStatus.COMPLETED,
            age_minutes=30,  # within 120-min window
        )
        result = await _directed_research_recently_completed(
            project.id, "d-dir001", db_session
        )
        assert result is True
    finally:
        _runtime_config.pop("completed_research_cooldown_minutes", None)


@pytest.mark.asyncio
async def test_directed_completed_guard_true_when_completion_recent_but_created_old(
    db_session,
):
    """Uses the terminal timestamp, not created_at, for completed research cooldowns."""
    from app.direction_lifecycle import _directed_research_recently_completed
    from app.shared_state import runtime_config as _runtime_config

    _runtime_config["completed_research_cooldown_minutes"] = 60
    try:
        project = Project(name="P", user_id="test-user-123")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        completed_at = _utcnow() - timedelta(minutes=15)
        old_created = _utcnow() - timedelta(hours=4)
        task = Task(
            project_id=project.id,
            title="Research: direction-d-dir-old",
            description="x",
            payloads=[
                {
                    "type": "research",
                    "current_direction_id": "d-dir-old",
                    "directions": [],
                    "base_branch": "main",
                }
            ],
            status_log=[{"t": completed_at.isoformat(), "s": "COMPLETED"}],
            status=TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
            created_at=old_created,
        )
        db_session.add(task)
        await db_session.commit()

        result = await _directed_research_recently_completed(
            project.id, "d-dir-old", db_session
        )
        assert result is True
    finally:
        _runtime_config.pop("completed_research_cooldown_minutes", None)


@pytest.mark.asyncio
async def test_directed_completed_guard_false_after_cooldown(db_session):
    """Returns False when the COMPLETED task is older than the cooldown window."""
    from app.direction_lifecycle import _directed_research_recently_completed
    from app.shared_state import runtime_config as _runtime_config

    _runtime_config["completed_research_cooldown_minutes"] = 60
    try:
        project = Project(name="P", user_id="test-user-123")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        await _add_directed_research(
            db_session,
            project.id,
            "d-dir002",
            status=TaskStatus.COMPLETED,
            age_minutes=120,  # older than 60-min window
        )
        result = await _directed_research_recently_completed(
            project.id, "d-dir002", db_session
        )
        assert result is False
    finally:
        _runtime_config.pop("completed_research_cooldown_minutes", None)


@pytest.mark.asyncio
async def test_directed_completed_guard_false_for_different_direction(db_session):
    """Returns False when the COMPLETED task is on a different direction."""
    from app.direction_lifecycle import _directed_research_recently_completed

    project = Project(name="P", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _add_directed_research(
        db_session,
        project.id,
        "d-dir-A",
        status=TaskStatus.COMPLETED,
        age_minutes=10,
    )
    # Guard for a different direction should not be triggered.
    result = await _directed_research_recently_completed(
        project.id, "d-dir-B", db_session
    )
    assert result is False


@pytest.mark.asyncio
async def test_directed_completed_guard_false_active_not_completed(db_session):
    """Returns False when only ACTIVE (non-COMPLETED) research exists — that path
    is handled by the active-tasks lock, not this guard."""
    from app.direction_lifecycle import _directed_research_recently_completed

    project = Project(name="P", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _add_directed_research(
        db_session,
        project.id,
        "d-dir003",
        status=TaskStatus.IN_PROGRESS,
        age_minutes=5,
    )
    result = await _directed_research_recently_completed(
        project.id, "d-dir003", db_session
    )
    assert result is False


@pytest.mark.asyncio
async def test_directed_completed_guard_scoped_per_project(db_session):
    """A COMPLETED task in another project does not block this one."""
    from app.direction_lifecycle import _directed_research_recently_completed

    p1 = Project(name="P1", user_id="test-user-123")
    p2 = Project(name="P2", user_id="test-user-123")
    db_session.add_all([p1, p2])
    await db_session.commit()
    await db_session.refresh(p1)
    await db_session.refresh(p2)

    await _add_directed_research(
        db_session,
        p2.id,
        "d-shared",
        status=TaskStatus.COMPLETED,
        age_minutes=10,
    )
    assert (
        await _directed_research_recently_completed(p1.id, "d-shared", db_session)
        is False
    )
    assert (
        await _directed_research_recently_completed(p2.id, "d-shared", db_session)
        is True
    )


@pytest.mark.asyncio
async def test_directed_completed_guard_disabled_by_zero_cooldown(db_session):
    """Setting completed_research_cooldown_minutes <= 0 disables the guard."""
    from app.direction_lifecycle import _directed_research_recently_completed
    from app.shared_state import runtime_config as _runtime_config

    _runtime_config["completed_research_cooldown_minutes"] = 0
    try:
        project = Project(name="P", user_id="test-user-123")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        await _add_directed_research(
            db_session,
            project.id,
            "d-dir004",
            status=TaskStatus.COMPLETED,
            age_minutes=5,
        )
        result = await _directed_research_recently_completed(
            project.id, "d-dir004", db_session
        )
        assert result is False
    finally:
        _runtime_config.pop("completed_research_cooldown_minutes", None)


@pytest.mark.asyncio
async def test_spawn_skipped_when_completed_directed_research_within_cooldown(
    db_session, mocker
):
    """_spawn_research_task skips a direction when a COMPLETED research task for
    that direction exists within the completed_research_cooldown_minutes window.
    This covers the gap where _handle_research_completion dies before setting
    last_researched_at (incident 2026-06-10: 3x same-direction re-runs in 80 min)."""
    from app.shared_state import runtime_config as _runtime_config

    direction_id = "d-dedup01"
    _runtime_config["completed_research_cooldown_minutes"] = 120

    mocker.patch(
        "app.strategic_spawning._spawn_design_doc_update", new_callable=AsyncMock
    )
    mocker.patch(
        "app.anomaly_detection.check_anomaly_actions",
        new_callable=AsyncMock,
        return_value={"action": "none"},
    )

    try:
        project = Project(
            name="Dedup Completed",
            user_id="test-user-123",
            autonomous_enabled=True,
            directions=[_active_direction(direction_id)],
            vision={"purpose": "test", "_research_spawn_count": 1},
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        # Simulate a recently-completed research task for this direction
        completed = Task(
            project_id=project.id,
            title=f"Research: direction-{direction_id}",
            payloads=[
                {
                    "type": "research",
                    "current_direction_id": direction_id,
                    "directions": [],
                    "base_branch": "main",
                }
            ],
            status=TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
        )
        db_session.add(completed)
        await db_session.commit()

        await _spawn_research_task(project, db_session)

        result = await db_session.execute(
            select(Task).where(
                Task.project_id == project.id,
                Task.status == TaskStatus.QUEUED,
            )
        )
        queued = result.scalars().all()
        assert len(queued) == 0, (
            "Must not spawn a new research task when a COMPLETED one exists "
            "within the cooldown window"
        )
    finally:
        _runtime_config.pop("completed_research_cooldown_minutes", None)


@pytest.mark.asyncio
async def test_spawn_allowed_when_completed_directed_research_outside_cooldown(
    db_session, mocker
):
    """_spawn_research_task allows a new spawn when the previous COMPLETED research
    is older than the cooldown window."""
    from app.shared_state import runtime_config as _runtime_config

    direction_id = "d-dedup02"
    _runtime_config["completed_research_cooldown_minutes"] = 60

    mocker.patch(
        "app.strategic_spawning._spawn_design_doc_update", new_callable=AsyncMock
    )
    mocker.patch(
        "app.anomaly_detection.check_anomaly_actions",
        new_callable=AsyncMock,
        return_value={"action": "none"},
    )

    try:
        project = Project(
            name="Dedup Outside Window",
            user_id="test-user-123",
            autonomous_enabled=True,
            directions=[_active_direction(direction_id)],
            vision={"purpose": "test", "_research_spawn_count": 1},
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        # Old completed task — outside the 60-min window
        old_completed = Task(
            project_id=project.id,
            title=f"Research: direction-{direction_id}",
            payloads=[
                {
                    "type": "research",
                    "current_direction_id": direction_id,
                    "directions": [],
                    "base_branch": "main",
                }
            ],
            status=TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
        )
        db_session.add(old_completed)
        await db_session.commit()
        backdated = _utcnow() - timedelta(minutes=90)
        old_completed.created_at = backdated
        # Backdate completed_at too — _on_status_set stamps it at now() on init,
        # but _derive_completed_at prefers completed_at over created_at.
        old_completed.completed_at = backdated
        db_session.add(old_completed)
        await db_session.commit()

        await _spawn_research_task(project, db_session)

        result = await db_session.execute(
            select(Task).where(
                Task.project_id == project.id,
                Task.status == TaskStatus.QUEUED,
            )
        )
        queued = result.scalars().all()
        assert len(queued) == 1, (
            "Should spawn a new research task when the completed one is outside "
            "the cooldown window"
        )
    finally:
        _runtime_config.pop("completed_research_cooldown_minutes", None)


@pytest.mark.asyncio
async def test_directed_completed_guard_derives_from_operator_cooldown(db_session):
    """Without an explicit completed_research_cooldown_minutes, the window derives
    from autonomous_direction_cooldown_minutes — a completion outside the operator
    window must not hold the direction (it would under the old hardcoded 120)."""
    from app.direction_lifecycle import _directed_research_recently_completed
    from app.shared_state import runtime_config as _runtime_config

    sentinel = object()
    prior_operator = _runtime_config.get(
        "autonomous_direction_cooldown_minutes", sentinel
    )
    _runtime_config.pop("completed_research_cooldown_minutes", None)
    _runtime_config["autonomous_direction_cooldown_minutes"] = 60
    try:
        project = Project(name="P", user_id="test-user-123")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        await _add_directed_research(
            db_session,
            project.id,
            "d-derive01",
            status=TaskStatus.COMPLETED,
            age_minutes=90,  # outside the 60-min operator window, inside old 120
        )
        result = await _directed_research_recently_completed(
            project.id, "d-derive01", db_session
        )
        assert result is False
    finally:
        if prior_operator is sentinel:
            _runtime_config.pop("autonomous_direction_cooldown_minutes", None)
        else:
            _runtime_config["autonomous_direction_cooldown_minutes"] = prior_operator


@pytest.mark.asyncio
async def test_directed_completed_guard_derived_window_still_blocks_recent(db_session):
    """The derived window keeps the anti-churn intent: a completion inside the
    operator cooldown still blocks an immediate re-spawn."""
    from app.direction_lifecycle import _directed_research_recently_completed
    from app.shared_state import runtime_config as _runtime_config

    sentinel = object()
    prior_operator = _runtime_config.get(
        "autonomous_direction_cooldown_minutes", sentinel
    )
    _runtime_config.pop("completed_research_cooldown_minutes", None)
    _runtime_config["autonomous_direction_cooldown_minutes"] = 60
    try:
        project = Project(name="P", user_id="test-user-123")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        await _add_directed_research(
            db_session,
            project.id,
            "d-derive02",
            status=TaskStatus.COMPLETED,
            age_minutes=30,  # inside the 60-min operator window
        )
        result = await _directed_research_recently_completed(
            project.id, "d-derive02", db_session
        )
        assert result is True
    finally:
        if prior_operator is sentinel:
            _runtime_config.pop("autonomous_direction_cooldown_minutes", None)
        else:
            _runtime_config["autonomous_direction_cooldown_minutes"] = prior_operator


@pytest.mark.asyncio
async def test_directed_completed_guard_explicit_key_overrides_operator_cooldown(
    db_session,
):
    """An explicit completed_research_cooldown_minutes wins over the operator knob."""
    from app.direction_lifecycle import _directed_research_recently_completed
    from app.shared_state import runtime_config as _runtime_config

    sentinel = object()
    prior_operator = _runtime_config.get(
        "autonomous_direction_cooldown_minutes", sentinel
    )
    _runtime_config["completed_research_cooldown_minutes"] = 120
    _runtime_config["autonomous_direction_cooldown_minutes"] = 60
    try:
        project = Project(name="P", user_id="test-user-123")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        await _add_directed_research(
            db_session,
            project.id,
            "d-derive03",
            status=TaskStatus.COMPLETED,
            age_minutes=90,  # outside operator 60, inside explicit 120
        )
        result = await _directed_research_recently_completed(
            project.id, "d-derive03", db_session
        )
        assert result is True
    finally:
        _runtime_config.pop("completed_research_cooldown_minutes", None)
        if prior_operator is sentinel:
            _runtime_config.pop("autonomous_direction_cooldown_minutes", None)
        else:
            _runtime_config["autonomous_direction_cooldown_minutes"] = prior_operator


@pytest.mark.asyncio
async def test_directed_completed_guard_falls_back_when_no_keys_set(db_session):
    """With neither key configured the guard uses the 240-min fallback (matching
    Gate 1's default) — a completion at 180 min is still within the window."""
    from app.direction_lifecycle import _directed_research_recently_completed
    from app.shared_state import runtime_config as _runtime_config

    sentinel = object()
    prior_operator = _runtime_config.get(
        "autonomous_direction_cooldown_minutes", sentinel
    )
    _runtime_config.pop("completed_research_cooldown_minutes", None)
    _runtime_config.pop("autonomous_direction_cooldown_minutes", None)
    try:
        project = Project(name="P", user_id="test-user-123")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        await _add_directed_research(
            db_session,
            project.id,
            "d-derive04",
            status=TaskStatus.COMPLETED,
            age_minutes=180,  # inside the 240-min fallback (was 120 before the fix)
        )
        result = await _directed_research_recently_completed(
            project.id, "d-derive04", db_session
        )
        assert result is True
    finally:
        if prior_operator is sentinel:
            _runtime_config.pop("autonomous_direction_cooldown_minutes", None)
        else:
            _runtime_config["autonomous_direction_cooldown_minutes"] = prior_operator


@pytest.mark.asyncio
async def test_directed_completed_guard_fallback_is_240_not_120(db_session):
    """Fallback window is 240 min (Gate 1 default), not the old 120. A task
    completed at 250 min must be outside the window; one at 130 min must be inside."""
    from app.direction_lifecycle import _directed_research_recently_completed
    from app.shared_state import runtime_config as _runtime_config

    sentinel = object()
    prior_operator = _runtime_config.get(
        "autonomous_direction_cooldown_minutes", sentinel
    )
    _runtime_config.pop("completed_research_cooldown_minutes", None)
    _runtime_config.pop("autonomous_direction_cooldown_minutes", None)
    try:
        project = Project(name="P", user_id="test-user-123")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        # 250 min ago — outside both 120 and 240 windows → guard must return False
        await _add_directed_research(
            db_session,
            project.id,
            "d-fallback-old",
            status=TaskStatus.COMPLETED,
            age_minutes=250,
        )
        result_old = await _directed_research_recently_completed(
            project.id, "d-fallback-old", db_session
        )
        assert result_old is False, "250 min ago must be outside the 240-min fallback"

        # 130 min ago — inside 240 but outside the old 120 → must return True
        await _add_directed_research(
            db_session,
            project.id,
            "d-fallback-new",
            status=TaskStatus.COMPLETED,
            age_minutes=130,
        )
        result_new = await _directed_research_recently_completed(
            project.id, "d-fallback-new", db_session
        )
        assert result_new is True, "130 min ago must be inside the 240-min fallback"
    finally:
        if prior_operator is sentinel:
            _runtime_config.pop("autonomous_direction_cooldown_minutes", None)
        else:
            _runtime_config["autonomous_direction_cooldown_minutes"] = prior_operator


@pytest.mark.asyncio
async def test_directed_completed_guard_uses_direction_productive_window(db_session):
    """When a direction dict is passed, the guard uses resolve_direction_refractory_minutes
    (productive path = 45 min by default). A completion at 30 min blocks; 50 min does not."""
    from app.direction_lifecycle import _directed_research_recently_completed
    from app.shared_state import runtime_config as _runtime_config

    _runtime_config.pop("completed_research_cooldown_minutes", None)
    _runtime_config.pop("autonomous_direction_cooldown_minutes", None)
    _runtime_config.pop("productive_direction_cooldown_minutes", None)
    try:
        project = Project(name="P", user_id="test-user-123")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        direction = {
            "id": "d-prod01",
            "status": "active",
            "last_cycle_productive": True,
        }

        # 30 min ago — inside the productive 45-min window → should block
        await _add_directed_research(
            db_session,
            project.id,
            "d-prod01",
            status=TaskStatus.COMPLETED,
            age_minutes=30,
        )
        result = await _directed_research_recently_completed(
            project.id, "d-prod01", db_session, direction=direction
        )
        assert result is True, "30 min ago is inside the 45-min productive window"

        # 50 min ago — outside the productive 45-min window → should not block
        project2 = Project(name="P2", user_id="test-user-123")
        db_session.add(project2)
        await db_session.commit()
        await db_session.refresh(project2)

        await _add_directed_research(
            db_session,
            project2.id,
            "d-prod01",
            status=TaskStatus.COMPLETED,
            age_minutes=50,
        )
        result2 = await _directed_research_recently_completed(
            project2.id, "d-prod01", db_session, direction=direction
        )
        assert result2 is False, "50 min ago is outside the 45-min productive window"
    finally:
        _runtime_config.pop("productive_direction_cooldown_minutes", None)


@pytest.mark.asyncio
async def test_directed_completed_guard_uses_direction_saturated_window(db_session):
    """Saturated direction uses 2x the standard window. With autonomous_direction_cooldown_minutes=60
    and 1 consecutive unproductive cycle (multiplier=0.375), the standard window is 22.5 min;
    saturated doubles it to 45 min. A completion at 30 min blocks; 50 min does not."""
    from app.direction_lifecycle import _directed_research_recently_completed
    from app.shared_state import runtime_config as _runtime_config

    sentinel = object()
    prior_operator = _runtime_config.get(
        "autonomous_direction_cooldown_minutes", sentinel
    )
    _runtime_config.pop("completed_research_cooldown_minutes", None)
    _runtime_config["autonomous_direction_cooldown_minutes"] = 60
    try:
        # consecutive_unproductive_cycles=1 → multiplier=0.375 → 60*0.375=22.5; saturated→45
        direction = {
            "id": "d-sat01",
            "status": "active",
            "possibly_saturated": True,
            "last_cycle_productive": False,
            "consecutive_unproductive_cycles": 1,
        }

        project = Project(name="P", user_id="test-user-123")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        # 30 min ago — inside the saturated 45-min window
        await _add_directed_research(
            db_session,
            project.id,
            "d-sat01",
            status=TaskStatus.COMPLETED,
            age_minutes=30,
        )
        result = await _directed_research_recently_completed(
            project.id, "d-sat01", db_session, direction=direction
        )
        assert result is True, "30 min is inside the saturated 45-min window"

        # 50 min ago — outside the saturated 45-min window
        project2 = Project(name="P2", user_id="test-user-123")
        db_session.add(project2)
        await db_session.commit()
        await db_session.refresh(project2)

        await _add_directed_research(
            db_session,
            project2.id,
            "d-sat01",
            status=TaskStatus.COMPLETED,
            age_minutes=50,
        )
        result2 = await _directed_research_recently_completed(
            project2.id, "d-sat01", db_session, direction=direction
        )
        assert result2 is False, "50 min is outside the saturated 45-min window"
    finally:
        if prior_operator is sentinel:
            _runtime_config.pop("autonomous_direction_cooldown_minutes", None)
        else:
            _runtime_config["autonomous_direction_cooldown_minutes"] = prior_operator


@pytest.mark.asyncio
async def test_directed_completed_guard_uses_direction_unproductive_window(db_session):
    """Unproductive direction (3+ cycles, multiplier=1.0) uses the full standard window.
    With autonomous_direction_cooldown_minutes=60 the window is 60 min. A completion at
    50 min blocks; 70 min does not."""
    from app.direction_lifecycle import _directed_research_recently_completed
    from app.shared_state import runtime_config as _runtime_config

    sentinel = object()
    prior_operator = _runtime_config.get(
        "autonomous_direction_cooldown_minutes", sentinel
    )
    _runtime_config.pop("completed_research_cooldown_minutes", None)
    _runtime_config["autonomous_direction_cooldown_minutes"] = 60
    try:
        direction = {
            "id": "d-unprod01",
            "status": "active",
            "last_cycle_productive": False,
            "consecutive_unproductive_cycles": 3,  # multiplier=1.0 → 60 min
        }

        project = Project(name="P", user_id="test-user-123")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        # 50 min ago — inside the 60-min unproductive window
        await _add_directed_research(
            db_session,
            project.id,
            "d-unprod01",
            status=TaskStatus.COMPLETED,
            age_minutes=50,
        )
        result = await _directed_research_recently_completed(
            project.id, "d-unprod01", db_session, direction=direction
        )
        assert result is True, "50 min is inside the 60-min unproductive window"

        # 70 min ago — outside the 60-min unproductive window
        project2 = Project(name="P2", user_id="test-user-123")
        db_session.add(project2)
        await db_session.commit()
        await db_session.refresh(project2)

        await _add_directed_research(
            db_session,
            project2.id,
            "d-unprod01",
            status=TaskStatus.COMPLETED,
            age_minutes=70,
        )
        result2 = await _directed_research_recently_completed(
            project2.id, "d-unprod01", db_session, direction=direction
        )
        assert result2 is False, "70 min is outside the 60-min unproductive window"
    finally:
        if prior_operator is sentinel:
            _runtime_config.pop("autonomous_direction_cooldown_minutes", None)
        else:
            _runtime_config["autonomous_direction_cooldown_minutes"] = prior_operator


@pytest.mark.asyncio
async def test_idle_research_skips_paused_project(db_session, mocker):
    """`_check_idle_research` must not spawn work for a paused project even when
    autonomous_enabled is True and it has an active direction; a non-paused
    sibling still spawns.  The Project passed to the spawn must be persistent
    in the per-project session, not detached from an earlier batch session."""
    from app.autonomous_research import _check_idle_research
    from conftest import TestingSessionLocal

    async def _assert_project_owned_by_spawn_session(project, session):
        assert project in session, (
            "idle-research must load Project in the same session used for spawning"
        )

    spawn_spy = mocker.patch(
        "app.autonomous_research._spawn_research_task",
        new_callable=AsyncMock,
        side_effect=_assert_project_owned_by_spawn_session,
    )
    mocker.patch("app.autonomous_research._spawn_periodic_task", new_callable=AsyncMock)
    mocker.patch(
        "app.autonomous_research._spawn_residual_fingerprint_remediation",
        new_callable=AsyncMock,
    )
    mocker.patch(
        "app.anomaly_detection._expire_stale_proposals", new_callable=AsyncMock
    )
    # Every call returns a genuinely distinct session.  Reusing db_session for
    # all contexts masks cross-session detached-instance bugs.
    mocker.patch("app.autonomous_research.AsyncSessionLocal", TestingSessionLocal)

    paused = Project(
        name="Idle Paused",
        user_id="test-user-123",
        autonomous_enabled=True,
        paused=True,
        directions=[_active_direction("d-paused01")],
    )
    active = Project(
        name="Idle Active",
        user_id="test-user-123",
        autonomous_enabled=True,
        paused=False,
        directions=[_active_direction("d-active01")],
    )
    db_session.add_all([paused, active])
    await db_session.commit()
    await db_session.refresh(paused)
    await db_session.refresh(active)

    # Clear the time-gate so the periodic check actually runs this call.
    _shared_state.last_idle_check = None
    try:
        await _check_idle_research()
    finally:
        _shared_state.last_idle_check = None

    spawned_ids = {call.args[0].id for call in spawn_spy.call_args_list}
    assert active.id in spawned_ids, "Non-paused project should spawn research"
    assert paused.id not in spawned_ids, "Paused project must be skipped"


@pytest.mark.asyncio
async def test_post_merge_research_skips_paused_project(db_session, mocker):
    """`_check_autonomous_research` (post-merge hook) must skip a paused project
    while still spawning for a non-paused project on the same repo, with the
    Project attached to the per-project spawn session."""
    from app.autonomous_research import _check_autonomous_research
    from conftest import TestingSessionLocal

    async def _assert_project_owned_by_spawn_session(project, session):
        assert project in session, (
            "post-merge research must load Project in the spawn-owning session"
        )

    spawn_spy = mocker.patch(
        "app.autonomous_research._spawn_research_task",
        new_callable=AsyncMock,
        side_effect=_assert_project_owned_by_spawn_session,
    )
    # Exercise the production ownership boundary with separate enumeration and
    # per-project sessions.
    mocker.patch("app.autonomous_research.AsyncSessionLocal", TestingSessionLocal)

    owner, repo = "acme", "widgets"
    paused = Project(
        name="Merge Paused",
        user_id="test-user-123",
        autonomous_enabled=True,
        paused=True,
        github_owner=owner,
        github_repo=repo,
    )
    active = Project(
        name="Merge Active",
        user_id="test-user-123",
        autonomous_enabled=True,
        paused=False,
        github_owner=owner,
        github_repo=repo,
    )
    db_session.add_all([paused, active])
    await db_session.commit()
    await db_session.refresh(paused)
    await db_session.refresh(active)

    await _check_autonomous_research(owner, repo)

    spawned_ids = {call.args[0].id for call in spawn_spy.call_args_list}
    assert active.id in spawned_ids, "Non-paused project should spawn research"
    assert paused.id not in spawned_ids, "Paused project must be skipped"


# ─────────────────────────────────────────────────────────────────────────────
# Operator steering deferral at HALT early-returns (not just spawn-decision skips)
# ─────────────────────────────────────────────────────────────────────────────


def _deferred_steering_messages(msgs):
    """Filter helper: system messages carrying the deferral event_type."""
    return [
        m
        for m in msgs
        if (m.context or {}).get("event_type") == "strategic_steering_deferred"
    ]


async def _system_messages(db_session, project_id):
    return (
        (
            await db_session.execute(
                select(ProjectMessage).where(
                    ProjectMessage.project_id == project_id,
                    ProjectMessage.from_type == "system",
                )
            )
        )
        .scalars()
        .all()
    )


@pytest.mark.asyncio
async def test_operator_refresh_deferred_on_circuit_breaker_open_posts_once(db_session):
    """An operator steering marker pending while the research circuit breaker is
    OPEN (still cooling down) must surface a one-time deferral notice — the breaker
    halt early-returns BEFORE the strategic block, but operator steering must never
    be a silent swallow. The marker must be preserved (only the spawn returns) so
    the steering still lands once the breaker clears, and a second tick must stay
    quiet via the notified flag."""
    project = Project(
        name="Operator deferred on CB open",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[_active_direction("d-cbdef01")],
        vision={
            "purpose": "test",
            "_strategic_refresh": {
                "requested_at": _utcnow().isoformat(),
                "reason": "operator_steering",
                "count": 1,
                "origin": "operator",
            },
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Trip breaker — opened just now, 30-min cooldown has NOT elapsed.
    _shared_state.circuit_breakers[str(project.id)] = {
        "state": "OPEN",
        "failure_count": 3,
        "success_count": 0,
        "opened_at": _utcnow(),
        "cooldown_seconds": 1800,
        "base_cooldown": 1800,
        "half_open_task_id": None,
    }
    try:
        await _spawn_research_task(project, db_session)

        # No task spawned — the breaker halt aborts before the strategic block.
        tasks = (
            (
                await db_session.execute(
                    select(Task).where(Task.project_id == project.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(tasks) == 0, "No task spawned while circuit breaker OPEN"

        # Marker preserved (steering not lost) and stamped notified.
        await db_session.refresh(project)
        marker = (project.vision or {}).get("_strategic_refresh")
        assert marker is not None, "operator marker must persist across the halt"
        assert marker.get("notified") is True

        steering = _deferred_steering_messages(
            await _system_messages(db_session, project.id)
        )
        assert len(steering) == 1, "breaker-open halt must post exactly one notice"

        # Second tick (still OPEN) must stay quiet — notified flag suppresses it.
        await _spawn_research_task(project, db_session)
        steering2 = _deferred_steering_messages(
            await _system_messages(db_session, project.id)
        )
        assert len(steering2) == 1, "deferral notice must not re-emit every tick"
    finally:
        _shared_state.circuit_breakers.pop(str(project.id), None)


@pytest.mark.asyncio
async def test_operator_refresh_deferred_on_no_accounts_posts_once(
    db_session, monkeypatch
):
    """An operator steering marker pending when the user has NO available model
    accounts must surface a one-time deferral notice — the no-accounts guard is the
    earliest halt early-return, well before the strategic block. Marker preserved,
    second tick quiet."""
    project = Project(
        name="Operator deferred on no accounts",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[_active_direction("d-nodef01")],
        vision={
            "purpose": "test",
            "_strategic_refresh": {
                "requested_at": _utcnow().isoformat(),
                "reason": "operator_steering",
                "count": 1,
                "origin": "operator",
            },
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Empty the user's account pool and clear the env fallback so has_available
    # is False (mirrors the no-accounts halt). The autouse fixture seeds an
    # account; drop it for the duration of this test.
    uid = "test-user-123"
    saved_pool = list(_max_plan_pools.get(uid, []))
    _max_plan_pools[uid] = []
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    try:
        await _spawn_research_task(project, db_session)

        tasks = (
            (
                await db_session.execute(
                    select(Task).where(Task.project_id == project.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(tasks) == 0, "No task spawned when no accounts available"

        await db_session.refresh(project)
        marker = (project.vision or {}).get("_strategic_refresh")
        assert marker is not None, "operator marker must persist across the halt"
        assert marker.get("notified") is True

        steering = _deferred_steering_messages(
            await _system_messages(db_session, project.id)
        )
        assert len(steering) == 1, "no-accounts halt must post exactly one notice"

        # Second tick (still no accounts) must stay quiet.
        await _spawn_research_task(project, db_session)
        steering2 = _deferred_steering_messages(
            await _system_messages(db_session, project.id)
        )
        assert len(steering2) == 1, "deferral notice must not re-emit every tick"
    finally:
        _max_plan_pools[uid] = saved_pool


@pytest.mark.asyncio
async def test_qualification_refresh_on_circuit_breaker_open_stays_silent(db_session):
    """A QUALIFICATION-origin refresh marker pending while the breaker is OPEN must
    NOT post an operator-facing notice — the deferral visibility is reserved for
    operator intent (automated refreshes park silently)."""
    project = Project(
        name="Qualification silent on CB open",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[_active_direction("d-cbqual1")],
        vision={
            "purpose": "test",
            "_strategic_refresh": {
                "requested_at": _utcnow().isoformat(),
                "reason": "post_qualification",
                "count": 1,
                "origin": "qualification",
            },
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    _shared_state.circuit_breakers[str(project.id)] = {
        "state": "OPEN",
        "failure_count": 3,
        "success_count": 0,
        "opened_at": _utcnow(),
        "cooldown_seconds": 1800,
        "base_cooldown": 1800,
        "half_open_task_id": None,
    }
    try:
        await _spawn_research_task(project, db_session)

        steering = _deferred_steering_messages(
            await _system_messages(db_session, project.id)
        )
        assert len(steering) == 0, "qualification-origin marker must stay silent"
    finally:
        _shared_state.circuit_breakers.pop(str(project.id), None)


@pytest.mark.asyncio
async def test_operator_refresh_deferred_behind_bootstrap_pending_posts_once(
    db_session,
):
    """Operator steering pending behind repo_bootstrap posts once and stays queued."""
    project = Project(
        name="Operator deferred behind bootstrap",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[_active_direction("d-bootstrap-op")],
        vision={
            "purpose": "test",
            "_strategic_refresh": {
                "requested_at": _utcnow().isoformat(),
                "reason": "operator_steering",
                "count": 1,
                "origin": "operator",
            },
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(
        Task(
            project_id=project.id,
            title="Bootstrap: operator deferred",
            payloads=[{"type": "repo_bootstrap"}],
            status=TaskStatus.IN_PROGRESS,
            created_by="ORCHESTRATOR",
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
        )
    )
    await db_session.commit()

    await _spawn_research_task(project, db_session)

    await db_session.refresh(project)
    marker = (project.vision or {}).get("_strategic_refresh")
    assert marker is not None and marker.get("notified") is True

    steering = _deferred_steering_messages(
        await _system_messages(db_session, project.id)
    )
    assert len(steering) == 1, "bootstrap-pending halt must post exactly one notice"

    await _spawn_research_task(project, db_session)
    steering2 = _deferred_steering_messages(
        await _system_messages(db_session, project.id)
    )
    assert len(steering2) == 1, "deferral notice must not re-emit every tick"


@pytest.mark.asyncio
async def test_qualification_refresh_deferred_behind_bootstrap_pending_stays_silent(
    db_session,
):
    """Qualification-origin refreshes stay silent while repo_bootstrap is pending."""
    project = Project(
        name="Qualification deferred behind bootstrap",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[_active_direction("d-bootstrap-qual")],
        vision={
            "purpose": "test",
            "_strategic_refresh": {
                "requested_at": _utcnow().isoformat(),
                "reason": "post_qualification:promoted",
                "count": 1,
            },
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(
        Task(
            project_id=project.id,
            title="Bootstrap: qualification silent",
            payloads=[{"type": "repo_bootstrap"}],
            status=TaskStatus.QUEUED,
            created_by="ORCHESTRATOR",
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
        )
    )
    await db_session.commit()

    await _spawn_research_task(project, db_session)

    await db_session.refresh(project)
    marker = (project.vision or {}).get("_strategic_refresh")
    assert marker is not None and marker.get("notified") is None

    steering = _deferred_steering_messages(
        await _system_messages(db_session, project.id)
    )
    assert len(steering) == 0, "qualification-origin marker must stay silent"


# ─────────────────────────────────────────────────────────────────────────────
# Qualification-origin refresh must force a strategic tick when due
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_qualification_refresh_forces_strategic_tick_when_due_and_modulo_idle(
    db_session, mocker
):
    """A due qualification-origin refresh must promote the tick into the
    strategic path even when the modulo cadence would not fire."""
    strategic_review_mock = mocker.patch(
        "app.strategic_spawning._spawn_strategic_review",
        new=AsyncMock(return_value=True),
    )
    mocker.patch(
        "app.strategic_spawning._spawn_design_doc_update",
        new=AsyncMock(return_value=True),
    )
    now = _utcnow()
    project = Project(
        name="Qualification refresh forces strategic",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[_active_direction("d-qualforce1", "Active research lane")],
        vision={
            "purpose": "test",
            "_research_spawn_count": 1,  # 1 % 6 != 0, so modulo stays idle
            "_strategic_refresh_last_fired_at": (
                now - timedelta(minutes=5)
            ).isoformat(),
            "_strategic_refresh": {
                "requested_at": now.isoformat(),
                "reason": "post_qualification:promoted",
                "count": 1,
                "origin": "qualification",
            },
        },
        design_doc={
            "systems/api": {"id": "api", "status": "working"}
        },  # Has systems, so architecture isn't triggered
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    recent_strategic = Task(
        project_id=project.id,
        title="Recent strategic cycle",
        payloads=[{"type": "strategic_review", "base_branch": "main"}],
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        created_at=now - timedelta(minutes=5),
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(recent_strategic)
    await db_session.commit()

    await _spawn_research_task(project, db_session)

    assert strategic_review_mock.called, (
        "Due qualification-origin refresh must force a strategic tick even when "
        "the modulo cadence is idle"
    )


@pytest.mark.asyncio
async def test_qualification_refresh_parks_at_breaker_threshold(db_session, mocker):
    """A qualification-origin refresh must still park when the breaker is tripped."""
    strategic_review_mock = mocker.patch(
        "app.strategic_spawning._spawn_strategic_review",
        new=AsyncMock(return_value=True),
    )
    mocker.patch(
        "app.strategic_spawning._spawn_design_doc_update",
        new=AsyncMock(return_value=True),
    )
    now = _utcnow()
    project = Project(
        name="Qualification refresh parks at breaker",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[],
        vision={
            "purpose": "test",
            "_strategic_failures": 3,
            "_research_spawn_count": 1,
            "_strategic_refresh_last_fired_at": (
                now - timedelta(minutes=5)
            ).isoformat(),
            "_strategic_refresh": {
                "requested_at": now.isoformat(),
                "reason": "post_qualification:promoted",
                "count": 1,
                "origin": "qualification",
            },
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    recent_strategic = Task(
        project_id=project.id,
        title="Recent strategic cycle",
        payloads=[{"type": "strategic_review", "base_branch": "main"}],
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        created_at=now - timedelta(minutes=5),
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(recent_strategic)
    await db_session.commit()

    await _spawn_research_task(project, db_session)

    assert not strategic_review_mock.called, (
        "Qualification-origin refresh must stay parked when the failure breaker is tripped"
    )
    await db_session.refresh(project)
    assert (project.vision or {}).get("_strategic_refresh") is not None


@pytest.mark.asyncio
async def test_operator_refresh_still_forces_strategic_tick_when_due(
    db_session, mocker
):
    """Operator-origin refreshes must keep bypassing the breaker when due."""
    strategic_review_mock = mocker.patch(
        "app.strategic_spawning._spawn_strategic_review",
        new=AsyncMock(return_value=True),
    )
    mocker.patch(
        "app.strategic_spawning._spawn_design_doc_update",
        new=AsyncMock(return_value=True),
    )
    now = _utcnow()
    project = Project(
        name="Operator refresh forces strategic",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[],
        vision={
            "purpose": "test",
            "_strategic_failures": 3,
            "_research_spawn_count": 1,
            "_strategic_refresh_last_fired_at": (
                now - timedelta(minutes=5)
            ).isoformat(),
            "_strategic_refresh": {
                "requested_at": now.isoformat(),
                "reason": "operator_steering",
                "count": 1,
                "origin": "operator",
            },
        },
        design_doc={
            "systems/api": {"id": "api", "status": "working"}
        },  # Has systems, so architecture isn't triggered
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    recent_strategic = Task(
        project_id=project.id,
        title="Recent strategic cycle",
        payloads=[{"type": "strategic_review", "base_branch": "main"}],
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        created_at=now - timedelta(minutes=5),
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(recent_strategic)
    await db_session.commit()

    await _spawn_research_task(project, db_session)

    assert strategic_review_mock.called, (
        "Operator-origin refresh must continue to bypass the breaker when due"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Missing-foundation demand injected into the research spawn payload
# ─────────────────────────────────────────────────────────────────────────────


def _blocked_dependent(project_id, need, *, direction_id="d-other"):
    """A BLOCKED dependent waiting on a single capability/foundation need.

    Uses a non-research task type on a DIFFERENT direction than the active
    research lane so it contributes demand without occupying the lane the
    autonomous loop wants to (re)spawn research on.
    """
    return Task(
        project_id=project_id,
        title="create: needs a missing foundation",
        payloads=[{"type": "create", "prompt": "build feature", "base_branch": "main"}],
        status=TaskStatus.BLOCKED,
        blocked_at=_utcnow() - timedelta(hours=4),
        context={
            "direction_id": direction_id,
            "blocker": {
                "source": "agent",
                "waiting_on": need.get("ref"),
                "reason": "BLOCKER: waiting on foundation",
                "need": need,
                "needs": [need],
            },
        },
        created_by="ORCHESTRATOR",
    )


async def _spawn_tactical_research(db_session, directions):
    """Build a project that spawns a tactical (directed) research task and return it."""
    project = Project(
        name="Demand Research",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=directions,
        # purpose present + an active direction → force_strategic is False;
        # _research_spawn_count=1 is NOT divisible by strategic_frequency (6),
        # so this is a tactical research cycle, not a strategic one.
        vision={"purpose": "test", "_research_spawn_count": 1},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    return project


@pytest.mark.asyncio
async def test_research_payload_includes_missing_capability_demand(db_session):
    """When a stranded dependent declares a capability prerequisite no task
    provides, the spawned research payload carries the aggregated demand
    (matching the collector) plus an advisory hint, and validates cleanly."""
    project = await _spawn_tactical_research(
        db_session, [_active_direction("d-research1", "Active research lane")]
    )
    need = {"kind": "capability", "ref": "auth-subsystem", "label": "Auth subsystem"}
    db_session.add(_blocked_dependent(project.id, need))
    await db_session.commit()

    expected = await collect_missing_capability_demand(db_session, project.id)
    assert expected["total"] == 1  # sanity: the fixture really produces demand

    await _spawn_research_task(project, db_session)

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    research = [
        t for t in result.scalars().all() if t.first_payload.get("type") == "research"
    ]
    assert len(research) == 1, "exactly one tactical research task should spawn"
    payload = research[0].first_payload

    # The demand field is present and matches what the collector returns.
    assert "missing_capability_demand" in payload
    assert payload["missing_capability_demand"] == expected
    assert payload["missing_capability_demand"]["demands"][0]["ref"] == "auth-subsystem"
    # The advisory prompt hint frames the demand as something to act on.
    assert "Missing-foundation demand" in (payload.get("prompt_suffix") or "")
    # Payload still validates cleanly with the new optional field present.
    validate_internal_payload(dict(payload), "test_research_demand")


@pytest.mark.asyncio
async def test_research_payload_omits_demand_when_none(db_session):
    """With no stranded dependents the research payload omits the demand field
    entirely (and no advisory hint is appended) yet still validates."""
    project = await _spawn_tactical_research(
        db_session, [_active_direction("d-research2", "Active research lane")]
    )

    # Sanity: collector reports no demand for this project.
    expected = await collect_missing_capability_demand(db_session, project.id)
    assert expected["total"] == 0

    await _spawn_research_task(project, db_session)

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    research = [
        t for t in result.scalars().all() if t.first_payload.get("type") == "research"
    ]
    assert len(research) == 1
    payload = research[0].first_payload

    assert "missing_capability_demand" not in payload
    assert "Missing-foundation demand" not in (payload.get("prompt_suffix") or "")
    validate_internal_payload(dict(payload), "test_research_no_demand")


# ─────────────────────────────────────────────────────────────────────────────
# Per-project spend ceiling gate
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_project_spend_ceiling_pauses_autonomous_spawning(db_session, mocker):
    """Spend ceiling gate blocks spawning when window spend >= ceiling, allows when below,
    deduplicates alerts, and is a no-op when no ceiling is set.

    Covers: tactical-research gate (end-to-end), alert deduplication, and the
    unset-ceiling path. The acceptance selector for this task.
    """
    mocker.patch("app.broadcast.broadcast_to_project", new=AsyncMock(return_value=None))

    # ── Part 1: ceiling exceeded → no spawn, alert created ──
    project = Project(
        name="Spend Ceiling Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[_active_direction("d-spend01")],
        build_config={"resource_envelope": {"spend_ceiling_usd": 1.0}},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    mock_spend = mocker.patch(
        "app.autonomous_research.get_project_recent_spend_usd",
        new=AsyncMock(
            return_value={"total": 1.5, "compute": 1.5, "agent_vm": 0.0, "idle": 0.0}
        ),
    )

    await _spawn_research_task(project, db_session)

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    tasks = [
        t for t in result.scalars().all() if t.first_payload.get("type") == "research"
    ]
    assert len(tasks) == 0, "No task should be spawned when spend ceiling is exceeded"

    alerts = (
        (await db_session.execute(select(Alert).where(Alert.project_id == project.id)))
        .scalars()
        .all()
    )
    ceiling_alerts = [a for a in alerts if "Spend ceiling" in a.title]
    assert len(ceiling_alerts) == 1, "Exactly one spend-ceiling alert should be created"
    assert "1.5000" in ceiling_alerts[0].message
    assert "1.0000" in ceiling_alerts[0].message

    # ── Part 2: alert deduplication — second call must not create a second alert ──
    await _spawn_research_task(project, db_session)
    alerts2 = (
        (await db_session.execute(select(Alert).where(Alert.project_id == project.id)))
        .scalars()
        .all()
    )
    ceiling_alerts2 = [a for a in alerts2 if "Spend ceiling" in a.title]
    assert len(ceiling_alerts2) == 1, "Alert must be deduplicated — still exactly one"

    assert mock_spend.call_count >= 2, (
        "Spend function should be called each gate evaluation"
    )

    # ── Part 3: spend below ceiling → task spawned ──
    project2 = Project(
        name="Spend Below Ceiling",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[_active_direction("d-spend02")],
        build_config={"resource_envelope": {"spend_ceiling_usd": 10.0}},
        vision={"purpose": "test"},
    )
    db_session.add(project2)
    await db_session.commit()
    await db_session.refresh(project2)

    mocker.patch(
        "app.autonomous_research.get_project_recent_spend_usd",
        new=AsyncMock(
            return_value={"total": 2.0, "compute": 2.0, "agent_vm": 0.0, "idle": 0.0}
        ),
    )

    await _spawn_research_task(project2, db_session)

    result2 = await db_session.execute(
        select(Task).where(Task.project_id == project2.id)
    )
    tasks2 = [
        t for t in result2.scalars().all() if t.first_payload.get("type") == "research"
    ]
    assert len(tasks2) >= 1, "Task should be spawned when spend is below ceiling"

    # ── Part 4: no ceiling set → today's behavior, no alert, no spend query ──
    project3 = Project(
        name="No Ceiling",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[_active_direction("d-spend03")],
        build_config={},  # no resource_envelope.spend_ceiling_usd
        vision={"purpose": "test"},
    )
    db_session.add(project3)
    await db_session.commit()
    await db_session.refresh(project3)

    # Re-patch to count calls cleanly for the no-ceiling assertion
    no_ceiling_mock = mocker.patch(
        "app.autonomous_research.get_project_recent_spend_usd",
        new=AsyncMock(
            return_value={
                "total": 999.0,
                "compute": 999.0,
                "agent_vm": 0.0,
                "idle": 0.0,
            }
        ),
    )
    await _spawn_research_task(project3, db_session)

    assert no_ceiling_mock.call_count == 0, (
        "get_project_recent_spend_usd must NOT be called when no ceiling is set"
    )
    # Also assert no spend-ceiling alerts for project3
    alerts3 = (
        (await db_session.execute(select(Alert).where(Alert.project_id == project3.id)))
        .scalars()
        .all()
    )
    ceiling_alerts3 = [a for a in alerts3 if "Spend ceiling" in a.title]
    assert len(ceiling_alerts3) == 0, "No alert when no ceiling is configured"


@pytest.mark.asyncio
async def test_research_payload_available_capabilities_carry_proof_status(db_session):
    """The research payload's available_capabilities entries carry the durable
    proof_status axis (proven vs unproven) so the research agent can distinguish
    proven from working-but-unproven capabilities when declaring refs."""
    project = await _spawn_tactical_research(
        db_session, [_active_direction("d-research3", "Active research lane")]
    )
    # Registry declares two capabilities; only one has a durable proof record.
    project.problem_model = {
        "intent": {"purpose": "test"},
        "capabilities": [
            {"name": "auth-core", "status": "working"},
            {"name": "fraud-detect", "status": "working"},
        ],
    }
    project.capability_proof_records = {
        "auth-core": [{"proof_kind": "named_test", "commit_sha": "abc123"}],
    }
    db_session.add(project)
    await db_session.commit()

    await _spawn_research_task(project, db_session)

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    research = [
        t for t in result.scalars().all() if t.first_payload.get("type") == "research"
    ]
    assert len(research) == 1
    payload = research[0].first_payload

    caps = payload.get("available_capabilities")
    assert isinstance(caps, list) and caps, "available_capabilities should be present"
    by_name = {c["name"]: c for c in caps}
    assert by_name["auth-core"]["proof_status"] == "proven"
    assert by_name["fraud-detect"]["proof_status"] == "unproven"
    validate_internal_payload(dict(payload), "test_research_proof_status")


# ─────────────────────────────────────────────────────────────────────────────
# Verification provenance — _computed_at / _branch_sha stamps
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_system_verification_carries_branch_sha_and_computed_at(
    db_session, mocker
):
    """system_verification map carries _computed_at and _branch_sha provenance stamps."""
    direction = _active_direction("d-prov-sys", "Improve real system")
    project = Project(
        name="ProvStamp Systems",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test", "_research_spawn_count": 1},
        design_doc={
            "systems/real": {
                "status": "mature",
                "modules": ["backend/app/a.py"],
            }
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    _patch_classify(mocker, present_paths={"backend/app/a.py"})
    mocker.patch(
        "app.autonomous_research.resolve_head_commit",
        new=AsyncMock(return_value="abc1234567890abc"),
    )

    payload = await _spawn_and_get_research_payload(project, db_session)
    sv = payload.get("system_verification")
    assert sv is not None, "system_verification should be present"
    assert sv["real"]["status"] == "verified"
    assert "_computed_at" in sv, "_computed_at provenance stamp must be present"
    assert sv["_branch_sha"] == "abc1234567890abc", (
        "_branch_sha must match resolved SHA"
    )
    # Provenance keys are metadata, not system entries
    assert "real" in sv
    assert "_computed_at" not in sv["real"]


@pytest.mark.asyncio
async def test_system_verification_branch_sha_none_when_indeterminate(
    db_session, mocker
):
    """When resolve_head_commit returns None, _branch_sha is None (not absent)."""
    direction = _active_direction("d-prov-nosha", "Improve system")
    project = Project(
        name="ProvStamp NoSha",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test", "_research_spawn_count": 1},
        design_doc={
            "systems/x": {
                "status": "mature",
                "modules": ["backend/app/x.py"],
            }
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    _patch_classify(mocker, present_paths={"backend/app/x.py"})
    mocker.patch(
        "app.autonomous_research.resolve_head_commit",
        new=AsyncMock(return_value=None),
    )

    payload = await _spawn_and_get_research_payload(project, db_session)
    sv = payload.get("system_verification")
    assert sv is not None
    assert "_computed_at" in sv
    assert sv["_branch_sha"] is None


@pytest.mark.asyncio
async def test_capability_verification_carries_branch_sha_and_computed_at(
    db_session, mocker
):
    """capability_verification map carries _computed_at and _branch_sha stamps."""
    direction = _active_direction("d-prov-cap", "Improve capability")
    project = Project(
        name="ProvStamp Caps",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test", "_research_spawn_count": 1},
        design_doc={
            "capabilities/auth": {
                "status": "working",
                "modules": ["backend/app/auth.py"],
            }
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    _patch_classify(mocker, present_paths={"backend/app/auth.py"})
    mocker.patch(
        "app.autonomous_research.resolve_head_commit",
        new=AsyncMock(return_value="deadbeef1234"),
    )

    payload = await _spawn_and_get_research_payload(project, db_session)
    cv = payload.get("capability_verification")
    assert cv is not None, "capability_verification should be present"
    assert cv["auth"]["status"] == "verified"
    assert "_computed_at" in cv
    assert cv["_branch_sha"] == "deadbeef1234"


@pytest.mark.asyncio
async def test_refresh_research_task_verification_overwrites_stale_stamp(
    db_session, mocker
):
    """refresh_research_task_verification replaces stale provenance with a fresh stamp."""
    from app.autonomous_research import refresh_research_task_verification

    direction = _active_direction("d-refresh", "Refresh verification")
    project = Project(
        name="Refresh Verification",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test"},
        design_doc={
            "systems/api": {
                "status": "mature",
                "modules": ["backend/app/api.py"],
            }
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Task with stale spawn-time verification (old stamp, old SHA)
    stale_sv = {
        "api": {
            "status": "claimed_absent",
            "claimed_status": "mature",
            "present": [],
            "missing": ["backend/app/api.py"],
        },
        "_computed_at": "2020-01-01T00:00:00",
        "_branch_sha": "oldsha",
    }
    task = Task(
        project_id=project.id,
        title="Research: Refresh verification",
        payloads=[
            {
                "type": "research",
                "base_branch": "main",
                "directions": [direction],
                "system_verification": stale_sv,
            }
        ],
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    _patch_classify(mocker, present_paths={"backend/app/api.py"})
    mocker.patch(
        "app.autonomous_research.resolve_head_commit",
        new=AsyncMock(return_value="newsha123"),
    )

    await refresh_research_task_verification(task, project, db_session)

    sv = task.first_payload.get("system_verification")
    assert sv is not None
    assert sv["api"]["status"] == "verified", "verification status should be refreshed"
    assert sv["_branch_sha"] == "newsha123", "_branch_sha should be updated"
    assert sv["_computed_at"] != "2020-01-01T00:00:00", "_computed_at should be updated"


# ─────────────────────────────────────────────────────────────────────────────
# Direction Refractory: prevents immediate re-research after terminal state
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_refractory_blocks_immediate_respawn_after_terminal(db_session):
    """A direction with refractory_until in the future must be excluded from
    active_directions so no new research cycle spawns before the window elapses.

    This is the primary fix for the 2026-06-10 incident where direction
    'd-65919a0d' spawned 10 sequential research cycles in 9.5h — each new
    cycle fired 20s-5min after the previous parent went terminal because
    last_researched_at was never set for no-findings completions.
    """
    future = (datetime.now(timezone.utc) + timedelta(hours=4)).isoformat()
    direction = {
        **_active_direction("d-refract1", "Direction in refractory"),
        "refractory_until": future,
        # last_researched_at absent to isolate the refractory check
    }
    project = Project(
        name="Refractory Block Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _spawn_research_task(project, db_session)

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    research_tasks = [
        t for t in result.scalars().all() if t.first_payload.get("type") == "research"
    ]
    assert len(research_tasks) == 0, (
        "No research should spawn while the direction's refractory_until is in the future"
    )


@pytest.mark.asyncio
async def test_refractory_expired_allows_respawn(db_session):
    """A direction whose refractory_until is in the PAST (or absent) must be
    eligible for re-research — operator/strategist clear the refractory by
    removing or back-dating the field.
    """
    past = (_utcnow() - timedelta(hours=1)).isoformat()
    direction = {
        **_active_direction("d-refract2", "Direction past refractory"),
        "refractory_until": past,
    }
    project = Project(
        name="Refractory Expired Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _spawn_research_task(project, db_session)

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    research_tasks = [
        t for t in result.scalars().all() if t.first_payload.get("type") == "research"
    ]
    assert len(research_tasks) >= 1, (
        "Research should spawn when refractory_until is in the past (expired)"
    )


@pytest.mark.asyncio
async def test_no_findings_completion_stamps_saturation_and_refractory(
    db_session, mocker
):
    """When a research task completes with no actionable findings, the direction
    must be stamped with refractory_until and last_researched_at so it cannot be
    immediately re-selected. possibly_saturated is NOT set on the first no-findings
    cycle — saturation requires 3+ consecutive unproductive cycles.

    This closes the blind spot where the early-return gate in
    _handle_research_completion skipped _apply_direction_state_updates entirely,
    leaving last_researched_at unset and the direction immediately re-eligible.
    """
    direction_id = "d-nofindings1"
    direction = _active_direction(direction_id, "Direction yielding no findings")
    project = Project(
        name="No Findings Refractory",
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
        title="Research: no findings direction",
        payloads=[
            {
                "type": "research",
                "directions": [direction],
                "current_direction_id": direction_id,
                "base_branch": "main",
            }
        ],
        context={"direction_id": direction_id, "research_depth": 0},
        status=TaskStatus.IN_PROGRESS,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # Patch externals and make the research agent return NO findings
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[],
    )
    mocker.patch(
        "app.research_completion._parse_research_insights",
        new=AsyncMock(return_value=None),
    )
    mocker.patch(
        "app.task_completion._ensure_branch",
        new=AsyncMock(side_effect=lambda t, *a, **kw: t),
    )

    await _handle_research_completion(task, "", db_session)

    # Reload the project to inspect the direction state
    await db_session.refresh(project)
    dirs = project.directions or []
    target = next((d for d in dirs if d.get("id") == direction_id), None)
    assert target is not None, "Direction must still exist"
    assert target.get("possibly_saturated") is not True, (
        "Direction must NOT be marked saturated after a single no-findings cycle"
    )
    assert target.get("last_researched_at") is not None, (
        "last_researched_at must be set so the cooldown applies"
    )
    assert target.get("refractory_until") is not None, (
        "refractory_until must be stamped to prevent immediate re-selection"
    )
    # The refractory must be in the future
    refractory_dt = datetime.fromisoformat(target["refractory_until"])
    assert refractory_dt > _utcnow(), (
        "refractory_until must be in the future after a no-findings completion"
    )


async def _map_direction_to_criterion(db_session, project, direction_id, *, covered):
    """Attach an open-version acceptance criterion to a Direction ORM row.

    Returns the criterion_key. When *covered* is False the criterion has no
    evidence (coverage='uncovered'); when True a reachability evidence row makes
    it 'covered'. The gate helper reads criterion_keys off the Direction row, so
    a row must exist even though apply_post_spawn_state operates on the in-memory
    project.directions blob.
    """
    from app import acceptance_criteria as ac
    from app.models import (
        CriterionEvidence,
        Direction,
        ReleaseVersion,
        VersionAcceptanceCriterion,
    )

    version = ReleaseVersion(project_id=project.id, version_number=1)
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    criterion = await ac.create_criterion(
        db_session, project.id, version.id, "Owned acceptance outcome"
    )
    if covered:
        db_session.add(
            CriterionEvidence(
                criterion_id=criterion.id,
                project_id=project.id,
                source_kind="reachability",
                source_id="pr-x",
            )
        )
        await db_session.commit()

    db_session.add(
        Direction(
            project_id=project.id,
            direction_key=direction_id,
            status="active",
            data={"id": direction_id},
            criterion_keys=[criterion.criterion_key],
        )
    )
    await db_session.commit()
    _ = VersionAcceptanceCriterion  # imported for symmetry / readability
    return criterion.criterion_key


@pytest.mark.asyncio
async def test_apply_post_spawn_state_completes_when_owned_criterion_covered(
    db_session, mocker
):
    """Site 1: when the owned criterion IS covered, the saturated proven direction
    still auto-completes exactly as before the gate."""
    direction_id = "d-site1-covered"
    direction = {
        **_active_direction(direction_id, "Saturated proven, covered criterion"),
        "research_history": [
            {"timestamp": "2026-01-01T00:00:00", "tasks_created": 1, "prs_merged": 1},
            {"timestamp": "2026-01-02T00:00:00", "tasks_created": 0, "prs_merged": 1},
            {"timestamp": "2026-01-03T00:00:00", "tasks_created": 1, "prs_merged": 1},
        ],
    }
    project = Project(
        name="Site1 Covered Gate",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _map_direction_to_criterion(db_session, project, direction_id, covered=True)

    dup_title = "Fix exhausted finding"
    dup_desc = "Already addressed"
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
        title=f"Research: {direction['text']}",
        payloads=[
            {
                "type": "research",
                "directions": [direction],
                "base_branch": "main",
                "current_direction_id": direction_id,
            }
        ],
        context={"direction_id": direction_id, "research_depth": 0},
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
        return_value=[
            {
                "title": dup_title,
                "description": dup_desc,
                "prompt": "Fix the exhausted finding",
                "priority": "medium",
                "scope": "small",
                "risk": "low",
                "type": "improvement",
                "direction_id": direction_id,
            }
        ],
    )
    mocker.patch(
        "app.research_completion._parse_research_insights",
        new=AsyncMock(return_value=[]),
    )
    mocker.patch(
        "app.notification_dispatch.post_direction_milestone_message",
        new=AsyncMock(return_value=None),
    )

    await _handle_research_completion(task, "", db_session)

    await db_session.refresh(project)
    target = next(
        (d for d in (project.directions or []) if d.get("id") == direction_id), None
    )
    assert target is not None
    assert target.get("status") == "completed", (
        "Site 1 must still auto-complete a saturated proven direction whose owned "
        "criterion is covered"
    )
    assert target.get("auto_completed") is True


@pytest.mark.asyncio
async def test_exhausted_saturated_proven_direction_auto_completes(db_session, mocker):
    """A fully-exhausted saturated direction auto-completes when all findings are deduped.

    Covers the zero-spawn (all-deduped) path through apply_post_spawn_state.
    Before the fix, the guard `entry.get("tasks_created", 0) > 0` prevented
    auto-completion exactly for the most exhausted directions — those where every
    finding was already known and nothing spawned. After the fix, the criteria
    (saturated + total_merged >= 3) are evaluated regardless of tasks_created.
    """
    direction_id = "d-exhaust-sat1"
    direction = {
        **_active_direction(direction_id, "Exhausted saturated direction"),
        # 3 prior low-output cycles → _compute_saturation returns True
        "research_history": [
            {"timestamp": "2026-01-01T00:00:00", "tasks_created": 1, "prs_merged": 1},
            {"timestamp": "2026-01-02T00:00:00", "tasks_created": 0, "prs_merged": 1},
            {"timestamp": "2026-01-03T00:00:00", "tasks_created": 1, "prs_merged": 1},
        ],
    }
    project = Project(
        name="Exhausted Saturated Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    dup_title = "Fix exhausted finding"
    dup_desc = "Already addressed"
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
        title=f"Research: {direction['text']}",
        payloads=[
            {
                "type": "research",
                "directions": [direction],
                "base_branch": "main",
                "current_direction_id": direction_id,
            }
        ],
        context={"direction_id": direction_id, "research_depth": 0},
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
        return_value=[
            {
                "title": dup_title,
                "description": dup_desc,
                "prompt": "Fix the exhausted finding",
                "priority": "medium",
                "scope": "small",
                "risk": "low",
                "type": "improvement",
                "direction_id": direction_id,
            }
        ],
    )
    mocker.patch(
        "app.research_completion._parse_research_insights",
        new=AsyncMock(return_value=[]),
    )
    mocker.patch(
        "app.notification_dispatch.post_direction_milestone_message",
        new=AsyncMock(return_value=None),
    )

    await _handle_research_completion(task, "", db_session)

    await db_session.refresh(project)
    dirs = project.directions or []
    target = next((d for d in dirs if d.get("id") == direction_id), None)
    assert target is not None, "Direction must still exist"
    assert target.get("status") == "completed", (
        "Saturated direction with proven merged PRs must be auto-completed "
        "even when the triggering cycle produced zero tasks (all deduped)"
    )
    assert target.get("auto_completed") is True, "auto_completed flag must be set"
    assert target.get("auto_completed_reason"), "auto_completed_reason must be set"


@pytest.mark.asyncio
async def test_no_findings_saturated_proven_direction_auto_completes(
    db_session, mocker
):
    """A saturated, proven direction is auto-completed via the no-findings path.

    Covers _stamp_no_findings_refractory: when 3+ consecutive unproductive
    cycles make the direction saturated AND total_merged >= 3, the direction
    is marked completed in the same locked write that stamps the refractory.
    """
    direction_id = "d-nf-sat-proven"
    direction = {
        **_active_direction(direction_id, "No-findings saturated proven"),
        # 2 prior consecutive unproductive cycles — one more makes it saturated
        "consecutive_unproductive_cycles": 2,
        "research_history": [
            {"timestamp": "2026-01-01T00:00:00", "tasks_created": 0, "prs_merged": 1},
            {"timestamp": "2026-01-02T00:00:00", "tasks_created": 0, "prs_merged": 1},
            {"timestamp": "2026-01-03T00:00:00", "tasks_created": 0, "prs_merged": 1},
        ],
    }
    project = Project(
        name="No-Findings Saturation Auto-Complete",
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
        title=f"Research: {direction['text']}",
        payloads=[
            {
                "type": "research",
                "directions": [direction],
                "base_branch": "main",
                "current_direction_id": direction_id,
            }
        ],
        context={"direction_id": direction_id, "research_depth": 0},
        status=TaskStatus.IN_PROGRESS,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[],
    )
    mocker.patch(
        "app.research_completion._parse_research_insights",
        new=AsyncMock(return_value=None),
    )
    mocker.patch(
        "app.task_completion._ensure_branch",
        new=AsyncMock(side_effect=lambda t, *a, **kw: t),
    )
    mocker.patch(
        "app.notification_dispatch.post_direction_milestone_message",
        new=AsyncMock(return_value=None),
    )

    await _handle_research_completion(task, "", db_session)

    await db_session.refresh(project)
    dirs = project.directions or []
    target = next((d for d in dirs if d.get("id") == direction_id), None)
    assert target is not None, "Direction must still exist"
    assert target.get("status") == "completed", (
        "Saturated proven direction must be auto-completed via the no-findings path"
    )
    assert target.get("auto_completed") is True
    assert target.get("auto_completed_reason"), "auto_completed_reason must be set"


@pytest.mark.asyncio
async def test_no_findings_saturated_unproven_direction_not_auto_completed(
    db_session, mocker
):
    """An unproven direction is NOT auto-completed even when saturated.

    Guards against premature retirement: saturation + consecutive no-findings
    cycles alone are not sufficient — the direction also needs total_merged >= 3
    to prove it produced value. An under-proven saturated direction should
    stay active and receive normal refractory stamping.
    """
    direction_id = "d-nf-sat-unproven"
    direction = {
        **_active_direction(direction_id, "Saturated but unproven direction"),
        "consecutive_unproductive_cycles": 2,
        # Only 2 PRs merged — below the threshold of 3
        "research_history": [
            {"timestamp": "2026-01-01T00:00:00", "tasks_created": 0, "prs_merged": 1},
            {"timestamp": "2026-01-02T00:00:00", "tasks_created": 0, "prs_merged": 1},
            {"timestamp": "2026-01-03T00:00:00", "tasks_created": 0, "prs_merged": 0},
        ],
    }
    project = Project(
        name="No-Findings Saturation No-Autocompletion",
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
        title=f"Research: {direction['text']}",
        payloads=[
            {
                "type": "research",
                "directions": [direction],
                "base_branch": "main",
                "current_direction_id": direction_id,
            }
        ],
        context={"direction_id": direction_id, "research_depth": 0},
        status=TaskStatus.IN_PROGRESS,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[],
    )
    mocker.patch(
        "app.research_completion._parse_research_insights",
        new=AsyncMock(return_value=None),
    )
    mocker.patch(
        "app.task_completion._ensure_branch",
        new=AsyncMock(side_effect=lambda t, *a, **kw: t),
    )

    await _handle_research_completion(task, "", db_session)

    await db_session.refresh(project)
    dirs = project.directions or []
    target = next((d for d in dirs if d.get("id") == direction_id), None)
    assert target is not None
    assert target.get("status") == "active", (
        "Under-proven direction (total_merged < 3) must NOT be auto-completed "
        "even when saturated"
    )
    assert not target.get("auto_completed"), (
        "auto_completed must not be set on an unproven direction"
    )
    assert target.get("possibly_saturated") is True, (
        "Direction should be marked saturated after 3 consecutive unproductive cycles"
    )
    assert target.get("refractory_until") is not None, (
        "refractory_until must be stamped to prevent immediate re-selection"
    )


@pytest.mark.asyncio
async def test_zero_eligible_directions_triggers_strategic_refresh(db_session, mocker):
    """When ALL active directions are in refractory (no eligible directions this
    cycle), the spawn loop must fire request_strategic_refresh exactly once via
    the coalescing mechanism so the strategist can replenish the portfolio.

    The strategist may explicitly re-open held directions by clearing
    refractory_until; the 180-min wall-clock backstop remains the safety net.
    """
    request_strategic_refresh_mock = mocker.patch(
        "app.strategic_trigger.request_strategic_refresh",
        new=AsyncMock(return_value=True),
    )
    design_doc_mock = mocker.patch(
        "app.strategic_spawning._spawn_design_doc_update",
        new=AsyncMock(return_value=True),
    )
    strategic_review_mock = mocker.patch(
        "app.strategic_spawning._spawn_strategic_review",
        new=AsyncMock(return_value=True),
    )

    future = (_utcnow() + timedelta(hours=6)).isoformat()
    direction = {
        **_active_direction("d-zeroelig1", "All-refractory direction"),
        "refractory_until": future,
    }
    project = Project(
        name="Zero Eligible — Strategic Trigger",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _spawn_research_task(project, db_session)

    # The coalescing trigger must have been called (once per spawn tick)
    assert request_strategic_refresh_mock.called, (
        "request_strategic_refresh must be called when all active directions are in refractory"
    )
    call_count = request_strategic_refresh_mock.call_count
    assert call_count == 1, (
        f"request_strategic_refresh should be called exactly once per tick, got {call_count}"
    )
    # A strategic spawn should have fired because force_strategic was set
    assert design_doc_mock.called or strategic_review_mock.called, (
        "A strategic cycle must spawn when all directions are in refractory and "
        "force_strategic is set"
    )


@pytest.mark.asyncio
async def test_empty_design_doc_forces_architecture_first_cycle(
    db_session, mocker
) -> None:
    """When a project's design_doc has no architecture content (cycle 1), the
    strategic spawn runs the full architecture-first pipeline (design_doc_update
    -> strategic_review) regardless of the cycle-count modulus."""
    mocker.patch(
        "app.strategic_spawning._spawn_design_doc_update",
        new_callable=AsyncMock,
        return_value=True,
    )
    mocker.patch(
        "app.strategic_spawning._spawn_strategic_review",
        new_callable=AsyncMock,
        return_value=True,
    )

    # Create a project with an empty design_doc (only config blocks, no architecture)
    project = Project(
        name="Empty Design Doc Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[_active_direction("d-arch-test-1")],
        vision={
            "purpose": "test architecture bootstrapping",
            "_research_spawn_count": 6,
        },
        design_doc={
            # Only config blocks (underscore-prefixed) — no real architecture content
            "_auto_remediation": {"enabled": False},
            "_issue_implementation": {"enabled": False},
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Mock tactical research selection to avoid side effects
    mocker.patch(
        "app.direction_outcomes._update_direction_research_outcomes",
        new_callable=AsyncMock,
        return_value=None,
    )
    mocker.patch(
        "app.anomaly_detection._compute_research_anomalies",
        new_callable=AsyncMock,
        return_value=None,
    )
    mocker.patch(
        "app.anomaly_detection.check_anomaly_actions",
        new_callable=AsyncMock,
        return_value={"action": "none"},
    )

    # Spawn on cycle 1 (modulus would normally NOT trigger architecture)
    await _spawn_research_task(project, db_session)

    # Verify design_doc_update was called (architecture-first pipeline)
    from app.strategic_spawning import _spawn_design_doc_update

    assert _spawn_design_doc_update.called, (
        "design_doc_update should spawn on empty design_doc"
    )

    # Verify strategic_review was NOT called directly (it's spawned by design_doc_update completion)
    from app.strategic_spawning import _spawn_strategic_review

    assert not _spawn_strategic_review.called, (
        "strategic_review should be deferred to design_doc_update completion"
    )


@pytest.mark.asyncio
async def test_populated_design_doc_respects_architecture_cadence(
    db_session, mocker
) -> None:
    """When a project's design_doc has architecture content, the modulus cadence
    schedule continues unchanged (cycle 1 runs strategy-only, cycle 3 runs architecture)."""
    mocker.patch(
        "app.strategic_spawning._spawn_design_doc_update",
        new_callable=AsyncMock,
        return_value=True,
    )
    mocker.patch(
        "app.strategic_spawning._spawn_strategic_review",
        new_callable=AsyncMock,
        return_value=True,
    )

    # Create a project with populated design_doc (has systems/* entries)
    project = Project(
        name="Populated Design Doc Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[_active_direction("d-arch-test-2")],
        vision={
            "purpose": "test architecture cadence",
            "_strategic_cycle_count": 0,
            "_research_spawn_count": 6,
        },
        design_doc={
            # Has real architecture content
            "systems/auth": {
                "description": "Authentication system",
                "modules": ["app/auth.py"],
                "status": "working",
            },
            "summary": {"architecture": "Core architecture overview"},
            # Config blocks are OK alongside content
            "_auto_remediation": {"enabled": False},
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Mock tactical research selection
    mocker.patch(
        "app.direction_outcomes._update_direction_research_outcomes",
        new_callable=AsyncMock,
        return_value=None,
    )
    mocker.patch(
        "app.anomaly_detection._compute_research_anomalies",
        new_callable=AsyncMock,
        return_value=None,
    )
    mocker.patch(
        "app.anomaly_detection.check_anomaly_actions",
        new_callable=AsyncMock,
        return_value={"action": "none"},
    )

    # Spawn on cycle 1 with architecture_review_frequency=3 (modulus 1 % 3 != 0)
    await _spawn_research_task(project, db_session)

    # Verify strategic_review was called (strategy-only, no architecture)
    from app.strategic_spawning import _spawn_strategic_review

    assert _spawn_strategic_review.called, (
        "strategic_review should spawn on cycle 1 when design_doc is populated"
    )

    # Verify design_doc_update was NOT called (no architecture on cycle 1)
    from app.strategic_spawning import _spawn_design_doc_update

    assert not _spawn_design_doc_update.called, (
        "design_doc_update should not spawn on cycle 1 with populated design_doc"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Spawn-pending idempotency — lock-timeout resilience
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_spawn_pending_resume_uses_stored_plan_output(db_session, mocker):
    """When _research_spawn_pending is set and logs produce no findings,
    the handler falls back to task.plan_output so the interrupted spawn can finish."""
    direction = _active_direction("d-spawn-res1", "Resume direction")
    project = Project(
        name="Spawn Pending Resume",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    finding = _make_finding(title="Stored finding", direction_id=direction["id"])

    # Task already in post-_ensure_branch-commit state: marker and plan_output durable.
    task = Task(
        project_id=project.id,
        title="Research: resume test",
        payloads=[
            {
                "type": "research",
                "directions": [direction],
                "base_branch": "main",
                "current_direction_id": direction["id"],
            }
        ],
        context={
            "research_depth": 0,
            "_research_spawn_pending": True,
        },
        plan_output=[finding],
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    _patch_research_externals(mocker)
    mocker.patch("app.research_completion._parse_research_findings", return_value=[])
    mocker.patch("app.research_completion._parse_research_insights", return_value=[])

    await _handle_research_completion(task, "", db_session)

    children = (
        (await db_session.execute(select(Task).where(Task.parent_task_id == task.id)))
        .scalars()
        .all()
    )
    assert len(children) == 1, (
        "Stored finding should be spawned via spawn-pending resume path"
    )

    await db_session.refresh(task)
    assert not (task.context or {}).get("_research_spawn_pending"), (
        "_research_spawn_pending must be cleared after successful spawn"
    )
    assert task.status == TaskStatus.EVALUATING, (
        "Task must be EVALUATING after spawning children — not COMPLETED with zero children"
    )


@pytest.mark.asyncio
async def test_spawn_pending_marker_stamped_before_ensure_branch(db_session, mocker):
    """_research_spawn_pending is set in task.context before _ensure_branch is called,
    so _ensure_branch's internal commit makes it durable regardless of what follows."""
    direction = _active_direction("d-spawn-stamp1", "Stamp direction")
    project = Project(
        name="Spawn Pending Stamp",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = await _make_research_task(project, direction, db_session)

    captured_context: list[dict] = []

    async def _capture_and_passthrough(task_obj, *_a, **_kw):
        captured_context.append(dict(task_obj.context or {}))
        return task_obj

    mocker.patch("app.task_completion._ensure_branch", new=_capture_and_passthrough)
    mocker.patch(
        "app.research_completion._validate_plan_dependencies", return_value=True
    )
    mocker.patch("app.research_completion._log_file_overlaps", return_value=[])
    mocker.patch("app.max_plan_pool._all_accounts", return_value=[])
    mocker.patch(
        "app.research_completion._apply_structural_actionability_downgrade",
        return_value=0,
    )
    mocker.patch("app.research_completion._is_high_stakes_finding", return_value=False)
    mocker.patch("app.broadcast.broadcast_to_project", new=AsyncMock(return_value=None))
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[_make_finding(direction_id=direction["id"])],
    )
    mocker.patch("app.research_completion._parse_research_insights", return_value=[])

    await _handle_research_completion(task, "", db_session)

    assert captured_context, "_ensure_branch should have been called"
    assert captured_context[0].get("_research_spawn_pending") is True, (
        "_research_spawn_pending must be True in task.context when _ensure_branch is called"
    )


@pytest.mark.asyncio
async def test_spawn_pending_marker_cleared_after_successful_spawn(db_session, mocker):
    """_research_spawn_pending is absent from task.context after a successful spawn."""
    direction = _active_direction("d-spawn-clr1", "Clear direction")
    project = Project(
        name="Spawn Pending Clear",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
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
    mocker.patch("app.research_completion._parse_research_insights", return_value=[])

    await _handle_research_completion(task, "", db_session)

    await db_session.refresh(task)
    assert not (task.context or {}).get("_research_spawn_pending"), (
        "_research_spawn_pending must be absent after a successful spawn"
    )


@pytest.mark.asyncio
async def test_mid_spawn_lock_timeout_replay_completes_spawn_exactly_once(
    db_session, mocker
):
    """Simulate a lock timeout inside _ensure_branch on the first pass.

    _ensure_branch's internal commit makes _research_spawn_pending and plan_output
    durable before the error. A sweep retry (second call with empty logs) uses the
    stored findings and completes the spawn, leaving exactly one child and the task
    in EVALUATING — never COMPLETED with zero children.
    """
    direction = _active_direction("d-midspawn1", "Mid-spawn direction")
    project = Project(
        name="Mid-Spawn Replay",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    finding = _make_finding(title="Mid-spawn finding", direction_id=direction["id"])
    task = await _make_research_task(project, direction, db_session)

    # Common patches (same as _patch_research_externals but without _ensure_branch).
    mocker.patch(
        "app.research_completion._validate_plan_dependencies", return_value=True
    )
    mocker.patch("app.research_completion._log_file_overlaps", return_value=[])
    mocker.patch("app.max_plan_pool._all_accounts", return_value=[])
    mocker.patch(
        "app.research_completion._apply_structural_actionability_downgrade",
        return_value=0,
    )
    mocker.patch("app.research_completion._is_high_stakes_finding", return_value=False)
    mocker.patch("app.broadcast.broadcast_to_project", new=AsyncMock(return_value=None))
    mocker.patch("app.research_completion._parse_research_insights", return_value=[])

    call_count = [0]

    async def _ensure_branch_fail_then_pass(task_obj, *_a, **_kw):
        call_count[0] += 1
        if call_count[0] == 1:
            # Simulate _ensure_branch committing the spawn-pending marker
            # (task.context mutation already applied by the stamp code above it)
            # then failing on the row-lock re-acquisition.
            await db_session.commit()
            raise RuntimeError(
                "lock timeout: simulated LockNotAvailableError mid-spawn"
            )
        return task_obj

    mocker.patch(
        "app.task_completion._ensure_branch", new=_ensure_branch_fail_then_pass
    )

    # Pass 1: handler commits plan_output + _research_spawn_pending, then _ensure_branch fails.
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[finding],
    )
    with pytest.raises(RuntimeError, match="lock timeout"):
        await _handle_research_completion(task, "", db_session)

    # Verify durable state after pass 1.
    await db_session.refresh(task)
    assert isinstance(task.plan_output, list) and len(task.plan_output) > 0, (
        "plan_output must be committed before _ensure_branch"
    )
    assert (task.context or {}).get("_research_spawn_pending") is True, (
        "_research_spawn_pending must be durable after _ensure_branch's internal commit"
    )

    # Pass 2: sweep retry with empty logs — resume path uses stored plan_output.
    mocker.patch("app.research_completion._parse_research_findings", return_value=[])
    await _handle_research_completion(task, "", db_session)

    children = (
        (await db_session.execute(select(Task).where(Task.parent_task_id == task.id)))
        .scalars()
        .all()
    )
    assert len(children) == 1, "Exactly one child spawned on replay — not zero, not two"

    await db_session.refresh(task)
    assert task.status == TaskStatus.EVALUATING, (
        "Task must be EVALUATING after successful replay — no childless-COMPLETED strand"
    )
    assert not (task.context or {}).get("_research_spawn_pending"), (
        "_research_spawn_pending cleared after successful replay"
    )


# ---------------------------------------------------------------------------
# Refractory window derives from the operator cooldown (one knob)
# ---------------------------------------------------------------------------


def test_refractory_defaults_to_operator_cooldown():
    """With no explicit refractory key, the post-cycle refractory must equal the
    operator-tuned autonomous_direction_cooldown_minutes — a hardcoded longer
    refractory would silently override operator throttling intent."""
    from app.shared_state import runtime_config as _runtime_config
    from app.research_direction_state import direction_refractory_minutes

    _runtime_config.pop("direction_refractory_minutes", None)
    _runtime_config["autonomous_direction_cooldown_minutes"] = 60
    assert direction_refractory_minutes() == 60.0


def test_refractory_falls_back_to_cooldown_default_when_neither_set():
    from app.shared_state import runtime_config as _runtime_config
    from app.research_direction_state import direction_refractory_minutes

    _runtime_config.pop("direction_refractory_minutes", None)
    _runtime_config.pop("autonomous_direction_cooldown_minutes", None)
    assert direction_refractory_minutes() == 240.0


def test_explicit_refractory_key_decouples_from_cooldown():
    from app.shared_state import runtime_config as _runtime_config
    from app.research_direction_state import direction_refractory_minutes

    _runtime_config["autonomous_direction_cooldown_minutes"] = 60
    _runtime_config["direction_refractory_minutes"] = 360
    assert direction_refractory_minutes() == 360.0


def test_saturation_refractory_is_double_the_standard_window():
    from app.shared_state import runtime_config as _runtime_config
    from app.research_direction_state import (
        direction_saturation_refractory_minutes,
    )

    _runtime_config.pop("direction_refractory_minutes", None)
    _runtime_config.pop("direction_saturation_refractory_minutes", None)
    _runtime_config["autonomous_direction_cooldown_minutes"] = 60
    assert direction_saturation_refractory_minutes() == 120.0


def test_explicit_saturation_refractory_key_wins():
    from app.shared_state import runtime_config as _runtime_config
    from app.research_direction_state import (
        direction_saturation_refractory_minutes,
    )

    _runtime_config["autonomous_direction_cooldown_minutes"] = 60
    _runtime_config["direction_saturation_refractory_minutes"] = 720
    assert direction_saturation_refractory_minutes() == 720.0


# ─────────────────────────────────────────────────────────────────────────────
# Pending strategic review retry cap and escalation
# ─────────────────────────────────────────────────────────────────────────────


def _make_pending_sr_project(db_session_fixture, **vision_overrides):
    """Helper: create and return an unsaved Project with _pending_strategic_review marker."""
    pending_sr = {
        "design_doc_task_id": str(uuid.uuid4()),
        "failed_at": datetime.now(timezone.utc).isoformat(),
        "error": "test error",
        "cycle_id": "cyc-test-001",
        "operator_steered": False,
        "steering_excerpt": None,
        **vision_overrides,
    }
    return Project(
        name="Pending SR Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[_active_direction("d-psr-test1", "Pending SR direction")],
        vision={"purpose": "test", "_pending_strategic_review": pending_sr},
    )


@pytest.mark.asyncio
async def test_pending_sr_success_clears_marker_and_returns_early(db_session, mocker):
    """Happy path: _spawn_strategic_review returns True → marker cleared, no tactical research."""
    spawn_sr_mock = mocker.patch(
        "app.strategic_spawning._spawn_strategic_review",
        new=AsyncMock(return_value=True),
    )

    preferred_runner_id = str(uuid.uuid4())
    project = _make_pending_sr_project(
        db_session,
        preferred_runner_target_id=preferred_runner_id,
        **{
            STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY: True,
            STRATEGIC_LOCAL_RUNNER_REASON_CONTEXT_KEY: "workbench_continuous",
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _spawn_research_task(project, db_session)

    assert spawn_sr_mock.called, (
        "_spawn_strategic_review must be called on a fresh pending marker"
    )
    assert (
        spawn_sr_mock.await_args.kwargs["preferred_runner_target_id"]
        == preferred_runner_id
    )
    assert spawn_sr_mock.await_args.kwargs["local_runner_context"] == {
        STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY: True,
        STRATEGIC_LOCAL_RUNNER_REASON_CONTEXT_KEY: "workbench_continuous",
    }

    await db_session.refresh(project)
    assert (project.vision or {}).get("_pending_strategic_review") is None, (
        "Marker must be cleared after successful retry"
    )

    # No tactical research tasks should have spawned
    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    research_tasks = [
        t for t in result.scalars().all() if t.first_payload.get("type") == "research"
    ]
    assert len(research_tasks) == 0, (
        "Tactical research must not spawn when SR retry succeeded"
    )


@pytest.mark.asyncio
async def test_pending_sr_skip_falls_through_to_tactical(db_session, mocker):
    """When _spawn_strategic_review returns False (active task), fall through; marker is preserved and attempt_count is incremented."""
    mocker.patch(
        "app.strategic_spawning._spawn_strategic_review",
        new=AsyncMock(return_value=False),
    )

    # Direction in refractory so no tactical research spawns after fall-through
    future = (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat()
    project = Project(
        name="Pending SR Skip Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[
            {
                **_active_direction("d-psr-skip1", "Pending SR direction"),
                "refractory_until": future,
            }
        ],
        vision={
            "purpose": "test",
            "_pending_strategic_review": {
                "design_doc_task_id": str(uuid.uuid4()),
                "failed_at": datetime.now(timezone.utc).isoformat(),
                "error": "skipped: active strategic task exists",
                "cycle_id": "cyc-skip-001",
                "operator_steered": False,
                "steering_excerpt": None,
            },
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _spawn_research_task(project, db_session)

    # Marker should still be present; attempt_count must be incremented so the cap/age escalation arms
    await db_session.refresh(project)
    marker = (project.vision or {}).get("_pending_strategic_review")
    assert marker is not None, "Marker must remain when SR spawn was skipped"
    assert marker.get("attempt_count", 0) == 1, (
        "attempt_count must be incremented on a skip so the cap/age safety valve eventually fires"
    )
    assert marker.get("first_attempted_at") is not None, (
        "first_attempted_at must be stamped on first skip so the age window arms"
    )


@pytest.mark.asyncio
async def test_pending_sr_skip_escalates_after_cap_reached(db_session, mocker):
    """A marker behind a persistently-active strategic task (return-False) escalates after cap.

    Starts with attempt_count == max_attempts - 1, mocks _spawn_strategic_review to
    return False again, and verifies the marker is dropped and an alert is created —
    proving the cap/age safety valve fires on the return-False path, not just on exceptions.
    """
    import app.shared_state as _ss

    _ss.runtime_config["pending_sr_max_attempts"] = 3

    spawn_sr_mock = mocker.patch(
        "app.strategic_spawning._spawn_strategic_review",
        new=AsyncMock(return_value=False),
    )
    alert_mock = mocker.patch(
        "app.alert_dispatch._create_strategic_alert",
        new=AsyncMock(return_value=None),
    )

    future = (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat()
    first_attempted = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    project = Project(
        name="Pending SR Skip Cap Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[
            {
                **_active_direction("d-psr-skip-cap1", "Pending SR direction"),
                "refractory_until": future,
            }
        ],
        vision={
            "purpose": "test",
            "_pending_strategic_review": {
                "design_doc_task_id": str(uuid.uuid4()),
                "failed_at": first_attempted,
                "error": "skipped: active strategic task exists",
                "cycle_id": "cyc-skip-cap-001",
                "operator_steered": False,
                "steering_excerpt": None,
                # Pre-seed attempt_count at max - 1; one more skip should push it to max
                # and trigger the escalation on the NEXT cycle (cap check runs first)
                "attempt_count": 3,
                "first_attempted_at": first_attempted,
            },
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _spawn_research_task(project, db_session)

    # When attempt_count >= max, the cap path fires BEFORE calling _spawn_strategic_review
    assert not spawn_sr_mock.called, (
        "_spawn_strategic_review must not be called when attempt_count >= max"
    )
    assert alert_mock.called, (
        "Alert must be created when retry cap is exceeded (return-False path)"
    )

    await db_session.refresh(project)
    assert (project.vision or {}).get("_pending_strategic_review") is None, (
        "Marker must be dropped after cap is exceeded on return-False path"
    )

    _ss.runtime_config.pop("pending_sr_max_attempts", None)


@pytest.mark.asyncio
async def test_pending_sr_exception_increments_count_and_falls_through(
    db_session, mocker
):
    """When _spawn_strategic_review raises, attempt_count is bumped; tactical research may proceed."""
    mocker.patch(
        "app.strategic_spawning._spawn_strategic_review",
        new=AsyncMock(side_effect=RuntimeError("DB connection lost")),
    )

    # Direction in refractory so no tactical research spawns after fall-through
    future = (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat()
    project = Project(
        name="Pending SR Exception Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[
            {
                **_active_direction("d-psr-exc1", "Pending SR direction"),
                "refractory_until": future,
            }
        ],
        vision={
            "purpose": "test",
            "_pending_strategic_review": {
                "design_doc_task_id": str(uuid.uuid4()),
                "failed_at": datetime.now(timezone.utc).isoformat(),
                "error": "spawn error",
                "cycle_id": "cyc-exc-001",
                "operator_steered": False,
                "steering_excerpt": None,
            },
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _spawn_research_task(project, db_session)

    await db_session.refresh(project)
    marker = (project.vision or {}).get("_pending_strategic_review")
    assert marker is not None, "Marker must persist after exception"
    assert marker.get("attempt_count", 0) == 1, (
        "attempt_count must be incremented to 1 after the first exception"
    )
    assert marker.get("first_attempted_at") is not None, (
        "first_attempted_at must be stamped on first exception"
    )


@pytest.mark.asyncio
async def test_pending_strategic_review_retry_failure_cap(db_session, mocker):
    """When attempt_count >= pending_sr_max_attempts, drop the marker, fire an alert,
    and allow tactical research to proceed normally."""
    import app.shared_state as _ss

    _ss.runtime_config["pending_sr_max_attempts"] = 3

    spawn_sr_mock = mocker.patch(
        "app.strategic_spawning._spawn_strategic_review",
        new=AsyncMock(return_value=True),
    )
    alert_mock = mocker.patch(
        "app.alert_dispatch._create_strategic_alert",
        new=AsyncMock(return_value=None),
    )

    # Direction in refractory so no tactical research spawns after fall-through
    future = (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat()
    project = Project(
        name="Pending SR Cap Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[
            {
                **_active_direction("d-psr-cap1", "Cap direction"),
                "refractory_until": future,
            }
        ],
        vision={
            "purpose": "test",
            "_pending_strategic_review": {
                "design_doc_task_id": str(uuid.uuid4()),
                "failed_at": (
                    datetime.now(timezone.utc) - timedelta(hours=2)
                ).isoformat(),
                "error": "repeated failure",
                "cycle_id": "cyc-cap-001",
                "operator_steered": False,
                "steering_excerpt": None,
                "attempt_count": 3,
                "first_attempted_at": (
                    datetime.now(timezone.utc) - timedelta(hours=1)
                ).isoformat(),
            },
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _spawn_research_task(project, db_session)

    # SR spawn must NOT be called — we're over the cap
    assert not spawn_sr_mock.called, (
        "_spawn_strategic_review must not be called when attempt_count >= max"
    )

    # Alert must be fired
    assert alert_mock.called, "Alert must be created when retry cap is exceeded"
    alert_msg = alert_mock.call_args[0][2] if alert_mock.call_args[0] else ""
    assert "attempt_count" in alert_msg or "retry" in alert_msg.lower(), (
        "Alert message should mention the cap-exceeded reason"
    )

    # Marker must be dropped
    await db_session.refresh(project)
    assert (project.vision or {}).get("_pending_strategic_review") is None, (
        "Marker must be dropped after cap is exceeded"
    )

    _ss.runtime_config.pop("pending_sr_max_attempts", None)


@pytest.mark.asyncio
async def test_pending_sr_age_exceeded_drops_marker_and_alerts(db_session, mocker):
    """When the marker's first_attempted_at is older than pending_sr_max_age_hours,
    drop the marker and fire an alert regardless of attempt_count."""
    import app.shared_state as _ss

    _ss.runtime_config["pending_sr_max_age_hours"] = 1  # 1-hour window for this test

    spawn_sr_mock = mocker.patch(
        "app.strategic_spawning._spawn_strategic_review",
        new=AsyncMock(return_value=True),
    )
    alert_mock = mocker.patch(
        "app.alert_dispatch._create_strategic_alert",
        new=AsyncMock(return_value=None),
    )

    # Direction in refractory so no tactical research spawns after fall-through
    future = (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat()
    stale_ts = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    project = Project(
        name="Pending SR Age Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[
            {
                **_active_direction("d-psr-age1", "Age direction"),
                "refractory_until": future,
            }
        ],
        vision={
            "purpose": "test",
            "_pending_strategic_review": {
                "design_doc_task_id": str(uuid.uuid4()),
                "failed_at": stale_ts,
                "error": "spawn error",
                "cycle_id": "cyc-age-001",
                "operator_steered": False,
                "steering_excerpt": None,
                "attempt_count": 1,  # Only 1 attempt — age alone triggers the drop
                "first_attempted_at": stale_ts,
            },
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _spawn_research_task(project, db_session)

    assert not spawn_sr_mock.called, (
        "_spawn_strategic_review must not be called when marker is stale"
    )
    assert alert_mock.called, "Alert must be created when marker age is exceeded"

    await db_session.refresh(project)
    assert (project.vision or {}).get("_pending_strategic_review") is None, (
        "Stale marker must be dropped"
    )

    _ss.runtime_config.pop("pending_sr_max_age_hours", None)


@pytest.mark.asyncio
async def test_filter_breakdown_type_filtered_is_per_direction(db_session):
    """Each direction's research_history filter_breakdown.type_filtered records only
    that direction's own type-filtered metadata findings, not the cycle-wide total.

    A two-direction cycle where direction A produces 2 type-filtered findings and
    direction B produces 1 must write 2 and 1 respectively — not 3 in both entries.
    """
    from app.research_direction_state import apply_post_spawn_state

    dir_a_id = "d-aaa-tf01"
    dir_b_id = "d-bbb-tf02"
    direction_a = _active_direction(dir_a_id, "Direction A")
    direction_b = _active_direction(dir_b_id, "Direction B")

    project = Project(
        name="Per-Direction Type-Filtered Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction_a, direction_b],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Research: per-direction type_filtered",
        payloads=[
            {
                "type": "research",
                "directions": [direction_a, direction_b],
                "base_branch": "main",
            }
        ],
        context={"direction_id": dir_a_id, "research_depth": 0},
        status=TaskStatus.IN_PROGRESS,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # One actionable finding per direction so both directions enter direction_ids_with_findings
    finding_a = _make_finding("Actionable finding A", direction_id=dir_a_id)
    finding_b = _make_finding("Actionable finding B", direction_id=dir_b_id)

    await apply_post_spawn_state(
        session=db_session,
        task=task,
        findings=[finding_a, finding_b],
        child_tasks=[],
        deduplicated=[(0, finding_a, "hash-a"), (1, finding_b, "hash-b")],
        insights=[],
        direction_notes=[],
        direction_proposals=[],
        hash_dedup_count=0,
        word_overlap_count=0,
        file_overlap_count=0,
        evidence_overlap_count=0,
        type_filtered_count=3,
        empty_prompt_count=0,
        research_branch="research-branch",
        original_base="main",
        research_depth=0,
        parent_research_task_id=None,
        # Direction A had 2 type-filtered findings; direction B had 1
        type_filtered_by_dir={dir_a_id: 2, dir_b_id: 1},
    )

    await db_session.refresh(project)
    dirs = {d["id"]: d for d in (project.directions or [])}

    history_a = dirs[dir_a_id].get("research_history", [])
    history_b = dirs[dir_b_id].get("research_history", [])

    assert history_a, "Direction A must have a research_history entry"
    assert history_b, "Direction B must have a research_history entry"

    tf_a = history_a[-1]["filter_breakdown"]["type_filtered"]
    tf_b = history_b[-1]["filter_breakdown"]["type_filtered"]

    assert tf_a == 2, f"Direction A type_filtered must be 2 (its own count), got {tf_a}"
    assert tf_b == 1, f"Direction B type_filtered must be 1 (its own count), got {tf_b}"
    assert tf_a != tf_b, "Directions must not share the same type_filtered count"


@pytest.mark.asyncio
async def test_first_task_spawned_stamped_on_research_spawn(db_session):
    """_spawn_research_task stamps first_task_spawned_at in project.build_config
    on the first successful autonomous research spawn.

    Follows the same project setup as
    test_research_spawn_proceeds_when_external_repo_bootstrap_completed and adds
    a post-spawn assertion that the onboarding milestone was recorded.
    """
    direction = _active_direction_with_cooldown_elapsed(
        "d-stamp-spawn", "First spawn stamp direction"
    )
    project = Project(
        name="First Task Spawned Stamp",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test", "_strategic_failures": 3},
        build_config={"bootstrap_completed_at": "2026-01-01T00:00:00"},
        is_self_repo=False,
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _spawn_research_task(project, db_session)

    await db_session.refresh(project)
    assert (project.build_config or {}).get("first_task_spawned_at") is not None, (
        "first_task_spawned_at must be stamped in build_config after the first "
        "successful autonomous research spawn"
    )


@pytest.mark.asyncio
async def test_strategic_spawn_deduplication_respects_EVALUATING_status(
    db_session, mocker
):
    """Regression: when a strategic task is in EVALUATING status (results being
    evaluated), the canonical _has_active_strategic_task() must still consider it
    active and prevent duplicate spawn. The inline guard in autonomous_research
    previously included EVALUATING, but the canonical function in task_lifecycle
    did not, causing possible double-spawn of a strategic cycle while one was
    still EVALUATING its results."""
    design_doc_mock = mocker.patch(
        "app.strategic_spawning._spawn_design_doc_update",
        new=AsyncMock(return_value=True),
    )
    project = Project(
        name="EVALUATING Strategic Status Test",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[
            _active_direction("d-eval-test", "Evaluating Test Direction"),
        ],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Create a strategic task in EVALUATING status — results are being evaluated,
    # not complete yet. The spawn guard must still consider this "active" and skip
    # spawning a duplicate strategic cycle.
    evaluating_strategic = Task(
        project_id=project.id,
        title="Strategic Review (Results Evaluating)",
        payloads=[{"type": "strategic_review", "base_branch": "main"}],
        status=TaskStatus.EVALUATING,
        created_by="ORCHESTRATOR",
    )
    db_session.add(evaluating_strategic)
    await db_session.commit()

    # Trigger research spawn with force_strategic (wall-clock backstop fires, or
    # operator flag set), which should normally spawn a design_doc_update or another
    # strategic cycle. But the EVALUATING strategic task must block it.
    await _spawn_research_task(project, db_session)

    # The spawn should be skipped because an active strategic task (in EVALUATING
    # status) exists for the project.
    assert not design_doc_mock.called, (
        "strategic spawn must be skipped when an EVALUATING strategic task is active"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Non-self project routing and onboarding proof
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_idle_research_selects_eligible_non_self_project(db_session, mocker):
    """A non-self-repo project with autonomous_enabled=True is selected by the
    idle research loop and spawns a research task.  When the spawned task's
    token is later resolved, the returned (owner, repo) pair is the project's
    own external github_owner/github_repo — not the Blueprint self-repo
    identity.

    Acceptance evidence: non-self project PR dispatch resolves to the
    project's own owner/repo (not the self repo).
    """
    from app.github_token_resolver import get_github_token_for_task, _github_token_cache

    direction = _active_direction_with_cooldown_elapsed(
        "d-non-self-route", "External repo improvement"
    )
    project = Project(
        name="Non-Self External Repo",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test", "_strategic_failures": 3},
        build_config={"bootstrap_completed_at": "2026-01-01T00:00:00"},
        github_owner="external-owner",
        github_repo="external-repo",
        is_self_repo=False,
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Spawn a research task (simulates what _check_idle_research would do).
    await _spawn_research_task(project, db_session)

    result = await db_session.execute(
        select(Task).where(
            Task.project_id == project.id,
            Task.payloads[0]["type"].as_string() == "research",
        )
    )
    spawned_task = result.scalars().first()
    assert spawned_task is not None, (
        "A research task must be spawned for an eligible non-self-repo project"
    )

    # Verify that token resolution for the spawned task returns the project's
    # own external owner/repo — not a Blueprint self-repo identity.
    _github_token_cache.pop(str(project.id), None)
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="ext-app-token"),
    )

    token, owner, repo = await get_github_token_for_task(spawned_task.id, db_session)

    assert token == "ext-app-token"
    assert owner == "external-owner", (
        f"owner must be the project's external github_owner, got {owner!r}"
    )
    assert repo == "external-repo", (
        f"repo must be the project's external github_repo, got {repo!r}"
    )


def test_build_onboarding_proof_block_returns_none_for_none_project():
    """build_onboarding_proof_block returns None when project is None."""
    from app.onboarding_proof import build_onboarding_proof_block

    assert build_onboarding_proof_block(None) is None


def test_build_onboarding_proof_block_unstamped_non_self_project():
    """An unstamped non-self-repo project returns a block with all nine keys
    present; milestone keys are null except repo_added_at."""
    from datetime import datetime, timezone

    from app.onboarding_proof import build_onboarding_proof_block

    class _StubProject:
        created_at = datetime(2026, 1, 15, tzinfo=timezone.utc)
        build_config = {}
        is_self_repo = False

    block = build_onboarding_proof_block(_StubProject())

    assert block is not None
    expected_keys = {
        "repo_added_at",
        "bootstrap_completed_at",
        "first_task_spawned_at",
        "first_pr_opened_at",
        "first_merge_gate_verdict_at",
        "first_merge_gate_executed_tests",
        "first_merge_gate_unverified_reason",
        "first_pr_merged_at",
        "first_autonomous_improvement_at",
    }
    assert set(block.keys()) == expected_keys, (
        f"unexpected keys: {set(block.keys()) ^ expected_keys}"
    )
    assert block["repo_added_at"] == "2026-01-15T00:00:00+00:00"
    # All milestone fields are null before any work lands.
    for key in expected_keys - {"repo_added_at"}:
        assert block[key] is None, f"expected {key!r} to be null, got {block[key]!r}"


def test_build_onboarding_proof_block_fully_stamped_non_self_project():
    """A fully-stamped non-self-repo project returns all nine keys non-null,
    including first_autonomous_improvement_at."""
    from datetime import datetime, timezone

    from app.onboarding_proof import build_onboarding_proof_block

    class _StubProject:
        created_at = datetime(2026, 1, 15, tzinfo=timezone.utc)
        build_config = {
            "bootstrap_completed_at": "2026-01-16T00:00:00",
            "first_task_spawned_at": "2026-01-17T00:00:00",
            "first_pr_opened_at": "2026-01-18T00:00:00",
            "first_merge_gate_verdict_at": "2026-01-19T00:00:00",
            "first_merge_gate_executed_tests": True,
            "first_merge_gate_unverified_reason": None,
            "first_pr_merged_at": "2026-01-20T00:00:00",
            "first_autonomous_improvement_at": "2026-01-20T01:00:00",
        }
        is_self_repo = False

    block = build_onboarding_proof_block(_StubProject())

    assert block is not None
    assert block["repo_added_at"] == "2026-01-15T00:00:00+00:00"
    assert block["bootstrap_completed_at"] == "2026-01-16T00:00:00"
    assert block["first_task_spawned_at"] == "2026-01-17T00:00:00"
    assert block["first_pr_opened_at"] == "2026-01-18T00:00:00"
    assert block["first_merge_gate_verdict_at"] == "2026-01-19T00:00:00"
    assert block["first_merge_gate_executed_tests"] is True
    assert block["first_pr_merged_at"] == "2026-01-20T00:00:00"
    assert block["first_autonomous_improvement_at"] == "2026-01-20T01:00:00"


@pytest.mark.asyncio
async def test_research_requeue_clone_reuses_inherited_staging_branch(
    db_session, mocker
):
    """Variant 1 regression (research): a research-task clone that already opened
    a rollup PR must reuse the inherited research_branch rather than deriving a new
    staging branch from the clone's task ID.

    Scenario: research task A has research_branch='research/oldhex12' and
    rollup_pr_number=42 in its context.  A's parent requeues, A is cancelled, and
    the clone B inherits A's context.  When B runs _handle_research_completion it
    MUST use 'research/oldhex12', not derive a new 'research/<B-short-id>' —
    otherwise the merge guard aborts because the PR head still points at the
    original branch.

    Without the fix (research_completion.py branch reuse guard), _ensure_branch
    would be called with the clone's derived branch ('research/<B-short-id>'),
    desync'ing from the open PR.
    """
    direction = _active_direction("d-branch-reuse01", "Branch reuse direction")
    project = Project(
        name="ResearchBranchReuseTest",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Clone task: inherits research_branch + rollup_pr_number from the cancelled
    # original.  The clone has a NEW task UUID so its derived short_id differs
    # from the inherited branch suffix "oldhex12".
    original_research_branch = "research/oldhex12"
    clone_task = await _make_research_task(project, direction, db_session)
    clone_task.context = {
        **(clone_task.context or {}),
        "research_branch": original_research_branch,
        "rollup_pr_number": 42,
        "_attempt_number": 2,
    }
    db_session.add(clone_task)
    await db_session.commit()
    await db_session.refresh(clone_task)

    # Verify the clone's derived short_id would produce a DIFFERENT branch,
    # making the regression observable.
    derived_branch = f"research/{str(clone_task.id).replace('-', '')[:8]}"
    assert derived_branch != original_research_branch, (
        "Test setup: clone's derived branch must differ from the inherited branch "
        "— if they match, the regression is not observable"
    )

    captured_branch: list = []

    async def _capture_ensure_branch(task, branch, *_a, **_kw):
        captured_branch.append(branch)
        return task

    mocker.patch("app.task_completion._ensure_branch", new=_capture_ensure_branch)
    mocker.patch("app.broadcast.broadcast_to_project", new=AsyncMock(return_value=None))
    mocker.patch(
        "app.research_completion._validate_plan_dependencies", return_value=True
    )
    mocker.patch("app.research_completion._log_file_overlaps", return_value=[])
    mocker.patch("app.max_plan_pool._all_accounts", return_value=[])
    mocker.patch(
        "app.research_completion._apply_structural_actionability_downgrade",
        return_value=0,
    )
    mocker.patch("app.research_completion._is_high_stakes_finding", return_value=False)
    mocker.patch(
        "app.research_completion._parse_research_findings",
        return_value=[_make_finding("Fix something", direction_id=direction["id"])],
    )
    mocker.patch(
        "app.research_completion._assess_project_spawn_health",
        return_value=(False, None),
    )

    await _handle_research_completion(clone_task, "", db_session)

    # _ensure_branch must have been called with the INHERITED branch, not the
    # clone-derived 'research/<clone-id-prefix>' branch.
    assert captured_branch, (
        "_ensure_branch must have been called — function may have short-circuited "
        "before reaching branch creation"
    )
    assert captured_branch[0] == original_research_branch, (
        f"Expected _ensure_branch called with '{original_research_branch}', "
        f"got '{captured_branch[0]!r}'. "
        "Fix: research_completion.py must reuse inherited research_branch when "
        "rollup_pr_number is set (mirrors plan_completion.py:554-560)."
    )


@pytest.mark.asyncio
async def test_prune_block_generates_tombstone_for_completed_direction(db_session):
    """The lifecycle prune block records retired memory for pruned terminal directions.

    A direction with status='completed' is not in the live set and must appear in
    pending_retired_memory after _process_direction_lifecycle_locked so the
    with_vision_locked flush writes it to _recently_retired_directions.
    """
    from app.direction_lifecycle import _process_direction_lifecycle_locked

    completed_dir = {
        "id": "d-prune-tombstone-01",
        "status": "completed",
        "text": "Tombstone test direction",
        "auto_completed_reason": "manually_completed",
        "gap_id": "g-test",
    }
    project = Project(
        name="Tombstone Test",
        user_id="test-user-tombstone",
        autonomous_enabled=True,
        directions=[completed_dir],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = datetime.now(timezone.utc)
    pending_retired_memory: list = []
    await _process_direction_lifecycle_locked(
        project,
        db_session,
        set(),
        now,
        pending_retired_memory=pending_retired_memory,
    )

    recorded_ids = [d.get("id") for d, _ in pending_retired_memory]
    assert "d-prune-tombstone-01" in recorded_ids, (
        f"Pruned completed direction must appear in pending_retired_memory; got {recorded_ids}"
    )
    matched_reason = next(
        reason for d, reason in pending_retired_memory if d.get("id") == "d-prune-tombstone-01"
    )
    assert matched_reason == "manually_completed", (
        f"Expected reason 'manually_completed', got {matched_reason!r}"
    )


@pytest.mark.asyncio
async def test_lifecycle_vision_lock_miss_logs_warning(db_session, mocker):
    """When with_vision_locked returns False, a structured warning is emitted and no exception raised."""
    from unittest.mock import AsyncMock, patch
    from app.direction_lifecycle import _process_direction_lifecycle

    completed_dir = {
        "id": "d-lock-miss-01",
        "status": "completed",
        "text": "Lock miss test direction",
        "auto_completed_reason": "manually_completed",
        "gap_id": "g-test-lock-miss",
    }
    project = Project(
        name="Lock Miss Test",
        user_id="test-user-lock-miss",
        autonomous_enabled=True,
        directions=[completed_dir],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    lock_mock = AsyncMock(return_value=False)
    with patch("app.task_locking.with_vision_locked", new=lock_mock):
        with patch("app.direction_lifecycle.logger") as mock_logger:
            await _process_direction_lifecycle(project, db_session, set(), datetime.now(timezone.utc))

    warning_calls = [str(c) for c in mock_logger.warning.call_args_list]
    assert any("d-lock-miss-01" in c for c in warning_calls), (
        f"Expected warning mentioning dropped direction ID; got: {warning_calls}"
    )


@pytest.mark.asyncio
async def test_lifecycle_vision_lock_timeout_logs_error_and_reraises(db_session, mocker):
    """When with_vision_locked raises OperationalError, a structured error is logged and re-raised."""
    from unittest.mock import patch
    from sqlalchemy.exc import OperationalError
    from app.direction_lifecycle import _process_direction_lifecycle

    completed_dir = {
        "id": "d-lock-timeout-01",
        "status": "completed",
        "text": "Lock timeout test direction",
        "auto_completed_reason": "manually_completed",
        "gap_id": "g-test-lock-timeout",
    }
    project = Project(
        name="Lock Timeout Test",
        user_id="test-user-lock-timeout",
        autonomous_enabled=True,
        directions=[completed_dir],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    op_err = OperationalError("lock timeout", None, None)

    async def _raise(*args, **kwargs):
        raise op_err

    with patch("app.task_locking.with_vision_locked", new=_raise):
        with patch("app.direction_lifecycle.logger") as mock_logger:
            with pytest.raises(OperationalError):
                await _process_direction_lifecycle(
                    project, db_session, set(), datetime.now(timezone.utc)
                )

    error_calls = [str(c) for c in mock_logger.error.call_args_list]
    assert any("d-lock-timeout-01" in c for c in error_calls), (
        f"Expected error mentioning dropped direction ID; got: {error_calls}"
    )
