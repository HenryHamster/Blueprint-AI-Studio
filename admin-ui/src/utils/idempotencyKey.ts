// Generate a client idempotency key for a mutating request. Prefers the native
// UUID generator and falls back to a timestamp+random token for older runtimes
// (and JSDOM, where crypto.randomUUID may be absent).
//
// One logical submit reuses a single key across double-clicks, failed retries,
// and lost-response replays so the backend collapses them into one resource and
// returns the existing one (`status: "duplicate"`) instead of creating a copy.
export function generateIdempotencyKey(): string {
  const c = typeof globalThis !== 'undefined' ? globalThis.crypto : undefined;
  if (c && typeof c.randomUUID === 'function') return c.randomUUID();
  return `idem-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}
