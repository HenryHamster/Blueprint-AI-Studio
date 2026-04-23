from __future__ import annotations

import logging
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

logger = logging.getLogger(__name__)

_OPENAI_LIST_MODELS_URL = "https://api.openai.com/v1/models"
_MODEL_LISTING_TIMEOUT_SECONDS = 5.0

_API_KEY_RE = re.compile(r"sk-[A-Za-z0-9_\-]+")

_API_KEY_RATE_LIMIT_PATTERNS: tuple[str, ...] = (
    "quota exceeded. check your plan and billing",
    "rate limit reached for",
    "please try again in",
    # Added from verbatim 429 bodies observed in codex-cli / openai-python:
    "you exceeded your current quota",
    "insufficient_quota",
    "exceeded the rate limit, please slow down",
    "please try again after",
    "rate_limit_exceeded",
)

_OAUTH_RATE_LIMIT_PATTERNS: tuple[str, ...] = (
    "you've hit your usage limit",
    "you've hit rate limit",
    "exceeded retry limit",
    "last status: 429",
    "insufficient_quota",
    "usage_not_included",
    "rate_limit_exceeded",
    # ChatGPT-plan Codex UI strings (OAuth mode only). Codex CLI pipes these
    # through stderr when the plan window or credit pool is exhausted.
    "you're out of codex messages",
    "your rate limit resets on",
    "add credits or upgrade to pro",
    "you've hit your usage limit. visit",
    "manage or purchase credits",
)

# Subset of the patterns above that mean "billing/quota wall, not a transient
# rate limit". These get a longer flat cooldown in max_plan_pool and a distinct
# admin-UI alert so operators know to top up credits, not wait it out.
_API_KEY_QUOTA_EXHAUSTED_PATTERNS: tuple[str, ...] = (
    "quota exceeded. check your plan and billing",
    "you exceeded your current quota",
    "insufficient_quota",
)

_OAUTH_QUOTA_EXHAUSTED_PATTERNS: tuple[str, ...] = (
    "insufficient_quota",
    "usage_not_included",
    "you're out of codex messages",
    "add credits or upgrade to pro",
    "manage or purchase credits",
)

_DEFAULT_TIER_MODELS = {
    "opus": "gpt-5",
    "sonnet": "gpt-5-codex",
    "haiku": "gpt-5-mini",
}


# Models that accept `service_tier="flex"` on the OpenAI Responses API.
# Sending flex to a non-flex model (e.g. gpt-4.1, gpt-4o-mini) returns a 400
# `Unsupported service_tier: flex`. Codex CLI passes our -c override verbatim,
# so we have to gate at the adapter layer before the request leaves the VM.
#
# Prefix-matched so variants within a family inherit support without a code
# change (gpt-5-mini, o3-pro, o4-mini-high, …). Explicit excludes carry
# documented exceptions — gpt-5-nano ships without flex per OpenAI docs. Keep
# these narrow: when a new family adds flex support, add its prefix here; when
# a family loses support (rare), add to the excludes set. Unknown models
# fail closed — we'd rather surface one warning than one silent 400.
_FLEX_SUPPORTED_PREFIXES: tuple[str, ...] = (
    "gpt-5",
    "o3",
    "o4-mini",
)
_FLEX_EXCLUDED_MODELS: frozenset[str] = frozenset({
    "gpt-5-nano",
    # Codex-optimized premium variant — per OpenAI docs it's not on the flex
    # tier. Leaving it on the flex-eligible side would reopen the original
    # "Unsupported service_tier: flex" 400 that the tier remap was meant to
    # fix, since the sonnet-tier default now routes here.
    "gpt-5-codex",
})


def _model_supports_flex(model: str | None) -> bool:
    """Return True when the given OpenAI model id accepts service_tier=flex.

    Case-insensitive prefix match against `_FLEX_SUPPORTED_PREFIXES`, minus
    any entries in `_FLEX_EXCLUDED_MODELS`. Unknown or empty model names
    return False — we fail closed so an unseen model id can't trigger a 400
    at dispatch time.
    """
    if not isinstance(model, str):
        return False
    normalized = model.strip().lower()
    if not normalized:
        return False
    if normalized in _FLEX_EXCLUDED_MODELS:
        return False
    return any(normalized.startswith(prefix) for prefix in _FLEX_SUPPORTED_PREFIXES)


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
        # OAuth: Codex expects auth.json with a nested `tokens` object.
        # A plain API key or ChatGPT session cookie will parse as JSON but
        # Codex will fail opaquely at runtime — reject here with a clear
        # message instead. Don't over-constrain; Codex tolerates extra keys.
        parsed = parse_json_secret(raw)
        tokens = parsed.get("tokens")
        if not isinstance(tokens, dict):
            raise ValueError(
                "Codex OAuth auth.json must contain a 'tokens' object "
                "(from `codex login` output)"
            )
        if not tokens.get("access_token"):
            raise ValueError("Codex OAuth auth.json missing tokens.access_token")
        return ValidationResult(secret=raw, provider_metadata=dict(provider_metadata or {}))

    def tier_to_model(self, tier: str, runtime_config: Mapping[str, Any]) -> str | None:
        return tier_model_from_runtime_config(self.name, tier, runtime_config, _DEFAULT_TIER_MODELS)

    def rate_limit_patterns(self, auth_mode: LLMAuthMode) -> tuple[str, ...]:
        self.ensure_supported_auth_mode(auth_mode)
        if auth_mode == LLMAuthMode.OAUTH:
            return _OAUTH_RATE_LIMIT_PATTERNS
        return _API_KEY_RATE_LIMIT_PATTERNS

    def quota_exhausted_patterns(self, auth_mode: LLMAuthMode) -> tuple[str, ...]:
        self.ensure_supported_auth_mode(auth_mode)
        if auth_mode == LLMAuthMode.OAUTH:
            return _OAUTH_QUOTA_EXHAUSTED_PATTERNS
        return _API_KEY_QUOTA_EXHAUSTED_PATTERNS

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
        # Intentionally tight surface: only the knobs worth tuning in practice.
        # Sandbox mode / approval prompts are NOT exposed because anything
        # other than full-access stalls the agent VM waiting for an operator
        # that never arrives — handled in settings_to_cli below.
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "model": {
                    "type": "string",
                    # Kept in sync with the Codex CLI supported model list. When
                    # OpenAI ships a new family (or promotes a preview to GA),
                    # add the id here so admins can pick it from the matrix
                    # dropdown without typing. Leave blank in a settings bag to
                    # inherit the tier mapping from provider_tier_model_map.
                    "enum": [
                        "gpt-5",
                        "gpt-5-codex",
                        "gpt-5-mini",
                        "gpt-5-nano",
                        "gpt-4.1",
                        "gpt-4o",
                        "gpt-4o-mini",
                        "o3",
                        "o3-pro",
                        "o3-mini",
                        "o4-mini",
                        "o4-mini-high",
                    ],
                    "description": (
                        "Codex CLI model. Leave blank to inherit the tier "
                        "mapping from provider_tier_model_map."
                    ),
                },
                "reasoning_effort": {
                    "type": "string",
                    "enum": ["minimal", "low", "medium", "high"],
                    "description": (
                        "How much Codex thinks before answering. `minimal`/`low` = "
                        "fast but shallower; `high` = deeper planning at higher cost "
                        "and latency. Codex CLI has no level above `high`."
                    ),
                },
                "verbosity": {
                    "type": "string",
                    "enum": ["low", "medium", "high"],
                    "description": (
                        "Output length. `low` for terse completions; `high` when you "
                        "need prose explanations alongside code."
                    ),
                },
                "service_tier": {
                    "type": "string",
                    "enum": ["flex", "fast"],
                    "description": (
                        "`flex` = cheaper but higher latency when OpenAI is under load. "
                        "`fast` = priority routing at standard price."
                    ),
                },
                "web_search": {
                    "type": "string",
                    "enum": ["disabled", "cached", "live"],
                    "description": (
                        "Let Codex fetch web content while solving. Works for both "
                        "API-key and ChatGPT OAuth auth modes (routed via Codex TOML "
                        "config). `disabled` (offline); `cached` (reuse prior "
                        "fetches); `live` (fresh fetches on every task — API-key mode "
                        "incurs per-search charges, OAuth mode counts against your "
                        "ChatGPT subscription quota)."
                    ),
                },
                "tool_output_token_limit": {
                    "type": "integer",
                    "description": (
                        "Truncate tool-call output past this many tokens. Guards against "
                        "a runaway `cat huge_log.txt` blowing the context window."
                    ),
                },
            },
        }

    def settings_to_cli(self, bag: Mapping[str, Any]) -> CLITranslation:
        args: list[str] = []
        unsupported: list[str] = []

        model = bag.get("model")
        if isinstance(model, str):
            args += ["-m", model]

        # Agent VMs are ephemeral and isolated — sandboxing inside them would
        # only stall the agent on permission prompts that nobody is there to
        # answer. Hardcode the "yolo" mode; don't accept a config override.
        args += ["-s", "danger-full-access"]

        # Codex exposes every non-model knob via -c key=value (TOML override).
        config_pairs: list[tuple[str, str]] = []

        effort = bag.get("reasoning_effort")
        if isinstance(effort, str):
            config_pairs.append(("model_reasoning_effort", f'"{effort}"'))

        verbosity = bag.get("verbosity")
        if isinstance(verbosity, str):
            config_pairs.append(("model_verbosity", f'"{verbosity}"'))

        service_tier = bag.get("service_tier")
        if isinstance(service_tier, str):
            if service_tier == "flex" and not _model_supports_flex(model):
                # Dropping instead of erroring: admin-level defaults can stamp
                # service_tier=flex across the matrix, and a per-task tier
                # downgrade (opus → sonnet → gpt-4.1) would otherwise turn
                # every sonnet task into a hard 400. The unsupported marker
                # surfaces the drop in observability.
                unsupported.append(
                    f"service_tier=flex requires a flex-capable model "
                    f"(gpt-5, gpt-5-mini, o3, o4-mini); effective model "
                    f"{model!r} — dropped"
                )
            else:
                config_pairs.append(("service_tier", f'"{service_tier}"'))

        web_search = bag.get("web_search")
        if isinstance(web_search, str):
            config_pairs.append(("web_search", f'"{web_search}"'))

        tool_limit = bag.get("tool_output_token_limit")
        if isinstance(tool_limit, int) and tool_limit > 0:
            config_pairs.append(("tool_output_token_limit", str(tool_limit)))

        for key, value in config_pairs:
            args += ["-c", f"{key}={value}"]

        return CLITranslation(
            args=tuple(args),
            unsupported=tuple(unsupported),
        )

    async def list_available_models(
        self,
        secret: str | None,
        auth_mode: LLMAuthMode,
        provider_metadata: Mapping[str, Any] | None = None,
    ) -> list[str] | None:
        """List model IDs this OpenAI key can call.

        Hits `GET /v1/models` with bearer auth. OpenAI org-level flags can
        scope which models an API key sees (preview access, beta tiers,
        fine-tuned models); the caller uses the result to skip accounts
        that don't list a required model.

        OAuth (Codex) mode: the ChatGPT-plan access token isn't generally
        accepted on `/v1/models`, so we skip it and return None (unknown).
        """
        self.ensure_supported_auth_mode(auth_mode)
        if auth_mode != LLMAuthMode.API_KEY or not secret:
            return None
        try:
            import httpx
        except ImportError:
            logger.warning("[OPENAI] httpx unavailable; skipping list_available_models")
            return None
        try:
            async with httpx.AsyncClient(timeout=_MODEL_LISTING_TIMEOUT_SECONDS) as client:
                resp = await client.get(
                    _OPENAI_LIST_MODELS_URL,
                    headers={"Authorization": f"Bearer {secret}"},
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:  # noqa: BLE001 — returning None is the contract
            logger.info(
                "[OPENAI] list_available_models failed: %s (returning None = unknown)",
                exc,
            )
            return None

        ids: list[str] = []
        for entry in data.get("data") or []:
            if not isinstance(entry, Mapping):
                continue
            mid = entry.get("id")
            if isinstance(mid, str) and mid:
                ids.append(mid)
        return sorted(set(ids))
