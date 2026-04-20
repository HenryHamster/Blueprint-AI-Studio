export interface DeployState {
  status: string;
  current_revision: string;
  current_sha: string;
  deployed_at: string;
  last_known_good_revision: string;
  last_known_good_sha: string;
  rollback_reason: string | null;
  rollback_count: number;
  pause_reason: string | null;
  paused_at: string | null;
  reason: string | null;
  pre_pause_max_vms: number | null;
  failure_rate: number;
  monitoring_window: { elapsed_seconds: number; total_seconds: number; percent: number } | null;
  task_results_count: number;
  gcp_project_id: string;
  health_recovery_streak: number;
  health_recovery_streak_required: number;
  health_failure_streak: number;
  health_failure_streak_threshold: number;
  health_check_interval_seconds?: number;
  external_health?: Record<string, unknown>;
}

export interface MaxPlanAccount {
  id: string;
  name: string;
  enabled: boolean;
  active_tasks: number;
  total_tasks: number;
  token_created_at: string | null;
  rl_hit_count?: number;
  disabled_models?: Record<string, { hits: number; at: string | null }>;
}

export interface OrchestratorHealth {
  last_cycle_seconds_ago: number | null;
  queued_tasks: number;
  active_tasks: number;
  pool_info: Record<string, unknown>;
  deploy_monitoring?: {
    external_health?: Record<string, boolean | string | string[]>;
  };
}

export interface ZoneHealthEntry {
  status: string;
  last_success: string | null;
  last_failure: string | null;
  last_failure_reason: string | null;
  consecutive_failures: number;
  cooldown_active: boolean;
}
