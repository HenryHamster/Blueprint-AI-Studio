from __future__ import annotations

import logging
import re
from typing import Any, Mapping

from app.models import LLMAuthMode, LLMProviderName
from app.providers.base import (
    CLITranslation,
    LLMProvider,
    SecretMaterial,
    UsageSnapshot,
    ValidationResult,
    tier_model_from_runtime_config,
    validate_required_metadata_fields,
)

logger = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"sk-ant-[A-Za-z0-9_\-]+")

# Undocumented OAuth usage-headroom endpoint. Returns per-window utilization
# (five_hour / seven_day load-bearing; per-model sub-buckets may be null). Used
# by the pool's proactive headroom gate. Probe is ADVISORY — any failure or
# schema drift resolves to UsageSnapshot.unknown().
_USAGE_PROBE_URL = "https://api.anthropic.com/api/oauth/usage"
_USAGE_PROBE_BETA_HEADER = "oauth-2025-04-20"
_USAGE_PROBE_TIMEOUT_SECONDS = 5.0


def _snapshot_from_rate_limit_data(
    rate_limit_data: dict[str, Any], probed_at: str | None
) -> UsageSnapshot:
    """Convert a normalize_rate_limit_data() blob to a UsageSnapshot.

    Maps ``unified_5h`` → ``five_hour`` and ``unified_7d`` → ``seven_day``
    so the Messages-API header path produces the same window keys used by the
    OAuth probe path.  Returns unknown when the blob is missing or lacks a
    numeric five_hour utilization (five_hour is load-bearing; seven_day is
    additive when present and its absence is documented — the API-key header
    path may not carry it on all response types).
    """
    if not isinstance(rate_limit_data, dict):
        return UsageSnapshot.unknown(probed_at, failure_kind="schema")
    entry_5h = rate_limit_data.get("unified_5h")
    if not isinstance(entry_5h, dict):
        return UsageSnapshot.unknown(probed_at, failure_kind="schema")
    util_5h = entry_5h.get("utilization")
    if isinstance(util_5h, bool) or not isinstance(util_5h, (int, float)):
        return UsageSnapshot.unknown(probed_at, failure_kind="schema")
    resets: dict[str, str | None] = {}
    reset_5h = entry_5h.get("resets_at")
    if isinstance(reset_5h, str) and reset_5h:
        resets["five_hour"] = reset_5h
    window_utilizations: dict[str, float] = {"five_hour": float(util_5h)}
    # unified_7d is absent from most API-key response types (the header is not
    # guaranteed); map it additively when present and numeric.
    entry_7d = rate_limit_data.get("unified_7d")
    if isinstance(entry_7d, dict):
        util_7d = entry_7d.get("utilization")
        if not isinstance(util_7d, bool) and isinstance(util_7d, (int, float)):
            window_utilizations["seven_day"] = float(util_7d)
            reset_7d = entry_7d.get("resets_at")
            resets["seven_day"] = reset_7d if isinstance(reset_7d, str) and reset_7d else None
    return UsageSnapshot(
        known=True,
        window_utilizations=window_utilizations,
        resets_at=resets,
        probed_at=probed_at,
        raw_rate_limit_blob=rate_limit_data,
    )


def _coerce_utilization(raw: Any) -> float | None:
    """Coerce a utilization field to a float, rejecting bools and non-numerics."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    return None


def _normalize_anthropic_usage(data: Any, probed_at: str | None) -> UsageSnapshot:
    """Normalize the /api/oauth/usage body into a UsageSnapshot.

    Pure + total: tolerates unknown/extra fields (undocumented endpoint) and
    resolves to ``UsageSnapshot.unknown()`` on ANY structural drift or a
    missing/null/non-numeric five-hour field. The five-hour utilization window
    is sufficient; seven-day and per-model windows are additive when present
    and numeric.
    """
    if not isinstance(data, Mapping):
        return UsageSnapshot.unknown(probed_at, failure_kind="schema")
    windows: dict[str, float] = {}
    resets: dict[str, str | None] = {}
    for window in ("five_hour", "seven_day"):
        cell = data.get(window)
        if not isinstance(cell, Mapping):
            if window == "five_hour":
                return UsageSnapshot.unknown(probed_at, failure_kind="schema")
            continue
        util = _coerce_utilization(cell.get("utilization"))
        if util is None:
            if window == "five_hour":
                return UsageSnapshot.unknown(probed_at, failure_kind="schema")
            continue
        windows[window] = util
        reset_raw = cell.get("resets_at")
        resets[window] = reset_raw if isinstance(reset_raw, str) and reset_raw else None
    # Additively carry any other window the endpoint surfaces (e.g. per-model
    # sub-buckets) when it has a numeric utilization. Non-numeric/null extras
    # are simply skipped — they are never load-bearing.
    for key, cell in data.items():
        if key in windows or not isinstance(cell, Mapping):
            continue
        util = _coerce_utilization(cell.get("utilization"))
        if util is None:
            continue
        windows[key] = util
        reset_raw = cell.get("resets_at")
        resets[key] = reset_raw if isinstance(reset_raw, str) and reset_raw else None
    return UsageSnapshot(
        known=True,
        window_utilizations=windows,
        resets_at=resets,
        probed_at=probed_at,
    )

_RATE_LIMIT_PATTERNS: tuple[str, ...] = (
    "you've hit your limit",
    "you've hit your usage limit",
    "you've hit your session limit",
    "session limit · resets",
    "you've hit your weekly limit",
    "weekly limit · resets",
    "claude ai usage limit reached",
    "you're out of extra usage",
    "5-hour limit reached",
    "api error: rate limit reached",
    "this request would exceed your account's rate limit",
    # Weekly (7-day / "seven_day") plan cap. The Claude CLI emits this as plain
    # text ("You've hit your weekly limit · resets …") in addition to the
    # structured rate_limit_event(seven_day). Without these the weekly message
    # fell through to UNKNOWN — so the account was never disabled and the task
    # burned its general retry budget and terminal-FAILED instead of being held
    # + rerouted. Also listed in _QUOTA_EXHAUSTED_PATTERNS so it earns the flat
    # 24h cooldown (the weekly reset is ~a day out, not a 5-hour throttle).
    "weekly limit reached",
    # Org billing cap: "You've hit your org's monthly spend limit · ask your
    # admin to raise it at claude.ai/settings/usage". Matched on the
    # apostrophe-free tail so a curly-quote "org's" can't break the substring
    # match. Also in _QUOTA_EXHAUSTED_PATTERNS — a spend cap is a billing wall
    # (doesn't self-reset until the billing month rolls or an admin raises it),
    # so it must earn the flat 24h cooldown, not the 5-hour exponential.
    "monthly spend limit",
)

# Weekly / 7-day caps resolve on a ~24h clock, not within the short exponential
# backoff used for 5-hour throttles — route them to the flat quota_exhausted
# cooldown so a rate-limited account stays disabled until it can actually serve
# again (the structured seven_day event also supplies an exact retry hint).
_QUOTA_EXHAUSTED_PATTERNS: tuple[str, ...] = (
    "you've hit your weekly limit",
    "weekly limit reached",
    "monthly spend limit",
)

# Substrings in Claude CLI stderr that prove the OAuth token is permanently
# revoked (org policy disabled Claude Code OAuth, or credentials expired/rotated).
# These differ from usage-limit strings above: they indicate the auth layer is
# dead, not just the rate window exhausted. Matching any of these auto-disables
# the account fleet-wide until an operator fixes the org policy / re-authenticates.
_OAUTH_DEAD_AUTH_PATTERNS: tuple[str, ...] = (
    "oauth_org_not_allowed",
    # token_refresh_failed is intentionally excluded: a single refresh failure
    # can be transient (network blip, temporary 5xx at the token endpoint) and
    # must not hard-disable the account on one occurrence. Only unambiguous
    # terminal signals (org policy blocked, session explicitly terminated) qualify.
)

_DEFAULT_TIER_MODELS = {
    "opus": "claude-opus-5",
    "sonnet": "claude-sonnet-5",
    "haiku": "claude-haiku-4-5-20251001",
}

# Advisor-class model IDs the executor may run as (opus-tier or above).
# Advisor pairing is gated on this set (an advisor-class executor silently
# ignores an opus advisor); keeping prior opus versions here means a version
# bump doesn't re-enable a dead advisor knob on a task still pinned to the
# older opus.
_OPUS_MODELS: frozenset[str] = frozenset(
    {
        "claude-opus-5",
        "claude-opus-4-8",
        "claude-opus-4-7",
    }
)


# Models that accept the `[1m]` suffix for 1M-token context. Per Anthropic
# docs, 1M is model-gated to Opus 5, the current Opus 4.x, and Sonnet 5 /
# Sonnet 4.6 variants; haiku and older models silently ignore the suffix or
# error. We drop the suffix with an unsupported marker when the effective
# model can't use it.
_1M_CONTEXT_MODELS: frozenset[str] = frozenset(
    {
        "claude-opus-5",
        "claude-opus-4-8",
        "claude-opus-4-7",
        "claude-opus-4-6",
        "claude-sonnet-5",
        "claude-sonnet-4-6",
    }
)


# Restricted/limited-availability → GA counterpart map for fallback_model().
# Used by the dispatcher when no pool account can run the requested model
# (e.g. plan doesn't include it, or the provider retires it): the task
# requeues on the GA counterpart with the downgrade recorded on task.context.
_MODEL_FALLBACKS: dict[str, str] = {
    # Safety net for in-flight tasks still carrying the retired fable-5 model id.
    # Tracks the opus-tier default, so this moves with opus-4-8 -> opus-5.
    "claude-fable-5": "claude-opus-5",
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
        return ValidationResult(
            secret=secret, provider_metadata=dict(provider_metadata or {})
        )

    def tier_to_model(self, tier: str, runtime_config: Mapping[str, Any]) -> str | None:
        return tier_model_from_runtime_config(
            self.name, tier, runtime_config, _DEFAULT_TIER_MODELS
        )

    def fallback_model(self, model: str | None) -> str | None:
        if not model:
            return None
        return _MODEL_FALLBACKS.get(model)

    def rate_limit_patterns(self, auth_mode: LLMAuthMode) -> tuple[str, ...]:
        self.ensure_supported_auth_mode(auth_mode)
        return _RATE_LIMIT_PATTERNS

    def quota_exhausted_patterns(self, auth_mode: LLMAuthMode) -> tuple[str, ...]:
        # Weekly (7-day) caps are clock-resolving quota walls — classify them as
        # quota_exhausted so disable_account_model applies the flat 24h cooldown
        # instead of the 5-hour exponential backoff.
        self.ensure_supported_auth_mode(auth_mode)
        return _QUOTA_EXHAUSTED_PATTERNS

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
        if auth_mode == LLMAuthMode.API_KEY:
            return SecretMaterial(env={"ANTHROPIC_API_KEY": secret or ""})
        return SecretMaterial(env={"CLAUDE_CODE_OAUTH_TOKEN": secret or ""})

    async def probe_usage(
        self,
        secret: str | None,
        auth_mode: LLMAuthMode,
        provider_metadata: Mapping[str, Any] | None = None,
    ) -> UsageSnapshot:
        """Probe for usage-headroom data. Advisory; never raises.

        OAuth accounts probe the /api/oauth/usage endpoint.
        API-key accounts make a minimal messages.create call to capture
        rate-limit headers from the response.  Any HTTP/auth/timeout error
        or schema drift resolves to unknown so a probe failure can never
        block, delay, or fail a claim.
        """
        self.ensure_supported_auth_mode(auth_mode)
        if not secret:
            return UsageSnapshot.unknown(failure_kind="missing_secret")
        from app.time_utils import _utcnow

        probed_at = _utcnow().isoformat()
        if auth_mode == LLMAuthMode.API_KEY:
            return await self._probe_usage_api_key(secret, probed_at)
        if auth_mode != LLMAuthMode.OAUTH:
            return UsageSnapshot.unknown(probed_at, failure_kind="unsupported")
        # Stamp probed_at already set above for API_KEY path; re-use here.
        try:
            import httpx
        except ImportError:
            logger.warning("[ANTHROPIC] httpx unavailable; skipping usage probe")
            return UsageSnapshot.unknown(probed_at, failure_kind="unsupported")
        try:
            async with httpx.AsyncClient(
                timeout=_USAGE_PROBE_TIMEOUT_SECONDS
            ) as client:
                resp = await client.get(
                    _USAGE_PROBE_URL,
                    headers={
                        "Authorization": f"Bearer {secret}",
                        "anthropic-beta": _USAGE_PROBE_BETA_HEADER,
                    },
                )
                if resp.status_code in (401, 403):
                    return UsageSnapshot.unknown(probed_at, failure_kind="authentication")
                if resp.status_code >= 400:
                    return UsageSnapshot.unknown(probed_at, failure_kind="endpoint")
                try:
                    data = resp.json()
                except ValueError:
                    return UsageSnapshot.unknown(probed_at, failure_kind="schema")
        except httpx.TimeoutException as exc:
            logger.info(
                "[ANTHROPIC] usage probe failed: %s (returning unknown)", exc
            )
            return UsageSnapshot.unknown(probed_at, failure_kind="timeout")
        except httpx.RequestError as exc:
            logger.info(
                "[ANTHROPIC] usage probe failed: %s (returning unknown)", exc
            )
            return UsageSnapshot.unknown(probed_at, failure_kind="transport")
        except Exception as exc:  # noqa: BLE001 — unknown is the contract
            logger.info(
                "[ANTHROPIC] usage probe failed: %s (returning unknown)", exc
            )
            return UsageSnapshot.unknown(probed_at, failure_kind="endpoint")
        return _normalize_anthropic_usage(data, probed_at)

    async def _probe_usage_api_key(
        self, api_key: str, probed_at: str
    ) -> UsageSnapshot:
        """Probe usage via a minimal messages.create call for API-key accounts.

        Makes a 1-token completion solely to capture the rate-limit headers
        returned by every Anthropic Messages API response. Advisory and
        never-raises — any error resolves to unknown.
        """
        try:
            from app.direct_llm_client import call_direct_llm
        except ImportError:
            logger.warning("[ANTHROPIC] direct_llm_client unavailable; skipping API-key usage probe")
            return UsageSnapshot.unknown(probed_at, failure_kind="unsupported")
        try:
            _text, rate_limit_data = await call_direct_llm(
                api_key,
                messages=[{"role": "user", "content": "1"}],
                max_tokens=1,
                timeout=_USAGE_PROBE_TIMEOUT_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001 — unknown is the contract
            logger.info(
                "[ANTHROPIC] API-key usage probe failed: %s (returning unknown)", exc
            )
            return UsageSnapshot.unknown(probed_at, failure_kind="provider_error")
        return _snapshot_from_rate_limit_data(rate_limit_data, probed_at)

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
                        "claude-opus-5",
                        "claude-opus-4-8",
                        "claude-opus-4-7",
                        "claude-sonnet-5",
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
                        "Requires opus-5, opus-4-8, opus-4-7, opus-4-6, sonnet-5, "
                        "or sonnet-4-6 — haiku ignores the suffix (we drop it with "
                        "an unsupported marker). 1M costs more and is only worth it "
                        "when the prompt or exploration genuinely needs the extra "
                        "window."
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
                        "claude-opus-5",
                        "claude-opus-4-8",
                        "claude-opus-4-7",
                        "claude-sonnet-5",
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
                            "enum": [
                                "claude-opus-5",
                                "claude-opus-4-8",
                                "claude-opus-4-7",
                            ],
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
                    f"(opus-5, opus-4-8, opus-4-7, opus-4-6, sonnet-5, sonnet-4-6); "
                    f"effective model {model!r} — suffix dropped, plain --model sent"
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
            if model in _OPUS_MODELS:
                unsupported.append(f"advisor.enabled (ignored: executor is {model})")
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
