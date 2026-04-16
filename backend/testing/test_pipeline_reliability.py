"""
Tests for pipeline reliability edge cases in the Blueprint backend.

Covers scenarios that could cause the pipeline to stall or waste resources over 24h:
  1. Orchestrator backoff counter escalates on consecutive errors (not reset per cycle)
  2. EVALUATING timeout cascades cancel to non-terminal children
  3. Reaper uses fresh DB task for provision_failures (not stale in-memory copy)
  4. CI polling fails a task when its PR is deleted (404)
  5. CI polling retries on transient GitHub failure (500)
  6. Deadlock detection clears failed/cancelled dependencies
"""
import asyncio
import contextlib
import pytest
import pytest_asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel

import app.orchestrator as _orch_module
import app.shared_state as _shared_state
from app.services import _check_evaluating_timeouts
from app.models import Project, Task, TaskStatus

# ── SQLite JSONB compatibility ────────────────────────────────────────────────

from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler as _SQLiteTypeCompiler
if not hasattr(_SQLiteTypeCompiler, "visit_JSONB"):
    _SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: self.visit_JSON(type_, **kw)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _naive_utc_ago(**kwargs) -> datetime:
    """Return a naive UTC datetime (no tzinfo) that is ``kwargs`` in the past."""
    return (datetime.now(timezone.utc) - timedelta(**kwargs)).replace(tzinfo=None)


def _naive_utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture()
async def eval_db(mocker):
    """Isolated in-memory SQLite DB for _check_evaluating_timeouts tests.

    Patches app.services.AsyncSessionLocal so the function uses this DB.
    """
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


@pytest_asyncio.fixture()
async def orch_db(mocker):
    """Isolated in-memory SQLite DB for orchestrator_worker tests.

    Patches app.orchestrator.AsyncSessionLocal with a sessionmaker so all
    DB calls (main cycle + deadlock sub-sessions) use the same in-memory DB.
    Also patches heavy external dependencies so the worker can run one cycle
    without hitting GCP or GitHub.
    """
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

    mocker.patch("app.orchestrator.AsyncSessionLocal", _Session)

    # Patch app.main functions imported lazily by orchestrator_worker()
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch("app.main._ensure_vm_deleted", new_callable=AsyncMock)
    mocker.patch("app.main._get_actual_gcp_vm_count", return_value=0)
    mocker.patch("app.main.load_max_plan_pool", new_callable=AsyncMock)
    mocker.patch("app.main.select_max_plan_account", new_callable=AsyncMock)
    mocker.patch("app.main.release_max_plan_account", new_callable=AsyncMock)
    mocker.patch("app.main.recheck_disabled_accounts", new_callable=AsyncMock)
    mocker.patch("app.routers.agent_status.record_task_result")  # sync function, no AsyncMock needed
    mocker.patch("app.deploy_health._check_monitoring_window", new_callable=AsyncMock)
    mocker.patch("app.deploy_health._check_external_health", new_callable=AsyncMock)
    mocker.patch("app.deploy_health._check_pause_mode", new_callable=AsyncMock)
    mocker.patch("app.autonomous_research._check_idle_research", new_callable=AsyncMock)
    mocker.patch("app.services._recover_orphaned_tasks", new_callable=AsyncMock)
    mocker.patch("app.main._create_orchestrator_error_alerts", new_callable=AsyncMock)
    mocker.patch("app.orchestrator._persist_zone_health", new_callable=AsyncMock)
    mocker.patch("app.main._extract_task_model", return_value=None)
    mocker.patch("app.main._maybe_create_failure_alert", new_callable=AsyncMock)

    # Advance periodic timers so they don't trigger on the first cycle
    now = _naive_utcnow()
    _orch_module._last_pool_refresh = now
    _orch_module._last_zone_health_persist = now
    _orch_module._last_orphan_check = now
    _orch_module._last_evaluating_timeout_check = now

    # Clear task_results so failure-rate gate doesn't trigger
    _shared_state.deploy_monitoring["task_results"].clear()

    async with _Session() as session:
        yield session, _Session

    async with _engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
    await _engine.dispose()


async def _run_one_cycle(mocker):
    """Run orchestrator_worker() for exactly one cycle.

    Patches asyncio.sleep to raise CancelledError, which the worker loop
    catches and uses to break cleanly.
    """
    mocker.patch("asyncio.sleep", side_effect=asyncio.CancelledError())
    with contextlib.suppress(asyncio.CancelledError):
        await _orch_module.orchestrator_worker()


# ══════════════════════════════════════════════════════════════════════════════
# Test 1 — Backoff escalation
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_orchestrator_backoff_escalates_on_consecutive_errors(mocker):
    """After N consecutive errors, backoff = min(0.5 * 2^(N-1), 60).

    The counter must NOT be reset at the start of each cycle — the pre-fix bug
    caused every failed cycle to behave as error #1 (always 0.5s backoff).
    This test verifies escalation by running the loop through 3 error cycles
    and checking that asyncio.sleep receives distinct increasing values.
    """
    saved_errors = _orch_module._orchestrator_consecutive_errors
    _orch_module._orchestrator_consecutive_errors = 0

    # ── Part 1: formula correctness ─────────────────────────────────────────
    expected = {1: 0.5, 2: 1.0, 3: 2.0, 4: 4.0, 5: 8.0, 6: 16.0, 7: 32.0, 8: 60.0}
    for n, exp_backoff in expected.items():
        backoff = min(0.5 * (2 ** (n - 1)), 60)
        assert backoff == exp_backoff, f"N={n}: expected {exp_backoff}s, got {backoff}s"

    # ── Part 2: counter accumulates across cycles (not reset at cycle start) ─
    sleep_calls = []
    call_count = [0]

    # Must inherit from BaseException, not Exception — the orchestrator worker
    # has broad `except Exception` blocks around both the main cycle and the
    # sleep call (orchestrator.py:1297, 1357) that would otherwise swallow this
    # signal, causing the test to loop forever until pytest-timeout fires.
    class _StopAfter3(BaseException):
        pass

    async def _capturing_sleep(n):
        sleep_calls.append(n)
        call_count[0] += 1
        if call_count[0] >= 3:
            raise _StopAfter3()

    # Force the main try-block to fail by making the DB session raise
    mocker.patch("app.orchestrator.AsyncSessionLocal", side_effect=RuntimeError("simulated DB error"))
    mocker.patch("asyncio.sleep", side_effect=_capturing_sleep)
    mocker.patch("app.deploy_health._check_monitoring_window", new_callable=AsyncMock)
    mocker.patch("app.deploy_health._check_external_health", new_callable=AsyncMock)
    mocker.patch("app.deploy_health._check_pause_mode", new_callable=AsyncMock)
    mocker.patch("app.autonomous_research._check_idle_research", new_callable=AsyncMock)
    mocker.patch("app.orchestrator._persist_zone_health", new_callable=AsyncMock)
    mocker.patch("app.main.load_max_plan_pool", new_callable=AsyncMock)
    mocker.patch("app.main._create_orchestrator_error_alerts", new_callable=AsyncMock)

    _orch_module._last_pool_refresh = _naive_utcnow()
    _orch_module._last_zone_health_persist = _naive_utcnow()

    # Reset periodic-check timestamps so the unmocked _recover_orphaned_tasks /
    # _check_evaluating_timeouts paths aren't reached during this test.
    _orch_module._last_orphan_check = _naive_utcnow()
    _orch_module._last_evaluating_timeout_check = _naive_utcnow()

    try:
        await _orch_module.orchestrator_worker()
    except _StopAfter3:
        pass
    finally:
        _orch_module._orchestrator_consecutive_errors = saved_errors

    # With the fix: counter keeps incrementing → backoffs are 0.5, 1.0, 2.0
    # With the pre-fix bug: counter resets each cycle → all backoffs are 0.5
    assert len(sleep_calls) == 3, (
        f"Expected 3 sleep calls (one per error cycle), got {len(sleep_calls)}: {sleep_calls}"
    )
    # Backoffs include ±20% jitter, so check ranges instead of exact values
    assert 0.4 <= sleep_calls[0] <= 0.6, f"Error 1: expected ~0.5s, got {sleep_calls[0]}s"
    assert 0.8 <= sleep_calls[1] <= 1.2, (
        f"Error 2: expected ~1.0s, got {sleep_calls[1]}s. "
        f"If ~0.5, the counter is being reset at the start of each cycle (the bug)."
    )
    assert 1.6 <= sleep_calls[2] <= 2.4, (
        f"Error 3: expected ~2.0s, got {sleep_calls[2]}s. "
        f"If ~0.5, the counter is being reset at the start of each cycle (the bug)."
    )


# ══════════════════════════════════════════════════════════════════════════════
# Test 2 — EVALUATING timeout cascades cancel to children
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_evaluating_timeout_cascades_cancel_to_children(eval_db, mocker):
    """When a parent EVALUATING task times out (>6h), all non-terminal children
    must be set to CANCELLED with an appropriate status message."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch("app.main._ensure_vm_deleted", new_callable=AsyncMock)

    project = Project(name="Cascade Cancel Test", user_id="user1")
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

    # Set parent to appear 7 hours into EVALUATING
    evaluating_since = _naive_utc_ago(hours=7)
    parent.status_log = [
        {"t": evaluating_since.isoformat(), "msg": "Status changed: IN_PROGRESS → EVALUATING"}
    ]
    eval_db.add(parent)
    await eval_db.commit()
    await eval_db.refresh(parent)

    # Three non-terminal children in different active states
    child_queued = Task(
        project_id=project.id,
        parent_task_id=parent.id,
        payloads=[{"type": "create", "prompt": "queued child"}],
        status=TaskStatus.QUEUED,
    )
    child_provisioning = Task(
        project_id=project.id,
        parent_task_id=parent.id,
        payloads=[{"type": "create", "prompt": "provisioning child"}],
        status=TaskStatus.PROVISIONING,
        vm_instance_id="agent-vm-test-01",
    )
    child_in_progress = Task(
        project_id=project.id,
        parent_task_id=parent.id,
        payloads=[{"type": "create", "prompt": "in-progress child"}],
        status=TaskStatus.IN_PROGRESS,
        vm_instance_id="agent-vm-test-02",
    )
    for child in (child_queued, child_provisioning, child_in_progress):
        eval_db.add(child)
    await eval_db.commit()

    await _check_evaluating_timeouts()

    for child in (child_queued, child_provisioning, child_in_progress):
        await eval_db.refresh(child)

    assert child_queued.status == TaskStatus.CANCELLED, (
        f"Expected QUEUED child to be CANCELLED, got {child_queued.status}"
    )
    assert child_provisioning.status == TaskStatus.CANCELLED, (
        f"Expected PROVISIONING child to be CANCELLED, got {child_provisioning.status}"
    )
    assert child_in_progress.status == TaskStatus.CANCELLED, (
        f"Expected IN_PROGRESS child to be CANCELLED, got {child_in_progress.status}"
    )

    for child in (child_queued, child_provisioning, child_in_progress):
        assert "cancelled" in (child.latest_status_message or "").lower() or \
               "timed out" in (child.latest_status_message or "").lower(), (
            f"Expected cancel message, got: {child.latest_status_message!r}"
        )

    # Parent itself should be FAILED
    await eval_db.refresh(parent)
    assert parent.status == TaskStatus.FAILED, (
        f"Expected parent to be FAILED, got {parent.status}"
    )


# ══════════════════════════════════════════════════════════════════════════════
# Test 3 — Reaper uses fresh task for provision_failures
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_reaper_uses_fresh_task_for_modifications(orch_db, mocker):
    """The orchestrator's stale-PROVISIONING recovery must read provision_failures
    from the freshly-locked DB object, not from the initial scan's stale copy.

    Scenario: task has provision_failures=2 in the DB.  After recovery the
    counter should be 3 (2+1), not 1 (0+1 as if the initial stale copy were used).
    """
    session, _Session = orch_db

    project = Project(name="Reaper Fresh Task Test", user_id="user1")
    session.add(project)
    await session.commit()
    await session.refresh(project)

    # Task with 2 prior provision failures, stuck in PROVISIONING for >10 min
    stale_task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "provision test"}],
        status=TaskStatus.PROVISIONING,
        vm_instance_id="agent-vm-stale-001",
        started_at=_naive_utc_ago(minutes=15),
        last_heartbeat=None,
        context={"provision_failures": 2},
    )
    session.add(stale_task)
    await session.commit()
    await session.refresh(stale_task)
    task_id = stale_task.id

    await _run_one_cycle(mocker)

    # Re-read the task from the DB to see the updated state
    async with _Session() as verify_session:
        from sqlmodel import select as _select
        result = await verify_session.execute(_select(Task).where(Task.id == task_id))
        updated = result.scalars().first()

    assert updated is not None

    # The orchestrator reads provision_failures from the freshly-locked task
    # (fresh_t), not from the stale initial scan result (t).
    # With DB value 2: provision_failures = 2 + 1 = 3 → hits retry limit → FAILED.
    # With stale value 0: provision_failures = 0 + 1 = 1 → below limit → requeued.
    assert updated.status == TaskStatus.FAILED, (
        f"Expected task to be FAILED (provision_failures=2+1=3 hits max_provision_retries=3), "
        f"got {updated.status}. "
        f"If status is QUEUED, the stale value 0 was used (0+1=1 < 3), not the DB value 2."
    )

    # The failure message must say "3 provision attempts" not "1 provision attempts"
    msg = updated.latest_status_message or ""
    assert "3" in msg, (
        f"Expected 'Failed after 3 provision attempts' (fresh DB value 2+1=3), "
        f"got: {msg!r}. "
        f"If message says '1', the stale in-memory value (0) was used instead of DB value (2)."
    )


@pytest.mark.asyncio
async def test_stale_provision_failure_creates_alert(orch_db, mocker):
    """When a PROVISIONING task exceeds its provision retry limit, _maybe_create_failure_alert
    must be called so the failure is visible to users (not a silent failure)."""
    session, _Session = orch_db

    project = Project(name="Alert Test", user_id="user1")
    session.add(project)
    await session.commit()
    await session.refresh(project)

    stale_task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "alert test"}],
        status=TaskStatus.PROVISIONING,
        vm_instance_id="agent-vm-alert-001",
        started_at=_naive_utc_ago(minutes=15),
        last_heartbeat=None,
        context={"provision_failures": 2},
    )
    session.add(stale_task)
    await session.commit()
    await session.refresh(stale_task)

    # Re-patch alert so we can assert on it (overrides the fixture's no-op mock)
    alert_mock = mocker.patch("app.main._maybe_create_failure_alert", new_callable=AsyncMock)
    await _run_one_cycle(mocker)

    alert_mock.assert_called_once()
    call_args = alert_mock.call_args
    assert "provision" in call_args[0][2].lower(), (
        f"Alert error_snippet should mention provisioning, got: {call_args[0][2]!r}"
    )


# ══════════════════════════════════════════════════════════════════════════════
# Test 4 — CI polling fails task on deleted PR (404)
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_ci_polling_deleted_pr_fails_task(orch_db, mocker):
    """When get_pull_request() raises a 404-style error (PR deleted), the
    CI-waiting EVALUATING task should transition to FAILED rather than staying
    EVALUATING and generating repeated 404 errors on every poll cycle.

    NOTE: This test documents the desired behavior introduced by the
    'detect and fail CI tasks polling deleted PRs' fix.  It will fail until
    that fix is applied (currently the task stays EVALUATING with an error msg).
    """
    session, _Session = orch_db

    project = Project(name="CI 404 Test", user_id="user1")
    session.add(project)
    await session.commit()
    await session.refresh(project)

    # EVALUATING task waiting on CI, started recently (within 30-min timeout)
    ci_task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "ci test task", "branch": "feat/test"}],
        status=TaskStatus.EVALUATING,
        context={
            "review_loop": {
                "ci_waiting": True,
                "pr_number": 42,
                "branch": "feat/test",
                "base_branch": "main",
                "ci_wait_started": _naive_utcnow().isoformat(),
            }
        },
    )
    session.add(ci_task)
    await session.commit()
    await session.refresh(ci_task)
    task_id = ci_task.id

    # Simulate a deleted PR (GitHub returns 404)
    class _NotFoundError(Exception):
        status_code = 404

    mocker.patch("app.routers.agent.get_github_token_for_task",
                 return_value=("fake-token", "owner", "repo"))
    mocker.patch("app.github.get_pull_request",
                 side_effect=_NotFoundError("PR not found"))

    await _run_one_cycle(mocker)

    async with _Session() as verify_session:
        from sqlmodel import select as _select
        result = await verify_session.execute(_select(Task).where(Task.id == task_id))
        updated = result.scalars().first()

    assert updated is not None
    assert updated.status == TaskStatus.FAILED, (
        f"Expected task to be FAILED after 404, got {updated.status}. "
        f"Status message: {updated.latest_status_message!r}. "
        f"A 404 on the PR means it was deleted and should not be polled forever."
    )
    assert updated.latest_status_message, "Expected a failure message to be set"


@pytest.mark.asyncio
async def test_ci_polling_deleted_pr_creates_alert(orch_db, mocker):
    """When a PR returns 404 during CI polling, _maybe_create_failure_alert must be
    called so the silent failure is visible to users."""
    session, _Session = orch_db

    project = Project(name="CI 404 Alert Test", user_id="user1")
    session.add(project)
    await session.commit()
    await session.refresh(project)

    ci_task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "ci alert test", "branch": "feat/alert"}],
        status=TaskStatus.EVALUATING,
        context={
            "review_loop": {
                "ci_waiting": True,
                "pr_number": 99,
                "branch": "feat/alert",
                "base_branch": "main",
                "ci_wait_started": _naive_utcnow().isoformat(),
            }
        },
    )
    session.add(ci_task)
    await session.commit()
    await session.refresh(ci_task)

    class _NotFoundError(Exception):
        status_code = 404

    mocker.patch("app.routers.agent.get_github_token_for_task",
                 return_value=("fake-token", "owner", "repo"))
    mocker.patch("app.github.get_pull_request",
                 side_effect=_NotFoundError("PR not found"))

    # Re-patch alert so we can assert on it (overrides the fixture's no-op mock)
    alert_mock = mocker.patch("app.main._maybe_create_failure_alert", new_callable=AsyncMock)
    await _run_one_cycle(mocker)

    alert_mock.assert_called_once()
    call_args = alert_mock.call_args
    assert "404" in call_args[0][2], (
        f"Alert error_snippet should mention 404, got: {call_args[0][2]!r}"
    )


# ══════════════════════════════════════════════════════════════════════════════
# Test 5 — CI polling transient failure retries
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_ci_polling_transient_failure_retries(orch_db, mocker):
    """When get_pull_request() fails transiently (500 error), the CI-waiting
    EVALUATING task should stay EVALUATING to be retried on the next cycle.

    Transient errors (network blip, GitHub 5xx) must not permanently fail tasks.
    """
    session, _Session = orch_db

    project = Project(name="CI 500 Test", user_id="user1")
    session.add(project)
    await session.commit()
    await session.refresh(project)

    ci_task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "ci retry task", "branch": "feat/retry"}],
        status=TaskStatus.EVALUATING,
        context={
            "review_loop": {
                "ci_waiting": True,
                "pr_number": 99,
                "branch": "feat/retry",
                "base_branch": "main",
                "ci_wait_started": _naive_utcnow().isoformat(),
            }
        },
    )
    session.add(ci_task)
    await session.commit()
    await session.refresh(ci_task)
    task_id = ci_task.id

    # Simulate a transient GitHub server error
    class _ServerError(Exception):
        status_code = 500

    mocker.patch("app.routers.agent.get_github_token_for_task",
                 return_value=("fake-token", "owner", "repo"))
    mocker.patch("app.github.get_pull_request",
                 side_effect=_ServerError("Internal Server Error"))

    await _run_one_cycle(mocker)

    async with _Session() as verify_session:
        from sqlmodel import select as _select
        result = await verify_session.execute(_select(Task).where(Task.id == task_id))
        updated = result.scalars().first()

    assert updated is not None
    assert updated.status == TaskStatus.EVALUATING, (
        f"Expected task to remain EVALUATING after transient 500 error, got {updated.status}. "
        f"Transient errors should be retried, not permanently fail the task."
    )


# ══════════════════════════════════════════════════════════════════════════════
# Test 6 — Deadlock detection clears failed dependencies
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_deadlock_detection_clears_failed_deps(orch_db, mocker):
    """Deadlock detection must clear depends_on for tasks whose dependencies are
    in terminal states (FAILED/CANCELLED), allowing them to be dispatched.

    Scenario: A depends on B, B depends on C.  C is FAILED.
    Expected: deadlock detection clears B's depends_on (unblocking B),
    and on the next scan also clears A's depends_on.
    """
    session, _Session = orch_db

    project = Project(name="Deadlock Test", user_id="user1")
    session.add(project)
    await session.commit()
    await session.refresh(project)

    # C: already FAILED (terminal dependency)
    task_c = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "task C"}],
        status=TaskStatus.FAILED,
        latest_status_message="C failed",
        created_at=_naive_utc_ago(minutes=15),
    )
    session.add(task_c)
    await session.commit()
    await session.refresh(task_c)

    # B: QUEUED but depends on FAILED C
    task_b = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "task B"}],
        status=TaskStatus.QUEUED,
        depends_on=[str(task_c.id)],
        created_at=_naive_utc_ago(minutes=12),
    )
    session.add(task_b)
    await session.commit()
    await session.refresh(task_b)

    # A: QUEUED and depends on B (which is stuck)
    task_a = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "task A"}],
        status=TaskStatus.QUEUED,
        depends_on=[str(task_b.id)],
        created_at=_naive_utc_ago(minutes=11),
    )
    session.add(task_a)
    await session.commit()
    await session.refresh(task_a)

    task_b_id = task_b.id

    # Force the deadlock check to run this cycle (reset throttle timestamp)
    _orch_module._last_deadlock_check = 0.0

    await _run_one_cycle(mocker)

    # Re-read from DB to check updated state
    async with _Session() as verify_session:
        from sqlmodel import select as _select
        b_result = await verify_session.execute(_select(Task).where(Task.id == task_b_id))
        updated_b = b_result.scalars().first()

    assert updated_b is not None
    assert updated_b.depends_on == [] or updated_b.depends_on is None, (
        f"Expected B's depends_on to be cleared after C failed, "
        f"got {updated_b.depends_on!r}"
    )
    assert "deadlock" in (updated_b.latest_status_message or "").lower() or \
           "dependencies cleared" in (updated_b.latest_status_message or "").lower(), (
        f"Expected deadlock message on task B, got: {updated_b.latest_status_message!r}"
    )


# ══════════════════════════════════════════════════════════════════════════════
# Test 6b — False-positive: A→B→D(running) must NOT clear A's deps
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_deadlock_no_false_positive_when_dep_has_active_parent(orch_db, mocker):
    """Tasks blocked on a chain that includes an active (IN_PROGRESS) task
    must NOT have their depends_on cleared — they are NOT deadlocked.

    Scenario: A depends on B, B depends on D, D is IN_PROGRESS.
    The old heuristic cleared A because B was in the stuck pool; the DFS-based
    algorithm must recognise that B is not in a cycle and leave A untouched.
    """
    session, _Session = orch_db

    project = Project(name="FP Test", user_id="user1")
    session.add(project)
    await session.commit()
    await session.refresh(project)

    # D: actively running
    task_d = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "task D"}],
        status=TaskStatus.IN_PROGRESS,
        created_at=_naive_utc_ago(minutes=5),
    )
    session.add(task_d)
    await session.commit()
    await session.refresh(task_d)

    # B: QUEUED, waiting on the running D
    task_b = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "task B"}],
        status=TaskStatus.QUEUED,
        depends_on=[str(task_d.id)],
        created_at=_naive_utc_ago(minutes=12),
    )
    session.add(task_b)
    await session.commit()
    await session.refresh(task_b)

    # A: QUEUED, waiting on B (which is stuck but not deadlocked)
    task_a = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "task A"}],
        status=TaskStatus.QUEUED,
        depends_on=[str(task_b.id)],
        created_at=_naive_utc_ago(minutes=11),
    )
    session.add(task_a)
    await session.commit()
    await session.refresh(task_a)

    task_a_id = task_a.id
    task_b_id = task_b.id
    original_a_deps = list(task_a.depends_on or [])
    original_b_deps = list(task_b.depends_on or [])

    _orch_module._last_deadlock_check = 0.0

    await _run_one_cycle(mocker)

    async with _Session() as verify_session:
        from sqlmodel import select as _select
        a_result = await verify_session.execute(_select(Task).where(Task.id == task_a_id))
        updated_a = a_result.scalars().first()
        b_result = await verify_session.execute(_select(Task).where(Task.id == task_b_id))
        updated_b = b_result.scalars().first()

    # Neither A nor B should be touched — D is still running
    assert updated_a is not None
    assert (updated_a.depends_on or []) == original_a_deps, (
        f"False positive: A's depends_on was incorrectly cleared. "
        f"A depends on B which depends on an IN_PROGRESS task D. "
        f"Got depends_on={updated_a.depends_on!r}"
    )
    assert updated_b is not None
    assert (updated_b.depends_on or []) == original_b_deps, (
        f"False positive: B's depends_on was incorrectly cleared. "
        f"B depends on an IN_PROGRESS task D. "
        f"Got depends_on={updated_b.depends_on!r}"
    )


# ══════════════════════════════════════════════════════════════════════════════
# Test 6c — True cycle: A→B→A clears both tasks
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_deadlock_true_cycle_cleared(orch_db, mocker):
    """True dependency cycles (A→B→A) must have depends_on cleared for all
    cycle participants so they can be dispatched.
    """
    session, _Session = orch_db

    project = Project(name="Cycle Test", user_id="user1")
    session.add(project)
    await session.commit()
    await session.refresh(project)

    # Create A and B with placeholder IDs first, then set mutual deps
    task_a = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "task A"}],
        status=TaskStatus.QUEUED,
        depends_on=[],  # filled in below
        created_at=_naive_utc_ago(minutes=12),
    )
    task_b = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "task B"}],
        status=TaskStatus.QUEUED,
        depends_on=[],  # filled in below
        created_at=_naive_utc_ago(minutes=12),
    )
    session.add(task_a)
    session.add(task_b)
    await session.commit()
    await session.refresh(task_a)
    await session.refresh(task_b)

    # Wire the cycle: A→B, B→A
    task_a.depends_on = [str(task_b.id)]
    task_b.depends_on = [str(task_a.id)]
    session.add(task_a)
    session.add(task_b)
    await session.commit()

    task_a_id = task_a.id
    task_b_id = task_b.id

    _orch_module._last_deadlock_check = 0.0

    await _run_one_cycle(mocker)

    async with _Session() as verify_session:
        from sqlmodel import select as _select
        a_result = await verify_session.execute(_select(Task).where(Task.id == task_a_id))
        updated_a = a_result.scalars().first()
        b_result = await verify_session.execute(_select(Task).where(Task.id == task_b_id))
        updated_b = b_result.scalars().first()

    assert updated_a is not None
    assert updated_a.depends_on == [] or updated_a.depends_on is None, (
        f"Expected A's depends_on to be cleared (true cycle A→B→A), "
        f"got {updated_a.depends_on!r}"
    )
    assert updated_b is not None
    assert updated_b.depends_on == [] or updated_b.depends_on is None, (
        f"Expected B's depends_on to be cleared (true cycle A→B→A), "
        f"got {updated_b.depends_on!r}"
    )


# ══════════════════════════════════════════════════════════════════════════════
# Test 7 — CI-waiting backstop timeout
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_ci_waiting_backstop_timeout_fails_stuck_task(eval_db, mocker):
    """A CI-waiting EVALUATING task with missing ci_wait_started should be failed
    by the 30-minute backstop in _check_evaluating_timeouts().

    Scenario: task has ci_waiting=True in context but no ci_wait_started field,
    and has been EVALUATING for >30 minutes.  The backstop must catch it.
    """
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch("app.main._ensure_vm_deleted", new_callable=AsyncMock)
    mocker.patch("app.main._maybe_create_failure_alert", new_callable=AsyncMock)

    project = Project(name="CI Backstop Test", user_id="user1")
    eval_db.add(project)
    await eval_db.commit()
    await eval_db.refresh(project)

    # CI-waiting task with NO ci_wait_started — exactly the corrupt/missing case
    task = Task(
        project_id=project.id,
        payloads=[{"type": "review", "prompt": "backstop test task"}],
        status=TaskStatus.EVALUATING,
        context={"ci_waiting": True},  # ci_wait_started deliberately absent
    )
    eval_db.add(task)
    await eval_db.commit()
    await eval_db.refresh(task)

    # Set task to appear 35 minutes into EVALUATING (> 30-min backstop)
    evaluating_since = _naive_utc_ago(minutes=35)
    task.status_log = [
        {"t": evaluating_since.isoformat(), "msg": "Status changed: IN_PROGRESS → EVALUATING"}
    ]
    eval_db.add(task)
    await eval_db.commit()
    await eval_db.refresh(task)

    await _check_evaluating_timeouts()

    await eval_db.refresh(task)
    assert task.status == TaskStatus.FAILED, (
        f"Expected CI-waiting task with missing ci_wait_started to be FAILED after "
        f"30-min backstop, got {task.status}"
    )
    assert "backstop" in (task.latest_status_message or "").lower(), (
        f"Expected 'backstop' in status message, got: {task.latest_status_message!r}"
    )
    assert "missing" in (task.latest_status_message or "").lower(), (
        f"Expected 'missing' in status message when ci_wait_started is absent, got: {task.latest_status_message!r}"
    )


@pytest.mark.asyncio
async def test_ci_waiting_under_backstop_threshold_not_failed(eval_db, mocker):
    """A CI-waiting task that is UNDER the 30-minute backstop threshold must NOT
    be failed by _check_evaluating_timeouts() — normal CI timeout handles it.
    """
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch("app.main._ensure_vm_deleted", new_callable=AsyncMock)
    mocker.patch("app.main._maybe_create_failure_alert", new_callable=AsyncMock)

    project = Project(name="CI Backstop Under Test", user_id="user1")
    eval_db.add(project)
    await eval_db.commit()
    await eval_db.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "review", "prompt": "under threshold task"}],
        status=TaskStatus.EVALUATING,
        context={"ci_waiting": True},  # ci_wait_started absent but under threshold
    )
    eval_db.add(task)
    await eval_db.commit()
    await eval_db.refresh(task)

    # Only 20 minutes into EVALUATING — well under the 30-min backstop
    evaluating_since = _naive_utc_ago(minutes=20)
    task.status_log = [
        {"t": evaluating_since.isoformat(), "msg": "Status changed: IN_PROGRESS → EVALUATING"}
    ]
    eval_db.add(task)
    await eval_db.commit()
    await eval_db.refresh(task)

    await _check_evaluating_timeouts()

    await eval_db.refresh(task)
    assert task.status == TaskStatus.EVALUATING, (
        f"Expected CI-waiting task under 30-min threshold to remain EVALUATING, "
        f"got {task.status}"
    )


@pytest.mark.asyncio
async def test_backstop_message_accurate_when_ci_wait_started_present(eval_db, mocker):
    """When ci_wait_started IS present and valid in review_loop, the backstop
    message must NOT say 'missing or corrupt' — it should diagnose the actual
    reason (e.g. 'CI did not complete within backstop window').
    """
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch("app.main._ensure_vm_deleted", new_callable=AsyncMock)
    mocker.patch("app.main._maybe_create_failure_alert", new_callable=AsyncMock)

    project = Project(name="Backstop Message Test", user_id="user1")
    eval_db.add(project)
    await eval_db.commit()
    await eval_db.refresh(project)

    # CI-waiting task with VALID ci_wait_started in review_loop
    ci_started = _naive_utc_ago(minutes=35).isoformat()
    task = Task(
        project_id=project.id,
        payloads=[{"type": "review", "prompt": "backstop message test"}],
        status=TaskStatus.EVALUATING,
        context={"review_loop": {
            "ci_waiting": True,
            "ci_wait_started": ci_started,
            "pr_number": 999,
        }},
    )
    eval_db.add(task)
    await eval_db.commit()
    await eval_db.refresh(task)

    # 35 minutes into EVALUATING (> 30-min backstop)
    evaluating_since = _naive_utc_ago(minutes=35)
    task.status_log = [
        {"t": evaluating_since.isoformat(), "msg": "Status changed: IN_PROGRESS → EVALUATING"}
    ]
    eval_db.add(task)
    await eval_db.commit()
    await eval_db.refresh(task)

    await _check_evaluating_timeouts()

    await eval_db.refresh(task)
    assert task.status == TaskStatus.FAILED, (
        f"Expected task to be FAILED after backstop, got {task.status}"
    )
    msg = (task.latest_status_message or "").lower()
    assert "backstop" in msg, f"Expected 'backstop' in message, got: {task.latest_status_message!r}"
    assert "missing" not in msg and "corrupt" not in msg, (
        f"Message should NOT say 'missing or corrupt' when ci_wait_started is valid, "
        f"got: {task.latest_status_message!r}"
    )


# ══════════════════════════════════════════════════════════════════════════════
# Tests — External health gate (get_confirmed_down_critical_deps)
# ══════════════════════════════════════════════════════════════════════════════

class TestExternalHealthGate:
    """Unit tests for the pre-flight external health gate in the dispatch path.

    The gate reads shared_state.external_health["consecutive_failures"] and
    blocks dispatch when a *critical* dependency (github_api, claude_api, gcp)
    has been confirmed down for 2+ consecutive health checks.
    """

    from app.orchestrator import get_confirmed_down_critical_deps

    def _set_consec(self, **kwargs):
        """Set consecutive_failures entries; reset all others to 0."""
        _shared_state.external_health["consecutive_failures"] = {
            "github_api": kwargs.get("github_api", 0),
            "claude_api": kwargs.get("claude_api", 0),
            "gcp": kwargs.get("gcp", 0),
            "db": kwargs.get("db", 0),
        }

    def setup_method(self):
        """Save and clear consecutive_failures before each test."""
        self._orig = dict(_shared_state.external_health.get("consecutive_failures", {}))
        _shared_state.external_health["consecutive_failures"] = {}

    def teardown_method(self):
        """Restore original consecutive_failures after each test."""
        _shared_state.external_health["consecutive_failures"] = self._orig

    def test_dispatch_skipped_when_github_api_confirmed_down(self):
        """Gate returns github_api when it has >=2 consecutive failures."""
        from app.orchestrator import get_confirmed_down_critical_deps
        self._set_consec(github_api=2)
        result = get_confirmed_down_critical_deps()
        assert "github_api" in result

    def test_dispatch_skipped_when_claude_api_confirmed_down(self):
        """Gate returns claude_api when it has >=2 consecutive failures."""
        from app.orchestrator import get_confirmed_down_critical_deps
        self._set_consec(claude_api=3)
        result = get_confirmed_down_critical_deps()
        assert "claude_api" in result

    def test_dispatch_skipped_when_gcp_confirmed_down(self):
        """Gate returns gcp when it has >=2 consecutive failures."""
        from app.orchestrator import get_confirmed_down_critical_deps
        self._set_consec(gcp=5)
        result = get_confirmed_down_critical_deps()
        assert "gcp" in result

    def test_single_failure_does_not_gate(self):
        """One failure is transient — gate does NOT block dispatch."""
        from app.orchestrator import get_confirmed_down_critical_deps
        self._set_consec(github_api=1)
        result = get_confirmed_down_critical_deps()
        assert result == []

    def test_dispatch_proceeds_when_only_db_is_down(self):
        """DB is not a critical dep for the gate — dispatch still proceeds."""
        from app.orchestrator import get_confirmed_down_critical_deps
        self._set_consec(db=10)
        result = get_confirmed_down_critical_deps()
        assert result == []

    def test_dispatch_proceeds_when_all_deps_healthy(self):
        """All zeros — nothing is blocked."""
        from app.orchestrator import get_confirmed_down_critical_deps
        self._set_consec()
        result = get_confirmed_down_critical_deps()
        assert result == []

    def test_dispatch_resumes_after_recovery(self):
        """Consecutive_failures reset to 0 after recovery — gate clears."""
        from app.orchestrator import get_confirmed_down_critical_deps
        self._set_consec(github_api=3)
        assert get_confirmed_down_critical_deps() == ["github_api"]

        # Recovery: reset to 0
        self._set_consec(github_api=0)
        assert get_confirmed_down_critical_deps() == []

    def test_multiple_deps_down_all_returned(self):
        """If multiple critical deps are confirmed down, all are returned."""
        from app.orchestrator import get_confirmed_down_critical_deps
        self._set_consec(github_api=2, claude_api=2)
        result = get_confirmed_down_critical_deps()
        assert "github_api" in result
        assert "claude_api" in result
        assert "gcp" not in result
        assert "db" not in result
