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

/** HTTP status codes that are transient on the heartbeat path (keep retrying). */
export const TRANSIENT_HEARTBEAT_STATUSES: ReadonlySet<number> = new Set([
  408, // Request Timeout
  409, // Conflict — row FOR UPDATE-locked by the status endpoint (own retry path)
  425, // Too Early
  429, // Too Many Requests — orchestrator rate-limited / overloaded
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
