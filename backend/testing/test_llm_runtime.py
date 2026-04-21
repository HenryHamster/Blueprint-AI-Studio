from types import SimpleNamespace

import pytest

from app.failure_classification import RATE_LIMIT, classify_failure
from app.max_plan_pool import (
    _account_by_id,
    _max_plan_pools,
    clear_task_llm_runtime_context,
    select_llm_runtime_for_task,
)
from app.orchestrator import _resolve_env_runtime_fallback, _sync_task_payload_runtime_fields


_TEST_USER = "runtime-user"


@pytest.fixture()
def clean_runtime_pool():
    orig_pools = {k: list(v) for k, v in _max_plan_pools.items()}
    orig_by_id = dict(_account_by_id)
    _max_plan_pools.clear()
    _account_by_id.clear()
    yield
    _max_plan_pools.clear()
    _account_by_id.clear()
    _max_plan_pools.update(orig_pools)
    _account_by_id.update(orig_by_id)


def _task(*, payload=None, context=None):
    return SimpleNamespace(
        payloads=[payload or {}],
        context=context or {},
    )


@pytest.mark.asyncio
async def test_select_llm_runtime_for_task_uses_explicit_model_provider(clean_runtime_pool):
    entry = {
        "id": "acct-anthropic",
        "user_id": _TEST_USER,
        "name": "anthropic",
        "provider": "anthropic",
        "auth_mode": "oauth",
        "oauth_token": "sk-ant-test",
        "runtime_ready": True,
        "enabled": True,
        "active_tasks": 0,
    }
    _max_plan_pools.setdefault(_TEST_USER, []).append(entry)
    _account_by_id[entry["id"]] = entry

    runtime_config = {
        "provider_tier_model_map": {
            "anthropic": {"opus": "claude-opus-4-7", "sonnet": "claude-sonnet-4-6", "haiku": "claude-haiku-4-5-20251001"},
            "openai": {"opus": "gpt-5", "sonnet": "gpt-4.1", "haiku": "gpt-4o-mini"},
            "gemini": {"opus": "gemini-2.5-pro", "sonnet": "gemini-2.5-flash", "haiku": "gemini-2.5-flash-lite"},
        },
        "task_type_provider_preference": {"create": ["anthropic", "openai"]},
    }

    result = await select_llm_runtime_for_task(
        user_id=_TEST_USER,
        task=_task(payload={"type": "create", "model": "claude-sonnet-4-6"}),
        runtime_config=runtime_config,
    )

    assert result is not None
    assert result.provider == "anthropic"
    assert result.model == "claude-sonnet-4-6"
    assert result.account_id == "acct-anthropic"


@pytest.mark.asyncio
async def test_select_llm_runtime_for_task_uses_quality_tier_mapping(clean_runtime_pool):
    entry = {
        "id": "acct-anthropic",
        "user_id": _TEST_USER,
        "name": "anthropic",
        "provider": "anthropic",
        "auth_mode": "oauth",
        "oauth_token": "sk-ant-test",
        "runtime_ready": True,
        "enabled": True,
        "active_tasks": 0,
    }
    _max_plan_pools.setdefault(_TEST_USER, []).append(entry)
    _account_by_id[entry["id"]] = entry

    runtime_config = {
        "provider_tier_model_map": {
            "anthropic": {"opus": "claude-opus-4-7", "sonnet": "claude-sonnet-4-6", "haiku": "claude-haiku-4-5-20251001"},
        },
        "task_type_provider_preference": {"create": ["anthropic"]},
    }

    result = await select_llm_runtime_for_task(
        user_id=_TEST_USER,
        task=_task(payload={"type": "create", "quality_tier": "sonnet"}),
        runtime_config=runtime_config,
    )

    assert result is not None
    assert result.provider == "anthropic"
    assert result.model == "claude-sonnet-4-6"
    assert result.quality_tier == "sonnet"


@pytest.mark.asyncio
async def test_select_llm_runtime_for_task_resolves_execution_mode_per_provider(clean_runtime_pool):
    entry = {
        "id": "acct-anthropic",
        "user_id": _TEST_USER,
        "name": "anthropic",
        "provider": "anthropic",
        "auth_mode": "oauth",
        "oauth_token": "sk-ant-test",
        "runtime_ready": True,
        "enabled": True,
        "active_tasks": 0,
    }
    _max_plan_pools.setdefault(_TEST_USER, []).append(entry)
    _account_by_id[entry["id"]] = entry

    runtime_config = {
        "provider_tier_model_map": {
            "anthropic": {"opus": "claude-opus-4-7", "sonnet": "claude-sonnet-4-6", "haiku": "claude-haiku-4-5-20251001"},
        },
        "task_type_provider_preference": {"research": ["anthropic"]},
        "provider_execution_modes": {"anthropic": ["default", "advisor"]},
        "task_type_execution_defaults": {"research": "advisor"},
    }

    result = await select_llm_runtime_for_task(
        user_id=_TEST_USER,
        task=_task(payload={"type": "research", "quality_tier": "sonnet"}),
        runtime_config=runtime_config,
    )

    assert result is not None
    assert result.execution_mode == "advisor"


@pytest.mark.asyncio
async def test_select_llm_runtime_for_task_prefers_concrete_model_overrides(clean_runtime_pool):
    openai = {
        "id": "acct-openai",
        "user_id": _TEST_USER,
        "name": "openai",
        "provider": "openai",
        "auth_mode": "api_key",
        "runtime_secret": "sk-test",
        "runtime_ready": True,
        "enabled": True,
        "active_tasks": 0,
    }
    anthropic = {
        "id": "acct-anthropic",
        "user_id": _TEST_USER,
        "name": "anthropic",
        "provider": "anthropic",
        "auth_mode": "oauth",
        "oauth_token": "sk-ant-test",
        "runtime_ready": True,
        "enabled": True,
        "active_tasks": 0,
    }
    _max_plan_pools.setdefault(_TEST_USER, []).extend([openai, anthropic])
    _account_by_id[openai["id"]] = openai
    _account_by_id[anthropic["id"]] = anthropic

    runtime_config = {
        "provider_tier_model_map": {
            "anthropic": {"opus": "claude-opus-4-7", "sonnet": "claude-sonnet-4-6", "haiku": "claude-haiku-4-5-20251001"},
            "openai": {"opus": "gpt-5", "sonnet": "gpt-4.1", "haiku": "gpt-4o-mini"},
        },
        "task_type_provider_preference": {"create": ["anthropic", "openai"]},
        "task_type_model_preferences": {
            "create": [
                {"provider": "openai", "model": "gpt-5"},
                {"provider": "anthropic", "model": "claude-opus-4-7"},
            ],
        },
    }

    result = await select_llm_runtime_for_task(
        user_id=_TEST_USER,
        task=_task(payload={"type": "create", "quality_tier": "opus"}),
        runtime_config=runtime_config,
    )

    assert result is not None
    assert result.provider == "openai"
    assert result.model == "gpt-5"
    assert result.account_id == "acct-openai"


@pytest.mark.asyncio
async def test_select_llm_runtime_for_task_respects_disabled_cross_provider_fallback(clean_runtime_pool):
    openai = {
        "id": "acct-openai",
        "user_id": _TEST_USER,
        "name": "openai",
        "provider": "openai",
        "auth_mode": "api_key",
        "runtime_secret": "sk-test",
        "runtime_ready": True,
        "enabled": True,
        "active_tasks": 0,
    }
    anthropic = {
        "id": "acct-anthropic",
        "user_id": _TEST_USER,
        "name": "anthropic",
        "provider": "anthropic",
        "auth_mode": "oauth",
        "oauth_token": "sk-ant-test",
        "runtime_ready": True,
        "enabled": False,
        "active_tasks": 0,
    }
    _max_plan_pools.setdefault(_TEST_USER, []).extend([anthropic, openai])
    _account_by_id[openai["id"]] = openai
    _account_by_id[anthropic["id"]] = anthropic

    runtime_config = {
        "cross_provider_fallback_enabled": False,
        "provider_tier_model_map": {
            "anthropic": {"opus": "claude-opus-4-7", "sonnet": "claude-sonnet-4-6", "haiku": "claude-haiku-4-5-20251001"},
            "openai": {"opus": "gpt-5", "sonnet": "gpt-4.1", "haiku": "gpt-4o-mini"},
        },
        "task_type_provider_preference": {"create": ["anthropic", "openai"]},
    }

    result = await select_llm_runtime_for_task(
        user_id=_TEST_USER,
        task=_task(payload={"type": "create", "quality_tier": "opus"}),
        runtime_config=runtime_config,
    )

    assert result is not None
    assert result.provider == "anthropic"
    assert result.account is None


@pytest.mark.asyncio
async def test_select_llm_runtime_for_task_keeps_explicit_provider_when_fallback_disabled(clean_runtime_pool):
    anthropic = {
        "id": "acct-anthropic",
        "user_id": _TEST_USER,
        "name": "anthropic",
        "provider": "anthropic",
        "auth_mode": "oauth",
        "oauth_token": "sk-ant-test",
        "runtime_ready": True,
        "enabled": True,
        "active_tasks": 0,
    }
    _max_plan_pools.setdefault(_TEST_USER, []).append(anthropic)
    _account_by_id[anthropic["id"]] = anthropic

    runtime_config = {
        "cross_provider_fallback_enabled": False,
        "provider_tier_model_map": {
            "anthropic": {"opus": "claude-opus-4-7", "sonnet": "claude-sonnet-4-6", "haiku": "claude-haiku-4-5-20251001"},
            "openai": {"opus": "gpt-5", "sonnet": "gpt-4.1", "haiku": "gpt-4o-mini"},
        },
        "task_type_provider_preference": {"create": ["anthropic", "openai"]},
    }

    result = await select_llm_runtime_for_task(
        user_id=_TEST_USER,
        task=_task(payload={"type": "create", "model": "gpt-5"}),
        runtime_config=runtime_config,
    )

    assert result is not None
    assert result.provider == "openai"
    assert result.model == "gpt-5"
    assert result.account is None


@pytest.mark.asyncio
async def test_select_llm_runtime_for_task_preserves_unknown_explicit_model(clean_runtime_pool):
    openai = {
        "id": "acct-openai",
        "user_id": _TEST_USER,
        "name": "openai",
        "provider": "openai",
        "auth_mode": "api_key",
        "runtime_secret": "sk-test",
        "runtime_ready": True,
        "enabled": True,
        "active_tasks": 0,
    }
    _max_plan_pools.setdefault(_TEST_USER, []).append(openai)
    _account_by_id[openai["id"]] = openai

    runtime_config = {
        "cross_provider_fallback_enabled": False,
        "provider_tier_model_map": {
            "anthropic": {"opus": "claude-opus-4-7", "sonnet": "claude-sonnet-4-6", "haiku": "claude-haiku-4-5-20251001"},
            "openai": {"opus": "gpt-5", "sonnet": "gpt-4.1", "haiku": "gpt-4o-mini"},
        },
        "task_type_provider_preference": {"create": ["openai", "anthropic"]},
    }

    result = await select_llm_runtime_for_task(
        user_id=_TEST_USER,
        task=_task(payload={"type": "create", "model": "gpt-5-custom-preview"}),
        runtime_config=runtime_config,
    )

    assert result is not None
    assert result.provider == "openai"
    assert result.model == "gpt-5-custom-preview"
    assert result.account_id == "acct-openai"


def test_clear_task_llm_runtime_context_strips_all_runtime_keys():
    cleared = clear_task_llm_runtime_context({
        "llm_account_id": "acct-1",
        "llm_provider": "anthropic",
        "llm_auth_mode": "oauth",
        "llm_model": "claude-opus-4-7",
        "max_plan_account_id": "acct-1",
        "scope": "small",
    })

    assert "llm_account_id" not in cleared
    assert "llm_provider" not in cleared
    assert "llm_auth_mode" not in cleared
    assert "llm_model" not in cleared
    assert "max_plan_account_id" not in cleared


def test_sync_task_payload_runtime_fields_overwrites_stale_values():
    task = SimpleNamespace(
        payloads=[{"type": "create", "model": "claude-opus-4-7", "execution_mode": "default"}]
    )

    changed = _sync_task_payload_runtime_fields(
        task,
        model="gpt-5",
        execution_mode="advisor",
    )

    assert changed is True
    assert task.payloads[0]["model"] == "gpt-5"
    assert task.payloads[0]["execution_mode"] == "advisor"


def test_resolve_env_runtime_fallback_prefers_openai_env(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-openai")

    secret, provider, auth_mode, model, execution_mode, metadata = _resolve_env_runtime_fallback(
        provider="openai",
        model="gpt-5",
        execution_mode=None,
        task=_task(payload={"type": "create"}),
        runtime_config={},
        resolve_execution_mode=lambda task, runtime_config, provider: "default",
    )

    assert secret == "sk-test-openai"
    assert provider == "openai"
    assert auth_mode == "api_key"
    assert model == "gpt-5"
    assert execution_mode == "default"
    assert metadata == {}


def test_resolve_env_runtime_fallback_reads_openai_oauth_from_codex_home(tmp_path, monkeypatch):
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    auth_file = codex_home / "auth.json"
    auth_file.write_text('{"access_token":"test"}', encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    secret, provider, auth_mode, model, execution_mode, metadata = _resolve_env_runtime_fallback(
        provider="openai",
        model="gpt-5",
        execution_mode=None,
        task=_task(payload={"type": "create"}),
        runtime_config={},
        resolve_execution_mode=lambda task, runtime_config, provider: "default",
    )

    assert secret == '{"access_token":"test"}'
    assert provider == "openai"
    assert auth_mode == "oauth"
    assert model == "gpt-5"
    assert execution_mode == "default"
    assert metadata == {}


def test_resolve_env_runtime_fallback_reads_gemini_service_account(tmp_path, monkeypatch):
    service_account_path = tmp_path / "sa.json"
    service_account_path.write_text('{"type":"service_account","client_email":"agent@test","private_key":"abc"}', encoding="utf-8")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(service_account_path))
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "demo-project")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")

    secret, provider, auth_mode, model, execution_mode, metadata = _resolve_env_runtime_fallback(
        provider="gemini",
        model="gemini-2.5-pro",
        execution_mode=None,
        task=_task(payload={"type": "research"}),
        runtime_config={},
        resolve_execution_mode=lambda task, runtime_config, provider: "advisor",
    )

    assert '"type":"service_account"' in secret
    assert provider == "gemini"
    assert auth_mode == "service_account"
    assert model == "gemini-2.5-pro"
    assert execution_mode == "advisor"
    assert metadata == {"project": "demo-project", "location": "us-central1"}


def test_resolve_env_runtime_fallback_rejects_gemini_service_account_without_project_location(tmp_path, monkeypatch):
    service_account_path = tmp_path / "sa.json"
    service_account_path.write_text('{"type":"service_account","client_email":"agent@test","private_key":"abc"}', encoding="utf-8")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(service_account_path))
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_LOCATION", raising=False)

    secret, provider, auth_mode, model, execution_mode, metadata = _resolve_env_runtime_fallback(
        provider="gemini",
        model="gemini-2.5-pro",
        execution_mode=None,
        task=_task(payload={"type": "research"}),
        runtime_config={},
        resolve_execution_mode=lambda task, runtime_config, provider: "advisor",
    )

    assert secret is None
    assert provider == "gemini"
    assert auth_mode is None
    assert model == "gemini-2.5-pro"
    assert execution_mode is None
    assert metadata == {}


def test_classify_failure_uses_provider_specific_rate_limit_patterns():
    task = SimpleNamespace(
        context={"llm_provider": "openai", "llm_auth_mode": "api_key"},
        latest_status_message="Rate limit reached for gpt-5 in organization test",
        payloads=[{"type": "create"}],
    )

    assert classify_failure(task) == RATE_LIMIT
