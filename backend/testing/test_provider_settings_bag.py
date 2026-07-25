"""Tests for the provider-settings bag system: schemas, validation, merge,
CLI translation, and end-to-end resolution via resolve_task_provider_settings."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.models import LLMProviderName
from app.providers.base import merge_settings
from app.providers.registry import get_provider


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


def test_anthropic_schema_accepts_known_knobs():
    provider = get_provider(LLMProviderName.ANTHROPIC)
    validated = provider.validate_settings(
        {
            "model": "claude-sonnet-4-6",
            "effort": "high",
            "advisor": {"enabled": True, "model": "claude-opus-4-7"},
            "betas": ["advisor-tool-2026-03-01"],
        }
    )
    assert validated["model"] == "claude-sonnet-4-6"
    assert validated["effort"] == "high"
    assert validated["advisor"]["enabled"] is True
    assert validated["betas"] == ["advisor-tool-2026-03-01"]


def test_anthropic_schema_rejects_unknown_keys():
    provider = get_provider(LLMProviderName.ANTHROPIC)
    with pytest.raises(ValueError, match="unknown key"):
        provider.validate_settings({"bogus_key": 123})


def test_anthropic_schema_rejects_invalid_enum():
    provider = get_provider(LLMProviderName.ANTHROPIC)
    with pytest.raises(ValueError, match="not in allowed values"):
        provider.validate_settings({"effort": "absurd"})


def test_anthropic_cli_skips_same_tier_fallback_model():
    # Post-merge, defaults + effort matrix can produce a bag where
    # fallback_model equals model. That's a no-op for Claude Code CLI
    # (can't fall back to the rate-limited tier), so settings_to_cli
    # omits --fallback-model rather than emitting a useless flag.
    # Account/provider fallback already handle cross-model rerouting.
    provider = get_provider(LLMProviderName.ANTHROPIC)
    cli = provider.settings_to_cli(
        {
            "model": "claude-sonnet-4-6",
            "fallback_model": "claude-sonnet-4-6",
        }
    )
    assert "--model" in cli.args
    assert "--fallback-model" not in cli.args


def test_anthropic_cli_emits_fallback_model_when_different():
    provider = get_provider(LLMProviderName.ANTHROPIC)
    cli = provider.settings_to_cli(
        {
            "model": "claude-opus-4-7",
            "fallback_model": "claude-sonnet-4-6",
        }
    )
    assert "--fallback-model" in cli.args
    idx = cli.args.index("--fallback-model")
    assert cli.args[idx + 1] == "claude-sonnet-4-6"


def test_anthropic_max_bag_validates_and_translates():
    # The default max-effort cell uses opus-4-8 (fable-5 was retired after its
    # limited availability window). Exercise the exact bag end-to-end.
    provider = get_provider(LLMProviderName.ANTHROPIC)
    bag = provider.validate_settings(
        {
            "model": "claude-opus-4-8",
            "effort": "max",
        }
    )
    cli = provider.settings_to_cli(bag)
    args = list(cli.args)
    assert args[args.index("--model") + 1] == "claude-opus-4-8"
    assert args[args.index("--effort") + 1] == "max"
    assert not cli.unsupported


def test_anthropic_opus_1m_opt_in_appends_suffix():
    # 1M context opt-in on opus: suffix is appended since it's in _1M_CONTEXT_MODELS.
    provider = get_provider(LLMProviderName.ANTHROPIC)
    cli = provider.settings_to_cli(
        {
            "model": "claude-opus-4-8",
            "use_1m_context": True,
        }
    )
    args = list(cli.args)
    assert args[args.index("--model") + 1] == "claude-opus-4-8[1m]"
    assert not any("use_1m_context" in u for u in cli.unsupported)


def test_anthropic_cli_translation_advisor_opus48_executor_ignored():
    # Opus-4-8 executor — pairing an opus advisor is a no-op (same tier).
    provider = get_provider(LLMProviderName.ANTHROPIC)
    translation = provider.settings_to_cli(
        {
            "model": "claude-opus-4-8",
            "advisor": {"enabled": True, "model": "claude-opus-4-8"},
        }
    )
    assert any("advisor.enabled" in u for u in translation.unsupported)
    assert not translation.prompt_fragments


def test_openai_schema_accepts_known_knobs():
    provider = get_provider(LLMProviderName.OPENAI)
    validated = provider.validate_settings(
        {
            "model": "gpt-5.6-terra",
            "reasoning_effort": "max",
            "verbosity": "low",
            "service_tier": "flex",
        }
    )
    assert validated["reasoning_effort"] == "max"
    assert validated["verbosity"] == "low"


def test_openai_schema_rejects_retired_minimal_effort():
    # `minimal` left the schema with the 5.6 picker cycle — no current model
    # accepts it, so the enum rejects it up front instead of 400ing at dispatch.
    provider = get_provider(LLMProviderName.OPENAI)
    with pytest.raises(ValueError):
        provider.validate_settings({"reasoning_effort": "minimal"})


def test_openai_schema_rejects_unknown_model():
    provider = get_provider(LLMProviderName.OPENAI)
    with pytest.raises(ValueError):
        provider.validate_settings({"model": "gpt-99-nonexistent"})


def test_openai_schema_rejects_invalid_reasoning_effort():
    provider = get_provider(LLMProviderName.OPENAI)
    with pytest.raises(ValueError):
        provider.validate_settings({"reasoning_effort": "extreme"})


def test_gemini_schema_accepts_core_knobs():
    # Safety filters, temperature, top_p, approval_mode and sandbox were
    # removed from the user-facing schema: safety/sampling are rarely touched
    # and add noise; approval_mode/sandbox must always resolve to the fully-
    # permissive flag (hardcoded in settings_to_cli) because anything else
    # stalls the ephemeral agent VM waiting on an operator.
    provider = get_provider(LLMProviderName.GEMINI)
    validated = provider.validate_settings(
        {
            "model": "gemini-2.5-pro",
            "thinking_budget": 4096,
            "include_thoughts": False,
            "max_output_tokens": 8192,
        }
    )
    assert validated["thinking_budget"] == 4096
    assert validated["include_thoughts"] is False


def test_gemini_schema_rejects_removed_safety_key():
    provider = get_provider(LLMProviderName.GEMINI)
    with pytest.raises(ValueError, match="unknown key"):
        provider.validate_settings({"safety": {"harassment": "OFF"}})


def test_gemini_schema_rejects_removed_sampling_keys():
    provider = get_provider(LLMProviderName.GEMINI)
    with pytest.raises(ValueError, match="unknown key"):
        provider.validate_settings({"temperature": 0.5})
    with pytest.raises(ValueError, match="unknown key"):
        provider.validate_settings({"top_p": 0.9})


def test_schema_validator_accepts_null_values_for_optional_scalars():
    """Null is always allowed for scalar types (string/number/boolean/integer).

    Anthropic's schema uses this so e.g. `max_budget_usd` can round-trip as
    null from the UI to represent "unset" without raising. Required-field
    enforcement is separate (see test_schema_validator_enforces_required_keys).
    """
    from app.providers.base import _validate_against_schema

    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "effort": {"type": "string"},
            "max_budget_usd": {"type": "number"},
            "enabled": {"type": "boolean"},
            "count": {"type": "integer"},
            "betas": {"type": "array", "items": {"type": "string"}},
        },
    }
    assert _validate_against_schema(
        {
            "effort": None,
            "max_budget_usd": None,
            "enabled": None,
            "count": None,
            "betas": None,
        },
        schema,
        path="",
    ) == {
        "effort": None,
        "max_budget_usd": None,
        "enabled": None,
        "count": None,
        "betas": None,
    }


def test_schema_validator_rejects_wrong_type_on_scalars():
    """Even when null is allowed, other wrong types raise."""
    from app.providers.base import _validate_against_schema

    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"effort": {"type": "string"}, "count": {"type": "integer"}},
    }
    with pytest.raises(ValueError, match="expected string"):
        _validate_against_schema({"effort": 42}, schema, path="")
    with pytest.raises(ValueError, match="expected integer"):
        _validate_against_schema({"count": "many"}, schema, path="")


def test_schema_validator_rejects_object_where_scalar_expected():
    from app.providers.base import _validate_against_schema

    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"effort": {"type": "string"}},
    }
    with pytest.raises(ValueError, match="expected string"):
        _validate_against_schema({"effort": {"nested": "value"}}, schema, path="")


def test_manifest_omits_translation_when_settings_produces_empty_bag():
    """Empty bag {} should not emit LLM_SETTINGS or LLM_CLI_TRANSLATION env vars.

    Protects the old-backend→new-agent back-compat path: if settings are
    unconfigured, the agent falls back to legacy LLM_MODEL/LLM_EXECUTION_MODE.
    """
    import uuid as _uuid
    from app.compute import build_agent_secret_manifest

    manifest = build_agent_secret_manifest(
        vm_instance_id="vm-test",
        task_id=_uuid.uuid4(),
        oauth_token="sk-ant-oat01-test",
        task_type="review",
        provider="anthropic",
        auth_mode="oauth",
        model="claude-opus-4-7",
        provider_settings=None,
    )
    assert "LLM_SETTINGS" not in manifest["env"]
    assert "LLM_CLI_TRANSLATION" not in manifest["env"]
    # Legacy model env var still populated for back-compat.
    assert manifest["env"]["LLM_MODEL"] == "claude-opus-4-7"


def test_manifest_includes_translation_even_when_bag_is_empty_dict():
    """An empty dict is a valid bag (=no overrides, inherit all); the manifest
    should still emit LLM_SETTINGS='{}' so the agent knows the backend is
    speaking the new protocol and doesn't fall back to legacy-only mode.

    Since compute.py injects the caller-supplied `model` into the bag before
    settings_to_cli (so compatibility gates see a concrete model), the
    translation picks up `--model <model>` even when the input bag was
    empty. `LLM_SETTINGS` continues to reflect the input bag exactly so
    downstream logging/audit can tell what the operator wrote vs what
    compute synthesized."""
    import json
    import uuid as _uuid
    from app.compute import build_agent_secret_manifest

    manifest = build_agent_secret_manifest(
        vm_instance_id="vm-test",
        task_id=_uuid.uuid4(),
        oauth_token="sk-ant-oat01-test",
        task_type="review",
        provider="anthropic",
        auth_mode="oauth",
        model="claude-opus-4-7",
        provider_settings={},
    )
    # LLM_SETTINGS tracks the input bag verbatim — the model merge happens
    # only on the copy passed to settings_to_cli, so operators see the
    # distinction between "operator-set" and "compute-synthesized" values.
    assert manifest["env"]["LLM_SETTINGS"] == "{}"
    translation = json.loads(manifest["env"]["LLM_CLI_TRANSLATION"])
    # Hardcoded permission flag is always present; the model flag now also
    # lands because compute injects the effective model into the bag.
    assert translation["args"] == [
        "--model",
        "claude-opus-4-7",
        "--permission-mode",
        "bypassPermissions",
    ]


def test_resolve_settings_handles_missing_provider_block_in_defaults():
    """Provider with no entry in provider_settings_defaults falls through to
    task_type_provider_overrides and payload. Missing across all three layers
    returns empty dict, not None or error."""
    from types import SimpleNamespace
    from app.max_plan_pool import resolve_task_provider_settings

    # Provider absent from defaults
    runtime_config = {
        "provider_settings_defaults": {"anthropic": {"effort": "medium"}},
        "task_type_provider_overrides": {
            "review": {"openai": {"reasoning_effort": "high"}}
        },
    }
    task = SimpleNamespace(payloads=[{"type": "review"}], context={})
    assert resolve_task_provider_settings(task, runtime_config, "openai") == {
        "reasoning_effort": "high"
    }
    # Gemini absent from both layers → empty bag
    assert resolve_task_provider_settings(task, runtime_config, "gemini") == {}


def test_schema_validator_enforces_required_keys():
    """Schema `required: [...]` is enforced by _validate_against_schema.

    Providers don't use `required` yet, but this guards future schemas that do.
    """
    from app.providers.base import _validate_against_schema

    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["model"],
        "properties": {
            "model": {"type": "string"},
            "effort": {"type": "string"},
        },
    }
    _validate_against_schema({"model": "x", "effort": "low"}, schema, path="")
    with pytest.raises(ValueError, match="missing required keys: model"):
        _validate_against_schema({"effort": "low"}, schema, path="")


# ---------------------------------------------------------------------------
# Deep merge semantics
# ---------------------------------------------------------------------------


def test_merge_settings_later_wins_for_scalars():
    result = merge_settings(
        {"model": "opus", "effort": "low"},
        {"effort": "high"},
    )
    assert result == {"model": "opus", "effort": "high"}


def test_merge_settings_deep_merges_nested_objects():
    result = merge_settings(
        {"advisor": {"enabled": False, "model": "claude-opus-4-7"}},
        {"advisor": {"enabled": True}},
    )
    assert result["advisor"] == {"enabled": True, "model": "claude-opus-4-7"}


def test_merge_settings_replaces_arrays_wholesale():
    result = merge_settings(
        {"betas": ["a", "b"]},
        {"betas": ["c"]},
    )
    assert result["betas"] == ["c"]


def test_merge_settings_skips_none_and_empty():
    result = merge_settings(None, {"model": "opus"}, None, {})
    assert result == {"model": "opus"}


# ---------------------------------------------------------------------------
# CLI translation
# ---------------------------------------------------------------------------


def test_anthropic_cli_translation_emits_model_and_effort():
    provider = get_provider(LLMProviderName.ANTHROPIC)
    translation = provider.settings_to_cli(
        {"model": "claude-opus-4-7", "effort": "high"}
    )
    assert "--model" in translation.args
    assert "claude-opus-4-7" in translation.args
    assert "--effort" in translation.args
    assert "high" in translation.args


def test_anthropic_cli_translation_passes_max_effort_verbatim():
    provider = get_provider(LLMProviderName.ANTHROPIC)
    translation = provider.settings_to_cli({"effort": "max"})
    # Claude Code CLI accepts `max` as a real distinct effort level now
    # (Opus 4.7 supports it, older models auto-fall-back downward). Don't
    # normalize — passing verbatim preserves the admin's intent.
    args = list(translation.args)
    assert "--effort" in args
    assert args[args.index("--effort") + 1] == "max"


def test_anthropic_cli_translation_keeps_xhigh_distinct_from_max():
    """xhigh and max are distinct CLI levels, not aliases."""
    provider = get_provider(LLMProviderName.ANTHROPIC)
    translation = provider.settings_to_cli({"effort": "xhigh"})
    args = list(translation.args)
    assert args[args.index("--effort") + 1] == "xhigh"


def test_anthropic_cli_translation_advisor_opus_executor_ignored():
    provider = get_provider(LLMProviderName.ANTHROPIC)
    translation = provider.settings_to_cli(
        {
            "model": "claude-opus-4-7",
            "advisor": {"enabled": True, "model": "claude-opus-4-7"},
        }
    )
    # opus executor + opus advisor is a no-op; recorded as unsupported.
    assert any("advisor.enabled" in u for u in translation.unsupported)
    assert not translation.prompt_fragments


def test_anthropic_cli_translation_advisor_non_opus_emits_prompt_fragment():
    provider = get_provider(LLMProviderName.ANTHROPIC)
    translation = provider.settings_to_cli(
        {
            "model": "claude-sonnet-4-6",
            "advisor": {"enabled": True, "model": "claude-opus-4-7"},
        }
    )
    # Sonnet executor is a valid advisor-pairing target; CLI can't pass the
    # tool, so fall back to a prompt fragment + log as unsupported.
    assert translation.prompt_fragments
    assert any("advisor.enabled" in u for u in translation.unsupported)


def test_openai_cli_translation_emits_config_overrides():
    provider = get_provider(LLMProviderName.OPENAI)
    translation = provider.settings_to_cli(
        {
            "model": "gpt-5.4",
            "reasoning_effort": "high",
            "verbosity": "low",
            "service_tier": "flex",
        }
    )
    args = list(translation.args)
    assert args[:2] == ["-m", "gpt-5.4"]
    # Each -c flag is followed by key=value; the set of emitted pairs is what we assert.
    config_flags = [args[i + 1] for i, a in enumerate(args) if a == "-c"]
    assert 'model_reasoning_effort="high"' in config_flags
    assert 'model_verbosity="low"' in config_flags
    assert 'service_tier="flex"' in config_flags


def test_gemini_cli_translation_writes_settings_alias_for_thinking():
    provider = get_provider(LLMProviderName.GEMINI)
    translation = provider.settings_to_cli(
        {
            "model": "gemini-2.5-pro",
            "thinking_budget": 8192,
            "max_output_tokens": 4096,
        }
    )
    # Gemini CLI has no per-call thinking flags; must materialize a
    # settings.json custom alias and invoke `-m <alias>`.
    assert len(translation.settings_files) == 1
    settings_file = translation.settings_files[0]
    assert settings_file.path.endswith(".gemini/settings.json")
    parsed = json.loads(settings_file.content)
    alias = next(iter(parsed["modelConfigs"]["customAliases"].values()))
    assert alias["modelConfig"]["model"] == "gemini-2.5-pro"
    assert (
        alias["modelConfig"]["generateContentConfig"]["thinkingConfig"][
            "thinkingBudget"
        ]
        == 8192
    )
    assert alias["modelConfig"]["generateContentConfig"]["maxOutputTokens"] == 4096
    # Args should reference the alias, not the raw model.
    assert "-m" in translation.args
    m_index = list(translation.args).index("-m")
    assert translation.args[m_index + 1] != "gemini-2.5-pro"
    # Approval mode is hardcoded to yolo on every Gemini invocation.
    assert "--approval-mode=yolo" in translation.args
    # Trust workspace env var forces the directory-trust gate open so the
    # CLI doesn't override --approval-mode back to default on headless runs.
    assert translation.env.get("GEMINI_CLI_TRUST_WORKSPACE") == "true"


def test_gemini_cli_translation_model_only_uses_direct_flag():
    provider = get_provider(LLMProviderName.GEMINI)
    translation = provider.settings_to_cli({"model": "gemini-2.5-flash"})
    # No generation-config tunables → no settings.json, just -m <model>.
    # Approval-mode is always yolo — a stalled VM helps nobody.
    assert not translation.settings_files
    assert list(translation.args) == ["-m", "gemini-2.5-flash", "--approval-mode=yolo"]
    assert translation.env.get("GEMINI_CLI_TRUST_WORKSPACE") == "true"


# ---------------------------------------------------------------------------
# End-to-end: resolve_task_provider_settings
# ---------------------------------------------------------------------------


def _make_task(
    task_type: str,
    payload_settings: dict | None = None,
    context_settings: dict | None = None,
):
    payload = {"type": task_type}
    if payload_settings:
        payload["provider_settings"] = payload_settings
    ctx = {}
    if context_settings:
        ctx["provider_settings"] = context_settings
    return SimpleNamespace(payloads=[payload], context=ctx)


def test_resolve_task_provider_settings_three_layer_merge():
    from app.max_plan_pool import resolve_task_provider_settings

    runtime_config = {
        "provider_settings_defaults": {
            "anthropic": {"model": "claude-opus-4-7", "effort": "medium"},
        },
        "task_type_provider_overrides": {
            "review": {"anthropic": {"effort": "high"}},
        },
    }
    # Use max_budget_usd for the payload-layer demonstration; permission_mode
    # used to live here but was retired — it's now hardcoded in the CLI
    # translation so users can't accidentally stall an agent VM.
    task = _make_task("review", payload_settings={"anthropic": {"max_budget_usd": 5.0}})

    resolved = resolve_task_provider_settings(task, runtime_config, "anthropic")

    assert resolved["model"] == "claude-opus-4-7"  # from defaults
    assert resolved["effort"] == "high"  # task-type override wins
    assert resolved["max_budget_usd"] == 5.0  # payload wins


def test_resolve_task_provider_settings_payload_beats_task_type():
    from app.max_plan_pool import resolve_task_provider_settings

    runtime_config = {
        "provider_settings_defaults": {"anthropic": {"model": "claude-opus-4-7"}},
        "task_type_provider_overrides": {
            "review": {"anthropic": {"effort": "high"}},
        },
    }
    task = _make_task("review", payload_settings={"anthropic": {"effort": "low"}})

    resolved = resolve_task_provider_settings(task, runtime_config, "anthropic")
    assert resolved["effort"] == "low"


def test_resolve_task_provider_settings_validates_bag():
    from app.max_plan_pool import resolve_task_provider_settings

    runtime_config = {
        "provider_settings_defaults": {"anthropic": {"effort": "medium"}},
    }
    task = _make_task("review", payload_settings={"anthropic": {"bogus": 1}})

    with pytest.raises(ValueError, match="unknown key"):
        resolve_task_provider_settings(task, runtime_config, "anthropic")


def test_resolve_task_provider_settings_uses_declared_model_fallback():
    from app.max_plan_pool import resolve_task_provider_settings

    runtime_config = {
        "provider_settings_defaults": {
            "anthropic": {"model": "claude-fable-5", "effort": "max"},
        },
    }

    # The retired fable-5 id falls back to the current opus tier, so this
    # target moved opus-4-8 -> opus-5 with the Opus 5 rollout.
    assert resolve_task_provider_settings(
        _make_task("strategic_review"), runtime_config, "anthropic"
    ) == {"model": "claude-opus-5", "effort": "max"}


def test_resolve_task_provider_settings_fallback_keeps_other_errors_fail_closed():
    from app.max_plan_pool import resolve_task_provider_settings

    runtime_config = {
        "provider_settings_defaults": {
            "anthropic": {
                "model": "claude-fable-5",
                "effort": "max",
                "bogus": 1,
            },
        },
    }

    with pytest.raises(ValueError, match="unknown key"):
        resolve_task_provider_settings(
            _make_task("strategic_review"), runtime_config, "anthropic"
        )


def test_resolve_task_provider_settings_empty_when_nothing_configured():
    from app.max_plan_pool import resolve_task_provider_settings

    runtime_config = {}
    task = _make_task("review")
    assert resolve_task_provider_settings(task, runtime_config, "anthropic") == {}


def test_resolve_task_provider_settings_applies_effort_matrix():
    """The effort matrix provides per-(provider, level) bags that include
    model + the provider's effort knob. category_routing picks the level."""
    from app.max_plan_pool import resolve_task_provider_settings

    runtime_config = {
        "task_type_categories": {"strategic": ["strategic_review"]},
        "category_routing": {
            "_default": {"providers": ["anthropic"], "effort": "medium"},
            "strategic": {"providers": ["anthropic"], "effort": "max"},
        },
        "provider_settings_by_effort": {
            "anthropic": {
                "medium": {"model": "claude-sonnet-4-6", "effort": "medium"},
                "max": {"model": "claude-opus-4-7", "effort": "max"},
            },
        },
    }
    # strategic_review → "strategic" category → max cell → opus + effort=max
    assert resolve_task_provider_settings(
        _make_task("strategic_review"), runtime_config, "anthropic"
    ) == {"model": "claude-opus-4-7", "effort": "max"}
    # create → no category → _default medium → sonnet + effort=medium
    assert resolve_task_provider_settings(
        _make_task("create"), runtime_config, "anthropic"
    ) == {"model": "claude-sonnet-4-6", "effort": "medium"}


def test_resolve_task_provider_settings_payload_effort_overrides_category():
    """payload.effort wins over task_type_routing and category_routing."""
    from app.max_plan_pool import resolve_task_provider_settings

    runtime_config = {
        "task_type_categories": {"strategic": ["strategic_review"]},
        "category_routing": {
            "strategic": {"providers": ["anthropic"], "effort": "max"}
        },
        "provider_settings_by_effort": {
            "anthropic": {
                "low": {"model": "claude-haiku-4-5-20251001", "effort": "low"},
                "max": {"model": "claude-opus-4-7", "effort": "max"},
            },
        },
    }
    task = SimpleNamespace(
        payloads=[{"type": "strategic_review", "effort": "low"}],
        context={},
    )
    assert resolve_task_provider_settings(task, runtime_config, "anthropic") == {
        "model": "claude-haiku-4-5-20251001",
        "effort": "low",
    }


def test_resolve_task_provider_settings_surgical_override_beats_matrix():
    """task_type_provider_overrides wins over the matrix — surgical overrides
    are for cases that don't fit the per-effort shape."""
    from app.max_plan_pool import resolve_task_provider_settings

    runtime_config = {
        "task_type_categories": {"ops": ["docs"]},
        "category_routing": {"ops": {"providers": ["anthropic"], "effort": "medium"}},
        "provider_settings_by_effort": {
            "anthropic": {"medium": {"model": "claude-sonnet-4-6", "effort": "medium"}},
        },
        "task_type_provider_overrides": {
            "docs": {"anthropic": {"model": "claude-haiku-4-5-20251001"}},
        },
    }
    # Effort comes from matrix; model comes from surgical override.
    assert resolve_task_provider_settings(
        _make_task("docs"), runtime_config, "anthropic"
    ) == {"model": "claude-haiku-4-5-20251001", "effort": "medium"}


def test_resolve_task_routing_layers():
    """Most-specific wins: payload > context > task_type_routing >
    category_routing > category_routing[_default] > ALL_PROVIDERS."""
    from app.max_plan_pool import resolve_task_routing

    runtime_config = {
        "task_type_categories": {"build": ["create", "refactor"]},
        "category_routing": {
            "_default": {"providers": ["anthropic"], "effort": "medium"},
            "build": {"providers": ["anthropic", "openai"], "effort": "high"},
        },
        "task_type_routing": {"debug": {"providers": ["openai"], "effort": "max"}},
    }
    # No category, no override → _default
    assert resolve_task_routing(_make_task("qa"), runtime_config) == {
        "providers": ["anthropic"],
        "effort": "medium",
    }
    # In "build" category → category routing wins
    assert resolve_task_routing(_make_task("create"), runtime_config) == {
        "providers": ["anthropic", "openai"],
        "effort": "high",
    }
    # task_type_routing override wins over category
    assert resolve_task_routing(_make_task("debug"), runtime_config) == {
        "providers": ["openai"],
        "effort": "max",
    }
    # payload.effort wins
    task = SimpleNamespace(
        payloads=[{"type": "create", "effort": "low"}],
        context={},
    )
    assert resolve_task_routing(task, runtime_config) == {
        "providers": ["anthropic", "openai"],
        "effort": "low",
    }


def test_resolve_task_routing_partial_override_inherits_other_field():
    """A task_type_routing entry with only `effort` inherits providers from
    the category, and vice versa."""
    from app.max_plan_pool import resolve_task_routing

    runtime_config = {
        "task_type_categories": {"ops": ["docs"]},
        "category_routing": {
            "ops": {"providers": ["anthropic", "gemini"], "effort": "medium"}
        },
        "task_type_routing": {
            "docs": {"effort": "low"},  # only override effort
            "smoketest": {"providers": ["gemini"]},  # only override providers
        },
    }
    # docs: providers inherited from ops category, effort overridden
    assert resolve_task_routing(_make_task("docs"), runtime_config) == {
        "providers": ["anthropic", "gemini"],
        "effort": "low",
    }
    # smoketest: effort inherited from _default (ops doesn't contain smoketest), providers overridden
    # No _default in this config; falls through to ALL_PROVIDERS effort=medium
    result = resolve_task_routing(_make_task("smoketest"), runtime_config)
    assert result["providers"] == ["gemini"]
    assert result["effort"] == "medium"


def test_resolve_task_routing_applies_adopt_verdict():
    """When provider_routing_verdict_state=ADOPT, DeepSeek is moved to first
    position for initiative/build/ops categories."""
    from app.max_plan_pool import resolve_task_routing

    runtime_config = {
        "task_type_categories": {"initiative": ["plan"], "build": ["create"]},
        "category_routing": {
            "initiative": {"providers": ["anthropic", "deepseek", "openai"]},
            "build": {"providers": ["openai", "deepseek"]},
        },
        "provider_routing_verdict_state": "ADOPT",
    }
    # initiative: deepseek moves from pos 1 to pos 0
    assert resolve_task_routing(_make_task("plan"), runtime_config)["providers"] == [
        "deepseek", "anthropic", "openai",
    ]
    # build: deepseek moves from pos 1 to pos 0
    assert resolve_task_routing(_make_task("create"), runtime_config)["providers"] == [
        "deepseek", "openai",
    ]


def test_resolve_task_routing_applies_reject_verdict():
    """When provider_routing_verdict_state=REJECT, Anthropic is moved to first
    position for initiative/build/ops categories."""
    from app.max_plan_pool import resolve_task_routing

    runtime_config = {
        "task_type_categories": {"build": ["create"], "ops": ["docs"]},
        "category_routing": {
            "build": {"providers": ["deepseek", "openai", "anthropic"]},
            "ops": {"providers": ["deepseek", "gemini"]},
        },
        "provider_routing_verdict_state": "REJECT",
    }
    # build: anthropic moves from pos 2 to pos 0
    assert resolve_task_routing(_make_task("create"), runtime_config)["providers"] == [
        "anthropic", "deepseek", "openai",
    ]
    # ops: anthropic (not in list) gets prepended
    assert resolve_task_routing(_make_task("docs"), runtime_config)["providers"] == [
        "anthropic", "deepseek", "gemini",
    ]


def test_resolve_task_routing_verdict_insufficient_data_unchanged():
    """INSUFFICIENT_DATA leaves static config order unchanged."""
    from app.max_plan_pool import resolve_task_routing

    runtime_config = {
        "task_type_categories": {"build": ["create"]},
        "category_routing": {
            "build": {"providers": ["anthropic", "deepseek", "openai"]},
        },
        "provider_routing_verdict_state": "INSUFFICIENT_DATA",
    }
    assert resolve_task_routing(_make_task("create"), runtime_config)["providers"] == [
        "anthropic", "deepseek", "openai",
    ]


def test_resolve_task_routing_verdict_skips_unaffected_categories():
    """Strategic and review categories are NOT affected by the verdict."""
    from app.max_plan_pool import resolve_task_routing

    runtime_config = {
        "task_type_categories": {
            "strategic": ["strategic_review"],
            "review": ["review"],
        },
        "category_routing": {
            "strategic": {"providers": ["zai", "anthropic", "deepseek"]},
            "review": {"providers": ["anthropic", "deepseek"]},
        },
        "provider_routing_verdict_state": "ADOPT",
    }
    # strategic: max effort, verdict doesn't apply — zai stays first
    assert resolve_task_routing(
        _make_task("strategic_review"), runtime_config
    )["providers"] == ["zai", "anthropic", "deepseek"]
    # review: high effort, verdict doesn't apply — anthropic stays first
    assert resolve_task_routing(
        _make_task("review"), runtime_config
    )["providers"] == ["anthropic", "deepseek"]


def test_resolve_task_routing_no_verdict_key_unchanged():
    """Absent provider_routing_verdict_state key = no reordering."""
    from app.max_plan_pool import resolve_task_routing

    runtime_config = {
        "task_type_categories": {"build": ["create"]},
        "category_routing": {
            "build": {"providers": ["anthropic", "deepseek"]},
        },
    }
    assert resolve_task_routing(_make_task("create"), runtime_config)["providers"] == [
        "anthropic", "deepseek",
    ]


# ---------------------------------------------------------------------------
# build_agent_secret_manifest integration
# ---------------------------------------------------------------------------


def test_manifest_serializes_settings_bag_and_cli_translation():
    import uuid as _uuid
    from app.compute import build_agent_secret_manifest

    manifest = build_agent_secret_manifest(
        vm_instance_id="vm-test",
        task_id=_uuid.uuid4(),
        oauth_token="sk-ant-oat01-test",
        task_type="review",
        provider="anthropic",
        auth_mode="oauth",
        model="claude-opus-4-7",
        execution_mode="default",
        provider_settings={"model": "claude-opus-4-7", "effort": "high"},
    )
    env = manifest["env"]
    parsed_settings = json.loads(env["LLM_SETTINGS"])
    assert parsed_settings == {"model": "claude-opus-4-7", "effort": "high"}
    assert "LLM_CLI_TRANSLATION" in env
    translation = json.loads(env["LLM_CLI_TRANSLATION"])
    assert "--effort" in translation["args"]
    assert "high" in translation["args"]
    assert manifest["metadata"]["provider_settings"] == {
        "model": "claude-opus-4-7",
        "effort": "high",
    }


def test_manifest_gemini_materializes_settings_file():
    import uuid as _uuid
    from app.compute import build_agent_secret_manifest

    manifest = build_agent_secret_manifest(
        vm_instance_id="vm-test",
        task_id=_uuid.uuid4(),
        task_type="research",
        provider="gemini",
        auth_mode="api_key",
        model="gemini-2.5-pro",
        execution_mode="default",
        provider_settings={"model": "gemini-2.5-pro", "thinking_budget": 4096},
    )
    paths = [f["path"] for f in manifest["files"]]
    assert any(p.endswith(".gemini/settings.json") for p in paths)


def test_manifest_without_settings_omits_llm_settings_env():
    import uuid as _uuid
    from app.compute import build_agent_secret_manifest

    manifest = build_agent_secret_manifest(
        vm_instance_id="vm-test",
        task_id=_uuid.uuid4(),
        oauth_token="sk-ant-oat01-test",
        task_type="review",
        provider="anthropic",
        auth_mode="oauth",
        model="claude-opus-4-7",
        execution_mode="default",
    )
    assert "LLM_SETTINGS" not in manifest["env"]
    assert "LLM_CLI_TRANSLATION" not in manifest["env"]
