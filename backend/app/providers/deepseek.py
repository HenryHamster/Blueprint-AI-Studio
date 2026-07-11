from __future__ import annotations

import logging
from typing import Any, Mapping

from app.models import LLMAuthMode, LLMProviderName
from app.providers.base import (
    CLITranslation,
    LLMProvider,
    SecretMaterial,
    ValidationResult,
    tier_model_from_runtime_config,
    validate_required_metadata_fields,
)

logger = logging.getLogger(__name__)

DEFAULT_ANTHROPIC_BASE_URL = "https://api.deepseek.com/anthropic"
DEFAULT_API_ROOT_URL = "https://api.deepseek.com"
_MODEL_LISTING_TIMEOUT_SECONDS = 5.0

_DEFAULT_TIER_MODELS = {
    "opus": "deepseek-v4-pro",
    "sonnet": "deepseek-v4-pro",
    "haiku": "deepseek-v4-flash",
}

# DeepSeek's documented Claude Code integration
# (api-docs.deepseek.com /quick_start/agent_integrations/claude_code) selects
# models through these environment variables rather than relying on the CLI's
# built-in claude-* aliases. Without them the CLI resolves its Anthropic
# default model names, which DeepSeek's compat layer silently remaps
# (claude-opus* → deepseek-v4-pro, claude-sonnet*/haiku* → deepseek-v4-flash,
# anything unrecognized → deepseek-v4-flash) — so an intended Pro run can
# silently downgrade to Flash. The ids are the documented plain forms; there
# is no "[1m]" long-context variant (both models are natively 1M-context and
# an unknown id would itself trigger the silent Flash fallback). An explicit
# `--model` CLI flag still overrides ANTHROPIC_MODEL, so per-task/tier model
# selection is unaffected.
_CLAUDE_CODE_MODEL_ENV = {
    "ANTHROPIC_MODEL": "deepseek-v4-pro",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "deepseek-v4-pro",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "deepseek-v4-pro",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "deepseek-v4-flash",
    "CLAUDE_CODE_SUBAGENT_MODEL": "deepseek-v4-flash",
}

_RATE_LIMIT_PATTERNS: tuple[str, ...] = (
    "rate limit exceeded",
    "rate_limit",
    "too many requests",
    "api error: rate limit",
    "api error: 429",
    "status 429",
    "quota exceeded",
    "api usage exhausted",
    "concurrency limit",
)

_QUOTA_EXHAUSTED_PATTERNS: tuple[str, ...] = (
    "insufficient balance",
    "insufficient_balance",
    "insufficient quota",
    "quota exceeded",
    "balance not enough",
)


def _anthropic_base_url(provider_metadata: Mapping[str, Any] | None) -> str:
    metadata = provider_metadata or {}
    for key in ("anthropic_base_url", "base_url"):
        raw = metadata.get(key)
        if isinstance(raw, str) and raw.strip():
            return raw.strip().rstrip("/")
    return DEFAULT_ANTHROPIC_BASE_URL


def _list_models_url(provider_metadata: Mapping[str, Any] | None) -> str:
    """Models listing lives at the API root, not under the /anthropic prefix."""
    base = _anthropic_base_url(provider_metadata)
    root = base[: -len("/anthropic")] if base.endswith("/anthropic") else base
    return f"{root}/models"


class DeepSeekProvider(LLMProvider):
    """DeepSeek models through the Claude Code / Anthropic-compatible surface."""

    name = LLMProviderName.DEEPSEEK
    supported_auth_modes = (LLMAuthMode.API_KEY,)

    def validate_secret(
        self,
        raw: str,
        auth_mode: LLMAuthMode,
        provider_metadata: Mapping[str, Any] | None = None,
    ) -> ValidationResult:
        self.ensure_supported_auth_mode(auth_mode)
        validate_required_metadata_fields(self, auth_mode, provider_metadata)
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError("secret is required")
        secret = raw.strip()
        if any(ch.isspace() for ch in secret):
            raise ValueError("DeepSeek API key must not contain whitespace")
        return ValidationResult(
            secret=secret, provider_metadata=dict(provider_metadata or {})
        )

    def tier_to_model(self, tier: str, runtime_config: Mapping[str, Any]) -> str | None:
        return tier_model_from_runtime_config(
            self.name, tier, runtime_config, _DEFAULT_TIER_MODELS
        )

    def rate_limit_patterns(self, auth_mode: LLMAuthMode) -> tuple[str, ...]:
        self.ensure_supported_auth_mode(auth_mode)
        return _RATE_LIMIT_PATTERNS

    def quota_exhausted_patterns(self, auth_mode: LLMAuthMode) -> tuple[str, ...]:
        self.ensure_supported_auth_mode(auth_mode)
        return _QUOTA_EXHAUSTED_PATTERNS

    def required_metadata_fields(self, auth_mode: LLMAuthMode) -> tuple[str, ...]:
        self.ensure_supported_auth_mode(auth_mode)
        return ()

    def secret_material(
        self,
        secret: str | None,
        auth_mode: LLMAuthMode,
        provider_metadata: Mapping[str, Any] | None = None,
    ) -> SecretMaterial:
        self.ensure_supported_auth_mode(auth_mode)
        key = secret or ""
        return SecretMaterial(
            env={
                "DEEPSEEK_API_KEY": key,
                "DEEPSEEK_ANTHROPIC_BASE_URL": _anthropic_base_url(provider_metadata),
                "ANTHROPIC_API_KEY": key,
                "ANTHROPIC_AUTH_TOKEN": key,
                "ANTHROPIC_BASE_URL": _anthropic_base_url(provider_metadata),
                **_CLAUDE_CODE_MODEL_ENV,
            }
        )

    async def list_available_models(
        self,
        secret: str | None,
        auth_mode: LLMAuthMode,
        provider_metadata: Mapping[str, Any] | None = None,
    ) -> list[str] | None:
        """List model IDs this DeepSeek key can call.

        Hits DeepSeek's OpenAI-style ``GET /models`` with bearer auth so
        callers can confirm an account actually has access to the model a
        task requests (e.g. deepseek-v4-pro) instead of trusting a bare
        auth-OK validation. Same contract as the base class: a concrete
        list on success, None for unknown/failure, never raises.
        """
        if auth_mode != LLMAuthMode.API_KEY or not secret:
            # Contract says unknown (None), never raise — even for a
            # corrupt/miskeyed account row carrying an unsupported auth mode.
            return None
        try:
            import httpx
        except ImportError:
            logger.warning("[DEEPSEEK] httpx unavailable; skipping list_available_models")
            return None
        try:
            async with httpx.AsyncClient(
                timeout=_MODEL_LISTING_TIMEOUT_SECONDS
            ) as client:
                resp = await client.get(
                    _list_models_url(provider_metadata),
                    headers={"Authorization": f"Bearer {secret}"},
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:  # noqa: BLE001 — returning None is the contract
            logger.info(
                "[DEEPSEEK] list_available_models failed: %s (returning None = unknown)",
                exc,
            )
            return None

        if not isinstance(data, Mapping):
            # Syntactically valid but non-object JSON body (e.g. a bare
            # array) — treat as unknown rather than raising out of the
            # never-raises contract.
            logger.info(
                "[DEEPSEEK] list_available_models got non-object body "
                "(returning None = unknown)"
            )
            return None

        ids: list[str] = []
        for entry in data.get("data") or []:
            if not isinstance(entry, Mapping):
                continue
            mid = entry.get("id")
            if isinstance(mid, str) and mid:
                ids.append(mid)
        models = sorted(set(ids))
        # Fail-open guard against id-vocabulary drift: the availability
        # sweeper refreshes every account with this result and the pool
        # hard-filters accounts whose list lacks the requested model. If a
        # SUCCESSFUL listing contains none of the documented ids the tier map
        # dispatches (deepseek-v4-pro / deepseek-v4-flash), the far more
        # likely cause is DeepSeek renaming its catalog than every account
        # simultaneously losing access — returning a concrete alien list
        # would starve the whole DeepSeek fleet within one sweep. Treat it
        # as unknown instead. An EMPTY list is kept as-is: that is the
        # legitimate revoked-key signal.
        known_ids = set(_DEFAULT_TIER_MODELS.values())
        if models and not known_ids.intersection(models):
            logger.warning(
                "[DEEPSEEK] /models listing %s contains none of the "
                "documented ids %s — treating as vocabulary drift "
                "(returning None = unknown) so the availability filter "
                "cannot starve the DeepSeek pool",
                models,
                sorted(known_ids),
            )
            return None
        return models

    def settings_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "model": {
                    "type": "string",
                    "enum": ["deepseek-v4-flash", "deepseek-v4-pro"],
                    "description": (
                        "DeepSeek model id passed to Claude Code through the "
                        "Anthropic-compatible endpoint. Leave blank to inherit "
                        "the tier mapping from provider_tier_model_map."
                    ),
                },
                "effort": {
                    "type": "string",
                    "enum": ["low", "medium", "high", "xhigh", "max"],
                    "description": (
                        "Claude Code effort flag for the Anthropic-compatible "
                        "runner. Unsupported values are left for the CLI/provider "
                        "to reject rather than silently remapping."
                    ),
                },
            },
        }

    def settings_to_cli(
        self,
        bag: Mapping[str, Any],
        auth_mode: LLMAuthMode | None = None,
    ) -> CLITranslation:
        self.ensure_supported_auth_mode(auth_mode or LLMAuthMode.API_KEY)
        args: list[str] = []
        model = bag.get("model")
        if isinstance(model, str):
            args += ["--model", model]
        effort = bag.get("effort")
        if isinstance(effort, str):
            args += ["--effort", effort]
        args += ["--permission-mode", "bypassPermissions"]
        return CLITranslation(args=tuple(args))
