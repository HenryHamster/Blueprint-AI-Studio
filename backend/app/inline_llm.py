"""Shared helpers for the backend's small inline Anthropic-compatible calls."""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import os
import re
from typing import Any

from app.direct_llm_client import (
    DIRECT_LLM_MODEL,
    call_direct_llm,
    get_anthropic_api_key,
)
from app.shared_state import runtime_config

logger = logging.getLogger(__name__)


async def _record_inline_llm_spend(
    *,
    purpose: str,
    provider: str,
    model: str,
    usage_data: dict[str, Any] | None,
) -> None:
    """Price a provider-reported usage probe and persist one spend-log row.

    Advisory and never-raises: a DB or pricing failure must not break the
    caller's parsed-JSON result, matching the advisory pattern used elsewhere
    at this call boundary (e.g. the rate-limit usage probe).

    A (provider, model) with no entry in ``LLM_TOKEN_PRICING`` is recorded with
    its usage and ``pricing_status="unpriced"`` (``cost_usd`` left NULL) rather
    than dropped, so the spend boundary stays visible even before a price row is
    added. Consumers that sum ``cost_usd`` must treat the total as a FLOOR when
    any unpriced rows exist — a fabricated fallback price would be
    indistinguishable from a real measurement downstream.
    """
    if not usage_data or not usage_data.get("reported"):
        return
    try:
        from app.llm_cost_attribution import LLM_TOKEN_PRICING, _price_llm_attempt

        input_tokens = int(usage_data.get("input_tokens") or 0)
        output_tokens = int(usage_data.get("output_tokens") or 0)
        cache_creation = int(usage_data.get("cache_creation_input_tokens") or 0)
        cache_read = int(usage_data.get("cache_read_input_tokens") or 0)
        inclusive_input = input_tokens + cache_creation + cache_read

        pricing = LLM_TOKEN_PRICING.get((provider.lower(), model.lower()))
        if pricing is None:
            cost_usd = None
            pricing_status = "unpriced"
        else:
            input_rate, output_rate = pricing
            cost_usd = _price_llm_attempt(
                inclusive_input,
                output_tokens,
                cache_creation,
                cache_read,
                input_rate,
                output_rate,
                provider=provider,
                model=model,
            )
            pricing_status = "priced"

        from app.database import closing_session
        from app.models import InlineLlmSpendLog

        async with closing_session() as session:
            session.add(
                InlineLlmSpendLog(
                    purpose=purpose,
                    provider=provider,
                    model=model,
                    input_tokens=inclusive_input,
                    output_tokens=output_tokens,
                    cache_creation_input_tokens=cache_creation,
                    cache_read_input_tokens=cache_read,
                    cost_usd=cost_usd,
                    pricing_status=pricing_status,
                )
            )
            await session.commit()
    except Exception as exc:  # noqa: BLE001 — advisory, never breaks the caller
        logger.info("[INLINE_LLM] %s spend-log write failed (%s)", purpose, exc)

INLINE_LLM_PURPOSES = (
    "compute_qa",
    "compute_analysis",
    "compute_draft",
    "review_categorize",
)
INLINE_LLM_PROVIDERS = frozenset({"anthropic", "deepseek", "zai"})

DEFAULT_DEEPSEEK_ANTHROPIC_BASE_URL = "https://api.deepseek.com/anthropic"
DEFAULT_ZAI_ANTHROPIC_BASE_URL = "https://api.z.ai/api/anthropic"

_INLINE_LLM_DEFAULTS: dict[str, dict[str, Any]] = {
    "compute_qa": {
        "provider": "deepseek",
        "model": "deepseek-v4-flash",
        "base_url": None,
        "timeout_seconds": 30.0,
        "max_tokens": 800,
        "disabled": False,
    },
    "compute_analysis": {
        "provider": "deepseek",
        "model": "deepseek-v4-flash",
        "base_url": None,
        "timeout_seconds": 30.0,
        "max_tokens": 2000,
        "disabled": False,
    },
    "compute_draft": {
        "provider": "deepseek",
        "model": "deepseek-v4-flash",
        "base_url": None,
        "timeout_seconds": 30.0,
        "max_tokens": 2000,
        "disabled": False,
    },
    "review_categorize": {
        "provider": "deepseek",
        "model": "deepseek-v4-flash",
        "base_url": None,
        "timeout_seconds": 10.0,
        "max_tokens": 512,
        "disabled": False,
    },
}

_INLINE_LLM_ENV_KILL_SWITCHES = {
    "compute_qa": "DISABLE_LLM_COMPUTE_ANALYSIS",
    "compute_analysis": "DISABLE_LLM_COMPUTE_ANALYSIS",
    "compute_draft": "DISABLE_COMPUTE_EXPERIMENT_DRAFT_LLM",
    "review_categorize": "DISABLE_LLM_REVIEW_CATEGORIZATION",
}

_INLINE_LLM_FIELDS = frozenset(
    {"provider", "model", "base_url", "timeout_seconds", "max_tokens", "disabled"}
)


@dataclass(frozen=True)
class InlineLLMSettings:
    provider: str
    model: str
    base_url: str | None
    timeout_seconds: float
    max_tokens: int
    disabled: bool


@dataclass(frozen=True)
class InlineLLMTarget:
    provider: str
    api_key: str
    model: str
    base_url: str | None


def _default_model(provider: str) -> str:
    if provider == "deepseek":
        return "deepseek-v4-flash"
    if provider == "zai":
        return "glm-5.2"
    return DIRECT_LLM_MODEL


def _provider_from_model(model: str) -> str | None:
    if model.startswith("claude-"):
        return "anthropic"
    if model.startswith("deepseek-"):
        return "deepseek"
    if model.startswith("glm-"):
        return "zai"
    return None


def _coerce_inline_llm_bag(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    bag: dict[str, Any] = {}
    provider = raw.get("provider")
    if isinstance(provider, str) and provider.strip():
        normalized_provider = provider.strip().lower()
        if normalized_provider in INLINE_LLM_PROVIDERS:
            bag["provider"] = normalized_provider
    model = raw.get("model")
    if isinstance(model, str) and model.strip():
        bag["model"] = model.strip()
    base_url = raw.get("base_url")
    if isinstance(base_url, str) and base_url.strip():
        bag["base_url"] = base_url.strip().rstrip("/")
    timeout = raw.get("timeout_seconds")
    if (
        isinstance(timeout, (int, float))
        and not isinstance(timeout, bool)
        and timeout > 0
    ):
        bag["timeout_seconds"] = float(timeout)
    max_tokens = raw.get("max_tokens")
    if (
        isinstance(max_tokens, int)
        and not isinstance(max_tokens, bool)
        and max_tokens > 0
    ):
        bag["max_tokens"] = int(max_tokens)
    disabled = raw.get("disabled")
    if isinstance(disabled, bool):
        bag["disabled"] = disabled
    return bag


def _merge_inline_llm_settings(
    base: dict[str, Any] | InlineLLMSettings,
    override: Any,
) -> InlineLLMSettings:
    if isinstance(base, InlineLLMSettings):
        base_map = {
            "provider": base.provider,
            "model": base.model,
            "base_url": base.base_url,
            "timeout_seconds": base.timeout_seconds,
            "max_tokens": base.max_tokens,
            "disabled": base.disabled,
        }
    else:
        base_map = base
    bag = _coerce_inline_llm_bag(override)
    base_provider = str(base_map.get("provider") or "anthropic")
    explicit_model = "model" in bag
    explicit_provider = "provider" in bag
    provider = str(bag.get("provider") or base_provider)
    model = str(bag["model"]) if explicit_model else str(base_map["model"])
    if not explicit_provider and explicit_model:
        provider = _provider_from_model(model) or provider
    elif explicit_provider and not explicit_model and provider != base_provider:
        model = _default_model(provider)
    if "base_url" in bag:
        base_url = bag["base_url"]
    elif provider == base_provider:
        base_url = base_map.get("base_url")
    else:
        base_url = None
    return InlineLLMSettings(
        provider=provider,
        model=model,
        base_url=str(base_url) if isinstance(base_url, str) and base_url else None,
        timeout_seconds=float(
            bag.get("timeout_seconds") or base_map["timeout_seconds"]
        ),
        max_tokens=int(bag.get("max_tokens") or base_map["max_tokens"]),
        disabled=bool(bag.get("disabled"))
        if "disabled" in bag
        else bool(base_map["disabled"]),
    )


def resolve_inline_llm_settings(purpose: str) -> InlineLLMSettings:
    if purpose not in _INLINE_LLM_DEFAULTS:
        raise ValueError(f"unknown inline LLM purpose: {purpose}")
    config = runtime_config.get("inline_llm")
    if not isinstance(config, dict):
        config = {}
    settings = _merge_inline_llm_settings(
        _INLINE_LLM_DEFAULTS[purpose], config.get("default")
    )
    return _merge_inline_llm_settings(settings, config.get(purpose))


def validate_inline_llm_runtime_config(raw: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, dict):
        raise ValueError("inline_llm must be a dict")
    allowed_keys = set(INLINE_LLM_PURPOSES) | {"default"}
    invalid = sorted(str(key) for key in raw if str(key) not in allowed_keys)
    if invalid:
        raise ValueError(
            f"inline_llm has unknown purpose(s) {invalid}; must be one of {sorted(allowed_keys)}"
        )
    cleaned: dict[str, dict[str, Any]] = {}
    for purpose, bag in raw.items():
        if not isinstance(bag, dict):
            raise ValueError(f"inline_llm[{purpose}] must be a dict")
        cleaned_bag: dict[str, Any] = {}
        extra = sorted(str(key) for key in bag if str(key) not in _INLINE_LLM_FIELDS)
        if extra:
            raise ValueError(
                f"inline_llm[{purpose}] has unknown keys: {', '.join(extra)}"
            )
        model = bag.get("model")
        provider = bag.get("provider")
        if provider is not None:
            if (
                not isinstance(provider, str)
                or provider.strip().lower() not in INLINE_LLM_PROVIDERS
            ):
                raise ValueError(
                    f"inline_llm[{purpose}].provider must be one of {sorted(INLINE_LLM_PROVIDERS)}"
                )
            cleaned_bag["provider"] = provider.strip().lower()
        base_url = bag.get("base_url")
        if base_url is not None:
            if not isinstance(base_url, str) or not base_url.strip():
                raise ValueError(
                    f"inline_llm[{purpose}].base_url must be a non-empty string"
                )
            cleaned_bag["base_url"] = base_url.strip().rstrip("/")
        if model is not None:
            if not isinstance(model, str) or not model.strip():
                raise ValueError(
                    f"inline_llm[{purpose}].model must be a non-empty string"
                )
            cleaned_bag["model"] = model.strip()
        timeout = bag.get("timeout_seconds")
        if timeout is not None:
            if (
                not isinstance(timeout, (int, float))
                or isinstance(timeout, bool)
                or timeout < 1
                or timeout > 300
            ):
                raise ValueError(
                    f"inline_llm[{purpose}].timeout_seconds must be a number between 1 and 300"
                )
            cleaned_bag["timeout_seconds"] = float(timeout)
        max_tokens = bag.get("max_tokens")
        if max_tokens is not None:
            if (
                not isinstance(max_tokens, int)
                or isinstance(max_tokens, bool)
                or max_tokens < 1
                or max_tokens > 8192
            ):
                raise ValueError(
                    f"inline_llm[{purpose}].max_tokens must be an integer between 1 and 8192"
                )
            cleaned_bag["max_tokens"] = int(max_tokens)
        disabled = bag.get("disabled")
        if disabled is not None:
            if not isinstance(disabled, bool):
                raise ValueError(f"inline_llm[{purpose}].disabled must be a boolean")
            cleaned_bag["disabled"] = disabled
        cleaned[purpose] = cleaned_bag
    return cleaned


def _parse_inline_llm_json(raw: str) -> dict[str, Any] | None:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z0-9]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            obj = json.loads(text[start : end + 1])
        except (json.JSONDecodeError, ValueError):
            return None
    return obj if isinstance(obj, dict) else None


def _env_kill_switch_enabled(purpose: str) -> bool:
    return os.environ.get(_INLINE_LLM_ENV_KILL_SWITCHES[purpose], "").strip() == "1"


def _inline_llm_target(settings: InlineLLMSettings) -> InlineLLMTarget:
    if settings.provider == "deepseek":
        base_url = (
            os.environ.get("DEEPSEEK_ANTHROPIC_BASE_URL", "").strip()
            or os.environ.get("DEEPSEEK_BASE_URL", "").strip()
            or settings.base_url
            or DEFAULT_DEEPSEEK_ANTHROPIC_BASE_URL
        )
        api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
        if api_key:
            return InlineLLMTarget(
                provider="deepseek",
                api_key=api_key,
                model=settings.model,
                base_url=base_url.rstrip("/"),
            )
        anthropic_key = get_anthropic_api_key()
        if anthropic_key:
            return InlineLLMTarget(
                provider="anthropic",
                api_key=anthropic_key,
                model=DIRECT_LLM_MODEL,
                base_url=None,
            )
        return InlineLLMTarget(
            provider="deepseek",
            api_key="",
            model=settings.model,
            base_url=base_url.rstrip("/"),
        )
    if settings.provider == "zai":
        base_url = (
            os.environ.get("ZAI_ANTHROPIC_BASE_URL", "").strip()
            or os.environ.get("ZAI_BASE_URL", "").strip()
            or settings.base_url
            or DEFAULT_ZAI_ANTHROPIC_BASE_URL
        )
        api_key = os.environ.get("ZAI_API_KEY", "").strip()
        if api_key:
            return InlineLLMTarget(
                provider="zai",
                api_key=api_key,
                model=settings.model,
                base_url=base_url.rstrip("/"),
            )
        anthropic_key = get_anthropic_api_key()
        if anthropic_key:
            return InlineLLMTarget(
                provider="anthropic",
                api_key=anthropic_key,
                model=DIRECT_LLM_MODEL,
                base_url=None,
            )
        return InlineLLMTarget(
            provider="zai",
            api_key="",
            model=settings.model,
            base_url=base_url.rstrip("/"),
        )
    return InlineLLMTarget(
        provider="anthropic",
        api_key=get_anthropic_api_key(),
        model=settings.model,
        base_url=settings.base_url,
    )


def _inline_llm_unavailable_reason(purpose: str) -> str:
    if _env_kill_switch_enabled(purpose):
        return "env-disabled"
    settings = resolve_inline_llm_settings(purpose)
    if settings.disabled:
        return "config-disabled"
    target = _inline_llm_target(settings)
    if not target.api_key:
        return f"no-{settings.provider}-api-key"
    return ""


def inline_llm_unavailable_reason(purpose: str) -> str:
    return _inline_llm_unavailable_reason(purpose)


async def call_inline_llm_json(
    purpose: str,
    prompt: str,
    *,
    system: str | None = None,
    messages: list[dict] | None = None,
    required_keys: tuple[str, ...] = (),
) -> dict[str, Any] | None:
    if purpose not in _INLINE_LLM_DEFAULTS:
        raise ValueError(f"unknown inline LLM purpose: {purpose}")
    unavailable = _inline_llm_unavailable_reason(purpose)
    if unavailable:
        logger.info("[INLINE_LLM] %s unavailable: %s", purpose, unavailable)
        return None

    settings = resolve_inline_llm_settings(purpose)
    target = _inline_llm_target(settings)
    payload_messages = (
        messages if messages is not None else [{"role": "user", "content": prompt}]
    )

    for attempt in range(2):
        try:
            # The discarded second element is Messages-API rate-limit header
            # data for the ORG-LEVEL env credential these inline calls use —
            # it cannot be attributed to any MaxPlanAccount, so the account
            # usage meter is fed by the OAuth probe instead (max_plan_pool).
            # The third element is the provider-reported token usage probe,
            # captured into inline_llm_spend_log so this spend boundary stops
            # being dark to cost attribution and reconciliation.
            raw, _, usage_data = await call_direct_llm(
                target.api_key,
                messages=payload_messages,
                max_tokens=settings.max_tokens,
                timeout=settings.timeout_seconds,
                model=target.model,
                system=system,
                base_url=target.base_url,
            )
            await _record_inline_llm_spend(
                purpose=purpose,
                provider=target.provider,
                model=target.model,
                usage_data=usage_data,
            )
            data = _parse_inline_llm_json(raw)
            if not isinstance(data, dict) or any(
                key not in data for key in required_keys
            ):
                raise ValueError("inline LLM response missing required keys")
            return data
        except (json.JSONDecodeError, KeyError, ValueError, IndexError) as exc:
            if attempt == 0:
                logger.info(
                    "[INLINE_LLM] %s parse error %s, retrying once", purpose, exc
                )
                continue
            logger.info("[INLINE_LLM] %s parse error on retry; unavailable", purpose)
            return None
        except Exception as exc:  # noqa: BLE001
            logger.info("[INLINE_LLM] %s call failed (%s); unavailable", purpose, exc)
            return None
    return None
