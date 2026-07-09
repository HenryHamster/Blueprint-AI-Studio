import { apiFetch } from './core';

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

export async function fetchTask(taskId: string): Promise<import('../components/TaskCard').Task> {
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

export async function fetchTaskSerialConsole(taskId: string): Promise<import('../types/task').SerialConsoleResponse> {
  const res = await apiFetch(`/api/admin/tasks/${encodeURIComponent(taskId)}/serial-console`);
  if (!res.ok) throw new Error(`Failed to fetch serial console: ${res.status}`);
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
