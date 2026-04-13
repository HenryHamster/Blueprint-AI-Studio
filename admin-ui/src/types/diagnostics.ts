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
  zone_health: { available: number; exhausted: string[] };
}

export interface HealthOverviewIssue {
  severity: 'critical' | 'warning' | 'info';
  title: string;
  hint: string;
  task_ids: string[];
  category: string;
}

export interface HealthOverview {
  overall_status: 'healthy' | 'degraded' | 'unhealthy';
  success_rate: { current: number | null; trend: 'improving' | 'stable' | 'declining'; window_hours: number };
  actionable_issues: HealthOverviewIssue[];
  direction_portfolio: { product: number; structural: number; mixed: number; uncategorized: number; total_active: number };
  queue_depth: { queued: number; active: number; evaluating: number };
}
