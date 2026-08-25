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
import re
from datetime import timedelta
from sqlalchemy.orm import aliased

from app.database import get_session
from app.deploy_identity import deploy_repo_identity as _deploy_repo_identity
from app.inline_llm import validate_inline_llm_runtime_config
from app.url_security import _is_safe_external_url
from app.notification_dispatch import _attempt_delivery
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
    get_meter_reliability_stats as _get_meter_reliability_stats,
    get_usage_headroom_armable_since as _get_usage_headroom_armable_since,
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
from app.autonomous_spawn_budget import (
    default_autonomous_spawn_budget_categories,
    normalize_autonomous_initiative_cap,
    normalize_autonomous_spawn_budget_categories,
    normalize_autonomous_spawn_budget_mode,
)
from app.field_encryption import encrypt_field
from app.time_utils import utc_iso

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
# payload` branches below; test_admin_routers asserts the round-trip.
_KNOWN_SETTINGS_KEYS = frozenset(
    {
        "agent_prompt_suffixes",
        "adversarial_spawn_gate_infra_failure_mode",
        "architecture_review_frequency",
        "auto_archive_enabled",
        "auto_archive_interval_minutes",
        "auto_archive_max_terminal_tasks_per_project",
        "auto_archive_terminal_after_days",
        "auto_debug_enabled",
        "autonomous_direction_cooldown_hours",
        "autonomous_direction_cooldown_minutes",
        "autonomous_periodic_tasks",
        "autonomous_research_interval_minutes",
        "autonomous_spawn_budget_categories",
        "autonomous_spawn_budget_mode",
        "capability_proof_gate_mode",
        "capability_proof_gate_auto_graduation_enabled",
        "capability_proof_gate_auto_graduation_observe_threshold",
        "capability_proof_gate_auto_graduation_enforce_threshold",
        "capability_proof_execution_trials_enabled",
        "cloud_run_task_type_allowlist",
        "evergreen_fill_mode",
        "evergreen_fill_interval_minutes",
        "evergreen_fill_util_threshold",
        "evergreen_fill_min_free_slots",
        "evergreen_fill_max_active",
        "evergreen_fill_daily_cap",
        "evergreen_fill_deferred_memo_enrollment",
        "structural_health_audit_mode",
        "structural_health_audit_interval_minutes",
        "structural_health_audit_fat_row_bytes",
        "metrics_integrity_sweep_mode",
        "metrics_integrity_sweep_interval_minutes",
        "metrics_integrity_sweep_max_checks",
        "metrics_integrity_sweep_max_projects",
        "metrics_integrity_sweep_sample_limit",
        "metrics_integrity_sweep_window_days",
        "completion_debt_weight_mode",
        "category_routing",
        "ci_backstop_timeout_minutes",
        "cochange_decomposition_enabled",
        "compute_experiment_spend_threshold_usd",
        "cross_provider_fallback_enabled",
        "daily_cost_ceiling_usd",
        "monthly_budget_target_usd",
        "deploy_repo_identity",
        "drift_over_claim_consistency_threshold",
        "drift_over_claim_min_claims",
        "drift_stall_min_claims",
        "evaluating_fail_hours",
        "evaluating_pipeline_timeout_minutes",
        "health_check_urls",
        "max_active_directions",
        "max_autonomous_tasks_per_hour",
        "max_concurrent_auto_remediations",
        "max_concurrent_compute_jobs_by_project",
        "max_concurrent_tasks_by_project",
        "max_concurrent_vms",
        "managed_compute_enabled",
        "max_global_autonomous_tasks_per_hour",
        "max_global_autonomous_initiative_tasks_per_hour",
        "max_parallel_research_per_project",
        "max_pipeline_slots",
        "max_provision_retries",
        "max_retries",
        "max_rollup_retries",
        "max_total_research_per_project",
        "next_role_poll_timeout_secs",
        "no_ci_test_gate_enabled",
        "notification_config",
        "module_interfaces_holdback_fraction",
        "related_tests_holdback_fraction",
        "affected_consumers_holdback_fraction",
        "repo_map_holdback_fraction",
        "productive_direction_cooldown_minutes",
        "direction_refractory_minutes",
        "direction_saturation_refractory_minutes",
        "promotion_enabled",
        "promotion_enabled_by_project",
        "merge_grade_block_threshold",
        "promotion_threshold",
        "promotion_weights",
        "park_and_retarget_enabled",
        "park_and_retarget_budget_seconds",
        "provider_selection_mode",
        "provider_settings_by_effort",
        "provider_settings_defaults",
        "provider_tier_model_map",
        "require_approval_gates",
        "reserved_slots_for_types",
        "residual_audit_defer_escalation_threshold",
        "residual_remediation_cooldown_hours",
        "research_quality_grader_enabled",
        "rollup_merge_queue_enabled",
        "env_preflight_enabled",
        "smoketest_enabled",
        "spend_approval_timeout_hours",
        "strategic_max_interval_minutes",
        "strategic_research_frequency",
        # Strategic session cadence and safety knobs.
        "strategic_continuation_ttl_seconds",
        "strategic_local_hold_timeout_seconds",
        "strategic_session_idle_tick_seconds",
        "strategic_session_turn_timeout_seconds",
        "strategic_session_exploration_interval_seconds",
        "strategic_session_max_ops_per_turn",
        "strategic_session_autonomous_turn_timeout_seconds",
        "strategic_session_max_unattended_seconds",
        "task_type_categories",
        "task_type_model_preferences",
        "task_type_provider_overrides",
        "task_type_routing",
        # Proactive account-usage headroom (advisory; ships dark by default).
        "usage_headroom_gate_enabled",
        "usage_headroom_auto_arm_mode",
        "usage_headroom_auto_arm_observation_minutes",
        "usage_headroom_long_running_task_types",
        "usage_headroom_min_coverage_pct",
        "usage_headroom_threshold_pct",
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
        "diagnostics_after_edit_holdback_fraction",
        "diagnostics_after_edit_enabled",
        "inline_llm",
        "improvement_lanes_enabled",
        "review_fix_lane_pilot_enabled",
        "direction_gap_respawn_block_mode",
        "direction_gap_respawn_cooldown_days",
        # Direction guardrail knobs (churn/lifetime pause thresholds + review ladder)
        "direction_churn_window_cycles",
        "direction_churn_non_merge_threshold",
        "direction_churn_min_merge_rate",
        "direction_lifetime_non_merge_threshold",
        "direction_lifetime_min_merge_rate",
        "direction_review_recommendation_threshold",
        # Runner health thresholds — govern liveness, degradation, resource-pressure
        # detection, escalation, and data-retention windows.  All are configurable
        # at runtime so operators with different hardware profiles can tune without
        # a deploy.
        "runner_liveness_seconds",
        "runner_degraded_failure_threshold",
        "runner_low_disk_threshold_bytes",
        "runner_low_memory_threshold_bytes",
        "runner_escalation_age_seconds",
        "runner_idle_gpu_threshold_pct",
        "runner_token_stale_days",
        "runner_post_update_silence_seconds",
        "runner_heartbeat_retention_days",
        "vm_low_disk_threshold_bytes",
        "vm_low_memory_threshold_bytes",
    }
)

# Operator-selectable notification event universe.  Only events in this set
# may appear in ``notification_config.enabled_events``; the external-webhook
# audience registry (``_WEBHOOK_EVENT_AUDIENCE`` in ``message_audience``) must
# contain a matching entry for every event here, or ``route_external_webhook_event``
# silently fails closed and the operator never receives it.
# Completeness is guarded by ``test_message_audience.test_valid_notification_events_all_present_in_webhook_registry``.
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
        # A task was held out of dispatch for a DELIBERATE spend/budget
        # decision (requires_approval + approval_hold_reason="spend_budget").
        # CREATED fires when the hold is first seen; EXPIRING fires once as
        # the configured spend_approval_timeout_hours window nears its end.
        # Both are operator-actionable, so they join the default-on list.
        "SPEND_HOLD_CREATED",
        "SPEND_HOLD_EXPIRING",
        "STRATEGIC_ENGINE_HALTED",
        # Runner-health fleet incidents dispatched by runner_health_alerts.
        # These are operator-actionable outages (a runner or the whole
        # fleet is down, out of disk, or an outage has aged past the
        # re-page threshold), so they join the default-on list below.
        "RUNNER_DEACTIVATED",
        "RUNNER_DEGRADED",
        "RUNNER_DEGRADED_ESCALATED",
        "RUNNER_FLEET_OFFLINE",
        "RUNNER_LOW_DISK",
        "RUNNER_LOW_MEMORY",
        "RUNNER_OUTAGE_ESCALATED",
        "RUNNER_LOCAL_HOLD",
        "RUNNER_CONTAINER_HOLD",
    ]
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


def _encrypt_notification_config(config: dict) -> dict:
    """Return a copy of config with webhook_url encrypted for DB persistence."""
    if not config:
        return config
    url = config.get("webhook_url", "")
    encrypted_url = encrypt_field(url) if url else url
    return {**config, "webhook_url": encrypted_url or ""}


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
                "SPEND_HOLD_CREATED",
                "SPEND_HOLD_EXPIRING",
                "STRATEGIC_ENGINE_HALTED",
                "RUNNER_DEACTIVATED",
                "RUNNER_DEGRADED",
                "RUNNER_DEGRADED_ESCALATED",
                "RUNNER_FLEET_OFFLINE",
                "RUNNER_LOW_DISK",
                "RUNNER_LOW_MEMORY",
                "RUNNER_OUTAGE_ESCALATED",
                "RUNNER_LOCAL_HOLD",
                "RUNNER_CONTAINER_HOLD",
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
    from app.models import UserIntegration
    from app.routers.admin_pool_accounts import list_authoritative_max_plan_accounts

    # Check if the user has a GitHub integration
    github_account = None
    stmt = select(UserIntegration).where(
        UserIntegration.user_id == current_user["user_id"]
    )
    integration = (await session.execute(stmt)).scalars().first()
    if integration and integration.github_account_name:
        github_account = integration.github_account_name

    max_plan_accounts = await list_authoritative_max_plan_accounts(
        session, current_user["user_id"]
    )

    # Surface the current fleet-wide capability-proof coverage ratio that the
    # auto-graduation observe/enforce thresholds are compared against, so the
    # operator can see where the gate stands rather than setting thresholds
    # blind. Best-effort: a failure here must never break the settings load.
    capability_proof_gate_coverage: dict = {
        "coverage_ratio": None,
        "total_proven": 0,
        "total_non_exempt_working": 0,
        "projects_contributing": 0,
        "num_projects": 0,
        "trial_eligible_unproven": 0,
    }
    try:
        from app.metrics_integrity_sweep import compute_fleet_proof_coverage

        _cov = await compute_fleet_proof_coverage(
            session,
            int(_runtime_config.get("metrics_integrity_sweep_max_projects", 25)),
        )
        # Expose only the scalar coverage summary — the internal
        # ``contributing_project_ids`` list (UUIDs, used by the graduation
        # notifier) is not part of the operator-facing settings surface.
        capability_proof_gate_coverage = {
            "coverage_ratio": _cov["coverage_ratio"],
            "total_proven": _cov["total_proven"],
            "total_non_exempt_working": _cov["total_non_exempt_working"],
            "projects_contributing": _cov["projects_contributing"],
            "num_projects": _cov["num_projects"],
            "trial_eligible_unproven": _cov.get("trial_eligible_unproven", 0),
            # Surface whether the coverage sample is truncated — the
            # auto-graduation guard already refuses to advance to enforce
            # when fleet_capped is True, so the operator needs visibility.
            "fleet_capped": _cov.get("fleet_capped", False),
            "fleet_total": _cov.get("fleet_total", 0),
        }
    except Exception:
        logger.warning(
            "failed to compute fleet capability-proof coverage for settings",
            exc_info=True,
        )

    # Projection: how many capabilities would be trial-eligible if the
    # execution-trials flag were enabled. Best-effort; must never break load.
    capability_proof_trial_projection: dict = {
        "trial_eligible": 0,
        "unproven_working": 0,
        "projects_contributing": 0,
        "num_projects": 0,
    }
    try:
        from app.metrics_integrity_sweep import compute_trial_eligibility_projection

        capability_proof_trial_projection = await compute_trial_eligibility_projection(
            session,
            int(_runtime_config.get("metrics_integrity_sweep_max_projects", 25)),
        )
    except Exception:
        logger.warning(
            "failed to compute capability-proof trial eligibility projection",
            exc_info=True,
        )

    # Fleet-wide usage-meter reliability summary. Best-effort: a failure here
    # must never break the settings load.
    _min_coverage_pct = int(_runtime_config.get("usage_headroom_min_coverage_pct", 80))
    meter_reliability: dict = {
        "meters_eligible": 0,
        "meters_known": 0,
        "meters_stale": 0,
        "meters_dark": 0,
        "meters_ineligible": 0,
        "dark_by_reason": {},
        "coverage_ratio": None,
        "persist_failure_count": 0,
        "armable": False,
    }
    try:
        meter_reliability = await _get_meter_reliability_stats(
            min_coverage_pct=_min_coverage_pct
        )
    except Exception:
        logger.warning(
            "failed to compute meter reliability stats for settings",
            exc_info=True,
        )

    # Leader-memory 'armable-since' marker for the auto-arm reconciler. None on
    # non-leader instances (they do not run the reconciler) or when not holding.
    _armable_since = _get_usage_headroom_armable_since()
    _armable_since_iso = utc_iso(_armable_since)

    return {
        "max_concurrent_vms": _runtime_config["max_concurrent_vms"],
        "managed_compute_enabled": bool(
            _runtime_config.get("managed_compute_enabled", True)
        ),
        "max_retries": _runtime_config["max_retries"],
        "max_autonomous_tasks_per_hour": _runtime_config[
            "max_autonomous_tasks_per_hour"
        ],
        "max_global_autonomous_tasks_per_hour": _runtime_config.get(
            "max_global_autonomous_tasks_per_hour", 500
        ),
        "autonomous_spawn_budget_mode": str(
            _runtime_config.get("autonomous_spawn_budget_mode", "observe")
        ),
        "max_global_autonomous_initiative_tasks_per_hour": int(
            _runtime_config.get("max_global_autonomous_initiative_tasks_per_hour", 50)
        ),
        "autonomous_spawn_budget_categories": _runtime_config.get(
            "autonomous_spawn_budget_categories",
            default_autonomous_spawn_budget_categories(),
        ),
        "autonomous_research_interval_minutes": _runtime_config.get(
            "autonomous_research_interval_minutes", 30
        ),
        "evergreen_fill_mode": str(_runtime_config.get("evergreen_fill_mode", "off")),
        "evergreen_fill_interval_minutes": int(
            _runtime_config.get("evergreen_fill_interval_minutes", 15)
        ),
        "evergreen_fill_util_threshold": float(
            _runtime_config.get("evergreen_fill_util_threshold", 0.5)
        ),
        "evergreen_fill_min_free_slots": int(
            _runtime_config.get("evergreen_fill_min_free_slots", 2)
        ),
        "evergreen_fill_max_active": int(
            _runtime_config.get("evergreen_fill_max_active", 1)
        ),
        "evergreen_fill_daily_cap": int(
            _runtime_config.get("evergreen_fill_daily_cap", 3)
        ),
        "evergreen_fill_deferred_memo_enrollment": bool(
            _runtime_config.get("evergreen_fill_deferred_memo_enrollment", False)
        ),
        "structural_health_audit_mode": str(
            _runtime_config.get("structural_health_audit_mode", "off")
        ),
        "structural_health_audit_interval_minutes": int(
            _runtime_config.get("structural_health_audit_interval_minutes", 60)
        ),
        "structural_health_audit_fat_row_bytes": int(
            _runtime_config.get("structural_health_audit_fat_row_bytes", 1048576)
        ),
        "metrics_integrity_sweep_mode": str(
            _runtime_config.get("metrics_integrity_sweep_mode", "observe")
        ),
        "capability_proof_gate_mode": str(
            _runtime_config.get("capability_proof_gate_mode", "off") or "off"
        ),
        "capability_proof_gate_auto_graduation_enabled": bool(
            _runtime_config.get("capability_proof_gate_auto_graduation_enabled", False)
        ),
        "capability_proof_gate_auto_graduation_observe_threshold": float(
            _runtime_config.get(
                "capability_proof_gate_auto_graduation_observe_threshold", 0.5
            )
        ),
        "capability_proof_gate_auto_graduation_enforce_threshold": float(
            _runtime_config.get(
                "capability_proof_gate_auto_graduation_enforce_threshold", 0.95
            )
        ),
        "capability_proof_gate_coverage": capability_proof_gate_coverage,
        "capability_proof_trial_projection": capability_proof_trial_projection,
        "capability_proof_execution_trials_enabled": bool(
            _runtime_config.get("capability_proof_execution_trials_enabled", False)
        ),
        "metrics_integrity_sweep_interval_minutes": int(
            _runtime_config.get("metrics_integrity_sweep_interval_minutes", 60)
        ),
        "metrics_integrity_sweep_max_checks": int(
            _runtime_config.get("metrics_integrity_sweep_max_checks", 30)
        ),
        "metrics_integrity_sweep_max_projects": int(
            _runtime_config.get("metrics_integrity_sweep_max_projects", 25)
        ),
        "metrics_integrity_sweep_sample_limit": int(
            _runtime_config.get("metrics_integrity_sweep_sample_limit", 200)
        ),
        "metrics_integrity_sweep_window_days": int(
            _runtime_config.get("metrics_integrity_sweep_window_days", 14)
        ),
        "completion_debt_weight_mode": str(
            _runtime_config.get("completion_debt_weight_mode", "observe")
        ),
        "adversarial_spawn_gate_infra_failure_mode": str(
            _runtime_config.get(
                "adversarial_spawn_gate_infra_failure_mode", "observe"
            )
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
        "deploy_repo_identity": _runtime_config.get("deploy_repo_identity"),
        # Resolved identity combines the DEPLOY_REPO_IDENTITY env var (stamped by
        # the deploy pipeline) with the operator runtime-config override.  Operators
        # can read this field to confirm whether deploy-level serving confirmation
        # (backend_revision, agent_image) is active; None means all promotions fall
        # back to merge-level confirmation only.
        "deploy_identity_resolved": (
            "/".join(_resolved_identity)
            if (_resolved_identity := _deploy_repo_identity()) is not None
            else None
        ),
        "providers": _build_provider_catalog(_runtime_config),
        # Published, backend-owned task-capability contract. Operator/agent
        # surfaces consume this map instead of maintaining private task-type
        # tables; it is purely additive so existing settings consumers are
        # unaffected. Also available standalone at GET /api/admin/task-capabilities.
        "task_capabilities": task_capability_contract(),
        "provider_settings_defaults": _runtime_config.get(
            "provider_settings_defaults", {}
        ),
        "inline_llm": _runtime_config.get("inline_llm", {}),
        "provider_settings_by_effort": _runtime_config.get(
            "provider_settings_by_effort", {}
        ),
        "task_type_provider_overrides": _runtime_config.get(
            "task_type_provider_overrides", {}
        ),
        "health_check_urls": _runtime_config.get("health_check_urls", []),
        "usage_headroom_gate_enabled": bool(
            _runtime_config.get("usage_headroom_gate_enabled", False)
        ),
        # Auto-arm reconciler state (closes the loop: usage_headroom_gate_enabled
        # is now the reconciler-written EFFECTIVE state in auto mode). Additive —
        # existing consumers keep their fields unchanged.
        "usage_headroom_auto_arm_mode": str(
            _runtime_config.get("usage_headroom_auto_arm_mode", "auto")
        ),
        "usage_headroom_auto_arm_observation_minutes": int(
            _runtime_config.get("usage_headroom_auto_arm_observation_minutes", 60)
        ),
        # Leader-memory marker of when `armable` first became (and has since
        # continuously held) True. None on non-leader instances or when not
        # currently holding — the sustained-window clock the reconciler uses.
        "usage_headroom_armable_since": _armable_since_iso,
        "usage_headroom_min_coverage_pct": _min_coverage_pct,
        "usage_headroom_threshold_pct": _runtime_config.get(
            "usage_headroom_threshold_pct", 90
        ),
        "usage_headroom_long_running_task_types": _runtime_config.get(
            "usage_headroom_long_running_task_types", []
        ),
        "meter_reliability": meter_reliability,
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
        "direction_refractory_minutes": _runtime_config.get(
            "direction_refractory_minutes",
            _runtime_config.get("autonomous_direction_cooldown_minutes", 240),
        ),
        "direction_saturation_refractory_minutes": _runtime_config.get(
            "direction_saturation_refractory_minutes",
            _runtime_config.get(
                "direction_refractory_minutes",
                _runtime_config.get("autonomous_direction_cooldown_minutes", 240),
            )
            * 2,
        ),
        "max_parallel_research_per_project": _runtime_config.get(
            "max_parallel_research_per_project", 6
        ),
        "max_total_research_per_project": _runtime_config.get(
            "max_total_research_per_project", 5
        ),
        "evaluating_pipeline_timeout_minutes": _runtime_config.get(
            "evaluating_pipeline_timeout_minutes", 120
        ),
        "evaluating_fail_hours": _runtime_config.get("evaluating_fail_hours", 48),
        "spend_approval_timeout_hours": _runtime_config.get(
            "spend_approval_timeout_hours", 24
        ),
        "ci_backstop_timeout_minutes": _runtime_config.get(
            "ci_backstop_timeout_minutes", 60
        ),
        "strategic_continuation_ttl_seconds": _runtime_config.get(
            "strategic_continuation_ttl_seconds", 1500
        ),
        "strategic_local_hold_timeout_seconds": _runtime_config.get(
            "strategic_local_hold_timeout_seconds", 900
        ),
        "strategic_session_idle_tick_seconds": _runtime_config.get(
            "strategic_session_idle_tick_seconds", 5
        ),
        "strategic_session_turn_timeout_seconds": _runtime_config.get(
            "strategic_session_turn_timeout_seconds", 120
        ),
        "strategic_session_exploration_interval_seconds": _runtime_config.get(
            "strategic_session_exploration_interval_seconds", 86_400
        ),
        "strategic_session_max_ops_per_turn": _runtime_config.get(
            "strategic_session_max_ops_per_turn", 3
        ),
        "strategic_session_autonomous_turn_timeout_seconds": _runtime_config.get(
            "strategic_session_autonomous_turn_timeout_seconds", 300
        ),
        "strategic_session_max_unattended_seconds": _runtime_config.get(
            "strategic_session_max_unattended_seconds", 7200
        ),
        "promotion_enabled": bool(_runtime_config.get("promotion_enabled", False)),
        "promotion_enabled_by_project": {
            str(k): bool(v)
            for k, v in (
                _runtime_config.get("promotion_enabled_by_project") or {}
            ).items()
        },
        "env_preflight_enabled": bool(_runtime_config.get("env_preflight_enabled", True)),
        "smoketest_enabled": bool(_runtime_config.get("smoketest_enabled", False)),
        "merge_grade_block_threshold": float(
            _runtime_config.get("merge_grade_block_threshold", 0.0) or 0.0
        ),
        "no_ci_test_gate_enabled": bool(
            _runtime_config.get("no_ci_test_gate_enabled", True)
        ),
        "promotion_threshold": _runtime_config.get("promotion_threshold"),
        "promotion_weights": _runtime_config.get("promotion_weights"),
        "warm_continuity_enabled": bool(
            _runtime_config.get("warm_continuity_enabled", False)
        ),
        "warm_backlog_reuse_enabled": bool(
            _runtime_config.get("warm_backlog_reuse_enabled", False)
        ),
        "rollup_merge_queue_enabled": bool(
            _runtime_config.get("rollup_merge_queue_enabled", True)
        ),
        "cochange_decomposition_enabled": bool(
            _runtime_config.get("cochange_decomposition_enabled", False)
        ),
        "research_quality_grader_enabled": bool(
            _runtime_config.get("research_quality_grader_enabled", False)
        ),
        "park_and_retarget_enabled": bool(
            _runtime_config.get("park_and_retarget_enabled", False)
        ),
        "park_and_retarget_budget_seconds": int(
            _runtime_config.get("park_and_retarget_budget_seconds", 600)
        ),
        "improvement_lanes_enabled": bool(
            _runtime_config.get("improvement_lanes_enabled", False)
        ),
        "review_fix_lane_pilot_enabled": bool(
            _runtime_config.get("review_fix_lane_pilot_enabled", False)
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
        "warm_pool_target_size": int(_runtime_config.get("warm_pool_target_size", 2)),
        "warm_pool_target_size_by_project": _normalize_warm_pool_target_map(
            _runtime_config.get("warm_pool_target_size_by_project") or {}
        ),
        "max_concurrent_tasks_by_project": {
            str(k): int(v)
            for k, v in (
                _runtime_config.get("max_concurrent_tasks_by_project") or {}
            ).items()
            if v and int(v) > 0
        },
        "max_concurrent_compute_jobs_by_project": {
            str(k): int(v)
            for k, v in (
                _runtime_config.get("max_concurrent_compute_jobs_by_project") or {}
            ).items()
            if v and int(v) > 0
        },
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
        "module_interfaces_holdback_fraction": float(
            _runtime_config.get("module_interfaces_holdback_fraction", 0.1)
        ),
        "related_tests_holdback_fraction": float(
            _runtime_config.get("related_tests_holdback_fraction", 0.1)
        ),
        "affected_consumers_holdback_fraction": float(
            _runtime_config.get("affected_consumers_holdback_fraction", 0.1)
        ),
        "repo_map_holdback_fraction": float(
            _runtime_config.get("repo_map_holdback_fraction", 0.1)
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
        "monthly_budget_target_usd": float(
            _runtime_config.get("monthly_budget_target_usd", 0)
        ),
        "compute_experiment_spend_threshold_usd": _runtime_config.get(
            "compute_experiment_spend_threshold_usd", 10.0
        ),
        "auto_debug_enabled": _runtime_config.get("auto_debug_enabled", True),
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
            "strategic_max_interval_minutes", 1_440
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
                        "SPEND_HOLD_CREATED",
                        "SPEND_HOLD_EXPIRING",
                        "STRATEGIC_ENGINE_HALTED",
                        "RUNNER_DEACTIVATED",
                        "RUNNER_DEGRADED",
                        "RUNNER_DEGRADED_ESCALATED",
                        "RUNNER_FLEET_OFFLINE",
                        "RUNNER_LOW_DISK",
                        "RUNNER_LOW_MEMORY",
                        "RUNNER_OUTAGE_ESCALATED",
                    ],
                    "throttle_seconds": 30,
                },
            ),
            is_admin=current_user.get("role") == "admin",
        ),
        "using_max_plan": len(
            [
                a
                for a in max_plan_accounts
                if a.get("enabled", True)
                and a.get("provider", LLMProviderName.ANTHROPIC.value)
                == LLMProviderName.ANTHROPIC.value
                and a.get("auth_mode", LLMAuthMode.OAUTH.value)
                == LLMAuthMode.OAUTH.value
            ]
        )
        > 0,
        "max_plan_accounts": max_plan_accounts,
        "cloud_run_task_type_allowlist": sorted(
            _runtime_config.get("cloud_run_task_type_allowlist") or []
        ),
        "direction_gap_respawn_block_mode": str(
            _runtime_config.get("direction_gap_respawn_block_mode", "observe") or "observe"
        ),
        "direction_gap_respawn_cooldown_days": float(
            _runtime_config.get("direction_gap_respawn_cooldown_days", 14) or 14
        ),
        # Direction guardrail knobs
        "direction_churn_window_cycles": int(
            _runtime_config.get("direction_churn_window_cycles", 5)
        ),
        "direction_churn_non_merge_threshold": int(
            _runtime_config.get("direction_churn_non_merge_threshold", 15)
        ),
        "direction_churn_min_merge_rate": float(
            _runtime_config.get("direction_churn_min_merge_rate", 0.10)
        ),
        "direction_lifetime_non_merge_threshold": int(
            _runtime_config.get("direction_lifetime_non_merge_threshold", 150)
        ),
        "direction_lifetime_min_merge_rate": float(
            _runtime_config.get("direction_lifetime_min_merge_rate", 0.10)
        ),
        "direction_review_recommendation_threshold": int(
            _runtime_config.get("direction_review_recommendation_threshold", 2)
        ),
        # Runner health thresholds
        "runner_liveness_seconds": int(
            _runtime_config.get("runner_liveness_seconds", 300)
        ),
        "runner_degraded_failure_threshold": int(
            _runtime_config.get("runner_degraded_failure_threshold", 3)
        ),
        "runner_low_disk_threshold_bytes": int(
            _runtime_config.get("runner_low_disk_threshold_bytes", 1_073_741_824)
        ),
        "runner_low_memory_threshold_bytes": int(
            _runtime_config.get("runner_low_memory_threshold_bytes", 536_870_912)
        ),
        "runner_escalation_age_seconds": int(
            _runtime_config.get("runner_escalation_age_seconds", 14_400)
        ),
        "runner_idle_gpu_threshold_pct": int(
            _runtime_config.get("runner_idle_gpu_threshold_pct", 10)
        ),
        "runner_token_stale_days": int(
            _runtime_config.get("runner_token_stale_days", 90)
        ),
        "runner_post_update_silence_seconds": int(
            _runtime_config.get("runner_post_update_silence_seconds", 600)
        ),
        "runner_heartbeat_retention_days": int(
            _runtime_config.get("runner_heartbeat_retention_days", 7)
        ),
        "vm_low_disk_threshold_bytes": int(
            _runtime_config.get("vm_low_disk_threshold_bytes", 1_073_741_824)
        ),
        "vm_low_memory_threshold_bytes": int(
            _runtime_config.get("vm_low_memory_threshold_bytes", 536_870_912)
        ),
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
    # Dormant-knob debts to resolve when a knob graduates to an enforcing value.
    _debt_keys_to_resolve: list[str] = []

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
    if "managed_compute_enabled" in payload:
        val = payload["managed_compute_enabled"]
        if not isinstance(val, bool):
            raise HTTPException(
                status_code=400, detail="managed_compute_enabled must be a boolean"
            )
        _runtime_config["managed_compute_enabled"] = val
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
    if "autonomous_spawn_budget_mode" in payload:
        try:
            _runtime_config["autonomous_spawn_budget_mode"] = (
                normalize_autonomous_spawn_budget_mode(
                    payload["autonomous_spawn_budget_mode"]
                )
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if _runtime_config["autonomous_spawn_budget_mode"] == "enforce":
            _debt_keys_to_resolve.append(
                "dormant_knob:autonomous_spawn_budget_mode"
            )
    if "max_global_autonomous_initiative_tasks_per_hour" in payload:
        try:
            _runtime_config["max_global_autonomous_initiative_tasks_per_hour"] = (
                normalize_autonomous_initiative_cap(
                    payload["max_global_autonomous_initiative_tasks_per_hour"]
                )
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    if "autonomous_spawn_budget_categories" in payload:
        try:
            _runtime_config["autonomous_spawn_budget_categories"] = (
                normalize_autonomous_spawn_budget_categories(
                    payload["autonomous_spawn_budget_categories"]
                )
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
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
    if "evergreen_fill_mode" in payload:
        val = str(payload["evergreen_fill_mode"] or "").strip().lower()
        if val not in ("off", "observe", "enforce"):
            raise HTTPException(
                status_code=400,
                detail="evergreen_fill_mode must be one of: off, observe, enforce",
            )
        _runtime_config["evergreen_fill_mode"] = val
        if val == "enforce":
            _debt_keys_to_resolve.append("dormant_knob:evergreen_fill_mode")
    if "evergreen_fill_interval_minutes" in payload:
        try:
            val = int(payload["evergreen_fill_interval_minutes"])
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=400,
                detail="evergreen_fill_interval_minutes must be a number",
            )
        if not (1 <= val <= 1440):
            raise HTTPException(
                status_code=400,
                detail="evergreen_fill_interval_minutes must be between 1 and 1440",
            )
        _runtime_config["evergreen_fill_interval_minutes"] = val
    if "evergreen_fill_util_threshold" in payload:
        try:
            fval = float(payload["evergreen_fill_util_threshold"])
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=400,
                detail="evergreen_fill_util_threshold must be a number",
            )
        if not (0.0 <= fval <= 1.0):
            raise HTTPException(
                status_code=400,
                detail="evergreen_fill_util_threshold must be between 0.0 and 1.0",
            )
        _runtime_config["evergreen_fill_util_threshold"] = fval
    if "evergreen_fill_min_free_slots" in payload:
        try:
            val = int(payload["evergreen_fill_min_free_slots"])
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=400,
                detail="evergreen_fill_min_free_slots must be a number",
            )
        if not (0 <= val <= 100):
            raise HTTPException(
                status_code=400,
                detail="evergreen_fill_min_free_slots must be between 0 and 100",
            )
        _runtime_config["evergreen_fill_min_free_slots"] = val
    if "evergreen_fill_max_active" in payload:
        try:
            val = int(payload["evergreen_fill_max_active"])
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=400,
                detail="evergreen_fill_max_active must be a number",
            )
        if not (0 <= val <= 50):
            raise HTTPException(
                status_code=400,
                detail="evergreen_fill_max_active must be between 0 and 50",
            )
        _runtime_config["evergreen_fill_max_active"] = val
    if "evergreen_fill_daily_cap" in payload:
        try:
            val = int(payload["evergreen_fill_daily_cap"])
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=400,
                detail="evergreen_fill_daily_cap must be a number",
            )
        if not (0 <= val <= 200):
            raise HTTPException(
                status_code=400,
                detail="evergreen_fill_daily_cap must be between 0 and 200",
            )
        _runtime_config["evergreen_fill_daily_cap"] = val
    if "evergreen_fill_deferred_memo_enrollment" in payload:
        raw = payload["evergreen_fill_deferred_memo_enrollment"]
        if not isinstance(raw, bool):
            raise HTTPException(
                status_code=400,
                detail="evergreen_fill_deferred_memo_enrollment must be a boolean",
            )
        _runtime_config["evergreen_fill_deferred_memo_enrollment"] = raw
    if "structural_health_audit_mode" in payload:
        val = str(payload["structural_health_audit_mode"] or "").strip().lower()
        if val not in ("off", "observe", "enforce"):
            raise HTTPException(
                status_code=400,
                detail="structural_health_audit_mode must be one of: off, observe, enforce",
            )
        _runtime_config["structural_health_audit_mode"] = val
        if val == "enforce":
            _debt_keys_to_resolve.append("dormant_knob:structural_health_audit_mode")
    if "completion_debt_weight_mode" in payload:
        val = str(payload["completion_debt_weight_mode"] or "").strip().lower()
        if val not in ("off", "observe", "enforce"):
            raise HTTPException(
                status_code=400,
                detail="completion_debt_weight_mode must be one of: off, observe, enforce",
            )
        _runtime_config["completion_debt_weight_mode"] = val
        # Graduating the debt-pressure signal to enforce resolves its standing
        # dormant-knob completion-debt row (mirrors evergreen_fill_mode).
        if val == "enforce":
            _debt_keys_to_resolve.append("dormant_knob:completion_debt_weight_mode")
    if "adversarial_spawn_gate_infra_failure_mode" in payload:
        val = (
            str(payload["adversarial_spawn_gate_infra_failure_mode"] or "")
            .strip()
            .lower()
        )
        if val not in ("observe", "enforce"):
            raise HTTPException(
                status_code=400,
                detail=(
                    "adversarial_spawn_gate_infra_failure_mode must be one of: "
                    "observe, enforce"
                ),
            )
        _runtime_config["adversarial_spawn_gate_infra_failure_mode"] = val
    if "structural_health_audit_interval_minutes" in payload:
        try:
            val = int(payload["structural_health_audit_interval_minutes"])
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=400,
                detail="structural_health_audit_interval_minutes must be a number",
            )
        if not (1 <= val <= 1440):
            raise HTTPException(
                status_code=400,
                detail="structural_health_audit_interval_minutes must be between 1 and 1440",
            )
        _runtime_config["structural_health_audit_interval_minutes"] = val
    if "structural_health_audit_fat_row_bytes" in payload:
        try:
            val = int(payload["structural_health_audit_fat_row_bytes"])
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=400,
                detail="structural_health_audit_fat_row_bytes must be a number",
            )
        # Floor at 1 KiB so an operator can't set a pathological near-zero
        # threshold that flags every row; ceiling well above the worst outage row.
        if not (1024 <= val <= 1_073_741_824):
            raise HTTPException(
                status_code=400,
                detail="structural_health_audit_fat_row_bytes must be between 1024 and 1073741824",
            )
        _runtime_config["structural_health_audit_fat_row_bytes"] = val
    if "metrics_integrity_sweep_mode" in payload:
        val = str(payload["metrics_integrity_sweep_mode"] or "").strip().lower()
        if val not in ("off", "observe", "enforce"):
            raise HTTPException(
                status_code=400,
                detail="metrics_integrity_sweep_mode must be one of: off, observe, enforce",
            )
        _runtime_config["metrics_integrity_sweep_mode"] = val
        if val == "enforce":
            _debt_keys_to_resolve.append("dormant_knob:metrics_integrity_sweep_mode")
    if "capability_proof_gate_mode" in payload:
        val = str(payload["capability_proof_gate_mode"] or "").strip().lower()
        if val not in ("off", "observe", "enforce"):
            raise HTTPException(
                status_code=400,
                detail="capability_proof_gate_mode must be one of: off, observe, enforce",
            )
        _runtime_config["capability_proof_gate_mode"] = val
        if val == "enforce":
            _debt_keys_to_resolve.append("dormant_knob:capability_proof_gate_mode")
    if "capability_proof_gate_auto_graduation_enabled" in payload:
        val = payload["capability_proof_gate_auto_graduation_enabled"]
        if not isinstance(val, bool):
            raise HTTPException(
                status_code=400,
                detail="capability_proof_gate_auto_graduation_enabled must be a boolean",
            )
        _runtime_config["capability_proof_gate_auto_graduation_enabled"] = val
    if "capability_proof_execution_trials_enabled" in payload:
        val = payload["capability_proof_execution_trials_enabled"]
        if not isinstance(val, bool):
            raise HTTPException(
                status_code=400,
                detail="capability_proof_execution_trials_enabled must be a boolean",
            )
        _runtime_config["capability_proof_execution_trials_enabled"] = val
    for _cap_grad_key, _cap_grad_default in (
        ("capability_proof_gate_auto_graduation_observe_threshold", 0.5),
        ("capability_proof_gate_auto_graduation_enforce_threshold", 0.95),
    ):
        if _cap_grad_key in payload:
            try:
                fval = float(payload[_cap_grad_key])
            except (ValueError, TypeError):
                raise HTTPException(
                    status_code=400,
                    detail=f"{_cap_grad_key} must be a number",
                )
            if not (0.0 <= fval <= 1.0):
                raise HTTPException(
                    status_code=400,
                    detail=f"{_cap_grad_key} must be between 0.0 and 1.0",
                )
            _runtime_config[_cap_grad_key] = round(fval, 4)
    for _mi_key, _mi_lo, _mi_hi in (
        ("metrics_integrity_sweep_interval_minutes", 1, 1440),
        ("metrics_integrity_sweep_max_checks", 1, 1000),
        ("metrics_integrity_sweep_max_projects", 1, 10000),
        ("metrics_integrity_sweep_sample_limit", 1, 100000),
        ("metrics_integrity_sweep_window_days", 1, 365),
    ):
        if _mi_key in payload:
            try:
                val = int(payload[_mi_key])
            except (ValueError, TypeError):
                raise HTTPException(
                    status_code=400,
                    detail=f"{_mi_key} must be a number",
                )
            if not (_mi_lo <= val <= _mi_hi):
                raise HTTPException(
                    status_code=400,
                    detail=f"{_mi_key} must be between {_mi_lo} and {_mi_hi}",
                )
            _runtime_config[_mi_key] = val
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
    if "env_preflight_enabled" in payload:
        _runtime_config["env_preflight_enabled"] = bool(payload["env_preflight_enabled"])
    if "smoketest_enabled" in payload:
        _runtime_config["smoketest_enabled"] = bool(payload["smoketest_enabled"])
    if "no_ci_test_gate_enabled" in payload:
        _runtime_config["no_ci_test_gate_enabled"] = bool(
            payload["no_ci_test_gate_enabled"]
        )
    if "usage_headroom_gate_enabled" in payload:
        val = payload["usage_headroom_gate_enabled"]
        if not isinstance(val, bool):
            raise HTTPException(
                status_code=400,
                detail="usage_headroom_gate_enabled must be a boolean",
            )
        _runtime_config["usage_headroom_gate_enabled"] = val
    if "usage_headroom_auto_arm_mode" in payload:
        val = payload["usage_headroom_auto_arm_mode"]
        if not isinstance(val, str) or val.strip().lower() not in (
            "auto",
            "manual",
            "off",
        ):
            raise HTTPException(
                status_code=400,
                detail="usage_headroom_auto_arm_mode must be one of "
                "'auto', 'manual', 'off'",
            )
        _runtime_config["usage_headroom_auto_arm_mode"] = val.strip().lower()
    if "usage_headroom_auto_arm_observation_minutes" in payload:
        try:
            val = int(payload["usage_headroom_auto_arm_observation_minutes"])
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=400,
                detail="usage_headroom_auto_arm_observation_minutes must be a number",
            )
        # A positive number of minutes with a sane upper bound (1 day). The
        # window governs how long `armable` must hold before auto mode arms.
        if not (1 <= val <= 1440):
            raise HTTPException(
                status_code=400,
                detail="usage_headroom_auto_arm_observation_minutes must be "
                "between 1 and 1440",
            )
        _runtime_config["usage_headroom_auto_arm_observation_minutes"] = val
    if "usage_headroom_threshold_pct" in payload:
        try:
            val = int(payload["usage_headroom_threshold_pct"])
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=400,
                detail="usage_headroom_threshold_pct must be a number",
            )
        # A utilization percentage in [1, 100]. 0 would gate out every probed
        # account (defeating the advisory intent); >100 is unreachable.
        if not (1 <= val <= 100):
            raise HTTPException(
                status_code=400,
                detail="usage_headroom_threshold_pct must be between 1 and 100",
            )
        _runtime_config["usage_headroom_threshold_pct"] = val
    if "usage_headroom_min_coverage_pct" in payload:
        try:
            val = int(payload["usage_headroom_min_coverage_pct"])
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=400,
                detail="usage_headroom_min_coverage_pct must be a number",
            )
        if not (0 <= val <= 100):
            raise HTTPException(
                status_code=400,
                detail="usage_headroom_min_coverage_pct must be between 0 and 100",
            )
        _runtime_config["usage_headroom_min_coverage_pct"] = val
    if "usage_headroom_long_running_task_types" in payload:
        raw = payload["usage_headroom_long_running_task_types"]
        if not isinstance(raw, list) or not all(isinstance(t, str) for t in raw):
            raise HTTPException(
                status_code=400,
                detail=(
                    "usage_headroom_long_running_task_types must be a list of "
                    "task-type strings"
                ),
            )
        cleaned_types = sorted({t.strip() for t in raw if t.strip()})
        _runtime_config["usage_headroom_long_running_task_types"] = cleaned_types
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
        _ACTIVE_WEIGHT_KEYS = ("audit", "test_erosion")
        _allowed_weight_keys = set(_ACTIVE_WEIGHT_KEYS) | {"smoketest", "completion_debt"}
        missing_core = set(_ACTIVE_WEIGHT_KEYS) - set(val.keys())
        extra_keys = set(val.keys()) - _allowed_weight_keys
        if missing_core or extra_keys:
            raise HTTPException(
                status_code=400,
                detail="promotion_weights requires audit and test_erosion; legacy smoketest and completion_debt are optional",
            )
        declared_keys = [*_ACTIVE_WEIGHT_KEYS]
        if "smoketest" in val:
            declared_keys.append("smoketest")
        cleaned_weights: Dict[str, float] = {}
        for wk in declared_keys:
            try:
                wv = float(val[wk])
            except (ValueError, TypeError):
                raise HTTPException(status_code=400, detail=f"promotion_weights[{wk}] must be a number")
            if wv < 0.0:
                raise HTTPException(status_code=400, detail=f"promotion_weights[{wk}] must be non-negative")
            cleaned_weights[wk] = wv
        declared_sum = sum(cleaned_weights.values())
        if abs(declared_sum - 1.0) > 0.01:
            raise HTTPException(status_code=400, detail=f"promotion_weights must sum to ~1.0 (got {declared_sum:.4f})")
        from decimal import Decimal

        active_decimals = {
            wk: Decimal(str(val[wk])) for wk in _ACTIVE_WEIGHT_KEYS
        }
        active_sum = sum(active_decimals.values(), Decimal("0"))
        if active_sum <= 0:
            raise HTTPException(status_code=400, detail="active promotion weights must be positive")
        stored_weights = {
            wk: float(active_decimals[wk] / active_sum) for wk in _ACTIVE_WEIGHT_KEYS
        }
        # Optional completion_debt blend factor: a number in [0.0, 0.5].
        if "completion_debt" in val:
            try:
                cd_weight = float(val["completion_debt"])
            except (ValueError, TypeError):
                raise HTTPException(
                    status_code=400,
                    detail="promotion_weights[completion_debt] must be a number",
                )
            if not (0.0 <= cd_weight <= 0.5):
                raise HTTPException(
                    status_code=400,
                    detail="promotion_weights[completion_debt] must be between 0.0 and 0.5",
                )
            stored_weights["completion_debt"] = round(cd_weight, 4)
        _runtime_config["promotion_weights"] = stored_weights
    if "merge_grade_block_threshold" in payload:
        try:
            val = float(payload["merge_grade_block_threshold"])
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=400, detail="merge_grade_block_threshold must be a number"
            )
        if not (0.0 <= val <= 1.0):
            raise HTTPException(
                status_code=400,
                detail="merge_grade_block_threshold must be between 0.0 and 1.0",
            )
        _runtime_config["merge_grade_block_threshold"] = round(val, 4)
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
    if "spend_approval_timeout_hours" in payload:
        try:
            val = int(payload["spend_approval_timeout_hours"])
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=400,
                detail="spend_approval_timeout_hours must be a number",
            )
        # Deliberate spend/budget holds get a longer, out-of-band window than
        # the review-escalation 2h gate. Cap at 168h (1 week) like
        # evaluating_fail_hours; min 1h.
        if not (1 <= val <= 168):
            raise HTTPException(
                status_code=400,
                detail="spend_approval_timeout_hours must be between 1 and 168",
            )
        _runtime_config["spend_approval_timeout_hours"] = val
    # Strategic-session integer knobs. Each is clamped to a sane range:
    # the continuation TTL and local-hold timeout are the safety windows (min 60s
    # so a live session can never be reaped mid-turn; ceiling 7200s / 2h so an
    # operator can NEVER reintroduce a >12h local-pin wall); the idle tick and
    # turn timeout bound the session loop cadence; sparse exploration is a
    # model-call fallback rather than liveness and is validated separately so
    # 0 can disable it without admitting sub-hour churn; the unattended cap
    # bounds total autonomous lifetime (ceiling 8h, still well under a 12h wall).
    for _key, _lo, _hi in (
        ("strategic_continuation_ttl_seconds", 60, 7_200),
        ("strategic_local_hold_timeout_seconds", 60, 7_200),
        ("strategic_session_idle_tick_seconds", 1, 300),
        ("strategic_session_turn_timeout_seconds", 10, 3_600),
        # Soft per-turn op cap: 1..20. Advisory prompt hint only; the hard
        # per-op spawn/retire gates are unaffected.
        ("strategic_session_max_ops_per_turn", 1, 20),
        # Deep-turn stall budget for autonomous turns: 30..3600s, all under the
        # 15-min absolute hard ceiling enforced agent-side.
        ("strategic_session_autonomous_turn_timeout_seconds", 30, 3_600),
        ("strategic_session_max_unattended_seconds", 60, 28_800),
    ):
        if _key in payload:
            try:
                _val = int(payload[_key])
            except (ValueError, TypeError):
                raise HTTPException(
                    status_code=400, detail=f"{_key} must be a number"
                )
            if not (_lo <= _val <= _hi):
                raise HTTPException(
                    status_code=400,
                    detail=f"{_key} must be between {_lo} and {_hi}",
                )
            _runtime_config[_key] = _val
    if "strategic_session_exploration_interval_seconds" in payload:
        try:
            _exploration_interval = int(
                payload["strategic_session_exploration_interval_seconds"]
            )
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=400,
                detail=(
                    "strategic_session_exploration_interval_seconds must be a number"
                ),
            )
        # Match strategic_max_interval_minutes's one-week policy window while
        # retaining the anti-churn floor for enabled exploration. Semantic,
        # watch, operator, follow-up, and first-turn wakes bypass this interval.
        if _exploration_interval != 0 and not (
            3_600 <= _exploration_interval <= 604_800
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    "strategic_session_exploration_interval_seconds must be 0 "
                    "(disabled) or between 3600 and 604800 (1 hour to 1 week)"
                ),
            )
        _runtime_config["strategic_session_exploration_interval_seconds"] = (
            _exploration_interval
        )
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
    if "rollup_merge_queue_enabled" in payload:
        val = payload["rollup_merge_queue_enabled"]
        if not isinstance(val, bool):
            raise HTTPException(
                status_code=400,
                detail="rollup_merge_queue_enabled must be a boolean",
            )
        _runtime_config["rollup_merge_queue_enabled"] = val
    if "cochange_decomposition_enabled" in payload:
        val = payload["cochange_decomposition_enabled"]
        if not isinstance(val, bool):
            raise HTTPException(
                status_code=400,
                detail="cochange_decomposition_enabled must be a boolean",
            )
        _runtime_config["cochange_decomposition_enabled"] = val
    if "research_quality_grader_enabled" in payload:
        val = payload["research_quality_grader_enabled"]
        if not isinstance(val, bool):
            raise HTTPException(
                status_code=400,
                detail="research_quality_grader_enabled must be a boolean",
            )
        _runtime_config["research_quality_grader_enabled"] = val
    if "park_and_retarget_enabled" in payload:
        val = payload["park_and_retarget_enabled"]
        if not isinstance(val, bool):
            raise HTTPException(
                status_code=400,
                detail="park_and_retarget_enabled must be a boolean",
            )
        _runtime_config["park_and_retarget_enabled"] = val
    if "park_and_retarget_budget_seconds" in payload:
        try:
            val = int(payload["park_and_retarget_budget_seconds"])
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=400,
                detail="park_and_retarget_budget_seconds must be a number",
            ) from None
        if val < 30 or val > 1800:
            raise HTTPException(
                status_code=400,
                detail="park_and_retarget_budget_seconds must be between 30 and 1800",
            )
        _runtime_config["park_and_retarget_budget_seconds"] = val
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
    if "cloud_run_task_type_allowlist" in payload:
        from app.task_capabilities import READ_ONLY_TASK_TYPES

        raw = payload["cloud_run_task_type_allowlist"]
        # Accept a JSON list or comma-separated string of task type names.
        # An empty allowlist disables Cloud Run routing (default behavior).
        if isinstance(raw, str):
            raw = [t.strip() for t in raw.split(",") if t.strip()]
        if not isinstance(raw, list):
            raise HTTPException(
                status_code=400,
                detail=(
                    "cloud_run_task_type_allowlist must be a list of task type names "
                    "(or a comma-separated string)"
                ),
            )
        if not all(isinstance(t, str) for t in raw):
            raise HTTPException(
                status_code=400,
                detail="cloud_run_task_type_allowlist entries must be strings",
            )
        invalid = [t for t in raw if t not in READ_ONLY_TASK_TYPES]
        if invalid:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"cloud_run_task_type_allowlist contains write/unknown task types: "
                    f"{sorted(invalid)} — only read-only task types are eligible for "
                    f"Cloud Run routing. Valid types: {sorted(READ_ONLY_TASK_TYPES)}"
                ),
            )
        _runtime_config["cloud_run_task_type_allowlist"] = sorted(set(raw))
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
    if "max_concurrent_tasks_by_project" in payload:
        raw = payload["max_concurrent_tasks_by_project"]
        # Per-project concurrent-VM cap: a mapping of project-id strings to
        # positive integers in 1..50. An empty map means no per-project cap
        # for any project (all fall back to uncapped behavior). A zero or
        # missing entry for a given project also means no cap for that project.
        if not isinstance(raw, dict):
            raise HTTPException(
                status_code=400,
                detail=(
                    "max_concurrent_tasks_by_project must be a mapping of "
                    "project-id strings to integers"
                ),
            )
        cleaned_proj_caps: dict[str, int] = {}
        for key, value in raw.items():
            sid = str(key).strip()
            if not sid:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "max_concurrent_tasks_by_project keys must be "
                        "non-empty project ids"
                    ),
                )
            try:
                ival = int(value)
            except (TypeError, ValueError):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"max_concurrent_tasks_by_project[{sid}] must be an integer"
                    ),
                )
            if not (0 <= ival <= 50):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"max_concurrent_tasks_by_project[{sid}] must be "
                        "between 0 and 50"
                    ),
                )
            if ival > 0:
                cleaned_proj_caps[sid] = ival
        _runtime_config["max_concurrent_tasks_by_project"] = cleaned_proj_caps
    if "max_concurrent_compute_jobs_by_project" in payload:
        raw = payload["max_concurrent_compute_jobs_by_project"]
        # Per-project concurrent compute-job cap: a mapping of project-id strings
        # to positive integers in 0..50. An empty map removes all caps. A zero
        # or absent entry for a project means no cap for that project.
        if not isinstance(raw, dict):
            raise HTTPException(
                status_code=400,
                detail=(
                    "max_concurrent_compute_jobs_by_project must be a mapping of "
                    "project-id strings to integers"
                ),
            )
        cleaned_job_caps: dict[str, int] = {}
        for key, value in raw.items():
            sid = str(key).strip()
            if not sid:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "max_concurrent_compute_jobs_by_project keys must be "
                        "non-empty project ids"
                    ),
                )
            try:
                ival = int(value)
            except (TypeError, ValueError):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"max_concurrent_compute_jobs_by_project[{sid}] must be an integer"
                    ),
                )
            if not (0 <= ival <= 50):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"max_concurrent_compute_jobs_by_project[{sid}] must be "
                        "between 0 and 50"
                    ),
                )
            if ival > 0:
                cleaned_job_caps[sid] = ival
        _runtime_config["max_concurrent_compute_jobs_by_project"] = cleaned_job_caps
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
    for float_key in (
        "module_interfaces_holdback_fraction",
        "related_tests_holdback_fraction",
        "affected_consumers_holdback_fraction",
        "repo_map_holdback_fraction",
    ):
        if float_key in payload:
            try:
                val = float(payload[float_key])
            except (ValueError, TypeError):
                raise HTTPException(
                    status_code=400,
                    detail=f"{float_key} must be a number",
                )
            if not (0.0 <= val <= 1.0):
                raise HTTPException(
                    status_code=400,
                    detail=f"{float_key} must be between 0.0 and 1.0",
                )
            _runtime_config[float_key] = round(val, 4)
    if "diagnostics_after_edit_holdback_fraction" in payload:
        try:
            val = float(payload["diagnostics_after_edit_holdback_fraction"])
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=400,
                detail="diagnostics_after_edit_holdback_fraction must be a number",
            )
        if not (0.0 <= val <= 1.0):
            raise HTTPException(
                status_code=400,
                detail="diagnostics_after_edit_holdback_fraction must be between 0.0 and 1.0",
            )
        _runtime_config["diagnostics_after_edit_holdback_fraction"] = round(val, 3)
    if "diagnostics_after_edit_enabled" in payload:
        val = payload["diagnostics_after_edit_enabled"]
        if not isinstance(val, bool):
            raise HTTPException(
                status_code=400,
                detail="diagnostics_after_edit_enabled must be a boolean",
            )
        _runtime_config["diagnostics_after_edit_enabled"] = val
    if "improvement_lanes_enabled" in payload:
        val = payload["improvement_lanes_enabled"]
        if not isinstance(val, bool):
            raise HTTPException(
                status_code=400,
                detail="improvement_lanes_enabled must be a boolean",
            )
        if val:
            from app.warm_pool import is_warm_pool_enabled as _is_warm_pool_enabled

            if not _is_warm_pool_enabled():
                raise HTTPException(
                    status_code=422,
                    detail={
                        "warm_pool_enabled": "not met",
                        "fix_hint": "Set warm_pool_enabled=true via PATCH /api/admin/settings.",
                    },
                )
        _runtime_config["improvement_lanes_enabled"] = val
    if "review_fix_lane_pilot_enabled" in payload:
        val = payload["review_fix_lane_pilot_enabled"]
        if not isinstance(val, bool):
            raise HTTPException(
                status_code=400,
                detail="review_fix_lane_pilot_enabled must be a boolean",
            )
        if val:
            from app.warm_pool import is_warm_pool_enabled as _is_warm_pool_enabled2

            if not _is_warm_pool_enabled2():
                raise HTTPException(
                    status_code=422,
                    detail={
                        "warm_pool_enabled": "not met",
                        "fix_hint": "Set warm_pool_enabled=true via PATCH /api/admin/settings.",
                    },
                )
        _runtime_config["review_fix_lane_pilot_enabled"] = val
    if "direction_gap_respawn_block_mode" in payload:
        val = (
            str(payload["direction_gap_respawn_block_mode"] or "").strip().lower()
        )
        if val not in ("observe", "enforce"):
            raise HTTPException(
                status_code=400,
                detail=(
                    "direction_gap_respawn_block_mode must be one of: observe, enforce"
                ),
            )
        _runtime_config["direction_gap_respawn_block_mode"] = val
        if val == "enforce":
            _debt_keys_to_resolve.append("dormant_knob:direction_gap_respawn_block_mode")
    if "direction_gap_respawn_cooldown_days" in payload:
        try:
            fval = float(payload["direction_gap_respawn_cooldown_days"])
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=400,
                detail="direction_gap_respawn_cooldown_days must be a number",
            )
        if not (1.0 <= fval <= 365.0):
            raise HTTPException(
                status_code=400,
                detail="direction_gap_respawn_cooldown_days must be between 1 and 365",
            )
        _runtime_config["direction_gap_respawn_cooldown_days"] = fval
    if "direction_churn_window_cycles" in payload:
        try:
            val = int(payload["direction_churn_window_cycles"])
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=400,
                detail="direction_churn_window_cycles must be an integer",
            )
        if not (1 <= val <= 100):
            raise HTTPException(
                status_code=400,
                detail="direction_churn_window_cycles must be between 1 and 100",
            )
        _runtime_config["direction_churn_window_cycles"] = val
    if "direction_churn_non_merge_threshold" in payload:
        try:
            val = int(payload["direction_churn_non_merge_threshold"])
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=400,
                detail="direction_churn_non_merge_threshold must be an integer",
            )
        if not (1 <= val <= 10000):
            raise HTTPException(
                status_code=400,
                detail="direction_churn_non_merge_threshold must be between 1 and 10000",
            )
        _runtime_config["direction_churn_non_merge_threshold"] = val
    if "direction_churn_min_merge_rate" in payload:
        try:
            fval = float(payload["direction_churn_min_merge_rate"])
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=400,
                detail="direction_churn_min_merge_rate must be a number",
            )
        if not (0.0 <= fval <= 1.0):
            raise HTTPException(
                status_code=400,
                detail="direction_churn_min_merge_rate must be between 0.0 and 1.0",
            )
        _runtime_config["direction_churn_min_merge_rate"] = fval
    if "direction_lifetime_non_merge_threshold" in payload:
        try:
            val = int(payload["direction_lifetime_non_merge_threshold"])
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=400,
                detail="direction_lifetime_non_merge_threshold must be an integer",
            )
        if not (1 <= val <= 100000):
            raise HTTPException(
                status_code=400,
                detail="direction_lifetime_non_merge_threshold must be between 1 and 100000",
            )
        _runtime_config["direction_lifetime_non_merge_threshold"] = val
    if "direction_lifetime_min_merge_rate" in payload:
        try:
            fval = float(payload["direction_lifetime_min_merge_rate"])
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=400,
                detail="direction_lifetime_min_merge_rate must be a number",
            )
        if not (0.0 <= fval <= 1.0):
            raise HTTPException(
                status_code=400,
                detail="direction_lifetime_min_merge_rate must be between 0.0 and 1.0",
            )
        _runtime_config["direction_lifetime_min_merge_rate"] = fval
    if "direction_review_recommendation_threshold" in payload:
        try:
            val = int(payload["direction_review_recommendation_threshold"])
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=400,
                detail="direction_review_recommendation_threshold must be an integer",
            )
        if not (1 <= val <= 100):
            raise HTTPException(
                status_code=400,
                detail="direction_review_recommendation_threshold must be between 1 and 100",
            )
        _runtime_config["direction_review_recommendation_threshold"] = val
    # ── Runner health thresholds ─────────────────────────────────────────────
    for _rh_key, _rh_lo, _rh_hi in (
        # Liveness window: 30s minimum (fast heartbeat cycle) to 3600s (1h).
        ("runner_liveness_seconds", 30, 3_600),
        # Degraded-failure threshold: 1 (any failure = degraded) to 50 (tolerant).
        ("runner_degraded_failure_threshold", 1, 50),
        # Resource-pressure floors: 1 MiB to 1 TiB.
        ("runner_low_disk_threshold_bytes", 1_048_576, 1_099_511_627_776),
        ("runner_low_memory_threshold_bytes", 1_048_576, 1_099_511_627_776),
        ("vm_low_disk_threshold_bytes", 1_048_576, 1_099_511_627_776),
        ("vm_low_memory_threshold_bytes", 1_048_576, 1_099_511_627_776),
        # Escalation age: 60s to 7 days.
        ("runner_escalation_age_seconds", 60, 604_800),
        # Idle-GPU threshold: 0%–100%.
        ("runner_idle_gpu_threshold_pct", 0, 100),
        # Token staleness: 1–365 days.
        ("runner_token_stale_days", 1, 365),
        # Post-update silence window: 60s to 1h.
        ("runner_post_update_silence_seconds", 60, 3_600),
        # Heartbeat retention: 1–365 days.
        ("runner_heartbeat_retention_days", 1, 365),
    ):
        if _rh_key in payload:
            try:
                _rh_val = int(payload[_rh_key])
            except (ValueError, TypeError):
                raise HTTPException(
                    status_code=400,
                    detail=f"{_rh_key} must be a number",
                )
            if not (_rh_lo <= _rh_val <= _rh_hi):
                raise HTTPException(
                    status_code=400,
                    detail=f"{_rh_key} must be between {_rh_lo} and {_rh_hi}",
                )
            _runtime_config[_rh_key] = _rh_val
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
    if "inline_llm" in payload:
        val = payload["inline_llm"]
        try:
            cleaned_inline_llm = validate_inline_llm_runtime_config(val)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        _runtime_config["inline_llm"] = cleaned_inline_llm
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
    if "deploy_repo_identity" in payload:
        # Deploy-level identity: the "owner/repo" this deployment serves. Accept
        # a non-empty "owner/repo" string, or null/"" to clear it (no deploy
        # identity → only the merge-level provider is observable). Validated as a
        # well-formed slug pair so a malformed value can't silently mis-gate
        # which project's Cloud Run serving signals are observable.
        val = payload["deploy_repo_identity"]
        if val is None or (isinstance(val, str) and not val.strip()):
            _runtime_config["deploy_repo_identity"] = None
        elif isinstance(val, str) and "/" in val:
            owner, _, repo = val.strip().partition("/")
            if not owner.strip() or not repo.strip():
                raise HTTPException(
                    status_code=400,
                    detail="deploy_repo_identity must be 'owner/repo'",
                )
            _runtime_config["deploy_repo_identity"] = f"{owner.strip()}/{repo.strip()}"
        else:
            raise HTTPException(
                status_code=400,
                detail="deploy_repo_identity must be 'owner/repo' or null",
            )
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
    if "direction_refractory_minutes" in payload:
        if payload["direction_refractory_minutes"] is None:
            _runtime_config.pop("direction_refractory_minutes", None)
        else:
            try:
                val = int(payload["direction_refractory_minutes"])
            except (ValueError, TypeError):
                raise HTTPException(
                    status_code=400,
                    detail="direction_refractory_minutes must be a number",
                )
            if not (1 <= val <= 525600):
                raise HTTPException(
                    status_code=400,
                    detail="direction_refractory_minutes must be between 1 and 525600",
                )
            _runtime_config["direction_refractory_minutes"] = val
    if "direction_saturation_refractory_minutes" in payload:
        if payload["direction_saturation_refractory_minutes"] is None:
            _runtime_config.pop("direction_saturation_refractory_minutes", None)
        else:
            try:
                val = int(payload["direction_saturation_refractory_minutes"])
            except (ValueError, TypeError):
                raise HTTPException(
                    status_code=400,
                    detail="direction_saturation_refractory_minutes must be a number",
                )
            if not (1 <= val <= 525600):
                raise HTTPException(
                    status_code=400,
                    detail="direction_saturation_refractory_minutes must be between 1 and 525600",
                )
            _runtime_config["direction_saturation_refractory_minutes"] = val
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
    if "monthly_budget_target_usd" in payload:
        try:
            val = float(payload["monthly_budget_target_usd"])
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=400, detail="monthly_budget_target_usd must be a number"
            )
        if not (0 <= val <= 100000):
            raise HTTPException(
                status_code=400,
                detail="monthly_budget_target_usd must be between 0 and 100000 (0 = disabled)",
            )
        _runtime_config["monthly_budget_target_usd"] = val
    if "compute_experiment_spend_threshold_usd" in payload:
        try:
            val = float(payload["compute_experiment_spend_threshold_usd"])
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=400,
                detail="compute_experiment_spend_threshold_usd must be a number",
            )
        if not (0 <= val <= 100000):
            raise HTTPException(
                status_code=400,
                detail=(
                    "compute_experiment_spend_threshold_usd must be between 0 and "
                    "100000 (<=0 holds every positive-cap launch for approval)"
                ),
            )
        _runtime_config["compute_experiment_spend_threshold_usd"] = val
    if "auto_debug_enabled" in payload:
        # #8528 added this key to _KNOWN_SETTINGS_KEYS (the auto-debug spawn
        # kill-switch read in agent_status.should_auto_debug) but shipped no
        # apply block — a PATCH validated, returned 200, and silently dropped
        # the write. Apply block added as the fix-forward.
        val = payload["auto_debug_enabled"]
        if not isinstance(val, bool):
            raise HTTPException(
                status_code=400, detail="auto_debug_enabled must be a boolean"
            )
        _runtime_config["auto_debug_enabled"] = val
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
                "SPEND_HOLD_CREATED",
                "SPEND_HOLD_EXPIRING",
                "STRATEGIC_ENGINE_HALTED",
                "RUNNER_DEACTIVATED",
                "RUNNER_DEGRADED",
                "RUNNER_DEGRADED_ESCALATED",
                "RUNNER_FLEET_OFFLINE",
                "RUNNER_LOW_DISK",
                "RUNNER_OUTAGE_ESCALATED",
                "RUNNER_LOCAL_HOLD",
                "RUNNER_CONTAINER_HOLD",
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
            status_code = await _attempt_delivery(
                webhook_url,
                {"type": "TEST", "message": "Blueprint test notification"},
            )
            if status_code == 0:
                raise HTTPException(
                    status_code=502,
                    detail="Webhook delivery blocked or failed (SSRF check or unreachable)",
                )
            if status_code >= 400:
                raise HTTPException(
                    status_code=502, detail=f"Webhook returned {status_code}"
                )
        except httpx.RequestError as e:
            raise HTTPException(status_code=502, detail=f"Webhook request failed: {e}")
        return {"ok": True, "test_notification_sent": True}

    # Best-effort: resolve dormant-knob debts for any knobs that graduated this
    # request. Each call uses its own SAVEPOINT so a ledger failure never rolls
    # back the config change.
    if _debt_keys_to_resolve:
        from app.completion_debt import aresolve_completion_debt

        for _dk in _debt_keys_to_resolve:
            await aresolve_completion_debt(session, _dk, reason="graduated:enforce")

    # Best-effort: reconcile the full dormant-knob registry against the new
    # runtime config so demotions back to observe/off reopen immediately.
    from app.completion_debt import areconcile_dormant_knob_debts

    await areconcile_dormant_knob_debts(session, _runtime_config)

    # Detect a genuine warm_pool_enabled True→False transition. Computed here —
    # after all config changes but before the DB persist block — so the flag is
    # available in both the early-return (no DeploymentState row) and normal
    # (post-commit) code paths.
    _pool_disable_transition = bool(
        _config_snapshot.get("warm_pool_enabled", False)
    ) and not bool(_runtime_config.get("warm_pool_enabled", False))
    # Disabling managed execution is an even stronger boundary: no new pool
    # member can boot (guarded at the primitive), and existing idle members
    # should be drained promptly instead of billing until their ordinary TTL.
    _managed_compute_disable_transition = bool(
        _config_snapshot.get("managed_compute_enabled", True)
    ) and not bool(_runtime_config.get("managed_compute_enabled", True))

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
                    # Still drain the pool if the global disable transition fired.
                    if _pool_disable_transition or _managed_compute_disable_transition:
                        from app.warm_pool_maintainer import drain_global_warm_pool

                        try:
                            await drain_global_warm_pool()
                        except Exception:
                            logger.warning(
                                "[SETTINGS] global warm-pool drain failed after disable; "
                                "idle reaper will reclaim any remaining live members",
                                exc_info=True,
                            )
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
                runtime_config_json = {
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
                    # Persist the deploy-level identity so an operator override
                    # survives a Cloud Run restart (config-sync reads it back on
                    # boot; otherwise the in-memory value reverts to the env
                    # default).
                    "deploy_repo_identity": _runtime_config.get("deploy_repo_identity"),
                    "provider_settings_defaults": _runtime_config.get(
                        "provider_settings_defaults", {}
                    ),
                    "inline_llm": _runtime_config.get("inline_llm", {}),
                    "provider_settings_by_effort": _runtime_config.get(
                        "provider_settings_by_effort", {}
                    ),
                    "task_type_provider_overrides": _runtime_config.get(
                        "task_type_provider_overrides", {}
                    ),
                    "module_interfaces_holdback_fraction": _runtime_config.get(
                        "module_interfaces_holdback_fraction", 0.1
                    ),
                    "related_tests_holdback_fraction": _runtime_config.get(
                        "related_tests_holdback_fraction", 0.1
                    ),
                    "affected_consumers_holdback_fraction": _runtime_config.get(
                        "affected_consumers_holdback_fraction", 0.1
                    ),
                    "repo_map_holdback_fraction": _runtime_config.get(
                        "repo_map_holdback_fraction", 0.1
                    ),
                    "agent_prompt_suffixes": _runtime_config.get(
                        "agent_prompt_suffixes", {}
                    ),
                    "autonomous_periodic_tasks": _runtime_config.get(
                        "autonomous_periodic_tasks", {}
                    ),
                    "autonomous_spawn_budget_mode": str(
                        _runtime_config.get("autonomous_spawn_budget_mode", "observe")
                    ),
                    "max_global_autonomous_initiative_tasks_per_hour": int(
                        _runtime_config.get(
                            "max_global_autonomous_initiative_tasks_per_hour", 50
                        )
                    ),
                    "autonomous_spawn_budget_categories": _runtime_config.get(
                        "autonomous_spawn_budget_categories",
                        default_autonomous_spawn_budget_categories(),
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
                    "monthly_budget_target_usd": float(
                        _runtime_config.get("monthly_budget_target_usd", 0)
                    ),
                    "compute_experiment_spend_threshold_usd": _runtime_config.get(
                        "compute_experiment_spend_threshold_usd", 10.0
                    ),
                    "max_provision_retries": _runtime_config.get(
                        "max_provision_retries", 3
                    ),
                    "max_rollup_retries": _runtime_config.get("max_rollup_retries", 5),
                    "architecture_review_frequency": _runtime_config.get(
                        "architecture_review_frequency", 3
                    ),
                    "strategic_max_interval_minutes": _runtime_config.get(
                        "strategic_max_interval_minutes", 1_440
                    ),
                    "max_active_directions": _runtime_config.get(
                        "max_active_directions", 10
                    ),
                    "max_parallel_research_per_project": _runtime_config.get(
                        "max_parallel_research_per_project", 6
                    ),
                    "auto_debug_enabled": _runtime_config.get(
                        "auto_debug_enabled", True
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
                    # Fleet-wide hard boundary for billable execution launches.
                    # Stored in JSONB so every Cloud Run API instance converges
                    # without a redeploy; this does not govern the Cloud Run API
                    # service itself, only GCE/Cloud Run Job execution paths.
                    "managed_compute_enabled": bool(
                        _runtime_config.get("managed_compute_enabled", True)
                    ),
                    "evaluating_pipeline_timeout_minutes": _runtime_config.get(
                        "evaluating_pipeline_timeout_minutes", 120
                    ),
                    "ci_backstop_timeout_minutes": _runtime_config.get(
                        "ci_backstop_timeout_minutes", 60
                    ),
                    "spend_approval_timeout_hours": _runtime_config.get(
                        "spend_approval_timeout_hours", 24
                    ),
                    "max_total_research_per_project": _runtime_config.get(
                        "max_total_research_per_project", 5
                    ),
                    "productive_direction_cooldown_minutes": _runtime_config.get(
                        "productive_direction_cooldown_minutes", 45
                    ),
                    "direction_refractory_minutes": _runtime_config.get(
                        "direction_refractory_minutes",
                        _runtime_config.get(
                            "autonomous_direction_cooldown_minutes", 240
                        ),
                    ),
                    "direction_saturation_refractory_minutes": _runtime_config.get(
                        "direction_saturation_refractory_minutes",
                        _runtime_config.get(
                            "direction_refractory_minutes",
                            _runtime_config.get(
                                "autonomous_direction_cooldown_minutes", 240
                            ),
                        )
                        * 2,
                    ),
                    "warm_continuity_enabled": bool(
                        _runtime_config.get("warm_continuity_enabled", False)
                    ),
                    "warm_backlog_reuse_enabled": bool(
                        _runtime_config.get("warm_backlog_reuse_enabled", False)
                    ),
                    # Persist the rollup merge-train flag so an operator
                    # disable survives a Cloud Run restart (config-sync reads
                    # this key back on boot; without it the in-memory value
                    # reverts to the True default).
                    "rollup_merge_queue_enabled": bool(
                        _runtime_config.get("rollup_merge_queue_enabled", True)
                    ),
                    "cochange_decomposition_enabled": bool(
                        _runtime_config.get("cochange_decomposition_enabled", False)
                    ),
                    "research_quality_grader_enabled": bool(
                        _runtime_config.get("research_quality_grader_enabled", False)
                    ),
                    "park_and_retarget_enabled": bool(
                        _runtime_config.get("park_and_retarget_enabled", False)
                    ),
                    "park_and_retarget_budget_seconds": int(
                        _runtime_config.get("park_and_retarget_budget_seconds", 600)
                    ),
                    "warm_pool_enabled": bool(
                        _runtime_config.get("warm_pool_enabled", False)
                    ),
                    "warm_pool_account_retarget_enabled": bool(
                        _runtime_config.get("warm_pool_account_retarget_enabled", False)
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
                    "max_concurrent_tasks_by_project": {
                        str(k): int(v)
                        for k, v in (
                            _runtime_config.get("max_concurrent_tasks_by_project") or {}
                        ).items()
                        if v and int(v) > 0
                    },
                    "max_concurrent_compute_jobs_by_project": {
                        str(k): int(v)
                        for k, v in (
                            _runtime_config.get(
                                "max_concurrent_compute_jobs_by_project"
                            )
                            or {}
                        ).items()
                        if v and int(v) > 0
                    },
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
                    "notification_config": _encrypt_notification_config(
                        _runtime_config.get(
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
                                    "SPEND_HOLD_CREATED",
                                    "SPEND_HOLD_EXPIRING",
                                    "STRATEGIC_ENGINE_HALTED",
                                    "RUNNER_DEACTIVATED",
                                    "RUNNER_DEGRADED",
                                    "RUNNER_DEGRADED_ESCALATED",
                                    "RUNNER_FLEET_OFFLINE",
                                    "RUNNER_LOW_DISK",
                                    "RUNNER_OUTAGE_ESCALATED",
                                    "RUNNER_LOCAL_HOLD",
                                    "RUNNER_CONTAINER_HOLD",
                                ],
                                "throttle_seconds": 30,
                            },
                        )
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
                    # Persist the env-preflight dimension kill-switch so an operator
                    # toggle survives a Cloud Run restart (config-sync reads this
                    # key back on boot; without it the in-memory value reverts to
                    # the _RUNTIME_CONFIG_DEFAULTS default — True).
                    "env_preflight_enabled": bool(
                        _runtime_config.get("env_preflight_enabled", True)
                    ),
                    # Persist the no-CI verify_tests merge-gate kill-switch so an
                    # operator toggle survives a Cloud Run restart (config-sync
                    # reads this key back on boot; without it the in-memory value
                    # reverts to the NO_CI_TEST_GATE_ENABLED env default — True).
                    "no_ci_test_gate_enabled": bool(
                        _runtime_config.get("no_ci_test_gate_enabled", True)
                    ),
                    "promotion_threshold": _runtime_config.get("promotion_threshold"),
                    "promotion_weights": _runtime_config.get("promotion_weights"),
                    "diagnostics_after_edit_holdback_fraction": float(
                        _runtime_config.get(
                            "diagnostics_after_edit_holdback_fraction", 0.1
                        )
                    ),
                    "diagnostics_after_edit_enabled": bool(
                        _runtime_config.get("diagnostics_after_edit_enabled", True)
                    ),
                    "evergreen_fill_mode": str(
                        _runtime_config.get("evergreen_fill_mode", "off")
                    ),
                    "evergreen_fill_interval_minutes": int(
                        _runtime_config.get("evergreen_fill_interval_minutes", 15)
                    ),
                    "evergreen_fill_util_threshold": float(
                        _runtime_config.get("evergreen_fill_util_threshold", 0.5)
                    ),
                    "evergreen_fill_min_free_slots": int(
                        _runtime_config.get("evergreen_fill_min_free_slots", 2)
                    ),
                    "evergreen_fill_max_active": int(
                        _runtime_config.get("evergreen_fill_max_active", 1)
                    ),
                    "evergreen_fill_daily_cap": int(
                        _runtime_config.get("evergreen_fill_daily_cap", 3)
                    ),
                    "evergreen_fill_deferred_memo_enrollment": bool(
                        _runtime_config.get(
                            "evergreen_fill_deferred_memo_enrollment", False
                        )
                    ),
                    # Persist structural-health audit config so an operator
                    # change survives a Cloud Run restart. runtime_config_state.
                    # sync_* reads this key back on boot; without it the in-memory
                    # values revert to the env defaults.
                    "structural_health_audit_mode": str(
                        _runtime_config.get("structural_health_audit_mode", "observe")
                    ),
                    "structural_health_audit_interval_minutes": int(
                        _runtime_config.get("structural_health_audit_interval_minutes", 60)
                    ),
                    "structural_health_audit_fat_row_bytes": int(
                        _runtime_config.get("structural_health_audit_fat_row_bytes", 1048576)
                    ),
                    # Persist the completion-debt weight-mode graduation knob so an
                    # operator's enforce graduation survives a Cloud Run restart.
                    "completion_debt_weight_mode": str(
                        _runtime_config.get("completion_debt_weight_mode", "observe")
                    ),
                    # Persist the capability proof gate mode so an operator
                    # graduation (manual or auto) survives a Cloud Run restart.
                    "capability_proof_gate_mode": str(
                        _runtime_config.get("capability_proof_gate_mode", "off") or "off"
                    ),
                    # Persist capability proof gate auto-graduation config so an
                    # operator enable/tune survives a Cloud Run restart.
                    "capability_proof_gate_auto_graduation_enabled": bool(
                        _runtime_config.get(
                            "capability_proof_gate_auto_graduation_enabled", False
                        )
                    ),
                    "capability_proof_gate_auto_graduation_observe_threshold": float(
                        _runtime_config.get(
                            "capability_proof_gate_auto_graduation_observe_threshold", 0.5
                        )
                    ),
                    "capability_proof_gate_auto_graduation_enforce_threshold": float(
                        _runtime_config.get(
                            "capability_proof_gate_auto_graduation_enforce_threshold", 0.95
                        )
                    ),
                    # Persist the Slice-1 dark executed-named_test trials flag so an
                    # operator enable survives a Cloud Run restart.
                    "capability_proof_execution_trials_enabled": bool(
                        _runtime_config.get(
                            "capability_proof_execution_trials_enabled", False
                        )
                    ),
                    "adversarial_spawn_gate_infra_failure_mode": str(
                        _runtime_config.get(
                            "adversarial_spawn_gate_infra_failure_mode", "observe"
                        )
                    ),
                    # Persist the Cloud Run task-type allowlist so an operator
                    # configuration survives a Cloud Run restart. runtime_config_state.
                    # sync_* reads this key back on boot; without it the in-memory
                    # allowlist reverts to [] (no Cloud Run routing) and the configured
                    # types are lost.
                    "cloud_run_task_type_allowlist": sorted(
                        _runtime_config.get("cloud_run_task_type_allowlist") or []
                    ),
                    # Persist Workday cadence and safety tuning so it survives a
                    # Cloud Run restart and propagates to the leader across instances.
                    # Workday mode itself is the on-switch; these are pacing knobs only.
                    "strategic_continuation_ttl_seconds": int(
                        _runtime_config.get("strategic_continuation_ttl_seconds", 1500)
                    ),
                    "strategic_local_hold_timeout_seconds": int(
                        _runtime_config.get(
                            "strategic_local_hold_timeout_seconds", 900
                        )
                    ),
                    "strategic_session_idle_tick_seconds": int(
                        _runtime_config.get("strategic_session_idle_tick_seconds", 5)
                    ),
                    "strategic_session_turn_timeout_seconds": int(
                        _runtime_config.get(
                            "strategic_session_turn_timeout_seconds", 120
                        )
                    ),
                    "strategic_session_exploration_interval_seconds": int(
                        _runtime_config.get(
                            "strategic_session_exploration_interval_seconds", 86_400
                        )
                    ),
                    "strategic_session_max_ops_per_turn": int(
                        _runtime_config.get("strategic_session_max_ops_per_turn", 3)
                    ),
                    "strategic_session_autonomous_turn_timeout_seconds": int(
                        _runtime_config.get(
                            "strategic_session_autonomous_turn_timeout_seconds", 300
                        )
                    ),
                    "strategic_session_max_unattended_seconds": int(
                        _runtime_config.get(
                            "strategic_session_max_unattended_seconds", 7200
                        )
                    ),
                    "direction_gap_respawn_block_mode": str(
                        _runtime_config.get(
                            "direction_gap_respawn_block_mode", "observe"
                        )
                        or "observe"
                    ),
                    "direction_gap_respawn_cooldown_days": float(
                        _runtime_config.get("direction_gap_respawn_cooldown_days", 14)
                        or 14
                    ),
                    "direction_churn_window_cycles": int(
                        _runtime_config.get("direction_churn_window_cycles", 5)
                    ),
                    "direction_churn_non_merge_threshold": int(
                        _runtime_config.get("direction_churn_non_merge_threshold", 15)
                    ),
                    "direction_churn_min_merge_rate": float(
                        _runtime_config.get("direction_churn_min_merge_rate", 0.10)
                    ),
                    "direction_lifetime_non_merge_threshold": int(
                        _runtime_config.get(
                            "direction_lifetime_non_merge_threshold", 150
                        )
                    ),
                    "direction_lifetime_min_merge_rate": float(
                        _runtime_config.get("direction_lifetime_min_merge_rate", 0.10)
                    ),
                    "direction_review_recommendation_threshold": int(
                        _runtime_config.get(
                            "direction_review_recommendation_threshold", 2
                        )
                    ),
                    # Runner health thresholds — persist so operator overrides
                    # survive Cloud Run restarts.
                    "runner_liveness_seconds": int(
                        _runtime_config.get("runner_liveness_seconds", 300)
                    ),
                    "runner_degraded_failure_threshold": int(
                        _runtime_config.get("runner_degraded_failure_threshold", 3)
                    ),
                    "runner_low_disk_threshold_bytes": int(
                        _runtime_config.get(
                            "runner_low_disk_threshold_bytes", 1_073_741_824
                        )
                    ),
                    "runner_low_memory_threshold_bytes": int(
                        _runtime_config.get(
                            "runner_low_memory_threshold_bytes", 536_870_912
                        )
                    ),
                    "runner_escalation_age_seconds": int(
                        _runtime_config.get("runner_escalation_age_seconds", 14_400)
                    ),
                    "runner_idle_gpu_threshold_pct": int(
                        _runtime_config.get("runner_idle_gpu_threshold_pct", 10)
                    ),
                    "runner_token_stale_days": int(
                        _runtime_config.get("runner_token_stale_days", 90)
                    ),
                    "runner_post_update_silence_seconds": int(
                        _runtime_config.get(
                            "runner_post_update_silence_seconds", 600
                        )
                    ),
                    "runner_heartbeat_retention_days": int(
                        _runtime_config.get(
                            "runner_heartbeat_retention_days", 7
                        )
                    ),
                    "vm_low_disk_threshold_bytes": int(
                        _runtime_config.get(
                            "vm_low_disk_threshold_bytes", 1_073_741_824
                        )
                    ),
                    "vm_low_memory_threshold_bytes": int(
                        _runtime_config.get(
                            "vm_low_memory_threshold_bytes", 536_870_912
                        )
                    ),
                    # Preserve the operator-approved migration-divergence exemption
                    # store when persisting settings. This dict is rebuilt wholesale
                    # and overwrites state.runtime_config_json, so without echoing the
                    # key here an unrelated settings PATCH would silently drop
                    # exemptions written via the dedicated admin router on the next
                    # restart. Managed by admin_migration_exemptions; only echoed here.
                    "migration_divergence_exemptions": _runtime_config.get(
                        "migration_divergence_exemptions", []
                    ),
                    # Preserve the usage-headroom selection gate and its auto-arm
                    # controls across this wholesale rebuild. sync_runtime_config_from_state
                    # reads all three keys back on boot / every sync cycle; without
                    # echoing them here an unrelated settings PATCH silently drops
                    # them, so on the next restart the module defaults
                    # (gate off, mode "auto") take over — the reconciler then
                    # re-arms the gate against an operator who set mode="off"
                    # (documented force-disarm kill switch) or "manual". The gate
                    # value itself is normally reconciler-managed (via a targeted
                    # merge-persist), but echoing it here keeps the armed state
                    # from being reverted by an operator settings PATCH.
                    "usage_headroom_gate_enabled": bool(
                        _runtime_config.get("usage_headroom_gate_enabled", False)
                    ),
                    "usage_headroom_auto_arm_mode": str(
                        _runtime_config.get("usage_headroom_auto_arm_mode", "auto")
                    ),
                    "usage_headroom_auto_arm_observation_minutes": int(
                        _runtime_config.get(
                            "usage_headroom_auto_arm_observation_minutes", 60
                        )
                    ),
                }
                if "direction_refractory_minutes" not in _runtime_config:
                    runtime_config_json.pop("direction_refractory_minutes", None)
                if "direction_saturation_refractory_minutes" not in _runtime_config:
                    runtime_config_json.pop(
                        "direction_saturation_refractory_minutes", None
                    )
                state.runtime_config_json = runtime_config_json
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

    # If warm_pool_enabled transitioned True→False and the DB commit succeeded,
    # drain all live pool members fleet-wide. Errors are best-effort — a
    # partial drain is recoverable by the idle reaper.
    if _pool_disable_transition or _managed_compute_disable_transition:
        from app.warm_pool_maintainer import drain_global_warm_pool

        try:
            await drain_global_warm_pool()
        except Exception:
            logger.warning(
                "[SETTINGS] global warm-pool drain failed after disable; "
                "idle reaper will reclaim any remaining live members",
                exc_info=True,
            )

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
@limiter.limit("10/minute")
async def list_github_repos(
    request: Request,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """List onboarding repos the caller can write and the App can reach.

    Platform admins retain the legacy stored-installation behavior. Ordinary
    users use the linked OAuth/PAT identity intersected with every App
    installation visible to that identity; an App token alone never proves the
    caller may onboard a repository.
    """
    if current_user.get("role") != "admin":
        from app.github_onboarding import list_onboarding_repositories

        repos, installation_count = await list_onboarding_repositories(
            session, current_user["user_id"]
        )
        return {
            "repositories": repos,
            "installation_count": installation_count,
        }

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


_MANUAL_GITHUB_REPO_RE = re.compile(
    r"^(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,38}))/(?P<repo>[A-Za-z0-9_.-]+)$"
)


def _parse_manual_github_repo(value: str) -> tuple[str, str]:
    match = _MANUAL_GITHUB_REPO_RE.fullmatch(value.strip())
    if match is None:
        raise HTTPException(
            status_code=400,
            detail="Repository must use the exact 'owner/repo' format.",
        )
    return match.group("owner"), match.group("repo")


@router.get("/github/branches")
@limiter.limit("20/minute")
async def list_github_branches(
    request: Request,
    repo: str = Query(...),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """List branches after caller authorization and App reachability checks.

    The exact-repository path intentionally remains available for safe manual
    repo input even when a large installation's discovery result is paginated.
    """
    owner, repo_name = _parse_manual_github_repo(repo)
    repo = f"{owner}/{repo_name}"
    if current_user.get("role") == "admin":
        token = await _get_installation_token_for_user(
            current_user["user_id"], session
        )
    else:
        from app.github import get_repo_installation_token
        from app.github_onboarding import require_caller_repo_push_access

        await require_caller_repo_push_access(
            session, current_user, owner, repo_name
        )
        token = await get_repo_installation_token(owner, repo_name)
        if not token:
            raise HTTPException(
                status_code=422,
                detail=(
                    "Your GitHub authorization is valid, but the Blueprint GitHub App "
                    "is not installed for this repository."
                ),
            )

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
    _ss.github_off_backend_estimate["cumulative_this_window"] = 0
    _ss.github_off_backend_estimate["_last_remaining"] = None
    _ss.github_off_backend_estimate["_last_backend_calls"] = 0
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
            t.latest_status_message = f"Repaired: stuck in {original_status} with no heartbeat since {cutoff.isoformat()}"  # utc-iso-ok: prose status message, not a wire timestamp field
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

    parent_ids = [p.id for p in evaluating_tasks]
    children_by_parent: dict = {}
    if parent_ids:
        all_children = (
            (
                await session.execute(
                    select(Task).where(Task.parent_task_id.in_(parent_ids)).limit(5000)
                )
            )
            .scalars()
            .all()
        )
        for child in all_children:
            children_by_parent.setdefault(child.parent_task_id, []).append(child)

    for parent in evaluating_tasks:
        children = children_by_parent.get(parent.id, [])

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

    # Batch-load all test tasks for HALF_OPEN breakers in one query
    half_open_items = [
        (proj_id, breaker)
        for proj_id, breaker in list(_circuit_breakers.items())
        if breaker.get("state") == "HALF_OPEN" and breaker.get("half_open_task_id")
    ]
    if half_open_items:
        test_ids = []
        for _, breaker in half_open_items:
            try:
                test_ids.append(uuid.UUID(breaker["half_open_task_id"]))
            except Exception:
                pass
        found_ids: set = set()
        if test_ids:
            try:
                rows = (
                    (await session.execute(select(Task).where(Task.id.in_(test_ids))))
                    .scalars()
                    .all()
                )
                found_ids = {r.id for r in rows}
            except Exception:
                pass

    for proj_id, breaker in half_open_items:
        test_id = breaker.get("half_open_task_id")
        try:
            task_uuid = uuid.UUID(test_id)
        except Exception:
            continue
        test_task_exists = task_uuid in found_ids
        if not test_task_exists:
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
