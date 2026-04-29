import asyncio
import os

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlmodel import select

from app.models import Project, Task, TaskHistory, TaskStatus
from app.task_lifecycle_service import (
    ContextPatch,
    CreateTaskCommand,
    SpawnTaskCommand,
    TaskLifecycleService,
    TransitionTaskCommand,
    apply_context_patch,
    is_active_status,
    is_terminal_status,
)
from app.task_lifecycle_service.transitions import (
    can_transition,
    is_archiveable_status,
    is_retryable_status,
)


USE_POSTGRES = (
    os.getenv("USE_TEST_POSTGRES", "").lower() == "true"
    and os.getenv("DATABASE_URL", "").startswith("postgresql")
)


def test_context_patch_preserves_unknown_fields_and_does_not_mutate_input():
    original = {
        "existing": "kept",
        "count": 2,
        "items": [{"id": "a"}],
        "delete_me": True,
        "future_capability": {"new": ["shape"]},
    }

    patched = apply_context_patch(
        original,
        ContextPatch(
            set={"new_field": {"nested": ["still", "allowed"]}},
            delete=("delete_me", "missing_is_ok"),
            increment={"count": 3, "new_counter": 1},
            append_unique={"items": [{"id": "a"}, {"id": "b"}]},
        ),
    )

    assert original["count"] == 2
    assert original["items"] == [{"id": "a"}]
    assert "delete_me" in original
    assert patched == {
        "existing": "kept",
        "count": 5,
        "items": [{"id": "a"}, {"id": "b"}],
        "future_capability": {"new": ["shape"]},
        "new_counter": 1,
        "new_field": {"nested": ["still", "allowed"]},
    }


def test_transition_helpers_match_current_status_semantics():
    assert is_active_status(TaskStatus.PROVISIONING)
    assert is_active_status("IN_PROGRESS")
    assert not is_active_status("FUTURE_STATUS")

    assert is_terminal_status(TaskStatus.COMPLETED)
    assert is_terminal_status("FAILED")
    assert not is_terminal_status(TaskStatus.QUEUED)

    assert is_retryable_status(TaskStatus.FAILED)
    assert not is_retryable_status(TaskStatus.COMPLETED)
    assert is_archiveable_status(TaskStatus.CANCELLED)

    assert can_transition(TaskStatus.QUEUED, TaskStatus.PROVISIONING)
    assert can_transition(TaskStatus.IN_PROGRESS, TaskStatus.FAILED)
    assert can_transition(TaskStatus.COMPLETED, TaskStatus.COMPLETED)
    assert not can_transition(TaskStatus.FAILED, TaskStatus.IN_PROGRESS)
    assert can_transition(
        TaskStatus.FAILED,
        TaskStatus.QUEUED,
        allow_terminal_requeue=True,
    )


@pytest.mark.asyncio
async def test_create_task_accepts_open_payload_context_and_extra_model_fields(db_session):
    project = Project(name="lifecycle create", user_id="test-user")
    db_session.add(project)
    await db_session.commit()

    service = TaskLifecycleService(db_session)
    task = await service.create_task(
        CreateTaskCommand(
            project_id=project.id,
            actor="test",
            reason="task.created.test",
            title="created centrally",
            payloads=[
                {
                    "type": "create",
                    "prompt": "build it",
                    "future_payload_field": {"still": "allowed"},
                }
            ],
            context={
                "future_context_field": {"nested": ["allowed"]},
            },
            task_fields={
                "auto_remediation": True,
                "strategic_cycle_id": "cycle-123",
            },
        )
    )
    await db_session.commit()

    result = await db_session.execute(select(Task).where(Task.id == task.id))
    refreshed = result.scalar_one()
    assert refreshed.title == "created centrally"
    assert refreshed.payloads[0]["future_payload_field"] == {"still": "allowed"}
    assert refreshed.context["future_context_field"] == {"nested": ["allowed"]}
    assert refreshed.auto_remediation is True
    assert refreshed.strategic_cycle_id == "cycle-123"

    history_result = await db_session.execute(
        select(TaskHistory).where(TaskHistory.task_id == task.id)
    )
    history = history_result.scalar_one()
    assert history.action == "CREATED"
    assert history.details["actor"] == "test"
    assert history.details["reason"] == "task.created.test"


@pytest.mark.asyncio
async def test_spawn_child_sets_parent_dependencies_and_history(db_session):
    project = Project(name="lifecycle spawn", user_id="test-user")
    db_session.add(project)
    await db_session.commit()

    parent = Task(
        project_id=project.id,
        title="parent",
        status=TaskStatus.EVALUATING,
    )
    dependency = Task(
        project_id=project.id,
        title="dependency",
        status=TaskStatus.COMPLETED,
    )
    db_session.add(parent)
    db_session.add(dependency)
    await db_session.commit()

    service = TaskLifecycleService(db_session)
    child = await service.spawn_child(
        SpawnTaskCommand(
            project_id=project.id,
            parent_task_id=parent.id,
            actor="test",
            reason="task.spawn.test_child",
            title="child",
            payloads=[{"type": "fix", "future_spawn_payload": True}],
            context={"future_spawn_context": {"ok": True}},
            depends_on=[str(dependency.id)],
            details={"source": "unit-test"},
        )
    )
    await db_session.commit()

    result = await db_session.execute(select(Task).where(Task.id == child.id))
    refreshed = result.scalar_one()
    assert refreshed.parent_task_id == parent.id
    assert refreshed.depends_on == [str(dependency.id)]
    assert refreshed.payloads[0]["future_spawn_payload"] is True
    assert refreshed.context["future_spawn_context"] == {"ok": True}

    history_result = await db_session.execute(
        select(TaskHistory).where(TaskHistory.task_id == child.id)
    )
    history = history_result.scalar_one()
    assert history.action == "SPAWNED"
    assert history.details["actor"] == "test"
    assert history.details["reason"] == "task.spawn.test_child"
    assert history.details["parent_task_id"] == str(parent.id)
    assert history.details["source"] == "unit-test"


@pytest.mark.asyncio
async def test_patch_context_records_history_and_preserves_future_fields(db_session):
    project = Project(name="lifecycle patch", user_id="test-user")
    db_session.add(project)
    await db_session.commit()

    task = Task(
        project_id=project.id,
        title="patch me",
        status=TaskStatus.QUEUED,
        context={
            "existing": "kept",
            "counter": 1,
            "future_payload_bridge": {"any": "shape"},
        },
    )
    db_session.add(task)
    await db_session.commit()

    service = TaskLifecycleService(db_session)
    updated = await service.patch_context(
        task.id,
        ContextPatch(
            set={"future_lifecycle_field": {"nested": ["allowed"]}},
            increment={"counter": 2},
            append_unique={"tags": ["a", "a", "b"]},
        ),
        actor="test",
        reason="task.context.test_patch",
    )
    assert updated is not None
    await db_session.commit()

    result = await db_session.execute(select(Task).where(Task.id == task.id))
    refreshed = result.scalar_one()
    assert refreshed.context["existing"] == "kept"
    assert refreshed.context["future_payload_bridge"] == {"any": "shape"}
    assert refreshed.context["future_lifecycle_field"] == {"nested": ["allowed"]}
    assert refreshed.context["counter"] == 3
    assert refreshed.context["tags"] == ["a", "b"]

    history_result = await db_session.execute(
        select(TaskHistory).where(TaskHistory.task_id == task.id)
    )
    history = history_result.scalar_one()
    assert history.action == "CONTEXT_PATCH"
    assert history.details["actor"] == "test"
    assert history.details["reason"] == "task.context.test_patch"
    assert set(history.details["keys"]) == {
        "counter",
        "future_lifecycle_field",
        "tags",
    }


@pytest.mark.asyncio
async def test_transition_updates_status_message_context_and_history(db_session):
    project = Project(name="lifecycle transition", user_id="test-user")
    db_session.add(project)
    await db_session.commit()

    task = Task(
        project_id=project.id,
        title="transition me",
        status=TaskStatus.IN_PROGRESS,
        context={"future_context": {"kept": True}},
    )
    db_session.add(task)
    await db_session.commit()

    service = TaskLifecycleService(db_session)
    updated = await service.transition(
        task.id,
        TransitionTaskCommand(
            status=TaskStatus.COMPLETED,
            actor="test",
            reason="task.status.test_complete",
            message="completed by lifecycle service",
            context_patch=ContextPatch(set={"terminal_future_field": ["ok"]}),
            details={"source": "unit-test"},
        ),
    )
    assert updated is not None
    await db_session.commit()

    result = await db_session.execute(select(Task).where(Task.id == task.id))
    refreshed = result.scalar_one()
    assert refreshed.status == TaskStatus.COMPLETED
    assert refreshed.latest_status_message == "completed by lifecycle service"
    assert refreshed.context["future_context"] == {"kept": True}
    assert refreshed.context["terminal_future_field"] == ["ok"]

    history_result = await db_session.execute(
        select(TaskHistory).where(TaskHistory.task_id == task.id)
    )
    history = history_result.scalar_one()
    assert history.action == "STATUS_CHANGE"
    assert history.details["actor"] == "test"
    assert history.details["reason"] == "task.status.test_complete"
    assert history.details["from_status"] == "IN_PROGRESS"
    assert history.details["to_status"] == "COMPLETED"
    assert history.details["source"] == "unit-test"


@pytest.mark.skipif(not USE_POSTGRES, reason="row-lock concurrency requires PostgreSQL")
@pytest.mark.asyncio
async def test_concurrent_context_patches_keep_both_mutations(db_session):
    project = Project(name="lifecycle concurrency", user_id="test-user")
    db_session.add(project)
    await db_session.commit()

    task = Task(
        project_id=project.id,
        title="concurrent patch",
        status=TaskStatus.QUEUED,
        context={"patches": []},
    )
    db_session.add(task)
    await db_session.commit()

    Session = sessionmaker(
        bind=db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    first_has_lock = asyncio.Event()

    async def patch(label: str, hold_first_lock: bool = False) -> None:
        async with Session() as session:
            service = TaskLifecycleService(session)
            updated = await service.patch_context(
                task.id,
                ContextPatch(append_unique={"patches": [label]}),
                actor="test",
                reason=f"task.context.concurrent.{label}",
                skip_locked=False,
                timeout=5,
            )
            assert updated is not None
            if hold_first_lock:
                first_has_lock.set()
                await asyncio.sleep(0.2)
            await session.commit()

    async def second_patch() -> None:
        await first_has_lock.wait()
        await patch("second")

    await asyncio.gather(
        patch("first", hold_first_lock=True),
        second_patch(),
    )

    async with Session() as verify_session:
        result = await verify_session.execute(select(Task).where(Task.id == task.id))
        refreshed = result.scalar_one()
        assert refreshed.context["patches"] == ["first", "second"]
