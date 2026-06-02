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
from unittest.mock import AsyncMock, patch
from sqlalchemy import select

import app.drift_detection  # noqa: F401 — registers DirectionDriftSnapshot in SQLModel.metadata for create_all
import app.shared_state as _shared_state
from app.models import (
    Project,
    Task,
    TaskStatus,
    ProjectMessage,
    RECOMMENDED_MODE_META_KEY,
)
from app.autonomous_research import _spawn_research_task
from app.strategic_spawning import (
    _spawn_design_doc_update,
    _spawn_strategic_review,
    _build_shipped_changes_manifest,
    _build_recent_interventions,
)
from app.max_plan_pool import _max_plan_pools, _account_by_id


# ── Account pool fixture (required by _spawn_research_task) ───────────────────


@pytest.fixture(autouse=True)
def _ensure_test_account_pool():
    """Ensure _spawn_research_task finds an available account for the test user."""
    uid = "test-user-123"
    entry = {
        "id": "test-acct-ss",
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
    _account_by_id["test-acct-ss"] = entry
    yield
    _max_plan_pools.get(uid, [])[:] = [
        a for a in _max_plan_pools.get(uid, []) if a["id"] != "test-acct-ss"
    ]
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
        "integration_branch": "automain",
        "promotion_target": "main",
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
        meta = {
            k: v
            for k, v in entry.items()
            if k
            not in {
                "cycle_id",
                "outcome",
                "phase1_task_id",
                "phase1_completed_at",
                "phase2_task_id",
                "phase2_completed_at",
                "phase1_started_at",
                "directions_created",
                "directions_retired",
                "tasks_spawned",
                "vision_updated",
                "cycle_reasoning",
                "summary_text",
                "started_at",
            }
        }
        session.add(
            _SC(
                id=entry["cycle_id"],
                project_id=project.id,
                outcome=entry.get("outcome") or "in_progress",
                phase1_task_id=None,  # legacy entries store str not UUID; skip FK
                phase1_started_at=_parse_ts(entry.get("started_at"))
                or _parse_ts(entry.get("phase1_started_at"))
                or _ut(),
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
            )
        )
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


@pytest.fixture
def _lts_split_active(monkeypatch):
    """Activate the LTS split for version-lifecycle tests.

    The strategic version-phase block is gated on the integration branch being
    a real trunk distinct from the promotion target. Override the per-project
    resolvers (the global runtime_config bridge was removed) so tests using
    `_make_project()` without an explicit `integration_branch` column see a
    distinct trunk/target pair.
    """
    monkeypatch.setattr(
        "app.strategic_spawning.get_project_integration_branch",
        lambda _p: "automain",
    )
    monkeypatch.setattr(
        "app.strategic_spawning.get_project_promotion_target",
        lambda _p: "main",
    )
    yield


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
            {
                "id": "d-001",
                "status": "active",
                "title": "Test direction",
                "possibly_saturated": False,
            },
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
    assert "operator_rejection_rate" in d_stat, (
        "Expected operator_rejection_rate in direction stat"
    )
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
async def test_success_evidence_present_in_direction_stats(db_session):
    """success_evidence/retirement_condition from context reach the direction stat entry, capped."""
    long_evidence = "E" * 700
    project = _make_project(
        directions=[
            {
                "id": "d-se01",
                "status": "active",
                "text": "Direction with stated outcome",
                "priority": "medium",
                "context": {
                    "success_evidence": long_evidence,
                    "retirement_condition": "  retire when X happens  ",
                },
                "research_history": [{"prs_merged": 1, "prs_rejected": 0}],
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
    d_stat = next(s for s in payload["directions_summary"] if s["id"] == "d-se01")

    assert d_stat["success_evidence"] == "E" * 500, (
        "success_evidence should be capped at 500"
    )
    assert d_stat["retirement_condition"] == "retire when X happens", (
        "retirement_condition should be stripped and surfaced"
    )


@pytest.mark.asyncio
async def test_success_evidence_absent_when_not_in_context(db_session):
    """success_evidence/retirement_condition are omitted when not set on context."""
    project = _make_project(
        directions=[
            {
                "id": "d-se02",
                "status": "active",
                "text": "Direction without stated outcome",
                "priority": "medium",
                "research_history": [{"prs_merged": 1, "prs_rejected": 0}],
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
    d_stat = next(s for s in payload["directions_summary"] if s["id"] == "d-se02")

    assert "success_evidence" not in d_stat
    assert "retirement_condition" not in d_stat


@pytest.mark.asyncio
async def test_cumulative_confirmed_finding_count_and_trend_in_direction_stats(
    db_session,
):
    """confirmed_finding_count and confirmed_finding_trend from outcome_progress_history reach direction stats."""
    project = _make_project(
        directions=[
            {
                "id": "d-cf01",
                "status": "active",
                "text": "Direction with findings trend",
                "priority": "medium",
                "research_history": [{"prs_merged": 1, "prs_rejected": 0}],
                "outcome_progress_history": [
                    {"progress": 1},
                    {"progress": 2},
                    {"progress": 3},
                    {"progress": 5},
                    {"progress": 8},
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
    d_stat = next(s for s in payload["directions_summary"] if s["id"] == "d-cf01")

    assert "confirmed_finding_count" in d_stat
    assert d_stat["confirmed_finding_count"] == 8
    assert "confirmed_finding_trend" in d_stat
    assert d_stat["confirmed_finding_trend"] == "improving"


@pytest.mark.asyncio
async def test_outcome_scaffolding_flag_surfaces_in_direction_stats(db_session):
    """outcome_scaffolding_flagged and _evidence appear in stat_entry for flagged direction, absent for unflagged."""
    project = _make_project(
        directions=[
            {
                "id": "d-scaff-flagged",
                "status": "active",
                "text": "Flagged scaffolding direction",
                "priority": "medium",
                "research_history": [{"prs_merged": 1, "prs_rejected": 0}],
                "outcome_scaffolding_flagged": True,
                "outcome_scaffolding_evidence": {"reason": "no progress", "cycles": 5},
            },
            {
                "id": "d-scaff-clean",
                "status": "active",
                "text": "Clean direction",
                "priority": "medium",
                "research_history": [{"prs_merged": 1, "prs_rejected": 0}],
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

    flagged = by_id["d-scaff-flagged"]
    assert flagged.get("outcome_scaffolding_flagged") is True
    assert flagged.get("outcome_scaffolding_evidence") == {
        "reason": "no progress",
        "cycles": 5,
    }

    clean = by_id["d-scaff-clean"]
    assert "outcome_scaffolding_flagged" not in clean
    assert "outcome_scaffolding_evidence" not in clean


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
                "history_aggregate": {
                    "total_prs_merged": 3,
                    "total_prs_rejected": 0,
                    "total_cycles": 3,
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
                    {
                        "autonomous_prs_merged": 2,
                        "prs_merged": 2,
                        "operator_action_count": 0,
                    },
                    {
                        "prs_merged": 2
                    },  # legacy: no autonomous_prs_merged, falls back to prs_merged
                ],
                "history_aggregate": {
                    "total_autonomous_prs_merged": 7,
                    "total_operator_actions": 0,
                },
            },
            {
                "id": "d-ret-ineligible-ops",
                "status": "active",
                "text": "Ineligible: has operator actions",
                "priority": "medium",
                "research_history": [
                    {
                        "autonomous_prs_merged": 15,
                        "prs_merged": 15,
                        "operator_action_count": 1,
                    },
                ],
                "history_aggregate": {
                    "total_autonomous_prs_merged": 0,
                    "total_operator_actions": 0,
                },
            },
            {
                "id": "d-ret-ineligible-count",
                "status": "active",
                "text": "Ineligible: not enough autonomous merges",
                "priority": "low",
                "research_history": [
                    {
                        "autonomous_prs_merged": 5,
                        "prs_merged": 5,
                        "operator_action_count": 0,
                    },
                ],
                "history_aggregate": {
                    "total_autonomous_prs_merged": 4,
                    "total_operator_actions": 0,
                },
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
                    {
                        "prs_merged": 2,
                        "tasks_created": 4,
                        "avg_time_to_merge_hours": 1.5,
                    },
                    {
                        "prs_merged": 1,
                        "tasks_created": 2,
                        "avg_time_to_merge_hours": 2.0,
                    },
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
async def test_cost_fields_use_recorded_spend_over_proxy(db_session):
    """When estimated_cost_usd is present in a history entry, it is used instead of the proxy."""
    project = _make_project(
        directions=[
            {
                "id": "d-cost-recorded",
                "status": "active",
                "text": "Direction with recorded cost",
                "priority": "medium",
                "research_history": [
                    {
                        "prs_merged": 2,
                        "tasks_created": 999,
                        "avg_time_to_merge_hours": 999.0,
                        "estimated_cost_usd": 6.50,
                    },
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
    d_stat = next(s for s in payload["directions_summary"] if s["id"] == "d-cost-recorded")

    assert d_stat["estimated_cost_usd"] == 6.50
    assert d_stat["cost_per_merged_pr"] == round(6.50 / 2, 2)


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
async def test_spawn_design_doc_update_skips_when_strategic_task_active(
    db_session, mocker
):
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
async def test_spawn_strategic_review_skips_when_strategic_task_active(
    db_session, mocker
):
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
            {
                "id": "d-sr01",
                "status": "active",
                "text": "Some direction",
                "priority": "medium",
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
    assert len(tasks) == 1
    assert tasks[0].payloads[0]["type"] == "strategic_review"
    assert tasks[0].status == TaskStatus.QUEUED


@pytest.mark.asyncio
async def test_spawn_strategic_review_enable_self_improvement_decoupled_from_is_self_repo(
    db_session,
):
    """enable_self_improvement in the strategic payload is driven by build_config, not is_self_repo.

    is_self_repo=True without the explicit flag → False.
    build_config={"enable_self_improvement": True} regardless of is_self_repo → True.
    """
    # Self-repo without explicit flag → False
    project_self_no_flag = _make_project(
        is_self_repo=True,
        build_config={"some_other_key": "value"},
        vision={"purpose": "test purpose"},
    )
    db_session.add(project_self_no_flag)
    await db_session.commit()
    await db_session.refresh(project_self_no_flag)
    await _seed_legacy_cycles(db_session, project_self_no_flag)

    spawned = await _spawn_strategic_review(project_self_no_flag, db_session)
    assert spawned is True
    result = await db_session.execute(
        select(Task).where(Task.project_id == project_self_no_flag.id)
    )
    task_self = result.scalars().one()
    assert task_self.payloads[0]["enable_self_improvement"] is False

    # Non-self-repo with explicit flag → True
    project_flag = _make_project(
        is_self_repo=False,
        build_config={"enable_self_improvement": True},
        vision={"purpose": "test purpose"},
    )
    db_session.add(project_flag)
    await db_session.commit()
    await db_session.refresh(project_flag)
    await _seed_legacy_cycles(db_session, project_flag)

    spawned2 = await _spawn_strategic_review(project_flag, db_session)
    assert spawned2 is True
    result2 = await db_session.execute(
        select(Task).where(Task.project_id == project_flag.id)
    )
    task_flag = result2.scalars().one()
    assert task_flag.payloads[0]["enable_self_improvement"] is True


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

    cycles = (
        (await db_session.execute(_select(_SC).where(_SC.project_id == project.id)))
        .scalars()
        .all()
    )
    assert len(cycles) == 1
    cycle = cycles[0]
    assert cycle.outcome == strategic_cycle.OUTCOME_PHASE1_RUNNING
    assert cycle.phase1_task_id is not None
    assert cycle.id.startswith("sc-")


@pytest.mark.asyncio
async def test_spawn_design_doc_update_cycle_history_unbounded_in_table(
    db_session, mocker
):
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
        db_session.add(
            _SC(
                id=f"sc-{i:08x}",
                project_id=project.id,
                outcome="completed",
                phase1_started_at=_utcnow() - timedelta(days=i + 1),
            )
        )
    await db_session.commit()

    await _spawn_design_doc_update(project, db_session)

    all_cycles = (
        (await db_session.execute(_select(_SC).where(_SC.project_id == project.id)))
        .scalars()
        .all()
    )
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
    db_session.add(
        _SC(
            id=cycle_id,
            project_id=project.id,
            outcome=strategic_cycle.OUTCOME_PHASE1_RUNNING,
            phase1_started_at=_utcnow(),
        )
    )
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
    d_stat_ship = next(
        s for s in payload["directions_summary"] if s["id"] == "d-mode03"
    )
    d_stat_unset = next(
        s for s in payload["directions_summary"] if s["id"] == "d-mode04"
    )

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
                RECOMMENDED_MODE_META_KEY: {
                    "source": "operator",
                    "set_at": "2026-04-01T00:00:00",
                },
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
async def test_spawn_strategic_review_no_cycle_id_does_not_modify_history(
    db_session, mocker
):
    """When called without cycle_id, _spawn_strategic_review leaves cycle history unchanged."""
    _patch_spawning_deps(mocker)
    mocker.patch(
        "app.task_lifecycle._has_active_strategic_task",
        new=AsyncMock(return_value=False),
    )
    project = _make_project(
        vision={
            "purpose": "test purpose",
            "_strategic_cycle_history": [{"cycle_id": "existing", "outcome": "ok"}],
        }
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _spawn_strategic_review(project, db_session, cycle_id=None)

    history = (project.vision or {}).get("_strategic_cycle_history", [])
    assert len(history) == 1
    assert history[0].get("outcome") == "ok"


@pytest.mark.asyncio
async def test_spawn_strategic_review_task_context_includes_cycle_id(
    db_session, mocker
):
    """Task created by _spawn_strategic_review has strategic_cycle_id in context."""
    _patch_spawning_deps(mocker)
    mocker.patch(
        "app.task_lifecycle._has_active_strategic_task",
        new=AsyncMock(return_value=False),
    )
    cycle_id = "sc-testcycle"
    project = _make_project(
        vision={
            "purpose": "test purpose",
            "_strategic_cycle_history": [{"cycle_id": cycle_id}],
        }
    )
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
                "id": "d-new01",
                "status": "active",
                "text": "Newly created direction",
                "priority": "medium",
                "research_history": [{"prs_merged": 2, "prs_rejected": 0}],
            },
            {
                "id": "d-retired01",
                "status": "retired",
                "text": "Direction retired last cycle",
                "priority": "low",
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
        directions=[
            {"id": "d-a", "status": "active", "text": "d", "priority": "medium"}
        ],
        vision={
            "purpose": "test",
            "_strategic_cycle_history": [
                {
                    "cycle_id": "sc-old",
                    "outcome": "completed",
                },  # no created/retired ids
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
                "id": "d-new01",
                "status": "active",
                "text": "new direction",
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
    assert (
        retired_stat.get("rationale") == "Saturated; low PR merge volume for 3 cycles."
    )


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
async def test_previous_decision_outcomes_rationale_absent_when_not_recorded(
    db_session,
):
    """When rationale fields are not present in the cycle entry, direction stats have no rationale key."""
    project = _make_project(
        directions=[
            {
                "id": "d-r",
                "status": "active",
                "text": "d",
                "priority": "medium",
                "research_history": [],
            },
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
    assert prev["retired"] == [
        {"direction_id": "d-deleted", "role": "retired", "status": "missing"}
    ]


# ── _build_shipped_changes_manifest ──────────────────────────────────────────


def _make_merged_task(
    project_id, title="Fix bug", direction_id="d-001", changed_files=None, scope="small"
):
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

    db_session.add(
        _make_merged_task(
            project.id, "Task A", direction_id="d-001", changed_files=["a.py"]
        )
    )
    db_session.add(
        _make_merged_task(
            project.id, "Task B", direction_id="d-001", changed_files=["a.py", "b.py"]
        )
    )
    db_session.add(
        _make_merged_task(
            project.id, "Task C", direction_id="d-002", changed_files=["c.py"]
        )
    )
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
                "last_updated": (
                    datetime.now(timezone.utc) + timedelta(hours=1)
                ).isoformat()
            }
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        _make_merged_task(project.id, "Old merged task", direction_id="d-001")
    )
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
                    "phase1_completed_at": (
                        datetime.now(timezone.utc) + timedelta(hours=1)
                    ).isoformat(),
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


@pytest.mark.asyncio
async def test_build_shipped_manifest_excludes_rollup_children(db_session):
    """Rollup child tasks (parent_task_id set) are excluded from the manifest.

    A rollup child's PR merges to the rollup staging branch, not the integration
    branch. Only the rollup parent (root task, parent_task_id=None) should appear
    in the manifest once its own PR lands on the integration branch.
    """
    project = _make_project(vision={"purpose": "test"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Rollup parent — its PR landed on the integration branch
    parent = Task(
        project_id=project.id,
        title="Rollup: ship feature X",
        description="rollup",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context={"pr_merged": True, "direction_id": "d-001"},
    )
    db_session.add(parent)
    await db_session.flush()

    # Rollup child — its PR merged to the staging branch only
    child = Task(
        project_id=project.id,
        title="child: add helper",
        description="child",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        parent_task_id=parent.id,
        context={"pr_merged": True, "direction_id": "d-001"},
    )
    db_session.add(child)
    await db_session.commit()

    manifest = await _build_shipped_changes_manifest(project, db_session)

    # Only the root task should appear; the child should be excluded
    assert len(manifest) == 1
    assert manifest[0]["direction_id"] == "d-001"
    assert len(manifest[0]["tasks"]) == 1
    assert manifest[0]["tasks"][0]["title"] == "Rollup: ship feature X"


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
                {
                    "taxonomy_summary": {"code_defect": 3},
                    "tasks_created": 2,
                    "prs_merged": 2,
                },
            ],
        }
        poor = {
            "research_history": [
                {
                    "taxonomy_summary": {"infra_related": 3, "review_quality": 2},
                    "tasks_created": 2,
                    "prs_merged": 0,
                },
            ],
        }
        assert predict_spawn_success("small", healthy) > predict_spawn_success(
            "small", poor
        )

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
        assert predict_spawn_success("small", hot) > predict_spawn_success(
            "small", neutral
        )

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
        assert predict_spawn_success("small", stalled) < predict_spawn_success(
            "small", neutral
        )

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
        assert predict_spawn_success("small", regressed) < predict_spawn_success(
            "small", clean
        )


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
        project_bad = _make_project(
            design_doc={
                "direction_first_attempt_feedback": {
                    "dir-abc": {"correctness": 8, "style": 2, "total": 10},
                },
            }
        )
        project_clean = _make_project(design_doc={})
        assert predict_spawn_success(
            "small", direction, project=project_bad
        ) < predict_spawn_success("small", direction, project=project_clean)

    def test_moderate_concentration_penalty(self):
        from app.spawn_calibration import predict_spawn_success

        direction = self._direction()
        project_mod = _make_project(
            design_doc={
                "direction_first_attempt_feedback": {
                    "dir-abc": {"correctness": 3, "style": 2, "scope": 2, "total": 7},
                },
            }
        )
        project_strong = _make_project(
            design_doc={
                "direction_first_attempt_feedback": {
                    "dir-abc": {"correctness": 8, "style": 2, "total": 10},
                },
            }
        )
        # Strong signal (0.3) penalizes more than moderate (0.5)
        assert predict_spawn_success(
            "small", direction, project=project_strong
        ) < predict_spawn_success("small", direction, project=project_mod)

    def test_below_threshold_omitted(self):
        from app.spawn_calibration import predict_spawn_success

        direction = self._direction()
        project_low = _make_project(
            design_doc={
                "direction_first_attempt_feedback": {
                    "dir-abc": {"correctness": 2, "style": 1, "total": 3},
                },
            }
        )
        project_none = _make_project(design_doc={})
        assert predict_spawn_success(
            "small", direction, project=project_low
        ) == predict_spawn_success("small", direction, project=project_none)

    def test_spread_not_concentrated_omitted(self):
        from app.spawn_calibration import predict_spawn_success

        direction = self._direction()
        project_spread = _make_project(
            design_doc={
                "direction_first_attempt_feedback": {
                    "dir-abc": {"a": 2, "b": 2, "c": 2, "d": 2, "total": 8},
                },
            }
        )
        project_none = _make_project(design_doc={})
        assert predict_spawn_success(
            "small", direction, project=project_spread
        ) == predict_spawn_success("small", direction, project=project_none)

    def test_missing_direction_id_omitted(self):
        from app.spawn_calibration import predict_spawn_success

        direction = {"research_history": [{"tasks_created": 2, "prs_merged": 1}]}
        project_bad = _make_project(
            design_doc={
                "direction_first_attempt_feedback": {
                    "dir-abc": {"correctness": 8, "total": 8},
                },
            }
        )
        project_none = _make_project(design_doc={})
        assert predict_spawn_success(
            "small", direction, project=project_bad
        ) == predict_spawn_success("small", direction, project=project_none)


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
            "occurrence_count": 5,
            "severity": "high",
            "first_seen": "2026-04-01T00:00:00",
            "last_seen": "2026-04-10T00:00:00",
        },
        "LOW_SIGNAL": {
            "occurrence_count": 2,
            "severity": "low",
            "first_seen": "2026-04-01T00:00:00",
            "last_seen": "2026-04-05T00:00:00",
        },
        "CrossDirectionFingerprint_abc123": {
            "occurrence_count": 4,
            "severity": "high",
            "fingerprint": "abc123",
            "affected_directions": ["d-1", "d-2", "d-3"],
            "failure_count": 12,
            "first_seen": "2026-04-01T00:00:00",
            "last_seen": "2026-04-10T00:00:00",
        },
    }
    try:
        spawned = await _spawn_strategic_review(project, db_session)
        assert spawned is True
        result = await db_session.execute(
            select(Task).where(Task.project_id == project.id)
        )
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
            "occurrence_count": 1,
            "severity": "medium",
            "first_seen": "2026-04-01T00:00:00",
            "last_seen": "2026-04-01T00:00:00",
        },
    }
    try:
        await _spawn_strategic_review(project, db_session)
        result = await db_session.execute(
            select(Task).where(Task.project_id == project.id)
        )
        payload = result.scalars().all()[0].payloads[0]
        assert "anomaly_summary" not in payload
    finally:
        _shared_state.persistent_anomalies.pop(proj_key, None)


@pytest.mark.asyncio
async def test_anomaly_summary_skips_non_dict_sentinel(db_session):
    """A non-dict sibling value (the 'signal_last_computed_at' ISO-string sentinel
    written by anomaly_detection._compute_research_anomalies) must not crash the
    strategic-review spawn. Regression: _build_anomaly_summary previously called
    .get() on the string and raised AttributeError, hard-killing every spawn."""
    project = _make_project(vision={"purpose": "test"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    proj_key = str(project.id)
    _shared_state.persistent_anomalies[proj_key] = {
        # Bookkeeping sentinel — a string value living alongside the entry dicts.
        "signal_last_computed_at": "2026-05-23T00:00:00",
        "CODE_ERROR": {
            "occurrence_count": 5,
            "severity": "high",
            "first_seen": "2026-04-01T00:00:00",
            "last_seen": "2026-04-10T00:00:00",
        },
    }
    try:
        spawned = await _spawn_strategic_review(project, db_session)
        assert spawned is True
        result = await db_session.execute(
            select(Task).where(Task.project_id == project.id)
        )
        payload = result.scalars().all()[0].payloads[0]

        # The real entry is still surfaced; the sentinel is silently skipped.
        summary = payload["anomaly_summary"]
        recurring_keys = [e["key"] for e in summary["recurring_anomalies"]]
        assert "CODE_ERROR" in recurring_keys
        assert "signal_last_computed_at" not in recurring_keys
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
            "updated_at": f"2026-04-{i + 1:02d}T00:00:00",
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
        github_owner="test",
        github_repo="repo",
        autonomous_enabled=True,
        vision={"purpose": "test"},
        design_doc={
            "summary": "ok",
            "playbook_archive": [
                {"_archived_key": f"playbook/e{i}"} for i in range(100)
            ],
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
        {
            "id": f"d{i}",
            "status": "active",
            "text": f"direction {i}",
            "priority": "medium",
            "research_history": [{"prs_merged": 1}],
        }
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
    assert factor < 1.0, (
        f"Expected factor < 1.0 for over-prediction history, got {factor}"
    )
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
            {
                "id": "d-sm01",
                "status": "active",
                "text": "x",
                "priority": "medium",
                "system_id": "sys-A",
            },
        ],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        SystemMaturity(
            project_id=project.id,
            system_id="sys-A",
            score=72,
            components={"test_coverage": 0.5},
            recommended_mode="ship",
            top_issues=[{"description": "thin tests"}],
            last_audited_at=_utcnow(),
        )
    )
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
            {
                "id": "d-sm02",
                "status": "active",
                "text": "x",
                "priority": "medium",
                "system_id": "sys-B",
            },
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
            {
                "id": "d-drift01",
                "status": "active",
                "text": "x",
                "priority": "medium",
                "system_id": "sys-C",
            },
        ],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Create 5 completed claim-tasks with no PR artifacts → stalled_despite_churn
    for i in range(5):
        db_session.add(
            _Task(
                project_id=project.id,
                title=f"claim {i}",
                description="",
                status=_TaskStatus.COMPLETED,
                created_by="ORCHESTRATOR",
                payloads=[
                    {
                        "type": "create",
                        "prompt": "x",
                        "current_direction_id": "d-drift01",
                    }
                ],
                context={"direction_id": "d-drift01"},
                plan_output={"summary": "shipped"},
                vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
            )
        )
    await db_session.commit()

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks
    payload = tasks[0].payloads[0]
    assert "drift_signals" in payload
    drift = payload["drift_signals"]
    assert "by_system" in drift
    assert "generated_at" in drift


@pytest.mark.asyncio
async def test_drift_signals_persist_snapshot_and_include_consecutive_cycles(
    db_session,
):
    """_spawn_strategic_review persists a drift snapshot and enriches the payload
    with consecutive_flagged_cycles when prior flagged snapshots exist."""
    from datetime import timedelta
    from app.drift_detection import DirectionDriftSnapshot
    from app.time_utils import _utcnow

    project = _make_project(
        directions=[
            {
                "id": "d-trend01",
                "status": "active",
                "text": "x",
                "priority": "medium",
                "system_id": "sys-T",
            },
        ],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Seed two prior flagged snapshots to simulate previous cycles.
    now = _utcnow()
    for i in range(2, 0, -1):
        db_session.add(
            DirectionDriftSnapshot(
                project_id=project.id,
                direction_id="d-trend01",
                snapshot_at=now - timedelta(hours=i * 6),
                claimed_changes_count=5,
                actually_merged_prs=0,
                claim_shipped_consistency=0.0,
                over_claim_flag=False,
                stalled_despite_churn_flag=True,
            )
        )
    await db_session.commit()

    # Create 5 completed claim-tasks with no PR artifacts → stalled_despite_churn
    for i in range(5):
        db_session.add(
            Task(
                project_id=project.id,
                title=f"trend claim {i}",
                description="",
                status=TaskStatus.COMPLETED,
                created_by="ORCHESTRATOR",
                payloads=[
                    {
                        "type": "create",
                        "prompt": "x",
                        "current_direction_id": "d-trend01",
                    }
                ],
                context={"direction_id": "d-trend01"},
                plan_output={"summary": "shipped"},
                vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
            )
        )
    await db_session.commit()

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    sr_tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert sr_tasks, "No strategic_review task spawned"
    payload = sr_tasks[0].payloads[0]
    assert "drift_signals" in payload, (
        f"drift_signals missing; keys: {sorted(payload.keys())}"
    )
    drift = payload["drift_signals"]
    assert "d-trend01" in drift.get("by_direction", {}), (
        f"d-trend01 not in by_direction: {list(drift.get('by_direction', {}).keys())}"
    )
    sig = drift["by_direction"]["d-trend01"]
    # 2 prior flagged + 1 current (written by persist_drift_snapshot) = 3 consecutive
    assert "consecutive_flagged_cycles" in sig, (
        f"consecutive_flagged_cycles missing from sig: {sig}"
    )
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
    assert len(snaps) == 3, (
        f"Expected 3 snapshots (2 seeded + 1 persisted), got {len(snaps)}"
    )


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
# efficiency_snapshot in strategic payload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_strategic_review_includes_efficiency_snapshot_when_data_present(
    db_session,
):
    """efficiency_snapshot is present in the payload when baseline data exists."""
    from unittest.mock import AsyncMock, patch

    project = _make_project(
        directions=[
            {"id": "d-eff01", "status": "active", "text": "x", "priority": "medium"}
        ],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    _sample_snapshot = {
        "axes": {
            "cost_per_merged_pr": {
                "current": 1.5,
                "baseline": 2.0,
                "drift_pct": -25.0,
                "history": [],
            },
            "vm_reuse_rate": {
                "current": 0.6,
                "baseline": 0.5,
                "drift_pct": 20.0,
                "history": [],
            },
            "vm_cold_start_p50_ms": {
                "current": 500.0,
                "baseline": 600.0,
                "drift_pct": -16.67,
                "history": [],
            },
            "median_api_calls_per_task": {
                "current": 40.0,
                "baseline": 50.0,
                "drift_pct": -20.0,
                "history": [],
            },
            "median_review_iterations": {
                "current": 1.5,
                "baseline": 2.0,
                "drift_pct": -25.0,
                "history": [],
            },
        },
        "baseline_captured_at": "2026-04-05T00:00:00",
        "last_snapshot_at": "2026-05-01T00:00:00",
    }

    with patch(
        "app.efficiency_snapshot.read_baseline_with_drift",
        new=AsyncMock(return_value=_sample_snapshot),
    ):
        spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is True
    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks
    payload = tasks[0].payloads[0]
    assert "efficiency_snapshot" in payload
    snap = payload["efficiency_snapshot"]
    assert "axes" in snap
    assert "cost_per_merged_pr" in snap["axes"]
    assert snap["axes"]["cost_per_merged_pr"]["drift_pct"] == -25.0


@pytest.mark.asyncio
async def test_strategic_review_omits_efficiency_snapshot_when_no_data(db_session):
    """efficiency_snapshot is omitted when all current axis values are None."""
    from unittest.mock import AsyncMock, patch

    project = _make_project(
        directions=[
            {"id": "d-eff02", "status": "active", "text": "x", "priority": "medium"}
        ],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    _empty_snapshot = {
        "axes": {
            "cost_per_merged_pr": {
                "current": None,
                "baseline": None,
                "drift_pct": None,
                "history": [],
            },
            "vm_reuse_rate": {
                "current": None,
                "baseline": None,
                "drift_pct": None,
                "history": [],
            },
            "vm_cold_start_p50_ms": {
                "current": None,
                "baseline": None,
                "drift_pct": None,
                "history": [],
            },
            "median_api_calls_per_task": {
                "current": None,
                "baseline": None,
                "drift_pct": None,
                "history": [],
            },
            "median_review_iterations": {
                "current": None,
                "baseline": None,
                "drift_pct": None,
                "history": [],
            },
        },
        "baseline_captured_at": None,
        "last_snapshot_at": None,
    }

    with patch(
        "app.efficiency_snapshot.read_baseline_with_drift",
        new=AsyncMock(return_value=_empty_snapshot),
    ):
        spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is True
    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks
    payload = tasks[0].payloads[0]
    assert "efficiency_snapshot" not in payload


# ---------------------------------------------------------------------------
# parallelism_snapshot removed from strategic payload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_strategic_review_omits_parallelism_snapshot(db_session):
    """parallelism_snapshot is no longer built or shipped in the strategic payload.

    It was a Blueprint VM-pool infra metric that does not generalize to an
    arbitrary target repo. Seeds VM-timing rows that would previously have
    populated the snapshot and confirms neither the payload key nor the
    prompt_suffix observation note appears, and that it is gone from the schema.
    """
    project = _make_project(
        directions=[
            {"id": "d-par01", "status": "active", "text": "x", "priority": "medium"}
        ],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    from app.time_utils import _utcnow

    now = _utcnow()
    active_task = Task(
        project_id=project.id,
        title="active",
        description="",
        payloads=[{"type": "research"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.IN_PROGRESS,
        created_by="ORCHESTRATOR",
        vm_started_at=now - timedelta(minutes=30),
        context={"direction_id": "d-par01"},
    )
    db_session.add(active_task)
    await db_session.commit()

    prev_cap = _shared_state.runtime_config.get("max_concurrent_vms")
    _shared_state.runtime_config["max_concurrent_vms"] = 15
    try:
        spawned = await _spawn_strategic_review(project, db_session)
    finally:
        if prev_cap is None:
            _shared_state.runtime_config.pop("max_concurrent_vms", None)
        else:
            _shared_state.runtime_config["max_concurrent_vms"] = prev_cap

    assert spawned is True
    result = await db_session.execute(
        select(Task).where(Task.project_id == project.id)
    )
    strategic_tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert strategic_tasks
    payload = strategic_tasks[0].payloads[0]
    assert "parallelism_snapshot" not in payload
    suffix = payload.get("prompt_suffix") or ""
    assert "throughput depends on" not in suffix
    assert "pool capacity" not in suffix

    # Gone from the pack schema too.
    from app.strategic_spawning import _STRATEGIC_REVIEW_SCHEMA

    assert "parallelism_snapshot" not in _STRATEGIC_REVIEW_SCHEMA


# ---------------------------------------------------------------------------
# held_directions block in strategic payload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_strategic_review_includes_held_directions_block(db_session):
    """When a direction is HELD, the strategic_review payload carries a
    read-only held_directions block: the direction text, consecutive-failure
    count, dominant error fingerprint, and a few RAW recent failure messages.
    """
    from app.shared_state import record_direction_result, DIRECTION_HOLD_THRESHOLD

    _shared_state.direction_failure_streaks.clear()
    direction_id = "d-held01"
    project = _make_project(
        directions=[
            {
                "id": direction_id,
                "status": "active",
                "text": "Decompose the giant module",
                "priority": "medium",
            }
        ],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Seed three FAILED tasks on the direction so the block can surface raw
    # failure messages + a dominant fingerprint.
    for i in range(3):
        db_session.add(
            Task(
                project_id=project.id,
                title=f"failed-{i}",
                description="",
                payloads=[{"type": "create"}],
                vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
                status=TaskStatus.FAILED,
                created_by="ORCHESTRATOR",
                latest_status_message=f"ImportError: cannot import name foo (attempt {i})",
                context={"direction_id": direction_id},
            )
        )
    await db_session.commit()

    # Drive the direction into the HELD state.
    try:
        for _ in range(DIRECTION_HOLD_THRESHOLD):
            record_direction_result(
                direction_id,
                success=False,
                failure_class="CODE_ERROR",
                project_id=str(project.id),
            )
        assert _shared_state.get_held_directions().get(direction_id)

        spawned = await _spawn_strategic_review(project, db_session)
    finally:
        _shared_state.direction_failure_streaks.clear()

    assert spawned is True
    result = await db_session.execute(
        select(Task).where(Task.project_id == project.id)
    )
    strategic_tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert strategic_tasks
    payload = strategic_tasks[0].payloads[0]
    assert "held_directions" in payload
    held = payload["held_directions"]
    assert isinstance(held, list) and len(held) == 1
    entry = held[0]
    assert entry["direction_id"] == direction_id
    assert "Decompose the giant module" in entry["direction_text"]
    assert entry["consecutive_failures"] == DIRECTION_HOLD_THRESHOLD
    assert entry["last_failure_class"] == "CODE_ERROR"
    assert entry["dominant_error_fingerprint"]
    msgs = entry["recent_failure_messages"]
    assert isinstance(msgs, list) and msgs
    assert any("ImportError" in m for m in msgs)


@pytest.mark.asyncio
async def test_strategic_review_omits_held_directions_when_none_held(db_session):
    """When no direction is held, the held_directions block is absent."""
    _shared_state.direction_failure_streaks.clear()
    project = _make_project(
        directions=[
            {"id": "d-ok01", "status": "active", "text": "x", "priority": "medium"}
        ],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    try:
        spawned = await _spawn_strategic_review(project, db_session)
    finally:
        _shared_state.direction_failure_streaks.clear()

    assert spawned is True
    result = await db_session.execute(
        select(Task).where(Task.project_id == project.id)
    )
    strategic_tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert strategic_tasks
    payload = strategic_tasks[0].payloads[0]
    assert "held_directions" not in payload


# ---------------------------------------------------------------------------
# _build_failure_feedback_mitigation unit tests
# ---------------------------------------------------------------------------


def test_failure_feedback_downscopes_large_to_medium():
    """3 first-attempt failures in a category downscopes large→medium."""
    from app.strategic_spawning import _build_failure_feedback_mitigation

    direction = {"id": "d-abc123"}
    design_doc = {
        "direction_first_attempt_feedback": {
            "d-abc123": {
                "code_defect": 3,
                "total": 3,
                "last_updated": "2026-04-20T00:00:00",
            },
        }
    }
    scope, note, evidence = _build_failure_feedback_mitigation(
        direction, "large", "create", design_doc
    )
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
            "d-def456": {
                "testing": 5,
                "scope_related": 1,
                "total": 6,
                "last_updated": "2026-04-20T00:00:00",
            },
        }
    }
    scope, note, evidence = _build_failure_feedback_mitigation(
        direction, "medium", "create", design_doc
    )
    assert scope == "small"
    assert note is not None and "testing" in note
    assert evidence["failure_downscope"]["category"] == "testing"


def test_failure_feedback_no_downscope_below_threshold():
    """2 failures (< threshold) leaves scope and note unchanged."""
    from app.strategic_spawning import _build_failure_feedback_mitigation

    direction = {"id": "d-ghi789"}
    design_doc = {
        "direction_first_attempt_feedback": {
            "d-ghi789": {
                "code_defect": 2,
                "total": 2,
                "last_updated": "2026-04-20T00:00:00",
            },
        }
    }
    scope, note, evidence = _build_failure_feedback_mitigation(
        direction, "medium", "create", design_doc
    )
    assert scope == "medium"
    assert note is None
    assert "failure_downscope" not in evidence


def test_success_pattern_biases_medium_to_small():
    """playbook/success-create-small with 3+ successes biases medium→small."""
    from app.strategic_spawning import _build_failure_feedback_mitigation

    direction = {"id": "d-jkl012"}
    design_doc = {
        "playbook/success-create-small": {
            "source": "success-pattern",
            "success_count": 4,
        },
    }
    scope, _note, evidence = _build_failure_feedback_mitigation(
        direction, "medium", "create", design_doc
    )
    assert scope == "small"
    assert evidence["success_bias"]["success_count"] == 4
    assert evidence["success_bias"]["to"] == "small"


def test_success_pattern_insufficient_count_no_bias():
    """2 successes (< threshold) leaves scope unchanged."""
    from app.strategic_spawning import _build_failure_feedback_mitigation

    direction = {"id": "d-mno345"}
    design_doc = {
        "playbook/success-create-small": {
            "source": "success-pattern",
            "success_count": 2,
        },
    }
    scope, _note, evidence = _build_failure_feedback_mitigation(
        direction, "medium", "create", design_doc
    )
    assert scope == "medium"
    assert "success_bias" not in evidence


def test_no_data_returns_proposed_scope_unchanged():
    """Empty design_doc leaves scope and note unchanged."""
    from app.strategic_spawning import _build_failure_feedback_mitigation

    scope, note, evidence = _build_failure_feedback_mitigation(
        {"id": "d-xyz"}, "medium", "create", {}
    )
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
            {
                "id": "d-small1",
                "status": "active",
                "text": "Small direction",
                "priority": "medium",
                "research_history": [{"prs_merged": 1}],
            }
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
    assert meta["trimmed"] == [], (
        f"Small project should have no trims: {meta['trimmed']}"
    )
    assert meta["dropped"] == [], (
        f"Small project should have no drops: {meta['dropped']}"
    )
    assert meta["budget_used"] > 0
    assert meta["budget_used"] < meta["budget_total"]


@pytest.mark.asyncio
async def test_strategic_review_packer_keeps_payload_under_cap_on_bloated_project(
    db_session,
):
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
                    "key_finding_descriptions": [
                        f"finding {j} " * 20 for j in range(5)
                    ],
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
                {
                    "id": f"g{i}",
                    "name": f"gap {i}",
                    "description": "x" * 500,
                    "history": [f"h{j}" * 10 for j in range(20)],
                    "past_moves": [f"m{j}" * 10 for j in range(20)],
                    "evidence": [f"e{j}" * 10 for j in range(20)],
                }
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
                    "key_finding_descriptions": [
                        f"finding {j} " * 20 for j in range(5)
                    ],
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
                {
                    "id": f"g{i}",
                    "name": f"gap {i}",
                    "description": "x" * 500,
                    "history": [f"h{j}" * 10 for j in range(20)],
                    "past_moves": [f"m{j}" * 10 for j in range(20)],
                    "evidence": [f"e{j}" * 10 for j in range(20)],
                }
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
        db_session.add(
            Task(
                project_id=project.id,
                title=f"drift claim {i}",
                description="",
                status=TaskStatus.COMPLETED,
                created_by="ORCHESTRATOR",
                payloads=[
                    {"type": "create", "prompt": "x", "current_direction_id": "d-dp00"}
                ],
                context={"direction_id": "d-dp00"},
                plan_output={"summary": "shipped"},
                vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
            )
        )
    await db_session.commit()

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
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


# ── version_phase: promotion_readiness + recent_versions enrichment ───────────


async def _ensure_verdict_table(session):
    """Create qualificationverdict table idempotently (mirrors test_release_qualifier pattern)."""
    from app.models import QualificationVerdict as _QV

    conn = await session.connection()
    await conn.run_sync(lambda c: _QV.__table__.create(c, checkfirst=True))
    await session.commit()


async def _ensure_releaseversion_table(session):
    """Create releaseversion table idempotently."""
    from app.models import ReleaseVersion as _RV

    conn = await session.connection()
    await conn.run_sync(lambda c: _RV.__table__.create(c, checkfirst=True))
    await session.commit()


@pytest.mark.asyncio
async def test_version_phase_includes_promotion_readiness(
    db_session, mocker, _lts_split_active
):
    """When is_self_repo=True and QualificationVerdict rows exist, the strategic
    payload's version_phase block carries promotion_readiness with ready,
    would_promote_rate, and recurring_blockers keys."""
    from datetime import datetime
    from app.models import ReleaseVersion, QualificationVerdict

    await _ensure_releaseversion_table(db_session)
    await _ensure_verdict_table(db_session)

    project = _make_project(
        is_self_repo=True,
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Open version in stabilize phase
    now = datetime.utcnow().replace(tzinfo=None)
    version = ReleaseVersion(
        project_id=project.id,
        version_number=2,
        phase="stabilize",
        status="open",
        opened_at=now,
    )
    db_session.add(version)
    await db_session.commit()

    await db_session.refresh(version)

    # Seed QualificationVerdict rows (promotion_enabled=False = dry-run regime)
    for i in range(3):
        db_session.add(
            QualificationVerdict(
                project_id=project.id,
                release_version_id=version.id,
                would_promote=True,
                blocker=None,
                confidence=0.85 + i * 0.01,
                promotion_enabled=False,
            )
        )
    await db_session.commit()

    _patch_spawning_deps(mocker)
    spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is True
    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks, "Expected a strategic_review task"
    payload = tasks[0].payloads[0]

    vp = payload.get("version_phase")
    assert vp is not None, "version_phase block should be present for is_self_repo=True"
    pr = vp.get("promotion_readiness")
    assert pr is not None, "promotion_readiness should be present when verdicts exist"
    assert "ready" in pr, "promotion_readiness must have 'ready' key"
    assert "would_promote_rate" in pr, (
        "promotion_readiness must have 'would_promote_rate' key"
    )
    assert "recurring_blockers" in pr, (
        "promotion_readiness must have 'recurring_blockers' key"
    )


@pytest.mark.asyncio
async def test_version_phase_includes_recent_versions(
    db_session, mocker, _lts_split_active
):
    """When is_self_repo=True and closed ReleaseVersion rows exist, version_phase
    carries a recent_versions list."""
    from datetime import datetime, timedelta
    from app.models import ReleaseVersion

    await _ensure_releaseversion_table(db_session)

    project = _make_project(
        is_self_repo=True,
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = datetime.utcnow().replace(tzinfo=None)

    # One closed (promoted) version
    closed = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        phase="stabilize",
        status="promoted",
        opened_at=now - timedelta(days=10),
        promoted_at=now - timedelta(days=2),
    )
    db_session.add(closed)

    # One open version
    open_v = ReleaseVersion(
        project_id=project.id,
        version_number=2,
        phase="build",
        status="open",
        opened_at=now,
    )
    db_session.add(open_v)
    await db_session.commit()

    _patch_spawning_deps(mocker)
    spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is True
    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks
    payload = tasks[0].payloads[0]

    vp = payload.get("version_phase")
    assert vp is not None
    rv = vp.get("recent_versions")
    assert rv is not None, (
        "recent_versions should be present when closed versions exist"
    )
    assert isinstance(rv, list)
    assert len(rv) >= 1
    # Each entry should have version_number and status
    for entry in rv:
        assert "version_number" in entry
        assert "status" in entry


@pytest.mark.asyncio
async def test_version_phase_uses_promotion_target_gate(db_session, mocker):
    """Version-phase LTS context is active only when the resolver sees distinct branches."""
    await _ensure_releaseversion_table(db_session)

    _patch_spawning_deps(mocker)
    current_open_version = mocker.patch(
        "app.release_version.current_open_version",
        new=AsyncMock(return_value=None),
    )
    ensure_initial_release_version = mocker.patch(
        "app.release_version.ensure_initial_release_version",
        new=AsyncMock(return_value=None),
    )

    skipped_project = _make_project(
        integration_branch="trunk",
        promotion_target="trunk",
        vision={"purpose": "test purpose"},
    )
    db_session.add(skipped_project)
    await db_session.commit()
    await db_session.refresh(skipped_project)

    # integration == promotion: the version-phase LTS context is gated off
    # (the resolver fails safe instead of raising), so the review still spawns
    # but without invoking the version lifecycle.
    assert await _spawn_strategic_review(skipped_project, db_session) is True
    assert current_open_version.call_count == 0
    assert ensure_initial_release_version.call_count == 0

    current_open_version.reset_mock()
    ensure_initial_release_version.reset_mock()

    active_project = _make_project(
        integration_branch="automain",
        promotion_target="main",
        vision={"purpose": "test purpose"},
    )
    db_session.add(active_project)
    await db_session.commit()
    await db_session.refresh(active_project)

    assert await _spawn_strategic_review(active_project, db_session) is True
    assert current_open_version.call_count == 1
    assert ensure_initial_release_version.call_args.kwargs["promotion_target"] == "main"


@pytest.mark.asyncio
async def test_version_phase_bootstrap_passes_custom_promotion_target(
    db_session, mocker
):
    """Bootstrap uses the project promotion target even when integration_branch is main."""
    await _ensure_releaseversion_table(db_session)

    _patch_spawning_deps(mocker)
    mocker.patch(
        "app.release_version.current_open_version",
        new=AsyncMock(return_value=None),
    )
    ensure_initial_release_version = mocker.patch(
        "app.release_version.ensure_initial_release_version",
        new=AsyncMock(return_value=None),
    )

    project = _make_project(
        integration_branch="main",
        promotion_target="deploy",
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    assert await _spawn_strategic_review(project, db_session) is True
    assert ensure_initial_release_version.call_count == 1
    assert (
        ensure_initial_release_version.call_args.kwargs["promotion_target"] == "deploy"
    )


# ── stabilize-stall strategic nudge ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_stabilize_stall_hint_fires_for_old_stabilize_version(
    db_session, mocker, _lts_split_active
):
    """An open stabilize-phase version older than _STABILIZE_STALL_AGE_DAYS with
    no qualification attempt gets a STALLED nudge in the strategic prompt_suffix
    and a stabilize_stall block in version_phase."""
    from datetime import timedelta
    from app.models import ReleaseVersion
    from app.release_qualifier import _STABILIZE_STALL_AGE_DAYS

    await _ensure_releaseversion_table(db_session)

    project = _make_project(
        is_self_repo=True,
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Version stuck in stabilize well past the age threshold
    now = datetime.utcnow().replace(tzinfo=None)
    version = ReleaseVersion(
        project_id=project.id,
        version_number=3,
        phase="stabilize",
        status="open",
        opened_at=now - timedelta(days=_STABILIZE_STALL_AGE_DAYS + 5),
        phase_changed_at=now - timedelta(days=_STABILIZE_STALL_AGE_DAYS + 5),
        last_qualification=None,
    )
    db_session.add(version)
    await db_session.commit()

    _patch_spawning_deps(mocker)
    spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is True
    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks, "Expected a strategic_review task"
    payload = tasks[0].payloads[0]

    suffix = payload.get("prompt_suffix", "")
    assert "STALLED" in suffix, (
        f"Expected STALLED nudge in prompt_suffix; got: {suffix}"
    )
    assert "stabilize" in suffix.lower()

    vp = payload.get("version_phase")
    assert vp is not None
    stall_block = vp.get("stabilize_stall")
    assert stall_block is not None, (
        "stabilize_stall block should appear in version_phase"
    )
    assert stall_block.get("age_days", 0) >= _STABILIZE_STALL_AGE_DAYS


@pytest.mark.asyncio
async def test_stabilize_hint_points_at_carried_audit_findings(
    db_session, mocker, _lts_split_active
):
    """A stabilize version whose last_qualification carries audit findings gets
    a prompt_suffix that names the findings and tells the agent to target them
    with an audit-mode direction — and the findings ride through in
    version_phase.last_qualification.audit_findings."""
    from app.models import ReleaseVersion

    await _ensure_releaseversion_table(db_session)

    project = _make_project(is_self_repo=True, vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = datetime.utcnow().replace(tzinfo=None)
    version = ReleaseVersion(
        project_id=project.id,
        version_number=4,
        phase="stabilize",
        status="open",
        opened_at=now,
        phase_changed_at=now,
        last_qualification={
            "status": "not_promoted",
            "confidence": 0.85,
            "blocker": None,
            "sub_scores": {"audit": 0.74, "test_erosion": 1.0},
            "audit_findings": [
                {"severity": "high", "summary": "Token baked into VM image."},
                {"severity": "low", "summary": "Private cross-module import."},
            ],
        },
    )
    db_session.add(version)
    await db_session.commit()

    _patch_spawning_deps(mocker)
    spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is True
    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks, "Expected a strategic_review task"
    payload = tasks[0].payloads[0]

    suffix = payload.get("prompt_suffix", "")
    assert "audit_findings" in suffix, f"Expected findings pointer; got: {suffix}"
    assert "Token baked into VM image" in suffix
    assert "recommended_mode='audit'" in suffix
    # The project has no active audit/fundamentals-mode direction, so the hint
    # must demand the agent CREATE one rather than merely "update" (a status
    # note had been satisfying the old phrasing).
    assert "MUST create a new direction" in suffix
    assert "does NOT count" in suffix

    vp = payload.get("version_phase")
    assert vp is not None
    carried = (vp.get("last_qualification") or {}).get("audit_findings")
    assert carried == [
        {"severity": "high", "summary": "Token baked into VM image."},
        {"severity": "low", "summary": "Private cross-module import."},
    ]


@pytest.mark.asyncio
async def test_stabilize_findings_hint_softens_when_remediation_direction_exists(
    db_session, mocker, _lts_split_active
):
    """When an active audit-mode direction already exists, the hint must NOT
    demand a new one — it should instead tell the agent to confirm the existing
    direction targets the findings."""
    from app.models import ReleaseVersion

    await _ensure_releaseversion_table(db_session)

    project = _make_project(
        is_self_repo=True,
        vision={"purpose": "test purpose"},
        directions=[
            {
                "id": "d-existing",
                "text": "Fix the release audit findings",
                "status": "active",
                "priority": "high",
                "recommended_mode": "audit",
            }
        ],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = datetime.utcnow().replace(tzinfo=None)
    version = ReleaseVersion(
        project_id=project.id,
        version_number=4,
        phase="stabilize",
        status="open",
        opened_at=now,
        phase_changed_at=now,
        qualification_attempts=2,
        last_qualification={
            "status": "not_promoted",
            "confidence": 0.6971,
            "blocker": None,
            "sub_scores": {"audit": 0.47, "test_erosion": 1.0},
            "audit_findings": [
                {"severity": "high", "summary": "Token TTL rollout gap."},
            ],
        },
    )
    db_session.add(version)
    await db_session.commit()

    _patch_spawning_deps(mocker)
    spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is True
    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks
    suffix = tasks[0].payloads[0].get("prompt_suffix", "")
    assert "Token TTL rollout gap" in suffix
    assert "already have an active audit/fundamentals-mode direction" in suffix
    assert "MUST create a new direction" not in suffix, (
        f"Should not demand a new direction when one exists; got: {suffix}"
    )


@pytest.mark.asyncio
async def test_post_promote_handoff_hint_fires_on_fresh_version(
    db_session, mocker, _lts_split_active
):
    """A version that just opened from a promotion (version_number > 1, never
    qualified) gets a prompt_suffix telling the agent to refresh problem_model
    so the inherited goal narrative doesn't get read as current status."""
    from app.models import ReleaseVersion

    await _ensure_releaseversion_table(db_session)

    project = _make_project(is_self_repo=True, vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = datetime.utcnow().replace(tzinfo=None)
    version = ReleaseVersion(
        project_id=project.id,
        version_number=2,
        phase="build",
        status="open",
        opened_at=now,
        phase_changed_at=now,
        last_qualification=None,
        qualification_attempts=0,
        goal="v1 milestone narrative carried forward as v2 goal",
    )
    db_session.add(version)
    await db_session.commit()

    _patch_spawning_deps(mocker)
    spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is True
    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks, "Expected a strategic_review task"
    payload = tasks[0].payloads[0]

    suffix = payload.get("prompt_suffix", "")
    assert "Version handoff" in suffix, (
        f"Expected post-promotion handoff hint; got: {suffix}"
    )
    assert "intent.current_focus" in suffix
    assert "v2" in suffix and "v1" in suffix


@pytest.mark.asyncio
async def test_post_promote_handoff_hint_self_clears_after_first_qualification(
    db_session, mocker, _lts_split_active
):
    """Once a version has been qualified at least once, the handoff hint must
    no longer fire — the agent should be reading real verdict data, not
    refreshing the carried narrative."""
    from app.models import ReleaseVersion

    await _ensure_releaseversion_table(db_session)

    project = _make_project(is_self_repo=True, vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = datetime.utcnow().replace(tzinfo=None)
    version = ReleaseVersion(
        project_id=project.id,
        version_number=2,
        phase="stabilize",
        status="open",
        opened_at=now,
        phase_changed_at=now,
        last_qualification={"status": "not_promoted", "confidence": 0.85},
        qualification_attempts=1,
    )
    db_session.add(version)
    await db_session.commit()

    _patch_spawning_deps(mocker)
    spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is True
    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks
    payload = tasks[0].payloads[0]

    suffix = payload.get("prompt_suffix", "")
    assert "Version handoff" not in suffix, (
        f"Handoff hint must self-clear once a verdict exists; got: {suffix}"
    )


@pytest.mark.asyncio
async def test_post_promote_handoff_hint_skipped_for_bootstrap_version(
    db_session, mocker, _lts_split_active
):
    """The bootstrap version (v1) didn't open from any prior promotion, so
    the handoff hint must NOT fire even when last_qualification is null."""
    from app.models import ReleaseVersion

    await _ensure_releaseversion_table(db_session)

    project = _make_project(is_self_repo=True, vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = datetime.utcnow().replace(tzinfo=None)
    version = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        phase="stabilize",
        status="open",
        opened_at=now,
        phase_changed_at=now,
        last_qualification=None,
        qualification_attempts=0,
    )
    db_session.add(version)
    await db_session.commit()

    _patch_spawning_deps(mocker)
    spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is True
    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks
    payload = tasks[0].payloads[0]

    suffix = payload.get("prompt_suffix", "")
    assert "Version handoff" not in suffix, (
        f"v1 has no prior promotion; handoff hint must not fire. Got: {suffix}"
    )


@pytest.mark.asyncio
async def test_stabilize_stall_hint_suppressed_for_dry_run(
    db_session, mocker, _lts_split_active
):
    """A dry-run hold (passing candidate, promotion_enabled=False) is healthy
    and must NOT trigger the stalled nudge."""
    from datetime import timedelta
    from app.models import ReleaseVersion
    from app.release_qualifier import _STABILIZE_STALL_AGE_DAYS

    await _ensure_releaseversion_table(db_session)

    project = _make_project(
        is_self_repo=True,
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = datetime.utcnow().replace(tzinfo=None)
    version = ReleaseVersion(
        project_id=project.id,
        version_number=4,
        phase="stabilize",
        status="open",
        opened_at=now - timedelta(days=_STABILIZE_STALL_AGE_DAYS + 5),
        phase_changed_at=now - timedelta(days=_STABILIZE_STALL_AGE_DAYS + 5),
        last_qualification={"status": "dry_run", "candidate_sha": "abc123"},
    )
    db_session.add(version)
    await db_session.commit()

    _patch_spawning_deps(mocker)
    spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is True
    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks
    payload = tasks[0].payloads[0]

    suffix = payload.get("prompt_suffix", "")
    assert "STALLED" not in suffix, (
        "dry_run hold should suppress the stabilize-stall nudge"
    )
    vp = payload.get("version_phase") or {}
    assert "stabilize_stall" not in vp, (
        "stabilize_stall block must not appear for dry_run hold"
    )


# ── build-stall strategic nudge ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_build_stall_nudge_counts_build_phase_merges_not_version_lifetime(
    db_session, mocker, _lts_split_active
):
    """A version that cycled build→stabilize→build must NOT fire the build-stall
    nudge prematurely based on the inflated version-lifetime merge count.

    Setup: version re-entered the build phase recently (phase_changed_at = now).
    There are many pre-phase-change tasks (version-lifetime count > threshold)
    but zero post-phase-change tasks (phase-scoped count = 0).  The nudge must
    NOT fire because neither age nor phase-scoped count exceed the threshold.
    """
    from app.models import ReleaseVersion
    from app.release_qualifier import _BUILD_STALL_MERGED_TASKS

    await _ensure_releaseversion_table(db_session)

    project = _make_project(
        is_self_repo=True,
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = datetime.utcnow().replace(tzinfo=None)
    # Version just re-entered build phase (age = 0 days)
    version = ReleaseVersion(
        project_id=project.id,
        version_number=5,
        phase="build",
        status="open",
        opened_at=now - timedelta(days=60),
        phase_changed_at=now,  # just re-entered build from stabilize
    )
    db_session.add(version)
    await db_session.commit()

    # Seed tasks merged BEFORE the phase transition (pre-phase-change, version-lifetime)
    pre_phase_count = _BUILD_STALL_MERGED_TASKS + 5
    for i in range(pre_phase_count):
        t = Task(
            project_id=project.id,
            title=f"Pre-phase task {i}",
            description="test",
            payloads=[{"type": "create"}],
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
            status=TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
            context={"pr_merged": True, "direction_id": "d-old"},
        )
        t.updated_at = now - timedelta(days=30 + i)
        db_session.add(t)
    await db_session.commit()

    _patch_spawning_deps(mocker)
    spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is True
    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks, "Expected a strategic_review task"
    payload = tasks[0].payloads[0]

    suffix = payload.get("prompt_suffix", "")
    assert "BUILD — STALLED" not in suffix, (
        "build-stall nudge must NOT fire when phase-scoped count is 0 "
        f"(only version-lifetime count {pre_phase_count} exceeds threshold); got: {suffix}"
    )
    vp = payload.get("version_phase") or {}
    assert "build_stall" not in vp, (
        "build_stall block must not appear when phase-scoped merge count is below threshold"
    )


@pytest.mark.asyncio
async def test_build_stall_nudge_tasks_merged_field_is_phase_scoped(
    db_session, mocker, _lts_split_active
):
    """When the build-stall nudge fires, version_phase['build_stall']['tasks_merged']
    must equal only the post-phase_changed_at merge count, not the version-lifetime count.
    """
    from app.models import ReleaseVersion
    from app.release_qualifier import _BUILD_STALL_AGE_DAYS

    await _ensure_releaseversion_table(db_session)

    project = _make_project(
        is_self_repo=True,
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = datetime.utcnow().replace(tzinfo=None)
    # Version re-entered build phase past the age threshold
    phase_entry = now - timedelta(days=_BUILD_STALL_AGE_DAYS + 2)
    version = ReleaseVersion(
        project_id=project.id,
        version_number=6,
        phase="build",
        status="open",
        opened_at=now - timedelta(days=60),
        phase_changed_at=phase_entry,
    )
    db_session.add(version)
    await db_session.commit()

    # Tasks merged BEFORE phase re-entry (should NOT be counted by nudge)
    for i in range(10):
        t = Task(
            project_id=project.id,
            title=f"Pre-entry task {i}",
            description="test",
            payloads=[{"type": "create"}],
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
            status=TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
            context={"pr_merged": True, "direction_id": "d-old"},
        )
        t.updated_at = phase_entry - timedelta(days=1 + i)
        db_session.add(t)

    # Tasks merged AFTER phase re-entry (should be counted)
    post_phase_count = 3
    for i in range(post_phase_count):
        t = Task(
            project_id=project.id,
            title=f"Post-entry task {i}",
            description="test",
            payloads=[{"type": "create"}],
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
            status=TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
            context={"pr_merged": True, "direction_id": "d-new"},
        )
        t.updated_at = phase_entry + timedelta(hours=1 + i)
        db_session.add(t)
    await db_session.commit()

    _patch_spawning_deps(mocker)
    spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is True
    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks, "Expected a strategic_review task"
    payload = tasks[0].payloads[0]

    # Nudge fires because age threshold is exceeded
    suffix = payload.get("prompt_suffix", "")
    assert "BUILD — STALLED" in suffix, (
        f"Expected build-stall nudge to fire on age; got: {suffix}"
    )

    vp = payload.get("version_phase") or {}
    stall_block = vp.get("build_stall")
    assert stall_block is not None, "build_stall block must appear in version_phase"
    reported_merged = stall_block.get("tasks_merged")
    assert reported_merged == post_phase_count, (
        f"build_stall.tasks_merged should be phase-scoped count {post_phase_count}, "
        f"not version-lifetime count; got {reported_merged}"
    )


# ── outcome_evaluation read path for success_evidence/retirement_condition ────


@pytest.mark.asyncio
async def test_outcome_evaluation_includes_success_evidence_and_retirement_condition(
    db_session,
):
    """outcome_evaluation in strategic payload includes success_evidence and retirement_condition when set."""
    long_evidence = "E" * 700
    project = _make_project(
        directions=[
            {
                "id": "d-oe-ev01",
                "status": "active",
                "text": "Direction with outcome criteria",
                "priority": "high",
                "context": {
                    "success_evidence": long_evidence,
                    "retirement_condition": "  retire when fully satisfied  ",
                },
                "research_history": [
                    {
                        "prs_merged": 2,
                        "prs_rejected": 0,
                        "tasks_created": 5,
                        "tasks_failed": 0,
                    }
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

    assert "outcome_evaluation" in payload
    outcome_eval = payload.get("outcome_evaluation", {})
    directions = outcome_eval.get("directions", [])

    assert len(directions) > 0, "outcome_evaluation should have directions"
    outcome = next((o for o in directions if o["id"] == "d-oe-ev01"), None)
    assert outcome is not None, "Direction should be in outcome_evaluation"
    # success_evidence should be capped at 500 chars
    assert outcome.get("success_evidence") == "E" * 500
    # retirement_condition should be stripped and capped
    assert outcome.get("retirement_condition") == "retire when fully satisfied"


@pytest.mark.asyncio
async def test_outcome_evaluation_omits_fields_when_not_set(db_session):
    """outcome_evaluation omits success_evidence/retirement_condition when not in context."""
    project = _make_project(
        directions=[
            {
                "id": "d-oe-ev02",
                "status": "active",
                "text": "Direction without outcome criteria",
                "priority": "medium",
                "research_history": [{"prs_merged": 1, "prs_rejected": 0}],
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
    outcome_eval = payload.get("outcome_evaluation", {})
    directions = outcome_eval.get("directions", [])

    outcome = next((o for o in directions if o["id"] == "d-oe-ev02"), None)
    assert outcome is not None
    assert "success_evidence" not in outcome
    assert "retirement_condition" not in outcome


@pytest.mark.asyncio
async def test_outcome_evaluation_handles_empty_strings(db_session):
    """outcome_evaluation omits empty/whitespace-only success_evidence/retirement_condition."""
    project = _make_project(
        directions=[
            {
                "id": "d-oe-ev03",
                "status": "active",
                "text": "Direction with empty criteria",
                "priority": "low",
                "context": {
                    "success_evidence": "   ",  # whitespace only
                    "retirement_condition": "",  # empty
                },
                "research_history": [{"prs_merged": 1, "prs_rejected": 0}],
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
    outcome_eval = payload.get("outcome_evaluation", {})
    directions = outcome_eval.get("directions", [])

    outcome = next((o for o in directions if o["id"] == "d-oe-ev03"), None)
    assert outcome is not None
    assert "success_evidence" not in outcome
    assert "retirement_condition" not in outcome


# ── on_automain flag in _build_recent_interventions ──────────────────────────


@pytest.mark.asyncio
async def test_on_automain_false_for_root_task_when_unverified(db_session):
    """Root tasks (no parent) report on_automain=False when ancestry cannot be verified.

    When GitHub credentials are unavailable the ancestry check is skipped and the
    task is NOT assumed to have landed on the integration branch (fail-closed).
    """
    project = _make_project(vision={"purpose": "test"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Direct fix",
        description="No parent",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context={"pr_merged": True},
    )
    db_session.add(task)
    await db_session.commit()

    result = await _build_recent_interventions(project, db_session)
    merges = result["merges"]
    assert len(merges) == 1
    assert merges[0]["on_automain"] is False


@pytest.mark.asyncio
async def test_on_automain_true_when_rollup_merged(db_session):
    """Child task reports on_automain=True when its rollup parent's pr_merged=True."""
    project = _make_project(vision={"purpose": "test"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    parent = Task(
        project_id=project.id,
        title="Rollup parent",
        description="rollup",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context={"pr_merged": True, "rollup_pr_number": 42},
    )
    db_session.add(parent)
    await db_session.flush()

    child = Task(
        project_id=project.id,
        title="Child fix",
        description="child",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        parent_task_id=parent.id,
        context={"pr_merged": True},
    )
    db_session.add(child)
    await db_session.commit()

    result = await _build_recent_interventions(project, db_session)
    merges = result["merges"]
    by_title = {m["pr_title"]: m for m in merges}
    # Child is on automain via the parent's pr_merged DB flag (fail-open for child
    # tasks is intentional — the flag is a DB-recorded rollup-merge event).
    assert by_title["Child fix"]["on_automain"] is True
    # Rollup parent is a root task with no credentials → ancestry unverifiable → False.
    assert by_title["Rollup parent"]["on_automain"] is False


@pytest.mark.asyncio
async def test_on_automain_false_when_rollup_not_merged(db_session):
    """Child task reports on_automain=False when its rollup parent is not yet merged."""
    project = _make_project(vision={"purpose": "test"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    parent = Task(
        project_id=project.id,
        title="Unmerged rollup",
        description="rollup",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.EVALUATING,
        created_by="ORCHESTRATOR",
        context={"rollup_pr_number": 99},
    )
    db_session.add(parent)
    await db_session.flush()

    child = Task(
        project_id=project.id,
        title="Staged fix",
        description="child merged into staging only",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        parent_task_id=parent.id,
        context={"pr_merged": True},
    )
    db_session.add(child)
    await db_session.commit()

    result = await _build_recent_interventions(project, db_session)
    merges = result["merges"]
    assert len(merges) == 1
    assert merges[0]["pr_title"] == "Staged fix"
    assert merges[0]["on_automain"] is False


@pytest.mark.asyncio
async def test_rolling_rates_exclude_rollup_children(db_session):
    """Rollup children (parent_task_id set) are not counted as merged in rolling_rates.

    A rollup child's pr_merged=True means its PR landed on the rollup staging branch,
    not the integration branch. Only the rollup parent (root task) should count
    toward the merged counter in rolling_rates when it lands on integration.
    """
    project = _make_project(vision={"purpose": "test"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Rollup parent — merged to integration branch
    parent = Task(
        project_id=project.id,
        title="Rollup parent",
        description="rollup",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context={"pr_merged": True},
    )
    db_session.add(parent)
    await db_session.flush()

    # Two rollup children — each merged to staging only
    for i in range(2):
        child = Task(
            project_id=project.id,
            title=f"Child fix {i}",
            description="child",
            payloads=[{"type": "create"}],
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
            status=TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
            parent_task_id=parent.id,
            context={"pr_merged": True},
        )
        db_session.add(child)
    await db_session.commit()

    result = await _build_recent_interventions(project, db_session)
    rates = result["rolling_rates"]
    # Only the root parent counts as a merged task; the two children are staging-only
    assert rates["last_7d"]["merged"] == 1, (
        f"Expected 1 integration-landed merge (root task only), "
        f"got {rates['last_7d']['merged']}"
    )


# ── git-ancestry landing verification in _build_recent_interventions ──────────


@pytest.mark.asyncio
async def test_on_automain_false_when_commit_not_ancestor(db_session, mocker):
    """pr_merged=True but commit is NOT an ancestor of automain → on_automain=False.

    The ancestry check returns False and there are no _pr_changed_files for the
    content-aware fallback, so the task is correctly reported as NOT landed.
    """
    project = _make_project(vision={"purpose": "test"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Stranded fix",
        description="pr_merged=True but not on automain",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context={"pr_merged": True, "pr_number": 201},
    )
    db_session.add(task)
    await db_session.commit()

    mocker.patch(
        "app.release_qualifier._resolve_github_with_session",
        new=AsyncMock(return_value=("tok", "owner", "repo")),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={
                "merge_commit_sha": "stranded-sha",
                "head": {"sha": "head-sha"},
            }
        ),
    )
    mocker.patch(
        "app.github.is_ancestor_of",
        new=AsyncMock(return_value=False),
    )

    result = await _build_recent_interventions(project, db_session)
    merges = result["merges"]
    assert len(merges) == 1
    assert merges[0]["pr_title"] == "Stranded fix"
    assert merges[0]["on_automain"] is False


@pytest.mark.asyncio
async def test_on_automain_true_when_commit_is_ancestor(db_session, mocker):
    """Commit IS a git-ancestor of automain → on_automain=True via ancestry check."""
    project = _make_project(vision={"purpose": "test"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Landed fix",
        description="merge SHA is a real ancestor",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context={"pr_merged": True, "pr_number": 202},
    )
    db_session.add(task)
    await db_session.commit()

    mocker.patch(
        "app.release_qualifier._resolve_github_with_session",
        new=AsyncMock(return_value=("tok", "owner", "repo")),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={"merge_commit_sha": "landed-sha", "head": {"sha": "head-sha"}}
        ),
    )
    mocker.patch(
        "app.github.is_ancestor_of",
        new=AsyncMock(return_value=True),
    )

    result = await _build_recent_interventions(project, db_session)
    merges = result["merges"]
    assert len(merges) == 1
    assert merges[0]["pr_title"] == "Landed fix"
    assert merges[0]["on_automain"] is True


@pytest.mark.asyncio
async def test_on_automain_false_when_ancestry_api_indeterminate(db_session, mocker):
    """is_ancestor_of returning None (API error / diverged) → on_automain=False.

    Degrade-safe convention: indeterminate result must NOT be treated as landed.
    """
    project = _make_project(vision={"purpose": "test"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Indeterminate fix",
        description="ancestry API returned None",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context={"pr_merged": True, "pr_number": 203},
    )
    db_session.add(task)
    await db_session.commit()

    mocker.patch(
        "app.release_qualifier._resolve_github_with_session",
        new=AsyncMock(return_value=("tok", "owner", "repo")),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={"merge_commit_sha": "some-sha", "head": {"sha": "head-sha"}}
        ),
    )
    mocker.patch(
        "app.github.is_ancestor_of",
        new=AsyncMock(return_value=None),
    )

    result = await _build_recent_interventions(project, db_session)
    merges = result["merges"]
    assert len(merges) == 1
    assert merges[0]["pr_title"] == "Indeterminate fix"
    assert merges[0]["on_automain"] is False


@pytest.mark.asyncio
async def test_on_automain_child_verified_via_parent_rollup_pr(db_session, mocker):
    """Rollup child: ancestry is verified against the PARENT's rollup PR, not the child's own.

    A rollup child's own PR merges into the rollup staging branch, so its merge
    commit is never an ancestor of the integration branch once the rollup PR
    squash-merges. The child is on automain iff the parent's rollup PR landed —
    verifying the child's own PR would (wrongly) report on_automain=False.
    """
    project = _make_project(vision={"purpose": "test"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    parent = Task(
        project_id=project.id,
        title="Rollup parent",
        description="rollup",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context={"pr_merged": True, "rollup_pr_number": 500},
    )
    db_session.add(parent)
    await db_session.flush()

    child = Task(
        project_id=project.id,
        title="Rollup child fix",
        description="child merged into staging branch",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        parent_task_id=parent.id,
        context={"pr_merged": True, "pr_number": 999},
    )
    db_session.add(child)
    await db_session.commit()

    def _pr_side_effect(token, owner, repo, pr_num, project_id=None):
        return {
            999: {"merge_commit_sha": "child-merge-sha", "head": {"sha": "child-head"}},
            500: {
                "merge_commit_sha": "rollup-merge-sha",
                "head": {"sha": "rollup-head"},
            },
        }[pr_num]

    def _anc_side_effect(token, owner, repo, sha, ref, project_id=None):
        # The child's own merge commit lives on the staging branch (not an
        # ancestor); only the rollup PR's squash commit is on the integration
        # branch.
        return sha == "rollup-merge-sha"

    mocker.patch(
        "app.release_qualifier._resolve_github_with_session",
        new=AsyncMock(return_value=("tok", "owner", "repo")),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(side_effect=_pr_side_effect),
    )
    mocker.patch(
        "app.github.is_ancestor_of",
        new=AsyncMock(side_effect=_anc_side_effect),
    )

    result = await _build_recent_interventions(project, db_session)
    by_title = {m["pr_title"]: m for m in result["merges"]}
    # Child is on automain because the PARENT's rollup PR #500 squash-landed —
    # not because the child's own PR #999 (staging-only) is an ancestor.
    assert by_title["Rollup child fix"]["on_automain"] is True
    assert by_title["Rollup parent"]["on_automain"] is True


@pytest.mark.asyncio
async def test_on_automain_child_false_when_parent_rollup_pr_not_ancestor(
    db_session, mocker
):
    """Rollup child: on_automain=False when the parent's rollup PR has not landed.

    The rollup PR's merge commit is not an ancestor of the integration branch
    (rollup merged into a staging branch that itself never reached automain), so
    the child is correctly reported as NOT landed.
    """
    project = _make_project(vision={"purpose": "test"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    parent = Task(
        project_id=project.id,
        title="Stranded rollup parent",
        description="rollup",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context={"pr_merged": True, "rollup_pr_number": 501},
    )
    db_session.add(parent)
    await db_session.flush()

    child = Task(
        project_id=project.id,
        title="Stranded rollup child",
        description="child whose rollup never landed",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        parent_task_id=parent.id,
        context={"pr_merged": True, "pr_number": 998},
    )
    db_session.add(child)
    await db_session.commit()

    mocker.patch(
        "app.release_qualifier._resolve_github_with_session",
        new=AsyncMock(return_value=("tok", "owner", "repo")),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={
                "merge_commit_sha": "rollup-merge-sha",
                "head": {"sha": "rollup-head"},
            }
        ),
    )
    mocker.patch(
        "app.github.is_ancestor_of",
        new=AsyncMock(return_value=False),
    )

    result = await _build_recent_interventions(project, db_session)
    by_title = {m["pr_title"]: m for m in result["merges"]}
    assert by_title["Stranded rollup child"]["on_automain"] is False
    assert by_title["Stranded rollup parent"]["on_automain"] is False


@pytest.mark.asyncio
async def test_on_automain_false_for_root_task_no_pr_number(db_session, mocker):
    """Root task with no pr_number reports on_automain=False even with credentials.

    When credentials resolve but the task has no pr_number or rollup_pr_number in
    its context, no ancestry check can run for it.  The task must NOT be assumed
    landed (fail-closed), so on_automain must be False.
    """
    project = _make_project(vision={"purpose": "test"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="No-PR root fix",
        description="merged but no pr_number",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        # pr_merged=True but no pr_number/rollup_pr_number → ancestry check skipped
        context={"pr_merged": True},
    )
    db_session.add(task)
    await db_session.commit()

    mocker.patch(
        "app.release_qualifier._resolve_github_with_session",
        new=AsyncMock(return_value=("tok", "owner", "repo")),
    )

    result = await _build_recent_interventions(project, db_session)
    merges = result["merges"]
    assert len(merges) == 1
    assert merges[0]["pr_title"] == "No-PR root fix"
    # No pr_number → ancestry check skipped → unverifiable → fail-closed = False
    assert merges[0]["on_automain"] is False


@pytest.mark.asyncio
async def test_on_automain_false_for_root_task_ancestry_api_exception(
    db_session, mocker
):
    """Root task reports on_automain=False when the ancestry API call raises an exception.

    An exception during the ancestry check leaves the task absent from
    _automain_verified.  The fallback must NOT assume landed (fail-closed).
    """
    project = _make_project(vision={"purpose": "test"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="API-error root fix",
        description="ancestry API raises",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context={"pr_merged": True, "pr_number": 777},
    )
    db_session.add(task)
    await db_session.commit()

    mocker.patch(
        "app.release_qualifier._resolve_github_with_session",
        new=AsyncMock(return_value=("tok", "owner", "repo")),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={"merge_commit_sha": "abc123", "head": {"sha": "head-sha"}}
        ),
    )
    mocker.patch(
        "app.github.is_ancestor_of",
        new=AsyncMock(side_effect=RuntimeError("GitHub API unreachable")),
    )

    result = await _build_recent_interventions(project, db_session)
    merges = result["merges"]
    assert len(merges) == 1
    assert merges[0]["pr_title"] == "API-error root fix"
    # API exception → task absent from _automain_verified → fail-closed = False
    assert merges[0]["on_automain"] is False


# ── Per-project integration_branch resolution (column-based) ─────────────────


class TestGetProjectIntegrationBranch:
    """get_project_integration_branch() — the canonical column-based resolver.

    The resolver fails SAFE and never raises, so it is safe to call from read
    endpoints, background recovery loops, and the protected_branches property.
    Self-repos map an unset integration branch to automain; non-self repos with
    an unset integration branch fall back to main. Refusing to land work on the
    promotion target is enforced at the merge/rollup boundary, not here.
    """

    def test_self_repo_without_integration_branch_resolves_to_automain(self):
        """Self-repos preserve the documented automain fallback when unset."""
        project = _make_project(
            integration_branch=None,
            promotion_target=None,
            is_self_repo=True,
            github_repo="BlueprintAIStudio",
        )
        assert _shared_state.get_project_integration_branch(project) == "automain"

    def test_non_self_repo_without_integration_branch_falls_back_to_main(self):
        """External repos with an unset integration branch fall back to main."""
        project = _make_project(integration_branch=None, is_self_repo=False)
        assert _shared_state.get_project_integration_branch(project) == "main"

    def test_column_override_used(self):
        """Project with integration_branch column set uses that value."""
        project = _make_project(integration_branch="feature-trunk")
        assert _shared_state.get_project_integration_branch(project) == "feature-trunk"

    def test_column_override_ignores_global(self):
        """Column value wins regardless of global state."""
        project = _make_project(integration_branch="my-branch")
        assert _shared_state.get_project_integration_branch(project) == "my-branch"

    def test_two_projects_resolve_independently(self):
        """Two projects with different column values each get their own value."""
        project_a = _make_project(integration_branch="trunk-a")
        project_b = _make_project(integration_branch="trunk-b")
        assert _shared_state.get_project_integration_branch(project_a) == "trunk-a"
        assert _shared_state.get_project_integration_branch(project_b) == "trunk-b"

    def test_project_with_override_and_project_without_are_independent(self):
        """A configured project is unaffected by another project's unset column."""
        project_with = _make_project(integration_branch="custom-trunk")
        project_without = _make_project(integration_branch=None, is_self_repo=False)
        assert (
            _shared_state.get_project_integration_branch(project_with) == "custom-trunk"
        )
        assert _shared_state.get_project_integration_branch(project_without) == "main"

    def test_same_branch_as_promotion_target_returns_configured_value(self):
        """The resolver returns the configured value even when it equals promotion.

        Refusing to land integration work on the promotion target is enforced at
        the merge/rollup boundary; the hot-path resolver must not raise.
        """
        project = _make_project(integration_branch="release", promotion_target="release")
        assert _shared_state.get_project_integration_branch(project) == "release"


# ── _verify_capability_backing_paths: advisory capability verification ────────


class TestVerifyCapabilityBackingPaths:
    """The strategic payload annotates each capability with advisory backing-
    path verification, never mutating the durable problem_model.

    classify_existing_paths is patched per-test so the cases are deterministic
    and don't depend on the integration-branch file tree.
    """

    @pytest.mark.asyncio
    async def test_empty_pm_returns_empty(self):
        from app.strategic_spawning import _verify_capability_backing_paths

        assert await _verify_capability_backing_paths(None) == {}
        assert await _verify_capability_backing_paths({}) == {}

    @pytest.mark.asyncio
    async def test_no_backing_paths_is_unverified(self):
        from app import strategic_spawning

        pm = {"capabilities": [{"name": "CapA", "status": "working"}]}
        # classify must never be called when there are no paths to check.
        with patch.object(
            strategic_spawning,
            "classify_existing_paths",
            new=AsyncMock(side_effect=AssertionError("should not be called")),
        ):
            out = await strategic_spawning._verify_capability_backing_paths(pm)
        ver = out["capabilities"][0]["verification"]
        assert ver["effective_status"] == "unverified"
        assert ver["present"] == [] and ver["missing"] == []

    @pytest.mark.asyncio
    async def test_all_present_preserves_status(self):
        from app import strategic_spawning

        pm = {
            "capabilities": [
                {
                    "name": "CapA",
                    "status": "working",
                    "backing_paths": ["a.py", "b.py"],
                }
            ]
        }
        with patch.object(
            strategic_spawning,
            "classify_existing_paths",
            new=AsyncMock(return_value=(["a.py", "b.py"], [])),
        ):
            out = await strategic_spawning._verify_capability_backing_paths(pm)
        ver = out["capabilities"][0]["verification"]
        assert ver["effective_status"] == "working"
        assert ver["present"] == ["a.py", "b.py"]
        assert ver["missing"] == []

    @pytest.mark.asyncio
    async def test_mixed_presence_is_partial(self):
        from app import strategic_spawning

        pm = {
            "capabilities": [
                {
                    "name": "CapA",
                    "status": "working",
                    "backing_paths": ["a.py", "gone.py"],
                }
            ]
        }
        with patch.object(
            strategic_spawning,
            "classify_existing_paths",
            new=AsyncMock(return_value=(["a.py"], ["gone.py"])),
        ):
            out = await strategic_spawning._verify_capability_backing_paths(pm)
        ver = out["capabilities"][0]["verification"]
        assert ver["effective_status"] == "partial"
        assert ver["present"] == ["a.py"]
        assert ver["missing"] == ["gone.py"]

    @pytest.mark.asyncio
    async def test_all_missing_is_absent(self):
        from app import strategic_spawning

        pm = {
            "capabilities": [
                {
                    "name": "CapA",
                    "status": "working",
                    "backing_paths": ["gone1.py", "gone2.py"],
                }
            ]
        }
        with patch.object(
            strategic_spawning,
            "classify_existing_paths",
            new=AsyncMock(return_value=([], ["gone1.py", "gone2.py"])),
        ):
            out = await strategic_spawning._verify_capability_backing_paths(pm)
        ver = out["capabilities"][0]["verification"]
        assert ver["effective_status"] == "absent"
        assert ver["present"] == []
        assert ver["missing"] == ["gone1.py", "gone2.py"]

    @pytest.mark.asyncio
    async def test_does_not_mutate_input(self):
        """The durable problem_model dict passed in is never mutated — only a
        copy is annotated."""
        from app import strategic_spawning

        pm = {
            "capabilities": [
                {
                    "name": "CapA",
                    "status": "working",
                    "backing_paths": ["a.py"],
                }
            ]
        }
        with patch.object(
            strategic_spawning,
            "classify_existing_paths",
            new=AsyncMock(return_value=(["a.py"], [])),
        ):
            out = await strategic_spawning._verify_capability_backing_paths(pm)
        # Input capability is untouched (no verification key added in place).
        assert "verification" not in pm["capabilities"][0]
        assert out is not pm
        assert out["capabilities"][0] is not pm["capabilities"][0]

    @pytest.mark.asyncio
    async def test_passes_repo_branch_context_to_classifier(self):
        from app import strategic_spawning

        seen_kwargs = {}
        pm = {
            "capabilities": [
                {
                    "name": "CapA",
                    "status": "working",
                    "backing_paths": ["a.py"],
                }
            ]
        }

        async def _classify(paths, **kwargs):
            seen_kwargs.update(kwargs)
            return (paths, [])

        with patch.object(
            strategic_spawning,
            "classify_existing_paths",
            new=AsyncMock(side_effect=_classify),
        ):
            out = await strategic_spawning._verify_capability_backing_paths(
                pm,
                token="ghs_test",
                owner="acme",
                repo="widget",
                branch="automain",
                project_id="proj-123",
            )
        assert out["capabilities"][0]["verification"]["effective_status"] == "working"
        assert seen_kwargs == {
            "token": "ghs_test",
            "owner": "acme",
            "repo": "widget",
            "branch": "automain",
            "project_id": "proj-123",
        }


# ── Onboarding goal reaches the first strategic cycle's payload ─────────────────
#
# The New Project onboarding flow seeds the user's goal into BOTH channels the
# strategic agent's bootstrap reads on cycle 1: problem_model.intent (purpose +
# current_focus) and a from_type='user' ProjectMessage that lands in the
# conversation thread. These tests lock in that the seeded intent and message
# actually reach the spawned strategic-cycle payloads, and that an empty goal
# leaves today's blank-slate behavior intact.

_ONBOARD_GOAL = "Ship a self-serve billing portal for small teams"


def _seeded_problem_model(goal: str) -> dict:
    """Build a problem_model dict shaped exactly like onboarding's seed.

    Mirrors admin_onboard._seed_strategic_intent: purpose + current_focus set
    to the goal with a user_message provenance source, validated through the
    real model so the test exercises the same payload the agent receives.
    """
    from app.problem_model import parse_problem_model, Source

    pm = parse_problem_model(None)
    pm.intent.purpose = goal
    pm.intent.current_focus = goal
    pm.intent.source = Source(type="user_message", excerpt=goal[:120])
    pm.last_updated_by = "user"
    return pm.model_dump()


@pytest.mark.asyncio
async def test_seeded_goal_reaches_strategic_review_payload(db_session):
    """Goal seeded at onboarding is visible to the first strategic_review cycle.

    Asserts both bootstrap channels carry it: problem_model.intent.purpose and
    the from_type='user' message in conversation_thread + open_messages.
    """
    project = _make_project(
        vision={},  # blank-slate vision, as a freshly-onboarded project has
        problem_model=_seeded_problem_model(_ONBOARD_GOAL),
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(
        ProjectMessage(
            project_id=project.id, from_type="user", content=_ONBOARD_GOAL
        )
    )
    await db_session.commit()

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    payload = result.scalars().all()[0].payloads[0]

    # Channel 1: intent/purpose
    assert payload["problem_model"]["intent"]["purpose"] == _ONBOARD_GOAL
    # Channel 2: user-message conversation history
    thread_contents = [m["content"] for m in payload["conversation_thread"]]
    assert _ONBOARD_GOAL in thread_contents
    open_contents = [m["content"] for m in payload["open_messages"]]
    assert _ONBOARD_GOAL in open_contents


@pytest.mark.asyncio
async def test_seeded_goal_reaches_design_doc_update_payload(db_session):
    """Goal seeded at onboarding is visible to the Phase-1 design_doc_update cycle."""
    project = _make_project(
        vision={},
        problem_model=_seeded_problem_model(_ONBOARD_GOAL),
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(
        ProjectMessage(
            project_id=project.id, from_type="user", content=_ONBOARD_GOAL
        )
    )
    await db_session.commit()

    spawned = await _spawn_design_doc_update(project, db_session)
    assert spawned is True

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    payload = result.scalars().all()[0].payloads[0]

    assert payload["problem_model"]["intent"]["purpose"] == _ONBOARD_GOAL
    open_contents = [m["content"] for m in payload["open_messages"]]
    assert _ONBOARD_GOAL in open_contents


@pytest.mark.asyncio
async def test_empty_goal_leaves_strategic_payload_blank(db_session):
    """A skipped goal preserves today's blank-slate behavior — no seeded
    purpose and no seeded user message reach the strategic payload."""
    project = _make_project(vision={}, problem_model={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    payload = result.scalars().all()[0].payloads[0]

    # No purpose was seeded (empty/absent problem_model intent).
    assert not (payload.get("problem_model") or {}).get("intent", {}).get("purpose")
    assert payload["conversation_thread"] == []
    assert payload["open_messages"] == []


class TestParallelismHint:
    """_build_parallelism_hint surfaces the direction-count -> parallelism cost."""

    def test_healthy_lane_count_returns_empty(self):
        from app.strategic_spawning import _build_parallelism_hint

        # 3 active directions with capacity for more — no nag.
        assert _build_parallelism_hint(3, max_concurrent_vms=8, max_active_directions=10) == ""
        assert _build_parallelism_hint(5, max_concurrent_vms=8, max_active_directions=10) == ""

    def test_below_floor_emits_cost_framed_hint(self):
        from app.strategic_spawning import _build_parallelism_hint

        hint = _build_parallelism_hint(1, max_concurrent_vms=8, max_active_directions=10)
        assert hint  # non-empty
        assert "PARALLELISM" in hint
        assert "1 active direction" in hint
        # Surfaces the idle fleet capacity and stays quality-framed (not a quota).
        assert "8 VMs" in hint
        assert "worthwhile parallel work" in hint
        assert "load-bearing" in hint

    def test_capacity_is_the_limit_does_not_nag(self):
        from app.strategic_spawning import _build_parallelism_hint

        # Fleet can only run 2 VMs, so 2 active directions is NOT underutilized.
        assert _build_parallelism_hint(2, max_concurrent_vms=2, max_active_directions=10) == ""
        # But 1 active on a 2-VM fleet still leaves a lane idle.
        assert _build_parallelism_hint(1, max_concurrent_vms=2, max_active_directions=10)

    def test_unknown_capacity_omits_vm_clause(self):
        from app.strategic_spawning import _build_parallelism_hint

        hint = _build_parallelism_hint(1, max_concurrent_vms=0, max_active_directions=10)
        assert hint
        assert "VMs at once" not in hint  # no concrete VM count when capacity unknown


# ── Deploy-model framing (integration-branch landing ≠ deployment) ────────────


async def _spawn_strategic_review_payload(project, session) -> dict:
    """Run a strategy-only cycle and return the spawned strategic_review payload."""
    old = _shared_state.runtime_config.get("architecture_review_frequency", 3)
    _shared_state.runtime_config["architecture_review_frequency"] = 3
    try:
        await _spawn_research_task(project, session)
    finally:
        _shared_state.runtime_config["architecture_review_frequency"] = old
    result = await session.execute(select(Task).where(Task.project_id == project.id))
    tasks = result.scalars().all()
    assert len(tasks) == 1, f"Expected one task, got {len(tasks)}"
    payload = tasks[0].payloads[0]
    assert payload["type"] == "strategic_review", payload["type"]
    return payload


@pytest.mark.asyncio
async def test_deploy_framing_present_when_integration_and_promotion_differ(db_session):
    """When the integration branch and promotion target differ (the LTS split),
    the strategic payload carries the deploy-model framing so the agent does not
    treat 'landed on the integration branch' as 'deployed/live'."""
    project = _make_project(
        vision={"_strategic_cycle_count": 1, "_research_spawn_count": 5},
        integration_branch="automain",
        promotion_target="main",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    payload = await _spawn_strategic_review_payload(project, db_session)
    suffix = payload.get("prompt_suffix") or ""
    assert "Deployment model" in suffix, "deploy framing missing from prompt_suffix"
    # Both sides of the split are named, using the project's real branch names.
    assert "`automain`" in suffix and "`main`" in suffix
    # The operative warnings that defuse the enable-and-measure / hard-pivot trap.
    assert "not yet PROMOTED" in suffix
    assert "nothing live to measure" in suffix


@pytest.mark.asyncio
async def test_deploy_framing_absent_when_branches_equal(db_session):
    """A single-branch project (integration branch == promotion target) deploys
    merges directly, so the deploy-model framing must NOT appear — it would be
    actively misleading there."""
    project = _make_project(
        vision={"_strategic_cycle_count": 1, "_research_spawn_count": 5},
        integration_branch="main",
        promotion_target="main",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    payload = await _spawn_strategic_review_payload(project, db_session)
    suffix = payload.get("prompt_suffix") or ""
    assert "Deployment model" not in suffix, (
        "deploy framing must be omitted when integration branch == promotion target"
    )


@pytest.mark.asyncio
async def test_deploy_framing_uses_project_branch_names_not_hardcoded(db_session):
    """The framing is project-generic: it interpolates the project's own
    integration/promotion branch names and never hardcodes Blueprint's
    'automain'/'main' literals."""
    project = _make_project(
        vision={"_strategic_cycle_count": 1, "_research_spawn_count": 5},
        integration_branch="staging",
        promotion_target="release",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    payload = await _spawn_strategic_review_payload(project, db_session)
    suffix = payload.get("prompt_suffix") or ""
    assert "Deployment model" in suffix
    assert "`staging`" in suffix and "`release`" in suffix
    # No Blueprint-specific branch literals leak into a non-Blueprint project.
    assert "automain" not in suffix
    assert "`main`" not in suffix
