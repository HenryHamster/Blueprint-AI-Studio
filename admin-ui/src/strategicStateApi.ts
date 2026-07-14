import { apiFetch, type StrategicRefreshMarker } from './api';
import type { ProjectMessagePreview } from './types/messages';
import type { WorkbenchEventEnvelopeV1 } from './types/workbenchEvents';

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
  movement_explanation?: string | null;
  started_at?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
}

export type StrategicStateMessagePreview = ProjectMessagePreview;
export type StrategicStateMessage = StrategicStateMessagePreview;

export interface StrategicStateAttentionItemAction {
  kind: string;
  label: string;
  method?: string | null;
  path?: string | null;
}

export interface StrategicStateAttentionItem {
  id: string;
  kind: 'message' | 'task' | 'direction' | 'confirmation' | string;
  severity: 'needs_operator' | 'watch' | string;
  reason: string;
  label: string;
  detail?: string | null;
  movement_explanation?: string | null;
  unread?: boolean;
  status?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  /** Present on ``kind: 'confirmation'`` items — the pending_user_confirmation type. */
  confirmation_type?: string | null;
  /** Inline affordances (accept/dismiss) the operator can act on from the daily surface. */
  actions?: StrategicStateAttentionItemAction[] | null;
  ref: {
    kind: string;
    id: string;
    project_id: string;
  };
  target?: StrategicContextTarget | null;
  lifecycle?: {
    mode?: string | null;
    clears_when?: string | null;
    operator_action?: string | null;
    acknowledgeable?: boolean | null;
    acknowledge_effect?: string | null;
  } | null;
  awaiting_since?: string | null;
  runner_id?: string | null;
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

export interface StrategicContextTarget {
  surface: 'project' | 'pipeline' | 'runtime' | 'messages' | 'task' | 'knowledge' | string;
  project_id?: string | null;
  section?: string | null;
  task_id?: string | null;
  message_id?: string | null;
  direction_id?: string | null;
  alert_id?: string | null;
  action?: string | null;
  document_id?: string | null;
  artifact_id?: string | null;
  cycle_id?: string | null;
  runner_id?: string | null;
}

export interface StrategicContextPackageDrilldown {
  id: string;
  label: string;
  target: StrategicContextTarget;
}

export interface StrategicContextDescriptorMetric {
  label: string;
  value: string | number;
  unit?: string | null;
  tone?: 'neutral' | 'info' | 'good' | 'warning' | 'critical' | string;
  target?: StrategicContextTarget | null;
}

export interface StrategicContextDescriptorTableColumn {
  key: string;
  label: string;
}

export interface StrategicContextDescriptorTableRow {
  id?: string | null;
  cells: Record<string, string | number | boolean | null>;
  target?: StrategicContextTarget | null;
}

export interface StrategicContextDescriptorDetail {
  label: string;
  value: string | number | boolean | null;
  target?: StrategicContextTarget | null;
}

export interface StrategicContextDescriptorChartPoint {
  label: string;
  value: number;
}

export interface StrategicContextRenderableDescriptor {
  schema_version: 1;
  id: string;
  kind:
    | 'context_map'
    | 'metric_cards'
    | 'table'
    | 'markdown'
    | 'text'
    | 'image'
    | 'video'
    | 'pdf'
    | 'chart'
    | 'plot'
    | 'task_detail'
    | 'direction_detail'
    | 'message_detail'
    | 'resource_detail'
    | 'unsupported'
    | string;
  title: string;
  summary?: string | null;
  status?: string | null;
  target?: StrategicContextTarget | null;
  empty?: boolean;
  stale?: boolean;
  unsupported_reason?: string | null;
  metrics?: StrategicContextDescriptorMetric[];
  columns?: StrategicContextDescriptorTableColumn[];
  rows?: StrategicContextDescriptorTableRow[];
  details?: StrategicContextDescriptorDetail[];
  body?: string | null;
  language?: string | null;
  image?: {
    url?: string | null;
    alt?: string | null;
    width?: number | null;
    height?: number | null;
    storage_uri?: string | null;
  } | null;
  video?: {
    url?: string | null;
    alt?: string | null;
    storage_uri?: string | null;
    mime_type?: string | null;
  } | null;
  images?: Array<{
    url?: string | null;
    alt?: string | null;
    filename?: string | null;
    storage_uri?: string | null;
  }> | null;
  videos?: Array<{
    url?: string | null;
    alt?: string | null;
    filename?: string | null;
    storage_uri?: string | null;
  }> | null;
  // PDFs are never rendered inline on the app origin — the descriptor carries a
  // storage_uri (and/or a baked signed url) that the frontend resolves fresh and
  // opens in a new browser tab (the PDF renders in the browser's sandboxed viewer
  // on the storage origin). Mirrors the image/video singular+list shape.
  pdf?: {
    url?: string | null;
    alt?: string | null;
    storage_uri?: string | null;
  } | null;
  pdfs?: Array<{
    url?: string | null;
    alt?: string | null;
    filename?: string | null;
    storage_uri?: string | null;
  }> | null;
  chart?: {
    kind?: 'bar' | 'line' | string;
    x_label?: string | null;
    y_label?: string | null;
    points?: StrategicContextDescriptorChartPoint[];
  } | null;
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

export interface StrategicContextArtifactViewablePreview {
  url: string;
  kind: 'image' | 'video' | string;
  filename?: string | null;
  /** Stable gs:// ref for lazy re-resolution when the baked signed URL expires. */
  storage_uri?: string | null;
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
  has_logs?: boolean | null;
  storage_uri?: string | null;
  viewable_previews?: StrategicContextArtifactViewablePreview[] | null;
}

export interface StrategicContextEvidenceCollection<T> {
  total: number;
  items: T[];
}

export interface StrategicContextPackageDescriptor {
  id: string;
  title: string;
  label?: string | null;
  summary?: string | null;
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
    knowledge?: StrategicContextEvidenceCollection<StrategicContextKnowledgeEvidence> & {
      pending_count?: number;
      oldest_pending_age_hours?: number | null;
    };
    artifacts?: StrategicContextEvidenceCollection<StrategicContextArtifactEvidence>;
  } | null;
  descriptors?: StrategicContextRenderableDescriptor[];
  drilldowns?: StrategicContextPackageDrilldown[];
}

export interface StrategicLocalRunnerRuntime {
  provider?: string | null;
  model?: string | null;
  account_id?: string | null;
  vm_instance_id?: string | null;
  runner_target_id?: string | null;
}

export interface StrategicLocalRunnerContinuation {
  enabled: boolean;
  fresh: boolean;
  preferred_runner_id?: string | null;
  reason?: string | null;
  updated_at?: string | null;
  last_seen_at?: string | null;
  expires_at?: string | null;
  owner_user_id?: string | null;
  owner_session_started_at?: string | null;
  owned_by_current_operator?: boolean | null;
  ownership?: 'none' | 'unknown' | 'owned' | 'current_operator' | 'other_operator' | string;
  ttl_seconds?: number | null;
}

export interface StrategicLocalRunnerCandidate {
  id: string;
  hostname?: string | null;
  status?: string | null;
  last_seen_at?: string | null;
  created_at?: string | null;
  capabilities?: {
    runner_preset?: string | null;
    workload?: string | null;
    task_types?: string[] | 'all';
    has_gpu?: boolean;
    max_concurrent?: number;
    agent_available?: boolean;
  };
  health?: {
    device_status?: string | null;
    device_status_reason?: string | null;
    is_live?: boolean;
    seconds_since_last_seen?: number | null;
    active_task_count?: number;
    active_compute_job_count?: number;
    active_work_count?: number;
    max_concurrent?: number | null;
    available_slots?: number | null;
    stale_reason?: string | null;
    agent_available?: boolean | null;
    liveness_window_seconds?: number;
  };
  handles_active_task_type: boolean;
  eligible_for_active_task: boolean;
  is_preferred: boolean;
  is_active_runner: boolean;
  blocked_reason?: string | null;
}

export interface StrategicLocalRunnerState {
  status?: string | null;
  location?: string | null;
  status_reason?: string | null;
  continuation?: StrategicLocalRunnerContinuation | null;
  mode: 'local' | 'local_ready' | 'cloud' | 'pending_local' | 'unknown' | string;
  strategic_runner_count: number;
  live_runner_count: number;
  eligible_live_count: number;
  stale_runner_count: number;
  busy_runner_count: number;
  agent_unavailable_count: number;
  active_runner_id?: string | null;
  active_task_pinned: boolean;
  active_task_requires_local?: boolean;
  active_task_awaiting_local_runner_since?: string | null;
  active_task_runtime?: StrategicLocalRunnerRuntime | null;
  runners?: StrategicLocalRunnerCandidate[];
  fallback_reason?:
    | 'no_runner'
    | 'runner_offline'
    | 'agent_unavailable'
    | 'at_capacity'
    | 'cloud_dispatch'
    | 'cloud_dispatch_pending'
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

export interface StrategicRuntimeLocalRunnerHoldPreview extends StrategicStateTask {
  awaiting_since?: string | null;
  blocked_reason?: string | null;
  park_reason?: string | null;
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
  local_runner_holds?: {
    count: number;
    compute_count?: number;
    strategic_count?: number;
    items: StrategicRuntimeLocalRunnerHoldPreview[];
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
  preferred_runner_id?: string | null;
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
  provenance?: {
    surface: string;
    path: string;
    status?: string | null;
    transform?: string | null;
    window_minutes?: number | null;
    [key: string]: unknown;
  };
}

export interface StrategicWorkSessionAction {
  id: string;
  kind: 'reply' | 'review' | 'approve' | 'inspect' | 'resume' | 'open' | string;
  label: string;
  reason: string;
  priority: 'critical' | 'high' | 'normal' | 'low' | string;
  source: 'attention' | 'runtime' | 'message' | 'task' | 'context' | 'session' | string;
  target?: StrategicContextPackageDrilldown['target'] | null;
  enabled: boolean;
  blocked_reason?: string | null;
}

export interface StrategicWorkSessionCommandMutation {
  method: 'GET' | 'POST' | 'PATCH' | 'PUT' | 'DELETE' | string;
  path: string;
  body?: Record<string, unknown>;
}

export interface StrategicWorkSessionCommand {
  id: string;
  label: string;
  group: string;
  priority: 'critical' | 'high' | 'normal' | 'low' | string;
  enabled: boolean;
  target?: StrategicContextPackageDrilldown['target'] | null;
  refetch_surfaces?: string[];
  executable?: boolean;
  mutation?: StrategicWorkSessionCommandMutation;
}

export interface StrategicWorkSessionCommandExecutionReceipt {
  id: string;
  command_id?: string | null;
  label?: string | null;
  status: 'succeeded' | 'failed' | 'duplicate' | string;
  summary?: string | null;
  created_at?: string | null;
  completed_at?: string | null;
  target?: StrategicContextPackageDrilldown['target'] | null;
  refetch_surfaces?: string[];
  result?: Record<string, unknown> | null;
}

export interface StrategicWorkSessionCommandJobFeed {
  version: 1;
  limit: number;
  has_more: boolean;
  items: StrategicWorkSessionCommandExecutionReceipt[];
}

export interface StrategicWorkSessionRefDescriptor {
  kind: string;
  id: string;
  label: string;
  summary?: string | null;
  status?: string | null;
  target?: StrategicContextPackageDrilldown['target'] | null;
}

export interface StrategicWorkSessionLastCheckIn {
  message_id: string;
  created_at?: string | null;
  content?: string | null;
  kind?: string | null;
}

export interface StrategicWorkSessionSinceLastCheckIn {
  version: 1;
  available: boolean;
  reason?: string | null;
  since?: string | null;
  until?: string | null;
  messages?: {
    total: number;
    operator?: number;
    strategic?: number;
    needs_operator?: number;
    watch?: number;
  };
  tasks?: {
    created: number;
    updated: number;
    completed?: number;
    failed?: number;
    blocked?: number;
    parked?: number;
  };
  alerts?: {
    raised: number;
    active?: number;
    errors?: number;
    warnings?: number;
  };
  strategic?: {
    cycles_completed: number;
    latest_cycle?: StrategicStateCycle | null;
  };
}

export interface StrategicWorkSessionEvent extends WorkbenchEventEnvelopeV1 {
  id: string;
  occurred_at?: string | null;
  source: string;
  title: string;
  summary?: string | null;
  tone?: 'neutral' | 'watch' | 'warning' | 'critical' | 'active' | string;
  ref?: {
    kind: string;
    id: string;
  } | null;
  target?: StrategicContextPackageDrilldown['target'] | null;
  data?: Record<string, unknown>;
}

export interface StrategicWorkSessionEventFeed {
  version: 1;
  limit: number;
  has_more: boolean;
  items: StrategicWorkSessionEvent[];
}

export interface StrategicWorkSession {
  version: 1;
  id: string;
  mode: string;
  active_surface: string;
  status?: string;
  location?: string;
  status_reason?: string | null;
  session_started_at?: string | null;
  last_interaction_at?: string | null;
  context_package_id?: string | null;
  attention_level?: string | null;
  selected_refs: StrategicWorkbenchState['selected_refs'];
  selected_ref_descriptors: StrategicWorkSessionRefDescriptor[];
  curated_metrics: StrategicWorkSessionMetric[];
  context_sources?: StrategicContextPackageSource[];
  actions?: StrategicWorkSessionAction[];
  commands?: StrategicWorkSessionCommand[];
  jobs?: StrategicWorkSessionCommandJobFeed;
  events?: StrategicWorkSessionEventFeed;
  last_check_in?: StrategicWorkSessionLastCheckIn | null;
  since_last_check_in?: StrategicWorkSessionSinceLastCheckIn | null;
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
    runtime?: StrategicLocalRunnerState | null;
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
  check_in?: {
    status: 'created' | 'duplicate' | string;
    message_id: string;
  };
}

export interface StrategicSessionMessageCreate {
  content: string;
  message_type?: string;
  context?: Record<string, unknown>;
  workbench_context?: Record<string, unknown>;
  idempotency_key?: string;
}

export interface StrategicSessionCheckInCreate {
  content?: string | null;
  kind?: string;
  workbench_context?: Record<string, unknown>;
  idempotency_key?: string;
}

export interface StrategicSessionCommandExecute {
  idempotency_key: string;
  observed_at?: number | null;
}

export interface StrategicSessionCommandExecutionResult {
  status: 'succeeded' | 'duplicate' | string;
  applied: boolean;
  command_id: string;
  receipt: StrategicWorkSessionCommandExecutionReceipt;
}

export interface StrategicSessionCommandExecutionResponse extends StrategicSessionResponse {
  command: StrategicSessionCommandExecutionResult;
}

export interface StrategicSessionMessageResponse {
  id: string;
  status: 'created' | 'duplicate' | string;
  project_id?: string;
  session_status?: string;
  location?: string;
  status_reason?: string | null;
  session?: StrategicSessionResponse['session'];
  workbench?: StrategicWorkbenchState | null;
  strategic?: StrategicStateResponse['strategic'];
  runtime?: StrategicStateResponse['runtime'];
  context_package?: StrategicContextPackageDescriptor | null;
  conversation?: StrategicSessionConversation;
  recent_outputs?: StrategicSessionOutput[];
  arming?: string | null;
  message?: StrategicStateMessage;
}

/**
 * Bounded projection of the resident Strategic session state returned by the
 * start/stop endpoints (mirrors `resident_session_snapshot` in
 * `strategic_resident_session.py`). Null when no resident session exists.
 */
export interface StrategicResidentSessionSnapshot {
  session_id?: string | null;
  task_id?: string | null;
  runner_id?: string | null;
  status?: string | null;
  stop_requested?: boolean;
  started_at?: string | null;
  updated_at?: string | null;
  ended_at?: string | null;
  last_progress_at?: string | null;
  last_operator_interaction_at?: string | null;
}

/**
 * Legible Workday resident-session availability (mirrors
 * `_resident_session_availability` in `strategic_state_read_model.py`).
 * `state` maps to the operator status chip; `reason` is present whenever the
 * session can't run (`sessions_disabled` / `no_runner` / `runner_offline` /
 * `start_failed: <msg>`). `runner_offline` is the mid-session block-on-offline
 * verdict: the pinned device went offline and the operator must restart to pick
 * another (no auto-migration).
 */
export interface StrategicResidentSessionAvailability {
  available: boolean;
  state: 'off' | 'ready' | 'starting' | 'active' | 'blocked' | 'error' | string;
  reason?: string | null;
}

/**
 * One device-picker option for the resident (Workday) session (mirrors an entry
 * of `build_resident_runner_options` in `strategic_resident_session.py`).
 * `online` = active + recent heartbeat; `agent_ready` = the agent can run;
 * `eligible` = online + agent-ready + can handle strategic work (the full
 * auto-pick gate). The pinned device rides on `resident_session.runner_id`.
 */
export interface StrategicMemoryNote {
  note: string;
  set_at: string;
}

export interface StrategicResidentRunnerOption {
  id: string;
  name: string;
  hostname?: string | null;
  online: boolean;
  agent_ready: boolean;
  eligible: boolean;
}

export interface StrategicSessionStartRequest {
  runner_id?: string | null;
}

export interface StrategicSessionStartResponse {
  project_id: string;
  enabled: boolean;
  session: StrategicResidentSessionSnapshot | null;
}

export interface StrategicSessionStopResponse {
  project_id: string;
  session: StrategicResidentSessionSnapshot | null;
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
  movement_explanation?: string | null;
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
  resident_session?: StrategicResidentSessionSnapshot | null;
  /**
   * Whether the resident-session feature flag (`strategic_session_enabled`) is
   * on. The container gates the Start affordance on this so the button is hidden
   * when the feature is off (a click would otherwise 403 at the start endpoint).
   */
  resident_session_available?: boolean;
  /**
   * Legible Workday resident-session state + the reason it isn't running
   * (mirrors `_resident_session_availability`). Workday is the on-switch:
   * entering `continuous` auto-starts the session, so the UI reads this to show
   * dark / ready / active / blocked / error instead of relying on a Start click.
   */
  resident_session_availability?: StrategicResidentSessionAvailability | null;
  /**
   * Device-picker options for the resident (Workday) session: every enrolled
   * runner with online / agent-ready / eligible flags so the UI can render the
   * auto-pick + override dropdown. The currently pinned device is
   * `resident_session.runner_id`.
   */
  resident_session_runners?: StrategicResidentRunnerOption[] | null;
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
      cancelled?: number;
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
    has_more?: boolean;
    limits?: {
      items?: number;
      message_blocker_preview?: number;
      message_question_preview?: number;
      message_watch_preview?: number;
      stale_task_preview?: number;
      alert_error_preview?: number;
      alert_warning_preview?: number;
    };
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
      local_runner_holds?: number;
      compute_local_runner_holds?: number;
      strategic_local_runner_holds?: number;
      stale_runner_pins?: number;
      stale_provisioning_tasks?: number;
      stale_queued_tasks?: number;
      manual_directions: number;
      pending_confirmations?: number;
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
  /** Opaque revision token for the strategic-state payload. Send it back as
   *  ?state_revision= on the next poll so the server can skip the expensive
   *  context-package build if inputs haven't changed. */
  state_revision?: string | null;
  /** Client-only field: NOT sent by the server in the body. Populated by
   *  {@link fetchStrategicStateWithRevision} from the `ETag` response header,
   *  which covers the FULL response body. Round-tripped as the `If-None-Match`
   *  request header on the next poll so the server can reply 304 Not Modified
   *  (empty body) when nothing changed. */
  state_etag?: string | null;
  model_integrity?: {
    orphaned_gaps: Array<{ id: string; name: string; status: string; recovery_hint?: string }>;
    ungrounded_directions: Array<{
      id: string;
      text: string;
      gap_id: string | null;
      reason: 'no_gap_id' | 'gap_not_active' | string;
      recovery_hint?: string;
    }>;
    stale_active_moves: Array<{
      gap_id: string;
      gap_name: string;
      stale_direction_ids: string[];
      recovery_hint?: string;
    }>;
    criterion_link_conflicts?: Array<{
      direction_id: string;
      direction_text: string;
      criterion_key: string;
      classification: 'inactive' | 'dangling' | string;
      recovery_hint?: string;
    }>;
    counts: {
      orphaned_gaps: number;
      ungrounded_directions: number;
      stale_active_moves: number;
      criterion_link_conflicts?: number;
      total: number;
    };
  } | null;
  /** Durable behavioral lessons and operator preferences accumulated by the
   *  resident Strategic session. Each entry is a {note, set_at} pair.
   *  Written by the set_memory strategic op. */
  strategic_memory_notes?: StrategicMemoryNote[];
}

/**
 * Lightweight mode + resident-session liveness probe (mirrors the backend
 * `GET /projects/{id}/strategic-liveness` endpoint). Carries ONLY the cheap
 * fields the chat/workday shell needs to render the mode indicator and the
 * resident-session liveness chip immediately on reload, computed without any of
 * the heavy `strategic-state` Phase-1/Phase-2 assembly. The full read model
 * ({@link fetchStrategicState}) still hydrates the complete state afterward and
 * takes over once it lands.
 */
export interface StrategicLivenessResponse {
  project_id: string;
  interaction_mode: StrategicWorkbenchState['interaction_mode'];
  resident_session?: StrategicResidentSessionSnapshot | null;
  resident_session_available?: boolean;
  resident_session_availability?: StrategicResidentSessionAvailability | null;
  resident_session_runners?: StrategicResidentRunnerOption[] | null;
}

export async function fetchStrategicLiveness(
  projectId: string,
  signal?: AbortSignal,
): Promise<StrategicLivenessResponse> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/strategic-liveness`,
    undefined,
    2,
    30000,
    signal,
  );
  if (!res.ok) throw new Error(`Failed to fetch strategic liveness: ${res.status}`);
  return res.json() as Promise<StrategicLivenessResponse>;
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
    // Bypass apiFetch's in-flight GET dedup: a post-mutation refresh must never
    // coalesce onto an older, in-flight pre-mutation request (which would
    // resolve with stale data). useStrategicReadModel sequences the results.
    false,
  );
  if (!res.ok) throw new Error(`Failed to fetch strategic state: ${res.status}`);
  return res.json() as Promise<StrategicStateResponse>;
}

/** Sentinel returned by {@link fetchStrategicStateWithRevision} when the server
 *  replies HTTP 304 Not Modified (the held revision/ETag still matches). Callers
 *  must keep their existing cached state and revision unchanged — the 304 body
 *  is empty and must NOT be parsed. */
export const STRATEGIC_STATE_NOT_MODIFIED = { notModified: true } as const;
export type StrategicStateNotModified = typeof STRATEGIC_STATE_NOT_MODIFIED;

/** Type guard so callers can narrow the fetch result and TypeScript can refine
 *  the non-sentinel branch to {@link StrategicStateResponse}. */
export function isStrategicStateNotModified(
  result: StrategicStateResponse | StrategicStateNotModified,
): result is StrategicStateNotModified {
  return result === STRATEGIC_STATE_NOT_MODIFIED;
}

/** Fetch strategic state with the held revision tokens for two server-side
 *  short-circuits:
 *   - `?state_revision=` (narrow token) lets the server skip the expensive
 *     context-package build when its inputs are unchanged.
 *   - `If-None-Match` (full-body ETag) lets the server reply 304 Not Modified
 *     with an empty body when NOTHING in the response changed, eliminating the
 *     redundant full-body transmission on an unchanged poll.
 *  Returns {@link STRATEGIC_STATE_NOT_MODIFIED} on a 304 (does not read the
 *  empty body); otherwise returns the parsed state with `state_etag` populated
 *  from the response ETag header for the next poll. */
export async function fetchStrategicStateWithRevision(
  projectId: string,
  stateRevision: string | null,
  signal?: AbortSignal,
  etag?: string | null,
): Promise<StrategicStateResponse | StrategicStateNotModified> {
  const params = stateRevision ? `?state_revision=${encodeURIComponent(stateRevision)}` : '';
  const options: RequestInit | undefined = etag
    ? { headers: { 'If-None-Match': etag } }
    : undefined;
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/strategic-state${params}`,
    options,
    2,
    30000,
    signal,
    false,
  );
  // 304 Not Modified: empty body — signal the caller to keep its cached state.
  if (res.status === 304) return STRATEGIC_STATE_NOT_MODIFIED;
  if (!res.ok) throw new Error(`Failed to fetch strategic state: ${res.status}`);
  const body = (await res.json()) as StrategicStateResponse;
  const responseEtag = res.headers.get('ETag');
  if (responseEtag) body.state_etag = responseEtag;
  return body;
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
  const res = await apiFetch(`/api/admin/projects/${encodeURIComponent(projectId)}/strategic-session`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ workbench }),
  });
  if (!res.ok) throw new Error(`Failed to update Strategic workbench state: ${res.status}`);
  const data = await res.json() as StrategicSessionResponse | StrategicWorkbenchStateResponse;
  if (!data.workbench) {
    throw new Error('Failed to update Strategic workbench state: missing workbench in response');
  }
  const result: StrategicWorkbenchStateResponse = { workbench: data.workbench };
  if (typeof data.arming === 'string') result.arming = data.arming;
  if (!('session' in data) && typeof data.status === 'string') result.status = data.status;
  return result;
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

export async function fetchStrategicConversation(
  projectId: string,
  includeMessageId?: string | null,
  signal?: AbortSignal,
): Promise<{ conversation: StrategicSessionConversation }> {
  const params = new URLSearchParams();
  if (includeMessageId) params.set('include_message_id', includeMessageId);
  const query = params.toString();
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/strategic-conversation${query ? `?${query}` : ''}`,
    undefined,
    2,
    30000,
    signal,
  );
  if (!res.ok) throw new Error(`Failed to fetch Strategic conversation: ${res.status}`);
  return res.json() as Promise<{ conversation: StrategicSessionConversation }>;
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

export async function checkInStrategicSession(
  projectId: string,
  payload: StrategicSessionCheckInCreate = {},
): Promise<StrategicSessionResponse> {
  const res = await apiFetch(`/api/admin/projects/${encodeURIComponent(projectId)}/strategic-session/check-in`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  if (!res.ok) throw new Error(`Failed to check in Strategic session: ${res.status}`);
  return res.json() as Promise<StrategicSessionResponse>;
}

async function strategicSessionErrorMessage(res: Response, fallback: string): Promise<string> {
  try {
    const data = await res.json() as { detail?: unknown; error?: { message?: unknown }; message?: unknown };
    if (typeof data.detail === 'string' && data.detail.trim()) return data.detail.trim();
    if (typeof data.error?.message === 'string' && data.error.message.trim()) return data.error.message.trim();
    if (typeof data.message === 'string' && data.message.trim()) return data.message.trim();
  } catch {
    // Use the fallback below.
  }
  return fallback;
}

export async function executeStrategicWorkSessionCommand(
  projectId: string,
  commandId: string,
  payload: StrategicSessionCommandExecute,
): Promise<StrategicSessionCommandExecutionResponse> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/strategic-session/commands/${encodeURIComponent(commandId)}/execute`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    },
  );
  if (!res.ok) {
    const detail = await strategicSessionErrorMessage(
      res,
      `Failed to execute Strategic session command: ${res.status}`,
    );
    throw new Error(detail);
  }
  const data = await res.json() as StrategicSessionCommandExecutionResponse;
  if (!data.command?.receipt?.id) {
    throw new Error('Failed to execute Strategic session command: missing command receipt');
  }
  return data;
}

export async function startStrategicSession(
  projectId: string,
  payload: StrategicSessionStartRequest = {},
): Promise<StrategicSessionStartResponse> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/strategic-session/start`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    },
  );
  if (!res.ok) {
    const detail = await strategicSessionErrorMessage(
      res,
      `Failed to start Strategic session: ${res.status}`,
    );
    throw new Error(detail);
  }
  return res.json() as Promise<StrategicSessionStartResponse>;
}

export async function stopStrategicSession(
  projectId: string,
): Promise<StrategicSessionStopResponse> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/strategic-session/stop`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    },
  );
  if (!res.ok) {
    const detail = await strategicSessionErrorMessage(
      res,
      `Failed to stop Strategic session: ${res.status}`,
    );
    throw new Error(detail);
  }
  return res.json() as Promise<StrategicSessionStopResponse>;
}

export interface MessageAttachmentUploadUrlResponse {
  upload_url: string;
  download_url: string;
  storage_uri: string;
  attachment_id: string;
}

export async function getMessageAttachmentUploadUrl(
  projectId: string,
  filename: string,
  contentType?: string,
): Promise<MessageAttachmentUploadUrlResponse> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/messages/attachment-upload-url`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ filename, content_type: contentType }),
    },
  );
  if (!res.ok) {
    const detail = await res.json().catch(() => ({}));
    throw new Error((detail as { detail?: string }).detail || `Failed to get upload URL: ${res.status}`);
  }
  return res.json() as Promise<MessageAttachmentUploadUrlResponse>;
}
