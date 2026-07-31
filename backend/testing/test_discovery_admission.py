"""Focused contracts for discovery-cycle admission accounting."""

from __future__ import annotations

from datetime import timedelta
import uuid

import pytest
from sqlalchemy import select

from app.discovery_admission import (
    authorize_discovery_task_execution,
    consume_discovery_admission,
    DiscoveryCycleUnavailable,
    discovery_admission_mode_from_runtime,
    ensure_discovery_cycle_for_research,
    release_discovery_admission,
    reserve_discovery_admission,
    resolve_discovery_cycle_lineage,
    validate_discovery_dispatch_authority,
)
from app.models import (
    DiscoveryAdmission,
    DiscoveryCycle,
    Project,
    StrategicSpawnRequest,
    Task,
    TaskStatus,
)
from app.time_utils import _utcnow
from app.task_blocked import reset_task_for_fresh_attempt


async def _project_and_cycle(db_session, **cycle_overrides):
    project = Project(
        name=f"admission-{uuid.uuid4().hex[:8]}",
        user_id="discovery-admission-test",
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
async def test_research_bootstrap_creates_one_frozen_bounded_cycle(db_session):
    project = Project(
        name="Purposeful product",
        user_id="discovery-bootstrap-test",
        vision={"goal": "Make the experiment useful to a real user."},
        problem_model={
            "intent": {
                "user_purpose": "Understand whether the mechanism works.",
            }
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
    }

    first = await ensure_discovery_cycle_for_research(
        db_session,
        project=project,
        direction_key=direction["id"],
        direction=direction,
        now=_utcnow(),
    )
    replay = await ensure_discovery_cycle_for_research(
        db_session,
        project=project,
        direction_key=direction["id"],
        direction=direction,
    )

    assert replay.id == first.id
    assert first.generation == 1
    assert first.state == "open"
    assert (
        first.intent_snapshot["user_purpose"]
        == "Understand whether the mechanism works."
    )
    assert "problem_model" not in first.intent_snapshot
    assert first.bet_snapshot["direction"]["completion_criteria"] == (
        direction["completion_criteria"]
    )
    assert first.task_credit_limit == 8
    assert first.attempt_credit_limit == 4
    assert first.concurrency_limit == 2
    assert first.deadline_at is not None


@pytest.mark.asyncio
async def test_terminal_policy_halts_new_direction_root_until_resolved(db_session):
    project = Project(
        name="Halted direction",
        user_id="discovery-halt-test",
    )
    db_session.add(project)
    await db_session.flush()
    prior = DiscoveryCycle(
        project_id=project.id,
        direction_key="unsafe-expansion",
        generation=1,
        outcome_kind="mixed",
        state="reviewed",
        verdict="expand",
    )
    db_session.add(prior)
    await db_session.flush()

    with pytest.raises(DiscoveryCycleUnavailable, match="awaiting policy"):
        await ensure_discovery_cycle_for_research(
            db_session,
            project=project,
            direction_key="unsafe-expansion",
            direction={"id": "unsafe-expansion", "text": "Expand the mechanism"},
            runtime_config={"discovery_admission_mode": "enforced"},
        )


@pytest.mark.asyncio
async def test_shadow_mode_observes_without_suppressing_a_second_root(db_session):
    project, cycle = await _project_and_cycle(
        db_session,
        direction_key="shadow-direction",
        state="reviewing",
    )

    observed = await ensure_discovery_cycle_for_research(
        db_session,
        project=project,
        direction_key="shadow-direction",
        direction={"id": "shadow-direction", "text": "Keep observing"},
        runtime_config={"discovery_admission_mode": "shadow"},
    )

    assert observed is None


@pytest.mark.asyncio
async def test_enforced_mode_halts_capped_renew_without_live_successor(db_session):
    project, cycle = await _project_and_cycle(
        db_session,
        direction_key="capped-renew",
        state="reviewed",
        generation=3,
        verdict="renew",
    )

    with pytest.raises(DiscoveryCycleUnavailable, match="awaiting policy"):
        await ensure_discovery_cycle_for_research(
            db_session,
            project=project,
            direction_key=cycle.direction_key,
            direction={"id": cycle.direction_key, "text": "Try yet again"},
            runtime_config={"discovery_admission_mode": "enforced"},
        )


@pytest.mark.asyncio
async def test_reservation_is_idempotent_and_settles_counters_once(db_session):
    _, cycle = await _project_and_cycle(
        db_session,
        task_credit_limit=2,
        attempt_credit_limit=2,
    )

    first = await reserve_discovery_admission(
        db_session,
        cycle_id=cycle.id,
        admission_key="task:one",
        mode="enforced",
    )
    duplicate = await reserve_discovery_admission(
        db_session,
        cycle_id=cycle.id,
        admission_key="task:one",
        mode="enforced",
    )

    assert first.allowed is True
    assert first.reason == "reserved"
    assert duplicate.allowed is True
    assert duplicate.idempotent is True
    assert duplicate.admission_id == first.admission_id
    assert cycle.reserved_task_credits == 1

    assert await consume_discovery_admission(
        db_session,
        cycle_id=cycle.id,
        admission_key="task:one",
    )
    assert await consume_discovery_admission(
        db_session,
        cycle_id=cycle.id,
        admission_key="task:one",
    )
    assert cycle.reserved_task_credits == 0
    assert cycle.consumed_task_credits == 1
    assert (
        await release_discovery_admission(
            db_session,
            cycle_id=cycle.id,
            admission_key="task:one",
        )
        is False
    )


@pytest.mark.asyncio
async def test_release_is_idempotent_and_does_not_consume(db_session):
    _, cycle = await _project_and_cycle(db_session)
    await reserve_discovery_admission(
        db_session,
        cycle_id=cycle.id,
        admission_key="task:released",
    )

    assert await release_discovery_admission(
        db_session,
        cycle_id=cycle.id,
        admission_key="task:released",
    )
    assert await release_discovery_admission(
        db_session,
        cycle_id=cycle.id,
        admission_key="task:released",
    )
    assert cycle.reserved_task_credits == 0
    assert cycle.consumed_task_credits == 0
    retried = await reserve_discovery_admission(
        db_session,
        cycle_id=cycle.id,
        admission_key="task:released",
    )
    assert retried.allowed is False
    assert retried.reason == "admission_released"


@pytest.mark.asyncio
async def test_enforced_limit_denies_without_mutation_while_shadow_records(db_session):
    _, cycle = await _project_and_cycle(db_session, task_credit_limit=1)
    await reserve_discovery_admission(
        db_session,
        cycle_id=cycle.id,
        admission_key="task:first",
        mode="enforced",
    )

    denied = await reserve_discovery_admission(
        db_session,
        cycle_id=cycle.id,
        admission_key="task:denied",
        mode="enforced",
    )
    assert denied.allowed is False
    assert denied.reason == "logical_task_credit_limit"
    assert cycle.reserved_task_credits == 1

    observed = await reserve_discovery_admission(
        db_session,
        cycle_id=cycle.id,
        admission_key="task:observed",
        mode="shadow",
    )
    assert observed.allowed is True
    assert observed.shadow_violation is True
    assert observed.reason == "logical_task_credit_limit"
    assert cycle.reserved_task_credits == 2
    ledger = (
        await db_session.execute(
            select(DiscoveryAdmission).where(
                DiscoveryAdmission.id == observed.admission_id
            )
        )
    ).scalar_one()
    assert ledger.admission_metadata["shadow_violation"] is True
    assert (
        ledger.admission_metadata["shadow_reason"]
        == "logical_task_credit_limit"
    )


@pytest.mark.asyncio
async def test_attempt_credit_is_accounted_separately(db_session):
    _, cycle = await _project_and_cycle(db_session, attempt_credit_limit=1)
    admitted = await reserve_discovery_admission(
        db_session,
        cycle_id=cycle.id,
        admission_key="attempt:one",
        admission_kind="attempt",
        mode="enforced",
    )
    denied = await reserve_discovery_admission(
        db_session,
        cycle_id=cycle.id,
        admission_key="attempt:two",
        admission_kind="attempt",
        mode="enforced",
    )
    assert admitted.allowed is True
    assert denied.allowed is False
    assert denied.reason == "attempt_credit_limit"
    assert cycle.reserved_attempt_credits == 1
    assert cycle.reserved_task_credits == 0


@pytest.mark.asyncio
async def test_same_row_fresh_attempts_charge_once_per_execution_incarnation(
    db_session,
):
    project, cycle = await _project_and_cycle(
        db_session,
        task_credit_limit=1,
        attempt_credit_limit=2,
    )
    task = Task(
        project_id=project.id,
        discovery_cycle_id=cycle.id,
        title="same-row retry",
        status=TaskStatus.QUEUED,
        payloads=[{"type": "create"}],
    )
    db_session.add(task)
    await db_session.flush()

    initial = await authorize_discovery_task_execution(
        db_session,
        task=task,
        mode="enforced",
    )
    assert initial.allowed is True
    assert cycle.consumed_task_credits == 1
    assert cycle.consumed_attempt_credits == 0

    reset_task_for_fresh_attempt(task)
    task.status = TaskStatus.QUEUED
    first_retry = await authorize_discovery_task_execution(
        db_session,
        task=task,
        mode="enforced",
    )
    duplicate_fence = await authorize_discovery_task_execution(
        db_session,
        task=task,
        mode="enforced",
    )

    assert first_retry.allowed is True
    assert duplicate_fence.allowed is True
    assert cycle.consumed_task_credits == 1
    assert cycle.consumed_attempt_credits == 1
    retry_rows = (
        (
            await db_session.execute(
                select(DiscoveryAdmission).where(
                    DiscoveryAdmission.cycle_id == cycle.id,
                    DiscoveryAdmission.task_id == task.id,
                    DiscoveryAdmission.admission_kind == "attempt",
                )
            )
        )
        .scalars()
        .all()
    )
    assert [row.admission_key for row in retry_rows] == [
        f"attempt:{task.id}:1"
    ]
    assert retry_rows[0].state == "consumed"

    reset_task_for_fresh_attempt(task)
    task.status = TaskStatus.QUEUED
    second_retry = await authorize_discovery_task_execution(
        db_session,
        task=task,
        mode="enforced",
    )
    assert second_retry.allowed is True
    assert cycle.consumed_attempt_credits == 2

    reset_task_for_fresh_attempt(task)
    task.status = TaskStatus.QUEUED
    task.latest_status_message = "Awaiting bounded retry admission"
    denied = await authorize_discovery_task_execution(
        db_session,
        task=task,
        mode="enforced",
    )
    assert denied.allowed is False
    assert denied.reason == "attempt_credit_limit"
    assert denied.terminal_denial is True
    assert task.status == TaskStatus.QUEUED
    assert task.latest_status_message == "Awaiting bounded retry admission"
    assert cycle.consumed_attempt_credits == 2
    assert cycle.reserved_attempt_credits == 0


@pytest.mark.asyncio
async def test_replacement_clone_charges_attempt_not_logical_task(db_session):
    project, cycle = await _project_and_cycle(
        db_session,
        task_credit_limit=1,
        attempt_credit_limit=1,
    )
    retry = Task(
        project_id=project.id,
        discovery_cycle_id=cycle.id,
        title="replacement retry",
        status=TaskStatus.QUEUED,
        payloads=[{"type": "create"}],
        context={
            "_fresh_attempt_started_at": _utcnow().isoformat(),
            "_discovery_attempt_ordinal": 1,
            "_attempt_number": 2,
        },
    )
    db_session.add(retry)
    await db_session.flush()

    admitted = await authorize_discovery_task_execution(
        db_session,
        task=retry,
        mode="enforced",
    )
    duplicate = await authorize_discovery_task_execution(
        db_session,
        task=retry,
        mode="enforced",
    )

    assert admitted.allowed is True
    assert duplicate.allowed is True
    assert cycle.consumed_task_credits == 0
    assert cycle.consumed_attempt_credits == 1
    ledger = (
        await db_session.execute(
            select(DiscoveryAdmission).where(
                DiscoveryAdmission.cycle_id == cycle.id,
                DiscoveryAdmission.task_id == retry.id,
            )
        )
    ).scalar_one()
    assert ledger.admission_kind == "attempt"
    assert ledger.admission_key == f"attempt:{retry.id}:1"
    assert cycle.reserved_attempt_credits == 0
    assert cycle.reserved_task_credits == 0


@pytest.mark.asyncio
async def test_closed_cycle_denies_fresh_retry_despite_prior_logical_admission(
    db_session,
):
    project, cycle = await _project_and_cycle(db_session)
    task = Task(
        project_id=project.id,
        discovery_cycle_id=cycle.id,
        title="drained initial execution",
        status=TaskStatus.QUEUED,
        payloads=[{"type": "create"}],
    )
    db_session.add(task)
    await db_session.flush()
    assert (
        await authorize_discovery_task_execution(
            db_session,
            task=task,
            mode="enforced",
        )
    ).allowed

    reset_task_for_fresh_attempt(task)
    task.status = TaskStatus.QUEUED
    cycle.state = "admission_closed"
    await db_session.flush()

    denied = await authorize_discovery_task_execution(
        db_session,
        task=task,
        mode="enforced",
    )
    assert denied.allowed is False
    assert denied.reason == "cycle_not_admitting"
    assert denied.terminal_denial is True
    assert cycle.consumed_task_credits == 1
    assert cycle.consumed_attempt_credits == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cycle_overrides", "reason"),
    [
        ({"state": "admission_closed"}, "cycle_not_admitting"),
        ({"deadline_at": _utcnow() - timedelta(seconds=1)}, "deadline_expired"),
    ],
)
async def test_new_reservations_respect_cycle_state_and_deadline(
    db_session,
    cycle_overrides,
    reason,
):
    _, cycle = await _project_and_cycle(db_session, **cycle_overrides)
    decision = await reserve_discovery_admission(
        db_session,
        cycle_id=cycle.id,
        admission_key=f"blocked:{reason}",
        mode="enforced",
    )
    assert decision.allowed is False
    assert decision.reason == reason
    assert cycle.reserved_task_credits == 0


@pytest.mark.asyncio
async def test_concurrency_backlogs_at_reservation_and_defers_at_execution(db_session):
    project, cycle = await _project_and_cycle(db_session, concurrency_limit=1)
    active = Task(
        project_id=project.id,
        discovery_cycle_id=cycle.id,
        title="active",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(active)
    await db_session.flush()

    queued = Task(
        project_id=project.id,
        discovery_cycle_id=cycle.id,
        title="queued",
    )
    db_session.add(queued)
    await db_session.flush()
    admitted = await reserve_discovery_admission(
        db_session,
        cycle_id=cycle.id,
        admission_key="task:next",
        task_id=queued.id,
        mode="enforced",
    )
    assert admitted.allowed is True
    assert admitted.reason == "reserved"

    dispatch = await authorize_discovery_task_execution(
        db_session,
        task=queued,
        mode="enforced",
    )
    assert dispatch.allowed is False
    assert dispatch.reason == "concurrency_limit"
    assert dispatch.terminal_denial is False
    assert cycle.reserved_task_credits == 0
    assert cycle.consumed_task_credits == 1
    active.status = TaskStatus.COMPLETED
    await db_session.flush()
    dispatch_after_slot_opens = await authorize_discovery_task_execution(
        db_session,
        task=queued,
        mode="enforced",
    )
    assert dispatch_after_slot_opens.allowed is True
    assert cycle.reserved_task_credits == 0
    assert cycle.consumed_task_credits == 1


@pytest.mark.asyncio
async def test_admitted_task_can_drain_after_cycle_closes_or_expires(db_session):
    project, cycle = await _project_and_cycle(db_session)
    task = Task(
        project_id=project.id,
        discovery_cycle_id=cycle.id,
        title="already admitted",
    )
    unadmitted = Task(
        project_id=project.id,
        discovery_cycle_id=cycle.id,
        title="not admitted",
    )
    db_session.add_all([task, unadmitted])
    await db_session.flush()
    await reserve_discovery_admission(
        db_session,
        cycle_id=cycle.id,
        admission_key="task:drain",
        task_id=task.id,
        mode="enforced",
    )
    cycle.state = "admission_closed"
    cycle.deadline_at = _utcnow() - timedelta(minutes=1)
    await db_session.flush()
    before = (cycle.reserved_task_credits, cycle.revision)

    admitted = await validate_discovery_dispatch_authority(
        db_session,
        cycle_id=cycle.id,
        task_id=task.id,
        mode="enforced",
    )
    missing = await validate_discovery_dispatch_authority(
        db_session,
        cycle_id=cycle.id,
        task_id=unadmitted.id,
        mode="enforced",
    )
    shadow_missing = await validate_discovery_dispatch_authority(
        db_session,
        cycle_id=cycle.id,
        task_id=unadmitted.id,
        mode="shadow",
    )
    assert admitted.allowed is True
    assert admitted.reason == "authority_valid"
    assert missing.allowed is False
    assert missing.reason == "admission_missing"
    assert shadow_missing.allowed is True
    assert shadow_missing.shadow_violation is True
    assert (cycle.reserved_task_credits, cycle.revision) == before


@pytest.mark.asyncio
async def test_retry_attempt_admission_grants_dispatch_authority(db_session):
    project, cycle = await _project_and_cycle(db_session)
    retry = Task(
        project_id=project.id,
        discovery_cycle_id=cycle.id,
        title="retry",
    )
    db_session.add(retry)
    await db_session.flush()
    await reserve_discovery_admission(
        db_session,
        cycle_id=cycle.id,
        admission_key="attempt:retry",
        admission_kind="attempt",
        task_id=retry.id,
        mode="enforced",
    )

    decision = await validate_discovery_dispatch_authority(
        db_session,
        cycle_id=cycle.id,
        task_id=retry.id,
        mode="enforced",
    )

    assert decision.allowed is True
    assert decision.admission_kind == "attempt"


@pytest.mark.asyncio
async def test_lineage_resolves_request_ancestors_and_active_direction(db_session):
    project, cycle = await _project_and_cycle(db_session)
    parent = Task(
        project_id=project.id,
        discovery_cycle_id=cycle.id,
        title="cycle parent",
    )
    child = Task(project_id=project.id, parent_task_id=parent.id, title="child")
    db_session.add_all([parent, child])
    await db_session.flush()
    request = StrategicSpawnRequest(
        project_id=project.id,
        parent_task_id=parent.id,
        discovery_cycle_id=cycle.id,
        idempotency_key=f"request-{uuid.uuid4()}",
        task_type="create",
        specification={},
        specification_hash="a" * 64,
    )
    db_session.add(request)
    await db_session.flush()

    assert (
        await resolve_discovery_cycle_lineage(db_session, task_id=child.id)
        == cycle.id
    )
    assert (
        await resolve_discovery_cycle_lineage(
            db_session,
            strategic_spawn_request_id=request.id,
        )
        == cycle.id
    )
    assert (
        await resolve_discovery_cycle_lineage(
            db_session,
            project_id=project.id,
            direction_key=cycle.direction_key,
        )
        == cycle.id
    )


@pytest.mark.asyncio
async def test_execution_backstop_stamps_and_admits_unstamped_grandchild(db_session):
    project, cycle = await _project_and_cycle(db_session)
    root = Task(
        project_id=project.id,
        discovery_cycle_id=cycle.id,
        title="research parent",
    )
    db_session.add(root)
    await db_session.flush()
    child = Task(
        project_id=project.id,
        parent_task_id=root.id,
        title="plan child",
    )
    db_session.add(child)
    await db_session.flush()
    grandchild = Task(
        project_id=project.id,
        parent_task_id=child.id,
        title="plan implementation grandchild",
    )
    db_session.add(grandchild)
    await db_session.flush()

    decision = await authorize_discovery_task_execution(
        db_session,
        task=grandchild,
        mode="enforced",
    )

    assert decision.allowed is True
    assert grandchild.discovery_cycle_id == cycle.id
    admission = (
        await db_session.execute(
            select(DiscoveryAdmission).where(
                DiscoveryAdmission.cycle_id == cycle.id,
                DiscoveryAdmission.task_id == grandchild.id,
            )
        )
    ).scalar_one()
    assert admission.admission_key == f"task:{grandchild.id}"
    assert admission.state == "consumed"
    assert cycle.reserved_task_credits == 0
    assert cycle.consumed_task_credits == 1


@pytest.mark.asyncio
async def test_execution_backstop_terminally_denies_unadmitted_closed_descendant(
    db_session,
):
    project, cycle = await _project_and_cycle(
        db_session,
        state="admission_closed",
    )
    root = Task(
        project_id=project.id,
        discovery_cycle_id=cycle.id,
        title="research parent",
    )
    db_session.add(root)
    await db_session.flush()
    grandchild = Task(
        project_id=project.id,
        parent_task_id=root.id,
        title="late plan descendant",
    )
    db_session.add(grandchild)
    await db_session.flush()

    decision = await authorize_discovery_task_execution(
        db_session,
        task=grandchild,
        mode="enforced",
    )

    assert grandchild.discovery_cycle_id == cycle.id
    assert decision.allowed is False
    assert decision.reason == "cycle_not_admitting"
    assert decision.terminal_denial is True


@pytest.mark.asyncio
async def test_review_freeze_is_invariant_even_in_shadow_mode(db_session):
    project, cycle = await _project_and_cycle(db_session, state="reviewed")
    task = Task(
        project_id=project.id,
        discovery_cycle_id=cycle.id,
        title="late parked descendant",
    )
    db_session.add(task)
    await db_session.flush()

    decision = await authorize_discovery_task_execution(
        db_session,
        task=task,
        mode="shadow",
    )

    assert decision.allowed is False
    assert decision.reason == "cycle_review_frozen"
    assert decision.terminal_denial is True


def test_runtime_mode_defaults_to_shadow_and_supports_enforcement():
    assert discovery_admission_mode_from_runtime({}) == "shadow"
    assert (
        discovery_admission_mode_from_runtime(
            {"discovery_admission_mode": "enforced"}
        )
        == "enforced"
    )
