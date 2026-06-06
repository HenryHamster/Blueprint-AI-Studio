"""Tests for per-type first-review rejection patterns (app.rejection_patterns).

Covers the aggregation shape (per-type grouping, window filtering, time
decay, min-sample gating, guidance fallback), the LOW-priority budget-drop
behavior of inject_rejection_patterns, and the claim-endpoint injection
into project_context.
"""

import uuid
from datetime import timedelta

import pytest
from httpx import AsyncClient

from app.auth import create_agent_token
from app.models import Task, TaskStatus
from app.rejection_patterns import (
    _DEFAULT_GUIDANCE,
    _PROJECT_CONTEXT_SOFT_BUDGET_BYTES,
    aggregate_rejection_patterns,
    build_rejection_patterns_block,
    guidance_for_category,
    inject_rejection_patterns,
)
from testing.factories import _utcnow, make_project, make_task


def _rejected_task(
    project_id,
    *,
    task_type: str = "create",
    categories: dict | None = None,
    age: timedelta = timedelta(hours=1),
    status: TaskStatus = TaskStatus.COMPLETED,
) -> Task:
    """A task whose first review rejected it for *categories*."""
    context = {}
    if categories is not None:
        context["_first_attempt_rejection_categories"] = categories
    return make_task(
        project_id=project_id,
        title=f"{task_type} task",
        status=status,
        payloads=[{"type": task_type, "prompt": "do work"}],
        context=context,
        created_at=_utcnow() - age,
    )


async def _make_project(db_session, name: str = "Rejection Patterns Test"):
    project = make_project(name=name)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    return project


# ── aggregate_rejection_patterns — shape ─────────────────────────────────────


@pytest.mark.asyncio
async def test_aggregation_shape_per_task_type(db_session):
    """Aggregates group by task type with the documented entry shape."""
    project = await _make_project(db_session)
    for _ in range(3):
        db_session.add(
            _rejected_task(
                project.id,
                task_type="create",
                categories={"correctness": 2, "test-coverage": 1},
            )
        )
    db_session.add(
        _rejected_task(project.id, task_type="create", categories={"correctness": 1})
    )
    # A task with no rejection marker contributes nothing.
    db_session.add(_rejected_task(project.id, task_type="create", categories=None))
    await db_session.commit()

    aggregates = await aggregate_rejection_patterns(db_session, project.id)

    assert set(aggregates.keys()) == {"create"}
    entry = aggregates["create"]
    assert entry["window_days"] == 7
    assert entry["sample_size"] == 4
    cats = entry["categories"]
    assert [c["category"] for c in cats] == ["correctness", "test-coverage"]
    correctness = cats[0]
    # 4 of 4 rejected tasks included correctness (issue counts inside a task
    # don't inflate task_count).
    assert correctness["task_count"] == 4
    assert correctness["share"] == 1.0
    assert correctness["weight"] > 0
    assert correctness["guidance"] == guidance_for_category("correctness")
    coverage = cats[1]
    assert coverage["task_count"] == 3
    assert coverage["share"] == 0.75


@pytest.mark.asyncio
async def test_aggregation_excludes_tasks_outside_window(db_session):
    """Rejections older than the window don't count toward the pattern."""
    project = await _make_project(db_session)
    for _ in range(3):
        db_session.add(
            _rejected_task(
                project.id,
                categories={"security": 1},
                age=timedelta(days=10),
            )
        )
    await db_session.commit()

    aggregates = await aggregate_rejection_patterns(db_session, project.id)
    assert aggregates == {}


@pytest.mark.asyncio
async def test_aggregation_min_sample_gating(db_session):
    """Types with fewer than min_sample rejected tasks are omitted entirely."""
    project = await _make_project(db_session)
    for _ in range(3):
        db_session.add(
            _rejected_task(project.id, task_type="fix", categories={"correctness": 1})
        )
    # Only 2 rejected create tasks — below the default threshold of 3.
    for _ in range(2):
        db_session.add(
            _rejected_task(project.id, task_type="create", categories={"security": 1})
        )
    await db_session.commit()

    aggregates = await aggregate_rejection_patterns(db_session, project.id)
    assert "fix" in aggregates
    assert "create" not in aggregates


@pytest.mark.asyncio
async def test_aggregation_time_decay_ranks_fresh_rejections_first(db_session):
    """With equal task counts, fresher rejections outrank week-old ones."""
    project = await _make_project(db_session)
    # scope-drift: 2 rejections from ~6.5 days ago (heavily decayed).
    for _ in range(2):
        db_session.add(
            _rejected_task(
                project.id,
                categories={"scope-drift": 1},
                age=timedelta(days=6, hours=12),
            )
        )
    # error-handling: 2 rejections from the last hour (full weight).
    for _ in range(2):
        db_session.add(
            _rejected_task(project.id, categories={"error-handling": 1})
        )
    await db_session.commit()

    aggregates = await aggregate_rejection_patterns(db_session, project.id)
    cats = aggregates["create"]["categories"]
    assert [c["category"] for c in cats] == ["error-handling", "scope-drift"]
    assert cats[0]["weight"] > cats[1]["weight"]
    # Equal raw counts — only the decay separates them.
    assert cats[0]["task_count"] == cats[1]["task_count"] == 2


@pytest.mark.asyncio
async def test_aggregation_caps_categories_at_top_n(db_session):
    """Only the top_n categories ship, ordered by decayed weight."""
    project = await _make_project(db_session)
    cat_names = ["correctness", "security", "test-coverage", "scope-drift"]
    # correctness on 4 tasks, security on 3, test-coverage on 2, scope-drift on 1.
    for i, cat in enumerate(cat_names):
        for _ in range(len(cat_names) - i):
            db_session.add(_rejected_task(project.id, categories={cat: 1}))
    await db_session.commit()

    aggregates = await aggregate_rejection_patterns(db_session, project.id)
    cats = aggregates["create"]["categories"]
    assert len(cats) == 3
    assert [c["category"] for c in cats] == [
        "correctness",
        "security",
        "test-coverage",
    ]


@pytest.mark.asyncio
async def test_aggregation_unknown_category_gets_fallback_guidance(db_session):
    """Vocabulary drift must not crash the block — unknown cats get the fallback."""
    project = await _make_project(db_session)
    for _ in range(3):
        db_session.add(
            _rejected_task(project.id, categories={"some-future-category": 1})
        )
    await db_session.commit()

    aggregates = await aggregate_rejection_patterns(db_session, project.id)
    cats = aggregates["create"]["categories"]
    assert cats[0]["category"] == "some-future-category"
    assert cats[0]["guidance"] == _DEFAULT_GUIDANCE


@pytest.mark.asyncio
async def test_aggregation_scopes_to_project(db_session):
    """Another project's rejections never leak into this project's patterns."""
    project = await _make_project(db_session)
    other = await _make_project(db_session, name="Other Project")
    for _ in range(3):
        db_session.add(_rejected_task(other.id, categories={"security": 1}))
    await db_session.commit()

    aggregates = await aggregate_rejection_patterns(db_session, project.id)
    assert aggregates == {}


# ── build_rejection_patterns_block ───────────────────────────────────────────


def test_build_block_slices_claiming_type():
    aggregates = {
        "create": {
            "window_days": 7,
            "sample_size": 4,
            "categories": [
                {
                    "category": "correctness",
                    "weight": 3.2,
                    "task_count": 4,
                    "share": 1.0,
                    "guidance": "x",
                }
            ],
        },
    }
    block = build_rejection_patterns_block(aggregates, "create")
    assert block is not None
    assert block["task_type"] == "create"
    assert block["sample_size"] == 4
    assert block["categories"][0]["category"] == "correctness"

    assert build_rejection_patterns_block(aggregates, "fix") is None
    assert build_rejection_patterns_block({}, "create") is None
    assert (
        build_rejection_patterns_block(
            {"create": {"window_days": 7, "sample_size": 3, "categories": []}},
            "create",
        )
        is None
    )


# ── inject_rejection_patterns — budget-drop behavior ─────────────────────────


def _sample_block() -> dict:
    return {
        "task_type": "create",
        "window_days": 7,
        "sample_size": 4,
        "categories": [
            {
                "category": "correctness",
                "weight": 3.2,
                "task_count": 4,
                "share": 1.0,
                "guidance": "Re-check edge cases.",
            }
        ],
    }


def test_inject_ships_block_when_under_budget():
    ctx = {"active_tasks": [{"id": "t1", "title": "small context"}]}
    assert inject_rejection_patterns(ctx, _sample_block()) is True
    assert ctx["rejection_patterns"]["task_type"] == "create"


def test_inject_drops_block_when_context_already_at_budget():
    """LOW-priority packing: the block drops, the existing fields stay intact."""
    bloated = {"mission_critical": "x" * _PROJECT_CONTEXT_SOFT_BUDGET_BYTES}
    before = dict(bloated)
    assert inject_rejection_patterns(bloated, _sample_block()) is False
    assert "rejection_patterns" not in bloated
    assert bloated == before  # never displaces what was already assembled


def test_inject_drops_block_when_block_itself_overflows_remaining_budget():
    """A context near (not over) the budget still drops the block, not the context."""
    block = _sample_block()
    near_budget = {
        "mission_critical": "x" * (_PROJECT_CONTEXT_SOFT_BUDGET_BYTES - 100)
    }
    assert inject_rejection_patterns(near_budget, block) is False
    assert "rejection_patterns" not in near_budget


def test_inject_respects_explicit_budget():
    ctx: dict = {}
    assert (
        inject_rejection_patterns(ctx, _sample_block(), budget_bytes=10) is False
    )
    assert ctx == {}
    assert (
        inject_rejection_patterns(ctx, _sample_block(), budget_bytes=10_000) is True
    )


def test_inject_noop_on_empty_block():
    ctx: dict = {}
    assert inject_rejection_patterns(ctx, None) is False
    assert inject_rejection_patterns(ctx, {}) is False
    assert ctx == {}


# ── claim-endpoint injection ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_claim_injects_rejection_patterns_for_create(
    client: AsyncClient, db_session
):
    """Claim ships project_context.rejection_patterns for the claiming type."""
    project = await _make_project(db_session)
    for _ in range(3):
        db_session.add(
            _rejected_task(
                project.id,
                task_type="create",
                categories={"correctness": 2, "test-coverage": 1},
            )
        )
    # Fix-type rejections must not leak into a create claim's block.
    for _ in range(3):
        db_session.add(
            _rejected_task(project.id, task_type="fix", categories={"security": 1})
        )
    await db_session.commit()

    vm_id = f"agent-vm-rejection-{uuid.uuid4().hex[:8]}"
    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "Add feature"}],
        vm_instance_id=vm_id,
        context={"related_files": ["backend/app/foo.py"]},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), vm_id)
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200
    project_ctx = resp.json().get("project_context", {})
    block = project_ctx.get("rejection_patterns")
    assert block is not None, (
        f"expected rejection_patterns in project_context, got {list(project_ctx.keys())}"
    )
    assert block["task_type"] == "create"
    assert block["window_days"] == 7
    assert block["sample_size"] == 3
    categories = {c["category"] for c in block["categories"]}
    assert "correctness" in categories
    assert "security" not in categories  # fix-type pattern stays out
    for cat in block["categories"]:
        assert cat["guidance"]


@pytest.mark.asyncio
async def test_claim_omits_rejection_patterns_below_sample_floor(
    client: AsyncClient, db_session
):
    """No block when the claiming type has too few rejected tasks in the window."""
    project = await _make_project(db_session)
    db_session.add(
        _rejected_task(project.id, task_type="create", categories={"security": 1})
    )
    await db_session.commit()

    vm_id = f"agent-vm-rejection-{uuid.uuid4().hex[:8]}"
    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "Add feature"}],
        vm_instance_id=vm_id,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), vm_id)
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200
    project_ctx = resp.json().get("project_context", {})
    assert "rejection_patterns" not in project_ctx


@pytest.mark.asyncio
async def test_claim_omits_rejection_patterns_for_non_code_change_types(
    client: AsyncClient, db_session
):
    """Research claims never carry the block even when patterns exist."""
    project = await _make_project(db_session)
    for _ in range(3):
        db_session.add(
            _rejected_task(
                project.id, task_type="research", categories={"correctness": 1}
            )
        )
    await db_session.commit()

    vm_id = f"agent-vm-rejection-{uuid.uuid4().hex[:8]}"
    task = Task(
        project_id=project.id,
        payloads=[{"type": "research", "prompt": "Investigate"}],
        vm_instance_id=vm_id,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), vm_id)
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200
    project_ctx = resp.json().get("project_context") or {}
    assert "rejection_patterns" not in project_ctx
