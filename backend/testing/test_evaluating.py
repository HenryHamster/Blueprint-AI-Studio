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
    _chain_has_unexhausted_optimistic_retry_work,
    _supersede_long_queued_pipeline_tasks,
)
from app.rollup_management import _check_and_trigger_rollup
from app.task_lifecycle import park_task
from app.models import (
    Direction,
    Project,
    Task,
    TaskStatus,
    Alert,
    AlertSeverity,
    TaskWakeRegistration,
    WakeAction,
    WakeDeliveryState,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _naive_utc_ago(**kwargs) -> datetime:
    """Return a naive UTC datetime (no tzinfo) that is ``kwargs`` in the past.

    _utcnow() in main.py strips tzinfo, so timestamps stored in status_log
    must also be naive for the arithmetic to work without a TypeError.
    """
    return (datetime.now(timezone.utc) - timedelta(**kwargs)).replace(tzinfo=None)


async def _parked_child(
    session, project_id, parent_id, *, wake_ref: str
) -> tuple[Task, TaskWakeRegistration]:
    """Create a PARKED child via the real park_task primitive."""
    child = Task(
        project_id=project_id,
        payloads=[{"type": "compute_experiment", "prompt": "parked child"}],
        status=TaskStatus.IN_PROGRESS,
        created_by="ORCHESTRATOR",
        parent_task_id=parent_id,
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
    )
    session.add(child)
    await session.commit()
    await session.refresh(child)
    reg = await park_task(
        session,
        child,
        wake_source_kind="compute_job_terminal",
        wake_source_ref=wake_ref,
        source_metadata={"compute_job_id": wake_ref},
        wake_action=WakeAction.RESUME,
        reason=f"waiting on compute job {wake_ref}",
    )
    await session.commit()
    await session.refresh(child)
    await session.refresh(reg)
    return child, reg


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
async def test_evaluating_all_parked_children_triggers_rollup(eval_db, mocker):
    """Parked children must count as settled for missed-rollup recovery."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mock_rollup = mocker.patch(
        "app.rollup_management._check_and_trigger_rollup", new_callable=AsyncMock
    )

    project = Project(name="Eval Parked Rollup Test", user_id="user1")
    eval_db.add(project)
    await eval_db.commit()
    await eval_db.refresh(project)

    parent = Task(
        project_id=project.id,
        payloads=[{"type": "research", "prompt": "parked parent"}],
        status=TaskStatus.EVALUATING,
    )
    eval_db.add(parent)
    await eval_db.commit()
    await eval_db.refresh(parent)

    await _parked_child(
        eval_db,
        project.id,
        parent.id,
        wake_ref=f"job-{uuid.uuid4().hex}",
    )

    await _check_evaluating_timeouts()

    mock_rollup.assert_called_once()


@pytest.mark.asyncio
async def test_evaluating_timeout_treats_parked_children_as_terminal_for_now(
    eval_db, mocker
):
    """PARKED children must survive both EVALUATING timeout branches.

    The CI-backstop and hard-timeout paths should both treat PARKED the same
    way they already treat BLOCKED: counted as settled for parent timeout
    accounting, but never cascade-cancelled just because the parent aged out.
    """
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    mocker.patch("app.vm_lifecycle._ensure_vm_deleted", new_callable=AsyncMock)

    from app.shared_state import runtime_config as _rc

    mocker.patch.dict(_rc, {"evaluating_fail_hours": 6})

    async def _seed_parent(
        *, ci_waiting: bool
    ) -> tuple[Task, Task, TaskWakeRegistration]:
        project = Project(name="Parked Timeout Guard", user_id="user1")
        eval_db.add(project)
        await eval_db.commit()
        await eval_db.refresh(project)

        parent = Task(
            project_id=project.id,
            payloads=[{"type": "research", "prompt": "parent"}],
            status=TaskStatus.EVALUATING,
            status_log=[
                {
                    "t": _naive_utc_ago(hours=7).isoformat(),
                    "msg": "Status changed: IN_PROGRESS → EVALUATING",
                }
            ],
            context={
                "review_loop": {
                    "ci_waiting": ci_waiting,
                    "ci_wait_started": _naive_utc_ago(minutes=90).isoformat(),
                    "pr_number": 88011 if ci_waiting else 88012,
                    "original_task_id": "parked-timeout",
                }
            },
        )
        eval_db.add(parent)
        await eval_db.commit()
        await eval_db.refresh(parent)

        parked_child, reg = await _parked_child(
            eval_db,
            project.id,
            parent.id,
            wake_ref=f"job-{uuid.uuid4().hex}",
        )
        eval_db.add(
            Task(
                project_id=project.id,
                payloads=[{"type": "create", "prompt": "active child"}],
                status=TaskStatus.IN_PROGRESS,
                created_by="ORCHESTRATOR",
                parent_task_id=parent.id,
            )
        )
        await eval_db.commit()
        return parent, parked_child, reg

    ci_parent, ci_parked_child, ci_reg = await _seed_parent(ci_waiting=True)
    await _check_evaluating_timeouts()
    await eval_db.refresh(ci_parent)
    await eval_db.refresh(ci_parked_child)
    await eval_db.refresh(ci_reg)

    assert ci_parent.status == TaskStatus.FAILED
    assert ci_parked_child.status == TaskStatus.PARKED
    assert ci_parked_child.parked_at is not None
    assert ci_parked_child.vm_instance_id is None
    assert ci_parked_child.park_reason is not None
    assert ci_parked_child.latest_status_message is None
    assert ci_reg.delivery_state == WakeDeliveryState.PENDING
    assert ci_reg.wake_source_kind == "compute_job_terminal"
    assert ci_reg.wake_action == WakeAction.RESUME

    hard_parent, hard_parked_child, hard_reg = await _seed_parent(ci_waiting=False)
    await _check_evaluating_timeouts()
    await eval_db.refresh(hard_parent)
    await eval_db.refresh(hard_parked_child)
    await eval_db.refresh(hard_reg)

    assert hard_parent.status == TaskStatus.FAILED
    assert hard_parked_child.status == TaskStatus.PARKED
    assert hard_parked_child.parked_at is not None
    assert hard_parked_child.vm_instance_id is None
    assert hard_parked_child.park_reason is not None
    assert hard_parked_child.latest_status_message is None
    assert hard_reg.delivery_state == WakeDeliveryState.PENDING
    assert hard_reg.wake_source_kind == "compute_job_terminal"
    assert hard_reg.wake_action == WakeAction.RESUME


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
async def test_evaluating_sweep_projection_loads_api_call_summary(eval_db, mocker):
    """Regression: the EVALUATING sweep's per-row load_only(..., raiseload=True)
    projection (recovery_evaluating.py Phase B) must include Task.api_call_summary.
    classify_failure → _stuck_llm_call_no_progress (failure_classification.py)
    reads it inside a broad ``try/except Exception: return False`` — when the
    projection omits the column the raiseload InvalidRequestError is silently
    swallowed and the silent-provider-hang → RATE_LIMIT reclassification is
    disabled for every failure classified by this sweep (misclassified as
    AGENT_TIMEOUT/CODE_ERROR, skewing retry budgets and analytics)."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    from app.shared_state import runtime_config as _rc

    mocker.patch.dict(_rc, {"evaluating_fail_hours": 6})

    project = Project(name="Projection Regression Test", user_id="user1")
    eval_db.add(project)
    await eval_db.commit()
    await eval_db.refresh(project)

    summary = {"llm_call_count": 3, "total_output_tokens": 0}
    parent = Task(
        project_id=project.id,
        payloads=[{"type": "research", "prompt": "long-running task"}],
        status=TaskStatus.EVALUATING,
        api_call_summary=summary,
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

    # A non-terminal child prevents the missed-rollup path from firing,
    # allowing the hard-timeout classify path to run instead.
    child = Task(
        project_id=project.id,
        parent_task_id=parent.id,
        payloads=[{"type": "create", "prompt": "stuck subtask"}],
        status=TaskStatus.IN_PROGRESS,
    )
    eval_db.add(child)
    await eval_db.commit()

    # Spy on classify_failure at the hard-timeout call site: read
    # api_call_summary off the SAME projected instance the sweep loaded.
    # Pre-fix this read raises raiseload's InvalidRequestError (which
    # _stuck_llm_call_no_progress would swallow, silently disabling the
    # reclassification); post-fix the value round-trips.
    from app.failure_classification import classify_failure as _real_classify

    seen: dict = {}

    def _spy(task, *args, **kwargs):
        if task.id == parent.id:
            seen["api_call_summary"] = task.api_call_summary
        return _real_classify(task, *args, **kwargs)

    mocker.patch("app.failure_classification.classify_failure", side_effect=_spy)

    await _check_evaluating_timeouts()

    await eval_db.refresh(parent)
    assert parent.status == TaskStatus.FAILED, (
        f"Expected FAILED but got {parent.status}"
    )
    assert seen.get("api_call_summary") == summary, (
        "classify_failure received a projected Task without api_call_summary "
        f"loaded (got {seen!r}) — the sweep projection must include the column"
    )


@pytest.mark.asyncio
async def test_evaluating_hard_timeout_preserves_parked_child(eval_db, mocker):
    """Regression: a PARKED child of a hard-timed-out EVALUATING parent must NOT be
    cascade-CANCELLED. PARKED is a durable non-terminal state (in-flight wake
    registration / compute-job result) and belongs in the sweep's TERMINAL preserve
    set alongside BLOCKED. An IN_PROGRESS sibling drives the hard-timeout cascade so
    we can assert the parked child survives while the active one is cancelled."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    from app.shared_state import runtime_config as _rc

    mocker.patch.dict(_rc, {"evaluating_fail_hours": 6})

    project = Project(name="Parked Preserve Test", user_id="user1")
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

    # An IN_PROGRESS sibling keeps not-all-children-settled so the hard-timeout
    # cascade path runs (rather than missed-rollup recovery).
    active_child = Task(
        project_id=project.id,
        parent_task_id=parent.id,
        payloads=[{"type": "create", "prompt": "stuck subtask"}],
        status=TaskStatus.IN_PROGRESS,
    )
    parked_child = Task(
        project_id=project.id,
        parent_task_id=parent.id,
        payloads=[{"type": "compute_experiment", "prompt": "parked on compute wake"}],
        status=TaskStatus.PARKED,
    )
    eval_db.add_all([active_child, parked_child])
    await eval_db.commit()
    await eval_db.refresh(active_child)
    await eval_db.refresh(parked_child)

    await _check_evaluating_timeouts()

    await eval_db.refresh(parent)
    await eval_db.refresh(active_child)
    await eval_db.refresh(parked_child)
    assert parent.status == TaskStatus.FAILED, (
        f"Expected parent FAILED but got {parent.status}"
    )
    assert active_child.status == TaskStatus.CANCELLED, (
        f"Expected IN_PROGRESS sibling CANCELLED but got {active_child.status}"
    )
    assert parked_child.status == TaskStatus.PARKED, (
        f"PARKED child must be preserved from cascade-cancel, got {parked_child.status}"
    )


@pytest.mark.asyncio
async def test_evaluating_hard_timeout_broadcasts_terminal_literals_when_expired(
    eval_db, expire_fidelity, mocker
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
        # Simulate a production rollback that reverts pending changes and
        # expires all tracked instances (rollback always expires, regardless
        # of expire_on_commit). After this, the handler's pending status
        # mutations (task.status=FAILED, child.status=CANCELLED) are
        # reverted in the DB, and all loaded attributes (including FK and
        # PK) are expired. The broadcast must use FAILED/CANCELLED literals
        # and pre-captured identifiers — not post-rollback lazy-loads that
        # would return stale pre-timeout status or crash MissingGreenlet.
        await session.rollback()

    mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", side_effect=_expire_alert
    )

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


# ── Fix 1: narrowed sibling lock — live siblings read-only, idempotency kept ──


@pytest.mark.asyncio
async def test_rollup_live_sibling_keeps_parent_evaluating_unmutated(eval_db, mocker):
    """Fix 1: with a LIVE (IN_PROGRESS) sibling present, the rollup must still
    WAIT (active_count > 0 → parent stays EVALUATING) and must NOT mutate the live
    sibling — it is only READ for the tally. This pins the invariant that lets the
    sibling lock be narrowed away from live children (so their heartbeats never
    409): the rollup never writes a live child, so it never needs to lock one."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)

    project = Project(name="Live Sibling Test", user_id="user1")
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

    # One terminal (COMPLETED, merged) child + one LIVE (IN_PROGRESS) sibling.
    done_child = Task(
        project_id=project.id,
        parent_task_id=parent.id,
        payloads=[{"type": "create", "prompt": "done"}],
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
    )
    live_child = Task(
        project_id=project.id,
        parent_task_id=parent.id,
        payloads=[{"type": "create", "prompt": "still working"}],
        status=TaskStatus.IN_PROGRESS,
        last_heartbeat=datetime.now(timezone.utc).replace(tzinfo=None),
        context={"sentinel": "untouched"},
    )
    eval_db.add(done_child)
    eval_db.add(live_child)
    await eval_db.commit()
    await eval_db.refresh(live_child)

    # Trigger on the terminal child's completion event.
    await _check_and_trigger_rollup(done_child, eval_db)

    # active_count > 0 (the live sibling) → rollup waits, parent stays EVALUATING.
    await eval_db.refresh(parent)
    assert parent.status == TaskStatus.EVALUATING

    # The live sibling was only READ — never mutated (status + context intact).
    await eval_db.refresh(live_child)
    assert live_child.status == TaskStatus.IN_PROGRESS
    assert (live_child.context or {}).get("sentinel") == "untouched"


@pytest.mark.asyncio
async def test_rollup_concurrent_terminal_events_respawn_at_most_once(eval_db, mocker):
    """Fix 1 idempotency: two back-to-back rollup invocations (simulating two
    sibling-terminal events firing near-simultaneously) on a stale-unmerged child
    must respawn the review AT MOST once. The parent lock — not the all-children
    lock — provides this guarantee, so narrowing the children lock does not open a
    double-respawn. The respawn-count bump persisted under the parent lock makes
    the second invocation see a non-stale (already-respawned) child."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    # _verify_pr_merged: child PR is NOT merged (so it stays in `unmerged`).
    mocker.patch(
        "app.rollup_management._verify_pr_merged",
        new=AsyncMock(return_value=False),
    )

    # Track spawn calls; return a distinct new task id so respawned += 1 fires and
    # the respawn-count bump persists (was_spawned=True).
    spawn_calls = {"n": 0}

    async def _mock_spawn(child, kind, session, pr_number, branch, base_branch, **kw):
        spawn_calls["n"] += 1
        new_task = Task(
            project_id=child.project_id,
            parent_task_id=child.parent_task_id,
            payloads=[{"type": "review", "prompt": "review"}],
            status=TaskStatus.QUEUED,
        )
        session.add(new_task)
        await session.flush()
        return new_task, True

    mocker.patch(
        "app.rollup_management._spawn_pipeline_task",
        new=AsyncMock(side_effect=_mock_spawn),
    )

    from app.models import Artifact, ArtifactType

    project = Project(name="Concurrent Respawn Test", user_id="user1")
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

    # A COMPLETED child whose PR is not merged and is STALE (>10min) so the
    # respawn path fires. updated_at must be old — set it explicitly.
    stale_when = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=20)
    child = Task(
        project_id=project.id,
        parent_task_id=parent.id,
        payloads=[{"type": "create", "prompt": "unmerged stale"}],
        status=TaskStatus.COMPLETED,
        context={},
    )
    eval_db.add(child)
    await eval_db.commit()
    await eval_db.refresh(child)
    child.updated_at = stale_when
    eval_db.add(child)
    # PR artifact so the respawn path can parse a pr_number.
    eval_db.add(
        Artifact(
            task_id=child.id,
            artifact_type=ArtifactType.PULL_REQUEST,
            pr_url="https://github.com/owner/repo/pull/4242",
        )
    )
    await eval_db.commit()

    # Two near-simultaneous terminal events. Each invocation re-reads the child;
    # the first respawns + bumps _review_respawn_count, the second sees the bumped
    # child (no longer eligible for another respawn within the same staleness).
    await _check_and_trigger_rollup(child, eval_db)
    # Re-stale the child's updated_at so a NAIVE (lock-free) second pass would
    # respawn again if idempotency were broken — the respawn-count guard must
    # still prevent it.
    await eval_db.refresh(child)
    child.updated_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=20)
    eval_db.add(child)
    await eval_db.commit()
    await _check_and_trigger_rollup(child, eval_db)

    assert spawn_calls["n"] <= 1, (
        f"Expected at most one review respawn across two concurrent terminal "
        f"events (parent-lock idempotency), got {spawn_calls['n']}"
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


@pytest.mark.asyncio
async def test_backstop_resolves_clean_rollup_via_rollup_pr_number(eval_db, mocker):
    """PR-identity split-brain: a rollup parked past the CI backstop with
    ci_waiting=True but NO review_loop.pr_number and NO cached head_sha — its PR
    identity living only at context.rollup_pr_number — must be VERIFIED live
    (PR resolved via the fallback, head_sha looked up) and routed to the
    completion handler on a green verdict, NOT hard-killed."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.rollup_management._check_and_trigger_rollup", new_callable=AsyncMock
    )
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "owner", "repo"),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        return_value={
            "state": "open",
            "head": {"sha": "deadbeef"},
            "base": {"ref": "automain"},
            "mergeable": True,
        },
    )
    mock_resolve = mocker.patch(
        "app.ci_resolution.resolve_pr_ci_status",
        new_callable=AsyncMock,
        return_value={
            "state": "green",
            "conclusion": "success",
            "details": [],
            "head_sha_resolved": "deadbeef",
            "source": "branch_head",
        },
    )
    mock_hcc = mocker.patch(
        "app.ci_completion.handle_ci_completion",
        new_callable=AsyncMock,
        return_value=True,
    )

    project = Project(name="Split-brain rollup", user_id="user1")
    eval_db.add(project)
    await eval_db.commit()
    await eval_db.refresh(project)

    parent = Task(
        project_id=project.id,
        payloads=[{"type": "research", "prompt": "rollup"}],
        status=TaskStatus.EVALUATING,
    )
    eval_db.add(parent)
    await eval_db.commit()
    await eval_db.refresh(parent)

    parent.status_log = [
        {
            "t": _naive_utc_ago(minutes=120).isoformat(),
            "msg": "Status changed: IN_PROGRESS → EVALUATING",
        }
    ]
    # The identity-less shape: ci_waiting with no review_loop.pr_number / head_sha.
    parent.context = {
        "rollup_pr_number": 5555,
        "review_loop": {
            "ci_waiting": True,
            "ci_wait_started": _naive_utc_ago(minutes=90).isoformat(),
            "base_branch": "automain",
        },
    }
    eval_db.add(parent)
    await eval_db.commit()

    await _check_evaluating_timeouts()

    await eval_db.refresh(parent)
    assert parent.status == TaskStatus.EVALUATING, (
        f"Backstop must verify (not kill) a resolvable rollup, got "
        f"{parent.status}: {parent.latest_status_message!r}"
    )
    # Resolver called with the PR number resolved from context.rollup_pr_number.
    mock_resolve.assert_awaited_once()
    assert mock_resolve.call_args.kwargs["pr_number"] == 5555
    # Green verdict routed to the shared completion handler (merge path).
    mock_hcc.assert_awaited_once()


@pytest.mark.asyncio
async def test_backstop_no_checks_keep_polling_fails_open_and_kills(eval_db, mocker):
    """Fail-open safety: a 0-check PR on a CI-gated repo (no-checks →
    keep_polling) must NOT be merged on absence of signal — the verify falls
    through and the backstop still terminates the stuck parent."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.rollup_management._check_and_trigger_rollup", new_callable=AsyncMock
    )
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "owner", "repo"),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        return_value={
            "state": "open",
            "head": {"sha": "cafe1234"},
            "base": {"ref": "main"},
            "mergeable": True,
        },
    )
    mocker.patch(
        "app.ci_resolution.resolve_pr_ci_status",
        new_callable=AsyncMock,
        return_value={"state": "no_checks", "details": [], "source": "branch_head"},
    )
    # CI-gated repo: the no-checks policy says keep polling — never auto-merge.
    mocker.patch(
        "app.ci_resolution.resolve_no_checks_escalation", return_value="keep_polling"
    )
    mock_hcc = mocker.patch(
        "app.ci_completion.handle_ci_completion", new_callable=AsyncMock
    )

    project = Project(name="No-checks keep polling", user_id="user1")
    eval_db.add(project)
    await eval_db.commit()
    await eval_db.refresh(project)

    parent = Task(
        project_id=project.id,
        payloads=[{"type": "research", "prompt": "stuck no-checks"}],
        status=TaskStatus.EVALUATING,
    )
    eval_db.add(parent)
    await eval_db.commit()
    await eval_db.refresh(parent)

    parent.status_log = [
        {
            "t": _naive_utc_ago(minutes=120).isoformat(),
            "msg": "Status changed: IN_PROGRESS → EVALUATING",
        }
    ]
    parent.context = {
        "review_loop": {
            "ci_waiting": True,
            "ci_wait_started": _naive_utc_ago(minutes=90).isoformat(),
            "pr_number": 7777,
            "base_branch": "main",
        }
    }
    eval_db.add(parent)
    await eval_db.commit()

    await _check_evaluating_timeouts()

    await eval_db.refresh(parent)
    assert parent.status == TaskStatus.FAILED, (
        f"Expected FAILED (no-checks keep_polling → fail-open → backstop kill), "
        f"got {parent.status}: {parent.latest_status_message!r}"
    )
    # Never merged on absence of signal for a CI-gated repo.
    mock_hcc.assert_not_awaited()


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


# ── RC-2: approval-gate timeout preserves mergeable rollup PRs ───────────────


@pytest.mark.asyncio
async def test_approval_gate_timeout_preserves_mergeable_rollup_pr(eval_db, mocker):
    """RC-2: when the approval-gate timeout fires on an EVALUATING parent whose
    rollup PR is still clean/mergeable, the task must NOT be marked FAILED.
    Instead it is left EVALUATING with approval_timeout_preserved_for_handmerge=True
    and a hand-merge alert is fired — the PR stays open for the operator."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch("app.services.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "owner", "repo"),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        return_value={
            "state": "open",
            "changed_files": 3,
            "mergeable_state": "clean",
        },
    )
    alert_mock = mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    rec = mocker.patch("app.routers.agent_status.record_task_result")

    project = Project(name="Rollup Preserve", user_id="user1")
    eval_db.add(project)
    await eval_db.commit()
    await eval_db.refresh(project)

    parent = Task(
        project_id=project.id,
        payloads=[{"type": "research", "prompt": "rollup parent"}],
        status=TaskStatus.EVALUATING,
        requires_approval=True,
        context={
            "_approval_set_at": _naive_utc_ago(hours=5).isoformat(),
            "rollup_pr_number": 474,
            "_is_rollup_review": True,
        },
    )
    eval_db.add(parent)
    await eval_db.commit()
    await eval_db.refresh(parent)

    await _check_evaluating_timeouts()

    await eval_db.refresh(parent)
    assert parent.status == TaskStatus.EVALUATING, (
        f"Mergeable rollup PR must NOT be FAILed by approval-gate timeout, "
        f"got {parent.status}: {parent.latest_status_message!r}"
    )
    assert (parent.context or {}).get(
        "approval_timeout_preserved_for_handmerge"
    ) is True
    assert (parent.context or {}).get(
        "failure_detail"
    ) == "approval_timeout_preserved_for_handmerge"
    alert_mock.assert_awaited()
    rec.assert_not_called()


@pytest.mark.asyncio
async def test_approval_gate_timeout_fails_non_rollup_task(eval_db, mocker):
    """RC-2 GUARDRAIL: approval-gate timeout on a non-rollup task (no rollup PR
    to preserve) keeps today's FAILED path unchanged."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch("app.services.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    rec = mocker.patch("app.routers.agent_status.record_task_result")

    project = Project(name="Non-Rollup Timeout", user_id="user1")
    eval_db.add(project)
    await eval_db.commit()
    await eval_db.refresh(project)

    parent = Task(
        project_id=project.id,
        payloads=[{"type": "research", "prompt": "non-rollup approval-gated parent"}],
        status=TaskStatus.EVALUATING,
        requires_approval=True,
        context={"_approval_set_at": _naive_utc_ago(hours=5).isoformat()},
    )
    eval_db.add(parent)
    await eval_db.commit()
    await eval_db.refresh(parent)

    await _check_evaluating_timeouts()

    await eval_db.refresh(parent)
    assert parent.status == TaskStatus.FAILED, (
        f"Non-rollup approval-gate timeout must still FAIL, got {parent.status}"
    )
    rec.assert_called_once()
    assert rec.call_args.kwargs.get("success") is False


@pytest.mark.asyncio
async def test_approval_gate_timeout_reconciles_already_merged_rollup_pr(
    eval_db, mocker
):
    """CREDIT-BLINDNESS: when the approval-gate timeout fires on an EVALUATING
    parent whose rollup PR has ALREADY MERGED, the parent must be reconciled to
    COMPLETED — never FAILED. Landed work counted as a failure costs the
    direction its merge credit (real incident: research be331963 / PR #14342
    merged, then FAILED 36s later). No failure result/alert must be recorded on
    this success path."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch("app.services.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "owner", "repo"),
    )
    # Merged-PR check returns merged=True (state closed, merged, base ref set).
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        return_value={
            "state": "closed",
            "merged": True,
            "number": 474,
            "base": {"ref": "automain"},
        },
    )
    alert_mock = mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    rec = mocker.patch("app.routers.agent_status.record_task_result")

    project = Project(name="Rollup Already Merged", user_id="user1")
    eval_db.add(project)
    await eval_db.commit()
    await eval_db.refresh(project)

    parent = Task(
        project_id=project.id,
        payloads=[{"type": "research", "prompt": "rollup parent, PR merged"}],
        status=TaskStatus.EVALUATING,
        requires_approval=True,
        context={
            "_approval_set_at": _naive_utc_ago(hours=5).isoformat(),
            "rollup_pr_number": 474,
            "_is_rollup_review": True,
        },
    )
    eval_db.add(parent)
    await eval_db.commit()
    await eval_db.refresh(parent)

    await _check_evaluating_timeouts()

    await eval_db.refresh(parent)
    assert parent.status == TaskStatus.COMPLETED, (
        f"Already-merged rollup PR must reconcile to COMPLETED, not FAILED — "
        f"got {parent.status}: {parent.latest_status_message!r}"
    )
    assert "474" in (parent.latest_status_message or "")
    assert "merged" in (parent.latest_status_message or "").lower()
    # The approval hold is cleared — the work landed, this is terminal.
    assert parent.requires_approval is False
    # SUCCESS path: never counted as a task failure.
    rec.assert_not_called()
    alert_mock.assert_not_awaited()


# ── Spend/budget approval holds: clean non-failure decline ────────────────────


@pytest.mark.asyncio
async def test_review_escalation_hold_past_2h_still_fails_and_records(eval_db, mocker):
    """A review-escalation hold (requires_approval=True, NO approval_hold_reason
    marker) past the 2h gate must keep today's behavior EXACTLY: FAILED and a
    failed task result recorded. Absence of the marker preserves the legacy path."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    rec = mocker.patch("app.routers.agent_status.record_task_result")

    project = Project(name="Review Escalation Hold", user_id="user1")
    eval_db.add(project)
    await eval_db.commit()
    await eval_db.refresh(project)

    parent = Task(
        project_id=project.id,
        payloads=[{"type": "review", "prompt": "review escalation churn"}],
        status=TaskStatus.EVALUATING,
        requires_approval=True,
        context={"_approval_set_at": _naive_utc_ago(hours=3).isoformat()},
    )
    eval_db.add(parent)
    await eval_db.commit()
    await eval_db.refresh(parent)

    await _check_evaluating_timeouts()

    await eval_db.refresh(parent)
    assert parent.status == TaskStatus.FAILED
    # A review-escalation hold IS counted as a task failure.
    rec.assert_called_once()
    assert rec.call_args.kwargs.get("success") is False


@pytest.mark.asyncio
async def test_spend_hold_first_sweep_fires_created_alert_and_notification(
    eval_db, mocker
):
    """First sweep over a fresh spend/budget hold (no _approval_set_at) stamps the
    timer, fires SPEND_HOLD_CREATED + a non-failure WARNING alert once, and leaves
    the task EVALUATING — it is NOT counted as a failure."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
    fail_alert = mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    rec = mocker.patch("app.routers.agent_status.record_task_result")
    notify = mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )

    project = Project(name="Spend Hold Created", user_id="user1")
    eval_db.add(project)
    await eval_db.commit()
    await eval_db.refresh(project)

    parent = Task(
        project_id=project.id,
        payloads=[{"type": "research", "prompt": "large-scope budget mismatch"}],
        status=TaskStatus.EVALUATING,
        requires_approval=True,
        context={"approval_hold_reason": "spend_budget"},
    )
    eval_db.add(parent)
    await eval_db.commit()
    await eval_db.refresh(parent)

    await _check_evaluating_timeouts()

    await eval_db.refresh(parent)
    # Still held — operators have the full window to decide.
    assert parent.status == TaskStatus.EVALUATING
    assert parent.requires_approval is True
    assert parent.context.get("_approval_set_at")
    assert parent.context.get("_spend_hold_created_notified") is True
    # NOT a failure.
    fail_alert.assert_not_called()
    rec.assert_not_called()
    # Non-failure WARNING alert created.
    alerts = (
        (await eval_db.execute(select(Alert).where(Alert.task_id == parent.id)))
        .scalars()
        .all()
    )
    assert len(alerts) == 1
    assert alerts[0].severity == AlertSeverity.WARNING
    # SPEND_HOLD_CREATED notification dispatched.
    assert notify.called
    created_events = [c.args[1] for c in notify.call_args_list]
    assert "SPEND_HOLD_CREATED" in created_events


@pytest.mark.asyncio
async def test_spend_hold_past_window_declines_cleanly_not_failed(eval_db, mocker):
    """A spend/budget hold past the configured window resolves to a CLEAN
    NON-FAILURE CANCELLED terminal: requires_approval cleared, declined message,
    NOT counted as a task failure (no record_task_result(success=False), no
    failure alert)."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
    fail_alert = mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    rec = mocker.patch("app.routers.agent_status.record_task_result")

    project = Project(name="Spend Hold Decline", user_id="user1")
    eval_db.add(project)
    await eval_db.commit()
    await eval_db.refresh(project)

    parent = Task(
        project_id=project.id,
        payloads=[{"type": "research", "prompt": "large-scope budget mismatch"}],
        status=TaskStatus.EVALUATING,
        requires_approval=True,
        context={
            "approval_hold_reason": "spend_budget",
            # 25h ago — past the default 24h spend window.
            "_approval_set_at": _naive_utc_ago(hours=25).isoformat(),
            # Pretend the created/expiring nudges already fired so this sweep
            # exercises only the decline branch.
            "_spend_hold_created_notified": True,
            "_spend_hold_expiring_notified": True,
        },
    )
    eval_db.add(parent)
    await eval_db.commit()
    await eval_db.refresh(parent)

    await _check_evaluating_timeouts()

    await eval_db.refresh(parent)
    assert parent.status == TaskStatus.CANCELLED, (
        f"Expected CANCELLED clean decline, got {parent.status}: "
        f"{parent.latest_status_message!r}"
    )
    assert parent.requires_approval is False
    assert "declined" in (parent.latest_status_message or "").lower()
    assert parent.context.get("spend_approval_declined") is True
    # MUST NOT be counted as a task failure.
    rec.assert_not_called()
    fail_alert.assert_not_called()
    # No failure (ERROR) alert row was created for this task either.
    alerts = (
        (await eval_db.execute(select(Alert).where(Alert.task_id == parent.id)))
        .scalars()
        .all()
    )
    assert all(a.severity != AlertSeverity.ERROR for a in alerts)


@pytest.mark.asyncio
async def test_spend_hold_expiring_notifies_once_within_lead(eval_db, mocker):
    """A spend/budget hold whose elapsed time is within the warning-lead window
    (but not yet expired) fires SPEND_HOLD_EXPIRING once and stays EVALUATING."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    notify = mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )

    project = Project(name="Spend Hold Expiring", user_id="user1")
    eval_db.add(project)
    await eval_db.commit()
    await eval_db.refresh(project)

    # Default window 24h; lead = max(24h*0.2, 1h) = 4.8h. 22h elapsed is inside
    # the lead window (22 >= 24-4.8=19.2) but not expired (< 24).
    parent = Task(
        project_id=project.id,
        payloads=[{"type": "research", "prompt": "large-scope budget mismatch"}],
        status=TaskStatus.EVALUATING,
        requires_approval=True,
        context={
            "approval_hold_reason": "spend_budget",
            "_approval_set_at": _naive_utc_ago(hours=22).isoformat(),
            "_spend_hold_created_notified": True,
        },
    )
    eval_db.add(parent)
    await eval_db.commit()
    await eval_db.refresh(parent)

    await _check_evaluating_timeouts()

    await eval_db.refresh(parent)
    assert parent.status == TaskStatus.EVALUATING
    assert parent.context.get("_spend_hold_expiring_notified") is True
    expiring_events = [c.args[1] for c in notify.call_args_list]
    assert "SPEND_HOLD_EXPIRING" in expiring_events

    # Idempotent: a second sweep does NOT re-emit the expiring notification.
    notify.reset_mock()
    await _check_evaluating_timeouts()
    expiring_events2 = [c.args[1] for c in notify.call_args_list]
    assert "SPEND_HOLD_EXPIRING" not in expiring_events2


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
    mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
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
    mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
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
    mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
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
    mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )

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
async def test_pr_state_check_consumer_respawn_failure_persists_marker(eval_db, mocker):
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
async def test_pr_state_check_consumer_respawn_success_clears_marker(eval_db, mocker):
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
        (await eval_db.execute(select(Alert).where(Alert.task_id == parent.id)))
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
        (await eval_db.execute(select(Alert).where(Alert.task_id == parent.id)))
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
        (await eval_db.execute(select(Alert).where(Alert.task_id == parent.id)))
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


def _mock_lost_marker_github(mocker, *, prs):
    """Mock the GitHub calls the lost-marker recovery makes (token +
    list-PRs-for-head-branch). ``prs`` is the list returned for the staging
    head branch."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    # The lost-marker recovery (now in app.recovery_evaluating_strand) broadcasts
    # via ``services.broadcast_to_project`` (a binding separate from
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
        "app.github.get_pull_requests_for_head_branch",
        new_callable=AsyncMock,
        return_value=prs,
    )


@pytest.mark.asyncio
async def test_lost_marker_converges_on_merged_pr(eval_db, mocker):
    """ROLLUP-PARENT LOST-MARKER RECOVERY: an EVALUATING parent that has a
    staging branch in context but never persisted its rollup_pr_number (and no
    pr_merged) converges when GitHub reports a merged PR from that head branch.
    The parent must terminalize COMPLETED, recover the lost rollup_pr_number,
    and carry the integration-landing signal."""
    _mock_lost_marker_github(
        mocker,
        prs=[
            {
                "number": 9901,
                "merged": True,
                "merged_at": "2026-06-17T00:00:00Z",
                "html_url": "https://github.com/owner/repo/pull/9901",
                "base": {"ref": "main"},
            }
        ],
    )

    project = Project(name="Lost Marker Test", user_id="user1")
    eval_db.add(project)
    await eval_db.commit()
    await eval_db.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "research", "prompt": "research parent"}],
        status=TaskStatus.EVALUATING,
        # Staging branch present, but NO rollup_pr_number and NO pr_merged —
        # the lost-marker window. No children: the lost-marker handler runs
        # before any children/rollup-trigger branch.
        context={"plan_branch": "rollup/stranded-staging"},
    )
    eval_db.add(task)
    await eval_db.commit()
    await eval_db.refresh(task)

    await _check_evaluating_timeouts()

    await eval_db.refresh(task)
    assert task.status == TaskStatus.COMPLETED, (
        f"Lost-marker parent whose rollup PR merged must converge COMPLETED, "
        f"got {task.status}"
    )
    ctx = task.context or {}
    assert ctx.get("rollup_pr_number") == 9901, (
        "Lost-marker recovery must recover the un-persisted rollup_pr_number"
    )
    assert ctx.get("pr_merged") is True
    assert ctx.get("landed_on_integration") is True


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


# ── Fix 4: hard-FAIL kill-suppression corroborated by off-row vmcontact ──────


async def _make_chain_parent_and_child(
    eval_db, *, child_status, last_heartbeat, vm_instance_id, pr_number=7777
):
    """Build an EVALUATING parent (with a review_loop chain) + one matching
    review child for _chain_has_active_pipeline_work tests."""
    project = Project(name="Chain VMContact Test", user_id="user1")
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
    parent.context = {
        "review_loop": {
            "pr_number": pr_number,
            "original_task_id": str(parent.id),
        }
    }
    eval_db.add(parent)
    await eval_db.commit()
    await eval_db.refresh(parent)

    child = Task(
        project_id=project.id,
        parent_task_id=parent.id,
        payloads=[
            {
                "type": "review",
                "pr_number": pr_number,
                "original_task_id": str(parent.id),
            }
        ],
        status=child_status,
        last_heartbeat=last_heartbeat,
        vm_instance_id=vm_instance_id,
    )
    eval_db.add(child)
    await eval_db.commit()
    return parent, child


@pytest.mark.asyncio
async def test_chain_active_fresh_heartbeat_defers_kill(eval_db):
    """Baseline: a chain child with a FRESH on-row last_heartbeat defers the kill
    (the pre-existing path, unchanged by Fix 4)."""
    parent, _child = await _make_chain_parent_and_child(
        eval_db,
        child_status=TaskStatus.IN_PROGRESS,
        last_heartbeat=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=30),
        vm_instance_id="vm-chain-alive",
    )
    assert await _chain_has_active_pipeline_work(parent, eval_db) is True


@pytest.mark.asyncio
async def test_chain_active_vmcontact_overrides_stale_heartbeat(eval_db):
    """Fix 4: a chain child whose on-row last_heartbeat is STALE (lock-starved)
    but whose off-row vmcontact.last_contact_at is FRESH must DEFER the kill — the
    child is alive; its contended heartbeat write was skipped by the rollup's
    task-row lock. Without this, the irreversible hard-FAIL would cascade-cancel a
    live review/fix child and delete its VM."""
    from app.models import VmContact

    parent, _child = await _make_chain_parent_and_child(
        eval_db,
        child_status=TaskStatus.IN_PROGRESS,
        # 20min-stale on-row heartbeat (past the default 600s/10min cutoff).
        last_heartbeat=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=20),
        vm_instance_id="vm-chain-lockstarved",
    )
    # Fresh off-row contact (30s) within the 10min stale cutoff.
    eval_db.add(
        VmContact(
            vm_instance_id="vm-chain-lockstarved",
            last_contact_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=30),
        )
    )
    await eval_db.commit()

    assert await _chain_has_active_pipeline_work(parent, eval_db) is True


@pytest.mark.asyncio
async def test_chain_active_stale_heartbeat_and_stale_vmcontact_allows_kill(eval_db):
    """Fix 4 safety: a chain child whose on-row heartbeat AND off-row vmcontact are
    BOTH stale (genuinely dead — no requests) must NOT defer the kill. The override
    is gated on the SAME stale cutoff so a dead child never wedges the parent's
    hard timeout forever."""
    from app.models import VmContact

    parent, _child = await _make_chain_parent_and_child(
        eval_db,
        child_status=TaskStatus.IN_PROGRESS,
        last_heartbeat=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=20),
        vm_instance_id="vm-chain-dead",
    )
    # Off-row contact is ALSO stale (15min, past the 10min cutoff).
    eval_db.add(
        VmContact(
            vm_instance_id="vm-chain-dead",
            last_contact_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=15),
        )
    )
    await eval_db.commit()

    assert await _chain_has_active_pipeline_work(parent, eval_db) is False


@pytest.mark.asyncio
async def test_chain_active_no_vmcontact_row_allows_kill(eval_db):
    """Fix 4 back-compat: a stale-heartbeat child with NO vmcontact row at all
    (genuinely dead VM that never recorded a contact) still allows the kill —
    unchanged from pre-fix behaviour."""
    parent, _child = await _make_chain_parent_and_child(
        eval_db,
        child_status=TaskStatus.IN_PROGRESS,
        last_heartbeat=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=20),
        vm_instance_id="vm-chain-no-contact-row",
    )
    assert await _chain_has_active_pipeline_work(parent, eval_db) is False


# ── BLOCKED review/fix child keeps rollup waiting (PR #8746) ─────────────────


@pytest.mark.asyncio
async def test_rollup_blocked_fix_child_keeps_parent_evaluating_no_completion_pass(
    eval_db, mocker
):
    """PR #8746 regression: a BLOCKED review/fix child must keep the rollup's
    ``pipeline_active`` gate True so the completion / review-loop-rebuild pass
    does NOT run while a fix is parked on a precondition.

    BLOCKED review/fix children are invisible to the subtask tally
    (``active_count`` excludes review/fix types), so ONLY the ``pipeline_active``
    loop can hold the parent. A BLOCKED child also never heartbeats, so it must
    bypass the staleness skip. Pre-fix the loop skipped BLOCKED entirely →
    ``pipeline_active=False`` → the function fell through to the
    ``_verify_pr_merged`` completion pass and rebuilt review_loop from scratch,
    re-arming ci_waiting and discarding ci_fix_spawned (research 5b250ecd).

    Sharp pre-fix signal: ``_verify_pr_merged`` is reached (and the parent
    progresses) only when the gate falls through. Post-fix the early return at
    the ``pipeline_active`` gate means it is NEVER called and the parent stays
    EVALUATING.
    """
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    # Spy: post-fix this must NOT be called (early return before the verify pass).
    verify_spy = mocker.patch(
        "app.rollup_management._verify_pr_merged",
        new=AsyncMock(return_value=True),
    )

    project = Project(name="Blocked Fix Child Test", user_id="user1")
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

    # One terminal, merged ROLLUP_TYPES child (keeps `children` non-empty so the
    # "all children review/fix → COMPLETED" short-circuit does NOT fire, and
    # active_count stays 0 so only pipeline_active can hold the parent).
    done_child = Task(
        project_id=project.id,
        parent_task_id=parent.id,
        payloads=[{"type": "create", "prompt": "done"}],
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
    )
    # The BLOCKED fix child — no heartbeat (parked on a precondition). Its
    # type is excluded from ROLLUP_TYPES, so only the pipeline_active loop sees
    # it; pre-fix it was skipped (status not in the active tuple).
    blocked_fix = Task(
        project_id=project.id,
        parent_task_id=parent.id,
        payloads=[{"type": "fix", "prompt": "fix", "pr_number": 8746}],
        status=TaskStatus.BLOCKED,
        last_heartbeat=None,
        context={"sentinel": "untouched"},
    )
    eval_db.add(done_child)
    eval_db.add(blocked_fix)
    await eval_db.commit()
    await eval_db.refresh(blocked_fix)

    # Trigger on the terminal child's completion event.
    await _check_and_trigger_rollup(done_child, eval_db)

    # pipeline_active True (the BLOCKED fix) → early return; the completion pass
    # never runs, so the parent stays EVALUATING and verify was never reached.
    await eval_db.refresh(parent)
    assert parent.status == TaskStatus.EVALUATING
    verify_spy.assert_not_called()

    # The BLOCKED fix child was only READ — never mutated.
    await eval_db.refresh(blocked_fix)
    assert blocked_fix.status == TaskStatus.BLOCKED
    assert (blocked_fix.context or {}).get("sentinel") == "untouched"


# ── CI-backstop robustness: recoverability guard, requeue-keyed liveness,
#    real-CI verify, fail-open, and the no-permanent-wedge regression guard ─────


@pytest.mark.asyncio
async def test_ci_backstop_defers_when_chain_has_recoverable_requeuing_child(
    eval_db, mocker
):
    """Gap A: a CI-waiting parent past the backstop must NOT be FAILED (and its
    children must NOT be cascade-cancelled) while the chain holds recoverable,
    still-requeuing work — it defers within a bounded window instead.

    Incident 2026-06-07 (research 5b250ecd): the backstop FAILED the parent and
    cascade-cancelled a freshly-requeued, fully-recoverable fix child (53a75ed5).
    Here the requeued fix child sits QUEUED, last touched >30min ago (so the
    updated_at-keyed liveness guard does NOT catch it) but with optimistic-retry
    budget still left — the reaper will requeue it again the moment a slot frees.
    MUST FAIL pre-fix (the backstop unconditionally FAILs the parent and
    cascade-cancels this non-terminal QUEUED child).
    """
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch("app.vm_lifecycle._ensure_vm_deleted", new_callable=AsyncMock)
    mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    mocker.patch(
        "app.rollup_management._check_and_trigger_rollup", new_callable=AsyncMock
    )

    project = Project(name="CI Backstop Recoverable Defer", user_id="user1")
    eval_db.add(project)
    await eval_db.commit()
    await eval_db.refresh(project)

    parent = Task(
        project_id=project.id,
        payloads=[{"type": "research", "prompt": "rollup parent"}],
        status=TaskStatus.EVALUATING,
        # ci_passed=False so it falls straight to the FAILED block; NO head_sha
        # so the Gap-C real-CI verify is skipped and we exercise Gap A directly.
        context={
            "review_loop": {
                "ci_waiting": True,
                "ci_wait_started": _naive_utc_ago(minutes=90).isoformat(),
                "pr_number": 88001,
                "original_task_id": "chain-recov",
            }
        },
    )
    eval_db.add(parent)
    await eval_db.commit()
    await eval_db.refresh(parent)

    # Recoverable requeuing fix child: QUEUED, last touched 50 min ago (past the
    # 30-min liveness window) but optimistic-retry budget not yet spent. Must NOT
    # be cascade-cancelled — it will be optimistically requeued again.
    requeuing_child = Task(
        project_id=project.id,
        parent_task_id=parent.id,
        payloads=[
            {
                "type": "fix",
                "pr_number": 88001,
                "original_task_id": "chain-recov",
                "prompt": "requeuing fix child",
            }
        ],
        status=TaskStatus.QUEUED,
        context={"_blocked_optimistic_retries": 1},
        created_at=_naive_utc_ago(hours=2),
        updated_at=_naive_utc_ago(minutes=50),
    )
    eval_db.add(requeuing_child)
    await eval_db.commit()
    await eval_db.refresh(requeuing_child)
    child_id = requeuing_child.id

    await _check_evaluating_timeouts()

    await eval_db.refresh(parent)
    await eval_db.refresh(requeuing_child)
    assert parent.status == TaskStatus.EVALUATING, (
        f"Backstop must DEFER while a recoverable requeuing child exists, got "
        f"{parent.status}: {parent.latest_status_message!r}"
    )
    assert requeuing_child.status == TaskStatus.QUEUED, (
        f"Recoverable requeuing child must NOT be cascade-cancelled, got "
        f"{requeuing_child.status}"
    )
    rl = (parent.context or {}).get("review_loop", {})
    assert rl.get("_ci_backstop_first_defer_at"), (
        "Backstop should stamp _ci_backstop_first_defer_at on first defer"
    )
    assert child_id == requeuing_child.id  # sanity


@pytest.mark.asyncio
async def test_chain_active_queued_child_keyed_on_last_transition_not_created_at(
    eval_db,
):
    """Gap B: a QUEUED child created 2h ago whose last STATUS TRANSITION (the
    optimistic BLOCKED → QUEUED requeue) was 2 min ago must count as live work —
    keyed on the status_log transition timestamp, not created_at.

    This is the 53a75ed5 shape: a fix child ran ~50 min, hit a transient blocked
    precondition, and was optimistically requeued to QUEUED. created_at reads old
    while the requeue transition is fresh. MUST FAIL pre-fix (the old code keyed
    on created_at, so a 2h-old created_at → not-live → unprotected). Deliberately
    NOT keyed on updated_at: any incidental row write refreshes updated_at, which
    would let a genuinely-stuck-but-touched child defer the hard timeout forever
    (see test_chain_active_stale_provisioning_does_not_count).
    """
    project = Project(name="Chain Queued Keyed Last Transition", user_id="user1")
    eval_db.add(project)
    await eval_db.commit()
    await eval_db.refresh(project)

    parent = Task(
        project_id=project.id,
        payloads=[{"type": "research", "prompt": "rollup parent"}],
        status=TaskStatus.EVALUATING,
        context={"review_loop": {"pr_number": 88002, "original_task_id": "chain-req"}},
    )
    eval_db.add(parent)
    await eval_db.commit()
    await eval_db.refresh(parent)

    requeued_child = Task(
        project_id=project.id,
        payloads=[
            {
                "type": "fix",
                "pr_number": 88002,
                "original_task_id": "chain-req",
            }
        ],
        status=TaskStatus.QUEUED,
        last_heartbeat=None,
        # Created 2 hours ago (well past the 30-min queued_max_age threshold)…
        created_at=_naive_utc_ago(hours=2),
        # …but its last real status transition (the optimistic requeue, as the
        # _on_status_set model listener records it) was 2 min ago.
        status_log=[
            {
                "t": _naive_utc_ago(minutes=50).isoformat(),
                "msg": "Status changed: IN_PROGRESS → BLOCKED",
            },
            {
                "t": _naive_utc_ago(minutes=30).isoformat(),
                "msg": "Optimistic infra-precondition retry 1/3",
            },
            {
                "t": _naive_utc_ago(minutes=2).isoformat(),
                "msg": "Status changed: BLOCKED → QUEUED",
            },
        ],
    )
    eval_db.add(requeued_child)
    await eval_db.commit()

    assert await _chain_has_active_pipeline_work(parent, eval_db) is True


@pytest.mark.asyncio
async def test_ci_backstop_routes_concluded_failure_to_ci_completion(eval_db, mocker):
    """Gap C: when the PR's check-runs have actually CONCLUDED in failure before
    the backstop fires, the parent must NOT be FAILED — the concluded result is
    routed to handle_ci_completion (the CI-failure fix-spawn path) instead.
    """
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch("app.vm_lifecycle._ensure_vm_deleted", new_callable=AsyncMock)
    mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    mocker.patch(
        "app.rollup_management._check_and_trigger_rollup", new_callable=AsyncMock
    )
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new=AsyncMock(return_value=("tok", "owner", "repo")),
    )
    # The backstop verifier now routes through the strict resolver
    # (ci_resolution.resolve_pr_ci_status), the SAME path the poll loop uses —
    # a red verdict is what drives the CI-failure route.
    mocker.patch(
        "app.ci_resolution.resolve_pr_ci_status",
        new=AsyncMock(
            return_value={
                "state": "red",
                "conclusion": "failure",
                "details": [],
                "failed_logs": "boom",
                "source": "branch_head",
            }
        ),
    )
    mock_handle = mocker.patch(
        "app.ci_completion.handle_ci_completion",
        new=AsyncMock(return_value=True),
    )

    project = Project(name="CI Backstop Concluded Failure", user_id="user1")
    eval_db.add(project)
    await eval_db.commit()
    await eval_db.refresh(project)

    parent = Task(
        project_id=project.id,
        payloads=[{"type": "review", "prompt": "concluded-fail task"}],
        status=TaskStatus.EVALUATING,
        context={
            "review_loop": {
                "ci_waiting": True,
                "ci_wait_started": _naive_utc_ago(minutes=90).isoformat(),
                "pr_number": 88003,
                "head_sha": "deadbeef",
            }
        },
    )
    eval_db.add(parent)
    await eval_db.commit()
    await eval_db.refresh(parent)

    await _check_evaluating_timeouts()

    await eval_db.refresh(parent)
    mock_handle.assert_awaited()
    assert parent.status != TaskStatus.FAILED, (
        f"Parent must not be FAILED when CI concluded (routed to handler), got "
        f"{parent.status}: {parent.latest_status_message!r}"
    )


@pytest.mark.asyncio
async def test_ci_backstop_routes_concluded_success_to_ci_completion(eval_db, mocker):
    """Gap C: when the PR's check-runs CONCLUDED in success before the backstop
    fires, the parent must NOT be FAILED — the pass is recorded / wait cleared
    via handle_ci_completion instead of timing out."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch("app.vm_lifecycle._ensure_vm_deleted", new_callable=AsyncMock)
    mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    mocker.patch(
        "app.rollup_management._check_and_trigger_rollup", new_callable=AsyncMock
    )
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new=AsyncMock(return_value=("tok", "owner", "repo")),
    )
    # Verifier routes through the strict resolver — a green verdict records the
    # pass / clears the wait via handle_ci_completion instead of timing out.
    mocker.patch(
        "app.ci_resolution.resolve_pr_ci_status",
        new=AsyncMock(
            return_value={
                "state": "green",
                "conclusion": "success",
                "details": [],
                "source": "branch_head",
            }
        ),
    )
    mock_handle = mocker.patch(
        "app.ci_completion.handle_ci_completion",
        new=AsyncMock(return_value=True),
    )

    project = Project(name="CI Backstop Concluded Success", user_id="user1")
    eval_db.add(project)
    await eval_db.commit()
    await eval_db.refresh(project)

    parent = Task(
        project_id=project.id,
        payloads=[{"type": "review", "prompt": "concluded-pass task"}],
        status=TaskStatus.EVALUATING,
        context={
            "review_loop": {
                "ci_waiting": True,
                "ci_wait_started": _naive_utc_ago(minutes=90).isoformat(),
                "pr_number": 88004,
                "head_sha": "cafebabe",
            }
        },
    )
    eval_db.add(parent)
    await eval_db.commit()
    await eval_db.refresh(parent)

    await _check_evaluating_timeouts()

    await eval_db.refresh(parent)
    mock_handle.assert_awaited()
    assert parent.status != TaskStatus.FAILED, (
        f"Parent must not be FAILED when CI concluded success, got {parent.status}: "
        f"{parent.latest_status_message!r}"
    )


@pytest.mark.asyncio
async def test_ci_backstop_fails_open_on_github_error(eval_db, mocker):
    """Gap C fail-OPEN: a GitHub error during the real-CI re-verify must NOT block
    the backstop — with no recoverable chain work, the parent still times out
    (existing behaviour preserved)."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch("app.vm_lifecycle._ensure_vm_deleted", new_callable=AsyncMock)
    mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    mocker.patch(
        "app.rollup_management._check_and_trigger_rollup", new_callable=AsyncMock
    )
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new=AsyncMock(return_value=("tok", "owner", "repo")),
    )
    # GitHub returns an API-error-tagged result → fail open (no signal).
    mocker.patch(
        "app.github_checks.get_check_runs_cached",
        new=AsyncMock(
            return_value={
                "status": "pending",
                "conclusion": None,
                "details": [],
                "failed_logs": "",
                "_from_api_error": True,
            }
        ),
    )
    mock_handle = mocker.patch(
        "app.ci_completion.handle_ci_completion",
        new=AsyncMock(return_value=True),
    )

    project = Project(name="CI Backstop Fail Open", user_id="user1")
    eval_db.add(project)
    await eval_db.commit()
    await eval_db.refresh(project)

    parent = Task(
        project_id=project.id,
        payloads=[{"type": "review", "prompt": "gh-error task"}],
        status=TaskStatus.EVALUATING,
        context={
            "review_loop": {
                "ci_waiting": True,
                "ci_wait_started": _naive_utc_ago(minutes=90).isoformat(),
                "pr_number": 88005,
                "head_sha": "f00ba12",
            }
        },
    )
    eval_db.add(parent)
    await eval_db.commit()
    await eval_db.refresh(parent)

    await _check_evaluating_timeouts()

    await eval_db.refresh(parent)
    mock_handle.assert_not_awaited()
    assert parent.status == TaskStatus.FAILED, (
        f"Backstop must fail OPEN (still time out) on a GitHub error, got "
        f"{parent.status}: {parent.latest_status_message!r}"
    )
    assert "backstop" in (parent.latest_status_message or "").lower()


@pytest.mark.asyncio
async def test_ci_backstop_fires_after_defer_window_and_exhausted_retries(
    eval_db, mocker
):
    """Regression guard (no permanent wedge): once the optimistic-retry budget is
    spent AND the bounded CI-backstop defer window has elapsed, the backstop must
    still fire — the recoverability guard cannot wedge a parent forever."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch("app.vm_lifecycle._ensure_vm_deleted", new_callable=AsyncMock)
    mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    mocker.patch(
        "app.rollup_management._check_and_trigger_rollup", new_callable=AsyncMock
    )

    from app.reaper_sweeps import _BLOCKED_OPTIMISTIC_RETRY_CAP

    project = Project(name="CI Backstop Window Elapsed", user_id="user1")
    eval_db.add(project)
    await eval_db.commit()
    await eval_db.refresh(project)

    # Defer marker set 7h ago — past the 6h CI_BACKSTOP_DEFER_WINDOW_HOURS — and
    # an EVALUATING transition older still so the marker is not treated as stale.
    parent = Task(
        project_id=project.id,
        payloads=[{"type": "research", "prompt": "wedge-guard parent"}],
        status=TaskStatus.EVALUATING,
        context={
            "review_loop": {
                "ci_waiting": True,
                "ci_wait_started": _naive_utc_ago(hours=9).isoformat(),
                "pr_number": 88006,
                "_ci_backstop_first_defer_at": _naive_utc_ago(hours=7).isoformat(),
            }
        },
        status_log=[
            {
                "t": _naive_utc_ago(hours=10).isoformat(),
                "msg": "Status changed: COMPLETED → EVALUATING",
            }
        ],
    )
    eval_db.add(parent)
    await eval_db.commit()
    await eval_db.refresh(parent)

    # A recoverable FAILED child still present, AND a QUEUED child whose optimistic
    # retry budget is EXHAUSTED — neither may keep the backstop deferring once the
    # window has elapsed.
    recoverable_child = Task(
        project_id=project.id,
        parent_task_id=parent.id,
        payloads=[{"type": "create", "prompt": "infra-failed child"}],
        status=TaskStatus.FAILED,
        context={"failure_category": "PROVISION_FAILURE"},
    )
    eval_db.add(recoverable_child)
    exhausted_child = Task(
        project_id=project.id,
        payloads=[
            {
                "type": "fix",
                "pr_number": 88006,
                "original_task_id": str(parent.id),
            }
        ],
        status=TaskStatus.QUEUED,
        context={"_blocked_optimistic_retries": _BLOCKED_OPTIMISTIC_RETRY_CAP},
        created_at=_naive_utc_ago(hours=2),
        updated_at=_naive_utc_ago(hours=2),
    )
    eval_db.add(exhausted_child)
    await eval_db.commit()

    # The exhausted child must NOT count as unexhausted-optimistic-retry work.
    assert await _chain_has_unexhausted_optimistic_retry_work(parent, eval_db) is False

    await _check_evaluating_timeouts()

    await eval_db.refresh(parent)
    assert parent.status == TaskStatus.FAILED, (
        f"Backstop must still fire after the defer window elapses with exhausted "
        f"retries (no permanent wedge), got {parent.status}: "
        f"{parent.latest_status_message!r}"
    )


# ── Approval-gate timeout: bounded active-chain defer ─────────────────────────


def _chain_busy_fix_child(project_id, parent_id, *, status=TaskStatus.EVALUATING):
    """A fix child still working the chain's PR — the de99593a shape: parked in
    CI-wait (EVALUATING), which _chain_has_active_pipeline_work does not count."""
    return Task(
        project_id=project_id,
        parent_task_id=parent_id,
        payloads=[
            {
                "type": "fix",
                "pr_number": 9179,
                "original_task_id": str(parent_id),
                "prompt": "fix CI on the rollup PR",
            }
        ],
        status=status,
    )


@pytest.mark.asyncio
async def test_approval_gate_timeout_defers_while_chain_busy(eval_db, mocker):
    """A review-escalation hold past the 2h window must NOT FAIL the parent
    while its review/fix chain still has non-terminal work for the PR —
    including a fix child parked in CI-wait (EVALUATING). Regression test for
    research 6506582b / PR #9179 (2026-06-09): the parent was FAILED at the
    bare 2h mark while fix iteration 3 was actively waiting on CI."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)

    project = Project(name="Approval Gate Busy Chain", user_id="user1")
    eval_db.add(project)
    await eval_db.commit()
    await eval_db.refresh(project)

    parent = Task(
        project_id=project.id,
        payloads=[{"type": "research", "prompt": "rollup parent"}],
        status=TaskStatus.EVALUATING,
        requires_approval=True,
        context={
            "_approval_set_at": _naive_utc_ago(hours=3).isoformat(),
            "review_loop": {"pr_number": 9179, "base_branch": "automain"},
        },
    )
    eval_db.add(parent)
    await eval_db.commit()
    await eval_db.refresh(parent)

    fix_child = _chain_busy_fix_child(project.id, parent.id)
    eval_db.add(fix_child)
    await eval_db.commit()

    await _check_evaluating_timeouts()

    await eval_db.refresh(parent)
    assert parent.status == TaskStatus.EVALUATING, (
        f"Approval-gate timeout must defer while the chain is busy, got "
        f"{parent.status}: {parent.latest_status_message!r}"
    )
    assert parent.requires_approval is True


@pytest.mark.asyncio
async def test_approval_gate_timeout_fails_past_defer_ceiling_even_if_busy(
    eval_db, mocker
):
    """The active-chain defer is BOUNDED: past 2h + APPROVAL_GATE_DEFER_WINDOW
    (6h) the original unconditional FAILED terminalization applies even with a
    busy chain, so a genuinely wedged loop still terminates."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    mocker.patch("app.routers.agent_status.record_task_result")

    project = Project(name="Approval Gate Ceiling", user_id="user1")
    eval_db.add(project)
    await eval_db.commit()
    await eval_db.refresh(project)

    parent = Task(
        project_id=project.id,
        payloads=[{"type": "research", "prompt": "rollup parent"}],
        status=TaskStatus.EVALUATING,
        requires_approval=True,
        context={
            "_approval_set_at": _naive_utc_ago(hours=9).isoformat(),
            "review_loop": {"pr_number": 9179, "base_branch": "automain"},
        },
    )
    eval_db.add(parent)
    await eval_db.commit()
    await eval_db.refresh(parent)

    fix_child = _chain_busy_fix_child(project.id, parent.id)
    eval_db.add(fix_child)
    await eval_db.commit()

    await _check_evaluating_timeouts()

    await eval_db.refresh(parent)
    assert parent.status == TaskStatus.FAILED, (
        f"Past the hard ceiling the approval-gate timeout must still fire, got "
        f"{parent.status}: {parent.latest_status_message!r}"
    )


@pytest.mark.asyncio
async def test_approval_gate_timeout_still_fails_when_chain_quiescent(eval_db, mocker):
    """With NO live chain work (only a terminal fix child), the 2h timeout
    keeps today's behavior exactly: FAILED."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    mocker.patch("app.routers.agent_status.record_task_result")

    project = Project(name="Approval Gate Quiescent", user_id="user1")
    eval_db.add(project)
    await eval_db.commit()
    await eval_db.refresh(project)

    parent = Task(
        project_id=project.id,
        payloads=[{"type": "research", "prompt": "rollup parent"}],
        status=TaskStatus.EVALUATING,
        requires_approval=True,
        context={
            "_approval_set_at": _naive_utc_ago(hours=3).isoformat(),
            "review_loop": {"pr_number": 9179, "base_branch": "automain"},
        },
    )
    eval_db.add(parent)
    await eval_db.commit()
    await eval_db.refresh(parent)

    done_fix = _chain_busy_fix_child(project.id, parent.id, status=TaskStatus.COMPLETED)
    eval_db.add(done_fix)
    await eval_db.commit()

    await _check_evaluating_timeouts()

    await eval_db.refresh(parent)
    assert parent.status == TaskStatus.FAILED, (
        f"A quiescent chain must keep the 2h FAILED terminalization, got "
        f"{parent.status}: {parent.latest_status_message!r}"
    )


# ── Reaped-owner reconcile (Defects 1 & 2) ──────────────────────────────────
#
# Regression coverage for owner tasks that get reaped mid-run with an open PR:
# the reaper puts them in EVALUATING, sets _pr_state_check_pending, spawns a
# Review child, then CLEARS the marker on success.  Once the marker is gone the
# task is invisible to every existing sweep path, so if the PR merges out-of-band
# or the Review/Fix sub-pipeline completes without another reconcile kick, the
# owner stays EVALUATING forever.


@pytest.mark.asyncio
async def test_reaped_owner_with_merged_pr_reconciled_on_all_children_terminal(
    eval_db, mocker
):
    """Defect 1: when all Review/Fix children are terminal AND the owner has a
    PULL_REQUEST artifact for a merged PR, the sweep must reconcile the owner to
    COMPLETED and stamp pr_merged — NOT fall through to the generic missed-rollup
    trigger.

    Regression for task 3eb15e9e / 1c286f23: sub-pipeline finished but the owner
    never received the merged-PR signal because _pr_state_check_pending was
    cleared by the reaper after successful Review spawn.
    """
    from app.models import Artifact, ArtifactType

    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.services.broadcast_to_project", new_callable=AsyncMock, create=True
    )
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
        name="Reaped Owner Reconcile",
        user_id="user-reaped-d1",
        integration_branch="main",
        promotion_target="main",
    )
    eval_db.add(project)
    await eval_db.commit()
    await eval_db.refresh(project)

    owner = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "original work"}],
        status=TaskStatus.EVALUATING,
        context={},
        status_log=[{"t": _naive_utc_ago(hours=2).isoformat(), "msg": "→ EVALUATING"}],
    )
    eval_db.add(owner)
    await eval_db.commit()
    await eval_db.refresh(owner)

    eval_db.add(
        Artifact(
            task_id=owner.id,
            artifact_type=ArtifactType.PULL_REQUEST,
            pr_url="https://github.com/owner/repo/pull/89",
        )
    )
    review_child = Task(
        project_id=project.id,
        parent_task_id=owner.id,
        payloads=[
            {
                "type": "review",
                "pr_number": 89,
                "original_task_id": str(owner.id),
                "prompt": "review PR #89",
            }
        ],
        status=TaskStatus.COMPLETED,
        context={"review_loop": {"pr_number": 89}},
    )
    fix_child = Task(
        project_id=project.id,
        parent_task_id=owner.id,
        payloads=[
            {
                "type": "fix",
                "pr_number": 89,
                "original_task_id": str(owner.id),
                "prompt": "fix CI on PR #89",
            }
        ],
        status=TaskStatus.COMPLETED,
        context={"review_loop": {"pr_number": 89}},
    )
    eval_db.add(review_child)
    eval_db.add(fix_child)
    await eval_db.commit()

    await _check_evaluating_timeouts()

    await eval_db.refresh(owner)
    assert owner.status == TaskStatus.COMPLETED, (
        f"Reaped owner with merged PR and all-terminal sub-pipeline must be "
        f"reconciled to COMPLETED, got {owner.status}: "
        f"{owner.latest_status_message!r}"
    )
    assert (owner.context or {}).get("pr_merged") is True, (
        "Reconciled reaped owner must have pr_merged stamped in context"
    )


@pytest.mark.asyncio
async def test_reaped_owner_with_open_pr_rearms_pr_state_check_pending(eval_db, mocker):
    """Defect 1 (open-PR branch): when all Review/Fix children are terminal but
    the owner's PR is still OPEN, the sweep must re-arm ``_pr_state_check_pending``
    and leave the owner EVALUATING — NOT reconcile to COMPLETED and NOT fall
    through to the generic missed-rollup trigger. The existing consumer then
    spawns the next Review iteration on a later sweep.

    Regression for task 3eb15e9e / PR #89: the Fix child pushed the final commit
    and COMPLETED, but no re-review → merge ran, leaving the PR open and the owner
    orphaned in EVALUATING ~13h with no marker to drive recovery.
    """
    from app.models import Artifact, ArtifactType

    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.services.broadcast_to_project", new_callable=AsyncMock, create=True
    )
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new=AsyncMock(return_value=("tok", "owner", "repo")),
    )
    # PR is still OPEN (the Fix child finished but no re-review+merge ran).
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={
                "state": "open",
                "merged": False,
                "base": {"ref": "main"},
            }
        ),
    )

    project = Project(
        name="Reaped Owner Open PR Rearm",
        user_id="user-reaped-d1-open",
        integration_branch="main",
        promotion_target="main",
    )
    eval_db.add(project)
    await eval_db.commit()
    await eval_db.refresh(project)

    owner = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "original work"}],
        status=TaskStatus.EVALUATING,
        context={},
        status_log=[{"t": _naive_utc_ago(hours=2).isoformat(), "msg": "→ EVALUATING"}],
    )
    eval_db.add(owner)
    await eval_db.commit()
    await eval_db.refresh(owner)

    eval_db.add(
        Artifact(
            task_id=owner.id,
            artifact_type=ArtifactType.PULL_REQUEST,
            pr_url="https://github.com/owner/repo/pull/89",
        )
    )
    review_child = Task(
        project_id=project.id,
        parent_task_id=owner.id,
        payloads=[
            {
                "type": "review",
                "pr_number": 89,
                "original_task_id": str(owner.id),
                "prompt": "review PR #89",
            }
        ],
        status=TaskStatus.COMPLETED,
        context={"review_loop": {"pr_number": 89}},
    )
    fix_child = Task(
        project_id=project.id,
        parent_task_id=owner.id,
        payloads=[
            {
                "type": "fix",
                "pr_number": 89,
                "original_task_id": str(owner.id),
                "prompt": "fix CI on PR #89",
            }
        ],
        status=TaskStatus.COMPLETED,
        context={"review_loop": {"pr_number": 89}},
    )
    eval_db.add(review_child)
    eval_db.add(fix_child)
    await eval_db.commit()

    await _check_evaluating_timeouts()

    await eval_db.refresh(owner)
    assert owner.status == TaskStatus.EVALUATING, (
        f"Reaped owner with an OPEN PR and all-terminal sub-pipeline must stay "
        f"EVALUATING (re-armed for the next Review iteration), not reconcile to "
        f"{owner.status}: {owner.latest_status_message!r}"
    )
    assert (owner.context or {}).get("_pr_state_check_pending") is True, (
        "Open-PR reaped owner must have _pr_state_check_pending re-armed so the "
        "existing consumer spawns the next Review iteration"
    )
    assert not (owner.context or {}).get("pr_merged"), (
        "An open (unmerged) PR must NOT stamp pr_merged on the owner"
    )


@pytest.mark.asyncio
async def test_reaped_owner_markerless_merged_pr_fallback_reconciles(eval_db, mocker):
    """Defect 2: an EVALUATING reaped-owner task with no ``review_loop`` or
    ``_pr_state_check_pending`` must be reconciled to COMPLETED when its PR
    merges out-of-band while the sub-pipeline is still active, once the 30-min
    grace period elapses.

    Regression for task 1c286f23: PR #10112 merged at 04:38Z but the owner
    stayed EVALUATING for 17+ hours because no marker remained to trigger any
    existing sweep consumer path.
    """
    from app.models import Artifact, ArtifactType

    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.services.broadcast_to_project", new_callable=AsyncMock, create=True
    )
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
        name="Reaped Owner Markerless Fallback",
        user_id="user-reaped-d2",
        integration_branch="main",
        promotion_target="main",
    )
    eval_db.add(project)
    await eval_db.commit()
    await eval_db.refresh(project)

    # No review_loop, no _pr_state_check_pending, no pr_merged — the "invisible"
    # state left after the reaper clears the write-ahead flag.
    owner = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "original work"}],
        status=TaskStatus.EVALUATING,
        context={},
        vm_instance_id=None,
        status_log=[{"t": _naive_utc_ago(hours=2).isoformat(), "msg": "→ EVALUATING"}],
    )
    eval_db.add(owner)
    await eval_db.commit()
    await eval_db.refresh(owner)

    eval_db.add(
        Artifact(
            task_id=owner.id,
            artifact_type=ArtifactType.PULL_REQUEST,
            pr_url="https://github.com/owner/repo/pull/10112",
        )
    )
    # One still-active review child → all_children_terminal=False so only the
    # marker-less fallback path (Defect 2) can reconcile the owner.
    active_review = Task(
        project_id=project.id,
        parent_task_id=owner.id,
        payloads=[
            {
                "type": "review",
                "pr_number": 10112,
                "original_task_id": str(owner.id),
                "prompt": "review PR #10112",
            }
        ],
        status=TaskStatus.IN_PROGRESS,
        context={"review_loop": {"pr_number": 10112}},
        last_heartbeat=_naive_utc_ago(hours=1),
    )
    eval_db.add(active_review)
    await eval_db.commit()

    await _check_evaluating_timeouts()

    await eval_db.refresh(owner)
    assert owner.status == TaskStatus.COMPLETED, (
        f"Reaped owner with out-of-band merged PR must be reconciled to COMPLETED "
        f"by the marker-less fallback after grace period, got {owner.status}: "
        f"{owner.latest_status_message!r}"
    )
    assert (owner.context or {}).get("pr_merged") is True, (
        "Reconciled reaped owner must have pr_merged stamped in context"
    )


# ── Extracted approval-gate handler: direct dispositions + return contract ────


@pytest.mark.asyncio
async def test_handle_approval_gate_timeout_dispositions_and_contract(eval_db, mocker):
    """Directly exercise the extracted ``_handle_approval_gate_timeout`` handler.

    Locks the structural-extraction contract: the handler returns ``True`` for
    every row it terminalizes/parks (so the caller ``continue``s) and ``False``
    only when it leaves the row for the remaining recovery branches — and both
    dispositions land exactly as before:

      * a deliberate spend/budget hold past its window → CLEAN non-failure
        ``CANCELLED`` (requires_approval cleared, ``spend_approval_declined``
        stamped, NO ``record_task_result`` / failure alert);
      * a non-marker (review-escalation) hold past 2h → ``FAILED`` with a failed
        task result recorded and a failure alert dispatched;
      * a non-marker hold still within the 2h window → handler returns ``False``
        and leaves the task EVALUATING (fall-through);
      * a task without ``requires_approval`` → handler returns ``False`` immediately.
    """
    from app.recovery_evaluating_approval import (
        _handle_approval_gate_timeout,
        APPROVAL_GATE_FAIL_SECONDS,
    )

    mocker.patch("app.services.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
    rec = mocker.patch("app.routers.agent_status.record_task_result")
    fail_alert = AsyncMock()

    project = Project(name="Approval Handler Direct", user_id="user1")
    eval_db.add(project)
    await eval_db.commit()
    await eval_db.refresh(project)

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    spend_hours = 24
    spend_fail_seconds = spend_hours * 3600

    def _make(ctx, *, requires_approval=True, ttype="research"):
        t = Task(
            project_id=project.id,
            payloads=[{"type": ttype, "prompt": "approval-gate parent"}],
            status=TaskStatus.EVALUATING,
            requires_approval=requires_approval,
            context=dict(ctx),
        )
        eval_db.add(t)
        return t

    spend_declined = _make(
        {
            "approval_hold_reason": "spend_budget",
            "_approval_set_at": _naive_utc_ago(hours=25).isoformat(),
            "_spend_hold_created_notified": True,
            "_spend_hold_expiring_notified": True,
        }
    )
    review_failed = _make(
        {"_approval_set_at": _naive_utc_ago(hours=3).isoformat()},
        ttype="review",
    )
    within_window = _make({"_approval_set_at": _naive_utc_ago(minutes=30).isoformat()})
    no_approval = _make({}, requires_approval=False)
    await eval_db.commit()
    for t in (spend_declined, review_failed, within_window, no_approval):
        await eval_db.refresh(t)

    async def _call(task):
        return await _handle_approval_gate_timeout(
            task,
            task.context or {},
            eval_db,
            now,
            spend_approval_fail_seconds=spend_fail_seconds,
            spend_hours=spend_hours,
            maybe_create_failure_alert=fail_alert,
        )

    # 1) Spend/budget hold past window → handled (True), CLEAN CANCELLED.
    assert await _call(spend_declined) is True
    await eval_db.refresh(spend_declined)
    assert spend_declined.status == TaskStatus.CANCELLED
    assert spend_declined.requires_approval is False
    assert spend_declined.context.get("spend_approval_declined") is True
    assert "declined" in (spend_declined.latest_status_message or "").lower()
    # Clean decline is NOT a task failure.
    rec.assert_not_called()
    fail_alert.assert_not_called()

    # 2) Non-marker hold past 2h → handled (True), FAILED + failed result recorded.
    assert await _call(review_failed) is True
    await eval_db.refresh(review_failed)
    assert review_failed.status == TaskStatus.FAILED
    assert "approval" in (review_failed.latest_status_message or "").lower()
    rec.assert_called_once()
    assert rec.call_args.kwargs.get("success") is False
    fail_alert.assert_awaited_once()

    # 3) Non-marker hold still within the 2h window → fall through (False), untouched.
    assert _naive_utc_ago(minutes=30) > now - timedelta(
        seconds=APPROVAL_GATE_FAIL_SECONDS
    )
    assert await _call(within_window) is False
    await eval_db.refresh(within_window)
    assert within_window.status == TaskStatus.EVALUATING

    # 4) Not approval-gated → fall through (False) without side effects.
    assert await _call(no_approval) is False
    await eval_db.refresh(no_approval)
    assert no_approval.status == TaskStatus.EVALUATING


# ── Orphaned direction provider task sweep ────────────────────────────────────


@pytest.mark.asyncio
async def test_sweep_orphaned_provider_task_detached_not_cancelled_for_retired_direction(
    eval_db, mocker
):
    """An EVALUATING task whose direction is retired must be DETACHED, not cancelled.

    direction_id is provenance, not a kill-tether: a retired direction must never
    cancel in-flight work.  The sweep should clear the stale direction_id while
    keeping the task EVALUATING (the generic EVALUATING sweep then resolves it via
    its own children/PR).
    """
    from app.reaper_evaluating_sweeps import sweep_orphaned_direction_provider_tasks

    project = Project(name="Orphan Provider Test", user_id="user1")
    eval_db.add(project)
    await eval_db.commit()
    await eval_db.refresh(project)

    direction_key = "d-orphan01"

    # Direction row with a retired status.
    direction = Direction(
        project_id=project.id,
        direction_key=direction_key,
        status="completed",
        data={"id": direction_key, "status": "completed"},
    )
    eval_db.add(direction)
    await eval_db.commit()

    # EVALUATING task tied to that direction (no VM held — slots already released).
    task = Task(
        project_id=project.id,
        payloads=[{"type": "research", "prompt": "direction provider"}],
        status=TaskStatus.EVALUATING,
        context={"direction_id": direction_key},
    )
    eval_db.add(task)
    await eval_db.commit()
    await eval_db.refresh(task)

    detached = await sweep_orphaned_direction_provider_tasks(eval_db)

    assert detached == 1
    await eval_db.refresh(task)
    # Must NOT be cancelled — status is preserved.
    assert task.status == TaskStatus.EVALUATING
    # The stale provenance direction_id is cleared.
    assert (task.context or {}).get("direction_id") is None


@pytest.mark.asyncio
async def test_sweep_orphaned_provider_task_null_updated_at_detached(
    eval_db, mocker
):
    """A null-updated_at EVALUATING task on a closed direction is detached.

    Regression: null updated_at makes a task invisible to the staleness sweep
    (NULL < cutoff evaluates to NULL/false in SQL).  Simulate the null by
    force-setting updated_at to None after insert, then verify the orphan sweep
    still detects the task via the direction-status check and detaches it (clears
    direction_id) without cancelling it.
    """
    from sqlalchemy import text
    from app.reaper_evaluating_sweeps import sweep_orphaned_direction_provider_tasks

    project = Project(name="Null UpdatedAt Test", user_id="user1")
    eval_db.add(project)
    await eval_db.commit()
    await eval_db.refresh(project)

    direction_key = "d-nullupd01"
    direction = Direction(
        project_id=project.id,
        direction_key=direction_key,
        status="retired",
        data={"id": direction_key, "status": "retired"},
    )
    eval_db.add(direction)
    await eval_db.commit()

    task = Task(
        project_id=project.id,
        payloads=[{"type": "refactor", "prompt": "null updated_at provider"}],
        status=TaskStatus.EVALUATING,
        context={"direction_id": direction_key},
    )
    eval_db.add(task)
    await eval_db.commit()
    await eval_db.refresh(task)

    # Force updated_at to NULL via raw SQL to simulate the production scenario
    # where some insertion path bypassed the ORM default (SQLite does not enforce
    # NOT NULL the same way Postgres does when using a raw UPDATE).
    await eval_db.execute(
        text("UPDATE task SET updated_at = NULL WHERE id = :id"),
        {"id": str(task.id)},
    )
    await eval_db.commit()

    detached = await sweep_orphaned_direction_provider_tasks(eval_db)

    assert detached == 1
    await eval_db.refresh(task)
    assert task.status == TaskStatus.EVALUATING
    assert (task.context or {}).get("direction_id") is None


@pytest.mark.asyncio
async def test_sweep_orphaned_provider_task_active_direction_not_touched(
    eval_db, mocker
):
    """An EVALUATING task whose direction is still active must be left untouched."""
    from app.reaper_evaluating_sweeps import sweep_orphaned_direction_provider_tasks

    project = Project(name="Active Direction Test", user_id="user1")
    eval_db.add(project)
    await eval_db.commit()
    await eval_db.refresh(project)

    direction_key = "d-active01"
    direction = Direction(
        project_id=project.id,
        direction_key=direction_key,
        status="active",
        data={"id": direction_key, "status": "active"},
    )
    eval_db.add(direction)
    await eval_db.commit()

    task = Task(
        project_id=project.id,
        payloads=[{"type": "research", "prompt": "active direction provider"}],
        status=TaskStatus.EVALUATING,
        context={"direction_id": direction_key},
    )
    eval_db.add(task)
    await eval_db.commit()
    await eval_db.refresh(task)

    detached = await sweep_orphaned_direction_provider_tasks(eval_db)

    assert detached == 0
    await eval_db.refresh(task)
    assert task.status == TaskStatus.EVALUATING
    # Active direction → provenance is preserved.
    assert (task.context or {}).get("direction_id") == direction_key


@pytest.mark.asyncio
async def test_sweep_orphaned_provider_task_no_direction_row_not_touched(
    eval_db, mocker
):
    """An EVALUATING task with a direction_id that has no Direction row is left alone.

    Missing row != retired; the sweep must not touch tasks for directions that
    simply were never synced to the Direction table.
    """
    from app.reaper_evaluating_sweeps import sweep_orphaned_direction_provider_tasks

    project = Project(name="No Direction Row Test", user_id="user1")
    eval_db.add(project)
    await eval_db.commit()
    await eval_db.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "research", "prompt": "no direction row"}],
        status=TaskStatus.EVALUATING,
        context={"direction_id": "d-missing01"},
    )
    eval_db.add(task)
    await eval_db.commit()
    await eval_db.refresh(task)

    detached = await sweep_orphaned_direction_provider_tasks(eval_db)

    assert detached == 0
    await eval_db.refresh(task)
    assert task.status == TaskStatus.EVALUATING
    # Missing direction row → provenance is preserved.
    assert (task.context or {}).get("direction_id") == "d-missing01"


@pytest.mark.asyncio
async def test_sweep_orphaned_provider_task_with_open_pr_not_cancelled(
    eval_db, mocker
):
    """An EVALUATING task with a retired direction but an open PR must be detached, not cancelled.

    When a direction expires/completes, tasks that have already opened a PR have
    salvageable work — the PR should be allowed to land rather than the task being
    killed. The sweep should clear the direction_id from context (detaching it so
    subsequent sweeps skip it) but preserve the task's EVALUATING status and PR.
    """
    from app.reaper_evaluating_sweeps import sweep_orphaned_direction_provider_tasks

    project = Project(name="Orphan With PR Test", user_id="user1")
    eval_db.add(project)
    await eval_db.commit()
    await eval_db.refresh(project)

    direction_key = "d-with-pr01"
    direction = Direction(
        project_id=project.id,
        direction_key=direction_key,
        status="expired",
        data={"id": direction_key, "status": "expired"},
    )
    eval_db.add(direction)
    await eval_db.commit()

    # EVALUATING task tied to that direction with an open PR.
    task = Task(
        project_id=project.id,
        payloads=[
            {
                "type": "create",
                "prompt": "implementation task",
                "pr_number": 12345,
            }
        ],
        status=TaskStatus.EVALUATING,
        context={"direction_id": direction_key},
    )
    eval_db.add(task)
    await eval_db.commit()
    await eval_db.refresh(task)

    # The open-PR task is detached just like every other case.
    detached = await sweep_orphaned_direction_provider_tasks(eval_db)

    assert detached == 1
    await eval_db.refresh(task)
    # Task must NOT be cancelled.
    assert task.status == TaskStatus.EVALUATING
    # Direction_id should be removed from context.
    assert (task.context or {}).get("direction_id") is None
    # The PR should still be in the payload.
    assert (task.payloads or [{}])[0].get("pr_number") == 12345


@pytest.mark.asyncio
async def test_sweep_orphaned_provider_task_with_live_child_not_cancelled(
    eval_db, mocker
):
    """Reproduces the incident: a retired direction must not destroy in-flight work.

    An audit/research provider task that already spawned downstream fix tasks (but
    holds no PR) used to be CANCELLED when its direction retired, which cascaded to
    FAIL the parent and tear down the shared staging branch — stranding the spawned
    fix tasks. After the fix, the provider task is merely DETACHED (direction_id
    cleared, status stays EVALUATING) and its live descendant is untouched.
    """
    from app.reaper_evaluating_sweeps import sweep_orphaned_direction_provider_tasks

    project = Project(name="Live Child Provider Test", user_id="user1")
    eval_db.add(project)
    await eval_db.commit()
    await eval_db.refresh(project)

    direction_key = "d-livechild01"
    direction = Direction(
        project_id=project.id,
        direction_key=direction_key,
        status="superseded",
        data={"id": direction_key, "status": "superseded"},
    )
    eval_db.add(direction)
    await eval_db.commit()

    # EVALUATING provider task tied to the now-retired direction (no PR).
    provider = Task(
        project_id=project.id,
        payloads=[{"type": "research", "prompt": "audit provider"}],
        status=TaskStatus.EVALUATING,
        context={"direction_id": direction_key},
    )
    eval_db.add(provider)
    await eval_db.commit()
    await eval_db.refresh(provider)

    # A live (QUEUED) descendant fix task spawned by the provider.
    child = Task(
        project_id=project.id,
        parent_task_id=provider.id,
        payloads=[{"type": "create", "prompt": "spawned fix"}],
        status=TaskStatus.QUEUED,
        context={"direction_id": direction_key},
    )
    eval_db.add(child)
    await eval_db.commit()
    await eval_db.refresh(child)

    detached = await sweep_orphaned_direction_provider_tasks(eval_db)

    # Only the EVALUATING provider is in scope; the QUEUED child is not swept.
    assert detached == 1
    await eval_db.refresh(provider)
    await eval_db.refresh(child)
    # Provider must NOT be cancelled — just detached.
    assert provider.status == TaskStatus.EVALUATING
    assert (provider.context or {}).get("direction_id") is None
    # The live descendant is completely untouched.
    assert child.status == TaskStatus.QUEUED
    assert (child.context or {}).get("direction_id") == direction_key
