export interface StuckTask {
  task_id: string;
  title: string;
  status: string;
  stuck_reason: 'provision_timeout' | 'no_heartbeat' | 'long_queued' | 'stuck_deps' | 'evaluation_timeout';
  stuck_for_seconds: number;
  attempt_count: number;
  failure_class: string | null;
  unresolvable_deps: string[];
}

export interface ProjectDiagnostics {
  project_id: string;
  stuck_tasks: StuckTask[];
  circuit_breaker: { state: string; failure_count: number; opened_at: string | null };
  queued_count: number;
  in_progress_count: number;
  zone_health: { available: number; exhausted: string[] };
}
