"""Focused contracts for discovery-cycle provenance."""

from __future__ import annotations

import uuid

import pytest

import app.discovery_admission as discovery_cycles
from app.discovery_admission import (
    DiscoveryCycleUnavailable,
    ensure_discovery_cycle_for_research,
    resolve_discovery_cycle_lineage,
)
from app.models import DiscoveryCycle, Project, StrategicSpawnRequest, Task, TaskStatus
from app.task_blocked import reset_task_for_fresh_attempt
from app.time_utils import _utcnow


async def _project_and_cycle(db_session, **cycle_overrides):
    project = Project(
        name=f"cycle-{uuid.uuid4().hex[:8]}",
        user_id="discovery-cycle-test",
    )
    db_session.add(project)
    await db_session.flush()
    values = {
        "project_id": project.id,
        "direction_key": f"d-{uuid.uuid4().hex[:8]}",
        "outcome_kind": "mixed",
    }
    values.update(cycle_overrides)
    cycle = DiscoveryCycle(**values)
    db_session.add(cycle)
    await db_session.flush()
    return project, cycle


@pytest.mark.asyncio
async def test_research_bootstrap_creates_one_frozen_provenance_cycle(db_session):
    project = Project(
        name="Purposeful product",
        user_id="discovery-bootstrap-test",
        vision={"goal": "Make the experiment useful to a real user."},
        problem_model={
            "intent": {"user_purpose": "Understand whether the mechanism works."}
        },
        running_update="Current product direction and constraints.",
    )
    db_session.add(project)
    await db_session.flush()
    direction = {
        "id": "paired-levels",
        "text": "Build paired constructor and evaluator levels",
        "category": "product",
        "completion_criteria": "A user can play and compare both generated levels.",
        "context": {
            "success_evidence": "A blinded evaluator prefers the paired levels.",
            "retirement_condition": "Retire if paired levels do not improve preference.",
            "implementation_constraints": "Use the existing level runtime.",
            "mutable_strategic_note": "Do not promote this to review truth.",
        },
    }

    first = await ensure_discovery_cycle_for_research(
        db_session,
        project=project,
        direction_key=direction["id"],
        direction=direction,
        now=_utcnow(),
    )
    direction["context"]["success_evidence"] = "A later Strategic rewrite."
    replay = await ensure_discovery_cycle_for_research(
        db_session,
        project=project,
        direction_key=direction["id"],
        direction=direction,
    )

    assert replay.id == first.id
    assert first.generation == 1
    assert first.state == "open"
    assert first.intent_snapshot["user_purpose"] == (
        "Understand whether the mechanism works."
    )
    assert "problem_model" not in first.intent_snapshot
    assert (
        first.bet_snapshot["direction"]["completion_criteria"]
        == (direction["completion_criteria"])
    )
    assert first.bet_snapshot["direction"]["context"] == {
        "success_evidence": "A blinded evaluator prefers the paired levels.",
        "retirement_condition": "Retire if paired levels do not improve preference.",
        "implementation_constraints": "Use the existing level runtime.",
    }
    assert first.deadline_at is not None
    assert not hasattr(first, "task_credit_limit")
    assert not hasattr(first, "attempt_credit_limit")
    assert not hasattr(first, "concurrency_limit")


@pytest.mark.asyncio
async def test_direct_commission_binds_source_free_cycle_but_not_occupied_replay(
    db_session,
):
    project = Project(name="Direct bounded question", user_id="direct-question-test")
    db_session.add(project)
    await db_session.flush()
    direction = {
        "id": "scheduler-race",
        "text": "Broad scheduler reliability",
        "completion_criteria": "The scheduler is reliable.",
        "paths": ["backend/app/orchestrator.py"],
    }
    reserved = await ensure_discovery_cycle_for_research(
        db_session,
        project=project,
        direction_key=direction["id"],
        direction=direction,
    )
    original_fingerprint = reserved.mechanism_fingerprint
    commissioned_question = "Which admission race causes childless parents?"

    owner = Task(
        project_id=project.id,
        title="Pending Strategic owner",
        payloads=[{"type": "strategic_review"}],
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(owner)
    await db_session.flush()
    pending = StrategicSpawnRequest(
        project_id=project.id,
        parent_task_id=owner.id,
        idempotency_key="pending-owner",
        task_type="research",
        specification={"task": {"payloads": [{"type": "research"}]}},
        specification_hash="b" * 64,
        direction_id=direction["id"],
        discovery_cycle_id=reserved.id,
        state="accepted",
    )
    db_session.add(pending)
    await db_session.flush()

    still_owned = await ensure_discovery_cycle_for_research(
        db_session,
        project=project,
        direction_key=direction["id"],
        direction=direction,
        commissioned_question=commissioned_question,
        join_open_cycle=True,
    )
    assert still_owned.mechanism_fingerprint == original_fingerprint
    assert still_owned.bet_snapshot["claim"] == direction["text"]

    # Once that reservation is cancelled, a source-free direct commission may
    # take ownership and bind the exact decision it will ask.
    pending.state = "cancelled"
    db_session.add(pending)
    await db_session.flush()

    bound = await ensure_discovery_cycle_for_research(
        db_session,
        project=project,
        direction_key=direction["id"],
        direction=direction,
        commissioned_question=commissioned_question,
        join_open_cycle=True,
    )

    assert bound.id == reserved.id
    assert bound.bet_snapshot["claim"] == commissioned_question
    assert bound.bet_snapshot["decision_question"] == commissioned_question
    assert bound.bet_snapshot["direction"]["paths"] == direction["paths"]
    assert bound.mechanism_fingerprint != original_fingerprint

    source = Task(
        project_id=project.id,
        discovery_cycle_id=bound.id,
        title="Occupied research source",
        payloads=[{"type": "research", "prompt": commissioned_question}],
        status=TaskStatus.QUEUED,
    )
    db_session.add(source)
    await db_session.flush()
    bound.source_research_task_id = source.id
    frozen_bet = dict(bound.bet_snapshot)
    frozen_fingerprint = bound.mechanism_fingerprint

    replay = await ensure_discovery_cycle_for_research(
        db_session,
        project=project,
        direction_key=direction["id"],
        direction=direction,
        commissioned_question="A different parent question must not rewrite the bet.",
        join_open_cycle=True,
    )

    assert replay.bet_snapshot == frozen_bet
    assert replay.mechanism_fingerprint == frozen_fingerprint


@pytest.mark.asyncio
async def test_terminal_policy_halts_new_direction_root_until_resolved(db_session):
    project = Project(name="Halted direction", user_id="discovery-halt-test")
    db_session.add(project)
    await db_session.flush()
    db_session.add(
        DiscoveryCycle(
            project_id=project.id,
            direction_key="unsafe-expansion",
            generation=1,
            outcome_kind="mixed",
            state="reviewed",
            verdict="ask_operator",
        )
    )
    await db_session.flush()

    with pytest.raises(DiscoveryCycleUnavailable, match="awaiting policy"):
        await ensure_discovery_cycle_for_research(
            db_session,
            project=project,
            direction_key="unsafe-expansion",
            direction={"id": "unsafe-expansion", "text": "Expand the mechanism"},
        )


@pytest.mark.asyncio
async def test_lineage_is_provenance_only_and_resolves_through_ancestors(db_session):
    project, cycle = await _project_and_cycle(db_session)
    root = Task(
        project_id=project.id,
        discovery_cycle_id=cycle.id,
        title="Research root",
        payloads=[{"type": "research"}],
        status=TaskStatus.COMPLETED,
    )
    child = Task(
        project_id=project.id,
        title="Implementation",
        payloads=[{"type": "create", "prompt": "build it"}],
        parent_task_id=root.id,
        status=TaskStatus.QUEUED,
    )
    grandchild = Task(
        project_id=project.id,
        title="Review",
        payloads=[{"type": "review", "pr_number": 1, "branch": "research/x"}],
        parent_task_id=child.id,
        status=TaskStatus.QUEUED,
    )
    db_session.add_all([root, child, grandchild])
    await db_session.flush()

    assert (
        await resolve_discovery_cycle_lineage(db_session, task_id=grandchild.id)
        == cycle.id
    )
    assert grandchild.status == TaskStatus.QUEUED
    assert not hasattr(discovery_cycles, "authorize_discovery_task_execution")
    assert not hasattr(discovery_cycles, "reserve_discovery_admission")


@pytest.mark.asyncio
async def test_lineage_resolves_durable_spawn_request(db_session):
    project, cycle = await _project_and_cycle(db_session)
    parent = Task(
        project_id=project.id,
        discovery_cycle_id=cycle.id,
        title="Strategic parent",
        payloads=[{"type": "strategic"}],
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(parent)
    await db_session.flush()
    request = StrategicSpawnRequest(
        project_id=project.id,
        parent_task_id=parent.id,
        idempotency_key="one-child",
        task_type="create",
        specification={"task": {"payloads": [{"type": "create"}]}},
        specification_hash="a" * 64,
        discovery_cycle_id=cycle.id,
        state="accepted",
    )
    db_session.add(request)
    await db_session.flush()

    assert (
        await resolve_discovery_cycle_lineage(
            db_session,
            strategic_spawn_request_id=request.id,
        )
        == cycle.id
    )


@pytest.mark.asyncio
async def test_lineage_resolution_does_not_cross_project_boundaries(db_session):
    own_project, _own_cycle = await _project_and_cycle(db_session)
    foreign_project, foreign_cycle = await _project_and_cycle(db_session)
    foreign_parent = Task(
        project_id=foreign_project.id,
        discovery_cycle_id=foreign_cycle.id,
        title="Foreign research root",
        payloads=[{"type": "research"}],
        status=TaskStatus.COMPLETED,
    )
    db_session.add(foreign_parent)
    await db_session.flush()
    foreign_request = StrategicSpawnRequest(
        project_id=foreign_project.id,
        parent_task_id=foreign_parent.id,
        idempotency_key="foreign-lineage",
        task_type="create",
        specification={"task": {"payloads": [{"type": "create"}]}},
        specification_hash="b" * 64,
        discovery_cycle_id=foreign_cycle.id,
        state="accepted",
    )
    db_session.add(foreign_request)
    await db_session.flush()

    assert (
        await resolve_discovery_cycle_lineage(
            db_session,
            task_id=foreign_parent.id,
            project_id=own_project.id,
        )
        is None
    )
    assert (
        await resolve_discovery_cycle_lineage(
            db_session,
            strategic_spawn_request_id=foreign_request.id,
            project_id=own_project.id,
        )
        is None
    )


def test_fresh_retry_keeps_normal_retry_state_without_discovery_accounting():
    task = Task(
        project_id=uuid.uuid4(),
        title="Retry me",
        payloads=[{"type": "create", "prompt": "build"}],
        status=TaskStatus.BLOCKED,
        context={"failure_category": "blocked_precondition"},
    )

    reset_task_for_fresh_attempt(task)

    assert task.context.get("_fresh_attempt_started_at")
    assert "_discovery_attempt_ordinal" not in task.context
