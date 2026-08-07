import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from app.inline_llm import call_inline_llm_json, resolve_inline_llm_settings
from app.models import InlineLlmSpendLog
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


@pytest.mark.asyncio
async def test_call_inline_llm_json_records_spend_log_from_provider_usage(
    monkeypatch, db_session
):
    """A provider-reported usage probe is priced and persisted to
    inline_llm_spend_log — the seam that was previously fully dark to cost
    attribution and cost_reconciliation.py's internal side."""
    monkeypatch.setitem(runtime_config, "inline_llm", {})

    mock_content = MagicMock()
    mock_content.text = json.dumps({"answer": "yes"})
    mock_resp = MagicMock()
    mock_resp.content = [mock_content]
    mock_resp.usage = SimpleNamespace(
        input_tokens=100,
        output_tokens=50,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )

    mock_create = AsyncMock(return_value=mock_resp)
    mock_client = MagicMock()
    mock_client.messages.create = mock_create

    with (
        patch.dict(os.environ, {"DEEPSEEK_API_KEY": "deepseek-test-key"}),
        patch("anthropic.AsyncAnthropic", MagicMock(return_value=mock_client)),
    ):
        result = await call_inline_llm_json(
            "compute_qa",
            "Is this working?",
            required_keys=("answer",),
        )

    assert result == {"answer": "yes"}

    # Filter on this call's distinguishing token counts rather than counting
    # the whole table: other tests in the same DB-backed session write their
    # own rows to this shared append-only ledger and are not this test's
    # concern (matches the isolation approach other spend/decision-log tests
    # in this suite use for append-only tables).
    rows = (
        await db_session.execute(
            select(InlineLlmSpendLog).where(
                InlineLlmSpendLog.purpose == "compute_qa",
                InlineLlmSpendLog.provider == "deepseek",
                InlineLlmSpendLog.input_tokens == 100,
                InlineLlmSpendLog.output_tokens == 50,
            )
        )
    ).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.model == "deepseek-v4-flash"
    assert row.pricing_status == "priced"
    assert row.cost_usd is not None
    # deepseek-v4-flash pricing: (0.14e-6, 0.28e-6) $/token
    assert row.cost_usd == pytest.approx(100 * 0.14e-6 + 50 * 0.28e-6)


@pytest.mark.asyncio
async def test_call_inline_llm_json_records_unpriced_model_usage_as_unpriced(
    monkeypatch, db_session
):
    """A call whose (provider, model) has no pricing row is RECORDED with its
    usage and marked unpriced (cost_usd NULL) rather than silently dropped —
    the dark-spend hole in the cost-capture seam this ledger exists to close.
    A consumer summing this ledger must read the total as a floor, not an exact
    figure, because the row's cost is unknown, not zero."""
    monkeypatch.setitem(
        runtime_config,
        "inline_llm",
        {"compute_qa": {"model": "deepseek-v999"}},
    )

    mock_content = MagicMock()
    mock_content.text = json.dumps({"answer": "yes"})
    mock_resp = MagicMock()
    mock_resp.content = [mock_content]
    mock_resp.usage = SimpleNamespace(
        input_tokens=200,
        output_tokens=60,
        cache_creation_input_tokens=10,
        cache_read_input_tokens=5,
    )

    mock_create = AsyncMock(return_value=mock_resp)
    mock_client = MagicMock()
    mock_client.messages.create = mock_create

    with (
        patch.dict(os.environ, {"DEEPSEEK_API_KEY": "deepseek-test-key"}),
        patch("anthropic.AsyncAnthropic", MagicMock(return_value=mock_client)),
    ):
        result = await call_inline_llm_json(
            "compute_qa",
            "Is this working?",
            required_keys=("answer",),
        )

    assert result == {"answer": "yes"}

    rows = (
        await db_session.execute(
            select(InlineLlmSpendLog).where(
                InlineLlmSpendLog.provider == "deepseek",
                InlineLlmSpendLog.model == "deepseek-v999",
                InlineLlmSpendLog.input_tokens == 215,  # 200 uncached + 10 cc + 5 cr
            )
        )
    ).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.pricing_status == "unpriced"
    assert row.cost_usd is None
    assert row.output_tokens == 60
    assert row.cache_creation_input_tokens == 10
    assert row.cache_read_input_tokens == 5


@pytest.mark.asyncio
async def test_call_inline_llm_json_no_usage_writes_no_spend_log(
    monkeypatch, db_session
):
    """A response with no usable usage block writes no spend-log row rather
    than fabricating a cost from unset fields."""
    monkeypatch.setitem(runtime_config, "inline_llm", {})

    before_count = len(
        (await db_session.execute(select(InlineLlmSpendLog))).scalars().all()
    )

    mock_content = MagicMock()
    mock_content.text = json.dumps({"answer": "yes"})
    mock_resp = SimpleNamespace(content=[mock_content])

    mock_create = AsyncMock(return_value=mock_resp)
    mock_client = MagicMock()
    mock_client.messages.create = mock_create

    with (
        patch.dict(os.environ, {"DEEPSEEK_API_KEY": "deepseek-test-key"}),
        patch("anthropic.AsyncAnthropic", MagicMock(return_value=mock_client)),
    ):
        result = await call_inline_llm_json(
            "compute_qa",
            "Is this working?",
            required_keys=("answer",),
        )

    assert result == {"answer": "yes"}

    after_count = len(
        (await db_session.execute(select(InlineLlmSpendLog))).scalars().all()
    )
    assert after_count == before_count
