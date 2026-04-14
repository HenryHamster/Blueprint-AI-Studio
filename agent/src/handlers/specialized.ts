import { execFileSync } from 'child_process';
import type WebSocket from 'ws';
import {
  WORKSPACE, strVal, sanitize, sendLog, validateBranchName, readHandlerOutput,
  apiPost, persistAndPostStatus, apiGet, git, gitAsync, cloneWithRetry, sendHeartbeatSync, runClaudeCode,
} from '../index.js';
import type { BuildConfig } from '../index.js';
import { DEFAULT_CLONE_DEPTH } from '../constants.js';
import {
  getTaskTypeHints, PLAYBOOK_GUIDANCE, readClaudeMd, buildContextPrefix, buildFileTree, configureAgentPermissions,
  writePlaybookHelper, writeMessagesHelper, formatPlaybook,
} from '../prompt.js';

// ── Handler: Security Audit (analyze codebase for vulnerabilities, output findings) ──
export async function handleSecurityAudit(
  taskId: string, payload: Record<string, unknown>, task: Record<string, unknown>,
  ghToken: string, owner: string, repo: string, ws: WebSocket | null, statusRef: { value: string },
  model?: string, effort?: string
) {
  const rawBaseBranch = strVal(payload.base_branch, 'main');
  const baseBranch = validateBranchName(rawBaseBranch, 'base_branch');

  statusRef.value = `Cloning ${owner}/${repo} for security audit...`;
  sendLog(ws, `Starting security audit...\n`);
  execFileSync('rm', ['-rf', WORKSPACE], { encoding: 'utf-8' });
  const cloneUrl = `https://x-access-token:${ghToken}@github.com/${owner}/${repo}.git`;
  await cloneWithRetry(['clone', '--depth', String(DEFAULT_CLONE_DEPTH), '--branch', baseBranch, cloneUrl, WORKSPACE]);
  writePlaybookHelper(taskId);
  writeMessagesHelper(taskId);
  const disallowedTools = configureAgentPermissions('security_audit');

  const contextPrefix = buildContextPrefix(task);

  const claudeMd = readClaudeMd();
  const codebaseSummary = claudeMd
    ? `## Codebase Instructions\n${claudeMd}\n\n`
    : '';

  const fileTreeSection = buildFileTree(ws);
  const secPlaybookSection = formatPlaybook(task.playbook as Record<string, string> | undefined);

  const auditScope = strVal(payload.prompt) || strVal(payload.description);
  const auditScopeSection = auditScope ? `Audit focus: ${auditScope}\n\n` : '';

  const securityPrompt = `${codebaseSummary}${secPlaybookSection}${contextPrefix}${fileTreeSection}${auditScopeSection}You are a security audit agent. You have full access to this repository. Your job is to analyze the codebase for security vulnerabilities and produce structured findings.

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
- IMPORTANT: Write your findings JSON array to /tmp/security_findings_${taskId}.json (NOT in the repository). The file should contain ONLY the JSON array, no markdown wrapping.
- Also output the same JSON array as the final output in your logs.`;

  const securityPromptSuffix = payload.prompt_suffix ? `\n\nAdditional instructions:\n${strVal(payload.prompt_suffix)}` : '';
  const secBuildConfig = (task.build_config ?? {}) as BuildConfig;
  const secTypeHints = getTaskTypeHints(secBuildConfig);
  const secTypeHint = secTypeHints['security_audit'] ? '\n\n' + secTypeHints['security_audit'] : '';
  statusRef.value = 'Running security audit...';
  const { exitCode, allOutput, lastResultText } = await runClaudeCode(securityPrompt + secTypeHint + securityPromptSuffix, ws, statusRef, model, effort, disallowedTools);

  const findings = readHandlerOutput(taskId, 'security_findings', ws);
  let logs = findings !== null ? sanitize(JSON.stringify(findings)) : '';
  if (!logs) {
    if (lastResultText && lastResultText.includes('"severity"')) {
      logs = sanitize(lastResultText);
    } else {
      const jsonMatch = allOutput.match(/\[\s*\{[\s\S]*"severity"[\s\S]*\}\s*\]/);
      logs = sanitize(jsonMatch ? jsonMatch[0] : (lastResultText || allOutput.slice(-50_000)));
    }
  }

  await persistAndPostStatus(taskId, {
    task_id: taskId,
    status: exitCode === 0 ? 'COMPLETED' : 'FAILED',
    ...(exitCode !== 0 ? { artifact_type: 'ERROR_LOG' } : { artifact_type: 'SECURITY_REPORT' }),
    logs,
  });
}

// ── Handler: Migrate (Alembic/SQLAlchemy database migration workflow) ────────
export async function handleMigrate(
  taskId: string, payload: Record<string, unknown>, task: Record<string, unknown>,
  ghToken: string, owner: string, repo: string, ws: WebSocket | null, statusRef: { value: string },
  model?: string, effort?: string
) {
  const rawPrompt = strVal(payload.prompt) || strVal(payload.description) || strVal(task.description);
  const rawBaseBranch = strVal(payload.base_branch, 'main');
  const rawTitle = strVal(task.title) || strVal(payload.title) || 'database migration';
  const migrateTaskTitle = strVal(task.title);
  const prompt = migrateTaskTitle ? `Task: ${migrateTaskTitle}\n\n${rawPrompt}` : rawPrompt;

  const baseBranch = validateBranchName(rawBaseBranch, 'base_branch');
  const taskTitle = rawTitle.replace(/[`$\\!"';&|<>(){}[\]]/g, '');
  if (!prompt) throw new Error('No prompt found in task payloads');

  // Clone
  statusRef.value = `Cloning ${owner}/${repo}...`;
  sendLog(ws, `Cloning ${owner}/${repo}...\n`);
  execFileSync('rm', ['-rf', WORKSPACE], { encoding: 'utf-8' });
  const cloneUrl = `https://x-access-token:${ghToken}@github.com/${owner}/${repo}.git`;
  await cloneWithRetry(['clone', '--depth', '100', '--branch', baseBranch, cloneUrl, WORKSPACE]);
  writePlaybookHelper(taskId);
  writeMessagesHelper(taskId);
  const disallowedTools = configureAgentPermissions('migrate');

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
      sendLog(ws, `WARNING: Could not delete stale remote branch ${workBranch}: ${e instanceof Error ? e.message : String(e)}\n`);
    }
  }
  git(['checkout', '-b', workBranch]);
  sendLog(ws, `Working branch: ${workBranch}\n`);

  const migrateBuildConfig = (task.build_config ?? {}) as BuildConfig;
  const migratePythonDirs = (migrateBuildConfig.directories ?? []).filter(d => d.type === 'python');
  const migratePyDir = migratePythonDirs.length > 0 ? migratePythonDirs[0].path.replace(/\/$/, '') : 'backend';
  const alembicVersionsPath = `${migratePyDir}/alembic/versions/`;
  const migrateModelsPath = `${migratePyDir}/app/models/`;

  const migratePrompt = `You are a database migration agent specializing in Alembic and SQLAlchemy/SQLModel workflows.

Your task: ${prompt}

Follow these steps precisely:

1. **Examine existing migrations**: Look at \`${alembicVersionsPath}\` to understand naming conventions, import patterns, and how previous migrations are structured. Note the revision ID format, how dependencies are chained, and any project-specific patterns.

2. **Make model changes**: Apply the requested SQLModel/SQLAlchemy model changes in the appropriate files (typically under \`${migrateModelsPath}\` or similar). Do NOT manually write migration files — autogenerate will handle that.

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
- Never delete or modify existing migration files in \`${alembicVersionsPath}\` (except the one you just generated)
- Never run \`alembic stamp\` or \`alembic downgrade base\` — these can corrupt production databases
- If autogenerate produces an empty migration with no changes, verify that your model changes were saved and the alembic env.py is configured to detect them
- Document any data loss risks prominently using inline comments in the migration file`;

  const migrateClaudeMd = readClaudeMd();
  const migrateClaudeMdSection = migrateClaudeMd ? `## Codebase Instructions\n${migrateClaudeMd}\n\n` : '';
  const migratePlaybookSection = formatPlaybook(task.playbook as Record<string, string> | undefined);
  const migratePromptSuffix = payload.prompt_suffix ? `\n\nAdditional instructions:\n${strVal(payload.prompt_suffix)}` : '';
  statusRef.value = 'Running migration workflow...';
  const migrateBaseCommit = git(['rev-parse', 'HEAD']);
  const { exitCode, allOutput, lastResultText } = await runClaudeCode(migrateClaudeMdSection + migratePlaybookSection + migratePrompt + PLAYBOOK_GUIDANCE + migratePromptSuffix, ws, statusRef, model, effort, disallowedTools);

  const diff = git(['status', '--porcelain']);
  const currentHead = git(['rev-parse', 'HEAD']);
  // Detect changes from BOTH uncommitted files AND commits Claude may have created
  const hasChanges = diff.length > 0 || currentHead !== migrateBaseCommit;

  if (hasChanges && exitCode === 0) {
    statusRef.value = 'Pushing changes...';
    sendLog(ws, '\nCommitting and pushing migration changes...\n');

    git(['add', '-A']);
    const migrateStaged = git(['status', '--porcelain']);
    if (migrateStaged.length > 0) {
      execFileSync('git', ['-C', WORKSPACE, 'commit', '-m', `migrate: ${taskTitle}\n\nAutomated by Blueprint Agent (task: ${shortId})`], { encoding: 'utf-8' });
    }

    // Rebase onto latest base branch before push
    statusRef.value = `Rebasing onto ${baseBranch}...`;
    try {
      git(['fetch', 'origin', baseBranch]);
      sendHeartbeatSync(taskId, `Fetched ${baseBranch}, rebasing...`);
      git(['rebase', `origin/${baseBranch}`]);
      sendLog(ws, `Rebased onto latest ${baseBranch}\n`);
    } catch {
      sendLog(ws, `Rebase onto ${baseBranch} had conflicts — pushing as-is\n`);
      try { git(['rebase', '--abort']); } catch { /* ignore */ }
    }

    const commitHash = git(['rev-parse', 'HEAD']);

    sendHeartbeatSync(taskId, `Pushing ${workBranch}...`);
    const freshGhRes = await apiGet(`/api/agent/github/token?task_id=${taskId}`) as { data: { token: string } };
    const pushUrl = `https://x-access-token:${freshGhRes.data.token}@github.com/${owner}/${repo}.git`;
    execFileSync('git', ['-C', WORKSPACE, 'remote', 'set-url', 'origin', pushUrl], { encoding: 'utf-8' });
    await gitAsync(['push', 'origin', workBranch]);
    sendLog(ws, `Pushed to branch: ${workBranch}\n`);

    sendHeartbeatSync(taskId, `Creating pull request for ${workBranch}...`);
    statusRef.value = 'Creating pull request...';
    let prUrl = '';
    try {
      const prBody = lastResultText
        ? `## Summary\n\n${lastResultText.slice(0, 3000)}\n\n---\n*Automated by Blueprint Agent*`
        : `Database migration: ${taskTitle}\n\n---\n*Automated by Blueprint Agent*`;
      const prRes = await apiPost('/api/agent/github/pull-requests', {
        task_id: taskId, owner, repo, title: `migrate: ${taskTitle}`,
        head: workBranch, base: baseBranch, body: prBody,
      }) as { data?: { html_url?: string } };
      prUrl = prRes.data?.html_url ?? '';
      sendLog(ws, `PR created: ${prUrl}\n`);
    } catch (e) { sendLog(ws, `Failed to create PR: ${String(e)}\n`); }

    await persistAndPostStatus(taskId, {
      task_id: taskId, status: 'COMPLETED', artifact_type: 'PULL_REQUEST',
      commit_hash: commitHash, pr_url: prUrl, repo_url: `https://github.com/${owner}/${repo}`,
    });
  } else if (exitCode === 0) {
    await persistAndPostStatus(taskId, { task_id: taskId, status: 'COMPLETED', logs: 'No migration changes generated.' });
  } else {
    await persistAndPostStatus(taskId, {
      task_id: taskId, status: 'FAILED', artifact_type: 'ERROR_LOG', logs: sanitize(allOutput.slice(-10_000)),
      failure_category: 'claude_code_failed',
    });
  }
}

// ── Handler: Smoketest (post-merge end-to-end health checks) ─────────────────
export async function handleSmoketest(
  taskId: string, payload: Record<string, unknown>, task: Record<string, unknown>,
  ghToken: string, owner: string, repo: string, ws: WebSocket | null, statusRef: { value: string },
  model?: string, effort?: string
) {
  const rawBaseBranch = strVal(payload.base_branch, 'main');
  const baseBranch = validateBranchName(rawBaseBranch, 'base_branch');

  // Clone (read-only — no work branch or push needed)
  statusRef.value = `Cloning ${owner}/${repo}...`;
  sendLog(ws, `Cloning ${owner}/${repo} @ ${baseBranch}...\n`);
  execFileSync('rm', ['-rf', WORKSPACE], { encoding: 'utf-8' });
  const cloneUrl = `https://x-access-token:${ghToken}@github.com/${owner}/${repo}.git`;
  await cloneWithRetry(['clone', '--depth', '100', '--branch', baseBranch, cloneUrl, WORKSPACE]);
  writePlaybookHelper(taskId);
  writeMessagesHelper(taskId);
  const disallowedTools = configureAgentPermissions('smoketest');

  const smoketestBuildConfig = (task.build_config ?? {}) as BuildConfig;
  const smoketestPythonDirs = (smoketestBuildConfig.directories ?? []).filter(d => d.type === 'python');
  const smoketestPyDir = smoketestPythonDirs.length > 0 ? smoketestPythonDirs[0].path.replace(/\/$/, '') : 'backend';
  const smoketestConfigPath = `${smoketestPyDir}/testing/smoketest.yaml`;

  const smoketestPrompt = `You are a smoke test agent. Your job is to run structured health checks on this codebase using the smoketest configuration.

Follow these steps:

1. Read ${smoketestConfigPath} to understand the test suites and guardrails.

2. Run each suite IN ORDER from the config (startup, curated, regression):

   a. STARTUP: Run each command listed in suites.startup.commands from the ${smoketestPyDir}/ directory.
      Record pass/fail for each command.

   b. CURATED: Run pytest with the args from suites.curated.pytest_args.
      Working directory: ${smoketestPyDir}/
      Command: python3 -m pytest <pytest_args from config>
      Record individual test results (name, pass/fail, error message if any).

   c. REGRESSION: Run pytest with the args from suites.regression.pytest_args.
      Working directory: ${smoketestPyDir}/
      Command: python3 -m pytest <pytest_args from config>
      Record individual test results.

3. Check guardrails from the config:
   - Count tests collected for curated and regression suites
   - If count < guardrails.min_test_count for either suite, record a guardrail_violation

4. Write results to /tmp/smoketest_result_${taskId}.json with this structure:
{
  "passed": false,
  "suites": {
    "startup": {"passed": true, "tests": [{"name": "...", "status": "pass", "message": "..."}]},
    "curated": {"passed": true, "tests": [...]},
    "regression": {"passed": false, "tests": [...]}
  },
  "guardrail_violations": [],
  "tests": [<flat list of ALL tests from all suites, for backward compatibility>]
}

Set top-level "passed" to true only if ALL suites passed and there are no guardrail violations.

IMPORTANT: Always write /tmp/smoketest_result_${taskId}.json even if you cannot run any tests (use status "skip" with a message explaining why). Never leave the file unwritten.`;

  statusRef.value = 'Running smoke tests...';
  const { exitCode, allOutput } = await runClaudeCode(smoketestPrompt, ws, statusRef, model, effort, disallowedTools);

  // Read result file written by Claude
  const smoketestResult = readHandlerOutput<{ passed: boolean; tests: Array<{ name: string; status: string; message: string }> }>(taskId, 'smoketest_result', ws);

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
    await persistAndPostStatus(taskId, {
      task_id: taskId,
      status: passed ? 'COMPLETED' : 'FAILED',
      artifact_type: 'SMOKETEST_RESULT',
      logs: JSON.stringify(smoketestResult),
      repo_url: `https://github.com/${owner}/${repo}`,
    });
  } else {
    await persistAndPostStatus(taskId, {
      task_id: taskId, status: 'FAILED', artifact_type: 'ERROR_LOG',
      logs: sanitize(allOutput.slice(-10_000)),
      failure_category: 'claude_code_failed',
    });
  }
}

// ── Handler: Debug (analyze task/CI/test failures and produce root cause analysis) ──
export async function handleDebug(
  taskId: string, payload: Record<string, unknown>, task: Record<string, unknown>,
  ghToken: string, owner: string, repo: string, ws: WebSocket | null, statusRef: { value: string },
  model?: string, effort?: string
) {
  const errorLogs = strVal(payload.error_logs) || strVal(payload.failure_context);
  const failedTaskId = strVal(payload.failed_task_id);
  const failedTaskTitle = strVal(payload.failed_task_title) || strVal(task.title);
  const rawBaseBranch = strVal(payload.base_branch, 'main');
  validateBranchName(rawBaseBranch, 'base_branch');

  // Clone for read-only analysis (no work branch or push needed)
  statusRef.value = `Cloning ${owner}/${repo} for debug analysis...`;
  sendLog(ws, `Cloning ${owner}/${repo} @ ${rawBaseBranch} for debug analysis...\n`);
  execFileSync('rm', ['-rf', WORKSPACE], { encoding: 'utf-8' });
  const cloneUrl = `https://x-access-token:${ghToken}@github.com/${owner}/${repo}.git`;
  await cloneWithRetry(['clone', '--depth', '100', '--branch', rawBaseBranch, cloneUrl, WORKSPACE]);
  writePlaybookHelper(taskId);
  writeMessagesHelper(taskId);
  const disallowedTools = configureAgentPermissions('debug');

  // Gather git context upfront to include in the prompt
  const gitLog = git(['log', '--oneline', '-20']);
  const gitDiffStat = (() => { try { return git(['diff', 'HEAD~5...HEAD', '--stat']); } catch { return '(not enough history)'; } })();

  // Load codebase instructions and project context (like other handlers)
  const claudeMd = readClaudeMd();
  const contextPrefix = buildContextPrefix(task);
  const fileTree = buildFileTree(ws);

  const debugPrompt = `You are a debug analysis agent. Your job is to analyze a task failure and produce a structured root cause analysis. Do NOT make any code changes.
${claudeMd ? `\n## Codebase Instructions\n${claudeMd}\n` : ''}${contextPrefix ? `\n${contextPrefix}` : ''}${fileTree ? `\n## Repository File Tree\n${fileTree}\n` : ''}
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

5. Write your analysis to /tmp/debug_report_${taskId}.json with EXACTLY this structure:
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

IMPORTANT: Always write /tmp/debug_report_${taskId}.json even if you cannot determine the root cause (use confidence "low" and explain what you found in root_cause). Never leave the file unwritten.`;

  statusRef.value = 'Analyzing failure...';
  const { allOutput } = await runClaudeCode(debugPrompt + PLAYBOOK_GUIDANCE + '\nNote: If your investigation revealed a root cause pattern, diagnostic technique, or system behavior that would help future debugging, save it to the playbook.\n', ws, statusRef, model, effort, disallowedTools);

  const debugResult = readHandlerOutput<{ root_cause: string; affected_files: string[]; fix_prompt: string; confidence: string }>(taskId, 'debug_report', ws);

  if (debugResult) {
    sendLog(ws, `\nDebug analysis complete (confidence: ${debugResult.confidence})\n`);
    sendLog(ws, `Root cause: ${debugResult.root_cause.slice(0, 500)}\n`);
    await persistAndPostStatus(taskId, {
      task_id: taskId,
      status: 'COMPLETED',
      artifact_type: 'DEBUG_RESULT',
      logs: JSON.stringify(debugResult),
      repo_url: `https://github.com/${owner}/${repo}`,
    });
  } else {
    await persistAndPostStatus(taskId, {
      task_id: taskId, status: 'FAILED', artifact_type: 'ERROR_LOG',
      logs: sanitize(allOutput.slice(-10_000)),
      failure_category: 'claude_code_failed',
    });
  }
}

// ── Handler: Perf (analyze codebase for performance bottlenecks, output findings) ──
export async function handlePerf(
  taskId: string, payload: Record<string, unknown>, task: Record<string, unknown>,
  ghToken: string, owner: string, repo: string, ws: WebSocket | null, statusRef: { value: string },
  model?: string, effort?: string
) {
  const rawBaseBranch = strVal(payload.base_branch, 'main');
  const baseBranch = validateBranchName(rawBaseBranch, 'base_branch');

  statusRef.value = `Cloning ${owner}/${repo} for performance analysis...`;
  sendLog(ws, `Starting performance analysis...\n`);
  execFileSync('rm', ['-rf', WORKSPACE], { encoding: 'utf-8' });
  const cloneUrl = `https://x-access-token:${ghToken}@github.com/${owner}/${repo}.git`;
  await cloneWithRetry(['clone', '--depth', String(DEFAULT_CLONE_DEPTH), '--branch', baseBranch, cloneUrl, WORKSPACE]);
  writePlaybookHelper(taskId);
  writeMessagesHelper(taskId);
  const disallowedTools = configureAgentPermissions('perf');

  const perfBuildConfig = (task.build_config ?? {}) as BuildConfig;
  const perfDirs = perfBuildConfig.directories ?? [];
  const perfPythonDirs = perfDirs.filter(d => d.type === 'python');
  const perfNodeDirs = perfDirs.filter(d => d.type === 'node');
  const perfPyDir = perfPythonDirs.length > 0 ? perfPythonDirs[0].path.replace(/\/$/, '') : 'backend';
  const perfNodeDir = perfNodeDirs.length > 0 ? perfNodeDirs[0].path.replace(/\/$/, '') : 'admin-ui';

  const perfPrompt = `You are a performance analysis agent. You have full access to this repository. Your job is to analyze the codebase for demonstrable performance bottlenecks and produce structured, evidence-based findings.

Analyze the codebase for the following performance anti-patterns. Only flag issues you can directly observe in the code — do NOT speculate about theoretical problems.

1. **N+1 database queries**: ORM relationships loaded inside loops (e.g. accessing \`obj.related\` inside a for loop without eager loading). Check ${perfPyDir}/ for SQLAlchemy patterns.
2. **Missing database indexes**: Columns used in WHERE clauses, JOIN conditions, or ORDER BY that lack an index. Check ${perfPyDir}/ for table definitions and compare against query patterns.
3. **Synchronous blocking operations**: Network calls, file I/O, or CPU-heavy work done synchronously inside async request handlers.
4. **Unpaginated large payload returns**: API endpoints that return full table scans or unbounded lists without LIMIT/offset or cursor pagination.
5. **Frontend large bundle imports**: Direct imports of entire libraries where only a small subset is used (e.g. \`import _ from 'lodash'\` instead of \`import debounce from 'lodash/debounce'\`). Check ${perfNodeDir}/src.
6. **Unnecessary React re-renders**: Components with expensive computations or callbacks not wrapped in useMemo/useCallback, causing re-renders on every parent update. Check ${perfNodeDir}/src for patterns.

Instructions:
1. Read key files in ${perfPyDir}/ (table/index definitions, query patterns, endpoint handlers) and ${perfNodeDir}/src (React components, imports).
2. For each finding, identify the specific file, function, and line range where the problem is observable.
3. Only report findings that are demonstrably present — cite the exact code pattern that shows the problem.
4. Produce your findings as a JSON array written to /tmp/perf_report_${taskId}.json:

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
- IMPORTANT: Write your findings JSON array to /tmp/perf_report_${taskId}.json (NOT in the repository). The file should contain ONLY the JSON array, no markdown wrapping.
- Also output the same JSON array as the final output in your logs.`;

  const perfPerfSuffix = payload.prompt_suffix ? `\n\nAdditional instructions:\n${strVal(payload.prompt_suffix)}` : '';
  const perfTypeHints = getTaskTypeHints(perfBuildConfig);
  const perfTypeHint = perfTypeHints['perf'] ? '\n\n' + perfTypeHints['perf'] : '';
  statusRef.value = 'Running performance analysis...';
  const { exitCode, allOutput, lastResultText } = await runClaudeCode(perfPrompt + perfTypeHint + perfPerfSuffix, ws, statusRef, model, effort, disallowedTools);

  const perfFindings = readHandlerOutput(taskId, 'perf_report', ws);
  let logs = perfFindings !== null ? sanitize(JSON.stringify(perfFindings)) : '';
  if (!logs) {
    if (lastResultText && lastResultText.includes('"severity"')) {
      logs = sanitize(lastResultText);
    } else {
      const jsonMatch = allOutput.match(/\[\s*\{[\s\S]*"severity"[\s\S]*\}\s*\]/);
      logs = sanitize(jsonMatch ? jsonMatch[0] : (lastResultText || allOutput.slice(-50_000)));
    }
  }

  await persistAndPostStatus(taskId, {
    task_id: taskId,
    status: exitCode === 0 ? 'COMPLETED' : 'FAILED',
    ...(exitCode !== 0 ? { artifact_type: 'ERROR_LOG' } : { artifact_type: 'PERF_REPORT' }),
    logs,
  });
}

// ── Handler: Investigate (analyze issue, output structured diagnosis report) ──
export async function handleInvestigate(
  taskId: string, payload: Record<string, unknown>, task: Record<string, unknown>,
  ghToken: string, owner: string, repo: string, ws: WebSocket | null, statusRef: { value: string },
  model?: string, effort?: string
) {
  const rawBaseBranch = strVal(payload.base_branch, 'main');
  const baseBranch = validateBranchName(rawBaseBranch, 'base_branch');
  const userPrompt = strVal(payload.prompt) || strVal(payload.description) || strVal(task.description);

  statusRef.value = `Cloning ${owner}/${repo} for investigation...`;
  sendLog(ws, `Starting investigation...\n`);
  execFileSync('rm', ['-rf', WORKSPACE], { encoding: 'utf-8' });
  const cloneUrl = `https://x-access-token:${ghToken}@github.com/${owner}/${repo}.git`;
  await cloneWithRetry(['clone', '--depth', String(DEFAULT_CLONE_DEPTH), '--branch', baseBranch, cloneUrl, WORKSPACE]);
  writePlaybookHelper(taskId);
  writeMessagesHelper(taskId);
  const disallowedTools = configureAgentPermissions('investigate');

  const investigatePrompt = `You are an investigation agent. You have full read access to this repository. Your job is to analyze the issue described below and produce a structured diagnosis report.

Issue to investigate:
${userPrompt}

Instructions:
1. Read relevant source files to understand the codebase and locate the root cause of the issue.
2. DO NOT make any changes to any files — this is a read-only analysis.
3. Identify the root cause, affected files, and a recommended fix.
4. Write your diagnosis as a JSON object to /tmp/investigation_report_${taskId}.json with EXACTLY this schema:

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
- Write ONLY the JSON object to /tmp/investigation_report_${taskId}.json — no markdown, no code fences, no extra text.
- Do NOT modify any files in the repository.
- Base every claim on code you have actually read.`;

  statusRef.value = 'Running investigation...';
  const { exitCode } = await runClaudeCode(investigatePrompt + PLAYBOOK_GUIDANCE + '\nNote: If your investigation revealed a root cause pattern, diagnostic technique, or system behavior that would help future debugging, save it to the playbook.\n', ws, statusRef, model, effort, disallowedTools);

  const investigationResult = readHandlerOutput(taskId, 'investigation_report', ws);
  if (investigationResult === null) {
    await persistAndPostStatus(taskId, {
      task_id: taskId,
      status: 'FAILED',
      artifact_type: 'ERROR_LOG',
      logs: `Failed to read investigation report from /tmp/investigation_report_${taskId}.json`,
      failure_category: 'parse_failed',
    });
    return;
  }

  const logs = sanitize(JSON.stringify(investigationResult));
  await persistAndPostStatus(taskId, {
    task_id: taskId,
    status: exitCode === 0 ? 'COMPLETED' : 'FAILED',
    ...(exitCode !== 0 ? { artifact_type: 'ERROR_LOG' } : { artifact_type: 'INVESTIGATION_REPORT' }),
    logs,
  });
}
