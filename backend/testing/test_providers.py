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
