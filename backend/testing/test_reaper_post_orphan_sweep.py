"""Tests for app/reaper_post_orphan_sweep.py rollup-loop batch-refresh logic.

Focus: the _rollup_task_batch batch-fetch pattern in run_post_orphan_sweep
(lines 120-171), specifically:

  1. Happy path: _check_and_trigger_rollup is called for FAILED tasks with parents.
  2. Non-failed / no-parent tasks are skipped without calling rollup check.
  3. Explicit-exception path: when _check_and_trigger_rollup raises, the batch is
     re-fetched and the next task is still processed (the working exception path).
  4. Multiple tasks with first already erroring: second task is still visited.
  5. Regression / gap test: when _check_and_trigger_rollup performs an internal
     session.rollback() (via _sess_poisoned path) WITHOUT re-raising, the sweep's
     _rollup_task_batch holds expired ORM objects; the next iteration's attribute
     access raises MissingGreenlet because the internal rollback is not signalled
     back to the caller. Marked xfail until the fix (return rolled_back=True) lands.
"""

import contextlib
import os
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel

from app.models import Project, Task, TaskStatus
from app.reaper_post_orphan_sweep import run_post_orphan_sweep

USE_POSTGRES = os.getenv("USE_TEST_POSTGRES", "").lower() == "true" and os.getenv(
    "DATABASE_URL", ""
).startswith("postgresql")


# ── SQLite fixture ────────────────────────────────────────────────────────────


@pytest_asyncio.fixture()
async def sweep_db():
    """Isolated in-memory SQLite session for post-orphan-sweep tests."""
    from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler

    if not hasattr(SQLiteTypeCompiler, "visit_JSONB"):
        SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: self.visit_JSON(
            type_, **kw
        )

    _engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with _engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    _Session = sessionmaker(
        bind=_engine,
        class_=AsyncSession,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
    )

    async with _Session() as session:
        yield session

    async with _engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
    await _engine.dispose()


# ── Helpers ───────────────────────────────────────────────────────────────────


async def _make_project(session) -> Project:
    project = Project(name="sweep-test", user_id="user1")
    session.add(project)
    await session.commit()
    await session.refresh(project)
    return project


async def _make_task(
    session,
    project_id,
    *,
    status=TaskStatus.FAILED,
    parent_task_id=None,
    task_type="create",
    context=None,
) -> Task:
    task = Task(
        project_id=project_id,
        payloads=[{"type": task_type, "prompt": "test"}],
        status=status,
        parent_task_id=parent_task_id,
        context=context or {},
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task


def _noop_asl():
    """A no-op AsyncSessionLocal context manager for dep-unblock / safeguard loops."""

    class _NullCtx:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def execute(self, *a, **kw):
            class _R:
                def scalars(self_):
                    class _S:
                        def all(self_2):
                            return []

                    return _S()

            return _R()

        def add(self, obj):
            pass

        async def get(self, model, pk):
            return None

        async def commit(self):
            pass

    class _Factory:
        def __call__(self):
            return _NullCtx()

    return _Factory()


async def _run_sweep(
    session,
    orphaned_tasks,
    *,
    orphaned_task_ids,
    mocker,
):
    """Run run_post_orphan_sweep with shared defaults for sweep tests.

    Patches AsyncSessionLocal (dep-unblock / safeguard gate) to a no-op so tests
    don't need JSONB-compatible dep queries against SQLite.
    """
    mocker.patch(
        "app.reaper_post_orphan_sweep.AsyncSessionLocal", _noop_asl()
    )
    landed = AsyncMock()
    await run_post_orphan_sweep(
        session,
        orphaned_tasks,
        orphan_commit_ok=True,
        orphaned_task_ids=orphaned_task_ids,
        deferred_parent_requeues=[],
        reaper_old_vms={},
        progress_cb=lambda: None,
        landed_cb=landed,
    )
    return landed


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rollup_loop_calls_check_for_failed_task_with_parent(sweep_db, mocker):
    """A FAILED task with a parent triggers _check_and_trigger_rollup exactly once."""
    mock_rollup = mocker.patch(
        "app.rollup_management._check_and_trigger_rollup", new_callable=AsyncMock
    )

    project = await _make_project(sweep_db)
    parent = await _make_task(sweep_db, project.id, status=TaskStatus.EVALUATING)
    child = await _make_task(
        sweep_db, project.id, status=TaskStatus.FAILED, parent_task_id=parent.id
    )

    await _run_sweep(
        sweep_db,
        orphaned_tasks=[child],
        orphaned_task_ids=[child.id],
        mocker=mocker,
    )

    mock_rollup.assert_awaited_once()
    call_args = mock_rollup.call_args
    assert call_args[0][0].id == child.id


@pytest.mark.asyncio
async def test_rollup_loop_skips_task_with_no_parent(sweep_db, mocker):
    """A FAILED task WITHOUT a parent_task_id is not passed to rollup check."""
    mock_rollup = mocker.patch(
        "app.rollup_management._check_and_trigger_rollup", new_callable=AsyncMock
    )

    project = await _make_project(sweep_db)
    orphan = await _make_task(
        sweep_db, project.id, status=TaskStatus.FAILED, parent_task_id=None
    )

    await _run_sweep(
        sweep_db,
        orphaned_tasks=[orphan],
        orphaned_task_ids=[orphan.id],
        mocker=mocker,
    )

    mock_rollup.assert_not_awaited()


@pytest.mark.asyncio
async def test_rollup_loop_skips_non_failed_task(sweep_db, mocker):
    """A COMPLETED task with a parent is not passed to rollup check."""
    mock_rollup = mocker.patch(
        "app.rollup_management._check_and_trigger_rollup", new_callable=AsyncMock
    )

    project = await _make_project(sweep_db)
    parent = await _make_task(sweep_db, project.id, status=TaskStatus.EVALUATING)
    child = await _make_task(
        sweep_db, project.id, status=TaskStatus.COMPLETED, parent_task_id=parent.id
    )

    await _run_sweep(
        sweep_db,
        orphaned_tasks=[child],
        orphaned_task_ids=[child.id],
        mocker=mocker,
    )

    mock_rollup.assert_not_awaited()


@pytest.mark.asyncio
async def test_rollup_loop_processes_all_eligible_tasks(sweep_db, mocker):
    """All FAILED tasks with parents in the batch have their rollup check called."""
    call_ids = []

    async def _track(task, session, **kwargs):
        call_ids.append(task.id)

    mocker.patch(
        "app.rollup_management._check_and_trigger_rollup", side_effect=_track
    )

    project = await _make_project(sweep_db)
    parent1 = await _make_task(sweep_db, project.id, status=TaskStatus.EVALUATING)
    parent2 = await _make_task(sweep_db, project.id, status=TaskStatus.EVALUATING)
    child1 = await _make_task(
        sweep_db, project.id, status=TaskStatus.FAILED, parent_task_id=parent1.id
    )
    child2 = await _make_task(
        sweep_db, project.id, status=TaskStatus.FAILED, parent_task_id=parent2.id
    )

    await _run_sweep(
        sweep_db,
        orphaned_tasks=[child1, child2],
        orphaned_task_ids=[child1.id, child2.id],
        mocker=mocker,
    )

    assert set(call_ids) == {child1.id, child2.id}


@pytest.mark.asyncio
async def test_rollup_loop_rebatches_after_explicit_exception(sweep_db, mocker):
    """When _check_and_trigger_rollup raises explicitly, the except-handler
    re-fetches _rollup_task_batch, so the second task is still processed.

    This tests the WORKING exception path (lines 153-171 in
    reaper_post_orphan_sweep.py): the explicit raise triggers the except block
    which re-batches before the next iteration.
    """
    first_call = {"done": False}
    call_ids = []

    async def _raise_first_then_succeed(task, session, **kwargs):
        call_ids.append(task.id)
        if not first_call["done"]:
            first_call["done"] = True
            raise Exception("simulated rollup DB error")

    mocker.patch(
        "app.rollup_management._check_and_trigger_rollup",
        side_effect=_raise_first_then_succeed,
    )

    project = await _make_project(sweep_db)
    parent1 = await _make_task(sweep_db, project.id, status=TaskStatus.EVALUATING)
    parent2 = await _make_task(sweep_db, project.id, status=TaskStatus.EVALUATING)
    child1 = await _make_task(
        sweep_db, project.id, status=TaskStatus.FAILED, parent_task_id=parent1.id
    )
    child2 = await _make_task(
        sweep_db, project.id, status=TaskStatus.FAILED, parent_task_id=parent2.id
    )

    # Should NOT raise — the except handler catches the first failure,
    # rolls back, re-batches, and continues to the second task.
    await _run_sweep(
        sweep_db,
        orphaned_tasks=[child1, child2],
        orphaned_task_ids=[child1.id, child2.id],
        mocker=mocker,
    )

    # Both tasks were visited (second one after re-batch).
    assert len(call_ids) == 2
    assert set(call_ids) == {child1.id, child2.id}


@pytest.mark.asyncio
async def test_rollup_loop_noop_when_no_orphaned_tasks(sweep_db, mocker):
    """Empty orphaned_tasks list causes the sweep to return immediately."""
    mock_rollup = mocker.patch(
        "app.rollup_management._check_and_trigger_rollup", new_callable=AsyncMock
    )

    await run_post_orphan_sweep(
        sweep_db,
        [],  # empty orphaned_tasks → early return guard
        orphan_commit_ok=True,
        orphaned_task_ids=[],
        deferred_parent_requeues=[],
        reaper_old_vms={},
        progress_cb=lambda: None,
        landed_cb=AsyncMock(),
    )

    mock_rollup.assert_not_awaited()


@pytest.mark.asyncio
async def test_rollup_loop_noop_when_orphan_commit_failed(sweep_db, mocker):
    """orphan_commit_ok=False causes the sweep to return immediately."""
    mock_rollup = mocker.patch(
        "app.rollup_management._check_and_trigger_rollup", new_callable=AsyncMock
    )

    project = await _make_project(sweep_db)
    parent = await _make_task(sweep_db, project.id, status=TaskStatus.EVALUATING)
    child = await _make_task(
        sweep_db, project.id, status=TaskStatus.FAILED, parent_task_id=parent.id
    )

    await run_post_orphan_sweep(
        sweep_db,
        orphaned_tasks=[child],
        orphan_commit_ok=False,  # commit failed — sweep must no-op
        orphaned_task_ids=[child.id],
        deferred_parent_requeues=[],
        reaper_old_vms={},
        progress_cb=lambda: None,
        landed_cb=AsyncMock(),
    )

    mock_rollup.assert_not_awaited()


@pytest.mark.asyncio
async def test_rollup_loop_landed_cb_called_after_successful_rollup(sweep_db, mocker):
    """landed_cb is awaited with (parent_task_id, session) after a successful
    _check_and_trigger_rollup call."""
    mocker.patch(
        "app.rollup_management._check_and_trigger_rollup", new_callable=AsyncMock
    )
    mocker.patch(
        "app.reaper_post_orphan_sweep.AsyncSessionLocal", _noop_asl()
    )

    project = await _make_project(sweep_db)
    parent = await _make_task(sweep_db, project.id, status=TaskStatus.EVALUATING)
    child = await _make_task(
        sweep_db, project.id, status=TaskStatus.FAILED, parent_task_id=parent.id
    )

    landed = AsyncMock()
    await run_post_orphan_sweep(
        sweep_db,
        orphaned_tasks=[child],
        orphan_commit_ok=True,
        orphaned_task_ids=[child.id],
        deferred_parent_requeues=[],
        reaper_old_vms={},
        progress_cb=lambda: None,
        landed_cb=landed,
    )

    landed.assert_awaited_once_with(parent.id, sweep_db)


@pytest.mark.skipif(
    not USE_POSTGRES,
    reason=(
        "MissingGreenlet on expired ORM attribute is asyncpg-specific; aiosqlite "
        "lazy-loads succeed in the async greenlet, masking the regression. "
        "Fault injection is not practical here because the sweep builds "
        "_rollup_task_batch via internal DB queries and accesses task.status "
        "before any mockable hook is called."
    ),
)
@pytest.mark.asyncio
async def test_rollup_loop_internal_rollback_does_not_leave_expired_batch(
    sweep_db, mocker, expire_fidelity
):
    """Regression test for audit finding 94eb541158680276.

    When _check_and_trigger_rollup silently rolls back the shared session (via
    the _sess_poisoned recovery path) without re-raising, the sweep must still
    process the second task correctly. The bug: _rollup_task_batch is NOT
    re-fetched after the internal rollback, so the second iteration accesses
    expired ORM objects. In PostgreSQL (asyncpg), `task.status` on an expired
    object triggers a sync lazy-load inside the async session and raises
    MissingGreenlet. In SQLite (aiosqlite), the lazy-refresh succeeds implicitly,
    masking the bug — but the test still asserts that both tasks were processed,
    which validates the fix's correctness regardless of backend.

    Fix: make _check_and_trigger_rollup return rolled_back=True when it
    performs an internal rollback, and update the sweep loop to re-batch
    whenever rolled_back=True is returned.
    """
    call_task_ids: list = []
    execute_counts_at_rollup_calls: list[int] = []

    async def _internal_rollback_first_then_return_true(
        task, session, **kwargs
    ):
        """Simulate the _sess_poisoned path on the FIRST call.

        First call: call session.rollback() without re-raising (exactly what the
        _sess_poisoned branch in _check_and_trigger_rollup does today), then
        return True to signal the silent rollback.
        Subsequent calls: just record the task id to verify they complete.
        """
        if not call_task_ids:
            # Snapshot id BEFORE rollback — task.id on an expired object raises
            # MissingGreenlet under fidelity mode (real _expire active).
            _task_id = task.id
            # First call: simulate internal rollback, return normally (no re-raise).
            with contextlib.suppress(Exception):
                await session.rollback()
            # Record execute count BEFORE any re-fetch, so we can prove a
            # re-query happened between the first and second rollup call.
            execute_counts_at_rollup_calls.append(execute_spy.call_count)
            call_task_ids.append(_task_id)
            return True
        execute_counts_at_rollup_calls.append(execute_spy.call_count)
        call_task_ids.append(task.id)

    mocker.patch(
        "app.rollup_management._check_and_trigger_rollup",
        side_effect=_internal_rollback_first_then_return_true,
    )
    mocker.patch("app.reaper_post_orphan_sweep.AsyncSessionLocal", _noop_asl())

    project = await _make_project(sweep_db)
    parent1 = await _make_task(sweep_db, project.id, status=TaskStatus.EVALUATING)
    parent2 = await _make_task(sweep_db, project.id, status=TaskStatus.EVALUATING)
    child1 = await _make_task(
        sweep_db, project.id, status=TaskStatus.FAILED, parent_task_id=parent1.id
    )
    child2 = await _make_task(
        sweep_db, project.id, status=TaskStatus.FAILED, parent_task_id=parent2.id
    )
    execute_spy = mocker.spy(sweep_db, "execute")

    # Snapshot IDs before the sweep: the mock calls session.rollback() which
    # expires all session objects. Under fidelity mode (real _expire active),
    # accessing .id on an expired object raises MissingGreenlet. child1/child2
    # are re-fetched by the production code so they survive, but parent1/parent2
    # are NOT in orphaned_task_ids and remain expired after the sweep.
    child1_id, child2_id = child1.id, child2.id

    # With the fix: the sweep re-batches after detecting the internal rollback
    # (via the returned rolled_back=True sentinel) and processes both tasks.
    landed = AsyncMock()
    await run_post_orphan_sweep(
        sweep_db,
        orphaned_tasks=[child1, child2],
        orphan_commit_ok=True,
        orphaned_task_ids=[child1_id, child2_id],
        deferred_parent_requeues=[],
        reaper_old_vms={},
        progress_cb=lambda: None,
        landed_cb=landed,
    )

    # Both tasks must be processed — the second one must NOT be skipped due to
    # expired-object access failure after the first task's internal rollback.
    both_ids = {child1_id, child2_id}
    assert set(call_task_ids) == both_ids, (
        f"Expected both {child1_id} and {child2_id} to be processed. "
        f"Got: {call_task_ids}. "
        "If only one task was processed, the second was likely blocked by "
        "MissingGreenlet (expired ORM object after internal rollback)."
    )
    assert len(call_task_ids) == 2, (
        f"Expected exactly 2 rollup calls (one per FAILED child), got {len(call_task_ids)}"
    )

    # SQLAlchemy's identity map may refresh the same Python object during the
    # re-query, so checking ``inspect(old).expired`` after the sweep is not a
    # reliable proof of re-batching. Instead, prove that an execute occurred
    # between the first rollup call's rollback signal and the second rollup call.
    assert len(execute_counts_at_rollup_calls) == 2
    assert execute_counts_at_rollup_calls[1] > execute_counts_at_rollup_calls[0], (
        "The rollup batch must be re-queried after a silent internal rollback"
    )


@pytest.mark.asyncio
async def test_expired_attribute_after_rollback_raises_under_fidelity_mode(
    sweep_db, mocker, expire_fidelity
):
    """Fidelity mode makes expired-attribute access raise on SQLite/aiosqlite,
    matching asyncpg production behaviour.

    Part A — calibration: verify that accessing a mapped attribute on an expired
    ORM object raises after session.rollback() when fidelity mode (real _expire)
    is active. Without fidelity mode the root conftest no-ops _expire and this
    bug class is invisible to SQLite tests.

    Part B — production exercise: run run_post_orphan_sweep with a
    silent-rollback scenario under fidelity mode. The production fixes (re-batch
    the rollup task dict, re-assign task from the fresh dict before landed_cb,
    and re-fetch post_reap_tasks after the rollup loop) ensure every attribute
    access downstream uses non-expired objects even when real expiry is active.
    """
    project = await _make_project(sweep_db)
    project_id = project.id  # snapshot raw UUID before rollback expiry

    # ── Part A: calibration ──────────────────────────────────────────────────
    canary = await _make_task(sweep_db, project_id, status=TaskStatus.FAILED)
    await sweep_db.rollback()  # with real _expire: marks canary expired

    with pytest.raises(Exception):
        # Accessing a mapped attribute on an expired object raises under fidelity
        # mode (aiosqlite MissingGreenlet, same as asyncpg in production).
        _ = canary.status  # noqa: F841

    # ── Part B: production code completes under fidelity mode ────────────────
    # Use project_id (raw UUID, captured before rollback) — no ORM attribute read.
    parent1 = await _make_task(sweep_db, project_id, status=TaskStatus.EVALUATING)
    parent2 = await _make_task(sweep_db, project_id, status=TaskStatus.EVALUATING)
    child1 = await _make_task(
        sweep_db, project_id, status=TaskStatus.FAILED, parent_task_id=parent1.id
    )
    child2 = await _make_task(
        sweep_db, project_id, status=TaskStatus.FAILED, parent_task_id=parent2.id
    )

    call_ids: list = []

    async def _silent_rollback_first(task, session, **kwargs):
        if not call_ids:
            # Snapshot id BEFORE rollback — task.id after rollback is expired
            # and raises MissingGreenlet under fidelity mode.
            _task_id = task.id
            with contextlib.suppress(Exception):
                await session.rollback()
            call_ids.append(_task_id)
            return True  # signal silent rollback so sweep re-batches
        call_ids.append(task.id)

    mocker.patch(
        "app.rollup_management._check_and_trigger_rollup",
        side_effect=_silent_rollback_first,
    )
    mocker.patch("app.reaper_post_orphan_sweep.AsyncSessionLocal", _noop_asl())

    # Snapshot IDs before the sweep: the mock calls session.rollback() which
    # expires parent1/parent2 (they are NOT in orphaned_task_ids so the sweep's
    # re-fetches won't refresh them). Accessing .id on an expired object raises
    # MissingGreenlet under fidelity mode.
    child1_id, child2_id = child1.id, child2.id
    parent1_id, parent2_id = parent1.id, parent2.id

    landed = AsyncMock()
    # Must NOT raise even under fidelity mode: the production code re-batches
    # and re-assigns task before landed_cb, then re-fetches post_reap_tasks
    # after the rollup loop so the safeguard and dead-PR loops get fresh objects.
    await run_post_orphan_sweep(
        sweep_db,
        orphaned_tasks=[child1, child2],
        orphan_commit_ok=True,
        orphaned_task_ids=[child1_id, child2_id],
        deferred_parent_requeues=[],
        reaper_old_vms={},
        progress_cb=lambda: None,
        landed_cb=landed,
    )

    assert set(call_ids) == {child1_id, child2_id}
    landed_parent_ids = {args[0] for args, _ in landed.await_args_list}
    assert parent1_id in landed_parent_ids
    assert parent2_id in landed_parent_ids
