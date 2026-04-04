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

/**
 * Fetch wrapper with auth headers and retry for transient Cloud Run errors.
 * Cloud Run 502/503 responses bypass FastAPI middleware and lack CORS headers,
 * causing the browser to report CORS errors. Retrying handles this gracefully.
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
}

export async function fetchTask(taskId: string): Promise<import('./components/TaskCard').Task> {
  const res = await apiFetch(`/api/admin/tasks/${encodeURIComponent(taskId)}`);
  if (!res.ok) throw new Error(`Failed to fetch task: ${res.status}`);
  return res.json();
}

export async function fetchAutonomyDebug(projectId: string): Promise<unknown> {
  const res = await apiFetch(`/api/admin/projects/${encodeURIComponent(projectId)}/autonomy-debug`);
  if (!res.ok) throw new Error(`Failed to fetch autonomy debug: ${res.status}`);
  return res.json();
}
