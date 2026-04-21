import { spawn, execFileSync } from 'child_process';
import { fileURLToPath } from 'url';
import fs from 'fs';
import WebSocket from 'ws';
import { handlers, type Handler } from './handlers/index.js';
import { extractJsonObject, extractJsonArray } from './json-extract.js';
import {
  API_TIMEOUT, API_MAX_RETRIES, LOGS_MAX_LENGTH,
  HEARTBEAT_INTERVAL, MAX_LOG_BUFFER,
  GIT_CLONE_TIMEOUT, CLONE_MAX_RETRIES, CLONE_RETRY_BASE_DELAY,
  CONFLICT_CHECK_TIMEOUT,
  MAX_LINT_FIX_ATTEMPTS, MAX_TEST_FIX_ATTEMPTS,
  FRONTEND_NPM_TIMEOUT, FRONTEND_ESLINT_TIMEOUT, FRONTEND_BUILD_TIMEOUT,
  AGENT_NPM_TIMEOUT, AGENT_TSC_TIMEOUT,
  RUFF_TIMEOUT,
  TEST_RUNNER_TIMEOUT, TEST_BASELINE_TIMEOUT, TEST_BASELINE_DOCKER_TIMEOUT,
  claudeTimeoutForEffort, MAX_OUTPUT_BYTES,
} from './constants.js';

// ── Failure categories ───────────────────────────────────────────────────────
export type FailureCategory = 'clone_failed' | 'build_failed' | 'test_failed' | 'lint_failed' | 'pr_creation_failed' | 'claude_code_failed' | 'parse_failed' | 'fatal' | 'rate_limited' | 'timeout';
type ProviderName = 'anthropic' | 'openai' | 'gemini';
type AuthMode = 'oauth' | 'api_key' | 'service_account';
type OutputMode = 'claude-stream-json' | 'text';

const RATE_LIMIT_PATTERNS: Readonly<Record<ProviderName, Partial<Record<AuthMode, readonly string[]>>>> = {
  anthropic: {
    oauth: [
      "you've hit your limit",
      "you've hit your usage limit",
      'claude ai usage limit reached',
      "you're out of extra usage",
      '5-hour limit reached',
      'api error: rate limit reached',
      "this request would exceed your account's rate limit",
    ],
    api_key: [
      "you've hit your limit",
      "you've hit your usage limit",
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
    ],
    service_account: [
      'resource has been exhausted',
      'resource_exhausted',
      'ratelimitexceeded',
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

function getRateLimitPatterns(provider: ProviderName, authMode: AuthMode): readonly string[] {
  return RATE_LIMIT_PATTERNS[provider][authMode] ?? RATE_LIMIT_PATTERNS[provider].oauth ?? [];
}

export function matchesRateLimit(text: string, provider: ProviderName = PROVIDER, authMode: AuthMode = AUTH_MODE): boolean {
  const lower = text.toLowerCase();
  return getRateLimitPatterns(provider, authMode).some(p => lower.includes(p));
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
}

// ── Environment ─────────────────────────────────────────────────────────────
const ORCHESTRATOR_URL = process.env.ORCHESTRATOR_URL || 'http://localhost:8080';
let AGENT_TOKEN = process.env.AGENT_SECRET_TOKEN || '';
const VM_INSTANCE_ID = process.env.VM_INSTANCE_ID || 'local';
export const WORKSPACE = process.env.AGENT_WORKSPACE || '/tmp/workspace';

/** Get the current agent token (may have been refreshed since startup). */
export function getAgentToken(): string { return AGENT_TOKEN; }

const apiHeaders: Record<string, string> = {
  'Content-Type': 'application/json',
  'X-Agent-Token': AGENT_TOKEN,
};

// ── Helpers ──────────────────────────────────────────────────────────────────
export function strVal(v: unknown, fallback = ''): string {
  return typeof v === 'string' ? v : fallback;
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

export async function apiPost(path: string, body: Record<string, unknown> = {}, retries = API_MAX_RETRIES, timeoutMs = API_TIMEOUT) {
  // Cap logs field to avoid 422 from backend's max_length=500000 validation.
  // A 422 is a 4xx = no retry = agent dies silently.
  if (body.logs && typeof body.logs === 'string' && body.logs.length > LOGS_MAX_LENGTH) {
    body = { ...body, logs: body.logs.slice(0, LOGS_MAX_LENGTH) + '\n...(truncated)' };
  }
  let tokenRefreshed = false;
  for (let attempt = 0; attempt < retries; attempt++) {
    try {
      const controller = new AbortController();
      const timeout = setTimeout(() => { controller.abort(); }, timeoutMs);
      const res = await fetch(`${ORCHESTRATOR_URL}${path}`, {
        method: 'POST', headers: apiHeaders, body: JSON.stringify(body), signal: controller.signal,
      });
      clearTimeout(timeout);
      if (res.ok) return await res.json() as unknown;
      const text = await res.text();
      // 401 with "no longer active": task completed/failed — our previous POST worked.
      // This happens when a status COMPLETED POST succeeded but the response was lost,
      // so the agent retries and hits JWT revocation. Treat as success.
      if (res.status === 401 && text.includes('no longer active')) {
        if (path === '/api/agent/status') {
          log(`POST ${path} got 401 (task terminated) — previous POST likely succeeded`);
          return { status: 'already_terminal', task_id: body.task_id };
        }
        throw new Error(`POST ${path} failed (401): ${text}`);
      }
      // 401: attempt token refresh at most once, then retry
      if (res.status === 401) {
        if (tokenRefreshed) throw new Error(`POST ${path} failed (401): ${text}`);
        log(`POST ${path} got 401 — attempting token refresh`);
        const refreshed = await tryRefreshToken();
        if (!refreshed) throw new Error(`POST ${path} failed (401): ${text}`);
        tokenRefreshed = true;
        continue;
      }
      // 429: retry after Retry-After header. Backend uses this for
      // temporary-contention signals (e.g. row-locked in /api/agent/status);
      // treating it as transient keeps the agent's retry loop aligned with
      // 5xx handling without tripping Cloud Run ContainerError classification.
      if (res.status === 429) {
        if (attempt === retries - 1) {
          throw new Error(`POST ${path} failed (${res.status}): ${text}`);
        }
        const retryAfterSec = parseInt(res.headers.get('Retry-After') || '5', 10);
        const delayMs = Math.max(1000, Math.min(retryAfterSec * 1000, 30000));
        log(`POST ${path} got 429 (rate-limited/locked), retrying in ${delayMs}ms`);
        await new Promise(r => setTimeout(r, delayMs));
        continue;
      }
      // Other 4xx errors are not transient — fail immediately
      if (res.status < 500) {
        throw new Error(`POST ${path} failed (${res.status}): ${text}`);
      }
      // 5xx: retry if we have attempts left
      if (attempt === retries - 1) {
        throw new Error(`POST ${path} failed (${res.status}): ${text}`);
      }
    } catch (e) {
      // Re-throw 4xx errors and final-attempt errors immediately. 429 is
      // intentionally excluded from the 4xx bail-out via the explicit check
      // above, which uses `continue` before this catch runs.
      if (attempt === retries - 1 || (e instanceof Error && /failed \(4\d{2}\)/.test(e.message))) throw e;
    }
    await new Promise(r => setTimeout(r, Math.min(1000 * Math.pow(2, attempt) + Math.random() * 1000, 30000)));
  }
  throw new Error(`POST ${path} failed after ${retries} retries`);
}

/** Persist result payload to disk before POSTing status, then clean up on success.
 *  If the POST fails (network error, VM kill), the file survives for retry on next startup. */
export async function persistAndPostStatus(taskId: string, payload: Record<string, unknown>) {
  const resultPath = `/tmp/task_${taskId}_result.json`;
  const enrichedPayload = enrichStatusPayload(taskId, payload);
  fs.writeFileSync(resultPath, JSON.stringify(enrichedPayload), 'utf-8');
  try {
    const result = await apiPost('/api/agent/status', enrichedPayload);
    try { fs.unlinkSync(resultPath); } catch { /* ignore — file already gone */ }
    return result;
  } catch (e) {
    // File intentionally left on disk — retried on next agent startup
    throw e;
  }
}

function enrichStatusPayload(taskId: string, payload: Record<string, unknown>): Record<string, unknown> {
  return {
    provider: PROVIDER,
    auth_mode: AUTH_MODE,
    model: process.env.LLM_MODEL || undefined,
    execution_mode: LLM_EXECUTION_MODE,
    task_id: taskId,
    ...payload,
  };
}

export async function apiGet(path: string, retries = API_MAX_RETRIES) {
  let tokenRefreshed = false;
  for (let attempt = 0; attempt < retries; attempt++) {
    try {
      const controller = new AbortController();
      const timeout = setTimeout(() => { controller.abort(); }, API_TIMEOUT);
      const res = await fetch(`${ORCHESTRATOR_URL}${path}`, { headers: apiHeaders, signal: controller.signal });
      clearTimeout(timeout);
      if (res.ok) return await res.json() as unknown;
      const text = await res.text();
      // 401: attempt token refresh at most once, then retry
      if (res.status === 401) {
        if (tokenRefreshed) throw new Error(`GET ${path} failed (401): ${text}`);
        log(`GET ${path} got 401 — attempting token refresh`);
        const refreshed = await tryRefreshToken();
        if (!refreshed) throw new Error(`GET ${path} failed (401): ${text}`);
        tokenRefreshed = true;
        continue;
      }
      if (res.status < 500) {
        throw new Error(`GET ${path} failed (${res.status}): ${text}`);
      }
      if (attempt === retries - 1) {
        throw new Error(`GET ${path} failed (${res.status}): ${text}`);
      }
    } catch (e) {
      if (attempt === retries - 1 || (e instanceof Error && /failed \(4\d{2}\)/.test(e.message))) throw e;
    }
    await new Promise(r => setTimeout(r, Math.min(1000 * Math.pow(2, attempt) + Math.random() * 1000, 30000)));
  }
  throw new Error(`GET ${path} failed after ${retries} retries`);
}

export function log(msg: string) {
  console.log(`[${new Date().toISOString()}] ${msg}`);
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

export function recordToolUse(toolName: string, input: unknown): void {
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

export function snapshotToolTrace(): ToolTraceEntry[] {
  return _toolTrace.slice();
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

function startHeartbeat(taskId: string, getMessage: () => string) {
  // Fire first heartbeat immediately — don't wait 30s.
  // If the agent crashes in the first 30s, at least one heartbeat is recorded.
  void (async () => {
    try {
      const metrics = collectMetrics();
      await apiPost('/api/agent/heartbeat', {
        task_id: taskId,
        status_message: `[${metrics.heap_used_mb}MB] ${getMessage()}`,
        metrics,
        tool_trace: snapshotToolTrace(),
      });
    } catch (e) {
      log(`Initial heartbeat failed: ${String(e)}`);
    }
  })();

  heartbeatTimer = setInterval(() => { void (async () => {
    try {
      const metrics = collectMetrics();
      // Proactive OOM warning — SIGKILL from cgroup can't be caught, so warn while we still can
      if (metrics.cgroup_mb && metrics.cgroup_limit_mb) {
        const memPct = metrics.cgroup_mb / metrics.cgroup_limit_mb;
        if (memPct > 0.85) {
          log(`WARNING: Memory at ${(memPct * 100).toFixed(0)}% of cgroup limit (${metrics.cgroup_mb}MB / ${metrics.cgroup_limit_mb}MB) — OOM kill imminent`);
        }
      }
      await apiPost('/api/agent/heartbeat', {
        task_id: taskId,
        status_message: `[${metrics.heap_used_mb}MB] ${getMessage()}`,
        metrics,
        tool_trace: snapshotToolTrace(),
      });
    } catch (e: unknown) {
      log(`Heartbeat failed: ${String(e)}`);
      // 409 = row temporarily locked (skip_locked) — retry once after 5s.
      // Silently dropping heartbeats can let last_heartbeat go stale if the
      // status endpoint holds its FOR UPDATE lock for multiple heartbeat cycles.
      if (e instanceof Error && /failed \(409\)/.test(e.message)) {
        // Exponential backoff: 5s, then 10s. Total window 15s, within 30s interval.
        for (let retry = 0; retry < 2; retry++) {
          const delay = 5000 * (retry + 1);
          log(`Heartbeat skipped (row locked), retrying in ${delay / 1000}s (attempt ${retry + 1}/2)`);
          await new Promise(r => setTimeout(r, delay));
          try {
            const retryMetrics = collectMetrics();
            await apiPost('/api/agent/heartbeat', {
              task_id: taskId,
              status_message: `[${retryMetrics.heap_used_mb}MB] ${getMessage()}`,
              metrics: retryMetrics,
              tool_trace: snapshotToolTrace(),
            });
            log('Heartbeat retry succeeded');
            return;
          } catch (retryErr: unknown) {
            log(`Heartbeat retry ${retry + 1}/2 failed: ${String(retryErr)}`);
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
    }
  })(); }, HEARTBEAT_INTERVAL);
}

function stopHeartbeat() {
  if (heartbeatTimer) { clearInterval(heartbeatTimer); heartbeatTimer = null; }
}

/** Synchronous heartbeat — use between blocking execFileSync calls to prevent reaper from killing us.
 *  Fires curl in a child process (sync) so the event loop doesn't need to be free. */
export function sendHeartbeatSync(taskId: string, statusMessage: string, extras?: Record<string, unknown>) {
  try {
    const metrics = collectMetrics();
    const payload = JSON.stringify({
      task_id: taskId,
      status_message: `[${metrics.heap_used_mb}MB] ${statusMessage}`,
      metrics,
      ...(extras || {}),
    });
    execFileSync('curl', [
      '-sf', '-X', 'POST',
      `${ORCHESTRATOR_URL}/api/agent/heartbeat`,
      '-H', 'Content-Type: application/json',
      '-H', `X-Agent-Token: ${AGENT_TOKEN}`,
      '-d', payload,
    ], { timeout: 10_000, stdio: 'ignore' });
  } catch (e) {
    log(`Sync heartbeat failed: ${e instanceof Error ? e.message : String(e)} (non-fatal)`);
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

  for (let attempt = 0; attempt < maxRetries; attempt++) {
    try {
      const ws = await new Promise<WebSocket | null>((resolve) => {
        const conn = new WebSocket(`${wsUrl}/api/agent/ws/${taskId}`);
        const timeout = setTimeout(() => { resolve(null); }, 5000);
        conn.on('open', () => {
          clearTimeout(timeout);
          conn.send(JSON.stringify({ token: AGENT_TOKEN }));
          resolve(conn);
        });
        conn.on('error', () => { clearTimeout(timeout); resolve(null); });
      });
      if (ws) {
        flushLogBuffer(ws);
        return ws;
      }
    } catch { /* ignore */ }
    if (attempt < maxRetries - 1) {
      await new Promise(r => setTimeout(r, baseDelay * Math.pow(2, attempt)));
    }
  }
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

export function readHandlerOutput<T = unknown>(
  taskId: string,
  outputType: string,
  ws: WebSocket | null,
): T | null {
  const filePath = `/tmp/${outputType}_${taskId}.json`;
  let raw: string;
  try {
    raw = fs.readFileSync(filePath, 'utf-8');
  } catch (err: unknown) {
    sendLog(ws, `[WARNING] Failed to read ${filePath}: ${err instanceof Error ? err.message : String(err)}\n`);
    return null;
  }
  try {
    return JSON.parse(raw) as T;
  } catch (err: unknown) {
    sendLog(ws, `[WARNING] Failed to parse ${filePath}: ${err instanceof Error ? err.message : String(err)}\n`);
    // Try extracting a JSON array (research findings, plans)
    // Only accept non-empty arrays; empty arrays (e.g. `[]` embedded inside an
    // object like `"direction_operations": []`) would otherwise shadow the
    // subsequent object extraction.
    const arrayResult = extractJsonArray<T>(raw);
    if (arrayResult && Array.isArray(arrayResult) && (arrayResult as unknown[]).length > 0) return arrayResult;
    // Try extracting a JSON object (strategic output)
    const objResult = extractJsonObject<T>(raw, 'vision', 'direction_operations');
    if (objResult) return objResult;
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
    proc.stdout!.on('data', (d: Buffer) => stdoutChunks.push(d));
    proc.stderr!.on('data', (d: Buffer) => stderrChunks.push(d));
    let timedOut = false;
    let killTimer: NodeJS.Timeout | null = null;
    const termTimer = setTimeout(() => {
      timedOut = true;
      const pid = proc.pid;
      if (pid) {
        try { process.kill(-pid, 'SIGTERM'); } catch { /* already exited */ }
        killTimer = setTimeout(() => {
          try { process.kill(-pid, 'SIGKILL'); } catch { /* already dead */ }
        }, 5000);
      }
    }, timeoutMs);
    proc.on('close', (code) => {
      clearTimeout(termTimer);
      if (killTimer) clearTimeout(killTimer);
      const stdout = Buffer.concat(stdoutChunks).toString('utf-8');
      const stderr = Buffer.concat(stderrChunks).toString('utf-8');
      if (code === 0 && !timedOut) {
        resolve(stdout);
        return;
      }
      const parts = [`Command failed: ${cmd} ${args.join(' ')}`];
      if (stdout.trim()) parts.push(stdout);
      if (stderr.trim()) parts.push(stderr);
      const err: Error & {
        stdout?: string; stderr?: string; status?: number | null; killed?: boolean; code?: string;
      } = new Error(parts.join('\n'));
      err.stdout = stdout;
      err.stderr = stderr;
      err.status = code;
      err.killed = timedOut;
      if (timedOut) err.code = 'ETIMEDOUT';
      reject(err);
    });
    proc.on('error', (e) => { clearTimeout(termTimer); if (killTimer) clearTimeout(killTimer); reject(e); });
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
    proc.stdout!.on('data', (d: Buffer) => chunks.push(d));
    proc.stderr!.on('data', (d: Buffer) => errChunks.push(d));
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
  try {
    await cloneWithRetry(['clone', '--depth', '100', '--branch', baseBranch, cloneUrl, WORKSPACE]);
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
        execFileSync(cmd, args, { cwd, encoding: 'utf-8', timeout: FRONTEND_NPM_TIMEOUT, env: extraEnv as NodeJS.ProcessEnv });
      }
      if (dir.lint_cmd) {
        sendLog(ws, `Running lint in ${dirPrefix}...\n`);
        sendHeartbeatSync(taskId, `${dir.lint_cmd} (${dirPrefix})...`);
        const [cmd, ...args] = dir.lint_cmd.split(' ');
        execFileSync(cmd, args, { cwd, encoding: 'utf-8', timeout: FRONTEND_ESLINT_TIMEOUT, env: extraEnv as NodeJS.ProcessEnv });
      }
      if (dir.build_cmd) {
        sendLog(ws, `Running build in ${dirPrefix}...\n`);
        sendHeartbeatSync(taskId, `${dir.build_cmd} (${dirPrefix})...`);
        const [cmd, ...args] = dir.build_cmd.split(' ');
        execFileSync(cmd, args, { cwd, encoding: 'utf-8', timeout: FRONTEND_BUILD_TIMEOUT, env: extraEnv as NodeJS.ProcessEnv });
      }
      sendLog(ws, `Checks passed for ${dirPrefix}.\n`);
    }
    return;
  }

  // Fallback: hardcoded Blueprint project structure
  const touchesFrontend = changedPaths.some(p => p.startsWith('admin-ui/'));
  const touchesAgent = changedPaths.some(p => p.startsWith('agent/'));
  const touchesBackend = changedPaths.some(p => p.startsWith('backend/'));
  if (touchesFrontend) {
    sendLog(ws, 'Running frontend lint + build...\n');
    sendHeartbeatSync(taskId, 'npm ci (admin-ui)...');
    execFileSync('npm', ['ci'], { cwd: `${WORKSPACE}/admin-ui`, encoding: 'utf-8', timeout: FRONTEND_NPM_TIMEOUT });
    sendHeartbeatSync(taskId, 'eslint (admin-ui)...');
    execFileSync('npx', ['eslint', '.'], { cwd: `${WORKSPACE}/admin-ui`, encoding: 'utf-8', timeout: FRONTEND_ESLINT_TIMEOUT });
    sendHeartbeatSync(taskId, 'npm run build (admin-ui)...');
    execFileSync('npm', ['run', 'build'], { cwd: `${WORKSPACE}/admin-ui`, encoding: 'utf-8', timeout: FRONTEND_BUILD_TIMEOUT, env: { ...process.env, VITE_API_URL: 'http://localhost:8080', VITE_FIREBASE_API_KEY: 'ci', VITE_FIREBASE_AUTH_DOMAIN: 'ci.firebaseapp.com', VITE_FIREBASE_PROJECT_ID: 'ci' } });
    sendLog(ws, 'Frontend checks passed.\n');
  }
  if (touchesAgent) {
    sendLog(ws, 'Running agent type check...\n');
    sendHeartbeatSync(taskId, 'npm ci (agent)...');
    execFileSync('npm', ['ci'], { cwd: `${WORKSPACE}/agent`, encoding: 'utf-8', timeout: AGENT_NPM_TIMEOUT });
    sendHeartbeatSync(taskId, 'tsc --noEmit (agent)...');
    execFileSync('npx', ['tsc', '--noEmit'], { cwd: `${WORKSPACE}/agent`, encoding: 'utf-8', timeout: AGENT_TSC_TIMEOUT });
    sendLog(ws, 'Agent type check passed.\n');
  }
  if (touchesBackend) {
    sendLog(ws, 'Running backend ruff lint...\n');
    try {
      const lintOut = execFileSync('python3', ['-m', 'ruff', 'check', 'backend/', '--select', 'E,F,W', '--ignore', 'E501'], { cwd: WORKSPACE, encoding: 'utf-8', timeout: RUFF_TIMEOUT });
      sendLog(ws, lintOut || 'Backend ruff lint passed.\n');
    } catch (err: unknown) {
      const errCode = (err as NodeJS.ErrnoException).code;
      const errMsg = err instanceof Error ? err.message : String(err);
      if (errCode === 'ENOENT' || errMsg.includes('No module named ruff')) {
        sendLog(ws, 'ruff not installed, skipping backend lint.\n');
      } else {
        throw err;
      }
    }
    sendLog(ws, 'Running backend ruff format check...\n');
    try {
      const fmtOut = execFileSync('python3', ['-m', 'ruff', 'format', '--check', 'backend/'], { cwd: WORKSPACE, encoding: 'utf-8', timeout: RUFF_TIMEOUT });
      sendLog(ws, fmtOut || 'Backend ruff format check passed.\n');
    } catch (err: unknown) {
      const errCode = (err as NodeJS.ErrnoException).code;
      const errMsg = err instanceof Error ? err.message : String(err);
      if (errCode === 'ENOENT' || errMsg.includes('No module named ruff')) {
        sendLog(ws, 'ruff not installed, skipping backend format check.\n');
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

/** Run lint checks in a loop, asking Claude to fix failures up to MAX_LINT_FIX_ATTEMPTS times. */
export async function lintFixLoop(
  changedPaths: string[], ws: WebSocket | null, statusRef: { value: string },
  context: string = 'the code you just wrote', model?: string, effort?: string,
  buildConfig?: BuildConfig
): Promise<boolean> {
  let prevFingerprint: string | null = null;
  for (let attempt = 0; attempt < MAX_LINT_FIX_ATTEMPTS; attempt++) {
    try {
      statusRef.value = attempt === 0 ? 'Running pre-commit checks...' : `Re-running lint (attempt ${attempt + 1}/${MAX_LINT_FIX_ATTEMPTS})...`;
      runProjectLintChecks(changedPaths, ws, buildConfig);
      return true;
    } catch (lintErr: unknown) {
      const msg = lintErr instanceof Error ? lintErr.message : String(lintErr);
      const fingerprint = errorFingerprint(msg);
      if (prevFingerprint !== null && fingerprint === prevFingerprint) {
        sendLog(ws, `Lint error unchanged from previous attempt — exiting early (persistent, unfixable by Claude).\n`);
        return false;
      }
      prevFingerprint = fingerprint;
      sendLog(ws, `Pre-commit check failed (attempt ${attempt + 1}/${MAX_LINT_FIX_ATTEMPTS}):\n${msg.slice(0, 3000)}\nClaude will attempt to fix...\n`);
      await runClaudeCode(
        `The following lint/build errors were found in ${context}. Fix ALL of them — do not use \`any\` types.\n\n${msg.slice(0, 5000)}`,
        ws, statusRef, model, effort
      );
      git(['add', '-A']);
    }
  }
  sendLog(ws, `WARNING: Lint checks still failing after ${MAX_LINT_FIX_ATTEMPTS} attempts.\n`);
  return false;
}

// ── Test runner detection (shared by testFixLoop and captureTestBaseline) ───
interface TestRunner {
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
  if (runners.length === 0) {
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

    if (runners.length === 0) {
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

    if (runners.length === 0 && existsSync(cargoPath)) {
      runners.push({ cmd: 'cargo', args: ['test'], cwd: WORKSPACE, label: 'cargo test (root)' });
    }

    // --- Subdirectory detection (only if no root-level runner found) ---
    if (runners.length === 0) {
      const backendPytestIni = `${WORKSPACE}/backend/pytest.ini`;
      const backendPyproject = `${WORKSPACE}/backend/pyproject.toml`;
      const backendTestScript = `${WORKSPACE}/scripts/test-backend.sh`;
      const agentPkg = `${WORKSPACE}/agent/package.json`;

      if (existsSync(backendPytestIni)) {
        if (existsSync(backendTestScript)) {
          runners.push({ cmd: 'bash', args: ['scripts/test-backend.sh'], cwd: WORKSPACE, label: 'pytest (backend)' });
        } else {
          runners.push({ cmd: 'python3', args: ['-m', 'pytest', '-x', '-q'], cwd: `${WORKSPACE}/backend`, label: 'pytest (backend)' });
        }
      } else if (existsSync(backendPyproject)) {
        try {
          if (readFileSync(backendPyproject, 'utf-8').includes('pytest')) {
            if (existsSync(backendTestScript)) {
              runners.push({ cmd: 'bash', args: ['scripts/test-backend.sh'], cwd: WORKSPACE, label: 'pytest (backend pyproject)' });
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
    // vitest: "✕ test description" or "× test description" or "✗ test description"
    const vitestMatch = trimmed.match(/^[✕✗×]\s+(.+)$/u);
    if (vitestMatch) failing.add(vitestMatch[1].trim());
  }
  return failing;
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
    try {
      sendHeartbeatSync(taskId, `Baseline test run [${runner.label}]...`);
      const runnerTimeout = isDockerRunner(runner) ? TEST_BASELINE_DOCKER_TIMEOUT : timeoutMs;
      await execFileAsync(runner.cmd, runner.args, {
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
      const parsed = parseFailingTests(msg);
      parsed.forEach(f => baselineFailures.add(f));
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

export async function testFixLoop(
  ws: WebSocket | null, statusRef: { value: string },
  model?: string, effort?: string, buildConfig?: BuildConfig,
  baselineFailures?: Set<string>,
  relatedFiles?: string[],
  analysisContext?: string,
): Promise<{ passed: boolean; testFailuresDetected: boolean; failureSummary?: string }> {
  let runners = detectTestRunners(buildConfig);

  // Scope-limit to runners relevant to the files being changed. Same logic as
  // captureTestBaseline — avoids running the full backend Docker test suite
  // when only agent files changed.
  if (relatedFiles && relatedFiles.length > 0) {
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

  let prevFingerprint: string | null = null;
  for (let attempt = 0; attempt <= MAX_TEST_FIX_ATTEMPTS; attempt++) {
    statusRef.value = attempt === 0 ? 'Running test suite...' : `Re-running tests (attempt ${attempt + 1}/${MAX_TEST_FIX_ATTEMPTS + 1})...`;
    sendLog(ws, `Running tests (attempt ${attempt + 1}/${MAX_TEST_FIX_ATTEMPTS + 1})...\n`);

    const failures: string[] = [];
    const taskId = process.env.TASK_ID || '';
    for (const runner of runners) {
      try {
        // Ensure node_modules are present for npm-based runners; runProjectLintChecks
        // installs them in the normal agent flow but testFixLoop may be called
        // standalone (e.g. from the Blueprint harness) without that step having run.
        if (runner.cmd === 'npm' && !fs.existsSync(`${runner.cwd}/node_modules`)) {
          sendLog(ws, `Installing dependencies for ${runner.label}...\n`);
          await execFileAsync('npm', ['ci'], { cwd: runner.cwd, timeoutMs: TEST_RUNNER_TIMEOUT });
        }
        sendHeartbeatSync(taskId, `Running tests [${runner.label}]...`);
        await execFileAsync(runner.cmd, runner.args, { cwd: runner.cwd, timeoutMs: TEST_RUNNER_TIMEOUT });
        sendLog(ws, `Tests passed [${runner.label}].\n`);
      } catch (testErr: unknown) {
        // If the runner timed out (killed by the process-group SIGTERM/SIGKILL
        // chain in execFileAsync) or the OS could not spawn the process
        // (ETIMEDOUT), treat as indeterminate — a Docker build timeout or
        // spawn failure is not a fixable code failure.
        const isRunnerTimeout = testErr instanceof Error && (
          (testErr as Error & { killed?: boolean }).killed === true ||
          (testErr as NodeJS.ErrnoException).code === 'ETIMEDOUT'
        );
        if (isRunnerTimeout) {
          sendLog(ws, `WARNING: Test runner [${runner.label}] timed out — skipping.\n`);
          continue;
        }
        const msg = testErr instanceof Error ? testErr.message : String(testErr);
        failures.push(`[${runner.label}]:\n${msg}`);
      }
    }

    if (failures.length === 0) {
      sendLog(ws, 'All tests passed.\n');
      return { passed: true, testFailuresDetected: false };
    }

    // When baseline is available, separate new failures from pre-existing ones.
    // A failure block is pre-existing when all parsed test IDs within it appear
    // in the baseline. Unparseable failures (size=0) are treated as new (safe default).
    let fixableFailures = failures;
    let preExistingNote = '';

    if (baselineFailures) {
      const newFailures: string[] = [];
      const preExistingFailures: string[] = [];

      for (const failure of failures) {
        const failureIds = parseFailingTests(failure);
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
        preExistingNote = `\n\nNote: The following test failures existed BEFORE your changes — do not attempt to fix these:\n${preExistingFailures.join('\n\n').slice(0, 1500)}`;
      }
    }

    const combinedFailures = fixableFailures.join('\n\n');
    const fingerprint = errorFingerprint(combinedFailures);
    if (prevFingerprint !== null && fingerprint === prevFingerprint) {
      sendLog(ws, `Test failures unchanged from previous attempt — exiting early (persistent, unfixable by Claude).\n`);
      const failureSummary = combinedFailures.slice(0, 2000);
      return { passed: false, testFailuresDetected: true, failureSummary };
    }
    prevFingerprint = fingerprint;
    if (attempt < MAX_TEST_FIX_ATTEMPTS) {
      const preExistingHint = preExistingNote ? ' (pre-existing failures excluded)' : '';
      sendLog(ws, `Tests failed (attempt ${attempt + 1}/${MAX_TEST_FIX_ATTEMPTS + 1})${preExistingHint}:\n${combinedFailures.slice(0, 3000)}\nClaude will attempt to fix...\n`);
      const analysisSections = analysisContext ? condensedAnalysisSections(analysisContext) : '';
      const analysisPrefix = analysisSections ? `## Module Analysis\n\n${analysisSections}\n\n` : '';
      const fixPrompt = baselineFailures
        ? `${analysisPrefix}These tests broke because of your changes. Fix ONLY these newly-broken tests:\n\n${combinedFailures.slice(0, 4000)}${preExistingNote.slice(0, 1000)}`
        : `${analysisPrefix}Refactoring broke these tests. Fix ONLY to restore passing tests without reverting the refactor's intent:\n\n${combinedFailures.slice(0, 5000)}`;
      await runClaudeCode(fixPrompt, ws, statusRef, model, effort);
      git(['add', '-A']);
    } else {
      sendLog(ws, `WARNING: Tests still failing after ${MAX_TEST_FIX_ATTEMPTS} fix attempts. Proceeding with commit.\n`);
      const failureSummary = combinedFailures.slice(0, 2000);
      return { passed: false, testFailuresDetected: true, failureSummary };
    }
  }
  return { passed: false, testFailuresDetected: true };
}

// ── Claude Code Runner (shared by all handlers) ─────────────────────────────
interface ClaudeOutputBlock { type: string; text?: string; name?: string; input?: unknown }
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
    'ORCHESTRATOR_URL',
    'TASK_ID',
  ]) {
    if (process.env[key]) env[key] = process.env[key]!;
  }
  if (AGENT_TOKEN) env.AGENT_API_TOKEN = AGENT_TOKEN;
  return env;
}

function withExecutionModePrompt(prompt: string): string {
  if (LLM_EXECUTION_MODE !== 'advisor') return prompt;
  return [
    'Execution mode: advisor.',
    'Prioritize analysis, recommendations, and risk assessment.',
    'Avoid making code changes unless the task explicitly requires implementation.',
    '',
    prompt,
  ].join('\n');
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
      buildRunCommand: (_prompt, model) => {
        const args = ['exec', '--full-auto'];
        if (model) args.push('-m', model);
        return {
          command: 'codex',
          args,
          env: baseEnv,
          outputMode: 'text',
          timeoutMs: claudeTimeoutForEffort(undefined),
          promptInput: 'stdin',
          displayName: 'OpenAI Codex',
        };
      },
      buildValidationCommand: (model) => {
        const args = ['exec'];
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
      buildRunCommand: (_prompt, model) => {
        const args = ['--approval-mode=yolo', '-o', 'text'];
        if (model) args.push('-m', model);
        return {
          command: 'gemini',
          args,
          env: baseEnv,
          outputMode: 'text',
          timeoutMs: claudeTimeoutForEffort(undefined),
          promptInput: 'stdin',
          displayName: 'Gemini CLI',
        };
      },
      buildValidationCommand: (model) => {
        const args = ['-o', 'text', '-p', 'Respond with OK.'];
        if (model) args.push('-m', model);
        return { command: 'gemini', args, env: baseEnv, timeoutMs: 30_000 };
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
      if (model) args.push('--model', model);
      if (effort) args.push('--effort', effort);
      if (disallowedTools && disallowedTools.length > 0) {
        for (const tool of disallowedTools) args.push('--disallowedTools', tool);
      }
      return {
        command: 'claude',
        args,
        env: baseEnv,
        outputMode: 'claude-stream-json',
        timeoutMs: claudeTimeoutForEffort(effort),
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

export async function runClaudeCode(prompt: string, ws: WebSocket | null, statusRef: { value: string }, model?: string, effort?: string, disallowedTools?: string[]): Promise<{
  exitCode: number; allOutput: string; lastResultText: string;
}> {
  return runProviderAgent(prompt, ws, statusRef, model, effort, disallowedTools);
}

export async function runProviderAgent(prompt: string, ws: WebSocket | null, statusRef: { value: string }, model?: string, effort?: string, disallowedTools?: string[]): Promise<{
  exitCode: number; allOutput: string; lastResultText: string;
}> {
  const runner = getProviderRunner();
  const runCommand = runner.buildRunCommand(prompt, model, effort, disallowedTools);
  let allOutput = '';
  let lastResultText = '';
  let stderrBuffer = '';
  let timedOut = false;

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
  case "\$1" in
    --page) PAGE="\$2"; shift 2 ;;
    --severity) SEVERITY="\$2"; shift 2 ;;
    *) CONTENT="\${CONTENT:+\$CONTENT }\$1"; shift ;;
  esac
done

if [ -z "\$CONTENT" ]; then
  echo "Error: content is required" >&2; exit 1
fi

API="\${ORCHESTRATOR_URL:?ORCHESTRATOR_URL not set}"
TOKEN="\${AGENT_API_TOKEN:?AGENT_API_TOKEN not set}"
TASK="\${TASK_ID:?TASK_ID not set}"
CURL_OPTS="-f -sS --max-time 15 --connect-timeout 10"

# Build JSON with printf to avoid shell expansion issues
JSON="{\\"task_id\\":\\"\$TASK\\",\\"annotation_type\\":\\"\$TYPE\\",\\"content\\":\\"\$(echo "\$CONTENT" | sed 's/\\\\/\\\\\\\\/g; s/"/\\\\"/g')\\""
[ -n "\$PAGE" ] && JSON="\${JSON},\\"target_page\\":\\"\$PAGE\\""
[ -n "\$SEVERITY" ] && JSON="\${JSON},\\"severity\\":\\"\$SEVERITY\\""
JSON="\${JSON}}"

printf '%s' "\$JSON" | \\
  curl \$CURL_OPTS -X POST "\$API/api/agent/annotate" \\
    -H "Content-Type: application/json" \\
    -H "X-Agent-Token: \$TOKEN" \\
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
      fs.writeFileSync(promptFile, withExecutionModePrompt(prompt));
      promptFd = fs.openSync(promptFile, 'r');
    }

    child = spawn(runCommand.command, runCommand.args, {
      cwd: WORKSPACE,
      env: runCommand.env,
      detached: true,
      stdio: [runCommand.promptInput === 'stdin' ? 'pipe' : (promptFd ?? 'ignore'), 'pipe', 'pipe'],
    });
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
    const killTimer = setTimeout(() => {
      log(`${runCommand.displayName} process timed out after ${timeoutMinutes} minutes — killing process group`);
      sendLog(ws, `\n[TIMEOUT] ${runCommand.displayName} exceeded ${timeoutMinutes}-minute time limit\n`);
      timedOut = true;
      const pid = child?.pid;
      if (pid) { try { process.kill(-pid, 'SIGTERM'); } catch { /* ignore */ } }
      setTimeout(() => { if (pid) { try { process.kill(-pid, 'SIGKILL'); } catch { /* ignore */ } } }, 5000);
    }, timeoutMs);

    child.stdout!.on('data', (data: Buffer) => {
      const text = data.toString();
      if (allOutput.length < MAX_OUTPUT_BYTES) allOutput += text;
      if (runCommand.outputMode === 'claude-stream-json') {
        for (const line of text.split('\n').filter(Boolean)) {
          try {
            const obj = JSON.parse(line) as ClaudeOutput;
            if (obj.type === 'assistant' && obj.message?.content) {
              for (const block of obj.message.content) {
                if (block.type === 'text') {
                  sendLog(ws, (block.text ?? '') + '\n');
                  statusRef.value = (block.text ?? '').slice(0, 100);
                } else if (block.type === 'tool_use') {
                  sendLog(ws, `[tool: ${strVal(block.name)}] ${JSON.stringify(block.input).slice(0, 200)}\n`);
                  statusRef.value = `Using tool: ${strVal(block.name)}`;
                  recordToolUse(strVal(block.name), block.input);
                }
              }
            } else if (obj.type === 'result') {
              lastResultText = obj.result ?? '';
              sendLog(ws, `\n--- Result ---\n${lastResultText}\n`);
            }
          } catch {
            sendLog(ws, text);
          }
        }
      } else {
        sendLog(ws, text);
        const trimmed = text.trim();
        if (trimmed) {
          lastResultText = `${lastResultText}${text}`.trim();
          statusRef.value = trimmed.slice(0, 100);
        }
      }
    });

    child.stderr!.on('data', (data: Buffer) => {
      const text = sanitize(data.toString());
      if (allOutput.length < MAX_OUTPUT_BYTES) allOutput += text;
      if (stderrBuffer.length < 50_000) stderrBuffer += text;
      sendLog(ws, `[stderr] ${text}`);
    });

    child.on('error', (err) => { clearTimeout(killTimer); sendLog(ws, `[error] ${err.message}\n`); resolve(1); });
    child.on('close', (code) => {
      clearTimeout(killTimer);
      if (promptFile) {
        try { fs.unlinkSync(promptFile); } catch { /* ignore */ }
      }
      log(`${runCommand.displayName} exited with code ${code ?? 'null'}`);
      resolve(code ?? 1);
    });
  });

  sendLog(ws, `\n--- ${runner.displayName} exited (code: ${exitCode}) ---\n`);

  if (timedOut) {
    throw new ClaudeTimeoutError(`${runner.displayName} exceeded ${runCommand.timeoutMs / 60000}-minute time limit`);
  }

  if (exitCode !== 0) {
    const combined = stderrBuffer + allOutput.slice(-5000);
    if (matchesRateLimit(combined, runner.provider, runner.authMode)) {
      const snippet = (stderrBuffer || allOutput.slice(-500)).slice(0, 500);
      throw new RateLimitError(`${runner.displayName} API usage exhausted — ${snippet}`);
    }
  }

  return { exitCode, allOutput, lastResultText: lastResultText || allOutput.trim() };
}

// ── Main ────────────────────────────────────────────────────────────────────
async function main() {
  const runner = getProviderRunner();
  log(`Blueprint Agent starting (VM: ${VM_INSTANCE_ID})`);
  log(`Registered handlers (${Object.keys(handlers).length}): ${Object.keys(handlers).join(', ')}`);
  log(`Agent source: ${import.meta.url}`);
  if (!AGENT_TOKEN) throw new Error('AGENT_SECRET_TOKEN is required');

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
  log(runner.credentialSummary());
  await validateProviderAuth(runner, reportPreClaimFailure, process.env.LLM_MODEL);
  if (PRE_CLAIM_TASK_ID) {
    sendHeartbeatSync(PRE_CLAIM_TASK_ID, 'AGENT_AUTH_OK: provider validation complete');
  }

  // Retry any stale result files from previous runs (network failure / VM kill mid-POST)
  try {
    const staleFiles = fs.readdirSync('/tmp').filter(f => /^task_.+_result\.json$/.test(f));
    for (const file of staleFiles) {
      const filePath = `/tmp/${file}`;
      try {
        const raw = fs.readFileSync(filePath, 'utf-8');
        const stalePayload = JSON.parse(raw) as Record<string, unknown>;
        const staleTaskId = strVal(stalePayload.task_id) || file.replace(/^task_/, '').replace(/_result\.json$/, '');
        log(`Found stale result file for task ${staleTaskId} — retrying POST`);
        await apiPost('/api/agent/status', stalePayload);
        try { fs.unlinkSync(filePath); } catch { /* ignore */ }
        log(`Stale result for task ${staleTaskId} posted successfully`);
      } catch (e) {
        log(`WARNING: Failed to retry stale result from ${file}: ${String(e)} — reaper will handle it`);
      }
    }
  } catch (e) {
    log(`WARNING: Stale result scan failed: ${String(e)}`);
  }

  // Claim task. Wrap in try/catch so a claim failure (network, 4xx, 5xx) is
  // reported via pre-claim heartbeat rather than dying silently. Without this
  // the task stays in PROVISIONING until the reaper times out with no evidence.
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
  (task as Record<string, unknown>).build_config = buildConfig;
  log(`Claimed task ${taskId} (type: ${taskType}): "${strVal(task.title)}"`);

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

    // Dispatch to handler (fall back to create for unknown types)
    const handlersMap: Partial<Record<string, Handler>> = handlers;
    const knownHandler = handlersMap[taskType];
    if (!knownHandler) sendLog(ws, `Warning: unknown task type "${taskType}", using create handler\n`);
    const handler = knownHandler ?? handlers.create;
    await handler(taskId, payload, task, ghToken, owner, repo, ws, statusRef, model, effort);
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
  } finally {
    stopHeartbeat();
    if (ws) ws.close();
    // Clean up workspace to avoid leaking tokens in git config
    try { execFileSync('rm', ['-rf', WORKSPACE], { encoding: 'utf-8', timeout: 30_000 }); } catch { /* ignore */ }
    // Clean up /tmp files written by Claude Code handlers (task-specific paths)
    try { fs.unlinkSync(`/tmp/agent_prompt_${taskId}.txt`); } catch { /* ignore */ }
    for (const name of ['review_verdict', 'plan_output', 'research_findings', 'security_findings', 'smoketest_result', 'debug_output', 'perf_output', 'investigation_report', 'strategic_output', 'design_doc_output']) {
      try { fs.unlinkSync(`/tmp/${name}_${taskId}.json`); } catch { /* ignore */ }
    }
    log('Agent shutdown complete.');
  }
}

// Global crash handlers — report errors via heartbeat before dying.
// process.exit() skips the finally block, so the crash handler is the
// ONLY chance to surface forensic evidence to the backend.  Without this,
// every failure mode (uncaughtException, SIGTERM, cgroup OOM SIGKILL)
// looks identical: heartbeats stop, task stays IN_PROGRESS, no evidence.
function _crashReport(label: string, detail: string, exitCode: number) {
  console.error(`[FATAL] ${label}: ${detail}`);
  // Synchronous curl — fire-and-forget fetch() couldn't guarantee the write
  // flushed before process.exit, so crashes would show up as silent heartbeat
  // timeouts instead of explained failures. sendHeartbeatSync blocks on curl
  // (5s max) so the request lands before we exit.
  const taskId = process.env.TASK_ID;
  if (taskId) {
    try {
      const metrics = collectMetrics();
      const msg = `[FATAL:${label}] ${detail.slice(0, 300)}`;
      sendHeartbeatSync(taskId, `[${metrics.heap_used_mb}MB cg:${metrics.cgroup_mb || '?'}MB] ${msg}`);
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
