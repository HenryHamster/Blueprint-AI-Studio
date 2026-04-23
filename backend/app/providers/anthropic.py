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


# Models that accept the `[1m]` suffix for 1M-token context. Per Anthropic
# docs, 1M is model-gated to the current Opus 4.x and Sonnet 4.6 variants;
# haiku and older models silently ignore the suffix or error. We drop the
# suffix with an unsupported marker when the effective model can't use it.
_1M_CONTEXT_MODELS: frozenset[str] = frozenset({
    "claude-opus-4-7",
    "claude-opus-4-6",
    "claude-sonnet-4-6",
})


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
                        "How hard Claude thinks before acting. Higher = deeper "
                        "planning, more cost, more latency. All five levels are "
                        "valid CLI values; per-model caps are handled by Claude "
                        "Code — unsupported levels on a given model auto-fall-back "
                        "downward (e.g. `max`/`xhigh` on sonnet-4-6 runs as high)."
                    ),
                },
                "use_1m_context": {
                    "type": "boolean",
                    "description": (
                        "Opt into the 1M-token context window. Appends `[1m]` to "
                        "the model ID so Claude Code routes to the 1M variant. "
                        "Requires opus-4-7, opus-4-6, or sonnet-4-6 — haiku "
                        "ignores the suffix (we drop it with an unsupported "
                        "marker). 1M costs more and is only worth it when the "
                        "prompt or exploration genuinely needs the extra window."
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
                        "Anthropic-only. Claude Code CLI's --fallback-model flag: "
                        "used when the primary is rate-limited. Pick a different tier "
                        "than `model` (e.g. sonnet when opus is over quota). OpenAI "
                        "and Gemini don't have a CLI-level equivalent — cross-provider "
                        "fallback is handled at the pool layer instead."
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

    def settings_to_cli(
        self,
        bag: Mapping[str, Any],
        auth_mode: LLMAuthMode | None = None,
    ) -> CLITranslation:
        # auth_mode is accepted for signature parity; Anthropic exposes the
        # same knobs regardless of API-key vs OAuth and has no auth-gated
        # features to enforce here.
        del auth_mode
        args: list[str] = []
        env: dict[str, str] = {}
        prompt_fragments: list[str] = []
        unsupported: list[str] = []

        model = bag.get("model")
        use_1m = bool(bag.get("use_1m_context"))
        if isinstance(model, str):
            if use_1m and model in _1M_CONTEXT_MODELS:
                args += ["--model", f"{model}[1m]"]
            elif use_1m:
                unsupported.append(
                    f"use_1m_context=true requires a 1M-capable model "
                    f"(opus-4-7, opus-4-6, sonnet-4-6); effective model "
                    f"{model!r} — suffix dropped, plain --model sent"
                )
                args += ["--model", model]
            else:
                args += ["--model", model]

        effort = bag.get("effort")
        if isinstance(effort, str):
            # Claude Code CLI accepts all five levels (low/medium/high/xhigh/max)
            # as real distinct values. Unsupported levels on a given model
            # auto-fall-back downward, so we pass through verbatim.
            args += ["--effort", effort]

        fallback_model = bag.get("fallback_model")
        # Same-tier fallback is a no-op (can't fall back to the rate-limited
        # tier), so treat it as "no fallback configured" rather than emitting
        # a useless flag. Account- and provider-level fallback already cover
        # the cross-model case anyway.
        if isinstance(fallback_model, str) and fallback_model != model:
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
