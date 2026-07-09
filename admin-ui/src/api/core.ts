import { getIdToken } from '../firebase';

/** Base URL for API requests to the Blueprint backend. Defaults to empty string for same-origin requests. */
export const API_BASE_URL = import.meta.env.VITE_API_URL || '';

/** Hourly cost in USD for a standard e2-standard-2 GCP VM on-demand pricing. Used to calculate task cost estimates. */
export const COST_PER_VM_HR = 0.067;

/** Maximum character count for operator/user-authored strategic chat messages. Must match MESSAGE_CONTENT_MAX in backend/app/models.py. */
export const MESSAGE_CONTENT_MAX = 20000;

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
