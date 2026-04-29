// Shape returned by GET /api/admin/warm-vm-state. Mirrors the Python
// payload in backend/app/routers/admin_health.py:get_warm_vm_state.
export interface WarmVmCurrentTask {
  id: string;
  title: string;
  task_type: string;
  status: string;
  warm_start: boolean;
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

export interface WarmVmStateResponse {
  active_vms: WarmVmRow[];
  warm_pin_metrics: {
    clears_last_hour: number;
    clears_last_24h: number;
    active_pinned_queued_total: number;
    active_warm_chains: number;
  };
  warm_continuity_enabled: boolean;
  next_role_poll_timeout_secs: number;
}
