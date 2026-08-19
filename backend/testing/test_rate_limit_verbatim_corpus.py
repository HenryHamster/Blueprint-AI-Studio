"""Verbatim rate-limit corpus across OpenAI, Gemini, and Codex CLI surfaces.

Strings are drawn directly from production-observed 429 bodies, Codex CLI
wrapper text, Gemini CLI wrappers, and Gemini Code Assist / Vertex AI
responses documented in the external-providers research memo.

Two things are checked per entry:
  1. The provider's classify_rate_limit_signal(auth_mode, text) returns a
     RateLimitSignal with the expected kind (rate_limit vs quota_exhausted).
  2. When a server-provided retry hint is present in the text,
     signal.retry_after_seconds reflects it (or is None otherwise).

These tests pin the pattern contract — any future trim of patterns that
regresses real-traffic matches will break here loudly.
"""

from __future__ import annotations

import pytest

from app.models import LLMAuthMode, LLMProviderName
from app.providers.registry import get_provider


# (provider, auth_mode, verbatim_text, expected_kind, expected_retry_seconds_or_None)
OPENAI_CASES = [
    # ── OpenAI API key (sk-) ──────────────────────────────────────────────
    (
        LLMProviderName.OPENAI,
        LLMAuthMode.API_KEY,
        "You've exceeded the rate limit, please slow down and try again after 60.616292 seconds.",
        "rate_limit",
        60,
    ),
    (
        LLMProviderName.OPENAI,
        LLMAuthMode.API_KEY,
        "Rate limit reached for o4-mini in organization org-xxx on tokens per min (TPM): "
        "Limit 200000, Used 162960, Requested 42288. Please try again in 1.574s.",
        "rate_limit",
        1,
    ),
    (
        LLMProviderName.OPENAI,
        LLMAuthMode.API_KEY,
        "You exceeded your current quota, please check your plan and billing details.",
        "quota_exhausted",
        None,
    ),
    (
        LLMProviderName.OPENAI,
        LLMAuthMode.API_KEY,
        '{"error":{"message":"...","type":"insufficient_quota","code":"insufficient_quota"}}',
        "quota_exhausted",
        None,
    ),
    # ── OpenAI ChatGPT / Codex OAuth ─────────────────────────────────────
    (
        LLMProviderName.OPENAI,
        LLMAuthMode.OAUTH,
        "You're out of Codex messages  Your rate limit resets on 2:49 PM. "
        "To continue using Codex, add credits or upgrade to Pro today.",
        "quota_exhausted",
        None,
    ),
    (
        # Was misclassified as "rate_limit" (near-miss of the sibling
        # "manage or purchase credits" quota pattern — this string says "to
        # purchase more credits" instead). This is a billing wall, not a
        # transient throttle: the reset is an absolute timestamp, not a
        # window that clears on its own. See task 833f8a29 (2026-08-19).
        LLMProviderName.OPENAI,
        LLMAuthMode.OAUTH,
        "■ You've hit your usage limit. Visit https://chatgpt.com/codex/settings/usage "
        "to purchase more credits or try again at Apr 17th, 2026 6:10 AM.",
        "quota_exhausted",
        None,
    ),
    (
        # Verbatim text observed in task 833f8a29 failure_reason
        # (2026-08-19T05:09Z) — the exact billing-wall wall that triggered
        # this fix. Pinned literally, not paraphrased.
        LLMProviderName.OPENAI,
        LLMAuthMode.OAUTH,
        "Fatal error: OpenAI Codex API usage exhausted - Reading prompt from stdin...\n"
        "[codex] error: You've hit your usage limit. Visit "
        "https://chatgpt.com/codex/settings/usage to purchase more credits or "
        "try again at Aug 20th, 2026 3:30 AM.",
        "quota_exhausted",
        None,
    ),
    (
        LLMProviderName.OPENAI,
        LLMAuthMode.OAUTH,
        "⚠ Insufficient quota: You exceeded your current quota, please check your plan and billing details. "
        "Manage or purchase credits at https://platform.openai.com/account/billing.",
        "quota_exhausted",
        None,
    ),
    (
        LLMProviderName.OPENAI,
        LLMAuthMode.OAUTH,
        "ERROR: exceeded retry limit, last status: 429 Too Many Requests",
        "rate_limit",
        None,
    ),
    # Agent-invented RateLimitError wording (runners.ts) — observed verbatim in
    # status_log when a Codex usage window is spent. Must classify as rate_limit
    # so the backend cools the account + reroutes (incident 2026-06-03).
    (
        LLMProviderName.OPENAI,
        LLMAuthMode.OAUTH,
        "Fatal error: OpenAI Codex API usage exhausted — Reading prompt from stdin",
        "rate_limit",
        None,
    ),
    (
        LLMProviderName.OPENAI,
        LLMAuthMode.API_KEY,
        "OpenAI API usage exhausted — last status: 429",
        "rate_limit",
        None,
    ),
]

GEMINI_CASES = [
    # ── Gemini API key ────────────────────────────────────────────────────
    (
        LLMProviderName.GEMINI,
        LLMAuthMode.API_KEY,
        "You exceeded your current quota, please check your plan and billing details. "
        "Quota exceeded for metric: generativelanguage.googleapis.com/generate_content_free_tier_input_token_count, "
        "limit: 250000, model: gemini-2.5-flash. Please retry in 39.844676573s.",
        "quota_exhausted",
        39,
    ),
    (
        LLMProviderName.GEMINI,
        LLMAuthMode.API_KEY,
        '✕ [API Error: {"error":{"code":429,"status":"Too Many Requests"}}] '
        "Please wait and try again later. To increase your limits, request a quota increase "
        "through AI Studio, or switch to another /auth method",
        "rate_limit",
        None,
    ),
    (
        LLMProviderName.GEMINI,
        LLMAuthMode.API_KEY,
        "Quota exceeded for quota metric 'Gemini 2.5 Pro Requests' and limit "
        "'Gemini 2.5 Pro Requests per day per user per tier' of service 'cloudcode-pa.googleapis.com'.",
        "rate_limit",
        None,
    ),
    (
        LLMProviderName.GEMINI,
        LLMAuthMode.API_KEY,
        "Possible quota limitations in place or slow response times detected. "
        "Switching to the gemini-2.5-flash model for the rest of this session.",
        "rate_limit",
        None,
    ),
    (
        LLMProviderName.GEMINI,
        LLMAuthMode.API_KEY,
        "Resource has been exhausted (e.g. check quota)",
        "rate_limit",
        None,
    ),
    # ── Gemini service account (Vertex AI) ───────────────────────────────
    (
        LLMProviderName.GEMINI,
        LLMAuthMode.SERVICE_ACCOUNT,
        "Resource exhausted. Please try again later. "
        "Please refer to https://cloud.google.com/vertex-ai/generative-ai/docs/error-code-429 for more details.",
        "rate_limit",
        None,
    ),
    (
        LLMProviderName.GEMINI,
        LLMAuthMode.SERVICE_ACCOUNT,
        "Quota exceeded for metric: aiplatform.googleapis.com/generate_content_requests",
        "rate_limit",
        None,
    ),
]

DEEPSEEK_ZAI_CASES = [
    (
        LLMProviderName.DEEPSEEK,
        LLMAuthMode.API_KEY,
        "HTTP 401 Unauthorized from api.deepseek.com: authentication fails: insufficient_balance",
        "quota_exhausted",
        None,
    ),
    (
        LLMProviderName.DEEPSEEK,
        LLMAuthMode.API_KEY,
        '{"error":{"type":"rate_limit","message":"Too many requests"}}',
        "rate_limit",
        None,
    ),
    (
        LLMProviderName.ZAI,
        LLMAuthMode.API_KEY,
        "HTTP 401 Unauthorized from api.z.ai: authentication_failed: insufficient balance",
        "quota_exhausted",
        None,
    ),
    (
        LLMProviderName.ZAI,
        LLMAuthMode.API_KEY,
        '{"error":{"code":429,"message":"rate_limit exceeded"}}',
        "rate_limit",
        None,
    ),
]


@pytest.mark.parametrize(
    "provider,auth_mode,text,expected_kind,expected_retry",
    OPENAI_CASES + GEMINI_CASES + DEEPSEEK_ZAI_CASES,
)
def test_classify_rate_limit_signal_verbatim(
    provider, auth_mode, text, expected_kind, expected_retry
):
    p = get_provider(provider)
    signal = p.classify_rate_limit_signal(auth_mode, text)
    assert signal is not None, (
        f"No match for {provider.value}/{auth_mode.value}: {text[:80]!r}"
    )
    assert signal.kind == expected_kind, (
        f"Expected kind={expected_kind} for {provider.value}/{auth_mode.value}: "
        f"{text[:80]!r}; got {signal.kind} (matched={signal.matched_pattern!r})"
    )
    assert signal.retry_after_seconds == expected_retry, (
        f"Expected retry_after_seconds={expected_retry} for {text[:80]!r}; "
        f"got {signal.retry_after_seconds}"
    )


@pytest.mark.parametrize(
    "provider,auth_mode,text",
    [
        # Generic errors must NOT match any provider's rate-limit classifier.
        (
            LLMProviderName.OPENAI,
            LLMAuthMode.API_KEY,
            "AssertionError: expected 3, got 4",
        ),
        (LLMProviderName.OPENAI, LLMAuthMode.OAUTH, "ECONNREFUSED connecting to api"),
        (LLMProviderName.GEMINI, LLMAuthMode.API_KEY, "Could not resolve hostname"),
        (LLMProviderName.GEMINI, LLMAuthMode.SERVICE_ACCOUNT, "401 unauthorized"),
        # Anthropic 529 overload is NOT a rate limit.
        (LLMProviderName.OPENAI, LLMAuthMode.OAUTH, "Error: Overloaded"),
        # GCP disk quota is NOT a rate limit (it's a provision failure).
        (
            LLMProviderName.GEMINI,
            LLMAuthMode.SERVICE_ACCOUNT,
            "disk quota exceeded on /var",
        ),
    ],
)
def test_classify_rate_limit_signal_no_false_positives(provider, auth_mode, text):
    p = get_provider(provider)
    signal = p.classify_rate_limit_signal(auth_mode, text)
    assert signal is None, (
        f"False positive: {provider.value}/{auth_mode.value} matched "
        f"{text!r} as {signal!r}"
    )
