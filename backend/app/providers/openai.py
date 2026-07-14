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
    UsageSnapshot,
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
    # Agent-invented wording: runners.ts raises
    # `RateLimitError("<displayName> API usage exhausted — <snippet>")` when the
    # Codex/OpenAI runner reports its usage window is spent. Without this the
    # backend never recorded a confirmed rate-limit, so the account was not
    # cooled and balanced selection kept routing work to the exhausted account
    # (incident 2026-06-03: reviews looping on a spent Codex account while idle
    # Anthropic accounts were available). Transient (window resets) → rate_limit,
    # NOT quota_exhausted (no 24h billing-wall cooldown).
    "api usage exhausted",
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
    # Agent-invented wording from runners.ts RateLimitError (see _API_KEY list).
    "api usage exhausted",
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

# Substrings in Codex CLI stderr that prove the ChatGPT OAuth session has been
# permanently terminated (user logged out elsewhere, admin revoked OAuth, session
# TTL expired). Matching any of these auto-disables the account fleet-wide until
# an operator re-runs `codex login`. Must remain narrow — only strings that
# appear EXCLUSIVELY on unrecoverable auth death, never on transient 401s.
_OAUTH_DEAD_AUTH_PATTERNS: tuple[str, ...] = (
    "app_session_terminated",
    "token_invalidated",
    "your session has ended. please log in again",
    # A consumed/rotated refresh token never self-recovers: the Codex CLI exits
    # with a 401 whose body says the refresh token was already used. Two phrasings
    # observed across Codex CLI versions (the JSON body varies by endpoint):
    #   "your refresh token was already used"  (CLI ≤ v0.x SDK error string)
    #   "your refresh token has already been used"  (API JSON body, incident 2026-06-13)
    # Incident: account SavarinCodex stayed healthy (success rate 2.9%) because
    # neither phrasing was matched → never auto-disabled → fleet kept routing into
    # the dead account. Never appears on transient 401s.
    "refresh token was already used",
    "refresh token has already been used",
)

_DEFAULT_TIER_MODELS = {
    # Maps onto the current Codex CLI picker inventory (GPT-5.6 family;
    # models cache retrieved 2026-07-10, codex CLI 0.144.1): sol is the
    # frontier agentic coding model, terra the balanced everyday coder,
    # luna the fast/affordable one. All three are Codex-entitled on
    # ChatGPT plans and marked supported_in_api (API-key auth works).
    "opus": "gpt-5.6-sol",
    "sonnet": "gpt-5.6-terra",
    "haiku": "gpt-5.6-luna",
}


# Models that accept `service_tier="flex"` on the OpenAI Responses API.
# Sourced from OpenAI's current pricing tables — flex has its own column
# for the models listed here but is absent for specialized variants
# (e.g. gpt-5.3-codex-spark). Explicit set rather than prefix match: the
# research showed prefix matching would have reopened the 400 on codex
# variants and on older models with no documented flex tier. GPT-5.6
# sol/terra/luna flex rates confirmed on the pricing page
# (developers.openai.com/api/docs/pricing, retrieved 2026-07-11).
_FLEX_SUPPORTED_MODELS: frozenset[str] = frozenset(
    {
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
        "gpt-5.5",
        "gpt-5.4",
        "gpt-5.4-mini",
    }
)


def _model_supports_flex(model: str | None) -> bool:
    """Return True when the given OpenAI model id accepts service_tier=flex."""
    if not isinstance(model, str):
        return False
    return model.strip().lower() in _FLEX_SUPPORTED_MODELS


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
            return ValidationResult(
                secret=secret, provider_metadata=dict(provider_metadata or {})
            )
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
        return ValidationResult(
            secret=raw, provider_metadata=dict(provider_metadata or {})
        )

    def tier_to_model(self, tier: str, runtime_config: Mapping[str, Any]) -> str | None:
        return tier_model_from_runtime_config(
            self.name, tier, runtime_config, _DEFAULT_TIER_MODELS
        )

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

    def dead_auth_patterns(self, auth_mode: LLMAuthMode) -> tuple[str, ...]:
        self.ensure_supported_auth_mode(auth_mode)
        if auth_mode == LLMAuthMode.OAUTH:
            return _OAUTH_DEAD_AUTH_PATTERNS
        return ()

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
                files=(
                    FileMaterial(path=auth_path, content=secret or "", mode="0600"),
                ),
            )
        return SecretMaterial(
            env={"OPENAI_API_KEY": secret or "", "CODEX_API_KEY": secret or ""}
        )

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
                    # Mirrors the current Codex CLI picker inventory (5.6
                    # family; picker cache 2026-07-10). Dotted names are
                    # literal `-m` identifiers, not display labels. When
                    # OpenAI bumps the picker (e.g. 5.6 → 5.7), update this
                    # list — old IDs 404 after their published shutdown
                    # rather than auto-forwarding, so stale enums cause hard
                    # failures (gpt-5.3-codex and gpt-5.2 were dropped from
                    # the picker in the 5.6 cycle). Leave blank in a settings
                    # bag to inherit the tier mapping from
                    # provider_tier_model_map.
                    "enum": [
                        "gpt-5.6-sol",
                        "gpt-5.6-terra",
                        "gpt-5.6-luna",
                        "gpt-5.5",
                        "gpt-5.4",
                        "gpt-5.4-mini",
                        "gpt-5.3-codex-spark",
                    ],
                    "description": (
                        "Codex CLI model. Leave blank to inherit the tier "
                        "mapping from provider_tier_model_map. "
                        "gpt-5.3-codex-spark is ChatGPT-plan-only (the picker "
                        "marks it supported_in_api=false) — picking it on an "
                        "API-key pool will 404."
                    ),
                },
                "reasoning_effort": {
                    "type": "string",
                    "enum": ["low", "medium", "high", "xhigh", "max", "ultra"],
                    "description": (
                        "How hard Codex thinks before answering. `low` = fast "
                        "but shallower; `high`/`xhigh` = deeper planning at "
                        "higher cost and latency; `max`/`ultra` are GPT-5.6-"
                        "family-only (ultra adds automatic task delegation and "
                        "is not offered on luna). Older gens cap at `xhigh`, "
                        "and no current picker model accepts the retired "
                        "`minimal`. Incompat shows up as a runtime 400 at "
                        "dispatch."
                    ),
                },
                "verbosity": {
                    "type": "string",
                    "enum": ["low", "medium", "high"],
                    "description": (
                        "Output length. `low` for terse completions; `high` when you "
                        "need prose explanations alongside code. GPT-5-family knob; "
                        "the current model enum is entirely GPT-5-family so no "
                        "extra gating applies."
                    ),
                },
                "service_tier": {
                    "type": "string",
                    "enum": ["flex", "fast"],
                    "description": (
                        "`flex` = cheaper, higher-latency billing (model-gated: "
                        "gpt-5.6 picker family plus gpt-5.5, gpt-5.4, and "
                        "gpt-5.4-mini). `fast` = ChatGPT-OAuth-only "
                        "priority routing that consumes Fast-mode credits. "
                        "Incompatible combinations are dropped with an "
                        "unsupported marker rather than letting the request 400."
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

    def settings_to_cli(
        self,
        bag: Mapping[str, Any],
        auth_mode: LLMAuthMode | None = None,
    ) -> CLITranslation:
        args: list[str] = []
        unsupported: list[str] = []
        is_oauth = auth_mode == LLMAuthMode.OAUTH

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
            if service_tier == "flex" and is_oauth:
                # Empirical: OpenAI's Responses API rejects service_tier=flex
                # on ChatGPT-subscription (OAuth) auth with 400 `Unsupported
                # service_tier: flex` regardless of model. Flex is an
                # API-key-billing-only pricing tier and has no equivalent on
                # ChatGPT credits. Official docs are ambiguous here, but the
                # runtime contradicts any "works on OAuth" reading. Drop with
                # marker; admins configuring flex for cost savings need an
                # API-key account in the pool.
                unsupported.append(
                    "service_tier=flex is not available on ChatGPT-account "
                    "(OAuth) auth; dropped"
                )
            elif service_tier == "flex" and not _model_supports_flex(model):
                # Model-level gate for API-key flows: flex is pricing-tier-
                # gated to the explicit _FLEX_SUPPORTED_MODELS inventory.
                # Admin-level defaults can stamp flex across the matrix; drop
                # instead of erroring when a tier downgrade lands on a
                # non-flex model.
                unsupported.append(
                    f"service_tier=flex requires a flex-capable model "
                    f"({', '.join(sorted(_FLEX_SUPPORTED_MODELS))}); effective model "
                    f"{model!r} — dropped"
                )
            elif service_tier == "fast" and not is_oauth:
                # Fast mode is a ChatGPT-subscription-only routing tier that
                # consumes Fast-mode credits. API-key sessions 400 on it.
                unsupported.append(
                    "service_tier=fast is only available on ChatGPT-account "
                    "(OAuth) auth; dropped"
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

    async def probe_usage(
        self,
        secret: str | None,
        auth_mode: LLMAuthMode,
        provider_metadata: Mapping[str, Any] | None = None,
    ) -> UsageSnapshot:
        """No clean usage-headroom source for OpenAI/Codex → always unknown.

        Neither the API-key billing surface nor the ChatGPT-plan (Codex OAuth)
        access token exposes a reliable, documented per-window utilization
        endpoint. Rather than scrape a flaky surface, we resolve to unknown so
        selection behaves exactly as today for OpenAI accounts. Revisit if a
        first-class usage endpoint appears.
        """
        return UsageSnapshot.unknown()

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
            async with httpx.AsyncClient(
                timeout=_MODEL_LISTING_TIMEOUT_SECONDS
            ) as client:
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
