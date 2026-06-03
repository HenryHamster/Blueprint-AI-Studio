import { getIdToken } from './firebase';
import type { SrMissedTask } from './components/SrRetrospectiveShared';
import type { WarmPoolTargets, WarmPoolTargetSetResponse } from './types/warmVm';
import type { ProblemModelPatch } from './types';
import type { MergeVerificationVerdict } from './types/project';

/** Base URL for API requests to the Blueprint backend. Defaults to empty string for same-origin requests. */
export const API_BASE_URL = import.meta.env.VITE_API_URL || '';

/** Hourly cost in USD for a standard e2-standard-2 GCP VM on-demand pricing. Used to calculate task cost estimates. */
export const COST_PER_VM_HR = 0.067;

/**
 * Generate Firebase authentication headers for API requests.
 * @returns Promise resolving to headers object with Authorization (Bearer token) and Content-Type
 */
export async function authHeaders(): Promise<Record<string, string>> {
  const token = await getIdToken();
  return { 'Authorization': `Bearer ${token}`, 'Content-Type': 'application/json' };
}

/**
 * Fetch a URL with Firebase authentication headers.
 * @param url — The URL to fetch
 * @returns Promise resolving to fetch Response
 */
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
  timeout = 30000,
  signal?: AbortSignal
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
    // Forward external abort to the internal timeout controller so both
    // sources can cancel the underlying fetch.
    const onExternalAbort = () => controller.abort();
    signal?.addEventListener('abort', onExternalAbort, { once: true });
    try {
      const res = await fetch(fullUrl, { ...mergedOptions, signal: controller.signal });
      clearTimeout(timer);
      signal?.removeEventListener('abort', onExternalAbort);
      return res;
    } catch (e) {
      clearTimeout(timer);
      signal?.removeEventListener('abort', onExternalAbort);
      if (e instanceof Error && e.name === 'AbortError') {
        // Distinguish external caller abort (should propagate as AbortError)
        // from internal timeout (should surface as a timeout message).
        if (signal?.aborted) throw e;
        throw new Error('Request timed out');
      }
      throw e;
    }
  };

  const run = (async () => {
    for (let i = 0; i <= maxAttempts; i++) {
      try {
        const res = await attemptFetch();
        if (res.ok || res.status < 500) return res;
      } catch (e) {
        // Never retry on external abort — the caller already moved on.
        if (i === maxAttempts || (e instanceof Error && e.name === 'AbortError')) throw e;
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

/**
 * Response envelope for the `sr-reviewer-disagreements` endpoint.
 *
 * `total_entries` is the legacy field (= number of returned entries, i.e. the
 * truncated page slice) and is always emitted for backward compatibility. The
 * additive `total_distinct_categories` / `returned_entries` fields report the
 * true distinct-category total for the window independently from the truncated
 * `disagreements` list; they are optional so a frontend can still render
 * sensibly against an older backend during a mixed deploy.
 */
export interface SrReviewerDisagreementsEnvelope {
  window_days: number;
  total_entries: number;
  total_distinct_categories?: number;
  returned_entries?: number;
  disagreements: import('./components/SrTrendBreakdown').SrReviewerDisagreement[];
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

export async function onboardProject(
  owner: string,
  repo: string,
  name?: string,
  buildConfigOverride?: Record<string, unknown>,
  promotionTarget?: string,
  goal?: string,
  initialSteering?: Pick<ProblemModelPatch, 'context' | 'constraints'>,
): Promise<{
  project_id: string;
  build_config: Record<string, unknown>;
  default_branch?: string;
  /** Backend-computed merge-gate verdict for the freshly-onboarded repo (sibling of build_config). */
  merge_verification_verdict?: MergeVerificationVerdict;
}> {
  const body: Record<string, unknown> = { github_owner: owner, github_repo: repo };
  if (name) body.name = name;
  if (buildConfigOverride) body.build_config_override = buildConfigOverride;
  if (promotionTarget) body.promotion_target = promotionTarget;
  if (goal) body.goal = goal;
  if (initialSteering) {
    const steering: Record<string, unknown> = {};
    const contextEntries = Object.entries(initialSteering.context ?? {})
      .map(([key, value]) => [key, typeof value === 'string' ? value.trim() : ''] as const)
      .filter(([, value]) => value.length > 0);
    if (contextEntries.length > 0) steering.context = Object.fromEntries(contextEntries);

    const constraints = initialSteering.constraints?.map((constraint) => constraint.trim()).filter((constraint) => constraint.length > 0);
    if (constraints && constraints.length > 0) steering.constraints = constraints;

    if (Object.keys(steering).length > 0) body.initial_steering = steering;
  }
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
  priced: boolean;
}

export interface LlmCostBreakdownResponse {
  window_days: number;
  generated_at: string;
  by_provider: Array<Omit<LlmCostModelRow, 'model' | 'priced'>>;
  by_model: LlmCostModelRow[];
  totals: {
    total_input_tokens: number;
    total_output_tokens: number;
    total_cost_usd: number;
    task_count: number;
    priced_task_count: number;
    unpriced_task_count: number;
    unpriced_coverage_fraction: number | null;
  };
  unpriced_combos: string[];
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
  | 'sha_error'
  | 'infra_failure';

export interface AuditReconciliation {
  model_reported_score: number | null;
  findings_derived_score: number | null;
  score_delta: number | null;
  excluded_findings_count: number;
}

export interface AuditFinding {
  severity: string;
  summary: string;
}

export interface LatestAudit {
  audit_task_id: string;
  task_status: string;
  candidate_sha: string | null;
  created_at: string | null;
  report: {
    findings?: AuditFinding[];
    audit_score?: number | null;
    test_erosion?: number | null;
  } | null;
}

export interface VersionQualification {
  status: QualificationStatus | null;
  confidence: number | null;
  threshold: number | null;
  sub_scores: VersionQualificationSubScores | null;
  ci_resolution: CiResolution | null;
  blocker: string | null;
  finalized_at: string | null;
  // Infra-failure context (populated only when status=infra_failure).
  error?: string | null;
  consecutive_infra_errors?: number | null;
  // Audit observability: why the audit sub-score is missing/unscoreable.
  audit_missing_reason?: string | null;
  // Audit reconciliation: model vs findings-derived scores and excluded count.
  audit_reconciliation?: AuditReconciliation | null;
  // Audit findings carried from the qualification run (severity + summary per finding).
  audit_findings?: AuditFinding[] | null;
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
  latest_audit?: LatestAudit | null;
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
  audit_missing_reason: string | null;
  would_promote: boolean | null;
  promotion_enabled: boolean | null;
  terminal_status: string | null;
  threshold: number | null;
  started_at: string | null;
  finalized_at: string | null;
  created_at: string;
}

export interface QualificationVerdictsInfraContext {
  infra_failure_count: number;
  consecutive_infra_errors: number;
}

export interface QualificationVerdictsResponse {
  verdicts: QualificationVerdictRecord[];
  total: number;
  infra_context?: QualificationVerdictsInfraContext;
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

export interface PrFileEntry {
  filename: string;
  status: string;
  additions: number;
  deletions: number;
  changes: number;
  patch?: string;
}

export interface PrFilesResponse {
  files: PrFileEntry[] | null;
  pr_number: number | null;
  reason?: string;
}

export async function fetchTaskPrFiles(
  taskId: string,
  includePatch = false,
): Promise<PrFilesResponse> {
  const url = `/api/admin/tasks/${encodeURIComponent(taskId)}/pr-files${includePatch ? '?include_patch=true' : ''}`;
  const res = await apiFetch(url);
  if (!res.ok) throw new Error(`Failed to fetch PR files: ${res.status}`);
  return res.json();
}

export interface RecentFinding {
  task_id: string;
  title: string;
  type: string;
  scope: string;
  priority: string;
  actionability_grade: 'HIGH' | 'MEDIUM' | 'LOW';
  direction_id: string | null;
  researched_at: string | null;
}

export interface RecentFindingsResponse {
  findings: RecentFinding[];
  grade_distribution: { HIGH: number; MEDIUM: number; LOW: number };
  grade_distribution_pct: { HIGH: number; MEDIUM: number; LOW: number };
  total: number;
}

export async function fetchRecentFindings(
  projectId: string,
  limit = 50,
): Promise<RecentFindingsResponse> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/recent-findings?limit=${limit}`,
  );
  if (!res.ok) throw new Error(`Failed to fetch recent findings: ${res.status}`);
  return res.json();
}

export interface ProjectFinding {
  task_id: string;
  direction_id: string | null;
  completed_at: string | null;
  finding_index: number;
  finding_title: string;
  finding_description: string;
  actionability_grade: 'HIGH' | 'MEDIUM' | 'LOW';
  claims: string[];
  sources: Array<{ ref: string; type?: string | null; excerpt?: string | null }>;
}

export interface ProjectFindingsResponse {
  findings: ProjectFinding[];
  count: number;
}

export async function fetchProjectFindings(
  projectId: string,
  params?: { directionId?: string; limit?: number; days?: number },
): Promise<ProjectFindingsResponse> {
  const searchParams = new URLSearchParams();
  if (params?.directionId) searchParams.set('direction_id', params.directionId);
  if (params?.limit !== undefined) searchParams.set('limit', String(params.limit));
  if (params?.days !== undefined) searchParams.set('days', String(params.days));
  const query = searchParams.toString();
  const res = await apiFetch(`/api/admin/projects/${encodeURIComponent(projectId)}/findings${query ? `?${query}` : ''}`);
  if (!res.ok) throw new Error(`Failed to fetch project findings: ${res.status}`);
  return res.json();
}

export interface StagingBranchPrefixesResponse {
  project_id: string;
  staging_branch_prefixes: string[];
  is_default: boolean;
}

export async function fetchStagingBranchPrefixes(
  projectId: string,
): Promise<StagingBranchPrefixesResponse> {
  const res = await apiFetch(`/api/admin/projects/${encodeURIComponent(projectId)}/staging-branch-prefixes`);
  if (!res.ok) throw new Error(`Failed to fetch staging branch prefixes: ${res.status}`);
  return res.json();
}

export async function updateStagingBranchPrefixes(
  projectId: string,
  prefixes: string[] | null,
): Promise<StagingBranchPrefixesResponse> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/staging-branch-prefixes`,
    { method: 'PATCH', body: JSON.stringify({ staging_branch_prefixes: prefixes }) },
  );
  if (!res.ok) throw new Error(`Failed to update staging branch prefixes: ${res.status}`);
  return res.json();
}

export interface ApprovalGateConfig {
  infra_files: string[];
  critical_files: string[];
}

export interface ApprovalGateConfigResponse {
  project_id: string;
  approval_gate_config: ApprovalGateConfig;
  /** True when both lists are empty (freshly onboarded repo, never customized). */
  is_default: boolean;
}

/**
 * Validate a single approval-gate path against the same rules the API enforces
 * (admin_project_config._validate_approval_gate_paths): no leading slash, no
 * parent-directory traversal segment, and only `[A-Za-z0-9._-/]` characters
 * (rejects shell metacharacters). Returns a human-readable error string, or
 * `null` when the path is valid. Lets the editor surface feedback inline
 * instead of only failing on submit.
 */
export function validateApprovalGatePath(path: string): string | null {
  const trimmed = path.trim();
  if (!trimmed) return 'Path must not be empty';
  if (trimmed.startsWith('/')) return 'Path must be repo-relative (no leading "/")';
  if (trimmed.split('/').includes('..')) return 'Path must not contain ".." traversal';
  if (!/^[A-Za-z0-9._\-/]+$/.test(trimmed)) return 'Path may only contain letters, digits, and . _ - /';
  return null;
}

export async function fetchApprovalGateConfig(
  projectId: string,
): Promise<ApprovalGateConfigResponse> {
  const res = await apiFetch(`/api/admin/projects/${encodeURIComponent(projectId)}/approval-gate-config`);
  if (!res.ok) throw new Error(`Failed to fetch approval-gate config: ${res.status}`);
  return res.json();
}

/**
 * Partial update of the approval-gate file lists (owner-only on the server).
 * Pass only the list(s) you want to change. Takes effect for agents
 * immediately via the existing claim-response seam.
 */
export async function updateApprovalGateConfig(
  projectId: string,
  config: Partial<ApprovalGateConfig>,
): Promise<ApprovalGateConfigResponse> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/approval-gate-config`,
    { method: 'PATCH', body: JSON.stringify(config) },
  );
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    const msg = (data?.error?.message) || (data?.detail) || `Failed to update approval-gate config: ${res.status}`;
    throw new Error(typeof msg === 'string' ? msg : `Failed to update approval-gate config: ${res.status}`);
  }
  return res.json();
}

/**
 * Owner-only project delete. The backend cascades to tasks/artifacts/history
 * and, when `force` is true, bulk-cancels active tasks and tears down their VMs
 * before removing the project. Without `force` the server refuses (409) if the
 * project still has active tasks. Surfaces the server `detail` (403 permission,
 * 409 active-tasks, 429 contention) as the thrown error message.
 */
export async function deleteProject(
  projectId: string,
  force = false,
): Promise<{ message: string; active_tasks_cancelled: number }> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}?force=${force}`,
    { method: 'DELETE' },
  );
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    const msg = (data?.error?.message) || (data?.detail) || `Failed to delete project: ${res.status}`;
    throw new Error(typeof msg === 'string' ? msg : `Failed to delete project: ${res.status}`);
  }
  return res.json();
}

export async function resetProject(
  projectId: string,
): Promise<{ message: string; active_tasks_cancelled: number; tasks_deleted: number }> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/reset`,
    { method: 'POST' },
  );
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    const msg = (data?.error?.message) || (data?.detail) || `Failed to reset project: ${res.status}`;
    throw new Error(typeof msg === 'string' ? msg : `Failed to reset project: ${res.status}`);
  }
  return res.json();
}

export async function setProjectPaused(
  projectId: string,
  paused: boolean,
): Promise<{ paused: boolean }> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/pause`,
    { method: 'PATCH', body: JSON.stringify({ paused }) },
  );
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    const msg = (data?.error?.message) || (data?.detail) || `Failed to update pause state: ${res.status}`;
    throw new Error(typeof msg === 'string' ? msg : `Failed to update pause state: ${res.status}`);
  }
  return res.json();
}

/**
 * Owner-only project archive. Sets `archived=True` so the project disappears
 * from the active dashboard and stops generating/dispatching new work; all
 * project data is retained for a later unarchive. Idempotent on the server.
 * Surfaces the server `detail` (403 permission, etc.) as the thrown error.
 */
export async function archiveProject(
  projectId: string,
): Promise<{ message: string; archived: boolean }> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/archive`,
    { method: 'POST' },
  );
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    const msg = (data?.error?.message) || (data?.detail) || `Failed to archive project: ${res.status}`;
    throw new Error(typeof msg === 'string' ? msg : `Failed to archive project: ${res.status}`);
  }
  return res.json();
}

/**
 * Owner-only project unarchive. Clears `archived` so the project reappears in
 * the active list and regains normal eligibility. Idempotent on the server.
 */
export async function unarchiveProject(
  projectId: string,
): Promise<{ message: string; archived: boolean }> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/unarchive`,
    { method: 'POST' },
  );
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    const msg = (data?.error?.message) || (data?.detail) || `Failed to unarchive project: ${res.status}`;
    throw new Error(typeof msg === 'string' ? msg : `Failed to unarchive project: ${res.status}`);
  }
  return res.json();
}

/**
 * Fetch the archived-only project set (the restore view). The active project
 * list is fetched elsewhere (useProjectsState); this dedicated helper requests
 * `GET /api/admin/projects?archived=true` and returns the lightweight `items`
 * array. Each item carries `archived: true`.
 */
export async function fetchArchivedProjects(
  limit = 100,
): Promise<import('./types/project').Project[]> {
  const res = await apiFetch(`/api/admin/projects?archived=true&limit=${limit}`);
  if (!res.ok) throw new Error(`Failed to fetch archived projects: ${res.status}`);
  const data = await res.json().catch(() => ({}));
  return Array.isArray(data?.items) ? data.items : [];
}

export interface NullBranchConfigEntry {
  id: string;
  name: string;
  github_owner: string;
  github_repo: string;
  is_self_repo: boolean;
  created_at: string | null;
}

export interface NullBranchConfigResponse {
  projects: NullBranchConfigEntry[];
  self_repo_skipped: NullBranchConfigEntry[];
  total: number;
}

export async function fetchNullBranchConfig(): Promise<NullBranchConfigResponse> {
  const res = await apiFetch('/api/admin/diagnostics/null-branch-config');
  if (!res.ok) throw new Error(`Failed to fetch null branch config: ${res.status}`);
  return res.json();
}

export async function backfillProjectBranches(projectId: string): Promise<unknown> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/backfill-branches`,
    { method: 'POST' },
  );
  if (!res.ok) throw new Error(`Failed to backfill branches: ${res.status}`);
  return res.json();
}

// Enable the warm pool for a single project: scopes the allowlist to it, forces
// the master switch on, establishes a sensible target, and warms first members.
// Backs POST /api/admin/projects/{id}/warm-pool/enable.
export async function enableWarmPoolForProject(projectId: string): Promise<unknown> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/warm-pool/enable`,
    { method: 'POST' },
  );
  if (!res.ok) throw new Error(`Failed to enable warm pool: ${res.status}`);
  return res.json();
}

// Disable the warm pool for a single project: drains its members and removes it
// from the allowlist without affecting peers that are still scoped.
// Backs POST /api/admin/projects/{id}/warm-pool/disable.
export async function disableWarmPoolForProject(projectId: string): Promise<unknown> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/warm-pool/disable`,
    { method: 'POST' },
  );
  if (!res.ok) throw new Error(`Failed to disable warm pool: ${res.status}`);
  return res.json();
}

// Read the current per-project warm-pool TARGET map (and the global fallback)
// from the settings endpoint. Returns the two target fields; the rest of the
// settings payload is ignored. An older backend that predates per-project
// targets omits the map — coerce that to {} so callers always get a record.
export async function fetchWarmPoolTargets(): Promise<WarmPoolTargets> {
  const res = await apiFetch('/api/admin/settings');
  if (!res.ok) throw new Error(`Failed to fetch warm-pool targets: ${res.status}`);
  const data = await res.json();
  return {
    warm_pool_target_size: Number(data?.warm_pool_target_size ?? 0),
    warm_pool_target_size_by_project: data?.warm_pool_target_size_by_project ?? {},
  };
}

// Set (targetSize 0..50) or clear (null) a single project's explicit warm-pool
// target. Backs POST /api/admin/projects/{id}/warm-pool/target with body
// {target_size}. Clearing falls the project back to the global default.
export async function setWarmPoolTargetForProject(
  projectId: string,
  targetSize: number | null,
): Promise<WarmPoolTargetSetResponse> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/warm-pool/target`,
    { method: 'POST', body: JSON.stringify({ target_size: targetSize }) },
  );
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    const msg = data?.detail || `Failed to set warm-pool target: ${res.status}`;
    throw new Error(typeof msg === 'string' ? msg : `Failed to set warm-pool target: ${res.status}`);
  }
  return res.json();
}

export interface SrDisagreementTask {
  task_id: string;
  title: string;
  task_type?: string | null;
  scope?: string | null;
  created_at?: string | null;
  pr_url?: string | null;
  sr_check_results?: Record<string, { passed: boolean; issue?: string | null; origin?: string | null }>;
  reviewer_findings?: Array<{ category: string; severity?: string | null; description: string }>;
}

export interface SrReviewerDisagreementTasksResponse {
  items: SrDisagreementTask[];
  total: number;
  page?: number;
  page_size?: number;
  category_counts?: Record<string, number>;
}

export async function fetchSrReviewerDisagreementTasks(
  projectId: string,
  params?: {
    category?: string;
    task_type?: string;
    severity?: string;
    days?: number;
    page?: number;
    page_size?: number;
  },
): Promise<SrReviewerDisagreementTasksResponse> {
  const searchParams = new URLSearchParams();
  if (params?.category) searchParams.set('category', params.category);
  if (params?.task_type) searchParams.set('task_type', params.task_type);
  if (params?.severity) searchParams.set('severity', params.severity);
  if (params?.days !== undefined) searchParams.set('days', String(params.days));
  if (params?.page !== undefined) searchParams.set('page', String(params.page));
  if (params?.page_size !== undefined) searchParams.set('page_size', String(params.page_size));
  const query = searchParams.toString();
  const url = `/api/admin/projects/${encodeURIComponent(projectId)}/sr-reviewer-disagreements/tasks${query ? `?${query}` : ''}`;
  const res = await apiFetch(url);
  if (!res.ok) throw new Error(`Failed to fetch SR reviewer disagreement tasks: ${res.status}`);
  return res.json();
}
