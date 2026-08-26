import { apiFetch } from './core';
import type { SrMissedTask } from '../components/SrRetrospectiveShared';

export interface RetirementCritique {
  direction_id: string | null;
  what_it_addressed: string;
  pattern_that_produced_it: string;
  lesson: string;
  cycle_id: string;
  cycle_created_at: string | null;
}

export interface RetirementCritiquesResponse {
  critiques: RetirementCritique[];
  total: number;
}

export async function fetchRetirementCritiques(
  projectId: string,
  limit = 25,
): Promise<RetirementCritiquesResponse> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/retirement-critiques?limit=${limit}`,
  );
  if (!res.ok) throw new Error(`Failed to fetch retirement critiques: ${res.status}`);
  return res.json();
}

export interface CycleHistoryEntry {
  cycle_id: string;
  cycle_created_at: string | null;
  total_findings: number;
  grade_distribution: Record<string, number>;
  grade_merged: Record<string, number>;
  grade_merge_rate: Record<string, number | null>;
  calibration_score: number | null;
}

export interface ActionabilityResponse {
  cycles_scanned: number;
  total_findings: number;
  grade_distribution: Record<string, number>;
  grade_tasked: Record<string, number>;
  grade_merged: Record<string, number>;
  grade_merge_rate?: Record<string, number | null>;
  grade_merge_rate_of_tasked?: Record<string, number | null>;
  calibration_score?: number | null;
  cycle_history?: CycleHistoryEntry[];
}

export async function fetchResearchActionability(
  projectId: string,
  cycles = 10,
): Promise<ActionabilityResponse> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/research-actionability?cycles=${cycles}`,
  );
  if (!res.ok) throw new Error(`Failed to fetch research actionability: ${res.status}`);
  return res.json();
}

export interface OuterLoopOutcomeCounts {
  merged?: number;
  rejected?: number;
  cap_exhausted?: number;
  stuck?: number;
  standoff?: number;
  repeat_fingerprint_halt?: number;
  [key: string]: number | undefined;
}

export interface OuterLoopIncidenceCounts {
  churn?: number;
  stuck?: number;
  standoff?: number;
  repeat_fingerprint?: number;
  overall_escalations?: number;
  [key: string]: number | undefined;
}

export interface OuterLoopIncidenceRates {
  churn?: number | null;
  stuck?: number | null;
  standoff?: number | null;
  repeat_fingerprint?: number | null;
  overall_escalations?: number | null;
  [key: string]: number | null | undefined;
}

export interface OuterLoopDominantBlocker {
  cause: string;
  count: number;
  total: number;
  share: number;
}

export interface OuterLoopConvergenceSummary {
  window_hours: number;
  prs_with_review_loop: number;
  iteration_median: number | null;
  iteration_p95: number | null;
  iteration_distribution: Record<string, number>;
  churn_total: number;
  churn_pr_count: number;
  drift_pr_count: number;
  outcomes: OuterLoopOutcomeCounts;
  terminal_outcome_histogram?: OuterLoopOutcomeCounts;
  incidence_counts?: OuterLoopIncidenceCounts;
  incidence_rates?: OuterLoopIncidenceRates;
  escalation_total: number;
  escalation_by_reason: Record<string, number>;
  convergence_rate: number | null;
  non_converged_total?: number;
  dominant_blocker?: OuterLoopDominantBlocker | null;
}

export interface OuterLoopConvergenceTrendBucket {
  date: string;
  pr_count: number;
  convergence_rate: number | null;
  escalation_total: number;
  churn_total: number;
  terminal_outcome_histogram?: OuterLoopOutcomeCounts;
  incidence_counts?: OuterLoopIncidenceCounts;
  incidence_rates?: OuterLoopIncidenceRates;
}

export interface OuterLoopConvergenceTrendResponse {
  days: number;
  buckets: OuterLoopConvergenceTrendBucket[];
  // Canonical churn-trend direction computed by the backend
  // (digest_metrics.compute_churn_direction). Consumed by the widget glyph so
  // it can never disagree with the narrative's "Review/fix churn is X." verdict,
  // which reads the same value. Optional for back-compat with older backends.
  churn_direction?: 'falling' | 'rising' | 'flat';
}

export async function fetchOuterLoopConvergence(
  hours = 24,
  projectId?: string,
  signal?: AbortSignal,
): Promise<OuterLoopConvergenceSummary> {
  const params = new URLSearchParams({ hours: String(hours) });
  if (projectId) params.set('project_id', projectId);
  const res = await apiFetch(`/api/admin/outer-loop-convergence?${params.toString()}`, undefined, 2, 30000, signal);
  if (!res.ok) throw new Error(`Failed to fetch outer-loop convergence: ${res.status}`);
  return res.json();
}

export async function fetchOuterLoopConvergenceTrend(
  days: 7 | 14 | 30,
  projectId?: string,
  signal?: AbortSignal,
): Promise<OuterLoopConvergenceTrendResponse> {
  const params = new URLSearchParams({ days: String(days) });
  if (projectId) params.set('project_id', projectId);
  const res = await apiFetch(`/api/admin/outer-loop-convergence-trend?${params.toString()}`, undefined, 2, 30000, signal);
  if (!res.ok) throw new Error(`Failed to fetch outer-loop convergence trend: ${res.status}`);
  return res.json();
}

/** A single outer review<->fix PR-chain behind a terminal outcome / blocker. */
export interface OuterLoopChain {
  pr_number: number | null;
  task_ids: string[];
  max_iteration: number;
  churn_count: number;
  terminal_reason: string | null;
  project_id: string | null;
}

export interface OuterLoopChainsResponse {
  window_hours: number;
  reason_filter: string | null;
  total: number;
  chains: OuterLoopChain[];
}

/**
 * List the specific PR-chains behind a terminal outcome / blocker reason.
 * When `reason` is omitted the backend returns all non-merged chains (the
 * actionable set). Mirrors the retry/timeout/abort semantics of the other
 * outer-loop fetchers.
 */
export async function fetchOuterLoopChains(
  hours = 24,
  projectId?: string,
  reason?: string,
  limit = 50,
  signal?: AbortSignal,
): Promise<OuterLoopChainsResponse> {
  const params = new URLSearchParams({ hours: String(hours), limit: String(limit) });
  if (projectId) params.set('project_id', projectId);
  if (reason) params.set('reason', reason);
  const res = await apiFetch(`/api/admin/outer-loop-chains?${params.toString()}`, undefined, 2, 30000, signal);
  if (!res.ok) throw new Error(`Failed to fetch outer-loop chains: ${res.status}`);
  return res.json();
}

export interface PipelineRegression {
  signal_name: string;
  regression_class: string;
  detected_at: string | null;
  severity: string;
  current_rate: number | null;
  prior_rate: number | null;
  resolved?: boolean;
  resolved_at?: string | null;
  details?: string | null;
  spawned_task_id?: string | null;
  spawned_task_status?: string | null;
  spawned_task_title?: string | null;
  viewport_component?: string | null;
  viewport_assertion_type?: string | null;
  viewport_width?: number | null;
  [key: string]: unknown;
}

export interface PipelineRegressionsResponse {
  project_id: string;
  regressions: PipelineRegression[];
  healthy: boolean;
}

export async function fetchPipelineRegressions(
  projectId: string,
  signal?: AbortSignal,
): Promise<PipelineRegressionsResponse> {
  const params = new URLSearchParams({ project_id: projectId });
  const res = await apiFetch(
    `/api/admin/pipeline-regressions?${params.toString()}`,
    undefined,
    2,
    30000,
    signal,
  );
  if (!res.ok) throw new Error(`Failed to fetch pipeline regressions: ${res.status}`);
  return res.json();
}

export interface ImprovementLaneRow {
  id: string;
  title: string | null;
  status: string;
  direction_id: string | null;
  branch: string | null;
  last_checkpoint_phase: string | null;
  last_checkpoint_seq: number | null;
  last_checkpoint_sha: string | null;
  last_checkpoint_age_seconds: number | null;
  checkpoint_loss_count: number;
  workspace_state: string | null;
  workspace_vm_instance_id: string | null;
  reservation_expires_at: string | null;
  reserved_vm_instance_id: string | null;
  reservation_claimed_by_fix_count: number | null;
  reservation_expired_before_fix_count: number | null;
  reservation_force_expired_count: number | null;
  pilot_arm: string | null;
  phase_requeues: number;
  at_risk: boolean;
  recovery: { reason: string; attempt: number; exhausted: boolean; at: string } | null;
  llm_input_tokens_total: number | null;
  llm_output_tokens_total: number | null;
  vm_cost_usd_total: number;
  reserved_idle_cost_usd_total: number;
  shadow: boolean;
  created_at: string | null;
  updated_at: string | null;
  [key: string]: unknown;
}

export interface ShadowLaneHealth {
  shadow_lane_count: number;
  checkpoints_written: number;
  checkpoint_miss_count: number;
  exhausted_close_count: number;
  miss_rate: number | null;
}

export interface LaneRelayTimingFields {
  wall_clock_seconds: number | null;
  active_seconds: number | null;
  gap_seconds: number | null;
}

export interface LaneRelayComparison {
  source_version: {
    version_number: number;
    captured_at: string | null;
    terminal_status: string | null;
  };
  relay: LaneRelayTimingFields;
  lane: LaneRelayTimingFields;
  deltas: LaneRelayTimingFields;
}

export interface LanePilotTrialMetrics {
  verdict: 'KILL' | 'ADOPT' | 'CONTINUE';
  verdict_reason: string;
  immediate_alert: boolean;
  n_lane: number;
  n_control: number;
  lane_merge_rate: number | null;
  control_merge_rate: number | null;
  lane_cost_per_merged: number | null;
  control_cost_per_merged: number | null;
  checkpoint_loss_rate: number | null;
  warm_resume_fraction: number | null;
  checkpoint_fraction: number | null;
  lane_fix_tasks_sampled: number;
  control_warm_resume_count: number;
  gate_integrity_violations: number;
  lane_relay_comparison?: LaneRelayComparison | null;
  [key: string]: unknown;
}

export interface ImprovementLanesResponse {
  lanes: ImprovementLaneRow[];
  total_returned: number;
  offset: number;
  limit: number;
  project_id: string;
  shadow_health?: ShadowLaneHealth;
  trial_metrics?: LanePilotTrialMetrics | null;
  workspace_state_counts?: Record<string, number>;
  [key: string]: unknown;
}

export async function fetchImprovementLanes(
  projectId: string,
  limit = 50,
  offset = 0,
  signal?: AbortSignal,
): Promise<ImprovementLanesResponse> {
  const params = new URLSearchParams({
    project_id: projectId,
    limit: String(limit),
    offset: String(offset),
  });
  const res = await apiFetch(
    `/api/admin/improvement-lanes?${params.toString()}`,
    undefined,
    2,
    30000,
    signal,
  );
  if (!res.ok) throw new Error(`Failed to fetch improvement lanes: ${res.status}`);
  return res.json();
}

export interface LanePilotDrainReport {
  project_id: string;
  flag_disabled: boolean;
  reservations_cleared: number;
  lanes_killed: number;
  reason: string;
}

export async function drainLanePilot(projectId: string): Promise<LanePilotDrainReport> {
  const res = await apiFetch(
    `/api/admin/improvement-lanes/drain?project_id=${encodeURIComponent(projectId)}`,
    { method: 'POST' },
  );
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    const msg = data?.detail || data?.error?.message || `Failed to drain lane pilot: ${res.status}`;
    throw new Error(typeof msg === 'string' ? msg : `Failed to drain lane pilot: ${res.status}`);
  }
  return res.json();
}

export async function reclaimOrphanedLaneWorkspaces(projectId: string): Promise<{ reclaimed: number }> {
  const res = await apiFetch(
    `/api/admin/improvement-lanes/reclaim-orphaned?project_id=${encodeURIComponent(projectId)}`,
    { method: 'POST' },
  );
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    const msg = data?.detail || data?.error?.message || `Failed to reclaim orphaned workspaces: ${res.status}`;
    throw new Error(typeof msg === 'string' ? msg : `Failed to reclaim orphaned workspaces: ${res.status}`);
  }
  return res.json();
}

export interface LanePilotPreflightCheck {
  name: string;
  met: boolean;
  blocking_value: string;
  fix_hint: string | null;
}

export interface LanePilotPreflightResponse {
  project_id: string;
  ready: boolean;
  checks: LanePilotPreflightCheck[];
  durable_verdict: string | null;
  pilot_mode?: string;
}

export async function fetchLanePilotPreflight(
  projectId: string,
  signal?: AbortSignal,
): Promise<LanePilotPreflightResponse> {
  const res = await apiFetch(
    `/api/admin/improvement-lanes/preflight?project_id=${encodeURIComponent(projectId)}`,
    undefined,
    2,
    15000,
    signal,
  );
  if (!res.ok) throw new Error(`Failed to fetch lane pilot preflight: ${res.status}`);
  return res.json();
}

export interface ImprovementLaneCheckpoint {
  seq: number;
  phase: string;
  task_id: string;
  sha: string;
  parent_sha: string | null;
  created_at: string;
  summary: string | null;
  validated: { lint: boolean | null; tests: boolean | null };
  wip_failing: boolean | null;
  tokens_in: number | null;
  tokens_out: number | null;
  vm_cost_usd: number | null;
  warm_resume: boolean | null;
}

export interface ImprovementLaneManifest {
  phase_log?: unknown[] | null;
  remaining?: unknown[] | null;
  critical_files?: string[] | null;
  failing_tests?: string[] | null;
  risks?: string[] | null;
  progress?: string | null;
  recovery?: unknown | null;
  [key: string]: unknown;
}

export interface ImprovementLaneCurrentTask {
  task_id: string;
  status: string;
  title: string | null;
  task_type: string | null;
}

export interface ImprovementLaneDetail {
  id: string;
  title: string | null;
  goal: string | null;
  direction_id: string | null;
  version_number: number | null;
  status: string;
  branch_name: string | null;
  base_branch: string | null;
  created_at: string | null;
  parked_at: string | null;
  last_resumed_at: string | null;
  retired_at: string | null;
  updated_at: string | null;
  last_checkpoint_sha: string | null;
  last_checkpoint_phase: string | null;
  checkpoint_seq: number;
  phase_requeues: number;
  at_risk: boolean;
  checkpoints: ImprovementLaneCheckpoint[];
  manifest: ImprovementLaneManifest;
  workspace_state: string | null;
  workspace_vm_instance_id: string | null;
  pilot_arm: string | null;
  llm_input_tokens_total: number | null;
  llm_output_tokens_total: number | null;
  vm_cost_usd_total: number;
  reserved_idle_cost_usd_total: number;
  reservation_claimed_by_fix_count: number;
  reservation_expired_before_fix_count: number;
  reservation_force_expired_count: number;
  current_task_id: string | null;
  current_task: ImprovementLaneCurrentTask | null;
}

export async function fetchImprovementLaneDetail(
  projectId: string,
  laneId: string,
  signal?: AbortSignal,
): Promise<ImprovementLaneDetail> {
  const params = new URLSearchParams({ project_id: projectId });
  const res = await apiFetch(
    `/api/admin/improvement-lanes/${encodeURIComponent(laneId)}?${params.toString()}`,
    undefined,
    2,
    15000,
    signal,
  );
  if (!res.ok) throw new Error(`Failed to fetch improvement lane detail: ${res.status}`);
  return res.json();
}

export interface SrMissedRetrospectivesResponse {
  items: SrMissedTask[];
  total: number;
  /** Full list of task_type values present in the matched result set, when returned by the server. */
  task_types?: string[];
  /** Current page number (0-indexed). When present, consumers can determine if results are truncated. */
  page?: number;
  /** Items per page. When present with page, consumers can determine if results are truncated. */
  page_size?: number;
  /** Per-category miss counts across the full filtered match set (not just the current page). */
  category_counts?: Record<string, number>;
}

export interface SrCatchRateHealthResponse {
  status: 'healthy' | 'stale' | 'low_data' | 'frozen' | 'never_sufficient' | 'missing';
  data_sufficient: boolean;
  last_sufficient_computed_at: string | null;
  last_sufficient_age_hours: number | null;
  signal_last_computed_at: string | null;
  signal_age_hours: number | null;
  category_count: number;
  categories_with_hit_rate: number;
  overall_hit_rate?: number | null;
  previous_overall_hit_rate?: number | null;
  overall_trend_verdict?: string | null;
  overall_trend_significant?: boolean | null;
  bucket_mode?: string | null;
  prompt_version_changed?: boolean;
  mixed_prompt_versions?: boolean;
}

export async function fetchSrMissedRetrospectives(
  projectId: string,
  params?: {
    category?: string;
    task_type?: string;
    scope?: string;
    min_severity?: string;
    days?: number;
    page?: number;
    page_size?: number;
  },
  signal?: AbortSignal,
): Promise<SrMissedRetrospectivesResponse> {
  const searchParams = new URLSearchParams();
  if (params?.category) searchParams.set('category', params.category);
  if (params?.task_type) searchParams.set('task_type', params.task_type);
  if (params?.scope) searchParams.set('scope', params.scope);
  if (params?.min_severity) searchParams.set('min_severity', params.min_severity);
  if (params?.days !== undefined) searchParams.set('days', String(params.days));
  if (params?.page !== undefined) searchParams.set('page', String(params.page));
  if (params?.page_size !== undefined) searchParams.set('page_size', String(params.page_size));
  const query = searchParams.toString();
  const url = `/api/admin/projects/${encodeURIComponent(projectId)}/sr-missed-retrospectives${query ? `?${query}` : ''}`;
  const res = await apiFetch(url, undefined, 2, 30000, signal);
  if (!res.ok) throw new Error(`Failed to fetch SR missed retrospectives: ${res.status}`);
  return res.json();
}

export async function fetchSrCatchRateHealth(projectId: string): Promise<SrCatchRateHealthResponse> {
  const res = await apiFetch(`/api/admin/projects/${encodeURIComponent(projectId)}/sr_catch_rate/health`);
  if (!res.ok) throw new Error(`Failed to fetch SR catch-rate health: ${res.status}`);
  return res.json();
}

export interface SrUnhandledEdge {
  case: string;
  handled: boolean;
  blind_spot?: string | null;
  origin?: string | null;
}

export interface SrUnhandledEdgesTask {
  task_id: string;
  title: string;
  task_type?: string | null;
  scope?: string | null;
  created_at?: string | null;
  unhandled_edges: SrUnhandledEdge[];
  pr_url?: string | null;
}

export interface SrUnhandledEdgesRetrospectiveResponse {
  items: SrUnhandledEdgesTask[];
  total: number;
  task_types?: string[];
  // edge_case_counts is the current contract: edge `case` labels ranked by
  // frequency. blind_spot_counts is retained for backend-rollout compatibility.
  edge_case_counts?: Record<string, number>;
  blind_spot_counts?: Record<string, number>;
}

export async function fetchSrUnhandledEdgesRetrospectives(
  projectId: string,
  params?: { task_type?: string; case_pattern?: string; blind_spot?: string; days?: number; page?: number; page_size?: number },
  signal?: AbortSignal,
): Promise<SrUnhandledEdgesRetrospectiveResponse> {
  const searchParams = new URLSearchParams();
  if (params?.task_type) searchParams.set('task_type', params.task_type);
  if (params?.case_pattern) searchParams.set('case_pattern', params.case_pattern);
  if (params?.blind_spot) searchParams.set('blind_spot', params.blind_spot);
  if (params?.days !== undefined) searchParams.set('days', String(params.days));
  if (params?.page !== undefined) searchParams.set('page', String(params.page));
  if (params?.page_size !== undefined) searchParams.set('page_size', String(params.page_size));
  const query = searchParams.toString();
  const url = `/api/admin/projects/${encodeURIComponent(projectId)}/sr-unhandled-edges-retrospectives${query ? `?${query}` : ''}`;
  const res = await apiFetch(url, undefined, 2, 30000, signal);
  if (!res.ok) throw new Error(`Failed to fetch SR unhandled edges: ${res.status}`);
  return res.json();
}

export interface SrCatchRateHealth {
  status: 'healthy' | 'stale' | 'frozen' | 'low_data' | 'never_sufficient' | 'missing';
  last_sufficient_computed_at: string | null;
  last_sufficient_age_hours: number | null;
  signal_last_computed_at: string | null;
  signal_age_hours: number | null;
  data_sufficient: boolean;
}

export interface DirectionSrCatStat {
  sample_count: number;
  hit_rate?: number | null;
  miss_rate: number | null;
  examples?: Array<{ desc: string; severity: string }>;
}

export interface DirectionSrEntry {
  direction_id: string;
  direction_text?: string;
  sample_size: number;
  overall_catch_rate: number | null;
  worst_category: string | null;
  categories: Record<string, DirectionSrCatStat>;
}

export interface DirectionSrBreakdownResponse {
  directions: DirectionSrEntry[];
  min_sample: number;
}

export async function fetchDirectionSrBreakdown(
  projectId: string,
  days = 30,
): Promise<DirectionSrBreakdownResponse> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/direction-sr-breakdown?days=${days}`,
  );
  if (!res.ok) throw new Error(`Failed to fetch direction SR breakdown: ${res.status}`);
  return res.json();
}

export interface EvictedFinding {
  title: string;
  claims: string[];
  source_refs: string[];
  status: string;
  evicted_at: string;
}

export interface EvictedFindingsResponse {
  direction_id: string;
  total: number;
  offset: number;
  limit: number;
  evicted_findings: EvictedFinding[];
}

export async function fetchEvictedFindings(
  projectId: string,
  directionId: string,
  params?: { offset?: number; limit?: number },
): Promise<EvictedFindingsResponse> {
  const searchParams = new URLSearchParams();
  if (params?.offset !== undefined) searchParams.set('offset', String(params.offset));
  if (params?.limit !== undefined) searchParams.set('limit', String(params.limit));
  const query = searchParams.toString();
  const url = `/api/admin/projects/${encodeURIComponent(projectId)}/directions/${encodeURIComponent(directionId)}/evicted-findings${query ? `?${query}` : ''}`;
  const res = await apiFetch(url);
  if (!res.ok) throw new Error(`Failed to fetch evicted findings: ${res.status}`);
  return res.json();
}

export async function submitChurnRetirementDecision(
  projectId: string,
  directionId: string,
  decision: 'KEEP' | 'RETIRE',
  rationale?: string,
): Promise<void> {
  const body: Record<string, unknown> = { decision };
  if (rationale) body.rationale = rationale;
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/directions/${encodeURIComponent(directionId)}/churn-retirement-decision`,
    { method: 'POST', body: JSON.stringify(body) },
  );
  if (!res.ok) throw new Error(`Failed to submit churn-retirement decision: ${res.status}`);
}

export interface SrCategoryTrend {
  current_hit_rate?: number | null;
  previous_hit_rate?: number | null;
  miss_rate?: number | null;
  all_iterations_miss_rate?: number | null;
  weighted_miss_rate?: number | null;
  false_positive_rate?: number | null;
  precision?: number | null;
  trend?: 'increasing' | 'decreasing' | 'stable';
  rate_history?: number[];
  prompt_version_changed?: boolean | null;
  below_floor?: boolean | null;
  reasoning_engagement_rate?: number | null;
  reasoning_engagement_with?: number | null;
  reasoning_engagement_total?: number | null;
  /** Median wall-clock seconds from SR PASS stamp to reviewer/CI rejection for misses in this category. */
  time_to_miss_detect_p50_sec?: number | null;
  /** 95th-percentile seconds from SR PASS stamp to reviewer/CI rejection for misses in this category. */
  time_to_miss_detect_p95_sec?: number | null;
  /** Number of miss samples used to compute the latency percentiles. */
  time_to_miss_detect_sample_count?: number | null;
  /** True when sample_count >= 2 AND p95 > 7 days — SR is effectively the sole checker for this category. */
  time_to_miss_detect_lone_checker?: boolean | null;
  /** Precision from the previous window (hit_count / (hit_count + fp_count)), used to compute precision_delta. */
  previous_precision?: number | null;
  /**
   * Quadrant classification from (precision_delta, recall_delta) across the previous-vs-current window.
   * 'conservative_shift': precision improving, recall declining (SR flags less → misses more genuine issues).
   * 'noisy_shift': precision declining, recall improving (SR flags more → more false positives).
   * 'both_declining': both metrics worsening.
   * 'healthy': both improving.
   * 'mixed': below threshold for any directional assignment.
   */
  precision_recall_quadrant?: 'healthy' | 'conservative_shift' | 'noisy_shift' | 'both_declining' | 'mixed' | null;
  /** True when the category enters conservative_shift or noisy_shift with both deltas exceeding the alert threshold. */
  precision_recall_alert?: boolean | null;
}

export interface SrFalsePositiveCategoryStat {
  category: string;
  sample_size: number;
  false_positive_rate: number;
  fp_count: number;
  tp_count: number;
}

export interface PromptFingerprintEntry {
  fingerprint: string;
  sample_size: number;
  first_attempt_measured: number;
  first_attempt_success_rate: number | null;
  sr_skip_rate: number | null;
  rejection_category_counts: Record<string, number>;
  delta_vs_project_mean: number | null;
  sr_sample_size: number;
  sr_catch_rate: number | null;
  sr_catch_sample_size: number;
  sr_false_positive_rate: number | null;
  sr_false_positive_sample_size: number;
  sr_false_positive_rate_by_category: SrFalsePositiveCategoryStat[];
  sr_catch_rate_delta_vs_project_mean: number | null;
}

export interface PromptFingerprintAnalyticsResponse {
  project_id: string;
  window_days: number;
  sample_size: number;
  project_first_attempt_success_rate: number | null;
  project_sr_skip_rate: number | null;
  fingerprints: PromptFingerprintEntry[];
}


export interface StrategicRefreshMarker {
  requested_at?: string;
  reason?: string;
  origin?: string;
  count?: number;
  /** Coalesced operator-steering requests only (excludes automated qualification/promotion
   *  refreshes). The UI badge uses this for the '+N steers coalesced' label so the count
   *  reflects genuine human steers, not system-triggered refreshes. */
  operator_count?: number;
  excerpt?: string;
  /** Timestamp when a prior marker was consumed by the strategic loop (receipt field). */
  last_fired_at?: string;
  /** Origin of the last consumed marker ('operator' or 'qualification'). Lets the UI
   *  attribute receipts correctly — only operator-origin consumes show 'Steer picked up'. */
  last_fired_origin?: string;
}

// ── Lane Diagnostics ─────────────────────────────────────────────────────────

export interface LaneWasteTaxonomy {
  infra: number;
  merge_conflict: number;
  ci_churn: number;
  review_ambiguity: number;
  lost_reasoning: number;
  unknown: number;
  [key: string]: number;
}

export interface LaneWasteChainRow {
  chain_key: string;
  original_task_id: string | null;
  /** Raw from the chain's review_loop context, so not normalised to a string. */
  pr_number: string | number | null;
  /** Every PR this review loop crossed, de-duplicated and sorted as strings. */
  review_loop_pr_numbers: string[];
  /** True when the chain crossed at least one PR boundary (superseded rollup). */
  supersede_affected: boolean;
  project_id: string | null;
  direction_id: string | null;
  terminal_outcome: string | null;
  cap_exhausted: boolean;
  task_count: number;
  cap_counted_iterations: number;
  cap_excluded_iterations: number;
  max_iteration: number | null;
  review_count: number;
  reviews_to_converge: number | null;
  churn_count: number;
  has_drift: boolean;
  waste_totals: Record<string, number>;
  tasks: Record<string, unknown>[];
}

export interface LaneWasteDiagnostics {
  waste_taxonomy: LaneWasteTaxonomy;
  waste_task_count: number;
  cap_counted_task_count: number;
  cap_excluded_task_count: number;
  chain_count: number;
  cap_exhausted_chain_count: number;
  /** Distinct directions that lost at least one chain to the review cap. */
  cap_exhausted_direction_count: number;
  cap_exhausted_directions?: {
    direction_id: string;
    direction_text: string | null;
  }[];
  /** Chains whose review loop crossed at least one PR boundary. */
  supersede_affected_chain_count: number;
  reviews_to_converge: {
    count: number;
    median: number | null;
    p95: number | null;
  };
  /** Reviews-to-converge restricted to merged, supersede-affected chains. */
  supersede_affected_reviews_to_converge: {
    count: number;
    sample_count: number;
    median: number | null;
    p95: number | null;
  };
  /** Rollup-only supersede-affected and unaffected control populations. */
  rollup_chain_count?: number;
  rollup_supersede_affected_chain_count?: number;
  rollup_supersede_unaffected_chain_count?: number;
  rollup_supersede_affected_reviews_to_converge?: {
    count: number;
    sample_count: number;
    median: number | null;
    p95: number | null;
  };
  rollup_supersede_unaffected_reviews_to_converge?: {
    count: number;
    sample_count: number;
    median: number | null;
    p95: number | null;
  };
  retarget_review_scope_counts: {
    delta: number;
    full: number;
  };
  stale_head_discard_count: number;
  chain_limit: number;
  chains_truncated: boolean;
  chains: LaneWasteChainRow[];
}

export interface PerPrPhaseRow {
  pr_number: string;
  task_count: number;
  review_seconds: number;
  fix_seconds: number;
  gap_seconds: number;
  total_seconds: number;
  queue_seconds: number;
  provision_seconds: number;
  cold_start_seconds: number;
  execution_seconds: number;
}

export interface LaneDiagResponse {
  lane_iteration_distribution: Record<string, {
    p50_iterations: number;
    p90_iterations: number;
    max_iterations: number;
    task_count: number;
    pr_count: number;
  }>;
  per_pr_phase_breakdown: PerPrPhaseRow[];
  relay_counts_by_lane: Record<string, number>;
  pr_count: number;
  task_count: number;
  pr_breakdown_limit: number;
  pr_breakdown_truncated: boolean;
  lane_waste: LaneWasteDiagnostics;
  project_id: string | null;
  window_start: string;
  max_tasks: number;
  truncated: boolean;
  window_days?: number;
  version?: {
    version_number: number;
    phase: string;
    status: string;
    opened_at: string;
    promoted_at: string | null;
  };
  checkpoint_recovery_waste?: {
    by_reason: Record<string, number>;
    total: number;
    exhausted_count: number;
  };
}

export interface LaneBaselineTrendBlock {
  latest: number | null;
  prior: number | null;
  delta: number | null;
  classification: 'improving' | 'declining' | 'stable' | null;
  /** Per-version measurement provenance; optional for older API responses. */
  availability?: {
    latest: 'available' | 'evicted' | 'unavailable';
    prior: 'available' | 'evicted' | 'unavailable';
  };
}

export interface LaneBaselineDirectionRecord {
  direction_id: string;
  direction_text: string | null;
}

export interface LaneBaselineTrend {
  latest_version: number | null;
  prior_version: number | null;
  relay_medians: {
    wall_clock_seconds: LaneBaselineTrendBlock;
    active_seconds: LaneBaselineTrendBlock;
    gap_seconds: LaneBaselineTrendBlock;
    task_count: LaneBaselineTrendBlock;
  };
  lane_iterations: {
    conflict: { p50: LaneBaselineTrendBlock; p90: LaneBaselineTrendBlock };
    ci: { p50: LaneBaselineTrendBlock; p90: LaneBaselineTrendBlock };
    review_convergence: { p50: LaneBaselineTrendBlock; p90: LaneBaselineTrendBlock };
  };
  waste_taxonomy_shares: Record<string, LaneBaselineTrendBlock>;
  checkpoint_recovery_waste_total: LaneBaselineTrendBlock;
  outer_loop_convergence: {
    iteration_median: LaneBaselineTrendBlock;
    iteration_p95: LaneBaselineTrendBlock;
    convergence_rate: LaneBaselineTrendBlock;
    cap_exhausted_chain_count: LaneBaselineTrendBlock;
    cap_exhausted_direction_count: LaneBaselineTrendBlock;
    cap_exhausted_directions?: {
      latest: LaneBaselineDirectionRecord[];
      prior: LaneBaselineDirectionRecord[];
    };
    supersede_affected_reviews_to_converge_median: LaneBaselineTrendBlock;
    supersede_affected_reviews_to_converge_p95: LaneBaselineTrendBlock;
    rollup_supersede_affected_reviews_to_converge_median?: LaneBaselineTrendBlock;
    rollup_supersede_affected_reviews_to_converge_p95?: LaneBaselineTrendBlock;
    rollup_supersede_unaffected_reviews_to_converge_median?: LaneBaselineTrendBlock;
    rollup_supersede_unaffected_reviews_to_converge_p95?: LaneBaselineTrendBlock;
  };
  retarget_convergence: {
    reviews_to_converge: {
      count: LaneBaselineTrendBlock;
      median: LaneBaselineTrendBlock;
      p95: LaneBaselineTrendBlock;
    };
    retarget_review_scope_counts: {
      delta: LaneBaselineTrendBlock;
      full: LaneBaselineTrendBlock;
    };
    stale_head_discard_count: LaneBaselineTrendBlock;
  };
}

export interface LaneBaselineConvergence {
  iteration_median: number | null;
  iteration_p95: number | null;
  convergence_rate: number | null;
  cap_exhausted_chain_count: number;
  /** Distinct directions that lost at least one chain to the review cap. */
  cap_exhausted_direction_count?: number;
  cap_exhausted_directions?: {
    direction_id: string;
    direction_text: string | null;
  }[];
  /** Chains that crossed at least one PR boundary (superseded rollups). */
  supersede_affected_chain_count?: number;
  supersede_affected_merged_chain_count?: number;
  supersede_affected_reviews_to_converge_count?: number;
  supersede_affected_reviews_to_converge_sample_count?: number;
  supersede_affected_reviews_to_converge_median?: number | null;
  supersede_affected_reviews_to_converge_p95?: number | null;
  rollup_chain_count?: number;
  rollup_supersede_affected_chain_count?: number;
  rollup_supersede_unaffected_chain_count?: number;
  rollup_supersede_affected_reviews_to_converge_count?: number;
  rollup_supersede_affected_reviews_to_converge_sample_count?: number;
  rollup_supersede_affected_reviews_to_converge_median?: number | null;
  rollup_supersede_affected_reviews_to_converge_p95?: number | null;
  rollup_supersede_unaffected_reviews_to_converge_count?: number;
  rollup_supersede_unaffected_reviews_to_converge_sample_count?: number;
  rollup_supersede_unaffected_reviews_to_converge_median?: number | null;
  rollup_supersede_unaffected_reviews_to_converge_p95?: number | null;
  terminal_outcome_counts: Record<string, number>;
}

export interface LaneBaseline {
  version_number: number;
  /** Fields removed only because the persisted snapshot exceeded its byte budget. */
  dropped_fields?: string[];
  outer_loop_convergence?: LaneBaselineConvergence;
  [key: string]: unknown;
}

export interface LaneBaselinesResponse {
  project_id: string;
  count: number;
  baselines: LaneBaseline[];
  trend: LaneBaselineTrend | null;
}

export async function fetchLaneBaselines(
  projectId: string,
  signal?: AbortSignal,
): Promise<LaneBaselinesResponse> {
  const res = await apiFetch(
    `/api/admin/lane-baselines?project_id=${encodeURIComponent(projectId)}`,
    undefined,
    2,
    30000,
    signal,
  );
  if (!res.ok) throw new Error(`Failed to fetch lane baselines: ${res.status}`);
  return res.json();
}

// --- Evergreen fill observability + backlog ----------------------------------
