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

export async function onboardProject(
  owner: string,
  repo: string,
  name?: string,
  buildConfigOverride?: Record<string, unknown>
): Promise<{ project_id: string; build_config: Record<string, unknown> }> {
  const body: Record<string, unknown> = { github_owner: owner, github_repo: repo };
  if (name) body.name = name;
  if (buildConfigOverride) body.build_config_override = buildConfigOverride;
  const res = await apiFetch('/api/admin/projects/onboard', { method: 'POST', body: JSON.stringify(body) });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    const msg = (data?.error?.message) || (data?.detail) || `Error ${res.status}`;
    throw new Error(msg);
  }
  return res.json();
}
