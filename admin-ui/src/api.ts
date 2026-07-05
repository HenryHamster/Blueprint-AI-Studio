import { getIdToken } from './firebase';
import type { SrMissedTask } from './components/SrRetrospectiveShared';
import type { WarmPoolTargets, WarmPoolTargetSetResponse } from './types/warmVm';
import type { ProblemModelPatch } from './types';
import type {
  BuildConfig,
  MergeGateRuntime,
  MergeVerificationVerdict,
  ProjectResourceEnvelopeResponse,
  ResourceEnvelopeConfig,
} from './types/project';
import type { WarmStartOriginBreakdown } from './hooks/useEfficiencySnapshot';
import type { Alert, AlertFilters, ProjectAlertDomainsResponse } from './types/alert';

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
  signal?: AbortSignal,
  dedup = true
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
  const dedupKey = safeToRetry && dedup ? `${method} ${fullUrl}` : null;
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

export async function fetchAlerts(
  filters: AlertFilters = {},
  signal?: AbortSignal,
): Promise<Alert[]> {
  const params = new URLSearchParams();
  if (filters.projectId) params.set('project_id', filters.projectId);
  if (filters.domain) params.set('domain', filters.domain);
  if (filters.eventType) params.set('event_type', filters.eventType);
  if (filters.source) params.set('source', filters.source);
  const query = params.toString();
  const res = await apiFetch(`/api/admin/alerts${query ? `?${query}` : ''}`, undefined, 2, 30000, signal);
  if (!res.ok) throw new Error(`Failed to fetch alerts: ${res.status}`);
  return res.json();
}

export async function fetchProjectAlertDomains(
  projectId: string,
  signal?: AbortSignal,
): Promise<ProjectAlertDomainsResponse> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/alert-domains`,
    undefined,
    2,
    30000,
    signal,
  );
  if (!res.ok) throw new Error(`Failed to fetch alert domains: ${res.status}`);
  return res.json();
}

export async function updateProjectAlertDomains(
  projectId: string,
  domains: Record<string, { enabled: boolean } | null>,
): Promise<ProjectAlertDomainsResponse> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/alert-domains`,
    {
      method: 'PATCH',
      body: JSON.stringify({ domains }),
    },
  );
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    const msg = data?.detail || `Failed to update alert domains: ${res.status}`;
    throw new Error(typeof msg === 'string' ? msg : `Failed to update alert domains: ${res.status}`);
  }
  return res.json();
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

export async function getBlockDegradedUnverifiedMerge(
  projectId: string,
): Promise<{ block_degraded_unverified_merge: boolean; is_default: boolean }> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/block-degraded-unverified-merge`,
  );
  if (!res.ok) throw new Error(`Failed to fetch block-degraded-unverified-merge: ${res.status}`);
  return res.json();
}

export async function setBlockDegradedUnverifiedMerge(
  projectId: string,
  enabled: boolean,
): Promise<{ block_degraded_unverified_merge: boolean }> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/block-degraded-unverified-merge`,
    {
      method: 'PATCH',
      body: JSON.stringify({ block_degraded_unverified_merge: enabled }),
    },
  );
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    const msg = data?.detail || `Failed to update block-degraded-unverified-merge: ${res.status}`;
    throw new Error(typeof msg === 'string' ? msg : `Failed to update block-degraded-unverified-merge: ${res.status}`);
  }
  return res.json();
}

export async function getBlockNoTestUnverifiedMerge(
  projectId: string,
): Promise<{ block_no_test_unverified_merge: boolean; is_default: boolean }> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/block-no-test-unverified-merge`,
  );
  if (!res.ok) throw new Error(`Failed to fetch block-no-test-unverified-merge: ${res.status}`);
  return res.json();
}

export async function setBlockNoTestUnverifiedMerge(
  projectId: string,
  enabled: boolean,
): Promise<{ block_no_test_unverified_merge: boolean }> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/block-no-test-unverified-merge`,
    {
      method: 'PATCH',
      body: JSON.stringify({ block_no_test_unverified_merge: enabled }),
    },
  );
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    const msg = data?.detail || `Failed to update block-no-test-unverified-merge: ${res.status}`;
    throw new Error(typeof msg === 'string' ? msg : `Failed to update block-no-test-unverified-merge: ${res.status}`);
  }
  return res.json();
}

export type ResourceEnvelopePatch = Partial<ResourceEnvelopeConfig>;

export async function fetchProjectResourceEnvelope(
  projectId: string,
  signal?: AbortSignal,
): Promise<ProjectResourceEnvelopeResponse> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/resource-envelope`,
    undefined,
    2,
    30000,
    signal,
  );
  if (!res.ok) {
    throw new Error(`Failed to fetch resource envelope: ${res.status}`);
  }
  return res.json();
}

export async function updateProjectResourceEnvelope(
  projectId: string,
  patch: ResourceEnvelopePatch,
): Promise<ProjectResourceEnvelopeResponse> {
  const res = await apiFetch(`/api/admin/projects/${encodeURIComponent(projectId)}/resource-envelope`, {
    method: 'PATCH',
    body: JSON.stringify(patch),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    const msg = data?.detail || `Failed to save resource envelope: ${res.status}`;
    throw new Error(typeof msg === 'string' ? msg : `Failed to save resource envelope: ${res.status}`);
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

export interface ViewportFailureDownloadUrlResponse {
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

export async function resolveViewportFailureDownloadUrl(
  projectId: string,
  component: string,
  width: number,
  signal?: AbortSignal,
): Promise<string> {
  const query = new URLSearchParams({
    project_id: projectId,
    component,
    width: String(width),
  });
  const res = await apiFetch(
    `/api/admin/viewport-failures/download-url?${query.toString()}`,
    undefined,
    2,
    30000,
    signal,
  );
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    const msg = data?.detail || `Failed to resolve viewport screenshot: ${res.status}`;
    throw new Error(typeof msg === 'string' ? msg : `Failed to resolve viewport screenshot: ${res.status}`);
  }
  const data = await res.json().catch(() => ({})) as Partial<ViewportFailureDownloadUrlResponse>;
  if (typeof data.url !== 'string' || data.url.length === 0) {
    throw new Error('Viewport screenshot resolver returned an empty URL');
  }
  return data.url;
}

export async function resolveViewportShotDownloadUrl(
  projectId: string,
  taskId: string,
  name: string,
  signal?: AbortSignal,
): Promise<string> {
  const query = new URLSearchParams({ project_id: projectId, task_id: taskId, name });
  const res = await apiFetch(
    `/api/admin/viewport-shots/download-url?${query.toString()}`,
    undefined,
    2,
    30000,
    signal,
  );
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    const msg = data?.detail || `Failed to resolve viewport shot: ${res.status}`;
    throw new Error(typeof msg === 'string' ? msg : `Failed to resolve viewport shot: ${res.status}`);
  }
  const data = await res.json().catch(() => ({})) as Partial<ViewportFailureDownloadUrlResponse>;
  if (typeof data.url !== 'string' || data.url.length === 0) {
    throw new Error('Viewport shot resolver returned an empty URL');
  }
  return data.url;
}

export async function fetchTaskSerialConsole(taskId: string): Promise<import('./types/task').SerialConsoleResponse> {
  const res = await apiFetch(`/api/admin/tasks/${encodeURIComponent(taskId)}/serial-console`);
  if (!res.ok) throw new Error(`Failed to fetch serial console: ${res.status}`);
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
  debt_pressure?: number | null;
}

export interface CompletionDebtQualItem {
  id: string;
  kind: string;
  title: string;
  description: string;
  created_at: string | null;
  age_days: number | null;
}

export interface CompletionDebtQualSnapshot {
  items: CompletionDebtQualItem[];
  total_open: number;
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

export interface GoalAlignment {
  assessed_grade?: number | null;
  state?: string | null;  // 'aligned' | 'shortfall' | 'not_applicable'
  biggest_gap?: string | null;
  reasoning_summary?: string | null;
}

export interface BlindMergeGrade {
  coverage_verdict?: string | null;  // 'covers' | 'partial' | 'misses'
  biggest_gap?: string | null;
}

export interface AuditFinding {
  severity: string;
  summary: string;
  // File attribution carried when the raw finding named one (additive; older
  // records predating stable-identity carry no file).
  file?: string;
  // Deterministic content-derived key (sha256→16-hex of normalized file|summary)
  // stable across re-extractions, so findings can be keyed in a resolution ledger.
  key?: string;
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
  // Pre-cap totals + truncation flags so the UI can tell whether the carried
  // findings lists were clipped at the carry cap (additive; absent on older records).
  audit_findings_total?: number | null;
  audit_findings_truncated?: boolean | null;
  excluded_audit_findings_total?: number | null;
  excluded_audit_findings_truncated?: boolean | null;
  // Challenger-confirmed false positives: findings refuted by an independent challenger (with reason).
  challenger_refuted_audit_findings?: ChallengerRefutedFinding[] | null;
  migration_safety?: string | null;
  migration_safety_detail?: string | null;
  migration_apply_safety?: string | null;
  migration_apply_safety_detail?: string | null;
  test_erosion_detail?: Record<string, unknown> | null;
  smoketest_detail?: Record<string, unknown> | null;
  // Whether the smoketest dimension was enabled for this qualification run.
  // False means the dimension was disabled and the 0.0 sub-score is a
  // zero-weighted placeholder, not an active lane. Null/absent on legacy records.
  smoketest_enabled?: boolean | null;
  promotion_pr_url?: string | null;
  promotion_conflict?: boolean | null;
  promotion_error_detail?: string | null;
  goal_alignment?: GoalAlignment | null;
  blind_merge_grade?: BlindMergeGrade | null;
  completion_debt?: CompletionDebtQualSnapshot | null;
  debt_enforce_confidence_preview?: number | null;
  debt_enforce_weight_preview?: number | null;
}

/** Single resolved serving block — the canonical answer to "is this version live?"
 *  Derived by the backend from the aggregate artifact evidence so every read site
 *  (UI panel, CLI) reads one field instead of re-implementing the three-field
 *  precedence chain (aggregate_serving_status ?? serving_status ?? qual_state.serving_status). */
export interface ResolvedServing {
  status?: string | null;
  sha?: string | null;
  confirmed_at?: string | null;
  failure_reason?: string | null;
  artifacts?: ReleaseArtifactEvidence[];
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

export interface ManagedCheckWaitSnapshotCheck {
  name: string;
  status?: string | null;
  conclusion?: string | null;
}

export interface ManagedCheckWaitSnapshot {
  state: string;
  classified_at: string;
  head_sha?: string | null;
  total_checks?: number | null;
  checks?: ManagedCheckWaitSnapshotCheck[];
  truncated?: boolean | null;
  reason?: string | null;
  detail?: string | null;
}

// The authoritative closed vocabulary of qualification states. This mirrors
// the backend's summarize_qualification_state (backend/app/release_version.py):
// every branch there returns exactly one of these state strings. Keeping this
// union in sync with that producer is what lets TypeScript catch a new backend
// state that lacks a QUAL_STATE_STYLE entry or a stale literal comparison in
// the lifecycle panels — the drift that let the 'qualifying' wait-guard
// regression ship silently.
export type QualificationStateValue =
  | 'qualifying'
  | 'audit_crashed'
  | 'promotion_check_wait'
  | 'promotion_in_flight'
  | 'promotion_failed'
  | 'required_check_failed'
  | 'protection_blocked_review'
  | 'not_promoted'
  | 'dry_run'
  | 'promoted'
  | 'promoted_unconfirmed'
  | 'promotion_deploy_failed'
  | 'promotion_confirmation_timed_out'
  | 'deferred_unsynced'
  | 'error'
  | 'infra_failure';

export interface QualificationState {
  state: QualificationStateValue;
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
  // Failing-check names / branch-protection detail surfaced for the
  // required_check_failed and protection_blocked_review states.
  detail?: string | null;
  // Managed promotion check-wait context: a migration-carrying promotion PR is
  // open and Blueprint is waiting for required checks before retrying the merge
  // (distinct from native auto-merge 'promotion_in_flight'). These are additive
  // and absent for native auto-merge.
  migration_carrying?: boolean | null;
  check_snapshot?: ManagedCheckWaitSnapshot | null;
  ci_state?: string | null;
  blocker?: string | null;
  // Post-promotion serving confirmation. A 'promoted' state is provisional:
  // serving_status is 'unconfirmed' while the deploy is still within its
  // confirmation window, 'serving_confirmed' once the promoted SHA is live, or
  // 'deploy_failed' (state becomes 'promotion_deploy_failed') when the deploy
  // never stabilized. deploy_failure_reason carries the failure detail.
  serving_status?: string | null;
  deploy_failure_reason?: string | null;
}

export interface OutOfBandPromotion {
  source: string;
  recorded_at: string;
  candidate_sha: string;
  promoted_sha: string;
}

export interface ChangelogEntry {
  task_type: string | null;
  title: string;
  pr_number: number | null;
  pr_url?: string | null;
}

export interface CriterionCoverageEntry {
  criterion_key: string;
  text: string;
  coverage: 'covered' | 'partial' | 'uncovered';
  status?: 'active' | 'retracted' | 'superseded';
  evidence_count: number;
  stuck_reason?: 'no_mapping' | 'direction_retired' | null;
}

export interface VersionCriterionCoverage {
  // Active criteria only; inactive criteria may still appear in criteria for audit.
  total: number;
  covered: number;
  partial: number;
  uncovered: number;
  retracted?: number;
  superseded?: number;
  inactive?: number;
  stuck_count?: number;
  criteria: CriterionCoverageEntry[];
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
  /** Human-readable what's-new headline + at-a-glance metrics (merged / proven /
   *  conf-thr) for the current version, mirroring the per-version summary
   *  endpoint's highlights. The 'serving' word in the headline derives from the
   *  aggregate required-artifact serving truth, not the scalar serving_status.
   *  Optional/additive — absent on older backends. */
  highlights?: VersionSummaryHighlights | null;
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
  /** Single resolved serving block — prefer this over the three peer fields.
   *  Absent on older backends (optional for zero-downtime mixed deploys). */
  serving?: ResolvedServing | null;
  // Post-promotion deploy-confirmation window deadline (ISO string). The sweep
  // resolves serving_status to confirmed/failed when this elapses; the panel
  // computes remaining/overdue at render time.
  deploy_confirm_deadline?: string | null;
  // Out-of-band promotion provenance. Non-null only when an operator force-merged
  // this version outside the normal qualification flow.
  out_of_band_promotion?: OutOfBandPromotion | null;
  /** Per-task changelog for this version window: type, title, and PR number for
   *  each merged task, newest-first. Capped at 50 newest; tasks_merged_has_more
   *  is true when the version has additional older tasks beyond the limit. */
  tasks_merged?: ChangelogEntry[];
  /** True when the version has more merged tasks than the 50 returned. */
  tasks_merged_has_more?: boolean;
  /** Acceptance-criteria bucket counts for all merged tasks in this version. */
  acceptance_aggregate?: Record<string, number> | null;
  /** Version-scoped criterion coverage derived from the canonical acceptance contract. */
  criterion_coverage?: VersionCriterionCoverage | null;
}

export interface RecentVersion {
  id?: string;
  version_number: number;
  status: string;
  phase?: string | null;
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
  /** Single resolved serving block — prefer this over the three peer fields.
   *  Absent on older backends (optional for zero-downtime mixed deploys). */
  serving?: ResolvedServing | null;
  // Post-promotion deploy-confirmation window deadline (ISO string) — see
  // CurrentVersion. Present on the recently-promoted history row whose
  // serving_status is still 'unconfirmed'.
  deploy_confirm_deadline?: string | null;
  // Out-of-band promotion provenance — see CurrentVersion.
  out_of_band_promotion?: OutOfBandPromotion | null;
  // Server-computed highlights (headline + metrics) — additive; null on older backends.
  highlights?: VersionSummaryHighlights | null;
  // Merged-task count for this version's window. Both keys present for consistency.
  merged_count?: number | null;
  merged_task_count?: number | null;
  acceptance_aggregate?: Record<string, number> | null;
  /** Version-scoped criterion coverage derived from the canonical acceptance contract. */
  criterion_coverage?: VersionCriterionCoverage | null;
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
  // Acceptance caveat when merged work is satisfied-but-not-machine-proven;
  // omitted when no caveat (all work proven or no unproven work present).
  acceptance_caveat?: string | null;
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

export type PromotionPolicySource = 'runtime_config' | 'yaml_file' | 'hardcoded_defaults';

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
  promotion_policy_source?: PromotionPolicySource;
  promotion_policy_error?: string | null;
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

export interface SelfAuditResponse {
  task_id: string;
  project_id: string;
  status: string;
}

export async function triggerSelfAudit(projectId: string): Promise<SelfAuditResponse> {
  const res = await apiFetch(`/api/admin/projects/${encodeURIComponent(projectId)}/self-audit`, {
    method: 'POST',
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    const msg = data?.detail || `Failed to trigger self-audit: ${res.status}`;
    throw new Error(typeof msg === 'string' ? msg : `Failed to trigger self-audit: ${res.status}`);
  }
  return res.json();
}

export async function fetchVersionLifecycle(projectId: string): Promise<VersionLifecycleResponse> {
  const res = await apiFetch(`/api/admin/projects/${encodeURIComponent(projectId)}/version-lifecycle`);
  if (!res.ok) throw new Error(`Failed to fetch version lifecycle: ${res.status}`);
  return res.json();
}

export interface VersionSummaryHighlights {
  headline: string;
  merged_count: number;
  proven: number;
  total_merged_for_acceptance: number;
  qualification_confidence: number | null;
  qualification_threshold: number | null;
  narrative?: string | null;
}

export interface VersionSummaryResponse {
  version_number: number;
  phase: string;
  status: string;
  goal: string | null;
  opened_at: string | null;
  promoted_at: string | null;
  served_at: string | null;
  phase_changed_at: string | null;
  merged_count: number;
  acceptance_aggregate: Record<string, number>;
  qualification_state: QualificationState | null;
  serving_status: string | null;
  served_sha: string | null;
  deploy_failure_reason: string | null;
  highlights: VersionSummaryHighlights;
  tasks_merged?: ChangelogEntry[];
}

export async function fetchVersionSummary(versionId: string): Promise<VersionSummaryResponse> {
  const res = await apiFetch(`/api/admin/release-versions/${encodeURIComponent(versionId)}/summary`);
  if (!res.ok) throw new Error(`Failed to fetch version summary: ${res.status}`);
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

export interface CreateCriterionResponse {
  criterion_key: string;
  text: string;
  release_version_id: string;
}

export async function createAcceptanceCriterion(
  projectId: string,
  text: string,
): Promise<CreateCriterionResponse> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/acceptance-criteria`,
    { method: 'POST', body: JSON.stringify({ text }) },
  );
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = data?.detail || `Failed to create criterion: ${res.status}`;
    throw new Error(detail);
  }
  return data;
}

export async function deleteAcceptanceCriterion(
  projectId: string,
  criterionKey: string,
): Promise<void> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/acceptance-criteria/${encodeURIComponent(criterionKey)}`,
    { method: 'DELETE' },
  );
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    const detail = data?.detail || `Failed to delete criterion: ${res.status}`;
    throw new Error(detail);
  }
}

export async function reinstateAcceptanceCriterion(
  projectId: string,
  criterionKey: string,
): Promise<{ status: string; criterion_key: string }> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/acceptance-criteria/${encodeURIComponent(criterionKey)}/reinstate`,
    { method: 'POST' },
  );
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = data?.detail || `Failed to reinstate criterion: ${res.status}`;
    throw new Error(detail);
  }
  return data;
}

export async function attestAcceptanceCriterion(
  projectId: string,
  criterionKey: string,
  reason: string,
): Promise<{ status: string; criterion_key: string; coverage: string }> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/acceptance-criteria/${encodeURIComponent(criterionKey)}/attest`,
    { method: 'POST', body: JSON.stringify({ reason }) },
  );
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = data?.detail || `Failed to attest criterion: ${res.status}`;
    throw new Error(typeof detail === 'string' ? detail : `Failed to attest criterion: ${res.status}`);
  }
  return data;
}

export async function linkCriteriaToDirection(
  projectId: string,
  directionKey: string,
  criterionKeys: string[],
): Promise<{ status: string; direction_key: string; criterion_keys: string[] }> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/acceptance-criteria/link-direction`,
    { method: 'POST', body: JSON.stringify({ direction_key: directionKey, criterion_keys: criterionKeys }) },
  );
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = data?.detail || `Failed to link criteria to direction: ${res.status}`;
    throw new Error(typeof detail === 'string' ? detail : `Failed to link criteria to direction: ${res.status}`);
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
  // Whether the smoketest dimension was enabled for this qualification run.
  // False means the 0.0 sub-score is a zero-weighted placeholder, not an
  // active lane. Null/absent on legacy verdict rows that predate this field.
  smoketest_enabled?: boolean | null;
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
  goal_alignment?: GoalAlignment | null;
  blind_merge_grade?: BlindMergeGrade | null;
  completion_debt?: CompletionDebtQualSnapshot | null;
  debt_enforce_confidence_preview?: number | null;
  debt_enforce_weight_preview?: number | null;
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

export interface StagingBranchPrefixesResponse {
  project_id: string;
  staging_branch_prefixes: string[];
  is_default: boolean;
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

export interface BootstrapRetryResponse {
  state: 'spawned' | 'already_active' | 'already_completed' | 'not_eligible';
  task_id?: string | null;
  reason?: string | null;
  [key: string]: unknown;
}

export async function retryProjectBootstrap(
  projectId: string,
): Promise<BootstrapRetryResponse> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/bootstrap/retry`,
    { method: 'POST' },
  );
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    const msg = (data?.error?.message) || (data?.detail) || `Failed to retry bootstrap: ${res.status}`;
    throw new Error(typeof msg === 'string' ? msg : `Failed to retry bootstrap: ${res.status}`);
  }
  return res.json();
}

export async function refreshProjectBootstrap(
  projectId: string,
): Promise<BootstrapRetryResponse> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/bootstrap/refresh`,
    { method: 'POST' },
  );
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    const msg = (data?.error?.message) || (data?.detail) || `Failed to refresh bootstrap: ${res.status}`;
    throw new Error(typeof msg === 'string' ? msg : `Failed to refresh bootstrap: ${res.status}`);
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

export async function purgeArchive(olderThanDays?: number): Promise<{ deleted_count: number; has_more: boolean }> {
  const body: Record<string, number> = {};
  if (olderThanDays !== undefined) body.older_than_days = olderThanDays;
  const res = await apiFetch('/api/admin/tasks/purge-archive', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`Failed to purge archive: ${res.status}`);
  return res.json();
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

export interface ComputeJobCommandLog {
  text: string;
  truncated: boolean;
  byte_count: number;
}

export interface ComputeJobLiveOutput {
  text: string;
  truncated: boolean;
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
): Promise<ComputeJobsListResponse> {
  const query = new URLSearchParams();
  if (typeof includeFinishedMinutes === 'number') {
    query.set('include_finished_minutes', String(includeFinishedMinutes));
  }
  if (offset > 0) query.set('offset', String(offset));
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

export interface BuildConfigCommandEntry {
  path: string | null;
  type: string | null;
  detected_test_cmd: string | null;
  detected_build_cmd: string | null;
  detected_install_cmd: string | null;
  detected_lint_cmd: string | null;
  confirmed_test_cmd: string | null;
  confirmed_build_cmd: string | null;
  confirmed_install_cmd: string | null;
  confirmed_lint_cmd: string | null;
  commands_confirmed_at: string | null;
  confirming_task_id: string | null;
  commands_stale: boolean;
}

/** Outcome of the repo_bootstrap command write-back, recorded independently of
 *  the repo overview. `confirmed` = commands were written back and verified;
 *  `missing_commands` = bootstrap produced an overview but no commands to confirm;
 *  `writeback_failed` = commands existed but the write-back did not land;
 *  `unknown` = a legacy agent omitted the status payload entirely. */
export type BuildConfigCommandMemoryStatus =
  | 'confirmed'
  | 'missing_commands'
  | 'writeback_failed'
  | 'unknown';

export interface BuildConfigCommandMemory {
  status: BuildConfigCommandMemoryStatus | string;
  complete?: boolean | null;
  detail?: string | null;
  paths?: string[] | null;
  updated_at?: string | null;
}

export interface BuildConfigCommandsResponse {
  project_id: string;
  directories: BuildConfigCommandEntry[];
  /** Bootstrap command-memory outcome. Null/absent on legacy projects that
   *  predate the command-memory status contract. */
  command_memory?: BuildConfigCommandMemory | null;
}

export async function fetchBuildConfigCommands(
  projectId: string,
  signal?: AbortSignal,
): Promise<BuildConfigCommandsResponse> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/build-config/commands`,
    undefined,
    2,
    30000,
    signal,
  );
  if (!res.ok) throw new Error(`Failed to fetch build config commands: ${res.status}`);
  return res.json() as Promise<BuildConfigCommandsResponse>;
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
  params?: { q?: string; topic?: string; tag?: string; direction?: string; source_kind?: string; origin_state?: string; challenge_state?: string; finding_hash?: string; sweep?: string; limit?: number; offset?: number },
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

export interface LaneWasteDiagnostics {
  waste_taxonomy: LaneWasteTaxonomy;
  waste_task_count: number;
  cap_counted_task_count: number;
  cap_excluded_task_count: number;
  chain_count: number;
  cap_exhausted_chain_count: number;
  chain_limit: number;
  chains_truncated: boolean;
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
    review: { p50: LaneBaselineTrendBlock; p90: LaneBaselineTrendBlock };
    fix: { p50: LaneBaselineTrendBlock; p90: LaneBaselineTrendBlock };
  };
  waste_taxonomy_shares: Record<string, LaneBaselineTrendBlock>;
  checkpoint_recovery_waste_total: LaneBaselineTrendBlock;
}

export interface LaneBaselinesResponse {
  project_id: string;
  count: number;
  baselines: Record<string, unknown>[];
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

export async function declareCapabilityVerdict(
  projectId: string,
  capName: string,
  verdict: 'adopt' | 'reject' | 'continue-trial',
  rationale?: string,
): Promise<void> {
  const res = await apiFetch(
    `/api/admin/projects/${projectId}/capability-trials/declare`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ cap_name: capName, verdict, rationale: rationale ?? null }),
    },
  );
  if (!res.ok) throw new Error(`Failed to declare capability verdict: ${res.status}`);
}

export async function dismissPendingConfirmation(
  projectId: string,
  pendingId: string,
  note?: string,
): Promise<void> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/pending-confirm/${encodeURIComponent(pendingId)}/dismiss`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ note: note ?? null }),
    },
  );
  if (!res.ok) throw new Error(`Failed to dismiss pending confirmation: ${res.status}`);
}

export async function acceptPendingConfirmation(
  projectId: string,
  pendingId: string,
  answer?: string,
): Promise<{ status: string; type: string }> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/pending-confirm/${encodeURIComponent(pendingId)}/accept`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ answer: answer ?? null }),
    },
  );
  if (!res.ok) throw new Error(`Failed to accept pending confirmation: ${res.status}`);
  return res.json();
}

/** A pull-based fleet runner registered to a project. */
export interface FleetRunner {
  id: string;
  hostname: string | null;
  version: string | null;
  status: string;
  capabilities: {
    task_types?: string | string[];
    has_gpu?: boolean;
    max_concurrent?: number;
    // Task ids the runner reported as currently executing in its last heartbeat.
    active_task_ids?: string[];
    active_compute_job_ids?: string[];
    runner_preset?: string;
    workload?: string;
    gpu_count?: number;
    gpu_names?: string[];
    // Display-only trust telemetry stashed on capabilities at heartbeat.
    disk_free_bytes?: number;
    gpu_utilization_pct?: number;
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
    available_slots: number | null;
    stale_reason: string | null;
    agent_available?: boolean | null;
    liveness_window_seconds: number;
    // Trust telemetry surfaced from the backend health computation.
    last_successful_job_at?: string | null;
    seconds_since_last_success?: number | null;
    consecutive_job_failures?: number;
    disk_free_bytes?: number | null;
    disk_low?: boolean;
    gpu_utilization_pct?: number | null;
    failure_streak_threshold?: number;
  };
  last_seen_at: string | null;
  created_at: string | null;
}

/** Register a new fleet runner for a project. Returns the one-time token. */
export interface FleetRunnerRegistrationResult {
  runner_id: string;
  token: string;
  token_expires_at?: string | null;
  expires_at?: string | null;
  expiresAt?: string | null;
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
): Promise<FleetRunnerRegistrationResult> {
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

/** Reset the review-fix iteration cap for a PR-linked task. Archives FAILED
 *  review/fix tasks on the PR to drop them from the global count and zeroes
 *  the per-chain iteration counter. */
export async function resetTaskIterationCap(
  taskId: string,
): Promise<{ archived_count: number; previous_per_chain_iteration: number }> {
  const res = await apiFetch(`/api/admin/tasks/${taskId}/reset-iteration-cap`, { method: 'POST' });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.detail || `Failed to reset iteration cap: ${res.status}`);
  }
  return res.json();
}

/** Force-create the rollup PR for an EVALUATING parent task. Idempotent —
 *  returns the existing PR if one is already in progress. */
export async function triggerTaskRollup(
  taskId: string,
): Promise<{ rollup_pr_number?: number; rollup_pr_url?: string; pr_number?: number; pr_url?: string }> {
  const res = await apiFetch(`/api/admin/tasks/${taskId}/rollup`, { method: 'POST' });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.detail || `Failed to trigger rollup: ${res.status}`);
  }
  return res.json();
}

export interface CompletionDebtEntry {
  id: string;
  project_id: string;
  kind: string;
  title: string;
  detail: string | null;
  status: 'open' | 'dismissed' | 'resolved';
  dismiss_reason: string | null;
  created_at: string;
  dismissed_at: string | null;
  resolved_at: string | null;
}

/** List completion-debt entries for a project. */
export async function fetchCompletionDebts(
  projectId: string,
  filters?: { status?: string; kind?: string },
): Promise<{ entries: CompletionDebtEntry[]; count: number; total_count: number }> {
  const params = new URLSearchParams();
  if (filters?.status) params.set('status', filters.status);
  if (filters?.kind) params.set('kind', filters.kind);
  const qs = params.toString();
  const url = `/api/admin/projects/${encodeURIComponent(projectId)}/completion-debts${qs ? `?${qs}` : ''}`;
  const res = await apiFetch(url);
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.detail || `Failed to fetch completion debts: ${res.status}`);
  }
  return res.json();
}

/** Dismiss a completion-debt entry with a reason. */
export async function dismissCompletionDebt(
  projectId: string,
  debtId: string,
  reason: string,
): Promise<{ status: string; entry: CompletionDebtEntry }> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/completion-debts/${encodeURIComponent(debtId)}/dismiss`,
    { method: 'POST', body: JSON.stringify({ reason }), headers: { 'Content-Type': 'application/json' } },
  );
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.detail || `Failed to dismiss completion debt: ${res.status}`);
  }
  return res.json();
}

/** Resolve a completion-debt entry (mark work complete). */
export async function resolveCompletionDebt(
  projectId: string,
  debtId: string,
): Promise<{ status: string; entry: CompletionDebtEntry }> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/completion-debts/${encodeURIComponent(debtId)}/resolve`,
    { method: 'POST' },
  );
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.detail || `Failed to resolve completion debt: ${res.status}`);
  }
  return res.json();
}
