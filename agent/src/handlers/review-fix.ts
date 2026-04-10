import fs from 'fs';
import { execFileSync } from 'child_process';
import type WebSocket from 'ws';
import {
  WORKSPACE, strVal, sanitize, sendLog, validateBranchName,
  apiPost, apiGet, git, cloneWithRetry,
  runClaudeCode, runProjectLintChecks, lintFixLoop, testFixLoop,
} from '../index.js';
import { DEFAULT_CLONE_DEPTH } from '../constants.js';
import {
  TASK_TYPE_HINTS, readClaudeMd, formatProjectContext, configureAgentPermissions,
} from '../prompt.js';

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
  const disallowedTools = configureAgentPermissions('review');

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
  const reviewPrompt = `${reviewClaudeMdPrefix}${reviewProjectCtxSection}${reviewHistorySection}You are reviewing PR #${prNumber} on this repository. The PR branch is "${branch}" targeting "${baseBranch}".

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

**Build & Type Safety:**
- For TypeScript changes (admin-ui/ or agent/): run \`cd admin-ui && npx tsc --noEmit\` and/or \`cd agent && npx tsc --noEmit\` and report any type errors
- For Python changes: verify imports are correct and consistent with existing patterns
- Include build/type check results in your verdict feedback

After your thorough review, output EXACTLY one of these JSON blocks as the very last thing in your response:

If the changes look good and should be merged:
{"verdict": "APPROVE", "feedback": "Brief summary of why it's approved"}

If changes are needed before merging:
{"verdict": "REQUEST_CHANGES", "feedback": "...", "issues": [{"severity": "critical|major|minor", "description": "..."}]}

IMPORTANT: Also write your verdict JSON to /tmp/review_verdict_${taskId}.json (NOT in the repository). The file should contain ONLY the JSON object, no markdown wrapping.`;

  statusRef.value = 'Running code review...';
  const { exitCode, allOutput, lastResultText } = await runClaudeCode(reviewPrompt, ws, statusRef, model, effort, disallowedTools);

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

  await apiPost('/api/agent/status', {
    task_id: taskId,
    status: exitCode === 0 ? 'COMPLETED' : 'FAILED',
    ...(exitCode !== 0 ? { artifact_type: 'ERROR_LOG' } : {}),
    logs,
    ...(verdictJsonField ? { verdict_json: verdictJsonField } : {}),
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

  if (!prompt) throw new Error('Fix task missing prompt');

  // Clone the existing PR branch (don't create a new one)
  statusRef.value = `Cloning ${owner}/${repo} (fixing PR #${prNumber})...`;
  sendLog(ws, `Fixing PR #${prNumber} on branch ${branch}\n`);
  execFileSync('rm', ['-rf', WORKSPACE], { encoding: 'utf-8' });
  const cloneUrl = `https://x-access-token:${ghToken}@github.com/${owner}/${repo}.git`;
  // Shallow clone of the PR branch directly — avoids fetch+checkout issues with --single-branch
  await cloneWithRetry(['clone', '--depth', String(DEFAULT_CLONE_DEPTH), '--branch', branch, cloneUrl, WORKSPACE]);
  const disallowedToolsFix = configureAgentPermissions('fix');

  git(['config', 'user.email', 'blueprint-agent@automated.dev']);
  git(['config', 'user.name', 'Blueprint Agent']);

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
    enrichedPrompt = `## Parent Task Context\nThis task is a subtask of: **${strVal(fixParentCtx.parent_title)}**\nParent goal: ${strVal(fixParentCtx.parent_prompt)}\n\n${enrichedPrompt}`;
  }

  // Prepend CLAUDE.md instructions
  const fixClaudeMd = readClaudeMd();
  if (fixClaudeMd) {
    enrichedPrompt = `## Codebase Instructions\n${fixClaudeMd}\n\n${enrichedPrompt}`;
  }

  // Append fix-specific task type hint
  if (TASK_TYPE_HINTS['fix']) {
    enrichedPrompt = `${enrichedPrompt}\n\n${TASK_TYPE_HINTS['fix']}`;
  }

  // Run Claude with the review feedback as the prompt
  statusRef.value = 'Addressing review feedback...';
  const { exitCode, allOutput } = await runClaudeCode(enrichedPrompt, ws, statusRef, model, effort, disallowedToolsFix);

  // Check for uncommitted changes (from Claude's fixes)
  const diff = git(['status', '--porcelain']);
  if (diff.length > 0 && exitCode === 0) {
    // Run lint/build checks before committing — retry up to 3 times with Claude fixes
    const changedPaths = diff.split('\n').map(l => l.trim().split(/\s+/).pop() || '');
    await lintFixLoop(changedPaths, ws, statusRef, 'the fix you just applied', model, effort);

    const { testFailuresDetected: fixTestFailures } = await testFixLoop(ws, statusRef, model, effort);

    git(['add', '-A']);
    const shortId = taskId.replace(/-/g, '').slice(0, 8);
    const testNote = fixTestFailures ? '\n\nNote: test failures detected, manual review recommended' : '';
    execFileSync('git', ['-C', WORKSPACE, 'commit', '-m', `blueprint: address review feedback\n\nAutomated by Blueprint Agent (task: ${shortId})${testNote}`], { encoding: 'utf-8' });
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
    try { remoteHead = git(['rev-parse', `origin/${branch}`]); } catch { /* ignore */ }

    if (localHead !== remoteHead) {
      const freshGhRes = await apiGet(`/api/agent/github/token?task_id=${taskId}`) as { data: { token: string } };
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
