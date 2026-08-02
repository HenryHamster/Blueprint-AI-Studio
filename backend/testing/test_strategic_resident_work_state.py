"""Focused coverage for the Resident's semantic autonomous-wake read model."""

from datetime import timedelta

import pytest

from app.models import Project, ProjectMessage, Task, TaskStatus, _utcnow
from app.strategic_resident_session import _compute_resident_work_state


async def _project(db_session) -> Project:
    project = Project(name="Resident semantic wake", user_id="wake-test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    return project


def _strategic_task(
    project: Project,
    title: str,
    *,
    status: TaskStatus = TaskStatus.IN_PROGRESS,
    parent_task_id=None,
) -> Task:
    return Task(
        project_id=project.id,
        title=title,
        payloads=[{"type": "create"}],
        status=status,
        created_by="STRATEGIC_AGENT",
        parent_task_id=parent_task_id,
    )


@pytest.mark.asyncio
async def test_work_state_ignores_host_heartbeat_and_intermediate_progress(
    db_session,
):
    project = await _project(db_session)
    host = _strategic_task(project, "Long-lived Resident")
    db_session.add(host)
    await db_session.flush()
    worker = _strategic_task(project, "Owned implementation", parent_task_id=host.id)
    db_session.add(worker)
    await db_session.commit()

    rev_before, summary_before = await _compute_resident_work_state(
        db_session,
        project.id,
        resident_task_id=host.id,
    )
    assert rev_before is None
    assert summary_before["in_flight"] == 1

    # Both the generic Resident host heartbeat and ordinary agent progress dirty
    # Task.updated_at. Neither presents a strategic decision, and moving between
    # active execution phases must remain inside the same coalesced frontier.
    host.last_heartbeat = _utcnow()
    host.updated_at = host.updated_at + timedelta(minutes=1)
    worker.latest_status_message = "Still implementing"
    worker.status = TaskStatus.EVALUATING
    worker.updated_at = worker.updated_at + timedelta(minutes=2)
    db_session.add_all([host, worker])
    await db_session.commit()

    rev_after, summary_after = await _compute_resident_work_state(
        db_session,
        project.id,
        resident_task_id=host.id,
    )
    assert rev_after == rev_before
    assert summary_after["in_flight"] == 1


@pytest.mark.asyncio
async def test_parallel_successes_coalesce_at_quiescent_barrier(db_session):
    project = await _project(db_session)
    host = _strategic_task(project, "Long-lived Resident")
    db_session.add(host)
    await db_session.flush()
    first = _strategic_task(project, "Parallel work A", parent_task_id=host.id)
    second = _strategic_task(project, "Parallel work B", parent_task_id=host.id)
    db_session.add_all([first, second])
    await db_session.commit()

    initial_rev, initial = await _compute_resident_work_state(
        db_session,
        project.id,
        resident_task_id=host.id,
    )
    assert initial_rev is None
    assert initial["in_flight"] == 2

    first.status = TaskStatus.COMPLETED
    first.completed_at = _utcnow()
    db_session.add(first)
    await db_session.commit()
    intermediate_rev, intermediate = await _compute_resident_work_state(
        db_session,
        project.id,
        resident_task_id=host.id,
    )
    assert intermediate_rev == initial_rev
    assert intermediate["in_flight"] == 1

    second.status = TaskStatus.COMPLETED
    second.completed_at = _utcnow() + timedelta(seconds=1)
    db_session.add(second)
    await db_session.commit()
    barrier_rev, barrier = await _compute_resident_work_state(
        db_session,
        project.id,
        resident_task_id=host.id,
    )
    assert barrier_rev is not None
    assert barrier_rev != intermediate_rev
    assert barrier["in_flight"] == 0
    assert barrier["quiescent"] == 1


@pytest.mark.asyncio
async def test_blocker_and_failure_wake_before_parallel_frontier_drains(db_session):
    project = await _project(db_session)
    host = _strategic_task(project, "Long-lived Resident")
    db_session.add(host)
    await db_session.flush()
    blocked = _strategic_task(project, "Blocked work", parent_task_id=host.id)
    sibling = _strategic_task(project, "Still running", parent_task_id=host.id)
    db_session.add_all([blocked, sibling])
    await db_session.commit()

    blocked.status = TaskStatus.BLOCKED
    blocked.blocked_at = _utcnow()
    blocked.blocked_reason = "A real operator-visible precondition is missing"
    db_session.add(blocked)
    await db_session.commit()
    blocked_rev, blocked_summary = await _compute_resident_work_state(
        db_session,
        project.id,
        resident_task_id=host.id,
    )
    assert blocked_rev is not None
    assert blocked_summary["blocked"] == 1
    # BLOCKED owns no runner slot; only the active sibling is still in flight.
    assert blocked_summary["in_flight"] == 1

    # Status chatter on the already-known blocker does not buy another turn.
    blocked.latest_status_message = "Still waiting on the same precondition"
    blocked.updated_at = blocked.updated_at + timedelta(minutes=1)
    db_session.add(blocked)
    await db_session.commit()
    unchanged_rev, _ = await _compute_resident_work_state(
        db_session,
        project.id,
        resident_task_id=host.id,
    )
    assert unchanged_rev == blocked_rev

    sibling.status = TaskStatus.PARKED
    sibling.parked_at = _utcnow() + timedelta(minutes=1)
    db_session.add(sibling)
    await db_session.commit()
    parked_rev, parked_summary = await _compute_resident_work_state(
        db_session,
        project.id,
        resident_task_id=host.id,
    )
    assert parked_rev != blocked_rev
    assert parked_summary["parked"] == 1
    assert parked_summary["in_flight"] == 0
    assert parked_summary["quiescent"] == 1

    sibling.status = TaskStatus.IN_PROGRESS
    db_session.add(sibling)
    await db_session.commit()

    blocked.status = TaskStatus.FAILED
    blocked.completed_at = _utcnow() + timedelta(minutes=2)
    db_session.add(blocked)
    await db_session.commit()
    failed_rev, failed_summary = await _compute_resident_work_state(
        db_session,
        project.id,
        resident_task_id=host.id,
    )
    assert failed_rev != blocked_rev
    assert failed_summary["failed"] == 1
    assert failed_summary["in_flight"] == 1


@pytest.mark.asyncio
async def test_only_explicit_internal_barrier_message_wakes(db_session):
    project = await _project(db_session)
    host = _strategic_task(project, "Long-lived Resident")
    worker = _strategic_task(project, "Still running")
    db_session.add_all([host, worker])
    await db_session.commit()

    routine = ProjectMessage(
        project_id=project.id,
        from_type="system",
        content="Routine PR bookkeeping",
        context={"audience": "strategist-internal", "event_type": "pr_merged"},
    )
    db_session.add(routine)
    await db_session.commit()
    routine_rev, routine_summary = await _compute_resident_work_state(
        db_session,
        project.id,
        resident_task_id=host.id,
    )
    assert routine_rev is None
    assert routine_summary["pending_barriers"] == 0

    barrier = ProjectMessage(
        project_id=project.id,
        from_type="system",
        content="Serving confirmation requires Strategic continuation",
        context={
            "audience": "strategist-internal",
            "event_type": "serving_confirmed",
            "resident_wake_barrier": True,
        },
    )
    db_session.add(barrier)
    await db_session.commit()
    barrier_rev, barrier_summary = await _compute_resident_work_state(
        db_session,
        project.id,
        resident_task_id=host.id,
    )
    assert barrier_rev is not None
    assert barrier_rev != routine_rev
    assert barrier_summary["pending_barriers"] == 1

    barrier.status = "resolved"
    db_session.add(barrier)
    await db_session.commit()
    resolved_rev, resolved_summary = await _compute_resident_work_state(
        db_session,
        project.id,
        resident_task_id=host.id,
    )
    # Resolution clears the advisory open count but the semantic marker remains
    # monotonic, so acknowledgement cannot revert to an older revision.
    assert resolved_rev == barrier_rev
    assert resolved_summary["pending_barriers"] == 0
