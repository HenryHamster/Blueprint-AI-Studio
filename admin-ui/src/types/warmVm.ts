// Shape returned by GET /api/admin/warm-vm-state. Mirrors the Python
// payload in backend/app/routers/admin_health.py:get_warm_vm_state.
export interface WarmVmCurrentTask {
  id: string;
  title: string;
  task_type: string;
  status: string;
  warm_start: boolean;
  // "backlog_reuse" | "next_role_endpoint" | null (legacy/unset). Set by the
  // server when a warm VM claims a task; distinguishes same-project backlog
  // reuse from PR-chain continuity. See admin_health._compute_warm_vm_state.
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
    active_chain_continuity_claims: number;
  };
  warm_continuity_enabled: boolean;
  warm_backlog_reuse_enabled: boolean;
  next_role_poll_timeout_secs: number;
  chain_continuity_funnel?: ChainContinuityFunnelSnapshot;
  spawn_pin_funnel?: SpawnPinFunnelSnapshot;
}
