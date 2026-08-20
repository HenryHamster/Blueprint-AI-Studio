"""strategic_spawning: summary blocks and the spawn_task operation.

Covers the efficiency-snapshot summary, direction-linked knowledge in the
strategist payload, the direction-free ``spawn_task`` dispatch path, CI
compliance in the review payload, and the reconciliation-sections fixture
contract.

Shared scaffolding lives in ``_strategic_spawning_test_helpers.py``.
"""

import json
import uuid
from datetime import datetime, timezone, timedelta

import pytest
from unittest.mock import AsyncMock, patch
from sqlalchemy import select

import app.drift_detection  # noqa: F401 — registers DirectionDriftSnapshot in SQLModel.metadata for create_all
import app.shared_state as _shared_state
from app.models import (
    DiscoveryCycle,
    Project,
    StrategicSpawnRequest,
    StrategicWatch,
    Task,
    TaskStatus,
    MessageType,
    ProjectMessage,
)
from app.strategic_spawning import (
    _spawn_strategic_review,
    _STRATEGIC_REVIEW_SCHEMA,
    _DESIGN_DOC_UPDATE_SCHEMA,
)
from app.payload_schemas import pack_under_budget
from _strategic_spawning_test_helpers import (  # noqa: F401 — _ensure_test_account_pool is an autouse fixture
    _ensure_test_account_pool,
    _make_project,
    _patch_spawning_deps,
    _seed_legacy_cycles,
    _select_agent_tasks,
)


async def _seed_knowledge_doc(session, project, *, title="KDoc", direction_id=None):
    """Persist one KnowledgeDocument for the project (FK source_task_id needs a task)."""
    from app.models import KnowledgeDocument

    task = Task(
        project_id=project.id,
        title="src",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "web_research", "topic": "t", "base_branch": "main"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)
    doc = KnowledgeDocument(
        project_id=project.id,
        source_task_id=task.id,
        title=title,
        summary="A prior knowledge summary.",
        body="# Body\n\n" + ("x" * 50),
        topic="auth",
        direction_id=direction_id,
        source_kind="web_research",
    )
    session.add(doc)
    await session.commit()
    await session.refresh(doc)
    return doc


def test_summarize_efficiency_snapshot_includes_dominant_failure_class():
    """strategist efficiency-snapshot summary forwards the dominant non-MERGED
    failure-class LLM cost (highest-cost failure mode excluding merged tasks)."""
    from app.strategic_spawning import _summarize_efficiency_snapshot

    snapshot = {
        "axes": {
            "cost_per_merged_pr": {
                "current": 1.5,
                "baseline": 2.0,
                "drift_pct": -25.0,
                "history": [],
            }
        },
        "baseline_captured_at": "2026-04-05T00:00:00",
        "last_snapshot_at": "2026-05-01T00:00:00",
        "llm_cost_attribution": {
            "total_llm_usd": 100.0,
            "rework_share": 0.15,
            "by_model": [
                {"provider": "anthropic", "model": "claude-opus", "cost_usd": 50.0}
            ],
            "by_task_type": [{"task_type": "task_edit", "cost_usd": 75.0}],
            # by_failure_class sorted by cost descending: context_limit (80) > MERGED (15) > import_error (5)
            "by_failure_class": [
                {"failure_class": "context_limit", "task_count": 2, "total_cost_usd": 80.0},
                {"failure_class": "MERGED", "task_count": 3, "total_cost_usd": 15.0},
                {"failure_class": "import_error", "task_count": 1, "total_cost_usd": 5.0},
            ],
        },
    }
    result = _summarize_efficiency_snapshot(snapshot)
    assert "llm_cost_attribution" in result
    attr = result["llm_cost_attribution"]
    assert attr["dominant_failure_class"] == {
        "failure_class": "context_limit",
        "task_count": 2,
        "total_cost_usd": 80.0,
    }


def test_summarize_efficiency_snapshot_forwards_format_miss_attribution():
    """strategist efficiency payload includes per-handler format-miss attribution.

    dominant_schema / dominant_reason / dominant_share are extracted from the
    format_miss_by_schema_and_reason cross-product and forwarded compactly so a
    format-compliance regression can be attributed to the producing handler
    without phantom low-sample noise driving the global delta.
    """
    from app.strategic_spawning import _summarize_efficiency_snapshot

    snapshot = {
        "axes": {},
        "baseline_captured_at": "2026-04-05T00:00:00",
        "last_snapshot_at": "2026-05-01T00:00:00",
        "format_compliance": {
            "format_compliance_rate": {
                "current": 0.88,
                "baseline": 0.92,
                "drift_pct": -4.3,
                "history": [],
            },
            "self_repair_success_rate": {
                "current": 0.70,
                "baseline": 0.75,
                "drift_pct": -6.7,
                "history": [],
            },
            "repair_attempts": 20,
            # strategic handler has 10 misses (8 schema_missing + 2 extra_key),
            # research handler has 3 misses (all schema_missing).
            "format_miss_by_schema_and_reason": {
                "strategic": {"schema_missing": 8, "extra_key": 2},
                "research": {"schema_missing": 3},
            },
        },
    }
    result = _summarize_efficiency_snapshot(snapshot)
    assert "format_compliance" in result
    fc = result["format_compliance"]

    # dominant_schema is "strategic" (10 misses vs 3)
    assert fc["dominant_schema"] == "strategic"
    # dominant_reason within "strategic" is "schema_missing" (8 vs 2)
    assert fc["dominant_reason"] == "schema_missing"
    # dominant_share = 10 / 13 ≈ 0.769
    assert fc["dominant_share"] == round(10 / 13, 3)

    # rates still forwarded
    assert fc["format_compliance_rate"]["current"] == 0.88
    assert fc["repair_attempts"] == 20


def test_summarize_efficiency_snapshot_format_miss_absent_no_dominant():
    """When format_miss_by_schema_and_reason is absent, no dominant_* keys are emitted."""
    from app.strategic_spawning import _summarize_efficiency_snapshot

    snapshot = {
        "axes": {},
        "format_compliance": {
            "format_compliance_rate": {
                "current": 0.90,
                "baseline": 0.90,
                "drift_pct": 0.0,
                "history": [],
            },
            "repair_attempts": 5,
            # no format_miss_by_schema_and_reason key
        },
    }
    result = _summarize_efficiency_snapshot(snapshot)
    fc = result["format_compliance"]
    assert "dominant_schema" not in fc
    assert "dominant_reason" not in fc
    assert "dominant_share" not in fc


# ── direction-linked knowledge in strategist payload ──────────────────────────


@pytest.mark.asyncio
async def test_strategist_context_surfaces_direction_linked_knowledge(db_session):
    """strategic_review outcome_evaluation surfaces direction_knowledge for
    directions that have linked knowledge documents."""
    direction_id = "d-linked-knowledge"
    project = _make_project(
        directions=[
            {
                "id": direction_id,
                "status": "active",
                "text": "Direction with linked knowledge",
                "priority": "high",
                "research_history": [{"prs_merged": 1, "prs_rejected": 0}],
            }
        ],
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)
    await _seed_knowledge_doc(
        db_session,
        project,
        title="Auth research findings",
        direction_id=direction_id,
    )

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    payload = next(
        t.payloads[0]
        for t in result.scalars().all()
        if t.payloads[0]["type"] == "strategic_review"
    )

    outcome_eval = payload["outcome_evaluation"]
    direction_outcomes = outcome_eval["directions"]
    matched = next((d for d in direction_outcomes if d["id"] == direction_id), None)
    assert matched is not None, f"direction {direction_id!r} not in outcome_evaluation"
    assert "direction_knowledge" in matched, (
        "direction_knowledge missing from direction outcome — linked knowledge not surfaced"
    )
    assert matched["direction_knowledge"][0]["title"] == "Auth research findings"


# ── spawn_task op: direction-free implementation dispatch ─────────────────────
#
# spawn_task lets the strategist dispatch bounded work with NO strategic
# direction. These tests
# exercise the production handler in routers/admin_agent_ops.py end-to-end over
# the real /api/agent/strategic endpoint and assert direction-free provenance.


async def _make_strategic_review_task(db_session):
    """Create a project + IN_PROGRESS strategic_review task; return
    (project, review_id) for spawn_task endpoint tests."""
    project = Project(name="P", user_id="u1")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    review = Task(
        project_id=project.id,
        user_id="u1",
        task_type="strategic_review",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"command": "review"}],
    )
    db_session.add(review)
    await db_session.commit()
    await db_session.refresh(review)
    return project, str(review.id)


async def _post_spawn_task(
    db_session,
    task_id,
    *,
    title,
    prompt,
    spawn_type="create",
    expect_status=200,
    extra=None,
):
    """POST a strategic spawn_task op against the test app over the real endpoint.

    Returns the parsed JSON body (success body on 200, FastAPI error envelope on
    an expected error status). ``extra`` merges additional fields into the spawn
    data (e.g. the pr_number + branch a fix spawn used to carry)."""
    from httpx import AsyncClient, ASGITransport
    from app.main import app
    from app.database import get_session
    from app.routers.agent import verify_agent_token

    async def _override_session():
        yield db_session

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[verify_agent_token] = lambda: {
        "sub": task_id,
        "task_type": "strategic_review",
    }
    data = {"type": spawn_type, "title": title, "prompt": prompt}
    if extra:
        data.update(extra)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": "Bearer test-token"},
        ) as ac:
            resp = await ac.post(
                "/api/agent/strategic",
                json={
                    "op": "spawn_task",
                    "task_id": task_id,
                    "data": data,
                },
            )
        assert resp.status_code == expect_status, resp.text
        return resp.json()
    finally:
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(verify_agent_token, None)


async def _post_strategic_operation(db_session, task_id, *, op, data, expect_status=200):
    from httpx import ASGITransport, AsyncClient

    from app.database import get_session
    from app.main import app
    from app.routers.agent import verify_agent_token

    async def _override_session():
        yield db_session

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[verify_agent_token] = lambda: {
        "sub": task_id,
        "task_type": "strategic_review",
    }
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": "Bearer test-token"},
        ) as ac:
            response = await ac.post(
                "/api/agent/strategic",
                json={"op": op, "task_id": task_id, "data": data},
            )
        assert response.status_code == expect_status, response.text
        return response.json()
    finally:
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(verify_agent_token, None)


@pytest.mark.asyncio
async def test_strategic_compute_preview_respects_project_local_runner_policy(db_session):
    project, review_id = await _make_strategic_review_task(db_session)
    project.build_config = {
        "resource_envelope": {"require_local_runner": True}
    }
    db_session.add(project)
    await db_session.commit()

    body = await _post_strategic_operation(
        db_session,
        review_id,
        op="spawn_task",
        data={
            "type": "compute_experiment",
            "title": "Run local benchmark",
            "prompt": "Run the benchmark on the required local runner.",
            "command": "python benchmark.py",
            "spend_cap_usd": 50,
            "deadline_seconds": 600,
            "preview_only": True,
        },
    )

    decision = body["preview"]["monetary_decision"]
    assert decision["execution_mode"] == "local_only"
    assert decision["billable_estimate_usd"] == 0.0
    assert decision["requires_monetary_approval"] is False
    assert decision["reason_code"] == "project_requires_local_runner"


@pytest.mark.asyncio
async def test_cancel_deferred_spawn_keeps_long_reason_out_of_bounded_column(db_session):
    project, review_id = await _make_strategic_review_task(db_session)
    request = StrategicSpawnRequest(
        project_id=project.id,
        parent_task_id=uuid.UUID(review_id),
        idempotency_key="cancel-long-reason",
        task_type="create",
        specification={},
        specification_hash="0" * 64,
        state="deferred",
        admission_reason="burst_cap_reached",
    )
    db_session.add(request)
    await db_session.commit()

    reason = "x" * 500
    body = await _post_strategic_operation(
        db_session,
        review_id,
        op="cancel_task",
        data={"request_id": str(request.id), "reason": reason},
    )

    assert body["request_state"] == "cancelled"
    await db_session.refresh(request)
    assert request.admission_reason == "cancelled_by_strategic"
    assert request.policy_snapshot["cancel_reason"] == reason


@pytest.mark.asyncio
async def test_spawn_task_creates_task_without_direction_id(db_session):
    """spawn_task with a valid implementation type (create) creates a QUEUED task
    carrying created_by=STRATEGIC_AGENT provenance, and with
    NO direction_id anywhere — neither in context nor in the payload — so it can
    never surface in any direction portfolio logic."""
    _project, review_id = await _make_strategic_review_task(db_session)

    body = await _post_spawn_task(
        db_session,
        review_id,
        title="Fix red CI test on automain",
        prompt="The launch test clamps the spend cap; fix the assertion.",
        spawn_type="create",
    )
    assert body["status"] == "ok"
    assert body["op"] == "spawn_task"
    assert body["type"] == "create"
    assert body["deduplicated"] is False
    assert body["priority"] == 50

    spawned = (
        await db_session.execute(select(Task).where(Task.id == body["task_id"]))
    ).scalar_one()
    assert spawned.status == TaskStatus.QUEUED
    assert spawned.created_by == "STRATEGIC_AGENT"
    assert spawned.priority == 50
    assert spawned.first_payload.get("type") == "create"

    # The canonical operation creates a normal direction-free task.
    ctx = spawned.context or {}
    assert ctx.get("spawned_by") == "strategic_direct"
    assert ctx.get("parent_strategic_task_id") == review_id
    assert "direction_id" not in ctx

    # The payload itself carries no direction_id key (direction-free by construction).
    assert "direction_id" not in spawned.first_payload


@pytest.mark.asyncio
async def test_spawn_task_inherits_low_direction_scheduler_priority(db_session):
    """Resident roots use direction intent instead of an unconditional P100."""

    project, review_id = await _make_strategic_review_task(db_session)
    project.directions = [
        {
            "id": "d-low-priority",
            "status": "active",
            "priority": "low",
            "text": "Useful work which should yield to higher-priority projects",
        }
    ]
    db_session.add(project)
    await db_session.commit()

    body = await _post_strategic_operation(
        db_session,
        review_id,
        op="spawn_task",
        data={
            "type": "create",
            "title": "Implement low-priority improvement",
            "prompt": "Implement the bounded improvement.",
            "direction_id": "d-low-priority",
            "idempotency_key": "low-priority-work-unit",
        },
    )

    assert body["priority"] == 0
    spawned = await db_session.get(Task, uuid.UUID(body["task_id"]))
    assert spawned.priority == 0


@pytest.mark.asyncio
async def test_spawn_task_routes_research_through_unified_policy(db_session):
    _project, review_id = await _make_strategic_review_task(db_session)

    body = await _post_spawn_task(
        db_session,
        review_id,
        title="Investigate something",
        prompt="research the codebase",
        spawn_type="research",
    )
    assert body["status"] == "ok"
    assert body["op"] == "spawn_task"
    spawned = await db_session.get(Task, uuid.UUID(body["task_id"]))
    assert spawned.discovery_cycle_id is None
    assert (
        await db_session.execute(
            select(StrategicWatch).where(
                StrategicWatch.target_task_id == spawned.id
            )
        )
    ).scalar_one().state == "pending"


@pytest.mark.asyncio
async def test_direction_research_rejects_second_root_without_raw_wake(db_session):
    """One Strategic root owns a direction cycle; web research cannot join as a peer.

    A web-research task may be subordinate work spawned beneath the owning
    research parent. A second direct Strategic spawn has the Strategic review as
    its parent, however, so accepting it would create a second full-headroom
    cycle source and split carry/review ownership.
    """
    project, review_id = await _make_strategic_review_task(db_session)
    project.directions = [
        {
            "id": "d-research",
            "status": "active",
            "priority": "medium",
            "text": "Determine whether the bounded mechanism improves the product",
        }
    ]
    db_session.add(project)
    await db_session.commit()

    first = await _post_strategic_operation(
        db_session,
        review_id,
        op="spawn_task",
        data={
            "type": "research",
            "title": "Investigate the mechanism",
            "prompt": "Inspect the evidence and determine whether it is worth building.",
            "direction_id": "d-research",
            "idempotency_key": "research-mechanism",
        },
    )
    first_task = await db_session.get(Task, uuid.UUID(first["task_id"]))
    assert first_task.discovery_cycle_id is not None
    cycle = await db_session.get(DiscoveryCycle, first_task.discovery_cycle_id)
    assert cycle.source_research_task_id == first_task.id
    assert (
        await db_session.execute(
            select(StrategicWatch).where(
                StrategicWatch.target_task_id == first_task.id
            )
        )
    ).scalar_one_or_none() is None

    rejected = await _post_strategic_operation(
        db_session,
        review_id,
        op="spawn_task",
        data={
            "type": "web_research",
            "title": "Check the external frontier",
            "prompt": "Check only the evidence needed to challenge the same bet.",
            "direction_id": "d-research",
            "idempotency_key": "research-frontier",
            "watch": True,
        },
        expect_status=409,
    )
    assert "refusing to attach a second parent" in rejected["error"]["message"]

    cycle_tasks = (
        await db_session.execute(
            select(Task).where(Task.discovery_cycle_id == cycle.id)
        )
    ).scalars().all()
    assert [task.id for task in cycle_tasks] == [first_task.id]
    cycle_requests = (
        await db_session.execute(
            select(StrategicSpawnRequest).where(
                StrategicSpawnRequest.discovery_cycle_id == cycle.id
            )
        )
    ).scalars().all()
    assert [request.task_id for request in cycle_requests] == [first_task.id]
    assert (
        await db_session.execute(select(StrategicWatch))
    ).scalars().all() == []


@pytest.mark.asyncio
async def test_spawn_task_fix_type_without_pr_context_fails_cleanly(db_session):
    """``fix`` is not strategist-spawnable: it needs a live-PR target (pr_number
    + branch) the strategist cannot bind at spawn. Even a prompt-only spawn must
    be rejected with a clean 400 naming that context (not an opaque 500), and
    create NO task."""
    from sqlalchemy import func as _f

    _project, review_id = await _make_strategic_review_task(db_session)

    body = await _post_spawn_task(
        db_session,
        review_id,
        title="Fix the thing",
        prompt="patch the bug",
        spawn_type="fix",
        expect_status=400,
    )
    detail = body["error"]["message"]
    assert "pr_number" in detail
    assert "branch" in detail

    # No task was created by the failed validation.
    count = (
        await db_session.execute(
            select(_f.count())
            .select_from(Task)
            .where(Task.created_by == "STRATEGIC_AGENT")
        )
    ).scalar()
    assert count == 0


@pytest.mark.asyncio
async def test_spawn_task_fix_type_with_pr_context_is_rejected(db_session):
    """REG(RESSION): the strategist's spawn_task must reject ``fix`` EVEN when
    the launch context (pr_number + branch) that used to pass the allowlist is
    supplied. Every strategist-spawned fix previously died at the PR-execution
    fence with "fix has no immutable assigned target" because the strategist
    cannot bind review_loop.fix_target / reviewed_head_sha at spawn. The chosen
    resolution is to remove ``fix`` from the strategist spawn surface: reject it
    loudly at the API (naming the correct direction → research → plan → create
    route) and never create a task row."""
    from sqlalchemy import func as _f

    _project, review_id = await _make_strategic_review_task(db_session)

    body = await _post_spawn_task(
        db_session,
        review_id,
        title="Fix the existing PR",
        prompt="address the review feedback",
        spawn_type="fix",
        expect_status=400,
        extra={"pr_number": 123, "branch": "task/existing-pr"},
    )
    detail = body["error"]["message"]
    assert "fix is not spawnable" in detail
    assert "pr_number" in detail
    assert "branch" in detail
    assert "research" in detail
    assert "plan" in detail
    assert "create" in detail

    # No task row was created by the rejected spawn.
    count = (
        await db_session.execute(
            select(_f.count())
            .select_from(Task)
            .where(Task.created_by == "STRATEGIC_AGENT")
        )
    ).scalar()
    assert count == 0


@pytest.mark.asyncio
async def test_spawn_task_idempotent_on_retry_duplicate(db_session):
    """A retried spawn_task (same source review, type and title)
    returns the existing task instead of creating a duplicate, keyed on the
    strategic_direct_fix provenance."""
    from sqlalchemy import func as _f

    _project, review_id = await _make_strategic_review_task(db_session)

    title = "Fix flaky compute spawn test"
    prompt = "Reset the spend-gate global config in the fixture."

    first = await _post_spawn_task(
        db_session, review_id, title=title, prompt=prompt, spawn_type="create"
    )
    assert first["status"] == "ok"
    assert first["deduplicated"] is False
    first_id = first["task_id"]

    second = await _post_spawn_task(
        db_session, review_id, title=title, prompt=prompt, spawn_type="create"
    )
    assert second["status"] == "ok"
    assert second["deduplicated"] is True
    assert second["task_id"] == first_id

    # Exactly one child exists for this review.
    count = (
        await db_session.execute(
            select(_f.count())
            .select_from(Task)
            .where(Task.created_by == "STRATEGIC_AGENT", Task.title == title)
        )
    ).scalar()
    assert count == 1

    # The parent's spawn counter incremented exactly once (dedup does not double-count).
    review = (
        await db_session.execute(select(Task).where(Task.id == review_id))
    ).scalar_one()
    assert (review.context or {}).get("_strategic_tasks_spawned") == 1


@pytest.mark.asyncio
async def test_spawn_task_has_no_routine_per_review_quota(db_session, mocker):
    from sqlalchemy import func as _f

    mocker.patch.dict(
        "app.shared_state.runtime_config",
        {"strategist_spawn_per_review_cap": 2, "strategist_spawn_window_cap": 50},
    )

    _project, review_id = await _make_strategic_review_task(db_session)

    # Distinct titles (overlap below threshold) so semantic dedup never fires.
    for n in ("alpha", "beta"):
        body = await _post_spawn_task(
            db_session, review_id, title=f"Fix {n}", prompt=f"do {n}"
        )
        assert body["status"] == "ok"
        assert body["deduplicated"] is False

    third = await _post_spawn_task(
        db_session,
        review_id,
        title="Fix gamma",
        prompt="do gamma",
    )
    assert third["status"] == "ok"

    # Only the two budgeted tasks exist; the third was not created.
    count = (
        await db_session.execute(
            select(_f.count())
            .select_from(Task)
            .where(Task.created_by == "STRATEGIC_AGENT")
        )
    ).scalar()
    assert count == 3


# ── CI compliance in strategic review payload ─────────────────────────────────


@pytest.mark.asyncio
async def test_spawn_strategic_review_includes_ci_compliance_when_available(
    db_session, mocker
):
    """When get_or_compute_ci_compliance returns a result it is included in the
    strategic review payload under the 'ci_compliance' key."""
    from unittest.mock import AsyncMock

    _patch_spawning_deps(mocker)
    mocker.patch(
        "app.task_lifecycle._has_active_strategic_task",
        new=AsyncMock(return_value=False),
    )

    fake_envelope = {
        "status": "success",
        "integration_branch": "automain",
        "head_sha": "abc123def456",
        "data": {
            "ref": "abc123def456",
            "compliant": True,
            "violations": [],
            "branch_literal_violations": [],
            "checked": 5,
            "limits_available": True,
            "tree_available": True,
        },
        "computed": False,
    }
    ensure_ci = mocker.patch(
        "app.ci_compliance.ensure_current_ci_compliance",
        new=AsyncMock(return_value=fake_envelope),
    )

    project = _make_project(vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True
    assert ensure_ci.await_args.args[0] is not db_session

    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = result.scalars().all()
    payload = tasks[0].payloads[0]
    assert "ci_compliance" in payload, "ci_compliance block missing from strategic payload"
    ci = payload["ci_compliance"]
    assert ci["head_sha"] == "abc123def456"
    assert ci["compliant"] is True


@pytest.mark.asyncio
async def test_spawn_strategic_review_omits_ci_compliance_when_unavailable(
    db_session, mocker
):
    """When get_or_compute_ci_compliance returns None the payload has no ci_compliance key."""
    from unittest.mock import AsyncMock

    _patch_spawning_deps(mocker)
    mocker.patch(
        "app.task_lifecycle._has_active_strategic_task",
        new=AsyncMock(return_value=False),
    )
    mocker.patch(
        "app.ci_compliance.ensure_current_ci_compliance",
        new=AsyncMock(return_value={
            "status": "unavailable",
            "reason": "github_credentials_unavailable",
            "integration_branch": "automain",
            "head_sha": None,
            "data": None,
        }),
    )

    project = _make_project(vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = result.scalars().all()
    payload = tasks[0].payloads[0]
    assert "ci_compliance" not in payload, (
        "ci_compliance should be absent when ensure_current_ci_compliance is unavailable"
    )


@pytest.mark.asyncio
async def test_direction_stats_reachable_prs_merged_in_stat_entry(db_session):
    """reachable_prs_merged is surfaced in directions_summary stat_entry and survives compaction.

    A direction with at least one history entry that carries reachable_prs_merged
    should expose the lifetime reachable total. A direction with only legacy history
    entries (no reachable_prs_merged field) should omit the key rather than fabricate 0.
    """
    project = _make_project(
        directions=[
            {
                "id": "d-reachable01",
                "status": "active",
                "text": "Direction with reachable credit",
                "priority": "medium",
                # 3 merged, 2 ancestry-confirmed reachable.
                "research_history": [
                    {
                        "prs_merged": 3,
                        "verified_prs_merged": 3,
                        "reachable_prs_merged": 2,
                    }
                ],
            },
            {
                "id": "d-legacy-noreachable",
                "status": "active",
                "text": "Direction with legacy entry (no reachable field)",
                "priority": "medium",
                # Pre-reachability: reachable_prs_merged key absent.
                "research_history": [{"prs_merged": 4, "verified_prs_merged": 4}],
            },
        ],
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = result.scalars().all()
    payload = tasks[0].payloads[0]

    reachable_dir = next(
        s for s in payload["directions_summary"] if s["id"] == "d-reachable01"
    )
    assert reachable_dir["reachable_prs_merged"] == 2, (
        "reachable_prs_merged must appear in stat_entry and survive compaction"
    )
    assert reachable_dir["reachable_prs_merged"] <= reachable_dir["prs_merged"]

    legacy_dir = next(
        s for s in payload["directions_summary"] if s["id"] == "d-legacy-noreachable"
    )
    assert "reachable_prs_merged" not in legacy_dir, (
        "legacy directions without reachable_prs_merged must not fabricate a value"
    )


@pytest.mark.asyncio
async def test_direction_stats_known_zero_reachable_in_window(db_session):
    """A direction with in-window reachable_prs_merged=0 surfaces reachable_prs_merged: 0 (present, not omitted).

    Known-zero (field present, value 0) must be distinguishable from legacy-unknown
    (field absent).  The stat_entry must include reachable_prs_merged=0 so the
    agent renderer can emit the explicit alarm annotation rather than silently
    treating it as legacy-unknown.
    """
    project = _make_project(
        directions=[
            {
                "id": "d-zero-reachable",
                "status": "active",
                "text": "Direction where all merges were badge-only (zero reachable)",
                "priority": "high",
                # 2 merged, reachability measured but zero confirmed reachable.
                "research_history": [
                    {
                        "prs_merged": 2,
                        "verified_prs_merged": 2,
                        "reachable_prs_merged": 0,
                    }
                ],
            },
        ],
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = result.scalars().all()
    payload = tasks[0].payloads[0]

    zero_dir = next(
        s for s in payload["directions_summary"] if s["id"] == "d-zero-reachable"
    )
    assert "reachable_prs_merged" in zero_dir, (
        "known-zero reachability (field present, value 0) must not be omitted from stat_entry"
    )
    assert zero_dir["reachable_prs_merged"] == 0, (
        "reachable_prs_merged must be 0, not inflated"
    )


@pytest.mark.asyncio
async def test_direction_stats_known_zero_reachable_survives_eviction(db_session):
    """A direction whose reachability-recorded cycles were all evicted still surfaces reachable_prs_merged: 0.

    When all in-window history is evicted and the history_aggregate only has
    total_reachable_prs_merged=0 but total_reachable_recorded_cycles>0, the
    stat_entry must still include reachable_prs_merged=0 (not omit the field).
    """
    from app.direction_metrics import _fold_evicted_into_aggregate

    # Build an aggregate that represents evicted cycles with known-zero reachability.
    evicted_cycles = [
        {
            "timestamp": "2025-01-01T00:00:00",
            "prs_merged": 3,
            "verified_prs_merged": 3,
            "reachable_prs_merged": 0,  # key present, value 0
        },
        {
            "timestamp": "2025-02-01T00:00:00",
            "prs_merged": 2,
            "verified_prs_merged": 2,
            "reachable_prs_merged": 0,
        },
    ]
    history_aggregate = _fold_evicted_into_aggregate(None, evicted_cycles)
    assert history_aggregate["total_reachable_recorded_cycles"] == 2
    assert history_aggregate["total_reachable_prs_merged"] == 0

    project = _make_project(
        directions=[
            {
                "id": "d-evicted-zero-reachable",
                "status": "active",
                "text": "Direction with evicted zero-reachable aggregate",
                "priority": "high",
                # No in-window history — all reachability info is in the aggregate.
                "research_history": [],
                "history_aggregate": history_aggregate,
            },
        ],
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = result.scalars().all()
    payload = tasks[0].payloads[0]

    evicted_dir = next(
        s for s in payload["directions_summary"] if s["id"] == "d-evicted-zero-reachable"
    )
    assert "reachable_prs_merged" in evicted_dir, (
        "known-zero reachability must survive eviction via total_reachable_recorded_cycles counter"
    )
    assert evicted_dir["reachable_prs_merged"] == 0


# ---------------------------------------------------------------------------
# Summarize fallbacks for previously-exempt droppable payload sections.
# Each of the six sections below dropped WHOLESALE under byte-budget pressure
# before gaining a summarize_fn. These tests assert each summarizer (a) shrinks
# a representative oversized input, (b) never raises on malformed/empty input,
# and (c) is wired into the schema with a callable summarize entry.
# ---------------------------------------------------------------------------


def _ser_size(value) -> int:
    """Serialized byte size, matching the packer's accounting."""
    return len(json.dumps(value, default=str))


_DROPPABLE_SUMMARIZE_SECTIONS = (
    "operator_overrides",
    "knowledge_index",
    "previous_decision_outcomes",
    "playbook_summary",
    "past_retirement_critiques",
    "self_reflection_log",
)


def test_droppable_sections_have_summarize_fallback():
    """The six previously-exempt droppable sections now carry a summarize_fn.

    Acceptance contract: each section key in _STRATEGIC_REVIEW_SCHEMA maps to a
    schema entry whose ``summarize`` value is CALLABLE (not exempt/None), so under
    byte-budget pressure the packer degrades the section instead of dropping it
    wholesale. knowledge_index is also checked in _DESIGN_DOC_UPDATE_SCHEMA.
    """

    for key in _DROPPABLE_SUMMARIZE_SECTIONS:
        entry = _STRATEGIC_REVIEW_SCHEMA.get(key)
        assert entry is not None, f"{key} missing from _STRATEGIC_REVIEW_SCHEMA"
        assert callable(entry.get("summarize")), (
            f"{key} must have a callable summarize fallback, not exempt-drop"
        )
        assert not entry.get("exempt"), (
            f"{key} must not be exempt once it has a summarize fallback"
        )

    # knowledge_index appears in the design-doc schema variant too.
    dd_entry = _DESIGN_DOC_UPDATE_SCHEMA.get("knowledge_index")
    assert dd_entry is not None
    assert callable(dd_entry.get("summarize")), (
        "knowledge_index must have a summarize fallback in _DESIGN_DOC_UPDATE_SCHEMA"
    )


def test_summarize_operator_overrides_shrinks_and_keeps_keys():
    from app.strategic_spawning import _summarize_operator_overrides

    overrides = [
        {
            "document": "vision",
            "field": f"field_{i}",
            "kind": "scalar_conflict",
            "agent_value": "A" * 240,
            "operator_value": "O" * 240,
            "at": f"2026-07-0{i}T00:00:00Z",
        }
        for i in range(1, 9)
    ]
    result = _summarize_operator_overrides(overrides)
    assert isinstance(result, list)
    assert _ser_size(result) < _ser_size(overrides)
    assert len(result) <= 4  # newest-few cap
    # Load-bearing routing keys survive; prose values are truncated hard.
    first = result[0]
    assert first["document"] == "vision"
    assert first["field"] == "field_1"
    assert first["kind"] == "scalar_conflict"
    assert first["at"] == "2026-07-01T00:00:00Z"
    assert len(first["agent_value"]) < 240


def test_summarize_operator_overrides_malformed_never_raises():
    from app.strategic_spawning import _summarize_operator_overrides

    assert _summarize_operator_overrides(None) is None
    assert _summarize_operator_overrides("str") == "str"
    assert _summarize_operator_overrides([]) == []
    # Non-dict list entries pass through untouched.
    assert _summarize_operator_overrides([123, "x"]) == [123, "x"]
    # Missing fields don't crash.
    assert _summarize_operator_overrides([{}]) == [
        {
            "document": None,
            "field": None,
            "kind": None,
            "agent_value": None,
            "operator_value": None,
            "at": None,
        }
    ]


def test_summarize_knowledge_index_shrinks_and_keeps_pointers():
    from app.strategic_spawning import _summarize_knowledge_index

    entries = [
        {
            "id": f"doc-{i}",
            "title": f"Doc {i}",
            "summary": "S" * 300,
            "topic": "research",
            "direction_id": f"d-{i}",
            "source_kind": "web_research",
            "sweep_id": None,
            "arm_label": None,
            "per_arm_verdicts": [{"arm": "a", "blob": "X" * 500}],
            "origin_provenance": {"deep": {"nested": "Y" * 500}},
            "knowledge_acceptance_evidence": "Z" * 300,
            "created_at": "2026-07-01T00:00:00Z",
        }
        for i in range(15)
    ]
    result = _summarize_knowledge_index(entries)
    assert isinstance(result, list)
    assert _ser_size(result) < _ser_size(entries)
    assert len(result) <= 8
    first = result[0]
    # Navigation pointers survive so ./fetch knowledge <id> still works.
    assert first["id"] == "doc-0"
    assert first["title"] == "Doc 0"
    assert first["direction_id"] == "d-0"
    # Heavy sub-fields dropped, summary truncated.
    assert "per_arm_verdicts" not in first
    assert "origin_provenance" not in first
    assert len(first["summary"]) < 300


def test_summarize_knowledge_index_malformed_never_raises():
    from app.strategic_spawning import _summarize_knowledge_index

    assert _summarize_knowledge_index(None) is None
    assert _summarize_knowledge_index("x") == "x"
    assert _summarize_knowledge_index([]) == []
    assert _summarize_knowledge_index([123]) == [123]
    # Entry with no summary key: no crash, id preserved.
    assert _summarize_knowledge_index([{"id": "d1"}]) == [{"id": "d1"}]


def test_summarize_previous_decision_outcomes_shrinks_keeps_ids():
    from app.strategic_spawning import _summarize_previous_decision_outcomes

    value = {
        "cycle_id": "c-1",
        "phase2_completed_at": "2026-07-01T00:00:00Z",
        "summary_text": "T" * 500,
        "cycle_reasoning": "R" * 2000,
        "created": [
            {
                "direction_id": f"d-{i}",
                "role": "created",
                "status": "active",
                "text": "X" * 200,
                "prs_merged": 3,
                "rationale": "why " * 100,
                "prediction": {"similar_past_merges": 2, "blob": "P" * 500},
            }
            for i in range(10)
        ],
        "retired": [
            {
                "direction_id": "d-ret",
                "role": "retired",
                "status": "missing",
                "final_metrics": {"merged": 5, "blob": "F" * 500},
            }
        ],
    }
    result = _summarize_previous_decision_outcomes(value)
    assert isinstance(result, dict)
    assert _ser_size(result) < _ser_size(value)
    assert result["cycle_id"] == "c-1"
    assert result["phase2_completed_at"] == "2026-07-01T00:00:00Z"
    assert "cycle_reasoning" not in result  # verbose free-text dropped
    assert len(result["created"]) <= 6
    first = result["created"][0]
    assert first["direction_id"] == "d-0"  # fetch pointer preserved
    assert "prediction" not in first  # heavy sub-body dropped
    assert "final_metrics" not in result["retired"][0]


def test_summarize_previous_decision_outcomes_malformed_never_raises():
    from app.strategic_spawning import _summarize_previous_decision_outcomes

    assert _summarize_previous_decision_outcomes(None) is None
    assert _summarize_previous_decision_outcomes("x") == "x"
    assert _summarize_previous_decision_outcomes({}) == {}
    # created not a list -> untouched key.
    out = _summarize_previous_decision_outcomes({"cycle_id": "c", "created": "bad"})
    assert out["cycle_id"] == "c"
    assert out["created"] == "bad"


def test_summarize_playbook_summary_shrinks_keeps_keys():
    from app.strategic_spawning import _summarize_playbook_summary

    value = {
        "entries": [
            {
                "key": f"pb-{i}",
                "content": "C" * 400,
                "source": "auto-generated",
                "updated_at": "2026-07-01T00:00:00Z",
                "hit_count": 3,
                "confirmed_count": 2,
            }
            for i in range(10)
        ]
    }
    result = _summarize_playbook_summary(value)
    assert isinstance(result, dict)
    assert _ser_size(result) < _ser_size(value)
    assert len(result["entries"]) <= 6
    first = result["entries"][0]
    assert first["key"] == "pb-0"  # fetch pointer preserved
    assert len(first["content"]) < 400
    assert "hit_count" not in first  # stat fields dropped


def test_summarize_playbook_summary_malformed_never_raises():
    from app.strategic_spawning import _summarize_playbook_summary

    assert _summarize_playbook_summary(None) is None
    assert _summarize_playbook_summary("x") == "x"
    assert _summarize_playbook_summary({}) == {}
    # entries not a list -> passthrough.
    assert _summarize_playbook_summary({"entries": "bad"}) == {"entries": "bad"}
    # Non-dict entry preserved.
    out = _summarize_playbook_summary({"entries": [1, {"key": "k"}]})
    assert out["entries"][0] == 1


def test_summarize_past_retirement_critiques_shrinks_keeps_ids():
    from app.strategic_spawning import _summarize_past_retirement_critiques

    value = [
        {
            "direction_id": f"d-{i}",
            "what_it_addressed": "W" * 300,
            "pattern_that_produced_it": "P" * 300,
            "lesson": "L" * 300,
        }
        for i in range(6)
    ]
    result = _summarize_past_retirement_critiques(value)
    assert isinstance(result, list)
    assert _ser_size(result) < _ser_size(value)
    assert len(result) <= 3
    first = result[0]
    assert first["direction_id"] == "d-0"
    assert len(first["lesson"]) < 300


def test_summarize_past_retirement_critiques_sentinel_and_malformed():
    from app.strategic_spawning import _summarize_past_retirement_critiques

    assert _summarize_past_retirement_critiques(None) is None
    assert _summarize_past_retirement_critiques("x") == "x"
    assert _summarize_past_retirement_critiques([]) == []
    # no_eligible_retirements sentinel is preserved.
    assert _summarize_past_retirement_critiques([{"no_eligible_retirements": True}]) == [
        {"no_eligible_retirements": True}
    ]
    assert _summarize_past_retirement_critiques([42]) == [42]


def test_summarize_self_reflection_log_shrinks_keeps_pointers():
    from app.strategic_spawning import _summarize_self_reflection_log

    value = [
        {
            "ts": f"2026-07-0{i}T00:00:00Z",
            "cycle_task_id": f"t-{i}",
            "what_was_hard": "H" * 300,
            "missing_context": "M" * 300,
            "proposed_self_improvement": "I" * 300,
        }
        for i in range(1, 6)
    ]
    result = _summarize_self_reflection_log(value)
    assert isinstance(result, list)
    assert _ser_size(result) < _ser_size(value)
    assert len(result) <= 3
    # Newest-few (tail) kept; pointers preserved.
    last = result[-1]
    assert last["cycle_task_id"] == "t-5"
    assert last["ts"] == "2026-07-05T00:00:00Z"
    assert len(last["what_was_hard"]) < 300


def test_summarize_self_reflection_log_malformed_never_raises():
    from app.strategic_spawning import _summarize_self_reflection_log

    assert _summarize_self_reflection_log(None) is None
    assert _summarize_self_reflection_log("x") == "x"
    assert _summarize_self_reflection_log([]) == []
    assert _summarize_self_reflection_log(["str", 1]) == ["str", 1]


def test_build_self_reflection_themes_finds_common_theme():
    """Themes are extracted when >5 entries share a recurring word in what_was_hard."""
    from app.strategic_spawning import _build_self_reflection_themes

    log = [
        {"what_was_hard": "Missing context on async patterns"},
        {"what_was_hard": "Needed more context for the change"},
        {"what_was_hard": "Hard to follow without broader context"},
        {"what_was_hard": "Confused by missing context in schema"},
        {"what_was_hard": "Struggled with test context setup"},
        {"what_was_hard": "Ran out of context for large refactor"},
    ]
    result = _build_self_reflection_themes(log)
    assert isinstance(result, list)
    assert len(result) >= 1
    context_themes = [t for t in result if t["theme"] == "context"]
    assert len(context_themes) == 1
    assert context_themes[0]["occurrences"] >= 5


def test_build_self_reflection_themes_empty_under_six():
    """Empty result when log has 5 or fewer entries."""
    from app.strategic_spawning import _build_self_reflection_themes

    assert _build_self_reflection_themes([]) == []
    assert _build_self_reflection_themes([{"what_was_hard": "x"}]) == []
    assert _build_self_reflection_themes([{"what_was_hard": str(i)} for i in range(5)]) == []


def test_build_self_reflection_themes_no_commonality():
    """Empty result when >5 entries have no overlapping significant words."""
    from app.strategic_spawning import _build_self_reflection_themes

    log = [
        {"what_was_hard": "Struggled with python packaging"},
        {"what_was_hard": "Hard to debug kubernetes networking"},
        {"what_was_hard": "Confused by react rendering lifecycle"},
        {"what_was_hard": "Missing documentation for build steps"},
        {"what_was_hard": "Unexpected behavior in database migrations"},
        {"what_was_hard": "Could not reproduce flaky integration test"},
    ]
    result = _build_self_reflection_themes(log)
    assert isinstance(result, list)
    assert result == []


def test_build_self_reflection_themes_malformed():
    """Malformed or None input returns empty list."""
    from app.strategic_spawning import _build_self_reflection_themes

    assert _build_self_reflection_themes(None) == []
    assert _build_self_reflection_themes("x") == []
    assert _build_self_reflection_themes(42) == []
    assert _build_self_reflection_themes([123]) == []


def test_build_self_reflection_themes_non_dict_entries_in_long_log():
    """Non-dict entries in a >5-entry log are skipped without raising AttributeError."""
    from app.strategic_spawning import _build_self_reflection_themes

    log = [
        {"what_was_hard": "Missing context for the async change"},
        {"what_was_hard": "Hard to follow without broader context"},
        "not a dict",
        {"what_was_hard": "Confused by missing context in schema"},
        42,
        {"what_was_hard": "Struggled with test context setup"},
        None,
        {"what_was_hard": "Ran out of context for large refactor"},
    ]
    result = _build_self_reflection_themes(log)
    assert isinstance(result, list)
    context_themes = [t for t in result if t["theme"] == "context"]
    assert len(context_themes) == 1
    assert context_themes[0]["occurrences"] >= 5


def test_build_self_reflection_themes_sorts_by_frequency():
    """Themes are sorted descending by occurrence count, at most 5 returned."""
    from app.strategic_spawning import _build_self_reflection_themes

    log = [
        {"what_was_hard": "Something about schema design testing"},
        {"what_was_hard": "Schema migration was painful testing"},
        {"what_was_hard": "Could not validate schema testing"},
        {"what_was_hard": "Schema changes broke everything testing"},
        {"what_was_hard": "Schema testing took too long"},
        {"what_was_hard": "Schema and testing both frustrating"},
    ]
    result = _build_self_reflection_themes(log)
    assert len(result) <= 5
    # Should have schema (6) first, then testing (6)
    assert result[0]["theme"] == "schema"
    occurrences = [t["occurrences"] for t in result]
    assert occurrences == sorted(occurrences, reverse=True)


def _oversized_section_fixtures() -> dict:
    """Realistic oversized values for the six previously-exempt sections."""
    return {
        "operator_overrides": [
            {
                "document": "vision",
                "field": f"f{i}",
                "kind": "k",
                "agent_value": "A" * 240,
                "operator_value": "O" * 240,
                "at": f"2026-07-0{(i % 9) + 1}T00:00:00Z",
            }
            for i in range(8)
        ],
        "knowledge_index": [
            {
                "id": f"doc-{i}",
                "title": f"Doc {i}",
                "summary": "S" * 300,
                "topic": "t",
                "direction_id": f"d-{i}",
                "source_kind": "web_research",
                "per_arm_verdicts": [{"blob": "X" * 400}],
                "origin_provenance": {"blob": "Y" * 400},
            }
            for i in range(15)
        ],
        "previous_decision_outcomes": {
            "cycle_id": "c-1",
            "phase2_completed_at": "2026-07-01T00:00:00Z",
            "summary_text": "T" * 500,
            "cycle_reasoning": "R" * 2000,
            "created": [
                {
                    "direction_id": f"d-{i}",
                    "role": "created",
                    "status": "active",
                    "text": "X" * 200,
                    "prediction": {"blob": "P" * 400},
                }
                for i in range(10)
            ],
            "retired": [],
        },
        "playbook_summary": {
            "entries": [
                {
                    "key": f"pb-{i}",
                    "content": "C" * 400,
                    "source": "auto-generated",
                    "hit_count": 3,
                }
                for i in range(10)
            ]
        },
        "past_retirement_critiques": [
            {
                "direction_id": f"d-{i}",
                "what_it_addressed": "W" * 300,
                "pattern_that_produced_it": "P" * 300,
                "lesson": "L" * 300,
            }
            for i in range(6)
        ],
        "self_reflection_log": [
            {
                "ts": f"2026-07-0{i}T00:00:00Z",
                "cycle_task_id": f"t-{i}",
                "what_was_hard": "H" * 300,
                "missing_context": "M" * 300,
                "proposed_self_improvement": "I" * 300,
            }
            for i in range(1, 6)
        ],
    }


_CRITICAL_IDENTITY_PAYLOAD = {
    "type": "strategic_review",
    "research_mode": False,
    "project_name": "P",
    "base_branch": "main",
    "is_self_repo": True,
    "enable_self_improvement": True,
    "model": "m",
    "effort": "medium",
    "direction_counts": {"active": 1},
    "max_active_directions": 5,
    "open_message_count": 0,
}


@pytest.mark.parametrize("section", _DROPPABLE_SUMMARIZE_SECTIONS)
def test_droppable_section_degrades_not_drops_under_budget(section):
    """Integration: each previously-exempt section trims (not drops) under pressure.

    Packs the critical identity keys plus ONE oversized section at a budget just
    below the whole-payload size, so the section cannot ship whole and must fall
    through to its summarize_fn. Asserts the section survives (appears in output,
    trimmed) rather than landing in the wholesale-dropped list — the exact
    behavior change from swapping ``exempt: True`` to ``summarize: <fn>``.
    """

    fixtures = _oversized_section_fixtures()
    payload = {**_CRITICAL_IDENTITY_PAYLOAD, section: fixtures[section]}
    # The packer's budget accounting sums per-VALUE sizes (not the whole-dict
    # size). Reserve exactly the critical values plus one byte under the whole
    # section value, so the section cannot ship whole and must fall through to
    # its summarize_fn (which produces a strictly smaller, budget-fitting form).
    critical_used = sum(
        _ser_size(v) for v in _CRITICAL_IDENTITY_PAYLOAD.values()
    )
    whole_section = _ser_size(fixtures[section])
    budget = critical_used + whole_section - 1
    packed = pack_under_budget(payload, _STRATEGIC_REVIEW_SCHEMA, budget=budget)
    meta = packed.get("_packer_meta", {})
    dropped_keys = {d["key"] for d in meta.get("dropped", [])}
    trimmed_keys = {t["key"] for t in meta.get("trimmed", [])}

    assert section in packed, (
        f"{section} degraded to nothing under budget — summarize should keep a skeleton"
    )
    assert section not in dropped_keys, (
        f"{section} was dropped wholesale; expected it to be trimmed instead"
    )
    assert section in trimmed_keys, (
        f"{section} should be recorded as trimmed in _packer_meta"
    )
    # The retained value is strictly smaller than the original.
    assert _ser_size(packed[section]) < _ser_size(fixtures[section])


# ── Reconciliation sections fixture contract ──────────────────────────────────


class TestReconciliationSectionsContract:
    """Pin the list-class keys of cycle_start_reconciliation to a fixture so
    that adding a backend key without updating the agent renderer fails CI.

    The fixture (backend/testing/__fixtures__/reconciliation_sections.json) is
    the single source of truth.  These tests verify the backend side; the agent
    side is covered in agent/src/handlers/strategic-sections.test.ts.
    """

    def _load_fixture(self) -> dict:
        from pathlib import Path

        fixture_path = (
            Path(__file__).parent / "__fixtures__" / "reconciliation_sections.json"
        )
        with open(fixture_path) as f:
            return json.load(f)

    def _minimal_recon_output(self) -> dict:
        """Return a non-None _build_cycle_start_reconciliation result."""
        from app.strategic_spawning import _build_cycle_start_reconciliation

        # An active direction with no gap_id is always ungrounded → non-empty
        # conflict list → function returns a dict (not None).
        direction_stats = [
            {"id": "d-contract-test", "status": "active", "text": "contract test"}
        ]
        out = _build_cycle_start_reconciliation(direction_stats, {}, {})
        assert out is not None, (
            "_build_cycle_start_reconciliation returned None for minimal data — "
            "the early-return guard may have changed; update this helper."
        )
        return out

    def test_fixture_schema_version_and_list_keys(self):
        fixture = self._load_fixture()
        assert fixture["schema_version"] == 1
        assert isinstance(fixture["list_keys"], list)
        assert len(fixture["list_keys"]) > 0

    def test_backend_output_contains_all_fixture_list_keys(self):
        """Every fixture key must appear as a list in the backend output.

        Fails when the fixture adds a key that _build_cycle_start_reconciliation
        does not yet emit — update the backend function or remove the key.
        """
        fixture = self._load_fixture()
        expected_keys = set(fixture["list_keys"])
        out = self._minimal_recon_output()

        actual_list_keys = {k for k, v in out.items() if isinstance(v, list)}
        missing = expected_keys - actual_list_keys
        assert not missing, (
            f"Keys in reconciliation_sections fixture but absent from "
            f"_build_cycle_start_reconciliation output: {sorted(missing)}. "
            f"Add them to the backend function or remove them from the fixture."
        )

    def test_fixture_covers_all_backend_list_keys(self):
        """Every list key the backend emits must be declared in the fixture.

        Fails when _build_cycle_start_reconciliation adds a new list key without
        updating backend/testing/__fixtures__/reconciliation_sections.json — the
        CI guard that catches the compute-but-never-render drift class.
        """
        fixture = self._load_fixture()
        expected_keys = set(fixture["list_keys"])
        out = self._minimal_recon_output()

        actual_list_keys = {k for k, v in out.items() if isinstance(v, list)}
        extra = actual_list_keys - expected_keys
        assert not extra, (
            f"_build_cycle_start_reconciliation emits list keys not declared in "
            f"the reconciliation_sections fixture: {sorted(extra)}. "
            f"Add them to backend/testing/__fixtures__/reconciliation_sections.json "
            f"AND add a renderer in agent/src/handlers/strategic-sections.ts."
        )


# ---------------------------------------------------------------------------
# _maybe_spawn_ci_compliance_cap_bump_fix — chronic vs routine classification
# ---------------------------------------------------------------------------

from app.strategic_spawning import _maybe_spawn_ci_compliance_cap_bump_fix  # noqa: E402


def _chronic_history():
    """Return a 3-bump history that trips the chronic-offender threshold."""
    from app.size_limit_policy import BumpRecord
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    return [
        BumpRecord(
            commit_sha="aaa",
            commit_date=(now - timedelta(days=150)).isoformat(),
            old_cap=3500,
            new_cap=4050,
        ),
        BumpRecord(
            commit_sha="bbb",
            commit_date=(now - timedelta(days=90)).isoformat(),
            old_cap=4050,
            new_cap=4500,
        ),
        BumpRecord(
            commit_sha="ccc",
            commit_date=(now - timedelta(days=30)).isoformat(),
            old_cap=4500,
            new_cap=4700,
        ),
    ]


def _make_gate(violations, *, head_sha="a" * 40):
    return {
        "blocked": True,
        "head_sha": head_sha,
        "violations": violations,
        "branch_literal_violations": [],
        "violation_count": len(violations),
    }


@pytest.mark.asyncio
async def test_cap_bump_task_decomposes_chronic_offender(db_session, mocker):
    """When a violation is on a chronic offender, the spawned task must direct
    decomposition and must NOT instruct a blind cap bump (--fix) for that file.

    This is the acceptance test named by the task:
    test_strategic_spawning_summaries.py::test_cap_bump_task_decomposes_chronic_offender
    """
    project = Project(
        name="chronic-test",
        user_id="test-user",
        github_owner="owner",
        github_repo="repo",
        integration_branch="automain",
        promotion_target="main",
        autonomous_enabled=True,
        directions=[],
        vision={"purpose": "test chronic spawn"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    gate = _make_gate(
        [{"section": "backend", "rel": "orchestrator.py", "count": 9200, "cap": 9000}]
    )

    with patch(
        "app.size_limit_policy.parse_bump_history",
        return_value=_chronic_history(),
    ):
        spawned = await _maybe_spawn_ci_compliance_cap_bump_fix(db_session, project, gate)
    await db_session.commit()

    assert spawned is not None
    prompt = spawned.first_payload["prompt"]
    title = spawned.first_payload["title"]

    # Must direct decomposition, not a cap bump.
    assert "decompos" in prompt.lower(), (
        "Expected prompt to mention decomposition for a chronic offender"
    )
    assert "orchestrator.py" in prompt, (
        "Expected prompt to name the chronic offender file"
    )
    # Must NOT tell the agent to run --fix on the chronic file.
    assert "python3 backend/testing/check_size_limits.py --fix" not in prompt, (
        "Prompt must not instruct --fix for a chronic offender"
    )
    # Title should signal decomposition, not a cap bump.
    assert "decompos" in title.lower() or "chronic" in title.lower(), (
        f"Task title should indicate decomposition, got: {title!r}"
    )
    # Context keys preserved.
    assert spawned.context["spawned_by"] == "ci_compliance_cap_bump_gate"
    assert spawned.context["head_sha"] == "a" * 40
    assert spawned.context["ci_compliance_cap_bump"] is True


@pytest.mark.asyncio
async def test_cap_bump_task_unreadable_history_routes_to_decomposition(db_session, mocker):
    """When bump history cannot be read, the ci-compliance cap-bump gate must
    route to DECOMPOSITION with NO suggested cap bump — the guard distinguishes
    "history shows no bumps" from "history could not be read" and refuses in the
    second case.

    This is the direction's success evidence: it must FAIL against the old
    fail-open code (git error -> is_chronic=False -> routine --fix bump) and
    PASS after the fail-closed fix (git error -> refuse -> decomposition).
    """
    from app.size_limit_policy import BumpHistoryUnavailableError

    project = Project(
        name="unreadable-test",
        user_id="test-user",
        github_owner="owner",
        github_repo="repo",
        integration_branch="automain",
        promotion_target="main",
        autonomous_enabled=True,
        directions=[],
        vision={"purpose": "test unreadable spawn"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    gate = _make_gate(
        [{"section": "backend", "rel": "task_lifecycle.py", "count": 9200, "cap": 7750}]
    )

    with patch(
        "app.size_limit_policy.parse_bump_history",
        side_effect=BumpHistoryUnavailableError("truncated clone"),
    ):
        spawned = await _maybe_spawn_ci_compliance_cap_bump_fix(db_session, project, gate)
    await db_session.commit()

    assert spawned is not None
    prompt = spawned.first_payload["prompt"]
    title = spawned.first_payload["title"]

    # Must route to decomposition, not a cap bump.
    assert "decompos" in prompt.lower(), (
        "Expected prompt to mention decomposition for unreadable bump history"
    )
    assert "task_lifecycle.py" in prompt, (
        "Expected prompt to name the offender file"
    )
    # Must NOT tell the agent to run --fix (a blind cap bump) on the file.
    assert "python3 backend/testing/check_size_limits.py --fix" not in prompt, (
        "Prompt must not instruct --fix for an unreadable-history offender"
    )
    # Title should signal decomposition, not a cap bump.
    assert "decompos" in title.lower() or "chronic" in title.lower(), (
        f"Task title should indicate decomposition, got: {title!r}"
    )


@pytest.mark.asyncio
async def test_cap_bump_task_classify_failure_fails_closed_to_decomposition(
    db_session, mocker
):
    """If classify_breach itself raises (not just an unreadable history), the
    gate must still refuse the bump and route to decomposition rather than
    falling back to a false routine verdict.
    """
    project = Project(
        name="classify-fail-test",
        user_id="test-user",
        github_owner="owner",
        github_repo="repo",
        integration_branch="automain",
        promotion_target="main",
        autonomous_enabled=True,
        directions=[],
        vision={"purpose": "test classify failure spawn"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    gate = _make_gate(
        [{"section": "backend", "rel": "task_lifecycle.py", "count": 9200, "cap": 7750}]
    )

    mocker.patch(
        "app.strategic_spawning.classify_breach",
        side_effect=RuntimeError("unexpected classification failure"),
    )
    spawned = await _maybe_spawn_ci_compliance_cap_bump_fix(db_session, project, gate)
    await db_session.commit()

    assert spawned is not None
    prompt = spawned.first_payload["prompt"]
    title = spawned.first_payload["title"]
    assert "decompos" in prompt.lower(), (
        "Expected decomposition routing when classify_breach fails"
    )
    assert "python3 backend/testing/check_size_limits.py --fix" not in prompt, (
        "Prompt must not instruct --fix when classification fails"
    )
    assert "decompos" in title.lower() or "chronic" in title.lower(), (
        f"Task title should indicate decomposition, got: {title!r}"
    )


@pytest.mark.asyncio
async def test_cap_bump_task_routine_violation_keeps_fix_instruction(db_session, mocker):
    """When all violations are routine (no chronic history), the spawned task
    must still instruct `check_size_limits.py --fix` as before.
    """
    project = Project(
        name="routine-test",
        user_id="test-user",
        github_owner="owner",
        github_repo="repo",
        integration_branch="automain",
        promotion_target="main",
        autonomous_enabled=True,
        directions=[],
        vision={"purpose": "test routine spawn"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    gate = _make_gate(
        [{"section": "backend", "rel": "new_module.py", "count": 1600, "cap": 1500}]
    )

    with patch("app.size_limit_policy.parse_bump_history", return_value=[]):
        spawned = await _maybe_spawn_ci_compliance_cap_bump_fix(db_session, project, gate)
    await db_session.commit()

    assert spawned is not None
    prompt = spawned.first_payload["prompt"]
    title = spawned.first_payload["title"]

    assert "python3 backend/testing/check_size_limits.py --fix" in prompt, (
        "Routine violation must still instruct --fix"
    )
    assert "bump" in title.lower() or "fix" in title.lower(), (
        f"Routine task title should reference bump/fix, got: {title!r}"
    )


@pytest.mark.asyncio
async def test_cap_bump_task_uncapped_violation_treated_as_routine(db_session, mocker):
    """Violations with cap=None (new files) are never chronic and must keep the
    --fix instruction regardless of git history.
    """
    project = Project(
        name="uncapped-test",
        user_id="test-user",
        github_owner="owner",
        github_repo="repo",
        integration_branch="automain",
        promotion_target="main",
        autonomous_enabled=True,
        directions=[],
        vision={"purpose": "test uncapped spawn"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    gate = _make_gate(
        [{"section": "backend", "rel": "brand_new.py", "count": 2000, "cap": None}]
    )

    classify_breach_mock = mocker.patch("app.size_limit_policy.classify_breach")
    spawned = await _maybe_spawn_ci_compliance_cap_bump_fix(db_session, project, gate)
    await db_session.commit()

    # classify_breach must NOT be called for uncapped violations.
    classify_breach_mock.assert_not_called()
    assert spawned is not None
    assert "python3 backend/testing/check_size_limits.py --fix" in spawned.first_payload["prompt"]


# ---------------------------------------------------------------------------
# Recent-handled conversation tail: bounds and ordering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_recent_handled_conversation_bounds_and_order(db_session):
    """``_fetch_recent_handled_conversation`` returns a bounded, chronological,
    excerpt-capped tail of the recently-ANSWERED operator conversation.

    Asserts the observable contract:
      * only status handled/resolved conversational rows are returned (open and
        system rows are excluded);
      * at most ``_RECENT_HANDLED_KEEP`` rows survive, keeping the NEWEST;
      * the survivors are ordered oldest-first (stable transcript read);
      * each content excerpt is capped at ``_RECENT_HANDLED_EXCERPT_MAX_CHARS``.
    """
    from app.strategic_spawning import (
        _fetch_recent_handled_conversation,
        _RECENT_HANDLED_KEEP,
        _RECENT_HANDLED_EXCERPT_MAX_CHARS,
    )

    project = _make_project(vision={"purpose": "test"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    base = datetime(2025, 1, 1, tzinfo=timezone.utc).replace(tzinfo=None)
    # 12 handled operator messages, ascending created_at (so idx 11 is newest).
    handled_msgs = []
    for i in range(12):
        m = ProjectMessage(
            project_id=project.id,
            from_type="user",
            message_type=MessageType.NOTE,
            content=f"handled-{i:02d}",
            status="handled",
        )
        db_session.add(m)
        handled_msgs.append(m)
    # A long resolved message to exercise the excerpt cap (newest of all).
    long_content = "x" * (_RECENT_HANDLED_EXCERPT_MAX_CHARS + 500)
    long_msg = ProjectMessage(
        project_id=project.id,
        from_type="strategic",
        message_type=MessageType.NOTE,
        content=long_content,
        status="resolved",
    )
    db_session.add(long_msg)
    # Excluded rows: an OPEN message and a handled SYSTEM (bookkeeping) message.
    open_msg = ProjectMessage(
        project_id=project.id,
        from_type="user",
        message_type=MessageType.NOTE,
        content="still-open",
        status="open",
    )
    sys_msg = ProjectMessage(
        project_id=project.id,
        from_type="system",
        message_type=MessageType.NOTE,
        content="internal bookkeeping",
        status="handled",
    )
    db_session.add_all([open_msg, sys_msg])
    await db_session.commit()

    # Stamp deterministic ascending timestamps so ordering is unambiguous.
    for i, m in enumerate(handled_msgs):
        m.created_at = base + timedelta(minutes=i)
        db_session.add(m)
    long_msg.created_at = base + timedelta(minutes=100)  # newest overall
    db_session.add(long_msg)
    await db_session.commit()

    tail = await _fetch_recent_handled_conversation(db_session, project.id)

    # Bounded to the KEEP count.
    assert len(tail) == _RECENT_HANDLED_KEEP
    contents = [m["content"] for m in tail]
    ids = {m["id"] for m in tail}

    # Excluded rows never appear.
    assert "still-open" not in contents
    assert str(open_msg.id) not in ids
    assert "internal bookkeeping" not in contents
    assert str(sys_msg.id) not in ids

    # The newest survivors are kept: long_msg (t+100) plus the newest handled
    # operator rows; the oldest handled rows are dropped under the bound.
    assert str(long_msg.id) in ids
    assert "handled-00" not in contents  # oldest, dropped
    assert "handled-11" in contents  # newest handled operator row, kept

    # Chronological (oldest-first) order among the survivors.
    created = [m["created_at"] for m in tail]
    assert created == sorted(created)
    # long_msg is the newest → it sorts LAST in the oldest-first tail.
    assert tail[-1]["id"] == str(long_msg.id)

    # Excerpt cap enforced on the long message.
    assert len(tail[-1]["content"]) == _RECENT_HANDLED_EXCERPT_MAX_CHARS


@pytest.mark.asyncio
async def test_resident_minimal_spawn_payload_ships_knowledge_index(db_session):
    """resident_minimal spawn payload ships the KnowledgeDocument index, not raw JSONB notes.

    Mocks build_strategic_memory_index to return a synthetic KnowledgeDoc entry
    and verifies:
    - the mock is called (i.e. the resident_minimal path invokes the index builder)
    - the stored task payload contains the index entry (id/title keys present)
    - the raw JSONB note shape ({note, set_at}) is NOT present in the result
    """
    project = _make_project(
        vision={"purpose": "test purpose"},
        strategic_memory_notes=[{"note": "legacy raw note", "set_at": "2024-01-01"}],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    fake_doc_id = str(uuid.uuid4())
    fake_index = [
        {
            "id": fake_doc_id,
            "title": "Keep scope tight",
            "topic": "dev-workflow",
            "summary": "Avoid touching unrelated files.",
            "tags": [],
            "created_at": "2024-01-01T00:00:00",
        }
    ]

    with patch(
        "app.strategic_spawning.build_strategic_memory_index",
        new=AsyncMock(return_value=fake_index),
    ) as mock_bsmi:
        spawned = await _spawn_strategic_review(project, db_session, resident_minimal=True)
        assert spawned is True
        # The index builder must be called at least once (for the resident_minimal path).
        assert mock_bsmi.called, "build_strategic_memory_index was not called"

    result = await db_session.execute(_select_agent_tasks(project.id))
    payload = result.scalars().all()[0].payloads[0]

    notes = payload.get("strategic_memory_notes", [])
    assert isinstance(notes, list)

    # The synthetic KnowledgeDocument entry must be in the payload.
    ids = [n.get("id") for n in notes]
    assert fake_doc_id in ids, f"KnowledgeDoc {fake_doc_id} not found in payload notes: {notes}"

    entry = next(n for n in notes if n.get("id") == fake_doc_id)
    assert entry.get("title") == "Keep scope tight"

    # Raw JSONB note shape ({note, set_at}) must NOT appear.
    for n in notes:
        assert "note" not in n, f"Raw JSONB note shape leaked into spawn payload: {n}"


@pytest.mark.asyncio
async def test_resident_minimal_spawn_ships_bounded_current_policy_only(
    db_session, mocker
):
    """Resident gets live policy truth without cadence/version attempt expansion."""
    from app import release_qualifier as rq

    mocker.patch.dict(
        rq._runtime_config,
        {
            "promotion_threshold": 0.81,
            "promotion_weights": {
                "audit": 4 / 7,
                "test_erosion": 3 / 7,
            },
            "completion_debt_weight_mode": "observe",
        },
        clear=False,
    )
    project = _make_project(
        vision={"purpose": "test purpose"},
        integration_branch="main",
        promotion_target="main",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    spawned = await _spawn_strategic_review(project, db_session, resident_minimal=True)

    assert spawned is True
    task = (
        (await db_session.execute(_select_agent_tasks(project.id)))
        .scalars()
        .all()[0]
    )
    payload = task.payloads[0]
    policy = payload["current_effective_policy"]
    assert policy["threshold"] == pytest.approx(0.81)
    assert policy["source"] == "runtime_config"
    assert policy["fallback_error"] is None
    assert len(policy["policy_hash"]) == 64
    assert set(policy) == {
        "threshold",
        "weights",
        "source",
        "fallback_error",
        "policy_hash",
    }
    assert "active_or_last_attempt_policy" not in payload
    assert "version_phase" not in payload


@pytest.mark.asyncio
async def test_resident_minimal_spawn_records_effort_tier(db_session):
    """A resident-minimal strategic_review spawn runs the pre-spawn effort
    classifier and records effort_tier/effort_reason on the StrategicCycle row.

    The resident fast path (operator START) historically early-returned before
    the classifier the cadence path runs, so resident cycles ran at the pinned/
    research fallback effort with no audit trail. With an equal-branch (non-LTS)
    project and clean signals the classifier resolves ROUTINE: the payload
    carries the 'medium' downgrade (the resident path's cost savings) and the
    cycle row is stamped effort_tier='routine' (the audit trail).
    """
    from app.models import CYCLE_META_EFFORT_REASON, CYCLE_META_EFFORT_TIER, StrategicCycle

    project = _make_project(
        vision={"purpose": "test purpose"},
        integration_branch="main",
        promotion_target="main",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    spawned = await _spawn_strategic_review(project, db_session, resident_minimal=True)
    assert spawned is True

    task = (
        (await db_session.execute(_select_agent_tasks(project.id)))
        .scalars()
        .all()[0]
    )
    payload = task.payloads[0]
    assert payload["type"] == "strategic_review"
    assert payload.get("effort") == "medium", (
        "A routine resident cycle must carry the classifier's medium downgrade "
        "so routing resolves a cheaper tier — that is the resident path's cost "
        "savings from the effort tier mechanism."
    )

    cycle = (
        (
            await db_session.execute(
                select(StrategicCycle).where(StrategicCycle.project_id == project.id)
            )
        )
        .scalars()
        .all()[0]
    )
    meta = cycle.cycle_metadata or {}
    assert meta.get(CYCLE_META_EFFORT_TIER) == "routine"
    assert meta.get(CYCLE_META_EFFORT_REASON) == "all routine signals clean"


@pytest.mark.asyncio
async def test_resident_minimal_spawn_lts_keeps_consequential(db_session):
    """An LTS-split resident-minimal spawn is forced CONSEQUENTIAL.

    The resident fast path does not compute the version-phase state (its build
    can hit GitHub), so a pending version decision must never ride a cheapened
    tier. The cycle row still records the tier + reason so the fail-closed
    decision is attributable in cost analysis.
    """
    from app.models import CYCLE_META_EFFORT_REASON, CYCLE_META_EFFORT_TIER, StrategicCycle

    project = _make_project(vision={"purpose": "test purpose"})  # automain/main LTS
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Set a cheap research fallback to confirm the LTS branch forces 'max'
    # on the payload instead of inheriting the fallback.
    old_efforts = dict(_shared_state.runtime_config.get("task_type_efforts") or {})
    _shared_state.runtime_config["task_type_efforts"] = {
        **old_efforts,
        "research": "low",
    }
    try:
        spawned = await _spawn_strategic_review(project, db_session, resident_minimal=True)
        assert spawned is True
    finally:
        _shared_state.runtime_config["task_type_efforts"] = old_efforts

    task = (
        (await db_session.execute(_select_agent_tasks(project.id)))
        .scalars()
        .all()[0]
    )
    assert task.payloads[0].get("effort") == "max", (
        "An LTS resident cycle must force CONSEQUENTIAL_EFFORT ('max') on the "
        "payload even when a cheaper research fallback is configured."
    )

    cycle = (
        (
            await db_session.execute(
                select(StrategicCycle).where(StrategicCycle.project_id == project.id)
            )
        )
        .scalars()
        .all()[0]
    )
    meta = cycle.cycle_metadata or {}
    assert meta.get(CYCLE_META_EFFORT_TIER) == "consequential"
    assert "version-phase state unexamined" in meta.get(CYCLE_META_EFFORT_REASON, "")


@pytest.mark.asyncio
async def test_consequential_cycle_forces_max_effort(db_session):
    """A strategic_review spawned while a version decision is pending must run
    at CONSEQUENTIAL_EFFORT ('max') even when the generic research effort
    fallback is pinned below 'max'.

    The LTS project (automain/main) bootstraps an open v1 with goal=None, so
    ``version_phase_block.goal_needed`` fires — the canonical consequential
    signal. With ``task_type_efforts['research']`` set to 'low', the old code
    inherited that cheaper fallback (silently cheapening the high-stakes
    cycle); the classifier's consequential verdict must force 'max' on the
    payload instead. The cycle row is still stamped effort_tier='consequential'
    with the classifier reason.
    """
    from app.models import CYCLE_META_EFFORT_REASON, CYCLE_META_EFFORT_TIER, StrategicCycle

    project = _make_project(vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    old_efforts = dict(_shared_state.runtime_config.get("task_type_efforts") or {})
    _shared_state.runtime_config["task_type_efforts"] = {
        **old_efforts,
        "research": "low",
    }
    try:
        spawned = await _spawn_strategic_review(project, db_session)
    finally:
        _shared_state.runtime_config["task_type_efforts"] = old_efforts
    assert spawned is True

    task = (
        (await db_session.execute(_select_agent_tasks(project.id)))
        .scalars()
        .all()[0]
    )
    payload = task.payloads[0]
    assert payload["type"] == "strategic_review"
    assert payload.get("effort") == "max", (
        "A consequential cycle must force CONSEQUENTIAL_EFFORT ('max') on the "
        "payload instead of inheriting the cheaper research fallback."
    )

    cycle = (
        (
            await db_session.execute(
                select(StrategicCycle).where(StrategicCycle.project_id == project.id)
            )
        )
        .scalars()
        .all()[0]
    )
    meta = cycle.cycle_metadata or {}
    assert meta.get(CYCLE_META_EFFORT_TIER) == "consequential"
    assert "version goal needed" in meta.get(CYCLE_META_EFFORT_REASON, "")


@pytest.mark.asyncio
async def test_spawn_review_explicit_effort_pin_wins(db_session):
    """An explicit ``task_type_efforts['strategic_review']`` pin wins over the
    pre-spawn classifier: the pinned effort rides the payload (not the routine
    downgrade) and the cycle row is stamped effort_tier='pinned' so the operator
    override is attributable in cost analysis."""
    from app.models import CYCLE_META_EFFORT_TIER, StrategicCycle

    # Equal branches → routine-eligible signals; the pin must still win.
    project = _make_project(
        vision={"purpose": "test purpose"},
        integration_branch="main",
        promotion_target="main",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    old_efforts = dict(_shared_state.runtime_config.get("task_type_efforts") or {})
    _shared_state.runtime_config["task_type_efforts"] = {
        **old_efforts,
        "strategic_review": "high",
    }
    try:
        spawned = await _spawn_strategic_review(project, db_session)
    finally:
        _shared_state.runtime_config["task_type_efforts"] = old_efforts
    assert spawned is True

    task = (
        (await db_session.execute(_select_agent_tasks(project.id)))
        .scalars()
        .all()[0]
    )
    assert task.payloads[0]["type"] == "strategic_review"
    assert task.payloads[0].get("effort") == "high", (
        "An explicit strategic_review effort pin must be carried on the payload "
        "instead of the classifier's routine downgrade."
    )

    cycle = (
        (
            await db_session.execute(
                select(StrategicCycle).where(StrategicCycle.project_id == project.id)
            )
        )
        .scalars()
        .all()[0]
    )
    meta = cycle.cycle_metadata or {}
    assert meta.get(CYCLE_META_EFFORT_TIER) == "pinned"
