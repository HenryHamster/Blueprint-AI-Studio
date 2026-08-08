"""Direct Anthropic-compatible API call helpers shared across backend inline LLM users.

Each call site manages its own disable-env checks, api-key availability checks,
retry-on-parse-error logic, and caller-specific fallback semantics. This module
centralises the three pieces that are byte-for-byte identical across all three
sites: the hardcoded model constant, ANTHROPIC_API_KEY env reading, and
AsyncAnthropic client construction + messages.create invocation. ``base_url``
lets callers target Anthropic-compatible providers such as DeepSeek.
"""

from __future__ import annotations

import os
from typing import Any

# Shared model for all direct (non-provider-stack) Anthropic calls.
# Do not drive this from runtime_config — that is the scope of in-flight task e61e6de8.
DIRECT_LLM_MODEL = "claude-haiku-4-5-20251001"

# Prefix for Anthropic rate-limit response headers.
_RATE_LIMIT_HEADER_PREFIX = "anthropic-ratelimit-"
_RATE_LIMIT_TOKENS_HEADER_PREFIX = f"{_RATE_LIMIT_HEADER_PREFIX}tokens-"


def get_anthropic_api_key() -> str:
    """Return the stripped ANTHROPIC_API_KEY env value (empty string if not set)."""
    return os.environ.get("ANTHROPIC_API_KEY", "").strip()


def extract_rate_limit_headers(
    response: Any,
) -> dict[str, str]:
    """Extract Anthropic rate-limit/usage headers from a messages.create response.

    Every Anthropic Messages API response carries rate-limit headers:
      - anthropic-ratelimit-requests-remaining / -limit / -reset
      - anthropic-ratelimit-tokens-remaining / -limit / -reset
      - anthropic-ratelimit-unified-5h-status / -remaining / -reset (/-limit)

    This is a best-effort extraction: header values are case-insensitive but the
    prefix is matched lowercased. Returns a flat dict of ``{header_name: value}``
    for every header starting with the rate-limit prefix; returns empty dict when
    the response object has no ``headers`` attribute (e.g. test stubs).
    """
    headers = getattr(response, "headers", None)
    if headers is None:
        # Some test stubs or mock responses may not have a headers attribute.
        return {}
    result: dict[str, str] = {}
    try:
        for key, value in headers.items():
            if key.lower().startswith(_RATE_LIMIT_HEADER_PREFIX):
                result[str(key)] = str(value)
    except (AttributeError, TypeError, ValueError):
        # Non-iterable or non-dict-like headers object.
        pass
    return result


def parse_rate_limit_header_value(raw: str) -> str | int | None:
    """Parse a rate-limit header value to int or return as-is.

    The unified-5h-remaining and unified-5h-limit headers are numeric (tokens).
    Other headers may be ISO timestamps (reset) or string status (status).
    """
    stripped = raw.strip()
    try:
        return int(stripped)
    except (ValueError, TypeError):
        if stripped:
            return stripped
        return None


def normalize_rate_limit_data(
    raw_headers: dict[str, str],
) -> dict[str, Any]:
    """Normalize raw rate-limit headers into a structured dict for persistence.

    Returns a dict suitable for storing in MaxPlanAccount.rate_limit_usage:

    .. code-block:: python

        {
            "unified_5h": {
                "remaining_tokens": int,
                "limit_tokens": int,
                "status": str,           # "live" | "near_limit" | "exhausted"
                "resets_at": str,        # ISO-8601
            },
            "requests": {
                "remaining": int,
                "limit": int,
                "resets_at": str,
            },
            "tokens": {
                "remaining": int,
                "limit": int,
                "resets_at": str,
            },
            "updated_at": str,           # ISO-8601
            "source": "messages_api_header",
        }
    """
    from datetime import datetime, timezone

    normalized: dict[str, Any] = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "source": "messages_api_header",
    }

    # Unified 5h window
    unified_5h: dict[str, Any] = {}
    for key, raw_val in raw_headers.items():
        lk = key.lower()
        if "unified-5h" not in lk:
            continue
        val = parse_rate_limit_header_value(raw_val)
        if "remaining" in lk and isinstance(val, int):
            unified_5h["remaining_tokens"] = val
        elif "limit" in lk and isinstance(val, int):
            unified_5h["limit_tokens"] = val
        elif "status" in lk and isinstance(val, str):
            unified_5h["status"] = val
        elif "reset" in lk and isinstance(val, str):
            unified_5h["resets_at"] = val
    if unified_5h:
        if (
            "remaining_tokens" in unified_5h
            and "limit_tokens" in unified_5h
            and unified_5h["limit_tokens"] > 0
        ):
            utilization = (
                1.0 - unified_5h["remaining_tokens"] / unified_5h["limit_tokens"]
            ) * 100.0
            unified_5h["utilization"] = round(utilization, 1)
        normalized["unified_5h"] = unified_5h

    # Unified 7d window (API-key accounts only; absent on OAuth responses)
    unified_7d: dict[str, Any] = {}
    for key, raw_val in raw_headers.items():
        lk = key.lower()
        if "unified-7d" not in lk:
            continue
        val = parse_rate_limit_header_value(raw_val)
        if "remaining" in lk and isinstance(val, int):
            unified_7d["remaining_tokens"] = val
        elif "limit" in lk and isinstance(val, int):
            unified_7d["limit_tokens"] = val
        elif "status" in lk and isinstance(val, str):
            unified_7d["status"] = val
        elif "reset" in lk and isinstance(val, str):
            unified_7d["resets_at"] = val
    if unified_7d:
        if (
            "remaining_tokens" in unified_7d
            and "limit_tokens" in unified_7d
            and unified_7d["limit_tokens"] > 0
        ):
            utilization = (
                1.0 - unified_7d["remaining_tokens"] / unified_7d["limit_tokens"]
            ) * 100.0
            unified_7d["utilization"] = round(utilization, 1)
        normalized["unified_7d"] = unified_7d

    # Per-request rate-limit
    requests: dict[str, Any] = {}
    for key, raw_val in raw_headers.items():
        lk = key.lower()
        if "requests-" not in lk:
            continue
        val = parse_rate_limit_header_value(raw_val)
        if "remaining" in lk and isinstance(val, int):
            requests["remaining"] = val
        elif "limit" in lk and isinstance(val, int):
            requests["limit"] = val
        elif "reset" in lk and isinstance(val, str):
            requests["resets_at"] = val
    if requests:
        normalized["requests"] = requests

    # Per-token rate-limit
    tokens: dict[str, Any] = {}
    for key, raw_val in raw_headers.items():
        lk = key.lower()
        if not lk.startswith(_RATE_LIMIT_TOKENS_HEADER_PREFIX):
            continue
        val = parse_rate_limit_header_value(raw_val)
        if "remaining" in lk and isinstance(val, int):
            tokens["remaining"] = val
        elif "limit" in lk and isinstance(val, int):
            tokens["limit"] = val
        elif "reset" in lk and isinstance(val, str):
            tokens["resets_at"] = val
    if tokens:
        normalized["tokens"] = tokens

    return normalized


def extract_usage_data(response: Any) -> dict[str, Any] | None:
    """Extract ``response.usage`` into the ``provider_reported_usage`` shape.

    Returns a dict with ``input_tokens``, ``output_tokens``,
    ``cache_creation_input_tokens``, ``cache_read_input_tokens`` (all ints) and
    ``reported: True`` — the same shape ``Task.provider_reported_usage`` uses,
    so callers can price it with the shared pricing helpers. Returns ``None``
    when the response carries no usable usage block (e.g. no ``usage``
    attribute, or a test stub whose fields aren't real integers).
    """
    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    try:
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        cache_creation = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
        cache_read = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
    except (TypeError, ValueError):
        return None
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_creation_input_tokens": cache_creation,
        "cache_read_input_tokens": cache_read,
        "reported": True,
    }


async def call_direct_llm(
    api_key: str,
    *,
    messages: list[dict],
    max_tokens: int,
    timeout: float,
    model: str = DIRECT_LLM_MODEL,
    system: str | None = None,
    base_url: str | None = None,
) -> tuple[str, dict[str, Any], dict[str, Any] | None]:
    """Construct an AsyncAnthropic client and make one messages.create call.

    Returns ``(text, rate_limit_data, usage_data)`` where *text* is the trimmed
    concatenated text from all content blocks, *rate_limit_data* is a
    structured dict from :func:`normalize_rate_limit_data` containing the
    response-header rate-limit information (unified-5h, request-level,
    token-level), and *usage_data* is :func:`extract_usage_data`'s reading of
    ``resp.usage`` (or ``None`` when unavailable). Callers without an account
    context can discard the second element with ``_``.

    Raises on any error — callers wrap this in their own retry loop so
    parse-error retries work exactly as before.
    """
    import anthropic as _anthropic  # lazy so module is importable without anthropic installed

    client_kwargs = {"api_key": api_key}
    if base_url:
        client_kwargs["base_url"] = base_url
    client = _anthropic.AsyncAnthropic(**client_kwargs)
    kwargs: dict = {
        "model": model,
        "max_tokens": max_tokens,
        "timeout": timeout,
        "messages": messages,
    }
    if system is not None:
        kwargs["system"] = system
    resp = await client.messages.create(**kwargs)
    text_parts: list[str] = []
    for block in resp.content:
        text = getattr(block, "text", None)
        if isinstance(text, str) and text:
            text_parts.append(text)
    text = "\n".join(text_parts).strip()
    raw_headers = extract_rate_limit_headers(resp)
    rate_limit_data = normalize_rate_limit_data(raw_headers)
    usage_data = extract_usage_data(resp)
    return text, rate_limit_data, usage_data


