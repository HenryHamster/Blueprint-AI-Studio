import { spawn, execFileSync } from 'child_process';
import { fileURLToPath } from 'url';
import fs from 'fs';
import WebSocket from 'ws';
import { handlers, type Handler } from './handlers/index.js';
import { extractJsonObject, extractJsonArray, JsonExtractionError } from './json-extract.js';
import {
  startEventLoopLagMonitor, traceHeartbeat, installExitMarkers, recordFinalState,
} from './diagnostics.js';
import { registerChildPid, unregisterChildPid } from './processTracking.js';
import { resetForNextRole } from './reset.js';
import { isHeartbeatTransient } from './heartbeat-classify.js';
import {
  type FixLoopEvent,
  pushFixLoopEvent,
  drainFixLoopEventBuffer,
  requeueFixLoopEvents,
  resetCheckSynthesizerState,
  synthesizeCheckFixLoopEvents,
} from './fix-loop-events.js';
import {
  API_TIMEOUT, API_MAX_RETRIES, LOGS_MAX_LENGTH, API_RETRY_BACKOFF_MAX_MS,
  PROCESS_KILL_GRACE_MS,
  HEARTBEAT_INTERVAL, MAX_LOG_BUFFER,
  HEARTBEAT_API_TIMEOUT, HEARTBEAT_API_MAX_RETRIES,
  GIT_CLONE_TIMEOUT, CLONE_MAX_RETRIES, CLONE_RETRY_BASE_DELAY,
  CONFLICT_CHECK_TIMEOUT,
} from './constants.js';
import {
  initTokenModule, tryRefreshToken,
  scheduleProactiveTokenRefresh, stopProactiveTokenRefresh,
  computeRefreshDelayMs, isBootstrapToken,
} from './utils/agent-token.js';
import { sanitize } from './utils/sanitize.js';
import {
  type StaleResultEnvelope,
  createStaleResultEnvelope, parseStaleResultFile,
  staleResultBackoffMs, shouldAbandonStaleResult, markStaleResultFailure,
} from './utils/stale-result.js';
import {
  type DiffLines,
  jaccardSimilarity, detectLoopDivergence,
} from './utils/diff-detection.js';
import {
  type BuildConfig, type TestRunner,
  detectTestRunners,
  parseFailingTests, normalizePytestSelectorForRunner,
  extractPytestSelectorsFromOutput, buildTargetedPytestRunners,
} from './utils/test-runners.js';
import {
  PROVIDER, AUTH_MODE, LLM_EXECUTION_MODE, LLM_CLI_TRANSLATION,
  parseLLMCliTranslation, matchesRateLimit, RateLimitError, getProviderRunner,
  __setRunClaudeCodeOverrideForSmoke, codexStreamHasTurnCompleted,
  runClaudeCode, runProviderAgent,
  type LLMCliTranslation, type ClaudeOutput, type RunnerCommand,
  type ValidationCommand, type ProviderRunner,
} from './providers/runners.js';

export {
  PROVIDER, AUTH_MODE, LLM_EXECUTION_MODE, LLM_CLI_TRANSLATION,
  parseLLMCliTranslation, matchesRateLimit, RateLimitError, getProviderRunner,
  __setRunClaudeCodeOverrideForSmoke, codexStreamHasTurnCompleted,
  runClaudeCode, runProviderAgent,
};
export type { LLMCliTranslation, ClaudeOutput, RunnerCommand, ValidationCommand, ProviderRunner };
export { computeRefreshDelayMs, isBootstrapToken };

// ── Failure categories ───────────────────────────────────────────────────────
export type FailureCategory = 'clone_failed' | 'build_failed' | 'test_failed' | 'lint_failed' | 'pr_creation_failed' | 'claude_code_failed' | 'parse_failed' | 'fatal' | 'rate_limited' | 'timeout' | 'oauth_rotation_failed' | 'reset_failed' | 'reassigned';

// ── Graceful-shutdown signalling (cluster C7) ────────────────────────────────
// A non-error "shutdown requested" control signal. Thrown from the deep call
// paths that previously did `setTimeout(process.exit); return` — the
// fall-through return raced main()'s role loop (which would re-enter
// pollForNextRole before the deferred exit fired) and skipped the terminal
// status POST. Throwing instead unwinds the stack cleanly back to main()'s
// role loop, which catches it, breaks, and lets the outer finally tear down
// the workspace before a single explicit exit. Distinct from Error so the
// global crash handler / runRoleOnce catch never mis-records it as a fault.
export class GracefulShutdownSignal {
  readonly reason: string;
  readonly exitCode: number;
  constructor(reason: string, exitCode = 0) {
    this.reason = reason;
    this.exitCode = exitCode;
  }
}

// Set once a non-transient heartbeat 4xx (reassignment/deauth) is observed.
// Read by the heartbeat loop so any in-flight tick that has already passed the
// classification gate does not keep firing after we have decided to shut down.
//
// WARM-VM ISOLATION: this is per-role state. On a warm VM (one process, many
// sequential roles) it MUST start false for each new role — otherwise a future
// code path that sets it without an immediate process.exit would leak the
// "shutting down" signal into the next role and silently suppress its
// heartbeats. Today the only setter (the heartbeat 4xx path) exits 5s later, so
// the leak is latent, but the warm-handoff reset (resetPerRoleModuleState)
// clears it unconditionally so the invariant holds regardless of future setters.
let cancelRequested = false;
function requestCancel(): void { cancelRequested = true; }
function resetCancelRequested(): void { cancelRequested = false; }
export function _isCancelRequestedForTesting(): boolean { return cancelRequested; }
export function _resetCancelRequestedForTesting(): void { cancelRequested = false; }
export function _setCancelRequestedForTesting(): void { requestCancel(); }

// Crash-report task identity, decoupled from process.env.TASK_ID. The role
// loop deliberately clears TASK_ID during the warm-poll window so a benign
// SIGTERM (VM being torn down with no follow-up work) does not overwrite the
// prior task's status with a FATAL message. But that also stripped crash
// context from *real* crashes (uncaughtException / unhandledRejection) that
// happen during the poll gap — they reported with no task id and were silently
// lost. This module-level id is set at EVERY TASK_ID assignment and is NOT
// cleared during the poll window, so _crashReport can still attribute a
// poll-gap crash to the warm-pinned task (labelled distinctly so the backend
// can tell a genuine crash from the prior task's completed work).
let _crashReportTaskId = '';
// True while the role loop is in the warm-poll gap (TASK_ID intentionally
// blank). Lets _crashReport label poll-gap crashes distinctly.
let _inWarmPollWindow = false;
function setCrashReportTaskId(taskId: string): void { _crashReportTaskId = taskId; }
export function _getCrashReportTaskIdForTesting(): string { return _crashReportTaskId; }
export function _setInWarmPollWindowForTesting(value: boolean): void { _inWarmPollWindow = value; }

// ── OAuth credential rotation + ClaudeTimeoutError ───────────────────────────
import {
  recordInitialOAuthAuthHash,
  rotateOAuthCredentialsIfChanged as _rotateOAuthCredentialsIfChanged,
  ClaudeTimeoutError,
} from './oauth-rotation.js';
export { recordInitialOAuthAuthHash, ClaudeTimeoutError };

/** Rotate OAuth credentials if changed, bound to the local apiPost. */
export function rotateOAuthCredentialsIfChanged(taskId: string): Promise<void> {
  return _rotateOAuthCredentialsIfChanged(taskId, apiPost);
}

// ── Build configuration (from project settings, passed via claim response) ───
export type { DirectoryConfig, MigrationConfig } from './utils/test-runners.js';
export type { BuildConfig };

// ── Environment ─────────────────────────────────────────────────────────────
const ORCHESTRATOR_URL = process.env.ORCHESTRATOR_URL || 'http://localhost:8080';
let AGENT_TOKEN = process.env.AGENT_SECRET_TOKEN || '';
const VM_INSTANCE_ID = process.env.VM_INSTANCE_ID || 'local';
export const WORKSPACE = process.env.AGENT_WORKSPACE || '/tmp/workspace';
// Root of the git object cache pre-baked into the agent VM image (see
// infra/packer/agent.pkr.hcl). A bare mirror lives at
// <root>/<owner>/<repo>.git; setupWorkspace passes it via
// `git clone --reference-if-able` so cold VMs reuse pre-fetched objects
// instead of re-downloading the full target repo every task.
export const GIT_REFERENCE_CACHE_ROOT = process.env.GIT_REFERENCE_CACHE_ROOT || '/opt/agent-cache/git';

// Populated at startup by fetchVmImageIdentity(); stays null in local dev /
// tests where the GCP metadata service is unreachable so vm-heartbeat sends
// a null vm_image_name (rather than 'unknown', which would pollute
// compute._vm_images and skew get_stale_image_vms accounting).
let vmImageName: string | null = null;
let vmImageTs: number | null = null;
// Content-hash fingerprint of /opt/agent/src baked into the image by packer
// (see infra/packer/agent.pkr.hcl). Read once at startup and included in
// vm-heartbeats and task heartbeats so stale-image diagnostics can identify
// the exact agent runtime revision independently of image name or build timestamp.
let agentRuntimeFingerprint: string | null = null;

/** Fetch the GCP boot-disk image path from the instance metadata service.
 *  Returns the short image name and extracted unix build timestamp, or
 *  falls back to { image_name: null, image_ts: null } on any error so
 *  callers can distinguish "unknown VM" from a real image. */
export async function fetchVmImageIdentity(): Promise<{ image_name: string | null; image_ts: number | null }> {
  try {
    const controller = new AbortController();
    const timer = setTimeout(() => { controller.abort(); }, 2000);
    const res = await fetch(
      'http://metadata.google.internal/computeMetadata/v1/instance/image',
      { headers: { 'Metadata-Flavor': 'Google' }, signal: controller.signal },
    );
    clearTimeout(timer);
    if (!res.ok) return { image_name: null, image_ts: null };
    const path = (await res.text()).trim();
    // path = "projects/<proj>/global/images/blueprint-agent-v2-1730000000"
    const name = path.split('/').pop() ?? null;
    if (!name) return { image_name: null, image_ts: null };
    const tsMatch = /[-_](\d{9,})$/.exec(name);
    const image_ts = tsMatch ? parseInt(tsMatch[1], 10) : null;
    return { image_name: name, image_ts };
  } catch {
    return { image_name: null, image_ts: null };
  }
}

/** Read the baked agent-runtime fingerprint from the well-known path written
 *  by the packer provisioner. Returns null in local dev / test environments
 *  where the file is absent so callers can distinguish "no fingerprint" from
 *  a real hash. */
export function readAgentRuntimeFingerprint(): string | null {
  try {
    const raw = fs.readFileSync('/opt/agent/AGENT_RUNTIME_FINGERPRINT', 'utf-8').trim();
    const m = /^AGENT_RUNTIME_FINGERPRINT=([0-9a-f]{64})$/.exec(raw);
    return m ? m[1] : null;
  } catch {
    return null;
  }
}

/** Get the current agent token (may have been refreshed since startup). */
export function getAgentToken(): string { return AGENT_TOKEN; }

/** Get the current API headers (Content-Type + X-Agent-Token). Always reads
 *  the live AGENT_TOKEN, so callers don't need to guard against rotation. */
export function getAgentHeaders(): Record<string, string> {
  return { 'Content-Type': 'application/json', 'X-Agent-Token': AGENT_TOKEN };
}

const apiHeaders: Record<string, string> = {
  'Content-Type': 'application/json',
  'X-Agent-Token': AGENT_TOKEN,
};

initTokenModule({
  getOrchestratorUrl: () => ORCHESTRATOR_URL,
  getHeaders: () => ({ ...apiHeaders }),
  getToken: () => AGENT_TOKEN,
  setToken: (newToken: string) => { AGENT_TOKEN = newToken; apiHeaders['X-Agent-Token'] = AGENT_TOKEN; },
  log,
});

// ── Helpers ──────────────────────────────────────────────────────────────────
export function strVal(v: unknown, fallback = ''): string {
  return typeof v === 'string' ? v : fallback;
}

/**
 * Resolve a task's base branch through the per-project cascade:
 * ``payload.base_branch`` → ``task.project_metadata.integration_branch`` → ``fallback``.
 *
 * The backend stamps every claim response's ``project_metadata.integration_branch``
 * (see backend/app/routers/agent.py), so any handler that defaults to ``'main'``
 * without consulting that field silently breaks projects whose integration branch
 * is something else (e.g. ``master``, ``production``, ``develop``). Use this helper
 * everywhere a handler needs the project's integration branch.
 */
export function resolveBaseBranch(
  payload: Record<string, unknown>,
  task: Record<string, unknown>,
  fallback = 'main',
): string {
  const fromPayload = strVal(payload.base_branch);
  if (fromPayload) return fromPayload;
  const meta = task.project_metadata as Record<string, unknown> | undefined;
  const fromMeta = strVal(meta?.integration_branch);
  if (fromMeta) return fromMeta;
  return fallback;
}


function writePrivateFileSync(path: string, contents: string): void {
  fs.writeFileSync(path, contents, { encoding: 'utf-8', mode: 0o600 });
  try { fs.chmodSync(path, 0o600); } catch { /* best effort on non-POSIX filesystems */ }
}

/**
 * Shared retry/auth/backoff core for orchestrator HTTP calls.
 *
 * Owns: per-attempt AbortController/timeout, 401 token-refresh-once retry,
 * 429 Retry-After honoring, 4xx fail-fast, 5xx exponential backoff, and the
 * final "failed after N retries" surface.
 *
 * `init()` is invoked fresh per attempt so apiHeaders mutations from a
 * token refresh land on the next request without re-plumbing.
 *
 * `responseInterceptor` lets POST inject the "/api/agent/status 401 → task
 * already terminal" success short-circuit that GET has no equivalent for.
 * Returning a non-undefined value short-circuits with that value; throwing
 * propagates through the outer catch where 4xx errors bail out of the loop.
 */
type ResponseInterceptor = (status: number, text: string) => unknown;

export async function apiRequest(
  method: string,
  path: string,
  init: () => RequestInit,
  retries = API_MAX_RETRIES,
  timeoutMs = API_TIMEOUT,
  responseInterceptor?: ResponseInterceptor,
): Promise<unknown> {
  let tokenRefreshed = false;
  for (let attempt = 0; attempt < retries; attempt++) {
    try {
      const controller = new AbortController();
      const timeout = setTimeout(() => { controller.abort(); }, timeoutMs);
      const res = await fetch(`${ORCHESTRATOR_URL}${path}`, { ...init(), signal: controller.signal });
      clearTimeout(timeout);
      if (res.ok) return await res.json() as unknown;
      const text = await res.text();

      if (responseInterceptor) {
        const intercepted = responseInterceptor(res.status, text);
        if (intercepted !== undefined) return intercepted;
      }

      // 401: attempt token refresh at most once, then retry
      if (res.status === 401) {
        if (tokenRefreshed) throw new Error(`${method} ${path} failed (401): ${text}`);
        log(`${method} ${path} got 401 — attempting token refresh`);
        const refreshed = await tryRefreshToken();
        if (!refreshed) throw new Error(`${method} ${path} failed (401): ${text}`);
        tokenRefreshed = true;
        continue;
      }
      // 429: retry after Retry-After header. Backend uses this for
      // temporary-contention signals (e.g. row-locked in /api/agent/status);
      // treating it as transient keeps the agent's retry loop aligned with
      // 5xx handling without tripping Cloud Run ContainerError classification.
      if (res.status === 429) {
        if (attempt === retries - 1) {
          throw new Error(`${method} ${path} failed (${res.status}): ${text}`);
        }
        const retryAfterSec = parseInt(res.headers.get('Retry-After') || '5', 10);
        const delayMs = Math.max(1000, Math.min(retryAfterSec * 1000, API_RETRY_BACKOFF_MAX_MS));
        log(`${method} ${path} got 429 (rate-limited/locked), retrying in ${delayMs}ms`);
        await new Promise(r => setTimeout(r, delayMs));
        continue;
      }
      // Other 4xx errors are not transient — fail immediately
      if (res.status < 500) {
        throw new Error(`${method} ${path} failed (${res.status}): ${text}`);
      }
      // 5xx: retry if we have attempts left
      if (attempt === retries - 1) {
        throw new Error(`${method} ${path} failed (${res.status}): ${text}`);
      }
    } catch (e) {
      // Re-throw 4xx errors and final-attempt errors immediately. 429 is
      // intentionally excluded from the 4xx bail-out via the explicit check
      // above, which uses `continue` before this catch runs.
      if (attempt === retries - 1 || (e instanceof Error && /failed \(4\d{2}\)/.test(e.message))) throw e;
    }
    await new Promise(r => setTimeout(r, Math.min(1000 * Math.pow(2, attempt) + Math.random() * 1000, API_RETRY_BACKOFF_MAX_MS)));
  }
  throw new Error(`${method} ${path} failed after ${retries} retries`);
}

export async function apiPost(path: string, body: Record<string, unknown> = {}, retries = API_MAX_RETRIES, timeoutMs = API_TIMEOUT) {
  // Cap logs field to avoid 422 from backend's max_length=500000 validation.
  // A 422 is a 4xx = no retry = agent dies silently.
  if (body.logs && typeof body.logs === 'string' && body.logs.length > LOGS_MAX_LENGTH) {
    body = { ...body, logs: body.logs.slice(0, LOGS_MAX_LENGTH) + '\n...(truncated)' };
  }
  return apiRequest(
    'POST', path,
    () => ({ method: 'POST', headers: apiHeaders, body: JSON.stringify(body) }),
    retries, timeoutMs,
    (status, text) => {
      // 401 with "no longer active": task completed/failed — our previous POST worked.
      // This happens when a status COMPLETED POST succeeded but the response was lost,
      // so the agent retries and hits JWT revocation. Treat as success for status path,
      // hard-fail (no retry) for everything else.
      if (status === 401 && text.includes('no longer active')) {
        if (path === '/api/agent/status') {
          log(`POST ${path} got 401 (task terminated) — previous POST likely succeeded`);
          return { status: 'already_terminal', task_id: body.task_id };
        }
        throw new Error(`POST ${path} failed (401): ${text}`);
      }
      return undefined;
    },
  );
}

export type { StaleResultEnvelope };
export { createStaleResultEnvelope, parseStaleResultFile, staleResultBackoffMs, shouldAbandonStaleResult, markStaleResultFailure };

function writeStaleResultFile(path: string, envelope: StaleResultEnvelope): void {
  writePrivateFileSync(path, JSON.stringify(envelope));
}

const pendingStatusFlagsByTask = new Map<string, Record<string, unknown>>();

function setPendingStatusFlag(taskId: string, key: string, value: unknown): void {
  const current = pendingStatusFlagsByTask.get(taskId) ?? {};
  pendingStatusFlagsByTask.set(taskId, { ...current, [key]: value });
}

function clearPendingStatusFlags(taskId: string): void {
  pendingStatusFlagsByTask.delete(taskId);
}

function statusFlagsForTask(taskId: string): Record<string, unknown> {
  return pendingStatusFlagsByTask.get(taskId) ?? {};
}

/**
 * True when an error is the backend's benign "task went terminal" 401 — i.e.
 * the task was CANCELLED/COMPLETED/superseded between /api/agent/claim and a
 * subsequent task-scoped GET (notably the startup /api/agent/github/token
 * fetch, which is intentionally NOT on verify_agent_token's idempotent-path
 * allowlist). The thrown apiGet error carries the shape
 * "GET /api/agent/... failed (401): {...\"Task is no longer active\"...}".
 *
 * This is NOT a code fault — it must not be recorded as failure_category
 * 'fatal'. Mirrors the graceful handling already in apiPost's 401 interceptor
 * (~line 362) and the heartbeat 4xx self-terminate branch (~line 1012).
 */
export function isTaskNoLongerActiveError(e: unknown): boolean {
  if (!(e instanceof Error)) return false;
  return /failed \(401\)/.test(e.message) && /no longer active/i.test(e.message);
}

function isTerminalStatus(status: unknown): boolean {
  return status === 'COMPLETED' || status === 'FAILED' || status === 'CANCELLED';
}

async function applyFinalStatusPrerequisites(
  taskId: string,
  payload: Record<string, unknown>,
): Promise<Record<string, unknown>> {
  if (!isTerminalStatus(payload.status)) return payload;
  try {
    await rotateOAuthCredentialsIfChanged(taskId);
    return payload;
  } catch (e) {
    const message = sanitize(e instanceof Error ? e.message : String(e)).slice(0, 500);
    log(`[ERROR] OAuth rotation failed; failing task with oauth_rotation_failed so the backend does not reuse a revoked token: ${message}`);
    const existingLogs = typeof payload.logs === 'string' && payload.logs.trim()
      ? `${payload.logs}\n\n`
      : '';
    return {
      ...payload,
      status: 'FAILED',
      artifact_type: 'ERROR_LOG',
      logs: `${existingLogs}OAuth credential rotation failed after task execution: ${message}`,
      failure_category: 'oauth_rotation_failed',
      oauth_rotation_failed: true,
      oauth_rotation_error: message,
    };
  }
}

export async function persistAndPostStatus(taskId: string, payload: Record<string, unknown>) {
  const resultPath = `/tmp/task_${taskId}_result.json`;
  const statusPayload = await applyFinalStatusPrerequisites(taskId, payload);
  const enrichedPayload = enrichStatusPayload(taskId, statusPayload);
  writeStaleResultFile(resultPath, createStaleResultEnvelope(enrichedPayload));
  const result = await apiPost('/api/agent/status', enrichedPayload);
  try { fs.unlinkSync(resultPath); } catch { /* ignore — file already gone */ }
  clearPendingStatusFlags(taskId);
  return result;
}

// Phase 0 telemetry — start-of-role timestamp captured by main() after a
// successful claim and reset at the start of each warm role. Used by
// enrichStatusPayload to derive setup_secs from firstToolUseAt.
let roleClaimTimestamp: number | null = null;

// Phase 0 telemetry — claim → handler-complete elapsed seconds. Captured at
// the success path of runRoleOnce() right after the handler returns; rides on
// the next status payload via enrichStatusPayload. Reset to null between
// roles in the role-loop telemetry-reset block. Stays null on failure paths
// so the backend doesn't conflate fatal-error duration with completion time.
let handlerDurationSecsForPayload: number | null = null;

/** Test-only: seed the role-claim timestamp so a test can simulate
 *  timing without going through main(). */
export function _setRoleClaimTimestampForTesting(value: number | null): void {
  roleClaimTimestamp = value;
}

/** Test-only: read the role-claim timestamp. */
export function _getRoleClaimTimestampForTesting(): number | null {
  return roleClaimTimestamp;
}

/** Test-only: seed the handler-duration value so a test can simulate the
 *  post-handler capture without invoking the full role-loop. */
export function _setHandlerDurationForTesting(value: number | null): void {
  handlerDurationSecsForPayload = value;
}

/** Test-only: read the handler-duration value. */
export function _getHandlerDurationForTesting(): number | null {
  return handlerDurationSecsForPayload;
}

function computeSetupSecs(): number | null {
  if (roleClaimTimestamp === null || firstToolUseAt === null) return null;
  const diff = (firstToolUseAt - roleClaimTimestamp) / 1000;
  // Defensive: never report negative setup_secs (clock skew, mis-ordered timestamps).
  return diff >= 0 ? diff : null;
}

function enrichStatusPayload(taskId: string, payload: Record<string, unknown>): Record<string, unknown> {
  // Phase 0 telemetry: setup_secs (claim → first tool use), warm_start, and
  // prior_role_vm_id ride along on every status payload. The backend's
  // task_lifecycle.py persists them under task.context["_timing"] when
  // present and ignores them otherwise. Compute eagerly so the shape is
  // stable across cold/warm/failed roles.
  const telemetry: Record<string, unknown> = {};
  const setupSecs = computeSetupSecs();
  if (setupSecs !== null) telemetry.setup_secs = setupSecs;
  // handler_duration_secs is captured by runRoleOnce after the handler returns
  // successfully; null on failure paths so a fatal-error duration doesn't get
  // logged as completion time.
  if (handlerDurationSecsForPayload !== null) {
    telemetry.handler_duration_secs = handlerDurationSecsForPayload;
  }
  // warm_start is a meaningful boolean even when false (cold roles); always
  // emit so the backend can distinguish "missing" from "explicitly false".
  telemetry.warm_start = isWarmStart;
  // prior_role_vm_id is the VM that ran the prior role. On a warm VM that's
  // ourselves (the same VM ran the prior role and is now running this one).
  // On cold roles there is no prior, so we send null.
  telemetry.prior_role_vm_id = isWarmStart ? (process.env.VM_INSTANCE_ID || null) : null;
  // Reviewer findings telemetry: when a fresh-reviewer pass ran and wrote
  // reviewer_findings into the self-review JSON, surface counts so the backend
  // can store them in task.context for catch-rate measurement (e.g. whether
  // reviewer-flagged categories were later confirmed by the human reviewer).
  // Best-effort: missing file or parse errors are silently ignored.
  if (isTerminalStatus(payload.status)) {
    const srPath = `/tmp/self_review_${taskId}.json`;
    try {
      const srRaw = fs.readFileSync(srPath, 'utf-8');
      const srData = JSON.parse(srRaw) as Record<string, unknown>;
      if (Array.isArray(srData.reviewer_findings) && srData.reviewer_findings.length > 0) {
        const findings = srData.reviewer_findings as Array<Record<string, unknown>>;
        telemetry.reviewer_findings_count = findings.length;
        telemetry.reviewer_critical_count = findings.filter(
          f => typeof f.severity === 'string' && f.severity === 'CRITICAL'
        ).length;
      }
    } catch { /* self-review file absent or unparseable — omit telemetry fields */ }
  }
  // Final flush of any buffered fix-loop events before the terminal status
  // post lands — heartbeats are best-effort, so this is the safety net that
  // guarantees the per-task fix_loop_history is at least eventually accurate.
  // Merge with any events the caller already passed in (rare, but explicit).
  const flushedEvents = collectFixLoopEventsForFlush();
  let mergedFixLoopEvents: unknown = (payload as { fix_loop_events?: unknown }).fix_loop_events;
  if (flushedEvents.length > 0) {
    const merged = Array.isArray(mergedFixLoopEvents)
      ? [...(mergedFixLoopEvents as unknown[]), ...flushedEvents]
      : flushedEvents;
    // Backend caps the field at 20 per request — drop oldest first so the
    // most recent terminal events (e.g. exit_reason='passed') always land.
    mergedFixLoopEvents = merged.length > 20 ? merged.slice(-20) : merged;
  }
  return {
    provider: PROVIDER,
    auth_mode: AUTH_MODE,
    model: process.env.LLM_MODEL || undefined,
    execution_mode: LLM_EXECUTION_MODE,
    task_id: taskId,
    ...telemetry,
    ...payload,
    ...statusFlagsForTask(taskId),
    ...(mergedFixLoopEvents !== undefined ? { fix_loop_events: mergedFixLoopEvents } : {}),
  };
}

export async function apiGet(path: string, retries = API_MAX_RETRIES) {
  return apiRequest('GET', path, () => ({ headers: apiHeaders }), retries, API_TIMEOUT);
}

export function log(msg: string) {
  console.log(`[${new Date().toISOString()}] ${msg}`);
}

// ── Warm-VM continuity polling (Phase 2a) ───────────────────────────────────
// pollForNextRole is the agent-side counterpart to the backend's
// /api/agent/next-role + /api/agent/vm-heartbeat endpoints. After a role
// finishes successfully, the agent calls this function instead of exiting.
// It polls the orchestrator for a warm-pinned QUEUED task targeting our VM,
// firing vm-heartbeat keepalives in parallel so the reaper doesn't kill us
// during the poll window. Returns either a claim (next task to run) or an
// exit (no more roles in this chain).
//
// The agent reads neither warm_continuity_enabled nor next_role_poll_timeout_secs
// — the backend gates the feature; the agent just calls the endpoints and
// honours whatever response shape comes back. The only knob agents see is
// the poll timeout default (180s), which matches the backend default.

export interface PollResult {
  action: 'claim' | 'exit';
  taskId?: string;
  reason?: string;
  // On a warm-start claim the backend mints a fresh JWT scoped to the new
  // task. The agent must rotate AGENT_TOKEN to this value before any
  // subsequent task-scoped call (status updates, github proxy, claim
  // idempotent-replay), since the prior task's JWT is terminal and only
  // authenticates the idempotent next-role/vm-heartbeat paths.
  agentToken?: string;
}

export interface PollForNextRoleOptions {
  vmInstanceId: string;
  priorTaskId: string;
  priorPrNumber: number | null;
  apiBase: string;
  agentJwt: string;
  pollIntervalMs?: number;
  timeoutMs?: number;
  heartbeatIntervalMs?: number;
}

export async function pollForNextRole(opts: PollForNextRoleOptions): Promise<PollResult> {
  const {
    vmInstanceId,
    priorTaskId,
    priorPrNumber,
    apiBase,
    agentJwt,
    pollIntervalMs = 5000,
    timeoutMs = 180_000,
    heartbeatIntervalMs = 30_000,
  } = opts;

  const start = Date.now();
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    'X-Agent-Token': agentJwt,
  };

  // Shared revoke flag — set by either heartbeat path or next-role path on
  // an explicit 401/403 from the backend. Polling without a valid JWT just
  // burns the timeout, so we exit fast and let the orchestrator cold-
  // provision a fresh VM with a freshly minted token.
  // Wrapper object: type-narrowing in TypeScript can't see through async
  // closures that mutate a let-bound boolean, so the unnecessary-condition
  // rule fires false-positively. A field-on-object dodges that limitation.
  const authState: { revoked: boolean } = { revoked: false };

  // VM-heartbeat keepalive — runs in parallel with the poll loop so the
  // reaper sees this VM as alive even though the task heartbeat stopped
  // when the prior role completed. setInterval, NOT setTimeout-recursion,
  // so a slow heartbeat call doesn't accumulate latency into the poll.
  const heartbeatTimer = setInterval(() => {
    void (async () => {
      try {
        const res = await fetch(`${apiBase}/api/agent/vm-heartbeat`, {
          method: 'POST',
          headers,
          body: JSON.stringify({ vm_instance_id: vmInstanceId, vm_image_name: vmImageName, vm_image_build_ts: vmImageTs, agent_runtime_fingerprint: agentRuntimeFingerprint }),
        });
        if (res.status === 401 || res.status === 403) {
          log(`pollForNextRole vm-heartbeat got ${String(res.status)} — auth revoked, will exit poll`);
          authState.revoked = true;
        }
      } catch (e) {
        log(`pollForNextRole vm-heartbeat error: ${String(e)}`);
      }
    })();
  }, heartbeatIntervalMs);

  // Single fire-and-forget initial heartbeat so we don't wait
  // heartbeatIntervalMs for the first liveness signal — the next-role poll
  // itself records liveness backend-side, but explicit heartbeat is the
  // documented contract.
  void (async () => {
    try {
      const res = await fetch(`${apiBase}/api/agent/vm-heartbeat`, {
        method: 'POST',
        headers,
        body: JSON.stringify({ vm_instance_id: vmInstanceId, vm_image_name: vmImageName, vm_image_build_ts: vmImageTs, agent_runtime_fingerprint: agentRuntimeFingerprint }),
      });
      if (res.status === 401 || res.status === 403) {
        log(`pollForNextRole initial vm-heartbeat got ${String(res.status)} — auth revoked, will exit poll`);
        authState.revoked = true;
      }
    } catch (e) {
      log(`pollForNextRole initial vm-heartbeat error: ${String(e)}`);
    }
  })();

  try {
    while (Date.now() - start < timeoutMs) {
      if (authState.revoked) {
        return { action: 'exit', reason: 'auth_revoked' };
      }
      try {
        const res = await fetch(`${apiBase}/api/agent/next-role`, {
          method: 'POST',
          headers,
          body: JSON.stringify({
            vm_instance_id: vmInstanceId,
            prior_task_id: priorTaskId,
            prior_pr_number: priorPrNumber,
          }),
        });
        // 401/403 — JWT revoked or otherwise rejected. Polling further is
        // hopeless; exit so the orchestrator can cold-provision with fresh
        // creds. We treat this as terminal even on the first iteration.
        if (res.status === 401 || res.status === 403) {
          log(`pollForNextRole next-role got ${String(res.status)} — auth revoked, exiting`);
          return { action: 'exit', reason: 'auth_revoked' };
        }
        // 204 No Content — keep polling.
        if (res.status === 204) {
          await new Promise(r => setTimeout(r, pollIntervalMs));
          continue;
        }
        // 200 OK with a JSON body that selects claim or exit.
        if (res.status === 200) {
          let body: { action?: string; task_id?: string; reason?: string; agent_token?: string } = {};
          try {
            body = await res.json() as { action?: string; task_id?: string; reason?: string; agent_token?: string };
          } catch (e) {
            log(`pollForNextRole: 200 with unparseable body: ${String(e)}`);
            await new Promise(r => setTimeout(r, pollIntervalMs));
            continue;
          }
          if (body.action === 'claim' && typeof body.task_id === 'string') {
            return {
              action: 'claim',
              taskId: body.task_id,
              agentToken: typeof body.agent_token === 'string' ? body.agent_token : undefined,
            };
          }
          if (body.action === 'exit') {
            return { action: 'exit', reason: body.reason };
          }
          log(`pollForNextRole: 200 with unrecognised action=${String(body.action)}`);
          await new Promise(r => setTimeout(r, pollIntervalMs));
          continue;
        }
        // Other non-2xx — treat as transient, log and keep polling.
        log(`pollForNextRole: HTTP ${String(res.status)} — continuing to poll`);
        await new Promise(r => setTimeout(r, pollIntervalMs));
      } catch (e) {
        // Network error, JSON error etc. Don't crash the agent — keep polling.
        log(`pollForNextRole fetch error: ${String(e)}`);
        await new Promise(r => setTimeout(r, pollIntervalMs));
      }
    }
    if (authState.revoked) {
      return { action: 'exit', reason: 'auth_revoked' };
    }
    return { action: 'exit', reason: 'poll_timeout' };
  } finally {
    clearInterval(heartbeatTimer);
  }
}

// ── Tool-use ring buffer ────────────────────────────────────────────────────
// Captures the most recent tool invocations so a post-mortem on AGENT_TIMEOUT
// reap can see what the agent was doing in the seconds before the heartbeat
// went silent. Sent with every heartbeat; the backend persists it as
// task.context._tool_trace, so the last-successful-heartbeat value IS the
// death-trace when the reaper later kills the task.
type ToolTraceEntry = { t: string; tool: string; input_preview: string };
const _toolTrace: ToolTraceEntry[] = [];
const TOOL_TRACE_MAX = 20;

// Phase 0 telemetry — first-tool-use timestamp per role. Captured by
// recordToolUse on its first call after a fresh role starts. Reset to null
// at the start of main() and at the start of each warm role so the next
// role gets its own setup_secs measurement. Module-level mutable so the
// SDK message loop (which records tool uses) doesn't need a handle to
// per-role state.
let firstToolUseAt: number | null = null;

// Phase 0 telemetry — true when the current role was started via
// pollForNextRole on a warm VM, false on the initial cold-start role.
let isWarmStart = false;

export function recordToolUse(toolName: string, input: unknown): void {
  if (firstToolUseAt === null) {
    firstToolUseAt = Date.now();
  }
  let preview: string;
  try {
    preview = typeof input === 'string' ? input : JSON.stringify(input);
  } catch {
    preview = '<unserializable>';
  }
  _toolTrace.push({
    t: new Date().toISOString(),
    tool: toolName,
    input_preview: preview.slice(0, 200),
  });
  if (_toolTrace.length > TOOL_TRACE_MAX) _toolTrace.shift();
}

/** Test-only: read the first-tool-use timestamp captured this role. */
export function _getFirstToolUseAtForTesting(): number | null {
  return firstToolUseAt;
}

/** Test-only: reset per-role telemetry state between cases. */
export function _resetTelemetryForTesting(): void {
  firstToolUseAt = null;
  isWarmStart = false;
  setCurrentPromptFingerprint(null);
}

/** Test-only: set the warm-start flag (mirrors what the role loop does). */
export function _setWarmStartForTesting(value: boolean): void {
  isWarmStart = value;
}

/** Test-only: read the warm-start flag. */
export function _getWarmStartForTesting(): boolean {
  return isWarmStart;
}

/** Test-only: set the baked agent-runtime fingerprint used in heartbeats. */
export function _setAgentRuntimeFingerprintForTesting(value: string | null): void {
  agentRuntimeFingerprint = value;
}

export function snapshotToolTrace(): ToolTraceEntry[] {
  return _toolTrace.slice();
}

// Drain accumulated fix-loop events for a heartbeat / status post. Also
// runs the Claude-driven `./check` synthesizer over the current tool-trace
// snapshot — it pushes any new events into the same buffer so a single
// drain returns wrapper-emitted + synthesized events together.
//
// Capped at the backend's per-request limit (20). When more accumulate
// between heartbeats, the buffer itself drops the oldest, so callers see
// at most MAX_BUFFERED_EVENTS here.
export function collectFixLoopEventsForFlush(): FixLoopEvent[] {
  try {
    const synthesized = synthesizeCheckFixLoopEvents(snapshotToolTrace());
    for (const ev of synthesized) pushFixLoopEvent(ev);
  } catch (e) {
    // Synthesizer failures must not break the heartbeat. Log and carry on.
    log(`fix-loop synthesizer failed: ${e instanceof Error ? e.message : String(e)}`);
  }
  return drainFixLoopEventBuffer();
}

/**
 * Clear the tool-trace ring buffer.
 *
 * Called between warm roles by the role-loop reset block so the next role's
 * extractInvestigatedFiles() / heartbeat snapshot doesn't mix prior-role tool
 * uses with current-role ones. (Without this, a fix agent's tool trace would
 * carry over the reviewer's reads — biasing the next role's fingerprint.)
 */
export function clearToolTrace(): void {
  _toolTrace.length = 0;
}

// Extract repo-relative file paths the agent looked at during a task. Used to
// hand the reviewer's working set forward to the fix agent so it doesn't have
// to rediscover which files matter. Reads input_preview (truncated to 200
// chars by recordToolUse) with a regex so a JSON-truncated entry still yields
// the leading path. WORKSPACE prefix is stripped so the consumer sees
// repo-relative paths regardless of where the producing agent ran.
const _INVESTIGATION_TOOLS = new Set(['Read', 'Edit', 'Write', 'NotebookEdit', 'Grep', 'Glob']);
export function extractInvestigatedFiles(trace: ToolTraceEntry[]): string[] {
  const seen = new Set<string>();
  const ordered: string[] = [];
  for (const entry of trace) {
    if (!_INVESTIGATION_TOOLS.has(entry.tool)) continue;
    const m = entry.input_preview.match(/"(?:file_path|path)"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"/);
    if (!m) continue;
    let p: string;
    try { p = JSON.parse(`"${m[1]}"`) as string; } catch { p = m[1]; }
    p = p.replace(/\\/g, '/').trim();
    if (p.startsWith(`${WORKSPACE}/`)) p = p.slice(WORKSPACE.length + 1);
    if (p.startsWith('/tmp/workspace/')) p = p.slice('/tmp/workspace/'.length);
    if (!p || p.includes('\n') || p === '.' || p === '/') continue;
    if (!seen.has(p)) { seen.add(p); ordered.push(p); }
  }
  return ordered;
}

// ── Per-role module-state isolation (warm-VM continuity) ─────────────────────
//
// On a warm VM a single agent process runs many sequential roles. EVERY piece
// of module-level mutable state that scopes to "the current role" must be reset
// here so a reused VM cannot leak state from the prior task into the next one.
// The warm-handoff path in main() calls this exactly once per transition,
// alongside resetForNextRole (which handles the filesystem/session side).
//
// Keep this the SINGLE source of truth for per-role state resets: when adding a
// new module-level `let` that holds per-task state, reset it here rather than
// re-adding an ad-hoc reset line at the call site. `flags` lets callers opt out
// of resets they will perform with different values (e.g. the role loop sets
// isWarmStart=true rather than the cold-start default false).
interface ResetPerRoleModuleStateFlags {
  /** Value to leave isWarmStart at (warm handoff sets true; tests use false). */
  warmStart?: boolean;
}
export function resetPerRoleModuleState(flags: ResetPerRoleModuleStateFlags = {}): void {
  // Shutdown signal — must start false so a leaked "cancel" never suppresses
  // the next role's heartbeats. See the cancelRequested declaration comment.
  resetCancelRequested();
  // Phase-0 telemetry: first-tool-use + warm-start marker.
  firstToolUseAt = null;
  isWarmStart = flags.warmStart ?? false;
  roleClaimTimestamp = null;
  handlerDurationSecsForPayload = null;
  // Tool-trace ring buffer — without clearing, the next role's
  // extractInvestigatedFiles() / heartbeat trace mixes in prior-role tool uses.
  clearToolTrace();
  // Prompt fingerprint attached to heartbeats.
  setCurrentPromptFingerprint(null);
  // `./check` synthesizer's monotonic iteration counter + started_at.
  resetCheckSynthesizerState();
  // Per-task LLM call/token telemetry (efficiency direction d-91d8dede).
  resetLlmTelemetry();
}

// ── Heartbeat ───────────────────────────────────────────────────────────────
let heartbeatTimer: ReturnType<typeof setInterval> | null = null;

function collectMetrics(): { rss_mb: number; heap_used_mb: number; cgroup_mb?: number; cgroup_limit_mb?: number; disk_free_mb?: number; disk_total_mb?: number } {
  const mem = process.memoryUsage();
  const rss_mb = Math.round(mem.rss / 1024 / 1024 * 10) / 10;
  const heap_used_mb = Math.round(mem.heapUsed / 1024 / 1024 * 10) / 10;
  // Read cgroup memory (includes agent + Claude Code + all subprocesses).
  // The agent's own RSS (93MB) hides the real picture — Claude Code can use
  // several GB. MemoryMax=6G triggers cgroup OOM killer (SIGKILL, no handler).
  let cgroup_mb: number | undefined;
  let cgroup_limit_mb: number | undefined;
  try {
    const current = fs.readFileSync('/sys/fs/cgroup/memory.current', 'utf-8').trim();
    cgroup_mb = Math.round(parseInt(current, 10) / 1024 / 1024);
    const max = fs.readFileSync('/sys/fs/cgroup/memory.max', 'utf-8').trim();
    if (max !== 'max') cgroup_limit_mb = Math.round(parseInt(max, 10) / 1024 / 1024);
  } catch { /* cgroup v2 not available */ }
  try {
    const dfOut = execFileSync('df', ['-k', '/tmp'], { timeout: 5000 }).toString();
    const line = dfOut.split('\n')[1];
    if (line) {
      const parts = line.trim().split(/\s+/);
      const disk_total_mb = Math.round(parseInt(parts[1], 10) / 1024);
      const disk_free_mb = Math.round(parseInt(parts[3], 10) / 1024);
      return { rss_mb, heap_used_mb, cgroup_mb, cgroup_limit_mb, disk_free_mb, disk_total_mb };
    }
  } catch { /* disk metrics are best-effort */ }
  return { rss_mb, heap_used_mb, cgroup_mb, cgroup_limit_mb };
}

// Mutex to prevent overlapping heartbeat HTTP calls. Without this, a slow
// heartbeat (apiPost 30s timeout × 5 retries with backoffs = up to ~3min) lets
// subsequent setInterval ticks queue up async invocations behind it. They
// can't deliver until the previous one settles, but they hold microtask state
// and add pressure on the event loop. With the mutex the next tick simply
// skips and logs — last_heartbeat at the orchestrator stays correct because
// the previous in-flight call still finishes and reports.
let heartbeatInFlight = false;

function startHeartbeat(taskId: string, getMessage: () => string) {
  // Fire first heartbeat immediately — don't wait 30s.
  // If the agent crashes in the first 30s, at least one heartbeat is recorded.
  // Audit 2026-04-25 (W4/C1+C2): replace `void async` IIFE with explicit
  // .catch() handler so a synchronous throw inside the async block (e.g.
  // collectMetrics() bug, JSON.stringify failure) doesn't silently disappear.
  // The previous `void` form would drop those errors entirely — no log, no
  // unhandledRejection event, the agent just goes silent until the orchestrator
  // reaper kills it minutes later with no forensic record.
  (async () => {
    const metrics = collectMetrics();
    const fix_loop_events = collectFixLoopEventsForFlush();
    const start = Date.now();
    try {
      await apiPost('/api/agent/heartbeat', {
        task_id: taskId,
        status_message: `[${metrics.heap_used_mb}MB] ${getMessage()}`,
        metrics,
        tool_trace: snapshotToolTrace(),
        ...(fix_loop_events.length > 0 ? { fix_loop_events } : {}),
        ...(getCurrentPromptFingerprint() ? { prompt_fingerprint: getCurrentPromptFingerprint() } : {}),
        ...(agentRuntimeFingerprint ? { agent_runtime_fingerprint: agentRuntimeFingerprint } : {}),
        ...getLlmTelemetry(),
      }, HEARTBEAT_API_MAX_RETRIES, HEARTBEAT_API_TIMEOUT);
      traceHeartbeat('initial', start, Date.now(), 'ok');
    } catch (e) {
      log(`Initial heartbeat failed: ${String(e)}`);
      traceHeartbeat('initial', start, Date.now(), 'err', String(e));
      // Re-queue drained-but-undelivered events so the next heartbeat (or
      // terminal status post) can carry them. Without this, a transient
      // POST failure on the first tick silently drops the events emitted
      // since the agent started.
      if (fix_loop_events.length > 0) requeueFixLoopEvents(fix_loop_events);
    }
  })().catch((e: unknown) => { log(`Initial heartbeat outer error: ${String(e)}`); });

  heartbeatTimer = setInterval(() => { (async () => {
    // A prior tick already classified a non-transient 4xx as reassignment and
    // requested shutdown — don't keep firing heartbeats (or re-trip the
    // self-terminate path) while the 5s wind-down timer runs.
    if (cancelRequested) {
      traceHeartbeat('periodic', Date.now(), Date.now(), 'skip-in-flight', 'cancel-requested');
      return;
    }
    if (heartbeatInFlight) {
      traceHeartbeat('periodic', Date.now(), Date.now(), 'skip-in-flight');
      log('Heartbeat skipped — previous still in flight');
      return;
    }
    heartbeatInFlight = true;
    const start = Date.now();
    const metrics = collectMetrics();
    // Proactive OOM warning — SIGKILL from cgroup can't be caught, so warn while we still can
    if (metrics.cgroup_mb && metrics.cgroup_limit_mb) {
      const memPct = metrics.cgroup_mb / metrics.cgroup_limit_mb;
      if (memPct > 0.85) {
        log(`WARNING: Memory at ${(memPct * 100).toFixed(0)}% of cgroup limit (${metrics.cgroup_mb}MB / ${metrics.cgroup_limit_mb}MB) — OOM kill imminent`);
      }
    }
    const fix_loop_events = collectFixLoopEventsForFlush();
    try {
      // Tighter heartbeat-specific retry/timeout (10s × 2 vs default 30s × 5).
      // Holding the in-flight mutex through the full 150s+ default chain on a
      // sustained 5xx run starves subsequent setInterval ticks — so the
      // orchestrator never sees a fresh heartbeat even after it recovers.
      // Tighter values keep the mutex turning over.
      await apiPost('/api/agent/heartbeat', {
        task_id: taskId,
        status_message: `[${metrics.heap_used_mb}MB] ${getMessage()}`,
        metrics,
        tool_trace: snapshotToolTrace(),
        ...(fix_loop_events.length > 0 ? { fix_loop_events } : {}),
        ...(getCurrentPromptFingerprint() ? { prompt_fingerprint: getCurrentPromptFingerprint() } : {}),
        ...(agentRuntimeFingerprint ? { agent_runtime_fingerprint: agentRuntimeFingerprint } : {}),
        ...getLlmTelemetry(),
      }, HEARTBEAT_API_MAX_RETRIES, HEARTBEAT_API_TIMEOUT);
      traceHeartbeat('periodic', start, Date.now(), 'ok');
    } catch (e: unknown) {
      log(`Heartbeat failed: ${String(e)}`);
      traceHeartbeat('periodic', start, Date.now(), 'err', String(e));
      // Re-queue drained events so a transient POST failure doesn't silently
      // lose this tick's fix-loop telemetry. The next heartbeat (or the
      // terminal status post) carries them.
      if (fix_loop_events.length > 0) requeueFixLoopEvents(fix_loop_events);
      // 409 = row temporarily locked (skip_locked) — retry once after 5s.
      // Silently dropping heartbeats can let last_heartbeat go stale if the
      // status endpoint holds its FOR UPDATE lock for multiple heartbeat cycles.
      if (e instanceof Error && /failed \(409\)/.test(e.message)) {
        // Exponential backoff: 5s, then 10s. Total window 15s, within 30s interval.
        for (let retry = 0; retry < 2; retry++) {
          const delay = 5000 * (retry + 1);
          log(`Heartbeat skipped (row locked), retrying in ${delay / 1000}s (attempt ${retry + 1}/2)`);
          await new Promise(r => setTimeout(r, delay));
          const retryStart = Date.now();
          try {
            const retryMetrics = collectMetrics();
            await apiPost('/api/agent/heartbeat', {
              task_id: taskId,
              status_message: `[${retryMetrics.heap_used_mb}MB] ${getMessage()}`,
              metrics: retryMetrics,
              tool_trace: snapshotToolTrace(),
              ...(getCurrentPromptFingerprint() ? { prompt_fingerprint: getCurrentPromptFingerprint() } : {}),
              ...getLlmTelemetry(),
            }, HEARTBEAT_API_MAX_RETRIES, HEARTBEAT_API_TIMEOUT);
            log('Heartbeat retry succeeded');
            traceHeartbeat('retry-409', retryStart, Date.now(), 'ok');
            return;
          } catch (retryErr: unknown) {
            log(`Heartbeat retry ${retry + 1}/2 failed: ${String(retryErr)}`);
            traceHeartbeat('retry-409', retryStart, Date.now(), 'err', String(retryErr));
          }
        }
        return;
      }
      // Transient 4xx (429 rate-limited, 408/425 timeout) — NOT a reassignment.
      // Back off and keep heartbeating; the reaper's no-heartbeat window is the
      // backstop. Regression 2026-05-29: a transient 429 here self-terminated
      // live agents mid-task (recorded as AGENT_TIMEOUT) under orchestrator load.
      // (409 row-locked is handled by its own retry branch above.)
      if (e instanceof Error && isHeartbeatTransient(e.message)) {
        log('Heartbeat transient failure (rate-limited/timeout) — backing off, will retry next interval');
        return;
      }
      // Other 4xx (401/403/404 …) = task genuinely reassigned/deauthorized — graceful shutdown
      if (e instanceof Error && /failed \(4\d{2}\)/.test(e.message)) {
        log('Task no longer assigned to this VM — self-terminating gracefully');
        // Signal the rest of the agent (incl. any later heartbeat tick that has
        // already passed the classification gate) that we are shutting down.
        requestCancel();
        stopHeartbeat();
        // Best-effort terminal status POST BEFORE exiting. Without this the
        // task is stranded IN_PROGRESS/QUEUED with no failure_category until
        // the reaper times it out (false AGENT_TIMEOUT, discarded work). The
        // heartbeat callback is a detached setInterval tick — a thrown
        // GracefulShutdownSignal can't reach main()'s role loop from here — so
        // fire a fire-and-forget SYNC status post (curl child, same mechanism
        // as sendHeartbeatSync) that survives the imminent process.exit even
        // if the event loop never drains. 'reassigned' tells the backend this
        // was an intentional handoff, not a code fault.
        postTerminalStatusSync(taskId, 'reassigned', 'Task reassigned/deauthorized — agent self-terminating after non-transient heartbeat 4xx');
        // Allow 5s for in-flight operations to wind down before hard exit.
        // VM TTL self-destruct (1hr) is the ultimate backstop.
        setTimeout(() => process.exit(0), 5000);
        return;
      }
    } finally {
      heartbeatInFlight = false;
    }
  })().catch((e: unknown) => { log(`Heartbeat outer error (W4/C1): ${String(e)}`); }); }, HEARTBEAT_INTERVAL);
}

function stopHeartbeat() {
  if (heartbeatTimer) { clearInterval(heartbeatTimer); heartbeatTimer = null; }
  stopProactiveTokenRefresh();
  // Reset mutex so warm-VM continuity (one agent process, multiple sequential
  // tasks) starts the next task with a clean in-flight state. Without this,
  // a stuck heartbeat at role-end would leave the flag true and the new
  // task's first periodic tick would skip-and-log instead of firing.
  heartbeatInFlight = false;
}

/** Test-only: drive the heartbeat loop without running main(). */
export function _startHeartbeatForTesting(taskId: string, getMessage: () => string): void {
  startHeartbeat(taskId, getMessage);
}

/** Test-only: stop the heartbeat loop between unit tests. */
export function _stopHeartbeatForTesting(): void {
  stopHeartbeat();
}

/** Synchronous heartbeat — use between blocking execFileSync calls to prevent reaper from killing us.
 *  Fires curl in a child process (sync) so the event loop doesn't need to be free.
 *  `timeoutMs` defaults to 10s for normal between-step heartbeats (git clone
 *  retry, npm ci, push, etc — degraded networks need room). Signal-handler
 *  callers (`_crashReport` SIGTERM/uncaughtException paths) pass 3s instead so
 *  one slow curl can't chew the GCE 30s grace window before SIGKILL. */
export function sendHeartbeatSync(
  taskId: string,
  statusMessage: string,
  extras?: Record<string, unknown>,
  timeoutMs = 10_000,
) {
  const start = Date.now();
  try {
    const metrics = collectMetrics();
    const payload = JSON.stringify({
      task_id: taskId,
      status_message: `[${metrics.heap_used_mb}MB] ${statusMessage}`,
      metrics,
      ...getLlmTelemetry(),
      ...(extras || {}),
    });
    execFileSync('curl', [
      '-sf', '-X', 'POST',
      `${ORCHESTRATOR_URL}/api/agent/heartbeat`,
      '-H', 'Content-Type: application/json',
      '-H', `X-Agent-Token: ${AGENT_TOKEN}`,
      '-d', payload,
    ], { timeout: timeoutMs, stdio: 'ignore' });
    traceHeartbeat('sync', start, Date.now(), 'ok');
  } catch (e) {
    log(`Sync heartbeat failed: ${e instanceof Error ? e.message : String(e)} (non-fatal)`);
    traceHeartbeat('sync', start, Date.now(), 'err', e instanceof Error ? e.message : String(e));
  }
}

/**
 * Synchronous, fire-and-forget terminal status POST — used from the heartbeat
 * self-terminate path (cluster C7) where a thrown GracefulShutdownSignal can't
 * reach main()'s role loop (the heartbeat is a detached setInterval tick) and
 * the imminent process.exit would abandon any pending async apiPost. Fires curl
 * in a child process (sync) so the request lands even though the event loop is
 * about to stop. Without this the reassigned task is stranded IN_PROGRESS with
 * no failure_category until the reaper times it out (false AGENT_TIMEOUT).
 *
 * Best-effort: a failed POST writes the stale-result file as the fallback
 * (the next process / warm role retries it), mirroring persistAndPostStatus.
 */
export function postTerminalStatusSync(
  taskId: string,
  failureCategory: FailureCategory,
  message: string,
  timeoutMs = 5_000,
) {
  if (!taskId) return;
  const enriched = enrichStatusPayload(taskId, {
    task_id: taskId,
    status: 'FAILED',
    artifact_type: 'ERROR_LOG',
    logs: sanitize(message),
    failure_category: failureCategory,
  });
  // Persist a stale-result file FIRST so a retry path survives even if the
  // curl below times out under a partition.
  try {
    writeStaleResultFile(`/tmp/task_${taskId}_result.json`, createStaleResultEnvelope(enriched));
  } catch { /* best effort — directory may be unwritable */ }
  try {
    execFileSync('curl', [
      '-sf', '-X', 'POST',
      `${ORCHESTRATOR_URL}/api/agent/status`,
      '-H', 'Content-Type: application/json',
      '-H', `X-Agent-Token: ${AGENT_TOKEN}`,
      '-d', JSON.stringify(enriched),
    ], { timeout: timeoutMs, stdio: 'ignore' });
    // POST landed — drop the stale-result fallback so it isn't re-posted.
    try { fs.unlinkSync(`/tmp/task_${taskId}_result.json`); } catch { /* already gone */ }
    log(`Terminal status (${failureCategory}) posted synchronously for task ${taskId}`);
  } catch (e) {
    log(`Sync terminal status POST failed: ${e instanceof Error ? e.message : String(e)} — stale-result fallback retained for retry`);
  }
}

// ── WebSocket Log Stream ────────────────────────────────────────────────────
interface LogEntry { timestamp: number; content: string; }
const logBuffer: LogEntry[] = [];

export function appendToLogBuffer(content: string) {
  if (logBuffer.length >= MAX_LOG_BUFFER) logBuffer.shift();
  logBuffer.push({ timestamp: Date.now(), content });
}

export function snapshotLogBuffer(): LogEntry[] {
  return logBuffer.slice();
}

export function flushLogBuffer(ws: WebSocket) {
  while (logBuffer.length > 0) {
    const entry = logBuffer[0];
    try { ws.send(JSON.stringify({ type: 'log', content: entry.content })); } catch { break; }
    logBuffer.shift();
  }
}

async function connectLogStream(taskId: string): Promise<WebSocket | null> {
  const wsUrl = ORCHESTRATOR_URL.replace(/^http/, 'ws');
  const maxRetries = 3;
  const baseDelay = 2000;
  let lastFailure = 'unknown';

  for (let attempt = 0; attempt < maxRetries; attempt++) {
    try {
      const ws = await new Promise<WebSocket | null>((resolve) => {
        const conn = new WebSocket(`${wsUrl}/api/agent/ws/${taskId}`);
        let settled = false;
        const finish = (socket: WebSocket | null, failure?: string) => {
          if (settled) return;
          settled = true;
          if (failure) lastFailure = failure;
          resolve(socket);
        };
        const timeout = setTimeout(() => {
          try { conn.terminate(); } catch { /* ignore */ }
          finish(null, 'connect timeout');
        }, 5000);
        conn.on('open', () => {
          clearTimeout(timeout);
          conn.send(JSON.stringify({ token: AGENT_TOKEN }));
          finish(conn);
        });
        conn.on('error', (err: Error) => {
          clearTimeout(timeout);
          finish(null, err.message || 'websocket error');
        });
      });
      if (ws) {
        flushLogBuffer(ws);
        return ws;
      }
    } catch (err) {
      lastFailure = err instanceof Error ? err.message : String(err);
    }
    if (attempt < maxRetries - 1) {
      await new Promise(r => setTimeout(r, baseDelay * Math.pow(2, attempt)));
    }
  }
  setPendingStatusFlag(taskId, 'websocket_connect_failed', true);
  setPendingStatusFlag(taskId, 'websocket_connect_error', sanitize(lastFailure).slice(0, 500));
  setPendingStatusFlag(taskId, 'websocket_connect_attempts', maxRetries);
  return null;
}

export { sanitize };

export function readHandlerOutput(
  taskId: string,
  outputType: string,
  ws: WebSocket | null,
): unknown {
  const filePath = `/tmp/${outputType}_${taskId}.json`;
  let raw: string;
  try {
    raw = fs.readFileSync(filePath, 'utf-8');
  } catch (err: unknown) {
    sendLog(ws, `[WARNING] Failed to read ${filePath}: ${err instanceof Error ? err.message : String(err)}\n`);
    return null;
  }
  try {
    return JSON.parse(raw) as unknown;
  } catch (err: unknown) {
    sendLog(ws, `[WARNING] Failed to parse ${filePath}: ${err instanceof Error ? err.message : String(err)}\n`);
    // Try extracting a JSON array (research findings, plans)
    // Only accept non-empty arrays; empty arrays (e.g. `[]` embedded inside an
    // object like `"direction_operations": []`) would otherwise shadow the
    // subsequent object extraction.
    const arrayResult = extractJsonArray(raw);
    if (arrayResult && Array.isArray(arrayResult) && arrayResult.length > 0) return arrayResult;
    // Try extracting a JSON object (strategic output)
    try {
      const objResult = extractJsonObject(raw, 'vision', 'direction_operations');
      if (objResult) return objResult;
    } catch (extractErr: unknown) {
      if (extractErr instanceof JsonExtractionError) {
        sendLog(ws, `[ERROR] ${extractErr.message}\n`);
      }
      throw extractErr;
    }
    return null;
  }
}

export function validateBranchName(branch: string, label: string): string {
  if (!branch) throw new Error(`${label} is required`);
  if (!/^[a-zA-Z0-9_./-]+$/.test(branch)) throw new Error(`Invalid ${label}: ${branch}`);
  if (branch.includes('..')) throw new Error(`Invalid ${label} (contains ..): ${branch}`);
  if (branch.startsWith('-')) throw new Error(`Invalid ${label} (starts with -): ${branch}`);
  return branch;
}

// Canonical task-type → branch-prefix mapping.
// Convention: {namespace}/{prefix}-{first-8-hex-of-task-uuid}
// Mirrors backend/app/shared_state.py TASK_TYPE_BRANCH_PREFIX (Python single source); keep in sync.
// Types absent from this map default to the 'task' prefix.
export const TASK_TYPE_BRANCH_PREFIX: Record<string, string> = {
  refactor: 'refactor',
  docs: 'docs',
  deps: 'deps',
  migrate: 'migrate',
};

/** Default task-branch namespace — preserves Blueprint self-repo behavior. */
export const TASK_BRANCH_NAMESPACE_DEFAULT = 'blueprint';

/** Return the canonical git branch name for a task.
 *
 *  ``namespace`` defaults to ``"blueprint"`` so existing call sites stay
 *  byte-identical; the per-project namespace flows in from
 *  ``buildConfig.task_branch_namespace`` (mirrors the backend resolver). */
export function deriveTaskBranch(
  taskType: string,
  taskId: string,
  namespace: string = TASK_BRANCH_NAMESPACE_DEFAULT,
): string {
  const prefix = TASK_TYPE_BRANCH_PREFIX[taskType] ?? 'task';
  const shortId = taskId.replace(/-/g, '').slice(0, 8);
  return `${namespace}/${prefix}-${shortId}`;
}

export function sendLog(ws: WebSocket | null, content: string) {
  const sanitized = sanitize(content);
  appendToLogBuffer(sanitized);
  if (ws && ws.readyState === WebSocket.OPEN) {
    try { ws.send(JSON.stringify({ type: 'log', content: sanitized })); } catch { /* ignore */ }
  } else {
    process.stdout.write(sanitized);
  }
}

// ── Git Helpers ─────────────────────────────────────────────────────────────
export function git(args: string[], cwd = WORKSPACE): string {
  return execFileSync('git', args, { cwd, encoding: 'utf-8', timeout: 120_000 }).trim();
}

/** Non-blocking exec that mirrors execFileSync's error shape.
 *  Use this for long-running commands (test runners, build tools) so the
 *  event loop stays free and the setInterval heartbeat keeps firing.
 *  On timeout, sends SIGTERM to the whole process group, then SIGKILL 5s
 *  later — this terminates npm/pytest worker processes that execFileSync's
 *  default signal handling leaks (stuck children block the sync call past
 *  its timeout, starving the heartbeat and tripping the reaper). */
export function execFileAsync(
  cmd: string,
  args: string[],
  opts: { cwd?: string; timeoutMs?: number; env?: NodeJS.ProcessEnv } = {},
): Promise<string> {
  const { cwd, timeoutMs = 180_000, env } = opts;
  return new Promise((resolve, reject) => {
    const stdoutChunks: Buffer[] = [];
    const stderrChunks: Buffer[] = [];
    // `timeout` is not a valid option for child_process.spawn (only execFile* and
    // spawnSync respect it), but Node silently ignores unknown options. We pass
    // it through so tests can verify the configured timeout per runner via the
    // spawn mock's call arguments. The actual timeout logic is handled by the
    // termTimer setInterval below.
    const proc = spawn(cmd, args, { cwd, env, stdio: ['ignore', 'pipe', 'pipe'], detached: true, timeout: timeoutMs } as Parameters<typeof spawn>[2]);
    // Phase 1: register the child's PID so a reset between roles can SIGKILL
    // a stuck test runner. Long-running by design (timeoutMs default 180s);
    // short utility execFile calls (git log, etc.) go through execFileSync
    // and are not tracked here.
    if (typeof proc.pid === 'number') {
      registerChildPid(proc.pid);
    }
    if (!proc.stdout || !proc.stderr) throw new Error('spawn returned no stdio streams');
    proc.stdout.on('data', (d: Buffer) => stdoutChunks.push(d));
    proc.stderr.on('data', (d: Buffer) => stderrChunks.push(d));
    let timedOut = false;
    let killTimer: NodeJS.Timeout | null = null;
    const originalPid = proc.pid;
    const processGroupStillOwned = () =>
      originalPid !== undefined
      && proc.pid === originalPid
      && proc.exitCode === null
      && proc.signalCode === null;
    const termTimer = setTimeout(() => {
      timedOut = true;
      const pid = originalPid;
      if (pid !== undefined && processGroupStillOwned()) {
        try { process.kill(-pid, 'SIGTERM'); } catch { /* already exited */ }
        killTimer = setTimeout(() => {
          if (!processGroupStillOwned()) return;
          try { process.kill(-pid, 'SIGKILL'); } catch { /* already dead */ }
        }, PROCESS_KILL_GRACE_MS);
      }
    }, timeoutMs);
    proc.on('close', (code, signal) => {
      const closeSignal = signal ?? null;
      clearTimeout(termTimer);
      if (killTimer) clearTimeout(killTimer);
      if (typeof originalPid === 'number') {
        unregisterChildPid(originalPid);
      }
      const stdout = Buffer.concat(stdoutChunks).toString('utf-8');
      const stderr = Buffer.concat(stderrChunks).toString('utf-8');
      if (code === 0 && closeSignal === null && !timedOut) {
        resolve(stdout);
        return;
      }
      const parts = [`Command failed: ${cmd} ${args.join(' ')}`];
      if (closeSignal) parts.push(`Signal: ${closeSignal}`);
      if (stdout.trim()) parts.push(stdout);
      if (stderr.trim()) parts.push(stderr);
      const err: Error & {
        stdout?: string; stderr?: string; status?: number | null; signal?: NodeJS.Signals | null; killed?: boolean; code?: string;
      } = new Error(parts.join('\n'));
      err.stdout = stdout;
      err.stderr = stderr;
      err.status = code;
      err.signal = closeSignal;
      err.killed = timedOut || closeSignal !== null;
      if (closeSignal === 'SIGKILL' && !timedOut) {
        err.code = 'EOOM';
      } else if (timedOut) {
        err.code = 'ETIMEDOUT';
      }
      reject(err);
    });
    proc.on('error', (e) => {
      clearTimeout(termTimer);
      if (killTimer) clearTimeout(killTimer);
      if (typeof originalPid === 'number') {
        unregisterChildPid(originalPid);
      }
      reject(e);
    });
  });
}

/** Commit with captured stderr. Returns true if a commit was created,
 *  false if there was nothing to commit (pre-commit hook may have reverted
 *  changes, or the working tree was already clean). Throws with full
 *  stdout+stderr for any other failure — the default execFileSync error
 *  only echoes the command, which hides the real cause. */
export function gitCommit(message: string, cwd = WORKSPACE): boolean {
  try {
    execFileSync('git', ['-C', cwd, 'commit', '-m', message], { encoding: 'utf-8' });
    return true;
  } catch (e: unknown) {
    const err = e as { stderr?: string | Buffer; stdout?: string | Buffer; message?: string };
    const stderr = (err.stderr ? err.stderr.toString() : '').trim();
    const stdout = (err.stdout ? err.stdout.toString() : '').trim();
    const combined = [stdout, stderr].filter(Boolean).join('\n');
    if (/nothing to commit|nothing added to commit|no changes added to commit/i.test(combined)) {
      return false;
    }
    throw new Error(`git commit failed: ${combined || err.message || 'unknown error'}`);
  }
}

/** Async git that doesn't block the event loop — use for long operations (push, clone)
 *  so the setInterval heartbeat can keep firing. */
export function gitAsync(args: string[], cwd = WORKSPACE, timeoutMs = 300_000): Promise<string> {
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = [];
    const errChunks: Buffer[] = [];
    const proc = spawn('git', args, { cwd, stdio: ['ignore', 'pipe', 'pipe'] });
    proc.stdout.on('data', (d: Buffer) => chunks.push(d));
    proc.stderr.on('data', (d: Buffer) => errChunks.push(d));
    const timer = setTimeout(() => { proc.kill('SIGKILL'); reject(new Error(`git ${args[0]} timed out after ${timeoutMs}ms`)); }, timeoutMs);
    proc.on('close', (code) => {
      clearTimeout(timer);
      if (code === 0) resolve(Buffer.concat(chunks).toString('utf-8').trim());
      else reject(new Error(`git ${args.join(' ')} failed (exit ${code}): ${Buffer.concat(errChunks).toString('utf-8').trim()}`));
    });
    proc.on('error', (e) => { clearTimeout(timer); reject(e); });
  });
}

/** Clone with retry — transient network failures get 3 attempts with exponential backoff. */
export async function cloneWithRetry(args: string[], timeoutMs = GIT_CLONE_TIMEOUT): Promise<void> {
  const taskId = process.env.TASK_ID || '';
  for (let attempt = 0; attempt < CLONE_MAX_RETRIES; attempt++) {
    try {
      if (attempt > 0 && taskId) sendHeartbeatSync(taskId, `Git clone retry ${attempt + 1}/${CLONE_MAX_RETRIES}...`);
      execFileSync('git', args, { encoding: 'utf-8', timeout: timeoutMs });
      return;
    } catch (e) {
      if (attempt === CLONE_MAX_RETRIES - 1) throw e;
      const delay = CLONE_RETRY_BASE_DELAY * Math.pow(2, attempt);
      log(`Git clone failed (attempt ${attempt + 1}/3), retrying in ${delay / 1000}s: ${e instanceof Error ? e.message : String(e)}`);
      try { execFileSync('rm', ['-rf', WORKSPACE], { encoding: 'utf-8' }); } catch { /* ignore */ }
      await new Promise(r => setTimeout(r, delay));
    }
  }
}

/** List open PRs whose head branch matches `branch` on `owner/repo`. */
export async function listOpenPullRequestsForBranch(
  ghToken: string, owner: string, repo: string, branch: string
): Promise<{ number: number; html_url: string }[]> {
  const url = `https://api.github.com/repos/${owner}/${repo}/pulls?state=open&head=${encodeURIComponent(`${owner}:${branch}`)}&per_page=10`;
  const controller = new AbortController();
  const timeout = setTimeout(() => { controller.abort(); }, 30_000);
  try {
    const res = await fetch(url, {
      headers: {
        'Authorization': `Bearer ${ghToken}`,
        'Accept': 'application/vnd.github.v3+json',
        'X-GitHub-Api-Version': '2022-11-28',
      },
      signal: controller.signal,
    });
    if (!res.ok) {
      log(`listOpenPullRequestsForBranch ${branch} got HTTP ${res.status} — assuming no open PRs`);
      return [];
    }
    const data = await res.json() as Array<{ number: number; html_url: string }>;
    return Array.isArray(data) ? data : [];
  } finally {
    clearTimeout(timeout);
  }
}

// ── Workspace + PR Helpers ───────────────────────────────────────────────────
export async function setupWorkspace(
  owner: string, repo: string, ghToken: string, baseBranch: string,
  branchPrefix: string, taskId: string,
  ws: WebSocket | null, statusRef: { value: string },
  buildConfig?: BuildConfig
): Promise<{ workBranch: string; shortId: string }> {
  statusRef.value = `Cloning ${owner}/${repo}...`;
  sendLog(ws, `Cloning ${owner}/${repo}...\n`);
  sendHeartbeatSync(taskId, `Cloning ${owner}/${repo}...`);
  execFileSync('rm', ['-rf', WORKSPACE], { encoding: 'utf-8' });
  const cloneUrl = `https://x-access-token:${ghToken}@github.com/${owner}/${repo}.git`;
  const cloneArgs = ['clone', '--depth', '100', '--branch', baseBranch];
  // Reuse the pre-baked git object cache when the image seeded a mirror for
  // this repo. `--reference-if-able` borrows objects from the local mirror and
  // silently falls back to a normal fetch if the path is missing or invalid,
  // so this is safe for repos the image never cached.
  const referenceRepo = `${GIT_REFERENCE_CACHE_ROOT}/${owner}/${repo}.git`;
  if (fs.existsSync(referenceRepo)) {
    cloneArgs.push('--reference-if-able', referenceRepo);
    sendLog(ws, `Using pre-baked git object cache at ${referenceRepo}\n`);
  }
  cloneArgs.push(cloneUrl, WORKSPACE);
  try {
    await cloneWithRetry(cloneArgs);
  } catch (e) {
    const err = e instanceof Error ? e : new Error(String(e));
    (err as Error & { failureCategory?: FailureCategory }).failureCategory = 'clone_failed';
    throw err;
  }
  sendHeartbeatSync(taskId, `Clone complete, configuring workspace...`);
  git(['config', 'user.email', buildConfig?.git_user_email || 'blueprint-agent@automated.dev']);
  git(['config', 'user.name', buildConfig?.git_user_name || 'Blueprint Agent']);
  const shortId = taskId.replace(/-/g, '').slice(0, 8);
  const namespace = buildConfig?.task_branch_namespace || TASK_BRANCH_NAMESPACE_DEFAULT;
  const workBranch = `${namespace}/${branchPrefix}-${shortId}`;
  // Clean up stale remote branch from previous attempt (requeue scenario).
  // CRITICAL: if the stale branch has an open PR, deleting it auto-closes the PR
  // and triggers head_ref_deleted on GitHub — silently destroying the previous
  // attempt's work. Bail out so the operator can decide what to do with the PR.
  let staleBranchExists = false;
  try {
    git(['ls-remote', '--exit-code', 'origin', workBranch]);
    staleBranchExists = true;
  } catch { /* branch doesn't exist on remote — normal case */ }
  if (staleBranchExists) {
    const openPrs = await listOpenPullRequestsForBranch(ghToken, owner, repo, workBranch);
    if (openPrs.length > 0) {
      const prList = openPrs.map(p => `#${p.number}`).join(', ');
      throw new Error(
        `Refusing to delete stale branch ${workBranch}: has ${openPrs.length} open PR(s) (${prList}) ` +
        `from a previous attempt. Resolve the PR(s) manually before retrying.`
      );
    }
    try {
      git(['push', 'origin', '--delete', workBranch]);
      sendLog(ws, `Deleted stale remote branch ${workBranch} from previous attempt (no open PRs)\n`);
    } catch (e) {
      sendLog(ws, `WARNING: Could not delete stale remote branch ${workBranch}: ${e instanceof Error ? e.message : String(e)}\n`);
    }
  }
  git(['checkout', '-b', workBranch]);
  sendLog(ws, `Working branch: ${workBranch}\n`);
  return { workBranch, shortId };
}

export async function pushAndCreatePR(
  owner: string, repo: string, taskId: string, workBranch: string,
  baseBranch: string, title: string, body: string,
  ws: WebSocket | null, statusRef: { value: string },
  skipRebase = false
): Promise<string> {
  if (!skipRebase) {
    // Rebase onto latest base branch before push to avoid merge conflicts in the PR.
    // If rebase fails, fall back to merge. If merge fails, push as-is — the review
    // pipeline will detect conflicts and spawn a fix task.
    statusRef.value = `Rebasing onto ${baseBranch}...`;
    try {
      git(['fetch', 'origin', baseBranch]);
      sendHeartbeatSync(taskId, `Fetched ${baseBranch}, rebasing...`);
      git(['rebase', `origin/${baseBranch}`]);
      sendLog(ws, `Rebased onto latest ${baseBranch}\n`);
    } catch {
      try { git(['rebase', '--abort']); } catch { /* ignore */ }
      sendLog(ws, `Rebase onto ${baseBranch} had conflicts — trying merge instead\n`);
      try {
        git(['merge', `origin/${baseBranch}`, '--no-edit']);
        sendLog(ws, `Merged ${baseBranch} successfully\n`);
      } catch {
        try { git(['merge', '--abort']); } catch { /* ignore */ }
        sendLog(ws, `Merge also conflicted — pushing as-is, review pipeline will handle\n`);
      }
    }

    // Verify no conflict markers before pushing
    try {
      const markers = execFileSync('git', ['-C', WORKSPACE, 'grep', '-rl', '<' + '<<<<<<', '--', '*.ts', '*.tsx', '*.py', '*.js', '*.jsx', '*.json'], { encoding: 'utf-8', timeout: CONFLICT_CHECK_TIMEOUT }).trim();
      if (markers) {
        sendLog(ws, `WARNING: Conflict markers found in:\n${markers}\nAborting rebase and pushing original commit.\n`);
        try { git(['rebase', '--abort']); } catch { /* ignore */ }
      }
    } catch { /* no markers found */ }
  }

  // Use async push so the event loop isn't blocked — the setInterval heartbeat
  // keeps firing during the push, preventing the reaper from killing us mid-push.
  sendHeartbeatSync(taskId, `Pushing ${workBranch}...`);
  const freshGhRes = await apiGet(`/api/agent/github/token?task_id=${taskId}`) as { data: { token: string } };
  const pushUrl = `https://x-access-token:${freshGhRes.data.token}@github.com/${owner}/${repo}.git`;
  execFileSync('git', ['-C', WORKSPACE, 'remote', 'set-url', 'origin', pushUrl], { encoding: 'utf-8' });
  await gitAsync(['push', 'origin', workBranch]);
  sendLog(ws, `Pushed to branch: ${workBranch}\n`);

  // Heartbeat after push, before PR creation — ensures the reaper sees fresh activity
  sendHeartbeatSync(taskId, `Creating pull request for ${workBranch}...`);
  statusRef.value = 'Creating pull request...';
  let prUrl = '';
  try {
    // Timeout must cover the backend's worst case: get_github_token_for_task,
    // existing-artifact idempotency check (which calls get_pull_request on
    // GitHub), plus create_pull_request itself — each a GitHub call with up
    // to 3 retries and a 120s rate-limit sleep in _github_request. 90s was
    // not enough on rate-limited or slow cycles and every retry hit the same
    // slow path, so tasks failed with AbortError.
    const prRes = await apiPost('/api/agent/github/pull-requests', {
      task_id: taskId, owner, repo, title,
      head: workBranch, base: baseBranch, body,
    }, API_MAX_RETRIES, 240_000) as { data?: { html_url?: string } };
    prUrl = prRes.data?.html_url ?? '';
    sendLog(ws, `PR created: ${prUrl}\n`);
  } catch (e) {
    sendLog(ws, `Failed to create PR: ${String(e)}\n`);
    const err = new Error(`PR creation failed: ${String(e)}`);
    (err as Error & { failureCategory?: FailureCategory }).failureCategory = 'pr_creation_failed';
    throw err;
  }

  return prUrl;
}

// ── Telemetry module ─────────────────────────────────────────────────────────
import {
  errorFingerprint,
  computePromptFingerprint,
  setCurrentPromptFingerprint,
  getCurrentPromptFingerprint,
  recordLlmCallStart,
  recordLlmTokens,
  getLlmTelemetry,
  resetLlmTelemetry,
  estimateTokensFromChars,
} from './telemetry.js';
export {
  errorFingerprint,
  computePromptFingerprint,
  setCurrentPromptFingerprint,
  getCurrentPromptFingerprint,
  recordLlmCallStart,
  recordLlmTokens,
  getLlmTelemetry,
  resetLlmTelemetry,
  estimateTokensFromChars,
};

// ── Divergence detection helpers ─────────────────────────────────────────────
export type { DivergenceReason } from './utils/diff-detection.js';
export type { DiffLines };
export { jaccardSimilarity, detectLoopDivergence };


// ── Test runner detection (shared by testFixLoop and captureTestBaseline) ───
export type { TestRunner };
export { detectTestRunners, parseFailingTests, normalizePytestSelectorForRunner, extractPytestSelectorsFromOutput, buildTargetedPytestRunners };

// ── Test verification loop (extracted to utils/test-fix-loop.ts) ─────────────
// Re-exported here so existing importers (and index.test.ts) keep working;
// handlers now import these directly from './utils/test-fix-loop.js'.
export {
  captureTestBaseline, testFixLoop, condensedAnalysisSections, parseFailingTestsFromVitestJson,
} from './utils/test-fix-loop.js';
export type { TestFixLoopOptions } from './utils/test-fix-loop.js';


// ── Claude Code Runner (shared by all handlers) ─────────────────────────────
async function validateProviderAuth(runner: ProviderRunner, reportPreClaimFailure: (reason: string) => void, model?: string): Promise<void> {
  // Bake-time lifecycle smoke: skip all credential checks so the smoke can
  // exercise the full claim→dispatch→complete chain without real LLM creds.
  if (process.env.BLUEPRINT_SMOKE_SKIP_VALIDATION === '1') return;
  const missing = runner.missingCredentials();
  if (missing.length > 0) {
    const reason = `missing provider credentials: ${missing.join(', ')}`;
    log(reason);
    reportPreClaimFailure(reason);
    process.exit(1);
  }
  const validation = runner.buildValidationCommand(model);
  if (!validation) return;
  try {
    log(`Validating ${runner.displayName} credentials...`);
    const result = await new Promise<{ exitCode: number; stdout: string; stderr: string }>((resolve) => {
      let stdout = '';
      let stderr = '';
      const check = spawn(validation.command, validation.args, { cwd: '/tmp', env: validation.env });
      const valTimeout = setTimeout(() => { try { check.kill(); } catch { /* ignore */ } }, validation.timeoutMs);
      check.stdout.on('data', (d: Buffer) => { stdout += d.toString(); });
      check.stderr.on('data', (d: Buffer) => { stderr += d.toString(); });
      check.on('close', (code) => { clearTimeout(valTimeout); resolve({ exitCode: code ?? 1, stdout, stderr }); });
    });
    const combined = `${result.stderr}
${result.stdout}`;
    if (result.exitCode !== 0 && matchesRateLimit(combined, runner.provider, runner.authMode)) {
      const snippet = combined.slice(0, 200);
      log(`${runner.displayName} validation failed — account rate-limited: ${snippet}`);
      reportPreClaimFailure(`token validation rate-limited: ${snippet}`);
      process.exit(1);
    }
    if (result.exitCode !== 0) {
      log(`${runner.displayName} validation exited non-zero (${result.exitCode}) — continuing`);
      return;
    }
    log(`${runner.displayName} validation OK`);
  } catch (e) {
    log(`${runner.displayName} validation error (continuing): ${e instanceof Error ? e.message : String(e)}`);
  }
}

// ── Main ────────────────────────────────────────────────────────────────────
async function main() {
  // Start forensic instrumentation as early as possible. The lag monitor + exit
  // markers don't depend on auth or task claim, so we want them recording from
  // the moment node starts so a pre-claim death still leaves /tmp logs.
  startEventLoopLagMonitor();
  installExitMarkers();

  const runner = getProviderRunner();
  log(`Blueprint Agent starting (VM: ${VM_INSTANCE_ID})`);
  log(`Registered handlers (${Object.keys(handlers).length}): ${Object.keys(handlers).join(', ')}`);
  log(`Agent source: ${import.meta.url}`);
  if (!AGENT_TOKEN) throw new Error('AGENT_SECRET_TOKEN is required');

  // Cold start — ensure prepareWorkspaceForRole takes the fresh-clone path on
  // role 1. The flag is set later by the role-loop AFTER resetForNextRole
  // succeeds; on first boot the workspace is empty and we always cold-clone.
  delete process.env.BLUEPRINT_WARM_HANDOFF;

  // Per-role state reset on cold-start. Mirrors the role-loop's warm handoff
  // (same single source of truth) so a stale value from a previous process
  // incarnation (shouldn't happen, but defensive) cannot leak into the first
  // role's status payload. warmStart:false — role 1 is a cold start.
  resetPerRoleModuleState({ warmStart: false });

  // TASK_ID is baked into /etc/agent.env by the orchestrator's startup script
  // so we can post pre-claim failure heartbeats. The JWT also binds sub=task_id,
  // so the backend heartbeat endpoint accepts this task_id before claim runs.
  const PRE_CLAIM_TASK_ID = process.env.TASK_ID || '';

  // Report a pre-claim failure via the existing heartbeat endpoint. The backend's
  // /api/agent/heartbeat accepts PROVISIONING-status tasks, so this turns silent
  // startup deaths into explained ones. Uses sendHeartbeatSync so the request
  // flushes before the process exits.
  const reportPreClaimFailure = (reason: string) => {
    if (!PRE_CLAIM_TASK_ID) {
      log(`Cannot report pre-claim failure — TASK_ID env not set. Reason: ${reason}`);
      return;
    }
    const truncated = reason.slice(0, 500);
    log(`Reporting pre-claim failure for task ${PRE_CLAIM_TASK_ID}: ${truncated}`);
    sendHeartbeatSync(PRE_CLAIM_TASK_ID, `PRE_CLAIM_FAILED: ${truncated}`);
  };

  // Early "node reached main" beacon. Lands before any async work, so if the
  // agent silently hangs during validation or claim we can at least tell
  // that node started and the TASK_ID env was readable — see the silent
  // pre-claim death forensics in the reaper. Best-effort, non-blocking.
  if (PRE_CLAIM_TASK_ID) {
    sendHeartbeatSync(PRE_CLAIM_TASK_ID, 'AGENT_BOOTED: node reached main(), validating auth');
  }

  log(`Provider runtime: provider=${PROVIDER} auth_mode=${AUTH_MODE} execution_mode=${LLM_EXECUTION_MODE}`);
  if (LLM_CLI_TRANSLATION) {
    log(`Settings bag: ${LLM_CLI_TRANSLATION.args.length} CLI args, ${LLM_CLI_TRANSLATION.prompt_fragments.length} prompt fragments`);
    if (LLM_CLI_TRANSLATION.unsupported.length > 0) {
      log(`Settings bag unsupported (not honored by CLI): ${LLM_CLI_TRANSLATION.unsupported.join('; ')}`);
    }
  }
  log(runner.credentialSummary());
  // Snapshot the OAuth auth file *before* provider validation runs. Validation
  // invokes the CLI with the provisioned token, which for Codex may trigger a
  // refresh immediately; without a baseline hash we'd miss rotations that
  // happen during validation itself.
  recordInitialOAuthAuthHash();
  await validateProviderAuth(runner, reportPreClaimFailure, process.env.LLM_MODEL);
  if (PRE_CLAIM_TASK_ID) {
    sendHeartbeatSync(PRE_CLAIM_TASK_ID, 'AGENT_AUTH_OK: provider validation complete');
  }

  // Read GCP boot-disk image identity once at startup so vm-heartbeats can
  // report which Packer image this VM is running. Defensive: unreachable
  // in local dev — vmImageName stays null and heartbeats omit it rather
  // than blocking startup.
  { const id = await fetchVmImageIdentity();
    vmImageName = id.image_name;
    vmImageTs = id.image_ts;
    log(`VM image: ${vmImageName ?? '<unknown>'}${vmImageTs ? ` (ts=${vmImageTs})` : ''}`);
  }
  agentRuntimeFingerprint = readAgentRuntimeFingerprint();
  log(`Agent runtime fingerprint: ${agentRuntimeFingerprint ?? '<not available>'}`);

  // Retry any stale result files from previous runs (network failure / VM kill mid-POST)
  try {
    const staleFiles = fs.readdirSync('/tmp').filter(f => /^task_.+_result\.json$/.test(f));
    for (const file of staleFiles) {
      const filePath = `/tmp/${file}`;
      let staleEnvelope: StaleResultEnvelope | null = null;
      try {
        const raw = fs.readFileSync(filePath, 'utf-8');
        staleEnvelope = parseStaleResultFile(raw);
        const stalePayload = staleEnvelope.payload;
        const staleTaskId = strVal(stalePayload.task_id) || file.replace(/^task_/, '').replace(/_result\.json$/, '');
        const now = Date.now();
        if (shouldAbandonStaleResult(staleEnvelope, now)) {
          log(`Abandoning stale result for task ${staleTaskId} after ${staleEnvelope.retry.attempts} failed retries / ${(now - staleEnvelope.retry.createdAt) / 1000}s age`);
          try { fs.unlinkSync(filePath); } catch { /* ignore */ }
          continue;
        }
        if (staleEnvelope.retry.nextAttemptAt > now) {
          const waitSec = Math.ceil((staleEnvelope.retry.nextAttemptAt - now) / 1000);
          log(`Skipping stale result for task ${staleTaskId}; retry backoff has ${waitSec}s remaining`);
          continue;
        }
        log(`Found stale result file for task ${staleTaskId} — retrying POST`);
        await apiPost('/api/agent/status', stalePayload);
        try { fs.unlinkSync(filePath); } catch { /* ignore */ }
        log(`Stale result for task ${staleTaskId} posted successfully`);
      } catch (e) {
        if (staleEnvelope) {
          const updated = markStaleResultFailure(staleEnvelope, e);
          const staleTaskId = strVal(updated.payload.task_id) || file.replace(/^task_/, '').replace(/_result\.json$/, '');
          if (shouldAbandonStaleResult(updated)) {
            log(`WARNING: Stale result for task ${staleTaskId} exhausted retries after ${updated.retry.attempts} attempts; deleting file so /tmp cannot grow unbounded`);
            try { fs.unlinkSync(filePath); } catch { /* ignore */ }
          } else {
            writeStaleResultFile(filePath, updated);
            const waitSec = Math.ceil((updated.retry.nextAttemptAt - Date.now()) / 1000);
            log(`WARNING: Failed to retry stale result from ${file}: ${String(e)} — next retry in ${waitSec}s`);
          }
        } else {
          log(`WARNING: Malformed stale result file ${file}: ${String(e)} — deleting`);
          try { fs.unlinkSync(filePath); } catch { /* ignore */ }
        }
      }
    }
  } catch (e) {
    log(`WARNING: Stale result scan failed: ${String(e)}`);
  }

  // ── Role loop ────────────────────────────────────────────────────────────
  // The first iteration is a cold start — claim via /api/agent/claim and
  // dispatch the handler. Subsequent iterations are warm: pollForNextRole
  // returned a claim, we ran resetForNextRole, and we re-enter the loop
  // body to claim the new task data. The PR_NUMBER threaded through the
  // poll lets the backend scope the lookup so a stray pin doesn't claim
  // the wrong VM.
  let lastTaskId = '';
  let lastPrNumber: number | null = null;
  let lastBaseBranch: string | null = null;

  // Top-level lifecycle catch — runRoleOnce throws on unrecoverable errors
  // (claim failure, OAuth rotation failure, fatal handler exception). The
  // outer catch reports via the existing global crash handlers which fire
  // on the rejected promise from main().
  try {
    for (;;) {
      let claimResult: RunRoleOnceResult;
      try {
        claimResult = await runRoleOnce({ reportPreClaimFailure });
      } catch (roleErr) {
        // GracefulShutdownSignal is a benign "task went terminal / reassigned"
        // teardown request, NOT a fault. runRoleOnce's finally already stopped
        // the heartbeat and closed the ws; break out so the outer finally
        // tears down the workspace and we exit ONCE (no race with a deferred
        // process.exit, no pollForNextRole re-entry, no double-role-start).
        if (roleErr instanceof GracefulShutdownSignal) {
          log(`Role abandoned gracefully (reason=${roleErr.reason}) — winding down`);
          break;
        }
        throw roleErr;
      }
      lastTaskId = claimResult.taskId;
      lastPrNumber = claimResult.prNumber;
      lastBaseBranch = claimResult.baseBranch;

      // After a successful role completion, poll for a warm next role. This
      // single unconditional poll loop serves BOTH warm-VM mechanisms: the
      // PR-chain pin (warm_continuity) and same-project backlog reuse
      // (warm_backlog_reuse, efficiency direction d-91d8dede). The agent is
      // agnostic to which is enabled — the backend decides what /next-role
      // returns (chain pin, backlog claim, exit, or 204). So no agent change
      // is needed for backlog reuse: the VM already stays alive and polls here
      // regardless of whether a chain follow-on is pinned. process.env.
      // VM_INSTANCE_ID is set by the orchestrator startup script and is what
      // the backend keys warm pinning on; local dev / tests where it's absent
      // skip the loop and exit normally.
      const vmInstanceId = process.env.VM_INSTANCE_ID;
      if (!vmInstanceId) break;

      // Clear TASK_ID for the duration of the warm-poll. While poll is
      // running, the prior task is already in EVALUATING server-side, so
      // any SIGTERM hitting the agent process (e.g. orchestrator decided
      // to delete the VM because there's no follow-up work) would
      // otherwise fire _crashReport and overwrite the prior task's
      // latest_status_message with "[FATAL:SIGTERM] VM shutting down" —
      // healthy work that's just sitting in EVALUATING gets mislabeled as
      // a fatal failure and downstream classify_failure tags it
      // AGENT_TIMEOUT. With TASK_ID empty, _crashReport's `if (taskId)`
      // gate skips the heartbeat. The 'claim' branch below restores
      // TASK_ID to the new task before any code that could crash.
      //
      // BUT clearing TASK_ID also stripped crash context from *genuine*
      // crashes (uncaughtException / unhandledRejection) during the poll gap —
      // they reported with no task id and were silently lost. _crashReportTaskId
      // is deliberately NOT cleared here (it still points at the warm-pinned
      // task) and _inWarmPollWindow lets _crashReport label a poll-gap crash
      // distinctly (so a real crash is still reported without overwriting the
      // prior task's healthy EVALUATING status the way a benign SIGTERM would).
      process.env.TASK_ID = '';
      _inWarmPollWindow = true;

      let pollResult: PollResult;
      try {
        pollResult = await pollForNextRole({
          vmInstanceId,
          priorTaskId: lastTaskId,
          priorPrNumber: lastPrNumber,
          apiBase: ORCHESTRATOR_URL,
          agentJwt: AGENT_TOKEN,
        });
      } catch (pollErr) {
        // Defensive — pollForNextRole has its own internal try/catch, but
        // if a programmer error escapes it we don't want to crash the
        // VM. Treat as exit and let the orchestrator cold-provision next.
        log(`pollForNextRole threw: ${String(pollErr)} — exiting`);
        break;
      }
      if (pollResult.action === 'exit') {
        log(`pollForNextRole returned exit (reason=${pollResult.reason ?? 'unspecified'})`);
        break;
      }
      // claim — rotate the agent token, reset, then re-enter the loop.
      log(`Warm-claimed next role: task ${pollResult.taskId ?? ''}`);
      // The backend mints a fresh JWT scoped to the new task as part of
      // the next-role response. Rotate AGENT_TOKEN immediately so the
      // upcoming runRoleOnce → /api/agent/claim idempotent-replay (and
      // every subsequent task-scoped call) authenticates against the new
      // task. Without rotation the prior task's terminal JWT is rejected
      // by verify_agent_token outside the idempotent-path allowlist.
      if (pollResult.agentToken) {
        AGENT_TOKEN = pollResult.agentToken;
        apiHeaders['X-Agent-Token'] = AGENT_TOKEN;
        // Re-schedule proactive refresh based on the new task token's real expiry.
        stopProactiveTokenRefresh();
        scheduleProactiveTokenRefresh(AGENT_TOKEN);
      } else {
        // Backend always mints one for warm claims; missing it indicates
        // a deploy mismatch (old backend, new agent). Treat as fatal so
        // we don't silently keep using the stale prior-task JWT.
        const err = new Error('reset_failed: warm-claim missing agent_token in next-role response') as Error & { failureCategory?: FailureCategory };
        err.failureCategory = 'reset_failed';
        throw err;
      }
      // Pre-set TASK_ID BEFORE resetForNextRole so any crash inside reset
      // (or this block) crash-reports against the new task, not the prior
      // terminal one. Without this, the global crash handler reads
      // process.env.TASK_ID == lastTaskId and the orchestrator attributes
      // the failure to the wrong (already-completed) task.
      if (pollResult.taskId) {
        process.env.TASK_ID = pollResult.taskId;
        setCrashReportTaskId(pollResult.taskId);
      }
      // We've claimed a follow-on; the poll gap is over. A crash from here on
      // attributes to the new task (the normal active-execution path).
      _inWarmPollWindow = false;
      try {
        await resetForNextRole({
          workspace: WORKSPACE,
          priorTaskId: lastTaskId,
          baseBranch: lastBaseBranch ?? undefined,
        });
      } catch (resetErr) {
        const resetMsg = resetErr instanceof Error ? resetErr.message : String(resetErr);
        log(`resetForNextRole failed: ${resetMsg}`);
        // Surface as a typed failure so the outer catch / global crash
        // handler tags this as reset_failed and the orchestrator knows
        // to cold-provision rather than retry warm.
        const err = new Error(`reset_failed: ${resetMsg}`) as Error & { failureCategory?: FailureCategory };
        err.failureCategory = 'reset_failed';
        throw err;
      }
      // Reset succeeded — flip the warm-handoff flag so handlers'
      // prepareWorkspaceForRole reuses the workspace (preserving caches)
      // instead of wiping + re-cloning.
      process.env.BLUEPRINT_WARM_HANDOFF = '1';
      // Reset ALL per-role module-level mutable state in one place so a reused
      // warm VM cannot leak state from the prior task into this one (cancel
      // signal, phase-0 telemetry, tool-trace, prompt fingerprint, ./check
      // synthesizer counters, LLM call/token totals). warmStart:true marks this
      // as a warm role; the next runRoleOnce sets roleClaimTimestamp after a
      // successful re-claim. See resetPerRoleModuleState for the field list.
      resetPerRoleModuleState({ warmStart: true });
    }
  } finally {
    // Workspace teardown happens once at process exit, not per-role —
    // warm continuity DEPENDS on the workspace surviving across roles.
    try { execFileSync('rm', ['-rf', WORKSPACE], { encoding: 'utf-8', timeout: 30_000 }); } catch { /* ignore */ }
    log('Agent shutdown complete.');
  }
}

// ── Per-role lifecycle (extracted so warm roles re-enter cleanly) ────────────
//
// Encapsulates one role end-to-end: claim → setup heartbeat + WS → fetch GH
// creds → dispatch handler → success/failure status post → teardown
// heartbeat + WS + per-role /tmp scratch. `main()` calls this once for the
// initial cold role and again for each warm role discovered via
// pollForNextRole. Returns the claimed task's id + PR number so the role
// loop can thread them into the next /api/agent/next-role call.

interface RunRoleOnceOptions {
  reportPreClaimFailure: (reason: string) => void;
}

interface RunRoleOnceResult {
  taskId: string;
  prNumber: number | null;
  /**
   * The base branch of the just-completed task. Threaded through to the
   * role-loop so resetForNextRole's optional fetch+rebase step (step 5) can
   * actually run. Without this, baseBranch was always undefined and step 5
   * was dead code in production.
   */
  baseBranch: string | null;
}

async function runRoleOnce(opts: RunRoleOnceOptions): Promise<RunRoleOnceResult> {
  const { reportPreClaimFailure } = opts;
  const PRE_CLAIM_TASK_ID = process.env.TASK_ID || '';

  if (PRE_CLAIM_TASK_ID) {
    sendHeartbeatSync(PRE_CLAIM_TASK_ID, 'AGENT_CLAIMING: requesting task payload');
  }
  let task: Record<string, unknown>;
  try {
    task = await apiPost('/api/agent/claim') as Record<string, unknown>;
  } catch (claimErr) {
    const errMsg = claimErr instanceof Error ? `${claimErr.message}${claimErr.stack ? '\n' + claimErr.stack : ''}` : String(claimErr);
    reportPreClaimFailure(`claim failed: ${errMsg}`);
    throw claimErr;
  }
  // Phase 0 telemetry: tClaim = now. firstToolUseAt is filled in by the
  // first recordToolUse() call after this point. setup_secs = the gap.
  roleClaimTimestamp = Date.now();
  const taskId = strVal(task.task_id);
  process.env.TASK_ID = taskId;  // Expose for crash handlers (they can't access local vars)
  setCrashReportTaskId(taskId);  // Decoupled crash identity — survives the poll-window TASK_ID clear
  _inWarmPollWindow = false;     // We're back in active role execution
  const payloads = Array.isArray(task.payloads) ? task.payloads as Record<string, unknown>[] : [];
  const payload = payloads[0] ?? {} as Record<string, unknown>;
  const taskType = strVal(payload.type, 'create');
  const model = payload.model ? strVal(payload.model) : (process.env.LLM_MODEL || undefined);
  const effort = payload.effort ? strVal(payload.effort) : undefined;
  const projectMetadata = task.project_metadata as Record<string, unknown> | undefined;
  const buildConfig = (projectMetadata?.build_config ?? {}) as Record<string, unknown>;
  // Attach buildConfig to the task object so handlers can access it via task.build_config
  task.build_config = buildConfig;
  log(`Claimed task ${taskId} (type: ${taskType}): "${strVal(task.title)}"`);

  // Capture pr_number for the warm-VM next-role poll. Different handlers
  // store it under different keys (review/fix use payload.pr_number,
  // research-plan derives it later); we read whatever is present at claim
  // time and accept null otherwise.
  let prNumber: number | null = null;
  const rawPr = payload.pr_number;
  if (typeof rawPr === 'number' && Number.isFinite(rawPr)) {
    prNumber = rawPr;
  } else if (typeof rawPr === 'string' && rawPr.trim() && Number.isFinite(Number(rawPr))) {
    prNumber = Number(rawPr);
  }

  // Capture the task's base_branch so the role-loop can pass it to
  // resetForNextRole(...) and the optional fetch+rebase step actually runs
  // between warm roles. Routes through resolveBaseBranch so projects whose
  // integration branch is not 'main' do not silently fall through to 'main'.
  const baseBranchForReset = resolveBaseBranch(payload, task).trim();

  // Start heartbeat & log stream
  const statusRef = { value: 'Initializing...' };
  startHeartbeat(taskId, () => statusRef.value);
  scheduleProactiveTokenRefresh(AGENT_TOKEN);
  const ws = await connectLogStream(taskId);
  if (!ws) {
    log('WARNING: WebSocket log stream unavailable — logs will be sent via status updates only');
  }
  sendLog(ws, `Agent started (VM: ${VM_INSTANCE_ID}, type: ${taskType})\n`);

  try {
    // Get GitHub credentials
    statusRef.value = 'Fetching GitHub credentials...';
    const ghRes = await apiGet(`/api/agent/github/token?task_id=${taskId}`) as { data: { token: string; owner: string; repo: string } };
    const { token: ghToken, owner, repo } = ghRes.data;
    log(`GitHub: ${owner}/${repo}`);

    // Dispatch to handler.
    // Audit 2026-04-25 (W4/C4): unknown task types used to silently fall back
    // to the `create` handler, which then operated on payloads of the wrong
    // shape and produced wrong output. Now we throw — the failure surfaces
    // immediately as a real failure, not silent wrong work. This satisfies
    // the "fail fast on actual failure" rule from the audit.
    const handlersMap: Partial<Record<string, Handler>> = handlers;
    const knownHandler = handlersMap[taskType];
    if (!knownHandler) {
      const supported = Object.keys(handlers).sort().join(', ');
      throw new Error(
        `Unknown task type "${taskType}". Backend emitted a task type the agent does not handle. ` +
        `Supported handlers: ${supported}`
      );
    }
    const handler = knownHandler;
    await handler(taskId, payload, task, ghToken, owner, repo, ws, statusRef, model, effort);
    // Phase 0 telemetry: t_handler_complete. Captured only on the success
    // path so failures don't pollute the completion-duration histogram.
    // enrichStatusPayload picks this up on the very next status post and
    // resets between roles in the role-loop's per-role telemetry block.
    // roleClaimTimestamp was set unconditionally above (line ~3197); the
    // Math.max guards against any clock skew producing a negative delta.
    const handlerDurationSecs = (Date.now() - roleClaimTimestamp) / 1000;
    handlerDurationSecsForPayload = Math.max(0, handlerDurationSecs);
    sendLog(ws, '\nTask completed.\n');

  } catch (error) {
    // Benign cancellation during startup: the task went terminal
    // (CANCELLED/COMPLETED/superseded) between /api/agent/claim and the
    // /api/agent/github/token fetch above, so verify_agent_token 401s with
    // "Task is no longer active". This is NOT a code fault — do not record it
    // as failure_category 'fatal' or post a FAILED status (the task is already
    // terminal and the backend would reject the status anyway). Mirror the
    // heartbeat reassignment path (~line 1012): stop the heartbeat, allow a
    // brief wind-down, and exit 0 so the role loop winds down cleanly instead
    // of crash-reporting a fatal failure.
    if (isTaskNoLongerActiveError(error)) {
      log('Task no longer active (cancelled/superseded during startup) — abandoning role gracefully');
      sendLog(ws, '\nTask no longer active (cancelled/superseded during startup) — abandoning role gracefully.\n');
      statusRef.value = 'Task no longer active — abandoning role gracefully';
      // Throw a typed shutdown signal rather than `setTimeout(exit); return`.
      // The old pattern returned successfully back into main()'s role loop,
      // which then proceeded to pollForNextRole — racing the deferred 2s
      // exit and risking a double-role-start (and leaking the in-flight
      // teardown). The throw unwinds through this function's `finally`
      // (which stops the heartbeat + closes the ws) and is caught in main()'s
      // role loop, which breaks cleanly and exits once. requestCancel() stops
      // any heartbeat tick from re-tripping its own self-terminate path.
      requestCancel();
      // Intentional non-Error control-flow throw: GracefulShutdownSignal must
      // NOT be an Error so the fault-classification paths (runRoleOnce catch,
      // global crash handler) never mis-record this benign teardown as a fault.
      // eslint-disable-next-line @typescript-eslint/only-throw-error -- typed graceful-shutdown control signal, caught by main()'s role loop
      throw new GracefulShutdownSignal('task_no_longer_active');
    }
    const errMsg = error instanceof Error ? error.message : String(error);
    const safeErr = sanitize(errMsg);
    log(`Fatal error: ${safeErr}`);
    sendLog(ws, `\nFatal error: ${safeErr}\n`);
    // Classify the failure — rate limit / usage exhaustion should NOT burn retry budget.
    // Detection is carried via RateLimitError (thrown from runClaudeCode). An
    // earlier version re-ran matchesRateLimit on the outer error message, but
    // that message was agent-invented wording ("Claude API usage exhausted —")
    // which doesn't contain any RATE_LIMIT_PATTERNS substring, silently
    // reclassifying rate-limits as CODE_ERROR.
    const isRateLimit = error instanceof RateLimitError;
    const typedError = error instanceof Error
      ? (error as Error & { failureCategory?: FailureCategory })
      : null;
    const errorCategory: FailureCategory | undefined = !isRateLimit ? typedError?.failureCategory : undefined;
    const failureCategory: FailureCategory = isRateLimit ? 'rate_limited' : (errorCategory ?? 'fatal');
    // Surface the error through the heartbeat so it's visible even if the
    // status POST fails — the heartbeat updates latest_status_message on the
    // backend, giving us forensic evidence instead of silent death.
    statusRef.value = `[ERROR] ${safeErr.slice(0, 200)}`;
    // Retry status POST aggressively — if this fails, the backend never learns
    // the result and the reaper eventually times out with no forensic evidence.
    // Keep heartbeat alive during retries so the reaper doesn't kill us mid-report.
    for (let attempt = 0; attempt < 3; attempt++) {
      try {
        await persistAndPostStatus(taskId, {
          task_id: taskId, status: 'FAILED', artifact_type: 'ERROR_LOG',
          logs: sanitize(`Fatal error: ${errMsg}`),
          failure_category: failureCategory,
        });
        break; // Success — stop retrying
      } catch (e) {
        const postErr = e instanceof Error ? e.message : String(e);
        log(`Failed to report error (attempt ${attempt + 1}/3): ${postErr}`);
        statusRef.value = `[ERROR] status POST failed: ${sanitize(postErr).slice(0, 150)}`;
        if (attempt < 2) await new Promise(r => setTimeout(r, 10_000 * (attempt + 1)));
      }
    }
    // Re-throw so the role loop terminates — failures don't get warm
    // continuity; the orchestrator cold-provisions for the next role.
    throw error;
  } finally {
    stopHeartbeat();
    if (ws) ws.close();
    // Per-role /tmp scratch cleanup. Workspace teardown happens at process
    // exit (in main()'s outer finally) so warm continuity preserves it.
    try { fs.unlinkSync(`/tmp/agent_prompt_${taskId}.txt`); } catch { /* ignore */ }
    for (const name of ['review_verdict', 'plan_output', 'research_findings', 'security_findings', 'smoketest_result', 'debug_output', 'perf_output', 'investigation_report', 'strategic_output', 'design_doc_output']) {
      try { fs.unlinkSync(`/tmp/${name}_${taskId}.json`); } catch { /* ignore */ }
    }
  }

  return { taskId, prNumber, baseBranch: baseBranchForReset };
}

// Global crash handlers — report errors via heartbeat before dying.
// process.exit() skips the finally block, so the crash handler is the
// ONLY chance to surface forensic evidence to the backend.  Without this,
// every failure mode (uncaughtException, SIGTERM, cgroup OOM SIGKILL)
// looks identical: heartbeats stop, task stays IN_PROGRESS, no evidence.
function _crashReport(label: string, detail: string, exitCode: number) {
  console.error(`[FATAL] ${label}: ${detail}`);
  // Local file marker before any network attempt — survives even if the
  // sendHeartbeatSync curl times out or the network is partitioned. Combined
  // with serial-console capture (plan task 25075953), we get the agent's
  // last words even when the heartbeat never lands.
  recordFinalState(`crash:${label}`, detail);
  // Synchronous curl — fire-and-forget fetch() couldn't guarantee the write
  // flushed before process.exit, so crashes would show up as silent heartbeat
  // timeouts instead of explained failures. 3s timeout in the signal-handler
  // path: GCE gives ~30s before SIGKILL, and a 10s blocking curl chews 1/3 of
  // that window for one best-effort flush. 3s fails fast on partition while
  // still succeeding on healthy networks.
  // Prefer the env TASK_ID (active role); fall back to the decoupled
  // _crashReportTaskId so a crash during the warm-poll window — where the role
  // loop deliberately blanks process.env.TASK_ID — still carries task context
  // instead of being silently dropped (cluster C7). A benign SIGTERM during
  // the poll gap is intentionally NOT reported against a task (it's just the VM
  // being torn down with no follow-up work), so it keeps the empty-env path;
  // only genuine crashes (uncaughtException / unhandledRejection) fall back to
  // _crashReportTaskId, and they're labelled distinctly so the backend can tell
  // a real poll-gap crash from the prior task's completed-and-EVALUATING work.
  const envTaskId = process.env.TASK_ID;
  const pollGapCrash = !envTaskId && _inWarmPollWindow && label !== 'SIGTERM' && !!_crashReportTaskId;
  const taskId = envTaskId || (pollGapCrash ? _crashReportTaskId : '');
  const reportLabel = pollGapCrash ? `${label}:during-poll` : label;
  if (taskId) {
    try {
      const metrics = collectMetrics();
      const msg = `[FATAL:${reportLabel}] ${detail.slice(0, 300)}`;
      sendHeartbeatSync(taskId, `[${metrics.heap_used_mb}MB cg:${metrics.cgroup_mb || '?'}MB] ${msg}`, undefined, 3_000);
    } catch { /* best effort — we're dying anyway */ }
  }
  // process.exit normally never returns; if it throws (e.g. when intercepted
  // by a test harness like vitest), swallow the throw so the caller — often
  // an uncaughtException handler — doesn't re-throw and crash the process
  // with a confusing secondary error instead of the original one.
  try { process.exit(exitCode); } catch { /* intercepted, best effort */ }
}

// Only register global handlers and call main() when this file is the entry
// point. When imported by tests (vitest), process.argv[1] points to vitest's
// worker, not this file, so these side-effects are skipped and the worker
// doesn't crash from the still-registered handlers calling process.exit.
if (process.argv[1] === fileURLToPath(import.meta.url)) {
  process.on('unhandledRejection', (reason) => {
    _crashReport('unhandledRejection', String(reason), 1);
  });
  process.on('uncaughtException', (err) => {
    _crashReport('uncaughtException', err instanceof Error ? `${err.message}\n${err.stack}` : String(err), 1);
  });
  process.on('SIGTERM', () => {
    _crashReport('SIGTERM', 'VM shutting down', 143);
  });

  main().catch((err: unknown) => {
    // Route through _crashReport so rejections before startHeartbeat() reaches
    // its first tick still produce a FATAL heartbeat the orchestrator can see.
    // Without this, the catch handler swallows the rejection and no global
    // handler fires — the task stays IN_PROGRESS until the reaper gives up.
    try {
      const detail = err instanceof Error ? `${err.message}\n${err.stack ?? ''}` : String(err);
      _crashReport('main_failed', detail, 1);
    } catch (reportErr) {
      console.error('Fatal error (crash report also failed):', err, reportErr);
      process.exit(1);
    }
  });
}
