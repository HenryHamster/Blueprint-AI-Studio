"""Regression guard for the app.shared_state facade split.

shared_state.py was split into cohesive domain modules (runtime_config_state,
branch_resolution, rate_limiter, anomaly_state, direction_streaks,
warm_vm_state, pipeline_health_state) with shared_state kept as a thin facade
that re-exports the SAME public names. These tests lock that contract:

  * every name the facade is expected to expose is still importable from it;
  * mutable container state is shared by *identity* between the facade and the
    owning domain module (so in-place mutation from either side is visible to
    the other — the property the many ``from app.shared_state import <dict>``
    importers rely on);
  * the rebindable scalars the facade OWNS are defined on the facade itself.
"""

import app.shared_state as ss


# The full public surface importers depend on. Locking it here means a future
# refactor that drops or fails to re-export a name fails loudly in unit tests
# rather than silently at an import site somewhere in the app.
EXPECTED_FACADE_NAMES = [
    # rate limiter
    "limiter",
    # runtime config
    "runtime_config",
    "runtime_config_lock",
    "runtime_config_sync_service",
    "sync_runtime_config_from_state",
    "resolve_retry_budget",
    "SCOPE_AWARE_RETRY_BUDGETS",
    "PROMOTION_POLICY_KEYS",
    "_migrate_legacy_provider_config",
    "_seed_promotion_policy",
    "_read_promotion_policy_file",
    # branch resolution
    "DEFAULT_TASK_SCOPE",
    "TASK_TYPE_BRANCH_PREFIX",
    "TASK_BRANCH_NAMESPACE_DEFAULT",
    "scope_to_effort",
    "derive_task_branch",
    "resolve_task_branch",
    "get_project_integration_branch",
    "get_project_promotion_target",
    "get_project_task_branch_namespace",
    "get_project_pr_title_prefix",
    "ensure_project_pr_title_prefix",
    "get_project_broadcast_limit",
    "backfill_branch_columns",
    "backfill_task_branch_namespace",
    "_missing_project_branch_alerts",
    # anomaly / circuit-breaker
    "persistent_anomalies",
    "anomaly_adjustments",
    "circuit_breakers",
    "autonomous_failure_counts",
    "get_circuit_breaker",
    "hydrate_persistent_anomalies",
    "persist_anomaly_snapshot",
    "with_design_doc_locked",
    "_ANOMALY_FINGERPRINTS_KEY",
    # direction streaks
    "direction_failure_streaks",
    "DIRECTION_HOLD_THRESHOLD",
    "DIRECTION_HOLD_TIMEOUT_HOURS",
    "record_direction_result",
    "clear_direction_hold",
    "get_direction_hold_info",
    "is_direction_held",
    "get_held_directions",
    # warm-VM / rate windows
    "vm_last_heartbeat",
    "record_vm_heartbeat",
    "is_vm_alive",
    "merge_error_occurrences",
    "record_merge_error_occurrence",
    "count_merge_error_pattern_prs",
    "user_debug_spawn_times",
    "record_user_debug_spawn",
    "count_recent_user_debug_spawns",
    "warm_pin_clear_events",
    "record_warm_pin_clear",
    "count_recent_warm_pin_clears",
    "warm_backlog_reuse_failure_events",
    "record_backlog_reuse_failure",
    "count_recent_backlog_reuse_failures",
    "warm_backlog_first_refusal",
    "register_backlog_first_refusal",
    "is_backlog_first_refusal_active",
    "sr_catch_rate_last_refreshed",
    "should_refresh_sr_catch_rate",
    "mark_sr_catch_rate_refreshed",
    "pr_is_not_behind_counts",
    "record_pr_is_not_behind",
    "snapshot_pr_is_not_behind_counts",
    "is_warm_continuity_suppressed",
    "is_warm_backlog_reuse_suppressed",
    "warm_continuity_suppressed_until",
    "warm_backlog_reuse_suppressed_until",
    # pipeline / deploy / dependency health
    "deploy_monitoring",
    "external_health",
    "github_quota",
    "github_jwt_quota",
    "github_call_counts",
    "github_cache_hits",
    "background_tasks",
    "record_pipeline_event",
    "count_pipeline_events",
    "get_pipeline_health_snapshot",
    "_post_pipeline_alert",
    "get_task_type_failure_rate",
    "dispatcher_cycle_log",
    "record_dispatcher_cycle",
    "get_dispatcher_cycles",
    "get_reaper_halt_state",
    "reset_reaper_halt_state",
    "sweep_stale_in_memory_state",
    # rebindable scalars owned by the facade
    "is_leader",
    "last_idle_check",
    "last_anomaly_detection_run_at",
    "last_github_success",
    "github_call_counts_window_reset_at",
    "tasks_queued_gauge",
    "tasks_active_gauge",
    "tasks_completed_total",
    "tasks_failed_total",
]


def test_facade_exposes_every_expected_name():
    missing = [name for name in EXPECTED_FACADE_NAMES if not hasattr(ss, name)]
    assert not missing, f"facade is missing re-exports: {missing}"


def test_mutable_containers_shared_by_identity_with_owner_modules():
    """In-place mutation from a domain module must be visible via the facade
    (and vice-versa) — i.e. the facade re-export is the SAME object, not a copy."""
    import app.runtime_config_state as rc
    import app.branch_resolution as br
    import app.anomaly_state as an
    import app.direction_streaks as dst
    import app.warm_vm_state as wv
    import app.pipeline_health_state as ph

    assert ss.runtime_config is rc.runtime_config
    assert ss.runtime_config_lock is rc.runtime_config_lock
    assert ss._missing_project_branch_alerts is br._missing_project_branch_alerts
    assert ss.persistent_anomalies is an.persistent_anomalies
    assert ss.circuit_breakers is an.circuit_breakers
    assert ss.direction_failure_streaks is dst.direction_failure_streaks
    assert ss.vm_last_heartbeat is wv.vm_last_heartbeat
    assert ss.merge_error_occurrences is wv.merge_error_occurrences
    assert ss.deploy_monitoring is ph.deploy_monitoring
    assert ss.external_health is ph.external_health
    assert ss.background_tasks is ph.background_tasks


def test_in_place_mutation_visible_across_facade_boundary():
    import app.warm_vm_state as wv

    sentinel_key = "__facade_identity_probe__"
    assert sentinel_key not in ss.vm_last_heartbeat
    try:
        ss.record_vm_heartbeat(sentinel_key)
        # Mutation done through the owner module's function must be visible
        # through the facade re-export (same dict object).
        assert sentinel_key in wv.vm_last_heartbeat
        assert sentinel_key in ss.vm_last_heartbeat
    finally:
        ss.vm_last_heartbeat.pop(sentinel_key, None)


def test_rebindable_scalars_are_facade_owned():
    """The scalars producers rebind through the facade must be defined on the
    facade module's own __dict__, not merely imported from a domain module."""
    for name in (
        "is_leader",
        "last_idle_check",
        "last_anomaly_detection_run_at",
        "last_github_success",
        "github_call_counts_window_reset_at",
        "tasks_queued_gauge",
        "tasks_active_gauge",
        "tasks_completed_total",
        "tasks_failed_total",
        "warm_continuity_suppressed_until",
        "warm_backlog_reuse_suppressed_until",
    ):
        assert name in ss.__dict__, f"{name} should be defined on the facade itself"
