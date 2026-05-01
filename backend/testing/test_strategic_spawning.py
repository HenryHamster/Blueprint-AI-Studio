"""Tests for strategic_spawning.py decision logic.

Covers:
  - Type selection: design_doc_update on architecture cycles, strategic_review on strategy cycles
  - force_strategic: no vision purpose, no active directions
  - strategic_failures >= 3 blocks force_strategic (without auto-decay)
  - operator_rejection_rate calculation in _spawn_strategic_review direction stats
  - Active-task guard: _spawn_design_doc_update and _spawn_strategic_review return False when blocked
  - _build_shipped_changes_manifest: cutoff, filtering, grouping, cap
  - Autonomy fields (lifetime_autonomous_prs_merged, lifetime_operator_actions, retirement_eligible)
    in directions_summary for the strategic agent
"""
import uuid
from datetime import datetime, timezone, timedelta

import pytest
from unittest.mock import AsyncMock
from sqlalchemy import select

import app.drift_detection  # noqa: F401 — registers DirectionDriftSnapshot in SQLModel.metadata for create_all
import app.shared_state as _shared_state
from app.models import Project, Task, TaskStatus, RECOMMENDED_MODE_META_KEY
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
    defaults = {
        "name": "Test Project",
        "user_id": "test-user-123",
        "autonomous_enabled": True,
        "directions": [],
        "vision": {},
    }
    defaults.update(kwargs)
    return Project(**defaults)


async def _seed_legacy_cycles(session, project: Project) -> None:
    """Audit W2: seed StrategicCycle rows from legacy ``vision._strategic_cycle_history``.

    Tests historically built cycle history as a JSONB list inline in vision.
    The W2 migration moved cycles to a real table; this helper backfills the
    table from the legacy shape so existing test fixtures still produce the
    same agent-context payload without rewriting every test.
    """
    from app.models import StrategicCycle as _SC
    from app.time_utils import _utcnow as _ut
    from datetime import datetime as _dt
    raw = (project.vision or {}).get("_strategic_cycle_history") or []
    if not raw:
        return
    def _parse_ts(v):
        if not v:
            return None
        try:
            d = _dt.fromisoformat(str(v).replace("Z", "+00:00"))
            return d.replace(tzinfo=None) if d.tzinfo else d
        except Exception:
            return None
    for entry in raw:
        if not isinstance(entry, dict) or not entry.get("cycle_id"):
            continue
        # Skip if a row already exists for this cycle id.
        existing = await session.get(_SC, entry["cycle_id"])
        if existing is not None:
            continue
        meta = {k: v for k, v in entry.items() if k not in {
            "cycle_id", "outcome", "phase1_task_id", "phase1_completed_at",
            "phase2_task_id", "phase2_completed_at", "phase1_started_at",
            "directions_created", "directions_retired", "tasks_spawned",
            "vision_updated", "cycle_reasoning", "summary_text", "started_at",
        }}
        session.add(_SC(
            id=entry["cycle_id"],
            project_id=project.id,
            outcome=entry.get("outcome") or "in_progress",
            phase1_task_id=None,  # legacy entries store str not UUID; skip FK
            phase1_started_at=_parse_ts(entry.get("started_at"))
                or _parse_ts(entry.get("phase1_started_at")) or _ut(),
            phase1_completed_at=_parse_ts(entry.get("phase1_completed_at")),
            phase2_started_at=_parse_ts(entry.get("phase2_started_at")),
            phase2_completed_at=_parse_ts(entry.get("phase2_completed_at")),
            directions_created=int(entry.get("directions_created") or 0),
            directions_retired=int(entry.get("directions_retired") or 0),
            tasks_spawned=int(entry.get("tasks_spawned") or 0),
            vision_updated=bool(entry.get("vision_updated", False)),
            cycle_reasoning=entry.get("cycle_reasoning"),
            summary_text=entry.get("summary_text"),
            cycle_metadata=meta,
        ))
    await session.commit()


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
    await _seed_legacy_cycles(db_session, project)

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
    await _seed_legacy_cycles(db_session, project)

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
    await _seed_legacy_cycles(db_session, project)

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
    await _seed_legacy_cycles(db_session, project)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    tasks = result.scalars().all()
    payload = tasks[0].payloads[0]
    d_stat = next(s for s in payload["directions_summary"] if s["id"] == "d-agg02")

    assert "operator_rejection_rate" not in d_stat
    assert "prs_rejected" not in d_stat


@pytest.mark.asyncio
async def test_retirement_eligibility_fields_in_direction_stats(db_session):
    """lifetime_autonomous_prs_merged, lifetime_operator_actions, and retirement_eligible are set on each direction stat."""
    project = _make_project(
        directions=[
            {
                "id": "d-ret-eligible",
                "status": "active",
                "text": "Eligible direction",
                "priority": "high",
                # 2 explicit autonomous + 2 legacy fallback = 4 from history; 7 from aggregate → total 11
                "research_history": [
                    {"autonomous_prs_merged": 2, "prs_merged": 2, "operator_action_count": 0},
                    {"prs_merged": 2},  # legacy: no autonomous_prs_merged, falls back to prs_merged
                ],
                "history_aggregate": {"total_autonomous_prs_merged": 7, "total_operator_actions": 0},
            },
            {
                "id": "d-ret-ineligible-ops",
                "status": "active",
                "text": "Ineligible: has operator actions",
                "priority": "medium",
                "research_history": [
                    {"autonomous_prs_merged": 15, "prs_merged": 15, "operator_action_count": 1},
                ],
                "history_aggregate": {"total_autonomous_prs_merged": 0, "total_operator_actions": 0},
            },
            {
                "id": "d-ret-ineligible-count",
                "status": "active",
                "text": "Ineligible: not enough autonomous merges",
                "priority": "low",
                "research_history": [
                    {"autonomous_prs_merged": 5, "prs_merged": 5, "operator_action_count": 0},
                ],
                "history_aggregate": {"total_autonomous_prs_merged": 4, "total_operator_actions": 0},
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

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    tasks = result.scalars().all()
    payload = tasks[0].payloads[0]
    by_id = {s["id"]: s for s in payload["directions_summary"]}

    eligible = by_id["d-ret-eligible"]
    assert eligible["lifetime_autonomous_prs_merged"] == 11  # 2 + 2 (fallback) + 7
    assert eligible["lifetime_operator_actions"] == 0
    assert eligible["retirement_eligible"] is True

    ineligible_ops = by_id["d-ret-ineligible-ops"]
    assert ineligible_ops["lifetime_operator_actions"] == 1
    assert ineligible_ops["retirement_eligible"] is False

    ineligible_count = by_id["d-ret-ineligible-count"]
    assert ineligible_count["lifetime_autonomous_prs_merged"] == 9  # 5 + 4
    assert ineligible_count["retirement_eligible"] is False


@pytest.mark.asyncio
async def test_cost_fields_present_when_history_has_merge_data(db_session):
    """cost_per_merged_pr and estimated_cost_usd appear when history has tasks_created + avg_time_to_merge_hours + merges."""
    from app.routers.admin_analytics import ON_DEMAND_RATE
    project = _make_project(
        directions=[
            {
                "id": "d-cost01",
                "status": "active",
                "text": "Direction with cost data",
                "priority": "medium",
                "research_history": [
                    {"prs_merged": 2, "tasks_created": 4, "avg_time_to_merge_hours": 1.5},
                    {"prs_merged": 1, "tasks_created": 2, "avg_time_to_merge_hours": 2.0},
                ],
            }
        ],
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    tasks = result.scalars().all()
    payload = tasks[0].payloads[0]
    d_stat = next(s for s in payload["directions_summary"] if s["id"] == "d-cost01")

    expected_cost = round((4 * 1.5 + 2 * 2.0) * 3600 * ON_DEMAND_RATE, 2)
    assert d_stat["estimated_cost_usd"] == expected_cost
    assert d_stat["cost_per_merged_pr"] == round(expected_cost / 3, 2)


@pytest.mark.asyncio
async def test_cost_fields_absent_without_history_data(db_session):
    """Cost fields are omitted when history lacks tasks_created or avg_time_to_merge_hours."""
    project = _make_project(
        directions=[
            {
                "id": "d-cost02",
                "status": "active",
                "text": "Direction with no cost data",
                "priority": "low",
                "research_history": [{"prs_merged": 2}],
            }
        ],
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    tasks = result.scalars().all()
    payload = tasks[0].payloads[0]
    d_stat = next(s for s in payload["directions_summary"] if s["id"] == "d-cost02")

    assert "estimated_cost_usd" not in d_stat
    assert "cost_per_merged_pr" not in d_stat


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
    await _seed_legacy_cycles(db_session, project)

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
    await _seed_legacy_cycles(db_session, project)

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
    await _seed_legacy_cycles(db_session, project)

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
    """_spawn_design_doc_update writes a new StrategicCycle row.

    Audit W2: cycle history is the StrategicCycle table now (was JSONB).
    """
    from app import strategic_cycle
    from app.models import StrategicCycle as _SC
    from sqlmodel import select as _select
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

    cycles = (await db_session.execute(
        _select(_SC).where(_SC.project_id == project.id)
    )).scalars().all()
    assert len(cycles) == 1
    cycle = cycles[0]
    assert cycle.outcome == strategic_cycle.OUTCOME_PHASE1_RUNNING
    assert cycle.phase1_task_id is not None
    assert cycle.id.startswith("sc-")


@pytest.mark.asyncio
async def test_spawn_design_doc_update_cycle_history_unbounded_in_table(db_session, mocker):
    """Audit W2: the 10-entry cap on cycle history was a JSONB-bloat guard.
    With cycles in a real table the cap is gone; ``recent_for_agent_context``
    applies a per-call limit at read time instead."""
    from app import strategic_cycle
    from app.models import StrategicCycle as _SC
    from sqlmodel import select as _select
    from app.time_utils import _utcnow
    _patch_spawning_deps(mocker)
    mocker.patch(
        "app.task_lifecycle._has_active_strategic_task",
        new=AsyncMock(return_value=False),
    )
    project = _make_project(vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    # Pre-populate with 10 historical cycles
    from datetime import timedelta
    for i in range(10):
        db_session.add(_SC(
            id=f"sc-{i:08x}", project_id=project.id,
            outcome="completed", phase1_started_at=_utcnow() - timedelta(days=i + 1),
        ))
    await db_session.commit()

    await _spawn_design_doc_update(project, db_session)

    all_cycles = (await db_session.execute(
        _select(_SC).where(_SC.project_id == project.id)
    )).scalars().all()
    assert len(all_cycles) == 11
    # The agent-context view caps at the helper's default (10).
    recent = await strategic_cycle.recent_for_agent_context(db_session, project.id)
    assert len(recent) == 10
    # Newest at the front; the just-spawned cycle should be first.
    assert recent[0]["outcome"] == strategic_cycle.OUTCOME_PHASE1_RUNNING


# ── Additional _spawn_strategic_review tests ──────────────────────────────────

@pytest.mark.asyncio
async def test_spawn_strategic_review_records_phase2_task_id(db_session, mocker):
    """When called with cycle_id, _spawn_strategic_review records phase2_task_id on the cycle row.

    Audit W2: cycle history is the StrategicCycle table now.
    """
    from app import strategic_cycle
    from app.models import StrategicCycle as _SC
    from app.time_utils import _utcnow
    _patch_spawning_deps(mocker)
    mocker.patch(
        "app.task_lifecycle._has_active_strategic_task",
        new=AsyncMock(return_value=False),
    )
    cycle_id = "sc-abc12345"
    project = _make_project(vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(_SC(
        id=cycle_id, project_id=project.id,
        outcome=strategic_cycle.OUTCOME_PHASE1_RUNNING,
        phase1_started_at=_utcnow(),
    ))
    await db_session.commit()

    await _spawn_strategic_review(project, db_session, cycle_id=cycle_id)

    cycle = await strategic_cycle.get(db_session, cycle_id)
    assert cycle is not None
    assert cycle.phase2_task_id is not None
    assert cycle.phase1_completed_at is not None
    assert cycle.outcome == strategic_cycle.OUTCOME_PHASE2_RUNNING


# ── recommended_mode in direction stats ──────────────────────────────────────

@pytest.mark.asyncio
async def test_recommended_mode_audit_present_in_direction_stats(db_session):
    """recommended_mode='audit' is surfaced in direction_stats payload."""
    project = _make_project(
        directions=[
            {
                "id": "d-mode01",
                "status": "active",
                "text": "Direction in audit mode",
                "priority": "high",
                "recommended_mode": "audit",
            }
        ],
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    tasks = result.scalars().all()
    payload = tasks[0].payloads[0]
    d_stat = next(s for s in payload["directions_summary"] if s["id"] == "d-mode01")

    assert d_stat.get("recommended_mode") == "audit", (
        "Expected recommended_mode='audit' in direction stats for audit-mode direction"
    )


@pytest.mark.asyncio
async def test_recommended_mode_fundamentals_present_in_direction_stats(db_session):
    """recommended_mode='fundamentals' is surfaced in direction_stats payload."""
    project = _make_project(
        directions=[
            {
                "id": "d-mode02",
                "status": "active",
                "text": "Direction in fundamentals mode",
                "priority": "medium",
                "recommended_mode": "fundamentals",
            }
        ],
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    tasks = result.scalars().all()
    payload = tasks[0].payloads[0]
    d_stat = next(s for s in payload["directions_summary"] if s["id"] == "d-mode02")

    assert d_stat.get("recommended_mode") == "fundamentals", (
        "Expected recommended_mode='fundamentals' in direction stats for fundamentals-mode direction"
    )


@pytest.mark.asyncio
async def test_recommended_mode_ship_absent_from_direction_stats(db_session):
    """recommended_mode='ship' and unset are both omitted — they represent default behavior."""
    project = _make_project(
        directions=[
            {
                "id": "d-mode03",
                "status": "active",
                "text": "Direction explicitly in ship mode",
                "priority": "low",
                "recommended_mode": "ship",
            },
            {
                "id": "d-mode04",
                "status": "active",
                "text": "Direction with no mode set",
                "priority": "low",
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

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    tasks = result.scalars().all()
    payload = tasks[0].payloads[0]
    d_stat_ship = next(s for s in payload["directions_summary"] if s["id"] == "d-mode03")
    d_stat_unset = next(s for s in payload["directions_summary"] if s["id"] == "d-mode04")

    assert "recommended_mode" not in d_stat_ship, (
        "recommended_mode='ship' should be omitted (default behavior)"
    )
    assert "recommended_mode" not in d_stat_unset, (
        "Unset recommended_mode should be omitted (default behavior)"
    )


@pytest.mark.asyncio
async def test_recommended_mode_pause_present_in_direction_stats(db_session):
    """recommended_mode='pause' is surfaced so the strategic agent can flag the direction."""
    project = _make_project(
        directions=[
            {
                "id": "d-mode05",
                "status": "active",
                "text": "Direction recommended for pause",
                "priority": "low",
                "recommended_mode": "pause",
            }
        ],
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    tasks = result.scalars().all()
    payload = tasks[0].payloads[0]
    d_stat = next(s for s in payload["directions_summary"] if s["id"] == "d-mode05")

    assert d_stat.get("recommended_mode") == "pause", (
        "Expected recommended_mode='pause' in direction stats for pause-mode direction"
    )


@pytest.mark.asyncio
async def test_strategic_review_prompt_suffix_includes_mode_hints(db_session):
    """Strategic review prompt suffix should spell out mode-specific guidance per direction."""
    project = _make_project(
        directions=[
            {
                "id": "d-mode-audit",
                "status": "active",
                "text": "Direction in audit mode",
                "priority": "high",
                "recommended_mode": "audit",
            },
            {
                "id": "d-mode-pause",
                "status": "active",
                "text": "Direction in pause mode",
                "priority": "low",
                "recommended_mode": "pause",
            },
            {
                "id": "d-mode-operator",
                "status": "active",
                "text": "Operator-pinned fundamentals direction",
                "priority": "medium",
                "recommended_mode": "fundamentals",
                RECOMMENDED_MODE_META_KEY: {"source": "operator", "set_at": "2026-04-01T00:00:00"},
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

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    tasks = result.scalars().all()
    payload = tasks[0].payloads[0]
    suffix = payload["prompt_suffix"]

    assert "Mode hints for this cycle:" in suffix
    assert "d-mode-audit" in suffix and "audit mode" in suffix
    assert "d-mode-pause" in suffix and "do not propose new feature work for" in suffix
    assert "d-mode-operator" in suffix and "fundamentals mode" in suffix


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
    await _seed_legacy_cycles(db_session, project)

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
    await _seed_legacy_cycles(db_session, project)

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
    await _seed_legacy_cycles(db_session, project)

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
    await _seed_legacy_cycles(db_session, project)

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
    await _seed_legacy_cycles(db_session, project)

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
    await _seed_legacy_cycles(db_session, project)

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
    await _seed_legacy_cycles(db_session, project)

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
    await _seed_legacy_cycles(db_session, project)

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


# ── predict_spawn_success: first-attempt rejection concentration ──────────────


class TestPredictSpawnSuccessFirstAttemptConcentration:
    """Signal 18 in predict_spawn_success."""

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

    def _direction(self):
        return {
            "id": "dir-abc",
            "research_history": [{"tasks_created": 2, "prs_merged": 1}],
        }

    def test_strong_concentration_penalty(self):
        from app.spawn_calibration import predict_spawn_success
        direction = self._direction()
        project_bad = _make_project(design_doc={
            "direction_first_attempt_feedback": {
                "dir-abc": {"correctness": 8, "style": 2, "total": 10},
            },
        })
        project_clean = _make_project(design_doc={})
        assert predict_spawn_success("small", direction, project=project_bad) < \
               predict_spawn_success("small", direction, project=project_clean)

    def test_moderate_concentration_penalty(self):
        from app.spawn_calibration import predict_spawn_success
        direction = self._direction()
        project_mod = _make_project(design_doc={
            "direction_first_attempt_feedback": {
                "dir-abc": {"correctness": 3, "style": 2, "scope": 2, "total": 7},
            },
        })
        project_strong = _make_project(design_doc={
            "direction_first_attempt_feedback": {
                "dir-abc": {"correctness": 8, "style": 2, "total": 10},
            },
        })
        # Strong signal (0.3) penalizes more than moderate (0.5)
        assert predict_spawn_success("small", direction, project=project_strong) < \
               predict_spawn_success("small", direction, project=project_mod)

    def test_below_threshold_omitted(self):
        from app.spawn_calibration import predict_spawn_success
        direction = self._direction()
        project_low = _make_project(design_doc={
            "direction_first_attempt_feedback": {
                "dir-abc": {"correctness": 2, "style": 1, "total": 3},
            },
        })
        project_none = _make_project(design_doc={})
        assert predict_spawn_success("small", direction, project=project_low) == \
               predict_spawn_success("small", direction, project=project_none)

    def test_spread_not_concentrated_omitted(self):
        from app.spawn_calibration import predict_spawn_success
        direction = self._direction()
        project_spread = _make_project(design_doc={
            "direction_first_attempt_feedback": {
                "dir-abc": {"a": 2, "b": 2, "c": 2, "d": 2, "total": 8},
            },
        })
        project_none = _make_project(design_doc={})
        assert predict_spawn_success("small", direction, project=project_spread) == \
               predict_spawn_success("small", direction, project=project_none)

    def test_missing_direction_id_omitted(self):
        from app.spawn_calibration import predict_spawn_success
        direction = {"research_history": [{"tasks_created": 2, "prs_merged": 1}]}
        project_bad = _make_project(design_doc={
            "direction_first_attempt_feedback": {
                "dir-abc": {"correctness": 8, "total": 8},
            },
        })
        project_none = _make_project(design_doc={})
        assert predict_spawn_success("small", direction, project=project_bad) == \
               predict_spawn_success("small", direction, project=project_none)


# ── anomaly_summary in _spawn_strategic_review payload ───────────────────────

@pytest.mark.asyncio
async def test_anomaly_summary_surfaces_high_occurrence_entries(db_session):
    """Entries with occurrence_count >= 3 are surfaced; low-occurrence entries are filtered."""
    project = _make_project(vision={"purpose": "test"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    proj_key = str(project.id)
    _shared_state.persistent_anomalies[proj_key] = {
        "CODE_ERROR": {
            "occurrence_count": 5, "severity": "high",
            "first_seen": "2026-04-01T00:00:00", "last_seen": "2026-04-10T00:00:00",
        },
        "LOW_SIGNAL": {
            "occurrence_count": 2, "severity": "low",
            "first_seen": "2026-04-01T00:00:00", "last_seen": "2026-04-05T00:00:00",
        },
        "CrossDirectionFingerprint_abc123": {
            "occurrence_count": 4, "severity": "high",
            "fingerprint": "abc123",
            "affected_directions": ["d-1", "d-2", "d-3"],
            "failure_count": 12,
            "first_seen": "2026-04-01T00:00:00", "last_seen": "2026-04-10T00:00:00",
        },
    }
    try:
        spawned = await _spawn_strategic_review(project, db_session)
        assert spawned is True
        result = await db_session.execute(select(Task).where(Task.project_id == project.id))
        payload = result.scalars().all()[0].payloads[0]

        assert "anomaly_summary" in payload
        summary = payload["anomaly_summary"]
        recurring_keys = [e["key"] for e in summary["recurring_anomalies"]]
        assert "CODE_ERROR" in recurring_keys
        assert "LOW_SIGNAL" not in recurring_keys

        cross = summary["cross_direction_fingerprints"]
        assert len(cross) == 1
        assert cross[0]["key"] == "CrossDirectionFingerprint_abc123"
        assert cross[0]["affected_directions"] == ["d-1", "d-2", "d-3"]
        assert cross[0]["fingerprint"] == "abc123"
        assert cross[0]["failure_count"] == 12
    finally:
        _shared_state.persistent_anomalies.pop(proj_key, None)


@pytest.mark.asyncio
async def test_anomaly_summary_absent_when_no_qualifying_entries(db_session):
    """When there are no occurrence_count>=3 entries, anomaly_summary is omitted."""
    project = _make_project(vision={"purpose": "test"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    proj_key = str(project.id)
    _shared_state.persistent_anomalies[proj_key] = {
        "ONE_OFF": {
            "occurrence_count": 1, "severity": "medium",
            "first_seen": "2026-04-01T00:00:00", "last_seen": "2026-04-01T00:00:00",
        },
    }
    try:
        await _spawn_strategic_review(project, db_session)
        result = await db_session.execute(select(Task).where(Task.project_id == project.id))
        payload = result.scalars().all()[0].payloads[0]
        assert "anomaly_summary" not in payload
    finally:
        _shared_state.persistent_anomalies.pop(proj_key, None)


# ── playbook_summary in _spawn_strategic_review payload ──────────────────────

@pytest.mark.asyncio
async def test_playbook_summary_includes_relevant_entries(db_session, mocker):
    """Strategy-relevant playbook entries are surfaced; irrelevant ones filtered."""
    _patch_spawning_deps(mocker)
    design_doc = {
        "playbook/auto-importerror-foo": {
            "content": "Repeated failure pattern (3x in 7d): importerror.",
            "source": "auto-generated",
            "updated_at": "2026-04-15T00:00:00",
            "confirmed_count": 0,
        },
        "playbook/review-test-coverage": {
            "content": "Review pattern from PR #100: test coverage issues.",
            "source": "review-extracted",
            "updated_at": "2026-04-18T00:00:00",
            "hit_count": 4,
        },
        "playbook/first-attempt-d12345-error-handling": {
            "content": "First-attempt rejection pattern (direction d12345).",
            "source": "first-attempt-pattern",
            "updated_at": "2026-04-10T00:00:00",
            "rejection_count": 5,
        },
        "playbook/manual-direction-note": {
            "content": "When working on directions, prefer auto-pause over delete.",
            "source": "manual",
            "updated_at": "2026-04-12T00:00:00",
        },
        "playbook/success-create-small": {
            "content": "First-attempt success pattern for create tasks.",
            "source": "success-pattern",
            "updated_at": "2026-04-19T00:00:00",
        },
    }
    project = _make_project(vision={"purpose": "test"}, design_doc=design_doc)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True
    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    payload = result.scalars().all()[0].payloads[0]

    assert "playbook_summary" in payload
    keys = [e["key"] for e in payload["playbook_summary"]["entries"]]
    assert "review-test-coverage" in keys
    assert "auto-importerror-foo" in keys
    assert "first-attempt-d12345-error-handling" in keys
    assert "manual-direction-note" in keys  # keyword "directions" + "auto-pause"
    assert "success-create-small" not in keys
    # Sorted newest-first
    assert keys[0] == "review-test-coverage"


@pytest.mark.asyncio
async def test_playbook_summary_absent_when_no_relevant_entries(db_session, mocker):
    """Without strategy-relevant playbook entries, the key is omitted."""
    _patch_spawning_deps(mocker)
    design_doc = {
        "playbook/success-create-small": {
            "content": "First-attempt success pattern for create tasks.",
            "source": "success-pattern",
            "updated_at": "2026-04-19T00:00:00",
        },
    }
    project = _make_project(vision={"purpose": "test"}, design_doc=design_doc)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    await _spawn_strategic_review(project, db_session)
    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    payload = result.scalars().all()[0].payloads[0]
    assert "playbook_summary" not in payload


@pytest.mark.asyncio
async def test_playbook_summary_caps_at_ten_entries(db_session, mocker):
    """Only the 10 most recent qualifying entries are included."""
    _patch_spawning_deps(mocker)
    design_doc = {
        f"playbook/auto-pattern-{i:02d}": {
            "content": f"Repeated failure pattern {i}.",
            "source": "auto-generated",
            "updated_at": f"2026-04-{i+1:02d}T00:00:00",
        }
        for i in range(15)
    }
    project = _make_project(vision={"purpose": "test"}, design_doc=design_doc)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    await _spawn_strategic_review(project, db_session)
    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    payload = result.scalars().all()[0].payloads[0]
    entries = payload["playbook_summary"]["entries"]
    assert len(entries) == 10
    # Newest-first ordering — entry 14 has the latest date
    assert entries[0]["key"] == "auto-pattern-14"


# ---------------------------------------------------------------------------
# _agent_view_design_doc — strips agent-unused + internal keys
# ---------------------------------------------------------------------------

def test_agent_view_design_doc_strips_playbook_archive():
    """playbook_archive is operator-only state — it must never ship in agent
    payloads. Unbounded growth of this field was the trigger for the silent
    pre-claim death investigation (task 595c235d, 351KB archive bloat)."""
    from app.strategic_spawning import _agent_view_design_doc
    doc = {
        "summary": "keep",
        "systems/sys-a": {"id": "sys-a"},
        "playbook_archive": [{"_archived_key": "playbook/foo"}] * 500,
    }
    view = _agent_view_design_doc(doc)
    assert "playbook_archive" not in view
    assert view["summary"] == "keep"
    assert view["systems/sys-a"] == {"id": "sys-a"}


def test_agent_view_design_doc_strips_internal_keys():
    """Keys prefixed with ``_`` are internal/operator state; the agent's
    design-doc handler already filters them client-side, so stripping them
    server-side is both safe and saves bytes on the wire."""
    from app.strategic_spawning import _agent_view_design_doc
    doc = {
        "summary": "keep",
        "_meta": {"last_updated": "x"},
        "_strategic_cycle_history": [1, 2, 3],
    }
    view = _agent_view_design_doc(doc)
    assert "_meta" not in view
    assert "_strategic_cycle_history" not in view
    assert view["summary"] == "keep"


def test_agent_view_design_doc_handles_empty_input():
    from app.strategic_spawning import _agent_view_design_doc
    assert _agent_view_design_doc(None) == {}
    assert _agent_view_design_doc({}) == {}


@pytest.mark.asyncio
async def test_design_doc_update_payload_omits_playbook_archive(db_session):
    """Integration: _spawn_design_doc_update must not ship playbook_archive."""
    project = _make_project(
        id=uuid.uuid4(),
        name="archive-strip-test",
        github_owner="test", github_repo="repo",
        autonomous_enabled=True,
        vision={"purpose": "test"},
        design_doc={
            "summary": "ok",
            "playbook_archive": [{"_archived_key": f"playbook/e{i}"} for i in range(100)],
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _spawn_design_doc_update(project, db_session)
    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    tasks = result.scalars().all()
    assert len(tasks) == 1
    payload = tasks[0].payloads[0]
    dd = payload["design_doc"]
    assert "playbook_archive" not in dd
    assert dd["summary"] == "ok"


@pytest.mark.asyncio
async def test_strategic_review_payload_includes_calibration_factor(db_session):
    """prediction_calibration_factor and note are always present in strategic payload."""
    project = _make_project(
        vision={"purpose": "test", "_strategic_cycle_history": []},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    tasks = result.scalars().all()
    payload = tasks[0].payloads[0]
    assert "prediction_calibration_factor" in payload
    assert "prediction_calibration_note" in payload
    assert isinstance(payload["prediction_calibration_factor"], float)
    assert isinstance(payload["prediction_calibration_note"], str)


@pytest.mark.asyncio
async def test_strategic_review_calibration_factor_reflects_history(db_session):
    """Factor < 1.0 when recent cycles consistently over-predicted merges."""
    directions = [
        {"id": f"d{i}", "status": "active", "text": f"direction {i}", "priority": "medium",
         "research_history": [{"prs_merged": 1}]}
        for i in range(3)
    ]
    cycles = [
        {
            "cycle_id": f"sc-{i}",
            "outcome": "completed",
            "created_direction_ids": [f"d{i}"],
            "created_predictions": {f"d{i}": {"similar_past_merges": 10}},
        }
        for i in range(3)
    ]
    project = _make_project(
        directions=directions,
        vision={"purpose": "test", "_strategic_cycle_history": cycles},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    tasks = result.scalars().all()
    payload = tasks[0].payloads[0]
    factor = payload["prediction_calibration_factor"]
    assert factor < 1.0, f"Expected factor < 1.0 for over-prediction history, got {factor}"
    assert "optimistic" in payload["prediction_calibration_note"]


# ── Mode-aware additions: system_maturity + drift_signals ─────────────────────

@pytest.mark.asyncio
async def test_strategic_review_includes_system_maturity_when_rows_exist(db_session):
    """system_maturity payload field is present and non-empty when SystemMaturity
    rows exist; absent when no rows."""
    from app.models import SystemMaturity
    from app.time_utils import _utcnow

    project = _make_project(
        directions=[
            {"id": "d-sm01", "status": "active", "text": "x", "priority": "medium",
             "system_id": "sys-A"},
        ],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(SystemMaturity(
        project_id=project.id,
        system_id="sys-A",
        score=72,
        components={"test_coverage": 0.5},
        recommended_mode="ship",
        top_issues=[{"description": "thin tests"}],
        last_audited_at=_utcnow(),
    ))
    await db_session.commit()

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    tasks = result.scalars().all()
    payload = tasks[0].payloads[0]
    assert "system_maturity" in payload
    assert isinstance(payload["system_maturity"], list)
    assert any(sm["system_id"] == "sys-A" for sm in payload["system_maturity"])
    sm_row = next(sm for sm in payload["system_maturity"] if sm["system_id"] == "sys-A")
    assert sm_row["score"] == 72
    assert sm_row["recommended_mode"] == "ship"


@pytest.mark.asyncio
async def test_strategic_review_omits_system_maturity_when_no_rows(db_session):
    """Without SystemMaturity rows, the system_maturity field is omitted from the payload."""
    project = _make_project(
        directions=[
            {"id": "d-sm02", "status": "active", "text": "x", "priority": "medium",
             "system_id": "sys-B"},
        ],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    tasks = result.scalars().all()
    payload = tasks[0].payloads[0]
    assert "system_maturity" not in payload, (
        f"system_maturity should not appear when no rows; payload keys: {sorted(payload.keys())}"
    )


@pytest.mark.asyncio
async def test_strategic_review_includes_drift_signals_when_drift_present(db_session):
    """drift_signals payload field is present when at least one direction has
    flagged drift; the field shape includes by_system and generated_at."""
    from app.models import Task as _Task, TaskStatus as _TaskStatus

    project = _make_project(
        directions=[
            {"id": "d-drift01", "status": "active", "text": "x", "priority": "medium",
             "system_id": "sys-C"},
        ],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Create 5 completed claim-tasks with no PR artifacts → stalled_despite_churn
    for i in range(5):
        db_session.add(_Task(
            project_id=project.id,
            title=f"claim {i}",
            description="",
            status=_TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
            payloads=[{"type": "create", "prompt": "x", "current_direction_id": "d-drift01"}],
            context={"direction_id": "d-drift01"},
            plan_output={"summary": "shipped"},
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
        ))
    await db_session.commit()

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    tasks = [t for t in result.scalars().all() if t.first_payload.get("type") == "strategic_review"]
    assert tasks
    payload = tasks[0].payloads[0]
    assert "drift_signals" in payload
    drift = payload["drift_signals"]
    assert "by_system" in drift
    assert "generated_at" in drift


@pytest.mark.asyncio
async def test_drift_signals_persist_snapshot_and_include_consecutive_cycles(db_session):
    """_spawn_strategic_review persists a drift snapshot and enriches the payload
    with consecutive_flagged_cycles when prior flagged snapshots exist."""
    from datetime import timedelta
    from app.drift_detection import DirectionDriftSnapshot
    from app.time_utils import _utcnow

    project = _make_project(
        directions=[
            {"id": "d-trend01", "status": "active", "text": "x", "priority": "medium",
             "system_id": "sys-T"},
        ],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Seed two prior flagged snapshots to simulate previous cycles.
    now = _utcnow()
    for i in range(2, 0, -1):
        db_session.add(DirectionDriftSnapshot(
            project_id=project.id,
            direction_id="d-trend01",
            snapshot_at=now - timedelta(hours=i * 6),
            claimed_changes_count=5,
            actually_merged_prs=0,
            claim_shipped_consistency=0.0,
            over_claim_flag=False,
            stalled_despite_churn_flag=True,
        ))
    await db_session.commit()

    # Create 5 completed claim-tasks with no PR artifacts → stalled_despite_churn
    for i in range(5):
        db_session.add(Task(
            project_id=project.id,
            title=f"trend claim {i}",
            description="",
            status=TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
            payloads=[{"type": "create", "prompt": "x", "current_direction_id": "d-trend01"}],
            context={"direction_id": "d-trend01"},
            plan_output={"summary": "shipped"},
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
        ))
    await db_session.commit()

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    sr_tasks = [t for t in result.scalars().all() if t.first_payload.get("type") == "strategic_review"]
    assert sr_tasks, "No strategic_review task spawned"
    payload = sr_tasks[0].payloads[0]
    assert "drift_signals" in payload, f"drift_signals missing; keys: {sorted(payload.keys())}"
    drift = payload["drift_signals"]
    assert "d-trend01" in drift.get("by_direction", {}), (
        f"d-trend01 not in by_direction: {list(drift.get('by_direction', {}).keys())}"
    )
    sig = drift["by_direction"]["d-trend01"]
    # 2 prior flagged + 1 current (written by persist_drift_snapshot) = 3 consecutive
    assert "consecutive_flagged_cycles" in sig, f"consecutive_flagged_cycles missing from sig: {sig}"
    assert sig["consecutive_flagged_cycles"] == 3

    # Verify snapshot was persisted to DB (one new row for the current cycle).
    from sqlmodel import select as _select
    snap_result = await db_session.execute(
        _select(DirectionDriftSnapshot).where(
            DirectionDriftSnapshot.project_id == project.id,
            DirectionDriftSnapshot.direction_id == "d-trend01",
        )
    )
    snaps = snap_result.scalars().all()
    assert len(snaps) == 3, f"Expected 3 snapshots (2 seeded + 1 persisted), got {len(snaps)}"


@pytest.mark.asyncio
async def test_strategic_review_payload_documents_recommended_mode(db_session):
    """The optional `recommended_mode` field is part of the StrategicReviewPayload
    schema (validated via validate_internal_payload). Absence in a typical
    payload should not cause validation failures."""
    from app.payload_schemas import StrategicReviewPayload

    fields = StrategicReviewPayload.model_fields
    assert "recommended_mode" in fields, (
        "recommended_mode should be part of StrategicReviewPayload schema"
    )
    # And: the existing strategic_review payload still validates.
    project = _make_project(
        directions=[
            {"id": "d-doc01", "status": "active", "text": "x", "priority": "medium"},
        ],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True


# ---------------------------------------------------------------------------
# _build_failure_feedback_mitigation unit tests
# ---------------------------------------------------------------------------

def test_failure_feedback_downscopes_large_to_medium():
    """3 first-attempt failures in a category downscopes large→medium."""
    from app.strategic_spawning import _build_failure_feedback_mitigation
    direction = {"id": "d-abc123"}
    design_doc = {
        "direction_first_attempt_feedback": {
            "d-abc123": {"code_defect": 3, "total": 3, "last_updated": "2026-04-20T00:00:00"},
        }
    }
    scope, note, evidence = _build_failure_feedback_mitigation(direction, "large", "create", design_doc)
    assert scope == "medium"
    assert note is not None and "code defect" in note
    assert evidence["failure_downscope"]["count"] == 3
    assert evidence["failure_downscope"]["from"] == "large"
    assert evidence["failure_downscope"]["to"] == "medium"


def test_failure_feedback_downscopes_medium_to_small():
    """3+ first-attempt failures downscopes medium→small and names the category."""
    from app.strategic_spawning import _build_failure_feedback_mitigation
    direction = {"id": "d-def456"}
    design_doc = {
        "direction_first_attempt_feedback": {
            "d-def456": {"testing": 5, "scope_related": 1, "total": 6, "last_updated": "2026-04-20T00:00:00"},
        }
    }
    scope, note, evidence = _build_failure_feedback_mitigation(direction, "medium", "create", design_doc)
    assert scope == "small"
    assert note is not None and "testing" in note
    assert evidence["failure_downscope"]["category"] == "testing"


def test_failure_feedback_no_downscope_below_threshold():
    """2 failures (< threshold) leaves scope and note unchanged."""
    from app.strategic_spawning import _build_failure_feedback_mitigation
    direction = {"id": "d-ghi789"}
    design_doc = {
        "direction_first_attempt_feedback": {
            "d-ghi789": {"code_defect": 2, "total": 2, "last_updated": "2026-04-20T00:00:00"},
        }
    }
    scope, note, evidence = _build_failure_feedback_mitigation(direction, "medium", "create", design_doc)
    assert scope == "medium"
    assert note is None
    assert "failure_downscope" not in evidence


def test_success_pattern_biases_medium_to_small():
    """playbook/success-create-small with 3+ successes biases medium→small."""
    from app.strategic_spawning import _build_failure_feedback_mitigation
    direction = {"id": "d-jkl012"}
    design_doc = {
        "playbook/success-create-small": {"source": "success-pattern", "success_count": 4},
    }
    scope, note, evidence = _build_failure_feedback_mitigation(direction, "medium", "create", design_doc)
    assert scope == "small"
    assert evidence["success_bias"]["success_count"] == 4
    assert evidence["success_bias"]["to"] == "small"


def test_success_pattern_insufficient_count_no_bias():
    """2 successes (< threshold) leaves scope unchanged."""
    from app.strategic_spawning import _build_failure_feedback_mitigation
    direction = {"id": "d-mno345"}
    design_doc = {
        "playbook/success-create-small": {"source": "success-pattern", "success_count": 2},
    }
    scope, note, evidence = _build_failure_feedback_mitigation(direction, "medium", "create", design_doc)
    assert scope == "medium"
    assert "success_bias" not in evidence


def test_no_data_returns_proposed_scope_unchanged():
    """Empty design_doc leaves scope and note unchanged."""
    from app.strategic_spawning import _build_failure_feedback_mitigation
    scope, note, evidence = _build_failure_feedback_mitigation({"id": "d-xyz"}, "medium", "create", {})
    assert scope == "medium"
    assert note is None
    assert evidence["proposed_scope"] == "medium"
    assert evidence["final_scope"] == "medium"


# ── pack_under_budget integration ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_strategic_review_packer_meta_present_on_small_project(db_session):
    """Small project: packer ships everything cleanly, meta lists no trims."""
    project = _make_project(
        directions=[
            {"id": "d-small1", "status": "active", "text": "Small direction",
             "priority": "medium", "research_history": [{"prs_merged": 1}]}
        ],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True
    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    payload = result.scalars().all()[0].payloads[0]

    assert "_packer_meta" in payload, "Packer must always attach _packer_meta"
    meta = payload["_packer_meta"]
    assert meta["trimmed"] == [], f"Small project should have no trims: {meta['trimmed']}"
    assert meta["dropped"] == [], f"Small project should have no drops: {meta['dropped']}"
    assert meta["budget_used"] > 0
    assert meta["budget_used"] < meta["budget_total"]


@pytest.mark.asyncio
async def test_strategic_review_packer_keeps_payload_under_cap_on_bloated_project(db_session):
    """Bloated project (50 directions, heavy history): packer must keep payload
    under the 256 KB validate_internal_payload hard cap and surface trims/drops
    so the agent can fetch full state via the ./fetch tool."""
    import json as _json
    from app.payload_schemas import _MAX_PAYLOAD_BYTES

    # Synthesize 50 directions, each with bulky research_history
    directions = [
        {
            "id": f"d-bloat{i:02d}",
            "status": "active",
            "text": f"Direction {i} " + ("filler text " * 30),
            "priority": "medium",
            "research_history": [
                {
                    "prs_merged": 1,
                    "prs_rejected": 0,
                    "tasks_failed": 0,
                    "tasks_created": 1,
                    "key_finding_descriptions": [f"finding {j} " * 20 for j in range(5)],
                    "failed_task_details": [],
                    "timestamp": "2026-04-01T00:00:00Z",
                }
                for _ in range(8)
            ],
            "research_context": "context narrative " * 200,
        }
        for i in range(50)
    ]
    project = _make_project(
        directions=directions,
        vision={"purpose": "bloated project test"},
        problem_model={
            "intent": {"purpose": "test", "current_focus": "x"},
            "gaps": [
                {"id": f"g{i}", "name": f"gap {i}", "description": "x" * 500,
                 "history": [f"h{j}" * 10 for j in range(20)],
                 "past_moves": [f"m{j}" * 10 for j in range(20)],
                 "evidence": [f"e{j}" * 10 for j in range(20)]}
                for i in range(20)
            ],
            "archived_gaps": [
                {"id": f"ag{i}", "name": "archived"} for i in range(40)
            ],
            "friction": [],
            "capabilities": [],
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True
    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    payload = result.scalars().all()[0].payloads[0]

    # Hard requirement: must fit under the 256KB cap that crashed autonomy before
    serialized_size = len(_json.dumps(payload, default=str).encode())
    assert serialized_size < _MAX_PAYLOAD_BYTES, (
        f"Packer failed to keep payload under cap: {serialized_size}B >= {_MAX_PAYLOAD_BYTES}B"
    )

    meta = payload["_packer_meta"]
    # On a 50-direction bloated project, directions_summary should have been
    # trimmed (cap is 12). archived_gaps drops via _agent_view_problem_model
    # before pack, so they don't appear in meta — the trim happened upstream.
    assert meta["budget_used"] <= meta["budget_total"]
    assert len(payload["directions_summary"]) <= 12, (
        f"Expected ≤12 directions after trim, got {len(payload['directions_summary'])}"
    )
    # The trim must be visible to the agent so it knows to fetch the rest.
    trimmed_keys = {entry["key"] for entry in meta["trimmed"]}
    assert "directions_summary" in trimmed_keys, (
        f"Trimmed list must include directions_summary; meta={meta}"
    )


@pytest.mark.asyncio
async def test_drift_signals_survives_budget_pressure(db_session):
    """drift_signals must not be dropped when low-priority fields are evicted.

    Constructs a bloated project that forces the packer to drop low-priority
    fields, then asserts drift_signals (now medium priority) is still present
    while at least one low-priority field is absent from the payload.
    """
    import json as _json
    from app.payload_schemas import _MAX_PAYLOAD_BYTES

    # 50 bulky directions to force budget pressure
    directions = [
        {
            "id": f"d-dp{i:02d}",
            "status": "active",
            "text": f"Direction {i} " + ("pressure filler " * 40),
            "priority": "medium",
            "system_id": "sys-dp",
            "research_history": [
                {
                    "prs_merged": 1,
                    "prs_rejected": 0,
                    "tasks_failed": 0,
                    "tasks_created": 1,
                    "key_finding_descriptions": [f"finding {j} " * 20 for j in range(5)],
                    "failed_task_details": [],
                    "timestamp": "2026-04-01T00:00:00Z",
                }
                for _ in range(8)
            ],
            "research_context": "context narrative " * 200,
        }
        for i in range(50)
    ]
    project = _make_project(
        directions=directions,
        vision={"purpose": "budget pressure test"},
        problem_model={
            "intent": {"purpose": "test", "current_focus": "x"},
            "gaps": [
                {"id": f"g{i}", "name": f"gap {i}", "description": "x" * 500,
                 "history": [f"h{j}" * 10 for j in range(20)],
                 "past_moves": [f"m{j}" * 10 for j in range(20)],
                 "evidence": [f"e{j}" * 10 for j in range(20)]}
                for i in range(20)
            ],
            "archived_gaps": [{"id": f"ag{i}", "name": "archived"} for i in range(40)],
            "friction": [],
            "capabilities": [],
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    # Add stalled tasks so drift_signals is non-empty and included in the payload
    for i in range(5):
        db_session.add(Task(
            project_id=project.id,
            title=f"drift claim {i}",
            description="",
            status=TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
            payloads=[{"type": "create", "prompt": "x", "current_direction_id": "d-dp00"}],
            context={"direction_id": "d-dp00"},
            plan_output={"summary": "shipped"},
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
        ))
    await db_session.commit()

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    tasks = [t for t in result.scalars().all() if t.first_payload.get("type") == "strategic_review"]
    assert tasks
    payload = tasks[0].payloads[0]

    # Payload must stay under hard cap
    serialized_size = len(_json.dumps(payload, default=str).encode())
    assert serialized_size < _MAX_PAYLOAD_BYTES

    meta = payload["_packer_meta"]
    dropped_keys = {entry["key"] for entry in meta.get("dropped", [])}

    # drift_signals (medium priority) must survive budget pressure
    assert "drift_signals" in payload, (
        f"drift_signals was dropped under budget pressure; dropped={dropped_keys}"
    )

    # Confirm budget pressure existed (something was trimmed or dropped)
    trimmed_keys = {entry["key"] for entry in meta.get("trimmed", [])}
    assert dropped_keys or trimmed_keys, (
        f"Expected budget pressure (trims or drops); meta={meta}"
    )

    # drift_signals must not be among the dropped fields
    assert "drift_signals" not in dropped_keys, (
        f"drift_signals should not be dropped; dropped={dropped_keys}"
    )
