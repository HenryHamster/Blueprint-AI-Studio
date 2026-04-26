"""Row-lock helpers for task / deployment-state mutations.

Audit 2026-04-25 (W1) found that the orchestrator dispatch loop and CI
polling loop load tasks once and then mutate them repeatedly across the
loop body without re-acquiring row locks. Concurrent dispatch instances,
the reaper, and status POST handlers race on the same in-memory copy and
overwrite each other's mutations. This drove the ~30-40% baseline failure
rate flagged in ``project_deferred_robustness.md``.

Use ``task_lock`` anywhere a task field is mutated under contention. The
yielded Task is freshly loaded inside the lock; the lock releases when
the surrounding session commits or rolls back.

For the singleton ``DeploymentState`` row, use ``deployment_state_lock``
which adds an optimistic-concurrency ``version`` bump alongside the
SELECT FOR UPDATE so callers that mutate without holding the lock for
the entire write window can still detect lost-update races.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DeploymentState, Task

logger = logging.getLogger(__name__)


@asynccontextmanager
async def task_lock(
    session: AsyncSession,
    task_id,
    *,
    skip_locked: bool = True,
    timeout: float = 30.0,
) -> AsyncIterator[Optional[Task]]:
    """Acquire SELECT FOR UPDATE on a task row, yield freshly-loaded Task or None.

    Args:
        session: AsyncSession to issue the lock against. The lock is held
            until this session commits or rolls back — the caller MUST
            commit/rollback before exiting the ``async with`` block.
        task_id: UUID or str of the task.
        skip_locked: If True (default), yield ``None`` immediately when the row
            is held by another session. Caller must check for None and skip
            the iteration. If False, wait up to ``timeout`` seconds and raise
            ``OperationalError`` on timeout.
        timeout: Max seconds to wait when ``skip_locked=False``. Ignored
            when skip_locked=True (the SELECT returns immediately either
            way; the asyncio timeout is only meaningful for the blocking
            variant or to bound the wait if the DB itself is slow).

    Yields:
        The freshly-loaded Task object inside the lock, or None if the row
        was held and skip_locked=True.

    Example:
        async with task_lock(session, task.id) as t:
            if t is None:
                continue  # held by another worker
            t.retry_after_until = None
            t.requeue_in_flight = True
        await session.commit()  # release lock
    """
    stmt = (
        select(Task)
        .where(Task.id == task_id)
        .with_for_update(skip_locked=skip_locked)
    )
    try:
        result = await asyncio.wait_for(session.execute(stmt), timeout=timeout)
    except asyncio.TimeoutError as e:
        logger.warning(
            "[task_lock] timeout acquiring lock on task %s after %.1fs (skip_locked=%s)",
            task_id, timeout, skip_locked,
        )
        raise OperationalError("task_lock acquisition timed out", None, e) from e
    yield result.scalar_one_or_none()


@asynccontextmanager
async def deployment_state_lock(
    session: AsyncSession,
    *,
    skip_locked: bool = True,
    timeout: float = 30.0,
) -> AsyncIterator[Optional[DeploymentState]]:
    """Acquire SELECT FOR UPDATE on the singleton DeploymentState row.

    Bumps ``version`` automatically when the caller exits the block normally
    (i.e. didn't set ``ds.version_unchanged = True``). Callers that mutate
    DeploymentState should use this helper everywhere; mixing locked and
    unlocked writes is the F4 race condition flagged in the audit.

    See ``task_lock`` for arg semantics — same shape.
    """
    stmt = (
        select(DeploymentState)
        .where(DeploymentState.id == 1)
        .with_for_update(skip_locked=skip_locked)
    )
    try:
        result = await asyncio.wait_for(session.execute(stmt), timeout=timeout)
    except asyncio.TimeoutError as e:
        logger.warning(
            "[deployment_state_lock] timeout after %.1fs (skip_locked=%s)",
            timeout, skip_locked,
        )
        raise OperationalError("deployment_state_lock acquisition timed out", None, e) from e
    ds = result.scalar_one_or_none()
    if ds is None:
        yield None
        return
    starting_version = ds.version
    yield ds
    # Auto-bump version on exit if the row was actually modified. Detect
    # modification by comparing the SQLAlchemy session.dirty set; if the
    # caller explicitly set version_unchanged=True (read-only inside lock)
    # skip the bump.
    if getattr(ds, "_version_unchanged", False):
        return
    if ds in session.dirty or ds.version == starting_version:
        ds.version = starting_version + 1
