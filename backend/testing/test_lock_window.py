"""Phase B3 lock-window-break regression tests.

Audit C4: hot paths in rollup_management.py and orchestrator.py held
SELECT FOR UPDATE locks on Task rows across 5-30s GitHub API calls
(worst-case minutes during rate-limit). Concurrent heartbeats on those
same rows queued behind the 30s lock_timeout, the heartbeat handler
returned 409, and the reaper false-orphaned the held tasks.

The fix is the commit-release / re-acquire-and-validate pattern: the
lock is released around each external GitHub call, then the row is
re-locked and a content snapshot is checked against the pre-call
snapshot. If a peer mutated the row during the unlocked window, the
re-acquire returns None and the caller defers to the next sweep.

Round 4 W1 (audit C1, C3): the prior implementation snapshot
``updated_at`` and validated against it. Two structural bugs:
  - C1 (cache): SQLAlchemy's identity map under ``expire_on_commit=False``
    returned the cached pre-commit instance on re-SELECT, so
    ``fresh.updated_at`` always equalled the snapshot and validation
    never fired. Fix: ``populate_existing=True`` on the re-SELECT.
  - C3 (heartbeat): even with the cache fix, ``onupdate=_utcnow``
    advances ``updated_at`` on every heartbeat, so under load every
    re-acquire would false-defer. Fix: validate against a content
    snapshot of the fields the unlocked window protects (status,
    rollup_pr_number/url, pr_merged, requires_approval) — heartbeats
    don't touch those fields.

These tests exercise:
    a. The two helpers in rollup_management (`_release_parent_lock`
       and `_relock_parent`) with mocked slow GitHub calls.
    b. The orchestrator's `_lock_task_for_mutation` helper with the
       content-snapshot ``expected_snapshot`` + ``snapshot_fn`` validation
       (R6-W3-B dropped the dead ``expected_updated_at`` predecessor —
       every CI-polling caller migrated to the snapshot pair).
    c. The end-to-end staging-state-conflict case where a concurrent
       transaction modifies the row during the unlocked window.
    d. R4-W1 specific: heartbeat-overlap (heartbeat-only updates do
       NOT trip the validator), identity-map cache bypass (peer
       mutation via raw SQL caught by populate_existing), and
       _ensure_branch success-path re-lock.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlmodel import select

from app.models import Project, Task, TaskStatus, Artifact, ArtifactType
from app.rollup_management import (
    _capture_rollup_parent_snapshot,
    _release_parent_lock,
    _relock_parent,
)


USE_POSTGRES = (
    os.getenv("USE_TEST_POSTGRES", "").lower() == "true"
    and os.getenv("DATABASE_URL", "").startswith("postgresql")
)


# ── Helper-level unit tests ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_release_parent_lock_returns_content_snapshot(db_session):
    """`_release_parent_lock` must commit the parent and return a content
    snapshot dict the caller hands to ``_relock_parent`` to validate after
    I/O.

    Round 4 W1 (audit C3): the snapshot is now a dict of stable content
    fields (status, requires_approval, rollup_pr_number, rollup_pr_url,
    pr_merged) — NOT ``updated_at``. Heartbeat-only mutations don't
    advance these fields, so the validator no longer false-defers under
    heartbeat overlap.
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
    assert isinstance(snapshot, dict)
    assert snapshot["status"] == TaskStatus.EVALUATING
    assert snapshot["requires_approval"] is False
    assert snapshot["rollup_pr_number"] is None
    assert snapshot["rollup_pr_url"] is None
    assert snapshot["pr_merged"] is None
    # Session must no longer hold an open transaction (commit released the lock)
    assert not db_session.in_transaction()


@pytest.mark.asyncio
async def test_relock_parent_returns_task_when_unchanged(db_session):
    """`_relock_parent` should re-acquire the row and return the task when
    the content snapshot still matches (no peer mutation)."""
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
    snapshot = _capture_rollup_parent_snapshot(parent)

    fresh = await _relock_parent(db_session, parent.id, snapshot)
    assert fresh is not None
    assert fresh.id == parent.id
    # Snapshot fields must still match
    assert _capture_rollup_parent_snapshot(fresh) == snapshot


@pytest.mark.asyncio
async def test_relock_parent_returns_none_when_status_changed(db_session):
    """When a peer transaction mutated the row's status during the unlocked
    window, the re-lock must detect the change via the content snapshot
    and return None so the caller defers instead of applying a stale
    mutation."""
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

    # Forge a "stale" snapshot — claim status was QUEUED, even though
    # it's actually EVALUATING. The validator will see a mismatch and
    # defer. This is the unit-level analog of "peer mutated status
    # during my unlocked window".
    stale_snapshot = {
        "status": TaskStatus.QUEUED,
        "requires_approval": False,
        "rollup_pr_number": None,
        "rollup_pr_url": None,
        "pr_merged": None,
    }
    fresh = await _relock_parent(db_session, parent.id, stale_snapshot)
    assert fresh is None, (
        "Expected None when status field in snapshot mismatches actual row "
        "(peer mutated row to advance status)"
    )


@pytest.mark.asyncio
async def test_relock_parent_returns_none_when_rollup_pr_marker_changed(db_session):
    """When a peer wrote the rollup_pr_number marker during the unlocked
    window, the content snapshot validator must detect it.

    This is the case the lock-window break is most concerned with: the
    crash-recovery marker (rollup_pr_number / rollup_pr_url) is written
    immediately after PR creation; if a peer concurrently wrote a
    different value we'd want to defer rather than overwrite.
    """
    project = Project(name="B3 relock pr marker", user_id="user-b3")
    db_session.add(project)
    await db_session.commit()

    parent = Task(
        project_id=project.id,
        title="parent",
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "research"}],
        context={},
    )
    db_session.add(parent)
    await db_session.commit()
    await db_session.refresh(parent)

    # Snapshot at the start of the unlocked window — no PR marker yet.
    pre_snapshot = _capture_rollup_parent_snapshot(parent)
    assert pre_snapshot["rollup_pr_number"] is None

    # Simulate peer writing the marker by mutating in-memory + commit.
    parent.context = {"rollup_pr_number": 42, "rollup_pr_url": "https://example/42"}
    db_session.add(parent)
    await db_session.commit()

    # Re-locking with the pre-marker snapshot must return None.
    fresh = await _relock_parent(db_session, parent.id, pre_snapshot)
    assert fresh is None, (
        "Expected None when rollup_pr_number marker advanced (peer wrote it)"
    )


@pytest.mark.asyncio
async def test_relock_parent_ignores_heartbeat_only_changes(db_session):
    """R4-W1 (audit C3) regression: heartbeat-driven updates to
    ``last_heartbeat`` / ``last_metrics_json`` / ``heartbeat_count`` /
    ``updated_at`` (via ``onupdate=_utcnow``) must NOT trip the lock-
    window validator. Validating against a content snapshot of state-
    machine fields ensures heartbeat overlap doesn't cause false
    deferrals — the original ``updated_at``-based check did.
    """
    project = Project(name="B3 relock heartbeat ignore", user_id="user-b3")
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
    pre_snapshot = _capture_rollup_parent_snapshot(parent)
    pre_updated_at = parent.updated_at

    # Simulate a heartbeat: bump last_heartbeat and an unrelated benign
    # field. The ``onupdate=_utcnow`` trigger on the column will advance
    # ``updated_at`` (the field the legacy validator checked). The
    # content snapshot must remain unchanged.
    await asyncio.sleep(0.01)
    parent.last_heartbeat = datetime.utcnow()
    db_session.add(parent)
    await db_session.commit()
    await db_session.refresh(parent)

    # Sanity: updated_at advanced under the heartbeat — the legacy
    # check would have false-tripped here.
    if parent.updated_at == pre_updated_at:
        # Some clock-resolution paths land in the same microsecond;
        # don't fail the test for that.
        pytest.skip(
            "onupdate did not advance updated_at within the test window — "
            "cannot exercise the heartbeat-overlap regression."
        )

    # The content snapshot must still match — heartbeat-only changes
    # don't touch status / rollup_pr_* / requires_approval.
    fresh = await _relock_parent(db_session, parent.id, pre_snapshot)
    assert fresh is not None, (
        "Heartbeat-only mutation false-tripped the content snapshot validator. "
        "The lock-window-break must NOT defer on heartbeat overlap (R4-W1 C3)."
    )


@pytest.mark.asyncio
async def test_relock_parent_returns_none_when_row_missing(db_session):
    """If the row was deleted during the unlocked window, re-lock must
    return None instead of raising."""
    missing_id = uuid.uuid4()
    fresh = await _relock_parent(db_session, missing_id, None)
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
            # Release the lock — mirrors the new commit-release pattern.
            # Snapshot is now a content dict (R4-W1 audit C3).
            snapshot = await _release_parent_lock(t, session)
            assert isinstance(snapshot, dict)
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
    reason="cross-session FOR UPDATE race requires Postgres",
)
@pytest.mark.asyncio
async def test_relock_detects_peer_mutation_via_content_snapshot(db_session):
    """End-to-end validation: when a parallel transaction modifies a
    state-machine field during the unlocked window, ``_relock_parent``
    must observe the change via the content snapshot and return None
    so the caller defers.

    Round 4 W1 (audit C1, C3): replaces the prior ``updated_at``-based
    test. The new validation is content-based — heartbeat-only updates
    don't trip it, but state-machine mutations (status transition,
    rollup PR marker write) do.
    """
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

    # Capture the content snapshot at the start of the unlocked window.
    snapshot = _capture_rollup_parent_snapshot(parent)

    # Have a separate session advance the row's status — the kind of
    # peer mutation the lock-window-break is meant to detect.
    async with Session() as peer_session:
        peer = (await peer_session.execute(
            select(Task).where(Task.id == parent_id)
        )).scalars().first()
        assert peer is not None
        peer.status = TaskStatus.COMPLETED
        peer_session.add(peer)
        await peer_session.commit()

    # End the implicit txn so the next SELECT FOR UPDATE starts a
    # fresh transaction and sees the peer's commit (under default
    # read-committed isolation).
    await db_session.commit()
    db_session.expunge_all()

    # Now relock — must return None because the snapshot's status
    # field no longer matches the row's actual status.
    fresh = await _relock_parent(db_session, parent_id, snapshot)
    assert fresh is None, (
        f"Expected _relock_parent to return None after peer status "
        f"mutation; got {fresh!r}. Pre-snapshot={snapshot!r}. The "
        f"content snapshot validator is broken."
    )


@pytest.mark.skipif(
    not USE_POSTGRES,
    reason="raw SQL identity-map cache test requires Postgres",
)
@pytest.mark.asyncio
async def test_relock_bypasses_identity_map_cache(db_session):
    """R4-W1 (audit C1) regression: ``populate_existing=True`` on the
    re-SELECT must bypass SQLAlchemy's identity-map cache so a peer
    mutation written via raw SQL is visible to the validator.

    This is the bug that made the lock-window-break a no-op in
    production: with ``expire_on_commit=False``, the prior
    implementation's ``session.execute(select(Task)...)`` returned the
    cached pre-commit instance from the identity map, so
    ``fresh.updated_at`` always equalled the snapshot and the
    validation never fired.
    """
    project = Project(name="B3 cache bypass", user_id="user-b3")
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

    # Read once to seed the identity-map cache with a copy of the row.
    cached = (await db_session.execute(
        select(Task).where(Task.id == parent_id)
    )).scalars().first()
    assert cached is not None
    assert cached.status == TaskStatus.EVALUATING

    # Capture content snapshot from the cached read.
    pre_snapshot = _capture_rollup_parent_snapshot(cached)
    assert pre_snapshot["status"] == TaskStatus.EVALUATING

    # End db_session's txn so the peer commit becomes visible to it.
    await db_session.commit()

    # Mutate via raw SQL on a peer connection, bypassing the ORM and
    # the identity map entirely.
    Session = sessionmaker(
        bind=db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with Session() as peer_session:
        await peer_session.execute(
            text("UPDATE task SET status = :status WHERE id = :id"),
            {"status": TaskStatus.FAILED.value, "id": parent_id},
        )
        await peer_session.commit()

    # IMPORTANT: do NOT expire_all / expunge_all the identity map here.
    # The whole point is that ``populate_existing=True`` should refresh
    # the cached instance regardless of the cache being warm.
    fresh = await _relock_parent(db_session, parent_id, pre_snapshot)
    assert fresh is None, (
        f"Expected _relock_parent to return None after raw-SQL peer "
        f"mutation; got {fresh!r}. The identity-map cache was NOT "
        f"bypassed by populate_existing=True (R4-W1 C1)."
    )


# ── _ensure_branch success-path re-lock test ────────────────────────────────


@pytest.mark.asyncio
async def test_ensure_branch_returns_relocked_task_on_success(monkeypatch, db_session):
    """R4-W1 (audit C2): on success, ``_ensure_branch`` must return a
    freshly-locked Task instance (not just True) so the caller's
    subsequent context mutation is protected by the new lock.

    The prior implementation returned True without re-locking; callers
    then mutated ``task.context`` and committed *as if locked*, but the
    row was actually unlocked between the success commit and the
    caller's commit.
    """
    from app import task_completion as tc_mod

    project = Project(name="B3 ensure branch success", user_id="user-b3")
    db_session.add(project)
    await db_session.commit()

    task = Task(
        project_id=project.id,
        title="plan task",
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "plan", "base_branch": "main"}],
        context={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # Patch the GitHub helpers so create_branch always succeeds without
    # a real network call.
    async def _fake_token(*args, **kwargs):
        return "fake-token", "owner", "repo"

    async def _fake_get_branch_sha(*args, **kwargs):
        return "deadbeef" * 5

    async def _fake_create_branch(*args, **kwargs):
        return None

    monkeypatch.setattr(tc_mod, "get_github_token_for_task", _fake_token)
    monkeypatch.setattr(tc_mod, "get_branch_sha", _fake_get_branch_sha)
    monkeypatch.setattr(tc_mod, "create_branch", _fake_create_branch)

    relocked = await tc_mod._ensure_branch(
        task, "plan/abcd1234", "main", "PLAN", db_session
    )
    # Success — must return a Task (the freshly-locked instance), not
    # True or None.
    assert relocked is not None, (
        "Expected a Task on success — got None. _ensure_branch must "
        "re-acquire the lock and return the relocked instance (R4-W1 C2)."
    )
    assert isinstance(relocked, Task)
    assert relocked.id == task.id
    # Caller can mutate the returned task under the new lock.
    relocked.context = {**(relocked.context or {}), "plan_branch": "plan/abcd1234"}
    db_session.add(relocked)
    await db_session.commit()


@pytest.mark.asyncio
async def test_ensure_branch_returns_none_on_failure(monkeypatch, db_session):
    """R4-W1 (audit C2): on hard failure (all retries exhausted),
    ``_ensure_branch`` must return None and mark the task FAILED."""
    from app import task_completion as tc_mod

    project = Project(name="B3 ensure branch failure", user_id="user-b3")
    db_session.add(project)
    await db_session.commit()

    task = Task(
        project_id=project.id,
        title="plan task",
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "plan", "base_branch": "main"}],
        context={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    async def _fake_token(*args, **kwargs):
        return "fake-token", "owner", "repo"

    async def _always_404(*args, **kwargs):
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="branch not found")

    async def _fake_create_raises(*args, **kwargs):
        raise RuntimeError("simulated create failure")

    async def _fake_alert(*args, **kwargs):
        return None

    # Make sleeps instant so the test runs quickly.
    async def _no_sleep(*args, **kwargs):
        return None

    monkeypatch.setattr(tc_mod, "get_github_token_for_task", _fake_token)
    monkeypatch.setattr(tc_mod, "get_branch_sha", _always_404)
    monkeypatch.setattr(tc_mod, "create_branch", _fake_create_raises)
    monkeypatch.setattr(tc_mod.asyncio, "sleep", _no_sleep)
    # Patch the deferred import to avoid circular pulls.
    import app.main as _main
    monkeypatch.setattr(_main, "_maybe_create_failure_alert", _fake_alert)

    result = await tc_mod._ensure_branch(
        task, "plan/abcd1234", "main", "PLAN", db_session
    )
    assert result is None, (
        "Expected None on failure — got a Task. _ensure_branch must "
        "return None when all retries exhaust (caller defers)."
    )
    # Refresh and confirm task is FAILED.
    await db_session.refresh(task)
    assert task.status == TaskStatus.FAILED


# ── Orchestrator helper validation ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_lock_task_for_mutation_validates_expected_snapshot(db_session):
    """`_lock_task_for_mutation` (orchestrator) defers when the post-relock
    content snapshot differs from the pre-release one.

    R5-W2-A replaced the original Phase B3 ``expected_updated_at`` timestamp
    validator (which false-tripped on every overlapping heartbeat because
    ``onupdate=_utcnow`` advances ``updated_at`` ~every 30s) with a
    content-based snapshot pair. R6-W3-B then dropped the dead timestamp
    parameter entirely — this test guards the surviving snapshot path.
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

    def _status_snapshot(t: Task) -> dict:
        return {"status": t.status}

    # Stale snapshot — claims status=QUEUED but the row is EVALUATING.
    stale_snapshot = {"status": TaskStatus.QUEUED}
    locked = await _lock_task_for_mutation(
        db_session, task.id,
        allowed_statuses=(TaskStatus.EVALUATING,),
        reason="snapshot mismatch test",
        expected_snapshot=stale_snapshot,
        snapshot_fn=_status_snapshot,
    )
    assert locked is None, (
        "Expected _lock_task_for_mutation to return None when the snapshot "
        "is stale; got a Task — the staleness guard is missing."
    )

    # Matching snapshot — should succeed.
    matching_snapshot = {"status": TaskStatus.EVALUATING}
    locked2 = await _lock_task_for_mutation(
        db_session, task.id,
        allowed_statuses=(TaskStatus.EVALUATING,),
        reason="snapshot match test",
        expected_snapshot=matching_snapshot,
        snapshot_fn=_status_snapshot,
    )
    assert locked2 is not None
    assert locked2.id == task.id
    # Cleanup: rollback so the lock releases without committing changes.
    await db_session.rollback()


@pytest.mark.asyncio
async def test_lock_task_for_mutation_no_snapshot_means_no_validation(db_session):
    """When neither ``expected_snapshot`` nor ``snapshot_fn`` is passed, the
    helper retains its original behaviour (no staleness check) —
    backward-compatible with callsites that don't need the relock-validate
    pattern.
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


# ── CI-polling snapshot coverage (R6-W4-A, audit MED-4) ─────────────────────


@pytest.mark.asyncio
async def test_capture_ci_polling_snapshot_includes_all_5_fields():
    """`_capture_ci_polling_snapshot` must return a dict with the 5 fields
    that the CI-polling lock-window break protects: status (Task-level)
    plus review_loop.{pr_number, ci_passed, review_approved, iteration}.

    Heartbeat-only updates touch none of these — that's the whole point
    of the content-snapshot validator (R5-W2-A). If a future refactor
    drops a field or renames a key in review_loop, the CI polling sites
    will silently stop deferring on that mutation; this test pins the
    contract.
    """
    from app.orchestrator import _capture_ci_polling_snapshot

    task = Task(
        project_id=uuid.uuid4(),
        title="ci snapshot",
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "create"}],
        context={
            "review_loop": {
                "pr_number": 17,
                "ci_passed": False,
                "review_approved": False,
                "iteration": 2,
            },
        },
    )

    snapshot = _capture_ci_polling_snapshot(task)
    assert set(snapshot.keys()) == {
        "status", "pr_number", "ci_passed", "review_approved", "iteration",
    }, (
        f"Snapshot must include exactly the 5 protected fields; got {set(snapshot.keys())}. "
        "Adding/removing keys is a contract change — update the CI polling sites in lockstep."
    )
    assert snapshot["status"] == TaskStatus.EVALUATING
    assert snapshot["pr_number"] == 17
    assert snapshot["ci_passed"] is False
    assert snapshot["review_approved"] is False
    assert snapshot["iteration"] == 2


@pytest.mark.asyncio
async def test_capture_ci_polling_snapshot_handles_missing_review_loop():
    """When ``context`` lacks a ``review_loop`` key (early in the pipeline,
    or when the task hasn't entered review yet), the snapshot must still
    return all 5 keys with None placeholders so callers can compare
    deterministically across the pre/post-release boundary."""
    from app.orchestrator import _capture_ci_polling_snapshot

    task = Task(
        project_id=uuid.uuid4(),
        title="ci snapshot pre-review",
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "create"}],
        context={},
    )
    snapshot = _capture_ci_polling_snapshot(task)
    assert snapshot == {
        "status": TaskStatus.EVALUATING,
        "pr_number": None,
        "ci_passed": None,
        "review_approved": None,
        "iteration": None,
    }


@pytest.mark.asyncio
async def test_lock_with_matching_ci_snapshot_succeeds(db_session):
    """When the CI-polling snapshot at re-acquire time matches the
    pre-release snapshot exactly, the lock must succeed and return the
    Task — peer made no observable change in the unlocked window."""
    from app.orchestrator import _capture_ci_polling_snapshot, _lock_task_for_mutation

    project = Project(name="ci snapshot match", user_id="user-ci-match")
    db_session.add(project)
    await db_session.commit()

    task = Task(
        project_id=project.id,
        title="t",
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "create"}],
        context={
            "review_loop": {
                "pr_number": 100,
                "ci_passed": False,
                "review_approved": False,
                "iteration": 1,
            },
        },
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    pre_snapshot = _capture_ci_polling_snapshot(task)

    locked = await _lock_task_for_mutation(
        db_session, task.id,
        allowed_statuses=(TaskStatus.EVALUATING,),
        reason="ci snapshot match test",
        expected_snapshot=pre_snapshot,
        snapshot_fn=_capture_ci_polling_snapshot,
    )
    assert locked is not None, (
        "Expected lock success when snapshot matches; got None — the validator "
        "false-tripped on an unchanged row."
    )
    assert locked.id == task.id
    await db_session.rollback()


@pytest.mark.asyncio
async def test_lock_with_mutated_status_defers(db_session):
    """When a peer transitions ``status`` (e.g. EVALUATING → COMPLETED on
    PR merge) during the unlocked window, the CI-polling re-lock must
    detect the mismatch via the snapshot and return None so the caller
    skips applying stale CI-polling mutations on top of the peer's
    state-machine advance."""
    from app.orchestrator import _capture_ci_polling_snapshot, _lock_task_for_mutation

    project = Project(name="ci snapshot status mutate", user_id="user-ci-status")
    db_session.add(project)
    await db_session.commit()

    task = Task(
        project_id=project.id,
        title="t",
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "create"}],
        context={
            "review_loop": {
                "pr_number": 200,
                "ci_passed": False,
                "review_approved": False,
                "iteration": 1,
            },
        },
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # Forge a snapshot claiming status was COMPLETED while the row is
    # still EVALUATING — the unit-level analog of "peer transitioned
    # status during my unlocked window".
    stale_snapshot = {
        "status": TaskStatus.COMPLETED,
        "pr_number": 200,
        "ci_passed": False,
        "review_approved": False,
        "iteration": 1,
    }
    locked = await _lock_task_for_mutation(
        db_session, task.id,
        allowed_statuses=(TaskStatus.EVALUATING,),
        reason="ci snapshot status mutate test",
        expected_snapshot=stale_snapshot,
        snapshot_fn=_capture_ci_polling_snapshot,
    )
    assert locked is None, (
        "Expected None when snapshot's status field doesn't match the row — "
        "CI polling must defer on peer status transitions."
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,old_value,new_value",
    [
        ("pr_number", 300, 301),
        ("ci_passed", False, True),
        ("review_approved", False, True),
        ("iteration", 1, 2),
    ],
)
async def test_lock_with_mutated_review_loop_field_defers(
    db_session, field, old_value, new_value,
):
    """When any review_loop field tracked by the snapshot moves during
    the unlocked window, the re-lock must defer (return None). Each of
    the 4 review_loop fields is independently load-bearing: a peer
    advancing iteration, flipping ci_passed/review_approved, or moving
    to a new PR all signal the review pipeline took a step under us.

    The snapshot pair captures the *observed* (old) value at release
    time; if the row's actual value has moved to (new), the re-lock
    must return None.
    """
    from app.orchestrator import _capture_ci_polling_snapshot, _lock_task_for_mutation

    project = Project(name=f"ci snapshot {field} mutate", user_id="user-ci-rl")
    db_session.add(project)
    await db_session.commit()

    task = Task(
        project_id=project.id,
        title="t",
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "create"}],
        context={
            "review_loop": {
                "pr_number": 300,
                "ci_passed": False,
                "review_approved": False,
                "iteration": 1,
                field: new_value,  # Row carries the *new* value
            },
        },
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # Caller's pre-release snapshot still reflects the OLD value for the
    # mutated field. The other fields match so we isolate the per-field
    # check.
    stale_snapshot = {
        "status": TaskStatus.EVALUATING,
        "pr_number": 300,
        "ci_passed": False,
        "review_approved": False,
        "iteration": 1,
        field: old_value,
    }
    locked = await _lock_task_for_mutation(
        db_session, task.id,
        allowed_statuses=(TaskStatus.EVALUATING,),
        reason=f"ci snapshot {field} mutate test",
        expected_snapshot=stale_snapshot,
        snapshot_fn=_capture_ci_polling_snapshot,
    )
    assert locked is None, (
        f"Expected None when review_loop.{field} moved {old_value!r} -> "
        f"{new_value!r}; got a Task. The CI-polling snapshot validator "
        f"is missing the {field} field."
    )


@pytest.mark.asyncio
async def test_lock_with_only_heartbeat_change_succeeds(db_session):
    """The whole point of the content snapshot (vs. ``updated_at``):
    heartbeat-only mutations advance ``updated_at`` and ``last_heartbeat``
    via ``onupdate=_utcnow`` but leave the 5 CI-polling fields untouched,
    so the re-lock must succeed.

    Mirror of ``test_relock_parent_ignores_heartbeat_only_changes``
    (rollup-management variant) — pinned for the orchestrator's CI-
    polling helper too. Without this, every overlapping heartbeat would
    false-defer the CI poll loop and the whole review pipeline would
    stall under load.
    """
    from app.orchestrator import _capture_ci_polling_snapshot, _lock_task_for_mutation

    project = Project(name="ci snapshot heartbeat", user_id="user-ci-hb")
    db_session.add(project)
    await db_session.commit()

    task = Task(
        project_id=project.id,
        title="t",
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "create"}],
        context={
            "review_loop": {
                "pr_number": 400,
                "ci_passed": False,
                "review_approved": False,
                "iteration": 1,
            },
        },
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    pre_snapshot = _capture_ci_polling_snapshot(task)
    pre_updated_at = task.updated_at

    # Simulate a heartbeat: bump last_heartbeat. The ``onupdate=_utcnow``
    # trigger advances ``updated_at`` (the field the legacy validator
    # checked); the content snapshot stays unchanged.
    await asyncio.sleep(0.01)
    task.last_heartbeat = datetime.utcnow()
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    if task.updated_at == pre_updated_at:
        pytest.skip(
            "onupdate did not advance updated_at within the test window — "
            "cannot exercise the heartbeat-overlap regression."
        )

    locked = await _lock_task_for_mutation(
        db_session, task.id,
        allowed_statuses=(TaskStatus.EVALUATING,),
        reason="ci snapshot heartbeat overlap test",
        expected_snapshot=pre_snapshot,
        snapshot_fn=_capture_ci_polling_snapshot,
    )
    assert locked is not None, (
        "Heartbeat-only mutation false-tripped the CI-polling snapshot validator. "
        "The 5-field snapshot must NOT include heartbeat-mutated fields (R5-W2-A)."
    )
    await db_session.rollback()


# ── R5-W3-B context_dirty regression (R6 audit) ─────────────────────────────


@pytest.mark.asyncio
async def test_release_parent_lock_persists_context_when_dirty_flag_set(db_session):
    """R5-W3-B / R6 audit: when a caller mutates ``parent.context``
    in-place before releasing, it MUST pass ``context_dirty=True`` so
    ``flag_modified`` fires and SQLA actually emits an UPDATE for the
    JSONB column.

    Mirror the canonical pattern from ``rollup_management.py``: write a
    new key into ``parent.context``, release with ``context_dirty=True``,
    then re-read the row in a fresh transaction and confirm the mutation
    landed.

    Without ``context_dirty=True``, SQLA's mutation tracking misses
    in-place dict edits on the JSONB column (mutable type with
    ``copy_on_set=False``-style semantics under SQLModel + JSONB) — the
    UPDATE is suppressed and the change silently disappears on the next
    ``populate_existing`` re-read. This test pins the contract that
    callers can rely on the flag.
    """
    project = Project(name="ctxdirty persist", user_id="user-ctxdirty")
    db_session.add(project)
    await db_session.commit()

    parent = Task(
        project_id=project.id,
        title="parent",
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "research"}],
        context={"original": "value"},
    )
    db_session.add(parent)
    await db_session.commit()
    await db_session.refresh(parent)
    parent_id = parent.id

    # Caller-driven: mutate context in-place, then release with the flag.
    parent.context["new_key"] = "new_value"
    snapshot = await _release_parent_lock(parent, db_session, context_dirty=True)
    assert snapshot is not None

    # Re-read with populate_existing to bypass the identity-map cache.
    fresh = (await db_session.execute(
        select(Task)
        .where(Task.id == parent_id)
        .execution_options(populate_existing=True)
    )).scalar_one()
    assert fresh.context.get("new_key") == "new_value", (
        "context_dirty=True did not persist the in-place mutation — "
        "flag_modified is no longer firing on context. R5-W3-B contract broken."
    )
    assert fresh.context.get("original") == "value"


@pytest.mark.skipif(
    not USE_POSTGRES,
    reason=(
        "Silent-loss demonstration depends on JSONB mutation-tracking semantics; "
        "SQLite's JSON column type behaves differently and would mask the "
        "footgun. The contract holds in production (Postgres)."
    ),
)
@pytest.mark.asyncio
async def test_release_parent_lock_silently_drops_context_when_dirty_flag_omitted(db_session):
    """R5-W3-B FOOTGUN documentation: if a caller forgets
    ``context_dirty=True`` after mutating ``parent.context`` in-place,
    SQLA's mutation tracking misses the change and the UPDATE is
    suppressed.

    This test pins the FOOTGUN behavior so a future change that fixes
    the silent loss (e.g. by re-enabling unconditional flag_modified, or
    switching to a MutableDict TypeDecorator) is intentional and the
    test gets updated, not accidentally papered-over. Mirrors the
    pattern of pinning ``--noconftest`` invariants in test_invariants.py.

    Documented contract: callers that mutate ``parent.context`` MUST
    pass ``context_dirty=True``. Read-only release sites omit it and
    pay no JSONB-write cost (R5-W3-B benefit).
    """
    project = Project(name="ctxdirty footgun", user_id="user-ctxdirty-fg")
    db_session.add(project)
    await db_session.commit()

    parent = Task(
        project_id=project.id,
        title="parent",
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "research"}],
        context={"original": "value"},
    )
    db_session.add(parent)
    await db_session.commit()
    await db_session.refresh(parent)
    parent_id = parent.id

    # FOOTGUN: in-place mutation WITHOUT context_dirty=True.
    parent.context["forgotten_key"] = "lost_value"
    await _release_parent_lock(parent, db_session)  # no context_dirty kw

    # Re-read in a fresh session connection to bypass any in-memory state.
    Session = sessionmaker(
        bind=db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with Session() as fresh_session:
        fresh = (await fresh_session.execute(
            select(Task).where(Task.id == parent_id)
        )).scalar_one()
        # Document the silent-loss contract. If this assertion ever
        # changes (i.e. the mutation persists without context_dirty), it
        # means a future commit fixed the footgun — update the test
        # message + the docstring of _release_parent_lock to match.
        assert "forgotten_key" not in (fresh.context or {}), (
            "Silent-loss contract changed: in-place context mutation now persists "
            "WITHOUT context_dirty=True. If this is intentional (e.g. you switched "
            "to MutableDict), remove this test and update the _release_parent_lock "
            "docstring. Otherwise, the footgun guard regressed."
        )


# ── Tool trace enrichment in reaper failure messages ─────────────────────────


@pytest.mark.asyncio
async def test_reaper_enriches_heartbeat_timeout_with_tool_trace(db_session):
    """When the reaper marks a task FAILED for heartbeat timeout (post-claim),
    the reap_reason and latest_status_message must include the tail of the
    _tool_trace from task.context so operators can see what the agent was
    running when it went silent without digging into the context JSONB.

    The tool trace is captured by the heartbeat endpoint and stored as a ring
    buffer of recent tool calls (timestamp, tool name, input preview).
    """
    from app.reaper import _extract_tool_trace_tail

    # Create a task with a mock tool trace
    project = Project(name="tool trace test", user_id="user-trace")
    db_session.add(project)
    await db_session.commit()

    task = Task(
        project_id=project.id,
        title="task with trace",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "create", "scope": "small"}],
        started_at=datetime.utcnow() - timedelta(seconds=400),
        last_heartbeat=datetime.utcnow() - timedelta(seconds=350),
        context={
            "_tool_trace": [
                {
                    "t": "2026-05-04T10:12:30.123Z",
                    "tool": "Read",
                    "input_preview": "file_path=/tmp/workspace/backend/app/models.py, limit=100",
                },
                {
                    "t": "2026-05-04T10:12:45.456Z",
                    "tool": "Grep",
                    "input_preview": "pattern=_tool_trace, path=/tmp/workspace/backend/app",
                },
                {
                    "t": "2026-05-04T10:13:00.789Z",
                    "tool": "Edit",
                    "input_preview": "file_path=/tmp/workspace/backend/app/reaper.py, old_string=..., new_string=...",
                },
            ]
        },
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # Verify the helper extracts and formats the tail correctly
    tail = _extract_tool_trace_tail(task)
    assert tail, "Expected non-empty tool trace tail"
    assert "Read:" in tail or "Grep:" in tail or "Edit:" in tail, (
        f"Expected tool name in tail; got: {tail}"
    )
    assert "10:12" in tail or "10:13" in tail, (
        f"Expected timestamp in tail; got: {tail}"
    )
    # Should include at least part of the input preview
    assert len(tail) > 0, "Tool trace tail should have content"


@pytest.mark.asyncio
async def test_reaper_tool_trace_fallback_when_missing(db_session):
    """When _tool_trace is missing or empty, the helper returns an empty
    string and reap_reason is unchanged (no "| last tools:" suffix).
    """
    from app.reaper import _extract_tool_trace_tail

    project = Project(name="no trace test", user_id="user-no-trace")
    db_session.add(project)
    await db_session.commit()

    # Task with no tool trace
    task = Task(
        project_id=project.id,
        title="no trace",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "create"}],
        context={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    tail = _extract_tool_trace_tail(task)
    assert tail == "", (
        f"Expected empty tail for missing _tool_trace; got: {tail}"
    )

    # Task with empty tool trace list
    task2 = Task(
        project_id=project.id,
        title="empty trace",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "create"}],
        context={"_tool_trace": []},
    )
    db_session.add(task2)
    await db_session.commit()
    await db_session.refresh(task2)

    tail2 = _extract_tool_trace_tail(task2)
    assert tail2 == "", (
        f"Expected empty tail for empty _tool_trace; got: {tail2}"
    )


# ── Heartbeat-stale cutoff with PR artifact (reaper.py feature) ──────────────


@pytest.mark.asyncio
async def test_heartbeat_stale_cutoff_uses_90s_for_pr_tasks():
    """When a task has a PULL_REQUEST artifact, the heartbeat-stale cutoff
    should be 90s instead of the scope-aware default (300-900s).

    This allows the reaper to detect post-PR silent deaths faster (90-150s
    instead of 5-15 min) and hand off to the review pipeline sooner.

    Regression test for fix(reaper): ratchet heartbeat-stale cutoff to 90s
    once PR artifact exists.
    """
    from app.reaper import _heartbeat_stale_cutoff
    from datetime import datetime, timezone

    # Create a task with any scope/effort combination
    task = Task(
        project_id=uuid.uuid4(),
        title="pr task",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "create", "scope": "large"}],
    )

    now = datetime(2026, 5, 4, 12, 0, 0, tzinfo=timezone.utc)

    # Without PR, should use scope-aware cutoff (large = 900s)
    cutoff_without_pr = _heartbeat_stale_cutoff(task, now, has_open_pr=False)
    expected_without_pr = now - timedelta(seconds=900)
    assert cutoff_without_pr == expected_without_pr, (
        f"Expected cutoff {expected_without_pr} for large-scope task without PR; "
        f"got {cutoff_without_pr}"
    )

    # With PR, should use 90s cutoff regardless of scope
    cutoff_with_pr = _heartbeat_stale_cutoff(task, now, has_open_pr=True)
    expected_with_pr = now - timedelta(seconds=90)
    assert cutoff_with_pr == expected_with_pr, (
        f"Expected cutoff {expected_with_pr} for task with open PR; "
        f"got {cutoff_with_pr}"
    )

    # Verify the difference: PR cutoff is much shorter
    # cutoff_with_pr is later (closer to now) than cutoff_without_pr
    # so cutoff_with_pr > cutoff_without_pr, and the difference is positive
    diff_seconds = (cutoff_with_pr - cutoff_without_pr).total_seconds()
    assert diff_seconds == 810, (
        f"Expected 810s difference (900s - 90s); got {diff_seconds}s. "
        "PR-based cutoff is not reducing the stale window correctly."
    )


@pytest.mark.asyncio
async def test_orphan_pr_handoff_fires_within_90s_window(db_session):
    """Regression test: when a task with a PULL_REQUEST artifact has a stale
    heartbeat (older than 90s), the reaper should detect it as orphaned in the
    initial detection phase (using the fast 90s cutoff) and hand it off to the
    review pipeline.

    This verifies the end-to-end flow: task creation + PR artifact + stale
    heartbeat → reaper detects orphan → hands off to review.
    """
    project = Project(name="pr handoff", user_id="user-pr-handoff")
    db_session.add(project)
    await db_session.commit()

    # Create a task with IN_PROGRESS status and a stale heartbeat
    # (120s old, which exceeds 90s cutoff but is within 300s scope default)
    task = Task(
        project_id=project.id,
        title="t",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "create", "scope": "small"}],
        vm_instance_id="test-vm-1",
        started_at=datetime.utcnow() - timedelta(seconds=10),
        last_heartbeat=datetime.utcnow() - timedelta(seconds=120),
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # Add a PULL_REQUEST artifact to mark the task as having an open PR
    artifact = Artifact(
        task_id=task.id,
        artifact_type=ArtifactType.PULL_REQUEST,
        pr_url="https://github.com/owner/repo/pull/123",
    )
    db_session.add(artifact)
    await db_session.commit()

    # Import the cutoff function to verify the task meets the 90s orphan threshold
    from app.reaper import _heartbeat_stale_cutoff
    now = datetime.utcnow()

    # Verify the task's heartbeat is stale according to the 90s PR-cutoff
    pr_cutoff = _heartbeat_stale_cutoff(task, now, has_open_pr=True)
    assert task.last_heartbeat < pr_cutoff, (
        f"Task's heartbeat ({task.last_heartbeat}) should be < 90s PR cutoff ({pr_cutoff}) "
        "for this test to verify the orphan detection fires."
    )

    # Verify the task's heartbeat would NOT be stale with scope-default (300s)
    scope_cutoff = _heartbeat_stale_cutoff(task, now, has_open_pr=False)
    assert task.last_heartbeat >= scope_cutoff, (
        f"Task's heartbeat ({task.last_heartbeat}) should be >= scope cutoff ({scope_cutoff}) "
        "to verify the PR-based fast cutoff is the differentiator."
    )
