import { spawn, execFileSync, execSync } from 'child_process';
import { createHash } from 'crypto';
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
import {
  formatFixIterationHistory,
  type FixAttempt,
  READ_THE_FAILURE_MESSAGE,
  SIZE_GUARDRAIL_GUIDANCE,
  PRIOR_ATTEMPT_DIVERGENCE,
} from './prompt.js';
import {
  type FixLoopEvent,
  type FixLoopExitReason,
  type FixLoopName,
  pushFixLoopEvent,
  drainFixLoopEventBuffer,
  requeueFixLoopEvents,
  resetCheckSynthesizerState,
  synthesizeCheckFixLoopEvents,
  fingerprintHash,
  isCheckInvocation,
  recordCheckOutcome,
  getCheckRunawayStatus,
} from './fix-loop-events.js';
import {
  API_TIMEOUT, API_MAX_RETRIES, LOGS_MAX_LENGTH, API_RETRY_BACKOFF_MAX_MS,
  STALE_RESULT_RETRY_BASE_MS, STALE_RESULT_RETRY_MAX_MS,
  STALE_RESULT_MAX_ATTEMPTS, STALE_RESULT_MAX_AGE_MS, STDERR_TAIL_BYTES,
  PROCESS_KILL_GRACE_MS, CLAUDE_RESULT_GRACE_MS, CLAUDE_STREAM_STALL_MS, LOG_TAIL_BYTES,
  HEARTBEAT_INTERVAL, MAX_LOG_BUFFER,
  HEARTBEAT_API_TIMEOUT, HEARTBEAT_API_MAX_RETRIES,
  WITHIN_CHUNK_YIELD_LINES, WITHIN_CHUNK_YIELD_MS,
  GIT_CLONE_TIMEOUT, CLONE_MAX_RETRIES, CLONE_RETRY_BASE_DELAY,
  CONFLICT_CHECK_TIMEOUT,
  MAX_LINT_FIX_ATTEMPTS, MAX_TEST_FIX_ATTEMPTS,
  FRONTEND_NPM_TIMEOUT, FRONTEND_ESLINT_TIMEOUT, FRONTEND_BUILD_TIMEOUT, FRONTEND_PLAYWRIGHT_INSTALL_TIMEOUT,
  AGENT_NPM_TIMEOUT, AGENT_TSC_TIMEOUT,
  RUFF_TIMEOUT,
  TEST_RUNNER_TIMEOUT, TEST_RUNNER_DOCKER_TIMEOUT, TEST_BASELINE_TIMEOUT, TEST_BASELINE_DOCKER_TIMEOUT,
  claudeTimeoutForEffort, MAX_OUTPUT_BYTES, TOOL_USE_STALL_TIMEOUT_MS,
  FIX_LOOP_JACCARD_THRESHOLD, FIX_LOOP_DIFF_REVERT_THRESHOLD,
  MAX_CLAUDE_CHECK_INVOCATIONS, CHECK_RUNAWAY_MIN_INVOCATIONS, CHECK_RUNAWAY_CONSECUTIVE_FAILURES,
} from './constants.js';

// ── Failure categories ───────────────────────────────────────────────────────
export type FailureCategory = 'clone_failed' | 'build_failed' | 'test_failed' | 'lint_failed' | 'pr_creation_failed' | 'claude_code_failed' | 'parse_failed' | 'fatal' | 'rate_limited' | 'timeout' | 'oauth_rotation_failed' | 'reset_failed';
type ProviderName = 'anthropic' | 'openai' | 'gemini';
type AuthMode = 'oauth' | 'api_key' | 'service_account';
type OutputMode = 'claude-stream-json' | 'codex-jsonl' | 'text';

const RATE_LIMIT_PATTERNS: Readonly<Record<ProviderName, Partial<Record<AuthMode, readonly string[]>>>> = {
  anthropic: {
    oauth: [
      "you've hit your limit",
      "you've hit your usage limit",
      "you've hit your session limit",
      'claude ai usage limit reached',
      "you're out of extra usage",
      '5-hour limit reached',
      'api error: rate limit reached',
      "this request would exceed your account's rate limit",
    ],
    api_key: [
      "you've hit your limit",
      "you've hit your usage limit",
      "you've hit your session limit",
      'claude ai usage limit reached',
      "you're out of extra usage",
      '5-hour limit reached',
      'api error: rate limit reached',
      "this request would exceed your account's rate limit",
    ],
  },
  openai: {
    oauth: [
      "you've hit your usage limit",
      "you've hit rate limit",
      'exceeded retry limit',
      'last status: 429',
      'insufficient_quota',
      'usage_not_included',
      'rate_limit_exceeded',
    ],
    api_key: [
      'quota exceeded. check your plan and billing',
      'rate limit reached for',
      'please try again in',
    ],
  },
  gemini: {
    api_key: [
      'rate limit exceeded',
      "you've hit the api rate limit",
      'resource has been exhausted',
      'resource_exhausted',
      'please wait and try again later',
      'you have reached your daily',
      'automatically switching from gemini-',
      'quota exceeded for quota metric',
      'quota exceeded for metric',
      "you've exceeded the rate limit",
      'you exceeded your current quota, please check your plan and billing details',
      'please retry in',
      'to increase your limits, request a quota increase',
      'switching to the gemini-2.5-flash model',
    ],
    service_account: [
      'resource has been exhausted',
      'resource_exhausted',
      'ratelimitexceeded',
      'quota exceeded for metric',
      'resource exhausted. please try again later',
      'please refer to https://cloud.google.com/vertex-ai/generative-ai/docs/error-code-429',
    ],
  },
} as const;

function normalizeProvider(raw: string | undefined): ProviderName {
  if (raw === 'openai' || raw === 'gemini') return raw;
  return 'anthropic';
}

function normalizeAuthMode(raw: string | undefined, provider: ProviderName): AuthMode {
  if (provider === 'gemini' && raw === 'service_account') return raw;
  if (raw === 'api_key' || raw === 'oauth') return raw;
  if (provider === 'gemini') return 'api_key';
  return 'oauth';
}

const PROVIDER: ProviderName = normalizeProvider(process.env.PROVIDER);
const AUTH_MODE: AuthMode = normalizeAuthMode(process.env.AUTH_MODE, PROVIDER);
const LLM_EXECUTION_MODE = process.env.LLM_EXECUTION_MODE || 'default';

// Provider settings bag + pre-translated CLI shape emitted by the backend
// (see build_agent_secret_manifest in backend/app/compute.py). The settings
// bag is provider-specific and validated server-side; the CLI translation
// already maps it into CLI env/args/prompt fragments so this runtime applies
// them verbatim.
export type LLMCliTranslation = {
  env: Record<string, string>;
  args: string[];
  prompt_fragments: string[];
  unsupported: string[];
};

export function parseLLMCliTranslation(): LLMCliTranslation | null {
  const raw = process.env.LLM_CLI_TRANSLATION;
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as Partial<LLMCliTranslation>;
    return {
      env: parsed.env ?? {},
      args: Array.isArray(parsed.args) ? parsed.args.map(String) : [],
      prompt_fragments: Array.isArray(parsed.prompt_fragments) ? parsed.prompt_fragments.map(String) : [],
      unsupported: Array.isArray(parsed.unsupported) ? parsed.unsupported.map(String) : [],
    };
  } catch (e) {
    // Log but don't fail — runner falls back to legacy model/effort env vars.
    console.warn(`[LLM_SETTINGS] Failed to parse LLM_CLI_TRANSLATION: ${e instanceof Error ? e.message : String(e)}`);
    return null;
  }
}

const LLM_CLI_TRANSLATION: LLMCliTranslation | null = parseLLMCliTranslation();

function getRateLimitPatterns(provider: ProviderName, authMode: AuthMode): readonly string[] {
  return RATE_LIMIT_PATTERNS[provider][authMode] ?? RATE_LIMIT_PATTERNS[provider].oauth ?? [];
}

// Structured, provider-agnostic 429 detection: matches JSON-shaped key/value
// pairs emitted in the streamed result event so future banner-wording changes
// cannot silently regress rate-limit classification.
const STRUCTURED_429_PATTERNS = ['"api_error_status":429', '"error":"rate_limit"'] as const;

export function matchesRateLimit(text: string, provider: ProviderName = PROVIDER, authMode: AuthMode = AUTH_MODE): boolean {
  const lower = text.toLowerCase();
  if (getRateLimitPatterns(provider, authMode).some(p => lower.includes(p))) return true;
  return STRUCTURED_429_PATTERNS.some(p => text.includes(p));
}

// Thrown when Claude CLI output matches a rate-limit pattern. Carries the
// classification via the type system so the outer catch doesn't have to
// re-match an invented error message (which dropped the signal when
// stderrBuffer was empty and the banner came through stdout).
export class RateLimitError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'RateLimitError';
  }
}

// ── OAuth credential rotation (agent → backend) ──────────────────────────────
//
// Some CLIs (currently Codex) refresh their OAuth tokens in-place during a
// run by rewriting `auth.json`. The refreshed token lives only on the
// ephemeral VM unless we propagate it back to the backend — on the next
// task, the backend would re-push the pre-refresh token and the upstream
// provider may reject it (invalid_grant) if it rotated the refresh token
// on the last use.
//
// hashFileOrNull + the module-level `initialOAuthAuthHash` let us detect
// the in-run mutation cheaply and skip the POST when nothing changed.

const OAUTH_AUTH_PATH_BY_PROVIDER: Partial<Record<ProviderName, string>> = {
  openai: '/opt/agent/openai/auth.json',
};

function hashFileOrNull(path: string): string | null {
  try {
    const raw = fs.readFileSync(path);
    return createHash('sha256').update(raw).digest('hex');
  } catch {
    return null;
  }
}

let initialOAuthAuthHash: string | null = null;

export function recordInitialOAuthAuthHash(): void {
  const path = OAUTH_AUTH_PATH_BY_PROVIDER[PROVIDER];
  if (!path || AUTH_MODE !== 'oauth') {
    initialOAuthAuthHash = null;
    return;
  }
  initialOAuthAuthHash = hashFileOrNull(path);
}

export async function rotateOAuthCredentialsIfChanged(taskId: string): Promise<void> {
  const path = OAUTH_AUTH_PATH_BY_PROVIDER[PROVIDER];
  if (!path || AUTH_MODE !== 'oauth') return;
  if (!fs.existsSync(path)) return;
  const currentHash = hashFileOrNull(path);
  if (currentHash === null) return;
  if (initialOAuthAuthHash !== null && currentHash === initialOAuthAuthHash) {
    // Unchanged — nothing to rotate.
    return;
  }
  let contents: string;
  try {
    contents = fs.readFileSync(path, 'utf-8');
  } catch (e) {
    throw new Error(`read ${path} failed: ${e instanceof Error ? e.message : String(e)}`);
  }
  await apiPost('/api/agent/credentials/rotate', { secret: contents, task_id: taskId });
  // Update the initial hash so a follow-up error path doesn't re-POST the
  // same blob twice. Defensive; the backend is idempotent regardless.
  initialOAuthAuthHash = currentHash;
}

// Thrown when the 45-minute kill timer fires. Carries a distinct failure
// category so the backend can map it to AGENT_TIMEOUT (scope-downgrade path)
// instead of CODE_ERROR (fix-tests/lint path).
export class ClaudeTimeoutError extends Error {
  failureCategory: FailureCategory = 'timeout';
  constructor(message: string) {
    super(message);
    this.name = 'ClaudeTimeoutError';
  }
}

// ── Build configuration (from project settings, passed via claim response) ───
export interface DirectoryConfig {
  path: string;
  type: string; // 'python' | 'node' | 'go' | 'rust'
  install_cmd?: string;
  lint_cmd?: string;
  build_cmd?: string;
  test_cmd?: string;
}
export interface BuildConfig {
  language?: string;
  directories?: DirectoryConfig[];
  env_vars?: Record<string, string>;
  git_user_name?: string;
  git_user_email?: string;
  enable_fresh_reviewer?: boolean;
}

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

// ── Helpers ──────────────────────────────────────────────────────────────────
export function strVal(v: unknown, fallback = ''): string {
  return typeof v === 'string' ? v : fallback;
}


function appendBoundedTail(current: string, chunk: string, maxBytes: number): string {
  const combined = current + chunk;
  if (Buffer.byteLength(combined, 'utf-8') <= maxBytes) return combined;
  return Buffer.from(combined, 'utf-8').subarray(-maxBytes).toString('utf-8');
}

function writePrivateFileSync(path: string, contents: string): void {
  fs.writeFileSync(path, contents, { encoding: 'utf-8', mode: 0o600 });
  try { fs.chmodSync(path, 0o600); } catch { /* best effort on non-POSIX filesystems */ }
}

// ── API Helpers ─────────────────────────────────────────────────────────────
async function tryRefreshToken(): Promise<boolean> {
  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      const controller = new AbortController();
      const timeout = setTimeout(() => { controller.abort(); }, API_TIMEOUT);
      const res = await fetch(`${ORCHESTRATOR_URL}/api/agent/refresh-token`, {
        method: 'POST', headers: { ...apiHeaders }, body: JSON.stringify({}), signal: controller.signal,
      });
      clearTimeout(timeout);
      if (res.ok) {
        const data = await res.json() as { token?: string };
        if (data.token) {
          AGENT_TOKEN = data.token;
          apiHeaders['X-Agent-Token'] = AGENT_TOKEN;
          log('Agent token refreshed successfully');
          return true;
        }
      }
      // Don't retry client errors (4xx) — only transient/network failures
      if (res.status >= 400 && res.status < 500) return false;
    } catch (e) {
      log(`Token refresh attempt ${attempt + 1}/3 failed: ${String(e)}`);
    }
    if (attempt < 2) await new Promise(r => setTimeout(r, 1000 * (attempt + 1))); // 1s, 2s backoff
  }
  return false;
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

/** Persist result payload to disk before POSTing status, then clean up on success.
 *  If the POST fails (network error, VM kill), the file survives for retry on next startup. */
export type StaleResultEnvelope = {
  payload: Record<string, unknown>;
  retry: {
    attempts: number;
    createdAt: number;
    nextAttemptAt: number;
    lastAttemptAt?: number;
    lastError?: string;
  };
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

export function createStaleResultEnvelope(
  payload: Record<string, unknown>,
  now = Date.now(),
): StaleResultEnvelope {
  return {
    payload,
    retry: {
      attempts: 0,
      createdAt: now,
      nextAttemptAt: 0,
    },
  };
}

export function parseStaleResultFile(raw: string, now = Date.now()): StaleResultEnvelope {
  const parsed = JSON.parse(raw) as unknown;
  if (!isRecord(parsed)) throw new Error('stale result file is not a JSON object');
  const payload = parsed.payload;
  const retry = parsed.retry;
  if (isRecord(payload) && isRecord(retry)) {
    return {
      payload,
      retry: {
        attempts: typeof retry.attempts === 'number' ? retry.attempts : 0,
        createdAt: typeof retry.createdAt === 'number' ? retry.createdAt : now,
        nextAttemptAt: typeof retry.nextAttemptAt === 'number' ? retry.nextAttemptAt : 0,
        lastAttemptAt: typeof retry.lastAttemptAt === 'number' ? retry.lastAttemptAt : undefined,
        lastError: typeof retry.lastError === 'string' ? retry.lastError : undefined,
      },
    };
  }
  return createStaleResultEnvelope(parsed, now);
}

export function staleResultBackoffMs(attempts: number): number {
  const exponent = Math.max(0, Math.min(attempts - 1, 10));
  return Math.min(STALE_RESULT_RETRY_BASE_MS * Math.pow(2, exponent), STALE_RESULT_RETRY_MAX_MS);
}

export function shouldAbandonStaleResult(envelope: StaleResultEnvelope, now = Date.now()): boolean {
  return envelope.retry.attempts >= STALE_RESULT_MAX_ATTEMPTS
    || now - envelope.retry.createdAt > STALE_RESULT_MAX_AGE_MS;
}

export function markStaleResultFailure(
  envelope: StaleResultEnvelope,
  error: unknown,
  now = Date.now(),
): StaleResultEnvelope {
  const attempts = envelope.retry.attempts + 1;
  const nextAttemptAt = now + staleResultBackoffMs(attempts);
  const lastError = error instanceof Error ? error.message : String(error);
  return {
    payload: envelope.payload,
    retry: {
      ...envelope.retry,
      attempts,
      lastAttemptAt: now,
      nextAttemptAt,
      lastError: sanitize(lastError).slice(0, 500),
    },
  };
}

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
          body: JSON.stringify({ vm_instance_id: vmInstanceId, vm_image_name: vmImageName, vm_image_build_ts: vmImageTs }),
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
        body: JSON.stringify({ vm_instance_id: vmInstanceId, vm_image_name: vmImageName, vm_image_build_ts: vmImageTs }),
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
  currentPromptFingerprint = null;
}

/** Test-only: set the warm-start flag (mirrors what the role loop does). */
export function _setWarmStartForTesting(value: boolean): void {
  isWarmStart = value;
}

/** Test-only: read the warm-start flag. */
export function _getWarmStartForTesting(): boolean {
  return isWarmStart;
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
        ...(currentPromptFingerprint ? { prompt_fingerprint: currentPromptFingerprint } : {}),
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
        ...(currentPromptFingerprint ? { prompt_fingerprint: currentPromptFingerprint } : {}),
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
              ...(currentPromptFingerprint ? { prompt_fingerprint: currentPromptFingerprint } : {}),
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
      // Other 4xx = task reassigned to new VM (404) or other client error — graceful shutdown
      if (e instanceof Error && /failed \(4\d{2}\)/.test(e.message)) {
        log('Task no longer assigned to this VM — self-terminating gracefully');
        stopHeartbeat();
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
  // Reset mutex so warm-VM continuity (one agent process, multiple sequential
  // tasks) starts the next task with a clean in-flight state. Without this,
  // a stuck heartbeat at role-end would leave the flag true and the new
  // task's first periodic tick would skip-and-log instead of firing.
  heartbeatInFlight = false;
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

export function sanitize(text: string): string {
  return text
    .replace(/https:\/\/x-access-token:[^@]+@/g, 'https://x-access-token:***@')
    .replace(/ghp_[A-Za-z0-9_]+/g, 'ghp_***')
    .replace(/ghs_[A-Za-z0-9_]+/g, 'ghs_***')
    .replace(/github_pat_[A-Za-z0-9_]+/g, 'github_pat_***')
    .replace(/sk-ant-[A-Za-z0-9_-]+/g, 'sk-ant-***')
    .replace(/eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+/g, 'eyJ***')
    .replace(/Bearer\s+[^\s,'"]+/gi, 'Bearer ***')
    .replace(/oauth_token[=:]\s*[^\s&'"]+/gi, 'oauth_token=***')
    .replace(/-----BEGIN[A-Z\s]+PRIVATE\s+KEY-----[\s\S]*?-----END[A-Z\s]+PRIVATE\s+KEY-----/g, '***PRIVATE KEY***');
}

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
  const workBranch = `blueprint/${branchPrefix}-${shortId}`;
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

function installPlaywrightChromium(cwd: string) {
  const playwrightCacheDir = `${process.env.HOME}/.cache/ms-playwright`;
  const chromiumPresent = fs.existsSync(playwrightCacheDir) &&
    fs.readdirSync(playwrightCacheDir).some(d => d.startsWith('chromium-'));

  if (chromiumPresent) {
    return;
  }

  try {
    execFileSync('npx', ['playwright', 'install', 'chromium'], {
      cwd,
      encoding: 'utf-8',
      timeout: FRONTEND_PLAYWRIGHT_INSTALL_TIMEOUT,
    });
  } catch {
    execFileSync('npx', ['playwright', 'install', '--with-deps', 'chromium'], {
      cwd,
      encoding: 'utf-8',
      timeout: FRONTEND_PLAYWRIGHT_INSTALL_TIMEOUT,
    });
  }
}

// ── Viewport Bootstrap Fallback ─────────────────────────────────────────────

// Missing-library / missing-install signatures. Deliberately excludes
// 'browserType.launch:' and 'failed to launch browser' which are too broad
// and would match sandbox crashes and OOMs.
const VIEWPORT_BOOTSTRAP_PATTERNS = [
  /libatk/,
  /libnss/,
  /libgbm/,
  /libxkbcommon/,
  /libxcomposite/,
  /libasound/,
  /cannot open shared object file/,
  /host system is missing dependencies/,
  /executable doesn't exist at/,
];

export function isViewportBootstrapFailure(msg: string): boolean {
  return VIEWPORT_BOOTSTRAP_PATTERNS.some(re => re.test(msg));
}

/** Find screenshot files captured on viewport test failures. Returns relative paths within the cwd. */
function findViewportScreenshots(cwd: string): string[] {
  const screenshotDir = `${cwd}/.vitest`;
  if (!fs.existsSync(screenshotDir)) return [];
  const screenshots: string[] = [];
  const walkDir = (dir: string, baseDir: string) => {
    try {
      const files = fs.readdirSync(dir);
      for (const file of files) {
        const fullPath = `${dir}/${file}`;
        if (fs.statSync(fullPath).isDirectory()) {
          walkDir(fullPath, baseDir);
        } else if (file.endsWith('.png')) {
          screenshots.push(fullPath.slice(baseDir.length + 1));
        }
      }
    } catch {
      // Ignore errors reading directories
    }
  };
  walkDir(screenshotDir, cwd);
  return screenshots;
}

/** Run `npm run test:viewport` locally; on a bootstrap failure retry inside the
 *  Playwright Docker image where all native deps are baked in. Real assertion
 *  failures (non-bootstrap errors) propagate immediately without Docker retry. */
export function runViewportSuiteWithFallback(
  cwd: string,
  env: NodeJS.ProcessEnv,
  timeout: number,
  ws: WebSocket | null = null,
): void {
  try {
    execFileSync('npm', ['run', 'test:viewport'], { cwd, encoding: 'utf-8', timeout, env });
    return;
  } catch (localErr: unknown) {
    const msg = localErr instanceof Error ? localErr.message : String(localErr);
    if (!isViewportBootstrapFailure(msg)) {
      throw localErr;
    }
  }
  // Bootstrap failure — retry inside the Playwright image where deps are baked in
  const envFlags = Object.entries(env)
    .filter(([k]) => k.startsWith('VITE_'))
    .flatMap(([k, v]) => ['--env', `${k}=${v ?? ''}`]);
  try {
    execFileSync(
      'docker',
      ['run', '--rm', '-v', `${WORKSPACE}:/workspace`, '-w', '/workspace/admin-ui', ...envFlags,
        'mcr.microsoft.com/playwright:v1.59.1-jammy', 'npm', 'run', 'test:viewport'],
      { cwd, encoding: 'utf-8', timeout },
    );
  } catch (dockerErr: unknown) {
    const dockerMsg = dockerErr instanceof Error ? dockerErr.message : String(dockerErr);
    // Check if docker itself is unavailable vs test assertions failed in docker
    if (dockerMsg.includes('command not found') || dockerMsg.includes('ENOENT') ||
        dockerMsg.includes('Cannot connect to Docker daemon')) {
      sendLog(ws, '[WARNING] viewport suite skipped — Chromium unavailable, CI will enforce coverage\n');
      return;
    }
    // Check if docker had file-import errors (pre-existing env issues) vs test assertion failures
    if (!dockerMsg.includes('Tests') || !dockerMsg.includes('failed')) {
      sendLog(ws, '[WARNING] viewport suite has import errors in Docker (pre-existing env issue); CI will enforce full coverage\n');
      return;
    }
    // Test assertion failures must propagate
    throw dockerErr;
  }
}

// ── Lint / Build Checks ─────────────────────────────────────────────────────
/** Run lint/build checks on changed files. Throws with error details on failure.
 *  Each blocking execFileSync starves the event loop (and heartbeat setInterval),
 *  so we inject sendHeartbeatSync between operations to prevent the reaper from
 *  killing us mid-check.  The taskId is read from process.env (set after claim). */
export function runProjectLintChecks(changedPaths: string[], ws: WebSocket | null, buildConfig?: BuildConfig) {
  const taskId = process.env.TASK_ID || '';

  // If project provides explicit directory configuration, use it instead of hardcoded paths
  if (buildConfig?.directories && buildConfig.directories.length > 0) {
    const extraEnv = buildConfig.env_vars ? { ...process.env, ...buildConfig.env_vars } : process.env;
    for (const dir of buildConfig.directories) {
      const dirPrefix = dir.path.replace(/\/$/, '');
      const touchesDir = changedPaths.some(p => p === dirPrefix || p.startsWith(dirPrefix + '/'));
      if (!touchesDir) continue;

      const cwd = `${WORKSPACE}/${dirPrefix}`;
      if (dir.install_cmd) {
        sendLog(ws, `Running install in ${dirPrefix}...\n`);
        sendHeartbeatSync(taskId, `${dir.install_cmd} (${dirPrefix})...`);
        const [cmd, ...args] = dir.install_cmd.split(' ');
        execFileSync(cmd, args, { cwd, encoding: 'utf-8', timeout: FRONTEND_NPM_TIMEOUT, env: extraEnv });
      }
      if (dir.lint_cmd) {
        sendLog(ws, `Running lint in ${dirPrefix}...\n`);
        sendHeartbeatSync(taskId, `${dir.lint_cmd} (${dirPrefix})...`);
        const [cmd, ...args] = dir.lint_cmd.split(' ');
        execFileSync(cmd, args, { cwd, encoding: 'utf-8', timeout: FRONTEND_ESLINT_TIMEOUT, env: extraEnv });
      }
      if (dir.build_cmd) {
        sendLog(ws, `Running build in ${dirPrefix}...\n`);
        sendHeartbeatSync(taskId, `${dir.build_cmd} (${dirPrefix})...`);
        const [cmd, ...args] = dir.build_cmd.split(' ');
        execFileSync(cmd, args, { cwd, encoding: 'utf-8', timeout: FRONTEND_BUILD_TIMEOUT, env: extraEnv });
      }
      sendLog(ws, `Checks passed for ${dirPrefix}.\n`);
    }
    return;
  }

  // Generic fallback: detect language/tooling from changed file extensions,
  // mirroring detectTestRunners which detects generically before falling back
  // to Blueprint-specific subdirectory names.

  // Admin-ui: Blueprint-specific frontend with Playwright viewport + custom env vars
  const touchesFrontend = changedPaths.some(p => p.startsWith('admin-ui/'));

  // TypeScript directories: any top-level dir with .ts/.tsx changes (admin-ui handled separately)
  const tsDirs = new Set<string>();
  for (const p of changedPaths) {
    if (p.endsWith('.ts') || p.endsWith('.tsx')) {
      const dir = p.split('/')[0];
      if (dir && !dir.includes('.')) tsDirs.add(dir);
    }
  }
  tsDirs.delete('admin-ui');

  // Python files per directory: scope ruff to changed .py files that still exist on disk
  // (deletions are skipped; repo-wide ruff is enforced in CI on main)
  const pyFilesByDir = new Map<string, string[]>();
  for (const p of changedPaths) {
    if (!p.endsWith('.py')) continue;
    const dir = p.split('/')[0];
    if (!dir || dir.includes('.')) continue;
    if (!fs.existsSync(`${WORKSPACE}/${p}`)) continue;
    const list = pyFilesByDir.get(dir) ?? [];
    list.push(p);
    pyFilesByDir.set(dir, list);
  }

  if (touchesFrontend) {
    const adminUiEnv = {
      ...process.env,
      VITE_API_URL: 'http://localhost:8080',
      VITE_FIREBASE_API_KEY: 'ci',
      VITE_FIREBASE_AUTH_DOMAIN: 'ci.firebaseapp.com',
      VITE_FIREBASE_PROJECT_ID: 'ci',
    };
    const viewportFailureGuidance = '\nadmin-ui viewport regression suite requires Playwright Chromium. If this VM is missing it, run `cd admin-ui && npx playwright install --with-deps chromium` and rerun the check.';
    sendLog(ws, 'Running frontend lint + viewport + build...\n');
    sendHeartbeatSync(taskId, 'npm ci (admin-ui)...');
    execFileSync('npm', ['ci'], { cwd: `${WORKSPACE}/admin-ui`, encoding: 'utf-8', timeout: FRONTEND_NPM_TIMEOUT });
    sendHeartbeatSync(taskId, 'eslint (admin-ui)...');
    execFileSync('npx', ['eslint', '.'], { cwd: `${WORKSPACE}/admin-ui`, encoding: 'utf-8', timeout: FRONTEND_ESLINT_TIMEOUT });
    try {
      sendHeartbeatSync(taskId, 'playwright chromium install (admin-ui)...');
      installPlaywrightChromium(`${WORKSPACE}/admin-ui`);
      sendHeartbeatSync(taskId, 'npm run test:viewport (admin-ui)...');
      runViewportSuiteWithFallback(`${WORKSPACE}/admin-ui`, adminUiEnv, FRONTEND_BUILD_TIMEOUT, ws);
    } catch (err: unknown) {
      const screenshotPaths = findViewportScreenshots(`${WORKSPACE}/admin-ui`);
      const screenshotGuidance = screenshotPaths.length > 0
        ? `\nViewport test screenshots:\n${screenshotPaths.map(p => `  admin-ui/${p}`).join('\n')}`
        : '';
      if (err instanceof Error) {
        err.message += viewportFailureGuidance + screenshotGuidance;
        throw err;
      }
      throw new Error(`${String(err)}${viewportFailureGuidance}${screenshotGuidance}`);
    }
    sendHeartbeatSync(taskId, 'npm run build (admin-ui)...');
    execFileSync('npm', ['run', 'build'], { cwd: `${WORKSPACE}/admin-ui`, encoding: 'utf-8', timeout: FRONTEND_BUILD_TIMEOUT, env: adminUiEnv });
    sendLog(ws, 'Frontend checks passed.\n');
  }
  for (const dir of tsDirs) {
    sendLog(ws, `Running TypeScript checks in ${dir}...\n`);
    sendHeartbeatSync(taskId, `npm ci (${dir})...`);
    execFileSync('npm', ['ci'], { cwd: `${WORKSPACE}/${dir}`, encoding: 'utf-8', timeout: AGENT_NPM_TIMEOUT });
    sendHeartbeatSync(taskId, `tsc --noEmit (${dir})...`);
    execFileSync('npx', ['tsc', '--noEmit'], { cwd: `${WORKSPACE}/${dir}`, encoding: 'utf-8', timeout: AGENT_TSC_TIMEOUT });
    sendLog(ws, `TypeScript checks passed in ${dir}.\n`);
  }
  for (const [dir, pyFiles] of pyFilesByDir) {
    sendLog(ws, `Running ruff lint on ${pyFiles.length} changed .py file(s) in ${dir}...\n`);
    try {
      const lintOut = execFileSync('python3', ['-m', 'ruff', 'check', '--select', 'E,F,W', '--ignore', 'E501', ...pyFiles], { cwd: WORKSPACE, encoding: 'utf-8', timeout: RUFF_TIMEOUT });
      sendLog(ws, lintOut || 'Ruff lint passed.\n');
    } catch (err: unknown) {
      const errCode = (err as NodeJS.ErrnoException).code;
      const errMsg = err instanceof Error ? err.message : String(err);
      if (errCode === 'ENOENT' || errMsg.includes('No module named ruff')) {
        sendLog(ws, 'ruff not installed, skipping lint.\n');
      } else {
        throw err;
      }
    }
    sendLog(ws, `Running ruff format check on changed .py files in ${dir}...\n`);
    try {
      const fmtOut = execFileSync('python3', ['-m', 'ruff', 'format', '--check', ...pyFiles], { cwd: WORKSPACE, encoding: 'utf-8', timeout: RUFF_TIMEOUT });
      sendLog(ws, fmtOut || 'Ruff format check passed.\n');
    } catch (err: unknown) {
      const errCode = (err as NodeJS.ErrnoException).code;
      const errMsg = err instanceof Error ? err.message : String(err);
      if (errCode === 'ENOENT' || errMsg.includes('No module named ruff')) {
        sendLog(ws, 'ruff not installed, skipping format check.\n');
      } else {
        throw err;
      }
    }
  }
}

/** Normalized error fingerprint for detecting identical consecutive failures. */
export function errorFingerprint(msg: string): string {
  return msg.replace(/\s+/g, ' ').trim().slice(0, 2000);
}

// ── Prompt fingerprint ───────────────────────────────────────────────────────
// SHA-256 of the static prompt content (guidance blocks + task-type sections),
// truncated to 12 hex chars. Handlers set this before calling runClaudeCode so
// heartbeats and task records can correlate behavioral regressions to prompt edits.
// Task-specific noise (task IDs, file paths, payload text) must NOT be included.

/** Compute a stable 12-hex SHA-256 fingerprint of static prompt content. */
export function computePromptFingerprint(staticContent: string): string {
  return createHash('sha256').update(staticContent).digest('hex').slice(0, 12);
}

let currentPromptFingerprint: string | null = null;

/** Register the fingerprint for the current task's prompt. Call before runClaudeCode. */
export function setCurrentPromptFingerprint(fp: string): void {
  currentPromptFingerprint = fp;
}

export function getCurrentPromptFingerprint(): string | null {
  return currentPromptFingerprint;
}

// ── Per-task LLM telemetry (efficiency direction d-91d8dede) ─────────────────
// Cumulative counters posted on every heartbeat / terminal status. Bumped by
// the LLM CLI runner (one increment per spawn) and by the stream-json result
// parser (token totals). Reset between roles so warm-VM continuity does not
// double-count prior roles' usage. Tokens accumulate across all turns within
// a single CLI invocation; llm_call_count counts CLI invocations.
let _llmCallCount = 0;
let _totalInputTokens = 0;
let _totalOutputTokens = 0;

/** Increment the per-task LLM call counter (one per CLI invocation). */
export function recordLlmCallStart(): void {
  _llmCallCount += 1;
}

/** Add the input/output token totals reported by a single CLI result event. */
export function recordLlmTokens(inputTokens: number, outputTokens: number): void {
  if (Number.isFinite(inputTokens) && inputTokens > 0) {
    _totalInputTokens += Math.floor(inputTokens);
  }
  if (Number.isFinite(outputTokens) && outputTokens > 0) {
    _totalOutputTokens += Math.floor(outputTokens);
  }
}

/** Snapshot the current cumulative LLM telemetry for heartbeat / status posts. */
export function getLlmTelemetry(): {
  llm_call_count: number;
  total_input_tokens: number;
  total_output_tokens: number;
} {
  return {
    llm_call_count: _llmCallCount,
    total_input_tokens: _totalInputTokens,
    total_output_tokens: _totalOutputTokens,
  };
}

/** Reset between roles so per-task counters don't bleed across warm-VM continuity. */
export function resetLlmTelemetry(): void {
  _llmCallCount = 0;
  _totalInputTokens = 0;
  _totalOutputTokens = 0;
}

// ── Divergence detection helpers ─────────────────────────────────────────────

const _TOKEN_RE = /\w+/g;

/** Jaccard token-set similarity between two strings. Range [0,1]. */
export function jaccardSimilarity(a: string, b: string): number {
  const tokA = new Set(a.toLowerCase().match(_TOKEN_RE) ?? []);
  const tokB = new Set(b.toLowerCase().match(_TOKEN_RE) ?? []);
  if (tokA.size === 0 && tokB.size === 0) return 1;
  if (tokA.size === 0 || tokB.size === 0) return 0;
  let intersection = 0;
  for (const t of tokA) if (tokB.has(t)) intersection++;
  return intersection / (tokA.size + tokB.size - intersection);
}

interface DiffLines { added: Set<string>; removed: Set<string> }

/** Capture the set of added/removed lines in the working tree vs HEAD. */
function captureDiffLines(): DiffLines {
  const added = new Set<string>();
  const removed = new Set<string>();
  try {
    const raw = execSync('git diff HEAD --no-color', { cwd: WORKSPACE, timeout: 10_000 }).toString();
    let count = 0;
    for (const line of raw.split('\n')) {
      if (++count > 5000) break;
      const content = line.slice(1).trim();
      if (!content) continue;
      if (line.startsWith('+') && !line.startsWith('+++')) added.add(content);
      else if (line.startsWith('-') && !line.startsWith('---')) removed.add(content);
    }
  } catch { /* git unavailable or clean tree */ }
  return { added, removed };
}

/**
 * True when currDiff substantially undoes prevDiff — meaning that a large
 * fraction of prevDiff's added lines have vanished in currDiff.
 */
function detectDiffRevert(prev: DiffLines, curr: DiffLines, threshold: number): boolean {
  if (prev.added.size === 0) return false;
  let vanished = 0;
  for (const line of prev.added) if (!curr.added.has(line)) vanished++;
  return vanished / prev.added.size >= threshold;
}

type DivergenceReason = 'divergence_oscillation' | 'divergence_similar_error' | 'divergence_diff_revert';

/**
 * Check for three divergence patterns after at least 3 fix iterations.
 * Returns the first detected reason, or null if none found.
 * Conservative: does not fire until fingerprintHistory has ≥ 3 entries.
 */
export function detectLoopDivergence(
  fingerprintHistory: string[],
  errorTextHistory: string[],
  prevDiff: DiffLines | null,
  currDiff: DiffLines | null,
): DivergenceReason | null {
  if (fingerprintHistory.length < 3) return null;
  const N = fingerprintHistory.length - 1;

  // Oscillation: error A -> error B -> error A (fingerprint repeats at N-2)
  if (fingerprintHistory[N] === fingerprintHistory[N - 2] && fingerprintHistory[N - 1] !== fingerprintHistory[N]) {
    return 'divergence_oscillation';
  }

  // Jaccard: both consecutive error pairs are semantically near-identical
  const j1 = jaccardSimilarity(errorTextHistory[N], errorTextHistory[N - 1]);
  const j2 = jaccardSimilarity(errorTextHistory[N - 1], errorTextHistory[N - 2]);
  if (j1 >= FIX_LOOP_JACCARD_THRESHOLD && j2 >= FIX_LOOP_JACCARD_THRESHOLD) {
    return 'divergence_similar_error';
  }

  // Diff revert: last Claude iteration substantially undid the previous one
  if (prevDiff !== null && currDiff !== null && detectDiffRevert(prevDiff, currDiff, FIX_LOOP_DIFF_REVERT_THRESHOLD)) {
    return 'divergence_diff_revert';
  }

  return null;
}

// Build + buffer a FixLoopEvent from one wrapper-loop iteration. Centralized
// so lintFixLoop and testFixLoop record identical-shaped events; the buffer
// drain on the next heartbeat flushes them to the backend.
function emitWrapperFixLoopEvent(opts: {
  loop: FixLoopName;
  iteration: number;
  errorMessage: string;
  exitReason: FixLoopExitReason;
  startedAt: string;
}): void {
  const fingerprint = errorFingerprint(opts.errorMessage);
  pushFixLoopEvent({
    loop: opts.loop,
    iteration: opts.iteration,
    fingerprint_hash: fingerprintHash(fingerprint),
    error_tail: opts.errorMessage.slice(-800),
    change_summary: captureFixIterationDiffStat(),
    exit_reason: opts.exitReason,
    source: 'agent_emitted',
    started_at: opts.startedAt,
    ended_at: new Date().toISOString(),
  });
}

/** Run lint checks in a loop, asking Claude to fix failures up to MAX_LINT_FIX_ATTEMPTS times. */
// Snapshot the cumulative working-tree change against task-start HEAD. Used
// by the within-task fix loops to give the next iteration's prompt visibility
// into what prior iterations actually modified — so it can pivot when the
// same approach has already been tried and failed.
function captureFixIterationDiffStat(): string {
  try {
    return execSync('git diff HEAD --stat --no-color', {
      cwd: WORKSPACE,
      encoding: 'utf-8',
      timeout: 5000,
    }).trim();
  } catch {
    return '(diff capture failed)';
  }
}

function buildFixLoopGuidanceSuffix(): string {
  return `\n\n${READ_THE_FAILURE_MESSAGE}\n${SIZE_GUARDRAIL_GUIDANCE}\n${PRIOR_ATTEMPT_DIVERGENCE}`;
}

/**
 * Detect infrastructure-class failures that cannot be fixed by Claude.
 * These include missing tools, Docker unavailability, OOM, DNS/network issues.
 * Pattern set is conservative. Used by lintFixLoop's first-attempt abandon
 * check; testFixLoop does its own runner-level setup-error detection and does
 * not call this.
 */
export function isInfrastructureFailure(msg: string): boolean {
  const lower = msg.toLowerCase();
  // Tool/command unavailable
  if (lower.includes('command not found') || lower.includes('enoent')) return true;
  // Docker daemon unavailable (Playwright, Docker-based tests)
  if (lower.includes('cannot connect to docker daemon')) return true;
  // OOM / SIGKILL (exit 137 shown as "killed" or "EOOM" code)
  if (lower.includes('exit 137') || lower.includes('eoom') || lower.includes('out of memory')) return true;
  if (lower.includes('killed') && (lower.includes('sigkill') || lower.includes('oom'))) return true;
  // Process timeout / cannot spawn (ETIMEDOUT, spawn timeout)
  if (lower.includes('etimedout') || lower.includes('spawn timeout')) return true;
  // DNS / network errors
  if (lower.includes('econnrefused') || lower.includes('enotfound') || lower.includes('enetunreach')) return true;
  // Chromium/Playwright installation infrastructure failures
  if (lower.includes('chromium') && (lower.includes('sudo') || lower.includes('permission denied'))) return true;
  // Docker/container infrastructure issues
  if (lower.includes('docker') && (lower.includes('daemon') || lower.includes('unavailable'))) return true;
  return false;
}

export interface LintFixLoopResult {
  /** True if lint converged green within MAX_LINT_FIX_ATTEMPTS. */
  passed: boolean;
  /** Last lint error message (tail). Set when passed=false; undefined on success. */
  lastErrorTail?: string;
  /** Why the loop stopped. */
  exitReason?: 'early_unchanged' | 'max_attempts' | 'divergence_oscillation' | 'divergence_similar_error' | 'divergence_diff_revert';
  /** Infrastructure/setup failure detected; no fixer invoked. */
  setupFailed?: boolean;
  /** Summary of infrastructure failure when setupFailed=true. */
  infraFailureSummary?: string;
  /** Per-attempt diff/error history, useful for downstream diagnosis. */
  attempts: FixAttempt[];
}

export async function lintFixLoop(
  changedPaths: string[], ws: WebSocket | null, statusRef: { value: string },
  context: string = 'the code you just wrote', model?: string, effort?: string,
  buildConfig?: BuildConfig
): Promise<LintFixLoopResult> {
  const fingerprintHistory: string[] = [];
  const errorTextHistory: string[] = [];
  const prevAttempts: FixAttempt[] = [];
  let lastErrorTail: string | undefined;
  let prevDiff: DiffLines | null = null;
  let currDiff: DiffLines | null = null;
  for (let attempt = 0; attempt < MAX_LINT_FIX_ATTEMPTS; attempt++) {
    const iterStartedAt = new Date().toISOString();
    try {
      statusRef.value = attempt === 0 ? 'Running pre-commit checks...' : `Re-running lint (attempt ${attempt + 1}/${MAX_LINT_FIX_ATTEMPTS})...`;
      runProjectLintChecks(changedPaths, ws, buildConfig);
      emitWrapperFixLoopEvent({
        loop: 'lint', iteration: attempt + 1, errorMessage: '',
        exitReason: 'passed', startedAt: iterStartedAt,
      });
      return { passed: true, attempts: prevAttempts };
    } catch (lintErr: unknown) {
      const msg = lintErr instanceof Error ? lintErr.message : String(lintErr);
      lastErrorTail = msg;
      const fingerprint = errorFingerprint(msg);

      // Infrastructure failure detection: on the first attempt, if the error
      // is infrastructure-class (missing tool, Docker unavailable, OOM, DNS),
      // abandon immediately without invoking Claude. This reduces wasted API
      // calls on the failing tail and improves failure classification.
      // Do NOT emit a fix-loop event here: this is not a lint outcome, and any
      // exit_reason would either be misclassified as success ('passed' is in
      // the backend's _FIX_LOOP_SUCCESS_REASONS) or be a misleading lint
      // terminal reason. The infra abandon is surfaced via setupFailed=true so
      // the caller classifies the task as an infra failure — matching the
      // testFixLoop setup-failure precedent, which also returns without
      // emitting a fix-loop event.
      if (attempt === 0 && isInfrastructureFailure(msg)) {
        sendLog(ws, `Lint check failed due to infrastructure issue (not a code/lint failure). Not invoking fixer.\n${msg.slice(0, 2000)}\n`);
        return { passed: false, lastErrorTail, setupFailed: true, infraFailureSummary: msg.slice(0, 2000), attempts: prevAttempts };
      }

      // Early exit: byte-identical consecutive error (existing behaviour).
      if (fingerprintHistory.length > 0 && fingerprint === fingerprintHistory[fingerprintHistory.length - 1]) {
        emitWrapperFixLoopEvent({
          loop: 'lint', iteration: attempt + 1, errorMessage: msg,
          exitReason: 'early_unchanged', startedAt: iterStartedAt,
        });
        sendLog(ws, `Lint error unchanged from previous attempt — exiting early (persistent, unfixable by Claude).\n`);
        return { passed: false, lastErrorTail, exitReason: 'early_unchanged', attempts: prevAttempts };
      }
      fingerprintHistory.push(fingerprint);
      errorTextHistory.push(msg);
      // Divergence detection: oscillation, Jaccard-similar errors, diff revert.
      // Requires ≥ 3 iterations of history to avoid false-positive abandons.
      const divergence = detectLoopDivergence(fingerprintHistory, errorTextHistory, prevDiff, currDiff);
      if (divergence !== null) {
        emitWrapperFixLoopEvent({
          loop: 'lint', iteration: attempt + 1, errorMessage: msg,
          exitReason: divergence, startedAt: iterStartedAt,
        });
        sendLog(ws, `Lint fix loop divergence detected (${divergence}) — abandoning to avoid wasted iterations.\n`);
        return { passed: false, lastErrorTail, exitReason: divergence, attempts: prevAttempts };
      }
      // Final iteration with a non-duplicate failure means the budget ran
      // out; mark it terminal so downstream telemetry sees the actual exit
      // reason rather than an orphaned in_progress.
      const isLastAttempt = attempt === MAX_LINT_FIX_ATTEMPTS - 1;
      emitWrapperFixLoopEvent({
        loop: 'lint', iteration: attempt + 1, errorMessage: msg,
        exitReason: isLastAttempt ? 'max_attempts' : 'in_progress',
        startedAt: iterStartedAt,
      });
      // On retry (attempt >= 1), record what the previous Claude call
      // actually did + the failure that persisted, so the next iteration's
      // prompt can pivot away from approaches that didn't work.
      if (attempt >= 1) {
        prevAttempts.push({
          attemptNumber: attempt,
          diffStat: captureFixIterationDiffStat(),
          errorTail: msg.slice(-800),
        });
      }
      sendLog(ws, `Pre-commit check failed (attempt ${attempt + 1}/${MAX_LINT_FIX_ATTEMPTS}):\n${msg.slice(0, 3000)}\nClaude will attempt to fix...\n`);
      const historySection = prevAttempts.length > 0 ? `${formatFixIterationHistory(prevAttempts)}\n\n` : '';
      const guidanceSuffix = attempt >= 1 ? buildFixLoopGuidanceSuffix() : '';
      const fixPrompt = `${historySection}The following lint/build errors were found in ${context}. Fix ALL of them — do not use \`any\` types.\n\n${msg.slice(0, 5000)}${guidanceSuffix}`;
      await runClaudeCode(fixPrompt, ws, statusRef, model, effort);
      git(['add', '-A']);
      prevDiff = currDiff;
      currDiff = captureDiffLines();
    }
  }
  sendLog(ws, `WARNING: Lint checks still failing after ${MAX_LINT_FIX_ATTEMPTS} attempts.\n`);
  return { passed: false, lastErrorTail, exitReason: 'max_attempts', attempts: prevAttempts };
}

// ── Test runner detection (shared by testFixLoop and captureTestBaseline) ───
export interface TestRunner {
  cmd: string;
  args: string[];
  cwd: string;
  label: string;
}

/** Returns true if the runner invokes Docker or the backend test shell script. */
function isDockerRunner(runner: TestRunner): boolean {
  const cmdAndArgs = [runner.cmd, ...runner.args].join(' ');
  return cmdAndArgs.includes('docker') || cmdAndArgs.includes('scripts/test-backend.sh');
}

function isDockerAvailable(): boolean {
  try {
    execSync('docker version', { stdio: 'ignore', timeout: 2000 });
    return true;
  } catch {
    return false;
  }
}

function detectTestRunners(buildConfig?: BuildConfig): TestRunner[] {
  const { existsSync, readFileSync } = fs;
  const runners: TestRunner[] = [];

  // --- BuildConfig-based detection (takes precedence over auto-detection) ---
  if (buildConfig?.directories) {
    for (const dir of buildConfig.directories) {
      if (dir.test_cmd) {
        const cwd = dir.path ? `${WORKSPACE}/${dir.path}` : WORKSPACE;
        const parts = dir.test_cmd.split(/\s+/);
        runners.push({ cmd: parts[0], args: parts.slice(1), cwd, label: `${dir.test_cmd} (${dir.path || 'root'})` });
      }
    }
  }

  // --- Auto-detection (only if buildConfig produced no runners) ---
  // TS doesn't track .push() mutations, so use a Boolean accessor that always reads the
  // current length to avoid spurious "always-true" narrowing on the literal-empty array.
  const noRunners = (): boolean => runners.length === 0;
  if (noRunners()) {
    const pkgPath = `${WORKSPACE}/package.json`;
    const pytestIniPath = `${WORKSPACE}/pytest.ini`;
    const pyprojectPath = `${WORKSPACE}/pyproject.toml`;
    const cargoPath = `${WORKSPACE}/Cargo.toml`;

    if (existsSync(pkgPath)) {
      try {
        const pkg = JSON.parse(readFileSync(pkgPath, 'utf-8')) as { scripts?: { test?: string } };
        if (pkg.scripts?.test) {
          runners.push({ cmd: 'npm', args: ['test'], cwd: WORKSPACE, label: 'npm test (root)' });
        }
      } catch { /* malformed package.json — skip */ }
    }

    if (noRunners()) {
      if (existsSync(pytestIniPath)) {
        runners.push({ cmd: 'python3', args: ['-m', 'pytest', '-x', '-q'], cwd: WORKSPACE, label: 'pytest (root)' });
      } else if (existsSync(pyprojectPath)) {
        try {
          if (readFileSync(pyprojectPath, 'utf-8').includes('pytest')) {
            runners.push({ cmd: 'python3', args: ['-m', 'pytest', '-x', '-q'], cwd: WORKSPACE, label: 'pytest (root pyproject)' });
          }
        } catch { /* unreadable — skip */ }
      }
    }

    if (noRunners() && existsSync(cargoPath)) {
      runners.push({ cmd: 'cargo', args: ['test'], cwd: WORKSPACE, label: 'cargo test (root)' });
    }

    // --- Subdirectory detection (only if no root-level runner found) ---
    if (noRunners()) {
      const backendPytestIni = `${WORKSPACE}/backend/pytest.ini`;
      const backendPyproject = `${WORKSPACE}/backend/pyproject.toml`;
      const backendTestScript = `${WORKSPACE}/scripts/test-backend.sh`;
      const agentPkg = `${WORKSPACE}/agent/package.json`;

      // --postgres makes test-backend.sh stand up a Postgres sidecar so the
      // local run matches CI. Without it, the script defaults to in-memory
      // SQLite and Postgres-only failures (enum/JSONB/migration drift) only
      // surface on CI, after a push, burning a full review-fix iteration.
      // When Docker is unavailable, fall back to --no-postgres so tests still
      // run locally via the in-memory SQLite path rather than hard-failing.
      const pgFlag = isDockerAvailable() ? '--postgres' : '--no-postgres';
      if (existsSync(backendPytestIni)) {
        if (existsSync(backendTestScript)) {
          runners.push({ cmd: 'bash', args: ['scripts/test-backend.sh', pgFlag], cwd: WORKSPACE, label: 'pytest (backend)' });
        } else {
          runners.push({ cmd: 'python3', args: ['-m', 'pytest', '-x', '-q'], cwd: `${WORKSPACE}/backend`, label: 'pytest (backend)' });
        }
      } else if (existsSync(backendPyproject)) {
        try {
          if (readFileSync(backendPyproject, 'utf-8').includes('pytest')) {
            if (existsSync(backendTestScript)) {
              runners.push({ cmd: 'bash', args: ['scripts/test-backend.sh', pgFlag], cwd: WORKSPACE, label: 'pytest (backend pyproject)' });
            } else {
              runners.push({ cmd: 'python3', args: ['-m', 'pytest', '-x', '-q'], cwd: `${WORKSPACE}/backend`, label: 'pytest (backend pyproject)' });
            }
          }
        } catch { /* unreadable — skip */ }
      }

      if (existsSync(agentPkg)) {
        try {
          const pkg = JSON.parse(readFileSync(agentPkg, 'utf-8')) as { scripts?: { test?: string } };
          if (pkg.scripts?.test) {
            runners.push({ cmd: 'npm', args: ['test'], cwd: `${WORKSPACE}/agent`, label: 'npm test (agent)' });
          }
        } catch { /* malformed package.json — skip */ }
      }
    }
  }

  return runners;
}

/**
 * Parse failing test identifiers from test runner output.
 * Returns a Set of stable test IDs (e.g. "path/test_foo.py::test_bar" for pytest,
 * "test description" for vitest) for baseline comparison.
 */
export function parseFailingTests(output: string): Set<string> {
  const failing = new Set<string>();
  for (const line of output.split('\n')) {
    const trimmed = line.trim();
    // pytest: "FAILED backend/testing/test_foo.py::test_bar - AssertionError: ..."
    if (trimmed.startsWith('FAILED ')) {
      const id = trimmed.slice('FAILED '.length).split(' - ')[0].trim();
      if (id) failing.add(id);
    }
    // vitest file-level failure line: " FAIL  src/foo.test.ts > suite > test"
    // (the default reporter emits one per failing file/test, leading space
    // trims away). This was silently missing from the prior parser —
    // vitest does NOT use "FAILED" (the pytest word) and doesn't always
    // use ✕ markers for per-test failures, so a vitest baseline run
    // with real failures was reporting 0.
    //
    // Tightened to `FAIL` + 2+ spaces + non-space body to avoid false
    // positives from test-output lines like `FAIL reason: expected...`
    // (tests that verify failure-formatting can emit FAIL as part of
    // their own stdout). JSON reporter remains the primary path — this
    // is only a fallback.
    const vitestFailMatch = trimmed.match(/^FAIL\s{2,}(\S.*)$/);
    if (vitestFailMatch) failing.add(vitestFailMatch[1]);
    // vitest: "✕ test description" or "× test description" or "✗ test description".
    // Fallback only — prefer parseFailingTestsFromVitestJson when available,
    // since stdout scanning mis-classifies simulated failure output inside
    // passing tests (a test that verifies failure-formatting logic can print
    // a mocked "✕ foo" line that this regex matches even though the real
    // test passed).
    const vitestMatch = trimmed.match(/^[✕✗×⨯]\s+(.+)$/u);
    if (vitestMatch) failing.add(vitestMatch[1].trim());
  }
  return failing;
}

export function normalizePytestSelectorForRunner(selector: string, runner?: TestRunner): string {
  const normalized = selector.trim().replace(/\\/g, '/');
  if (!normalized) return '';
  const invokesBackendScript = Boolean(
    runner && [runner.cmd, ...runner.args].some(arg => arg.includes('scripts/test-backend.sh'))
  );
  const cwd = runner?.cwd.replace(/\\/g, '/') || '';
  const runsFromBackend = invokesBackendScript || cwd.endsWith('/backend');
  if (runsFromBackend && normalized.startsWith('backend/')) {
    return normalized.slice('backend/'.length);
  }
  if (!runsFromBackend && normalized.startsWith('testing/')) {
    return `backend/${normalized}`;
  }
  return normalized;
}

export function extractPytestSelectorsFromOutput(output: string): string[] {
  const selectors = new Set<string>();
  for (const id of parseFailingTests(output)) {
    const normalized = id.trim().replace(/\\/g, '/');
    if (/^(backend\/)?testing\/.+\.py(?:::|$)/.test(normalized)) {
      selectors.add(normalized);
    }
  }

  const selectorPattern = /\b((?:backend\/)?testing\/[A-Za-z0-9_./-]+\.py(?:::[^\s\r\n]+)?)/g;
  for (const line of output.split('\n')) {
    if (!/\b(?:FAILED|ERROR)\b|::error\b/i.test(line)) continue;
    for (const match of line.matchAll(selectorPattern)) {
      selectors.add(match[1].trim().replace(/\\/g, '/'));
    }
  }

  return Array.from(selectors);
}

function acceptsPytestSelectors(runner: TestRunner): boolean {
  const cmdAndArgs = [runner.cmd, ...runner.args].join(' ');
  return cmdAndArgs.includes('pytest') || cmdAndArgs.includes('scripts/test-backend.sh');
}

function withPytestSelectors(runner: TestRunner, selectors: string[]): TestRunner | null {
  if (!acceptsPytestSelectors(runner)) return null;
  const normalized = Array.from(new Set(
    selectors
      .map(selector => normalizePytestSelectorForRunner(selector, runner))
      .filter(Boolean)
  ));
  if (normalized.length === 0) return null;
  return {
    ...runner,
    args: [...runner.args, ...normalized],
    label: `${runner.label} (targeted CI failures)`,
  };
}

export function buildTargetedPytestRunners(runners: TestRunner[], selectors: string[]): TestRunner[] {
  return runners
    .map(runner => withPytestSelectors(runner, selectors))
    .filter((runner): runner is TestRunner => runner !== null);
}

function withRequiredTestmon(runner: TestRunner): TestRunner {
  const invokesBackendScript = [runner.cmd, ...runner.args].some(arg => arg.includes('scripts/test-backend.sh'));
  if (!invokesBackendScript || runner.args.includes('--require-testmon')) return runner;
  return {
    ...runner,
    args: [...runner.args, '--require-testmon'],
    label: `${runner.label} (testmon impact)`,
  };
}

// Minimal vitest JSON reporter shape — we only need the failed-test names.
// Full schema at https://vitest.dev/guide/reporters.html#json-reporter; these
// fields are stable across vitest 2.x/3.x.
interface VitestJsonReport {
  testResults?: Array<{
    name?: string;
    assertionResults?: Array<{
      fullName?: string;
      title?: string;
      status?: 'passed' | 'failed' | 'skipped' | 'pending' | 'todo';
    }>;
  }>;
}

/**
 * Extract failing test IDs from a vitest JSON reporter file. Returns the
 * fullName (or title) of every assertion with status="failed". Preferred
 * over stdout scanning because stdout can contain false-positive markers
 * from inside passing tests' mocked output.
 */
export function parseFailingTestsFromVitestJson(jsonText: string): Set<string> {
  const failing = new Set<string>();
  let parsed: VitestJsonReport;
  try {
    parsed = JSON.parse(jsonText) as VitestJsonReport;
  } catch {
    return failing;
  }
  for (const file of parsed.testResults ?? []) {
    for (const assertion of file.assertionResults ?? []) {
      if (assertion.status === 'failed') {
        const id = (assertion.fullName || assertion.title || '').trim();
        if (id) failing.add(id);
      }
    }
  }
  return failing;
}

function isVitestRunner(cwd: string): boolean {
  return fs.existsSync(`${cwd}/vitest.config.ts`) ||
    fs.existsSync(`${cwd}/vitest.config.js`) ||
    fs.existsSync(`${cwd}/vitest.config.mjs`);
}

/**
 * Run the test suite with a short timeout to capture which tests are already failing
 * before any code changes are made. Used to distinguish pre-existing failures from
 * newly-broken tests introduced by the agent.
 *
 * - Scope-limited to runners relevant to relatedFiles (avoids running full suite).
 * - Time-bounded by timeoutMs (default 60s) — returns null on timeout/error.
 * - Returns an empty Set if all tests pass (all future failures are new).
 * - Returns null to signal "skip baseline" — fall back to current behavior.
 */
export async function captureTestBaseline(
  ws: WebSocket | null,
  statusRef: { value: string },
  buildConfig?: BuildConfig,
  relatedFiles?: string[],
  timeoutMs = TEST_BASELINE_TIMEOUT,
): Promise<Set<string> | null> {
  let runners = detectTestRunners(buildConfig);

  if (runners.length === 0) return null;

  // Scope-limit to runners relevant to the files being changed. This avoids
  // running the full backend Docker test suite when only agent files changed.
  if (relatedFiles && relatedFiles.length > 0) {
    const touchesAgent = relatedFiles.some(f => f.startsWith('agent/'));
    const touchesBackend = relatedFiles.some(f => f.startsWith('backend/'));
    if (touchesAgent || touchesBackend) {
      const filtered = runners.filter(r => {
        if (touchesAgent && (r.label.includes('agent') || r.cwd.includes('/agent'))) return true;
        if (touchesBackend && (r.label.includes('backend') || r.label.includes('pytest') || r.cwd.includes('/backend'))) return true;
        return false;
      });
      // Only apply the filter if it produced at least one runner
      if (filtered.length > 0) runners = filtered;
    }
  }

  sendLog(ws, `Capturing test baseline for: ${runners.map(r => r.label).join(', ')}\n`);
  statusRef.value = 'Capturing test baseline...';

  const baselineFailures = new Set<string>();
  const taskId = process.env.TASK_ID || '';

  for (const runner of runners) {
    // For vitest, request the JSON reporter so we can extract failing
    // test names authoritatively — the stdout parser historically missed
    // vitest's " FAIL" format, producing a "0 pre-existing failures"
    // baseline even when the suite had many. When JSON is available it's
    // the source of truth; stdout is a fallback for runners (pytest) that
    // don't emit a comparable JSON report at this runner-invocation layer.
    const useVitestJson = runner.cmd === 'npm' && isVitestRunner(runner.cwd);
    const jsonOutputPath = useVitestJson
      ? `/tmp/vitest-baseline-${Date.now()}-${Math.random().toString(36).slice(2, 8)}.json`
      : null;
    const runArgs = jsonOutputPath
      ? [...runner.args, '--', '--reporter=default', '--reporter=json', `--outputFile=${jsonOutputPath}`]
      : runner.args;

    try {
      sendHeartbeatSync(taskId, `Baseline test run [${runner.label}]...`);
      const runnerTimeout = isDockerRunner(runner) ? TEST_BASELINE_DOCKER_TIMEOUT : timeoutMs;
      await execFileAsync(runner.cmd, runArgs, {
        cwd: runner.cwd,
        timeoutMs: runnerTimeout,
      });
      // Runner exited cleanly — no failures
    } catch (err: unknown) {
      // execFileAsync sets killed=true and code='ETIMEDOUT' when the process is
      // terminated due to timeout; ENOENT-style errors (failure to spawn) also
      // surface with code. Both are infrastructure issues — can't capture
      // baseline, proceed without it.
      const isTimeout = err instanceof Error && (
        (err as Error & { killed?: boolean }).killed === true ||
        (err as NodeJS.ErrnoException).code === 'ETIMEDOUT'
      );
      if (isTimeout) {
        sendLog(ws, `Test baseline timed out for [${runner.label}] — proceeding without baseline.\n`);
        return null;
      }
      const msg = err instanceof Error ? err.message : String(err);
      // Prefer JSON reporter output when available — authoritative and
      // format-stable across vitest 2.x/3.x.
      let parsedFromJson = false;
      if (jsonOutputPath && fs.existsSync(jsonOutputPath)) {
        try {
          const jsonText = fs.readFileSync(jsonOutputPath, 'utf-8');
          const parsed = parseFailingTestsFromVitestJson(jsonText);
          if (parsed.size > 0) {
            parsed.forEach(f => baselineFailures.add(f));
            parsedFromJson = true;
          } else if (jsonText.includes('"numFailedTests":0') || jsonText.includes('"success":true')) {
            // JSON reporter says 0 failures but the process exited non-zero.
            // That's a non-test issue (setup error, runner crash); fall
            // through to stdout parser to surface whatever broke.
            parsedFromJson = false;
          } else {
            // JSON loaded but extraction returned empty — fall back to
            // stdout so we don't silently treat a broken suite as passing.
            parsedFromJson = false;
          }
        } catch {
          // JSON file unreadable/malformed — fall through to stdout.
        }
      }
      if (!parsedFromJson) {
        const parsed = parseFailingTests(msg);
        parsed.forEach(f => baselineFailures.add(f));
        // If BOTH paths came up empty despite the process exiting
        // non-zero, something's wrong — log loudly so an operator can
        // investigate rather than silently treating it as a clean baseline.
        if (baselineFailures.size === 0) {
          sendLog(
            ws,
            `WARNING: Test baseline for [${runner.label}] exited non-zero ` +
            `but neither JSON reporter nor stdout parser extracted any ` +
            `failures. Proceeding without baseline — treat any failures ` +
            `in the fix loop as potentially pre-existing.\n`,
          );
          return null;
        }
      }
    } finally {
      // Best-effort cleanup of the baseline JSON file.
      if (jsonOutputPath) {
        try { fs.unlinkSync(jsonOutputPath); } catch { /* ignore */ }
      }
    }
  }

  if (baselineFailures.size > 0) {
    sendLog(ws, `Baseline: ${baselineFailures.size} pre-existing failure(s) found — these will be excluded from test fix analysis.\n`);
  } else {
    sendLog(ws, 'Baseline captured: all tests passing before changes.\n');
  }

  return baselineFailures;
}

/**
 * Detect the project's test runner, run tests, and retry via Claude on failure.
 * When baselineFailures is provided, distinguishes pre-existing failures from
 * newly-broken tests and only counts new failures toward task FAILED status.
 * Returns whether tests passed and whether failures were detected (for commit note).
 */

/** Extracts plan-alignment-relevant sections ('Files to change', 'Interfaces to preserve',
 * 'Key dependencies', 'Callers to verify after implementation', 'Tests at risk of breaking')
 * from analysis context. Heading match is fuzzy (lowercased, key-term based) so minor wording
 * variations from the analysis agent (e.g. 'Files to modify') still produce complete section data. */
export function condensedAnalysisSections(analysisContext: string): string {
  // Split at section boundaries (lines starting a new **[A-Z] heading)
  const sectionBreak = /(?=\n\*\*[A-Z])/;
  const sections = analysisContext.split(sectionBreak);
  const isWantedHeading = (section: string): boolean => {
    const headingMatch = section.match(/^\s*\*\*([^*\n]+)\*\*/);
    if (!headingMatch) return false;
    const h = headingMatch[1].toLowerCase();
    return (
      (h.includes('files') && (h.includes('change') || h.includes('modify') || h.includes('verify')))
      || (h.includes('interfaces') && h.includes('preserve'))
      || h.includes('dependencies')
      || (h.includes('callers') && h.includes('verify'))
      || (h.includes('tests') && h.includes('risk'))
    );
  };
  const wanted = sections.filter(isWantedHeading);
  if (wanted.length === 0) return analysisContext.slice(0, 800).trim();
  return wanted.map(s => s.trim()).join('\n\n');
}

export interface TestFixLoopOptions {
  targetPytestSelectors?: string[];
  runImpactAfterTargeted?: boolean;
  requireTestmonForImpact?: boolean;
  maxFixAttempts?: number;
  phaseLabel?: string;
  runnersOverride?: TestRunner[];
}

export async function testFixLoop(
  ws: WebSocket | null, statusRef: { value: string },
  model?: string, effort?: string, buildConfig?: BuildConfig,
  baselineFailures?: Set<string>,
  relatedFiles?: string[],
  analysisContext?: string,
  options: TestFixLoopOptions = {},
): Promise<{ passed: boolean; testFailuresDetected: boolean; setupFailed?: boolean; failureSummary?: string }> {
  let runners = options.runnersOverride ?? detectTestRunners(buildConfig);
  const targetPytestSelectors = Array.from(new Set(options.targetPytestSelectors ?? []));

  // Scope-limit to runners relevant to the files being changed. Same logic as
  // captureTestBaseline — avoids running the full backend Docker test suite
  // when only agent files changed.
  if (!options.runnersOverride && targetPytestSelectors.length === 0 && relatedFiles && relatedFiles.length > 0) {
    const touchesAgent = relatedFiles.some(f => f.startsWith('agent/'));
    const touchesBackend = relatedFiles.some(f => f.startsWith('backend/'));
    if (touchesAgent || touchesBackend) {
      const filtered = runners.filter(r => {
        if (touchesAgent && (r.label.includes('agent') || r.cwd.includes('/agent'))) return true;
        if (touchesBackend && (r.label.includes('backend') || r.label.includes('pytest') || r.cwd.includes('/backend'))) return true;
        return false;
      });
      if (filtered.length > 0) runners = filtered;
    }
  }

  if (runners.length === 0) {
    sendLog(ws, 'No test runner detected — skipping test step.\n');
    return { passed: true, testFailuresDetected: false };
  }

  sendLog(ws, `Detected test runner(s): ${runners.map(r => r.label).join(', ')}\n`);

  if (!options.runnersOverride && targetPytestSelectors.length > 0) {
    const targetedRunners = buildTargetedPytestRunners(runners, targetPytestSelectors);
    if (targetedRunners.length > 0) {
      sendLog(ws, `Running targeted CI failure tests first: ${targetPytestSelectors.slice(0, 8).join(', ')}${targetPytestSelectors.length > 8 ? ` (+${targetPytestSelectors.length - 8} more)` : ''}\n`);
      const targetedResult = await testFixLoop(
        ws, statusRef, model, effort, buildConfig,
        undefined, relatedFiles, analysisContext,
        {
          ...options,
          targetPytestSelectors: [],
          runImpactAfterTargeted: false,
          phaseLabel: 'Targeted CI failure verification',
          runnersOverride: targetedRunners,
        },
      );
      if (!targetedResult.passed || !options.runImpactAfterTargeted) {
        return targetedResult;
      }
      sendLog(ws, 'Targeted CI failure tests passed; running impact coverage with the detected test runner(s).\n');
      const impactRunners = options.requireTestmonForImpact
        ? runners.map(runner => withRequiredTestmon(runner))
        : runners;
      return testFixLoop(
        ws, statusRef, model, effort, buildConfig,
        baselineFailures, relatedFiles, analysisContext,
        {
          ...options,
          targetPytestSelectors: [],
          runImpactAfterTargeted: false,
          phaseLabel: 'Impact coverage',
          runnersOverride: impactRunners,
        },
      );
    }
    sendLog(ws, 'No pytest-compatible runner found for targeted CI failures — falling back to detected test runner(s).\n');
  }

  const fingerprintHistory: string[] = [];
  const errorTextHistory: string[] = [];
  const prevAttempts: FixAttempt[] = [];
  let prevDiff: DiffLines | null = null;
  let currDiff: DiffLines | null = null;
  const maxFixAttempts = Math.max(0, options.maxFixAttempts ?? MAX_TEST_FIX_ATTEMPTS);
  const totalAttempts = maxFixAttempts + 1;
  const phasePrefix = options.phaseLabel ? `${options.phaseLabel}: ` : '';
  for (let attempt = 0; attempt <= maxFixAttempts; attempt++) {
    const iterStartedAt = new Date().toISOString();
    statusRef.value = attempt === 0 ? `${phasePrefix}Running test suite...` : `${phasePrefix}Re-running tests (attempt ${attempt + 1}/${totalAttempts})...`;
    sendLog(ws, `${phasePrefix}Running tests (attempt ${attempt + 1}/${totalAttempts})...\n`);

    // structuredIds is populated for vitest runners from the JSON reporter
    // output file. When present, baseline comparison uses it as the source
    // of truth for WHICH tests failed, bypassing stdout pattern matching
    // (which mis-classifies mocked failure output inside passing tests).
    const failures: Array<{ label: string; message: string; structuredIds?: Set<string> }> = [];
    // Track setup errors (dep install, spawn timeout) separately from test
    // failures. If NO runner actually gets to execute tests, we can't
    // claim tests passed — the result must propagate as a setup failure
    // so the caller doesn't push unverified code.
    const setupErrors: Array<{ label: string; message: string }> = [];
    let ranAnyRunner = false;
    const taskId = process.env.TASK_ID || '';
    for (const runner of runners) {
      // Setup step: ensure node_modules exist for npm runners. A failure
      // HERE is NOT a test failure — it's dep-install breakage (registry
      // flake, lockfile drift, disk, postinstall crash). The Claude fix
      // loop can't meaningfully repair these; skip this runner but record
      // the error so we can report "tests couldn't run" when every runner
      // fails setup.
      if (runner.cmd === 'npm' && !fs.existsSync(`${runner.cwd}/node_modules`)) {
        sendLog(ws, `Installing dependencies for ${runner.label}...\n`);
        try {
          await execFileAsync('npm', ['ci'], { cwd: runner.cwd, timeoutMs: TEST_RUNNER_TIMEOUT });
        } catch (installErr) {
          const msg = installErr instanceof Error ? installErr.message : String(installErr);
          sendLog(
            ws,
            `WARNING: Dependency install failed for [${runner.label}] — ` +
            `setup error, not a fixable test failure. Skipping runner.\n` +
            `${msg.slice(0, 500)}\n`,
          );
          setupErrors.push({ label: runner.label, message: `npm ci failed: ${msg}` });
          continue;
        }
      }
      // For vitest runners, request the JSON reporter alongside default
      // (human-readable) output — the JSON file is the authoritative source
      // of failed-test IDs, stdout is kept for operator diagnosis.
      const useVitestJson = runner.cmd === 'npm' && isVitestRunner(runner.cwd);
      const jsonOutputPath = useVitestJson
        ? `/tmp/vitest-${Date.now()}-${Math.random().toString(36).slice(2, 8)}.json`
        : null;
      const runArgs = jsonOutputPath
        ? [...runner.args, '--', '--reporter=default', '--reporter=json', `--outputFile=${jsonOutputPath}`]
        : runner.args;

      try {
        sendHeartbeatSync(taskId, `Running tests [${runner.label}]...`);
        const runnerTimeout = isDockerRunner(runner) ? TEST_RUNNER_DOCKER_TIMEOUT : TEST_RUNNER_TIMEOUT;
        await execFileAsync(runner.cmd, runArgs, { cwd: runner.cwd, timeoutMs: runnerTimeout });
        ranAnyRunner = true;
        sendLog(ws, `Tests passed [${runner.label}].\n`);
      } catch (testErr: unknown) {
        // If the runner timed out (killed by the process-group SIGTERM/SIGKILL
        // chain in execFileAsync) or the OS could not spawn the process
        // (ETIMEDOUT), treat as a setup error — a Docker build timeout or
        // spawn failure is not a fixable code failure, but we also can't
        // claim tests passed.
        const isRunnerTimeout = testErr instanceof Error && (
          (testErr as Error & { killed?: boolean }).killed === true ||
          (testErr as NodeJS.ErrnoException).code === 'ETIMEDOUT'
        );
        if (isRunnerTimeout) {
          const msg = testErr instanceof Error ? testErr.message : String(testErr);
          sendLog(ws, `WARNING: Test runner [${runner.label}] timed out — skipping.\n`);
          setupErrors.push({ label: runner.label, message: `runner timeout: ${msg}` });
          continue;
        }
        // Non-timeout non-zero exit = the runner actually ran the suite
        // and at least one test failed. Record the failure.
        ranAnyRunner = true;
        const msg = testErr instanceof Error ? testErr.message : String(testErr);
        let structuredIds: Set<string> | undefined;
        if (jsonOutputPath && fs.existsSync(jsonOutputPath)) {
          try {
            const jsonText = fs.readFileSync(jsonOutputPath, 'utf-8');
            structuredIds = parseFailingTestsFromVitestJson(jsonText);
          } catch {
            // Fall back to stdout scanning via parseFailingTests downstream.
          }
        }
        failures.push({ label: runner.label, message: msg, structuredIds });
      } finally {
        // Don't leave per-run temp files behind (cheap, but they accumulate).
        if (jsonOutputPath) {
          try { fs.unlinkSync(jsonOutputPath); } catch { /* already gone */ }
        }
      }
    }

    if (failures.length === 0) {
      // "No failures" is only a pass if we actually ran at least one
      // runner. If every runner hit setup errors (npm ci / spawn
      // timeout), tests didn't run — we can't claim they passed, and
      // there's nothing for Claude to fix. Surface as a distinct
      // setupFailed state so the caller fails the task with an infra
      // category instead of merging unverified code.
      if (!ranAnyRunner && setupErrors.length > 0) {
        const summary = setupErrors
          .map(e => `[${e.label}] ${e.message}`)
          .join('\n\n')
          .slice(0, 2000);
        sendLog(ws, `Tests could not run — setup failed for all runners. Not invoking fix loop.\n${summary}\n`);
        return { passed: false, testFailuresDetected: false, setupFailed: true, failureSummary: summary };
      }
      emitWrapperFixLoopEvent({
        loop: 'test', iteration: attempt + 1, errorMessage: '',
        exitReason: 'passed', startedAt: iterStartedAt,
      });
      sendLog(ws, 'All tests passed.\n');
      return { passed: true, testFailuresDetected: false };
    }

    // Serialize each failure for operator-facing output. This same format
    // fed the old pattern matcher; it's still used as a fallback when we
    // don't have structured IDs (non-vitest runners).
    const renderFailure = (f: { label: string; message: string }) => `[${f.label}]:\n${f.message}`;

    // When baseline is available, separate new failures from pre-existing ones.
    // A failure block is pre-existing when all parsed test IDs within it appear
    // in the baseline. Unparseable failures (size=0) are treated as new (safe default).
    let fixableFailures: Array<{ label: string; message: string; structuredIds?: Set<string> }> = failures;
    let preExistingNote = '';

    if (baselineFailures) {
      const newFailures: typeof failures = [];
      const preExistingFailures: typeof failures = [];

      for (const failure of failures) {
        // Structured IDs (from vitest JSON reporter) win over stdout parsing.
        // Fall back to parseFailingTests only when JSON wasn't available.
        const failureIds = failure.structuredIds ?? parseFailingTests(failure.message);
        const isAllPreExisting = failureIds.size > 0 &&
          Array.from(failureIds).every(id => baselineFailures.has(id));
        if (isAllPreExisting) {
          preExistingFailures.push(failure);
        } else {
          newFailures.push(failure);
        }
      }

      if (newFailures.length === 0) {
        // Every detected failure existed before our changes — task is not responsible
        sendLog(ws, `All ${preExistingFailures.length} test failure(s) are pre-existing and were not caused by this task.\n`);
        return { passed: true, testFailuresDetected: false };
      }

      fixableFailures = newFailures;
      if (preExistingFailures.length > 0) {
        preExistingNote = `\n\nNote: The following test failures existed BEFORE your changes — do not attempt to fix these:\n${preExistingFailures.map(renderFailure).join('\n\n').slice(0, 1500)}`;
      }
    }

    const combinedFailures = fixableFailures.map(renderFailure).join('\n\n');
    const fingerprint = errorFingerprint(combinedFailures);
    // Early exit: byte-identical consecutive failure (existing behaviour).
    if (fingerprintHistory.length > 0 && fingerprint === fingerprintHistory[fingerprintHistory.length - 1]) {
      emitWrapperFixLoopEvent({
        loop: 'test', iteration: attempt + 1, errorMessage: combinedFailures,
        exitReason: 'early_unchanged', startedAt: iterStartedAt,
      });
      sendLog(ws, `Test failures unchanged from previous attempt — exiting early (persistent, unfixable by Claude).\n`);
      const failureSummary = combinedFailures.slice(-8000);
      return { passed: false, testFailuresDetected: true, failureSummary };
    }
    fingerprintHistory.push(fingerprint);
    errorTextHistory.push(combinedFailures);
    // Divergence detection: oscillation, Jaccard-similar errors, diff revert.
    const divergence = detectLoopDivergence(fingerprintHistory, errorTextHistory, prevDiff, currDiff);
    if (divergence !== null) {
      emitWrapperFixLoopEvent({
        loop: 'test', iteration: attempt + 1, errorMessage: combinedFailures,
        exitReason: divergence, startedAt: iterStartedAt,
      });
      sendLog(ws, `Test fix loop divergence detected (${divergence}) — abandoning to avoid wasted iterations.\n`);
      const failureSummary = combinedFailures.slice(-8000);
      return { passed: false, testFailuresDetected: true, failureSummary };
    }
    // On retry (attempt >= 1), record what the previous Claude call
    // actually changed + the test failures that persisted, so the next
    // iteration's prompt can pivot if the same approach already failed.
    if (attempt >= 1) {
      prevAttempts.push({
        attemptNumber: attempt,
        diffStat: captureFixIterationDiffStat(),
        errorTail: combinedFailures.slice(-800),
      });
    }
    if (attempt < maxFixAttempts) {
      emitWrapperFixLoopEvent({
        loop: 'test', iteration: attempt + 1, errorMessage: combinedFailures,
        exitReason: 'in_progress', startedAt: iterStartedAt,
      });
      const preExistingHint = preExistingNote ? ' (pre-existing failures excluded)' : '';
      sendLog(ws, `${phasePrefix}Tests failed (attempt ${attempt + 1}/${totalAttempts})${preExistingHint}:\n${combinedFailures.slice(-LOG_TAIL_BYTES)}\nClaude will attempt to fix...\n`);
      const analysisSections = analysisContext ? condensedAnalysisSections(analysisContext) : '';
      const analysisPrefix = analysisSections ? `## Module Analysis\n\n${analysisSections}\n\n` : '';
      // Broader, investigation-first prompt. The previous "These tests
      // broke because of your changes" framing made Claude presume
      // self-caused regressions and miss pre-existing failures on main
      // (missing mock exports, stale fixtures, outdated imports) that
      // are just as valid to fix. Give it agency to consider either.
      //
      // The explicit "pre-existing bugs are fair game" language is
      // load-bearing: Claude Code's default disposition is to leave
      // pre-existing issues alone unless told otherwise. This task says
      // otherwise.
      const guidance = (
        `Tests are failing. Investigate and fix them.\n\n` +
        `Scope note (important): Claude Code's default is to avoid ` +
        `touching "unrelated" pre-existing bugs. That default DOES NOT ` +
        `apply here. On this task, pre-existing failures on main are ` +
        `fair game to fix. Edit test mocks, fixtures, imports, or ` +
        `implementation code wherever the root cause actually lives.\n\n` +
        `Causes to consider:\n` +
        `- Code your changes introduced (fix the code, or the test if ` +
        `it asserts the wrong thing).\n` +
        `- Pre-existing issues on main nobody has fixed yet: missing ` +
        `mock exports (e.g. "No 'foo' export is defined on the mock" ` +
        `→ add 'foo' to the mock), stale fixtures, outdated imports, ` +
        `recently-renamed functions the tests still reference.\n` +
        `- Flakiness or environmental issues (note them; don't chase).\n\n` +
        `Do NOT silence, skip, or weaken assertions to make tests pass. ` +
        `Do NOT revert your task's intent unless the test was correctly ` +
        `catching a real bug.`
      );
      // Slice from the tail: test output puts setup/boilerplate at the
      // head and the actual failure line at the end. 5000 chars is
      // enough to carry a diagnostic traceback or a handful of failed
      // assertions without drowning the agent in noise.
      const historySection = prevAttempts.length > 0 ? `${formatFixIterationHistory(prevAttempts)}\n\n` : '';
      const guidanceSuffix = attempt >= 1 ? buildFixLoopGuidanceSuffix() : '';
      const fixPrompt = baselineFailures
        ? `${historySection}${analysisPrefix}${guidance}\n\nFailures to fix:\n\n${combinedFailures.slice(-LOG_TAIL_BYTES)}${preExistingNote.slice(-1500)}${guidanceSuffix}`
        : `${historySection}${analysisPrefix}${guidance}\n\nAll test failures (baseline skipped — pre-existing failures are also fair game on this task):\n\n${combinedFailures.slice(-LOG_TAIL_BYTES)}${guidanceSuffix}`;
      await runClaudeCode(fixPrompt, ws, statusRef, model, effort);
      git(['add', '-A']);
      prevDiff = currDiff;
      currDiff = captureDiffLines();
    } else {
      emitWrapperFixLoopEvent({
        loop: 'test', iteration: attempt + 1, errorMessage: combinedFailures,
        exitReason: 'max_attempts', startedAt: iterStartedAt,
      });
      const exhaustedMessage = maxFixAttempts === 0
        ? `${phasePrefix}Tests failed during focused verification.\n`
        : `WARNING: ${phasePrefix}Tests still failing after ${maxFixAttempts} fix attempts. Proceeding with commit.\n`;
      sendLog(ws, exhaustedMessage);
      const failureSummary = combinedFailures.slice(-8000);
      return { passed: false, testFailuresDetected: true, failureSummary };
    }
  }
  return { passed: false, testFailuresDetected: true };
}

// ── Claude Code Runner (shared by all handlers) ─────────────────────────────
interface ClaudeOutputBlock { type: string; text?: string; name?: string; id?: string; input?: unknown }
interface ClaudeOutput {
  type: string;
  message?: { content: ClaudeOutputBlock[] };
  result?: string;
}

interface RunnerCommand {
  command: string;
  args: string[];
  env: Record<string, string>;
  outputMode: OutputMode;
  timeoutMs: number;
  promptInput: 'arg' | 'stdin' | 'stdin-file';
  displayName: string;
}

interface ValidationCommand {
  command: string;
  args: string[];
  env: Record<string, string>;
  timeoutMs: number;
}

interface ProviderRunner {
  provider: ProviderName;
  authMode: AuthMode;
  displayName: string;
  credentialSummary(): string;
  buildRunCommand(prompt: string, model?: string, effort?: string, disallowedTools?: string[]): RunnerCommand;
  buildValidationCommand(model?: string): ValidationCommand | null;
  missingCredentials(): string[];
}

function buildBaseCliEnv(): Record<string, string> {
  const env: Record<string, string> = {
    HOME: process.env.HOME || '/home/agent',
    PATH: process.env.PATH || '/usr/local/bin:/usr/bin:/bin',
    LANG: process.env.LANG || 'en_US.UTF-8',
    TERM: process.env.TERM || 'xterm-256color',
  };
  for (const key of [
    'CLAUDE_CODE_OAUTH_TOKEN',
    'ANTHROPIC_API_KEY',
    'OPENAI_API_KEY',
    'CODEX_API_KEY',
    'CODEX_HOME',
    'GEMINI_API_KEY',
    'GOOGLE_APPLICATION_CREDENTIALS',
    'GOOGLE_CLOUD_PROJECT',
    'GOOGLE_CLOUD_LOCATION',
    'GOOGLE_GENAI_USE_VERTEXAI',
    'ORCHESTRATOR_URL',
    'TASK_ID',
  ]) {
    const v = process.env[key];
    if (v !== undefined) env[key] = v;
  }
  if (AGENT_TOKEN) env.AGENT_API_TOKEN = AGENT_TOKEN;
  // Merge provider-settings translation env last so it can't overwrite auth
  // keys (those came from secret_material above). Extra env vars from the
  // translator extend the baseline for the subprocess.
  if (LLM_CLI_TRANSLATION?.env) {
    for (const [k, v] of Object.entries(LLM_CLI_TRANSLATION.env)) {
      if (!(k in env)) env[k] = v;
    }
  }
  return env;
}

function withExecutionModePrompt(prompt: string): string {
  const fragments: string[] = [];
  // Settings-bag prompt fragments (from backend translator) — these carry
  // fallback instructions for knobs the CLI can't express natively, like
  // the advisor tool on non-opus executors.
  if (LLM_CLI_TRANSLATION?.prompt_fragments.length) {
    fragments.push(...LLM_CLI_TRANSLATION.prompt_fragments);
  }
  // Legacy advisor mode path — retained for back-compat during rollout of
  // the settings-bag system. Deleted when the legacy execution_mode field
  // is retired in Phase E of the provider-settings redesign.
  if (LLM_EXECUTION_MODE === 'advisor' && !LLM_CLI_TRANSLATION) {
    fragments.push(
      'Execution mode: advisor.',
      'Prioritize analysis, recommendations, and risk assessment.',
      'Avoid making code changes unless the task explicitly requires implementation.',
    );
  }
  if (!fragments.length) return prompt;
  return [...fragments, '', prompt].join('\n');
}

export function getProviderRunner(provider: ProviderName = PROVIDER, authMode: AuthMode = AUTH_MODE): ProviderRunner {
  const baseEnv = buildBaseCliEnv();
  if (provider === 'openai') {
    return {
      provider,
      authMode,
      displayName: 'OpenAI Codex',
      credentialSummary: () => authMode === 'oauth' ? 'Using Codex OAuth auth.json' : 'Using OPENAI/CODEX API key auth',
      missingCredentials: () => {
        if (authMode === 'oauth') return baseEnv.CODEX_HOME ? [] : ['CODEX_HOME'];
        return (baseEnv.OPENAI_API_KEY || baseEnv.CODEX_API_KEY) ? [] : ['OPENAI_API_KEY or CODEX_API_KEY'];
      },
      buildRunCommand: (_prompt, model, effort) => {
        // --skip-git-repo-check: some task types run outside a git checkout
        //   (standalone agentic handlers); without this flag Codex refuses
        //   to start.
        // Sandbox/approval flags intentionally NOT set here — the backend's
        //   settings translation emits them (typically `-s danger-full-access`
        //   or the canonical `--dangerously-bypass-approvals-and-sandbox`).
        //   The VM itself is the isolation boundary. Do NOT pass `--full-auto`:
        //   it aliases `--sandbox workspace-write` which would override a
        //   bypass and leave sandbox behavior undefined.
        // --json: emit JSONL events instead of human-formatted output. Required
        // for tool-call telemetry — without it, Codex tasks are a black box
        // (last_tool_use_at stays NULL because recordToolUse never fires).
        // 2026-05-08 incident: the agent_stalled watchdog falsely reaped
        // healthy Codex work because we couldn't see its tool calls.
        const args = ['exec', '--skip-git-repo-check', '--json'];
        if (LLM_CLI_TRANSLATION) {
          args.push(...LLM_CLI_TRANSLATION.args);
        } else {
          // Legacy fallback path (no settings-bag from backend): emit the
          // canonical bypass flag — disables both approval policy and
          // sandbox in one knob. Equivalent to Claude's
          // --dangerously-skip-permissions and documented for externally
          // hardened environments.
          args.push('--dangerously-bypass-approvals-and-sandbox');
          if (model) args.push('-m', model);
        }
        return {
          command: 'codex',
          args,
          env: baseEnv,
          outputMode: 'codex-jsonl',
          timeoutMs: claudeTimeoutForEffort(effort),
          promptInput: 'stdin',
          displayName: 'OpenAI Codex',
        };
      },
      buildValidationCommand: (model) => {
        // Validation runs in a non-git cwd (e.g. /tmp or /opt/agent), so
        // --skip-git-repo-check is mandatory. Sandbox hardcoded to
        // danger-full-access for the same VM-is-sandbox reasoning as the
        // run command.
        const args = ['exec', '--skip-git-repo-check', '-s', 'danger-full-access'];
        if (model) args.push('-m', model);
        args.push('Respond with OK.');
        return { command: 'codex', args, env: baseEnv, timeoutMs: 30_000 };
      },
    };
  }
  if (provider === 'gemini') {
    return {
      provider,
      authMode,
      displayName: 'Gemini CLI',
      credentialSummary: () => authMode === 'service_account' ? 'Using Gemini Vertex service account auth' : 'Using GEMINI_API_KEY auth',
      missingCredentials: () => {
        if (authMode === 'service_account') return baseEnv.GOOGLE_APPLICATION_CREDENTIALS ? [] : ['GOOGLE_APPLICATION_CREDENTIALS'];
        return baseEnv.GEMINI_API_KEY ? [] : ['GEMINI_API_KEY'];
      },
      buildRunCommand: (_prompt, model, effort) => {
        // --approval-mode and -o are baseline flags the backend translation
        // won't emit; keep them here. Settings-bag translation carries -m
        // (alias if thinking/temp are configured, else raw model) plus any
        // sandbox/approval overrides the operator set explicitly.
        // Gemini needs TWO knobs — approval-mode and sandbox are separate:
        //   --approval-mode=yolo      auto-approves tool calls
        //   GEMINI_SANDBOX=false      disables the outer docker/podman
        //                             sandbox that YOLO would auto-enable
        //                             (also turns off the workspace path
        //                             gate that blocks writes to /tmp/*).
        // The VM itself is the isolation boundary, so both are safe.
        const args = ['-o', 'text'];
        const translationArgs = LLM_CLI_TRANSLATION?.args ?? [];
        const hasApprovalOverride = translationArgs.some(a => typeof a === 'string' && a.startsWith('--approval-mode'));
        if (!hasApprovalOverride) args.unshift('--approval-mode=yolo');
        if (LLM_CLI_TRANSLATION) {
          args.push(...translationArgs);
        } else if (model) {
          args.push('-m', model);
        }
        // GEMINI_CLI_TRUST_WORKSPACE=true bypasses the trusted-directory check
        // that otherwise downgrades --approval-mode=yolo to "default" (which
        // then prompts and stalls). The VM is the isolation boundary so the
        // workspace trust check is redundant here.
        const geminiEnv = { ...baseEnv, GEMINI_SANDBOX: 'false', GEMINI_CLI_TRUST_WORKSPACE: 'true' };
        return {
          command: 'gemini',
          args,
          env: geminiEnv,
          outputMode: 'text',
          timeoutMs: claudeTimeoutForEffort(effort),
          promptInput: 'stdin',
          displayName: 'Gemini CLI',
        };
      },
      buildValidationCommand: (model) => {
        const args = ['-o', 'text', '-p', 'Respond with OK.'];
        if (model) args.push('-m', model);
        // GEMINI_CLI_TRUST_WORKSPACE=true bypasses the trusted-directory check
        // that otherwise downgrades --approval-mode=yolo to "default" (which
        // then prompts and stalls). The VM is the isolation boundary so the
        // workspace trust check is redundant here.
        const geminiEnv = { ...baseEnv, GEMINI_SANDBOX: 'false', GEMINI_CLI_TRUST_WORKSPACE: 'true' };
        return { command: 'gemini', args, env: geminiEnv, timeoutMs: 30_000 };
      },
    };
  }
  return {
    provider: 'anthropic',
    authMode,
    displayName: 'Claude Code',
    credentialSummary: () => baseEnv.CLAUDE_CODE_OAUTH_TOKEN ? 'Using OAuth token for Claude Code auth' : 'Using ANTHROPIC_API_KEY as fallback auth',
    missingCredentials: () => (baseEnv.CLAUDE_CODE_OAUTH_TOKEN || baseEnv.ANTHROPIC_API_KEY) ? [] : ['CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY'],
    buildRunCommand: (_prompt, model, effort, disallowedTools) => {
      const args = ['-p', '--output-format', 'stream-json', '--verbose', '--dangerously-skip-permissions'];
      // Prefer settings-bag translation when present — it already contains
      // --model, --effort, --betas, --permission-mode, --fallback-model, etc.
      // Fall back to legacy model/effort params only when no translation is
      // available (older backend deploys or missing settings).
      let effectiveEffort = effort;
      if (LLM_CLI_TRANSLATION) {
        args.push(...LLM_CLI_TRANSLATION.args);
        // Pick up effort from the translation for timeout computation.
        const idx = LLM_CLI_TRANSLATION.args.indexOf('--effort');
        if (idx >= 0 && idx + 1 < LLM_CLI_TRANSLATION.args.length) {
          effectiveEffort = LLM_CLI_TRANSLATION.args[idx + 1];
        }
      } else {
        if (model) args.push('--model', model);
        if (effort) args.push('--effort', effort);
      }
      if (disallowedTools && disallowedTools.length > 0) {
        for (const tool of disallowedTools) args.push('--disallowedTools', tool);
      }
      return {
        command: 'claude',
        args,
        env: baseEnv,
        outputMode: 'claude-stream-json',
        timeoutMs: claudeTimeoutForEffort(effectiveEffort),
        promptInput: 'stdin-file',
        displayName: 'Claude Code',
      };
    },
    buildValidationCommand: () => ({
      command: 'claude',
      args: ['-p', 'respond with OK', '--max-turns', '1', '--output-format', 'stream-json', '--dangerously-skip-permissions'],
      env: baseEnv,
      timeoutMs: 30_000,
    }),
  };
}

async function validateProviderAuth(runner: ProviderRunner, reportPreClaimFailure: (reason: string) => void, model?: string): Promise<void> {
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
    const combined = `${result.stderr}\n${result.stdout}`;
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

export async function runClaudeCode(prompt: string, ws: WebSocket | null, statusRef: { value: string }, model?: string, effort?: string, disallowedTools?: string[], enableCheckWatchdog?: boolean): Promise<{
  exitCode: number; allOutput: string; lastResultText: string;
}> {
  return runProviderAgent(prompt, ws, statusRef, model, effort, disallowedTools, enableCheckWatchdog);
}

export async function runProviderAgent(prompt: string, ws: WebSocket | null, statusRef: { value: string }, model?: string, effort?: string, disallowedTools?: string[], enableCheckWatchdog?: boolean): Promise<{
  exitCode: number; allOutput: string; lastResultText: string;
}> {
  const runner = getProviderRunner();
  const runCommand = runner.buildRunCommand(prompt, model, effort, disallowedTools);
  let allOutput = '';
  let lastResultText = '';
  let stderrBuffer = '';
  // Wrap mutable flags in an object so TS doesn't narrow them to `false` after
  // the Promise resolves — the close/timeout callbacks mutate them inside the
  // Promise scope, and TS's flow analysis can't see across the await boundary.
  const flags: { timedOut: boolean; providerKilledByOOM: boolean; stalledMidStream: boolean } = { timedOut: false, providerKilledByOOM: false, stalledMidStream: false };
  // Mid-stream stall tracking: set when a tool_use block is seen, cleared when
  // the corresponding tool-result (user event) arrives.
  let lastToolUseAt: number | null = null;
  let toolInFlight = false;
  // Check-runaway watchdog: tracks allOutput offset when a ./check tool_use fires
  // so the outcome can be parsed from the subsequent user (tool_result) event.
  let lastCheckOutputOffset = -1;

  sendLog(ws, `\n--- ${runner.displayName} starting ---\nPrompt: ${prompt.slice(0, 200)}${prompt.length > 200 ? '...' : ''}\nMode: ${LLM_EXECUTION_MODE}\n\n`);
  log(`Spawning ${runner.displayName} CLI...`);

  // Write ./note helper for agent annotations (available to all task types)
  try {
    if (fs.existsSync(WORKSPACE)) {
      const noteScript = `#!/bin/bash
# Agent annotation tool — leave comments, issues, or vision suggestions
# Usage:
#   ./note comment "This module uses deprecated API" --page systems/sys-auth
#   ./note issue "Tests fail on large repos" --severity high
#   ./note vision "Consider adding agent reliability metrics"

TYPE="\${1:?Usage: note comment|issue|vision <content> [--page <key>] [--severity low|medium|high]}"
shift

CONTENT=""
PAGE=""
SEVERITY=""

while [ $# -gt 0 ]; do
  case "$1" in
    --page) PAGE="$2"; shift 2 ;;
    --severity) SEVERITY="$2"; shift 2 ;;
    *) CONTENT="\${CONTENT:+$CONTENT }$1"; shift ;;
  esac
done

if [ -z "$CONTENT" ]; then
  echo "Error: content is required" >&2; exit 1
fi

API="\${ORCHESTRATOR_URL:?ORCHESTRATOR_URL not set}"
TOKEN="\${AGENT_API_TOKEN:?AGENT_API_TOKEN not set}"
TASK="\${TASK_ID:?TASK_ID not set}"
CURL_OPTS="-f -sS --max-time 15 --connect-timeout 10"

# Build JSON with printf to avoid shell expansion issues
JSON="{\\"task_id\\":\\"$TASK\\",\\"annotation_type\\":\\"$TYPE\\",\\"content\\":\\"$(echo "$CONTENT" | sed 's/\\\\/\\\\\\\\/g; s/"/\\\\"/g')\\""
[ -n "$PAGE" ] && JSON="\${JSON},\\"target_page\\":\\"$PAGE\\""
[ -n "$SEVERITY" ] && JSON="\${JSON},\\"severity\\":\\"$SEVERITY\\""
JSON="\${JSON}}"

printf '%s' "$JSON" | \\
  curl $CURL_OPTS -X POST "$API/api/agent/annotate" \\
    -H "Content-Type: application/json" \\
    -H "X-Agent-Token: $TOKEN" \\
    --data-binary @-
echo ""
`;
      fs.writeFileSync(`${WORKSPACE}/note`, noteScript, { mode: 0o755 });
      // Exclude agent helper scripts from git so they don't get committed into PRs
      try {
        const excludePath = `${WORKSPACE}/.git/info/exclude`;
        const existing = fs.existsSync(excludePath) ? fs.readFileSync(excludePath, 'utf-8') : '';
        if (!existing.includes('/note')) {
          fs.appendFileSync(excludePath, '/note\n');
        }
      } catch { /* shallow clone may lack .git/info */ }
    }
  } catch { /* workspace may not exist yet for some handlers */ }

  const exitCode = await new Promise<number>((resolve) => {
    let promptFile = '';
    let promptFd: number | undefined;
    let child = null as ReturnType<typeof spawn> | null;

    if (runCommand.promptInput === 'stdin-file') {
      // Write prompt to a temp file and pipe via stdin to avoid E2BIG when
      // the prompt (design doc + project context + directions) exceeds ARG_MAX (~128KB).
      promptFile = `/tmp/agent_prompt_${process.env.TASK_ID || 'unknown'}.txt`;
      writePrivateFileSync(promptFile, withExecutionModePrompt(prompt));
      promptFd = fs.openSync(promptFile, 'r');
    }

    child = spawn(runCommand.command, runCommand.args, {
      cwd: WORKSPACE,
      env: runCommand.env,
      detached: true,
      stdio: [runCommand.promptInput === 'stdin' ? 'pipe' : (promptFd ?? 'ignore'), 'pipe', 'pipe'],
    });
    // Per-task LLM telemetry (efficiency direction d-91d8dede): one CLI
    // invocation = one llm_call_count tick. Token totals are added when the
    // runner emits a `result` event with a usage block.
    recordLlmCallStart();
    // Phase 1: register the child's PID so the reset routine can SIGKILL
    // it between roles if it lingers. Unregistered on close. We track the
    // leader PID; the timeout path already kills the whole process group
    // via process.kill(-pid).
    if (typeof child.pid === 'number') {
      registerChildPid(child.pid);
    }
    if (promptFd !== undefined) fs.closeSync(promptFd);
    if (runCommand.promptInput === 'stdin') {
      try {
        child.stdin?.end(withExecutionModePrompt(prompt));
      } catch {
        // If the child exits immediately, the close path will surface the real failure.
      }
    }

    const timeoutMs = runCommand.timeoutMs;
    const timeoutMinutes = timeoutMs / 60000;
    let forceKillTimer: NodeJS.Timeout | null = null;
    // Post-result grace timer: armed when claude-code emits its structured
    // `result` event. If the subprocess doesn't exit within
    // CLAUDE_RESULT_GRACE_MS, send SIGTERM so the wrapper can proceed to
    // commit/push/PR rather than waiting the full wall-clock timeout.
    // This is NOT a timeout (flags.timedOut stays false) — we got a clean
    // result event, the model finished, the work product is in the diff.
    let resultGraceTimer: NodeJS.Timeout | null = null;
    // Mid-stream stall watchdog: resets on every stdout chunk; fires if stdout
    // goes silent for CLAUDE_STREAM_STALL_MS while the process is still alive
    // (e.g. hung tool call). Uses the same SIGTERM→SIGKILL escalation as
    // resultGraceTimer; flags.timedOut stays false so post-Claude phase runs.
    let stallWatchdog: NodeJS.Timeout | null = null;
    const originalPid = child.pid;
    const processGroupStillOwned = () =>
      originalPid !== undefined
      && child.pid === originalPid
      && child.exitCode === null
      && child.signalCode === null;
    const resetStallWatchdog = () => {
      if (stallWatchdog) clearTimeout(stallWatchdog);
      stallWatchdog = setTimeout(() => {
        if (!processGroupStillOwned()) return;
        sendLog(
          ws,
          `\n[runtime] stream-stall: no stdout for >${CLAUDE_STREAM_STALL_MS / 1000}s — `
            + `sending SIGTERM so wrapper can proceed to commit/push.\n`,
        );
        const pid = originalPid;
        if (pid !== undefined) {
          try { process.kill(-pid, 'SIGTERM'); } catch { /* ignore */ }
          forceKillTimer = setTimeout(() => {
            if (!processGroupStillOwned()) return;
            try { process.kill(-pid, 'SIGKILL'); } catch { /* ignore */ }
          }, PROCESS_KILL_GRACE_MS);
        }
      }, CLAUDE_STREAM_STALL_MS);
    };
    // Arm eagerly so pre-data silence is also guarded.
    resetStallWatchdog();
    const killTimer = setTimeout(() => {
      log(`${runCommand.displayName} process timed out after ${timeoutMinutes} minutes — killing process group`);
      sendLog(ws, `\n[TIMEOUT] ${runCommand.displayName} exceeded ${timeoutMinutes}-minute time limit\n`);
      flags.timedOut = true;
      const pid = originalPid;
      if (pid !== undefined && processGroupStillOwned()) {
        try { process.kill(-pid, 'SIGTERM'); } catch { /* ignore */ }
        forceKillTimer = setTimeout(() => {
          if (!processGroupStillOwned()) return;
          try { process.kill(-pid, 'SIGKILL'); } catch { /* ignore */ }
        }, PROCESS_KILL_GRACE_MS);
      }
    }, timeoutMs);

    // Stall watchdog: fires every 60s to detect tool_use events that never
    // receive a tool-result (user) event back from claude-code. This catches
    // hangs that occur mid-tool rather than post-result.
    let stallWatchdogTimer: NodeJS.Timeout | null = setInterval(() => {
      if (
        toolInFlight &&
        lastToolUseAt !== null &&
        Date.now() - lastToolUseAt > TOOL_USE_STALL_TIMEOUT_MS &&
        processGroupStillOwned()
      ) {
        sendLog(
          ws,
          `\n[runtime] tool_use stall: tool in-flight for >${TOOL_USE_STALL_TIMEOUT_MS / 60000} min without result — sending SIGTERM\n`,
        );
        flags.stalledMidStream = true;
        if (stallWatchdogTimer) { clearInterval(stallWatchdogTimer); stallWatchdogTimer = null; }
        const pid = originalPid;
        if (pid !== undefined) {
          try { process.kill(-pid, 'SIGTERM'); } catch { /* ignore */ }
          forceKillTimer = setTimeout(() => {
            if (!processGroupStillOwned()) return;
            try { process.kill(-pid, 'SIGKILL'); } catch { /* ignore */ }
          }, PROCESS_KILL_GRACE_MS);
        }
      }
    }, 60_000);

    // Check-runaway watchdog: polls every 60s to detect Claude-driven ./check
    // loops that spin without convergence. Only active for code-change flows
    // (enableCheckWatchdog). Disable via DISABLE_CHECK_RUNAWAY_WATCHDOG=1.
    let checkRunawayWatchdog: NodeJS.Timeout | null = null;
    if (enableCheckWatchdog && !process.env.DISABLE_CHECK_RUNAWAY_WATCHDOG) {
      checkRunawayWatchdog = setInterval(() => {
        if (!processGroupStillOwned()) {
          if (checkRunawayWatchdog) { clearInterval(checkRunawayWatchdog); checkRunawayWatchdog = null; }
          return;
        }
        const { totalChecks, shouldTrigger } = getCheckRunawayStatus(
          CHECK_RUNAWAY_MIN_INVOCATIONS, CHECK_RUNAWAY_CONSECUTIVE_FAILURES,
        );
        let triggerReason: string | null = null;
        if (totalChecks >= MAX_CLAUDE_CHECK_INVOCATIONS) {
          triggerReason = `hard ceiling: ${totalChecks}/${MAX_CLAUDE_CHECK_INVOCATIONS} ./check calls in one session`;
        } else if (shouldTrigger) {
          triggerReason = `${CHECK_RUNAWAY_CONSECUTIVE_FAILURES} consecutive ./check failures after ${totalChecks} calls`;
        }
        if (triggerReason === null) return;
        sendLog(ws, `\n[runtime] check-runaway watchdog: ${triggerReason} — sending SIGTERM\n`);
        if (checkRunawayWatchdog) { clearInterval(checkRunawayWatchdog); checkRunawayWatchdog = null; }
        const pid = originalPid;
        if (pid !== undefined) {
          try { process.kill(-pid, 'SIGTERM'); } catch { /* ignore */ }
          forceKillTimer = setTimeout(() => {
            if (!processGroupStillOwned()) return;
            try { process.kill(-pid, 'SIGKILL'); } catch { /* ignore */ }
          }, PROCESS_KILL_GRACE_MS);
        }
      }, 60_000);
    }

    if (!child.stdout || !child.stderr) {
      throw new Error('spawn returned no stdio streams');
    }
    // Stream-json chunks are processed via a serial async queue with event-
    // loop yields between chunks. Earlier this work was synchronous inside
    // the data callback, which on heavy bursts (e.g. test-backend.sh emitting
    // dozens of large stream-json events back-to-back) saturated the agent's
    // main thread with JSON.parse work. setInterval timers (heartbeat, stall
    // watchdogs) couldn't fire, the reaper then killed the VM as silent-
    // death even though the agent was still making progress. The fix: push
    // chunks to a queue, drain serially with `await setImmediate` between
    // each so timers get a chance to fire.
    const pendingJsonChunks: string[] = [];
    let drainingJsonChunks = false;
    // Yield every WITHIN_CHUNK_YIELD_LINES parsed lines OR every
    // WITHIN_CHUNK_YIELD_MS of wall time, whichever fires first. Tunables in
    // constants.ts. Pre-parse yield (BEFORE next JSON.parse, not after) bounds
    // event-loop blocking to one parse + dispatch even when a single line is
    // a 1-2 MB tool_use payload. Adversarial review 2026-05-04: post-parse
    // yield was bounded by `single_line_parse_time × N`, not by wall time —
    // a pathological huge line could block past the bound.
    // Force a yield before parsing if a chunk is byte-fat — even a single huge
    // line (e.g. a tool_use/tool_result event with a 1-2 MB inline payload, the
    // worst-case shape stream-json can deliver) must not block past the wall-
    // time bound on its first parse. Without this, the per-chunk first-line
    // case bypasses the line/wall-time bounds (linesSinceYield=0, lastYieldAt=
    // now). Adversarial review 2026-05-04 R3.
    const HEAVY_CHUNK_BYTES = 256 * 1024;
    const drainJsonChunks = async () => {
      if (drainingJsonChunks) return;
      drainingJsonChunks = true;
      try {
        while (pendingJsonChunks.length > 0) {
          const chunk = pendingJsonChunks.shift();
          if (chunk === undefined) break;
          if (chunk.length >= HEAVY_CHUNK_BYTES) {
            await new Promise(r => setImmediate(r));
          }
          let linesSinceYield = 0;
          let lastYieldAt = Date.now();
          for (const line of chunk.split('\n').filter(Boolean)) {
            // Pre-parse yield: if the previous iteration crossed the line-count
            // OR wall-time bound, yield NOW before parsing the next line. This
            // bounds event-loop blocking to (one JSON.parse + one dispatch),
            // not (N × parse + dispatch).
            if (linesSinceYield >= WITHIN_CHUNK_YIELD_LINES
                || Date.now() - lastYieldAt >= WITHIN_CHUNK_YIELD_MS) {
              await new Promise(r => setImmediate(r));
              linesSinceYield = 0;
              lastYieldAt = Date.now();
            }
            let obj: ClaudeOutput | null = null;
            try {
              obj = JSON.parse(line) as ClaudeOutput;
            } catch {
              sendLog(ws, chunk);
              continue;
            }
            // Audit 2026-04-25 (W4/C3): explicit type dispatch instead of
            // silent fall-through. Previously a `type: 'error'` message was
            // logged as raw text and then dropped — `lastResultText` stayed
            // stale from earlier output and the handler would return success
            // with the WRONG result text. Now an error-type message captures
            // the error into stderr buffer so downstream classification sees it.
            const objType = (obj as { type?: string } | null)?.type;
            if (objType === 'assistant' && obj.message?.content) {
              for (const block of obj.message.content) {
                if (block.type === 'text') {
                  sendLog(ws, (block.text ?? '') + '\n');
                  statusRef.value = (block.text ?? '').slice(0, 100);
                } else if (block.type === 'tool_use') {
                  sendLog(ws, `[tool: ${strVal(block.name)}] ${JSON.stringify(block.input).slice(0, 200)}\n`);
                  statusRef.value = `Using tool: ${strVal(block.name)}`;
                  recordToolUse(strVal(block.name), block.input);
                  toolInFlight = true;
                  lastToolUseAt = Date.now();
                  if (enableCheckWatchdog && strVal(block.name) === 'Bash') {
                    const inputPreview = JSON.stringify(block.input).slice(0, 200);
                    if (isCheckInvocation(inputPreview)) {
                      lastCheckOutputOffset = allOutput.length;
                    }
                  }
                }
              }
            } else if (objType === 'result') {
              lastResultText = (obj as { result?: string }).result ?? '';
              sendLog(ws, `\n--- Result ---\n${lastResultText}\n`);
              // Per-task token telemetry (efficiency direction d-91d8dede).
              // Claude stream-json result events carry a `usage` block with
              // input_tokens / output_tokens (cache hit/creation tokens
              // optional). Sum cache tokens into input so the heartbeat
              // counter reflects total tokens billed against the account.
              const usage = (obj as { usage?: Record<string, unknown> }).usage;
              if (usage && typeof usage === 'object') {
                const inputTokens = (
                  Number(usage.input_tokens ?? 0)
                  + Number(usage.cache_creation_input_tokens ?? 0)
                  + Number(usage.cache_read_input_tokens ?? 0)
                );
                const outputTokens = Number(usage.output_tokens ?? 0);
                recordLlmTokens(inputTokens, outputTokens);
              }
              // Arm the post-result grace timer so a hung subprocess doesn't
              // burn the wall-clock budget. Only set it once — extra result
              // events (defensive — Claude Code shouldn't emit multiple) don't
              // reset the timer.
              if (resultGraceTimer === null && processGroupStillOwned()) {
                resultGraceTimer = setTimeout(() => {
                  if (!processGroupStillOwned()) return;
                  sendLog(
                    ws,
                    `\n[runtime] result event received >${CLAUDE_RESULT_GRACE_MS / 1000}s ago without process exit — `
                      + `sending SIGTERM so wrapper can proceed to commit/push.\n`,
                  );
                  const pid = originalPid;
                  if (pid !== undefined) {
                    try { process.kill(-pid, 'SIGTERM'); } catch { /* ignore */ }
                    forceKillTimer = setTimeout(() => {
                      if (!processGroupStillOwned()) return;
                      try { process.kill(-pid, 'SIGKILL'); } catch { /* ignore */ }
                    }, PROCESS_KILL_GRACE_MS);
                  }
                }, CLAUDE_RESULT_GRACE_MS);
              }
            } else if (objType === 'error') {
              // Surface explicit error messages from the runner so failure
              // classification & status reporting see them.
              const errMsg = (obj as { message?: string; error?: string }).message
                || (obj as { error?: string }).error
                || JSON.stringify(obj);
              const errLine = `[claude-stream error] ${errMsg}\n`;
              sendLog(ws, errLine);
              // Push into stderr buffers (analogous to the stderr handler below)
              // so combined-output classification picks it up.
              if (allOutput.length < MAX_OUTPUT_BYTES) allOutput += errLine;
              stderrBuffer = appendBoundedTail(stderrBuffer, errLine, STDERR_TAIL_BYTES);
            } else if (objType === 'system' || objType === 'user') {
              // user event carries tool results back to the model — tool is no longer in-flight.
              if (objType === 'user') {
                toolInFlight = false;
                // If a ./check call was pending, parse its outcome from the JSON in
                // allOutput since we set lastCheckOutputOffset. The bash stdout flows
                // through the user event's JSON content, so [check].*FAILED appears
                // as a substring in the JSON-encoded tool_result text.
                if (enableCheckWatchdog && lastCheckOutputOffset >= 0) {
                  const slice = allOutput.slice(lastCheckOutputOffset);
                  recordCheckOutcome(!/\[check\].*FAILED/.test(slice));
                  lastCheckOutputOffset = -1;
                }
              }
            } else if (objType) {
              // Unknown type: log so we don't silently drop information.
              sendLog(ws, `[claude-stream unknown type: ${objType}]\n`);
            }
            linesSinceYield++;
          }
          // Yield to the event loop after each chunk so heartbeat / stall-
          // watchdog timers get a chance to fire between bursts.
          await new Promise(r => setImmediate(r));
        }
      } finally {
        drainingJsonChunks = false;
      }
    };
    // Codex JSONL parser (mirrors drainJsonChunks). Codex's --json output uses
    // a different event taxonomy than Claude (thread.started / turn.* / item.*
    // with item subtypes command_execution / file_change / mcp_tool_call /
    // web_search / agent_message / reasoning / todo_list / error). Each
    // tool-bearing item subtype maps to recordToolUse so last_tool_use_at gets
    // populated — without this, every Codex task looks structurally stalled
    // (2026-05-08 incident: false-reaped healthy Codex work).
    const pendingCodexChunks: string[] = [];
    let drainingCodexChunks = false;
    let codexCarry = '';
    const _codexToolNameForItem = (item: unknown): string | null => {
      if (!item || typeof item !== 'object') return null;
      const itemType = (item as { type?: string }).type;
      switch (itemType) {
        case 'command_execution':
          return 'Bash';
        case 'file_change':
          return 'apply_patch';
        case 'mcp_tool_call': {
          const t = (item as { tool?: string }).tool;
          return typeof t === 'string' && t ? `MCP:${t}` : 'MCP';
        }
        case 'web_search':
          return 'WebSearch';
        case 'todo_list':
          return 'TodoWrite';
        default:
          return null;
      }
    };
    const _codexItemPreview = (item: unknown): string => {
      if (!item || typeof item !== 'object') return '';
      const it = item as Record<string, unknown>;
      switch (it.type) {
        case 'command_execution':
          return typeof it.command === 'string' ? it.command : '';
        case 'file_change': {
          const changes = Array.isArray(it.changes) ? it.changes : [];
          return `${changes.length} file change(s)`;
        }
        case 'mcp_tool_call':
          return JSON.stringify(it.arguments ?? {}).slice(0, 200);
        case 'web_search':
          return typeof it.query === 'string' ? it.query : '';
        case 'agent_message':
        case 'reasoning':
          return typeof it.text === 'string' ? it.text.slice(0, 200) : '';
        default:
          return '';
      }
    };
    const drainCodexChunks = async () => {
      if (drainingCodexChunks) return;
      drainingCodexChunks = true;
      try {
        while (pendingCodexChunks.length > 0) {
          const chunk = pendingCodexChunks.shift();
          if (chunk === undefined) break;
          if (chunk.length >= HEAVY_CHUNK_BYTES) {
            await new Promise(r => setImmediate(r));
          }
          // Carry handles partial-line splits across data events.
          const combined = codexCarry + chunk;
          const lastNewline = combined.lastIndexOf('\n');
          const completeChunk = lastNewline >= 0 ? combined.slice(0, lastNewline) : '';
          codexCarry = lastNewline >= 0 ? combined.slice(lastNewline + 1) : combined;
          let linesSinceYield = 0;
          let lastYieldAt = Date.now();
          for (const line of completeChunk.split('\n')) {
            if (!line) continue;
            if (linesSinceYield >= WITHIN_CHUNK_YIELD_LINES
                || Date.now() - lastYieldAt >= WITHIN_CHUNK_YIELD_MS) {
              await new Promise(r => setImmediate(r));
              linesSinceYield = 0;
              lastYieldAt = Date.now();
            }
            // Codex emits non-JSON ERROR log lines to stdout when auth fails
            // (`2026-05-08T... ERROR codex_login: ...`). Skip them quietly so
            // they don't pollute the parse stream — they're already on stderr.
            if (!line.startsWith('{')) {
              sendLog(ws, line + '\n');
              continue;
            }
            let obj: Record<string, unknown> | null = null;
            try {
              obj = JSON.parse(line) as Record<string, unknown>;
            } catch {
              sendLog(ws, line + '\n');
              continue;
            }
            linesSinceYield += 1;
            const eventType = typeof obj.type === 'string' ? obj.type : '';
            if (eventType === 'thread.started') {
              const tid = typeof obj.thread_id === 'string' ? obj.thread_id : '';
              sendLog(ws, `[codex] thread.started ${tid}\n`);
            } else if (eventType === 'turn.started') {
              sendLog(ws, '[codex] turn.started\n');
              statusRef.value = 'Codex thinking';
            } else if (eventType === 'turn.completed') {
              sendLog(ws, '[codex] turn.completed\n');
              // Codex turn.completed may carry a usage block analogous to
              // Claude's result-event usage. Best-effort — older Codex
              // versions omit it, in which case the counter stays put.
              const usage = obj.usage as Record<string, unknown> | undefined;
              if (usage && typeof usage === 'object') {
                const inputTokens = (
                  Number(usage.input_tokens ?? 0)
                  + Number(usage.cache_creation_input_tokens ?? 0)
                  + Number(usage.cache_read_input_tokens ?? 0)
                );
                const outputTokens = Number(usage.output_tokens ?? 0);
                recordLlmTokens(inputTokens, outputTokens);
              }
            } else if (eventType === 'turn.failed') {
              const err = (obj.error as { message?: string } | undefined)?.message ?? '';
              const oneLine = `[codex] turn.failed: ${err}\n`;
              sendLog(ws, oneLine);
              stderrBuffer = appendBoundedTail(stderrBuffer, oneLine, STDERR_TAIL_BYTES);
            } else if (eventType === 'error') {
              const msg = typeof obj.message === 'string' ? obj.message : '';
              const oneLine = `[codex] error: ${msg}\n`;
              sendLog(ws, oneLine);
              stderrBuffer = appendBoundedTail(stderrBuffer, oneLine, STDERR_TAIL_BYTES);
            } else if (
              eventType === 'item.started'
              || eventType === 'item.updated'
              || eventType === 'item.completed'
            ) {
              const item = obj.item as Record<string, unknown> | undefined;
              const itemType = item && typeof item.type === 'string' ? item.type : '';
              const preview = _codexItemPreview(item);
              const toolName = _codexToolNameForItem(item);
              if (toolName) {
                // Tool-bearing items: stamp tool use on item.started so the
                // watchdog sees fresh activity early. item.updated also stamps
                // (covers long-running shell exec). item.completed clears
                // toolInFlight so the stall watchdog stops counting.
                if (eventType === 'item.started' || eventType === 'item.updated') {
                  sendLog(
                    ws,
                    `[tool: ${toolName}] ${preview.slice(0, 200)}\n`,
                  );
                  statusRef.value = `Using tool: ${toolName}`;
                  recordToolUse(toolName, item ?? {});
                  toolInFlight = true;
                  lastToolUseAt = Date.now();
                  if (
                    enableCheckWatchdog
                    && toolName === 'Bash'
                    && isCheckInvocation(preview)
                  ) {
                    lastCheckOutputOffset = allOutput.length;
                  }
                } else {
                  toolInFlight = false;
                  const status = item && typeof item.status === 'string' ? item.status : '';
                  sendLog(ws, `[tool: ${toolName}] ${status}\n`);
                }
              } else if (itemType === 'agent_message') {
                if (eventType === 'item.completed' || eventType === 'item.updated') {
                  sendLog(ws, preview + '\n');
                  if (preview) statusRef.value = preview.slice(0, 100);
                  if (eventType === 'item.completed') {
                    lastResultText = preview;
                  }
                }
              } else if (itemType === 'reasoning') {
                // Don't count reasoning as tool use — it's the model thinking.
                // But still update status so heartbeats show progress.
                if (eventType === 'item.completed') {
                  statusRef.value = `Reasoning: ${preview.slice(0, 80)}`;
                }
              } else if (itemType === 'error') {
                const msg = item && typeof item.message === 'string' ? item.message : '';
                const oneLine = `[codex] item error: ${msg}\n`;
                sendLog(ws, oneLine);
                stderrBuffer = appendBoundedTail(stderrBuffer, oneLine, STDERR_TAIL_BYTES);
              }
            }
          }
          await new Promise(r => setImmediate(r));
        }
      } finally {
        drainingCodexChunks = false;
      }
    };
    child.stdout.on('data', (data: Buffer) => {
      resetStallWatchdog();
      const text = data.toString();
      if (allOutput.length < MAX_OUTPUT_BYTES) allOutput += text;
      if (runCommand.outputMode === 'claude-stream-json') {
        pendingJsonChunks.push(text);
        drainJsonChunks().catch((e: unknown) => {
          sendLog(ws, `[runtime] drainJsonChunks error: ${String(e)}\n`);
        });
      } else if (runCommand.outputMode === 'codex-jsonl') {
        pendingCodexChunks.push(text);
        drainCodexChunks().catch((e: unknown) => {
          sendLog(ws, `[runtime] drainCodexChunks error: ${String(e)}\n`);
        });
      } else {
        sendLog(ws, text);
        const trimmed = text.trim();
        if (trimmed) {
          lastResultText = `${lastResultText}${text}`.trim();
          statusRef.value = trimmed.slice(0, 100);
        }
      }
    });

    child.stderr.on('data', (data: Buffer) => {
      const text = sanitize(data.toString());
      if (allOutput.length < MAX_OUTPUT_BYTES) allOutput += text;
      stderrBuffer = appendBoundedTail(stderrBuffer, text, STDERR_TAIL_BYTES);
      sendLog(ws, `[stderr] ${text}`);
    });

    // Wait for any pending stream-json chunks to drain before resolving so a
    // final `result`/`error` event in the queue isn't silently lost — the
    // serial drainer yields between chunks via setImmediate, and the close
    // event can fire before the drainer finishes the last chunk.
    const drainPendingThenResolve = async (resolveValue: number) => {
      try {
        if (pendingJsonChunks.length > 0 || drainingJsonChunks) {
          await drainJsonChunks();
        }
        if (pendingCodexChunks.length > 0 || drainingCodexChunks) {
          await drainCodexChunks();
        }
      } catch (e) {
        sendLog(ws, `[runtime] final drain error: ${String(e)}\n`);
      }
      resolve(resolveValue);
    };
    child.on('error', (err) => {
      clearTimeout(killTimer);
      if (forceKillTimer) clearTimeout(forceKillTimer);
      if (resultGraceTimer) clearTimeout(resultGraceTimer);
      if (stallWatchdog) clearTimeout(stallWatchdog);
      if (stallWatchdogTimer) { clearInterval(stallWatchdogTimer); stallWatchdogTimer = null; }
      if (checkRunawayWatchdog) { clearInterval(checkRunawayWatchdog); checkRunawayWatchdog = null; }
      if (typeof originalPid === 'number') {
        unregisterChildPid(originalPid);
      }
      sendLog(ws, `[error] ${err.message}\n`);
      void drainPendingThenResolve(1);
    });
    child.on('close', (code, signal) => {
      const closeSignal = signal ?? null;
      clearTimeout(killTimer);
      if (forceKillTimer) clearTimeout(forceKillTimer);
      if (resultGraceTimer) clearTimeout(resultGraceTimer);
      if (stallWatchdog) clearTimeout(stallWatchdog);
      if (stallWatchdogTimer) { clearInterval(stallWatchdogTimer); stallWatchdogTimer = null; }
      if (checkRunawayWatchdog) { clearInterval(checkRunawayWatchdog); checkRunawayWatchdog = null; }
      // Phase 1: unregister now that we know the child exited; reset routine
      // doesn't need to chase a dead PID.
      if (typeof originalPid === 'number') {
        unregisterChildPid(originalPid);
      }
      if (closeSignal === 'SIGKILL' && !flags.timedOut) {
        flags.providerKilledByOOM = true;
        const oomLine = `EOOM: ${runCommand.displayName} process received external SIGKILL before agent timeout; likely cgroup OOM\n`;
        stderrBuffer = appendBoundedTail(stderrBuffer, oomLine, STDERR_TAIL_BYTES);
        if (allOutput.length < MAX_OUTPUT_BYTES) allOutput += oomLine;
        sendLog(ws, `[stderr] ${oomLine}`);
      }
      if (promptFile) {
        try { fs.unlinkSync(promptFile); } catch { /* ignore */ }
      }
      log(`${runCommand.displayName} exited with code ${code ?? 'null'}${closeSignal ? ` signal ${closeSignal}` : ''}`);
      void drainPendingThenResolve(code ?? 1);
    });
  });

  sendLog(ws, `\n--- ${runner.displayName} exited (code: ${exitCode}) ---\n`);

  if (flags.timedOut) {
    throw new ClaudeTimeoutError(`${runner.displayName} exceeded ${runCommand.timeoutMs / 60000}-minute time limit`);
  }
  if (flags.stalledMidStream) {
    throw new ClaudeTimeoutError(`${runner.displayName} mid-stream tool-use stall: tool in-flight for >${TOOL_USE_STALL_TIMEOUT_MS / 60000} min without result`);
  }
  if (flags.providerKilledByOOM) {
    const err = new ClaudeTimeoutError(`EOOM: ${runner.displayName} received external SIGKILL before agent timeout; likely cgroup OOM`);
    (err as Error & { code?: string }).code = 'EOOM';
    throw err;
  }

  if (exitCode !== 0) {
    const combined = stderrBuffer + allOutput.slice(-LOG_TAIL_BYTES);
    if (matchesRateLimit(combined, runner.provider, runner.authMode)) {
      const snippet = (stderrBuffer || allOutput.slice(-500)).slice(0, 500);
      throw new RateLimitError(`${runner.displayName} API usage exhausted — ${snippet}`);
    }
  }

  return { exitCode, allOutput, lastResultText: lastResultText || allOutput.trim() };
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

  // Per-role telemetry reset on cold-start. Mirrors the role-loop's warm
  // handoff reset so a stale value from a previous process incarnation
  // (shouldn't happen, but defensive) cannot leak into the first role's
  // status payload.
  handlerDurationSecsForPayload = null;

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
      const claimResult = await runRoleOnce({ reportPreClaimFailure });
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
      process.env.TASK_ID = '';

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
      }
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
      // Per-role telemetry reset. The next loop iteration's runRoleOnce
      // will set roleClaimTimestamp after a successful re-claim.
      isWarmStart = true;
      firstToolUseAt = null;
      roleClaimTimestamp = null;
      handlerDurationSecsForPayload = null;
      // Drain the tool-trace ring buffer so the next role's
      // extractInvestigatedFiles() and heartbeat-attached trace start clean
      // — without this, the next agent's heartbeat surfaces prior-role tool
      // uses and the reaper's death-trace conflates two roles' work.
      clearToolTrace();
      currentPromptFingerprint = null;
      // Same rationale for the `./check` synthesizer's monotonic state:
      // without a reset, the next role's synthesized iterations continue
      // numbering from the prior role and carry a stale started_at.
      resetCheckSynthesizerState();
      // Per-task LLM telemetry must reset too (efficiency direction
      // d-91d8dede) — without this, warm-VM continuity rolls the prior
      // role's call/token totals into the next task's heartbeat payload.
      resetLlmTelemetry();
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
  // between warm roles. Default to 'main' when unspecified — matches the
  // handlers' own strVal(payload.base_branch, 'main') fallback.
  const rawBaseBranch = payload.base_branch;
  const baseBranchForReset =
    typeof rawBaseBranch === 'string' && rawBaseBranch.trim()
      ? rawBaseBranch.trim()
      : 'main';

  // Start heartbeat & log stream
  const statusRef = { value: 'Initializing...' };
  startHeartbeat(taskId, () => statusRef.value);
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
    const errorCategory = (!isRateLimit && error instanceof Error)
      ? (error as Error & { failureCategory?: FailureCategory }).failureCategory
      : undefined;
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
  const taskId = process.env.TASK_ID;
  if (taskId) {
    try {
      const metrics = collectMetrics();
      const msg = `[FATAL:${label}] ${detail.slice(0, 300)}`;
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
