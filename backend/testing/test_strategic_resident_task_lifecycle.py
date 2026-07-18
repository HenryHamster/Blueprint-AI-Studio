"""Resident task-lifecycle adapters route through the operator modify path."""

import uuid
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel

from app.models import Project, Task, TaskStatus
from app.strategic_resident_task_lifecycle import (
    build_task_lifecycle_effect,
    task_lifecycle_operation_kind,
)


@pytest_asyncio.fixture()
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    factory = sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )
    async with factory() as session:
        yield session
    await engine.dispose()


async def _project_and_task(db, *, payload_type="plan"):
    project = Project(name="X", user_id="op")
    db.add(project)
    await db.flush()
    task = Task(
        project_id=project.id,
        title="worker",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": payload_type}],
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)
    return project, task


def test_operation_kinds_cover_the_status_transitions():
    assert task_lifecycle_operation_kind("task.cancel") == "cancel_task"
    assert task_lifecycle_operation_kind("task.requeue") == "requeue_task"
    assert task_lifecycle_operation_kind("task.retry") == "retry_task"
    assert task_lifecycle_operation_kind("task.unblock") == "unblock_task"
    assert task_lifecycle_operation_kind("task.reprioritize") is None  # sync path
    assert task_lifecycle_operation_kind("pr.create") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("capability", "expected_status"),
    [
        ("task.cancel", "CANCELLED"),
        ("task.requeue", "QUEUED"),
        ("task.retry", "QUEUED"),
        ("task.unblock", "QUEUED"),
    ],
)
async def test_lifecycle_effect_calls_operator_modify_path(
    db, monkeypatch, capability, expected_status
):
    from app import admin_task_mutations

    project, task = await _project_and_task(db)
    captured = {}

    async def _fake_modify(task_arg, payload, background_tasks, session, **kw):
        captured["task_id"] = task_arg.id
        captured["payload"] = payload
        return {"status": payload["status"]}

    monkeypatch.setattr(admin_task_mutations, "modify_task_apply", _fake_modify)

    perform = build_task_lifecycle_effect(
        capability, project_id=project.id, parameters={"task_id": str(task.id)}
    )
    result = await perform(db)
    # It ran the operator path with the correct status transition on the target.
    assert captured["task_id"] == task.id
    assert captured["payload"] == {"status": expected_status}
    assert result["status"] == expected_status


@pytest.mark.asyncio
async def test_lifecycle_effect_refuses_a_resident_turn_target(db, monkeypatch):
    from app import admin_task_mutations

    project, turn = await _project_and_task(db, payload_type="strategic_resident_turn")
    modify = AsyncMock()
    monkeypatch.setattr(admin_task_mutations, "modify_task_apply", modify)

    perform = build_task_lifecycle_effect(
        "task.cancel", project_id=project.id, parameters={"task_id": str(turn.id)}
    )
    with pytest.raises(ValueError, match="Resident turn task"):
        await perform(db)
    modify.assert_not_awaited()


@pytest.mark.asyncio
async def test_lifecycle_effect_refuses_a_cross_project_target(db, monkeypatch):
    from app import admin_task_mutations

    _project, task = await _project_and_task(db)
    modify = AsyncMock()
    monkeypatch.setattr(admin_task_mutations, "modify_task_apply", modify)

    # A different project id cannot reach this project's task.
    perform = build_task_lifecycle_effect(
        "task.cancel", project_id=uuid.uuid4(), parameters={"task_id": str(task.id)}
    )
    with pytest.raises(ValueError, match="not found in project"):
        await perform(db)
    modify.assert_not_awaited()
