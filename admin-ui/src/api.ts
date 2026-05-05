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
