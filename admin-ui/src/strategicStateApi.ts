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

export interface StrategicMessageTarget {
  messageId: string;
  action?: 'highlight' | 'reply';
}

export interface StrategicContextPackageSource {
  kind: string;
  count: number;
}

export interface StrategicContextPackageDrilldown {
  id: string;
  label: string;
  target: {
    surface: 'project' | 'pipeline' | 'runtime' | 'messages' | 'task' | 'knowledge' | string;
    section?: string | null;
    task_id?: string | null;
    document_id?: string | null;
    direction_id?: string | null;
    artifact_id?: string | null;
  };
}

export interface StrategicContextCanvasNode {
  id: string;
  kind: string;
  label: string;
  summary?: string | null;
  status?: string | null;
  count?: number | null;
  target?: StrategicContextPackageDrilldown['target'] | null;
}

export interface StrategicContextCanvasEdge {
  source: string;
  target: string;
  label?: string | null;
}

export interface StrategicContextResourceRef {
  id: string;
  kind: 'canvas' | 'knowledge_document' | 'artifact' | 'task' | 'project_section' | string;
  label: string;
  summary?: string | null;
  target?: StrategicContextPackageDrilldown['target'] | null;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface StrategicContextKnowledgeEvidence {
  id: string;
  kind: 'knowledge_document' | string;
  label?: string | null;
  summary?: string | null;
  source_kind?: string | null;
  topic?: string | null;
  direction_id?: string | null;
  source_task_id?: string | null;
  target?: StrategicContextPackageDrilldown['target'] | null;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface StrategicContextArtifactEvidence {
  id: string;
  kind: string;
  label?: string | null;
  artifact_type?: string | null;
  task_id?: string | null;
  task_title?: string | null;
  task_status?: string | null;
  logical_key?: string | null;
  target?: StrategicContextPackageDrilldown['target'] | null;
  created_at?: string | null;
}

export interface StrategicContextEvidenceCollection<T> {
  total: number;
  items: T[];
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
    edge_count?: number | null;
    nodes?: StrategicContextCanvasNode[];
    edges?: StrategicContextCanvasEdge[];
    drilldowns?: StrategicContextPackageDrilldown[];
  } | null;
  resources?: StrategicContextResourceRef[];
  evidence?: {
    knowledge?: StrategicContextEvidenceCollection<StrategicContextKnowledgeEvidence>;
    artifacts?: StrategicContextEvidenceCollection<StrategicContextArtifactEvidence>;
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

export interface StrategicRuntimeComputeJobPreview {
  id: string;
  task_id: string | null;
  task_title?: string | null;
  task_type?: string | null;
  status: string;
  backend: string;
  machine_type?: string | null;
  accelerator_type?: string | null;
  accelerator_count?: number | null;
  spend_cap_usd?: number | null;
  cost_usd?: number | null;
  created_at?: string | null;
  updated_at?: string | null;
  started_at?: string | null;
  completed_at?: string | null;
  last_heartbeat_at?: string | null;
  next_poll_at?: string | null;
  terminal_reason?: string | null;
}

export interface StrategicRuntimeWakePreview {
  id: string;
  task_id: string;
  task_title?: string | null;
  task_status?: string | null;
  wake_source_kind: string;
  wake_source_ref?: string | null;
  wake_action: string;
  delivery_state: string;
  requested_at?: string | null;
  deadline_at?: string | null;
  fired_at?: string | null;
  delivered_at?: string | null;
  attempt_count: number;
  result_state?: string | null;
  result_detail?: string | null;
}

export interface StrategicRuntimeWorkState {
  compute_jobs: {
    window_minutes: number;
    counts: {
      active: number;
      terminal_recent: number;
      total: number;
      queued: number;
      starting: number;
      running: number;
      polling: number;
      succeeded_recent: number;
      failed_recent: number;
      cancelled_recent: number;
      timed_out_recent: number;
      criterion_measured: number;
      criterion_met: number;
    };
    spend: {
      active_accrued_usd: number;
      recent_terminal_usd: number;
      window_total_usd: number;
    };
    items: StrategicRuntimeComputeJobPreview[];
  };
  held_approvals: {
    count: number;
    items: StrategicStateTask[];
  };
  parked_wakes: {
    counts: {
      active: number;
      pending: number;
      fired: number;
      problem: number;
      expired: number;
      failed: number;
    };
    items: StrategicRuntimeWakePreview[];
  };
}

export interface StrategicWorkbenchState {
  version: 1;
  interaction_mode: 'chat' | 'check_in' | 'monitor' | 'continuous' | string;
  active_surface: 'strategic' | 'pipeline' | 'project' | 'runtime' | string;
  selected_refs: Array<{
    kind: string;
    id: string;
    label?: string;
  }>;
  session_started_at?: string | null;
  last_interaction_at?: string | null;
}

export interface StrategicWorkSessionMetric {
  id: string;
  label: string;
  value: number;
  unit?: string | null;
  tone?: 'neutral' | 'watch' | 'warning' | 'critical' | 'active' | string;
  target?: StrategicContextPackageDrilldown['target'] | null;
}

export interface StrategicWorkSessionRefDescriptor {
  kind: string;
  id: string;
  label: string;
  summary?: string | null;
  status?: string | null;
  target?: StrategicContextPackageDrilldown['target'] | null;
}

export interface StrategicWorkSession {
  version: 1;
  id: string;
  mode: string;
  active_surface: string;
  session_started_at?: string | null;
  last_interaction_at?: string | null;
  context_package_id?: string | null;
  attention_level?: string | null;
  selected_refs: StrategicWorkbenchState['selected_refs'];
  selected_ref_descriptors: StrategicWorkSessionRefDescriptor[];
  curated_metrics: StrategicWorkSessionMetric[];
  context_sources?: StrategicContextPackageSource[];
}

export interface StrategicSessionConversation {
  limit: number;
  has_more?: boolean;
  order?: 'oldest_first' | 'newest_first' | string;
  items: StrategicStateMessage[];
}

export interface StrategicSessionOutput {
  kind: 'message' | 'cycle' | string;
  id: string | null;
  message_type: string;
  from_type: string;
  from_task_id?: string | null;
  content: string;
  status: string | null;
  created_at?: string | null;
  context?: Record<string, unknown>;
}

export interface StrategicSessionResponse {
  project_id: string;
  status: string;
  location: string;
  status_reason?: string | null;
  session: StrategicWorkSession & {
    status?: string;
    location?: string;
    status_reason?: string | null;
    conversation?: StrategicSessionConversation;
    recent_outputs?: StrategicSessionOutput[];
  };
  workbench?: StrategicWorkbenchState | null;
  strategic?: StrategicStateResponse['strategic'];
  runtime?: StrategicStateResponse['runtime'];
  context_package?: StrategicContextPackageDescriptor | null;
  conversation: StrategicSessionConversation;
  recent_outputs: StrategicSessionOutput[];
  arming?: string | null;
}

export interface StrategicSessionMessageCreate {
  content: string;
  message_type?: string;
  context?: Record<string, unknown>;
  workbench_context?: Record<string, unknown>;
  idempotency_key?: string;
}

export interface StrategicSessionMessageResponse {
  id: string;
  status: 'created' | 'duplicate' | string;
  arming?: string | null;
  message?: StrategicStateMessage;
}

export interface StrategicWorkbenchStateResponse {
  status?: string;
  workbench: StrategicWorkbenchState;
  arming?: string | null;
}

export interface StrategicStateTaskFocusItem {
  id: string;
  bucket: 'needs_judgment' | 'active' | 'friction' | 'queued' | 'recent_completed' | string;
  status: string;
  title: string | null;
  task_type?: string | null;
  latest_status_message?: string | null;
  detail?: string | null;
  requires_approval: boolean;
  reasons: string[];
  direction_id?: string | null;
  parent_task_id?: string | null;
  blocker_kind?: string | null;
  blocked_reason?: string | null;
  blocked_at?: string | null;
  recheck_after?: string | null;
  park_reason?: string | null;
  parked_at?: string | null;
  created_at?: string | null;
  started_at?: string | null;
  completed_at?: string | null;
}

export interface StrategicStateTaskFocusBucket {
  total: number;
  limit: number;
  has_more: boolean;
  items: StrategicStateTaskFocusItem[];
}

export interface StrategicStateTaskFocus {
  version: 1;
  bucket_limit: number;
  has_more: boolean;
  buckets: Record<string, StrategicStateTaskFocusBucket>;
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
      parked: number;
      failed: number;
      completed: number;
      total: number;
    };
    cumulative_estimated_cost_usd: number;
    focus?: StrategicStateTaskFocus;
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
    work?: StrategicRuntimeWorkState | null;
  };
  workbench?: StrategicWorkbenchState | null;
  work_session?: StrategicWorkSession | null;
  design: {
    has_design_doc: boolean;
    revision?: number | null;
  };
  sha_freshness?: {
    active_candidate_sha?: string | null;
    integration_head_sha?: string | null;
    verdict: 'stale' | 'fresh' | 'unknown' | string;
    generated_at?: string | null;
    checked_at?: string | null;
    integration_branch?: string | null;
    stale?: boolean | null;
    stale_reason?: string | null;
  } | null;
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

export async function fetchStrategicWorkbenchState(
  projectId: string,
  signal?: AbortSignal,
): Promise<StrategicWorkbenchState> {
  const params = new URLSearchParams({ project_id: projectId });
  const res = await apiFetch(
    `/api/admin/messages/workbench-state?${params}`,
    undefined,
    2,
    30000,
    signal,
  );
  if (!res.ok) throw new Error(`Failed to fetch Strategic workbench state: ${res.status}`);
  const data = await res.json() as { workbench: StrategicWorkbenchState };
  return data.workbench;
}

export async function updateStrategicWorkbenchState(
  projectId: string,
  workbench: Partial<StrategicWorkbenchState>,
): Promise<StrategicWorkbenchStateResponse> {
  const res = await apiFetch('/api/admin/messages/workbench-state', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ project_id: projectId, workbench }),
  });
  if (!res.ok) throw new Error(`Failed to update Strategic workbench state: ${res.status}`);
  return res.json() as Promise<StrategicWorkbenchStateResponse>;
}

export async function fetchStrategicSession(
  projectId: string,
  includeMessageId?: string | null,
  signal?: AbortSignal,
): Promise<StrategicSessionResponse> {
  const params = new URLSearchParams();
  if (includeMessageId) params.set('include_message_id', includeMessageId);
  const query = params.toString();
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/strategic-session${query ? `?${query}` : ''}`,
    undefined,
    2,
    30000,
    signal,
  );
  if (!res.ok) throw new Error(`Failed to fetch Strategic session: ${res.status}`);
  return res.json() as Promise<StrategicSessionResponse>;
}

export async function createStrategicSessionMessage(
  projectId: string,
  payload: StrategicSessionMessageCreate,
): Promise<StrategicSessionMessageResponse> {
  const res = await apiFetch(`/api/admin/projects/${encodeURIComponent(projectId)}/strategic-session/messages`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  if (!res.ok) throw new Error(`Failed to create Strategic session message: ${res.status}`);
  return res.json() as Promise<StrategicSessionMessageResponse>;
}
