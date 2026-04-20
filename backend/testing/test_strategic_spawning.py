"""Tests for strategic_spawning.py decision logic.

Covers:
  - Type selection: design_doc_update on architecture cycles, strategic_review on strategy cycles
  - force_strategic: no vision purpose, no active directions
  - strategic_failures >= 3 blocks force_strategic (without auto-decay)
  - operator_rejection_rate calculation in _spawn_strategic_review direction stats
  - Active-task guard: _spawn_design_doc_update and _spawn_strategic_review return False when blocked
  - _build_shipped_changes_manifest: cutoff, filtering, grouping, cap
"""
import uuid
from datetime import datetime, timezone, timedelta

import pytest
from unittest.mock import AsyncMock
from sqlalchemy import select

import app.shared_state as _shared_state
from app.models import Project, Task, TaskStatus
from app.autonomous_research import _spawn_research_task
from app.strategic_spawning import _spawn_design_doc_update, _spawn_strategic_review, _build_shipped_changes_manifest
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


def _patch_spawning_deps(mocker):
    """Patch external dependencies required by the spawning functions."""
    mocker.patch(
        "app.strategic_spawning.validate_internal_payload",
        side_effect=lambda p, _src: p,
    )
    mocker.patch(
        "app.strategic_analysis._build_outcome_evaluation",
        return_value={},
    )


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


@pytest.mark.asyncio
async def test_tasks_failed_infra_includes_history_aggregate(db_session):
    """tasks_failed_infra aggregates from both research_history and history_aggregate."""
    project = _make_project(
        directions=[
            {
                "id": "d-agg03",
                "status": "active",
                "text": "Direction with archived infra failures",
                "priority": "high",
                "research_history": [
                    {"tasks_failed": 1, "tasks_failed_infra": 1},
                ],
                "history_aggregate": {
                    "total_cycles": 5,
                    "total_prs_merged": 0,
                    "total_prs_rejected": 0,
                    "total_tasks_failed": 4,
                    "total_tasks_failed_infra": 3,
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
    d_stat = next(s for s in payload["directions_summary"] if s["id"] == "d-agg03")

    # history: 1 failed (1 infra); aggregate: 4 failed (3 infra)
    assert d_stat["tasks_failed"] == 5
    assert d_stat.get("tasks_failed_infra") == 4


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


# ── Additional _spawn_design_doc_update tests ─────────────────────────────────

@pytest.mark.asyncio
async def test_spawn_design_doc_update_adds_cycle_history_entry(db_session, mocker):
    """_spawn_design_doc_update writes a new entry to _strategic_cycle_history."""
    _patch_spawning_deps(mocker)
    mocker.patch(
        "app.task_lifecycle._has_active_strategic_task",
        new=AsyncMock(return_value=False),
    )
    project = _make_project(vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _spawn_design_doc_update(project, db_session)

    cycle_history = (project.vision or {}).get("_strategic_cycle_history", [])
    assert len(cycle_history) == 1
    entry = cycle_history[0]
    assert entry.get("outcome") == "in_progress"
    assert entry.get("phase1_task_id") is not None
    assert entry.get("cycle_id", "").startswith("sc-")


@pytest.mark.asyncio
async def test_spawn_design_doc_update_cycle_history_capped_at_10(db_session, mocker):
    """_spawn_design_doc_update caps _strategic_cycle_history at 10 entries."""
    _patch_spawning_deps(mocker)
    mocker.patch(
        "app.task_lifecycle._has_active_strategic_task",
        new=AsyncMock(return_value=False),
    )
    existing = [{"cycle_id": f"sc-{i:08x}", "outcome": "done"} for i in range(10)]
    project = _make_project(vision={
        "purpose": "test purpose",
        "_strategic_cycle_history": existing,
    })
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _spawn_design_doc_update(project, db_session)

    cycle_history = (project.vision or {}).get("_strategic_cycle_history", [])
    assert len(cycle_history) == 10
    assert cycle_history[-1].get("outcome") == "in_progress"


# ── Additional _spawn_strategic_review tests ──────────────────────────────────

@pytest.mark.asyncio
async def test_spawn_strategic_review_records_phase2_task_id(db_session, mocker):
    """When called with cycle_id, _spawn_strategic_review records phase2_task_id in history."""
    _patch_spawning_deps(mocker)
    mocker.patch(
        "app.task_lifecycle._has_active_strategic_task",
        new=AsyncMock(return_value=False),
    )
    cycle_id = "sc-abc12345"
    project = _make_project(vision={
        "purpose": "test purpose",
        "_strategic_cycle_history": [
            {"cycle_id": cycle_id, "phase1_task_id": "old-id", "phase1_completed_at": None}
        ],
    })
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _spawn_strategic_review(project, db_session, cycle_id=cycle_id)

    history = (project.vision or {}).get("_strategic_cycle_history", [])
    entry = next((e for e in history if e.get("cycle_id") == cycle_id), None)
    assert entry is not None
    assert entry.get("phase2_task_id") is not None
    assert entry.get("phase1_completed_at") is not None


@pytest.mark.asyncio
async def test_spawn_strategic_review_no_cycle_id_does_not_modify_history(db_session, mocker):
    """When called without cycle_id, _spawn_strategic_review leaves cycle history unchanged."""
    _patch_spawning_deps(mocker)
    mocker.patch(
        "app.task_lifecycle._has_active_strategic_task",
        new=AsyncMock(return_value=False),
    )
    project = _make_project(vision={
        "purpose": "test purpose",
        "_strategic_cycle_history": [{"cycle_id": "existing", "outcome": "ok"}],
    })
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _spawn_strategic_review(project, db_session, cycle_id=None)

    history = (project.vision or {}).get("_strategic_cycle_history", [])
    assert len(history) == 1
    assert history[0].get("outcome") == "ok"


@pytest.mark.asyncio
async def test_spawn_strategic_review_task_context_includes_cycle_id(db_session, mocker):
    """Task created by _spawn_strategic_review has strategic_cycle_id in context."""
    _patch_spawning_deps(mocker)
    mocker.patch(
        "app.task_lifecycle._has_active_strategic_task",
        new=AsyncMock(return_value=False),
    )
    cycle_id = "sc-testcycle"
    project = _make_project(vision={
        "purpose": "test purpose",
        "_strategic_cycle_history": [{"cycle_id": cycle_id}],
    })
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _spawn_strategic_review(project, db_session, cycle_id=cycle_id)

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    tasks = result.scalars().all()
    assert tasks[0].context.get("strategic_cycle_id") == cycle_id


# ── previous_decision_outcomes in _spawn_strategic_review payload ────────────

@pytest.mark.asyncio
async def test_previous_decision_outcomes_populated_from_prior_cycle(db_session):
    """Payload surfaces current performance of directions created/retired by the prior cycle."""
    project = _make_project(
        directions=[
            {
                "id": "d-new01", "status": "active", "text": "Newly created direction",
                "priority": "medium",
                "research_history": [{"prs_merged": 2, "prs_rejected": 0}],
            },
            {
                "id": "d-retired01", "status": "retired",
                "text": "Direction retired last cycle", "priority": "low",
                "research_history": [],
            },
        ],
        vision={
            "purpose": "test purpose",
            "_strategic_cycle_history": [
                {
                    "cycle_id": "sc-prev0001",
                    "outcome": "completed",
                    "phase2_completed_at": "2026-04-19T00:00:00+00:00",
                    "created_direction_ids": ["d-new01"],
                    "retired_direction_ids": ["d-retired01"],
                    "summary_text": "rev 2, 3 active, 1 created, 1 retired",
                },
            ],
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    tasks = result.scalars().all()
    payload = tasks[0].payloads[0]
    prev = payload.get("previous_decision_outcomes")
    assert prev is not None, "Expected previous_decision_outcomes in payload"
    assert prev["cycle_id"] == "sc-prev0001"
    assert len(prev["created"]) == 1
    assert len(prev["retired"]) == 1
    new_stat = prev["created"][0]
    assert new_stat["direction_id"] == "d-new01"
    assert new_stat["role"] == "created"
    assert new_stat["status"] == "active"
    assert new_stat["prs_merged"] == 2
    assert new_stat["cycles_active"] == 1
    assert "quality_score" in new_stat
    assert new_stat["auto_paused"] is False
    retired_stat = prev["retired"][0]
    assert retired_stat["role"] == "retired"
    assert retired_stat["status"] == "retired"


@pytest.mark.asyncio
async def test_previous_decision_outcomes_absent_when_no_prior_decisions(db_session):
    """No created/retired ids in prior cycles → previous_decision_outcomes omitted."""
    project = _make_project(
        directions=[{"id": "d-a", "status": "active", "text": "d", "priority": "medium"}],
        vision={
            "purpose": "test",
            "_strategic_cycle_history": [
                {"cycle_id": "sc-old", "outcome": "completed"},  # no created/retired ids
            ],
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _spawn_strategic_review(project, db_session)
    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    payload = result.scalars().first().payloads[0]
    assert "previous_decision_outcomes" not in payload


@pytest.mark.asyncio
async def test_previous_decision_outcomes_surfaces_created_rationales(db_session):
    """created_rationales from prior cycle history appear on created direction stats."""
    project = _make_project(
        directions=[
            {
                "id": "d-new01", "status": "active", "text": "new direction",
                "priority": "medium",
                "research_history": [{"prs_merged": 2}],
            },
        ],
        vision={
            "purpose": "test",
            "_strategic_cycle_history": [
                {
                    "cycle_id": "sc-rat0001",
                    "outcome": "completed",
                    "created_direction_ids": ["d-new01"],
                    "retired_direction_ids": [],
                    "created_rationales": {
                        "d-new01": "Observed gap in error handling coverage; new direction targets resilience."
                    },
                },
            ],
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _spawn_strategic_review(project, db_session)

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    payload = result.scalars().first().payloads[0]
    prev = payload.get("previous_decision_outcomes")
    assert prev is not None
    new_stat = prev["created"][0]
    assert new_stat["direction_id"] == "d-new01"
    assert new_stat.get("rationale") == (
        "Observed gap in error handling coverage; new direction targets resilience."
    )


@pytest.mark.asyncio
async def test_previous_decision_outcomes_surfaces_retired_rationales(db_session):
    """retired_rationales are attached to retired direction stats (including missing ones)."""
    project = _make_project(
        directions=[],  # retired direction removed
        vision={
            "purpose": "test",
            "_strategic_cycle_history": [
                {
                    "cycle_id": "sc-rat0002",
                    "outcome": "completed",
                    "created_direction_ids": [],
                    "retired_direction_ids": ["d-retired01"],
                    "retired_rationales": {
                        "d-retired01": "Saturated; low PR merge volume for 3 cycles."
                    },
                },
            ],
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _spawn_strategic_review(project, db_session)

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    payload = result.scalars().first().payloads[0]
    prev = payload.get("previous_decision_outcomes")
    assert prev is not None
    retired_stat = prev["retired"][0]
    assert retired_stat["direction_id"] == "d-retired01"
    assert retired_stat.get("rationale") == "Saturated; low PR merge volume for 3 cycles."


@pytest.mark.asyncio
async def test_previous_decision_outcomes_surfaces_cycle_reasoning(db_session):
    """cycle_reasoning from prior cycle history appears at the top level of the outcome block."""
    project = _make_project(
        directions=[
            {"id": "d-x", "status": "active", "text": "d", "priority": "medium"},
        ],
        vision={
            "purpose": "test",
            "_strategic_cycle_history": [
                {
                    "cycle_id": "sc-reason01",
                    "outcome": "completed",
                    "created_direction_ids": ["d-x"],
                    "cycle_reasoning": (
                        "Portfolio skewed toward infra; created direction to restore "
                        "feature-work balance."
                    ),
                },
            ],
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _spawn_strategic_review(project, db_session)

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    payload = result.scalars().first().payloads[0]
    prev = payload.get("previous_decision_outcomes")
    assert prev is not None
    assert prev.get("cycle_reasoning") == (
        "Portfolio skewed toward infra; created direction to restore feature-work balance."
    )


@pytest.mark.asyncio
async def test_previous_decision_outcomes_rationale_absent_when_not_recorded(db_session):
    """When rationale fields are not present in the cycle entry, direction stats have no rationale key."""
    project = _make_project(
        directions=[
            {"id": "d-r", "status": "active", "text": "d", "priority": "medium",
             "research_history": []},
        ],
        vision={
            "purpose": "test",
            "_strategic_cycle_history": [
                {
                    "cycle_id": "sc-norat",
                    "outcome": "completed",
                    "created_direction_ids": ["d-r"],
                    # no created_rationales / retired_rationales / cycle_reasoning
                },
            ],
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _spawn_strategic_review(project, db_session)

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    payload = result.scalars().first().payloads[0]
    prev = payload.get("previous_decision_outcomes")
    assert prev is not None
    assert "rationale" not in prev["created"][0]
    assert "cycle_reasoning" not in prev


@pytest.mark.asyncio
async def test_previous_decision_outcomes_missing_retired_direction(db_session):
    """A retired direction id no longer in project.directions is reported as status=missing."""
    project = _make_project(
        directions=[],  # retired direction was removed
        vision={
            "purpose": "test",
            "_strategic_cycle_history": [
                {
                    "cycle_id": "sc-gone",
                    "outcome": "completed",
                    "created_direction_ids": [],
                    "retired_direction_ids": ["d-deleted"],
                },
            ],
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _spawn_strategic_review(project, db_session)
    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    payload = result.scalars().first().payloads[0]
    prev = payload.get("previous_decision_outcomes")
    assert prev is not None
    assert prev["retired"] == [{"direction_id": "d-deleted", "role": "retired", "status": "missing"}]


# ── _build_shipped_changes_manifest ──────────────────────────────────────────

def _make_merged_task(project_id, title="Fix bug", direction_id="d-001", changed_files=None, scope="small"):
    """Create a COMPLETED task with pr_merged=True context."""
    return Task(
        project_id=project_id,
        title=title,
        description="test",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context={
            "pr_merged": True,
            "direction_id": direction_id,
            "_pr_changed_files": changed_files or ["backend/app/foo.py"],
            "scope": scope,
        },
    )


@pytest.mark.asyncio
async def test_build_shipped_manifest_empty_when_no_merged_tasks(db_session):
    """Returns empty list when there are no completed tasks with pr_merged=True."""
    project = _make_project(vision={"purpose": "test"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Add a COMPLETED task without pr_merged
    task = Task(
        project_id=project.id,
        title="No merge",
        description="test",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context={"pr_merged": False},
    )
    db_session.add(task)
    await db_session.commit()

    manifest = await _build_shipped_changes_manifest(project, db_session)
    assert manifest == []


@pytest.mark.asyncio
async def test_build_shipped_manifest_groups_by_direction(db_session):
    """Tasks are grouped by direction_id; all_changed_files is deduplicated."""
    project = _make_project(vision={"purpose": "test"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(_make_merged_task(project.id, "Task A", direction_id="d-001", changed_files=["a.py"]))
    db_session.add(_make_merged_task(project.id, "Task B", direction_id="d-001", changed_files=["a.py", "b.py"]))
    db_session.add(_make_merged_task(project.id, "Task C", direction_id="d-002", changed_files=["c.py"]))
    await db_session.commit()

    manifest = await _build_shipped_changes_manifest(project, db_session)

    assert len(manifest) == 2
    by_dir = {g["direction_id"]: g for g in manifest}
    assert "d-001" in by_dir
    assert "d-002" in by_dir
    assert len(by_dir["d-001"]["tasks"]) == 2
    assert len(by_dir["d-002"]["tasks"]) == 1
    # Deduplication: a.py appears twice in d-001 but should appear once
    assert by_dir["d-001"]["all_changed_files"].count("a.py") == 1
    assert "b.py" in by_dir["d-001"]["all_changed_files"]


@pytest.mark.asyncio
async def test_build_shipped_manifest_excludes_non_merged(db_session):
    """Tasks without pr_merged=True are excluded even if COMPLETED."""
    project = _make_project(vision={"purpose": "test"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(_make_merged_task(project.id, "Merged task", direction_id="d-001"))
    # Non-merged completed task
    non_merged = Task(
        project_id=project.id,
        title="Unmerged task",
        description="test",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context={"pr_merged": False, "direction_id": "d-001"},
    )
    db_session.add(non_merged)
    await db_session.commit()

    manifest = await _build_shipped_changes_manifest(project, db_session)

    assert len(manifest) == 1
    assert len(manifest[0]["tasks"]) == 1
    assert manifest[0]["tasks"][0]["title"] == "Merged task"


@pytest.mark.asyncio
async def test_build_shipped_manifest_unassigned_direction(db_session):
    """Tasks with no direction_id are grouped under 'unassigned' direction_id=None."""
    project = _make_project(vision={"purpose": "test"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Orphan task",
        description="test",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context={"pr_merged": True, "_pr_changed_files": ["x.py"], "scope": "medium"},
    )
    db_session.add(task)
    await db_session.commit()

    manifest = await _build_shipped_changes_manifest(project, db_session)

    assert len(manifest) == 1
    assert manifest[0]["direction_id"] is None  # grouped with None key


@pytest.mark.asyncio
async def test_build_shipped_manifest_cutoff_from_design_doc(db_session):
    """Tasks completed before design_doc._meta.last_updated are excluded."""
    project = _make_project(
        vision={"purpose": "test"},
        design_doc={
            "_meta": {
                # Future cutoff — excludes all current tasks
                "last_updated": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
            }
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(_make_merged_task(project.id, "Old merged task", direction_id="d-001"))
    await db_session.commit()

    manifest = await _build_shipped_changes_manifest(project, db_session)
    # All tasks have updated_at now (before the future cutoff), so they're excluded
    assert manifest == []


@pytest.mark.asyncio
async def test_build_shipped_manifest_cutoff_from_cycle_history(db_session):
    """Falls back to _strategic_cycle_history phase1_completed_at when design_doc has no timestamp."""
    project = _make_project(
        vision={
            "purpose": "test",
            "_strategic_cycle_history": [
                {
                    "cycle_id": "sc-old",
                    # Future cutoff in cycle history
                    "phase1_completed_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
                }
            ],
        },
        design_doc={"_meta": {}},  # No last_updated
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(_make_merged_task(project.id, "Excluded task", direction_id="d-001"))
    await db_session.commit()

    manifest = await _build_shipped_changes_manifest(project, db_session)
    assert manifest == []


# ── predict_spawn_success: quality-score and momentum signals ─────────────────


class TestPredictSpawnSuccessQualityAndMomentum:
    """Signals 16 (quality score) and 17 (momentum factor) in predict_spawn_success."""

    @pytest.fixture(autouse=True)
    def _clear_pipeline_rates(self):
        from app.shared_state import runtime_config
        prev = runtime_config.get("pipeline_scope_merge_rates")
        runtime_config["pipeline_scope_merge_rates"] = {}
        yield
        if prev is None:
            runtime_config.pop("pipeline_scope_merge_rates", None)
        else:
            runtime_config["pipeline_scope_merge_rates"] = prev

    def test_quality_score_signal_present_with_history(self):
        from app.spawn_calibration import predict_spawn_success
        healthy = {
            "research_history": [
                {"taxonomy_summary": {"code_defect": 3}, "tasks_created": 2, "prs_merged": 2},
            ],
        }
        poor = {
            "research_history": [
                {"taxonomy_summary": {"infra_related": 3, "review_quality": 2}, "tasks_created": 2, "prs_merged": 0},
            ],
        }
        assert predict_spawn_success("small", healthy) > predict_spawn_success("small", poor)

    def test_no_history_skips_quality_signal(self):
        from app.spawn_calibration import predict_spawn_success
        assert predict_spawn_success("small", {}) == 0.5

    def test_momentum_hot_streak_boosts_prediction(self):
        from app.spawn_calibration import predict_spawn_success
        hot = {
            "research_history": [
                {"tasks_created": 2, "prs_merged": 1},
                {"tasks_created": 2, "prs_merged": 1},
            ],
        }
        neutral = {
            "research_history": [
                {"tasks_created": 2, "prs_merged": 1},
            ],
        }
        assert predict_spawn_success("small", hot) > predict_spawn_success("small", neutral)

    def test_momentum_stall_penalizes_prediction(self):
        from app.spawn_calibration import predict_spawn_success
        stalled = {
            "research_history": [
                {"tasks_created": 0, "taxonomy_summary": {"code_defect": 1}},
                {"tasks_created": 0, "taxonomy_summary": {"code_defect": 1}},
                {"tasks_created": 0, "taxonomy_summary": {"code_defect": 1}},
            ],
        }
        neutral = {
            "research_history": [
                {"tasks_created": 0, "taxonomy_summary": {"code_defect": 1}},
            ],
        }
        assert predict_spawn_success("small", stalled) < predict_spawn_success("small", neutral)

    def test_result_within_bounds(self):
        from app.spawn_calibration import predict_spawn_success
        direction = {
            "research_history": [
                {"tasks_created": 2, "prs_merged": 1},
            ],
        }
        result = predict_spawn_success("small", direction)
        assert 0.0 <= result <= 1.0

    def test_remediation_regression_penalizes_prediction(self):
        from app.spawn_calibration import predict_spawn_success
        base_history = [{"tasks_created": 2, "prs_merged": 1}]
        clean = {
            "research_history": base_history,
            "remediation_history": [
                {"result": "merged", "fingerprint": "fp-a"},
                {"result": "merged", "fingerprint": "fp-b"},
            ],
        }
        regressed = {
            "research_history": base_history,
            "remediation_history": [
                {"result": "merged", "fingerprint": "fp-a"},
                {"result": "merged", "fingerprint": "fp-b"},
                {"result": "failed", "fingerprint": "fp-a"},
                {"result": "failed", "fingerprint": "fp-b"},
            ],
        }
        assert predict_spawn_success("small", regressed) < predict_spawn_success("small", clean)
