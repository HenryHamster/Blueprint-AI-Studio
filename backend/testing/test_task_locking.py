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
    DESIGN_DOC_MAX_BYTES,
    DesignDocSizeLimitExceeded,
    acquire_pr_spawn_lock,
    lock_project_or_429,
    project_lock,
    task_lock,
    with_design_doc_locked,
    with_directions_locked,
    with_task_context_locked,
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
    mocker.patch("app.alert_dispatch._create_strategic_alert", new=create_alert)

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
    mocker.patch("app.alert_dispatch._create_strategic_alert", new=create_alert)

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
    instance. Phase A1, was site failure_classification.py:887,921
    (residual subsystem since extracted to failure_residual.py).
    """
    import logging

    from app.failure_residual import _upsert_residual_case_async

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

    with caplog.at_level(logging.INFO, logger="app.failure_residual"):
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
    """The _expire_stale_proposals cron reads project.directions under the
    canonical project row lock with skip_locked=True (PR #6727 — a background
    sweep must not block on lock_timeout). Two guarantees under a concurrent
    with_directions_locked append:
      1. the append is NEVER lost — the cron either serializes behind it (reads
         fresh under the lock) or skips the contended project entirely, so it
         can't clobber the append;
      2. the stale-proposal expiry is deferred-not-dropped — if the cron loses
         the lock race it skips this run, and a subsequent uncontended run
         applies the expiry."""
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

    async def _load_dirs():
        async with Session() as verify:
            result = await verify.execute(
                select(Project).where(Project.id == project.id)
            )
            return {d["id"]: d for d in result.scalar_one().directions}

    # Invariant 1: the concurrent append is never lost, regardless of which side
    # won the lock race — the cron skips a contended project rather than
    # clobbering it (and reads fresh under the lock when it does acquire).
    by_id = await _load_dirs()
    assert by_id.get("d-new", {}).get("status") == "active", (
        f"lost concurrent append: {by_id}"
    )

    # Invariant 2: the expiry is deferred-not-dropped. If the cron lost the race
    # it skipped d-old this run (skip_locked=True); a follow-up uncontended run
    # must apply it. (If the cron won the first race, d-old is already expired
    # and this run is a no-op.)
    await expire()
    by_id = await _load_dirs()
    assert by_id.get("d-old", {}).get("status") == "expired", (
        f"stale-proposal expiry not applied even after an uncontended run: {by_id}"
    )
    assert by_id.get("d-new", {}).get("status") == "active", (
        f"concurrent append lost after follow-up expiry run: {by_id}"
    )


@pytest.mark.skipif(not USE_POSTGRES, reason="row-lock concurrency requires PostgreSQL")
@pytest.mark.asyncio
async def test_remediation_direction_stamp_serializes_with_concurrent_writer(
    db_session,
):
    """The direction-stamp in auto_remediation now uses with_directions_locked.
    A concurrent writer's mutation must survive alongside the stamp fields —
    no lost update when the remediation task insert races a directions write."""
    from app.task_completion import _stamp_remediation_spawn_on_direction

    dir_id = "d-target"
    project = Project(name="remediation-stamp-lock", user_id="user")
    project.directions = [{"id": dir_id, "status": "active"}]
    db_session.add(project)
    await db_session.commit()

    stamp_task = Task(
        project_id=project.id,
        title="auto-fix: CI failure",
        status=TaskStatus.QUEUED,
        payloads=[{"type": "create"}],
    )
    db_session.add(stamp_task)
    await db_session.commit()

    Session = sessionmaker(
        bind=db_session.bind, class_=AsyncSession, expire_on_commit=False
    )

    async def stamp_direction() -> None:
        async with Session() as s:

            def _mutate(directions: list) -> None:
                target = next((d for d in directions if d.get("id") == dir_id), None)
                if target is not None:
                    _stamp_remediation_spawn_on_direction(target, stamp_task)

            await with_directions_locked(s, project.id, _mutate)
            await s.commit()

    async def concurrent_writer() -> None:
        async with Session() as s:
            await with_directions_locked(
                s,
                project.id,
                lambda dirs: next(
                    (d for d in dirs if d.get("id") == dir_id), {}
                ).update({"concurrent_key": "set"}),
            )
            await s.commit()

    await asyncio.gather(stamp_direction(), concurrent_writer())

    async with Session() as verify:
        result = await verify.execute(select(Project).where(Project.id == project.id))
        dirs = result.scalar_one().directions
    by_id = {d["id"]: d for d in dirs}
    target = by_id.get(dir_id, {})
    assert target.get("last_remediation_task_id") == str(stamp_task.id), (
        f"stamp fields lost in concurrent write: {target}"
    )
    assert target.get("concurrent_key") == "set", (
        f"concurrent writer's update lost: {target}"
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
async def test_concurrent_research_outcomes_preserve_both_directions(db_session):
    """Two concurrent _update_direction_research_outcomes calls for DIFFERENT
    directions of the same project must both persist — no lost update."""
    from app.direction_outcomes import _update_direction_research_outcomes

    project = Project(name="research-outcome-lock", user_id="user")
    project.directions = []
    db_session.add(project)
    await db_session.commit()

    research_task_a = Task(
        project_id=project.id,
        title="research A",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "research"}],
    )
    research_task_b = Task(
        project_id=project.id,
        title="research B",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "research"}],
    )
    db_session.add(research_task_a)
    db_session.add(research_task_b)
    await db_session.commit()

    child_a = Task(
        project_id=project.id,
        parent_task_id=research_task_a.id,
        title="child A",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
        context={"pr_merged": True},
    )
    child_b = Task(
        project_id=project.id,
        parent_task_id=research_task_b.id,
        title="child B",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
        context={"pr_merged": True},
    )
    db_session.add(child_a)
    db_session.add(child_b)
    await db_session.commit()

    dir_id_a = "dir-ro-a"
    dir_id_b = "dir-ro-b"
    project.directions = [
        {
            "id": dir_id_a,
            "status": "active",
            "research_history": [
                {"task_id": str(research_task_a.id), "tasks_created": 1}
            ],
        },
        {
            "id": dir_id_b,
            "status": "active",
            "research_history": [
                {"task_id": str(research_task_b.id), "tasks_created": 1}
            ],
        },
    ]
    db_session.add(project)
    await db_session.commit()

    Session = sessionmaker(
        bind=db_session.bind, class_=AsyncSession, expire_on_commit=False
    )

    async def run_outcomes(direction_id: str, research_task_id: str) -> None:
        async with Session() as s:
            await _update_direction_research_outcomes(
                project.id, direction_id, research_task_id, s
            )

    await asyncio.gather(
        run_outcomes(dir_id_a, str(research_task_a.id)),
        run_outcomes(dir_id_b, str(research_task_b.id)),
    )

    async with Session() as verify:
        result = await verify.execute(select(Project).where(Project.id == project.id))
        dirs = result.scalar_one().directions

    by_id = {d["id"]: d for d in dirs}
    entry_a = by_id[dir_id_a]["research_history"][0]
    entry_b = by_id[dir_id_b]["research_history"][0]
    assert entry_a.get("tasks_completed") == 1, f"dir-a lost update: {entry_a}"
    assert entry_b.get("tasks_completed") == 1, f"dir-b lost update: {entry_b}"


@pytest.mark.asyncio
async def test_with_directions_locked_refreshes_stale_identity_map(db_session):
    """Regression: project_lock must use populate_existing=True so a Project
    already in the session's identity map gets refreshed from the DB before
    mutate_fn runs, not the stale cached snapshot from before the lock.

    Scenario: session A loads the project (caching directions=[old]); session B
    commits directions=[new]; session A calls with_directions_locked — without
    populate_existing the mutate_fn would see [old] and overwrite [new].
    """
    project = Project(name="populate-existing-test", user_id="user")
    project.directions = [{"id": "d-original", "status": "active"}]
    db_session.add(project)
    await db_session.commit()

    # Pull the project into db_session's identity map.
    result = await db_session.execute(select(Project).where(Project.id == project.id))
    cached = result.scalar_one()
    assert cached.directions == [{"id": "d-original", "status": "active"}]

    # Commit a direction change from a second session (simulates another writer).
    Session = sessionmaker(
        bind=db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with Session() as second_session:
        r2 = await second_session.execute(
            select(Project).where(Project.id == project.id)
        )
        proj2 = r2.scalar_one()
        from sqlalchemy.orm.attributes import flag_modified as _flag_modified

        proj2.directions = [{"id": "d-updated", "status": "active"}]
        _flag_modified(proj2, "directions")
        second_session.add(proj2)
        await second_session.commit()

    # with_directions_locked must refresh the identity-mapped instance via
    # populate_existing=True and pass the committed [d-updated] to mutate_fn.
    seen_directions: list = []

    def _capture(dirs: list) -> None:
        seen_directions.extend(dirs)

    await with_directions_locked(db_session, project.id, _capture)
    await db_session.commit()

    ids_seen = [d["id"] for d in seen_directions]
    assert "d-updated" in ids_seen, (
        f"with_directions_locked passed stale cached directions to mutate_fn: "
        f"{seen_directions!r} — populate_existing=True is required on the lock SELECT"
    )
    assert "d-original" not in ids_seen, (
        f"stale direction 'd-original' was not evicted by populate_existing=True: "
        f"{seen_directions!r}"
    )


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


# ── with_task_context_locked ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_with_task_context_locked_preserves_disjoint_keys(db_session):
    """with_task_context_locked must atomically apply the mutate_fn and PRESERVE
    pre-existing keys owned by another writer — the lost-update property.

    Regression for finding 8ff6c48f: a heartbeat mirror writes
    ``_exam_ledger`` / ``message_cursor``
    into ``task.context`` while the strategic-API paths in admin_agent_ops.py
    write ``_strategic_api_called`` — each via a whole-column overwrite. Loading
    the context INSIDE the FOR UPDATE lock and merging must keep the other
    side's key rather than clobbering it.
    """
    project = Project(name="task-ctx-lock", user_id="user")
    db_session.add(project)
    await db_session.commit()

    # Simulate a strategic-API write already present on the row.
    task = Task(
        project_id=project.id,
        title="context lock task",
        status=TaskStatus.IN_PROGRESS,
        context={"_strategic_api_called": True},
    )
    db_session.add(task)
    await db_session.commit()

    # The heartbeat mirror re-applies only its own durable restart keys.
    def _mirror(ctx: dict) -> None:
        ctx["_exam_ledger"] = "ledger-blob-v1"
        ctx["message_cursor"] = "cursor-42"

    applied = await with_task_context_locked(db_session, task.id, _mirror)
    await db_session.commit()
    await db_session.refresh(task)

    assert applied is True
    # Both the pre-existing strategic-API flag AND the mirrored keys survive.
    # Use subset check: the model may add framework keys (e.g. provides_capability)
    # that the test doesn't control.
    expected_subset = {
        "_strategic_api_called": True,
        "_exam_ledger": "ledger-blob-v1",
        "message_cursor": "cursor-42",
    }
    assert expected_subset.items() <= task.context.items(), (
        f"lost update on task.context: {task.context}"
    )


@pytest.mark.asyncio
async def test_with_task_context_locked_returns_false_when_task_missing(db_session):
    """A missing task row yields False (no crash), mirroring the sibling helpers."""
    result = await with_task_context_locked(
        db_session, uuid.uuid4(), lambda ctx: ctx.update({"x": 1})
    )
    assert result is False


@pytest.mark.skipif(not USE_POSTGRES, reason="row-lock concurrency requires PostgreSQL")
@pytest.mark.asyncio
async def test_with_task_context_locked_serializes_concurrent_writers(db_session):
    """Two concurrent callers writing disjoint keys via with_task_context_locked
    must both survive — the second must see the first's committed write.

    This is the exact race in finding 8ff6c48f: a heartbeat ledger mirror
    (``_exam_ledger``) and a strategic-API write (``_strategic_api_called``)
    targeting the SAME task row. Without the FOR UPDATE re-read inside the
    helper, whichever commits second drops the other's key."""
    project = Project(name="task-ctx-concurrent", user_id="user")
    db_session.add(project)
    await db_session.commit()

    task = Task(
        project_id=project.id,
        title="contended context task",
        status=TaskStatus.IN_PROGRESS,
        context={},
    )
    db_session.add(task)
    await db_session.commit()

    Session = sessionmaker(
        bind=db_session.bind, class_=AsyncSession, expire_on_commit=False
    )

    async def write_key(key: str) -> None:
        async with Session() as s:
            await with_task_context_locked(s, task.id, lambda ctx: ctx.update({key: "v"}))
            await s.commit()

    await asyncio.gather(
        write_key("_exam_ledger"),
        write_key("_strategic_api_called"),
    )

    async with Session() as verify:
        result = await verify.execute(select(Task).where(Task.id == task.id))
        ctx = result.scalar_one().context
    assert ctx.get("_exam_ledger") == "v", f"_exam_ledger missing from {ctx}"
    assert ctx.get("_strategic_api_called") == "v", (
        f"_strategic_api_called missing from {ctx}"
    )


# ── post_auto_fix_result_message concurrent writers ─────────────────────────


@pytest.mark.skipif(not USE_POSTGRES, reason="row-lock concurrency requires PostgreSQL")
@pytest.mark.asyncio
async def test_post_auto_fix_result_message_serializes_concurrent_writers(db_session):
    """Two concurrent post_auto_fix_result_message calls writing disjoint branches
    via with_design_doc_locked must both persist — no lost update."""
    from app.notification_dispatch import post_auto_fix_result_message

    project = Project(name="autofix-concurrent", user_id="user")
    project.design_doc = {}
    db_session.add(project)
    await db_session.commit()

    Session = sessionmaker(
        bind=db_session.bind, class_=AsyncSession, expire_on_commit=False
    )

    async def write_branch(branch: str) -> None:
        async with Session() as s:
            await post_auto_fix_result_message(
                s, project.id, branch=branch, result="failed"
            )
            await s.commit()

    await asyncio.gather(write_branch("main"), write_branch("feat/x"))

    async with Session() as verify:
        result = await verify.execute(select(Project).where(Project.id == project.id))
        doc = result.scalar_one().design_doc
    ar = doc.get("_auto_remediation", {})
    counts = ar.get("branch_failure_counts", {})
    assert counts.get("main") == 1, f"main branch count missing from {doc}"
    assert counts.get("feat/x") == 1, f"feat/x branch count missing from {doc}"


@pytest.mark.skipif(not USE_POSTGRES, reason="row-lock concurrency requires PostgreSQL")
@pytest.mark.asyncio
async def test_auto_remediation_recovery_serializes_concurrent_writers(db_session):
    """Two concurrent auto-remediation suspension recoveries for disjoint branches
    must both persist — the migrated _try_auto_recover_suspension write goes through
    with_design_doc_locked, so neither recovery may clobber the other (no lost update)."""
    from datetime import timedelta

    from app.auto_remediation import _try_auto_recover_suspension
    from app.time_utils import _utcnow

    # Both branches suspended long enough ago that their cooldown has elapsed,
    # so each call will recover its branch and bump its recovery-attempt count.
    long_ago = (_utcnow() - timedelta(days=30)).isoformat()
    ar_config = {
        "suspended_branches": ["feat/a", "feat/b"],
        "branch_failure_counts": {"feat/a": 5, "feat/b": 5},
        "suspension_metadata": {
            "feat/a": {"suspended_at": long_ago, "recovery_attempts": 0},
            "feat/b": {"suspended_at": long_ago, "recovery_attempts": 0},
        },
    }
    project = Project(name="auto-remediation-concurrent", user_id="user")
    project.design_doc = {"_auto_remediation": ar_config}
    db_session.add(project)
    await db_session.commit()

    Session = sessionmaker(
        bind=db_session.bind, class_=AsyncSession, expire_on_commit=False
    )

    async def recover(branch: str) -> bool:
        async with Session() as s:
            recovered = await _try_auto_recover_suspension(
                project_id=project.id,
                branch=branch,
                config=ar_config,
                session=s,
            )
            await s.commit()
            return recovered

    results = await asyncio.gather(recover("feat/a"), recover("feat/b"))
    assert all(results), f"both branches should report recovery, got {results}"

    async with Session() as verify:
        result = await verify.execute(select(Project).where(Project.id == project.id))
        doc = result.scalar_one().design_doc
    ar = doc.get("_auto_remediation", {})
    # Both branches must be cleared from the suspended list — if one writer
    # clobbered the other, one branch would survive in suspended_branches.
    assert ar.get("suspended_branches") == [], (
        f"both branches should be recovered, got {ar.get('suspended_branches')}"
    )
    meta = ar.get("suspension_metadata", {})
    assert meta.get("feat/a", {}).get("recovery_attempts") == 1, (
        f"feat/a recovery attempt lost from {doc}"
    )
    assert meta.get("feat/b", {}).get("recovery_attempts") == 1, (
        f"feat/b recovery attempt lost from {doc}"
    )


# ── design_doc 500KB ceiling enforcement ────────────────────────────────────


@pytest.mark.asyncio
async def test_background_write_rejects_growth_over_ceiling_but_allows_shrink(
    db_session,
):
    """Core acceptance test: with_design_doc_locked must reject a mutate_fn
    that would grow design_doc past DESIGN_DOC_MAX_BYTES, but must allow a
    shrinking write even when the doc is already over the ceiling.

    Growth over ceiling → returns False, doc unchanged.
    Shrink from over-ceiling → returns True, doc is written (recovery path).
    """
    # Build a doc that is just under the ceiling so a small addition pushes it over.
    padding = "x" * (DESIGN_DOC_MAX_BYTES - 30)
    project = Project(name="ceiling-test", user_id="user")
    project.design_doc = {"pad": padding}
    db_session.add(project)
    await db_session.commit()

    # Attempt a growing write that pushes the doc over the ceiling.
    def _grow(doc: dict) -> None:
        doc["new_key"] = "y" * 100  # adds ~100 bytes, crossing the ceiling

    result = await with_design_doc_locked(db_session, project.id, _grow)
    await db_session.commit()
    await db_session.refresh(project)

    assert result is False, "growing write over ceiling must return False"
    assert "new_key" not in project.design_doc, "growing write must not be persisted"

    # Now set the doc to over-ceiling so we can verify the shrink path.
    bloated_padding = "x" * (DESIGN_DOC_MAX_BYTES + 100)
    project.design_doc = {"pad": bloated_padding}
    from sqlalchemy.orm.attributes import flag_modified as _flag_modified

    _flag_modified(project, "design_doc")
    db_session.add(project)
    await db_session.commit()

    # A shrinking write (removes the bloated padding) must succeed even though
    # the doc is currently over the ceiling — this is the recovery path.
    def _shrink(doc: dict) -> None:
        doc["pad"] = "small"

    result = await with_design_doc_locked(db_session, project.id, _shrink)
    await db_session.commit()
    await db_session.refresh(project)

    assert result is True, "shrinking write from over-ceiling must return True"
    assert project.design_doc.get("pad") == "small", "shrink must be persisted"


@pytest.mark.asyncio
async def test_with_design_doc_locked_allows_write_at_ceiling_boundary(db_session):
    """A write that lands exactly at DESIGN_DOC_MAX_BYTES must succeed."""
    import json as _json

    project = Project(name="ceiling-boundary", user_id="user")
    project.design_doc = {}
    db_session.add(project)
    await db_session.commit()

    # Compute padding so final doc JSON is exactly DESIGN_DOC_MAX_BYTES bytes.
    # The final doc will be {"pad": "<padding>"}.
    wrapper_overhead = len(_json.dumps({"pad": ""}))
    padding = "x" * (DESIGN_DOC_MAX_BYTES - wrapper_overhead)

    def _set_pad(doc: dict) -> None:
        doc["pad"] = padding

    result = await with_design_doc_locked(db_session, project.id, _set_pad)
    await db_session.commit()
    await db_session.refresh(project)

    assert result is True, "write at exact ceiling must be allowed"
    final_size = len(_json.dumps(project.design_doc))
    assert final_size == DESIGN_DOC_MAX_BYTES, (
        f"expected exactly {DESIGN_DOC_MAX_BYTES} bytes, got {final_size}"
    )


@pytest.mark.asyncio
async def test_with_design_doc_locked_skips_when_doc_already_over_ceiling_and_grows(
    db_session,
):
    """When the existing doc is already over the ceiling and mutate_fn would
    grow it further, the write must be skipped (False returned)."""
    bloated = "x" * (DESIGN_DOC_MAX_BYTES + 50)
    project = Project(name="ceiling-already-over", user_id="user")
    project.design_doc = {"pad": bloated}
    from sqlalchemy.orm.attributes import flag_modified as _flag_modified

    _flag_modified(project, "design_doc")
    db_session.add(project)
    await db_session.commit()

    def _grow_more(doc: dict) -> None:
        doc["extra"] = "y" * 100

    result = await with_design_doc_locked(db_session, project.id, _grow_more)
    await db_session.commit()
    await db_session.refresh(project)

    assert result is False, "growing an already-over-ceiling doc must return False"
    assert "extra" not in project.design_doc, "growth must not be persisted"


@pytest.mark.asyncio
async def test_with_design_doc_locked_reserved_blobs_excluded_from_ceiling(
    db_session,
):
    """Regression: when code-owned reserved blobs push total design_doc past
    DESIGN_DOC_MAX_BYTES, agent-owned writes via with_design_doc_locked must
    still succeed as long as the AGENT-OWNED content stays under the ceiling.

    Steps:
    1. Seed design_doc with reserved keys (efficiency_baseline, lane_baselines,
       etc.) whose SERIALIZED SIZE alone exceeds DESIGN_DOC_MAX_BYTES.
    2. Write a small agent-owned key — must succeed (reserved blobs excluded
       from the size check).
    3. Write an agent-owned key that would push agent-owned content over the
       ceiling — must be rejected.
    """
    import json as _json

    # Build large reserved blobs: a single 600KB string under a reserved key.
    huge_blob = "z" * 600_000
    project = Project(name="reserved-ceiling-regression", user_id="user")
    project.design_doc = {
        "efficiency_baseline": huge_blob,
        "lane_baselines": huge_blob,
    }
    from sqlalchemy.orm.attributes import flag_modified as _flag_modified

    _flag_modified(project, "design_doc")
    db_session.add(project)
    await db_session.commit()

    # Verify the total stored doc is indeed over the ceiling.
    assert len(_json.dumps(project.design_doc)) > DESIGN_DOC_MAX_BYTES, (
        "test setup must create a doc larger than the ceiling"
    )

    # Step 2: write a small agent-owned key — must succeed (reserved blobs
    # are excluded from the size check).
    def _write_small_agent_key(doc: dict) -> None:
        doc["agent_key"] = "small_value"

    result = await with_design_doc_locked(db_session, project.id, _write_small_agent_key)
    await db_session.commit()
    await db_session.refresh(project)

    assert result is True, (
        "agent-owned write must succeed even though total doc exceeds ceiling"
    )
    assert project.design_doc.get("agent_key") == "small_value", (
        "agent-owned write must be persisted"
    )
    # Reserved blobs must be preserved (mutate_fn only added a new key).
    assert project.design_doc.get("efficiency_baseline") == huge_blob
    assert project.design_doc.get("lane_baselines") == huge_blob

    # Step 3: grow agent-owned content over the ceiling — must be rejected.
    def _grow_agent_over_ceiling(doc: dict) -> None:
        doc["another_agent_key"] = "x" * DESIGN_DOC_MAX_BYTES

    result2 = await with_design_doc_locked(
        db_session, project.id, _grow_agent_over_ceiling
    )
    await db_session.commit()
    await db_session.refresh(project)

    assert result2 is False, (
        "agent-owned write that exceeds ceiling must be rejected"
    )
    assert "another_agent_key" not in project.design_doc, (
        "over-ceiling agent write must not be persisted"
    )


# ── load_columns projection (perf finding 1) ────────────────────────────────


@pytest.mark.asyncio
async def test_lock_project_or_429_load_columns_generates_projected_sql(
    db_session, mocker
):
    """lock_project_or_429 with load_columns issues a SELECT that includes only
    the specified columns (verified by inspecting the SQL statement)."""
    from app.task_locking import lock_project_or_429

    project = Project(
        name="load-columns-test",
        user_id="user-lc",
        design_doc={"key": "value"},
        directions=[{"id": "d1"}],
    )
    db_session.add(project)
    await db_session.commit()

    executed_stmts: list[str] = []
    original_execute = db_session.execute

    async def _capture(stmt, *args, **kwargs):
        try:
            compiled = str(stmt.compile(compile_kwargs={"literal_binds": False}))
        except Exception:
            compiled = str(stmt)
        executed_stmts.append(compiled)
        return await original_execute(stmt, *args, **kwargs)

    mocker.patch.object(db_session, "execute", new=AsyncMock(side_effect=_capture))

    locked = await lock_project_or_429(
        db_session, project.id, load_columns=[Project.id, Project.design_doc]
    )
    assert str(locked.id) == str(project.id)

    locking_stmts = [s for s in executed_stmts if "FOR UPDATE" in s.upper()]
    assert locking_stmts, "expected at least one FOR UPDATE statement"
    locking_sql = locking_stmts[0]
    assert "design_doc" in locking_sql.lower(), (
        "projected SELECT must include design_doc"
    )
    assert "directions" not in locking_sql.lower(), (
        "projected SELECT must NOT include directions (excluded by load_columns)"
    )


@pytest.mark.asyncio
async def test_lock_project_or_429_default_loads_all_columns(db_session):
    """lock_project_or_429 without load_columns loads all columns (backward compat)."""
    from app.task_locking import lock_project_or_429

    project = Project(
        name="load-all-test",
        user_id="user-all",
        design_doc={"x": 1},
        directions=[{"id": "d2"}],
    )
    db_session.add(project)
    await db_session.commit()

    locked = await lock_project_or_429(db_session, project.id)
    # All columns must be accessible without error
    assert str(locked.id) == str(project.id)
    assert locked.design_doc == {"x": 1}
    assert locked.directions == [{"id": "d2"}]


@pytest.mark.asyncio
async def test_with_design_doc_locked_does_not_load_directions(db_session, mocker):
    """with_design_doc_locked must issue a projected SELECT that loads only
    id and design_doc — directions and other bulky columns are excluded.

    This verifies the load_columns=[Project.id, Project.design_doc] projection
    passes through to project_lock correctly.
    """
    executed_stmts: list[str] = []
    original_execute = db_session.execute

    async def _capture(stmt, *args, **kwargs):
        try:
            compiled = str(stmt.compile(compile_kwargs={"literal_binds": False}))
        except Exception:
            compiled = str(stmt)
        executed_stmts.append(compiled)
        return await original_execute(stmt, *args, **kwargs)

    mocker.patch.object(db_session, "execute", new=AsyncMock(side_effect=_capture))

    project = Project(
        name="dsl-projection-test",
        user_id="user-proj",
        design_doc={"z": 99},
        directions=[{"id": "d3"}],
    )
    db_session.add(project)
    await db_session.commit()
    executed_stmts.clear()

    def _touch(doc: dict) -> None:
        doc["touched"] = True

    await with_design_doc_locked(db_session, project.id, _touch)

    # The FOR UPDATE SELECT must mention design_doc but NOT directions.
    locking_stmts = [s for s in executed_stmts if "FOR UPDATE" in s.upper()]
    assert locking_stmts, "expected at least one FOR UPDATE statement"
    locking_sql = locking_stmts[0]
    assert "design_doc" in locking_sql.lower(), (
        "projected SELECT must include design_doc column"
    )
    assert "directions" not in locking_sql.lower(), (
        "projected SELECT must NOT include directions (load_columns projection)"
    )


# ── backfill_file_to_system_index (perf findings 2 & 3) ────────────────────


@pytest.mark.asyncio
async def test_backfill_file_to_system_index_paginates_in_batches(db_session, mocker):
    """backfill_file_to_system_index must process all projects via cursor
    pagination (no single unbounded query that loads all IDs at once).

    We seed 3 projects and verify all are processed and the returned count
    reflects actual updates.
    """
    from app.routers.admin_agent_ops import backfill_file_to_system_index

    projects = []
    for i in range(3):
        p = Project(
            name=f"backfill-batch-{i}",
            user_id="user-bf",
            design_doc={
                f"systems/sys-{i}": {
                    "key_paths": [f"src/file_{i}.py"],
                }
            },
        )
        db_session.add(p)
        projects.append(p)
    await db_session.commit()

    count = await backfill_file_to_system_index(db_session)
    await db_session.commit()

    assert count == 3, f"expected 3 updates, got {count}"

    for p in projects:
        await db_session.refresh(p)
        fts = p.design_doc.get("_file_to_system")
        assert fts is not None, f"_file_to_system missing on project {p.id}"


@pytest.mark.asyncio
async def test_backfill_file_to_system_index_skips_unchanged(db_session):
    """Projects whose _file_to_system index is already correct must NOT count
    as updated — the idempotency guard (current == rebuilt) must fire.
    """
    from app.routers.admin_agent_ops import backfill_file_to_system_index

    p = Project(
        name="backfill-idempotent",
        user_id="user-bi",
        design_doc={
            "systems/comp": {"key_paths": ["a/b.py"]},
            "_file_to_system": {"a/b.py": "comp"},
        },
    )
    db_session.add(p)
    await db_session.commit()

    count = await backfill_file_to_system_index(db_session)
    await db_session.commit()

    assert count == 0, (
        f"expected 0 updates for already-correct index, got {count}"
    )


@pytest.mark.asyncio
async def test_backfill_file_to_system_index_empty_design_doc_skipped(db_session):
    """Projects with an empty or null design_doc must be skipped entirely."""
    from app.routers.admin_agent_ops import backfill_file_to_system_index

    p_null = Project(name="backfill-null", user_id="user-bn", design_doc={})
    db_session.add(p_null)
    await db_session.commit()

    count = await backfill_file_to_system_index(db_session)
    await db_session.commit()

    assert count == 0, f"expected 0 for empty design_doc, got {count}"


def test_reserved_design_doc_keys_contains_cochange_and_audit_keys():
    """Regression: cochange_index and cochange_applied_prs (written by
    direction_outcomes.py, ~38KB each) must be reserved so they don't
    consume the agent-write 500KB budget.  Also covers the full set of
    code-written analytics keys discovered in the 2026-07 audit.
    """
    from app.task_locking import _RESERVED_DESIGN_DOC_KEYS

    # Primary fix: cochange keys written by direction_outcomes.py
    assert "cochange_index" in _RESERVED_DESIGN_DOC_KEYS
    assert "cochange_applied_prs" in _RESERVED_DESIGN_DOC_KEYS

    # Audit additions: background analytics/sweep keys
    audit_keys = {
        "repo_overview",
        "review_effectiveness",
        "playbook_deleted_keys",
        "fresh_reviewer_trend",
        "edges_trend",
        "sr_reviewer_disagreements",
        "sr_catch_rate_trends_alert",
        "sr_catch_rate_floor_alert",
        "sr_quadrant_alert",
        "sr_pass_engagement_alert",
        "sr_regression_risk_test_signal_trends",
        "sr_false_attestation_trends",
        "sr_blind_spot_signal_last_computed_at",
        "fix_loop_divergence_signal_last_computed_at",
        "fix_loop_divergence_alerted_window",
        "fix_loop_divergence_regressions",
        "fix_loop_divergence_detected_at",
        "viewport_coverage_snapshot",
    }
    missing = audit_keys - _RESERVED_DESIGN_DOC_KEYS
    assert not missing, f"code-written keys not yet reserved: {sorted(missing)}"


# ── raise_on_size_limit: opt-in exception path ──────────────────────────────


@pytest.mark.asyncio
async def test_with_design_doc_locked_raises_on_size_limit_when_opted_in(db_session):
    """When raise_on_size_limit=True, a growth-over-ceiling write must raise
    DesignDocSizeLimitExceeded instead of silently returning False.

    This lets agent-facing callers surface the failure rather than returning
    an implicit success that leaves the strategist believing the write persisted.
    """
    padding = "x" * (DESIGN_DOC_MAX_BYTES - 30)
    project = Project(name="raise-on-limit-test", user_id="user")
    project.design_doc = {"pad": padding}
    db_session.add(project)
    await db_session.commit()

    def _grow(doc: dict) -> None:
        doc["new_key"] = "y" * 100

    with pytest.raises(DesignDocSizeLimitExceeded) as exc_info:
        await with_design_doc_locked(
            db_session, project.id, _grow, raise_on_size_limit=True
        )

    exc = exc_info.value
    assert exc.project_id == project.id, "exception must carry the project id"
    assert exc.new_size > exc.old_size, "new_size must be larger than old_size"
    assert exc.ceiling == DESIGN_DOC_MAX_BYTES, "ceiling must match DESIGN_DOC_MAX_BYTES"

    # Doc must not have been mutated.
    await db_session.refresh(project)
    assert "new_key" not in project.design_doc, (
        "doc must be unchanged after raise_on_size_limit exception"
    )


@pytest.mark.asyncio
async def test_with_design_doc_locked_silent_on_size_limit_by_default(db_session):
    """Default raise_on_size_limit=False preserves backward-compatible behavior:
    a growth-over-ceiling write returns False without raising.

    Existing callers that check the boolean return value must continue to work.
    """
    padding = "x" * (DESIGN_DOC_MAX_BYTES - 30)
    project = Project(name="silent-limit-test", user_id="user")
    project.design_doc = {"pad": padding}
    db_session.add(project)
    await db_session.commit()

    def _grow(doc: dict) -> None:
        doc["new_key"] = "y" * 100

    # Default: no raise_on_size_limit kwarg — must return False, not raise.
    result = await with_design_doc_locked(db_session, project.id, _grow)
    assert result is False, (
        "growth over ceiling with default raise_on_size_limit=False must return False"
    )

    # Explicit False — same expectation.
    result2 = await with_design_doc_locked(
        db_session, project.id, _grow, raise_on_size_limit=False
    )
    assert result2 is False, (
        "growth over ceiling with raise_on_size_limit=False must return False"
    )

    await db_session.refresh(project)
    assert "new_key" not in project.design_doc, "write must not be persisted"


@pytest.mark.asyncio
async def test_raise_on_size_limit_does_not_fire_for_shrinking_write(db_session):
    """raise_on_size_limit=True must NOT raise when the write shrinks the
    agent-owned portion — the shrink path is the recovery path and must always
    succeed even when the doc is already over the ceiling.
    """
    bloated = "x" * (DESIGN_DOC_MAX_BYTES + 100)
    project = Project(name="shrink-no-raise", user_id="user")
    project.design_doc = {"pad": bloated}
    from sqlalchemy.orm.attributes import flag_modified as _flag_modified

    _flag_modified(project, "design_doc")
    db_session.add(project)
    await db_session.commit()

    def _shrink(doc: dict) -> None:
        doc["pad"] = "small"

    # Must not raise — shrinking from over-ceiling is the self-healing path.
    result = await with_design_doc_locked(
        db_session, project.id, _shrink, raise_on_size_limit=True
    )
    await db_session.commit()
    assert result is True, "shrinking write must succeed even with raise_on_size_limit=True"

    await db_session.refresh(project)
    assert project.design_doc.get("pad") == "small", "shrink must be persisted"
