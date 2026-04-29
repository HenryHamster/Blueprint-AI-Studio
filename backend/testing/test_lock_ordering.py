"""Phase C1 lock-ordering regression tests.

The audit found cross-table lock-ordering deadlock potential (CRITICAL
C5): some paths locked Project then Task, others locked Task then
Project. Two flows holding opposite orders deadlock until the 30s
``lock_timeout`` fires and surfaces ``LockNotAvailable`` as 500 — Cloud
Run then quarantines the instance.

The fix established **Project before Task, always** as the canonical
order, plus the ``lock_project_then_task`` helper. These tests guard
against regression.

Postgres-only: SQLite has no row-level locking semantics, so contention
is trivial there. The deadlock risk only exists on Postgres.
"""
from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker

from app.models import Project, Task, TaskStatus
from app.task_locking import lock_project_then_task


USE_POSTGRES = (
    os.getenv("USE_TEST_POSTGRES", "").lower() == "true"
    and os.getenv("DATABASE_URL", "").startswith("postgresql")
)


@pytest.mark.skipif(not USE_POSTGRES, reason="row-level locking requires PostgreSQL")
@pytest.mark.asyncio
async def test_lock_project_then_task_canonical_order_no_deadlock(db_session):
    """Two concurrent flows competing for the SAME (project, task) pair must
    serialize via the canonical Project-before-Task order: one wins both
    locks first, the other waits at the project lock and gets
    ``(None, None)`` under skip_locked. Neither deadlocks.

    Regression for the audit C5 scenario: prior code mixed orders so
    flow A (Project→Task) and flow B (Task→Project) could each acquire
    the first row and then block on the second, hitting Postgres'
    ``lock_timeout``.
    """
    project = Project(name="C1 lock-order test", user_id="user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="C1 task",
        status=TaskStatus.QUEUED,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    Session = sessionmaker(
        bind=db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    first_holds_project = asyncio.Event()
    outcomes: list[str] = []

    async def first_flow() -> None:
        async with Session() as s, lock_project_then_task(
            s, project.id, task.id, skip_locked=False, timeout=5,
        ) as (proj, t):
            assert proj is not None and t is not None
            first_holds_project.set()
            # Hold both locks briefly so the second flow's project-lock
            # attempt sees contention.
            await asyncio.sleep(0.2)
            outcomes.append("first")
            await s.commit()

    async def second_flow() -> None:
        await first_holds_project.wait()
        async with Session() as s, lock_project_then_task(
            s, project.id, task.id, skip_locked=True,
        ) as (proj, t):
            # Under SKIP LOCKED, the second flow must observe the project
            # row as held — and per the helper contract, the task lock is
            # NOT attempted in that case (we don't sit on a half-pair).
            assert proj is None, "second flow should see project as held"
            assert t is None, "task lock must not be attempted when project is held"
            outcomes.append("second-deferred")
            await s.commit()

    # Bounded by asyncio.wait_for so a regression that deadlocks fails
    # the test loudly rather than hanging the suite.
    await asyncio.wait_for(
        asyncio.gather(first_flow(), second_flow()),
        timeout=10,
    )

    # Both flows must complete; second saw contention and deferred, first
    # held both locks through its sleep. Order in `outcomes` is not
    # significant: under SKIP LOCKED the second flow returns immediately
    # when the project lock is held, so it often finishes its commit
    # before the first flow's sleep completes — that's correct
    # serialization, not a regression.
    assert sorted(outcomes) == sorted(["first", "second-deferred"]), (
        f"Expected both flows to complete without deadlock, got {outcomes}"
    )


@pytest.mark.asyncio
async def test_lock_project_then_task_yields_none_on_missing_project(db_session):
    """When the project doesn't exist, the helper yields ``(None, None)``
    immediately and never attempts the task lock. The "missing" and
    "held" cases both surface as ``(None, None)`` under skip_locked —
    callers that need to disambiguate must do their own follow-up probe
    (the audit pattern is to issue a non-locking SELECT for the project
    id; a missing row is a hard error, contention defers).
    """
    missing_project_id = uuid.uuid4()
    missing_task_id = uuid.uuid4()
    async with lock_project_then_task(
        db_session, missing_project_id, missing_task_id, skip_locked=True,
    ) as (proj, t):
        assert proj is None
        assert t is None
