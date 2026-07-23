"""Tests for app.direct_llm_client — the shared direct Anthropic-compatible helper.

Covers: model constant, API-key getter, call_direct_llm happy path, and the
three call sites (admin._call_draft_llm, knowledge_completion._call_compute_analysis_llm,
review_learning.categorize_review_issues_llm) to confirm no behavioral regression
after the refactor that moved client construction here.
"""

import json
import os
from types import SimpleNamespace
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


# ---------------------------------------------------------------------------
# Rate-limit header extraction + normalization
# ---------------------------------------------------------------------------


def test_extract_rate_limit_headers_empty_when_no_headers():
    """Returns empty dict when response has no headers attribute."""
    from app.direct_llm_client import extract_rate_limit_headers

    mock_resp = SimpleNamespace(headers=None)
    assert extract_rate_limit_headers(mock_resp) == {}

    # Also works when headers attribute is absent (e.g. test stubs)
    mock_resp = SimpleNamespace()
    with patch.object(mock_resp, "headers", None, create=True) or True:
        pass
    # Just test with an object that has no headers at all
    result = extract_rate_limit_headers(SimpleNamespace())
    assert result == {}


def test_extract_rate_limit_headers_filters_relevant_headers():
    from app.direct_llm_client import extract_rate_limit_headers

    mock_headers = {
        "anthropic-ratelimit-requests-remaining": "10",
        "anthropic-ratelimit-requests-limit": "100",
        "anthropic-ratelimit-tokens-remaining": "5000",
        "anthropic-ratelimit-unified-5h-status": "live",
        "anthropic-ratelimit-unified-5h-remaining": "800000",
        "anthropic-ratelimit-unified-5h-reset": "2026-07-09T15:00:00Z",
        "content-type": "application/json",
        "x-request-id": "req_123",
    }
    mock_resp = SimpleNamespace(headers=mock_headers)
    result = extract_rate_limit_headers(mock_resp)

    assert "anthropic-ratelimit-requests-remaining" in result
    assert "anthropic-ratelimit-unified-5h-remaining" in result
    assert "content-type" not in result
    assert "x-request-id" not in result
    assert len(result) == 6  # All 6 rate-limit headers


def test_extract_rate_limit_headers_case_insensitive():
    from app.direct_llm_client import extract_rate_limit_headers

    mock_headers = {
        "ANTHROPIC-RATELIMIT-UNIFIED-5H-STATUS": "exhausted",
        "Anthropic-RateLimit-Tokens-Remaining": "1000",
    }
    mock_resp = SimpleNamespace(headers=mock_headers)
    result = extract_rate_limit_headers(mock_resp)

    assert "ANTHROPIC-RATELIMIT-UNIFIED-5H-STATUS" in result
    assert "Anthropic-RateLimit-Tokens-Remaining" in result


def test_normalize_rate_limit_data_with_all_headers():
    from app.direct_llm_client import normalize_rate_limit_data

    raw_headers = {
        "anthropic-ratelimit-requests-remaining": "10",
        "anthropic-ratelimit-requests-limit": "100",
        "anthropic-ratelimit-requests-reset": "2026-07-09T15:00:00Z",
        "anthropic-ratelimit-tokens-remaining": "5000",
        "anthropic-ratelimit-tokens-limit": "50000",
        "anthropic-ratelimit-tokens-reset": "2026-07-09T15:00:00Z",
        "anthropic-ratelimit-unified-5h-status": "live",
        "anthropic-ratelimit-unified-5h-remaining": "800000",
        "anthropic-ratelimit-unified-5h-reset": "2026-07-09T18:00:00Z",
    }
    result = normalize_rate_limit_data(raw_headers)

    assert result["source"] == "messages_api_header"
    assert "updated_at" in result
    assert "unified_5h" in result
    assert result["unified_5h"]["remaining_tokens"] == 800000
    assert result["unified_5h"]["status"] == "live"
    assert result["unified_5h"]["resets_at"] == "2026-07-09T18:00:00Z"
    # Utilization computed: (1 - 800000/limit) but no limit header → utilization absent
    assert "utilization" not in result["unified_5h"]

    assert result["requests"]["remaining"] == 10
    assert result["requests"]["limit"] == 100
    assert result["tokens"]["remaining"] == 5000
    assert result["tokens"]["limit"] == 50000


def test_normalize_rate_limit_data_keeps_token_families_separate():
    from app.direct_llm_client import normalize_rate_limit_data

    raw_headers = {
        "anthropic-ratelimit-tokens-remaining": "5000",
        "anthropic-ratelimit-tokens-limit": "50000",
        "anthropic-ratelimit-input-tokens-remaining": "111",
        "anthropic-ratelimit-input-tokens-limit": "222",
        "anthropic-ratelimit-output-tokens-remaining": "333",
        "anthropic-ratelimit-output-tokens-limit": "444",
    }
    result = normalize_rate_limit_data(raw_headers)

    assert result["tokens"]["remaining"] == 5000
    assert result["tokens"]["limit"] == 50000


def test_normalize_rate_limit_data_with_limit_and_utilization():
    from app.direct_llm_client import normalize_rate_limit_data

    raw_headers = {
        "anthropic-ratelimit-unified-5h-remaining": "400000",
        "anthropic-ratelimit-unified-5h-limit": "1000000",
        "anthropic-ratelimit-unified-5h-status": "live",
        "anthropic-ratelimit-unified-5h-reset": "2026-07-09T18:00:00Z",
    }
    result = normalize_rate_limit_data(raw_headers)

    assert result["unified_5h"]["remaining_tokens"] == 400000
    assert result["unified_5h"]["limit_tokens"] == 1000000
    # (1 - 400000/1000000) * 100 = 60.0
    assert result["unified_5h"]["utilization"] == 60.0


def test_normalize_rate_limit_data_empty_headers():
    from app.direct_llm_client import normalize_rate_limit_data

    result = normalize_rate_limit_data({})
    assert result["source"] == "messages_api_header"
    assert "unified_5h" not in result
    assert "requests" not in result
    assert "tokens" not in result


def test_normalize_rate_limit_data_exhausted_status():
    from app.direct_llm_client import normalize_rate_limit_data

    raw_headers = {
        "anthropic-ratelimit-unified-5h-status": "exhausted",
        "anthropic-ratelimit-unified-5h-remaining": "0",
        "anthropic-ratelimit-unified-5h-limit": "1000000",
        "anthropic-ratelimit-unified-5h-reset": "2026-07-09T18:00:00Z",
    }
    result = normalize_rate_limit_data(raw_headers)

    assert result["unified_5h"]["remaining_tokens"] == 0
    assert result["unified_5h"]["status"] == "exhausted"
    assert result["unified_5h"]["utilization"] == 100.0


def test_normalize_rate_limit_data_with_unified_7d_headers():
    """unified_7d bucket is extracted and utilization computed from 7d headers."""
    from app.direct_llm_client import normalize_rate_limit_data

    raw_headers = {
        "anthropic-ratelimit-unified-7d-remaining": "3000000",
        "anthropic-ratelimit-unified-7d-limit": "10000000",
        "anthropic-ratelimit-unified-7d-status": "live",
        "anthropic-ratelimit-unified-7d-reset": "2026-07-16T00:00:00Z",
    }
    result = normalize_rate_limit_data(raw_headers)

    assert "unified_7d" in result
    assert result["unified_7d"]["remaining_tokens"] == 3000000
    assert result["unified_7d"]["limit_tokens"] == 10000000
    assert result["unified_7d"]["status"] == "live"
    assert result["unified_7d"]["resets_at"] == "2026-07-16T00:00:00Z"
    # (1 - 3000000/10000000) * 100 = 70.0
    assert result["unified_7d"]["utilization"] == 70.0


def test_normalize_rate_limit_data_with_both_unified_windows():
    """Both unified_5h and unified_7d buckets are populated simultaneously."""
    from app.direct_llm_client import normalize_rate_limit_data

    raw_headers = {
        "anthropic-ratelimit-unified-5h-remaining": "400000",
        "anthropic-ratelimit-unified-5h-limit": "1000000",
        "anthropic-ratelimit-unified-5h-status": "live",
        "anthropic-ratelimit-unified-5h-reset": "2026-07-09T18:00:00Z",
        "anthropic-ratelimit-unified-7d-remaining": "6000000",
        "anthropic-ratelimit-unified-7d-limit": "10000000",
        "anthropic-ratelimit-unified-7d-status": "live",
        "anthropic-ratelimit-unified-7d-reset": "2026-07-16T00:00:00Z",
    }
    result = normalize_rate_limit_data(raw_headers)

    assert result["unified_5h"]["utilization"] == 60.0
    assert result["unified_5h"]["remaining_tokens"] == 400000
    assert result["unified_7d"]["utilization"] == 40.0
    assert result["unified_7d"]["remaining_tokens"] == 6000000


# ---------------------------------------------------------------------------
# call_direct_llm — rate-limit header extraction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_call_direct_llm_returns_rate_limit_data():
    """call_direct_llm now returns (text, rate_limit_data) including headers."""
    from app.direct_llm_client import call_direct_llm

    mock_content = MagicMock()
    mock_content.text = "hello world"
    mock_headers = {
        "anthropic-ratelimit-unified-5h-status": "live",
        "anthropic-ratelimit-unified-5h-remaining": "500000",
    }
    mock_resp = MagicMock()
    mock_resp.content = [mock_content]
    mock_resp.headers = mock_headers

    mock_create = AsyncMock(return_value=mock_resp)
    mock_client = MagicMock()
    mock_client.messages.create = mock_create

    with patch("anthropic.AsyncAnthropic", MagicMock(return_value=mock_client)):
        text, rate_limit_data = await call_direct_llm(
            "sk-test",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=100,
            timeout=5.0,
        )

    assert text == "hello world"
    assert rate_limit_data["source"] == "messages_api_header"
    assert rate_limit_data["unified_5h"]["status"] == "live"
    assert rate_limit_data["unified_5h"]["remaining_tokens"] == 500000
    mock_client.messages.create.assert_called_once()


@pytest.mark.asyncio
async def test_call_direct_llm_no_headers_on_mock():
    """When mock response has no headers attribute, rate_limit_data is empty."""
    from app.direct_llm_client import call_direct_llm

    mock_content = MagicMock()
    mock_content.text = "answer"
    mock_resp = MagicMock(spec=[])  # No headers
    mock_resp.content = [mock_content]
    # Remove the headers attribute that MagicMock auto-creates
    if hasattr(mock_resp, "headers"):
        del mock_resp.headers

    mock_create = AsyncMock(return_value=mock_resp)
    mock_client = MagicMock()
    mock_client.messages.create = mock_create

    with patch("anthropic.AsyncAnthropic", MagicMock(return_value=mock_client)):
        text, rate_limit_data = await call_direct_llm(
            "sk-test",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=100,
            timeout=5.0,
        )

    assert text == "answer"
    assert rate_limit_data["source"] == "messages_api_header"
    assert "unified_5h" not in rate_limit_data


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
        result, _ = await call_direct_llm(
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
async def test_call_direct_llm_skips_non_text_blocks():
    from app.direct_llm_client import call_direct_llm

    mock_resp = MagicMock()
    mock_resp.content = [
        SimpleNamespace(type="thinking", thinking="reasoning"),
        SimpleNamespace(type="text", text=" OK "),
    ]

    mock_create = AsyncMock(return_value=mock_resp)
    mock_client = MagicMock()
    mock_client.messages.create = mock_create

    with patch("anthropic.AsyncAnthropic", MagicMock(return_value=mock_client)):
        result, _ = await call_direct_llm(
            "sk-test",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=100,
            timeout=5.0,
        )

    assert result == "OK"


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
async def test_call_direct_llm_passes_base_url_to_client():
    from app.direct_llm_client import call_direct_llm

    mock_content = MagicMock()
    mock_content.text = "answer"
    mock_resp = MagicMock()
    mock_resp.content = [mock_content]

    mock_create = AsyncMock(return_value=mock_resp)
    mock_client = MagicMock()
    mock_client.messages.create = mock_create
    mock_client_cls = MagicMock(return_value=mock_client)

    with patch("anthropic.AsyncAnthropic", mock_client_cls):
        await call_direct_llm(
            "deepseek-test-key",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=50,
            timeout=5.0,
            base_url="https://api.deepseek.com/anthropic",
        )

    mock_client_cls.assert_called_once_with(
        api_key="deepseek-test-key",
        base_url="https://api.deepseek.com/anthropic",
    )


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
async def test_categorize_review_issues_llm_uses_shared_model(monkeypatch):
    """Anthropic fallback still uses DIRECT_LLM_MODEL when no DeepSeek key exists."""
    from app.direct_llm_client import DIRECT_LLM_MODEL
    from app.review_learning import categorize_review_issues_llm

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    issues = [
        {"description": "SQL injection risk", "severity": "CRITICAL", "iteration": 1}
    ]
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
async def test_call_draft_llm_uses_shared_model(monkeypatch):
    """Anthropic fallback still uses DIRECT_LLM_MODEL when no DeepSeek key exists."""
    from app.direct_llm_client import DIRECT_LLM_MODEL
    from app.admin import _call_draft_llm

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    good_draft = {
        "title": "Test",
        "prompt": "run it",
        "hypothesis": "it works",
        "command": "python test.py",
        "arms": None,
        "declared_metric": None,
        "spend_cap_usd": 2.0,
        "deadline_seconds": 1800,
        "experiment": "test-exp",
        "benchmark": None,
        "metric": None,
        "success_criterion": None,
        "accelerator": None,
        "machine_type": None,
    }
    fake_text = json.dumps(good_draft)

    mock_content = MagicMock()
    mock_content.text = fake_text
    mock_resp = MagicMock()
    mock_resp.content = [mock_content]

    mock_create = AsyncMock(return_value=mock_resp)
    mock_client = MagicMock()
    mock_client.messages.create = mock_create

    with (
        patch.dict(
            os.environ, {"ANTHROPIC_API_KEY": "sk-test", "DEEPSEEK_API_KEY": ""}
        ),
        patch("anthropic.AsyncAnthropic", MagicMock(return_value=mock_client)),
    ):
        result = await _call_draft_llm("run an experiment", None, None)

    assert result is not None
    call_kwargs = mock_create.call_args[1]
    assert call_kwargs["model"] == DIRECT_LLM_MODEL


# ---------------------------------------------------------------------------
# Regression: knowledge_completion._call_compute_analysis_llm uses shared helper
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_call_compute_analysis_llm_uses_shared_model(monkeypatch):
    """Anthropic fallback still uses DIRECT_LLM_MODEL when no DeepSeek key exists."""
    from app.direct_llm_client import DIRECT_LLM_MODEL
    from app.knowledge_completion import _call_compute_analysis_llm

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    good_analysis = {"title": "Results", "summary": "Good", "body": "Details here"}
    fake_text = json.dumps(good_analysis)

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
        result = await _call_compute_analysis_llm("Analyze this experiment.")

    assert result == good_analysis
    call_kwargs = mock_create.call_args[1]
    assert call_kwargs["model"] == DIRECT_LLM_MODEL
