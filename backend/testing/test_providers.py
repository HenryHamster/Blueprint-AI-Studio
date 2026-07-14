import json

import pytest

from app.models import LLMAuthMode, LLMProviderName
from app.providers.registry import get_provider, list_providers


def test_registry_lists_expected_providers():
    names = {provider.name for provider in list_providers()}
    assert names == {
        LLMProviderName.ANTHROPIC,
        LLMProviderName.OPENAI,
        LLMProviderName.GEMINI,
        LLMProviderName.ZAI,
        LLMProviderName.DEEPSEEK,
    }


def test_anthropic_provider_validates_oauth_token_and_exposes_rate_limit_patterns():
    provider = get_provider(LLMProviderName.ANTHROPIC)
    result = provider.validate_secret("sk-ant-oat01-valid_token", LLMAuthMode.OAUTH, {})
    assert result.secret == "sk-ant-oat01-valid_token"
    assert "you've hit your usage limit" in provider.rate_limit_patterns(
        LLMAuthMode.OAUTH
    )


def test_openai_provider_validates_api_key():
    provider = get_provider(LLMProviderName.OPENAI)
    result = provider.validate_secret("sk-proj-test_token", LLMAuthMode.API_KEY, {})
    assert result.secret == "sk-proj-test_token"


def test_openai_provider_oauth_materializes_auth_file():
    provider = get_provider(LLMProviderName.OPENAI)
    material = provider.secret_material(
        '{"tokens":{"access_token":"abc"}}', LLMAuthMode.OAUTH, {}
    )

    assert material.env["CODEX_HOME"] == "/opt/agent/openai"
    assert material.files[0].path == "/opt/agent/openai/auth.json"
    assert material.files[0].mode == "0600"


def test_gemini_service_account_requires_metadata_and_service_account_json():
    provider = get_provider(LLMProviderName.GEMINI)

    with pytest.raises(
        ValueError, match="Missing provider metadata fields: project, location"
    ):
        provider.validate_secret(
            '{"type":"service_account","client_email":"bot@example.com","private_key":"abc"}',
            LLMAuthMode.SERVICE_ACCOUNT,
            {},
        )

    with pytest.raises(
        ValueError,
        match="Service account JSON missing fields: client_email, private_key",
    ):
        provider.validate_secret(
            '{"type":"service_account"}',
            LLMAuthMode.SERVICE_ACCOUNT,
            {"project": "demo", "location": "us-central1"},
        )

    ok = provider.validate_secret(
        '{"type":"service_account","client_email":"bot@example.com","private_key":"abc"}',
        LLMAuthMode.SERVICE_ACCOUNT,
        {"project": "demo", "location": "us-central1"},
    )
    assert ok.provider_metadata == {"project": "demo", "location": "us-central1"}


def test_gemini_service_account_materializes_credentials_file():
    provider = get_provider(LLMProviderName.GEMINI)
    material = provider.secret_material(
        '{"type":"service_account","client_email":"bot@example.com","private_key":"abc"}',
        LLMAuthMode.SERVICE_ACCOUNT,
        {"project": "demo", "location": "us-central1"},
    )

    assert material.env["GOOGLE_APPLICATION_CREDENTIALS"] == "/opt/agent/gemini/sa.json"
    assert material.env["GOOGLE_CLOUD_PROJECT"] == "demo"
    assert material.env["GOOGLE_CLOUD_LOCATION"] == "us-central1"
    # GOOGLE_GENAI_USE_VERTEXAI is a Google GenAI SDK flag, not a Gemini CLI
    # flag — Gemini CLI picks the Vertex path from standard GCP auth env
    # vars. Regression guard: never set it, even if historical code did.
    assert "GOOGLE_GENAI_USE_VERTEXAI" not in material.env
    assert material.files[0].path == "/opt/agent/gemini/sa.json"
    # Trust gate opt-out — ephemeral VM is the isolation boundary.
    assert material.env["GEMINI_CLI_TRUST_WORKSPACE"] == "true"


def test_gemini_api_key_secret_material_includes_trust_workspace():
    provider = get_provider(LLMProviderName.GEMINI)
    material = provider.secret_material("my-api-key", LLMAuthMode.API_KEY)
    assert material.env["GEMINI_API_KEY"] == "my-api-key"
    # Trust gate opt-out — ephemeral VM is the isolation boundary.
    assert material.env["GEMINI_CLI_TRUST_WORKSPACE"] == "true"


def test_zai_provider_uses_claude_compat_secret_material():
    provider = get_provider(LLMProviderName.ZAI)
    result = provider.validate_secret("zai-test-key", LLMAuthMode.API_KEY, {})
    assert result.secret == "zai-test-key"

    material = provider.secret_material("zai-test-key", LLMAuthMode.API_KEY, {})
    assert material.env["ZAI_API_KEY"] == "zai-test-key"
    assert material.env["ZAI_ANTHROPIC_BASE_URL"] == "https://api.z.ai/api/anthropic"
    assert material.env["ANTHROPIC_API_KEY"] == "zai-test-key"
    assert material.env["ANTHROPIC_AUTH_TOKEN"] == "zai-test-key"
    assert material.env["ANTHROPIC_BASE_URL"] == "https://api.z.ai/api/anthropic"


def test_zai_provider_accepts_base_url_metadata_override():
    provider = get_provider(LLMProviderName.ZAI)
    material = provider.secret_material(
        "zai-test-key",
        LLMAuthMode.API_KEY,
        {"anthropic_base_url": "https://example.test/anthropic/"},
    )
    assert material.env["ZAI_ANTHROPIC_BASE_URL"] == "https://example.test/anthropic"
    assert material.env["ANTHROPIC_BASE_URL"] == "https://example.test/anthropic"


def test_zai_provider_translates_settings_to_claude_cli_args():
    provider = get_provider(LLMProviderName.ZAI)
    translation = provider.settings_to_cli(
        {"model": "glm-5.2", "effort": "high"},
        auth_mode=LLMAuthMode.API_KEY,
    )
    assert translation.args == (
        "--model",
        "glm-5.2",
        "--effort",
        "high",
        "--permission-mode",
        "bypassPermissions",
    )


def test_deepseek_provider_uses_claude_compat_secret_material():
    provider = get_provider(LLMProviderName.DEEPSEEK)
    result = provider.validate_secret("deepseek-test-key", LLMAuthMode.API_KEY, {})
    assert result.secret == "deepseek-test-key"

    material = provider.secret_material("deepseek-test-key", LLMAuthMode.API_KEY, {})
    assert material.env["DEEPSEEK_API_KEY"] == "deepseek-test-key"
    assert (
        material.env["DEEPSEEK_ANTHROPIC_BASE_URL"]
        == "https://api.deepseek.com/anthropic"
    )
    assert material.env["ANTHROPIC_API_KEY"] == "deepseek-test-key"
    assert material.env["ANTHROPIC_AUTH_TOKEN"] == "deepseek-test-key"
    assert material.env["ANTHROPIC_BASE_URL"] == "https://api.deepseek.com/anthropic"
    # DeepSeek's documented Claude Code integration selects models via env —
    # without these the CLI falls back to its claude-* aliases, which the
    # compat layer silently remaps (unknown/sonnet/haiku ids → v4-flash), so
    # an intended Pro session can silently downgrade. Plain ids per the docs:
    # there is no "[1m]" variant (an unknown id would itself hit the flash
    # fallback).
    assert material.env["ANTHROPIC_MODEL"] == "deepseek-v4-pro"
    assert material.env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "deepseek-v4-pro"
    assert material.env["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "deepseek-v4-pro"
    assert material.env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "deepseek-v4-flash"
    assert material.env["CLAUDE_CODE_SUBAGENT_MODEL"] == "deepseek-v4-flash"


def test_deepseek_provider_accepts_base_url_metadata_override():
    provider = get_provider(LLMProviderName.DEEPSEEK)
    material = provider.secret_material(
        "deepseek-test-key",
        LLMAuthMode.API_KEY,
        {"anthropic_base_url": "https://example.test/deepseek/"},
    )
    assert (
        material.env["DEEPSEEK_ANTHROPIC_BASE_URL"]
        == "https://example.test/deepseek"
    )
    assert material.env["ANTHROPIC_BASE_URL"] == "https://example.test/deepseek"


def test_deepseek_provider_translates_settings_to_claude_cli_args():
    provider = get_provider(LLMProviderName.DEEPSEEK)
    translation = provider.settings_to_cli(
        {"model": "deepseek-v4-flash", "effort": "low"},
        auth_mode=LLMAuthMode.API_KEY,
    )
    assert translation.args == (
        "--model",
        "deepseek-v4-flash",
        "--effort",
        "low",
        "--permission-mode",
        "bypassPermissions",
    )


def test_gemini_thinking_budget_clamped_to_per_model_cap():
    provider = get_provider(LLMProviderName.GEMINI)

    # flash-lite caps at 24576 — 30000 is rejected by the real API as
    # INVALID_ARGUMENT. Clamp silently and record the clamp in unsupported.
    cli = provider.settings_to_cli(
        {
            "model": "gemini-2.5-flash-lite",
            "thinking_budget": 30000,
        }
    )
    settings_blob = cli.settings_files[0].content
    assert '"thinkingBudget": 24576' in settings_blob
    assert any("thinking_budget=30000" in u and "clamped" in u for u in cli.unsupported)


def test_gemini_thinking_budget_within_cap_not_clamped():
    provider = get_provider(LLMProviderName.GEMINI)

    cli = provider.settings_to_cli(
        {
            "model": "gemini-2.5-pro",
            "thinking_budget": 16384,
        }
    )
    settings_blob = cli.settings_files[0].content
    assert '"thinkingBudget": 16384' in settings_blob
    assert cli.unsupported == ()


def test_openai_oauth_rejects_payload_without_tokens_object():
    provider = get_provider(LLMProviderName.OPENAI)

    with pytest.raises(ValueError, match="tokens"):
        provider.validate_secret("{}", LLMAuthMode.OAUTH, {})

    with pytest.raises(ValueError, match="tokens"):
        # Plain API key accidentally pasted as an OAuth secret — JSON-parseable
        # but missing the tokens envelope. Prior behavior silently accepted
        # anything parseable; Codex would then fail opaquely at runtime.
        provider.validate_secret('{"api_key":"sk-proj-abc"}', LLMAuthMode.OAUTH, {})


def test_openai_oauth_rejects_missing_access_token():
    provider = get_provider(LLMProviderName.OPENAI)

    with pytest.raises(ValueError, match="access_token"):
        provider.validate_secret(
            '{"tokens":{"refresh_token":"rt"}}', LLMAuthMode.OAUTH, {}
        )


def test_openai_oauth_accepts_valid_shape():
    provider = get_provider(LLMProviderName.OPENAI)
    raw = '{"tokens":{"access_token":"at","refresh_token":"rt"},"OPENAI_API_KEY":null}'
    result = provider.validate_secret(raw, LLMAuthMode.OAUTH, {})
    assert result.secret == raw


def test_providers_expose_default_execution_mode_only():
    """Execution modes were retired with the settings-bag redesign; advisor
    is now a per-provider knob under `anthropic.advisor` in the settings bag,
    not a dispatch mode. The base class reports `("default",)` for all
    providers now."""
    for provider_name in (
        LLMProviderName.ANTHROPIC,
        LLMProviderName.OPENAI,
        LLMProviderName.GEMINI,
        LLMProviderName.ZAI,
    ):
        provider = get_provider(provider_name)
        assert provider.supported_execution_modes() == ("default",)


# ---------------------------------------------------------------------------
# OpenAI flex service_tier gate — regression for the "Unsupported service_tier:
# flex" 400 that blew up any sonnet/haiku task when admin defaults stamped
# flex across the provider matrix.
# ---------------------------------------------------------------------------


class TestOpenAIFlexGate:
    @pytest.mark.parametrize(
        "model",
        ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.4", "gpt-5.4-mini"],
    )
    def test_flex_passes_through_on_supported_models(self, model):
        provider = get_provider(LLMProviderName.OPENAI)
        translation = provider.settings_to_cli(
            {
                "model": model,
                "service_tier": "flex",
            }
        )
        config_flags = [
            translation.args[i + 1] for i, a in enumerate(translation.args) if a == "-c"
        ]
        assert 'service_tier="flex"' in config_flags
        assert not any("service_tier=flex" in u for u in translation.unsupported)

    @pytest.mark.parametrize(
        "model",
        [
            "gpt-5.3-codex",  # codex variants — no flex column in OpenAI pricing
            "gpt-5.3-codex-spark",  # spark — likewise
            "gpt-5.2",  # prior frontier — not in current flex table
        ],
    )
    def test_flex_dropped_on_unsupported_models(self, model):
        provider = get_provider(LLMProviderName.OPENAI)
        translation = provider.settings_to_cli(
            {
                "model": model,
                "service_tier": "flex",
            }
        )
        config_flags = [
            translation.args[i + 1] for i, a in enumerate(translation.args) if a == "-c"
        ]
        assert 'service_tier="flex"' not in config_flags
        assert any(
            "service_tier=flex" in u and "dropped" in u for u in translation.unsupported
        ), f"expected dropped-flex marker for {model}, got {translation.unsupported}"

    def test_flex_dropped_when_model_unknown(self):
        """Unknown model → fail closed (drop flex)."""
        provider = get_provider(LLMProviderName.OPENAI)
        translation = provider.settings_to_cli({"service_tier": "flex"})
        config_flags = [
            translation.args[i + 1] for i, a in enumerate(translation.args) if a == "-c"
        ]
        assert 'service_tier="flex"' not in config_flags
        assert any("service_tier=flex" in u for u in translation.unsupported)

    def test_flex_dropped_on_oauth_regardless_of_model(self):
        """Empirical regression: OpenAI's Responses API rejects flex on
        ChatGPT-subscription auth with 400 `Unsupported service_tier: flex`
        even on a flex-capable model like gpt-5.4. Flex is an API-key
        billing tier with no ChatGPT-credits equivalent."""
        provider = get_provider(LLMProviderName.OPENAI)
        translation = provider.settings_to_cli(
            {"model": "gpt-5.4", "service_tier": "flex"},
            auth_mode=LLMAuthMode.OAUTH,
        )
        config_flags = [
            translation.args[i + 1] for i, a in enumerate(translation.args) if a == "-c"
        ]
        assert 'service_tier="flex"' not in config_flags
        assert any(
            "service_tier=flex" in u and "OAuth" in u for u in translation.unsupported
        ), f"expected OAuth-flex marker, got {translation.unsupported}"

    def test_flex_passes_on_api_key_with_flex_capable_model(self):
        """API-key auth on a flex-capable model is the happy path — must
        not regress when the OAuth gate tightens."""
        provider = get_provider(LLMProviderName.OPENAI)
        translation = provider.settings_to_cli(
            {"model": "gpt-5.4", "service_tier": "flex"},
            auth_mode=LLMAuthMode.API_KEY,
        )
        config_flags = [
            translation.args[i + 1] for i, a in enumerate(translation.args) if a == "-c"
        ]
        assert 'service_tier="flex"' in config_flags


# ---------------------------------------------------------------------------
# OpenAI fast service_tier gate — regression for ChatGPT-subscription-only
# Fast-mode routing. API-key auth 400s on fast since it consumes Fast-mode
# credits that don't exist on pay-as-you-go billing.
# ---------------------------------------------------------------------------


class TestOpenAIFastGate:
    def test_fast_passes_on_oauth(self):
        provider = get_provider(LLMProviderName.OPENAI)
        translation = provider.settings_to_cli(
            {"model": "gpt-5.4", "service_tier": "fast"},
            auth_mode=LLMAuthMode.OAUTH,
        )
        config_flags = [
            translation.args[i + 1] for i, a in enumerate(translation.args) if a == "-c"
        ]
        assert 'service_tier="fast"' in config_flags

    def test_fast_dropped_on_api_key(self):
        provider = get_provider(LLMProviderName.OPENAI)
        translation = provider.settings_to_cli(
            {"model": "gpt-5.4", "service_tier": "fast"},
            auth_mode=LLMAuthMode.API_KEY,
        )
        config_flags = [
            translation.args[i + 1] for i, a in enumerate(translation.args) if a == "-c"
        ]
        assert 'service_tier="fast"' not in config_flags
        assert any(
            "service_tier=fast" in u and "OAuth" in u for u in translation.unsupported
        ), f"expected OAuth-fast marker, got {translation.unsupported}"

    def test_fast_dropped_without_auth_mode(self):
        """Legacy callers that don't pass auth_mode default to the
        conservative API-key interpretation — fast drops."""
        provider = get_provider(LLMProviderName.OPENAI)
        translation = provider.settings_to_cli(
            {"model": "gpt-5.4", "service_tier": "fast"}
        )
        config_flags = [
            translation.args[i + 1] for i, a in enumerate(translation.args) if a == "-c"
        ]
        assert 'service_tier="fast"' not in config_flags


# ---------------------------------------------------------------------------
# OpenAI reasoning_effort + tier map — exercise the xhigh value that the
# current Codex CLI accepts (was capped at `high` in earlier schemas) and
# confirm the tier map resolves to current picker-visible models.
# ---------------------------------------------------------------------------


class TestOpenAITierAndEffort:
    @pytest.mark.parametrize(
        "effort", ["low", "medium", "high", "xhigh", "max", "ultra"]
    )
    def test_reasoning_effort_passes_through(self, effort):
        """All five reasoning levels are accepted at translation; per-model
        compat gaps surface as runtime 400s (deferred to model/list)."""
        provider = get_provider(LLMProviderName.OPENAI)
        translation = provider.settings_to_cli(
            {
                "model": "gpt-5.4",
                "reasoning_effort": effort,
            }
        )
        config_flags = [
            translation.args[i + 1] for i, a in enumerate(translation.args) if a == "-c"
        ]
        assert f'model_reasoning_effort="{effort}"' in config_flags

    def test_tier_map_resolves_to_current_picker_models(self):
        provider = get_provider(LLMProviderName.OPENAI)
        assert provider.tier_to_model("opus", {}) == "gpt-5.6-sol"
        assert provider.tier_to_model("sonnet", {}) == "gpt-5.6-terra"
        assert provider.tier_to_model("haiku", {}) == "gpt-5.6-luna"


# ---------------------------------------------------------------------------
# Anthropic 1M context opt-in — regression for the missed capability.
# The [1m] suffix on the model ID is the documented activation mechanism;
# model-gated to fable-5, opus-4-8, opus-4-7, opus-4-6, sonnet-4-6.
# ---------------------------------------------------------------------------


class TestAnthropic1MContext:
    @pytest.mark.parametrize(
        "model",
        [
            "claude-fable-5",
            "claude-opus-4-8",
            "claude-opus-4-7",
            "claude-opus-4-6",
            "claude-sonnet-4-6",
        ],
    )
    def test_1m_suffix_appended_on_compatible_models(self, model):
        provider = get_provider(LLMProviderName.ANTHROPIC)
        translation = provider.settings_to_cli(
            {
                "model": model,
                "use_1m_context": True,
            }
        )
        args = list(translation.args)
        assert args[args.index("--model") + 1] == f"{model}[1m]"
        assert not any("use_1m_context" in u for u in translation.unsupported)

    def test_1m_opt_in_on_haiku_drops_suffix_with_marker(self):
        provider = get_provider(LLMProviderName.ANTHROPIC)
        translation = provider.settings_to_cli(
            {
                "model": "claude-haiku-4-5-20251001",
                "use_1m_context": True,
            }
        )
        args = list(translation.args)
        assert args[args.index("--model") + 1] == "claude-haiku-4-5-20251001"
        assert any(
            "use_1m_context" in u and "suffix dropped" in u
            for u in translation.unsupported
        ), f"expected 1M-unsupported marker, got {translation.unsupported}"

    def test_1m_default_off_emits_plain_model(self):
        """Without the opt-in, model goes out plain — no accidental [1m]."""
        provider = get_provider(LLMProviderName.ANTHROPIC)
        translation = provider.settings_to_cli({"model": "claude-opus-4-7"})
        args = list(translation.args)
        assert args[args.index("--model") + 1] == "claude-opus-4-7"

    def test_1m_explicit_false_emits_plain_model(self):
        provider = get_provider(LLMProviderName.ANTHROPIC)
        translation = provider.settings_to_cli(
            {
                "model": "claude-opus-4-7",
                "use_1m_context": False,
            }
        )
        args = list(translation.args)
        assert args[args.index("--model") + 1] == "claude-opus-4-7"


# ---------------------------------------------------------------------------
# Gemini preview / unknown model advisory markers.
# ---------------------------------------------------------------------------


class TestGeminiPreviewAdvisory:
    @pytest.mark.parametrize(
        "model",
        [
            "gemini-3.1-pro-preview",
            "gemini-3-flash-preview",
            "gemini-3.1-flash-lite-preview",
        ],
    )
    def test_preview_models_emit_advisory(self, model):
        provider = get_provider(LLMProviderName.GEMINI)
        translation = provider.settings_to_cli({"model": model})
        assert any(
            "preview model" in u and "preview access" in u
            for u in translation.unsupported
        )

    @pytest.mark.parametrize(
        "model",
        [
            "gemini-2.5-pro",
            "gemini-2.5-flash",
            "gemini-2.5-flash-lite",
        ],
    )
    def test_ga_models_emit_no_advisory(self, model):
        provider = get_provider(LLMProviderName.GEMINI)
        translation = provider.settings_to_cli({"model": model})
        assert not any(
            "preview" in u or "known-availability" in u for u in translation.unsupported
        )

    def test_unknown_model_emits_advisory(self):
        provider = get_provider(LLMProviderName.GEMINI)
        translation = provider.settings_to_cli({"model": "gemini-99-ultra"})
        assert any("known-availability" in u for u in translation.unsupported)


# ---------------------------------------------------------------------------
# Gemini preview → GA fallback map.
# ---------------------------------------------------------------------------


class TestGeminiFallbackModel:
    @pytest.mark.parametrize(
        "preview,ga",
        [
            ("gemini-3.1-pro-preview", "gemini-2.5-pro"),
            ("gemini-3-flash-preview", "gemini-2.5-flash"),
            ("gemini-3.1-flash-lite-preview", "gemini-2.5-flash-lite"),
        ],
    )
    def test_preview_falls_back_to_ga_counterpart(self, preview, ga):
        provider = get_provider(LLMProviderName.GEMINI)
        assert provider.fallback_model(preview) == ga

    def test_ga_models_have_no_fallback(self):
        provider = get_provider(LLMProviderName.GEMINI)
        # GA models aren't "restricted" — they already work, so there's
        # nothing to fall back from.
        assert provider.fallback_model("gemini-2.5-pro") is None

    def test_legacy_preview_alias_falls_back_through_live_preview(self):
        provider = get_provider(LLMProviderName.GEMINI)
        assert provider.fallback_model("gemini-3-pro-preview") == "gemini-2.5-pro"


# ---------------------------------------------------------------------------
# Gemini thinking-config bifurcation — 2.5 uses thinkingBudget (int),
# 3.x uses thinkingLevel (enum). Mismatched pairings surface as markers
# rather than silent drops.
# ---------------------------------------------------------------------------


class TestGeminiThinkingConfig:
    def test_budget_on_25_model_emits_clean(self):
        provider = get_provider(LLMProviderName.GEMINI)
        translation = provider.settings_to_cli(
            {
                "model": "gemini-2.5-pro",
                "thinking_budget": 4096,
            }
        )
        # Check the settings.json file payload — it's where the thinking
        # config lands (Gemini CLI has no per-invocation flag).
        assert translation.settings_files
        payload = json.loads(translation.settings_files[0].content)
        thinking = payload["modelConfigs"]["customAliases"]["blueprint-runtime"][
            "modelConfig"
        ]["generateContentConfig"]["thinkingConfig"]
        assert thinking["thinkingBudget"] == 4096
        # No 3.x-mismatch marker on a 2.5 model.
        assert not any("back-compat" in u for u in translation.unsupported)

    def test_level_on_3x_preview_emits_clean(self):
        provider = get_provider(LLMProviderName.GEMINI)
        translation = provider.settings_to_cli(
            {
                "model": "gemini-3.1-pro-preview",
                "thinking_level": "high",
            }
        )
        assert translation.settings_files
        payload = json.loads(translation.settings_files[0].content)
        thinking = payload["modelConfigs"]["customAliases"]["blueprint-runtime"][
            "modelConfig"
        ]["generateContentConfig"]["thinkingConfig"]
        assert thinking["thinkingLevel"] == "high"
        # No 2.5-mismatch marker on a 3.x model.
        assert not any(
            "thinking_level on Gemini 2.5" in u for u in translation.unsupported
        )

    def test_level_on_25_model_dropped_with_marker(self):
        provider = get_provider(LLMProviderName.GEMINI)
        translation = provider.settings_to_cli(
            {
                "model": "gemini-2.5-pro",
                "thinking_level": "high",
            }
        )
        # thinking_level is dropped on 2.5; settings_files is only emitted
        # when generate_config is non-empty, so this specific bag ends up
        # with no thinking config at all.
        if translation.settings_files:
            payload = json.loads(translation.settings_files[0].content)
            tc = payload["modelConfigs"]["customAliases"]["blueprint-runtime"][
                "modelConfig"
            ]["generateContentConfig"].get("thinkingConfig", {})
            assert "thinkingLevel" not in tc
        assert any(
            "thinking_level on Gemini 2.5" in u and "dropped" in u
            for u in translation.unsupported
        ), f"expected 2.5-mismatch marker, got {translation.unsupported}"

    def test_budget_on_3x_model_dropped_with_marker(self):
        """Stricter than Google's docs suggest: on 3.x, thinking_budget
        is dropped entirely because Gemini CLI appears to inject its own
        default thinkingLevel that conflicts with budget. Operators need
        to use thinking_level for explicit control on 3.x."""
        provider = get_provider(LLMProviderName.GEMINI)
        translation = provider.settings_to_cli(
            {
                "model": "gemini-3.1-pro-preview",
                "thinking_budget": 4096,
            }
        )
        # No thinkingConfig should be emitted — the CLI's default takes over.
        if translation.settings_files:
            payload = json.loads(translation.settings_files[0].content)
            cfg = payload["modelConfigs"]["customAliases"]["blueprint-runtime"][
                "modelConfig"
            ]["generateContentConfig"]
            tc = cfg.get("thinkingConfig", {})
            assert "thinkingBudget" not in tc
            assert "thinkingLevel" not in tc
        assert any(
            "thinking_budget" in u and "3.x" in u and "unreliable" in u
            for u in translation.unsupported
        ), f"expected 3.x-budget-dropped marker, got {translation.unsupported}"

    def test_budget_and_level_both_set_on_3x_drops_budget(self):
        """Gemini API rejects the combination with 400 `You can only set
        only one of thinking budget and thinking level`. On 3.x, prefer
        the newer thinkingLevel knob and drop thinkingBudget."""
        provider = get_provider(LLMProviderName.GEMINI)
        translation = provider.settings_to_cli(
            {
                "model": "gemini-3.1-pro-preview",
                "thinking_budget": 4096,
                "thinking_level": "high",
            }
        )
        assert translation.settings_files
        payload = json.loads(translation.settings_files[0].content)
        thinking = payload["modelConfigs"]["customAliases"]["blueprint-runtime"][
            "modelConfig"
        ]["generateContentConfig"]["thinkingConfig"]
        assert thinking == {"thinkingLevel": "high"}
        assert any(
            "thinking_budget" in u and "thinking_level" in u and "dropped" in u
            for u in translation.unsupported
        ), f"expected budget-dropped marker, got {translation.unsupported}"

    def test_budget_and_level_both_set_on_25_drops_level(self):
        """On 2.5, prefer thinking_budget (the supported knob) and drop
        thinking_level with marker. Prevents the same API-reject pattern."""
        provider = get_provider(LLMProviderName.GEMINI)
        translation = provider.settings_to_cli(
            {
                "model": "gemini-2.5-pro",
                "thinking_budget": 4096,
                "thinking_level": "high",
            }
        )
        assert translation.settings_files
        payload = json.loads(translation.settings_files[0].content)
        thinking = payload["modelConfigs"]["customAliases"]["blueprint-runtime"][
            "modelConfig"
        ]["generateContentConfig"]["thinkingConfig"]
        assert thinking == {"thinkingBudget": 4096}
        assert any(
            "thinking_level on Gemini 2.5" in u and "dropped" in u
            for u in translation.unsupported
        ), f"expected level-dropped marker, got {translation.unsupported}"


# ---------------------------------------------------------------------------
# Gemini legacy aliases — Google retired gemini-3-pro-preview on 2026-03-09
# and repointed the name to gemini-3.1-pro-preview. Admins with old configs
# shouldn't hard-fail; rewrite silently with a marker.
# ---------------------------------------------------------------------------


class TestGeminiLegacyAliases:
    def test_3_pro_preview_rewrites_to_3_1(self):
        provider = get_provider(LLMProviderName.GEMINI)
        translation = provider.settings_to_cli(
            {
                "model": "gemini-3-pro-preview",
            }
        )
        # The -m flag should carry either the aliased model or an alias
        # name; for this bag (no generate_config), we expect bare -m with
        # the rewritten ID.
        args = list(translation.args)
        m_idx = args.index("-m")
        assert args[m_idx + 1] == "gemini-3.1-pro-preview"
        assert any(
            "retired" in u and "gemini-3.1-pro-preview" in u
            for u in translation.unsupported
        ), f"expected retirement marker, got {translation.unsupported}"

    def test_live_model_not_rewritten(self):
        provider = get_provider(LLMProviderName.GEMINI)
        translation = provider.settings_to_cli(
            {
                "model": "gemini-3.1-pro-preview",
            }
        )
        args = list(translation.args)
        m_idx = args.index("-m")
        assert args[m_idx + 1] == "gemini-3.1-pro-preview"
        # No retirement marker on a live model.
        assert not any("retired" in u for u in translation.unsupported)
        assert provider.fallback_model("gemini-2.5-flash") is None

    def test_none_and_empty_return_none(self):
        provider = get_provider(LLMProviderName.GEMINI)
        assert provider.fallback_model(None) is None
        assert provider.fallback_model("") is None

    def test_anthropic_fallback_map_covers_fable_only(self):
        """Anthropic's only restricted entry is the TEMPORARY fable-5 →
        opus-4-8 mapping (removal-safety net for the limited-availability
        window); GA models have no fallback."""
        provider = get_provider(LLMProviderName.ANTHROPIC)
        assert provider.fallback_model("claude-fable-5") == "claude-opus-4-8"
        assert provider.fallback_model("claude-opus-4-8") is None
        assert provider.fallback_model("claude-opus-4-7") is None
        assert provider.fallback_model(None) is None


# ── Usage-headroom probe (provider-agnostic, advisory) ───────────────────────


class _FakeResponse:
    def __init__(self, json_data=None, raise_status=False, raise_json=False):
        self._json = json_data
        self._raise_status = raise_status
        self._raise_json = raise_json

    def raise_for_status(self):
        if self._raise_status:
            raise RuntimeError("HTTP 401 Unauthorized")

    def json(self):
        if self._raise_json:
            raise ValueError("not json")
        return self._json


class _FakeAsyncClient:
    """Minimal async-context-manager stand-in for httpx.AsyncClient."""

    def __init__(self, response=None, raise_on_get=False, **_kw):
        self._response = response
        self._raise_on_get = raise_on_get

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None, **_kw):
        if self._raise_on_get:
            raise RuntimeError("connect timeout")
        # Expose the request so tests can assert headers were sent.
        _FakeAsyncClient.last_url = url
        _FakeAsyncClient.last_headers = headers
        return self._response


def _patch_httpx(monkeypatch, client):
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client)


class TestAnthropicUsageNormalization:
    def test_valid_body_known_with_load_bearing_windows(self):
        from app.providers.anthropic import _normalize_anthropic_usage

        snap = _normalize_anthropic_usage(
            {
                "five_hour": {"utilization": 28.0, "resets_at": "2026-06-06T00:00:00Z"},
                "seven_day": {"utilization": 64.0, "resets_at": None},
                "seven_day_sonnet": {"utilization": None},
            },
            "2026-06-06T01:00:00",
        )
        assert snap.known is True
        assert snap.window_utilizations["five_hour"] == 28.0
        assert snap.window_utilizations["seven_day"] == 64.0
        assert snap.resets_at["five_hour"] == "2026-06-06T00:00:00Z"
        assert snap.resets_at["seven_day"] is None
        # Per-model null window is tolerated and simply not carried.
        assert "seven_day_sonnet" not in snap.window_utilizations
        assert snap.probed_at == "2026-06-06T01:00:00"
        assert set(snap.load_bearing_utilizations()) == {28.0, 64.0}

    def test_extra_numeric_window_carried_additively(self):
        from app.providers.anthropic import _normalize_anthropic_usage

        snap = _normalize_anthropic_usage(
            {
                "five_hour": {"utilization": 1.0},
                "seven_day": {"utilization": 2.0},
                "seven_day_opus": {"utilization": 3.0, "resets_at": "x"},
            },
            "n",
        )
        assert snap.window_utilizations["seven_day_opus"] == 3.0
        # But extras are never load-bearing.
        assert set(snap.load_bearing_utilizations()) == {1.0, 2.0}

    def test_missing_load_bearing_window_is_unknown(self):
        from app.providers.anthropic import _normalize_anthropic_usage

        snap = _normalize_anthropic_usage({"five_hour": {"utilization": 10.0}}, "n")
        assert snap.known is False

    def test_null_load_bearing_field_is_unknown(self):
        from app.providers.anthropic import _normalize_anthropic_usage

        snap = _normalize_anthropic_usage(
            {"five_hour": {"utilization": None}, "seven_day": {"utilization": 5.0}}, "n"
        )
        assert snap.known is False

    def test_bool_utilization_is_unknown(self):
        from app.providers.anthropic import _normalize_anthropic_usage

        snap = _normalize_anthropic_usage(
            {"five_hour": {"utilization": True}, "seven_day": {"utilization": 5.0}}, "n"
        )
        assert snap.known is False

    def test_non_mapping_body_is_unknown(self):
        from app.providers.anthropic import _normalize_anthropic_usage

        assert _normalize_anthropic_usage("garbage", "n").known is False
        assert _normalize_anthropic_usage(None, "n").known is False
        assert _normalize_anthropic_usage([1, 2, 3], "n").known is False

    def test_window_cell_not_mapping_is_unknown(self):
        from app.providers.anthropic import _normalize_anthropic_usage

        snap = _normalize_anthropic_usage(
            {"five_hour": 28.0, "seven_day": {"utilization": 5.0}}, "n"
        )
        assert snap.known is False


class TestAnthropicProbeUsage:
    @pytest.mark.asyncio
    async def test_probe_returns_known_snapshot_on_valid_response(self, monkeypatch):
        provider = get_provider(LLMProviderName.ANTHROPIC)
        resp = _FakeResponse(
            json_data={
                "five_hour": {"utilization": 28.0, "resets_at": "2026-06-06T00:00:00Z"},
                "seven_day": {"utilization": 64.0, "resets_at": None},
            }
        )
        _patch_httpx(monkeypatch, _FakeAsyncClient(response=resp))
        snap = await provider.probe_usage("sk-ant-oat01-secret", LLMAuthMode.OAUTH, {})
        assert snap.known is True
        assert snap.window_utilizations["five_hour"] == 28.0
        # Sends the OAuth bearer + beta header.
        assert _FakeAsyncClient.last_headers["Authorization"] == (
            "Bearer sk-ant-oat01-secret"
        )
        assert _FakeAsyncClient.last_headers["anthropic-beta"] == "oauth-2025-04-20"

    @pytest.mark.asyncio
    async def test_http_error_resolves_to_unknown(self, monkeypatch):
        provider = get_provider(LLMProviderName.ANTHROPIC)
        _patch_httpx(monkeypatch, _FakeAsyncClient(raise_on_get=True))
        snap = await provider.probe_usage("sk-ant-oat01-secret", LLMAuthMode.OAUTH, {})
        assert snap.known is False

    @pytest.mark.asyncio
    async def test_status_error_resolves_to_unknown(self, monkeypatch):
        provider = get_provider(LLMProviderName.ANTHROPIC)
        _patch_httpx(
            monkeypatch, _FakeAsyncClient(response=_FakeResponse(raise_status=True))
        )
        snap = await provider.probe_usage("sk-ant-oat01-secret", LLMAuthMode.OAUTH, {})
        assert snap.known is False

    @pytest.mark.asyncio
    async def test_garbage_json_resolves_to_unknown(self, monkeypatch):
        provider = get_provider(LLMProviderName.ANTHROPIC)
        _patch_httpx(
            monkeypatch, _FakeAsyncClient(response=_FakeResponse(raise_json=True))
        )
        snap = await provider.probe_usage("sk-ant-oat01-secret", LLMAuthMode.OAUTH, {})
        assert snap.known is False

    @pytest.mark.asyncio
    async def test_schema_drift_resolves_to_unknown(self, monkeypatch):
        provider = get_provider(LLMProviderName.ANTHROPIC)
        _patch_httpx(
            monkeypatch,
            _FakeAsyncClient(response=_FakeResponse(json_data={"unexpected": "shape"})),
        )
        snap = await provider.probe_usage("sk-ant-oat01-secret", LLMAuthMode.OAUTH, {})
        assert snap.known is False

    @pytest.mark.asyncio
    async def test_api_key_auth_mode_is_unknown_without_probe(self, monkeypatch):
        provider = get_provider(LLMProviderName.ANTHROPIC)

        # Probe must not even attempt I/O for API-key mode.
        def _boom(**_kw):
            raise AssertionError("must not call httpx for API_KEY mode")

        import httpx

        monkeypatch.setattr(httpx, "AsyncClient", _boom)
        snap = await provider.probe_usage("sk-ant-key", LLMAuthMode.API_KEY, {})
        assert snap.known is False

    @pytest.mark.asyncio
    async def test_missing_secret_is_unknown(self):
        provider = get_provider(LLMProviderName.ANTHROPIC)
        snap = await provider.probe_usage(None, LLMAuthMode.OAUTH, {})
        assert snap.known is False


class TestOtherProvidersProbeUnknown:
    @pytest.mark.asyncio
    async def test_openai_probe_is_unknown(self):
        provider = get_provider(LLMProviderName.OPENAI)
        snap = await provider.probe_usage("sk-proj-x", LLMAuthMode.API_KEY, {})
        assert snap.known is False

    @pytest.mark.asyncio
    async def test_gemini_probe_is_unknown(self):
        provider = get_provider(LLMProviderName.GEMINI)
        snap = await provider.probe_usage("key", LLMAuthMode.API_KEY, {})
        assert snap.known is False
