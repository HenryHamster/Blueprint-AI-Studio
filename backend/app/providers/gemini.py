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
    # Added from verbatim Gemini CLI / Developer API / Code Assist bodies:
    "quota exceeded for quota metric",
    "quota exceeded for metric",
    "you've exceeded the rate limit",
    "you exceeded your current quota, please check your plan and billing details",
    "please retry in",
    "to increase your limits, request a quota increase",
    # Code Assist backend auto-falls back to flash and emits this message;
    # treat it as a rate-limit hit on whatever tier was originally requested.
    "switching to the gemini-2.5-flash model",
)

_SERVICE_ACCOUNT_RATE_LIMIT_PATTERNS: tuple[str, ...] = (
    "resource has been exhausted",
    "resource_exhausted",
    "ratelimitexceeded",
    # Vertex AI-specific:
    "quota exceeded for metric",
    "resource exhausted. please try again later",
    "please refer to https://cloud.google.com/vertex-ai/generative-ai/docs/error-code-429",
)

# Gemini doesn't have a clean billing-wall signal the way OpenAI does — most
# free-tier exhaustion is framed as "quota exceeded for metric" which is the
# same wording used for transient per-minute quotas. We tag the API-key
# "you exceeded your current quota, please check your plan" body as quota-
# exhausted because that phrasing only appears on paid-account billing walls;
# everything else stays as rate_limit so a transient 429 doesn't cause a 24h
# cooldown.
_API_KEY_QUOTA_EXHAUSTED_PATTERNS: tuple[str, ...] = (
    "you exceeded your current quota, please check your plan and billing details",
)

_SERVICE_ACCOUNT_QUOTA_EXHAUSTED_PATTERNS: tuple[str, ...] = ()

_DEFAULT_TIER_MODELS = {
    "opus": "gemini-2.5-pro",
    "sonnet": "gemini-2.5-flash",
    "haiku": "gemini-2.5-flash-lite",
}

# Per-model upper bound for thinkingBudget. Sending a higher value triggers
# an INVALID_ARGUMENT from the Gemini API at runtime. Keep this close to the
# documented caps; preview models default to the generous pro cap until
# Google publishes concrete numbers. Flash-lite and flash cap at 24576 per
# docs as of 2026-04.
_THINKING_BUDGET_CAPS = {
    "gemini-2.5-pro": 32768,
    "gemini-2.5-flash": 24576,
    "gemini-2.5-flash-lite": 24576,
    "gemini-3-pro-preview": 32768,
    "gemini-3-flash-preview": 24576,
    "gemini-3.1-pro-preview": 32768,
    "gemini-3.1-flash-preview": 24576,
}
_THINKING_BUDGET_DEFAULT_CAP = 32768


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

    def quota_exhausted_patterns(self, auth_mode: LLMAuthMode) -> tuple[str, ...]:
        self.ensure_supported_auth_mode(auth_mode)
        if auth_mode == LLMAuthMode.SERVICE_ACCOUNT:
            return _SERVICE_ACCOUNT_QUOTA_EXHAUSTED_PATTERNS
        return _API_KEY_QUOTA_EXHAUSTED_PATTERNS

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
            # GOOGLE_GENAI_USE_VERTEXAI is the switch Gemini CLI inspects in
            # getAuthTypeFromEnv; without it the CLI can't pick the Vertex
            # path from GOOGLE_APPLICATION_CREDENTIALS alone and
            # validateNonInteractiveAuth throws on every invocation.
            return SecretMaterial(
                env={
                    "GOOGLE_APPLICATION_CREDENTIALS": secret_path,
                    "GOOGLE_CLOUD_PROJECT": str(metadata.get("project") or ""),
                    "GOOGLE_CLOUD_LOCATION": str(metadata.get("location") or ""),
                    "GOOGLE_GENAI_USE_VERTEXAI": "true",
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
                    "minimum": 1,
                    "maximum": 65536,
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
            # Per-model cap: schema permits up to 32768 (pro's limit), but
            # flash and flash-lite cap at 24576 — over-budget requests get
            # INVALID_ARGUMENT from the API. Clamp silently rather than
            # raise so admin UI overrides don't wedge dispatch; record the
            # clamp in unsupported so the operator can see it.
            cap = _THINKING_BUDGET_CAPS.get(model, _THINKING_BUDGET_DEFAULT_CAP) if isinstance(model, str) else _THINKING_BUDGET_DEFAULT_CAP
            if thinking_budget > cap:
                unsupported.append(
                    f"thinking_budget={thinking_budget} > cap {cap} for model "
                    f"{model!r}; clamped"
                )
                thinking_budget = cap
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
