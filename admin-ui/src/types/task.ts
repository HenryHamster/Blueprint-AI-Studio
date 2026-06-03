import type { TaskType } from '../taskCapabilities';

export type TaskStatus = 'QUEUED' | 'PROVISIONING' | 'IN_PROGRESS' | 'EVALUATING' | 'BLOCKED' | 'COMPLETED' | 'FAILED' | 'CANCELLED';
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
  claims?: string[];
  sources?: Array<{ type?: string; ref: string; excerpt?: string }>;
  _provenance_warnings?: { broken_source_ref_count: number; broken_refs: string[] };
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
  // Durable blocker metadata for the non-terminal BLOCKED state (backend
  // alembic 0137 / models.py). All four are nullable — older task payloads
  // (and any non-BLOCKED task) carry null/undefined, so every consumer must
  // treat these as optional and guard on presence before rendering.
  blocker_kind?: string | null;
  blocked_reason?: string | null;
  blocked_at?: string | null;
  recheck_after?: string | null;
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
  // Slim, read-only mirror of the no-CI merge gate's UNVERIFIED-merge reason
  // (backend admin.py mirrors review_loop["unverified_merge_reason"]). Null /
  // absent for CI-verified, tests-gated, or otherwise normal merges. The Task
  // Detail drawer's Merge Test Gate section reads this to render the UNVERIFIED
  // verdict + human-readable reason when no MERGE_TEST_RESULT artifact exists.
  unverified_merge_reason?: string | null;
  payloads?: Record<string, unknown>[];
  artifacts?: Array<{artifact_type: string; pr_url?: string; commit_hash?: string; repo_url?: string; logs?: string; iteration: number; created_at?: string}>;
  pr_url?: string;
  commit_hash?: string;
  repo_url?: string;
  error_logs?: string;
  failure_class?: string | null;
  research_findings?: ResearchFinding[] | null;
  context?: {
    fix_loop_abandoned?: { loop: string; exit_reason: string; at: string };
    [key: string]: unknown;
  };
  history?: { timestamp: string; action: string; details: Record<string, unknown> }[];
  child_task_ids?: string[];
  estimated_cost_usd?: number | null;
  api_call_summary?: {
    llm_call_count?: number;
    total_input_tokens?: number;
    total_output_tokens?: number;
    last_updated_at?: string;
  } | null;
  fix_loop_history?: Record<string, FixLoopAttempt[]> | null;
  fix_loop_summary?: FixLoopSummary[] | null;
  edges_considered?: EdgeConsidered[] | null;
}

export interface EdgeConsidered {
  case: string;
  handled: boolean;
  evidence?: string;
  blind_spot?: string;
}

export interface FixLoopSummary {
  loop: string;
  iterations: number;
  last_exit_reason: string | null;
  non_success: boolean;
}

export interface FixLoopAttempt {
  loop: string;
  iteration: number;
  fingerprint_hash?: string | null;
  error_tail?: string | null;
  change_summary?: string | null;
  exit_reason?: string | null;
  source?: string | null;
  started_at?: string | null;
  ended_at?: string | null;
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
