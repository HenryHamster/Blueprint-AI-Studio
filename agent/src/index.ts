import { spawn, execFileSync, type ChildProcess } from 'child_process';
import WebSocket from 'ws';

// ── Environment ─────────────────────────────────────────────────────────────
const ORCHESTRATOR_URL = process.env.ORCHESTRATOR_URL || 'http://localhost:8080';
const AGENT_TOKEN = process.env.AGENT_SECRET_TOKEN || '';
const VM_INSTANCE_ID = process.env.VM_INSTANCE_ID || 'local';
const WORKSPACE = '/tmp/workspace';

const apiHeaders: Record<string, string> = {
  'Content-Type': 'application/json',
  'X-Agent-Token': AGENT_TOKEN,
};

// ── API Helpers ─────────────────────────────────────────────────────────────
async function apiPost(path: string, body: Record<string, unknown> = {}, retries = 3) {
  for (let attempt = 0; attempt < retries; attempt++) {
    try {
      const res = await fetch(`${ORCHESTRATOR_URL}${path}`, {
        method: 'POST', headers: apiHeaders, body: JSON.stringify(body),
      });
      if (res.ok) return res.json();
      if (res.status < 500 || attempt === retries - 1) {
        const text = await res.text();
        throw new Error(`POST ${path} failed (${res.status}): ${text}`);
      }
    } catch (e) {
      if (attempt === retries - 1) throw e;
    }
    await new Promise(r => setTimeout(r, 2000 * (attempt + 1)));
  }
  throw new Error(`POST ${path} failed after ${retries} retries`);
}

async function apiGet(path: string, retries = 3) {
  for (let attempt = 0; attempt < retries; attempt++) {
    try {
      const res = await fetch(`${ORCHESTRATOR_URL}${path}`, { headers: apiHeaders });
      if (res.ok) return res.json();
      if (res.status < 500 || attempt === retries - 1) {
        const text = await res.text();
        throw new Error(`GET ${path} failed (${res.status}): ${text}`);
      }
    } catch (e) {
      if (attempt === retries - 1) throw e;
    }
    await new Promise(r => setTimeout(r, 2000 * (attempt + 1)));
  }
  throw new Error(`GET ${path} failed after ${retries} retries`);
}

function log(msg: string) {
  console.log(`[${new Date().toISOString()}] ${msg}`);
}

// ── Heartbeat ───────────────────────────────────────────────────────────────
let heartbeatTimer: ReturnType<typeof setInterval> | null = null;

function startHeartbeat(taskId: string, getMessage: () => string) {
  heartbeatTimer = setInterval(async () => {
    try {
      await apiPost('/api/agent/heartbeat', { task_id: taskId, status_message: getMessage() });
    } catch (e) { log(`Heartbeat failed: ${e}`); }
  }, 30_000);
}

function stopHeartbeat() {
  if (heartbeatTimer) { clearInterval(heartbeatTimer); heartbeatTimer = null; }
}

// ── WebSocket Log Stream ────────────────────────────────────────────────────
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
      if (ws) return ws;
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

function sendLog(ws: WebSocket | null, content: string) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    try { ws.send(JSON.stringify({ type: 'log', content: sanitize(content) })); } catch {}
  }
}

// ── Git Helpers ─────────────────────────────────────────────────────────────
function git(args: string[], cwd = WORKSPACE): string {
  return execFileSync('git', args, { cwd, encoding: 'utf-8', timeout: 120_000 }).trim();
}

// ── Lint / Build Checks ─────────────────────────────────────────────────────
const MAX_LINT_FIX_ATTEMPTS = 3;

/** Run lint/build checks on changed files. Throws with error details on failure. */
function runProjectLintChecks(changedPaths: string[], ws: WebSocket | null) {
  const touchesFrontend = changedPaths.some(p => p.startsWith('admin-ui/'));
  const touchesAgent = changedPaths.some(p => p.startsWith('agent/'));
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

    claude.stdout.on('data', (data: Buffer) => {
      const text = data.toString();
      allOutput += text;
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
      allOutput += text;
      sendLog(ws, `[stderr] ${text}`);
    });

    claude.on('error', (err) => { sendLog(ws, `[error] ${err.message}\n`); resolve(1); });
    claude.on('close', (code) => { log(`Claude Code exited with code ${code}`); resolve(code ?? 1); });
  });

  sendLog(ws, `\n--- Claude Code exited (code: ${exitCode}) ---\n`);
  return { exitCode, allOutput, lastResultText };
}

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

  if (!/^[a-zA-Z0-9_./-]+$/.test(rawBaseBranch)) throw new Error(`Invalid base_branch: ${rawBaseBranch}`);
  const baseBranch = rawBaseBranch;
  const taskTitle = rawTitle.replace(/[`$\\!"';&|<>(){}[\]]/g, '');
  if (!prompt) throw new Error('No prompt found in task payloads');

  const projectCtx = task.project_context as Record<string, unknown> | undefined;
  const contextPrefix = projectCtx && Object.keys(projectCtx).some(k => {
    const v = projectCtx[k]; return Array.isArray(v) && v.length > 0;
  }) ? formatProjectContext(projectCtx) + '\n\n' : '';
  const finalPrompt = `${contextPrefix}${prompt}`;

  // Clone
  statusRef.value = `Cloning ${owner}/${repo}...`;
  sendLog(ws, `Cloning ${owner}/${repo}...\n`);
  execFileSync('rm', ['-rf', WORKSPACE], { encoding: 'utf-8' });
  const cloneUrl = `https://x-access-token:${ghToken}@github.com/${owner}/${repo}.git`;
  execFileSync('git', ['clone', '--depth', '50', '--branch', baseBranch, cloneUrl, WORKSPACE], { encoding: 'utf-8', timeout: 300_000 });

  git(['config', 'user.email', 'blueprint-agent@automated.dev']);
  git(['config', 'user.name', 'Blueprint Agent']);

  const shortId = taskId.replace(/-/g, '').slice(0, 8);
  const workBranch = `blueprint/task-${shortId}`;
  git(['checkout', '-b', workBranch]);
  sendLog(ws, `Working branch: ${workBranch}\n`);

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

    // Rebase onto latest base branch before push to avoid merge conflicts in the PR
    statusRef.value = `Rebasing onto ${baseBranch}...`;
    try {
      git(['fetch', 'origin', baseBranch]);
      git(['rebase', `origin/${baseBranch}`]);
      sendLog(ws, `Rebased onto latest ${baseBranch}\n`);
    } catch {
      sendLog(ws, `Rebase onto ${baseBranch} had conflicts — pushing as-is, review pipeline will handle\n`);
      try { git(['rebase', '--abort']); } catch {}
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
        : `Automated changes for: ${taskTitle}\n\n---\n*Automated by Blueprint Agent*`;
      const prRes = await apiPost('/api/agent/github/pull-requests', {
        task_id: taskId, owner, repo, title: `[Blueprint] ${taskTitle}`,
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
  const prNumber = payload.pr_number as number;
  const branch = String(payload.branch || '');
  const baseBranch = String(payload.base_branch || 'main');
  const originalPrompt = String(payload.original_prompt || '');

  if (!branch || !prNumber) throw new Error('Review task missing branch or pr_number');

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

Review the changes thoroughly:
- Read the changed files and their surrounding context
- Check for correctness, bugs, and security issues
- Verify the changes accomplish the original task
- Check that nothing is broken by the changes

After your thorough review, output EXACTLY one of these JSON blocks as the very last thing in your response:

If the changes look good and should be merged:
{"verdict": "APPROVE", "feedback": "Brief summary of why it's approved"}

If changes are needed before merging:
{"verdict": "REQUEST_CHANGES", "feedback": "Detailed description of what needs to be fixed"}

IMPORTANT: Also write your verdict JSON to /tmp/review_verdict.json (NOT in the repository). The file should contain ONLY the JSON object, no markdown wrapping.`;

  statusRef.value = 'Running code review...';
  const { exitCode, allOutput, lastResultText } = await runClaudeCode(reviewPrompt, ws, statusRef, model, effort);

  // Prefer reading verdict from /tmp/ file (avoids output truncation), fall back to output parsing
  const fs = await import('fs');
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
  const branch = String(payload.branch || '');
  const baseBranch = String(payload.base_branch || 'main');
  const prompt = String(payload.prompt || '');
  const prNumber = payload.pr_number as number;

  if (!branch || !prompt) throw new Error('Fix task missing branch or prompt');

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

  // Always check for conflict markers — even if rebase/merge appeared to succeed
  let enrichedPrompt = prompt;
  try {
    const conflictFiles = git(['diff', '--name-only', '--diff-filter=U']);
    if (conflictFiles.trim()) {
      hasConflicts = true;
      enrichedPrompt = `MERGE CONFLICTS DETECTED in the following files:\n${conflictFiles}\n\nThis is the PRIMARY task — resolve ALL conflict markers (${'<'.repeat(7)} ${'='.repeat(7)} ${'>'.repeat(7)}) in each file listed above. Keep the intent of this PR's changes (HEAD) while incorporating upstream updates from ${baseBranch}.\n\nAfter resolving conflicts, stage the files with git add and ensure no conflict markers remain.\n\n${prompt}`;
      sendLog(ws, `Conflicting files:\n${conflictFiles}\n`);
    }
  } catch { /* no unmerged files */ }
  // Also check for conflict markers in tracked files via grep
  if (!hasConflicts) {
    try {
      const markerCheck = execFileSync('git', ['-C', WORKSPACE, 'grep', '-rl', '<' + '<<<<<<', '--', '*.ts', '*.tsx', '*.py', '*.js', '*.json'], { encoding: 'utf-8', timeout: 10_000 }).trim();
      if (markerCheck) {
        enrichedPrompt = `CONFLICT MARKERS FOUND in the following files:\n${markerCheck}\n\nThis is the PRIMARY task — resolve ALL conflict markers (${'<'.repeat(7)} ${'='.repeat(7)} ${'>'.repeat(7)}) in each file. Keep the intent of this PR's changes while incorporating upstream updates from ${baseBranch}.\n\n${prompt}`;
        sendLog(ws, `Files with conflict markers:\n${markerCheck}\n`);
      }
    } catch { /* no markers found, good */ }
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

  // Final rebase onto base branch to ensure no conflicts remain before push
  if (exitCode === 0) {
    try {
      git(['fetch', 'origin', baseBranch]);
      git(['rebase', `origin/${baseBranch}`]);
      sendLog(ws, `Final rebase onto ${baseBranch} successful\n`);
    } catch {
      sendLog(ws, `Final rebase onto ${baseBranch} had conflicts — pushing as-is\n`);
      try { git(['rebase', '--abort']); } catch {}
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
  const lines: string[] = [
    '## Project Context',
    'The following tasks are already active or completed in this project. Avoid duplicating their work.',
    '',
  ];

  const activeTasks = ctx.active_tasks as Array<Record<string, unknown>> | undefined;
  if (activeTasks && activeTasks.length > 0) {
    lines.push('### Active Tasks');
    for (const t of activeTasks) {
      lines.push(`- [${t.task_type}] **${t.title}** (${t.status}) — ${String(t.prompt || '').slice(0, 120)}`);
    }
    lines.push('');
  }

  const queuedTasks = ctx.queued_tasks as Array<Record<string, unknown>> | undefined;
  if (queuedTasks && queuedTasks.length > 0) {
    lines.push('### Queued Tasks');
    for (const t of queuedTasks) {
      lines.push(`- [${t.task_type}] **${t.title}** — ${String(t.prompt || '').slice(0, 120)}`);
    }
    lines.push('');
  }

  const recentTasks = ctx.recently_completed_tasks as Array<Record<string, unknown>> | undefined;
  if (recentTasks && recentTasks.length > 0) {
    lines.push('### Recently Completed Tasks');
    for (const t of recentTasks) {
      const pr = t.pr_url ? ` (${t.pr_url})` : '';
      const summary = t.research_findings_summary ? ` — ${String(t.research_findings_summary).slice(0, 120)}` : '';
      lines.push(`- [${t.task_type}] **${t.title}**${pr}${summary}`);
    }
    lines.push('');
  }

  const openPRs = ctx.open_prs as string[] | undefined;
  if (openPRs && openPRs.length > 0) {
    lines.push('### Open PRs');
    for (const pr of openPRs) {
      lines.push(`- ${pr}`);
    }
    lines.push('');
  }

  return lines.join('\n');
}

// ── Handler: Plan (decompose complex task into subtasks) ───────────────────
async function handlePlan(
  taskId: string, payload: Record<string, unknown>, task: Record<string, unknown>,
  ghToken: string, owner: string, repo: string, ws: WebSocket | null, statusRef: { value: string },
  model?: string, effort?: string
) {
  const prompt = String(payload.prompt || payload.description || task.description || '');
  const baseBranch = String(payload.base_branch || 'main');

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
      "related_files": ["src/main.py", "src/utils.py"]
    },
    {
      "title": "feat: second subtask",
      "prompt": "...",
      "depends_on_indices": [0],
      "related_files": ["src/main.py"]
    }
  ]
}

Rules:
- depends_on_indices uses 0-based array positions (e.g., [0, 1] means this subtask depends on the first and second subtasks)
- related_files: list the key files each subtask will create or modify
- Each subtask prompt must be self-contained — don't reference other subtasks by index
- Keep prompts concise — describe what to achieve, not step-by-step how
- Order subtasks so that dependencies flow forward (a subtask can only depend on earlier subtasks)
- If subtasks involve structural refactors of the same files (renames, interface changes, restructuring), set depends_on_indices between them. Small additive changes to shared files can stay parallel.
- Aim for 2-6 subtasks. If the task is simple enough for 1 PR, output a single subtask with no dependencies
- Do NOT include review, testing, or deployment subtasks — those are handled automatically by the pipeline
- IMPORTANT: Write your plan JSON to /tmp/plan_output.json (NOT in the repository). The file should contain ONLY the JSON object, no markdown wrapping.`;

  statusRef.value = 'Running Claude Code for planning...';
  const { exitCode, allOutput, lastResultText } = await runClaudeCode(planPrompt, ws, statusRef, model, effort);

  // Prefer reading plan from /tmp/ file (avoids output truncation), fall back to output parsing
  let logs = '';
  const fs = await import('fs');
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

  const directionsText = directions.length > 0
    ? directions.map((d, i) => `${i + 1}. [${d.priority || 'medium'}] ${d.text} (id: ${d.id})`).join('\n')
    : 'No specific directions provided. Identify general improvements.';

  const projectCtx = task.project_context as Record<string, unknown> | undefined;
  const contextPrefix = projectCtx && Object.keys(projectCtx).some(k => {
    const v = projectCtx[k]; return Array.isArray(v) && v.length > 0;
  }) ? formatProjectContext(projectCtx) + '\n\n' : '';

  const researchPrompt = `${contextPrefix}You are a codebase research agent. You have full access to this repository. Your job is to analyze the codebase and produce structured findings that will be used to create concrete implementation tasks.

${userPrompt ? `Research focus:\n${userPrompt}\n` : ''}Current project directions from the user:
${directionsText}

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
- type: improvement, bug, refactor, feature, test, research, security_audit, qa, deps, docs, migrate
  (use "research" when an area needs deeper investigation before implementation)
  (use "security_audit" when the finding involves security vulnerabilities that need a dedicated audit)
  (use "qa" when the finding is about missing test coverage)
  (use "deps" when the finding is about outdated or insecure dependencies)
  (use "docs" when the finding is about missing or outdated documentation)
  (use "migrate" when the finding requires a database schema migration via Alembic)
- scope: small (single file, under 50 lines), medium (2-5 files, 50-200 lines), large (cross-component, architectural, orchestrator/model/agent/security changes — anything that could break the system if done wrong)
- risk: low (tests, docs, cosmetic), medium (logic changes, new endpoints), high (core logic, auth, data models)
- priority: high, medium, low

Rules:
- Each finding should be ONE PR-sized unit of work (scope=small) or a plannable initiative (scope=medium/large)
- The prompt field must be self-contained — the implementing agent knows nothing about this research
- depends_on_indices uses 0-based array positions. If findings involve structural refactors of the same files (renames, interface changes, restructuring), set depends_on_indices so they run sequentially. Small additive changes to shared files can stay parallel.
- Limit to 30 findings maximum, prioritized by impact
- For medium/large scope findings, the prompt should describe the full scope — it will be sent to a planning agent for decomposition
- For type=research findings, the prompt should describe what needs deeper investigation — it will spawn a sub-research task
- For type=security_audit findings, the prompt should describe the audit scope/focus — it will spawn a dedicated security audit agent
- For type=qa findings, the prompt should describe what to test — it will spawn a dedicated QA agent
- For type=deps findings, the prompt can be minimal (e.g. 'Update dependencies') — it will spawn a dedicated dependency update agent
- For type=docs findings, the prompt can be minimal (e.g. 'Update documentation') — it will spawn a dedicated documentation agent
- Use model=claude-opus-4-6 and effort=high only for complex architectural changes. Leave null for most findings to use the project default.

How your findings get routed (you control this via type and scope):
- type=security_audit → spawns a **security audit** agent that scans for vulnerabilities and auto-spawns fix tasks for critical/high findings
- type=qa → spawns a **QA** agent that generates missing tests and opens a PR
- type=deps → spawns a **dependency updater** agent that checks for outdated/insecure deps and opens a PR
- type=docs → spawns a **docs** agent that generates/updates documentation and opens a PR
- type=migrate → spawns a **migration** agent that generates Alembic migrations and opens a PR
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

  statusRef.value = 'Running codebase research...';
  const { exitCode, allOutput, lastResultText } = await runClaudeCode(researchPrompt, ws, statusRef, model, effort);

  // Prefer reading findings from /tmp/ file (avoids output truncation), fall back to output parsing
  let logs = '';
  const fs = await import('fs');
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
  if (!/^[a-zA-Z0-9_./-]+$/.test(rawBaseBranch)) throw new Error(`Invalid base_branch: ${rawBaseBranch}`);
  const baseBranch = rawBaseBranch;

  // Clone
  statusRef.value = `Cloning ${owner}/${repo}...`;
  sendLog(ws, `Cloning ${owner}/${repo}...\n`);
  execFileSync('rm', ['-rf', WORKSPACE], { encoding: 'utf-8' });
  const cloneUrl = `https://x-access-token:${ghToken}@github.com/${owner}/${repo}.git`;
  execFileSync('git', ['clone', '--depth', '50', '--branch', baseBranch, cloneUrl, WORKSPACE], { encoding: 'utf-8', timeout: 300_000 });

  git(['config', 'user.email', 'blueprint-agent@automated.dev']);
  git(['config', 'user.name', 'Blueprint Agent']);

  const shortId = taskId.replace(/-/g, '').slice(0, 8);
  const workBranch = `blueprint/docs-${shortId}`;
  git(['checkout', '-b', workBranch]);
  sendLog(ws, `Working branch: ${workBranch}\n`);

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

  statusRef.value = 'Running Claude Code...';
  const { exitCode, allOutput, lastResultText } = await runClaudeCode(docsPrompt, ws, statusRef, model, effort);

  const diff = git(['status', '--porcelain']);
  const hasChanges = diff.length > 0;

  if (hasChanges && exitCode === 0) {
    statusRef.value = 'Pushing changes...';
    sendLog(ws, '\nCommitting and pushing changes...\n');

    git(['add', '-A']);
    execFileSync('git', ['-C', WORKSPACE, 'commit', '-m', `docs: update documentation\n\nAutomated by Blueprint Agent (task: ${shortId})`], { encoding: 'utf-8' });


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
        : `Documentation improvements\n\n---\n*Automated by Blueprint Agent*`;
      const prRes = await apiPost('/api/agent/github/pull-requests', {
        task_id: taskId, owner, repo, title: 'docs: update documentation',
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
  if (!/^[a-zA-Z0-9_./-]+$/.test(rawBaseBranch)) throw new Error(`Invalid base_branch: ${rawBaseBranch}`);
  const baseBranch = rawBaseBranch;

  statusRef.value = `Cloning ${owner}/${repo} for security audit...`;
  sendLog(ws, `Starting security audit...\n`);
  execFileSync('rm', ['-rf', WORKSPACE], { encoding: 'utf-8' });
  const cloneUrl = `https://x-access-token:${ghToken}@github.com/${owner}/${repo}.git`;
  execFileSync('git', ['clone', '--branch', baseBranch, cloneUrl, WORKSPACE], { encoding: 'utf-8', timeout: 300_000 });

  const securityPrompt = `You are a security audit agent. You have full access to this repository. Your job is to analyze the codebase for security vulnerabilities and produce structured findings.

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

  statusRef.value = 'Running security audit...';
  const { exitCode, allOutput, lastResultText } = await runClaudeCode(securityPrompt, ws, statusRef, model, effort);

  let logs = '';
  const fs = await import('fs');
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

  if (!/^[a-zA-Z0-9_./-]+$/.test(rawBaseBranch)) throw new Error(`Invalid base_branch: ${rawBaseBranch}`);
  const baseBranch = rawBaseBranch;
  const taskTitle = rawTitle.replace(/[`$\\!"';&|<>(){}[\]]/g, '');

  // Clone
  statusRef.value = `Cloning ${owner}/${repo}...`;
  sendLog(ws, `Cloning ${owner}/${repo}...\n`);
  execFileSync('rm', ['-rf', WORKSPACE], { encoding: 'utf-8' });
  const cloneUrl = `https://x-access-token:${ghToken}@github.com/${owner}/${repo}.git`;
  execFileSync('git', ['clone', '--depth', '50', '--branch', baseBranch, cloneUrl, WORKSPACE], { encoding: 'utf-8', timeout: 300_000 });

  git(['config', 'user.email', 'blueprint-agent@automated.dev']);
  git(['config', 'user.name', 'Blueprint Agent']);

  const shortId = taskId.replace(/-/g, '').slice(0, 8);
  const workBranch = `blueprint/task-${shortId}`;
  git(['checkout', '-b', workBranch]);
  sendLog(ws, `Working branch: ${workBranch}\n`);

  const qaPrompt = `You are a QA agent. Your job is to improve test coverage for this codebase.

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
  statusRef.value = 'Running Claude Code...';
  const { exitCode, allOutput, lastResultText } = await runClaudeCode(qaPrompt, ws, statusRef, model, effort);

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
  if (!/^[a-zA-Z0-9_./-]+$/.test(rawBaseBranch)) throw new Error(`Invalid base_branch: ${rawBaseBranch}`);
  const baseBranch = rawBaseBranch;

  // Clone
  statusRef.value = `Cloning ${owner}/${repo}...`;
  sendLog(ws, `Cloning ${owner}/${repo}...\n`);
  execFileSync('rm', ['-rf', WORKSPACE], { encoding: 'utf-8' });
  const cloneUrl = `https://x-access-token:${ghToken}@github.com/${owner}/${repo}.git`;
  execFileSync('git', ['clone', '--depth', '50', '--branch', baseBranch, cloneUrl, WORKSPACE], { encoding: 'utf-8', timeout: 300_000 });

  git(['config', 'user.email', 'blueprint-agent@automated.dev']);
  git(['config', 'user.name', 'Blueprint Agent']);

  const shortId = taskId.replace(/-/g, '').slice(0, 8);
  const workBranch = `blueprint/deps-${shortId}`;
  git(['checkout', '-b', workBranch]);
  sendLog(ws, `Working branch: ${workBranch}\n`);

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

  statusRef.value = 'Running dependency updates...';
  const { exitCode, allOutput, lastResultText } = await runClaudeCode(depsPrompt, ws, statusRef, model, effort);

  const diff = git(['status', '--porcelain']);
  const hasChanges = diff.length > 0;

  if (hasChanges && exitCode === 0) {
    statusRef.value = 'Pushing changes...';
    sendLog(ws, '\nCommitting and pushing dependency updates...\n');

    git(['add', '-A']);
    execFileSync('git', ['-C', WORKSPACE, 'commit', '-m', `chore: update dependencies to latest minor/patch versions\n\nAutomated by Blueprint Agent (task: ${shortId})`], { encoding: 'utf-8' });

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
        : `Automated dependency updates: minor/patch versions only.\n\n---\n*Automated by Blueprint Agent*`;
      const prRes = await apiPost('/api/agent/github/pull-requests', {
        task_id: taskId, owner, repo, title: 'chore: update dependencies',
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

  if (!/^[a-zA-Z0-9_./-]+$/.test(rawBaseBranch)) throw new Error(`Invalid base_branch: ${rawBaseBranch}`);
  const baseBranch = rawBaseBranch;
  const taskTitle = rawTitle.replace(/[`$\\!"';&|<>(){}[\]]/g, '');
  if (!prompt) throw new Error('No prompt found in task payloads');

  // Clone
  statusRef.value = `Cloning ${owner}/${repo}...`;
  sendLog(ws, `Cloning ${owner}/${repo}...\n`);
  execFileSync('rm', ['-rf', WORKSPACE], { encoding: 'utf-8' });
  const cloneUrl = `https://x-access-token:${ghToken}@github.com/${owner}/${repo}.git`;
  execFileSync('git', ['clone', '--depth', '50', '--branch', baseBranch, cloneUrl, WORKSPACE], { encoding: 'utf-8', timeout: 300_000 });

  git(['config', 'user.email', 'blueprint-agent@automated.dev']);
  git(['config', 'user.name', 'Blueprint Agent']);

  const shortId = taskId.replace(/-/g, '').slice(0, 8);
  const workBranch = `blueprint/migrate-${shortId}`;
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

  statusRef.value = 'Running migration workflow...';
  const { exitCode, allOutput, lastResultText } = await runClaudeCode(migratePrompt, ws, statusRef, model, effort);

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

  if (!/^[a-zA-Z0-9_./-]+$/.test(rawBaseBranch)) throw new Error(`Invalid base_branch: ${rawBaseBranch}`);
  const baseBranch = rawBaseBranch;
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

  // Clone
  statusRef.value = `Cloning ${owner}/${repo}...`;
  sendLog(ws, `Cloning ${owner}/${repo}...\n`);
  execFileSync('rm', ['-rf', WORKSPACE], { encoding: 'utf-8' });
  const cloneUrl = `https://x-access-token:${ghToken}@github.com/${owner}/${repo}.git`;
  execFileSync('git', ['clone', '--depth', '50', '--branch', baseBranch, cloneUrl, WORKSPACE], { encoding: 'utf-8', timeout: 300_000 });

  git(['config', 'user.email', 'blueprint-agent@automated.dev']);
  git(['config', 'user.name', 'Blueprint Agent']);

  const shortId = taskId.replace(/-/g, '').slice(0, 8);
  const workBranch = `blueprint/refactor-${shortId}`;
  git(['checkout', '-b', workBranch]);
  sendLog(ws, `Working branch: ${workBranch}\n`);

  // Run Claude
  statusRef.value = 'Running Claude Code...';
  const { exitCode, allOutput, lastResultText } = await runClaudeCode(refactorPrompt, ws, statusRef, model, effort);

  const diff = git(['status', '--porcelain']);
  const hasChanges = diff.length > 0;

  if (hasChanges && exitCode === 0) {
    statusRef.value = 'Pushing changes...';
    sendLog(ws, '\nCommitting and pushing changes...\n');
    const changedPaths = diff.split('\n').map(l => l.trim().split(/\s+/).pop() || '');
    await lintFixLoop(changedPaths, ws, statusRef, 'the refactored code', model, effort);

    git(['add', '-A']);
    execFileSync('git', ['-C', WORKSPACE, 'commit', '-m', `refactor: ${taskTitle}\n\nAutomated by Blueprint Agent (task: ${shortId})`], { encoding: 'utf-8' });

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
        : `Refactor: ${taskTitle}\n\n---\n*Automated by Blueprint Agent*`;
      const prRes = await apiPost('/api/agent/github/pull-requests', {
        task_id: taskId, owner, repo, title: `refactor: ${taskTitle}`,
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
    await apiPost('/api/agent/status', { task_id: taskId, status: 'COMPLETED', logs: 'No file changes.' });
  } else {
    await apiPost('/api/agent/status', {
      task_id: taskId, status: 'FAILED', artifact_type: 'ERROR_LOG', logs: sanitize(allOutput.slice(-10_000)),
    });
  }
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

    // Dispatch to handler
    const handler = handlers[taskType];
    if (!handler) throw new Error(`Unknown task type: ${taskType}`);
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
    log('Agent shutdown complete.');
  }
}

main().catch((err) => { console.error('Agent failed to start:', err); process.exit(1); });
