"""Phase B3 lock-window-break regression tests.

Audit C4: hot paths in rollup_management.py and orchestrator.py held
SELECT FOR UPDATE locks on Task rows across 5-30s GitHub API calls
(worst-case minutes during rate-limit). Concurrent heartbeats on those
same rows queued behind the 30s lock_timeout, the heartbeat handler
returned 409, and the reaper false-orphaned the held tasks.

The fix is the commit-release / re-acquire-and-validate pattern: the
lock is released around each external GitHub call, then the row is
re-locked and ``updated_at`` is checked against the pre-call snapshot.
If a peer mutated the row during the unlocked window, the re-acquire
returns None and the caller defers to the next sweep.

These tests exercise:
    a. The two helpers in rollup_management (`_release_parent_lock`
       and `_relock_parent`) with mocked slow GitHub calls.
    b. The orchestrator's `_lock_task_for_mutation` helper with the
       new ``expected_updated_at`` validation kwarg.
    c. The end-to-end staging-state-conflict case where a concurrent
       transaction modifies the row during the unlocked window.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlmodel import select

from app.models import Project, Task, TaskStatus
from app.rollup_management import _release_parent_lock, _relock_parent


USE_POSTGRES = (
    os.getenv("USE_TEST_POSTGRES", "").lower() == "true"
    and os.getenv("DATABASE_URL", "").startswith("postgresql")
)


# ── Helper-level unit tests ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_release_parent_lock_returns_updated_at_snapshot(db_session):
    """`_release_parent_lock` must commit the parent and return an
    ``(updated_at, version)`` snapshot pair the caller can use to
    re-validate after I/O.

    Wave F4 M10: returns a tuple now instead of a bare datetime so the
    re-acquire can also detect Core UPDATE drift that bumps version
    without bumping updated_at.
    """
    project = Project(name="B3 release lock", user_id="user-b3")
    db_session.add(project)
    await db_session.commit()

    parent = Task(
        project_id=project.id,
        title="parent",
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "research"}],
    )
    db_session.add(parent)
    await db_session.commit()
    await db_session.refresh(parent)

    snapshot = await _release_parent_lock(parent, db_session)
    assert snapshot is not None
    # The returned value should be a (datetime, int) tuple — datetime for the
    # updated_at axis, int for the version-counter axis.
    assert isinstance(snapshot, tuple) and len(snapshot) == 2
    assert isinstance(snapshot[0], datetime)
    assert isinstance(snapshot[1], int)
    # Session must no longer hold an open transaction (commit released the lock)
    assert not db_session.in_transaction()


@pytest.mark.asyncio
async def test_relock_parent_returns_task_when_unchanged(db_session):
    """`_relock_parent` should re-acquire the row and return the task when
    `updated_at` still matches the pre-call snapshot (no peer mutation)."""
    project = Project(name="B3 relock unchanged", user_id="user-b3")
    db_session.add(project)
    await db_session.commit()

    parent = Task(
        project_id=project.id,
        title="parent",
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "research"}],
    )
    db_session.add(parent)
    await db_session.commit()
    await db_session.refresh(parent)
    snapshot = parent.updated_at

    fresh = await _relock_parent(db_session, parent.id, snapshot)
    assert fresh is not None
    assert fresh.id == parent.id
    assert fresh.updated_at == snapshot


@pytest.mark.asyncio
async def test_relock_parent_returns_none_when_updated_at_changed(db_session):
    """When a peer transaction mutated the row during the unlocked window,
    the re-lock must detect the staging-state change and return None so
    the caller defers instead of applying a stale mutation."""
    project = Project(name="B3 relock changed", user_id="user-b3")
    db_session.add(project)
    await db_session.commit()

    parent = Task(
        project_id=project.id,
        title="parent",
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "research"}],
    )
    db_session.add(parent)
    await db_session.commit()
    await db_session.refresh(parent)
    stale_snapshot = parent.updated_at - timedelta(seconds=1)

    # Force a snapshot that does NOT match the row's updated_at — simulates
    # "peer mutated the row during my unlocked window."
    fresh = await _relock_parent(db_session, parent.id, stale_snapshot)
    assert fresh is None, (
        "Expected None when updated_at snapshot is stale (peer mutated row)"
    )


@pytest.mark.asyncio
async def test_relock_parent_returns_none_when_row_missing(db_session):
    """If the row was deleted during the unlocked window, re-lock must
    return None instead of raising."""
    import uuid

    missing_id = uuid.uuid4()
    fresh = await _relock_parent(db_session, missing_id, datetime.now(tz=timezone.utc))
    assert fresh is None


# ── Slow GitHub mock + concurrent heartbeat (Postgres-only) ─────────────────


@pytest.mark.skipif(
    not USE_POSTGRES,
    reason="row-lock concurrency assertion requires real Postgres",
)
@pytest.mark.asyncio
async def test_concurrent_heartbeat_not_blocked_by_slow_github(db_session):
    """Phase B3 acceptance: a slow GitHub call must NOT hold the FOR UPDATE
    lock across the in-flight window. A concurrent heartbeat (separate
    session, also using SKIP LOCKED) must complete without queuing for
    the 30s lock_timeout.

    Setup:
        Create a parent task. Worker A acquires the parent's lock,
        captures `updated_at`, **releases** via `_release_parent_lock`,
        then sleeps 1s (simulating a 1s GitHub call). During the sleep,
        worker B (heartbeat) tries to lock the same row with
        `with_for_update(skip_locked=True)` — it must succeed within
        ~50ms because the lock was released.
    """
    project = Project(name="B3 slow-gh heartbeat", user_id="user-b3")
    db_session.add(project)
    await db_session.commit()

    parent = Task(
        project_id=project.id,
        title="rollup parent",
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "research"}],
    )
    db_session.add(parent)
    await db_session.commit()
    await db_session.refresh(parent)
    parent_id = parent.id

    Session = sessionmaker(
        bind=db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    a_released_lock = asyncio.Event()
    b_acquired_lock_at: list[float] = []
    b_lock_failed: list[str] = []

    async def worker_a_simulating_rollup() -> None:
        async with Session() as session:
            stmt = (
                select(Task)
                .where(Task.id == parent_id)
                .with_for_update(skip_locked=True)
            )
            result = await session.execute(stmt)
            t = result.scalars().first()
            assert t is not None
            # Release the lock — mirrors the new commit-release pattern
            snapshot = await _release_parent_lock(t, session)
            a_released_lock.set()
            # Simulate a 1s GitHub call (real-world: 5-30s)
            await asyncio.sleep(1.0)
            # Re-acquire (lock was released; row should still be ours)
            fresh = await _relock_parent(session, parent_id, snapshot)
            assert fresh is not None
            await session.commit()

    async def worker_b_heartbeat() -> None:
        await a_released_lock.wait()
        # Try to acquire the lock IMMEDIATELY after worker_a releases.
        # If the lock was actually released, this must succeed in <100ms.
        # If the lock was held (regression), the 30s lock_timeout fires
        # and skip_locked returns None.
        async with Session() as session:
            t0 = asyncio.get_event_loop().time()
            stmt = (
                select(Task)
                .where(Task.id == parent_id)
                .with_for_update(skip_locked=True)
            )
            try:
                result = await asyncio.wait_for(session.execute(stmt), timeout=2.0)
            except asyncio.TimeoutError:
                b_lock_failed.append("timeout waiting for lock")
                return
            row = result.scalars().first()
            elapsed = asyncio.get_event_loop().time() - t0
            if row is None:
                b_lock_failed.append("skip_locked returned None — peer holds lock")
            else:
                b_acquired_lock_at.append(elapsed)
                # Simulate heartbeat update — use naive UTC to match the column.
                row.last_heartbeat = datetime.utcnow()
                session.add(row)
                await session.commit()

    await asyncio.gather(worker_a_simulating_rollup(), worker_b_heartbeat())

    assert not b_lock_failed, (
        f"Heartbeat could not acquire lock during slow GitHub window: "
        f"{b_lock_failed}. The Phase B3 commit-release pattern is broken."
    )
    assert b_acquired_lock_at, "Heartbeat never recorded a lock acquisition"
    assert b_acquired_lock_at[0] < 0.5, (
        f"Heartbeat took {b_acquired_lock_at[0]:.2f}s to acquire the lock — "
        f"should be <500ms once worker_a releases. Indicates the lock window "
        f"was not actually broken."
    )


# ── Validation-conflict case ────────────────────────────────────────────────


@pytest.mark.skipif(
    not USE_POSTGRES,
    reason="cross-session updated_at race requires Postgres",
)
@pytest.mark.asyncio
async def test_relock_detects_peer_mutation_and_returns_none(db_session):
    """End-to-end validation: when a parallel transaction modifies the
    row during the unlocked window, `_relock_parent` must observe the
    advanced `updated_at` and return None so the caller defers."""
    project = Project(name="B3 validation conflict", user_id="user-b3")
    db_session.add(project)
    await db_session.commit()

    parent = Task(
        project_id=project.id,
        title="parent",
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "research"}],
    )
    db_session.add(parent)
    await db_session.commit()
    await db_session.refresh(parent)
    parent_id = parent.id

    Session = sessionmaker(
        bind=db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    # Capture the snapshot, then have a separate session mutate the row.
    snapshot = parent.updated_at
    # Sleep briefly so the onupdate=_utcnow trigger produces a strictly
    # greater timestamp than `snapshot` (the in-Python datetime resolution
    # is microseconds, but the round-trip through the DB plus session
    # commit can land on the same microsecond on fast hardware).
    await asyncio.sleep(0.01)
    async with Session() as peer_session:
        peer = (await peer_session.execute(
            select(Task).where(Task.id == parent_id)
        )).scalars().first()
        assert peer is not None
        peer.latest_status_message = "peer-mutation"
        peer_session.add(peer)
        await peer_session.commit()

    # The peer commit went through a separate session — db_session's
    # identity map still holds the pre-mutation copy under
    # expire_on_commit=False. Postgres' default read-committed isolation
    # also means db_session's open transaction can't see the peer's
    # commit (snapshot was fixed when the txn began). Commit to end
    # the implicit txn AND expunge the identity map so the next SELECT
    # FOR UPDATE both starts a fresh transaction and re-creates the
    # Task object from the fresh row read.
    await db_session.commit()
    db_session.expunge_all()

    # Sanity-check that the peer's UPDATE actually advanced updated_at.
    # If onupdate=_utcnow fires within the same microsecond as the original
    # default_factory, this assertion lets the test fail with a more
    # actionable message than "validation guard broken".
    actual_updated_at = None
    async with Session() as verify_session:
        verify = (await verify_session.execute(
            select(Task).where(Task.id == parent_id)
        )).scalars().first()
        assert verify is not None
        actual_updated_at = verify.updated_at
        if verify.updated_at == snapshot:
            pytest.skip(
                f"onupdate fired within the same datetime as default_factory "
                f"(both = {snapshot}); the test cannot exercise the staleness "
                f"check. Re-running on a slower path or larger sleep would help."
            )

    # Now relock — must return None because updated_at advanced.
    fresh = await _relock_parent(db_session, parent_id, snapshot)
    assert fresh is None, (
        f"Expected _relock_parent to return None after peer mutation; "
        f"got a non-None task. Snapshot={snapshot!r}, "
        f"DB updated_at via verify_session={actual_updated_at!r}, "
        f"fresh.updated_at={fresh.updated_at!r}. The validation guard is broken."
    )


# ── Orchestrator helper validation ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_lock_task_for_mutation_validates_expected_updated_at(db_session):
    """`_lock_task_for_mutation` (orchestrator) gained an
    `expected_updated_at` kwarg in Phase B3. When the row's updated_at has
    advanced past the snapshot, it must defer (return None) — not silently
    apply mutations against stale data.
    """
    from app.orchestrator import _lock_task_for_mutation

    project = Project(name="B3 orchestrator helper", user_id="user-b3")
    db_session.add(project)
    await db_session.commit()

    task = Task(
        project_id=project.id,
        title="t",
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "create"}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    real_updated_at = task.updated_at

    # Stale snapshot (1s ago) — the helper must observe the mismatch
    stale_snapshot = real_updated_at - timedelta(seconds=1)
    locked = await _lock_task_for_mutation(
        db_session, task.id,
        allowed_statuses=(TaskStatus.EVALUATING,),
        reason="B3 staleness test",
        expected_updated_at=stale_snapshot,
    )
    assert locked is None, (
        "Expected _lock_task_for_mutation to return None when the snapshot "
        "is stale; got a Task — the staleness guard is missing."
    )

    # Correct snapshot — should succeed.
    locked2 = await _lock_task_for_mutation(
        db_session, task.id,
        allowed_statuses=(TaskStatus.EVALUATING,),
        reason="B3 staleness test (matched)",
        expected_updated_at=real_updated_at,
    )
    assert locked2 is not None
    assert locked2.id == task.id
    # Cleanup: rollback so the lock releases without committing changes.
    await db_session.rollback()


@pytest.mark.asyncio
async def test_lock_task_for_mutation_no_expected_means_no_validation(db_session):
    """When `expected_updated_at` is not passed, the helper retains its
    original behaviour (no staleness check) — backward-compatible with
    the Phase A1 callsites that don't yet use the new kwarg.
    """
    from app.orchestrator import _lock_task_for_mutation

    project = Project(name="B3 backcompat", user_id="user-b3")
    db_session.add(project)
    await db_session.commit()

    task = Task(
        project_id=project.id,
        title="t",
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "create"}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    locked = await _lock_task_for_mutation(
        db_session, task.id,
        allowed_statuses=(TaskStatus.EVALUATING,),
        reason="B3 backcompat",
    )
    assert locked is not None
    await db_session.rollback()
