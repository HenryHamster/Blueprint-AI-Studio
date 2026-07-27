import { execFileSync, spawn } from 'child_process';
import fs from 'fs';
/**
 * Standard task handlers — core code-change operations.
 *
 * Provides handlers for common code-change task types:
 *
 * - **handleCreate**: Implement a new feature from a prompt. Clones repo, runs
 *   Claude Code with full tool access, creates a PR with changes.
 *
 * - **handleRefactor**: Refactor existing code for clarity, performance, or
 *   consistency. Clones repo, runs Claude Code, creates a PR.
 *
 * - **handleDocs**: Write or update documentation. May edit code comments,
 *   README, or dedicated doc files. Clones repo, runs Claude Code, creates a PR.
 *
 * - **handleDeps**: Update dependencies (npm packages, pip requirements, etc.).
 *   Clones repo, runs Claude Code, creates a PR with updated lock files.
 *
 * - **handleQA**: Write or run quality assurance tests. Clones repo, runs Claude
 *   Code, may run test suites, creates a PR with test additions/fixes.
 *
 * All standard handlers follow the same template: clone, setup workspace,
 * run Claude Code, fix loop (if tests fail), commit, and create PR.
 */

import type WebSocket from 'ws';
import {
  WORKSPACE, strVal, sendLog, validateBranchName,
  persistAndPostStatus, git, setupWorkspace, runClaudeCode,
  getProviderRunner, setCurrentPromptFingerprint, sendHeartbeatSync,
  resolveBaseBranch, resolveTaskBranchNamespace,
} from '../index.js';
import { buildPromptSizeTelemetry, setPromptSizeTelemetry } from '../telemetry.js';
import { captureTestBaseline } from '../utils/test-fix-loop.js';
import {
  getTaskTypeHints, DEFAULT_TOOL_GUIDANCE, PLAYBOOK_GUIDANCE, FIRST_TASK_GUIDANCE,
  buildCodeChangeHelperGuidance, codeChangeGuidanceVariant,
  getTaskTypeFocusBlock,
  assembleFailureGuidanceBlocks,
  readClaudeMd, buildContextPrefix, buildFileTree, buildRepoMap, discoverRelatedTests, configureAgentPermissions,
  writePlaybookHelper, writeMessagesHelper, writeViewportSnapshotHelper, writeViewportLiveHelper, formatPlaybook, buildRetryContext,
  buildAnalysisPrompt, parseAnalysisPlanFiles, parseAnalysisPlanTestFiles, parseAnalysisPlanVerifyFiles, extractModuleInterfaces,
  discoverAffectedConsumers, type PlaybookEntry, computePromptFingerprint,
  formatProvenanceWarnings, formatAcceptanceEvidence,
  formatSkills, writeSkillHelpers, type TaskSkill,
  formatProjectTools, type TaskToolContent,
} from '../prompt.js';
import { providerAutoInjectsClaudeMd } from '../providers/runners.js';
import { writeFetchHelper, buildFetchGuidance, PLAN_FETCH_VERBS } from './strategic-helpers.js';
import {
  fetchKnowledgeDocumentById,
  fetchArtifactDownloadUrl,
  buildPrerequisiteKnowledgeSection,
  buildPrerequisiteArtifactSection,
  type PrerequisiteKnowledgePointer,
  type PrereqKnowledgeDoc,
  type PrerequisiteArtifactPointer,
} from './prior-knowledge.js';
import { resolveBuildConfig } from '../build-config.js';
import { extractReservedMigrationRevision, buildReservedRevisionGuidance } from './review-fix-migration.js';
import { resolveMigrationLayout } from './specialized.js';
import { SCOPE_OVERFLOW_THRESHOLDS, MEDIUM_ANALYSIS_PLAN_FILE_LIMIT, DEFAULT_CLONE_DEPTH, ANALYSIS_CONTEXT_CHAR_CAP } from '../constants.js';
import { prepareWorkspaceForRole } from '../workspacePrep.js';
import { writeGitTokenFile, ensureGitCredentialHelperScript } from '../git-credential.js';
import { runCodeChangeTask } from './code-change.js';
import { consumeAndCommitArtifacts, hydrateComputeResultArtifacts } from './artifact-consume.js';
import { writeArtifactsHelper, COMPUTE_ARTIFACT_GUIDANCE } from './artifacts-helper.js';
import { isBlueprintSelfRepo } from '../prompts/code-change-helpers.js';
import { writeDiagnosticsAfterEditHook } from '../prompts/diagnostics-after-edit-hook.js';
import { formatExecutionEvidenceForSrPrompt, formatHighFpRateLoosenHintForSrPrompt, formatSrCoFailurePairsForSrPrompt, formatSeverityCalibrationHintForSrPrompt, formatPrecisionRecallQuadrantHintForSrPrompt } from '../prompts/self-review.js';
import {
  runFreshReviewerPass,
  loneCheckerCategoriesForTask,
  sensitivePathsFromMetadata,
  getRegressingCategories,
  getThinningCategories,
  VIEWPORT_INFRA_SKIP_GUIDANCE,
} from './fresh-reviewer.js';

export {
  runFreshReviewerPass,
  loneCheckerCategoriesForTask,
  sensitivePathsFromMetadata,
  pickFreshReviewerEffort,
  extractSelfReviewJson,
  SR_SKIP_REASONS,
} from './fresh-reviewer.js';

// ── Standard Handler Helper ───────────────────────────────────────────────

/**
 * Computes a heuristic regression-risk score from a git diff when no test
 * baseline file is available. Signals: high source-to-test line ratio and
 * removed exported symbols. Always tags issue text with [heuristic-derived].
 */
export function computeRegressionRiskHeuristic(diff: string): { passed: boolean; issue: string } {
  const lines = diff.split('\n');
  let srcChanged = 0, testChanged = 0, removedExports = 0;
  let currentFile = '';

  for (const line of lines) {
    if (line.startsWith('diff --git ')) {
      currentFile = line.replace(/^diff --git a\/\S+ b\//, '');
      continue;
    }
    if (line.startsWith('--- ') || line.startsWith('+++ ')) continue;
    const isTest =
      /\/(test|spec|__tests__)\//.test(currentFile) ||
      /\.(test|spec)\.(ts|tsx|js|py)$/.test(currentFile) ||
      /(?:^|\/)test_[^/]+\.py$/.test(currentFile);
    if (/^[+-]/.test(line)) {
      if (isTest) testChanged++; else srcChanged++;
    }
    if (line.startsWith('-') &&
        /^-\s*export\s+(?:default\s+)?(?:async\s+)?(?:function|class|const|let|var|type|interface|enum)\s+\w/.test(line)) {
      removedExports++;
    }
  }

  const signals: string[] = [];
  if (srcChanged >= 80 && testChanged === 0) signals.push(`+${srcChanged} source lines / 0 test lines`);
  if (removedExports > 0) signals.push(`${removedExports} export(s) removed`);

  const tag = '[heuristic-derived]';
  if (signals.length === 0) {
    return { passed: true, issue: `${tag} no high-risk signals (${srcChanged} src / ${testChanged} test lines)` };
  }
  return { passed: false, issue: `${tag} ${signals.join('; ')}` };
}



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

function resolvePrTitlePrefix(task: Record<string, unknown>): string {
  const buildConfig = task.build_config as Record<string, unknown> | undefined;
  const prefix = buildConfig?.pr_title_prefix;
  return typeof prefix === 'string' ? prefix.trim() : '';
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
  captureBaseline?: boolean;
  runSelfReview?: boolean;
  runSmallScopeAnalysis?: boolean;
  lintContext?: string;
  includeTaskTypeHints?: boolean;
  issueReference?: string;
  relatedFiles?: string[];
  runMigrationCheck?: boolean;
  promptSectionFlags?: {
    hadModuleInterfaces?: boolean;
    hadRelatedTests?: boolean;
    hadAffectedConsumers?: boolean;
    moduleInterfacesArm?: boolean;
    relatedTestsArm?: boolean;
    affectedConsumersArm?: boolean;
  };
}

type CapabilityHoldbackName = 'module_interfaces' | 'related_tests' | 'affected_consumers' | 'repo_map' | 'diagnostics_after_edit';

function holdbackFraction(meta: Record<string, unknown> | undefined, capability: CapabilityHoldbackName): number {
  const value = meta?.[`${capability}_holdback_fraction`];
  // Default-OFF: absent an explicit per-project runtime_config holdback fraction,
  // these context blocks were evaluated no_effect at the futility bound (2026-07-10)
  // and are suppressed by default (holdback fraction 1.0 => holdback arm). A project
  // can re-enable a section by setting its `<capability>_holdback_fraction` to a
  // value < 1.0 (0.0 = always-treatment/opt-in).
  return typeof value === 'number' ? value : 1.0;
}

/**
 * Deterministically assigns a task to an independent per-capability trial arm.
 * `true` means treatment (section injected); `false` means holdback.
 */
export function assignCapabilityHoldbackArm(
  taskId: string,
  capability: CapabilityHoldbackName,
  fraction: number,
): boolean | undefined {
  if (fraction <= 0) return undefined;
  let h = 5381;
  const salted = `${capability}:${taskId}`;
  for (let i = 0; i < salted.length; i++) {
    h = ((h << 5) + h + salted.charCodeAt(i)) >>> 0;
  }
  return (h / 0xffffffff) >= fraction;
}

function logCapabilityHoldback(
  ws: WebSocket | null,
  taskId: string,
  capability: CapabilityHoldbackName,
  fraction: number,
): void {
  sendLog(ws, `[CapabilityTrial] ${capability} holdback arm for task ${taskId} (fraction=${fraction}) — suppressing section\n`);
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

/** Extract a `visual` acceptance-evidence declaration (component + viewport
 *  width) from a task's payload or context, for threading into the generated
 *  ./check success-path capture+upload (see writeCheckHelper / buildVisualCaptureBlock).
 *  Returns undefined when no well-formed visual declaration is present. Two
 *  channels carry it, and a single planned declaration lands on exactly one of
 *  them depending on which vocabulary the planner used:
 *    - payload AcceptanceEvidence: {kind:"visual", component, viewport_width}
 *    - context PlanAcceptanceEvidence: {kind:"visual", descriptor=component, selector=width}
 *  The component is restricted to the backend's safe object-key character class
 *  (also the shell-safe set) and width to 1–4096, so it can be interpolated into
 *  the generated bash; anything else is skipped (best-effort — this never blocks). */
export function resolveVisualAcceptance(
  payload: Record<string, unknown>,
  ctx: Record<string, unknown>,
): { component: string; width: number } | undefined {
  const SAFE_COMPONENT = /^[A-Za-z0-9._-]{1,100}$/;
  const fromPayload = payload.acceptance_evidence as Record<string, unknown> | undefined;
  const fromCtx = ctx.acceptance_evidence as Record<string, unknown> | undefined;
  let component: string | undefined;
  let width = NaN;
  if (fromPayload && typeof fromPayload === 'object' && strVal(fromPayload.kind) === 'visual') {
    component = strVal(fromPayload.component);
    width = Number(fromPayload.viewport_width);
  } else if (fromCtx && typeof fromCtx === 'object' && strVal(fromCtx.kind) === 'visual') {
    // Context channel (PlanAcceptanceEvidence): descriptor=component, selector=width-string.
    component = strVal(fromCtx.descriptor);
    width = Number(strVal(fromCtx.selector));
  }
  if (!component || !SAFE_COMPONENT.test(component)) return undefined;
  if (!Number.isInteger(width) || width < 1 || width > 4096) return undefined;
  return { component, width };
}

/**
 * Section strings for a standard-handler prompt, split by cache-stability.
 * See {@link assembleStandardPrompt}.
 */
export interface StandardPromptSections {
  // ── Stable prefix (deterministic literal/config guidance) ──────────────────
  /** ## Codebase Instructions block (CLAUDE.md), or '' when the provider auto-injects it. */
  claudeMdPrefix: string;
  /** Whether this is a retry (toggles a retry-specific failure-guidance block; still stable for identical payload). */
  isRetry: boolean;
  /** ./check + ./pr + ./done + ./fail helper guidance. */
  codeChangeHelperGuidance: string;
  /** Task-type focus block (constant per task type). */
  taskTypeFocusBlock: string;
  /** Task-type hint (constant per task type + base branch). */
  taskTypeHint: string;
  /** First-task-on-repo guidance, or ''. */
  firstTaskSection: string;
  /** Read-only ./fetch verb guidance, or '' when no project_id. */
  fetchGuidance: string;
  // ── Variable tail (task-payload / project-aggregate content) ────────────────
  acceptanceEvidenceNotice: string;
  playbookSection: string;
  skillsSection: string;
  projectToolsSection: string;
  retryContext: string;
  provenanceNotice: string;
  /** buildPrompt() output — carries contextPrefix + rawPrompt (the per-task core). */
  corePrompt: string;
  reservedRevisionGuidance: string;
  /** Self-review feedback + execution-evidence blocks (project-aggregate, slow-moving). */
  srFeedbackBlock: string;
  promptSuffix: string;
  prTitlePrefixNote: string;
}

/**
 * Assemble a standard-handler prompt so the automatically-cacheable prefix is
 * maximized: ALL stable literal/config guidance forms one contiguous leading
 * prefix, and ALL task-variable content forms a contiguous trailing tail.
 *
 * This is a byte-ORDER change only — every section that was sent before is
 * still sent; only its position changes. Anthropic prefix caching bills a
 * cache read at ~10% of base input, so a variable byte appearing early would
 * bust the automatic prefix match for everything after it. Keeping the stable
 * guidance first means two builds of the same handler for an identical payload
 * but different wall-clock / git HEAD produce a BYTE-IDENTICAL stable prefix
 * (none of the prefix sections read the clock or HEAD).
 *
 * Returns the leading `stablePrefix`, the trailing `variableBody`, and their
 * concatenation `finalPrompt` (fileTree/repoMap/analysis are appended by the
 * caller, after the tail).
 */
export function assembleStandardPrompt(s: StandardPromptSections): {
  stablePrefix: string;
  variableBody: string;
  finalPrompt: string;
} {
  const stablePrefix =
    s.claudeMdPrefix
    + PLAYBOOK_GUIDANCE
    + `\nNote: If lint or test fixes required non-obvious steps, save them to the playbook (e.g., ./playbook set "lint-gotcha" "description").\n`
    + assembleFailureGuidanceBlocks(s.isRetry)
    + VIEWPORT_INFRA_SKIP_GUIDANCE
    + s.codeChangeHelperGuidance
    + s.taskTypeFocusBlock
    + s.taskTypeHint
    + s.firstTaskSection
    + `\n\n${s.fetchGuidance}${DEFAULT_TOOL_GUIDANCE}`;
  const variableBody =
    s.acceptanceEvidenceNotice
    + s.playbookSection
    + s.skillsSection
    + s.projectToolsSection
    + s.retryContext
    + s.provenanceNotice
    + s.corePrompt
    + s.reservedRevisionGuidance
    + s.srFeedbackBlock
    + s.promptSuffix
    + s.prTitlePrefixNote;
  return { stablePrefix, variableBody, finalPrompt: stablePrefix + variableBody };
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
    promptSectionFlags,
  } = opts;
  // commitPrefix, prTitlePrefix, runLintFix, runSelfReview, lintContext,
  // issueReference, relatedFiles, runMigrationCheck are no longer
  // wrapper-driven (Claude owns its own loop via ./check + ./pr).
  // They remain in StandardHandlerOpts so existing call-sites compile but
  // are intentionally ignored here.

  const buildConfig = resolveBuildConfig(task);
  const ctx = (task.context as Record<string, unknown> | undefined) || {};
  // Cascade: explicit payload override → research-mode context branches →
  // per-project integration_branch → 'main'. resolveBaseBranch supplies the
  // final two steps so non-Blueprint projects whose integration branch is
  // not 'main' (e.g. 'master', 'production') do not silently fall through.
  const rawBaseBranch =
    strVal(payload.base_branch)
    || strVal(ctx.research_branch)
    || strVal(ctx.original_base)
    || resolveBaseBranch({}, task);
  const baseBranch = validateBranchName(rawBaseBranch, 'base_branch');
  const projectMetaForPromotion = task.project_metadata as Record<string, unknown> | undefined;
  const promotionTarget =
    strVal(payload.promotion_target) ||
    strVal(projectMetaForPromotion?.promotion_target) ||
    undefined;
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
  const taskTypeHintsMap = getTaskTypeHints(buildConfig, promotionTarget);
  const taskTypeHint = includeTaskTypeHints && taskTypeHintsMap[taskTypeKey]
    ? `\n\n${taskTypeHintsMap[taskTypeKey].replace(/\{\{BASE_BRANCH\}\}/g, baseBranch)}`
    : '';

  const claudeMd = readClaudeMd();
  const claudeMdPrefix = (claudeMd && !providerAutoInjectsClaudeMd()) ? `## Codebase Instructions\n${claudeMd}\n\n` : '';
  const playbookSection = formatPlaybook(task.playbook as Record<string, PlaybookEntry | string> | undefined);
  const skillsSection = formatSkills(task.skills as TaskSkill[] | undefined);
  const projectToolsSection = formatProjectTools(task.project_tools as TaskToolContent[] | undefined);
  const retryContext = buildRetryContext(task);
  const firstTaskSection = task.first_task_on_repo ? FIRST_TASK_GUIDANCE : '';
  const taskTypeFocusBlock = getTaskTypeFocusBlock(taskTypeKey);
  const executionEvidenceSection = formatExecutionEvidenceForSrPrompt({
    blueprintSelfRepo: !buildConfig?.directories?.length && isBlueprintSelfRepo(owner, repo),
  });
  const projectCtxForSr = task.project_context as Record<string, unknown> | undefined;
  const srCategories = (projectCtxForSr?.self_review_catch_rate as { categories?: Record<string, { false_positive_rate?: number; precision?: number; sr_flag_sample_size?: number; severity_calibration?: Record<string, unknown>; precision_recall_quadrant?: string; precision_delta?: number; recall_delta?: number }> } | null | undefined)?.categories;
  const loosenHintSection = formatHighFpRateLoosenHintForSrPrompt(srCategories);
  const coFailurePairs = projectCtxForSr?.sr_co_failure_pairs as Array<{ cat_a: string; cat_b: string; lift: number }> | null | undefined;
  const coFailurePairsSection = formatSrCoFailurePairsForSrPrompt(coFailurePairs);
  const severityCalibrationHintSection = formatSeverityCalibrationHintForSrPrompt(srCategories);
  const precisionRecallQuadrantHintSection = formatPrecisionRecallQuadrantHintForSrPrompt(srCategories);
  const prTitlePrefix = opts.prTitlePrefix.trim();
  const prTitlePrefixNote = prTitlePrefix
    ? `\n\nWhen you open the PR with ./pr open, start the title with ${prTitlePrefix}.`
    : '';
  // A reserved provisional revision rides the claim only for migration-producing
  // tasks (a create that touches models.py / an alembic file). When present, the
  // agent must consume the locked id verbatim instead of deriving max+1, so
  // concurrent schema work does not collide on the same revision number.
  const reservedRevision = extractReservedMigrationRevision(task);
  const migrationLayout = resolveMigrationLayout(buildConfig);
  const reservedRevisionGuidance = reservedRevision
    ? buildReservedRevisionGuidance(
        reservedRevision,
        `${migrationLayout.versionsDir}/`,
        migrationLayout.schemaRevisionFile,
        migrationLayout.schemaRevisionConst,
      )
    : '';
  // Read-only ./fetch (direction, section, playbook) so a create/refactor agent
  // can resolve by-reference ids and trimmed launch snapshots mid-run. Gate both
  // provisioning and guidance on project_id so the prompt never advertises a
  // helper that was not written.
  const stdProjectId = ((task.project_metadata as Record<string, unknown> | undefined)?.project_id as string | undefined) ?? '';
  const stdFetchGuidance = stdProjectId ? buildFetchGuidance(PLAN_FETCH_VERBS) : '';
  const acceptanceEvidenceNotice = formatAcceptanceEvidence(payload);
  // Stale-premise/provenance notice for finding-derived tasks (create/refactor/
  // docs/qa/deps). Rendered early/prominently so the agent reproduces-first
  // before trusting a possibly-stale finding. Empty string when no warnings
  // fired, so the prompt is byte-unchanged for non-finding-derived tasks.
  const provenanceNotice = formatProvenanceWarnings(ctx);
  const isRetry = !!retryContext;
  // ── Cache-optimized prompt assembly ────────────────────────────────────────
  // Stable literal/config guidance forms one contiguous leading prefix; all
  // task-variable content forms a contiguous trailing tail. This maximizes the
  // automatically-cacheable prefix for Anthropic prefix caching (cache reads
  // billed at ~10% of base input) across a dispatch burst. See
  // assembleStandardPrompt. Section groups below are also reused for the
  // prompt-size telemetry further down, so they are computed exactly once.
  const codeChangeHelperGuidance = buildCodeChangeHelperGuidance({ buildConfig, owner, repo });
  const corePromptBody = buildPrompt({ contextPrefix, parentCtx, rawPrompt });
  const srFeedbackBlock =
    loosenHintSection
    + coFailurePairsSection
    + severityCalibrationHintSection
    + precisionRecallQuadrantHintSection
    + executionEvidenceSection;
  const { finalPrompt } = assembleStandardPrompt({
    claudeMdPrefix,
    isRetry,
    codeChangeHelperGuidance,
    taskTypeFocusBlock,
    taskTypeHint,
    firstTaskSection,
    fetchGuidance: stdFetchGuidance,
    acceptanceEvidenceNotice,
    playbookSection,
    skillsSection,
    projectToolsSection,
    retryContext,
    provenanceNotice,
    corePrompt: corePromptBody,
    reservedRevisionGuidance,
    srFeedbackBlock,
    promptSuffix,
    prTitlePrefixNote,
  });

  const { workBranch } = await setupWorkspace(owner, repo, ghToken, baseBranch, branchPrefix, taskId, ws, statusRef, resolveTaskBranchNamespace(task), buildConfig);
  writePlaybookHelper(taskId);
  writeMessagesHelper(taskId);
  const artifactsHelperAvailable = writeArtifactsHelper(WORKSPACE);
  writeSkillHelpers(task.skills as Record<string, unknown>[] | undefined);
  if (isBlueprintSelfRepo(owner, repo)) {
    writeViewportSnapshotHelper(taskId);
    writeViewportLiveHelper(taskId);
  }

  // Build ctags symbol index for language-aware navigation (./symbols helper).
  // Graceful fallback: if ctags is unavailable or indexing fails, agents still
  // work normally and ./symbols reports "index not available".
  // Runs asynchronously so the event loop stays free for heartbeats and WS I/O.
  void (async () => {
    const spawnWithTimeout = (file: string, args: string[], timeoutMs: number, opts?: { cwd?: string }) =>
      new Promise<void>((resolve, reject) => {
        const child = spawn(file, args, { stdio: 'ignore', ...(opts?.cwd ? { cwd: opts.cwd } : {}) });
        const timer = setTimeout(() => { child.kill('SIGKILL'); reject(new Error(`timed out after ${timeoutMs}ms: ${file} ${args.join(' ')}`)); }, timeoutMs);
        child.on('exit', (code) => { clearTimeout(timer); if (code === 0) resolve(); else reject(new Error(`exit ${code}: ${file} ${args.join(' ')}`)); });
        child.on('error', (err) => { clearTimeout(timer); reject(err); });
      });
    try {
      try {
        await spawnWithTimeout('ctags', ['--version'], 5000);
      } catch {
        await spawnWithTimeout('sudo', ['apt-get', 'install', '-y', '-q', 'universal-ctags'], 60000);
      }
      await spawnWithTimeout('ctags', ['-R', '--output-format=json', '-f', `/tmp/symbols_${taskId}.json`, '.'], 120000, { cwd: WORKSPACE });
    } catch { /* ctags unavailable or indexing failed — continue without symbol index */ }
  })();
  if (stdProjectId) writeFetchHelper(taskId, stdProjectId, WORKSPACE, { verbs: PLAN_FETCH_VERBS });
  const disallowedTools = configureAgentPermissions(taskTypeKey);

  // ── SCOPE_EMPTY pre-flight (auto-decomposed cluster siblings) ────────────
  // When all declared related_files were already shipped by completed sibling
  // cluster PRs, exit cleanly rather than burning retries on CODE_ERROR.
  if (ctx.auto_decomposed) {
    const guardFiles = (
      (payload.related_files as string[] | undefined) ||
      (ctx.related_files as string[] | undefined) ||
      []
    );
    if (guardFiles.length > 0) {
      const projectCtxForGuard = task.project_context as Record<string, unknown> | undefined;
      const siblingTasksRaw = projectCtxForGuard?.sibling_tasks as Array<Record<string, unknown>> | undefined;
      if (Array.isArray(siblingTasksRaw)) {
        const completedSiblingFiles = new Set<string>();
        const coveringTitles: string[] = [];
        for (const sibling of siblingTasksRaw) {
          if (sibling.status !== 'COMPLETED') continue;
          const sFiles = sibling.files as string[] | undefined;
          if (!Array.isArray(sFiles)) continue;
          const overlaps = sFiles.filter(f => guardFiles.includes(f));
          if (overlaps.length > 0) {
            for (const f of sFiles) completedSiblingFiles.add(f);
            const rawTitle = sibling.title;
          const rawId = sibling.id;
          const title = typeof rawTitle === 'string' && rawTitle ? rawTitle
            : typeof rawId === 'string' && rawId ? rawId
            : 'sibling task';
            if (!coveringTitles.includes(title)) coveringTitles.push(title);
          }
        }
        if (completedSiblingFiles.size > 0 && guardFiles.every(f => completedSiblingFiles.has(f))) {
          const scopeEmptyMsg = `SCOPE_EMPTY: All declared related_files [${guardFiles.join(', ')}] were already covered by completed sibling tasks (${coveringTitles.join(', ')}). Exiting cleanly — no retry needed.`;
          sendLog(ws, `[ScopeGuard] ${scopeEmptyMsg}\n`);
          await persistAndPostStatus(taskId, {
            task_id: taskId,
            status: 'BLOCKED_PRECONDITION',
            artifact_type: 'ERROR_LOG',
            logs: scopeEmptyMsg,
            failure_category: 'SCOPE_EMPTY',
          });
          return;
        }
      }
    }
  }

  // Capture test baseline on the integration branch so the test fix loop can
  // distinguish pre-existing failures from newly-broken tests. Only when the
  // task's base branch is that integration branch. Scoped to related_files to
  // stay fast.
  //
  // EXCEPTION: auto-remediation tasks deliberately skip baseline capture —
  // their WHOLE JOB is to fix whatever's broken on the integration branch,
  // pre-existing or otherwise. A baseline that excludes those broken tests
  // would make the remediation into a no-op: the agent would see "pre-existing
  // failures, don't touch" and give up. We want it to see all failures and fix
  // them.
  const isAutoRemediation = Boolean(
    (task.context as Record<string, unknown> | undefined)?.auto_remediation
  );
  if (captureBaseline && runTestFix && baseBranch === (promotionTarget ?? resolveBaseBranch({}, task)) && !isAutoRemediation) {
    const relatedFiles = (
      (payload.related_files as string[] | undefined) ||
      ((task.context as Record<string, unknown> | undefined)?.related_files as string[] | undefined) ||
      []
    );
    const baselineFailures = await captureTestBaseline(ws, statusRef, buildConfig, relatedFiles);
    if (baselineFailures !== null) {
      try {
        await fs.promises.writeFile(
          `/tmp/test_baseline_${taskId}.json`,
          JSON.stringify(Array.from(baselineFailures)),
          { mode: 0o644 },
        );
      } catch (e) {
        sendLog(ws, `[Baseline] Failed to write baseline file: ${e instanceof Error ? e.message : String(e)}\n`);
      }
    }
  } else if (isAutoRemediation) {
    sendLog(ws, '[Baseline] Skipped — this is an auto-remediation task; all failures are fair game.\n');
  }

  const fileTree = buildFileTree(ws);
  const repoMapRelatedFiles = (
    (payload.related_files as string[] | undefined) ||
    ((task.context as Record<string, unknown> | undefined)?.related_files as string[] | undefined) ||
    []
  );
  const providerRunner = getProviderRunner();

  // ── Deterministic repo-map holdback arm ──────────────────────────────────
  // A fraction of tasks (configurable via runtime_config.repo_map_holdback_fraction,
  // injected into project_metadata at claim time) are assigned to the "without repo
  // map" arm. The assignment is computed from a stable djb2 hash of the task ID,
  // independent of whether buildRepoMap returns content. This ensures intent-to-treat
  // analysis can use the assignment signal rather than the receipt boolean.
  // repoMapArm=true → treatment (map injected); false → holdback; null → no trial.
  //
  // Default-OFF (2026-07-10 futility bound: repo_map evaluated no_effect): absent an
  // explicit project fraction, the default is 1.0 (holdback), so buildRepoMap is NOT
  // called and no repo map is injected — saving both tokens and the discovery compute.
  // A project re-enables by setting repo_map_holdback_fraction < 1.0 (0.0 = always-on).
  const projectMetaForHoldback = task.project_metadata as Record<string, unknown> | undefined;
  const holdbackFraction = typeof projectMetaForHoldback?.repo_map_holdback_fraction === 'number'
    ? projectMetaForHoldback.repo_map_holdback_fraction
    : 1.0;
  let repoMapArm: boolean | null = null;
  if (holdbackFraction > 0) {
    repoMapArm = assignCapabilityHoldbackArm(taskId, 'repo_map', holdbackFraction) ?? true;
  }
  let repoMap = '';
  if (repoMapArm === false) {
    sendLog(ws, `[RepoMap] holdback arm for task ${taskId} (fraction=${holdbackFraction}) — suppressing repo map\n`);
  } else {
    repoMap = buildRepoMap(ws, { taskPrompt: rawPrompt, relatedFiles: repoMapRelatedFiles });
  }

  // ── Deterministic diagnostics-after-edit holdback arm ───────────────────
  // Claude-Code-only, Blueprint self-repo-only trial. Codex/foreign repos are
  // capability-not-applicable and intentionally receive no arm or receipt keys.
  // Default-OFF (2026-07-10 futility bound: diagnostics_after_edit evaluated no_effect):
  // absent an explicit project fraction, default to 1.0 (holdback) so the post-edit
  // diagnostics hook is not materialized. Re-enable via a fraction < 1.0.
  const diagnosticsHoldbackFraction = typeof projectMetaForHoldback?.diagnostics_after_edit_holdback_fraction === 'number'
    ? projectMetaForHoldback.diagnostics_after_edit_holdback_fraction
    : 1.0;
  const diagnosticsEnabled = projectMetaForHoldback?.diagnostics_after_edit_enabled === true;
  const diagnosticsEligible = providerRunner.provider === 'anthropic' && isBlueprintSelfRepo(owner, repo);
  let diagnosticsAfterEditArm: boolean | null = null;
  let hadDiagnosticsAfterEdit: boolean | undefined;
  if (diagnosticsEligible && diagnosticsHoldbackFraction > 0) {
    diagnosticsAfterEditArm = assignCapabilityHoldbackArm(taskId, 'diagnostics_after_edit', diagnosticsHoldbackFraction) ?? true;
    if (!diagnosticsAfterEditArm) {
      sendLog(ws, `[DiagnosticsAfterEdit] holdback arm for task ${taskId} (fraction=${diagnosticsHoldbackFraction}) - not writing Claude hook\n`);
    } else if (diagnosticsEnabled) {
      hadDiagnosticsAfterEdit = writeDiagnosticsAfterEditHook();
      if (!hadDiagnosticsAfterEdit) {
        sendLog(ws, '[DiagnosticsAfterEdit] hook materialization failed; continuing without post-edit diagnostics\n');
      }
    } else {
      hadDiagnosticsAfterEdit = false;
      sendLog(ws, '[DiagnosticsAfterEdit] treatment arm assigned but kill switch is disabled; continuing without post-edit diagnostics\n');
    }
  }

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
    const cappedTrimmed = capAnalysisText(trimmed, ws);
    analysisContext = cappedTrimmed.startsWith('## Pre-Implementation Analysis')
      ? `${cappedTrimmed}\n\n`
      : `## Pre-Implementation Analysis\n\n${cappedTrimmed}\n\n`;
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

      analysisContext = `## Pre-Implementation Analysis\n\n${capAnalysisText(effectiveAnalysis, ws)}\n\n`;
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

  // ── Commissioned prerequisite knowledge ──────────────────────────────────
  // When a plan made this implementation task depend on a web_research/design
  // prerequisite, the backend stamps a durable pointer onto
  // ctx.knowledge_prerequisites when that prerequisite completes. Resolve each
  // pointer to its full document body at launch (fail-soft) and frame it as
  // commissioned input so the agent builds ON it. Empty string when none, so the
  // prompt is byte-unchanged for tasks with no prerequisites.
  let prerequisiteKnowledgeSection = '';
  const rawPrereqs = ctx.knowledge_prerequisites;
  const prereqPointers: PrerequisiteKnowledgePointer[] = Array.isArray(rawPrereqs)
    ? (rawPrereqs as unknown[])
        .filter((p): p is Record<string, unknown> => !!p && typeof p === 'object')
        .map((p) => ({
          doc_id: strVal(p.doc_id),
          source_task_id: strVal(p.source_task_id),
          source_kind: strVal(p.source_kind),
          title: strVal(p.title),
          summary: strVal(p.summary),
        }))
        .filter((p) => p.doc_id)
    : [];
  if (prereqPointers.length > 0) {
    const resolved = new Map<string, PrereqKnowledgeDoc>();
    const prereqResults = await Promise.all(
      prereqPointers.map(async (p) => {
        const doc = await fetchKnowledgeDocumentById(p.doc_id);
        return { doc_id: p.doc_id, doc };
      }),
    );
    for (const { doc_id, doc } of prereqResults) {
      if (doc) resolved.set(doc_id, doc);
    }
    prerequisiteKnowledgeSection = buildPrerequisiteKnowledgeSection(prereqPointers, resolved);
    sendLog(ws, `[PrereqKnowledge] Injected ${resolved.size}/${prereqPointers.length} commissioned knowledge document(s).\n`);
  }

  let prerequisiteArtifactSection = '';
  const rawArtifactPrereqs = ctx.artifact_prerequisites;
  const artifactPointers: PrerequisiteArtifactPointer[] = Array.isArray(rawArtifactPrereqs)
    ? (rawArtifactPrereqs as unknown[])
        .filter((p): p is Record<string, unknown> => !!p && typeof p === 'object')
        .map((p) => ({
          artifact_id: strVal(p.artifact_id),
          logical_key: strVal(p.logical_key),
          artifact_type: strVal(p.artifact_type),
          source_task_id: strVal(p.source_task_id),
          source_compute_job_id: strVal(p.source_compute_job_id),
          storage_uri: strVal(p.storage_uri),
        }))
        .filter((p) => p.artifact_id)
    : [];
  const signedArtifactUrls = new Map<string, string>();
  const hydratedArtifactPaths = new Map<string, string>();
  if (artifactPointers.length > 0) {
    const urlResults = await Promise.all(
      artifactPointers.map(async (p) => {
        const url = await fetchArtifactDownloadUrl(p.artifact_id);
        return { artifact_id: p.artifact_id, url };
      }),
    );
    for (const { artifact_id, url } of urlResults) {
      if (url) signedArtifactUrls.set(artifact_id, url);
    }
    const hydrated = await hydrateComputeResultArtifacts({
      pointers: artifactPointers,
      repoRoot: WORKSPACE,
      ws,
      signedUrls: signedArtifactUrls,
    });
    for (const [artifactId, localPath] of hydrated.localPaths) {
      hydratedArtifactPaths.set(artifactId, localPath);
    }
    prerequisiteArtifactSection = buildPrerequisiteArtifactSection(
      artifactPointers,
      signedArtifactUrls,
      taskId,
      hydratedArtifactPaths,
    );
    sendLog(ws, `[PrereqArtifacts] Injected ${artifactPointers.length} commissioned artifact pointer(s); resolved ${signedArtifactUrls.size} signed URL(s).\n`);
  }

  const artifactFetchGuidance = artifactsHelperAvailable ? COMPUTE_ARTIFACT_GUIDANCE : '';
  const finalPromptWithTree = finalPrompt
    + fileTree
    + repoMap
    + analysisContext
    + prerequisiteKnowledgeSection
    + prerequisiteArtifactSection
    + artifactFetchGuidance;
  const promptFingerprint = computePromptFingerprint(taskTypeKey, buildConfig, {
    codeChangeGuidanceVariant: codeChangeGuidanceVariant({ buildConfig, owner, repo }),
    emittedFailureGuidance: assembleFailureGuidanceBlocks(isRetry),
  });

  statusRef.value = `Running ${providerRunner.displayName}...`;
  setCurrentPromptFingerprint(promptFingerprint);
  // Emit once before Claude starts so short code-change tasks persist the
  // fingerprint even if they finish before the next periodic heartbeat.
  // Send both the arm assignment (intent-to-treat) and receipt (compliance).
  const _heartbeatOpts: Record<string, unknown> = {
    prompt_fingerprint: promptFingerprint,
    had_repo_map: repoMap !== '',
    ...(promptSectionFlags?.hadModuleInterfaces !== undefined && { had_module_interfaces: promptSectionFlags.hadModuleInterfaces }),
    ...(promptSectionFlags?.hadRelatedTests !== undefined && { had_related_tests: promptSectionFlags.hadRelatedTests }),
    ...(promptSectionFlags?.hadAffectedConsumers !== undefined && { had_affected_consumers: promptSectionFlags.hadAffectedConsumers }),
    ...(promptSectionFlags?.moduleInterfacesArm !== undefined && { module_interfaces_arm: promptSectionFlags.moduleInterfacesArm }),
    ...(promptSectionFlags?.relatedTestsArm !== undefined && { related_tests_arm: promptSectionFlags.relatedTestsArm }),
    ...(promptSectionFlags?.affectedConsumersArm !== undefined && { affected_consumers_arm: promptSectionFlags.affectedConsumersArm }),
  };
  if (repoMapArm !== null) {
    _heartbeatOpts.repo_map_arm = repoMapArm;
  }
  if (diagnosticsAfterEditArm !== null) {
    _heartbeatOpts.diagnostics_after_edit_arm = diagnosticsAfterEditArm;
  }
  if (hadDiagnosticsAfterEdit !== undefined) {
    _heartbeatOpts.had_diagnostics_after_edit = hadDiagnosticsAfterEdit;
  }
  sendHeartbeatSync(taskId, statusRef.value, _heartbeatOpts);

  // ── Prompt-size telemetry ──────────────────────────────────────────────────
  // Record the assembled prompt's byte size and top-3 section breakdown so
  // getLlmTelemetry() reports prompt bulk for standard-handler runs the same
  // way it already does for plan/research. Group logically distinct sections;
  // the builder caps output at 3 largest, so fine granularity is harmless.
  // corePromptBody and srFeedbackBlock are computed once at assembly time above.
  const guidanceFooter = PLAYBOOK_GUIDANCE
    + `\nNote: If lint or test fixes required non-obvious steps, save them to the playbook (e.g., ./playbook set "lint-gotcha" "description").\n`
    + assembleFailureGuidanceBlocks(isRetry)
    + VIEWPORT_INFRA_SKIP_GUIDANCE;
  const taskNotices = acceptanceEvidenceNotice + provenanceNotice;
  const taskGuidance = taskTypeHint + firstTaskSection + taskTypeFocusBlock;
  const promptSuffixBlock = promptSuffix + prTitlePrefixNote;
  const fetchToolBlock = `\n\n${stdFetchGuidance}${artifactFetchGuidance}${DEFAULT_TOOL_GUIDANCE}`;
  const promptedTelemetry = buildPromptSizeTelemetry(finalPromptWithTree, [
    ['claude_md', claudeMdPrefix],
    ['playbook', playbookSection],
    ['retry_context', retryContext],
    ['task_notices', taskNotices],
    ['core_prompt', corePromptBody],
    ['reserved_revision', reservedRevisionGuidance],
    ['task_guidance', taskGuidance],
    ['code_change_helper', codeChangeHelperGuidance],
    ['sr_feedback', srFeedbackBlock],
    ['guidance_footer', guidanceFooter],
    ['fetch_tool_guidance', fetchToolBlock],
    ['prompt_suffix', promptSuffixBlock],
    ['file_tree', fileTree],
    ['repo_map', repoMap],
    ['analysis', analysisContext],
    ['prereq_knowledge', prerequisiteKnowledgeSection],
    ['prereq_artifacts', prerequisiteArtifactSection],
  ]);
  setPromptSizeTelemetry(
    promptedTelemetry.prompt_size_bytes,
    promptedTelemetry.prompt_section_count,
    promptedTelemetry.prompt_section_sizes,
  );

  // Record HEAD before Claude runs so we can detect commits Claude made (e.g. before rebasing)
  const baseCommit = git(['rev-parse', 'HEAD']);

  // ── Consume-and-commit delivered artifacts (Pillar 5: SHIP THE ARTIFACT) ──
  // When a direct dependency delivered a committable (GIT_COMMIT) artifact as a
  // prerequisite, fetch its bundle, extract it (path-traversal-guarded), and
  // commit the files into the repo clone so they ride the normal review gate.
  // Runs AFTER baseCommit is captured so the artifact commit advances HEAD and
  // is seen as part of the shipped diff. Fail-soft: any failure logs and
  // proceeds — the prompt section + signed URL still let the agent consume
  // manually.
  if (artifactPointers.length > 0) {
    try {
      const consumed = await consumeAndCommitArtifacts({
        taskId,
        pointers: artifactPointers,
        repoRoot: WORKSPACE,
        ws,
        signedUrls: signedArtifactUrls,
      });
      if (consumed.committedArtifactIds.length > 0) {
        sendLog(ws, `[ArtifactConsume] Committed ${consumed.committedArtifactIds.length} delivered artifact(s) into the repo: ${consumed.committedArtifactIds.join(', ')}.\n`);
      }
    } catch (e) {
      sendLog(ws, `[ArtifactConsume] consume-and-commit step failed (non-fatal): ${e instanceof Error ? e.message : String(e)}\n`);
    }
  }
  // 2026-05-04: wrapper-driven lint/test/push/PR pipeline replaced with
  // runCodeChangeTask. Claude owns its own loop via ./check + ./pr +
  // ./done + ./fail helpers. Wrapper just runs Claude, watches markers,
  // and posts terminal status. See agent/src/handlers/code-change.ts.
  const regressingCats = getRegressingCategories(projectCtxForSr);
  const thinningCats = getThinningCategories(projectCtxForSr);
  const reasoningGateCats = [...new Set([...regressingCats, ...thinningCats])];
  // For auto-remediation create tasks the backend stamped the failing pytest
  // node-ids onto the payload as ci_failure_selectors.  Forward them so the
  // generated ./check script runs those selectors against real Postgres via
  // buildCiFailureVerifyBlock — closing the SQLite-vs-Postgres masking gap
  // where a fix declared green locally could still fail CI on Postgres-only
  // assertions (see auto_remediation.py task_payload construction).
  const rawCiSelectors = Array.isArray(payload.ci_failure_selectors)
    ? (payload.ci_failure_selectors as unknown[]).filter((s): s is string => typeof s === 'string')
    : [];
  await runCodeChangeTask({
    taskId,
    prompt: finalPromptWithTree,
    taskPrompt: rawPrompt,
    ws,
    statusRef,
    workBranch,
    baseBranch,
    owner,
    repo,
    testsOnMainBranch: baseBranch === (promotionTarget ?? resolveBaseBranch({}, task)),
    baseCommit,
    model,
    effort,
    disallowedTools,
    noChangesMessage,
    analysisContext,
    regressingCategories: reasoningGateCats.length > 0 ? reasoningGateCats : undefined,
    buildConfig,
    taskCapability: task.task_capability as Partial<Record<'agent_mode' | 'run_shape' | 'output_kind', unknown>> | undefined,
    visualAcceptance: resolveVisualAcceptance(payload, ctx),
    ciFailureSelectors: rawCiSelectors.length > 0 ? rawCiSelectors : undefined,
  });

  // Fresh reviewer pass: read-only second session to catch what the implementing
  // session missed. Enabled per-project via project_metadata.enable_fresh_reviewer.
  // Skipped for auto-remediation tasks (their job IS to fix what a reviewer would flag).
  const projectMeta = task.project_metadata as Record<string, unknown> | undefined;
  const freshReviewerEnabled =
    projectMeta?.enable_fresh_reviewer === true;
  let freshReviewerAlreadyRan = false;
  if (freshReviewerEnabled && !isAutoRemediation) {
    freshReviewerAlreadyRan = true;
    const reviewerRelatedFiles = (
      (payload.related_files as string[] | undefined) ||
      ((task.context as Record<string, unknown> | undefined)?.related_files as string[] | undefined) ||
      []
    );
    await runFreshReviewerPass(
      taskId,
      taskTitle,
      rawPrompt,
      reviewerRelatedFiles,
      ws,
      statusRef,
      model,
      task.project_context as Record<string, unknown> | undefined,
      // Self-merge: persist the enabled-path reviewer output into THIS task's
      // backend _self_review_outcome (originalTaskId = taskId). No recovery flavor.
      undefined, undefined, undefined, taskId,
      sensitivePathsFromMetadata(projectMeta),
    );
  }

  // Auto-trigger fresh-reviewer when lone-checker categories overlap this task's
  // risk surface. SR is the sole corroborator for these categories, so a PASS is
  // uncorroborated. Fires regardless of enable_fresh_reviewer.
  if (!isAutoRemediation && !freshReviewerAlreadyRan) {
    const loneCheckerRelatedFiles = (
      (payload.related_files as string[] | undefined) ||
      ((task.context as Record<string, unknown> | undefined)?.related_files as string[] | undefined) ||
      []
    );
    const loneCheckerCats = loneCheckerCategoriesForTask(
      task.project_context as Record<string, unknown> | undefined,
      loneCheckerRelatedFiles,
      buildConfig,
    );
    if (loneCheckerCats.length > 0) {
      sendLog(ws, `[FreshReview] Lone-checker categories detected (${loneCheckerCats.join(', ')}) — auto-triggering fresh-reviewer pass.\n`);
      freshReviewerAlreadyRan = true;
      await runFreshReviewerPass(
        taskId, taskTitle, rawPrompt, loneCheckerRelatedFiles, ws, statusRef, model,
        task.project_context as Record<string, unknown> | undefined,
        // Self-merge: persist lone-checker recovery output into THIS task's
        // backend _self_review_outcome (originalTaskId = taskId).
        false, true, undefined, taskId,
        sensitivePathsFromMetadata(projectMeta),
      );
    }
  }

  // Auto-trigger fresh-reviewer recovery when SR admits incomplete_review and
  // the normal fresh-reviewer path hasn't already run. Fires regardless of
  // project_metadata.enable_fresh_reviewer. Skipped for auto-remediation tasks
  // (same exclusion as the normal path).
  if (!isAutoRemediation && !freshReviewerAlreadyRan) {
    const srRecoveryPath = `/tmp/self_review_${taskId}.json`;
    let srRecovery: Record<string, unknown> = {};
    try {
      const raw = await fs.promises.readFile(srRecoveryPath, 'utf-8');
      if (raw) srRecovery = JSON.parse(raw) as Record<string, unknown>;
    } catch { /* may not exist */ }
    const srCategories = Array.isArray(srRecovery.categories) ? srRecovery.categories as string[] : [];
    const existingFindings = srRecovery.reviewer_findings;
    const hasExistingFindings = Array.isArray(existingFindings) && existingFindings.length > 0;
    if (srCategories.includes('incomplete_review') && !hasExistingFindings) {
      sendLog(ws, '[FreshReview] SR reports incomplete_review — auto-triggering recovery pass.\n');
      const recoveryRelatedFiles = (
        (payload.related_files as string[] | undefined) ||
        ((task.context as Record<string, unknown> | undefined)?.related_files as string[] | undefined) ||
        []
      );
      await runFreshReviewerPass(
        taskId,
        taskTitle,
        rawPrompt,
        recoveryRelatedFiles,
        ws,
        statusRef,
        model,
        task.project_context as Record<string, unknown> | undefined,
        // Self-merge: persist incomplete-SR recovery output into THIS task's
        // backend _self_review_outcome (originalTaskId = taskId).
        true, undefined, undefined, taskId,
        sensitivePathsFromMetadata(projectMeta),
      );
    }
  }
}

// ── Handler: Create (default — make changes, push, open PR) ─────────────────
/**
 * Handle a 'create' task: implement a new feature from a prompt.
 *
 * Runs Claude Code with the provided prompt, captures output, runs tests
 * if applicable, commits changes, and opens a pull request. Supports
 * sibling-task coordination and file-overlap warnings for parallel tasks.
 *
 * @param taskId - Task UUID
 * @param payload - Task payload with 'prompt', 'branch', optional 'related_files'
 * @param task - Full task object from API
 * @param ghToken - GitHub token for repo access
 * @param owner - GitHub repo owner
 * @param repo - GitHub repo name
 * @param ws - Optional WebSocket for live log streaming
 * @param statusRef - Status message reference (mutated by handler)
 * @param model - Optional model override (e.g. 'claude-opus-4-8')
 * @param effort - Optional effort level for planning ('low', 'medium', 'high')
 *
 * Throws on fatal errors: invalid prompt, git clone failure, etc.
 */

/**
 * Cap analysis text at ANALYSIS_CONTEXT_CHAR_CAP characters.
 * When truncation fires, appends a notice and emits a warning via sendLog.
 * When text is within the cap, returns it unchanged.
 */
function capAnalysisText(text: string, ws: WebSocket | null): string {
  if (text.length <= ANALYSIS_CONTEXT_CHAR_CAP) return text;
  sendLog(ws, `[Analysis Phase] WARNING: analysis result (${text.length} chars) exceeds cap (${ANALYSIS_CONTEXT_CHAR_CAP} chars) — truncating before injection.\n`);
  return `${text.slice(0, ANALYSIS_CONTEXT_CHAR_CAP)}\n\n[Analysis truncated at ${ANALYSIS_CONTEXT_CHAR_CAP} chars — tail omitted]`;
}

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

  // Operator-requested fresh-reviewer: clone the original PR branch, run the pass, and return.
  const operatorReviewFor = taskCtx?.operator_requested_review_for as string | undefined;
  if (operatorReviewFor) {
    const prBranch = validateBranchName(strVal(payload.branch), 'branch');
    const prBaseBranch = validateBranchName(resolveBaseBranch(payload, task), 'base_branch');
    const cleanRepoUrl = `https://github.com/${owner}/${repo}.git`;

    // Check the branch still exists before cloning — auto-delete-on-merge removes
    // the head branch as soon as the PR is merged, making clone fatal. This is a
    // permanent state (not a transient error), so short-circuit cleanly.
    // Write the token to the credential-helper file and pass the helper via -c
    // so git ls-remote can authenticate without embedding the token in the URL.
    writeGitTokenFile(ghToken);
    const lsHelperPath = ensureGitCredentialHelperScript();
    let branchExists = false;
    try {
      const lsOut = execFileSync('git', ['-c', `credential.https://github.com.helper=${lsHelperPath}`, 'ls-remote', '--heads', cleanRepoUrl, prBranch], { timeout: 30000 });
      branchExists = lsOut.toString().trim().length > 0;
    } catch {
      // ls-remote itself failed (auth error, network, etc.) — let the clone attempt
      // proceed and fail loudly so non-branch errors still surface.
      branchExists = true;
    }
    if (!branchExists) {
      const skipMsg = `PR head branch '${prBranch}' no longer exists on ${owner}/${repo} — likely merged and auto-deleted. Skipping review (nothing to review).`;
      sendLog(ws, `[FreshReview] ${skipMsg}\n`);
      await persistAndPostStatus(taskId, {
        task_id: taskId,
        status: 'COMPLETED',
        artifact_type: 'ERROR_LOG',
        logs: skipMsg,
        failure_category: 'PR_BRANCH_DELETED',
      });
      return;
    }

    sendLog(ws, `[FreshReview] Operator-requested pass — cloning ${owner}/${repo} branch ${prBranch}\n`);
    await prepareWorkspaceForRole({ workspace: WORKSPACE, repoUrl: cleanRepoUrl, githubToken: ghToken, branch: prBranch, baseBranch: prBaseBranch, depth: DEFAULT_CLONE_DEPTH });
    try { git(['fetch', 'origin', `+refs/heads/${prBaseBranch}:refs/remotes/origin/${prBaseBranch}`, '--depth', String(DEFAULT_CLONE_DEPTH)]); } catch { /* ok */ }
    try {
      const mergeBase = git(['merge-base', 'HEAD', `origin/${prBaseBranch}`]).trim();
      if (mergeBase) await fs.promises.writeFile(`/tmp/base_commit_${taskId}`, mergeBase);
    } catch { /* ok */ }
    const title = strVal(task.title) || 'Operator-requested fresh review';
    await runFreshReviewerPass(taskId, title, rawPrompt || '', relatedFiles, ws, statusRef, model, task.project_context as Record<string, unknown> | undefined, false, false, true, operatorReviewFor, sensitivePathsFromMetadata(task.project_metadata as Record<string, unknown> | undefined));
    return;
  }

  const createScope = taskCtx?.scope as string | undefined;
  const isMediumOrLargeScope = createScope === 'medium' || createScope === 'large';
  const createProjectMeta = task.project_metadata as Record<string, unknown> | undefined;
  const moduleInterfacesFraction = holdbackFraction(createProjectMeta, 'module_interfaces');
  const relatedTestsFraction = holdbackFraction(createProjectMeta, 'related_tests');
  const affectedConsumersFraction = holdbackFraction(createProjectMeta, 'affected_consumers');
  const moduleInterfacesArm = isMediumOrLargeScope
    ? assignCapabilityHoldbackArm(taskId, 'module_interfaces', moduleInterfacesFraction)
    : undefined;
  const relatedTestsArm = assignCapabilityHoldbackArm(taskId, 'related_tests', relatedTestsFraction);
  const affectedConsumersArm = isMediumOrLargeScope
    ? assignCapabilityHoldbackArm(taskId, 'affected_consumers', affectedConsumersFraction)
    : undefined;

  const createBuildConfig = resolveBuildConfig(task);
  const relatedTestContext = relatedTestsArm === false
    ? ''
    : discoverRelatedTests(ws, relatedFiles, createBuildConfig);
  let moduleInterfaceContext = '';
  if (isMediumOrLargeScope && moduleInterfacesArm !== false) {
    moduleInterfaceContext = extractModuleInterfaces(ws, relatedFiles);
  }
  let affectedConsumersContext = '';
  if (isMediumOrLargeScope && affectedConsumersArm !== false) {
    affectedConsumersContext = discoverAffectedConsumers(ws, relatedFiles);
  }
  if (moduleInterfacesArm === false) logCapabilityHoldback(ws, taskId, 'module_interfaces', moduleInterfacesFraction);
  if (relatedTestsArm === false) logCapabilityHoldback(ws, taskId, 'related_tests', relatedTestsFraction);
  if (affectedConsumersArm === false) logCapabilityHoldback(ws, taskId, 'affected_consumers', affectedConsumersFraction);
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
    prTitlePrefix: resolvePrTitlePrefix(task),
    noChangesMessage: 'No file changes.',
    runLintFix: true,
    runTestFix: true,
    captureBaseline: true,
    runSelfReview: true,
    runSmallScopeAnalysis: true,
    lintContext: 'the code you just wrote',
    includeTaskTypeHints: true,
    issueReference,
    relatedFiles,
    runMigrationCheck: true,
    promptSectionFlags: {
      hadModuleInterfaces: isMediumOrLargeScope ? moduleInterfaceContext !== '' : undefined,
      hadRelatedTests: relatedTestContext !== '',
      hadAffectedConsumers: isMediumOrLargeScope ? affectedConsumersContext !== '' : undefined,
      moduleInterfacesArm,
      relatedTestsArm,
      affectedConsumersArm,
    },
  });
}

/**
 * Handle a 'refactor' task: improve code quality without changing behavior.
 *
 * Runs Claude Code with the refactoring prompt, runs tests to ensure no
 * behavioral changes, commits, and opens a PR. Respects file-overlap warnings.
 *
 * @param taskId - Task UUID
 * @param payload - Task payload with 'prompt', optional 'related_files'
 * @param task - Full task object from API
 * @param ghToken - GitHub token for repo access
 * @param owner - GitHub repo owner
 * @param repo - GitHub repo name
 * @param ws - Optional WebSocket for live log streaming
 * @param statusRef - Status message reference (mutated by handler)
 * @param model - Optional model override
 * @param effort - Optional effort level
 *
 * Throws on fatal errors: missing prompt, git failure, etc.
 */
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
  const refactorScope = strVal((task.context as Record<string, unknown> | undefined)?.scope);
  const refactorIsMediumOrLargeScope = refactorScope === 'medium' || refactorScope === 'large';
  const refactorProjectMeta = task.project_metadata as Record<string, unknown> | undefined;
  const refactorModuleInterfacesFraction = holdbackFraction(refactorProjectMeta, 'module_interfaces');
  const refactorRelatedTestsFraction = holdbackFraction(refactorProjectMeta, 'related_tests');
  const refactorAffectedConsumersFraction = holdbackFraction(refactorProjectMeta, 'affected_consumers');
  const refactorModuleInterfacesArm = refactorIsMediumOrLargeScope
    ? assignCapabilityHoldbackArm(taskId, 'module_interfaces', refactorModuleInterfacesFraction)
    : undefined;
  const refactorRelatedTestsArm = assignCapabilityHoldbackArm(taskId, 'related_tests', refactorRelatedTestsFraction);
  const refactorAffectedConsumersArm = refactorIsMediumOrLargeScope
    ? assignCapabilityHoldbackArm(taskId, 'affected_consumers', refactorAffectedConsumersFraction)
    : undefined;

  const refactorBuildConfig = resolveBuildConfig(task);
  const refactorRelatedTestContext = refactorRelatedTestsArm === false
    ? ''
    : discoverRelatedTests(ws, refactorRelatedFiles, refactorBuildConfig);
  let refactorModuleInterfaceContext = '';
  if (refactorIsMediumOrLargeScope && refactorModuleInterfacesArm !== false) {
    refactorModuleInterfaceContext = extractModuleInterfaces(ws, refactorRelatedFiles);
  }
  let refactorAffectedConsumersContext = '';
  if (refactorIsMediumOrLargeScope && refactorAffectedConsumersArm !== false) {
    refactorAffectedConsumersContext = discoverAffectedConsumers(ws, refactorRelatedFiles);
  }
  if (refactorModuleInterfacesArm === false) logCapabilityHoldback(ws, taskId, 'module_interfaces', refactorModuleInterfacesFraction);
  if (refactorRelatedTestsArm === false) logCapabilityHoldback(ws, taskId, 'related_tests', refactorRelatedTestsFraction);
  if (refactorAffectedConsumersArm === false) logCapabilityHoldback(ws, taskId, 'affected_consumers', refactorAffectedConsumersFraction);
  const refactorBudget = refactorScope ? SCOPE_OVERFLOW_THRESHOLDS[refactorScope] : undefined;
  const refactorScopeBudgetNote = refactorBudget
    ? `\n\n## Scope File Budget\n\nYour scope budget allows at most ${refactorBudget} non-test production files. If your changes would exceed this, focus on the single most critical subset and note what was deferred.\n`
    : '';

  const refactorTitle = strVal(task.title, 'Refactor');
  await runStandardHandler({
    taskId, payload, task, ghToken, owner, repo, ws, statusRef, model, effort,
    branchPrefix: 'refactor',
    promptSectionFlags: {
      hadModuleInterfaces: refactorIsMediumOrLargeScope ? refactorModuleInterfaceContext !== '' : undefined,
      hadRelatedTests: refactorRelatedTestContext !== '',
      hadAffectedConsumers: refactorIsMediumOrLargeScope ? refactorAffectedConsumersContext !== '' : undefined,
      moduleInterfacesArm: refactorModuleInterfacesArm,
      relatedTestsArm: refactorRelatedTestsArm,
      affectedConsumersArm: refactorAffectedConsumersArm,
    },
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

This task follows the standard code-change loop: make the docs changes, run the repo's checks, and open a PR with a docs-prefixed title when the work is complete.

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

5. Once tests pass, run \`./check\` to confirm lint/build, then \`./pr open --title "deps: ..." --body "Summary of bumps"\` to ship and \`./done\`.

IMPORTANT: Only update minor/patch versions. If a package would require a major version bump to get any update, skip it. Focus on security patches and bug fixes.`,
    commitPrefix: 'chore',
    prTitlePrefix: 'chore:',
    noChangesMessage: 'No dependency updates needed.',
    runLintFix: false,
    runTestFix: false,
  });
}

// ── Handler: Salvage Bundle (consolidated fix for decayed findings) ──────
/**
 * Handle a 'salvage_bundle' task: consolidate multiple decayed/discarded research
 * findings into a SINGLE pull request.
 *
 * Extracts the salvaged findings from the payload, builds a prompt instructing
 * the agent to produce ONE consolidated PR addressing all findings, then
 * delegates to the standard create handler flow.
 */
export async function handleSalvageBundle(
  taskId: string, payload: Record<string, unknown>, task: Record<string, unknown>,
  ghToken: string, owner: string, repo: string, ws: WebSocket | null, statusRef: { value: string },
  model?: string, effort?: string
): Promise<void> {
  const rawFindings = payload.salvaged_findings;
  const salvagedFindings = Array.isArray(rawFindings)
    ? rawFindings as Array<Record<string, unknown>>
    : [];
  const findingsText = salvagedFindings.length > 0
    ? salvagedFindings.map((f, i) => {
        let text = `${i + 1}. ${strVal(f.title) || '(untitled)'} (reason: ${strVal(f.discard_reason) || 'unknown'})`;
        const prompt = strVal(f.prompt);
        if (prompt) {
          text += `\n   Prompt: ${prompt.slice(0, 500)}`;
        } else {
          const desc = strVal(f.description);
          if (desc) text += `\n   Description: ${desc.slice(0, 200)}`;
        }
        const claims = f.claims;
        if (Array.isArray(claims) && claims.length > 0) {
          const claimLines = claims.map((c: unknown) => {
            const claimStr = typeof c === 'string' ? c : strVal((c as Record<string, unknown>).claim ?? (c as Record<string, unknown>).text ?? '');
            return claimStr ? `     - ${claimStr}` : '';
          }).filter(Boolean);
          if (claimLines.length > 0) text += `\n   Claims:\n${claimLines.join('\n')}`;
        }
        const files = f.related_files;
        if (Array.isArray(files) && files.length > 0) {
          const fileList = files.map((rf: unknown) =>
            strVal(typeof rf === 'string' ? rf : (rf as Record<string, unknown>).path ?? (rf as Record<string, unknown>).file ?? '')
          ).filter(Boolean).join(', ');
          if (fileList) text += `\n   Related files: ${fileList}`;
        }
        return text;
      }).join('\n')
    : 'No specific findings provided.';
  const prompt = `SALVAGE BUNDLE — ${salvagedFindings.length} decayed finding(s)

Salvaged findings:
${findingsText}

INSTRUCTIONS:
- Fix ALL of the above findings in a SINGLE pull request.
- Do NOT split this into multiple PRs.
- For each finding, scan its "Related files" — those files are the most
  likely locations for the needed change. Start by reading them.
- When a fix changes logic (not just comments/whitespace/formatting),
  add or update tests that cover the changed behavior.
- In the PR description, include a table mapping every finding to the
  change that addresses it. Example format:
  | Finding | Change | Files |
  |---------|--------|-------|
  | ...     | ...    | ...   |
- Run the project's checks and open one PR with all fixes integrated.`;

  const enrichedPayload = { ...payload, prompt };
  return handleCreate(taskId, enrichedPayload, task, ghToken, owner, repo, ws, statusRef, model, effort);
}
