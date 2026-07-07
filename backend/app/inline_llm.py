"""Shared helpers for the backend's small inline Anthropic calls."""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import os
import re
from typing import Any

from app.direct_llm_client import call_direct_llm, get_anthropic_api_key
from app.shared_state import runtime_config

logger = logging.getLogger(__name__)

INLINE_LLM_PURPOSES = (
    "compute_qa",
    "compute_analysis",
    "compute_draft",
    "review_categorize",
)

_INLINE_LLM_DEFAULTS: dict[str, dict[str, Any]] = {
    "compute_qa": {
        "model": "claude-haiku-4-5-20251001",
        "timeout_seconds": 30.0,
        "max_tokens": 800,
        "disabled": False,
    },
    "compute_analysis": {
        "model": "claude-haiku-4-5-20251001",
        "timeout_seconds": 30.0,
        "max_tokens": 2000,
        "disabled": False,
    },
    "compute_draft": {
        "model": "claude-haiku-4-5-20251001",
        "timeout_seconds": 30.0,
        "max_tokens": 2000,
        "disabled": False,
    },
    "review_categorize": {
        "model": "claude-haiku-4-5-20251001",
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

_INLINE_LLM_FIELDS = frozenset({"model", "timeout_seconds", "max_tokens", "disabled"})


@dataclass(frozen=True)
class InlineLLMSettings:
    model: str
    timeout_seconds: float
    max_tokens: int
    disabled: bool


def _coerce_inline_llm_bag(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    bag: dict[str, Any] = {}
    model = raw.get("model")
    if isinstance(model, str) and model.strip():
        bag["model"] = model.strip()
    timeout = raw.get("timeout_seconds")
    if isinstance(timeout, (int, float)) and not isinstance(timeout, bool) and timeout > 0:
        bag["timeout_seconds"] = float(timeout)
    max_tokens = raw.get("max_tokens")
    if isinstance(max_tokens, int) and not isinstance(max_tokens, bool) and max_tokens > 0:
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
            "model": base.model,
            "timeout_seconds": base.timeout_seconds,
            "max_tokens": base.max_tokens,
            "disabled": base.disabled,
        }
    else:
        base_map = base
    bag = _coerce_inline_llm_bag(override)
    return InlineLLMSettings(
        model=str(bag.get("model") or base_map["model"]),
        timeout_seconds=float(bag.get("timeout_seconds") or base_map["timeout_seconds"]),
        max_tokens=int(bag.get("max_tokens") or base_map["max_tokens"]),
        disabled=bool(bag.get("disabled")) if "disabled" in bag else bool(base_map["disabled"]),
    )


def resolve_inline_llm_settings(purpose: str) -> InlineLLMSettings:
    if purpose not in _INLINE_LLM_DEFAULTS:
        raise ValueError(f"unknown inline LLM purpose: {purpose}")
    config = runtime_config.get("inline_llm")
    if not isinstance(config, dict):
        config = {}
    settings = _merge_inline_llm_settings(_INLINE_LLM_DEFAULTS[purpose], config.get("default"))
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
            raise ValueError(f"inline_llm[{purpose}] has unknown keys: {', '.join(extra)}")
        model = bag.get("model")
        if model is not None:
            if not isinstance(model, str) or not model.strip():
                raise ValueError(f"inline_llm[{purpose}].model must be a non-empty string")
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


def _inline_llm_unavailable_reason(purpose: str) -> str:
    if _env_kill_switch_enabled(purpose):
        return "env-disabled"
    settings = resolve_inline_llm_settings(purpose)
    if settings.disabled:
        return "config-disabled"
    if not get_anthropic_api_key():
        return "no-api-key"
    return ""


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
    api_key = get_anthropic_api_key()
    payload_messages = messages if messages is not None else [{"role": "user", "content": prompt}]

    for attempt in range(2):
        try:
            raw = await call_direct_llm(
                api_key,
                messages=payload_messages,
                max_tokens=settings.max_tokens,
                timeout=settings.timeout_seconds,
                model=settings.model,
                system=system,
            )
            data = _parse_inline_llm_json(raw)
            if not isinstance(data, dict) or any(key not in data for key in required_keys):
                raise ValueError("inline LLM response missing required keys")
            return data
        except (json.JSONDecodeError, KeyError, ValueError, IndexError) as exc:
            if attempt == 0:
                logger.info("[INLINE_LLM] %s parse error %s, retrying once", purpose, exc)
                continue
            logger.info("[INLINE_LLM] %s parse error on retry; unavailable", purpose)
            return None
        except Exception as exc:  # noqa: BLE001
            logger.info("[INLINE_LLM] %s call failed (%s); unavailable", purpose, exc)
            return None
    return None
