import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.inline_llm import call_inline_llm_json, resolve_inline_llm_settings
from app.shared_state import runtime_config


def test_resolve_inline_llm_settings_defaults(monkeypatch):
    monkeypatch.setitem(runtime_config, "inline_llm", {})

    settings = resolve_inline_llm_settings("compute_qa")

    assert settings.provider == "deepseek"
    assert settings.model == "deepseek-v4-flash"
    assert settings.base_url is None
    assert settings.timeout_seconds == 30.0
    assert settings.max_tokens == 800
    assert settings.disabled is False


def test_resolve_inline_llm_settings_provider_only_uses_provider_model(monkeypatch):
    monkeypatch.setitem(
        runtime_config,
        "inline_llm",
        {"default": {"provider": "zai"}},
    )

    settings = resolve_inline_llm_settings("compute_qa")

    assert settings.provider == "zai"
    assert settings.model == "glm-5.2"


def test_resolve_inline_llm_settings_anthropic_provider_only_uses_claude_model(
    monkeypatch,
):
    monkeypatch.setitem(
        runtime_config,
        "inline_llm",
        {"default": {"provider": "anthropic"}},
    )

    settings = resolve_inline_llm_settings("compute_qa")

    assert settings.provider == "anthropic"
    assert settings.model == "claude-haiku-4-5-20251001"


@pytest.mark.asyncio
async def test_call_inline_llm_json_uses_runtime_config_override(monkeypatch):
    monkeypatch.setitem(
        runtime_config,
        "inline_llm",
        {
            "default": {
                "model": "claude-sonnet-4-6",
                "timeout_seconds": 12,
                "max_tokens": 999,
            },
            "compute_qa": {
                "max_tokens": 123,
            },
        },
    )

    mock_content = MagicMock()
    mock_content.text = json.dumps({"answer": "yes"})
    mock_resp = MagicMock()
    mock_resp.content = [mock_content]

    mock_create = AsyncMock(return_value=mock_resp)
    mock_client = MagicMock()
    mock_client.messages.create = mock_create

    with (
        patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}),
        patch("anthropic.AsyncAnthropic", MagicMock(return_value=mock_client)),
    ):
        result = await call_inline_llm_json(
            "compute_qa",
            "Is this working?",
            required_keys=("answer",),
        )

    assert result == {"answer": "yes"}
    call_kwargs = mock_create.call_args[1]
    assert call_kwargs["model"] == "claude-sonnet-4-6"
    assert call_kwargs["timeout"] == 12.0
    assert call_kwargs["max_tokens"] == 123


@pytest.mark.asyncio
async def test_call_inline_llm_json_uses_deepseek_default(monkeypatch):
    monkeypatch.setitem(runtime_config, "inline_llm", {})

    mock_content = MagicMock()
    mock_content.text = json.dumps({"answer": "yes"})
    mock_resp = MagicMock()
    mock_resp.content = [mock_content]

    mock_create = AsyncMock(return_value=mock_resp)
    mock_client = MagicMock()
    mock_client.messages.create = mock_create
    mock_client_cls = MagicMock(return_value=mock_client)

    with (
        patch.dict(os.environ, {"DEEPSEEK_API_KEY": "deepseek-test-key"}),
        patch("anthropic.AsyncAnthropic", mock_client_cls),
    ):
        result = await call_inline_llm_json(
            "compute_qa",
            "Is this working?",
            required_keys=("answer",),
        )

    assert result == {"answer": "yes"}
    mock_client_cls.assert_called_once_with(
        api_key="deepseek-test-key",
        base_url="https://api.deepseek.com/anthropic",
    )
    call_kwargs = mock_create.call_args[1]
    assert call_kwargs["model"] == "deepseek-v4-flash"


@pytest.mark.asyncio
async def test_call_inline_llm_json_prefers_deepseek_base_url_env_override(monkeypatch):
    monkeypatch.setitem(runtime_config, "inline_llm", {})

    mock_content = MagicMock()
    mock_content.text = json.dumps({"answer": "yes"})
    mock_resp = MagicMock()
    mock_resp.content = [mock_content]

    mock_create = AsyncMock(return_value=mock_resp)
    mock_client = MagicMock()
    mock_client.messages.create = mock_create
    mock_client_cls = MagicMock(return_value=mock_client)

    with (
        patch.dict(
            os.environ,
            {
                "DEEPSEEK_API_KEY": "deepseek-test-key",
                "DEEPSEEK_ANTHROPIC_BASE_URL": "https://proxy.test/deepseek/",
            },
        ),
        patch("anthropic.AsyncAnthropic", mock_client_cls),
    ):
        result = await call_inline_llm_json(
            "compute_qa",
            "Is this working?",
            required_keys=("answer",),
        )

    assert result == {"answer": "yes"}
    mock_client_cls.assert_called_once_with(
        api_key="deepseek-test-key",
        base_url="https://proxy.test/deepseek",
    )


@pytest.mark.asyncio
async def test_call_inline_llm_json_honors_kill_switch(monkeypatch):
    monkeypatch.setitem(runtime_config, "inline_llm", {})

    mock_client_cls = MagicMock()

    with (
        patch.dict(
            os.environ,
            {
                "ANTHROPIC_API_KEY": "sk-test",
                "DISABLE_LLM_COMPUTE_ANALYSIS": "1",
            },
        ),
        patch("anthropic.AsyncAnthropic", mock_client_cls),
    ):
        result = await call_inline_llm_json("compute_analysis", "Analyze this.")

    assert result is None
    mock_client_cls.assert_not_called()
