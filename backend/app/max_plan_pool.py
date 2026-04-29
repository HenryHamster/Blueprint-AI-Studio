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
from typing import Any, Dict, List, Mapping

from sqlalchemy.exc import SQLAlchemyError

from app.database import AsyncSessionLocal
from app.models import LLMAuthMode, LLMProviderName
from app.providers.base import merge_settings
from app.providers.registry import get_provider
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


def _task_context(task_or_ctx: Any) -> dict[str, Any]:
    if isinstance(task_or_ctx, Mapping):
        return dict(task_or_ctx)
    return dict(getattr(task_or_ctx, "context", {}) or {})


def get_task_llm_account_id(task_or_ctx: Any) -> str | None:
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


def clear_task_llm_runtime_context(context: Mapping[str, Any] | None) -> dict[str, Any]:
    new_ctx = dict(context or {})
    for key in TASK_RUNTIME_CONTEXT_KEYS:
        new_ctx.pop(key, None)
    return new_ctx


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


def infer_provider_for_model(model: str | None, runtime_config: Mapping[str, Any]) -> str | None:
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


ALL_PROVIDERS: tuple[str, ...] = tuple(p.value for p in LLMProviderName)
_VALID_SELECTION_MODES: frozenset[str] = frozenset({"balanced", "preferred", "exclusive"})
VALID_EFFORT_LEVELS: tuple[str, ...] = ("low", "medium", "high", "max")
DEFAULT_EFFORT_LEVEL = "medium"


def _normalize_provider_list(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    values = [_normalize_provider_name(v) for v in raw]
    return [v for v in values if v]


def _category_for_task_type(task_type: str, runtime_config: Mapping[str, Any]) -> str | None:
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
    providers = _normalize_provider_list(providers_raw) if providers_raw is not None else None
    if providers is not None and not providers:
        providers = None
    effort_raw = raw.get("effort")
    effort = effort_raw.strip() if isinstance(effort_raw, str) and effort_raw.strip() else None
    return providers, effort


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
        for k in ("category_routing", "task_type_routing", "provider_settings_by_effort")
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


def resolve_task_routing(task: Any, runtime_config: Mapping[str, Any]) -> dict[str, Any]:
    """Resolve ``{providers, effort}`` for a task, most-specific first.

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

    ctx = _task_context(task)
    payloads = getattr(task, "payloads", None) or []
    payload = payloads[0] if payloads else {}

    # Layer 1+2: per-task payload/context — payload wins.
    for source in (payload if isinstance(payload, Mapping) else None, ctx):
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

    task_type = _task_type(task)

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

    return {
        "providers": providers if providers is not None else list(ALL_PROVIDERS),
        "effort": effort if effort is not None else DEFAULT_EFFORT_LEVEL,
    }


def resolve_task_provider_preference(task: Any, runtime_config: Mapping[str, Any]) -> list[str]:
    """Thin back-compat shim — returns just the provider list."""
    return resolve_task_routing(task, runtime_config)["providers"]


def resolve_task_model_preferences(task: Any, runtime_config: Mapping[str, Any]) -> list[dict[str, str]]:
    ctx = _task_context(task)
    payloads = getattr(task, "payloads", None) or []
    payload = payloads[0] if payloads else {}
    explicit = payload.get("model_preferences") or ctx.get("model_preferences")
    raw_prefs = explicit if isinstance(explicit, list) else (runtime_config.get("task_type_model_preferences", {}) or {}).get(_task_type(task))
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

    base = defaults_all.get(provider_name) if isinstance(defaults_all, Mapping) else None

    effort_bag = None
    effort = resolve_task_routing(task, runtime_config)["effort"]
    if isinstance(matrix_all, Mapping):
        per_provider = matrix_all.get(provider_name)
        if isinstance(per_provider, Mapping):
            candidate = per_provider.get(effort)
            if isinstance(candidate, Mapping):
                effort_bag = candidate

    task_type = _task_type(task)
    task_override = overrides_all.get(task_type) if isinstance(overrides_all, Mapping) else None
    type_bag = task_override.get(provider_name) if isinstance(task_override, Mapping) else None

    ctx = _task_context(task)
    payloads = getattr(task, "payloads", None) or []
    payload = payloads[0] if payloads else {}
    payload_settings = payload.get("provider_settings") if isinstance(payload, Mapping) else None
    ctx_settings = ctx.get("provider_settings")
    payload_bag = payload_settings.get(provider_name) if isinstance(payload_settings, Mapping) else None
    ctx_bag = ctx_settings.get(provider_name) if isinstance(ctx_settings, Mapping) else None

    merged = merge_settings(base, effort_bag, type_bag, ctx_bag, payload_bag)
    if not merged:
        return {}
    provider = get_provider(provider_name)
    return provider.validate_settings(merged)




def _account_runtime_ready(acct: dict) -> bool:
    provider = _normalize_provider_name(acct.get("provider")) or LLMProviderName.ANTHROPIC.value
    auth_mode = _normalize_auth_mode(acct.get("auth_mode")) or LLMAuthMode.OAUTH.value
    if provider == LLMProviderName.ANTHROPIC.value and auth_mode == LLMAuthMode.OAUTH.value:
        return bool(acct.get("oauth_token"))
    return bool(acct.get("runtime_ready"))


# --- Max Plan Account Pool ---
# In-memory cache of DB-persisted accounts. Keeps active_tasks count (runtime only).
# Per-user pools keyed by user_id. Flat lookup by account_id for O(1) release.
_max_plan_pools: Dict[str, List[Dict[str, Any]]] = {}  # user_id → accounts
_account_by_id: Dict[str, Dict[str, Any]] = {}  # account_id → account dict
_max_plan_pool_lock = asyncio.Lock()
_pending_rl_persists: List[str] = []  # Account IDs whose rate-limit state failed to persist
_pending_cap_persists: set = set()  # Account IDs with dirty daily_task_count / disabled_reason to flush

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
#   - Haiku shares Opus's meter exactly — it does NOT have its own bucket,
#     so haiku tracking normalizes to opus via _normalize_rl_model().
_MODEL_TIER_CASCADE: Dict[str, tuple] = {
    "claude-opus-4-7": ("claude-opus-4-7",),
    # Legacy mappings kept so prior-queued tasks with 4-6 models still resolve
    # to a valid cascade; all new tasks use 4-7.
    "claude-opus-4-6": ("claude-opus-4-7",),
    "claude-sonnet-4-6": ("claude-opus-4-7",),
}

_HAIKU_MODEL = "claude-haiku-4-5-20251001"
_OPUS_MODEL = "claude-opus-4-7"


def _normalize_rl_model(model: str | None) -> str | None:
    """Map Haiku to Opus for rate-limit tracking — they share the same budget.
    Used at every rate-limit code path that keys into disabled_models so Haiku
    tasks bookkeep against Opus's meter and Opus disable/clear automatically
    covers Haiku."""
    if model == _HAIKU_MODEL:
        return _OPUS_MODEL
    return model


def cascade_disabled_models(model: str) -> tuple:
    """Return every model that should be marked disabled when `model` hits a 429.
    Normalizes Haiku to Opus first (shared meter). Falls back to the single
    model for unknown names so new variants don't silently cascade to the
    whole account."""
    normalized = _normalize_rl_model(model)
    return _MODEL_TIER_CASCADE.get(normalized, (normalized,) if normalized else (model,))


# Rolling window for account health score computation
_HEALTH_WINDOW_SECONDS = 24 * 3600  # 24-hour rolling window
_OUTCOME_WINDOW_SECONDS = 7 * 24 * 3600  # 7-day rolling window for success-rate tracking

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
    completion_count = sum(1 for e in acct.get("completion_events", []) if e["at"] > cutoff)
    return completion_count - 2.0 * rl_count


def _compute_success_rate_7d(acct: Dict[str, Any], now: datetime | None = None) -> float | None:
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
        e for e in acct.get("outcome_events", [])
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


def _compute_avg_duration_24h(acct: Dict[str, Any], now: datetime | None = None) -> float | None:
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


def get_account_health_stats(acct: Dict[str, Any], now: datetime | None = None) -> Dict[str, Any]:
    """Return observable health stats for an account (used by admin endpoints)."""
    now = now or _utcnow()
    cutoff = now - timedelta(seconds=_HEALTH_WINDOW_SECONDS)
    rl_events = [t for t in acct.get("rl_events", []) if t > cutoff]
    completion_events = [e for e in acct.get("completion_events", []) if e["at"] > cutoff]
    durations = [e["duration_seconds"] for e in completion_events if e.get("duration_seconds") is not None]
    outcome_cutoff_7d = now - timedelta(seconds=_OUTCOME_WINDOW_SECONDS)
    outcome_events_7d = [e for e in acct.get("outcome_events", []) if e["at"] > outcome_cutoff_7d]
    task_types = sorted({e.get("task_type") for e in outcome_events_7d if e.get("task_type")})
    success_by_type: dict[str, float] = {}
    for tt in task_types:
        typed = [e for e in outcome_events_7d if e.get("task_type") == tt]
        if len(typed) >= 3:
            success_by_type[tt] = round(sum(1 for e in typed if e["success"]) / len(typed), 4)
    return {
        "rl_events_24h": len(rl_events),
        "completions_24h": len(completion_events),
        "avg_duration_seconds": round(sum(durations) / len(durations), 1) if durations else None,
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
        acct["completion_events"] = [e for e in acct["completion_events"] if e["at"] > cutoff]


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
    Reconciles active_tasks from DB by counting tasks currently PROVISIONING/IN_PROGRESS."""
    from sqlmodel import select as sql_select
    from app.models import MaxPlanAccount, Task, TaskStatus

    async with AsyncSessionLocal() as session:
        result = await session.execute(sql_select(MaxPlanAccount))
        accounts = result.scalars().all()

        # Count actually active tasks per account to reconcile after restarts
        active_tasks = (await session.execute(
            sql_select(Task).where(Task.status.in_([TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS]))
        )).scalars().all()
        active_per_account: Dict[str, int] = {}
        for t in active_tasks:
            acct_id = get_task_llm_account_id(t)
            if acct_id:
                active_per_account[acct_id] = active_per_account.get(acct_id, 0) + 1

        _max_plan_pools.clear()
        _account_by_id.clear()
        for acct in accounts:
            acct_id = str(acct.id)
            provider = acct.provider or LLMProviderName.ANTHROPIC
            auth_mode = acct.auth_mode or LLMAuthMode.OAUTH
            runtime_secret = acct.secret_blob_ref or acct.oauth_token
            if runtime_secret and runtime_secret.startswith("projects/"):
                try:
                    from app.secrets_manager import get_user_secret
                    runtime_secret = await get_user_secret(runtime_secret)
                except Exception:
                    logger.exception(f"[POOL] Failed to fetch secret from Secret Manager for account {acct.name}")
                    runtime_secret = None
            token = runtime_secret if provider == LLMProviderName.ANTHROPIC and auth_mode == LLMAuthMode.OAUTH else None
            entry = {
                "id": acct_id,
                "user_id": acct.user_id,
                "name": acct.name,
                "provider": provider.value if isinstance(provider, LLMProviderName) else str(provider),
                "auth_mode": auth_mode.value if isinstance(auth_mode, LLMAuthMode) else str(auth_mode),
                "secret_blob_ref": acct.secret_blob_ref,
                "provider_metadata": acct.provider_metadata or {},
                "runtime_secret": runtime_secret,
                "oauth_token": token,
                "runtime_ready": bool(runtime_secret),
                "claude_config_json": acct.claude_config_json,
                "enabled": acct.enabled,
                "active_tasks": active_per_account.get(acct_id, 0),
                "total_tasks": acct.total_tasks,
                "token_created_at": acct.token_created_at.isoformat() if acct.token_created_at else None,
                "daily_task_cap": acct.daily_task_cap,
                "max_concurrent_tasks": acct.max_concurrent_tasks,
                "auto_disable_after_rate_limits": acct.auto_disable_after_rate_limits,
                "daily_task_count": acct.daily_task_count,
                "daily_task_count_reset_at": acct.daily_task_count_reset_at,
                "disabled_reason": acct.disabled_reason,
                "disabled_at": acct.disabled_at,
            }
            # Restore rate-limit state from DB (skip expired cooldowns)
            rl_state = acct.rate_limit_state or {}
            db_disabled_models = rl_state.get("disabled_models", {})
            if db_disabled_models:
                now = _utcnow()
                restored_dm = {}
                for model_name, info in db_disabled_models.items():
                    at_str = info.get("at")
                    if at_str:
                        at_dt = datetime.fromisoformat(at_str)
                        restored_info: dict[str, Any] = {
                            "hits": info.get("hits", 1),
                            "at": at_dt,
                        }
                        # Preserve structured fields (kind, retry_after_seconds)
                        # across restarts so quota_exhausted accounts aren't
                        # silently demoted to an exponential-rate-limit cooldown.
                        if info.get("kind") == "quota_exhausted":
                            restored_info["kind"] = "quota_exhausted"
                        hint = info.get("retry_after_seconds")
                        if isinstance(hint, (int, float)) and hint > 0:
                            restored_info["retry_after_seconds"] = int(hint)
                        if (now - at_dt).total_seconds() < _cooldown_seconds_for_info(restored_info):
                            restored_dm[model_name] = restored_info
                if restored_dm:
                    entry["disabled_models"] = restored_dm
                    logger.info(f"[STARTUP] Restored rate-limit state for {acct.name}: {list(restored_dm.keys())}")
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
                            restored_oe.append({
                                "at": at_dt,
                                "success": bool(ev.get("success")),
                                "task_type": ev.get("task_type"),
                            })
                if restored_oe:
                    entry["outcome_events"] = restored_oe
            _max_plan_pools.setdefault(acct.user_id, []).append(entry)
            _account_by_id[acct_id] = entry
        total_active = sum(active_per_account.values())
        logger.info(f"[STARTUP] Loaded {len(_account_by_id)} Max plan accounts for {len(_max_plan_pools)} users ({total_active} active tasks reconciled)")


_DAILY_WINDOW_SECONDS = 24 * 60 * 60


def _daily_reset_if_elapsed(acct: dict, now: datetime | None = None) -> bool:
    """Reset the daily counter if the 24h window since reset_at has elapsed (or
    no reset_at has been set yet). Returns True when a reset happened so callers
    can mark the account dirty for persistence."""
    now = now or _utcnow()
    reset_at = acct.get("daily_task_count_reset_at")
    if reset_at is None:
        acct["daily_task_count_reset_at"] = now
        acct["daily_task_count"] = int(acct.get("daily_task_count") or 0)
        return False
    if (now - reset_at).total_seconds() >= _DAILY_WINDOW_SECONDS:
        acct["daily_task_count"] = 0
        acct["daily_task_count_reset_at"] = now
        return True
    return False


def _account_at_cap(acct: dict) -> str | None:
    """Return a reason string if the account is at any usage cap, else None."""
    daily_cap = acct.get("daily_task_cap")
    if daily_cap is not None and int(acct.get("daily_task_count") or 0) >= int(daily_cap):
        return f"daily cap ({daily_cap}) reached"
    concurrent_cap = acct.get("max_concurrent_tasks")
    if concurrent_cap is not None and int(acct.get("active_tasks") or 0) >= int(concurrent_cap):
        return f"concurrent cap ({concurrent_cap}) reached"
    return None


_QUOTA_EXHAUSTED_COOLDOWN_SECONDS = 24 * 60 * 60  # flat 24h for billing walls
_QUARANTINE_COOLDOWN_SECONDS = 15 * 60  # flat 15-min recheck for confidence-gated quarantine
QUARANTINE_CONSECUTIVE_N = 3  # consecutive qualifying failures to trip the quarantine
QUARANTINE_CONFIDENCE_THRESHOLD = 0.8  # minimum per-record confidence to count
_QUARANTINE_ELIGIBLE_CLASSES = frozenset({"RATE_LIMIT", "PROVISION_FAILURE"})


def _serialize_disabled_model_info(info: dict) -> dict:
    """JSON-safe snapshot of a disabled_models entry for DB persistence.

    Only emits fields that are set. `at` becomes an ISO string; numeric
    retry_after_seconds is preserved; string kind is preserved.
    """
    out: dict[str, Any] = {
        "hits": info.get("hits", 1),
        "at": info["at"].isoformat(),
    }
    if info.get("kind") in ("quota_exhausted", "quarantine"):
        out["kind"] = info["kind"]
    hint = info.get("retry_after_seconds")
    if isinstance(hint, (int, float)) and hint > 0:
        out["retry_after_seconds"] = int(hint)
    return out


def _cooldown_seconds_for_info(info: dict) -> int:
    """Compute remaining-effective cooldown window (seconds) for a disabled_models entry.

    Rules:
    - kind="quota_exhausted" → flat 24h. These are billing walls; the
      exponential-backoff curve doesn't apply because the error doesn't
      resolve on clock.
    - Otherwise exponential: min(30 * 2^(hits-1), 240) minutes.
    - Server-provided retry_after_seconds is treated as a floor — we never
      shorten below it, and we never extend below the exponential value.
    """
    if info.get("kind") == "quota_exhausted":
        return _QUOTA_EXHAUSTED_COOLDOWN_SECONDS
    if info.get("kind") == "quarantine":
        return _QUARANTINE_COOLDOWN_SECONDS
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
    from app.failure_classification import get_account_failure_class_records  # lazy — avoids cycle
    records = get_account_failure_class_records(account_id)
    if len(records) < QUARANTINE_CONSECUTIVE_N:
        return False
    last_n = records[-QUARANTINE_CONSECUTIVE_N:]
    return all(
        cls in _QUARANTINE_ELIGIBLE_CLASSES and (conf or 0.0) >= QUARANTINE_CONFIDENCE_THRESHOLD
        for cls, conf, _ts in last_n
    )


def _account_state_for_model(acct: dict, model: str | None, now: datetime | None = None) -> str:
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
    model = _normalize_rl_model(model)
    info = dm.get(model)
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


def _account_available_for_model(acct: dict, model: str | None) -> bool:
    """True if the account is either healthy OR probing for the given model.
    Probing accounts are serialized inside select_max_plan_account; outside
    callers (blacklist pruning, gates, health checks) treat them as usable."""
    return _account_state_for_model(acct, model) != "unavailable"


def summarize_pool_state(model: str | None, user_id: str | None = None) -> Dict[str, Any]:
    """Summarize account pool state for diagnostics. Returns counts per state
    plus the earliest cooldown recovery time across rate-limited accounts.

    If user_id is provided, only that user's accounts are considered; otherwise
    the entire pool is summarized.
    """
    accts = _max_plan_pools.get(user_id, []) if user_id else _all_accounts()
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
    user_id: str | None = None, now: datetime | None = None
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
            rate_limited_models.append({
                "model": model_name,
                "kind": info.get("kind", "rate_limit"),
                "hits": info.get("hits", 1),
                "cooldown_remaining_seconds": int(remaining),
            })

        results.append({
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
        })
    return results


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


def resolve_task_quality_tier(task: Any, runtime_config: Mapping[str, Any]) -> str:
    ctx = _task_context(task)
    payloads = getattr(task, "payloads", None) or []
    payload = payloads[0] if payloads else {}
    explicit_tier = payload.get("quality_tier") or ctx.get("quality_tier")
    if isinstance(explicit_tier, str) and explicit_tier.strip():
        return explicit_tier.strip()
    explicit_model = payload.get("model") or ctx.get("llm_model")
    inferred_provider = get_task_llm_provider(task) or infer_provider_for_model(explicit_model, runtime_config)
    inferred_tier = infer_quality_tier_for_model(inferred_provider, explicit_model, runtime_config)
    return inferred_tier or DEFAULT_QUALITY_TIER


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
    out: dict[str, Any] = {}
    if dm:
        out["disabled_models"] = {m: _serialize_disabled_model_info(i) for m, i in dm.items()}
    if pruned_outcomes:
        out["outcome_events"] = pruned_outcomes
    return out if out else None


async def _persist_rate_limit_state(acct_id: str) -> None:
    """Persist a single account's in-memory disabled_models + outcome_events to DB.
    Caller MUST hold _max_plan_pool_lock. Raises on DB failure — caller is
    responsible for queuing retries via _pending_rl_persists."""
    from app.models import MaxPlanAccount
    acct = _account_by_id.get(acct_id)
    if acct is None:
        return
    async with AsyncSessionLocal() as rl_session:
        db_acct = await rl_session.get(MaxPlanAccount, uuid.UUID(acct_id))
        if db_acct is None:
            return
        db_acct.rate_limit_state = _build_rate_limit_state_blob(acct)
        db_acct.enabled = acct.get("enabled", True)
        rl_session.add(db_acct)
        await rl_session.commit()


async def select_max_plan_account_balanced(
    user_id: str,
    provider_models: List[tuple[str, str | None]],
    blacklist: List[str] | None = None,
    task_type: str | None = None,
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
    """
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
        for provider_name, model in provider_models:
            normalized_provider = _normalize_provider_name(provider_name)
            if not normalized_provider:
                continue
            for a in user_pool:
                if a["id"] in seen_ids:
                    continue
                if blacklist and a["id"] in blacklist:
                    continue
                acct_provider = (
                    _normalize_provider_name(a.get("provider"))
                    or LLMProviderName.ANTHROPIC.value
                )
                if acct_provider != normalized_provider:
                    continue
                if _daily_reset_if_elapsed(a, now):
                    dirty_from_reset.append(a["id"])
                if _account_at_cap(a):
                    continue
                state = _account_state_for_model(a, model, now)
                if state == "healthy":
                    candidates_healthy.append((a, normalized_provider, model))
                    seen_ids.add(a["id"])
                elif state == "probing" and a.get("active_tasks", 0) == 0:
                    candidates_probing.append((a, normalized_provider, model))
                    seen_ids.add(a["id"])

        for acct_id in dirty_from_reset:
            _pending_cap_persists.add(acct_id)

        winner: tuple[Dict[str, Any], str, str | None] | None = None
        if candidates_healthy:
            # Gate decisively-negative accounts when alternatives exist — but
            # otherwise rank primarily by load. Ranking by health_score across
            # providers is unfair: a mature anthropic account with 100
            # completions scores 100 while a brand-new openai account scores 0,
            # so balanced mode would never pick the new account.
            if len(candidates_healthy) > 1:
                qualified = [
                    c for c in candidates_healthy
                    if _compute_health_score(c[0], now) >= _HEALTH_SCORE_DISQUALIFY_THRESHOLD
                ]
                if qualified:
                    candidates_healthy = qualified
                # Success-rate soft bias: deprioritize accounts whose 7-day success
                # rate is materially below the pool median when alternatives exist.
                # Sparse accounts (< 3 outcomes) return None and are treated as
                # neutral so brand-new accounts are never starved.
                if len(candidates_healthy) > 1:
                    rates = [_effective_success_rate(c[0], task_type, now) for c in candidates_healthy]
                    sampled = [r for r in rates if r is not None]
                    if sampled:
                        pool_median = sorted(sampled)[len(sampled) // 2]
                        preferred = [
                            c for c, r in zip(candidates_healthy, rates)
                            if r is None or r >= pool_median - _SUCCESS_RATE_DEPRIORITIZE_MARGIN
                        ]
                        if preferred:
                            candidates_healthy = preferred
            winner = min(
                candidates_healthy,
                # Primary: fewest active_tasks. Secondary: higher 7d success rate
                # (task-type-scoped when confident, aggregate fallback, None → 0.5
                # so sparse accounts aren't penalised).
                # Tertiary: faster avg duration (None → neutral 0 penalty so sparse
                # accounts aren't penalised). Tiebreak: deterministic by id.
                key=lambda c: (
                    c[0].get("active_tasks", 0),
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
        _pending_cap_persists.add(account["id"])
        return account, prov, model


async def select_max_plan_account(
    user_id: str,
    model: str | None = None,
    blacklist: List[str] | None = None,
    provider: str | LLMProviderName | None = None,
    task_type: str | None = None,
) -> Dict[str, Any] | None:
    """Select a Max plan account for a task. Two-tier circuit-breaker selection:

    1. Healthy accounts first, picked by min(active_tasks) for load balancing.
    2. Probing accounts (post-cooldown, unverified) second, but only if
       active_tasks == 0 — this serializes the first task after cooldown as a
       de-facto probe, preventing a thundering herd on a still-rate-limited
       account. If the probe succeeds, the COMPLETED path clears disabled_models
       (restoring full concurrency); if it 429s, the mark path extends cooldown.

    Pre-increments active_tasks atomically under lock."""
    async with _max_plan_pool_lock:
        user_pool = _max_plan_pools.get(user_id, [])
        healthy: List[Dict[str, Any]] = []
        probing: List[Dict[str, Any]] = []
        normalized_provider = _normalize_provider_name(provider)
        now = _utcnow()
        dirty_from_reset: list[str] = []
        for a in user_pool:
            if blacklist and a["id"] in blacklist:
                continue
            if normalized_provider and (_normalize_provider_name(a.get("provider")) or LLMProviderName.ANTHROPIC.value) != normalized_provider:
                continue
            if _daily_reset_if_elapsed(a, now):
                dirty_from_reset.append(a["id"])
            cap_reason = _account_at_cap(a)
            if cap_reason:
                continue
            state = _account_state_for_model(a, model, now)
            if state == "healthy":
                healthy.append(a)
            elif state == "probing" and a.get("active_tasks", 0) == 0:
                probing.append(a)

        for acct_id in dirty_from_reset:
            _pending_cap_persists.add(acct_id)

        if healthy:
            # Exclude decisively-negative-health-score accounts when alternatives exist.
            # This prevents chronically rate-limited accounts from receiving new tasks
            # while still allowing them as last resort if no other account qualifies.
            if len(healthy) > 1:
                qualified = [a for a in healthy if _compute_health_score(a, now) >= _HEALTH_SCORE_DISQUALIFY_THRESHOLD]
                if qualified:
                    healthy = qualified
            # Rank by health score, then 7d success rate (task-type-scoped when
            # confident, aggregate fallback), then latency (faster is better;
            # None → 0.0 so sparse accounts get no penalty), then fewest active tasks.
            account = max(healthy, key=lambda a: (
                _compute_health_score(a, now),
                _effective_success_rate(a, task_type, now) or 0.0,
                -(_compute_avg_duration_24h(a, now) or 0.0),
                -a.get("active_tasks", 0),
            ))
        elif probing:
            # Deterministic by id so concurrent selects under the same lock get
            # distinct probes when multiple accounts are eligible.
            account = min(probing, key=lambda a: a["id"])
            logger.info(f"[POOL] Probing account {account['name']} for model {model} (cooldown elapsed, serializing as probe)")
        else:
            return None

        account["active_tasks"] = account.get("active_tasks", 0) + 1
        account["daily_task_count"] = int(account.get("daily_task_count") or 0) + 1
        _pending_cap_persists.add(account["id"])
        return account


async def select_llm_runtime_for_task(
    user_id: str,
    task: Any,
    runtime_config: Mapping[str, Any],
    blacklist: List[str] | None = None,
) -> LLMSelectionResult | None:
    explicit_model = _extract_task_model(task)
    mode = provider_selection_mode(runtime_config)
    allow_fallback = mode in ("balanced", "preferred")
    model_preferences = [] if explicit_model else resolve_task_model_preferences(task, runtime_config)
    provider_preference = resolve_task_provider_preference(task, runtime_config)
    quality_tier = resolve_task_quality_tier(task, runtime_config)
    explicit_provider = get_task_llm_provider(task) or infer_provider_for_model(explicit_model, runtime_config)
    routing_task_type = _task_type(task)

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

    if model_preferences:
        for pref in model_preferences:
            provider_name = pref["provider"]
            model_name = pref["model"]
            account = await select_max_plan_account(
                user_id=user_id,
                model=model_name,
                blacklist=blacklist,
                provider=provider_name,
                task_type=routing_task_type,
            )
            if account:
                return LLMSelectionResult(
                    provider=provider_name,
                    auth_mode=_normalize_auth_mode(account.get("auth_mode")) or LLMAuthMode.OAUTH.value,
                    model=model_name,
                    quality_tier=infer_quality_tier_for_model(provider_name, model_name, runtime_config),
                    account=account,
                    settings=resolve_task_provider_settings(task, runtime_config, provider_name),
                )

    if explicit_provider:
        if allow_fallback:
            provider_preference = [explicit_provider] + [p for p in provider_preference if p != explicit_provider]
        else:
            provider_preference = [explicit_provider]
    elif explicit_model and provider_preference:
        # Unknown/custom model strings must not silently degrade into tier defaults
        # or cross-provider retries. Keep the caller's requested model on the
        # first configured provider only until mappings are updated.
        provider_preference = provider_preference[:1]

    # Balanced mode: pool all listed providers' accounts and pick globally by
    # (health, -load). An explicit per-task provider/model still pins the
    # selection (handled above via trimmed provider_preference), so this only
    # kicks in for the common path where no pin is set.
    if mode == "balanced" and len(provider_preference) > 1 and not explicit_model:
        provider_models: list[tuple[str, str | None]] = []
        for provider_name in provider_preference:
            provider = get_provider(provider_name)
            pre_settings = resolve_task_provider_settings(task, runtime_config, provider_name)
            bag_model = pre_settings.get("model") if isinstance(pre_settings, Mapping) else None
            if isinstance(bag_model, str) and bag_model:
                model = bag_model
            else:
                model = provider.tier_to_model(quality_tier, runtime_config)
            if not model:
                continue
            provider_models.append((provider_name, model))
        if provider_models:
            balanced_result = await select_max_plan_account_balanced(
                user_id=user_id,
                provider_models=provider_models,
                blacklist=blacklist,
                task_type=routing_task_type,
            )
            if balanced_result is not None:
                account, provider_name, chosen_model = balanced_result
                return LLMSelectionResult(
                    provider=provider_name,
                    auth_mode=_normalize_auth_mode(account.get("auth_mode")) or LLMAuthMode.OAUTH.value,
                    model=chosen_model,
                    quality_tier=quality_tier,
                    account=account,
                    settings=resolve_task_provider_settings(task, runtime_config, provider_name),
                )
            # Fall through to per-provider iteration so the unaccounted-for
            # fallback (no account present, but explicit_provider/model set)
            # still resolves a synthetic result below.

    for provider_name in provider_preference:
        provider = get_provider(provider_name)
        # Resolve settings bag first so a bag-pinned `model` (e.g. a task-type
        # override forcing gemini-2.5-flash for smoketest) takes precedence
        # over tier resolution. Explicit payload.model still wins over both.
        pre_settings = resolve_task_provider_settings(task, runtime_config, provider_name)
        bag_model = pre_settings.get("model") if isinstance(pre_settings, Mapping) else None
        if explicit_model and (explicit_provider is None or provider_name == explicit_provider):
            model = explicit_model
        elif isinstance(bag_model, str) and bag_model:
            model = bag_model
        else:
            model = provider.tier_to_model(quality_tier, runtime_config)
        if not model:
            continue
        account = await select_max_plan_account(
            user_id=user_id,
            model=model,
            blacklist=blacklist,
            provider=provider_name,
            task_type=routing_task_type,
        )
        if account:
            return LLMSelectionResult(
                provider=provider_name,
                auth_mode=_normalize_auth_mode(account.get("auth_mode")) or LLMAuthMode.OAUTH.value,
                model=model,
                quality_tier=quality_tier,
                account=account,
                settings=resolve_task_provider_settings(task, runtime_config, provider_name),
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
            account = await select_max_plan_account(
                user_id=user_id,
                model=fallback,
                blacklist=blacklist,
                provider=provider_name,
                task_type=routing_task_type,
            )
            if account:
                logger.warning(
                    "[LLM_SELECT] No pool account has %s; falling back to GA "
                    "counterpart %s on %s. Recorded on task.context.model_fallback.",
                    model, fallback, provider_name,
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
                    auth_mode=_normalize_auth_mode(account.get("auth_mode")) or LLMAuthMode.OAUTH.value,
                    model=fallback,
                    quality_tier=quality_tier,
                    account=account,
                    settings=resolve_task_provider_settings(task, runtime_config, provider_name),
                )

    if explicit_provider and explicit_model:
        return LLMSelectionResult(
            provider=explicit_provider,
            auth_mode=get_task_llm_auth_mode(task) or LLMAuthMode.OAUTH.value,
            model=explicit_model,
            quality_tier=quality_tier,
            account=None,
            settings=resolve_task_provider_settings(task, runtime_config, explicit_provider),
        )

    if provider_preference:
        fallback_provider = provider_preference[0]
        provider = get_provider(fallback_provider)
        fallback_model = explicit_model or provider.tier_to_model(quality_tier, runtime_config)
        return LLMSelectionResult(
            provider=fallback_provider,
            auth_mode=LLMAuthMode.OAUTH.value,
            model=fallback_model,
            quality_tier=quality_tier,
            account=None,
            settings=resolve_task_provider_settings(task, runtime_config, fallback_provider),
        )

    return None


async def release_max_plan_account(account_id: str):
    """Decrement active tasks for an account when a task finishes."""
    async with _max_plan_pool_lock:
        account = _account_by_id.get(account_id)
        if account:
            if account["active_tasks"] <= 0:
                logger.warning(f"[POOL] Account {account['name']} ({account_id}) release underflow — active_tasks was {account['active_tasks']}")
            account["active_tasks"] = max(0, account["active_tasks"] - 1)


async def disable_account_model(
    acct_id: str,
    model: str,
    *,
    retry_after_seconds: int | None = None,
    kind: str = "rate_limit",
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

    Acquires _max_plan_pool_lock. On DB persist failure, appends to
    _pending_rl_persists for retry (same contract as clear_rate_limit_state_on_success).
    """
    if not acct_id or not model:
        return
    # Normalize Haiku → Opus so the meter is keyed correctly.
    model = _normalize_rl_model(model)
    cascade = cascade_disabled_models(model)
    async with _max_plan_pool_lock:
        acct = _account_by_id.get(acct_id)
        if acct is None:
            return
        dm = acct.setdefault("disabled_models", {})
        now = _utcnow()
        for m in cascade:
            info = dm.setdefault(m, {"hits": 0})
            info["hits"] += 1
            info["at"] = now
            if kind == "quota_exhausted":
                info["kind"] = "quota_exhausted"
            if retry_after_seconds is not None:
                info["retry_after_seconds"] = int(retry_after_seconds)
        # Record rate-limit event for rolling health score + success-rate tracking
        _prune_health_events(acct, now)
        acct.setdefault("rl_events", []).append(now)
        _prune_outcome_events(acct, now)
        acct.setdefault("outcome_events", []).append({"at": now, "success": False})
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
            cooldown = min(30 * (2 ** (primary_hits - 1)), 240) * random.uniform(0.85, 1.15)
            if retry_after_seconds is not None:
                hint_minutes = retry_after_seconds / 60.0
                if hint_minutes > cooldown:
                    cooldown = hint_minutes
            logger.warning(
                f"[POOL] Account {acct['name']} disabled models {list(cascade)} "
                f"(primary {primary_key} hit #{primary_hits}, cooldown {cooldown:.0f}min"
                + (f", server-hinted {retry_after_seconds}s" if retry_after_seconds else "")
                + ")"
            )

        auto_disable_threshold = acct.get("auto_disable_after_rate_limits")
        if auto_disable_threshold is not None and primary_hits >= int(auto_disable_threshold):
            reason = f"auto-disabled after {primary_hits} rate-limit hit(s) (threshold {auto_disable_threshold})"
            acct["enabled"] = False
            acct["disabled_at"] = now
            acct["disabled_reason"] = reason
            _pending_cap_persists.add(acct_id)
            logger.warning(f"[POOL] Account {acct['name']} auto-disabled: {reason}")

        if kind != "quota_exhausted" and _check_quarantine_trigger(acct_id):
            for m in cascade:
                dm[m]["kind"] = "quarantine"
            logger.warning(
                f"[POOL] Account {acct['name']} quarantine triggered: last {QUARANTINE_CONSECUTIVE_N} "
                f"high-confidence failures — {_QUARANTINE_COOLDOWN_SECONDS // 60}min recheck cooldown"
            )

        try:
            await _persist_rate_limit_state(acct_id)
        except SQLAlchemyError as e:
            logger.warning(f"[POOL] Failed to persist rate-limit state for {acct_id}: {e} — queued for retry")
            if acct_id not in _pending_rl_persists:
                _pending_rl_persists.append(acct_id)


async def clear_rate_limit_state_on_success(acct_id: str, model: str) -> bool:
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
    async with _max_plan_pool_lock:
        acct = _account_by_id.get(acct_id)
        if acct is None:
            return False
        dm = acct.get("disabled_models") or {}
        cleared = []
        for m in cascade:
            if m in dm:
                cleared.append(m)
                del dm[m]
        if not cleared:
            return False
        if not dm:
            acct.pop("disabled_models", None)
        logger.info(f"[POOL] Account {acct['name']} cleared disabled models {cleared} after successful {model} task")
        try:
            await _persist_rate_limit_state(acct_id)
        except SQLAlchemyError as e:
            logger.warning(f"[POOL] Failed to persist rate-limit clear for {acct_id}: {e} — queued for retry")
            if acct_id not in _pending_rl_persists:
                _pending_rl_persists.append(acct_id)
        return True


async def recheck_disabled_accounts():
    """Re-enable legacy full-account disables after cooldown and drain the
    persist-retry queue. Per-model disables are NOT auto-deleted here — the
    state machine in _account_state_for_model transitions them to "probing"
    lazily once cooldown elapses, and clear_rate_limit_state_on_success drops
    entries after a successful probe task."""
    from app.models import MaxPlanAccount

    changed_accounts = []
    async with _max_plan_pool_lock:
        now = _utcnow()

        for acct in _account_by_id.values():
            # Legacy full-account disable (backward compat)
            if not acct.get("enabled", True):
                disabled_at = acct.get("disabled_at")
                if not disabled_at:
                    continue
                rl_hit_count = acct.get("rl_hit_count", 1)
                cooldown_minutes = min(30 * (2 ** (rl_hit_count - 1)), 240)
                if (now - disabled_at).total_seconds() >= cooldown_minutes * 60:
                    acct["enabled"] = True
                    acct["disabled_at"] = None
                    changed_accounts.append(acct)
                    logger.warning(f"[POOL] Re-enabled account {acct['name']} after {cooldown_minutes:.0f}min cooldown (rl_hit #{rl_hit_count})")

    # Retry any previously failed rate-limit persists
    if _pending_rl_persists:
        retry_ids = list(_pending_rl_persists)
        for acct_id in retry_ids:
            acct = _account_by_id.get(acct_id)
            if acct and acct not in changed_accounts:
                changed_accounts.append(acct)

    # Persist changes to DB (outside lock to avoid holding it during IO)
    if changed_accounts:
        try:
            async with AsyncSessionLocal() as rl_session:
                for acct in changed_accounts:
                    db_acct = await rl_session.get(MaxPlanAccount, uuid.UUID(acct["id"]))
                    if db_acct:
                        db_acct.rate_limit_state = _build_rate_limit_state_blob(acct)
                        db_acct.enabled = acct.get("enabled", True)
                        if acct.get("enabled", True):
                            db_acct.disabled_reason = None
                            acct["disabled_reason"] = None
                            db_acct.disabled_at = None
                        else:
                            db_acct.disabled_at = acct.get("disabled_at")
                        rl_session.add(db_acct)
                await rl_session.commit()
            # Clear any retried accounts on success
            for acct in changed_accounts:
                if acct["id"] in _pending_rl_persists:
                    _pending_rl_persists.remove(acct["id"])
        except Exception as e:
            logger.warning(f"[POOL] Failed to persist rate-limit state changes: {e}")

    # Flush dirty usage-cap state (daily counter + auto-disable reason) to DB.
    if _pending_cap_persists:
        flush_ids = list(_pending_cap_persists)
        try:
            async with AsyncSessionLocal() as cap_session:
                for acct_id in flush_ids:
                    acct = _account_by_id.get(acct_id)
                    if not acct:
                        _pending_cap_persists.discard(acct_id)
                        continue
                    db_acct = await cap_session.get(MaxPlanAccount, uuid.UUID(acct_id))
                    if db_acct is None:
                        _pending_cap_persists.discard(acct_id)
                        continue
                    db_acct.daily_task_count = int(acct.get("daily_task_count") or 0)
                    db_acct.daily_task_count_reset_at = acct.get("daily_task_count_reset_at")
                    db_acct.disabled_reason = acct.get("disabled_reason")
                    db_acct.enabled = acct.get("enabled", True)
                    db_acct.disabled_at = acct.get("disabled_at")
                    cap_session.add(db_acct)
                await cap_session.commit()
            for acct_id in flush_ids:
                _pending_cap_persists.discard(acct_id)
        except Exception as e:
            logger.warning(f"[POOL] Failed to persist usage-cap state: {e}")
