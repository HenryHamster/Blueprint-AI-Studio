"""Tests for app.direct_llm_client — the shared direct-Anthropic call helper.

Covers: model constant, API-key getter, call_direct_llm happy path, and the
three call sites (admin._call_draft_llm, knowledge_completion._call_compute_analysis_llm,
review_learning.categorize_review_issues_llm) to confirm no behavioral regression
after the refactor that moved client construction here.
"""
import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# direct_llm_client unit tests
# ---------------------------------------------------------------------------

def test_direct_llm_model_constant():
    from app.direct_llm_client import DIRECT_LLM_MODEL
    assert DIRECT_LLM_MODEL == "claude-haiku-4-5-20251001"


def test_get_anthropic_api_key_present(monkeypatch):
    from app.direct_llm_client import get_anthropic_api_key
    monkeypatch.setenv("ANTHROPIC_API_KEY", "  sk-test  ")
    assert get_anthropic_api_key() == "sk-test"


def test_get_anthropic_api_key_absent(monkeypatch):
    from app.direct_llm_client import get_anthropic_api_key
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert get_anthropic_api_key() == ""


@pytest.mark.asyncio
async def test_call_direct_llm_returns_text():
    from app.direct_llm_client import call_direct_llm

    mock_content = MagicMock()
    mock_content.text = "  hello world  "
    mock_resp = MagicMock()
    mock_resp.content = [mock_content]

    mock_create = AsyncMock(return_value=mock_resp)
    mock_client = MagicMock()
    mock_client.messages.create = mock_create
    mock_client_cls = MagicMock(return_value=mock_client)

    with patch("anthropic.AsyncAnthropic", mock_client_cls):
        result = await call_direct_llm(
            "sk-test",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=100,
            timeout=5.0,
        )

    assert result == "hello world"
    mock_client_cls.assert_called_once_with(api_key="sk-test")
    call_kwargs = mock_create.call_args[1]
    assert call_kwargs["model"] == "claude-haiku-4-5-20251001"
    assert call_kwargs["max_tokens"] == 100
    assert call_kwargs["timeout"] == 5.0
    assert "system" not in call_kwargs


@pytest.mark.asyncio
async def test_call_direct_llm_passes_system_prompt():
    from app.direct_llm_client import call_direct_llm

    mock_content = MagicMock()
    mock_content.text = "answer"
    mock_resp = MagicMock()
    mock_resp.content = [mock_content]

    mock_create = AsyncMock(return_value=mock_resp)
    mock_client = MagicMock()
    mock_client.messages.create = mock_create

    with patch("anthropic.AsyncAnthropic", MagicMock(return_value=mock_client)):
        await call_direct_llm(
            "sk-test",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=50,
            timeout=5.0,
            system="Be concise.",
        )

    call_kwargs = mock_create.call_args[1]
    assert call_kwargs["system"] == "Be concise."


@pytest.mark.asyncio
async def test_call_direct_llm_raises_on_api_error():
    from app.direct_llm_client import call_direct_llm

    with patch(
        "anthropic.AsyncAnthropic",
        MagicMock(side_effect=Exception("network failure")),
    ):
        with pytest.raises(Exception, match="network failure"):
            await call_direct_llm(
                "sk-test",
                messages=[{"role": "user", "content": "hi"}],
                max_tokens=50,
                timeout=5.0,
            )


# ---------------------------------------------------------------------------
# Regression: review_learning.categorize_review_issues_llm uses shared helper
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_categorize_review_issues_llm_uses_shared_model():
    """After refactor, categorize_review_issues_llm calls Anthropic with DIRECT_LLM_MODEL."""
    from app.direct_llm_client import DIRECT_LLM_MODEL
    from app.review_learning import categorize_review_issues_llm

    issues = [{"description": "SQL injection risk", "severity": "CRITICAL", "iteration": 1}]
    fake_text = json.dumps({"classifications": [{"index": 0, "category": "security"}]})

    mock_content = MagicMock()
    mock_content.text = fake_text
    mock_resp = MagicMock()
    mock_resp.content = [mock_content]

    mock_create = AsyncMock(return_value=mock_resp)
    mock_client = MagicMock()
    mock_client.messages.create = mock_create

    with (
        patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}),
        patch("anthropic.AsyncAnthropic", MagicMock(return_value=mock_client)),
    ):
        result = await categorize_review_issues_llm(issues)

    assert "security" in result
    call_kwargs = mock_create.call_args[1]
    assert call_kwargs["model"] == DIRECT_LLM_MODEL


# ---------------------------------------------------------------------------
# Regression: admin._call_draft_llm uses shared helper
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_call_draft_llm_uses_shared_model():
    """After refactor, _call_draft_llm calls Anthropic with DIRECT_LLM_MODEL."""
    from app.direct_llm_client import DIRECT_LLM_MODEL
    from app.admin import _call_draft_llm

    good_draft = {
        "title": "Test", "prompt": "run it", "hypothesis": "it works",
        "command": "python test.py", "arms": None, "declared_metric": None,
        "spend_cap_usd": 2.0, "deadline_seconds": 1800,
        "experiment": "test-exp", "benchmark": None, "metric": None,
        "success_criterion": None, "accelerator": None, "machine_type": None,
    }
    fake_text = json.dumps(good_draft)

    mock_content = MagicMock()
    mock_content.text = fake_text
    mock_resp = MagicMock()
    mock_resp.content = [mock_content]

    mock_create = AsyncMock(return_value=mock_resp)
    mock_client = MagicMock()
    mock_client.messages.create = mock_create

    with patch("anthropic.AsyncAnthropic", MagicMock(return_value=mock_client)):
        result = await _call_draft_llm("sk-test", "run an experiment", None, None)

    assert result is not None
    call_kwargs = mock_create.call_args[1]
    assert call_kwargs["model"] == DIRECT_LLM_MODEL


# ---------------------------------------------------------------------------
# Regression: knowledge_completion._call_compute_analysis_llm uses shared helper
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_call_compute_analysis_llm_uses_shared_model():
    """After refactor, _call_compute_analysis_llm calls Anthropic with DIRECT_LLM_MODEL."""
    from app.direct_llm_client import DIRECT_LLM_MODEL
    from app.knowledge_completion import _call_compute_analysis_llm

    good_analysis = {"title": "Results", "summary": "Good", "body": "Details here"}
    fake_text = json.dumps(good_analysis)

    mock_content = MagicMock()
    mock_content.text = fake_text
    mock_resp = MagicMock()
    mock_resp.content = [mock_content]

    mock_create = AsyncMock(return_value=mock_resp)
    mock_client = MagicMock()
    mock_client.messages.create = mock_create

    with patch("anthropic.AsyncAnthropic", MagicMock(return_value=mock_client)):
        result = await _call_compute_analysis_llm("sk-test", "Analyze this experiment.")

    assert result == good_analysis
    call_kwargs = mock_create.call_args[1]
    assert call_kwargs["model"] == DIRECT_LLM_MODEL
