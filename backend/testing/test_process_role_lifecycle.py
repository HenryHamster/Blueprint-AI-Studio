"""Role-split safety tests for the application lifespan."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.lifecycle as lifecycle_module
from app.lifecycle import (
    PROCESS_ROLE_ALL,
    PROCESS_ROLE_API,
    PROCESS_ROLE_ENV,
    PROCESS_ROLE_WORKER,
    _acquire_background_leadership,
    _commit_background_leadership,
    _restore_worker_routing_state,
    _run_leader_bootstrap_maintenance,
    _start_leader_keepalive_and_run_maintenance,
    process_role_is_activated,
    process_role_status,
    process_role_uses_leader_election,
    resolve_process_role,
)


class TestProcessRoleResolution:
    def test_default_preserves_legacy_combined_service(self, monkeypatch):
        monkeypatch.delenv(PROCESS_ROLE_ENV, raising=False)
        assert resolve_process_role() == PROCESS_ROLE_ALL

    @pytest.mark.parametrize("raw", ["", "   ", "\t\n"])
    def test_blank_values_preserve_legacy_combined_service(self, raw):
        assert resolve_process_role(raw) == PROCESS_ROLE_ALL

    @pytest.mark.parametrize("raw, expected", [
        ("api", PROCESS_ROLE_API),
        (" WORKER ", PROCESS_ROLE_WORKER),
        ("ALL", PROCESS_ROLE_ALL),
    ])
    def test_accepts_each_documented_role(self, raw, expected):
        assert resolve_process_role(raw) == expected

    @pytest.mark.parametrize("raw", ["scheduler", "api,worker", "true"])
    def test_rejects_ambiguous_or_unknown_roles(self, raw):
        with pytest.raises(ValueError, match=PROCESS_ROLE_ENV):
            resolve_process_role(raw)


class TestProcessRoleBackgroundSafety:
    _MAINTENANCE_FUNCTIONS = (
        "_seed_completion_debt_inventory",
        "_reconcile_dormant_knob_debts",
        "_seed_completion_debt_design_doc",
        "_heal_poisoned_problem_models",
        "_seed_deploy_project_futility_verdicts",
    )

    @pytest.mark.asyncio
    async def test_api_role_skips_worker_routing_hydration(self, monkeypatch):
        seed = AsyncMock()
        monkeypatch.setattr(
            lifecycle_module, "_seed_routing_verdict_from_snapshot", seed
        )
        await _restore_worker_routing_state(worker_services_permitted=False)
        seed.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_worker_role_hydrates_routing_before_dispatch(self, monkeypatch):
        seed = AsyncMock()
        monkeypatch.setattr(
            lifecycle_module, "_seed_routing_verdict_from_snapshot", seed
        )
        await _restore_worker_routing_state(worker_services_permitted=True)
        seed.assert_awaited_once_with()

    @pytest.mark.asyncio
    async def test_nonleader_skips_project_wide_bootstrap_scans(self, monkeypatch):
        mocks = []
        for name in self._MAINTENANCE_FUNCTIONS:
            mock = AsyncMock()
            monkeypatch.setattr(lifecycle_module, name, mock)
            mocks.append(mock)

        await _run_leader_bootstrap_maintenance(is_leader=False)

        for mock in mocks:
            mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_leader_runs_each_bootstrap_maintenance_step(self, monkeypatch):
        mocks = []
        for name in self._MAINTENANCE_FUNCTIONS:
            mock = AsyncMock()
            monkeypatch.setattr(lifecycle_module, name, mock)
            mocks.append(mock)

        await _run_leader_bootstrap_maintenance(is_leader=True)

        for mock in mocks:
            mock.assert_awaited_once_with()

    @pytest.mark.asyncio
    async def test_leader_keepalive_starts_before_slow_bootstrap(self, monkeypatch):
        keepalive_ran = asyncio.Event()
        release_maintenance = asyncio.Event()

        async def tick(_connection):
            keepalive_ran.set()
            return "held"

        async def slow_maintenance(*, is_leader):
            assert is_leader is True
            await release_maintenance.wait()

        monkeypatch.setattr(lifecycle_module, "_leader_lock_keepalive_tick", tick)
        monkeypatch.setattr(
            lifecycle_module, "_run_leader_bootstrap_maintenance", slow_maintenance
        )

        startup = asyncio.create_task(
            _start_leader_keepalive_and_run_maintenance(
                leader_conn=object(),
                is_leader=True,
                keepalive_seconds=0,
            )
        )
        await asyncio.wait_for(keepalive_ran.wait(), timeout=1)
        assert not startup.done()

        release_maintenance.set()
        keepalive_task = await startup
        keepalive_task.cancel()
        await asyncio.gather(keepalive_task, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_bootstrap_failure_releases_leadership(self, monkeypatch):
        class Connection:
            closed = False

            async def close(self):
                self.closed = True

        async def failed_maintenance(*, is_leader):
            assert is_leader is True
            raise RuntimeError("bootstrap failed")

        connection = Connection()
        monkeypatch.setattr(
            lifecycle_module,
            "_run_leader_bootstrap_maintenance",
            failed_maintenance,
        )

        with pytest.raises(RuntimeError, match="bootstrap failed"):
            await _start_leader_keepalive_and_run_maintenance(
                leader_conn=connection,
                is_leader=True,
                keepalive_seconds=60,
            )
        assert connection.closed is True

    @pytest.mark.asyncio
    async def test_nonleader_does_not_start_lock_keepalive(self, monkeypatch):
        tick = AsyncMock(return_value="held")
        maintenance = AsyncMock()
        monkeypatch.setattr(lifecycle_module, "_leader_lock_keepalive_tick", tick)
        monkeypatch.setattr(
            lifecycle_module, "_run_leader_bootstrap_maintenance", maintenance
        )

        keepalive_task = await _start_leader_keepalive_and_run_maintenance(
            leader_conn=object(),
            is_leader=False,
            keepalive_seconds=0,
        )

        assert keepalive_task is None
        tick.assert_not_awaited()
        maintenance.assert_awaited_once_with(is_leader=False)

    @pytest.mark.parametrize(
        "role", [PROCESS_ROLE_API, PROCESS_ROLE_WORKER, PROCESS_ROLE_ALL]
    )
    def test_each_documented_role_is_activated(self, role):
        assert process_role_is_activated(role) is True

    def test_status_is_safe_and_truthful_for_api_role(self):
        status = process_role_status(PROCESS_ROLE_API, is_leader=False)
        assert status == {
            "role": "api",
            "lifecycle_activation": "active",
            "worker_services_permitted": False,
            "worker_services_running": False,
            "leader_election_enabled": False,
            "is_leader": False,
        }

    def test_invalid_role_cannot_be_used_as_an_activation_gate(self):
        with pytest.raises(ValueError, match="unknown process role"):
            process_role_is_activated("not-a-role")

    def test_all_nonleader_status_does_not_claim_worker_services_running(self):
        status = process_role_status(PROCESS_ROLE_ALL, is_leader=False)
        assert status["worker_services_permitted"] is True
        assert status["worker_services_running"] is False
        assert status["leader_election_enabled"] is True

    @pytest.mark.parametrize("role", [PROCESS_ROLE_WORKER, PROCESS_ROLE_ALL])
    def test_worker_capable_roles_use_leader_election(self, role):
        assert process_role_uses_leader_election(role) is True

    def test_api_role_is_fenced_out_of_leader_election(self):
        assert process_role_uses_leader_election(PROCESS_ROLE_API) is False

    def test_api_status_never_claims_worker_services(self):
        status = process_role_status(PROCESS_ROLE_API, is_leader=True)
        assert status["worker_services_permitted"] is False
        assert status["worker_services_running"] is False
        assert status["leader_election_enabled"] is False

    def test_invalid_role_cannot_be_used_as_leader_election_gate(self):
        with pytest.raises(ValueError, match="unknown process role"):
            process_role_uses_leader_election("not-a-role")

    @pytest.mark.asyncio
    async def test_api_role_never_opens_a_leader_election_connection(self):
        class ForbiddenEngine:
            dialect = SimpleNamespace(name="postgresql")

            async def connect(self):  # pragma: no cover - must not run
                raise AssertionError("API role must not touch the advisory lock")

        connection, is_leader = await _acquire_background_leadership(
            PROCESS_ROLE_API, db_engine=ForbiddenEngine()
        )
        assert connection is None
        assert is_leader is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize("role", [PROCESS_ROLE_WORKER, PROCESS_ROLE_ALL])
    async def test_worker_capable_role_attempts_advisory_lock(self, role):
        class ScalarResult:
            @staticmethod
            def scalar():
                return True

        class Connection:
            async def execute(self, *_args, **_kwargs):
                return ScalarResult()

        connection = Connection()

        class Engine:
            dialect = SimpleNamespace(name="postgresql")

            async def connect(self):
                return connection

        acquired_connection, is_leader = await _acquire_background_leadership(
            role, db_engine=Engine()
        )
        assert acquired_connection is connection
        assert is_leader is True

    @pytest.mark.asyncio
    async def test_postgres_lock_failure_fails_closed_and_closes_connection(self):
        class Connection:
            closed = False

            async def execute(self, *_args, **_kwargs):
                raise ConnectionError("database unavailable")

            async def close(self):
                self.closed = True

        connection = Connection()

        class Engine:
            dialect = SimpleNamespace(name="postgresql")

            async def connect(self):
                return connection

        with pytest.raises(ConnectionError, match="database unavailable"):
            await _acquire_background_leadership(
                PROCESS_ROLE_WORKER, db_engine=Engine()
            )
        assert connection.closed is True

    @pytest.mark.asyncio
    async def test_non_postgres_lock_failure_preserves_sole_leader_fallback(self):
        class Connection:
            closed = False

            async def execute(self, *_args, **_kwargs):
                raise RuntimeError("advisory locks unsupported")

            async def close(self):
                self.closed = True

        connection = Connection()

        class Engine:
            dialect = SimpleNamespace(name="sqlite")

            async def connect(self):
                return connection

        acquired_connection, is_leader = await _acquire_background_leadership(
            PROCESS_ROLE_WORKER, db_engine=Engine()
        )
        assert acquired_connection is None
        assert is_leader is True
        assert connection.closed is True

    @pytest.mark.asyncio
    async def test_lock_commit_success_keeps_connection_for_leadership(self):
        class Connection:
            committed = False
            closed = False

            async def commit(self):
                self.committed = True

            async def close(self):
                self.closed = True

        connection = Connection()
        assert await _commit_background_leadership(connection) is True
        assert connection.committed is True
        assert connection.closed is False

    @pytest.mark.asyncio
    async def test_lock_commit_failure_closes_connection_and_fails_closed(self):
        class Connection:
            closed = False

            async def commit(self):
                raise ConnectionError("commit failed")

            async def close(self):
                self.closed = True

        connection = Connection()
        assert await _commit_background_leadership(connection) is False
        assert connection.closed is True
