"""Global deployment settings and operations endpoints (admin router).

Backs the admin Settings page and operator maintenance actions: reading and
patching deployment-wide configuration (including LLM provider/model and auth
mode), listing available providers, browsing GitHub repos and branches,
refreshing GitHub quota, restarting the orchestrator, and the tracemalloc
memory-profiling controls. External URLs are validated via ``url_security``.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select
from typing import Dict, Any
import asyncio
import os
import uuid
import httpx
import logging
from datetime import timedelta
from sqlalchemy.orm import aliased

from app.database import get_session
from app.url_security import _is_safe_external_url
from app.shared_state import limiter, circuit_breakers as _circuit_breakers
from app.models import (
    DeploymentState,
    DeploymentStatus,
    LLMAuthMode,
    LLMProviderName,
    Task,
    TaskStatus,
    UserIntegration,
    _utcnow,
)
from app.task_locking import deployment_state_lock
from app.auth import get_current_user, require_admin
from app.github import generate_installation_access_token
from app.max_plan_pool import (
    provider_selection_mode as _provider_selection_mode_from_config,
)
from app.providers.registry import get_provider, list_providers
from app.task_capabilities import (
    ALL_TASK_TYPES,
    PERIODIC_TASK_TYPES,
    PROVIDER_CONFIGURABLE_TASK_TYPES,
    RESERVABLE_TASK_TYPES,
    task_capability_contract,
)

_VALID_PROVIDER_SELECTION_MODES = frozenset({"balanced", "preferred", "exclusive"})


def _reject_unknown_task_types(
    field: str, keys, allowed: frozenset[str] | set[str] | tuple[str, ...]
) -> None:
    """Reject a task-type-keyed settings map whose keys aren't registry-allowed.

    ``allowed`` is the registry-derived set of task types permitted for THIS
    surface (e.g. ``ALL_TASK_TYPES`` for categorization, the provider-configurable
    subset for routing, ``RESERVABLE_TASK_TYPES`` for reserved slots). Keys are
    checked against it so a typoed or unknown task type is a 400 instead of a
    silently-stored no-op, and a surface only accepts types whose declared
    capability flags permit that usage. Deriving ``allowed`` from the registry
    means a newly registered task type is honored automatically — no per-surface
    allowlist edit required.
    """
    allowed_set = frozenset(allowed)
    invalid = sorted(str(k) for k in keys if str(k) not in allowed_set)
    if invalid:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{field}: unknown or ineligible task type(s) {invalid}; "
                f"must be one of {sorted(allowed_set)}"
            ),
        )

# Every runtime-config key the PATCH /api/admin/settings handler (_apply_settings)
# knows how to validate + apply. A payload key NOT in this set is a typo or a
# stale field name — _apply_settings logs it as a WARNING and rejects the whole
# request (400) rather than silently dropping it, so a misconfigured admin call
# is detectable instead of vanishing. Keep this in sync with the `"<key>" in
# payload` branches below; test_admin_settings asserts the round-trip.
_KNOWN_SETTINGS_KEYS = frozenset(
    {
        "agent_prompt_suffixes",
        "architecture_review_frequency",
        "auto_archive_enabled",
        "auto_archive_interval_minutes",
        "auto_archive_max_terminal_tasks_per_project",
        "auto_archive_terminal_after_days",
        "autonomous_direction_cooldown_hours",
        "autonomous_direction_cooldown_minutes",
        "autonomous_periodic_tasks",
        "autonomous_research_interval_minutes",
        "category_routing",
        "ci_backstop_timeout_minutes",
        "cochange_decomposition_enabled",
        "cross_provider_fallback_enabled",
        "daily_cost_ceiling_usd",
        "drift_over_claim_consistency_threshold",
        "drift_over_claim_min_claims",
        "drift_stall_min_claims",
        "evaluating_fail_hours",
        "evaluating_pipeline_timeout_minutes",
        "health_check_urls",
        "max_active_directions",
        "max_autonomous_tasks_per_hour",
        "max_concurrent_auto_remediations",
        "max_concurrent_vms",
        "max_global_autonomous_tasks_per_hour",
        "max_parallel_research_per_project",
        "max_pipeline_slots",
        "max_provision_retries",
        "max_retries",
        "max_rollup_retries",
        "max_total_research_per_project",
        "next_role_poll_timeout_secs",
        "notification_config",
        "productive_direction_cooldown_minutes",
        "promotion_enabled",
        "promotion_enabled_by_project",
        "promotion_threshold",
        "promotion_weights",
        "provider_selection_mode",
        "provider_settings_by_effort",
        "provider_settings_defaults",
        "provider_tier_model_map",
        "require_approval_gates",
        "reserved_slots_for_types",
        "residual_audit_defer_escalation_threshold",
        "residual_remediation_cooldown_hours",
        "smoketest_enabled",
        "strategic_max_interval_minutes",
        "strategic_research_frequency",
        "task_type_categories",
        "task_type_model_preferences",
        "task_type_provider_overrides",
        "task_type_routing",
        # Side-effect-only key: fires a test webhook, never stored.
        "test_notification",
        "warm_backlog_reuse_enabled",
        "warm_backlog_reuse_cooldown_seconds",
        "warm_backlog_reuse_failure_rate_threshold",
        "warm_backlog_scan_limit",
        "warm_backlog_teardown_defer_secs",
        "warm_continuity_enabled",
        "warm_continuity_cooldown_seconds",
        "warm_pin_clear_rate_threshold",
        "warm_pool_enabled",
        "warm_pool_project_allowlist",
        "warm_pool_target_size",
        "warm_pool_target_size_by_project",
        "warm_pool_idle_ttl_secs",
        "warm_pool_account_retarget_enabled",
        "warm_pool_cross_provider_retarget_enabled",
    }
)

logger = logging.getLogger(__name__)


def _normalize_warm_pool_allowlist(raw) -> set[str]:
    """Coerce a warm-pool allowlist value to a set of trimmed, non-empty ids.

    Mirrors ``warm_pool.warm_pool_project_scope`` so the admin surface and the
    maintainer agree on what counts as "scoped": accepts a list or a single
    scalar, stringifies every entry, and drops blanks.
    """
    if isinstance(raw, str):
        raw = raw.split(",")
    if not isinstance(raw, (list, tuple, set)):
        raw = [raw]
    return {str(p).strip() for p in raw if str(p).strip()}


def _normalize_warm_pool_target_map(raw) -> dict[str, int]:
    """Coerce a per-project warm-pool target map to ``{project_id: int}``.

    Mirrors ``warm_pool.warm_pool_project_target_map`` (and the config-sync
    normalizer) so the admin surface and the maintainer agree on the per-project
    targets: trimmed, non-empty string keys mapping to integer values clamped to
    the same 0..50 range the global target uses, dropping malformed entries. A
    non-dict input yields an empty map.
    """
    if not isinstance(raw, dict):
        return {}
    out: dict[str, int] = {}
    for key, value in raw.items():
        sid = str(key).strip()
        if not sid:
            continue
        try:
            ival = int(value)
        except (TypeError, ValueError):
            continue
        out[sid] = max(0, min(50, ival))
    return out


def _build_provider_catalog(runtime_config: Dict[str, Any]) -> dict[str, Any]:  # noqa: ARG001
    catalog: dict[str, Any] = {}
    for provider in list_providers():
        catalog[provider.name.value] = {
            "auth_modes": {
                mode.value: {
                    "required_metadata_fields": list(
                        provider.required_metadata_fields(mode)
                    ),
                }
                for mode in provider.supported_auth_modes
            },
            # Schema for the per-provider settings bag (consumed by the admin UI
            # to render dynamic knob editors). Shape is provider-defined; see
            # providers/*.py:settings_schema().
            "settings_schema": provider.settings_schema(),
        }
    return catalog


router = APIRouter(
    prefix="/api/admin",
    tags=["admin-settings"],
    dependencies=[Depends(get_current_user)],
)


def _build_notification_config_response(config: dict, *, is_admin: bool) -> dict:
    """Return notification config, masking webhook_url for non-admin callers.

    Webhook URLs (Slack, PagerDuty, Discord) embed auth tokens, so they must
    not be returned to non-admin users. Non-admins receive a boolean flag
    indicating whether a URL is configured instead.
    """
    if is_admin:
        return config
    return {
        "webhook_url": "",
        "webhook_url_configured": bool(config.get("webhook_url")),
        "enabled_events": config.get(
            "enabled_events",
            [
                "TASK_FAILED",
                "APPROVAL_NEEDED",
                "DEPLOY_HEALTH_ALERT",
                "REMEDIATION_ESCALATED",
                "DIRECTION_MILESTONE",
                "RESIDUAL_FIX_ESCALATED",
                "DIRECTION_BURN_PAUSE",
                "MERGE_UNVERIFIED",
            ],
        ),
        "throttle_seconds": config.get("throttle_seconds", 30),
    }


# ---------------------------------------------------------------------------
# Settings endpoints
# ---------------------------------------------------------------------------


@router.get("/settings")
async def get_settings(
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
):
    from app.shared_state import runtime_config as _runtime_config
    from app.max_plan_pool import _max_plan_pools
    from app.models import UserIntegration

    # Check if the user has a GitHub integration
    github_account = None
    stmt = select(UserIntegration).where(
        UserIntegration.user_id == current_user["user_id"]
    )
    integration = (await session.execute(stmt)).scalars().first()
    if integration and integration.github_account_name:
        github_account = integration.github_account_name

    user_pool = _max_plan_pools.get(current_user["user_id"], [])
    return {
        "max_concurrent_vms": _runtime_config["max_concurrent_vms"],
        "max_retries": _runtime_config["max_retries"],
        "max_autonomous_tasks_per_hour": _runtime_config[
            "max_autonomous_tasks_per_hour"
        ],
        "max_global_autonomous_tasks_per_hour": _runtime_config.get(
            "max_global_autonomous_tasks_per_hour", 50
        ),
        "autonomous_research_interval_minutes": _runtime_config.get(
            "autonomous_research_interval_minutes", 30
        ),
        "max_concurrent_auto_remediations": _runtime_config.get(
            "max_concurrent_auto_remediations", 2
        ),
        "residual_remediation_cooldown_hours": _runtime_config.get(
            "residual_remediation_cooldown_hours", 24
        ),
        "residual_audit_defer_escalation_threshold": int(
            _runtime_config.get("residual_audit_defer_escalation_threshold", 5)
        ),
        "provider_tier_model_map": _runtime_config.get("provider_tier_model_map", {}),
        "task_type_categories": _runtime_config.get("task_type_categories", {}),
        "category_routing": _runtime_config.get("category_routing", {}),
        "task_type_routing": _runtime_config.get("task_type_routing", {}),
        "task_type_model_preferences": _runtime_config.get(
            "task_type_model_preferences", {}
        ),
        "provider_selection_mode": _provider_selection_mode_from_config(
            _runtime_config
        ),
        "cross_provider_fallback_enabled": _provider_selection_mode_from_config(
            _runtime_config
        )
        in ("balanced", "preferred"),
        "providers": _build_provider_catalog(_runtime_config),
        # Published, backend-owned task-capability contract. Operator/agent
        # surfaces consume this map instead of maintaining private task-type
        # tables; it is purely additive so existing settings consumers are
        # unaffected. Also available standalone at GET /api/admin/task-capabilities.
        "task_capabilities": task_capability_contract(),
        "provider_settings_defaults": _runtime_config.get(
            "provider_settings_defaults", {}
        ),
        "provider_settings_by_effort": _runtime_config.get(
            "provider_settings_by_effort", {}
        ),
        "task_type_provider_overrides": _runtime_config.get(
            "task_type_provider_overrides", {}
        ),
        "health_check_urls": _runtime_config.get("health_check_urls", []),
        "agent_prompt_suffixes": _runtime_config.get("agent_prompt_suffixes", {}),
        "autonomous_periodic_tasks": _runtime_config.get(
            "autonomous_periodic_tasks", {}
        ),
        "autonomous_direction_cooldown_minutes": _runtime_config.get(
            "autonomous_direction_cooldown_minutes", 240
        ),
        "productive_direction_cooldown_minutes": _runtime_config.get(
            "productive_direction_cooldown_minutes", 45
        ),
        "max_parallel_research_per_project": _runtime_config.get(
            "max_parallel_research_per_project", 3
        ),
        "max_total_research_per_project": _runtime_config.get(
            "max_total_research_per_project", 5
        ),
        "evaluating_pipeline_timeout_minutes": _runtime_config.get(
            "evaluating_pipeline_timeout_minutes", 120
        ),
        "evaluating_fail_hours": _runtime_config.get("evaluating_fail_hours", 48),
        "ci_backstop_timeout_minutes": _runtime_config.get(
            "ci_backstop_timeout_minutes", 60
        ),
        "promotion_enabled": bool(_runtime_config.get("promotion_enabled", False)),
        "promotion_enabled_by_project": {
            str(k): bool(v)
            for k, v in (
                _runtime_config.get("promotion_enabled_by_project") or {}
            ).items()
        },
        "smoketest_enabled": bool(_runtime_config.get("smoketest_enabled", False)),
        "promotion_threshold": _runtime_config.get("promotion_threshold"),
        "promotion_weights": _runtime_config.get("promotion_weights"),
        "warm_continuity_enabled": bool(
            _runtime_config.get("warm_continuity_enabled", False)
        ),
        "warm_backlog_reuse_enabled": bool(
            _runtime_config.get("warm_backlog_reuse_enabled", False)
        ),
        "cochange_decomposition_enabled": bool(
            _runtime_config.get("cochange_decomposition_enabled", False)
        ),
        "warm_pool_enabled": bool(_runtime_config.get("warm_pool_enabled", False)),
        "warm_pool_account_retarget_enabled": bool(
            _runtime_config.get("warm_pool_account_retarget_enabled", False)
        ),
        "warm_pool_cross_provider_retarget_enabled": bool(
            _runtime_config.get("warm_pool_cross_provider_retarget_enabled", False)
        ),
        "warm_pool_project_allowlist": sorted(
            _normalize_warm_pool_allowlist(
                _runtime_config.get("warm_pool_project_allowlist") or []
            )
        ),
        "warm_pool_target_size": int(
            _runtime_config.get("warm_pool_target_size", 2)
        ),
        "warm_pool_target_size_by_project": _normalize_warm_pool_target_map(
            _runtime_config.get("warm_pool_target_size_by_project") or {}
        ),
        "warm_pool_idle_ttl_secs": int(
            _runtime_config.get("warm_pool_idle_ttl_secs", 1800)
        ),
        "warm_pin_clear_rate_threshold": int(
            _runtime_config.get("warm_pin_clear_rate_threshold", 10)
        ),
        "warm_continuity_cooldown_seconds": int(
            _runtime_config.get("warm_continuity_cooldown_seconds", 1800)
        ),
        "warm_backlog_reuse_failure_rate_threshold": int(
            _runtime_config.get("warm_backlog_reuse_failure_rate_threshold", 5)
        ),
        "warm_backlog_reuse_cooldown_seconds": int(
            _runtime_config.get("warm_backlog_reuse_cooldown_seconds", 1800)
        ),
        "warm_backlog_scan_limit": int(
            _runtime_config.get("warm_backlog_scan_limit", 10)
        ),
        "warm_backlog_teardown_defer_secs": int(
            _runtime_config.get("warm_backlog_teardown_defer_secs", 20)
        ),
        "next_role_poll_timeout_secs": int(
            _runtime_config.get("next_role_poll_timeout_secs", 180)
        ),
        "drift_over_claim_min_claims": int(
            _runtime_config.get("drift_over_claim_min_claims", 3)
        ),
        "drift_over_claim_consistency_threshold": float(
            _runtime_config.get("drift_over_claim_consistency_threshold", 0.4)
        ),
        "drift_stall_min_claims": int(_runtime_config.get("drift_stall_min_claims", 5)),
        "reserved_slots_for_types": _runtime_config.get(
            "reserved_slots_for_types", {"create": 1, "research": 1}
        ),
        "max_pipeline_slots": _runtime_config.get("max_pipeline_slots", 4),
        "strategic_research_frequency": _runtime_config.get(
            "strategic_research_frequency", 6
        ),
        "github_account": github_account,
        "require_approval_gates": _runtime_config.get("require_approval_gates", True),
        "daily_cost_ceiling_usd": _runtime_config.get("daily_cost_ceiling_usd", 0),
        "auto_archive_enabled": _runtime_config.get("auto_archive_enabled", True),
        "auto_archive_terminal_after_days": _runtime_config.get(
            "auto_archive_terminal_after_days", 7
        ),
        "auto_archive_max_terminal_tasks_per_project": _runtime_config.get(
            "auto_archive_max_terminal_tasks_per_project", 200
        ),
        "auto_archive_interval_minutes": _runtime_config.get(
            "auto_archive_interval_minutes", 60
        ),
        "max_provision_retries": _runtime_config.get("max_provision_retries", 3),
        "max_rollup_retries": _runtime_config.get("max_rollup_retries", 5),
        "architecture_review_frequency": _runtime_config.get(
            "architecture_review_frequency", 3
        ),
        "strategic_max_interval_minutes": _runtime_config.get(
            "strategic_max_interval_minutes", 180
        ),
        "max_active_directions": _runtime_config.get("max_active_directions", 10),
        "webhook_url_configured": bool(os.getenv("PIPELINE_WEBHOOK_URL")),
        "notification_config": _build_notification_config_response(
            _runtime_config.get(
                "notification_config",
                {
                    "webhook_url": "",
                    "enabled_events": [
                        "TASK_FAILED",
                        "APPROVAL_NEEDED",
                        "DEPLOY_HEALTH_ALERT",
                        "REMEDIATION_ESCALATED",
                        "DIRECTION_MILESTONE",
                        "RESIDUAL_FIX_ESCALATED",
                        "DIRECTION_BURN_PAUSE",
                        "MERGE_UNVERIFIED",
                    ],
                    "throttle_seconds": 30,
                },
            ),
            is_admin=current_user.get("role") == "admin",
        ),
        "using_max_plan": len(
            [
                a
                for a in user_pool
                if a.get("enabled", True)
                and a.get("provider", LLMProviderName.ANTHROPIC.value)
                == LLMProviderName.ANTHROPIC.value
                and a.get("auth_mode", LLMAuthMode.OAUTH.value)
                == LLMAuthMode.OAUTH.value
            ]
        )
        > 0,
        "max_plan_accounts": [
            {
                "id": a["id"],
                "name": a["name"],
                "enabled": a.get("enabled", True),
                "provider": a.get("provider", LLMProviderName.ANTHROPIC.value),
                "auth_mode": a.get("auth_mode", LLMAuthMode.OAUTH.value),
                "provider_metadata": a.get("provider_metadata", {}),
                "active_tasks": a["active_tasks"],
                "total_tasks": a["total_tasks"],
                "token_created_at": a.get("token_created_at"),
                "rl_hit_count": a.get("rl_hit_count", 0),
                "daily_task_cap": a.get("daily_task_cap"),
                "max_concurrent_tasks": a.get("max_concurrent_tasks"),
                "auto_disable_after_rate_limits": a.get(
                    "auto_disable_after_rate_limits"
                ),
                "daily_task_count": int(a.get("daily_task_count") or 0),
                "daily_task_count_reset_at": (
                    a["daily_task_count_reset_at"].isoformat()
                    if a.get("daily_task_count_reset_at")
                    else None
                ),
                "disabled_reason": a.get("disabled_reason"),
                "disabled_models": {
                    model: {
                        "hits": info.get("hits", 0),
                        "at": info["at"].isoformat() if info.get("at") else None,
                    }
                    for model, info in a.get("disabled_models", {}).items()
                },
            }
            for a in user_pool
        ],
    }


@router.get("/providers")
async def get_provider_catalog(
    current_user: dict = Depends(get_current_user),
):
    from app.shared_state import runtime_config as _runtime_config

    return {"providers": _build_provider_catalog(_runtime_config)}


@router.get("/task-capabilities")
async def get_task_capabilities(
    current_user: dict = Depends(require_admin),
):
    """Publish the authoritative task-capability contract.

    Mirrors the ``task_capabilities`` key embedded in GET /settings, exposed as
    a standalone, backend-owned endpoint so operator and agent surfaces can fetch
    the capability contract without pulling the full settings payload.
    """
    return {"task_capabilities": task_capability_contract()}


@router.patch("/settings")
@limiter.limit("30/minute")
async def update_settings(
    request: Request,
    payload: Dict[str, Any],
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
):
    from app.shared_state import runtime_config as _runtime_config, runtime_config_lock

    async with runtime_config_lock:
        return await _apply_settings(payload, session, _runtime_config)


async def _apply_settings(
    payload: Dict[str, Any], session: AsyncSession, _runtime_config: Dict
):
    # Snapshot config so we can restore on DB commit failure
    _config_snapshot = dict(_runtime_config)
    clear_pause_state = False

    # Validate the payload keys BEFORE applying anything. An unknown/misspelled
    # key was previously silently ignored — a typo like "promotion_treshold"
    # would return 200 having changed nothing, with no signal to the operator.
    # Log it as a WARNING and reject the whole request (400) so the
    # misconfiguration is detectable. (Done up front so a bad key never leaves a
    # half-applied multi-key PATCH.)
    if isinstance(payload, dict):
        unknown_keys = sorted(k for k in payload if k not in _KNOWN_SETTINGS_KEYS)
        if unknown_keys:
            logger.warning(
                "[SETTINGS] rejected PATCH /api/admin/settings — unknown config "
                "key(s) %s (not in the validated settings allowlist); rejecting "
                "rather than silently ignoring",
                unknown_keys,
            )
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Unknown settings key(s): {unknown_keys}. "
                    "Misspelled or stale field name; nothing was changed."
                ),
            )

    if "max_concurrent_vms" in payload:
        try:
            val = int(payload["max_concurrent_vms"])
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=400, detail="max_concurrent_vms must be a number"
            )
        if not (0 <= val <= 100):
            raise HTTPException(
                status_code=400, detail="max_concurrent_vms must be between 0 and 100"
            )
        _runtime_config["max_concurrent_vms"] = val
        # Clear PAUSED/HALTED state if user explicitly sets VMs > 0
        # Otherwise the next cold start will override back to 0
        if val > 0:
            clear_pause_state = True
    if "max_retries" in payload:
        try:
            val = int(payload["max_retries"])
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="max_retries must be a number")
        if not (0 <= val <= 10):
            raise HTTPException(
                status_code=400, detail="max_retries must be between 0 and 10"
            )
        _runtime_config["max_retries"] = val
    if "max_autonomous_tasks_per_hour" in payload:
        try:
            val = int(payload["max_autonomous_tasks_per_hour"])
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=400, detail="max_autonomous_tasks_per_hour must be a number"
            )
        if not (1 <= val <= 50):
            raise HTTPException(
                status_code=400,
                detail="max_autonomous_tasks_per_hour must be between 1 and 50",
            )
        _runtime_config["max_autonomous_tasks_per_hour"] = val
    if "max_global_autonomous_tasks_per_hour" in payload:
        try:
            val = int(payload["max_global_autonomous_tasks_per_hour"])
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=400,
                detail="max_global_autonomous_tasks_per_hour must be a number",
            )
        if not (1 <= val <= 500):
            raise HTTPException(
                status_code=400,
                detail="max_global_autonomous_tasks_per_hour must be between 1 and 500",
            )
        _runtime_config["max_global_autonomous_tasks_per_hour"] = val
    if "autonomous_research_interval_minutes" in payload:
        try:
            val = int(payload["autonomous_research_interval_minutes"])
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=400,
                detail="autonomous_research_interval_minutes must be a number",
            )
        if not (1 <= val <= 1440):
            raise HTTPException(
                status_code=400,
                detail="autonomous_research_interval_minutes must be between 1 and 1440",
            )
        _runtime_config["autonomous_research_interval_minutes"] = val
    if "evaluating_pipeline_timeout_minutes" in payload:
        try:
            val = int(payload["evaluating_pipeline_timeout_minutes"])
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=400,
                detail="evaluating_pipeline_timeout_minutes must be a number",
            )
        if not (1 <= val <= 1440):
            raise HTTPException(
                status_code=400,
                detail="evaluating_pipeline_timeout_minutes must be between 1 and 1440",
            )
        _runtime_config["evaluating_pipeline_timeout_minutes"] = val
    if "ci_backstop_timeout_minutes" in payload:
        try:
            val = int(payload["ci_backstop_timeout_minutes"])
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=400, detail="ci_backstop_timeout_minutes must be a number"
            )
        if not (1 <= val <= 1440):
            raise HTTPException(
                status_code=400,
                detail="ci_backstop_timeout_minutes must be between 1 and 1440",
            )
        _runtime_config["ci_backstop_timeout_minutes"] = val
    if "promotion_enabled" in payload:
        _runtime_config["promotion_enabled"] = bool(payload["promotion_enabled"])
    if "smoketest_enabled" in payload:
        _runtime_config["smoketest_enabled"] = bool(payload["smoketest_enabled"])
    if "promotion_enabled_by_project" in payload:
        raw = payload["promotion_enabled_by_project"]
        # Per-project live-promotion overrides: a mapping of project-id strings to
        # booleans. An empty map means every project falls back to the fleet-wide
        # promotion_enabled. Validated strictly (reject 400 on a non-mapping or an
        # empty key) then normalized to bools before storing.
        if not isinstance(raw, dict):
            raise HTTPException(
                status_code=400,
                detail=(
                    "promotion_enabled_by_project must be a mapping of "
                    "project-id strings to booleans"
                ),
            )
        cleaned_promo: dict[str, bool] = {}
        for key, value in raw.items():
            sid = str(key).strip()
            if not sid:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "promotion_enabled_by_project keys must be "
                        "non-empty project ids"
                    ),
                )
            cleaned_promo[sid] = bool(value)
        _runtime_config["promotion_enabled_by_project"] = cleaned_promo
    if "promotion_threshold" in payload:
        try:
            val = float(payload["promotion_threshold"])
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=400, detail="promotion_threshold must be a number"
            )
        # The composite confidence is in [0, 1]; the threshold must live there
        # too. A threshold of 0 promotes everything that clears the hard
        # blockers and >1 can never be reached (gate stuck closed) — both are
        # almost certainly operator error, so bound it to (0, 1].
        if not (0.0 < val <= 1.0):
            raise HTTPException(
                status_code=400,
                detail="promotion_threshold must be between 0.0 (exclusive) and 1.0",
            )
        _runtime_config["promotion_threshold"] = round(val, 4)
    if "promotion_weights" in payload:
        val = payload["promotion_weights"]
        if not isinstance(val, dict):
            raise HTTPException(
                status_code=400, detail="promotion_weights must be a dict"
            )
        _PROMOTION_WEIGHT_KEYS = ("audit", "test_erosion", "smoketest")
        if set(val.keys()) != set(_PROMOTION_WEIGHT_KEYS):
            raise HTTPException(
                status_code=400,
                detail=(
                    "promotion_weights must have exactly the keys "
                    f"{sorted(_PROMOTION_WEIGHT_KEYS)}"
                ),
            )
        cleaned_weights: Dict[str, float] = {}
        for wk in _PROMOTION_WEIGHT_KEYS:
            try:
                wv = float(val[wk])
            except (ValueError, TypeError):
                raise HTTPException(
                    status_code=400,
                    detail=f"promotion_weights[{wk}] must be a number",
                )
            if wv < 0.0:
                raise HTTPException(
                    status_code=400,
                    detail=f"promotion_weights[{wk}] must be non-negative",
                )
            cleaned_weights[wk] = wv
        # Weights decide what reaches production main: a sum != 1.0 either
        # saturates confidence (gate effectively disabled) or makes it
        # unreachable (gate stuck closed). Reject a mis-weighted set rather than
        # silently honour it — same invariant release_qualifier._load_policy
        # enforces on the YAML file.
        weight_sum = sum(cleaned_weights.values())
        if abs(weight_sum - 1.0) > 0.01:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"promotion_weights must sum to ~1.0 (got {weight_sum:.4f})"
                ),
            )
        _runtime_config["promotion_weights"] = {
            wk: round(cleaned_weights[wk], 4) for wk in _PROMOTION_WEIGHT_KEYS
        }
    if "evaluating_fail_hours" in payload:
        try:
            val = int(payload["evaluating_fail_hours"])
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=400, detail="evaluating_fail_hours must be a number"
            )
        # Cap at 168h (1 week) — protects against accidental "1000h" typos
        # while leaving plenty of room above the 48h default.
        if not (1 <= val <= 168):
            raise HTTPException(
                status_code=400,
                detail="evaluating_fail_hours must be between 1 and 168",
            )
        _runtime_config["evaluating_fail_hours"] = val
    if "max_total_research_per_project" in payload:
        try:
            val = int(payload["max_total_research_per_project"])
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=400,
                detail="max_total_research_per_project must be a number",
            )
        if not (1 <= val <= 50):
            raise HTTPException(
                status_code=400,
                detail="max_total_research_per_project must be between 1 and 50",
            )
        _runtime_config["max_total_research_per_project"] = val
    if "max_concurrent_auto_remediations" in payload:
        try:
            val = int(payload["max_concurrent_auto_remediations"])
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=400,
                detail="max_concurrent_auto_remediations must be a number",
            )
        if not (0 <= val <= 20):
            raise HTTPException(
                status_code=400,
                detail="max_concurrent_auto_remediations must be between 0 and 20",
            )
        _runtime_config["max_concurrent_auto_remediations"] = val
    if "residual_remediation_cooldown_hours" in payload:
        try:
            val = int(payload["residual_remediation_cooldown_hours"])
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=400,
                detail="residual_remediation_cooldown_hours must be a number",
            )
        if not (1 <= val <= 720):
            raise HTTPException(
                status_code=400,
                detail="residual_remediation_cooldown_hours must be between 1 and 720",
            )
        _runtime_config["residual_remediation_cooldown_hours"] = val
    if "residual_audit_defer_escalation_threshold" in payload:
        try:
            val = int(payload["residual_audit_defer_escalation_threshold"])
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=400,
                detail="residual_audit_defer_escalation_threshold must be a number",
            )
        if not (1 <= val <= 50):
            raise HTTPException(
                status_code=400,
                detail="residual_audit_defer_escalation_threshold must be between 1 and 50",
            )
        _runtime_config["residual_audit_defer_escalation_threshold"] = val
    if "warm_continuity_enabled" in payload:
        val = payload["warm_continuity_enabled"]
        if not isinstance(val, bool):
            raise HTTPException(
                status_code=400, detail="warm_continuity_enabled must be a boolean"
            )
        _runtime_config["warm_continuity_enabled"] = val
    if "warm_backlog_reuse_enabled" in payload:
        val = payload["warm_backlog_reuse_enabled"]
        if not isinstance(val, bool):
            raise HTTPException(
                status_code=400,
                detail="warm_backlog_reuse_enabled must be a boolean",
            )
        _runtime_config["warm_backlog_reuse_enabled"] = val
    if "cochange_decomposition_enabled" in payload:
        val = payload["cochange_decomposition_enabled"]
        if not isinstance(val, bool):
            raise HTTPException(
                status_code=400,
                detail="cochange_decomposition_enabled must be a boolean",
            )
        _runtime_config["cochange_decomposition_enabled"] = val
    if "warm_pool_enabled" in payload:
        val = payload["warm_pool_enabled"]
        if not isinstance(val, bool):
            raise HTTPException(
                status_code=400, detail="warm_pool_enabled must be a boolean"
            )
        _runtime_config["warm_pool_enabled"] = val
    if "warm_pool_account_retarget_enabled" in payload:
        val = payload["warm_pool_account_retarget_enabled"]
        if not isinstance(val, bool):
            raise HTTPException(
                status_code=400,
                detail="warm_pool_account_retarget_enabled must be a boolean",
            )
        _runtime_config["warm_pool_account_retarget_enabled"] = val
    if "warm_pool_cross_provider_retarget_enabled" in payload:
        val = payload["warm_pool_cross_provider_retarget_enabled"]
        if not isinstance(val, bool):
            raise HTTPException(
                status_code=400,
                detail="warm_pool_cross_provider_retarget_enabled must be a boolean",
            )
        _runtime_config["warm_pool_cross_provider_retarget_enabled"] = val
    if "warm_pool_project_allowlist" in payload:
        raw = payload["warm_pool_project_allowlist"]
        # Accept a JSON list, a comma-separated string, or a single id — the
        # same shapes warm_pool.warm_pool_project_scope() tolerates. An empty
        # allowlist means fleet-wide (legacy all-or-nothing); a non-empty one
        # scopes provisioning to exactly the listed projects so the pool can be
        # rolled out one project at a time.
        if isinstance(raw, str):
            raw = raw.split(",")
        if not isinstance(raw, list):
            raise HTTPException(
                status_code=400,
                detail=(
                    "warm_pool_project_allowlist must be a list of project ids "
                    "(or a comma-separated string)"
                ),
            )
        if not all(isinstance(p, (str, int)) for p in raw):
            raise HTTPException(
                status_code=400,
                detail="warm_pool_project_allowlist entries must be strings or ints",
            )
        _runtime_config["warm_pool_project_allowlist"] = sorted(
            _normalize_warm_pool_allowlist(raw)
        )
    if "warm_pool_target_size" in payload:
        try:
            val = int(payload["warm_pool_target_size"])
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=400, detail="warm_pool_target_size must be a number"
            )
        # 0 keeps the flag-on pool empty (maintainer provisions nothing); cap at
        # 50 so a typo can't request a runaway idle fleet.
        if not (0 <= val <= 50):
            raise HTTPException(
                status_code=400,
                detail="warm_pool_target_size must be between 0 and 50",
            )
        _runtime_config["warm_pool_target_size"] = val
    if "warm_pool_target_size_by_project" in payload:
        raw = payload["warm_pool_target_size_by_project"]
        # Per-project IDLE target overrides: a mapping of project-id strings to
        # integers in the same 0..50 range as the global warm_pool_target_size.
        # An empty map means every scoped project falls back to the global target.
        # Validated strictly (reject 400 on a non-mapping or an out-of-range /
        # non-integer value) — consistent with the other warm-pool validators —
        # then normalized via the shared helper before storing.
        if not isinstance(raw, dict):
            raise HTTPException(
                status_code=400,
                detail=(
                    "warm_pool_target_size_by_project must be a mapping of "
                    "project-id strings to integers"
                ),
            )
        cleaned_targets: dict[str, int] = {}
        for key, value in raw.items():
            sid = str(key).strip()
            if not sid:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "warm_pool_target_size_by_project keys must be "
                        "non-empty project ids"
                    ),
                )
            try:
                ival = int(value)
            except (ValueError, TypeError):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"warm_pool_target_size_by_project[{sid}] must be an integer"
                    ),
                )
            if not (0 <= ival <= 50):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"warm_pool_target_size_by_project[{sid}] must be "
                        "between 0 and 50"
                    ),
                )
            cleaned_targets[sid] = ival
        _runtime_config["warm_pool_target_size_by_project"] = cleaned_targets
    if "warm_pool_idle_ttl_secs" in payload:
        try:
            val = int(payload["warm_pool_idle_ttl_secs"])
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=400, detail="warm_pool_idle_ttl_secs must be a number"
            )
        if not (60 <= val <= 86400):
            raise HTTPException(
                status_code=400,
                detail="warm_pool_idle_ttl_secs must be between 60 and 86400",
            )
        _runtime_config["warm_pool_idle_ttl_secs"] = val
        # The idle-reaper in warm_pool_maintainer.py reads the ``_seconds``
        # variant of this key; keep both in sync so an operator's TTL change
        # via this surface actually reaches the maintainer (the two names
        # coexist for back-compat — see runtime_config_state defaults).
        _runtime_config["warm_pool_idle_ttl_seconds"] = val
    if "warm_pin_clear_rate_threshold" in payload:
        try:
            val = int(payload["warm_pin_clear_rate_threshold"])
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=400,
                detail="warm_pin_clear_rate_threshold must be a number",
            )
        if not (1 <= val <= 100):
            raise HTTPException(
                status_code=400,
                detail="warm_pin_clear_rate_threshold must be between 1 and 100",
            )
        _runtime_config["warm_pin_clear_rate_threshold"] = val
    if "warm_continuity_cooldown_seconds" in payload:
        try:
            val = int(payload["warm_continuity_cooldown_seconds"])
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=400,
                detail="warm_continuity_cooldown_seconds must be a number",
            )
        if not (60 <= val <= 86400):
            raise HTTPException(
                status_code=400,
                detail="warm_continuity_cooldown_seconds must be between 60 and 86400",
            )
        _runtime_config["warm_continuity_cooldown_seconds"] = val
    if "warm_backlog_reuse_failure_rate_threshold" in payload:
        try:
            val = int(payload["warm_backlog_reuse_failure_rate_threshold"])
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=400,
                detail="warm_backlog_reuse_failure_rate_threshold must be a number",
            )
        if not (1 <= val <= 100):
            raise HTTPException(
                status_code=400,
                detail=(
                    "warm_backlog_reuse_failure_rate_threshold must be between 1 and 100"
                ),
            )
        _runtime_config["warm_backlog_reuse_failure_rate_threshold"] = val
    if "warm_backlog_reuse_cooldown_seconds" in payload:
        try:
            val = int(payload["warm_backlog_reuse_cooldown_seconds"])
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=400,
                detail="warm_backlog_reuse_cooldown_seconds must be a number",
            )
        if not (60 <= val <= 86400):
            raise HTTPException(
                status_code=400,
                detail="warm_backlog_reuse_cooldown_seconds must be between 60 and 86400",
            )
        _runtime_config["warm_backlog_reuse_cooldown_seconds"] = val
    if "warm_backlog_scan_limit" in payload:
        try:
            val = int(payload["warm_backlog_scan_limit"])
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=400,
                detail="warm_backlog_scan_limit must be a number",
            )
        if not (1 <= val <= 100):
            raise HTTPException(
                status_code=400,
                detail="warm_backlog_scan_limit must be between 1 and 100",
            )
        _runtime_config["warm_backlog_scan_limit"] = val
    if "warm_backlog_teardown_defer_secs" in payload:
        try:
            val = int(payload["warm_backlog_teardown_defer_secs"])
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=400,
                detail="warm_backlog_teardown_defer_secs must be a number",
            )
        if not (5 <= val <= 120):
            raise HTTPException(
                status_code=400,
                detail="warm_backlog_teardown_defer_secs must be between 5 and 120",
            )
        _runtime_config["warm_backlog_teardown_defer_secs"] = val
    if "drift_over_claim_min_claims" in payload:
        try:
            val = int(payload["drift_over_claim_min_claims"])
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=400, detail="drift_over_claim_min_claims must be a number"
            )
        if not (1 <= val <= 50):
            raise HTTPException(
                status_code=400,
                detail="drift_over_claim_min_claims must be between 1 and 50",
            )
        _runtime_config["drift_over_claim_min_claims"] = val
    if "drift_over_claim_consistency_threshold" in payload:
        try:
            val = float(payload["drift_over_claim_consistency_threshold"])
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=400,
                detail="drift_over_claim_consistency_threshold must be a number",
            )
        if not (0.0 <= val <= 1.0):
            raise HTTPException(
                status_code=400,
                detail="drift_over_claim_consistency_threshold must be between 0.0 and 1.0",
            )
        _runtime_config["drift_over_claim_consistency_threshold"] = round(val, 3)
    if "drift_stall_min_claims" in payload:
        try:
            val = int(payload["drift_stall_min_claims"])
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=400, detail="drift_stall_min_claims must be a number"
            )
        if not (1 <= val <= 50):
            raise HTTPException(
                status_code=400,
                detail="drift_stall_min_claims must be between 1 and 50",
            )
        _runtime_config["drift_stall_min_claims"] = val
    if "next_role_poll_timeout_secs" in payload:
        try:
            val = int(payload["next_role_poll_timeout_secs"])
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=400, detail="next_role_poll_timeout_secs must be a number"
            )
        if not (30 <= val <= 600):
            raise HTTPException(
                status_code=400,
                detail="next_role_poll_timeout_secs must be between 30 and 600",
            )
        _runtime_config["next_role_poll_timeout_secs"] = val
    if "provider_tier_model_map" in payload:
        val = payload["provider_tier_model_map"]
        if not isinstance(val, dict):
            raise HTTPException(
                status_code=400, detail="provider_tier_model_map must be a dict"
            )
        valid_providers = {p.value for p in LLMProviderName}
        valid_tiers = {"opus", "sonnet", "haiku"}
        for provider_name, tier_map in val.items():
            if provider_name not in valid_providers:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid provider in provider_tier_model_map: {provider_name}",
                )
            if not isinstance(tier_map, dict):
                raise HTTPException(
                    status_code=400,
                    detail=f"provider_tier_model_map[{provider_name}] must be a dict",
                )
            for tier, model in tier_map.items():
                if tier not in valid_tiers:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Invalid tier for {provider_name}: {tier}",
                    )
                if not isinstance(model, str) or not model.strip():
                    raise HTTPException(
                        status_code=400,
                        detail=f"Model for {provider_name}/{tier} must be a non-empty string",
                    )
        _runtime_config["provider_tier_model_map"] = val
    if "task_type_categories" in payload:
        val = payload["task_type_categories"]
        if not isinstance(val, dict):
            raise HTTPException(
                status_code=400, detail="task_type_categories must be a dict"
            )
        for category, members in val.items():
            if not isinstance(category, str) or not category or category == "_default":
                raise HTTPException(
                    status_code=400,
                    detail="task_type_categories keys must be non-empty strings and not '_default'",
                )
            if not isinstance(members, list) or not all(
                isinstance(m, str) and m for m in members
            ):
                raise HTTPException(
                    status_code=400,
                    detail=f"task_type_categories[{category}] must be a list of task-type strings",
                )
            # Members are task types — reject typoed/unknown ones. Every
            # registered task type can be categorized, so the allowlist is the
            # full registry.
            _reject_unknown_task_types(
                f"task_type_categories[{category}]", members, ALL_TASK_TYPES
            )
        _runtime_config["task_type_categories"] = val
    if "category_routing" in payload:
        val = payload["category_routing"]
        if not isinstance(val, dict):
            raise HTTPException(
                status_code=400, detail="category_routing must be a dict"
            )
        valid_providers = {p.value for p in LLMProviderName}
        valid_effort = {"low", "medium", "high", "max"}
        for category, entry in val.items():
            # "_default" allowed as a catch-all.
            if not isinstance(category, str) or not category:
                raise HTTPException(
                    status_code=400,
                    detail="category_routing keys must be non-empty strings",
                )
            if not isinstance(entry, dict):
                raise HTTPException(
                    status_code=400,
                    detail=f"category_routing[{category}] must be an object with providers/effort",
                )
            providers = entry.get("providers")
            if not isinstance(providers, list) or not all(
                isinstance(p, str) for p in providers
            ):
                raise HTTPException(
                    status_code=400,
                    detail=f"category_routing[{category}].providers must be a list of strings",
                )
            if not providers:
                raise HTTPException(
                    status_code=400,
                    detail=f"category_routing[{category}].providers must not be empty — at least one provider is required",
                )
            invalid = [p for p in providers if p not in valid_providers]
            if invalid:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid providers in category_routing[{category}]: {', '.join(invalid)}",
                )
            effort = entry.get("effort")
            if not isinstance(effort, str) or effort not in valid_effort:
                raise HTTPException(
                    status_code=400,
                    detail=f"category_routing[{category}].effort must be one of {sorted(valid_effort)}",
                )
        _runtime_config["category_routing"] = val
    if "task_type_routing" in payload:
        val = payload["task_type_routing"]
        if not isinstance(val, dict):
            raise HTTPException(
                status_code=400, detail="task_type_routing must be a dict"
            )
        valid_providers = {p.value for p in LLMProviderName}
        valid_effort = {"low", "medium", "high", "max"}
        # Keys are task types whose provider routing is being overridden — gate
        # on the registry's provider-configurable subset.
        _reject_unknown_task_types(
            "task_type_routing", val.keys(), PROVIDER_CONFIGURABLE_TASK_TYPES
        )
        for task_type, entry in val.items():
            if not isinstance(task_type, str) or not task_type:
                raise HTTPException(
                    status_code=400,
                    detail="task_type_routing keys must be non-empty strings",
                )
            if not isinstance(entry, dict) or not entry:
                raise HTTPException(
                    status_code=400,
                    detail=f"task_type_routing[{task_type}] must be a non-empty object with providers and/or effort",
                )
            if "providers" in entry:
                providers = entry["providers"]
                if not isinstance(providers, list) or not all(
                    isinstance(p, str) for p in providers
                ):
                    raise HTTPException(
                        status_code=400,
                        detail=f"task_type_routing[{task_type}].providers must be a list of strings",
                    )
                if not providers:
                    raise HTTPException(
                        status_code=400,
                        detail=f"task_type_routing[{task_type}].providers must not be empty — omit the `providers` key to inherit from the category instead",
                    )
                invalid = [p for p in providers if p not in valid_providers]
                if invalid:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Invalid providers in task_type_routing[{task_type}]: {', '.join(invalid)}",
                    )
            if "effort" in entry:
                effort = entry["effort"]
                if not isinstance(effort, str) or effort not in valid_effort:
                    raise HTTPException(
                        status_code=400,
                        detail=f"task_type_routing[{task_type}].effort must be one of {sorted(valid_effort)}",
                    )
            extra = [k for k in entry if k not in ("providers", "effort")]
            if extra:
                raise HTTPException(
                    status_code=400,
                    detail=f"task_type_routing[{task_type}] has unknown keys: {', '.join(extra)}",
                )
        _runtime_config["task_type_routing"] = val
    if "task_type_model_preferences" in payload:
        val = payload["task_type_model_preferences"]
        if not isinstance(val, dict):
            raise HTTPException(
                status_code=400, detail="task_type_model_preferences must be a dict"
            )
        valid_providers = {p.value for p in LLMProviderName}
        # Keys are task types whose model preferences are being configured — gate
        # on the registry's provider-configurable subset.
        _reject_unknown_task_types(
            "task_type_model_preferences",
            val.keys(),
            PROVIDER_CONFIGURABLE_TASK_TYPES,
        )
        for task_type, prefs in val.items():
            if not isinstance(task_type, str) or not task_type:
                raise HTTPException(
                    status_code=400,
                    detail="task_type_model_preferences keys must be non-empty strings",
                )
            if not isinstance(prefs, list):
                raise HTTPException(
                    status_code=400,
                    detail=f"Model preferences for {task_type} must be a list",
                )
            for pref in prefs:
                if not isinstance(pref, dict):
                    raise HTTPException(
                        status_code=400,
                        detail=f"Each model preference for {task_type} must be an object",
                    )
                provider_name = pref.get("provider")
                model_name = pref.get("model")
                if provider_name not in valid_providers:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Invalid provider in model preferences for {task_type}: {provider_name}",
                    )
                if not isinstance(model_name, str) or not model_name:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Invalid model in model preferences for {task_type}",
                    )
        _runtime_config["task_type_model_preferences"] = val
    # provider_selection_mode is the canonical control; cross_provider_fallback_enabled
    # is accepted as a compatibility shim (True→"preferred", False→"exclusive").
    # When both are present in the same PATCH, the enum wins.
    if "provider_selection_mode" in payload:
        val = payload["provider_selection_mode"]
        if not isinstance(val, str) or val not in _VALID_PROVIDER_SELECTION_MODES:
            raise HTTPException(
                status_code=400,
                detail=f"provider_selection_mode must be one of {sorted(_VALID_PROVIDER_SELECTION_MODES)}",
            )
        _runtime_config["provider_selection_mode"] = val
        _runtime_config["cross_provider_fallback_enabled"] = val in (
            "balanced",
            "preferred",
        )
    elif "cross_provider_fallback_enabled" in payload:
        val = payload["cross_provider_fallback_enabled"]
        if not isinstance(val, bool):
            raise HTTPException(
                status_code=400,
                detail="cross_provider_fallback_enabled must be a boolean",
            )
        _runtime_config["cross_provider_fallback_enabled"] = val
        _runtime_config["provider_selection_mode"] = "preferred" if val else "exclusive"
    if "provider_settings_defaults" in payload:
        val = payload["provider_settings_defaults"]
        if not isinstance(val, dict):
            raise HTTPException(
                status_code=400, detail="provider_settings_defaults must be a dict"
            )
        valid_providers = {p.value for p in LLMProviderName}
        cleaned_defaults: dict[str, dict] = {}
        for provider_name, bag in val.items():
            if provider_name not in valid_providers:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid provider in provider_settings_defaults: {provider_name}",
                )
            if not isinstance(bag, dict):
                raise HTTPException(
                    status_code=400,
                    detail=f"provider_settings_defaults[{provider_name}] must be a dict",
                )
            provider = get_provider(LLMProviderName(provider_name))
            try:
                cleaned_defaults[provider_name] = provider.validate_settings(bag)
            except ValueError as exc:
                raise HTTPException(
                    status_code=400,
                    detail=f"provider_settings_defaults[{provider_name}]: {exc}",
                )
        _runtime_config["provider_settings_defaults"] = cleaned_defaults
    if "task_type_provider_overrides" in payload:
        val = payload["task_type_provider_overrides"]
        if not isinstance(val, dict):
            raise HTTPException(
                status_code=400, detail="task_type_provider_overrides must be a dict"
            )
        valid_providers = {p.value for p in LLMProviderName}
        # Keys are task types whose provider settings are being overridden — gate
        # on the registry's provider-configurable subset.
        _reject_unknown_task_types(
            "task_type_provider_overrides",
            val.keys(),
            PROVIDER_CONFIGURABLE_TASK_TYPES,
        )
        cleaned_overrides: dict[str, dict[str, dict]] = {}
        for task_type, per_provider in val.items():
            if not isinstance(task_type, str) or not task_type:
                raise HTTPException(
                    status_code=400,
                    detail="task_type_provider_overrides keys must be non-empty strings",
                )
            if not isinstance(per_provider, dict):
                raise HTTPException(
                    status_code=400,
                    detail=f"task_type_provider_overrides[{task_type}] must be a dict",
                )
            cleaned_per_provider: dict[str, dict] = {}
            for provider_name, bag in per_provider.items():
                if provider_name not in valid_providers:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Invalid provider in task_type_provider_overrides[{task_type}]: {provider_name}",
                    )
                if not isinstance(bag, dict):
                    raise HTTPException(
                        status_code=400,
                        detail=f"task_type_provider_overrides[{task_type}][{provider_name}] must be a dict",
                    )
                provider = get_provider(LLMProviderName(provider_name))
                try:
                    cleaned_per_provider[provider_name] = provider.validate_settings(
                        bag
                    )
                except ValueError as exc:
                    raise HTTPException(
                        status_code=400,
                        detail=f"task_type_provider_overrides[{task_type}][{provider_name}]: {exc}",
                    )
            cleaned_overrides[task_type] = cleaned_per_provider
        _runtime_config["task_type_provider_overrides"] = cleaned_overrides
    if "provider_settings_by_effort" in payload:
        val = payload["provider_settings_by_effort"]
        if not isinstance(val, dict):
            raise HTTPException(
                status_code=400, detail="provider_settings_by_effort must be a dict"
            )
        valid_providers = {p.value for p in LLMProviderName}
        valid_levels = {"low", "medium", "high", "max"}
        cleaned_matrix: dict[str, dict[str, dict]] = {}
        for provider_name, per_level in val.items():
            if provider_name not in valid_providers:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid provider in provider_settings_by_effort: {provider_name}",
                )
            if not isinstance(per_level, dict):
                raise HTTPException(
                    status_code=400,
                    detail=f"provider_settings_by_effort[{provider_name}] must be a dict",
                )
            cleaned_per_level: dict[str, dict] = {}
            for level, bag in per_level.items():
                if level not in valid_levels:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Invalid effort level in provider_settings_by_effort[{provider_name}]: {level} (must be one of {sorted(valid_levels)})",
                    )
                if not isinstance(bag, dict):
                    raise HTTPException(
                        status_code=400,
                        detail=f"provider_settings_by_effort[{provider_name}][{level}] must be a dict",
                    )
                provider = get_provider(LLMProviderName(provider_name))
                try:
                    cleaned_per_level[level] = provider.validate_settings(bag)
                except ValueError as exc:
                    raise HTTPException(
                        status_code=400,
                        detail=f"provider_settings_by_effort[{provider_name}][{level}]: {exc}",
                    )
            cleaned_matrix[provider_name] = cleaned_per_level
        _runtime_config["provider_settings_by_effort"] = cleaned_matrix
    if "agent_prompt_suffixes" in payload:
        val = payload["agent_prompt_suffixes"]
        if not isinstance(val, dict):
            raise HTTPException(
                status_code=400, detail="agent_prompt_suffixes must be a dict"
            )
        if any(not isinstance(v, str) for v in val.values()):
            raise HTTPException(
                status_code=400, detail="agent_prompt_suffixes values must be strings"
            )
        _runtime_config["agent_prompt_suffixes"] = val
    if "autonomous_periodic_tasks" in payload:
        val = payload["autonomous_periodic_tasks"]
        if not isinstance(val, dict):
            raise HTTPException(
                status_code=400, detail="autonomous_periodic_tasks must be a dict"
            )
        # Periodic-eligibility is registry-derived (capability.periodic_eligible),
        # not a hardcoded tuple.
        _reject_unknown_task_types(
            "autonomous_periodic_tasks", val.keys(), PERIODIC_TASK_TYPES
        )
        for type_key, type_cfg in val.items():
            if not isinstance(type_cfg, dict):
                raise HTTPException(
                    status_code=400, detail=f"Config for {type_key} must be a dict"
                )
            if "enabled" in type_cfg and not isinstance(type_cfg["enabled"], bool):
                raise HTTPException(
                    status_code=400, detail=f"enabled for {type_key} must be a boolean"
                )
            if "interval_hours" in type_cfg:
                try:
                    ih = int(type_cfg["interval_hours"])
                except (ValueError, TypeError):
                    raise HTTPException(
                        status_code=400,
                        detail=f"interval_hours for {type_key} must be a number",
                    )
                if not (1 <= ih <= 8760):
                    raise HTTPException(
                        status_code=400,
                        detail=f"interval_hours for {type_key} must be between 1 and 8760",
                    )
        # Merge into existing config (don't replace keys not in the payload)
        existing = _runtime_config.get("autonomous_periodic_tasks", {})
        for type_key, type_cfg in val.items():
            existing.setdefault(type_key, {}).update(type_cfg)
        _runtime_config["autonomous_periodic_tasks"] = existing
    # Accept both new (minutes) and old (hours) keys for backward compat
    if "autonomous_direction_cooldown_minutes" in payload:
        try:
            val = int(payload["autonomous_direction_cooldown_minutes"])
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=400,
                detail="autonomous_direction_cooldown_minutes must be a number",
            )
        if not (1 <= val <= 525600):
            raise HTTPException(
                status_code=400,
                detail="autonomous_direction_cooldown_minutes must be between 1 and 525600",
            )
        _runtime_config["autonomous_direction_cooldown_minutes"] = val
    elif "autonomous_direction_cooldown_hours" in payload:
        try:
            val = int(payload["autonomous_direction_cooldown_hours"])
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=400,
                detail="autonomous_direction_cooldown_hours must be a number",
            )
        _runtime_config["autonomous_direction_cooldown_minutes"] = val * 60
    if "productive_direction_cooldown_minutes" in payload:
        try:
            val = int(payload["productive_direction_cooldown_minutes"])
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=400,
                detail="productive_direction_cooldown_minutes must be a number",
            )
        if not (1 <= val <= 525600):
            raise HTTPException(
                status_code=400,
                detail="productive_direction_cooldown_minutes must be between 1 and 525600",
            )
        _runtime_config["productive_direction_cooldown_minutes"] = val
    if "strategic_research_frequency" in payload:
        try:
            val = int(payload["strategic_research_frequency"])
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=400, detail="strategic_research_frequency must be a number"
            )
        if not (0 <= val <= 100):
            raise HTTPException(
                status_code=400,
                detail="strategic_research_frequency must be between 0 and 100 (0 = disabled)",
            )
        _runtime_config["strategic_research_frequency"] = val
    if "require_approval_gates" in payload:
        val = payload["require_approval_gates"]
        if not isinstance(val, bool):
            raise HTTPException(
                status_code=400, detail="require_approval_gates must be a boolean"
            )
        _runtime_config["require_approval_gates"] = val
    if "daily_cost_ceiling_usd" in payload:
        try:
            val = float(payload["daily_cost_ceiling_usd"])
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=400, detail="daily_cost_ceiling_usd must be a number"
            )
        if not (0 <= val <= 100000):
            raise HTTPException(
                status_code=400,
                detail="daily_cost_ceiling_usd must be between 0 and 100000 (0 = disabled)",
            )
        _runtime_config["daily_cost_ceiling_usd"] = val
    if "auto_archive_enabled" in payload:
        val = payload["auto_archive_enabled"]
        if not isinstance(val, bool):
            raise HTTPException(
                status_code=400, detail="auto_archive_enabled must be a boolean"
            )
        _runtime_config["auto_archive_enabled"] = val
    if "auto_archive_terminal_after_days" in payload:
        try:
            val = int(payload["auto_archive_terminal_after_days"])
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=400,
                detail="auto_archive_terminal_after_days must be a number",
            )
        if not (1 <= val <= 90):
            raise HTTPException(
                status_code=400,
                detail="auto_archive_terminal_after_days must be between 1 and 90",
            )
        _runtime_config["auto_archive_terminal_after_days"] = val
    if "auto_archive_max_terminal_tasks_per_project" in payload:
        try:
            val = int(payload["auto_archive_max_terminal_tasks_per_project"])
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=400,
                detail="auto_archive_max_terminal_tasks_per_project must be a number",
            )
        if not (0 <= val <= 10000):
            raise HTTPException(
                status_code=400,
                detail="auto_archive_max_terminal_tasks_per_project must be between 0 and 10000 (0 = disabled)",
            )
        _runtime_config["auto_archive_max_terminal_tasks_per_project"] = val
    if "auto_archive_interval_minutes" in payload:
        try:
            val = int(payload["auto_archive_interval_minutes"])
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=400, detail="auto_archive_interval_minutes must be a number"
            )
        if not (5 <= val <= 1440):
            raise HTTPException(
                status_code=400,
                detail="auto_archive_interval_minutes must be between 5 and 1440",
            )
        _runtime_config["auto_archive_interval_minutes"] = val
    if "max_provision_retries" in payload:
        try:
            val = int(payload["max_provision_retries"])
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=400, detail="max_provision_retries must be a number"
            )
        if not (1 <= val <= 10):
            raise HTTPException(
                status_code=400, detail="max_provision_retries must be between 1 and 10"
            )
        _runtime_config["max_provision_retries"] = val
    if "max_rollup_retries" in payload:
        try:
            val = int(payload["max_rollup_retries"])
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=400, detail="max_rollup_retries must be a number"
            )
        if not (1 <= val <= 20):
            raise HTTPException(
                status_code=400, detail="max_rollup_retries must be between 1 and 20"
            )
        _runtime_config["max_rollup_retries"] = val
    if "architecture_review_frequency" in payload:
        try:
            val = int(payload["architecture_review_frequency"])
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=400, detail="architecture_review_frequency must be a number"
            )
        if not (0 <= val <= 100):
            raise HTTPException(
                status_code=400,
                detail="architecture_review_frequency must be between 0 and 100 (0 = disabled)",
            )
        _runtime_config["architecture_review_frequency"] = val
    if "strategic_max_interval_minutes" in payload:
        try:
            val = int(payload["strategic_max_interval_minutes"])
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=400,
                detail="strategic_max_interval_minutes must be a number",
            )
        if not (0 <= val <= 10080):
            raise HTTPException(
                status_code=400,
                detail="strategic_max_interval_minutes must be between 0 and 10080 (0 = disabled, 10080 = 1 week)",
            )
        _runtime_config["strategic_max_interval_minutes"] = val
    if "max_active_directions" in payload:
        try:
            val = int(payload["max_active_directions"])
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=400, detail="max_active_directions must be a number"
            )
        if not (1 <= val <= 50):
            raise HTTPException(
                status_code=400, detail="max_active_directions must be between 1 and 50"
            )
        _runtime_config["max_active_directions"] = val
    if "max_parallel_research_per_project" in payload:
        try:
            val = int(payload["max_parallel_research_per_project"])
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=400,
                detail="max_parallel_research_per_project must be a number",
            )
        if not (1 <= val <= 20):
            raise HTTPException(
                status_code=400,
                detail="max_parallel_research_per_project must be between 1 and 20",
            )
        _runtime_config["max_parallel_research_per_project"] = val
    if "reserved_slots_for_types" in payload:
        val = payload["reserved_slots_for_types"]
        if not isinstance(val, dict):
            raise HTTPException(
                status_code=400, detail="reserved_slots_for_types must be a dict"
            )
        # Reserved-slot eligibility is registry-derived (capability.reservable),
        # not a hardcoded tuple — a newly registered reservable type is accepted
        # here automatically.
        _reject_unknown_task_types(
            "reserved_slots_for_types", val.keys(), RESERVABLE_TASK_TYPES
        )
        normalized: Dict[str, int] = {}
        for type_key, count in val.items():
            try:
                cnt = int(count)
            except (ValueError, TypeError):
                raise HTTPException(
                    status_code=400,
                    detail=f"reserved_slots_for_types[{type_key}] must be a number",
                )
            if not (0 <= cnt <= 10):
                raise HTTPException(
                    status_code=400,
                    detail=f"reserved_slots_for_types[{type_key}] must be between 0 and 10",
                )
            normalized[type_key] = cnt
        _runtime_config["reserved_slots_for_types"] = normalized
    if "max_pipeline_slots" in payload:
        try:
            val = int(payload["max_pipeline_slots"])
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=400, detail="max_pipeline_slots must be a number"
            )
        if not (0 <= val <= 100):
            raise HTTPException(
                status_code=400,
                detail="max_pipeline_slots must be between 0 and 100 (0 = disabled)",
            )
        _runtime_config["max_pipeline_slots"] = val
    if "health_check_urls" in payload:
        val = payload["health_check_urls"]
        if not isinstance(val, list):
            raise HTTPException(
                status_code=400, detail="health_check_urls must be a list"
            )
        if any(not isinstance(u, str) for u in val):
            raise HTTPException(
                status_code=400, detail="health_check_urls entries must be strings"
            )
        if any(not u.startswith(("http://", "https://")) for u in val if u):
            raise HTTPException(
                status_code=400,
                detail="health_check_urls entries must start with http:// or https://",
            )
        _runtime_config["health_check_urls"] = [u for u in val if u]
    if "notification_config" in payload:
        val = payload["notification_config"]
        if not isinstance(val, dict):
            raise HTTPException(
                status_code=400, detail="notification_config must be an object"
            )
        webhook_url = val.get("webhook_url", "")
        if not isinstance(webhook_url, str):
            raise HTTPException(
                status_code=400,
                detail="notification_config.webhook_url must be a string",
            )
        if webhook_url:
            safe, reason = await asyncio.to_thread(_is_safe_external_url, webhook_url)
            if not safe:
                raise HTTPException(
                    status_code=400,
                    detail=f"notification_config.webhook_url is not allowed: {reason}",
                )
        _VALID_NOTIFICATION_EVENTS = frozenset(
            [
                "TASK_FAILED",
                "TASK_COMPLETED",
                "APPROVAL_NEEDED",
                "DEPLOY_HEALTH_ALERT",
                "PR_MERGED",
                "BUDGET_EXCEEDED",
                "AUTO_FIX_SPAWNED",
                "AUTO_FIX_MERGED",
                "AUTO_FIX_ESCALATED",
                "REMEDIATION_ESCALATED",
                "DIRECTION_MILESTONE",
                # Residual remediation lifecycle (mirrors AUTO_FIX_*).
                # SPAWNED/MERGED are noisier — opt-in only. ESCALATED is BLOCKER
                # severity and joins the default-on list per the
                # direction-milestone-webhook-pattern playbook.
                "RESIDUAL_FIX_SPAWNED",
                "RESIDUAL_FIX_MERGED",
                "RESIDUAL_FIX_ESCALATED",
                # Recovery event: operator manually unsuspends a suspended fingerprint.
                # Opt-in only (not in default list) — informational, not urgent.
                "RESIDUAL_FIX_UNSUSPENDED",
                # Escalation fired when a case is deferred by audit too many times.
                "RESIDUAL_FIX_AUDIT_DEFERRED",
                # Direction auto-paused for lifetime burn (non-merging finished
                # tasks beyond the rolling-window threshold). BLOCKER severity /
                # manual-intervention-required, so it joins the default-on list.
                "DIRECTION_BURN_PAUSE",
                # No-CI repo merged a PR without test verification (UNVERIFIED-merge
                # fallback). The operator who turned off required CI checks needs to
                # know when a merge proceeds without tests, so it is default-on.
                "MERGE_UNVERIFIED",
            ]
        )
        enabled_events = val.get(
            "enabled_events",
            [
                "TASK_FAILED",
                "APPROVAL_NEEDED",
                "DEPLOY_HEALTH_ALERT",
                "REMEDIATION_ESCALATED",
                "DIRECTION_MILESTONE",
                "RESIDUAL_FIX_ESCALATED",
                "DIRECTION_BURN_PAUSE",
                "MERGE_UNVERIFIED",
            ],
        )
        if not isinstance(enabled_events, list):
            raise HTTPException(
                status_code=400,
                detail="notification_config.enabled_events must be a list",
            )
        invalid_events = [
            e for e in enabled_events if e not in _VALID_NOTIFICATION_EVENTS
        ]
        if invalid_events:
            raise HTTPException(
                status_code=400,
                detail=f"notification_config.enabled_events contains unknown events: {invalid_events}. "
                f"Valid values: {sorted(_VALID_NOTIFICATION_EVENTS)}",
            )
        throttle_seconds = val.get("throttle_seconds", 30)
        try:
            throttle_seconds = int(throttle_seconds)
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=400,
                detail="notification_config.throttle_seconds must be a number",
            )
        if not (0 <= throttle_seconds <= 3600):
            raise HTTPException(
                status_code=400,
                detail="notification_config.throttle_seconds must be between 0 and 3600",
            )
        _runtime_config["notification_config"] = {
            "webhook_url": webhook_url,
            "enabled_events": enabled_events,
            "throttle_seconds": throttle_seconds,
        }
    if "test_notification" in payload and payload["test_notification"] is True:
        nc = _runtime_config.get("notification_config", {})
        webhook_url = nc.get("webhook_url", "")
        if not webhook_url:
            raise HTTPException(status_code=400, detail="No webhook URL configured")
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    webhook_url,
                    json={"type": "TEST", "message": "Blueprint test notification"},
                )
            if resp.status_code >= 400:
                raise HTTPException(
                    status_code=502, detail=f"Webhook returned {resp.status_code}"
                )
        except httpx.RequestError as e:
            raise HTTPException(status_code=502, detail=f"Webhook request failed: {e}")
        return {"ok": True, "test_notification_sent": True}

    # Persist to DB so values survive Cloud Run restarts
    # Always persist all runtime config values (not just changed ones) to ensure consistency
    try:
        # Non-blocking FOR UPDATE prevents concurrent settings writes from
        # racing without wedging the admin request behind deploy-health writes.
        # state can be None either because the row is held by a peer (rare) or
        # because no DeploymentState row has been created yet (fresh install /
        # tests). The original sync version silently skipped the persist when
        # the row was missing; preserve that behavior — only raise 409 when we
        # can confirm the row exists but is held.
        async with deployment_state_lock(session, skip_locked=True) as state:
            if state is None:
                exists = (
                    await session.execute(
                        select(DeploymentState.id).where(DeploymentState.id == 1)
                    )
                ).first()
                if exists is None:
                    # No DeploymentState row yet — runtime config is in-memory only.
                    # Skip the persist; the next admin/deploy bootstrap will create the row.
                    return _runtime_config
                raise HTTPException(
                    status_code=409,
                    detail="Deployment state locked; retry settings update",
                )
            if clear_pause_state and state.status in (
                DeploymentStatus.PAUSED,
                DeploymentStatus.HALTED,
            ):
                old_status = state.status
                state.status = DeploymentStatus.STABLE
                state.pause_reason = None
                state.paused_at = None
                state.pre_pause_max_vms = None
                logger.warning(
                    f"[SETTINGS] Cleared {old_status} state — user set max_vms={_runtime_config['max_concurrent_vms']}"
                )
            state.runtime_max_concurrent_vms = _runtime_config["max_concurrent_vms"]
            state.runtime_max_retries = _runtime_config["max_retries"]
            state.runtime_max_autonomous_tasks_per_hour = _runtime_config[
                "max_autonomous_tasks_per_hour"
            ]
            state.runtime_max_global_autonomous_tasks_per_hour = _runtime_config.get(
                "max_global_autonomous_tasks_per_hour"
            )
            state.runtime_autonomous_research_interval_minutes = _runtime_config.get(
                "autonomous_research_interval_minutes", 30
            )
            state.runtime_autonomous_direction_cooldown_minutes = _runtime_config.get(
                "autonomous_direction_cooldown_minutes", 240
            )
            # Persist dict/list config fields to JSONB column
            try:
                state.runtime_config_json = {
                    "provider_tier_model_map": _runtime_config.get(
                        "provider_tier_model_map", {}
                    ),
                    "task_type_categories": _runtime_config.get(
                        "task_type_categories", {}
                    ),
                    "category_routing": _runtime_config.get("category_routing", {}),
                    "task_type_routing": _runtime_config.get("task_type_routing", {}),
                    "task_type_model_preferences": _runtime_config.get(
                        "task_type_model_preferences", {}
                    ),
                    "provider_selection_mode": _provider_selection_mode_from_config(
                        _runtime_config
                    ),
                    "cross_provider_fallback_enabled": _provider_selection_mode_from_config(
                        _runtime_config
                    )
                    in ("balanced", "preferred"),
                    "provider_settings_defaults": _runtime_config.get(
                        "provider_settings_defaults", {}
                    ),
                    "provider_settings_by_effort": _runtime_config.get(
                        "provider_settings_by_effort", {}
                    ),
                    "task_type_provider_overrides": _runtime_config.get(
                        "task_type_provider_overrides", {}
                    ),
                    "agent_prompt_suffixes": _runtime_config.get(
                        "agent_prompt_suffixes", {}
                    ),
                    "autonomous_periodic_tasks": _runtime_config.get(
                        "autonomous_periodic_tasks", {}
                    ),
                    "health_check_urls": _runtime_config.get("health_check_urls", []),
                    "strategic_research_frequency": _runtime_config.get(
                        "strategic_research_frequency", 6
                    ),
                    "require_approval_gates": _runtime_config.get(
                        "require_approval_gates", True
                    ),
                    "daily_cost_ceiling_usd": _runtime_config.get(
                        "daily_cost_ceiling_usd", 0
                    ),
                    "max_provision_retries": _runtime_config.get(
                        "max_provision_retries", 3
                    ),
                    "max_rollup_retries": _runtime_config.get("max_rollup_retries", 5),
                    "architecture_review_frequency": _runtime_config.get(
                        "architecture_review_frequency", 3
                    ),
                    "strategic_max_interval_minutes": _runtime_config.get(
                        "strategic_max_interval_minutes", 180
                    ),
                    "max_active_directions": _runtime_config.get(
                        "max_active_directions", 10
                    ),
                    "max_parallel_research_per_project": _runtime_config.get(
                        "max_parallel_research_per_project", 3
                    ),
                    "auto_archive_enabled": _runtime_config.get(
                        "auto_archive_enabled", True
                    ),
                    "auto_archive_terminal_after_days": _runtime_config.get(
                        "auto_archive_terminal_after_days", 7
                    ),
                    "auto_archive_max_terminal_tasks_per_project": _runtime_config.get(
                        "auto_archive_max_terminal_tasks_per_project", 200
                    ),
                    "auto_archive_interval_minutes": _runtime_config.get(
                        "auto_archive_interval_minutes", 60
                    ),
                    "reserved_slots_for_types": _runtime_config.get(
                        "reserved_slots_for_types", {"create": 1, "research": 1}
                    ),
                    "max_pipeline_slots": _runtime_config.get("max_pipeline_slots", 4),
                    "evaluating_pipeline_timeout_minutes": _runtime_config.get(
                        "evaluating_pipeline_timeout_minutes", 120
                    ),
                    "ci_backstop_timeout_minutes": _runtime_config.get(
                        "ci_backstop_timeout_minutes", 60
                    ),
                    "max_total_research_per_project": _runtime_config.get(
                        "max_total_research_per_project", 5
                    ),
                    "productive_direction_cooldown_minutes": _runtime_config.get(
                        "productive_direction_cooldown_minutes", 45
                    ),
                    "warm_continuity_enabled": bool(
                        _runtime_config.get("warm_continuity_enabled", False)
                    ),
                    "warm_backlog_reuse_enabled": bool(
                        _runtime_config.get("warm_backlog_reuse_enabled", False)
                    ),
                    "cochange_decomposition_enabled": bool(
                        _runtime_config.get("cochange_decomposition_enabled", False)
                    ),
                    "warm_pool_enabled": bool(
                        _runtime_config.get("warm_pool_enabled", False)
                    ),
                    "warm_pool_account_retarget_enabled": bool(
                        _runtime_config.get(
                            "warm_pool_account_retarget_enabled", False
                        )
                    ),
                    "warm_pool_cross_provider_retarget_enabled": bool(
                        _runtime_config.get(
                            "warm_pool_cross_provider_retarget_enabled", False
                        )
                    ),
                    # Persist the per-project enablement scope so a per-project
                    # enable (via /warm-pool/enable or the raw PATCH) survives a
                    # Cloud Run restart. runtime_config_state.sync_* reads this key
                    # back on boot; without it the in-memory allowlist reverts to
                    # the [] (fleet-wide) default and the controlled scope is lost.
                    "warm_pool_project_allowlist": sorted(
                        _normalize_warm_pool_allowlist(
                            _runtime_config.get("warm_pool_project_allowlist") or []
                        )
                    ),
                    "warm_pool_target_size": int(
                        _runtime_config.get("warm_pool_target_size", 2)
                    ),
                    # Persist the per-project IDLE target overrides so a
                    # per-project target (set via /warm-pool/target, /warm-pool/
                    # enable, or the raw PATCH) survives a Cloud Run restart.
                    # runtime_config_state.sync_* reads this key back on boot;
                    # without it the in-memory map reverts to the {} (all-global)
                    # default and every project's configured size is lost.
                    "warm_pool_target_size_by_project": _normalize_warm_pool_target_map(
                        _runtime_config.get("warm_pool_target_size_by_project") or {}
                    ),
                    "warm_pool_idle_ttl_secs": int(
                        _runtime_config.get("warm_pool_idle_ttl_secs", 1800)
                    ),
                    # Persist the ``_seconds`` spelling too — it is the one the
                    # maintainer/idle-reaper actually read (warm_pool_maintainer.py).
                    # Without it, sync_runtime_config_from_state finds no key on
                    # restart and the operator's TTL change silently reverts to the
                    # 1800 default. The PATCH handler keeps both spellings in sync.
                    "warm_pool_idle_ttl_seconds": int(
                        _runtime_config.get(
                            "warm_pool_idle_ttl_seconds",
                            _runtime_config.get("warm_pool_idle_ttl_secs", 1800),
                        )
                    ),
                    "warm_pin_clear_rate_threshold": _runtime_config.get(
                        "warm_pin_clear_rate_threshold", 10
                    ),
                    "warm_continuity_cooldown_seconds": _runtime_config.get(
                        "warm_continuity_cooldown_seconds", 1800
                    ),
                    "warm_backlog_reuse_failure_rate_threshold": _runtime_config.get(
                        "warm_backlog_reuse_failure_rate_threshold", 5
                    ),
                    "warm_backlog_reuse_cooldown_seconds": _runtime_config.get(
                        "warm_backlog_reuse_cooldown_seconds", 1800
                    ),
                    "warm_backlog_scan_limit": _runtime_config.get(
                        "warm_backlog_scan_limit", 10
                    ),
                    "warm_backlog_teardown_defer_secs": _runtime_config.get(
                        "warm_backlog_teardown_defer_secs", 20
                    ),
                    "next_role_poll_timeout_secs": int(
                        _runtime_config.get("next_role_poll_timeout_secs", 180)
                    ),
                    "drift_over_claim_min_claims": int(
                        _runtime_config.get("drift_over_claim_min_claims", 3)
                    ),
                    "drift_over_claim_consistency_threshold": float(
                        _runtime_config.get(
                            "drift_over_claim_consistency_threshold", 0.4
                        )
                    ),
                    "drift_stall_min_claims": int(
                        _runtime_config.get("drift_stall_min_claims", 5)
                    ),
                    "residual_audit_defer_escalation_threshold": int(
                        _runtime_config.get(
                            "residual_audit_defer_escalation_threshold", 5
                        )
                    ),
                    "notification_config": _runtime_config.get(
                        "notification_config",
                        {
                            "webhook_url": "",
                            "enabled_events": [
                                "TASK_FAILED",
                                "APPROVAL_NEEDED",
                                "DEPLOY_HEALTH_ALERT",
                                "REMEDIATION_ESCALATED",
                                "RESIDUAL_FIX_ESCALATED",
                                "DIRECTION_BURN_PAUSE",
                                "MERGE_UNVERIFIED",
                            ],
                            "throttle_seconds": 30,
                        },
                    ),
                    "promotion_enabled": bool(
                        _runtime_config.get("promotion_enabled", False)
                    ),
                    # Persist the per-project live-promotion overrides so a
                    # per-project arm/hold (set via /promotion-enabled or the raw
                    # PATCH) survives a Cloud Run restart. runtime_config_state.
                    # sync_* reads this key back on boot; without it the in-memory
                    # map reverts to {} (all-fleet-default) and overrides are lost.
                    "promotion_enabled_by_project": {
                        str(k): bool(v)
                        for k, v in (
                            _runtime_config.get("promotion_enabled_by_project") or {}
                        ).items()
                    },
                    "smoketest_enabled": bool(
                        _runtime_config.get("smoketest_enabled", False)
                    ),
                    "promotion_threshold": _runtime_config.get("promotion_threshold"),
                    "promotion_weights": _runtime_config.get("promotion_weights"),
                }
            except AttributeError:
                pass  # Column not yet added (rolling deploy, old schema)
            session.add(state)
        await session.commit()
        logger.info(
            f"[SETTINGS] Persisted runtime config: max_vms={state.runtime_max_concurrent_vms if state else 'missing'}"
        )
    except HTTPException:
        _runtime_config.update(_config_snapshot)
        await session.rollback()
        raise
    except Exception as e:
        # Restore in-memory config to pre-change state on DB failure
        _runtime_config.update(_config_snapshot)
        logger.exception("[SETTINGS] DB persist failed, rolling back in-memory config")
        await session.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to persist settings: {e}")

    return _runtime_config


# --- GitHub Repository & Branch Discovery ---


async def _get_installation_token_for_user(user_id: str, session: AsyncSession) -> str:
    """
    Retrieves a GitHub installation access token for the given user.
    Raises HTTPException if no integration or installation is found.
    """
    stmt = select(UserIntegration).where(UserIntegration.user_id == user_id)
    integration = (await session.execute(stmt)).scalars().first()

    if not integration:
        raise HTTPException(
            status_code=400,
            detail="No GitHub integration found. Please connect your GitHub account first.",
        )

    if integration.github_installation_id:
        try:
            token = await generate_installation_access_token(
                integration.github_installation_id
            )
            return token
        except HTTPException:
            raise HTTPException(
                status_code=502,
                detail="Failed to generate GitHub installation token. The installation may have been removed.",
            )

    raise HTTPException(
        status_code=400,
        detail="No GitHub App installation found for your account. Please install the GitHub App first.",
    )


@router.get("/github/repos")
async def list_github_repos(
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """
    Lists repositories accessible via the current user's GitHub App installation.
    Returns a sorted list of {full_name, default_branch} objects.
    """
    token = await _get_installation_token_for_user(current_user["user_id"], session)

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    repos = []
    url = "https://api.github.com/installation/repositories?per_page=100"

    async with httpx.AsyncClient(timeout=15.0) as client:
        while url:
            resp = await client.get(url, headers=headers)
            if resp.status_code != 200:
                logger.error(
                    f"GitHub API error listing repos: {resp.status_code} {resp.text}"
                )
                raise HTTPException(
                    status_code=502, detail="Failed to fetch repositories from GitHub."
                )
            data = resp.json()
            for r in data.get("repositories", []):
                repos.append(
                    {
                        "full_name": r["full_name"],
                        "default_branch": r.get("default_branch", "main"),
                    }
                )
            # Follow pagination
            url = resp.links.get("next", {}).get("url")

    repos.sort(key=lambda r: r["full_name"].lower())
    return {"repositories": repos}


@router.get("/github/branches")
async def list_github_branches(
    repo: str = Query(..., pattern=r"^[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+$"),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """
    Lists branches for a specific repository accessible via the user's GitHub App installation.
    Query param: repo=owner/repo
    Returns a sorted list of branch names.
    """
    token = await _get_installation_token_for_user(current_user["user_id"], session)

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    branches = []
    url = f"https://api.github.com/repos/{repo}/branches?per_page=100"

    async with httpx.AsyncClient(timeout=15.0) as client:
        while url:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 404:
                raise HTTPException(
                    status_code=404,
                    detail=f"Repository '{repo}' not found or not accessible.",
                )
            if resp.status_code != 200:
                logger.error(
                    f"GitHub API error listing branches for {repo}: {resp.status_code} {resp.text}"
                )
                raise HTTPException(
                    status_code=502, detail="Failed to fetch branches from GitHub."
                )
            data = resp.json()
            for b in data:
                branches.append(b["name"])
            # Follow pagination
            url = resp.links.get("next", {}).get("url")

    branches.sort(key=str.lower)
    return {"branches": branches}


@router.post("/restart-orchestrator")
async def restart_orchestrator(current_user: dict = Depends(require_admin)):
    """Restart the orchestrator background task without restarting the entire service."""
    import asyncio
    from app.shared_state import background_tasks
    from app.orchestrator import orchestrator_worker

    old_task = background_tasks.get("orchestrator")
    if old_task and not old_task.done():
        old_task.cancel()
        try:
            await old_task
        except asyncio.CancelledError:
            pass

    background_tasks["orchestrator"] = asyncio.create_task(orchestrator_worker())
    logger.info("[ADMIN] Orchestrator restarted via admin endpoint")
    return {"status": "restarted"}


@router.post("/memory/tracemalloc/start")
async def memory_tracemalloc_start(
    frames: int = Query(default=10, ge=1, le=50),
    current_user: dict = Depends(require_admin),
):
    """Enable tracemalloc to diagnose the 4GB Cloud Run OOM.

    Default kept off at startup (tracemalloc has ~10-25% overhead). Operator
    turns it on, waits for RSS to climb, then calls /memory/snapshot to
    identify the leak source. Turn off again with /memory/tracemalloc/stop.
    """
    import tracemalloc

    if tracemalloc.is_tracing():
        return {
            "status": "already_running",
            "frames": tracemalloc.get_traceback_limit(),
        }
    tracemalloc.start(frames)
    logger.warning(f"[MEMORY] tracemalloc enabled with {frames} frames")
    return {"status": "started", "frames": frames}


@router.post("/memory/tracemalloc/stop")
async def memory_tracemalloc_stop(
    current_user: dict = Depends(require_admin),
):
    import tracemalloc

    if not tracemalloc.is_tracing():
        return {"status": "not_running"}
    tracemalloc.stop()
    logger.warning("[MEMORY] tracemalloc disabled")
    return {"status": "stopped"}


@router.get("/memory/snapshot")
async def memory_snapshot(
    top: int = Query(default=30, ge=5, le=100),
    current_user: dict = Depends(require_admin),
):
    """Return top-N allocations plus RSS so we can ground the OOM debugging
    in real data rather than guesses. Each snapshot is also emitted at
    WARNING level so Cloud Logging preserves it even if the container OOMs
    mid-request — that's the whole point of calling this right before the
    limit is hit.
    """
    import tracemalloc
    import os

    def _read_rss_mb() -> float | None:
        try:
            with open(f"/proc/{os.getpid()}/status", "r") as _st:
                for _line in _st:
                    if _line.startswith("VmRSS:"):
                        return int(_line.split()[1]) / 1024
        except Exception as _rss_exc:
            logger.debug(f"[MEMORY] RSS read failed: {_rss_exc}")
        return None

    rss_mb = await asyncio.to_thread(_read_rss_mb)

    if not tracemalloc.is_tracing():
        logger.warning(f"[MEMORY SNAPSHOT] rss_mb={rss_mb} tracemalloc=off")
        return {
            "rss_mb": rss_mb,
            "tracemalloc_running": False,
            "note": "Call /memory/tracemalloc/start first to collect allocation traces.",
        }

    snap = tracemalloc.take_snapshot()
    stats = snap.statistics("lineno")[:top]
    result = []
    for s in stats:
        frame = s.traceback[0]
        result.append(
            {
                "file": frame.filename,
                "line": frame.lineno,
                "size_mb": round(s.size / (1024 * 1024), 3),
                "count": s.count,
            }
        )
    # Log top-10 at WARNING so Cloud Logging retains the snapshot through OOM.
    top_log = "; ".join(
        f"{r['file']}:{r['line']} size={r['size_mb']}MB n={r['count']}"
        for r in result[:10]
    )
    logger.warning(f"[MEMORY SNAPSHOT] rss_mb={rss_mb} top10={top_log}")
    return {
        "rss_mb": rss_mb,
        "tracemalloc_running": True,
        "top": result,
    }


@router.post("/github-quota/refresh")
async def refresh_github_quota(
    current_user: dict = Depends(require_admin),
):
    """Force an immediate probe of GitHub's /rate_limit endpoint.

    Returns the refreshed shared_state.github_quota dict so the UI can
    update without waiting for the next 30s health-detailed poll.
    Useful after the pill shows suspicious stale values (e.g. a
    remaining=0 reading that got stuck across a bucket switch).
    """
    import app.shared_state as _ss  # noqa: PLC0415
    from app.github import probe_rate_limit  # noqa: PLC0415

    ok = await probe_rate_limit()
    gq = dict(_ss.github_quota)
    remaining = gq.get("remaining")
    if remaining is None:
        status = "unknown"
    elif remaining < 100:
        status = "critical"
    elif remaining < 500:
        status = "degraded"
    else:
        status = "ok"
    gq["status"] = status
    return {"probed": bool(ok), "github_quota": gq}


@router.post("/github-quota/reset-tracking")
async def reset_github_quota_tracking(
    current_user: dict = Depends(require_admin),
):
    """Clear the in-memory quota tracking state.

    Forces the next _github_request response to repopulate from scratch —
    useful to unstick a stale `remaining` value from a polluted bucket
    observation (e.g. App-JWT contamination before the install-token
    filter deployed).
    """
    import app.shared_state as _ss  # noqa: PLC0415

    _ss.github_quota.update(
        {
            "remaining": None,
            "limit": None,
            "reset_at": None,
            "last_updated": None,
        }
    )
    _ss.github_call_counts.clear()
    _ss.github_call_counts_window_reset_at = None
    # Immediately probe to repopulate with a fresh observation.
    from app.github import probe_rate_limit  # noqa: PLC0415

    await probe_rate_limit()
    return {"status": "reset", "github_quota": dict(_ss.github_quota)}


@router.post("/repair-state")
async def repair_state(
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
):
    """Post-rollback state audit and repair. Fixes stuck tasks, orphaned parents, stale evaluating tasks."""
    from app.models import TaskHistory

    repairs = []

    # 1. Tasks stuck in PROVISIONING/IN_PROGRESS with no heartbeat for 15+ min → FAILED
    fifteen_min_ago = _utcnow() - timedelta(minutes=15)
    # Scope to tasks updated in the last 24 hours; active tasks older than that are
    # extremely rare and would require manual intervention anyway.
    recent_cutoff = _utcnow() - timedelta(hours=24)
    stuck_tasks = (
        (
            await session.execute(
                select(Task)
                .where(
                    Task.status.in_([TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS]),
                    Task.vm_instance_id.isnot(None),
                    Task.updated_at >= recent_cutoff,
                )
                .limit(500)
            )
        )
        .scalars()
        .all()
    )

    for t in stuck_tasks:
        cutoff = t.last_heartbeat or t.started_at or t.created_at
        if cutoff and cutoff < fifteen_min_ago:
            original_status = t.status  # Capture before overwriting
            t.status = TaskStatus.FAILED
            t.latest_status_message = f"Repaired: stuck in {original_status} with no heartbeat since {cutoff.isoformat()}"
            from app.failure_classification import persist_failure_category  # noqa: PLC0415

            persist_failure_category(t, "AGENT_TIMEOUT")
            session.add(t)
            session.add(
                TaskHistory(
                    task_id=t.id,
                    action="REPAIR",
                    details={
                        "reason": "stuck_no_heartbeat",
                        "original_status": str(original_status),
                    },
                )
            )
            repairs.append(f"Failed stuck task {t.id}")

            # Clean up VM
            if t.vm_instance_id:
                from app.vm_lifecycle import _ensure_vm_deleted

                await _ensure_vm_deleted(t, reason="admin_repair")

    # 2. EVALUATING tasks where all children are terminal but parent not updated
    evaluating_tasks = (
        (
            await session.execute(
                select(Task).where(Task.status == TaskStatus.EVALUATING).limit(500)
            )
        )
        .scalars()
        .all()
    )

    for parent in evaluating_tasks:
        children = (
            (
                await session.execute(
                    select(Task).where(Task.parent_task_id == parent.id)
                )
            )
            .scalars()
            .all()
        )

        if not children:
            continue

        all_terminal = all(
            c.status in [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED]
            for c in children
        )
        if all_terminal:
            # Re-trigger rollup by setting back to EVALUATING (the orchestrator will pick it up)
            parent.latest_status_message = (
                "Repaired: all children terminal, re-triggering rollup"
            )
            session.add(parent)
            session.add(
                TaskHistory(
                    task_id=parent.id,
                    action="REPAIR",
                    details={"reason": "stale_evaluating"},
                )
            )
            repairs.append(f"Re-triggered rollup for {parent.id}")

    # 3. Tasks with parent_task_id pointing to non-existent tasks
    ParentTask = aliased(Task)
    orphan_children = (
        (
            await session.execute(
                select(Task)
                .where(
                    Task.parent_task_id.isnot(None),
                    ~select(ParentTask.id)
                    .where(ParentTask.id == Task.parent_task_id)
                    .correlate(Task)
                    .exists(),
                )
                .limit(500)
            )
        )
        .scalars()
        .all()
    )

    for t in orphan_children:
        t.parent_task_id = None
        t.latest_status_message = "Repaired: cleared orphaned parent reference"
        session.add(t)
        session.add(
            TaskHistory(
                task_id=t.id, action="REPAIR", details={"reason": "orphaned_parent_ref"}
            )
        )
        repairs.append(f"Cleared orphaned parent ref on {t.id}")

    # 4. Circuit breakers stuck in HALF_OPEN with a deleted/non-existent test task
    from app.alert_dispatch import _maybe_create_circuit_breaker_alert
    from app.time_utils import _utcnow as _main_utcnow

    for proj_id, breaker in list(_circuit_breakers.items()):
        if breaker.get("state") != "HALF_OPEN":
            continue
        test_id = breaker.get("half_open_task_id")
        if not test_id:
            continue
        try:
            test_task = await session.get(Task, uuid.UUID(test_id))
        except Exception:
            test_task = None
        if test_task is None:
            breaker["state"] = "OPEN"
            breaker["opened_at"] = _main_utcnow()
            breaker["cooldown_seconds"] = min(breaker["cooldown_seconds"] * 2, 7200)
            breaker["half_open_task_id"] = None
            logger.warning(
                f"[ADMIN REPAIR] Circuit breaker for {proj_id} stuck in HALF_OPEN — test task {test_id} not found, resetting to OPEN"
            )
            try:
                project_uuid = uuid.UUID(proj_id)
                await _maybe_create_circuit_breaker_alert(
                    project_uuid,
                    session,
                    f"HALF_OPEN test task deleted/reaped (admin repair) — circuit breaker re-opened (cooldown: {breaker['cooldown_seconds']}s)",
                )
            except Exception:
                logger.exception(
                    f"[ADMIN REPAIR] Failed to create circuit breaker alert for {proj_id}"
                )
            repairs.append(
                f"Reset HALF_OPEN circuit breaker for project {proj_id} (test task {test_id} not found)"
            )

    await session.commit()
    logger.warning(f"[ADMIN] State repair completed: {len(repairs)} repairs")
    return {"repairs": repairs, "count": len(repairs)}
