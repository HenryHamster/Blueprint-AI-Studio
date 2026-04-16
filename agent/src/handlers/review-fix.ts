import fs from 'fs';
import { execFileSync } from 'child_process';
import type WebSocket from 'ws';
import {
  WORKSPACE, strVal, sanitize, sendLog, validateBranchName,
  persistAndPostStatus, apiGet, git, gitAsync, cloneWithRetry,
  runClaudeCode, runProjectLintChecks, lintFixLoop, testFixLoop,
  sendHeartbeatSync,
} from '../index.js';
import type { BuildConfig } from '../index.js';
import { DEFAULT_CLONE_DEPTH } from '../constants.js';
import {
  TASK_TYPE_HINTS, PLAYBOOK_GUIDANCE, FIRST_TASK_GUIDANCE, DEFAULT_TOOL_GUIDANCE, readClaudeMd, formatProjectContext, configureAgentPermissions,
  writePlaybookHelper, writeMessagesHelper, formatPlaybook, buildRetryContext, discoverRelatedTests,
} from '../prompt.js';
import { resolveBuildConfig } from '../build-config.js';

// ── Build instruction generator for review prompts ───────────────────────────
function buildTypeCheckInstructions(buildConfig?: BuildConfig): string {
  if (!buildConfig || !buildConfig.directories || buildConfig.directories.length === 0) {
    return `**Build & Type Safety (MUST run these — do not skip):**
- For frontend changes (admin-ui/): run \`cd admin-ui && npm ci && npm run build\` — report any errors
- For agent changes (agent/): run \`cd agent && npm ci && npx tsc --noEmit\` — report any type errors
- For backend changes (backend/): run \`cd backend && python -m py_compile app/main.py\` or check imports
- For ANY code changes: run the relevant build command above and include results in your verdict. A build failure is an automatic REQUEST_CHANGES.`;
  }
  const checks = buildConfig.directories
    .filter(d => d.build_cmd || d.lint_cmd)
    .map(d => {
      const cmd = d.build_cmd || d.lint_cmd;
      return `- For changes in \`${d.path}/\`: run \`${cmd}\` — report any errors`;
    });
  if (checks.length === 0) {
    return '**Build & Type Safety:** No build commands configured — review code manually for type correctness.';
  }
  return `**Build & Type Safety (MUST run these — do not skip):**
${checks.join('\n')}
- For ANY code changes: run the relevant command above and include results in your verdict. A build failure is an automatic REQUEST_CHANGES.`;
}

// ── Handler: Review (review a PR, output verdict) ───────────────────────────
export async function handleReview(
  taskId: string, payload: Record<string, unknown>, task: Record<string, unknown>,
  ghToken: string, owner: string, repo: string, ws: WebSocket | null, statusRef: { value: string },
  model?: string, effort?: string
) {
  const prNumber = Number(payload.pr_number);
  if (!Number.isInteger(prNumber) || prNumber <= 0) throw new Error(`Invalid pr_number: ${String(payload.pr_number)}`);
  const branch = validateBranchName(strVal(payload.branch), 'branch');
  const baseBranch = validateBranchName(strVal(payload.base_branch, 'main'), 'base_branch');
  const originalPrompt = strVal(payload.original_prompt);

  // Read review iteration from task context
  const taskContext = task.context as Record<string, unknown> | undefined;
  const reviewLoop = taskContext?.review_loop as Record<string, unknown> | undefined;
  const iteration = (reviewLoop?.iteration as number | undefined) || 1;
  const reviewHistory = (reviewLoop?.review_history as Array<{ iteration: number; feedback: string; issues?: Array<{severity: string; description: string}> }> | undefined) || [];
  const taskScope = (taskContext?.scope as string | undefined) || 'medium';
  const origTaskType = strVal(payload.original_task_type || '');

  // Clone the full repo so Claude can explore everything, then checkout the PR branch
  statusRef.value = `Cloning ${owner}/${repo} (PR #${prNumber})...`;
  sendLog(ws, `Reviewing PR #${prNumber} on branch ${branch} (iteration ${iteration})\n`);
  execFileSync('rm', ['-rf', WORKSPACE], { encoding: 'utf-8' });
  const cloneUrl = `https://x-access-token:${ghToken}@github.com/${owner}/${repo}.git`;
  // Shallow clone of the PR branch directly — avoids fetch+checkout issues with --single-branch
  await cloneWithRetry(['clone', '--depth', String(DEFAULT_CLONE_DEPTH), '--branch', branch, cloneUrl, WORKSPACE]);
  writePlaybookHelper(taskId);
  writeMessagesHelper(taskId);
  const disallowedTools = configureAgentPermissions('review');
  const reviewBuildConfig = resolveBuildConfig(task);

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

  // Read CLAUDE.md for codebase instructions
  const reviewClaudeMd = readClaudeMd();
  const reviewClaudeMdPrefix = reviewClaudeMd
    ? `## Codebase Instructions\n${reviewClaudeMd}\n\nCheck that the changes respect the File Restrictions listed above. Changes to restricted files should result in REQUEST_CHANGES.\n\n`
    : '';

  // Read project_context for active/sibling task awareness
  const reviewProjectCtx = task.project_context as Record<string, unknown> | undefined;
  const reviewProjectCtxSection = reviewProjectCtx ? formatProjectContext(reviewProjectCtx) + '\n\n' : '';

  // Build prior review history section (for iterations > 1)
  const reviewHistorySection = reviewHistory.length > 0 && iteration > 1
    ? `Issues from previous iterations (verify each was addressed):\n${reviewHistory.map(e => {
        if (e.issues && e.issues.length > 0) {
          const critical = e.issues.filter(i => i.severity === 'critical').map(i => '  [CRITICAL] ' + i.description);
          const major = e.issues.filter(i => i.severity === 'major').map(i => '  [MAJOR] ' + i.description);
          const minor = e.issues.filter(i => i.severity === 'minor').map(i => '  [MINOR] ' + i.description);
          return `- Iteration ${e.iteration}:\n` + [...critical, ...major, ...minor].join('\n');
        }
        return `- Iteration ${e.iteration}: ` + e.feedback.slice(0, 800);
      }).join('\n')}\n\n`
    : '';

  // Build iteration preamble
  const iterationPreamble = `This is review iteration ${iteration} of the automated review-fix cycle.${iteration > 1 ? '\nPrevious iterations have already addressed some issues. Focus on what remains unresolved.' : ''}`;

  // Build review prompt — Claude has full repo access, not just a diff blob
  const reviewPlaybookSection = formatPlaybook(task.playbook as Record<string, string> | undefined);
  const reviewPrompt = `${reviewClaudeMdPrefix}${reviewPlaybookSection}${reviewProjectCtxSection}${reviewHistorySection}You are reviewing PR #${prNumber} on this repository. The PR branch is "${branch}" targeting "${baseBranch}".

${iterationPreamble}

${originalPrompt ? `The original task was: ${originalPrompt}\n\n` : ''}${prTitle ? `PR title: ${prTitle}\n` : ''}${prBody ? `PR description:\n${prBody}\n\n` : ''}Files changed in this PR:
${changedFiles}

You have full access to the codebase. Read any files you need to understand the changes in context. Use \`git diff origin/${baseBranch}...HEAD\` to see the full diff, or \`git diff origin/${baseBranch}...HEAD -- <file>\` for specific files.

Review the changes thoroughly using this checklist:

${taskScope === 'small' ? 'This is a small-scope change. Focus your review on Correctness & Logic and Build & Type Safety. Security and performance sections are lower priority unless the change touches auth, user input handling, or database queries.\n\n' : taskScope === 'large' ? 'This is a large-scope change with higher risk. Review ALL checklist sections thoroughly. Pay special attention to backwards compatibility, data model safety, and security implications.\n\n' : ''}${origTaskType === 'qa' ? 'This is a test-only change. The Test Coverage section is less relevant (this IS the test). Focus on whether tests are meaningful and correctly assert behavior.\n\n' : origTaskType === 'migrate' ? 'This is a database migration. Focus on: reversibility (downgrade path), data preservation, and index impact on existing queries.\n\n' : ''}**Correctness & Logic:**
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

${buildTypeCheckInstructions(reviewBuildConfig)}

**Severity Classification Guide:**
Use these definitions when assigning severity to issues. Severity affects how the review appears in GitHub (critical = failure annotation, major = warning, minor = notice).

- **CRITICAL** — Issues that could cause security vulnerabilities, data loss, crashes, or correctness bugs in production. Examples:
  - SQL/command injection from unsanitized user input passed directly to a query or shell
  - Missing auth check on a new endpoint that exposes sensitive data
  - Off-by-one in a loop that silently skips or double-processes records in a DB migration

- **MAJOR** — Logic errors, missing error handling, race conditions, missing tests for new behavior, or violations of the codebase's established patterns. Examples:
  - Async function whose rejection is silently swallowed (missing try/catch or .catch())
  - New API endpoint with no test coverage at all
  - New code that bypasses an existing invariant (e.g., writing to a field that has a single designated writer elsewhere in the codebase)

- **MINOR** — Style inconsistencies, naming suggestions, documentation gaps, or optional improvements that do not affect correctness. Examples:
  - Variable named \`data\` where \`userRecord\` would be clearer
  - Missing docstring on a public function
  - Redundant else after a return statement

After your thorough review, output EXACTLY one of these JSON blocks as the very last thing in your response:

If the changes look good and should be merged:
{"verdict": "APPROVE", "feedback": "Brief summary of why it's approved"}

If changes are needed before merging:
{"verdict": "REQUEST_CHANGES", "feedback": "...", "issues": [{"severity": "critical|major|minor", "description": "...", "file": "path/to/file.py", "line": 42}]}

If the code is functionally correct but you have purely observational or stylistic notes (no blockers, no required changes):
{"verdict": "COMMENT", "feedback": "...", "issues": [{"severity": "minor", "description": "...", "file": "path/to/file.py", "line": 42}]}

Use COMMENT when: all issues are minor/stylistic and the code is functionally correct, the review is purely observational with no actionable changes needed, or you want to leave informational feedback without blocking the author. COMMENT does not spawn a fix task and does not merge the PR — the author decides what to do.

For each issue in the \`issues\` array:
- \`file\`: repo-relative path to the file where the issue is located (optional — omit if not tied to a specific file)
- \`line\`: line number in the file (optional — omit if not applicable)
- Issues without \`file\`/\`line\` are still valid and will be included in the review body summary

IMPORTANT: Also write your verdict JSON to /tmp/review_verdict_${taskId}.json (NOT in the repository). The file should contain ONLY the JSON object, no markdown wrapping.`;

  statusRef.value = 'Running code review...';
  const { exitCode, allOutput, lastResultText } = await runClaudeCode(reviewPrompt + `\n\n${DEFAULT_TOOL_GUIDANCE}`, ws, statusRef, model, effort, disallowedTools);

  // Prefer reading verdict from /tmp/ file (avoids output truncation), fall back to output parsing

  let logs = '';
  let verdictJsonField: string | undefined;
  try {
    if (fs.existsSync(`/tmp/review_verdict_${taskId}.json`)) {
      const verdictJson = fs.readFileSync(`/tmp/review_verdict_${taskId}.json`, 'utf-8').trim();
      // Validate it's actual verdict JSON
      const parsed = JSON.parse(verdictJson) as { verdict?: string };
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

  await persistAndPostStatus(taskId, {
    task_id: taskId,
    status: exitCode === 0 ? 'COMPLETED' : 'FAILED',
    ...(exitCode !== 0 ? { artifact_type: 'ERROR_LOG' } : {}),
    logs,
    ...(verdictJsonField ? { verdict_json: verdictJsonField } : {}),
    ...(exitCode !== 0 ? { failure_category: 'claude_code_failed' } : {}),
  });
}

// ── Handler: Fix (address review feedback on existing PR branch) ─────────────
export async function handleFix(
  taskId: string, payload: Record<string, unknown>, task: Record<string, unknown>,
  ghToken: string, owner: string, repo: string, ws: WebSocket | null, statusRef: { value: string },
  model?: string, effort?: string
) {
  const branch = validateBranchName(strVal(payload.branch), 'branch');
  const baseBranch = validateBranchName(strVal(payload.base_branch, 'main'), 'base_branch');
  const prompt = strVal(payload.prompt);
  const prNumber = Number(payload.pr_number);
  if (!Number.isInteger(prNumber) || prNumber <= 0) throw new Error(`Invalid pr_number: ${String(payload.pr_number)}`);
  const originalPrompt = strVal(payload.original_prompt);
  const buildConfig = (task.build_config || {}) as BuildConfig;

  if (!prompt) throw new Error('Fix task missing prompt');

  // Clone the existing PR branch (don't create a new one)
  statusRef.value = `Cloning ${owner}/${repo} (fixing PR #${prNumber})...`;
  sendLog(ws, `Fixing PR #${prNumber} on branch ${branch}\n`);
  execFileSync('rm', ['-rf', WORKSPACE], { encoding: 'utf-8' });
  const cloneUrl = `https://x-access-token:${ghToken}@github.com/${owner}/${repo}.git`;
  // Shallow clone of the PR branch directly — avoids fetch+checkout issues with --single-branch
  await cloneWithRetry(['clone', '--depth', String(DEFAULT_CLONE_DEPTH), '--branch', branch, cloneUrl, WORKSPACE]);
  writePlaybookHelper(taskId);
  writeMessagesHelper(taskId);
  const disallowedToolsFix = configureAgentPermissions('fix');
  const fixBuildConfig = resolveBuildConfig(task);
  const fixRelatedFiles = (
    ((task.context as Record<string, unknown> | undefined)?.related_files as string[] | undefined) ||
    (payload.related_files as string[] | undefined) ||
    []
  );
  const fixRelatedTestContext = discoverRelatedTests(ws, fixRelatedFiles, fixBuildConfig);

  git(['config', 'user.email', fixBuildConfig?.git_user_email || 'blueprint-agent@automated.dev']);
  git(['config', 'user.name', fixBuildConfig?.git_user_name || 'Blueprint Agent']);

  // Always rebase onto target branch to resolve merge conflicts
  statusRef.value = `Rebasing onto ${baseBranch}...`;
  sendLog(ws, `Rebasing ${branch} onto origin/${baseBranch}...\n`);
  let hasConflicts = false;
  try {
    git(['fetch', 'origin', `+refs/heads/${baseBranch}:refs/remotes/origin/${baseBranch}`]);
    git(['rebase', `origin/${baseBranch}`]);
    sendLog(ws, `Rebase successful\n`);
  } catch {
    sendLog(ws, `Rebase had conflicts — Claude will resolve them\n`);
    // Abort the failed rebase so Claude can handle it with a merge strategy
    try { git(['rebase', '--abort']); } catch { /* ignore */ }
    // Try merge instead so Claude sees conflict markers
    try {
      git(['merge', `origin/${baseBranch}`, '--no-edit']);
    } catch {
      hasConflicts = true;
      sendLog(ws, `Merge conflicts present — Claude will resolve\n`);
    }
  }

  // Prepend accumulated review history so the agent knows what was previously requested
  const reviewLoop = (task.context as Record<string, unknown> | undefined)?.review_loop as Record<string, unknown> | undefined;
  const reviewHistory = (reviewLoop?.review_history as Array<{ iteration: number; feedback: string; issues?: Array<{severity: string; description: string}> }> | undefined) || [];
  const iteration = (reviewLoop?.iteration as number | undefined) || 1;
  const maxIterations = 5;
  let iterationHeader = `You are on fix iteration ${iteration} of ${maxIterations}.\n`;
  if (iteration >= maxIterations) {
    iterationHeader += 'This is your FINAL attempt. If this fix is not accepted, the task will require manual intervention. Address every feedback point thoroughly.\n';
  }
  const originalPromptSection = originalPrompt ? `## Original Task Goal\n${originalPrompt}\n\n` : '';
  let basePrompt = originalPromptSection + iterationHeader + prompt;
  if (reviewHistory.length > 0) {
    const historyLines = reviewHistory
      .map(e => {
        const issues = e.issues;
        if (issues && issues.length > 0) {
          const critical = issues.filter(i => i.severity === 'critical').map(i => `  [CRITICAL] ${i.description}`);
          const major = issues.filter(i => i.severity === 'major').map(i => `  [MAJOR] ${i.description}`);
          const minor = issues.filter(i => i.severity === 'minor').map(i => `  [MINOR] ${i.description}`);
          return `- Iteration ${e.iteration}:\n${[...critical, ...major, ...minor].join('\n')}`;
        } else {
          return `- Iteration ${e.iteration}: ${e.feedback.slice(0, 1500)}`;
        }
      })
      .join('\n');
    basePrompt = `${originalPromptSection}Previous review iterations:\n${historyLines}\n\nCurrent review feedback to address:\n${prompt}`;
  }

  // Always check for conflict markers — even if rebase/merge appeared to succeed
  let enrichedPrompt = basePrompt;
  try {
    const conflictFiles = git(['diff', '--name-only', '--diff-filter=U']);
    if (conflictFiles.trim()) {
      hasConflicts = true;
      const prGoal = originalPrompt ? `\n\nThis PR's goal: ${originalPrompt.slice(0, 500)}\n` : '';
      enrichedPrompt = `MERGE CONFLICTS DETECTED in the following files:\n${conflictFiles}${prGoal}\n\nResolve ALL conflict markers (${'<'.repeat(7)} ${'='.repeat(7)} ${'>'.repeat(7)}) in each file listed above.\n\nIMPORTANT: ${baseBranch} is the source of truth. It may have received many commits since this branch diverged. When resolving conflicts:\n- ALWAYS keep ${baseBranch}'s version of code that was refactored, moved, or restructured upstream\n- Only re-apply this PR's specific changes (described above) on top of ${baseBranch}'s current state\n- If a file was heavily rewritten on ${baseBranch}, adapt this PR's changes to the new structure rather than reverting to the old one\n- When in doubt, prefer ${baseBranch}'s version and carefully layer this PR's additions back in\n\nAfter resolving conflicts, stage the files with git add and ensure no conflict markers remain.\n\n${basePrompt}`;
      sendLog(ws, `Conflicting files:\n${conflictFiles}\n`);
    }
  } catch { /* no unmerged files */ }
  // Also check for conflict markers in tracked files via grep
  if (!hasConflicts) {
    try {
      const markerCheck = execFileSync('git', ['-C', WORKSPACE, 'grep', '-rl', '<' + '<<<<<<', '--', '*.ts', '*.tsx', '*.py', '*.js', '*.json'], { encoding: 'utf-8', timeout: 10_000 }).trim();
      if (markerCheck) {
        enrichedPrompt = `CONFLICT MARKERS FOUND in the following files:\n${markerCheck}${originalPrompt ? `\n\nThis PR's goal: ${originalPrompt.slice(0, 500)}` : ''}\n\nResolve ALL conflict markers (${'<'.repeat(7)} ${'='.repeat(7)} ${'>'.repeat(7)}) in each file. ${baseBranch} is the source of truth — keep its refactors and restructuring, and re-apply only this PR's specific changes (described above) on top.\n\n${basePrompt}`;
        sendLog(ws, `Files with conflict markers:\n${markerCheck}\n`);
      }
    } catch { /* no markers found, good */ }
  }

  // Stale-premise detection: the orchestrator spawned this task based on a snapshot
  // of PR state that can be minutes to hours old. If the prompt claims "merge conflicts"
  // but our rebase came up clean and no markers are present, re-query GitHub directly
  // and pivot Claude's plan to whatever is actually blocking the PR now.
  const promptMentionsConflicts = /merge conflicts?|conflict markers|rebase.*resolve/i.test(prompt);
  if (promptMentionsConflicts && !hasConflicts) {
    sendLog(ws, `Prompt claims conflicts but rebase was clean — verifying PR state against GitHub...\n`);
    let currentMergeable: boolean | null | undefined;
    let failedChecks: Array<{ name: string; conclusion: string; url: string }> = [];
    try {
      const prJson = execFileSync('curl', [
        '-sS', '--fail-with-body', '--max-time', '20',
        '-H', `Authorization: Bearer ${ghToken}`,
        '-H', 'Accept: application/vnd.github+json',
        `https://api.github.com/repos/${owner}/${repo}/pulls/${prNumber}`,
      ], { encoding: 'utf-8', timeout: 25_000 });
      const prObj = JSON.parse(prJson) as { mergeable?: boolean | null; head?: { sha?: string } };
      currentMergeable = prObj.mergeable;
      const headSha = prObj.head?.sha;
      if (headSha) {
        const checksJson = execFileSync('curl', [
          '-sS', '--fail-with-body', '--max-time', '20',
          '-H', `Authorization: Bearer ${ghToken}`,
          '-H', 'Accept: application/vnd.github+json',
          `https://api.github.com/repos/${owner}/${repo}/commits/${headSha}/check-runs?per_page=100`,
        ], { encoding: 'utf-8', timeout: 25_000 });
        const checksObj = JSON.parse(checksJson) as { check_runs?: Array<{ name?: string; conclusion?: string; html_url?: string; details_url?: string }> };
        failedChecks = (checksObj.check_runs || [])
          .filter(c => c.conclusion === 'failure' || c.conclusion === 'timed_out' || c.conclusion === 'cancelled')
          .map(c => ({ name: c.name || '(unnamed)', conclusion: c.conclusion || '', url: c.html_url || c.details_url || '' }));
      }
      sendLog(ws, `PR #${prNumber}: mergeable=${String(currentMergeable)}, failed_checks=${failedChecks.length}\n`);
    } catch (e) {
      sendLog(ws, `Could not fetch current PR state (will proceed with original prompt): ${e instanceof Error ? e.message : String(e)}\n`);
    }

    if (currentMergeable === true && failedChecks.length > 0) {
      const failedList = failedChecks.map(c => `  - ${c.name} (${c.conclusion})`).join('\n');
      enrichedPrompt =
        `## Stale-premise notice\n` +
        `This task was spawned because the orchestrator observed PR #${prNumber} as unmergeable, but the branch now ` +
        `rebases cleanly onto ${baseBranch}. The PR's current blocker is failing CI, not merge conflicts:\n\n` +
        `${failedList}\n\n` +
        `Investigate each failing check's logs (use the GitHub API or the details URL above) and fix the underlying ` +
        `build/lint/test errors. Do NOT hunt for conflict markers — there are none.\n\n` +
        basePrompt;
    } else if (currentMergeable === true && failedChecks.length === 0) {
      // No conflicts, no CI failures. Task is obsolete. Complete cleanly without running Claude.
      sendLog(ws, `PR #${prNumber} has no conflicts and no failing checks — task is obsolete, completing without changes.\n`);
      await persistAndPostStatus(taskId, {
        task_id: taskId,
        status: 'COMPLETED',
        logs: `Task was spawned to fix merge conflicts on PR #${prNumber}, but the branch now rebases cleanly and no CI checks are failing. No action taken (stale premise).`,
      });
      return;
    } else if (currentMergeable === false) {
      // GitHub still reports unmergeable even though our rebase was clean —
      // usually means GitHub is mid-recompute. Tell Claude explicitly.
      enrichedPrompt =
        `## Stale-premise notice\n` +
        `The local rebase completed cleanly, but GitHub still reports PR #${prNumber} as unmergeable ` +
        `(mergeable=false). GitHub may still be recomputing, or the conflict may lie in a file outside the ` +
        `diff you'd normally check. Before assuming there's nothing to do:\n` +
        `  1. Run \`git grep '${'<'.repeat(7)}'\` across the whole tree.\n` +
        `  2. Inspect \`git diff origin/${baseBranch}...HEAD\` for anything that touches files recently refactored upstream.\n` +
        `  3. If nothing surfaces, push the clean rebase — GitHub will reassess on push.\n\n` +
        basePrompt;
    }
    // If currentMergeable is null/undefined (couldn't query), leave basePrompt alone.
  }

  // Prepend parent context if this fix agent is part of a larger initiative
  const fixParentCtx = (task.context as Record<string, unknown> | undefined)?.parent_context as Record<string, unknown> | undefined;
  if (fixParentCtx?.parent_prompt) {
    enrichedPrompt = `## Parent Task Context\nThis task is a subtask of: **${strVal(fixParentCtx.parent_title)}**\nParent goal: ${strVal(fixParentCtx.parent_prompt)}\n\n${enrichedPrompt}`;
  }

  // Prepend related test context so the agent knows which tests to update
  if (fixRelatedTestContext) {
    enrichedPrompt = `${fixRelatedTestContext}${enrichedPrompt}`;
  }

  // Prepend CLAUDE.md instructions + playbook
  const fixClaudeMd = readClaudeMd();
  const fixPlaybookSection = formatPlaybook(task.playbook as Record<string, string> | undefined);
  const fixRetryContext = buildRetryContext(task);
  const fixFirstTaskSection = task.first_task_on_repo ? FIRST_TASK_GUIDANCE : '';
  if (fixClaudeMd) {
    enrichedPrompt = `## Codebase Instructions\n${fixClaudeMd}\n\n${fixPlaybookSection}${fixRetryContext}${enrichedPrompt}`;
  } else if (fixPlaybookSection || fixRetryContext) {
    enrichedPrompt = `${fixPlaybookSection}${fixRetryContext}${enrichedPrompt}`;
  }

  // Append fix-specific task type hint and playbook guidance
  if (TASK_TYPE_HINTS['fix']) {
    enrichedPrompt = `${enrichedPrompt}\n\n${TASK_TYPE_HINTS['fix']}`;
  }
  enrichedPrompt = `${enrichedPrompt}${fixFirstTaskSection}${PLAYBOOK_GUIDANCE}\nNote: If the review feedback revealed a recurring pattern or non-obvious codebase rule, save it to the playbook.\n\n${DEFAULT_TOOL_GUIDANCE}`;

  // Run Claude with the review feedback as the prompt
  statusRef.value = 'Addressing review feedback...';
  const fixBaseCommit = git(['rev-parse', 'HEAD']);
  const { exitCode, allOutput } = await runClaudeCode(enrichedPrompt, ws, statusRef, model, effort, disallowedToolsFix);

  // Check for changes — both uncommitted files and commits Claude may have made
  const diff = git(['status', '--porcelain']);
  const fixCurrentHead = git(['rev-parse', 'HEAD']);
  const hasFixChanges = diff.length > 0 || fixCurrentHead !== fixBaseCommit;
  if (hasFixChanges && exitCode === 0) {
    // Collect changed paths from both uncommitted changes and any commits Claude made
    const fixUncommittedPaths = diff.length > 0
      ? diff.split('\n').map(l => l.trim().split(/\s+/).pop() || '')
      : [];
    const fixCommittedPaths = fixCurrentHead !== fixBaseCommit
      ? git(['diff', '--name-only', `${fixBaseCommit}..HEAD`]).split('\n').filter(Boolean)
      : [];
    const changedPaths = [...new Set([...fixUncommittedPaths, ...fixCommittedPaths])];
    await lintFixLoop(changedPaths, ws, statusRef, 'the fix you just applied', model, effort, fixBuildConfig);

    const { testFailuresDetected: fixTestFailures, failureSummary: fixTestFailureSummary } = await testFixLoop(ws, statusRef, model, effort, buildConfig);

    git(['add', '-A']);
    const shortId = taskId.replace(/-/g, '').slice(0, 8);
    const testNote = fixTestFailures
      ? `\n\nNote: test failures detected, manual review recommended${fixTestFailureSummary ? `:\n${fixTestFailureSummary.slice(0, 500)}` : ''}`
      : '';
    // Only create a new commit if there are uncommitted changes (Claude may have already committed)
    const fixStaged = git(['status', '--porcelain']);
    if (fixStaged.length > 0) {
      execFileSync('git', ['-C', WORKSPACE, 'commit', '-m', `blueprint: address review feedback\n\nAutomated by Blueprint Agent (task: ${shortId})${testNote}`], { encoding: 'utf-8' });
    }
  }

  // Final rebase onto base branch to ensure no conflicts remain before push.
  // If rebase fails, fall back to merge — don't throw, let the push succeed
  // and the review pipeline handle any remaining issues.
  if (exitCode === 0) {
    try {
      git(['fetch', 'origin', `+refs/heads/${baseBranch}:refs/remotes/origin/${baseBranch}`]);
      git(['rebase', `origin/${baseBranch}`]);
      sendLog(ws, `Final rebase onto ${baseBranch} successful\n`);
    } catch {
      let conflictList = '';
      try { conflictList = git(['diff', '--name-only', '--diff-filter=U']).trim(); } catch { /* ignore */ }
      try { git(['rebase', '--abort']); } catch { /* ignore */ }
      sendLog(ws, `Final rebase had conflicts — trying merge instead\n`);
      try {
        git(['merge', `origin/${baseBranch}`, '--no-edit']);
        sendLog(ws, `Merge onto ${baseBranch} successful\n`);
      } catch {
        // Merge also conflicted — abort and push as-is.
        // The PR will show conflicts and the review pipeline will handle it.
        try { git(['merge', '--abort']); } catch { /* ignore */ }
        sendLog(ws, `Merge also conflicted (${conflictList || 'unknown files'}) — pushing as-is, review pipeline will handle\n`);
      }
    }
  }

  // Check for remaining conflict markers — give Claude one chance to fix them
  if (exitCode === 0) {
    try {
      const remainingMarkers = execFileSync('git', ['-C', WORKSPACE, 'grep', '-rl', '<' + '<<<<<<', '--', '*.ts', '*.tsx', '*.py', '*.js', '*.jsx', '*.json'], { encoding: 'utf-8', timeout: 10_000 }).trim();
      if (remainingMarkers) {
        sendLog(ws, `Conflict markers still present in:\n${remainingMarkers}\nAsking Claude to resolve...\n`);
        await runClaudeCode(
          `CRITICAL: These files still have git conflict markers (${'<'.repeat(7)}, ${'='.repeat(7)}, ${'>'.repeat(7)}):\n${remainingMarkers}\n\nResolve ALL conflict markers in every file listed. The base branch is the source of truth — keep its code structure and only re-apply this PR's specific additions. After resolving, stage with git add.`,
          ws, statusRef, model, effort
        );
        git(['add', '-A']);
        try {
          execFileSync('git', ['-C', WORKSPACE, 'commit', '-m', 'blueprint: resolve remaining conflict markers'], { encoding: 'utf-8' });
        } catch { /* nothing to commit — markers may have already been staged */ }
        // Final check — if markers persist after retry, warn but proceed
        try {
          const stillConflicting = execFileSync('git', ['-C', WORKSPACE, 'grep', '-rl', '<' + '<<<<<<', '--', '*.ts', '*.tsx', '*.py', '*.js', '*.jsx', '*.json'], { encoding: 'utf-8', timeout: 10_000 }).trim();
          if (stillConflicting) {
            sendLog(ws, `WARNING: Conflict markers persist after retry in:\n${stillConflicting}\n`);
          }
        } catch { /* no markers — resolved successfully */ }
      }
    } catch { /* grep found nothing — no markers, good */ }
  }

  // Re-run lint after rebase — upstream changes can introduce new errors
  if (exitCode === 0 && hasFixChanges) {
    const postRebaseDiff = git(['status', '--porcelain']);
    const changedPaths = postRebaseDiff.length > 0
      ? postRebaseDiff.split('\n').map(l => l.trim().split(/\s+/).pop() || '')
      : git(['diff', '--name-only', `origin/${baseBranch}..HEAD`]).split('\n').filter(Boolean);
    try {
      runProjectLintChecks(changedPaths, ws, fixBuildConfig);
    } catch (postRebaseLintErr: unknown) {
      const msg = postRebaseLintErr instanceof Error ? postRebaseLintErr.message : String(postRebaseLintErr);
      sendLog(ws, `Post-rebase lint failed:\n${msg.slice(0, 3000)}\nClaude will fix...\n`);
      await runClaudeCode(
        `After rebasing onto ${baseBranch}, lint/build errors appeared. Fix ALL of them — do not use \`any\` types.\n\n${msg.slice(0, 5000)}`,
        ws, statusRef, model, effort
      );
      git(['add', '-A']);
      const postLintStaged = git(['status', '--porcelain']);
      if (postLintStaged.length > 0) {
        const shortId = taskId.replace(/-/g, '').slice(0, 8);
        execFileSync('git', ['-C', WORKSPACE, 'commit', '-m', `blueprint: fix post-rebase lint errors\n\nAutomated by Blueprint Agent (task: ${shortId})`], { encoding: 'utf-8' });
      }
    }
  }

  // Always push if the branch has diverged from origin (rebase or new commits)
  if (exitCode === 0) {
    statusRef.value = 'Pushing fixes...';
    const localHead = git(['rev-parse', 'HEAD']);
    let remoteHead = '';
    try { remoteHead = git(['rev-parse', `origin/${branch}`]); } catch { /* ignore */ }

    if (localHead !== remoteHead) {
      sendHeartbeatSync(taskId, `Pushing fixes to ${branch}...`);
      const freshGhRes = await apiGet(`/api/agent/github/token?task_id=${taskId}`) as { data: { token: string } };
      const pushUrl = `https://x-access-token:${freshGhRes.data.token}@github.com/${owner}/${repo}.git`;
      execFileSync('git', ['-C', WORKSPACE, 'remote', 'set-url', 'origin', pushUrl], { encoding: 'utf-8' });
      await gitAsync(['push', '--force-with-lease', 'origin', branch]);
      sendLog(ws, `Pushed fixes to ${branch}\n`);

      await persistAndPostStatus(taskId, {
        task_id: taskId, status: 'COMPLETED', artifact_type: 'GIT_COMMIT',
        commit_hash: localHead, repo_url: `https://github.com/${owner}/${repo}`,
      });
    } else {
      await persistAndPostStatus(taskId, { task_id: taskId, status: 'COMPLETED', logs: 'No changes needed for review feedback.' });
    }
  } else {
    await persistAndPostStatus(taskId, {
      task_id: taskId, status: 'FAILED', artifact_type: 'ERROR_LOG', logs: sanitize(allOutput.slice(-10_000)),
      failure_category: 'claude_code_failed',
    });
  }
}
