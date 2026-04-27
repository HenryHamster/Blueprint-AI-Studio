import uuid

import pytest
from sqlalchemy import UniqueConstraint

from app.models import Project, Task, TaskStatus, UserIntegration
from app.orchestrator import _lock_provisioning_task_for_vm
from app.review_pipeline import _get_active_pipeline_task_by_key, _pipeline_task_idempotency_key


def test_userintegration_user_id_has_named_unique_constraint():
    constraints = UserIntegration.__table__.constraints
    assert any(
        isinstance(constraint, UniqueConstraint)
        and constraint.name == "uq_userintegration_user_id"
        and [column.name for column in constraint.columns] == ["user_id"]
        for constraint in constraints
    )


def test_task_active_idempotency_key_index_declared():
    index = next(
        idx for idx in Task.__table__.indexes
        if idx.name == "ux_task_active_idempotency_key"
    )
    assert index.unique is True
    assert [expr.name for expr in index.expressions] == ["idempotency_key"]


def test_pipeline_task_idempotency_key_is_stable_for_same_spawn():
    project_id = uuid.uuid4()
    first = _pipeline_task_idempotency_key(
        project_id=project_id,
        task_type="review",
        pr_number=123,
        original_task_id="orig",
        iteration=1,
        is_rollup=False,
    )
    second = _pipeline_task_idempotency_key(
        project_id=project_id,
        task_type="review",
        pr_number=123,
        original_task_id="orig",
        iteration=1,
        is_rollup=False,
    )
    assert first == second
    assert first != _pipeline_task_idempotency_key(
        project_id=project_id,
        task_type="fix",
        pr_number=123,
        original_task_id="orig",
        iteration=1,
        is_rollup=False,
    )


@pytest.mark.asyncio
async def test_active_pipeline_task_lookup_ignores_terminal_same_key(db_session):
    project = Project(name="Consistency", user_id="user-1")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    key = "review_pipeline:test-key"
    completed = Task(
        project_id=project.id,
        title="old completed review",
        status=TaskStatus.COMPLETED,
        idempotency_key=key,
        payloads=[{"type": "review", "pr_number": 1, "original_task_id": "orig"}],
    )
    active = Task(
        project_id=project.id,
        title="active review",
        status=TaskStatus.QUEUED,
        idempotency_key=key,
        payloads=[{"type": "review", "pr_number": 1, "original_task_id": "orig"}],
    )
    db_session.add_all([completed, active])
    await db_session.commit()

    found = await _get_active_pipeline_task_by_key(db_session, key)

    assert found is not None
    assert found.id == active.id


@pytest.mark.asyncio
async def test_provisioning_vm_lock_rejects_stale_vm_result(db_session):
    project = Project(name="Consistency", user_id="user-1")
    task = Task(
        project_id=project.id,
        title="requeued task",
        status=TaskStatus.QUEUED,
        vm_instance_id="agent-vm-new",
        payloads=[{"type": "create", "prompt": "x"}],
    )
    db_session.add(project)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    locked = await _lock_provisioning_task_for_vm(db_session, task.id, "agent-vm-old")

    assert locked is None
