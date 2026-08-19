// Heartbeat-failure classification, extracted from index.ts so it is unit-testable
// (the heartbeat handler itself is an inline setInterval closure).
//
// A heartbeat POST can fail with an HTTP status embedded in the error message
// ("POST /api/agent/heartbeat failed (NNN): ..."). Most 4xx mean the task is no
// longer ours (reassigned / deauthorized) and the agent should self-terminate.
// But a handful of 4xx are TRANSIENT — the orchestrator is rate-limiting (429),
// briefly row-locked (409), or timed out (408/425). On those the agent MUST keep
// heartbeating, not power off its VM.
//
// Regression (2026-05-29): the self-terminate branch matched /failed \(4\d{2}\)/,
// which includes 429. Under orchestrator load the heartbeat got a transient 429,
// the agent read it as "task reassigned", and powered off its own VM mid-task —
// which the reaper then recorded as AGENT_TIMEOUT, silently vaporizing live
// agents (worst on the large rollups, where heartbeats coincided with load).
//
// Kill-path (NeurIPS task 38b16082, 2026-08-18): the heartbeat POST got a 401/403
// that was NOT in TRANSIENT_HEARTBEAT_STATUSES, so the periodic tick's catch
// block treated it as "task reassigned/deauthorized" and self-terminated on the
// spot — 39s after the task had pushed a real fix commit (+811/-82) and passed
// its local publication gate. No retry, no fresh-credential re-acquisition was
// attempted first, so a transient auth blip (token-refresh race, clock skew, a
// momentary lease lapse in the cross-instance revocation cache) destroyed
// already-completed, already-published work instead of just costing one missed
// heartbeat. 401/403 are auth-shaped failures, not "the task is definitely
// gone" failures (unlike a genuine 404 task-not-found or a 409 already-terminal
// signal) — they deserve a credential-refresh-and-retry cascade before the
// agent accepts them as fatal. See isHeartbeatAuthRetryable / the caller's
// retry loop in index.ts's periodic heartbeat tick.

/** HTTP status codes that are transient on the heartbeat path (keep retrying). */
export const TRANSIENT_HEARTBEAT_STATUSES: ReadonlySet<number> = new Set([
  408, // Request Timeout
  409, // Conflict — row FOR UPDATE-locked by the status endpoint (own retry path)
  425, // Too Early
  429, // Too Many Requests — orchestrator rate-limited / overloaded
]);

/**
 * HTTP status codes that MAY be a transient auth blip rather than a genuine
 * reassignment/deauthorization — worth a bounded credential-refresh-and-retry
 * cascade before the agent declares the task lost. Distinct from
 * TRANSIENT_HEARTBEAT_STATUSES: those are safe to silently re-poll next tick
 * with the SAME credentials; these need a fresh token acquired first, and only
 * count as fatal once that fresh-credential retry also fails.
 *
 * 404/other 4xx are deliberately excluded — a 404 means the task row itself is
 * gone (genuinely reassigned to/claimed by another worker), which no amount of
 * credential refreshing fixes.
 */
export const AUTH_RETRY_HEARTBEAT_STATUSES: ReadonlySet<number> = new Set([
  401, // Unauthorized — expired/invalid JWT (token-refresh race, clock skew)
  403, // Forbidden — project/VM-instance mismatch, or a momentary lease lapse
       // in the cross-instance revocation deny-list cache
]);

/**
 * Extract the HTTP status from a "... failed (NNN): ..." error message, or null
 * when the message carries no parseable status.
 */
export function parseHeartbeatStatus(errMessage: string): number | null {
  const m = errMessage.match(/failed \((\d{3})\)/);
  return m ? Number(m[1]) : null;
}

/**
 * True when a heartbeat failure is transient (rate-limit / brief lock / timeout)
 * and the agent should back off and keep heartbeating rather than self-terminate.
 */
export function isHeartbeatTransient(errMessage: string): boolean {
  const status = parseHeartbeatStatus(errMessage);
  return status !== null && TRANSIENT_HEARTBEAT_STATUSES.has(status);
}

/**
 * True when a heartbeat failure's status is auth-shaped (401/403) and worth a
 * fresh-credential retry cascade before the agent treats it as a genuine
 * reassignment/deauthorization and self-terminates.
 */
export function isHeartbeatAuthRetryable(errMessage: string): boolean {
  const status = parseHeartbeatStatus(errMessage);
  return status !== null && AUTH_RETRY_HEARTBEAT_STATUSES.has(status);
}
