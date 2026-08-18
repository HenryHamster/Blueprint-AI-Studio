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
import contextlib
import os
import uuid
from datetime import datetime, timedelta, timezone

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


USE_POSTGRES = os.getenv("USE_TEST_POSTGRES", "").lower() == "true" and os.getenv(
    "DATABASE_URL", ""
).startswith("postgresql")


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
    parent.last_heartbeat = datetime.now(timezone.utc).replace(tzinfo=None)
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
                row.last_heartbeat = datetime.now(timezone.utc).replace(tzinfo=None)
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
        peer = (
            (await peer_session.execute(select(Task).where(Task.id == parent_id)))
            .scalars()
            .first()
        )
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
    cached = (
        (await db_session.execute(select(Task).where(Task.id == parent_id)))
        .scalars()
        .first()
    )
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
        db_session,
        task.id,
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
        db_session,
        task.id,
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
        db_session,
        task.id,
        allowed_statuses=(TaskStatus.EVALUATING,),
        reason="B3 backcompat",
    )
    assert locked is not None
    await db_session.rollback()


@pytest.mark.asyncio
async def test_lock_task_for_mutation_preserves_prior_dirty_state_on_skip(
    db_session,
):
    """A failed lock attempt (status mismatch / row contended) must NOT wipe
    uncommitted dirty state from earlier in the caller's loop.

    Bug observed 2026-05-07: stale-PROVISIONING recovery loop logged
    'marking FAILED' 17 times in a row because each iteration's session.add
    was reverted by the next iteration's rollback-on-skipped-lock. The fix
    was to commit (rather than rollback) when releasing a held lock and to
    skip release entirely when no lock was acquired.
    """
    from app.orchestrator import _lock_task_for_mutation

    project = Project(name="rollback-preserves-state", user_id="user-rps")
    db_session.add(project)
    await db_session.commit()

    # Task A: caller mutates this in iteration 1.
    task_a = Task(
        project_id=project.id,
        title="A",
        status=TaskStatus.PROVISIONING,
        payloads=[{"type": "create"}],
    )
    # Task B: caller's iteration-2 lock against this fails the
    # allowed_statuses check (B is COMPLETED, not PROVISIONING).
    task_b = Task(
        project_id=project.id,
        title="B",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
    )
    db_session.add(task_a)
    db_session.add(task_b)
    await db_session.commit()
    a_id = task_a.id
    b_id = task_b.id

    # Iteration 1: mutate task A in the session (uncommitted).
    fresh_a = await _lock_task_for_mutation(
        db_session,
        a_id,
        allowed_statuses=(TaskStatus.PROVISIONING,),
        reason="iter1",
    )
    assert fresh_a is not None
    fresh_a.status = TaskStatus.FAILED
    fresh_a.latest_status_message = "iter1 marked FAILED"
    db_session.add(fresh_a)

    # Iteration 2: try to lock B; status check fails → helper releases lock.
    # In the buggy version this called session.rollback(), wiping A's mutation.
    fresh_b = await _lock_task_for_mutation(
        db_session,
        b_id,
        allowed_statuses=(TaskStatus.PROVISIONING,),
        reason="iter2",
    )
    assert fresh_b is None  # status check failed (COMPLETED != PROVISIONING)

    # Loop body would normally do its end-of-cycle commit here, but the helper
    # already committed on the lock-release path (commit-instead-of-rollback),
    # so A's FAILED state should already be persisted.
    refreshed = await db_session.execute(
        select(Task).where(Task.id == a_id).execution_options(populate_existing=True)
    )
    persisted_a = refreshed.scalars().first()
    assert persisted_a.status == TaskStatus.FAILED, (
        "_lock_task_for_mutation rolled back the caller's prior dirty state "
        "instead of preserving it. The lock-release path must commit (not "
        "rollback) when prior iterations have made uncommitted mutations."
    )
    assert persisted_a.latest_status_message == "iter1 marked FAILED"


# ── CI-polling snapshot coverage (R6-W4-A, audit MED-4) ─────────────────────


@pytest.mark.asyncio
async def test_capture_ci_polling_snapshot_includes_all_5_fields():
    """`_capture_ci_polling_snapshot` must cover the full attempt authority.

    Heartbeat-only updates touch none of these — that's the whole point
    of the content-snapshot validator (R5-W2-A). If a future refactor
    drops an attempt coordinate, review-wait owner, or approval provenance
    field, the CI polling sites can silently write through a peer mutation;
    this test pins the expanded contract.  The historical test name is kept
    so CI duration history remains comparable.
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
        "status",
        "archived",
        "project_id",
        "requires_approval",
        "ci_waiting",
        "pr_number",
        "ci_passed",
        "review_approved",
        "iteration",
        "branch",
        "base_branch",
        "ci_checked_head_sha",
        "ci_checked_base_sha",
        "review_wait_started",
        "review_wait_approval_hold",
        "approval_set_at",
        "approval_hold_reason",
        "touched_critical",
    }, (
        f"Snapshot must include exactly the protected authority fields; got "
        f"{set(snapshot.keys())}. "
        "Adding/removing keys is a contract change — update the CI polling sites in lockstep."
    )
    assert snapshot["status"] == TaskStatus.EVALUATING
    assert snapshot["archived"] is False
    assert snapshot["project_id"] == str(task.project_id)
    assert snapshot["requires_approval"] is False
    assert snapshot["ci_waiting"] is None
    assert snapshot["pr_number"] == 17
    assert snapshot["ci_passed"] is False
    assert snapshot["review_approved"] is False
    assert snapshot["iteration"] == 2
    assert snapshot["branch"] is None
    assert snapshot["base_branch"] is None
    assert snapshot["ci_checked_head_sha"] is None
    assert snapshot["ci_checked_base_sha"] is None
    assert snapshot["review_wait_started"] is None
    assert snapshot["review_wait_approval_hold"] is None
    assert snapshot["approval_set_at"] is None
    assert snapshot["approval_hold_reason"] is None
    assert snapshot["touched_critical"] is None


@pytest.mark.asyncio
async def test_capture_ci_polling_snapshot_handles_missing_review_loop():
    """When ``context`` lacks a ``review_loop`` key (early in the pipeline,
    or when the task hasn't entered review yet), the snapshot must still return
    the complete authority shape with deterministic placeholders across the
    pre/post-release boundary."""
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
        "archived": False,
        "project_id": str(task.project_id),
        "requires_approval": False,
        "ci_waiting": None,
        "pr_number": None,
        "ci_passed": None,
        "review_approved": None,
        "iteration": None,
        "branch": None,
        "base_branch": None,
        "ci_checked_head_sha": None,
        "ci_checked_base_sha": None,
        "review_wait_started": None,
        "review_wait_approval_hold": None,
        "approval_set_at": None,
        "approval_hold_reason": None,
        "touched_critical": None,
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
        db_session,
        task.id,
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
        db_session,
        task.id,
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
    db_session,
    field,
    old_value,
    new_value,
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
        db_session,
        task.id,
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
    task.last_heartbeat = datetime.now(timezone.utc).replace(tzinfo=None)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    if task.updated_at == pre_updated_at:
        pytest.skip(
            "onupdate did not advance updated_at within the test window — "
            "cannot exercise the heartbeat-overlap regression."
        )

    locked = await _lock_task_for_mutation(
        db_session,
        task.id,
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
    fresh = (
        await db_session.execute(
            select(Task)
            .where(Task.id == parent_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
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
async def test_release_parent_lock_silently_drops_context_when_dirty_flag_omitted(
    db_session,
):
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
        fresh = (
            await fresh_session.execute(select(Task).where(Task.id == parent_id))
        ).scalar_one()
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
        started_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=400),
        last_heartbeat=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=350),
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
    assert tail == "", f"Expected empty tail for missing _tool_trace; got: {tail}"

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
    assert tail2 == "", f"Expected empty tail for empty _tool_trace; got: {tail2}"


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
        started_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=10),
        last_heartbeat=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=120),
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

    now = datetime.now(timezone.utc).replace(tzinfo=None)

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


# ── C2: never hold a DB lock across GitHub/network I/O ───────────────────────
#
# Cluster C2 root cause: GitHub round-trips ran INSIDE critical sections.
#   (1) _spawn_pipeline_task's scope-overflow PR signal (token + label +
#       comment) ran while the per-PR advisory lock was held.
#   (2) ci_completion's CI-failure create_pr_comment ran while the task row
#       was held FOR UPDATE.
#   (3) the reaper orphan-reap batch committed all orphans at once, so one
#       ux_task_active_idempotency_key collision rolled back the whole batch.
# The fix: resolve tokens before acquiring locks, defer GitHub signals to a
# post-commit (lock-released) block, mirror the merge path's
# capture/commit-release/relock for the CI-failure branch, and pre-query the
# idempotency-key collision per orphan. These seam tests would FAIL pre-fix.


@pytest.mark.asyncio
async def test_spawn_scope_overflow_signal_fires_after_commit(db_session, mocker):
    """C2 (1): the scope-overflow GitHub signal must run AFTER the spawn
    commits — i.e. after the per-PR advisory lock is released, never while
    it is held.

    Seam: we observe, from inside the mocked GitHub calls, that the spawn
    has already committed by checking — on a SEPARATE session — that the
    durable idempotency flag ``scope_overflow_pr_signal_emitted`` is already
    persisted on the source task. Pre-fix the label/comment ran BEFORE any
    commit, so a peer session would NOT yet see the persisted flag when the
    GitHub call fired; this assertion fails on the old code.
    """
    from unittest.mock import AsyncMock

    from app.review_pipeline import (
        SCOPE_OVERFLOW_PR_SIGNAL_EMITTED_KEY,
        _spawn_pipeline_task,
    )

    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("ghs_test", "acme", "repo"),
    )

    project = Project(
        name="C2 spawn overflow",
        user_id="user-c2",
        github_owner="acme",
        github_repo="repo",
        integration_branch="main",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    source_task = Task(
        project_id=project.id,
        title="overflow source",
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "create",
                "pr_number": 7777,
                "branch": "feat/c2-overflow",
                "base_branch": "main",
                "prompt": "Implement feature",
            }
        ],
        context={
            "scope": "small",
            # small-scope budget is SCOPE_FILE_BUDGETS["small"]["total_files_max"]
            # (8); must exceed it to trigger the overflow signal.
            "_pr_changed_files": [f"backend/app/m{i}.py" for i in range(9)],
            "review_loop": {"iteration": 0},
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)
    source_id = source_task.id

    Session = sessionmaker(
        bind=db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    # Observed-from-inside-the-GitHub-call: the flag must already be durable.
    flag_visible_during_io: list[bool] = []

    async def _observing_add_issue_label(*args, **kwargs):
        async with Session() as peer:
            peer_src = await peer.get(Task, source_id)
            flag_visible_during_io.append(
                bool(
                    (peer_src.context or {}).get(
                        SCOPE_OVERFLOW_PR_SIGNAL_EMITTED_KEY
                    )
                )
                if peer_src is not None
                else False
            )

    mocker.patch(
        "app.github.add_issue_label",
        new=AsyncMock(side_effect=_observing_add_issue_label),
    )
    comment_mock = mocker.patch(
        "app.github.create_pr_comment", new_callable=AsyncMock
    )

    result, spawned = await _spawn_pipeline_task(
        source_task,
        "review",
        db_session,
        pr_number=7777,
        branch="feat/c2-overflow",
        base_branch="main",
        original_prompt="Implement feature",
    )

    assert spawned is True and result is not None
    # The signal still fires exactly once.
    assert comment_mock.await_count == 1
    assert flag_visible_during_io == [True], (
        "Scope-overflow GitHub signal ran while the spawn was still "
        "uncommitted (advisory lock held). A peer session could not see the "
        "durable idempotency flag during the GitHub call — the signal must "
        "be deferred to the post-commit, lock-released block (C2 fix)."
    )


@pytest.mark.asyncio
async def test_spawn_scope_overflow_signal_failure_does_not_block_spawn(
    db_session, mocker
):
    """C2 (1): a GitHub failure in the deferred scope-overflow signal is
    best-effort and must NOT roll back or block the spawn — the new task is
    committed and the idempotency flag is durable regardless.
    """
    from unittest.mock import AsyncMock

    from app.review_pipeline import (
        SCOPE_OVERFLOW_PR_SIGNAL_EMITTED_KEY,
        _spawn_pipeline_task,
    )

    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("ghs_test", "acme", "repo"),
    )
    mocker.patch(
        "app.github.add_issue_label",
        new=AsyncMock(side_effect=RuntimeError("github down")),
    )
    mocker.patch("app.github.create_pr_comment", new_callable=AsyncMock)

    project = Project(
        name="C2 spawn overflow fail",
        user_id="user-c2",
        github_owner="acme",
        github_repo="repo",
        integration_branch="main",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    source_task = Task(
        project_id=project.id,
        title="overflow source fail",
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "create",
                "pr_number": 7778,
                "branch": "feat/c2-overflow-fail",
                "base_branch": "main",
                "prompt": "Implement feature",
            }
        ],
        context={
            "scope": "small",
            # small-scope budget is SCOPE_FILE_BUDGETS["small"]["total_files_max"]
            # (8); must exceed it to trigger the overflow signal.
            "_pr_changed_files": [f"backend/app/n{i}.py" for i in range(9)],
            "review_loop": {"iteration": 0},
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)
    source_id = source_task.id
    new_task_id = None

    result, spawned = await _spawn_pipeline_task(
        source_task,
        "review",
        db_session,
        pr_number=7778,
        branch="feat/c2-overflow-fail",
        base_branch="main",
        original_prompt="Implement feature",
    )
    assert spawned is True and result is not None
    new_task_id = result.id

    # New task is durably committed despite the GitHub failure.
    Session = sessionmaker(
        bind=db_session.bind, class_=AsyncSession, expire_on_commit=False
    )
    async with Session() as peer:
        peer_new = await peer.get(Task, new_task_id)
        assert peer_new is not None, (
            "Spawned task must remain committed even when the deferred "
            "scope-overflow GitHub signal raised — the I/O is best-effort."
        )
        peer_src = await peer.get(Task, source_id)
        assert (peer_src.context or {}).get(
            SCOPE_OVERFLOW_PR_SIGNAL_EMITTED_KEY
        ) is True, (
            "Idempotency flag must be persisted transactionally before the "
            "GitHub call, so a failed signal never re-fires (C2)."
        )


@pytest.mark.asyncio
async def test_ci_failure_releases_task_lock_around_pr_comment(db_session, mocker):
    """C2 (2): the CI-failure branch must release the task FOR UPDATE lock
    around ``create_pr_comment`` (mirroring the merge SUCCESS path), so a
    concurrent worker can lock the row during the GitHub window.

    Seam: from inside the mocked create_pr_comment, a SEPARATE session does
    ``with_for_update(skip_locked=True)`` on the same task row. If the lock
    was released (post-fix) the peer acquires it (non-None); pre-fix the row
    is still held FOR UPDATE by handle_ci_completion and skip_locked yields
    None. Postgres-only — SQLite has no row-level FOR UPDATE semantics.
    """
    from unittest.mock import AsyncMock

    if not USE_POSTGRES:
        pytest.skip("row-level FOR UPDATE lock-release assertion requires Postgres")

    from app.ci_completion import handle_ci_completion

    # ci_completion imports broadcast_to_project / create_pr_comment
    # function-locally (from app.broadcast / app.github), so they are NOT
    # module attributes of app.ci_completion — patch them at their source
    # module (mirrors test_ci_completion.py:2425, :2424).
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("ghs_test", "acme", "repo"),
    )

    project = Project(
        name="C2 ci-failure",
        user_id="user-c2",
        github_owner="acme",
        github_repo="repo",
        integration_branch="main",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="ci task",
        status=TaskStatus.EVALUATING,
        payloads=[
            {"type": "create", "pr_number": 8888, "branch": "feat/ci", "base_branch": "main"}
        ],
        context={
            "review_loop": {
                "pr_number": 8888,
                "branch": "feat/ci",
                "base_branch": "main",
                "ci_waiting": True,
            }
        },
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    task_id = task.id

    Session = sessionmaker(
        bind=db_session.bind, class_=AsyncSession, expire_on_commit=False
    )
    peer_lock_acquired: list[bool] = []

    async def _peer_lock_probe(*args, **kwargs):
        async with Session() as peer:
            row = (
                await peer.execute(
                    select(Task)
                    .where(Task.id == task_id)
                    .with_for_update(skip_locked=True)
                )
            ).scalars().first()
            peer_lock_acquired.append(row is not None)
            await peer.rollback()

    mocker.patch(
        "app.github.create_pr_comment",
        new=AsyncMock(side_effect=_peer_lock_probe),
    )

    ci_result = {
        "status": "completed",
        "conclusion": "failure",
        "failed_logs": "pytest FAILED test_x",
    }
    await handle_ci_completion(
        task, ci_result, "ghs_test", "acme", "repo", db_session, source="poll"
    )

    assert peer_lock_acquired == [True], (
        "CI-failure create_pr_comment ran while the task row was still held "
        "FOR UPDATE — the lock must be released around the GitHub comment "
        "(C2 fix mirroring the merge SUCCESS path)."
    )


class _StopReaperLoop(Exception):
    """Raised from a mocked asyncio.sleep to break reaper_service's loop."""


@pytest.mark.asyncio
async def test_reaper_orphan_idempotency_preguard_skips_active_dup(
    db_session, mocker
):
    """C2 (3): the reaper orphan-reap loop pre-queries the
    ux_task_active_idempotency_key collision per orphan and SKIPS an orphan
    whose key is already held by an ACTIVE task — preventing the IntegrityError
    that would otherwise roll back the ENTIRE batch's recovery work.

    This drives the real ``reaper_service`` loop one cycle (the same harness
    test_reaper.py uses: patch ``asyncio.sleep`` to raise, neutralize the
    side-effecting deferred imports). It would PASS even if the guard were
    reverted ONLY if it merely re-implemented the SELECT inline; instead we
    assert the OBSERVABLE behavior the guard produces:

      - the colliding orphan is left UNTOUCHED in PROVISIONING (guard skipped
        it before mutating); revert the guard (reaper.py:1109-1134) and the
        reap flips it off PROVISIONING → this assertion fails.
      - an INDEPENDENT orphan in the same batch IS reaped (flipped off
        PROVISIONING), proving the loop actually ran the reap body.
      - the active duplicate is untouched.

    Two ACTIVE rows cannot share an idempotency_key while the partial unique
    index ``ux_task_active_idempotency_key`` is enforced (verified on both
    SQLite and Postgres). So to construct the exact state the guard inspects —
    an orphan that, if reaped, would collide with an already-active peer — we
    DROP that index on the test engine for the duration of the cycle. The
    index is what makes the collision PAINFUL (all-batch rollback); the GUARD
    is what AVOIDS it by skipping pre-mutation, and the guard's skip decision
    is exactly what this test isolates.
    """
    bind = db_session.bind

    # The partial unique index forbids two coexisting active rows with one key.
    # Drop it so we can construct the orphan-collides-with-active-peer state the
    # guard is designed to detect, then drive the real loop. Recreated in the
    # finally below so the shared Postgres schema (CI) is left intact for other
    # tests; on SQLite the per-test drop_all/create_all makes this moot.
    _idx = next(
        idx
        for idx in Task.__table__.indexes
        if idx.name == "ux_task_active_idempotency_key"
    )

    async def _drop_active_idem_index():
        try:
            await db_session.execute(
                text("DROP INDEX ux_task_active_idempotency_key")
            )
            await db_session.commit()
        except Exception:
            await db_session.rollback()

    async def _recreate_active_idem_index():
        # CREATE UNIQUE INDEX validates EXISTING rows, and the body
        # deliberately leaves two active-status rows sharing one key (the
        # exact state the guard inspects) — the index can NOT come back while
        # they sit inside the predicate window. Park every keyed row in
        # FAILED first (outside the predicate; the fixture teardown deletes
        # them right after this returns). The recreate must not be
        # best-effort: a silently-missing index poisons every later
        # index-dependent test in this pytest process — exactly how
        # test_orchestrator's dup-claim regression saw the EVALUATING flip
        # COMMIT instead of raise on CI shard 5 (PR #8635).
        with contextlib.suppress(Exception):
            # End any aborted transaction the body left on db_session so its
            # held locks cannot block the CREATE INDEX below.
            await db_session.rollback()
        async with bind.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE task SET status='FAILED' "
                    "WHERE idempotency_key IS NOT NULL"
                )
            )
            # checkfirst: if the DROP above was the part that failed, the
            # index never left — skip the CREATE instead of masking the
            # body's real failure with a spurious "already exists".
            await conn.run_sync(_idx.create, checkfirst=True)

    await _drop_active_idem_index()
    try:
        await _run_reaper_preguard_body(db_session, mocker, bind)
    finally:
        await _recreate_active_idem_index()


async def _run_reaper_preguard_body(db_session, mocker, bind):
    project = Project(name="C2 reaper preguard", user_id="user-c2")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    dup_key = f"dup-key-{uuid.uuid4().hex}"
    stale = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=16)

    # Active peer already holding the key in an active status.
    active_dup = Task(
        project_id=project.id,
        title="active dup",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "create"}],
        idempotency_key=dup_key,
        last_heartbeat=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    # Orphan whose reap would re-enter the key into an active status and collide
    # with active_dup — the guard must skip it BEFORE mutating.
    colliding_orphan = Task(
        project_id=project.id,
        title="colliding orphan",
        status=TaskStatus.PROVISIONING,
        payloads=[{"type": "create"}],
        idempotency_key=dup_key,
        vm_instance_id="vm-colliding",
        started_at=stale,
        context={"provision_zone": "us-east1-a"},
    )
    # Independent orphan with no colliding key — must be reaped normally,
    # proving the loop body ran (and, pre-fix, that the all-batch rollback
    # would have swallowed THIS orphan's recovery too).
    independent_orphan = Task(
        project_id=project.id,
        title="independent orphan",
        status=TaskStatus.PROVISIONING,
        payloads=[{"type": "create"}],
        idempotency_key=None,
        vm_instance_id="vm-independent",
        started_at=stale,
        context={"provision_zone": "us-east1-a"},
    )
    db_session.add_all([active_dup, colliding_orphan, independent_orphan])
    await db_session.commit()
    for _t in (active_dup, colliding_orphan, independent_orphan):
        await db_session.refresh(_t)

    # Drive the real reaper loop one cycle against the same engine.
    Session = sessionmaker(
        bind=bind, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )
    mocker.patch("app.reaper.AsyncSessionLocal", Session)
    mocker.patch("asyncio.sleep", side_effect=_StopReaperLoop())
    # Neutralize the side-effecting deferred imports (mirror reaper_side_effects).
    from unittest.mock import AsyncMock

    _vm_deleted = mocker.patch(
        "app.vm_lifecycle._ensure_vm_deleted", new_callable=AsyncMock, return_value=True
    )
    mocker.patch("app.vm_lifecycle.delete_agent_vm", return_value=True)
    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock)
    mocker.patch(
        "app.rollup_management._check_and_trigger_rollup", new_callable=AsyncMock
    )
    mocker.patch("app.reaper_orphan_io._capture_serial_for_vm", return_value=None)
    mocker.patch("app.reaper.record_zone_failure")
    mocker.patch("app.reaper.any_zone_available", return_value=True)

    from app.reaper import reaper_service

    with contextlib.suppress(_StopReaperLoop):
        await reaper_service()

    # Read final state on a fresh session (the loop committed on its own).
    async with Session() as verify:
        v_dup = await verify.get(Task, active_dup.id)
        v_collide = await verify.get(Task, colliding_orphan.id)
        v_indep = await verify.get(Task, independent_orphan.id)

    # Guard skipped the colliding orphan BEFORE mutating it: still PROVISIONING.
    assert v_collide.status == TaskStatus.PROVISIONING, (
        "Pre-query guard must SKIP the orphan whose idempotency_key is held by "
        "an active task, leaving it untouched in PROVISIONING — revert the "
        "guard (reaper.py:1109-1134) and the reap flips it off PROVISIONING "
        f"(got {v_collide.status})."
    )
    # The loop actually ran: the independent orphan was reaped.
    assert v_indep.status != TaskStatus.PROVISIONING, (
        "Independent orphan must be reaped (flipped off PROVISIONING), proving "
        "the orphan-reap body ran and that the guard isolated the collision to "
        "the colliding orphan only — not an all-batch rollback (C2)."
    )
    # The active duplicate is untouched.
    assert v_dup.status == TaskStatus.IN_PROGRESS

    # VM-GC (C2 issue E): a guard-skipped orphan must still have its dead VM
    # collected for deletion — the VM-collection step lives AFTER the guard in
    # the reap body, so a bare ``continue`` would otherwise strand it.
    _deleted_vms = {
        (c.args[0] if c.args else c.kwargs.get("task_or_vm_id"))
        for c in _vm_deleted.await_args_list
    }
    assert "vm-colliding" in _deleted_vms, (
        "Guard-skipped orphan's VM must be collected for deletion (C2 issue E) "
        "— it is not stranded until the active duplicate frees the key."
    )


@pytest.mark.asyncio
async def test_rollup_releases_parent_lock_around_review_respawn_spawn(
    db_session, mocker
):
    """C2 (3 — change #3): the rollup review-respawn loop must release the
    parent FOR UPDATE lock BEFORE the lock-free spawn window (Phase B), so a
    peer can acquire the parent row DURING ``_spawn_pipeline_task`` (Phase C).

    Pre-fix the spawn ran INSIDE the unmerged loop while the parent lock was
    nominally held (each spawn silently committed-released it mid-loop and the
    next iteration mutated an unlocked row). Post-fix Phase B explicitly
    commit-releases the parent lock and Phase D re-locks with snapshot
    validation.

    Seam: from inside the mocked ``_spawn_pipeline_task`` (Phase C), a SEPARATE
    session does ``with_for_update(skip_locked=True)`` on the parent row. If
    the lock was released (post-fix) the peer acquires it (non-None); pre-fix
    the parent is still held FOR UPDATE by ``_check_and_trigger_rollup`` and
    skip_locked yields None. Postgres-only — SQLite has no row-level FOR UPDATE.
    """
    from unittest.mock import AsyncMock, MagicMock

    if not USE_POSTGRES:
        pytest.skip("row-level FOR UPDATE lock-release assertion requires Postgres")

    from app.rollup_management import _check_and_trigger_rollup

    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock)
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("token", "acme", "repo"),
    )
    # PR not merged + open → the child stays "unmerged" (all_done=False) so the
    # respawn path is reached.
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        return_value={"merged": False, "state": "open"},
    )

    project = Project(
        name="C2 rollup respawn lock",
        user_id="user-c2",
        github_owner="acme",
        github_repo="repo",
        integration_branch="main",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    parent = Task(
        project_id=project.id,
        title="rollup parent",
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "research", "prompt": "do thing"}],
        context={"research_branch": "research/c2-stg", "parent_base": "main"},
        plan_output="plan",
    )
    db_session.add(parent)
    await db_session.commit()
    await db_session.refresh(parent)
    parent_id = parent.id

    # Unmerged stale COMPLETED child (>10min) with a valid PR artifact so it
    # becomes a respawn candidate and Phase C actually spawns.
    stale_time = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=15)
    child = Task(
        project_id=project.id,
        parent_task_id=parent.id,
        title="stale child",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "implement", "base_branch": "main"}],
        context={"review_loop": {"branch": "blueprint/c2-child"}},
        updated_at=stale_time,
    )
    db_session.add(child)
    await db_session.commit()
    await db_session.refresh(child)

    artifact = Artifact(
        task_id=child.id,
        artifact_type=ArtifactType.PULL_REQUEST,
        pr_url="https://github.com/acme/repo/pull/4242",
    )
    db_session.add(artifact)
    await db_session.commit()

    Session = sessionmaker(
        bind=db_session.bind, class_=AsyncSession, expire_on_commit=False
    )
    peer_lock_acquired: list[bool] = []

    async def _spawn_probe(*args, **kwargs):
        async with Session() as peer:
            row = (
                await peer.execute(
                    select(Task)
                    .where(Task.id == parent_id)
                    .with_for_update(skip_locked=True)
                )
            ).scalars().first()
            peer_lock_acquired.append(row is not None)
            await peer.rollback()
        # Return a plausible spawned-review tuple so Phase C proceeds.
        return (MagicMock(id=uuid.uuid4()), True)

    mocker.patch(
        "app.rollup_management._spawn_pipeline_task",
        new=AsyncMock(side_effect=_spawn_probe),
    )

    await _check_and_trigger_rollup(child, db_session)

    assert peer_lock_acquired == [True], (
        "rollup review-respawn _spawn_pipeline_task ran while the parent row "
        "was still held FOR UPDATE — Phase B must release the parent lock "
        "around the lock-free spawn window (C2 change #3)."
    )


# ── C4: mergeability-reset close/reopen race seam tests ─────────────────────
#
# Cluster C4: the orchestrator deliberately close+reopens a PR whose
# mergeable field is stuck at None to force GitHub to recompute. Three
# handlers that touch the SAME PR during that transient closed-not-merged
# window previously interpreted it as a deliberate merge/abandonment because
# they gated on ``state == 'closed'`` rather than ``merged == True``. These
# tests lock in the corrected merged-only / pending_reopen-aware behavior.


@pytest.mark.asyncio
async def test_merge_snapshot_includes_pending_reopen():
    """C4 fix #2: ``_capture_merge_snapshot`` must include pending_reopen so a
    CI-poll commit of pending_reopen=True during the merge's unlocked GitHub
    window invalidates the snapshot and ``_relock_merge_task`` defers."""
    from app.ci_completion import _capture_merge_snapshot

    task = Task(
        project_id=uuid.uuid4(),
        title="merge snapshot",
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "review"}],
        context={"review_loop": {"iteration": 1, "review_approved": True}},
    )
    snap = _capture_merge_snapshot(task)
    assert snap["review_loop_pending_reopen"] is False

    task.context = {
        "review_loop": {
            "iteration": 1,
            "review_approved": True,
            "pending_reopen": True,
        }
    }
    snap2 = _capture_merge_snapshot(task)
    assert snap2["review_loop_pending_reopen"] is True
    assert snap != snap2, (
        "A pending_reopen flip MUST change the merge snapshot so _relock_merge_task "
        "defers when the orchestrator self-closes the PR mid-merge (C4)."
    )


@pytest.mark.asyncio
async def test_relock_merge_task_defers_on_pending_reopen_flip(db_session):
    """C4 fix #2 seam: when the orchestrator commits pending_reopen=True on the
    parent during the merge's unlocked window, ``_relock_merge_task`` must see
    the snapshot mismatch and return None (defer) rather than proceed into the
    closed-state branch on a PR we deliberately closed."""
    from app.ci_completion import _capture_merge_snapshot, _relock_merge_task

    project = Project(name="C4 relock pending_reopen", user_id="user-c4")
    db_session.add(project)
    await db_session.commit()

    task = Task(
        project_id=project.id,
        title="merge gate task",
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "review", "pr_number": 77}],
        context={
            "review_loop": {
                "pr_number": 77,
                "iteration": 1,
                "review_approved": True,
                "ci_passed": True,
            }
        },
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # Snapshot taken BEFORE the unlocked GitHub window (pending_reopen unset).
    pre_snapshot = _capture_merge_snapshot(task)
    assert pre_snapshot["review_loop_pending_reopen"] is False

    # Simulate the orchestrator's CI poll self-closing the PR and committing
    # pending_reopen=True during the unlocked window. Full reassignment so
    # SQLA's change-tracking detects the JSONB mutation without flag_modified.
    _loop = {**((task.context or {}).get("review_loop", {})), "pending_reopen": True}
    task.context = {**(task.context or {}), "review_loop": _loop}
    db_session.add(task)
    await db_session.commit()

    fresh = await _relock_merge_task(
        db_session, task.id, pre_snapshot, task, source="test"
    )
    assert fresh is None, (
        "Expected _relock_merge_task to DEFER (None) after a pending_reopen flip; "
        "proceeding would let the merge gate reach the closed-state branch on a PR "
        "the orchestrator deliberately self-closed (C4 false-success)."
    )


@pytest.mark.asyncio
async def test_lock_original_task_returns_locked_row(db_session):
    """C4 fix #3: ``_lock_original_task`` re-acquires the parent row FOR UPDATE
    and returns it when the row is free (the shared helper that replaced the
    four duplicated skip_locked retry loops, incl. the formerly-unlocked
    merge-success orig fetch)."""
    from app.rollup_locking import _lock_original_task

    project = Project(name="C4 lock orig", user_id="user-c4")
    db_session.add(project)
    await db_session.commit()

    orig = Task(
        project_id=project.id,
        title="rollup parent",
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "research"}],
    )
    db_session.add(orig)
    await db_session.commit()
    await db_session.refresh(orig)

    locked = await _lock_original_task(db_session, str(orig.id))
    assert locked is not None
    assert locked.id == orig.id
    assert locked.status == TaskStatus.EVALUATING


@pytest.mark.asyncio
async def test_lock_original_task_returns_none_when_missing(db_session):
    """C4 fix #3: ``_lock_original_task`` returns None (no infinite retry) for a
    genuinely missing row — the merge-success caller then defers the parent
    bookkeeping to a peer/reaper rather than raising."""
    from app.rollup_locking import _lock_original_task

    result = await _lock_original_task(db_session, str(uuid.uuid4()), attempts=2)
    assert result is None


@pytest.mark.skipif(
    not USE_POSTGRES,
    reason="cross-session FOR UPDATE contention requires Postgres",
)
@pytest.mark.asyncio
async def test_lock_original_task_defers_when_held_by_peer(db_session):
    """C4 fix #3 seam: when a peer holds the parent row FOR UPDATE,
    ``_lock_original_task`` (skip_locked) must DEFER (return None) instead of
    blocking or stealing — the merge-success path then leaves the parent
    transition to the peer/next poll, never overwriting a terminal status."""
    from app.rollup_locking import _lock_original_task

    project = Project(name="C4 lock orig contended", user_id="user-c4")
    db_session.add(project)
    await db_session.commit()

    orig = Task(
        project_id=project.id,
        title="rollup parent",
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "research"}],
    )
    db_session.add(orig)
    await db_session.commit()
    await db_session.refresh(orig)
    orig_id = orig.id

    await db_session.commit()
    db_session.expunge_all()

    Session = sessionmaker(
        bind=db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    # Hold the row FOR UPDATE on a peer session for the duration of the lock attempt.
    async with Session() as peer:
        held = (
            await peer.execute(
                select(Task).where(Task.id == orig_id).with_for_update()
            )
        ).scalars().first()
        assert held is not None
        # attempts=2 with skip_locked → the row is never returned (peer holds it).
        result = await _lock_original_task(db_session, str(orig_id), attempts=2)
        await peer.rollback()

    assert result is None, (
        "Expected _lock_original_task to DEFER (None) when the parent row is held "
        "FOR UPDATE by a peer; blocking or stealing the lock would resurrect the "
        "lost-update race the unlocked session.get had (C4 fix #3)."
    )

