// Shape returned by GET /api/admin/warm-vm-state. Mirrors the Python
// payload in backend/app/routers/admin_health.py:get_warm_vm_state.
export interface WarmVmCurrentTask {
  id: string;
  title: string;
  task_type: string;
  status: string;
  warm_start: boolean;
  // "warm_pool" | "backlog_reuse" | "next_role_endpoint" | null (legacy/unset).
  // Set by the server when a warm VM claims a task; distinguishes pre-provisioned
  // pool-origin claims from same-project backlog reuse and PR-chain continuity.
  // See admin_health._compute_warm_vm_state.
  warm_claim_origin: string | null;
  prior_role_vm_id: string | null;
  setup_secs: number | null;
  started_at: string | null;
  pr_number: number | null;
  project_id: string | null;
}

export interface WarmVmPinnedQueued {
  id: string;
  task_type: string;
  warm_target_pr: number | null;
  warm_handoff_deadline: string | null;
}

export interface WarmVmRecentCompletion {
  id: string;
  task_type: string;
  completed_at: string | null;
  status: string;
}

export interface WarmVmRow {
  vm_instance_id: string;
  last_heartbeat: string | null;
  is_alive: boolean;
  current_task: WarmVmCurrentTask | null;
  warm_pinned_queued: WarmVmPinnedQueued[];
  recent_completions: WarmVmRecentCompletion[];
}

export interface ChainContinuityFunnelSnapshot {
  window_hours: number;
  poll_arrivals: number;
  chain_claimed: number;
  no_pinned_task: number;
  another_vm_owns_pr: number;
  doomed_vm: number;
  skipped_by_reason: { disabled?: number; breaker_tripped?: number };
  skipped_total: number;
}

export interface BacklogReuseFunnelSnapshot {
  window_hours: number;
  completed_vms_with_eligible_backlog: number;
  teardown_defers_scheduled: number;
  next_role_polls_during_defer: number;
  warm_claims_succeeded: number;
  claim_skipped_by_reason: Record<string, number>;
  claim_skipped_total: number;
  // Eligibility-scan skip-reason rolling counters: per-poll aggregation
  // (one poll that skipped N rows for the same reason counts as 1).
  scan_polls_with_depends_on_skip: number;
  scan_polls_with_pinned_vm_skip: number;
  scan_polls_with_file_conflict_skip: number;
  scan_polls_saturated: number;
}

export interface SpawnPinFunnelSnapshot {
  window_hours: number;
  spawn_attempts: number;
  pin_set: number;
  skipped_by_reason: {
    disabled?: number;
    breaker_tripped?: number;
    source_cold?: number;
    stale_source?: number;
    caller_suppressed?: number;
  };
  skipped_total: number;
}

// Per-account slice of the pool dispatch funnel, as built by
// warm_backlog_reuse.get_pool_funnel_snapshot -> by_account. Events recorded
// without a resolved account fall under the _UNATTRIBUTED_ACCOUNT label.
export interface PoolFunnelAccountBreakdown {
  pool_lease_hits: number;
  cold_dispatches: number;
  total_dispatches: number;
  pool_hit_rate: number | null;
  lease_miss_by_reason: Record<string, number>;
  lease_miss_total: number;
}

export interface PoolFunnelSnapshot {
  window_hours: number;
  pool_lease_hits: number;
  cold_dispatches: number;
  total_dispatches: number;
  pool_hit_rate: number | null;
  lease_miss_by_reason: Record<string, number>;
  lease_miss_total: number;
  // Per-account breakdown of the same funnel (omitted by older backends).
  by_account?: Record<string, PoolFunnelAccountBreakdown>;
}

// Per-account warm-pool idle cost, as built by
// efficiency_snapshot.compute_pool_idle_cost -> by_account. Covers LIVE members
// only; the ledger carry-forward of already-reaped members is fleet-wide and
// intentionally absent here (PoolIdleCostLedger carries no account_id).
export interface PoolIdleCostAccountBreakdown {
  settled_idle_cost_usd: number;
  live_idle_cost_usd: number;
  total_idle_cost_usd: number;
  member_count: number;
  idle_count: number;
}

// Per-account cost-amortization view, as built by
// efficiency_snapshot.compute_pool_cost_amortization_by_account: joins the
// per-account idle cost with the per-account dispatch funnel so an operator can
// read warm-claim counts, hit rate, idle cost, and idle dollars per warm start
// per account. cost_per_pool_hit_usd is null when that account has no warm hit
// yet (its idle spend is not being amortized).
export interface PoolAccountAmortization {
  pool_idle_cost_usd: number;
  pool_lease_hits: number;
  cold_dispatches: number;
  total_dispatches: number;
  pool_hit_rate: number | null;
  cost_per_pool_hit_usd: number | null;
}

// Warm-pool sizing + idle-cost + dispatch-funnel snapshot. All fields optional
// for backward compatibility: an older backend that predates the pool metrics
// returns no `pool_metrics` block (or {}) and the panel renders without it.
export interface PoolMetrics {
  enabled?: boolean;
  target_size?: number;
  idle_ttl_secs?: number;
  current_size?: number;
  idle_count?: number;
  projected_idle_cost_usd?: number;
  settled_idle_cost_usd?: number;
  live_idle_cost_usd?: number;
  funnel?: PoolFunnelSnapshot;
  // Current per-project enablement scope (sorted project ids). An empty array
  // means the pool is fleet-wide (legacy all-or-nothing). Omitted by older
  // backends. The panel renders each scoped project with a disable control.
  project_allowlist?: string[];
  // Per-account breakdowns for a controlled enable's 'retire or pivot'
  // decision (omitted by older backends). idle_cost_by_account answers which
  // account's idle members drive cost; cost_amortization_by_account joins that
  // with the per-account dispatch funnel for per-account hit rate + $/warm.
  idle_cost_by_account?: Record<string, PoolIdleCostAccountBreakdown>;
  cost_amortization_by_account?: Record<string, PoolAccountAmortization>;
  // Cost-aware auto-drain guard diagnostic, as built by
  // warm_pool_maintainer.evaluate_pool_cost_guard and surfaced through
  // admin_health._get_pool_metrics. When `engaged`, the maintainer is draining
  // idle members and pausing provisioning because the pool's hit rate sits below
  // `min_hit_rate` while idle spend exceeds `min_idle_cost_usd`. `reason` is the
  // human-readable explanation. Optional/nullable: omitted by older backends and
  // null when the diagnostic could not be computed — the panel renders no banner.
  cost_guard?: {
    engaged: boolean;
    reason: string | null;
    hit_rate: number | null;
    total_dispatches: number;
    idle_cost_usd: number;
    min_hit_rate: number;
    min_idle_cost_usd: number;
  } | null;
}

// Per-account slice of the offline pool hit-rate estimate, as built by
// efficiency_snapshot._estimate_pool_hit_rate -> by_account.
export interface PoolHitRateEstimateAccount {
  total_dispatches: number;
  estimated_warm_hits: number;
  estimated_hit_rate: number | null;
  allocated_slots: number;
}

// Offline, read-only counterfactual estimate of the warm-pool hit rate a
// project would achieve at a candidate target size, returned by GET
// /api/admin/projects/{project_id}/pool-hit-rate-estimate. Derived purely from
// recent per-account dispatch history — it provisions nothing and does not
// change the dispatch path. estimated_hit_rate is null when there are no
// dispatches in the window. account_retarget_scenario is the same estimate with
// account-retarget on (any idle member can serve any account).
export interface PoolHitRateEstimate {
  project_id: string;
  target_size: number;
  window_days: number;
  replenish_seconds: number;
  idle_ttl_seconds: number;
  total_dispatches: number;
  estimated_warm_hits: number;
  estimated_hit_rate: number | null;
  by_account: Record<string, PoolHitRateEstimateAccount>;
  account_retarget_scenario: {
    estimated_warm_hits: number;
    estimated_hit_rate: number | null;
  };
  assumptions: string;
}

// Per-reason dominant warm-reuse dropout diagnostic, returned by GET
// /api/admin/projects/{project_id}/warm-reuse-dropout. Mirrors the return value
// of warm_backlog_reuse.compute_dominant_reuse_dropout. dominant_dropout is null
// when no single reason clearly dominates the non-converting population (see the
// gating constants in the backend helper); the panel renders "—" in that case.
export interface WarmReuseDropoutDominant {
  reason: string;
  count: number;
  total: number;
  share: number;
}

export interface WarmReuseDropoutDiagnostic {
  window_hours: number;
  dropout_by_reason: Record<string, number>;
  dropout_total: number;
  dominant_dropout: WarmReuseDropoutDominant | null;
  attribution_note: string;
}

// One persisted day-over-day bucket from GET
// /api/admin/projects/{project_id}/warm-reuse-dropout-trend. Each bucket
// corresponds to a daily efficiency snapshot; dominant_reason and dominant_share
// are null when that day's snapshot had no dominant dropout.
export interface WarmReuseDropoutTrendBucket {
  captured_at: string | null;
  dropout_total: number | null;
  dominant_reason: string | null;
  dominant_share: number | null;
}

// Per-project warm-pool TARGET sizing, read from GET /api/admin/settings. The
// maintainer reconciles each scoped project toward its explicit per-project
// target in `warm_pool_target_size_by_project` (keyed by project id), falling
// back to the global `warm_pool_target_size` when the project has no explicit
// entry. Both fields are always present on a current backend; an older backend
// that predates per-project targets simply omits the map (treated as {}).
export interface WarmPoolTargets {
  warm_pool_target_size: number;
  warm_pool_target_size_by_project: Record<string, number>;
}

// Response from POST /api/admin/projects/{project_id}/warm-pool/target. Mirrors
// the additive-only keys returned by admin_warm_pool.set_warm_pool_target_for_project:
// `target_size` is the explicit per-project value now stored (null when cleared),
// `effective_target` is the value the maintainer reconciles toward after global
// fallback, `cleared` is whether the explicit entry was removed, and
// `warm_pool_target_size_by_project` is the resulting map.
export interface WarmPoolTargetSetResponse {
  project_id: string;
  target_size: number | null;
  effective_target: number;
  cleared: boolean;
  warm_pool_target_size_by_project: Record<string, number>;
}

export interface WarmVmStateResponse {
  active_vms: WarmVmRow[];
  // Whether warm_pool_account_retarget_enabled is on (any idle member serves
  // any account in the tenant). Optional for backward compatibility with older
  // backends that predate the field; panel treats absent as false.
  account_retarget_enabled?: boolean;
  warm_pin_metrics: {
    clears_last_hour: number;
    clears_last_24h: number;
    clears_by_reason_last_hour?: Record<string, number>;
    clears_by_reason_last_24h?: Record<string, number>;
    active_pinned_queued_total: number;
    active_warm_chains: number;
    active_backlog_reuse_claims: number;
    // Pool-origin in-flight claims, isolated from chain-continuity (a
    // pre-provisioned warm-pool member served the dispatch). Optional for
    // backward compatibility with older backends that folded it into the
    // chain-continuity bucket. pool_origin_share is the pool fraction of all
    // active warm chains (null when there are no active warm chains).
    active_pool_origin_claims?: number;
    pool_origin_share?: number | null;
    active_chain_continuity_claims: number;
    // Warm-subsystem circuit-breaker suppression state. All optional for
    // backward compatibility with older backends (and existing test fixtures)
    // that predate the breaker telemetry — when absent the panel renders no
    // callout. The backend emits these from admin_health._compute_warm_vm_state:
    // a *_circuit_breaker_suppressed boolean, the *_suppressed_until reset
    // timestamp (naive UTC ISO, or null), the recent-failure count in the last
    // hour, and the failure-rate threshold that trips the breaker.
    // Chain continuity (warm-pin clear breaker):
    circuit_breaker_suppressed?: boolean;
    suppressed_until?: string | null;
    clear_rate_threshold?: number;
    // Backlog reuse claim breaker:
    backlog_reuse_circuit_breaker_suppressed?: boolean;
    backlog_reuse_suppressed_until?: string | null;
    backlog_reuse_failures_last_hour?: number;
    backlog_reuse_failure_rate_threshold?: number;
    // Warm-pool claim breaker:
    pool_circuit_breaker_suppressed?: boolean;
    pool_suppressed_until?: string | null;
    pool_claim_failures_last_hour?: number;
    pool_failure_rate_threshold?: number;
  };
  warm_continuity_enabled: boolean;
  warm_backlog_reuse_enabled: boolean;
  // Warm-pool retarget controls. Optional for backward compatibility with an
  // older backend that predates surfacing them in the warm-vm-state payload —
  // the panel defaults each to false when absent. account_retarget lets an idle
  // pool member serve a dispatch whose account differs; cross_provider_retarget
  // further relaxes the provider match.
  warm_pool_account_retarget_enabled?: boolean;
  warm_pool_cross_provider_retarget_enabled?: boolean;
  next_role_poll_timeout_secs: number;
  chain_continuity_funnel?: ChainContinuityFunnelSnapshot;
  spawn_pin_funnel?: SpawnPinFunnelSnapshot;
  backlog_reuse_funnel?: BacklogReuseFunnelSnapshot;
  // Optional for backward compatibility (older backend omits it entirely).
  pool_metrics?: PoolMetrics;
}
