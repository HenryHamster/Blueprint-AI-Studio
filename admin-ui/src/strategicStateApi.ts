import { apiFetch, type StrategicRefreshMarker } from './api';
import type { ProjectMessagePreview } from './types/messages';

export interface StrategicStateCycle {
  id: string;
  outcome: string;
  has_active_task?: boolean;
  stale?: boolean;
  phase1_task_id: string | null;
  phase2_task_id: string | null;
  phase1_started_at: string | null;
  phase1_completed_at: string | null;
  phase2_started_at: string | null;
  phase2_completed_at: string | null;
  summary_text?: string | null;
  failure_reason?: string | null;
  directions_created: number;
  directions_retired: number;
  tasks_spawned: number;
  vision_updated: boolean;
  problem_model_changed: boolean;
  running_update_changed: boolean;
}

export interface StrategicStateTask {
  id: string;
  status: string;
  title?: string | null;
  task_type?: string | null;
  latest_status_message?: string | null;
  started_at?: string | null;
  created_at?: string | null;
}

export type StrategicStateMessagePreview = ProjectMessagePreview;
export type StrategicStateMessage = StrategicStateMessagePreview;

export interface StrategicStateAttentionItem {
  id: string;
  kind: 'message' | 'task' | 'direction' | string;
  severity: 'needs_operator' | 'watch' | string;
  reason: string;
  label: string;
  detail?: string | null;
  unread?: boolean;
  status?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  ref: {
    kind: string;
    id: string;
    project_id: string;
  };
  task_id?: string | null;
  domain?: string | null;
  event_type?: string | null;
}

export interface StrategicContextPackageSource {
  kind: string;
  count: number;
}

export interface StrategicContextPackageDrilldown {
  id: string;
  label: string;
  target: {
    surface: 'project' | 'pipeline' | 'runtime' | string;
    section?: string | null;
  };
}

export interface StrategicContextPackageDescriptor {
  id: string;
  title: string;
  status?: 'ready' | 'building' | 'stale' | string;
  updated_at?: string | null;
  token_count?: number | null;
  item_count?: number | null;
  sources?: StrategicContextPackageSource[];
  canvas?: {
    id?: string;
    title?: string;
    kind?: string;
    node_count?: number | null;
  } | null;
  drilldowns?: StrategicContextPackageDrilldown[];
}

export interface StrategicLocalRunnerRuntime {
  provider?: string | null;
  model?: string | null;
  account_id?: string | null;
  vm_instance_id?: string | null;
  runner_target_id?: string | null;
}

export interface StrategicLocalRunnerState {
  mode: 'local' | 'local_ready' | 'cloud' | 'pending_local' | 'unknown' | string;
  strategic_runner_count: number;
  live_runner_count: number;
  eligible_live_count: number;
  stale_runner_count: number;
  busy_runner_count: number;
  agent_unavailable_count: number;
  active_runner_id?: string | null;
  active_task_pinned: boolean;
  active_task_runtime?: StrategicLocalRunnerRuntime | null;
  fallback_reason?:
    | 'no_runner'
    | 'runner_offline'
    | 'agent_unavailable'
    | 'at_capacity'
    | 'cloud_dispatch'
    | 'capability_mismatch'
    | 'runner_claim_pending'
    | 'runner_unavailable'
    | string
    | null;
  liveness_window_seconds: number;
}

export interface StrategicStateResponse {
  project_id: string;
  strategic: {
    active_cycle: StrategicStateCycle | null;
    active_task: StrategicStateTask | null;
    active_phase: 'design_doc_update' | 'strategic_review' | string | null;
    latest_cycle: StrategicStateCycle | null;
    refresh: {
      pending: StrategicRefreshMarker | null;
      last_fired_at?: string | null;
      last_fired_origin?: string | null;
    };
  };
  running_update: {
    excerpt: string | null;
    char_count: number;
    acknowledged_at: string | null;
  };
  problem_model: {
    revision?: number | null;
    last_updated?: string | null;
    current_focus?: string | null;
    purpose?: string | null;
    active_gap_count: number;
    capability_count: number;
    pending_confirmation_count: number;
    top_gap: {
      id: string;
      name: string | null;
      priority: string;
      status: string;
    } | null;
  };
  directions: {
    total_count: number;
    active_count: number;
    paused_count: number;
    primary: {
      id: string;
      text: string | null;
      priority: string;
      status: string;
      mode?: string | null;
    } | null;
  };
  messages: {
    counts: { open: number; linked: number };
    items: StrategicStateMessage[];
  };
  tasks: {
    counts: {
      queued: number;
      running: number;
      evaluating: number;
      blocked: number;
      failed: number;
      completed: number;
      total: number;
    };
    cumulative_estimated_cost_usd: number;
  };
  attention?: {
    level: 'clear' | 'watch' | 'needs_operator' | string;
    summary: string;
    last_seen_at: string | null;
    counts: {
      needs_operator: number;
      watch: number;
      visible_needs_operator?: number;
      visible_watch?: number;
      unread: number;
      questions: number;
      blockers: number;
      bug_reports?: number;
      feedback?: number;
      approvals: number;
      manual_directions: number;
      alerts?: number;
      error_alerts?: number;
      warning_alerts?: number;
      failed_tasks: number;
      blocked_tasks: number;
      parked_tasks: number;
    };
    items: StrategicStateAttentionItem[];
  };
  context_package?: StrategicContextPackageDescriptor | null;
  runtime?: {
    local_runner?: StrategicLocalRunnerState | null;
  };
  design: {
    has_design_doc: boolean;
    revision?: number | null;
  };
}

export async function fetchStrategicState(
  projectId: string,
  signal?: AbortSignal,
): Promise<StrategicStateResponse> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/strategic-state`,
    undefined,
    2,
    30000,
    signal,
  );
  if (!res.ok) throw new Error(`Failed to fetch strategic state: ${res.status}`);
  return res.json() as Promise<StrategicStateResponse>;
}
