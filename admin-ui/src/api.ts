import { getIdToken } from './firebase';

export const API_BASE_URL = import.meta.env.VITE_API_URL || '';

export const COST_PER_VM_HR = 0.067;  // $/hr, e2-standard-2 on-demand VM cost

export async function authHeaders(): Promise<Record<string, string>> {
  const token = await getIdToken();
  return { 'Authorization': `Bearer ${token}`, 'Content-Type': 'application/json' };
}

export async function authGet(url: string) {
  const token = await getIdToken();
  return fetch(url, { headers: { 'Authorization': `Bearer ${token}` } });
}

// In-flight dedup for idempotent reads: when several panels poll the same
// endpoint within the same render tick (DashboardHome + CatchUpPanel both
// hitting /api/admin/digest, etc.), we collapse them onto one underlying
// fetch and clone the Response per caller. Map entries auto-delete when the
// shared promise settles, so the map cannot grow without bound.
const inFlightRequests = new Map<string, Promise<Response>>();

/**
 * Fetch wrapper with auth headers and retry for transient Cloud Run errors.
 * Cloud Run 502/503 responses bypass FastAPI middleware and lack CORS headers,
 * causing the browser to report CORS errors. Retrying handles this gracefully.
 *
 * Concurrent calls to the same idempotent endpoint share a single underlying
 * fetch; each caller receives an independent Response.clone() so .json()/
 * .text() consumption stays isolated.
 */
export async function apiFetch(
  url: string,
  options?: RequestInit,
  retries = 2,
  timeout = 30000
): Promise<Response> {
  const headers = await authHeaders();
  const fullUrl = url.startsWith('http') ? url : `${API_BASE_URL}${url}`;
  const mergedOptions = { ...options, headers: { ...headers, ...options?.headers } };

  // Only retry on safe (idempotent) methods to avoid double-mutation
  const method = (options?.method || 'GET').toUpperCase();
  const safeToRetry = method === 'GET' || method === 'HEAD' || method === 'OPTIONS';
  const maxAttempts = safeToRetry ? retries : 0;

  // Only dedup idempotent reads. POST/PUT/DELETE are intentionally separate
  // requests even when fired in parallel — the body's identity is too
  // expensive to hash reliably and the semantics rarely overlap.
  const dedupKey = safeToRetry ? `${method} ${fullUrl}` : null;
  if (dedupKey) {
    const existing = inFlightRequests.get(dedupKey);
    if (existing) {
      const shared = await existing;
      return shared.clone();
    }
  }

  const attemptFetch = async () => {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeout);
    try {
      const res = await fetch(fullUrl, { ...mergedOptions, signal: controller.signal });
      clearTimeout(timer);
      return res;
    } catch (e) {
      clearTimeout(timer);
      if (e instanceof Error && e.name === 'AbortError') throw new Error('Request timed out');
      throw e;
    }
  };

  const run = (async () => {
    for (let i = 0; i <= maxAttempts; i++) {
      try {
        const res = await attemptFetch();
        if (res.ok || res.status < 500) return res;
      } catch (e) {
        if (i === maxAttempts) throw e;
      }
      await new Promise(r => setTimeout(r, 1000 * (i + 1)));
    }
    return attemptFetch();
  })();

  if (dedupKey) {
    inFlightRequests.set(dedupKey, run);
    run.finally(() => {
      // Only delete if this exact promise is still the one cached — guards
      // against a racy fast-follow request that already replaced it.
      if (inFlightRequests.get(dedupKey) === run) {
        inFlightRequests.delete(dedupKey);
      }
    });
  }

  const res = await run;
  // Clone for the first caller too so the in-flight Response can still
  // be cloned by late joiners that arrive between resolution and the
  // .finally() cleanup.
  return dedupKey ? res.clone() : res;
}

export async function fetchTask(taskId: string): Promise<import('./components/TaskCard').Task> {
  const res = await apiFetch(`/api/admin/tasks/${encodeURIComponent(taskId)}`);
  if (!res.ok) throw new Error(`Failed to fetch task: ${res.status}`);
  return res.json();
}

export async function fetchTaskSerialConsole(taskId: string): Promise<import('./types/task').SerialConsoleResponse> {
  const res = await apiFetch(`/api/admin/tasks/${encodeURIComponent(taskId)}/serial-console`);
  if (!res.ok) throw new Error(`Failed to fetch serial console: ${res.status}`);
  return res.json();
}

export async function fetchAutonomyDebug(projectId: string): Promise<unknown> {
  const res = await apiFetch(`/api/admin/projects/${encodeURIComponent(projectId)}/autonomy-debug`);
  if (!res.ok) throw new Error(`Failed to fetch autonomy debug: ${res.status}`);
  return res.json();
}

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

export interface SrMissedRetrospectivesResponse {
  items: import('./components/SrTrendBreakdown').SrMissedTask[];
  total: number;
}

export async function fetchSrMissedRetrospectives(
  projectId: string,
  params?: {
    category?: string;
    task_type?: string;
    days?: number;
    page?: number;
    page_size?: number;
  }
): Promise<SrMissedRetrospectivesResponse> {
  const searchParams = new URLSearchParams();
  if (params?.category) searchParams.set('category', params.category);
  if (params?.task_type) searchParams.set('task_type', params.task_type);
  if (params?.days !== undefined) searchParams.set('days', String(params.days));
  if (params?.page !== undefined) searchParams.set('page', String(params.page));
  if (params?.page_size !== undefined) searchParams.set('page_size', String(params.page_size));
  const query = searchParams.toString();
  const url = `/api/admin/projects/${encodeURIComponent(projectId)}/sr-missed-retrospectives${query ? `?${query}` : ''}`;
  const res = await apiFetch(url);
  if (!res.ok) throw new Error(`Failed to fetch SR missed retrospectives: ${res.status}`);
  return res.json();
}

export interface SrRecentMiss {
  task_id: string;
  title: string;
  scope: string | null;
  task_type: string | null;
  created_at?: string | null;
  missed_categories: string[];
  caught_categories: string[];
  pr_url: string | null;
  sr_check_results?: Record<string, { passed: boolean; issue?: string | null; origin?: string | null }>;
  reviewer_findings?: Array<{ category: string; severity?: string | null; description: string }>;
}

export interface SrRecentMissesResponse {
  misses: SrRecentMiss[];
  total: number;
}

export async function fetchSrRecentMisses(
  projectId: string,
  params?: { limit?: number; category?: string; days?: number },
): Promise<SrRecentMissesResponse> {
  const searchParams = new URLSearchParams();
  if (params?.limit !== undefined) searchParams.set('limit', String(params.limit));
  if (params?.category) searchParams.set('category', params.category);
  if (params?.days !== undefined) searchParams.set('days', String(params.days));
  const query = searchParams.toString();
  const url = `/api/admin/projects/${encodeURIComponent(projectId)}/sr-recent-misses${query ? `?${query}` : ''}`;
  const res = await apiFetch(url);
  if (!res.ok) throw new Error(`Failed to fetch SR recent misses: ${res.status}`);
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
}

export async function fetchSrUnhandledEdgesRetrospectives(
  projectId: string,
  params?: { task_type?: string; days?: number; page?: number; page_size?: number },
): Promise<SrUnhandledEdgesRetrospectiveResponse> {
  const searchParams = new URLSearchParams();
  if (params?.task_type) searchParams.set('task_type', params.task_type);
  if (params?.days !== undefined) searchParams.set('days', String(params.days));
  if (params?.page !== undefined) searchParams.set('page', String(params.page));
  if (params?.page_size !== undefined) searchParams.set('page_size', String(params.page_size));
  const query = searchParams.toString();
  const url = `/api/admin/projects/${encodeURIComponent(projectId)}/sr-unhandled-edges-retrospectives${query ? `?${query}` : ''}`;
  const res = await apiFetch(url);
  if (!res.ok) throw new Error(`Failed to fetch SR unhandled edges: ${res.status}`);
  return res.json();
}

export interface DirectionSrCatStat {
  sample_count: number;
  hit_rate?: number | null;
  miss_rate: number | null;
  examples?: Array<{ desc: string; severity: string }>;
  sr_alone_hit_count?: number | null;
  reviewer_save_hit_count?: number | null;
  sr_alone_hit_rate?: number | null;
  reviewer_save_rate?: number | null;
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

export interface DispatcherRejectionReasons {
  dedup: number;
  cooldown: number;
  system_cap: number;
  manual_intervention_suppressed: number;
}

export interface DispatcherCycleRecord {
  cycle_started_at: string;
  cycle_finished_at: string | null;
  project_id: string;
  signals_evaluated: number;
  signals_tripped: number;
  spawns_emitted: number;
  rejections_by_reason: DispatcherRejectionReasons;
  mttd_p50_sec: number | null;
}

export interface DispatcherClassStatus {
  regression_class: string;
  project_id: string;
  last_spawn_at: string | null;
  last_rejection_reason: string | null;
  in_cooldown_until: string | null;
}

export interface DispatcherHealthSummary {
  cycles_24h: number;
  signals_tripped_24h: number;
  spawns_24h: number;
  cooldown_rejections_24h: number;
  cap_rejections_24h: number;
}

export interface DispatcherHealthResponse {
  summary: DispatcherHealthSummary;
  recent_cycles: DispatcherCycleRecord[];
  class_statuses: DispatcherClassStatus[];
}

export async function fetchDispatcherHealth(): Promise<DispatcherHealthResponse> {
  const res = await apiFetch('/api/admin/regression-dispatcher/health');
  if (!res.ok) throw new Error(`Failed to fetch dispatcher health: ${res.status}`);
  return res.json();
}

export interface SrAlertDrilldownRow {
  task_id: string;
  title: string;
  missed_categories: string[];
  sr_verdict: string | null;
  pr_url?: string | null;
}

export interface SrAlertDrilldownResponse {
  items: SrAlertDrilldownRow[];
  total: number;
  alert_type: string;
  categories: string[];
}

export async function fetchSrAlertDrilldown(
  projectId: string,
  categories: string[],
  alertType: 'trends' | 'floor' = 'trends',
  pageSize = 50,
): Promise<SrAlertDrilldownResponse> {
  const params = new URLSearchParams({ alert_type: alertType, page_size: String(pageSize) });
  categories.forEach(c => params.append('categories', c));
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/sr-alert-drilldown?${params}`,
  );
  if (!res.ok) throw new Error(`Failed to fetch SR alert drilldown: ${res.status}`);
  return res.json();
}

export interface SrCategoryTrend {
  current_hit_rate?: number | null;
  previous_hit_rate?: number | null;
  miss_rate?: number | null;
  all_iterations_miss_rate?: number | null;
  weighted_miss_rate?: number | null;
  false_positive_rate?: number | null;
  precision?: number | null;
  sr_alone_hit_count?: number | null;
  reviewer_save_hit_count?: number | null;
  sr_alone_hit_rate?: number | null;
  reviewer_save_rate?: number | null;
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

export interface SrFixIterationBucket {
  hit_rate?: number | null;
  miss_rate?: number | null;
  sample_count?: number | null;
}

export type SrFixIterationBreakdown = Record<string, SrFixIterationBucket>;

export interface SrFalsePositiveCategoryStat {
  category: string;
  sample_size: number;
  false_positive_rate: number;
  fp_count: number;
  tp_count: number;
}

export interface SrReviewerDisagreementCategory {
  category: string;
  sample_size: number;
  reviewer_overrode_sr_count: number;
  sr_overrode_reviewer_count: number;
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
  reviewer_rescue_rate: number | null;
  reviewer_rescue_sample_size: number;
  sr_false_positive_rate: number | null;
  sr_false_positive_sample_size: number;
  sr_false_positive_rate_by_category: SrFalsePositiveCategoryStat[];
  sr_reviewer_disagreement_top_categories: SrReviewerDisagreementCategory[];
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

export async function fetchPromptFingerprintAnalytics(
  projectId: string,
  params?: { days?: number; min_sample_size?: number; top_n?: number },
): Promise<PromptFingerprintAnalyticsResponse> {
  const searchParams = new URLSearchParams();
  if (params?.days !== undefined) searchParams.set('days', String(params.days));
  if (params?.min_sample_size !== undefined) searchParams.set('min_sample_size', String(params.min_sample_size));
  if (params?.top_n !== undefined) searchParams.set('top_n', String(params.top_n));
  const query = searchParams.toString();
  const url = `/api/admin/projects/${encodeURIComponent(projectId)}/prompt-fingerprint-analytics${query ? `?${query}` : ''}`;
  const res = await apiFetch(url);
  if (!res.ok) throw new Error(`Failed to fetch prompt fingerprint analytics: ${res.status}`);
  return res.json();
}

export interface RegressionClassKpiRow {
  regression_class: string;
  cases_opened: number;
  cases_closed: number;
  dispatch_attempt_count: number;
  remediation_success_rate: number | null;
  mean_time_to_detect_seconds: number | null;
  mean_time_to_remediate_seconds: number | null;
}

export interface RegressionClassKpisResponse {
  project_id: string;
  window_hours: number;
  class_count: number;
  classes: RegressionClassKpiRow[];
}

export async function fetchRegressionClassKpis(
  projectId: string,
  windowHours = 168,
): Promise<RegressionClassKpisResponse> {
  const res = await apiFetch(
    `/api/admin/regression-class-kpis?project_id=${encodeURIComponent(projectId)}&window_hours=${windowHours}`,
  );
  if (!res.ok) throw new Error(`Failed to fetch regression class KPIs: ${res.status}`);
  return res.json();
}

export interface RegressionTrendBucket {
  date: string;
  opened: number;
  closed: number;
}

export interface RegressionRateTrendResponse {
  project_id: string;
  days: number;
  daily: RegressionTrendBucket[];
  by_class: Record<string, RegressionTrendBucket[]>;
}

export async function fetchRegressionRateTrend(
  projectId: string,
  windowDays = 30,
): Promise<RegressionRateTrendResponse> {
  const res = await apiFetch(
    `/api/admin/regression-cases/trend?project_id=${encodeURIComponent(projectId)}&days=${windowDays}`,
  );
  if (!res.ok) throw new Error(`Failed to fetch regression rate trend: ${res.status}`);
  return res.json();
}

export interface LayerCoverageCombo { layers: string[]; count: number; }
export interface LayerCoverageEntry { total_fixes_with_layers: number; combinations: LayerCoverageCombo[]; }
export type LayerCoverageByClass = Record<string, LayerCoverageEntry>;

export async function fetchLayerCoverageByClass(
  projectId: string,
): Promise<LayerCoverageByClass | null> {
  const res = await apiFetch(
    `/api/admin/regression-cases?project_id=${encodeURIComponent(projectId)}`,
  );
  if (!res.ok) throw new Error(`Failed to fetch regression cases: ${res.status}`);
  const data = await res.json();
  return (data.summary?.layer_coverage_by_class as LayerCoverageByClass) ?? null;
}

export interface CanonicalRegressionLayer {
  name: string;
  description: string;
}

export interface CanonicalRegressionLayersResponse {
  layers: CanonicalRegressionLayer[];
}

// Canonical regression layers in stable display order (single source of truth from backend)
export const CANONICAL_LAYER_NAMES = ['CI_WORKFLOW', 'AGENT_VM', 'AGENT_VERB', 'BASELINE_GATE', 'REVIEWER_PASS', 'VIEWPORT'] as const;
export type CanonicalLayerName = typeof CANONICAL_LAYER_NAMES[number];

export async function fetchCanonicalRegressionLayers(): Promise<CanonicalRegressionLayersResponse> {
  const res = await apiFetch('/api/admin/canonical-regression-layers');
  if (!res.ok) throw new Error(`Failed to fetch canonical regression layers: ${res.status}`);
  return res.json();
}

export interface CrossLayerComplianceByClass {
  compliant: number;
  non_compliant: number;
  awaiting_backfill: number;
  compliance_rate: number | null;
}

export interface RecurrenceByBucketEntry {
  merged_count: number;
  retripped_count: number;
  recurrence_rate: number | null;
  median_hold_days: number | null;
}

export interface CrossLayerComplianceResponse {
  total_classified: number;
  compliant: number;
  non_compliant: number;
  awaiting_backfill_count: number;
  compliance_rate: number | null;
  failure_modes: {
    doc_test_only: number;
    most_recent_layer_only: number;
  };
  by_class: Record<string, CrossLayerComplianceByClass>;
  recurrence_by_bucket?: Record<string, RecurrenceByBucketEntry>;
}

export async function fetchCrossLayerCompliance(
  projectId: string,
  windowDays = 30,
): Promise<CrossLayerComplianceResponse> {
  const res = await apiFetch(
    `/api/admin/regression-cases/cross-layer-compliance?project_id=${encodeURIComponent(projectId)}&window_days=${windowDays}`,
  );
  if (!res.ok) throw new Error(`Failed to fetch cross-layer compliance: ${res.status}`);
  return res.json();
}

export interface MttaStageStat {
  p50_sec: number | null;
  p90_sec: number | null;
  sample_count: number;
}

export interface MttaActiveCase {
  case_id: string;
  status: string;
  live_elapsed_sec: number | null;
  live_elapsed_investigate_sec: number | null;
  recovery_check_in_sec: number | null;
}

export interface MttaClassRow {
  regression_class: string;
  sample_count: number;
  stages: {
    signal_trip_to_detect: MttaStageStat;
    detect_to_investigate: MttaStageStat;
    investigate_to_merge: MttaStageStat;
    merge_to_recovery: MttaStageStat;
  };
  active_cases: MttaActiveCase[];
}

export interface RegressionMttaResponse {
  project_id: string | null;
  window_days: number;
  class_count: number;
  classes: MttaClassRow[];
  recovery_cooldown_sec: number;
}

export async function fetchRegressionMtta(
  params?: { project_id?: string; window_days?: number },
): Promise<RegressionMttaResponse> {
  const searchParams = new URLSearchParams();
  if (params?.project_id) searchParams.set('project_id', params.project_id);
  if (params?.window_days !== undefined) searchParams.set('window_days', String(params.window_days));
  const query = searchParams.toString();
  const res = await apiFetch(`/api/admin/regression-dispatcher/mtta${query ? `?${query}` : ''}`);
  if (!res.ok) throw new Error(`Failed to fetch regression MTTA: ${res.status}`);
  return res.json();
}

export async function onboardProject(
  owner: string,
  repo: string,
  name?: string,
  buildConfigOverride?: Record<string, unknown>,
  promotionTarget?: string,
): Promise<{ project_id: string; build_config: Record<string, unknown> }> {
  const body: Record<string, unknown> = { github_owner: owner, github_repo: repo };
  if (name) body.name = name;
  if (buildConfigOverride) body.build_config_override = buildConfigOverride;
  if (promotionTarget) body.promotion_target = promotionTarget;
  const res = await apiFetch('/api/admin/projects/onboard', { method: 'POST', body: JSON.stringify(body) });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    const msg = (data?.error?.message) || (data?.detail) || `Error ${res.status}`;
    throw new Error(msg);
  }
  return res.json();
}

export interface EfficiencyHistoryPoint {
  captured_at: string | null;
  value: number | null;
}

export interface EfficiencyAxis {
  current: number | null;
  baseline: number | null;
  drift_pct: number | null;
  history: EfficiencyHistoryPoint[];
}

export interface EfficiencyAxesResponse {
  axes: Record<string, EfficiencyAxis>;
  baseline_captured_at: string | null;
  last_snapshot_at: string | null;
}

export async function fetchEfficiencyAxes(projectId: string): Promise<EfficiencyAxesResponse> {
  const res = await apiFetch(`/api/admin/projects/${encodeURIComponent(projectId)}/efficiency-axes`);
  if (!res.ok) throw new Error(`Failed to fetch efficiency axes: ${res.status}`);
  return res.json();
}

export interface CiResolution {
  state: 'green' | 'red' | 'pending' | 'no_checks';
  resolved_sha: string | null;
  detail: string;
}

export interface VersionQualificationSubScores {
  audit: number;
  test_erosion: number;
  smoketest: number;
  ci_resolution?: CiResolution | null;
}

export type QualificationStatus =
  | 'not_promoted'
  | 'dry_run'
  | 'already_dry_run'
  | 'promoted'
  | 'promotion_failed'
  | 'error'
  | 'sha_error';

export interface VersionQualification {
  status: QualificationStatus | null;
  confidence: number | null;
  threshold: number | null;
  sub_scores: VersionQualificationSubScores | null;
  ci_resolution: CiResolution | null;
  blocker: string | null;
  finalized_at: string | null;
}

export interface CurrentVersion {
  version_number: number;
  phase: string;
  phase_changed_at: string | null;
  phase_reasoning: string | null;
  goal: string | null;
  opened_at: string;
  qualification_attempts: number;
  merged_task_count: number;
  last_qualification: VersionQualification | null;
}

export interface RecentVersion {
  version_number: number;
  status: string;
  opened_at: string;
  promoted_at: string | null;
  qualification_attempts: number;
  last_qualification: VersionQualification | null;
}

export interface PromotionReadinessConfidence {
  mean: number;
  stdev: number;
  min: number;
  max: number;
  stable: boolean;
}

export interface PromotionReadiness {
  ready: boolean;
  reason: string;
  verdicts_evaluated: number;
  would_promote_count: number;
  would_promote_rate: number;
  consecutive_clean_passes: number;
  confidence: PromotionReadinessConfidence;
  recurring_blockers: Record<string, number>;
  blocker_counts: Record<string, number>;
  thresholds: Record<string, number>;
}

export interface VersionLifecycleResponse {
  current_version: CurrentVersion | null;
  recent_versions: RecentVersion[];
  promotion_enabled?: boolean;
  promotion_readiness?: PromotionReadiness | null;
}

export async function fetchVersionLifecycle(projectId: string): Promise<VersionLifecycleResponse> {
  const res = await apiFetch(`/api/admin/projects/${encodeURIComponent(projectId)}/version-lifecycle`);
  if (!res.ok) throw new Error(`Failed to fetch version lifecycle: ${res.status}`);
  return res.json();
}

export interface SetVersionPhaseResponse {
  version_number: number;
  phase: string;
  phase_changed_at: string | null;
  phase_reasoning: string | null;
}

export async function setVersionPhase(
  projectId: string,
  phase: 'build' | 'stabilize',
  reasoning?: string,
): Promise<SetVersionPhaseResponse> {
  const res = await apiFetch(`/api/admin/projects/${encodeURIComponent(projectId)}/version-phase`, {
    method: 'POST',
    body: JSON.stringify({ phase, reasoning: reasoning ?? '' }),
  });
  if (!res.ok) throw new Error(`Failed to set version phase: ${res.status}`);
  return res.json();
}

export type QualifyTriggerStatus =
  | 'wrong_phase'
  | 'already_pending'
  | 'project_not_found'
  | 'no_credentials'
  | 'sha_error'
  | 'version_not_found'
  | 'already_dry_run'
  | 'spawn_failed'
  | 'not_promoted'
  | 'dry_run'
  | 'promoted'
  | 'promotion_failed'
  | 'error';

export interface QualifyTriggerResponse {
  status: QualifyTriggerStatus;
  error?: string | null;
  conflict?: boolean | null;
}

export async function triggerQualify(projectId: string): Promise<QualifyTriggerResponse> {
  const res = await apiFetch(`/api/admin/projects/${encodeURIComponent(projectId)}/qualify`, {
    method: 'POST',
  });
  if (!res.ok) throw new Error(`Failed to trigger qualify: ${res.status}`);
  return res.json();
}

export interface QualificationVerdictRecord {
  id: string;
  release_version_id: string;
  candidate_sha: string;
  confidence: number | null;
  sub_scores: VersionQualificationSubScores | null;
  blocker: string | null;
  would_promote: boolean | null;
  promotion_enabled: boolean | null;
  terminal_status: string | null;
  threshold: number | null;
  started_at: string | null;
  finalized_at: string | null;
  created_at: string;
}

export interface QualificationVerdictsResponse {
  verdicts: QualificationVerdictRecord[];
  total: number;
}

export async function fetchQualificationVerdicts(
  projectId: string,
  limit = 20,
): Promise<QualificationVerdictsResponse> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/qualification-verdicts?limit=${limit}`,
  );
  if (!res.ok) throw new Error(`Failed to fetch qualification verdicts: ${res.status}`);
  return res.json();
}
