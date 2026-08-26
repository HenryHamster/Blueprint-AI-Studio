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


# ---------------------------------------------------------------------------
# Project-wide ambient activity
#
# The lineage walk is anchored on created_by='STRATEGIC_AGENT'. The orchestrator
# authors nearly every real pipeline task as 'ORCHESTRATOR', so a Resident
# watching only its own lineage reported a quiet system while the fleet was
# busy. The project_* keys fix that WITHOUT touching the two things the lineage
# keys drive: the wake fingerprint and the exploration-suppression gate.
# ---------------------------------------------------------------------------


def _orchestrator_task(
    project: Project,
    title: str,
    *,
    status: TaskStatus = TaskStatus.IN_PROGRESS,
) -> Task:
    return Task(
        project_id=project.id,
        title=title,
        payloads=[{"type": "create"}],
        status=status,
        created_by="ORCHESTRATOR",
    )


@pytest.mark.asyncio
async def test_project_counts_see_orchestrator_work_the_lineage_walk_cannot(
    db_session,
):
    """The exact reported blind spot: a busy ORCHESTRATOR fleet, a lineage that
    owns nothing, and a heartbeat that used to report a quiet system."""
    project = await _project(db_session)
    host = _strategic_task(project, "Long-lived Resident")
    db_session.add(host)
    db_session.add_all(
        [
            _orchestrator_task(project, "Pipeline A"),
            _orchestrator_task(project, "Pipeline B"),
            _orchestrator_task(project, "Pipeline C", status=TaskStatus.QUEUED),
            _orchestrator_task(project, "Pipeline D", status=TaskStatus.BLOCKED),
            _orchestrator_task(project, "Pipeline E", status=TaskStatus.PARKED),
        ]
    )
    await db_session.commit()

    rev, summary = await _compute_resident_work_state(
        db_session,
        project.id,
        resident_task_id=host.id,
    )

    # Lineage view: the Resident owns nothing. Unchanged.
    assert summary["in_flight"] == 0
    assert summary["blocked"] == 0
    assert summary["parked"] == 0

    # Project view: the fleet is plainly busy.
    assert summary["project_in_flight"] == 3
    assert summary["project_blocked"] == 1
    assert summary["project_parked"] == 1
    assert summary["project_scan_capped"] == 0

    # And none of it moved the wake fingerprint.
    assert rev is None


@pytest.mark.asyncio
async def test_project_activity_never_moves_the_wake_fingerprint(db_session):
    """work_state_rev is a WAKE trigger: a changed rev runs a full model turn.

    Estate-wide churn must not schedule turns, or the Resident wakes on every
    ORCHESTRATOR transition anywhere in the project.
    """
    project = await _project(db_session)
    host = _strategic_task(project, "Long-lived Resident")
    owned = _strategic_task(project, "Owned work")
    db_session.add_all([host, owned])
    await db_session.commit()

    rev_before, summary_before = await _compute_resident_work_state(
        db_session,
        project.id,
        resident_task_id=host.id,
    )

    # Everything an orchestrator-authored task can do: start, fail, block, park.
    failing = _orchestrator_task(project, "Fleet failure", status=TaskStatus.FAILED)
    failing.completed_at = _utcnow()
    blocking = _orchestrator_task(project, "Fleet blocker", status=TaskStatus.BLOCKED)
    blocking.blocked_at = _utcnow()
    parking = _orchestrator_task(project, "Fleet park", status=TaskStatus.PARKED)
    parking.parked_at = _utcnow()
    db_session.add_all([failing, blocking, parking])
    await db_session.commit()

    rev_after, summary_after = await _compute_resident_work_state(
        db_session,
        project.id,
        resident_task_id=host.id,
    )

    assert rev_after == rev_before
    # The counts still moved, so the Resident can SEE it without being WOKEN.
    assert summary_after["project_blocked"] > summary_before["project_blocked"]
    assert summary_after["project_parked"] > summary_before["project_parked"]
    assert summary_after["project_terminal_since"] > summary_before[
        "project_terminal_since"
    ]


@pytest.mark.asyncio
async def test_project_counts_do_not_reuse_the_pacing_gate_key_names(db_session):
    """`in_flight` gates exploration suppression in the agent's
    hasPendingOwnedWork. Widening THAT key in place would silence the Resident
    for as long as the fleet had anything running, so the project-wide numbers
    must live under distinct keys and leave the lineage keys at zero."""
    project = await _project(db_session)
    host = _strategic_task(project, "Long-lived Resident")
    db_session.add(host)
    db_session.add_all(
        [_orchestrator_task(project, f"Fleet {i}") for i in range(4)]
    )
    await db_session.commit()

    _, summary = await _compute_resident_work_state(
        db_session,
        project.id,
        resident_task_id=host.id,
    )
    assert summary["in_flight"] == 0, (
        "project-wide work must not leak into the lineage key the agent's "
        "exploration-suppression gate reads"
    )
    assert summary["project_in_flight"] == 4


@pytest.mark.asyncio
async def test_project_counts_are_project_scoped(db_session):
    """A busy neighbouring project must not read as this project's fleet."""
    project = await _project(db_session)
    other = Project(name="Other project", user_id="wake-test-user")
    db_session.add(other)
    await db_session.commit()
    await db_session.refresh(other)

    host = _strategic_task(project, "Long-lived Resident")
    db_session.add(host)
    db_session.add_all(
        [_orchestrator_task(other, f"Neighbour {i}") for i in range(3)]
    )
    await db_session.commit()

    _, summary = await _compute_resident_work_state(
        db_session,
        project.id,
        resident_task_id=host.id,
    )
    assert summary["project_in_flight"] == 0


@pytest.mark.asyncio
async def test_project_counts_exclude_archived_and_terminal_rows(db_session):
    """Only live work counts as in flight; archived rows are excluded outright."""
    project = await _project(db_session)
    host = _strategic_task(project, "Long-lived Resident")
    db_session.add(host)
    archived = _orchestrator_task(project, "Archived but running")
    archived.archived = True
    done = _orchestrator_task(project, "Finished", status=TaskStatus.COMPLETED)
    done.completed_at = _utcnow()
    live = _orchestrator_task(project, "Actually running")
    db_session.add_all([archived, done, live])
    await db_session.commit()

    _, summary = await _compute_resident_work_state(
        db_session,
        project.id,
        resident_task_id=host.id,
    )
    assert summary["project_in_flight"] == 1
    # The terminal row is still visible as recent throughput, which is how a
    # draining fleet is told apart from a stalled one.
    assert summary["project_terminal_since"] == 1


@pytest.mark.asyncio
async def test_project_summary_returns_counts_not_rows(db_session):
    """The heartbeat fires constantly and its output lands in the strategic
    envelope, so this surface is bounded by construction: a fixed handful of
    integers, never a task list. Row-level detail stays on the on-demand
    ./fetch task-census pull."""
    project = await _project(db_session)
    host = _strategic_task(project, "Long-lived Resident")
    db_session.add(host)
    db_session.add_all(
        [_orchestrator_task(project, f"Fleet {i}") for i in range(30)]
    )
    await db_session.commit()

    _, summary = await _compute_resident_work_state(
        db_session,
        project.id,
        resident_task_id=host.id,
    )
    project_keys = {k: v for k, v in summary.items() if k.startswith("project_")}
    assert project_keys, "expected project-wide keys on the heartbeat summary"
    for key, value in project_keys.items():
        assert isinstance(value, int), f"{key} must be a count, got {type(value)}"
    # Whole summary stays a flat scalar map regardless of how busy the fleet is.
    for key, value in summary.items():
        assert isinstance(value, int), f"{key} must be a scalar count"
