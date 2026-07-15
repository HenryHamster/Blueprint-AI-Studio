"""Health check primitives for external dependency monitoring.

Extracted from deploy_health._check_external_health to keep deploy_health.py
under its function ceiling. Each function checks one external dependency,
updates the shared ext dict, and returns the dependency name if failed or
None if healthy.
"""

import logging
import os
from datetime import datetime, timedelta
from typing import Any, Optional

import app.shared_state as _shared_state
from app.database import AsyncSessionLocal
from app.max_plan_pool import (
    _OPUS_MODEL,
    _account_available_for_model,
    _account_by_id,
    infer_provider_for_model,
    provider_selection_mode,
)
from app.models import LLMProviderName, Task
from app.providers.registry import get_provider
from app.time_utils import _utcnow
from sqlmodel import select

logger = logging.getLogger("blueprint")

# Entries older than this in task_results are considered stale and ignored by
# health checks. This prevents an auto-pause/auto-resume deadlock: while paused
# (max_concurrent_vms=0) no new results are appended, so the last pre-pause
# failures would otherwise keep tripping checks forever.
_TASK_RESULT_STALENESS_MINUTES = 15
_PROVIDER_DEP_KEYS = {
    LLMProviderName.ANTHROPIC.value: "anthropic_api",
    LLMProviderName.OPENAI.value: "openai_api",
    LLMProviderName.GEMINI.value: "gemini_api",
    LLMProviderName.ZAI.value: "zai_api",
    LLMProviderName.DEEPSEEK.value: "deepseek_api",
}
_RAW_PROVIDER_ENV_VARS = {
    LLMProviderName.ANTHROPIC.value: ("ANTHROPIC_API_KEY",),
    LLMProviderName.OPENAI.value: ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_HOME"),
    LLMProviderName.GEMINI.value: ("GEMINI_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS"),
    LLMProviderName.ZAI.value: ("ZAI_API_KEY",),
    LLMProviderName.DEEPSEEK.value: ("DEEPSEEK_API_KEY",),
}


async def check_db_health(ext: dict) -> Optional[str]:
    """Check database connectivity. Returns 'db' if failed, None if healthy."""
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(select(Task).limit(1))
        ext["db"] = True
        ext["errors"].pop("db", None)
        return None
    except Exception as e:
        logger.warning(f"[HEALTH] DB health check failed: {e}", exc_info=True)
        ext["db"] = False
        ext["errors"]["db"] = f"{type(e).__name__}: {str(e)[:200]}"
        return "db"


async def check_github_health(ext: dict, now: datetime) -> Optional[str]:
    """Check GitHub API health using passive detection with active fallback.

    Returns 'github_api' if failed, None if healthy.
    """
    github_stale_threshold = 60  # 1 minute — detect outages faster during idle
    github_age = (
        (now - _shared_state.last_github_success).total_seconds()
        if _shared_state.last_github_success
        else None
    )

    if github_age is not None and github_age < github_stale_threshold:
        # Recent success — GitHub is healthy, no active check needed
        ext["github_api"] = True
        ext["errors"].pop("github_api", None)
        return None

    # No recent success — either idle or actually down. Make a dedicated active check
    # using a separate short-lived client to avoid connection pool contention.
    check_method = "passive" if github_age is not None else "active (cold start)"
    try:
        import httpx
        from app.github import generate_github_app_jwt

        jwt_token = generate_github_app_jwt()
        async with httpx.AsyncClient(timeout=10.0) as health_client:
            resp = await health_client.get(
                "https://api.github.com/app",
                headers={"Authorization": f"Bearer {jwt_token}"},
            )
        ext["github_api"] = resp.status_code == 200
        if ext["github_api"]:
            _shared_state.last_github_success = now
            ext["errors"].pop("github_api", None)
            return None
        else:
            logger.warning(f"[HEALTH] GitHub API returned {resp.status_code}")
            ext["errors"]["github_api"] = (
                f"HTTP {resp.status_code} (check: {check_method})"
            )
            return "github_api"
    except Exception as e:
        logger.warning(
            f"[HEALTH] GitHub API health check failed: {type(e).__name__}: {e!r}"
        )
        ext["github_api"] = False
        ext["errors"]["github_api"] = (
            f"{type(e).__name__}: {str(e)[:200]} (check: {check_method})"
        )
        return "github_api"


def _normalized_provider_name(raw: str | LLMProviderName | None) -> str | None:
    if isinstance(raw, LLMProviderName):
        return raw.value
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return None


def _provider_dep_key(provider_name: str | LLMProviderName) -> str:
    normalized = _normalized_provider_name(provider_name)
    if not normalized:
        raise ValueError("provider_name is required")
    return _PROVIDER_DEP_KEYS[normalized]


def _has_raw_provider_credentials(provider_name: str) -> bool:
    return any(
        bool(os.getenv(env_name))
        for env_name in _RAW_PROVIDER_ENV_VARS.get(provider_name, ())
    )


def _ordered_provider_list(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    ordered: list[str] = []
    for provider_name in raw:
        normalized = _normalized_provider_name(provider_name)
        if normalized and normalized not in ordered:
            ordered.append(normalized)
    return ordered


def _add_provider_candidates(
    providers: list[str], primary: set[str], all_candidates: set[str]
) -> None:
    if not providers:
        return
    primary.add(providers[0])
    all_candidates.update(providers)


def _dispatch_provider_sets() -> tuple[set[str], set[str]]:
    runtime_config = _shared_state.runtime_config
    primary: set[str] = set()
    all_candidates: set[str] = set()

    unified_present = False
    for routing_key in ("category_routing", "task_type_routing"):
        routing = runtime_config.get(routing_key, {})
        if not isinstance(routing, dict):
            continue
        for entry in routing.values():
            if not isinstance(entry, dict):
                continue
            providers = _ordered_provider_list(entry.get("providers"))
            if providers:
                unified_present = True
                _add_provider_candidates(providers, primary, all_candidates)

    if not unified_present:
        provider_preferences = runtime_config.get("task_type_provider_preference", {})
        if isinstance(provider_preferences, dict):
            for providers in provider_preferences.values():
                _add_provider_candidates(
                    _ordered_provider_list(providers), primary, all_candidates
                )

        model_preferences = runtime_config.get("task_type_model_preferences", {})
        if isinstance(model_preferences, dict):
            for prefs in model_preferences.values():
                if not isinstance(prefs, list):
                    continue
                ordered = []
                for pref in prefs:
                    if not isinstance(pref, dict):
                        continue
                    normalized = _normalized_provider_name(pref.get("provider"))
                    if normalized and normalized not in ordered:
                        ordered.append(normalized)
                _add_provider_candidates(ordered, primary, all_candidates)

        task_type_models = runtime_config.get("task_type_models", {})
        if isinstance(task_type_models, dict):
            for model_name in task_type_models.values():
                if not isinstance(model_name, str):
                    continue
                normalized = _normalized_provider_name(
                    infer_provider_for_model(model_name.strip(), runtime_config)
                )
                if normalized:
                    primary.add(normalized)
                    all_candidates.add(normalized)

    if not primary and not all_candidates:
        primary.add(LLMProviderName.ANTHROPIC.value)
        all_candidates.add(LLMProviderName.ANTHROPIC.value)

    return primary, all_candidates


def _dispatch_relevant_providers() -> set[str]:
    primary, all_candidates = _dispatch_provider_sets()
    mode = provider_selection_mode(_shared_state.runtime_config)
    if mode in ("balanced", "preferred"):
        return all_candidates
    return primary


def _settings_model_for_effort(provider_name: str, effort: Any) -> str | None:
    if not isinstance(effort, str) or not effort.strip():
        return None
    matrix = _shared_state.runtime_config.get("provider_settings_by_effort", {})
    if not isinstance(matrix, dict):
        return None
    per_provider = matrix.get(provider_name)
    if not isinstance(per_provider, dict):
        return None
    bag = per_provider.get(effort.strip())
    if not isinstance(bag, dict):
        return None
    model = bag.get("model")
    return model.strip() if isinstance(model, str) and model.strip() else None


def _configured_primary_model(provider_name: str) -> str | None:
    runtime_config = _shared_state.runtime_config
    model_preferences = runtime_config.get("task_type_model_preferences", {})
    if isinstance(model_preferences, dict):
        for prefs in model_preferences.values():
            if not isinstance(prefs, list):
                continue
            for pref in prefs:
                if not isinstance(pref, dict):
                    continue
                normalized = _normalized_provider_name(pref.get("provider"))
                model = pref.get("model")
                if (
                    normalized == provider_name
                    and isinstance(model, str)
                    and model.strip()
                ):
                    return model.strip()

    task_type_models = runtime_config.get("task_type_models", {})
    if isinstance(task_type_models, dict):
        for model_name in task_type_models.values():
            if not isinstance(model_name, str) or not model_name.strip():
                continue
            normalized = _normalized_provider_name(
                infer_provider_for_model(model_name.strip(), runtime_config)
            )
            if normalized == provider_name:
                return model_name.strip()

    for routing_key in ("task_type_routing", "category_routing"):
        routing = runtime_config.get(routing_key, {})
        if not isinstance(routing, dict):
            continue
        for entry in routing.values():
            if not isinstance(entry, dict):
                continue
            if provider_name not in _ordered_provider_list(entry.get("providers")):
                continue
            model = _settings_model_for_effort(provider_name, entry.get("effort"))
            if model:
                return model
    return None


def _provider_rate_limit_bursting(provider_name: str) -> bool:
    if provider_name != LLMProviderName.ANTHROPIC.value:
        return False
    recent_results = list(_shared_state.deploy_monitoring["task_results"])[-5:]
    # Audit C10: use _utcnow() (naive UTC) so the ISO cutoff string matches the
    # naive timestamps record_task_result() writes into task_results — an aware
    # cutoff sorts lexically above same-instant naive entries and undercounts at
    # the boundary.
    cutoff = (_utcnow() - timedelta(minutes=_TASK_RESULT_STALENESS_MINUTES)).isoformat()
    fresh_results = [r for r in recent_results if r[0] >= cutoff]
    rate_limit_count = sum(1 for r in fresh_results if r[2] == "RATE_LIMIT")
    return rate_limit_count >= 3


def get_provider_health_snapshot(provider_name: str | LLMProviderName) -> dict:
    normalized = _normalized_provider_name(provider_name)
    if not normalized:
        raise ValueError("provider_name is required")
    provider = get_provider(normalized)
    accounts = [
        account
        for account in _account_by_id.values()
        if (
            _normalized_provider_name(account.get("provider"))
            or LLMProviderName.ANTHROPIC.value
        )
        == normalized
    ]
    raw_credentials = _has_raw_provider_credentials(normalized)
    relevant_providers = _dispatch_relevant_providers()
    dispatch_relevant = normalized in relevant_providers
    configured = dispatch_relevant or bool(accounts) or raw_credentials
    primary_model = (
        _configured_primary_model(normalized)
        or provider.tier_to_model("opus", _shared_state.runtime_config)
        or _OPUS_MODEL
    )

    enabled_accounts = [account for account in accounts if account.get("enabled", True)]
    ready_accounts = [
        account
        for account in enabled_accounts
        if provider.account_has_credentials(account)
    ]
    dispatchable_accounts = [
        account
        for account in ready_accounts
        if _account_available_for_model(account, primary_model)
    ]

    status = "inactive"
    reason = None
    if configured and accounts:
        if not ready_accounts:
            status = "down"
            reason = "no enabled accounts with credentials"
        elif not dispatchable_accounts:
            status = "down"
            reason = f"no accounts available for {primary_model}"
        elif len(dispatchable_accounts) < len(ready_accounts):
            status = "degraded"
            reason = f"{len(ready_accounts) - len(dispatchable_accounts)} account(s) unavailable for {primary_model}"
        else:
            status = "ok"
    elif configured and raw_credentials:
        if _provider_rate_limit_bursting(normalized):
            status = "down"
            reason = "recent rate-limit burst detected for raw provider credentials"
        else:
            status = "ok"
    elif configured:
        status = "down"
        reason = "provider selected for dispatch but no credentials are configured"

    return {
        "provider": normalized,
        "dep_name": _provider_dep_key(normalized),
        "configured": configured,
        "dispatch_relevant": dispatch_relevant,
        "status": status,
        "reason": reason,
        "has_raw_credentials": raw_credentials,
        "total_accounts": len(accounts),
        "enabled_accounts": len(enabled_accounts),
        "ready_accounts": len(ready_accounts),
        "available_accounts": len(dispatchable_accounts),
        "primary_model": primary_model,
        "dispatch_available": status in ("ok", "degraded"),
    }


def check_provider_api_health(
    ext: dict, provider_name: str | LLMProviderName
) -> Optional[str]:
    snapshot = get_provider_health_snapshot(provider_name)
    provider_key = snapshot["provider"]
    dep_key = snapshot["dep_name"]
    ext.setdefault("provider_health", {})[provider_key] = snapshot
    ext[dep_key] = snapshot["status"] != "down"
    if snapshot["status"] == "down" and snapshot["configured"]:
        ext.setdefault("errors", {})[dep_key] = (
            snapshot["reason"] or "provider unavailable"
        )
        return dep_key
    ext.setdefault("errors", {}).pop(dep_key, None)
    return None


def get_dispatch_health_snapshot(ext: dict) -> dict:
    relevant_providers = sorted(_dispatch_relevant_providers())
    provider_health = ext.get("provider_health", {}) or {}
    available = []
    degraded = []
    down = []
    inactive = []
    for provider_name in relevant_providers:
        snapshot = provider_health.get(provider_name) or get_provider_health_snapshot(
            provider_name
        )
        status = snapshot.get("status", "inactive")
        if status in ("ok", "degraded"):
            available.append(provider_name)
        if status == "degraded":
            degraded.append(provider_name)
        elif status == "down":
            down.append(provider_name)
        elif status == "inactive":
            inactive.append(provider_name)

    if not relevant_providers:
        status = "down"
    elif not available:
        status = "down"
    elif degraded or down:
        status = "degraded"
    else:
        status = "ok"

    return {
        "status": status,
        "fallback_enabled": provider_selection_mode(_shared_state.runtime_config)
        in ("balanced", "preferred"),
        "required_providers": relevant_providers,
        "available_providers": available,
        "degraded_providers": degraded,
        "down_providers": down,
        "inactive_providers": inactive,
    }


def check_gcp_health(ext: dict) -> Optional[str]:
    """Check GCP health via recent provisioning success/failure tracking.

    Local-runner failures can share the broad ``PROVISION_FAILURE`` operational
    class (for example a provider CLI that cannot start inside bwrap), but they
    are not evidence about GCP. Route metadata recorded alongside task results
    keeps those samples out of both the provision-failure count and its
    corroborating recent-failure window.

    Returns 'gcp' if failed, None if healthy.
    """
    # Audit C10: naive UTC cutoff to match record_task_result()'s naive ISO
    # timestamps (see _provider_rate_limit_bursting above).
    cutoff = (_utcnow() - timedelta(minutes=_TASK_RESULT_STALENESS_MINUTES)).isoformat()
    fresh_all = [
        r for r in _shared_state.deploy_monitoring["task_results"] if r[0] >= cutoff
    ]
    metadata = _shared_state.deploy_monitoring.get("task_result_metadata", {})
    fresh_cloud = [
        r
        for r in fresh_all
        if metadata.get(r[0], {}).get("backend") != "local_runner"
    ]
    recent_provisions = [r for r in fresh_cloud if r[2] == "PROVISION_FAILURE"]
    if len(recent_provisions) >= 3:
        # Check if ALL recent failures are provision failures (guard against empty filter)
        recent_all = fresh_cloud[-5:]
        recent_failures = [r for r in recent_all if r[1] == "failure"]
        if recent_failures and all(
            r[2] == "PROVISION_FAILURE" for r in recent_failures
        ):
            ext["gcp"] = False
            return "gcp"
    ext["gcp"] = True
    return None


def log_zone_health_transitions(
    current_exhausted: set,
    previous_exhausted: set,
    zone_summary: dict,
) -> None:
    """Log zone_health_exhausted and zone_health_recovered events for zone transitions."""
    available_zones = zone_summary.get("available", 0)
    total_zones = zone_summary.get("total_zones", 0)
    for zone in current_exhausted - previous_exhausted:
        logger.warning(
            "zone_health_exhausted",
            extra={
                "zone": zone,
                "available_zones": available_zones,
                "total_zones": total_zones,
            },
        )
    for zone in previous_exhausted - current_exhausted:
        logger.info(
            "zone_health_recovered",
            extra={
                "zone": zone,
                "available_zones": available_zones,
                "total_zones": total_zones,
            },
        )
