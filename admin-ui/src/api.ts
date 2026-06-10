import { getIdToken } from './firebase';
import type { SrMissedTask } from './components/SrRetrospectiveShared';
import type { WarmPoolTargets, WarmPoolTargetSetResponse } from './types/warmVm';
import type { ProblemModelPatch } from './types';
import type { BuildConfig, MergeGateRuntime, MergeVerificationVerdict } from './types/project';
import type { WarmStartOriginBreakdown } from './hooks/useEfficiencySnapshot';

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

/**
 * One entry of the backend task-type registry catalog. Mirrors the dict shape
 * emitted by `task_type_catalog()` in backend/app/task_capabilities.py — keep
 * the field names in sync with `_capability_to_catalog_entry` there. This is
 * the single backend source of truth for the operator catalog and the task
 * launcher, so the admin-ui static `taskCapabilities.ts` list cannot drift.
 */
export interface TaskTypeCatalogEntry {
  id: string;
  label: string;
  description: string;
  agent_mode: 'write' | 'read_only';
  default_category: string | null;
  user_launchable: boolean;
  // True for a non-user-launchable type that still has a dedicated structured
  // launch-contract path (compute_experiment → POST /api/admin/tasks/compute-experiment).
  // Optional so an older catalog payload that omits the flag still type-checks.
  operator_contract_launchable?: boolean;
  provider_configurable: boolean;
  filter_visible: boolean;
  run_shape: 'single_agent' | 'workflow' | 'experiment_loop';
  output_kind: 'pr' | 'artifact' | 'knowledge' | 'verdict' | 'none';
  requires_code: boolean;
  requires_web: boolean;
  requires_compute: boolean;
  evaluation_strategy: 'ci_and_review' | 'review_only' | 'forward_quality' | 'metric' | 'none';
  cost_ceiling_usd: number | null;
}

/**
 * Fetch the enriched task-type registry catalog from the backend. The endpoint
 * is static and project-agnostic; consumers can render the catalog or derive
 * the launchable-type allowlist from it.
 */
export async function fetchTaskTypeCatalog(
  signal?: AbortSignal,
): Promise<TaskTypeCatalogEntry[]> {
  const res = await apiFetch('/api/admin/task-types', undefined, 2, 30000, signal);
  if (!res.ok) throw new Error(`Failed to fetch task-type catalog: ${res.status}`);
  const data = await res.json();
  return Array.isArray(data?.task_types) ? data.task_types : [];
}

/** The closed set of comparison operators a compute success criterion may use.
 * Mirrors the backend `ComputeSuccessCriterion.operator` Literal verbatim. */
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
  command: string;
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
  model?: string;
  effort?: string;
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

export interface TaskTypeCostAdherenceEntry {
  id: string;
  declared_ceiling_usd: number | null;
  avg_cost_usd: number | null;
  p95_cost_usd: number | null;
  sample_count: number;
  over_ceiling: boolean;
}

/**
 * Fetch per-task-type actual VM cost (avg/p95 over a recent window) versus the
 * declared ceiling. DB-backed sibling of {@link fetchTaskTypeCatalog}; the
 * catalog endpoint stays DB-free, so actual-cost adherence lives here.
 */
export async function fetchTaskTypeCostAdherence(
  signal?: AbortSignal,
): Promise<TaskTypeCostAdherenceEntry[]> {
  const res = await apiFetch(
    '/api/admin/task-types/cost-adherence',
    undefined,
    2,
    30000,
    signal,
  );
  if (!res.ok) throw new Error(`Failed to fetch task-type cost adherence: ${res.status}`);
  const data = await res.json();
  return Array.isArray(data?.task_types) ? data.task_types : [];
}

export async function fetchTask(taskId: string): Promise<import('./components/TaskCard').Task> {
  const res = await apiFetch(`/api/admin/tasks/${encodeURIComponent(taskId)}`);
  if (!res.ok) throw new Error(`Failed to fetch task: ${res.status}`);
  return res.json();
}

export interface ArtifactDownloadUrlResponse {
  url: string;
}

export async function resolveArtifactDownloadUrl(
  projectId: string,
  storageUri: string,
  signal?: AbortSignal,
): Promise<string> {
  const query = new URLSearchParams({
    project_id: projectId,
    storage_uri: storageUri,
  });
  const res = await apiFetch(
    `/api/admin/artifacts/download-url?${query.toString()}`,
    undefined,
    2,
    30000,
    signal,
  );
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    const msg = data?.detail || `Failed to resolve artifact download URL: ${res.status}`;
    throw new Error(typeof msg === 'string' ? msg : `Failed to resolve artifact download URL: ${res.status}`);
  }
  const data = await res.json().catch(() => ({})) as Partial<ArtifactDownloadUrlResponse>;
  if (typeof data.url !== 'string' || data.url.length === 0) {
    throw new Error('Artifact download resolver returned an empty URL');
  }
  return data.url;
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
  buildConfigOverride?: Record<string, unknown> | BuildConfig,
  promotionTarget?: string,
  goal?: string,
  initialSteering?: Pick<ProblemModelPatch, 'context' | 'constraints'>,
): Promise<{
  project_id: string;
  build_config: BuildConfig;
  default_branch?: string;
  /** Backend-computed merge-gate verdict for the freshly-onboarded repo (sibling of build_config). */
  merge_verification_verdict?: MergeVerificationVerdict;
  /** Optional live no-CI gate runtime evidence, present when onboarding response already carries it. */
  merge_gate_runtime?: MergeGateRuntime;
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
  return submitOnboardProject(body);
}

export interface OnboardPreviewResponse {
  build_config: BuildConfig;
  default_branch?: string;
  merge_verification_verdict?: MergeVerificationVerdict;
}

export interface OnboardCreateResponse extends OnboardPreviewResponse {
  project_id: string;
}

async function submitOnboardProject(body: Record<string, unknown>): Promise<OnboardCreateResponse> {
  const res = await apiFetch('/api/admin/projects/onboard', { method: 'POST', body: JSON.stringify(body) });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    const msg = (data?.error?.message) || (data?.detail) || `Error ${res.status}`;
    throw new Error(msg);
  }
  return res.json();
}

export async function previewOnboardProject(
  owner: string,
  repo: string,
  name?: string,
  buildConfigOverride?: Record<string, unknown> | BuildConfig,
  promotionTarget?: string,
  goal?: string,
  initialSteering?: Pick<ProblemModelPatch, 'context' | 'constraints'>,
): Promise<OnboardPreviewResponse> {
  const body: Record<string, unknown> = { github_owner: owner, github_repo: repo, preview_only: true };
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
  return submitOnboardProject(body);
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
  // Per-origin warm-start breakdown (pool vs chain-continuity vs backlog-reuse)
  // + the pool-origin share, from the latest snapshot. Backend emits it at the
  // top level of the efficiency-axes response; surfaces render it beside the
  // contaminated aggregate vm_reuse_rate so the aggregate is never the sole
  // reuse number read. Optional/nullable — older backends omit it, and the
  // backend returns null when there were no warm starts to attribute.
  warm_start_origin_breakdown?: WarmStartOriginBreakdown | null;
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

export interface LlmCostBreakdownResponse {
  window_days: number;
  generated_at: string;
  by_provider: Array<Omit<LlmCostModelRow, 'model'>>;
  by_model: LlmCostModelRow[];
  /** Per-task-type cost attribution — same pricing table as by_model, bucketed by first_payload["type"]. */
  by_task_type: LlmCostTaskTypeRow[];
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
  | 'cached_verdict'
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

export interface ChallengerRefutedFinding {
  severity: string;
  summary: string;
  challenger_reason: string;
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
  started_at?: string | null;
  finalized_at: string | null;
  // The candidate commit this qualification was evaluated against. Used to detect
  // when the latest audit's findings were computed against a different commit.
  candidate_sha?: string | null;
  // Infra-failure context (populated only when status=infra_failure).
  error?: string | null;
  consecutive_infra_errors?: number | null;
  // Audit observability: why the audit sub-score is missing/unscoreable.
  audit_missing_reason?: string | null;
  // Audit reconciliation: model vs findings-derived scores and excluded count.
  audit_reconciliation?: AuditReconciliation | null;
  // Audit findings carried from the qualification run (severity + summary per finding).
  audit_findings?: AuditFinding[] | null;
  excluded_audit_findings?: AuditFinding[] | null;
  // Challenger-confirmed false positives: findings refuted by an independent challenger (with reason).
  challenger_refuted_audit_findings?: ChallengerRefutedFinding[] | null;
  migration_safety?: string | null;
  migration_safety_detail?: string | null;
  migration_apply_safety?: string | null;
  migration_apply_safety_detail?: string | null;
  test_erosion_detail?: Record<string, unknown> | null;
  smoketest_detail?: Record<string, unknown> | null;
  promotion_pr_url?: string | null;
  promotion_conflict?: boolean | null;
  promotion_error_detail?: string | null;
}

/** Per-artifact serving evidence for a release version. One entry per registered
 *  deploy artifact (e.g. the backend Cloud Run revision, plus any other required
 *  artifact). serving_status is one of 'unconfirmed' | 'serving_confirmed' |
 *  'deploy_failed' | null (null = no serving expectation, e.g. a never-promoted
 *  version). reason_code carries the failure/pending detail. */
export interface ReleaseArtifactEvidence {
  artifact_kind: string;
  required: boolean;
  serving_status?: string | null;
  served_identifier?: string | null;
  served_at?: string | null;
  reason_code?: string | null;
}

export interface QualificationState {
  state: string;
  summary: string;
  promotion_pr_number?: number | null;
  promotion_pr_url?: string | null;
  // Additive promotion freshness context for the shared "promotion in flight"
  // state. When absent, callers must fall back to the timeless PR-only copy.
  promotion_enabled_at?: string | null;
  promotion_age_seconds?: number | null;
  promotion_timeout_seconds?: number | null;
  promotion_timeout_at?: string | null;
  promotion_past_timeout?: boolean | null;
  promotion_nearing_timeout?: boolean | null;
  error?: string | null;
  blocker?: string | null;
  // Post-promotion serving confirmation. A 'promoted' state is provisional:
  // serving_status is 'unconfirmed' while the deploy is still within its
  // confirmation window, 'serving_confirmed' once the promoted SHA is live, or
  // 'deploy_failed' (state becomes 'promotion_deploy_failed') when the deploy
  // never stabilized. deploy_failure_reason carries the failure detail.
  serving_status?: string | null;
  deploy_failure_reason?: string | null;
}

export interface CurrentVersion {
  version_number: number;
  phase: string;
  phase_changed_at: string | null;
  phase_reasoning: string | null;
  goal: string | null;
  goal_updated_at?: string | null;
  goal_editor_info?: string | null;
  live_current_focus?: string | null;
  opened_at: string;
  qualification_attempts: number;
  merged_task_count: number;
  /** Tasks merged at/after the last qualification verdict's finalized_at — a
   *  verdict-staleness signal. >0 means work landed since the verdict was
   *  computed, so the displayed confidence no longer reflects the current tree.
   *  null when there is no verdict or no parseable finalized_at. */
  merged_since_verdict?: number | null;
  /** Non-terminal tasks still in flight against this version. A version cut does
   *  not stop them — they continue and land against the freshly-opened version. */
  in_flight_task_count?: number;
  qualification_state?: QualificationState | null;
  last_qualification: VersionQualification | null;
  // The candidate commit currently under evaluation for this open version
  // (mirror of last_qualification.candidate_sha / the resolver's evaluated SHA).
  // Surfaced so the UI can show what is being qualified and, when audit evidence
  // is stale, contrast it against the historical audited candidate.
  active_candidate_sha?: string | null;
  latest_audit?: LatestAudit | null;
  // Version-scoped audit evidence for the open candidate. `latest_audit` is
  // current only when audit_evidence_state='current'; `stale_audit` carries
  // historical provenance when the newest scoped audit belongs to an older
  // candidate, and 'none' means this candidate has no scoped audit evidence yet.
  audit_evidence_state?: 'current' | 'stale' | 'none';
  stale_audit?: LatestAudit | null;
  // Post-promotion serving confirmation (additive). serving_status is one of
  // 'unconfirmed' | 'serving_confirmed' | 'deploy_failed'; served_sha/served_at
  // record the confirmed-serving commit, and deploy_failure_reason the detail
  // when the deploy never stabilized.
  serving_status?: string | null;
  served_sha?: string | null;
  served_at?: string | null;
  deploy_failure_reason?: string | null;
  // Aggregate required-artifact serving truth (additive). The headline
  // 'is it actually shipped' decision derives from this, NOT the backend-only
  // scalar: 'serving_confirmed' only when every required artifact confirms,
  // 'deploy_failed' when any required artifact failed, 'unconfirmed' while any
  // required artifact is still pending. `artifacts` is the per-artifact evidence
  // list (one entry per registered deploy artifact). Both may be absent when the
  // API has no artifact registry — fall back to the scalar fields above.
  aggregate_serving_status?: string | null;
  aggregate_deploy_failure_reason?: string | null;
  artifacts?: ReleaseArtifactEvidence[];
  // Post-promotion deploy-confirmation window deadline (ISO string). The sweep
  // resolves serving_status to confirmed/failed when this elapses; the panel
  // computes remaining/overdue at render time.
  deploy_confirm_deadline?: string | null;
}

export interface RecentVersion {
  version_number: number;
  status: string;
  opened_at: string;
  promoted_at: string | null;
  qualification_attempts: number;
  last_qualification: VersionQualification | null;
  abandoned_at?: string | null;
  abandoned_by?: string | null;
  abandon_reason?: string | null;
  // Post-promotion serving confirmation (additive) — see CurrentVersion.
  qualification_state?: QualificationState | null;
  serving_status?: string | null;
  served_sha?: string | null;
  served_at?: string | null;
  deploy_failure_reason?: string | null;
  // Aggregate required-artifact serving truth (additive) — see CurrentVersion.
  aggregate_serving_status?: string | null;
  aggregate_deploy_failure_reason?: string | null;
  artifacts?: ReleaseArtifactEvidence[];
  // Post-promotion deploy-confirmation window deadline (ISO string) — see
  // CurrentVersion. Present on the recently-promoted history row whose
  // serving_status is still 'unconfirmed'.
  deploy_confirm_deadline?: string | null;
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
  // True when ground truth is validated (sufficient resolved-outcome sample exists);
  // false otherwise. When true, `validated_precision`, `serving_confirmed_count`,
  // `deploy_failed_count`, and `resolved_outcome_sample` are populated.
  ground_truth_validated?: boolean;
  caveat?: string;
  // The readiness window only measures DRY-RUN-regime verdicts, so it reflects
  // *current* readiness only pre-go-live. Once promotion is effectively enabled
  // the window is frozen pre-go-live history: regime='live', is_current_readiness=false.
  regime?: 'pre_go_live_calibration' | 'live';
  is_current_readiness?: boolean;
  promotion_enabled?: boolean;
  // Ground-truth-validated qualifier precision (when validated_precision is non-null).
  validated_precision?: number | null;
  // Number of promotion outcomes resolved (served or failed) used to derive validated_precision.
  resolved_outcome_sample?: number;
  // Number of promoted verdicts that were confirmed serving (contributing to validated_precision).
  serving_confirmed_count?: number;
  // Number of promoted verdicts whose deploy failed without stabilizing (contributing to validated_precision).
  deploy_failed_count?: number;
}

export interface StagedDirection {
  id: string | null;
  text: string | null;
  priority: string | null;
  staged_for_version: number | null;
  staged_at: string | null;
  staged_age_days: number | null;
}

export interface ChallengedVsUnchallengedCohort {
  sample_size: number;
  insufficient_sample: boolean;
  exclusion_rate?: number;
  exclusion_count?: number;
  disagreement_rate?: number;
  disagreement_count?: number;
  wrong_decision_rate?: number | null;
  wrong_decision_resolved_sample?: number;
  /** Count of findings the adversarial challenger explicitly refuted as false positives (challenged cohort only). */
  challenge_refuted_count?: number;
  /** Rate of challenger-refuted false positives per verdict in the challenged cohort. Distinct from exclusion_rate. */
  challenge_refuted_rate?: number;
}

export interface ChallengedVsUnchallengedTelemetry {
  challenged: ChallengedVsUnchallengedCohort;
  unchallenged: ChallengedVsUnchallengedCohort;
  total_verdicts: number;
}

export interface VersionLifecycleResponse {
  current_version: CurrentVersion | null;
  recent_versions: RecentVersion[];
  promotion_enabled?: boolean;
  /** The promotion gate value actually consulted for this project (override when set, else fleet default). */
  promotion_enabled_effective?: boolean;
  /** The explicit per-project override, or null when the project rides the fleet default. */
  promotion_enabled_override?: boolean | null;
  /** Which value is in effect: a per-project override or the fleet-wide default. */
  promotion_enabled_source?: 'project' | 'fleet';
  promotion_readiness?: PromotionReadiness | null;
  staged_directions?: StagedDirection[];
  /** Challenged-vs-unchallenged decision-quality cohort comparison. Advisory/display only. */
  challenged_vs_unchallenged?: ChallengedVsUnchallengedTelemetry | null;
}

export interface AgentServingShaBucket {
  source_sha: string | null;
  count: number;
  warm_pool_count: number;
  task_vm_count: number;
  image_names: string[];
  latest_build_ts: number | null;
  last_heartbeat_at: string | null;
  matches_backend_served?: boolean;
  matches_active_candidate?: boolean;
  matches_integration_head?: boolean;
  matches_current_image?: boolean;
}

export interface AgentServingShaFleet {
  total_observed: number;
  unknown_count: number;
  mixed: boolean;
  buckets: AgentServingShaBucket[];
}

export interface AgentCurrentImage {
  image_name: string | null;
  source_git_sha: string | null;
  build_ts: number | null;
}

export interface AgentServingShasResponse {
  fleet: AgentServingShaFleet;
  current_image: AgentCurrentImage | null;
  backend_served_sha: string | null;
  active_candidate_sha: string | null;
  integration_branch: string | null;
  integration_head_sha: string | null;
  promotion_target: string | null;
  promotion_head_sha: string | null;
}

export async function fetchAgentServingShas(
  projectId: string,
): Promise<AgentServingShasResponse> {
  const res = await apiFetch(`/api/admin/projects/${encodeURIComponent(projectId)}/agent-serving-shas`);
  if (!res.ok) throw new Error(`Failed to fetch agent serving SHAs: ${res.status}`);
  return res.json();
}

export interface SetPromotionEnabledResponse {
  project_id: string;
  override: boolean | null;
  effective: boolean;
  source: 'project' | 'fleet';
  cleared: boolean;
}

/** Arm (true), hold (false), or clear (null) this project's per-project live-promotion override. */
export async function setProjectPromotionEnabled(
  projectId: string,
  enabled: boolean | null,
): Promise<SetPromotionEnabledResponse> {
  const res = await apiFetch(`/api/admin/projects/${encodeURIComponent(projectId)}/promotion-enabled`, {
    method: 'POST',
    body: JSON.stringify({ enabled }),
  });
  if (!res.ok) throw new Error(`Failed to set promotion enablement: ${res.status}`);
  return res.json();
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

export interface SetVersionGoalResponse {
  version_number: number;
  goal: string | null;
  goal_updated_at: string | null;
  goal_editor_info: string | null;
}

export async function setVersionGoal(
  projectId: string,
  goal: string | null,
): Promise<SetVersionGoalResponse> {
  const res = await apiFetch(`/api/admin/projects/${encodeURIComponent(projectId)}/version-goal`, {
    method: 'POST',
    body: JSON.stringify({ goal: goal ?? null }),
  });
  if (!res.ok) throw new Error(`Failed to set version goal: ${res.status}`);
  return res.json();
}

export interface CutVersionResponse {
  abandoned_version: {
    version_number: number;
    status: string;
  };
  new_version: {
    version_number: number;
    phase: string;
    goal: string | null;
    opened_at: string;
  };
}

export async function cutVersion(
  projectId: string,
  reason?: string,
  idempotencyKey?: string,
): Promise<CutVersionResponse> {
  const res = await apiFetch(`/api/admin/projects/${encodeURIComponent(projectId)}/version-cut`, {
    method: 'POST',
    body: JSON.stringify({
      reason: reason ?? '',
      ...(idempotencyKey ? { idempotency_key: idempotencyKey } : {}),
    }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = data?.detail || data?.error?.message || `Failed to cut version: ${res.status}`;
    throw new Error(detail);
  }
  return data;
}

export interface ClearDeployFailedResponse {
  version_number: number;
  serving_status: string | null;
  served_sha: string | null;
  served_at: string | null;
  deploy_failure_reason: string | null;
  clear_mode: 'verified' | 'operator_attested';
  operator_attested: boolean;
  attestation_reason: string | null;
}

// Clear a stale post-promotion deploy_failed serving state. Without `force`, the
// backend only clears when the promoted SHA is independently observed serving;
// with `force` + a `reason` it records an operator-attested clear (used in the
// interim where the live serving SHA is unobservable).
export async function clearDeployFailed(
  projectId: string,
  force?: boolean,
  reason?: string,
): Promise<ClearDeployFailedResponse> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/clear-deploy-failed`,
    {
      method: 'POST',
      body: JSON.stringify({
        ...(force ? { force: true } : {}),
        ...(reason ? { reason } : {}),
      }),
    },
  );
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail =
      data?.detail || data?.error?.message || `Failed to clear deploy-failed: ${res.status}`;
    throw new Error(detail);
  }
  return data;
}

export type QualifyTriggerStatus =
  | 'wrong_phase'
  | 'already_pending'
  | 'project_not_found'
  | 'no_credentials'
  | 'sha_error'
  | 'version_not_found'
  | 'cached_verdict'
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
  audit_findings?: AuditFinding[] | null;
  excluded_audit_findings?: AuditFinding[] | null;
  // Challenger-confirmed false positives: findings refuted by an independent challenger (with reason).
  challenger_refuted_audit_findings?: ChallengerRefutedFinding[] | null;
  migration_safety?: string | null;
  migration_safety_detail?: string | null;
  migration_apply_safety?: string | null;
  migration_apply_safety_detail?: string | null;
  test_erosion_detail?: Record<string, unknown> | null;
  smoketest_detail?: Record<string, unknown> | null;
  audit_reconciliation?: AuditReconciliation | null;
  would_promote: boolean | null;
  promotion_enabled: boolean | null;
  terminal_status: string | null;
  threshold: number | null;
  started_at: string | null;
  finalized_at: string | null;
  created_at: string;
  // How this verdict's promotion actually played out in production:
  // 'serving_confirmed' | 'deploy_failed' | 'unconfirmed' (null until recorded).
  post_promotion_outcome?: string | null;
  post_promotion_outcome_at?: string | null;
  promotion_pr_url?: string | null;
  promotion_conflict?: boolean | null;
  promotion_error_detail?: string | null;
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

export interface ChallengeSummary {
  challenged_boundary: string | null;
  agreement: 'agrees' | 'disagrees' | 'indeterminate';
  second_opinion_verdict: string | null;
  reason: string;
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
  challenge_summary?: ChallengeSummary | null;
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

/**
 * Owner-only project cancel. Marks the project terminal, cancels all active
 * tasks, and tears down their VMs. Idempotent on the server: already-terminal
 * projects return `ignored` rather than an error.
 */
export async function cancelProject(
  projectId: string,
): Promise<{ status: 'cancelled' | 'ignored'; message: string }> {
  const res = await apiFetch(
    `/api/projects/${encodeURIComponent(projectId)}/cancel`,
    { method: 'POST' },
  );
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    const msg = (data?.error?.message) || (data?.detail) || `Failed to cancel project: ${res.status}`;
    throw new Error(typeof msg === 'string' ? msg : `Failed to cancel project: ${res.status}`);
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

export async function patchAdminSettings(settings: { promotion_enabled: boolean }): Promise<unknown> {
  const res = await apiFetch('/api/admin/settings', {
    method: 'PATCH',
    body: JSON.stringify(settings),
  });
  if (!res.ok) throw new Error(`Failed to patch admin settings: ${res.status}`);
  return res.json();
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

// ── Compute jobs ────────────────────────────────────────────────────────────

/** Bounded diagnostic recorded by the backend when a compute backend RPC
 *  (launch / poll / cost / termination) fails in the monitor or kill path.
 *  Mirrors `_build_compute_rpc_diagnostic` in backend/app/compute_jobs.py. */
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
  polling_metadata?: Record<string, unknown>;
  // Bounded RPC failure diagnostic; null/absent when no backend RPC has failed
  // (or once it resolves). Older responses may carry it under polling_metadata.
  compute_rpc_diagnostic?: ComputeRpcDiagnostic | null;
  // Augmented by the admin list endpoint from the launching Task.
  task_title: string | null;
  task_type: string | null;
}

export async function fetchComputeJobs(
  projectId: string,
  signal?: AbortSignal,
  includeFinishedMinutes?: number,
): Promise<ComputeJob[]> {
  const query = new URLSearchParams();
  if (typeof includeFinishedMinutes === 'number') {
    query.set('include_finished_minutes', String(includeFinishedMinutes));
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
  return Array.isArray(data.compute_jobs) ? data.compute_jobs : [];
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
  /** Durable origin provenance (backend `serialize_origin_provenance`): whether the
   *  producing task came from a research finding, and the second-opinion decision that
   *  allowed or rejected its dispatch. Absent on older backend responses — treat a
   *  missing object as legacy/unknown. */
  origin_provenance?: KnowledgeOriginProvenance | null;
  /** Promoted serving release evidence that authorized a live-release compute proof.
   *  Present (non-null) only for compute_experiment documents whose launching job ran as
   *  a live-release proof; null/absent otherwise. */
  release_evidence?: LiveReleaseEvidence | null;
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
  params?: { q?: string; topic?: string; tag?: string; direction?: string; source_kind?: string; origin_state?: string; challenge_state?: string; finding_hash?: string; limit?: number; offset?: number },
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
}

export async function fetchFleetUtilization(): Promise<FleetUtilizationResponse> {
  const res = await apiFetch('/api/admin/fleet-utilization');
  if (!res.ok) throw new Error(`fleet-utilization fetch failed: ${res.status}`);
  return res.json();
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
