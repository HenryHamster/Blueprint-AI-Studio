"""Automatic continuation admission at completion and agent-status seams."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from contextlib import asynccontextmanager

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlmodel import select

from app.admin_task_approvals import _maybe_spawn_rollup_smoketest
from app.agent_status_spawn import maybe_spawn_debug_create, maybe_spawn_smoketest_fix
from app.models import ArtifactType, Project, Task, TaskStatus
from app.routers.agent_status import _lock_agent_status_project_authority
from app.schemas import AgentStatusPayload
from app.task_completion import (
    _COMPUTE_WAKE_FOLLOWUP_PENDING_KEY,
    _admit_compute_followup,
    _spawn_act_on_verdict_followon,
)
from app.task_locking import (
    AUTOMATIC_TASK_CREATION_PROJECT_PAUSED,
    AUTOMATIC_TASK_CREATION_PROJECT_UNAVAILABLE,
    automatic_task_creation_parent_lock,
)


USE_POSTGRES = os.getenv("USE_TEST_POSTGRES", "").lower() == "true" and os.getenv(
    "DATABASE_URL", ""
).startswith("postgresql")


async def _project_and_task(db_session, *, paused: bool, task_type: str):
    project = Project(
        name=f"completion-pause-{task_type}-{uuid.uuid4().hex[:8]}",
        user_id="test-user",
        paused=paused,
    )
    db_session.add(project)
    await db_session.flush()
    task = Task(
        project_id=project.id,
        title=f"source {task_type}",
        payloads=[{"type": task_type, "base_branch": "main"}],
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
    )
    db_session.add(task)
    await db_session.commit()
    return project, task


@pytest.mark.asyncio
async def test_parent_boundary_does_not_lock_task_when_project_is_missing(
    db_session, monkeypatch
):
    """A missing Project fails before taking a child-row lock."""

    @asynccontextmanager
    async def _unexpected_task_lock(*args, **kwargs):
        raise AssertionError("Task lock taken without Project authority")
        yield  # pragma: no cover

    monkeypatch.setattr("app.task_locking.task_lock", _unexpected_task_lock)
    async with automatic_task_creation_parent_lock(
        db_session,
        uuid.uuid4(),
        uuid.uuid4(),
    ) as (decision, project, task):
        assert decision == AUTOMATIC_TASK_CREATION_PROJECT_UNAVAILABLE
        assert project is None
        assert task is None


@pytest.mark.asyncio
async def test_paused_rollup_approval_does_not_publish_smoketest(db_session):
    project, parent = await _project_and_task(
        db_session, paused=True, task_type="rollup"
    )

    await _maybe_spawn_rollup_smoketest(
        parent,
        "example",
        "repo",
        "main",
        db_session,
    )

    children = (
        await db_session.execute(select(Task).where(Task.parent_task_id == parent.id))
    ).scalars().all()
    await db_session.refresh(parent)
    assert children == []
    assert (parent.context or {})["automatic_smoketest_creation_suppressed"] == (
        AUTOMATIC_TASK_CREATION_PROJECT_PAUSED
    )
    await db_session.refresh(project)
    assert project.paused is True


@pytest.mark.asyncio
async def test_paused_smoketest_failure_does_not_publish_fix(db_session):
    project, source = await _project_and_task(
        db_session, paused=True, task_type="smoketest"
    )
    source.status = TaskStatus.FAILED
    payload = AgentStatusPayload(
        task_id=source.id,
        status=TaskStatus.FAILED,
        artifact_type=ArtifactType.SMOKETEST_RESULT,
        logs=json.dumps(
            {
                "passed": False,
                "tests": [
                    {"name": "live health", "status": "fail", "message": "red"}
                ],
                "suites": {"startup": {"passed": False}},
            }
        ),
    )

    await maybe_spawn_smoketest_fix(
        source,
        payload,
        db_session,
        str(source.id),
        locked_project=project,
    )
    await db_session.commit()

    children = (
        await db_session.execute(select(Task).where(Task.parent_task_id == source.id))
    ).scalars().all()
    await db_session.refresh(source)
    assert children == []
    assert (source.context or {})["automatic_fix_creation_suppressed"] == (
        AUTOMATIC_TASK_CREATION_PROJECT_PAUSED
    )


@pytest.mark.asyncio
async def test_paused_debug_result_does_not_publish_fix(db_session):
    project, source = await _project_and_task(
        db_session, paused=True, task_type="debug"
    )
    payload = AgentStatusPayload(
        task_id=source.id,
        status=TaskStatus.COMPLETED,
        artifact_type=ArtifactType.DEBUG_RESULT,
        logs=json.dumps(
            {"confidence": "high", "fix_prompt": "ship the fix", "root_cause": "x"}
        ),
    )

    await maybe_spawn_debug_create(
        source,
        payload,
        db_session,
        str(source.id),
        locked_project=project,
    )
    await db_session.commit()

    children = (
        await db_session.execute(select(Task).where(Task.parent_task_id == source.id))
    ).scalars().all()
    await db_session.refresh(source)
    assert children == []
    assert (source.context or {})["automatic_fix_creation_suppressed"] == (
        AUTOMATIC_TASK_CREATION_PROJECT_PAUSED
    )


@pytest.mark.asyncio
async def test_paused_compute_disposition_checks_authority_before_child_query(
    db_session, monkeypatch
):
    project, source = await _project_and_task(
        db_session, paused=True, task_type="compute_experiment"
    )

    async def _unexpected_execute(*args, **kwargs):
        raise AssertionError("child query ran after pause denied admission")

    monkeypatch.setattr(db_session, "execute", _unexpected_execute)
    child = await _spawn_act_on_verdict_followon(
        db_session,
        source,
        {"outcome": "SUCCESS", "final_cost_usd": 1.0},
        "{}",
        automatic_admission=AUTOMATIC_TASK_CREATION_PROJECT_PAUSED,
        locked_project=project,
    )

    assert child is None
    assert (source.context or {})["automatic_followup_creation_suppressed"] == (
        AUTOMATIC_TASK_CREATION_PROJECT_PAUSED
    )


def test_wake_followup_unavailable_is_deferred_but_pause_is_terminal():
    task = Task(
        project_id=uuid.uuid4(),
        title="compute wake",
        payloads=[{"type": "compute_experiment"}],
        context={},
    )

    assert not _admit_compute_followup(
        task,
        AUTOMATIC_TASK_CREATION_PROJECT_UNAVAILABLE,
        failure_mode=True,
        sweep_id=None,
        defer_unavailable=True,
    )
    assert (task.context or {})[_COMPUTE_WAKE_FOLLOWUP_PENDING_KEY] == {
        "failure_mode": True
    }

    assert not _admit_compute_followup(
        task,
        AUTOMATIC_TASK_CREATION_PROJECT_PAUSED,
        failure_mode=True,
        sweep_id=None,
        defer_unavailable=True,
    )
    assert _COMPUTE_WAKE_FOLLOWUP_PENDING_KEY not in (task.context or {})
    assert (task.context or {})["automatic_followup_creation_suppressed"] == (
        AUTOMATIC_TASK_CREATION_PROJECT_PAUSED
    )


@pytest.mark.skipif(not USE_POSTGRES, reason="row-level locking requires PostgreSQL")
@pytest.mark.asyncio
async def test_two_in_progress_heartbeats_do_not_take_project_lock(db_session):
    """Ordinary heartbeats stay concurrent while another tx owns Project."""

    project = Project(name="heartbeat-project-lock-control", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    project_id = project.id

    tasks = [
        Task(
            project_id=project_id,
            title=f"heartbeat {index}",
            payloads=[{"type": "create"}],
            status=TaskStatus.IN_PROGRESS,
            context={},
        )
        for index in range(2)
    ]
    payloads = [
        AgentStatusPayload(task_id=task.id, status=TaskStatus.IN_PROGRESS)
        for task in tasks
    ]
    Session = sessionmaker(
        bind=db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    async with Session() as holder:
        await holder.execute(
            select(Project)
            .where(Project.id == project_id)
            .with_for_update(key_share=True)
        )
        try:
            async with Session() as first, Session() as second:
                results = await asyncio.wait_for(
                    asyncio.gather(
                        _lock_agent_status_project_authority(
                            first, tasks[0], payloads[0]
                        ),
                        _lock_agent_status_project_authority(
                            second, tasks[1], payloads[1]
                        ),
                    ),
                    timeout=0.5,
                )
        finally:
            await holder.rollback()

    assert results == [None, None]


@pytest.mark.asyncio
async def test_failed_status_is_spawn_capable_and_requests_project_authority(
    db_session,
):
    project, source = await _project_and_task(
        db_session, paused=False, task_type="create"
    )
    payload = AgentStatusPayload(task_id=source.id, status=TaskStatus.FAILED)

    locked = await _lock_agent_status_project_authority(db_session, source, payload)

    assert locked is not None
    assert locked.id == project.id
    await db_session.rollback()
