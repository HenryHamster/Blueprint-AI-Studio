"""Shared helpers for the split test_playbook_* test files.

Lifted verbatim from the former testing/test_playbook_auto.py monolith so
the per-module split files can reuse the common task/project builders
without duplication. Behavior-preserving: helper defs are unchanged.
"""

import uuid
from datetime import timedelta
from sqlalchemy.ext.asyncio import AsyncSession
from app.models import Project, Task, TaskStatus
from app.routers.agent_status import _utcnow


async def _make_project(session: AsyncSession, **overrides) -> Project:
    defaults = {
        "name": "Test Project",
        "user_id": "test-user-123",
        "design_doc": {},
    }
    defaults.update(overrides)
    project = Project(**defaults)
    session.add(project)
    await session.flush()
    return project


async def _make_failed_task(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    status_message: str = "vm failed to start after provision attempt",
    task_type: str = "create",
    age_days: float = 0,
) -> Task:
    task = Task(
        project_id=project_id,
        title="Some task",
        payloads=[{"type": task_type, "prompt": "do something"}],
        status=TaskStatus.FAILED,
        latest_status_message=status_message,
    )
    if age_days:
        task.created_at = _utcnow() - timedelta(days=age_days)
    session.add(task)
    await session.flush()
    return task


async def _make_completed_task(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    task_type: str = "create",
    scope: str = "small",
    related_files: list | None = None,
    direction_id: str | None = None,
) -> Task:
    ctx: dict = {"scope": scope}
    if related_files:
        ctx["related_files"] = related_files
    if direction_id:
        ctx["direction_id"] = direction_id
    task = Task(
        project_id=project_id,
        title="Successful task",
        payloads=[{"type": task_type, "prompt": "do something"}],
        status=TaskStatus.COMPLETED,
        context=ctx,
    )
    session.add(task)
    await session.flush()
    return task
