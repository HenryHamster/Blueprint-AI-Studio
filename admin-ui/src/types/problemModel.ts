// Shape of the strategic layer's problem_model — mirrors the Pydantic
// schema in backend/app/problem_model.py. Kept free-form where the backend
// schema uses string literals so the UI can render future additions
// without a type-error wall.

export type SourceType =
  | 'user_message'
  | 'friction_promotion'
  | 'agent_observation'
  | 'self_reflection'
  | 'shipped_change'
  | 'direction_outcome';

export interface Source {
  type: SourceType;
  message_id?: string | null;
  excerpt?: string | null;
  friction_id?: string | null;
  direction_id?: string | null;
  pr_number?: number | null;
}

export interface Intent {
  purpose: string;
  current_focus: string;
  user_purpose?: string | null;
  source?: Source | null;
}

export interface ProjectContext {
  project_type?: string | null;
  users?: string | null;
  team?: string | null;
  deploy_cadence?: string | null;
  notes?: string | null;
}

export type CapabilityStatus = 'working' | 'partial' | 'planned';
export type CapabilityProofStatus = 'proven' | 'path_proven' | 'unproven';

export interface CapabilityProofRecord {
  task_id: string;
  proved_at: string;
  proof_kind: string;
  proof_ref: string;
  commit_sha: string;
}

export interface EvidenceRef {
  kind: string;
  ref: string;
  proven: boolean;
  source_id?: string | null;
  recorded_at?: string | null;
}

export interface Capability {
  name: string;
  status: CapabilityStatus;
  notes?: string | null;
  source?: Source | null;
  proof_status?: CapabilityProofStatus | null;
  proof_records?: CapabilityProofRecord[] | null;
  evidence_refs?: EvidenceRef[] | null;
  backing_paths?: string[] | null;
  proof_exempt?: boolean | null;
}

export type GapSize = 'small' | 'medium' | 'large';
export type GapPriority = 'high' | 'medium' | 'low';
export type GapStatus = 'active' | 'closed' | 'abandoned' | 'blocked';

export interface GapHistoryEvent {
  ts: string;
  event: string;
  note?: string | null;
}

export interface Gap {
  id: string;
  name: string;
  description?: string;
  evidence?: string;
  size: GapSize;
  priority: GapPriority;
  status: GapStatus;
  opened?: string;
  closed?: string | null;
  tag?: string | null;
  source?: Source | null;
  active_moves: string[];
  past_moves: string[];
  history: GapHistoryEvent[];
}

export type FrictionStatus = 'active' | 'mitigated' | 'resolved';

export interface Friction {
  id: string;
  name: string;
  description?: string;
  seen_in: string[];
  status: FrictionStatus;
  mitigations_tried: string[];
  source?: Source | null;
  promoted_to_gap_id?: string | null;
}

export type PendingConfirmationType =
  | 'credentials_needed'
  | 'access_needed'
  | 'intent_change'
  | 'major_decision';

export interface PendingConfirmation {
  id: string;
  type: PendingConfirmationType;
  proposal: string;
  rationale?: string;
  created_at?: string;
}

export interface RevisionLogEntry {
  revision: number;
  ts: string;
  summary?: string;
  changes?: Record<string, unknown>;
}

export interface ProblemModelSizeHealth {
  active_gap_count: number;
  capability_count: number;
  active_friction_count: number;
  estimated_token_footprint: number;
  serialized_bytes: number;
  model_size_status: 'healthy' | 'growing' | 'large' | 'oversized';
}

export interface ProblemModel {
  revision?: number;
  last_updated?: string | null;
  last_updated_by?: 'agent' | 'user' | null;
  intent?: Intent;
  context?: ProjectContext;
  constraints?: string[];
  capabilities?: Capability[];
  gaps?: Gap[];
  archived_gaps?: Gap[];
  friction?: Friction[];
  pending_user_confirmation?: PendingConfirmation[];
  revision_log?: RevisionLogEntry[];
  capability_proof_records?: Record<string, CapabilityProofRecord[]> | null;
  _size_health?: ProblemModelSizeHealth;
}

// Running update — a single markdown document the strategic agent rewrites
// each cycle as the project's current-state snapshot. Acknowledge stamps a
// read-marker; the document itself persists and is refined next cycle.

// Summary of operator-wins running_update reconciliations: how many times an
// operator's mid-cycle edit superseded the agent's refreshed document, and
// when the most recent one happened. Null on the payload when no such
// reconciliation has occurred. Mirrors
// running_update_merge.summarize_running_update_reconciliation.
export interface RunningUpdateReconciliation {
  operator_wins_count: number;
  last_operator_win_at: string | null;
}

// Self-reflection log — one entry per cycle on self-repo projects.
export interface SelfReflectionEntry {
  ts: string;
  cycle_task_id?: string | null;
  what_was_hard?: string;
  missing_context?: string;
  proposed_self_improvement?: string;
}

// Subset of problem_model fields the user is allowed to edit via PATCH.
export interface ProblemModelPatch {
  intent?: Partial<Intent>;
  context?: Partial<ProjectContext>;
  constraints?: string[];
}

// Shape of what the Directions endpoint returns (plus the legacy fields
// it has always returned).
export interface DirectionsEndpointResponse {
  directions: Array<Record<string, unknown>>;
  autonomous_enabled: boolean;
  vision: Record<string, unknown>;
  design_doc: Record<string, unknown>;
  problem_model: ProblemModel;
  problem_model_size_health?: ProblemModelSizeHealth | null;
  running_update: string | null;
  running_update_acknowledged_at: string | null;
  running_update_reconciliation: RunningUpdateReconciliation | null;
  self_reflection_log: SelfReflectionEntry[];
  is_self_repo: boolean;
}
