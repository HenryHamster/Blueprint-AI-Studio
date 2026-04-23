from __future__ import annotations

import json
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
    "gemini-3-flash-preview": 24576,
    "gemini-3.1-pro-preview": 32768,
    "gemini-3.1-flash-lite-preview": 24576,
}
_THINKING_BUDGET_DEFAULT_CAP = 32768


# Model availability classifications. Used to surface advisory markers in
# `unsupported` when the settings bag resolves to a model whose availability
# depends on external state (preview access on the API key, GA rollout,
# deprecation). The CLI invocation still goes out as-is — these are signals
# for observability, not hard blocks, because a preview flag doesn't mean
# unavailable (the key may have preview access) and an unknown model may be
# a freshly released variant we haven't added yet.
#
# When Google promotes a preview model to GA, move it from
# `_GEMINI_PREVIEW_MODELS` into `_GEMINI_GA_MODELS`. When a new family lands,
# add it to `_GEMINI_GA_MODELS` (and `_THINKING_BUDGET_CAPS` above). The
# settings-schema enum in `GeminiProvider.settings_schema` must stay in sync
# — test_gemini_known_models_match_budget_caps enforces that.
_GEMINI_GA_MODELS: frozenset[str] = frozenset({
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
})
_GEMINI_PREVIEW_MODELS: frozenset[str] = frozenset({
    "gemini-3.1-pro-preview",
    "gemini-3-flash-preview",
    "gemini-3.1-flash-lite-preview",
})

# Gemini 3.x text models. Matters because the thinking surface bifurcated:
# 3.x uses `thinkingLevel` (minimal|low|medium|high) as the primary knob,
# while 2.5 uses `thinkingBudget` (int). thinkingBudget on 3.x is
# back-compat only and may behave unexpectedly on 3 Pro per Google's docs.
_GEMINI_3_X_MODELS: frozenset[str] = frozenset({
    "gemini-3.1-pro-preview",
    "gemini-3-flash-preview",
    "gemini-3.1-flash-lite-preview",
})


# Retired/renamed preview IDs Google has repointed to live equivalents.
# Applied at settings_to_cli time before the model hits the CLI, with an
# `unsupported` marker so operators notice their config is on a redirect.
# gemini-3-pro-preview was shut down 2026-03-09 and now resolves to
# gemini-3.1-pro-preview per Google's deprecation notice.
_LEGACY_MODEL_ALIASES: dict[str, str] = {
    "gemini-3-pro-preview": "gemini-3.1-pro-preview",
}


# Preview→GA fallback map. When a task targets a preview model but none of
# the pool's API keys have preview access (list_available_models filtered
# them all out), the dispatcher swaps to the GA counterpart rather than
# failing. Pick by capability class: pro preview degrades to 2.5-pro,
# flash preview degrades to 2.5-flash, flash-lite preview to 2.5-flash-lite.
# No mapping ⇒ no fallback ⇒ dispatch fails loudly so the operator knows to
# enable preview access somewhere. Keep in sync with _GEMINI_PREVIEW_MODELS.
_PREVIEW_TO_GA_FALLBACK: dict[str, str] = {
    "gemini-3.1-pro-preview":        "gemini-2.5-pro",
    "gemini-3-flash-preview":        "gemini-2.5-flash",
    "gemini-3.1-flash-lite-preview": "gemini-2.5-flash-lite",
}


_GEMINI_API_KEY_LIST_MODELS_URL = "https://generativelanguage.googleapis.com/v1beta/models"
_MODEL_LISTING_TIMEOUT_SECONDS = 5.0


def _gemini_model_availability(model: str | None) -> str | None:
    """Return `"ga"`, `"preview"`, `"unknown"`, or None (when no model given).

    Preview means the model exists on Google's side but requires preview
    access on the API key/project; without that access the CLI returns
    404 `ModelNotFoundError` from the v1beta endpoint. `"unknown"` means
    the model isn't in our registered lists — could be a freshly released
    variant or a typo. None when no model is supplied (caller shouldn't
    emit a marker).
    """
    if not isinstance(model, str) or not model.strip():
        return None
    normalized = model.strip()
    if normalized in _GEMINI_GA_MODELS:
        return "ga"
    if normalized in _GEMINI_PREVIEW_MODELS:
        return "preview"
    return "unknown"


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
            # Gemini CLI picks the Vertex path from ADC / service-account
            # creds plus GOOGLE_CLOUD_PROJECT + GOOGLE_CLOUD_LOCATION. The
            # GOOGLE_GENAI_USE_VERTEXAI flag is a Google GenAI SDK switch,
            # not a Gemini CLI switch — setting it here did nothing.
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
                        "gemini-3.1-pro-preview",
                        "gemini-3-flash-preview",
                        "gemini-3.1-flash-lite-preview",
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
                        "answering. Primary control for Gemini 2.5: 0 disables "
                        "thinking (flash/flash-lite only; pro floor is 128). Higher "
                        "= deeper analysis, more cost and latency. On Gemini 3.x "
                        "this is back-compat only and may behave unexpectedly on "
                        "3 Pro — prefer `thinking_level` there."
                    ),
                },
                "thinking_level": {
                    "type": "string",
                    "enum": ["minimal", "low", "medium", "high"],
                    "description": (
                        "Gemini 3.x thinking intensity. Replaces `thinking_budget` "
                        "for 3.x models where budget is backward-compat-only. 3.1 "
                        "Pro cannot fully disable thinking; `minimal` is the "
                        "closest approximation on all 3.x variants. Ignored on 2.5 "
                        "models with an unsupported marker — use `thinking_budget` "
                        "there."
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

    def settings_to_cli(
        self,
        bag: Mapping[str, Any],
        auth_mode: LLMAuthMode | None = None,
    ) -> CLITranslation:
        # auth_mode is accepted for signature parity; Gemini's knobs don't
        # differ between API-key and OAuth modes in the CLI.
        del auth_mode
        args: list[str] = []
        env: dict[str, str] = {}
        settings_files: list[FileMaterial] = []
        unsupported: list[str] = []

        model = bag.get("model")

        # Legacy alias rewrite: Google has shut down certain preview IDs and
        # repointed them to live equivalents. Rewrite before anything else
        # keys off the model so downstream logic sees the live ID.
        if isinstance(model, str) and model in _LEGACY_MODEL_ALIASES:
            aliased = _LEGACY_MODEL_ALIASES[model]
            unsupported.append(
                f"model={model!r} retired; redirected to {aliased!r} "
                f"(Google repointed the name)"
            )
            model = aliased

        # Availability advisory: 3.x preview models exist on Google's side but
        # their v1beta endpoint returns 404 `ModelNotFoundError` when the key
        # lacks preview access. Unknown model IDs — typos or freshly released
        # variants we haven't registered — also warrant a marker so the
        # inevitable 404 is easy to diagnose from the recorded translation.
        # This is advisory only; the CLI invocation still goes out as-is
        # because preview access is legitimate, just per-key.
        availability = _gemini_model_availability(model)
        if availability == "preview":
            unsupported.append(
                f"model={model!r} is a preview model; a v1beta "
                f"ModelNotFoundError on dispatch means the API key lacks "
                f"preview access"
            )
        elif availability == "unknown":
            unsupported.append(
                f"model={model!r} is not in the known-availability list; "
                f"verify with GET /v1beta/models or add to "
                f"_GEMINI_GA_MODELS / _GEMINI_PREVIEW_MODELS"
            )

        # Assemble generation config for an alias entry in ~/.gemini/settings.json.
        # Gemini CLI has almost no per-invocation knobs; any tunable beyond model
        # requires a settings.json alias plus `-m <alias>`.
        generate_config: dict[str, Any] = {}
        thinking_config: dict[str, Any] = {}

        is_3x = isinstance(model, str) and model in _GEMINI_3_X_MODELS
        is_25 = isinstance(model, str) and model in _GEMINI_GA_MODELS

        raw_budget = bag.get("thinking_budget")
        raw_level = bag.get("thinking_level")
        has_budget = isinstance(raw_budget, int)
        has_level = isinstance(raw_level, str)

        # Gemini API rejects requests that set BOTH thinkingBudget and
        # thinkingLevel ("You can only set only one of thinking budget and
        # thinking level"). Pick exactly one per model family:
        #   2.5 → thinking_budget is primary; drop level if also set.
        #   3.x → thinking_level is primary; drop budget if also set.
        # A bag with neither is fine — nothing gets emitted.
        if is_25:
            if has_budget:
                cap = _THINKING_BUDGET_CAPS.get(model, _THINKING_BUDGET_DEFAULT_CAP)
                budget = raw_budget
                if budget > cap:
                    unsupported.append(
                        f"thinking_budget={budget} > cap {cap} for model "
                        f"{model!r}; clamped"
                    )
                    budget = cap
                thinking_config["thinkingBudget"] = budget
            if has_level:
                # 2.5 family doesn't speak thinkingLevel. Drop with marker.
                unsupported.append(
                    f"thinking_level on Gemini 2.5 ({model!r}) is ignored; "
                    f"use thinking_budget instead — dropped"
                )
        elif is_3x:
            if has_level:
                thinking_config["thinkingLevel"] = raw_level
                if has_budget:
                    # Co-setting would 400 the API; prefer level (primary
                    # knob for 3.x per Google docs) and drop budget.
                    unsupported.append(
                        f"thinking_budget + thinking_level both set on "
                        f"Gemini 3.x ({model!r}); Gemini API rejects the "
                        f"combination — thinking_budget dropped in favor "
                        f"of thinking_level"
                    )
            elif has_budget:
                # No level configured — fall back to back-compat budget.
                cap = _THINKING_BUDGET_CAPS.get(model, _THINKING_BUDGET_DEFAULT_CAP)
                budget = raw_budget
                if budget > cap:
                    unsupported.append(
                        f"thinking_budget={budget} > cap {cap} for model "
                        f"{model!r}; clamped"
                    )
                    budget = cap
                unsupported.append(
                    f"thinking_budget on Gemini 3.x ({model!r}) is back-compat "
                    f"only and may behave unexpectedly — prefer thinking_level"
                )
                thinking_config["thinkingBudget"] = budget
        else:
            # Unknown model family (not in GA or 3.x sets). Emit whatever's
            # set but don't combine; if both, prefer level as the newer knob.
            if has_level:
                thinking_config["thinkingLevel"] = raw_level
                if has_budget:
                    unsupported.append(
                        f"thinking_budget + thinking_level both set on "
                        f"unknown model {model!r}; thinking_budget dropped"
                    )
            elif has_budget:
                cap = _THINKING_BUDGET_CAPS.get(model, _THINKING_BUDGET_DEFAULT_CAP) if isinstance(model, str) else _THINKING_BUDGET_DEFAULT_CAP
                budget = raw_budget
                if budget > cap:
                    unsupported.append(
                        f"thinking_budget={budget} > cap {cap} for model "
                        f"{model!r}; clamped"
                    )
                    budget = cap
                thinking_config["thinkingBudget"] = budget

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

    async def list_available_models(
        self,
        secret: str | None,
        auth_mode: LLMAuthMode,
        provider_metadata: Mapping[str, Any] | None = None,
    ) -> list[str] | None:
        """List model IDs this Gemini key can call.

        Hits `GET /v1beta/models?key=<key>` and strips the `models/` prefix
        from each entry. Used to decide which pool accounts are eligible for
        a given model (especially preview models whose access is gated per
        project/key).

        Service-account mode not yet implemented: it would need OAuth2
        token minting from the SA JSON to call Vertex AI's publisher-models
        endpoint, which adds complexity and a `google-auth` dependency.
        Falls back to None (unknown) until that's wired; SA accounts will
        simply skip the filter and be tried for any model.
        """
        self.ensure_supported_auth_mode(auth_mode)
        if auth_mode != LLMAuthMode.API_KEY or not secret:
            return None
        try:
            import httpx
        except ImportError:
            logger.warning("[GEMINI] httpx unavailable; skipping list_available_models")
            return None
        try:
            async with httpx.AsyncClient(timeout=_MODEL_LISTING_TIMEOUT_SECONDS) as client:
                resp = await client.get(
                    _GEMINI_API_KEY_LIST_MODELS_URL,
                    params={"key": secret, "pageSize": 1000},
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:  # noqa: BLE001 — returning None is the contract
            # Log at info (not warning): a transient failure here is
            # recoverable on the next refresh and shouldn't page operators.
            logger.info(
                "[GEMINI] list_available_models failed: %s (returning None = unknown)",
                exc,
            )
            return None

        ids: list[str] = []
        for entry in data.get("models") or []:
            if not isinstance(entry, Mapping):
                continue
            name = entry.get("name")
            # Response shape: {"name": "models/gemini-2.5-pro", ...}
            if isinstance(name, str) and name.startswith("models/"):
                ids.append(name[len("models/"):])
        # Sort for stable diffs when operators inspect stored snapshots.
        return sorted(set(ids))

    def fallback_model(self, model: str | None) -> str | None:
        """Return the GA counterpart for a Gemini preview model.

        Invoked by the dispatcher when the requested preview model is
        unavailable on every account in the pool. Picks by capability
        class (pro/flash) so the fallback stays in the same performance
        bracket — not a strictly equivalent swap, but close enough that
        quality degrades gracefully rather than the task failing outright.
        """
        if not isinstance(model, str):
            return None
        return _PREVIEW_TO_GA_FALLBACK.get(model.strip())
