// Bake-time lifecycle smoke: validates the full agent claim → dispatch →
// complete chain for a memo task using a local stub HTTP server.
//
// Unlike the per-handler smokes (memo-bake-smoke*.ts) that call
// handlers.memo() directly, this smoke invokes the main agent entry point
// (src/index.ts) as a subprocess, exercising:
//   1. AGENT_SECRET_TOKEN check (bypassed via BLUEPRINT_SMOKE_SKIP_VALIDATION=1)
//   2. POST /api/agent/claim → dispatcher reads task_type from payload
//   3. handlers['memo'] lookup → routes to handlers.memo
//   4. handleMemo synthesizes from source_finding (dispatcher path, no Claude)
//   5. POST /api/agent/status with status=COMPLETED and sentinel in logs
//
// A rename of the 'memo' handler key, missing handler registration, or broken
// dispatch path will cause the subprocess to exit non-zero, failing the bake.
//
// Run from /opt/agent on the bake VM:
//   sudo -u agent -H bash -lc 'cd /opt/agent && node --import tsx ./src/handlers/memo-bake-smoke-lifecycle.ts'

import { createServer } from 'http';
import { spawn } from 'child_process';
import { mkdtempSync, rmSync } from 'fs';
import { tmpdir } from 'os';
import { fileURLToPath } from 'url';
import path from 'path';

import { buildBakeSmokeCapabilityContract } from './bake-smoke-capability-contract.js';

const LIFECYCLE_SENTINEL = 'memo-lifecycle-bake-smoke-2026';

// Dispatcher-path fixture: direction_retirement with a rationale causes
// handleMemo to synthesize the memo directly without invoking Claude.
const MEMO_TASK: Record<string, unknown> = {
  task_id: 'lifecycle-smoke-task-001',
  title: 'Lifecycle smoke memo',
  description: '',
  context: {},
  payloads: [{
    type: 'memo',
    reasoning_kind: 'direction_retirement',
    source_task_id: 'lifecycle-source',
    direction_id: 'lifecycle-direction',
    source_finding: {
      rationale: LIFECYCLE_SENTINEL,
      direction_text: 'lifecycle bake smoke chain check',
    },
  }],
};

let completedRecorded = false;

const server = createServer((req, res) => {
  const url = req.url ?? '';
  let body = '';
  req.on('data', (chunk: Buffer) => { body += chunk.toString(); });
  req.on('end', () => {
    if (req.method === 'POST' && url.includes('/api/agent/heartbeat')) {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ ok: true }));
    } else if (req.method === 'POST' && url.includes('/api/agent/claim')) {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify(MEMO_TASK));
    } else if (req.method === 'GET' && url.includes('/api/agent/github/token')) {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ data: { token: 'fake-gh-token', owner: 'smoke-owner', repo: 'smoke-repo' } }));
    } else if (req.method === 'GET' && url.includes('/api/agent/task-capabilities')) {
      // The real agent fetches + loads the capability contract at startup,
      // before any handler dispatch (ensureTaskCapabilityContractLoaded in
      // src/index.ts) — so this smoke exercises the production load path
      // end-to-end against a complete stand-in contract.
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ task_capabilities: buildBakeSmokeCapabilityContract() }));
    } else if (req.method === 'POST' && url.includes('/api/agent/status')) {
      let parsed: { status?: string; logs?: string } = {};
      try { parsed = JSON.parse(body || '{}') as typeof parsed; } catch { /* ignore */ }
      const status = parsed.status ?? '';
      const logs = parsed.logs ?? '';
      if (status !== 'COMPLETED' || !logs.includes(LIFECYCLE_SENTINEL)) {
        res.writeHead(500, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({
          error: `lifecycle smoke: unexpected status post — status=${status} sentinel_found=${logs.includes(LIFECYCLE_SENTINEL)}`,
        }));
      } else {
        completedRecorded = true;
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ ok: true }));
      }
    } else {
      // Drain body to avoid socket leak; 404 for any unexpected endpoint.
      res.writeHead(404, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: `lifecycle smoke: unexpected ${req.method} ${url}` }));
    }
  });
});

await new Promise<void>(resolve => server.listen(0, '127.0.0.1', () => { resolve(); }));
const addr = server.address() as { port: number };
const orchestratorUrl = `http://127.0.0.1:${addr.port}`;

const agentEntry = fileURLToPath(new URL('../index.ts', import.meta.url));

// Build subprocess env: inherit current env but override orchestrator config.
// Omit VM_INSTANCE_ID so the role loop exits after one task (no warm-poll).
const childEnv: Record<string, string> = {};
for (const [k, v] of Object.entries(process.env)) {
  if (k !== 'VM_INSTANCE_ID' && v !== undefined) childEnv[k] = v;
}
childEnv.ORCHESTRATOR_URL = orchestratorUrl;
childEnv.AGENT_SECRET_TOKEN = 'lifecycle-smoke-token';
childEnv.TASK_ID = MEMO_TASK.task_id as string;
childEnv.BLUEPRINT_SMOKE_SKIP_VALIDATION = '1';

// Redirect token files to an isolated sandbox so the smoke child cannot
// overwrite the host session's live JWT or GitHub installation token.
// Same isolation rationale as omitting VM_INSTANCE_ID above.
const smokeSandboxDir = mkdtempSync(path.join(tmpdir(), 'memo-lifecycle-smoke-'));
childEnv['AGENT_TOKEN_FILE'] = path.join(smokeSandboxDir, 'agent-token.jwt');
childEnv['GITHUB_TOKEN_FILE'] = path.join(smokeSandboxDir, 'github-token.txt');

const proc = spawn('node', ['--import', 'tsx', agentEntry], {
  env: childEnv,
  stdio: ['ignore', 'pipe', 'pipe'],
});

let stdout = '';
let stderr = '';
proc.stdout.on('data', (d: Buffer) => { stdout += d.toString(); });
proc.stderr.on('data', (d: Buffer) => { stderr += d.toString(); });

const exitCode = await new Promise<number>((resolve) => {
  const safetyTimer = setTimeout(() => {
    try { proc.kill(); } catch { /* ignore */ }
    resolve(1);
  }, 90_000);
  proc.on('close', (code) => {
    clearTimeout(safetyTimer);
    resolve(code ?? 1);
  });
});

server.close();
try { rmSync(smokeSandboxDir, { recursive: true, force: true }); } catch { /* ignore */ }

if (exitCode !== 0) {
  throw new Error(
    `lifecycle smoke: agent subprocess exited ${exitCode}\n` +
    `stdout:\n${stdout.slice(-2000)}\nstderr:\n${stderr.slice(-2000)}`,
  );
}

// eslint-disable-next-line @typescript-eslint/no-unnecessary-condition -- completedRecorded is mutated inside the server callback closure; TS narrows it to false here.
if (!completedRecorded) {
  throw new Error(
    `lifecycle smoke: agent exited 0 but COMPLETED status was never posted\n` +
    `stdout:\n${stdout.slice(-2000)}\nstderr:\n${stderr.slice(-2000)}`,
  );
}

console.log('PACKER_MEMO_LIFECYCLE_OK');
