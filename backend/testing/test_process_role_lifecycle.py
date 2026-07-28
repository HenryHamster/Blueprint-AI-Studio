"""Role-split safety tests for the application lifespan."""

from types import SimpleNamespace
import warnings

import pytest

from app.lifecycle import (
    PROCESS_ROLE_ALL,
    PROCESS_ROLE_API,
    PROCESS_ROLE_ENV,
    PROCESS_ROLE_WORKER,
    process_role_is_activated,
    process_role_status,
    resolve_process_role,
    lifespan,
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
    def test_only_legacy_all_role_is_activated(self):
        # This is the exact predicate the lifespan uses. api/worker are parsed
        # but must remain inert until a later durable ownership split.
        assert process_role_is_activated(PROCESS_ROLE_API) is False
        assert process_role_is_activated(PROCESS_ROLE_WORKER) is False
        assert process_role_is_activated(PROCESS_ROLE_ALL) is True

    def test_status_is_safe_and_truthful_for_api_role(self):
        status = process_role_status(PROCESS_ROLE_API, is_leader=False)
        assert status == {
            "role": "api",
            "lifecycle_activation": "not_activated",
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

    @pytest.mark.asyncio
    @pytest.mark.parametrize("role", [PROCESS_ROLE_API, PROCESS_ROLE_WORKER])
    async def test_inactive_role_fails_before_bootstrap_or_global_side_effects(
        self, monkeypatch, role
    ):
        monkeypatch.setenv(PROCESS_ROLE_ENV, role)
        app = SimpleNamespace(state=SimpleNamespace())

        async def forbidden_init_db(*args, **kwargs):  # pragma: no cover - must not run
            raise AssertionError("inactive role must fail before database bootstrap")

        async def forbidden_connect(*args, **kwargs):  # pragma: no cover - must not run
            raise AssertionError("inactive role must fail before advisory-lock access")

        def forbidden_create_task(*args, **kwargs):  # pragma: no cover - must not run
            raise AssertionError("inactive role must fail before creating background tasks")

        def forbidden_basic_config(*args, **kwargs):  # pragma: no cover - must not run
            raise AssertionError("inactive role must fail before logging configuration")

        original_showwarning = warnings.showwarning
        monkeypatch.setattr("app.lifecycle.init_db", forbidden_init_db)
        monkeypatch.setattr(
            "app.lifecycle.engine", SimpleNamespace(connect=forbidden_connect)
        )
        monkeypatch.setattr("app.lifecycle.asyncio.create_task", forbidden_create_task)
        monkeypatch.setattr("app.lifecycle.logging.basicConfig", forbidden_basic_config)
        with pytest.raises(RuntimeError, match="recognized but not yet activated"):
            async with lifespan(app):
                raise AssertionError("inactive role must not yield a running application")
        assert not hasattr(app.state, "process_role_status")
        assert warnings.showwarning is original_showwarning
