export type TaskStatus = 'QUEUED' | 'PROVISIONING' | 'IN_PROGRESS' | 'EVALUATING' | 'COMPLETED' | 'FAILED' | 'CANCELLED';
export type TaskType = 'create' | 'plan' | 'research' | 'review' | 'fix' | 'refactor' | 'docs' | 'security_audit' | 'qa' | 'deps' | 'migrate' | 'investigate' | 'debug' | 'smoketest' | 'rollup' | 'perf' | 'strategic_review' | 'design_doc_update';

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
  payloads?: Record<string, unknown>[];
  artifacts?: Array<{artifact_type: string; pr_url?: string; commit_hash?: string; repo_url?: string; logs?: string; iteration: number; created_at?: string}>;
  pr_url?: string;
  commit_hash?: string;
  repo_url?: string;
  error_logs?: string;
  error_logs_truncated?: boolean;
  failure_class?: string | null;
  research_findings?: ResearchFinding[] | null;
  context?: Record<string, unknown>;
  history?: { timestamp: string; action: string; details: Record<string, unknown> }[];
  child_task_ids?: string[];
}

export type TrackLogType = 'log' | 'status' | 'artifact' | 'error' | 'system';

export interface TrackLogEntry {
  id: number;
  t: string;
  type: TrackLogType;
  msg: string;
}
