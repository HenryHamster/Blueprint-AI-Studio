"""Focused integration coverage for discovery lineage at task boundaries."""

from __future__ import annotations

import inspect
import uuid

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from app.models import (
    DiscoveryAdmission,
    DiscoveryCycle,
    Project,
    StrategicSpawnRequest,
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


def test_cold_and_warm_execution_boundaries_use_discovery_authority_backstop():
    """Every path that flips queued work toward execution shares one fence."""

    from app import orchestrator
    from app.routers import agent_warm_continuity

    helper_name = "authorize_discovery_task_execution"
    cold_source = inspect.getsource(orchestrator.orchestrator_worker)
    assert cold_source.count(helper_name) >= 2
    final_claim = cold_source.rfind(helper_name)
    provisioning = cold_source.find(
        "task.status = TaskStatus.PROVISIONING",
        final_claim,
    )
    claim_commit = cold_source.find("await session.commit()", provisioning)
    assert final_claim < provisioning < claim_commit
    assert "session.rollback()" not in cold_source[final_claim:claim_commit]
    assert "recompute_launch_staleness" not in cold_source[final_claim:provisioning]
    assert helper_name in inspect.getsource(
        agent_warm_continuity._finalize_warm_claim
    )


def test_all_fresh_execution_retry_families_stamp_a_dispatch_incarnation():
    """Canonical resets plus low-level recovery bypasses cannot escape charging."""

    from app import (
        blocked_task_unblock_sweeps,
        orchestrator,
        reaper,
        reaper_infra_recovery,
        task_blocked,
        task_lifecycle_requeue,
    )

    reset_source = inspect.getsource(task_blocked.reset_task_for_fresh_attempt)
    assert "mark_task_for_fresh_execution_attempt(task)" in reset_source

    # Replacement clones do not call the same-row reset helper, but carry the
    # same canonical incarnation marker into the common dispatch fence.
    clone_source = inspect.getsource(task_lifecycle_requeue._requeue_parent_task)
    assert '"_discovery_attempt_ordinal": 1' in clone_source
    assert "reserve_discovery_admission" not in clone_source

    # Provision recovery and the reaper's provider-specific fast paths retain
    # bespoke context cleanup, so they explicitly stamp after that cleanup.
    assert "mark_task_for_fresh_execution_attempt(fresh_t)" in inspect.getsource(
        orchestrator.orchestrator_worker
    )
    reaper_source = inspect.getsource(reaper.reaper_service)
    assert reaper_source.count("mark_task_for_fresh_execution_attempt(task)") >= 2
    assert "mark_task_for_fresh_execution_attempt(_t)" in inspect.getsource(
        reaper_infra_recovery.run_infra_recovery_and_hold_maintenance
    )
    blocked_source = inspect.getsource(
        blocked_task_unblock_sweeps.sweep_unblock_blocked_tasks
    )
    assert blocked_source.count(
        "mark_task_for_fresh_execution_attempt(task)"
    ) >= 4
    assert "mark_task_for_fresh_execution_attempt(task)" in inspect.getsource(
        blocked_task_unblock_sweeps._apply_needs_disposition
    )


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
        task_credit_limit=4,
        attempt_credit_limit=2,
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
async def test_strategic_spawn_reserves_once_and_propagates_cycle(db_session):
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

    admissions = (
        (
            await db_session.execute(
                select(DiscoveryAdmission).where(
                    DiscoveryAdmission.cycle_id == cycle.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(admissions) == 1
    assert admissions[0].state == "consumed"
    assert admissions[0].task_id == first.task.id
    await db_session.refresh(cycle)
    assert cycle.reserved_task_credits == 0
    assert cycle.consumed_task_credits == 1


@pytest.mark.asyncio
async def test_closed_cycle_rejects_new_strategic_intent(db_session):
    project, cycle, owner = await _cycle_owner(
        db_session,
        state="admission_closed",
    )
    project_id = project.id
    cycle_id = cycle.id
    owner_id = owner.id

    with pytest.raises(ValueError, match="cycle_not_admitting"):
        await submit_strategic_spawn(
            db_session,
            project_id=project_id,
            parent_task_id=owner_id,
            task_type="create",
            specification=_spec(),
            idempotency_key="too-late",
            direction_id="direction-a",
        )

    assert (
        await db_session.execute(
            select(StrategicSpawnRequest).where(
                StrategicSpawnRequest.parent_task_id == owner_id
            )
        )
    ).scalar_one_or_none() is None
    assert (
        await db_session.execute(
            select(DiscoveryAdmission).where(
                DiscoveryAdmission.cycle_id == cycle_id
            )
        )
    ).scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_cancelling_deferred_spawn_releases_reserved_credit(db_session):
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
    await db_session.refresh(cycle)
    assert cycle.reserved_task_credits == 1

    await cancel_deferred_spawn_request(
        db_session,
        project_id=project.id,
        request_id=deferred.request.id,
    )
    await db_session.commit()
    await db_session.refresh(cycle)

    assert cycle.reserved_task_credits == 0
    admission = (
        await db_session.execute(
            select(DiscoveryAdmission).where(
                DiscoveryAdmission.spawn_request_id == deferred.request.id
            )
        )
    ).scalar_one()
    assert admission.state == "released"
