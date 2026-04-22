from __future__ import annotations

import json
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

_GEMINI_SETTINGS_ALIAS = "blueprint-runtime"
# Gemini CLI looks up custom aliases from $HOME/.gemini/settings.json and does
# not expose an env var to override the config dir. The path below matches the
# agent VM image's fixed home (/home/agent); in LOCAL_MODE the manifest writer
# rewrites it under the temp manifest root, so the CLI won't find the alias —
# LOCAL_MODE + per-task generation config is a known limitation. Production is
# unaffected.
_GEMINI_SETTINGS_PATH = "/home/agent/.gemini/settings.json"

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

    def settings_schema(self) -> dict[str, Any]:
        # Approval mode / sandbox / safety filters / sampling knobs are
        # intentionally NOT exposed:
        #  - approval_mode != yolo stalls the agent VM on unanswered prompts
        #    (hardcoded in settings_to_cli below).
        #  - sandbox is redundant — the VM is already isolated and disposable.
        #  - temperature/top_p/safety are rarely touched and add noise.
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "model": {
                    "type": "string",
                    "enum": [
                        "gemini-2.5-pro",
                        "gemini-2.5-flash",
                        "gemini-2.5-flash-lite",
                        "gemini-3-pro-preview",
                        "gemini-3-flash-preview",
                        "gemini-3.1-pro-preview",
                        "gemini-3.1-flash-preview",
                    ],
                    "description": (
                        "Concrete model. Leave blank to inherit the tier mapping from "
                        "provider_tier_model_map."
                    ),
                },
                "thinking_budget": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 32768,
                    "description": (
                        "Token budget Gemini spends on internal reasoning before "
                        "answering. 0 disables thinking entirely. Higher = deeper "
                        "analysis, more cost and latency. Per-model hard caps apply; "
                        "gemini-2.5-pro supports up to 32768."
                    ),
                },
                "include_thoughts": {
                    "type": "boolean",
                    "description": (
                        "Surface Gemini's reasoning trace in the output stream. "
                        "Useful for debugging weird behavior; noisy for normal runs."
                    ),
                },
                "max_output_tokens": {
                    "type": "integer",
                    "description": (
                        "Hard cap on response length. Keep generous (8k+) unless you "
                        "really want to truncate."
                    ),
                },
            },
        }

    def settings_to_cli(self, bag: Mapping[str, Any]) -> CLITranslation:
        args: list[str] = []
        env: dict[str, str] = {}
        settings_files: list[FileMaterial] = []
        unsupported: list[str] = []

        model = bag.get("model")

        # Assemble generation config for an alias entry in ~/.gemini/settings.json.
        # Gemini CLI has almost no per-invocation knobs; any tunable beyond model
        # requires a settings.json alias plus `-m <alias>`.
        generate_config: dict[str, Any] = {}
        thinking_config: dict[str, Any] = {}

        thinking_budget = bag.get("thinking_budget")
        if isinstance(thinking_budget, int):
            thinking_config["thinkingBudget"] = thinking_budget
        include_thoughts = bag.get("include_thoughts")
        if isinstance(include_thoughts, bool):
            thinking_config["includeThoughts"] = include_thoughts
        if thinking_config:
            generate_config["thinkingConfig"] = thinking_config

        max_tokens = bag.get("max_output_tokens")
        if isinstance(max_tokens, int):
            generate_config["maxOutputTokens"] = max_tokens

        if generate_config and isinstance(model, str):
            # Write a settings.json with a custom alias that layers our generation
            # config atop the real model id, then select it via `-m <alias>`.
            settings_payload = {
                "modelConfigs": {
                    "customAliases": {
                        _GEMINI_SETTINGS_ALIAS: {
                            "modelConfig": {
                                "model": model,
                                "generateContentConfig": generate_config,
                            }
                        }
                    }
                }
            }
            settings_files.append(
                FileMaterial(
                    path=_GEMINI_SETTINGS_PATH,
                    content=json.dumps(settings_payload, indent=2),
                    mode="0600",
                )
            )
            args += ["-m", _GEMINI_SETTINGS_ALIAS]
        elif isinstance(model, str):
            args += ["-m", model]

        # Agent VMs are ephemeral and isolated; any approval mode other than
        # yolo stalls the agent on an approval that never arrives. Not user-
        # configurable — hardcode it. Sandbox intentionally omitted because
        # the VM itself IS the sandbox.
        args += ["--approval-mode=yolo"]

        return CLITranslation(
            env=env,
            args=tuple(args),
            settings_files=tuple(settings_files),
            unsupported=tuple(unsupported),
        )
