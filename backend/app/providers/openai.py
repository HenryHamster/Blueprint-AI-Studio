from __future__ import annotations

import re
from typing import Any, Mapping

from app.models import LLMAuthMode, LLMProviderName
from app.providers.base import (
    CLITranslation,
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

    def settings_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "model": {"type": "string"},
                "reasoning_effort": {
                    "type": "string",
                    "enum": ["minimal", "low", "medium", "high", "xhigh"],
                },
                "verbosity": {
                    "type": "string",
                    "enum": ["low", "medium", "high"],
                },
                "reasoning_summary": {
                    "type": "string",
                    "enum": ["auto", "concise", "detailed", "none"],
                },
                "service_tier": {
                    "type": "string",
                    "enum": ["flex", "fast"],
                },
                "web_search": {
                    "type": "string",
                    "enum": ["disabled", "cached", "live"],
                },
                "personality": {
                    "type": "string",
                    "enum": ["none", "friendly", "pragmatic"],
                },
                "sandbox_mode": {
                    "type": "string",
                    "enum": ["read-only", "workspace-write", "danger-full-access"],
                },
                "tool_output_token_limit": {"type": "integer"},
            },
        }

    def settings_to_cli(self, bag: Mapping[str, Any]) -> CLITranslation:
        args: list[str] = []
        unsupported: list[str] = []

        model = bag.get("model")
        if isinstance(model, str):
            args += ["-m", model]

        # Codex exposes every non-model knob via -c key=value (TOML override).
        config_pairs: list[tuple[str, str]] = []

        effort = bag.get("reasoning_effort")
        if isinstance(effort, str):
            config_pairs.append(("model_reasoning_effort", f'"{effort}"'))

        verbosity = bag.get("verbosity")
        if isinstance(verbosity, str):
            config_pairs.append(("model_verbosity", f'"{verbosity}"'))

        summary = bag.get("reasoning_summary")
        if isinstance(summary, str):
            config_pairs.append(("model_reasoning_summary", f'"{summary}"'))

        service_tier = bag.get("service_tier")
        if isinstance(service_tier, str):
            config_pairs.append(("service_tier", f'"{service_tier}"'))

        web_search = bag.get("web_search")
        if isinstance(web_search, str):
            config_pairs.append(("web_search", f'"{web_search}"'))

        personality = bag.get("personality")
        if isinstance(personality, str):
            config_pairs.append(("personality", f'"{personality}"'))

        sandbox_mode = bag.get("sandbox_mode")
        if isinstance(sandbox_mode, str):
            args += ["-s", sandbox_mode]

        tool_limit = bag.get("tool_output_token_limit")
        if isinstance(tool_limit, int) and tool_limit > 0:
            config_pairs.append(("tool_output_token_limit", str(tool_limit)))

        for key, value in config_pairs:
            args += ["-c", f"{key}={value}"]

        return CLITranslation(
            args=tuple(args),
            unsupported=tuple(unsupported),
        )
