"""
Max Plan account pool — in-memory cache of DB-persisted LLM accounts.

Extracted from main.py. This module owns:
  - Global pool state (_max_plan_pools, _account_by_id, _max_plan_pool_lock, _pending_rl_persists)
  - Anthropic RATE_LIMIT_PATTERNS constant (shared with classify_failure in main.py and reaper.py)
  - Pool lifecycle: load_max_plan_pool, select_max_plan_account, release_max_plan_account,
    recheck_disabled_accounts
  - Helpers: _all_accounts, _account_available_for_model, _extract_task_model
"""

import asyncio
import logging
import random
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Mapping, Set

from app.database import AsyncSessionLocal
from app.field_encryption import decrypt_field
from app.max_plan_project_state import reconcile_project_state
from app.models import LLMAuthMode, LLMProviderName
from app.providers.base import UsageSnapshot, merge_settings
from app.providers.openai import (
    codex_oauth_refresh_serialization_reason as _codex_oauth_refresh_serialization_reason,
    codex_oauth_requires_refresh_probe as _codex_oauth_requires_refresh_probe,  # noqa: F401 - compatibility re-export
)
from app.providers.registry import get_provider
from app.task_capabilities import get_task_capability
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)


DEFAULT_QUALITY_TIER = "opus"
TASK_RUNTIME_CONTEXT_KEYS = (
    "llm_account_id",
    "llm_provider",
    "llm_auth_mode",
    "llm_model",
    "llm_execution_mode",
    "max_plan_account_id",
    # Runner pin — cleared on requeue so a dead runner is not re-pinned forever
    # and the next dispatch cycle can fall over to GCP cloud fallback.
    "runner_target_id",
)


@dataclass(frozen=True)
class LLMSelectionResult:
    provider: str
    auth_mode: str
    model: str | None
    quality_tier: str | None
    account: Dict[str, Any] | None
    # Resolved provider-specific settings bag (deep-merged from
    # provider_settings_defaults → task_type_provider_overrides → payload).
    # Shape is provider-defined and validated by provider.validate_settings().
    settings: Dict[str, Any] | None = None
    # Retained for historical task records that still persist llm_execution_mode
    # in their context; no longer drives dispatch behavior. Always None for
    # newly-selected tasks.
    execution_mode: str | None = None

    @property
    def account_id(self) -> str | None:
        return self.account.get("id") if self.account else None


@dataclass(frozen=True)
class LLMRuntimeIdentity:
    """A reconciled runtime assignment that must travel as one value."""

    account_id: str | None
    provider: str | None
    auth_mode: str | None
    model: str | None


def _task_context(task_or_ctx: Any) -> dict[str, Any]:
    if isinstance(task_or_ctx, Mapping):
        return dict(task_or_ctx)
    return dict(getattr(task_or_ctx, "context", {}) or {})


def get_task_llm_account_id(task_or_ctx: Any) -> str | None:
    # Prefer the promoted Task.llm_account_id column (D2 over-fetch fix,
    # d-57436105) when passed a Task whose column is set. Fall back to the
    # context JSONB lookup for rows written before the backfill migration or
    # when called with a plain context dict. Nearly every reader funnels
    # through this accessor, so the column-preference applies codebase-wide.
    if not isinstance(task_or_ctx, Mapping):
        col_val = getattr(task_or_ctx, "llm_account_id", None)
        # Guard on str specifically: the column is typed ``str | None``, so a
        # real persisted id (non-empty string) still wins, while an unset column
        # (None) or a non-string attribute (e.g. a test MagicMock) correctly
        # falls through to the context JSONB lookup instead of being returned.
        if isinstance(col_val, str) and col_val:
            return col_val
    ctx = _task_context(task_or_ctx)
    return ctx.get("llm_account_id") or ctx.get("max_plan_account_id")


def get_task_llm_provider(task_or_ctx: Any) -> str | None:
    ctx = _task_context(task_or_ctx)
    raw = ctx.get("llm_provider")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return None


def get_task_llm_auth_mode(task_or_ctx: Any) -> str | None:
    ctx = _task_context(task_or_ctx)
    raw = ctx.get("llm_auth_mode")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return None


def get_task_llm_model(task_or_ctx: Any) -> str | None:
    ctx = _task_context(task_or_ctx)
    raw = ctx.get("llm_model")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return None


def get_task_rate_limit_scope(task_or_ctx: Any) -> dict[str, Any]:
    """Return the candidate-pool scope for rate-limit correlation."""
    providers: list[str] | None = None
    provider = get_task_llm_provider(task_or_ctx)
    if provider:
        providers = [provider]

    user_id: str | None = None
    account_id = get_task_llm_account_id(task_or_ctx)
    if account_id:
        acct = _account_by_id.get(str(account_id))
        raw_user_id = acct.get("user_id") if acct else None
        if isinstance(raw_user_id, str) and raw_user_id.strip():
            user_id = raw_user_id.strip()

    return {
        "providers": providers,
        "model": get_task_llm_model(task_or_ctx),
        "user_id": user_id,
    }


def clear_task_llm_runtime_context(context: Mapping[str, Any] | None) -> dict[str, Any]:
    new_ctx = dict(context or {})
    for key in TASK_RUNTIME_CONTEXT_KEYS:
        new_ctx.pop(key, None)
    return new_ctx


def filter_user_task_context(context: Mapping[str, Any] | None) -> dict[str, Any]:
    """Strip runtime account/provider keys from task context before user-facing serialization."""
    return {
        k: v
        for k, v in (context or {}).items()
        if not k.startswith("max_plan_") and not k.startswith("llm_")
    }


def apply_task_llm_runtime_context(
    context: Mapping[str, Any] | None,
    *,
    account_id: str | None,
    provider: str | None,
    auth_mode: str | None,
    model: str | None,
    execution_mode: str | None = None,
) -> dict[str, Any]:
    new_ctx = clear_task_llm_runtime_context(context)
    if account_id:
        new_ctx["llm_account_id"] = account_id
        # Compatibility bridge for old call sites still keyed on max_plan_account_id.
        new_ctx["max_plan_account_id"] = account_id
    if provider:
        new_ctx["llm_provider"] = provider
    if auth_mode:
        new_ctx["llm_auth_mode"] = auth_mode
    if model:
        new_ctx["llm_model"] = model
    if execution_mode:
        new_ctx["llm_execution_mode"] = execution_mode
    return new_ctx


def _normalize_provider_name(raw: str | LLMProviderName | None) -> str | None:
    if isinstance(raw, LLMProviderName):
        return raw.value
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return None


def _normalize_auth_mode(raw: str | LLMAuthMode | None) -> str | None:
    if isinstance(raw, LLMAuthMode):
        return raw.value
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return None


def _task_type(task: Any) -> str:
    payloads = getattr(task, "payloads", None) or []
    if payloads:
        return payloads[0].get("type", "create")
    return "create"


def get_task_llm_execution_mode(task_or_ctx: Any) -> str | None:
    ctx = _task_context(task_or_ctx)
    raw = ctx.get("llm_execution_mode")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return None


def infer_provider_for_model(
    model: str | None, runtime_config: Mapping[str, Any]
) -> str | None:
    if not model:
        return None
    for provider_name in LLMProviderName:
        provider = get_provider(provider_name)
        for tier in ("opus", "sonnet", "haiku"):
            if provider.tier_to_model(tier, runtime_config) == model:
                return provider_name.value
    return None


def infer_quality_tier_for_model(
    provider_name: str | LLMProviderName | None,
    model: str | None,
    runtime_config: Mapping[str, Any],
) -> str | None:
    normalized_provider = _normalize_provider_name(provider_name)
    if not normalized_provider or not model:
        return None
    provider = get_provider(normalized_provider)
    for tier in ("opus", "sonnet", "haiku"):
        if provider.tier_to_model(tier, runtime_config) == model:
            return tier
    return None


def reconcile_model_for_provider(
    provider: str | LLMProviderName | None,
    model: str | None,
    quality_tier: str | None,
    runtime_config: Mapping[str, Any],
) -> str | None:
    """Return a model valid for ``provider``, correcting a cross-provider mispair.

    A task pinned to provider A's model can be dispatched to provider B when A is
    unavailable (cooldown / no account). If the resolved ``model`` still carries
    A's id while ``provider`` is B, the runtime is handed e.g.
    ``provider=anthropic`` + ``model=deepseek-v4-pro`` and the anthropic CLI
    rejects the foreign id → EXTERNAL_API failure → retries burned → task FAILED.

    When ``model`` demonstrably belongs to a DIFFERENT known provider than
    ``provider``, re-derive the model from ``provider``'s ``quality_tier`` map so
    the pair is consistent. Left unchanged when: no provider/model given; the
    model already belongs to ``provider``; the model infers to None (an
    operator-custom id absent from every tier map — never clobber an explicit
    unknown model); or re-derivation yields nothing (no regression vs. today).
    """
    normalized_provider = _normalize_provider_name(provider)
    if not normalized_provider or not model:
        return model
    model_provider = infer_provider_for_model(model, runtime_config)
    if model_provider is None or model_provider == normalized_provider:
        return model
    rederived = get_provider(normalized_provider).tier_to_model(
        quality_tier or DEFAULT_QUALITY_TIER, runtime_config
    )
    return rederived or model


def reconcile_llm_runtime_identity(
    *,
    account: Mapping[str, Any] | None,
    account_id: str | None,
    provider: str | LLMProviderName | None,
    auth_mode: str | LLMAuthMode | None,
    model: str | None,
    quality_tier: str | None,
    runtime_config: Mapping[str, Any],
) -> LLMRuntimeIdentity:
    """Return one internally-consistent runtime assignment.

    A selected account is authoritative for account id, provider, and auth
    mode. The provider is then authoritative for model reconciliation. With no
    account (environment-credential fallback), the supplied provider/auth pair
    remains authoritative and the same model guard still applies.
    """

    account_provider: str | None = None
    account_auth_mode: str | None = None
    resolved_account_id = str(account_id) if account_id else None
    if account is not None:
        raw_account_id = account.get("id")
        if raw_account_id:
            resolved_account_id = str(raw_account_id)
        account_provider = _normalize_provider_name(account.get("provider"))
        # Historical pool rows without a provider are Anthropic accounts.
        if account_provider is None:
            account_provider = LLMProviderName.ANTHROPIC.value
        account_auth_mode = _normalize_auth_mode(account.get("auth_mode"))
        if account_auth_mode is None:
            account_auth_mode = LLMAuthMode.OAUTH.value

    resolved_provider = account_provider or _normalize_provider_name(provider)
    resolved_auth_mode = account_auth_mode or _normalize_auth_mode(auth_mode)
    resolved_model = reconcile_model_for_provider(
        resolved_provider,
        model.strip() if isinstance(model, str) and model.strip() else None,
        quality_tier,
        runtime_config,
    )
    return LLMRuntimeIdentity(
        account_id=resolved_account_id,
        provider=resolved_provider,
        auth_mode=resolved_auth_mode,
        model=resolved_model,
    )


def get_llm_account_identity(account_id: str | None) -> dict[str, Any] | None:
    """Return non-secret identity fields for a cached pool account."""

    if not account_id:
        return None
    account = _account_by_id.get(str(account_id))
    if account is None:
        return None
    return {
        "id": account.get("id"),
        "provider": account.get("provider"),
        "auth_mode": account.get("auth_mode"),
    }


ALL_PROVIDERS: tuple[str, ...] = tuple(p.value for p in LLMProviderName)
_VALID_SELECTION_MODES: frozenset[str] = frozenset(
    {"balanced", "preferred", "exclusive"}
)
VALID_EFFORT_LEVELS: tuple[str, ...] = ("low", "medium", "high", "max")
DEFAULT_EFFORT_LEVEL = "medium"


def _normalize_provider_list(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    values = [_normalize_provider_name(v) for v in raw]
    return [v for v in values if v]


# Provider-to-CLI binary mapping for runner-side CLI inventory filtering.
# When a runner advertises its available LLM CLI binaries via heartbeat, the
# pool runtime selector filters providers whose required CLI is absent or
# has present=False. This prevents dispatching a task to a runner that lacks
# the CLI needed to execute it (e.g. codex ENOENT crash-requeues).
_PROVIDER_REQUIRED_CLI: dict[str, str] = {
    "anthropic": "claude",
    "deepseek": "claude",
    "zai": "claude",
    "openai": "codex",
    "gemini": "gemini",
}


def _filter_providers_by_cli(
    providers: list[str],
    runner_available_clis: dict[str, dict] | None,
    *,
    require_resident_ready: bool = False,
) -> list[str]:
    """Filter *providers* to those whose required CLI is present on the runner.

    When *runner_available_clis* is ``None``, no filtering is applied (backward
    compat / cloud pre-provision path where all CLIs are installed).

    When *runner_available_clis* is an empty dict (runner probed but found
    nothing), apply the conservative fallback: assume ``claude`` is available
    since all runners have it installed. This avoids starvation for runners
    whose probe returned empty.

    When *runner_available_clis* has entries, a provider is kept only when its
    required CLI's ``"present"`` field is ``True``.
    """
    if runner_available_clis is None:
        return providers
    # Empty concrete inventory cannot positively prove Resident capability.
    # Preserve the legacy Claude-only fallback for ordinary one-shot work.
    if not runner_available_clis:
        if require_resident_ready:
            return []
        return [p for p in providers if _PROVIDER_REQUIRED_CLI.get(p) == "claude"]
    result: list[str] = []
    for provider in providers:
        required_cli = _PROVIDER_REQUIRED_CLI.get(provider)
        if required_cli is None:
            continue  # Unknown provider — can't determine requirement
        cli_entry = runner_available_clis.get(required_cli)
        resident_blocked = (
            require_resident_ready
            and required_cli == "claude"
            and (
                not isinstance(cli_entry, dict)
                or cli_entry.get("resident_ready") is not True
            )
        )
        if (
            isinstance(cli_entry, dict)
            and cli_entry.get("present", False)
            and not resident_blocked
        ):
            result.append(provider)
    return result


def _category_for_task_type(
    task_type: str, runtime_config: Mapping[str, Any]
) -> str | None:
    """Return the first category whose member list contains ``task_type``.

    task_type_categories is ``{category_name: [task_types...]}``. The first
    match wins — categories are expected to partition task types, but if a
    user's config overlaps we pick deterministically by dict order.
    """
    categories = runtime_config.get("task_type_categories", {}) or {}
    if not isinstance(categories, Mapping):
        return None
    for cat_name, members in categories.items():
        if not isinstance(cat_name, str) or not isinstance(members, list):
            continue
        if task_type in members:
            return cat_name
    return None


def _routing_entry(raw: Any) -> tuple[list[str] | None, str | None]:
    """Extract (providers, effort) from a {providers, effort} mapping, honoring
    partial entries (either field optional). Returns (None, None) when the
    input isn't a mapping. An empty providers list is treated as missing —
    otherwise resolution would short-circuit to an empty allow-list, which
    would crash downstream on `provider_preference[0]`."""
    if not isinstance(raw, Mapping):
        return None, None
    providers_raw = raw.get("providers")
    providers = (
        _normalize_provider_list(providers_raw) if providers_raw is not None else None
    )
    if providers is not None and not providers:
        providers = None
    effort_raw = raw.get("effort")
    effort = (
        effort_raw.strip()
        if isinstance(effort_raw, str) and effort_raw.strip()
        else None
    )
    return providers, effort


# Categories whose low/medium-effort routing is affected by the provider
# routing A/B verdict (direction d-cf39cb40). Strategic (max effort) and
# review (high effort) are excluded — the verdict only compares DeepSeek
# cost/quality on the cheaper tiers.
_VERDICT_AFFECTED_CATEGORIES: frozenset[str] = frozenset({"initiative", "build", "ops"})


def _apply_routing_verdict(
    providers: list[str],
    task_type: str,
    runtime_config: Mapping[str, Any],
) -> list[str]:
    """Reorder *providers* based on the provider routing A/B verdict.

    Reads ``provider_routing_verdict_state`` from *runtime_config* (set by
    the daily efficiency snapshot). Only reorders when the verdict is ADOPT
    or REJECT and the task's category is one of the affected low/medium-effort
    categories (initiative, build, ops).

    ADOPT → DeepSeek first.  REJECT → Anthropic first.
    INSUFFICIENT_DATA or absent → no change.
    """
    verdict_state = runtime_config.get("provider_routing_verdict_state")
    if not verdict_state or verdict_state not in ("ADOPT", "REJECT"):
        return providers
    category = _category_for_task_type(task_type, runtime_config)
    if category not in _VERDICT_AFFECTED_CATEGORIES:
        return providers
    preferred = "deepseek" if verdict_state == "ADOPT" else "anthropic"
    if providers and providers[0] == preferred:
        return providers
    return [preferred] + [p for p in providers if p != preferred]


def _ensure_migrated(runtime_config: Mapping[str, Any]) -> Mapping[str, Any]:
    """Translate any legacy routing/effort keys to the new shape at read time.

    Keeps the hot path cheap (no-op when new keys already present) and lets
    callers pass a legacy-shaped dict without going through sync. Lazy import
    avoids a module-level cycle with app.shared_state.
    """
    if not isinstance(runtime_config, Mapping):
        return runtime_config
    has_new = any(
        k in runtime_config
        for k in (
            "category_routing",
            "task_type_routing",
            "provider_settings_by_effort",
        )
    )
    has_legacy = any(
        k in runtime_config
        for k in (
            "provider_preference_by_category",
            "task_type_provider_preference",
            "task_type_effort_profile",
            "effort_profiles",
        )
    )
    if has_new or not has_legacy:
        return runtime_config
    from app.shared_state import _migrate_legacy_provider_config

    return _migrate_legacy_provider_config(dict(runtime_config))


def _resolve_routing_core(
    payload: Any,
    context: Any,
    task_type: str,
    runtime_config: Mapping[str, Any],
) -> dict[str, Any]:
    """Core layered routing resolution over already-extracted projected fields.

    Operates purely on ``(payload, context, task_type)`` so callers can drive
    it from SQL-projected fields (the first payload dict, the context dict, and
    the task type) without touching ``task.payloads`` — which would trigger a
    deferred JSONB load under the digest's ``load_only`` projection.

    :func:`resolve_task_routing` (the task-driven entry point) and
    :func:`resolve_effective_effort` (the projection-safe effort-only entry
    point) both delegate here, so the two can never drift.

    Order:
      1. payload.provider_preference / payload.effort — per-task explicit
      2. context.provider_preference / context.effort — per-task context
      3. task_type_routing[task_type] — surgical per-task-type override
      4. category_routing[category_of(task_type)] — category batch config
      5. category_routing["_default"] — global default
      6. fallback: {providers: ALL_PROVIDERS, effort: "medium"}

    Providers and effort resolve independently — a partial entry at one
    layer doesn't shadow the other at a less specific layer.
    """
    runtime_config = _ensure_migrated(runtime_config)
    providers: list[str] | None = None
    effort: str | None = None

    # Layer 1+2: per-task payload/context — payload wins.
    for source in (payload if isinstance(payload, Mapping) else None, context):
        if not isinstance(source, Mapping):
            continue
        if providers is None:
            candidate = _normalize_provider_list(source.get("provider_preference"))
            if candidate:
                providers = candidate
        if effort is None:
            raw = source.get("effort")
            if isinstance(raw, str) and raw.strip():
                effort = raw.strip()

    # Layer 3: task_type_routing[task_type]
    task_routing_all = runtime_config.get("task_type_routing", {}) or {}
    if isinstance(task_routing_all, Mapping):
        cand_providers, cand_effort = _routing_entry(task_routing_all.get(task_type))
        if providers is None and cand_providers:
            providers = cand_providers
        if effort is None and cand_effort:
            effort = cand_effort

    # Layer 4+5: category_routing[category] → category_routing["_default"]
    category_routing_all = runtime_config.get("category_routing", {}) or {}
    if isinstance(category_routing_all, Mapping):
        category = _category_for_task_type(task_type, runtime_config)
        for key in (category, "_default"):
            if providers is not None and effort is not None:
                break
            if not key:
                continue
            cand_providers, cand_effort = _routing_entry(category_routing_all.get(key))
            if providers is None and cand_providers:
                providers = cand_providers
            if effort is None and cand_effort:
                effort = cand_effort

    resolved_providers = providers if providers is not None else list(ALL_PROVIDERS)
    resolved_providers = _apply_routing_verdict(
        resolved_providers, task_type, runtime_config
    )
    capability = get_task_capability(task_type)
    if capability is not None and capability.allowed_providers is not None:
        # A task capability's verified execution boundary is stricter than
        # runtime routing.  In particular, an experiment verdict may reorder
        # providers but must never widen a capability to a provider whose
        # isolation contract has not been qualified.
        resolved_providers = [
            provider
            for provider in resolved_providers
            if provider in capability.allowed_providers
        ]
    return {
        "providers": resolved_providers,
        "effort": effort if effort is not None else DEFAULT_EFFORT_LEVEL,
    }


def resolve_task_routing(
    task: Any, runtime_config: Mapping[str, Any]
) -> dict[str, Any]:
    """Resolve ``{providers, effort}`` for a task, most-specific first.

    Thin wrapper: extracts ``(payload, context, task_type)`` from the task and
    delegates to :func:`_resolve_routing_core`. Reads ``task.payloads`` — use
    :func:`resolve_effective_effort` instead on the digest projection path,
    where ``task.payloads`` is deferred.
    """
    ctx = _task_context(task)
    payloads = getattr(task, "payloads", None) or []
    payload = payloads[0] if payloads else {}
    task_type = _task_type(task)
    return _resolve_routing_core(payload, ctx, task_type, runtime_config)


def resolve_effective_effort(
    payload: Any,
    context: Any,
    task_type: Any,
    runtime_config: Mapping[str, Any],
) -> str:
    """Resolve the EFFECTIVE routing effort from already-projected fields.

    This is the projection-safe counterpart to :func:`resolve_task_routing`:
    it takes the first payload dict, the context dict, and the task type
    directly (callers read them from SQL-projected fields) and reuses the same
    :func:`_resolve_routing_core` layered fall-through, so the resolved effort
    is identical to what the dispatcher would compute — without ever touching
    ``task.payloads``.

    ``task_type`` falls back to ``"create"`` (the dispatcher's default) when
    absent, matching :func:`_task_type`.
    """
    tt = (
        task_type.strip()
        if isinstance(task_type, str) and task_type.strip()
        else "create"
    )
    return _resolve_routing_core(payload, context, tt, runtime_config)["effort"]


def resolve_task_provider_preference(
    task: Any, runtime_config: Mapping[str, Any]
) -> list[str]:
    """Thin back-compat shim — returns just the provider list."""
    return resolve_task_routing(task, runtime_config)["providers"]


def resolve_task_model_preferences(
    task: Any, runtime_config: Mapping[str, Any]
) -> list[dict[str, str]]:
    ctx = _task_context(task)
    payloads = getattr(task, "payloads", None) or []
    payload = payloads[0] if payloads else {}
    explicit = payload.get("model_preferences") or ctx.get("model_preferences")
    raw_prefs = (
        explicit
        if isinstance(explicit, list)
        else (runtime_config.get("task_type_model_preferences", {}) or {}).get(
            _task_type(task)
        )
    )
    if not isinstance(raw_prefs, list):
        return []
    prefs: list[dict[str, str]] = []
    for pref in raw_prefs:
        if not isinstance(pref, Mapping):
            continue
        provider_name = _normalize_provider_name(pref.get("provider"))
        model_name = pref.get("model")
        if provider_name and isinstance(model_name, str) and model_name.strip():
            prefs.append({"provider": provider_name, "model": model_name.strip()})
    return prefs


def provider_selection_mode(runtime_config: Mapping[str, Any]) -> str:
    """Return the active provider-selection mode.

    Reads the canonical ``provider_selection_mode`` key first; falls back to
    the legacy ``cross_provider_fallback_enabled`` boolean so deployments
    that haven't migrated their persisted config still behave sensibly
    (True → "preferred", False → "exclusive"). Unknown values fall back to
    "balanced" — the safest default when a new account is added.
    """
    if not isinstance(runtime_config, Mapping):
        return "balanced"
    raw = runtime_config.get("provider_selection_mode")
    if isinstance(raw, str) and raw in _VALID_SELECTION_MODES:
        return raw
    legacy = runtime_config.get("cross_provider_fallback_enabled")
    if legacy is False:
        return "exclusive"
    if legacy is True:
        return "preferred"
    return "balanced"


def cross_provider_fallback_enabled(runtime_config: Mapping[str, Any]) -> bool:
    """Legacy boolean view of provider_selection_mode — True iff a task may
    cross provider boundaries (balanced or preferred). Kept for callers that
    still expect the pre-enum flag shape."""
    return provider_selection_mode(runtime_config) in ("balanced", "preferred")


def resolve_task_provider_settings(
    task: Any,
    runtime_config: Mapping[str, Any],
    provider_name: str,
) -> dict[str, Any]:
    """Resolve the provider-specific settings bag for a task.

    Five-layer deep-merge (later wins):
      1. provider_settings_defaults[provider] — effort-independent base
      2. provider_settings_by_effort[provider][effort] — per-effort matrix
         cell (includes model). ``effort`` comes from resolve_task_routing.
      3. task_type_provider_overrides[task_type][provider] — surgical overrides
      4. context.provider_settings[provider] — per-task context override
      5. payload.provider_settings[provider] — per-task payload override

    Validated against provider.settings_schema() before return. Unknown keys
    raise ValueError (fail-closed — typos should be loud).
    """
    runtime_config = _ensure_migrated(runtime_config)
    defaults_all = runtime_config.get("provider_settings_defaults", {}) or {}
    matrix_all = runtime_config.get("provider_settings_by_effort", {}) or {}
    overrides_all = runtime_config.get("task_type_provider_overrides", {}) or {}

    base = (
        defaults_all.get(provider_name) if isinstance(defaults_all, Mapping) else None
    )

    effort_bag = None
    effort = resolve_task_routing(task, runtime_config)["effort"]
    if isinstance(matrix_all, Mapping):
        per_provider = matrix_all.get(provider_name)
        if isinstance(per_provider, Mapping):
            candidate = per_provider.get(effort)
            if isinstance(candidate, Mapping):
                effort_bag = candidate

    task_type = _task_type(task)
    task_override = (
        overrides_all.get(task_type) if isinstance(overrides_all, Mapping) else None
    )
    type_bag = (
        task_override.get(provider_name) if isinstance(task_override, Mapping) else None
    )

    ctx = _task_context(task)
    payloads = getattr(task, "payloads", None) or []
    payload = payloads[0] if payloads else {}
    payload_settings = (
        payload.get("provider_settings") if isinstance(payload, Mapping) else None
    )
    ctx_settings = ctx.get("provider_settings")
    payload_bag = (
        payload_settings.get(provider_name)
        if isinstance(payload_settings, Mapping)
        else None
    )
    ctx_bag = (
        ctx_settings.get(provider_name) if isinstance(ctx_settings, Mapping) else None
    )

    merged = merge_settings(base, effort_bag, type_bag, ctx_bag, payload_bag)
    if not merged:
        return {}
    provider = get_provider(provider_name)
    try:
        return provider.validate_settings(merged)
    except ValueError:
        # A persisted settings cell can outlive a limited-availability model.
        # Retry only the provider-declared counterpart; other schema errors still
        # fail closed during the second validation.
        model = _settings_bag_model(merged)
        fallback = provider.fallback_model(model)
        if not fallback or fallback == model:
            raise
        fallback_settings = dict(merged)
        fallback_settings["model"] = fallback
        return provider.validate_settings(fallback_settings)


def _settings_bag_model(settings: Mapping[str, Any] | None) -> str | None:
    model = settings.get("model") if isinstance(settings, Mapping) else None
    return model.strip() if isinstance(model, str) and model.strip() else None


def _settings_for_selected_model(
    task: Any,
    runtime_config: Mapping[str, Any],
    provider_name: str,
    model: str | None,
) -> dict[str, Any]:
    """Return provider settings with the selector's model made authoritative.

    Effort-matrix bags normally include a model. A concrete model preference or
    explicit payload pin is resolved later and can choose a different model; if
    the old bag model survives into LLM_CLI_TRANSLATION, the agent trusts its
    -m argument and silently runs the bag model instead of LLM_MODEL. Preserve
    every other validated knob, but stamp the model actually selected.
    """
    settings = resolve_task_provider_settings(task, runtime_config, provider_name)
    bag_model = _settings_bag_model(settings)
    if not model or not bag_model:
        return settings
    if bag_model == model:
        return settings
    return {**settings, "model": model}


def _account_runtime_ready(acct: dict) -> bool:
    provider = (
        _normalize_provider_name(acct.get("provider"))
        or LLMProviderName.ANTHROPIC.value
    )
    auth_mode = _normalize_auth_mode(acct.get("auth_mode")) or LLMAuthMode.OAUTH.value
    if (
        provider == LLMProviderName.ANTHROPIC.value
        and auth_mode == LLMAuthMode.OAUTH.value
    ):
        return bool(acct.get("oauth_token"))
    return bool(acct.get("runtime_ready"))


# --- Max Plan Account Pool ---
# In-memory cache of DB-persisted accounts. Keeps active_tasks count (runtime only).
# Per-user pools keyed by user_id. Flat lookup by account_id for O(1) release.
_max_plan_pools: Dict[str, List[Dict[str, Any]]] = {}  # user_id → accounts
_account_by_id: Dict[str, Dict[str, Any]] = {}  # account_id → account dict
_max_plan_pool_lock = asyncio.Lock()
_pending_rl_persists: List[
    str
] = []  # Account IDs whose rate-limit state failed to persist
_pending_cap_persists: set = (
    set()
)  # Account IDs with dirty daily_task_count / disabled_reason to flush
_usage_persist_failing_accounts: set[str] = (
    set()
)  # Account IDs whose last usage-snapshot DB persist failed (after retry)
# Exact substrings from Claude Code CLI / Anthropic API rate-limit messages.
# Shared between classify_failure() and any caller that needs to detect a real
# Claude rate-limit (vs GitHub 429, GCP quota, Anthropic 529 overload, etc).
#
# Sourced from verbatim error strings reported in anthropics/claude-code issues.
# Do NOT add loose keywords like "credit", "billing", "429", "quota exceeded",
# or "overloaded" — those caused false positives that disabled accounts for
# unrelated failures (GCP quota, credit-card setup errors, Anthropic 529s).
RATE_LIMIT_PATTERNS = list(
    get_provider(LLMProviderName.ANTHROPIC).rate_limit_patterns(LLMAuthMode.OAUTH)
)

# Claude Max plan shares session quota across model tiers. Empirically:
#   - Opus 429 implies Sonnet is also rate-limited (shared session bucket);
#     not symmetric, since Opus has a stricter sub-bucket of its own.
#   - The current and legacy Claude aliases share the same Max-plan meter.  A
#     cooldown is therefore stored under one canonical Opus bucket and every
#     alias must normalize to it before availability is checked.
_MODEL_TIER_CASCADE: Dict[str, tuple] = {
    "claude-opus-4-8": ("claude-opus-4-8",),
    # Opus 5 is the opus-tier default but still books against the opus-4-8
    # meter head. The API gives Opus 5 its own rate-limit bucket, but this pool
    # tracks the *Max-plan session* quota, which is shared across tiers — and
    # opus-4-8 is the single head (_OPUS_MODEL) that health_check_primitives,
    # fleet_utilization, and deploy_health_alerts summarize. Giving Opus 5 its
    # own head would make those blind to opus-5 429s.
    "claude-opus-5": ("claude-opus-4-8",),
    # Sonnet shares the Max-plan session bucket with Opus (see note above), so
    # a Sonnet 5 429 books against the shared opus-4-8 meter like sonnet-4-6.
    "claude-sonnet-5": ("claude-opus-4-8",),
    # Legacy mappings kept so prior-queued tasks with older models still resolve
    # to the current shared meter (opus-4-8); all new tasks use 4-8.
    "claude-opus-4-7": ("claude-opus-4-8",),
    "claude-opus-4-6": ("claude-opus-4-8",),
    "claude-sonnet-4-6": ("claude-opus-4-8",),
    "claude-haiku-4-5-20251001": ("claude-opus-4-8",),
    # fable-5 availability window elapsed (2026-06-09 + ~2 weeks); cascade to
    # the shared opus meter like other legacy entries.
    "claude-fable-5": ("claude-opus-4-8",),
}

_OPUS_MODEL = "claude-opus-4-8"


def _normalize_rl_model(model: str | None) -> str | None:
    """Map every known shared Claude quota alias to its canonical bucket.

    ``disable_account_model`` stores a Claude Opus 5 limit under
    ``claude-opus-4-8`` (the shared Max-plan meter head).  Availability must
    use the exact same normalization; otherwise a later task selected as
    ``claude-opus-5`` sees no direct ``disabled_models`` entry and burns a
    fresh claim rediscovering the already-known weekly limit.
    """
    if model is None:
        return None
    return _MODEL_TIER_CASCADE.get(model, (model,))[0]


def cascade_disabled_models(model: str) -> tuple:
    """Return every model that should be marked disabled when `model` hits a 429.
    Normalizes Haiku to Opus first (shared meter). Falls back to the single
    model for unknown names so new variants don't silently cascade to the
    whole account."""
    normalized = _normalize_rl_model(model)
    return _MODEL_TIER_CASCADE.get(
        normalized, (normalized,) if normalized else (model,)
    )


# Rolling window for account health score computation
_HEALTH_WINDOW_SECONDS = 24 * 3600  # 24-hour rolling window
_OUTCOME_WINDOW_SECONDS = (
    7 * 24 * 3600
)  # 7-day rolling window for success-rate tracking
_OUTCOME_EVENTS_CAP = 500  # per-account cardinality cap before JSONB persistence

# Accounts whose 24h health score is below this threshold are excluded from
# the healthy candidate list when at least one alternative exists. Prevents
# chronically rate-limited accounts from receiving new tasks while still
# allowing the account to be selected as a last resort (no deadlock).
_HEALTH_SCORE_DISQUALIFY_THRESHOLD = -4

# Balanced-mode success-rate bias: accounts whose 7d success rate is more than
# this margin below the pool median are deprioritized when alternatives exist.
# Sparse accounts (< 3 outcomes) return None and are never penalized.
_SUCCESS_RATE_DEPRIORITIZE_MARGIN = 0.20
_LATENCY_MIN_SAMPLES = 3

# Startup-failure breaker: an account that consistently fails to start agents
# (VM created but agent never claims) across multiple zones is account-sick,
# not zone-sick. These constants gate the per-account cooldown.
_STARTUP_FAILURE_WINDOW_SECONDS = 2 * 3600  # 2h rolling window for event collection
_STARTUP_FAILURE_THRESHOLD = 5  # consecutive failures to trip the cooldown
_STARTUP_FAILURE_MIN_ZONES = (
    2  # multi-zone signature required (single-zone = zone fault)
)
_STARTUP_FAILURE_COOLDOWN_SECONDS = 30 * 60  # flat 30min cooldown after tripping


def _compute_health_score(acct: Dict[str, Any], now: datetime | None = None) -> float:
    """Compute a rolling 24h health score. Higher is healthier.

    Formula: completions_24h - 2 * rl_events_24h
    Accounts with no history score 0 (neutral). A single rate-limit event
    weighs twice as much as a completion to penalize chronically problematic
    accounts even when they occasionally succeed.
    """
    now = now or _utcnow()
    cutoff = now - timedelta(seconds=_HEALTH_WINDOW_SECONDS)
    rl_count = sum(1 for t in acct.get("rl_events", []) if t > cutoff)
    completion_count = sum(
        1 for e in acct.get("completion_events", []) if e["at"] > cutoff
    )
    return completion_count - 2.0 * rl_count


def _compute_success_rate_7d(
    acct: Dict[str, Any], now: datetime | None = None
) -> float | None:
    """Return 7-day task success rate, or None when fewer than 3 outcomes exist.

    Requires ≥3 outcomes so brand-new accounts aren't misranked on one unlucky
    failure. Returns a float in [0.0, 1.0] when enough data is present.
    """
    now = now or _utcnow()
    cutoff = now - timedelta(seconds=_OUTCOME_WINDOW_SECONDS)
    events = [e for e in acct.get("outcome_events", []) if e["at"] > cutoff]
    if len(events) < 3:
        return None
    return sum(1 for e in events if e["success"]) / len(events)


def _compute_success_rate_7d_for_type(
    acct: Dict[str, Any], task_type: str, now: datetime | None = None
) -> float | None:
    """Return 7-day success rate scoped to task_type; None when < 3 typed outcomes.

    Sparse accounts return None so callers can fall back to the aggregate.
    """
    now = now or _utcnow()
    cutoff = now - timedelta(seconds=_OUTCOME_WINDOW_SECONDS)
    events = [
        e
        for e in acct.get("outcome_events", [])
        if e["at"] > cutoff and e.get("task_type") == task_type
    ]
    if len(events) < 3:
        return None
    return sum(1 for e in events if e["success"]) / len(events)


def _effective_success_rate(
    acct: Dict[str, Any], task_type: str | None, now: datetime
) -> float | None:
    """Task-type-scoped success rate, falling back to aggregate when the typed
    sample is too sparse or task_type is unknown."""
    if task_type:
        rate = _compute_success_rate_7d_for_type(acct, task_type, now)
        if rate is not None:
            return rate
    return _compute_success_rate_7d(acct, now)


def _compute_avg_duration_24h(
    acct: Dict[str, Any], now: datetime | None = None
) -> float | None:
    """Return 24h average task duration in seconds, or None when fewer than
    _LATENCY_MIN_SAMPLES completions exist so noise doesn't whip the picker."""
    now = now or _utcnow()
    cutoff = now - timedelta(seconds=_HEALTH_WINDOW_SECONDS)
    durations = [
        e["duration_seconds"]
        for e in acct.get("completion_events", [])
        if e["at"] > cutoff and e.get("duration_seconds") is not None
    ]
    if len(durations) < _LATENCY_MIN_SAMPLES:
        return None
    return sum(durations) / len(durations)


def get_account_health_stats(
    acct: Dict[str, Any], now: datetime | None = None
) -> Dict[str, Any]:
    """Return observable health stats for an account (used by admin endpoints)."""
    now = now or _utcnow()
    cutoff = now - timedelta(seconds=_HEALTH_WINDOW_SECONDS)
    rl_events = [t for t in acct.get("rl_events", []) if t > cutoff]
    completion_events = [
        e for e in acct.get("completion_events", []) if e["at"] > cutoff
    ]
    durations = [
        e["duration_seconds"]
        for e in completion_events
        if e.get("duration_seconds") is not None
    ]
    outcome_cutoff_7d = now - timedelta(seconds=_OUTCOME_WINDOW_SECONDS)
    outcome_events_7d = [
        e for e in acct.get("outcome_events", []) if e["at"] > outcome_cutoff_7d
    ]
    task_types = sorted(
        {e.get("task_type") for e in outcome_events_7d if e.get("task_type")}
    )
    success_by_type: dict[str, float] = {}
    for tt in task_types:
        typed = [e for e in outcome_events_7d if e.get("task_type") == tt]
        if len(typed) >= 3:
            success_by_type[tt] = round(
                sum(1 for e in typed if e["success"]) / len(typed), 4
            )
    return {
        "rl_events_24h": len(rl_events),
        "completions_24h": len(completion_events),
        "avg_duration_seconds": round(sum(durations) / len(durations), 1)
        if durations
        else None,
        "health_score": _compute_health_score(acct, now=now),
        "success_rate_7d": _compute_success_rate_7d(acct, now=now),
        "success_rate_by_type_7d": success_by_type if success_by_type else None,
    }


def _prune_health_events(acct: Dict[str, Any], now: datetime) -> None:
    """Prune health event lists to the 24h window (call under lock)."""
    cutoff = now - timedelta(seconds=_HEALTH_WINDOW_SECONDS)
    if "rl_events" in acct:
        acct["rl_events"] = [t for t in acct["rl_events"] if t > cutoff]
    if "completion_events" in acct:
        acct["completion_events"] = [
            e for e in acct["completion_events"] if e["at"] > cutoff
        ]


def _prune_outcome_events(acct: Dict[str, Any], now: datetime) -> None:
    """Prune outcome_events to the 7-day window (call under lock)."""
    cutoff = now - timedelta(seconds=_OUTCOME_WINDOW_SECONDS)
    if "outcome_events" in acct:
        acct["outcome_events"] = [e for e in acct["outcome_events"] if e["at"] > cutoff]


async def record_completion_event(
    acct_id: str, duration_seconds: float, task_type: str | None = None
) -> None:
    """Record a successful task completion for account health tracking.

    Should be called whenever a Max Plan task completes successfully.
    Acquires _max_plan_pool_lock. Safe to call when acct_id is absent (no-op).
    """
    if not acct_id:
        return
    async with _max_plan_pool_lock:
        acct = _account_by_id.get(acct_id)
        if acct is None:
            return
        now = _utcnow()
        _prune_health_events(acct, now)
        _prune_outcome_events(acct, now)
        acct.setdefault("completion_events", []).append(
            {"at": now, "duration_seconds": duration_seconds}
        )
        acct.setdefault("outcome_events", []).append(
            {"at": now, "success": True, "task_type": task_type}
        )


async def record_task_outcome(
    acct_id: str, success: bool, task_type: str | None = None
) -> None:
    """Record a task outcome for 7-day success-rate tracking.

    Call for CODE_ERROR and ACCOUNT_FAILURE endings where no other hook
    records the outcome. RATE_LIMIT failures are tracked automatically via
    disable_account_model; COMPLETED tasks via record_completion_event.
    Safe to call when acct_id is absent (no-op). Acquires _max_plan_pool_lock.
    """
    if not acct_id:
        return
    async with _max_plan_pool_lock:
        acct = _account_by_id.get(acct_id)
        if acct is None:
            return
        now = _utcnow()
        _prune_outcome_events(acct, now)
        acct.setdefault("outcome_events", []).append(
            {"at": now, "success": success, "task_type": task_type}
        )


# Backward compat: flat view for code that needs all accounts
def _all_accounts() -> List[Dict[str, Any]]:
    return list(_account_by_id.values())


async def load_max_plan_pool():
    """Load Max plan accounts from database into per-user pools on startup.
    Reconcile active slots from durable task state.

    PROVISIONING/IN_PROGRESS tasks own their normal runtime-account slot.  A
    QUEUED task owns a slot only while it carries the warm-pool reservation
    marker written in the same transaction as its pool lease.  Including that
    marker is important on restart: rebuilding the in-memory pool must not make
    a handed-off-but-not-yet-claimed slot available a second time.
    """
    from sqlmodel import select as sql_select
    from app.models import MaxPlanAccount, Project, Task, TaskStatus
    from app.warm_pool import WARM_POOL_RESERVATION_ACCOUNT_KEY

    async with AsyncSessionLocal() as session:
        result = await session.execute(sql_select(MaxPlanAccount))
        accounts = result.scalars().all()

        # Projects that still exist — lets the prune evict a DELETED project's
        # entry at once instead of waiting a daily window to zero its counters.
        live_project_ids: Set[str] = {
            str(p)
            for p in (await session.execute(sql_select(Project.id))).scalars().all()
        }

        # Count actually active tasks plus durable QUEUED warm-pool handoffs to
        # reconcile after restarts/rebuilds.
        active_tasks = (
            (
                await session.execute(
                    sql_select(Task).where(
                        Task.status.in_(
                            [TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS]
                        )
                        | (
                            (Task.status == TaskStatus.QUEUED)
                            & Task.context[
                                WARM_POOL_RESERVATION_ACCOUNT_KEY
                            ].as_string().isnot(None)
                        )
                    )
                )
            )
            .scalars()
            .all()
        )
        active_per_account: Dict[str, int] = {}
        active_per_account_project: Dict[str, Dict[str, int]] = {}
        for t in active_tasks:
            if t.status == TaskStatus.QUEUED:
                # The SQL predicate bounds the rebuild to marker-bearing queued
                # rows. Keep this check as a defensive guard against malformed
                # JSON or future query changes.
                from app.warm_pool import pool_reservation_account_id

                acct_id = pool_reservation_account_id(t)
                if not acct_id:
                    continue
            else:
                acct_id = get_task_llm_account_id(t)
            if acct_id:
                active_per_account[acct_id] = active_per_account.get(acct_id, 0) + 1
                # Per-project reconciliation: count active tasks per (account, project).
                pid = str(t.project_id) if t.project_id else None
                if pid:
                    proj_map = active_per_account_project.setdefault(acct_id, {})
                    proj_map[pid] = proj_map.get(pid, 0) + 1

        # Resident auxiliary workers are model processes without Task rows. Their
        # bounded leases are persisted inside the owning project's Resident state,
        # so include every unexpired reserved lease in the concurrency ledger
        # while deliberately NOT touching daily_task_count. This is also the
        # crash/restart reconciliation path: expired records disappear from the
        # capacity floor without relying on an in-memory release callback.
        now = _utcnow()
        call_rows = await session.execute(sql_select(Project.id, Project.vision))
        for project_id, vision in call_rows.all():
            resident = (vision or {}).get("_resident_strategic_session")
            if (
                not isinstance(resident, dict)
                or resident.get("status") != "active"
                or bool(resident.get("stop_requested"))
            ):
                continue
            for lease_key in ("call", "typed_fast"):
                lease = resident.get(lease_key)
                if not isinstance(lease, dict) or lease.get("status") != "active":
                    continue
                raw_expiry = lease.get("expires_at")
                try:
                    expiry = datetime.fromisoformat(str(raw_expiry).replace("Z", "+00:00"))
                    if expiry.tzinfo is not None:
                        expiry = expiry.replace(tzinfo=None) - expiry.utcoffset()
                except (TypeError, ValueError):
                    continue
                if expiry <= now:
                    continue
                account_id = str(lease.get("account_id") or "")
                if not account_id or not bool(lease.get("slot_reserved")):
                    continue
                active_per_account[account_id] = active_per_account.get(account_id, 0) + 1
                project_counts = active_per_account_project.setdefault(account_id, {})
                pid = str(project_id)
                project_counts[pid] = project_counts.get(pid, 0) + 1

        # Preserve in-memory usage-meter state across the periodic rebuild —
        # this loader runs every ~300s from the orchestrator cycle, and the
        # probe cache would otherwise be wiped with every refresh (the meter
        # showed permanently-unknown in prod because of exactly that).
        previous_usage_state: Dict[str, tuple] = {
            aid: (
                e.get("usage_snapshot"),
                e.get("usage_snapshot_at"),
                e.get("usage_probe_failed_at"),
                e.get("usage_probe_attempt_at"),
                e.get("usage_probe_failure_kind"),
            )
            for aid, e in _account_by_id.items()
        }
        _max_plan_pools.clear()
        _account_by_id.clear()
        for acct in accounts:
            acct_id = str(acct.id)
            provider = acct.provider or LLMProviderName.ANTHROPIC
            auth_mode = acct.auth_mode or LLMAuthMode.OAUTH
            runtime_secret = decrypt_field(acct.secret_blob_ref) or decrypt_field(acct.oauth_token)
            secret_fetch_failed = False
            if runtime_secret and runtime_secret.startswith("projects/"):
                try:
                    from app.secrets_manager import get_user_secret

                    runtime_secret = await get_user_secret(runtime_secret)
                except Exception:
                    logger.warning(
                        f"[POOL] Usage meter: failed to fetch secret for account {acct.name}"
                        " — meter will be dark"
                    )
                    runtime_secret = None
                    secret_fetch_failed = True
            token = (
                runtime_secret
                if provider == LLMProviderName.ANTHROPIC
                and auth_mode == LLMAuthMode.OAUTH
                else None
            )
            # Per-project state: persisted JSONB reconciled against the live task
            # counts, then BOUNDED (see app/max_plan_project_state.py — without
            # the prune, one entry per project ever dispatched on this account
            # survives forever, recopied on every rebuild and re-persisted on
            # every cap flush).
            reconciled_project_state = reconcile_project_state(
                getattr(acct, "project_state", None),
                active_per_account_project.get(acct_id, {}),
                live_project_ids=live_project_ids,
            )
            entry = {
                "id": acct_id,
                "user_id": acct.user_id,
                "name": acct.name,
                "provider": provider.value
                if isinstance(provider, LLMProviderName)
                else str(provider),
                "auth_mode": auth_mode.value
                if isinstance(auth_mode, LLMAuthMode)
                else str(auth_mode),
                "secret_blob_ref": decrypt_field(acct.secret_blob_ref),
                "provider_metadata": acct.provider_metadata or {},
                "runtime_secret": runtime_secret,
                "oauth_token": token,
                "runtime_ready": bool(runtime_secret),
                "secret_fetch_failed": secret_fetch_failed,
                "claude_config_json": decrypt_field(acct.claude_config_json),
                "enabled": acct.enabled,
                "active_tasks": active_per_account.get(acct_id, 0),
                "total_tasks": acct.total_tasks,
                "token_created_at": acct.token_created_at.isoformat()
                if acct.token_created_at
                else None,
                "daily_task_cap": acct.daily_task_cap,
                "max_concurrent_tasks": acct.max_concurrent_tasks,
                # Per-project caps (persisted config — null or empty dict = unlimited).
                "project_daily_task_cap": getattr(acct, "project_daily_task_cap", None) or {},
                "project_max_concurrent_tasks": getattr(acct, "project_max_concurrent_tasks", None) or {},
                "auto_disable_after_rate_limits": acct.auto_disable_after_rate_limits,
                "daily_task_count": acct.daily_task_count,
                "daily_task_count_reset_at": acct.daily_task_count_reset_at,
                "disabled_reason": acct.disabled_reason,
                "disabled_at": acct.disabled_at,
                # Per-project runtime state (reconciled from DB on restart).
                "project_state": reconciled_project_state,
            }
            # Usage-meter snapshot: carry over the fresher of the previous
            # in-memory probe result and the DB-persisted rate_limit_usage
            # blob, so the meter survives both the periodic pool rebuild and
            # orchestrator restarts.
            _restore_usage_state(
                entry,
                previous_usage_state.get(acct_id),
                getattr(acct, "rate_limit_usage", None),
            )
            # Restore rate-limit state from DB (skip expired cooldowns)
            rl_state = acct.rate_limit_state or {}
            now = _utcnow()
            db_disabled_models = rl_state.get("disabled_models", {})
            if db_disabled_models:
                restored_dm = {}
                for model_name, info in db_disabled_models.items():
                    at_str = info.get("at")
                    if at_str:
                        at_dt = datetime.fromisoformat(at_str)
                        restored_info: dict[str, Any] = {
                            "hits": info.get("hits", 1),
                            "at": at_dt,
                        }
                        # Preserve structured fields (kind, retry_after_seconds,
                        # regression metadata) across restarts so quota_exhausted
                        # / regression accounts aren't silently demoted to an
                        # exponential-rate-limit cooldown.
                        if info.get("kind") in (
                            "quota_exhausted",
                            "regression",
                            "quarantine",
                        ):
                            restored_info["kind"] = info["kind"]
                        hint = info.get("retry_after_seconds")
                        if isinstance(hint, (int, float)) and hint > 0:
                            restored_info["retry_after_seconds"] = int(hint)
                        if info.get("provider"):
                            restored_info["provider"] = info["provider"]
                        stats = info.get("regression_stats")
                        if isinstance(stats, dict):
                            restored_info["regression_stats"] = dict(stats)
                        if (now - at_dt).total_seconds() < _cooldown_seconds_for_info(
                            restored_info
                        ):
                            restored_dm[model_name] = restored_info
                if restored_dm:
                    entry["disabled_models"] = restored_dm
                    logger.info(
                        f"[STARTUP] Restored rate-limit state for {acct.name}: {list(restored_dm.keys())}"
                    )
            # Restore outcome_events from rate_limit_state (7d window)
            outcome_events_raw = rl_state.get("outcome_events", [])
            if outcome_events_raw:
                oe_now = _utcnow()
                oe_cutoff = oe_now - timedelta(seconds=_OUTCOME_WINDOW_SECONDS)
                restored_oe = []
                for ev in outcome_events_raw:
                    at_str = ev.get("at")
                    if at_str:
                        at_dt = datetime.fromisoformat(at_str)
                        if at_dt > oe_cutoff:
                            restored_oe.append(
                                {
                                    "at": at_dt,
                                    "success": bool(ev.get("success")),
                                    "task_type": ev.get("task_type"),
                                }
                            )
                if restored_oe:
                    entry["outcome_events"] = restored_oe
            # Restore startup-failure cooldown if still active
            sf_at_str = rl_state.get("startup_failure_cooldown_at")
            if sf_at_str:
                try:
                    sf_at_dt = datetime.fromisoformat(sf_at_str)
                    elapsed = (now - sf_at_dt).total_seconds()
                    if elapsed < _STARTUP_FAILURE_COOLDOWN_SECONDS:
                        entry["startup_failure_cooldown_at"] = sf_at_dt
                        # Restore the reason text from the blob; if the cap-persist
                        # hadn't flushed disabled_reason before the restart, reconstruct
                        # it so the admin health UI always shows why the account is benched.
                        sf_reason = (
                            rl_state.get("startup_failure_disabled_reason") or ""
                        )
                        if sf_reason.startswith("startup-failure cooldown"):
                            if not (entry.get("disabled_reason") or "").startswith(
                                "startup-failure cooldown"
                            ):
                                entry["disabled_reason"] = sf_reason
                        elif not (entry.get("disabled_reason") or "").startswith(
                            "startup-failure cooldown"
                        ):
                            entry["disabled_reason"] = (
                                "startup-failure cooldown: agent-startup failures detected"
                            )
                        logger.info(
                            f"[STARTUP] Restored startup-failure cooldown for {acct.name}: "
                            f"{(_STARTUP_FAILURE_COOLDOWN_SECONDS - elapsed):.0f}s remaining"
                        )
                    else:
                        # Cooldown expired — clear stale disabled_reason so the UI
                        # doesn't show an indefinite startup-failure reason after restart.
                        reason = entry.get("disabled_reason") or ""
                        if reason.startswith("startup-failure cooldown"):
                            entry["disabled_reason"] = None
                except (ValueError, TypeError):
                    pass
            _max_plan_pools.setdefault(acct.user_id, []).append(entry)
            _account_by_id[acct_id] = entry
        total_active = sum(active_per_account.values())
        logger.info(
            f"[STARTUP] Loaded {len(_account_by_id)} Max plan accounts for {len(_max_plan_pools)} users ({total_active} active tasks reconciled)"
        )


_DAILY_WINDOW_SECONDS = 24 * 60 * 60


def _stage_usage_cap_mutation(acct: dict, mutation: dict) -> None:
    acct.setdefault("_pending_usage_cap_mutations", []).append(mutation)
    _pending_cap_persists.add(acct["id"])


def _stage_usage_delta(acct: dict, delta: int, project_id: str | None) -> None:
    generation = acct.get("daily_task_count_reset_at")
    _stage_usage_cap_mutation(
        acct,
        {
            "op": "usage_delta",
            "generation": generation.isoformat() if generation else None,
            "observed_generation_missing": bool(
                acct.get("_daily_generation_initialized_from_none")
            ),
            "delta": int(delta),
            "project_id": project_id,
            "project_delta": int(delta) if project_id else 0,
        },
    )


def _daily_reset_if_elapsed(acct: dict, now: datetime | None = None) -> bool:
    """Reset the daily counter if the 24h window since reset_at has elapsed (or
    no reset_at has been set yet). Returns True when a reset happened so callers
    can mark the account dirty for persistence."""
    now = now or _utcnow()
    reset_at = acct.get("daily_task_count_reset_at")
    if reset_at is None:
        acct["daily_task_count_reset_at"] = now
        acct["_daily_generation_initialized_from_none"] = True
        acct["daily_task_count"] = int(acct.get("daily_task_count") or 0)
        return False
    if (now - reset_at).total_seconds() >= _DAILY_WINDOW_SECONDS:
        acct["daily_task_count"] = 0
        acct["daily_task_count_reset_at"] = now
        # Also reset per-project daily_task_count in project_state so
        # project_daily_task_cap is not a permanent lifetime cap.
        pstate = acct.get("project_state")
        if pstate:
            for proj_entry in pstate.values():
                if isinstance(proj_entry, dict) and "daily_task_count" in proj_entry:
                    proj_entry["daily_task_count"] = 0
        _stage_usage_cap_mutation(
            acct,
            {
                "op": "daily_reset",
                "observed_generation": reset_at.isoformat(),
                "generation": now.isoformat(),
            },
        )
        # Zeroed entries are COLLECTED on the next ~300s pool rebuild (which is
        # the growth vector: it copies the map forward and re-persists it), not
        # here on the hot dispatch path. See max_plan_project_state.
        return True
    return False


def _account_at_cap(acct: dict) -> str | None:
    """Return a reason string if the account is at any usage cap, else None."""
    daily_cap = acct.get("daily_task_cap")
    if daily_cap is not None and int(acct.get("daily_task_count") or 0) >= int(
        daily_cap
    ):
        return f"daily cap ({daily_cap}) reached"
    concurrent_cap = acct.get("max_concurrent_tasks")
    if concurrent_cap is not None and int(acct.get("active_tasks") or 0) >= int(
        concurrent_cap
    ):
        return f"concurrent cap ({concurrent_cap}) reached"
    oauth_refresh_reason = _codex_oauth_refresh_serialization_reason(acct)
    if oauth_refresh_reason:
        return oauth_refresh_reason
    return None


def _project_at_cap(acct: dict, project_id: str | None) -> str | None:
    """Return a reason string if *project_id* is at its per-project cap on
    *acct*, else None.

    When *project_id* is None or the account has no per-project cap set for
    that project, returns None (no per-project gating). This makes the
    per-project layer fully backward-compatible: absent caps = no limit.
    """
    if project_id is None:
        return None
    # Check per-project daily cap.
    project_daily_cap = acct.get("project_daily_task_cap") or {}
    daily_cap = project_daily_cap.get(project_id)
    if daily_cap is not None:
        proj_state = (acct.get("project_state") or {}).get(project_id, {}) or {}
        proj_daily = int(proj_state.get("daily_task_count", 0))
        if proj_daily >= int(daily_cap):
            return f"project daily cap ({daily_cap}) reached"
    # Check per-project concurrent cap.
    project_concurrent_cap = acct.get("project_max_concurrent_tasks") or {}
    concurrent_cap = project_concurrent_cap.get(project_id)
    if concurrent_cap is not None:
        proj_state = (acct.get("project_state") or {}).get(project_id, {}) or {}
        proj_active = int(proj_state.get("active_tasks", 0))
        if proj_active >= int(concurrent_cap):
            return f"project concurrent cap ({concurrent_cap}) reached"
    return None


_QUOTA_EXHAUSTED_COOLDOWN_SECONDS = 24 * 60 * 60  # flat 24h for billing walls
_QUARANTINE_COOLDOWN_SECONDS = (
    15 * 60
)  # flat 15-min recheck for confidence-gated quarantine
_REGRESSION_COOLDOWN_SECONDS = 6 * 60 * 60  # flat 6h reprobe for regression quarantine
QUARANTINE_CONSECUTIVE_N = 3  # consecutive qualifying failures to trip the quarantine
QUARANTINE_CONFIDENCE_THRESHOLD = 0.8  # minimum per-record confidence to count
_QUARANTINE_ELIGIBLE_CLASSES = frozenset(
    {"RATE_LIMIT", "PROVISION_FAILURE", "DEAD_AUTH"}
)


def _serialize_disabled_model_info(info: dict) -> dict:
    """JSON-safe snapshot of a disabled_models entry for DB persistence.

    Only emits fields that are set. `at` becomes an ISO string; numeric
    retry_after_seconds is preserved; string kind is preserved.
    """
    out: dict[str, Any] = {
        "hits": info.get("hits", 1),
        "at": info["at"].isoformat(),
    }
    if info.get("kind") in ("quota_exhausted", "quarantine", "regression"):
        out["kind"] = info["kind"]
    hint = info.get("retry_after_seconds")
    if isinstance(hint, (int, float)) and hint > 0:
        out["retry_after_seconds"] = int(hint)
    if info.get("provider"):
        out["provider"] = info["provider"]
    stats = info.get("regression_stats")
    if isinstance(stats, dict):
        out["regression_stats"] = dict(stats)
    return out


def _cooldown_seconds_for_info(info: dict) -> int:
    """Compute remaining-effective cooldown window (seconds) for a disabled_models entry.

    Rules:
    - kind="quota_exhausted" → flat 24h. These are billing walls; the
      exponential-backoff curve doesn't apply because the error doesn't
      resolve on clock.
    - kind="regression" → flat 6h reprobe. Regression triggers on slow
      drift in success rate; a short cooldown would keep cycling the
      bad combination.
    - Otherwise exponential: min(30 * 2^(hits-1), 240) minutes.
    - Server-provided retry_after_seconds is treated as a floor — we never
      shorten below it, and we never extend below the exponential value.
    """
    if info.get("kind") == "quota_exhausted":
        return _QUOTA_EXHAUSTED_COOLDOWN_SECONDS
    if info.get("kind") == "quarantine":
        return _QUARANTINE_COOLDOWN_SECONDS
    if info.get("kind") == "regression":
        return _REGRESSION_COOLDOWN_SECONDS
    hits = info.get("hits", 1)
    exponential = min(30 * (2 ** (hits - 1)), 240) * 60
    hint = info.get("retry_after_seconds")
    if isinstance(hint, (int, float)) and hint > exponential:
        return int(hint)
    return exponential


def _check_quarantine_trigger(account_id: str) -> bool:
    """Return True if the last QUARANTINE_CONSECUTIVE_N failure records for account_id
    are all RATE_LIMIT or PROVISION_FAILURE with confidence >= QUARANTINE_CONFIDENCE_THRESHOLD.

    Lazy-imports get_account_failure_class_records to avoid the module-level cycle:
    failure_classification already imports from max_plan_pool at module load time.
    """
    from app.failure_classification import (
        get_account_failure_class_records,
    )  # lazy — avoids cycle

    records = get_account_failure_class_records(account_id)
    if len(records) < QUARANTINE_CONSECUTIVE_N:
        return False
    last_n = records[-QUARANTINE_CONSECUTIVE_N:]
    return all(
        cls in _QUARANTINE_ELIGIBLE_CLASSES
        and (conf or 0.0) >= QUARANTINE_CONFIDENCE_THRESHOLD
        for cls, conf, _ts in last_n
    )


def _account_state_for_model(
    acct: dict, model: str | None, now: datetime | None = None
) -> str:
    """Derive the circuit-breaker state for an (account, model) pair.

    Returns "healthy", "probing", or "unavailable":
    - "healthy"     — usable with full concurrency
    - "probing"     — cooldown elapsed but quota not yet verified via a live task;
                      must be serialized to one task at a time by the caller
    - "unavailable" — missing token, legacy-disabled, within cooldown, or
                      (conservative) model=None on an account with any disables

    Check is a direct lookup: only disabled_models[model] matters for model's
    state. The mark path (agent_status.py) writes all cascade entries up-front
    (Opus 429 → opus, sonnet, haiku entries); the clear path (on success)
    removes the cascade scope. Each entry is independent.

    Also skips accounts that are confirmed NOT to have the requested model
    via the availability cache (e.g. Gemini preview access isn't enabled on
    this key). Accounts with unknown availability (never probed or probe
    failed) are treated as may-have-access and go through the normal
    healthy/probing path — we fail open so a failed refresh doesn't starve
    a key that actually works.
    """
    if not _account_runtime_ready(acct):
        return "unavailable"
    if not acct.get("enabled", True):
        return "unavailable"
    # Startup-failure cooldown: account tripped the multi-zone breaker.
    sf_cooldown_at = acct.get("startup_failure_cooldown_at")
    if sf_cooldown_at is not None:
        _now = now or _utcnow()
        if (_now - sf_cooldown_at).total_seconds() < _STARTUP_FAILURE_COOLDOWN_SECONDS:
            return "unavailable"
    # Availability filter: only consult when we have a concrete model.
    # account_has_model returns True, False, or None (unknown); False is
    # the only value that forces a skip.
    if model is not None:
        from app.account_model_availability import account_has_model

        if account_has_model(acct, model) is False:
            return "unavailable"
    dm = acct.get("disabled_models") or {}
    if not dm:
        return "healthy"
    if model is None:
        # Conservative: unknown model with any disable is unavailable. Preserves
        # the existing behavior for health checks and the autonomous-research gate.
        return "unavailable"
    # Haiku shares Opus's meter — look up Opus's entry.
    orig_model = model
    model = _normalize_rl_model(model)
    info = dm.get(model)
    # Regression entries are keyed under the actual model name (not normalized),
    # so fall back to the un-normalized key if the normalized lookup misses.
    if info is None and model != orig_model:
        info = dm.get(orig_model)
    if info is None:
        return "healthy"
    at_dt = info.get("at")
    if at_dt is None:
        return "unavailable"
    cooldown_seconds = _cooldown_seconds_for_info(info)
    now = now or _utcnow()
    if (now - at_dt).total_seconds() < cooldown_seconds:
        return "unavailable"
    return "probing"


def _account_unavailable_reason(
    acct: dict, model: str | None, now: datetime | None = None
) -> str:
    """The WHY behind an ``"unavailable"`` verdict from _account_state_for_model.

    ``_account_state_for_model`` collapses five distinct causes (no token,
    disabled, startup-failure cooldown, model-not-on-account, rate-limit
    cooldown) into one string, so a caller that only sees "unavailable" cannot
    tell an operator what to fix. This recomputes the cause — lazily, only for
    accounts a selector actually skipped, so the hot path stays a single state
    lookup.

    Mirrors the branch ORDER of ``_account_state_for_model``; the two must be
    kept in sync or the reason will name the wrong cause.
    """
    if not _account_runtime_ready(acct):
        return "credentials not runtime-ready"
    if not acct.get("enabled", True):
        return f"account disabled ({acct.get('disabled_reason') or 'manually'})"
    sf_cooldown_at = acct.get("startup_failure_cooldown_at")
    if sf_cooldown_at is not None:
        sf_elapsed = ((now or _utcnow()) - sf_cooldown_at).total_seconds()
        if sf_elapsed < _STARTUP_FAILURE_COOLDOWN_SECONDS:
            left = int(_STARTUP_FAILURE_COOLDOWN_SECONDS - sf_elapsed)
            return f"startup-failure cooldown ({left}s left)"
    if model is not None:
        from app.account_model_availability import account_has_model

        if account_has_model(acct, model) is False:
            return f"model {model} not available on this account"
    dm = acct.get("disabled_models") or {}
    if not dm:
        return "unavailable"
    if model is None:
        return "no concrete model requested and account has active disables"
    # Haiku shares Opus's meter (normalized key); regression entries are keyed
    # un-normalized — same two-step lookup as the state function.
    info = dm.get(_normalize_rl_model(model)) or dm.get(model)
    if info is None:
        return "unavailable"
    at_dt = info.get("at")
    if at_dt is None:
        return f"model {model} disabled (no cooldown timestamp)"
    left = int(
        _cooldown_seconds_for_info(info)
        - ((now or _utcnow()) - at_dt).total_seconds()
    )
    kind = info.get("kind") or "rate-limit"
    return f"model {model} in {kind} cooldown ({max(left, 0)}s left)"


def _account_available_for_model(acct: dict, model: str | None) -> bool:
    """True if the account is either healthy OR probing for the given model.
    Probing accounts are serialized inside select_max_plan_account; outside
    callers (blacklist pruning, gates, health checks) treat them as usable."""
    return _account_state_for_model(acct, model) != "unavailable"


def summarize_pool_state(
    model: str | None, user_id: str | None = None, project_id: str | None = None
) -> Dict[str, Any]:
    """Summarize account pool state for diagnostics. Returns counts per state
    plus the earliest cooldown recovery time across rate-limited accounts.

    If user_id is provided, only that user's accounts are considered; otherwise
    the entire pool is summarized.

    If project_id is provided, only accounts that have per-project usage for
    that project are included in the summary.
    """
    accts = _max_plan_pools.get(user_id, []) if user_id else _all_accounts()
    if project_id:
        accts = [
            a for a in accts
            if (a.get("project_state") or {}).get(project_id, {}).get("active_tasks", 0) > 0
            or (a.get("project_state") or {}).get(project_id, {}).get("daily_task_count", 0) > 0
        ]
    now = _utcnow()
    healthy = probing = rate_limited = 0
    other_unavailable = 0
    earliest_recovery: datetime | None = None
    norm_model = _normalize_rl_model(model) if model else None
    for a in accts:
        state = _account_state_for_model(a, model, now=now)
        if state == "healthy":
            healthy += 1
            continue
        if state == "probing":
            probing += 1
            continue
        # unavailable — distinguish active rate-limit cooldown from other causes
        dm = a.get("disabled_models") or {}
        info = dm.get(norm_model) if norm_model else None
        # If model is None and account has any disabled model, treat as rate-limited
        if info is None and norm_model is None and dm:
            info = next(iter(dm.values()))
        if info and info.get("at") is not None:
            cooldown_seconds = _cooldown_seconds_for_info(info)
            recovery = info["at"] + timedelta(seconds=cooldown_seconds)
            if recovery > now:
                rate_limited += 1
                if earliest_recovery is None or recovery < earliest_recovery:
                    earliest_recovery = recovery
                continue
        other_unavailable += 1
    return {
        "total": len(accts),
        "healthy": healthy,
        "probing": probing,
        "rate_limited": rate_limited,
        "other_unavailable": other_unavailable,
        "earliest_recovery": earliest_recovery,
    }


def get_all_account_health_summaries(
    user_id: str | None = None,
    now: datetime | None = None,
    project_id: str | None = None,
    persisted_usage_by_id: Mapping[str, Any] | None = None,
) -> list[dict]:
    """Return per-account health summaries for the admin dashboard.

    Reads exclusively from the in-memory pool so the operator sees the same
    state that routing decisions are based on.

    Each entry includes: id, name, provider, auth_mode, state
    (healthy/probing/disqualified/unavailable/fully_disabled), health_score,
    completions_24h, rl_events_24h, avg_duration_seconds, active_tasks,
    daily_task_count, daily_task_cap, max_concurrent_tasks,
    disqualify_threshold, disabled_reason, and rate_limited_models (with
    model name, kind, hits, cooldown_remaining_seconds per entry).

    When *project_id* is provided, each entry also includes
    project_active_tasks, project_daily_task_count, project_daily_task_cap,
    and project_max_concurrent_tasks scoped to that project.
    """
    now = now or _utcnow()
    accts = _max_plan_pools.get(user_id, []) if user_id else _all_accounts()
    results = []
    for acct in accts:
        health_stats = get_account_health_stats(acct, now=now)

        if not acct.get("enabled", True):
            state = "fully_disabled"
        else:
            # Use Opus as the reference model (primary dispatch model).
            circuit_state = _account_state_for_model(acct, _OPUS_MODEL, now=now)
            if (
                circuit_state == "healthy"
                and health_stats["health_score"] < _HEALTH_SCORE_DISQUALIFY_THRESHOLD
            ):
                state = "disqualified"
            else:
                state = circuit_state

        rate_limited_models = []
        for model_name, info in (acct.get("disabled_models") or {}).items():
            at_dt = info.get("at")
            if at_dt is None:
                continue
            total_cooldown = _cooldown_seconds_for_info(info)
            remaining = max(0.0, total_cooldown - (now - at_dt).total_seconds())
            entry: dict = {
                "model": model_name,
                "kind": info.get("kind", "rate_limit"),
                "hits": info.get("hits", 1),
                "cooldown_remaining_seconds": int(remaining),
            }
            if info.get("kind") == "regression" and isinstance(
                info.get("regression_stats"), dict
            ):
                entry["regression_stats"] = dict(info["regression_stats"])
            rate_limited_models.append(entry)

        entry_dict = {
            "id": acct["id"],
            "name": acct.get("name", ""),
            "provider": acct.get("provider", "anthropic"),
            "auth_mode": acct.get("auth_mode", "oauth"),
            "state": state,
            "disabled_reason": acct.get("disabled_reason"),
            "health_score": health_stats["health_score"],
            "success_rate_7d": health_stats["success_rate_7d"],
            "completions_24h": health_stats["completions_24h"],
            "rl_events_24h": health_stats["rl_events_24h"],
            "avg_duration_seconds": health_stats["avg_duration_seconds"],
            "active_tasks": acct.get("active_tasks", 0),
            "daily_task_count": acct.get("daily_task_count", 0),
            "daily_task_cap": acct.get("daily_task_cap"),
            "max_concurrent_tasks": acct.get("max_concurrent_tasks"),
            "disqualify_threshold": _HEALTH_SCORE_DISQUALIFY_THRESHOLD,
            "rate_limited_models": rate_limited_models,
            # Additive: latest cached proactive usage-headroom snapshot
            # (5h/7d utilization, reset times, probed_at, known/stale).
            # "known": False covers never-probed and probe-unknown states.
            # Falls back to the DB-persisted blob so the meter is populated on
            # non-leader instances that never re-ran the (leader-only) probe.
            "usage_snapshot": usage_snapshot_summary_with_fallback(
                acct,
                (persisted_usage_by_id or {}).get(str(acct["id"])),
                now,
            ),
        }
        # Per-project fields when scoped to a specific project.
        if project_id:
            proj_state = (acct.get("project_state") or {}).get(project_id, {}) or {}
            proj_daily_cap = acct.get("project_daily_task_cap") or {}
            proj_concurrent_cap = acct.get("project_max_concurrent_tasks") or {}
            entry_dict["proj_state"] = proj_state
            entry_dict["project_active_tasks"] = int(proj_state.get("active_tasks", 0))
            entry_dict["project_daily_task_count"] = int(proj_state.get("daily_task_count", 0))
            entry_dict["project_daily_task_cap"] = proj_daily_cap.get(project_id)
            entry_dict["project_max_concurrent_tasks"] = proj_concurrent_cap.get(project_id)
        results.append(entry_dict)
    return results


def get_project_account_usage(
    project_id: str, user_id: str
) -> list[dict[str, Any]]:
    """Return per-account consumption for a single project, read from the in-memory pool.

    For each account in *user_id*'s pool, returns the project's active task count,
    daily task count, configured per-project caps, and whether the project is at
    any cap on this account. Includes global (non-project-scoped) account usage
    for cross-project contention visibility.

    Returns an empty list when the user has no pool entries or the project has
    no recorded activity on any account.
    """
    accts = _max_plan_pools.get(user_id, [])
    results: list[dict[str, Any]] = []
    for acct in accts:
        pstate = (acct.get("project_state") or {}).get(project_id, {})
        pdaily_cap = (acct.get("project_daily_task_cap") or {}).get(project_id)
        pconcurrent_cap = (acct.get("project_max_concurrent_tasks") or {}).get(
            project_id
        )
        project_active = int(pstate.get("active_tasks") or 0)
        project_daily = int(pstate.get("daily_task_count") or 0)

        at_project_daily_cap = (
            pdaily_cap is not None and project_daily >= int(pdaily_cap)
        )
        at_project_concurrent_cap = (
            pconcurrent_cap is not None and project_active >= int(pconcurrent_cap)
        )

        results.append(
            {
                "account_id": acct["id"],
                "account_name": acct.get("name", ""),
                "provider": acct.get("provider", "anthropic"),
                "enabled": acct.get("enabled", True),
                "project_active_tasks": project_active,
                "project_daily_task_count": project_daily,
                "project_daily_task_cap": pdaily_cap,
                "project_max_concurrent_tasks": pconcurrent_cap,
                "at_project_daily_cap": at_project_daily_cap,
                "at_project_concurrent_cap": at_project_concurrent_cap,
                "at_project_cap": at_project_daily_cap or at_project_concurrent_cap,
                "global_active_tasks": acct.get("active_tasks", 0),
                "global_daily_task_count": acct.get("daily_task_count", 0),
                "global_daily_task_cap": acct.get("daily_task_cap"),
                "global_max_concurrent_tasks": acct.get("max_concurrent_tasks"),
            }
        )
    return results


async def get_user_project_pool_summary(
    user_id: str, session: Any
) -> list[dict[str, Any]]:
    """Return a per-project pool-consumption summary for all of a user's projects.

    Reads from the in-memory pool for counters and caps; does a single batched
    Project lookup for display names. Returns one entry per project the user
    belongs to.

    Each entry: project_id, project_name, total_active_tasks, total_daily_task_count,
    accounts_at_cap (count of accounts where this project is at a per-project cap),
    accounts_available (accounts with no per-project cap or below cap).
    """
    from sqlmodel import select as sql_select
    from app.models import Project, ProjectMember

    # Collect all project_ids referenced across the user's pool accounts
    all_project_ids: set[str] = set()
    accts = _max_plan_pools.get(user_id, [])
    for acct in accts:
        pstate = acct.get("project_state") or {}
        pdaily_cap = acct.get("project_daily_task_cap") or {}
        pconcurrent_cap = acct.get("project_max_concurrent_tasks") or {}
        all_project_ids.update(pstate.keys())
        all_project_ids.update(pdaily_cap.keys())
        all_project_ids.update(pconcurrent_cap.keys())

    # Also include all projects the user is a member of (even those with no
    # pool activity yet — they show as zero-state entries).
    member_project_ids: set[str] = set()
    try:
        member_rows = (
            await session.execute(
                sql_select(ProjectMember.project_id).where(
                    ProjectMember.user_id == user_id
                )
            )
        ).all()
        member_project_ids = {str(pid) for (pid,) in member_rows}
        all_project_ids.update(member_project_ids)
    except Exception:
        logger.debug(
            "[POOL] get_user_project_pool_summary: failed to query ProjectMember rows"
        )

    if not all_project_ids:
        return []

    # Single batched project-name lookup
    project_names: dict[str, str] = {}
    try:
        project_rows = (
            await session.execute(
                sql_select(Project.id, Project.name).where(
                    Project.id.in_(
                        [pid for pid in all_project_ids if _is_valid_uuid(pid)]
                    )
                )
            )
        ).all()
        project_names = {str(pid): name for pid, name in project_rows}
    except Exception:
        logger.debug(
            "[POOL] get_user_project_pool_summary: failed to query Project names"
        )

    results: list[dict[str, Any]] = []
    for pid in sorted(all_project_ids):
        total_active = 0
        total_daily = 0
        accounts_at_cap = 0
        accounts_available = 0
        caps_configured = False

        for acct in accts:
            pstate = (acct.get("project_state") or {}).get(pid, {})
            pdaily_cap = (acct.get("project_daily_task_cap") or {}).get(pid)
            pconcurrent_cap = (acct.get("project_max_concurrent_tasks") or {}).get(pid)

            pa = int(pstate.get("active_tasks") or 0)
            pd = int(pstate.get("daily_task_count") or 0)
            total_active += pa
            total_daily += pd

            if pdaily_cap is not None or pconcurrent_cap is not None:
                caps_configured = True

            at_cap = False
            if pdaily_cap is not None and pd >= int(pdaily_cap):
                at_cap = True
            if pconcurrent_cap is not None and pa >= int(pconcurrent_cap):
                at_cap = True

            if at_cap:
                accounts_at_cap += 1
            else:
                accounts_available += 1

        results.append(
            {
                "project_id": pid,
                "project_name": project_names.get(pid, pid[:8]),
                "total_active_tasks": total_active,
                "total_daily_task_count": total_daily,
                "accounts_at_cap": accounts_at_cap,
                "accounts_available": accounts_available,
                "total_accounts": len(accts),
                "caps_configured": caps_configured,
            }
        )

    return results


def _is_valid_uuid(value: str) -> bool:
    """Return True if *value* is a valid UUID string."""
    try:
        uuid.UUID(value)
        return True
    except (ValueError, AttributeError):
        return False


def account_in_rate_limit_cooldown(account_id: str | None) -> bool:
    """Return True if the given account has any model currently in rate-limit cooldown.

    Reads the in-memory pool state (_account_by_id). Returns False when the
    account is unresolvable (pool empty, standalone test path, no account
    assigned). Safe to call from synchronous contexts — individual dict reads
    are atomic in CPython and do not require the async lock.
    """
    if not account_id:
        return False
    acct = _account_by_id.get(str(account_id))
    if not acct:
        return False
    dm = acct.get("disabled_models") or {}
    if not dm:
        return False
    now = _utcnow()
    for info in dm.values():
        at_dt = info.get("at")
        if at_dt is None:
            continue
        if (now - at_dt).total_seconds() < _cooldown_seconds_for_info(info):
            return True
    return False


def _account_model_recovery_at(
    acct: dict,
    model: str | None,
    *,
    now: datetime | None = None,
) -> datetime | None:
    """Return the earliest cooldown recovery for ``acct``/``model``, or None.

    Used by the park-and-retarget gate to answer "no alternate account is
    usable NOW, but will one recover within the bounded warm-park window?".
    Only active disabled-model cooldowns count; permanently disabled /
    auth-broken / model-missing / cap-exhausted accounts yield None because this
    helper cannot predict a bounded recovery for them.
    """
    if not _account_runtime_ready(acct):
        return None
    if not acct.get("enabled", True):
        return None
    dm = acct.get("disabled_models") or {}
    if not dm:
        return None

    now = now or _utcnow()
    infos: list[dict] = []
    if model is None:
        infos = [info for info in dm.values() if isinstance(info, dict)]
    else:
        normalized = _normalize_rl_model(model)
        info = dm.get(normalized) if normalized else None
        if info is None and normalized != model:
            info = dm.get(model)
        if isinstance(info, dict):
            infos = [info]

    earliest: datetime | None = None
    for info in infos:
        at_dt = info.get("at")
        if at_dt is None:
            continue
        recovery_at = at_dt + timedelta(seconds=_cooldown_seconds_for_info(info))
        if recovery_at <= now:
            continue
        if earliest is None or recovery_at < earliest:
            earliest = recovery_at
    return earliest


async def alternate_account_recovery_deadline(
    failed_account_id: str | None,
    model: str | None,
    *,
    blacklist: list[str] | None = None,
    park_budget_seconds: int = 600,
    now: datetime | None = None,
) -> datetime | None:
    """Return when an alternate same-pool account becomes usable, or None.

    The failed account itself is always excluded, alongside ``blacklist``.
    Returns ``now`` when an alternate account is already healthy/probing, or the
    earliest cooldown recovery time within ``park_budget_seconds`` when no
    alternate is usable yet. Returns None when the pool is unknown/empty or no
    alternate can recover inside the bounded park window.
    """
    failed_id = str(failed_account_id).strip() if failed_account_id else ""
    if not failed_id:
        return None
    now = now or _utcnow()
    park_budget_seconds = max(0, int(park_budget_seconds))
    excluded = {failed_id}
    for raw in blacklist or []:
        if raw is None:
            continue
        sid = str(raw).strip()
        if sid:
            excluded.add(sid)

    async with _max_plan_pool_lock:
        failed_acct = _account_by_id.get(failed_id)
        if failed_acct is None:
            return None
        user_id = str(failed_acct.get("user_id") or "").strip()
        if not user_id:
            return None
        user_pool = _max_plan_pools.get(user_id, [])
        if not user_pool:
            return None

        budget_deadline = now + timedelta(seconds=park_budget_seconds)
        earliest_recovery: datetime | None = None
        for acct in user_pool:
            acct_id = str(acct.get("id") or "").strip()
            if not acct_id or acct_id in excluded:
                continue
            if _account_available_for_model(acct, model):
                return now
            recovery_at = _account_model_recovery_at(acct, model, now=now)
            if recovery_at is None or recovery_at > budget_deadline:
                continue
            if earliest_recovery is None or recovery_at < earliest_recovery:
                earliest_recovery = recovery_at
        return earliest_recovery


def pool_wide_rate_limit_cooldown(
    at_time: datetime | None = None,
    *,
    user_id: str | None = None,
    providers: list[str] | None = None,
    model: str | None = None,
    project_id: str | None = None,
) -> bool:
    """Return True if every eligible account is in rate-limit cooldown.

    Scoped to the candidate set the task could actually dispatch to:
    - user_id: only that user's pool (omit → global pool, backward-compatible)
    - providers: only accounts on those providers
    - model: use model-specific state check; omitting falls back to the
      conservative model=None path (any disabled entry → unavailable)

    When *project_id* is provided, accounts at their per-project cap for
    that project are treated as unavailable (skipped in the check).

    Returns False when the eligible pool is empty or any account is healthy/probing.
    Safe to call from synchronous contexts — individual dict reads are atomic in CPython.
    """
    accts: list[dict] = _max_plan_pools.get(user_id, []) if user_id else _all_accounts()
    # Filter out accounts at per-project cap for this project (if provided).
    if project_id:
        accts = [
            a for a in accts if not _project_at_cap(a, project_id)
        ]
    if providers:
        norm_providers = {
            _normalize_provider_name(p) or LLMProviderName.ANTHROPIC.value
            for p in providers
        }
        accts = [
            a
            for a in accts
            if (
                _normalize_provider_name(a.get("provider"))
                or LLMProviderName.ANTHROPIC.value
            )
            in norm_providers
        ]
    if not accts:
        return False
    now = at_time if at_time is not None else _utcnow()
    for acct in accts:
        if _account_state_for_model(acct, model, now=now) in ("healthy", "probing"):
            return False
    # All accounts unavailable — confirm at least one has an active rate-limit entry.
    norm_model = _normalize_rl_model(model) if model else None
    for acct in accts:
        dm = acct.get("disabled_models") or {}
        infos = [dm[norm_model]] if (norm_model and norm_model in dm) else dm.values()
        for info in infos:
            at_dt = info.get("at")
            if at_dt is not None and (
                now - at_dt
            ).total_seconds() < _cooldown_seconds_for_info(info):
                return True
    return False


async def db_account_was_rate_limited(
    account_id: str, at_time: datetime | None = None
) -> bool:
    """Return True if the account's persisted rate-limit state shows an active cooldown.

    Reads MaxPlanAccount.rate_limit_state directly from the DB, bypassing the
    in-memory pool. Use as a cross-instance fallback: when the orchestrator
    instance classifying a failure is not the one that recorded the rate-limit,
    the in-memory pool is empty for that account but the DB record is authoritative.

    Never raises — any exception returns False so the classification path degrades
    gracefully to the in-memory/text-only path.
    """
    try:
        from sqlmodel import select as sql_select
        from app.models import MaxPlanAccount

        async with AsyncSessionLocal() as session:
            row = await session.execute(
                sql_select(MaxPlanAccount).where(MaxPlanAccount.id == account_id)
            )
            acct = row.scalar_one_or_none()
            if acct is None:
                return False
            return acct.is_in_rate_limit_cooldown(at_time=at_time)
    except Exception:
        logger.debug(
            "[MAX_PLAN_POOL] db_account_was_rate_limited degraded to False for account %s",
            account_id,
        )
        return False


def _db_account_runtime_ready(acct: Any) -> bool:
    """DB-row equivalent of _account_runtime_ready (which operates on in-memory dicts).

    Mirrors the in-memory readiness check so db_pool_wide_rate_limit_cooldown can
    determine whether an enabled account is *potentially* usable (i.e., has runtime
    credentials) from a DB row alone, without consulting the in-memory pool.

    For legacy Anthropic OAuth this is `oauth_token`. For multi-provider
    account secrets stored through the provider abstraction, this is
    `secret_blob_ref`; DB-side cooldown classification only needs to know that
    credentials exist, not materialize the secret itself.
    """
    try:
        provider = getattr(acct.provider, "value", str(acct.provider or "")).lower()
        auth_mode = getattr(acct.auth_mode, "value", str(acct.auth_mode or "")).lower()
        if (
            provider == LLMProviderName.ANTHROPIC.value
            and auth_mode == LLMAuthMode.OAUTH.value
        ):
            return bool(acct.oauth_token)
        if (
            provider in (LLMProviderName.ZAI.value, LLMProviderName.DEEPSEEK.value)
            and auth_mode == LLMAuthMode.API_KEY.value
        ):
            return bool(acct.secret_blob_ref)
        return False
    except Exception:
        return False


async def db_pool_wide_rate_limit_cooldown(
    at_time: datetime | None = None,
    *,
    user_id: str | None = None,
    providers: list[str] | None = None,
    model: str | None = None,
) -> bool:
    """Return True if the eligible pool is rate-limit blocked per DB state.

    DB-backed cross-instance equivalent of pool_wide_rate_limit_cooldown. Use as a
    fallback when the in-memory pool may not reflect the current state — e.g., when
    a different Cloud Run instance recorded the cooldown, or when no account was
    assigned to the failed task (accountless death) so per-account checks are skipped.

    Scoped to the candidate set the task could actually dispatch to:
    - user_id: filter to that user's accounts
    - providers: filter to accounts on those providers
    - model: when set, "potentially usable" means credentials present AND the
      specific model has no active cooldown (vs. the conservative no-disabled-models
      check used when model is omitted)

    The pool is classified as rate-limit blocked iff no eligible account is potentially
    usable AND at least one eligible account is in active rate-limit cooldown. This
    correctly handles the mixed-state pool (e.g., one account rate-limited, another
    missing credentials): if the only thing blocking the pool IS the cooldown, classify
    the failure as RATE_LIMIT rather than burning retry budget as AGENT_TIMEOUT.

    Returns False on any DB/parse error or when the eligible pool has no accounts.
    Never raises.
    """
    try:
        from sqlmodel import select as sql_select
        from app.models import MaxPlanAccount

        async with AsyncSessionLocal() as session:
            stmt = sql_select(MaxPlanAccount).where(MaxPlanAccount.enabled == True)  # noqa: E712
            if user_id is not None:
                stmt = stmt.where(MaxPlanAccount.user_id == user_id)
            result = await session.execute(stmt)
            accounts = result.scalars().all()

        if not accounts:
            return False

        # Optional provider filter (post-query — provider is an enum, easier to
        # normalise in Python than in SQL for the multi-provider case).
        if providers:
            norm_providers = {
                _normalize_provider_name(p) or LLMProviderName.ANTHROPIC.value
                for p in providers
            }
            accounts = [
                a
                for a in accounts
                if (
                    _normalize_provider_name(a.provider)
                    or LLMProviderName.ANTHROPIC.value
                )
                in norm_providers
            ]
        if not accounts:
            return False

        norm_model = _normalize_rl_model(model) if model else None
        any_in_cooldown = False
        for acct in accounts:
            disabled_models = (acct.rate_limit_state or {}).get("disabled_models") or {}
            if _db_account_runtime_ready(acct):
                if norm_model:
                    # Model-scoped: potentially usable if the specific model has no
                    # active cooldown (more accurate than "no disabled_models at all").
                    if (
                        norm_model not in disabled_models
                        or not acct.is_in_rate_limit_cooldown(at_time=at_time)
                    ):
                        return False
                else:
                    # Conservative model=None path: must have credentials AND no
                    # rate-limit entries at all (expired or not).
                    if not disabled_models:
                        return False
            if acct.is_in_rate_limit_cooldown(at_time=at_time):
                any_in_cooldown = True
        return any_in_cooldown
    except Exception:
        logger.debug(
            "[MAX_PLAN_POOL] db_pool_wide_rate_limit_cooldown degraded to False"
        )
        return False


def describe_pool_blockage(model: str | None, user_id: str | None = None) -> str:
    """Human-readable description of why no account can pick up a task.
    Used by reaper queued-age alerts to distinguish rate-limit cooldown from
    slot contention or missing configuration."""
    s = summarize_pool_state(model, user_id=user_id)
    if s["total"] == 0:
        return "no accounts configured"
    if s["healthy"] == 0 and s["probing"] == 0 and s["rate_limited"] > 0:
        rec = s["earliest_recovery"]
        return f"all accounts in rate-limit cooldown (earliest recovery: {rec.isoformat() if rec else 'unknown'})"
    if s["rate_limited"] > 0:
        return f"{s['rate_limited']} of {s['total']} accounts rate-limited, remaining accounts at capacity"
    return "no eligible accounts or slot contention"


def _extract_task_model(task: Any) -> str | None:
    """Extract which model a task uses from its payload."""
    ctx = _task_context(task)
    if ctx.get("llm_model"):
        return ctx.get("llm_model")
    payloads = task.payloads or []
    if payloads:
        return payloads[0].get("model")
    return None


def _record_model_fallback(
    task: Any,
    *,
    requested: str,
    effective: str,
    provider_name: str,
    reason: str,
) -> None:
    """Record a preview→GA (or equivalent) model fallback on task.context.

    Written at dispatch when the pool had no account with access to the
    requested model and the provider exposes a fallback. Keeps the record
    visible in the admin UI and in the task's context audit trail so a
    silently-downgraded task doesn't confuse an operator expecting preview
    output quality.

    Shape:
        context.model_fallback = {
            "requested": "gemini-3.1-pro-preview",
            "effective": "gemini-2.5-pro",
            "provider": "gemini",
            "reason": "no_account_with_preview_access",
            "at": "2026-04-22T14:53:00+00:00",
        }
    """
    try:
        ctx = dict(getattr(task, "context", {}) or {})
        ctx["model_fallback"] = {
            "requested": requested,
            "effective": effective,
            "provider": provider_name,
            "reason": reason,
            "at": _utcnow().isoformat(),
        }
        task.context = ctx
    except Exception:
        logger.exception(
            "[LLM_SELECT] Failed to record model_fallback on task %s",
            getattr(task, "id", "?"),
        )


def _record_routing_fallback(
    task: Any,
    *,
    intended_provider: str | None,
    intended_model: str | None,
    effective_provider: str,
    effective_model: str | None,
    reason: str,
) -> None:
    """Record a provider/tier routing divergence on task.context.

    Companion to :func:`_record_model_fallback` (same-provider preview→GA
    downgrade). Covers the cross-provider fallback (the intended provider had no
    usable account, so selection landed on a later-preference provider) and the
    synthetic no-account path (no account could be reserved at all). Keeps the
    divergence visible so downstream cost-by-model attribution can caveat that
    the spend reflects a fallback rather than the intended routing.

    Best-effort — never raises and never changes which account is chosen.

    Shape:
        context.routing_fallback = {
            "intended_provider": "anthropic", "intended_model": "claude-opus-4-8",
            "effective_provider": "openai", "effective_model": "gpt-...",
            "reason": "cross_provider" | "no_account",
            "at": "2026-06-05T...",
        }
    """
    try:
        ctx = dict(getattr(task, "context", {}) or {})
        ctx["routing_fallback"] = {
            "intended_provider": intended_provider,
            "intended_model": intended_model,
            "effective_provider": effective_provider,
            "effective_model": effective_model,
            "reason": reason,
            "at": _utcnow().isoformat(),
        }
        task.context = ctx
    except Exception:
        logger.exception(
            "[LLM_SELECT] Failed to record routing_fallback on task %s",
            getattr(task, "id", "?"),
        )


def resolve_task_quality_tier(task: Any, runtime_config: Mapping[str, Any]) -> str:
    ctx = _task_context(task)
    payloads = getattr(task, "payloads", None) or []
    payload = payloads[0] if payloads else {}
    explicit_tier = payload.get("quality_tier") or ctx.get("quality_tier")
    if isinstance(explicit_tier, str) and explicit_tier.strip():
        return explicit_tier.strip()
    explicit_model = payload.get("model") or ctx.get("llm_model")
    inferred_provider = get_task_llm_provider(task) or infer_provider_for_model(
        explicit_model, runtime_config
    )
    inferred_tier = infer_quality_tier_for_model(
        inferred_provider, explicit_model, runtime_config
    )
    return inferred_tier or DEFAULT_QUALITY_TIER


def resolve_deepseek_preference(
    task_type: str, runtime_config: Mapping[str, Any]
) -> bool:
    """Return True if DeepSeek is the first preferred provider for the task type.

    Low/medium-effort categories (build, initiative, ops, _default) list DeepSeek
    first in their routing. Strategic/review/high-effort categories list other
    providers first. This provides a programmatic check for monitoring and admin
    surfaces to verify the DeepSeek routing preference is intact.
    """
    routing = _resolve_routing_core({}, {}, task_type, runtime_config)
    providers = routing.get("providers", [])
    return bool(providers and providers[0] == "deepseek")


def _build_rate_limit_state_blob(acct: Dict[str, Any]) -> dict | None:
    """Serialize an account's rate-limit state (disabled_models + outcome_events) for DB."""
    dm = acct.get("disabled_models") or {}
    cutoff = _utcnow() - timedelta(seconds=_OUTCOME_WINDOW_SECONDS)
    pruned_outcomes = []
    for e in acct.get("outcome_events", []):
        if e["at"] <= cutoff:
            continue
        entry: dict[str, Any] = {"at": e["at"].isoformat(), "success": e["success"]}
        if e.get("task_type"):
            entry["task_type"] = e["task_type"]
        pruned_outcomes.append(entry)
    if len(pruned_outcomes) > _OUTCOME_EVENTS_CAP:
        pruned_outcomes = pruned_outcomes[-_OUTCOME_EVENTS_CAP:]
    out: dict[str, Any] = {}
    if dm:
        out["disabled_models"] = {
            m: _serialize_disabled_model_info(i) for m, i in dm.items()
        }
    if pruned_outcomes:
        out["outcome_events"] = pruned_outcomes
    sf_cooldown_at = acct.get("startup_failure_cooldown_at")
    if sf_cooldown_at is not None:
        out["startup_failure_cooldown_at"] = sf_cooldown_at.isoformat()
        sf_reason = acct.get("disabled_reason") or ""
        if sf_reason.startswith("startup-failure cooldown"):
            out["startup_failure_disabled_reason"] = sf_reason
    return out if out else None


def _stage_rate_limit_mutation(acct: Dict[str, Any], mutation: dict) -> None:
    """Stage a serializable DB-authoritative cooldown mutation on an account."""
    acct.setdefault("_pending_rate_limit_mutations", []).append(mutation)


def _apply_rate_limit_mutations(blob: dict, mutations: list[dict]) -> dict | None:
    """Apply cooldown deltas to a DB blob already locked FOR UPDATE."""
    state = dict(blob or {})
    disabled_models = {
        model: dict(info)
        for model, info in (state.get("disabled_models") or {}).items()
        if isinstance(info, dict)
    }
    outcomes = [
        dict(event)
        for event in (state.get("outcome_events") or [])
        if isinstance(event, dict)
    ]
    reset_at_raw = state.get("cooldown_reset_at")
    try:
        reset_at = datetime.fromisoformat(reset_at_raw) if reset_at_raw else None
    except (TypeError, ValueError):
        reset_at = None

    def _predates_operator_reset(raw_at: Any) -> bool:
        if reset_at is None or not raw_at:
            return False
        try:
            return datetime.fromisoformat(str(raw_at)) <= reset_at
        except (TypeError, ValueError):
            # A malformed/unstamped cooldown mutation cannot safely supersede an
            # explicit operator reset generation.
            return True

    for mutation in mutations:
        op = mutation.get("op")
        if op == "disable_models":
            for model, incoming in (mutation.get("models") or {}).items():
                if _predates_operator_reset(incoming.get("at")):
                    continue
                existing = dict(disabled_models.get(model) or {})
                existing["hits"] = int(existing.get("hits", 0)) + 1
                for key in (
                    "at",
                    "kind",
                    "retry_after_seconds",
                    "provider",
                    "regression_stats",
                ):
                    if key in incoming:
                        existing[key] = incoming[key]
                disabled_models[model] = existing
            outcome = mutation.get("outcome_event")
            if isinstance(outcome, dict) and not _predates_operator_reset(
                outcome.get("at")
            ):
                outcomes.append(dict(outcome))
        elif op == "clear_models":
            required_kind = mutation.get("required_kind")
            model_conditions = mutation.get("models") or {}
            if isinstance(model_conditions, list):
                model_conditions = {model: {} for model in model_conditions}
            for model, condition in model_conditions.items():
                existing = disabled_models.get(model)
                if existing is None:
                    continue
                if required_kind and existing.get("kind") != required_kind:
                    continue
                observed_at = condition.get("observed_at")
                if observed_at and existing.get("at") != observed_at:
                    continue
                observed_provider = condition.get("provider")
                if observed_provider and existing.get("provider") != observed_provider:
                    continue
                clear_at = condition.get("clear_at")
                if clear_at:
                    try:
                        existing_at_dt = datetime.fromisoformat(existing.get("at", ""))
                        clear_at_dt = datetime.fromisoformat(clear_at)
                    except (TypeError, ValueError):
                        continue
                    if existing_at_dt > clear_at_dt:
                        continue
                disabled_models.pop(model, None)
        elif op == "set_startup_cooldown":
            if _predates_operator_reset(mutation.get("at")):
                continue
            state["startup_failure_cooldown_at"] = mutation["at"]
            reason = mutation.get("reason")
            if reason:
                state["startup_failure_disabled_reason"] = reason
        elif op == "clear_startup_cooldown":
            existing_at = state.get("startup_failure_cooldown_at")
            observed_at = mutation.get("observed_at")
            if observed_at and existing_at != observed_at:
                continue
            clear_at = mutation.get("clear_at")
            if clear_at:
                try:
                    existing_at_dt = datetime.fromisoformat(existing_at or "")
                    clear_at_dt = datetime.fromisoformat(clear_at)
                except (TypeError, ValueError):
                    continue
                if existing_at_dt > clear_at_dt:
                    continue
            state.pop("startup_failure_cooldown_at", None)
            state.pop("startup_failure_disabled_reason", None)

    if disabled_models:
        state["disabled_models"] = disabled_models
    else:
        state.pop("disabled_models", None)
    cutoff = _utcnow() - timedelta(seconds=_OUTCOME_WINDOW_SECONDS)
    fresh_outcomes = []
    for event in outcomes:
        try:
            at_dt = datetime.fromisoformat(event.get("at", ""))
        except (TypeError, ValueError):
            continue
        if at_dt > cutoff:
            fresh_outcomes.append(event)
    if fresh_outcomes:
        state["outcome_events"] = fresh_outcomes[-_OUTCOME_EVENTS_CAP:]
    else:
        state.pop("outcome_events", None)
    return state or None


def _refresh_account_rate_limit_state(acct: Dict[str, Any], blob: dict | None) -> None:
    """Refresh this pod's cooldown cache from the committed authoritative blob."""
    state = blob or {}
    restored_dm: dict[str, dict] = {}
    for model, raw in (state.get("disabled_models") or {}).items():
        if not isinstance(raw, dict) or not raw.get("at"):
            continue
        try:
            info = dict(raw)
            info["at"] = datetime.fromisoformat(raw["at"])
        except (TypeError, ValueError):
            continue
        restored_dm[model] = info
    if restored_dm:
        acct["disabled_models"] = restored_dm
    else:
        acct.pop("disabled_models", None)

    restored_outcomes = []
    for raw in state.get("outcome_events") or []:
        if not isinstance(raw, dict) or not raw.get("at"):
            continue
        try:
            event = dict(raw)
            event["at"] = datetime.fromisoformat(raw["at"])
        except (TypeError, ValueError):
            continue
        restored_outcomes.append(event)
    if restored_outcomes:
        acct["outcome_events"] = restored_outcomes
    else:
        acct.pop("outcome_events", None)
    startup_at = state.get("startup_failure_cooldown_at")
    if startup_at:
        try:
            acct["startup_failure_cooldown_at"] = datetime.fromisoformat(startup_at)
        except (TypeError, ValueError):
            acct.pop("startup_failure_cooldown_at", None)
    else:
        acct.pop("startup_failure_cooldown_at", None)


async def _persist_rate_limit_state(acct_id: str) -> None:
    """Persist cooldown state without cross-pod lost updates.

    Cooldown callers stage precise mutations. The DB row is locked, those
    mutations are applied to the latest blob, and this pod is refreshed from
    the committed result. A caller without a staged blob mutation preserves the
    authoritative blob and may only update non-blob account columns.
    """
    from app.models import MaxPlanAccount
    from sqlmodel import select as sql_select

    acct = _account_by_id.get(acct_id)
    if acct is None:
        return
    mutations = list(acct.get("_pending_rate_limit_mutations") or [])
    async with AsyncSessionLocal() as rl_session:
        result = await rl_session.execute(
            sql_select(MaxPlanAccount)
            .where(MaxPlanAccount.id == uuid.UUID(acct_id))
            .with_for_update()
        )
        db_acct = result.scalar_one_or_none()
        if db_acct is None:
            return
        if mutations:
            committed_blob = _apply_rate_limit_mutations(
                db_acct.rate_limit_state or {}, mutations
            )
        else:
            committed_blob = db_acct.rate_limit_state
        db_acct.rate_limit_state = committed_blob
        # Field ownership: ordinary cooldown mutations own only the blob. They
        # must never copy this pod's cached enabled/disabled state over an
        # operator or auth change committed by another session.
        for mutation in mutations:
            if mutation.get("op") == "disable_account":
                reset_raw = (committed_blob or {}).get("cooldown_reset_at")
                disabled_at_raw = mutation.get("disabled_at")
                is_auto_cooldown_disable = str(mutation.get("reason") or "").startswith(
                    "auto-disabled after"
                )
                if reset_raw and disabled_at_raw and is_auto_cooldown_disable:
                    try:
                        if datetime.fromisoformat(disabled_at_raw) <= datetime.fromisoformat(
                            reset_raw
                        ):
                            continue
                    except (TypeError, ValueError):
                        continue
                db_acct.enabled = False
                raw_disabled_at = mutation.get("disabled_at")
                db_acct.disabled_at = (
                    datetime.fromisoformat(raw_disabled_at)
                    if raw_disabled_at
                    else None
                )
                db_acct.disabled_reason = mutation.get("reason")
            elif mutation.get("op") == "set_startup_cooldown":
                db_acct.disabled_reason = mutation.get("reason")
        rl_session.add(db_acct)
        await rl_session.commit()
    if mutations:
        del acct["_pending_rate_limit_mutations"][: len(mutations)]
        if not acct["_pending_rate_limit_mutations"]:
            acct.pop("_pending_rate_limit_mutations", None)
    _refresh_account_rate_limit_state(acct, committed_blob)
    # The locked row is also authoritative for operator/auth-owned fields.
    # Refresh this pod's cache after preserving them so it cannot continue
    # dispatching an account that another session disabled.
    acct["enabled"] = db_acct.enabled
    acct["disabled_at"] = db_acct.disabled_at
    acct["disabled_reason"] = db_acct.disabled_reason


def _queue_rate_limit_persist_retry(acct_id: str) -> None:
    if acct_id not in _pending_rl_persists:
        _pending_rl_persists.append(acct_id)


async def _persist_rate_limit_state_best_effort(acct_id: str, reason: str) -> None:
    """Persist cooldown state without breaking the caller's control flow.

    Some failures surface as raw driver or transport exceptions before SQLAlchemy
    wraps them. Rate-limit bookkeeping is already designed to retry DB persists
    later, so treat any persistence exception as non-fatal and queue the account
    for the next retry sweep.
    """
    try:
        await _persist_rate_limit_state(acct_id)
    except Exception as exc:
        logger.warning(
            "[POOL] Failed to persist %s for %s: %s — queued for retry",
            reason,
            acct_id,
            exc,
        )
        _queue_rate_limit_persist_retry(acct_id)


# --- Proactive account-usage headroom (advisory) ---
#
# A provider usage-probe snapshot is cached per in-memory pool entry under
# ``usage_snapshot`` (a UsageSnapshot) + ``usage_snapshot_at`` (datetime). The
# selection functions READ ONLY the cached snapshot — they NEVER probe under the
# pool lock. A missing/stale snapshot is treated as "unknown" for the current
# decision and triggers a non-blocking background refresh.
#
# Everything here is advisory: an unknown snapshot makes selection behave
# exactly as it did before this feature. The reactive rate-limit cooldowns
# (disable_account_model / _account_state_for_model) remain the backstop.
_USAGE_SNAPSHOT_TTL_SECONDS = 900
# Cadence for the display-oriented usage-meter refresh (orchestrator calls
# refresh_usage_meter_snapshots on this interval). Aligned with the selection
# TTL above: probes run every 900 s, so the selection gate considers a snapshot
# fresh for the same window to avoid treating the gap as "unknown" on every call.
_USAGE_METER_REFRESH_INTERVAL_SECONDS = 900
# Spread cadence-triggered probes so a pool refresh does not hit every provider
# account in one provider-side burst and manufacture avoidable 429s. Claude's
# subscription usage endpoint is much more tightly throttled than inference.
_USAGE_METER_PROBE_STAGGER_SECONDS = 30.0
# A snapshot older than this renders as "stale" on the admin meter — three
# missed meter refreshes. Keeping display freshness separate from one missed
# probe prevents a transient provider throttle from immediately greying values.
_USAGE_METER_DISPLAY_STALE_SECONDS = 2700
# Neutral headroom rank for accounts whose snapshot is unknown — the midpoint of
# the [0, 100] headroom scale, mirroring how sparse success-rate falls back to
# 0.5. Keeps unprobed accounts from being either starved or unduly preferred.
_USAGE_HEADROOM_NEUTRAL = 50.0
# Conservative default set of "long-running" task types the claim-time gate
# applies to (kept in sync with runtime_config_state defaults). Used only as a
# fallback when runtime_config omits the key.
_DEFAULT_LONG_RUNNING_TASK_TYPES: tuple[str, ...] = (
    "create",
    "refactor",
    "migrate",
    "research",
    "plan",
    "security_audit",
)

# Strong refs to in-flight probe tasks so fire-and-forget refreshes aren't
# garbage-collected mid-flight. Entries are discarded on completion.
_usage_probe_tasks: set = set()


def _runtime_config_snapshot() -> Mapping[str, Any]:
    """Best-effort read of the process-global runtime_config (advisory only).

    Lazy import avoids a module-load cycle with app.shared_state. Returns an
    empty mapping if config is unavailable so callers degrade to "feature off".
    """
    try:
        from app.shared_state import runtime_config as _rc

        return _rc if isinstance(_rc, Mapping) else {}
    except Exception:  # advisory; absence means feature off
        return {}


def _usage_headroom_settings(
    runtime_config: Mapping[str, Any] | None,
) -> tuple[bool, float, frozenset[str]]:
    """Resolve (enabled, threshold_pct, long_running_task_types) from config.

    Tolerant of malformed values — any bad field degrades to the conservative
    default rather than raising in the hot path.
    """
    cfg = (
        runtime_config
        if isinstance(runtime_config, Mapping)
        else _runtime_config_snapshot()
    )
    enabled = bool(cfg.get("usage_headroom_gate_enabled", False))
    raw_threshold = cfg.get("usage_headroom_threshold_pct", 90)
    try:
        threshold = float(raw_threshold)
    except (TypeError, ValueError):
        threshold = 90.0
    raw_types = cfg.get("usage_headroom_long_running_task_types")
    if isinstance(raw_types, (list, tuple, set)):
        long_types = frozenset(str(t).strip() for t in raw_types if str(t).strip())
    else:
        long_types = frozenset(_DEFAULT_LONG_RUNNING_TASK_TYPES)
    return enabled, threshold, long_types


def _cached_usage_snapshot(acct: Dict[str, Any], now: datetime) -> UsageSnapshot | None:
    """Return the account's cached snapshot iff fresh, else None (→ unknown).

    None is returned for a missing OR a stale (TTL-expired) snapshot. Callers
    treat None exactly as an unknown snapshot.
    """
    snap = acct.get("usage_snapshot")
    at = acct.get("usage_snapshot_at")
    if not isinstance(snap, UsageSnapshot) or at is None:
        return None
    if (now - at).total_seconds() >= _USAGE_SNAPSHOT_TTL_SECONDS:
        return None
    return snap


def _usage_gate_should_skip(
    acct: Dict[str, Any], now: datetime, threshold_pct: float
) -> bool:
    """True iff a fresh, KNOWN snapshot shows 5h OR 7d util >= threshold.

    Unknown / stale / missing snapshots return False (never skipped) so an
    account without a probe is never gated out.
    """
    snap = _cached_usage_snapshot(acct, now)
    if snap is None:
        return False
    return any(util >= threshold_pct for util in snap.load_bearing_utilizations())


def _usage_headroom_rank(acct: Dict[str, Any], now: datetime) -> float:
    """Remaining-headroom rank (higher = more headroom; 0..100).

    Unknown / stale / missing snapshots rank at the neutral midpoint so
    unprobed accounts are never starved (mirrors sparse success-rate → 0.5).
    """
    snap = _cached_usage_snapshot(acct, now)
    if snap is None:
        return _USAGE_HEADROOM_NEUTRAL
    utils = snap.load_bearing_utilizations()
    if not utils:
        return _USAGE_HEADROOM_NEUTRAL
    return 100.0 - max(utils)


async def _refresh_usage_snapshot(acct_id: str) -> None:
    """Probe one account's usage OUTSIDE the pool lock and cache the result.

    Advisory + total: never raises. Writes the cache under the pool lock; the
    network probe itself runs lock-free.

    Failure semantics: a failed probe never clobbers a previously KNOWN
    snapshot — the meter keeps showing last-known values (which age into
    ``stale``) and only ``usage_probe_failed_at`` is stamped so schedulers
    back off instead of re-probing on every selection touch. When there is no
    known prior, the unknown result is cached with a fresh timestamp (same
    back-off effect through the TTL).

    On a SUCCESSFUL probe the snapshot is also persisted to the DB row's
    ``rate_limit_usage`` column so the data survives orchestrator restarts.
    """
    acct = _account_by_id.get(acct_id)
    if acct is None:
        return
    snapshot: UsageSnapshot
    secret: str | None = None
    success = False
    try:
        provider_name = (
            _normalize_provider_name(acct.get("provider"))
            or LLMProviderName.ANTHROPIC.value
        )
        auth_mode_raw = (
            _normalize_auth_mode(acct.get("auth_mode")) or LLMAuthMode.OAUTH.value
        )
        secret, secret_failure_kind = await _rehydrate_usage_probe_secret(acct)
        provider = get_provider(provider_name)
        if secret_failure_kind is not None:
            snapshot = UsageSnapshot.unknown(
                _utcnow().isoformat(), failure_kind=secret_failure_kind
            )
        else:
            snapshot = await provider.probe_usage(
                secret,
                LLMAuthMode(auth_mode_raw),
                acct.get("provider_metadata") or {},
            )
        success = snapshot.known
    except Exception as exc:  # advisory; cache unknown on failure
        logger.debug("[POOL] usage probe errored for %s: %s", acct_id, exc)
        snapshot = UsageSnapshot.unknown(
            _utcnow().isoformat(), failure_kind="provider_error"
        )
    async with _max_plan_pool_lock:
        live = _account_by_id.get(acct_id)
        if live is not None:
            prev = live.get("usage_snapshot")
            has_known_prior = isinstance(prev, UsageSnapshot) and prev.known
            if success or not has_known_prior:
                live["usage_snapshot"] = snapshot
                live["usage_snapshot_at"] = _utcnow()
            if success:
                live.pop("usage_probe_failed_at", None)
            else:
                live["usage_probe_failed_at"] = _utcnow()
            live["usage_probe_attempt_at"] = _utcnow()
            live["usage_probe_failure_kind"] = (
                None if success else snapshot.probe_failure_kind or "provider_error"
            )
            if secret is not None:
                live["runtime_secret"] = secret
                if auth_mode_raw == LLMAuthMode.OAUTH.value:
                    live["oauth_token"] = secret
                live["runtime_ready"] = True
                live["secret_fetch_failed"] = False
            elif snapshot.probe_failure_kind == "secret_unavailable":
                live["secret_fetch_failed"] = True
            live.pop("usage_probe_inflight", None)
    # Persist every attempt.  A failed probe updates its classified attempt
    # state while retaining an existing last-good snapshot and its freshness.
    # Best-effort: a write failure is non-fatal and never affects admission.
    await _persist_rate_limit_usage_to_db(acct_id, snapshot)


async def _rehydrate_usage_probe_secret(
    acct: Dict[str, Any],
) -> tuple[str | None, str | None]:
    """Return a probe credential immediately before network I/O.

    Pool rebuilds intentionally avoid keeping Secret Manager material fresh
    forever.  The usage meter must not mistake that memory-saving behavior for
    an account capability failure, so it resolves a Secret Manager reference
    here, outside the pool lock.  The return value is ``(secret, failure_kind)``
    and never contains an exception message or secret material in diagnostics.
    """
    reference = acct.get("secret_blob_ref")
    if isinstance(reference, str) and reference.startswith("projects/"):
        try:
            from app.secrets_manager import get_user_secret

            secret = await get_user_secret(reference)
        except Exception:  # advisory: show a stable operator-visible reason
            logger.warning(
                "[POOL] usage meter could not rehydrate Secret Manager credential for %s",
                acct.get("id", "<unknown>"),
            )
            return None, "secret_unavailable"
        if isinstance(secret, str) and secret:
            return secret, None
        return None, "secret_unavailable"
    secret = acct.get("oauth_token") or acct.get("runtime_secret") or reference
    if isinstance(secret, str) and secret:
        return secret, None
    return None, "missing_secret"


async def _persist_rate_limit_usage_to_db(
    acct_id: str, snapshot: UsageSnapshot
) -> None:
    """Write the usage snapshot into MaxPlanAccount.rate_limit_usage.

    Best-effort advisory: failures are logged but never raised.
    On failure, adds the account id to ``_usage_persist_failing_accounts`` and
    retries once so a transient DB hiccup does not leave the meter dark on
    non-leader instances (which rely on the persisted ``rate_limit_usage``
    column).  On success, removes ONLY this account from the failing set so one
    account's recovery never clears another account's failure attribution.
    """
    from app.models import MaxPlanAccount

    for attempt in range(2):
        try:
            async with AsyncSessionLocal() as persist_session:
                row = await persist_session.get(
                    MaxPlanAccount, uuid.UUID(acct_id)
                )
                if row is not None:
                    # Successful snapshots replace the last-good reading.  A
                    # failed attempt only annotates the existing blob, keeping
                    # both its historical reading and its original freshness.
                    raw = getattr(snapshot, "raw_rate_limit_blob", None)
                    if snapshot.known:
                        if (
                            isinstance(raw, dict)
                            and raw.get("source") == "messages_api_header"
                        ):
                            usage_dict = dict(raw)
                        else:
                            usage_dict = snapshot.to_dict()
                        usage_dict["updated_at"] = _utcnow().isoformat()
                    else:
                        existing = row.rate_limit_usage
                        usage_dict = (
                            dict(existing)
                            if isinstance(existing, Mapping)
                            else snapshot.to_dict()
                        )
                    usage_dict["last_attempt_at"] = (
                        snapshot.probed_at or _utcnow().isoformat()
                    )
                    usage_dict["last_attempt_status"] = (
                        "known"
                        if snapshot.known
                        else snapshot.probe_failure_kind or "provider_error"
                    )
                    row.rate_limit_usage = usage_dict
                    row.updated_at = _utcnow()
                    persist_session.add(row)
                    await persist_session.commit()
                _usage_persist_failing_accounts.discard(acct_id)
                return  # success
        except Exception:  # noqa: PERF203 - bounded persist-retry, not a hot loop
            if attempt == 0:
                _usage_persist_failing_accounts.add(acct_id)
                logger.warning(
                    "[POOL] failed to persist rate_limit_usage for account %s; retrying...",
                    acct_id,
                )
            else:
                logger.warning(
                    "[POOL] retry also failed for rate_limit_usage for account %s; keeping in-memory only",
                    acct_id,
                )


async def _delayed_usage_refresh(acct_id: str, delay_seconds: float) -> None:
    if delay_seconds > 0:
        await asyncio.sleep(delay_seconds)
    await _refresh_usage_snapshot(acct_id)


def _schedule_usage_refresh(
    acct: Dict[str, Any], *, delay_seconds: float = 0.0
) -> None:
    """Fire-and-forget a background usage refresh for an account.

    MUST be called under the pool lock (reads/sets the in-flight flag). Network
    I/O happens later in the scheduled task, after the lock is released. A no-op
    when a refresh is already in flight or no event loop is running.
    """
    if acct.get("usage_probe_inflight"):
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    acct["usage_probe_inflight"] = True
    task = loop.create_task(_delayed_usage_refresh(acct["id"], delay_seconds))
    _usage_probe_tasks.add(task)
    task.add_done_callback(_usage_probe_tasks.discard)


def _maybe_schedule_usage_refresh(acct: Dict[str, Any], now: datetime) -> None:
    """Schedule a refresh only when the cached snapshot is missing/stale.

    A recent probe FAILURE also suppresses scheduling for one TTL window —
    failed probes preserve the last-known snapshot (see
    ``_refresh_usage_snapshot``), so without this stamp a flaky probe would be
    retried on every selection touch.
    """
    if _cached_usage_snapshot(acct, now) is not None:
        return
    # A provider can explicitly report that no stable machine-readable meter
    # exists.  That is a durable product limitation, not a transient failure;
    # do not keep spending background work rediscovering it.
    if acct.get("usage_probe_failure_kind") == "unsupported":
        return
    failed_at = acct.get("usage_probe_failed_at")
    if (
        failed_at is not None
        and (now - failed_at).total_seconds() < _USAGE_SNAPSHOT_TTL_SECONDS
    ):
        return
    _schedule_usage_refresh(acct)


def _snapshot_from_persisted_usage(
    raw: Any,
) -> tuple[UsageSnapshot, datetime] | None:
    """Rebuild (UsageSnapshot, its wall-clock stamp) from a persisted
    ``MaxPlanAccount.rate_limit_usage`` blob written by
    ``_persist_rate_limit_usage_to_db``. None on any structural mismatch.

    Accepts two source schemas:
    - ``oauth_usage_probe``: five_hour/seven_day window cells with utilization.
    - ``messages_api_header``: unified_5h cell with utilization (0..100 scale),
      mapped to the five_hour window so per-response header data is not silently
      discarded on non-leader instances that read the DB-persisted blob.
    """
    if not isinstance(raw, Mapping):
        return None
    source = raw.get("source")
    try:
        updated_at = datetime.fromisoformat(raw.get("updated_at"))
    except (TypeError, ValueError):
        return None
    # oauth_usage_probe blobs round-trip through UsageSnapshot.to_dict/from_dict.
    snapshot = UsageSnapshot.from_dict(raw)
    if snapshot is not None and snapshot.known:
        return snapshot, updated_at
    # from_dict only understands the oauth_usage_probe schema. The Messages API
    # persists a ``messages_api_header`` blob (unified_5h utilization, 0..100
    # scale) that non-leader instances must still read — map it to the five_hour
    # window so per-response header data is not silently discarded.
    if source == "messages_api_header":
        entry = raw.get("unified_5h")
        if isinstance(entry, Mapping):
            util = entry.get("utilization")
            if not isinstance(util, bool) and isinstance(util, (int, float)):
                resets: dict[str, str | None] = {}
                reset = entry.get("resets_at")
                if isinstance(reset, str):
                    resets["five_hour"] = reset
                snapshot = UsageSnapshot(
                    known=True,
                    window_utilizations={"five_hour": float(util)},
                    resets_at=resets,
                    probed_at=updated_at.isoformat(),
                )
                return snapshot, updated_at
    return None


def _restore_usage_state(
    entry: Dict[str, Any],
    prev_state: tuple | None,
    persisted_raw: Any,
) -> None:
    """Seed a freshly-built pool entry's usage-meter state.

    Takes the fresher of the previous in-memory snapshot (survives the
    periodic pool rebuild) and the DB-persisted blob (survives restarts).
    """
    candidates: list[tuple[datetime, UsageSnapshot]] = []
    if prev_state is not None:
        prev_snap, prev_at, prev_failed_at, *attempt_state = prev_state
        if (
            isinstance(prev_snap, UsageSnapshot)
            and prev_snap.known
            and prev_at is not None
        ):
            candidates.append((prev_at, prev_snap))
        if prev_failed_at is not None:
            entry["usage_probe_failed_at"] = prev_failed_at
        if len(attempt_state) >= 2:
            attempt_at, failure_kind = attempt_state[:2]
            if attempt_at is not None:
                entry["usage_probe_attempt_at"] = attempt_at
            if isinstance(failure_kind, str) and failure_kind:
                entry["usage_probe_failure_kind"] = failure_kind
    parsed = _snapshot_from_persisted_usage(persisted_raw)
    if parsed is not None:
        candidates.append((parsed[1], parsed[0]))
    if candidates:
        best_at, best_snap = max(candidates, key=lambda c: c[0])
        entry["usage_snapshot"] = best_snap
        entry["usage_snapshot_at"] = best_at


def _account_meter_eligible(acct: Dict[str, Any]) -> bool:
    """True iff this account can produce a usage snapshot worth probing.

    Anthropic accounts and OpenAI Codex OAuth accounts have machine-readable
    usage sources. Eligibility is based on configured credential capability,
    not whether a Secret Manager secret happened to be hydrated during the last
    pool rebuild; rehydration happens immediately before probing.
    """
    provider = _normalize_provider_name(acct.get("provider"))
    auth_mode = _normalize_auth_mode(acct.get("auth_mode"))
    if not bool(acct.get("enabled", True)):
        return False
    has_secret = bool(
        acct.get("secret_blob_ref")
        or acct.get("oauth_token")
        or acct.get("runtime_secret")
    )
    if provider == LLMProviderName.OPENAI.value:
        return auth_mode == LLMAuthMode.OAUTH.value and has_secret
    if provider != LLMProviderName.ANTHROPIC.value:
        return False
    if auth_mode == LLMAuthMode.OAUTH.value:
        return has_secret
    if auth_mode == LLMAuthMode.API_KEY.value:
        return bool(acct.get("secret_blob_ref") or acct.get("runtime_secret"))
    return False


async def refresh_usage_meter_snapshots(now: datetime | None = None) -> int:
    """Schedule usage probes for meter-eligible accounts with aging snapshots.

    Called on an orchestrator cadence (every
    ``_USAGE_METER_REFRESH_INTERVAL_SECONDS``) so the admin usage meter stays
    populated regardless of the ``usage_headroom_gate_enabled`` selection gate
    (default off) — previously the gate was the meter's ONLY data supply, so
    the meter rendered permanently-unknown in any default-configured deploy.
    Advisory and non-blocking: probes are fire-and-forget; returns the number
    scheduled.
    """
    now = now or _utcnow()
    scheduled = 0
    async with _max_plan_pool_lock:
        for acct in _account_by_id.values():
            if not _account_meter_eligible(acct):
                # Warn when an Anthropic OAuth account is dark due to a missing
                # secret — operators cannot distinguish this from a probe failure
                # without an explicit log.
                provider = _normalize_provider_name(acct.get("provider"))
                auth_mode = _normalize_auth_mode(acct.get("auth_mode"))
                if (
                    provider == LLMProviderName.ANTHROPIC.value
                    and auth_mode == LLMAuthMode.OAUTH.value
                    and bool(acct.get("enabled", True))
                    and not bool(acct.get("oauth_token") or acct.get("runtime_secret"))
                ):
                    logger.warning(
                        "[POOL] usage meter dark for account %s: OAuth secret unavailable "
                        "(Secret Manager fetch failed or secret not set); meter will stay "
                        "unknown until the secret is restored",
                        acct.get("id", "<unknown>"),
                    )
                continue
            snap = acct.get("usage_snapshot")
            at = acct.get("usage_snapshot_at")
            if (
                isinstance(snap, UsageSnapshot)
                and snap.known
                and at is not None
                and (now - at).total_seconds()
                < _USAGE_METER_REFRESH_INTERVAL_SECONDS
            ):
                continue
            failed_at = acct.get("usage_probe_failed_at")
            if (
                failed_at is not None
                and (now - failed_at).total_seconds()
                < _USAGE_METER_REFRESH_INTERVAL_SECONDS
            ):
                continue
            if acct.get("usage_probe_failure_kind") == "unsupported":
                continue
            _schedule_usage_refresh(
                acct,
                delay_seconds=scheduled * _USAGE_METER_PROBE_STAGGER_SECONDS,
            )
            scheduled += 1
    if scheduled:
        logger.info("[POOL] usage meter: scheduled %d usage probe(s)", scheduled)
    return scheduled


def get_usage_persist_failure_count() -> int:
    """Return the number of accounts currently failing to persist their usage snapshot."""
    return len(_usage_persist_failing_accounts)


def get_usage_persist_failing_accounts() -> list[dict[str, str]]:
    """Return accounts currently failing to persist usage snapshots.

    Each entry has 'id' and 'name' (name resolved from the in-memory pool where
    available, otherwise the id string).
    """
    result = []
    for acct_id in _usage_persist_failing_accounts:
        acct = _account_by_id.get(acct_id)
        result.append(
            {"id": acct_id, "name": acct["name"] if acct else acct_id}
        )
    return result


async def detect_persistently_dark_meters(
    now: datetime | None = None,
    threshold_seconds: float = _USAGE_METER_DISPLAY_STALE_SECONDS * 2,
) -> List[Dict[str, Any]]:
    """Return meter-eligible accounts whose usage snapshot has been stale/dark
    for longer than ``threshold_seconds``.

    "Dark" means: known=False (never probed, or probe returned unknown).
    "Persistently dark" means the darkness has persisted beyond the threshold,
    suggesting a stuck probe rather than a transient failure.

    Returns a list of dicts with keys: id, name, dark_reason, dark_since.
    """
    now = now or _utcnow()
    stale: List[Dict[str, Any]] = []
    async with _max_plan_pool_lock:
        for acct in _account_by_id.values():
            if not _account_meter_eligible(acct):
                continue
            snap = acct.get("usage_snapshot")
            if isinstance(snap, UsageSnapshot) and snap.known:
                continue
            # Account is meter-eligible but has no known snapshot.
            # Determine how long it has been dark using the oldest relevant stamp.
            failed_at = acct.get("usage_probe_failed_at")
            at = acct.get("usage_snapshot_at")
            dark_since: datetime | None = None
            if failed_at is not None and at is not None:
                dark_since = min(failed_at, at)
            elif failed_at is not None:
                dark_since = failed_at
            elif at is not None:
                dark_since = at
            # No stamps at all → never probed; use None to indicate unknown onset.
            if dark_since is not None and (now - dark_since).total_seconds() < threshold_seconds:
                continue
            stale.append(
                {
                    "id": acct.get("id"),
                    "name": acct.get("name"),
                    "dark_reason": _usage_dark_reason(acct),
                    "dark_since": dark_since,
                }
            )
    return stale


async def _load_persisted_usage_blobs() -> Dict[str, Any]:
    """Fetch every account's persisted ``rate_limit_usage`` blob keyed by id str.

    Best-effort and read-only: on any DB error returns an empty map so the
    caller degrades to in-memory-only classification rather than raising. The
    fleet-wide reliability summary must never break the settings load.
    """
    from sqlmodel import select as sql_select

    from app.models import MaxPlanAccount

    blobs: Dict[str, Any] = {}
    try:
        async with AsyncSessionLocal() as session:
            rows = (
                await session.execute(
                    sql_select(MaxPlanAccount.id, MaxPlanAccount.rate_limit_usage)
                )
            ).all()
        for acct_id, raw in rows:
            if raw is not None:
                blobs[str(acct_id)] = raw
    except Exception:
        logger.warning(
            "[POOL] failed to load persisted usage blobs for reliability stats",
            exc_info=True,
        )
    return blobs


def _fresher_usage_stamp(
    acct: Dict[str, Any], persisted_raw: Any
) -> datetime | None:
    """Return the wall-clock stamp of the fresher KNOWN usage snapshot across
    the in-memory probe result and the DB-persisted blob, or None if neither
    source has a known snapshot. Mirrors ``usage_snapshot_summary_with_fallback``
    so non-leader instances see the leader's persisted meters.
    """
    stamps: list[datetime] = []
    snap = acct.get("usage_snapshot")
    at = acct.get("usage_snapshot_at")
    if isinstance(snap, UsageSnapshot) and snap.known and at is not None:
        stamps.append(at)
    parsed = _snapshot_from_persisted_usage(persisted_raw)
    if parsed is not None:
        stamps.append(parsed[1])
    return max(stamps) if stamps else None


async def get_meter_reliability_stats(
    now: datetime | None = None,
    min_coverage_pct: int = 80,
) -> Dict[str, Any]:
    """Return a fleet-wide usage-meter reliability summary.

    Walks the account pool under ``_max_plan_pool_lock`` and classifies every
    account as ineligible / known-fresh / known-stale / dark.  The returned
    dict is JSON-safe and side-effect-free.

    Keys returned:
    - ``meters_eligible``    — accounts that can produce a usage snapshot
    - ``meters_known``       — eligible accounts with a fresh known snapshot
    - ``meters_stale``       — eligible accounts with a stale but known snapshot
    - ``meters_dark``        — eligible accounts with no known snapshot
    - ``meters_ineligible``  — accounts skipped (non-OAuth / disabled / no secret)
    - ``dark_by_reason``     — dict mapping dark-reason string → count
    - ``coverage_ratio``     — meters_known / meters_eligible (None if eligible=0)
    - ``persist_failure_count`` — module-level DB persist failure counter
    - ``armable``            — True iff coverage_ratio >= min_coverage_pct/100
                               and persist_failure_count == 0

    Cross-instance correctness: probes are scheduled only from the leader
    orchestrator loop, so the in-memory ``usage_snapshot`` is absent on
    non-leader Cloud Run instances. Classifying purely off memory would report
    every account dark (coverage_ratio=0, armable=False) on non-leaders even
    when meters are healthy per the shared DB — the exact blindness
    ``usage_snapshot_summary_with_fallback`` exists to fix. So we fetch the
    DB-persisted ``MaxPlanAccount.rate_limit_usage`` blobs and classify each
    account off the fresher of the in-memory and persisted snapshots.
    """
    now = now or _utcnow()
    meters_known = 0
    meters_stale = 0
    meters_dark = 0
    meters_ineligible = 0
    dark_by_reason: Dict[str, int] = {}

    persisted_by_id = await _load_persisted_usage_blobs()

    async with _max_plan_pool_lock:
        for acct in _account_by_id.values():
            if not _account_meter_eligible(acct):
                meters_ineligible += 1
                continue
            # Prefer the fresher of the in-memory probe result and the
            # DB-persisted blob (mirrors usage_snapshot_summary_with_fallback),
            # so non-leader instances read the leader's persisted meters.
            best_at = _fresher_usage_stamp(
                acct, persisted_by_id.get(str(acct.get("id")))
            )
            if best_at is not None:
                stale = (now - best_at).total_seconds() >= _USAGE_METER_DISPLAY_STALE_SECONDS
                if stale:
                    meters_stale += 1
                else:
                    meters_known += 1
            else:
                meters_dark += 1
                reason = _usage_dark_reason(acct) or "unknown"
                dark_by_reason[reason] = dark_by_reason.get(reason, 0) + 1

    meters_eligible = meters_known + meters_stale + meters_dark
    persist_failures = get_usage_persist_failure_count()
    coverage_ratio: float | None = (
        meters_known / meters_eligible if meters_eligible > 0 else None
    )
    armable = (
        coverage_ratio is not None
        and coverage_ratio >= min_coverage_pct / 100.0
        and persist_failures == 0
    )
    return {
        "meters_eligible": meters_eligible,
        "meters_known": meters_known,
        "meters_stale": meters_stale,
        "meters_dark": meters_dark,
        "meters_ineligible": meters_ineligible,
        "dark_by_reason": dark_by_reason,
        "coverage_ratio": coverage_ratio,
        "persist_failure_count": persist_failures,
        "armable": armable,
    }


# Leader-memory marker: the timestamp at which ``armable`` first became — and has
# since continuously stayed — True. Reset to None the instant ``armable`` regresses
# (or the mode leaves "auto"), so the sustained-observation clock restarts. A leader
# restart also resets it via module reload, which is fail-closed / conservative: the
# window is simply re-observed before the gate is (re-)armed.
_usage_headroom_armable_since: datetime | None = None


def get_usage_headroom_armable_since() -> datetime | None:
    """Return the leader-memory 'armable-since' marker (None if not currently
    holding, or if this instance is not the leader that runs the reconciler)."""
    return _usage_headroom_armable_since


async def _persist_usage_headroom_armable_since(dt: datetime | None) -> bool:
    """Persist the armable-since timestamp into DeploymentState.runtime_config_json.

    Modeled after ``_persist_usage_headroom_gate_enabled`` — the same JSONB config
    row, different key. None values are persisted as JSON null so the restore
    path can distinguish ``never-set`` from ``explicitly-reset``.
    """
    from app.models import DeploymentState

    value = dt.isoformat() if dt is not None else None
    try:
        async with AsyncSessionLocal() as session:
            state = await session.get(DeploymentState, 1)
            if state is None:
                logger.warning(
                    "[POOL] usage_headroom persist armable-since: "
                    "DeploymentState id=1 missing; skipped"
                )
                return False
            config_json = dict(getattr(state, "runtime_config_json", None) or {})
            config_json["usage_headroom_armable_since"] = value
            state.runtime_config_json = config_json
            session.add(state)
            await session.commit()
    except Exception:
        logger.warning(
            "[POOL] usage_headroom persist armable-since: failed to persist %s",
            value,
            exc_info=True,
        )
        return False
    return True


async def _restore_usage_headroom_armable_since() -> bool:
    """Restore armable-since from DB on leader start.

    Called from ``reconcile_usage_headroom_arming`` when the module variable is
    None (process restart / module reload). Reads the previously-persisted ISO
    string from DeploymentState.runtime_config_json and sets the module variable
    so the sustained-observation window is **not** reset by a restart.

    Returns True if a value was restored.
    """
    global _usage_headroom_armable_since

    try:
        from app.models import DeploymentState

        async with AsyncSessionLocal() as session:
            state = await session.get(DeploymentState, 1)
            if state is None:
                return False
            config_json = getattr(state, "runtime_config_json", None) or {}
            raw = config_json.get("usage_headroom_armable_since")
            if raw is not None and isinstance(raw, str):
                _usage_headroom_armable_since = datetime.fromisoformat(raw)
                return True
    except Exception:
        logger.warning(
            "[POOL] usage_headroom restore armable-since: failed; "
            "window will start fresh",
            exc_info=True,
        )
    return False


async def _persist_usage_headroom_gate_enabled(value: bool) -> bool:
    """Persist ``usage_headroom_gate_enabled`` into DeploymentState.runtime_config_json
    (id=1) BEFORE mutating in-memory ``runtime_config``.

    A process must never run a gate state that was not durably committed, so the
    DB write happens first; only on a successful commit do we mirror the value
    into process-local config (non-leader instances pick it up via
    ``sync_runtime_config_from_state``). A persist failure leaves in-memory state
    unchanged and returns False — it never raises.
    """
    from app.models import DeploymentState

    try:
        async with AsyncSessionLocal() as session:
            state = await session.get(DeploymentState, 1)
            if state is None:
                logger.warning(
                    "[POOL] usage_headroom auto-arm: DeploymentState id=1 missing; "
                    "gate left unchanged (wanted=%s)",
                    value,
                )
                return False
            config_json = dict(getattr(state, "runtime_config_json", None) or {})
            config_json["usage_headroom_gate_enabled"] = value
            state.runtime_config_json = config_json
            session.add(state)
            await session.commit()
    except Exception:
        logger.warning(
            "[POOL] usage_headroom auto-arm: failed to persist gate=%s to "
            "DeploymentState; in-memory state unchanged",
            value,
            exc_info=True,
        )
        return False

    # Durably committed — now mirror into process-local runtime_config.
    try:
        from app.shared_state import runtime_config as _rc

        if isinstance(_rc, dict):
            _rc["usage_headroom_gate_enabled"] = value
    except Exception:  # pragma: no cover - config mirror is best-effort
        logger.warning(
            "[POOL] usage_headroom auto-arm: persisted gate=%s but failed to "
            "update in-memory runtime_config",
            value,
            exc_info=True,
        )
    return True


async def reconcile_usage_headroom_arming(
    now: datetime | None = None,
) -> Dict[str, Any]:
    """Autonomously arm/disarm the usage_headroom selection gate from sustained
    meter reliability. Leader-only (probes and the observation-window marker are
    leader-memory). Returns a JSON-safe decision dict for logging/observability.

    Governed by ``usage_headroom_auto_arm_mode`` (default "auto"):

    - ``"auto"``: ARM (set ``usage_headroom_gate_enabled`` True) only once the
      ``armable`` verdict has held True CONTINUOUSLY for
      ``usage_headroom_auto_arm_observation_minutes`` (default 60). DISARM
      immediately (fail-safe, no window) the instant ``armable`` goes False.
    - ``"manual"``: the reconciler never touches the gate — operator owns it.
    - ``"off"``: force-disarm kill switch — drive the gate to False and hold it.

    Flips are persisted to DeploymentState BEFORE the in-memory mutation and are
    idempotent (only written when the value actually changes). A persist failure
    leaves state unchanged and never crashes the caller.

    Persistence: the armable-since marker is written to
    ``DeploymentState.runtime_config_json`` on every transition (non-None to None
    on reset, or None to a timestamp on first observation).  On process restart
    the marker is restored on the first reconciler tick so the observation window
    is **not** reset by a restart — only a genuine meter-health regression
    (armable=False) discards it.
    """
    global _usage_headroom_armable_since
    now = now or _utcnow()

    # After a process restart the module variable is None (module reload). Restore
    # any previously-persisted marker so the observation window survives restarts.
    # Only a genuine metering-health regression (armable=False below) discards it.
    if _usage_headroom_armable_since is None:
        await _restore_usage_headroom_armable_since()

    cfg = _runtime_config_snapshot()
    mode = str(cfg.get("usage_headroom_auto_arm_mode", "auto") or "auto").strip().lower()
    if mode not in ("auto", "manual", "off"):
        mode = "auto"
    current_enabled = bool(cfg.get("usage_headroom_gate_enabled", False))

    # manual: reconciler is a no-op; the operator owns the gate. Reset the window
    # so a later switch to auto starts a fresh observation rather than arming on a
    # marker that accrued while manual. Persist the reset so the next leader also
    # starts fresh.
    if mode == "manual":
        had_marker = _usage_headroom_armable_since is not None
        _usage_headroom_armable_since = None
        if had_marker:
            await _persist_usage_headroom_armable_since(None)
        return {
            "mode": "manual",
            "action": "noop",
            "gate_enabled": current_enabled,
            "armable": None,
            "armable_since": None,
        }

    # off: force-disarm kill switch. Hold the gate False. Persist the marker
    # reset so a subsequent "auto" mode restart doesn't inherit the stale window.
    if mode == "off":
        had_marker = _usage_headroom_armable_since is not None
        _usage_headroom_armable_since = None
        if had_marker:
            await _persist_usage_headroom_armable_since(None)
        if not current_enabled:
            return {
                "mode": "off",
                "action": "noop",
                "gate_enabled": False,
                "armable": None,
                "armable_since": None,
            }
        ok = await _persist_usage_headroom_gate_enabled(False)
        if ok:
            logger.info(
                "[POOL] usage_headroom auto-arm: mode=off — force-disarmed gate"
            )
            return {
                "mode": "off",
                "action": "disarmed",
                "gate_enabled": False,
                "armable": None,
                "armable_since": None,
            }
        return {
            "mode": "off",
            "action": "persist_failed",
            "gate_enabled": current_enabled,
            "armable": None,
            "armable_since": None,
        }

    # auto mode.
    min_coverage_pct = 80
    try:
        min_coverage_pct = int(cfg.get("usage_headroom_min_coverage_pct", 80))
    except (TypeError, ValueError):
        min_coverage_pct = 80
    stats = await get_meter_reliability_stats(
        now=now, min_coverage_pct=min_coverage_pct
    )
    armable = bool(stats.get("armable"))

    observation_minutes = 60
    try:
        observation_minutes = int(
            cfg.get("usage_headroom_auto_arm_observation_minutes", 60)
        )
    except (TypeError, ValueError):
        observation_minutes = 60
    if observation_minutes < 1:
        observation_minutes = 1

    # Fail-safe: the instant armable regresses, reset the window and disarm now
    # (no observation window on the way down). Persist the reset so a subsequent
    # restart doesn't inherit the stale window.
    if not armable:
        had_marker = _usage_headroom_armable_since is not None
        _usage_headroom_armable_since = None
        if had_marker:
            await _persist_usage_headroom_armable_since(None)
        if not current_enabled:
            return {
                "mode": "auto",
                "action": "noop",
                "gate_enabled": False,
                "armable": False,
                "armable_since": None,
            }
        ok = await _persist_usage_headroom_gate_enabled(False)
        if ok:
            logger.info(
                "[POOL] usage_headroom auto-arm: armable regressed — fail-safe "
                "disarmed gate (coverage_ratio=%s, persist_failures=%s)",
                stats.get("coverage_ratio"),
                stats.get("persist_failure_count"),
            )
            return {
                "mode": "auto",
                "action": "disarmed",
                "gate_enabled": False,
                "armable": False,
                "armable_since": None,
            }
        return {
            "mode": "auto",
            "action": "persist_failed",
            "gate_enabled": current_enabled,
            "armable": False,
            "armable_since": None,
        }

    # armable is True — track the sustained-observation window.
    # If this is a fresh observation (never held or restored), mark the start
    # and persist it so the window survives a restart.
    if _usage_headroom_armable_since is None:
        _usage_headroom_armable_since = now
        await _persist_usage_headroom_armable_since(now)
    since = _usage_headroom_armable_since
    elapsed_seconds = (now - since).total_seconds()
    window_seconds = observation_minutes * 60

    # Already armed — idempotent no-op (do NOT re-persist each cycle).
    if current_enabled:
        return {
            "mode": "auto",
            "action": "noop",
            "gate_enabled": True,
            "armable": True,
            "armable_since": since.isoformat(),
        }

    # Not yet armed and the window has not elapsed — keep waiting.
    if elapsed_seconds < window_seconds:
        return {
            "mode": "auto",
            "action": "waiting",
            "gate_enabled": False,
            "armable": True,
            "armable_since": since.isoformat(),
            "elapsed_seconds": elapsed_seconds,
            "window_seconds": window_seconds,
        }

    # Sustained across the window — arm the gate.
    ok = await _persist_usage_headroom_gate_enabled(True)
    if ok:
        logger.info(
            "[POOL] usage_headroom auto-arm: armable sustained %.0fs "
            "(window=%.0fs) — armed gate (coverage_ratio=%s)",
            elapsed_seconds,
            window_seconds,
            stats.get("coverage_ratio"),
        )
        return {
            "mode": "auto",
            "action": "armed",
            "gate_enabled": True,
            "armable": True,
            "armable_since": since.isoformat(),
        }
    return {
        "mode": "auto",
        "action": "persist_failed",
        "gate_enabled": current_enabled,
        "armable": True,
        "armable_since": since.isoformat(),
    }


def _apply_usage_gate(
    candidates: List[Any],
    *,
    now: datetime,
    threshold_pct: float,
    acct_getter,
) -> List[Any]:
    """Soft-skip candidates whose KNOWN util >= threshold, only if some remain.

    Mirrors the health-score disqualification idiom: never empties the candidate
    set (no deadlock / no claim blocked) — if every candidate is over threshold
    the original set is returned unchanged. Unknown snapshots are never skipped.
    """
    if len(candidates) <= 1:
        return candidates
    not_exhausted = [
        c
        for c in candidates
        if not _usage_gate_should_skip(acct_getter(c), now, threshold_pct)
    ]
    return not_exhausted if not_exhausted else candidates


def _usage_dark_reason(acct: Dict[str, Any]) -> str | None:
    """Classify WHY an account's usage meter is dark (known=False).

    Returns one of:
    - ``"unsupported"``: the provider has no stable machine-readable plan
      usage contract (currently Codex/OpenAI); this is not retried as an
      endpoint outage.
    - ``"no_secret"``: OAuth account but both oauth_token and runtime_secret
      are absent and Secret Manager was not attempted — probe cannot
      authenticate.
    - ``"secret_fetch_failed"``: OAuth account whose Secret Manager fetch
      failed — credential is unreachable, meter cannot authenticate.
    - ``"never_probed"``: meter-eligible account that has never been probed
      (no snapshot and no probe-failure stamp).
    - a stable probe failure class (``authentication``, ``timeout``,
      ``transport``, ``endpoint``, ``schema``, ``provider_error``) when the
      latest attempt supplied one.
    - ``None``: meter is known (should not be called on known snapshots).
    """
    auth_mode = _normalize_auth_mode(acct.get("auth_mode")) or LLMAuthMode.OAUTH.value
    provider = _normalize_provider_name(acct.get("provider"))
    if provider == LLMProviderName.OPENAI.value:
        return "unsupported"
    failure_kind = acct.get("usage_probe_failure_kind")
    if isinstance(failure_kind, str) and failure_kind:
        return failure_kind
    # Anthropic API-key accounts with a runtime_secret are now meter-eligible
    # (they probe via messages.create headers).  All other non-OAuth modes are
    # ineligible and classified as not_oauth.
    if auth_mode != LLMAuthMode.OAUTH.value:
        if (
            auth_mode == LLMAuthMode.API_KEY.value
            and provider == LLMProviderName.ANTHROPIC.value
            and bool(acct.get("secret_blob_ref") or acct.get("runtime_secret"))
        ):
            pass  # fall through to the eligible path below
        else:
            return "not_oauth"
    secret = acct.get("oauth_token") or acct.get("runtime_secret")
    if not secret:
        if acct.get("secret_fetch_failed"):
            return "secret_fetch_failed"
        return "no_secret"
    # Meter-eligible: OAuth + secret present.
    snap = acct.get("usage_snapshot")
    failed_at = acct.get("usage_probe_failed_at")
    at = acct.get("usage_snapshot_at")
    if snap is None and failed_at is None and at is None:
        return "never_probed"
    # Probe was attempted (has a snapshot timestamp or failure stamp).
    return "endpoint_error"


def _usage_attempt_from_persisted(
    raw: Any,
) -> tuple[str | None, str | None]:
    """Read the non-sensitive last-attempt state from a persisted meter blob."""
    if not isinstance(raw, Mapping):
        return None, None
    attempted_at = raw.get("last_attempt_at")
    status = raw.get("last_attempt_status")
    # Blobs written before explicit attempt state still carry an unknown probe
    # as source=oauth_usage_probe, known=false, probed_at=... .
    if not isinstance(attempted_at, str):
        if raw.get("known") is False and isinstance(raw.get("probed_at"), str):
            attempted_at = raw["probed_at"]
    if not isinstance(status, str) or not status:
        if raw.get("known") is False:
            raw_kind = raw.get("probe_failure_kind")
            status = (
                raw_kind
                if isinstance(raw_kind, str) and raw_kind
                else "provider_error"
            )
        else:
            status = None
    return attempted_at, status


def _usage_attempt_from_memory(
    acct: Dict[str, Any] | None,
) -> tuple[str | None, str | None]:
    if acct is None:
        return None, None
    at = acct.get("usage_probe_attempt_at")
    if isinstance(at, datetime):
        attempted_at = at.isoformat()
    elif isinstance(at, str):
        attempted_at = at
    else:
        snap = acct.get("usage_snapshot")
        attempted_at = snap.probed_at if isinstance(snap, UsageSnapshot) else None
    kind = acct.get("usage_probe_failure_kind")
    if isinstance(kind, str) and kind:
        return attempted_at, kind
    snap = acct.get("usage_snapshot")
    if isinstance(snap, UsageSnapshot) and not snap.known:
        return attempted_at, snap.probe_failure_kind or "provider_error"
    if attempted_at:
        return attempted_at, "known"
    return None, None


def _latest_usage_attempt(
    acct: Dict[str, Any] | None, persisted_raw: Any
) -> tuple[str | None, str | None]:
    """Choose the latest safe probe-attempt receipt across memory and DB."""
    candidates = [
        _usage_attempt_from_memory(acct),
        _usage_attempt_from_persisted(persisted_raw),
    ]
    candidates = [
        item for item in candidates if item[0] is not None or item[1] is not None
    ]
    if not candidates:
        return None, None
    # ISO-8601 timestamps are lexicographically sortable for the format we
    # write.  A malformed timestamp is still rendered but safely loses to a
    # well-formed/current one instead of breaking the account response.
    return max(candidates, key=lambda item: item[0] or "")


def _with_usage_attempt(
    summary: Dict[str, Any], attempted_at: str | None, status: str | None
) -> Dict[str, Any]:
    """Attach additive attempt diagnostics without changing last-good values."""
    summary["last_attempt_at"] = attempted_at
    summary["last_attempt_status"] = status
    summary["last_attempt_failure_kind"] = (
        status if status and status != "known" else None
    )
    return summary


def _usage_snapshot_summary(acct: Dict[str, Any], now: datetime) -> Dict[str, Any]:
    """JSON-safe per-account usage snapshot for the admin health endpoint.

    Always returns a dict (additive field). ``known=False`` covers the
    never-probed and probe-unknown states; ``stale`` flags a snapshot older
    than the DISPLAY freshness window (three missed meter refreshes) — NOT
    the 60s selection TTL, which would flag nearly every render under the
    300s meter cadence. Selection freshness is handled separately by
    ``_cached_usage_snapshot``.

    ``dark_reason`` (str | None) is populated on the ``known=False`` branch so
    operators can distinguish ineligible accounts from probe failures.
    """
    snap = acct.get("usage_snapshot")
    at = acct.get("usage_snapshot_at")
    stale = (
        at is None
        or (now - at).total_seconds() >= _USAGE_METER_DISPLAY_STALE_SECONDS
    )
    if not isinstance(snap, UsageSnapshot) or not snap.known:
        probed_at = snap.probed_at if isinstance(snap, UsageSnapshot) else None
        return _with_usage_attempt({
            "known": False,
            "dark_reason": _usage_dark_reason(acct),
            "five_hour_utilization": None,
            "seven_day_utilization": None,
            "resets_at": {},
            "probed_at": probed_at,
            "stale": stale,
        }, *_usage_attempt_from_memory(acct))
    return _with_usage_attempt({
        "known": True,
        "dark_reason": None,
        "five_hour_utilization": snap.window_utilizations.get("five_hour"),
        "seven_day_utilization": snap.window_utilizations.get("seven_day"),
        "resets_at": dict(snap.resets_at),
        "probed_at": snap.probed_at,
        "stale": stale,
    }, *_usage_attempt_from_memory(acct))


def _usage_summary_from_snapshot(
    snap: UsageSnapshot,
    at: datetime,
    now: datetime,
    *,
    attempted_at: str | None = None,
    attempt_status: str | None = None,
) -> Dict[str, Any]:
    """JSON-safe summary for a KNOWN snapshot with wall-clock stamp ``at``."""
    stale = (now - at).total_seconds() >= _USAGE_METER_DISPLAY_STALE_SECONDS
    return _with_usage_attempt({
        "known": True,
        "dark_reason": None,
        "five_hour_utilization": snap.window_utilizations.get("five_hour"),
        "seven_day_utilization": snap.window_utilizations.get("seven_day"),
        "resets_at": dict(snap.resets_at),
        "probed_at": snap.probed_at,
        "stale": stale,
    }, attempted_at, attempt_status)


def usage_snapshot_summary_with_fallback(
    acct: Dict[str, Any] | None,
    persisted_raw: Any,
    now: datetime,
) -> Dict[str, Any]:
    """Usage-snapshot summary that falls back to the DB-persisted blob.

    The in-memory probe result only exists on the instance whose orchestrator
    leader actually ran the probe: probes are scheduled ONLY from the
    leader-gated orchestrator cycle (``refresh_usage_meter_snapshots``), and
    non-leader Cloud Run instances load the pool once at startup and never
    refresh it (see ``lifecycle`` — the periodic ``load_max_plan_pool`` /
    meter-refresh both live inside the leader-only loop). Because the admin API
    is load-balanced across every instance, the in-memory snapshot is missing
    (``known=False``) on each non-leader — so the meter renders blank for the
    operator even though a fresh probe result is durably persisted in the shared
    DB column ``MaxPlanAccount.rate_limit_usage``.

    Fix: when the in-memory snapshot is missing/unknown — or when the persisted
    blob is fresher than the frozen in-memory copy — reconstruct the summary
    from the persisted blob so the meter is correct on every instance
    regardless of which one is the leader. Picks the fresher of the two known
    sources (mirrors ``_restore_usage_state``).
    """
    candidates: list[tuple[datetime, UsageSnapshot]] = []
    if acct is not None:
        snap = acct.get("usage_snapshot")
        at = acct.get("usage_snapshot_at")
        if isinstance(snap, UsageSnapshot) and snap.known and at is not None:
            candidates.append((at, snap))
    parsed = _snapshot_from_persisted_usage(persisted_raw)
    if parsed is not None:
        candidates.append((parsed[1], parsed[0]))
    if not candidates:
        # No known source anywhere — preserve the in-memory unknown summary
        # (carries probed_at/stale from a probe-unknown result) when present.
        if acct is not None:
            summary = _usage_snapshot_summary(acct, now)
            attempted_at, attempt_status = _latest_usage_attempt(acct, persisted_raw)
            return _with_usage_attempt(summary, attempted_at, attempt_status)
        attempted_at, attempt_status = _latest_usage_attempt(acct, persisted_raw)
        return _with_usage_attempt({
            "known": False,
            "dark_reason": "unsupported" if acct is None else _usage_dark_reason(acct),
            "five_hour_utilization": None,
            "seven_day_utilization": None,
            "resets_at": {},
            "probed_at": None,
            "stale": True,
        }, attempted_at, attempt_status)
    best_at, best_snap = max(candidates, key=lambda c: c[0])
    attempted_at, attempt_status = _latest_usage_attempt(acct, persisted_raw)
    return _usage_summary_from_snapshot(
        best_snap,
        best_at,
        now,
        attempted_at=attempted_at,
        attempt_status=attempt_status,
    )


async def select_max_plan_account_balanced(
    user_id: str,
    provider_models: List[tuple[str, str | None]],
    blacklist: List[str] | None = None,
    task_type: str | None = None,
    runtime_config: Mapping[str, Any] | None = None,
    project_id: str | None = None,
    reject_reasons: List[str] | None = None,
) -> tuple[Dict[str, Any], str, str | None] | None:
    """Balanced-mode selection: flatten all accounts across the given providers
    into one pool and pick the single best by (health_score, -active_tasks).

    ``provider_models`` is an ordered list of (provider_name, model) pairs —
    the model determines which disabled_models cascade is checked for that
    provider. Returns (account, provider_name, model) for the winner, or
    None when no account is eligible across any provider.

    Probing-mode semantics carry over: an account in ``probing`` state is
    eligible only when its ``active_tasks == 0`` (serialized probe). Healthy
    accounts win over probing when both are available, matching the
    single-provider selector's contract.

    When *project_id* is provided, per-project cap gating is applied BEFORE
    the global cap check. See ``_project_at_cap``.

    When *reject_reasons* is provided, a short per-account reason is appended
    for every provider-matching account that was skipped."""
    if not provider_models:
        return None
    async with _max_plan_pool_lock:
        user_pool = _max_plan_pools.get(user_id, [])
        # Build lookup from account id → (provider, model) so we can resolve
        # the winner's attribution when we rank across all providers at once.
        candidates_healthy: List[tuple[Dict[str, Any], str, str | None]] = []
        candidates_probing: List[tuple[Dict[str, Any], str, str | None]] = []
        now = _utcnow()
        dirty_from_reset: list[str] = []
        seen_ids: set[str] = set()

        def _reject(acct: dict, provider: str, why: str) -> None:
            if reject_reasons is not None:
                reject_reasons.append(
                    f"{provider}:{acct.get('name') or acct.get('id')}: {why}"
                )

        for provider_name, model in provider_models:
            normalized_provider = _normalize_provider_name(provider_name)
            if not normalized_provider:
                continue
            for a in user_pool:
                if a["id"] in seen_ids:
                    continue
                acct_provider = (
                    _normalize_provider_name(a.get("provider"))
                    or LLMProviderName.ANTHROPIC.value
                )
                if acct_provider != normalized_provider:
                    continue
                if blacklist and a["id"] in blacklist:
                    _reject(a, normalized_provider, "blacklisted for this task")
                    continue
                if _daily_reset_if_elapsed(a, now):
                    dirty_from_reset.append(a["id"])
                # Per-project cap check runs BEFORE global cap check.
                proj_cap_reason = _project_at_cap(a, project_id)
                if proj_cap_reason:
                    _reject(a, normalized_provider, proj_cap_reason)
                    continue
                cap_reason = _account_at_cap(a)
                if cap_reason:
                    _reject(a, normalized_provider, cap_reason)
                    continue
                state = _account_state_for_model(a, model, now)
                if state == "healthy":
                    candidates_healthy.append((a, normalized_provider, model))
                    seen_ids.add(a["id"])
                elif state == "probing":
                    if a.get("active_tasks", 0) == 0:
                        candidates_probing.append((a, normalized_provider, model))
                        seen_ids.add(a["id"])
                    else:
                        _reject(
                            a,
                            normalized_provider,
                            "cooldown elapsed, probe already in flight",
                        )
                else:
                    # Guarded AT THE CALL SITE, not inside _reject: Python
                    # evaluates arguments eagerly, so an unguarded
                    # `_reject(a, p, _account_unavailable_reason(...))` would
                    # build the reason (in-function import + datetime math +
                    # f-strings) for EVERY rejected account on EVERY selection,
                    # while this function holds the global _max_plan_pool_lock.
                    # Cost would scale with pool size x rejected accounts and
                    # peak during a cooldown storm — the exact incident the
                    # reasons exist to diagnose. Every other _reject arg above
                    # is a constant or a value the control flow already had.
                    if reject_reasons is not None:
                        _reject(
                            a,
                            normalized_provider,
                            _account_unavailable_reason(a, model, now),
                        )

        for acct_id in dirty_from_reset:
            _pending_cap_persists.add(acct_id)

        # Proactive usage-headroom (advisory; no I/O under this lock). When
        # enabled, warm the per-account snapshot cache for every candidate and
        # apply the claim-time gate for long-running task types. Unknown/stale
        # snapshots are never gated and rank neutrally.
        gate_enabled, gate_threshold, gate_long_types = _usage_headroom_settings(
            runtime_config
        )
        if gate_enabled:
            for c in candidates_healthy:
                _maybe_schedule_usage_refresh(c[0], now)
            for c in candidates_probing:
                _maybe_schedule_usage_refresh(c[0], now)
            if task_type in gate_long_types:
                candidates_healthy = _apply_usage_gate(
                    candidates_healthy,
                    now=now,
                    threshold_pct=gate_threshold,
                    acct_getter=lambda c: c[0],
                )
                candidates_probing = _apply_usage_gate(
                    candidates_probing,
                    now=now,
                    threshold_pct=gate_threshold,
                    acct_getter=lambda c: c[0],
                )

        winner: tuple[Dict[str, Any], str, str | None] | None = None
        if candidates_healthy:
            # Gate decisively-negative accounts when alternatives exist — but
            # otherwise rank primarily by load. Ranking by health_score across
            # providers is unfair: a mature anthropic account with 100
            # completions scores 100 while a brand-new openai account scores 0,
            # so balanced mode would never pick the new account.
            if len(candidates_healthy) > 1:
                qualified = [
                    c
                    for c in candidates_healthy
                    if _compute_health_score(c[0], now)
                    >= _HEALTH_SCORE_DISQUALIFY_THRESHOLD
                ]
                if qualified:
                    candidates_healthy = qualified
                # Success-rate soft bias: deprioritize accounts whose 7-day success
                # rate is materially below the pool median when alternatives exist.
                # Sparse accounts (< 3 outcomes) return None and are treated as
                # neutral so brand-new accounts are never starved.
                if len(candidates_healthy) > 1:
                    rates = [
                        _effective_success_rate(c[0], task_type, now)
                        for c in candidates_healthy
                    ]
                    sampled = [r for r in rates if r is not None]
                    if sampled:
                        pool_median = sorted(sampled)[len(sampled) // 2]
                        preferred = [
                            c
                            for c, r in zip(candidates_healthy, rates, strict=False)
                            if r is None
                            or r >= pool_median - _SUCCESS_RATE_DEPRIORITIZE_MARGIN
                        ]
                        if preferred:
                            candidates_healthy = preferred
            winner = min(
                candidates_healthy,
                # Primary: fewest active_tasks. When usage-headroom is enabled,
                # prefer more remaining headroom next (advisory; unknown ranks
                # neutrally so unprobed accounts aren't starved). Then higher 7d
                # success rate (task-type-scoped when confident, aggregate
                # fallback, None → 0.5 so sparse accounts aren't penalised), then
                # faster avg duration (None → neutral 0). Tiebreak: by id.
                key=lambda c: (
                    c[0].get("active_tasks", 0),
                    -_usage_headroom_rank(c[0], now) if gate_enabled else 0.0,
                    -(_effective_success_rate(c[0], task_type, now) or 0.5),
                    _compute_avg_duration_24h(c[0], now) or 0.0,
                    c[0]["id"],
                ),
            )
        elif candidates_probing:
            # Deterministic tiebreak on id — same contract as the single-provider selector.
            winner = min(candidates_probing, key=lambda c: c[0]["id"])
            logger.info(
                f"[POOL] Balanced-mode probing account {winner[0]['name']} "
                f"(provider={winner[1]}, cooldown elapsed, serializing as probe)"
            )

        if winner is None:
            return None
        account, prov, model = winner
        account["active_tasks"] = account.get("active_tasks", 0) + 1
        account["daily_task_count"] = int(account.get("daily_task_count") or 0) + 1
        # Per-project counter increment (runtime-only, best-effort).
        if project_id:
            proj_state = account.setdefault("project_state", {}).setdefault(project_id, {})
            proj_state["active_tasks"] = proj_state.get("active_tasks", 0) + 1
            proj_state["daily_task_count"] = proj_state.get("daily_task_count", 0) + 1
        _stage_usage_delta(account, 1, project_id)
        selected_winner = (account, prov, model)

    await _maybe_refresh_account_secret(selected_winner[0])
    return selected_winner


async def select_max_plan_account(
    user_id: str,
    model: str | None = None,
    blacklist: List[str] | None = None,
    provider: str | LLMProviderName | None = None,
    task_type: str | None = None,
    runtime_config: Mapping[str, Any] | None = None,
    project_id: str | None = None,
    reject_reasons: List[str] | None = None,
) -> Dict[str, Any] | None:
    """Select a Max plan account for a task. Two-tier circuit-breaker selection:

    1. Healthy accounts first, picked by min(active_tasks) for load balancing.
    2. Probing accounts (post-cooldown, unverified) second, but only if
       active_tasks == 0 — this serializes the first task after cooldown as a
       de-facto probe, preventing a thundering herd on a still-rate-limited
       account. If the probe succeeds, the COMPLETED path clears disabled_models
       (restoring full concurrency); if it 429s, the mark path extends cooldown.

    When *project_id* is provided, per-project cap gating is applied BEFORE
    the global cap check: accounts at their per-project daily/concurrent cap
    for this project are skipped, and per-project counters are incremented on
    selection alongside the global counters. When *project_id* is None, no
    per-project gating is applied (fully backward-compatible).

    When *reject_reasons* is provided, a short per-account reason is appended
    for every provider-matching account that was skipped, so callers can report
    WHY the pool came up empty instead of a bare "pool exhausted".

    Pre-increments active_tasks atomically under lock."""
    async with _max_plan_pool_lock:
        user_pool = _max_plan_pools.get(user_id, [])
        healthy: List[Dict[str, Any]] = []
        probing: List[Dict[str, Any]] = []
        normalized_provider = _normalize_provider_name(provider)
        now = _utcnow()
        dirty_from_reset: list[str] = []
        provider_matched = 0

        def _reject(acct: dict, why: str) -> None:
            if reject_reasons is not None:
                reject_reasons.append(f"{acct.get('name') or acct.get('id')}: {why}")

        for a in user_pool:
            if (
                normalized_provider
                and (
                    _normalize_provider_name(a.get("provider"))
                    or LLMProviderName.ANTHROPIC.value
                )
                != normalized_provider
            ):
                continue
            provider_matched += 1
            if blacklist and a["id"] in blacklist:
                _reject(a, "blacklisted for this task")
                continue
            if _daily_reset_if_elapsed(a, now):
                dirty_from_reset.append(a["id"])
            # Per-project cap check runs BEFORE global cap check.
            proj_cap_reason = _project_at_cap(a, project_id)
            if proj_cap_reason:
                _reject(a, proj_cap_reason)
                continue
            cap_reason = _account_at_cap(a)
            if cap_reason:
                _reject(a, cap_reason)
                continue
            state = _account_state_for_model(a, model, now)
            if state == "healthy":
                healthy.append(a)
            elif state == "probing":
                if a.get("active_tasks", 0) == 0:
                    probing.append(a)
                else:
                    _reject(a, "cooldown elapsed, probe already in flight")
            else:
                # Guarded at the CALL SITE — see the identical guard in
                # select_max_plan_account_balanced. Arguments are evaluated
                # eagerly, so the guard inside _reject does not stop
                # _account_unavailable_reason from running under the global
                # _max_plan_pool_lock for every rejected account.
                if reject_reasons is not None:
                    _reject(a, _account_unavailable_reason(a, model, now))

        if provider_matched == 0 and reject_reasons is not None:
            reject_reasons.append(
                f"no accounts configured for provider {normalized_provider or 'any'}"
            )

        for acct_id in dirty_from_reset:
            _pending_cap_persists.add(acct_id)

        # Proactive usage-headroom (advisory; no I/O under this lock). When
        # enabled, warm the per-account snapshot cache for every candidate and
        # apply the claim-time gate for long-running task types. Unknown/stale
        # snapshots are never gated and rank neutrally.
        gate_enabled, gate_threshold, gate_long_types = _usage_headroom_settings(
            runtime_config
        )
        if gate_enabled:
            for a in healthy:
                _maybe_schedule_usage_refresh(a, now)
            for a in probing:
                _maybe_schedule_usage_refresh(a, now)
            if task_type in gate_long_types:
                healthy = _apply_usage_gate(
                    healthy,
                    now=now,
                    threshold_pct=gate_threshold,
                    acct_getter=lambda a: a,
                )
                probing = _apply_usage_gate(
                    probing,
                    now=now,
                    threshold_pct=gate_threshold,
                    acct_getter=lambda a: a,
                )

        if healthy:
            # Exclude decisively-negative-health-score accounts when alternatives exist.
            # This prevents chronically rate-limited accounts from receiving new tasks
            # while still allowing them as last resort if no other account qualifies.
            if len(healthy) > 1:
                qualified = [
                    a
                    for a in healthy
                    if _compute_health_score(a, now)
                    >= _HEALTH_SCORE_DISQUALIFY_THRESHOLD
                ]
                if qualified:
                    healthy = qualified
            # Rank by health score, then 7d success rate (task-type-scoped when
            # confident, aggregate fallback), then latency (faster is better;
            # None → 0.0 so sparse accounts get no penalty). When usage-headroom
            # is enabled, prefer more remaining headroom before the active_tasks
            # tiebreak (advisory; unknown ranks neutrally so unprobed accounts
            # aren't starved). Finally fewest active tasks.
            account = max(
                healthy,
                key=lambda a: (
                    _compute_health_score(a, now),
                    _effective_success_rate(a, task_type, now) or 0.0,
                    -(_compute_avg_duration_24h(a, now) or 0.0),
                    _usage_headroom_rank(a, now) if gate_enabled else 0.0,
                    -a.get("active_tasks", 0),
                ),
            )
        elif probing:
            # Deterministic by id so concurrent selects under the same lock get
            # distinct probes when multiple accounts are eligible.
            account = min(probing, key=lambda a: a["id"])
            logger.info(
                f"[POOL] Probing account {account['name']} for model {model} (cooldown elapsed, serializing as probe)"
            )
        else:
            return None

        account["active_tasks"] = account.get("active_tasks", 0) + 1
        account["daily_task_count"] = int(account.get("daily_task_count") or 0) + 1
        # Per-project counter increment (runtime-only, best-effort).
        if project_id:
            proj_state = account.setdefault("project_state", {}).setdefault(project_id, {})
            proj_state["active_tasks"] = proj_state.get("active_tasks", 0) + 1
            proj_state["daily_task_count"] = proj_state.get("daily_task_count", 0) + 1
        _stage_usage_delta(account, 1, project_id)
        selected = account

    await _maybe_refresh_account_secret(selected)
    return selected


async def select_llm_runtime_for_task(
    user_id: str,
    task: Any,
    runtime_config: Mapping[str, Any],
    blacklist: List[str] | None = None,
    project_id: str | None = None,
    runner_available_clis: dict[str, dict] | None = None,
    failure_reasons: List[str] | None = None,
) -> LLMSelectionResult | None:
    """Resolve the (provider, model, account) runtime for a task.

    Returns None — or an account-less result when routing knows the intended
    provider/model — when no pool account is eligible. In that case the
    per-(provider, model) rejection reasons are logged at WARNING and, when the
    caller passes ``failure_reasons``, copied into it: a computed-but-discarded
    reason is how "pool exhausted" turns into an hour of debugging the wrong
    thing.
    """
    # Did THIS call's caller actually ask for the WHY? Everything expensive
    # about reason-building is gated on this.
    #
    # Per-account reasons are built inside select_max_plan_account{,_balanced}
    # while they hold the global _max_plan_pool_lock. Handing those selectors a
    # non-None reject_reasons list unconditionally would make every LLM
    # selection system-wide — cloud dispatch included — construct a reason
    # string for every rejected account under that lock, with the cost peaking
    # during a cooldown storm. So the sub-lists below are None unless the
    # caller wants them, which turns the selectors' reason work off entirely.
    _want_reasons = failure_reasons is not None

    # Coarse per-(provider, model) attempt lines. Cheap (a handful of f-strings,
    # built OUTSIDE the pool lock) and always collected, so the no-account log
    # line still names which routes were tried even at DEBUG.
    _reasons: List[str] = []

    def _sub_reasons() -> List[str] | None:
        """Fresh per-account reason sink, or None when nobody asked."""
        return [] if _want_reasons else None

    def _note_attempt(
        provider_name: str, model_name: str | None, sub: List[str] | None
    ) -> None:
        detail = "; ".join(sub) if sub else "no eligible accounts"
        _reasons.append(f"{provider_name}/{model_name or 'any'}: {detail}")

    def _flush_reasons(outcome: str) -> None:
        if not _reasons:
            _reasons.append("no provider/model attempts were possible")
        # WARNING only when the caller asked for reasons (i.e. it will surface
        # them to an operator). Otherwise DEBUG: a pool-exhaustion storm makes
        # EVERY dispatch attempt fleet-wide hit this path, and a multi-KB
        # WARNING per attempt is log amplification at the worst possible moment
        # — the incident itself. The caller that wants the WHY (the runner pull
        # path) still gets both the log line and the list.
        log = logger.warning if _want_reasons else logger.debug
        log(
            "[LLM_SELECT] project=%s task_type=%s %s — %s",
            _log_project_id,
            routing_task_type,
            outcome,
            " | ".join(_reasons)[:1500],
        )
        if failure_reasons is not None:
            failure_reasons.extend(_reasons)

    explicit_model = _extract_task_model(task)
    mode = provider_selection_mode(runtime_config)
    allow_fallback = mode in ("balanced", "preferred")
    model_preferences = (
        [] if explicit_model else resolve_task_model_preferences(task, runtime_config)
    )
    provider_preference = resolve_task_provider_preference(task, runtime_config)
    quality_tier = resolve_task_quality_tier(task, runtime_config)
    explicit_provider = get_task_llm_provider(task) or infer_provider_for_model(
        explicit_model, runtime_config
    )
    routing_task_type = _task_type(task)
    capability = get_task_capability(routing_task_type)
    hard_allowed_providers = (
        capability.allowed_providers
        if capability is not None
        else None
    )
    if hard_allowed_providers is not None:
        # `llm_provider` and model preferences can be retained from an earlier
        # claim.  Do not let that durable runtime identity bypass a newly
        # enforced capability boundary on retry or after routing changes.
        if explicit_provider not in hard_allowed_providers:
            explicit_provider = None
            explicit_model = None
        model_preferences = [
            preference
            for preference in model_preferences
            if preference.get("provider") in hard_allowed_providers
        ]
    # Resolve project_id: explicit parameter takes precedence, then task attribute.
    _resolved_project_id = (
        project_id
        if project_id is not None
        else str(getattr(task, "project_id", None) or "")
        or None
    )
    _log_project_id = _resolved_project_id or "unknown"
    logger.info("[LLM_SELECT] project=%s user=%s task_type=%s", _log_project_id, user_id, routing_task_type)

    # If the inferred explicit_provider isn't allowed by the resolved
    # routing, drop the pin so category_routing wins. Catches:
    #   - legacy payload hints (research findings historically embedded
    #     "model": "claude-opus-4-7" which anchored children to anthropic
    #     regardless of user config)
    #   - stale ctx.llm_provider from a prior claim after the operator
    #     changed provider_preference
    # User-explicit pins still work: if they set payload.provider_preference
    # to include anthropic, it stays in the preference list.
    if (
        explicit_provider
        and provider_preference
        and explicit_provider not in provider_preference
    ):
        logger.info(
            f"[LLM_SELECT] Dropping explicit model pin "
            f"(model={explicit_model}, inferred_provider={explicit_provider}) "
            f"— not in routing preference={provider_preference}. "
            f"Falling back to routing-based selection."
        )
        explicit_model = None
        explicit_provider = None
        if model_preferences:
            model_preferences = [
                p for p in model_preferences if p.get("provider") in provider_preference
            ]

    if not allow_fallback:
        if model_preferences:
            model_preferences = model_preferences[:1]
        if provider_preference:
            provider_preference = provider_preference[:1]

    # Reconcile an explicit provider before applying runner capability filters.
    # Otherwise the pin could be inserted after filtering and bypass a failed
    # Resident readiness proof on the claimed runner.
    if explicit_provider:
        if allow_fallback:
            provider_preference = [explicit_provider] + [
                p for p in provider_preference if p != explicit_provider
            ]
        else:
            provider_preference = [explicit_provider]
    elif explicit_model and provider_preference:
        # Unknown/custom model strings must not silently degrade into tier defaults
        # or cross-provider retries. Keep the caller's requested model on the
        # first configured provider only until mappings are updated.
        provider_preference = provider_preference[:1]

    # Filter providers by runner CLI inventory: when a concrete runner is
    # known (runner claim path), remove providers whose required CLI is not
    # present on that runner. The orchestrator pre-provision / warm-retarget
    # paths pass runner_available_clis=None and skip this filter entirely.
    if runner_available_clis is not None:
        require_resident_ready = (
            _task_context(task).get("_resident_session") is True
        )
        provider_preference = _filter_providers_by_cli(
            provider_preference,
            runner_available_clis,
            require_resident_ready=require_resident_ready,
        )
        if model_preferences:
            model_preferences = [
                p
                for p in model_preferences
                if _filter_providers_by_cli(
                    [p.get("provider", "")],
                    runner_available_clis,
                    require_resident_ready=require_resident_ready,
                )
            ]
        if not provider_preference and not model_preferences:
            logger.info(
                "[LLM_SELECT] All providers filtered by runner CLI inventory "
                "— no runtime available for runner project=%s",
                _log_project_id,
            )
            if failure_reasons is not None:
                failure_reasons.append(
                    "all routed providers filtered out by the runner's available "
                    "CLI inventory (runner has no CLI for any allowed provider)"
                )
            return None

    if model_preferences:
        for pref in model_preferences:
            provider_name = pref["provider"]
            model_name = pref["model"]
            sub_reasons = _sub_reasons()
            account = await select_max_plan_account(
                user_id=user_id,
                model=model_name,
                blacklist=blacklist,
                provider=provider_name,
                task_type=routing_task_type,
                runtime_config=runtime_config,
                project_id=_resolved_project_id,
                reject_reasons=sub_reasons,
            )
            if not account:
                _note_attempt(provider_name, model_name, sub_reasons)
            if account:
                logger.info("[LLM_SELECT] project=%s account=%s provider=%s model=%s", _log_project_id, account.get("id"), provider_name, model_name)
                return LLMSelectionResult(
                    provider=provider_name,
                    auth_mode=_normalize_auth_mode(account.get("auth_mode"))
                    or LLMAuthMode.OAUTH.value,
                    model=model_name,
                    quality_tier=infer_quality_tier_for_model(
                        provider_name, model_name, runtime_config
                    ),
                    account=account,
                    settings=_settings_for_selected_model(
                        task, runtime_config, provider_name, model_name
                    ),
                )

    # The intended routing is the most-preferred provider and its tier/explicit
    # model. Captured here (after provider_preference is finalized) so every
    # selection outcome below can be compared against it to detect a fallback.
    intended_provider = (
        provider_preference[0] if provider_preference else explicit_provider
    )
    intended_model = explicit_model
    if intended_model is None and intended_provider:
        intended_settings = resolve_task_provider_settings(
            task, runtime_config, intended_provider
        )
        intended_model = _settings_bag_model(
            intended_settings
        ) or get_provider(intended_provider).tier_to_model(quality_tier, runtime_config)

    # Balanced mode: pool all listed providers' accounts and pick globally by
    # (health, -load). An explicit per-task provider/model still pins the
    # selection (handled above via trimmed provider_preference), so this only
    # kicks in for the common path where no pin is set.
    if mode == "balanced" and len(provider_preference) > 1 and not explicit_model:
        provider_models: list[tuple[str, str | None]] = []
        for provider_name in provider_preference:
            provider = get_provider(provider_name)
            pre_settings = resolve_task_provider_settings(
                task, runtime_config, provider_name
            )
            bag_model = _settings_bag_model(pre_settings)
            if isinstance(bag_model, str) and bag_model:
                model = bag_model
            else:
                model = provider.tier_to_model(quality_tier, runtime_config)
            if not model:
                continue
            provider_models.append((provider_name, model))
        if provider_models:
            balanced_sub = _sub_reasons()
            balanced_result = await select_max_plan_account_balanced(
                user_id=user_id,
                provider_models=provider_models,
                blacklist=blacklist,
                task_type=routing_task_type,
                runtime_config=runtime_config,
                project_id=_resolved_project_id,
                reject_reasons=balanced_sub,
            )
            if balanced_result is None:
                _note_attempt(
                    "balanced[" + ",".join(p for p, _m in provider_models) + "]",
                    None,
                    balanced_sub,
                )
            if balanced_result is not None:
                account, provider_name, chosen_model = balanced_result
                if intended_provider and provider_name != intended_provider:
                    _record_routing_fallback(
                        task,
                        intended_provider=intended_provider,
                        intended_model=intended_model,
                        effective_provider=provider_name,
                        effective_model=chosen_model,
                        reason="cross_provider",
                    )
                return LLMSelectionResult(
                    provider=provider_name,
                    auth_mode=_normalize_auth_mode(account.get("auth_mode"))
                    or LLMAuthMode.OAUTH.value,
                    model=chosen_model,
                    quality_tier=quality_tier,
                    account=account,
                    settings=_settings_for_selected_model(
                        task, runtime_config, provider_name, chosen_model
                    ),
                )
            # Fall through to per-provider iteration so the unaccounted-for
            # fallback (no account present, but explicit_provider/model set)
            # still resolves a synthetic result below.

    for provider_name in provider_preference:
        provider = get_provider(provider_name)
        # Resolve settings bag first so a bag-pinned `model` (e.g. a task-type
        # override forcing gemini-2.5-flash for smoketest) takes precedence
        # over tier resolution. Explicit payload.model still wins over both.
        pre_settings = resolve_task_provider_settings(
            task, runtime_config, provider_name
        )
        bag_model = _settings_bag_model(pre_settings)
        if explicit_model and (
            explicit_provider is None or provider_name == explicit_provider
        ):
            model = explicit_model
        elif isinstance(bag_model, str) and bag_model:
            model = bag_model
        else:
            model = provider.tier_to_model(quality_tier, runtime_config)
        if not model:
            continue
        sub_reasons = _sub_reasons()
        account = await select_max_plan_account(
            user_id=user_id,
            model=model,
            blacklist=blacklist,
            provider=provider_name,
            task_type=routing_task_type,
            runtime_config=runtime_config,
            project_id=_resolved_project_id,
            reject_reasons=sub_reasons,
        )
        if not account:
            _note_attempt(provider_name, model, sub_reasons)
        if account:
            if intended_provider and provider_name != intended_provider:
                _record_routing_fallback(
                    task,
                    intended_provider=intended_provider,
                    intended_model=intended_model,
                    effective_provider=provider_name,
                    effective_model=model,
                    reason="cross_provider",
                )
            return LLMSelectionResult(
                provider=provider_name,
                auth_mode=_normalize_auth_mode(account.get("auth_mode"))
                or LLMAuthMode.OAUTH.value,
                model=model,
                quality_tier=quality_tier,
                account=account,
                settings=_settings_for_selected_model(
                    task, runtime_config, provider_name, model
                ),
            )

        # Preview→GA fallback: if the requested model has a GA counterpart
        # (e.g. gemini-3.1-pro-preview → gemini-2.5-pro) and no account had
        # preview access for the requested one, try the counterpart before
        # moving on to the next provider. Recorded on task.context so the
        # admin UI / logs surface the downgrade explicitly — silently
        # running a different tier than the user asked for is a trust-losing
        # failure mode.
        fallback = provider.fallback_model(model)
        if fallback and fallback != model:
            sub_reasons = _sub_reasons()
            account = await select_max_plan_account(
                user_id=user_id,
                model=fallback,
                blacklist=blacklist,
                provider=provider_name,
                task_type=routing_task_type,
                runtime_config=runtime_config,
                project_id=_resolved_project_id,
                reject_reasons=sub_reasons,
            )
            if not account:
                _note_attempt(provider_name, fallback, sub_reasons)
            if account:
                logger.warning(
                    "[LLM_SELECT] project=%s No pool account has %s; falling back to GA "
                    "counterpart %s on %s.",
                    _log_project_id,
                    model,
                    fallback,
                    provider_name,
                )
                _record_model_fallback(
                    task,
                    requested=model,
                    effective=fallback,
                    provider_name=provider_name,
                    reason="no_account_with_preview_access",
                )
                return LLMSelectionResult(
                    provider=provider_name,
                    auth_mode=_normalize_auth_mode(account.get("auth_mode"))
                    or LLMAuthMode.OAUTH.value,
                    model=fallback,
                    quality_tier=quality_tier,
                    account=account,
                    settings=_settings_for_selected_model(
                        task, runtime_config, provider_name, fallback
                    ),
                )

    if explicit_provider and explicit_model:
        _flush_reasons("no pool account (returning account-less explicit result)")
        _record_routing_fallback(
            task,
            intended_provider=intended_provider,
            intended_model=intended_model,
            effective_provider=explicit_provider,
            effective_model=explicit_model,
            reason="no_account",
        )
        return LLMSelectionResult(
            provider=explicit_provider,
            auth_mode=get_task_llm_auth_mode(task) or LLMAuthMode.OAUTH.value,
            model=explicit_model,
            quality_tier=quality_tier,
            account=None,
            settings=_settings_for_selected_model(
                task, runtime_config, explicit_provider, explicit_model
            ),
        )

    if provider_preference:
        _flush_reasons("no pool account (returning account-less fallback result)")
        fallback_provider = provider_preference[0]
        provider = get_provider(fallback_provider)
        fallback_settings = resolve_task_provider_settings(
            task, runtime_config, fallback_provider
        )
        fallback_model = (
            explicit_model
            or _settings_bag_model(fallback_settings)
            or provider.tier_to_model(quality_tier, runtime_config)
        )
        fallback_settings = _settings_for_selected_model(
            task, runtime_config, fallback_provider, fallback_model
        )
        fallback_auth_mode = (
            LLMAuthMode.API_KEY.value
            if set(provider.supported_auth_modes) == {LLMAuthMode.API_KEY}
            else LLMAuthMode.OAUTH.value
        )
        _record_routing_fallback(
            task,
            intended_provider=intended_provider,
            intended_model=intended_model,
            effective_provider=fallback_provider,
            effective_model=fallback_model,
            reason="no_account",
        )
        return LLMSelectionResult(
            provider=fallback_provider,
            auth_mode=fallback_auth_mode,
            model=fallback_model,
            quality_tier=quality_tier,
            account=None,
            settings=fallback_settings,
        )

    _flush_reasons("no pool account and no routing to fall back on")
    return None


async def _maybe_refresh_account_secret(acct: Dict[str, Any]) -> None:
    """Re-fetch runtime_secret from Secret Manager when it has been cleared.

    Called after selecting an account, outside the pool lock, so I/O does not
    block other pool operations.  Only acts when runtime_secret is absent and
    secret_blob_ref is a Secret Manager path (starts with "projects/").
    """
    if acct.get("runtime_secret"):
        return
    secret_ref = acct.get("secret_blob_ref")
    if not secret_ref or not str(secret_ref).startswith("projects/"):
        return
    try:
        from app.secrets_manager import get_user_secret

        resolved = await get_user_secret(secret_ref)
        if resolved:
            acct["runtime_secret"] = resolved
            provider = (
                _normalize_provider_name(acct.get("provider"))
                or LLMProviderName.ANTHROPIC.value
            )
            auth_mode = (
                _normalize_auth_mode(acct.get("auth_mode")) or LLMAuthMode.OAUTH.value
            )
            if (
                provider == LLMProviderName.ANTHROPIC.value
                and auth_mode == LLMAuthMode.OAUTH.value
            ):
                acct["oauth_token"] = resolved
            logger.debug(
                f"[POOL] Refreshed runtime_secret for account {acct.get('name')} ({acct.get('id')})"
            )
    except Exception:
        logger.exception(
            f"[POOL] Failed to refresh secret from Secret Manager for account {acct.get('name')}"
        )


async def release_max_plan_account(
    account_id: str,
    *,
    revert_daily_task_count: bool = False,
    project_id: str | None = None,
):
    """Decrement active tasks for an account when a task finishes or aborts.

    When *project_id* is provided, per-project active_tasks are also
    decremented (best-effort, floored at zero).  When active_tasks drops to zero
    and the account's credential is backed by Secret Manager (secret_blob_ref
    starts with "projects/"), runtime_secret is cleared from the in-memory dict
    so the resolved credential does not linger indefinitely.  The next
    select_max_plan_account call re-fetches it.
    """
    async with _max_plan_pool_lock:
        account = _account_by_id.get(account_id)
        if account:
            if account["active_tasks"] <= 0:
                logger.warning(
                    f"[POOL] Account {account['name']} ({account_id}) release underflow — active_tasks was {account['active_tasks']}"
                )
            account["active_tasks"] = max(0, account["active_tasks"] - 1)
            # Per-project active_tasks release (best-effort).
            if project_id:
                proj_state = account.setdefault("project_state", {}).setdefault(project_id, {})
                current = proj_state.get("active_tasks", 0)
                if current > 0:
                    proj_state["active_tasks"] = current - 1
                elif current <= 0 and account.get("active_tasks", 0) >= 0:
                    # Non-zero underflow on per-project is unusual but not
                    # actionable — log and floor.
                    logger.debug(
                        f"[POOL] Per-project release underflow for project {project_id} "
                        f"on account {account['name']} ({account_id}) — was {current}"
                    )
            if revert_daily_task_count:
                account["daily_task_count"] = max(
                    0, int(account.get("daily_task_count") or 0) - 1
                )
                # Per-project daily_task_count revert (best-effort).
                if project_id:
                    proj_state = account.setdefault("project_state", {}).setdefault(project_id, {})
                    current_daily = proj_state.get("daily_task_count", 0)
                    if current_daily > 0:
                        proj_state["daily_task_count"] = current_daily - 1
                _stage_usage_delta(account, -1, project_id)
            if account["active_tasks"] == 0:
                secret_ref = account.get("secret_blob_ref")
                if secret_ref and str(secret_ref).startswith("projects/"):
                    account.pop("runtime_secret", None)


async def reserve_max_plan_account(
    account_id: str,
    *,
    user_id: str | None = None,
    project_id: str | None = None,
    model: str | None = None,
    blacklist: List[str] | None = None,
    count_daily_task: bool = False,
) -> bool:
    """Atomically gate and reserve a known account for warm task reuse.

    Mirror of ``release_max_plan_account``: a warm-reuse claim runs a task on
    the VM's boot LLM account but never goes through ``select_max_plan_account``
    (the normal cap gate and counter acquisition). This exact-account variant
    applies the same blacklist, health/cooldown, project-cap, and global-cap
    primitives under the same pool lock before incrementing counters.

    ``count_daily_task=True`` makes a fresh warm role equivalent to an ordinary
    selected task: global and project daily counters advance together with the
    concurrency counters. An abandoned claim must then call
    ``release_max_plan_account(..., revert_daily_task_count=True)``.

    No-op (returns False) when the account is absent from the pool (e.g. local /
    test VMs with no real boot account) or is not currently eligible. Acquires
    ``_max_plan_pool_lock``.
    """
    if not account_id:
        return False
    async with _max_plan_pool_lock:
        account = _account_by_id.get(str(account_id))
        if account is None:
            return False
        if user_id is not None and not any(
            str(candidate.get("id")) == str(account_id)
            for candidate in _max_plan_pools.get(user_id, [])
        ):
            return False
        if blacklist and str(account_id) in blacklist:
            return False
        now = _utcnow()
        if _daily_reset_if_elapsed(account, now):
            _pending_cap_persists.add(str(account_id))
        if _project_at_cap(account, project_id) or _account_at_cap(account):
            return False
        state = _account_state_for_model(account, model, now)
        if state != "healthy" and not (
            state == "probing" and int(account.get("active_tasks") or 0) == 0
        ):
            return False
        account["active_tasks"] = int(account.get("active_tasks") or 0) + 1
        if count_daily_task:
            account["daily_task_count"] = (
                int(account.get("daily_task_count") or 0) + 1
            )
        if project_id:
            project_state = account.setdefault("project_state", {}).setdefault(
                project_id, {}
            )
            project_state["active_tasks"] = (
                int(project_state.get("active_tasks") or 0) + 1
            )
            if count_daily_task:
                project_state["daily_task_count"] = (
                    int(project_state.get("daily_task_count") or 0) + 1
                )
        if count_daily_task:
            _stage_usage_delta(account, 1, project_id)
        return True


async def reserve_max_plan_auxiliary_slot(account_id: str, *, project_id: str) -> bool:
    """Reserve one non-Task Resident model-process slot without task quota use.

    The caller supplies the already-assigned Workday account.  Reusing that
    account is permitted only when both its global and project concurrent caps
    have room for the additional model process.  Unlike task selection this
    path intentionally skips daily caps/counters.
    """
    if not account_id or not project_id:
        return False
    async with _max_plan_pool_lock:
        account = _account_by_id.get(str(account_id))
        if account is None:
            return False
        concurrent_cap = account.get("max_concurrent_tasks")
        if concurrent_cap is not None and int(account.get("active_tasks") or 0) >= int(concurrent_cap):
            return False
        project_cap = (account.get("project_max_concurrent_tasks") or {}).get(project_id)
        project_state = account.setdefault("project_state", {}).setdefault(project_id, {})
        if project_cap is not None and int(project_state.get("active_tasks") or 0) >= int(project_cap):
            return False
        account["active_tasks"] = int(account.get("active_tasks") or 0) + 1
        project_state["active_tasks"] = int(project_state.get("active_tasks") or 0) + 1
        return True


async def release_max_plan_auxiliary_slot(account_id: str | None, *, project_id: str | None) -> None:
    """Idempotently release a non-Task Resident slot; daily task use is untouched."""
    if not account_id:
        return
    async with _max_plan_pool_lock:
        account = _account_by_id.get(str(account_id))
        if account is None:
            return
        account["active_tasks"] = max(0, int(account.get("active_tasks") or 0) - 1)
        if project_id:
            project_state = account.setdefault("project_state", {}).setdefault(project_id, {})
            project_state["active_tasks"] = max(0, int(project_state.get("active_tasks") or 0) - 1)


async def reserve_max_plan_call_slot(account_id: str, *, project_id: str) -> bool:
    """Compatibility wrapper for the bounded call Fast-Resident slot."""
    return await reserve_max_plan_auxiliary_slot(account_id, project_id=project_id)


async def release_max_plan_call_slot(account_id: str | None, *, project_id: str | None) -> None:
    """Compatibility wrapper for the bounded call Fast-Resident slot."""
    await release_max_plan_auxiliary_slot(account_id, project_id=project_id)


def build_account_retarget_manifest(
    account_id: str | None,
    *,
    provider: str | None = None,
    auth_mode: str | None = None,
    model: str | None = None,
    task: Any = None,
    runtime_config: Mapping[str, Any] | None = None,
) -> dict | None:
    """Build a credential re-target secret manifest for ``account_id``.

    Resolves live account credentials and optional task provider settings.
    Returns None for unknown accounts or unavailable live credentials so the
    caller aborts rather than reexecing a VM onto a guaranteed-bad runtime.
    """
    if not account_id:
        return None
    acct = _account_by_id.get(str(account_id))
    if acct is None:
        return None
    live_secret = acct.get("runtime_secret") or acct.get("oauth_token")
    if not isinstance(live_secret, str) or not live_secret.strip():
        logger.warning(
            "[WARM-POOL] Cannot build re-target manifest for account=%s: "
            "live credential unavailable",
            account_id,
        )
        return None
    resolved_provider = provider or acct.get("provider")
    resolved_auth_mode = auth_mode or acct.get("auth_mode")
    settings: dict[str, Any] | None = None
    if task is not None and runtime_config is not None and resolved_provider:
        try:
            settings = resolve_task_provider_settings(
                task, runtime_config, resolved_provider
            )
        except Exception:
            # Settings are best-effort; a bad bag must not block the
            # credential re-target (the agent falls back to provider
            # defaults). Loud failure here would strand the warm claim.
            logger.exception(
                "[WARM-POOL] provider-settings resolve failed for re-target "
                "account=%s provider=%s; emitting manifest without settings",
                account_id,
                resolved_provider,
            )
            settings = None
    from app.compute_manifest import build_warm_claim_retarget_manifest

    return build_warm_claim_retarget_manifest(
        provider=resolved_provider,
        auth_mode=resolved_auth_mode,
        model=model,
        oauth_token=live_secret,
        claude_config_json=acct.get("claude_config_json"),
        provider_metadata=acct.get("provider_metadata") or {},
        provider_settings=settings,
    )


async def disable_account_model(
    acct_id: str,
    model: str,
    *,
    retry_after_seconds: int | None = None,
    kind: str = "rate_limit",
    regression_stats: dict | None = None,
    provider: str | None = None,
    task_type: str | None = None,
    allow_unknown_model_identity: bool = False,
) -> None:
    """Mark an account's cascade of models as rate-limited and persist.

    Called from both agent_status.py (when the agent POSTs a RATE_LIMIT failure)
    and reaper.py (when the agent's pre-flight dies without POSTing). Both paths
    must update disabled_models so the orchestrator's blacklist pruning
    preserves the entry on the next dispatch cycle.

    - `retry_after_seconds`: server-provided retry floor parsed from the error
      body (Gemini's `Please retry in 39s`, OpenAI's `try again after 60s`).
      When present, cooldown is `max(parsed_hint_minutes, exponential)` so a
      tighter server hint never shortens our backoff below the computed value,
      and a long hint is respected.
    - `kind="quota_exhausted"`: billing wall (insufficient_quota, out-of-credits).
      Uses a flat 24h cooldown instead of exponential — these don't resolve on
      clock, they need operator action (refill credits / raise budget).
    - `kind="regression"`: per-(provider, model) success-rate regression
      detected by scan_account_model_regressions. Disables ONLY the specific
      model (no Opus→Sonnet→Haiku cascade — regression is per-(provider, model)
      and the rate-limit cascade does not apply); confidence-gated quarantine
      trip-wire is also bypassed. Cooldown is a flat 6h reprobe. When provided,
      ``regression_stats`` (account_rate, project_median, sample_size,
      distinct_accounts, started_at) is persisted on the entry so operators
      can see why the model was quarantined.

    The account provider, explicit provider (when supplied), and inferred model
    provider must agree before any cascade is written. Provider is persisted on
    every entry, including ordinary rate limits/quarantines, so readers can
    validate the cooldown identity instead of trusting a model-only key.

    Acquires _max_plan_pool_lock. On DB persist failure, appends to
    _pending_rl_persists for retry (same contract as clear_rate_limit_state_on_success).
    """
    if not acct_id or not model:
        return
    async with _max_plan_pool_lock:
        acct = _account_by_id.get(acct_id)
        if acct is None:
            return
        account_provider = (
            _normalize_provider_name(acct.get("provider"))
            or LLMProviderName.ANTHROPIC.value
        )
        explicit_provider = _normalize_provider_name(provider)
        model_provider = infer_provider_for_model(model, _runtime_config_snapshot())
        if model_provider is None:
            model_lower = model.lower()
            if model_lower.startswith("claude-"):
                model_provider = LLMProviderName.ANTHROPIC.value
            elif model_lower.startswith(("gpt-", "codex-", "o1", "o3", "o4")):
                model_provider = LLMProviderName.OPENAI.value
            elif model_lower.startswith("gemini-"):
                model_provider = LLMProviderName.GEMINI.value
            elif model_lower.startswith("deepseek-"):
                model_provider = LLMProviderName.DEEPSEEK.value
            elif model_lower.startswith("glm-"):
                model_provider = LLMProviderName.ZAI.value
        if model_provider is None and not allow_unknown_model_identity:
            logger.error(
                "[POOL] Rejected cooldown mutation with unknown model identity "
                "account=%s account_provider=%s explicit_provider=%s model=%s",
                acct_id,
                account_provider,
                explicit_provider,
                model,
            )
            return
        if (
            (explicit_provider and explicit_provider != account_provider)
            or (model_provider and model_provider != account_provider)
            or (
                explicit_provider
                and model_provider
                and explicit_provider != model_provider
            )
        ):
            logger.error(
                "[POOL] Rejected cross-provider cooldown mutation account=%s "
                "account_provider=%s explicit_provider=%s model=%s model_provider=%s",
                acct_id,
                account_provider,
                explicit_provider,
                model,
                model_provider,
            )
            return
        cooldown_provider = explicit_provider or model_provider or account_provider

        # Normalize Haiku → Opus so the meter is keyed correctly. Regression is
        # per-(provider, model) and operates on the actual model name — skip the
        # Haiku→Opus normalization and cascade for regression entries.
        if kind != "regression":
            model = _normalize_rl_model(model)
            cascade = cascade_disabled_models(model)
        else:
            cascade = (model,)
        dm = acct.setdefault("disabled_models", {})
        now = _utcnow()
        for m in cascade:
            info = dm.setdefault(m, {"hits": 0})
            info["hits"] += 1
            info["at"] = now
            info["provider"] = cooldown_provider
            if kind == "quota_exhausted":
                info["kind"] = "quota_exhausted"
            elif kind == "regression":
                info["kind"] = "regression"
                if regression_stats is not None:
                    info["regression_stats"] = dict(regression_stats)
            if retry_after_seconds is not None:
                info["retry_after_seconds"] = int(retry_after_seconds)
        # Regression quarantine is driven by aggregated success-rate trends, not
        # by an individual rate-limit/provision event — skip rl_events / health
        # bookkeeping, the auto-disable counter, and the confidence-gated
        # quarantine trip-wire (those are scoped to per-event failure signals).
        if kind == "regression":
            primary_key = cascade[0]
            logger.warning(
                f"[POOL] Account {acct['name']} regression-quarantined on model {primary_key} "
                f"(flat {_REGRESSION_COOLDOWN_SECONDS // 60}min reprobe cooldown)"
            )
            _stage_rate_limit_mutation(
                acct,
                {
                    "op": "disable_models",
                    "models": {
                        m: _serialize_disabled_model_info(dm[m]) for m in cascade
                    },
                },
            )
            await _persist_rate_limit_state_best_effort(acct_id, "regression state")
            return
        # Record rate-limit event for rolling health score + success-rate tracking
        _prune_health_events(acct, now)
        acct.setdefault("rl_events", []).append(now)
        _prune_outcome_events(acct, now)
        acct.setdefault("outcome_events", []).append({"at": now, "success": False, "task_type": task_type})
        # Legacy model names (e.g. claude-sonnet-4-6) may not match any cascade
        # entry directly, so key off the cascade primary we just wrote. Guaranteed
        # non-empty: cascade_disabled_models always returns at least one entry.
        primary_key = cascade[0]
        primary_hits = dm[primary_key]["hits"]
        if kind == "quota_exhausted":
            # Flat 24h — billing walls don't resolve on clock. Operator action
            # (refill credits / raise budget) is required; a short cooldown
            # would just cycle the same error.
            cooldown = 24 * 60
            logger.warning(
                f"[POOL] Account {acct['name']} QUOTA EXHAUSTED on models {list(cascade)} "
                f"(primary {primary_key}, flat 24h cooldown — refill required)"
            )
        else:
            cooldown = min(30 * (2 ** (primary_hits - 1)), 240) * random.uniform(
                0.85, 1.15
            )
            if retry_after_seconds is not None:
                hint_minutes = retry_after_seconds / 60.0
                if hint_minutes > cooldown:
                    cooldown = hint_minutes
            logger.warning(
                f"[POOL] Account {acct['name']} disabled models {list(cascade)} "
                f"(primary {primary_key} hit #{primary_hits}, cooldown {cooldown:.0f}min"
                + (
                    f", server-hinted {retry_after_seconds}s"
                    if retry_after_seconds
                    else ""
                )
                + ")"
            )

        auto_disable_threshold = acct.get("auto_disable_after_rate_limits")
        if auto_disable_threshold is not None and primary_hits >= int(
            auto_disable_threshold
        ):
            reason = f"auto-disabled after {primary_hits} rate-limit hit(s) (threshold {auto_disable_threshold})"
            acct["enabled"] = False
            acct["disabled_at"] = now
            acct["disabled_reason"] = reason
            _stage_rate_limit_mutation(
                acct,
                {
                    "op": "disable_account",
                    "disabled_at": now.isoformat(),
                    "reason": reason,
                },
            )
            logger.warning(f"[POOL] Account {acct['name']} auto-disabled: {reason}")

        if kind != "quota_exhausted" and _check_quarantine_trigger(acct_id):
            for m in cascade:
                dm[m]["kind"] = "quarantine"
            logger.warning(
                f"[POOL] Account {acct['name']} quarantine triggered: last {QUARANTINE_CONSECUTIVE_N} "
                f"high-confidence failures — {_QUARANTINE_COOLDOWN_SECONDS // 60}min recheck cooldown"
            )

        _stage_rate_limit_mutation(
            acct,
            {
                "op": "disable_models",
                "models": {
                    m: _serialize_disabled_model_info(dm[m]) for m in cascade
                },
                "outcome_event": {
                    "at": now.isoformat(),
                    "success": False,
                    **({"task_type": task_type} if task_type else {}),
                },
            },
        )
        await _persist_rate_limit_state_best_effort(acct_id, "rate-limit state")


async def disable_account_for_auth(acct_id: str, reason: str) -> bool:
    """Hard-disable an account after a NON-recoverable auth failure.

    Unlike a rate-limit cooldown (disable_account_model), this does NOT
    auto-recover: the failure (e.g. Anthropic ``oauth_org_not_allowed`` — the
    org has disabled OAuth for Claude Code, or expired/revoked credentials)
    persists until an operator re-authenticates / re-enables OAuth and manually
    re-enables the account. We leave ``disabled_at`` unset so
    recheck_disabled_accounts() skips it (its cooldown re-enable keys on
    disabled_at), keeping the account out of selection indefinitely. Persisting
    ``enabled=False`` is what stops further tasks from selecting it and burning
    VM hours on a credential that will only 4xx again.

    Returns True if the account was newly disabled, False if it was already
    disabled or unknown. Acquires _max_plan_pool_lock.
    """
    if not acct_id:
        return False
    async with _max_plan_pool_lock:
        acct = _account_by_id.get(acct_id)
        if acct is None or not acct.get("enabled", True):
            return False
        acct["enabled"] = False
        # Intentionally NOT setting disabled_at — that flag drives the
        # cooldown-based auto-re-enable in recheck_disabled_accounts(), which
        # must not fire for a non-recoverable auth failure.
        acct["disabled_at"] = None
        acct["disabled_reason"] = reason
        _stage_rate_limit_mutation(
            acct,
            {"op": "disable_account", "disabled_at": None, "reason": reason},
        )
        logger.warning(
            f"[POOL] Account {acct['name']} HARD-DISABLED (non-recoverable auth): {reason}"
        )
        await _persist_rate_limit_state_best_effort(acct_id, "auth-disable")
        return True


async def record_account_startup_failure(acct_id: str, zone: str) -> None:
    """Record an agent-startup failure (VM created, agent never claimed) for an account.

    Tracks rolling startup failure events per account. When an account accumulates
    >= _STARTUP_FAILURE_THRESHOLD failures across >= _STARTUP_FAILURE_MIN_ZONES distinct
    zones within _STARTUP_FAILURE_WINDOW_SECONDS, a flat cooldown is set.

    The multi-zone requirement is the key conservative guard: failures in a single
    zone blame the zone (not the account), while multi-zone failures with a single
    account are account-side. Callers must NOT call this when is_infra=True (global
    outage) — that distinction belongs to the reaper.
    """
    async with _max_plan_pool_lock:
        acct = _account_by_id.get(acct_id)
        if acct is None:
            return

        now = _utcnow()

        # Record this failure event and prune to the rolling window
        events = acct.setdefault("startup_failure_events", [])
        events.append({"at": now, "zone": zone})
        cutoff = now - timedelta(seconds=_STARTUP_FAILURE_WINDOW_SECONDS)
        acct["startup_failure_events"] = [e for e in events if e["at"] > cutoff]
        events = acct["startup_failure_events"]

        # Already in cooldown — record the event but don't reset the timer
        sf_cooldown_at = acct.get("startup_failure_cooldown_at")
        if (
            sf_cooldown_at
            and (now - sf_cooldown_at).total_seconds()
            < _STARTUP_FAILURE_COOLDOWN_SECONDS
        ):
            return

        # Not enough failures to consider tripping
        if len(events) < _STARTUP_FAILURE_THRESHOLD:
            return

        # Require the multi-zone signature: single-zone failures blame the zone
        zones = {e["zone"] for e in events}
        if len(zones) < _STARTUP_FAILURE_MIN_ZONES:
            return

        # Trip the breaker — flat cooldown, visible in UI via disabled_reason
        acct["startup_failure_cooldown_at"] = now
        reason = (
            f"startup-failure cooldown: {len(events)} agent-startup failure(s) in "
            f"{_STARTUP_FAILURE_WINDOW_SECONDS // 60}min across zones "
            f"{', '.join(sorted(zones))}"
        )
        acct["disabled_reason"] = reason
        logger.warning(
            f"[POOL] Account {acct['name']} startup-failure breaker tripped — "
            f"{len(events)} failures across {len(zones)} zone(s) "
            f"({_STARTUP_FAILURE_COOLDOWN_SECONDS // 60}min cooldown): {', '.join(sorted(zones))}"
        )
        _stage_rate_limit_mutation(
            acct,
            {
                "op": "set_startup_cooldown",
                "at": now.isoformat(),
                "reason": reason,
            },
        )
        await _persist_rate_limit_state_best_effort(acct_id, "startup-failure cooldown")


async def clear_rate_limit_state_on_success(
    acct_id: str,
    model: str,
    *,
    provider: str | None = None,
    success_at: datetime | None = None,
) -> bool:
    """Clear cascade-scoped disabled_models entries after a successful task.

    Cascade: Opus success clears (opus, sonnet, haiku); Sonnet success clears
    (sonnet, haiku); Haiku success clears (haiku,). A successful task proves
    the shared quota is fine for that model and everything it cascades into,
    but does NOT prove the higher tiers are fine (Sonnet success says nothing
    about Opus's stricter sub-bucket).

    Returns True if any entry was removed. Acquires _max_plan_pool_lock.
    On DB persist failure, appends to _pending_rl_persists for retry."""
    if not acct_id or not model:
        return False
    # Normalize Haiku → Opus so clearing Haiku's bucket clears the shared meter.
    model = _normalize_rl_model(model)
    cascade = cascade_disabled_models(model)
    success_at = success_at or _utcnow()
    async with _max_plan_pool_lock:
        acct = _account_by_id.get(acct_id)
        if acct is None:
            return False
        dm = acct.get("disabled_models") or {}
        clear_conditions: dict[str, dict] = {}
        for m in cascade:
            info = dm.get(m)
            if info is not None:
                observed_provider = (
                    _normalize_provider_name(provider)
                    or info.get("provider")
                    or _normalize_provider_name(acct.get("provider"))
                    or LLMProviderName.ANTHROPIC.value
                )
                clear_conditions[m] = {
                    "observed_at": info["at"].isoformat(),
                    "provider": observed_provider,
                    "clear_at": success_at.isoformat(),
                }
                del dm[m]
        if not clear_conditions:
            return False
        if not dm:
            acct.pop("disabled_models", None)
        logger.info(
            f"[POOL] Account {acct['name']} clearing disabled models {list(clear_conditions)} after successful {model} task"
        )
        _stage_rate_limit_mutation(
            acct, {"op": "clear_models", "models": clear_conditions}
        )
        await _persist_rate_limit_state_best_effort(acct_id, "rate-limit clear")
        return all(
            m not in (acct.get("disabled_models") or {}) for m in clear_conditions
        )


async def clear_account_model_regression(
    acct_id: str, model: str, provider: str | None = None
) -> bool:
    """Manually clear a regression-kind disabled_models entry for (account, model).

    Returns True iff an entry existed AND its kind was 'regression' (untouched
    otherwise). When ``provider`` is supplied and the stored entry also carries
    a provider tag, the two must match — a mismatch returns False so operators
    get a hard rejection rather than silently clearing the wrong entry.

    This is the manual override behind the recovery endpoint —
    rate_limit / quota_exhausted / quarantine entries have their own recovery
    semantics and must not be cleared here. The 6h time-based reprobe is the
    automatic recovery; this function is for operators who want to re-enable
    a (provider, model) before the cooldown elapses.

    Acquires _max_plan_pool_lock. On DB persist failure, appends to
    _pending_rl_persists for retry.
    """
    if not acct_id or not model:
        return False
    async with _max_plan_pool_lock:
        acct = _account_by_id.get(acct_id)
        if acct is None:
            return False
        dm = acct.get("disabled_models") or {}
        info = dm.get(model)
        if info is None or info.get("kind") != "regression":
            return False
        # Validate provider when both sides have it — reject mismatches so
        # operators can't silently clear an entry for a different provider.
        stored_provider = info.get("provider")
        if provider and stored_provider and stored_provider != provider:
            return False
        observed_at = info["at"].isoformat()
        observed_provider = (
            provider
            or stored_provider
            or _normalize_provider_name(acct.get("provider"))
            or LLMProviderName.ANTHROPIC.value
        )
        del dm[model]
        if not dm:
            acct.pop("disabled_models", None)
        logger.info(
            f"[POOL] Account {acct['name']} cleared regression-quarantine on {model} (manual override)"
        )
        _stage_rate_limit_mutation(
            acct,
            {
                "op": "clear_models",
                "models": {
                    model: {
                        "observed_at": observed_at,
                        "provider": observed_provider,
                        "clear_at": _utcnow().isoformat(),
                    }
                },
                "required_kind": "regression",
            },
        )
        await _persist_rate_limit_state_best_effort(acct_id, "regression clear")
        return model not in (acct.get("disabled_models") or {})


async def clear_regression_on_success(
    acct_id: str,
    model: str,
    *,
    provider: str | None = None,
    success_at: datetime | None = None,
) -> dict | None:
    """Clear a regression-kind disabled_models entry after a successful probe task.

    Returns the cleared entry's metadata dict (account_id, account_label,
    user_id, provider, model, and regression_stats fields) if cleared, else None.
    No cascade: regression entries are keyed under the literal model name (not
    normalized), so only the exact model key is examined and removed.

    Acquires _max_plan_pool_lock. On DB persist failure appends to
    _pending_rl_persists for retry (same contract as clear_rate_limit_state_on_success).
    """
    if not acct_id or not model:
        return None
    async with _max_plan_pool_lock:
        acct = _account_by_id.get(acct_id)
        if acct is None:
            return None
        dm = acct.get("disabled_models") or {}
        info = dm.get(model)
        if info is None or info.get("kind") != "regression":
            return None
        success_at = success_at or _utcnow()
        observed_at = info["at"].isoformat()
        observed_provider = (
            provider
            or info.get("provider")
            or _normalize_provider_name(acct.get("provider"))
            or LLMProviderName.ANTHROPIC.value
        )
        stats = info.get("regression_stats") or {}
        recovery_info = {
            "account_id": acct_id,
            "account_label": acct.get("name") or acct_id,
            "user_id": acct.get("user_id"),
            "provider": info.get("provider"),
            "model": model,
            "account_rate": stats.get("account_rate"),
            "project_median": stats.get("project_median"),
            "sample_size": stats.get("sample_size"),
            "distinct_accounts": stats.get("distinct_accounts"),
            "started_at": stats.get("started_at"),
        }
        del dm[model]
        if not dm:
            acct.pop("disabled_models", None)
        logger.info(
            f"[POOL] Account {acct['name']} auto-recovered regression on {model} after probe success"
        )
        _stage_rate_limit_mutation(
            acct,
            {
                "op": "clear_models",
                "models": {
                    model: {
                        "observed_at": observed_at,
                        "provider": observed_provider,
                        "clear_at": success_at.isoformat(),
                    }
                },
                "required_kind": "regression",
            },
        )
        await _persist_rate_limit_state_best_effort(acct_id, "regression recovery")
        if model in (acct.get("disabled_models") or {}):
            return None
        return recovery_info


async def scan_account_model_regressions(session) -> list[dict[str, Any]]:
    """Periodic scan: detect (account, provider, model) regressions vs project
    median, write disabled_models entries with kind='regression', and return
    NEWLY-fired events for callers to dispatch notifications.

    Idempotency: if an account already has a regression entry on the same
    (provider, model) and that entry's cooldown has not expired, do nothing —
    the lock-held read-modify-write keeps the dedupe atomic so concurrent
    ticks cannot double-fire.

    Each newly-fired event dict carries enough payload (account_id,
    account_label, user_id, provider, model, account_rate, project_median,
    sample_size, distinct_accounts, started_at) for the notification layer
    to fan out to the user's projects.

    Tolerant on errors: a DB failure inside the regression query is logged
    and returns an empty list rather than poisoning the calling reaper loop.
    """
    from app.account_model_regression import find_account_model_regressions

    async with _max_plan_pool_lock:
        account_ids = list(_account_by_id.keys())
    if not account_ids:
        return []

    try:
        candidates = await find_account_model_regressions(session, account_ids)
    except Exception as e:
        logger.warning(f"[POOL] regression scan query failed: {e}")
        return []
    if not candidates:
        return []

    fired: list[dict[str, Any]] = []
    now = _utcnow()
    for cand in candidates:
        acct_id = cand.account_id
        if not acct_id:
            continue
        async with _max_plan_pool_lock:
            acct = _account_by_id.get(acct_id)
            if acct is None:
                continue
            dm = acct.get("disabled_models") or {}
            existing = dm.get(cand.model)
            if existing is not None and existing.get("kind") == "regression":
                # Already quarantined for this (provider, model). Re-fire only if
                # the prior cooldown has elapsed AND the regression still holds —
                # find_account_model_regressions only returns currently-failing
                # cells, so the cooldown check alone is the dedupe boundary.
                at_dt = existing.get("at")
                if at_dt is not None:
                    cooldown = _cooldown_seconds_for_info(existing)
                    if (now - at_dt).total_seconds() < cooldown:
                        continue
            user_id = acct.get("user_id")
            account_label = acct.get("name") or acct_id

        stats = {
            "account_rate": cand.account_rate,
            "project_median": cand.project_median,
            "sample_size": cand.sample_size,
            "distinct_accounts": cand.distinct_accounts,
            "started_at": now.isoformat(),
        }
        try:
            await disable_account_model(
                acct_id,
                cand.model,
                kind="regression",
                regression_stats=stats,
                provider=cand.provider,
            )
        except Exception as e:
            logger.warning(
                f"[POOL] Failed to apply regression quarantine for {acct_id}/{cand.model}: {e}"
            )
            continue
        fired.append(
            {
                "account_id": acct_id,
                "account_label": account_label,
                "user_id": user_id,
                "provider": cand.provider,
                "model": cand.model,
                "account_rate": cand.account_rate,
                "project_median": cand.project_median,
                "sample_size": cand.sample_size,
                "distinct_accounts": cand.distinct_accounts,
                "started_at": stats["started_at"],
            }
        )
    return fired


def _apply_usage_cap_mutations(db_acct: Any, mutations: list[dict]) -> None:
    """Apply generation-aware usage deltas to one row locked account."""

    project_state = {
        str(project_id): dict(value) if isinstance(value, dict) else value
        for project_id, value in (db_acct.project_state or {}).items()
    }
    generation_aliases: dict[str, datetime] = {}
    rate_limit_state = dict(db_acct.rate_limit_state or {})
    window_origin = dict(rate_limit_state.get("usage_window_origin") or {})

    for mutation in mutations:
        op = mutation.get("op")
        if op == "daily_reset":
            try:
                observed = datetime.fromisoformat(mutation["observed_generation"])
                requested = datetime.fromisoformat(mutation["generation"])
            except (KeyError, TypeError, ValueError):
                continue
            current = db_acct.daily_task_count_reset_at
            if current == observed:
                db_acct.daily_task_count = 0
                db_acct.daily_task_count_reset_at = requested
                for entry in project_state.values():
                    if isinstance(entry, dict) and "daily_task_count" in entry:
                        entry["daily_task_count"] = 0
                generation_aliases[mutation["generation"]] = requested
                window_origin = {
                    "generation": requested.isoformat(),
                    "kind": "daily_reset",
                }
            elif current is not None and current > observed:
                # Another pod already won this reset generation. Its timestamp
                # is authoritative; following deltas from our equivalent reset
                # belong to that same new window.
                generation_aliases[mutation["generation"]] = current
            continue

        if op != "usage_delta":
            continue
        raw_generation = mutation.get("generation")
        try:
            requested_generation = (
                generation_aliases.get(str(raw_generation))
                or datetime.fromisoformat(str(raw_generation))
            )
        except (TypeError, ValueError):
            continue
        current = db_acct.daily_task_count_reset_at
        if current is None:
            db_acct.daily_task_count_reset_at = requested_generation
            current = requested_generation
            window_origin = {
                "generation": current.isoformat(),
                "kind": "initialization",
            }
        elif current != requested_generation:
            # Only a row explicitly stamped as a first-use initialization may
            # alias another pod's equivalent observed-missing generation. A
            # genuinely stale delta after a daily reset is always rejected,
            # regardless of how close the two timestamps happen to be.
            if not (
                mutation.get("observed_generation_missing") is True
                and window_origin.get("kind") == "initialization"
                and window_origin.get("generation") == current.isoformat()
            ):
                continue
        db_acct.daily_task_count = max(
            0, int(db_acct.daily_task_count or 0) + int(mutation.get("delta") or 0)
        )
        project_id = mutation.get("project_id")
        if project_id:
            entry = project_state.setdefault(str(project_id), {})
            if not isinstance(entry, dict):
                entry = {}
                project_state[str(project_id)] = entry
            entry["daily_task_count"] = max(
                0,
                int(entry.get("daily_task_count") or 0)
                + int(mutation.get("project_delta") or 0),
            )

    db_acct.project_state = project_state or None
    if window_origin:
        rate_limit_state["usage_window_origin"] = window_origin
    db_acct.rate_limit_state = rate_limit_state or None


async def recheck_disabled_accounts():
    """Re-enable legacy full-account disables after cooldown and drain the
    persist-retry queue. Per-model disables are NOT auto-deleted here — the
    state machine in _account_state_for_model transitions them to "probing"
    lazily once cooldown elapses, and clear_rate_limit_state_on_success drops
    entries after a successful probe task."""
    from app.models import MaxPlanAccount
    from sqlmodel import select as sql_select

    changed_accounts = []
    async with _max_plan_pool_lock:
        now = _utcnow()

        for acct in _account_by_id.values():
            # Clear expired startup-failure cooldowns
            sf_cooldown_at = acct.get("startup_failure_cooldown_at")
            if (
                sf_cooldown_at
                and (now - sf_cooldown_at).total_seconds()
                >= _STARTUP_FAILURE_COOLDOWN_SECONDS
            ):
                # Do not clear the cache yet. The DB compare below must prove
                # this is still the same cooldown; another pod may have written
                # a newer startup failure after this stale snapshot.
                acct["_startup_clear_observed_at"] = sf_cooldown_at
                if acct not in changed_accounts:
                    changed_accounts.append(acct)

            # Legacy full-account disable (backward compat)
            if not acct.get("enabled", True):
                disabled_at = acct.get("disabled_at")
                if not disabled_at:
                    continue
                rl_hit_count = acct.get("rl_hit_count", 1)
                cooldown_minutes = min(30 * (2 ** (rl_hit_count - 1)), 240)
                if (now - disabled_at).total_seconds() >= cooldown_minutes * 60:
                    # Compare the observed disable generation under DB row lock
                    # before changing either DB or local state. This prevents a
                    # stale pod from undoing a newer auth/operator disable.
                    acct["_reenable_observed_disabled_at"] = disabled_at
                    acct["_reenable_observed_reason"] = acct.get("disabled_reason")
                    if acct not in changed_accounts:
                        changed_accounts.append(acct)

    # Retry failed cooldown mutations through the row-locked delta path. Do not
    # fold them into the legacy changed_accounts full-blob batch: doing so would
    # resurrect the cross-pod lost-update bug on the retry sweep.
    if _pending_rl_persists:
        retry_ids = list(_pending_rl_persists)
        for acct_id in retry_ids:
            try:
                async with _max_plan_pool_lock:
                    await _persist_rate_limit_state(acct_id)
                if acct_id in _pending_rl_persists:
                    _pending_rl_persists.remove(acct_id)
            except Exception as exc:  # noqa: PERF203 - account retries are isolated
                logger.warning(
                    "[POOL] Failed to retry row-locked rate-limit mutation for %s: %s",
                    acct_id,
                    exc,
                )

    if changed_accounts:
        persisted_accounts: list[dict] = []
        for acct in changed_accounts:
            try:
                uuid.UUID(acct["id"])
            except (KeyError, TypeError, ValueError):  # noqa: PERF203 - per-account validation
                if acct.pop("_startup_clear_observed_at", None) is not None:
                    acct.pop("startup_failure_cooldown_at", None)
                    if (acct.get("disabled_reason") or "").startswith("startup-failure cooldown"):
                        acct["disabled_reason"] = None
                if acct.pop("_reenable_observed_disabled_at", None) is not None:
                    acct.update(enabled=True, disabled_at=None)
                acct.pop("_reenable_observed_reason", None)
            else:
                persisted_accounts.append(acct)
        changed_accounts = persisted_accounts
    if changed_accounts:
        try:
            async with AsyncSessionLocal() as rl_session:
                # Batch-fetch all changed accounts in a single query
                acct_ids = [uuid.UUID(a["id"]) for a in changed_accounts]
                db_accounts = {
                    str(a.id): a
                    for a in (
                        await rl_session.execute(
                            sql_select(MaxPlanAccount)
                            .where(MaxPlanAccount.id.in_(acct_ids))
                            .with_for_update()
                        )
                    )
                    .scalars()
                    .all()
                }
                for acct in changed_accounts:
                    db_acct = db_accounts.get(acct["id"])
                    if db_acct:
                        # This sweep owns only startup-cooldown/full-account
                        # fields. Merge those into the row-locked authoritative
                        # blob; never replace disabled_models/outcome_events
                        # from this pod's potentially stale cache.
                        authoritative_state = dict(db_acct.rate_limit_state or {})
                        startup_observed = acct.get("_startup_clear_observed_at")
                        persisted_startup_at = authoritative_state.get(
                            "startup_failure_cooldown_at"
                        )
                        if (
                            startup_observed is not None
                            and persisted_startup_at == startup_observed.isoformat()
                        ):
                            authoritative_state.pop(
                                "startup_failure_cooldown_at", None
                            )
                            authoritative_state.pop(
                                "startup_failure_disabled_reason", None
                            )
                            if (db_acct.disabled_reason or "").startswith(
                                "startup-failure cooldown"
                            ):
                                db_acct.disabled_reason = None
                        db_acct.rate_limit_state = authoritative_state or None
                        reenable_observed = acct.get(
                            "_reenable_observed_disabled_at"
                        )
                        if (
                            reenable_observed is not None
                            and db_acct.enabled is False
                            and db_acct.disabled_at == reenable_observed
                            and db_acct.disabled_reason
                            == acct.get("_reenable_observed_reason")
                        ):
                            db_acct.enabled = True
                            db_acct.disabled_at = None
                            if (db_acct.disabled_reason or "").startswith(
                                "auto-disabled after"
                            ):
                                db_acct.disabled_reason = None
                        rl_session.add(db_acct)
                await rl_session.commit()
            # Refresh only fields this compare-and-mutate batch owns.
            for acct in changed_accounts:
                db_acct = db_accounts.get(acct["id"])
                if db_acct is not None:
                    _refresh_account_rate_limit_state(
                        acct, db_acct.rate_limit_state
                    )
                    acct["enabled"] = db_acct.enabled
                    acct["disabled_at"] = db_acct.disabled_at
                    acct["disabled_reason"] = db_acct.disabled_reason
                acct.pop("_startup_clear_observed_at", None)
                acct.pop("_reenable_observed_disabled_at", None)
                acct.pop("_reenable_observed_reason", None)
                if acct["id"] in _pending_rl_persists:
                    _pending_rl_persists.remove(acct["id"])
        except Exception as e:
            logger.warning(f"[POOL] Failed to persist rate-limit state changes: {e}")

    # Flush usage-counter DELTAS under both the process lock and DB row locks.
    # Copying a pod's whole cached project_state loses increments made by other
    # pods and can silently bypass configured account/project caps.
    if _pending_cap_persists:
        try:
            async with _max_plan_pool_lock:
                flush_ids = list(_pending_cap_persists)
                mutation_snapshots = {
                    acct_id: list(
                        (_account_by_id.get(acct_id) or {}).get(
                            "_pending_usage_cap_mutations", []
                        )
                    )
                    for acct_id in flush_ids
                }
                async with AsyncSessionLocal() as cap_session:
                    cap_ids = [uuid.UUID(a) for a in flush_ids]
                    cap_accounts = {
                        str(a.id): a
                        for a in (
                            await cap_session.execute(
                                sql_select(MaxPlanAccount)
                                .where(MaxPlanAccount.id.in_(cap_ids))
                                .with_for_update()
                            )
                        )
                        .scalars()
                        .all()
                    }
                    for acct_id, mutations in mutation_snapshots.items():
                        db_acct = cap_accounts.get(acct_id)
                        if db_acct is None or not mutations:
                            continue
                        _apply_usage_cap_mutations(db_acct, mutations)
                        cap_session.add(db_acct)
                    await cap_session.commit()

                for acct_id, mutations in mutation_snapshots.items():
                    acct = _account_by_id.get(acct_id)
                    db_acct = cap_accounts.get(acct_id)
                    if acct is None or db_acct is None:
                        _pending_cap_persists.discard(acct_id)
                        continue
                    pending = acct.get("_pending_usage_cap_mutations") or []
                    del pending[: len(mutations)]
                    if pending:
                        acct["_pending_usage_cap_mutations"] = pending
                        continue
                    acct.pop("_pending_usage_cap_mutations", None)
                    acct.pop("_daily_generation_initialized_from_none", None)
                    acct["daily_task_count"] = int(db_acct.daily_task_count or 0)
                    acct["daily_task_count_reset_at"] = db_acct.daily_task_count_reset_at
                    authoritative_projects = db_acct.project_state or {}
                    local_projects = acct.setdefault("project_state", {})
                    for project_id, persisted in authoritative_projects.items():
                        if not isinstance(persisted, dict):
                            continue
                        local = local_projects.setdefault(project_id, {})
                        local["daily_task_count"] = int(
                            persisted.get("daily_task_count") or 0
                        )
                    acct["enabled"] = db_acct.enabled
                    acct["disabled_at"] = db_acct.disabled_at
                    acct["disabled_reason"] = db_acct.disabled_reason
                    _pending_cap_persists.discard(acct_id)
        except Exception as e:
            logger.warning(f"[POOL] Failed to persist usage-cap state: {e}")


_CREDENTIAL_SWEEP_INTERVAL_SECONDS = 5 * 60  # 5 minutes


async def credential_sweep_service() -> None:
    """Background service: periodically clears runtime_secret for idle accounts.

    Accounts with zero active_tasks and a Secret Manager-backed secret_blob_ref
    (starts with "projects/") have their resolved credential cleared every
    _CREDENTIAL_SWEEP_INTERVAL_SECONDS.  The next select_max_plan_account call
    re-fetches from Secret Manager, bounding how long a resolved credential
    lives in process memory.

    This is a non-leader service: every process instance runs its own sweep
    against its own in-memory pool state.
    """
    while True:
        await asyncio.sleep(_CREDENTIAL_SWEEP_INTERVAL_SECONDS)
        try:
            async with _max_plan_pool_lock:
                for acct in _account_by_id.values():
                    if acct.get("active_tasks", 0) == 0:
                        secret_ref = acct.get("secret_blob_ref")
                        if secret_ref and str(secret_ref).startswith("projects/"):
                            acct.pop("runtime_secret", None)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[POOL] credential_sweep_service error")
