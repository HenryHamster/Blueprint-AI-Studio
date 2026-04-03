import { spawn, execFileSync, type ChildProcess } from 'child_process';
import fs from 'fs';
import WebSocket from 'ws';

// ── Environment ─────────────────────────────────────────────────────────────
const ORCHESTRATOR_URL = process.env.ORCHESTRATOR_URL || 'http://localhost:8080';
let AGENT_TOKEN = process.env.AGENT_SECRET_TOKEN || '';
const VM_INSTANCE_ID = process.env.VM_INSTANCE_ID || 'local';
const WORKSPACE = '/tmp/workspace';

const apiHeaders: Record<string, string> = {
  'Content-Type': 'application/json',
  'X-Agent-Token': AGENT_TOKEN,
};

// ── API Helpers ─────────────────────────────────────────────────────────────
async function tryRefreshToken(): Promise<boolean> {
  try {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 30_000);
    const res = await fetch(`${ORCHESTRATOR_URL}/api/agent/refresh-token`, {
      method: 'POST', headers: { ...apiHeaders }, body: JSON.stringify({}), signal: controller.signal,
    });
    clearTimeout(timeout);
    if (!res.ok) return false;
    const data = await res.json() as { token?: string };
    if (data.token) {
      AGENT_TOKEN = data.token;
      apiHeaders['X-Agent-Token'] = AGENT_TOKEN;
      log('Agent token refreshed successfully');
      return true;
    }
    return false;
  } catch (e) {
    log(`Token refresh failed: ${e}`);
    return false;
  }
}

async function apiPost(path: string, body: Record<string, unknown> = {}, retries = 5) {
  let tokenRefreshed = false;
  for (let attempt = 0; attempt < retries; attempt++) {
    try {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 30_000);
      const res = await fetch(`${ORCHESTRATOR_URL}${path}`, {
        method: 'POST', headers: apiHeaders, body: JSON.stringify(body), signal: controller.signal,
      });
      clearTimeout(timeout);
      if (res.ok) return res.json();
      const text = await res.text();
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

async function apiGet(path: string, retries = 5) {
  let tokenRefreshed = false;
  for (let attempt = 0; attempt < retries; attempt++) {
    try {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 30_000);
      const res = await fetch(`${ORCHESTRATOR_URL}${path}`, { headers: apiHeaders, signal: controller.signal });
      clearTimeout(timeout);
      if (res.ok) return res.json();
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

function log(msg: string) {
  console.log(`[${new Date().toISOString()}] ${msg}`);
}

// ── Heartbeat ───────────────────────────────────────────────────────────────
let heartbeatTimer: ReturnType<typeof setInterval> | null = null;

function collectMetrics(): { rss_mb: number; heap_used_mb: number; disk_free_mb?: number; disk_total_mb?: number } {
  const mem = process.memoryUsage();
  const rss_mb = Math.round(mem.rss / 1024 / 1024 * 10) / 10;
  const heap_used_mb = Math.round(mem.heapUsed / 1024 / 1024 * 10) / 10;
  try {
    const dfOut = execFileSync('df', ['-k', '/tmp'], { timeout: 5000 }).toString();
    const line = dfOut.split('\n')[1];
    if (line) {
      const parts = line.trim().split(/\s+/);
      const disk_total_mb = Math.round(parseInt(parts[1], 10) / 1024);
      const disk_free_mb = Math.round(parseInt(parts[3], 10) / 1024);
      return { rss_mb, heap_used_mb, disk_free_mb, disk_total_mb };
    }
  } catch { /* disk metrics are best-effort */ }
  return { rss_mb, heap_used_mb };
}

function startHeartbeat(taskId: string, getMessage: () => string) {
  heartbeatTimer = setInterval(async () => {
    try {
      const metrics = collectMetrics();
      await apiPost('/api/agent/heartbeat', {
        task_id: taskId,
        status_message: `[${metrics.heap_used_mb}MB] ${getMessage()}`,
        metrics,
      });
    } catch (e: any) {
      log(`Heartbeat failed: ${e}`);
      // 4xx = task reassigned to new VM (404) or cancelled (409) — exit immediately
      if (e instanceof Error && /failed \(4\d{2}\)/.test(e.message)) {
        log('Task no longer assigned to this VM — self-terminating');
        process.exit(0);
      }
    }
  }, 30_000);
}

function stopHeartbeat() {
  if (heartbeatTimer) { clearInterval(heartbeatTimer); heartbeatTimer = null; }
}

// ── WebSocket Log Stream ────────────────────────────────────────────────────
const MAX_LOG_BUFFER = 1000;
interface LogEntry { timestamp: number; content: string; }
const logBuffer: LogEntry[] = [];

function appendToLogBuffer(content: string) {
  if (logBuffer.length >= MAX_LOG_BUFFER) logBuffer.shift();
  logBuffer.push({ timestamp: Date.now(), content });
}

function flushLogBuffer(ws: WebSocket) {
  while (logBuffer.length > 0) {
    const entry = logBuffer.shift()!;
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
    } catch {}
    if (attempt < maxRetries - 1) {
      await new Promise(r => setTimeout(r, baseDelay * Math.pow(2, attempt)));
    }
  }
  return null;
}

function sanitize(text: string): string {
  return text
    .replace(/https:\/\/x-access-token:[^@]+@/g, 'https://x-access-token:***@')
    .replace(/ghp_[A-Za-z0-9_]+/g, 'ghp_***')
    .replace(/ghs_[A-Za-z0-9_]+/g, 'ghs_***')
    .replace(/github_pat_[A-Za-z0-9_]+/g, 'github_pat_***')
    .replace(/sk-ant-[A-Za-z0-9_\-]+/g, 'sk-ant-***')
    .replace(/eyJ[A-Za-z0-9_\-]+\.eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+/g, 'eyJ***');
}

function validateBranchName(branch: string, label: string): string {
  if (!branch) throw new Error(`${label} is required`);
  if (!/^[a-zA-Z0-9_./-]+$/.test(branch)) throw new Error(`Invalid ${label}: ${branch}`);
  if (branch.includes('..')) throw new Error(`Invalid ${label} (contains ..): ${branch}`);
  if (branch.startsWith('-')) throw new Error(`Invalid ${label} (starts with -): ${branch}`);
  return branch;
}

function sendLog(ws: WebSocket | null, content: string) {
  const sanitized = sanitize(content);
  appendToLogBuffer(sanitized);
  if (ws && ws.readyState === WebSocket.OPEN) {
    try { ws.send(JSON.stringify({ type: 'log', content: sanitized })); } catch {}
  } else {
    process.stdout.write(sanitized);
  }
}

// ── Git Helpers ─────────────────────────────────────────────────────────────
function git(args: string[], cwd = WORKSPACE): string {
  return execFileSync('git', args, { cwd, encoding: 'utf-8', timeout: 120_000 }).trim();
}

// ── Workspace + PR Helpers ───────────────────────────────────────────────────
async function setupWorkspace(
  owner: string, repo: string, ghToken: string, baseBranch: string,
  branchPrefix: string, taskId: string,
  ws: WebSocket | null, statusRef: { value: string }
): Promise<{ workBranch: string; shortId: string }> {
  statusRef.value = `Cloning ${owner}/${repo}...`;
  sendLog(ws, `Cloning ${owner}/${repo}...\n`);
  execFileSync('rm', ['-rf', WORKSPACE], { encoding: 'utf-8' });
  const cloneUrl = `https://x-access-token:${ghToken}@github.com/${owner}/${repo}.git`;
  execFileSync('git', ['clone', '--depth', '100', '--branch', baseBranch, cloneUrl, WORKSPACE], { encoding: 'utf-8', timeout: 300_000 });
  git(['config', 'user.email', 'blueprint-agent@automated.dev']);
  git(['config', 'user.name', 'Blueprint Agent']);
  const shortId = taskId.replace(/-/g, '').slice(0, 8);
  const workBranch = `blueprint/${branchPrefix}-${shortId}`;
  // Clean up stale remote branch from previous attempt (requeue scenario)
  let staleBranchExists = false;
  try {
    git(['ls-remote', '--exit-code', 'origin', workBranch]);
    staleBranchExists = true;
  } catch { /* branch doesn't exist on remote — normal case */ }
  if (staleBranchExists) {
    try {
      git(['push', 'origin', '--delete', workBranch]);
      sendLog(ws, `Deleted stale remote branch ${workBranch} from previous attempt\n`);
    } catch (e) {
      sendLog(ws, `WARNING: Could not delete stale remote branch ${workBranch}: ${e instanceof Error ? e.message : e}\n`);
    }
  }
  git(['checkout', '-b', workBranch]);
  sendLog(ws, `Working branch: ${workBranch}\n`);
  return { workBranch, shortId };
}

async function pushAndCreatePR(
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
      git(['rebase', `origin/${baseBranch}`]);
      sendLog(ws, `Rebased onto latest ${baseBranch}\n`);
    } catch {
      try { git(['rebase', '--abort']); } catch {}
      sendLog(ws, `Rebase onto ${baseBranch} had conflicts — trying merge instead\n`);
      try {
        git(['merge', `origin/${baseBranch}`, '--no-edit']);
        sendLog(ws, `Merged ${baseBranch} successfully\n`);
      } catch {
        try { git(['merge', '--abort']); } catch {}
        sendLog(ws, `Merge also conflicted — pushing as-is, review pipeline will handle\n`);
      }
    }

    // Verify no conflict markers before pushing
    try {
      const markers = execFileSync('git', ['-C', WORKSPACE, 'grep', '-rl', '<' + '<<<<<<', '--', '*.ts', '*.tsx', '*.py', '*.js', '*.jsx', '*.json'], { encoding: 'utf-8', timeout: 10_000 }).trim();
      if (markers) {
        sendLog(ws, `WARNING: Conflict markers found in:\n${markers}\nAborting rebase and pushing original commit.\n`);
        try { git(['rebase', '--abort']); } catch {}
      }
    } catch { /* no markers found */ }
  }

  const freshGhRes = await apiGet(`/api/agent/github/token?task_id=${taskId}`);
  const pushUrl = `https://x-access-token:${freshGhRes.data.token}@github.com/${owner}/${repo}.git`;
  execFileSync('git', ['-C', WORKSPACE, 'remote', 'set-url', 'origin', pushUrl], { encoding: 'utf-8' });
  git(['push', 'origin', workBranch]);
  sendLog(ws, `Pushed to branch: ${workBranch}\n`);

  statusRef.value = 'Creating pull request...';
  let prUrl = '';
  try {
    const prRes = await apiPost('/api/agent/github/pull-requests', {
      task_id: taskId, owner, repo, title,
      head: workBranch, base: baseBranch, body,
    });
    prUrl = prRes.data?.html_url || '';
    sendLog(ws, `PR created: ${prUrl}\n`);
  } catch (e) {
    sendLog(ws, `Failed to create PR: ${e}\n`);
    throw new Error(`PR creation failed: ${e}`);
  }

  return prUrl;
}

// ── Lint / Build Checks ─────────────────────────────────────────────────────
const MAX_LINT_FIX_ATTEMPTS = 3;

/** Run lint/build checks on changed files. Throws with error details on failure. */
function runProjectLintChecks(changedPaths: string[], ws: WebSocket | null) {
  const touchesFrontend = changedPaths.some(p => p.startsWith('admin-ui/'));
  const touchesAgent = changedPaths.some(p => p.startsWith('agent/'));
  const touchesBackend = changedPaths.some(p => p.startsWith('backend/'));
  if (touchesFrontend) {
    sendLog(ws, 'Running frontend lint + build...\n');
    execFileSync('npm', ['ci'], { cwd: `${WORKSPACE}/admin-ui`, encoding: 'utf-8', timeout: 120_000 });
    execFileSync('npx', ['eslint', '.'], { cwd: `${WORKSPACE}/admin-ui`, encoding: 'utf-8', timeout: 60_000 });
    execFileSync('npm', ['run', 'build'], { cwd: `${WORKSPACE}/admin-ui`, encoding: 'utf-8', timeout: 120_000, env: { ...process.env, VITE_API_URL: 'http://localhost:8080', VITE_FIREBASE_API_KEY: 'ci', VITE_FIREBASE_AUTH_DOMAIN: 'ci.firebaseapp.com', VITE_FIREBASE_PROJECT_ID: 'ci' } });
    sendLog(ws, 'Frontend checks passed.\n');
  }
  if (touchesAgent) {
    sendLog(ws, 'Running agent type check...\n');
    execFileSync('npm', ['ci'], { cwd: `${WORKSPACE}/agent`, encoding: 'utf-8', timeout: 120_000 });
    execFileSync('npx', ['tsc', '--noEmit'], { cwd: `${WORKSPACE}/agent`, encoding: 'utf-8', timeout: 60_000 });
    sendLog(ws, 'Agent type check passed.\n');
  }
  if (touchesBackend) {
    sendLog(ws, 'Running backend ruff lint...\n');
    try {
      const lintOut = execFileSync('python', ['-m', 'ruff', 'check', 'backend/', '--select', 'E,F,W', '--ignore', 'E501'], { cwd: WORKSPACE, encoding: 'utf-8', timeout: 60_000 });
      sendLog(ws, lintOut || 'Backend ruff lint passed.\n');
    } catch (err: unknown) {
      if ((err as NodeJS.ErrnoException).code === 'ENOENT') {
        sendLog(ws, 'ruff not installed, skipping backend lint.\n');
      } else {
        throw err;
      }
    }
    sendLog(ws, 'Running backend ruff format check...\n');
    try {
      const fmtOut = execFileSync('python', ['-m', 'ruff', 'format', '--check', 'backend/'], { cwd: WORKSPACE, encoding: 'utf-8', timeout: 60_000 });
      sendLog(ws, fmtOut || 'Backend ruff format check passed.\n');
    } catch (err: unknown) {
      if ((err as NodeJS.ErrnoException).code === 'ENOENT') {
        sendLog(ws, 'ruff not installed, skipping backend format check.\n');
      } else {
        throw err;
      }
    }
  }
}

/** Run lint checks in a loop, asking Claude to fix failures up to MAX_LINT_FIX_ATTEMPTS times. */
async function lintFixLoop(
  changedPaths: string[], ws: WebSocket | null, statusRef: { value: string },
  context: string = 'the code you just wrote', model?: string, effort?: string
): Promise<boolean> {
  for (let attempt = 0; attempt < MAX_LINT_FIX_ATTEMPTS; attempt++) {
    try {
      statusRef.value = attempt === 0 ? 'Running pre-commit checks...' : `Re-running lint (attempt ${attempt + 1}/${MAX_LINT_FIX_ATTEMPTS})...`;
      runProjectLintChecks(changedPaths, ws);
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

const MAX_TEST_FIX_ATTEMPTS = 2;

/**
 * Detect the project's test runner, run tests, and retry via Claude on failure.
 * Returns whether tests passed and whether failures were detected (for commit note).
 */
async function testFixLoop(
  ws: WebSocket | null, statusRef: { value: string },
  model?: string, effort?: string
): Promise<{ passed: boolean; testFailuresDetected: boolean }> {
  const { existsSync, readFileSync } = fs;

  interface TestRunner {
    cmd: string;
    args: string[];
    cwd: string;
    label: string;
  }

  const runners: TestRunner[] = [];

  // --- Root-level detection (existing logic) ---
  const pkgPath = `${WORKSPACE}/package.json`;
  const pytestIniPath = `${WORKSPACE}/pytest.ini`;
  const pyprojectPath = `${WORKSPACE}/pyproject.toml`;
  const cargoPath = `${WORKSPACE}/Cargo.toml`;

  if (existsSync(pkgPath)) {
    try {
      const pkg = JSON.parse(readFileSync(pkgPath, 'utf-8'));
      if (pkg.scripts?.test) {
        runners.push({ cmd: 'npm', args: ['test'], cwd: WORKSPACE, label: 'npm test (root)' });
      }
    } catch { /* malformed package.json — skip */ }
  }

  if (runners.length === 0) {
    if (existsSync(pytestIniPath)) {
      runners.push({ cmd: 'python', args: ['-m', 'pytest', '-x', '-q'], cwd: WORKSPACE, label: 'pytest (root)' });
    } else if (existsSync(pyprojectPath)) {
      try {
        if (readFileSync(pyprojectPath, 'utf-8').includes('pytest')) {
          runners.push({ cmd: 'python', args: ['-m', 'pytest', '-x', '-q'], cwd: WORKSPACE, label: 'pytest (root pyproject)' });
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
    const agentPkg = `${WORKSPACE}/agent/package.json`;

    if (existsSync(backendPytestIni)) {
      runners.push({ cmd: 'python', args: ['-m', 'pytest', '-x', '-q'], cwd: `${WORKSPACE}/backend`, label: 'pytest (backend)' });
    } else if (existsSync(backendPyproject)) {
      try {
        if (readFileSync(backendPyproject, 'utf-8').includes('pytest')) {
          runners.push({ cmd: 'python', args: ['-m', 'pytest', '-x', '-q'], cwd: `${WORKSPACE}/backend`, label: 'pytest (backend pyproject)' });
        }
      } catch { /* unreadable — skip */ }
    }

    if (existsSync(agentPkg)) {
      try {
        const pkg = JSON.parse(readFileSync(agentPkg, 'utf-8'));
        if (pkg.scripts?.test) {
          runners.push({ cmd: 'npm', args: ['test'], cwd: `${WORKSPACE}/agent`, label: 'npm test (agent)' });
        }
      } catch { /* malformed package.json — skip */ }
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
    for (const runner of runners) {
      try {
        execFileSync(runner.cmd, runner.args, { cwd: runner.cwd, encoding: 'utf-8', timeout: 180_000 });
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
      return { passed: false, testFailuresDetected: true };
    }
  }
  return { passed: false, testFailuresDetected: true };
}

// ── Claude Code Runner (shared by all handlers) ─────────────────────────────
async function runClaudeCode(prompt: string, ws: WebSocket | null, statusRef: { value: string }, model?: string, effort?: string): Promise<{
  exitCode: number; allOutput: string; lastResultText: string;
}> {
  let allOutput = '';
  let lastResultText = '';

  sendLog(ws, `\n--- Claude Code starting ---\nPrompt: ${prompt.slice(0, 200)}${prompt.length > 200 ? '...' : ''}\n\n`);
  log('Spawning Claude Code CLI...');

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

    const args = ['-p', prompt, '--output-format', 'stream-json', '--verbose', '--dangerously-skip-permissions'];
    if (model) args.push('--model', model);
    if (effort) args.push('--effort', effort);
    const claude = spawn('claude', args, { cwd: WORKSPACE, env: claudeEnv });

    // Kill Claude if it exceeds 30 minutes
    const CLAUDE_TIMEOUT_MS = 30 * 60 * 1000;
    const killTimer = setTimeout(() => {
      log('Claude Code process timed out after 30 minutes — killing');
      sendLog(ws, '\n[TIMEOUT] Claude Code exceeded 30-minute time limit\n');
      claude.kill('SIGTERM');
      setTimeout(() => { try { claude.kill('SIGKILL'); } catch {} }, 5000);
    }, CLAUDE_TIMEOUT_MS);

    const MAX_OUTPUT = 10_000_000; // 10MB cap — prevents unbounded memory growth

    claude.stdout.on('data', (data: Buffer) => {
      const text = data.toString();
      if (allOutput.length < MAX_OUTPUT) allOutput += text;
      for (const line of text.split('\n').filter(Boolean)) {
        try {
          const obj = JSON.parse(line);
          if (obj.type === 'assistant' && obj.message?.content) {
            for (const block of obj.message.content) {
              if (block.type === 'text') {
                sendLog(ws, block.text + '\n');
                statusRef.value = block.text.slice(0, 100);
              } else if (block.type === 'tool_use') {
                sendLog(ws, `[tool: ${block.name}] ${JSON.stringify(block.input).slice(0, 200)}\n`);
                statusRef.value = `Using tool: ${block.name}`;
              }
            }
          } else if (obj.type === 'result') {
            lastResultText = obj.result || '';
            sendLog(ws, `\n--- Result ---\n${lastResultText}\n`);
          }
        } catch { sendLog(ws, text); }
      }
    });

    claude.stderr.on('data', (data: Buffer) => {
      const text = sanitize(data.toString());
      if (allOutput.length < MAX_OUTPUT) allOutput += text;
      sendLog(ws, `[stderr] ${text}`);
    });

    claude.on('error', (err) => { clearTimeout(killTimer); sendLog(ws, `[error] ${err.message}\n`); resolve(1); });
    claude.on('close', (code) => { clearTimeout(killTimer); log(`Claude Code exited with code ${code}`); resolve(code ?? 1); });
  });

  sendLog(ws, `\n--- Claude Code exited (code: ${exitCode}) ---\n`);
  return { exitCode, allOutput, lastResultText };
}

// ── Task Type Hints (appended to prompts for known task types) ───────────────
const TASK_TYPE_HINTS: Record<string, string> = {
  'qa': 'Focus on writing tests. Run the existing test suite first to understand patterns. Do not modify production code unless fixing a test setup issue.',
  'migrate': 'This is a database migration task. Check existing Alembic migrations in backend/alembic/versions/ for naming conventions. Use `alembic revision --autogenerate` as a starting point only — always review and clean up generated migrations.',
  'security_audit': 'Focus on identifying security vulnerabilities. Do not implement fixes — create a detailed findings report as your output.',
  'perf': 'Profile before optimizing. Identify the specific bottleneck with measurements before writing any fix.',
};

// ── Handler: Create (default — make changes, push, open PR) ─────────────────
async function handleCreate(
  taskId: string, payload: Record<string, unknown>, task: Record<string, unknown>,
  ghToken: string, owner: string, repo: string, ws: WebSocket | null, statusRef: { value: string },
  model?: string, effort?: string
) {
  const rawPrompt = String(payload.prompt || payload.description || task.description || '');
  const rawBaseBranch = String(payload.base_branch || 'main');
  const rawTitle = String(task.title || payload.title || 'automated changes');
  // Include title as context so the agent understands intent even if the prompt is terse
  const prompt = task.title ? `Task: ${task.title}\n\n${rawPrompt}` : rawPrompt;

  const baseBranch = validateBranchName(rawBaseBranch, 'base_branch');
  const taskTitle = rawTitle.replace(/[`$\\!"';&|<>(){}[\]]/g, '');
  if (!prompt) throw new Error('No prompt found in task payloads');

  const projectCtx = task.project_context as Record<string, unknown> | undefined;
  const contextPrefix = projectCtx && Object.keys(projectCtx).some(k => {
    const v = projectCtx[k]; return Array.isArray(v) && v.length > 0;
  }) ? formatProjectContext(projectCtx) + '\n\n' : '';
  const parentCtx = (task.context as Record<string, unknown> | undefined)?.parent_context as Record<string, unknown> | undefined;
  const parentContextSection = parentCtx?.parent_prompt
    ? `## Parent Task Context\nThis task is a subtask of: **${parentCtx.parent_title}**\nParent goal: ${parentCtx.parent_prompt}\n\n`
    : '';
  const promptSuffix = payload.prompt_suffix ? `\n\nAdditional instructions:\n${payload.prompt_suffix}` : '';
  const siblingNote = (projectCtx?.sibling_tasks as unknown[] | undefined)?.length
    ? '\n\nNote: Check the Sibling Tasks listed above and avoid making changes that overlap with or duplicate files they are already working on.'
    : '';
  const taskTypeHint = TASK_TYPE_HINTS[String(payload.type || '')] ? `\n\n${TASK_TYPE_HINTS[String(payload.type || '')]}` : '';
  const finalPrompt = `${contextPrefix}${parentContextSection}${prompt}${siblingNote}${promptSuffix}${taskTypeHint}`;

  // Clone and setup workspace
  const { workBranch, shortId } = await setupWorkspace(owner, repo, ghToken, baseBranch, 'task', taskId, ws, statusRef);

  // Run Claude
  statusRef.value = 'Running Claude Code...';
  const { exitCode, allOutput, lastResultText } = await runClaudeCode(finalPrompt, ws, statusRef, model, effort);

  const diff = git(['status', '--porcelain']);
  const hasChanges = diff.length > 0;

  if (hasChanges && exitCode === 0) {
    statusRef.value = 'Pushing changes...';
    sendLog(ws, '\nCommitting and pushing changes...\n');
    // Run lint/build checks before committing — retry up to 3 times with Claude fixes
    const changedPaths = diff.split('\n').map(l => l.trim().split(/\s+/).pop() || '');
    await lintFixLoop(changedPaths, ws, statusRef, 'the code you just wrote', model, effort);

    git(['add', '-A']);
    execFileSync('git', ['-C', WORKSPACE, 'commit', '-m', `blueprint: ${taskTitle}\n\nAutomated by Blueprint Agent (task: ${shortId})`], { encoding: 'utf-8' });

    // Rebase onto latest base branch before push to avoid merge conflicts in the PR.
    // If rebase fails, fall back to merge. If merge fails, push as-is.
    statusRef.value = `Rebasing onto ${baseBranch}...`;
    try {
      git(['fetch', 'origin', baseBranch]);
      git(['rebase', `origin/${baseBranch}`]);
      sendLog(ws, `Rebased onto latest ${baseBranch}\n`);
    } catch {
      try { git(['rebase', '--abort']); } catch {}
      sendLog(ws, `Rebase onto ${baseBranch} had conflicts — trying merge instead\n`);
      try {
        git(['merge', `origin/${baseBranch}`, '--no-edit']);
        sendLog(ws, `Merged ${baseBranch} successfully\n`);
      } catch {
        try { git(['merge', '--abort']); } catch {}
        sendLog(ws, `Merge also conflicted — pushing as-is, review pipeline will handle\n`);
      }
    }

    // Verify no conflict markers before pushing
    try {
      const markers = execFileSync('git', ['-C', WORKSPACE, 'grep', '-rl', '<' + '<<<<<<', '--', '*.ts', '*.tsx', '*.py', '*.js', '*.jsx', '*.json'], { encoding: 'utf-8', timeout: 10_000 }).trim();
      if (markers) {
        sendLog(ws, `WARNING: Conflict markers found in:\n${markers}\nAborting rebase and pushing original commit.\n`);
        try { git(['rebase', '--abort']); } catch {}
      }
    } catch { /* no markers found */ }

    // Re-run lint after rebase — upstream changes can introduce new errors
    try {
      runProjectLintChecks(changedPaths, ws);
    } catch (postRebaseLintErr: unknown) {
      const msg = postRebaseLintErr instanceof Error ? postRebaseLintErr.message : String(postRebaseLintErr);
      sendLog(ws, `Post-rebase lint failed:\n${msg.slice(0, 3000)}\nClaude will fix...\n`);
      await runClaudeCode(
        `After rebasing onto ${baseBranch}, lint/build errors appeared. Fix ALL of them — do not use \`any\` types.\n\n${msg.slice(0, 5000)}`,
        ws, statusRef, model, effort
      );
      git(['add', '-A']);
      execFileSync('git', ['-C', WORKSPACE, 'commit', '-m', 'blueprint: fix post-rebase lint errors'], { encoding: 'utf-8' });
    }

    // Detect and run tests
    statusRef.value = 'Running tests...';
    sendLog(ws, '\nDetecting and running tests...\n');
    const { passed: testsPassed, testFailuresDetected } = await testFixLoop(ws, statusRef, model, effort);
    const testSummary = testsPassed ? '✅ Tests passed' : testFailuresDetected ? '⚠️ Test failures detected' : '⚠️ Could not run tests';

    const prBody = `## Summary\n\n${lastResultText?.slice(0, 2000) || `Automated changes for: ${taskTitle}`}\n\n## Test Results\n\n${testSummary}\n\n---\n*Automated by Blueprint Agent*`;
    const prUrl = await pushAndCreatePR(owner, repo, taskId, workBranch, baseBranch, `[Blueprint] ${taskTitle}`, prBody, ws, statusRef, true);
    const commitHash = git(['rev-parse', 'HEAD']);

    await apiPost('/api/agent/status', {
      task_id: taskId, status: 'COMPLETED', artifact_type: 'PULL_REQUEST',
      commit_hash: commitHash, pr_url: prUrl, repo_url: `https://github.com/${owner}/${repo}`,
    });
  } else if (exitCode === 0) {
    await apiPost('/api/agent/status', { task_id: taskId, status: 'COMPLETED', logs: 'No file changes.' });
  } else {
    await apiPost('/api/agent/status', {
      task_id: taskId, status: 'FAILED', artifact_type: 'ERROR_LOG', logs: sanitize(allOutput.slice(-10_000)),
    });
  }
}

// ── Handler: Review (review a PR, output verdict) ───────────────────────────
async function handleReview(
  taskId: string, payload: Record<string, unknown>, task: Record<string, unknown>,
  ghToken: string, owner: string, repo: string, ws: WebSocket | null, statusRef: { value: string },
  model?: string, effort?: string
) {
  const prNumber = Number(payload.pr_number);
  if (!Number.isInteger(prNumber) || prNumber <= 0) throw new Error(`Invalid pr_number: ${payload.pr_number}`);
  const branch = validateBranchName(String(payload.branch || ''), 'branch');
  const baseBranch = validateBranchName(String(payload.base_branch || 'main'), 'base_branch');
  const originalPrompt = String(payload.original_prompt || '');

  // Read review iteration from task context
  const iteration = (task.context as any)?.review_loop?.iteration || 1;

  // Clone the full repo so Claude can explore everything, then checkout the PR branch
  statusRef.value = `Cloning ${owner}/${repo} (PR #${prNumber})...`;
  sendLog(ws, `Reviewing PR #${prNumber} on branch ${branch} (iteration ${iteration})\n`);
  execFileSync('rm', ['-rf', WORKSPACE], { encoding: 'utf-8' });
  const cloneUrl = `https://x-access-token:${ghToken}@github.com/${owner}/${repo}.git`;
  execFileSync('git', ['clone', cloneUrl, WORKSPACE], { encoding: 'utf-8', timeout: 300_000 });
  git(['checkout', branch]);

  // Fetch PR metadata (title + body) from GitHub API — best-effort
  let prTitle = '';
  let prBody = '';
  try {
    const prRes = await fetch(`https://api.github.com/repos/${owner}/${repo}/pulls/${prNumber}`, {
      headers: { Authorization: `token ${ghToken}`, Accept: 'application/vnd.github+json' },
    });
    if (prRes.ok) {
      const prData = await prRes.json() as { title?: string; body?: string };
      prTitle = prData.title || '';
      prBody = prData.body || '';
    }
  } catch { /* best-effort, continue without PR metadata */ }

  // Get a summary of changed files for context
  let changedFiles = '';
  try {
    changedFiles = git(['diff', '--name-status', `origin/${baseBranch}...HEAD`]);
  } catch { changedFiles = '(could not generate file list)'; }

  // Build iteration preamble
  const iterationPreamble = `This is review iteration ${iteration} of the automated review-fix cycle.${iteration > 1 ? '\nPrevious iterations have already addressed some issues. Focus on what remains unresolved.' : ''}`;

  // Build review prompt — Claude has full repo access, not just a diff blob
  const reviewPrompt = `You are reviewing PR #${prNumber} on this repository. The PR branch is "${branch}" targeting "${baseBranch}".

${iterationPreamble}

${originalPrompt ? `The original task was: ${originalPrompt}\n\n` : ''}${prTitle ? `PR title: ${prTitle}\n` : ''}${prBody ? `PR description:\n${prBody}\n\n` : ''}Files changed in this PR:
${changedFiles}

You have full access to the codebase. Read any files you need to understand the changes in context. Use \`git diff origin/${baseBranch}...HEAD\` to see the full diff, or \`git diff origin/${baseBranch}...HEAD -- <file>\` for specific files.

Review the changes thoroughly using this checklist:

**Correctness & Logic:**
- Does the code correctly implement what the original task asked for?
- Are there edge cases, null/undefined handling, or off-by-one errors?
- Are async operations properly awaited? Are errors properly handled?
- Do any database queries have N+1 problems or missing indexes?

**Security:**
- Are user inputs validated and sanitized before use?
- Are there SQL injection, XSS, path traversal, or command injection risks?
- Are secrets/tokens exposed in logs, responses, or error messages?
- Are authorization checks present on new endpoints?

**Code Quality:**
- Is the code readable and following existing patterns in the codebase?
- Are there obvious performance issues (unnecessary loops, redundant DB calls)?
- Does the change break any existing interfaces or contracts?
- Are type annotations correct (TypeScript/Python)?

**Test Coverage:**
- Are there tests for the new/changed behavior?
- Do existing tests still pass given these changes?

After your thorough review, output EXACTLY one of these JSON blocks as the very last thing in your response:

If the changes look good and should be merged:
{"verdict": "APPROVE", "feedback": "Brief summary of why it's approved"}

If changes are needed before merging:
{"verdict": "REQUEST_CHANGES", "feedback": "...", "issues": [{"severity": "critical|major|minor", "description": "..."}]}

IMPORTANT: Also write your verdict JSON to /tmp/review_verdict.json (NOT in the repository). The file should contain ONLY the JSON object, no markdown wrapping.`;

  statusRef.value = 'Running code review...';
  const { exitCode, allOutput, lastResultText } = await runClaudeCode(reviewPrompt, ws, statusRef, model, effort);

  // Prefer reading verdict from /tmp/ file (avoids output truncation), fall back to output parsing

  let logs = '';
  let verdictJsonField: string | undefined;
  try {
    if (fs.existsSync('/tmp/review_verdict.json')) {
      const verdictJson = fs.readFileSync('/tmp/review_verdict.json', 'utf-8').trim();
      // Validate it's actual verdict JSON
      const parsed = JSON.parse(verdictJson);
      if (parsed.verdict) {
        // Send verdict via dedicated field (structured, tamper-resistant vs log parsing)
        verdictJsonField = verdictJson;
        // Also append to logs for backwards compatibility
        const reviewText = sanitize(lastResultText || allOutput.slice(-50_000));
        logs = reviewText + '\n\n' + verdictJson;
        sendLog(ws, `Verdict read from file: ${parsed.verdict}\n`);
      }
    }
  } catch { /* fall through to output parsing */ }

  if (!logs) {
    logs = sanitize(lastResultText || allOutput.slice(-50_000));
  }

  await apiPost('/api/agent/status', {
    task_id: taskId,
    status: exitCode === 0 ? 'COMPLETED' : 'FAILED',
    ...(exitCode !== 0 ? { artifact_type: 'ERROR_LOG' } : {}),
    logs,
    ...(verdictJsonField ? { verdict_json: verdictJsonField } : {}),
  });
}

// ── Handler: Fix (address review feedback on existing PR branch) ─────────────
async function handleFix(
  taskId: string, payload: Record<string, unknown>, task: Record<string, unknown>,
  ghToken: string, owner: string, repo: string, ws: WebSocket | null, statusRef: { value: string },
  model?: string, effort?: string
) {
  const branch = validateBranchName(String(payload.branch || ''), 'branch');
  const baseBranch = validateBranchName(String(payload.base_branch || 'main'), 'base_branch');
  const prompt = String(payload.prompt || '');
  const prNumber = Number(payload.pr_number);
  if (!Number.isInteger(prNumber) || prNumber <= 0) throw new Error(`Invalid pr_number: ${payload.pr_number}`);

  if (!prompt) throw new Error('Fix task missing prompt');

  // Clone the existing PR branch (don't create a new one)
  statusRef.value = `Cloning ${owner}/${repo} (fixing PR #${prNumber})...`;
  sendLog(ws, `Fixing PR #${prNumber} on branch ${branch}\n`);
  execFileSync('rm', ['-rf', WORKSPACE], { encoding: 'utf-8' });
  const cloneUrl = `https://x-access-token:${ghToken}@github.com/${owner}/${repo}.git`;
  execFileSync('git', ['clone', cloneUrl, WORKSPACE], { encoding: 'utf-8', timeout: 300_000 });
  git(['checkout', branch]);

  git(['config', 'user.email', 'blueprint-agent@automated.dev']);
  git(['config', 'user.name', 'Blueprint Agent']);

  // Always rebase onto target branch to resolve merge conflicts
  statusRef.value = `Rebasing onto ${baseBranch}...`;
  sendLog(ws, `Rebasing ${branch} onto origin/${baseBranch}...\n`);
  let hasConflicts = false;
  try {
    git(['fetch', 'origin', baseBranch]);
    git(['rebase', `origin/${baseBranch}`]);
    sendLog(ws, `Rebase successful\n`);
  } catch (e) {
    sendLog(ws, `Rebase had conflicts — Claude will resolve them\n`);
    // Abort the failed rebase so Claude can handle it with a merge strategy
    try { git(['rebase', '--abort']); } catch {}
    // Try merge instead so Claude sees conflict markers
    try {
      git(['merge', `origin/${baseBranch}`, '--no-edit']);
    } catch {
      hasConflicts = true;
      sendLog(ws, `Merge conflicts present — Claude will resolve\n`);
    }
  }

  // Prepend accumulated review history so the agent knows what was previously requested
  const reviewLoop = (task.context as Record<string, unknown>)?.review_loop as Record<string, unknown> | undefined;
  const reviewHistory = (reviewLoop?.review_history as Array<{ iteration: number; feedback: string }> | undefined) || [];
  let basePrompt = prompt;
  if (reviewHistory.length > 0) {
    const historyLines = reviewHistory
      .map(e => `- Iteration ${e.iteration}: ${e.feedback.slice(0, 300)}`)
      .join('\n');
    basePrompt = `Previous review iterations:\n${historyLines}\n\nCurrent review feedback to address:\n${prompt}`;
  }

  // Always check for conflict markers — even if rebase/merge appeared to succeed
  let enrichedPrompt = basePrompt;
  try {
    const conflictFiles = git(['diff', '--name-only', '--diff-filter=U']);
    if (conflictFiles.trim()) {
      hasConflicts = true;
      enrichedPrompt = `MERGE CONFLICTS DETECTED in the following files:\n${conflictFiles}\n\nThis is the PRIMARY task — resolve ALL conflict markers (${'<'.repeat(7)} ${'='.repeat(7)} ${'>'.repeat(7)}) in each file listed above. Keep the intent of this PR's changes (HEAD) while incorporating upstream updates from ${baseBranch}.\n\nAfter resolving conflicts, stage the files with git add and ensure no conflict markers remain.\n\n${basePrompt}`;
      sendLog(ws, `Conflicting files:\n${conflictFiles}\n`);
    }
  } catch { /* no unmerged files */ }
  // Also check for conflict markers in tracked files via grep
  if (!hasConflicts) {
    try {
      const markerCheck = execFileSync('git', ['-C', WORKSPACE, 'grep', '-rl', '<' + '<<<<<<', '--', '*.ts', '*.tsx', '*.py', '*.js', '*.json'], { encoding: 'utf-8', timeout: 10_000 }).trim();
      if (markerCheck) {
        enrichedPrompt = `CONFLICT MARKERS FOUND in the following files:\n${markerCheck}\n\nThis is the PRIMARY task — resolve ALL conflict markers (${'<'.repeat(7)} ${'='.repeat(7)} ${'>'.repeat(7)}) in each file. Keep the intent of this PR's changes while incorporating upstream updates from ${baseBranch}.\n\n${basePrompt}`;
        sendLog(ws, `Files with conflict markers:\n${markerCheck}\n`);
      }
    } catch { /* no markers found, good */ }
  }

  // Prepend parent context if this fix agent is part of a larger initiative
  const fixParentCtx = (task.context as Record<string, unknown> | undefined)?.parent_context as Record<string, unknown> | undefined;
  if (fixParentCtx?.parent_prompt) {
    enrichedPrompt = `## Parent Task Context\nThis task is a subtask of: **${fixParentCtx.parent_title}**\nParent goal: ${fixParentCtx.parent_prompt}\n\n${enrichedPrompt}`;
  }

  // Run Claude with the review feedback as the prompt
  statusRef.value = 'Addressing review feedback...';
  const { exitCode, allOutput } = await runClaudeCode(enrichedPrompt, ws, statusRef, model, effort);

  // Check for uncommitted changes (from Claude's fixes)
  const diff = git(['status', '--porcelain']);
  if (diff.length > 0 && exitCode === 0) {
    // Run lint/build checks before committing — retry up to 3 times with Claude fixes
    const changedPaths = diff.split('\n').map(l => l.trim().split(/\s+/).pop() || '');
    await lintFixLoop(changedPaths, ws, statusRef, 'the fix you just applied', model, effort);

    git(['add', '-A']);
    const shortId = taskId.replace(/-/g, '').slice(0, 8);
    execFileSync('git', ['-C', WORKSPACE, 'commit', '-m', `blueprint: address review feedback\n\nAutomated by Blueprint Agent (task: ${shortId})`], { encoding: 'utf-8' });
  }

  // Final rebase onto base branch to ensure no conflicts remain before push.
  // If rebase fails, fall back to merge — don't throw, let the push succeed
  // and the review pipeline handle any remaining issues.
  if (exitCode === 0) {
    try {
      git(['fetch', 'origin', baseBranch]);
      git(['rebase', `origin/${baseBranch}`]);
      sendLog(ws, `Final rebase onto ${baseBranch} successful\n`);
    } catch {
      let conflictList = '';
      try { conflictList = git(['diff', '--name-only', '--diff-filter=U']).trim(); } catch {}
      try { git(['rebase', '--abort']); } catch {}
      sendLog(ws, `Final rebase had conflicts — trying merge instead\n`);
      try {
        git(['merge', `origin/${baseBranch}`, '--no-edit']);
        sendLog(ws, `Merge onto ${baseBranch} successful\n`);
      } catch {
        // Merge also conflicted — abort and push as-is.
        // The PR will show conflicts and the review pipeline will handle it.
        try { git(['merge', '--abort']); } catch {}
        sendLog(ws, `Merge also conflicted (${conflictList || 'unknown files'}) — pushing as-is, review pipeline will handle\n`);
      }
    }
  }

  // Check for remaining conflict markers — warn but don't block push
  if (exitCode === 0) {
    try {
      const remainingMarkers = execFileSync('git', ['-C', WORKSPACE, 'grep', '-rl', '<' + '<<<<<<', '--', '*.ts', '*.tsx', '*.py', '*.js', '*.jsx', '*.json'], { encoding: 'utf-8', timeout: 10_000 }).trim();
      if (remainingMarkers) {
        sendLog(ws, `WARNING: Conflict markers still present in:\n${remainingMarkers}\nPushing anyway — review pipeline will continue resolving.\n`);
      }
    } catch { /* grep found nothing — no markers, good */ }
  }

  // Re-run lint after rebase — upstream changes can introduce new errors
  if (exitCode === 0 && diff.length > 0) {
    const changedPaths = diff.split('\n').map(l => l.trim().split(/\s+/).pop() || '');
    try {
      runProjectLintChecks(changedPaths, ws);
    } catch (postRebaseLintErr: unknown) {
      const msg = postRebaseLintErr instanceof Error ? postRebaseLintErr.message : String(postRebaseLintErr);
      sendLog(ws, `Post-rebase lint failed:\n${msg.slice(0, 3000)}\nClaude will fix...\n`);
      await runClaudeCode(
        `After rebasing onto ${baseBranch}, lint/build errors appeared. Fix ALL of them — do not use \`any\` types.\n\n${msg.slice(0, 5000)}`,
        ws, statusRef, model, effort
      );
      git(['add', '-A']);
      const shortId = taskId.replace(/-/g, '').slice(0, 8);
      execFileSync('git', ['-C', WORKSPACE, 'commit', '-m', `blueprint: fix post-rebase lint errors\n\nAutomated by Blueprint Agent (task: ${shortId})`], { encoding: 'utf-8' });
    }
  }

  // Always push if the branch has diverged from origin (rebase or new commits)
  if (exitCode === 0) {
    statusRef.value = 'Pushing fixes...';
    const localHead = git(['rev-parse', 'HEAD']);
    let remoteHead = '';
    try { remoteHead = git(['rev-parse', `origin/${branch}`]); } catch {}

    if (localHead !== remoteHead) {
      const freshGhRes = await apiGet(`/api/agent/github/token?task_id=${taskId}`);
      const pushUrl = `https://x-access-token:${freshGhRes.data.token}@github.com/${owner}/${repo}.git`;
      execFileSync('git', ['-C', WORKSPACE, 'remote', 'set-url', 'origin', pushUrl], { encoding: 'utf-8' });
      git(['push', '--force-with-lease', 'origin', branch]);
      sendLog(ws, `Pushed fixes to ${branch}\n`);

      await apiPost('/api/agent/status', {
        task_id: taskId, status: 'COMPLETED', artifact_type: 'GIT_COMMIT',
        commit_hash: localHead, repo_url: `https://github.com/${owner}/${repo}`,
      });
    } else {
      await apiPost('/api/agent/status', { task_id: taskId, status: 'COMPLETED', logs: 'No changes needed for review feedback.' });
    }
  } else {
    await apiPost('/api/agent/status', {
      task_id: taskId, status: 'FAILED', artifact_type: 'ERROR_LOG', logs: sanitize(allOutput.slice(-10_000)),
    });
  }
}

// ── Shared: Format project_context into a markdown section ────────────────
function formatProjectContext(ctx: Record<string, unknown>): string {
  const CONTEXT_BUDGET = 8000;

  const header = [
    '## Project Context',
    'The following tasks are already active or completed in this project. Avoid duplicating their work.',
    '',
  ];

  // Build active tasks section (always included in full)
  const activeLines: string[] = [];
  const activeTasks = ctx.active_tasks as Array<Record<string, unknown>> | undefined;
  if (activeTasks && activeTasks.length > 0) {
    activeLines.push('### Active Tasks');
    for (const t of activeTasks) {
      activeLines.push(`- [${t.task_type}] **${t.title}** (${t.status}) — ${String(t.prompt || '').slice(0, 400)}`);
    }
    activeLines.push('');
  }

  // Build open PRs section (always included in full)
  const prLines: string[] = [];
  const openPRs = ctx.open_prs as string[] | undefined;
  if (openPRs && openPRs.length > 0) {
    prLines.push('### Open PRs');
    for (const pr of openPRs) {
      prLines.push(`- ${pr}`);
    }
    prLines.push('');
  }

  // Build sibling tasks section (always included in full)
  const siblingLines: string[] = [];
  const siblingTasks = ctx.sibling_tasks as Array<Record<string, unknown>> | undefined;
  if (siblingTasks && siblingTasks.length > 0) {
    siblingLines.push('## Sibling Tasks (parallel tasks from the same parent — avoid duplicating their work)');
    for (const t of siblingTasks) {
      const files = (t.related_files as string[] | undefined);
      const filesSuffix = files && files.length > 0 ? ` (files: ${files.join(', ')})` : '';
      siblingLines.push(`- [${t.status}] ${t.title}${filesSuffix}`);
    }
    siblingLines.push('');
  }

  const fixedContent = [...header, ...activeLines, ...prLines, ...siblingLines].join('\n');
  let budget = CONTEXT_BUDGET - fixedContent.length;

  // Build queued tasks section — fall back to one-liners if over budget
  const queuedTasks = ctx.queued_tasks as Array<Record<string, unknown>> | undefined;
  let queuedLines: string[] = [];
  if (queuedTasks && queuedTasks.length > 0) {
    const fullLines: string[] = ['### Queued Tasks'];
    for (const t of queuedTasks) {
      fullLines.push(`- [${t.task_type}] **${t.title}** — ${String(t.prompt || '').slice(0, 400)}`);
    }
    fullLines.push('');
    const fullContent = fullLines.join('\n');

    if (fullContent.length <= budget) {
      queuedLines = fullLines;
    } else {
      // Reduce to one-liners
      const shortLines: string[] = ['### Queued Tasks'];
      for (const t of queuedTasks) {
        shortLines.push(`- [${t.task_type}] **${t.title}**`);
      }
      shortLines.push('');
      queuedLines = shortLines;
    }
    budget -= queuedLines.join('\n').length;
  }

  // Build recently completed section — drop oldest entries to fit budget
  const recentTasks = ctx.recently_completed_tasks as Array<Record<string, unknown>> | undefined;
  let recentLines: string[] = [];
  let recentDropped = 0;
  if (recentTasks && recentTasks.length > 0) {
    const allEntries: string[] = [];
    for (const t of recentTasks) {
      const pr = t.pr_url ? ` (${t.pr_url})` : '';
      const summary = t.research_findings_summary ? ` — ${String(t.research_findings_summary).slice(0, 500)}` : '';
      allEntries.push(`- [${t.task_type}] **${t.title}**${pr}${summary}`);
    }

    // Keep most recent entries that fit within budget
    const headerLine = '### Recently Completed Tasks';
    let kept = allEntries.slice(); // most recent assumed to be last
    while (kept.length > 0) {
      const candidate = [headerLine, ...kept, ''].join('\n');
      if (candidate.length <= budget) break;
      kept.shift(); // drop oldest (first entry)
      recentDropped++;
    }
    if (kept.length > 0) {
      recentLines = [headerLine, ...kept, ''];
      budget -= recentLines.join('\n').length;
    } else {
      recentDropped = allEntries.length;
    }
  }

  // Append footer if anything was omitted
  const footerLines: string[] = [];
  const omittedCount = recentDropped;
  if (omittedCount > 0) {
    footerLines.push(`_(${omittedCount} more tasks not shown)_`);
    footerLines.push('');
  }

  const allLines = [...header, ...activeLines, ...queuedLines, ...recentLines, ...prLines, ...siblingLines, ...footerLines];
  return allLines.join('\n');
}

// ── Handler: Plan (decompose complex task into subtasks) ───────────────────
async function handlePlan(
  taskId: string, payload: Record<string, unknown>, task: Record<string, unknown>,
  ghToken: string, owner: string, repo: string, ws: WebSocket | null, statusRef: { value: string },
  model?: string, effort?: string
) {
  const prompt = String(payload.prompt || payload.description || task.description || '');
  const baseBranch = String(payload.base_branch || 'main');
  const planPromptSuffix = payload.prompt_suffix ? `\n\nAdditional instructions:\n${payload.prompt_suffix}` : '';

  if (!prompt) throw new Error('Plan task missing prompt');

  // Clone repo — planner needs codebase context for good decomposition
  statusRef.value = `Cloning ${owner}/${repo} for planning...`;
  sendLog(ws, `Planning task decomposition for: ${prompt.slice(0, 200)}\n`);
  execFileSync('rm', ['-rf', WORKSPACE], { encoding: 'utf-8' });
  const cloneUrl = `https://x-access-token:${ghToken}@github.com/${owner}/${repo}.git`;
  execFileSync('git', ['clone', '--branch', baseBranch, cloneUrl, WORKSPACE], { encoding: 'utf-8', timeout: 300_000 });

  const projectCtx = task.project_context as Record<string, unknown> | undefined;
  const contextPrefix = projectCtx && Object.keys(projectCtx).some(k => {
    const v = projectCtx[k]; return Array.isArray(v) && v.length > 0;
  }) ? formatProjectContext(projectCtx) + '\n\n' : '';

  const planPrompt = `${contextPrefix}You are a planning agent. Your job is to analyze this codebase and decompose the following task into concrete, ordered subtasks that can each be implemented as a single pull request.

Task to decompose:
${prompt}

Instructions:
1. Explore the codebase thoroughly. Read key files, understand the architecture, find relevant patterns.
2. Break the task into the smallest meaningful units of work. Each subtask should be one PR-sized change.
3. Identify dependencies between subtasks — which must complete before others can start.
4. Write clear, self-contained prompts for each subtask. The implementing agent knows nothing about this planning session.

Output EXACTLY one JSON block as the VERY LAST thing in your response:

{
  "subtasks": [
    {
      "title": "Short title (under 60 chars, conventional commit style: feat:, fix:, refactor:, etc.)",
      "prompt": "Self-contained implementation prompt. Describe WHAT to achieve and key constraints. The agent has full repo access and will figure out implementation details.",
      "depends_on_indices": [],
      "related_files": ["src/main.py", "src/utils.py"],
      "model": "claude-sonnet-4-6 | claude-opus-4-6 | null (inherit from parent)",
      "effort": "low | medium | high | null (inherit from parent)",
      "scope": "small",
      "risk": "low",
      "type": "create",
      "approval_reason": "Only set when risk=high — brief reason why human approval is needed (omit otherwise)"
    },
    {
      "title": "feat: second subtask",
      "prompt": "...",
      "depends_on_indices": [0],
      "related_files": ["src/main.py"],
      "model": null,
      "effort": null,
      "scope": "medium",
      "risk": "medium",
      "type": "create"
    }
  ]
}

Risk guidance:
- risk=high: changes to auth flows, JWT/token handling, database models (models.py), Alembic migrations, core orchestration (main.py worker loops), VM provisioning (compute.py), any change that if wrong could cause data loss or security breach
- risk=medium: new API endpoints, changes to existing API request/response shapes, new agent handler types, frontend state management, WebSocket protocol changes
- risk=low: UI-only changes, new utility functions, log/error message changes, documentation, tests that don't touch production code
When risk=high for a subtask, the task will require human approval before executing. Set this judiciously — only when a mistake would be hard to roll back. When risk=high, also set "approval_reason" to a brief explanation of why this needs approval.

Rules:
- depends_on_indices uses 0-based array positions (e.g., [0, 1] means this subtask depends on the first and second subtasks)
- related_files: list the key files each subtask will create or modify
- Each subtask prompt must be self-contained — don't reference other subtasks by index
- Keep prompts concise — describe what to achieve, not step-by-step how
- Order subtasks so that dependencies flow forward (a subtask can only depend on earlier subtasks)
- If subtasks involve structural refactors of the same files (renames, interface changes, restructuring), set depends_on_indices between them. Small additive changes to shared files can stay parallel.
- Aim for 2-6 subtasks. If the task is simple enough for 1 PR, output a single subtask with no dependencies
- Do NOT include review, testing, or deployment subtasks — those are handled automatically by the pipeline
- Use model=claude-opus-4-6 and effort=high only for complex architectural subtasks. Leave null for simple additive changes.
- scope: "small" for changes under ~100 lines across 1-3 files with clear, narrow scope, "medium" for changes spanning 3-8 files or 100-300 lines, or touching shared infrastructure, "large" for cross-component architectural changes, 300+ lines, or changes to auth/data models/core orchestration
- The "type" field controls how the subtask is executed: "create" (default) for new features/additions, "refactor" for code quality improvements with no behavior change, "migrate" for database schema changes (Alembic migrations), "qa" for test-only PRs.
- If any subtask modifies SQLAlchemy/SQLModel models (backend/app/models.py) or adds new database tables/columns, include a separate subtask with set type="migrate" (e.g., title "feat: add index for user lookup", type "migrate"). This subtask should depend on the model change subtask. The migration agent will handle Alembic generation.
- If any subtask adds new user-facing functionality, consider whether a test subtask is needed. New API endpoints should have corresponding integration tests. New agent handlers should have at least one smoke test. Add a test subtask with depends_on_indices pointing to the implementation subtask.
- If any subtask changes a public API interface (endpoint signatures, request/response shapes, TypeScript types exported to frontend), include explicit notes in the prompt about backward compatibility requirements.
- For subtasks that involve the agent system (agent/src/index.ts), note that the agent is a monolithic file — changes must be minimal and focused to avoid conflicts with parallel subtasks.
- IMPORTANT: Write your plan JSON to /tmp/plan_output.json (NOT in the repository). The file should contain ONLY the JSON object, no markdown wrapping.${planPromptSuffix}`;

  statusRef.value = 'Running Claude Code for planning...';
  const { exitCode, allOutput, lastResultText } = await runClaudeCode(planPrompt, ws, statusRef, model, effort);

  // Prefer reading plan from /tmp/ file (avoids output truncation), fall back to output parsing
  let logs = '';

  const planPath = '/tmp/plan_output.json';
  try {
    if (fs.existsSync(planPath)) {
      logs = sanitize(fs.readFileSync(planPath, 'utf-8'));
      sendLog(ws, 'Read plan output from /tmp/plan_output.json\n');
    }
  } catch (e) {
    sendLog(ws, `Failed to read ${planPath}, falling back to output parsing\n`);
  }
  if (!logs) {
    if (lastResultText && lastResultText.includes('"subtasks"')) {
      logs = sanitize(lastResultText);
    } else {
      const jsonMatch = allOutput.match(/\{[\s\S]*"subtasks"\s*:\s*\[[\s\S]*\]\s*\}/);
      logs = sanitize(jsonMatch ? jsonMatch[0] : (lastResultText || allOutput.slice(-50_000)));
    }
  }

  // Try to parse plan JSON and spawn subtasks directly from the agent
  if (exitCode === 0 && logs) {
    try {
      let planData: Record<string, unknown> | null = null;
      try {
        planData = JSON.parse(logs);
      } catch {
        const match = logs.match(/\{[\s\S]*"subtasks"\s*:\s*\[[\s\S]*\]\s*\}/);
        if (match) planData = JSON.parse(match[0]);
      }

      const subtasks = planData?.subtasks;
      if (Array.isArray(subtasks) && subtasks.length > 0 && subtasks.length <= 10) {
        sendLog(ws, `Parsed ${subtasks.length} subtasks from plan output\n`);

        const spawnPayloads = subtasks.map((st: Record<string, unknown>) => ({
          title: String(st.title || 'Untitled subtask'),
          description: String(st.prompt || ''),
          prompt: String(st.prompt || ''),
          type: String(st.type || 'create'),
          scope: String(st.scope || 'small'),
          risk: String(st.risk || 'low'),
          base_branch: baseBranch,
          depends_on_indices: st.depends_on_indices || [],
          related_files: st.related_files || [],
          ...(st.model ? { model: String(st.model) } : {}),
          ...(st.effort ? { effort: String(st.effort) } : {}),
          ...(st.approval_reason ? { approval_reason: String(st.approval_reason) } : {}),
        }));

        statusRef.value = 'Spawning subtasks...';
        const spawnResult = await apiPost('/api/agent/spawn', {
          task_id: taskId,
          payloads: spawnPayloads,
          requires_approval: false,
          context: { agent_spawned: true },
        }) as Record<string, unknown>;

        const spawnedIds = (spawnResult.task_ids as string[]) || [];
        sendLog(ws, `Spawned ${spawnedIds.length} subtasks: ${spawnedIds.join(', ')}\n`);

        // Include full plan data with spawn info for backend enrichment
        logs = JSON.stringify({
          ...planData,
          agent_spawned: true,
          spawned_task_ids: spawnedIds,
        });
      }
    } catch (err) {
      sendLog(ws, `Agent-side spawn failed: ${err instanceof Error ? err.message : err}, falling back to backend processing\n`);
    }
  }

  await apiPost('/api/agent/status', {
    task_id: taskId,
    status: exitCode === 0 ? 'COMPLETED' : 'FAILED',
    ...(exitCode !== 0 ? { artifact_type: 'ERROR_LOG' } : {}),
    logs,
  });
}

// ── Handler: Research (analyze codebase, output improvement findings) ──────
async function handleResearch(
  taskId: string, payload: Record<string, unknown>, task: Record<string, unknown>,
  ghToken: string, owner: string, repo: string, ws: WebSocket | null, statusRef: { value: string },
  model?: string, effort?: string
) {
  const directions = payload.directions as Array<Record<string, unknown>> || [];
  const baseBranch = String(payload.base_branch || 'main');
  const userPrompt = String(payload.prompt || payload.description || task.description || '');

  // Full clone — research needs history context
  statusRef.value = `Cloning ${owner}/${repo} for research...`;
  sendLog(ws, `Starting codebase research...\n`);
  execFileSync('rm', ['-rf', WORKSPACE], { encoding: 'utf-8' });
  const cloneUrl = `https://x-access-token:${ghToken}@github.com/${owner}/${repo}.git`;
  execFileSync('git', ['clone', '--branch', baseBranch, cloneUrl, WORKSPACE], { encoding: 'utf-8', timeout: 300_000 });

  let gitLogOutput = '';
  let gitDiffStatOutput = '';
  try {
    gitLogOutput = execFileSync('git', ['-C', WORKSPACE, 'log', '--oneline', '-30'], { encoding: 'utf-8' }).trim();
  } catch (e) {
    sendLog(ws, `Warning: git log failed: ${e}\n`);
  }
  let gitShortlogOutput = '';
  try {
    gitShortlogOutput = execFileSync('git', ['-C', WORKSPACE, 'shortlog', '-sn', '--no-merges', '-20'], { encoding: 'utf-8' }).trim();
  } catch (e) {
    sendLog(ws, `Warning: git shortlog failed: ${e}\n`);
  }
  try {
    gitDiffStatOutput = execFileSync('git', ['-C', WORKSPACE, 'diff', '--stat', 'HEAD~5..HEAD'], { encoding: 'utf-8' }).trim();
  } catch (e) {
    sendLog(ws, `Warning: git diff --stat failed: ${e}\n`);
  }
  let hotspotsSection = '';
  try {
    const hotspots = execFileSync('git', ['log', '--pretty=format:', '--name-only', '-50'], {cwd: WORKSPACE, encoding: 'utf-8'})
      .split('\n').filter(Boolean);
    const fileCounts = hotspots.reduce((acc, f) => ({...acc, [f]: (acc[f]||0)+1}), {} as Record<string, number>);
    const topFiles = Object.entries(fileCounts).sort((a,b) => b[1]-a[1]).slice(0, 10);
    hotspotsSection = `### Most Changed Files (last 50 commits)\n${topFiles.map(([f,c]) => `- ${f} (${c} changes)`).join('\n')}`;
  } catch (e) {
    sendLog(ws, `Warning: git hotspots failed: ${e}\n`);
  }
  const gitHistorySection = (gitLogOutput || gitDiffStatOutput) ? `## Recent Git History (last 30 commits)
${gitLogOutput || '(unavailable)'}

## Top Contributors (last 20, no merges)
${gitShortlogOutput || '(unavailable)'}

## Recent Changes Summary (last 5 commits diff stat)
${gitDiffStatOutput || '(unavailable)'}

${hotspotsSection}

` : '';

  // Build codebase summary to reduce exploration overhead

  const claudeMdPath = `${WORKSPACE}/CLAUDE.md`;
  const claudeMd = fs.existsSync(claudeMdPath)
    ? fs.readFileSync(claudeMdPath, 'utf-8').slice(0, 2000)
    : '';
  const codebaseSummary = claudeMd
    ? `## Codebase Instructions\n${claudeMd}\n\n`
    : '';

  let fileTree = '';
  try {
    fileTree = execFileSync(
      'find', ['.', '-type', 'f',
        '-not', '-path', '*/node_modules/*',
        '-not', '-path', '*/.git/*',
        '-not', '-path', '*/dist/*'],
      { cwd: WORKSPACE, encoding: 'utf-8' }
    ).split('\n').slice(0, 150).join('\n');
  } catch (e) {
    sendLog(ws, `Warning: file tree generation failed: ${e}\n`);
  }
  const fileTreeSection = fileTree
    ? `## Repository File Tree (first 150 files)\n\`\`\`\n${fileTree}\n\`\`\`\n\n`
    : '';

  const directionsText = directions.length > 0
    ? directions.map((d, i) => {
        const pathScope = Array.isArray(d.paths) && (d.paths as string[]).length > 0
          ? ` [scope: ${(d.paths as string[]).join(', ')}]`
          : '';
        return `${i + 1}. [${d.priority || 'medium'}] ${d.text}${pathScope} (id: ${d.id})`;
      }).join('\n')
    : 'No specific directions provided. Identify general improvements.';

  const projectCtx = task.project_context as Record<string, unknown> | undefined;
  const contextPrefix = projectCtx && Object.keys(projectCtx).some(k => {
    const v = projectCtx[k]; return Array.isArray(v) && v.length > 0;
  }) ? formatProjectContext(projectCtx) + '\n\n' : '';

  const recentlyCompleted = Array.isArray(projectCtx?.recently_completed_tasks)
    ? (projectCtx.recently_completed_tasks as Array<{title: string, task_type: string}>)
    : [];

  const researchPrompt = `${contextPrefix}${codebaseSummary}${fileTreeSection}${gitHistorySection}You are a codebase research agent. You have full access to this repository. Your job is to analyze the codebase and produce structured findings that will be used to create concrete implementation tasks.

${userPrompt ? `Research focus:\n${userPrompt}\n` : ''}Current project directions from the user:
${directionsText}

Recently completed tasks (DO NOT re-propose these — they were already implemented):
${recentlyCompleted.length > 0
  ? recentlyCompleted.map(t => `- [${t.task_type}] ${t.title}`).join('\n')
  : '(none yet)'}

Rules for avoiding duplication:
- Do not propose findings that are substantially the same as any recently completed task above.
- If a recently completed task partially addressed an issue and more work remains, you MAY propose a follow-up but must reference the prior work in your description.

Instructions:
1. Explore the codebase thoroughly. Read key files, understand the architecture, find patterns.
2. Based on the user's directions, identify specific, actionable improvements.
3. For each finding, assess scope and risk.
4. Output your findings as a JSON array as the VERY LAST thing in your response:

[
  {
    "type": "improvement",
    "title": "Short title (under 60 chars, conventional commit style)",
    "description": "1-3 sentence description of the change",
    "prompt": "The exact prompt to give to an implementing agent. Must be self-contained.",
    "scope": "small",
    "risk": "low",
    "priority": "high",
    "related_files": ["path/to/file1.py"],
    "depends_on_indices": [],
    "direction_id": "d-1 or null if self-discovered",
    "model": "claude-opus-4-6 | claude-sonnet-4-6 | null (use project default)",
    "effort": "high | medium | low | null (use project default)"
  }
]

Valid values:
- type: improvement, bug, refactor, feature, test, research, security_audit, qa, deps, docs, migrate, perf
  (use "research" when an area needs deeper investigation before implementation)
  (use "security_audit" when the finding involves security vulnerabilities that need a dedicated audit)
  (use "qa" when the finding is about missing test coverage)
  (use "deps" when the finding is about outdated or insecure dependencies)
  (use "docs" when the finding is about missing or outdated documentation)
  (use "migrate" when the finding requires a database schema migration via Alembic)
  (use "perf" when the finding involves performance bottlenecks — database queries, API latency, frontend bundle size)
- scope: small (single file, under 50 lines), medium (2-5 files, 50-200 lines), large (cross-component, architectural, orchestrator/model/agent/security changes — anything that could break the system if done wrong)
- risk: low (tests, docs, cosmetic), medium (logic changes, new endpoints), high (core logic, auth, data models)
- priority: high, medium, low

Rules:
- When a direction includes a [scope: path/prefix] annotation, limit your file exploration and findings to files under those path prefixes. Findings outside the scoped paths should only be included if they are blockers.
- Do NOT propose tasks for changes that appear in the Recent Git History above — those are already done or in progress.
- Each finding should be ONE PR-sized unit of work (scope=small) or a plannable initiative (scope=medium/large)
- The prompt field must be self-contained — the implementing agent knows nothing about this research
- depends_on_indices uses 0-based array positions. If findings involve structural refactors of the same files (renames, interface changes, restructuring), set depends_on_indices so they run sequentially. Small additive changes to shared files can stay parallel.
- Check the Sibling Tasks listed above (if any). Do not propose findings that duplicate or conflict with work those sibling tasks are already covering. If a sibling task is working on the same files, either skip that area or ensure your findings are clearly additive and non-overlapping.
- Limit to 30 findings maximum, prioritized by impact
- For medium/large scope findings, the prompt should describe the full scope — it will be sent to a planning agent for decomposition
- For type=research findings, the prompt should describe what needs deeper investigation — it will spawn a sub-research task
- For type=security_audit findings, the prompt should describe the audit scope/focus — it will spawn a dedicated security audit agent
- For type=qa findings, the prompt should describe what to test — it will spawn a dedicated QA agent
- For type=deps findings, the prompt can be minimal (e.g. 'Update dependencies') — it will spawn a dedicated dependency update agent
- For type=docs findings, the prompt can be minimal (e.g. 'Update documentation') — it will spawn a dedicated documentation agent
- Use model=claude-opus-4-6 and effort=high only for complex architectural changes. Leave null for most findings to use the project default.
- Do NOT propose findings that duplicate recently completed tasks listed above.

How your findings get routed (you control this via type and scope):
- type=security_audit → spawns a **security audit** agent that scans for vulnerabilities and auto-spawns fix tasks for critical/high findings
- type=qa → spawns a **QA** agent that generates missing tests and opens a PR
- type=deps → spawns a **dependency updater** agent that checks for outdated/insecure deps and opens a PR
- type=docs → spawns a **docs** agent that generates/updates documentation and opens a PR
- type=migrate → spawns a **migration** agent that generates Alembic migrations and opens a PR
- type=perf → spawns a **performance** agent that scans for bottlenecks and auto-spawns fix tasks for high-severity findings
- type=research → spawns another **research** agent for deeper investigation before any implementation
- type=improvement/bug/refactor/feature/test with scope=large → spawns a **plan** agent that decomposes into subtasks
- type=improvement/bug/refactor/feature/test with scope=small or medium → spawns a **create** agent (single PR)
- IMPORTANT: Write your findings JSON array to /tmp/research_findings.json (NOT in the repository). The file should contain ONLY the JSON array, no markdown wrapping.

Context — you are part of an autonomous pipeline:
- You are a research agent inside Blueprint, an autonomous AI coding system. Your findings will be automatically implemented by other agents, reviewed by review agents, and merged. After merging, another research cycle may run and build on your work.
- Think iteratively. You don't need to solve everything in one pass. Prefer laying clean foundations that future research cycles can build on over shipping everything at once.
- Keep the codebase clean. Other agents will read and modify the same code after you. Messy patterns multiply — extensible patterns compound.
- Use type=research findings for areas that need deeper investigation before implementation. This spawns a sub-research agent to dig in further. Don't rush to implement something you're uncertain about.

Approach:
- This is a full-stack system. Consider ALL layers: backend (Python/FastAPI), frontend (React/TypeScript), agent (Node.js/TypeScript), and infrastructure. Changes that touch one layer usually need corresponding changes in other layers.
- Prefer extensible, data-driven designs over copy-paste. If adding a new instance of something (a new type, a new endpoint, a new UI panel), first check if the pattern should be generalized.
- Frontend changes are just as important as backend changes. Every new backend capability needs corresponding UI to be useful.
- Architectural refactors (making something extensible, adding a registry, restructuring) are valuable findings. Include them when the current pattern won't scale.`;

  const researchPromptSuffix = payload.prompt_suffix ? `\n\nAdditional instructions:\n${payload.prompt_suffix}` : '';
  statusRef.value = 'Running codebase research...';
  const { exitCode, allOutput, lastResultText } = await runClaudeCode(researchPrompt + researchPromptSuffix, ws, statusRef, model, effort);

  // Prefer reading findings from /tmp/ file (avoids output truncation), fall back to output parsing
  let logs = '';
  const findingsPath = '/tmp/research_findings.json';
  try {
    if (fs.existsSync(findingsPath)) {
      logs = sanitize(fs.readFileSync(findingsPath, 'utf-8'));
      sendLog(ws, 'Read research findings from /tmp/research_findings.json\n');
    }
  } catch (e) {
    sendLog(ws, `Failed to read ${findingsPath}, falling back to output parsing\n`);
  }
  if (!logs) {
    if (lastResultText && lastResultText.includes('"prompt"')) {
      logs = sanitize(lastResultText);
    } else {
      const jsonMatch = allOutput.match(/\[\s*\{[\s\S]*"prompt"[\s\S]*\}\s*\]/);
      logs = sanitize(jsonMatch ? jsonMatch[0] : (lastResultText || allOutput.slice(-50_000)));
    }
  }

  await apiPost('/api/agent/status', {
    task_id: taskId,
    status: exitCode === 0 ? 'COMPLETED' : 'FAILED',
    ...(exitCode !== 0 ? { artifact_type: 'ERROR_LOG' } : { artifact_type: 'RESEARCH_FINDINGS' }),
    logs,
  });
}

// ── Handler: Docs (analyze codebase, generate/update documentation, open PR) ─
async function handleDocs(
  taskId: string, payload: Record<string, unknown>, task: Record<string, unknown>,
  ghToken: string, owner: string, repo: string, ws: WebSocket | null, statusRef: { value: string },
  model?: string, effort?: string
) {
  const rawBaseBranch = String(payload.base_branch || 'main');
  const baseBranch = validateBranchName(rawBaseBranch, 'base_branch');

  // Clone and setup workspace
  const { workBranch, shortId } = await setupWorkspace(owner, repo, ghToken, baseBranch, 'docs', taskId, ws, statusRef);

  const docsPrompt = `You are a documentation agent. Your job is to improve the documentation of this codebase.

Steps:
1. Analyze the codebase to understand what it does — read the README, key source files, API routes, configuration, and entry points.
2. Identify documentation gaps:
   - Missing or outdated README sections (installation, usage, architecture, configuration)
   - Undocumented public functions, classes, or modules (missing docstrings)
   - Undocumented API endpoints
   - Missing setup or deployment instructions
3. Generate or update documentation to fill those gaps:
   - Improve the README with accurate installation, usage, and architecture sections
   - Add or update docstrings for public functions and classes
   - Create or update API documentation where applicable
4. Focus on accuracy — only document what actually exists in the code. Do not invent features or describe things that don't exist.
5. Do NOT change any production code. Only modify documentation files (README, docs/, etc.) and add/update docstrings within existing source files.

Be thorough but precise. Every statement in the documentation must reflect the actual code.`;

  const docsPromptSuffix = payload.prompt_suffix ? `\n\nAdditional instructions:\n${payload.prompt_suffix}` : '';
  statusRef.value = 'Running Claude Code...';
  const { exitCode, allOutput, lastResultText } = await runClaudeCode(docsPrompt + docsPromptSuffix, ws, statusRef, model, effort);

  const diff = git(['status', '--porcelain']);
  const hasChanges = diff.length > 0;

  if (hasChanges && exitCode === 0) {
    statusRef.value = 'Pushing changes...';
    sendLog(ws, '\nCommitting and pushing changes...\n');

    git(['add', '-A']);
    execFileSync('git', ['-C', WORKSPACE, 'commit', '-m', `docs: update documentation\n\nAutomated by Blueprint Agent (task: ${shortId})`], { encoding: 'utf-8' });


    const prBody = lastResultText
      ? `## Summary\n\n${lastResultText.slice(0, 3000)}\n\n---\n*Automated by Blueprint Agent*`
      : `Documentation improvements\n\n---\n*Automated by Blueprint Agent*`;
    const prUrl = await pushAndCreatePR(owner, repo, taskId, workBranch, baseBranch, 'docs: update documentation', prBody, ws, statusRef);
    const commitHash = git(['rev-parse', 'HEAD']);

    await apiPost('/api/agent/status', {
      task_id: taskId, status: 'COMPLETED', artifact_type: 'PULL_REQUEST',
      commit_hash: commitHash, pr_url: prUrl, repo_url: `https://github.com/${owner}/${repo}`,
    });
  } else if (exitCode === 0) {
    await apiPost('/api/agent/status', { task_id: taskId, status: 'COMPLETED', logs: 'No documentation changes needed.' });
  } else {
    await apiPost('/api/agent/status', {
      task_id: taskId, status: 'FAILED', artifact_type: 'ERROR_LOG', logs: sanitize(allOutput.slice(-10_000)),
    });
  }
}

// ── Handler: Security Audit (analyze codebase for vulnerabilities, output findings) ──
async function handleSecurityAudit(
  taskId: string, payload: Record<string, unknown>, task: Record<string, unknown>,
  ghToken: string, owner: string, repo: string, ws: WebSocket | null, statusRef: { value: string },
  model?: string, effort?: string
) {
  const rawBaseBranch = String(payload.base_branch || 'main');
  const baseBranch = validateBranchName(rawBaseBranch, 'base_branch');

  statusRef.value = `Cloning ${owner}/${repo} for security audit...`;
  sendLog(ws, `Starting security audit...\n`);
  execFileSync('rm', ['-rf', WORKSPACE], { encoding: 'utf-8' });
  const cloneUrl = `https://x-access-token:${ghToken}@github.com/${owner}/${repo}.git`;
  execFileSync('git', ['clone', '--branch', baseBranch, cloneUrl, WORKSPACE], { encoding: 'utf-8', timeout: 300_000 });

  const projectCtx = task.project_context as Record<string, unknown> | undefined;
  const contextPrefix = projectCtx && Object.keys(projectCtx).some(k => {
    const v = projectCtx[k]; return Array.isArray(v) && v.length > 0;
  }) ? formatProjectContext(projectCtx) + '\n\n' : '';

  const claudeMdPath = `${WORKSPACE}/CLAUDE.md`;
  const claudeMd = fs.existsSync(claudeMdPath)
    ? fs.readFileSync(claudeMdPath, 'utf-8').slice(0, 2000)
    : '';
  const codebaseSummary = claudeMd
    ? `## Codebase Instructions\n${claudeMd}\n\n`
    : '';

  let fileTree = '';
  try {
    fileTree = execFileSync(
      'find', ['.', '-type', 'f',
        '-not', '-path', '*/node_modules/*',
        '-not', '-path', '*/.git/*',
        '-not', '-path', '*/dist/*'],
      { cwd: WORKSPACE, encoding: 'utf-8' }
    ).split('\n').slice(0, 150).join('\n');
  } catch (e) {
    sendLog(ws, `Warning: file tree generation failed: ${e}\n`);
  }
  const fileTreeSection = fileTree
    ? `## Repository File Tree (first 150 files)\n\`\`\`\n${fileTree}\n\`\`\`\n\n`
    : '';

  const auditScope = String(payload.prompt || payload.description || '');
  const auditScopeSection = auditScope ? `Audit focus: ${auditScope}\n\n` : '';

  const securityPrompt = `${contextPrefix}${codebaseSummary}${fileTreeSection}${auditScopeSection}You are a security audit agent. You have full access to this repository. Your job is to analyze the codebase for security vulnerabilities and produce structured findings.

Analyze the codebase for the following vulnerability categories:
1. OWASP Top 10 vulnerabilities
2. Hardcoded secrets, credentials, or API keys
3. SQL injection risks
4. Cross-site scripting (XSS) risks
5. Insecure dependencies (outdated packages with known CVEs)
6. Authentication and authorization issues
7. Unsafe file operations (path traversal, arbitrary file read/write)

Instructions:
1. Explore the codebase thoroughly. Read key files, understand the architecture, find patterns.
2. For each vulnerability found, assess its severity and provide a concrete fix prompt.
3. Output your findings as a JSON array as the VERY LAST thing in your response:

[
  {
    "title": "Short title (under 60 chars)",
    "description": "1-3 sentence description of the vulnerability",
    "severity": "critical",
    "file_path": "path/to/affected/file.py",
    "line_number": 42,
    "fix_prompt": "The exact prompt to give to a fixing agent. Must be self-contained and describe exactly what to change."
  }
]

Valid severity values: critical, high, medium, low

Rules:
- Only report genuine vulnerabilities, not theoretical ones
- Each finding must have a concrete, actionable fix_prompt
- Limit to 20 findings maximum, prioritized by severity
- For findings without a specific line number, use 0
- IMPORTANT: Write your findings JSON array to /tmp/security_findings.json (NOT in the repository). The file should contain ONLY the JSON array, no markdown wrapping.
- Also output the same JSON array as the final output in your logs.`;

  const securityPromptSuffix = payload.prompt_suffix ? `\n\nAdditional instructions:\n${payload.prompt_suffix}` : '';
  statusRef.value = 'Running security audit...';
  const { exitCode, allOutput, lastResultText } = await runClaudeCode(securityPrompt + securityPromptSuffix, ws, statusRef, model, effort);

  let logs = '';
  const findingsPath = '/tmp/security_findings.json';
  try {
    if (fs.existsSync(findingsPath)) {
      logs = sanitize(fs.readFileSync(findingsPath, 'utf-8'));
      sendLog(ws, 'Read security findings from /tmp/security_findings.json\n');
    }
  } catch (e) {
    sendLog(ws, `Failed to read ${findingsPath}, falling back to output parsing\n`);
  }
  if (!logs) {
    if (lastResultText && lastResultText.includes('"severity"')) {
      logs = sanitize(lastResultText);
    } else {
      const jsonMatch = allOutput.match(/\[\s*\{[\s\S]*"severity"[\s\S]*\}\s*\]/);
      logs = sanitize(jsonMatch ? jsonMatch[0] : (lastResultText || allOutput.slice(-50_000)));
    }
  }

  await apiPost('/api/agent/status', {
    task_id: taskId,
    status: exitCode === 0 ? 'COMPLETED' : 'FAILED',
    ...(exitCode !== 0 ? { artifact_type: 'ERROR_LOG' } : { artifact_type: 'SECURITY_REPORT' }),
    logs,
  });
}

// ── Handler: QA (analyze coverage, generate tests, open PR) ─────────────────
async function handleQA(
  taskId: string, payload: Record<string, unknown>, task: Record<string, unknown>,
  ghToken: string, owner: string, repo: string, ws: WebSocket | null, statusRef: { value: string },
  model?: string, effort?: string
) {
  const rawPrompt = String(payload.prompt || payload.description || task.description || '');
  const rawBaseBranch = String(payload.base_branch || 'main');
  const rawTitle = String(task.title || payload.title || 'add test coverage');

  const baseBranch = validateBranchName(rawBaseBranch, 'base_branch');
  const taskTitle = rawTitle.replace(/[`$\\!"';&|<>(){}[\]]/g, '');

  // Clone
  statusRef.value = `Cloning ${owner}/${repo}...`;
  sendLog(ws, `Cloning ${owner}/${repo}...\n`);
  execFileSync('rm', ['-rf', WORKSPACE], { encoding: 'utf-8' });
  const cloneUrl = `https://x-access-token:${ghToken}@github.com/${owner}/${repo}.git`;
  execFileSync('git', ['clone', '--depth', '100', '--branch', baseBranch, cloneUrl, WORKSPACE], { encoding: 'utf-8', timeout: 300_000 });

  git(['config', 'user.email', 'blueprint-agent@automated.dev']);
  git(['config', 'user.name', 'Blueprint Agent']);

  const shortId = taskId.replace(/-/g, '').slice(0, 8);
  const workBranch = `blueprint/task-${shortId}`;
  let staleBranchExists = false;
  try {
    git(['ls-remote', '--exit-code', 'origin', workBranch]);
    staleBranchExists = true;
  } catch { /* branch doesn't exist on remote — normal case */ }
  if (staleBranchExists) {
    try {
      git(['push', 'origin', '--delete', workBranch]);
      sendLog(ws, `Deleted stale remote branch ${workBranch} from previous attempt\n`);
    } catch (e) {
      sendLog(ws, `WARNING: Could not delete stale remote branch ${workBranch}: ${e instanceof Error ? e.message : e}\n`);
    }
  }
  git(['checkout', '-b', workBranch]);
  sendLog(ws, `Working branch: ${workBranch}\n`);

  const projectCtx = task.project_context as Record<string, unknown> | undefined;
  const contextPrefix = projectCtx && Object.keys(projectCtx).some(k => {
    const v = projectCtx[k]; return Array.isArray(v) && v.length > 0;
  }) ? formatProjectContext(projectCtx) + '\n\n' : '';
  const parentCtx = (task.context as Record<string, unknown> | undefined)?.parent_context as Record<string, unknown> | undefined;
  const parentContextSection = parentCtx?.parent_prompt
    ? `## Parent Task Context\nThis task is a subtask of: **${parentCtx.parent_title}**\nParent goal: ${parentCtx.parent_prompt}\n\n`
    : '';

  const qaPrompt = `${contextPrefix}${parentContextSection}You are a QA agent. Your job is to improve test coverage for this codebase.

${rawPrompt ? `Task context:\n${rawPrompt}\n\n` : ''}Instructions:
1. Analyze the existing test files to understand the test framework, patterns, and conventions used.
2. Identify functions, modules, or code paths that have no or insufficient test coverage.
3. Write comprehensive test files (unit tests and/or integration tests) following the exact same patterns and conventions as the existing tests.
4. Only add new test files or append tests to existing test files — do NOT modify any production code.
5. After writing the tests, run them to verify they pass. Fix any failures before finishing.

IMPORTANT RULES:
- Follow the existing test framework and patterns exactly (same imports, same assertion style, same file naming)
- Do not change production source files under any circumstances
- Each test should be self-contained and not depend on external state
- Cover edge cases and error paths, not just happy paths
- If a test requires mocking, use the same mocking approach as existing tests`;

  // Run Claude
  const qaPromptSuffix = payload.prompt_suffix ? `\n\nAdditional instructions:\n${payload.prompt_suffix}` : '';
  statusRef.value = 'Running Claude Code...';
  const { exitCode, allOutput, lastResultText } = await runClaudeCode(qaPrompt + qaPromptSuffix, ws, statusRef, model, effort);

  const diff = git(['status', '--porcelain']);
  const hasChanges = diff.length > 0;

  if (hasChanges && exitCode === 0) {
    statusRef.value = 'Pushing changes...';
    sendLog(ws, '\nCommitting and pushing changes...\n');

    git(['add', '-A']);
    execFileSync('git', ['-C', WORKSPACE, 'commit', '-m', `test: ${taskTitle}\n\nAutomated by Blueprint Agent (task: ${shortId})`], { encoding: 'utf-8' });

    try {
      git(['fetch', 'origin', baseBranch]);
      git(['rebase', `origin/${baseBranch}`]);
      sendLog(ws, `Rebased onto latest ${baseBranch}\n`);
    } catch {
      sendLog(ws, `Rebase onto ${baseBranch} had conflicts — pushing as-is\n`);
      try { git(['rebase', '--abort']); } catch {}
    }

    const commitHash = git(['rev-parse', 'HEAD']);

    const freshGhRes = await apiGet(`/api/agent/github/token?task_id=${taskId}`);
    const pushUrl = `https://x-access-token:${freshGhRes.data.token}@github.com/${owner}/${repo}.git`;
    execFileSync('git', ['-C', WORKSPACE, 'remote', 'set-url', 'origin', pushUrl], { encoding: 'utf-8' });
    git(['push', 'origin', workBranch]);
    sendLog(ws, `Pushed to branch: ${workBranch}\n`);

    statusRef.value = 'Creating pull request...';
    let prUrl = '';
    try {
      const prBody = lastResultText
        ? `## Summary\n\n${lastResultText.slice(0, 3000)}\n\n---\n*Automated by Blueprint Agent*`
        : `Test coverage improvements for: ${taskTitle}\n\n---\n*Automated by Blueprint Agent*`;
      const prRes = await apiPost('/api/agent/github/pull-requests', {
        task_id: taskId, owner, repo, title: `test: add test coverage for ${taskTitle}`,
        head: workBranch, base: baseBranch, body: prBody,
      });
      prUrl = prRes.data?.html_url || '';
      sendLog(ws, `PR created: ${prUrl}\n`);
    } catch (e) { sendLog(ws, `Failed to create PR: ${e}\n`); }

    await apiPost('/api/agent/status', {
      task_id: taskId, status: 'COMPLETED', artifact_type: 'PULL_REQUEST',
      commit_hash: commitHash, pr_url: prUrl, repo_url: `https://github.com/${owner}/${repo}`,
    });
  } else if (exitCode === 0) {
    await apiPost('/api/agent/status', { task_id: taskId, status: 'COMPLETED', logs: 'No test files added.' });
  } else {
    await apiPost('/api/agent/status', {
      task_id: taskId, status: 'FAILED', artifact_type: 'ERROR_LOG', logs: sanitize(allOutput.slice(-10_000)),
    });
  }
}

// ── Handler: Deps (update dependencies to latest minor/patch versions) ─────
async function handleDeps(
  taskId: string, payload: Record<string, unknown>, task: Record<string, unknown>,
  ghToken: string, owner: string, repo: string, ws: WebSocket | null, statusRef: { value: string },
  model?: string, effort?: string
) {
  const rawBaseBranch = String(payload.base_branch || 'main');
  const baseBranch = validateBranchName(rawBaseBranch, 'base_branch');

  // Clone and setup workspace
  const { workBranch, shortId } = await setupWorkspace(owner, repo, ghToken, baseBranch, 'deps', taskId, ws, statusRef);

  const depsPrompt = `You are a dependency update agent. Your job is to update this project's dependencies to their latest MINOR and PATCH versions — do NOT make major version bumps as those can introduce breaking changes.

Steps to follow:
1. Detect which package managers are in use by checking for these files:
   - package.json → npm/yarn/pnpm
   - requirements.txt → pip
   - pyproject.toml → poetry or pip
   - Cargo.toml → cargo
   - go.mod → go mod
   - Gemfile → bundler

2. For each package manager found, check what's outdated:
   - npm: run \`npm outdated\` (or \`yarn outdated\` / \`pnpm outdated\`)
   - pip: run \`pip list --outdated\`
   - poetry: run \`poetry show --outdated\`
   - cargo: run \`cargo outdated\`
   - go: run \`go list -u -m all\`

3. Update dependencies to their latest MINOR/PATCH versions only:
   - npm: run \`npm update\` (respects semver ranges) or update package.json version ranges then \`npm install\`
   - pip: update requirements.txt with new versions
   - poetry: run \`poetry update\`
   - cargo: run \`cargo update\`
   - go: run \`go get -u ./...\` (patch only) then \`go mod tidy\`

4. Run the project's test suite to verify nothing broke:
   - Look for test scripts in package.json (e.g. \`npm test\`, \`npm run test\`)
   - Or pytest, cargo test, go test, etc.
   - If tests fail, identify which dependency update caused the failure and revert just that package to its previous version, then re-run tests.

5. Once tests pass, the dependency files should already be modified. Do NOT commit — the agent harness will handle the commit.

IMPORTANT: Only update minor/patch versions. If a package would require a major version bump to get any update, skip it. Focus on security patches and bug fixes.`;

  const depsPromptSuffix = payload.prompt_suffix ? `\n\nAdditional instructions:\n${payload.prompt_suffix}` : '';
  statusRef.value = 'Running dependency updates...';
  const { exitCode, allOutput, lastResultText } = await runClaudeCode(depsPrompt + depsPromptSuffix, ws, statusRef, model, effort);

  const diff = git(['status', '--porcelain']);
  const hasChanges = diff.length > 0;

  if (hasChanges && exitCode === 0) {
    statusRef.value = 'Pushing changes...';
    sendLog(ws, '\nCommitting and pushing dependency updates...\n');

    git(['add', '-A']);
    execFileSync('git', ['-C', WORKSPACE, 'commit', '-m', `chore: update dependencies to latest minor/patch versions\n\nAutomated by Blueprint Agent (task: ${shortId})`], { encoding: 'utf-8' });

    const prBody = lastResultText
      ? `## Summary\n\n${lastResultText.slice(0, 3000)}\n\n---\n*Automated by Blueprint Agent*`
      : `Automated dependency updates: minor/patch versions only.\n\n---\n*Automated by Blueprint Agent*`;
    const prUrl = await pushAndCreatePR(owner, repo, taskId, workBranch, baseBranch, 'chore: update dependencies', prBody, ws, statusRef);
    const commitHash = git(['rev-parse', 'HEAD']);

    await apiPost('/api/agent/status', {
      task_id: taskId, status: 'COMPLETED', artifact_type: 'PULL_REQUEST',
      commit_hash: commitHash, pr_url: prUrl, repo_url: `https://github.com/${owner}/${repo}`,
    });
  } else if (exitCode === 0) {
    await apiPost('/api/agent/status', { task_id: taskId, status: 'COMPLETED', logs: 'No dependency updates needed.' });
  } else {
    await apiPost('/api/agent/status', {
      task_id: taskId, status: 'FAILED', artifact_type: 'ERROR_LOG', logs: sanitize(allOutput.slice(-10_000)),
    });
  }
}

// ── Handler: Migrate (Alembic/SQLAlchemy database migration workflow) ────────
async function handleMigrate(
  taskId: string, payload: Record<string, unknown>, task: Record<string, unknown>,
  ghToken: string, owner: string, repo: string, ws: WebSocket | null, statusRef: { value: string },
  model?: string, effort?: string
) {
  const rawPrompt = String(payload.prompt || payload.description || task.description || '');
  const rawBaseBranch = String(payload.base_branch || 'main');
  const rawTitle = String(task.title || payload.title || 'database migration');
  const prompt = task.title ? `Task: ${task.title}\n\n${rawPrompt}` : rawPrompt;

  const baseBranch = validateBranchName(rawBaseBranch, 'base_branch');
  const taskTitle = rawTitle.replace(/[`$\\!"';&|<>(){}[\]]/g, '');
  if (!prompt) throw new Error('No prompt found in task payloads');

  // Clone
  statusRef.value = `Cloning ${owner}/${repo}...`;
  sendLog(ws, `Cloning ${owner}/${repo}...\n`);
  execFileSync('rm', ['-rf', WORKSPACE], { encoding: 'utf-8' });
  const cloneUrl = `https://x-access-token:${ghToken}@github.com/${owner}/${repo}.git`;
  execFileSync('git', ['clone', '--depth', '100', '--branch', baseBranch, cloneUrl, WORKSPACE], { encoding: 'utf-8', timeout: 300_000 });

  git(['config', 'user.email', 'blueprint-agent@automated.dev']);
  git(['config', 'user.name', 'Blueprint Agent']);

  const shortId = taskId.replace(/-/g, '').slice(0, 8);
  const workBranch = `blueprint/migrate-${shortId}`;
  let staleBranchExists = false;
  try {
    git(['ls-remote', '--exit-code', 'origin', workBranch]);
    staleBranchExists = true;
  } catch { /* branch doesn't exist on remote — normal case */ }
  if (staleBranchExists) {
    try {
      git(['push', 'origin', '--delete', workBranch]);
      sendLog(ws, `Deleted stale remote branch ${workBranch} from previous attempt\n`);
    } catch (e) {
      sendLog(ws, `WARNING: Could not delete stale remote branch ${workBranch}: ${e instanceof Error ? e.message : e}\n`);
    }
  }
  git(['checkout', '-b', workBranch]);
  sendLog(ws, `Working branch: ${workBranch}\n`);

  const migratePrompt = `You are a database migration agent specializing in Alembic and SQLAlchemy/SQLModel workflows.

Your task: ${prompt}

Follow these steps precisely:

1. **Examine existing migrations**: Look at \`backend/alembic/versions/\` to understand naming conventions, import patterns, and how previous migrations are structured. Note the revision ID format, how dependencies are chained, and any project-specific patterns.

2. **Make model changes**: Apply the requested SQLModel/SQLAlchemy model changes in the appropriate files (typically under \`backend/app/models/\` or similar). Do NOT manually write migration files — autogenerate will handle that.

3. **Generate migration**: Run \`alembic revision --autogenerate -m "description"\` from the backend directory (where alembic.ini lives). Use a concise, snake_case description that matches existing naming conventions.

   IMPORTANT: Do NOT hand-edit the generated migration file unless autogenerate produced clearly incorrect output (e.g., dropped the wrong table, missed an index, used wrong column type). If you must edit, add a comment explaining why.

4. **Review the generated migration**: Open and carefully read the generated file. Check for:
   - Data loss risks (e.g., \`DROP COLUMN\`, \`DROP TABLE\`, type changes that truncate data)
   - Missing indexes on foreign keys or frequently-queried columns
   - Incorrect \`nullable\` settings
   - Server defaults that should be included for backfills
   - \`op.execute()\` statements needed for data migrations
   If you find issues, fix them and document your changes with inline comments.

5. **Test the migration applies cleanly**: Run \`alembic upgrade head\` to verify the migration applies without errors. If it fails:
   - Read the error carefully
   - Fix the migration file (add a comment explaining the manual fix)
   - Run \`alembic upgrade head\` again
   - Repeat until it succeeds

6. **Test downgrade** (if the migration is reversible): Run \`alembic downgrade -1\` then \`alembic upgrade head\` to confirm round-trip works. If downgrade is not possible (e.g., data loss), ensure the \`downgrade()\` function raises \`NotImplementedError\` with an explanation.

7. Once migration applies cleanly, the files should be modified. Do NOT commit — the agent harness will handle the commit.

CRITICAL RULES:
- Never delete or modify existing migration files in \`backend/alembic/versions/\` (except the one you just generated)
- Never run \`alembic stamp\` or \`alembic downgrade base\` — these can corrupt production databases
- If autogenerate produces an empty migration with no changes, verify that your model changes were saved and the alembic env.py is configured to detect them
- Document any data loss risks prominently using inline comments in the migration file`;

  const migratePromptSuffix = payload.prompt_suffix ? `\n\nAdditional instructions:\n${payload.prompt_suffix}` : '';
  statusRef.value = 'Running migration workflow...';
  const { exitCode, allOutput, lastResultText } = await runClaudeCode(migratePrompt + migratePromptSuffix, ws, statusRef, model, effort);

  const diff = git(['status', '--porcelain']);
  const hasChanges = diff.length > 0;

  if (hasChanges && exitCode === 0) {
    statusRef.value = 'Pushing changes...';
    sendLog(ws, '\nCommitting and pushing migration changes...\n');

    git(['add', '-A']);
    execFileSync('git', ['-C', WORKSPACE, 'commit', '-m', `migrate: ${taskTitle}\n\nAutomated by Blueprint Agent (task: ${shortId})`], { encoding: 'utf-8' });

    // Rebase onto latest base branch before push
    statusRef.value = `Rebasing onto ${baseBranch}...`;
    try {
      git(['fetch', 'origin', baseBranch]);
      git(['rebase', `origin/${baseBranch}`]);
      sendLog(ws, `Rebased onto latest ${baseBranch}\n`);
    } catch {
      sendLog(ws, `Rebase onto ${baseBranch} had conflicts — pushing as-is\n`);
      try { git(['rebase', '--abort']); } catch {}
    }

    const commitHash = git(['rev-parse', 'HEAD']);

    const freshGhRes = await apiGet(`/api/agent/github/token?task_id=${taskId}`);
    const pushUrl = `https://x-access-token:${freshGhRes.data.token}@github.com/${owner}/${repo}.git`;
    execFileSync('git', ['-C', WORKSPACE, 'remote', 'set-url', 'origin', pushUrl], { encoding: 'utf-8' });
    git(['push', 'origin', workBranch]);
    sendLog(ws, `Pushed to branch: ${workBranch}\n`);

    statusRef.value = 'Creating pull request...';
    let prUrl = '';
    try {
      const prBody = lastResultText
        ? `## Summary\n\n${lastResultText.slice(0, 3000)}\n\n---\n*Automated by Blueprint Agent*`
        : `Database migration: ${taskTitle}\n\n---\n*Automated by Blueprint Agent*`;
      const prRes = await apiPost('/api/agent/github/pull-requests', {
        task_id: taskId, owner, repo, title: `migrate: ${taskTitle}`,
        head: workBranch, base: baseBranch, body: prBody,
      });
      prUrl = prRes.data?.html_url || '';
      sendLog(ws, `PR created: ${prUrl}\n`);
    } catch (e) { sendLog(ws, `Failed to create PR: ${e}\n`); }

    await apiPost('/api/agent/status', {
      task_id: taskId, status: 'COMPLETED', artifact_type: 'PULL_REQUEST',
      commit_hash: commitHash, pr_url: prUrl, repo_url: `https://github.com/${owner}/${repo}`,
    });
  } else if (exitCode === 0) {
    await apiPost('/api/agent/status', { task_id: taskId, status: 'COMPLETED', logs: 'No migration changes generated.' });
  } else {
    await apiPost('/api/agent/status', {
      task_id: taskId, status: 'FAILED', artifact_type: 'ERROR_LOG', logs: sanitize(allOutput.slice(-10_000)),
    });
  }
}

// ── Handler: Smoketest (post-merge end-to-end health checks) ─────────────────
async function handleSmoketest(
  taskId: string, payload: Record<string, unknown>, _task: Record<string, unknown>,
  ghToken: string, owner: string, repo: string, ws: WebSocket | null, statusRef: { value: string },
  model?: string, effort?: string
) {
  const rawBaseBranch = String(payload.base_branch || 'main');
  const baseBranch = validateBranchName(rawBaseBranch, 'base_branch');

  // Clone (read-only — no work branch or push needed)
  statusRef.value = `Cloning ${owner}/${repo}...`;
  sendLog(ws, `Cloning ${owner}/${repo} @ ${baseBranch}...\n`);
  execFileSync('rm', ['-rf', WORKSPACE], { encoding: 'utf-8' });
  const cloneUrl = `https://x-access-token:${ghToken}@github.com/${owner}/${repo}.git`;
  execFileSync('git', ['clone', '--depth', '100', '--branch', baseBranch, cloneUrl, WORKSPACE], { encoding: 'utf-8', timeout: 300_000 });

  const smoketestPrompt = `You are a smoke test agent. Your job is to run basic end-to-end health checks on this codebase.

Follow these steps:

1. Read the project README to understand the project structure, how to run it, and any existing smoke test setup.

2. Look for existing smoke test scripts:
   - Check for files matching scripts/smoke*.sh or tests/smoke*.py (or similar names)
   - Also check for health check scripts, integration test entry points, or CI smoke test jobs in .github/workflows/

3. If a smoke test script exists:
   - Run it and capture the output
   - Record each test that passes or fails

4. If no smoke test script exists:
   - Identify the main entry points from the code (API server, health endpoints, key routes)
   - Run quick sanity checks: (a) verify the app can start without import errors, (b) check key health/ping endpoints return 200 using curl or the test framework, (c) confirm critical dependencies are importable
   - Use the project's own test runner (pytest, jest, etc.) with a smoke/health filter if available

5. Write the results to /tmp/smoketest_result.json with this exact structure:
{
  "passed": true,
  "tests": [
    {"name": "test name", "status": "pass", "message": "details"}
  ]
}

Set "passed" to true only if ALL tests have status "pass" or "skip". Set it to false if any test has status "fail".

IMPORTANT: Always write /tmp/smoketest_result.json even if you cannot run any tests (use status "skip" with a message explaining why). Never leave the file unwritten.`;

  statusRef.value = 'Running smoke tests...';
  const { exitCode, allOutput } = await runClaudeCode(smoketestPrompt, ws, statusRef, model, effort);

  // Read result file written by Claude
  let smoketestResult: { passed: boolean; tests: Array<{ name: string; status: string; message: string }> } | null = null;
  try {
    const resultJson = fs.readFileSync('/tmp/smoketest_result.json', 'utf-8');
    smoketestResult = JSON.parse(resultJson);
  } catch {
    sendLog(ws, 'Warning: /tmp/smoketest_result.json not found or invalid\n');
  }

  if (exitCode === 0 && smoketestResult) {
    // Live endpoint checks
    const healthCheckUrls: string[] = Array.isArray(payload.health_check_urls)
      ? (payload.health_check_urls as string[])
      : [];
    if (healthCheckUrls.length > 0) {
      sendLog(ws, '\nLive endpoint checks:\n');
      for (const url of healthCheckUrls) {
        let curlStatus: string;
        let curlMessage: string;
        try {
          execFileSync('curl', ['-sf', '--max-time', '10', url], { encoding: 'utf-8' });
          curlStatus = 'pass';
          curlMessage = 'HTTP 2xx';
          sendLog(ws, `  PASS  ${url}\n`);
        } catch (err: unknown) {
          curlStatus = 'fail';
          const exitErr = err as { status?: number };
          curlMessage = exitErr.status !== undefined ? `curl exited with code ${exitErr.status}` : 'curl failed';
          sendLog(ws, `  FAIL  ${url} — ${curlMessage}\n`);
        }
        smoketestResult.tests.push({ name: `HTTP GET ${url}`, status: curlStatus, message: curlMessage });
      }
      // Recompute passed: true only if all tests pass or skip
      smoketestResult.passed = smoketestResult.tests.every(t => t.status === 'pass' || t.status === 'skip');
    }

    const passed = smoketestResult.passed;
    const passCount = smoketestResult.tests.filter(t => t.status === 'pass').length;
    const summary = `Smoke tests ${passed ? 'PASSED' : 'FAILED'}: ${passCount}/${smoketestResult.tests.length} passed`;
    sendLog(ws, `\n${summary}\n`);
    await apiPost('/api/agent/status', {
      task_id: taskId,
      status: passed ? 'COMPLETED' : 'FAILED',
      artifact_type: 'SMOKETEST_RESULT',
      logs: JSON.stringify(smoketestResult),
      repo_url: `https://github.com/${owner}/${repo}`,
    });
  } else {
    await apiPost('/api/agent/status', {
      task_id: taskId, status: 'FAILED', artifact_type: 'ERROR_LOG',
      logs: sanitize(allOutput.slice(-10_000)),
    });
  }
}

// ── Handler Registry ────────────────────────────────────────────────────────
type Handler = (
  taskId: string, payload: Record<string, unknown>, task: Record<string, unknown>,
  ghToken: string, owner: string, repo: string, ws: WebSocket | null, statusRef: { value: string },
  model?: string, effort?: string
) => Promise<void>;

// ── Handler: Refactor (improve code quality without changing behavior) ────────
async function handleRefactor(
  taskId: string, payload: Record<string, unknown>, task: Record<string, unknown>,
  ghToken: string, owner: string, repo: string, ws: WebSocket | null, statusRef: { value: string },
  model?: string, effort?: string
) {
  const rawPrompt = String(payload.prompt || payload.description || task.description || '');
  const rawBaseBranch = String(payload.base_branch || 'main');
  const rawTitle = String(task.title || payload.title || 'refactor');

  const baseBranch = validateBranchName(rawBaseBranch, 'base_branch');
  const taskTitle = rawTitle.replace(/[`$\\!"';&|<>(){}[\]]/g, '');
  if (!rawPrompt) throw new Error('No prompt found in task payloads');

  const projectCtx = task.project_context as Record<string, unknown> | undefined;
  const contextPrefix = projectCtx && Object.keys(projectCtx).some(k => {
    const v = projectCtx[k]; return Array.isArray(v) && v.length > 0;
  }) ? formatProjectContext(projectCtx) + '\n\n' : '';

  const refactorPrompt = `${contextPrefix}REFACTOR TASK — STRICT CONSTRAINTS:
- Do NOT add new features or new behavior.
- Do NOT change any observable behavior, API contracts, or public interfaces.
- Focus on ONE refactor concern per task (e.g. extract functions, reduce duplication, improve naming, simplify logic, or remove dead code — pick the most impactful single concern).
- Run the existing test suite BEFORE making any changes to establish a baseline. If tests fail before you start, report them and do not proceed.
- Make your refactor changes.
- Run the test suite AGAIN after your changes. All tests that passed before must still pass. If any test regresses, revert or fix the regression before committing.
- Keep the diff minimal — only change what is necessary for the chosen refactor concern.

Task: ${task.title ? task.title : 'Refactor'}

${rawPrompt}`;

  // Clone and setup workspace
  const { workBranch, shortId } = await setupWorkspace(owner, repo, ghToken, baseBranch, 'refactor', taskId, ws, statusRef);

  // Run Claude
  const refactorPromptSuffix = payload.prompt_suffix ? `\n\nAdditional instructions:\n${payload.prompt_suffix}` : '';
  statusRef.value = 'Running Claude Code...';
  const { exitCode, allOutput, lastResultText } = await runClaudeCode(refactorPrompt + refactorPromptSuffix, ws, statusRef, model, effort);

  const diff = git(['status', '--porcelain']);
  const hasChanges = diff.length > 0;

  if (hasChanges && exitCode === 0) {
    statusRef.value = 'Pushing changes...';
    sendLog(ws, '\nCommitting and pushing changes...\n');
    const changedPaths = diff.split('\n').map(l => l.trim().split(/\s+/).pop() || '');
    await lintFixLoop(changedPaths, ws, statusRef, 'the refactored code', model, effort);

    const { testFailuresDetected } = await testFixLoop(ws, statusRef, model, effort);

    git(['add', '-A']);
    const commitNote = testFailuresDetected ? '\n\nNote: test failures detected, manual review recommended' : '';
    execFileSync('git', ['-C', WORKSPACE, 'commit', '-m', `refactor: ${taskTitle}\n\nAutomated by Blueprint Agent (task: ${shortId})${commitNote}`], { encoding: 'utf-8' });

    statusRef.value = `Rebasing onto ${baseBranch}...`;
    try {
      git(['fetch', 'origin', baseBranch]);
      git(['rebase', `origin/${baseBranch}`]);
      sendLog(ws, `Rebased onto latest ${baseBranch}\n`);
    } catch {
      sendLog(ws, `Rebase onto ${baseBranch} had conflicts — pushing as-is, review pipeline will handle\n`);
      try { git(['rebase', '--abort']); } catch {}
    }

    try {
      const markers = execFileSync('git', ['-C', WORKSPACE, 'grep', '-rl', '<' + '<<<<<<', '--', '*.ts', '*.tsx', '*.py', '*.js', '*.jsx', '*.json'], { encoding: 'utf-8', timeout: 10_000 }).trim();
      if (markers) {
        sendLog(ws, `WARNING: Conflict markers found in:\n${markers}\nAborting rebase and pushing original commit.\n`);
        try { git(['rebase', '--abort']); } catch {}
      }
    } catch { /* no markers found */ }

    try {
      runProjectLintChecks(changedPaths, ws);
    } catch (postRebaseLintErr: unknown) {
      const msg = postRebaseLintErr instanceof Error ? postRebaseLintErr.message : String(postRebaseLintErr);
      sendLog(ws, `Post-rebase lint failed:\n${msg.slice(0, 3000)}\nClaude will fix...\n`);
      await runClaudeCode(
        `After rebasing onto ${baseBranch}, lint/build errors appeared. Fix ALL of them — do not use \`any\` types.\n\n${msg.slice(0, 5000)}`,
        ws, statusRef, model, effort
      );
      git(['add', '-A']);
      execFileSync('git', ['-C', WORKSPACE, 'commit', '-m', 'refactor: fix post-rebase lint errors'], { encoding: 'utf-8' });
    }

    const prBody = lastResultText
      ? `## Summary\n\n${lastResultText.slice(0, 3000)}\n\n---\n*Automated by Blueprint Agent*`
      : `Refactor: ${taskTitle}\n\n---\n*Automated by Blueprint Agent*`;
    const prUrl = await pushAndCreatePR(owner, repo, taskId, workBranch, baseBranch, `refactor: ${taskTitle}`, prBody, ws, statusRef, true);
    const commitHash = git(['rev-parse', 'HEAD']);

    await apiPost('/api/agent/status', {
      task_id: taskId, status: 'COMPLETED', artifact_type: 'PULL_REQUEST',
      commit_hash: commitHash, pr_url: prUrl, repo_url: `https://github.com/${owner}/${repo}`,
    });
  } else if (exitCode === 0) {
    await apiPost('/api/agent/status', { task_id: taskId, status: 'COMPLETED', logs: 'No file changes.' });
  } else {
    await apiPost('/api/agent/status', {
      task_id: taskId, status: 'FAILED', artifact_type: 'ERROR_LOG', logs: sanitize(allOutput.slice(-10_000)),
    });
  }
}

// ── Handler: Debug (analyze task/CI/test failures and produce root cause analysis) ──
async function handleDebug(
  taskId: string, payload: Record<string, unknown>, task: Record<string, unknown>,
  ghToken: string, owner: string, repo: string, ws: WebSocket | null, statusRef: { value: string },
  model?: string, effort?: string
) {
  const errorLogs = String(payload.error_logs || payload.failure_context || '');
  const failedTaskId = String(payload.failed_task_id || '');
  const failedTaskTitle = String(payload.failed_task_title || task.title || '');
  const rawBaseBranch = String(payload.base_branch || 'main');
  validateBranchName(rawBaseBranch, 'base_branch');

  // Clone for read-only analysis (no work branch or push needed)
  statusRef.value = `Cloning ${owner}/${repo} for debug analysis...`;
  sendLog(ws, `Cloning ${owner}/${repo} @ ${rawBaseBranch} for debug analysis...\n`);
  execFileSync('rm', ['-rf', WORKSPACE], { encoding: 'utf-8' });
  const cloneUrl = `https://x-access-token:${ghToken}@github.com/${owner}/${repo}.git`;
  execFileSync('git', ['clone', '--depth', '100', '--branch', rawBaseBranch, cloneUrl, WORKSPACE], { encoding: 'utf-8', timeout: 300_000 });

  // Gather git context upfront to include in the prompt
  const gitLog = git(['log', '--oneline', '-20']);
  const gitDiffStat = (() => { try { return git(['diff', 'HEAD~5...HEAD', '--stat']); } catch { return '(not enough history)'; } })();

  const debugPrompt = `You are a debug analysis agent. Your job is to analyze a task failure and produce a structured root cause analysis. Do NOT make any code changes.

## Failed Task
Title: ${failedTaskTitle}
${failedTaskId ? `Task ID: ${failedTaskId}` : ''}

## Error Logs
\`\`\`
${errorLogs.slice(0, 8000)}
\`\`\`

## Recent Git History (last 20 commits)
\`\`\`
${gitLog}
\`\`\`

## Recent Changes (last 5 commits, file stats)
\`\`\`
${gitDiffStat}
\`\`\`

## Your Analysis Steps

1. Read the error logs carefully. Identify the exact error message, exception type, or test name that failed.

2. Trace the error to its source in the codebase:
   - Search for the file(s) and function(s) mentioned in the error or stack trace
   - Read those files to understand the context around the failure
   - Check if the error is in a test, CI step, import, or runtime code

3. Check the git history for when this issue was introduced:
   - Review the recent commits listed above — look for changes to the relevant files
   - If a recent commit looks responsible, read those changed files
   - Determine whether this is a regression (something that recently broke) or a pre-existing issue

4. Identify the specific root cause — name the file, function, and line where the problem originates.

5. Write your analysis to /tmp/debug_output.json with EXACTLY this structure:
{
  "root_cause": "One clear paragraph explaining what caused the failure, which file/function is responsible, and how it broke",
  "affected_files": ["path/to/file1.ts", "path/to/file2.py"],
  "fix_prompt": "Self-contained prompt for a create-type agent to fix this issue. Include: what the problem is, which files to modify, what the correct behavior should be, and what the fix should look like. Be specific enough that the agent can implement the fix without additional context.",
  "confidence": "high"
}

Set confidence to:
- "high" if you found the exact code causing the failure and have a clear fix
- "medium" if you have a strong hypothesis but some uncertainty
- "low" if the root cause is unclear or spans many systems

IMPORTANT: Always write /tmp/debug_output.json even if you cannot determine the root cause (use confidence "low" and explain what you found in root_cause). Never leave the file unwritten.`;

  statusRef.value = 'Analyzing failure...';
  const { allOutput } = await runClaudeCode(debugPrompt, ws, statusRef, model, effort);

  let debugResult: { root_cause: string; affected_files: string[]; fix_prompt: string; confidence: string } | null = null;
  try {
    const resultJson = fs.readFileSync('/tmp/debug_output.json', 'utf-8');
    debugResult = JSON.parse(resultJson);
  } catch {
    sendLog(ws, 'Warning: /tmp/debug_output.json not found or invalid\n');
  }

  if (debugResult) {
    sendLog(ws, `\nDebug analysis complete (confidence: ${debugResult.confidence})\n`);
    sendLog(ws, `Root cause: ${debugResult.root_cause.slice(0, 500)}\n`);
    await apiPost('/api/agent/status', {
      task_id: taskId,
      status: 'COMPLETED',
      artifact_type: 'DEBUG_RESULT',
      logs: JSON.stringify(debugResult),
      repo_url: `https://github.com/${owner}/${repo}`,
    });
  } else {
    await apiPost('/api/agent/status', {
      task_id: taskId, status: 'FAILED', artifact_type: 'ERROR_LOG',
      logs: sanitize(allOutput.slice(-10_000)),
    });
  }
}

// ── Handler: Perf (analyze codebase for performance bottlenecks, output findings) ──
async function handlePerf(
  taskId: string, payload: Record<string, unknown>, task: Record<string, unknown>,
  ghToken: string, owner: string, repo: string, ws: WebSocket | null, statusRef: { value: string },
  model?: string, effort?: string
) {
  const rawBaseBranch = String(payload.base_branch || 'main');
  const baseBranch = validateBranchName(rawBaseBranch, 'base_branch');

  statusRef.value = `Cloning ${owner}/${repo} for performance analysis...`;
  sendLog(ws, `Starting performance analysis...\n`);
  execFileSync('rm', ['-rf', WORKSPACE], { encoding: 'utf-8' });
  const cloneUrl = `https://x-access-token:${ghToken}@github.com/${owner}/${repo}.git`;
  execFileSync('git', ['clone', '--branch', baseBranch, cloneUrl, WORKSPACE], { encoding: 'utf-8', timeout: 300_000 });

  const perfPrompt = `You are a performance analysis agent. You have full access to this repository. Your job is to analyze the codebase for demonstrable performance bottlenecks and produce structured, evidence-based findings.

Analyze the codebase for the following performance anti-patterns. Only flag issues you can directly observe in the code — do NOT speculate about theoretical problems.

1. **N+1 database queries**: ORM relationships loaded inside loops (e.g. accessing \`obj.related\` inside a for loop without eager loading). Check backend/app/main.py for SQLAlchemy patterns.
2. **Missing database indexes**: Columns used in WHERE clauses, JOIN conditions, or ORDER BY that lack an index. Check backend/app/models.py for table definitions and compare against query patterns in main.py.
3. **Synchronous blocking operations**: Network calls, file I/O, or CPU-heavy work done synchronously inside async request handlers.
4. **Unpaginated large payload returns**: API endpoints that return full table scans or unbounded lists without LIMIT/offset or cursor pagination.
5. **Frontend large bundle imports**: Direct imports of entire libraries where only a small subset is used (e.g. \`import _ from 'lodash'\` instead of \`import debounce from 'lodash/debounce'\`). Check admin-ui/src.
6. **Unnecessary React re-renders**: Components with expensive computations or callbacks not wrapped in useMemo/useCallback, causing re-renders on every parent update. Check admin-ui/src for patterns.

Instructions:
1. Read key files: backend/app/models.py (table/index definitions), backend/app/main.py (query patterns, endpoint handlers), and admin-ui/src (React components, imports).
2. For each finding, identify the specific file, function, and line range where the problem is observable.
3. Only report findings that are demonstrably present — cite the exact code pattern that shows the problem.
4. Produce your findings as a JSON array written to /tmp/perf_output.json:

[
  {
    "area": "database",
    "severity": "high",
    "title": "Short description (under 60 chars)",
    "description": "What the problem is, why it matters, and the exact code pattern observed.",
    "fix_prompt": "Self-contained prompt for a create agent to fix this issue. Must describe exactly what to change and where.",
    "related_files": ["backend/app/main.py"]
  }
]

Valid values:
- area: database, api, frontend, agent
- severity: high (measurable user-facing impact), medium (latency/memory concern under load), low (minor inefficiency)

Rules:
- Evidence-based only: every finding must cite the specific file and code pattern that demonstrates the problem
- Do NOT flag theoretical issues (e.g. "this could be slow if data grows") — only flag patterns that are demonstrably inefficient now
- fix_prompt must be fully self-contained — the fixing agent has no context beyond that string
- Limit to 15 findings maximum, prioritized by severity
- IMPORTANT: Write your findings JSON array to /tmp/perf_output.json (NOT in the repository). The file should contain ONLY the JSON array, no markdown wrapping.
- Also output the same JSON array as the final output in your logs.`;

  statusRef.value = 'Running performance analysis...';
  const { exitCode, allOutput, lastResultText } = await runClaudeCode(perfPrompt, ws, statusRef, model, effort);

  let logs = '';
  const findingsPath = '/tmp/perf_output.json';
  try {
    if (fs.existsSync(findingsPath)) {
      logs = sanitize(fs.readFileSync(findingsPath, 'utf-8'));
      sendLog(ws, 'Read perf findings from /tmp/perf_output.json\n');
    }
  } catch (e) {
    sendLog(ws, `Failed to read ${findingsPath}, falling back to output parsing\n`);
  }
  if (!logs) {
    if (lastResultText && lastResultText.includes('"severity"')) {
      logs = sanitize(lastResultText);
    } else {
      const jsonMatch = allOutput.match(/\[\s*\{[\s\S]*"severity"[\s\S]*\}\s*\]/);
      logs = sanitize(jsonMatch ? jsonMatch[0] : (lastResultText || allOutput.slice(-50_000)));
    }
  }

  await apiPost('/api/agent/status', {
    task_id: taskId,
    status: exitCode === 0 ? 'COMPLETED' : 'FAILED',
    ...(exitCode !== 0 ? { artifact_type: 'ERROR_LOG' } : { artifact_type: 'PERF_REPORT' }),
    logs,
  });
}

// ── Handler: Investigate (analyze issue, output structured diagnosis report) ──
async function handleInvestigate(
  taskId: string, payload: Record<string, unknown>, task: Record<string, unknown>,
  ghToken: string, owner: string, repo: string, ws: WebSocket | null, statusRef: { value: string },
  model?: string, effort?: string
) {
  const rawBaseBranch = String(payload.base_branch || 'main');
  const baseBranch = validateBranchName(rawBaseBranch, 'base_branch');
  const userPrompt = String(payload.prompt || payload.description || task.description || '');

  statusRef.value = `Cloning ${owner}/${repo} for investigation...`;
  sendLog(ws, `Starting investigation...\n`);
  execFileSync('rm', ['-rf', WORKSPACE], { encoding: 'utf-8' });
  const cloneUrl = `https://x-access-token:${ghToken}@github.com/${owner}/${repo}.git`;
  execFileSync('git', ['clone', '--branch', baseBranch, cloneUrl, WORKSPACE], { encoding: 'utf-8', timeout: 300_000 });

  const investigatePrompt = `You are an investigation agent. You have full read access to this repository. Your job is to analyze the issue described below and produce a structured diagnosis report.

Issue to investigate:
${userPrompt}

Instructions:
1. Read relevant source files to understand the codebase and locate the root cause of the issue.
2. DO NOT make any changes to any files — this is a read-only analysis.
3. Identify the root cause, affected files, and a recommended fix.
4. Write your diagnosis as a JSON object to /tmp/investigation_report.json with EXACTLY this schema:

{
  "summary": "Brief summary of the issue and findings",
  "root_cause": "Detailed explanation of the root cause",
  "affected_files": [
    { "path": "path/to/file.ts", "issue": "Description of the problem in this file" }
  ],
  "recommended_fix": "Concrete description of what needs to be changed to fix the issue",
  "severity": "critical|high|medium|low",
  "confidence": "high|medium|low"
}

IMPORTANT:
- Write ONLY the JSON object to /tmp/investigation_report.json — no markdown, no code fences, no extra text.
- Do NOT modify any files in the repository.
- Base every claim on code you have actually read.`;

  statusRef.value = 'Running investigation...';
  const { exitCode, allOutput, lastResultText } = await runClaudeCode(investigatePrompt, ws, statusRef, model, effort);


  const reportPath = '/tmp/investigation_report.json';

  let logs = '';
  try {
    if (!fs.existsSync(reportPath)) {
      throw new Error(`Investigation report not found at ${reportPath}`);
    }
    const raw = fs.readFileSync(reportPath, 'utf-8');
    // Validate it parses as JSON
    JSON.parse(raw);
    logs = sanitize(raw);
    sendLog(ws, 'Read investigation report from /tmp/investigation_report.json\n');
  } catch (e) {
    const errMsg = `Failed to read investigation report: ${e}`;
    sendLog(ws, `${errMsg}\n`);
    await apiPost('/api/agent/status', {
      task_id: taskId,
      status: 'FAILED',
      artifact_type: 'ERROR_LOG',
      logs: errMsg,
    });
    return;
  }

  await apiPost('/api/agent/status', {
    task_id: taskId,
    status: exitCode === 0 ? 'COMPLETED' : 'FAILED',
    ...(exitCode !== 0 ? { artifact_type: 'ERROR_LOG' } : { artifact_type: 'INVESTIGATION_REPORT' }),
    logs,
  });
}

// ── Handler Registry ────────────────────────────────────────────────────────
const handlers: Record<string, Handler> = {
  create: handleCreate,
  refactor: handleRefactor,
  review: handleReview,
  fix: handleFix,
  plan: handlePlan,
  research: handleResearch,
  security_audit: handleSecurityAudit,
  qa: handleQA,
  deps: handleDeps,
  docs: handleDocs,
  migrate: handleMigrate,
  smoketest: handleSmoketest,
  debug: handleDebug,
  perf: handlePerf,
  investigate: handleInvestigate,
};

// ── Main ────────────────────────────────────────────────────────────────────
async function main() {
  log(`Blueprint Agent starting (VM: ${VM_INSTANCE_ID})`);
  if (!AGENT_TOKEN) throw new Error('AGENT_SECRET_TOKEN is required');

  // Auth: CLAUDE_CODE_OAUTH_TOKEN env var is read directly by Claude Code CLI (no file needed)
  if (process.env.CLAUDE_CODE_OAUTH_TOKEN) {
    log('Using OAuth token for Claude Code auth');
  } else if (process.env.ANTHROPIC_API_KEY) {
    log('Using ANTHROPIC_API_KEY as fallback auth');
  }

  // Claim task
  const task = await apiPost('/api/agent/claim');
  const taskId = String(task.task_id);
  const payloads = task.payloads || [];
  const payload = payloads[0] || {};
  const taskType = String(payload.type || 'create');
  const model = payload.model ? String(payload.model) : undefined;
  const effort = payload.effort ? String(payload.effort) : undefined;
  log(`Claimed task ${taskId} (type: ${taskType}): "${task.title}"`);

  // Start heartbeat & log stream
  const statusRef = { value: 'Initializing...' };
  startHeartbeat(taskId, () => statusRef.value);
  const ws = await connectLogStream(taskId);
  sendLog(ws, `Agent started (VM: ${VM_INSTANCE_ID}, type: ${taskType})\n`);

  try {
    // Get GitHub credentials
    statusRef.value = 'Fetching GitHub credentials...';
    const ghRes = await apiGet(`/api/agent/github/token?task_id=${taskId}`);
    const { token: ghToken, owner, repo } = ghRes.data;
    log(`GitHub: ${owner}/${repo}`);

    // Dispatch to handler (fall back to create for unknown types)
    const handler = handlers[taskType] || handlers.create;
    if (!handlers[taskType]) sendLog(ws, `Warning: unknown task type "${taskType}", using create handler\n`);
    await handler(taskId, payload, task, ghToken, owner, repo, ws, statusRef, model, effort);
    sendLog(ws, '\nTask completed.\n');

  } catch (error) {
    const errMsg = error instanceof Error ? error.message : String(error);
    const safeErr = sanitize(errMsg);
    log(`Fatal error: ${safeErr}`);
    sendLog(ws, `\nFatal error: ${safeErr}\n`);
    try {
      await apiPost('/api/agent/status', {
        task_id: taskId, status: 'FAILED', artifact_type: 'ERROR_LOG',
        logs: sanitize(`Fatal error: ${errMsg}`),
      });
    } catch (e) { log(`Failed to report error: ${e}`); }
  } finally {
    stopHeartbeat();
    if (ws) ws.close();
    // Clean up workspace to avoid leaking tokens in git config
    try { execFileSync('rm', ['-rf', WORKSPACE], { encoding: 'utf-8', timeout: 30_000 }); } catch {}
    // Clean up /tmp files written by Claude Code handlers
    for (const f of ['/tmp/review_verdict.json', '/tmp/smoketest_result.json', '/tmp/debug_output.json', '/tmp/investigation_report.json', '/tmp/perf_output.json']) {
      try { fs.unlinkSync(f); } catch {}
    }
    log('Agent shutdown complete.');
  }
}

// Global crash handlers — ensure fatal errors are logged before the process dies
// (without these, unhandled rejections/exceptions silently kill the agent and
// the reaper eventually times out with zero forensic evidence)
process.on('unhandledRejection', (reason) => {
  console.error(`[FATAL] Unhandled rejection: ${reason}`);
  process.exit(1);
});
process.on('uncaughtException', (err) => {
  console.error(`[FATAL] Uncaught exception: ${err instanceof Error ? `${err.message}\n${err.stack}` : err}`);
  process.exit(1);
});
process.on('SIGTERM', () => {
  console.error('[FATAL] Received SIGTERM — VM shutting down');
  process.exit(143);
});

main().catch((err) => { console.error('Agent failed to start:', err); process.exit(1); });
