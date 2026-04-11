"""
Integration tests for Blueprint orchestrator background services.

Tests three areas:
1. reaper_service() — stale heartbeat detection, retry logic, skip-locked protection
2. Max Plan account pool — load/reconcile, account selection, disabled-account filtering
3. Research task spawning — deduplication guard and depends_on_indices ordering
"""

import pytest
import json
import uuid
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import app.main as _main_module
import app.shared_state as _shared_state
from app.main import (
    reaper_service,
    _spawn_research_task,
    _handle_research_completion,
    classify_failure,
)
from app.max_plan_pool import (
    load_max_plan_pool,
    select_max_plan_account,
    release_max_plan_account,
    _max_plan_pools,
    _account_by_id,
)
from app.models import Project, Task, TaskStatus, MaxPlanAccount
from testing.factories import make_project, make_task

_TEST_USER = "test-user-123"


# ── Helper: redirect AsyncSessionLocal to the test database ─────────────────

class _FakeSessionFactory:
    """Replaces AsyncSessionLocal so background services use the test session."""

    def __init__(self, session):
        self._session = session

    def __call__(self):
        return self

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return False


# ── Fixture: isolate _max_plan_pool between tests ────────────────────────────

@pytest.fixture(autouse=False)
def reset_max_plan_pool():
    """Save and restore the in-memory Max Plan pool around each test."""
    orig_pools = {k: list(v) for k, v in _main_module._max_plan_pools.items()}
    orig_by_id = dict(_main_module._account_by_id)
    _main_module._max_plan_pools.clear()
    _main_module._account_by_id.clear()
    yield
    _main_module._max_plan_pools.clear()
    _main_module._account_by_id.clear()
    _main_module._max_plan_pools.update(orig_pools)
    _main_module._account_by_id.update(orig_by_id)


# ── Fixture: reset circuit breaker state for a project ───────────────────────

@pytest.fixture(autouse=False)
def reset_circuit_breakers():
    original = dict(_shared_state.circuit_breakers)
    yield
    _shared_state.circuit_breakers.clear()
    _shared_state.circuit_breakers.update(original)


# ══════════════════════════════════════════════════════════════════════════════
# Section 1 — reaper_service()
# ══════════════════════════════════════════════════════════════════════════════

class TestReaperService:
    """Tests for reaper_service() background worker."""

    # Helper shared by all reaper tests
    @staticmethod
    def _stop_after_one_cycle(mocker):
        class _StopLoop(Exception):
            pass
        mocker.patch("asyncio.sleep", side_effect=_StopLoop)
        return _StopLoop

    # ── Test 1: stale heartbeat, retries exhausted → FAILED ─────────────────

    @pytest.mark.asyncio
    async def test_stale_heartbeat_exhausted_retries_marks_task_failed(self, db_session, mocker):
        """
        A task whose last heartbeat is >5 min old AND has reached max_retries
        must be marked FAILED and its VM deleted.
        """
        mock_delete = mocker.patch("app.main.delete_agent_vm", return_value=None)
        mocker.patch("app.main.broadcast_to_project", new_callable=AsyncMock)
        mocker.patch("app.reaper.AsyncSessionLocal", _FakeSessionFactory(db_session))
        StopLoop = self._stop_after_one_cycle(mocker)

        project = make_project(name="Reaper FAILED test", user_id="user1")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        max_retries = _main_module._runtime_config["max_retries"]
        task = make_task(
            project_id=project.id,
            vm_instance_id="stale-vm-001",
            status=TaskStatus.IN_PROGRESS,
            # Heartbeat older than the 5-minute window
            last_heartbeat=datetime.utcnow() - timedelta(minutes=6),
            attempt_count=max_retries,  # exhausted — next reap should FAIL, not retry
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        try:
            await reaper_service()
        except StopLoop:
            pass

        await db_session.refresh(task)
        assert task.status == TaskStatus.FAILED
        mock_delete.assert_called_once_with("stale-vm-001")

    # ── Test 2: recent heartbeat → NOT reaped ───────────────────────────────

    @pytest.mark.asyncio
    async def test_recent_heartbeat_task_is_not_reaped(self, db_session, mocker):
        """
        A task whose last heartbeat is within the 5-minute window must
        be left alone by the reaper.
        """
        mock_delete = mocker.patch("app.main.delete_agent_vm", return_value=None)
        mocker.patch("app.main.broadcast_to_project", new_callable=AsyncMock)
        mocker.patch("app.reaper.AsyncSessionLocal", _FakeSessionFactory(db_session))
        StopLoop = self._stop_after_one_cycle(mocker)

        project = make_project(name="Heartbeat alive", user_id="user1")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        task = make_task(
            project_id=project.id,
            vm_instance_id="healthy-vm-001",
            status=TaskStatus.IN_PROGRESS,
            last_heartbeat=datetime.utcnow() - timedelta(minutes=2),  # within window
            attempt_count=0,
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        try:
            await reaper_service()
        except StopLoop:
            pass

        await db_session.refresh(task)
        assert task.status == TaskStatus.IN_PROGRESS, "Healthy task should not be reaped"
        mock_delete.assert_not_called()

    # ── Test 3: stale heartbeat, retries remaining → requeued ───────────────

    @pytest.mark.asyncio
    async def test_stale_task_under_retry_limit_is_requeued(self, db_session, mocker):
        """
        A task with a stale heartbeat that still has retry attempts remaining
        must be requeued (status=QUEUED) with incremented attempt_count,
        NOT marked FAILED.
        """
        mocker.patch("app.main.delete_agent_vm", return_value=None)
        mocker.patch("app.main.broadcast_to_project", new_callable=AsyncMock)
        mocker.patch("app.reaper.AsyncSessionLocal", _FakeSessionFactory(db_session))
        StopLoop = self._stop_after_one_cycle(mocker)

        project = make_project(name="Retry test", user_id="user1")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        task = make_task(
            project_id=project.id,
            vm_instance_id="retry-vm-001",
            status=TaskStatus.IN_PROGRESS,
            last_heartbeat=datetime.utcnow() - timedelta(minutes=7),
            attempt_count=0,  # first attempt — should be retried
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        try:
            await reaper_service()
        except StopLoop:
            pass

        await db_session.refresh(task)
        assert task.status == TaskStatus.QUEUED, "Task with remaining retries should be requeued"
        assert task.attempt_count == 1, "Retry counter must be incremented"

    # ── Test 4: task without vm_instance_id → skipped ───────────────────────

    @pytest.mark.asyncio
    async def test_task_without_vm_id_is_skipped(self, db_session, mocker):
        """
        Logical/parent tasks that have no VM assigned (vm_instance_id=None)
        must be skipped — the reaper only acts on tasks with real VMs.
        """
        mock_delete = mocker.patch("app.main.delete_agent_vm", return_value=None)
        mocker.patch("app.main.broadcast_to_project", new_callable=AsyncMock)
        mocker.patch("app.reaper.AsyncSessionLocal", _FakeSessionFactory(db_session))
        StopLoop = self._stop_after_one_cycle(mocker)

        project = make_project(name="No-VM test", user_id="user1")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        max_retries = _main_module._runtime_config["max_retries"]
        task = make_task(
            project_id=project.id,
            vm_instance_id=None,          # no VM — logical parent task
            status=TaskStatus.IN_PROGRESS,
            last_heartbeat=datetime.utcnow() - timedelta(hours=2),
            attempt_count=max_retries,
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        try:
            await reaper_service()
        except StopLoop:
            pass

        await db_session.refresh(task)
        assert task.status == TaskStatus.IN_PROGRESS, "Task without VM should be untouched"
        mock_delete.assert_not_called()

    # ── Test 5: FOR UPDATE SKIP LOCKED / pre-validation guard ───────────────

    @pytest.mark.asyncio
    async def test_skip_locked_pre_validation_prevents_double_reap(self, db_session, mocker):
        """
        The reaper re-validates each task's status via session.get() just before acting.
        If the task's status has already transitioned out of PROVISIONING/IN_PROGRESS
        (e.g. reaped by a concurrent instance), the reaper skips it.

        This is the application-level complement to FOR UPDATE SKIP LOCKED:
        even if two reaper instances select the same task, the second one
        detects the status change during re-validation and does nothing.
        """
        mock_delete = mocker.patch("app.main.delete_agent_vm", return_value=None)
        mocker.patch("app.main.broadcast_to_project", new_callable=AsyncMock)
        StopLoop = self._stop_after_one_cycle(mocker)

        project = make_project(name="Skip-locked test", user_id="user1")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        max_retries = _main_module._runtime_config["max_retries"]
        task = make_task(
            project_id=project.id,
            vm_instance_id="race-vm-001",
            status=TaskStatus.IN_PROGRESS,
            last_heartbeat=datetime.utcnow() - timedelta(minutes=6),
            attempt_count=max_retries,
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        # Intercept session.get so the "fresh" re-validation returns a COMPLETED task,
        # simulating a concurrent reaper instance having already processed it.
        original_get = db_session.get

        async def _intercepted_get(model, pk, **kwargs):
            if model is Task and pk == task.id:
                # Construct a stub that looks already-completed
                stub = Task(
                    project_id=task.project_id,
                    payloads=[],
                    vm_instance_id="race-vm-001",
                    status=TaskStatus.COMPLETED,   # already done by another instance
                    attempt_count=max_retries,
                )
                stub.id = task.id
                return stub
            return await original_get(model, pk, **kwargs)

        db_session.get = _intercepted_get
        mocker.patch("app.reaper.AsyncSessionLocal", _FakeSessionFactory(db_session))

        try:
            await reaper_service()
        except StopLoop:
            pass

        # Restore db_session.get for the refresh below
        db_session.get = original_get

        # The pre-validation guard must have skipped this task
        mock_delete.assert_not_called()

        # The task's real DB state is still IN_PROGRESS (the reaper never committed a change)
        await db_session.refresh(task)
        assert task.status == TaskStatus.IN_PROGRESS

    # ── Test 6: FAILED tasks excluded from the initial scan ─────────────────

    @pytest.mark.asyncio
    async def test_already_failed_task_is_excluded_from_reaper_scan(self, db_session, mocker):
        """
        The reaper's initial query only selects PROVISIONING/IN_PROGRESS tasks.
        A task that is already FAILED must never be picked up — this is the
        database-level guarantee that prevents double-reaping after the first pass.
        """
        mock_delete = mocker.patch("app.main.delete_agent_vm", return_value=None)
        mocker.patch("app.main.broadcast_to_project", new_callable=AsyncMock)
        mocker.patch("app.reaper.AsyncSessionLocal", _FakeSessionFactory(db_session))
        StopLoop = self._stop_after_one_cycle(mocker)

        project = make_project(name="Already-failed test", user_id="user1")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        task = make_task(
            project_id=project.id,
            vm_instance_id="already-failed-vm",
            status=TaskStatus.FAILED,          # terminal state
            last_heartbeat=datetime.utcnow() - timedelta(hours=2),
            attempt_count=3,
        )
        db_session.add(task)
        await db_session.commit()

        try:
            await reaper_service()
        except StopLoop:
            pass

        mock_delete.assert_not_called()
        await db_session.refresh(task)
        assert task.status == TaskStatus.FAILED  # unchanged


# ══════════════════════════════════════════════════════════════════════════════
# Section 2 — Max Plan Account Pool
# ══════════════════════════════════════════════════════════════════════════════

class TestMaxPlanPool:
    """Tests for load_max_plan_pool(), select_max_plan_account(), and release_max_plan_account()."""

    # ── Test 1: load_max_plan_pool reconciles active_tasks from DB ───────────

    @pytest.mark.asyncio
    async def test_load_max_plan_pool_reconciles_active_tasks_after_restart(
        self, db_session, mocker, reset_max_plan_pool
    ):
        """
        On startup/restart, load_max_plan_pool() counts the actual PROVISIONING/IN_PROGRESS
        tasks per account in the DB — not trusting any stale in-memory counters.
        """
        mocker.patch("app.max_plan_pool.AsyncSessionLocal", _FakeSessionFactory(db_session))

        acct_id = uuid.uuid4()
        account = MaxPlanAccount(
            id=acct_id,
            user_id=_TEST_USER,
            name="test-account",
            oauth_token="sk-ant-test-token",
            enabled=True,
            total_tasks=10,
        )
        db_session.add(account)
        await db_session.commit()

        project = make_project(name="Pool reconcile test", user_id="user1")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        # Two tasks referencing this account are IN_PROGRESS
        for _ in range(2):
            t = make_task(
                project_id=project.id,
                vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
                status=TaskStatus.IN_PROGRESS,
                context={"max_plan_account_id": str(acct_id)},
            )
            db_session.add(t)
        # One task is COMPLETED — must NOT be counted
        completed = make_task(
            project_id=project.id,
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
            status=TaskStatus.COMPLETED,
            context={"max_plan_account_id": str(acct_id)},
        )
        db_session.add(completed)
        await db_session.commit()

        await load_max_plan_pool()

        assert len(_main_module._account_by_id) == 1
        entry = _main_module._account_by_id[str(acct_id)]
        assert entry["id"] == str(acct_id)
        assert entry["active_tasks"] == 2, "Only IN_PROGRESS tasks should be counted"

    # ── Test 2: load excludes disabled accounts from active count but includes them ─

    @pytest.mark.asyncio
    async def test_load_max_plan_pool_includes_disabled_accounts(self, db_session, mocker, reset_max_plan_pool):
        """load_max_plan_pool() loads all accounts regardless of enabled flag."""
        mocker.patch("app.max_plan_pool.AsyncSessionLocal", _FakeSessionFactory(db_session))

        db_session.add(MaxPlanAccount(user_id=_TEST_USER, name="enabled-acct", oauth_token="tok-a", enabled=True, total_tasks=0))
        db_session.add(MaxPlanAccount(user_id=_TEST_USER, name="disabled-acct", oauth_token="tok-b", enabled=False, total_tasks=0))
        await db_session.commit()

        await load_max_plan_pool()

        assert len(_main_module._account_by_id) == 2
        names = {a["name"] for a in _main_module._account_by_id.values()}
        assert names == {"enabled-acct", "disabled-acct"}

    # ── Test 3: select_max_plan_account returns lowest active count ──────────

    @pytest.mark.asyncio
    async def test_select_max_plan_account_returns_account_with_lowest_active_count(self, reset_max_plan_pool):
        """select_max_plan_account() must pick the enabled account with the fewest active tasks."""
        for entry in [
            {"id": "aaa", "name": "busy",   "oauth_token": "t1", "enabled": True, "active_tasks": 5, "user_id": _TEST_USER},
            {"id": "bbb", "name": "idle",   "oauth_token": "t2", "enabled": True, "active_tasks": 1, "user_id": _TEST_USER},
            {"id": "ccc", "name": "medium", "oauth_token": "t3", "enabled": True, "active_tasks": 3, "user_id": _TEST_USER},
        ]:
            _max_plan_pools.setdefault(_TEST_USER, []).append(entry)
            _account_by_id[entry["id"]] = entry

        chosen = await select_max_plan_account(_TEST_USER)
        assert chosen is not None
        assert chosen["name"] == "idle"

    # ── Test 4: disabled accounts are skipped ────────────────────────────────

    @pytest.mark.asyncio
    async def test_select_max_plan_account_skips_disabled_accounts(self, reset_max_plan_pool):
        """Disabled accounts (enabled=False) must never be selected."""
        for entry in [
            {"id": "aaa", "name": "disabled-low", "oauth_token": "t1", "enabled": False, "active_tasks": 0, "user_id": _TEST_USER},
            {"id": "bbb", "name": "enabled-high", "oauth_token": "t2", "enabled": True,  "active_tasks": 9, "user_id": _TEST_USER},
        ]:
            _max_plan_pools.setdefault(_TEST_USER, []).append(entry)
            _account_by_id[entry["id"]] = entry

        chosen = await select_max_plan_account(_TEST_USER)
        assert chosen is not None
        assert chosen["name"] == "enabled-high", "Must pick enabled account even if it has more tasks"

    # ── Test 5: all accounts disabled → None ────────────────────────────────

    @pytest.mark.asyncio
    async def test_select_max_plan_account_returns_none_when_all_disabled(self, reset_max_plan_pool):
        """When no enabled accounts exist, select_max_plan_account() returns None."""
        for entry in [
            {"id": "aaa", "name": "acct-a", "oauth_token": "t1", "enabled": False, "active_tasks": 0, "user_id": _TEST_USER},
            {"id": "bbb", "name": "acct-b", "oauth_token": "t2", "enabled": False, "active_tasks": 0, "user_id": _TEST_USER},
        ]:
            _max_plan_pools.setdefault(_TEST_USER, []).append(entry)
            _account_by_id[entry["id"]] = entry
        assert await select_max_plan_account(_TEST_USER) is None

    # ── Test 6: empty pool → None ────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_select_max_plan_account_returns_none_for_empty_pool(self, reset_max_plan_pool):
        """Empty pool returns None without raising."""
        assert len(_main_module._account_by_id) == 0
        assert await select_max_plan_account(_TEST_USER) is None

    # ── Test 7: release decrements active_tasks ──────────────────────────────

    @pytest.mark.asyncio
    async def test_release_max_plan_account_decrements_active_tasks(self, reset_max_plan_pool):
        """release_max_plan_account() must decrement active_tasks for the given account."""
        acct_id = "test-release-id"
        entry = {"id": acct_id, "name": "acct", "oauth_token": "tok", "enabled": True, "active_tasks": 3, "user_id": _TEST_USER}
        _max_plan_pools.setdefault(_TEST_USER, []).append(entry)
        _account_by_id[acct_id] = entry

        await release_max_plan_account(acct_id)

        assert _account_by_id[acct_id]["active_tasks"] == 2

    # ── Test 8: release never goes below zero ────────────────────────────────

    @pytest.mark.asyncio
    async def test_release_max_plan_account_floors_at_zero(self, reset_max_plan_pool):
        """active_tasks should not go negative — floor at 0 on release."""
        acct_id = "floor-test-id"
        entry = {"id": acct_id, "name": "acct", "oauth_token": "tok", "enabled": True, "active_tasks": 0, "user_id": _TEST_USER}
        _max_plan_pools.setdefault(_TEST_USER, []).append(entry)
        _account_by_id[acct_id] = entry
        await release_max_plan_account(acct_id)
        assert _account_by_id[acct_id]["active_tasks"] == 0

    # ── Test 9: concurrency limit — select respects in-flight task count ──────

    @pytest.mark.asyncio
    async def test_select_max_plan_account_respects_concurrency_by_returning_least_loaded(self, reset_max_plan_pool):
        """
        When all accounts are busy, select_max_plan_account() still returns the
        least-loaded one (the caller is responsible for enforcing a hard limit).
        Verifies that active_tasks correctly gate account selection ordering.
        """
        for entry in [
            {"id": "x1", "name": "a1", "oauth_token": "t1", "enabled": True, "active_tasks": 10, "user_id": _TEST_USER},
            {"id": "x2", "name": "a2", "oauth_token": "t2", "enabled": True, "active_tasks": 7, "user_id": _TEST_USER},
            {"id": "x3", "name": "a3", "oauth_token": "t3", "enabled": True, "active_tasks": 10, "user_id": _TEST_USER},
        ]:
            _max_plan_pools.setdefault(_TEST_USER, []).append(entry)
            _account_by_id[entry["id"]] = entry
        chosen = await select_max_plan_account(_TEST_USER)
        assert chosen["id"] == "x2"


# ══════════════════════════════════════════════════════════════════════════════
# Section 3 — Research Task Spawning & Deduplication
# ══════════════════════════════════════════════════════════════════════════════

class TestResearchSpawning:
    """Tests for _spawn_research_task() guard conditions."""

    @pytest.fixture(autouse=True)
    def _ensure_test_accounts(self):
        """Ensure _spawn_research_task finds available accounts for test users."""
        for uid in ("user1", _TEST_USER):
            entry = {"id": f"acct-{uid}", "user_id": uid, "name": "test", "enabled": True,
                     "active_tasks": 0, "oauth_token": "sk-ant-test", "total_tasks": 0,
                     "token_created_at": None, "claude_config_json": None}
            _max_plan_pools.setdefault(uid, []).append(entry)
            _account_by_id[f"acct-{uid}"] = entry
        yield
        for uid in ("user1", _TEST_USER):
            _max_plan_pools.get(uid, [])[:] = [a for a in _max_plan_pools.get(uid, []) if a["id"] != f"acct-{uid}"]
            _account_by_id.pop(f"acct-{uid}", None)

    # ── Test 1: autonomous disabled → no task spawned ───────────────────────

    @pytest.mark.asyncio
    async def test_spawn_skipped_when_autonomous_disabled(self, db_session, reset_circuit_breakers):
        """_spawn_research_task() must be a no-op when autonomous_enabled=False."""
        project = Project(
            name="Disabled autonomous",
            user_id="user1",
            autonomous_enabled=False,
            directions=[{"id": "d1", "status": "active", "text": "improve coverage"}],
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        await _spawn_research_task(project, db_session)

        from sqlmodel import select as sql_select
        tasks = (await db_session.execute(sql_select(Task).where(Task.project_id == project.id))).scalars().all()
        assert len(tasks) == 0, "No task should be spawned when autonomous_enabled=False"

    # ── Test 2: circuit breaker OPEN → no task spawned ──────────────────────

    @pytest.mark.asyncio
    async def test_spawn_skipped_when_circuit_breaker_open(self, db_session, reset_circuit_breakers):
        """_spawn_research_task() must respect an OPEN circuit breaker in its cooldown period."""
        project = Project(
            name="CB open test",
            user_id="user1",
            autonomous_enabled=True,
            directions=[{"id": "d1", "status": "active", "text": "improve coverage"}],
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        # Trip the circuit breaker with a recent open time (still in cooldown)
        breaker = _shared_state.get_circuit_breaker(str(project.id))
        breaker["state"] = "OPEN"
        breaker["opened_at"] = datetime.utcnow() - timedelta(minutes=5)  # only 5 min; cooldown is 30 min
        breaker["cooldown_seconds"] = 1800

        await _spawn_research_task(project, db_session)

        from sqlmodel import select as sql_select
        tasks = (await db_session.execute(sql_select(Task).where(Task.project_id == project.id))).scalars().all()
        assert len(tasks) == 0, "No task should be spawned while circuit breaker is OPEN"

    # ── Test 2b: HALF_OPEN stuck test task → reverts to OPEN ────

    @pytest.mark.asyncio
    async def test_half_open_stuck_task_reverts_to_open(self, db_session, reset_circuit_breakers):
        """If the HALF_OPEN test task is stuck for >30 min, breaker reverts to OPEN with doubled cooldown."""
        project = Project(
            name="CB half-open test",
            user_id="user1",
            autonomous_enabled=True,
            directions=[{"id": "d1", "status": "active", "text": "improve coverage"}],
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        # Create a "stuck" test task that started 45 minutes ago
        stuck_task = Task(
            project_id=project.id,
            title="Research: half-open test task",
            payloads=[{"type": "research", "base_branch": "main"}],
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
            status=TaskStatus.IN_PROGRESS,
            created_by="ORCHESTRATOR",
            started_at=datetime.utcnow() - timedelta(minutes=45),
        )
        db_session.add(stuck_task)
        await db_session.commit()
        await db_session.refresh(stuck_task)

        # Set breaker to HALF_OPEN with the stuck task as the test task
        breaker = _shared_state.get_circuit_breaker(str(project.id))
        breaker["state"] = "HALF_OPEN"
        breaker["half_open_task_id"] = str(stuck_task.id)
        breaker["cooldown_seconds"] = 1800  # 30 min base

        await _spawn_research_task(project, db_session)

        # Breaker should have reverted to OPEN with doubled cooldown
        assert breaker["state"] == "OPEN", "Stuck HALF_OPEN should revert to OPEN"
        assert breaker["cooldown_seconds"] == 3600, "Cooldown should double from 1800 to 3600"
        assert breaker["half_open_task_id"] is None, "Test task ID should be cleared"

        # No new task should have been spawned (breaker is now OPEN)
        from sqlmodel import select as sql_select
        all_tasks = (await db_session.execute(sql_select(Task).where(Task.project_id == project.id))).scalars().all()
        new_queued = [t for t in all_tasks if t.status == TaskStatus.QUEUED]
        assert len(new_queued) == 0, "No new task should be spawned when breaker reverts to OPEN"

    # ── Test 3: existing research task in progress → no duplicate spawned ────

    @pytest.mark.asyncio
    async def test_spawn_skipped_when_research_task_already_active(self, db_session, reset_circuit_breakers):
        """
        _spawn_research_task() must not spawn if a research-type task is
        already QUEUED/PROVISIONING/IN_PROGRESS for the same project.
        """
        project = Project(
            name="Dedup spawn test",
            user_id="user1",
            autonomous_enabled=True,
            directions=[{"id": "d1", "status": "active", "text": "security scan"}],
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        # Existing research task already running
        existing = Task(
            project_id=project.id,
            title="Research: existing scan",
            payloads=[{"type": "research", "base_branch": "main"}],
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
            status=TaskStatus.IN_PROGRESS,
            created_by="ORCHESTRATOR",
        )
        db_session.add(existing)
        await db_session.commit()

        await _spawn_research_task(project, db_session)

        from sqlmodel import select as sql_select
        all_tasks = (await db_session.execute(sql_select(Task).where(Task.project_id == project.id))).scalars().all()
        research_tasks = [t for t in all_tasks if (t.payloads or [{}])[0].get("type") == "research"]
        assert len(research_tasks) == 1, "A second research task must not be spawned"

    # ── Test 4: rate limit — too many orchestrator tasks this hour ───────────

    @pytest.mark.asyncio
    async def test_spawn_skipped_when_rate_limit_reached(self, db_session, reset_circuit_breakers):
        """_spawn_research_task() must stop spawning after max_autonomous_tasks_per_hour."""
        max_per_hour = _main_module._runtime_config["max_autonomous_tasks_per_hour"]

        project = Project(
            name="Rate limit test",
            user_id="user1",
            autonomous_enabled=True,
            directions=[{"id": "d1", "status": "active", "text": "performance review"}],
            vision={"purpose": "test"},
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        # Fill up the hourly quota with recent orchestrator research tasks
        for i in range(max_per_hour):
            t = Task(
                project_id=project.id,
                title=f"Research: past scan {i}",
                payloads=[{"type": "research", "base_branch": "main"}],
                vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
                status=TaskStatus.COMPLETED,
                created_by="ORCHESTRATOR",
                created_at=datetime.utcnow() - timedelta(minutes=10),
            )
            db_session.add(t)
        await db_session.commit()

        await _spawn_research_task(project, db_session)

        from sqlmodel import select as sql_select
        # Only the COMPLETED tasks we inserted should exist — no new QUEUED one
        queued = (await db_session.execute(
            sql_select(Task).where(Task.project_id == project.id, Task.status == TaskStatus.QUEUED)
        )).scalars().all()
        assert len(queued) == 0, "Rate limit must prevent additional spawns"


# ══════════════════════════════════════════════════════════════════════════════
# Section 4 — Research Completion: Deduplication & Dependency Ordering
# ══════════════════════════════════════════════════════════════════════════════

class TestResearchCompletion:
    """Tests for _handle_research_completion() deduplication and ordering."""

    @staticmethod
    def _findings_json(*findings):
        """Serialize a list of finding dicts to a log string for _handle_research_completion."""
        return json.dumps(findings)

    # ── Test 5: duplicate findings are skipped ───────────────────────────────

    @pytest.mark.asyncio
    async def test_duplicate_findings_are_skipped(self, db_session, mocker):
        """
        Findings whose titles share 3+ meaningful words with an existing task title
        must be deduplicated — no new child task should be created for them.
        """
        mocker.patch("app.main.broadcast_to_project", new_callable=AsyncMock)
        mocker.patch("app.task_completion.get_github_token_for_task", new_callable=AsyncMock, return_value=("fake-token", "owner", "repo"))
        mocker.patch("app.task_completion.get_branch_sha", new_callable=AsyncMock, return_value="abc123")
        mocker.patch("app.task_completion.create_branch", new_callable=AsyncMock, return_value=None)

        project = Project(name="Dedup findings test", user_id="user1")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        # Existing active task whose title will match finding #1
        existing_task = Task(
            project_id=project.id,
            title="optimize database query performance",
            payloads=[{"type": "create"}],
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
            status=TaskStatus.IN_PROGRESS,
            created_by="ORCHESTRATOR",
        )
        db_session.add(existing_task)

        # Research task that will produce findings
        research_task = Task(
            project_id=project.id,
            title="Research: autonomous scan",
            payloads=[{"type": "research", "base_branch": "main"}],
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
            status=TaskStatus.IN_PROGRESS,
            created_by="ORCHESTRATOR",
        )
        db_session.add(research_task)
        await db_session.commit()
        await db_session.refresh(research_task)

        # Finding #1 duplicates the existing task title
        # Finding #2 is genuinely new
        logs = self._findings_json(
            {
                "title": "optimize database query performance",  # duplicate — 4 matching words
                "prompt": "Improve the slow SQL queries in the reports module.",
                "priority": "medium",
            },
            {
                "title": "add request tracing middleware",  # genuinely new
                "prompt": "Instrument HTTP handlers with OpenTelemetry trace IDs.",
                "priority": "medium",
            },
        )

        await _handle_research_completion(research_task, logs, db_session)

        from sqlmodel import select as sql_select
        children = (await db_session.execute(
            sql_select(Task).where(
                Task.parent_task_id == research_task.id,
                Task.status == TaskStatus.QUEUED,
            )
        )).scalars().all()

        assert len(children) == 1, "Only the non-duplicate finding should produce a child task"
        assert "tracing" in children[0].title.lower() or "middleware" in children[0].title.lower()

    # ── Test 6: depends_on_indices creates sequential task ordering ──────────

    @pytest.mark.asyncio
    async def test_depends_on_indices_creates_sequential_task_ordering(self, db_session, mocker):
        """
        When findings carry explicit depends_on_indices, the resulting child tasks
        must have their depends_on field set to the UUIDs of the referenced tasks.
        """
        mocker.patch("app.main.broadcast_to_project", new_callable=AsyncMock)
        mocker.patch("app.task_completion.get_github_token_for_task", new_callable=AsyncMock, return_value=("fake-token", "owner", "repo"))
        mocker.patch("app.task_completion.get_branch_sha", new_callable=AsyncMock, return_value="abc123")
        mocker.patch("app.task_completion.create_branch", new_callable=AsyncMock, return_value=None)

        project = Project(name="Deps ordering test", user_id="user1")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        research_task = Task(
            project_id=project.id,
            title="Research: autonomous scan",
            payloads=[{"type": "research", "base_branch": "main"}],
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
            status=TaskStatus.IN_PROGRESS,
            created_by="ORCHESTRATOR",
        )
        db_session.add(research_task)
        await db_session.commit()
        await db_session.refresh(research_task)

        # Finding[0]: no dependencies
        # Finding[1]: depends on finding[0] via depends_on_indices
        logs = self._findings_json(
            {
                "title": "scaffold authentication module",
                "prompt": "Create the base authentication service with JWT support.",
                "priority": "high",
            },
            {
                "title": "add login endpoint to authentication",
                "prompt": "Implement POST /auth/login using the scaffolded auth module.",
                "priority": "high",
                "depends_on_indices": [0],  # must run after finding[0]
            },
        )

        await _handle_research_completion(research_task, logs, db_session)

        from sqlmodel import select as sql_select
        children = (await db_session.execute(
            sql_select(Task).where(Task.parent_task_id == research_task.id)
            .order_by(Task.created_at)
        )).scalars().all()

        assert len(children) == 2, "Both findings should produce child tasks"

        # Identify tasks by title keyword
        task_a = next(t for t in children if "scaffold" in t.title.lower())
        task_b = next(t for t in children if "login" in t.title.lower())

        # task_b must declare task_a as a dependency
        assert task_b.depends_on is not None
        assert str(task_a.id) in task_b.depends_on, (
            "depends_on_indices=[0] must translate to the UUID of the first child task"
        )
        # task_a must have no dependencies
        assert not task_a.depends_on

    # ── Test 7: wave-based ordering — low-priority tasks depend on high-priority ─

    @pytest.mark.asyncio
    async def test_priority_ordering_no_artificial_wave_dependencies(self, db_session, mocker):
        """
        Findings get priority values (high=30, medium=50, low=70) but NO
        inter-wave dependencies. The orchestrator's priority-based dispatch
        naturally runs high-priority tasks first without blocking low-priority
        ones behind artificial dependency chains.
        """
        mocker.patch("app.main.broadcast_to_project", new_callable=AsyncMock)
        mocker.patch("app.task_completion.get_github_token_for_task", new_callable=AsyncMock, return_value=("fake-token", "owner", "repo"))
        mocker.patch("app.task_completion.get_branch_sha", new_callable=AsyncMock, return_value="abc123")
        mocker.patch("app.task_completion.create_branch", new_callable=AsyncMock, return_value=None)

        project = Project(name="Priority ordering test", user_id="user1")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        research_task = Task(
            project_id=project.id,
            title="Research: priority test",
            payloads=[{"type": "research", "base_branch": "main"}],
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
            status=TaskStatus.IN_PROGRESS,
            created_by="ORCHESTRATOR",
        )
        db_session.add(research_task)
        await db_session.commit()
        await db_session.refresh(research_task)

        logs = self._findings_json(
            {
                "title": "critical security patch",
                "prompt": "Apply the CVE-2024-1234 fix immediately.",
                "priority": "high",
            },
            {
                "title": "improve documentation coverage",
                "prompt": "Expand JSDoc comments across all public APIs.",
                "priority": "low",
            },
        )

        await _handle_research_completion(research_task, logs, db_session)

        from sqlmodel import select as sql_select
        children = (await db_session.execute(
            sql_select(Task).where(Task.parent_task_id == research_task.id)
        )).scalars().all()

        assert len(children) == 2

        task_high = next(t for t in children if "security" in t.title.lower() or "critical" in t.title.lower())
        task_low = next(t for t in children if "documentation" in t.title.lower() or "improve" in t.title.lower())

        # Both tasks are independent — no artificial wave dependencies
        assert not task_high.depends_on, "High-priority task should have no dependencies"
        assert not task_low.depends_on, "Low-priority task should have no wave dependencies"

        # Priority values reflect finding priority (high=100, low=0)
        assert task_high.priority > task_low.priority, (
            f"High-priority task ({task_high.priority}) should have higher numeric priority than low ({task_low.priority})"
        )


# ── Tests: classify_failure ──────────────────────────────────────────────────

class TestClassifyFailure:
    """Unit tests for classify_failure() — verifies exit-code and process-killed patterns."""

    def _make_task(self, status_message: str) -> Task:
        task = Task(
            id=uuid.uuid4(),
            title="test task",
            prompt="test",
            latest_status_message=status_message,
            status=TaskStatus.FAILED,
        )
        return task

    # ── exit code patterns ───────────────────────────────────────────────────

    def test_exit_code_137_is_agent_timeout(self):
        task = self._make_task("Agent failed with exit code 137")
        assert classify_failure(task) == "AGENT_TIMEOUT"

    def test_exit_code_143_is_agent_timeout(self):
        task = self._make_task("Process terminated with exit code 143")
        assert classify_failure(task) == "AGENT_TIMEOUT"

    def test_container_exited_is_agent_timeout(self):
        task = self._make_task("container exited unexpectedly")
        assert classify_failure(task) == "AGENT_TIMEOUT"

    def test_non_zero_exit_code_is_agent_timeout(self):
        task = self._make_task("non-zero exit code returned by agent runner")
        assert classify_failure(task) == "AGENT_TIMEOUT"

    def test_process_exited_is_agent_timeout(self):
        task = self._make_task("process exited with status 1")
        assert classify_failure(task) == "AGENT_TIMEOUT"

    def test_node_process_killed_is_agent_timeout(self):
        task = self._make_task("node process killed by system")
        assert classify_failure(task) == "AGENT_TIMEOUT"

    def test_oom_killer_is_agent_timeout(self):
        task = self._make_task("oom killer terminated the agent")
        assert classify_failure(task) == "AGENT_TIMEOUT"

    # ── provision failure patterns ───────────────────────────────────────────

    def test_address_already_in_use_is_provision_failure(self):
        task = self._make_task("address already in use on port 8080")
        assert classify_failure(task) == "PROVISION_FAILURE"

    def test_failed_to_connect_is_provision_failure(self):
        task = self._make_task("failed to connect to agent VM during init")
        assert classify_failure(task) == "PROVISION_FAILURE"

    # ── existing patterns still pass ─────────────────────────────────────────

    def test_no_heartbeat_is_agent_timeout(self):
        task = self._make_task("no heartbeat received from agent")
        assert classify_failure(task) == "AGENT_TIMEOUT"

    def test_reaped_is_agent_timeout(self):
        task = self._make_task("task was reaped by orchestrator")
        assert classify_failure(task) == "AGENT_TIMEOUT"

    def test_sigterm_is_agent_timeout(self):
        task = self._make_task("process received sigterm")
        assert classify_failure(task) == "AGENT_TIMEOUT"

    def test_out_of_memory_is_agent_timeout(self):
        task = self._make_task("out of memory error")
        assert classify_failure(task) == "AGENT_TIMEOUT"

    def test_provision_failure(self):
        task = self._make_task("failed to provision VM")
        assert classify_failure(task) == "PROVISION_FAILURE"

    def test_rate_limit(self):
        task = self._make_task("rate limit exceeded")
        assert classify_failure(task) == "RATE_LIMIT"

    def test_code_error_default(self):
        task = self._make_task("AttributeError: 'NoneType' object has no attribute 'foo'")
        assert classify_failure(task) == "CODE_ERROR"

    def test_logs_also_searched(self):
        task = self._make_task("")
        assert classify_failure(task, logs="exit code 137 returned") == "AGENT_TIMEOUT"

    def test_failed_to_connect_does_not_override_db(self):
        """Database connection errors take priority over 'failed to connect'."""
        task = self._make_task("could not connect to server: Connection refused (psycopg)")
        assert classify_failure(task) == "EXTERNAL_API"
