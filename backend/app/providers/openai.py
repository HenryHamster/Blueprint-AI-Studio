from __future__ import annotations

import re
from typing import Any, Mapping

from app.models import LLMAuthMode, LLMProviderName
from app.providers.base import (
    FileMaterial,
    LLMProvider,
    SecretMaterial,
    ValidationResult,
    parse_json_secret,
    tier_model_from_runtime_config,
    validate_required_metadata_fields,
)

_API_KEY_RE = re.compile(r"sk-[A-Za-z0-9_\-]+")

_API_KEY_RATE_LIMIT_PATTERNS: tuple[str, ...] = (
    "quota exceeded. check your plan and billing",
    "rate limit reached for",
    "please try again in",
)

_OAUTH_RATE_LIMIT_PATTERNS: tuple[str, ...] = (
    "you've hit your usage limit",
    "you've hit rate limit",
    "exceeded retry limit",
    "last status: 429",
    "insufficient_quota",
    "usage_not_included",
    "rate_limit_exceeded",
)

_DEFAULT_TIER_MODELS = {
    "opus": "gpt-5",
    "sonnet": "gpt-4.1",
    "haiku": "gpt-4o-mini",
}


class OpenAIProvider(LLMProvider):
    name = LLMProviderName.OPENAI
    supported_auth_modes = (LLMAuthMode.API_KEY, LLMAuthMode.OAUTH)

    def validate_secret(
        self,
        raw: str,
        auth_mode: LLMAuthMode,
        provider_metadata: Mapping[str, Any] | None = None,
    ) -> ValidationResult:
        self.ensure_supported_auth_mode(auth_mode)
        validate_required_metadata_fields(self, auth_mode, provider_metadata)
        if auth_mode == LLMAuthMode.API_KEY:
            if not isinstance(raw, str) or not raw.strip():
                raise ValueError("secret is required")
            secret = raw.strip()
            if not secret.startswith("sk-"):
                raise ValueError("OpenAI API key should start with 'sk-'")
            if not _API_KEY_RE.fullmatch(secret):
                raise ValueError("OpenAI API key contains invalid characters")
            return ValidationResult(secret=secret, provider_metadata=dict(provider_metadata or {}))
        parse_json_secret(raw)
        return ValidationResult(secret=raw, provider_metadata=dict(provider_metadata or {}))

    def tier_to_model(self, tier: str, runtime_config: Mapping[str, Any]) -> str | None:
        return tier_model_from_runtime_config(self.name, tier, runtime_config, _DEFAULT_TIER_MODELS)

    def rate_limit_patterns(self, auth_mode: LLMAuthMode) -> tuple[str, ...]:
        self.ensure_supported_auth_mode(auth_mode)
        if auth_mode == LLMAuthMode.OAUTH:
            return _OAUTH_RATE_LIMIT_PATTERNS
        return _API_KEY_RATE_LIMIT_PATTERNS

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
        if auth_mode == LLMAuthMode.OAUTH:
            auth_path = "/opt/agent/openai/auth.json"
            return SecretMaterial(
                env={"CODEX_HOME": "/opt/agent/openai"},
                files=(FileMaterial(path=auth_path, content=secret or "", mode="0600"),),
            )
        return SecretMaterial(env={"OPENAI_API_KEY": secret or "", "CODEX_API_KEY": secret or ""})
