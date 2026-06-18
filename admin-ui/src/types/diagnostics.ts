export interface StuckTask {
  task_id: string;
  title: string;
  status: string;
  stuck_reason: 'provision_timeout' | 'no_heartbeat' | 'long_queued' | 'stuck_deps' | 'evaluation_timeout';
  stuck_for_seconds: number;
  attempt_count: number;
  failure_class: string | null;
  unresolvable_deps: string[];
  remediation_hint?: string;
}

export interface ProjectDiagnostics {
  project_id: string;
  stuck_tasks: StuckTask[];
  circuit_breaker: { state: string; failure_count: number; opened_at: string | null };
  queued_count: number;
  in_progress_count: number;
  zone_health: { available: number; exhausted: string[]; cooldown_recovery?: Record<string, string> };
}

export interface FailingTaskTypeEntry {
  task_type: string;
  failure_rate: number;
  sample_size: number;
  threshold: number;
}

export interface FailingTaskTypes {
  gated_types: FailingTaskTypeEntry[];
  global_failure_threshold: number;
  anomaly_tightened: boolean;
  window_minutes: number;
  generated_at: string;
}

export interface DirectionAutoPauseStats {
  window_hours: number;
  auto_paused_count: number;
  auto_recovered_count: number;
  auto_retired_count: number;
  currently_auto_paused: number;
  top_reasons: { reason: string; count: number }[];
  trend: 'up' | 'down' | 'flat';
  prev_window_auto_paused_count: number;
}
