import { spawn, execFileSync } from 'child_process';
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
  TEST_RUNNER_TIMEOUT,
  CLAUDE_TIMEOUT_MS, MAX_OUTPUT_BYTES,
} from './constants.js';

// ── Failure categories ───────────────────────────────────────────────────────
export type FailureCategory = 'clone_failed' | 'build_failed' | 'test_failed' | 'lint_failed' | 'pr_creation_failed' | 'claude_code_failed' | 'parse_failed' | 'fatal' | 'rate_limited';

// Exact substrings from Claude Code CLI / Anthropic API rate-limit messages.
// Must stay in sync with backend/app/max_plan_pool.py:RATE_LIMIT_PATTERNS.
// Do NOT add loose keywords like "credit", "billing", "429", or "overloaded" —
// those cause false positives that disable accounts for unrelated failures.
const RATE_LIMIT_PATTERNS: readonly string[] = [
  "you've hit your limit",
  "you've hit your usage limit",
  "claude ai usage limit reached",
  "you're out of extra usage",
  "5-hour limit reached",
  "api error: rate limit reached",
  "this request would exceed your account's rate limit",
] as const;

export function matchesRateLimit(text: string): boolean {
  const lower = text.toLowerCase();
  return RATE_LIMIT_PATTERNS.some(p => lower.includes(p));
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
      // Other 4xx errors are not transient — fail immediately
      if (res.status < 500) {
        throw new Error(`POST ${path} failed (${res.status}): ${text}`);
      }
      // 5xx: retry if we have attempts left
      if (attempt === retries - 1) {
        throw new Error(`POST ${path} failed (${res.status}): ${text}`);
      }
    } catch (e) {
      // Re-throw 4xx errors and final-attempt errors immediately
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
  fs.writeFileSync(resultPath, JSON.stringify(payload), 'utf-8');
  try {
    const result = await apiPost('/api/agent/status', payload);
    try { fs.unlinkSync(resultPath); } catch { /* ignore — file already gone */ }
    return result;
  } catch (e) {
    // File intentionally left on disk — retried on next agent startup
    throw e;
  }
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
export function sendHeartbeatSync(taskId: string, statusMessage: string) {
  try {
    const metrics = collectMetrics();
    const payload = JSON.stringify({
      task_id: taskId,
      status_message: `[${metrics.heap_used_mb}MB] ${statusMessage}`,
      metrics,
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

function appendToLogBuffer(content: string) {
  if (logBuffer.length >= MAX_LOG_BUFFER) logBuffer.shift();
  logBuffer.push({ timestamp: Date.now(), content });
}

function flushLogBuffer(ws: WebSocket) {
  while (logBuffer.length > 0) {
    const entry = logBuffer.shift();
    if (!entry) break;
    try { ws.send(JSON.stringify({ type: 'log', content: entry.content })); } catch { break; }
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
  await cloneWithRetry(['clone', '--depth', '100', '--branch', baseBranch, cloneUrl, WORKSPACE]);
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
    const prRes = await apiPost('/api/agent/github/pull-requests', {
      task_id: taskId, owner, repo, title,
      head: workBranch, base: baseBranch, body,
    }, API_MAX_RETRIES, 90_000) as { data?: { html_url?: string } };
    prUrl = prRes.data?.html_url ?? '';
    sendLog(ws, `PR created: ${prUrl}\n`);
  } catch (e) {
    sendLog(ws, `Failed to create PR: ${String(e)}\n`);
    throw new Error(`PR creation failed: ${String(e)}`);
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

/** Run lint checks in a loop, asking Claude to fix failures up to MAX_LINT_FIX_ATTEMPTS times. */
export async function lintFixLoop(
  changedPaths: string[], ws: WebSocket | null, statusRef: { value: string },
  context: string = 'the code you just wrote', model?: string, effort?: string,
  buildConfig?: BuildConfig
): Promise<boolean> {
  for (let attempt = 0; attempt < MAX_LINT_FIX_ATTEMPTS; attempt++) {
    try {
      statusRef.value = attempt === 0 ? 'Running pre-commit checks...' : `Re-running lint (attempt ${attempt + 1}/${MAX_LINT_FIX_ATTEMPTS})...`;
      runProjectLintChecks(changedPaths, ws, buildConfig);
      return true;
    } catch (lintErr: unknown) {
      const msg = lintErr instanceof Error ? lintErr.message : String(lintErr);
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

/**
 * Detect the project's test runner, run tests, and retry via Claude on failure.
 * Returns whether tests passed and whether failures were detected (for commit note).
 */
export async function testFixLoop(
  ws: WebSocket | null, statusRef: { value: string },
  model?: string, effort?: string, buildConfig?: BuildConfig
): Promise<{ passed: boolean; testFailuresDetected: boolean; failureSummary?: string }> {
  const { existsSync, readFileSync } = fs;

  interface TestRunner {
    cmd: string;
    args: string[];
    cwd: string;
    label: string;
  }

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

  if (runners.length === 0) {
    sendLog(ws, 'No test runner detected — skipping test step.\n');
    return { passed: true, testFailuresDetected: false };
  }

  sendLog(ws, `Detected test runner(s): ${runners.map(r => r.label).join(', ')}\n`);

  for (let attempt = 0; attempt <= MAX_TEST_FIX_ATTEMPTS; attempt++) {
    statusRef.value = attempt === 0 ? 'Running test suite...' : `Re-running tests (attempt ${attempt + 1}/${MAX_TEST_FIX_ATTEMPTS + 1})...`;
    sendLog(ws, `Running tests (attempt ${attempt + 1}/${MAX_TEST_FIX_ATTEMPTS + 1})...\n`);

    const failures: string[] = [];
    const taskId = process.env.TASK_ID || '';
    for (const runner of runners) {
      try {
        sendHeartbeatSync(taskId, `Running tests [${runner.label}]...`);
        execFileSync(runner.cmd, runner.args, { cwd: runner.cwd, encoding: 'utf-8', timeout: TEST_RUNNER_TIMEOUT });
        sendLog(ws, `Tests passed [${runner.label}].\n`);
      } catch (testErr: unknown) {
        const msg = testErr instanceof Error ? testErr.message : String(testErr);
        failures.push(`[${runner.label}]:\n${msg}`);
      }
    }

    if (failures.length === 0) {
      sendLog(ws, 'All tests passed.\n');
      return { passed: true, testFailuresDetected: false };
    }

    const combinedFailures = failures.join('\n\n');
    if (attempt < MAX_TEST_FIX_ATTEMPTS) {
      sendLog(ws, `Tests failed (attempt ${attempt + 1}/${MAX_TEST_FIX_ATTEMPTS + 1}):\n${combinedFailures.slice(0, 3000)}\nClaude will attempt to fix...\n`);
      await runClaudeCode(
        `Refactoring broke these tests. Fix ONLY to restore passing tests without reverting the refactor's intent:\n\n${combinedFailures.slice(0, 5000)}`,
        ws, statusRef, model, effort
      );
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

export async function runClaudeCode(prompt: string, ws: WebSocket | null, statusRef: { value: string }, model?: string, effort?: string, disallowedTools?: string[]): Promise<{
  exitCode: number; allOutput: string; lastResultText: string;
}> {
  let allOutput = '';
  let lastResultText = '';
  let stderrBuffer = '';

  sendLog(ws, `\n--- Claude Code starting ---\nPrompt: ${prompt.slice(0, 200)}${prompt.length > 200 ? '...' : ''}\n\n`);
  log('Spawning Claude Code CLI...');

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
    const claudeEnv: Record<string, string> = {
      HOME: process.env.HOME || '/home/agent',
      PATH: process.env.PATH || '/usr/local/bin:/usr/bin:/bin',
      LANG: process.env.LANG || 'en_US.UTF-8',
      TERM: process.env.TERM || 'xterm-256color',
    };
    if (process.env.CLAUDE_CODE_OAUTH_TOKEN) {
      claudeEnv.CLAUDE_CODE_OAUTH_TOKEN = process.env.CLAUDE_CODE_OAUTH_TOKEN;
    }
    if (process.env.ANTHROPIC_API_KEY) {
      claudeEnv.ANTHROPIC_API_KEY = process.env.ANTHROPIC_API_KEY;
    }
    if (process.env.ORCHESTRATOR_URL) {
      claudeEnv.ORCHESTRATOR_URL = process.env.ORCHESTRATOR_URL;
    }
    // Pass agent token via env (not disk) so helpers like ./design-doc can auth
    if (AGENT_TOKEN) {
      claudeEnv.AGENT_API_TOKEN = AGENT_TOKEN;
    }
    if (process.env.TASK_ID) {
      claudeEnv.TASK_ID = process.env.TASK_ID;
    }

    // Write prompt to a temp file and pipe via stdin to avoid E2BIG when
    // the prompt (design doc + project context + directions) exceeds ARG_MAX (~128KB).
    const promptFile = `/tmp/agent_prompt_${process.env.TASK_ID || 'unknown'}.txt`;
    fs.writeFileSync(promptFile, prompt);

    const args = ['-p', '--output-format', 'stream-json', '--verbose', '--dangerously-skip-permissions'];
    if (model) args.push('--model', model);
    if (effort) args.push('--effort', effort);
    if (disallowedTools && disallowedTools.length > 0) {
      for (const tool of disallowedTools) {
        args.push('--disallowedTools', tool);
      }
    }
    const promptFd = fs.openSync(promptFile, 'r');
    const claude = spawn('claude', args, { cwd: WORKSPACE, env: claudeEnv, detached: true, stdio: [promptFd, 'pipe', 'pipe'] });
    fs.closeSync(promptFd);

    // Kill Claude if it exceeds 45 minutes — kill entire process group (negative PID)
    // so child processes (npm, pytest, etc.) are also terminated
    const killTimer = setTimeout(() => {
      log('Claude Code process timed out after 45 minutes — killing process group');
      sendLog(ws, '\n[TIMEOUT] Claude Code exceeded 45-minute time limit\n');
      const pid = claude.pid;
      if (pid) { try { process.kill(-pid, 'SIGTERM'); } catch { /* ignore */ } }
      setTimeout(() => { if (pid) { try { process.kill(-pid, 'SIGKILL'); } catch { /* ignore */ } } }, 5000);
    }, CLAUDE_TIMEOUT_MS);

    claude.stdout!.on('data', (data: Buffer) => {
      const text = data.toString();
      if (allOutput.length < MAX_OUTPUT_BYTES) allOutput += text;
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
              }
            }
          } else if (obj.type === 'result') {
            lastResultText = obj.result ?? '';
            sendLog(ws, `\n--- Result ---\n${lastResultText}\n`);
          }
        } catch { sendLog(ws, text); }
      }
    });

    claude.stderr!.on('data', (data: Buffer) => {
      const text = sanitize(data.toString());
      if (allOutput.length < MAX_OUTPUT_BYTES) allOutput += text;
      if (stderrBuffer.length < 50_000) stderrBuffer += text;
      sendLog(ws, `[stderr] ${text}`);
    });

    claude.on('error', (err) => { clearTimeout(killTimer); sendLog(ws, `[error] ${err.message}\n`); resolve(1); });
    claude.on('close', (code) => { clearTimeout(killTimer); log(`Claude Code exited with code ${code ?? 'null'}`); resolve(code ?? 1); });
  });

  sendLog(ws, `\n--- Claude Code exited (code: ${exitCode}) ---\n`);

  // Detect Claude rate-limit / usage exhaustion from stderr using exact
  // substrings from RATE_LIMIT_PATTERNS.
  if (exitCode !== 0) {
    const combined = stderrBuffer + allOutput.slice(-5000);
    if (matchesRateLimit(combined)) {
      const snippet = (stderrBuffer || allOutput.slice(-500)).slice(0, 500);
      throw new RateLimitError(`Claude API usage exhausted — ${snippet}`);
    }
  }

  return { exitCode, allOutput, lastResultText };
}

// ── Main ────────────────────────────────────────────────────────────────────
async function main() {
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

  // Auth: CLAUDE_CODE_OAUTH_TOKEN env var is read directly by Claude Code CLI (no file needed)
  if (process.env.CLAUDE_CODE_OAUTH_TOKEN) {
    log('Using OAuth token for Claude Code auth');
  } else if (process.env.ANTHROPIC_API_KEY) {
    log('Using ANTHROPIC_API_KEY as fallback auth');
  }

  // Pre-validate token before claiming task — catch dead tokens in ~5s instead of 2+ min
  try {
    const valEnv: Record<string, string> = {
      HOME: process.env.HOME || '/home/agent',
      PATH: process.env.PATH || '/usr/local/bin:/usr/bin:/bin',
      LANG: process.env.LANG || 'en_US.UTF-8',
      TERM: process.env.TERM || 'xterm-256color',
    };
    if (process.env.CLAUDE_CODE_OAUTH_TOKEN) valEnv.CLAUDE_CODE_OAUTH_TOKEN = process.env.CLAUDE_CODE_OAUTH_TOKEN;
    if (process.env.ANTHROPIC_API_KEY) valEnv.ANTHROPIC_API_KEY = process.env.ANTHROPIC_API_KEY;

    log('Validating Claude auth token...');
    const valResult = await new Promise<{exitCode: number; stderr: string}>((resolve) => {
      let stderr = '';
      const check = spawn('claude', ['-p', 'respond with OK', '--max-turns', '1', '--output-format', 'stream-json', '--dangerously-skip-permissions'],
        { cwd: '/tmp', env: valEnv });
      const valTimeout = setTimeout(() => { try { check.kill(); } catch { /* ignore */ } }, 30000);
      check.stderr.on('data', (d: Buffer) => { stderr += d.toString(); });
      check.on('close', (code) => { clearTimeout(valTimeout); resolve({ exitCode: code ?? 1, stderr }); });
    });
    if (valResult.exitCode !== 0 && matchesRateLimit(valResult.stderr)) {
      log(`Token validation FAILED — account rate-limited: ${valResult.stderr.slice(0, 200)}`);
      // Report the rate-limit before exiting so the reaper's status message has
      // forensic evidence instead of a silent provision failure.
      reportPreClaimFailure(`token validation rate-limited: ${valResult.stderr.slice(0, 200)}`);
      // Exit immediately — reaper will catch the task in PROVISIONING and requeue.
      // Don't waste 2+ minutes of setup only to discover the same thing.
      process.exit(1);
    }
    log('Token validation OK');
  } catch (e) {
    log(`Token validation error (continuing): ${e instanceof Error ? e.message : String(e)}`);
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
  const model = payload.model ? strVal(payload.model) : undefined;
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
    const failureCategory: FailureCategory = isRateLimit ? 'rate_limited' : 'fatal';
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
  process.exit(exitCode);
}

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
