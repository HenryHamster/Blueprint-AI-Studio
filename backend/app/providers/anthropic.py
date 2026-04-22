from __future__ import annotations

import re
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

    def settings_schema(self) -> dict[str, Any]:
        # Permission mode is intentionally NOT exposed: anything other than
        # bypassPermissions stalls the agent VM waiting on an approval that
        # won't come. Hardcoded in settings_to_cli below.
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "model": {
                    "type": "string",
                    "enum": [
                        "claude-opus-4-7",
                        "claude-sonnet-4-6",
                        "claude-haiku-4-5-20251001",
                    ],
                    "description": (
                        "Concrete model. Leave blank to inherit the tier mapping from "
                        "provider_tier_model_map."
                    ),
                },
                "effort": {
                    "type": "string",
                    "enum": ["low", "medium", "high", "xhigh", "max"],
                    "description": (
                        "How hard Claude thinks before acting. Higher = deeper planning, "
                        "more cost, more latency. `max` routes to xhigh at the CLI layer."
                    ),
                },
                "max_budget_usd": {
                    "type": "number",
                    "description": (
                        "Hard stop on token spend for this task. Enforced by Claude "
                        "Code CLI by multiplying streamed token counts against "
                        "Anthropic's list prices — it's a real-time cutoff, not a "
                        "pre-run estimate, but it tracks list prices so negotiated "
                        "rates will differ. 0 or blank = unlimited."
                    ),
                },
                "fallback_model": {
                    "type": "string",
                    "enum": [
                        "claude-opus-4-7",
                        "claude-sonnet-4-6",
                        "claude-haiku-4-5-20251001",
                    ],
                    "description": (
                        "Model to fall back to when the primary is rate-limited. "
                        "Typically a cheaper tier (e.g. sonnet when opus is over quota)."
                    ),
                },
                "betas": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Anthropic beta feature flags (API-key auth only). Comma-"
                        "separated. Most users leave this empty."
                    ),
                },
                "advisor": {
                    "type": "object",
                    "additionalProperties": False,
                    "description": (
                        "Pair a non-opus executor with an opus-class advisor. ONLY "
                        "meaningful when the executor model is sonnet or haiku — "
                        "enabling advisor while executor is opus is silently ignored. "
                        "The Claude Code CLI can't wire a real advisor tool, so we "
                        "inject a prompt fragment telling the executor an advisor "
                        "exists in principle."
                    ),
                    "properties": {
                        "enabled": {"type": "boolean"},
                        "model": {
                            "type": "string",
                            "enum": ["claude-opus-4-7"],
                        },
                    },
                },
            },
        }

    def settings_to_cli(self, bag: Mapping[str, Any]) -> CLITranslation:
        args: list[str] = []
        env: dict[str, str] = {}
        prompt_fragments: list[str] = []
        unsupported: list[str] = []

        model = bag.get("model")
        if isinstance(model, str):
            args += ["--model", model]

        effort = bag.get("effort")
        if isinstance(effort, str):
            # Claude Code CLI accepts low|medium|high|xhigh (not "max") as of v2.1.
            # "max" maps to xhigh to stay within the CLI's vocabulary.
            cli_effort = "xhigh" if effort == "max" else effort
            args += ["--effort", cli_effort]

        fallback_model = bag.get("fallback_model")
        if isinstance(fallback_model, str):
            args += ["--fallback-model", fallback_model]

        # Agent VMs are ephemeral and isolated; any permission mode other than
        # bypassPermissions stalls the agent waiting for an operator that won't
        # come. Not user-configurable — hardcode it.
        args += ["--permission-mode", "bypassPermissions"]

        max_budget = bag.get("max_budget_usd")
        if isinstance(max_budget, (int, float)) and max_budget > 0:
            args += ["--max-budget-usd", str(max_budget)]

        betas = bag.get("betas")
        if isinstance(betas, list) and betas:
            # --betas takes space-separated values; only applies to API-key auth.
            args += ["--betas", *betas]

        advisor = bag.get("advisor") or {}
        if isinstance(advisor, Mapping) and advisor.get("enabled"):
            # Claude Code CLI does not expose the advisor tool directly, and the
            # executor model must be non-opus for pairing to make sense. When
            # configured we append a prompt fragment so the executor at least
            # behaves advisor-ish, and log it as unsupported for observability.
            if model == "claude-opus-4-7":
                unsupported.append("advisor.enabled (ignored: executor is opus-4-7)")
            else:
                prompt_fragments.append(
                    "An opus-class advisor is available in principle, but the "
                    "advisor_20260301 beta tool is not passable via this CLI. "
                    "Plan carefully up front; state assumptions explicitly before "
                    "committing to non-trivial approaches."
                )
                unsupported.append("advisor.enabled (fallback: prompt fragment only)")

        return CLITranslation(
            env=env,
            args=tuple(args),
            prompt_fragments=tuple(prompt_fragments),
            unsupported=tuple(unsupported),
        )
