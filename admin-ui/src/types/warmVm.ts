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
  // Per-account breakdowns for a controlled enable's 'retire or pivot'
  // decision (omitted by older backends). idle_cost_by_account answers which
  // account's idle members drive cost; cost_amortization_by_account joins that
  // with the per-account dispatch funnel for per-account hit rate + $/warm.
  idle_cost_by_account?: Record<string, PoolIdleCostAccountBreakdown>;
  cost_amortization_by_account?: Record<string, PoolAccountAmortization>;
}

export interface WarmVmStateResponse {
  active_vms: WarmVmRow[];
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
  };
  warm_continuity_enabled: boolean;
  warm_backlog_reuse_enabled: boolean;
  next_role_poll_timeout_secs: number;
  chain_continuity_funnel?: ChainContinuityFunnelSnapshot;
  spawn_pin_funnel?: SpawnPinFunnelSnapshot;
  backlog_reuse_funnel?: BacklogReuseFunnelSnapshot;
  // Optional for backward compatibility (older backend omits it entirely).
  pool_metrics?: PoolMetrics;
}
