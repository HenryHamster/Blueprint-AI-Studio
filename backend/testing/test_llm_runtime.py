import json
import uuid
from types import SimpleNamespace

import pytest

from app.failure_classification import RATE_LIMIT, classify_failure
from app.max_plan_pool import (
    EXACT_MODEL_ROUTE_CONTEXT_KEY,
    _account_by_id,
    _max_plan_pools,
    apply_task_llm_runtime_context,
    clear_task_llm_runtime_context,
    reconcile_llm_runtime_identity,
    resolve_task_routing,
    select_llm_runtime_for_task,
)
from app.llm_cost_attribution import compute_llm_cost_breakdown
from app.models import TaskStatus
from app.task_blocked import reset_task_for_fresh_attempt
from app.orchestrator import (
    _resolve_env_runtime_fallback,
    _sync_task_payload_runtime_fields,
)
from testing.factories import make_task


def _translation_args(env: dict) -> list[str]:
    raw = env.get("LLM_CLI_TRANSLATION")
    assert raw, "manifest must include a pre-translated CLI settings blob"
    return json.loads(raw)["args"]


_TEST_USER = "runtime-user"


def test_reconcile_runtime_identity_makes_account_and_provider_authoritative():
    runtime_config = {
        "provider_tier_model_map": {
            "anthropic": {"sonnet": "claude-sonnet-4-6"},
            "openai": {"sonnet": "gpt-5.6-terra"},
        }
    }

    identity = reconcile_llm_runtime_identity(
        account={"id": "openai-1", "provider": "openai", "auth_mode": "oauth"},
        account_id="stale-id",
        provider="anthropic",
        auth_mode="api_key",
        model="claude-sonnet-4-6",
        quality_tier="sonnet",
        runtime_config=runtime_config,
    )

    assert identity.account_id == "openai-1"
    assert identity.provider == "openai"
    assert identity.auth_mode == "oauth"
    assert identity.model == "gpt-5.6-terra"


def test_fresh_attempt_restores_requested_model_and_clears_runtime_identity():
    task = make_task(
        payloads=[{"type": "create", "model": "claude-sonnet-4-6"}],
        context={
            "llm_account_id": "anthropic-1",
            "llm_provider": "anthropic",
            "llm_auth_mode": "oauth",
            "llm_model": "claude-sonnet-4-6",
            "runner_target_id": "runner-dead-1",
            "runner_hostname": "dead-runner.local",
            "forced_runner_target_id": "operator-pin-1",
            "_requested_payload_model": "gpt-5.6-sol",
            "_resolved_payload_model": "claude-sonnet-4-6",
            "agent_error_report": {"exit_code": 1},
        },
    )
    task.llm_account_id = "anthropic-1"

    reset_task_for_fresh_attempt(task)

    assert task.first_payload["model"] == "gpt-5.6-sol"
    assert task.llm_account_id is None
    assert "llm_provider" not in task.context
    assert "llm_model" not in task.context
    assert "runner_target_id" not in task.context
    assert "runner_hostname" not in task.context
    assert task.context["forced_runner_target_id"] == "operator-pin-1"
    assert "agent_error_report" not in task.context
    assert task.context.get("_fresh_attempt_started_at")


def test_fresh_attempt_preserves_legacy_explicit_model_without_provenance():
    task = make_task(
        payloads=[{"type": "create", "model": "gpt-5.5"}],
        context={
            "llm_provider": "openai",
            "llm_model": "gpt-5.5",
        },
    )

    reset_task_for_fresh_attempt(task)

    assert task.first_payload["model"] == "gpt-5.5"


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
async def test_select_llm_runtime_for_task_uses_explicit_model_provider(
    clean_runtime_pool,
):
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
            "anthropic": {
                "opus": "claude-opus-4-7",
                "sonnet": "claude-sonnet-4-6",
                "haiku": "claude-haiku-4-5-20251001",
            },
            "openai": {
                "opus": "gpt-5.4",
                "sonnet": "gpt-5.3-codex",
                "haiku": "gpt-5.4-mini",
            },
            "gemini": {
                "opus": "gemini-2.5-pro",
                "sonnet": "gemini-2.5-flash",
                "haiku": "gemini-2.5-flash-lite",
            },
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
async def test_exact_route_preserves_provider_model_and_effort_without_fallback(
    clean_runtime_pool,
):
    ga_only = {
        "id": "acct-gemini-ga-only",
        "user_id": _TEST_USER,
        "name": "gemini ga only",
        "provider": "gemini",
        "auth_mode": "api_key",
        "runtime_secret": "AIzaTestKeyForBlueprint",
        "runtime_ready": True,
        "enabled": True,
        "active_tasks": 0,
        "provider_metadata": {"available_models": ["gemini-2.5-pro"]},
    }
    alternate = {
        "id": "acct-deepseek",
        "user_id": _TEST_USER,
        "name": "deepseek",
        "provider": "deepseek",
        "auth_mode": "api_key",
        "runtime_secret": "deepseek-test-key",
        "runtime_ready": True,
        "enabled": True,
        "active_tasks": 0,
    }
    _max_plan_pools.setdefault(_TEST_USER, []).extend([ga_only, alternate])
    _account_by_id[ga_only["id"]] = ga_only
    _account_by_id[alternate["id"]] = alternate

    task = _task(
        payload={
            "type": "create",
            "model": "gemini-3.1-pro-preview",
            "effort": "high",
            "provider_preference": ["gemini"],
        },
        context={
            EXACT_MODEL_ROUTE_CONTEXT_KEY: {
                "provider": "gemini",
                "model": "gemini-3.1-pro-preview",
                "effort": "high",
            }
        },
    )
    runtime_config = {
        "provider_selection_mode": "balanced",
        "provider_routing_verdict_state": "ADOPT",
        "task_type_categories": {"build": ["create"]},
        "category_routing": {
            "build": {
                "providers": ["deepseek", "gemini"],
                "effort": "low",
            }
        },
    }

    assert resolve_task_routing(task, runtime_config) == {
        "providers": ["gemini"],
        "effort": "high",
    }
    result = await select_llm_runtime_for_task(
        user_id=_TEST_USER,
        task=task,
        runtime_config=runtime_config,
    )

    assert result is not None
    assert result.provider == "gemini"
    assert result.model == "gemini-3.1-pro-preview"
    assert result.account is None
    assert "routing_fallback" not in task.context
    assert "model_fallback" not in task.context


@pytest.mark.asyncio
async def test_select_llm_runtime_for_task_uses_quality_tier_mapping(
    clean_runtime_pool,
):
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
            "anthropic": {
                "opus": "claude-opus-4-7",
                "sonnet": "claude-sonnet-4-6",
                "haiku": "claude-haiku-4-5-20251001",
            },
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
async def test_select_llm_runtime_for_task_resolves_settings_bag_per_task_type(
    clean_runtime_pool,
):
    """Task-type override in the settings bag surfaces on the resolved
    LLMSelectionResult. Replaces the old execution_mode test since modes
    were retired in favor of schema-driven per-provider settings."""
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
            "anthropic": {
                "opus": "claude-opus-4-7",
                "sonnet": "claude-sonnet-4-6",
                "haiku": "claude-haiku-4-5-20251001",
            },
        },
        "task_type_provider_preference": {"research": ["anthropic"]},
        "provider_settings_defaults": {"anthropic": {"effort": "medium"}},
        "task_type_provider_overrides": {"research": {"anthropic": {"effort": "high"}}},
    }

    result = await select_llm_runtime_for_task(
        user_id=_TEST_USER,
        task=_task(payload={"type": "research", "quality_tier": "sonnet"}),
        runtime_config=runtime_config,
    )

    assert result is not None
    assert result.settings == {"effort": "high"}


@pytest.mark.asyncio
async def test_select_llm_runtime_keeps_smoketest_gpt_5_6_pin_in_cli_translation(
    clean_runtime_pool,
):
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
            "anthropic": {
                "opus": "claude-opus-4-7",
                "sonnet": "claude-sonnet-4-6",
                "haiku": "claude-haiku-4-5-20251001",
            },
            "openai": {
                "opus": "gpt-5.6-sol",
                "sonnet": "gpt-5.6-terra",
                "haiku": "gpt-5.6-luna",
            },
        },
        "task_type_routing": {
            "smoketest": {"providers": ["anthropic", "openai"], "effort": "low"}
        },
        "task_type_model_preferences": {
            "smoketest": [
                {"provider": "openai", "model": "gpt-5.6-sol"},
                {"provider": "anthropic", "model": "claude-opus-4-7"},
            ],
        },
        # The concrete preference must beat this low-effort default all the way
        # through CLI translation, not only in LLMSelectionResult.model.
        "provider_settings_by_effort": {
            "openai": {
                "low": {
                    "model": "gpt-5.6-luna",
                    "reasoning_effort": "low",
                    "verbosity": "low",
                }
            }
        },
    }

    result = await select_llm_runtime_for_task(
        user_id=_TEST_USER,
        task=_task(payload={"type": "smoketest", "quality_tier": "opus"}),
        runtime_config=runtime_config,
    )

    assert result is not None
    assert result.provider == "openai"
    assert result.model == "gpt-5.6-sol"
    assert result.account_id == "acct-openai"
    assert result.settings["model"] == "gpt-5.6-sol"
    assert result.settings["reasoning_effort"] == "low"

    from app.compute_manifest import build_agent_secret_manifest

    manifest = build_agent_secret_manifest(
        vm_instance_id="agent-vm-gpt-5-6-smoke",
        task_id=uuid.uuid4(),
        oauth_token="sk-test",
        provider=result.provider,
        auth_mode=result.auth_mode,
        model=result.model,
        provider_settings=result.settings,
    )
    assert manifest["env"]["LLM_MODEL"] == "gpt-5.6-sol"
    assert json.loads(manifest["env"]["LLM_SETTINGS"])["model"] == "gpt-5.6-sol"
    args = _translation_args(manifest["env"])
    assert args[args.index("-m") + 1] == "gpt-5.6-sol"


@pytest.mark.asyncio
async def test_select_llm_runtime_for_task_respects_disabled_cross_provider_fallback(
    clean_runtime_pool,
):
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
            "anthropic": {
                "opus": "claude-opus-4-7",
                "sonnet": "claude-sonnet-4-6",
                "haiku": "claude-haiku-4-5-20251001",
            },
            "openai": {
                "opus": "gpt-5.4",
                "sonnet": "gpt-5.3-codex",
                "haiku": "gpt-5.4-mini",
            },
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
async def test_select_llm_runtime_for_task_keeps_explicit_provider_when_fallback_disabled(
    clean_runtime_pool,
):
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
            "anthropic": {
                "opus": "claude-opus-4-7",
                "sonnet": "claude-sonnet-4-6",
                "haiku": "claude-haiku-4-5-20251001",
            },
            "openai": {
                "opus": "gpt-5.4",
                "sonnet": "gpt-5.3-codex",
                "haiku": "gpt-5.4-mini",
            },
        },
        "task_type_provider_preference": {"create": ["anthropic", "openai"]},
    }

    result = await select_llm_runtime_for_task(
        user_id=_TEST_USER,
        task=_task(payload={"type": "create", "model": "gpt-5.4"}),
        runtime_config=runtime_config,
    )

    assert result is not None
    assert result.provider == "openai"
    assert result.model == "gpt-5.4"
    assert result.account is None


@pytest.mark.asyncio
async def test_select_llm_runtime_for_task_preserves_unknown_explicit_model(
    clean_runtime_pool,
):
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
            "anthropic": {
                "opus": "claude-opus-4-7",
                "sonnet": "claude-sonnet-4-6",
                "haiku": "claude-haiku-4-5-20251001",
            },
            "openai": {
                "opus": "gpt-5.4",
                "sonnet": "gpt-5.3-codex",
                "haiku": "gpt-5.4-mini",
            },
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


@pytest.mark.asyncio
async def test_select_llm_runtime_balanced_mode_picks_less_loaded_provider(
    clean_runtime_pool,
):
    """Balanced mode flattens accounts across listed providers and picks by
    (health_score, -active_tasks). With zero-history accounts the health score
    ties, so the provider with fewer active tasks should win — no bias by
    list order."""
    busy_anthropic = {
        "id": "acct-anthropic",
        "user_id": _TEST_USER,
        "name": "anthropic",
        "provider": "anthropic",
        "auth_mode": "oauth",
        "oauth_token": "sk-ant-test",
        "runtime_ready": True,
        "enabled": True,
        "active_tasks": 3,
    }
    idle_openai = {
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
    _max_plan_pools.setdefault(_TEST_USER, []).extend([busy_anthropic, idle_openai])
    _account_by_id[busy_anthropic["id"]] = busy_anthropic
    _account_by_id[idle_openai["id"]] = idle_openai

    runtime_config = {
        "provider_selection_mode": "balanced",
        "provider_tier_model_map": {
            "anthropic": {
                "opus": "claude-opus-4-7",
                "sonnet": "claude-sonnet-4-6",
                "haiku": "claude-haiku-4-5-20251001",
            },
            "openai": {
                "opus": "gpt-5.4",
                "sonnet": "gpt-5.3-codex",
                "haiku": "gpt-5.4-mini",
            },
        },
        # Anthropic listed first — balanced must NOT honor list order when load differs.
        "task_type_provider_preference": {"create": ["anthropic", "openai"]},
    }

    result = await select_llm_runtime_for_task(
        user_id=_TEST_USER,
        task=_task(payload={"type": "create", "quality_tier": "opus"}),
        runtime_config=runtime_config,
    )

    assert result is not None
    assert result.provider == "openai"
    assert result.account_id == "acct-openai"
    assert result.model == "gpt-5.4"


@pytest.mark.asyncio
async def test_select_llm_runtime_preferred_mode_honors_list_order(clean_runtime_pool):
    """Preferred mode tries providers in list order. First listed wins even
    when it's busier, unless it's fully exhausted."""
    busy_anthropic = {
        "id": "acct-anthropic",
        "user_id": _TEST_USER,
        "name": "anthropic",
        "provider": "anthropic",
        "auth_mode": "oauth",
        "oauth_token": "sk-ant-test",
        "runtime_ready": True,
        "enabled": True,
        "active_tasks": 3,
    }
    idle_openai = {
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
    _max_plan_pools.setdefault(_TEST_USER, []).extend([busy_anthropic, idle_openai])
    _account_by_id[busy_anthropic["id"]] = busy_anthropic
    _account_by_id[idle_openai["id"]] = idle_openai

    runtime_config = {
        "provider_selection_mode": "preferred",
        "provider_tier_model_map": {
            "anthropic": {
                "opus": "claude-opus-4-7",
                "sonnet": "claude-sonnet-4-6",
                "haiku": "claude-haiku-4-5-20251001",
            },
            "openai": {
                "opus": "gpt-5.4",
                "sonnet": "gpt-5.3-codex",
                "haiku": "gpt-5.4-mini",
            },
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
    assert result.account_id == "acct-anthropic"


@pytest.mark.asyncio
async def test_resolve_provider_preference_falls_through_category_to_default(
    clean_runtime_pool,
):
    """Resolution layers (most specific wins): per-task ctx → per-type →
    category → _default → ALL_PROVIDERS. Verify the category tier by
    configuring just a category override."""
    from app.max_plan_pool import resolve_task_provider_preference

    runtime_config = {
        "task_type_categories": {"build": ["create", "refactor"], "review": ["fix"]},
        "provider_preference_by_category": {
            "build": ["anthropic"],
            "_default": ["anthropic", "openai", "gemini"],
        },
        "task_type_provider_preference": {},
    }

    # "create" is in the "build" category → anthropic only.
    assert resolve_task_provider_preference(
        _task(payload={"type": "create"}), runtime_config
    ) == ["anthropic"]

    # "fix" is in the "review" category, no category-level override → _default.
    assert resolve_task_provider_preference(
        _task(payload={"type": "fix"}), runtime_config
    ) == ["anthropic", "openai", "gemini"]

    # Unknown task type → no category → _default.
    assert resolve_task_provider_preference(
        _task(payload={"type": "novel_type"}), runtime_config
    ) == ["anthropic", "openai", "gemini"]


@pytest.mark.asyncio
async def test_resolve_provider_preference_per_task_override_wins_over_category(
    clean_runtime_pool,
):
    """A per-task-type entry in task_type_provider_preference beats the
    category-level default even when the category also has a value."""
    from app.max_plan_pool import resolve_task_provider_preference

    runtime_config = {
        "task_type_categories": {"build": ["create", "refactor"]},
        "provider_preference_by_category": {
            "build": ["anthropic"],
            "_default": ["anthropic", "openai", "gemini"],
        },
        # Per-task narrowing for "create" specifically.
        "task_type_provider_preference": {"create": ["openai", "anthropic"]},
    }

    # create → per-task override wins.
    assert resolve_task_provider_preference(
        _task(payload={"type": "create"}), runtime_config
    ) == ["openai", "anthropic"]
    # refactor → still inherits category since there's no per-task entry.
    assert resolve_task_provider_preference(
        _task(payload={"type": "refactor"}), runtime_config
    ) == ["anthropic"]


@pytest.mark.asyncio
async def test_select_llm_runtime_balanced_mode_honors_narrowed_allow_list(
    clean_runtime_pool,
):
    """Narrowing task_type_provider_preference to one provider must keep that
    provider even under balanced mode — the mode controls how to pick among
    listed providers, not which providers are listed."""
    anthropic = {
        "id": "acct-anthropic",
        "user_id": _TEST_USER,
        "name": "anthropic",
        "provider": "anthropic",
        "auth_mode": "oauth",
        "oauth_token": "sk-ant-test",
        "runtime_ready": True,
        "enabled": True,
        "active_tasks": 5,
    }
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
    _max_plan_pools.setdefault(_TEST_USER, []).extend([anthropic, openai])
    _account_by_id[anthropic["id"]] = anthropic
    _account_by_id[openai["id"]] = openai

    runtime_config = {
        "provider_selection_mode": "balanced",
        "provider_tier_model_map": {
            "anthropic": {
                "opus": "claude-opus-4-7",
                "sonnet": "claude-sonnet-4-6",
                "haiku": "claude-haiku-4-5-20251001",
            },
            "openai": {
                "opus": "gpt-5.4",
                "sonnet": "gpt-5.3-codex",
                "haiku": "gpt-5.4-mini",
            },
        },
        # research pinned to anthropic only.
        "task_type_provider_preference": {"research": ["anthropic"]},
    }

    result = await select_llm_runtime_for_task(
        user_id=_TEST_USER,
        task=_task(payload={"type": "research", "quality_tier": "opus"}),
        runtime_config=runtime_config,
    )

    assert result is not None
    assert result.provider == "anthropic"
    assert result.account_id == "acct-anthropic"


@pytest.mark.asyncio
async def test_memory_consolidation_provider_pin_cannot_fall_through_to_another_pool(
    clean_runtime_pool,
):
    """The verified text-only worker is Anthropic-only until another provider
    has the same isolation contract.  A backend-owned task pin must remain a
    complete allow-list even when global routing would prefer another account.
    """
    anthropic = {
        "id": "acct-anthropic",
        "user_id": _TEST_USER,
        "name": "anthropic",
        "provider": "anthropic",
        "auth_mode": "oauth",
        "oauth_token": "sk-ant-test",
        "runtime_ready": True,
        "enabled": True,
        "active_tasks": 9,
    }
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
    _max_plan_pools.setdefault(_TEST_USER, []).extend([anthropic, openai])
    _account_by_id[anthropic["id"]] = anthropic
    _account_by_id[openai["id"]] = openai

    runtime_config = {
        "provider_selection_mode": "balanced",
        "provider_routing_verdict_state": "ADOPT",
        "task_type_categories": {"build": ["memory_consolidation"]},
        "provider_tier_model_map": {
            "anthropic": {"sonnet": "claude-sonnet-4-6"},
            "openai": {"sonnet": "gpt-5.6-terra"},
            "deepseek": {"sonnet": "deepseek-chat"},
        },
        # Deliberately hostile global routing: a scheduled memory task must
        # still never be assigned this more-idle OpenAI account.
        "task_type_routing": {
            "memory_consolidation": {"providers": ["openai"], "effort": "medium"}
        },
    }

    task = _task(
        payload={"type": "memory_consolidation"},
        context={"provider_preference": ["anthropic"]},
    )
    assert resolve_task_routing(task, runtime_config)["providers"] == ["anthropic"]

    result = await select_llm_runtime_for_task(
        user_id=_TEST_USER,
        task=task,
        runtime_config=runtime_config,
    )

    assert result is not None
    assert result.provider == "anthropic"
    assert result.account_id == "acct-anthropic"


@pytest.mark.asyncio
async def test_select_llm_runtime_legacy_fallback_false_maps_to_exclusive(
    clean_runtime_pool,
):
    """The legacy cross_provider_fallback_enabled=False boolean should behave
    as exclusive mode — first listed provider only, no cross-provider pickup."""
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
        # No provider_selection_mode set — legacy deployment.
        "cross_provider_fallback_enabled": False,
        "provider_tier_model_map": {
            "anthropic": {
                "opus": "claude-opus-4-7",
                "sonnet": "claude-sonnet-4-6",
                "haiku": "claude-haiku-4-5-20251001",
            },
            "openai": {
                "opus": "gpt-5.4",
                "sonnet": "gpt-5.3-codex",
                "haiku": "gpt-5.4-mini",
            },
        },
        "task_type_provider_preference": {"create": ["anthropic", "openai"]},
    }

    result = await select_llm_runtime_for_task(
        user_id=_TEST_USER,
        task=_task(payload={"type": "create", "quality_tier": "opus"}),
        runtime_config=runtime_config,
    )

    # Anthropic (first listed) is empty, openai is ignored because exclusive
    # mode trims the preference to [anthropic]. Falls through to the synthetic
    # stub result with no account.
    assert result is not None
    assert result.provider == "anthropic"
    assert result.account is None


def test_clear_task_llm_runtime_context_strips_all_runtime_keys():
    cleared = clear_task_llm_runtime_context(
        {
            "llm_account_id": "acct-1",
            "llm_provider": "anthropic",
            "llm_auth_mode": "oauth",
            "llm_model": "claude-opus-4-7",
            "max_plan_account_id": "acct-1",
            "scope": "small",
        }
    )

    assert "llm_account_id" not in cleared
    assert "llm_provider" not in cleared
    assert "llm_auth_mode" not in cleared
    assert "llm_model" not in cleared
    assert "max_plan_account_id" not in cleared


def test_clear_task_llm_runtime_context_strips_runner_target_id():
    """runner_target_id must be cleared on requeue so a dead runner is not
    re-pinned forever — the next dispatch cycle falls over to GCP instead."""
    cleared = clear_task_llm_runtime_context(
        {
            "runner_target_id": "some-runner-uuid",
            "provision_failures": 1,
            "scope": "small",
        }
    )

    assert "runner_target_id" not in cleared
    # Non-runtime keys are preserved.
    assert cleared.get("provision_failures") == 1
    assert cleared.get("scope") == "small"


def test_sync_task_payload_runtime_fields_overwrites_stale_values():
    task = SimpleNamespace(
        payloads=[
            {"type": "create", "model": "claude-opus-4-7", "execution_mode": "default"}
        ]
    )

    changed = _sync_task_payload_runtime_fields(
        task,
        model="gpt-5.4",
        execution_mode="advisor",
    )

    assert changed is True
    assert task.payloads[0]["model"] == "gpt-5.4"
    assert task.payloads[0]["execution_mode"] == "advisor"


def test_manifest_cli_translation_uses_tier_mapped_model():
    from app.compute_manifest import build_agent_secret_manifest

    task = _task(payload={"type": "create", "quality_tier": "opus"})
    runtime_config = {
        "provider_tier_model_map": {
            "openai": {
                "opus": "gpt-5.4",
                "sonnet": "gpt-5.3-codex",
                "haiku": "gpt-5.4-mini",
            }
        }
    }
    changed = _sync_task_payload_runtime_fields(
        task,
        model="gpt-5.4",
        execution_mode=None,
    )

    manifest = build_agent_secret_manifest(
        vm_instance_id="agent-vm-runtime-tier",
        task_id=uuid.uuid4(),
        oauth_token="sk-test",
        provider="openai",
        auth_mode="api_key",
        model=task.payloads[0]["model"],
        provider_settings={},
    )

    assert changed is True
    assert task.payloads[0]["model"] == "gpt-5.4"
    assert manifest["env"]["LLM_MODEL"] == "gpt-5.4"
    args = _translation_args(manifest["env"])
    assert args[args.index("-m") + 1] == "gpt-5.4"
    assert runtime_config["provider_tier_model_map"]["openai"]["opus"] == "gpt-5.4"


def test_manifest_cli_translation_uses_explicit_payload_model():
    from app.compute_manifest import build_agent_secret_manifest

    task = _task(payload={"type": "create", "model": "gpt-5-custom-preview"})
    changed = _sync_task_payload_runtime_fields(
        task,
        model="gpt-5-custom-preview",
        execution_mode=None,
    )

    manifest = build_agent_secret_manifest(
        vm_instance_id="agent-vm-runtime-explicit",
        task_id=uuid.uuid4(),
        oauth_token="sk-test",
        provider="openai",
        auth_mode="api_key",
        model=task.payloads[0]["model"],
        provider_settings={"reasoning_effort": "high"},
    )

    assert changed is False
    assert task.payloads[0]["model"] == "gpt-5-custom-preview"
    assert manifest["env"]["LLM_MODEL"] == "gpt-5-custom-preview"
    args = _translation_args(manifest["env"])
    assert args[args.index("-m") + 1] == "gpt-5-custom-preview"


@pytest.mark.asyncio
async def test_selection_payload_and_manifest_agree_for_effort_settings_model(
    clean_runtime_pool,
):
    from app.compute_manifest import build_agent_secret_manifest

    account = {
        "id": "acct-openai-effort",
        "user_id": _TEST_USER,
        "name": "openai effort",
        "provider": "openai",
        "auth_mode": "api_key",
        "runtime_secret": "sk-test",
        "runtime_ready": True,
        "enabled": True,
        "active_tasks": 0,
    }
    _max_plan_pools.setdefault(_TEST_USER, []).append(account)
    _account_by_id[account["id"]] = account

    runtime_config = {
        "provider_selection_mode": "preferred",
        "task_type_provider_preference": {"create": ["openai"]},
        "provider_settings_by_effort": {
            "openai": {
                "medium": {
                    "model": "gpt-5.4-mini",
                    "reasoning_effort": "medium",
                }
            }
        },
    }
    task = _task(payload={"type": "create"})
    selection = await select_llm_runtime_for_task(
        user_id=_TEST_USER,
        task=task,
        runtime_config=runtime_config,
    )
    assert selection is not None
    changed = _sync_task_payload_runtime_fields(
        task,
        model=selection.model,
        execution_mode=selection.execution_mode,
    )

    manifest = build_agent_secret_manifest(
        vm_instance_id="agent-vm-runtime-effort",
        task_id=uuid.uuid4(),
        oauth_token=account["runtime_secret"],
        provider=selection.provider,
        auth_mode=selection.auth_mode,
        model=selection.model,
        provider_settings=selection.settings,
    )

    assert changed is True
    assert task.payloads[0]["model"] == "gpt-5.4-mini"
    assert manifest["env"]["LLM_MODEL"] == "gpt-5.4-mini"
    assert json.loads(manifest["env"]["LLM_SETTINGS"])["model"] == "gpt-5.4-mini"
    args = _translation_args(manifest["env"])
    assert args[args.index("-m") + 1] == "gpt-5.4-mini"


@pytest.mark.asyncio
async def test_selection_payload_and_manifest_agree_for_model_fallback(
    clean_runtime_pool,
):
    from app.compute_manifest import build_agent_secret_manifest

    account = {
        "id": "acct-gemini-ga",
        "user_id": _TEST_USER,
        "name": "gemini ga",
        "provider": "gemini",
        "auth_mode": "api_key",
        "runtime_secret": "AIzaTestKeyForBlueprint",
        "runtime_ready": True,
        "enabled": True,
        "active_tasks": 0,
        "provider_metadata": {
            "available_models": ["gemini-2.5-pro"],
        },
    }
    _max_plan_pools.setdefault(_TEST_USER, []).append(account)
    _account_by_id[account["id"]] = account

    runtime_config = {
        "provider_selection_mode": "preferred",
        "task_type_provider_preference": {"create": ["gemini"]},
    }
    task = _task(payload={"type": "create", "model": "gemini-3.1-pro-preview"})
    selection = await select_llm_runtime_for_task(
        user_id=_TEST_USER,
        task=task,
        runtime_config=runtime_config,
    )
    assert selection is not None
    changed = _sync_task_payload_runtime_fields(
        task,
        model=selection.model,
        execution_mode=selection.execution_mode,
    )

    manifest = build_agent_secret_manifest(
        vm_instance_id="agent-vm-runtime-fallback",
        task_id=uuid.uuid4(),
        oauth_token=account["runtime_secret"],
        provider=selection.provider,
        auth_mode=selection.auth_mode,
        model=selection.model,
        provider_settings=selection.settings,
    )

    assert changed is True
    assert selection.model == "gemini-2.5-pro"
    assert task.payloads[0]["model"] == "gemini-2.5-pro"
    assert manifest["env"]["LLM_MODEL"] == "gemini-2.5-pro"
    args = _translation_args(manifest["env"])
    assert args[args.index("-m") + 1] == "gemini-2.5-pro"


@pytest.mark.asyncio
async def test_no_account_deepseek_ops_env_fallback_uses_settings_model(
    clean_runtime_pool, monkeypatch
):
    from app.compute_manifest import build_agent_secret_manifest

    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-env-key")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    runtime_config = {
        "provider_selection_mode": "preferred",
        "provider_tier_model_map": {
            "deepseek": {
                "opus": "deepseek-v4-pro",
                "sonnet": "deepseek-v4-pro",
                "haiku": "deepseek-v4-flash",
            }
        },
        "task_type_categories": {"ops": ["ops"]},
        "category_routing": {
            "ops": {"providers": ["deepseek", "anthropic"], "effort": "low"}
        },
        "provider_settings_by_effort": {
            "deepseek": {
                "low": {"model": "deepseek-v4-flash", "effort": "low"},
                "max": {"model": "deepseek-v4-pro", "effort": "max"},
            }
        },
    }
    task = _task(payload={"type": "ops"})

    selection = await select_llm_runtime_for_task(
        user_id=_TEST_USER,
        task=task,
        runtime_config=runtime_config,
    )

    assert selection is not None
    assert selection.account is None
    assert selection.provider == "deepseek"
    assert selection.auth_mode == "api_key"
    assert selection.model == "deepseek-v4-flash"
    assert selection.settings == {"model": "deepseek-v4-flash", "effort": "low"}

    secret, provider, auth_mode, model, execution_mode, metadata = (
        _resolve_env_runtime_fallback(
            provider=selection.provider,
            model=selection.model,
            execution_mode=selection.execution_mode,
            task=task,
            runtime_config=runtime_config,
        )
    )
    task.context = apply_task_llm_runtime_context(
        task.context,
        account_id=None,
        provider=provider,
        auth_mode=auth_mode,
        model=model,
        execution_mode=execution_mode,
    )
    changed = _sync_task_payload_runtime_fields(
        task,
        model=model,
        execution_mode=execution_mode,
    )
    manifest = build_agent_secret_manifest(
        vm_instance_id="agent-vm-deepseek-env",
        task_id=uuid.uuid4(),
        oauth_token=secret,
        provider=provider,
        auth_mode=auth_mode,
        model=model,
        provider_metadata=metadata,
        provider_settings=selection.settings,
    )

    assert changed is True
    assert secret == "deepseek-env-key"
    assert task.context["llm_model"] == "deepseek-v4-flash"
    assert task.payloads[0]["model"] == "deepseek-v4-flash"
    assert manifest["env"]["LLM_MODEL"] == "deepseek-v4-flash"
    assert json.loads(manifest["env"]["LLM_SETTINGS"])["model"] == (
        "deepseek-v4-flash"
    )
    args = _translation_args(manifest["env"])
    assert args[args.index("--model") + 1] == "deepseek-v4-flash"

    cost_task = make_task(
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        payloads=[{"type": "ops", "model": model}],
        provider=provider,
        model=model,
        api_call_summary={
            "llm_call_count": 1,
            "total_input_tokens": 1000,
            "total_output_tokens": 500,
        },
    )
    breakdown = compute_llm_cost_breakdown([cost_task])
    by_model = {(row["provider"], row["model"]): row for row in breakdown["by_model"]}
    assert ("deepseek", "deepseek-v4-flash") in by_model
    assert ("deepseek", "deepseek-v4-pro") not in by_model
    assert breakdown["totals"]["total_cost_usd"] > 0


def test_resolve_env_runtime_fallback_prefers_openai_env(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-openai")

    secret, provider, auth_mode, model, execution_mode, metadata = (
        _resolve_env_runtime_fallback(
            provider="openai",
            model="gpt-5.4",
            execution_mode=None,
            task=_task(payload={"type": "create"}),
            runtime_config={},
        )
    )

    assert secret == "sk-test-openai"
    assert provider == "openai"
    assert auth_mode == "api_key"
    assert model == "gpt-5.4"
    # execution_mode is retired — the fallback round-trips whatever the caller
    # passed (None here) without resolving anything from runtime_config.
    assert execution_mode is None
    assert metadata == {}


def test_resolve_env_runtime_fallback_reads_openai_oauth_from_codex_home(
    tmp_path, monkeypatch
):
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    auth_file = codex_home / "auth.json"
    auth_file.write_text('{"access_token":"test"}', encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    secret, provider, auth_mode, model, execution_mode, metadata = (
        _resolve_env_runtime_fallback(
            provider="openai",
            model="gpt-5.4",
            execution_mode=None,
            task=_task(payload={"type": "create"}),
            runtime_config={},
        )
    )

    assert secret == '{"access_token":"test"}'
    assert provider == "openai"
    assert auth_mode == "oauth"
    assert model == "gpt-5.4"
    assert execution_mode is None
    assert metadata == {}


def test_resolve_env_runtime_fallback_reads_zai_anthropic_compat_env(monkeypatch):
    monkeypatch.delenv("ZAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "zai-compat-token")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.z.ai/api/anthropic/")

    secret, provider, auth_mode, model, execution_mode, metadata = (
        _resolve_env_runtime_fallback(
            provider="zai",
            model=None,
            execution_mode=None,
            task=_task(payload={"type": "create"}),
            runtime_config={},
        )
    )

    assert secret == "zai-compat-token"
    assert provider == "zai"
    assert auth_mode == "api_key"
    assert model == "glm-5.2"
    assert execution_mode is None
    assert metadata == {"anthropic_base_url": "https://api.z.ai/api/anthropic/"}


def test_resolve_env_runtime_fallback_rejects_zai_generic_anthropic_key_without_base_url(
    monkeypatch,
):
    for env_name in (
        "ZAI_API_KEY",
        "ZAI_ANTHROPIC_BASE_URL",
        "ZAI_BASE_URL",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
    ):
        monkeypatch.delenv(env_name, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "wrong-provider-key")

    secret, provider, auth_mode, model, execution_mode, metadata = (
        _resolve_env_runtime_fallback(
            provider="zai",
            model=None,
            execution_mode=None,
            task=_task(payload={"type": "create"}),
            runtime_config={},
        )
    )

    assert secret is None
    assert provider == "zai"
    assert auth_mode is None
    assert model == "glm-5.2"
    assert execution_mode is None
    assert metadata == {}


def test_resolve_env_runtime_fallback_reads_deepseek_anthropic_compat_env(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "deepseek-compat-token")
    monkeypatch.setenv(
        "DEEPSEEK_ANTHROPIC_BASE_URL", "https://api.deepseek.com/anthropic/"
    )

    secret, provider, auth_mode, model, execution_mode, metadata = (
        _resolve_env_runtime_fallback(
            provider="deepseek",
            model=None,
            execution_mode=None,
            task=_task(payload={"type": "ops"}),
            runtime_config={},
        )
    )

    assert secret == "deepseek-compat-token"
    assert provider == "deepseek"
    assert auth_mode == "api_key"
    assert model == "deepseek-v4-flash"
    assert execution_mode is None
    assert metadata == {"anthropic_base_url": "https://api.deepseek.com/anthropic/"}


def test_resolve_env_runtime_fallback_rejects_deepseek_generic_anthropic_key_without_base_url(
    monkeypatch,
):
    for env_name in (
        "DEEPSEEK_API_KEY",
        "DEEPSEEK_ANTHROPIC_BASE_URL",
        "DEEPSEEK_BASE_URL",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
    ):
        monkeypatch.delenv(env_name, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "wrong-provider-key")

    secret, provider, auth_mode, model, execution_mode, metadata = (
        _resolve_env_runtime_fallback(
            provider="deepseek",
            model=None,
            execution_mode=None,
            task=_task(payload={"type": "ops"}),
            runtime_config={},
        )
    )

    assert secret is None
    assert provider == "deepseek"
    assert auth_mode is None
    assert model == "deepseek-v4-flash"
    assert execution_mode is None
    assert metadata == {}


def test_resolve_env_runtime_fallback_reads_gemini_service_account(
    tmp_path, monkeypatch
):
    service_account_path = tmp_path / "sa.json"
    service_account_path.write_text(
        '{"type":"service_account","client_email":"agent@test","private_key":"abc"}',
        encoding="utf-8",
    )
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(service_account_path))
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "demo-project")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")

    secret, provider, auth_mode, model, execution_mode, metadata = (
        _resolve_env_runtime_fallback(
            provider="gemini",
            model="gemini-2.5-pro",
            execution_mode=None,
            task=_task(payload={"type": "research"}),
            runtime_config={},
        )
    )

    assert '"type":"service_account"' in secret
    assert provider == "gemini"
    assert auth_mode == "service_account"
    assert model == "gemini-2.5-pro"
    assert execution_mode is None
    assert metadata == {"project": "demo-project", "location": "us-central1"}


def test_resolve_env_runtime_fallback_rejects_gemini_service_account_without_project_location(
    tmp_path, monkeypatch
):
    service_account_path = tmp_path / "sa.json"
    service_account_path.write_text(
        '{"type":"service_account","client_email":"agent@test","private_key":"abc"}',
        encoding="utf-8",
    )
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(service_account_path))
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_LOCATION", raising=False)

    secret, provider, auth_mode, model, execution_mode, metadata = (
        _resolve_env_runtime_fallback(
            provider="gemini",
            model="gemini-2.5-pro",
            execution_mode=None,
            task=_task(payload={"type": "research"}),
            runtime_config={},
        )
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


def test_runtime_fallback_seam_no_circular_import_and_identical_symbols():
    """Seam guard: the extracted module is importable standalone (no circular
    import back through app.orchestrator) and orchestrator re-exports the exact
    same function/table objects so the dispatch loop resolves fallbacks
    identically."""
    import app.orchestrator as orch
    import app.orchestrator_runtime_env as env

    assert orch._resolve_env_runtime_fallback is env._resolve_env_runtime_fallback
    assert orch._provider_env_credential_hint is env._provider_env_credential_hint
    assert orch._ENV_FALLBACK_DEFAULT_MODELS is env._ENV_FALLBACK_DEFAULT_MODELS
    assert callable(orch._sync_task_payload_runtime_fields)
    assert env._ENV_FALLBACK_DEFAULT_MODELS == {
        "anthropic": "claude-opus-5",
        "openai": "gpt-5.4",
        "gemini": "gemini-2.5-pro",
        "zai": "glm-5.2",
        "deepseek": "deepseek-v4-flash",
    }


@pytest.mark.asyncio
async def test_select_llm_runtime_records_cross_provider_fallback(clean_runtime_pool):
    """When the most-preferred provider has no usable account and selection
    lands on a later-preference provider, the divergence is stamped on
    task.context.routing_fallback (best-effort, doesn't change the choice)."""
    idle_openai = {
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
    _max_plan_pools.setdefault(_TEST_USER, []).append(idle_openai)
    _account_by_id[idle_openai["id"]] = idle_openai

    runtime_config = {
        "provider_selection_mode": "preferred",
        "provider_tier_model_map": {
            "anthropic": {
                "opus": "claude-opus-4-7",
                "sonnet": "claude-sonnet-4-6",
                "haiku": "claude-haiku-4-5-20251001",
            },
            "openai": {
                "opus": "gpt-5.4",
                "sonnet": "gpt-5.3-codex",
                "haiku": "gpt-5.4-mini",
            },
        },
        "task_type_provider_preference": {"create": ["anthropic", "openai"]},
    }

    task = _task(payload={"type": "create", "quality_tier": "opus"})
    result = await select_llm_runtime_for_task(
        user_id=_TEST_USER, task=task, runtime_config=runtime_config
    )

    assert result is not None
    assert result.provider == "openai"
    assert result.account_id == "acct-openai"
    marker = task.context["routing_fallback"]
    assert marker["reason"] == "cross_provider"
    assert marker["intended_provider"] == "anthropic"
    assert marker["effective_provider"] == "openai"


@pytest.mark.asyncio
async def test_select_llm_runtime_records_no_account_fallback(clean_runtime_pool):
    """When no account can be reserved at all, the synthetic result is stamped
    with a no_account routing_fallback so the empty-pool dispatch is visible."""
    runtime_config = {
        "provider_selection_mode": "exclusive",
        "provider_tier_model_map": {
            "anthropic": {
                "opus": "claude-opus-4-7",
                "sonnet": "claude-sonnet-4-6",
                "haiku": "claude-haiku-4-5-20251001",
            },
        },
        "task_type_provider_preference": {"create": ["anthropic"]},
    }

    task = _task(payload={"type": "create", "quality_tier": "opus"})
    result = await select_llm_runtime_for_task(
        user_id=_TEST_USER, task=task, runtime_config=runtime_config
    )

    assert result is not None
    assert result.account is None
    marker = task.context["routing_fallback"]
    assert marker["reason"] == "no_account"
    assert marker["effective_provider"] == "anthropic"


@pytest.mark.asyncio
async def test_no_account_fallback_keeps_explicit_model_in_cli_translation(
    clean_runtime_pool,
):
    runtime_config = {
        "provider_selection_mode": "exclusive",
        "provider_tier_model_map": {
            "openai": {
                "opus": "gpt-5.6-sol",
                "sonnet": "gpt-5.6-terra",
                "haiku": "gpt-5.6-luna",
            },
        },
        "task_type_routing": {
            "create": {"providers": ["openai"], "effort": "low"}
        },
        "provider_settings_by_effort": {
            "openai": {
                "low": {
                    "model": "gpt-5.6-luna",
                    "reasoning_effort": "low",
                }
            }
        },
    }

    result = await select_llm_runtime_for_task(
        user_id=_TEST_USER,
        task=_task(
            payload={
                "type": "create",
                "quality_tier": "haiku",
                "model": "gpt-5-custom-preview",
            }
        ),
        runtime_config=runtime_config,
    )

    assert result is not None
    assert result.provider == "openai"
    assert result.account is None
    assert result.model == "gpt-5-custom-preview"
    assert result.settings["model"] == "gpt-5-custom-preview"

    from app.compute_manifest import build_agent_secret_manifest

    manifest = build_agent_secret_manifest(
        vm_instance_id="agent-vm-no-account-custom-model",
        task_id=uuid.uuid4(),
        oauth_token="oauth-test",
        provider=result.provider,
        auth_mode=result.auth_mode,
        model=result.model,
        provider_settings=result.settings,
    )
    args = _translation_args(manifest["env"])
    assert args[args.index("-m") + 1] == "gpt-5-custom-preview"


@pytest.mark.asyncio
async def test_select_llm_runtime_populates_failure_reasons(clean_runtime_pool):
    """A no-account outcome fills the caller's failure_reasons with the
    per-(provider, model) WHY — e.g. "in rate-limit cooldown" — instead of
    leaving the caller with a bare "pool exhausted" bit. The reason is computed
    either way; discarding it is what turns a 30-second fix into an hour of
    guessing whether the pool is rate-limited or the task is poisoned."""
    from datetime import timedelta

    from app.time_utils import _utcnow

    cooled = {
        "id": "acct-cooled",
        "user_id": _TEST_USER,
        "name": "cooled",
        "provider": "anthropic",
        "auth_mode": "oauth",
        "oauth_token": "sk-ant-test",
        "runtime_ready": True,
        "enabled": True,
        "active_tasks": 0,
        "disabled_models": {
            "claude-opus-4-7": {"hits": 3, "at": _utcnow() - timedelta(seconds=10)}
        },
    }
    _max_plan_pools.setdefault(_TEST_USER, []).append(cooled)
    _account_by_id[cooled["id"]] = cooled

    runtime_config = {
        "provider_selection_mode": "exclusive",
        "provider_tier_model_map": {
            "anthropic": {
                "opus": "claude-opus-4-7",
                "sonnet": "claude-sonnet-4-6",
                "haiku": "claude-haiku-4-5-20251001",
            },
        },
        "task_type_provider_preference": {"create": ["anthropic"]},
    }

    task = _task(payload={"type": "create", "quality_tier": "opus"})
    failure_reasons: list[str] = []
    result = await select_llm_runtime_for_task(
        user_id=_TEST_USER,
        task=task,
        runtime_config=runtime_config,
        failure_reasons=failure_reasons,
    )

    assert result is None or result.account is None
    assert failure_reasons, "a no-account outcome must surface WHY"
    joined = " | ".join(failure_reasons)
    assert "cooled" in joined  # names the account...
    assert "cooldown" in joined  # ...and the actual cause


@pytest.mark.asyncio
async def test_no_reason_strings_built_when_caller_did_not_ask(
    clean_runtime_pool, monkeypatch
):
    """No reason string is CONSTRUCTED when the caller passes no failure_reasons.

    The per-account reasons are built inside select_max_plan_account{,_balanced}
    while they hold the global _max_plan_pool_lock, and _account_unavailable_reason
    does real work (an in-function import, datetime arithmetic, f-string builds).
    Python evaluates call arguments EAGERLY, so a `_reject(a, _account_unavailable_reason(...))`
    with the None-check only inside `_reject` would run that work for every
    rejected account on EVERY LLM selection system-wide — cloud dispatch included
    — under the hottest lock in the system, with the cost peaking during exactly
    the cooldown storm the reasons exist to explain.

    This pins the guard: same pool, same no-account outcome, reasons requested or
    not — and _account_unavailable_reason must not be called in the "not
    requested" case.
    """
    from datetime import timedelta

    from app.time_utils import _utcnow

    cooled = {
        "id": "acct-cooled-lazy",
        "user_id": _TEST_USER,
        "name": "cooled-lazy",
        "provider": "anthropic",
        "auth_mode": "oauth",
        "oauth_token": "sk-ant-test",
        "runtime_ready": True,
        "enabled": True,
        "active_tasks": 0,
        "disabled_models": {
            "claude-opus-4-7": {"hits": 3, "at": _utcnow() - timedelta(seconds=10)}
        },
    }
    _max_plan_pools.setdefault(_TEST_USER, []).append(cooled)
    _account_by_id[cooled["id"]] = cooled

    runtime_config = {
        "provider_selection_mode": "exclusive",
        "provider_tier_model_map": {
            "anthropic": {
                "opus": "claude-opus-4-7",
                "sonnet": "claude-sonnet-4-6",
                "haiku": "claude-haiku-4-5-20251001",
            },
        },
        "task_type_provider_preference": {"create": ["anthropic"]},
    }
    task = _task(payload={"type": "create", "quality_tier": "opus"})

    import app.max_plan_pool as mpp

    real_reason = mpp._account_unavailable_reason
    calls: list[str] = []

    def _counting_reason(acct, model, now=None):
        calls.append(acct.get("id"))
        return real_reason(acct, model, now)

    monkeypatch.setattr(mpp, "_account_unavailable_reason", _counting_reason)

    # (a) Caller does NOT want reasons — the hot path must build none.
    result = await select_llm_runtime_for_task(
        user_id=_TEST_USER,
        task=task,
        runtime_config=runtime_config,
        # failure_reasons deliberately omitted — this is the cloud-dispatch
        # shape, i.e. the overwhelming majority of selections.
    )
    assert result is None or result.account is None  # same no-account outcome
    assert calls == [], (
        "reason strings were built under the global pool lock for a caller that "
        f"never asked for them (built for: {calls})"
    )

    # (b) Caller DOES want reasons — the same rejection now explains itself, so
    #     the guard suppresses the work without suppressing the diagnostic.
    failure_reasons: list[str] = []
    await select_llm_runtime_for_task(
        user_id=_TEST_USER,
        task=task,
        runtime_config=runtime_config,
        failure_reasons=failure_reasons,
    )
    assert calls == [cooled["id"]], "the WHY must still be computed on request"
    assert any("cooldown" in r for r in failure_reasons)


@pytest.mark.asyncio
async def test_select_llm_runtime_no_fallback_marker_on_intended_provider(
    clean_runtime_pool,
):
    """The intended provider having a usable account leaves context clean — no
    routing_fallback marker so the cost-analytics caveat stays uncluttered."""
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
        "provider_selection_mode": "preferred",
        "provider_tier_model_map": {
            "anthropic": {
                "opus": "claude-opus-4-7",
                "sonnet": "claude-sonnet-4-6",
                "haiku": "claude-haiku-4-5-20251001",
            },
            "openai": {
                "opus": "gpt-5.4",
                "sonnet": "gpt-5.3-codex",
                "haiku": "gpt-5.4-mini",
            },
        },
        "task_type_provider_preference": {"create": ["anthropic", "openai"]},
    }

    task = _task(payload={"type": "create", "quality_tier": "opus"})
    result = await select_llm_runtime_for_task(
        user_id=_TEST_USER, task=task, runtime_config=runtime_config
    )

    assert result is not None
    assert result.provider == "anthropic"
    assert "routing_fallback" not in task.context


# ── Cross-provider model reconciliation (EXTERNAL_API mispair fix) ───────────
# A task pinned to provider A's model can be dispatched to provider B when A is
# unavailable; if the model id isn't re-derived for B, the runtime is handed a
# foreign model (e.g. provider=anthropic + model=deepseek-v4-pro) and the CLI
# rejects it → EXTERNAL_API failure → retries burned → task FAILED. These test
# reconcile_model_for_provider, the pure guard the orchestrator dispatch applies.

from app.max_plan_pool import (  # noqa: E402
    infer_provider_for_model,
    infer_quality_tier_for_model,
    reconcile_model_for_provider,
)
from app.providers.registry import get_provider  # noqa: E402


def test_reconcile_corrects_cross_provider_mispair_opus():
    """provider=anthropic but model=deepseek-v4-pro (deepseek's opus tier) →
    re-derived to anthropic's opus-tier model, preserving the requested tier."""
    rc = {}
    # Precondition: the foreign model really does belong to a different provider.
    assert infer_provider_for_model("deepseek-v4-pro", rc) == "deepseek"
    result = reconcile_model_for_provider("anthropic", "deepseek-v4-pro", "opus", rc)
    assert result != "deepseek-v4-pro"
    assert infer_provider_for_model(result, rc) == "anthropic"
    assert infer_quality_tier_for_model("anthropic", result, rc) == "opus"


def test_reconcile_preserves_tier_on_haiku_mispair():
    """The re-derived model matches the resolved quality tier, not just any model
    for the provider (haiku pin → haiku on the new provider)."""
    rc = {}
    assert infer_provider_for_model("deepseek-v4-flash", rc) == "deepseek"
    result = reconcile_model_for_provider(
        "anthropic", "deepseek-v4-flash", "haiku", rc
    )
    assert infer_provider_for_model(result, rc) == "anthropic"
    assert infer_quality_tier_for_model("anthropic", result, rc) == "haiku"


def test_reconcile_leaves_consistent_pair_untouched():
    """When the model already belongs to the resolved provider, return it as-is
    (no needless re-derivation)."""
    rc = {}
    assert (
        reconcile_model_for_provider("deepseek", "deepseek-v4-pro", "opus", rc)
        == "deepseek-v4-pro"
    )
    anthropic_opus = get_provider("anthropic").tier_to_model("opus", rc)
    assert (
        reconcile_model_for_provider("anthropic", anthropic_opus, "opus", rc)
        == anthropic_opus
    )


def test_reconcile_never_clobbers_unknown_model():
    """An operator-custom model id that infers to no provider is left untouched —
    the guard only corrects models that demonstrably belong to a DIFFERENT known
    provider, never an explicit unknown one."""
    rc = {}
    assert infer_provider_for_model("acme-custom-model-9000", rc) is None
    assert (
        reconcile_model_for_provider(
            "anthropic", "acme-custom-model-9000", "opus", rc
        )
        == "acme-custom-model-9000"
    )


def test_reconcile_handles_missing_inputs():
    """No provider / no model → return the model unchanged (defensive no-op)."""
    rc = {}
    assert reconcile_model_for_provider(None, "deepseek-v4-pro", "opus", rc) == (
        "deepseek-v4-pro"
    )
    assert reconcile_model_for_provider("anthropic", None, "opus", rc) is None


def test_reconcile_defaults_tier_when_none():
    """A None quality_tier falls back to DEFAULT_QUALITY_TIER and still yields a
    provider-valid model (no crash, no foreign id)."""
    rc = {}
    result = reconcile_model_for_provider("anthropic", "deepseek-v4-pro", None, rc)
    assert infer_provider_for_model(result, rc) == "anthropic"
