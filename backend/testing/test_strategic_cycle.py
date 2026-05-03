"""Tests for the StrategicCycle table + helper module + 3-way vision merge.

Audit 2026-04-25 W2 — backstop coverage for cycle promotion from JSONB to
real table.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlmodel import select

from app import strategic_cycle, vision_merge
from app.models import Project, StrategicCycle, Task, TaskStatus
from app.time_utils import _utcnow


# --- helpers ---------------------------------------------------------------

async def _make_project(session, name: str = "W2 Test Project") -> Project:
    p = Project(name=name, user_id="test-user-w2")
    session.add(p)
    await session.commit()
    await session.refresh(p)
    return p


# --- strategic_cycle helper ------------------------------------------------


@pytest.mark.asyncio
async def test_create_inserts_row_with_baseline(db_session):
    project = await _make_project(db_session)
    baseline = {"purpose": "ship pipeline", "systems": [{"id": "s1", "status": "active"}]}
    cycle = await strategic_cycle.create(
        db_session, project_id=project.id, baseline_vision=baseline,
    )
    await db_session.commit()
    fetched = await strategic_cycle.get(db_session, cycle.id)
    assert fetched is not None
    assert fetched.outcome == strategic_cycle.OUTCOME_PHASE1_RUNNING
    assert fetched.phase1_started_at is not None
    assert strategic_cycle.baseline_vision(fetched) == baseline


@pytest.mark.asyncio
async def test_mark_phase1_completed_with_phase2_spawn(db_session):
    project = await _make_project(db_session)
    phase1 = Task(
        project_id=project.id, payloads=[{"type": "design_doc_update"}],
        status=TaskStatus.COMPLETED,
    )
    phase2 = Task(
        project_id=project.id, payloads=[{"type": "strategic_review"}],
        status=TaskStatus.QUEUED,
    )
    db_session.add_all([phase1, phase2])
    await db_session.commit()
    await db_session.refresh(phase1)
    await db_session.refresh(phase2)

    cycle = await strategic_cycle.create(
        db_session, project_id=project.id, phase1_task_id=phase1.id,
    )
    await db_session.commit()

    updated = await strategic_cycle.mark_phase1_completed(
        db_session, cycle.id, phase2_task_id=phase2.id,
    )
    await db_session.commit()
    assert updated is not None
    assert updated.outcome == strategic_cycle.OUTCOME_PHASE2_RUNNING
    assert updated.phase1_completed_at is not None
    assert updated.phase2_task_id == phase2.id
    assert updated.phase2_started_at is not None


@pytest.mark.asyncio
async def test_mark_failure_records_reason(db_session):
    project = await _make_project(db_session)
    cycle = await strategic_cycle.create(db_session, project_id=project.id)
    await db_session.commit()
    updated = await strategic_cycle.mark_failure(
        db_session, cycle.id, phase="phase1", reason="task crashed mid-run",
    )
    await db_session.commit()
    assert updated.outcome == strategic_cycle.OUTCOME_FAILED_PHASE1
    assert updated.failure_reason == "task crashed mid-run"
    assert updated.phase1_completed_at is not None


@pytest.mark.asyncio
async def test_get_by_task_finds_cycle(db_session):
    project = await _make_project(db_session)
    phase1 = Task(project_id=project.id, payloads=[{"type": "design_doc_update"}])
    db_session.add(phase1)
    await db_session.commit()
    await db_session.refresh(phase1)
    cycle = await strategic_cycle.create(
        db_session, project_id=project.id, phase1_task_id=phase1.id,
    )
    await db_session.commit()
    found = await strategic_cycle.get_by_task(db_session, phase1.id)
    assert found is not None and found.id == cycle.id


@pytest.mark.asyncio
async def test_recent_for_agent_context_returns_legacy_shape(db_session):
    project = await _make_project(db_session)
    for _ in range(3):
        await strategic_cycle.create(db_session, project_id=project.id)
        await db_session.commit()
    history = await strategic_cycle.recent_for_agent_context(db_session, project.id, limit=5)
    assert len(history) == 3
    # Shape mirrors the legacy JSONB cycle_history entry — has cycle_id, outcome, etc.
    for entry in history:
        assert "cycle_id" in entry
        assert "outcome" in entry
        assert "started_at" in entry


@pytest.mark.asyncio
async def test_find_stalled_returns_phase1_old_cycle(db_session):
    project = await _make_project(db_session)
    cycle = await strategic_cycle.create(db_session, project_id=project.id)
    # Backdate phase1_started_at to 4h ago
    cycle.phase1_started_at = _utcnow() - timedelta(hours=4)
    db_session.add(cycle)
    await db_session.commit()
    stalled = await strategic_cycle.find_stalled(db_session)
    assert any(c.id == cycle.id for c in stalled)


@pytest.mark.asyncio
async def test_find_stalled_skips_recently_started(db_session):
    project = await _make_project(db_session)
    await strategic_cycle.create(db_session, project_id=project.id)
    await db_session.commit()
    stalled = await strategic_cycle.find_stalled(db_session)
    assert stalled == []


# --- vision_merge ----------------------------------------------------------


def test_merge_vision_takes_agent_when_user_unchanged():
    baseline = {"purpose": "old", "systems": [{"id": "s1", "name": "A"}]}
    agent = {"purpose": "new", "systems": [{"id": "s1", "name": "A"}]}
    current = {"purpose": "old", "systems": [{"id": "s1", "name": "A"}]}
    merged, rejected = vision_merge.merge_vision(baseline, agent, current)
    assert merged["purpose"] == "new"
    assert rejected == []


def test_merge_vision_keeps_user_on_conflict_logs_rejection():
    baseline = {"purpose": "old"}
    agent = {"purpose": "agent_proposed"}
    current = {"purpose": "user_edited"}
    merged, rejected = vision_merge.merge_vision(baseline, agent, current)
    assert merged["purpose"] == "user_edited"
    assert len(rejected) == 1
    assert rejected[0]["field"] == "vision.purpose"
    assert rejected[0]["kind"] == "value_conflict"
    assert rejected[0]["agent_value"] == "agent_proposed"


def test_merge_vision_user_unchanged_takes_agent_for_lists():
    baseline = {"systems": [{"id": "s1", "name": "Auth", "status": "planned"}]}
    agent = {"systems": [
        {"id": "s1", "name": "Auth", "status": "active"},
        {"id": "s2", "name": "Billing", "status": "planned"},
    ]}
    current = {"systems": [{"id": "s1", "name": "Auth", "status": "planned"}]}
    merged, rejected = vision_merge.merge_vision(baseline, agent, current)
    sys_by_id = {s["id"]: s for s in merged["systems"]}
    assert sys_by_id["s1"]["status"] == "active"  # agent updated
    assert "s2" in sys_by_id  # agent added
    assert rejected == []


def test_merge_vision_user_removed_item_agent_re_add_rejected():
    baseline = {"systems": [{"id": "s1", "name": "Auth"}]}
    agent = {"systems": [{"id": "s1", "name": "Auth", "status": "active"}]}
    current = {"systems": []}  # user removed s1
    merged, rejected = vision_merge.merge_vision(baseline, agent, current)
    assert merged["systems"] == []
    assert len(rejected) == 1
    assert rejected[0]["kind"] == "user_removed_agent_kept"


def test_merge_vision_agent_only_change_takes_agent():
    baseline = {"context": "small project"}
    agent = {"context": "small project, added auth in q2"}
    current = {"context": "small project"}
    merged, rejected = vision_merge.merge_vision(baseline, agent, current)
    assert merged["context"] == "small project, added auth in q2"
    assert rejected == []


def test_append_rejected_edits_caps_list():
    vision = {vision_merge.REJECTED_EDITS_KEY: [{"x": i} for i in range(50)]}
    new = [{"x": 100}, {"x": 101}]
    vision_merge.append_rejected_edits(vision, new)
    assert len(vision[vision_merge.REJECTED_EDITS_KEY]) == vision_merge.REJECTED_EDITS_MAX
    # Newest at the end.
    assert vision[vision_merge.REJECTED_EDITS_KEY][-1] == {"x": 101}


# --- end-to-end: cycle row updates through spawn + completion --------------


@pytest.mark.asyncio
async def test_cycle_row_created_on_phase1_spawn(db_session, mocker):
    """Spawning a strategic cycle creates a StrategicCycle row, sets the
    Task.strategic_cycle_id column on the spawned phase 1 task, and snapshots
    the project's vision into cycle_metadata.baseline_vision."""
    from app.strategic_spawning import _spawn_design_doc_update
    project = await _make_project(db_session)
    project.vision = {"purpose": "test purpose", "revision": 1}
    db_session.add(project)
    await db_session.commit()

    spawned = await _spawn_design_doc_update(project, db_session)
    assert spawned is True

    # Verify cycle row exists
    cycles = (await db_session.execute(
        select(StrategicCycle).where(StrategicCycle.project_id == project.id)
    )).scalars().all()
    assert len(cycles) == 1
    cycle = cycles[0]
    assert cycle.outcome == strategic_cycle.OUTCOME_PHASE1_RUNNING
    assert cycle.phase1_task_id is not None

    # Verify Task.strategic_cycle_id column populated
    task = await db_session.get(Task, cycle.phase1_task_id)
    assert task.strategic_cycle_id == cycle.id

    # Verify baseline_vision snapshot
    assert strategic_cycle.baseline_vision(cycle).get("purpose") == "test purpose"
