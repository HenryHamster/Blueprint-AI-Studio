import pytest

from app.models import LLMAuthMode, LLMProviderName
from app.providers.registry import get_provider, list_providers


def test_registry_lists_expected_providers():
    names = {provider.name for provider in list_providers()}
    assert names == {
        LLMProviderName.ANTHROPIC,
        LLMProviderName.OPENAI,
        LLMProviderName.GEMINI,
    }


def test_anthropic_provider_validates_oauth_token_and_exposes_rate_limit_patterns():
    provider = get_provider(LLMProviderName.ANTHROPIC)
    result = provider.validate_secret("sk-ant-oat01-valid_token", LLMAuthMode.OAUTH, {})
    assert result.secret == "sk-ant-oat01-valid_token"
    assert "you've hit your usage limit" in provider.rate_limit_patterns(LLMAuthMode.OAUTH)


def test_openai_provider_validates_api_key():
    provider = get_provider(LLMProviderName.OPENAI)
    result = provider.validate_secret("sk-proj-test_token", LLMAuthMode.API_KEY, {})
    assert result.secret == "sk-proj-test_token"


def test_openai_provider_oauth_materializes_auth_file():
    provider = get_provider(LLMProviderName.OPENAI)
    material = provider.secret_material('{"tokens":{"access_token":"abc"}}', LLMAuthMode.OAUTH, {})

    assert material.env["CODEX_HOME"] == "/opt/agent/openai"
    assert material.files[0].path == "/opt/agent/openai/auth.json"
    assert material.files[0].mode == "0600"


def test_gemini_service_account_requires_metadata_and_service_account_json():
    provider = get_provider(LLMProviderName.GEMINI)

    with pytest.raises(ValueError, match="Missing provider metadata fields: project, location"):
        provider.validate_secret(
            '{"type":"service_account","client_email":"bot@example.com","private_key":"abc"}',
            LLMAuthMode.SERVICE_ACCOUNT,
            {},
        )

    with pytest.raises(ValueError, match="Service account JSON missing fields: client_email, private_key"):
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
    # Without this env var, Gemini CLI's getAuthTypeFromEnv returns undefined
    # and validateNonInteractiveAuth throws on every invocation. Regression
    # test for the SA-mode dispatch failure.
    assert material.env["GOOGLE_GENAI_USE_VERTEXAI"] == "true"
    assert material.files[0].path == "/opt/agent/gemini/sa.json"


def test_gemini_thinking_budget_clamped_to_per_model_cap():
    provider = get_provider(LLMProviderName.GEMINI)

    # flash-lite caps at 24576 — 30000 is rejected by the real API as
    # INVALID_ARGUMENT. Clamp silently and record the clamp in unsupported.
    cli = provider.settings_to_cli({
        "model": "gemini-2.5-flash-lite",
        "thinking_budget": 30000,
    })
    settings_blob = cli.settings_files[0].content
    assert '"thinkingBudget": 24576' in settings_blob
    assert any("thinking_budget=30000" in u and "clamped" in u for u in cli.unsupported)


def test_gemini_thinking_budget_within_cap_not_clamped():
    provider = get_provider(LLMProviderName.GEMINI)

    cli = provider.settings_to_cli({
        "model": "gemini-2.5-pro",
        "thinking_budget": 16384,
    })
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
        provider.validate_secret('{"tokens":{"refresh_token":"rt"}}', LLMAuthMode.OAUTH, {})


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
    for provider_name in (LLMProviderName.ANTHROPIC, LLMProviderName.OPENAI, LLMProviderName.GEMINI):
        provider = get_provider(provider_name)
        assert provider.supported_execution_modes() == ("default",)


# ---------------------------------------------------------------------------
# OpenAI flex service_tier gate — regression for the "Unsupported service_tier:
# flex" 400 that blew up any sonnet/haiku task when admin defaults stamped
# flex across the provider matrix.
# ---------------------------------------------------------------------------

class TestOpenAIFlexGate:
    @pytest.mark.parametrize("model", [
        "gpt-5", "gpt-5-mini", "o3", "o3-pro", "o4-mini", "o4-mini-high",
        # Case-insensitive prefix match so spot-casing variants survive.
        "GPT-5", "O4-Mini",
    ])
    def test_flex_passes_through_on_supported_models(self, model):
        provider = get_provider(LLMProviderName.OPENAI)
        translation = provider.settings_to_cli({
            "model": model,
            "service_tier": "flex",
        })
        config_flags = [
            translation.args[i + 1]
            for i, a in enumerate(translation.args) if a == "-c"
        ]
        assert 'service_tier="flex"' in config_flags
        assert not any("service_tier=flex" in u for u in translation.unsupported)

    @pytest.mark.parametrize("model", [
        "gpt-4.1",        # gpt-4 family — not flex-capable
        "gpt-4o-mini",    # gpt-4 family — not flex-capable
        "gpt-5-nano",     # explicit-excludes match — nano drops flex per docs
        "gpt-5-codex",    # explicit-excludes — codex variant drops flex per docs
        "gpt-4",          # legacy
    ])
    def test_flex_dropped_on_unsupported_models(self, model):
        provider = get_provider(LLMProviderName.OPENAI)
        translation = provider.settings_to_cli({
            "model": model,
            "service_tier": "flex",
        })
        config_flags = [
            translation.args[i + 1]
            for i, a in enumerate(translation.args) if a == "-c"
        ]
        assert 'service_tier="flex"' not in config_flags
        assert any(
            "service_tier=flex" in u and "dropped" in u
            for u in translation.unsupported
        ), f"expected dropped-flex marker for {model}, got {translation.unsupported}"

    def test_flex_dropped_when_model_unknown(self):
        """Unknown model → fail closed (drop flex). Better one warning than
        a silent 400 at dispatch time."""
        provider = get_provider(LLMProviderName.OPENAI)
        translation = provider.settings_to_cli({
            "service_tier": "flex",
            # no model → the effective-model injection in compute.py normally
            # fills this in, but we still want the gate to fail closed if a
            # settings_to_cli caller forgets.
        })
        config_flags = [
            translation.args[i + 1]
            for i, a in enumerate(translation.args) if a == "-c"
        ]
        assert 'service_tier="flex"' not in config_flags
        assert any("service_tier=flex" in u for u in translation.unsupported)

    def test_fast_not_gated_by_flex_check(self):
        """service_tier=fast is a distinct tier (priority routing) with its
        own model rules; the flex gate must not accidentally drop it."""
        provider = get_provider(LLMProviderName.OPENAI)
        translation = provider.settings_to_cli({
            "model": "gpt-4.1",
            "service_tier": "fast",
        })
        config_flags = [
            translation.args[i + 1]
            for i, a in enumerate(translation.args) if a == "-c"
        ]
        assert 'service_tier="fast"' in config_flags


# ---------------------------------------------------------------------------
# Gemini preview / unknown model advisory markers.
# ---------------------------------------------------------------------------

class TestGeminiPreviewAdvisory:
    @pytest.mark.parametrize("model", [
        "gemini-3.1-pro-preview",
        "gemini-3.1-flash-preview",
        "gemini-3-pro-preview",
        "gemini-3-flash-preview",
    ])
    def test_preview_models_emit_advisory(self, model):
        provider = get_provider(LLMProviderName.GEMINI)
        translation = provider.settings_to_cli({"model": model})
        assert any(
            "preview model" in u and "preview access" in u
            for u in translation.unsupported
        )

    @pytest.mark.parametrize("model", [
        "gemini-2.5-pro",
        "gemini-2.5-flash",
        "gemini-2.5-flash-lite",
    ])
    def test_ga_models_emit_no_advisory(self, model):
        provider = get_provider(LLMProviderName.GEMINI)
        translation = provider.settings_to_cli({"model": model})
        assert not any(
            "preview" in u or "known-availability" in u
            for u in translation.unsupported
        )

    def test_unknown_model_emits_advisory(self):
        provider = get_provider(LLMProviderName.GEMINI)
        translation = provider.settings_to_cli({"model": "gemini-99-ultra"})
        assert any(
            "known-availability" in u for u in translation.unsupported
        )


# ---------------------------------------------------------------------------
# Gemini preview → GA fallback map.
# ---------------------------------------------------------------------------

class TestGeminiFallbackModel:
    @pytest.mark.parametrize("preview,ga", [
        ("gemini-3-pro-preview", "gemini-2.5-pro"),
        ("gemini-3-flash-preview", "gemini-2.5-flash"),
        ("gemini-3.1-pro-preview", "gemini-2.5-pro"),
        ("gemini-3.1-flash-preview", "gemini-2.5-flash"),
    ])
    def test_preview_falls_back_to_ga_counterpart(self, preview, ga):
        provider = get_provider(LLMProviderName.GEMINI)
        assert provider.fallback_model(preview) == ga

    def test_ga_models_have_no_fallback(self):
        provider = get_provider(LLMProviderName.GEMINI)
        # GA models aren't "restricted" — they already work, so there's
        # nothing to fall back from.
        assert provider.fallback_model("gemini-2.5-pro") is None
        assert provider.fallback_model("gemini-2.5-flash") is None

    def test_none_and_empty_return_none(self):
        provider = get_provider(LLMProviderName.GEMINI)
        assert provider.fallback_model(None) is None
        assert provider.fallback_model("") is None

    def test_base_class_returns_none_by_default(self):
        """Anthropic doesn't gate models per key; no fallback map needed."""
        provider = get_provider(LLMProviderName.ANTHROPIC)
        assert provider.fallback_model("claude-opus-4-7") is None
