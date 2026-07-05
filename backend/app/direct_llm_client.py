"""Direct Anthropic API call helpers shared across admin, knowledge_completion, and review_learning.

Each call site manages its own disable-env checks, api-key availability checks,
retry-on-parse-error logic, and caller-specific fallback semantics. This module
centralises the three pieces that are byte-for-byte identical across all three
sites: the hardcoded model constant, ANTHROPIC_API_KEY env reading, and
AsyncAnthropic client construction + messages.create invocation.
"""
from __future__ import annotations

import os

# Shared model for all direct (non-provider-stack) Anthropic calls.
# Do not drive this from runtime_config — that is the scope of in-flight task e61e6de8.
DIRECT_LLM_MODEL = "claude-haiku-4-5-20251001"


def get_anthropic_api_key() -> str:
    """Return the stripped ANTHROPIC_API_KEY env value (empty string if not set)."""
    return os.environ.get("ANTHROPIC_API_KEY", "").strip()


async def call_direct_llm(
    api_key: str,
    *,
    messages: list[dict],
    max_tokens: int,
    timeout: float,
    system: str | None = None,
) -> str:
    """Construct an AsyncAnthropic client and make one messages.create call.

    Returns the raw text from the first content block. Raises on any error —
    callers wrap this in their own retry loop so parse-error retries work exactly
    as before.
    """
    import anthropic as _anthropic  # noqa: PLC0415 — lazy so module is importable without anthropic installed

    client = _anthropic.AsyncAnthropic(api_key=api_key)
    kwargs: dict = dict(
        model=DIRECT_LLM_MODEL,
        max_tokens=max_tokens,
        timeout=timeout,
        messages=messages,
    )
    if system is not None:
        kwargs["system"] = system
    resp = await client.messages.create(**kwargs)
    return resp.content[0].text.strip()
