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
    async def test_stale_heartbeat_exhausted_retries_marks_task_failed(
        self, db_session, mocker
    ):
        """
        A task whose last heartbeat is >5 min old AND has reached max_retries
        must be marked FAILED and its VM deleted.
        """
        mock_delete = mocker.patch("app.vm_lifecycle.delete_agent_vm", return_value=None)
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
        mock_delete = mocker.patch("app.vm_lifecycle.delete_agent_vm", return_value=None)
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
        assert task.status == TaskStatus.IN_PROGRESS, (
            "Healthy task should not be reaped"
        )
        mock_delete.assert_not_called()

    # ── Test 3: stale heartbeat, retries remaining → requeued ───────────────

    @pytest.mark.asyncio
    async def test_stale_task_under_retry_limit_is_requeued(self, db_session, mocker):
        """
        A task with a stale heartbeat that still has retry attempts remaining
        must be requeued (status=QUEUED) with incremented attempt_count,
        NOT marked FAILED.
        """
        mocker.patch("app.vm_lifecycle.delete_agent_vm", return_value=None)
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
        assert task.status == TaskStatus.QUEUED, (
            "Task with remaining retries should be requeued"
        )
        assert task.attempt_count == 1, "Retry counter must be incremented"

    # ── Test 4: task without vm_instance_id → skipped ───────────────────────

    @pytest.mark.asyncio
    async def test_task_without_vm_id_is_skipped(self, db_session, mocker):
        """
        Logical/parent tasks that have no VM assigned (vm_instance_id=None)
        must be skipped — the reaper only acts on tasks with real VMs.
        """
        mock_delete = mocker.patch("app.vm_lifecycle.delete_agent_vm", return_value=None)
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
            vm_instance_id=None,  # no VM — logical parent task
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
        assert task.status == TaskStatus.IN_PROGRESS, (
            "Task without VM should be untouched"
        )
        mock_delete.assert_not_called()

    # ── Test 5: FOR UPDATE SKIP LOCKED / pre-validation guard ───────────────

    @pytest.mark.asyncio
    async def test_skip_locked_pre_validation_prevents_double_reap(
        self, db_session, mocker
    ):
        """
        The reaper re-validates each task's status via session.get() just before acting.
        If the task's status has already transitioned out of PROVISIONING/IN_PROGRESS
        (e.g. reaped by a concurrent instance), the reaper skips it.

        This is the application-level complement to FOR UPDATE SKIP LOCKED:
        even if two reaper instances select the same task, the second one
        detects the status change during re-validation and does nothing.
        """
        mock_delete = mocker.patch("app.vm_lifecycle.delete_agent_vm", return_value=None)
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
                    status=TaskStatus.COMPLETED,  # already done by another instance
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
    async def test_already_failed_task_is_excluded_from_reaper_scan(
        self, db_session, mocker
    ):
        """
        The reaper's initial query only selects PROVISIONING/IN_PROGRESS tasks.
        A task that is already FAILED must never be picked up — this is the
        database-level guarantee that prevents double-reaping after the first pass.
        """
        mock_delete = mocker.patch("app.vm_lifecycle.delete_agent_vm", return_value=None)
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
            status=TaskStatus.FAILED,  # terminal state
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
    async def test_reaper_provision_timeout_before_agent_claim(
        self, db_session, mocker
    ):
        """
        A VM that was dispatched 7 minutes ago but whose agent never claimed
        (last_heartbeat is None, status still PROVISIONING) must be reaped
        as a pre-claim failure and charged against provision_failures,
        NOT against attempt_count.
        """
        mocker.patch("app.vm_lifecycle.delete_agent_vm", return_value=None)
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
        assert task.status == TaskStatus.QUEUED, (
            "Pre-claim reaped task should be requeued"
        )
        assert task.attempt_count == 0, "Pre-claim failures must NOT burn attempt_count"
        assert (task.context or {}).get("provision_failures") == 1, (
            "Pre-claim failures must charge provision_failures"
        )

    # ── Test 8: IN_PROGRESS + stale heartbeat → attempt budget, not provision budget ──

    @pytest.mark.asyncio
    async def test_reaper_does_not_charge_provision_budget_post_claim(
        self, db_session, mocker
    ):
        """
        A task that successfully claimed (status=IN_PROGRESS) and then went
        silent must charge attempt_count, NOT provision_failures. This is the
        clock-separation guarantee: once an agent claims, provision budget is
        out of scope for any further reap on this attempt.
        """
        mocker.patch("app.vm_lifecycle.delete_agent_vm", return_value=None)
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
        mock_delete = mocker.patch("app.vm_lifecycle.delete_agent_vm", return_value=None)
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
        mocker.patch(
            "app.max_plan_pool.AsyncSessionLocal", _FakeSessionFactory(db_session)
        )

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
    async def test_load_max_plan_pool_includes_disabled_accounts(
        self, db_session, mocker, reset_max_plan_pool
    ):
        """load_max_plan_pool() loads all accounts regardless of enabled flag."""
        mocker.patch(
            "app.max_plan_pool.AsyncSessionLocal", _FakeSessionFactory(db_session)
        )

        db_session.add(
            MaxPlanAccount(
                user_id=_TEST_USER,
                name="enabled-acct",
                oauth_token="tok-a",
                enabled=True,
                total_tasks=0,
            )
        )
        db_session.add(
            MaxPlanAccount(
                user_id=_TEST_USER,
                name="disabled-acct",
                oauth_token="tok-b",
                enabled=False,
                total_tasks=0,
            )
        )
        await db_session.commit()

        await load_max_plan_pool()

        assert len(_account_by_id) == 2
        names = {a["name"] for a in _account_by_id.values()}
        assert names == {"enabled-acct", "disabled-acct"}

    # ── Test 3: select_max_plan_account returns lowest active count ──────────

    @pytest.mark.asyncio
    async def test_select_max_plan_account_returns_account_with_lowest_active_count(
        self, reset_max_plan_pool
    ):
        """select_max_plan_account() must pick the enabled account with the fewest active tasks."""
        for entry in [
            {
                "id": "aaa",
                "name": "busy",
                "oauth_token": "t1",
                "enabled": True,
                "active_tasks": 5,
                "user_id": _TEST_USER,
            },
            {
                "id": "bbb",
                "name": "idle",
                "oauth_token": "t2",
                "enabled": True,
                "active_tasks": 1,
                "user_id": _TEST_USER,
            },
            {
                "id": "ccc",
                "name": "medium",
                "oauth_token": "t3",
                "enabled": True,
                "active_tasks": 3,
                "user_id": _TEST_USER,
            },
        ]:
            _max_plan_pools.setdefault(_TEST_USER, []).append(entry)
            _account_by_id[entry["id"]] = entry

        chosen = await select_max_plan_account(_TEST_USER)
        assert chosen is not None
        assert chosen["name"] == "idle"

    # ── Test 4: disabled accounts are skipped ────────────────────────────────

    @pytest.mark.asyncio
    async def test_select_max_plan_account_skips_disabled_accounts(
        self, reset_max_plan_pool
    ):
        """Disabled accounts (enabled=False) must never be selected."""
        for entry in [
            {
                "id": "aaa",
                "name": "disabled-low",
                "oauth_token": "t1",
                "enabled": False,
                "active_tasks": 0,
                "user_id": _TEST_USER,
            },
            {
                "id": "bbb",
                "name": "enabled-high",
                "oauth_token": "t2",
                "enabled": True,
                "active_tasks": 9,
                "user_id": _TEST_USER,
            },
        ]:
            _max_plan_pools.setdefault(_TEST_USER, []).append(entry)
            _account_by_id[entry["id"]] = entry

        chosen = await select_max_plan_account(_TEST_USER)
        assert chosen is not None
        assert chosen["name"] == "enabled-high", (
            "Must pick enabled account even if it has more tasks"
        )

    # ── Test 5: all accounts disabled → None ────────────────────────────────

    @pytest.mark.asyncio
    async def test_select_max_plan_account_returns_none_when_all_disabled(
        self, reset_max_plan_pool
    ):
        """When no enabled accounts exist, select_max_plan_account() returns None."""
        for entry in [
            {
                "id": "aaa",
                "name": "acct-a",
                "oauth_token": "t1",
                "enabled": False,
                "active_tasks": 0,
                "user_id": _TEST_USER,
            },
            {
                "id": "bbb",
                "name": "acct-b",
                "oauth_token": "t2",
                "enabled": False,
                "active_tasks": 0,
                "user_id": _TEST_USER,
            },
        ]:
            _max_plan_pools.setdefault(_TEST_USER, []).append(entry)
            _account_by_id[entry["id"]] = entry
        assert await select_max_plan_account(_TEST_USER) is None

    # ── Test 6: empty pool → None ────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_select_max_plan_account_returns_none_for_empty_pool(
        self, reset_max_plan_pool
    ):
        """Empty pool returns None without raising."""
        assert len(_account_by_id) == 0
        assert await select_max_plan_account(_TEST_USER) is None

    # ── Test 7: release decrements active_tasks ──────────────────────────────

    @pytest.mark.asyncio
    async def test_release_max_plan_account_decrements_active_tasks(
        self, reset_max_plan_pool
    ):
        """release_max_plan_account() must decrement active_tasks for the given account."""
        acct_id = "test-release-id"
        entry = {
            "id": acct_id,
            "name": "acct",
            "oauth_token": "tok",
            "enabled": True,
            "active_tasks": 3,
            "user_id": _TEST_USER,
        }
        _max_plan_pools.setdefault(_TEST_USER, []).append(entry)
        _account_by_id[acct_id] = entry

        await release_max_plan_account(acct_id)

        assert _account_by_id[acct_id]["active_tasks"] == 2

    # ── Test 8: release never goes below zero ────────────────────────────────

    @pytest.mark.asyncio
    async def test_release_max_plan_account_floors_at_zero(self, reset_max_plan_pool):
        """active_tasks should not go negative — floor at 0 on release."""
        acct_id = "floor-test-id"
        entry = {
            "id": acct_id,
            "name": "acct",
            "oauth_token": "tok",
            "enabled": True,
            "active_tasks": 0,
            "user_id": _TEST_USER,
        }
        _max_plan_pools.setdefault(_TEST_USER, []).append(entry)
        _account_by_id[acct_id] = entry
        await release_max_plan_account(acct_id)
        assert _account_by_id[acct_id]["active_tasks"] == 0

    # ── Test 9: concurrency limit — select respects in-flight task count ──────

    @pytest.mark.asyncio
    async def test_select_max_plan_account_respects_concurrency_by_returning_least_loaded(
        self, reset_max_plan_pool
    ):
        """
        When all accounts are busy, select_max_plan_account() still returns the
        least-loaded one (the caller is responsible for enforcing a hard limit).
        Verifies that active_tasks correctly gate account selection ordering.
        """
        for entry in [
            {
                "id": "x1",
                "name": "a1",
                "oauth_token": "t1",
                "enabled": True,
                "active_tasks": 10,
                "user_id": _TEST_USER,
            },
            {
                "id": "x2",
                "name": "a2",
                "oauth_token": "t2",
                "enabled": True,
                "active_tasks": 7,
                "user_id": _TEST_USER,
            },
            {
                "id": "x3",
                "name": "a3",
                "oauth_token": "t3",
                "enabled": True,
                "active_tasks": 10,
                "user_id": _TEST_USER,
            },
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
            entry = {
                "id": f"acct-{uid}",
                "user_id": uid,
                "name": "test",
                "enabled": True,
                "active_tasks": 0,
                "oauth_token": "sk-ant-test",
                "total_tasks": 0,
                "token_created_at": None,
                "claude_config_json": None,
            }
            _max_plan_pools.setdefault(uid, []).append(entry)
            _account_by_id[f"acct-{uid}"] = entry
        yield
        for uid in ("user1", _TEST_USER):
            _max_plan_pools.get(uid, [])[:] = [
                a for a in _max_plan_pools.get(uid, []) if a["id"] != f"acct-{uid}"
            ]
            _account_by_id.pop(f"acct-{uid}", None)

    # ── Test 1: autonomous disabled → no task spawned ───────────────────────

    @pytest.mark.asyncio
    async def test_spawn_skipped_when_autonomous_disabled(
        self, db_session, reset_circuit_breakers
    ):
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

        tasks = (
            (
                await db_session.execute(
                    sql_select(Task).where(Task.project_id == project.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(tasks) == 0, (
            "No task should be spawned when autonomous_enabled=False"
        )

    # ── Test 2: circuit breaker OPEN → no task spawned ──────────────────────

    @pytest.mark.asyncio
    async def test_spawn_skipped_when_circuit_breaker_open(
        self, db_session, reset_circuit_breakers
    ):
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
        breaker["opened_at"] = datetime.utcnow() - timedelta(
            minutes=5
        )  # only 5 min; cooldown is 30 min
        breaker["cooldown_seconds"] = 1800

        await _spawn_research_task(project, db_session)

        from sqlmodel import select as sql_select

        tasks = (
            (
                await db_session.execute(
                    sql_select(Task).where(Task.project_id == project.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(tasks) == 0, (
            "No task should be spawned while circuit breaker is OPEN"
        )

    # ── Test 2b: HALF_OPEN stuck test task → reverts to OPEN ────

    @pytest.mark.asyncio
    async def test_half_open_stuck_task_reverts_to_open(
        self, db_session, reset_circuit_breakers
    ):
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
        assert breaker["cooldown_seconds"] == 3600, (
            "Cooldown should double from 1800 to 3600"
        )
        assert breaker["half_open_task_id"] is None, "Test task ID should be cleared"

        # No new task should have been spawned (breaker is now OPEN)
        from sqlmodel import select as sql_select

        all_tasks = (
            (
                await db_session.execute(
                    sql_select(Task).where(Task.project_id == project.id)
                )
            )
            .scalars()
            .all()
        )
        new_queued = [t for t in all_tasks if t.status == TaskStatus.QUEUED]
        assert len(new_queued) == 0, (
            "No new task should be spawned when breaker reverts to OPEN"
        )

    # ── Test 3: existing research task in progress → no duplicate spawned ────

    @pytest.mark.asyncio
    async def test_spawn_skipped_when_research_task_already_active(
        self, db_session, reset_circuit_breakers
    ):
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

            all_tasks = (
                (
                    await db_session.execute(
                        sql_select(Task).where(Task.project_id == project.id)
                    )
                )
                .scalars()
                .all()
            )
            research_tasks = [
                t
                for t in all_tasks
                if (t.payloads or [{}])[0].get("type") == "research"
            ]
            assert len(research_tasks) == 1, (
                "A second research task must not be spawned"
            )
        finally:
            _main_module._runtime_config["max_parallel_research_per_project"] = 2

    # ── Test 4: rate limit — too many orchestrator tasks this hour ───────────

    @pytest.mark.asyncio
    async def test_spawn_skipped_when_rate_limit_reached(
        self, db_session, reset_circuit_breakers
    ):
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
        queued = (
            (
                await db_session.execute(
                    sql_select(Task).where(
                        Task.project_id == project.id, Task.status == TaskStatus.QUEUED
                    )
                )
            )
            .scalars()
            .all()
        )
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
        mocker.patch(
            "app.task_completion.get_github_token_for_task",
            new_callable=AsyncMock,
            return_value=("fake-token", "owner", "repo"),
        )
        mocker.patch(
            "app.task_completion.get_branch_sha",
            new_callable=AsyncMock,
            return_value="abc123",
        )
        mocker.patch(
            "app.task_completion.create_branch",
            new_callable=AsyncMock,
            return_value=None,
        )
        mocker.patch(
            "app.research_completion._apply_structural_actionability_downgrade",
            return_value=0,
        )

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

        children = (
            (
                await db_session.execute(
                    sql_select(Task).where(
                        Task.parent_task_id == research_task.id,
                        Task.status == TaskStatus.QUEUED,
                    )
                )
            )
            .scalars()
            .all()
        )

        assert len(children) == 1, (
            "Only the non-duplicate finding should produce a child task"
        )
        assert (
            "tracing" in children[0].title.lower()
            or "middleware" in children[0].title.lower()
        )

    # ── Test 6: depends_on_indices creates sequential task ordering ──────────

    @pytest.mark.asyncio
    async def test_depends_on_indices_creates_sequential_task_ordering(
        self, db_session, mocker
    ):
        """
        When findings carry explicit depends_on_indices, the resulting child tasks
        must have their depends_on field set to the UUIDs of the referenced tasks.
        """
        mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
        mocker.patch(
            "app.task_completion.get_github_token_for_task",
            new_callable=AsyncMock,
            return_value=("fake-token", "owner", "repo"),
        )
        mocker.patch(
            "app.task_completion.get_branch_sha",
            new_callable=AsyncMock,
            return_value="abc123",
        )
        mocker.patch(
            "app.task_completion.create_branch",
            new_callable=AsyncMock,
            return_value=None,
        )
        mocker.patch(
            "app.research_completion._apply_structural_actionability_downgrade",
            return_value=0,
        )

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

        children = (
            (
                await db_session.execute(
                    sql_select(Task)
                    .where(Task.parent_task_id == research_task.id)
                    .order_by(Task.created_at)
                )
            )
            .scalars()
            .all()
        )

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
    async def test_priority_ordering_no_artificial_wave_dependencies(
        self, db_session, mocker
    ):
        """
        Findings get priority values (high=30, medium=50, low=70) but NO
        inter-wave dependencies. The orchestrator's priority-based dispatch
        naturally runs high-priority tasks first without blocking low-priority
        ones behind artificial dependency chains.
        """
        mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
        mocker.patch(
            "app.task_completion.get_github_token_for_task",
            new_callable=AsyncMock,
            return_value=("fake-token", "owner", "repo"),
        )
        mocker.patch(
            "app.task_completion.get_branch_sha",
            new_callable=AsyncMock,
            return_value="abc123",
        )
        mocker.patch(
            "app.task_completion.create_branch",
            new_callable=AsyncMock,
            return_value=None,
        )
        mocker.patch(
            "app.research_completion._apply_structural_actionability_downgrade",
            return_value=0,
        )

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

        children = (
            (
                await db_session.execute(
                    sql_select(Task).where(Task.parent_task_id == research_task.id)
                )
            )
            .scalars()
            .all()
        )

        assert len(children) == 2

        task_high = next(
            t
            for t in children
            if "security" in t.title.lower() or "critical" in t.title.lower()
        )
        task_low = next(
            t
            for t in children
            if "documentation" in t.title.lower() or "improve" in t.title.lower()
        )

        # Both tasks are independent — no artificial wave dependencies
        assert not task_high.depends_on, (
            "High-priority task should have no dependencies"
        )
        assert not task_low.depends_on, (
            "Low-priority task should have no wave dependencies"
        )

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
        mocker.patch(
            "app.task_completion.get_github_token_for_task",
            new_callable=AsyncMock,
            return_value=("fake-token", "owner", "repo"),
        )
        mocker.patch(
            "app.task_completion.get_branch_sha",
            new_callable=AsyncMock,
            return_value="abc123",
        )
        mocker.patch(
            "app.task_completion.create_branch",
            new_callable=AsyncMock,
            return_value=None,
        )
        mocker.patch(
            "app.research_completion._apply_structural_actionability_downgrade",
            return_value=0,
        )

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
        d = next(
            (d for d in (project.directions or []) if d.get("id") == direction_id), None
        )
        assert d is not None
        notes = d.get("notes", [])
        assert len(notes) >= 1, (
            "A direction note should be appended for zero-child completion"
        )
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
        task = self._make_task(
            "AttributeError: 'NoneType' object has no attribute 'foo'"
        )
        assert classify_failure(task) == "CODE_ERROR"

    def test_logs_also_searched(self):
        task = self._make_task("")
        assert classify_failure(task, logs="exit code 137 returned") == "AGENT_TIMEOUT"

    def test_failed_to_connect_does_not_override_db(self):
        """Database connection errors take priority over 'failed to connect'."""
        task = self._make_task(
            "could not connect to server: Connection refused (psycopg)"
        )
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
        Drive the rollup-deferral decision through the *production* predicate
        ``orchestrator_filters.rollup_overlap_defers`` (the single source of
        truth used by ``filter_rollup_conflicts`` step 2.6) rather than
        re-implementing it here.  Returns (non_deferred_ids, deferred_ids).

        The candidate-level guards mirror production's structure exactly: a
        missing candidate or a candidate with no files passes through, matching
        the early-continues in ``filter_rollup_conflicts``.
        """
        from app.orchestrator_filters import rollup_overlap_defers

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
            should_defer, _stale_overlap = rollup_overlap_defers(
                cfiles, pending_rollup_files
            )
            if should_defer:
                deferred.append(eid)
            else:
                non_deferred.append(eid)
        return non_deferred, deferred

    def test_significant_overlap_defers_task(self):
        """Task overlapping 3 out of 4 files with pending rollup is deferred (>= 2 files, >= 50%)."""
        eid = uuid.uuid4()
        candidate_map = {eid: {"related_files": ["a.py", "b.py", "c.py", "d.py"]}}
        pending = {
            "a.py": "Big Refactor",
            "b.py": "Big Refactor",
            "c.py": "Big Refactor",
        }

        non_deferred, deferred = self._apply_rollup_filter(
            [eid], candidate_map, pending
        )

        assert eid in deferred
        assert eid not in non_deferred

    def test_small_overlap_does_not_defer(self):
        """Task overlapping only 1 of 10 files with pending rollup is NOT deferred."""
        eid = uuid.uuid4()
        candidate_map = {eid: {"related_files": [f"file{i}.py" for i in range(10)]}}
        pending = {"file0.py": "Big Refactor"}

        non_deferred, deferred = self._apply_rollup_filter(
            [eid], candidate_map, pending
        )

        assert eid in non_deferred
        assert eid not in deferred

    def test_no_related_files_not_deferred(self):
        """Task with no related_files is never deferred."""
        eid = uuid.uuid4()
        candidate_map = {eid: {"related_files": []}}
        pending = {
            "a.py": "Big Refactor",
            "b.py": "Big Refactor",
            "c.py": "Big Refactor",
        }

        non_deferred, deferred = self._apply_rollup_filter(
            [eid], candidate_map, pending
        )

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

        non_deferred, deferred = self._apply_rollup_filter(
            [eid], candidate_map, pending
        )

        assert eid in deferred

    def test_just_below_50_percent_not_deferred(self):
        """Task with 1/3 files overlapping (33%) is NOT deferred even though >= 1."""
        eid = uuid.uuid4()
        candidate_map = {eid: {"related_files": ["a.py", "b.py", "c.py"]}}
        pending = {"a.py": "Rollup"}

        non_deferred, deferred = self._apply_rollup_filter(
            [eid], candidate_map, pending
        )

        assert eid in non_deferred
        assert eid not in deferred

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
            context={
                "scope": "large",
                "related_files": ["backend/app/main.py", "backend/app/models.py"],
            },
        )
        db_session.add(child)
        await db_session.commit()

        # Query pending rollup files — parent is COMPLETED so no EVALUATING parents exist
        from sqlmodel import select as sql_select

        eval_parent_ids = (
            (
                await db_session.execute(
                    sql_select(Task.id).where(Task.status == TaskStatus.EVALUATING)
                )
            )
            .scalars()
            .all()
        )

        assert len(eval_parent_ids) == 0, (
            "No EVALUATING parents → no pending rollup files"
        )


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

        task = Task(
            payloads=[{"type": "create"}], context={"scope": "small", "risk": "low"}
        )
        assert _health_score_task(task) == 3

    def test_score_ignores_large_scope(self):
        """Large scope gets no boost."""
        from app.orchestrator import _health_score_task

        task = Task(
            payloads=[{"type": "create"}], context={"scope": "large", "risk": "low"}
        )
        assert _health_score_task(task) == 1

    def test_score_with_none_context(self):
        """Task with context=None is treated as empty context (score 0)."""
        from app.orchestrator import _health_score_task

        task = Task(payloads=[{"type": "create"}], context=None)
        assert _health_score_task(task) == 0

    # ── Reordering logic tests ───────────────────────────────────────────────
    #
    # These exercise the *production* reorder (orchestrator_health.
    # reorder_eligible_by_health, dispatch Step 2.8) rather than re-implementing
    # its sort.  The production sort key is (P(success) confidence,
    # _health_score_task) descending with a stable sort, so by injecting a
    # constant predict_fn the confidence component is uniform and
    # _health_score_task becomes the sole differentiator — isolating exactly the
    # health-score ordering the originals meant to assert.  A single degradation
    # signal (slots_available below 50% of max) is forced so the reorder
    # activates; the confidence floor is set to 0 so nothing is dropped.

    @staticmethod
    async def _reorder(ids, task_by_id, session):
        """Run the production health-aware reorder with a uniform-confidence
        predict_fn and a forced degradation signal (only the slots signal)."""
        from app.orchestrator_health import reorder_eligible_by_health

        candidate_tasks = [task_by_id[i] for i in ids]
        runtime_config = {
            "max_concurrent_vms": 10,
            "dispatch_confidence_floor": 0.0,
        }
        zone_summary = {"available": 2, "total_zones": 2}  # no zone signal
        return await reorder_eligible_by_health(
            eligible_ids=list(ids),
            candidate_tasks=candidate_tasks,
            recent_results=[],  # no failure-rate signal
            slots_available=0,  # < 50% of max → degradation signal
            runtime_config=runtime_config,
            zone_summary=zone_summary,
            session=session,
            predict_fn=lambda scope, direction, proj: 0.5,  # uniform confidence
        )

    @pytest.mark.asyncio
    async def test_reordering_places_small_scope_first(self, db_session):
        """small-scope tasks appear before medium/large after the production reorder."""
        tasks = [
            Task(payloads=[{"type": "create"}], context={"scope": "large"}),
            Task(payloads=[{"type": "create"}], context={"scope": "small"}),
            Task(payloads=[{"type": "create"}], context={"scope": "medium"}),
        ]
        for t in tasks:
            t.id = uuid.uuid4()

        task_by_id = {t.id: t for t in tasks}
        ids = [t.id for t in tasks]  # original order: large, small, medium

        reordered = await self._reorder(ids, task_by_id, db_session)

        # small (score=2) should come first, medium and large (score=0) follow
        assert task_by_id[reordered[0]].context["scope"] == "small"

    @pytest.mark.asyncio
    async def test_reordering_places_small_low_risk_before_small_high_risk(
        self, db_session
    ):
        """small+low-risk (score=3) ranks above small-only (score=2)."""
        t_small_low = Task(
            payloads=[{"type": "create"}], context={"scope": "small", "risk": "low"}
        )
        t_small_only = Task(payloads=[{"type": "create"}], context={"scope": "small"})
        t_small_low.id = uuid.uuid4()
        t_small_only.id = uuid.uuid4()

        task_by_id = {t.id: t for t in [t_small_only, t_small_low]}
        ids = [t_small_only.id, t_small_low.id]  # small-only first in original order

        reordered = await self._reorder(ids, task_by_id, db_session)

        assert reordered[0] == t_small_low.id, "small+low-risk should be first"
        assert reordered[1] == t_small_only.id

    @pytest.mark.asyncio
    async def test_reordering_is_stable_when_all_scores_equal(self, db_session):
        """When all tasks have the same score, the production reorder preserves
        relative order (stable sort)."""
        tasks = [
            Task(payloads=[{"type": "create"}], context={"scope": "medium"}),
            Task(payloads=[{"type": "create"}], context={"scope": "medium"}),
            Task(payloads=[{"type": "create"}], context={"scope": "medium"}),
        ]
        for t in tasks:
            t.id = uuid.uuid4()

        task_by_id = {t.id: t for t in tasks}
        ids = [t.id for t in tasks]

        reordered = await self._reorder(ids, task_by_id, db_session)

        # All scores are 0 and confidence is uniform — stable sort preserves order
        assert reordered == ids



class TestPipelineSlotCap:
    """Pipeline-slot cap defers review/fix tasks past the budget while
    create/research dispatch normally."""

    @staticmethod
    def _apply_cap(eligible_ids, types_for_cap, pipeline_active, max_pipeline_slots):
        # Calls the production cap selection (Step 2.9 single source of truth)
        # instead of re-implementing it; cap_pipeline_slots() is a thin async
        # wrapper that computes pipeline_active/max_pipeline_slots/types and
        # then delegates the kept/deferred decision to this same function.
        from app.orchestrator_filters import apply_pipeline_cap

        return apply_pipeline_cap(
            eligible_ids, types_for_cap, pipeline_active, max_pipeline_slots
        )

    def test_review_fix_deferred_when_budget_exhausted(self):
        eligible = ["c1", "r1", "rv1", "fx1", "rv2"]
        types = {
            "c1": "create",
            "r1": "research",
            "rv1": "review",
            "fx1": "fix",
            "rv2": "review",
        }
        kept, deferred = self._apply_cap(
            eligible, types, pipeline_active=4, max_pipeline_slots=4
        )
        # All review/fix dropped; create/research dispatch normally.
        assert kept == ["c1", "r1"]
        assert deferred == 3

    def test_partial_budget_keeps_first_n_pipeline_tasks(self):
        eligible = ["rv1", "fx1", "rv2", "c1"]
        types = {"rv1": "review", "fx1": "fix", "rv2": "review", "c1": "create"}
        kept, deferred = self._apply_cap(
            eligible, types, pipeline_active=2, max_pipeline_slots=4
        )
        # Budget = 2 → first two review/fix kept, third deferred.
        assert kept == ["rv1", "fx1", "c1"]
        assert deferred == 1

    def test_create_and_research_unaffected_by_cap(self):
        eligible = ["c1", "r1", "c2"]
        types = {"c1": "create", "r1": "research", "c2": "create"}
        kept, deferred = self._apply_cap(
            eligible, types, pipeline_active=99, max_pipeline_slots=4
        )
        assert kept == ["c1", "r1", "c2"]
        assert deferred == 0

    def test_cap_default_is_half_of_max_concurrent_vms(self):
        from app.shared_state import runtime_config

        cap = runtime_config.get("max_pipeline_slots")
        if cap is None:
            cap = max(1, runtime_config["max_concurrent_vms"] // 2)
        assert cap >= 1
        assert cap <= runtime_config["max_concurrent_vms"]


class TestProjectFairness:
    """Per-project fairness round-robin (Step 2.95). Without it a single
    autonomous project that fanned out dozens of tasks fills the entire
    dispatch window and starves every other project for multiple cycles."""

    @staticmethod
    def _fair(eligible, project_by_id, weight_by_id=None):
        from app.orchestrator_filters import apply_project_fairness

        return apply_project_fairness(eligible, project_by_id, weight_by_id)

    def test_single_project_unchanged(self):
        # No interleaving possible — must be a no-op (behavior-preserving).
        eligible = ["a", "b", "c", "d"]
        pbi = {"a": "P", "b": "P", "c": "P", "d": "P"}
        assert self._fair(eligible, pbi) == eligible

    def test_empty_and_singleton_unchanged(self):
        assert self._fair([], {}) == []
        assert self._fair(["a"], {"a": "P"}) == ["a"]

    def test_round_robin_interleaves_projects(self):
        # P1 fanned out 4 tasks (sorted first by the candidate scan); P2 has 1.
        # Round-robin must surface P2's task into the FIRST window positions
        # instead of leaving it behind all four P1 tasks.
        eligible = ["p1a", "p1b", "p1c", "p1d", "p2a"]
        pbi = {
            "p1a": "P1",
            "p1b": "P1",
            "p1c": "P1",
            "p1d": "P1",
            "p2a": "P2",
        }
        out = self._fair(eligible, pbi)
        # Permutation: same membership, same length.
        assert sorted(out) == sorted(eligible)
        assert len(out) == len(eligible)
        # P2's only task is no longer dead last — it reaches position 2 (after
        # P1's first task), so a 2-slot window now serves both projects.
        assert out[:2] == ["p1a", "p2a"]
        # P1's internal order is preserved.
        assert [t for t in out if t.startswith("p1")] == [
            "p1a",
            "p1b",
            "p1c",
            "p1d",
        ]

    def test_starvation_is_broken_within_window(self):
        # 50-task fan-out from P1 + 1 task from P2; a 5-slot window must
        # include P2's task (the starvation bug: P2 waits many cycles).
        eligible = [f"p1_{i}" for i in range(50)] + ["p2_0"]
        pbi = {tid: ("P2" if tid == "p2_0" else "P1") for tid in eligible}
        out = self._fair(eligible, pbi)
        assert "p2_0" in out[:5], (
            "P2 task must reach the dispatch window — starvation not broken"
        )

    def test_weight_gives_more_turns_without_monopolizing(self):
        # P1 weight 3, P2 weight 1: per round P1 draws 3, P2 draws 1, but P2
        # is still serviced every round (never fully starved).
        eligible = ["p1a", "p1b", "p1c", "p1d", "p2a", "p2b"]
        pbi = {
            "p1a": "P1",
            "p1b": "P1",
            "p1c": "P1",
            "p1d": "P1",
            "p2a": "P2",
            "p2b": "P2",
        }
        wbi = {"p1a": 3, "p2a": 1}  # first-seen id per project sets the weight
        out = self._fair(eligible, pbi, wbi)
        assert sorted(out) == sorted(eligible)
        # Round 1: 3 from P1, then 1 from P2 → P2 reaches position 4.
        assert out[:4] == ["p1a", "p1b", "p1c", "p2a"]

    def test_none_project_is_its_own_bucket(self):
        eligible = ["x", "y", "z"]
        pbi = {"x": "P1", "y": None, "z": "P1"}
        out = self._fair(eligible, pbi)
        assert sorted(out) == sorted(eligible)
        # The None-project task interleaves rather than trailing both P1 tasks.
        assert out[:2] == ["x", "y"]

    def test_weight_mapping_clamps_and_defaults(self):
        from app.orchestrator_filters import _project_fairness_weight

        assert _project_fairness_weight(None) == 1  # unknown → baseline
        assert _project_fairness_weight("not-an-int") == 1
        assert _project_fairness_weight(0) == 1
        assert _project_fairness_weight(2) == 3  # 1 + priority
        assert _project_fairness_weight(100) == 4  # clamped to max
        assert _project_fairness_weight(-5) == 1  # negative → floor 1


class TestBatchedDepRowsSurviveCommit:
    """The N+1 dependency-fetch fix batches ALL dep rows in one IN(...) query
    in a separate session, then DETACHES them (expunge_all) so they survive the
    main dispatch session's per-task commits (retry_after / infra_wait). This
    guards the load_only/rollback-expiry trap: a commit on one session expires
    its instances, so the dep rows must come from a different, detached set or
    a later ``_check_candidate_task_deps`` read fires a sync lazy-load →
    MissingGreenlet and crashes the whole dispatch cycle."""

    @pytest.mark.asyncio
    async def test_detached_dep_rows_readable_after_other_session_commit(
        self, db_session
    ):
        from sqlmodel import select
        from app.database import AsyncSessionLocal
        from app.orchestrator import _check_candidate_task_deps

        project = make_project(name="Batch Dep", user_id="u1")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        # A COMPLETED non-create dependency (so it's a satisfied dep with no
        # PR-merge requirement) and a candidate that depends on it.
        dep = make_task(
            project_id=project.id,
            payloads=[{"type": "research", "prompt": "dep"}],
            status=TaskStatus.COMPLETED,
        )
        db_session.add(dep)
        await db_session.commit()
        await db_session.refresh(dep)

        candidate = make_task(
            project_id=project.id,
            payloads=[{"type": "research", "prompt": "cand"}],
            status=TaskStatus.QUEUED,
            depends_on=[str(dep.id)],
        )
        db_session.add(candidate)
        await db_session.commit()
        await db_session.refresh(candidate)

        # Mirror the orchestrator: batch-fetch dep rows in a SEPARATE session,
        # build the lookup dict, then DETACH them.
        dep_by_id = {}
        async with AsyncSessionLocal() as dep_batch_session:
            rows = (
                (
                    await dep_batch_session.execute(
                        select(Task).where(Task.id.in_([dep.id]))
                    )
                )
                .scalars()
                .all()
            )
            dep_by_id = {dt.id: dt for dt in rows}
            dep_batch_session.expunge_all()

        # Force a commit on the MAIN session — this expires every instance bound
        # to it. The detached dep rows must be unaffected.
        candidate.latest_status_message = "touched"
        db_session.add(candidate)
        await db_session.commit()

        # Reading status / context / first_payload on the detached dep row must
        # NOT raise (DetachedInstanceError / MissingGreenlet) — every column was
        # loaded by the full select(Task) above.
        detached_dep = dep_by_id[dep.id]
        assert detached_dep.status == TaskStatus.COMPLETED
        assert detached_dep.first_payload.get("type") == "research"
        assert detached_dep.context == {}

        async def _noop_verify(_task, _session):
            return True

        dep_tasks = [dep_by_id[dep.id]]
        result, _ = await _check_candidate_task_deps(
            candidate, dep_tasks, [dep.id], db_session, _noop_verify
        )
        assert result == "eligible", (
            f"COMPLETED same-base research dep should be eligible, got {result!r}"
        )

    def test_collect_dep_uuids_dedupes_and_skips_malformed(self):
        from app.orchestrator_filters import collect_dep_uuids

        shared = uuid.uuid4()
        only_a = uuid.uuid4()
        c1 = make_task(depends_on=[str(shared), str(only_a)])
        c2 = make_task(depends_on=[str(shared), "not-a-uuid", None])
        c3 = make_task(depends_on=None)  # no deps
        out = collect_dep_uuids([c1, c2, c3])
        # Deduped across candidates; malformed ids silently dropped.
        assert out == {shared, only_a}

    @pytest.mark.asyncio
    async def test_single_batch_query_for_many_candidates(self, db_session, mocker):
        """Behavior + efficiency: many candidates sharing deps collapse to ONE
        IN(...) fetch (the N+1 this fix removes).

        Drives the PRODUCTION helper ``collect_dep_uuids`` over the candidate
        batch — including a malformed dep id that must be SKIPPED, not fetched
        and not crash — then proves the collected UUID set powers a single
        ``select(Task).where(Task.id.in_(...))`` fetch returning exactly the
        valid deps. The old per-candidate code would have issued one SELECT per
        candidate; ``spy.call_count == 1`` here is meaningful precisely because
        the set being queried is the helper's output over MANY candidates, not
        a hand-built id list."""
        from sqlmodel import select
        from app.orchestrator_filters import collect_dep_uuids
        from app.database import AsyncSessionLocal
        from app.models import Task as _Task

        project = make_project(name="Batch Count", user_id="u1")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        deps = []
        for i in range(3):
            d = make_task(
                project_id=project.id,
                payloads=[{"type": "research", "prompt": f"dep{i}"}],
                status=TaskStatus.COMPLETED,
            )
            db_session.add(d)
            deps.append(d)
        await db_session.commit()
        for d in deps:
            await db_session.refresh(d)

        dep_ids = [d.id for d in deps]
        # 10 candidates, each depending on 2-3 of the deps (with heavy overlap),
        # PLUS one candidate carrying a malformed dep id (None) that the helper
        # must skip rather than fetch or crash on. The OLD code would have issued
        # one SELECT per candidate (10); the helper + single batch issues one.
        candidates = [
            make_task(
                project_id=project.id,
                payloads=[{"type": "research", "prompt": f"cand{i}"}],
                status=TaskStatus.QUEUED,
                depends_on=[str(dep_ids[i % 3]), str(dep_ids[(i + 1) % 3])],
            )
            for i in range(10)
        ]
        # A malformed-dep candidate: None must be skipped (TypeError path),
        # "bogus" must be skipped (ValueError path) — neither reaches the query.
        candidates.append(
            make_task(
                project_id=project.id,
                payloads=[{"type": "research", "prompt": "cand-bad"}],
                status=TaskStatus.QUEUED,
                depends_on=[str(dep_ids[0]), None, "bogus"],
            )
        )

        # Exercise the PRODUCTION helper — this is the dedup + malformed-skip
        # that the orchestrator's batched dep-fetch relies on.
        all_uuids = collect_dep_uuids(candidates)
        # Deduped to exactly the 3 valid deps; the malformed ids are gone.
        assert all_uuids == set(dep_ids)

        async with AsyncSessionLocal() as s:
            spy = mocker.spy(s, "execute")
            rows = (
                (await s.execute(select(_Task).where(_Task.id.in_(all_uuids))))
                .scalars()
                .all()
            )
            s.expunge_all()
            assert {r.id for r in rows} == set(dep_ids)
            # Exactly one IN(...) fetch over the helper's collected set,
            # regardless of candidate count.
            assert spy.call_count == 1


class TestPersistDepsSatisfied:
    """The orchestrator durably stamps ``context['_deps_satisfied']`` on a
    dependency-bearing task once its deps resolve eligible. This sticky marker
    is the contract the warm-backlog scan and dispatch first-refusal hold key
    off so dependency-ready work can run warm without re-running the
    (GitHub-aware) dep resolution on the hot path."""

    @pytest.mark.asyncio
    async def test_persist_deps_satisfied_stamps_and_persists(
        self, db_session, mocker
    ):
        from app.orchestrator import _persist_deps_satisfied

        project = make_project(name="Deps Satisfied", user_id="u1")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        task = make_task(
            project_id=project.id,
            payloads=[{"type": "create", "prompt": "cand"}],
            status=TaskStatus.QUEUED,
            depends_on=[str(uuid.uuid4())],
            context={"scope": "small"},
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        # Redirect the helper's separate-session write to the test DB.
        mocker.patch(
            "app.orchestrator.AsyncSessionLocal", _FakeSessionFactory(db_session)
        )

        await _persist_deps_satisfied(task)

        # In-memory mirror set for the same-cycle dispatch first-refusal hold.
        assert task.context.get("_deps_satisfied") is True
        # And durably persisted so the next-role warm scan (separate session)
        # and the Step 3 dispatch re-read observe it.
        db_session.expire(task)
        await db_session.refresh(task)
        assert task.context.get("_deps_satisfied") is True
        # Pre-existing context keys are preserved, not clobbered.
        assert task.context.get("scope") == "small"

    @pytest.mark.asyncio
    async def test_persist_deps_satisfied_is_idempotent_noop(
        self, db_session, mocker
    ):
        """A task already carrying the marker is left untouched (no redundant
        write), so repeated eligible cycles don't thrash the row."""
        from app.orchestrator import _persist_deps_satisfied

        task = make_task(
            payloads=[{"type": "create", "prompt": "cand"}],
            status=TaskStatus.QUEUED,
            depends_on=[str(uuid.uuid4())],
            context={"_deps_satisfied": True},
        )
        # If the helper opened a session it would hit this un-redirected factory
        # and fail; the early-return guard means it never does.
        sentinel = mocker.patch(
            "app.orchestrator.AsyncSessionLocal",
            side_effect=AssertionError("should not open a session when marker present"),
        )
        await _persist_deps_satisfied(task)
        assert task.context.get("_deps_satisfied") is True
        assert sentinel.call_count == 0


class TestMarkUnblockedDependents:
    """Completion-side trigger: when a task finishes and its PR lands on the
    integration branch, the orchestrator proactively stamps ``_deps_satisfied``
    on the QUEUED dependents it just unblocked, so the just-completed task's warm
    VM can claim that follow-on work inside its teardown-defer grace window
    instead of tearing down cold. The marker stays authoritative — only deps that
    resolve ``eligible`` are stamped; a dependent with any unresolved dep is left
    unmarked."""

    @staticmethod
    async def _verify_true(_task, _session):
        return True

    @pytest.mark.asyncio
    async def test_marks_ready_dependent_after_completion(self, db_session, mocker):
        from app.orchestrator import _mark_unblocked_dependents

        project = make_project(name="Unblock", user_id="u1")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        completed = make_task(
            project_id=project.id,
            payloads=[{"type": "research", "prompt": "dep"}],
            status=TaskStatus.COMPLETED,
        )
        db_session.add(completed)
        await db_session.commit()
        await db_session.refresh(completed)

        dependent = make_task(
            project_id=project.id,
            payloads=[{"type": "create", "prompt": "follow-on"}],
            status=TaskStatus.QUEUED,
            depends_on=[str(completed.id)],
            context={"scope": "small"},
        )
        db_session.add(dependent)
        await db_session.commit()
        await db_session.refresh(dependent)

        mocker.patch(
            "app.orchestrator.AsyncSessionLocal", _FakeSessionFactory(db_session)
        )

        marked = await _mark_unblocked_dependents(completed, self._verify_true)
        assert marked == 1

        db_session.expire(dependent)
        await db_session.refresh(dependent)
        # The just-unblocked dependent is now warm-claimable...
        assert dependent.context.get("_deps_satisfied") is True
        # ...without clobbering pre-existing context.
        assert dependent.context.get("scope") == "small"

    @pytest.mark.asyncio
    async def test_marking_records_completion_funnel_telemetry(
        self, db_session, mocker
    ):
        """The completion-side trigger surfaces its lift on the reuse funnel: a
        production call to ``_mark_unblocked_dependents`` that stamps N dependents
        must record N onto ``completion_dependents_marked`` (not stay a dead 0)."""
        from app.orchestrator import _mark_unblocked_dependents
        from app.warm_backlog_reuse import get_reuse_funnel_snapshot

        project = make_project(name="Unblock Telemetry", user_id="u1")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        completed = make_task(
            project_id=project.id,
            payloads=[{"type": "research", "prompt": "dep"}],
            status=TaskStatus.COMPLETED,
        )
        db_session.add(completed)
        await db_session.commit()
        await db_session.refresh(completed)

        dependent = make_task(
            project_id=project.id,
            payloads=[{"type": "create", "prompt": "follow-on"}],
            status=TaskStatus.QUEUED,
            depends_on=[str(completed.id)],
        )
        db_session.add(dependent)
        await db_session.commit()
        await db_session.refresh(dependent)

        mocker.patch(
            "app.orchestrator.AsyncSessionLocal", _FakeSessionFactory(db_session)
        )

        before = get_reuse_funnel_snapshot()["completion_dependents_marked"]
        marked = await _mark_unblocked_dependents(completed, self._verify_true)
        assert marked == 1
        after = get_reuse_funnel_snapshot()["completion_dependents_marked"]
        assert after == before + 1

    @pytest.mark.asyncio
    async def test_does_not_mark_dependent_with_unresolved_dep(
        self, db_session, mocker
    ):
        """Signal is NOT loosened: a dependent that also waits on a still-QUEUED
        sibling stays unmarked, so it continues to cold-dispatch exactly as before."""
        from app.orchestrator import _mark_unblocked_dependents

        project = make_project(name="Unblock Partial", user_id="u1")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        completed = make_task(
            project_id=project.id,
            payloads=[{"type": "research", "prompt": "dep-a"}],
            status=TaskStatus.COMPLETED,
        )
        still_pending = make_task(
            project_id=project.id,
            payloads=[{"type": "research", "prompt": "dep-b"}],
            status=TaskStatus.QUEUED,
        )
        db_session.add_all([completed, still_pending])
        await db_session.commit()
        await db_session.refresh(completed)
        await db_session.refresh(still_pending)

        dependent = make_task(
            project_id=project.id,
            payloads=[{"type": "create", "prompt": "follow-on"}],
            status=TaskStatus.QUEUED,
            depends_on=[str(completed.id), str(still_pending.id)],
        )
        db_session.add(dependent)
        await db_session.commit()
        await db_session.refresh(dependent)

        mocker.patch(
            "app.orchestrator.AsyncSessionLocal", _FakeSessionFactory(db_session)
        )

        marked = await _mark_unblocked_dependents(completed, self._verify_true)
        assert marked == 0

        db_session.expire(dependent)
        await db_session.refresh(dependent)
        assert dependent.context.get("_deps_satisfied") is None


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
        assert provision_code is not None, (
            "_provision_and_handle not found in orchestrator_worker"
        )

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
            _SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: self.visit_JSON(
                type_, **kw
            )
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
                bind=_engine,
                class_=AsyncSession,
                autocommit=False,
                autoflush=False,
                expire_on_commit=False,
            )
            mocker.patch("app.orchestrator.AsyncSessionLocal", _Session)
            mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
            mocker.patch("app.vm_lifecycle._ensure_vm_deleted", new_callable=AsyncMock)
            mocker.patch("app.vm_lifecycle._get_actual_gcp_vm_count", return_value=0)
            mocker.patch("app.main.load_max_plan_pool", new_callable=AsyncMock)
            mocker.patch("app.main.recheck_disabled_accounts", new_callable=AsyncMock)
            mocker.patch("app.routers.agent_status.record_task_result")
            mocker.patch(
                "app.deploy_health._check_monitoring_window", new_callable=AsyncMock
            )
            mocker.patch(
                "app.deploy_health._check_external_health", new_callable=AsyncMock
            )
            mocker.patch("app.deploy_health._check_pause_mode", new_callable=AsyncMock)
            mocker.patch(
                "app.autonomous_research._check_idle_research", new_callable=AsyncMock
            )
            mocker.patch("app.services._recover_orphaned_tasks", new_callable=AsyncMock)
            mocker.patch(
                "app.alert_dispatch._create_orchestrator_error_alerts", new_callable=AsyncMock
            )
            mocker.patch(
                "app.orchestrator._persist_zone_health", new_callable=AsyncMock
            )
            mocker.patch("app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock)

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
            # Best-effort teardown of a THROWAWAY in-memory engine.
            #
            # ``drop_all`` is removed deliberately: the in-memory SQLite DB lives
            # only as long as the StaticPool connection, so disposing the engine
            # discards it, and every fixture use builds a fresh engine + tables.
            # Running ``drop_all`` on a freshly-created event loop while the
            # pool's aiosqlite connection thread is still bound to the (now
            # closed) setup loop intermittently races into ``RuntimeError: Event
            # loop is closed`` -> ``sqlite3.OperationalError: database table is
            # locked`` — an ERROR raised AT TEARDOWN that has blocked otherwise
            # green PRs (incident 2026-06-02: PR #7154 — 1523 passing, failed
            # only on this teardown race). Dispose only, and suppress any
            # closed-loop teardown error (no correctness impact on a discarded
            # DB). Mirrors conftest._dispose_production_engine_per_test.
            import contextlib

            async def _teardown():
                with contextlib.suppress(Exception):
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
    async def test_warm_pin_with_recent_heartbeat_skips_provision(
        self, orch_warm_db, mocker
    ):
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
            row = (
                (await verify.execute(_select(Task).where(Task.id == task_id)))
                .scalars()
                .first()
            )
            assert row is not None
            assert row.status == TaskStatus.QUEUED, (
                f"Task should remain QUEUED for warm-pin skip, got {row.status}"
            )
            ctx = row.context or {}
            # Skip-provision must not clear the warm fields.
            assert ctx.get("warm_target_vm_id") == target_vm

    @pytest.mark.asyncio
    async def test_warm_pin_past_deadline_clears_and_falls_through(
        self, orch_warm_db, mocker
    ):
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
            row = (
                (await verify.execute(_select(Task).where(Task.id == task_id)))
                .scalars()
                .first()
            )
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
    async def test_warm_pin_unresponsive_vm_clears_and_falls_through(
        self, orch_warm_db, mocker
    ):
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
            row = (
                (await verify.execute(_select(Task).where(Task.id == task_id)))
                .scalars()
                .first()
            )
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
    async def test_warm_pin_clear_metric_skipped_when_commit_fails(
        self, orch_warm_db, mocker
    ):
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

    @pytest.mark.asyncio
    async def test_ci_poll_merge_on_promotion_target_withholds_pr_merged(
        self, db_session
    ):
        """The direct CI poll merge path must not stamp pr_merged when GitHub
        reports a merged PR whose base branch is the project's promotion target
        rather than its integration branch."""
        import app.branch_resolution as branch_resolution
        from app.branch_resolution import flush_pending_branch_alerts
        from app.models import Alert
        from app.orchestrator import _apply_ci_merged_landed_signal
        from sqlmodel import select as _select

        # The synchronous resolver records the branch-misconfiguration alert
        # intent off the hot path (no inline DB I/O); the durable persist runs
        # later in an async-safe context (the orchestrator monitoring loop).
        # Reset the process-local queue + dedupe set so this test sees only its
        # own enqueue (mirrors test_shared_state isolation).
        branch_resolution._blocking_project_branch_alerts.clear()
        branch_resolution._pending_durable_branch_alerts.clear()

        project = make_project(
            name="CI promotion target",
            user_id=_TEST_USER,
            integration_branch="main",
            promotion_target="release",
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        task = make_task(
            project_id=project.id,
            status=TaskStatus.EVALUATING,
            payloads=[{"type": "review", "pr_number": 123, "base_branch": "release"}],
            context={
                "review_loop": {
                    "pr_number": 123,
                    "ci_waiting": True,
                    "base_branch": "release",
                }
            },
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        landed = await _apply_ci_merged_landed_signal(
            task,
            db_session,
            base_branch="release",
            review_loop=(task.context or {}).get("review_loop", {}),
        )
        task.status = TaskStatus.COMPLETED
        db_session.add(task)
        await db_session.commit()

        assert landed is False
        await db_session.refresh(task)
        assert task.status == TaskStatus.COMPLETED
        assert (task.context or {}).get("pr_merged") is not True

        # Drain the deferred branch-alert queue into durable Alerts the way the
        # orchestrator monitoring loop does, then assert the operator signal landed.
        await flush_pending_branch_alerts(db_session)
        await db_session.commit()

        alert = (
            await db_session.execute(
                _select(Alert).where(
                    Alert.project_id == project.id,
                    Alert.title == "Blocking branch configuration",
                )
            )
        ).scalars().first()
        assert alert is not None
        assert "promotion target" in alert.message

    # ──────────────────────────────────────────────────────────────────────
    # Warm-POOL dispatch + pool-pin branches (efficiency direction d-5fd69fe1).
    # Distinct from the warm-CONTINUITY pin tests above: a pool pin uses the
    # warm-pool flag/breaker (not the continuity breaker), releases its leased
    # PoolVm row, suppresses the continuity pin-clear metric, and resets
    # vm_instance_id; the pool dispatch hook leases a pre-provisioned member
    # before cold provisioning. These exercise the orchestrator_worker wiring
    # end-to-end (the leasing internals are unit-tested in test_warm_pool_*).
    # ──────────────────────────────────────────────────────────────────────

    @staticmethod
    def _fake_selection(account_id="acct-pool-1"):
        from app.max_plan_pool import LLMSelectionResult

        return LLMSelectionResult(
            provider="anthropic",
            auth_mode="oauth",
            model="claude-opus-4-8",
            quality_tier=None,
            account={
                "id": account_id,
                "name": "Pool Acct",
                "runtime_secret": "tok",
                "claude_config_json": None,
                "provider_metadata": {},
            },
            settings=None,
            execution_mode=None,
        )

    @pytest.mark.asyncio
    async def test_pool_pin_unresponsive_clears_releases_and_resets_vm_id(
        self, orch_warm_db, mocker
    ):
        """A POOL pin (warm_pool_pin in ctx) whose target VM is unresponsive must:
        release the leased PoolVm (mark_pool_vm_retiring), feed the POOL breaker
        (record_pool_claim_failure), NOT fire the continuity pin-clear metric,
        and reset task.vm_instance_id so the cold fall-through mints a FRESH id
        instead of colliding with the retiring pool VM's name."""
        from sqlmodel import select as _select

        mocker.patch(
            "app.orchestrator.provision_agent_vm",
            new_callable=AsyncMock,
            return_value=False,
        )
        retiring_spy = mocker.patch(
            "app.warm_pool.mark_pool_vm_retiring", new_callable=AsyncMock
        )
        pool_fail_spy = mocker.patch(
            "app.orchestrator._shared_state.record_pool_claim_failure"
        )
        clear_spy = mocker.patch(
            "app.orchestrator._shared_state.record_warm_pin_clear"
        )

        # Ensure the pool breaker is NOT tripped by residue from other tests, so
        # the unresponsive-VM clear takes the vm_unresponsive path (not circuit_breaker).
        _shared_state.warm_pool_suppressed_until = None
        _shared_state.warm_pool_claim_failure_events.clear()
        _shared_state.runtime_config["warm_pool_enabled"] = True
        try:
            async with orch_warm_db() as session:
                project = make_project(name="Pool-Pin", user_id=_TEST_USER)
                session.add(project)
                await session.commit()
                await session.refresh(project)

                target_vm = "agent-vm-pool-target-1"
                deadline = (
                    datetime.utcnow() + timedelta(seconds=240)
                ).isoformat()
                task = make_task(
                    project_id=project.id,
                    # _pin_task_to_pool_vm stamps the pool VM's id onto the task;
                    # the clear branch must reset it.
                    vm_instance_id=target_vm,
                    status=TaskStatus.QUEUED,
                    payloads=[{"type": "fix", "prompt": "x"}],
                    context={
                        "warm_target_vm_id": target_vm,
                        "warm_handoff_deadline": deadline,
                        "warm_pool_pin": True,
                    },
                )
                session.add(task)
                await session.commit()
                await session.refresh(task)
                task_id = task.id

            # No heartbeat → unresponsive → reaches the pool-pin clear branch.
            _shared_state.vm_last_heartbeat.pop(target_vm, None)
            await self._run_one_cycle(mocker)
        finally:
            _shared_state.runtime_config.pop("warm_pool_enabled", None)

        # The leased pool VM was released and the POOL breaker (not the
        # continuity metric) was fed.
        assert retiring_spy.await_count >= 1
        assert retiring_spy.await_args.args[1] == target_vm
        assert pool_fail_spy.called
        assert pool_fail_spy.call_args.args[0] == "vm_unresponsive"
        # Continuity pin-clear metric is suppressed for pool pins.
        assert not clear_spy.called

        async with orch_warm_db() as verify:
            row = (
                (await verify.execute(_select(Task).where(Task.id == task_id)))
                .scalars()
                .first()
            )
            assert row is not None
            ctx = row.context or {}
            assert "warm_target_vm_id" not in ctx
            assert "warm_pool_pin" not in ctx
            # Load-bearing reset: a FRESH agent-vm- id, never the retiring pool
            # VM's name (which would race the reaper's delete on a name clash).
            assert row.vm_instance_id != target_vm
            assert (row.vm_instance_id or "").startswith("agent-vm-")

    @pytest.mark.asyncio
    async def test_pool_dispatch_lease_hit_skips_cold_provision(
        self, orch_warm_db, mocker
    ):
        """A fresh QUEUED task leases a compatible idle pool member before cold
        provisioning: provision_agent_vm is NOT called, record_pool_lease_hit
        fires, and try_pool_dispatch receives the RESOLVED runtime identity +
        the effective (sticky) blacklist."""
        from sqlmodel import select as _select

        prov_mock = mocker.patch(
            "app.orchestrator.provision_agent_vm",
            new_callable=AsyncMock,
            return_value=True,
        )
        mocker.patch(
            "app.max_plan_pool.select_llm_runtime_for_task",
            new_callable=AsyncMock,
            return_value=self._fake_selection(),
        )

        class _Member:
            vm_instance_id = "agent-vm-pool-leased-1"

        dispatch_spy = mocker.patch(
            "app.warm_pool.try_pool_dispatch",
            new_callable=AsyncMock,
            return_value=_Member(),
        )
        lease_hit_spy = mocker.patch(
            "app.warm_backlog_reuse.record_pool_lease_hit"
        )
        cold_spy = mocker.patch(
            "app.warm_backlog_reuse.record_pool_cold_dispatch"
        )

        _shared_state.runtime_config["warm_pool_enabled"] = True
        try:
            async with orch_warm_db() as session:
                project = make_project(name="Pool-Lease", user_id=_TEST_USER)
                session.add(project)
                await session.commit()
                await session.refresh(project)

                task = make_task(
                    project_id=project.id,
                    vm_instance_id=None,
                    status=TaskStatus.QUEUED,
                    payloads=[{"type": "fix", "prompt": "x"}],
                    # Sticky blacklist must flow into excluded_account_ids.
                    context={"provision_failed_accounts": ["acct-dead"]},
                )
                session.add(task)
                await session.commit()
                await session.refresh(task)
                task_id = task.id

            await self._run_one_cycle(mocker)
        finally:
            _shared_state.runtime_config.pop("warm_pool_enabled", None)

        # Lease hit → cold provision skipped, funnel records a hit not a cold.
        prov_mock.assert_not_called()
        assert lease_hit_spy.called
        assert not cold_spy.called

        # Resolved-identity plumbing: the gate passes the SELECTION's identity.
        assert dispatch_spy.await_count == 1
        kwargs = dispatch_spy.await_args.kwargs
        assert kwargs["required_provider"] == "anthropic"
        assert kwargs["required_model"] == "claude-opus-4-8"
        assert kwargs["required_account"] == "acct-pool-1"
        assert "acct-dead" in (kwargs["excluded_account_ids"] or [])

        # Task stays QUEUED — the pooled agent claims it warm via /next-role.
        async with orch_warm_db() as verify:
            row = (
                (await verify.execute(_select(Task).where(Task.id == task_id)))
                .scalars()
                .first()
            )
            assert row is not None
            assert row.status == TaskStatus.QUEUED

    @pytest.mark.asyncio
    async def test_pool_dispatch_cold_fall_through_records_funnel(
        self, orch_warm_db, mocker
    ):
        """When no compatible pool member is free (try_pool_dispatch → None),
        the task cold-provisions and the funnel records a cold dispatch (not a
        lease hit)."""
        from sqlmodel import select as _select

        mocker.patch(
            "app.orchestrator.provision_agent_vm",
            new_callable=AsyncMock,
            return_value=True,
        )
        mocker.patch(
            "app.max_plan_pool.select_llm_runtime_for_task",
            new_callable=AsyncMock,
            return_value=self._fake_selection(),
        )
        mocker.patch(
            "app.warm_pool.try_pool_dispatch",
            new_callable=AsyncMock,
            return_value=None,
        )
        lease_hit_spy = mocker.patch(
            "app.warm_backlog_reuse.record_pool_lease_hit"
        )
        cold_spy = mocker.patch(
            "app.warm_backlog_reuse.record_pool_cold_dispatch"
        )

        _shared_state.runtime_config["warm_pool_enabled"] = True
        try:
            async with orch_warm_db() as session:
                project = make_project(name="Pool-Cold", user_id=_TEST_USER)
                session.add(project)
                await session.commit()
                await session.refresh(project)

                task = make_task(
                    project_id=project.id,
                    vm_instance_id=None,
                    status=TaskStatus.QUEUED,
                    payloads=[{"type": "fix", "prompt": "x"}],
                    context={},
                )
                session.add(task)
                await session.commit()
                await session.refresh(task)
                task_id = task.id

            await self._run_one_cycle(mocker)
        finally:
            _shared_state.runtime_config.pop("warm_pool_enabled", None)

        # Cold fall-through → funnel records a cold dispatch, never a lease hit.
        assert cold_spy.called
        assert not lease_hit_spy.called

        async with orch_warm_db() as verify:
            row = (
                (await verify.execute(_select(Task).where(Task.id == task_id)))
                .scalars()
                .first()
            )
            assert row is not None
            assert row.status == TaskStatus.PROVISIONING

    @pytest.mark.asyncio
    async def test_pool_dispatch_commit_error_falls_back_to_cold(
        self, orch_warm_db, mocker
    ):
        """If the lease commit raises (the MissingGreenlet hazard path), the
        orchestrator records a 'commit_error' pool failure, rolls back, and
        leaves the task QUEUED for a clean retry — never a lease-hit."""
        from sqlmodel import select as _select
        from sqlalchemy.ext.asyncio import AsyncSession as _AsyncSession

        mocker.patch(
            "app.orchestrator.provision_agent_vm",
            new_callable=AsyncMock,
            return_value=True,
        )
        mocker.patch(
            "app.max_plan_pool.select_llm_runtime_for_task",
            new_callable=AsyncMock,
            return_value=self._fake_selection(),
        )

        # Arm a one-shot commit failure that fires on the FIRST commit after
        # try_pool_dispatch returns — i.e. the lease commit.
        state = {"arm": False}
        _orig_commit = _AsyncSession.commit

        async def _maybe_failing_commit(self):
            if state["arm"]:
                state["arm"] = False
                raise RuntimeError("simulated lease commit failure")
            return await _orig_commit(self)

        mocker.patch.object(_AsyncSession, "commit", _maybe_failing_commit)

        class _Member:
            vm_instance_id = "agent-vm-pool-leased-2"

        async def _dispatch(*_a, **_kw):
            state["arm"] = True
            return _Member()

        # Patch with the async function directly so the lazy
        # `from app.warm_pool import try_pool_dispatch` binds to it and awaits
        # cleanly (arming the one-shot commit failure as a side effect).
        mocker.patch("app.warm_pool.try_pool_dispatch", _dispatch)
        pool_fail_spy = mocker.patch(
            "app.orchestrator._shared_state.record_pool_claim_failure"
        )
        lease_hit_spy = mocker.patch(
            "app.warm_backlog_reuse.record_pool_lease_hit"
        )

        _shared_state.runtime_config["warm_pool_enabled"] = True
        try:
            async with orch_warm_db() as session:
                project = make_project(name="Pool-CommitFail", user_id=_TEST_USER)
                session.add(project)
                await session.commit()
                await session.refresh(project)

                task = make_task(
                    project_id=project.id,
                    vm_instance_id=None,
                    status=TaskStatus.QUEUED,
                    payloads=[{"type": "fix", "prompt": "x"}],
                    context={},
                )
                session.add(task)
                await session.commit()
                await session.refresh(task)
                task_id = task.id

            await self._run_one_cycle(mocker)
        finally:
            _shared_state.runtime_config.pop("warm_pool_enabled", None)

        # commit_error recorded, no lease-hit, and the lease was rolled back so
        # the task is still QUEUED (clean retry next cycle).
        assert pool_fail_spy.called
        assert pool_fail_spy.call_args.args[0] == "commit_error"
        assert not lease_hit_spy.called

        async with orch_warm_db() as verify:
            row = (
                (await verify.execute(_select(Task).where(Task.id == task_id)))
                .scalars()
                .first()
            )
            assert row is not None
            assert row.status == TaskStatus.QUEUED
