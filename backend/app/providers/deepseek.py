from __future__ import annotations

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

DEFAULT_ANTHROPIC_BASE_URL = "https://api.deepseek.com/anthropic"

_DEFAULT_TIER_MODELS = {
    "opus": "deepseek-v4-pro",
    "sonnet": "deepseek-v4-pro",
    "haiku": "deepseek-v4-flash",
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
            }
        )

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
