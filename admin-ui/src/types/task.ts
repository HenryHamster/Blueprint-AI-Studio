import type { TaskType } from '../taskCapabilities';

export type TaskStatus = 'QUEUED' | 'PROVISIONING' | 'IN_PROGRESS' | 'EVALUATING' | 'COMPLETED' | 'FAILED' | 'CANCELLED';
export type { TaskType };

export interface ResearchFinding {
  type?: string;
  title: string;
  description: string;
  prompt: string;
  scope: 'small' | 'medium' | 'large';
  risk: 'low' | 'medium' | 'high';
  priority: 'high' | 'medium' | 'low';
  related_files: string[];
  direction_id?: string;
}

export interface Task {
  id: string;
  project_id?: string;
  title: string;
  description: string;
  status: TaskStatus;
  task_type?: TaskType;
  vm_instance_id?: string;
  latest_status_message?: string;
  status_log?: { t: string; msg: string }[];
  created_by: string;
  requires_approval: boolean;
  parent_task_id?: string;
  depends_on?: string[];
  plan_output?: Record<string, unknown>;
  created_at: string;
  started_at?: string;
  last_heartbeat?: string;
  attempt_count?: number;
  // R3-W2 (H1): heartbeat-hot fields promoted from Task.context to dedicated
  // columns in Round 2 (B2 / F). The frontend reads from the columns; the
  // legacy `task.context.last_metrics` mirror was retired and reading it
  // silently breaks the System Metrics panel.
  last_metrics_json?: {
    rss_mb?: number;
    heap_used_mb?: number;
    disk_free_mb?: number;
    disk_total_mb?: number;
    cgroup_mb?: number;
    cgroup_limit_mb?: number;
  } | null;
  last_tool_use_at?: string | null;
  payloads?: Record<string, unknown>[];
  artifacts?: Array<{artifact_type: string; pr_url?: string; commit_hash?: string; repo_url?: string; logs?: string; iteration: number; created_at?: string}>;
  pr_url?: string;
  commit_hash?: string;
  repo_url?: string;
  error_logs?: string;
  failure_class?: string | null;
  research_findings?: ResearchFinding[] | null;
  context?: Record<string, unknown>;
  history?: { timestamp: string; action: string; details: Record<string, unknown> }[];
  child_task_ids?: string[];
  estimated_cost_usd?: number | null;
}

export interface SerialConsoleResponse {
  vm_instance_id: string;
  zone: string | null;
  vm_status: string | null;
  serial_log: string | null;
  captured_at: string;
  source: 'live' | 'cached';
}

export type TrackLogType = 'log' | 'status' | 'artifact' | 'error' | 'system';

export interface TrackLogEntry {
  id: number;
  t: string;
  type: TrackLogType;
  msg: string;
}
