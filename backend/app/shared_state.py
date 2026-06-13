"""Shared runtime state used by both main.py and admin.py.

Historically this was a single ~2700-line process-global module mixing many
unrelated concerns (runtime_config, the rate-limit pool, branch resolution,
anomaly / circuit-breaker state, warm-VM continuity, pipeline-health
monitoring, direction-failure streaks, …). It has been split into cohesive
domain modules and now serves as a **thin facade** that re-exports the SAME
public names so the many importers (``from app.shared_state import X``) and
attribute users (``app.shared_state.X``) keep working unchanged:

  * :mod:`app.rate_limiter`          — the slowapi ``limiter``.
  * :mod:`app.runtime_config_state`  — ``runtime_config`` + lock, promotion
                                       policy seed, config sync, retry budgets.
  * :mod:`app.branch_resolution`     — task-branch naming + per-project branch
                                       resolvers + the startup backfills.
  * :mod:`app.anomaly_state`         — anomaly fingerprints, circuit breakers,
                                       autonomous failure counts.
  * :mod:`app.direction_streaks`     — per-direction failure-streak / hold state.
  * :mod:`app.warm_vm_state`         — warm-VM heartbeat, warm-pin / backlog
                                       reuse breakers, SR-catch-rate debounce,
                                       user-debug + merge-error rate windows.
  * :mod:`app.pipeline_health_state` — deploy monitoring, pipeline-health
                                       events/alerts, dependency + GitHub-quota
                                       health, dispatcher-cycle log, reaper
                                       halt accessors, the stale-state sweep.

This file additionally OWNS the handful of *rebindable scalar* module
attributes — and the two warm-continuity circuit-breaker checks that mutate
deadline scalars via ``global`` — because producers and consumers (and tests)
read/write them through this facade (``shared_state.is_leader = ...``,
``mocker.patch.object(shared_state, "tasks_queued_gauge", …)``). Re-exporting
an immutable scalar would silently diverge the moment a writer rebinds the
facade attribute, so those stay defined here.

Behaviour is byte-identical to the pre-split module.
"""

import logging
from datetime import datetime, timedelta

from app.time_utils import _utcnow

logger = logging.getLogger(__name__)

# --- Rate limiter (app.rate_limiter) -------------------------------------
from app.rate_limiter import limiter  # noqa: F401

# --- Runtime config (app.runtime_config_state) ---------------------------
from app.runtime_config_state import (  # noqa: F401
    PROMOTION_POLICY_KEYS,
    SCOPE_AWARE_RETRY_BUDGETS,
    _PROMOTION_POLICY_PATH,
    _PROMOTION_WEIGHT_KEYS,
    _migrate_legacy_provider_config,
    _read_promotion_policy_file,
    _seed_promotion_policy,
    promotion_enabled_by_project_map,
    resolve_project_promotion_enabled,
    resolve_retry_budget,
    runtime_config,
    runtime_config_lock,
    runtime_config_sync_service,
    sync_runtime_config_from_state,
)

# --- Branch resolution (app.branch_resolution) ---------------------------
from app.branch_resolution import (  # noqa: F401
    DEFAULT_TASK_SCOPE,
    TASK_BRANCH_NAMESPACE_DEFAULT,
    TASK_TYPE_BRANCH_PREFIX,
    _BLUEPRINT_REPO_ALIASES,
    _SCOPE_EFFORT_MAP,
    _is_blueprint_self_repo,
    _missing_project_branch_alerts,
    _signal_missing_project_branch,
    backfill_branch_columns,
    backfill_task_branch_namespace,
    derive_task_branch,
    ensure_project_pr_title_prefix,
    flush_pending_branch_alerts,
    get_project_broadcast_limit,
    get_project_integration_branch,
    get_project_promotion_target,
    get_project_pr_title_prefix,
    get_project_task_branch_namespace,
    resolve_task_branch,
    scope_to_effort,
)

# --- Anomaly / circuit-breaker / autonomous-loop state (app.anomaly_state)
from app.anomaly_state import (  # noqa: F401
    _ANOMALY_FINGERPRINTS_KEY,
    anomaly_adjustments,
    autonomous_failure_counts,
    circuit_breakers,
    get_circuit_breaker,
    hydrate_persistent_anomalies,
    persist_anomaly_snapshot,
    persistent_anomalies,
    reset_project_state,
    with_design_doc_locked,
)

# --- Direction failure streaks (app.direction_streaks) -------------------
from app.direction_streaks import (  # noqa: F401
    DIRECTION_HOLD_THRESHOLD,
    DIRECTION_HOLD_TIMEOUT_HOURS,
    _DIRECTION_INFRA_CLASSES,
    clear_direction_hold,
    direction_failure_streaks,
    get_direction_hold_info,
    get_held_directions,
    is_direction_held,
    record_direction_result,
)

# --- Warm-VM / rate-limit window state (app.warm_vm_state) ---------------
from app.warm_vm_state import (  # noqa: F401
    MERGE_PATTERN_MIN_PRS,
    MERGE_PATTERN_WINDOW_SECONDS,
    PR_IS_NOT_BEHIND_CALL_SITES,
    SR_CATCH_RATE_DEBOUNCE_SECONDS,
    USER_DEBUG_MAX_PER_HOUR,
    WARM_BACKLOG_FIRST_REFUSAL_SECS,
    _empty_pr_is_not_behind_bucket,
    count_merge_error_pattern_prs,
    count_recent_backlog_reuse_failures,
    count_recent_pool_claim_failures,
    count_recent_user_debug_spawns,
    count_recent_warm_pin_clears,
    count_recent_warm_pin_clears_by_reason,
    is_backlog_first_refusal_active,
    is_vm_alive,
    mark_sr_catch_rate_refreshed,
    merge_error_occurrences,
    pr_is_not_behind_counts,
    record_backlog_reuse_failure,
    record_merge_error_occurrence,
    record_pool_claim_failure,
    record_pr_is_not_behind,
    record_user_debug_spawn,
    record_vm_heartbeat,
    record_warm_pin_clear,
    register_backlog_first_refusal,
    should_refresh_sr_catch_rate,
    snapshot_pr_is_not_behind_counts,
    sr_catch_rate_last_refreshed,
    user_debug_spawn_times,
    vm_last_heartbeat,
    warm_backlog_first_refusal,
    warm_backlog_reuse_failure_events,
    warm_pin_clear_events,
    warm_pool_claim_failure_events,
)

# --- Pipeline / deploy / dependency health (app.pipeline_health_state) ---
from app.pipeline_health_state import (  # noqa: F401
    PIPELINE_ALERT_DEDUP_SECONDS,
    PIPELINE_ALERT_THRESHOLDS,
    PIPELINE_EVENT_TYPES,
    _BREAKER_RETENTION_HOURS,
    _DEBUG_SPAWN_RETENTION_SECONDS,
    _DISPATCHER_CYCLE_LOG_MAXLEN,
    _DISPATCHER_CYCLE_TTL_DAYS,
    _MERGE_ERROR_RETENTION_SECONDS,
    _REAP_REASON_EVENT_TYPES,
    _STREAK_RETENTION_HOURS,
    _maybe_fire_pipeline_alert,
    _post_pipeline_alert,
    background_tasks,
    count_pipeline_events,
    deploy_monitoring,
    dispatcher_cycle_log,
    external_health,
    get_dispatcher_cycles,
    get_pipeline_health_snapshot,
    get_reaper_halt_state,
    get_task_type_failure_rate,
    github_cache_hits,
    github_call_counts,
    github_jwt_quota,
    github_quota,
    record_dispatcher_cycle,
    record_pipeline_event,
    reset_reaper_halt_state,
    sweep_stale_in_memory_state,
)


# =========================================================================
# Rebindable scalar module attributes — OWNED HERE (see module docstring).
# These are written by producers as ``shared_state.<attr> = value`` and read
# by consumers as ``shared_state.<attr>``; tests rebind them via
# ``mocker.patch.object(shared_state, "<attr>", …)``. They must live on this
# facade so a rebind on one side is observed by the other.
# =========================================================================

# --- Autonomous loop / anomaly timing ---
last_idle_check: datetime | None = None
# Throttle anchor for the orchestrator's _check_evergreen_fill() sibling
# (utilization-triggered evergreen backlog auto-fill, direction d-2877ddc7).
last_evergreen_check: datetime | None = None
# Last time _compute_research_anomalies completed successfully for ANY project.
# Used by /api/admin/anomaly-tracker/snapshot so operators can detect a silent
# writer failure (corrupted shared_state, swallowed exception). Ephemeral —
# not persisted across pod restarts.
last_anomaly_detection_run_at: datetime | None = None

# --- Leader Election State ---
# True on the instance that won the pg_try_advisory_lock; False on followers.
# Defaults to True so non-Postgres environments (tests, local dev without PG) behave normally.
is_leader: bool = True

# --- Prometheus-compatible metrics counters ---
# Gauges: updated by orchestrator each cycle
tasks_queued_gauge: int = 0
tasks_active_gauge: int = 0
# Counters: cumulative totals, incremented on terminal state transitions
tasks_completed_total: int = 0
tasks_failed_total: int = 0

# GitHub REST-call-counter window rollover marker (rebound when a new quota
# window opens). The counts dict itself lives in pipeline_health_state.
github_call_counts_window_reset_at: int | None = None

# Track last successful GitHub API operation (tokens, PR comments, etc.)
# Used by health check to passively verify GitHub is reachable without
# making an active API call that competes for the shared connection pool.
# Seed with current time so the first health check uses the passive path
# instead of an active probe that can fail on cold start.
last_github_success: datetime | None = (
    datetime.utcnow()
)  # Module-level init, _utcnow not available here at import order

# --- Warm-continuity circuit-breaker deadlines ---
# Auto-suppression deadline set by is_warm_continuity_suppressed() when the
# pin-clear rate exceeds warm_pin_clear_rate_threshold. None means not suppressed.
warm_continuity_suppressed_until: datetime | None = None
# Auto-suppression deadline set by is_warm_backlog_reuse_suppressed() when the
# failure rate exceeds warm_backlog_reuse_failure_rate_threshold. None = not suppressed.
warm_backlog_reuse_suppressed_until: datetime | None = None
# Auto-suppression deadline set by is_warm_pool_suppressed() when the pool-claim
# failure rate exceeds warm_pool_failure_rate_threshold. None = not suppressed.
warm_pool_suppressed_until: datetime | None = None


def is_warm_continuity_suppressed() -> bool:
    """True iff warm continuity is auto-suppressed by the pin-clear circuit breaker.

    Checks the recent pin-clear rate against the configured threshold. When the
    rate exceeds the threshold, sets a suppression window so callers fall back to
    cold provisioning until the cooldown expires and the rate normalises.
    """
    global warm_continuity_suppressed_until
    now = _utcnow()
    if (
        warm_continuity_suppressed_until is not None
        and now < warm_continuity_suppressed_until
    ):
        return True
    threshold = runtime_config.get("warm_pin_clear_rate_threshold", 10)
    recent_clears = count_recent_warm_pin_clears(3600)
    if recent_clears >= threshold:
        cooldown = runtime_config.get("warm_continuity_cooldown_seconds", 1800)
        warm_continuity_suppressed_until = now + timedelta(seconds=cooldown)
        logger.warning(
            f"[WARM_CONTINUITY] Circuit breaker tripped: {recent_clears} pin-clear events "
            f"in last hour (threshold {threshold}). Suppressed until "
            f"{warm_continuity_suppressed_until.isoformat()}."
        )
        return True
    warm_continuity_suppressed_until = None
    return False


def is_warm_backlog_reuse_suppressed() -> bool:
    """True iff warm-backlog reuse is auto-suppressed by the failure circuit breaker.

    Checks the recent failure rate against the configured threshold. When the
    rate exceeds the threshold, sets a suppression window so callers fall back
    to cold provisioning until the cooldown expires and the rate normalises.
    """
    global warm_backlog_reuse_suppressed_until
    now = _utcnow()
    if (
        warm_backlog_reuse_suppressed_until is not None
        and now < warm_backlog_reuse_suppressed_until
    ):
        return True
    threshold = runtime_config.get("warm_backlog_reuse_failure_rate_threshold", 5)
    recent_failures = count_recent_backlog_reuse_failures(3600)
    if recent_failures >= threshold:
        cooldown = runtime_config.get("warm_backlog_reuse_cooldown_seconds", 1800)
        warm_backlog_reuse_suppressed_until = now + timedelta(seconds=cooldown)
        logger.warning(
            f"[BACKLOG_REUSE] Circuit breaker tripped: {recent_failures} failure events "
            f"in last hour (threshold {threshold}). Suppressed until "
            f"{warm_backlog_reuse_suppressed_until.isoformat()}."
        )
        return True
    warm_backlog_reuse_suppressed_until = None
    return False


def is_warm_pool_suppressed() -> bool:
    """True iff warm-pool claims are auto-suppressed by the failure circuit breaker.

    Checks the recent pool-claim failure rate against the configured threshold.
    When the rate exceeds the threshold, sets a suppression window so the
    maintainer stops replenishing and the orchestrator dispatch hook stops
    leasing (falling back to cold provisioning) until the cooldown expires and
    the rate normalises. Mirrors is_warm_backlog_reuse_suppressed.
    """
    global warm_pool_suppressed_until
    now = _utcnow()
    if warm_pool_suppressed_until is not None and now < warm_pool_suppressed_until:
        return True
    threshold = runtime_config.get("warm_pool_failure_rate_threshold", 5)
    recent_failures = count_recent_pool_claim_failures(3600)
    if recent_failures >= threshold:
        cooldown = runtime_config.get("warm_pool_cooldown_seconds", 1800)
        warm_pool_suppressed_until = now + timedelta(seconds=cooldown)
        logger.warning(
            f"[WARM_POOL] Circuit breaker tripped: {recent_failures} pool-claim "
            f"failure events in last hour (threshold {threshold}). Suppressed until "
            f"{warm_pool_suppressed_until.isoformat()}."
        )
        return True
    warm_pool_suppressed_until = None
    return False
