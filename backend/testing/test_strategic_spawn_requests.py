"""Focused tests for durable, unified Strategic task dispatch."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlmodel import select

from app.depends_on_provenance import get_structural_depends_on_ids
from app.models import (
    CompletionDebtEntry,
    Project,
    StrategicSpawnRequest,
    StrategicWatch,
    Task,
    TaskStatus,
)
from app.strategic_spawn_requests import (
    MAX_DRAIN_PER_PASS,
    cancel_deferred_spawn_request,
    drain_deferred_spawn_requests,
    drain_due_strategic_spawn_requests,
    submit_strategic_spawn,
)
from app.time_utils import _utcnow


async def _owner(db_session):
    project = Project(name=f"Spawn requests {uuid.uuid4().hex[:8]}", user_id="test-user")
    db_session.add(project)
    await db_session.flush()
    owner = Task(
        project_id=project.id,
        title="Resident Strategic",
        created_by="STRATEGIC_AGENT",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "strategic_review"}],
    )
    db_session.add(owner)
    await db_session.commit()
    return project, owner


def _spec(title: str, *, watch: bool = True) -> dict:
    return {
        "task": {
            "title": title,
            "description": "bounded work",
            "payloads": [{"type": "create", "prompt": "implement it"}],
            "context": {"spawned_by": "strategic_direct"},
            "priority": 50,
            "watch": watch,
        }
    }


async def _next_owner(db_session, project: Project, label: str) -> Task:
    owner = Task(
        project_id=project.id,
        title=label,
        created_by="STRATEGIC_AGENT",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "strategic_review"}],
    )
    db_session.add(owner)
    await db_session.commit()
    return owner


async def _seed_due_queue(
    db_session,
    *,
    due_at,
    created_at,
    recent_dispatch_at=None,
):
    project, owner = await _owner(db_session)
    if recent_dispatch_at is not None:
        db_session.add(
            StrategicSpawnRequest(
                project_id=project.id,
                parent_task_id=owner.id,
                idempotency_key=f"occupy-{uuid.uuid4().hex}",
                task_type="create",
                specification=_spec("Occupy window", watch=False),
                specification_hash="a" * 64,
                state="dispatched",
                dispatched_at=recent_dispatch_at,
            )
        )
    request = StrategicSpawnRequest(
        project_id=project.id,
        parent_task_id=owner.id,
        idempotency_key=f"due-{uuid.uuid4().hex}",
        task_type="create",
        specification=_spec(f"Due work {project.id}", watch=False),
        specification_hash="b" * 64,
        state="deferred",
        admission_reason="project_spawn_burst",
        not_before=due_at,
        created_at=created_at,
    )
    db_session.add(request)
    await db_session.commit()
    return project, request


@pytest.mark.asyncio
async def test_spawn_request_dispatches_once_and_registers_one_watch(db_session):
    project, owner = await _owner(db_session)
    first = await submit_strategic_spawn(
        db_session,
        project_id=project.id,
        parent_task_id=owner.id,
        task_type="create",
        specification=_spec("Implement verified fix"),
        idempotency_key="stable-request",
        direction_id=None,
    )
    replay = await submit_strategic_spawn(
        db_session,
        project_id=project.id,
        parent_task_id=owner.id,
        task_type="create",
        specification=_spec("Implement verified fix"),
        idempotency_key="stable-request",
        direction_id=None,
    )

    assert first.request.state == "dispatched"
    assert replay.replayed is True
    assert replay.task.id == first.task.id
    assert len(
        (
            await db_session.execute(
                select(StrategicSpawnRequest).where(
                    StrategicSpawnRequest.parent_task_id == owner.id
                )
            )
        )
        .scalars()
        .all()
    ) == 1
    watches = (
        (
            await db_session.execute(
                select(StrategicWatch).where(
                    StrategicWatch.created_by_task_id == owner.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert [watch.target_task_id for watch in watches] == [first.task.id]


@pytest.mark.asyncio
async def test_reused_idempotency_key_rejects_different_specification(db_session):
    project, owner = await _owner(db_session)
    await submit_strategic_spawn(
        db_session,
        project_id=project.id,
        parent_task_id=owner.id,
        task_type="create",
        specification=_spec("First"),
        idempotency_key="same-key",
        direction_id=None,
    )
    with pytest.raises(ValueError, match="different spawn specification"):
        await submit_strategic_spawn(
            db_session,
            project_id=project.id,
            parent_task_id=owner.id,
            task_type="create",
            specification=_spec("Second"),
            idempotency_key="same-key",
            direction_id=None,
        )


@pytest.mark.asyncio
async def test_active_work_deduplicates_across_resident_generations(db_session):
    project, first_owner = await _owner(db_session)
    second_owner = await _next_owner(db_session, project, "Replacement Resident")
    first_spec = _spec("Implement bounded direction work")
    first_spec["task"]["context"]["parent_strategic_task_id"] = str(first_owner.id)
    second_spec = _spec("Implement bounded direction work")
    second_spec["task"]["context"]["parent_strategic_task_id"] = str(second_owner.id)

    first = await submit_strategic_spawn(
        db_session,
        project_id=project.id,
        parent_task_id=first_owner.id,
        task_type="create",
        specification=first_spec,
        idempotency_key="direction-work-unit",
        direction_id="d-product-loop",
    )
    replay = await submit_strategic_spawn(
        db_session,
        project_id=project.id,
        parent_task_id=second_owner.id,
        task_type="create",
        specification=second_spec,
        idempotency_key="direction-work-unit",
        direction_id="d-product-loop",
    )

    assert replay.replayed is True
    assert replay.task.id == first.task.id
    assert replay.request.parent_task_id == second_owner.id
    assert replay.request.admission_reason == "active_work_deduplicated"
    assert first.task.idempotency_key.startswith("strategic-direct:")
    watches = (
        (
            await db_session.execute(
                select(StrategicWatch).where(
                    StrategicWatch.target_task_id == first.task.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert {watch.created_by_task_id for watch in watches} == {
        first_owner.id,
        second_owner.id,
    }


@pytest.mark.asyncio
async def test_terminal_work_releases_identity_for_new_generation_retry(db_session):
    project, first_owner = await _owner(db_session)
    second_owner = await _next_owner(db_session, project, "Retrying Resident")
    first = await submit_strategic_spawn(
        db_session,
        project_id=project.id,
        parent_task_id=first_owner.id,
        task_type="create",
        specification=_spec("Retryable bounded work", watch=False),
        idempotency_key="retryable-work-unit",
        direction_id="d-retry",
    )
    first.task.status = TaskStatus.FAILED
    db_session.add(first.task)
    await db_session.commit()

    retry = await submit_strategic_spawn(
        db_session,
        project_id=project.id,
        parent_task_id=second_owner.id,
        task_type="create",
        specification=_spec("Retryable bounded work", watch=False),
        idempotency_key="retryable-work-unit",
        direction_id="d-retry",
    )

    assert retry.replayed is False
    assert retry.task.id != first.task.id
    assert retry.task.idempotency_key == first.task.idempotency_key


@pytest.mark.asyncio
async def test_cross_generation_work_key_rejects_different_active_specification(
    db_session,
):
    project, first_owner = await _owner(db_session)
    second_owner = await _next_owner(db_session, project, "Conflicting Resident")
    await submit_strategic_spawn(
        db_session,
        project_id=project.id,
        parent_task_id=first_owner.id,
        task_type="create",
        specification=_spec("Original bounded work", watch=False),
        idempotency_key="shared-work-unit",
        direction_id="d-conflict",
    )

    with pytest.raises(ValueError, match="different active work"):
        await submit_strategic_spawn(
            db_session,
            project_id=project.id,
            parent_task_id=second_owner.id,
            task_type="create",
            specification=_spec("Conflicting bounded work", watch=False),
            idempotency_key="shared-work-unit",
            direction_id="d-conflict",
        )


@pytest.mark.asyncio
async def test_direct_initiative_root_uses_existing_enforced_budget_backstop(
    db_session, mocker
):
    from app.shared_state import runtime_config

    project, owner = await _owner(db_session)
    mocker.patch.dict(
        runtime_config,
        {
            "autonomous_spawn_budget_mode": "enforce",
            "max_global_autonomous_initiative_tasks_per_hour": 1,
            "max_global_autonomous_tasks_per_hour": 500,
        },
    )

    first = await submit_strategic_spawn(
        db_session,
        project_id=project.id,
        parent_task_id=owner.id,
        task_type="create",
        specification=_spec("First budgeted direct root", watch=False),
        idempotency_key="first-budgeted-direct-root",
        direction_id="d-budget",
    )
    submission = await submit_strategic_spawn(
        db_session,
        project_id=project.id,
        parent_task_id=owner.id,
        task_type="create",
        specification=_spec("Budgeted direct root", watch=False),
        idempotency_key="budgeted-direct-root",
        direction_id="d-budget",
    )

    assert first.task is not None
    assert submission.task is None
    assert submission.request.state == "deferred"
    assert submission.request.admission_reason == "initiative_rate_limit_exceeded"
    assert submission.request.not_before is not None


@pytest.mark.asyncio
async def test_abuse_burst_defers_then_drains_fifo(db_session):
    project, owner = await _owner(db_session)
    first = await submit_strategic_spawn(
        db_session,
        project_id=project.id,
        parent_task_id=owner.id,
        task_type="create",
        specification=_spec("First", watch=False),
        idempotency_key="first",
        direction_id=None,
        burst_cap=1,
        burst_window_minutes=60,
    )
    second = await submit_strategic_spawn(
        db_session,
        project_id=project.id,
        parent_task_id=owner.id,
        task_type="create",
        specification=_spec("Second", watch=False),
        idempotency_key="second",
        direction_id=None,
        burst_cap=1,
        burst_window_minutes=60,
    )
    assert first.task is not None
    assert second.task is None
    assert second.request.state == "deferred"

    first.request.dispatched_at = _utcnow() - timedelta(minutes=61)
    second.request.not_before = _utcnow() - timedelta(seconds=1)
    db_session.add(first.request)
    db_session.add(second.request)
    await db_session.commit()

    drained = await drain_deferred_spawn_requests(
        db_session,
        project_id=project.id,
        burst_cap=1,
        burst_window_minutes=60,
    )
    await db_session.commit()
    assert [row.id for row in drained] == [second.request.id]
    await db_session.refresh(second.request)
    assert second.request.state == "dispatched"
    assert second.request.task_id is not None


@pytest.mark.asyncio
async def test_paused_project_rejects_new_work_and_preserves_deferred_intent(
    db_session,
):
    project, owner = await _owner(db_session)
    first = await submit_strategic_spawn(
        db_session,
        project_id=project.id,
        parent_task_id=owner.id,
        task_type="create",
        specification=_spec("Occupy the window", watch=False),
        idempotency_key="pause-window",
        direction_id=None,
        burst_cap=1,
        burst_window_minutes=60,
    )
    deferred = await submit_strategic_spawn(
        db_session,
        project_id=project.id,
        parent_task_id=owner.id,
        task_type="create",
        specification=_spec("Preserve this intent", watch=False),
        idempotency_key="pause-deferred",
        direction_id=None,
        burst_cap=1,
        burst_window_minutes=60,
    )
    first.request.dispatched_at = _utcnow() - timedelta(minutes=61)
    deferred.request.not_before = _utcnow() - timedelta(seconds=1)
    project.paused = True
    db_session.add(first.request)
    db_session.add(deferred.request)
    db_session.add(project)
    await db_session.commit()

    with pytest.raises(ValueError, match="project is paused"):
        await submit_strategic_spawn(
            db_session,
            project_id=project.id,
            parent_task_id=owner.id,
            task_type="create",
            specification=_spec("Do not author this", watch=False),
            idempotency_key="pause-new-work",
            direction_id=None,
        )
    assert (
        await drain_deferred_spawn_requests(
            db_session,
            project_id=project.id,
            burst_cap=1,
            burst_window_minutes=60,
        )
        == []
    )
    await db_session.refresh(deferred.request)
    assert deferred.request.state == "deferred"
    assert deferred.request.task_id is None

    project.paused = False
    db_session.add(project)
    await db_session.commit()
    drained = await drain_deferred_spawn_requests(
        db_session,
        project_id=project.id,
        burst_cap=1,
        burst_window_minutes=60,
    )
    assert [row.id for row in drained] == [deferred.request.id]


@pytest.mark.asyncio
async def test_cancelled_deferred_request_never_drains(db_session):
    project, owner = await _owner(db_session)
    first = await submit_strategic_spawn(
        db_session,
        project_id=project.id,
        parent_task_id=owner.id,
        task_type="create",
        specification=_spec("First", watch=False),
        idempotency_key="first",
        direction_id=None,
        burst_cap=1,
        burst_window_minutes=60,
    )
    deferred = await submit_strategic_spawn(
        db_session,
        project_id=project.id,
        parent_task_id=owner.id,
        task_type="create",
        specification=_spec("Cancelled", watch=False),
        idempotency_key="cancelled",
        direction_id=None,
        burst_cap=1,
        burst_window_minutes=60,
    )
    assert deferred.request.state == "deferred"

    await cancel_deferred_spawn_request(
        db_session,
        project_id=project.id,
        request_id=deferred.request.id,
    )
    first.request.dispatched_at = _utcnow() - timedelta(minutes=61)
    db_session.add(first.request)
    await db_session.commit()

    drained = await drain_deferred_spawn_requests(
        db_session,
        project_id=project.id,
        burst_cap=1,
        burst_window_minutes=60,
    )
    await db_session.commit()
    assert drained == []
    await db_session.refresh(deferred.request)
    assert deferred.request.state == "cancelled"
    assert deferred.request.task_id is None

    with pytest.raises(ValueError, match=r"already terminal \(cancelled\)"):
        await submit_strategic_spawn(
            db_session,
            project_id=project.id,
            parent_task_id=owner.id,
            task_type="create",
            specification=_spec("Cancelled", watch=False),
            idempotency_key="cancelled",
            direction_id=None,
            burst_cap=1,
            burst_window_minutes=60,
        )


@pytest.mark.asyncio
async def test_orchestrator_sweep_orders_projects_by_oldest_due_request(db_session):
    now = _utcnow()
    paused_project, paused = await _seed_due_queue(
        db_session,
        due_at=now - timedelta(minutes=4),
        created_at=now - timedelta(minutes=14),
    )
    paused_project.paused = True
    db_session.add(paused_project)
    await db_session.commit()
    newest_project, newest = await _seed_due_queue(
        db_session,
        due_at=now - timedelta(minutes=1),
        created_at=now - timedelta(minutes=11),
    )
    oldest_project, oldest = await _seed_due_queue(
        db_session,
        due_at=now - timedelta(minutes=3),
        created_at=now - timedelta(minutes=13),
    )
    middle_project, middle = await _seed_due_queue(
        db_session,
        due_at=now - timedelta(minutes=2),
        created_at=now - timedelta(minutes=12),
    )

    drained = await drain_due_strategic_spawn_requests(
        db_session,
        burst_cap=1,
        burst_window_minutes=60,
        max_projects=2,
    )
    await db_session.commit()

    assert [row.id for row in drained] == [oldest.id, middle.id]
    await db_session.refresh(paused)
    await db_session.refresh(newest)
    assert paused.state == "deferred"
    assert paused.task_id is None
    assert newest.state == "deferred"
    assert newest.task_id is None
    assert {row.project_id for row in drained} == {
        oldest_project.id,
        middle_project.id,
    }
    assert paused_project.id not in {row.project_id for row in drained}
    assert newest_project.id not in {row.project_id for row in drained}


@pytest.mark.asyncio
async def test_large_old_queue_yields_to_next_project_after_one_bounded_pass(
    db_session,
):
    now = _utcnow()
    large_project, first_large = await _seed_due_queue(
        db_session,
        due_at=now - timedelta(minutes=30),
        created_at=now - timedelta(minutes=40),
    )
    for offset in range(MAX_DRAIN_PER_PASS):
        db_session.add(
            StrategicSpawnRequest(
                project_id=large_project.id,
                parent_task_id=first_large.parent_task_id,
                idempotency_key=f"large-{offset}-{uuid.uuid4().hex}",
                task_type="create",
                specification=_spec(f"Large queue item {offset}", watch=False),
                specification_hash="d" * 64,
                state="deferred",
                admission_reason="project_spawn_burst",
                not_before=now - timedelta(minutes=30),
                created_at=now - timedelta(minutes=39) + timedelta(seconds=offset),
            )
        )
    next_project, next_request = await _seed_due_queue(
        db_session,
        due_at=now - timedelta(minutes=20),
        created_at=now - timedelta(minutes=20),
    )

    first_tick = await drain_due_strategic_spawn_requests(
        db_session,
        burst_cap=100,
        burst_window_minutes=60,
        max_projects=1,
    )
    await db_session.commit()
    assert len(first_tick) == MAX_DRAIN_PER_PASS
    assert {row.project_id for row in first_tick} == {large_project.id}
    remaining_large = (
        (
            await db_session.execute(
                select(StrategicSpawnRequest).where(
                    StrategicSpawnRequest.project_id == large_project.id,
                    StrategicSpawnRequest.state == "deferred",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(remaining_large) == 1
    assert remaining_large[0].not_before > now

    second_tick = await drain_due_strategic_spawn_requests(
        db_session,
        burst_cap=100,
        burst_window_minutes=60,
        max_projects=1,
    )
    await db_session.commit()
    assert [row.id for row in second_tick] == [next_request.id]
    assert second_tick[0].project_id == next_project.id


@pytest.mark.asyncio
async def test_no_room_project_is_advanced_and_does_not_starve_next_project(
    db_session,
):
    now = _utcnow()
    blocked_project, blocked = await _seed_due_queue(
        db_session,
        due_at=now - timedelta(minutes=3),
        created_at=now - timedelta(minutes=13),
        recent_dispatch_at=now - timedelta(minutes=1),
    )
    ready_project, ready = await _seed_due_queue(
        db_session,
        due_at=now - timedelta(minutes=2),
        created_at=now - timedelta(minutes=12),
    )

    first_tick = await drain_due_strategic_spawn_requests(
        db_session,
        burst_cap=1,
        burst_window_minutes=60,
        max_projects=1,
    )
    await db_session.commit()
    assert first_tick == []
    await db_session.refresh(blocked)
    assert blocked.state == "deferred"
    assert blocked.task_id is None
    assert blocked.not_before > now

    second_tick = await drain_due_strategic_spawn_requests(
        db_session,
        burst_cap=1,
        burst_window_minutes=60,
        max_projects=1,
    )
    await db_session.commit()
    assert [row.id for row in second_tick] == [ready.id]
    assert second_tick[0].project_id == ready_project.id
    assert second_tick[0].project_id != blocked_project.id


@pytest.mark.asyncio
async def test_orchestrator_sweep_drains_without_heartbeat_and_links_debt_once(
    db_session,
):
    project, owner = await _owner(db_session)
    debt = CompletionDebtEntry(
        project_id=project.id,
        kind="capability_trial",
        dedupe_key=f"strategic-spawn:{uuid.uuid4()}",
        title="Follow through on accepted Strategic work",
        detail={},
    )
    db_session.add(debt)
    await db_session.commit()

    first = await submit_strategic_spawn(
        db_session,
        project_id=project.id,
        parent_task_id=owner.id,
        task_type="create",
        specification=_spec("Occupy the abuse window", watch=False),
        idempotency_key="occupy-window",
        direction_id=None,
        burst_cap=1,
        burst_window_minutes=60,
    )
    deferred_spec = _spec("Deferred linked work", watch=False)
    deferred_spec["debt_id"] = str(debt.id)
    deferred = await submit_strategic_spawn(
        db_session,
        project_id=project.id,
        parent_task_id=owner.id,
        task_type="create",
        specification=deferred_spec,
        idempotency_key="deferred-linked",
        direction_id=None,
        burst_cap=1,
        burst_window_minutes=60,
    )
    assert deferred.request.state == "deferred"

    first.request.dispatched_at = _utcnow() - timedelta(minutes=61)
    deferred.request.not_before = _utcnow() - timedelta(seconds=1)
    db_session.add(first.request)
    db_session.add(deferred.request)
    await db_session.commit()

    drained = await drain_due_strategic_spawn_requests(
        db_session,
        burst_cap=1,
        burst_window_minutes=60,
    )
    await db_session.commit()
    assert [row.id for row in drained] == [deferred.request.id]

    # A later orchestrator tick is a no-op and the idempotent debt ledger keeps
    # exactly one forward pointer for the task created by the deferred request.
    assert (
        await drain_due_strategic_spawn_requests(
            db_session,
            burst_cap=1,
            burst_window_minutes=60,
        )
    ) == []
    await db_session.commit()
    await db_session.refresh(deferred.request)
    await db_session.refresh(debt)
    linked = (debt.detail or {}).get("linked_tasks") or []
    assert [row["task_id"] for row in linked] == [str(deferred.request.task_id)]


@pytest.mark.asyncio
async def test_immediate_spawn_carries_depends_on_at_task_creation(db_session):
    """The ordering edge is set when the Task row is built, not patched after.

    A create-then-patch would publish a QUEUED successor with no dependencies
    for the window between the two commits, letting the dispatch loop claim it
    ahead of its prerequisite.
    """
    project, owner = await _owner(db_session)
    prereq = Task(
        project_id=project.id,
        title="Prerequisite",
        created_by="STRATEGIC_AGENT",
        status=TaskStatus.QUEUED,
        payloads=[{"type": "create", "prompt": "first"}],
    )
    db_session.add(prereq)
    await db_session.flush()

    spec = _spec("Successor", watch=False)
    spec["task"]["depends_on"] = [str(prereq.id)]
    submission = await submit_strategic_spawn(
        db_session,
        project_id=project.id,
        parent_task_id=owner.id,
        task_type="create",
        specification=spec,
        idempotency_key="immediate-ordered",
        direction_id=None,
        burst_cap=10,
        burst_window_minutes=60,
    )
    await db_session.commit()

    assert submission.task is not None
    assert submission.task.status == TaskStatus.QUEUED
    assert [str(d) for d in (submission.task.depends_on or [])] == [str(prereq.id)]
    assert get_structural_depends_on_ids(submission.task) == [str(prereq.id)]


@pytest.mark.asyncio
async def test_deferred_spawn_keeps_depends_on_when_later_drained(db_session):
    """Deferral is a normal outcome; it must not drop the ordering edge.

    The router only ever saw the immediate (200) path, so a burst-capped spawn
    used to be created by the drain with ``depends_on == []`` and dispatched
    unordered — the exact failure this feature exists to prevent.
    """
    project, owner = await _owner(db_session)
    prereq = Task(
        project_id=project.id,
        title="Prerequisite",
        created_by="STRATEGIC_AGENT",
        status=TaskStatus.QUEUED,
        payloads=[{"type": "create", "prompt": "first"}],
    )
    db_session.add(prereq)
    await db_session.flush()

    first = await submit_strategic_spawn(
        db_session,
        project_id=project.id,
        parent_task_id=owner.id,
        task_type="create",
        specification=_spec("Occupy the window", watch=False),
        idempotency_key="burst-occupier",
        direction_id=None,
        burst_cap=1,
        burst_window_minutes=60,
    )
    assert first.task is not None

    spec = _spec("Deferred successor", watch=False)
    spec["task"]["depends_on"] = [str(prereq.id), str(prereq.id)]
    deferred = await submit_strategic_spawn(
        db_session,
        project_id=project.id,
        parent_task_id=owner.id,
        task_type="create",
        specification=spec,
        idempotency_key="deferred-ordered",
        direction_id=None,
        burst_cap=1,
        burst_window_minutes=60,
    )
    assert deferred.task is None
    assert deferred.request.state == "deferred"

    first.request.dispatched_at = _utcnow() - timedelta(minutes=61)
    deferred.request.not_before = _utcnow() - timedelta(seconds=1)
    db_session.add(first.request)
    db_session.add(deferred.request)
    await db_session.commit()

    drained = await drain_deferred_spawn_requests(
        db_session,
        project_id=project.id,
        burst_cap=1,
        burst_window_minutes=60,
    )
    await db_session.commit()
    assert [row.id for row in drained] == [deferred.request.id]

    await db_session.refresh(deferred.request)
    spawned = await db_session.get(Task, deferred.request.task_id)
    assert spawned is not None
    # Deduplicated, and marked structural so cleanup paths keep the edge.
    assert [str(d) for d in (spawned.depends_on or [])] == [str(prereq.id)]
    assert get_structural_depends_on_ids(spawned) == [str(prereq.id)]
