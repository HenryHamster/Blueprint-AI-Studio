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
    assert material.files[0].path == "/opt/agent/gemini/sa.json"


def test_providers_expose_default_execution_mode_only():
    """Execution modes were retired with the settings-bag redesign; advisor
    is now a per-provider knob under `anthropic.advisor` in the settings bag,
    not a dispatch mode. The base class reports `("default",)` for all
    providers now."""
    for provider_name in (LLMProviderName.ANTHROPIC, LLMProviderName.OPENAI, LLMProviderName.GEMINI):
        provider = get_provider(provider_name)
        assert provider.supported_execution_modes() == ("default",)
