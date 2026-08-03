"""Concurrency coverage for the bulk Strategic task cancellation fence."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker

from app import strategic_cycle, task_locking
from app.models import Project, StrategicCycle, Task, TaskStatus


USE_POSTGRES = os.getenv("USE_TEST_POSTGRES", "").lower() == "true" and os.getenv(
    "DATABASE_URL", ""
).startswith("postgresql")


@pytest.mark.skipif(
    not USE_POSTGRES,
    reason="deterministic row-lock contention requires PostgreSQL",
)
@pytest.mark.asyncio
async def test_bulk_cancel_waits_for_completion_and_preserves_terminal_cycle(
    db_session, mocker
):
    """A completion holding the canonical pair wins over a stale bulk request."""
    from app.admin import _bulk_cancel_task

    project = Project(name="bulk cancellation fence", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="strategic phase 2",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "strategic_review", "prompt": "review"}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    cycle = StrategicCycle(
        id="sc-bulk-fence-race",
        project_id=project.id,
        outcome=strategic_cycle.OUTCOME_PHASE2_RUNNING,
        phase1_started_at=task.created_at,
        phase2_started_at=task.created_at,
        phase2_task_id=task.id,
    )
    db_session.add(cycle)
    await db_session.commit()

    Session = sessionmaker(
        bind=db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    completion_holds_lock = asyncio.Event()
    release_completion = asyncio.Event()
    bulk_lock_attempted = asyncio.Event()
    bulk_session: AsyncSession | None = None
    original_lock = task_locking.lock_project_then_task

    @asynccontextmanager
    async def observed_lock(session, project_id, task_id, **kwargs):
        if session is bulk_session:
            bulk_lock_attempted.set()
        async with original_lock(session, project_id, task_id, **kwargs) as pair:
            yield pair

    mocker.patch.object(task_locking, "lock_project_then_task", observed_lock)

    async def authoritative_completion() -> None:
        async with Session() as completion_session:
            async with task_locking.lock_project_then_task(
                completion_session,
                project.id,
                task.id,
                skip_locked=False,
            ) as (_project, locked_task):
                assert locked_task is not None
                locked_task.status = TaskStatus.COMPLETED
                await strategic_cycle.mark_phase2_completed(
                    completion_session,
                    cycle.id,
                    summary_text="authoritative completion",
                )
                completion_session.add(locked_task)
                completion_holds_lock.set()
                await release_completion.wait()
                await completion_session.commit()

    async with Session() as cancellation_session:
        bulk_session = cancellation_session
        stale_task = await cancellation_session.get(Task, task.id)
        assert stale_task is not None
        assert stale_task.status == TaskStatus.IN_PROGRESS
        completion = asyncio.create_task(authoritative_completion())
        await asyncio.wait_for(completion_holds_lock.wait(), timeout=5)

        cancellation = asyncio.create_task(
            _bulk_cancel_task(
                cancellation_session,
                task_id=task.id,
                project_id=project.id,
            )
        )
        await asyncio.wait_for(bulk_lock_attempted.wait(), timeout=5)
        assert not cancellation.done(), "bulk cancellation must wait on completion"

        release_completion.set()
        cancellation_result = await asyncio.wait_for(cancellation, timeout=5)
        await asyncio.wait_for(completion, timeout=5)

    await db_session.refresh(task)
    await db_session.refresh(cycle)
    assert cancellation_result.startswith("Task already in terminal state ")
    assert cancellation_result.rsplit(" ", 1)[-1] == TaskStatus.COMPLETED.value
    assert task.status == TaskStatus.COMPLETED
    assert cycle.outcome == strategic_cycle.OUTCOME_COMPLETED
    assert cycle.outcome not in strategic_cycle.ACTIVE_OUTCOMES
