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

Audit 2026-04-29 (Phase A1) added project-row helpers for the same
rationale: 9 sites used plain ``with_for_update()`` without
``skip_locked``, so the 30s ``lock_timeout`` could fire and propagate
``LockNotAvailableError`` as a 500 — Cloud Run then quarantines the
instance. ``lock_project_or_429`` is the canonical helper for handler
paths (returns 429 Retry-After on contention); ``project_lock`` is the
context-manager variant for background / post-commit chains that need
``skip_locked`` + structured retry.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

import uuid
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DeploymentState, Project, Task

logger = logging.getLogger(__name__)


@asynccontextmanager
async def task_lock(
    session: AsyncSession,
    task_id,
    *,
    skip_locked: bool = True,
    timeout: float = 30.0,
) -> AsyncIterator[Task | None]:
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
) -> AsyncIterator[DeploymentState | None]:
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


async def lock_project_or_429(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> Project:
    """Acquire a row lock on a Project for atomic read-modify-write (handler path).

    Uses SKIP LOCKED so concurrent agent writes don't queue behind the 30s
    lock_timeout set in database.py. If another handler currently holds the
    lock, returns 429 Retry-After so the client retries (same contract as
    agent_status.py uses for task-row contention; see commit 88a77903).
    Cloud Run flags any 5xx as ContainerError and quarantines the instance,
    so 429 is deliberate — it conveys "temporary, retry" without tripping
    container-fault classification.

    Use this from request handlers where the user can retry on 429. For
    background loops or post-commit chains that should not bubble up an
    HTTPException, use ``project_lock`` instead.
    """
    result = await session.execute(
        select(Project).where(Project.id == project_id).with_for_update(skip_locked=True)
    )
    project = result.scalars().first()
    if project is not None:
        return project
    # Distinguish "row locked" from "row missing" with a non-locking read.
    exists = (await session.execute(
        select(Project.id).where(Project.id == project_id)
    )).first()
    if exists is None:
        raise HTTPException(status_code=404, detail="Project not found")
    raise HTTPException(
        status_code=429,
        detail="Project row locked by concurrent write, retry",
        headers={"Retry-After": "2"},
    )


async def acquire_pr_spawn_lock(session: AsyncSession, pr_number: int) -> None:
    """Serialize pipeline-task spawns for a single PR via a Postgres advisory lock.

    The dedup logic at the top of ``_spawn_pipeline_task`` reads existing
    pipeline tasks (SELECT … status IN active), decides whether to dedup /
    supersede / proceed, then inserts a new task. Two concurrent callers
    for the same PR can both pass the SELECT before either commits — and
    because each spawn computes its own ``idempotency_key``, the existing
    partial UNIQUE on ``ux_task_active_idempotency_key`` does NOT catch
    that race. The result is a double-spawn that wastes an agent slot and
    has both review/fix tasks running against the same PR.

    ``pg_advisory_xact_lock(pr_number)`` makes the SELECT-decide-INSERT
    window structurally serial across concurrent transactions on the same
    PR. The lock is held until the surrounding transaction commits or
    rolls back, so the caller MUST commit (or rollback) at the end of the
    spawn flow to release it. Different PRs hash to different lock keys
    and proceed in parallel.

    No-op on non-Postgres dialects (SQLite tests rely on
    serialization-by-event-loop within a single process and don't need
    the lock).

    Args:
        session: AsyncSession to issue the lock against. Must be the same
            session that performs the subsequent SELECT/INSERT — the lock
            is transaction-scoped, not session-scoped.
        pr_number: Integer PR number. Cast to int for the int8 advisory
            key; numbers up to 2**63-1 are valid keys.
    """
    bind = session.bind
    if bind is None or bind.dialect.name != "postgresql":
        return
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:pr)"),
        {"pr": int(pr_number)},
    )


@asynccontextmanager
async def project_lock(
    session: AsyncSession,
    project_id,
    *,
    skip_locked: bool = True,
    timeout: float = 30.0,
) -> AsyncIterator[Project | None]:
    """Acquire SELECT FOR UPDATE on a project row, yield freshly-loaded Project or None.

    Mirror of ``task_lock`` for the project row. Use from background
    loops or post-commit chains that should react to contention rather
    than surface a 5xx — when ``skip_locked=True`` (the default) the
    contextmanager yields ``None`` immediately if the row is held by
    another session, and the caller must check for None and choose
    whether to skip or retry.

    For request handlers that should bubble a retry-friendly 429 to the
    client, use ``lock_project_or_429`` instead.

    Args:
        session: AsyncSession to issue the lock against. Lock is held
            until commit/rollback.
        project_id: UUID or str of the project.
        skip_locked: If True (default), yield ``None`` when the row is
            held; if False, wait up to ``timeout`` and raise on timeout.
        timeout: Max seconds to wait when ``skip_locked=False``.

    Yields:
        The freshly-loaded Project object, or None if held under skip_locked.
    """
    stmt = (
        select(Project)
        .where(Project.id == project_id)
        .with_for_update(skip_locked=skip_locked)
    )
    try:
        result = await asyncio.wait_for(session.execute(stmt), timeout=timeout)
    except asyncio.TimeoutError as e:
        logger.warning(
            "[project_lock] timeout acquiring lock on project %s after %.1fs (skip_locked=%s)",
            project_id, timeout, skip_locked,
        )
        raise OperationalError("project_lock acquisition timed out", None, e) from e
    yield result.scalar_one_or_none()
