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

_API_KEY_RE = re.compile(r"AIza[A-Za-z0-9_\-]{10,}")

_API_KEY_RATE_LIMIT_PATTERNS: tuple[str, ...] = (
    "rate limit exceeded",
    "you've hit the api rate limit",
    "resource has been exhausted",
    "resource_exhausted",
    "please wait and try again later",
)

_SERVICE_ACCOUNT_RATE_LIMIT_PATTERNS: tuple[str, ...] = (
    "resource has been exhausted",
    "resource_exhausted",
    "ratelimitexceeded",
)

_DEFAULT_TIER_MODELS = {
    "opus": "gemini-2.5-pro",
    "sonnet": "gemini-2.5-flash",
    "haiku": "gemini-2.5-flash-lite",
}


class GeminiProvider(LLMProvider):
    name = LLMProviderName.GEMINI
    supported_auth_modes = (LLMAuthMode.API_KEY, LLMAuthMode.SERVICE_ACCOUNT)

    def validate_secret(
        self,
        raw: str,
        auth_mode: LLMAuthMode,
        provider_metadata: Mapping[str, Any] | None = None,
    ) -> ValidationResult:
        self.ensure_supported_auth_mode(auth_mode)
        metadata = validate_required_metadata_fields(self, auth_mode, provider_metadata)
        if auth_mode == LLMAuthMode.API_KEY:
            if not isinstance(raw, str) or not raw.strip():
                raise ValueError("secret is required")
            secret = raw.strip()
            if not secret.startswith("AIza"):
                raise ValueError("Gemini API key should start with 'AIza'")
            if not _API_KEY_RE.fullmatch(secret):
                raise ValueError("Gemini API key contains invalid characters")
            return ValidationResult(secret=secret, provider_metadata=metadata)

        parsed = parse_json_secret(raw)
        required_secret_keys = ("type", "client_email", "private_key")
        missing_keys = [key for key in required_secret_keys if not parsed.get(key)]
        if missing_keys:
            raise ValueError(f"Service account JSON missing fields: {', '.join(missing_keys)}")
        if parsed.get("type") != "service_account":
            raise ValueError("Service account JSON must have type='service_account'")
        return ValidationResult(secret=raw, provider_metadata=metadata)

    def tier_to_model(self, tier: str, runtime_config: Mapping[str, Any]) -> str | None:
        return tier_model_from_runtime_config(self.name, tier, runtime_config, _DEFAULT_TIER_MODELS)

    def rate_limit_patterns(self, auth_mode: LLMAuthMode) -> tuple[str, ...]:
        self.ensure_supported_auth_mode(auth_mode)
        if auth_mode == LLMAuthMode.SERVICE_ACCOUNT:
            return _SERVICE_ACCOUNT_RATE_LIMIT_PATTERNS
        return _API_KEY_RATE_LIMIT_PATTERNS

    def required_metadata_fields(self, auth_mode: LLMAuthMode) -> tuple[str, ...]:
        self.ensure_supported_auth_mode(auth_mode)
        if auth_mode == LLMAuthMode.SERVICE_ACCOUNT:
            return ("project", "location")
        return ()

    def secret_material(
        self,
        secret: str | None,
        auth_mode: LLMAuthMode,
        provider_metadata: Mapping[str, Any] | None = None,
    ) -> SecretMaterial:
        self.ensure_supported_auth_mode(auth_mode)
        metadata = dict(provider_metadata or {})
        if auth_mode == LLMAuthMode.SERVICE_ACCOUNT:
            secret_path = "/opt/agent/gemini/sa.json"
            return SecretMaterial(
                env={
                    "GOOGLE_APPLICATION_CREDENTIALS": secret_path,
                    "GOOGLE_CLOUD_PROJECT": str(metadata.get("project") or ""),
                    "GOOGLE_CLOUD_LOCATION": str(metadata.get("location") or ""),
                },
                files=(FileMaterial(path=secret_path, content=secret or "", mode="0600"),),
            )
        return SecretMaterial(env={"GEMINI_API_KEY": secret or ""})
