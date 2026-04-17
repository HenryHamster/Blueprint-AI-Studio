import { execFileSync } from 'child_process';
import type WebSocket from 'ws';
import {
  WORKSPACE, strVal, sanitize, sendLog, validateBranchName,
  persistAndPostStatus, git, setupWorkspace, pushAndCreatePR,
  runClaudeCode, lintFixLoop, testFixLoop, captureTestBaseline, matchesRateLimit,
  runProjectLintChecks, condensedAnalysisSections,
} from '../index.js';
import {
  getTaskTypeHints, DEFAULT_TOOL_GUIDANCE, PLAYBOOK_GUIDANCE, FIRST_TASK_GUIDANCE,
  readClaudeMd, buildContextPrefix, buildFileTree, discoverRelatedTests, configureAgentPermissions,
  writePlaybookHelper, writeMessagesHelper, formatPlaybook, buildRetryContext,
  buildSelfReviewPrompt, buildAnalysisPrompt, parseAnalysisPlanFiles, parseAnalysisPlanTestFiles, extractModuleInterfaces, getMandatoryChecks,
  discoverAffectedConsumers, type PlaybookEntry,
} from '../prompt.js';
import { resolveBuildConfig } from '../build-config.js';
import { SCOPE_OVERFLOW_THRESHOLDS, MEDIUM_ANALYSIS_PLAN_FILE_LIMIT } from '../constants.js';

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

/**
 * Returns true if the path looks like a test file. Test files are excluded
 * from scope overflow counts since they are essential for merge quality and
 * should not penalise well-tested tasks.
 */
function isTestFile(path: string): boolean {
  if (!path) return false;
  const segments = path.split('/');
  if (segments.includes('testing') || segments.includes('__tests__')) return true;
  const base = segments[segments.length - 1];
  if (/^test_/.test(base)) return true;
  if (/\.(test|spec)\.(ts|tsx|js|jsx|mjs|cjs)$/i.test(base)) return true;
  return false;
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
  runSmallScopeAnalysis?: boolean;
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
    runSmallScopeAnalysis = false,
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
  const playbookSection = formatPlaybook(task.playbook as Record<string, PlaybookEntry | string> | undefined);
  const retryContext = buildRetryContext(task);
  const firstTaskSection = task.first_task_on_repo ? FIRST_TASK_GUIDANCE : '';
  const finalPrompt = claudeMdPrefix + playbookSection + retryContext + buildPrompt({ contextPrefix, parentCtx, rawPrompt }) + taskTypeHint + firstTaskSection + PLAYBOOK_GUIDANCE + `\nNote: If lint or test fixes required non-obvious steps, save them to the playbook (e.g., ./playbook set "lint-gotcha" "description").\n` + `\n\n${DEFAULT_TOOL_GUIDANCE}` + promptSuffix;

  const { workBranch, shortId } = await setupWorkspace(owner, repo, ghToken, baseBranch, branchPrefix, taskId, ws, statusRef, buildConfig);
  writePlaybookHelper(taskId);
  writeMessagesHelper(taskId);
  const disallowedTools = configureAgentPermissions(taskTypeKey);

  // Capture test baseline on main branch so the test fix loop can distinguish
  // pre-existing failures from newly-broken tests. Scoped to related_files to stay fast.
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
  const previousAnalysisPlan = strVal(ctx._previous_analysis_plan as unknown);
  if (taskScope === 'medium' && previousAnalysisPlan) {
    // Retry with an existing plan: reuse it instead of burning another ~3-5 min
    // Claude Code session re-deriving the same analysis.
    sendLog(ws, '\n[Analysis Phase] Reusing plan from previous attempt — skipping re-analysis.\n');
    const trimmed = previousAnalysisPlan.trim();
    analysisContext = trimmed.startsWith('## Pre-Implementation Analysis')
      ? `${trimmed}\n\n`
      : `## Pre-Implementation Analysis\n\n${trimmed}\n\n`;
  } else if (taskScope === 'medium') {
    sendLog(ws, '\n[Analysis Phase] Mapping module interfaces and dependencies...\n');
    statusRef.value = 'Analyzing module interfaces...';

    const analysisRelatedFiles = (
      (payload.related_files as string[] | undefined) ||
      (ctx.related_files as string[] | undefined) ||
      []
    ) as string[];

    const totalFileBudget = SCOPE_OVERFLOW_THRESHOLDS.medium;
    const analysisPrompt = buildAnalysisPrompt({
      rawPrompt,
      taskTitle,
      relatedFiles: analysisRelatedFiles,
      fileTree,
      fileLimit: MEDIUM_ANALYSIS_PLAN_FILE_LIMIT,
      totalFileBudget,
    });

    const { exitCode: analysisExitCode, lastResultText: analysisResult } = await runClaudeCode(
      analysisPrompt,
      ws, statusRef, model, effort,
      ['Write', 'Edit', 'NotebookEdit'],
    );

    if (analysisExitCode === 0 && analysisResult.trim()) {
      let effectiveAnalysis = analysisResult.trim();
      let plannedFiles = parseAnalysisPlanFiles(effectiveAnalysis);
      let plannedTestFiles = parseAnalysisPlanTestFiles(effectiveAnalysis);
      let totalPlanned = plannedFiles.length + plannedTestFiles.length;

      // Scope feasibility gate: if the analysis plans more production files
      // than the medium-scope limit, OR the combined production+test count
      // exceeds the scope_overflow threshold, retry the analysis once with
      // explicit narrowing guidance. If still over budget, fail fast — the
      // implementation pass would otherwise burn ~5-10 minutes only to hit
      // scope_overflow after the agent touches both production and test files.
      const exceedsProdLimit = plannedFiles.length > MEDIUM_ANALYSIS_PLAN_FILE_LIMIT;
      const exceedsTotalBudget = totalPlanned > totalFileBudget;
      if (exceedsProdLimit || exceedsTotalBudget) {
        const reason = exceedsTotalBudget
          ? `combined plan lists ${plannedFiles.length} production + ${plannedTestFiles.length} test = ${totalPlanned} files (total budget ${totalFileBudget})`
          : `plan lists ${plannedFiles.length} files (limit ${MEDIUM_ANALYSIS_PLAN_FILE_LIMIT})`;
        sendLog(ws, `[Analysis Phase] ${reason}; retrying with narrowing directive.\n`);
        const testFilesBlock = plannedTestFiles.length > 0
          ? `\nPreviously planned test files:\n${plannedTestFiles.map(f => `- ${f}`).join('\n')}`
          : '';
        const narrowingGuidance = `The previous analysis pass planned ${plannedFiles.length} production files plus ${plannedTestFiles.length} test files (${totalPlanned} total). Production limit is ${MEDIUM_ANALYSIS_PLAN_FILE_LIMIT}; combined budget (production + tests) is ${totalFileBudget}. Cut the plan down to the minimum viable change set within both limits. Drop any files that are nice-to-have refactors, peripheral cleanups, or follow-on test/doc updates that could ship in a separate task. In **Implementation approach**, briefly note what was deferred so the next task can pick it up. Previously planned production files:\n${plannedFiles.map(f => `- ${f}`).join('\n')}${testFilesBlock}`;
        const retryPrompt = buildAnalysisPrompt({
          rawPrompt,
          taskTitle,
          relatedFiles: analysisRelatedFiles,
          fileTree,
          fileLimit: MEDIUM_ANALYSIS_PLAN_FILE_LIMIT,
          totalFileBudget,
          narrowingGuidance,
        });
        const { exitCode: retryExitCode, lastResultText: retryResult } = await runClaudeCode(
          retryPrompt,
          ws, statusRef, model, effort,
          ['Write', 'Edit', 'NotebookEdit'],
        );
        if (retryExitCode === 0 && retryResult.trim()) {
          effectiveAnalysis = retryResult.trim();
          plannedFiles = parseAnalysisPlanFiles(effectiveAnalysis);
          plannedTestFiles = parseAnalysisPlanTestFiles(effectiveAnalysis);
          totalPlanned = plannedFiles.length + plannedTestFiles.length;
        }

        const stillExceedsProd = plannedFiles.length > MEDIUM_ANALYSIS_PLAN_FILE_LIMIT;
        const stillExceedsTotal = totalPlanned > totalFileBudget;
        if (stillExceedsProd || stillExceedsTotal) {
          const fileList = plannedFiles.map(f => `- ${f}`).join('\n');
          const testList = plannedTestFiles.length > 0
            ? `\n\nPlanned test files:\n${plannedTestFiles.map(f => `- ${f}`).join('\n')}`
            : '';
          const failureLog = `Pre-implementation analysis planned ${plannedFiles.length} production + ${plannedTestFiles.length} test = ${totalPlanned} files, exceeding the medium-scope budget (production limit ${MEDIUM_ANALYSIS_PLAN_FILE_LIMIT}, combined budget ${totalFileBudget}). The narrowing retry could not bring the plan within budget. Split this task into smaller subtasks or scope it down to a single concern before retrying.\n\nPlanned production files:\n${fileList}${testList}`;
          sendLog(ws, `[Analysis Phase] Plan still exceeds budget after retry (${plannedFiles.length} prod + ${plannedTestFiles.length} test = ${totalPlanned}) — failing fast.\n`);
          await persistAndPostStatus(taskId, {
            task_id: taskId, status: 'FAILED', artifact_type: 'ERROR_LOG',
            logs: failureLog,
            failure_category: 'scope_overflow',
            analysis_plan: effectiveAnalysis.slice(0, 5000),
          });
          return;
        }
        sendLog(ws, `[Analysis Phase] Plan narrowed to ${plannedFiles.length} production + ${plannedTestFiles.length} test = ${totalPlanned} files.\n`);
      }

      analysisContext = `## Pre-Implementation Analysis\n\n${effectiveAnalysis}\n\n`;
      sendLog(ws, '[Analysis Phase] Complete.\n');
    } else {
      sendLog(ws, '[Analysis Phase] Skipped — proceeding to implementation.\n');
    }
  } else if (taskScope === 'small' && runSmallScopeAnalysis) {
    analysisContext = `## Pre-Implementation Analysis\n\nBefore writing code: read the files relevant to this task, confirm the scope stays under ~50 lines across 1–3 files, and check for callers that import from files you will modify.\n\n`;
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
    // Test files are excluded from the count since they are essential for merge
    // quality and should not penalise well-tested tasks.
    const taskScope = strVal((task.context as Record<string, unknown> | undefined)?.scope);
    const overflowThreshold = taskScope ? SCOPE_OVERFLOW_THRESHOLDS[taskScope] : undefined;
    const nonTestChangedPaths = changedPaths.filter(p => !isTestFile(p));
    const scopeOverflowWarning = overflowThreshold !== undefined && nonTestChangedPaths.length > overflowThreshold
      ? `This task has scope "${taskScope}" (expected few files) but ${nonTestChangedPaths.length} non-test files were changed (${changedPaths.length} total including tests), exceeding the ${overflowThreshold}-file threshold. Changed files:\n${changedPaths.map(f => `- ${f}`).join('\n')}`
      : undefined;
    if (scopeOverflowWarning) {
      sendLog(ws, `\nWARNING: Scope overflow detected — ${nonTestChangedPaths.length} non-test files changed for a "${taskScope}" task (threshold: ${overflowThreshold}, ${changedPaths.length} total including tests).\n`);
    }

    // ── Scope Trim Enforcement (medium-scope only) ────────────────────────────
    // For medium tasks exceeding the file threshold, prompt the agent to evaluate
    // which files are essential and automatically revert non-essential ones before
    // lint/test/self-review. Large tasks are not subject to this enforcement.
    const scopeTrimReverted: string[] = [];
    let scopeTrimJustification: string | undefined;
    if (taskScope === 'medium' && scopeOverflowWarning) {
      sendLog(ws, '\nEnforcing scope limits — prompting agent to identify non-essential files...\n');
      statusRef.value = 'Trimming scope overflow...';

      const fileStatsLines = changedPaths.map(f => {
        const stats = diffSummary.find(d => d.file === f);
        return stats
          ? `- ${f} (+${stats.additions}/-${stats.deletions}, ${stats.change_type})`
          : `- ${f}`;
      }).join('\n');

      const plannedFilesForTrim = analysisContext ? parseAnalysisPlanFiles(analysisContext) : [];
      const planSection = plannedFilesForTrim.length > 0
        ? `\n## Pre-Implementation Plan — Files to change\n${plannedFilesForTrim.map(f => `- ${f}`).join('\n')}\n\nYour pre-implementation plan targeted these files. Implementation often reveals necessary changes that were not anticipated during planning, so treat files outside the plan neutrally — evaluate each file on its own merit based on whether it is required for the task to work correctly.\n`
        : '';

      let implementationNotes = '';
      if (currentHead !== baseCommit) {
        try {
          const commitLog = git(['log', '--format=%s%n%b%n---', `${baseCommit}..HEAD`]).trim();
          if (commitLog) {
            implementationNotes = `\n## Implementation notes — recent commit messages\n${commitLog.slice(0, 2000)}\n`;
          }
        } catch { /* ignore log errors */ }
      }

      const scopeTrimPrompt = `You are reviewing the scope of a completed implementation task to remove non-essential file changes before creating a PR.

## Original Task
${rawPrompt.slice(0, 3000)}
${planSection}${implementationNotes}
## Changed Files (${changedPaths.length} files, threshold: ${overflowThreshold})
${fileStatsLines}

## Instructions
Review each changed file and classify it as ESSENTIAL or NON-ESSENTIAL based on the original task${plannedFilesForTrim.length > 0 ? ' and pre-implementation plan' : ''} above.

A file is ESSENTIAL only if it is directly required to implement what was asked.
Revert files that are: formatting-only changes, style cleanups not requested, tangential refactors, or test files for code you did not change.

If ALL files are truly necessary for a legitimate cross-cutting change, you may keep them all but MUST provide a specific per-file justification explaining why each file is required.

Output ONLY a JSON object in a \`\`\`json code fence:
\`\`\`json
{
  "assessment": "Brief explanation of what was trimmed and why",
  "essential_files": ["file1.ts", "file2.py"],
  "files_to_revert": ["file3.ts"],
  "all_essential_justification": "Only populate if files_to_revert is empty — specific reason every file is required",
  "per_file_justification": {"file1.ts": "Required for X because...", "file2.py": "Required for Y because..."}
}
\`\`\``;

      const { exitCode: trimExitCode, allOutput: trimOutput } = await runClaudeCode(
        scopeTrimPrompt, ws, statusRef, model, effort,
        ['Write', 'Edit', 'NotebookEdit'],
      );

      if (trimExitCode === 0) {
        const trimResult = extractSelfReviewJson(trimOutput);
        const filesToRevert = Array.isArray(trimResult?.files_to_revert)
          ? (trimResult.files_to_revert as unknown[]).filter((f): f is string => typeof f === 'string')
          : [];

        if (filesToRevert.length > 0) {
          sendLog(ws, `\nReverting ${filesToRevert.length} non-essential file(s):\n`);
          for (const fileToRevert of filesToRevert) {
            if (!changedPaths.includes(fileToRevert)) continue;
            try {
              // Restore file to its state at baseCommit (works for modified files)
              execFileSync('git', ['-C', WORKSPACE, 'checkout', baseCommit, '--', fileToRevert], { encoding: 'utf-8' });
              sendLog(ws, `  Reverted: ${fileToRevert}\n`);
              scopeTrimReverted.push(fileToRevert);
            } catch {
              // File did not exist at baseCommit — it is new, remove it
              try {
                execFileSync('git', ['-C', WORKSPACE, 'rm', '-f', '--', fileToRevert], { encoding: 'utf-8' });
                sendLog(ws, `  Removed (new file): ${fileToRevert}\n`);
                scopeTrimReverted.push(fileToRevert);
              } catch {
                try {
                  execFileSync('rm', ['-f', `${WORKSPACE}/${fileToRevert}`], { encoding: 'utf-8' });
                  sendLog(ws, `  Deleted (untracked): ${fileToRevert}\n`);
                  scopeTrimReverted.push(fileToRevert);
                } catch (err3) {
                  const msg = err3 instanceof Error ? err3.message : String(err3);
                  sendLog(ws, `  WARNING: Could not revert ${fileToRevert}: ${msg}\n`);
                }
              }
            }
          }
          // Remove reverted files from changedPaths so downstream steps (lint, test, self-review) skip them
          const revertedSet = new Set(scopeTrimReverted);
          changedPaths.splice(0, changedPaths.length, ...changedPaths.filter(f => !revertedSet.has(f)));
          sendLog(ws, `Scope trim complete: ${changedPaths.length} file(s) remaining.\n`);
        } else {
          sendLog(ws, 'Scope trim: agent determined all files are essential.\n');
        }
        if (typeof trimResult?.all_essential_justification === 'string' && (trimResult.all_essential_justification as string).trim()) {
          scopeTrimJustification = trimResult.all_essential_justification as string;
        }
      } else {
        sendLog(ws, 'WARNING: Scope trim evaluation could not complete — proceeding with all files.\n');
      }
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
          ...(analysisContext ? { analysis_plan: analysisContext.slice(0, 5000) } : {}),
        });
        return;
      }
    }

    let testFailuresDetected = false;
    let testsPassed = true;
    let testFailureSummary: string | undefined;
    if (runTestFix && baseBranch === 'main') {
      const result = await testFixLoop(ws, statusRef, model, effort, buildConfig, baselineFailures, relatedFiles, analysisContext || undefined);
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
        ...(analysisContext ? { analysis_plan: analysisContext.slice(0, 5000) } : {}),
      });
      return;
    }

    // ── Scope overflow gate (small-scope tasks only) ─────────────────────────────
    // Small tasks that touch too many files waste review cycles — fail fast so the
    // retry can focus on fewer files. Medium tasks legitimately touch more files, so
    // keep the existing warning-only behaviour for them.
    if (taskScope === 'small' && scopeOverflowWarning) {
      sendLog(ws, `\nScope overflow: failing small-scope task — ${nonTestChangedPaths.length} non-test files changed exceeds the ${overflowThreshold}-file threshold. Retry should focus on the single most critical change and limit to 1-3 files.\n`);
      await persistAndPostStatus(taskId, {
        task_id: taskId, status: 'FAILED', artifact_type: 'ERROR_LOG',
        logs: `Scope overflow: ${nonTestChangedPaths.length} non-test files changed for a small-scope task (threshold: ${overflowThreshold}, ${changedPaths.length} total including tests). Retry guidance: focus on the single most critical change and limit to 1-3 files.\n\nChanged files:\n${changedPaths.map(f => `- ${f}`).join('\n')}`,
        failure_category: 'scope_overflow',
        changed_files: changedPaths.slice(0, 50),
        ...(diffSummary.length > 0 ? { diff_summary: diffSummary } : {}),
      });
      return;
    }

    // ── Self-Review Phase ──────────────────────────────────────────────────────
    // Runs after lint/test validation, before committing, to catch critical issues.
    let selfReviewOutcome: {
      issues_found: boolean;
      categories: string[];
      fixes_applied: boolean;
      passes: number;
      total_fixes: number;
      verdict?: string;
      check_results?: Record<string, { passed: boolean; issue?: string }>;
      checks_performed?: string[];
      files_reviewed?: number;
      truncated_files_read?: string[];
      parse_failed?: boolean;
      feedback_categories_injected?: string[];
    } | undefined;
    if (runSelfReview) {
      sendLog(ws, '\nRunning self-review...\n');
      statusRef.value = 'Running self-review...';

      // Gather diff: committed changes since base + any uncommitted changes
      const committedDiffFull = currentHead !== baseCommit
        ? git(['diff', `${baseCommit}..HEAD`])
        : '';
      const uncommittedDiffFull = git(['diff', 'HEAD']);
      // Diff budget scales with scope so medium/large tasks (12-24k+ diffs)
      // aren't truncated on virtually every run, which wasted review cycles by
      // sending unverified changes (via the forced-PASS path) to PR review.
      const COMMITTED_LIMIT = taskScope === 'large' ? 40000 : taskScope === 'medium' ? 24000 : 8000;
      const UNCOMMITTED_LIMIT = taskScope === 'large' ? 15000 : taskScope === 'medium' ? 9000 : 3000;
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
      const playbook = task.playbook as Record<string, PlaybookEntry | string> | undefined;
      const reviewPlaybook: Record<string, string> = playbook
        ? Object.fromEntries(Object.entries(playbook).map(([k, v]) =>
            [k, typeof v === 'string' ? v : v.content]))
        : {};

      const selfReviewProjectCtx = task.project_context as Record<string, unknown> | undefined;
      const reviewIssueDist = selfReviewProjectCtx?.review_issue_distribution as Record<string, number> | undefined;
      const feedbackCategoriesInjected: string[] | undefined = reviewIssueDist && Object.keys(reviewIssueDist).length > 0
        ? Object.keys(reviewIssueDist)
        : undefined;
      const selfReviewPipelineHealth = selfReviewProjectCtx?.pipeline_health as Record<string, unknown> | undefined;
      const selfReviewFasr = selfReviewPipelineHealth?.first_attempt_success_rate as Record<string, unknown> | undefined;
      const firstAttemptByCat = selfReviewFasr?.first_attempt_by_review_category as Record<string, number> | undefined;
      const persistentReviewIssues = selfReviewPipelineHealth?.persistent_review_issues as string[] | undefined;
      const topFirstReviewIssues = selfReviewProjectCtx?.top_first_review_issues as
        | Array<{ description: string; count: number; category: string }>
        | undefined;
      const scopeSpecificReviewIssues = selfReviewProjectCtx?.scope_specific_review_issues as
        | Array<{ description: string; count: number; category: string; scope: string }>
        | undefined;

      const selfReviewTaskScope = (task.context as Record<string, unknown> | undefined)?.scope as string | undefined;
      const selfReviewAnalysisPlan = selfReviewTaskScope === 'medium' && analysisContext
        ? condensedAnalysisSections(analysisContext)
        : undefined;

      // Plan completion: compare planned file lists against actual changed files so
      // the self-review can flag planned files that were skipped in implementation.
      let planCompletion: {
        plannedProductionCount: number;
        changedPlannedProductionCount: number;
        plannedTestCount: number;
        changedPlannedTestCount: number;
        missingProduction: string[];
        missingTest: string[];
      } | undefined;
      if (selfReviewTaskScope === 'medium' && analysisContext) {
        const plannedProduction = parseAnalysisPlanFiles(analysisContext);
        const plannedTest = parseAnalysisPlanTestFiles(analysisContext);
        if (plannedProduction.length > 0 || plannedTest.length > 0) {
          const changedSet = new Set(changedPaths);
          const missingProduction = plannedProduction.filter(f => !changedSet.has(f));
          const missingTest = plannedTest.filter(f => !changedSet.has(f));
          planCompletion = {
            plannedProductionCount: plannedProduction.length,
            changedPlannedProductionCount: plannedProduction.length - missingProduction.length,
            plannedTestCount: plannedTest.length,
            changedPlannedTestCount: plannedTest.length - missingTest.length,
            missingProduction,
            missingTest,
          };
        }
      }

      const selfReviewPrompt = buildSelfReviewPrompt({
        diff: reviewDiff,
        reviewPlaybook,
        claudeMd,
        buildConfig,
        changedFiles,
        reviewIssueDistribution: reviewIssueDist,
        taskScope: selfReviewTaskScope,
        scopeOverflowWarning,
        firstAttemptByCategory: firstAttemptByCat,
        persistentReviewIssues,
        analysisPlan: selfReviewAnalysisPlan,
        planCompletion,
        topFirstReviewIssues,
        scopeSpecificReviewIssues,
      });

      const headBeforeReview = git(['rev-parse', 'HEAD']);
      const statusBeforeReview = git(['status', '--porcelain']);

      let selfReviewFailed = false;
      let lastReviewOutput = '';
      // lastResultText is the parsed text from the `result` stream-json event —
      // it has real newlines, so ```json fences parse cleanly.  allOutput is
      // the raw stream with JSON-escape sequences (literal \n, \") which breaks
      // extractSelfReviewJson's fence regex.
      let lastReviewResult = '';
      let passes = 0;
      let totalFixes = 0;

      // Pass 1: always run self-review once
      passes++;
      const { exitCode: pass1ExitCode, allOutput: pass1Output, lastResultText: pass1Result } = await runClaudeCode(selfReviewPrompt, ws, statusRef, model, effort);
      if (pass1ExitCode !== 0) {
        if (matchesRateLimit(pass1Output)) {
          sendLog(ws, '\nSelf-review: rate limit encountered, skipping.\n');
        } else {
          sendLog(ws, `\nWARNING: Self-review could not complete (exit code ${pass1ExitCode}), proceeding without review.\n`);
        }
        selfReviewFailed = true;
      } else {
        lastReviewOutput = pass1Output;
        lastReviewResult = pass1Result;
        const headAfterPass1 = git(['rev-parse', 'HEAD']);
        const statusAfterPass1 = git(['status', '--porcelain']);
        const pass1FixesApplied = headAfterPass1 !== headBeforeReview || statusAfterPass1 !== statusBeforeReview;
        if (pass1FixesApplied) {
          totalFixes++;
          // Pass 2: verification pass when pass 1 applied fixes
          passes++;
          const { exitCode: pass2ExitCode, allOutput: pass2Output, lastResultText: pass2Result } = await runClaudeCode(selfReviewPrompt, ws, statusRef, model, effort);
          if (pass2ExitCode === 0) {
            lastReviewOutput = pass2Output;
            lastReviewResult = pass2Result;
            const headAfterPass2 = git(['rev-parse', 'HEAD']);
            const statusAfterPass2 = git(['status', '--porcelain']);
            if (headAfterPass2 !== headAfterPass1 || statusAfterPass2 !== statusAfterPass1) {
              totalFixes++;
            }
          }
        }
      }

      if (selfReviewFailed) {
        selfReviewOutcome = { issues_found: false, categories: ['self_review_failed'], fixes_applied: false, passes, total_fixes: totalFixes, ...(feedbackCategoriesInjected !== undefined ? { feedback_categories_injected: feedbackCategoriesInjected } : {}) };
      } else {
        const fixesApplied = totalFixes > 0;

        // Parse structured JSON output from self-review
        const reviewCategories: string[] = [];
        let parsedVerdict: string | undefined;
        let parsedChecks: string[] | undefined;
        let parsedFilesReviewed: number | undefined;
        let parsedTruncatedFiles: string[] | undefined;
        let unreviewedFiles: string[] | undefined;
        let parseFailed = false;

        // Prefer lastReviewResult (parsed `result` event text with real
        // newlines) over lastReviewOutput (raw stream-json with \n escapes).
        const parsed = extractSelfReviewJson(lastReviewResult || lastReviewOutput);
        let parsedCheckResults: Record<string, { passed: boolean; issue?: string }> | undefined;
        if (!parsed) {
          reviewCategories.push('self_review_failed', 'json_parse_failed');
          parseFailed = true;
        } else {
          parsedVerdict = typeof parsed.verdict === 'string' ? parsed.verdict : undefined;
          parsedChecks = Array.isArray(parsed.checks_performed) ? parsed.checks_performed as string[] : undefined;
          parsedFilesReviewed = typeof parsed.files_reviewed === 'number' ? parsed.files_reviewed : undefined;
          parsedTruncatedFiles = Array.isArray(parsed.truncated_files_read) ? parsed.truncated_files_read as string[] : [];

          // Extract check_results if present and derive checks_performed from
          // its keys when the agent omitted that field.
          if (parsed.check_results && typeof parsed.check_results === 'object' && !Array.isArray(parsed.check_results)) {
            parsedCheckResults = parsed.check_results as Record<string, { passed: boolean; issue?: string }>;
            // When the agent provided an explicit checks_performed array alongside
            // a verdict that contradicts check_results, that's a higher-signal
            // inconsistency worth retrying so the agent self-corrects. When
            // checks_performed was omitted, derive it silently and just flip the
            // verdict to FAIL — no retry needed for a pure formatting miss.
            const hadExplicitChecks = parsedChecks !== undefined;
            if (!parsedChecks) {
              parsedChecks = Object.keys(parsedCheckResults);
            }
            const failedCheckIds = Object.entries(parsedCheckResults)
              .filter(([, r]) => r && r.passed === false)
              .map(([id]) => id);
            if (failedCheckIds.length > 0 && parsedVerdict !== 'FAIL') {
              if (hadExplicitChecks) {
                reviewCategories.push('verdict_check_mismatch');
                parseFailed = true;
              }
              parsedVerdict = 'FAIL';
            }
          }

          // Validate checks_performed contains all mandatory items
          const mandatory = getMandatoryChecks(selfReviewTaskScope || undefined, !!selfReviewAnalysisPlan);
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

          // When diff was truncated, validate that the agent actually read the files.
          // For medium/large tasks touching N files, zero-files-read isn't the only failure:
          // the agent may read a couple and skip the rest. We require ≥80% coverage of the
          // changedFiles list; below that, retry with an explicit list of MISSING files.
          // If the retry still falls short, flag as incomplete and force PASS (downstream
          // review sees the flag + unreviewed file list).
          const TRUNCATION_COVERAGE_THRESHOLD = 0.8;
          const computeMissing = (readList: string[] | undefined): string[] => {
            if (!changedFiles || changedFiles.length === 0) return [];
            const readSet = new Set(readList ?? []);
            return changedFiles.filter(f => !readSet.has(f));
          };
          const isInsufficient = (readList: string[] | undefined): boolean => {
            if (!changedFiles || changedFiles.length === 0) {
              return !readList || readList.length === 0;
            }
            const missing = computeMissing(readList);
            const coverage = (changedFiles.length - missing.length) / changedFiles.length;
            return coverage < TRUNCATION_COVERAGE_THRESHOLD;
          };

          if (diffTruncated && isInsufficient(parsedTruncatedFiles)) {
            reviewCategories.push('truncation_not_verified');

            if (failOnTestFailure && !parseFailed) {
              const missingFiles = computeMissing(parsedTruncatedFiles);
              const fileListForRetry = missingFiles.length > 0
                ? missingFiles.map(f => `- ${f}`).join('\n')
                : (changedFiles && changedFiles.length > 0
                  ? changedFiles.map(f => `- ${f}`).join('\n')
                  : '(no file list available — use `git diff --name-only` to find changed files)');
              const gapDesc = (parsedTruncatedFiles && parsedTruncatedFiles.length > 0)
                ? `${missingFiles.length} of ${changedFiles?.length ?? '?'} file(s) unread`
                : 'no files were read';
              sendLog(ws, `\nSelf-review: diff truncated and ${gapDesc} — retrying with explicit file-read guidance...\n`);
              const truncationRetryPrompt = `${selfReviewPrompt}\n\nCRITICAL: Your previous self-review did not read all the truncated files. You MUST read each of the following MISSING files in full with the Read tool before issuing a verdict, and include them (along with any files already read) in "truncated_files_read":\n${fileListForRetry}\n\nReturn ONLY a valid JSON object with truncated_files_read populated.`;
              const { exitCode: truncRetryExit, allOutput: truncRetryOutput, lastResultText: truncRetryResult } = await runClaudeCode(
                truncationRetryPrompt, ws, statusRef, model, effort,
              );
              if (truncRetryExit === 0) {
                const truncRetryParsed = extractSelfReviewJson(truncRetryResult || truncRetryOutput);
                if (truncRetryParsed) {
                  const retryTruncatedFiles = Array.isArray(truncRetryParsed.truncated_files_read)
                    ? (truncRetryParsed.truncated_files_read as unknown[]).filter((f): f is string => typeof f === 'string')
                    : [];
                  if (retryTruncatedFiles.length > 0) {
                    // Union with pass-1 reads so the agent can't shrink coverage by
                    // omitting previously-listed files from the retry response.
                    const merged = Array.from(new Set([...(parsedTruncatedFiles ?? []), ...retryTruncatedFiles]));
                    parsedTruncatedFiles = merged;
                    const retryVerdict = typeof truncRetryParsed.verdict === 'string' ? truncRetryParsed.verdict : undefined;
                    if (retryVerdict !== undefined) parsedVerdict = retryVerdict;
                    sendLog(ws, `Self-review: truncation retry — ${merged.length} file(s) now covered.\n`);
                  }
                }
              }

              if (isInsufficient(parsedTruncatedFiles)) {
                reviewCategories.push('incomplete_review');
                parsedVerdict = 'PASS';
                const unreviewedAfter = computeMissing(parsedTruncatedFiles);
                if (unreviewedAfter.length > 0) {
                  unreviewedFiles = unreviewedAfter;
                  sendLog(ws, `Self-review: truncation retry still missing ${unreviewedAfter.length} file(s) — flagging review as incomplete and forcing PASS. Unreviewed: ${unreviewedAfter.join(', ')}\n`);
                } else {
                  sendLog(ws, 'Self-review: truncation retry still reported zero files read — flagging review as incomplete and forcing PASS.\n');
                }
              }
            }
          }
        }

        selfReviewOutcome = {
          issues_found: fixesApplied || (typeof parsed?.issues_found === 'number' && (parsed.issues_found as number) > 0),
          categories: reviewCategories,
          fixes_applied: fixesApplied,
          passes,
          total_fixes: totalFixes,
          ...(parsedVerdict !== undefined ? { verdict: parsedVerdict } : {}),
          ...(parsedCheckResults !== undefined ? { check_results: parsedCheckResults } : {}),
          ...(parsedChecks !== undefined ? { checks_performed: parsedChecks } : {}),
          ...(parsedFilesReviewed !== undefined ? { files_reviewed: parsedFilesReviewed } : {}),
          ...(parsedTruncatedFiles !== undefined ? { truncated_files_read: parsedTruncatedFiles } : {}),
          ...(unreviewedFiles !== undefined ? { unreviewed_files: unreviewedFiles } : {}),
          ...(parseFailed ? { parse_failed: true } : {}),
          ...(feedbackCategoriesInjected !== undefined ? { feedback_categories_injected: feedbackCategoriesInjected } : {}),
        };
        sendLog(ws, fixesApplied ? '\nSelf-review: issues found and fixed.\n' : '\nSelf-review: no critical issues found.\n');
        if (fixesApplied) {
          try {
            runProjectLintChecks(changedPaths, ws, buildConfig);
          } catch (lintErr: unknown) {
            const msg = lintErr instanceof Error ? lintErr.message : String(lintErr);
            if (failOnTestFailure) {
              sendLog(ws, `\nLint check after self-review fixes failed — marking task as FAILED.\n${msg.slice(0, 2000)}\n`);
              await persistAndPostStatus(taskId, {
                task_id: taskId, status: 'FAILED', artifact_type: 'ERROR_LOG',
                logs: `Lint/build checks failed after self-review fixes. See logs above for details.`,
                failure_category: 'lint_failed_after_self_review',
                changed_files: changedPaths.slice(0, 50),
                ...(diffSummary.length > 0 ? { diff_summary: diffSummary } : {}),
              });
              return;
            }
            sendLog(ws, `\nWARNING: Lint check after self-review fixes failed — proceeding anyway:\n${msg.slice(0, 2000)}\n`);
          }

          // Re-run tests after self-review fixes to catch regressions before PR creation.
          // Without this, regressions only surface in CI and spawn a fix task adding 2+ review iterations.
          if (runTestFix && failOnTestFailure && baseBranch === 'main') {
            sendLog(ws, '\nRe-running tests after self-review fixes...\n');
            statusRef.value = 'Re-running tests after self-review...';
            const postReviewTestResult = await testFixLoop(
              ws, statusRef, model, effort, buildConfig, baselineFailures, relatedFiles, analysisContext || undefined,
            );
            testsPassed = postReviewTestResult.passed;
            testFailuresDetected = postReviewTestResult.testFailuresDetected;
            if (postReviewTestResult.failureSummary) testFailureSummary = postReviewTestResult.failureSummary;
            if (postReviewTestResult.testFailuresDetected && !postReviewTestResult.passed) {
              sendLog(ws, '\nTests failed after self-review fixes — marking task as FAILED.\n');
              const failedLogs = postReviewTestResult.failureSummary
                ? `Test suite failed after self-review fixes. Failing tests:\n${postReviewTestResult.failureSummary}`
                : `Test suite failed after self-review fixes.`;
              await persistAndPostStatus(taskId, {
                task_id: taskId, status: 'FAILED', artifact_type: 'ERROR_LOG',
                logs: failedLogs,
                failure_category: 'test_failed_after_self_review',
                changed_files: changedPaths.slice(0, 50),
                ...(diffSummary.length > 0 ? { diff_summary: diffSummary } : {}),
                ...(postReviewTestResult.failureSummary ? { test_failure_summary: postReviewTestResult.failureSummary.slice(0, 5000) } : {}),
              });
              return;
            }
          }
        }

        // ── Self-Review Hard Gate (create tasks / failOnTestFailure=true only) ────────
        // Blocks PR creation when verdict=FAIL or validation is unresolvable.
        if (failOnTestFailure) {
          // Validation failure: retry once with explicit guidance about what was missing
          if (parseFailed) {
            const mandatory = getMandatoryChecks(selfReviewTaskScope || undefined, !!selfReviewAnalysisPlan);
            const missingChecks = parsedChecks
              ? mandatory.filter(m => !parsedChecks!.includes(m))
              : mandatory;
            const hadVerdictMismatch = reviewCategories.includes('verdict_check_mismatch');
            const failedCheckIds = parsedCheckResults
              ? Object.entries(parsedCheckResults).filter(([, r]) => r && r.passed === false).map(([id]) => id)
              : [];
            const retryNoteParts: string[] = [];
            if (missingChecks.length > 0) {
              retryNoteParts.push(`The checks_performed field was missing these mandatory items: ${missingChecks.join(', ')}.`);
            }
            if (hadVerdictMismatch) {
              retryNoteParts.push(`The verdict was inconsistent with check_results: ${failedCheckIds.join(', ')} had passed:false but verdict was not FAIL. When any check has passed:false, verdict MUST be FAIL and issues_found MUST be ≥1.`);
            }
            if (retryNoteParts.length === 0) {
              retryNoteParts.push('The JSON output could not be parsed as valid JSON.');
            }
            const retryNote = retryNoteParts.join(' ');
            sendLog(ws, `\nSelf-review: validation failed — retrying with explicit guidance...\n`);
            const { exitCode: retryExitCode, allOutput: retryOutput, lastResultText: retryResult } = await runClaudeCode(
              `${selfReviewPrompt}\n\nCRITICAL: Your previous self-review response was invalid. ${retryNote} Return ONLY a valid JSON object with all mandatory checks_performed items and verdict consistent with check_results.`,
              ws, statusRef, model, effort,
            );
            let retryValid = false;
            if (retryExitCode === 0) {
              const retryParsed = extractSelfReviewJson(retryResult || retryOutput);
              if (retryParsed) {
                const retryChecks = Array.isArray(retryParsed.checks_performed) ? retryParsed.checks_performed as string[] : undefined;
                const retryMissing = mandatory.filter(m => !retryChecks?.includes(m));
                if (retryChecks && retryMissing.length === 0) {
                  retryValid = true;
                  let retryVerdict = typeof retryParsed.verdict === 'string' ? retryParsed.verdict : undefined;
                  const retryCheckResults = retryParsed.check_results && typeof retryParsed.check_results === 'object' && !Array.isArray(retryParsed.check_results)
                    ? retryParsed.check_results as Record<string, { passed: boolean; issue?: string }>
                    : undefined;
                  // Re-enforce verdict/check_results consistency on retry response.
                  if (retryCheckResults) {
                    const retryFailedIds = Object.entries(retryCheckResults)
                      .filter(([, r]) => r && r.passed === false)
                      .map(([id]) => id);
                    if (retryFailedIds.length > 0 && retryVerdict !== 'FAIL') {
                      retryVerdict = 'FAIL';
                    }
                    selfReviewOutcome!.check_results = retryCheckResults;
                  }
                  if (retryVerdict !== undefined) selfReviewOutcome!.verdict = retryVerdict;
                }
              }
            }
            if (!retryValid) {
              sendLog(ws, '\nSelf-review: validation still failed after retry — marking task as FAILED.\n');
              await persistAndPostStatus(taskId, {
                task_id: taskId, status: 'FAILED', artifact_type: 'ERROR_LOG',
                logs: `Self-review validation failed: did not produce valid JSON with all mandatory checks.\n\nLast output:\n${lastReviewOutput.slice(0, 5000)}`,
                failure_category: 'self_review_rejected',
                changed_files: changedPaths.slice(0, 50),
                ...(diffSummary.length > 0 ? { diff_summary: diffSummary } : {}),
              });
              return;
            }
          }
          // FAIL verdict gate: block PR when agent identifies unresolvable issues
          if (selfReviewOutcome?.verdict === 'FAIL') {
            sendLog(ws, '\nSelf-review FAIL verdict — blocking PR creation.\n');
            await persistAndPostStatus(taskId, {
              task_id: taskId, status: 'FAILED', artifact_type: 'ERROR_LOG',
              logs: `Self-review verdict: FAIL.\n\nSelf-review output:\n${lastReviewOutput.slice(0, 5000)}`,
              failure_category: 'self_review_rejected',
              changed_files: changedPaths.slice(0, 50),
              ...(diffSummary.length > 0 ? { diff_summary: diffSummary } : {}),
              self_review_outcome: selfReviewOutcome,
            });
            return;
          }
        }
      }
    }

    // ── Guard: Post-self-review scope overflow check ─────────────────────────
    // Re-count changed files after self-review (which may have trimmed files).
    // If count still exceeds the threshold, block PR creation. Test files are
    // excluded from the count since they are essential for merge quality.
    if (overflowThreshold !== undefined) {
      const postReviewCommittedPaths = currentHead !== baseCommit
        ? git(['diff', '--name-only', `${baseCommit}..HEAD`]).split('\n').filter(Boolean)
        : [];
      const postReviewDiff = git(['status', '--porcelain']);
      const postReviewUncommitted = postReviewDiff.length > 0
        ? postReviewDiff.split('\n').map((l: string) => l.trim().split(/\s+/).pop() || '').filter(Boolean)
        : [];
      const postReviewPaths = [...new Set([...postReviewCommittedPaths, ...postReviewUncommitted])];
      const postReviewNonTestPaths = postReviewPaths.filter((f: string) => !isTestFile(f));
      if (postReviewNonTestPaths.length > overflowThreshold) {
        const fileList = postReviewPaths.map((f: string) => `- ${f}`).join('\n');
        const failureLog = `Scope overflow: ${postReviewNonTestPaths.length} non-test files changed for a "${taskScope}" task (threshold: ${overflowThreshold}, ${postReviewPaths.length} total including tests).\n\nChanged files:\n${fileList}\n\nTrim unrelated files and retry with a focused change set.`;
        sendLog(ws, `\nScope overflow gate: ${postReviewNonTestPaths.length} non-test files changed (threshold: ${overflowThreshold}) — blocking PR creation.\n`);
        await persistAndPostStatus(taskId, {
          task_id: taskId, status: 'FAILED', artifact_type: 'ERROR_LOG',
          logs: failureLog,
          failure_category: 'scope_overflow',
          changed_files: postReviewPaths.slice(0, 50),
        });
        return;
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
    if (scopeTrimReverted.length > 0) {
      prBody += `## Scope Trim Applied\n\n${scopeTrimReverted.length} non-essential file(s) were automatically reverted before PR creation:\n${scopeTrimReverted.map(f => `- ${f}`).join('\n')}\n\n`;
    } else if (scopeOverflowWarning) {
      prBody += `## ⚠️ Scope Overflow Warning\n\n${scopeOverflowWarning}\n\n${scopeTrimJustification ? `**Agent justification (all files kept):** ${scopeTrimJustification}\n\n` : 'Please verify that all changed files are necessary for this task.\n\n'}`;
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
      ...(analysisContext ? { analysis_plan: analysisContext.slice(0, 5000) } : {}),
    });
  } else if (exitCode === 0) {
    await persistAndPostStatus(taskId, { task_id: taskId, status: 'COMPLETED', logs: noChangesMessage });
  } else {
    await persistAndPostStatus(taskId, {
      task_id: taskId, status: 'FAILED', artifact_type: 'ERROR_LOG', logs: sanitize(allOutput.slice(-10_000)),
      failure_category: 'claude_code_failed',
      ...(fileChangeDetails.length > 0 ? { file_change_details: fileChangeDetails.slice(0, 50) } : {}),
      ...(analysisContext ? { analysis_plan: analysisContext.slice(0, 5000) } : {}),
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
    captureBaseline: true,
    runSelfReview: true,
    runSmallScopeAnalysis: true,
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
    failOnTestFailure: true,
    captureBaseline: true,
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
