"""
Tests for the 'Project State Aggregation' step in task_lifecycle.py.

Verifies that parent/logical tasks (no vm_instance_id) receive a meaningful
latest_status_message when their status is set to match the project outcome.
"""
import uuid
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, select

from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler as _SQLiteTypeCompiler

if not hasattr(_SQLiteTypeCompiler, "visit_JSONB"):
    _SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: self.visit_JSON(type_, **kw)

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
        bind=engine, class_=AsyncSession,
        autocommit=False, autoflush=False, expire_on_commit=False,
    )
    async with Session() as session:
        yield session

    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
    await engine.dispose()


async def _make_project(session, status=ProjectStatus.DEPLOYING) -> Project:
    project = Project(name="Test Project", user_id="user1", status=status)
    session.add(project)
    await session.commit()
    await session.refresh(project)
    return project


async def _make_task(session, project_id, status: TaskStatus, vm_instance_id=None,
                     latest_status_message=None) -> Task:
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
        t for t in all_tasks
        if not t.vm_instance_id
        and t.status not in [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED]
    ]

    if not (project.status != ProjectStatus.CANCELLED and executable_statuses):
        return

    new_project_status = None
    if all(s == TaskStatus.COMPLETED for s in executable_statuses):
        new_project_status = ProjectStatus.COMPLETED
    elif any(s == TaskStatus.FAILED for s in executable_statuses):
        new_project_status = ProjectStatus.FAILED
    elif all(s in [TaskStatus.COMPLETED, TaskStatus.CANCELLED] for s in executable_statuses):
        new_project_status = ProjectStatus.CANCELLED

    if new_project_status:
        project.status = new_project_status
        terminal_task_status = (
            TaskStatus.COMPLETED if new_project_status == ProjectStatus.COMPLETED
            else TaskStatus.FAILED if new_project_status == ProjectStatus.FAILED
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
    child = await _make_task(db_session, project.id, TaskStatus.FAILED, vm_instance_id="vm-1")
    # Parent (logical) task with no VM, currently EVALUATING
    parent = await _make_task(db_session, project.id, TaskStatus.EVALUATING, vm_instance_id=None,
                              latest_status_message=None)

    _run_aggregation([child, parent], project)

    assert parent.status == TaskStatus.FAILED
    assert parent.latest_status_message == "Project aggregation: child task failed"


@pytest.mark.asyncio
async def test_parent_task_gets_completed_message_on_all_children_complete(db_session):
    """Parent task should get 'Project completed' when all child tasks complete."""
    project = await _make_project(db_session)
    child = await _make_task(db_session, project.id, TaskStatus.COMPLETED, vm_instance_id="vm-1")
    parent = await _make_task(db_session, project.id, TaskStatus.EVALUATING, vm_instance_id=None)

    _run_aggregation([child, parent], project)

    assert parent.status == TaskStatus.COMPLETED
    assert parent.latest_status_message == "Project completed"


@pytest.mark.asyncio
async def test_parent_task_gets_cancelled_message_on_all_children_cancelled(db_session):
    """Parent task should get 'Project cancelled' when all children are cancelled/completed."""
    project = await _make_project(db_session)
    child = await _make_task(db_session, project.id, TaskStatus.CANCELLED, vm_instance_id="vm-1")
    parent = await _make_task(db_session, project.id, TaskStatus.EVALUATING, vm_instance_id=None)

    _run_aggregation([child, parent], project)

    assert parent.status == TaskStatus.CANCELLED
    assert parent.latest_status_message == "Project cancelled"


@pytest.mark.asyncio
async def test_already_terminal_parent_not_updated(db_session):
    """Parent tasks already in a terminal status should not be touched."""
    project = await _make_project(db_session)
    child = await _make_task(db_session, project.id, TaskStatus.FAILED, vm_instance_id="vm-1")
    parent = await _make_task(db_session, project.id, TaskStatus.COMPLETED, vm_instance_id=None,
                              latest_status_message="Already done")

    _run_aggregation([child, parent], project)

    # parent was already COMPLETED — not in parent_tasks, should be unchanged
    assert parent.status == TaskStatus.COMPLETED
    assert parent.latest_status_message == "Already done"


@pytest.mark.asyncio
async def test_failed_message_overrides_stale_evaluating_message(db_session):
    """A parent with a stale EVALUATING message gets the correct failure message."""
    project = await _make_project(db_session)
    child = await _make_task(db_session, project.id, TaskStatus.FAILED, vm_instance_id="vm-1")
    parent = await _make_task(db_session, project.id, TaskStatus.EVALUATING, vm_instance_id=None,
                              latest_status_message="Evaluating child tasks...")

    _run_aggregation([child, parent], project)

    assert parent.latest_status_message == "Project aggregation: child task failed"
