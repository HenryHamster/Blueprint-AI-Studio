"""Focused integration coverage for discovery lineage at task boundaries."""

from __future__ import annotations

import inspect
import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from app.models import (
    DiscoveryCycle,
    Project,
    Task,
    TaskStatus,
)
from app.strategic_spawn_requests import (
    cancel_deferred_spawn_request,
    submit_strategic_spawn,
)


def test_research_spawn_only_absorbs_the_known_direction_claim_collision():
    from app.autonomous_research import _is_research_claim_integrity_error

    known = IntegrityError(
        "insert task",
        {},
        Exception("UNIQUE constraint failed: task.idempotency_key"),
    )
    unrelated = IntegrityError(
        "insert task",
        {},
        Exception("NOT NULL constraint failed: task.project_id"),
    )

    assert _is_research_claim_integrity_error(known) is True
    assert _is_research_claim_integrity_error(unrelated) is False


def test_cold_and_warm_execution_boundaries_do_not_use_discovery_authority():
    """Discovery lineage must not strand already-admitted execution work."""

    from app import orchestrator
    from app.routers import agent_warm_continuity

    cold_source = inspect.getsource(orchestrator.orchestrator_worker)
    warm_source = inspect.getsource(agent_warm_continuity._finalize_warm_claim)
    assert "authorize_discovery_task_execution" not in cold_source
    assert "reserve_discovery_admission" not in cold_source
    assert "authorize_discovery_task_execution" not in warm_source
    assert "discovery_admission_denied" not in cold_source
    assert "discovery_admission_denied" not in warm_source


def test_fresh_execution_retries_do_not_stamp_discovery_accounting():
    """Retry policy is independent from discovery provenance."""

    from app import (
        blocked_task_unblock_sweeps,
        orchestrator,
        reaper,
        reaper_infra_recovery,
        task_blocked,
        task_lifecycle_requeue,
    )

    reset_source = inspect.getsource(task_blocked.reset_task_for_fresh_attempt)
    assert "_fresh_attempt_started_at" in reset_source
    assert "_discovery_attempt_ordinal" not in reset_source

    clone_source = inspect.getsource(task_lifecycle_requeue._requeue_parent_task)
    assert "_discovery_attempt_ordinal" not in clone_source
    assert "reserve_discovery_admission" not in clone_source
    for module in (
        blocked_task_unblock_sweeps,
        orchestrator,
        reaper,
        reaper_infra_recovery,
    ):
        assert "mark_task_for_fresh_execution_attempt" not in inspect.getsource(module)


def test_autonomous_direction_research_bootstraps_and_attaches_cycle():
    from app import autonomous_research

    source = inspect.getsource(autonomous_research._spawn_research_task_inner)
    assert "ensure_discovery_cycle_for_research" in source
    assert "_discovery_cycle.source_research_task_id = new_task.id" in source


async def _cycle_owner(db_session, *, state: str = "open"):
    project = Project(
        name=f"Discovery integration {uuid.uuid4().hex[:8]}",
        user_id="test-user",
    )
    db_session.add(project)
    await db_session.flush()
    cycle = DiscoveryCycle(
        project_id=project.id,
        direction_key="direction-a",
        outcome_kind="mixed",
        state=state,
    )
    db_session.add(cycle)
    await db_session.flush()
    owner = Task(
        project_id=project.id,
        discovery_cycle_id=cycle.id,
        title="Resident Strategic",
        created_by="STRATEGIC_AGENT",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "strategic_review"}],
    )
    db_session.add(owner)
    await db_session.commit()
    return project, cycle, owner


def _spec() -> dict:
    return {
        "task": {
            "title": "Build the bounded probe",
            "description": "implement it",
            "payloads": [{"type": "create", "prompt": "implement it"}],
            "context": {},
            "watch": False,
        }
    }


@pytest.mark.asyncio
async def test_strategic_spawn_propagates_cycle_without_execution_gate(db_session):
    project, cycle, owner = await _cycle_owner(db_session)

    first = await submit_strategic_spawn(
        db_session,
        project_id=project.id,
        parent_task_id=owner.id,
        task_type="create",
        specification=_spec(),
        idempotency_key="bounded-probe",
        direction_id="direction-a",
    )
    replay = await submit_strategic_spawn(
        db_session,
        project_id=project.id,
        parent_task_id=owner.id,
        task_type="create",
        specification=_spec(),
        idempotency_key="bounded-probe",
        direction_id="direction-a",
    )

    assert first.request.discovery_cycle_id == cycle.id
    assert first.task is not None
    assert first.task.discovery_cycle_id == cycle.id
    assert replay.replayed is True
    assert replay.task.id == first.task.id

    await db_session.refresh(cycle)
    assert not hasattr(cycle, "reserved_task_credits")
    assert not hasattr(cycle, "consumed_task_credits")


@pytest.mark.asyncio
async def test_closed_cycle_lineage_does_not_block_closure_spawn(db_session):
    project, cycle, owner = await _cycle_owner(
        db_session,
        state="admission_closed",
    )
    project_id = project.id
    submission = await submit_strategic_spawn(
        db_session,
        project_id=project_id,
        parent_task_id=owner.id,
        task_type="create",
        specification=_spec(),
        idempotency_key="closure-work",
        direction_id="direction-a",
    )

    assert submission.task is not None
    assert submission.task.discovery_cycle_id == cycle.id
    assert submission.request.discovery_cycle_id == cycle.id


@pytest.mark.asyncio
async def test_cancelling_deferred_spawn_preserves_cycle_provenance(db_session):
    project, cycle, owner = await _cycle_owner(db_session)
    submission = await submit_strategic_spawn(
        db_session,
        project_id=project.id,
        parent_task_id=owner.id,
        task_type="create",
        specification=_spec(),
        idempotency_key="deferred-probe",
        direction_id="direction-a",
        burst_cap=1,
    )
    assert submission.task is not None

    deferred = await submit_strategic_spawn(
        db_session,
        project_id=project.id,
        parent_task_id=owner.id,
        task_type="create",
        specification={
            **_spec(),
            "task": {**_spec()["task"], "title": "Deferred probe"},
        },
        idempotency_key="deferred-probe-2",
        direction_id="direction-a",
        burst_cap=1,
    )
    assert deferred.request.state == "deferred"

    await cancel_deferred_spawn_request(
        db_session,
        project_id=project.id,
        request_id=deferred.request.id,
    )
    await db_session.commit()
    await db_session.refresh(cycle)

    assert deferred.request.state == "cancelled"
    assert deferred.request.discovery_cycle_id == cycle.id
