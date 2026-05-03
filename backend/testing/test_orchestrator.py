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
from app.reaper import reaper_service
from app.autonomous_research import _spawn_research_task
from app.failure_classification import classify_failure
from app.research_completion import _handle_research_completion
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
    orig_pools = {k: list(v) for k, v in _max_plan_pools.items()}
    orig_by_id = dict(_account_by_id)
    _max_plan_pools.clear()
    _account_by_id.clear()
    yield
    _max_plan_pools.clear()
    _account_by_id.clear()
    _max_plan_pools.update(orig_pools)
    _account_by_id.update(orig_by_id)


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
        mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
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
        mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
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
        mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
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
        mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
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
        mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
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
        mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
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

    # ── Test 7: PROVISIONING + no heartbeat + stale vm_provisioned_at → pre-claim reap ──

    @pytest.mark.asyncio
    async def test_reaper_provision_timeout_before_agent_claim(self, db_session, mocker):
        """
        A VM that was dispatched 7 minutes ago but whose agent never claimed
        (last_heartbeat is None, status still PROVISIONING) must be reaped
        as a pre-claim failure and charged against provision_failures,
        NOT against attempt_count.
        """
        mocker.patch("app.main.delete_agent_vm", return_value=None)
        mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
        mocker.patch("app.reaper.AsyncSessionLocal", _FakeSessionFactory(db_session))
        StopLoop = self._stop_after_one_cycle(mocker)

        project = make_project(name="Pre-claim reap test", user_id="user1")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        now = datetime.utcnow()
        task = make_task(
            project_id=project.id,
            vm_instance_id="silent-vm-001",
            status=TaskStatus.PROVISIONING,
            started_at=now - timedelta(minutes=7),
            vm_provisioned_at=now - timedelta(minutes=7),
            last_heartbeat=None,  # agent never contacted us
            attempt_count=0,
            context={"provision_zone": "us-east1-b"},
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        try:
            await reaper_service()
        except StopLoop:
            pass

        await db_session.refresh(task)
        assert task.status == TaskStatus.QUEUED, "Pre-claim reaped task should be requeued"
        assert task.attempt_count == 0, "Pre-claim failures must NOT burn attempt_count"
        assert (task.context or {}).get("provision_failures") == 1, (
            "Pre-claim failures must charge provision_failures"
        )

    # ── Test 8: IN_PROGRESS + stale heartbeat → attempt budget, not provision budget ──

    @pytest.mark.asyncio
    async def test_reaper_does_not_charge_provision_budget_post_claim(self, db_session, mocker):
        """
        A task that successfully claimed (status=IN_PROGRESS) and then went
        silent must charge attempt_count, NOT provision_failures. This is the
        clock-separation guarantee: once an agent claims, provision budget is
        out of scope for any further reap on this attempt.
        """
        mocker.patch("app.main.delete_agent_vm", return_value=None)
        mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
        mocker.patch("app.reaper.AsyncSessionLocal", _FakeSessionFactory(db_session))
        StopLoop = self._stop_after_one_cycle(mocker)

        project = make_project(name="Post-claim crash test", user_id="user1")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        now = datetime.utcnow()
        task = make_task(
            project_id=project.id,
            vm_instance_id="crashed-vm-001",
            status=TaskStatus.IN_PROGRESS,
            vm_provisioned_at=now - timedelta(minutes=10),
            last_heartbeat=now - timedelta(minutes=6),  # stale
            attempt_count=0,
            # Pre-existing provision_failures must NOT be bumped here.
            context={"provision_failures": 1},
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        try:
            await reaper_service()
        except StopLoop:
            pass

        await db_session.refresh(task)
        assert task.status == TaskStatus.QUEUED
        assert task.attempt_count == 1, "Post-claim crash must increment attempt_count"
        assert (task.context or {}).get("provision_failures") == 1, (
            "Post-claim crash must NOT touch provision_failures"
        )

    # ── Test 9: fresh vm_provisioned_at → NOT reaped even with no heartbeat ────

    @pytest.mark.asyncio
    async def test_reaper_respects_pre_claim_grace_window(self, db_session, mocker):
        """
        A VM that was just dispatched (vm_provisioned_at < 6min ago) and has
        no heartbeat yet must NOT be reaped — the agent is still within its
        boot + auth + claim budget.
        """
        mock_delete = mocker.patch("app.main.delete_agent_vm", return_value=None)
        mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
        mocker.patch("app.reaper.AsyncSessionLocal", _FakeSessionFactory(db_session))
        StopLoop = self._stop_after_one_cycle(mocker)

        project = make_project(name="Grace window test", user_id="user1")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        now = datetime.utcnow()
        task = make_task(
            project_id=project.id,
            vm_instance_id="booting-vm-001",
            status=TaskStatus.PROVISIONING,
            started_at=now - timedelta(minutes=2),
            vm_provisioned_at=now - timedelta(minutes=2),
            last_heartbeat=None,
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
        assert task.status == TaskStatus.PROVISIONING, "Fresh VM must be left alone"
        mock_delete.assert_not_called()


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

        assert len(_account_by_id) == 1
        entry = _account_by_id[str(acct_id)]
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

        assert len(_account_by_id) == 2
        names = {a["name"] for a in _account_by_id.values()}
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
        assert len(_account_by_id) == 0
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
        _spawn_research_task() must not spawn beyond the parallel limit if a
        research-type task is already QUEUED/PROVISIONING/IN_PROGRESS for the same project.
        """
        _main_module._runtime_config["max_parallel_research_per_project"] = 1
        try:
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
        finally:
            _main_module._runtime_config["max_parallel_research_per_project"] = 2

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
        mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
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
        mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
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
        mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
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

    # ── Test 8: all findings deduped → auto-complete with direction note ────

    @pytest.mark.asyncio
    async def test_all_findings_deduped_auto_completes_task(self, db_session, mocker):
        """
        When _handle_research_completion produces 0 child tasks (all findings
        are duplicates), the research task must transition to COMPLETED (not
        stuck in EVALUATING) and a direction note must be written.
        """
        mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
        mocker.patch("app.task_completion.get_github_token_for_task", new_callable=AsyncMock, return_value=("fake-token", "owner", "repo"))
        mocker.patch("app.task_completion.get_branch_sha", new_callable=AsyncMock, return_value="abc123")
        mocker.patch("app.task_completion.create_branch", new_callable=AsyncMock, return_value=None)

        direction_id = f"d-{uuid.uuid4().hex[:8]}"
        direction = {
            "id": direction_id,
            "text": "Improve test coverage",
            "status": "active",
            "notes": [],
        }
        project = Project(
            name="Zero children test",
            user_id="user1",
            autonomous_enabled=True,
            directions=[direction],
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        # Existing task whose title matches the finding — triggers dedup
        existing_task = Task(
            project_id=project.id,
            title="improve test coverage for auth module",
            payloads=[{"type": "create"}],
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
            status=TaskStatus.IN_PROGRESS,
            created_by="ORCHESTRATOR",
        )
        db_session.add(existing_task)

        research_task = Task(
            project_id=project.id,
            title="Research: test coverage scan",
            payloads=[{"type": "research", "base_branch": "main"}],
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
            status=TaskStatus.IN_PROGRESS,
            created_by="ORCHESTRATOR",
            context={"direction_id": direction_id},
        )
        db_session.add(research_task)
        await db_session.commit()
        await db_session.refresh(research_task)

        # Single finding that duplicates the existing task
        logs = self._findings_json(
            {
                "title": "improve test coverage for auth module",
                "prompt": "Add unit tests for the authentication service.",
                "priority": "medium",
            },
        )

        await _handle_research_completion(research_task, logs, db_session)

        await db_session.refresh(research_task)
        assert research_task.status == TaskStatus.COMPLETED, (
            f"Research task with 0 children should be COMPLETED, got {research_task.status}"
        )
        assert "auto-completed" in (research_task.latest_status_message or "").lower()

        # Verify direction note was written
        await db_session.refresh(project)
        d = next((d for d in (project.directions or []) if d.get("id") == direction_id), None)
        assert d is not None
        notes = d.get("notes", [])
        assert len(notes) >= 1, "A direction note should be appended for zero-child completion"
        assert "0 tasks" in notes[-1].get("msg", "")


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

    def test_container_exited_is_code_error(self):
        task = self._make_task("container exited unexpectedly")
        assert classify_failure(task) == "CODE_ERROR"

    def test_non_zero_exit_code_is_code_error(self):
        task = self._make_task("non-zero exit code returned by agent runner")
        assert classify_failure(task) == "CODE_ERROR"

    def test_process_exited_is_code_error(self):
        task = self._make_task("process exited with status 1")
        assert classify_failure(task) == "CODE_ERROR"

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
        task = self._make_task("API Error: Rate limit reached")
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


# ══════════════════════════════════════════════════════════════════════════════
# Section 5 — Rollup deferral filtering
# ══════════════════════════════════════════════════════════════════════════════

class TestRollupDeferral:
    """
    Unit tests for the rollup-deferral filtering logic in orchestrator_worker().

    The logic (orchestrator.py step 2.6) defers QUEUED tasks whose related_files
    significantly overlap with files from COMPLETED children of EVALUATING rollup
    parents.  Deferral threshold: >= 2 files AND >= 50% of candidate's files.
    """

    def _apply_rollup_filter(self, eligible_ids, candidate_map, pending_rollup_files):
        """
        Pure re-implementation of the rollup-deferral filtering logic from
        orchestrator_worker() step 2.6.  Returns (non_deferred_ids, deferred_ids).
        """
        non_deferred = []
        deferred = []
        for eid in eligible_ids:
            candidate = candidate_map.get(eid)
            if not candidate:
                non_deferred.append(eid)
                continue
            cfiles = set(candidate.get("related_files", []))
            if not cfiles:
                non_deferred.append(eid)
                continue
            stale_overlap = cfiles & pending_rollup_files.keys()
            should_defer = len(stale_overlap) >= 2 and len(stale_overlap) >= len(cfiles) * 0.5
            if should_defer:
                deferred.append(eid)
            else:
                non_deferred.append(eid)
        return non_deferred, deferred

    def test_significant_overlap_defers_task(self):
        """Task overlapping 3 out of 4 files with pending rollup is deferred (>= 2 files, >= 50%)."""
        eid = uuid.uuid4()
        candidate_map = {eid: {"related_files": ["a.py", "b.py", "c.py", "d.py"]}}
        pending = {"a.py": "Big Refactor", "b.py": "Big Refactor", "c.py": "Big Refactor"}

        non_deferred, deferred = self._apply_rollup_filter([eid], candidate_map, pending)

        assert eid in deferred
        assert eid not in non_deferred

    def test_small_overlap_does_not_defer(self):
        """Task overlapping only 1 of 10 files with pending rollup is NOT deferred."""
        eid = uuid.uuid4()
        candidate_map = {eid: {"related_files": [f"file{i}.py" for i in range(10)]}}
        pending = {"file0.py": "Big Refactor"}

        non_deferred, deferred = self._apply_rollup_filter([eid], candidate_map, pending)

        assert eid in non_deferred
        assert eid not in deferred

    def test_no_related_files_not_deferred(self):
        """Task with no related_files is never deferred."""
        eid = uuid.uuid4()
        candidate_map = {eid: {"related_files": []}}
        pending = {"a.py": "Big Refactor", "b.py": "Big Refactor", "c.py": "Big Refactor"}

        non_deferred, deferred = self._apply_rollup_filter([eid], candidate_map, pending)

        assert eid in non_deferred
        assert eid not in deferred

    def test_no_pending_rollup_files_passes_through(self):
        """When there are no pending rollup files, all tasks pass through unaffected."""
        ids = [uuid.uuid4() for _ in range(3)]
        candidate_map = {eid: {"related_files": ["a.py", "b.py"]} for eid in ids}

        non_deferred, deferred = self._apply_rollup_filter(ids, candidate_map, {})

        assert set(non_deferred) == set(ids)
        assert deferred == []

    def test_exactly_50_percent_overlap_defers(self):
        """Task with exactly 2/4 files overlapping (50%) is deferred (boundary case)."""
        eid = uuid.uuid4()
        candidate_map = {eid: {"related_files": ["a.py", "b.py", "c.py", "d.py"]}}
        pending = {"a.py": "Rollup", "b.py": "Rollup"}

        non_deferred, deferred = self._apply_rollup_filter([eid], candidate_map, pending)

        assert eid in deferred

    def test_just_below_50_percent_not_deferred(self):
        """Task with 1/3 files overlapping (33%) is NOT deferred even though >= 1."""
        eid = uuid.uuid4()
        candidate_map = {eid: {"related_files": ["a.py", "b.py", "c.py"]}}
        pending = {"a.py": "Rollup"}

        non_deferred, deferred = self._apply_rollup_filter([eid], candidate_map, pending)

        assert eid in non_deferred
        assert eid not in deferred

    @pytest.mark.asyncio
    async def test_rollup_deferral_sets_status_message(self, db_session, mocker):
        """
        When a QUEUED task significantly overlaps pending-rollup files, its
        latest_status_message is updated to indicate it's waiting for the rollup.
        """
        from unittest.mock import AsyncMock
        mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)

        project = Project(name="Rollup deferral test", user_id="user1")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        # Parent task in EVALUATING state (pending rollup)
        parent = Task(
            project_id=project.id,
            title="Rollup: big refactor",
            payloads=[{"type": "plan"}],
            status=TaskStatus.EVALUATING,
        )
        db_session.add(parent)
        await db_session.commit()
        await db_session.refresh(parent)

        # Completed child of the EVALUATING parent with large scope
        child = Task(
            project_id=project.id,
            title="Refactor: decompose module",
            payloads=[{"type": "refactor"}],
            status=TaskStatus.COMPLETED,
            parent_task_id=parent.id,
            context={"scope": "large", "related_files": ["backend/app/main.py", "backend/app/models.py", "backend/app/admin.py"]},
        )
        db_session.add(child)

        # QUEUED candidate that overlaps 3/3 of its files with the pending rollup
        candidate = Task(
            project_id=project.id,
            title="Add new feature",
            payloads=[{"type": "create"}],
            status=TaskStatus.QUEUED,
            context={"related_files": ["backend/app/main.py", "backend/app/models.py", "backend/app/admin.py"]},
        )
        db_session.add(candidate)
        await db_session.commit()
        await db_session.refresh(candidate)

        # Apply the rollup-deferral logic directly (mirrors orchestrator step 2.6)
        from sqlmodel import select as sql_select
        eval_parent_ids = (await db_session.execute(
            sql_select(Task.id).where(Task.status == TaskStatus.EVALUATING)
        )).scalars().all()

        pending_children = (await db_session.execute(
            sql_select(Task).where(
                Task.parent_task_id.in_(eval_parent_ids),
                Task.status == TaskStatus.COMPLETED,
            )
        )).scalars().all()

        pending_rollup_files: dict[str, str] = {}
        for pc in pending_children:
            pc_scope = (pc.context or {}).get("scope", "small")
            pc_type = (pc.first_payload).get("type", "")
            if pc_scope == "large" or pc_type in ("refactor", "plan"):
                for f in (pc.context or {}).get("related_files", []):
                    pending_rollup_files[f] = pc.title or str(pc.id)

        assert len(pending_rollup_files) == 3, "All 3 child files should be in pending rollup set"

        cfiles = set((candidate.context or {}).get("related_files", []))
        stale_overlap = cfiles & pending_rollup_files.keys()
        should_defer = len(stale_overlap) >= 2 and len(stale_overlap) >= len(cfiles) * 0.5

        assert should_defer, "Candidate with 3/3 file overlap should be deferred"

        if should_defer:
            blocking_title = pending_rollup_files[next(iter(stale_overlap))]
            defer_msg = f"Waiting: overlaps pending rollup '{blocking_title[:60]}'"
            candidate.latest_status_message = defer_msg
            db_session.add(candidate)
            await db_session.commit()
            await db_session.refresh(candidate)

        assert "Waiting: overlaps pending rollup" in candidate.latest_status_message

    @pytest.mark.asyncio
    async def test_completed_parent_no_longer_defers(self, db_session, mocker):
        """
        Once the rollup parent transitions out of EVALUATING (e.g., COMPLETED),
        its children's files no longer appear in pending_rollup_files and the
        candidate task is NOT deferred.
        """
        project = Project(name="Completed rollup test", user_id="user1")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        # Parent is now COMPLETED (rollup merged) — not EVALUATING
        parent = Task(
            project_id=project.id,
            title="Rollup: merged refactor",
            payloads=[{"type": "plan"}],
            status=TaskStatus.COMPLETED,
        )
        db_session.add(parent)
        await db_session.commit()
        await db_session.refresh(parent)

        child = Task(
            project_id=project.id,
            title="Refactor: decompose module",
            payloads=[{"type": "refactor"}],
            status=TaskStatus.COMPLETED,
            parent_task_id=parent.id,
            context={"scope": "large", "related_files": ["backend/app/main.py", "backend/app/models.py"]},
        )
        db_session.add(child)
        await db_session.commit()

        # Query pending rollup files — parent is COMPLETED so no EVALUATING parents exist
        from sqlmodel import select as sql_select
        eval_parent_ids = (await db_session.execute(
            sql_select(Task.id).where(Task.status == TaskStatus.EVALUATING)
        )).scalars().all()

        assert len(eval_parent_ids) == 0, "No EVALUATING parents → no pending rollup files"


# ══════════════════════════════════════════════════════════════════════════════
# Section 5 — Health-aware dispatch reordering
# ══════════════════════════════════════════════════════════════════════════════

class TestHealthAwareDispatch:
    """Tests for _health_score_task and the health-aware dispatch reordering logic."""

    # ── Score computation tests ──────────────────────────────────────────────

    def test_score_zero_for_task_with_no_scope_or_risk(self):
        """A task with no context gets a score of 0."""
        from app.orchestrator import _health_score_task
        task = Task(payloads=[{"type": "create"}])
        assert _health_score_task(task) == 0

    def test_score_zero_for_medium_scope(self):
        """A medium-scope task gets no boost (non-small)."""
        from app.orchestrator import _health_score_task
        task = Task(payloads=[{"type": "create"}], context={"scope": "medium"})
        assert _health_score_task(task) == 0

    def test_score_boost_for_small_scope(self):
        """Small scope adds +2."""
        from app.orchestrator import _health_score_task
        task = Task(payloads=[{"type": "create"}], context={"scope": "small"})
        assert _health_score_task(task) == 2

    def test_score_boost_for_low_risk(self):
        """Low risk adds +1."""
        from app.orchestrator import _health_score_task
        task = Task(payloads=[{"type": "create"}], context={"risk": "low"})
        assert _health_score_task(task) == 1

    def test_score_max_for_small_scope_and_low_risk(self):
        """Small scope + low risk = +3 (maximum boost)."""
        from app.orchestrator import _health_score_task
        task = Task(payloads=[{"type": "create"}], context={"scope": "small", "risk": "low"})
        assert _health_score_task(task) == 3

    def test_score_ignores_large_scope(self):
        """Large scope gets no boost."""
        from app.orchestrator import _health_score_task
        task = Task(payloads=[{"type": "create"}], context={"scope": "large", "risk": "low"})
        assert _health_score_task(task) == 1

    def test_score_with_none_context(self):
        """Task with context=None is treated as empty context (score 0)."""
        from app.orchestrator import _health_score_task
        task = Task(payloads=[{"type": "create"}], context=None)
        assert _health_score_task(task) == 0

    # ── Reordering logic tests ───────────────────────────────────────────────

    def test_reordering_places_small_scope_first(self):
        """Sorted by _health_score_task, small-scope tasks appear before medium/large."""
        from app.orchestrator import _health_score_task
        tasks = [
            Task(payloads=[{"type": "create"}], context={"scope": "large"}),
            Task(payloads=[{"type": "create"}], context={"scope": "small"}),
            Task(payloads=[{"type": "create"}], context={"scope": "medium"}),
        ]
        for t in tasks:
            t.id = uuid.uuid4()

        task_by_id = {t.id: t for t in tasks}
        ids = [t.id for t in tasks]  # original order: large, small, medium

        reordered = sorted(ids, key=lambda tid: _health_score_task(task_by_id[tid]), reverse=True)

        # small (score=2) should come first, medium and large (score=0) follow
        assert task_by_id[reordered[0]].context["scope"] == "small"

    def test_reordering_places_small_low_risk_before_small_high_risk(self):
        """small+low-risk (score=3) ranks above small-only (score=2)."""
        from app.orchestrator import _health_score_task
        t_small_low = Task(payloads=[{"type": "create"}], context={"scope": "small", "risk": "low"})
        t_small_only = Task(payloads=[{"type": "create"}], context={"scope": "small"})
        t_small_low.id = uuid.uuid4()
        t_small_only.id = uuid.uuid4()

        task_by_id = {t.id: t for t in [t_small_only, t_small_low]}
        ids = [t_small_only.id, t_small_low.id]  # small-only first in original order

        reordered = sorted(ids, key=lambda tid: _health_score_task(task_by_id[tid]), reverse=True)

        assert reordered[0] == t_small_low.id, "small+low-risk should be first"
        assert reordered[1] == t_small_only.id

    def test_reordering_is_stable_when_all_scores_equal(self):
        """When all tasks have the same score, sorted() preserves relative order (stable sort)."""
        from app.orchestrator import _health_score_task
        tasks = [
            Task(payloads=[{"type": "create"}], context={"scope": "medium"}),
            Task(payloads=[{"type": "create"}], context={"scope": "medium"}),
            Task(payloads=[{"type": "create"}], context={"scope": "medium"}),
        ]
        for t in tasks:
            t.id = uuid.uuid4()

        task_by_id = {t.id: t for t in tasks}
        ids = [t.id for t in tasks]

        reordered = sorted(ids, key=lambda tid: _health_score_task(task_by_id[tid]), reverse=True)

        # All scores are 0 — stable sort preserves original order
        assert reordered == ids

    # ── Degradation-signal tests ─────────────────────────────────────────────

    def test_zone_degradation_signal_detects_partial_cooldown(self):
        """Zone summary with available < total_zones triggers the zone degradation signal."""
        summary = {"available": 3, "total_zones": 5}
        assert summary["available"] < summary["total_zones"]

    def test_zone_degradation_signal_absent_when_all_zones_healthy(self):
        """Zone summary with available == total_zones should not trigger zone degradation."""
        summary = {"available": 5, "total_zones": 5}
        assert not (summary["available"] < summary["total_zones"])

    def test_failure_rate_signal_triggers_above_30_percent(self):
        """Failure rate > 30% in the recent window triggers the signal."""
        # 4 failures in 10 results = 40% — above threshold
        recent_window = [
            ("ts", "failure"), ("ts", "success"), ("ts", "failure"), ("ts", "success"),
            ("ts", "failure"), ("ts", "success"), ("ts", "failure"), ("ts", "success"),
            ("ts", "success"), ("ts", "success"),
        ]
        fail_count = sum(1 for r in recent_window if r[1] == "failure")
        rate = fail_count / len(recent_window)
        assert rate > 0.30

    def test_failure_rate_signal_absent_below_30_percent(self):
        """Failure rate ≤ 30% should not trigger the signal."""
        # 3 failures in 10 results = 30% — at threshold but NOT above (strict >)
        recent_window = [
            ("ts", "failure"), ("ts", "failure"), ("ts", "failure"),
            ("ts", "success"), ("ts", "success"), ("ts", "success"),
            ("ts", "success"), ("ts", "success"), ("ts", "success"), ("ts", "success"),
        ]
        fail_count = sum(1 for r in recent_window if r[1] == "failure")
        rate = fail_count / len(recent_window)
        assert not (rate > 0.30)

    def test_slot_signal_triggers_below_50_percent(self):
        """slots_available < max_vms * 0.5 triggers the slots degradation signal."""
        max_vms = 8
        slots_available = 3  # < 4.0 (50% of 8)
        assert slots_available < max_vms * 0.5

    def test_slot_signal_absent_at_50_percent(self):
        """slots_available == max_vms * 0.5 does not trigger (strict <)."""
        max_vms = 8
        slots_available = 4  # == 4.0 (exactly 50%)
        assert not (slots_available < max_vms * 0.5)

    # ── Dispatch confidence floor tests ──────────────────────────────────────

    def test_confidence_floor_default_from_runtime_config(self):
        """Default floor is 0.10 when the key is missing from runtime_config."""
        _runtime_config: dict = {}
        _conf_floor = float(_runtime_config.get("dispatch_confidence_floor", 0.10) or 0.0)
        assert _conf_floor == 0.10

    def test_confidence_floor_none_value_coerced_to_zero(self):
        """An explicit None value in runtime_config disables the floor (treated as 0.0)."""
        _runtime_config = {"dispatch_confidence_floor": None}
        _conf_floor = float(_runtime_config.get("dispatch_confidence_floor", 0.10) or 0.0)
        assert _conf_floor == 0.0

    def test_confidence_floor_filters_low_confidence_tasks(self):
        """Tasks with P(success) below the floor are removed from eligible_ids."""
        confidence_by_id = {"t_low": 0.05, "t_mid": 0.15, "t_high": 0.50}
        eligible_ids = ["t_low", "t_mid", "t_high"]
        _conf_floor = 0.10
        filtered = [tid for tid in eligible_ids if confidence_by_id[tid] >= _conf_floor]
        assert filtered == ["t_mid", "t_high"]

    def test_confidence_floor_at_threshold_is_kept(self):
        """A task with P(success) exactly at the floor is kept (>= comparison)."""
        confidence_by_id = {"t_exact": 0.10, "t_above": 0.20}
        eligible_ids = ["t_exact", "t_above"]
        _conf_floor = 0.10
        filtered = [tid for tid in eligible_ids if confidence_by_id[tid] >= _conf_floor]
        assert filtered == ["t_exact", "t_above"]

    def test_confidence_floor_zero_skips_filter(self):
        """_conf_floor == 0 bypasses the filter entirely — no tasks removed."""
        confidence_by_id = {"t1": 0.01, "t2": 0.99}
        eligible_ids = ["t1", "t2"]
        _conf_floor = 0.0
        if _conf_floor > 0:
            filtered = [tid for tid in eligible_ids if confidence_by_id[tid] >= _conf_floor]
        else:
            filtered = eligible_ids
        assert filtered == ["t1", "t2"]

    def test_confidence_floor_respects_runtime_config_override(self):
        """An operator-provided floor overrides the 0.10 default."""
        _runtime_config = {"dispatch_confidence_floor": 0.25}
        _conf_floor = float(_runtime_config.get("dispatch_confidence_floor", 0.10) or 0.0)
        confidence_by_id = {"t_low": 0.15, "t_high": 0.30}
        eligible_ids = ["t_low", "t_high"]
        filtered = [tid for tid in eligible_ids if confidence_by_id[tid] >= _conf_floor]
        assert filtered == ["t_high"]


class TestPipelineSlotCap:
    """Pipeline-slot cap defers review/fix tasks past the budget while
    create/research dispatch normally."""

    @staticmethod
    def _apply_cap(eligible_ids, types_for_cap, pipeline_active, max_pipeline_slots):
        # Mirrors the gate in orchestrator.py Step 2.9.
        budget = max(0, max_pipeline_slots - pipeline_active)
        kept: list = []
        deferred = 0
        pipeline_kept = 0
        for tid in eligible_ids:
            if types_for_cap.get(tid) in ("review", "fix"):
                if pipeline_kept < budget:
                    pipeline_kept += 1
                    kept.append(tid)
                else:
                    deferred += 1
            else:
                kept.append(tid)
        return kept, deferred

    def test_review_fix_deferred_when_budget_exhausted(self):
        eligible = ["c1", "r1", "rv1", "fx1", "rv2"]
        types = {"c1": "create", "r1": "research",
                 "rv1": "review", "fx1": "fix", "rv2": "review"}
        kept, deferred = self._apply_cap(eligible, types, pipeline_active=4, max_pipeline_slots=4)
        # All review/fix dropped; create/research dispatch normally.
        assert kept == ["c1", "r1"]
        assert deferred == 3

    def test_partial_budget_keeps_first_n_pipeline_tasks(self):
        eligible = ["rv1", "fx1", "rv2", "c1"]
        types = {"rv1": "review", "fx1": "fix", "rv2": "review", "c1": "create"}
        kept, deferred = self._apply_cap(eligible, types, pipeline_active=2, max_pipeline_slots=4)
        # Budget = 2 → first two review/fix kept, third deferred.
        assert kept == ["rv1", "fx1", "c1"]
        assert deferred == 1

    def test_create_and_research_unaffected_by_cap(self):
        eligible = ["c1", "r1", "c2"]
        types = {"c1": "create", "r1": "research", "c2": "create"}
        kept, deferred = self._apply_cap(eligible, types, pipeline_active=99, max_pipeline_slots=4)
        assert kept == ["c1", "r1", "c2"]
        assert deferred == 0

    def test_cap_default_is_half_of_max_concurrent_vms(self):
        from app.shared_state import runtime_config
        cap = runtime_config.get("max_pipeline_slots")
        if cap is None:
            cap = max(1, runtime_config["max_concurrent_vms"] // 2)
        assert cap >= 1
        assert cap <= runtime_config["max_concurrent_vms"]


class TestOrchestratorClosureSafety:
    """Regression: a `from app.compute import X` inside orchestrator_worker
    silently shadows the module-level X for the *entire* function, which then
    makes any reference to X from a nested closure (like _provision_and_handle)
    resolve to an unbound enclosing-scope cell when the import branch hasn't
    run yet. Production saw this on every successful provision via 8294c265 →
    `cannot access free variable 'get_vm_zone'` → vm_provisioned_at refresh
    skipped → tasks stuck in PROVISIONING. Guard the names that matter."""

    def test_provision_and_handle_has_no_get_vm_zone_freevar(self):
        """Static check: the inner _provision_and_handle closure must not
        treat get_vm_zone as a free variable. If it does, orchestrator_worker
        is locally re-importing get_vm_zone somewhere, and provisioning will
        crash silently the moment the local import branch hasn't run."""
        from app import orchestrator

        # Find the nested _provision_and_handle code object inside orchestrator_worker
        worker_code = orchestrator.orchestrator_worker.__code__
        provision_code = None
        for const in worker_code.co_consts:
            if hasattr(const, "co_name") and const.co_name == "_provision_and_handle":
                provision_code = const
                break
        assert provision_code is not None, "_provision_and_handle not found in orchestrator_worker"

        # If get_vm_zone is in co_freevars, it's being captured from the
        # enclosing scope as a closure cell — which means orchestrator_worker
        # has it as a local. That's the bug.
        assert "get_vm_zone" not in provision_code.co_freevars, (
            "_provision_and_handle is capturing get_vm_zone as a closure variable, "
            "which means orchestrator_worker has it as a local (likely from a "
            "`from app.compute import get_vm_zone` somewhere). Move the import "
            "to module level."
        )


# ══════════════════════════════════════════════════════════════════════════════
# Phase 2c — warm-VM continuity skip-provision branch
# ══════════════════════════════════════════════════════════════════════════════


def _extract_warm_pin_clear_args(spy):
    """Helper: pull (vm_id, reason) from a record_warm_pin_clear spy regardless
    of whether the orchestrator passes them as positional or keyword args.

    record_warm_pin_clear(vm_id: str, reason: str) — orchestrator currently
    uses positional, but we tolerate both so the assertion stays robust if
    the call site is later refactored to kwargs."""
    call = spy.call_args
    args, kwargs = call.args, call.kwargs
    if len(args) >= 2:
        return args[0], args[1]
    if len(args) == 1 and "reason" in kwargs:
        return args[0], kwargs["reason"]
    return kwargs.get("vm_id"), kwargs.get("reason")


class TestWarmVMSkipProvision:
    """Tests for the orchestrator's warm-pin branch in the dispatch loop.

    The full dispatch loop is heavyweight to drive end-to-end (account pool,
    deadlock detection, etc.), so we use a one-cycle harness like
    test_pipeline_reliability.py and assert on the post-cycle DB state and
    on whether ``provision_agent_vm`` was called.
    """

    @staticmethod
    def _naive_utcnow():
        from datetime import timezone as _tz
        return datetime.now(_tz.utc).replace(tzinfo=None)

    @pytest.fixture()
    def orch_warm_db(self, mocker):
        """In-memory DB plus the lazy mocks orchestrator_worker needs."""
        import asyncio
        from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool
        from sqlmodel import SQLModel
        from sqlalchemy.dialects.sqlite.base import (
            SQLiteTypeCompiler as _SQLiteTypeCompiler,
        )
        if not hasattr(_SQLiteTypeCompiler, "visit_JSONB"):
            _SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: self.visit_JSON(type_, **kw)
        import app.orchestrator as _orch_module

        async def _build():
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
            mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
            mocker.patch("app.main._ensure_vm_deleted", new_callable=AsyncMock)
            mocker.patch("app.main._get_actual_gcp_vm_count", return_value=0)
            mocker.patch("app.main.load_max_plan_pool", new_callable=AsyncMock)
            mocker.patch("app.main.recheck_disabled_accounts", new_callable=AsyncMock)
            mocker.patch("app.routers.agent_status.record_task_result")
            mocker.patch("app.deploy_health._check_monitoring_window", new_callable=AsyncMock)
            mocker.patch("app.deploy_health._check_external_health", new_callable=AsyncMock)
            mocker.patch("app.deploy_health._check_pause_mode", new_callable=AsyncMock)
            mocker.patch("app.autonomous_research._check_idle_research", new_callable=AsyncMock)
            mocker.patch("app.services._recover_orphaned_tasks", new_callable=AsyncMock)
            mocker.patch("app.main._create_orchestrator_error_alerts", new_callable=AsyncMock)
            mocker.patch("app.orchestrator._persist_zone_health", new_callable=AsyncMock)
            mocker.patch("app.main._maybe_create_failure_alert", new_callable=AsyncMock)

            now = self._naive_utcnow()
            _orch_module._last_pool_refresh = now
            _orch_module._last_zone_health_persist = now
            _orch_module._last_orphan_check = now
            _orch_module._last_evaluating_timeout_check = now
            _shared_state.deploy_monitoring["task_results"].clear()
            return _Session, _engine

        # Run the async setup synchronously so the fixture body returns.
        loop = asyncio.new_event_loop()
        try:
            _Session, _engine = loop.run_until_complete(_build())
        finally:
            loop.close()
        try:
            yield _Session
        finally:
            async def _teardown():
                async with _engine.begin() as conn:
                    await conn.run_sync(SQLModel.metadata.drop_all)
                await _engine.dispose()
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(_teardown())
            finally:
                loop.close()

    @staticmethod
    async def _run_one_cycle(mocker):
        import asyncio
        import contextlib
        import app.orchestrator as _orch_module
        mocker.patch("asyncio.sleep", side_effect=asyncio.CancelledError())
        with contextlib.suppress(asyncio.CancelledError):
            await _orch_module.orchestrator_worker()

    @pytest.mark.asyncio
    async def test_warm_pin_with_recent_heartbeat_skips_provision(self, orch_warm_db, mocker):
        """A QUEUED task with valid warm_target_vm_id + recent VM heartbeat
        keeps its QUEUED status and does NOT trigger provision_agent_vm."""
        from sqlmodel import select as _select

        prov_mock = mocker.patch(
            "app.orchestrator.provision_agent_vm",
            new_callable=AsyncMock,
            return_value=True,
        )

        async with orch_warm_db() as session:
            project = make_project(name="Warm-Pin Skip", user_id=_TEST_USER)
            session.add(project)
            await session.commit()
            await session.refresh(project)

            target_vm = "agent-vm-warm-target-1"
            deadline = (datetime.utcnow() + timedelta(seconds=240)).isoformat()
            task = make_task(
                project_id=project.id,
                vm_instance_id=None,
                status=TaskStatus.QUEUED,
                payloads=[{"type": "fix", "prompt": "x"}],
                context={
                    "warm_target_vm_id": target_vm,
                    "warm_target_pr": "1234",
                    "warm_handoff_deadline": deadline,
                },
            )
            session.add(task)
            await session.commit()
            await session.refresh(task)
            task_id = task.id

        # Mark VM as alive via the shared-state heartbeat map
        _shared_state.vm_last_heartbeat[target_vm] = datetime.utcnow()
        try:
            await self._run_one_cycle(mocker)
        finally:
            _shared_state.vm_last_heartbeat.pop(target_vm, None)

        prov_mock.assert_not_called()

        async with orch_warm_db() as verify:
            row = (await verify.execute(_select(Task).where(Task.id == task_id))).scalars().first()
            assert row is not None
            assert row.status == TaskStatus.QUEUED, (
                f"Task should remain QUEUED for warm-pin skip, got {row.status}"
            )
            ctx = row.context or {}
            # Skip-provision must not clear the warm fields.
            assert ctx.get("warm_target_vm_id") == target_vm

    @pytest.mark.asyncio
    async def test_warm_pin_past_deadline_clears_and_falls_through(self, orch_warm_db, mocker):
        """When warm_handoff_deadline is in the past, the orchestrator clears
        the warm fields and proceeds to provision. We verify the warm fields
        are gone after one cycle, and assert record_warm_pin_clear was invoked
        with reason='deadline_expired' (Issue 9a)."""
        from sqlmodel import select as _select

        # Block any actual provision to keep the test fast
        mocker.patch(
            "app.orchestrator.provision_agent_vm",
            new_callable=AsyncMock,
            return_value=False,
        )
        # Spy on record_warm_pin_clear: it's a sync call inside the dispatch
        # path. We patch at the orchestrator's _shared_state import binding
        # so the spy intercepts the in-loop call (Issue 9a).
        record_clear_spy = mocker.patch(
            "app.orchestrator._shared_state.record_warm_pin_clear",
        )

        async with orch_warm_db() as session:
            project = make_project(name="Warm-Pin Expired", user_id=_TEST_USER)
            session.add(project)
            await session.commit()
            await session.refresh(project)

            target_vm = "agent-vm-warm-target-2"
            past = (datetime.utcnow() - timedelta(seconds=60)).isoformat()
            task = make_task(
                project_id=project.id,
                vm_instance_id=None,
                status=TaskStatus.QUEUED,
                payloads=[{"type": "fix", "prompt": "x"}],
                context={
                    "warm_target_vm_id": target_vm,
                    "warm_target_pr": "5555",
                    "warm_handoff_deadline": past,
                },
            )
            session.add(task)
            await session.commit()
            await session.refresh(task)
            task_id = task.id

        # Heartbeat is fresh, but deadline is past — the warm fields should
        # still be cleared (deadline takes precedence).
        _shared_state.vm_last_heartbeat[target_vm] = datetime.utcnow()
        try:
            await self._run_one_cycle(mocker)
        finally:
            _shared_state.vm_last_heartbeat.pop(target_vm, None)

        async with orch_warm_db() as verify:
            row = (await verify.execute(_select(Task).where(Task.id == task_id))).scalars().first()
            assert row is not None
            ctx = row.context or {}
            assert "warm_target_vm_id" not in ctx, (
                "Past-deadline warm pin must be cleared from context"
            )
            assert "warm_target_pr" not in ctx
            assert "warm_handoff_deadline" not in ctx

        # Issue 9a: assert record_warm_pin_clear was called with the correct
        # reason for the deadline-expired path.
        assert record_clear_spy.called, "record_warm_pin_clear must be invoked"
        # Match the (vm_id, reason) positional signature; tolerate kwargs too.
        _vm_arg, _reason_arg = _extract_warm_pin_clear_args(record_clear_spy)
        assert _vm_arg == target_vm
        assert _reason_arg == "deadline_expired", (
            f"Expected reason='deadline_expired', got {_reason_arg!r}"
        )

    @pytest.mark.asyncio
    async def test_warm_pin_unresponsive_vm_clears_and_falls_through(self, orch_warm_db, mocker):
        """A QUEUED task whose warm target VM has no recent heartbeat must
        have warm fields cleared (deadline still future, but VM is silent).
        Verifies record_warm_pin_clear is called with reason='vm_unresponsive'
        (Issue 9a)."""
        from sqlmodel import select as _select

        mocker.patch(
            "app.orchestrator.provision_agent_vm",
            new_callable=AsyncMock,
            return_value=False,
        )
        record_clear_spy = mocker.patch(
            "app.orchestrator._shared_state.record_warm_pin_clear",
        )

        async with orch_warm_db() as session:
            project = make_project(name="Warm-Pin Silent VM", user_id=_TEST_USER)
            session.add(project)
            await session.commit()
            await session.refresh(project)

            target_vm = "agent-vm-warm-target-3"
            deadline = (datetime.utcnow() + timedelta(seconds=240)).isoformat()
            task = make_task(
                project_id=project.id,
                vm_instance_id=None,
                status=TaskStatus.QUEUED,
                payloads=[{"type": "fix", "prompt": "x"}],
                context={
                    "warm_target_vm_id": target_vm,
                    "warm_target_pr": "9999",
                    "warm_handoff_deadline": deadline,
                },
            )
            session.add(task)
            await session.commit()
            await session.refresh(task)
            task_id = task.id

        # No heartbeat entry → VM unresponsive
        _shared_state.vm_last_heartbeat.pop(target_vm, None)
        await self._run_one_cycle(mocker)

        async with orch_warm_db() as verify:
            row = (await verify.execute(_select(Task).where(Task.id == task_id))).scalars().first()
            assert row is not None
            ctx = row.context or {}
            assert "warm_target_vm_id" not in ctx, (
                "Unresponsive-VM warm pin must be cleared from context"
            )
            assert "warm_target_pr" not in ctx
            assert "warm_handoff_deadline" not in ctx

        # Issue 9a: assert record_warm_pin_clear was called with the correct
        # reason for the silent-VM path.
        assert record_clear_spy.called, "record_warm_pin_clear must be invoked"
        _vm_arg, _reason_arg = _extract_warm_pin_clear_args(record_clear_spy)
        assert _vm_arg == target_vm
        assert _reason_arg == "vm_unresponsive", (
            f"Expected reason='vm_unresponsive', got {_reason_arg!r}"
        )

    @pytest.mark.asyncio
    async def test_warm_pin_clear_metric_skipped_when_commit_fails(self, orch_warm_db, mocker):
        """Issue 11: the warm-pin clear metric must NOT fire if the commit
        that persists the warm-field clear raises. On commit failure the
        rollback path leaves the warm fields intact, so a re-run of the
        dispatch loop can record the clear cleanly without double-counting."""

        # Force the dispatch-time PROVISIONING commit to fail. We reach the
        # main commit path by having an account selected, then the commit
        # raises. Simpler: drive the unresponsive-VM clear path and patch
        # session.commit to raise on first call after the warm-clear.
        mocker.patch(
            "app.orchestrator.provision_agent_vm",
            new_callable=AsyncMock,
            return_value=False,
        )
        record_clear_spy = mocker.patch(
            "app.orchestrator._shared_state.record_warm_pin_clear",
        )

        # Force a path between warm-clear (which sets _pending_warm_pin_clear)
        # and the first commit to raise an unhandled exception. The orch's
        # outer `except Exception` rolls back the session, so the warm-clear
        # writes never persist — and the metric must therefore NOT fire
        # (Issue 11 acceptance: if commit fails, record_warm_pin_clear was
        # not called).
        # select_llm_runtime_for_task is lazily imported INSIDE
        # orchestrator_worker via `from app.max_plan_pool import ...`, so
        # the binding inside that function resolves through app.max_plan_pool
        # — patch there to reliably intercept the call.
        async def _raising_select(*_a, **_kw):
            raise RuntimeError("simulated runtime selection failure")
        mocker.patch(
            "app.max_plan_pool.select_llm_runtime_for_task",
            side_effect=_raising_select,
        )

        async with orch_warm_db() as session:
            project = make_project(name="Warm-Pin CommitFail", user_id=_TEST_USER)
            session.add(project)
            await session.commit()
            await session.refresh(project)

            target_vm = "agent-vm-warm-target-commitfail"
            deadline = (datetime.utcnow() + timedelta(seconds=240)).isoformat()
            task = make_task(
                project_id=project.id,
                vm_instance_id=None,
                status=TaskStatus.QUEUED,
                payloads=[{"type": "fix", "prompt": "x"}],
                context={
                    "warm_target_vm_id": target_vm,
                    "warm_target_pr": "1234",
                    "warm_handoff_deadline": deadline,
                },
            )
            session.add(task)
            await session.commit()
            await session.refresh(task)

        # No heartbeat entry → VM unresponsive → reaches warm-clear branch.
        _shared_state.vm_last_heartbeat.pop(target_vm, None)
        await self._run_one_cycle(mocker)

        # Because select_llm_runtime_for_task raised AFTER warm-clear was
        # written but BEFORE any commit completed, no commit landed — so
        # record_warm_pin_clear must NOT have fired (Issue 11 acceptance).
        assert not record_clear_spy.called, (
            "record_warm_pin_clear must not be invoked when the commit "
            "that persists the warm-field clear has not landed"
        )
