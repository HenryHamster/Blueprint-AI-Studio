"""Tests for strategic_spawning.py decision logic.

Covers:
  - Type selection: design_doc_update on architecture cycles, strategic_review on strategy cycles
  - force_strategic: no vision purpose, no active directions
  - strategic_failures >= 3 blocks force_strategic (without auto-decay)
  - operator_rejection_rate calculation in _spawn_strategic_review direction stats
  - Active-task guard: _spawn_design_doc_update and _spawn_strategic_review return False when blocked
"""
import pytest
from unittest.mock import AsyncMock
from sqlalchemy import select

import app.shared_state as _shared_state
from app.models import Project, Task, TaskStatus
from app.autonomous_research import _spawn_research_task
from app.strategic_spawning import _spawn_design_doc_update, _spawn_strategic_review
from app.max_plan_pool import _max_plan_pools, _account_by_id


# ── Account pool fixture (required by _spawn_research_task) ───────────────────

@pytest.fixture(autouse=True)
def _ensure_test_account_pool():
    """Ensure _spawn_research_task finds an available account for the test user."""
    uid = "test-user-123"
    entry = {
        "id": "test-acct-ss", "user_id": uid, "name": "test", "enabled": True,
        "active_tasks": 0, "oauth_token": "sk-ant-test", "total_tasks": 0,
        "token_created_at": None, "claude_config_json": None,
    }
    _max_plan_pools.setdefault(uid, []).append(entry)
    _account_by_id["test-acct-ss"] = entry
    yield
    _max_plan_pools.get(uid, [])[:] = [a for a in _max_plan_pools.get(uid, []) if a["id"] != "test-acct-ss"]
    _account_by_id.pop("test-acct-ss", None)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_project(**kwargs) -> Project:
    """Build a Project with sensible test defaults."""
    defaults = dict(
        name="Test Project",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[],
        vision={},
    )
    defaults.update(kwargs)
    return Project(**defaults)


# ── Type selection: arch_frequency / strategic_cycle_count ────────────────────

@pytest.mark.asyncio
async def test_architecture_cycle_spawns_design_doc_update(db_session):
    """strategic_cycle_count divisible by arch_frequency → design_doc_update."""
    # With arch_frequency=3, _strategic_cycle_count=2 → next=3 → architecture cycle.
    project = _make_project(
        vision={
            # No purpose → force_strategic fires due to missing purpose
            "_strategic_cycle_count": 2,
            "_research_spawn_count": 5,
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    old = _shared_state.runtime_config.get("architecture_review_frequency", 3)
    _shared_state.runtime_config["architecture_review_frequency"] = 3
    try:
        await _spawn_research_task(project, db_session)
    finally:
        _shared_state.runtime_config["architecture_review_frequency"] = old

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    tasks = result.scalars().all()
    assert len(tasks) == 1, f"Expected one task, got {len(tasks)}"
    assert tasks[0].payloads[0]["type"] == "design_doc_update", (
        f"Expected design_doc_update on architecture cycle, got {tasks[0].payloads[0]['type']}"
    )


@pytest.mark.asyncio
async def test_strategy_only_cycle_spawns_strategic_review(db_session):
    """strategic_cycle_count NOT divisible by arch_frequency → strategic_review."""
    # With arch_frequency=3, _strategic_cycle_count=1 → next=2 → strategy-only cycle.
    project = _make_project(
        vision={
            "_strategic_cycle_count": 1,
            "_research_spawn_count": 5,
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    old = _shared_state.runtime_config.get("architecture_review_frequency", 3)
    _shared_state.runtime_config["architecture_review_frequency"] = 3
    try:
        await _spawn_research_task(project, db_session)
    finally:
        _shared_state.runtime_config["architecture_review_frequency"] = old

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    tasks = result.scalars().all()
    assert len(tasks) == 1, f"Expected one task, got {len(tasks)}"
    assert tasks[0].payloads[0]["type"] == "strategic_review", (
        f"Expected strategic_review on strategy-only cycle, got {tasks[0].payloads[0]['type']}"
    )


@pytest.mark.asyncio
async def test_arch_frequency_zero_always_spawns_strategic_review(db_session):
    """arch_frequency=0 disables architecture cycles → always strategic_review."""
    # _strategic_cycle_count=2 would be an arch cycle with freq=3, but freq=0 disables it.
    project = _make_project(
        vision={
            "_strategic_cycle_count": 2,
            "_research_spawn_count": 5,
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    old = _shared_state.runtime_config.get("architecture_review_frequency", 3)
    _shared_state.runtime_config["architecture_review_frequency"] = 0
    try:
        await _spawn_research_task(project, db_session)
    finally:
        _shared_state.runtime_config["architecture_review_frequency"] = old

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    tasks = result.scalars().all()
    assert len(tasks) == 1, f"Expected one task, got {len(tasks)}"
    assert tasks[0].payloads[0]["type"] == "strategic_review", (
        f"Expected strategic_review when arch_frequency=0, got {tasks[0].payloads[0]['type']}"
    )


@pytest.mark.asyncio
async def test_strategic_cycle_count_increments_in_vision(db_session):
    """After a strategic cycle spawns, _strategic_cycle_count increments by 1."""
    project = _make_project(
        vision={
            "_strategic_cycle_count": 0,
            "_research_spawn_count": 5,
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _spawn_research_task(project, db_session)

    # Assert on in-memory attribute — refresh would re-read from DB, overwriting
    # uncommitted vision changes (see filter-test-session-refresh-pattern).
    assert (project.vision or {}).get("_strategic_cycle_count") == 1, (
        "_strategic_cycle_count should be incremented to 1 after one strategic spawn"
    )


# ── force_strategic: no vision purpose ────────────────────────────────────────

@pytest.mark.asyncio
async def test_no_vision_purpose_triggers_force_strategic(db_session):
    """Missing vision purpose triggers force_strategic even when spawn count is low."""
    project = _make_project(
        directions=[
            {"id": "d-001", "status": "active", "title": "Test direction", "possibly_saturated": False},
        ],
        vision={
            # No "purpose" key → force_strategic fires
            "_research_spawn_count": 1,  # Not at strategic_frequency (6), but force overrides
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _spawn_research_task(project, db_session)

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    tasks = result.scalars().all()
    task_types = [t.payloads[0]["type"] for t in tasks]
    assert any(t in ("design_doc_update", "strategic_review") for t in task_types), (
        f"Expected a strategic task when vision has no purpose, got {task_types}"
    )


@pytest.mark.asyncio
async def test_no_active_directions_triggers_force_strategic(db_session):
    """No active directions triggers force_strategic when strategic_failures < 3."""
    project = _make_project(
        directions=[
            {"id": "d-001", "status": "paused", "title": "Paused direction"},
        ],
        vision={
            "purpose": "test purpose",
            "_research_spawn_count": 1,  # Not at strategic_frequency, but force applies
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _spawn_research_task(project, db_session)

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    tasks = result.scalars().all()
    task_types = [t.payloads[0]["type"] for t in tasks]
    assert any(t in ("design_doc_update", "strategic_review") for t in task_types), (
        f"Expected a strategic task when no active directions, got {task_types}"
    )


# ── force_strategic: strategic_failures >= 3 blocks ──────────────────────────

@pytest.mark.asyncio
async def test_strategic_failures_exactly_three_blocks_force_strategic(db_session):
    """_strategic_failures == 3 (no decay timestamp) → force_strategic is False → no task."""
    project = _make_project(
        directions=[],  # No active directions — would normally force strategic
        vision={
            "purpose": "test purpose",
            "_strategic_failures": 3,
            # No _strategic_failures_at → auto-decay cannot apply
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _spawn_research_task(project, db_session)

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    tasks = result.scalars().all()
    assert len(tasks) == 0, (
        f"Expected no tasks when strategic_failures >= 3 blocks force_strategic, "
        f"got {[t.payloads[0]['type'] for t in tasks]}"
    )


@pytest.mark.asyncio
async def test_strategic_failures_below_three_allows_force_strategic(db_session):
    """_strategic_failures == 2 → force_strategic is allowed → spawns a strategic task."""
    project = _make_project(
        directions=[],  # No active directions → force_strategic condition met
        vision={
            "purpose": "test purpose",
            "_strategic_failures": 2,
            "_research_spawn_count": 1,
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _spawn_research_task(project, db_session)

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    tasks = result.scalars().all()
    assert len(tasks) == 1, f"Expected one strategic task, got {len(tasks)}"
    assert tasks[0].payloads[0]["type"] in ("design_doc_update", "strategic_review")


# ── operator_rejection_rate in _spawn_strategic_review ────────────────────────

@pytest.mark.asyncio
async def test_operator_rejection_rate_computed_correctly(db_session):
    """operator_rejection_rate = rejected / (merged + rejected) when total_rejected > 0."""
    project = _make_project(
        directions=[
            {
                "id": "d-rej01",
                "status": "active",
                "text": "Direction with rejections",
                "priority": "medium",
                "research_history": [
                    {"prs_merged": 3, "prs_rejected": 1},
                    {"prs_merged": 1, "prs_rejected": 1},
                ],
            }
        ],
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is True
    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    tasks = result.scalars().all()
    assert len(tasks) == 1
    payload = tasks[0].payloads[0]
    d_stat = next(s for s in payload["directions_summary"] if s["id"] == "d-rej01")

    # 4 merged + 2 rejected = 6 total PRs, rate = 2/6 ≈ 0.33
    assert "operator_rejection_rate" in d_stat, "Expected operator_rejection_rate in direction stat"
    assert d_stat["operator_rejection_rate"] == round(2 / 6, 2)
    assert d_stat.get("prs_rejected") == 2


@pytest.mark.asyncio
async def test_operator_rejection_rate_absent_when_no_rejections(db_session):
    """operator_rejection_rate and prs_rejected are absent when total_rejected == 0."""
    project = _make_project(
        directions=[
            {
                "id": "d-norej01",
                "status": "active",
                "text": "Direction with no rejections",
                "priority": "medium",
                "research_history": [
                    {"prs_merged": 4, "prs_rejected": 0},
                ],
            }
        ],
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is True
    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    tasks = result.scalars().all()
    payload = tasks[0].payloads[0]
    d_stat = next(s for s in payload["directions_summary"] if s["id"] == "d-norej01")

    assert "operator_rejection_rate" not in d_stat, (
        "operator_rejection_rate should not appear when total_rejected == 0"
    )
    assert "prs_rejected" not in d_stat, (
        "prs_rejected should not appear when total_rejected == 0"
    )


@pytest.mark.asyncio
async def test_operator_rejection_rate_uses_history_aggregate(db_session):
    """operator_rejection_rate aggregates from both research_history and history_aggregate."""
    project = _make_project(
        directions=[
            {
                "id": "d-agg01",
                "status": "active",
                "text": "Direction with aggregated history",
                "priority": "high",
                "research_history": [
                    {"prs_merged": 1, "prs_rejected": 1},  # 1 + 1
                ],
                "history_aggregate": {
                    "total_prs_merged": 4,
                    "total_prs_rejected": 2,  # 4 + 2
                    "total_cycles": 5,
                    "total_tasks_failed": 0,
                },
            }
        ],
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    tasks = result.scalars().all()
    payload = tasks[0].payloads[0]
    d_stat = next(s for s in payload["directions_summary"] if s["id"] == "d-agg01")

    # history: 1 merged + 1 rejected; aggregate: 4 merged + 2 rejected
    # totals: 5 merged + 3 rejected = 8 PRs → rate = 3/8 = 0.375 → 0.38
    assert d_stat.get("prs_rejected") == 3
    assert d_stat.get("operator_rejection_rate") == round(3 / 8, 2)


@pytest.mark.asyncio
async def test_operator_rejection_rate_zero_rejected_in_history_aggregate(db_session):
    """operator_rejection_rate absent when total_rejected sums to zero across both sources."""
    project = _make_project(
        directions=[
            {
                "id": "d-agg02",
                "status": "active",
                "text": "Clean direction",
                "priority": "low",
                "research_history": [{"prs_merged": 2, "prs_rejected": 0}],
                "history_aggregate": {"total_prs_merged": 3, "total_prs_rejected": 0, "total_cycles": 3},
            }
        ],
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    tasks = result.scalars().all()
    payload = tasks[0].payloads[0]
    d_stat = next(s for s in payload["directions_summary"] if s["id"] == "d-agg02")

    assert "operator_rejection_rate" not in d_stat
    assert "prs_rejected" not in d_stat


# ── Active-task guard in _spawn_design_doc_update / _spawn_strategic_review ───

@pytest.mark.asyncio
async def test_spawn_design_doc_update_skips_when_strategic_task_active(db_session, mocker):
    """_spawn_design_doc_update returns False when a strategic task is already active."""
    mocker.patch(
        "app.task_lifecycle._has_active_strategic_task",
        new=AsyncMock(return_value=True),
    )
    project = _make_project(vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    spawned = await _spawn_design_doc_update(project, db_session)
    assert spawned is False


@pytest.mark.asyncio
async def test_spawn_strategic_review_skips_when_strategic_task_active(db_session, mocker):
    """_spawn_strategic_review returns False when a strategic task is already active."""
    mocker.patch(
        "app.task_lifecycle._has_active_strategic_task",
        new=AsyncMock(return_value=True),
    )
    project = _make_project(vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is False


@pytest.mark.asyncio
async def test_spawn_design_doc_update_proceeds_when_no_active_strategic(db_session):
    """_spawn_design_doc_update returns True and creates a task when no strategic task is active."""
    project = _make_project(vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    spawned = await _spawn_design_doc_update(project, db_session)

    assert spawned is True
    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    tasks = result.scalars().all()
    assert len(tasks) == 1
    assert tasks[0].payloads[0]["type"] == "design_doc_update"
    assert tasks[0].status == TaskStatus.QUEUED


@pytest.mark.asyncio
async def test_spawn_strategic_review_proceeds_when_no_active_strategic(db_session):
    """_spawn_strategic_review returns True and creates a task when no strategic task is active."""
    project = _make_project(
        directions=[
            {"id": "d-sr01", "status": "active", "text": "Some direction", "priority": "medium"},
        ],
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is True
    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    tasks = result.scalars().all()
    assert len(tasks) == 1
    assert tasks[0].payloads[0]["type"] == "strategic_review"
    assert tasks[0].status == TaskStatus.QUEUED
