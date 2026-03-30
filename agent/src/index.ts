import { spawn, execSync, execFileSync } from 'child_process';
import WebSocket from 'ws';

// ── Environment ─────────────────────────────────────────────────────────────
const ORCHESTRATOR_URL = process.env.ORCHESTRATOR_URL || 'http://localhost:8080';
const AGENT_TOKEN = process.env.AGENT_SECRET_TOKEN || '';
const VM_INSTANCE_ID = process.env.VM_INSTANCE_ID || 'local';
const WORKSPACE = '/tmp/workspace';

const headers: Record<string, string> = {
  'Content-Type': 'application/json',
  'X-Agent-Token': AGENT_TOKEN,
};

// ── API Helpers ─────────────────────────────────────────────────────────────
async function apiPost(path: string, body: Record<string, unknown> = {}) {
  const res = await fetch(`${ORCHESTRATOR_URL}${path}`, {
    method: 'POST',
    headers,
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`POST ${path} failed (${res.status}): ${text}`);
  }
  return res.json();
}

async function apiGet(path: string) {
  const res = await fetch(`${ORCHESTRATOR_URL}${path}`, { headers });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`GET ${path} failed (${res.status}): ${text}`);
  }
  return res.json();
}

function log(msg: string) {
  const ts = new Date().toISOString();
  console.log(`[${ts}] ${msg}`);
}

// ── Heartbeat ───────────────────────────────────────────────────────────────
let heartbeatTimer: ReturnType<typeof setInterval> | null = null;

function startHeartbeat(taskId: string, getMessage: () => string) {
  heartbeatTimer = setInterval(async () => {
    try {
      await apiPost('/api/agent/heartbeat', {
        task_id: taskId,
        status_message: getMessage(),
      });
    } catch (e) {
      log(`Heartbeat failed: ${e}`);
    }
  }, 30_000);
}

function stopHeartbeat() {
  if (heartbeatTimer) {
    clearInterval(heartbeatTimer);
    heartbeatTimer = null;
  }
}

// ── WebSocket Log Stream ────────────────────────────────────────────────────
function connectLogStream(taskId: string): Promise<WebSocket | null> {
  return new Promise((resolve) => {
    try {
      const wsUrl = ORCHESTRATOR_URL.replace(/^http/, 'ws');
      const ws = new WebSocket(`${wsUrl}/api/agent/ws/${taskId}`);

      const timeout = setTimeout(() => {
        log('WebSocket connection timeout, continuing without log streaming');
        resolve(null);
      }, 5000);

      ws.on('open', () => {
        clearTimeout(timeout);
        ws.send(JSON.stringify({ token: AGENT_TOKEN }));
        log('WebSocket log stream connected');
        resolve(ws);
      });

      ws.on('error', (err) => {
        clearTimeout(timeout);
        log(`WebSocket error: ${err.message}, continuing without log streaming`);
        resolve(null);
      });
    } catch {
      log('Failed to create WebSocket, continuing without log streaming');
      resolve(null);
    }
  });
}

// Strip credentials from any output before logging or transmitting
function sanitize(text: string): string {
  return text
    .replace(/https:\/\/x-access-token:[^@]+@/g, 'https://x-access-token:***@')
    .replace(/ghp_[A-Za-z0-9_]+/g, 'ghp_***')
    .replace(/ghs_[A-Za-z0-9_]+/g, 'ghs_***')
    .replace(/github_pat_[A-Za-z0-9_]+/g, 'github_pat_***');
}

function sendLog(ws: WebSocket | null, content: string) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    try {
      ws.send(JSON.stringify({ type: 'log', content: sanitize(content) }));
    } catch {
      // Ignore send failures
    }
  }
}

// ── Git Helpers ─────────────────────────────────────────────────────────────
function git(args: string[], cwd = WORKSPACE): string {
  return execFileSync('git', args, { cwd, encoding: 'utf-8', timeout: 120_000 }).trim();
}

// ── Main ────────────────────────────────────────────────────────────────────
async function main() {
  log(`Blueprint Agent starting (VM: ${VM_INSTANCE_ID})`);

  if (!AGENT_TOKEN) {
    throw new Error('AGENT_SECRET_TOKEN is required');
  }

  // Install Max plan credentials if provided by orchestrator (local mode via temp file)
  if (process.env.CLAUDE_CREDENTIALS_FILE) {
    const fs = await import('fs');
    const path = await import('path');
    const claudeDir = path.join(process.env.HOME || '/home/agent', '.claude');
    fs.mkdirSync(claudeDir, { recursive: true });
    fs.copyFileSync(process.env.CLAUDE_CREDENTIALS_FILE, path.join(claudeDir, '.credentials.json'));
    fs.chmodSync(path.join(claudeDir, '.credentials.json'), 0o600);
    // Scrub the temp file — credentials now only exist at ~/.claude/
    fs.unlinkSync(process.env.CLAUDE_CREDENTIALS_FILE);
    delete process.env.CLAUDE_CREDENTIALS_FILE;
    log('Max plan credentials installed (temp file scrubbed)');
  } else if (process.env.ANTHROPIC_API_KEY) {
    log('Using ANTHROPIC_API_KEY as fallback auth');
  }

  // 1. Claim task
  log('Claiming task from orchestrator...');
  const task = await apiPost('/api/agent/claim');
  const taskId = String(task.task_id);
  const payloads = task.payloads || [];
  const payload = payloads[0] || {};
  const prompt = payload.prompt || payload.description || task.description || '';
  const rawBaseBranch = String(payload.base_branch || 'main');
  const rawTaskTitle = String(task.title || payload.title || 'automated changes');

  // Validate inputs to prevent injection (defense in depth — server should also validate)
  const SAFE_BRANCH = /^[a-zA-Z0-9_./-]+$/;
  if (!SAFE_BRANCH.test(rawBaseBranch)) {
    throw new Error(`Invalid base_branch: ${rawBaseBranch}`);
  }
  const baseBranch = rawBaseBranch;
  // Sanitize task title for use in commit messages (strip shell metacharacters)
  const taskTitle = rawTaskTitle.replace(/[`$\\!"';&|<>(){}[\]]/g, '');
  log(`Claimed task ${taskId}: "${taskTitle}"`);

  if (!prompt) {
    throw new Error('No prompt found in task payloads');
  }

  // 2. Start heartbeat
  let statusMessage = 'Initializing...';
  startHeartbeat(taskId, () => statusMessage);

  // 3. Connect log stream
  const ws = await connectLogStream(taskId);
  sendLog(ws, `Agent started on VM ${VM_INSTANCE_ID}\nTask: ${taskTitle}\n`);

  let allOutput = '';
  let lastResultText = '';

  try {
    // 4. Get GitHub credentials
    statusMessage = 'Fetching GitHub credentials...';
    sendLog(ws, 'Fetching GitHub credentials...\n');
    const ghRes = await apiGet(`/api/agent/github/token?task_id=${taskId}`);
    const { token: ghToken, owner, repo } = ghRes.data;
    log(`GitHub: ${owner}/${repo}`);

    // 5. Clone repository (shallow for speed)
    statusMessage = `Cloning ${owner}/${repo}...`;
    sendLog(ws, `Cloning ${owner}/${repo}...\n`);
    execFileSync('rm', ['-rf', WORKSPACE], { encoding: 'utf-8' });
    const cloneUrl = `https://x-access-token:${ghToken}@github.com/${owner}/${repo}.git`;
    execFileSync('git', ['clone', '--depth', '50', '--branch', baseBranch, cloneUrl, WORKSPACE], {
      encoding: 'utf-8',
      timeout: 300_000,
    });

    // Configure git identity for commits
    git(['config', 'user.email', 'blueprint-agent@automated.dev']);
    git(['config', 'user.name', 'Blueprint Agent']);

    // 6. Create working branch
    const shortId = taskId.replace(/-/g, '').slice(0, 8);
    const workBranch = `blueprint/task-${shortId}`;
    git(['checkout', '-b', workBranch]);
    log(`Created branch: ${workBranch}`);
    sendLog(ws, `Working branch: ${workBranch}\n`);

    // 7. Run Claude Code
    statusMessage = 'Running Claude Code...';
    sendLog(ws, `\n--- Claude Code starting ---\nPrompt: ${prompt.slice(0, 200)}${prompt.length > 200 ? '...' : ''}\n\n`);
    log('Spawning Claude Code CLI...');

    const exitCode = await new Promise<number>((resolve) => {
      const claudeEnv: Record<string, string> = {
        ...process.env as Record<string, string>,
        HOME: process.env.HOME || '/home/agent',
      };
      // Only pass API key if no Max plan credentials were installed (API key is fallback)
      if (process.env.ANTHROPIC_API_KEY) {
        claudeEnv.ANTHROPIC_API_KEY = process.env.ANTHROPIC_API_KEY;
      }

      const claude = spawn('claude', [
        '-p', prompt,
        '--output-format', 'stream-json',
        '--verbose',
        '--dangerously-skip-permissions',
      ], {
        cwd: WORKSPACE,
        env: claudeEnv,
      });

      claude.stdout.on('data', (data: Buffer) => {
        const text = data.toString();
        allOutput += text;

        // Parse stream-json lines for display
        for (const line of text.split('\n').filter(Boolean)) {
          try {
            const obj = JSON.parse(line);
            if (obj.type === 'assistant' && obj.message?.content) {
              for (const block of obj.message.content) {
                if (block.type === 'text') {
                  sendLog(ws, block.text + '\n');
                  statusMessage = block.text.slice(0, 100);
                } else if (block.type === 'tool_use') {
                  sendLog(ws, `[tool: ${block.name}] ${JSON.stringify(block.input).slice(0, 200)}\n`);
                  statusMessage = `Using tool: ${block.name}`;
                }
              }
            } else if (obj.type === 'result') {
              lastResultText = obj.result || '';
              sendLog(ws, `\n--- Result ---\n${lastResultText}\n`);
            }
          } catch {
            // Non-JSON line, forward raw
            sendLog(ws, text);
          }
        }
      });

      claude.stderr.on('data', (data: Buffer) => {
        const text = sanitize(data.toString());
        allOutput += text;
        sendLog(ws, `[stderr] ${text}`);
      });

      claude.on('error', (err) => {
        log(`Claude Code process error: ${err.message}`);
        sendLog(ws, `[error] ${err.message}\n`);
        resolve(1);
      });

      claude.on('close', (code) => {
        log(`Claude Code exited with code ${code}`);
        resolve(code ?? 1);
      });
    });

    sendLog(ws, `\n--- Claude Code exited (code: ${exitCode}) ---\n`);

    // 8. Check for changes
    const diff = git(['status', '--porcelain']);
    const hasChanges = diff.length > 0;
    log(`Changes detected: ${hasChanges} (${diff.split('\n').filter(Boolean).length} files)`);

    if (hasChanges && exitCode === 0) {
      // 9. Commit and push
      statusMessage = 'Pushing changes...';
      sendLog(ws, '\nCommitting and pushing changes...\n');

      git(['add', '-A']);
      const commitMsg = `blueprint: ${taskTitle}\n\nAutomated by Blueprint Agent (task: ${shortId})`;
      execFileSync('git', ['-C', WORKSPACE, 'commit', '-m', commitMsg], { encoding: 'utf-8' });
      const commitHash = git(['rev-parse', 'HEAD']);

      // Fetch fresh GitHub token for push (original may have expired)
      log('Fetching fresh GitHub token for push...');
      const freshGhRes = await apiGet(`/api/agent/github/token?task_id=${taskId}`);
      const freshToken = freshGhRes.data.token;
      const pushUrl = `https://x-access-token:${freshToken}@github.com/${owner}/${repo}.git`;
      execFileSync('git', ['-C', WORKSPACE, 'remote', 'set-url', 'origin', pushUrl], { encoding: 'utf-8' });
      git(['push', 'origin', workBranch]);
      log(`Pushed to ${workBranch}`);
      sendLog(ws, `Pushed to branch: ${workBranch}\n`);

      // 10. Create PR
      statusMessage = 'Creating pull request...';
      sendLog(ws, 'Creating pull request...\n');
      let prUrl = '';
      try {
        const prBody = lastResultText
          ? `## Summary\n\n${lastResultText.slice(0, 3000)}\n\n---\n*Automated by Blueprint Agent*`
          : `Automated changes for: ${taskTitle}\n\n---\n*Automated by Blueprint Agent*`;

        const prRes = await apiPost('/api/agent/github/pull-requests', {
          task_id: taskId,
          owner,
          repo,
          title: `[Blueprint] ${taskTitle}`,
          head: workBranch,
          base: baseBranch,
          body: prBody,
        });
        prUrl = prRes.data?.html_url || '';
        log(`PR created: ${prUrl}`);
        sendLog(ws, `PR created: ${prUrl}\n`);
      } catch (e) {
        log(`Failed to create PR: ${e}`);
        sendLog(ws, `Failed to create PR: ${e}\n`);
      }

      // 11. Report success
      await apiPost('/api/agent/status', {
        task_id: taskId,
        status: 'COMPLETED',
        artifact_type: 'PULL_REQUEST',
        commit_hash: commitHash,
        pr_url: prUrl,
        repo_url: `https://github.com/${owner}/${repo}`,
      });
      sendLog(ws, '\nTask completed successfully.\n');

    } else if (exitCode === 0) {
      // Claude succeeded but made no changes
      sendLog(ws, '\nClaude Code completed with no file changes.\n');
      await apiPost('/api/agent/status', {
        task_id: taskId,
        status: 'COMPLETED',
        logs: 'Claude Code completed successfully but made no file changes.',
      });

    } else {
      // Claude failed
      sendLog(ws, '\nClaude Code failed.\n');
      await apiPost('/api/agent/status', {
        task_id: taskId,
        status: 'FAILED',
        artifact_type: 'ERROR_LOG',
        logs: sanitize(allOutput.slice(-10_000)),
      });
    }

  } catch (error) {
    const errMsg = error instanceof Error ? error.message : String(error);
    const safeErr = sanitize(errMsg);
    log(`Fatal error: ${safeErr}`);
    sendLog(ws, `\nFatal error: ${safeErr}\n`);

    try {
      await apiPost('/api/agent/status', {
        task_id: taskId,
        status: 'FAILED',
        artifact_type: 'ERROR_LOG',
        logs: sanitize(`Fatal error: ${errMsg}\n\n${allOutput.slice(-5_000)}`),
      });
    } catch (e) {
      log(`Failed to report error: ${e}`);
    }
  } finally {
    stopHeartbeat();
    if (ws) ws.close();
    log('Agent shutdown complete.');
  }
}

main().catch((err) => {
  console.error('Agent failed to start:', err);
  process.exit(1);
});
