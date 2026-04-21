from __future__ import annotations

import re
from typing import Any, Mapping

from app.models import LLMAuthMode, LLMProviderName
from app.providers.base import (
    LLMProvider,
    SecretMaterial,
    ValidationResult,
    tier_model_from_runtime_config,
    validate_required_metadata_fields,
)

_TOKEN_RE = re.compile(r"sk-ant-[A-Za-z0-9_\-]+")

_RATE_LIMIT_PATTERNS: tuple[str, ...] = (
    "you've hit your limit",
    "you've hit your usage limit",
    "claude ai usage limit reached",
    "you're out of extra usage",
    "5-hour limit reached",
    "api error: rate limit reached",
    "this request would exceed your account's rate limit",
)

_DEFAULT_TIER_MODELS = {
    "opus": "claude-opus-4-7",
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5-20251001",
}


class AnthropicProvider(LLMProvider):
    name = LLMProviderName.ANTHROPIC
    supported_auth_modes = (LLMAuthMode.OAUTH, LLMAuthMode.API_KEY)

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
        if not secret.startswith("sk-ant-"):
            raise ValueError("Anthropic secret should start with 'sk-ant-'")
        if not _TOKEN_RE.fullmatch(secret):
            raise ValueError("Anthropic secret contains invalid characters")
        return ValidationResult(secret=secret, provider_metadata=dict(provider_metadata or {}))

    def tier_to_model(self, tier: str, runtime_config: Mapping[str, Any]) -> str | None:
        return tier_model_from_runtime_config(self.name, tier, runtime_config, _DEFAULT_TIER_MODELS)

    def rate_limit_patterns(self, auth_mode: LLMAuthMode) -> tuple[str, ...]:
        self.ensure_supported_auth_mode(auth_mode)
        return _RATE_LIMIT_PATTERNS

    def required_metadata_fields(self, auth_mode: LLMAuthMode) -> tuple[str, ...]:
        self.ensure_supported_auth_mode(auth_mode)
        return ()

    def supported_execution_modes(self) -> tuple[str, ...]:
        return ("default", "advisor")

    def secret_material(
        self,
        secret: str | None,
        auth_mode: LLMAuthMode,
        provider_metadata: Mapping[str, Any] | None = None,
    ) -> SecretMaterial:
        self.ensure_supported_auth_mode(auth_mode)
        if auth_mode == LLMAuthMode.API_KEY:
            return SecretMaterial(env={"ANTHROPIC_API_KEY": secret or ""})
        return SecretMaterial(env={"CLAUDE_CODE_OAUTH_TOKEN": secret or ""})
