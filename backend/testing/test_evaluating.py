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
import pytest
import pytest_asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy import select
from sqlmodel import SQLModel

from app.main import _check_evaluating_timeouts
from app.task_lifecycle import _check_and_trigger_rollup
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
        SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: self.visit_JSON(type_, **kw)

    _engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with _engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    _Session = sessionmaker(
        bind=_engine, class_=AsyncSession,
        autocommit=False, autoflush=False, expire_on_commit=False,
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
    mocker.patch("app.main.broadcast_to_project", new_callable=AsyncMock)
    mock_rollup = mocker.patch("app.task_lifecycle._check_and_trigger_rollup", new_callable=AsyncMock)

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
    mocker.patch("app.main.broadcast_to_project", new_callable=AsyncMock)

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
        {"t": evaluating_since.isoformat(), "msg": "Status changed: IN_PROGRESS → EVALUATING"}
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
    """Tasks with context['ci_waiting'] = True must be skipped entirely by the
    timeout sweep — not timed out even after many hours."""
    mocker.patch("app.main.broadcast_to_project", new_callable=AsyncMock)
    mock_rollup = mocker.patch("app.task_lifecycle._check_and_trigger_rollup", new_callable=AsyncMock)

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

    # Set an elapsed time that would normally trigger both the warning and the
    # hard-timeout, proving ci_waiting is evaluated before any time-based check.
    evaluating_since = _naive_utc_ago(hours=10)
    parent.status_log = [
        {"t": evaluating_since.isoformat(), "msg": "Status changed: IN_PROGRESS → EVALUATING"}
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
async def test_evaluating_warning_alert_created(eval_db, mocker):
    """A task EVALUATING for >2 hours (the warning threshold) with non-terminal
    children should produce an undismissed WARNING alert."""
    mocker.patch("app.main.broadcast_to_project", new_callable=AsyncMock)

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
        {"t": evaluating_since.isoformat(), "msg": "Status changed: IN_PROGRESS → EVALUATING"}
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
    mocker.patch("app.main.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch("app.task_lifecycle._cleanup_branches", new_callable=AsyncMock)

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
    mocker.patch("app.main.broadcast_to_project", new_callable=AsyncMock)

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
    mocker.patch("app.task_lifecycle._verify_pr_merged", new=mock_verify)

    await _check_evaluating_timeouts()

    # _check_evaluating_timeouts commits the session after calling _check_and_trigger_rollup,
    # which persists the pr_merged flag written by the mock.
    await eval_db.refresh(child)
    assert child.context.get("pr_merged") is True, (
        "Expected child context to have pr_merged=True after _verify_pr_merged succeeded"
    )
    mock_verify.assert_called_once()
