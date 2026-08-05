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


@pytest.mark.skipif(
    not USE_POSTGRES,
    reason="deterministic row-lock contention requires PostgreSQL",
)
@pytest.mark.asyncio
async def test_bulk_cancel_cascade_preserves_terminal_child(db_session, mocker):
    """A cascade child that completes between the ID query and the re-lock must not be
    force-flipped to CANCELLED.

    The bulk cancel path queries active child IDs, then re-reads each under a
    row lock (skip_locked=False, which waits). A child that completes while the
    bulk cancel is waiting for its lock is re-read as COMPLETED. Without the
    terminal-status filter added after the cascade_targets collection loop, that
    child would be force-set to CANCELLED, overwriting a real terminal outcome.
    """
    from app.admin import _bulk_cancel_task
    from sqlalchemy.ext.asyncio import AsyncSession

    project = Project(name="cascade fencing test", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    parent = Task(
        project_id=project.id,
        title="cascade parent",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "strategic_review", "prompt": "parent"}],
    )
    db_session.add(parent)
    await db_session.commit()
    await db_session.refresh(parent)

    child = Task(
        project_id=project.id,
        title="cascade child",
        status=TaskStatus.IN_PROGRESS,
        parent_task_id=parent.id,
        payloads=[{"type": "create", "prompt": "child"}],
    )
    db_session.add(child)
    await db_session.commit()
    await db_session.refresh(child)

    Session = sessionmaker(
        bind=db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    # Simulate the race: the child completes while bulk_cancel waits for the child's lock.
    child_lock_attempted = asyncio.Event()
    release_child_completion = asyncio.Event()
    completion_holds_child_lock = asyncio.Event()
    cancel_session_ref: AsyncSession | None = None
    original_lock = task_locking.lock_project_then_task

    @asynccontextmanager
    async def intercepted_lock(session, project_id, task_id, **kwargs):
        nonlocal cancel_session_ref
        if session is cancel_session_ref and task_id == child.id:
            child_lock_attempted.set()
        async with original_lock(session, project_id, task_id, **kwargs) as pair:
            yield pair

    mocker.patch.object(task_locking, "lock_project_then_task", intercepted_lock)

    async def complete_child_while_cancel_waits() -> None:
        """Lock the child task row directly (no project lock) so _bulk_cancel_task
        can acquire the project lock for the parent without conflict.  Mark the
        child COMPLETED and hold its row lock until signalled."""
        from sqlalchemy import select as _select

        async with Session() as s:
            async with s.begin():
                result = await s.execute(
                    _select(Task).where(Task.id == child.id).with_for_update()
                )
                locked_child = result.scalar_one()
                locked_child.status = TaskStatus.COMPLETED
                s.add(locked_child)
                completion_holds_child_lock.set()
                await release_child_completion.wait()
                # s.begin() commits here, releasing the child lock

    async with Session() as cancel_session:
        cancel_session_ref = cancel_session

        # Let the child-completion task acquire the child's lock first.
        completion = asyncio.create_task(complete_child_while_cancel_waits())
        await asyncio.wait_for(completion_holds_child_lock.wait(), timeout=5)

        # Now run bulk_cancel — it will block waiting for the child's lock.
        cancellation = asyncio.create_task(
            _bulk_cancel_task(
                cancel_session,
                task_id=parent.id,
                project_id=project.id,
            )
        )
        await asyncio.wait_for(child_lock_attempted.wait(), timeout=5)
        assert not cancellation.done(), "bulk cancellation must wait for the child lock"

        # Release the child-completion transaction; bulk_cancel re-reads child as COMPLETED.
        release_child_completion.set()
        cancellation_result = await asyncio.wait_for(cancellation, timeout=5)
        await asyncio.wait_for(completion, timeout=5)

    # The parent must be CANCELLED; the child that completed must remain COMPLETED.
    await db_session.refresh(parent)
    await db_session.refresh(child)
    assert cancellation_result is None, f"expected parent cancelled; got: {cancellation_result}"
    assert parent.status == TaskStatus.CANCELLED
    assert child.status == TaskStatus.COMPLETED, (
        f"child completed before cancel re-read its status; must not be overwritten "
        f"(got {child.status})"
    )
