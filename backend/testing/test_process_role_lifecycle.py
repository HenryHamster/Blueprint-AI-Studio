"""Role-split safety tests for the application lifespan."""

from types import SimpleNamespace

import pytest

from app.lifecycle import (
    PROCESS_ROLE_ALL,
    PROCESS_ROLE_API,
    PROCESS_ROLE_ENV,
    PROCESS_ROLE_WORKER,
    _acquire_background_leadership,
    _commit_background_leadership,
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
