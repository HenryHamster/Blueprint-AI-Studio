import { execFileSync } from 'child_process';
import type WebSocket from 'ws';
import {
  WORKSPACE, strVal, sanitize, sendLog, validateBranchName,
  persistAndPostStatus, git, setupWorkspace, pushAndCreatePR,
  runClaudeCode, lintFixLoop, testFixLoop, captureTestBaseline, matchesRateLimit,
  runProjectLintChecks,
} from '../index.js';
import {
  getTaskTypeHints, DEFAULT_TOOL_GUIDANCE, PLAYBOOK_GUIDANCE, FIRST_TASK_GUIDANCE,
  readClaudeMd, buildContextPrefix, buildFileTree, discoverRelatedTests, configureAgentPermissions,
  writePlaybookHelper, writeMessagesHelper, formatPlaybook, buildRetryContext,
  buildSelfReviewPrompt, buildAnalysisPrompt, extractModuleInterfaces, getMandatoryChecks,
  discoverAffectedConsumers,
} from '../prompt.js';
import { resolveBuildConfig } from '../build-config.js';
import { MAX_SELF_REVIEW_ATTEMPTS, SCOPE_OVERFLOW_THRESHOLDS } from '../constants.js';

// ── Standard Handler Helper ───────────────────────────────────────────────

/**
 * Returns per-file diff statistics (additions, deletions, change type) for
 * all files changed between baseCommit and HEAD.  Returns [] if the commits
 * are identical or git commands fail.
 */
function computeFileDiffSummary(
  baseCommit: string,
  currentHead: string,
): Array<{ file: string; additions: number; deletions: number; change_type: string }> {
  if (baseCommit === currentHead) return [];
  try {
    const numstat = git(['diff', '--numstat', `${baseCommit}..HEAD`]);
    const nameStatus = git(['diff', '--name-status', `${baseCommit}..HEAD`]);

    const changeTypes: Record<string, string> = {};
    for (const line of nameStatus.split('\n').filter(Boolean)) {
      const parts = line.split('\t');
      if (parts.length >= 2) {
        const typeChar = parts[0][0];
        const file = parts[parts.length - 1];
        changeTypes[file] =
          typeChar === 'A' ? 'added' :
          typeChar === 'D' ? 'deleted' :
          typeChar === 'R' ? 'renamed' : 'modified';
      }
    }

    const result: Array<{ file: string; additions: number; deletions: number; change_type: string }> = [];
    for (const line of numstat.split('\n').filter(Boolean)) {
      const parts = line.split('\t');
      if (parts.length === 3) {
        const additions = parts[0] === '-' ? 0 : (parseInt(parts[0], 10) || 0);
        const deletions = parts[1] === '-' ? 0 : (parseInt(parts[1], 10) || 0);
        const file = parts[2];
        result.push({ file, additions, deletions, change_type: changeTypes[file] || 'modified' });
      }
    }
    return result.slice(0, 50);
  } catch {
    return [];
  }
}

export interface StandardHandlerOpts {
  taskId: string;
  payload: Record<string, unknown>;
  task: Record<string, unknown>;
  ghToken: string;
  owner: string;
  repo: string;
  ws: WebSocket | null;
  statusRef: { value: string };
  model?: string;
  effort?: string;
  branchPrefix: string;
  buildPrompt: (context: { contextPrefix: string; parentCtx: string; rawPrompt: string }) => string;
  commitPrefix: string;
  prTitlePrefix: string;
  noChangesMessage?: string;
  runLintFix?: boolean;
  runTestFix?: boolean;
  failOnTestFailure?: boolean;
  captureBaseline?: boolean;
  runSelfReview?: boolean;
  lintContext?: string;
  includeTaskTypeHints?: boolean;
  issueReference?: string;
  relatedFiles?: string[];
}

/**
 * Extracts the structured self-review JSON object from the agent's output.
 * Looks for a ```json ... ``` code fence first, then falls back to finding
 * the first `{` that can be parsed as JSON.
 */
function extractSelfReviewJson(output: string): Record<string, unknown> | null {
  const fenceMatch = output.match(/```json\s*\n?([\s\S]*?)\n?```/);
  if (fenceMatch) {
    try { return JSON.parse(fenceMatch[1].trim()) as Record<string, unknown>; } catch { /* fallthrough */ }
  }
  const start = output.indexOf('{');
  if (start !== -1) {
    const end = output.lastIndexOf('}');
    if (end > start) {
      try { return JSON.parse(output.slice(start, end + 1)) as Record<string, unknown>; } catch { /* ignore */ }
    }
  }
  return null;
}

async function runStandardHandler(opts: StandardHandlerOpts): Promise<void> {
  const {
    taskId, payload, task, ghToken, owner, repo, ws, statusRef,
    model, effort, branchPrefix, buildPrompt, commitPrefix, prTitlePrefix,
    noChangesMessage = 'No file changes.',
    runLintFix = true,
    runTestFix = false,
    failOnTestFailure = false,
    captureBaseline = false,
    runSelfReview = false,
    lintContext = 'the code you just wrote',
    includeTaskTypeHints = true,
    issueReference,
    relatedFiles,
  } = opts;

  const buildConfig = resolveBuildConfig(task);
  const ctx = (task.context as Record<string, unknown> | undefined) || {};
  const rawBaseBranch = strVal(payload.base_branch) || strVal(ctx.research_branch) || strVal(ctx.original_base) || 'main';
  const baseBranch = validateBranchName(rawBaseBranch, 'base_branch');
  const rawTitle = strVal(task.title) || strVal(payload.title) || 'automated changes';
  const taskTitle = rawTitle.replace(/[`$\\!"';&|<>(){}[\]]/g, '');

  const contextPrefix = buildContextPrefix(task);

  const parentCtxObj = (task.context as Record<string, unknown> | undefined)?.parent_context as Record<string, unknown> | undefined;
  const parentCtx = parentCtxObj?.parent_prompt
    ? `## Parent Task Context\nThis task is a subtask of: **${strVal(parentCtxObj.parent_title)}**\nParent goal: ${strVal(parentCtxObj.parent_prompt)}\n\n`
    : '';

  const rawPrompt = strVal(payload.prompt) || strVal(payload.description) || strVal(task.description);
  const promptSuffix = payload.prompt_suffix ? `\n\nAdditional instructions:\n${strVal(payload.prompt_suffix)}` : '';
  const taskTypeKey = strVal(payload.type);
  const taskTypeHintsMap = getTaskTypeHints(buildConfig);
  const taskTypeHint = includeTaskTypeHints && taskTypeHintsMap[taskTypeKey]
    ? `\n\n${taskTypeHintsMap[taskTypeKey].replace(/\{\{BASE_BRANCH\}\}/g, baseBranch)}`
    : '';

  const claudeMd = readClaudeMd();
  const claudeMdPrefix = claudeMd ? `## Codebase Instructions\n${claudeMd}\n\n` : '';
  const playbookSection = formatPlaybook(task.playbook as Record<string, string> | undefined);
  const retryContext = buildRetryContext(task);
  const firstTaskSection = task.first_task_on_repo ? FIRST_TASK_GUIDANCE : '';
  const finalPrompt = claudeMdPrefix + playbookSection + retryContext + buildPrompt({ contextPrefix, parentCtx, rawPrompt }) + taskTypeHint + firstTaskSection + PLAYBOOK_GUIDANCE + `\nNote: If lint or test fixes required non-obvious steps, save them to the playbook (e.g., ./playbook set "lint-gotcha" "description").\n` + `\n\n${DEFAULT_TOOL_GUIDANCE}` + promptSuffix;

  const { workBranch, shortId } = await setupWorkspace(owner, repo, ghToken, baseBranch, branchPrefix, taskId, ws, statusRef, buildConfig);
  writePlaybookHelper(taskId);
  writeMessagesHelper(taskId);
  const disallowedTools = configureAgentPermissions(taskTypeKey);

  // Capture test baseline for medium/large scope tasks on main branch.
  // This lets the test fix loop distinguish pre-existing failures from newly-broken tests.
  // We scope the baseline run to directories matching related_files to keep it fast.
  let baselineFailures: Set<string> | undefined;
  if (captureBaseline && runTestFix && baseBranch === 'main') {
    const relatedFiles = (
      (payload.related_files as string[] | undefined) ||
      ((task.context as Record<string, unknown> | undefined)?.related_files as string[] | undefined) ||
      []
    );
    const captured = await captureTestBaseline(ws, statusRef, buildConfig, relatedFiles);
    baselineFailures = captured ?? undefined;
  }

  const fileTree = buildFileTree(ws);

  // ── Phase 1: Pre-implementation analysis (medium-scope tasks only) ──────────
  // Runs a read-only pass to map module interfaces and dependencies before coding.
  let analysisContext = '';
  const taskScope = strVal(ctx.scope);
  if (taskScope === 'medium') {
    sendLog(ws, '\n[Analysis Phase] Mapping module interfaces and dependencies...\n');
    statusRef.value = 'Analyzing module interfaces...';

    const analysisRelatedFiles = (
      (payload.related_files as string[] | undefined) ||
      (ctx.related_files as string[] | undefined) ||
      []
    ) as string[];

    const analysisPrompt = buildAnalysisPrompt({
      rawPrompt,
      taskTitle,
      relatedFiles: analysisRelatedFiles,
      fileTree,
    });

    const { exitCode: analysisExitCode, lastResultText: analysisResult } = await runClaudeCode(
      analysisPrompt,
      ws, statusRef, model, effort,
      ['Write', 'Edit', 'NotebookEdit'],
    );

    if (analysisExitCode === 0 && analysisResult.trim()) {
      analysisContext = `## Pre-Implementation Analysis\n\n${analysisResult.trim()}\n\n`;
      sendLog(ws, '[Analysis Phase] Complete.\n');
    } else {
      sendLog(ws, '[Analysis Phase] Skipped — proceeding to implementation.\n');
    }
  }

  const finalPromptWithTree = fileTree + analysisContext + finalPrompt;

  statusRef.value = 'Running Claude Code...';
  // Record HEAD before Claude runs so we can detect commits Claude made (e.g. before rebasing)
  const baseCommit = git(['rev-parse', 'HEAD']);
  const { exitCode, allOutput, lastResultText } = await runClaudeCode(finalPromptWithTree, ws, statusRef, model, effort, disallowedTools);

  const diff = git(['status', '--porcelain']);
  const currentHead = git(['rev-parse', 'HEAD']);
  // Detect changes from BOTH uncommitted files AND commits Claude may have created
  const hasChanges = diff.length > 0 || currentHead !== baseCommit;

  // Compute per-file diff stats (lines added/removed) for retry context enrichment
  const fileChangeDetails: { file: string; additions: number; deletions: number }[] = [];
  if (currentHead !== baseCommit) {
    try {
      const numstatOut = git(['diff', '--numstat', `${baseCommit}..HEAD`]);
      for (const line of numstatOut.split('\n').filter(Boolean)) {
        const parts = line.split('\t');
        if (parts.length >= 3 && parts[2]) {
          fileChangeDetails.push({
            file: parts[2],
            additions: parseInt(parts[0]) || 0,
            deletions: parseInt(parts[1]) || 0,
          });
        }
      }
    } catch { /* ignore diff errors */ }
  }

  if (hasChanges && exitCode === 0) {
    statusRef.value = 'Pushing changes...';
    sendLog(ws, '\nCommitting and pushing changes...\n');

    // Collect changed paths from both uncommitted changes and any commits Claude made
    const uncommittedPaths = diff.length > 0
      ? diff.split('\n').map(l => l.trim().split(/\s+/).pop() || '')
      : [];
    const committedPaths = currentHead !== baseCommit
      ? git(['diff', '--name-only', `${baseCommit}..HEAD`]).split('\n').filter(Boolean)
      : [];
    const changedPaths = [...new Set([...uncommittedPaths, ...committedPaths])];
    const diffSummary = computeFileDiffSummary(baseCommit, currentHead);

    // ── Scope overflow detection ───────────────────────────────────────────────
    const taskScope = strVal((task.context as Record<string, unknown> | undefined)?.scope);
    const overflowThreshold = taskScope ? SCOPE_OVERFLOW_THRESHOLDS[taskScope] : undefined;
    const scopeOverflowWarning = overflowThreshold !== undefined && changedPaths.length > overflowThreshold
      ? `This task has scope "${taskScope}" (expected few files) but ${changedPaths.length} files were changed, exceeding the ${overflowThreshold}-file threshold. Changed files:\n${changedPaths.map(f => `- ${f}`).join('\n')}`
      : undefined;
    if (scopeOverflowWarning) {
      sendLog(ws, `\nWARNING: Scope overflow detected — ${changedPaths.length} files changed for a "${taskScope}" task (threshold: ${overflowThreshold}).\n`);
    }
    if (runLintFix) {
      const lintPassed = await lintFixLoop(changedPaths, ws, statusRef, lintContext, model, effort, buildConfig);
      if (!lintPassed && failOnTestFailure) {
        sendLog(ws, 'Lint checks still failing after max attempts — marking task as FAILED.\n');
        await persistAndPostStatus(taskId, {
          task_id: taskId, status: 'FAILED', artifact_type: 'ERROR_LOG',
          logs: `Lint/build checks failed after max fix attempts. See logs above for details.`,
          failure_category: 'lint_failed',
          changed_files: changedPaths.slice(0, 50),
          ...(diffSummary.length > 0 ? { diff_summary: diffSummary } : {}),
        });
        return;
      }
    }

    let testFailuresDetected = false;
    let testsPassed = true;
    let testFailureSummary: string | undefined;
    if (runTestFix && baseBranch === 'main') {
      const result = await testFixLoop(ws, statusRef, model, effort, buildConfig, baselineFailures, relatedFiles);
      testFailuresDetected = result.testFailuresDetected;
      testsPassed = result.passed;
      testFailureSummary = result.failureSummary;
    }

    if (failOnTestFailure && baseBranch === 'main' && testFailuresDetected && !testsPassed) {
      sendLog(ws, 'Tests failed after max fix attempts — marking task as FAILED.\n');
      const failedLogs = testFailureSummary
        ? `Test suite failed after max fix attempts. Failing tests:\n${testFailureSummary}`
        : `Test suite failed after max fix attempts. Last test output:\n${sanitize(allOutput.slice(-10_000))}`;
      await persistAndPostStatus(taskId, {
        task_id: taskId, status: 'FAILED', artifact_type: 'ERROR_LOG',
        logs: failedLogs,
        failure_category: 'test_failed',
        changed_files: changedPaths.slice(0, 50),
        ...(diffSummary.length > 0 ? { diff_summary: diffSummary } : {}),
        ...(testFailureSummary ? { test_failure_summary: testFailureSummary.slice(0, 5000) } : {}),
      });
      return;
    }

    // ── Self-Review Phase ──────────────────────────────────────────────────────
    // Runs after lint/test validation, before committing, to catch critical issues.
    let selfReviewOutcome: {
      issues_found: boolean;
      categories: string[];
      fixes_applied: boolean;
      verdict?: string;
      check_results?: Record<string, { passed: boolean; issue?: string }>;
      checks_performed?: string[];
      files_reviewed?: number;
      truncated_files_read?: string[];
      parse_failed?: boolean;
    } | undefined;
    if (runSelfReview) {
      sendLog(ws, '\nRunning self-review...\n');
      statusRef.value = 'Running self-review...';

      // Gather diff: committed changes since base + any uncommitted changes
      const committedDiffFull = currentHead !== baseCommit
        ? git(['diff', `${baseCommit}..HEAD`])
        : '';
      const uncommittedDiffFull = git(['diff', 'HEAD']);
      const COMMITTED_LIMIT = 8000;
      const UNCOMMITTED_LIMIT = 3000;
      const committedDiff = committedDiffFull.slice(0, COMMITTED_LIMIT);
      const uncommittedDiff = uncommittedDiffFull.slice(0, UNCOMMITTED_LIMIT);
      const reviewDiff = (committedDiff + (uncommittedDiff ? '\n' + uncommittedDiff : '')).trim();

      const diffTruncated = committedDiffFull.length > COMMITTED_LIMIT || uncommittedDiffFull.length > UNCOMMITTED_LIMIT;
      let changedFiles: string[] | undefined;
      if (diffTruncated) {
        const nameOnly = git(['diff', '--name-only', `${baseCommit}..HEAD`]);
        const uncommittedNames = git(['diff', '--name-only', 'HEAD']);
        const allNames = new Set([
          ...nameOnly.split('\n'),
          ...uncommittedNames.split('\n'),
        ].filter(f => f.trim()));
        changedFiles = [...allNames];
      }

      // Pass all playbook entries — backend already filters by applicable_types,
      // so every entry is relevant to this task type.
      const playbook = task.playbook as Record<string, string> | undefined;
      const reviewPlaybook: Record<string, string> = playbook ? { ...playbook } : {};

      const selfReviewProjectCtx = task.project_context as Record<string, unknown> | undefined;
      const reviewIssueDist = selfReviewProjectCtx?.review_issue_distribution as Record<string, number> | undefined;

      const selfReviewTaskScope = (task.context as Record<string, unknown> | undefined)?.scope as string | undefined;
      const selfReviewPrompt = buildSelfReviewPrompt({
        diff: reviewDiff,
        reviewPlaybook,
        claudeMd,
        buildConfig,
        changedFiles,
        reviewIssueDistribution: reviewIssueDist,
        taskScope: selfReviewTaskScope,
        scopeOverflowWarning,
      });

      const headBeforeReview = git(['rev-parse', 'HEAD']);
      const statusBeforeReview = git(['status', '--porcelain']);

      const taskScope = (task.context as Record<string, unknown> | undefined)?.scope as string | undefined;
      const maxSelfReviewAttempts = (taskScope === 'medium' || taskScope === 'large') ? 2 : MAX_SELF_REVIEW_ATTEMPTS;
      let selfReviewFailed = false;
      let lastReviewOutput = '';
      for (let attempt = 0; attempt < maxSelfReviewAttempts; attempt++) {
        const { exitCode: reviewExitCode, allOutput: reviewOutput } = await runClaudeCode(selfReviewPrompt, ws, statusRef, model, effort);
        if (reviewExitCode !== 0) {
          if (matchesRateLimit(reviewOutput)) {
            sendLog(ws, '\nSelf-review: rate limit encountered, skipping.\n');
          } else {
            sendLog(ws, `\nWARNING: Self-review could not complete (exit code ${reviewExitCode}), proceeding without review.\n`);
          }
          selfReviewFailed = true;
          break;
        }
        lastReviewOutput = reviewOutput;
      }

      if (selfReviewFailed) {
        selfReviewOutcome = { issues_found: false, categories: ['self_review_failed'], fixes_applied: false };
      } else {
        const headAfterReview = git(['rev-parse', 'HEAD']);
        const statusAfterReview = git(['status', '--porcelain']);
        const fixesApplied = headAfterReview !== headBeforeReview || statusAfterReview !== statusBeforeReview;

        // Parse structured JSON output from self-review
        const reviewCategories: string[] = [];
        let parsedVerdict: string | undefined;
        let parsedChecks: string[] | undefined;
        let parsedFilesReviewed: number | undefined;
        let parsedTruncatedFiles: string[] | undefined;
        let parseFailed = false;

        const parsed = extractSelfReviewJson(lastReviewOutput);
        let parsedCheckResults: Record<string, { passed: boolean; issue?: string }> | undefined;
        if (!parsed) {
          reviewCategories.push('self_review_failed', 'json_parse_failed');
          parseFailed = true;
        } else {
          parsedVerdict = typeof parsed.verdict === 'string' ? parsed.verdict : undefined;
          parsedChecks = Array.isArray(parsed.checks_performed) ? parsed.checks_performed as string[] : undefined;
          parsedFilesReviewed = typeof parsed.files_reviewed === 'number' ? parsed.files_reviewed : undefined;
          parsedTruncatedFiles = Array.isArray(parsed.truncated_files_read) ? parsed.truncated_files_read as string[] : [];

          // Extract check_results if present
          if (parsed.check_results && typeof parsed.check_results === 'object' && !Array.isArray(parsed.check_results)) {
            parsedCheckResults = parsed.check_results as Record<string, { passed: boolean; issue?: string }>;
            // Derive checks_performed from check_results keys if not already set
            if (!parsedChecks) {
              parsedChecks = Object.keys(parsedCheckResults);
            }
          }

          // Validate checks_performed contains all mandatory items
          const mandatory = getMandatoryChecks(selfReviewTaskScope || undefined);
          if (!parsedChecks) {
            reviewCategories.push('self_review_failed', 'validation_failed');
            parseFailed = true;
          } else {
            const missing = mandatory.filter(m => !parsedChecks!.includes(m));
            if (missing.length > 0) {
              reviewCategories.push('self_review_failed', 'validation_failed');
              parseFailed = true;
            }
          }

          // When diff was truncated, validate that the agent actually read the files
          if (diffTruncated && (!parsedTruncatedFiles || parsedTruncatedFiles.length === 0)) {
            reviewCategories.push('truncation_not_verified');
          }
        }

        selfReviewOutcome = {
          issues_found: fixesApplied || (typeof parsed?.issues_found === 'number' && (parsed.issues_found as number) > 0),
          categories: reviewCategories,
          fixes_applied: fixesApplied,
          ...(parsedVerdict !== undefined ? { verdict: parsedVerdict } : {}),
          ...(parsedCheckResults !== undefined ? { check_results: parsedCheckResults } : {}),
          ...(parsedChecks !== undefined ? { checks_performed: parsedChecks } : {}),
          ...(parsedFilesReviewed !== undefined ? { files_reviewed: parsedFilesReviewed } : {}),
          ...(parsedTruncatedFiles !== undefined ? { truncated_files_read: parsedTruncatedFiles } : {}),
          ...(parseFailed ? { parse_failed: true } : {}),
        };
        sendLog(ws, fixesApplied ? '\nSelf-review: issues found and fixed.\n' : '\nSelf-review: no critical issues found.\n');
        if (fixesApplied) {
          try {
            runProjectLintChecks(changedPaths, ws, buildConfig);
          } catch (lintErr: unknown) {
            const msg = lintErr instanceof Error ? lintErr.message : String(lintErr);
            sendLog(ws, `\nWARNING: Lint check after self-review fixes failed — proceeding anyway:\n${msg.slice(0, 2000)}\n`);
          }
        }
      }
    }

    git(['add', '-A']);

    // ── Guard 1: File restriction violations ────────────────────────────────
    // Parse restricted paths from CLAUDE.md and unstage any violations before commit.
    {
      const claudeMdContent = readClaudeMd();
      if (claudeMdContent) {
        const restrictedPaths: string[] = [];
        for (const line of claudeMdContent.split('\n')) {
          // Match lines like: "- Do NOT modify files in `.github/workflows/`"
          // or "- Do NOT modify `backend/alembic/` migration files"
          const match = line.match(/Do NOT modify.*?`([^`]+)`/i);
          if (match) restrictedPaths.push(match[1]);
        }
        if (restrictedPaths.length > 0) {
          let stagedFiles: string[];
          try {
            stagedFiles = execFileSync('git', ['-C', WORKSPACE, 'diff', '--name-only', '--cached'], { encoding: 'utf-8' })
              .trim().split('\n').filter(Boolean);
          } catch { stagedFiles = []; }
          const violations = stagedFiles.filter(f => restrictedPaths.some(p => f.startsWith(p)));
          if (violations.length > 0) {
            sendLog(ws, `WARNING: Unstaging ${violations.length} restricted file(s) that must not be modified:\n${violations.join('\n')}\n`);
            for (const v of violations) {
              try { execFileSync('git', ['-C', WORKSPACE, 'restore', '--staged', v], { encoding: 'utf-8' }); } catch { /* ignore */ }
            }
          }
        }
      }
    }

    // ── Guard 2: Conflict marker detection ──────────────────────────────────
    // Detect conflict markers in staged files before committing.
    {
      try {
        const conflictFiles = execFileSync('git', ['-C', WORKSPACE, 'grep', '-rl', '<' + '<<<<<<', '--cached', '--', '*.ts', '*.tsx', '*.py', '*.js', '*.jsx', '*.json'], { encoding: 'utf-8', timeout: 10_000 }).trim();
        if (conflictFiles) {
          sendLog(ws, `WARNING: Conflict markers detected in staged files:\n${conflictFiles}\nAsking Claude to resolve...\n`);
          await runClaudeCode(
            `CRITICAL: These staged files contain git conflict markers (${'<'.repeat(7)}, ${'='.repeat(7)}, ${'>'.repeat(7)}):\n${conflictFiles}\n\nResolve ALL conflict markers in every file listed before the commit. After resolving, stage the fixed files with git add.`,
            ws, statusRef, model, effort
          );
          git(['add', '-A']);
        }
      } catch { /* grep exit 1 means no matches — good */ }
    }

    const commitNote = testFailuresDetected
      ? `\n\nNote: test failures detected, manual review recommended${testFailureSummary ? `:\n${testFailureSummary.slice(0, 500)}` : ''}`
      : '';
    // Only create a new commit if there are staged changes — Claude may have
    // already committed, and lint/test fix loops may not have added new changes.
    const staged = git(['status', '--porcelain']);
    if (staged.length > 0) {
      execFileSync('git', ['-C', WORKSPACE, 'commit', '-m', `${commitPrefix}: ${taskTitle}\n\nAutomated by Blueprint Agent (task: ${shortId})${commitNote}`], { encoding: 'utf-8' });
    }

    let prBody = `## Summary\n\n${lastResultText.slice(0, 2000) || `Automated changes for: ${taskTitle}`}\n\n`;
    if (runTestFix) {
      const testSummary = testsPassed ? '\u2705 Tests passed' : testFailuresDetected ? '\u26a0\ufe0f Test failures detected' : '\u26a0\ufe0f Could not run tests';
      const testDetail = !testsPassed && testFailureSummary ? `\n\n<details><summary>Failing tests</summary>\n\n\`\`\`\n${testFailureSummary.slice(0, 1000)}\n\`\`\`\n</details>` : '';
      prBody += `## Test Results\n\n${testSummary}${testDetail}\n\n`;
    }
    if (scopeOverflowWarning) {
      prBody += `## ⚠️ Scope Overflow Warning\n\n${scopeOverflowWarning}\n\nPlease verify that all changed files are necessary for this task.\n\n`;
    }
    if (selfReviewOutcome) {
      const reviewStatus = selfReviewOutcome.fixes_applied
        ? '\u26a0\ufe0f Issues found and auto-fixed'
        : '\u2705 No critical issues found';
      prBody += `## Self-Review\n\n${reviewStatus}\n\n`;
    }
    if (issueReference) {
      prBody += `${issueReference}\n\n`;
    }
    prBody += `---\n*Automated by Blueprint Agent*`;

    const prUrl = await pushAndCreatePR(owner, repo, taskId, workBranch, baseBranch, `${prTitlePrefix} ${taskTitle}`, prBody, ws, statusRef);
    const commitHash = git(['rev-parse', 'HEAD']);

    await persistAndPostStatus(taskId, {
      task_id: taskId, status: 'COMPLETED', artifact_type: 'PULL_REQUEST',
      commit_hash: commitHash, pr_url: prUrl, repo_url: `https://github.com/${owner}/${repo}`,
      changed_files: changedPaths.slice(0, 50),
      ...(diffSummary.length > 0 ? { diff_summary: diffSummary } : {}),
      ...(selfReviewOutcome ? { self_review_outcome: selfReviewOutcome } : {}),
      ...(testFailureSummary ? { test_failure_summary: testFailureSummary.slice(0, 5000) } : {}),
    });
  } else if (exitCode === 0) {
    await persistAndPostStatus(taskId, { task_id: taskId, status: 'COMPLETED', logs: noChangesMessage });
  } else {
    await persistAndPostStatus(taskId, {
      task_id: taskId, status: 'FAILED', artifact_type: 'ERROR_LOG', logs: sanitize(allOutput.slice(-10_000)),
      failure_category: 'claude_code_failed',
      ...(fileChangeDetails.length > 0 ? { file_change_details: fileChangeDetails.slice(0, 50) } : {}),
    });
  }
}

// ── Handler: Create (default — make changes, push, open PR) ─────────────────
export async function handleCreate(
  taskId: string, payload: Record<string, unknown>, task: Record<string, unknown>,
  ghToken: string, owner: string, repo: string, ws: WebSocket | null, statusRef: { value: string },
  model?: string, effort?: string
) {
  const rawPrompt = strVal(payload.prompt) || strVal(payload.description) || strVal(task.description);
  // Include title as context so the agent understands intent even if the prompt is terse
  const taskTitleStr = strVal(task.title);
  const prompt = taskTitleStr ? `Task: ${taskTitleStr}\n\n${rawPrompt}` : rawPrompt;
  if (!prompt) throw new Error('No prompt found in task payloads');

  const taskCtx = task.context as Record<string, unknown> | undefined;
  const issueNumber = taskCtx?.issue_number as number | undefined;
  const issueUrl = taskCtx?.issue_url as string | undefined;
  const issueReference = issueNumber != null
    ? `Closes ${issueUrl || ('#' + issueNumber)}`
    : undefined;

  const projectCtx = task.project_context as Record<string, unknown> | undefined;
  const siblingTasks = projectCtx?.sibling_tasks;
  const siblingNote = Array.isArray(siblingTasks) && siblingTasks.length > 0
    ? '\n\nNote: Check the Sibling Tasks listed above and avoid making changes that overlap with or duplicate files they are already working on.'
    : '';

  const overlapWarnings = (task.context as Record<string, unknown> | undefined)?.file_overlap_warnings as Array<{shared_files: string[], with_task: string}> | undefined;
  const overlapWarningText = overlapWarnings && overlapWarnings.length > 0
    ? overlapWarnings.map(w =>
        `WARNING: This task shares files (${w.shared_files.join(', ')}) with parallel task "${w.with_task}". Coordinate carefully or check if that task already addressed this area.`
      ).join('\n') + '\n\n'
    : '';

  const relatedFiles = (
    (payload.related_files as string[] | undefined) ||
    ((task.context as Record<string, unknown> | undefined)?.related_files as string[] | undefined) ||
    []
  );
  const createBuildConfig = resolveBuildConfig(task);
  const relatedTestContext = discoverRelatedTests(ws, relatedFiles, createBuildConfig);
  const createScope = taskCtx?.scope as string | undefined;
  const isMediumOrLargeScope = createScope === 'medium' || createScope === 'large';
  const moduleInterfaceContext = isMediumOrLargeScope
    ? extractModuleInterfaces(ws, relatedFiles)
    : '';
  const affectedConsumersContext = isMediumOrLargeScope
    ? discoverAffectedConsumers(ws, relatedFiles)
    : '';
  const mediumScopePlanningNote = isMediumOrLargeScope
    ? '\n\n## Pre-Execution Planning\n\nBefore writing any code: (1) Identify which files need to change and why. (2) Consider dependency order — make foundational changes first (e.g., models before services, shared types before consumers). (3) Run the relevant test suite to establish a baseline. (4) Execute changes methodically, verifying each step builds/compiles before moving on.\n'
    : '';

  await runStandardHandler({
    taskId, payload, task, ghToken, owner, repo, ws, statusRef, model, effort,
    branchPrefix: 'task',
    buildPrompt: ({ contextPrefix, parentCtx }) => `${contextPrefix}${parentCtx}${moduleInterfaceContext}${affectedConsumersContext}${relatedTestContext}${overlapWarningText}${mediumScopePlanningNote}${prompt}${siblingNote}`,
    commitPrefix: 'blueprint',
    prTitlePrefix: '[Blueprint]',
    noChangesMessage: 'No file changes.',
    runLintFix: true,
    runTestFix: true,
    failOnTestFailure: true,
    captureBaseline: isMediumOrLargeScope,
    runSelfReview: true,
    lintContext: 'the code you just wrote',
    includeTaskTypeHints: true,
    issueReference,
    relatedFiles,
  });
}

// ── Handler: Refactor (improve code quality without changing behavior) ────────
export async function handleRefactor(
  taskId: string, payload: Record<string, unknown>, task: Record<string, unknown>,
  ghToken: string, owner: string, repo: string, ws: WebSocket | null, statusRef: { value: string },
  model?: string, effort?: string
) {
  const rawPrompt = strVal(payload.prompt) || strVal(payload.description) || strVal(task.description);
  if (!rawPrompt) throw new Error('No prompt found in task payloads');

  const refactorOverlapWarnings = (task.context as Record<string, unknown> | undefined)?.file_overlap_warnings as Array<{shared_files: string[], with_task: string}> | undefined;
  const refactorOverlapWarningText = refactorOverlapWarnings && refactorOverlapWarnings.length > 0
    ? refactorOverlapWarnings.map(w =>
        `WARNING: This task shares files (${w.shared_files.join(', ')}) with parallel task "${w.with_task}". Coordinate carefully or check if that task already addressed this area.`
      ).join('\n') + '\n\n'
    : '';

  const refactorRelatedFiles = (
    (payload.related_files as string[] | undefined) ||
    ((task.context as Record<string, unknown> | undefined)?.related_files as string[] | undefined) ||
    []
  );
  const refactorBuildConfig = resolveBuildConfig(task);
  const refactorRelatedTestContext = discoverRelatedTests(ws, refactorRelatedFiles, refactorBuildConfig);
  const refactorScope = strVal((task.context as Record<string, unknown> | undefined)?.scope);
  const refactorModuleInterfaceContext = (refactorScope === 'medium' || refactorScope === 'large')
    ? extractModuleInterfaces(ws, refactorRelatedFiles)
    : '';
  const refactorAffectedConsumersContext = (refactorScope === 'medium' || refactorScope === 'large')
    ? discoverAffectedConsumers(ws, refactorRelatedFiles)
    : '';

  const refactorTitle = strVal(task.title, 'Refactor');
  await runStandardHandler({
    taskId, payload, task, ghToken, owner, repo, ws, statusRef, model, effort,
    branchPrefix: 'refactor',
    buildPrompt: ({ contextPrefix, rawPrompt: rp }) =>
      `${contextPrefix}${refactorModuleInterfaceContext}${refactorAffectedConsumersContext}${refactorRelatedTestContext}${refactorOverlapWarningText}REFACTOR TASK — STRICT CONSTRAINTS:
- Do NOT add new features or new behavior.
- Do NOT change any observable behavior, API contracts, or public interfaces.
- Focus on ONE refactor concern per task (e.g. extract functions, reduce duplication, improve naming, simplify logic, or remove dead code — pick the most impactful single concern).
- Run the existing test suite BEFORE making any changes to establish a baseline. If tests fail before you start, report them and do not proceed.
- Make your refactor changes.
- Run the test suite AGAIN after your changes. All tests that passed before must still pass. If any test regresses, revert or fix the regression before committing.
- Keep the diff minimal — only change what is necessary for the chosen refactor concern.

Task: ${refactorTitle}

${rp}`,
    commitPrefix: 'refactor',
    prTitlePrefix: 'refactor:',
    noChangesMessage: 'No file changes.',
    runLintFix: true,
    runTestFix: true,
    runSelfReview: true,
    lintContext: 'the refactored code',
  });
}

// ── Handler: Docs (analyze codebase, generate/update documentation, open PR) ─
export async function handleDocs(
  taskId: string, payload: Record<string, unknown>, task: Record<string, unknown>,
  ghToken: string, owner: string, repo: string, ws: WebSocket | null, statusRef: { value: string },
  model?: string, effort?: string
) {
  await runStandardHandler({
    taskId, payload, task, ghToken, owner, repo, ws, statusRef, model, effort,
    branchPrefix: 'docs',
    buildPrompt: () => `You are a documentation agent. Your job is to improve the documentation of this codebase.

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

Be thorough but precise. Every statement in the documentation must reflect the actual code.`,
    commitPrefix: 'docs',
    prTitlePrefix: 'docs:',
    noChangesMessage: 'No documentation changes needed.',
    runLintFix: false,
    runTestFix: false,
  });
}

// ── Handler: QA (analyze coverage, generate tests, open PR) ─────────────────
export async function handleQA(
  taskId: string, payload: Record<string, unknown>, task: Record<string, unknown>,
  ghToken: string, owner: string, repo: string, ws: WebSocket | null, statusRef: { value: string },
  model?: string, effort?: string
) {
  await runStandardHandler({
    taskId, payload, task, ghToken, owner, repo, ws, statusRef, model, effort,
    branchPrefix: 'task',
    buildPrompt: ({ contextPrefix, parentCtx, rawPrompt }) =>
      `${contextPrefix}${parentCtx}You are a QA agent. Your job is to improve test coverage for this codebase.

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
- If a test requires mocking, use the same mocking approach as existing tests`,
    commitPrefix: 'test',
    prTitlePrefix: 'test: add test coverage for',
    noChangesMessage: 'No test files added.',
    runLintFix: true,
    runTestFix: false,
    runSelfReview: true,
  });
}

// ── Handler: Deps (update dependencies to latest minor/patch versions) ─────
export async function handleDeps(
  taskId: string, payload: Record<string, unknown>, task: Record<string, unknown>,
  ghToken: string, owner: string, repo: string, ws: WebSocket | null, statusRef: { value: string },
  model?: string, effort?: string
) {
  await runStandardHandler({
    taskId, payload, task, ghToken, owner, repo, ws, statusRef, model, effort,
    branchPrefix: 'deps',
    buildPrompt: () => `You are a dependency update agent. Your job is to update this project's dependencies to their latest MINOR and PATCH versions — do NOT make major version bumps as those can introduce breaking changes.

Steps to follow:
1. Detect which package managers are in use by checking for these files:
   - package.json \u2192 npm/yarn/pnpm
   - requirements.txt \u2192 pip
   - pyproject.toml \u2192 poetry or pip
   - Cargo.toml \u2192 cargo
   - go.mod \u2192 go mod
   - Gemfile \u2192 bundler

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

IMPORTANT: Only update minor/patch versions. If a package would require a major version bump to get any update, skip it. Focus on security patches and bug fixes.`,
    commitPrefix: 'chore',
    prTitlePrefix: 'chore:',
    noChangesMessage: 'No dependency updates needed.',
    runLintFix: false,
    runTestFix: false,
  });
}
