"""Seam/regression tests for cluster C3 (leader-loop saturation).

Root cause: heavy, unbounded, un-heartbeated serial GitHub/DB work ran inline on
the singleton-leader hot cycle and the reaper cycle, so a large backlog grew the
cycle wall-time past the liveness watchdog window mid-I/O (2026-05-30 incident).

These tests lock in the FAILURE-MECHANISM fix (not the business logic):

  * ``_bounded`` enforces a hard per-call ceiling so one hung GitHub/post-commit
    call can no longer pin a whole leader-cycle sweep — it raises TimeoutError
    and the caller's per-row try/except skips that row, the rest still run.
  * ``_check_evaluating_timeouts`` applies a per-cycle row cap and pumps the
    orchestrator progress heartbeat once per processed row.
  * ``_ci_waiting_sql_filter`` pushes the ci_waiting predicate into SQL so the
    orchestrator CI-poll loop no longer locks-and-rolls-back every non-CI-waiting
    EVALUATING row.
  * ``run_infra_recovery_and_hold_maintenance`` (reaper Step 5) calls the
    ``progress_cb`` heartbeat between its sub-steps.
  * ``_check_and_trigger_rollup`` calls ``progress_cb`` inside its per-child
    PR-verification loop so a fan-heavy parent keeps reaper liveness fresh.

Each test would FAIL against the pre-fix code (no cap / no heartbeat / no SQL
filter / no bounded await / no progress_cb).
"""

import asyncio

import pytest
import pytest_asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy import select
from sqlmodel import SQLModel

from app.models import Project, Task, TaskStatus


def _naive_utc_ago(**kwargs) -> datetime:
    return (datetime.now(timezone.utc) - timedelta(**kwargs)).replace(tzinfo=None)


# ── In-memory DB fixture (mirrors test_evaluating.eval_db) ────────────────────


@pytest_asyncio.fixture()
async def db(mocker):
    from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler

    if not hasattr(SQLiteTypeCompiler, "visit_JSONB"):
        SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: self.visit_JSON(
            type_, **kw
        )

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
    mocker.patch("app.services.AsyncSessionLocal", Session)

    async with Session() as session:
        yield session

    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
    await engine.dispose()


# ── _bounded: the core failure-mechanism guard ────────────────────────────────


@pytest.mark.asyncio
async def test_bounded_raises_on_hang():
    """A coroutine that never returns must be cancelled at the ceiling and raise
    TimeoutError — the guarantee that one hung GitHub call can't saturate a sweep."""
    from app.services import _bounded

    async def _never():
        # Block until cancelled at the ceiling — never returns on its own.
        await asyncio.Event().wait()

    with pytest.raises(asyncio.TimeoutError):
        await _bounded(_never(), 0.05, what="unit-hang")


@pytest.mark.asyncio
async def test_bounded_returns_value_under_ceiling():
    from app.services import _bounded

    async def _quick():
        return "ok"

    assert await _bounded(_quick(), 1.0, what="unit-quick") == "ok"


# ── _check_evaluating_timeouts: per-cycle row cap + per-row heartbeat ──────────


@pytest.mark.asyncio
async def test_eval_timeout_row_cap_and_heartbeat(db, mocker):
    """More EVALUATING rows than the cap → only the cap is fetched/processed, and
    the supplied progress heartbeat fires at least once per processed row.

    Pre-fix: the SELECT had no .limit() and the loop emitted no heartbeat, so a
    large EVALUATING backlog ran unbounded with stale liveness.

    Fix D: the sweep heartbeats the CALLER'S OWN progress_cb (its own background
    task's marker), NOT the orchestrator_worker's shared marker — pumping the
    worker's marker from a foreign task would mask a hung worker. We assert on
    the threaded callback and that _mark_orchestrator_progress is NOT touched."""
    from app import services
    from app.services import _check_evaluating_timeouts

    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock)
    orch_heartbeat = mocker.patch("app.orchestrator._mark_orchestrator_progress")
    # Shrink the cap so the test is cheap and deterministic.
    mocker.patch.object(services, "_EVAL_TIMEOUT_MAX_PER_CYCLE", 3)

    project = Project(name="cap-test", user_id="u")
    db.add(project)
    await db.commit()
    await db.refresh(project)

    # 5 childless EVALUATING parents, none CI-waiting, freshly transitioned so no
    # warning/timeout branch mutates them — the loop just visits each row.
    for i in range(5):
        db.add(
            Task(
                project_id=project.id,
                payloads=[{"type": "research", "prompt": f"p{i}"}],
                status=TaskStatus.EVALUATING,
                updated_at=_naive_utc_ago(minutes=i),  # distinct order
            )
        )
    await db.commit()

    cb_calls = {"n": 0}

    def _cb():
        cb_calls["n"] += 1

    await _check_evaluating_timeouts(progress_cb=_cb)

    # Cap honoured: caller's heartbeat fired once per processed row, never more
    # than the cap.
    assert 1 <= cb_calls["n"] <= 3
    # Fix D: the sweep must NOT pump the orchestrator_worker's shared marker.
    assert orch_heartbeat.call_count == 0


@pytest.mark.asyncio
async def test_eval_timeout_hung_github_call_does_not_block_other_rows(db, mocker):
    """SEAM: a single row whose GitHub PR check hangs is bounded + skipped; the
    sweep continues to the remaining rows on a non-poisoned session.

    This is the C3 guarantee — inject a hang mid-chain and assert subsequent
    rows still run. Pre-fix the un-bounded await would have pinned the whole
    sweep until the watchdog killed the leader."""
    from app import services
    from app.services import _check_evaluating_timeouts

    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock)
    # Tiny ceiling so the test runs fast.
    mocker.patch.object(services, "_GH_CALL_TIMEOUT_S", 0.05)

    project = Project(name="hang-test", user_id="u")
    db.add(project)
    await db.commit()
    await db.refresh(project)

    # Two EVALUATING parents carrying _pr_state_check_pending (the branch that
    # makes a GitHub get_pull_request call). The first hangs; both must be
    # visited (heartbeat fires twice).
    for i in range(2):
        db.add(
            Task(
                project_id=project.id,
                payloads=[{"type": "create", "prompt": f"p{i}"}],
                status=TaskStatus.EVALUATING,
                updated_at=_naive_utc_ago(minutes=10 + i),
                context={
                    "_pr_state_check_pending": True,
                    "review_loop": {"pr_number": 100 + i},
                },
                status_log=[
                    {
                        "t": _naive_utc_ago(minutes=20).isoformat(),
                        "msg": "checking PR state",
                    }
                ],
            )
        )
    await db.commit()

    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "owner", "repo"),
    )

    async def _hanging_get_pr(*a, **k):
        await asyncio.Event().wait()  # never returns within the ceiling

    mocker.patch("app.github.get_pull_request", side_effect=_hanging_get_pr)

    cb_calls = {"n": 0}

    def _cb():
        cb_calls["n"] += 1

    # Must complete promptly (bounded), not hang for 100s.
    await asyncio.wait_for(
        _check_evaluating_timeouts(progress_cb=_cb), timeout=10
    )

    # Both rows were visited despite the per-row GitHub hang → the hang was
    # bounded and the sweep proceeded.
    assert cb_calls["n"] == 2


# ── _ci_waiting_sql_filter: predicate pushed into SQL ─────────────────────────


@pytest.mark.asyncio
async def test_ci_waiting_sql_filter_selects_only_ci_waiting(db):
    """The SQL filter selects only EVALUATING rows whose
    context.review_loop.ci_waiting is true — so the CI-poll loop no longer
    locks-and-rolls-back the (large) non-CI-waiting backlog."""
    from app.orchestrator import _ci_waiting_sql_filter

    project = Project(name="ci-filter", user_id="u")
    db.add(project)
    await db.commit()
    await db.refresh(project)

    waiting = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        status=TaskStatus.EVALUATING,
        context={"review_loop": {"ci_waiting": True, "pr_number": 7}},
    )
    not_waiting = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        status=TaskStatus.EVALUATING,
        context={"review_loop": {"ci_waiting": False}},
    )
    no_loop = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        status=TaskStatus.EVALUATING,
        context={},
    )
    db.add_all([waiting, not_waiting, no_loop])
    await db.commit()
    await db.refresh(waiting)

    rows = (
        (
            await db.execute(
                select(Task.id).where(
                    Task.status == TaskStatus.EVALUATING,
                    _ci_waiting_sql_filter(db),
                )
            )
        )
        .scalars()
        .all()
    )
    assert rows == [waiting.id]


# ── reaper Step 5 heartbeat: run_infra_recovery_and_hold_maintenance ──────────


@pytest.mark.asyncio
async def test_infra_recovery_calls_progress_cb(db, mocker):
    """Step 5 (run_infra_recovery_and_hold_maintenance) must pump the supplied
    progress callback between its sub-steps so a slow sub-step keeps the reaper
    liveness heartbeat fresh. Pre-fix it never called _mark_reaper_progress."""
    from app.reaper_infra_recovery import run_infra_recovery_and_hold_maintenance

    # Neutralise the heaviest sub-steps so the test only exercises the heartbeat
    # wiring, not their internals.
    mocker.patch(
        "app.reaper_infra_recovery._sweep_orphan_direction_holds",
        new_callable=AsyncMock,
    )
    mocker.patch(
        "app.reaper_infra_recovery._sweep_stale_residual_cases",
        new_callable=AsyncMock,
    )

    calls = {"n": 0}

    def _cb():
        calls["n"] += 1

    now = _naive_utc_ago(minutes=0)
    await run_infra_recovery_and_hold_maintenance(
        db, now, just_reaped_ids=set(), progress_cb=_cb
    )

    # One heartbeat per sub-step (5, 5a, 5c, 5d, 5b, 5e) = 6.
    assert calls["n"] >= 6


@pytest.mark.asyncio
async def test_infra_recovery_default_progress_cb_is_noop(db, mocker):
    """Direct callers (and tests) need not supply progress_cb — it defaults to a
    no-op rather than raising."""
    from app.reaper_infra_recovery import run_infra_recovery_and_hold_maintenance

    mocker.patch(
        "app.reaper_infra_recovery._sweep_orphan_direction_holds",
        new_callable=AsyncMock,
    )
    mocker.patch(
        "app.reaper_infra_recovery._sweep_stale_residual_cases",
        new_callable=AsyncMock,
    )

    # Should not raise.
    await run_infra_recovery_and_hold_maintenance(
        db, _naive_utc_ago(minutes=0), just_reaped_ids=set()
    )


# ── _check_and_trigger_rollup: per-child verify-loop heartbeat ────────────────


@pytest.mark.asyncio
async def test_rollup_progress_cb_fires_per_completed_child(db, mocker):
    """_check_and_trigger_rollup must call progress_cb once per COMPLETED child
    in its PR-verification loop, so a fan-heavy parent keeps the reaper liveness
    fresh across serial GitHub round-trips. Pre-fix there was no callback."""
    from app.rollup_management import _check_and_trigger_rollup

    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock)
    # _verify_pr_merged is the per-child GitHub call — stub it so the loop runs
    # without touching the network (returns True = "merged/done").
    mocker.patch(
        "app.rollup_management._verify_pr_merged",
        new_callable=AsyncMock,
        return_value=True,
    )

    project = Project(name="rollup-cb", user_id="u")
    db.add(project)
    await db.commit()
    await db.refresh(project)

    parent = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        status=TaskStatus.EVALUATING,
    )
    db.add(parent)
    await db.commit()
    await db.refresh(parent)

    children = []
    for i in range(3):
        c = Task(
            project_id=project.id,
            parent_task_id=parent.id,
            payloads=[{"type": "create", "prompt": f"c{i}"}],
            status=TaskStatus.COMPLETED,
        )
        children.append(c)
    db.add_all(children)
    await db.commit()
    await db.refresh(children[0])

    calls = {"n": 0}

    def _cb():
        calls["n"] += 1

    await _check_and_trigger_rollup(children[0], db, progress_cb=_cb)

    # One heartbeat per COMPLETED child verified.
    assert calls["n"] == 3
