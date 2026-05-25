import { execFileSync } from 'child_process';
import type WebSocket from 'ws';
import {
  WORKSPACE, strVal, sanitize, sendLog, validateBranchName, readHandlerOutput,
  persistAndPostStatus, git, runClaudeCode, sendHeartbeatSync,
  setCurrentPromptFingerprint, listOpenPullRequestsForBranch, deriveTaskBranch, matchesRateLimit,
} from '../index.js';
import { prepareWorkspaceForRole } from '../workspacePrep.js';
import type { BuildConfig } from '../index.js';
import { DEFAULT_CLONE_DEPTH } from '../constants.js';
import { describeJsonExtractionResult, extractJsonArrayDetailed, extractJsonObjectDetailed, JsonExtractionError } from '../json-extract.js';
import {
  getTaskTypeHints, PLAYBOOK_GUIDANCE, FIRST_TASK_GUIDANCE, DEFAULT_TOOL_GUIDANCE, CODE_CHANGE_HELPER_GUIDANCE,
  READ_THE_FAILURE_MESSAGE, SIZE_GUARDRAIL_GUIDANCE, PRIOR_ATTEMPT_DIVERGENCE, BEYOND_SCOPE_REPORTING,
  PARALLEL_AUDIT_GUIDANCE,
  getTaskTypeFocusBlock,
  readClaudeMd, buildContextPrefix, buildFileTree, configureAgentPermissions,
  writePlaybookHelper, writeMessagesHelper, formatPlaybook, buildRetryContext, computePromptFingerprint, type PlaybookEntry,
} from '../prompt.js';
import { resolveBuildConfig } from '../build-config.js';
import { runCodeChangeTask } from './code-change.js';

/** Returns true when the project has opted into parallel audit fan-out. */
function isParallelAuditEnabled(task: Record<string, unknown>): boolean {
  const meta = task.project_metadata;
  if (meta && typeof meta === 'object' && !Array.isArray(meta)) {
    return (meta as Record<string, unknown>).enable_parallel_review === true;
  }
  return false;
}

// ── Candidate-SHA helpers (release qualifier) ────────────────────────────────

/** A full 40-char lowercase hex git commit SHA. */
const FULL_SHA_RE = /^[0-9a-f]{40}$/;

/**
 * Resolve the pinned candidate commit SHA for a qualifier task.
 *
 * The release qualifier (backend/app/release_qualifier.py) snapshots
 * `automain` HEAD as a full 40-char SHA and passes it as `candidate_sha`
 * into BOTH the `release_audit` and qualifier-`smoketest` task payloads, so
 * the agent scores the EXACT pinned commit rather than whatever `automain`
 * has drifted to by the time the agent VM starts. The qualifier also stamps
 * `task.context.candidate_sha`; we accept that as a fallback so the handler
 * keeps working regardless of which field the backend populates.
 *
 * Returns the validated SHA, or null when no well-formed SHA is present.
 */
export function resolveCandidateSha(
  payload: Record<string, unknown>,
  task: Record<string, unknown>,
): string | null {
  const candidates: unknown[] = [payload.candidate_sha];
  const ctx = task.context;
  if (ctx && typeof ctx === 'object' && !Array.isArray(ctx)) {
    candidates.push((ctx as Record<string, unknown>).candidate_sha);
  }
  for (const raw of candidates) {
    if (typeof raw === 'string') {
      const sha = raw.trim().toLowerCase();
      if (FULL_SHA_RE.test(sha)) return sha;
    }
  }
  return null;
}

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
  const cloneUrl = `https://x-access-token:${ghToken}@github.com/${owner}/${repo}.git`;
  await prepareWorkspaceForRole({
    workspace: WORKSPACE, repoUrl: cloneUrl, githubToken: ghToken,
    branch: baseBranch, baseBranch, depth: DEFAULT_CLONE_DEPTH,
  });
  writePlaybookHelper(taskId);
  writeMessagesHelper(taskId);
  const disallowedTools = configureAgentPermissions('security_audit');

  const contextPrefix = buildContextPrefix(task);

  const claudeMd = readClaudeMd();
  const codebaseSummary = claudeMd
    ? `## Codebase Instructions\n${claudeMd}\n\n`
    : '';

  const fileTreeSection = buildFileTree(ws);
  const secPlaybookSection = formatPlaybook(task.playbook as Record<string, PlaybookEntry | string> | undefined);
  const secRetryContext = buildRetryContext(task);
  const secFirstTaskSection = task.first_task_on_repo ? FIRST_TASK_GUIDANCE : '';

  const auditScope = strVal(payload.prompt) || strVal(payload.description);
  const auditScopeSection = auditScope ? `Audit focus: ${auditScope}\n\n` : '';

  const securityPrompt = `${codebaseSummary}${secPlaybookSection}${secRetryContext}${contextPrefix}${fileTreeSection}${auditScopeSection}You are a security audit agent. You have full access to this repository. Your job is to analyze the codebase for security vulnerabilities and produce structured findings.

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

  const secBuildConfig = resolveBuildConfig(task);
  const securityPromptSuffix = payload.prompt_suffix ? `\n\nAdditional instructions:\n${strVal(payload.prompt_suffix)}` : '';
  const secTypeHints = getTaskTypeHints(secBuildConfig);
  const secTypeHint = secTypeHints['security_audit'] ? '\n\n' + secTypeHints['security_audit'] : '';
  statusRef.value = 'Running security audit...';
  const secFingerprint = computePromptFingerprint('security_audit', secBuildConfig);
  setCurrentPromptFingerprint(secFingerprint);
  sendHeartbeatSync(taskId, statusRef.value, { prompt_fingerprint: secFingerprint });
  const secParallelGuidance = isParallelAuditEnabled(task) ? PARALLEL_AUDIT_GUIDANCE : '';
  const { exitCode, allOutput, lastResultText } = await runClaudeCode(securityPrompt + secTypeHint + secFirstTaskSection + securityPromptSuffix + secParallelGuidance + PLAYBOOK_GUIDANCE + `\n\n${DEFAULT_TOOL_GUIDANCE}`, ws, statusRef, model, effort, disallowedTools);

  const findings = readHandlerOutput(taskId, 'security_findings', ws);
  let logs = findings !== null ? sanitize(JSON.stringify(findings)) : '';
  if (!logs) {
    if (lastResultText && lastResultText.includes('"severity"')) {
      logs = sanitize(lastResultText);
    } else {
      const findingsArr = extractJsonArrayDetailed(allOutput, 'severity');
      if (!findingsArr.ok) {
        sendLog(ws, `[WARNING] Could not extract security findings JSON: ${describeJsonExtractionResult(findingsArr)}\n`);
      }
      logs = sanitize(findingsArr.ok ? JSON.stringify(findingsArr.value) : (lastResultText || allOutput.slice(-50_000)));
    }
  }

  await persistAndPostStatus(taskId, {
    task_id: taskId,
    status: exitCode === 0 ? 'COMPLETED' : 'FAILED',
    ...(exitCode !== 0 ? { artifact_type: 'ERROR_LOG', failure_category: 'claude_code_failed' } : { artifact_type: 'SECURITY_REPORT' }),
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
  const migrateTaskTitle = strVal(task.title);
  const prompt = migrateTaskTitle ? `Task: ${migrateTaskTitle}\n\n${rawPrompt}` : rawPrompt;

  const baseBranch = validateBranchName(rawBaseBranch, 'base_branch');
  if (!prompt) throw new Error('No prompt found in task payloads');

  // Clone
  statusRef.value = `Cloning ${owner}/${repo}...`;
  sendLog(ws, `Cloning ${owner}/${repo}...\n`);
  const cloneUrl = `https://x-access-token:${ghToken}@github.com/${owner}/${repo}.git`;
  await prepareWorkspaceForRole({
    workspace: WORKSPACE, repoUrl: cloneUrl, githubToken: ghToken,
    branch: baseBranch, baseBranch, depth: 100,
  });
  writePlaybookHelper(taskId);
  writeMessagesHelper(taskId);
  const disallowedTools = configureAgentPermissions('migrate');
  const migrateBuildConfig = resolveBuildConfig(task);

  git(['config', 'user.email', migrateBuildConfig?.git_user_email || 'blueprint-agent@automated.dev']);
  git(['config', 'user.name', migrateBuildConfig?.git_user_name || 'Blueprint Agent']);

  const workBranch = deriveTaskBranch('migrate', taskId);
  let staleBranchExists = false;
  try {
    git(['ls-remote', '--exit-code', 'origin', workBranch]);
    staleBranchExists = true;
  } catch { /* branch doesn't exist on remote — normal case */ }
  if (staleBranchExists) {
    // Refuse to delete the branch if it has open PRs — that auto-closes them
    // and silently destroys the previous attempt's work.
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

  const migratePythonDirs = (migrateBuildConfig?.directories ?? []).filter(d => d.type === 'python');
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

4. **Bump EXPECTED_SCHEMA_REVISION (MANDATORY)**: Open \`${migratePyDir}/app/main.py\` and update the \`EXPECTED_SCHEMA_REVISION\` constant to match the revision string of the migration file you just generated (e.g. \`"0039"\` for \`0039_add_user_role.py\`). This is a hard gate — CI and startup checks will fail if the constant does not match the latest migration revision. Do NOT skip this step.

5. **Review the generated migration**: Open and carefully read the generated file. Check for:
   - Data loss risks (e.g., \`DROP COLUMN\`, \`DROP TABLE\`, type changes that truncate data)
   - Missing indexes on foreign keys or frequently-queried columns
   - Incorrect \`nullable\` settings
   - Server defaults that should be included for backfills
   - \`op.execute()\` statements needed for data migrations
   If you find issues, fix them and document your changes with inline comments.

6. **Test the migration applies cleanly**: Run \`alembic upgrade head\` to verify the migration applies without errors. If it fails:
   - Read the error carefully
   - Fix the migration file (add a comment explaining the manual fix)
   - Run \`alembic upgrade head\` again
   - Repeat until it succeeds

7. **Test downgrade** (if the migration is reversible): Run \`alembic downgrade -1\` then \`alembic upgrade head\` to confirm round-trip works. If downgrade is not possible (e.g., data loss), ensure the \`downgrade()\` function raises \`NotImplementedError\` with an explanation.

8. Once migration applies cleanly, run \`./check test\` (NOT just \`./check\` — migrations always touch backend, so the Docker pytest suite must pass before shipping). Then \`./pr open --title "migrate: <description>" --body "<summary>"\` to push and open the PR, then \`./done\` to mark the task complete. Use \`./fail "<reason>"\` if the migration genuinely cannot be made to work (e.g. autogenerate is fundamentally wrong about the schema and you can't recover).

CRITICAL RULES:
- Never delete or modify existing migration files in \`${alembicVersionsPath}\` (except the one you just generated)
- Never run \`alembic stamp\` or \`alembic downgrade base\` — these can corrupt production databases
- If autogenerate produces an empty migration with no changes, verify that your model changes were saved and the alembic env.py is configured to detect them
- Document any data loss risks prominently using inline comments in the migration file`;

  const migrateClaudeMd = readClaudeMd();
  const migrateClaudeMdSection = migrateClaudeMd ? `## Codebase Instructions\n${migrateClaudeMd}\n\n` : '';
  const migratePlaybookSection = formatPlaybook(task.playbook as Record<string, PlaybookEntry | string> | undefined);
  const migrateRetryContext = buildRetryContext(task);
  const migrateFirstTaskSection = task.first_task_on_repo ? FIRST_TASK_GUIDANCE : '';
  const migratePromptSuffix = payload.prompt_suffix ? `\n\nAdditional instructions:\n${strVal(payload.prompt_suffix)}` : '';
  statusRef.value = 'Running migration workflow...';
  const migrateBaseCommit = git(['rev-parse', 'HEAD']);
  const migrateFocusBlock = getTaskTypeFocusBlock('migrate');
  const finalMigratePrompt = migrateClaudeMdSection + migratePlaybookSection + migrateRetryContext
    + migratePrompt + migrateFirstTaskSection
    + CODE_CHANGE_HELPER_GUIDANCE + migrateFocusBlock + PLAYBOOK_GUIDANCE
    + READ_THE_FAILURE_MESSAGE + SIZE_GUARDRAIL_GUIDANCE + PRIOR_ATTEMPT_DIVERGENCE + BEYOND_SCOPE_REPORTING
    + migratePromptSuffix + `

${DEFAULT_TOOL_GUIDANCE}`;
  const migrateFingerprint = computePromptFingerprint('migrate', migrateBuildConfig);
  setCurrentPromptFingerprint(migrateFingerprint);
  sendHeartbeatSync(taskId, statusRef.value, { prompt_fingerprint: migrateFingerprint });
  // 2026-05-04: post-Claude commit/rebase/push/PR pipeline replaced with
  // runCodeChangeTask. Claude calls ./check + ./pr open + ./done from
  // inside its own session. See agent/src/handlers/code-change.ts.
  await runCodeChangeTask({
    taskId,
    prompt: finalMigratePrompt,
    ws,
    statusRef,
    workBranch,
    baseBranch,
    owner,
    repo,
    testsOnMainBranch: baseBranch === 'main',
    baseCommit: migrateBaseCommit,
    model,
    effort,
    disallowedTools,
    noChangesMessage: 'No migration changes generated.',
  });
}

// ── Handler: Smoketest (post-merge end-to-end health checks) ─────────────────
export async function handleSmoketest(
  taskId: string, payload: Record<string, unknown>, task: Record<string, unknown>,
  ghToken: string, owner: string, repo: string, ws: WebSocket | null, statusRef: { value: string },
  model?: string, effort?: string
) {
  const rawBaseBranch = strVal(payload.base_branch, 'main');
  const baseBranch = validateBranchName(rawBaseBranch, 'base_branch');

  // CRITICAL (C4): the qualifier-smoketest must run against ONE pinned
  // candidate commit, not whatever `automain` has drifted to by the time this
  // VM starts. The release qualifier passes the snapshotted candidate as a
  // full 40-char `candidate_sha` in the task payload. `git clone --branch <X>`
  // (the path prepareWorkspaceForRole takes) accepts a branch/tag name, NOT a
  // SHA — so we clone the branch normally, then fetch+checkout the exact SHA.
  // A plain post-merge smoketest carries no candidate_sha and is unaffected.
  const candidateSha = resolveCandidateSha(payload, task);

  // Clone (read-only — no work branch or push needed). When pinning a
  // candidate SHA, clone with FULL history (depth 0): the snapshotted commit
  // may be older than a shallow clone's depth, and the SHA checkout below must
  // be able to reach it.
  statusRef.value = `Cloning ${owner}/${repo}...`;
  sendLog(
    ws,
    candidateSha
      ? `Cloning ${owner}/${repo} @ ${baseBranch}, pinning candidate ${candidateSha.slice(0, 12)}...\n`
      : `Cloning ${owner}/${repo} @ ${baseBranch}...\n`,
  );
  const cloneUrl = `https://x-access-token:${ghToken}@github.com/${owner}/${repo}.git`;
  await prepareWorkspaceForRole({
    workspace: WORKSPACE, repoUrl: cloneUrl, githubToken: ghToken,
    branch: baseBranch, baseBranch, depth: candidateSha ? 0 : 100,
  });

  // Pin the workspace to the exact candidate commit. `git fetch origin <sha>`
  // brings the precise object (GitHub permits fetching a SHA reachable from a
  // ref; candidate_sha is the snapshotted `automain` HEAD, so it is reachable),
  // then a detached checkout moves the working tree onto it. Fail loud if the
  // SHA cannot be obtained — a smoketest of the wrong commit produces a
  // meaningless qualifier signal.
  if (candidateSha) {
    sendLog(ws, `Fetching + checking out candidate commit ${candidateSha.slice(0, 12)}...\n`);
    try {
      execFileSync('git', ['fetch', 'origin', candidateSha], { cwd: WORKSPACE, stdio: ['ignore', 'pipe', 'pipe'] });
      execFileSync('git', ['checkout', '--detach', candidateSha], { cwd: WORKSPACE, stdio: ['ignore', 'pipe', 'pipe'] });
      sendLog(ws, `Checked out candidate commit ${candidateSha.slice(0, 12)} (detached HEAD).\n`);
    } catch (e) {
      const detail = e instanceof Error ? e.message : String(e);
      sendLog(ws, `[ERROR] Could not check out candidate commit ${candidateSha}: ${detail}\n`);
      await persistAndPostStatus(taskId, {
        task_id: taskId,
        status: 'FAILED',
        artifact_type: 'ERROR_LOG',
        failure_category: 'clone_failed',
        logs: sanitize(
          `Qualifier smoketest aborted: could not check out the pinned ` +
          `candidate commit ${candidateSha}. The candidate must be the exact ` +
          `commit the release qualifier snapshotted; running the smoketest ` +
          `against a different commit would produce a meaningless signal. ` +
          `Detail: ${detail}`,
        ),
      });
      return;
    }
  }

  writePlaybookHelper(taskId);
  writeMessagesHelper(taskId);
  const disallowedTools = configureAgentPermissions('smoketest');

  const smoketestBuildConfig = (task.build_config ?? {}) as BuildConfig;
  const smoketestPythonDirs = (smoketestBuildConfig.directories ?? []).filter(d => d.type === 'python');
  const smoketestPyDir = smoketestPythonDirs.length > 0 ? smoketestPythonDirs[0].path.replace(/\/$/, '') : 'backend';
  const smoketestConfigPath = `${smoketestPyDir}/testing/smoketest.yaml`;

  const smoketestRetryContext = buildRetryContext(task);
  const smoketestFirstTaskSection = task.first_task_on_repo ? FIRST_TASK_GUIDANCE : '';
  const smoketestPrompt = `${smoketestRetryContext}You are a smoke test agent. Your job is to run structured health checks on this codebase using the smoketest configuration.

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

IMPORTANT: Always write /tmp/smoketest_result_${taskId}.json even if you cannot run any tests (use status "skip" with a message explaining why). Never leave the file unwritten.${smoketestFirstTaskSection}

${DEFAULT_TOOL_GUIDANCE}`;

  statusRef.value = 'Running smoke tests...';
  const smoketestFingerprint = computePromptFingerprint('smoketest', smoketestBuildConfig);
  setCurrentPromptFingerprint(smoketestFingerprint);
  sendHeartbeatSync(taskId, statusRef.value, { prompt_fingerprint: smoketestFingerprint });
  const { exitCode, allOutput } = await runClaudeCode(smoketestPrompt + PLAYBOOK_GUIDANCE, ws, statusRef, model, effort, disallowedTools);

  // Read result file written by Claude
  const smoketestResult = readHandlerOutput(taskId, 'smoketest_result', ws) as { passed: boolean; tests: Array<{ name: string; status: string; message: string }> } | null;

  if (exitCode === 0 && smoketestResult) {
    // Live endpoint checks
    const healthCheckUrls: string[] = Array.isArray(payload.health_check_urls)
      ? (payload.health_check_urls as string[])
      : [];
    // Filter URLs: only allow http(s), block metadata endpoints and internal IPs
    const safeUrls = healthCheckUrls.filter(u => {
      try {
        const parsed = new URL(u);
        if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return false;
        const host = parsed.hostname;
        if (host === '169.254.169.254' || host === 'metadata.google.internal') return false;
        if (host.startsWith('10.') || host.startsWith('192.168.') || host === 'localhost' || host === '127.0.0.1') return false;
        if (host.startsWith('172.') && parseInt(host.split('.')[1]) >= 16 && parseInt(host.split('.')[1]) <= 31) return false;
        return true;
      } catch { return false; }
    });
    if (safeUrls.length > 0) {
      sendLog(ws, '\nLive endpoint checks:\n');
      for (const url of safeUrls) {
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
      ...(!passed ? { failure_category: 'claude_code_failed' } : {}),
    });
  } else {
    await persistAndPostStatus(taskId, {
      task_id: taskId, status: 'FAILED', artifact_type: 'ERROR_LOG',
      logs: sanitize(allOutput.slice(-10_000)),
      failure_category: matchesRateLimit(allOutput) ? 'rate_limited' : 'claude_code_failed',
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
  const cloneUrl = `https://x-access-token:${ghToken}@github.com/${owner}/${repo}.git`;
  await prepareWorkspaceForRole({
    workspace: WORKSPACE, repoUrl: cloneUrl, githubToken: ghToken,
    branch: rawBaseBranch, baseBranch: rawBaseBranch, depth: 100,
  });
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
  const debugRetryContext = buildRetryContext(task);
  const debugFirstTaskSection = task.first_task_on_repo ? FIRST_TASK_GUIDANCE : '';

  const debugPrompt = `You are a debug analysis agent. Your job is to analyze a task failure and produce a structured root cause analysis. Do NOT make any code changes.
${claudeMd ? `\n## Codebase Instructions\n${claudeMd}\n` : ''}${debugRetryContext}${contextPrefix ? `\n${contextPrefix}` : ''}${fileTree ? `\n## Repository File Tree\n${fileTree}\n` : ''}
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
  const debugFingerprint = computePromptFingerprint('debug');
  setCurrentPromptFingerprint(debugFingerprint);
  sendHeartbeatSync(taskId, statusRef.value, { prompt_fingerprint: debugFingerprint });
  const debugParallelGuidance = isParallelAuditEnabled(task) ? PARALLEL_AUDIT_GUIDANCE : '';
  const { allOutput } = await runClaudeCode(debugPrompt + debugFirstTaskSection + debugParallelGuidance + PLAYBOOK_GUIDANCE + '\nNote: If your investigation revealed a root cause pattern, diagnostic technique, or system behavior that would help future debugging, save it to the playbook.\n' + `\n\n${DEFAULT_TOOL_GUIDANCE}`, ws, statusRef, model, effort, disallowedTools);

  const debugResult = readHandlerOutput(taskId, 'debug_report', ws) as { root_cause: string; affected_files: string[]; fix_prompt: string; confidence: string } | null;

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
    const debugLogs = sanitize(allOutput.slice(-10_000));
    await persistAndPostStatus(taskId, {
      task_id: taskId, status: 'FAILED', artifact_type: 'ERROR_LOG',
      logs: debugLogs,
      failure_category: matchesRateLimit(allOutput) ? 'rate_limited' : 'claude_code_failed',
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
  const cloneUrl = `https://x-access-token:${ghToken}@github.com/${owner}/${repo}.git`;
  await prepareWorkspaceForRole({
    workspace: WORKSPACE, repoUrl: cloneUrl, githubToken: ghToken,
    branch: baseBranch, baseBranch, depth: DEFAULT_CLONE_DEPTH,
  });
  writePlaybookHelper(taskId);
  writeMessagesHelper(taskId);
  const disallowedTools = configureAgentPermissions('perf');

  const perfBuildConfig = resolveBuildConfig(task);
  const perfDirs = perfBuildConfig?.directories ?? [];
  const perfPythonDirs = perfDirs.filter(d => d.type === 'python');
  const perfNodeDirs = perfDirs.filter(d => d.type === 'node');
  const perfPyDir = perfPythonDirs.length > 0 ? perfPythonDirs[0].path.replace(/\/$/, '') : 'backend';
  const perfNodeDir = perfNodeDirs.length > 0 ? perfNodeDirs[0].path.replace(/\/$/, '') : 'admin-ui';
  const perfRetryContext = buildRetryContext(task);
  const perfFirstTaskSection = task.first_task_on_repo ? FIRST_TASK_GUIDANCE : '';

  const perfPrompt = `${perfRetryContext}You are a performance analysis agent. You have full access to this repository. Your job is to analyze the codebase for demonstrable performance bottlenecks and produce structured, evidence-based findings.

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
  const perfFingerprint = computePromptFingerprint('perf', perfBuildConfig);
  setCurrentPromptFingerprint(perfFingerprint);
  sendHeartbeatSync(taskId, statusRef.value, { prompt_fingerprint: perfFingerprint });
  const perfParallelGuidance = isParallelAuditEnabled(task) ? PARALLEL_AUDIT_GUIDANCE : '';
  const { exitCode, allOutput, lastResultText } = await runClaudeCode(perfPrompt + perfTypeHint + perfFirstTaskSection + perfPerfSuffix + perfParallelGuidance + PLAYBOOK_GUIDANCE + `\n\n${DEFAULT_TOOL_GUIDANCE}`, ws, statusRef, model, effort, disallowedTools);

  const perfFindings = readHandlerOutput(taskId, 'perf_report', ws);
  let logs = perfFindings !== null ? sanitize(JSON.stringify(perfFindings)) : '';
  if (!logs) {
    if (lastResultText && lastResultText.includes('"severity"')) {
      logs = sanitize(lastResultText);
    } else {
      const findingsArr = extractJsonArrayDetailed(allOutput, 'severity');
      if (!findingsArr.ok) {
        sendLog(ws, `[WARNING] Could not extract performance findings JSON: ${describeJsonExtractionResult(findingsArr)}\n`);
      }
      logs = sanitize(findingsArr.ok ? JSON.stringify(findingsArr.value) : (lastResultText || allOutput.slice(-50_000)));
    }
  }

  await persistAndPostStatus(taskId, {
    task_id: taskId,
    status: exitCode === 0 ? 'COMPLETED' : 'FAILED',
    ...(exitCode !== 0 ? { artifact_type: 'ERROR_LOG', failure_category: 'claude_code_failed' } : { artifact_type: 'PERF_REPORT' }),
    logs,
  });
}

function formatRecentChanges(recentChanges: unknown): string {
  if (!recentChanges) return '';
  const text = typeof recentChanges === 'string' ? recentChanges.trim() : JSON.stringify(recentChanges, null, 2);
  return text ? `\n\nRecent changes:\n${text.slice(0, 4096)}` : '';
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
  // Match handleMigrate (line 133): a missing prompt produces a malformed
  // template "Issue to investigate:\n\nInstructions:..." that Claude can't
  // act on, leading to a 3-second silent failure (status PROVISIONING →
  // IN_PROGRESS → FAILED with "no JSON object with root_cause found in
  // transcript"). Fail loud instead so the orchestrator surfaces a real
  // diagnosis rather than retrying the same empty prompt 3×.
  if (!userPrompt) throw new Error('No prompt found in task payloads (investigate)');

  statusRef.value = `Cloning ${owner}/${repo} for investigation...`;
  sendLog(ws, `Starting investigation...\n`);
  const cloneUrl = `https://x-access-token:${ghToken}@github.com/${owner}/${repo}.git`;
  await prepareWorkspaceForRole({
    workspace: WORKSPACE, repoUrl: cloneUrl, githubToken: ghToken,
    branch: baseBranch, baseBranch, depth: DEFAULT_CLONE_DEPTH,
  });
  writePlaybookHelper(taskId);
  writeMessagesHelper(taskId);
  const disallowedTools = configureAgentPermissions('investigate');
  const investigateRetryContext = buildRetryContext(task);
  const investigateFirstTaskSection = task.first_task_on_repo ? FIRST_TASK_GUIDANCE : '';
  const recentChangesSection = formatRecentChanges(payload.recent_changes);

  const investigatePrompt = `${investigateRetryContext}You are an investigation agent. You have full read access to this repository. Your job is to analyze the issue described below and produce a structured diagnosis report.
Use the Recent changes section first to correlate the issue with the latest deploy window and recently merged changes.

Issue to investigate:
${userPrompt}${recentChangesSection}

Instructions:
1. Read relevant source files to understand the codebase and locate the root cause of the issue.
2. DO NOT make any changes to any files — this is a read-only analysis.
3. If the user prompt above frames this as a regression / signal trip / pipeline anomaly, FIRST triage whether what was observed is actually a real regression, or whether it is intended behavior, expected variance, or a known-incomplete pipeline step. When the prompt explicitly asks for a verdict, you MUST emit one.
4. Otherwise, identify the root cause, affected files, and a recommended fix.
5. Write your diagnosis as a JSON object to /tmp/investigation_report_${taskId}.json with EXACTLY this schema:

{
  "summary": "Brief summary of the issue and findings",
  "root_cause": "Detailed explanation of the root cause",
  "affected_files": [
    { "path": "path/to/file.ts", "issue": "Description of the problem in this file" }
  ],
  "recommended_fix": "Concrete description of what needs to be changed to fix the issue",
  "severity": "critical|high|medium|low",
  "confidence": "high|medium|low",
  "verdict": "real_regression|not_a_regression",
  "verdict_reason": "One to three sentences citing concrete evidence (file paths, design doc passages, prior commit SHAs) that justify the verdict. Required when verdict='not_a_regression', optional otherwise."
}

verdict guidance (only meaningful for regression / signal-trip prompts; default 'real_regression' if the prompt isn't framed that way):
- "real_regression": the observed behavior is a genuine bug, regression, or unintended degradation. Diagnose normally and populate root_cause / recommended_fix.
- "not_a_regression": set this when ANY of the following apply, and leave recommended_fix empty (do NOT fabricate a fix):
  * The observed behavior is intentional / by design (feature flag deliberately gating a step, recently merged change altered the baseline, design doc explicitly documents the behavior).
  * Sample size is too small for the prior/current delta to be statistically meaningful.
  * The 'regression' is just a known-incomplete pipeline step (e.g. the agent did not run the full backend test suite — that's documented design, not a bug).
  * The signal observed transient infrastructure noise (one-off VM crash, flaky external dep) with no code-side root cause.
  Your verdict_reason must cite concrete evidence — vague claims ("seems intentional") are rejected; the dispatcher will still suppress respawning for the TTL but operators will lose trust in the verdict.

Confidence scoring criteria (be honest — low confidence triggers follow-up investigation, medium/high triggers an automated remediation task):
- "high": You found the exact code location causing the failure with clear evidence — a specific function, wrong condition, missing check, or faulty expression directly explains the observed behavior. A fix can be written without further investigation.
- "medium": Strong hypothesis supported by code analysis, but some uncertainty remains — likely cause identified but you could not reproduce it, verify it end-to-end, or rule out other contributors.
- "low": Root cause unclear. Multiple plausible explanations, insufficient evidence in the code, or the failure could be environmental/transient. Prefer this over guessing.

recommended_fix guidance:
- Write it as a self-contained prompt for an implementing agent that will NOT see this investigation's full context. Be concrete and specific.
- Good: "Add a null check for \`user.profile\` in \`getUserDisplayName\` (src/user.ts:42) before accessing \`.name\`; return the fallback 'Anonymous' when profile is null."
- Bad: "Fix the error handling" / "Improve null safety" / "Handle the edge case".
- Name the file paths, function names, and exact conditions to change. Include the intended behavior, not just the symptom.

IMPORTANT:
- Write ONLY the JSON object to /tmp/investigation_report_${taskId}.json — no markdown, no code fences, no extra text.
- Do NOT modify any files in the repository.
- Base every claim on code you have actually read.`;

  statusRef.value = 'Running investigation...';
  const investigateFingerprint = computePromptFingerprint('investigate');
  setCurrentPromptFingerprint(investigateFingerprint);
  sendHeartbeatSync(taskId, statusRef.value, { prompt_fingerprint: investigateFingerprint });
  const investigateParallelGuidance = isParallelAuditEnabled(task) ? PARALLEL_AUDIT_GUIDANCE : '';
  const { exitCode, allOutput, lastResultText } = await runClaudeCode(investigatePrompt + investigateFirstTaskSection + investigateParallelGuidance + PLAYBOOK_GUIDANCE + '\nNote: If your investigation revealed a root cause pattern, diagnostic technique, or system behavior that would help future debugging, save it to the playbook.\n' + `\n\n${DEFAULT_TOOL_GUIDANCE}`, ws, statusRef, model, effort, disallowedTools);

  let investigationResult = readHandlerOutput(taskId, 'investigation_report', ws);
  if (investigationResult === null) {
    const requiredFields = ['summary', 'root_cause', 'affected_files', 'recommended_fix', 'severity', 'confidence'];

    // Try lastResultText first (cheaper — usually the final message)
    if (lastResultText && lastResultText.includes('"root_cause"')) {
      try {
        const res = extractJsonObjectDetailed(lastResultText, 'root_cause', 'recommended_fix');
        if (res.ok && requiredFields.every(k => k in (res.value))) {
          investigationResult = res.value;
          sendLog(ws, `[WARNING] Investigation JSON recovered from lastResultText transcript rather than file.\n`);
        }
      } catch (e) {
        // JsonExtractionError: candidate found but JSON.parse failed (e.g. malformed output).
        // Fall through to allOutput scan below.
        sendLog(ws, `[WARNING] lastResultText JSON extraction failed: ${e instanceof JsonExtractionError ? e.message : String(e)}\n`);
      }
    }

    // Fall back to full transcript
    if (investigationResult === null) {
      try {
        const res = extractJsonObjectDetailed(allOutput, 'root_cause', 'recommended_fix');
        if (res.ok && requiredFields.every(k => k in (res.value))) {
          investigationResult = res.value;
          sendLog(ws, `[WARNING] Investigation JSON recovered from allOutput transcript rather than file.\n`);
        }
      } catch (e) {
        // JsonExtractionError: malformed JSON candidate found in transcript — treat as not found.
        sendLog(ws, `[WARNING] allOutput JSON extraction failed: ${e instanceof JsonExtractionError ? e.message : String(e)}\n`);
      }
    }

    if (investigationResult === null) {
      // Surface the real Claude exit code + transcript tail so the orchestrator
      // can distinguish "Claude crashed" (3-second deaths with empty allOutput)
      // from "Claude ran but didn't write the report" (exitCode 0, transcript
      // exists, just no JSON). The bare "Failed to read investigation report"
      // message previously hid both modes.
      const tail = (allOutput || '').slice(-2000);
      const lastResultTail = (lastResultText || '').slice(-500);
      const logsBlob =
        `Failed to read investigation report from /tmp/investigation_report_${taskId}.json; ` +
        `no JSON object with root_cause found in transcript ` +
        `(exitCode=${exitCode}, allOutput=${(allOutput || '').length}b, ` +
        `lastResultText=${(lastResultText || '').length}b)\n` +
        `--- lastResultText tail ---\n${lastResultTail}\n` +
        `--- allOutput tail ---\n${tail}`;
      const rateLimitDetected = matchesRateLimit((allOutput || '') + '\n' + (lastResultText || ''));
      await persistAndPostStatus(taskId, {
        task_id: taskId,
        status: 'FAILED',
        artifact_type: 'ERROR_LOG',
        logs: logsBlob,
        failure_category: rateLimitDetected ? 'rate_limited' : 'parse_failed',
      });
      return;
    }
  }

  const logs = sanitize(JSON.stringify(investigationResult));
  await persistAndPostStatus(taskId, {
    task_id: taskId,
    status: exitCode === 0 ? 'COMPLETED' : 'FAILED',
    ...(exitCode !== 0 ? { artifact_type: 'ERROR_LOG', failure_category: 'claude_code_failed' } : { artifact_type: 'INVESTIGATION_REPORT' }),
    logs,
  });
}

// ── Handler: Self Audit (score system maturity, output findings JSON) ────────
export async function handleSelfAudit(
  taskId: string, payload: Record<string, unknown>, task: Record<string, unknown>,
  ghToken: string, owner: string, repo: string, ws: WebSocket | null, statusRef: { value: string },
  model?: string, effort?: string
) {
  const rawBaseBranch = strVal(payload.base_branch, 'main');
  const baseBranch = validateBranchName(rawBaseBranch, 'base_branch');

  statusRef.value = `Cloning ${owner}/${repo} for self audit...`;
  sendLog(ws, `Starting self audit...\n`);
  const cloneUrl = `https://x-access-token:${ghToken}@github.com/${owner}/${repo}.git`;
  await prepareWorkspaceForRole({
    workspace: WORKSPACE, repoUrl: cloneUrl, githubToken: ghToken,
    branch: baseBranch, baseBranch, depth: DEFAULT_CLONE_DEPTH,
  });
  writePlaybookHelper(taskId);
  writeMessagesHelper(taskId);
  const disallowedTools = configureAgentPermissions('self_audit');

  const contextPrefix = buildContextPrefix(task);
  const claudeMd = readClaudeMd();
  const codebaseSummary = claudeMd ? `## Codebase Instructions\n${claudeMd}\n\n` : '';
  const fileTreeSection = buildFileTree(ws);
  const playbookSection = formatPlaybook(task.playbook as Record<string, PlaybookEntry | string> | undefined);
  const retryContext = buildRetryContext(task);
  const firstTaskSection = task.first_task_on_repo ? FIRST_TASK_GUIDANCE : '';

  const focusSystemsRaw = Array.isArray(payload.focus_systems) ? (payload.focus_systems as unknown[]) : [];
  const focusSystems = focusSystemsRaw.filter((s): s is string => typeof s === 'string' && s.length > 0);
  const maxSystemsRaw = typeof payload.max_systems === 'number' && Number.isFinite(payload.max_systems)
    ? Math.floor(payload.max_systems)
    : 3;
  const maxSystems = Math.max(1, Math.min(maxSystemsRaw, 10));

  const focusSection = focusSystems.length > 0
    ? `Audit ONLY these systems (use these exact system_id values in output): ${JSON.stringify(focusSystems)}\n\n`
    : `No focus_systems provided. Pick up to ${maxSystems} systems to audit by reading the project design_doc and direction system_id values (round-robin across systems not recently audited). Use the discovered system_id verbatim in output.\n\n`;

  const taskCtx = task.context && typeof task.context === 'object' ? task.context as Record<string, unknown> : {};
  const driftCtxRaw = taskCtx.drift_context;
  let driftSection = '';
  if (driftCtxRaw && typeof driftCtxRaw === 'object') {
    const entries = Object.entries(driftCtxRaw as Record<string, unknown>);
    if (entries.length > 0) {
      const fmt = (v: unknown, fallback: string): string => {
        if (v === undefined || v === null) return fallback;
        if (typeof v === 'string' || typeof v === 'number' || typeof v === 'boolean') return String(v);
        return JSON.stringify(v);
      };
      const lines = entries.map(([sid, sig]) => {
        const s = sig as Record<string, unknown>;
        return `  - ${sid}: claimed=${fmt(s.claimed_changes_count, '?')}, merged=${fmt(s.actually_merged_prs, '?')}, consistency=${fmt(s.claim_shipped_consistency, '?')}, over_claim=${fmt(s.over_claim_flag, 'false')}, stalled=${fmt(s.stalled_despite_churn_flag, 'false')}`;
      });
      driftSection = `## Drift Scanner Findings\nThe following systems were flagged by the drift scanner before this audit was dispatched. For each, reconcile what the pipeline *claimed* to ship vs what was *actually merged*:\n${lines.join('\n')}\n\n`;
    }
  }

  const selfAuditPrompt = `${codebaseSummary}${playbookSection}${retryContext}${contextPrefix}${fileTreeSection}${driftSection}${focusSection}You are a self-audit agent. You have full read access to this repository. Your job is to score the maturity of one or more systems on four objective signals and output a structured findings report.

For each system, score four components (each 0-100, where 100 is healthy and 0 is broken):

1. **test_coverage** — Are there tests covering the system's core paths? Look at tests directories, count test files mentioning the system, check whether key modules have direct test coverage. Low score: little or no test coverage. High score: each module has direct, runnable tests.

2. **friction_density** — How often does work in this system get stuck? Inspect recent task history (titles, status_log, retry counts in payloads/context) and recent git history. Low score: many recurring failures, retries, or aborts in this area. High score: tasks in this system flow through without recurring blockers.

3. **claim_consistency** — Does what shipped actually match what was claimed? Compare recent task summaries / PR descriptions against the diffs that were merged. Low score: PR titles/summaries do not match what the diff actually does (drift). High score: claims and shipped diffs are consistent.

4. **failure_mix** — What is the failure-class mix in this area? Look at recent FAILED tasks scoped to this system. Low score: heavy CODE_ERROR / parse_failed / auto-remediation churn. High score: most failures are recoverable / transient.

Then assign an overall \`score\` (0-100) and a \`recommended_mode\` based on the worst components:
- \`audit\` — score < 40 in any component, system is silently broken; pause feature work and investigate
- \`fundamentals\` — score 40-70, system is functioning but accumulating friction; prioritize structural cleanup over new features
- \`ship\` — score >= 70, system is healthy; continue feature work
- \`pause\` — pipeline-wide hold (use sparingly when the audit cannot make a confident assessment)

Output a single JSON object as the VERY LAST thing in your response with this exact shape:

{
  "findings": [
    {
      "system_id": "exact-system-id-from-design-doc",
      "score": 72,
      "components": {
        "test_coverage": 80,
        "friction_density": 65,
        "claim_consistency": 75,
        "failure_mix": 70
      },
      "top_issues": [
        "One short sentence per issue, max 5 issues"
      ],
      "recommended_mode": "fundamentals",
      "evidence": [
        {"file": "path/to/file.py", "summary": "What this file shows about the system"}
      ]
    }
  ]
}

Rules:
- Every score MUST be an integer in [0, 100]
- recommended_mode MUST be one of: audit, fundamentals, ship, pause
- Limit findings to ${maxSystems} systems unless focus_systems was provided (then use all focus_systems)
- Each finding must cite at least one evidence file from the actual repo
- IMPORTANT: Write the JSON object to /tmp/self_audit_findings_${taskId}.json (NOT in the repository). The file should contain ONLY the JSON object, no markdown wrapping.
- Also output the same JSON object as the final output in your logs.`;

  const buildConfig = resolveBuildConfig(task);
  const promptSuffix = payload.prompt_suffix ? `\n\nAdditional instructions:\n${strVal(payload.prompt_suffix)}` : '';
  const typeHints = getTaskTypeHints(buildConfig);
  const typeHint = typeHints['self_audit'] ? '\n\n' + typeHints['self_audit'] : '';
  statusRef.value = 'Running self audit...';
  const selfAuditFingerprint = computePromptFingerprint('self_audit', buildConfig);
  setCurrentPromptFingerprint(selfAuditFingerprint);
  sendHeartbeatSync(taskId, statusRef.value, { prompt_fingerprint: selfAuditFingerprint });
  const selfAuditParallelGuidance = isParallelAuditEnabled(task) ? PARALLEL_AUDIT_GUIDANCE : '';
  const { exitCode, allOutput, lastResultText } = await runClaudeCode(selfAuditPrompt + typeHint + firstTaskSection + promptSuffix + selfAuditParallelGuidance + PLAYBOOK_GUIDANCE + `\n\n${DEFAULT_TOOL_GUIDANCE}`, ws, statusRef, model, effort, disallowedTools);

  // Parse findings: prefer structured file output, then a {findings:[...]} object
  // anywhere in the output, then a bare array of findings (wrapped on our side).
  const handlerOutput = readHandlerOutput(taskId, 'self_audit_findings', ws) as { findings?: unknown[] } | unknown[] | null;
  let logs = '';
  if (handlerOutput !== null) {
    logs = sanitize(JSON.stringify(handlerOutput));
  } else {
    const haystack = (allOutput || '') + '\n' + (lastResultText || '');
    const objExtract = extractJsonObjectDetailed(haystack, 'findings', 'system_id');
    if (objExtract.ok) {
      logs = sanitize(JSON.stringify(objExtract.value));
    } else {
      const arrExtract = extractJsonArrayDetailed(haystack, 'system_id');
      if (arrExtract.ok) {
        logs = sanitize(JSON.stringify({ findings: arrExtract.value }));
      } else {
        sendLog(ws, `[WARNING] Could not extract self audit findings JSON: ${describeJsonExtractionResult(objExtract)}\n`);
        logs = sanitize(lastResultText || allOutput.slice(-50_000));
      }
    }
  }

  await persistAndPostStatus(taskId, {
    task_id: taskId,
    status: exitCode === 0 ? 'COMPLETED' : 'FAILED',
    ...(exitCode !== 0 ? { artifact_type: 'ERROR_LOG', failure_category: 'claude_code_failed' } : { artifact_type: 'SELF_AUDIT_REPORT' }),
    logs,
  });
}

// ── Handler: Release Audit (review main...<candidate_sha> diff against main's standards) ──
export async function handleReleaseAudit(
  taskId: string, payload: Record<string, unknown>, task: Record<string, unknown>,
  ghToken: string, owner: string, repo: string, ws: WebSocket | null, statusRef: { value: string },
  model?: string, effort?: string
) {
  // CRITICAL: always clone base_branch (main), never automain. The audit must
  // judge the incoming changes by main's standards, not the new code's standards.
  const rawBaseBranch = strVal(payload.base_branch, 'main');
  const baseBranch = validateBranchName(rawBaseBranch, 'base_branch');

  // CRITICAL (C2/C4): the qualifier must score one PINNED candidate commit, not
  // live `automain`. The bare branch name `automain` does NOT resolve as a
  // revision after cloning `main` — `git fetch origin automain` only updates
  // FETCH_HEAD, never a local `automain` ref — so a `main...automain` range can
  // never resolve and the audit would ALWAYS fail "diff_range_unresolvable".
  // The backend pins the candidate by passing a full 40-char `candidate_sha`
  // (automain HEAD at snapshot time) in the task payload; we fetch that exact
  // commit object and diff `main...<candidate_sha>`, which always resolves.
  const candidateSha = resolveCandidateSha(payload, task);
  if (!candidateSha) {
    // No pinned candidate ⇒ nothing safe to audit. Fail loud rather than fall
    // back to the bare `automain` name (which cannot resolve) or to live HEAD
    // (which is not the snapshotted candidate). A missing candidate_sha is a
    // real contract bug between the qualifier and the agent.
    const rawSeen = strVal(payload.candidate_sha) || strVal(
      (task.context && typeof task.context === 'object' && !Array.isArray(task.context)
        ? (task.context as Record<string, unknown>).candidate_sha
        : undefined),
    );
    sendLog(ws, `[ERROR] Release audit aborted: no valid candidate_sha in task payload.\n`);
    await persistAndPostStatus(taskId, {
      task_id: taskId,
      status: 'FAILED',
      artifact_type: 'ERROR_LOG',
      failure_category: 'diff_range_unresolvable',
      logs: sanitize(
        `Release audit aborted: the task payload carries no valid candidate_sha. ` +
        `The release qualifier must pin the candidate commit by passing a ` +
        `full 40-char commit SHA as payload.candidate_sha. ` +
        `Without it there is no commit to diff against ${baseBranch}. ` +
        `Saw: ${rawSeen ? `"${rawSeen}"` : '(absent)'}`,
      ),
    });
    return;
  }
  // Three-dot (merge-base) diff of the pinned candidate vs main.
  const diffRange = `${baseBranch}...${candidateSha}`;

  statusRef.value = `Cloning ${owner}/${repo} (${baseBranch}) for release audit...`;
  sendLog(ws, `Starting release audit — cloning ${baseBranch} (reference branch), candidate ${candidateSha.slice(0, 12)}...\n`);
  const cloneUrl = `https://x-access-token:${ghToken}@github.com/${owner}/${repo}.git`;
  // CRITICAL: a release audit runs `git diff main...<candidate_sha>` (three-dot
  // = merge-base diff). On an LTS integration trunk the candidate routinely
  // diverges from `main` by far more than DEFAULT_CLONE_DEPTH commits, so a
  // shallow clone leaves the merge-base commit unreachable and `git diff`
  // either errors or produces a wrong diff. A FULL clone (depth 0) is the only
  // safe option — the merge-base must be reachable for the three-dot diff.
  await prepareWorkspaceForRole({
    workspace: WORKSPACE, repoUrl: cloneUrl, githubToken: ghToken,
    branch: baseBranch, baseBranch, depth: 0,
  });

  // Fetch the EXACT candidate commit so git diff can compute the merge-base
  // diff — but do NOT check it out. The working tree stays on baseBranch
  // (main). GitHub permits fetching a SHA reachable from a ref, and the
  // candidate_sha is `automain` HEAD at snapshot time, so it is reachable.
  // After the fetch the SHA is a local object and `main...<candidate_sha>`
  // resolves. (`git fetch origin <sha>` works where `git fetch origin
  // automain` does not: a SHA fetch leaves a real commit object behind, a
  // bare-branch fetch only updates FETCH_HEAD.)
  let fetchOk = false;
  let fetchFailDetail = '';
  sendLog(ws, `Fetching candidate commit ${candidateSha.slice(0, 12)} (full history) for diff computation (staying on ${baseBranch})...\n`);
  try {
    execFileSync('git', ['fetch', 'origin', candidateSha], { cwd: WORKSPACE, stdio: ['ignore', 'pipe', 'pipe'] });
    fetchOk = true;
    sendLog(ws, `Fetched candidate commit ${candidateSha.slice(0, 12)} successfully.\n`);
  } catch (e) {
    fetchFailDetail = e instanceof Error ? e.message : String(e);
    sendLog(ws, `[WARNING] git fetch origin ${candidateSha.slice(0, 12)} failed: ${fetchFailDetail}\n`);
  }

  // Pre-flight: verify the diff range actually resolves before invoking the
  // agent. A successful-but-incomplete fetch can still leave the merge-base
  // unreachable; if it is, `git diff ${diffRange}` errors and the agent can
  // misread that as an "empty diff" and emit audit_score: 1.0 — a FALSE
  // strong-promote signal to the release qualifier. Fail loud instead.
  let diffRangeResolves = false;
  let diffRangeFailDetail = '';
  if (fetchOk) {
    try {
      // `git rev-parse --verify <sha>^{commit}` errors when the SHA is not a
      // reachable commit object; `git rev-list --count main...<sha>` errors
      // when either endpoint or the merge-base is unreachable. Run both so a
      // genuinely unresolvable range fails loud, while a clean exit proves the
      // symmetric-difference range is fully resolvable.
      git(['rev-parse', '--verify', `${candidateSha}^{commit}`]);
      git(['rev-list', '--count', diffRange]);
      diffRangeResolves = true;
      sendLog(ws, `Diff range ${diffRange} resolves — merge-base is reachable.\n`);
    } catch (e) {
      diffRangeFailDetail = e instanceof Error ? e.message : String(e);
      sendLog(ws, `[ERROR] Diff range ${diffRange} does not resolve: ${diffRangeFailDetail}\n`);
    }
  } else {
    diffRangeFailDetail = `git fetch origin ${candidateSha} failed (${fetchFailDetail || 'no detail'}); ${diffRange} cannot be resolved`;
  }

  if (!diffRangeResolves) {
    // Never let the agent run an unaudited audit. A diff range that does not
    // resolve makes every downstream audit_score meaningless — post a FAILED
    // status with a clear failure category so the qualifier records a blocker
    // rather than a false audit_score: 1.0.
    sendLog(ws, `Aborting release audit: cannot compute ${diffRange} — refusing to emit an unaudited audit_score.\n`);
    await persistAndPostStatus(taskId, {
      task_id: taskId,
      status: 'FAILED',
      artifact_type: 'ERROR_LOG',
      failure_category: 'diff_range_unresolvable',
      logs: sanitize(
        `Release audit aborted: diff range ${diffRange} does not resolve. ` +
        `The candidate commit ${candidateSha} could not be fetched, or the ` +
        `merge-base of ${baseBranch} and the candidate is unreachable, so ` +
        `git diff would error or produce a wrong diff. ` +
        `Detail: ${diffRangeFailDetail || '(no detail)'}`,
      ),
    });
    return;
  }

  // Deterministically compute test erosion from the actual diff so the
  // hard-blocker (deleted test files) and sub-score deltas are not dependent
  // on the LLM spotting every change in a potentially large diff.
  type ComputedTestErosion = {
    deleted_test_files: string[];
    assertion_delta: number;
    test_count_delta: number;
  };
  let computedTestErosion: ComputedTestErosion | null = null;
  try {
    const TEST_PATH_RE = /\/(testing|tests|__tests__)\/|\.test\.|\.spec\./;

    const nameStatus = execFileSync('git', ['diff', '--name-status', diffRange], {
      cwd: WORKSPACE, encoding: 'utf-8', stdio: ['ignore', 'pipe', 'pipe'],
    });
    const deletedTestFiles = nameStatus
      .split('\n')
      .filter((line: string) => line.startsWith('D\t'))
      .map((line: string) => line.slice(2).trim())
      .filter((p: string) => TEST_PATH_RE.test(p));

    const diffText = execFileSync('git', ['diff', diffRange], {
      cwd: WORKSPACE, encoding: 'utf-8', stdio: ['ignore', 'pipe', 'pipe'],
      maxBuffer: 50 * 1024 * 1024,
    });

    const ASSERTION_RE = /\b(assert[\s(]|expect\(|assertEqual|assertRaises|it\(|describe\()/;
    const TEST_FN_RE = /\b(def test_|test\(|it\(|describe\()/;

    let assertionDelta = 0;
    let testCountDelta = 0;
    for (const line of diffText.split('\n')) {
      if (line.startsWith('+') && !line.startsWith('+++')) {
        if (ASSERTION_RE.test(line)) assertionDelta++;
        if (TEST_FN_RE.test(line)) testCountDelta++;
      } else if (line.startsWith('-') && !line.startsWith('---')) {
        if (ASSERTION_RE.test(line)) assertionDelta--;
        if (TEST_FN_RE.test(line)) testCountDelta--;
      }
    }

    computedTestErosion = {
      deleted_test_files: deletedTestFiles,
      assertion_delta: assertionDelta,
      test_count_delta: testCountDelta,
    };
    sendLog(ws, `[AUDIT] Computed test erosion: ${deletedTestFiles.length} deleted test file(s), assertion_delta=${assertionDelta}, test_count_delta=${testCountDelta}\n`);
  } catch (e) {
    sendLog(ws, `[WARNING] Could not compute deterministic test erosion: ${e instanceof Error ? e.message : String(e)}\n`);
  }

  writePlaybookHelper(taskId);
  writeMessagesHelper(taskId);
  const disallowedTools = configureAgentPermissions('release_audit');

  const contextPrefix = buildContextPrefix(task);
  const claudeMd = readClaudeMd();
  const codebaseSummary = claudeMd ? `## Codebase Instructions\n${claudeMd}\n\n` : '';
  const fileTreeSection = buildFileTree(ws);
  const playbookSection = formatPlaybook(task.playbook as Record<string, PlaybookEntry | string> | undefined);
  const retryContext = buildRetryContext(task);
  const firstTaskSection = task.first_task_on_repo ? FIRST_TASK_GUIDANCE : '';

  const releaseAuditPrompt = `${codebaseSummary}${playbookSection}${retryContext}${contextPrefix}${fileTreeSection}You are a release audit agent. You have been cloned onto \`${baseBranch}\` — the last known-good LTS branch — and your job is to review the incoming diff range \`${diffRange}\` against \`${baseBranch}\`'s standards. \`${candidateSha}\` is the PINNED release candidate commit (a snapshot of the integration branch); it has already been fetched into this clone, so the diff range resolves.

IMPORTANT: You are running on \`${baseBranch}\` intentionally. You must judge the incoming changes by \`${baseBranch}\`'s standards, NOT by the new code's standards. This asymmetry is the entire point of the audit.

## Your task

1. **Obtain the diff.** Run: \`git diff ${diffRange}\` to see what the candidate commit \`${candidateSha}\` adds relative to \`${baseBranch}\`. Also run \`git diff --stat ${diffRange}\` for a file summary and \`git log ${diffRange} --oneline\` for the commit list. Do NOT use the bare name \`automain\` — it is not a resolvable revision in this clone; always diff against the pinned SHA \`${candidateSha}\`.

2. **Assess test erosion.** Identify:
   - Any test files deleted in the diff (files under \`testing/\`, \`tests/\`, \`__tests__/\`, \`*.test.*\`, \`*.spec.*\` that appear as deleted).
   - Net change in assertion count: search for \`assert \`, \`expect(\`, \`assertEqual\`, \`assertRaises\`, \`it(\`, \`describe(\` in the diff's added/removed lines.
   - Net change in test function/case count: count added vs removed \`def test_\`, \`test(\`, \`it(\`, \`describe(\` patterns.

3. **Review code quality** against \`${baseBranch}\`'s conventions (read CLAUDE.md for standards). Focus on:
   - Correctness and logic errors introduced
   - Security regressions (new hardcoded secrets, unsafe operations, auth bypasses)
   - Architecture drift (patterns inconsistent with the existing codebase conventions)
   - Test coverage gaps for new behavior
   - Documentation / comment accuracy

4. **Score and emit results.** Write your findings as a JSON object to \`/tmp/release_audit_report_${taskId}.json\` with EXACTLY this structure:

\`\`\`json
{
  "audit_score": 0.85,
  "test_erosion": {
    "deleted_test_files": [],
    "assertion_delta": 12,
    "test_count_delta": 3
  },
  "findings": [
    {
      "severity": "high",
      "summary": "One sentence describing the issue",
      "file": "path/to/affected/file.py"
    }
  ]
}
\`\`\`

Field semantics:
- \`audit_score\`: float [0, 1] — holistic cross-check of your overall assessment (1.0 = no issues found, 0.0 = critical problems). The backend derives the authoritative gating score from the \`findings\` list, so focus on producing accurate, well-categorised findings rather than computing this number precisely.
- \`test_erosion.deleted_test_files\`: list of test file paths deleted in the diff. An empty list is good.
- \`test_erosion.assertion_delta\`: integer — positive means more assertions added than removed (good), negative means erosion.
- \`test_erosion.test_count_delta\`: integer — positive means more tests added than removed (good), negative means erosion.
- \`findings\`: each entry has \`severity\` (critical|high|medium|low), \`summary\` (one sentence), and \`file\` (file path or empty string if cross-cutting).

Rules:
- Read the actual diff — do NOT speculate about code you haven't read.
- If \`git diff ${diffRange}\` produces no output (empty diff), set \`audit_score: 1.0\`, \`findings: []\`, and note it in a single low-severity finding.
- IMPORTANT: Write ONLY the JSON object to \`/tmp/release_audit_report_${taskId}.json\` — no markdown, no code fences, no extra text.
- Also output the same JSON object as the final output in your logs.
- Do NOT modify any files in the repository.`;

  const buildConfig = resolveBuildConfig(task);
  const promptSuffix = payload.prompt_suffix ? `\n\nAdditional instructions:\n${strVal(payload.prompt_suffix)}` : '';
  const typeHints = getTaskTypeHints(buildConfig);
  const typeHint = typeHints['release_audit'] ? '\n\n' + typeHints['release_audit'] : '';
  statusRef.value = 'Running release audit...';
  const releaseAuditFingerprint = computePromptFingerprint('release_audit', buildConfig);
  setCurrentPromptFingerprint(releaseAuditFingerprint);
  sendHeartbeatSync(taskId, statusRef.value, { prompt_fingerprint: releaseAuditFingerprint });
  const releaseAuditParallelGuidance = isParallelAuditEnabled(task) ? PARALLEL_AUDIT_GUIDANCE : '';
  const { exitCode, allOutput, lastResultText } = await runClaudeCode(
    releaseAuditPrompt + typeHint + firstTaskSection + promptSuffix + releaseAuditParallelGuidance + PLAYBOOK_GUIDANCE + `\n\n${DEFAULT_TOOL_GUIDANCE}`,
    ws, statusRef, model, effort, disallowedTools,
  );

  const handlerOutput = readHandlerOutput(taskId, 'release_audit_report', ws) as {
    audit_score?: number;
    test_erosion?: { deleted_test_files?: unknown[]; assertion_delta?: number; test_count_delta?: number };
    computed_test_erosion?: { deleted_test_files: string[]; assertion_delta: number; test_count_delta: number };
    findings?: unknown[];
  } | null;

  // Inject computed_test_erosion into the artifact so the backend can use it
  // as the authoritative signal rather than the LLM's self-reported test_erosion.
  const enrichedOutput = handlerOutput !== null && computedTestErosion !== null
    ? { ...handlerOutput, computed_test_erosion: computedTestErosion }
    : handlerOutput;

  let logs = '';
  if (enrichedOutput !== null) {
    logs = sanitize(JSON.stringify(enrichedOutput));
  } else {
    const haystack = (allOutput || '') + '\n' + (lastResultText || '');
    const objExtract = extractJsonObjectDetailed(haystack, 'audit_score', 'findings');
    if (objExtract.ok) {
      const enrichedExtracted = computedTestErosion !== null
        ? { ...objExtract.value, computed_test_erosion: computedTestErosion }
        : objExtract.value;
      logs = sanitize(JSON.stringify(enrichedExtracted));
    } else {
      sendLog(ws, `[WARNING] Could not extract release audit JSON: ${describeJsonExtractionResult(objExtract)}\n`);
      logs = sanitize(lastResultText || allOutput.slice(-50_000));
    }
  }

  await persistAndPostStatus(taskId, {
    task_id: taskId,
    status: exitCode === 0 ? 'COMPLETED' : 'FAILED',
    ...(exitCode !== 0 ? { artifact_type: 'ERROR_LOG', failure_category: 'claude_code_failed' } : { artifact_type: 'RELEASE_AUDIT_REPORT' }),
    logs,
  });
}
