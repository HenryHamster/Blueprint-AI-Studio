import { execFileSync } from 'child_process';
import type WebSocket from 'ws';
import {
  WORKSPACE, strVal, sendLog, validateBranchName,
  persistAndPostStatus, git, setupWorkspace, runClaudeCode,
  captureTestBaseline, getProviderRunner,
} from '../index.js';
import {
  getTaskTypeHints, DEFAULT_TOOL_GUIDANCE, PLAYBOOK_GUIDANCE, FIRST_TASK_GUIDANCE,
  SIZE_GUARDRAIL_GUIDANCE, READ_THE_FAILURE_MESSAGE, PRIOR_ATTEMPT_DIVERGENCE, BEYOND_SCOPE_REPORTING,
  readClaudeMd, buildContextPrefix, buildFileTree, discoverRelatedTests, configureAgentPermissions,
  writePlaybookHelper, writeMessagesHelper, formatPlaybook, buildRetryContext,
  buildAnalysisPrompt, parseAnalysisPlanFiles, parseAnalysisPlanTestFiles, parseAnalysisPlanVerifyFiles, extractModuleInterfaces,
  discoverAffectedConsumers, type PlaybookEntry,
} from '../prompt.js';
import { resolveBuildConfig } from '../build-config.js';
import { SCOPE_OVERFLOW_THRESHOLDS, MEDIUM_ANALYSIS_PLAN_FILE_LIMIT } from '../constants.js';
import { runCodeChangeTask } from './code-change.js';

// ── Standard Handler Helper ───────────────────────────────────────────────



const MODELS_PATH = 'backend/app/models.py';
const ALEMBIC_VERSIONS_DIR = 'backend/alembic/versions/';

/**
 * Returns true when changedPaths includes backend/app/models.py but no new
 * alembic migration file was added under backend/alembic/versions/.
 */
export function needsMigrationGeneration(changedPaths: string[]): boolean {
  if (!changedPaths.some(p => p === MODELS_PATH)) return false;
  return !changedPaths.some(p => p.startsWith(ALEMBIC_VERSIONS_DIR) && p.endsWith('.py'));
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
  runMigrationCheck?: boolean;
}

/**
 * For small-scope tasks, produce a brief note listing the primary target
 * file's top-level exports and any files that appear to import it. This is
 * a lightweight caller/import check — NOT a full analysis pass — meant to
 * catch import/type mismatches and missed caller updates. Returns '' when
 * nothing useful can be derived.
 */
export function buildSmallScopeCallerNote(relatedFiles: string[]): string {
  if (relatedFiles.length === 0) return '';

  const MAX_FILES = 3;
  const targets = relatedFiles.slice(0, MAX_FILES).filter(t => t && !t.includes('..'));
  if (targets.length === 0) return '';

  const lines: string[] = [];
  let anyFindings = false;

  for (let i = 0; i < targets.length; i++) {
    const target = targets[i];
    let content = '';
    try {
      content = execFileSync('cat', [`${WORKSPACE}/${target}`], { encoding: 'utf-8', timeout: 5_000 });
    } catch { continue; }

    const isPy = target.endsWith('.py');
    const re = isPy
      ? /^(?:async\s+)?(?:def|class)\s+([A-Za-z_]\w*)/gm
      : /^export\s+(?:default\s+|async\s+)?(?:function|class|const|let|var|type|interface|enum)\s+([A-Za-z_]\w*)/gm;
    const exports: string[] = [];
    let m: RegExpExecArray | null;
    while ((m = re.exec(content)) !== null && exports.length < 8) {
      if (!m[1].startsWith('_')) exports.push(m[1]);
    }

    const basename = (target.split('/').pop() || '').replace(/\.(ts|tsx|mjs|cjs|js|jsx|py)$/, '');
    let importers: string[] = [];
    if (basename && /^[A-Za-z_][\w-]*$/.test(basename)) {
      try {
        const out = execFileSync(
          'git',
          ['-C', WORKSPACE, 'grep', '-lE', `^\\s*(import|from)\\b.*\\b${basename}\\b`, '--', '*.ts', '*.tsx', '*.js', '*.py'],
          { encoding: 'utf-8', timeout: 10_000 },
        );
        importers = out.split('\n').filter(f => f && !targets.includes(f)).slice(0, 5);
      } catch { /* git grep returns 1 when no matches */ }
    }

    if (exports.length === 0 && importers.length === 0) continue;
    anyFindings = true;
    const label = i === 0 ? 'Primary target' : 'Related file';
    if (exports.length) lines.push(`${label} \`${target}\` exposes: ${exports.join(', ')}.`);
    if (importers.length) lines.push(`Files that reference \`${target}\`: ${importers.map(f => `\`${f}\``).join(', ')}.`);
  }

  if (!anyFindings) return '';
  lines.push('Preserve these exports and update any callers you change.');
  return lines.join('\n');
}

/**
 * Extracts the structured self-review JSON object from the agent's output.
 * Looks for a ```json ... ``` code fence first, then falls back to finding
 * the first `{` that can be parsed as JSON.
 */
export function extractSelfReviewJson(output: string): Record<string, unknown> | null {
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
    model, effort, branchPrefix, buildPrompt,
    noChangesMessage = 'No file changes.',
    runTestFix = false,
    captureBaseline = false,
    runSmallScopeAnalysis = false,
    includeTaskTypeHints = true,
  } = opts;
  // commitPrefix, prTitlePrefix, runLintFix, failOnTestFailure, runSelfReview,
  // lintContext, issueReference, relatedFiles, runMigrationCheck are no
  // longer wrapper-driven (Claude owns its own loop via ./check + ./pr).
  // They remain in StandardHandlerOpts so existing call-sites compile but
  // are intentionally ignored here.

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
  const finalPrompt = claudeMdPrefix + playbookSection + retryContext + buildPrompt({ contextPrefix, parentCtx, rawPrompt }) + taskTypeHint + firstTaskSection + PLAYBOOK_GUIDANCE + `\nNote: If lint or test fixes required non-obvious steps, save them to the playbook (e.g., ./playbook set "lint-gotcha" "description").\n` + READ_THE_FAILURE_MESSAGE + SIZE_GUARDRAIL_GUIDANCE + PRIOR_ATTEMPT_DIVERGENCE + BEYOND_SCOPE_REPORTING + `\n\n${DEFAULT_TOOL_GUIDANCE}` + promptSuffix;

  const { workBranch } = await setupWorkspace(owner, repo, ghToken, baseBranch, branchPrefix, taskId, ws, statusRef, buildConfig);
  writePlaybookHelper(taskId);
  writeMessagesHelper(taskId);
  const disallowedTools = configureAgentPermissions(taskTypeKey);

  // Capture test baseline on main branch so the test fix loop can distinguish
  // pre-existing failures from newly-broken tests. Scoped to related_files to stay fast.
  //
  // EXCEPTION: auto-remediation tasks deliberately skip baseline capture —
  // their WHOLE JOB is to fix whatever's broken on main, pre-existing or
  // otherwise. A baseline that excludes broken-main tests would make the
  // remediation into a no-op: Claude would see "pre-existing failures, don't
  // touch" and give up. We want it to see all failures and fix them.
  const isAutoRemediation = Boolean(
    (task.context as Record<string, unknown> | undefined)?.auto_remediation
  );
  if (captureBaseline && runTestFix && baseBranch === 'main' && !isAutoRemediation) {
    const relatedFiles = (
      (payload.related_files as string[] | undefined) ||
      ((task.context as Record<string, unknown> | undefined)?.related_files as string[] | undefined) ||
      []
    );
    await captureTestBaseline(ws, statusRef, buildConfig, relatedFiles);
  } else if (isAutoRemediation) {
    sendLog(ws, '[Baseline] Skipped — this is an auto-remediation task; all failures are fair game.\n');
  }

  const fileTree = buildFileTree(ws);

  // ── Phase 1: Pre-implementation analysis (medium-scope tasks only) ──────────
  // Runs a read-only pass to map module interfaces and dependencies before coding.
  let analysisContext = '';
  const taskScope = strVal(ctx.scope);
  const previousAnalysisPlan = strVal(ctx._previous_analysis_plan);
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
    );

    const analysisProjectCtx = task.project_context as Record<string, unknown> | undefined;
    const rawConcurrent = analysisProjectCtx?.direction_concurrent_files;
    const directionConcurrentFiles = Array.isArray(rawConcurrent)
      ? (rawConcurrent as unknown[]).filter((f): f is string => typeof f === 'string' && f.length > 0)
      : [];
    const rawCochange = analysisProjectCtx?.cochange_files;
    const cochangeFiles = Array.isArray(rawCochange)
      ? (rawCochange as unknown[]).filter((f): f is string => typeof f === 'string' && f.length > 0)
      : [];

    const diagnosisTaskId = strVal(ctx.diagnosis_task_id);
    const diagnosisContext = diagnosisTaskId
      ? {
          rootCause: strVal(ctx.diagnosis_root_cause),
          recommendedFix: strVal(ctx.diagnosis_recommended_fix),
          priorFailedApproaches: (() => {
            const summary = strVal(ctx._previous_error_summary);
            return summary ? [summary] : [];
          })(),
        }
      : undefined;
    const ctxPlanLimit = ctx._analysis_plan_file_limit;
    const prodFileLimit = (typeof ctxPlanLimit === 'number' && ctxPlanLimit > 0 && ctxPlanLimit <= MEDIUM_ANALYSIS_PLAN_FILE_LIMIT)
      ? Math.floor(ctxPlanLimit)
      : MEDIUM_ANALYSIS_PLAN_FILE_LIMIT;
    if (prodFileLimit !== MEDIUM_ANALYSIS_PLAN_FILE_LIMIT) {
      sendLog(ws, `[Analysis Phase] Using tightened production file limit ${prodFileLimit} (from prior-attempt retry context).\n`);
    }
    const analysisPrompt = buildAnalysisPrompt({
      rawPrompt,
      taskTitle,
      relatedFiles: analysisRelatedFiles,
      fileTree,
      fileLimit: prodFileLimit,
      totalFileBudget: prodFileLimit,
      directionConcurrentFiles,
      cochangeFiles,
      diagnosisContext,
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
      let plannedVerifyFiles = parseAnalysisPlanVerifyFiles(effectiveAnalysis);

      // Scope feasibility gate: if the analysis plans more production files
      // than the medium-scope limit, retry the analysis once with explicit
      // narrowing guidance. If still over budget, fail fast — the
      // implementation pass would otherwise burn ~5-10 minutes only to hit
      // scope_overflow. Test files are deliberately excluded from this budget
      // so coverage can scale with the production change.
      if (plannedFiles.length > prodFileLimit) {
        sendLog(ws, `[Analysis Phase] plan lists ${plannedFiles.length} production files (limit ${prodFileLimit}); retrying with narrowing directive.\n`);
        const testFilesBlock = plannedTestFiles.length > 0
          ? `\nPreviously planned test files (not counted against the budget — keep whatever coverage the change needs):\n${plannedTestFiles.map(f => `- ${f}`).join('\n')}`
          : '';
        const narrowingGuidance = `The previous analysis pass planned ${plannedFiles.length} production files, exceeding the production-file limit of ${prodFileLimit}. Test files do NOT count toward this limit — do not trim test coverage to hit the number. Narrow the **production** plan to the minimum set of files that form a **coherent change** — the files that MUST change together for the feature to work correctly (e.g. a shared type touched in backend + agent is one logical unit and should stay together). Do NOT split a logical unit just to reduce file count; instead, drop files that belong to a *different* concern — peripheral cleanups, nice-to-have refactors, or independent follow-on work that can ship as a separate task without breaking this one. In **Implementation approach**, add a "Deferred" subsection explicitly listing any files/concerns you dropped so the next task can pick them up. Previously planned production files:\n${plannedFiles.map(f => `- ${f}`).join('\n')}${testFilesBlock}`;
        const retryPrompt = buildAnalysisPrompt({
          rawPrompt,
          taskTitle,
          relatedFiles: analysisRelatedFiles,
          fileTree,
          fileLimit: prodFileLimit,
          totalFileBudget: prodFileLimit,
          narrowingGuidance,
          directionConcurrentFiles,
          cochangeFiles,
          diagnosisContext,
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
          plannedVerifyFiles = parseAnalysisPlanVerifyFiles(effectiveAnalysis);
        }

        if (plannedFiles.length > prodFileLimit) {
          const fileList = plannedFiles.map(f => `- ${f}`).join('\n');
          const testList = plannedTestFiles.length > 0
            ? `\n\nPlanned test files (not counted against the budget):\n${plannedTestFiles.map(f => `- ${f}`).join('\n')}`
            : '';
          const failureLog = `Pre-implementation analysis planned ${plannedFiles.length} production files, exceeding the production-file limit of ${prodFileLimit}. Test files do not count toward this limit. The narrowing retry could not bring the plan within budget. Split this task along a logical boundary — separate out any files belonging to a different concern as a follow-on task — rather than forcing a single-file solution that breaks the coherent change set.\n\nPlanned production files:\n${fileList}${testList}`;
          sendLog(ws, `[Analysis Phase] Plan still exceeds production budget after retry (${plannedFiles.length} prod files, limit ${prodFileLimit}) — failing fast.\n`);
          await persistAndPostStatus(taskId, {
            task_id: taskId, status: 'FAILED', artifact_type: 'ERROR_LOG',
            logs: failureLog,
            failure_category: 'scope_overflow',
            scope_gate_triggered: 'analysis_overflow',
            analysis_plan: effectiveAnalysis.slice(0, 5000),
          });
          return;
        }
        sendLog(ws, `[Analysis Phase] Plan narrowed to ${plannedFiles.length} production files (${plannedTestFiles.length} test files, uncapped; ${plannedVerifyFiles.length} verify-only files, uncapped).\n`);
      }

      analysisContext = `## Pre-Implementation Analysis\n\n${effectiveAnalysis}\n\n`;
      sendLog(ws, '[Analysis Phase] Complete.\n');
    } else {
      sendLog(ws, '[Analysis Phase] Skipped — proceeding to implementation.\n');
    }
  } else if (taskScope === 'small' && runSmallScopeAnalysis) {
    const smallRelated = (
      (payload.related_files as string[] | undefined) ||
      (ctx.related_files as string[] | undefined) ||
      []
    );
    const callerNote = buildSmallScopeCallerNote(smallRelated);
    const callerSection = callerNote ? `\n\n${callerNote}` : '';
    analysisContext = `## Pre-Implementation Analysis\n\nBefore writing code: read the files relevant to this task, confirm the scope stays under ~50 lines across 1–3 files, and check for callers that import from files you will modify.${callerSection}\n\n`;
  }

  const finalPromptWithTree = fileTree + analysisContext + finalPrompt;

  statusRef.value = `Running ${getProviderRunner().displayName}...`;
  // Record HEAD before Claude runs so we can detect commits Claude made (e.g. before rebasing)
  const baseCommit = git(['rev-parse', 'HEAD']);
  // 2026-05-04: wrapper-driven lint/test/push/PR pipeline replaced with
  // runCodeChangeTask. Claude owns its own loop via ./check + ./pr +
  // ./done + ./fail helpers. Wrapper just runs Claude, watches markers,
  // and posts terminal status. See agent/src/handlers/code-change.ts.
  await runCodeChangeTask({
    taskId,
    prompt: finalPromptWithTree,
    ws,
    statusRef,
    workBranch,
    baseBranch,
    owner,
    repo,
    testsOnMainBranch: baseBranch === 'main',
    baseCommit,
    model,
    effort,
    disallowedTools,
    noChangesMessage: noChangesMessage ?? 'No file changes.',
    analysisContext,
  });
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
    ? `Closes ${issueUrl || `#${issueNumber}`}`
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
  const createBudget = createScope ? SCOPE_OVERFLOW_THRESHOLDS[createScope] : undefined;
  const createScopeBudgetNote = createBudget
    ? `\n\n## Scope File Budget\n\nYour scope budget allows at most ${createBudget} non-test production files. If your changes would exceed this, focus on the single most critical subset and note what was deferred.\n`
    : '';

  await runStandardHandler({
    taskId, payload, task, ghToken, owner, repo, ws, statusRef, model, effort,
    branchPrefix: 'task',
    buildPrompt: ({ contextPrefix, parentCtx }) => `${contextPrefix}${parentCtx}${moduleInterfaceContext}${affectedConsumersContext}${relatedTestContext}${overlapWarningText}${mediumScopePlanningNote}${createScopeBudgetNote}${prompt}${siblingNote}`,
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
    runMigrationCheck: true,
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
  const refactorBudget = refactorScope ? SCOPE_OVERFLOW_THRESHOLDS[refactorScope] : undefined;
  const refactorScopeBudgetNote = refactorBudget
    ? `\n\n## Scope File Budget\n\nYour scope budget allows at most ${refactorBudget} non-test production files. If your changes would exceed this, focus on the single most critical subset and note what was deferred.\n`
    : '';

  const refactorTitle = strVal(task.title, 'Refactor');
  await runStandardHandler({
    taskId, payload, task, ghToken, owner, repo, ws, statusRef, model, effort,
    branchPrefix: 'refactor',
    buildPrompt: ({ contextPrefix, rawPrompt: rp }) =>
      `${contextPrefix}${refactorModuleInterfaceContext}${refactorAffectedConsumersContext}${refactorRelatedTestContext}${refactorOverlapWarningText}${refactorScopeBudgetNote}REFACTOR TASK — STRICT CONSTRAINTS:
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
    runMigrationCheck: true,
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
