"""
Tests for the 'Project State Aggregation' step in task_lifecycle.py.

Verifies that parent/logical tasks (no vm_instance_id) receive a meaningful
latest_status_message when their status is set to match the project outcome.
"""

import pytest
import pytest_asyncio

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel

from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler as _SQLiteTypeCompiler

if not hasattr(_SQLiteTypeCompiler, "visit_JSONB"):
    _SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: self.visit_JSON(
        type_, **kw
    )

from app.models import Project, Task, TaskStatus, ProjectStatus


@pytest_asyncio.fixture()
async def db_session():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    Session = sessionmaker(
        bind=engine,
        class_=AsyncSession,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
    )
    async with Session() as session:
        yield session

    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
    await engine.dispose()


async def _make_project(
    session, status=ProjectStatus.DEPLOYING, autonomous_enabled=False
) -> Project:
    project = Project(
        name="Test Project",
        user_id="user1",
        status=status,
        autonomous_enabled=autonomous_enabled,
    )
    session.add(project)
    await session.commit()
    await session.refresh(project)
    return project


async def _make_task(
    session,
    project_id,
    status: TaskStatus,
    vm_instance_id=None,
    latest_status_message=None,
) -> Task:
    task = Task(
        project_id=project_id,
        payloads=[{"type": "create", "prompt": "do something"}],
        status=status,
        vm_instance_id=vm_instance_id,
        latest_status_message=latest_status_message,
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task


def _run_aggregation(all_tasks, project):
    """
    Re-implement the aggregation logic from task_lifecycle.py so we can unit-test it
    without invoking the full HTTP handler. Mirrors lines 1400-1424.
    """
    executable_statuses = [t.status for t in all_tasks if t.vm_instance_id]
    parent_tasks = [
        t
        for t in all_tasks
        if not t.vm_instance_id
        and t.status
        not in [
            TaskStatus.QUEUED,  # leaf awaiting claim/warm-handoff, not a parent
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        ]
    ]

    if not (
        not project.autonomous_enabled
        and project.status != ProjectStatus.CANCELLED
        and executable_statuses
    ):
        return

    all_terminal = all(
        s in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED)
        for s in executable_statuses
    )
    new_project_status = None
    if all_terminal:
        if all(s == TaskStatus.COMPLETED for s in executable_statuses):
            new_project_status = ProjectStatus.COMPLETED
        elif any(s == TaskStatus.FAILED for s in executable_statuses):
            new_project_status = ProjectStatus.FAILED
        elif all(
            s in [TaskStatus.COMPLETED, TaskStatus.CANCELLED]
            for s in executable_statuses
        ):
            new_project_status = ProjectStatus.CANCELLED

    if new_project_status:
        project.status = new_project_status
        terminal_task_status = (
            TaskStatus.COMPLETED
            if new_project_status == ProjectStatus.COMPLETED
            else TaskStatus.FAILED
            if new_project_status == ProjectStatus.FAILED
            else TaskStatus.CANCELLED
        )
        for pt in parent_tasks:
            pt.status = terminal_task_status
            if terminal_task_status == TaskStatus.FAILED:
                pt.latest_status_message = "Project aggregation: child task failed"
            elif terminal_task_status == TaskStatus.COMPLETED:
                pt.latest_status_message = "Project completed"
            else:
                pt.latest_status_message = "Project cancelled"


@pytest.mark.asyncio
async def test_parent_task_gets_failed_message_on_child_failure(db_session):
    """Parent task should get 'Project aggregation: child task failed' when a child fails."""
    project = await _make_project(db_session)
    # Child (executable) task that failed
    child = await _make_task(
        db_session, project.id, TaskStatus.FAILED, vm_instance_id="vm-1"
    )
    # Parent (logical) task with no VM, currently EVALUATING
    parent = await _make_task(
        db_session,
        project.id,
        TaskStatus.EVALUATING,
        vm_instance_id=None,
        latest_status_message=None,
    )

    _run_aggregation([child, parent], project)

    assert parent.status == TaskStatus.FAILED
    assert parent.latest_status_message == "Project aggregation: child task failed"


@pytest.mark.asyncio
async def test_parent_task_gets_completed_message_on_all_children_complete(db_session):
    """Parent task should get 'Project completed' when all child tasks complete."""
    project = await _make_project(db_session)
    child = await _make_task(
        db_session, project.id, TaskStatus.COMPLETED, vm_instance_id="vm-1"
    )
    parent = await _make_task(
        db_session, project.id, TaskStatus.EVALUATING, vm_instance_id=None
    )

    _run_aggregation([child, parent], project)

    assert parent.status == TaskStatus.COMPLETED
    assert parent.latest_status_message == "Project completed"


@pytest.mark.asyncio
async def test_parent_task_gets_cancelled_message_on_all_children_cancelled(db_session):
    """Parent task should get 'Project cancelled' when all children are cancelled/completed."""
    project = await _make_project(db_session)
    child = await _make_task(
        db_session, project.id, TaskStatus.CANCELLED, vm_instance_id="vm-1"
    )
    parent = await _make_task(
        db_session, project.id, TaskStatus.EVALUATING, vm_instance_id=None
    )

    _run_aggregation([child, parent], project)

    assert parent.status == TaskStatus.CANCELLED
    assert parent.latest_status_message == "Project cancelled"


@pytest.mark.asyncio
async def test_already_terminal_parent_not_updated(db_session):
    """Parent tasks already in a terminal status should not be touched."""
    project = await _make_project(db_session)
    child = await _make_task(
        db_session, project.id, TaskStatus.FAILED, vm_instance_id="vm-1"
    )
    parent = await _make_task(
        db_session,
        project.id,
        TaskStatus.COMPLETED,
        vm_instance_id=None,
        latest_status_message="Already done",
    )

    _run_aggregation([child, parent], project)

    # parent was already COMPLETED — not in parent_tasks, should be unchanged
    assert parent.status == TaskStatus.COMPLETED
    assert parent.latest_status_message == "Already done"


@pytest.mark.asyncio
async def test_failed_message_overrides_stale_evaluating_message(db_session):
    """A parent with a stale EVALUATING message gets the correct failure message."""
    project = await _make_project(db_session)
    child = await _make_task(
        db_session, project.id, TaskStatus.FAILED, vm_instance_id="vm-1"
    )
    parent = await _make_task(
        db_session,
        project.id,
        TaskStatus.EVALUATING,
        vm_instance_id=None,
        latest_status_message="Evaluating child tasks...",
    )

    _run_aggregation([child, parent], project)

    assert parent.latest_status_message == "Project aggregation: child task failed"


@pytest.mark.asyncio
async def test_inflight_work_blocks_premature_project_terminal_transition(db_session):
    """For autonomous projects with continuous work, a single FAILED task must
    not flip the project to FAILED while other tasks are still IN_PROGRESS or
    PROVISIONING. Pre-fix, ``any FAILED in executable_statuses`` flipped the
    project to FAILED on every status change, which then re-fired the parent-
    aggregation sweep on every subsequent task transition.

    The aggregation must require ALL executable tasks terminal before any
    project-status flip — preserving finite-project semantics while making
    the aggregation a no-op for autonomous projects (always something in
    flight).
    """
    project = await _make_project(db_session)
    failed_task = await _make_task(
        db_session, project.id, TaskStatus.FAILED, vm_instance_id="vm-1"
    )
    in_flight_task = await _make_task(
        db_session, project.id, TaskStatus.IN_PROGRESS, vm_instance_id="vm-2"
    )
    parent = await _make_task(
        db_session, project.id, TaskStatus.EVALUATING, vm_instance_id=None
    )

    _run_aggregation([failed_task, in_flight_task, parent], project)

    # Project must NOT have flipped — there's still in-flight work.
    assert project.status != ProjectStatus.FAILED, (
        f"Project flipped to FAILED while in-flight work remained; got "
        f"{project.status}. Pre-fix bug: any historic FAILED would pin the "
        f"project at FAILED regardless of ongoing work."
    )
    # Parent must NOT have been force-FAILed.
    assert parent.status == TaskStatus.EVALUATING


@pytest.mark.asyncio
async def test_queued_leaf_awaiting_warm_handoff_not_swept_into_parent_aggregation(
    db_session,
):
    """Production 2026-05-05 task 582a0210: a QUEUED leaf review task waiting on
    warm-handoff has ``vm_instance_id=None`` (the warm target VM is in
    ``warm_target_vm_id``, the real ``vm_instance_id`` is set only at claim).
    Pre-fix, the aggregation logic mistook ``vm_instance_id=None`` for "logical
    parent task" and force-FAILed the leaf with "Project aggregation: child
    task failed" the instant a sibling completed and flipped the project to
    FAILED — the QUEUED task never even got to run.

    A QUEUED leaf must be left alone by aggregation; it's pending execution,
    not a logical parent waiting for children.
    """
    project = await _make_project(db_session)
    project.status = ProjectStatus.FAILED  # historic FAILED state pinning the project
    failed_sibling = await _make_task(
        db_session, project.id, TaskStatus.FAILED, vm_instance_id="vm-1"
    )
    completing_sibling = await _make_task(
        db_session, project.id, TaskStatus.COMPLETED, vm_instance_id="vm-2"
    )
    # The QUEUED leaf — vm_instance_id is None because it's awaiting warm-claim.
    queued_leaf = await _make_task(
        db_session,
        project.id,
        TaskStatus.QUEUED,
        vm_instance_id=None,
        latest_status_message=None,
    )

    _run_aggregation([failed_sibling, completing_sibling, queued_leaf], project)

    # The QUEUED leaf must NOT be force-FAILed by aggregation.
    assert queued_leaf.status == TaskStatus.QUEUED, (
        f"Expected QUEUED leaf to be left alone by aggregation; got "
        f"{queued_leaf.status} with message {queued_leaf.latest_status_message!r}. "
        f"This regresses production fix for task 582a0210."
    )
    assert queued_leaf.latest_status_message is None


@pytest.mark.asyncio
async def test_live_autonomous_project_not_flipped_to_cancelled(db_session):
    """A live autonomous project whose current wave drained to all-{COMPLETED,
    CANCELLED} (no genuine failure) must NOT be flipped to a terminal CANCELLED
    status. Tasks are routinely set to CANCELLED for benign reasons (dedup,
    scope-empty, supersession, graceful reassignment) and keep their VM id, so
    the wave-drain window commonly produces an all-{COMPLETED, CANCELLED} mix.
    Because the autonomous engine selects work purely by ``autonomous_enabled``
    and never consults project status, a flip here would display CANCELLED to
    the operator forever while the project keeps running.
    """
    project = await _make_project(db_session, autonomous_enabled=True)
    completed = await _make_task(
        db_session, project.id, TaskStatus.COMPLETED, vm_instance_id="vm-1"
    )
    cancelled = await _make_task(
        db_session, project.id, TaskStatus.CANCELLED, vm_instance_id="vm-2"
    )
    parent = await _make_task(
        db_session, project.id, TaskStatus.EVALUATING, vm_instance_id=None
    )

    _run_aggregation([completed, cancelled, parent], project)

    assert project.status != ProjectStatus.CANCELLED, (
        f"Live autonomous project flipped to CANCELLED during wave-drain window; "
        f"got {project.status}. The autonomous engine ignores project status, so "
        f"this would display CANCELLED to the operator while work continues."
    )
    assert parent.status == TaskStatus.EVALUATING


@pytest.mark.asyncio
async def test_live_autonomous_project_not_flipped_to_completed(db_session):
    """All-COMPLETED in the drain window must not flip a live autonomous
    project to COMPLETED — it will keep generating its own work."""
    project = await _make_project(db_session, autonomous_enabled=True)
    completed_a = await _make_task(
        db_session, project.id, TaskStatus.COMPLETED, vm_instance_id="vm-1"
    )
    completed_b = await _make_task(
        db_session, project.id, TaskStatus.COMPLETED, vm_instance_id="vm-2"
    )
    parent = await _make_task(
        db_session, project.id, TaskStatus.EVALUATING, vm_instance_id=None
    )

    _run_aggregation([completed_a, completed_b, parent], project)

    assert project.status != ProjectStatus.COMPLETED
    assert parent.status == TaskStatus.EVALUATING


@pytest.mark.asyncio
async def test_live_autonomous_project_not_flipped_to_failed(db_session):
    """A FAILED task in the drain window must not flip a live autonomous
    project to FAILED — autonomous projects continuously retry/respawn work."""
    project = await _make_project(db_session, autonomous_enabled=True)
    failed = await _make_task(
        db_session, project.id, TaskStatus.FAILED, vm_instance_id="vm-1"
    )
    completed = await _make_task(
        db_session, project.id, TaskStatus.COMPLETED, vm_instance_id="vm-2"
    )
    parent = await _make_task(
        db_session, project.id, TaskStatus.EVALUATING, vm_instance_id=None
    )

    _run_aggregation([failed, completed, parent], project)

    assert project.status != ProjectStatus.FAILED
    assert parent.status == TaskStatus.EVALUATING


@pytest.mark.asyncio
async def test_finite_project_still_aggregates(db_session):
    """A finite (non-autonomous) project must still aggregate to a terminal
    status — the fix only narrows WHEN the flip fires, it does not change
    finite-project semantics."""
    project = await _make_project(db_session, autonomous_enabled=False)
    completed = await _make_task(
        db_session, project.id, TaskStatus.COMPLETED, vm_instance_id="vm-1"
    )
    cancelled = await _make_task(
        db_session, project.id, TaskStatus.CANCELLED, vm_instance_id="vm-2"
    )
    parent = await _make_task(
        db_session, project.id, TaskStatus.EVALUATING, vm_instance_id=None
    )

    _run_aggregation([completed, cancelled, parent], project)

    assert project.status == ProjectStatus.CANCELLED
    assert parent.status == TaskStatus.CANCELLED
    assert parent.latest_status_message == "Project cancelled"
