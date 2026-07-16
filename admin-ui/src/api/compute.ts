import { apiFetch } from './core';
import type { WarmStartOriginBreakdown } from '../hooks/useEfficiencySnapshot';

export type ComputeSuccessOperator = '>=' | '<=' | '>' | '<' | '==' | '!=';

/** A cheap, machine-checkable acceptance criterion for a compute_experiment.
 * Mirrors backend `ComputeSuccessCriterion` ({metric, operator, threshold}). */
export interface ComputeSuccessCriterion {
  metric: string;
  operator: ComputeSuccessOperator;
  threshold: number;
}

/**
 * Operator-supplied launch contract for the dedicated compute_experiment path
 * (POST /api/admin/tasks/compute-experiment). The backend validates the launch
 * rules (non-empty command, positive spend cap + deadline) and the spend-approval
 * threshold before creating the task. Field names mirror the backend launch
 * contract / `ComputeExperimentPayload`; optional fields are omitted when blank.
 */
export interface ComputeExperimentLaunchRequest {
  github_repo: string;
  base_branch: string;
  title: string;
  prompt: string;
  command?: string;
  arms?: Array<{ name: string; command: string }>;
  declared_metric?: string;
  spend_cap_usd: number;
  deadline_seconds: number;
  accelerator?: string;
  machine_type?: string;
  cwd?: string;
  env?: Record<string, string>;
  experiment?: string;
  benchmark?: string;
  hypothesis?: string;
  metric?: string;
  success_criterion?: ComputeSuccessCriterion;
  require_live_release?: boolean;
  require_durable_artifact?: boolean;
  provisioning_model?: string;
  // Optional container image the experiment runs inside (e.g. nvcr.io/nvidia/...).
  // Required for CUDA/NGC stacks; absent ⇒ the command runs bare on the runner.
  container_image?: string;
  // Optional credentials for a private/gated registry. The self-hosted local
  // runner performs `docker login --password-stdin` with these before pulling.
  // The password is write-only — never returned from any GET.
  container_registry_auth?: {
    server?: string;
    username?: string;
    password?: string;
  };
  model?: string;
  effort?: string;
  // Provenance: the operator's original idea text, the LLM draft rationale, and
  // whether this contract came from a drafted refinement (vs. hand-typed).
  idea?: string;
  draft_notes?: string;
  drafted_from_refinement?: boolean;
  // Optional runner UUID or hostname to hard-pin this job to a specific runner.
  // When set, the job stays QUEUED until that runner is available rather than
  // falling back to another runner or cloud. Omit for auto-selection.
  runner_target_id?: string;
}

/** Response from the dedicated compute_experiment launch endpoint — the created
 * task. Kept loose so additive backend fields don't force a frontend change. */
export interface ComputeExperimentLaunchResponse {
  id: string;
  status?: string;
  requires_approval?: boolean;
  is_duplicate?: boolean;
  [key: string]: unknown;
}

/**
 * Launch a bounded compute_experiment through its dedicated contract endpoint.
 * Throws an Error carrying the backend message (error.message / detail) on a
 * non-2xx response so callers can surface a precise inline error.
 */
export async function createComputeExperiment(
  body: ComputeExperimentLaunchRequest,
): Promise<ComputeExperimentLaunchResponse> {
  const res = await apiFetch('/api/admin/tasks/compute-experiment', {
    method: 'POST',
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    const msg = data?.error?.message || data?.detail || `Error ${res.status}`;
    throw new Error(msg);
  }
  return res.json();
}

/** Overridable fields for re-running a compute_experiment from its result. */
export interface RerunComputeExperimentRequest {
  success_criterion?: ComputeSuccessCriterion;
  spend_cap_usd?: number;
  deadline_seconds?: number;
}

/**
 * Re-run a compute_experiment task, optionally overriding criterion/spend/deadline.
 * Creates a new task linked to the source via parent_task_id.
 */
export async function rerunComputeExperiment(
  taskId: string,
  body: RerunComputeExperimentRequest,
): Promise<ComputeExperimentLaunchResponse> {
  const res = await apiFetch(`/api/admin/tasks/${encodeURIComponent(taskId)}/rerun`, {
    method: 'POST',
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    const msg = (data?.detail as string | undefined) || `Error ${res.status}`;
    throw new Error(msg);
  }
  return res.json();
}

/** Response from POST /compute-jobs/{job_id}/ask — grounded answer to a free-form question. */
export interface ComputeJobAskResponse {
  answer: string | null;
  citations: string[];
  grounding: { checked: boolean; file_citations: number; grounded: number; unresolved: string[] };
  grounding_miss: boolean;
  fallback_reason: string | null;
}

/**
 * Ask a free-form question about a terminal compute job, grounded in that run's
 * durable evidence (metrics, artifacts, logs). POST /api/admin/compute-jobs/{id}/ask.
 */
export async function askComputeJob(
  jobId: string,
  question: string,
  history?: Array<{ question: string; answer: string }>,
): Promise<ComputeJobAskResponse> {
  const res = await apiFetch(`/api/admin/compute-jobs/${encodeURIComponent(jobId)}/ask`, {
    method: 'POST',
    body: JSON.stringify({ question, ...(history && history.length > 0 ? { history } : {}) }),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    const msg = data?.detail || `Error ${res.status}`;
    throw new Error(typeof msg === 'string' ? msg : `Error ${res.status}`);
  }
  return res.json();
}

/**
 * Request body for the compute-experiment preflight projection endpoint.
 * Mirrors `ComputeExperimentPreflightPayload` in backend/app/admin.py.
 */
export interface ComputeExperimentPreflightRequest {
  github_repo: string;
  accelerator?: string;
  machine_type?: string;
  spend_cap_usd: number;
  deadline_seconds: number;
  provisioning_model?: string;
}

/**
 * Response from the preflight endpoint — projected cost, rate, and hardware-policy verdict.
 * Kept loose/additive so backend-additive fields don't force a frontend change.
 */
export interface ComputeExperimentPreflightResponse {
  resolved_machine_type: string;
  rate_per_second: number | null;
  rate_per_hour: number | null;
  projected_cost_usd: number | null;
  max_runtime_seconds: number | null;
  effective_spend_cap_usd: number | null;
  effective_deadline_seconds: number | null;
  allowed_accelerator_types: string[] | null;
  allowed_machine_types: string[] | null;
  in_policy: boolean;
  reason: string | null;
  shape_rejection: string | null;
  [key: string]: unknown;
}

/**
 * Run a pre-launch cost/policy preflight check for a compute experiment.
 * Returns projected cost for the full deadline and max runtime under the spend cap,
 * plus a hardware-policy verdict for the selected accelerator/machine type.
 * Throws on non-2xx (caller should degrade gracefully on error, not block submission).
 */
export async function computeExperimentPreflight(
  body: ComputeExperimentPreflightRequest,
  signal?: AbortSignal,
): Promise<ComputeExperimentPreflightResponse> {
  const res = await apiFetch('/api/admin/tasks/compute-experiment/preflight', {
    method: 'POST',
    body: JSON.stringify(body),
  }, 0, 15000, signal);
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    const msg = data?.error?.message || data?.detail || `Error ${res.status}`;
    throw new Error(msg);
  }
  return res.json();
}

/** Request body for the compute-experiment idea-to-draft endpoint. */
export interface ComputeExperimentDraftRequest {
  idea: string;
  github_repo?: string;
  base_branch?: string;
  /** Optional prior draft for multi-turn refinement — the backend threads it into the LLM conversation. */
  prior_draft?: ComputeExperimentDraftResponse;
}

/**
 * Response from the draft endpoint. `draft_available` is false when the model
 * could not produce a usable contract; callers should fall back to manual entry.
 * All contract fields are optional — only populated when `draft_available` is true.
 * `notes` explains why a draft is unavailable or provides context on success.
 */
export interface ComputeExperimentDraftResponse {
  draft_available: boolean;
  notes?: string;
  title?: string;
  prompt?: string;
  hypothesis?: string;
  command?: string;
  arms?: Array<{ name: string; command: string }>;
  declared_metric?: string;
  spend_cap_usd?: number;
  deadline_seconds?: number;
  experiment?: string;
  benchmark?: string;
  metric?: string;
  success_criterion?: ComputeSuccessCriterion;
  accelerator?: string;
  machine_type?: string;
  [key: string]: unknown;
}

/**
 * Generate a draft compute-experiment launch contract from a plain-language idea.
 * POST /api/admin/tasks/compute-experiment/draft — 0 retries, 30 s timeout.
 * Throws on non-2xx; check `draft_available` before using contract fields.
 */
export async function draftComputeExperiment(
  body: ComputeExperimentDraftRequest,
  signal?: AbortSignal,
): Promise<ComputeExperimentDraftResponse> {
  const res = await apiFetch('/api/admin/tasks/compute-experiment/draft', {
    method: 'POST',
    body: JSON.stringify(body),
  }, 0, 30000, signal);
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    const msg = data?.error?.message || data?.detail || `Error ${res.status}`;
    throw new Error(msg);
  }
  return res.json();
}


export interface EfficiencyHistoryPoint {
  captured_at: string | null;
  value: number | null;
  def_version?: string | null;
}

export interface EfficiencyAxis {
  current: number | null;
  baseline: number | null;
  drift_pct: number | null;
  history: EfficiencyHistoryPoint[];
}

export interface EfficiencyDriftAxis {
  current: number | null;
  baseline: number | null;
  drift_pct: number | null;
  history?: EfficiencyHistoryPoint[];
}

export interface FormatComplianceBySchemaEntry {
  format_miss: number;
  repair_attempts: number;
  repair_successes: number;
  repair_infra_failures: number;
  repair_miss: number;
  repair_input_tokens: number;
  repair_output_tokens: number;
  self_repair_success_rate: number | null;
}

export interface FormatComplianceResponse {
  format_compliance_rate: EfficiencyDriftAxis;
  self_repair_success_rate: EfficiencyDriftAxis;
  tasks_measured: number | null;
  well_formed_tasks: number | null;
  format_error_total: number | null;
  repair_attempts: number | null;
  repair_successes: number | null;
  by_schema?: Record<string, FormatComplianceBySchemaEntry> | null;
}

export interface FormatComplianceByTypeEntry {
  tasks_measured: number | null;
  well_formed_tasks: number | null;
  format_compliance_rate: number | null;
  format_error_total: number | null;
  repair_attempts: number | null;
  repair_successes: number | null;
  self_repair_success_rate: number | null;
}

export interface AgentTimeoutRateResponse {
  overall: EfficiencyDriftAxis;
  by_subtype: Record<string, { current: number | null; baseline: number | null; drift_pct: number | null }>;
  dominant_subtype: string | null;
  dominant_share: number | null;
  denominator: number | null;
  denominator_definition: string | null;
  timeout_count: number | null;
  window_days: number | null;
}

// Per-project warm-pool payoff verdict: a single synthesized answer to "did
// enabling the pool produce a measured reuse lift WITHOUT a cost regression?",
// folded from the pool-origin warm-start share + dispatch-funnel hit rate (lift)
// and the idle-inclusive cost-per-merged-PR drift (no-regression). Optional so
// the panel degrades silently on an older backend that omits it.
export type PoolPayoffState =
  | 'PROVEN'
  | 'COST_REGRESSION'
  | 'NO_LIFT'
  | 'INSUFFICIENT_DATA';

export interface PoolPayoffVerdict {
  state: PoolPayoffState;
  supporting: {
    pool_origin_share: number | null;
    pool_warm_starts: number | null;
    pool_hit_rate: number | null;
    total_pool_dispatches: number | null;
    cost_drift_pct: number | null;
    cost_incl_idle_current: number | null;
    cost_baseline: number | null;
  };
  recommended_action: string;
}

export interface PanelReviewEffectiveness {
  challenged_confirmed: number;
  challenged_revised: number;
  challenged_rejected: number;
  total_challenged: number;
  overturn_rate: number | null;
  cost_per_challenge_usd?: number | null;
  challenged_escalated?: number;
  challenged_degraded?: number;
  sampled_out_count?: number;
  effective_coverage?: number | null;
  fail_open_rate?: number | null;
  gate_health?: {
    state: 'NO_DATA' | 'DEGRADED' | 'INSUFFICIENT' | 'LOW_COVERAGE' | 'PARTIAL' | 'HEALTHY';
    fail_open_rate: number | null;
    total_invocations: number;
    min_sample: number;
  };
}

export interface ChallengerSampleRateRecommendation {
  status: 'no_data' | 'insufficient_data' | 'gate_earning' | 'reduce';
  recommended_sample_rate: number | null;
  rationale: string;
}

export interface VmCostAttributionEntry {
  task_type: string;
  task_count: number;
  merged_count: number;
  total_cost_usd: number;
  share_of_total_cost: number | null;
  cost_per_merge: number | null;
}

export interface VmCostAttribution {
  by_task_type: VmCostAttributionEntry[];
  total_vm_usd: number;
}

export interface VmCostAttributionTrendEntry {
  captured_at: string | null;
  by_task_type: VmCostAttributionEntry[] | null;
  total_vm_usd: number | null;
}

export interface LlmCostByFailureClassEntry {
  failure_class: string;
  task_count: number;
  total_cost_usd: number;
}

/** Compact per-model row emitted by _compact_llm_cost_attribution (uses cost_usd, not total_cost_usd). */
export interface LlmCostCompactModelRow {
  provider: string;
  model: string;
  cost_usd: number;
}

/** Compact per-task-type row emitted by _compact_llm_cost_attribution. */
export interface LlmCostCompactTaskTypeRow {
  task_type: string;
  cost_usd: number;
  mean_input_tokens_per_call?: number | null;
}

export interface LlmCostAttribution {
  by_failure_class: LlmCostByFailureClassEntry[];
  total_llm_usd: number;
  /** Fraction of total LLM spend attributable to rework chains. Null when no chain data. */
  rework_share?: number | null;
  /** Fraction of total LLM tokens consumed by self-repair (format fix) loops. Null when no data. */
  self_repair_burn_share?: number | null;
  /** Per-(provider, model) compact cost breakdown. Absent on older snapshots. */
  by_model?: LlmCostCompactModelRow[] | null;
  /** Per-task-type compact cost breakdown. Absent on older snapshots. */
  by_task_type?: LlmCostCompactTaskTypeRow[] | null;
}

/** One entry in the day-over-day LLM cost attribution trend. */
export interface LlmCostAttributionTrendEntry {
  captured_at?: string | null;
  by_model?: LlmCostCompactModelRow[] | null;
  by_task_type?: LlmCostCompactTaskTypeRow[] | null;
  rework_share?: number | null;
  total_llm_usd?: number | null;
  self_repair_burn_share?: number | null;
  by_failure_class?: LlmCostByFailureClassEntry[] | null;
  merged_count?: number | null;
}

/** Per-scalar slot in a cost-attribution delta (baseline/recent/prior vs current). */
export interface LlmCostScalarDelta {
  baseline?: number | null;
  current?: number | null;
  abs_delta?: number | null;
  rel_pct?: number | null;
}

/** Per-scalar slot specific to the window delta (prior vs recent). */
export interface LlmCostWindowScalarDelta {
  prior?: number | null;
  recent?: number | null;
  abs_delta?: number | null;
  rel_pct?: number | null;
}

/** Per-model row in a cost-attribution delta. */
export interface LlmCostModelDeltaRow {
  provider: string;
  model: string;
  baseline_cost_usd?: number | null;
  current_cost_usd?: number | null;
  cost_delta_usd: number;
  baseline_share?: number | null;
  current_share?: number | null;
}

/** Per-model row in the trailing-window cost delta. */
export interface LlmCostModelWindowDeltaRow {
  provider: string;
  model: string;
  prior_cost_usd?: number | null;
  recent_cost_usd?: number | null;
  cost_delta_usd: number;
  prior_share?: number | null;
  recent_share?: number | null;
  share_delta?: number | null;
}

/** Per-task-type row in a cost-attribution delta. */
export interface LlmCostTaskTypeDeltaRow {
  task_type: string;
  baseline_cost_usd?: number | null;
  current_cost_usd?: number | null;
  cost_delta_usd: number;
  baseline_share?: number | null;
  current_share?: number | null;
  share_delta?: number | null;
}

/** Per-task-type row in the trailing-window cost delta. */
export interface LlmCostTaskTypeWindowDeltaRow {
  task_type: string;
  prior_cost_usd?: number | null;
  recent_cost_usd?: number | null;
  cost_delta_usd: number;
  prior_share?: number | null;
  recent_share?: number | null;
  share_delta?: number | null;
}

/** Genesis-baseline-vs-current LLM cost-mix delta. */
export interface LlmCostAttributionDelta {
  total_llm_usd?: LlmCostScalarDelta | null;
  rework_share?: LlmCostScalarDelta | null;
  self_repair_burn_share?: LlmCostScalarDelta | null;
  by_model_delta?: LlmCostModelDeltaRow[] | null;
  by_task_type_delta?: LlmCostTaskTypeDeltaRow[] | null;
}

/** Window metadata for the trailing-window cost delta. */
export interface LlmCostWindowMeta {
  recent_n: number;
  prior_count: number;
  recent_count: number;
  prior_start?: string | null;
  prior_end?: string | null;
  recent_start?: string | null;
  recent_end?: string | null;
}

/** Recent-vs-prior trailing-window LLM cost-mix delta. */
export interface LlmCostAttributionWindowDelta {
  total_llm_usd?: LlmCostWindowScalarDelta | null;
  rework_share?: LlmCostWindowScalarDelta | null;
  self_repair_burn_share?: LlmCostWindowScalarDelta | null;
  by_model_delta?: LlmCostModelWindowDeltaRow[] | null;
  by_task_type_delta?: LlmCostTaskTypeWindowDeltaRow[] | null;
  window_meta?: LlmCostWindowMeta | null;
}

export interface EfficiencyAxesResponse {
  axes: Record<string, EfficiencyAxis>;
  baseline_captured_at: string | null;
  last_snapshot_at: string | null;
  // Idle-inclusive cost-per-merged-PR as a companion trended axis (current +
  // history + a baseline sourced from the immutable pre-pool cost axis), so the
  // warm-reuse-vs-cost trend can chart true cost regression beside the headline
  // idle-exclusive cost line. Absent on older snapshots → optional.
  cost_per_merged_pr_incl_pool_idle_axis?: EfficiencyAxis;
  // Synthesized warm-pool payoff verdict (see PoolPayoffVerdict). Optional so
  // older backends that omit it degrade silently.
  pool_payoff_verdict?: PoolPayoffVerdict;
  // Per-origin warm-start breakdown (pool vs chain-continuity vs backlog-reuse)
  // + the pool-origin share, from the latest snapshot. Backend emits it at the
  // top level of the efficiency-axes response; surfaces render it beside the
  // contaminated aggregate vm_reuse_rate so the aggregate is never the sole
  // reuse number read. Optional/nullable — older backends omit it, and the
  // backend returns null when there were no warm starts to attribute.
  warm_start_origin_breakdown?: WarmStartOriginBreakdown | null;
  // Optional nested blocks surfaced by the backend for the operator dashboard.
  format_compliance?: FormatComplianceResponse;
  agent_timeout_rate?: AgentTimeoutRateResponse;
  // Panel review effectiveness: overturn_rate, challenged outcome counts, and
  // total gate invocations from the latest snapshot. Null when no gate
  // invocations exist in the window. Optional for older backends.
  panel_review_effectiveness?: PanelReviewEffectiveness | null;
  // Deploy-scoped companion: same metric restricted to tasks after the earliest
  // snapshot under the current serving SHA. Null on older backends or when the
  // deploy boundary is unknown (first snapshot under this build).
  panel_review_effectiveness_post_deploy?: PanelReviewEffectiveness | null;
  // Data-driven recommended sample_rate for the challenger gate, computed from
  // the latest snapshot's overturn_rate. Optional for older backends.
  panel_review_recommended_sample_rate?: ChallengerSampleRateRecommendation | null;
  // VM cost attribution: per-task-type breakdown of infrastructure spend from
  // the latest snapshot, showing which task types (review, create, side-car, etc.)
  // dominate VM spend. Optional for older backends.
  vm_cost_attribution?: VmCostAttribution | null;
  // Day-over-day VM cost trend (one entry per daily snapshot). Optional/null
  // for older backends that omit it.
  vm_cost_attribution_trend?: VmCostAttributionTrendEntry[] | null;
  // Per-task-type format-compliance breakdown from the latest snapshot. Keyed
  // by task type; each entry mirrors FormatComplianceByTypeEntry. Optional/null
  // for older snapshots.
  format_compliance_by_type?: Record<string, FormatComplianceByTypeEntry> | null;
  // LLM cost attribution from the latest snapshot: per-terminal-outcome
  // (MERGED vs each failure class) breakdown. Null on older snapshots.
  llm_cost_attribution?: LlmCostAttribution | null;
  // Day-over-day trend of LLM cost attribution headline scalars (oldest-first).
  // Optional/null for older backends that omit it.
  llm_cost_attribution_trend?: LlmCostAttributionTrendEntry[] | null;
  // Baseline-vs-current delta for LLM cost attribution (genesis baseline vs latest).
  // Complements llm_cost_attribution_window_delta. Null when either side is absent.
  llm_cost_attribution_baseline_delta?: LlmCostAttributionDelta | null;
  // Recent-vs-prior trailing-window cost-mix delta (most-recent-K vs prior-K snapshots).
  // Complements the genesis-baseline delta — distinguishes recent regression from
  // cumulative drift. Null when trend depth is insufficient or backend is older.
  llm_cost_attribution_window_delta?: LlmCostAttributionWindowDelta | null;
}

export async function fetchEfficiencyAxes(projectId: string): Promise<EfficiencyAxesResponse> {
  const res = await apiFetch(`/api/admin/projects/${encodeURIComponent(projectId)}/efficiency-axes`);
  if (!res.ok) throw new Error(`Failed to fetch efficiency axes: ${res.status}`);
  return res.json();
}

export interface LlmCostModelRow {
  provider: string;
  model: string;
  total_input_tokens: number;
  total_output_tokens: number;
  total_cost_usd: number;
  mean_cost_per_task: number;
  share_of_total_cost: number | null;
  task_count: number;
  llm_call_count: number;
  /** Count of tasks in this bucket that landed/merged on the integration branch. */
  merged_count: number;
  /** merged_count / task_count, or null when the bucket has no tasks. */
  merge_rate: number | null;
  /** Count of tasks in this bucket that landed and were verified (not fall-open degraded). */
  verified_merged_count: number;
  /** verified_merged_count / task_count, or null when the bucket has no tasks. */
  verified_merge_rate: number | null;
  priced: boolean;
  /** True when this (provider, model) is priced at a proxy rate (closest GA equivalent) rather than exact published pricing. */
  approximate_priced: boolean;
  /**
   * cost_usd / verified_merged_count, present only when the bucket is fully
   * priced AND has at least one verified merge. Null for unpriced (understated
   * cost) or zero-verified-merge buckets so the panel never shows a
   * misleading/divide-by-zero figure.
   */
  cost_per_merge: number | null;
}

export interface LlmCostTaskTypeRow {
  /** Task type from first_payload["type"], or "unspecified" when absent. */
  task_type: string;
  total_input_tokens: number;
  total_output_tokens: number;
  total_cost_usd: number;
  llm_call_count: number;
  task_count: number;
  merged_count: number;
  merge_rate: number | null;
  verified_merged_count: number;
  verified_merge_rate: number | null;
  /** cost_usd / verified_merged_count, null when cost is zero or no verified merges. */
  cost_per_merge: number | null;
  share_of_total_cost: number | null;
}

export interface ReworkPrEntry {
  pr_number: number;
  rework_llm_cost_usd: number;
  rework_vm_cost_usd: number;
}

/** Per-window LLM-dollar rework attribution: first-pass vs rework split + top rework PRs. */
export interface ReworkAttribution {
  /** Fraction of total LLM spend attributable to rework review-fix chains (null when no priced chain data). */
  rework_share: number | null;
  /** LLM spend on first-pass attempts within the window. */
  first_pass_llm_cost_usd: number;
  /** LLM spend on rework (retry / review-fix chain) attempts within the window. */
  rework_llm_cost_usd: number;
  /** Top PRs ranked by rework LLM spend (descending). */
  top_rework_prs: ReworkPrEntry[];
}

export interface LlmCostBreakdownResponse {
  window_days: number;
  generated_at: string;
  by_provider: Array<Omit<LlmCostModelRow, 'model'>>;
  by_model: LlmCostModelRow[];
  /** Per-task-type cost attribution — same pricing table as by_model, bucketed by first_payload["type"]. */
  by_task_type: LlmCostTaskTypeRow[];
  /** Rework attribution: first-pass vs rework LLM spend split and top rework PRs. Absent on older backends. */
  rework_attribution?: ReworkAttribution;
  totals: {
    total_input_tokens: number;
    total_output_tokens: number;
    total_cost_usd: number;
    task_count: number;
    merged_count: number;
    merge_rate: number | null;
    verified_merged_count: number;
    verified_merge_rate: number | null;
    priced_task_count: number;
    unpriced_task_count: number;
    unpriced_coverage_fraction: number | null;
    /** Tasks whose runtime selection fell back to a provider/model other than
     * the intended routing — their spend is folded into the resolved model's
     * bucket, so by-model attribution is not purely the configured routing. */
    fallback_task_count: number;
    /** Dollar spend attributable to the fallback tasks above. */
    fallback_cost_usd: number;
  };
  unpriced_combos: string[];
  /** (provider, model) combos priced at a proxy rate (no separately published API rate). */
  approximate_priced_combos: string[];
}

export async function fetchLlmCostBreakdown(
  projectId: string,
  windowDays = 7,
): Promise<LlmCostBreakdownResponse> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/llm-cost-breakdown?window_days=${windowDays}`,
  );
  if (!res.ok) throw new Error(`Failed to fetch LLM cost breakdown: ${res.status}`);
  return res.json();
}


export interface ComputeRpcDiagnostic {
  operation: string;
  error_type: string;
  error_message: string;
  started_at: string | null;
  failed_at: string | null;
  duration_ms: number;
  attempt_count: number;
  job_status: string;
  next_retry_at: string | null;
  retry_after_seconds: number | null;
  reason?: string | null;
}

export interface ComputeJobCommandLog {
  text: string;
  truncated: boolean;
  byte_count: number;
}

export interface ComputeJobLiveOutput {
  text: string;
  truncated: boolean;
}

/** One directly viewable output file (plot/render/video) from a compute job's
 *  artifacts, surfaced as the first-class `viewable_artifacts` field on the
 *  job response (normalized server-side from the job's `viewable_files`
 *  manifest). `storage_uri` is a durable `gs://` reference — resolve it to a
 *  transient signed URL via `resolveArtifactDownloadUrl` before rendering;
 *  signed URLs expire and must never be persisted. */
export interface ViewableArtifact {
  storage_uri: string;
  filename: string | null;
  content_type?: string | null;
  size_bytes?: number | null;
  kind: 'image' | 'video' | 'pdf' | null;
  revision?: string | null;
  live?: boolean | null;
}

export interface ComputeJob {
  id: string;
  project_id: string;
  task_id: string | null;
  backend: string;
  external_compute_id: string | null;
  machine_type: string | null;
  status: 'QUEUED' | 'STARTING' | 'RUNNING' | 'POLLING' | 'SUCCEEDED' | 'FAILED' | 'CANCELLED' | 'TIMED_OUT';
  terminal_reason: string | null;
  outcome: 'SUCCESS' | 'FAILURE' | 'CANCELLED' | 'TIMED_OUT' | 'SPEND_CAP_EXCEEDED' | null;
  spend_cap_usd: number | null;
  wall_clock_deadline: string | null;
  created_at: string;
  updated_at: string;
  started_at: string | null;
  completed_at: string | null;
  last_polled_at: string | null;
  accrued_cost_usd: number | null;
  final_cost_usd: number | null;
  result_reference: string | null;
  // Identifier of the durable COMPUTE_RESULT artifact the backend creates for
  // every terminal compute job. This — not result_reference — is the primary,
  // completion-defining outcome of a finished compute_experiment. Optional/null
  // on legacy or in-flight rows that predate artifact creation.
  artifact_id?: string | null;
  artifact?: {
    id: string;
    artifact_type: string;
    storage_uri: string | null;
    logical_key?: string | null;
    created_at?: string | null;
  } | null;
  accelerator_type: string | null;
  accelerator_count: number | null;
  // Phase the VM reached before going terminal, promoted by the admin endpoint:
  // 'provisioning' (never started — $0 cost, VM never reached RUNNING) vs
  // 'runtime' (failed during execution). Absent on agent-facing / legacy rows.
  vm_phase?: 'provisioning' | 'runtime' | null;
  // Echo of the requested launch contract, surfaced for provisioning-context
  // diagnostics (machine_type / accelerator_type are already top-level fields).
  // Present only on the admin endpoint; absent on legacy rows.
  launch_contract?: {
    requested_deadline_seconds?: number | null;
    provisioning_model?: string | null;
  } | null;
  polling_metadata?: Record<string, unknown>;
  // First-class exit_code and command_log promoted from polling_metadata.
  // Null when not yet available or not applicable.
  exit_code?: number | null;
  command_log?: ComputeJobCommandLog | null;
  // Bounded RPC failure diagnostic; null/absent when no backend RPC has failed
  // (or once it resolves). Older responses may carry it under polling_metadata.
  compute_rpc_diagnostic?: ComputeRpcDiagnostic | null;
  // Augmented by the admin list endpoint from the launching Task.
  task_title: string | null;
  task_type: string | null;
  // Declared success criterion from the task payload (metric/operator/threshold).
  success_criterion?: ComputeSuccessCriterion | null;
  // Evaluated verdict from task.plan_output.compute_result.criterion — present
  // only on terminal jobs whose task recorded a criterion evaluation.
  criterion_verdict?: {
    metric: string;
    operator: string;
    threshold: number;
    value: number | null;
    met: boolean;
    measurable: boolean;
    binding_status?: string | null;
    per_arm?: Record<string, number | null>;
  } | null;
  // Durable KnowledgeDocument auto-persisted on task completion for all
  // measured terminal jobs. Surfaced from task.plan_output.knowledge_document.id.
  knowledge_document_id?: string | null;
  // Compute experiment identity fields from the task launch payload.
  // Absent on legacy rows or non-compute-experiment tasks.
  experiment?: string | null;
  benchmark?: string | null;
  hypothesis?: string | null;
  // Per-arm criterion verdicts with apportioned costs; present when the task
  // recorded per-arm evaluation (multi-arm experiments with a success criterion).
  // Keyed by arm name. Absent on legacy rows or single-arm jobs.
  per_arm_verdicts?: Record<string, {
    value: number | null;
    met: boolean | null;
    cost_usd: number | null;
  }> | null;
  // Canonical earned-vs-claimed evidence tier, derived server-side by
  // knowledge_completion._compute_evidence_tier and served here as a first-class
  // field. Prefer this over any client re-derivation. Older/cached responses may
  // omit it (undefined), in which case consumers fall back to client derivation.
  evidence_tier?: 'covered' | 'attested' | 'backfilled' | 'partial' | 'uncovered' | null;
  // Runner identity fields — present when the job was dispatched to a registered
  // local runner. Absent on legacy rows or cloud-dispatched jobs. Older backends
  // may omit these fields entirely; consumers must treat absence as null.
  runner_target_id?: string | null;
  runner_claimed_at?: string | null;
  awaiting_local_runner_since?: string | null;
  runner?: {
    id: string;
    hostname: string | null;
    status: string | null;
  } | null;
  // Original launch configuration from the task payload, projected for cloning.
  // Absent on legacy rows where the task payload predates this field.
  launch_params?: {
    command?: string | null;
    // Multi-arm commands are stored base64-wrapped for escape safety. The
    // backend decodes them on read; null for single-arm/operator commands,
    // which are stored verbatim and need no decoding.
    command_decoded?: string | null;
    arms?: Array<{ name: string; command: string }> | null;
    declared_metric?: string | null;
    metric?: string | null;
    cwd?: string | null;
    env_keys?: string[] | null;
    accelerator?: string | null;
    machine_type?: string | null;
    container_image?: string | null;
    provisioning_model?: string | null;
    spend_cap_usd?: number | null;
    deadline_seconds?: number | null;
    require_live_release?: boolean | null;
    require_durable_artifact?: boolean | null;
    // Provenance persisted at launch time; absent on legacy or manually-configured launches.
    provenance?: {
      idea?: string | null;
      draft_notes?: string | null;
      drafted_from_refinement?: boolean | null;
    } | null;
  } | null;
  /** Per-file viewable-artifact manifest for artifacts produced by this compute
   *  job. Present only on jobs whose backend persisted viewable files (images,
   *  videos) as individually-addressable storage objects. Absent on legacy rows
   *  or jobs without viewable output. */
  viewable_artifacts?: ViewableArtifact[] | null;
  /** Job-level aggregate: number of viewable files omitted from
   *  `viewable_artifacts` for exceeding the 25-file count cap or the 8 MiB
   *  per-file size cap. Additive/backward-compatible — absent or 0 on legacy
   *  rows and jobs where nothing was omitted. */
  viewable_artifacts_dropped_count?: number | null;
  /** Bounded time-series of intermediate progress samples emitted during the
   *  job. Each sample has an ISO timestamp, a metric name, and a numeric value.
   *  Absent on legacy rows or jobs that did not emit progress. */
  progress_series?: Array<{ t: string; name: string; value: number }> | null;
  /** Stable terminal disposition marker set when a job ends. Distinguishes a
   *  no-progress watchdog cut ('no_progress') from a full wall-clock expiry
   *  ('deadline'), budget cap ('spend_cap'), operator kill ('operator'), and
   *  natural failure ('failure'). Absent on legacy rows. */
  terminal_kind?: 'deadline' | 'no_progress' | 'spend_cap' | 'operator' | 'failure' | null;
  /** True when the server-side no-progress watchdog is actively monitoring this
   *  job (i.e. a declared_metric was found and the metric-name bridge is intact).
   *  False when the watchdog exists but is inert. Absent on legacy rows or when
   *  the watchdog subsystem is not involved. */
  watchdog_active?: boolean | null;
  /** Effective no-progress timeout in seconds enforced for this job, derived
   *  from the per-job override if set, otherwise the global default. Null when
   *  the watchdog is not configured for this job. */
  no_progress_timeout_seconds?: number | null;
  /** ISO timestamp of the most recent progress sample recorded for this job.
   *  Null when no progress samples have been emitted yet. */
  last_progress_at?: string | null;
  /** Human-readable reason the watchdog is NOT protecting this job, e.g.
   *  'no_declared_metric' or 'metric_name_mismatch'. Null when watchdog_active
   *  is true or when the watchdog is not involved. */
  watchdog_inert_reason?: string | null;
  /** Cross-job checkpoint resume: predecessor job ID that the backend validated
   *  as the resume source for checkpoint hydration. Present when the compute job
   *  was launched with a checkpoint source; absent otherwise. Promoted from
   *  polling_metadata by the backend serialization layer. */
  input_artifact_job_id?: string | null;
  /** What actually happened at checkpoint hydration time. A dict with keys:
   *  `ok` (boolean), `resumed_from_job_id` (string), `file_count` (int),
   *  `recorded_at` (ISO timestamp), and optionally `reason` (string when !ok).
   *  Absent when no resume was attempted. Promoted from polling_metadata by the
   *  backend serialization layer. */
  resume_provenance?: Record<string, unknown> | null;
}

export interface ComputeJobsSummary {
  total_jobs: number;
  active_jobs: number;
  terminal_jobs: number;
  total_spend_usd: number;
  outcome_breakdown: Record<string, number>;
  criterion_measured: number;
  criterion_met: number;
}

export interface ComputeJobsListResponse {
  jobs: ComputeJob[];
  hasMore: boolean;
  summary: ComputeJobsSummary;
  limit: number;
  offset: number;
}

export interface HeldComputeExperiment {
  id: string;
  title: string;
  status: string;
  requires_approval: boolean;
  payloads?: Record<string, unknown>[];
  created_at: string;
}

export async function fetchHeldComputeExperiments(
  projectId: string,
  signal?: AbortSignal,
): Promise<HeldComputeExperiment[]> {
  const query = new URLSearchParams({
    task_type: 'compute_experiment',
    approval_needed: 'true',
    archived: 'false',
    project_id: projectId,
    limit: '20',
  });
  const res = await apiFetch(`/api/admin/tasks?${query.toString()}`, undefined, 2, 20000, signal);
  if (!res.ok) throw new Error(`Failed to fetch held compute experiments: ${res.status}`);
  const data = await res.json();
  return Array.isArray(data) ? data : (data.tasks ?? []);
}

/**
 * A compute_experiment task held QUEUED because project policy requires local
 * execution and no eligible runner is live. These tasks have NO ComputeJob row,
 * so this is the only surface that exposes them. Shape mirrors the backend
 * `_awaiting_local_runner_entry` contract in admin_compute_jobs.py.
 */
export interface AwaitingLocalRunnerHold {
  id: string;
  title: string | null;
  awaiting_local_runner_since: string;
  created_at: string | null;
  requires_approval: boolean;
  spend_cap_usd: number | null;
  command: string | null;
  machine_type: string | null;
  accelerator: string | null;
  container_image: string | null;
  provisioning_model: string | null;
  deadline_seconds: number | null;
  // Fields present when the job is pinned to a specific runner (forced target).
  // Absent on holds that are waiting for any available runner. Backend returns
  // these as top-level scalars plus a nested forced_runner descriptor.
  forced_runner_target_id?: string | null;
  awaiting_forced_runner_since?: string | null;
  forced_runner?: { id: string; hostname: string | null; status: string } | null;
}

export async function fetchAwaitingLocalRunnerHolds(
  projectId: string,
  signal?: AbortSignal,
): Promise<AwaitingLocalRunnerHold[]> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/compute-jobs/awaiting-local-runner`,
    undefined,
    2,
    20000,
    signal,
  );
  if (!res.ok) throw new Error(`Failed to fetch awaiting-local-runner holds: ${res.status}`);
  const data = await res.json();
  return Array.isArray(data) ? data : [];
}

export async function fetchComputeJobs(
  projectId: string,
  signal?: AbortSignal,
  includeFinishedMinutes?: number,
  offset = 0,
  taskId?: string,
): Promise<ComputeJobsListResponse> {
  const query = new URLSearchParams();
  if (typeof includeFinishedMinutes === 'number') {
    query.set('include_finished_minutes', String(includeFinishedMinutes));
  }
  if (offset > 0) query.set('offset', String(offset));
  if (taskId) query.set('task_id', taskId);
  const suffix = query.toString() ? `?${query.toString()}` : '';
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/compute-jobs${suffix}`,
    undefined,
    2,
    30000,
    signal,
  );
  if (!res.ok) throw new Error(`Failed to fetch compute jobs: ${res.status}`);
  const data = await res.json();
  const emptySummary: ComputeJobsSummary = {
    total_jobs: 0, active_jobs: 0, terminal_jobs: 0, total_spend_usd: 0,
    outcome_breakdown: {}, criterion_measured: 0, criterion_met: 0,
  };
  return {
    jobs: Array.isArray(data.compute_jobs) ? data.compute_jobs : [],
    hasMore: data.has_more ?? false,
    summary: data.summary ?? emptySummary,
    limit: data.limit ?? 50,
    offset: data.offset ?? offset,
  };
}

export interface ComputeJobsPagedResponse {
  jobs: ComputeJob[];
  hasMore: boolean;
  summary: ComputeJobsSummary;
}

export async function fetchComputeJobsPaged(
  projectId: string,
  signal?: AbortSignal,
  opts?: { allTerminal?: boolean; includeFinishedMinutes?: number; offset?: number },
): Promise<ComputeJobsPagedResponse> {
  const query = new URLSearchParams();
  if (opts?.allTerminal) {
    query.set('all_terminal', 'true');
  } else if (typeof opts?.includeFinishedMinutes === 'number') {
    query.set('include_finished_minutes', String(opts.includeFinishedMinutes));
  }
  if (typeof opts?.offset === 'number' && opts.offset > 0) {
    query.set('offset', String(opts.offset));
  }
  const suffix = query.toString() ? `?${query.toString()}` : '';
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/compute-jobs${suffix}`,
    undefined,
    2,
    30000,
    signal,
  );
  if (!res.ok) throw new Error(`Failed to fetch compute jobs: ${res.status}`);
  const data = await res.json();
  const emptySummary: ComputeJobsSummary = {
    total_jobs: 0, active_jobs: 0, terminal_jobs: 0, total_spend_usd: 0,
    outcome_breakdown: {}, criterion_measured: 0, criterion_met: 0,
  };
  return {
    jobs: Array.isArray(data.compute_jobs) ? data.compute_jobs : [],
    hasMore: !!data.has_more,
    summary: data.summary ?? emptySummary,
  };
}

export interface ComputeSweepKnowledgeEntry {
  id: string;
  title: string;
  summary?: string | null;
  compute_acceptance_grade?: 'acceptance' | 'measured_only' | null;
  criterion_met?: boolean | null;
  criterion?: ComputeSuccessCriterion | null;
  per_arm_verdicts?: Record<string, { value: number | null; met: boolean | null; cost_usd: number | null }> | null;
  arm_label?: string | null;
  created_at?: string | null;
  source_kind?: string | null;
  evidence_tier?: 'covered' | 'attested' | 'backfilled' | 'partial' | 'uncovered' | null;
  run_provenance?: {
    executing_backend?: string | null;
    runner_hostname?: string | null;
    runner_target_id?: string | null;
  } | null;
}

export interface ComputeSweepGroupsResponse {
  sweep_groups: Record<string, ComputeSweepKnowledgeEntry[]>;
}

export async function fetchComputeSweepGroups(
  projectId: string,
  signal?: AbortSignal,
): Promise<ComputeSweepGroupsResponse> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/compute-jobs/sweep-groups`,
    undefined,
    2,
    30000,
    signal,
  );
  if (!res.ok) throw new Error(`Failed to fetch sweep groups: ${res.status}`);
  return res.json();
}

export async function fetchComputeJobsAggregateSummary(
  projectId: string,
  signal?: AbortSignal,
): Promise<ComputeJobsSummary> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/compute-jobs/summary`,
    undefined,
    2,
    30000,
    signal,
  );
  if (!res.ok) throw new Error(`Failed to fetch compute jobs summary: ${res.status}`);
  return res.json();
}


export async function killComputeJob(jobId: string): Promise<ComputeJob> {
  const res = await apiFetch(
    `/api/admin/compute-jobs/${encodeURIComponent(jobId)}/kill`,
    { method: 'POST' },
  );
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    const msg = data?.error?.message || data?.detail || `Failed to kill compute job: ${res.status}`;
    throw new Error(typeof msg === 'string' ? msg : `Failed to kill compute job: ${res.status}`);
  }
  const data = await res.json();
  return data.compute_job;
}

export type AnalyzeComputeJobStatus = 'upgraded' | 'analysis_unavailable';

export interface AnalyzeComputeJobEnvelope {
  status: AnalyzeComputeJobStatus;
  reason?: string | null;
  analysis_mode?: string | null;
  knowledge_document_id?: string | null;
  [key: string]: unknown;
}

export async function analyzeComputeJob(jobId: string): Promise<AnalyzeComputeJobEnvelope> {
  const res = await apiFetch(
    `/api/admin/compute-jobs/${encodeURIComponent(jobId)}/analyze`,
    { method: 'POST' },
  );
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const msg = data?.error?.message || data?.detail || `Failed to analyze compute job: ${res.status}`;
    throw new Error(typeof msg === 'string' ? msg : `Failed to analyze compute job: ${res.status}`);
  }
  const status = data?.status === 'analysis_unavailable' ? 'analysis_unavailable' : 'upgraded';
  return {
    ...data,
    status,
    reason: typeof data?.reason === 'string' ? data.reason : data?.reason ?? null,
  };
}

// --- Knowledge documents (durable, queryable home for web_research output) ---

/** A single knowledge document. List responses omit `body`; get-by-id includes it. */
/**
 * Citation-grounding probe detail, present for BOTH knowledge kinds when the
 * agent stamped a grounding result (absent only for older / un-probed docs):
 * for `design` the counts are file-reference citations resolved against the
 * repo; for `web_research` they are cited URLs probed for reachability. The
 * field names are shared across both kinds — `file_citations` is the number of
 * citations checked, `grounded` the number confirmed (resolved files / reachable
 * URLs).
 */
export interface KnowledgeGroundingProbe {
  file_citations: number;
  grounded: number;
  /** Citation strings that did NOT ground: absent repo paths / dead URLs. */
  unresolved: string[];
}

/** Topic-responsiveness probe detail: keyword overlap between request and document. */
export interface KnowledgeTopicResponsivenessProbe {
  passed: boolean;
  overlap: number;
  total: number;
}

/** Goal-level acceptance-probe detail persisted under the verdict's `probes` key. */
export interface KnowledgeAcceptanceProbes {
  grounding?: KnowledgeGroundingProbe | null;
  topic_responsiveness?: KnowledgeTopicResponsivenessProbe | null;
}

/**
 * Forward-quality verdict persisted on a knowledge document. All fields are
 * optional/nullable so documents persisted before a given field existed (e.g.
 * pre-`probes` docs) render without errors. The index signature keeps it
 * structurally compatible with the opaque `Record<string, unknown>` this used
 * to be.
 */
export interface KnowledgeForwardQualityVerdict {
  passed?: boolean;
  reason?: string;
  strategy?: string;
  grader_task_id?: string;
  probes?: KnowledgeAcceptanceProbes | null;
  [key: string]: unknown;
}

export interface KnowledgeCitationRecord {
  ref?: string | null;
  url?: string | null;
  title?: string | null;
  excerpt?: string | null;
}

export type KnowledgeCitation = string | KnowledgeCitationRecord;

/** Origin provenance state for a knowledge document's producing task (backend
 *  `serialize_origin_provenance`). `research_finding` documents were spawned from a
 *  research finding (and may carry a challenge); `not_research_finding_derived`
 *  documents came from a non-finding source; `legacy_unknown` documents predate the
 *  provenance contract. `string` keeps it forward-compatible. */
export type KnowledgeOriginState =
  | 'research_finding'
  | 'not_research_finding_derived'
  | 'legacy_unknown'
  | string;

/** Second-opinion decision on the originating research finding (backend
 *  `_challenge_state`). `challenger_confirmed` / `overturned` /
 *  `indeterminate_fail_closed` describe the resolved challenge; `unchallenged` means
 *  a finding origin with no challenge; `not_applicable` / `legacy_unknown` mean the
 *  challenge axis does not apply. */
export type KnowledgeChallengeState =
  | 'unchallenged'
  | 'challenger_confirmed'
  | 'overturned'
  | 'indeterminate_fail_closed'
  | 'not_applicable'
  | 'legacy_unknown'
  | string;

/** Bounded second-opinion decision record for the originating research finding. */
export interface KnowledgeFindingChallenge {
  challenged_boundary?: string | null;
  first_verdict?: string | null;
  second_opinion_verdict?: string | null;
  agreement?: 'agrees' | 'disagrees' | 'indeterminate' | string | null;
  reason?: string | null;
  sidecar_task_id?: string | null;
  [key: string]: unknown;
}

/** Durable, bounded origin provenance attached by the backend to design /
 *  web_research knowledge documents. All fields optional/nullable so legacy
 *  documents and older backend responses (where the object is absent) render
 *  cleanly. */
export interface KnowledgeOriginProvenance {
  origin_state?: KnowledgeOriginState;
  research_task_id?: string | null;
  parent_research_task_id?: string | null;
  finding_index?: number | null;
  finding_hash?: string | null;
  direction_id?: string | null;
  challenge_state?: KnowledgeChallengeState | null;
  finding_challenge?: KnowledgeFindingChallenge | null;
  [key: string]: unknown;
}

/** Promoted-and-serving release evidence that authorized a live-release compute proof.
 *  Surfaced as a top-level field on compute_experiment KnowledgeDocument responses
 *  (backend `knowledge_query.serialize_knowledge_document`, sourced from the launching
 *  ComputeJob's `polling_metadata.live_release_evidence`). Null/absent for non-live-proof
 *  documents and non-compute documents. All fields optional/nullable to stay
 *  back-compatible with evidence serialized before a field existed. */
export interface LiveReleaseEvidence {
  promoted_sha?: string | null;
  promoted_at?: string | null;
  aggregate_serving_status?: string | null;
  aggregate_reason_code?: string | null;
  release_version_id?: string | null;
  release_version_number?: number | null;
  required_artifacts?: Array<{
    artifact_kind?: string;
    required?: boolean;
    serving_status?: string | null;
    served_identifier?: string | null;
    served_at?: string | null;
    reason_code?: string | null;
  }>;
}

export interface KnowledgeDocument {
  id: string;
  title: string;
  summary: string | null;
  topic: string | null;
  /** Strategic direction key (e.g. `d-1a2b3c4d`) that motivated the producing task, or null. */
  direction_id: string | null;
  tags: string[];
  source_citations: KnowledgeCitation[];
  source_task_id: string | null;
  authoring_branch?: string | null;
  head_sha?: string | null;
  /** Which kind of task produced this document. `string` keeps it forward-compatible. */
  source_kind?: 'design' | 'web_research' | 'unknown' | string;
  created_at: string | null;
  updated_at: string | null;
  forward_quality_verdict: KnowledgeForwardQualityVerdict | null;
  // Backend-classified acceptance signal (app.knowledge_query.classify_knowledge_acceptance,
  // the single source of truth). For compute_experiment documents the backend grades
  // LLM-grounded, artifact-cited analyses as 'acceptance' and deterministic fallback /
  // grounding-miss / unresolved-citation documents as 'measured_only'; non-compute docs
  // grade as null (they cleared their own forward-quality gate). `knowledge_acceptance_evidence`
  // is the boolean strict-acceptance flag, and `compute_acceptance_reason` carries the
  // classifier's reason code (e.g. 'llm_artifact_cited', 'analysis_mode_not_llm',
  // 'citation_grounding_miss'). All optional/nullable to stay back-compatible with docs
  // serialized before the fields existed.
  knowledge_acceptance_evidence?: boolean;
  compute_acceptance_grade?: 'acceptance' | 'measured_only' | null;
  compute_acceptance_reason?: string;
  /** Mirror of the backend analysis mode for compute_experiment documents. */
  analysis_mode?: string | null;
  /** Concrete compute-analysis fallback cause mirrored from the backend verdict.
   *  Present only for measured-only compute_experiment docs that fell back. */
  analysis_fallback_reason?: string | null;
  /** Explicit unavailable cause mirrored from the backend verdict. */
  unavailable_cause?: string | null;
  /** Durable origin provenance (backend `serialize_origin_provenance`): whether the
   *  producing task came from a research finding, and the second-opinion decision that
   *  allowed or rejected its dispatch. Absent on older backend responses — treat a
   *  missing object as legacy/unknown. */
  origin_provenance?: KnowledgeOriginProvenance | null;
  /** Promoted serving release evidence that authorized a live-release compute proof.
   *  Present (non-null) only for compute_experiment documents whose launching job ran as
   *  a live-release proof; null/absent otherwise. */
  release_evidence?: LiveReleaseEvidence | null;
  /** Sweep grouping ID for compute_experiment multi-arm experiments. Groups all arms and aggregate. */
  sweep_id?: string | null;
  /** Arm label within a sweep (e.g. 'baseline', 'variant'). Null for the aggregate sweep doc. */
  arm_label?: string | null;
  /** Criterion verdict (True/False/None) from the success criterion evaluation for compute_experiment
   *  documents. None = unmeasured/not evaluable. Distinct from compute_acceptance_grade. */
  criterion_met?: boolean | null;
  /** Machine-readable reason the criterion was unmeasured (criterion_met === null).
   *  'no_result_metrics': experiment produced no metrics at all;
   *  'metric_key_absent': metrics were produced but the declared key was absent (possible name mismatch);
   *  'metric_not_numeric': key exists but value is not numeric.
   *  Absent/null when criterion_met is true or false. */
  criterion_unmeasured_reason?: 'no_result_metrics' | 'metric_key_absent' | 'metric_not_numeric' | null;
  /** Proof-strength tier for compute_experiment documents: 'covered' (local hw execution),
   *  'attested' (cloud execution), 'backfilled' (metrics without direct run evidence),
   *  'partial' (attempted but incomplete), 'uncovered' (no execution evidence). Null for
   *  non-compute documents and older docs persisted before the field existed. */
  evidence_tier?: 'covered' | 'attested' | 'backfilled' | 'partial' | 'uncovered' | null;
  /** Lifecycle tracking for the strategist follow-up workflow. */
  lifecycle_status?: 'pending' | 'consumed' | 'dismissed' | string;
  consumed_at?: string | null;
  dismissed_at?: string | null;
  dismissed_reason?: string | null;
  /** Only present on get-by-id (the full markdown body). */
  body?: string;
}

export interface KnowledgeDocumentsResponse {
  documents: KnowledgeDocument[];
  total: number;
  limit: number;
  offset: number;
}

/** List / search a project's knowledge documents, newest-first. `q` drives full-text/tag/topic search. */
export async function fetchKnowledgeDocuments(
  projectId: string,
  params?: { q?: string; topic?: string; tag?: string; direction?: string; source_kind?: string; origin_state?: string; challenge_state?: string; finding_hash?: string; sweep?: string; lifecycle_status?: string; limit?: number; offset?: number },
): Promise<KnowledgeDocumentsResponse> {
  const sp = new URLSearchParams();
  if (params?.q) sp.set('q', params.q);
  if (params?.topic) sp.set('topic', params.topic);
  if (params?.tag) sp.set('tag', params.tag);
  if (params?.direction) sp.set('direction', params.direction);
  if (params?.source_kind) sp.set('source_kind', params.source_kind);
  if (params?.origin_state) sp.set('origin_state', params.origin_state);
  if (params?.challenge_state) sp.set('challenge_state', params.challenge_state);
  if (params?.finding_hash) sp.set('finding_hash', params.finding_hash);
  if (params?.sweep) sp.set('sweep_id', params.sweep);
  if (params?.lifecycle_status) sp.set('lifecycle_status', params.lifecycle_status);
  if (params?.limit !== undefined) sp.set('limit', String(params.limit));
  if (params?.offset !== undefined) sp.set('offset', String(params.offset));
  const query = sp.toString();
  const url = `/api/admin/projects/${encodeURIComponent(projectId)}/knowledge-documents${query ? `?${query}` : ''}`;
  const res = await apiFetch(url);
  if (!res.ok) throw new Error(`Failed to fetch knowledge documents: ${res.status}`);
  return res.json();
}

/** Fetch a single knowledge document including its markdown body. */
export async function fetchKnowledgeDocument(
  projectId: string,
  documentId: string,
): Promise<KnowledgeDocument> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/knowledge-documents/${encodeURIComponent(documentId)}`,
  );
  if (!res.ok) throw new Error(`Failed to fetch knowledge document: ${res.status}`);
  return res.json();
}

/** Operator-steering strategic-refresh marker persisted on `project.vision._strategic_refresh`.
 *  Armed when operator steering wakes the strategist; cleared backend-side when the
 *  autonomous loop consumes it. Returned (inside `vision`) by GET /api/admin/projects/{id}. */
export interface FleetUtilizationResponse {
  active_now: number;
  nominal_capacity: number | null;
  usable_capacity: number | null;
  cooled_accounts: number;
  util_now: number | null;
  util_avg: number | null;
  window_hours: number;
  /** Derived saturation label; null when no enabled accounts are configured. */
  saturation: 'healthy' | 'busy' | 'saturated' | null;
  vm_cap: number | null;
  active_task_vms: number | null;
  managed_vm_count: number | null;
  idle_pool_vms: number | null;
  vm_slots_available: number | null;
  vm_saturation: 'healthy' | 'busy' | 'saturated' | null;
  precomputed?: boolean;
  precomputed_at?: string | null;
  precompute_age_seconds?: number | null;
  served_stale?: boolean;
}

export async function fetchFleetUtilization(): Promise<FleetUtilizationResponse> {
  const res = await apiFetch('/api/admin/fleet-utilization');
  if (!res.ok) throw new Error(`fleet-utilization fetch failed: ${res.status}`);
  return res.json();
}

// ── Fleet-wide runner-health summary ───────────────────────────────────────────

/** Per-project runner-health breakdown in the fleet summary. */
export interface FleetHealthProjectEntry {
  project_id: string;
  total: number;
  connected: number;
  degraded: number;
  stale: number;
  offline: number;
  low_disk: number;
  // Count of runners below the low-memory threshold. Optional/null-tolerant:
  // a runner (old build) or serving backend that never reported memory omits it.
  low_memory?: number;
  runners: FleetRunner[];
}

export interface FleetHealthSummaryResponse {
  total: number;
  connected: number;
  degraded: number;
  stale: number;
  offline: number;
  low_disk: number;
  low_memory?: number;
  per_project: FleetHealthProjectEntry[];
  project_count: number;
  generated_at: string;
}

export async function fetchFleetHealthSummary(): Promise<FleetHealthSummaryResponse> {
  const res = await apiFetch('/api/admin/fleet/runners/health-summary');
  if (!res.ok) throw new Error(`fleet-health-summary fetch failed: ${res.status}`);
  return res.json();
}


export interface EvergreenFillDecisionRecord {
  action: string;
  reason: string;
  trigger: string | null;
  project_id: string | null;
  /** ISO timestamp; present when backed by the durable log (not in-memory fallback). */
  ts?: string;
}

export interface EvergreenFillObservabilityResponse {
  created_count: number;
  reason_counts: Record<string, number>;
  trigger_counts: Record<string, number>;
  recent: EvergreenFillDecisionRecord[];
}

export interface EvergreenBacklogItem {
  id: string;
  project_id: string;
  finding_key: string;
  title: string;
  detail: string | null;
  status: string;
  outcome: string | null;
  provenance: string;
  direction_id: string | null;
  attempts: number;
  spawned_task_id: string | null;
  verified_against_sha: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface EvergreenBacklogResponse {
  items: EvergreenBacklogItem[];
}

export type EvergreenBacklogSummaryResponse = Record<string, number>;

interface EvergreenBacklogSummaryEnvelope {
  project_id: string;
  counts: EvergreenBacklogSummaryResponse;
}

export interface EvergreenFillSettings {
  evergreen_fill_mode: string;
  evergreen_fill_interval_minutes: number;
  evergreen_fill_util_threshold: number;
  evergreen_fill_min_free_slots: number;
  evergreen_fill_max_active: number;
  evergreen_fill_daily_cap: number;
  evergreen_fill_deferred_memo_enrollment: boolean;
}

export async function fetchEvergreenFillObservability(
  signal?: AbortSignal,
): Promise<EvergreenFillObservabilityResponse> {
  const res = await apiFetch('/api/admin/evergreen/fill-observability', undefined, 2, 30000, signal);
  if (!res.ok) throw new Error(`Failed to fetch evergreen fill observability: ${res.status}`);
  return res.json();
}

export async function fetchEvergreenBacklog(
  projectId: string,
  signal?: AbortSignal,
): Promise<EvergreenBacklogResponse> {
  const params = new URLSearchParams({ project_id: projectId });
  const res = await apiFetch(`/api/admin/evergreen/backlog?${params}`, undefined, 2, 30000, signal);
  if (!res.ok) throw new Error(`Failed to fetch evergreen backlog: ${res.status}`);
  return res.json();
}

export async function fetchEvergreenBacklogSummary(
  projectId: string,
  signal?: AbortSignal,
): Promise<EvergreenBacklogSummaryResponse> {
  const params = new URLSearchParams({ project_id: projectId });
  const res = await apiFetch(`/api/admin/evergreen/backlog-summary?${params}`, undefined, 2, 30000, signal);
  if (!res.ok) throw new Error(`Failed to fetch evergreen backlog summary: ${res.status}`);
  const data = await res.json() as EvergreenBacklogSummaryEnvelope;
  return data.counts ?? {};
}

export async function fetchEvergreenFillSettings(signal?: AbortSignal): Promise<EvergreenFillSettings> {
  const res = await apiFetch('/api/admin/settings', undefined, 2, 30000, signal);
  if (!res.ok) throw new Error(`Failed to fetch settings: ${res.status}`);
  const data = await res.json();
  return {
    evergreen_fill_mode: data?.evergreen_fill_mode ?? 'off',
    evergreen_fill_interval_minutes: data?.evergreen_fill_interval_minutes ?? 15,
    evergreen_fill_util_threshold: data?.evergreen_fill_util_threshold ?? 0.5,
    evergreen_fill_min_free_slots: data?.evergreen_fill_min_free_slots ?? 2,
    evergreen_fill_max_active: data?.evergreen_fill_max_active ?? 1,
    evergreen_fill_daily_cap: data?.evergreen_fill_daily_cap ?? 3,
    evergreen_fill_deferred_memo_enrollment: data?.evergreen_fill_deferred_memo_enrollment ?? false,
  };
}

export interface StructuralHealthRowMeasurement {
  task_id: string;
  project_id: string;
  bytes: number;
}

export interface StructuralHealthObservabilityResponse {
  cycles: number;
  enrolled_total: number;
  last_decision: Record<string, unknown> | null;
  largest_rows: StructuralHealthRowMeasurement[];
  recent: Record<string, unknown>[];
  last_jsonb_bloat_decision: Record<string, unknown> | null;
  last_gin_overhead_decision: Record<string, unknown> | null;
  last_pool_saturation_decision: Record<string, unknown> | null;
}

export async function fetchStructuralHealthObservability(
  signal?: AbortSignal,
): Promise<StructuralHealthObservabilityResponse> {
  const res = await apiFetch('/api/admin/evergreen/structural-health-observability', undefined, 2, 30000, signal);
  if (!res.ok) throw new Error(`Failed to fetch structural health observability: ${res.status}`);
  return res.json();
}

export interface ObserveModeVerdictResponse {
  verdict: 'go' | 'no_go' | 'insufficient_data';
  verdict_reason: string;
  window_hours: number;
  observe_evaluations: number;
  would_have_fired: number;
  fire_rate_pct: number | null;
  max_fire_rate_pct: number;
  min_evaluations: number;
  daily_cap: number;
  cap_adjusted_would_have_fired: number;
  cap_adjusted_fire_rate_pct: number | null;
}

export async function fetchObserveModeVerdict(
  windowHours = 168,
  maxFireRatePct = 20.0,
  minEvaluations = 10,
  dailyCap = 3,
  signal?: AbortSignal,
): Promise<ObserveModeVerdictResponse> {
  const params = new URLSearchParams({
    window_hours: String(windowHours),
    max_fire_rate_pct: String(maxFireRatePct),
    min_evaluations: String(minEvaluations),
    daily_cap: String(dailyCap),
  });
  const res = await apiFetch(`/api/admin/evergreen/observe-mode-verdict?${params}`, undefined, 2, 30000, signal);
  if (!res.ok) throw new Error(`Failed to fetch observe mode verdict: ${res.status}`);
  return res.json();
}

export async function patchEvergreenFillSettings(patch: Partial<EvergreenFillSettings>): Promise<void> {
  const res = await apiFetch('/api/admin/settings', {
    method: 'PATCH',
    body: JSON.stringify(patch),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    const msg = data?.detail || `Failed to update evergreen fill settings: ${res.status}`;
    throw new Error(typeof msg === 'string' ? msg : `Failed to update evergreen fill settings: ${res.status}`);
  }
}

export async function patchStructuralHealthSettings(patch: { structural_health_audit_mode?: string }): Promise<void> {
  const res = await apiFetch('/api/admin/settings', {
    method: 'PATCH',
    body: JSON.stringify(patch),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    const msg = data?.detail || `Failed to update structural health settings: ${res.status}`;
    throw new Error(typeof msg === 'string' ? msg : `Failed to update structural health settings: ${res.status}`);
  }
}

export interface MetricsIntegrityObservabilityResponse {
  ticks: number;
  checks_run: number;
  flags_total: number;
  errors_total: number;
  enrolled_total: number;
  last_tick_at: string | null;
  last_tick: Record<string, unknown> | null;
  recent_ticks: Record<string, unknown>[];
  recent_flags: Record<string, unknown>[];
}

export async function fetchMetricsIntegrityObservability(
  signal?: AbortSignal,
): Promise<MetricsIntegrityObservabilityResponse> {
  const res = await apiFetch('/api/admin/evergreen/metrics-integrity-observability', undefined, 2, 30000, signal);
  if (!res.ok) throw new Error(`Failed to fetch metrics integrity observability: ${res.status}`);
  return res.json();
}

export async function patchMetricsIntegritySettings(patch: { metrics_integrity_sweep_mode?: string }): Promise<void> {
  const res = await apiFetch('/api/admin/settings', {
    method: 'PATCH',
    body: JSON.stringify(patch),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    const msg = data?.detail || `Failed to update metrics integrity settings: ${res.status}`;
    throw new Error(typeof msg === 'string' ? msg : `Failed to update metrics integrity settings: ${res.status}`);
  }
}

export type CapabilityProofGateMode = 'off' | 'observe' | 'enforce';

/**
 * Fleet-wide capability-proof coverage that the auto-graduation observe/enforce
 * thresholds are compared against. Surfaced read-only in settings so the
 * operator can see where the gate stands. `coverage_ratio` is null when the
 * denominator (non-exempt working capabilities) is zero.
 */
export interface CapabilityProofGateCoverage {
  coverage_ratio: number | null;
  total_proven: number;
  total_non_exempt_working: number;
  projects_contributing: number;
  num_projects: number;
  trial_eligible_unproven?: number;
}

export interface CapabilityProofTrialProjection {
  trial_eligible: number;
  unproven_working: number;
  projects_contributing: number;
  num_projects: number;
}

export interface CapabilityProofGateSettings {
  capability_proof_gate_mode: CapabilityProofGateMode;
  capability_proof_gate_auto_graduation_enabled: boolean;
  capability_proof_gate_auto_graduation_observe_threshold: number;
  capability_proof_gate_auto_graduation_enforce_threshold: number;
  capability_proof_execution_trials_enabled?: boolean;
}

// PATCHes any partial of the four capability-proof-gate config keys. The backend
// validates the mode enum, the boolean, and clamps thresholds to 0.0–1.0; only
// the keys the operator changed should be sent.
export async function patchCapabilityProofGateSettings(
  patch: Partial<CapabilityProofGateSettings>,
): Promise<void> {
  const res = await apiFetch('/api/admin/settings', {
    method: 'PATCH',
    body: JSON.stringify(patch),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    const msg = data?.detail || `Failed to update capability proof gate settings: ${res.status}`;
    throw new Error(typeof msg === 'string' ? msg : `Failed to update capability proof gate settings: ${res.status}`);
  }
}

// Shape returned by both the grant (POST) and revoke (DELETE) capability-exemption
// endpoints. `exempt_capabilities` is the full normalized set of operator-exempt
// capability names AFTER the mutation, so the caller can reconcile local state.
export interface CapabilityExemptionResponse {
  status: 'granted' | 'revoked';
  capability_name: string;
  exempt_capabilities: string[];
  // Present only on grant — true when the capability was already exempt (no-op).
  already_exempt?: boolean;
  // Present only on revoke — number of exemption records removed (0 when none).
  removed?: number;
}

// Grants an OPERATOR proof exemption for a named `working` capability, clearing
// the enforce gate for it WITHOUT machine proof and excluding it from the
// auto-graduation coverage denominator. OWNER-guarded server-side (403 otherwise);
// 404 if the name is not a declared `working` capability. Idempotent.
export async function grantCapabilityProofExemption(
  projectId: string,
  capabilityName: string,
  reason?: string,
): Promise<CapabilityExemptionResponse> {
  const res = await apiFetch(
    `/api/admin/projects/${projectId}/capability-exemptions`,
    {
      method: 'POST',
      body: JSON.stringify(
        reason ? { capability_name: capabilityName, reason } : { capability_name: capabilityName },
      ),
    },
  );
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    const msg = data?.detail || `Failed to grant capability exemption: ${res.status}`;
    throw new Error(typeof msg === 'string' ? msg : `Failed to grant capability exemption: ${res.status}`);
  }
  return res.json();
}

// Revokes the OPERATOR proof exemption for a named capability, so it once again
// counts against the coverage denominator and must earn a real proof to clear
// the enforce gate. OWNER-guarded server-side. Idempotent (removed: 0 when none).
export async function revokeCapabilityProofExemption(
  projectId: string,
  capabilityName: string,
): Promise<CapabilityExemptionResponse> {
  const res = await apiFetch(
    `/api/admin/projects/${projectId}/capability-exemptions/${encodeURIComponent(capabilityName)}`,
    { method: 'DELETE' },
  );
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    const msg = data?.detail || `Failed to revoke capability exemption: ${res.status}`;
    throw new Error(typeof msg === 'string' ? msg : `Failed to revoke capability exemption: ${res.status}`);
  }
  return res.json();
}


export interface FleetRunner {
  id: string;
  // Present on newer servers. Mutations remain scoped here; absence preserves
  // rolling-deployment compatibility by falling back to the viewed project.
  enrollment_project_id?: string;
  hostname: string | null;
  version: string | null;
  status: string;
  capabilities: {
    task_types?: string | string[];
    has_gpu?: boolean;
    max_concurrent?: number;
    max_concurrent_compute?: number;
    // Task ids the runner reported as currently executing in its last heartbeat.
    active_task_ids?: string[];
    active_compute_job_ids?: string[];
    runner_preset?: string;
    workload?: string;
    gpu_count?: number;
    gpu_names?: string[];
    // Isolation-backend inventory + selected backends reported by the runner
    // heartbeat, for per-device observability in the fleet panel.
    isolation_backends?: {
      bwrap_ok?: boolean;
      userns_ok?: boolean;
      docker_usable?: boolean;
      docker_cdi_gpu?: boolean;
      podman_usable?: boolean;
      apptainer_ok?: boolean;
      enroot_ok?: boolean;
      systemd_run_ok?: boolean;
    };
    agent_backend?: string;
    compute_backend?: string;
    // Display-only trust telemetry stashed on capabilities at heartbeat.
    disk_free_bytes?: number;
    memory_free_bytes?: number;
    cpu_load_avg?: number;
    gpu_utilization_pct?: number;
    // Per-project docker image-cache accounting (docker-backed runners only).
    // A multi-project runner shares one image cache; this attributes its size
    // back to the project that pulled each image, so the operator can see which
    // allowed project fills a small image-layer disk.
    image_cache_by_project?: {
      total_bytes?: number;
      image_count?: number;
      by_project?: Record<string, { bytes?: number; image_count?: number }>;
      unattributed_bytes?: number;
      unattributed_image_count?: number;
    };
    [key: string]: unknown;
  };
  health?: {
    device_status?: 'connected' | 'degraded' | 'stale' | 'offline' | string;
    device_status_reason?: string | null;
    is_live: boolean;
    // "connected but failing": live (recent heartbeat) but its jobs are failing.
    degraded?: boolean;
    degraded_reason?: 'job_failure_streak' | 'success_stale' | string | null;
    seconds_since_last_seen: number | null;
    active_task_count: number;
    active_compute_job_count?: number;
    active_work_count?: number;
    max_concurrent: number | null;
    max_concurrent_compute?: number | null;
    available_slots: number | null;
    available_agent_slots?: number | null;
    available_compute_slots?: number | null;
    stale_reason: string | null;
    agent_available?: boolean | null;
    agent_unavailable_reason?: string | null;
    // Per-leg ok/failed breakdown of the four agent-backend legs so the fleet
    // panel can render a checklist instead of parsing one folded reason string.
    // null/absent when the runner (older build) never reported it.
    agent_unavailable_legs?: {
      bwrap_sandbox?: boolean;
      agent_dist?: boolean;
      node_runtime?: boolean;
      docker?: boolean;
    } | null;
    liveness_window_seconds: number;
    // Trust telemetry surfaced from the backend health computation.
    last_successful_job_at?: string | null;
    seconds_since_last_success?: number | null;
    consecutive_job_failures?: number;
    disk_free_bytes?: number | null;
    disk_low?: boolean;
    // Memory-pressure telemetry, symmetric with disk. memory_low is true when
    // free memory is below the backend's low-memory threshold. Emitted by
    // _runner_health(); null/absent for runners (old build) that never reported it.
    memory_free_bytes?: number | null;
    memory_low?: boolean;
    cpu_load_avg?: number | null;
    gpu_utilization_pct?: number | null;
    failure_streak_threshold?: number;
  };
  // Server-authoritative live-safe config the daemon converges to on heartbeat,
  // plus the desired-vs-applied generation markers. Optional: runners enrolled
  // before this contract, or that have not yet reported an applied generation,
  // omit it (or report applied_config_generation=null → config_pending=true).
  config?: {
    desired?: {
      max_concurrent?: number | null;
      max_concurrent_compute?: number | null;
      task_types?: string | string[];
      gpu_indices_allowed?: 'all' | number[];
      gpus_per_job?: number;
    };
    config_generation?: string;
    applied?: Record<string, unknown> | null;
    applied_config_generation?: string | null;
    // True whenever the operator changed config but the daemon has not yet
    // echoed the matching generation (includes the never-reported case).
    config_pending?: boolean;
    // ISO timestamp of when the desired live-safe config last actually changed
    // (operator PATCH or enrollment baseline). Paired with last_seen_at it lets
    // the UI bound how long a push has been pending and flag a STUCK state.
    // Absent on runners enrolled before this stamp existed (unknown, not stuck).
    desired_changed_at?: string | null;
    // Whether the daemon has EVER echoed an applied generation. False = too old
    // to speak the live-config contract (never reports); True but still pending =
    // reported a stale/rejected generation. Distinguishes those honest cases.
    daemon_reports_applied?: boolean;
    // Structured config-apply outcome the daemon reported (all additive/optional).
    // apply_status: 'applied' | 'rejected'. apply_reason names the offending
    // field(s) — the backend only surfaces it as LIVE when the rejected
    // generation matches the currently-desired generation (a stale reason tied to
    // a superseded generation is suppressed to null). rejected_config_generation
    // is the generation the daemon could not apply. config_rejected_fields is the
    // offending field name(s) as a structured list, surfaced under the same
    // live/stale suppression as apply_reason. All absent on a legacy daemon.
    apply_status?: string | null;
    apply_reason?: string | null;
    rejected_config_generation?: string | null;
    config_rejected_fields?: string[] | null;
    gpu_indices_allowed?: 'all' | number[];
    // Number of GPUs the runner reserves per compute job (server-authoritative,
    // mirrored top-level for the fleet editor). Absent on runners enrolled before
    // this contract; defaults to 1 GPU per job.
    gpus_per_job?: number;
  };
  last_seen_at: string | null;
  created_at: string | null;
  // Server-side proof that a heartbeat authenticated with the current token
  // generation. False immediately after rotation until the replacement token
  // is actually used; immune to last_seen_at/rotation commit races.
  current_token_heartbeat_confirmed?: boolean;
}

/** Register a new fleet runner for a project. Returns the one-time token.
 *
 * The registration endpoint returns exactly `{runner_id, token}` — runner tokens
 * do not expire, so there is no expiry field here. A prior version of this type
 * declared phantom
 * `token_expires_at`/`expires_at`/`expiresAt` fields that the server never sent,
 * driving a dead "Token expires" affordance in the enroll dialog; both removed. */
export interface FleetRunnerRegistrationResult {
  runner_id: string;
  token: string;
}

/** Rotate a fleet runner token and return its one-time replacement token.
 * `rotated_at` remains optional for rolling-deployment compatibility and display;
 * reconnect proof comes from FleetRunner.current_token_heartbeat_confirmed. */
export interface FleetRunnerTokenRotationResult {
  runner_id: string;
  token: string;
  rotated_at?: string;
}

export async function registerFleetRunner(
  projectId: string,
  opts: {
    hostname?: string;
    version?: string;
    capabilities?: Record<string, unknown>;
    preset?: 'default' | 'strategic_local';
  } = {},
): Promise<FleetRunnerRegistrationResult> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/runners/register`,
    { method: 'POST', body: JSON.stringify(opts) },
  );
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    const msg = data?.detail || `Failed to register runner: ${res.status}`;
    throw new Error(typeof msg === 'string' ? msg : `Failed to register runner: ${res.status}`);
  }
  return res.json();
}

/** Rotate a runner token. Returns the one-time replacement token. */
export async function rotateFleetRunnerToken(
  projectId: string,
  runnerId: string,
): Promise<FleetRunnerTokenRotationResult> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/runners/${encodeURIComponent(runnerId)}/rotate-token`,
    { method: 'POST' },
  );
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    const msg = data?.detail || `Failed to rotate runner token: ${res.status}`;
    throw new Error(typeof msg === 'string' ? msg : `Failed to rotate runner token: ${res.status}`);
  }
  return res.json();
}

/** List active (non-deleted) runners for a project. */
export async function listFleetRunners(projectId: string): Promise<FleetRunner[]> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/runners`,
  );
  if (!res.ok) throw new Error(`Failed to list runners: ${res.status}`);
  return res.json();
}

/** Revoke a runner by ID (marks it deleted). */
export async function revokeFleetRunner(projectId: string, runnerId: string): Promise<void> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/runners/${encodeURIComponent(runnerId)}`,
    { method: 'DELETE' },
  );
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    const msg = data?.detail || `Failed to revoke runner: ${res.status}`;
    throw new Error(typeof msg === 'string' ? msg : `Failed to revoke runner: ${res.status}`);
  }
}

/** Update a runner's operator-configured capabilities in-place (partial merge).
 * Only provided fields are updated; runner-reported hardware fields are preserved.
 * Returns the updated runner view. */
export async function updateRunnerCapabilities(
  projectId: string,
  runnerId: string,
  capabilities: {
    max_concurrent?: number;
    task_types?: string | string[];
    max_concurrent_compute?: number;
    gpu_indices_allowed?: 'all' | number[];
    gpus_per_job?: number;
  },
): Promise<FleetRunner> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/runners/${encodeURIComponent(runnerId)}/capabilities`,
    { method: 'PATCH', body: JSON.stringify(capabilities) },
  );
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    const msg = data?.detail || `Failed to update runner capabilities: ${res.status}`;
    throw new Error(typeof msg === 'string' ? msg : `Failed to update runner capabilities: ${res.status}`);
  }
  return res.json();
}

/** Update a runner's lifecycle status (pause / drain / resume).
 * Valid transitions: active→paused, active→draining, paused→active, draining→active, draining→paused.
 * Returns the updated runner view. */
export async function patchRunnerStatus(
  projectId: string,
  runnerId: string,
  status: 'active' | 'paused' | 'draining',
): Promise<FleetRunner> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/runners/${encodeURIComponent(runnerId)}/status`,
    { method: 'PATCH', body: JSON.stringify({ status }) },
  );
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    const msg = data?.detail || `Failed to update runner status: ${res.status}`;
    throw new Error(typeof msg === 'string' ? msg : `Failed to update runner status: ${res.status}`);
  }
  return res.json();
}

// --- Runner project allow-list (multi-project runner scoping) ------------------

/** One project a runner is allowed to serve. `is_enrollment` marks the runner's
 *  enrollment/default project — the implicit anchor member that cannot be removed
 *  from the allow-list. Mirrors the backend `_runner_projects_view` entry shape. */
export interface RunnerProjectEntry {
  project_id: string;
  is_enrollment: boolean;
}

/** A runner's project allow-list: the enrollment anchor UNIONed with every
 *  explicit binding grant. Mirrors the backend `_runner_projects_view` payload. */
export interface RunnerProjectAllowList {
  runner_id: string;
  enrollment_project_id: string;
  projects: RunnerProjectEntry[];
}

/** List the projects a runner is allowed to serve (owner-gated). */
export async function listRunnerProjects(
  projectId: string,
  runnerId: string,
): Promise<RunnerProjectAllowList> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/runners/${encodeURIComponent(runnerId)}/projects`,
  );
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    const msg = data?.detail || `Failed to list runner projects: ${res.status}`;
    throw new Error(typeof msg === 'string' ? msg : `Failed to list runner projects: ${res.status}`);
  }
  return res.json();
}

/** Add a project to a runner's allow-list so the daemon also serves it.
 *  Throws with the backend detail (e.g. the 403 cross-account-attach rejection)
 *  so callers can surface the precise reason inline. Returns the updated list. */
export async function addRunnerProject(
  projectId: string,
  runnerId: string,
  addProjectId: string,
): Promise<RunnerProjectAllowList> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/runners/${encodeURIComponent(runnerId)}/projects`,
    { method: 'POST', body: JSON.stringify({ project_id: addProjectId }) },
  );
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    const msg = data?.detail || `Failed to add runner project: ${res.status}`;
    throw new Error(typeof msg === 'string' ? msg : `Failed to add runner project: ${res.status}`);
  }
  return res.json();
}

/** Remove a non-default project from a runner's allow-list. The enrollment
 *  project cannot be removed (backend rejects it 400). Returns the updated list. */
export async function removeRunnerProject(
  projectId: string,
  runnerId: string,
  addedProjectId: string,
): Promise<RunnerProjectAllowList> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/runners/${encodeURIComponent(runnerId)}/projects/${encodeURIComponent(addedProjectId)}`,
    { method: 'DELETE' },
  );
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    const msg = data?.detail || `Failed to remove runner project: ${res.status}`;
    throw new Error(typeof msg === 'string' ? msg : `Failed to remove runner project: ${res.status}`);
  }
  return res.json();
}

/** A project the current user can attach to a runner (owner role). */
export interface OwnedProjectSummary {
  id: string;
  name: string;
}

/** List projects the current user OWNS — the candidate set for the runner
 *  allow-list add-picker. Filters the admin project list to `my_role === 'owner'`
 *  (the role the add endpoint requires on the project being attached). */
export async function listOwnedProjects(): Promise<OwnedProjectSummary[]> {
  const res = await apiFetch('/api/admin/projects');
  if (!res.ok) throw new Error(`Failed to list projects: ${res.status}`);
  const data = await res.json();
  const items: Array<{ id?: string; name?: string; my_role?: string }> = Array.isArray(data?.items)
    ? data.items
    : [];
  return items
    .filter(p => p.my_role === 'owner' && typeof p.id === 'string')
    .map(p => ({ id: p.id as string, name: (p.name as string) || (p.id as string) }));
}

/** One heartbeat time-series snapshot for a runner (from RunnerHeartbeatSnapshot). */
export interface RunnerTelemetrySnapshot {
  captured_at: string | null;
  gpu_utilization_pct: number | null;
  disk_free_bytes: number | null;
  memory_free_bytes: number | null;
  cpu_load_avg: number | null;
  active_task_count: number;
  active_compute_job_count: number;
  agent_available: boolean | null;
}

export interface RunnerTelemetryResponse {
  runner_id: string;
  since_seconds: number;
  count: number;
  /** Oldest-first, ready to plot left-to-right. */
  snapshots: RunnerTelemetrySnapshot[];
}

/** Fetch recent heartbeat time-series snapshots for one runner (default last hour). */
export async function fetchRunnerTelemetry(
  projectId: string,
  runnerId: string,
  opts?: { sinceSeconds?: number; limit?: number },
): Promise<RunnerTelemetryResponse> {
  const params = new URLSearchParams();
  if (opts?.sinceSeconds != null) params.set('since', String(opts.sinceSeconds));
  if (opts?.limit != null) params.set('limit', String(opts.limit));
  const qs = params.toString();
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/runners/${encodeURIComponent(runnerId)}/telemetry${qs ? `?${qs}` : ''}`,
  );
  if (!res.ok) throw new Error(`Failed to fetch runner telemetry: ${res.status}`);
  return res.json();
}

export interface RunnerOutcome {
  job_id: string;
  task_id: string | null;
  status: string;
  outcome: string | null;
  created_at: string | null;
  completed_at: string | null;
  /** Terminal diagnostic reason for a failed/timed-out job; null on success/legacy rows. */
  terminal_reason?: string | null;
  /** Process exit code for a failed job; null when not an int / absent. */
  exit_code?: number | null;
  /**
   * Server-side redacted + truncated tail of the job's command log, surfaced
   * inline on failed/timed-out rows. null on success/legacy rows or when no
   * log was captured.
   */
  command_log_tail?: string | null;
}

export interface RunnerStatsResponse {
  runner_id: string;
  jobs_run_count: number;
  jobs_terminal_count: number;
  jobs_succeeded_count: number;
  /** succeeded / terminal, or null when no terminal jobs yet. */
  jobs_success_rate: number | null;
  /** On-demand cloud VM cost the runner's terminal jobs would have incurred. */
  cloud_cost_avoided_usd: number;
  /** Number of terminal jobs with an honest runtime window backing the total. */
  cloud_cost_avoided_basis: number;
  /** Median claim latency (vm_cold_start_ms) across terminal tasks, ms. */
  claim_latency_ms_p50: number | null;
  /** 95th percentile claim latency, ms. */
  claim_latency_ms_p95: number | null;
  /** 99th percentile claim latency, ms. */
  claim_latency_ms_p99: number | null;
  /** Maximum claim latency, ms. */
  claim_latency_ms_max: number | null;
  /** Number of tasks with vm_cold_start_ms in the sample. */
  claim_latency_sample_count: number;
  last_n_outcomes: RunnerOutcome[];
}

/** Fetch per-runner compute-job rollups: total, success rate, recent outcomes. */
export async function fetchRunnerStats(
  projectId: string,
  runnerId: string,
  opts?: { recentLimit?: number },
): Promise<RunnerStatsResponse> {
  const params = new URLSearchParams();
  if (opts?.recentLimit != null) params.set('recent_limit', String(opts.recentLimit));
  const qs = params.toString();
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/runners/${encodeURIComponent(runnerId)}/stats${qs ? `?${qs}` : ''}`,
  );
  if (!res.ok) throw new Error(`Failed to fetch runner stats: ${res.status}`);
  return res.json();
}

/** Reset the review-fix iteration cap for a PR-linked task. Archives FAILED
 *  review/fix tasks on the PR to drop them from the global count and zeroes
 *  the per-chain iteration counter. */
