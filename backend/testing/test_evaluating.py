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
    mocker.patch("app.main._maybe_create_failure_alert", new_callable=AsyncMock)
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
    mocker.patch("app.main._maybe_create_failure_alert", new_callable=AsyncMock)
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
    mocker.patch("app.main._maybe_create_failure_alert", new_callable=AsyncMock)
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
