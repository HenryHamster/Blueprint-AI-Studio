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
    assert first.bet_snapshot["direction"]["completion_criteria"] == (
        direction["completion_criteria"]
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

    assert await resolve_discovery_cycle_lineage(
        db_session, task_id=grandchild.id
    ) == cycle.id
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

    assert await resolve_discovery_cycle_lineage(
        db_session,
        strategic_spawn_request_id=request.id,
    ) == cycle.id


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
