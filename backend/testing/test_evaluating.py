"""
Tests for EVALUATING state timeout and recovery mechanisms in the Blueprint pipeline.

Covers _check_evaluating_timeouts() and _check_and_trigger_rollup() edge cases:
  - Missed rollup detection (all children terminal)
  - Hard timeout (>6 h) → FAILED
  - CI-waiting skip
  - Warning alert (>2 h with non-terminal children)
  - Rollup guard: only review/fix children after ROLLUP_TYPES filter → COMPLETED
  - Unmerged-PR verification triggers rollup path
"""

import asyncio
import uuid

import pytest
import pytest_asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy import select
from sqlmodel import SQLModel

from app.services import (
    _check_evaluating_timeouts,
    _chain_has_active_pipeline_work,
    _chain_has_recoverable_failed_work,
    _supersede_long_queued_pipeline_tasks,
)
from app.rollup_management import _check_and_trigger_rollup
from app.models import Project, Task, TaskStatus, Alert, AlertSeverity


# ── Helpers ──────────────────────────────────────────────────────────────────


def _naive_utc_ago(**kwargs) -> datetime:
    """Return a naive UTC datetime (no tzinfo) that is ``kwargs`` in the past.

    _utcnow() in main.py strips tzinfo, so timestamps stored in status_log
    must also be naive for the arithmetic to work without a TypeError.
    """
    return (datetime.now(timezone.utc) - timedelta(**kwargs)).replace(tzinfo=None)


# ── Shared fixture ────────────────────────────────────────────────────────────


@pytest_asyncio.fixture()
async def eval_db(mocker):
    """Isolated in-memory SQLite DB for evaluating-timeout tests.

    Patches app.main.AsyncSessionLocal so _check_evaluating_timeouts() uses
    this DB instead of the production one.  Mirrors the pattern used by the
    reaper_db and rollup_db fixtures in other test modules.
    """
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
    mocker.patch("app.services.AsyncSessionLocal", _Session)

    async with _Session() as session:
        yield session

    async with _engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
    await _engine.dispose()


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_evaluating_all_children_terminal_triggers_rollup(eval_db, mocker):
    """When all children are terminal but the parent is still EVALUATING, the
    missed-rollup recovery path should call _check_and_trigger_rollup."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mock_rollup = mocker.patch(
        "app.rollup_management._check_and_trigger_rollup", new_callable=AsyncMock
    )

    project = Project(name="Eval Rollup Test", user_id="user1")
    eval_db.add(project)
    await eval_db.commit()
    await eval_db.refresh(project)

    parent = Task(
        project_id=project.id,
        payloads=[{"type": "research", "prompt": "research parent"}],
        status=TaskStatus.EVALUATING,
    )
    eval_db.add(parent)
    await eval_db.commit()
    await eval_db.refresh(parent)

    child = Task(
        project_id=project.id,
        parent_task_id=parent.id,
        payloads=[{"type": "create", "prompt": "create subtask"}],
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
    )
    eval_db.add(child)
    await eval_db.commit()

    await _check_evaluating_timeouts()

    # The missed-rollup branch executes regardless of elapsed time — the only
    # requirement is that all children are in a terminal state.
    mock_rollup.assert_called_once()


@pytest.mark.asyncio
async def test_evaluating_hard_timeout_marks_failed(eval_db, mocker):
    """A task that has been EVALUATING for >6 hours with non-terminal children
    should be marked FAILED with an appropriate status message."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    # Pin the backstop to 6h for this test (production default is 48h while
    # silent-death stabilizes; tests still validate the 6h boundary behavior).
    from app.shared_state import runtime_config as _rc

    mocker.patch.dict(_rc, {"evaluating_fail_hours": 6})

    project = Project(name="Hard Timeout Test", user_id="user1")
    eval_db.add(project)
    await eval_db.commit()
    await eval_db.refresh(project)

    parent = Task(
        project_id=project.id,
        payloads=[{"type": "research", "prompt": "long-running task"}],
        status=TaskStatus.EVALUATING,
    )
    eval_db.add(parent)
    await eval_db.commit()
    await eval_db.refresh(parent)

    # Backdate the "→ EVALUATING" status_log entry to simulate 7 hours elapsed.
    # _check_evaluating_timeouts searches for this marker to compute elapsed time.
    evaluating_since = _naive_utc_ago(hours=7)
    parent.status_log = [
        {
            "t": evaluating_since.isoformat(),
            "msg": "Status changed: IN_PROGRESS → EVALUATING",
        }
    ]
    eval_db.add(parent)
    await eval_db.commit()
    await eval_db.refresh(parent)

    # A non-terminal child prevents the missed-rollup path from firing,
    # allowing the hard-timeout check to run instead.
    child = Task(
        project_id=project.id,
        parent_task_id=parent.id,
        payloads=[{"type": "create", "prompt": "stuck subtask"}],
        status=TaskStatus.IN_PROGRESS,
    )
    eval_db.add(child)
    await eval_db.commit()

    await _check_evaluating_timeouts()

    await eval_db.refresh(parent)
    assert parent.status == TaskStatus.FAILED, (
        f"Expected FAILED but got {parent.status}"
    )
    assert "timeout" in (parent.latest_status_message or "").lower(), (
        f"Expected 'timeout' in status message but got: {parent.latest_status_message!r}"
    )


@pytest.mark.asyncio
async def test_evaluating_hard_timeout_broadcasts_terminal_literals_when_expired(
    eval_db, mocker
):
    """Regression: the hard-timeout path must broadcast terminal status LITERALS
    (parent FAILED, child CANCELLED), never a post-commit task.status/child.status
    ORM read. _maybe_create_failure_alert + commit can roll back and expire those
    instances (rollback expires even with expire_on_commit=False), so a post-commit
    read would sync lazy-load in async ctx → MissingGreenlet (same family as the
    ci_completion #6853 fix).

    Simulates the expiry by having the alert helper expire the parent + children
    on the working session — that discards their pending FAILED/CANCELLED writes,
    so a post-commit ORM read would surface the stale pre-timeout status. The
    broadcast must still carry FAILED/CANCELLED from the literals. Verified to
    fail against the pre-fix (task.status / child.status) code.
    """
    # services.py binds broadcast_to_project at module import (line 26), so the
    # capturing mock must target app.services, not app.broadcast.
    bcast = mocker.patch("app.services.broadcast_to_project", new_callable=AsyncMock)
    from app.shared_state import runtime_config as _rc

    mocker.patch.dict(_rc, {"evaluating_fail_hours": 6})

    async def _expire_alert(task, session, *args, **kwargs):
        # Mimic the prod poison/rollback: rewrite the persisted parent + children
        # status to a non-terminal sentinel and expire the instances, so a
        # post-commit ORM status read would lazy-load the WRONG value (an
        # autoflush would otherwise persist the in-memory terminal status and
        # mask the bug). The literal-based broadcast must be immune.
        from sqlalchemy import update

        kids = (
            (
                await session.execute(
                    select(Task).where(Task.parent_task_id == task.id)
                )
            )
            .scalars()
            .all()
        )
        await session.execute(
            update(Task)
            .where((Task.id == task.id) | (Task.parent_task_id == task.id))
            .values(status=TaskStatus.QUEUED)
        )
        for k in kids:
            session.expire(k)
        session.expire(task)

    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", side_effect=_expire_alert)

    project = Project(name="Hard Timeout Expired Bcast", user_id="user1")
    eval_db.add(project)
    await eval_db.commit()
    await eval_db.refresh(project)

    parent = Task(
        project_id=project.id,
        payloads=[{"type": "research", "prompt": "long-running task"}],
        status=TaskStatus.EVALUATING,
    )
    eval_db.add(parent)
    await eval_db.commit()
    await eval_db.refresh(parent)

    parent.status_log = [
        {
            "t": _naive_utc_ago(hours=7).isoformat(),
            "msg": "Status changed: IN_PROGRESS → EVALUATING",
        }
    ]
    eval_db.add(parent)
    await eval_db.commit()
    await eval_db.refresh(parent)

    child = Task(
        project_id=project.id,
        parent_task_id=parent.id,
        payloads=[{"type": "create", "prompt": "stuck subtask"}],
        status=TaskStatus.IN_PROGRESS,
    )
    eval_db.add(child)
    await eval_db.commit()
    await eval_db.refresh(child)

    await _check_evaluating_timeouts()

    by_id = {}
    for c in bcast.call_args_list:
        if len(c.args) >= 3 and c.args[1] == "task_updated":
            payload = c.args[2]
            by_id[str(payload.get("task_id"))] = payload.get("status")

    assert by_id.get(str(parent.id)) == TaskStatus.FAILED, (
        "parent must broadcast the FAILED literal even when the instance is "
        f"expired, got {by_id.get(str(parent.id))!r}"
    )
    assert by_id.get(str(child.id)) == TaskStatus.CANCELLED, (
        "cascade-cancelled child must broadcast the CANCELLED literal even when "
        f"the instance is expired, got {by_id.get(str(child.id))!r}"
    )


@pytest.mark.asyncio
async def test_evaluating_ci_waiting_skipped(eval_db, mocker):
    """Tasks with context['ci_waiting'] = True must be skipped by the timeout
    sweep while under the 30-minute CI backstop threshold — the normal 20-min
    CI escalation in orchestrator.py handles them instead."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mock_rollup = mocker.patch(
        "app.rollup_management._check_and_trigger_rollup", new_callable=AsyncMock
    )

    project = Project(name="CI Waiting Test", user_id="user1")
    eval_db.add(project)
    await eval_db.commit()
    await eval_db.refresh(project)

    parent = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "ci-gated task"}],
        status=TaskStatus.EVALUATING,
        context={"ci_waiting": True},
    )
    eval_db.add(parent)
    await eval_db.commit()
    await eval_db.refresh(parent)

    # 25 minutes — under the 30-minute CI backstop threshold.
    # The normal orchestrator CI timeout should handle it, not this sweep.
    evaluating_since = _naive_utc_ago(minutes=25)
    parent.status_log = [
        {
            "t": evaluating_since.isoformat(),
            "msg": "Status changed: IN_PROGRESS → EVALUATING",
        }
    ]
    eval_db.add(parent)
    await eval_db.commit()

    await _check_evaluating_timeouts()

    await eval_db.refresh(parent)
    assert parent.status == TaskStatus.EVALUATING, (
        f"Expected EVALUATING (skipped) but got {parent.status}"
    )
    mock_rollup.assert_not_called()


@pytest.mark.asyncio
async def test_evaluating_ci_backstop_uses_ci_wait_started(eval_db, mocker):
    """Regression: the CI backstop must measure elapsed time from
    review_loop.ci_wait_started, not from the EVALUATING transition.

    A research task may sit in EVALUATING for a long time waiting on subtasks
    before the rollup PR opens and CI wait actually begins. Using the
    EVALUATING transition as the time base false-positives the backstop the
    instant CI polling starts (seen in task 5b1fa3ee: killed after ~3min of
    healthy CI because the task had been EVALUATING for ~48min waiting on
    subtasks).
    """
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.rollup_management._check_and_trigger_rollup", new_callable=AsyncMock
    )

    project = Project(name="CI Backstop Time Base", user_id="user1")
    eval_db.add(project)
    await eval_db.commit()
    await eval_db.refresh(project)

    parent = Task(
        project_id=project.id,
        payloads=[{"type": "research", "prompt": "long-subtask task"}],
        status=TaskStatus.EVALUATING,
    )
    eval_db.add(parent)
    await eval_db.commit()
    await eval_db.refresh(parent)

    # EVALUATING transition was 48 minutes ago (subtask phase took 45 min,
    # then CI wait began) — well beyond the 30-min backstop threshold.
    evaluating_since = _naive_utc_ago(minutes=48)
    # But CI wait only actually started 3 minutes ago — still healthy.
    ci_wait_started = _naive_utc_ago(minutes=3)
    parent.status_log = [
        {
            "t": evaluating_since.isoformat(),
            "msg": "Status changed: COMPLETED → EVALUATING",
        }
    ]
    parent.context = {
        "review_loop": {
            "ci_waiting": True,
            "ci_wait_started": ci_wait_started.isoformat(),
            "pr_number": 1381,
        }
    }
    eval_db.add(parent)
    await eval_db.commit()

    await _check_evaluating_timeouts()

    await eval_db.refresh(parent)
    assert parent.status == TaskStatus.EVALUATING, (
        f"Expected EVALUATING (CI still within backstop window) but got "
        f"{parent.status}: {parent.latest_status_message!r}"
    )


@pytest.mark.asyncio
async def test_evaluating_ci_backstop_fires_when_ci_wait_started_old(eval_db, mocker):
    """Complement: when review_loop.ci_wait_started IS older than the backstop
    threshold, the task must be hard-killed."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.rollup_management._check_and_trigger_rollup", new_callable=AsyncMock
    )

    project = Project(name="CI Backstop Fires", user_id="user1")
    eval_db.add(project)
    await eval_db.commit()
    await eval_db.refresh(project)

    parent = Task(
        project_id=project.id,
        payloads=[{"type": "research", "prompt": "stalled CI task"}],
        status=TaskStatus.EVALUATING,
    )
    eval_db.add(parent)
    await eval_db.commit()
    await eval_db.refresh(parent)

    # CI wait started 90 minutes ago — over the 60-min backstop default.
    ci_wait_started = _naive_utc_ago(minutes=90)
    parent.status_log = [
        {
            "t": _naive_utc_ago(minutes=120).isoformat(),
            "msg": "Status changed: COMPLETED → EVALUATING",
        }
    ]
    parent.context = {
        "review_loop": {
            "ci_waiting": True,
            "ci_wait_started": ci_wait_started.isoformat(),
            "pr_number": 9999,
        }
    }
    eval_db.add(parent)
    await eval_db.commit()

    await _check_evaluating_timeouts()

    await eval_db.refresh(parent)
    assert parent.status == TaskStatus.FAILED, (
        f"Expected FAILED (CI wait exceeded backstop) but got "
        f"{parent.status}: {parent.latest_status_message!r}"
    )
    assert "backstop" in (parent.latest_status_message or "").lower()


@pytest.mark.asyncio
async def test_evaluating_warning_alert_created(eval_db, mocker):
    """A task EVALUATING for >2 hours (the warning threshold) with non-terminal
    children should produce an undismissed WARNING alert."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)

    project = Project(name="Warning Alert Test", user_id="user1")
    eval_db.add(project)
    await eval_db.commit()
    await eval_db.refresh(project)

    parent = Task(
        project_id=project.id,
        payloads=[{"type": "research", "prompt": "stuck evaluating"}],
        status=TaskStatus.EVALUATING,
    )
    eval_db.add(parent)
    await eval_db.commit()
    await eval_db.refresh(parent)

    # 3 hours: past the 2-hour warning threshold, but below the 6-hour fail threshold.
    evaluating_since = _naive_utc_ago(hours=3)
    parent.status_log = [
        {
            "t": evaluating_since.isoformat(),
            "msg": "Status changed: IN_PROGRESS → EVALUATING",
        }
    ]
    eval_db.add(parent)
    await eval_db.commit()
    await eval_db.refresh(parent)

    # Non-terminal child: prevents missed-rollup path, forces the time-based checks.
    child = Task(
        project_id=project.id,
        parent_task_id=parent.id,
        payloads=[{"type": "create", "prompt": "stuck subtask"}],
        status=TaskStatus.IN_PROGRESS,
    )
    eval_db.add(child)
    await eval_db.commit()

    await _check_evaluating_timeouts()

    result = await eval_db.execute(
        select(Alert).where(
            Alert.task_id == parent.id,
            Alert.severity == AlertSeverity.WARNING,
            Alert.dismissed_at.is_(None),
        )
    )
    alerts = result.scalars().all()
    assert alerts, "Expected a WARNING alert for a long-evaluating task, but none found"
    assert "evaluating" in alerts[0].message.lower(), (
        f"Unexpected alert message: {alerts[0].message!r}"
    )


@pytest.mark.asyncio
async def test_rollup_empty_children_after_filter(eval_db, mocker):
    """When every child is a review/fix type (excluded from ROLLUP_TYPES),
    _check_and_trigger_rollup should mark the parent COMPLETED — not CANCELLED."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch("app.rollup_management._cleanup_branches", new_callable=AsyncMock)

    project = Project(name="Rollup Filter Test", user_id="user1")
    eval_db.add(project)
    await eval_db.commit()
    await eval_db.refresh(project)

    parent = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "parent task"}],
        status=TaskStatus.EVALUATING,
    )
    eval_db.add(parent)
    await eval_db.commit()
    await eval_db.refresh(parent)

    review_child = Task(
        project_id=project.id,
        parent_task_id=parent.id,
        payloads=[{"type": "review", "prompt": "review PR"}],
        status=TaskStatus.COMPLETED,
    )
    fix_child = Task(
        project_id=project.id,
        parent_task_id=parent.id,
        payloads=[{"type": "fix", "prompt": "fix issues"}],
        status=TaskStatus.COMPLETED,
    )
    eval_db.add(review_child)
    eval_db.add(fix_child)
    await eval_db.commit()
    await eval_db.refresh(review_child)
    await eval_db.refresh(fix_child)

    await _check_and_trigger_rollup(review_child, eval_db)

    await eval_db.refresh(parent)
    assert parent.status == TaskStatus.COMPLETED, (
        f"Expected COMPLETED but got {parent.status}: {parent.latest_status_message!r}"
    )
    assert "pipeline" in (parent.latest_status_message or "").lower(), (
        f"Unexpected status message: {parent.latest_status_message!r}"
    )


@pytest.mark.asyncio
async def test_evaluating_unmerged_pr_verification(eval_db, mocker):
    """When _check_evaluating_timeouts detects all children terminal, it calls
    _check_and_trigger_rollup, which invokes _verify_pr_merged on each COMPLETED
    child.  If _verify_pr_merged returns True (e.g. GitHub confirms merge), the
    child's context.pr_merged flag should be set and persisted."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)

    project = Project(name="PR Verify Test", user_id="user1")
    eval_db.add(project)
    await eval_db.commit()
    await eval_db.refresh(project)

    parent = Task(
        project_id=project.id,
        payloads=[{"type": "research", "prompt": "parent task"}],
        status=TaskStatus.EVALUATING,
    )
    eval_db.add(parent)
    await eval_db.commit()
    await eval_db.refresh(parent)

    # Child is COMPLETED but has no pr_merged in context and no PR artifact —
    # _verify_pr_merged must be called to determine merge status.
    child = Task(
        project_id=project.id,
        parent_task_id=parent.id,
        payloads=[{"type": "create", "prompt": "create subtask"}],
        status=TaskStatus.COMPLETED,
        context={},
    )
    eval_db.add(child)
    await eval_db.commit()
    await eval_db.refresh(child)

    # Mock _verify_pr_merged to simulate a verified merge and update the
    # task's context in-memory (mirrors what the real function does).
    async def _mock_verify(task: Task, session: AsyncSession) -> bool:
        task.context = {**(task.context or {}), "pr_merged": True}
        return True

    mock_verify = AsyncMock(side_effect=_mock_verify)
    mocker.patch("app.rollup_management._verify_pr_merged", new=mock_verify)

    await _check_evaluating_timeouts()

    # _check_evaluating_timeouts commits the session after calling _check_and_trigger_rollup,
    # which persists the pr_merged flag written by the mock.
    await eval_db.refresh(child)
    assert child.context.get("pr_merged") is True, (
        "Expected child context to have pr_merged=True after _verify_pr_merged succeeded"
    )
    mock_verify.assert_called_once()


@pytest.mark.asyncio
async def test_recent_rollup_query_builds_against_updated_at(eval_db):
    """Regression: the concurrent-rollup observability query in
    _check_and_trigger_rollup's merge-conflict branch referenced
    Task.updated_at before that column existed. Migration 0033 adds the
    real column; this test pins the query to it and confirms SQLModel
    compiles the filter without raising."""
    from sqlalchemy import func as _func

    project = Project(name="Recent Rollup Query", user_id="user1")
    eval_db.add(project)
    await eval_db.commit()
    await eval_db.refresh(project)

    recent = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "recent rollup"}],
        status=TaskStatus.COMPLETED,
        updated_at=_naive_utc_ago(minutes=30),
        context={"rollup_pr_url": "https://github.com/o/r/pull/1"},
    )
    eval_db.add(recent)
    await eval_db.commit()
    await eval_db.refresh(recent)

    result = await eval_db.execute(
        select(_func.count())
        .select_from(Task)
        .where(
            Task.status == TaskStatus.COMPLETED,
            Task.updated_at >= _naive_utc_ago(hours=1),
        )
    )
    count = result.scalar() or 0
    assert count >= 1, (
        f"Expected at least the recent rollup task (updated 30min ago) to match, got {count}"
    )


@pytest.mark.asyncio
async def test_check_and_trigger_rollup_computes_age_from_updated_at(eval_db, mocker):
    """Regression: _check_and_trigger_rollup computed `age` from c.updated_at
    before that column existed. Migration 0033 adds the real column; this
    test pins the age computation to the real column and confirms a fresh
    child (updated within the stale threshold) is skipped by the respawn
    loop (respawn count stays at 0)."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.rollup_management._verify_pr_merged", new=AsyncMock(return_value=False)
    )
    mock_spawn = mocker.patch(
        "app.rollup_management._spawn_pipeline_task",
        new=AsyncMock(return_value=(None, None)),
    )

    project = Project(name="Age From Updated At", user_id="user1")
    eval_db.add(project)
    await eval_db.commit()
    await eval_db.refresh(project)

    parent = Task(
        project_id=project.id,
        payloads=[{"type": "research", "prompt": "parent"}],
        status=TaskStatus.EVALUATING,
    )
    eval_db.add(parent)
    await eval_db.commit()
    await eval_db.refresh(parent)

    child = Task(
        project_id=project.id,
        parent_task_id=parent.id,
        payloads=[{"type": "create", "prompt": "child"}],
        status=TaskStatus.COMPLETED,
        context={},
        updated_at=_naive_utc_ago(minutes=2),
    )
    eval_db.add(child)
    await eval_db.commit()
    await eval_db.refresh(child)

    await _check_and_trigger_rollup(child, eval_db)

    # Fresh child (2 min old, stale threshold is 10 min) → must not be respawned.
    mock_spawn.assert_not_called()


@pytest.mark.asyncio
async def test_verify_pr_merged_closed_pr_sets_status_message(eval_db, mocker):
    """When _verify_pr_merged sees a closed-but-not-merged PR, it must set
    latest_status_message on the in-memory task — not just inside the separate
    write_session that may fail with a row-lock or commit error.

    Regression for task 512c4514: the verifier flipped task.status to FAILED
    via the outer session, but the explanatory message was only written in a
    failing write_session, leaving the row with FAILED status and a stale
    'Creating pull request...' message.
    """
    from app.rollup_management import _verify_pr_merged
    from app.models import Artifact, ArtifactType

    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)

    project = Project(name="Closed PR Verify Test", user_id="user1")
    eval_db.add(project)
    await eval_db.commit()
    await eval_db.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "make a thing"}],
        status=TaskStatus.COMPLETED,
        context={},
        latest_status_message="Creating pull request for blueprint/task-deadbeef...",
    )
    eval_db.add(task)
    await eval_db.commit()
    await eval_db.refresh(task)

    eval_db.add(
        Artifact(
            task_id=task.id,
            artifact_type=ArtifactType.PULL_REQUEST,
            pr_url="https://github.com/foo/bar/pull/42",
        )
    )
    await eval_db.commit()

    # _verify_pr_merged imports these inside the function body, so patch at the
    # source modules (not on app.rollup_management).
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new=AsyncMock(return_value=("tok", "foo", "bar")),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(return_value={"state": "closed", "merged": False}),
    )
    # The separate write_session path goes through AsyncSessionLocal — force it
    # to fail so we exercise the in-memory message fallback.
    mocker.patch(
        "app.rollup_management.AsyncSessionLocal",
        side_effect=RuntimeError("simulated row lock"),
    )

    result = await _verify_pr_merged(task, eval_db)

    assert result is True, (
        "verifier should return True so callers don't block on FAILED tasks"
    )
    assert task.status == TaskStatus.FAILED
    assert task.context.get("pr_closed_unmerged") is True
    assert task.latest_status_message == "PR #42 closed without merging", (
        f"Expected in-memory latest_status_message to reflect closure, got: {task.latest_status_message!r}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("marker", ["pr_closed_success", "pr_closed_empty"])
async def test_verify_pr_merged_deliberate_success_close_completes(
    eval_db, mocker, marker
):
    """A PR closed-without-merging that the system marked as a *deliberate
    success-close* (work landed elsewhere) must COMPLETE the child, not FAIL it.

    Two markers carry this signal:
      • pr_closed_success — the same change merged via a sibling/dedup PR
        ("already merged by another task"), or an agent early-exit.
      • pr_closed_empty   — the empty/branch-equivalent rollup-PR close.

    Failing such a child mislabels a success and can cascade a rollup parent
    into a partial-failure finalize. Regression for incident 2026-05-28
    (1a828018 / 6540dd42 'via #6628' were hand-cancelled because the verifier
    would otherwise have wrongly FAILED them).
    """
    from app.rollup_management import _verify_pr_merged
    from app.models import Artifact, ArtifactType

    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)

    project = Project(name="Success Close Verify Test", user_id="user1")
    eval_db.add(project)
    await eval_db.commit()
    await eval_db.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "make a thing"}],
        status=TaskStatus.COMPLETED,
        context={marker: True},
        latest_status_message="done",
    )
    eval_db.add(task)
    await eval_db.commit()
    await eval_db.refresh(task)

    eval_db.add(
        Artifact(
            task_id=task.id,
            artifact_type=ArtifactType.PULL_REQUEST,
            pr_url="https://github.com/foo/bar/pull/77",
        )
    )
    await eval_db.commit()

    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new=AsyncMock(return_value=("tok", "foo", "bar")),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(return_value={"state": "closed", "merged": False}),
    )
    # Force the persisting write_session to fail so we also prove the in-memory
    # mutation (status + pr_merged) is set regardless of the DB write.
    mocker.patch(
        "app.rollup_management.AsyncSessionLocal",
        side_effect=RuntimeError("simulated row lock"),
    )

    result = await _verify_pr_merged(task, eval_db)

    assert result is True, "success-close child must resolve True (work landed)"
    assert task.status == TaskStatus.COMPLETED, (
        f"deliberate success-close must stay COMPLETED, got {task.status}"
    )
    assert task.context.get("pr_merged") is True, (
        "success-close must set pr_merged so the rollup treats work as landed"
    )
    assert task.context.get("pr_closed_unmerged") is not True, (
        "success-close must NOT be flagged as a rejected/abandoned PR"
    )


# ── skip_locked lock contention tests ────────────────────────────────────────


@pytest.mark.asyncio
async def test_rollup_skips_when_parent_lock_held(eval_db, mocker, caplog):
    """Regression for LockNotAvailableError observed in prod: when a concurrent
    sibling child holds the row lock on the parent task, the SELECT ... FOR
    UPDATE SKIP LOCKED returns zero rows.  _check_and_trigger_rollup must
    detect that, log the deferral, and return without raising — the reaper's
    stale EVALUATING sweep retries within 15 minutes as the safety net.
    """
    import logging

    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)

    project = Project(name="Lock Skip Test", user_id="user1")
    eval_db.add(project)
    await eval_db.commit()
    await eval_db.refresh(project)

    parent = Task(
        project_id=project.id,
        payloads=[{"type": "research", "prompt": "parent research"}],
        status=TaskStatus.EVALUATING,
    )
    eval_db.add(parent)
    await eval_db.commit()
    await eval_db.refresh(parent)

    child = Task(
        project_id=project.id,
        parent_task_id=parent.id,
        payloads=[{"type": "create", "prompt": "child create"}],
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
    )
    eval_db.add(child)
    await eval_db.commit()
    await eval_db.refresh(child)

    # Simulate the parent row being locked by another session: the first
    # session.execute call (the parent SELECT ... FOR UPDATE SKIP LOCKED) must
    # return an empty scalar result.  Subsequent execute calls (if any) pass
    # through to the real session.
    original_execute = eval_db.execute
    call_count = {"n": 0}

    class _EmptyResult:
        def scalars(self):
            return self

        def first(self):
            return None

        def all(self):
            return []

    async def _mock_execute(stmt, *args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return _EmptyResult()
        return await original_execute(stmt, *args, **kwargs)

    # Use the same pattern as test_diagnostics.py: pytest-mock auto-wraps async
    # side_effect fns into AsyncMock when the target is awaited.
    mocker.patch.object(eval_db, "execute", new=AsyncMock(side_effect=_mock_execute))

    with caplog.at_level(logging.INFO, logger="app.rollup_management"):
        # Must not raise LockNotAvailableError or any other exception
        await _check_and_trigger_rollup(child, eval_db)

    # Verify the expected skip log line was emitted
    assert any(
        "Skipped rollup check" in rec.message and "parent lock held" in rec.message
        for rec in caplog.records
    ), (
        f"Expected '[ROLLUP] Skipped rollup check ... parent lock held' log, got: {[r.message for r in caplog.records]}"
    )

    # Parent status must remain EVALUATING (rollup was deferred, not processed)
    await eval_db.refresh(parent)
    assert parent.status == TaskStatus.EVALUATING, (
        f"Expected EVALUATING (skip path defers work) but got {parent.status}"
    )


@pytest.mark.asyncio
async def test_rollup_proceeds_when_parent_lock_available(eval_db, mocker):
    """Complement to test_rollup_skips_when_parent_lock_held: when the parent
    row is NOT locked, _check_and_trigger_rollup must proceed with the rollup
    decision normally (exercising the review/fix-only short-circuit path
    already covered by test_rollup_empty_children_after_filter, but here the
    point is that skip_locked=True does not break the happy path).
    """
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch("app.rollup_management._cleanup_branches", new_callable=AsyncMock)

    project = Project(name="Lock Available Test", user_id="user1")
    eval_db.add(project)
    await eval_db.commit()
    await eval_db.refresh(project)

    parent = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "parent create"}],
        status=TaskStatus.EVALUATING,
    )
    eval_db.add(parent)
    await eval_db.commit()
    await eval_db.refresh(parent)

    # All children are review/fix (excluded from ROLLUP_TYPES) — short-circuits
    # to parent COMPLETED without needing github/PR mocks.
    review_child = Task(
        project_id=project.id,
        parent_task_id=parent.id,
        payloads=[{"type": "review", "prompt": "review"}],
        status=TaskStatus.COMPLETED,
    )
    eval_db.add(review_child)
    await eval_db.commit()
    await eval_db.refresh(review_child)

    # No mock on session.execute — lock is available, query returns the parent row.
    await _check_and_trigger_rollup(review_child, eval_db)

    await eval_db.refresh(parent)
    assert parent.status == TaskStatus.COMPLETED, (
        f"Expected COMPLETED (happy path with available lock) but got "
        f"{parent.status}: {parent.latest_status_message!r}"
    )


# ── Backstop diagnosis honesty + one-shot recovery ──────────────────────────
#
# Regression coverage for the PR #1705/#1688/#1676 incident: CI genuinely
# passed but the review-approved gate never closed, and the backstop killed
# the task with the message "CI polling stalled after escalation" — a lie.
# These tests pin the honest-reason and one-shot recovery behaviours.


@pytest.mark.asyncio
async def test_backstop_reason_names_review_when_ci_passed(eval_db, mocker):
    """When CI has passed but review_approved is absent AND recovery was
    already attempted (so we proceed to kill), the backstop's failure message
    must name the review gate, not claim CI stalled."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    # Stop the recovery path from masking the kill — force recovery-already-attempted
    # so we exercise the reason-build path end-to-end.

    project = Project(name="Backstop Honest Reason", user_id="user1")
    eval_db.add(project)
    await eval_db.commit()
    await eval_db.refresh(project)

    parent = Task(
        project_id=project.id,
        payloads=[{"type": "research", "prompt": "stuck on review"}],
        status=TaskStatus.EVALUATING,
    )
    eval_db.add(parent)
    await eval_db.commit()
    await eval_db.refresh(parent)

    ci_wait_started = _naive_utc_ago(
        minutes=90
    )  # well past the 60-min backstop default
    parent.status_log = [
        {
            "t": _naive_utc_ago(minutes=120).isoformat(),
            "msg": "Status changed: IN_PROGRESS → EVALUATING",
        }
    ]
    parent.context = {
        "review_loop": {
            "ci_waiting": True,
            "ci_wait_started": ci_wait_started.isoformat(),
            "ci_passed": True,
            "review_approved": False,
            "review_recovery_attempted": True,  # force the kill path, not recovery
            "escalated": True,
            "pr_number": 12345,
            "branch": "blueprint/task-honest",
            "base_branch": "main",
            "original_task_id": str(parent.id),
        }
    }
    eval_db.add(parent)
    await eval_db.commit()

    await _check_evaluating_timeouts()

    await eval_db.refresh(parent)
    assert parent.status == TaskStatus.FAILED
    msg = (parent.latest_status_message or "").lower()
    assert "backstop" in msg
    assert "review" in msg, (
        f"Failure message must name the review gate, got: {parent.latest_status_message!r}"
    )
    assert "ci polling stalled" not in msg, (
        f"The old misleading 'CI polling stalled' phrasing must not appear, got: "
        f"{parent.latest_status_message!r}"
    )


@pytest.mark.asyncio
async def test_backstop_spawns_recovery_review_once(eval_db, mocker):
    """When CI passed but review_approved is missing and no recovery has yet
    been attempted, the backstop must spawn a fresh review, mark recovery
    attempted, and reset the CI-wait clock — instead of hard-killing on the
    first tick past the 30-minute threshold."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)

    mock_spawn = mocker.patch(
        "app.review_pipeline._spawn_pipeline_task", new_callable=AsyncMock
    )
    # Return a fake "new review task" so recovery reports success.
    fake_review = Task(
        project_id=uuid.uuid4(),  # placeholder; not the target of any assertion
        payloads=[{"type": "review", "pr_number": 77777}],
        status=TaskStatus.QUEUED,
    )
    mock_spawn.return_value = (fake_review, True)

    project = Project(name="Backstop Recovery", user_id="user1")
    eval_db.add(project)
    await eval_db.commit()
    await eval_db.refresh(project)

    parent = Task(
        project_id=project.id,
        payloads=[{"type": "research", "prompt": "stuck on review"}],
        status=TaskStatus.EVALUATING,
    )
    eval_db.add(parent)
    await eval_db.commit()
    await eval_db.refresh(parent)

    ci_wait_started = _naive_utc_ago(minutes=90)
    parent.status_log = [
        {
            "t": _naive_utc_ago(minutes=120).isoformat(),
            "msg": "Status changed: IN_PROGRESS → EVALUATING",
        }
    ]
    parent.context = {
        "review_loop": {
            "ci_waiting": True,
            "ci_wait_started": ci_wait_started.isoformat(),
            "ci_passed": True,
            "review_approved": False,
            "pr_number": 77777,
            "branch": "blueprint/task-recover",
            "base_branch": "main",
            "original_task_id": str(parent.id),
        }
    }
    eval_db.add(parent)
    await eval_db.commit()

    await _check_evaluating_timeouts()

    await eval_db.refresh(parent)
    assert parent.status == TaskStatus.EVALUATING, (
        f"Backstop must NOT kill on the first recovery attempt, got "
        f"{parent.status}: {parent.latest_status_message!r}"
    )
    mock_spawn.assert_called_once()
    # recovery flag set and CI-wait window reset
    rl = (parent.context or {}).get("review_loop", {})
    assert rl.get("review_recovery_attempted") is True
    assert rl.get("review_recovery_at")
    # ci_wait_started must be newer than the original stale timestamp
    assert rl.get("ci_wait_started") != ci_wait_started.isoformat()


@pytest.mark.asyncio
async def test_backstop_early_recovery_when_review_child_terminal(eval_db, mocker):
    """Early trigger: CI has passed, review_approved is missing, and the review
    child task is already FAILED — recovery must fire immediately without
    waiting for the 30-minute backstop window to elapse."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)

    mock_spawn = mocker.patch(
        "app.review_pipeline._spawn_pipeline_task", new_callable=AsyncMock
    )
    fake_review = Task(
        project_id=uuid.uuid4(),
        payloads=[{"type": "review", "pr_number": 55555}],
        status=TaskStatus.QUEUED,
    )
    mock_spawn.return_value = (fake_review, True)

    project = Project(name="Backstop Early Recovery", user_id="user1")
    eval_db.add(project)
    await eval_db.commit()
    await eval_db.refresh(project)

    parent = Task(
        project_id=project.id,
        payloads=[{"type": "research", "prompt": "early recovery"}],
        status=TaskStatus.EVALUATING,
    )
    eval_db.add(parent)
    await eval_db.commit()
    await eval_db.refresh(parent)

    # CI wait began only 8 minutes ago — well under the 30-min backstop.
    ci_wait_started = _naive_utc_ago(minutes=8)
    parent.context = {
        "review_loop": {
            "ci_waiting": True,
            "ci_wait_started": ci_wait_started.isoformat(),
            "ci_passed": True,
            "review_approved": False,
            "pr_number": 55555,
            "branch": "blueprint/task-early",
            "base_branch": "main",
            "original_task_id": str(parent.id),
        }
    }
    eval_db.add(parent)
    await eval_db.commit()

    # Review child task: FAILED without posting a verdict.
    dead_review = Task(
        project_id=project.id,
        payloads=[
            {
                "type": "review",
                "pr_number": 55555,
                "original_task_id": str(parent.id),
            }
        ],
        status=TaskStatus.FAILED,
    )
    eval_db.add(dead_review)
    await eval_db.commit()

    await _check_evaluating_timeouts()

    await eval_db.refresh(parent)
    assert parent.status == TaskStatus.EVALUATING, (
        f"Early recovery must not kill the parent, got "
        f"{parent.status}: {parent.latest_status_message!r}"
    )
    mock_spawn.assert_called_once()
    rl = (parent.context or {}).get("review_loop", {})
    assert rl.get("review_recovery_attempted") is True
    assert rl.get("review_recovery_at")


@pytest.mark.asyncio
async def test_backstop_no_early_recovery_when_review_child_in_progress(
    eval_db, mocker
):
    """Guard: if the review child is still running (not terminal), the early
    trigger must NOT fire — the elapsed guard should still cause a skip."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mock_spawn = mocker.patch(
        "app.review_pipeline._spawn_pipeline_task", new_callable=AsyncMock
    )

    project = Project(name="Backstop Early Recovery Negative", user_id="user1")
    eval_db.add(project)
    await eval_db.commit()
    await eval_db.refresh(project)

    parent = Task(
        project_id=project.id,
        payloads=[{"type": "research", "prompt": "no early recovery"}],
        status=TaskStatus.EVALUATING,
    )
    eval_db.add(parent)
    await eval_db.commit()
    await eval_db.refresh(parent)

    ci_wait_started = _naive_utc_ago(minutes=8)
    parent.context = {
        "review_loop": {
            "ci_waiting": True,
            "ci_wait_started": ci_wait_started.isoformat(),
            "ci_passed": True,
            "review_approved": False,
            "pr_number": 66666,
            "branch": "blueprint/task-live",
            "base_branch": "main",
            "original_task_id": str(parent.id),
        }
    }
    eval_db.add(parent)
    await eval_db.commit()

    live_review = Task(
        project_id=project.id,
        payloads=[
            {
                "type": "review",
                "pr_number": 66666,
                "original_task_id": str(parent.id),
            }
        ],
        status=TaskStatus.IN_PROGRESS,
    )
    eval_db.add(live_review)
    await eval_db.commit()

    await _check_evaluating_timeouts()

    await eval_db.refresh(parent)
    assert parent.status == TaskStatus.EVALUATING
    mock_spawn.assert_not_called()
    rl = (parent.context or {}).get("review_loop", {})
    assert rl.get("review_recovery_attempted") is not True


# ── Chain-active / hard-timeout bypass tests ──────────────────────────────────


@pytest.mark.asyncio
async def test_chain_active_queued_review_recent_counts_live(eval_db):
    """A recently-created QUEUED review task on the same chain counts as live
    pipeline work — _chain_has_active_pipeline_work returns True, which would
    defer the parent's 6h hard timeout."""
    project = Project(name="Chain Queued Recent", user_id="user1")
    eval_db.add(project)
    await eval_db.commit()
    await eval_db.refresh(project)

    parent = Task(
        project_id=project.id,
        payloads=[{"type": "research", "prompt": "rollup parent"}],
        status=TaskStatus.EVALUATING,
        context={"review_loop": {"pr_number": 77001, "original_task_id": "chain-abc"}},
    )
    eval_db.add(parent)
    await eval_db.commit()
    await eval_db.refresh(parent)

    queued_review = Task(
        project_id=project.id,
        payloads=[
            {
                "type": "review",
                "pr_number": 77001,
                "original_task_id": "chain-abc",
            }
        ],
        status=TaskStatus.QUEUED,
        last_heartbeat=None,
    )
    queued_review.created_at = _naive_utc_ago(minutes=5)
    eval_db.add(queued_review)
    await eval_db.commit()

    assert await _chain_has_active_pipeline_work(parent, eval_db) is True


@pytest.mark.asyncio
async def test_chain_active_stale_provisioning_does_not_count(eval_db):
    """A PROVISIONING review/fix task created >30min ago is stuck (slot caps,
    direction holds) and must NOT count as live work — otherwise it would
    indefinitely defer the parent's 6h EVALUATING hard timeout."""
    project = Project(name="Chain Stale Provisioning", user_id="user1")
    eval_db.add(project)
    await eval_db.commit()
    await eval_db.refresh(project)

    parent = Task(
        project_id=project.id,
        payloads=[{"type": "research", "prompt": "rollup parent"}],
        status=TaskStatus.EVALUATING,
        context={"review_loop": {"pr_number": 77002, "original_task_id": "chain-def"}},
    )
    eval_db.add(parent)
    await eval_db.commit()
    await eval_db.refresh(parent)

    stale_fix = Task(
        project_id=project.id,
        payloads=[
            {
                "type": "fix",
                "pr_number": 77002,
                "original_task_id": "chain-def",
            }
        ],
        status=TaskStatus.PROVISIONING,
        last_heartbeat=None,
    )
    # 40 minutes ago — past the 30-min queued_max_age_seconds threshold.
    stale_fix.created_at = _naive_utc_ago(minutes=40)
    eval_db.add(stale_fix)
    await eval_db.commit()

    assert await _chain_has_active_pipeline_work(parent, eval_db) is False


@pytest.mark.asyncio
async def test_evaluating_approval_gate_hard_timeout_fails(eval_db, mocker):
    """An approval-gated parent (requires_approval=True) whose _approval_set_at
    is older than APPROVAL_GATE_FAIL_SECONDS (2h) must be marked FAILED by the
    companion timeout — otherwise it stalls indefinitely because the 6h
    backstop can be deferred by _chain_has_active_pipeline_work."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)

    project = Project(name="Approval Gate Timeout", user_id="user1")
    eval_db.add(project)
    await eval_db.commit()
    await eval_db.refresh(project)

    approval_since = _naive_utc_ago(hours=3)
    parent = Task(
        project_id=project.id,
        payloads=[{"type": "research", "prompt": "approval-gated parent"}],
        status=TaskStatus.EVALUATING,
        requires_approval=True,
        context={"_approval_set_at": approval_since.isoformat()},
    )
    eval_db.add(parent)
    await eval_db.commit()
    await eval_db.refresh(parent)

    await _check_evaluating_timeouts()

    await eval_db.refresh(parent)
    assert parent.status == TaskStatus.FAILED, (
        f"Expected FAILED after approval-gate hard timeout, got {parent.status}: "
        f"{parent.latest_status_message!r}"
    )
    assert "approval" in (parent.latest_status_message or "").lower()


@pytest.mark.asyncio
async def test_evaluating_approval_gate_within_window_not_failed(eval_db, mocker):
    """Complement: an approval-gated parent within the 2h window must remain
    EVALUATING (operators still have time to approve/deny)."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)

    project = Project(name="Approval Gate Fresh", user_id="user1")
    eval_db.add(project)
    await eval_db.commit()
    await eval_db.refresh(project)

    parent = Task(
        project_id=project.id,
        payloads=[{"type": "research", "prompt": "fresh approval gate"}],
        status=TaskStatus.EVALUATING,
        requires_approval=True,
        context={"_approval_set_at": _naive_utc_ago(minutes=30).isoformat()},
    )
    eval_db.add(parent)
    await eval_db.commit()
    await eval_db.refresh(parent)

    await _check_evaluating_timeouts()

    await eval_db.refresh(parent)
    assert parent.status == TaskStatus.EVALUATING


# ── Backstop recovery: supersede long-queued blocking pipeline tasks ──────────


@pytest.mark.asyncio
async def test_supersede_cancels_long_queued_fix_blocking_recovery(eval_db):
    """A fix task QUEUED past the abandon threshold (30 min) without ever
    being claimed must be cancelled when CI backstop recovery runs — without
    this the recovery review's dedup guard defers and the parent gets
    hard-killed instead of recovered.

    Regression for task b215fe9e: review b2f19dce died without verdict;
    fix 36406295 queued for 1h waiting for an account; recovery review
    deferred behind it; parent backstopped after 1.7h.
    """
    project = Project(name="Backstop Supersede", user_id="user1")
    eval_db.add(project)
    await eval_db.commit()
    await eval_db.refresh(project)

    parent = Task(
        project_id=project.id,
        payloads=[{"type": "research"}],
        status=TaskStatus.EVALUATING,
    )
    eval_db.add(parent)
    await eval_db.commit()
    await eval_db.refresh(parent)

    stuck_fix = Task(
        project_id=project.id,
        payloads=[
            {
                "type": "fix",
                "pr_number": 90001,
                "original_task_id": str(parent.id),
            }
        ],
        status=TaskStatus.QUEUED,
        last_heartbeat=None,
        started_at=None,
    )
    stuck_fix.created_at = _naive_utc_ago(minutes=60)  # well past 30-min threshold
    eval_db.add(stuck_fix)
    await eval_db.commit()
    await eval_db.refresh(stuck_fix)

    superseded_count = await _supersede_long_queued_pipeline_tasks(
        parent,
        eval_db,
        pr_number=90001,
        original_task_id=str(parent.id),
    )
    await eval_db.commit()

    assert superseded_count == 1
    await eval_db.refresh(stuck_fix)
    assert stuck_fix.status == TaskStatus.CANCELLED
    assert "Superseded by CI backstop recovery" in (
        stuck_fix.latest_status_message or ""
    )


@pytest.mark.asyncio
async def test_supersede_skips_recently_queued_fix(eval_db):
    """A fix task queued within the 30-min freshness window is not yet
    'abandoned' — could legitimately be waiting on capacity. Leave alone."""
    project = Project(name="Backstop Fresh Queue", user_id="user1")
    eval_db.add(project)
    await eval_db.commit()
    await eval_db.refresh(project)

    parent = Task(
        project_id=project.id,
        payloads=[{"type": "research"}],
        status=TaskStatus.EVALUATING,
    )
    eval_db.add(parent)
    await eval_db.commit()
    await eval_db.refresh(parent)

    fresh_fix = Task(
        project_id=project.id,
        payloads=[
            {
                "type": "fix",
                "pr_number": 90002,
                "original_task_id": str(parent.id),
            }
        ],
        status=TaskStatus.QUEUED,
        last_heartbeat=None,
        started_at=None,
    )
    fresh_fix.created_at = _naive_utc_ago(minutes=10)  # fresh
    eval_db.add(fresh_fix)
    await eval_db.commit()
    await eval_db.refresh(fresh_fix)

    superseded_count = await _supersede_long_queued_pipeline_tasks(
        parent,
        eval_db,
        pr_number=90002,
        original_task_id=str(parent.id),
    )

    assert superseded_count == 0
    await eval_db.refresh(fresh_fix)
    assert fresh_fix.status == TaskStatus.QUEUED


@pytest.mark.asyncio
async def test_supersede_skips_already_claimed_fix(eval_db):
    """A fix task that has actually started (heartbeated or set started_at)
    is real work — the abandon threshold only applies to never-claimed
    QUEUED tasks. Don't cancel work in progress."""
    project = Project(name="Backstop Claimed Fix", user_id="user1")
    eval_db.add(project)
    await eval_db.commit()
    await eval_db.refresh(project)

    parent = Task(
        project_id=project.id,
        payloads=[{"type": "research"}],
        status=TaskStatus.EVALUATING,
    )
    eval_db.add(parent)
    await eval_db.commit()
    await eval_db.refresh(parent)

    # QUEUED but with last_heartbeat set (e.g., requeued mid-flight)
    requeued_fix = Task(
        project_id=project.id,
        payloads=[
            {
                "type": "fix",
                "pr_number": 90003,
                "original_task_id": str(parent.id),
            }
        ],
        status=TaskStatus.QUEUED,
        last_heartbeat=_naive_utc_ago(minutes=5),
        started_at=_naive_utc_ago(minutes=10),
    )
    requeued_fix.created_at = _naive_utc_ago(minutes=60)
    eval_db.add(requeued_fix)
    await eval_db.commit()
    await eval_db.refresh(requeued_fix)

    superseded_count = await _supersede_long_queued_pipeline_tasks(
        parent,
        eval_db,
        pr_number=90003,
        original_task_id=str(parent.id),
    )

    assert superseded_count == 0
    await eval_db.refresh(requeued_fix)
    assert requeued_fix.status == TaskStatus.QUEUED


@pytest.mark.asyncio
async def test_supersede_only_matches_chain(eval_db):
    """Only review/fix tasks matching the parent's PR number AND
    original_task_id get cancelled. Tasks on a different chain are
    untouched even when long-queued."""
    project = Project(name="Backstop Chain Match", user_id="user1")
    eval_db.add(project)
    await eval_db.commit()
    await eval_db.refresh(project)

    parent = Task(
        project_id=project.id,
        payloads=[{"type": "research"}],
        status=TaskStatus.EVALUATING,
    )
    eval_db.add(parent)
    await eval_db.commit()
    await eval_db.refresh(parent)

    # Different PR
    other_pr_fix = Task(
        project_id=project.id,
        payloads=[
            {
                "type": "fix",
                "pr_number": 99999,
                "original_task_id": str(parent.id),
            }
        ],
        status=TaskStatus.QUEUED,
    )
    other_pr_fix.created_at = _naive_utc_ago(minutes=60)
    eval_db.add(other_pr_fix)

    # Different chain (different original_task_id) on same PR
    other_chain_fix = Task(
        project_id=project.id,
        payloads=[
            {
                "type": "fix",
                "pr_number": 90004,
                "original_task_id": "some-other-chain-id",
            }
        ],
        status=TaskStatus.QUEUED,
    )
    other_chain_fix.created_at = _naive_utc_ago(minutes=60)
    eval_db.add(other_chain_fix)
    await eval_db.commit()

    superseded_count = await _supersede_long_queued_pipeline_tasks(
        parent,
        eval_db,
        pr_number=90004,
        original_task_id=str(parent.id),
    )

    assert superseded_count == 0
    await eval_db.refresh(other_pr_fix)
    await eval_db.refresh(other_chain_fix)
    assert other_pr_fix.status == TaskStatus.QUEUED
    assert other_chain_fix.status == TaskStatus.QUEUED


# ── Recoverable-FAILED defer tests ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_evaluating_hard_timeout_defers_with_recoverable_failed_child(
    eval_db, mocker
):
    """Parent past 6h with a child FAILED + PROVISION_FAILURE must NOT be killed.
    The child is eligible for resurrection sweep / operator requeue, so cascade-
    cancelling it would destroy salvageable work. Regression for incident
    2026-04-29 (PRs #3450/#3408 + parents c30701d3/ddd987fc)."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock)
    from app.shared_state import runtime_config as _rc

    mocker.patch.dict(_rc, {"evaluating_fail_hours": 6})

    project = Project(name="Recoverable Defer", user_id="user1")
    eval_db.add(project)
    await eval_db.commit()
    await eval_db.refresh(project)

    parent = Task(
        project_id=project.id,
        payloads=[{"type": "research", "prompt": "research parent"}],
        status=TaskStatus.EVALUATING,
        status_log=[
            {
                "t": _naive_utc_ago(hours=7).isoformat(),
                "msg": "Status changed: IN_PROGRESS → EVALUATING",
            }
        ],
    )
    eval_db.add(parent)
    await eval_db.commit()
    await eval_db.refresh(parent)

    # Need at least one non-terminal child to bypass the missed-rollup path
    # (all-children-terminal fires _check_and_trigger_rollup before the 6h
    # branch is reached).
    in_progress_child = Task(
        project_id=project.id,
        parent_task_id=parent.id,
        payloads=[{"type": "create", "prompt": "active child"}],
        status=TaskStatus.IN_PROGRESS,
    )
    eval_db.add(in_progress_child)
    failed_child = Task(
        project_id=project.id,
        parent_task_id=parent.id,
        payloads=[{"type": "create", "prompt": "create child"}],
        status=TaskStatus.FAILED,
        context={"failure_category": "PROVISION_FAILURE"},
    )
    eval_db.add(failed_child)
    await eval_db.commit()

    await _check_evaluating_timeouts()

    await eval_db.refresh(parent)
    assert parent.status == TaskStatus.EVALUATING, (
        f"Expected parent to stay EVALUATING (deferred), got {parent.status}: "
        f"{parent.latest_status_message!r}"
    )
    # Defer marker recorded
    assert (parent.context or {}).get("_eval_timeout_first_defer_at") is not None


@pytest.mark.asyncio
async def test_evaluating_hard_timeout_kills_when_failed_child_is_code_error(
    eval_db, mocker
):
    """A FAILED child with CODE_ERROR (not infra-recoverable) must NOT trigger
    the defer path — the existing 6h kill path runs as before."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock)
    from app.shared_state import runtime_config as _rc

    mocker.patch.dict(_rc, {"evaluating_fail_hours": 6})

    project = Project(name="Code Error Kill", user_id="user1")
    eval_db.add(project)
    await eval_db.commit()
    await eval_db.refresh(project)

    parent = Task(
        project_id=project.id,
        payloads=[{"type": "research", "prompt": "research parent"}],
        status=TaskStatus.EVALUATING,
        status_log=[
            {
                "t": _naive_utc_ago(hours=7).isoformat(),
                "msg": "Status changed: IN_PROGRESS → EVALUATING",
            }
        ],
    )
    eval_db.add(parent)
    await eval_db.commit()
    await eval_db.refresh(parent)

    # Need a non-terminal child to bypass the missed-rollup path; a FAILED
    # CODE_ERROR child alone would be all-terminal and trigger rollup recovery.
    in_progress_child = Task(
        project_id=project.id,
        parent_task_id=parent.id,
        payloads=[{"type": "create", "prompt": "active child"}],
        status=TaskStatus.IN_PROGRESS,
    )
    eval_db.add(in_progress_child)
    code_error_child = Task(
        project_id=project.id,
        parent_task_id=parent.id,
        payloads=[{"type": "create", "prompt": "code error child"}],
        status=TaskStatus.FAILED,
        context={"failure_category": "CODE_ERROR"},
    )
    eval_db.add(code_error_child)
    await eval_db.commit()

    await _check_evaluating_timeouts()

    await eval_db.refresh(parent)
    assert parent.status == TaskStatus.FAILED, (
        f"Expected FAILED (no recoverable signal), got {parent.status}: "
        f"{parent.latest_status_message!r}"
    )


@pytest.mark.asyncio
async def test_evaluating_defer_window_exhausted_kills(eval_db, mocker):
    """A parent that has been deferring for >24h despite recoverable FAILED
    children must eventually fall through to the kill path — the defer is
    bounded so a truly stuck operator-intervention case still terminates."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock)
    from app.shared_state import runtime_config as _rc

    mocker.patch.dict(_rc, {"evaluating_fail_hours": 6})

    project = Project(name="Defer Window Exhausted", user_id="user1")
    eval_db.add(project)
    await eval_db.commit()
    await eval_db.refresh(project)

    # Defer marker recorded 25h ago — past the 24h window
    parent = Task(
        project_id=project.id,
        payloads=[{"type": "research", "prompt": "long-deferred"}],
        status=TaskStatus.EVALUATING,
        status_log=[
            {
                "t": _naive_utc_ago(hours=32).isoformat(),
                "msg": "Status changed: IN_PROGRESS → EVALUATING",
            }
        ],
        context={"_eval_timeout_first_defer_at": _naive_utc_ago(hours=25).isoformat()},
    )
    eval_db.add(parent)
    await eval_db.commit()
    await eval_db.refresh(parent)

    failed_child = Task(
        project_id=project.id,
        parent_task_id=parent.id,
        payloads=[{"type": "create", "prompt": "still-failed child"}],
        status=TaskStatus.FAILED,
        context={"failure_category": "PROVISION_FAILURE"},
    )
    eval_db.add(failed_child)
    # Need at least one non-terminal child or rollup would fire; add one
    in_progress_child = Task(
        project_id=project.id,
        parent_task_id=parent.id,
        payloads=[{"type": "create", "prompt": "active child"}],
        status=TaskStatus.IN_PROGRESS,
    )
    eval_db.add(in_progress_child)
    await eval_db.commit()

    await _check_evaluating_timeouts()

    await eval_db.refresh(parent)
    assert parent.status == TaskStatus.FAILED, (
        f"Expected FAILED after defer-window exhaustion, got {parent.status}: "
        f"{parent.latest_status_message!r}"
    )


@pytest.mark.asyncio
async def test_chain_has_recoverable_failed_work_via_descendant_pipeline_task(eval_db):
    """A FAILED review/fix pipeline task whose original_task_id points at one
    of the parent's direct children, with an infra failure_category, must
    register as recoverable chain work. This is the c30701d3 shape: research
    parent → create child (COMPLETED-but-unmerged) → fix task FAILED with
    PROVISION_FAILURE."""
    project = Project(name="Chain Recoverable Descendant", user_id="user1")
    eval_db.add(project)
    await eval_db.commit()
    await eval_db.refresh(project)

    parent = Task(
        project_id=project.id,
        payloads=[{"type": "research"}],
        status=TaskStatus.EVALUATING,
    )
    eval_db.add(parent)
    await eval_db.commit()
    await eval_db.refresh(parent)

    create_child = Task(
        project_id=project.id,
        parent_task_id=parent.id,
        payloads=[{"type": "create"}],
        status=TaskStatus.COMPLETED,
        context={"pr_merged": False},
    )
    eval_db.add(create_child)
    await eval_db.commit()
    await eval_db.refresh(create_child)

    fix_descendant = Task(
        project_id=project.id,
        payloads=[
            {
                "type": "fix",
                "pr_number": 80001,
                "original_task_id": str(create_child.id),
            }
        ],
        status=TaskStatus.FAILED,
        context={"failure_category": "PROVISION_FAILURE"},
    )
    eval_db.add(fix_descendant)
    await eval_db.commit()

    assert await _chain_has_recoverable_failed_work(parent, eval_db) is True


@pytest.mark.asyncio
async def test_chain_has_recoverable_failed_work_ignores_code_error(eval_db):
    """A FAILED descendant with CODE_ERROR (not in INFRA_CLASSES) must NOT
    register as recoverable — those have already burned retry budget on real
    code defects."""
    project = Project(name="Chain Not Recoverable", user_id="user1")
    eval_db.add(project)
    await eval_db.commit()
    await eval_db.refresh(project)

    parent = Task(
        project_id=project.id,
        payloads=[{"type": "research"}],
        status=TaskStatus.EVALUATING,
    )
    eval_db.add(parent)
    await eval_db.commit()
    await eval_db.refresh(parent)

    create_child = Task(
        project_id=project.id,
        parent_task_id=parent.id,
        payloads=[{"type": "create"}],
        status=TaskStatus.FAILED,
        context={"failure_category": "CODE_ERROR"},
    )
    eval_db.add(create_child)
    await eval_db.commit()

    assert await _chain_has_recoverable_failed_work(parent, eval_db) is False


# ── C3 fix C: CI-backstop lost-update guard ───────────────────────────────────


@pytest.mark.asyncio
async def test_ci_backstop_does_not_clobber_concurrently_resolved_row(eval_db, mocker):
    """C3 lost-update seam: _check_evaluating_timeouts now runs in its OWN
    background task and can interleave with the orchestrator CI-poll loop on the
    SAME ci_waiting EVALUATING row. The backstop reaches its FAILED decision from
    a NON-locked read, then several awaits later commits FAILED. If the CI-poll
    loop RESOLVES the row (e.g. merge → COMPLETED + ci_waiting cleared) in that
    window, the backstop MUST NOT overwrite that resolution.

    This test injects exactly that interleaving: it patches the await that runs
    just before the FAILED decision (_chain_has_active_pipeline_work) to flip the
    SAME row to COMPLETED on a SEPARATE session and commit — simulating the CI
    loop winning the race. With the fix C lock+re-validate, the backstop sees the
    row is no longer EVALUATING under the lock and DEFERS instead of clobbering.

    Pre-fix (no lock/re-validate): the backstop would blindly set status=FAILED
    on its stale in-memory instance and commit, clobbering the COMPLETED the CI
    loop just wrote.
    """
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch("app.vm_lifecycle._ensure_vm_deleted", new_callable=AsyncMock)
    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock)

    from app import services as _services

    project = Project(name="CI Backstop Race", user_id="user1")
    eval_db.add(project)
    await eval_db.commit()
    await eval_db.refresh(project)

    # CI-waiting task, 90 min into the wait (> 60-min backstop), ci_passed=False
    # so it falls straight through to the FAILED block (no recovery branch).
    ci_started = _naive_utc_ago(minutes=90).isoformat()
    task = Task(
        project_id=project.id,
        payloads=[{"type": "review", "prompt": "race task"}],
        status=TaskStatus.EVALUATING,
        context={
            "review_loop": {
                "ci_waiting": True,
                "ci_wait_started": ci_started,
                "pr_number": 4242,
            }
        },
        status_log=[
            {
                "t": _naive_utc_ago(minutes=90).isoformat(),
                "msg": "Status changed: IN_PROGRESS → EVALUATING",
            }
        ],
    )
    eval_db.add(task)
    await eval_db.commit()
    await eval_db.refresh(task)
    task_id = task.id

    interleaved = {"fired": False}

    async def _resolve_row_then_report_idle(_task, _session):
        # Simulate the orchestrator CI-poll loop committing a merge resolution
        # on the SAME row in the window between the backstop's read and its
        # commit. The commit lands in the DB so the fix's lock+re-read (which
        # uses populate_existing=True, refreshing from the row) observes the
        # post-resolution state. (We commit on the sweep's own session here:
        # the in-memory SQLite StaticPool shares a single connection across
        # sessions, so a second concurrent session would self-deadlock; the
        # FOR UPDATE re-validate is exercised on real Postgres in CI. Either
        # way the assertion below proves the backstop did not clobber.)
        if not interleaved["fired"]:
            interleaved["fired"] = True
            row = await _session.get(Task, task_id)
            row.status = TaskStatus.COMPLETED
            rl = dict((row.context or {}).get("review_loop", {}))
            rl["ci_waiting"] = False
            rl["ci_passed"] = True
            rl["review_approved"] = True
            row.context = {**(row.context or {}), "review_loop": rl}
            from sqlalchemy.orm.attributes import flag_modified

            flag_modified(row, "context")
            row.latest_status_message = "CI loop: PR merged, rollup complete"
            _session.add(row)
            await _session.commit()
        # Report "no active pipeline work" so the sweep proceeds to the FAILED
        # decision (where the lock+re-validate must now defer).
        return False

    mocker.patch.object(
        _services,
        "_chain_has_active_pipeline_work",
        side_effect=_resolve_row_then_report_idle,
    )

    await _check_evaluating_timeouts()

    assert interleaved["fired"], "interleave hook did not run — test is vacuous"

    # The CI loop's COMPLETED resolution must survive — the backstop must NOT
    # have clobbered it to FAILED.
    async with _services.AsyncSessionLocal() as verify:
        final = await verify.get(Task, task_id)
        assert final.status == TaskStatus.COMPLETED, (
            f"Lost-update: backstop clobbered the CI-loop's COMPLETED resolution "
            f"with {final.status} (msg={final.latest_status_message!r})"
        )


# ══════════════════════════════════════════════════════════════════════════════
# C6 rework (A): the _pr_state_check_pending CONSUMER is not one-shot
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_pr_state_check_consumer_respawn_failure_persists_marker(
    eval_db, mocker
):
    """C6 rework seam (A): the services ``_check_evaluating_timeouts`` consumer
    that recovers ``_pr_state_check_pending`` must itself NOT be one-shot.

    Scenario the classifier targets — a SUSTAINED transient outage:
      * pass 1: the PRODUCER (agent_status / reaper) keeps the marker set
        because its own review spawn failed.
      * pass 2: this consumer re-checks the PR (still open) and re-spawns the
        review — but the spawn ALSO fails (the outage hasn't cleared).

    Pre-rework the consumer popped the marker up front (services.py ~1081) and
    committed ``_ctx2`` UNCONDITIONALLY, so the failed re-spawn DROPPED the
    marker → the childless EVALUATING parent was abandoned to the 6h/48h
    backstop (the reviewer verified ``marker_present:None`` after the 2nd
    failed spawn). The fix re-stages the marker on spawn failure so the
    consumer self-retries on the next sweep.

    Would FAIL pre-rework: ``_pr_state_check_pending`` would be absent after the
    failed re-spawn.
    """
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.services.broadcast_to_project", new_callable=AsyncMock, create=True
    )

    # _bounded wraps GH calls in asyncio.wait_for, which detaches the coroutine
    # into a new task — under the in-memory SQLite test session that loses the
    # greenlet context and raises MissingGreenlet on any DB IO (a harness-only
    # artifact; the production async engine is unaffected). Pass through so the
    # consumer's GH-lookup + spawn path is exercised on this loop.
    async def _passthrough_bounded(coro, *_a, **_k):
        return await coro

    mocker.patch("app.services._bounded", side_effect=_passthrough_bounded)
    # PR is still OPEN — the consumer takes the re-spawn branch.
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new=AsyncMock(return_value=("tok", "owner", "repo")),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(return_value={"state": "open", "merged": False}),
    )
    # The re-spawn FAILS (outage not cleared).
    spawn_mock = mocker.patch(
        "app.review_pipeline._spawn_pipeline_task",
        new=AsyncMock(side_effect=RuntimeError("review spawn still failing")),
    )

    project = Project(name="PR-state consumer retry", user_id="user-consumer-retry")
    eval_db.add(project)
    await eval_db.commit()
    await eval_db.refresh(project)

    parent = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "do work"}],
        status=TaskStatus.EVALUATING,
        # Producer left the recovery marker set on its own failed spawn.
        context={
            "_pr_state_check_pending": True,
            "review_loop": {"pr_number": 9090, "original_task_id": "x"},
        },
    )
    eval_db.add(parent)
    await eval_db.commit()
    await eval_db.refresh(parent)

    await _check_evaluating_timeouts()

    await eval_db.refresh(parent)
    spawn_mock.assert_awaited()
    # Parent stays EVALUATING (recoverable), NOT abandoned.
    assert parent.status == TaskStatus.EVALUATING, (
        f"consumer must leave the parent EVALUATING on re-spawn failure, got "
        f"{parent.status}"
    )
    # The marker MUST persist so the NEXT sweep retries — this is the whole
    # point of the rework. Pre-fix it would have been dropped.
    assert (parent.context or {}).get("_pr_state_check_pending") is True, (
        "consumer must re-stage _pr_state_check_pending on re-spawn failure so "
        "it self-retries next sweep; otherwise the childless EVALUATING parent "
        "is abandoned to the 6h/48h backstop"
    )


@pytest.mark.asyncio
async def test_pr_state_check_consumer_respawn_success_clears_marker(
    eval_db, mocker
):
    """C6 rework guard (A): on a CONFIRMED re-spawn success the consumer must
    clear ``_pr_state_check_pending`` — a childed EVALUATING parent is a
    normally parked parent, and a leftover marker would make the next sweep
    spawn a SECOND review."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.services.broadcast_to_project", new_callable=AsyncMock, create=True
    )

    async def _passthrough_bounded(coro, *_a, **_k):
        return await coro

    mocker.patch("app.services._bounded", side_effect=_passthrough_bounded)
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new=AsyncMock(return_value=("tok", "owner", "repo")),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(return_value={"state": "open", "merged": False}),
    )
    spawn_mock = mocker.patch(
        "app.review_pipeline._spawn_pipeline_task",
        new=AsyncMock(return_value=(None, True)),
    )

    project = Project(name="PR-state consumer ok", user_id="user-consumer-ok")
    eval_db.add(project)
    await eval_db.commit()
    await eval_db.refresh(project)

    parent = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "do work"}],
        status=TaskStatus.EVALUATING,
        context={
            "_pr_state_check_pending": True,
            "review_loop": {"pr_number": 9091, "original_task_id": "x"},
        },
    )
    eval_db.add(parent)
    await eval_db.commit()
    await eval_db.refresh(parent)

    await _check_evaluating_timeouts()

    await eval_db.refresh(parent)
    spawn_mock.assert_awaited_once()
    assert not (parent.context or {}).get("_pr_state_check_pending"), (
        "consumer must clear the marker on confirmed re-spawn success"
    )


@pytest.mark.asyncio
async def test_pr_state_check_consumer_merged_promotion_target_alerts_without_pr_merged(
    eval_db, mocker
):
    """Merged promotion-target recovery completes the task but must not mark it landed on integration."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.services.broadcast_to_project", new_callable=AsyncMock, create=True
    )
    dispatch_mock = mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )

    async def _passthrough_bounded(coro, *_a, **_k):
        return await coro

    mocker.patch("app.services._bounded", side_effect=_passthrough_bounded)
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new=AsyncMock(return_value=("tok", "owner", "repo")),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={
                "state": "closed",
                "merged": True,
                "base": {"ref": "main"},
            }
        ),
    )

    project = Project(
        name="PR-state consumer promotion merge",
        user_id="user-consumer-promotion",
        integration_branch="automain",
        promotion_target="main",
    )
    eval_db.add(project)
    await eval_db.commit()
    await eval_db.refresh(project)

    parent = Task(
        project_id=project.id,
        payloads=[
            {
                "type": "create",
                "prompt": "do work",
                "base_branch": "automain",
            }
        ],
        status=TaskStatus.EVALUATING,
        context={
            "_pr_state_check_pending": True,
            "review_loop": {"pr_number": 9092, "base_branch": "automain"},
        },
    )
    eval_db.add(parent)
    await eval_db.commit()
    await eval_db.refresh(parent)

    await _check_evaluating_timeouts()
    await asyncio.sleep(0)

    await eval_db.refresh(parent)
    assert parent.status == TaskStatus.COMPLETED
    assert (parent.context or {}).get("pr_merged") is None
    assert not (parent.context or {}).get("_pr_state_check_pending")
    alerts = (
        (
            await eval_db.execute(select(Alert).where(Alert.task_id == parent.id))
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 1
    assert alerts[0].severity == AlertSeverity.ERROR
    assert alerts[0].title == "Merged PR landed on promotion target before integration"
    approval_calls = [
        c for c in dispatch_mock.call_args_list if c.args[1] == "APPROVAL_NEEDED"
    ]
    assert len(approval_calls) == 1
    assert approval_calls[0].args[2]["pr_number"] == 9092


@pytest.mark.asyncio
async def test_pr_state_check_consumer_same_branch_merge_sets_pr_merged(
    eval_db, mocker
):
    """Same-branch projects still treat a merged PR as landed on integration."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.services.broadcast_to_project", new_callable=AsyncMock, create=True
    )
    dispatch_mock = mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )

    async def _passthrough_bounded(coro, *_a, **_k):
        return await coro

    mocker.patch("app.services._bounded", side_effect=_passthrough_bounded)
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new=AsyncMock(return_value=("tok", "owner", "repo")),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={
                "state": "closed",
                "merged": True,
                "base": {"ref": "main"},
            }
        ),
    )

    project = Project(
        name="PR-state consumer same-branch merge",
        user_id="user-consumer-same-branch",
        integration_branch="main",
        promotion_target="main",
    )
    eval_db.add(project)
    await eval_db.commit()
    await eval_db.refresh(project)

    parent = Task(
        project_id=project.id,
        payloads=[
            {
                "type": "create",
                "prompt": "do work",
                "base_branch": "automain",
            }
        ],
        status=TaskStatus.EVALUATING,
        context={
            "_pr_state_check_pending": True,
            "review_loop": {"pr_number": 9093, "base_branch": "automain"},
        },
    )
    eval_db.add(parent)
    await eval_db.commit()
    await eval_db.refresh(parent)

    await _check_evaluating_timeouts()
    await asyncio.sleep(0)

    await eval_db.refresh(parent)
    assert parent.status == TaskStatus.COMPLETED
    assert (parent.context or {}).get("pr_merged") is True
    alerts = (
        (
            await eval_db.execute(select(Alert).where(Alert.task_id == parent.id))
        )
        .scalars()
        .all()
    )
    assert alerts == []
    dispatch_mock.assert_not_called()


@pytest.mark.asyncio
async def test_pr_state_check_consumer_intermediate_rollup_stays_evaluating(
    eval_db, mocker
):
    """Merged custom staged rollup roots must await downstream landing."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.services.broadcast_to_project", new_callable=AsyncMock, create=True
    )
    dispatch_mock = mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )

    async def _passthrough_bounded(coro, *_a, **_k):
        return await coro

    mocker.patch("app.services._bounded", side_effect=_passthrough_bounded)
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new=AsyncMock(return_value=("tok", "owner", "repo")),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={
                "state": "closed",
                "merged": True,
                "base": {"ref": "release/candidate-1"},
            }
        ),
    )

    project = Project(
        name="PR-state consumer intermediate rollup",
        user_id="user-consumer-intermediate",
        integration_branch="automain",
        promotion_target="release/main",
        build_config={"staging_branch_prefixes": ["release/"]},
    )
    eval_db.add(project)
    await eval_db.commit()
    await eval_db.refresh(project)

    parent = Task(
        project_id=project.id,
        payloads=[
            {
                "type": "research",
                "prompt": "do work",
                "base_branch": "release/candidate-1",
            }
        ],
        status=TaskStatus.EVALUATING,
        context={
            "_pr_state_check_pending": True,
            "rollup_pr_number": 9094,
            "review_loop": {"pr_number": 9094, "base_branch": "release/candidate-1"},
        },
    )
    eval_db.add(parent)
    await eval_db.commit()
    await eval_db.refresh(parent)

    await _check_evaluating_timeouts()
    await asyncio.sleep(0)

    await eval_db.refresh(parent)
    assert parent.status == TaskStatus.EVALUATING
    assert (parent.context or {}).get("pr_merged") is None
    assert not (parent.context or {}).get("_pr_state_check_pending")
    assert "awaiting downstream landing" in (parent.latest_status_message or "")
    alerts = (
        (
            await eval_db.execute(select(Alert).where(Alert.task_id == parent.id))
        )
        .scalars()
        .all()
    )
    assert alerts == []
    dispatch_mock.assert_not_called()


# ── EVALUATING-STRAND BACKSTOP ─────────────────────────────────────────────


def _mock_strand_github(mocker, *, pr_state, pr_merged, reviews=None):
    """Mock the GitHub calls the strand backstop makes (token + PR fetch, and
    the FIX A reviews fetch used on the closed-not-merged path)."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    # The strand backstop lives in app.recovery_sweeps and broadcasts via
    # ``services.broadcast_to_project`` (a binding separate from
    # app.broadcast.broadcast_to_project), so patch it there too.
    mocker.patch(
        "app.services.broadcast_to_project", new_callable=AsyncMock, create=True
    )
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "owner", "repo"),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        return_value={"state": pr_state, "merged": pr_merged, "user": {}},
    )
    mocker.patch(
        "app.github.get_pull_request_reviews",
        new_callable=AsyncMock,
        return_value=reviews or [],
    )


async def _make_strand_task(eval_db, *, pr_number, extra_ctx=None):
    """A non-rollup review task stranded in EVALUATING with a conflict-fix
    spawned (the historical strand)."""
    project = Project(name="Strand Test", user_id="user1")
    eval_db.add(project)
    await eval_db.commit()
    await eval_db.refresh(project)

    ctx = {
        "review_loop": {
            "pr_number": pr_number,
            "conflict_fix_spawned": str(uuid.uuid4()),
        },
    }
    if extra_ctx:
        ctx.update(extra_ctx)
    task = Task(
        project_id=project.id,
        payloads=[{"type": "review", "pr_number": pr_number, "base_branch": "main"}],
        status=TaskStatus.EVALUATING,
        context=ctx,
    )
    eval_db.add(task)
    await eval_db.commit()
    await eval_db.refresh(task)
    return task


@pytest.mark.asyncio
async def test_strand_backstop_completes_on_merged_pr(eval_db, mocker):
    """A stranded (non-rollup) review with conflict_fix_spawned whose PR has
    MERGED out-of-band is terminalized COMPLETED."""
    _mock_strand_github(mocker, pr_state="closed", pr_merged=True)
    task = await _make_strand_task(eval_db, pr_number=8801)

    await _check_evaluating_timeouts()

    await eval_db.refresh(task)
    assert task.status == TaskStatus.COMPLETED, (
        f"Strand whose PR merged must become COMPLETED, got {task.status}"
    )
    assert (task.context or {}).get("pr_merged") is True


@pytest.mark.asyncio
async def test_strand_backstop_leaves_open_pr_evaluating(eval_db, mocker):
    """GUARDRAIL: a stranded review whose PR is still OPEN must be LEFT in
    EVALUATING — never completed while the PR is open."""
    _mock_strand_github(mocker, pr_state="open", pr_merged=False)
    task = await _make_strand_task(eval_db, pr_number=8802)

    await _check_evaluating_timeouts()

    await eval_db.refresh(task)
    assert task.status == TaskStatus.EVALUATING, (
        f"Open-PR strand must stay EVALUATING, got {task.status}"
    )


@pytest.mark.asyncio
async def test_strand_backstop_terminalizes_closed_unmerged_pr(eval_db, mocker):
    """A stranded review whose PR was CLOSED without merging is terminalized
    (here: genuine — no cleanup signal, no CHANGES_REQUESTED → FAILED)."""
    mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    _mock_strand_github(mocker, pr_state="closed", pr_merged=False, reviews=[])
    task = await _make_strand_task(eval_db, pr_number=8803)

    await _check_evaluating_timeouts()

    await eval_db.refresh(task)
    assert task.status in (TaskStatus.FAILED, TaskStatus.CANCELLED), (
        f"Closed-unmerged strand must be terminalized, got {task.status}"
    )
    # No cleanup signal + no CHANGES_REQUESTED → defaults to the genuine FAILED arm.
    assert task.status == TaskStatus.FAILED
    assert (task.context or {}).get("pr_closed_unmerged") is True


@pytest.mark.asyncio
async def test_strand_backstop_skips_rollup_parent(eval_db, mocker):
    """GUARDRAIL: a real rollup PARENT (_is_rollup_review=True) with a
    conflict-fix spawned must NOT be touched by the strand backstop — its own
    merge gate owns it. Even with the PR reported merged, the parent is left for
    ci_completion to promote (the backstop must not race it)."""
    # PR reported merged, but the rollup-parent guard must skip the backstop.
    _mock_strand_github(mocker, pr_state="closed", pr_merged=True)
    task = await _make_strand_task(
        eval_db,
        pr_number=8804,
        extra_ctx={"_is_rollup_review": True, "rollup_pr_number": 8804},
    )

    await _check_evaluating_timeouts()

    await eval_db.refresh(task)
    # The strand backstop is skipped for rollup parents; the parent stays
    # EVALUATING (no children terminal, not yet at the hard-timeout backstop).
    assert task.status == TaskStatus.EVALUATING, (
        f"Rollup parent must be skipped by the strand backstop, got {task.status}"
    )
    assert not (task.context or {}).get("pr_merged")


# ── Rollup all-cancelled finalizer routes through the shared verdict ───────────
# These exercise the REAL production entry point (the EVALUATING-timeout sweep →
# the un-mocked rollup finalizer) for a parent whose children are all CANCELLED,
# proving the rollup path now produces the SAME terminal verdict as the
# requeue/reaper path instead of the old quiet-CANCELLED divergence.


@pytest.mark.asyncio
async def test_eval_sweep_all_non_genuine_cancelled_finalizes_failed_scope_empty(
    eval_db, mocker
):
    """A parent whose children are ALL non-genuine cancels (superseded /
    replaced-by-retry / reassigned / scope-empty), reached via the real
    _check_evaluating_timeouts() sweep + un-mocked _check_and_trigger_rollup,
    finalizes FAILED with the SCOPE_EMPTY category, NO direction-health penalty,
    NO failure alert — and still clears rollup transient state + runs branch
    cleanup."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mock_cleanup = mocker.patch(
        "app.rollup_management._cleanup_branches", new_callable=AsyncMock
    )
    mock_alert = mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    mock_signals = mocker.patch("app.rollup_management._record_rollup_failure_signals")

    project = Project(name="Eval NonGenuine Cancel", user_id="user1")
    eval_db.add(project)
    await eval_db.commit()
    await eval_db.refresh(project)

    parent = Task(
        project_id=project.id,
        payloads=[{"type": "research", "prompt": "research parent"}],
        status=TaskStatus.EVALUATING,
        # A leftover rollup transient counter that MUST be scrubbed on finalize.
        context={"direction_id": "d-nongenuine", "_rollup_retries": 2},
    )
    eval_db.add(parent)
    await eval_db.commit()
    await eval_db.refresh(parent)

    # One child per non-genuine taxonomy class.
    non_genuine_children = [
        # cascade kill: replaced-by-retry clone took over
        Task(
            project_id=project.id,
            parent_task_id=parent.id,
            payloads=[{"type": "create", "prompt": "c1"}],
            status=TaskStatus.CANCELLED,
            context={"_replaced_by_task_id": str(uuid.uuid4())},
        ),
        # cascade kill: superseded message prefix
        Task(
            project_id=project.id,
            parent_task_id=parent.id,
            payloads=[{"type": "create", "prompt": "c2"}],
            status=TaskStatus.CANCELLED,
            latest_status_message="Superseded by fix on PR #1",
        ),
        # work landed elsewhere markers
        Task(
            project_id=project.id,
            parent_task_id=parent.id,
            payloads=[{"type": "create", "prompt": "c3"}],
            status=TaskStatus.CANCELLED,
            context={"failure_detail": "pr_closed_non_rejection"},
        ),
        # REASSIGNED / SCOPE_EMPTY handoffs that land CANCELLED
        Task(
            project_id=project.id,
            parent_task_id=parent.id,
            payloads=[{"type": "create", "prompt": "c4"}],
            status=TaskStatus.CANCELLED,
            context={"failure_category": "SCOPE_EMPTY"},
        ),
    ]
    for _c in non_genuine_children:
        eval_db.add(_c)
    await eval_db.commit()

    await _check_evaluating_timeouts()

    await eval_db.refresh(parent)
    assert parent.status == TaskStatus.FAILED
    assert parent.context.get("failure_category") == "SCOPE_EMPTY"
    # Non-genuine → no failure alert and no direction-health signal.
    mock_alert.assert_not_awaited()
    mock_signals.assert_not_called()
    # Rollup-path obligations the shared requeue helper does NOT perform.
    mock_cleanup.assert_awaited()
    assert "_rollup_retries" not in (parent.context or {})


@pytest.mark.asyncio
async def test_eval_sweep_all_genuine_cancelled_finalizes_failed_with_alert(
    eval_db, mocker
):
    """A parent whose children are ALL GENUINE cancels (no non-genuine markers —
    e.g. an operator manually cancelled every child), reached via the same real
    sweep + un-mocked finalizer, now finalizes FAILED (NOT the old quiet
    CANCELLED) with a failure alert + direction-health signal — matching the
    requeue/reaper verdict so the two sweeps cannot disagree."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mock_cleanup = mocker.patch(
        "app.rollup_management._cleanup_branches", new_callable=AsyncMock
    )
    mock_alert = mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    # The genuine-cancel branch now routes through the SAME shared
    # ``_record_parent_failure`` ding the requeue/reaper path uses (parity locked
    # by test_parent_terminal_parity) rather than the rollup-only
    # ``_record_rollup_failure_signals``.
    mock_parent_failure = mocker.patch(
        "app.task_lifecycle_requeue._record_parent_failure"
    )
    mock_signals = mocker.patch("app.rollup_management._record_rollup_failure_signals")

    project = Project(name="Eval Genuine Cancel", user_id="user1")
    eval_db.add(project)
    await eval_db.commit()
    await eval_db.refresh(project)

    parent = Task(
        project_id=project.id,
        payloads=[{"type": "research", "prompt": "research parent"}],
        status=TaskStatus.EVALUATING,
        context={"direction_id": "d-genuine", "_rollup_retries": 1},
    )
    eval_db.add(parent)
    await eval_db.commit()
    await eval_db.refresh(parent)

    for _i in range(2):
        eval_db.add(
            Task(
                project_id=project.id,
                parent_task_id=parent.id,
                payloads=[{"type": "create", "prompt": f"c{_i}"}],
                status=TaskStatus.CANCELLED,
            )
        )
    await eval_db.commit()

    await _check_evaluating_timeouts()

    await eval_db.refresh(parent)
    assert parent.status == TaskStatus.FAILED
    assert parent.context.get("failure_category") == "AGENT_TIMEOUT"
    # Genuine cancel is a terminal failure in BOTH sweeps — alert + the shared
    # parent-failure ding fire. The rollup-only signal helper is NOT used; the
    # finalizer mirrors the requeue/reaper path's ``_record_parent_failure``.
    mock_alert.assert_awaited()
    mock_parent_failure.assert_called_once()
    mock_signals.assert_not_called()
    mock_cleanup.assert_awaited()
    assert "_rollup_retries" not in (parent.context or {})
