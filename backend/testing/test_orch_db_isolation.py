"""Regression guard for the shard-5 orchestrator dispatch flake.

Symptom: ``TestWarmVMSkipProvision::test_resumed_project_queued_task_is_dispatched``
(and ``...::test_pool_dispatch_cold_fall_through_records_funnel``) intermittently
failed in CI with the dispatched task observed QUEUED instead of PROVISIONING.

Root cause: the orchestrator one-cycle test harness (``orch_warm_db``) binds
EVERY ``AsyncSessionLocal()`` session to ONE shared aiosqlite connection
(``StaticPool``, required so a ``:memory:`` SQLite DB survives across sessions).
The dispatch path commits ``status=PROVISIONING`` INLINE, then fires the real VM
provision on an INDEPENDENT ``asyncio.create_task(_provision_and_handle(...))``
that the cycle harness did not cancel. Left dangling, that coroutine opens its
own session on the SAME shared connection and runs uncoordinated transaction
work (``_lock_provisioning_task_for_vm`` + commit/rollback). Because both
sessions multiplex one connection, a transaction RESET (rollback / new BEGIN) on
that connection can DISCARD a peer session's flushed-but-uncommitted state
(SQLAlchemy: "Session's state has been changed on a non-active transaction —
this state will be discarded"), so a PROVISIONING write never lands and the row
stays QUEUED.

This module demonstrates the corruption DETERMINISTICALLY against the same
StaticPool design the fixture uses, and asserts the positive control (no
concurrent session on the shared connection → the read is clean). The actual
fix lives in ``TestWarmVMSkipProvision._run_one_cycle`` (cancel + clear the
dangling provision coroutines before the verify); see
``test_orchestrator.py::...::test_cycle_leaves_no_dangling_provision_coroutine``
for the harness-level invariant guard.
"""

import contextlib

import pytest
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, select
from sqlalchemy.dialects.sqlite.base import (
    SQLiteTypeCompiler as _SQLiteTypeCompiler,
)

from app.models import Task, TaskStatus
from testing.factories import make_project, make_task

_TEST_USER = "test-user-123"


if not hasattr(_SQLiteTypeCompiler, "visit_JSONB"):
    _SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: self.visit_JSON(
        type_, **kw
    )


async def _make_engine_and_session():
    """Build the same shared-connection StaticPool in-memory engine the
    ``orch_warm_db`` fixture uses."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    Session = sessionmaker(
        bind=engine,
        class_=AsyncSession,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
    )
    return engine, Session


async def _seed_queued_task(Session):
    async with Session() as s:
        project = make_project(name="Iso", user_id=_TEST_USER)
        s.add(project)
        await s.commit()
        await s.refresh(project)
        task = make_task(
            project_id=project.id,
            vm_instance_id="agent-vm-iso-1",
            status=TaskStatus.QUEUED,
            payloads=[{"type": "fix", "prompt": "x"}],
            context={},
        )
        s.add(task)
        await s.commit()
        await s.refresh(task)
        return task.id


async def _reset_to_queued(Session, task_id):
    async with Session() as r:
        res = await r.execute(select(Task).where(Task.id == task_id))
        t = res.scalars().first()
        t.status = TaskStatus.QUEUED
        r.add(t)
        await r.commit()


async def _commit_provisioning_with_interference(
    Session, task_id, *, interfere: bool
):
    """Commit ``status=PROVISIONING`` on session A. When ``interfere`` is True a
    concurrent session B resets the SHARED connection's transaction between A's
    flush and A's commit — the exact dangling-``_provision_and_handle`` race.
    Returns the status a fresh verify read observes afterward.
    """
    session_a = Session()
    res_a = await session_a.execute(select(Task).where(Task.id == task_id))
    ta = res_a.scalars().first()
    ta.status = TaskStatus.PROVISIONING
    session_a.add(ta)
    # Flush A's PROVISIONING write into the shared connection's transaction
    # without committing yet (mirrors the dispatch session mid-commit window).
    await session_a.flush()

    if interfere:
        # A concurrent background session on the SAME shared connection resets
        # its transaction. This is what a dangling _provision_and_handle does
        # (open AsyncSessionLocal(), run DB work, rollback / close).
        async with Session() as b:
            res_b = await b.execute(select(Task).where(Task.id == task_id))
            _ = res_b.scalars().first()
            await b.rollback()

    # A commits — on a connection whose transaction may have been reset. The
    # commit may raise if the connection's transaction was already discarded;
    # the corruption we assert on is the resulting persisted row, not the raise.
    with contextlib.suppress(Exception):
        await session_a.commit()
    await session_a.close()

    async with Session() as verify:
        res_v = await verify.execute(select(Task).where(Task.id == task_id))
        row = res_v.scalars().first()
        return row.status if row is not None else None


@pytest.mark.asyncio
async def test_shared_connection_background_reset_discards_provisioning():
    """WITH interference (a dangling background session resets the shared
    connection mid-commit), the inline PROVISIONING write is discarded and the
    row stays QUEUED — the shard-5 corruption. This proves the hazard is real
    and connection-sharing-bound, not a flaky environmental artifact."""
    engine, Session = await _make_engine_and_session()
    try:
        task_id = await _seed_queued_task(Session)

        observed = await _commit_provisioning_with_interference(
            Session, task_id, interfere=True
        )

        assert observed == TaskStatus.QUEUED, (
            "expected the shared-connection reset to discard the PROVISIONING "
            f"commit (corruption repro), but observed {observed} — the hazard "
            "model no longer holds; revisit the fix rationale"
        )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_no_background_session_commit_lands_cleanly():
    """Positive control / the fix's effect: with NO concurrent session touching
    the shared connection (the dangling provision coroutine cancelled before any
    DB work), the inline PROVISIONING commit lands and the verify reads it."""
    engine, Session = await _make_engine_and_session()
    try:
        task_id = await _seed_queued_task(Session)

        observed = await _commit_provisioning_with_interference(
            Session, task_id, interfere=False
        )

        assert observed == TaskStatus.PROVISIONING, (
            "with the shared connection free of concurrent background work the "
            f"PROVISIONING commit must land, got {observed}"
        )
    finally:
        await engine.dispose()
