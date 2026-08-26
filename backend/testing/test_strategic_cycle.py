"""Tests for the StrategicCycle table + helper module + 3-way vision merge.

Audit 2026-04-25 W2 — backstop coverage for cycle promotion from JSONB to
real table.
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from fastapi import BackgroundTasks, HTTPException
from sqlmodel import select

from app import pause_clock, strategic_cycle, vision_merge
from app.models import (
    CYCLE_META_INTEGRITY_CONFLICT_COUNTS,
    DeploymentState,
    Project,
    StrategicCycle,
    Task,
    TaskStatus,
)
from app.time_utils import _utcnow
from app.workbench_state import (
    STRATEGIC_LOCAL_RUNNER_HOLD_CONTEXT_KEY,
    STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY,
)


# --- helpers ---------------------------------------------------------------


async def _make_project(session, name: str = "W2 Test Project") -> Project:
    p = Project(name=name, user_id="test-user-w2")
    session.add(p)
    await session.commit()
    await session.refresh(p)
    return p


# --- strategic_cycle helper ------------------------------------------------


@pytest.mark.asyncio
async def test_terminal_event_without_completion_still_broadcasts(monkeypatch):
    """Quiet operator stops still close the client's live cycle indicator."""
    emit = AsyncMock()
    monkeypatch.setattr(
        "app.strategic_events.emit_strategic_cycle_completed",
        emit,
    )
    event = {
        "project_id": "project-1",
        "task_id": "task-1",
        "phase": 2,
        "cycle_id": "cycle-1",
        "phase_state": "failed_phase2",
    }

    await strategic_cycle.emit_terminal_completion_events([event])

    emit.assert_awaited_once_with(
        "project-1",
        "task-1",
        2,
        cycle_id="cycle-1",
        phase_state="failed_phase2",
        completion=None,
    )


@pytest.mark.asyncio
async def test_create_inserts_row_with_baseline(db_session):
    project = await _make_project(db_session)
    baseline = {
        "purpose": "ship pipeline",
        "systems": [{"id": "s1", "status": "active"}],
    }
    cycle = await strategic_cycle.create(
        db_session,
        project_id=project.id,
        baseline_vision=baseline,
    )
    await db_session.commit()
    fetched = await strategic_cycle.get(db_session, cycle.id)
    assert fetched is not None
    assert fetched.outcome == strategic_cycle.OUTCOME_PHASE1_RUNNING
    assert fetched.phase1_started_at is not None
    assert strategic_cycle.baseline_vision(fetched) == baseline


@pytest.mark.asyncio
async def test_create_snapshots_baseline_problem_model(db_session):
    """The problem-model snapshot is captured alongside the vision baseline so
    the 3-way operator-wins merge can compare the agent's write against it."""
    project = await _make_project(db_session)
    baseline_pm = {
        "revision": 4,
        "intent": {"purpose": "Ship billing", "current_focus": "invoicing"},
        "constraints": ["No PII in logs"],
    }
    cycle = await strategic_cycle.create(
        db_session,
        project_id=project.id,
        baseline_vision={"purpose": "x"},
        baseline_problem_model=baseline_pm,
    )
    await db_session.commit()
    fetched = await strategic_cycle.get(db_session, cycle.id)
    assert strategic_cycle.baseline_problem_model(fetched) == baseline_pm
    # The vision baseline is unaffected by the new key.
    assert strategic_cycle.baseline_vision(fetched) == {"purpose": "x"}


@pytest.mark.asyncio
async def test_baseline_problem_model_defaults_empty(db_session):
    """A cycle created without a problem-model baseline (e.g. legacy spawn)
    reads back an empty dict, so apply_agent_update degrades to None safely."""
    project = await _make_project(db_session)
    cycle = await strategic_cycle.create(
        db_session,
        project_id=project.id,
        baseline_vision={"purpose": "x"},
    )
    await db_session.commit()
    fetched = await strategic_cycle.get(db_session, cycle.id)
    assert strategic_cycle.baseline_problem_model(fetched) == {}


@pytest.mark.asyncio
async def test_create_snapshots_baseline_running_update(db_session):
    """The running_update snapshot is captured at cycle start so the
    operator-wins reconciliation can compare the agent's write against it."""
    project = await _make_project(db_session)
    cycle = await strategic_cycle.create(
        db_session,
        project_id=project.id,
        baseline_running_update="## Current state\nShipping v4.",
    )
    await db_session.commit()
    fetched = await strategic_cycle.get(db_session, cycle.id)
    assert (
        strategic_cycle.baseline_running_update(fetched)
        == "## Current state\nShipping v4."
    )


@pytest.mark.asyncio
async def test_baseline_running_update_empty_when_none_at_spawn(db_session):
    """A cycle whose running_update was empty at spawn reads back as ``""``
    (a captured-but-empty baseline), distinct from a legacy cycle's ``None``."""
    project = await _make_project(db_session)
    cycle = await strategic_cycle.create(db_session, project_id=project.id)
    await db_session.commit()
    fetched = await strategic_cycle.get(db_session, cycle.id)
    assert strategic_cycle.baseline_running_update(fetched) == ""


def test_baseline_running_update_none_for_legacy_cycle():
    """A legacy cycle row predating the snapshot (no key in cycle_metadata)
    reports ``None`` so the reconciliation degrades to blind-assign."""
    cycle = StrategicCycle(
        id="sc-legacy-ru",
        project_id="p-1",
        outcome=strategic_cycle.OUTCOME_COMPLETED,
        phase1_started_at=_utcnow(),
        cycle_metadata={"baseline_vision": {"purpose": "x"}},
    )
    assert strategic_cycle.baseline_running_update(cycle) is None


def test_to_legacy_dict_omits_baseline_problem_model():
    """The problem-model snapshot must never leak into the agent-facing
    cycle-history shape, exactly as baseline_vision is hidden."""
    cycle = StrategicCycle(
        id="sc-test1",
        project_id="p-1",
        outcome=strategic_cycle.OUTCOME_COMPLETED,
        phase1_started_at=_utcnow(),
        cycle_metadata={
            "baseline_vision": {"purpose": "secret"},
            "baseline_problem_model": {"intent": {"purpose": "also secret"}},
            "baseline_running_update": "secret running update doc",
            "memos_cited": 2,
        },
    )
    out = strategic_cycle._to_legacy_dict(cycle)
    assert "baseline_vision" not in out
    assert "baseline_problem_model" not in out
    assert "baseline_running_update" not in out
    # Non-baseline metadata is still spread back in.
    assert out["memos_cited"] == 2


@pytest.mark.asyncio
async def test_agent_problem_model_proposal_roundtrip(db_session):
    """The agent's per-cycle pre-merge proposal stores and reads back; absent
    until the first write (so callers fall back to the cycle-start snapshot)."""
    project = await _make_project(db_session)
    cycle = await strategic_cycle.create(db_session, project_id=project.id)
    await db_session.commit()
    fetched = await strategic_cycle.get(db_session, cycle.id)
    # No write yet → None (callers fall back to baseline_problem_model snapshot).
    assert strategic_cycle.agent_problem_model_proposal(fetched) is None

    subset = {
        "intent": {"purpose": "P"},
        "context": {"project_type": "startup"},
        "constraints": ["c1"],
    }
    strategic_cycle.set_agent_problem_model_proposal(fetched, subset)
    db_session.add(fetched)
    await db_session.commit()
    refetched = await strategic_cycle.get(db_session, cycle.id)
    assert strategic_cycle.agent_problem_model_proposal(refetched) == subset


@pytest.mark.asyncio
async def test_agent_vision_proposal_roundtrip(db_session):
    """The agent's per-cycle pre-merge vision proposal stores and reads back;
    absent until the first write (so callers fall back to baseline_vision)."""
    project = await _make_project(db_session)
    cycle = await strategic_cycle.create(db_session, project_id=project.id)
    await db_session.commit()
    fetched = await strategic_cycle.get(db_session, cycle.id)
    # No write yet → None (callers fall back to baseline_vision snapshot).
    assert strategic_cycle.agent_vision_proposal(fetched) is None

    vision = {"purpose": "P", "systems": [{"id": "s1", "name": "Core"}]}
    strategic_cycle.set_agent_vision_proposal(fetched, vision)
    db_session.add(fetched)
    await db_session.commit()
    refetched = await strategic_cycle.get(db_session, cycle.id)
    assert strategic_cycle.agent_vision_proposal(refetched) == vision


def test_to_legacy_dict_omits_agent_vision_proposal():
    """The per-cycle agent vision proposal must never leak into the agent-facing
    cycle-history shape, exactly as the baseline snapshots are hidden."""
    cycle = StrategicCycle(
        id="sc-test-vprop",
        project_id="p-1",
        outcome=strategic_cycle.OUTCOME_COMPLETED,
        phase1_started_at=_utcnow(),
        cycle_metadata={
            strategic_cycle.AGENT_VISION_PROPOSAL_KEY: {"purpose": "secret"},
            "memos_cited": 1,
        },
    )
    out = strategic_cycle._to_legacy_dict(cycle)
    assert strategic_cycle.AGENT_VISION_PROPOSAL_KEY not in out
    assert out["memos_cited"] == 1


def test_to_legacy_dict_omits_agent_problem_model_proposal():
    """The per-cycle agent proposal must never leak into the agent-facing
    cycle-history shape, exactly as the baseline snapshots are hidden."""
    cycle = StrategicCycle(
        id="sc-test-prop",
        project_id="p-1",
        outcome=strategic_cycle.OUTCOME_COMPLETED,
        phase1_started_at=_utcnow(),
        cycle_metadata={
            strategic_cycle.AGENT_PM_PROPOSAL_KEY: {"intent": {"purpose": "secret"}},
            "memos_cited": 1,
        },
    )
    out = strategic_cycle._to_legacy_dict(cycle)
    assert strategic_cycle.AGENT_PM_PROPOSAL_KEY not in out
    assert out["memos_cited"] == 1


@pytest.mark.asyncio
async def test_advance_running_update_baseline_updates_snapshot(db_session):
    """After an agent write the baseline advances to the persisted document so
    a subsequent same-cycle write reconciles against it, not the cycle-start
    snapshot."""
    project = await _make_project(db_session)
    cycle = await strategic_cycle.create(
        db_session,
        project_id=project.id,
        baseline_running_update="## v1",
    )
    await db_session.commit()
    strategic_cycle.advance_running_update_baseline(cycle, "## v2 (agent doc A)")
    db_session.add(cycle)
    await db_session.commit()
    fetched = await strategic_cycle.get(db_session, cycle.id)
    assert strategic_cycle.baseline_running_update(fetched) == "## v2 (agent doc A)"


def test_advance_running_update_baseline_noop_for_legacy_cycle():
    """A legacy cycle (no baseline_running_update key) is left untouched — the
    advance must not silently flip it onto the reconciliation path mid-cycle."""
    cycle = StrategicCycle(
        id="sc-legacy-adv",
        project_id="p-1",
        outcome=strategic_cycle.OUTCOME_PHASE2_RUNNING,
        phase1_started_at=_utcnow(),
        cycle_metadata={"baseline_vision": {"purpose": "x"}},
    )
    strategic_cycle.advance_running_update_baseline(cycle, "ignored doc")
    assert "baseline_running_update" not in (cycle.cycle_metadata or {})
    assert strategic_cycle.baseline_running_update(cycle) is None


def test_advance_running_update_baseline_handles_empty_baseline():
    """A captured-but-empty baseline (``""``) is a real snapshot, so it advances
    to the agent's document on the first write."""
    cycle = StrategicCycle(
        id="sc-empty-adv",
        project_id="p-1",
        outcome=strategic_cycle.OUTCOME_PHASE2_RUNNING,
        phase1_started_at=_utcnow(),
        cycle_metadata={"baseline_running_update": ""},
    )
    strategic_cycle.advance_running_update_baseline(cycle, "## first agent doc")
    assert strategic_cycle.baseline_running_update(cycle) == "## first agent doc"


@pytest.mark.asyncio
async def test_mark_phase1_completed_with_phase2_spawn(db_session):
    project = await _make_project(db_session)
    phase1 = Task(
        project_id=project.id,
        payloads=[{"type": "design_doc_update"}],
        status=TaskStatus.COMPLETED,
    )
    phase2 = Task(
        project_id=project.id,
        payloads=[{"type": "strategic_review"}],
        status=TaskStatus.QUEUED,
    )
    db_session.add_all([phase1, phase2])
    await db_session.commit()
    await db_session.refresh(phase1)
    await db_session.refresh(phase2)

    cycle = await strategic_cycle.create(
        db_session,
        project_id=project.id,
        phase1_task_id=phase1.id,
    )
    await db_session.commit()

    updated = await strategic_cycle.mark_phase1_completed(
        db_session,
        cycle.id,
        phase2_task_id=phase2.id,
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
        db_session,
        cycle.id,
        phase="phase1",
        reason="task crashed mid-run",
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
        db_session,
        project_id=project.id,
        phase1_task_id=phase1.id,
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
    history = await strategic_cycle.recent_for_agent_context(
        db_session, project.id, limit=5
    )
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


@pytest.mark.asyncio
async def test_find_stalled_skips_strategic_local_runner_holds(db_session):
    project = await _make_project(db_session)
    task = Task(
        project_id=project.id,
        title="Strategic review waiting for local runner",
        payloads=[{"type": "strategic_review"}],
        status=TaskStatus.QUEUED,
        context={
            STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY: True,
            STRATEGIC_LOCAL_RUNNER_HOLD_CONTEXT_KEY: (
                _utcnow() - timedelta(hours=4)
            ).isoformat(),
        },
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    cycle = StrategicCycle(
        id="sc-local-hold",
        project_id=project.id,
        outcome=strategic_cycle.OUTCOME_PHASE2_RUNNING,
        phase1_started_at=_utcnow() - timedelta(hours=5),
        phase1_completed_at=_utcnow() - timedelta(hours=4),
        phase2_started_at=_utcnow() - timedelta(hours=4),
        phase2_task_id=task.id,
    )
    db_session.add(cycle)
    await db_session.commit()

    stalled = await strategic_cycle.find_stalled(db_session)

    assert all(c.id != cycle.id for c in stalled)


@pytest.mark.asyncio
async def test_find_stalled_excludes_open_pause_window(db_session):
    """Mutation-proof: a cycle 4h old ONLY because the pipeline is paused is
    reported stalled by the raw (deploy_state=None) path but NOT when the paused
    deploy state is threaded — the open pause window is subtracted from its age.
    """
    project = await _make_project(db_session)
    cycle = StrategicCycle(
        id="sc-open-pause",
        project_id=project.id,
        outcome=strategic_cycle.OUTCOME_PHASE1_RUNNING,
        phase1_started_at=_utcnow() - timedelta(hours=4),
        cycle_metadata={},  # legacy row — no stamped baseline
    )
    db_session.add(cycle)
    await db_session.commit()

    # Mutation control: without pause accounting the raw 4h age stalls it.
    raw = await strategic_cycle.find_stalled(db_session)
    assert any(c.id == cycle.id for c in raw)

    # Pipeline paused for the whole 4h → active age ~0 → NOT stalled.
    paused_state = DeploymentState(
        id=1,
        total_manual_paused_seconds=0.0,
        total_auto_paused_seconds=0.0,
        paused_at=_utcnow() - timedelta(hours=4),
        paused_kind=pause_clock.PAUSED_KIND_MANUAL,
    )
    paused = await strategic_cycle.find_stalled(db_session, deploy_state=paused_state)
    assert all(c.id != cycle.id for c in paused)


@pytest.mark.asyncio
async def test_find_stalled_resumed_uses_stamped_baseline(db_session):
    """After resume, the stall age excludes the CLOSED pause window via the
    per-phase baseline stamped at phase start. Same 4h-old cycle: with 2h of
    pause it is 2h active → not stalled; with only 0.5h of pause it is 3.5h
    active → still stalled (proving the subtraction doesn't over-protect).
    """
    project = await _make_project(db_session)
    cycle = StrategicCycle(
        id="sc-resumed",
        project_id=project.id,
        outcome=strategic_cycle.OUTCOME_PHASE1_RUNNING,
        phase1_started_at=_utcnow() - timedelta(hours=4),
        cycle_metadata={"phase1_pause_baseline": 0.0},  # started before any pause
    )
    db_session.add(cycle)
    await db_session.commit()

    # 2h of pause accumulated since the baseline → 4h - 2h = 2h active.
    resumed_2h = DeploymentState(
        id=1, total_manual_paused_seconds=2 * 3600.0,
        total_auto_paused_seconds=0.0, paused_at=None, paused_kind=None,
    )
    s2 = await strategic_cycle.find_stalled(db_session, deploy_state=resumed_2h)
    assert all(c.id != cycle.id for c in s2)  # 2h < 3h → not stalled

    # Only 30min of pause → 3.5h active → genuinely stalled (no over-protection).
    resumed_30m = DeploymentState(
        id=1, total_manual_paused_seconds=1800.0,
        total_auto_paused_seconds=0.0, paused_at=None, paused_kind=None,
    )
    s30 = await strategic_cycle.find_stalled(db_session, deploy_state=resumed_30m)
    assert any(c.id == cycle.id for c in s30)  # 3.5h >= 3h → stalled


@pytest.mark.asyncio
async def test_find_stalled_phase2_excludes_open_pause_window(db_session):
    """The same pause-aware age applies to the phase-2 (strategic_review) leg."""
    project = await _make_project(db_session)
    cycle = StrategicCycle(
        id="sc-phase2-pause",
        project_id=project.id,
        outcome=strategic_cycle.OUTCOME_PHASE2_RUNNING,
        phase1_started_at=_utcnow() - timedelta(hours=5),
        phase1_completed_at=_utcnow() - timedelta(hours=4),
        phase2_started_at=_utcnow() - timedelta(hours=4),
        cycle_metadata={},  # legacy — no baseline
    )
    db_session.add(cycle)
    await db_session.commit()

    raw = await strategic_cycle.find_stalled(db_session)
    assert any(c.id == cycle.id for c in raw)

    paused_state = DeploymentState(
        id=1, total_manual_paused_seconds=0.0, total_auto_paused_seconds=0.0,
        paused_at=_utcnow() - timedelta(hours=4), paused_kind=pause_clock.PAUSED_KIND_MANUAL,
    )
    paused = await strategic_cycle.find_stalled(db_session, deploy_state=paused_state)
    assert all(c.id != cycle.id for c in paused)


@pytest.mark.asyncio
async def test_find_stalled_phase2_never_borrows_the_phase1_clock(db_session):
    """A NULL ``phase2_started_at`` is missing evidence, not a 9h-old phase 2.

    ``find_stalled`` used to fall back to ``phase1_started_at``, which reports
    phase 1's age as phase 2's and hands the reaper a >3h verdict it never
    measured. Mutation control: restoring
    ``ref = c.phase2_started_at or c.phase1_started_at`` makes this cycle
    stalled.
    """
    project = await _make_project(db_session)
    cycle = StrategicCycle(
        id="sc-p2-null-start",
        project_id=project.id,
        outcome=strategic_cycle.OUTCOME_PHASE2_RUNNING,
        phase1_started_at=_utcnow() - timedelta(hours=9),
        phase2_started_at=None,
    )
    db_session.add(cycle)
    await db_session.commit()

    stalled = await strategic_cycle.find_stalled(db_session)
    assert all(c.id != cycle.id for c in stalled)


@pytest.mark.asyncio
async def test_repair_missing_phase_starts_prefers_the_linked_task_start(db_session):
    """The phase task is CREATED at phase start, so its created_at is the start."""
    project = await _make_project(db_session)
    task = Task(
        project_id=project.id,
        title="Strategic review",
        payloads=[{"type": "strategic_review"}],
        status=TaskStatus.QUEUED,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    cycle = StrategicCycle(
        id="sc-p2-repair",
        project_id=project.id,
        outcome=strategic_cycle.OUTCOME_PHASE2_RUNNING,
        phase1_started_at=_utcnow() - timedelta(hours=9),
        phase1_completed_at=_utcnow() - timedelta(hours=8),
        phase2_task_id=task.id,
        phase2_started_at=None,
    )
    db_session.add(cycle)
    await db_session.commit()

    repairs = await strategic_cycle.repair_missing_phase_starts(db_session)
    await db_session.commit()
    await db_session.refresh(cycle)

    assert [r["cycle_id"] for r in repairs] == [cycle.id]
    assert repairs[0]["source"] == strategic_cycle.PHASE_START_FROM_LINKED_TASK
    assert cycle.phase2_started_at == task.created_at
    # A pause baseline is stamped with the repair so the pause-aware deadline
    # has one, and historical closed windows are not retroactively credited.
    assert "phase2_pause_baseline" in cycle.cycle_metadata


def test_phase_start_evidence_never_uses_phase1_start_for_phase2():
    """Pure-function pin: phase 1's start is never evidence about phase 2.

    This is the whole defect in one assertion — the fallback that produced the
    ">3h in phase2" claim is a phase-1 timestamp.
    """
    phase1_start = _utcnow() - timedelta(hours=9)
    cycle = StrategicCycle(
        id="sc-evidence",
        project_id=uuid.uuid4(),
        outcome=strategic_cycle.OUTCOME_PHASE2_RUNNING,
        phase1_started_at=phase1_start,
        phase1_completed_at=None,
        phase2_started_at=None,
    )
    now = _utcnow()
    value, source = strategic_cycle.phase_start_evidence(
        cycle, phase="phase2", linked_task=None, now=now
    )
    assert source == strategic_cycle.PHASE_START_FROM_OBSERVATION
    assert value == now
    assert value != phase1_start

    # With a phase-1 completion the phase-2 clock starts there, not at phase-1 start.
    cycle.phase1_completed_at = _utcnow() - timedelta(hours=8)
    value, source = strategic_cycle.phase_start_evidence(
        cycle, phase="phase2", linked_task=None, now=now
    )
    assert source == strategic_cycle.PHASE_START_FROM_PHASE1_COMPLETED
    assert value == cycle.phase1_completed_at


def test_phase_start_evidence_phase1_falls_back_to_the_cycle_row():
    """The phase-1 arm (unreachable via the NOT NULL column) still decides."""
    created = _utcnow() - timedelta(hours=6)
    cycle = StrategicCycle(
        id="sc-evidence-p1",
        project_id=uuid.uuid4(),
        outcome=strategic_cycle.OUTCOME_PHASE1_RUNNING,
        phase1_started_at=None,
        created_at=created,
    )
    value, source = strategic_cycle.phase_start_evidence(
        cycle, phase="phase1", linked_task=None, now=_utcnow()
    )
    assert source == strategic_cycle.PHASE_START_FROM_CYCLE_CREATED
    assert value == created


@pytest.mark.asyncio
async def test_create_stamps_phase1_pause_baseline(db_session):
    """create() stamps a phase-1 pause baseline so the reaper can be pause-aware."""
    project = await _make_project(db_session)
    cycle = await strategic_cycle.create(db_session, project_id=project.id)
    await db_session.commit()
    assert "phase1_pause_baseline" in (cycle.cycle_metadata or {})


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
    agent = {
        "systems": [
            {"id": "s1", "name": "Auth", "status": "active"},
            {"id": "s2", "name": "Billing", "status": "planned"},
        ]
    }
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
    # Use realistic entries with distinct field/kind/agent_value fingerprints.
    existing = [
        {"field": f"f{i}", "kind": "replace", "agent_value": f"v{i}", "at": "t"}
        for i in range(50)
    ]
    vision = {vision_merge.REJECTED_EDITS_KEY: existing}
    new = [
        {"field": "f_new_a", "kind": "replace", "agent_value": "va", "at": "t2"},
        {"field": "f_new_b", "kind": "replace", "agent_value": "vb", "at": "t2"},
    ]
    vision_merge.append_rejected_edits(vision, new)
    assert (
        len(vision[vision_merge.REJECTED_EDITS_KEY]) == vision_merge.REJECTED_EDITS_MAX
    )
    # Newest at the end.
    assert vision[vision_merge.REJECTED_EDITS_KEY][-1] == new[-1]


def test_append_rejected_edits_dedup():
    """Same field/kind/agent_value fingerprint is dropped; distinct fingerprints are kept."""
    dup_entry = {"field": "purpose", "kind": "replace", "agent_value": "old", "at": "t1"}
    vision = {vision_merge.REJECTED_EDITS_KEY: [dup_entry]}
    # A replay of the same logical rejection (same fingerprint, different timestamp).
    replay = {"field": "purpose", "kind": "replace", "agent_value": "old", "at": "t2"}
    # A genuinely new rejection with a different agent_value.
    new_entry = {"field": "purpose", "kind": "replace", "agent_value": "new", "at": "t2"}
    vision_merge.append_rejected_edits(vision, [replay, new_entry])
    result = vision[vision_merge.REJECTED_EDITS_KEY]
    # replay is dropped; new_entry is kept; original is preserved.
    assert len(result) == 2
    assert result[0] == dup_entry
    assert result[1] == new_entry


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
    cycles = (
        (
            await db_session.execute(
                select(StrategicCycle).where(StrategicCycle.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(cycles) == 1
    cycle = cycles[0]
    assert cycle.outcome == strategic_cycle.OUTCOME_PHASE1_RUNNING
    assert cycle.phase1_task_id is not None

    # Verify Task.strategic_cycle_id column populated
    task = await db_session.get(Task, cycle.phase1_task_id)
    assert task.strategic_cycle_id == cycle.id

    # Verify baseline_vision snapshot
    assert strategic_cycle.baseline_vision(cycle).get("purpose") == "test purpose"


@pytest.mark.asyncio
async def test_cycle_row_snapshots_problem_model_on_phase1_spawn(db_session, mocker):
    """Spawning a strategic cycle snapshots project.problem_model into
    cycle_metadata.baseline_problem_model so the completion-time 3-way merge
    can detect a concurrent operator edit."""
    from app.strategic_spawning import _spawn_design_doc_update

    project = await _make_project(db_session)
    project.vision = {"purpose": "test purpose", "revision": 1}
    project.problem_model = {
        "revision": 3,
        "intent": {"purpose": "Operator goal", "current_focus": "phase 1"},
        "constraints": ["No CI changes"],
    }
    db_session.add(project)
    await db_session.commit()

    spawned = await _spawn_design_doc_update(project, db_session)
    assert spawned is True

    cycle = (
        (
            await db_session.execute(
                select(StrategicCycle).where(StrategicCycle.project_id == project.id)
            )
        )
        .scalars()
        .first()
    )
    snap = strategic_cycle.baseline_problem_model(cycle)
    assert snap.get("intent", {}).get("purpose") == "Operator goal"
    assert snap.get("constraints") == ["No CI changes"]


# --- memo exit counting + citation tracking ------------------------------------


@pytest.mark.asyncio
async def test_count_memo_exits_counts_completed_memo_tasks(db_session):
    project = await _make_project(db_session)
    # Two completed memo tasks
    for _ in range(2):
        t = Task(
            project_id=project.id,
            payloads=[{"type": "memo"}],
            status=TaskStatus.COMPLETED,
        )
        db_session.add(t)
    # One completed non-memo task — should not count
    db_session.add(
        Task(
            project_id=project.id,
            payloads=[{"type": "create"}],
            status=TaskStatus.COMPLETED,
        )
    )
    # One in-progress memo task — should not count
    db_session.add(
        Task(
            project_id=project.id,
            payloads=[{"type": "memo"}],
            status=TaskStatus.IN_PROGRESS,
        )
    )
    await db_session.commit()
    count = await strategic_cycle.count_memo_exits(db_session, project.id)
    assert count == 2


def test_count_cited_memos_finds_task_ids_in_text():
    citations = [
        {"task_id": "abc-123", "summary_excerpt": "foo"},
        {"task_id": "def-456", "summary_excerpt": "bar"},
        {"task_id": "ghi-789", "summary_excerpt": "baz"},
    ]
    text = "Based on findings from task abc-123 and def-456, we recommend..."
    assert strategic_cycle.count_cited_memos(citations, text) == 2


def test_count_cited_memos_empty_inputs():
    assert strategic_cycle.count_cited_memos([], "some text") == 0
    assert strategic_cycle.count_cited_memos([{"task_id": "x"}], "") == 0


@pytest.mark.asyncio
async def test_mark_phase2_completed_stores_memo_counts(db_session):
    project = await _make_project(db_session)
    cycle = await strategic_cycle.create(db_session, project_id=project.id)
    await db_session.commit()

    updated = await strategic_cycle.mark_phase2_completed(
        db_session,
        cycle.id,
        memos_available=5,
        memos_cited=2,
    )
    await db_session.commit()
    assert updated is not None
    assert updated.cycle_metadata["memos_available"] == 5
    assert updated.cycle_metadata["memos_cited"] == 2
    assert updated.outcome == strategic_cycle.OUTCOME_COMPLETED


@pytest.mark.asyncio
async def test_mark_phase2_completed_stores_knowledge_counts(db_session):
    project = await _make_project(db_session)
    cycle = await strategic_cycle.create(db_session, project_id=project.id)
    await db_session.commit()

    updated = await strategic_cycle.mark_phase2_completed(
        db_session,
        cycle.id,
        knowledge_available=3,
        knowledge_cited=1,
    )
    await db_session.commit()
    assert updated is not None
    assert updated.cycle_metadata["knowledge_available"] == 3
    assert updated.cycle_metadata["knowledge_cited"] == 1
    assert updated.outcome == strategic_cycle.OUTCOME_COMPLETED


@pytest.mark.asyncio
async def test_mark_phase2_completed_caps_summary_text(db_session):
    """summary_text is stored under the declared CYCLE_SUMMARY_MAX cap, so an
    oversized cycle summary can never bloat the row (mirrors the cycle_reasoning
    cap applied alongside it)."""
    from app.strategic_constants import CYCLE_SUMMARY_MAX

    project = await _make_project(db_session)
    cycle = await strategic_cycle.create(db_session, project_id=project.id)
    await db_session.commit()

    oversized = "x" * (CYCLE_SUMMARY_MAX + 500)
    updated = await strategic_cycle.mark_phase2_completed(
        db_session,
        cycle.id,
        summary_text=oversized,
    )
    await db_session.commit()
    assert updated is not None
    assert updated.summary_text == oversized[:CYCLE_SUMMARY_MAX]
    assert len(updated.summary_text) == CYCLE_SUMMARY_MAX


@pytest.mark.asyncio
async def test_ever_cited_memo_no_cycles(db_session):
    """Project with no cycles returns ever_cited=False."""
    project = await _make_project(db_session)
    result = await strategic_cycle.ever_cited_memo(db_session, project.id)
    assert result == {
        "ever_cited": False,
        "first_cycle_id": None,
        "first_cited_at": None,
    }


@pytest.mark.asyncio
async def test_ever_cited_memo_no_citations(db_session):
    """Completed cycles with memos_cited=0 return ever_cited=False."""
    project = await _make_project(db_session)
    for _ in range(3):
        cycle = await strategic_cycle.create(db_session, project_id=project.id)
        await db_session.commit()
        await strategic_cycle.mark_phase2_completed(
            db_session, cycle.id, memos_available=2, memos_cited=0
        )
        await db_session.commit()

    result = await strategic_cycle.ever_cited_memo(db_session, project.id)
    assert result == {
        "ever_cited": False,
        "first_cycle_id": None,
        "first_cited_at": None,
    }


@pytest.mark.asyncio
async def test_ever_cited_memo_with_citation(db_session):
    """When a cycle has memos_cited > 0, ever_cited=True and returns that cycle."""
    project = await _make_project(db_session)
    # First cycle: no citations
    c1 = await strategic_cycle.create(db_session, project_id=project.id)
    await db_session.commit()
    await strategic_cycle.mark_phase2_completed(
        db_session, c1.id, memos_available=1, memos_cited=0
    )
    await db_session.commit()
    # Second cycle: one citation
    c2 = await strategic_cycle.create(db_session, project_id=project.id)
    await db_session.commit()
    await strategic_cycle.mark_phase2_completed(
        db_session, c2.id, memos_available=2, memos_cited=1
    )
    await db_session.commit()
    await db_session.refresh(c2)

    result = await strategic_cycle.ever_cited_memo(db_session, project.id)
    assert result["ever_cited"] is True
    assert result["first_cycle_id"] == c2.id
    assert result["first_cited_at"] is not None


@pytest.mark.asyncio
async def test_ever_cited_memo_citation_outside_recent_window(db_session):
    """Citation found even when it falls outside the 20-cycle recent window."""
    project = await _make_project(db_session)
    # Create 25 cycles; only cycle 1 has a citation (outside a 20-cycle window)
    first_cycle_id = None
    for i in range(25):
        c = await strategic_cycle.create(db_session, project_id=project.id)
        await db_session.commit()
        cited = 1 if i == 0 else 0
        await strategic_cycle.mark_phase2_completed(
            db_session, c.id, memos_available=1, memos_cited=cited
        )
        await db_session.commit()
        if i == 0:
            first_cycle_id = c.id

    result = await strategic_cycle.ever_cited_memo(db_session, project.id)
    assert result["ever_cited"] is True
    assert result["first_cycle_id"] == first_cycle_id


@pytest.mark.asyncio
async def test_mark_failure_phase2_spawn_records_outcome(db_session):
    """mark_failure(phase='phase2_spawn') records OUTCOME_FAILED_PHASE2_SPAWN on the row.

    This is the outcome the task_completion exception handler writes when the
    Phase-2 strategic_review spawn raises (migration 0047: was a dead JSONB write).
    """
    project = await _make_project(db_session)
    cycle = await strategic_cycle.create(db_session, project_id=project.id)
    await db_session.commit()
    # Simulate phase1 completing before spawn attempt
    await strategic_cycle.mark_phase1_completed(db_session, cycle.id)
    await db_session.commit()

    updated = await strategic_cycle.mark_failure(
        db_session,
        cycle.id,
        phase="phase2_spawn",
        reason="spawn raised: VM limit reached",
    )
    await db_session.commit()

    assert updated is not None
    assert updated.outcome == strategic_cycle.OUTCOME_FAILED_PHASE2_SPAWN
    assert updated.failure_reason == "spawn raised: VM limit reached"
    assert updated.phase1_completed_at is not None
    assert updated.outcome not in strategic_cycle.ACTIVE_OUTCOMES


@pytest.mark.asyncio
async def test_reconcile_terminal_task_reconciles_linked_phase_and_is_idempotent(
    db_session,
):
    """A task's cycle linkage, not its payload, selects the failed phase."""
    project = await _make_project(db_session)
    phase1_task = Task(
        project_id=project.id,
        title="cancelled phase one",
        status=TaskStatus.CANCELLED,
        payloads=[{"type": "unrelated_type"}],
    )
    phase2_task = Task(
        project_id=project.id,
        title="failed phase two",
        status=TaskStatus.FAILED,
        payloads=[{"type": "unrelated_type"}],
    )
    db_session.add_all([phase1_task, phase2_task])
    await db_session.commit()

    await strategic_cycle.create(
        db_session, project_id=project.id, phase1_task_id=phase1_task.id
    )
    phase2_cycle = StrategicCycle(
        id="sc-terminal-phase2",
        project_id=project.id,
        outcome=strategic_cycle.OUTCOME_PHASE2_RUNNING,
        phase1_started_at=_utcnow(),
        phase2_started_at=_utcnow(),
        phase2_task_id=phase2_task.id,
    )
    db_session.add(phase2_cycle)
    await db_session.commit()

    reconciled_phase1 = await strategic_cycle.reconcile_terminal_task(
        db_session, phase1_task, source="test_single_cancel"
    )
    reconciled_phase2 = await strategic_cycle.reconcile_terminal_task(
        db_session, phase2_task, source="test_bulk_failure"
    )
    await db_session.commit()

    persisted_phase1 = await strategic_cycle.get(db_session, reconciled_phase1.id)
    persisted_phase2 = await strategic_cycle.get(db_session, reconciled_phase2.id)
    assert persisted_phase1.cycle_metadata[
        strategic_cycle.CYCLE_META_TERMINAL_DISPOSITION
    ] == strategic_cycle.TERMINAL_DISPOSITION_CANCELLED
    assert persisted_phase2.cycle_metadata[
        strategic_cycle.CYCLE_META_TERMINAL_DISPOSITION
    ] == strategic_cycle.TERMINAL_DISPOSITION_FAILED

    assert reconciled_phase1 is not None
    assert reconciled_phase1.outcome == strategic_cycle.OUTCOME_FAILED_PHASE1
    assert reconciled_phase1.phase1_completed_at is not None
    assert reconciled_phase1.failure_reason == (
        f"terminal_task_reconciliation: source=test_single_cancel "
        f"task_id={phase1_task.id} status=CANCELLED"
    )
    assert reconciled_phase1.cycle_metadata[
        strategic_cycle.CYCLE_META_TERMINAL_DISPOSITION
    ] == strategic_cycle.TERMINAL_DISPOSITION_CANCELLED
    assert reconciled_phase2 is not None
    assert reconciled_phase2.outcome == strategic_cycle.OUTCOME_FAILED_PHASE2
    assert reconciled_phase2.phase2_completed_at is not None
    assert reconciled_phase2.failure_reason == (
        f"terminal_task_reconciliation: source=test_bulk_failure "
        f"task_id={phase2_task.id} status=FAILED"
    )
    assert reconciled_phase2.cycle_metadata[
        strategic_cycle.CYCLE_META_TERMINAL_DISPOSITION
    ] == strategic_cycle.TERMINAL_DISPOSITION_FAILED

    original_reason = reconciled_phase1.failure_reason
    assert (
        await strategic_cycle.reconcile_terminal_task(
            db_session, phase1_task, source="duplicate_delivery"
        )
        is None
    )
    assert reconciled_phase1.failure_reason == original_reason


@pytest.mark.asyncio
async def test_reconcile_terminal_task_never_regresses_phase1_after_phase2_started(
    db_session,
):
    project = await _make_project(db_session)
    phase1_task = Task(
        project_id=project.id,
        title="late phase one failure",
        status=TaskStatus.CANCELLED,
        payloads=[{"type": "design_doc_update"}],
    )
    phase2_task = Task(
        project_id=project.id,
        title="active phase two",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "strategic_review"}],
    )
    db_session.add_all([phase1_task, phase2_task])
    await db_session.commit()
    cycle = StrategicCycle(
        id="sc-phase2-protected",
        project_id=project.id,
        outcome=strategic_cycle.OUTCOME_PHASE2_RUNNING,
        phase1_task_id=phase1_task.id,
        phase2_task_id=phase2_task.id,
        phase1_started_at=_utcnow(),
        phase1_completed_at=_utcnow(),
        phase2_started_at=_utcnow(),
    )
    db_session.add(cycle)
    await db_session.commit()

    reconciled = await strategic_cycle.reconcile_terminal_task(
        db_session, phase1_task, source="late_agent_status"
    )

    assert reconciled is None
    assert cycle.outcome == strategic_cycle.OUTCOME_PHASE2_RUNNING
    assert cycle.phase2_completed_at is None


@pytest.mark.asyncio
async def test_admin_cancel_is_serialized_against_completed_continuation(
    db_session, mocker
):
    """An admin cancel cannot overwrite a continuation that just completed.

    SQLite does not enforce PostgreSQL row locks, so this test supplies the same
    canonical pair-lock boundary with an asyncio lock and controls both sides
    of the race explicitly. The production admin mutation must wait for the
    continuation, re-read COMPLETED + pending, and fence the stale cancel.
    """
    project = await _make_project(db_session, name="terminal race")
    task = Task(
        project_id=project.id,
        title="strategic continuation",
        status=TaskStatus.COMPLETED,
        post_commit_pending=True,
        payloads=[{"type": "strategic_review", "prompt": "review"}],
        context={"_post_commit_pending_meta": {"generation_token": "g1"}},
    )
    db_session.add(task)
    await db_session.commit()
    cycle = StrategicCycle(
        id="sc-admin-cancel-race",
        project_id=project.id,
        outcome=strategic_cycle.OUTCOME_PHASE2_RUNNING,
        phase1_started_at=_utcnow(),
        phase2_started_at=_utcnow(),
        phase2_task_id=task.id,
    )
    db_session.add(cycle)
    await db_session.commit()

    pair_lock = asyncio.Lock()
    continuation_holding_lock = asyncio.Event()
    release_continuation = asyncio.Event()

    @asynccontextmanager
    async def controlled_pair_lock(session, project_id, task_id, **_kwargs):
        await pair_lock.acquire()
        try:
            yield (await session.get(Project, project_id), await session.get(Task, task_id))
        finally:
            pair_lock.release()

    mocker.patch("app.task_locking.lock_project_then_task", controlled_pair_lock)

    async def complete_continuation():
        async with controlled_pair_lock(db_session, project.id, task.id):
            continuation_holding_lock.set()
            await release_continuation.wait()
            task.status = TaskStatus.COMPLETED
            cycle.outcome = strategic_cycle.OUTCOME_COMPLETED
            await db_session.commit()

    continuation = asyncio.create_task(complete_continuation())
    await continuation_holding_lock.wait()

    from app.admin_task_mutations import modify_task_apply

    admin_cancel = asyncio.create_task(
        modify_task_apply(
            task,
            {"status": TaskStatus.CANCELLED.value},
            BackgroundTasks(),
            db_session,
        )
    )
    await asyncio.sleep(0)
    assert not admin_cancel.done()

    release_continuation.set()
    await continuation
    with pytest.raises(HTTPException) as exc_info:
        await admin_cancel
    assert exc_info.value.status_code == 409

    await db_session.refresh(task)
    await db_session.refresh(cycle)
    assert task.status == TaskStatus.COMPLETED
    assert cycle.outcome == strategic_cycle.OUTCOME_COMPLETED


@pytest.mark.asyncio
async def test_strategic_debug_recent_cycles_from_table(client, db_session):
    """GET /strategic-debug returns recent_cycles and last_cycle from the
    StrategicCycle table, not the retired vision._strategic_cycle_history JSONB."""
    from testing.factories import make_membership

    project = await _make_project(db_session)
    db_session.add(make_membership(project.id))
    await db_session.commit()
    await db_session.refresh(project)

    # Create two completed cycles — table is the only source of truth.
    for i in range(2):
        c = await strategic_cycle.create(db_session, project_id=project.id)
        await db_session.commit()
        await strategic_cycle.mark_phase1_completed(db_session, c.id)
        await db_session.commit()
        await strategic_cycle.mark_phase2_completed(
            db_session, c.id, directions_created=i + 1
        )
        await db_session.commit()

    resp = await client.get(f"/api/admin/projects/{project.id}/strategic-debug")
    assert resp.status_code == 200
    data = resp.json()

    # Both cycles should appear in recent_cycles (capped at 3).
    assert len(data["recent_cycles"]) == 2
    last = data["last_cycle"]
    assert last is not None
    assert last["outcome"] == strategic_cycle.OUTCOME_COMPLETED
    # Newest first — directions_created on the second cycle is 2.
    assert last["directions_created"] == 2


@pytest.mark.asyncio
async def test_strategic_debug_surfaces_automatic_cycle_lane_disabled(
    client, db_session
):
    """Strategic debug explains the automatic-lane hold while leaving manual
    Strategic availability explicit."""
    from testing.factories import make_membership

    project = await _make_project(db_session, name="Strategic lane disabled")
    project.build_config = {"automatic_strategic_cycles_enabled": False}
    db_session.add(project)
    db_session.add(make_membership(project.id))
    await db_session.commit()

    resp = await client.get(f"/api/admin/projects/{project.id}/strategic-debug")
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert data["automatic_strategic_cycles_enabled"] is False
    guard = next(
        item
        for item in data["blocking_guards"]
        if item["guard"] == "automatic_strategic_cycles_disabled"
    )
    assert "explicit/manual Strategic launches remain available" in guard["reason"]
    assert data["phase"] == 3


@pytest.mark.asyncio
async def test_strategic_debug_reports_evaluating_task_as_active(client, db_session):
    """Debug truth matches the admission guard while a cycle awaits children."""
    from testing.factories import make_membership

    project = await _make_project(db_session, name="Strategic evaluating debug")
    task = Task(
        project_id=project.id,
        title="Evaluating Strategic cycle",
        payloads=[{"type": "strategic_review"}],
        status=TaskStatus.EVALUATING,
    )
    db_session.add_all([make_membership(project.id), task])
    await db_session.commit()

    resp = await client.get(f"/api/admin/projects/{project.id}/strategic-debug")
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert data["phase"] == 2
    assert data["active_task"] == {
        "id": str(task.id),
        "phase": "strategic_review",
        "status": TaskStatus.EVALUATING.value,
        "started_at": None,
    }


# --- integrity conflict counts -----------------------------------------------


@pytest.mark.asyncio
async def test_record_integrity_conflict_counts_stamps_metadata(db_session):
    """record_integrity_conflict_counts writes the three counts into cycle_metadata."""
    project = await _make_project(db_session)
    cycle = await strategic_cycle.create(db_session, project_id=project.id)
    await db_session.commit()

    counts = {"orphaned_gaps": 2, "ungrounded_directions": 1, "stale_active_moves": 0, "staffed_but_deferred": 3}
    strategic_cycle.record_integrity_conflict_counts(cycle, counts)
    db_session.add(cycle)
    await db_session.commit()

    fetched = await strategic_cycle.get(db_session, cycle.id)
    assert fetched is not None
    stored = fetched.cycle_metadata.get(CYCLE_META_INTEGRITY_CONFLICT_COUNTS)
    assert stored == {"orphaned_gaps": 2, "ungrounded_directions": 1, "stale_active_moves": 0, "staffed_but_deferred": 3}


@pytest.mark.asyncio
async def test_record_integrity_conflict_counts_zeros_are_explicit(db_session):
    """Explicit zero counts are stored, distinguishing clean cycles from legacy (no stamp)."""
    project = await _make_project(db_session)
    cycle = await strategic_cycle.create(db_session, project_id=project.id)
    await db_session.commit()

    counts = {"orphaned_gaps": 0, "ungrounded_directions": 0, "stale_active_moves": 0, "staffed_but_deferred": 0}
    strategic_cycle.record_integrity_conflict_counts(cycle, counts)
    db_session.add(cycle)
    await db_session.commit()

    fetched = await strategic_cycle.get(db_session, cycle.id)
    assert fetched is not None
    stored = fetched.cycle_metadata.get(CYCLE_META_INTEGRITY_CONFLICT_COUNTS)
    assert stored is not None
    assert stored["orphaned_gaps"] == 0
    assert stored["ungrounded_directions"] == 0
    assert stored["stale_active_moves"] == 0
    assert stored["staffed_but_deferred"] == 0


@pytest.mark.asyncio
async def test_recent_integrity_conflict_trend_consecutive_clean(db_session):
    """consecutive_clean_cycles counts unbroken clean cycles from most recent."""
    project = await _make_project(db_session)

    async def _make_completed_cycle(counts):
        c = await strategic_cycle.create(db_session, project_id=project.id)
        await db_session.commit()
        strategic_cycle.record_integrity_conflict_counts(c, counts)
        db_session.add(c)
        await strategic_cycle.mark_phase1_completed(db_session, c.id)
        await db_session.commit()
        await strategic_cycle.mark_phase2_completed(db_session, c.id)
        await db_session.commit()
        return c

    # Oldest cycle has conflicts, two newer ones are clean.
    await _make_completed_cycle({"orphaned_gaps": 3, "ungrounded_directions": 1, "stale_active_moves": 0, "staffed_but_deferred": 0})
    await _make_completed_cycle({"orphaned_gaps": 0, "ungrounded_directions": 0, "stale_active_moves": 0, "staffed_but_deferred": 0})
    await _make_completed_cycle({"orphaned_gaps": 0, "ungrounded_directions": 0, "stale_active_moves": 0, "staffed_but_deferred": 0})

    trend = await strategic_cycle.recent_integrity_conflict_trend(db_session, project.id)
    assert trend["stamped_cycles"] == 3
    assert trend["consecutive_clean_cycles"] == 2
    assert trend["last_counts"] == {"orphaned_gaps": 0, "ungrounded_directions": 0, "stale_active_moves": 0, "staffed_but_deferred": 0}


@pytest.mark.asyncio
async def test_recent_integrity_conflict_trend_dirty_resets_count(db_session):
    """A dirty cycle breaks the clean streak even when older cycles are clean."""
    project = await _make_project(db_session)

    async def _make_completed_cycle(counts):
        c = await strategic_cycle.create(db_session, project_id=project.id)
        await db_session.commit()
        strategic_cycle.record_integrity_conflict_counts(c, counts)
        db_session.add(c)
        await strategic_cycle.mark_phase1_completed(db_session, c.id)
        await db_session.commit()
        await strategic_cycle.mark_phase2_completed(db_session, c.id)
        await db_session.commit()
        return c

    # Most-recent cycle has conflicts — streak is zero even though prior was clean.
    await _make_completed_cycle({"orphaned_gaps": 0, "ungrounded_directions": 0, "stale_active_moves": 0, "staffed_but_deferred": 0})
    await _make_completed_cycle({"orphaned_gaps": 1, "ungrounded_directions": 0, "stale_active_moves": 0, "staffed_but_deferred": 0})

    trend = await strategic_cycle.recent_integrity_conflict_trend(db_session, project.id)
    assert trend["stamped_cycles"] == 2
    assert trend["consecutive_clean_cycles"] == 0
    assert trend["last_counts"]["orphaned_gaps"] == 1


@pytest.mark.asyncio
async def test_recent_integrity_conflict_trend_no_stamps(db_session):
    """Legacy cycles without stamps return zero stamped_cycles and clean streak."""
    project = await _make_project(db_session)

    # Completed cycle with no integrity_conflict_counts stamp (legacy).
    c = await strategic_cycle.create(db_session, project_id=project.id)
    await db_session.commit()
    await strategic_cycle.mark_phase1_completed(db_session, c.id)
    await db_session.commit()
    await strategic_cycle.mark_phase2_completed(db_session, c.id)
    await db_session.commit()

    trend = await strategic_cycle.recent_integrity_conflict_trend(db_session, project.id)
    assert trend["stamped_cycles"] == 0
    assert trend["consecutive_clean_cycles"] == 0
    assert trend["last_counts"] is None


@pytest.mark.asyncio
async def test_to_legacy_dict_includes_integrity_conflict_counts(db_session):
    """integrity_conflict_counts from cycle_metadata propagates into the agent-context dict."""
    project = await _make_project(db_session)
    cycle = await strategic_cycle.create(db_session, project_id=project.id)
    await db_session.commit()

    counts = {"orphaned_gaps": 0, "ungrounded_directions": 0, "stale_active_moves": 0, "staffed_but_deferred": 0}
    strategic_cycle.record_integrity_conflict_counts(cycle, counts)
    db_session.add(cycle)
    await db_session.commit()

    legacy = strategic_cycle._to_legacy_dict(cycle)
    assert CYCLE_META_INTEGRITY_CONFLICT_COUNTS in legacy
    assert legacy[CYCLE_META_INTEGRITY_CONFLICT_COUNTS] == counts


@pytest.mark.asyncio
async def test_integrity_conflict_trend_includes_staffed_but_deferred(db_session):
    """staffed_but_deferred is persisted and factored into the trend's clean/dirty determination."""
    project = await _make_project(db_session)

    async def _make_completed_cycle(counts):
        c = await strategic_cycle.create(db_session, project_id=project.id)
        await db_session.commit()
        strategic_cycle.record_integrity_conflict_counts(c, counts)
        db_session.add(c)
        await strategic_cycle.mark_phase1_completed(db_session, c.id)
        await db_session.commit()
        await strategic_cycle.mark_phase2_completed(db_session, c.id)
        await db_session.commit()
        return c

    # Cycle 1: all clean including staffed_but_deferred
    await _make_completed_cycle({
        "orphaned_gaps": 0,
        "ungrounded_directions": 0,
        "stale_active_moves": 0,
        "staffed_but_deferred": 0,
    })
    # Cycle 2 (most recent): staffed_but_deferred is nonzero — should break clean streak
    await _make_completed_cycle({
        "orphaned_gaps": 0,
        "ungrounded_directions": 0,
        "stale_active_moves": 0,
        "staffed_but_deferred": 2,
    })

    trend = await strategic_cycle.recent_integrity_conflict_trend(db_session, project.id)
    assert trend["stamped_cycles"] == 2
    # Most-recent cycle has staffed_but_deferred=2 — streak must be 0
    assert trend["consecutive_clean_cycles"] == 0
    last = trend["last_counts"]
    assert last is not None
    assert last["staffed_but_deferred"] == 2
    # All four classes present in last_counts
    assert "orphaned_gaps" in last
    assert "ungrounded_directions" in last
    assert "stale_active_moves" in last


@pytest.mark.asyncio
async def test_recent_integrity_conflict_trend_legacy_stamp_breaks_streak(db_session):
    """Legacy stamps (without staffed_but_deferred key) break the clean streak."""
    project = await _make_project(db_session)

    # Create a completed cycle with only the three legacy keys (simulates a
    # pre-staffed_but_deferred stamp written before this feature landed).
    c = await strategic_cycle.create(db_session, project_id=project.id)
    await db_session.commit()
    # Manually write a legacy-format stamp (three keys only)
    from app.models import CYCLE_META_INTEGRITY_CONFLICT_COUNTS  # noqa: PLC0415
    from sqlalchemy.orm.attributes import flag_modified as _flag_modified  # noqa: PLC0415
    new_meta = dict(c.cycle_metadata or {})
    new_meta[CYCLE_META_INTEGRITY_CONFLICT_COUNTS] = {
        "orphaned_gaps": 0,
        "ungrounded_directions": 0,
        "stale_active_moves": 0,
        # intentionally NO "staffed_but_deferred"
    }
    c.cycle_metadata = new_meta
    _flag_modified(c, "cycle_metadata")
    db_session.add(c)
    await strategic_cycle.mark_phase1_completed(db_session, c.id)
    await db_session.commit()
    await strategic_cycle.mark_phase2_completed(db_session, c.id)
    await db_session.commit()

    trend = await strategic_cycle.recent_integrity_conflict_trend(db_session, project.id)
    # Cycle is stamped so stamped_cycles == 1
    assert trend["stamped_cycles"] == 1
    # Legacy stamp breaks the streak (staffed_but_deferred class unknown)
    assert trend["consecutive_clean_cycles"] == 0
    # last_counts exists but does NOT include staffed_but_deferred
    assert trend["last_counts"] is not None
    assert "staffed_but_deferred" not in trend["last_counts"]
