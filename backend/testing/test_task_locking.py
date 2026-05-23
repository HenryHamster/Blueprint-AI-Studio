"""Phase A1 row-lock regression tests.

Audit found 9 sites that used plain ``with_for_update()`` — when the 30s
``lock_timeout`` fires, ``LockNotAvailableError`` propagates as a 500 and
Cloud Run quarantines the instance. Each fix site below has a regression
test that asserts contention surfaces as 429 (handler path) or "skip /
defer" (loop / post-commit path), never as 500.
"""

import asyncio
import os
import uuid
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlmodel import select

from app.models import Project, ProjectMember, Task, TaskStatus
from app.task_locking import (
    acquire_pr_spawn_lock,
    lock_project_or_429,
    project_lock,
    task_lock,
    with_design_doc_locked,
    with_directions_locked,
    with_vision_locked,
)


USE_POSTGRES = os.getenv("USE_TEST_POSTGRES", "").lower() == "true" and os.getenv(
    "DATABASE_URL", ""
).startswith("postgresql")


async def _add_admin_membership(session, project, user_id="test-user-123"):
    """Helper: add the test user as owner on the given project so handler
    role checks (_get_user_project_with_role with min_role='member') pass.
    """
    pm = ProjectMember(
        project_id=project.id,
        user_id=user_id,
        role="owner",
    )
    session.add(pm)
    await session.commit()


# ── Existing concurrency test (kept for regression on Postgres CI) ──────────


@pytest.mark.skipif(not USE_POSTGRES, reason="row-lock concurrency requires PostgreSQL")
@pytest.mark.asyncio
async def test_concurrent_dispatch_mutations_keep_both_context_updates(db_session):
    project = Project(name="lock-test", user_id="user-lock-test")
    db_session.add(project)
    await db_session.commit()

    task = Task(
        project_id=project.id,
        title="queued dispatch lock test",
        status=TaskStatus.QUEUED,
        context={"dispatch_mutations": []},
    )
    db_session.add(task)
    await db_session.commit()

    Session = sessionmaker(
        bind=db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    first_has_lock = asyncio.Event()

    async def dispatch_mutation(label: str, hold_first_lock: bool = False) -> None:
        async with (
            Session() as session,
            task_lock(session, task.id, skip_locked=False, timeout=5) as locked_task,
        ):
            assert locked_task is not None
            if hold_first_lock:
                first_has_lock.set()
                await asyncio.sleep(0.2)
            ctx = dict(locked_task.context or {})
            mutations = list(ctx.get("dispatch_mutations") or [])
            mutations.append(label)
            ctx["dispatch_mutations"] = mutations
            locked_task.context = ctx
            session.add(locked_task)
            await session.commit()

    async def second_dispatch() -> None:
        await first_has_lock.wait()
        await dispatch_mutation("second")

    await asyncio.gather(
        dispatch_mutation("first", hold_first_lock=True),
        second_dispatch(),
    )

    # The sub-sessions committed via fresh sessions; db_session's identity-map
    # copy still holds the pre-mutation snapshot (expire_on_commit=False).
    # Read through a fresh session to see the actual post-commit row state.
    async with Session() as verify_session:
        result = await verify_session.execute(select(Task).where(Task.id == task.id))
        refreshed = result.scalar_one()
        assert refreshed.context["dispatch_mutations"] == ["first", "second"]


# ── Phase A1 helpers ────────────────────────────────────────────────────────


def _stub_first_select_returns_none(session, follow_through_match=None):
    """Wrap session.execute so the first call returns an empty result.

    Simulates a SELECT ... FOR UPDATE SKIP LOCKED that comes up empty
    because a peer holds the row. Subsequent calls pass through to the
    real session — the lock_project_or_429 / project_lock helpers issue a
    follow-up non-locking probe to distinguish "row missing" from "row
    held"; that probe must return real data.

    Returns the AsyncMock so callers can inspect call counts.
    """
    original_execute = session.execute
    state = {"first_locking_seen": False}

    class _EmptyResult:
        def scalars(self):
            return self

        def first(self):
            return None

        def scalar_one_or_none(self):
            return None

        def all(self):
            return []

    async def _mock_execute(stmt, *args, **kwargs):
        # Detect "SELECT FOR UPDATE" by stringifying the compiled statement.
        # We only want to short-circuit the FIRST locking SELECT; subsequent
        # locking SELECTs (the retry, or the non-locking probe) pass through.
        try:
            stmt_str = str(stmt.compile(compile_kwargs={"literal_binds": False}))
        except Exception:
            stmt_str = str(stmt)
        is_locking = "FOR UPDATE" in stmt_str.upper()
        if is_locking and not state["first_locking_seen"]:
            state["first_locking_seen"] = True
            return _EmptyResult()
        return await original_execute(stmt, *args, **kwargs)

    return AsyncMock(side_effect=_mock_execute), state


# ── A1.1 — admin_notifications.py:444 (playbook set) ────────────────────────


@pytest.mark.asyncio
async def test_admin_playbook_set_returns_429_on_lock_contention(
    client, db_session, mocker
):
    """When a peer holds the project row, PUT /admin/projects/{id}/playbook
    must return 429 Retry-After (not 500). Phase A1, was site
    admin_notifications.py:445.
    """
    from fastapi import HTTPException

    project = Project(name="A1 admin playbook lock", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _add_admin_membership(db_session, project)

    # Patch lock_project_or_429 so it raises 429 directly — simulates
    # the SELECT ... FOR UPDATE SKIP LOCKED returning no rows.
    async def _stub_lock(session, project_id):
        raise HTTPException(
            status_code=429,
            detail="Project row locked by concurrent write, retry",
            headers={"Retry-After": "2"},
        )

    mocker.patch(
        "app.routers.admin_notifications.lock_project_or_429",
        new=AsyncMock(side_effect=_stub_lock),
    )

    resp = await client.put(
        f"/api/admin/projects/{project.id}/playbook",
        json={"key": "test-key", "value": "test-value"},
    )
    assert resp.status_code == 429, (
        f"Expected 429 on contention, got {resp.status_code}: {resp.text}"
    )


# ── A1.2 — admin_notifications.py:498 (playbook delete) ─────────────────────


@pytest.mark.asyncio
async def test_admin_playbook_delete_returns_429_on_lock_contention(
    client, db_session, mocker
):
    """DELETE /admin/projects/{id}/playbook/{key} must 429 on contention.
    Phase A1, was site admin_notifications.py:498.
    """
    from fastapi import HTTPException

    project = Project(name="A1 admin playbook delete", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _add_admin_membership(db_session, project)

    async def _stub_lock(session, project_id):
        raise HTTPException(
            status_code=429,
            detail="held",
            headers={"Retry-After": "2"},
        )

    mocker.patch(
        "app.routers.admin_notifications.lock_project_or_429",
        new=AsyncMock(side_effect=_stub_lock),
    )
    resp = await client.delete(f"/api/admin/projects/{project.id}/playbook/test-key")
    assert resp.status_code == 429


# ── A1.3 — admin_notifications.py:535 (playbook seed) ───────────────────────


@pytest.mark.asyncio
async def test_admin_playbook_seed_returns_429_on_lock_contention(
    client, db_session, mocker
):
    """POST /admin/projects/{id}/playbook/seed must 429 on contention.
    Phase A1, was site admin_notifications.py:535.
    """
    from fastapi import HTTPException

    project = Project(name="A1 admin playbook seed", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _add_admin_membership(db_session, project)

    async def _stub_lock(session, project_id):
        raise HTTPException(
            status_code=429,
            detail="held",
            headers={"Retry-After": "2"},
        )

    mocker.patch(
        "app.routers.admin_notifications.lock_project_or_429",
        new=AsyncMock(side_effect=_stub_lock),
    )
    resp = await client.post(f"/api/admin/projects/{project.id}/playbook/seed")
    assert resp.status_code == 429


# ── A1.4 — directions.py:537 (manual strategic-cycle trigger) ───────────────


@pytest.mark.asyncio
async def test_trigger_strategic_cycle_returns_429_on_lock_contention(
    client, db_session, mocker
):
    """POST /admin/projects/{id}/strategic-cycle must 429 on contention.
    Phase A1, was site directions.py:537.
    """
    from fastapi import HTTPException

    project = Project(name="A1 strategic cycle lock", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _add_admin_membership(db_session, project)

    async def _stub_lock(session, project_id):
        raise HTTPException(
            status_code=429,
            detail="held",
            headers={"Retry-After": "2"},
        )

    mocker.patch(
        "app.routers.directions.lock_project_or_429",
        new=AsyncMock(side_effect=_stub_lock),
    )
    resp = await client.post(
        f"/api/admin/projects/{project.id}/strategic-cycle",
        params={"phase": "design_doc"},
    )
    assert resp.status_code == 429, (
        f"Expected 429 on contention, got {resp.status_code}: {resp.text}"
    )


# ── A1.5 — task_lifecycle.py:408 (_has_active_strategic_task) ───────────────


@pytest.mark.asyncio
async def test_has_active_strategic_task_returns_true_when_project_locked(
    db_session, mocker
):
    """When the project row is held by a peer, _has_active_strategic_task
    must return True (fail-safe — defer this caller's spawn). Returning
    False under contention would risk both concurrent callers concluding
    "none active" and double-spawning. Phase A1, was site
    task_lifecycle.py:408.
    """
    from app.task_lifecycle import _has_active_strategic_task

    project = Project(name="A1 strategic lock", user_id="user-strategic")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    mock_exec, _state = _stub_first_select_returns_none(db_session)
    mocker.patch.object(db_session, "execute", new=mock_exec)

    result = await _has_active_strategic_task(project.id, db_session)
    assert result is True, (
        "Under contention _has_active_strategic_task must fail-safe to True "
        "so the caller skips spawn; got False."
    )


# ── A1.6 — task_lifecycle.py:1771 / 2232 / 2315 (rollup parent locking) ─────


@pytest.mark.asyncio
async def test_project_lock_helper_yields_none_under_contention(db_session, mocker):
    """The new project_lock context manager yields None when the row is
    held under skip_locked=True (instead of raising LockNotAvailable).
    This is the primitive used by all post-commit chain sites in
    task_lifecycle.py / task_completion.py / strategic_completion.py.
    """
    project = Project(name="A1 project_lock helper", user_id="user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    mock_exec, _state = _stub_first_select_returns_none(db_session)
    mocker.patch.object(db_session, "execute", new=mock_exec)

    async with project_lock(db_session, project.id, skip_locked=True) as p:
        assert p is None, "project_lock must yield None when row is held"


@pytest.mark.asyncio
async def test_lock_project_or_429_raises_429_under_contention(db_session, mocker):
    """Direct unit test for the helper used by all 4 handler-path sites.
    Phase A1, exercises the canonical 429 contract that admin_notifications
    and directions inherit.
    """
    from fastapi import HTTPException

    project = Project(name="A1 lock_project_or_429", user_id="user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    mock_exec, _state = _stub_first_select_returns_none(db_session)
    mocker.patch.object(db_session, "execute", new=mock_exec)

    with pytest.raises(HTTPException) as exc_info:
        await lock_project_or_429(db_session, project.id)
    assert exc_info.value.status_code == 429
    assert exc_info.value.headers.get("Retry-After") == "2"


@pytest.mark.asyncio
async def test_lock_project_or_429_raises_404_when_project_missing(db_session, mocker):
    """Distinguishes "row held" (429) from "row genuinely missing" (404).
    Without this, a missing-project bug would be silently retried as 429.
    """
    from fastapi import HTTPException

    missing_id = uuid.uuid4()
    with pytest.raises(HTTPException) as exc_info:
        await lock_project_or_429(db_session, missing_id)
    assert exc_info.value.status_code == 404


# ── A1.7 — task_completion.py:2693 (_handle_design_doc_completion) ──────────


@pytest.mark.asyncio
async def test_handle_design_doc_completion_defers_on_lock_contention(
    db_session, mocker
):
    """When the project row is held by a peer, _handle_design_doc_completion
    must surface a soft alert and return — never propagate
    LockNotAvailable as 500. Phase A1, was site task_completion.py:2693.
    """
    from app.task_completion import _handle_design_doc_completion

    project = Project(name="A1 design_doc lock", user_id="user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "design_doc_update"}],
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # Stub _create_strategic_alert so we observe the deferred-mutation alert
    create_alert = AsyncMock()
    mocker.patch("app.main._create_strategic_alert", new=create_alert)

    # Phase C3: the bounded-retry sleep now lives in task_locking, not
    # task_completion. Patch the canonical site so retries don't slow
    # the test.
    mocker.patch(
        "app.task_locking.asyncio.sleep",
        new=AsyncMock(return_value=None),
    )

    # Force every locking SELECT to return None (row "always" held).
    original_execute = db_session.execute

    class _EmptyResult:
        def scalars(self):
            return self

        def first(self):
            return None

        def all(self):
            return []

    async def _mock_execute(stmt, *args, **kwargs):
        try:
            stmt_str = str(stmt.compile(compile_kwargs={"literal_binds": False}))
        except Exception:
            stmt_str = str(stmt)
        if "FOR UPDATE" in stmt_str.upper():
            return _EmptyResult()
        return await original_execute(stmt, *args, **kwargs)

    mocker.patch.object(db_session, "execute", new=AsyncMock(side_effect=_mock_execute))

    # Must return without raising — never 500
    await _handle_design_doc_completion(task, "", db_session)

    # The contention path posts a soft alert and returns
    assert create_alert.await_count >= 1, "Expected deferred-mutation alert"


# ── A1.8 — strategic_completion.py:679 (_handle_strategic_completion) ───────


@pytest.mark.asyncio
async def test_handle_strategic_completion_defers_on_lock_contention(
    db_session, mocker
):
    """When the project row is held during the legacy fallback path,
    _handle_strategic_completion must alert + return rather than 500.
    Phase A1, was site strategic_completion.py:679. Phase C3 centralized
    the bounded-retry into ``task_locking.lock_project_for_strategic_transition``;
    this test patches ``app.task_locking.asyncio.sleep`` accordingly.
    """
    from app.strategic_completion import _handle_strategic_completion

    project = Project(name="A1 strategic completion lock", user_id="user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "strategic_review"}],
        status=TaskStatus.IN_PROGRESS,
        context={},  # not api-flagged → falls into legacy log-parse path
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    create_alert = AsyncMock()
    mocker.patch("app.main._create_strategic_alert", new=create_alert)

    # Phase C3: the bounded-retry sleep now lives in task_locking, not
    # strategic_completion. Patch the canonical site so retries don't
    # slow the test.
    mocker.patch(
        "app.task_locking.asyncio.sleep",
        new=AsyncMock(return_value=None),
    )

    original_execute = db_session.execute

    class _EmptyResult:
        def scalars(self):
            return self

        def first(self):
            return None

        def all(self):
            return []

    async def _mock_execute(stmt, *args, **kwargs):
        try:
            stmt_str = str(stmt.compile(compile_kwargs={"literal_binds": False}))
        except Exception:
            stmt_str = str(stmt)
        if "FOR UPDATE" in stmt_str.upper():
            return _EmptyResult()
        return await original_execute(stmt, *args, **kwargs)

    mocker.patch.object(db_session, "execute", new=AsyncMock(side_effect=_mock_execute))

    # Provide a parseable-JSON log so we reach the contention site.
    parseable_logs = '{"vision": {}, "direction_operations": []}'

    # Must return without raising
    await _handle_strategic_completion(task, parseable_logs, db_session)

    assert create_alert.await_count >= 1, (
        "Expected deferred-mutation alert under contention"
    )


# ── A1.9 — failure_classification.py:887 / 921 (residual upsert loop) ───────


@pytest.mark.asyncio
async def test_residual_upsert_skips_on_lock_contention(mocker, caplog):
    """When the residual-case row is held by a peer, the background upsert
    must skip rather than raise. The next residual hit will retry the
    increment — losing one count is far cheaper than a 500-quarantined
    instance. Phase A1, was site failure_classification.py:887,921.
    """
    import logging

    from app.failure_classification import _upsert_residual_case_async

    # Stub AsyncSessionLocal to return a session whose execute() emulates
    # contention: locking SELECTs return empty, but the non-locking probe
    # returns a row — telling the upsert "row exists but is held."
    project_id = uuid.uuid4()
    fingerprint = "test-fp"
    fake_row_id = uuid.uuid4()

    class _LockedResult:
        def scalars(self):
            return self

        def first(self):
            return None

        def scalar_one_or_none(self):
            return None

    class _ExistsResult:
        """Returned by the non-locking probe: row exists at fake_row_id."""

        def first(self):
            return (fake_row_id,)

        def scalars(self):
            return self

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _fake_begin():
        yield None

    class _FakeSession:
        async def execute(self, stmt, *args, **kwargs):
            try:
                stmt_str = str(stmt.compile(compile_kwargs={"literal_binds": False}))
            except Exception:
                stmt_str = str(stmt)
            if "FOR UPDATE" in stmt_str.upper():
                return _LockedResult()
            # Non-locking probe — return a row to simulate "row exists, held"
            return _ExistsResult()

        def add(self, _):
            pass

        def begin(self):
            return _fake_begin()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

    class _FakeSessionLocal:
        async def __aenter__(self):
            return _FakeSession()

        async def __aexit__(self, *args):
            return None

    # Patch AsyncSessionLocal — when called, returns our fake session-local
    mocker.patch(
        "app.database.AsyncSessionLocal",
        new=lambda: _FakeSessionLocal(),
    )

    from datetime import datetime, timezone

    now_dt = datetime.now(tz=timezone.utc)

    with caplog.at_level(logging.INFO, logger="app.failure_classification"):
        # Must complete without raising — the helper logs and returns.
        await _upsert_residual_case_async(project_id, fingerprint, now_dt, "sample")

    # Confirm the skip was logged (the helper logs at INFO when it skips)
    skip_logged = any(
        "held by peer" in rec.message or "skipping" in rec.message
        for rec in caplog.records
    )
    assert skip_logged, (
        f"Expected skip log under contention, got: {[r.message for r in caplog.records]}"
    )


# ── PR-spawn advisory lock ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_acquire_pr_spawn_lock_is_noop_on_sqlite(db_session):
    """The helper must be a no-op on non-Postgres dialects so SQLite-backed
    unit tests don't fail when they hit the spawn path. On Postgres it issues
    pg_advisory_xact_lock; on SQLite it returns silently without executing
    any SQL.
    """
    if db_session.bind.dialect.name == "postgresql":
        pytest.skip("This test only verifies the SQLite no-op fallback")
    # Should not raise
    await acquire_pr_spawn_lock(db_session, 12345)
    # Sanity: subsequent queries on the same session still work
    result = await db_session.execute(select(Project).limit(1))
    result.scalars().all()


@pytest.mark.skipif(not USE_POSTGRES, reason="advisory locks require PostgreSQL")
@pytest.mark.asyncio
async def test_acquire_pr_spawn_lock_serializes_concurrent_holders(db_session):
    """Two transactions trying to acquire the same PR's advisory lock must
    serialize: the second waits until the first commits/rolls back.

    Regression for the spawn race: without this lock, two concurrent
    _spawn_pipeline_task calls for PR #N can both pass the dedup SELECT
    before either commits, producing a duplicate review/fix task.
    """
    Session = sessionmaker(
        bind=db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    pr_number = 99001
    first_acquired = asyncio.Event()
    second_acquired_at: list[float] = []
    first_released_at: list[float] = []

    async def hold_lock_first():
        async with Session() as session:
            await acquire_pr_spawn_lock(session, pr_number)
            first_acquired.set()
            # Hold the lock briefly so the second caller's acquire is forced
            # to wait. 200ms is enough to detect serialization vs
            # interleaving without slowing the test perceptibly.
            await asyncio.sleep(0.2)
            first_released_at.append(asyncio.get_event_loop().time())
            await session.commit()  # release advisory_xact_lock

    async def acquire_second():
        await first_acquired.wait()
        async with Session() as session:
            await acquire_pr_spawn_lock(session, pr_number)
            second_acquired_at.append(asyncio.get_event_loop().time())
            await session.commit()

    await asyncio.gather(hold_lock_first(), acquire_second())

    assert first_released_at and second_acquired_at, (
        "both calls should record timestamps"
    )
    # Second acquire must land AFTER first release. Small epsilon for clock
    # jitter; outright failure would have second landing >100ms before first.
    assert second_acquired_at[0] >= first_released_at[0] - 0.005, (
        f"Second acquire ran before first released "
        f"(second={second_acquired_at[0]:.4f}, first_released={first_released_at[0]:.4f}) "
        f"— advisory lock failed to serialize concurrent holders"
    )


@pytest.mark.skipif(not USE_POSTGRES, reason="advisory locks require PostgreSQL")
@pytest.mark.asyncio
async def test_acquire_pr_spawn_lock_different_prs_dont_block(db_session):
    """Locks keyed on different PR numbers must NOT serialize each other —
    parallel spawns for unrelated PRs should run concurrently.
    """
    Session = sessionmaker(
        bind=db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    pr_a_acquired = asyncio.Event()

    async def hold_a():
        async with Session() as session:
            await acquire_pr_spawn_lock(session, 99100)
            pr_a_acquired.set()
            await asyncio.sleep(0.3)
            await session.commit()

    async def acquire_b():
        await pr_a_acquired.wait()
        # If different-PR locks were not independent, this would block
        # behind the 0.3s hold above. Bound the wait at 0.1s — well below
        # the 0.3s hold — so a spurious dependency surfaces as a timeout.
        async with Session() as session:
            await asyncio.wait_for(
                acquire_pr_spawn_lock(session, 99101),
                timeout=0.1,
            )
            await session.commit()

    await asyncio.gather(hold_a(), acquire_b())


# ── with_design_doc_locked ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_with_design_doc_locked_applies_mutation(db_session):
    """with_design_doc_locked must atomically apply the mutate_fn and preserve
    pre-existing keys so callers writing disjoint key sets don't clobber each other."""
    project = Project(name="design-doc-lock", user_id="user")
    project.design_doc = {"existing": "kept"}
    db_session.add(project)
    await db_session.commit()

    await with_design_doc_locked(
        db_session, project.id, lambda doc: doc.update({"added": "yes"})
    )
    await db_session.commit()
    await db_session.refresh(project)

    assert project.design_doc == {"existing": "kept", "added": "yes"}


@pytest.mark.skipif(not USE_POSTGRES, reason="row-lock concurrency requires PostgreSQL")
@pytest.mark.asyncio
async def test_with_design_doc_locked_serializes_concurrent_writers(db_session):
    """Two concurrent callers writing disjoint keys via with_design_doc_locked
    must both survive — the second must see the first's committed write."""
    project = Project(name="design-doc-concurrent", user_id="user")
    project.design_doc = {}
    db_session.add(project)
    await db_session.commit()

    Session = sessionmaker(
        bind=db_session.bind, class_=AsyncSession, expire_on_commit=False
    )

    async def write_key(key: str) -> None:
        async with Session() as s:
            await with_design_doc_locked(
                s, project.id, lambda doc: doc.update({key: "v"})
            )
            await s.commit()

    await asyncio.gather(write_key("key_a"), write_key("key_b"))

    async with Session() as verify:
        result = await verify.execute(select(Project).where(Project.id == project.id))
        doc = result.scalar_one().design_doc
    assert doc.get("key_a") == "v", f"key_a missing from {doc}"
    assert doc.get("key_b") == "v", f"key_b missing from {doc}"


# ── with_directions_locked ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_with_directions_locked_in_place_mutation(db_session):
    """A mutate_fn that edits the list in place and returns None must persist
    the edit and preserve pre-existing entries."""
    project = Project(name="dir-lock-inplace", user_id="user")
    project.directions = [{"id": "d-1", "status": "active"}]
    db_session.add(project)
    await db_session.commit()

    def _append(dirs: list) -> None:
        dirs.append({"id": "d-2", "status": "proposed"})

    await with_directions_locked(db_session, project.id, _append)
    await db_session.commit()
    await db_session.refresh(project)

    ids = [d["id"] for d in project.directions]
    assert ids == ["d-1", "d-2"], project.directions


@pytest.mark.asyncio
async def test_with_directions_locked_whole_array_overwrite(db_session):
    """A mutate_fn returning a replacement list must overwrite the array."""
    project = Project(name="dir-lock-replace", user_id="user")
    project.directions = [{"id": "d-1"}, {"id": "d-2"}]
    db_session.add(project)
    await db_session.commit()

    await with_directions_locked(db_session, project.id, lambda dirs: [{"id": "d-9"}])
    await db_session.commit()
    await db_session.refresh(project)

    assert project.directions == [{"id": "d-9"}]


@pytest.mark.skipif(not USE_POSTGRES, reason="row-lock concurrency requires PostgreSQL")
@pytest.mark.asyncio
async def test_with_directions_locked_serializes_concurrent_appends(db_session):
    """Two concurrent appends via with_directions_locked must both survive —
    the second writer must see the first's committed append (no lost update)."""
    project = Project(name="dir-lock-concurrent", user_id="user")
    project.directions = []
    db_session.add(project)
    await db_session.commit()

    Session = sessionmaker(
        bind=db_session.bind, class_=AsyncSession, expire_on_commit=False
    )

    async def append_dir(dir_id: str) -> None:
        async with Session() as s:
            await with_directions_locked(
                s, project.id, lambda dirs: dirs.append({"id": dir_id})
            )
            await s.commit()

    await asyncio.gather(append_dir("d-a"), append_dir("d-b"))

    async with Session() as verify:
        result = await verify.execute(select(Project).where(Project.id == project.id))
        dirs = result.scalar_one().directions
    ids = {d["id"] for d in dirs}
    assert ids == {"d-a", "d-b"}, f"lost update: {dirs}"


@pytest.mark.skipif(not USE_POSTGRES, reason="row-lock concurrency requires PostgreSQL")
@pytest.mark.asyncio
async def test_expire_stale_proposals_serializes_with_concurrent_append(db_session):
    """The _expire_stale_proposals cron now reads project.directions under the
    canonical project row lock. An interleaved direction append via
    with_directions_locked must survive AND the stale proposal must still expire
    — before the lock, the cron's whole-array write-back could clobber the
    concurrent append (lost update)."""
    from datetime import timedelta

    from app.anomaly_detection import _expire_stale_proposals
    from app.time_utils import _utcnow

    old_iso = (_utcnow() - timedelta(days=40)).isoformat()
    project = Project(name="expire-lock-concurrent", user_id="user")
    project.directions = [{"id": "d-old", "status": "proposed", "proposed_at": old_iso}]
    db_session.add(project)
    await db_session.commit()

    Session = sessionmaker(
        bind=db_session.bind, class_=AsyncSession, expire_on_commit=False
    )

    async def expire():
        async with Session() as s:
            await _expire_stale_proposals(s)  # commits internally

    async def append():
        async with Session() as s:
            await with_directions_locked(
                s,
                project.id,
                lambda dirs: dirs.append({"id": "d-new", "status": "active"}),
            )
            await s.commit()

    await asyncio.gather(expire(), append())

    async with Session() as verify:
        result = await verify.execute(select(Project).where(Project.id == project.id))
        dirs = result.scalar_one().directions
    by_id = {d["id"]: d for d in dirs}
    assert by_id.get("d-new", {}).get("status") == "active", (
        f"lost concurrent append: {dirs}"
    )
    assert by_id.get("d-old", {}).get("status") == "expired", (
        f"stale-proposal expiry not applied: {dirs}"
    )


# ── with_vision_locked ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_with_vision_locked_applies_mutation(db_session):
    """with_vision_locked must atomically apply the mutate_fn and preserve
    pre-existing keys so callers writing disjoint key sets don't clobber each other."""
    project = Project(name="vision-lock", user_id="user")
    project.vision = {"existing": "kept"}
    db_session.add(project)
    await db_session.commit()

    await with_vision_locked(
        db_session, project.id, lambda v: v.update({"added": "yes"})
    )
    await db_session.commit()
    await db_session.refresh(project)

    assert project.vision == {"existing": "kept", "added": "yes"}


@pytest.mark.skipif(not USE_POSTGRES, reason="row-lock concurrency requires PostgreSQL")
@pytest.mark.asyncio
async def test_with_vision_locked_serializes_concurrent_writers(db_session):
    """Two concurrent callers writing disjoint keys via with_vision_locked
    must both survive — the second must see the first's committed write."""
    project = Project(name="vision-lock-concurrent", user_id="user")
    project.vision = {}
    db_session.add(project)
    await db_session.commit()

    Session = sessionmaker(
        bind=db_session.bind, class_=AsyncSession, expire_on_commit=False
    )

    async def write_key(key: str) -> None:
        async with Session() as s:
            await with_vision_locked(s, project.id, lambda v: v.update({key: "v"}))
            await s.commit()

    await asyncio.gather(write_key("key_a"), write_key("key_b"))

    async with Session() as verify:
        result = await verify.execute(select(Project).where(Project.id == project.id))
        vision = result.scalar_one().vision
    assert vision.get("key_a") == "v", f"key_a missing from {vision}"
    assert vision.get("key_b") == "v", f"key_b missing from {vision}"
