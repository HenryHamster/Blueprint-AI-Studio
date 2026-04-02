import { getIdToken } from './firebase';

export const API_BASE_URL = import.meta.env.VITE_API_URL || '';

export const COST_PER_VM_MAX_PLAN = 0.07;   // $/hr, VM only
export const COST_PER_VM_STANDARD = 1.07;    // $/hr, VM + API tokens

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
  retries = 2
): Promise<Response> {
  const headers = await authHeaders();
  const fullUrl = url.startsWith('http') ? url : `${API_BASE_URL}${url}`;
  const mergedOptions = { ...options, headers: { ...headers, ...options?.headers } };

  // Only retry on safe (idempotent) methods to avoid double-mutation
  const method = (options?.method || 'GET').toUpperCase();
  const safeToRetry = method === 'GET' || method === 'HEAD' || method === 'OPTIONS';
  const maxAttempts = safeToRetry ? retries : 0;

  for (let i = 0; i <= maxAttempts; i++) {
    try {
      const res = await fetch(fullUrl, mergedOptions);
      if (res.ok || res.status < 500) return res;
    } catch (e) {
      if (i === maxAttempts) throw e;
    }
    await new Promise(r => setTimeout(r, 1000 * (i + 1)));
  }
  return fetch(fullUrl, mergedOptions);
}
