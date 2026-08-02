import fs from 'fs';
import { execFileSync } from 'child_process';
/**
 * Review and fix handlers — code review and iterative repair.
 *
 * - **handleReview**: Comments on an open PR's code diff. Checks out the PR
 *   branch, analyzes changes, and posts review comments via GitHub API (no local
 *   changes or new commit). Useful for human-in-the-loop workflows.
 *
 * - **handleFix**: Repairs code issues identified in a prior review or analysis.
 *   Checks out the PR branch, makes fixes, commits, and pushes updates to the
 *   same PR. Implements feedback from parent review tasks or self-improvement loops.
 *
 * These handlers form the review→fix cycle: a review task identifies issues,
 * then downstream fix tasks address them. Fix tasks can loop: the fix handler
 * may run tests, analyze failures, iterate, and re-push until COMPLETED.
 */

import type WebSocket from 'ws';
import {
  WORKSPACE, strVal, sanitize, sendLog, validateBranchName,
  persistAndPostStatus, git, gitAsync,
  runClaudeCode, sendHeartbeatSync, setCurrentPromptFingerprint,
  extractPytestSelectorsFromOutput,
  snapshotToolTrace, extractInvestigatedFiles,
  resolveBaseBranch, getAgentToken,
} from '../index.js';
import { captureTestBaseline, testFixLoop } from '../utils/test-fix-loop.js';
import { runCodeChangeTask } from './code-change.js';
import { writeFetchHelper, buildFetchGuidance, PLAN_FETCH_VERBS } from './strategic-helpers.js';
import { prepareWorkspaceForRole } from '../workspacePrep.js';
import type { BuildConfig } from '../index.js';
import { DEFAULT_CLONE_DEPTH, SCOPE_OVERFLOW_THRESHOLDS, PUSH_FORCE_WITH_LEASE_MAX_RETRIES, PUSH_FORCE_WITH_LEASE_RETRY_DELAY, CONFLICT_CHECK_TIMEOUT, FRONTEND_NPM_TIMEOUT, shouldFanOutReview, shouldFanOutFix } from '../constants.js';
import {
  getTaskTypeHints, PLAYBOOK_GUIDANCE, FIRST_TASK_GUIDANCE, DEFAULT_TOOL_GUIDANCE,
  buildCodeChangeHelperGuidance, codeChangeGuidanceVariant,
  FIX_TASK_FOCUS, getTaskTypeFocusBlock, PARALLEL_REVIEW_GUIDANCE, PARALLEL_FIX_GUIDANCE,
  assembleFailureGuidanceBlocks,
  readClaudeMd, formatProjectContext, configureAgentPermissions, computePromptFingerprint,
  writePlaybookHelper, writeMessagesHelper, formatPlaybook, buildRetryContext, discoverRelatedTests, formatSelfReviewBlindSpots, formatProvenanceWarnings, formatAcceptanceEvidence, buildViewportScreenshotSection, type PlaybookEntry,
} from '../prompt.js';
import {
  formatProjectVision,
  formatRejectionPatternsSection,
  formatStrategicIntent,
  formatRepoOverview,
  formatDomainOverview,
  type RejectionPatternsSource,
} from '../prompts/context-prefix.js';
import { resolveBuildConfig } from '../build-config.js';
import {
  REVIEW_CLAUDEMD_PROMPT_HEADER, REVIEW_CLAUDEMD_PROMPT_FOOTER,
  REVIEW_QA_FOCUS_HINT, REVIEW_MIGRATE_FOCUS_HINT,
  REVIEW_SMALL_SCOPE_PREAMBLE, REVIEW_LARGE_SCOPE_PREAMBLE,
  REVIEW_FIX_RESPONSES_HEADER, REVIEW_FIX_RESPONSES_INSTRUCTIONS,
  buildReviewIterationPreamble,
  buildReviewScopeOverflowPreamble, buildReviewScopeBudgetSection,
  buildReviewFixResponsesIntro,
  buildReviewMasterPreamble, buildReviewDiffAccessHint,
  buildReviewCoreGuidance, buildReviewVerdictInstructions,
  buildRollupReviewGuidance, buildPlannedFilesClassificationGuidance,
  computeReviewPromptFingerprint,
} from '../prompts/review-prompt.js';
export {
  buildReviewIterationPreamble,
  computeReviewPromptFingerprint,
  buildTypeCheckInstructions,
} from '../prompts/review-prompt.js';
import { repairStructuredOutput } from './structured-output-repair.js';
import { providerAutoInjectsClaudeMd } from '../providers/runners.js';

// ── Extracted helper modules ──────────────────────────────────────────────────
import type { DriftSignal, ReviewHistoryEntry } from './review-fix-history.js';
import { computeMaxFixIterations, formatTrajectorySection, formatDriftSection } from './review-fix-history.js';
export { computeMaxFixIterations, formatTrajectorySection, formatDriftSection, verifyStepsOk } from './review-fix-history.js';
import { detectMigrationConflict, buildMigrationConflictGuidance, extractReservedMigrationRevision } from './review-fix-migration.js';
import { resolveMigrationLayout } from './specialized.js';
import { downloadViewportShots } from '../utils/viewport-shot-download.js';
import { recordViewportScreenshotDownload, buildPromptSizeTelemetry, setPromptSizeTelemetry } from '../telemetry.js';
export { detectMigrationConflict, buildMigrationConflictGuidance } from './review-fix-migration.js';
export type { MigrationConflict } from './review-fix-migration.js';

/**
 * Push with --force-with-lease, retrying if another agent raced us.
 *
 * "stale info" is the lease-check rejection emitted when the remote branch
 * advanced between our last fetch and this push — typically because another
 * agent on a sibling task of the same research parent pushed first. Retrying
 * unconditionally with plain --force would clobber their work; instead we
 * refetch, rebase our local commits onto the new remote tip, and then push
 * with a fresh lease. With 3+ sibling agents racing, a single retry may also
 * be stale — allow up to `maxRetries` retries after the initial attempt, with
 * a brief delay between successive retries to reduce contention. Any non-
 * stale push failure and any rebase conflict surface immediately.
 */
export type PushRejectionCategory =
  | 'workflow-permission'
  | 'push-protection'
  | 'protected-branch';

export function classifyDeterministicPushRejection(errMsg: string): {
  category: PushRejectionCategory;
  message: string;
} | null {
  const msg = errMsg.toLowerCase();
  if (
    msg.includes('refusing to allow a github app to create or update workflow')
    || msg.includes('workflow permission')
    || msg.includes('workflows permission')
  ) {
    return {
      category: 'workflow-permission',
      message:
        'GitHub rejected the push because this GitHub App cannot create or update workflow files without the workflows permission. Revert the workflow file(s) before pushing, or use a token with workflows permission.',
    };
  }
  if (
    msg.includes('gh013')
    || msg.includes('secret scanning')
    || msg.includes('push protection')
    || msg.includes('blocked by secret scanning')
    || msg.includes('contains secrets')
  ) {
    return {
      category: 'push-protection',
      message:
        'GitHub rejected the push because push protection or secret scanning blocked it. Remove the secret or revert the offending file(s) before pushing.',
    };
  }
  if (
    msg.includes('protected branch')
    || msg.includes('branch protection')
    || msg.includes('gh006')
    || msg.includes('required status check')
    || msg.includes('admin rights required')
  ) {
    return {
      category: 'protected-branch',
      message:
        'GitHub rejected the push because the branch is protected. This push cannot succeed with force-with-lease; push to an unprotected branch or update branch protection.',
    };
  }
  return null;
}

export async function pushForceWithLeaseRetrying(
  branch: string,
  ws: WebSocket | null,
  maxRetries: number = PUSH_FORCE_WITH_LEASE_MAX_RETRIES,
): Promise<void> {
  let lastErr: unknown;
  try {
    await gitAsync(['push', '--force-with-lease', 'origin', branch]);
    return;
  } catch (e) {
    const errMsg = e instanceof Error ? e.message : String(e);
    if (!errMsg.includes('stale info')) {
      const classified = classifyDeterministicPushRejection(errMsg);
      if (classified) {
        sendLog(ws, `${classified.message}\n`);
        throw new Error(classified.message);
      }
      throw e;
    }
    lastErr = e;
    sendLog(ws, `Push rejected (stale info) — concurrent push detected, will retry up to ${maxRetries} time(s).\n`);
  }
  for (let attempt = 1; attempt <= maxRetries; attempt++) {
    if (attempt > 1) {
      await new Promise(resolve => setTimeout(resolve, PUSH_FORCE_WITH_LEASE_RETRY_DELAY));
    }
    sendLog(ws, `Retry attempt ${attempt}/${maxRetries} — refetching ${branch} and rebasing...\n`);
    git(['fetch', 'origin', `+refs/heads/${branch}:refs/remotes/origin/${branch}`]);
    try {
      git(['rebase', `origin/${branch}`]);
    } catch {
      try { git(['rebase', '--abort']); } catch { /* ignore */ }
      throw new Error(`Rebase onto updated origin/${branch} conflicted after concurrent push`);
    }
    try {
      await gitAsync(['push', '--force-with-lease', 'origin', branch]);
      sendLog(ws, `Retry push to ${branch} succeeded on attempt ${attempt}/${maxRetries}.\n`);
      return;
    } catch (e) {
      const errMsg = e instanceof Error ? e.message : String(e);
      if (!errMsg.includes('stale info')) {
        const classified = classifyDeterministicPushRejection(errMsg);
        if (classified) {
          sendLog(ws, `${classified.message}\n`);
          throw new Error(classified.message);
        }
        throw e;
      }
      lastErr = e;
      if (attempt < maxRetries) {
        sendLog(ws, `Retry ${attempt}/${maxRetries} also rejected (stale info) — another concurrent push, will retry.\n`);
      }
    }
  }
  throw lastErr;
}

// ── PR-body metadata footer ───────────────────────────────────────────────────
// Builds a compact badge-style footer appended/updated in the GitHub PR body
// after each review-fix iteration. Exported for testing; callers should treat
// patchPrBodyMeta as best-effort and swallow its errors.
export function buildBlueprintPrFooter(opts: {
  iterations?: number;
  maxIterations?: number;
  directionId?: string;
  directionTitle?: string;
  provider?: string;
  model?: string;
}): string {
  const parts: string[] = [];
  if (opts.iterations && opts.iterations > 1) {
    const iterLabel = opts.maxIterations
      ? `Review iteration ${opts.iterations} of ${opts.maxIterations}`
      : `Review iteration ${opts.iterations}`;
    parts.push(iterLabel);
  }
  if (opts.directionId) {
    const title = opts.directionTitle
      ? `${opts.directionTitle.slice(0, 60)} (${opts.directionId})`
      : opts.directionId;
    parts.push(`direction: ${title}`);
  }
  if (opts.provider || opts.model) {
    const modelStr = opts.model ? ` (${opts.model})` : '';
    parts.push(`provider: ${opts.provider || 'unknown'}${modelStr}`);
  }
  if (parts.length === 0) return '';
  return `<!-- blueprint:meta -->\n*${parts.join(' · ')}*`;
}

// Medium-scope priority hint based on which subsystem the PR touches. The
// reviewer otherwise weights every checklist section equally, accumulating
// off-target findings (e.g. SQL injection notes on agent-only TypeScript
// changes) that drive extra fix iterations. Returns '' when no buildConfig
// directories are configured, or when files don't match any configured dir.
export function detectMediumScopeSubsystemHint(changedFiles: string, buildConfig?: BuildConfig): string {
  const paths = changedFiles
    .split('\n')
    .map(l => l.trim())
    .filter(l => l && !l.startsWith('('))
    .map(l => l.split(/\s+/).pop() || '')
    .filter(Boolean);
  if (paths.length === 0) return '';

  const dirs = buildConfig?.directories;
  if (!dirs?.length) return '';

  // Match each path to the first configured directory whose path it starts with.
  const matchedDirPaths = new Set<string>();
  for (const p of paths) {
    for (const d of dirs) {
      const prefix = d.path.endsWith('/') ? d.path : d.path + '/';
      if (p === d.path || p.startsWith(prefix)) {
        matchedDirPaths.add(d.path);
        break;
      }
    }
  }

  if (matchedDirPaths.size === 0) return '';

  if (matchedDirPaths.size >= 2) {
    const list = Array.from(matchedDirPaths).sort().join(', ');
    return `This is a medium-scope cross-system change touching multiple subsystems (${list}). Prioritize interface consistency between subsystems over single-file style concerns: shared types, API contracts, payload shapes, and field names must match across boundaries. Verify both sides of every integration point are updated together — e.g., a backend response field has a corresponding frontend/agent reader, a new payload key is produced and consumed, schema changes are reflected in serializers and TypeScript types. Cross-file inconsistencies (mismatched names, missing updates on one side, divergent assumptions about shape/nullability) are higher severity than local style issues.\n\n`;
  }

  const dirPath = Array.from(matchedDirPaths)[0];
  const dir = dirs.find(d => d.path === dirPath);
  if (!dir) return '';

  if (dir.type === 'python') {
    const dirPrefix = dir.path.endsWith('/') ? dir.path : dir.path + '/';
    const matchedPaths = paths.filter(p => p === dir.path || p.startsWith(dirPrefix));
    const isAllTests = matchedPaths.every(p =>
      p.includes('/testing/') || /(^|\/)test_[^/]+\.py$|(^|\/)conftest\.py$/.test(p)
    );
    if (isAllTests) {
      return `This is a medium-scope change confined to test files in ${dir.path}/. Deprioritize SQL injection, authorization, and input-validation checks — these tests do not handle real user input or production database connections. Focus on test quality: correct assertions, test isolation, fixture cleanup, and coverage of the intended behavior.\n\n`;
    }
    return `This is a medium-scope change confined to ${dir.path}/. Apply full security scrutiny — review SQL injection, authorization checks, input validation, and server-side concerns thoroughly.\n\n`;
  }

  if (dir.type === 'node') {
    return `This is a medium-scope change confined to ${dir.path}/ (TypeScript/Node.js). This code does not directly access the database, so deprioritize SQL injection and database query checks. Focus on Correctness & Logic, Build & Type Safety, and TypeScript-specific concerns.\n\n`;
  }

  // other types — external family repos (Ruby, Gradle, Maven, Elixir, .NET, PHP, Makefile, etc.)
  return `This is a medium-scope change confined to ${dir.path}/ (external family: non-core language/toolchain). Review this code for correctness and the conventions appropriate to its ecosystem. Deprioritize SQL injection checks unless you know this code directly queries a database.\n\n`;
}

// Pre-install node_modules in every node-typed directory in the reviewer's
// workspace. Best-effort: any individual install failure is logged and
// swallowed so the review can still proceed — the prompt also instructs
// reviewers to install before building, this is the belt to that suspenders.
//
// For repos with a configured BuildConfig, iterates buildConfig.directories.
// For projects with buildConfig.directories, iterates the configured node directories.
// Otherwise scans the workspace root and immediate subdirectories for package.json and
// installs in every directory it finds.
function preinstallNodeDeps(
  workspace: string,
  buildConfig: BuildConfig | undefined,
  ws: WebSocket | null,
  taskId: string,
): void {
  const targets: string[] = [];
  if (buildConfig?.directories) {
    for (const d of buildConfig.directories) {
      if (d.type === 'node') targets.push(d.path.replace(/\/$/, ''));
    }
  } else {
    // Generic fallback: scan root and top-level directories for package.json,
    // mirroring the detectTestRunners pattern in index.ts.
    const candidates: string[] = [''];  // '' = workspace root
    try {
      for (const entry of fs.readdirSync(workspace, { withFileTypes: true })) {
        if (entry.isDirectory() && !entry.name.startsWith('.') && entry.name !== 'node_modules') {
          candidates.push(entry.name);
        }
      }
    } catch { /* best-effort: directory listing failed */ }
    for (const candidate of candidates) {
      const checkPath = candidate ? `${workspace}/${candidate}` : workspace;
      if (fs.existsSync(`${checkPath}/package.json`)) {
        targets.push(candidate);
      }
    }
  }
  for (const rel of targets) {
    const cwd = rel ? `${workspace}/${rel}` : workspace;
    const label = rel || '<root>';
    try {
      sendHeartbeatSync(taskId, `npm ci (${label})...`);
      execFileSync('npm', ['ci'], { cwd, encoding: 'utf-8', timeout: FRONTEND_NPM_TIMEOUT });
      sendLog(ws, `[review-prep] npm ci OK in ${label}\n`);
    } catch (e) {
      // Non-fatal: log the failure so a reviewer that DOES try to build can see
      // why deps are missing, but don't block the review.
      sendLog(ws, `[review-prep] npm ci failed in ${label}: ${String(e).slice(0, 300)}\n`);
    }
  }
}

// ── Lane-resume section (review-fix improvement-lane pilot) ──────────────────
// Cap rendered items per lane-resume list. The backend already bounds the block;
// this is a belt-and-suspenders render cap so a malformed oversized block can
// never balloon the prompt.
const _RESUME_LIST_RENDER_MAX = 12;

// Renders the bounded `lane_resume` block the backend attaches to a lane-arm
// FIX task context (accumulated chain knowledge: prior phase summaries,
// changed/critical files, open issues, known risks, last checkpoint). Returns
// '' when the block is absent or carries no usable signal — so the section
// renders only on lane-arm fix tasks and never on classic/control fixes or any
// reviewer (the key is stripped from review/audit spawns backend-side).
export function formatLaneResumeSection(
  resume: Record<string, unknown> | undefined | null,
): string {
  if (!resume || typeof resume !== 'object') return '';
  const lines: string[] = [];
  const fmtList = (key: string, label: string) => {
    const v = resume[key];
    if (Array.isArray(v) && v.length > 0) {
      const items = v
        .filter((x): x is string => typeof x === 'string' && x.length > 0)
        .slice(0, _RESUME_LIST_RENDER_MAX);
      if (items.length > 0) {
        lines.push(`${label}:\n${items.map(i => `  - ${i}`).join('\n')}`);
      }
    }
  };
  fmtList('phase_log', 'Prior phases of this chain');
  fmtList('critical_files', 'Files changed across the chain');
  fmtList('remaining', 'Open issues carried into this fix');
  fmtList('failing_tests', 'Known failing tests / test-failure summary');
  fmtList('risks', 'Known risks / deferred items');
  fmtList('progress', 'Progress so far');
  const sha = typeof resume.last_checkpoint_sha === 'string' ? resume.last_checkpoint_sha : '';
  if (lines.length === 0 && !sha) return '';
  const seq = typeof resume.checkpoint_seq === 'number' ? resume.checkpoint_seq : undefined;
  const phase = typeof resume.last_phase === 'string' ? resume.last_phase : '';
  const headerSuffix = seq !== undefined && seq > 0
    ? ` (checkpoint ${seq}${phase ? `, last phase ${phase}` : ''})`
    : '';
  const shaLine = sha ? `Resumed from checkpoint SHA \`${sha}\`.\n` : '';
  return (
    `## Lane Resume — accumulated chain knowledge${headerSuffix}\n` +
    shaLine +
    `This fix runs on a persistent improvement lane. The context below accumulated ` +
    `across prior phases of this chain — use it to avoid re-investigating from scratch ` +
    `(it is supplementary; trust the current review feedback for what to change):\n\n` +
    `${lines.join('\n\n')}\n\n`
  );
}

// ── Handler: Review (review a PR, output verdict) ───────────────────────────
function validatedCachedReviewVerdict(
  payload: Record<string, unknown>,
  reviewTarget: Record<string, unknown> | undefined,
): string | undefined {
  const raw = strVal(payload.cached_verdict_json);
  if (!raw) return undefined;
  try {
    const parsed = JSON.parse(raw) as Record<string, unknown>;
    const verdict = parsed.verdict;
    if (verdict !== 'APPROVE' && verdict !== 'REQUEST_CHANGES') return undefined;
    const targetBaseSha = strVal(reviewTarget?.base_sha);
    const targetHeadSha = strVal(reviewTarget?.head_sha);
    const cachedBaseSha = strVal(parsed.reviewed_base_sha);
    const cachedHeadSha = strVal(parsed.reviewed_head_sha);
    const isSha = (sha: string) => /^[0-9a-f]{7,64}$/i.test(sha);
    if (!isSha(targetBaseSha) || !isSha(targetHeadSha)
      || !isSha(cachedBaseSha) || !isSha(cachedHeadSha)
      || cachedBaseSha !== targetBaseSha || cachedHeadSha !== targetHeadSha) {
      return undefined;
    }
    return JSON.stringify(parsed);
  } catch {
    return undefined;
  }
}

type OpenLedgerIssue = {
  issue_id?: string;
  severity?: string;
  description?: string;
  file?: string;
  line?: number;
};

function authoritativeOpenLedgerIssues(
  reviewLoop: Record<string, unknown> | undefined,
): OpenLedgerIssue[] | undefined {
  const ledger = reviewLoop?.issue_ledger as Record<string, unknown> | undefined;
  if (!ledger || !Array.isArray(ledger.open)) return undefined;
  return ledger.open.filter(
    (item): item is OpenLedgerIssue => Boolean(item) && typeof item === 'object',
  );
}

function fmtIssueLocation(issue: { file?: string; line?: number }): string {
  if (issue.file && typeof issue.line === 'number') return ` [${issue.file}:${issue.line}]`;
  if (issue.file) return ` [${issue.file}]`;
  return '';
}

function formatAuthoritativeLedgerSection(
  reviewLoop: Record<string, unknown> | undefined,
  role: 'review' | 'fix',
): string {
  const open = authoritativeOpenLedgerIssues(reviewLoop) ?? [];
  if (open.length === 0) return '';
  const rows = open.map(issue => {
    const issueId = issue.issue_id ? ` [${issue.issue_id}]` : '';
    const severity = (issue.severity || 'major').toUpperCase();
    return `- [${severity}]${issueId}${fmtIssueLocation(issue)} ${issue.description || '(missing description)'}`;
  });
  const roleInstruction = role === 'review'
    ? `Re-raise an unresolved blocker with its exact BP-... id, or record an explicit supported disposition.`
    : `Address every blocker below. If one is wrong or needs qualification, provide a concrete structured response so the next exact-head reviewer can verify it; fix agents cannot close blockers.`;
  return `## Authoritative open blockers\n` +
    `This is the complete binding blocker set derived by the backend. The compact trajectory is historical context only; ` +
    `do not infer closure from omission or wording changes. ${roleInstruction}\n${rows.join('\n')}\n\n`;
}

export async function handleReview(
  taskId: string, payload: Record<string, unknown>, task: Record<string, unknown>,
  ghToken: string, owner: string, repo: string, ws: WebSocket | null, statusRef: { value: string },
  model?: string, effort?: string
) {
  const prNumber = Number(payload.pr_number);
  if (!Number.isInteger(prNumber) || prNumber <= 0) throw new Error(`Invalid pr_number: ${String(payload.pr_number)}`);
  const branch = validateBranchName(strVal(payload.branch), 'branch');
  const baseBranch = validateBranchName(resolveBaseBranch(payload, task), 'base_branch');
  const originalPrompt = strVal(payload.original_prompt);

  // Read review iteration from task context
  const taskContext = task.context as Record<string, unknown> | undefined;
  const reviewLoop = taskContext?.review_loop as Record<string, unknown> | undefined;
  const iteration = (reviewLoop?.iteration as number | undefined) || 1;
  const reviewHistory = (reviewLoop?.review_history as ReviewHistoryEntry[] | undefined) || [];
  const taskScope = (taskContext?.scope as string | undefined) || 'medium';
  const origTaskType = strVal(payload.original_task_type || '');
  // A follow-up reviewer may be given the exact head SHA the prior review
  // examined.  Treat it as an opaque Git object only after validating its
  // shape and resolving it locally below; a missing or unavailable object must
  // preserve the established full-review path rather than guessing a range.
  const suppliedDeltaBaseSha = strVal(payload.delta_base_sha) || strVal(reviewLoop?.delta_base_sha);
  const validDeltaBaseSha = /^[0-9a-f]{7,64}$/i.test(suppliedDeltaBaseSha)
    ? suppliedDeltaBaseSha
    : '';
  const reviewTarget = reviewLoop?.review_target as Record<string, unknown> | undefined;
  const reviewTargetBaseSha = strVal(reviewTarget?.base_sha);

  // Deduplication is defense in depth: the backend may dispatch a normal
  // review task carrying a completed structured verdict for this exact target.
  // Reuse it only when its JSON parses, contains a recognized terminal
  // verdict, and proves it reviewed this task's exact immutable target.
  // Missing or mismatched provenance follows the normal independent review.
  const cachedVerdictJson = validatedCachedReviewVerdict(payload, reviewTarget);
  if (cachedVerdictJson) {
    const sourceTaskId = strVal(payload.cached_review_task_id);
    const reuseMessage = sourceTaskId
      ? `Reused completed review verdict from task ${sourceTaskId}; no new model call was needed.`
      : 'Reused a completed review verdict; no new model call was needed.';
    sendLog(ws, reuseMessage + '\n');
    await persistAndPostStatus(taskId, {
      task_id: taskId,
      status: 'COMPLETED',
      logs: reuseMessage,
      verdict_json: cachedVerdictJson,
    });
    return;
  }

  // Clone the full repo so Claude can explore everything, then checkout the PR branch
  statusRef.value = `Cloning ${owner}/${repo} (PR #${prNumber})...`;
  sendLog(ws, `Reviewing PR #${prNumber} on branch ${branch} (iteration ${iteration})\n`);
  const cloneUrl = `https://x-access-token:${ghToken}@github.com/${owner}/${repo}.git`;
  // Check the branch still exists before cloning — auto-delete-on-merge removes
  // the head branch as soon as the PR is merged, making clone fatal. This is a
  // permanent state (not a transient error), so short-circuit cleanly.
  let reviewBranchExists = false;
  try {
    const lsOut = execFileSync('git', ['ls-remote', '--heads', cloneUrl, branch], { timeout: 30000 });
    reviewBranchExists = lsOut.toString().trim().length > 0;
  } catch {
    // ls-remote itself failed (auth error, network, etc.) — let the clone attempt
    // proceed and fail loudly so non-branch errors still surface.
    reviewBranchExists = true;
  }
  if (!reviewBranchExists) {
    const skipMsg = `PR head branch '${branch}' no longer exists on ${owner}/${repo} — likely merged and auto-deleted. Skipping review (nothing to review).`;
    sendLog(ws, skipMsg + '\n');
    await persistAndPostStatus(taskId, {
      task_id: taskId,
      status: 'COMPLETED',
      artifact_type: 'ERROR_LOG',
      logs: skipMsg,
      failure_category: 'PR_BRANCH_DELETED',
    });
    return;
  }
  // Shallow clone of the PR branch directly — avoids fetch+checkout issues with --single-branch
  await prepareWorkspaceForRole({
    workspace: WORKSPACE, repoUrl: cloneUrl, githubToken: ghToken,
    branch, baseBranch, depth: DEFAULT_CLONE_DEPTH,
    // Gate integrity: a reviewer NEVER warm-resumes onto a lane workspace.
    // The review-fix lane pilot lane-izes only the fix side; the review path
    // stays cold and independent. Defense in depth against a leaked lane env.
    allowLaneResume: false,
  });
  // Prefer an exact prior-review SHA for follow-ups.  This avoids both the
  // ambiguous HEAD~1 heuristic and eagerly fetching/scanning the full
  // base-branch diff.  A shallow clone can lack an older commit, so prove the
  // object is available before selecting delta mode; otherwise retain the
  // existing full-review behavior.
  let deltaBaseSha = '';
  // A delta review deliberately does not fetch the base branch. Its task
  // target is therefore the first provenance source; GitHub PR metadata below
  // supplies the current base SHA when the target was not populated.
  let reviewedBaseSha = /^[0-9a-f]{7,64}$/i.test(reviewTargetBaseSha)
    ? reviewTargetBaseSha
    : '';
  if (suppliedDeltaBaseSha && !validDeltaBaseSha) {
    sendLog(ws, `WARNING: Ignoring invalid delta_base_sha; falling back to full PR review.\n`);
  } else if (validDeltaBaseSha) {
    try {
      git(['cat-file', '-e', `${validDeltaBaseSha}^{commit}`]);
      deltaBaseSha = validDeltaBaseSha;
      sendLog(ws, `Running exact-SHA delta review: ${deltaBaseSha}..HEAD\n`);
    } catch (e) {
      sendLog(ws, `WARNING: delta_base_sha ${validDeltaBaseSha} is unavailable locally; falling back to full PR review: ${String(e)}\n`);
    }
  }
  // Full reviews need the base ref. Delta reviews intentionally do not fetch
  // it: their primary evidence is the exact prior-head-to-current-head range.
  if (!deltaBaseSha) {
    try {
      git(['fetch', 'origin', `+refs/heads/${baseBranch}:refs/remotes/origin/${baseBranch}`, '--depth', String(DEFAULT_CLONE_DEPTH)]);
      const fetchedBaseSha = git(['rev-parse', `origin/${baseBranch}`]).trim();
      if (fetchedBaseSha) reviewedBaseSha = fetchedBaseSha;
    } catch (e) {
      sendLog(ws, `WARNING: Failed to fetch origin/${baseBranch} for diff comparison: ${String(e)}\n`);
    }
  }
  // Pin the head SHA this review runs against so the backend can detect stale verdicts.
  let reviewedHeadSha = '';
  try {
    reviewedHeadSha = git(['rev-parse', 'HEAD']).trim();
  } catch { /* best-effort */ }
  writePlaybookHelper(taskId);
  writeMessagesHelper(taskId);
  const disallowedTools = configureAgentPermissions('review');
  const reviewBuildConfig = resolveBuildConfig(task);

  // Pre-install node_modules in every node-typed directory the reviewer might
  // build. Without this, every cold-clone reviewer hits "cannot resolve <pkg>"
  // when running `npm run build`, mistakes the empty workspace for a real
  // build defect, and flags it as a blocker — burning review-fix iterations on
  // a phantom env failure.
  // A delta reviewer is not being asked to run the broad review's build/lint
  // sweep; avoid paying the cold npm-install cost before it has inspected the
  // narrow fix range. It may still use the repository and run a targeted check
  // if that is necessary to substantiate a specific concern.
  if (!deltaBaseSha) {
    preinstallNodeDeps(WORKSPACE, reviewBuildConfig, ws, taskId);
  }

  // Fetch PR metadata (title + body) from GitHub API — best-effort
  let prTitle = '';
  let prBody = '';
  try {
    const prMetaController = new AbortController();
    const prMetaTimeout = setTimeout(() => { prMetaController.abort(); }, 25_000);
    try {
      const prRes = await fetch(`https://api.github.com/repos/${owner}/${repo}/pulls/${prNumber}`, {
        headers: { Authorization: `token ${ghToken}`, Accept: 'application/vnd.github+json' },
        signal: prMetaController.signal,
      });
      if (prRes.ok) {
        const prData = await prRes.json() as { title?: string; body?: string; base?: { sha?: string } };
        prTitle = prData.title || '';
        prBody = prData.body || '';
        // In delta mode the base ref is intentionally not fetched. PR metadata
        // is a second, live provenance source; do not overwrite an explicit
        // task review target, which is the dispatch-time target identity.
        if (!reviewedBaseSha && /^[0-9a-f]{7,64}$/i.test(prData.base?.sha || '')) {
          reviewedBaseSha = String(prData.base?.sha);
        }
      }
    } finally {
      clearTimeout(prMetaTimeout);
    }
  } catch { /* best-effort, continue without PR metadata */ }

  // Download viewport screenshots captured by the implementer for visual review.
  // The payload carries `viewport_shot_entries` (list of {name, storage_uri} dicts)
  // and `viewport_source_task_id` (the create/fix task that uploaded them).
  // The agent JWT authenticates cross-task access within the same project.
  const viewportShotEntries = (payload.viewport_shot_entries as Array<{ name: string }> | undefined) ?? [];
  const viewportSourceTaskId = strVal(payload.viewport_source_task_id);
  const reviewProjectMetaEarly = task.project_metadata as Record<string, unknown> | undefined;
  const reviewProjectIdEarly = strVal(reviewProjectMetaEarly?.project_id);
  const reviewOrchestratorUrl = process.env.ORCHESTRATOR_URL || 'http://localhost:8080';
  let downloadedShotPaths: string[] = [];
  if (!deltaBaseSha && viewportShotEntries.length > 0 && viewportSourceTaskId && reviewProjectIdEarly) {
    downloadedShotPaths = downloadViewportShots({
      entries: viewportShotEntries,
      shotDir: `/tmp/viewport_review_${taskId}`,
      orchestratorUrl: reviewOrchestratorUrl,
      projectId: reviewProjectIdEarly,
      sourceTaskId: viewportSourceTaskId,
      agentToken: getAgentToken(),
      onError: (name) => { sendLog(ws, `[VIEWPORT] Failed to download screenshot: ${name}\n`); },
    });
    if (downloadedShotPaths.length > 0) {
      sendLog(ws, `[VIEWPORT] Downloaded ${downloadedShotPaths.length} screenshot(s) for visual review\n`);
      recordViewportScreenshotDownload(downloadedShotPaths.length);
    }
  }

  // Get a summary of the actual review target. A delta review is deliberately
  // the exact two-SHA range; the established full review retains its
  // merge-base PR comparison.
  const reviewDiffRange = deltaBaseSha ? `${deltaBaseSha}..HEAD` : `origin/${baseBranch}...HEAD`;
  let changedFiles = '';
  try {
    changedFiles = git(['diff', '--name-status', reviewDiffRange]);
  } catch { changedFiles = '(could not generate file list)'; }

  // Shortstat gives the reviewer objective line-change volume alongside the
  // file list. A "3 files changed" PR that touches 300 lines reads very
  // differently from one that touches 10 — surfacing both prevents scope-label
  // mismatches and gives rollup reviewers proof the PR has real changes.
  let diffShortstat = '';
  try {
    diffShortstat = git(['diff', '--shortstat', reviewDiffRange]).trim();
  } catch { /* shortstat is best-effort context */ }

  // Scope-drift handling: the `taskScope` label is set at task creation and never
  // re-validated against the actual diff. If fix iterations ballooned this PR
  // beyond its original scope, REPLACE the scope-narrowed preamble entirely —
  // augmenting it with a warning leaves contradictory text in place ("review
  // everything" alongside "deprioritize security"), and reviewers tend to
  // follow the more specific (wrong) instruction.
  const changedFilePathList = changedFiles
    .split('\n')
    .map(l => l.trim())
    .filter(l => l && !l.startsWith('('))
    .map(l => l.split(/\s+/).pop() || '')
    .filter(Boolean);
  const changedFileCount = changedFilePathList.length;
  const scopeThreshold = SCOPE_OVERFLOW_THRESHOLDS[taskScope];
  const isScopeOverflow = !!(scopeThreshold && changedFileCount > scopeThreshold);
  const scopePreamble = isScopeOverflow
    ? buildReviewScopeOverflowPreamble(taskScope, changedFileCount, scopeThreshold)
    : taskScope === 'small'
      ? REVIEW_SMALL_SCOPE_PREAMBLE
      : taskScope === 'large'
        ? REVIEW_LARGE_SCOPE_PREAMBLE
        : taskScope === 'medium'
          ? detectMediumScopeSubsystemHint(changedFiles, reviewBuildConfig)
          : '';

  // Read CLAUDE.md for codebase instructions
  const reviewClaudeMd = readClaudeMd();
  const reviewClaudeMdPrefix = (reviewClaudeMd && !providerAutoInjectsClaudeMd())
    ? `${REVIEW_CLAUDEMD_PROMPT_HEADER}${reviewClaudeMd}${REVIEW_CLAUDEMD_PROMPT_FOOTER}`
    : '';

  // Read project_context for active/sibling task awareness
  const reviewProjectCtx = task.project_context as Record<string, unknown> | undefined;
  const reviewDirectionId = (taskContext?.direction_id as string | undefined) || '';
  const reviewRelatedFiles =
    (payload.related_files as string[] | undefined) ||
    (taskContext?.related_files as string[] | undefined) ||
    [];
  const reviewProjectCtxSection = reviewProjectCtx ? formatProjectContext(reviewProjectCtx, 'review', taskScope, reviewDirectionId, reviewRelatedFiles.length) + '\n\n' : '';

  // Keep only the compact trajectory plus the complete backend-owned blocker
  // ledger. Repeating every historical issue/feedback blob duplicated the same
  // evidence and made each low-tier review progressively more expensive.
  const reviewTrajectorySection = formatTrajectorySection(reviewHistory);
  const authoritativeLedgerSection = formatAuthoritativeLedgerSection(reviewLoop, 'review');

  // Build iteration preamble with graduated budget guidance
  const reviewMaxIterations = computeMaxFixIterations(task);
  const reviewMidBudget = iteration * 2 >= reviewMaxIterations && iteration < reviewMaxIterations;
  const smallScopeEarly = iteration === 1 && taskScope === 'small' && !isScopeOverflow;
  const iterationPreamble = buildReviewIterationPreamble(
    iteration, reviewMaxIterations, reviewMidBudget, smallScopeEarly,
  );
  const deltaReviewSection = deltaBaseSha
    ? `## Exact-SHA Delta Review\n` +
      `This follow-up review is scoped to the fix delta from the previously reviewed head to the current head: ` +
      `\`git diff ${deltaBaseSha}..HEAD\`. Treat that exact range as the primary evidence. ` +
      `Verify whether the prior findings were resolved and whether this delta introduced a new blocker. ` +
      `Do not start a full base-branch/PR rescan or re-litigate unrelated pre-existing code. ` +
      `You retain full repository access: inspect surrounding code when it is needed to understand or verify this delta.\n\n`
    : '';
  const reviewIterationContext = deltaBaseSha
    ? `This is a focused follow-up after review iteration ${iteration}.\n\n`
    : `${iterationPreamble}\n`;

  // Rollup PRs (head branch research/* or plan/*) contain N subtask squash-merge
  // commits whose titles end in "(#NNNN)". The `(#NNNN)` marker means a PR was
  // squash-merged — NOT that it landed on origin/main. Subtask PRs merge INTO
  // the rollup branch. We've seen reviewers misread these as "already-on-main"
  // and flag perfectly valid rollup PRs as empty, looping the fix agent on
  // nothing. Make the verification steps explicit.
  //
  // Gate to iteration 1 only: by round 2 the PR is already established as a
  // real, non-empty rollup (iteration 1 reviewed it and requested changes, so
  // it cannot have been "empty" or "already merged"). Re-running the
  // merge-base / diff --stat non-emptiness verification every round is pure
  // overhead that pulls the reviewer into "verification-only" drift instead of
  // converging on the remaining code issues.
  const rollupGuidance = !deltaBaseSha && iteration === 1
    ? buildRollupReviewGuidance(branch, baseBranch, reviewBuildConfig?.staging_branch_prefixes)
    : '';

  // Surface which files the create task's self-review verified vs. left unread
  // (truncation blind spots). Only present when _self_review_outcome was
  // propagated by the backend from the create task's context.
  const selfReviewOutcome = taskContext?._self_review_outcome as Record<string, unknown> | undefined;
  const selfReviewBlindSpotsSection = formatSelfReviewBlindSpots(selfReviewOutcome, changedFilePathList);

  // Propagated from the original create task's plan: files the task was scoped
  // to modify. Changed files not in this list may indicate scope creep.
  const plannedFiles = (taskContext?._planned_files as string[] | undefined) || [];
  let plannedFilesSection = '';
  if (plannedFiles.length > 0) {
    const isTestPath = (p: string) => /(^|\/)testing\//.test(p) || /\.test\.[tj]sx?$/.test(p) || /(^|\/)tests?\//.test(p) || /_test\.py$/.test(p);
    const plannedSet = new Set(plannedFiles);
    const unplannedNonTest = changedFilePathList.filter(f => !plannedSet.has(f) && !isTestPath(f));
    plannedFilesSection = `Planned files from the original analysis plan:\n${plannedFiles.map(f => `  - ${f}`).join('\n')}\n` +
      (unplannedNonTest.length > 0
        ? `\nFiles changed but NOT in the original plan:\n${unplannedNonTest.map(f => `  - ${f}`).join('\n')}\n\n` +
          `${buildPlannedFilesClassificationGuidance()}\n\n`
        : `\nAll non-test changed files are in the original plan.\n\n`);
  }

  // Scope-budget awareness for reviewers: when the PR is already ≥50% of its
  // file budget, warn the reviewer so feedback that would require touching
  // additional files is weighed against pushing the PR over scope.
  const reviewScopeUtilization = taskContext?.scope_budget_utilization as
    { scope?: string; file_count?: number; non_test_file_count?: number; budget?: number; percentage?: number } | undefined;
  let reviewScopeBudgetSection = '';
  if (reviewScopeUtilization && typeof reviewScopeUtilization.percentage === 'number' && reviewScopeUtilization.percentage >= 50) {
    const rsNonTest = reviewScopeUtilization.non_test_file_count ?? 0;
    const rsBudget = reviewScopeUtilization.budget ?? 0;
    const rsPct = reviewScopeUtilization.percentage;
    reviewScopeBudgetSection = buildReviewScopeBudgetSection(rsNonTest, rsBudget, rsPct);
  }

  // Verify-mode block: when the prior fix iteration submitted structured
  // rationale for issues it left unaddressed, surface those to this review
  // pass as CLAIMS TO VERIFY (never as conclusions to accept).  Sycophancy
  // is blocked at the verification gate — non-mechanical claims get flagged
  // 'unverifiable' rather than silently approved.
  const fixAgentResponses = (reviewLoop?.fix_agent_responses as Array<Record<string, unknown>> | undefined) || [];
  let fixResponsesSection = '';
  if (fixAgentResponses.length > 0) {
    const responseLines = fixAgentResponses.slice(0, 10).map((r, idx) => {
      const summary = typeof r.issue_summary === 'string' ? r.issue_summary : '(no summary)';
      const sourceIter = typeof r.source_iteration === 'number' ? r.source_iteration : '?';
      const responseType = typeof r.response_type === 'string' ? r.response_type : 'unknown';
      const claim = typeof r.claim === 'string' ? r.claim : '';
      const verify = Array.isArray(r.verify) ? r.verify.filter((v: unknown): v is string => typeof v === 'string') : [];
      const verifyLines = verify.length > 0
        ? verify.map(v => `      - ${v}`).join('\n')
        : '      (none provided — auto-reject)';
      return (
        `  ${idx + 1}. [${responseType}] iter ${sourceIter}: "${summary}"\n` +
        `     claim: ${claim}\n` +
        `     verify steps:\n${verifyLines}`
      );
    });
    fixResponsesSection =
      REVIEW_FIX_RESPONSES_HEADER +
      buildReviewFixResponsesIntro(fixAgentResponses.length) +
      REVIEW_FIX_RESPONSES_INSTRUCTIONS +
      `Responses to verify:\n${responseLines.join('\n\n')}\n\n`;
  }

  // Build review prompt — Claude has full repo access, not just a diff blob
  const reviewPlaybookSection = formatPlaybook(task.playbook as Record<string, PlaybookEntry | string> | undefined);
  const taskTypeFocusHint = origTaskType === 'qa' ? REVIEW_QA_FOCUS_HINT
    : origTaskType === 'migrate' ? REVIEW_MIGRATE_FOCUS_HINT
    : '';

  // Surface the project vision and strategic intent so reviewers judge changes
  // against the direction's goals, not just local correctness.
  const reviewProjectVision = formatProjectVision(
    task.vision as Record<string, unknown> | undefined,
  );
  const reviewStrategicIntent = formatStrategicIntent(
    task.strategic_intent as Record<string, unknown> | undefined,
  );
  const reviewRepoOverview = formatRepoOverview(task.repo_overview);
  const reviewDomainOverview = formatDomainOverview(task.domain_overview);
  const reviewStrategicFraming = [reviewProjectVision, reviewStrategicIntent, reviewRepoOverview, reviewDomainOverview]
    .filter(Boolean)
    .join('\n');

  // Build a section pointing the reviewer at any downloaded viewport screenshots.
  // Group by component/width prefix (strip trailing -before/-after/-diff suffix)
  // so the reviewer sees paired sets rather than a flat list.
  const viewportScreenshotSection = buildViewportScreenshotSection(downloadedShotPaths);
  const reviewDiffAccessHint = deltaBaseSha
    ? `You have full access to the codebase. Start with \`git diff ${deltaBaseSha}..HEAD\`; read surrounding files only as needed to understand that delta.\n`
    : buildReviewDiffAccessHint(baseBranch);
  // The standard checklist also emits base-branch-scoped lint suggestions when
  // given a build config.  Do not inject those into an exact delta review: they
  // would undermine the narrow review range. The generic checklist and verdict
  // contract remain identical.
  const reviewCoreGuidance = buildReviewCoreGuidance(
    deltaBaseSha ? undefined : reviewBuildConfig,
    taskId,
    deltaBaseSha ? undefined : baseBranch,
  );
  const reviewScopePreamble = deltaBaseSha ? '' : scopePreamble;
  const changedFilesLabel = deltaBaseSha
    ? `Files changed in the exact review delta (${deltaBaseSha}..HEAD):`
    : 'Files changed in this PR:';

  // Exact-SHA follow-ups need decision-relevant context, not the entire first
  // review briefing. Keep repository instructions, findings/responses and PR
  // summary, but leave out broad strategic/project framing, playbook, planned
  // file analysis, self-review blind spots, screenshots and scope metadata.
  const reviewStrategicFramingForPrompt = deltaBaseSha ? '' : reviewStrategicFraming;
  const reviewPlaybookForPrompt = deltaBaseSha ? '' : reviewPlaybookSection;
  const reviewProjectCtxForPrompt = deltaBaseSha ? '' : reviewProjectCtxSection;
  const selfReviewBlindSpotsForPrompt = deltaBaseSha ? '' : selfReviewBlindSpotsSection;
  const plannedFilesForPrompt = deltaBaseSha ? '' : plannedFilesSection;
  const viewportScreenshotsForPrompt = deltaBaseSha ? '' : viewportScreenshotSection;
  const reviewScopeBudgetForPrompt = deltaBaseSha ? '' : reviewScopeBudgetSection;

  const reviewPrompt = `${reviewStrategicFramingForPrompt}${reviewStrategicFramingForPrompt ? '\n\n' : ''}${reviewClaudeMdPrefix}${reviewPlaybookForPrompt}${reviewProjectCtxForPrompt}${fixResponsesSection}${buildReviewMasterPreamble(prNumber, branch, baseBranch)}

${reviewIterationContext}${deltaReviewSection}
${reviewTrajectorySection}${authoritativeLedgerSection}
${rollupGuidance}
${originalPrompt ? `The original task was: ${originalPrompt}\n\n` : ''}${prTitle ? `PR title: ${prTitle}\n` : ''}${prBody ? `PR description:\n${prBody}\n\n` : ''}${changedFilesLabel}
${changedFiles}${diffShortstat ? `\nDiff volume: ${diffShortstat}\n` : ''}

${selfReviewBlindSpotsForPrompt}
${plannedFilesForPrompt}
${viewportScreenshotsForPrompt}
${reviewDiffAccessHint}
${reviewScopeBudgetForPrompt}${reviewScopePreamble}${taskTypeFocusHint}${reviewCoreGuidance}`;

  // Parallel-review fan-out gating: per-project flag AND helper-threshold both
  // required. False positives (extra fan-out) are cheap subagent overhead;
  // false negatives are wall-clock that subagent investigation would have
  // saved. Inject the guidance just before DEFAULT_TOOL_GUIDANCE so prompt
  // ordering stays deterministic for the fingerprint.
  const reviewProjectMeta = task.project_metadata as Record<string, unknown> | undefined;
  const parallelReviewFlag = reviewProjectMeta?.enable_parallel_review === true;
  // Follow-up delta review is intentionally a single focused judgment over the
  // exact repair range. Fan-out belongs to full PR discovery, not this narrow
  // verification pass.
  const parallelReviewInjected = !deltaBaseSha
    && parallelReviewFlag
    && shouldFanOutReview(changedFilePathList, reviewBuildConfig);
  const parallelReviewBlock = parallelReviewInjected ? `\n\n${PARALLEL_REVIEW_GUIDANCE}` : '';
  // Read-only ./fetch (direction, section, playbook) so the reviewer can resolve
  // by-reference ids and trimmed launch snapshots mid-run. Gate provisioning and
  // guidance on project_id so we never advertise a helper we did not write.
  const reviewProjectId = (reviewProjectMeta?.project_id as string | undefined) ?? '';
  if (reviewProjectId) writeFetchHelper(taskId, reviewProjectId, WORKSPACE, { verbs: PLAN_FETCH_VERBS });
  const reviewFetchGuidance = reviewProjectId ? buildFetchGuidance(PLAN_FETCH_VERBS) : '';
  const launchedReviewPrompt = reviewPrompt + parallelReviewBlock + `\n\n${reviewFetchGuidance}${DEFAULT_TOOL_GUIDANCE}`;
  const reviewTelemetry = buildPromptSizeTelemetry(launchedReviewPrompt, [
    ['strategic_framing', reviewStrategicFramingForPrompt],
    ['claude_md', reviewClaudeMdPrefix],
    ['playbook', reviewPlaybookForPrompt],
    ['project_context', reviewProjectCtxForPrompt],
    ['review_trajectory', reviewTrajectorySection],
    ['authoritative_ledger', authoritativeLedgerSection],
    ['fix_responses', fixResponsesSection],
    ['review_preamble', buildReviewMasterPreamble(prNumber, branch, baseBranch)],
    ['pr_context', `${reviewIterationContext}${deltaReviewSection}${rollupGuidance}${originalPrompt ? `The original task was: ${originalPrompt}\n\n` : ''}${prTitle ? `PR title: ${prTitle}\n` : ''}${prBody ? `PR description:\n${prBody}\n\n` : ''}${changedFilesLabel}\n${changedFiles}${diffShortstat ? `\nDiff volume: ${diffShortstat}\n` : ''}`],
    ['review_meta', `${selfReviewBlindSpotsForPrompt}${plannedFilesForPrompt}${viewportScreenshotsForPrompt}${reviewDiffAccessHint}`],
    ['review_guidance', `${reviewScopeBudgetForPrompt}${reviewScopePreamble}${taskTypeFocusHint}${reviewCoreGuidance}`],
    ['parallel_review', parallelReviewBlock],
    ['fetch_and_tools', `\n\n${reviewFetchGuidance}${DEFAULT_TOOL_GUIDANCE}`],
  ]);
  setPromptSizeTelemetry(reviewTelemetry.prompt_size_bytes, reviewTelemetry.prompt_section_count, reviewTelemetry.prompt_section_sizes);
  const promptFingerprint = computeReviewPromptFingerprint(reviewBuildConfig, {
    branch,
    baseBranch,
    includePlannedFilesClassificationGuidance: plannedFiles.length > 0 && plannedFilesSection.includes('Files changed but NOT in the original plan:'),
    includeParallelReviewGuidance: parallelReviewInjected,
  });
  statusRef.value = 'Running code review...';
  setCurrentPromptFingerprint(promptFingerprint);
  // Emit once before the review session starts so short review tasks still
  // persist a stable prompt fingerprint before the first periodic heartbeat.
  sendHeartbeatSync(taskId, statusRef.value, { prompt_fingerprint: promptFingerprint });
  const { exitCode, allOutput, lastResultText } = await runClaudeCode(launchedReviewPrompt, ws, statusRef, model, effort, disallowedTools);

  // Prefer reading verdict from /tmp/ file (avoids output truncation), fall back to output parsing

  let logs = '';
  let verdictJsonField: string | undefined;
  // Reviewer's working set: every file path the agent Read/Edit/Grep/Glob'd
  // during this run, derived from the in-process tool trace. Forwarded to the
  // downstream fix agent so it can start from the reviewer's locations
  // instead of redoing the investigation from scratch.
  const investigatedFiles = extractInvestigatedFiles(snapshotToolTrace());
  const reviewOutput = sanitize(lastResultText || allOutput.slice(-50_000));
  function augmentVerdict(parsed: Record<string, unknown>): string {
    const augmented = { ...parsed };
    if (investigatedFiles.length > 0 && !augmented.investigated_files) {
      augmented.investigated_files = investigatedFiles.slice(0, 25);
    }
    if (parallelReviewInjected) {
      augmented.review_was_parallelized = true;
    }
    // SHA provenance is mechanical. Never let model-authored JSON replace the
    // exact local head and mechanically resolved base the agent actually used.
    if (reviewedHeadSha) {
      augmented.reviewed_head_sha = reviewedHeadSha;
    }
    if (reviewedBaseSha) {
      augmented.reviewed_base_sha = reviewedBaseSha;
    }
    return JSON.stringify(augmented);
  }
  function readReviewVerdictFromTempFile(): Record<string, unknown> | null {
    const verdictPath = `/tmp/review_verdict_${taskId}.json`;
    if (!fs.existsSync(verdictPath)) return null;
    try {
      const verdictJson = fs.readFileSync(verdictPath, 'utf-8').trim();
      const parsed = JSON.parse(verdictJson) as Record<string, unknown>;
      return parsed.verdict ? parsed : null;
    } catch {
      return null;
    }
  }
  const parsedVerdict = readReviewVerdictFromTempFile();
  if (parsedVerdict) {
    // Inject investigated_files alongside the agent-emitted verdict fields,
    // then re-serialize. Capped to keep verdict_json under
    // schemas.StatusUpdate.verdict_json max_length=10000.
    const augmented = augmentVerdict(parsedVerdict);
    verdictJsonField = augmented;
    // Also append to logs for backwards compatibility
    logs = reviewOutput + '\n\n' + augmented;
    sendLog(ws, `Verdict read from file: ${typeof parsedVerdict.verdict === 'string' ? parsedVerdict.verdict : JSON.stringify(parsedVerdict.verdict)}\n`);
  }

  if (!logs) {
    logs = reviewOutput;
  }

  if (!verdictJsonField && exitCode === 0) {
    const repair = await repairStructuredOutput<Record<string, unknown>>({
      mode: 'object',
      requiredKeys: ['verdict'],
      first: { exitCode, allOutput, lastResultText },
      reopenPrompt: buildReviewVerdictInstructions(taskId, { repairContext: true }),
      ws,
      statusRef,
      model,
      effort,
      disallowedTools,
      retryStatus: 'Retrying review verdict extraction...',
      schemaLabel: 'review_verdict',
    });
    if (repair.value !== null) {
      verdictJsonField = augmentVerdict(repair.value);
      logs = reviewOutput + '\n\n' + verdictJsonField;
      sendLog(ws, `Verdict recovered via repair: ${typeof repair.value.verdict === 'string' ? repair.value.verdict : JSON.stringify(repair.value.verdict)}\n`);
    }
  }

  // Surface the PR's current changed-file list to the orchestrator so the
  // next fix iteration's drift detector has a fresh snapshot. Without this,
  // drift falls back to the original create task's _pr_changed_files (which
  // is stale once a rebase-fix or new commits land), leaving drift_in/drift_out
  // empty for exactly the cases we need them to fire (rebase-induced drift).
  // Cap at 50 to match AgentStatusPayload.changed_files limit.
  const reviewChangedFiles = changedFilePathList.slice(0, 50);

  await persistAndPostStatus(taskId, {
    task_id: taskId,
    status: exitCode === 0 ? 'COMPLETED' : 'FAILED',
    ...(exitCode !== 0 ? { artifact_type: 'ERROR_LOG' } : {}),
    logs,
    ...(verdictJsonField ? { verdict_json: verdictJsonField } : {}),
    ...(exitCode !== 0 ? { failure_category: 'claude_code_failed' } : {}),
    ...(reviewChangedFiles.length > 0 ? { changed_files: reviewChangedFiles } : {}),
  });
}

// ── Handler: Fix (address review feedback on existing PR branch) ─────────────
export async function handleFix(
  taskId: string, payload: Record<string, unknown>, task: Record<string, unknown>,
  ghToken: string, owner: string, repo: string, ws: WebSocket | null, statusRef: { value: string },
  model?: string, effort?: string
) {
  const branch = validateBranchName(strVal(payload.branch), 'branch');
  const baseBranch = validateBranchName(resolveBaseBranch(payload, task), 'base_branch');
  const prompt = strVal(payload.prompt);
  const prNumber = Number(payload.pr_number);
  if (!Number.isInteger(prNumber) || prNumber <= 0) throw new Error(`Invalid pr_number: ${String(payload.pr_number)}`);
  const originalPrompt = strVal(payload.original_prompt);
  const fixOrigTaskType = strVal(payload.original_task_type || '');
  const promotionTarget = strVal(payload.promotion_target) || strVal((task.project_metadata as Record<string, unknown> | undefined)?.promotion_target) || undefined;

  if (!prompt) throw new Error('Fix task missing prompt');

  // Clone the existing PR branch (don't create a new one)
  statusRef.value = `Cloning ${owner}/${repo} (fixing PR #${prNumber})...`;
  sendLog(ws, `Fixing PR #${prNumber} on branch ${branch}\n`);
  const cloneUrl = `https://x-access-token:${ghToken}@github.com/${owner}/${repo}.git`;
  // Shallow clone of the PR branch directly — avoids fetch+checkout issues with --single-branch
  await prepareWorkspaceForRole({
    workspace: WORKSPACE, repoUrl: cloneUrl, githubToken: ghToken,
    branch, baseBranch, depth: DEFAULT_CLONE_DEPTH,
    // Review-fix lane pilot: the FIX side is the lane-ized side. When the
    // backend dispatched this fix warm onto a lane-reserved member it set the
    // lane env (BLUEPRINT_LANE_MODE / _CHECKPOINT_SHA); allowLaneResume stays
    // true (default) so prepareWorkspaceForRole fetch+resets to the checkpoint
    // and skips the cold clone/install. A classic fix never carries the env.
    allowLaneResume: true,
  });
  writePlaybookHelper(taskId);
  writeMessagesHelper(taskId);
  const disallowedToolsFix = configureAgentPermissions('fix');
  const fixBuildConfig = resolveBuildConfig(task);
  const fixTaskContext = (task.context as Record<string, unknown> | undefined) || {};
  const fixReviewLoop = fixTaskContext.review_loop as Record<string, unknown> | undefined;
  const fixReviewHistory = (fixReviewLoop?.review_history as ReviewHistoryEntry[] | undefined) || [];
  // The backend ledger is binding whenever present, including an explicitly
  // empty open set. Review history is retained only as a legacy fallback for
  // tasks created before authoritative ledgers were persisted.
  const fixIssues = authoritativeOpenLedgerIssues(fixReviewLoop)
    ?? fixReviewHistory[fixReviewHistory.length - 1]?.issues
    ?? [];
  const latestReviewIssueFiles = fixIssues
    .map(issue => issue.file)
    .filter((file): file is string => typeof file === 'string' && file.length > 0);
  const existingFixRelatedFiles = (
    (fixTaskContext.related_files as string[] | undefined) ||
    (payload.related_files as string[] | undefined) ||
    []
  );
  const fixRelatedFiles = Array.from(new Set([
    ...existingFixRelatedFiles,
    ...latestReviewIssueFiles,
  ]));
  const fixRelatedTestContext = discoverRelatedTests(ws, fixRelatedFiles, fixBuildConfig);
  const fixRelatedTestFiles = fixRelatedTestContext
    .split('\n')
    .map(line => line.match(/^- ([^:]+)(?::|$)/)?.[1]?.trim())
    .filter((file): file is string => Boolean(file));

  git(['config', 'user.email', fixBuildConfig?.git_user_email || 'blueprint-agent@automated.dev']);
  git(['config', 'user.name', fixBuildConfig?.git_user_name || 'Blueprint Agent']);

  // Download viewport screenshots captured by the implementer for visual context.
  // The payload carries `viewport_shot_entries` (list of {name, storage_uri} dicts)
  // and `viewport_source_task_id` (the create/fix task that uploaded them).
  // The agent JWT authenticates cross-task access within the same project.
  const viewportShotEntries = (payload.viewport_shot_entries as Array<{ name: string }> | undefined) ?? [];
  const viewportSourceTaskId = strVal(payload.viewport_source_task_id);
  const fixViewportProjectMeta = task.project_metadata as Record<string, unknown> | undefined;
  const fixViewportProjectId = strVal(fixViewportProjectMeta?.project_id);
  const fixViewportOrchestratorUrl = process.env.ORCHESTRATOR_URL || 'http://localhost:8080';
  let downloadedFixShotPaths: string[] = [];
  if (viewportShotEntries.length > 0 && viewportSourceTaskId && fixViewportProjectId) {
    downloadedFixShotPaths = downloadViewportShots({
      entries: viewportShotEntries,
      shotDir: `/tmp/viewport_fix_${taskId}`,
      orchestratorUrl: fixViewportOrchestratorUrl,
      projectId: fixViewportProjectId,
      sourceTaskId: viewportSourceTaskId,
      agentToken: getAgentToken(),
      onError: (name) => { sendLog(ws, `[VIEWPORT] Failed to download screenshot: ${name}\n`); },
    });
    if (downloadedFixShotPaths.length > 0) {
      sendLog(ws, `[VIEWPORT] Downloaded ${downloadedFixShotPaths.length} screenshot(s) for visual context\n`);
      recordViewportScreenshotDownload(downloadedFixShotPaths.length);
    }
  }
  // Prompt section that points the fix agent at the downloaded PNGs so it can
  // visually inspect the rendered components (empty when none were downloaded).
  const fixViewportScreenshotSection = buildViewportScreenshotSection(downloadedFixShotPaths);

  // Always rebase onto target branch to resolve merge conflicts
  statusRef.value = `Rebasing onto ${baseBranch}...`;
  sendLog(ws, `Rebasing ${branch} onto origin/${baseBranch}...\n`);
  let hasConflicts = false;
  // True when the rebase onto base hit conflicts (the agent integrated a
  // moving base and a resolution was required). Threaded into ./check as
  // mergeVerify so the gate runs the affected backend test files under Postgres
  // before ./pr open can push — catching a silent drop / vintage skew the
  // standalone gate would miss.
  let conflictResolutionOccurred = false;
  try {
    git(['fetch', 'origin', `+refs/heads/${baseBranch}:refs/remotes/origin/${baseBranch}`]);
    git(['rebase', `origin/${baseBranch}`]);
    sendLog(ws, `Rebase successful\n`);
  } catch {
    conflictResolutionOccurred = true;
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
  const reviewLoop = fixReviewLoop;
  const fixReason = reviewLoop?.fix_reason as string | undefined;
  const taskContext = fixTaskContext;
  const ciFailureLogs = [
    strVal(taskContext.ci_failure_logs),
    strVal(taskContext._ci_failure_logs),
    strVal(taskContext._test_failure_summary),
    fixReason === 'ci_failure' ? prompt : '',
  ].map(s => s.trim()).filter(Boolean).join('\n\n');
  const ciFailureSelectors = fixReason === 'ci_failure'
    ? extractPytestSelectorsFromOutput(ciFailureLogs)
    : [];
  const reviewHistory = fixReviewHistory;
  const iteration = (reviewLoop?.iteration as number | undefined) || 1;
  const maxIterations = computeMaxFixIterations(task);
  let iterationHeader = `You are on fix iteration ${iteration} of ${maxIterations}.\n`;
  if (iteration >= maxIterations) {
    iterationHeader += 'This is your FINAL attempt. If this fix is not accepted, the task will require manual intervention. Address every feedback point thoroughly.\n';
  } else if (iteration * 2 >= maxIterations) {
    const fixCtx = (task.context as Record<string, unknown> | undefined) || {};
    const fixScope = (fixCtx.scope as string | undefined) || 'medium';
    const includeOriginalScope = fixScope !== 'small';
    iterationHeader += includeOriginalScope
      ? 'Convergence mode: you are at or past 50% of the iteration budget. Make only the minimal changes needed to address the specific issues listed below. You may modify files from the original task scope when fixing a flagged issue requires updating a related definition or consumer, but do NOT introduce new files outside the original scope, do NOT refactor or clean up adjacent code, and defer any non-blocking improvements to follow-up tasks. Scope creep at this stage commonly exhausts the remaining budget and forces the PR to close unmerged.\n'
      : 'Convergence mode: you are at or past 50% of the iteration budget. Make only the minimal changes needed to address the specific issues listed below. Do NOT touch files that are not mentioned in the review feedback, do NOT refactor or clean up adjacent code, and defer any non-blocking improvements to follow-up tasks. Scope creep at this stage commonly exhausts the remaining budget and forces the PR to close unmerged.\n';
    const latestIssues = fixIssues;
    const issueFiles = latestIssues.map(i => i.file).filter((f): f is string => typeof f === 'string' && f.length > 0);
    const originalScopeFiles = includeOriginalScope
      ? [
          ...((fixCtx._planned_files as string[] | undefined) || []),
          ...((fixCtx.related_files as string[] | undefined) || []),
          ...((payload.related_files as string[] | undefined) || []),
        ].filter((f): f is string => typeof f === 'string' && f.length > 0)
      : [];
    // Related tests are part of the repair surface, not scope drift. They are
    // discovered from cited source files using the repository's existing test
    // conventions, so a requested regression is not blocked by convergence.
    const allowedFiles = Array.from(new Set([
      ...issueFiles,
      ...originalScopeFiles,
      ...fixRelatedTestFiles,
    ]));
    if (allowedFiles.length > 0) {
      iterationHeader += `Files you may modify: ${allowedFiles.join(', ')}\n`;
    }
    const criticalCount = latestIssues.filter(i => i.severity === 'critical').length;
    const majorCount = latestIssues.filter(i => i.severity === 'major').length;
    if (criticalCount + majorCount > 0) {
      iterationHeader += `Address exactly ${criticalCount} CRITICAL and ${majorCount} MAJOR issue(s) below. Do NOT introduce new changes beyond what these issues require.\n`;
    }
  }
  const originalPromptSection = originalPrompt ? `## Original Task Goal\n${originalPrompt}\n\n` : '';
  // Compact trajectory + drift context; the binding issue set is rendered once
  // from the authoritative ledger below.
  const trajectorySection = formatTrajectorySection(reviewHistory);
  const driftSignal = (reviewLoop?.drift_signal as DriftSignal | undefined);
  const driftSection = formatDriftSection(driftSignal);
  const authoritativeLedgerSection = formatAuthoritativeLedgerSection(reviewLoop, 'fix');
  let basePrompt = originalPromptSection + trajectorySection + driftSection +
    iterationHeader + authoritativeLedgerSection + `Current review feedback to address:\n${prompt}`;

  // Surface self-review blind-spots (truncated_files_read, incomplete_review,
  // and author-flagged blind_spots) propagated from the
  // original create task. Empty string when no _self_review_outcome was
  // attached (e.g. CI-failure-driven fixes).
  const fixSelfReviewOutcome = taskContext._self_review_outcome as Record<string, unknown> | undefined;
  let fixChangedFilePathList: string[] = [];
  try {
    fixChangedFilePathList = git(['diff', '--name-only', `origin/${baseBranch}...HEAD`])
      .split('\n').map(s => s.trim()).filter(Boolean);
  } catch { /* diff not available — fall through to fallback */ }
  if (fixChangedFilePathList.length === 0) {
    const plannedFallback = (taskContext._planned_files as string[] | undefined) || [];
    fixChangedFilePathList = [...plannedFallback, ...fixRelatedFiles]
      .filter((f): f is string => typeof f === 'string' && f.length > 0);
  }
  const fixSelfReviewSection = formatSelfReviewBlindSpots(fixSelfReviewOutcome, fixChangedFilePathList);
  if (fixSelfReviewSection) {
    basePrompt = `${fixSelfReviewSection}${basePrompt}`;
  }

  // Stale-premise/provenance notice for finding-derived fix tasks. Renders the
  // SAME provenance block as the standard handler — complementary to (not a
  // replacement for) the merge-conflict-vs-CI stale-premise notice computed
  // later; if both apply, both render. Prepended early so the agent
  // reproduces-first before trusting a possibly-stale finding. Empty when no
  // provenance warnings fired.
  const fixProvenanceNotice = formatProvenanceWarnings(taskContext);
  if (fixProvenanceNotice) {
    basePrompt = `${fixProvenanceNotice}${basePrompt}`;
  }

  const fixAcceptanceEvidenceNotice = formatAcceptanceEvidence(payload);
  if (fixAcceptanceEvidenceNotice) {
    basePrompt = `${fixAcceptanceEvidenceNotice}${basePrompt}`;
  }

  // Parallel fix fan-out gating: per-project flag AND helper-threshold both
  // required. Inject PARALLEL_FIX_GUIDANCE near DEFAULT_TOOL_GUIDANCE so the
  // fingerprint is stable and telemetry can A/B by fingerprint.
  const fixProjectMetaEarly = task.project_metadata as Record<string, unknown> | undefined;
  // Read-only ./fetch (direction, section, playbook) so the fix agent can
  // resolve by-reference ids and trimmed launch snapshots mid-run. Gate
  // provisioning and guidance on project_id so we never advertise a helper we
  // did not write.
  const fixProjectId = (fixProjectMetaEarly?.project_id as string | undefined) ?? '';
  if (fixProjectId) writeFetchHelper(taskId, fixProjectId, WORKSPACE, { verbs: PLAN_FETCH_VERBS });
  const fixFetchGuidance = fixProjectId ? buildFetchGuidance(PLAN_FETCH_VERBS) : '';
  const parallelFixFlag = fixProjectMetaEarly?.enable_parallel_fix === true;
  const latestIssuesForFanOut = fixIssues;
  const reviewIssueFilesForFanOut = latestIssuesForFanOut
    .map(i => i.file)
    .filter((f): f is string => typeof f === 'string' && f.length > 0);
  const parallelFixInjected =
    parallelFixFlag &&
    shouldFanOutFix(
      fixChangedFilePathList,
      latestIssuesForFanOut.length,
      reviewIssueFilesForFanOut,
      fixBuildConfig,
    );

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
      const markerCheck = execFileSync('git', ['-C', WORKSPACE, 'grep', '-rl', '<' + '<<<<<<', '--', '*.ts', '*.tsx', '*.py', '*.js', '*.jsx', '*.json'], { encoding: 'utf-8', timeout: CONFLICT_CHECK_TIMEOUT }).trim();
      if (markerCheck) {
        enrichedPrompt = `CONFLICT MARKERS FOUND in the following files:\n${markerCheck}${originalPrompt ? `\n\nThis PR's goal: ${originalPrompt.slice(0, 500)}` : ''}\n\nResolve ALL conflict markers (${'<'.repeat(7)} ${'='.repeat(7)} ${'>'.repeat(7)}) in each file. ${baseBranch} is the source of truth — keep its refactors and restructuring, and re-apply only this PR's specific changes (described above) on top.\n\n${basePrompt}`;
        sendLog(ws, `Files with conflict markers:\n${markerCheck}\n`);
      }
    } catch { /* no markers found, good */ }
  }

  // Migration-aware conflict detection: even when text-level conflict markers
  // are absent, a new migration on base can silently invalidate this PR's
  // migration revision chain. Run unconditionally so silent revision-number
  // collisions are surfaced to Claude alongside any textual conflicts.
  try {
    // Resolve the per-project alembic versions directory (falls back to
    // Blueprint's backend/alembic/versions when config is unset). The layout
    // returns no trailing slash; the conflict-detection helpers expect a
    // trailing-slash prefix, so normalize it here exactly as the migrate
    // handler does for its own path filters.
    const migrationLayout = resolveMigrationLayout(fixBuildConfig);
    const versionsDir = `${migrationLayout.versionsDir}/`;
    const prFiles = git(['diff', '--name-only', `origin/${baseBranch}...HEAD`])
      .split('\n').map(s => s.trim()).filter(Boolean);
    const baseFiles = git(['ls-tree', '-r', '--name-only', `origin/${baseBranch}`, versionsDir])
      .split('\n').map(s => s.trim()).filter(Boolean);
    const migrationConflict = detectMigrationConflict(prFiles, baseFiles, versionsDir);
    const reservedMigration = extractReservedMigrationRevision(task);
    // A reserved migration may have a stale advisory parent even when no
    // filename conflict is visible. Repair it against the current base ref in
    // either case; the PR migration itself must never be mistaken for that head.
    if (migrationConflict.detected || (reservedMigration && migrationConflict.prMigrations.length > 0)) {
      const guidance = buildMigrationConflictGuidance(
        migrationConflict, versionsDir,
        migrationLayout.schemaRevisionFile, migrationLayout.schemaRevisionConst,
        reservedMigration,
      );
      enrichedPrompt = `${guidance}${enrichedPrompt}`;
      sendLog(ws, `Migration revision repair guidance injected: PR=${migrationConflict.prMigrations.join(',')} base-new=${migrationConflict.baseNewMigrations.join(',')} reserved=${reservedMigration?.provisionalRevision ?? 'none'}\n`);
    }
  } catch { /* diff/ls-tree failures are non-fatal */ }

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

  // Surface operator/strategist implementation constraints authored on the
  // direction (e.g. "Must not modify the public API surface"). The fix path
  // builds its prompt manually and bypasses buildContextPrefix, so wire the
  // field in directly here. Bounded backend-side; omit cleanly when absent.
  const fixImplConstraints = strVal(
    (task.direction_context as Record<string, unknown> | undefined)?.implementation_constraints,
  );
  if (fixImplConstraints) {
    enrichedPrompt =
      `## Implementation Constraints\n` +
      `The direction author set these constraints for whoever implements this work. Respect them:\n${fixImplConstraints}\n\n` +
      enrichedPrompt;
  }

  // Prepend parent context if this fix agent is part of a larger initiative
  const fixParentCtx = (task.context as Record<string, unknown> | undefined)?.parent_context as Record<string, unknown> | undefined;
  if (fixParentCtx?.parent_prompt) {
    enrichedPrompt = `## Parent Task Context\nThis task is a subtask of: **${strVal(fixParentCtx.parent_title)}**\nParent goal: ${strVal(fixParentCtx.parent_prompt)}\n\n${enrichedPrompt}`;
  }

  // Scope-pressure hint: warn the agent when the PR is already close to its
  // scope file budget so it prefers minimal fixes over expansive refactoring.
  // The percentage + counts are computed backend-side in review_pipeline.py.
  const scopeUtilization = (task.context as Record<string, unknown> | undefined)?.scope_budget_utilization as
    { scope?: string; file_count?: number; non_test_file_count?: number; budget?: number; percentage?: number } | undefined;
  if (scopeUtilization && typeof scopeUtilization.percentage === 'number' && scopeUtilization.percentage >= 70) {
    const nonTest = scopeUtilization.non_test_file_count ?? 0;
    const budget = scopeUtilization.budget ?? 0;
    const pct = scopeUtilization.percentage;
    const scopeLabel = scopeUtilization.scope ?? 'unknown';
    enrichedPrompt =
      `## Scope-Pressure Warning\n` +
      `This PR already touches ${nonTest} non-test files against a ${scopeLabel}-scope budget of ${budget} (${pct}% utilized). ` +
      `Prefer the minimal fix that addresses the review feedback — avoid expansive refactoring or touching additional files. ` +
      `Defer peripheral cleanup, drive-by improvements, and adjacent refactors to follow-up tasks so this PR stays within scope and merges.\n\n` +
      enrichedPrompt;
  }

  // Prepend related test context so the agent knows which tests to update
  if (fixRelatedTestContext) {
    enrichedPrompt = `${fixRelatedTestContext}${enrichedPrompt}`;
  }

  // Reviewer's working set: paths the previous review agent Read/Edit/Grep'd
  // while forming this verdict. Hand them forward as starting context so the
  // fix agent doesn't re-do the same investigation. Informational only — the
  // agent stays free to look at other files if the issues lead elsewhere.
  const reviewerInvestigatedFiles = (taskContext.investigated_files as string[] | undefined) || [];
  if (reviewerInvestigatedFiles.length > 0) {
    const formatted = reviewerInvestigatedFiles.slice(0, 25).map(f => `- ${f}`).join('\n');
    enrichedPrompt =
      `## Reviewer's Working Set\n` +
      `The reviewer Read/Edit/Grep'd these paths while forming the verdict above. Start your investigation here — they are the most likely place the cited issues live. You may look elsewhere if the trail leads there:\n${formatted}\n\n` +
      enrichedPrompt;
  }

  // Lane-resume block: when the backend dispatched this fix on a persistent
  // improvement lane (lane arm of the review-fix pilot), it attaches accumulated
  // chain knowledge under `lane_resume`. Surface it so the fix agent starts with
  // the chain's accumulated context. Present ONLY on lane-arm fix tasks —
  // reviewers/audits never carry it (stripped backend-side as a transient lane
  // directive), and classic/control fixes never have it set. Empty string when
  // the block is absent or carries no usable signal.
  const laneResumeSection = formatLaneResumeSection(
    taskContext.lane_resume as Record<string, unknown> | undefined,
  );
  if (laneResumeSection) {
    enrichedPrompt = `${laneResumeSection}${enrichedPrompt}`;
  }

  // Prepend the per-type rejection-patterns block (backend-injected at claim
  // time) so fix agents see why fix-type work commonly gets rejected and avoid
  // introducing fresh instances while addressing the review feedback. The fix
  // path builds its prompt manually and bypasses buildContextPrefix, so wire
  // the same formatter in directly — mirroring the Project Vision block below.
  const fixRejectionPatterns = formatRejectionPatternsSection(
    (task.project_context as Record<string, unknown> | undefined)
      ?.rejection_patterns as RejectionPatternsSource | undefined,
    'fix',
  );
  if (fixRejectionPatterns) {
    enrichedPrompt = `${fixRejectionPatterns}${enrichedPrompt}`;
  }

  // Prepend the shared Project Vision block so review-driven fixes inherit the
  // same strategic "why" as standard task agents.
  const fixProjectVision = formatProjectVision(
    task.vision as Record<string, unknown> | undefined,
  );
  if (fixProjectVision) {
    enrichedPrompt = `${fixProjectVision}${enrichedPrompt}`;
  }

  // Prepend operator steering (current strategic intent). The fix path
  // assembles its prompt independently and does NOT call buildContextPrefix, so
  // it must render the same Operator Steering section explicitly — alongside the
  // existing CI/review/error context, never replacing it.
  const fixStrategicIntent = formatStrategicIntent(
    task.strategic_intent as Record<string, unknown> | undefined,
  );
  if (fixStrategicIntent) {
    enrichedPrompt = `${fixStrategicIntent}\n${enrichedPrompt}`;
  }

  // Surface the downloaded viewport screenshots to the fix agent so it can
  // visually inspect the rendered components while addressing the feedback.
  // Mirrors handleReview's viewportScreenshotSection wiring.
  if (fixViewportScreenshotSection) {
    enrichedPrompt = `${enrichedPrompt}\n\n${fixViewportScreenshotSection}`;
  }

  // Prepend repo overview so fix agents know the tech stack and layout without
  // re-discovering them. The fix path bypasses buildContextPrefix, so wire in
  // the same formatter directly — mirroring the strategic intent block above.
  const fixRepoOverview = formatRepoOverview(task.repo_overview);
  if (fixRepoOverview) {
    enrichedPrompt = `${fixRepoOverview}\n${enrichedPrompt}`;
  }

  // Prepend domain overview (product/domain context from bootstrap). Mirrors
  // the repo overview wiring above — fix path bypasses buildContextPrefix.
  const fixDomainOverview = formatDomainOverview(task.domain_overview);
  if (fixDomainOverview) {
    enrichedPrompt = `${fixDomainOverview}\n${enrichedPrompt}`;
  }

  // Prepend CLAUDE.md instructions + playbook
  const fixClaudeMd = readClaudeMd();
  const fixPlaybookSection = formatPlaybook(task.playbook as Record<string, PlaybookEntry | string> | undefined);
  const fixRetryContext = buildRetryContext(task);
  const fixFirstTaskSection = task.first_task_on_repo ? FIRST_TASK_GUIDANCE : '';
  if (fixClaudeMd && !providerAutoInjectsClaudeMd()) {
    enrichedPrompt = `## Codebase Instructions\n${fixClaudeMd}\n\n${fixPlaybookSection}${fixRetryContext}${enrichedPrompt}`;
  } else if (fixPlaybookSection || fixRetryContext) {
    enrichedPrompt = `${fixPlaybookSection}${fixRetryContext}${enrichedPrompt}`;
  }

  // Append fix-specific task type hint and playbook guidance
  const fixTypeHints = getTaskTypeHints(fixBuildConfig);
  if (fixTypeHints['fix']) {
    enrichedPrompt = `${enrichedPrompt}\n\n${fixTypeHints['fix']}`;
  }
  // Fix iterations always include FIX_TASK_FOCUS so self-review covers regression_risk
  // and scope-drift for this iteration's diff. Also preserve the original task-type
  // focus block (e.g. CREATE_TASK_FOCUS) so type-specific checks aren't lost.
  const origTypeFocusBlock = getTaskTypeFocusBlock(fixOrigTaskType);
  // Avoid duplicating FIX_TASK_FOCUS when the original task was also a fix task.
  const fixSrExtra = fixOrigTaskType === 'fix' ? '' : FIX_TASK_FOCUS;
  // Fix tasks never have a test baseline file, so the diff-based heuristic described
  // in CODE_CHANGE_HELPER_GUIDANCE always applies. Surface this explicitly so Claude
  // does not skip regression_risk entirely.
  const fixHeuristicNote = '\n> **Fix-task note:** No test baseline exists for this iteration. For `regression_risk`, always use the diff-based heuristic (source-to-test ratio + removed exports) described in the Regression-risk check instructions above, and append `[heuristic-derived]` to the issue text.\n';
  let fixSrFocusBlock = origTypeFocusBlock + fixSrExtra + fixHeuristicNote;
  if (reviewHistory.length >= 2) {
    const priorNorms = new Set(reviewHistory.slice(0, -1).flatMap(e =>
      (e.issues ?? [])
        .filter(i => ['critical', 'major'].includes(i.severity.toLowerCase()))
        .map(i => i.description.toLowerCase().replace(/\s+/g, ' ').trim().slice(0, 80)),
    ));
    const repeating = (reviewHistory[reviewHistory.length - 1]?.issues ?? [])
      .filter(i => ['critical', 'major'].includes(i.severity.toLowerCase())
        && priorNorms.has(i.description.toLowerCase().replace(/\s+/g, ' ').trim().slice(0, 80)))
      .map(i => i.description).slice(0, 5);
    if (repeating.length > 0) {
      fixSrFocusBlock += `\n4. **Repeating-issues verification** — these CRITICAL/MAJOR issues appeared in multiple prior iterations. Verify each is now resolved and record under \`correctness\` or \`regression_risk\` in your self-review JSON:\n${repeating.map(d => `- ${d}`).join('\n')}\n`;
    }
  }
  const parallelFixBlock = parallelFixInjected ? `\n\n${PARALLEL_FIX_GUIDANCE}` : '';
  enrichedPrompt = `${enrichedPrompt}${fixFirstTaskSection}${buildCodeChangeHelperGuidance({ buildConfig: fixBuildConfig, owner, repo })}${fixSrFocusBlock}${PLAYBOOK_GUIDANCE}\nNote: If the review feedback revealed a recurring pattern or non-obvious codebase rule, save it to the playbook.\n${assembleFailureGuidanceBlocks()}${parallelFixBlock}\n\n${fixFetchGuidance}${DEFAULT_TOOL_GUIDANCE}`;

  // Structured-response invitation: when the agent disagrees with a reviewer
  // issue (e.g. an unplanned file is actually caller-impact, not scope-creep),
  // it can record a verifiable claim instead of either capitulating or being
  // silently re-flagged.  Reviewer's job in the next iteration is to RUN the
  // listed verify steps, not to be persuaded by rhetoric — sycophancy is
  // blocked at the verification gate.  Required fields enforced backend-side
  // (schemas.AgentStatusPayload caps at 5 entries; review_pipeline drops
  // entries lacking mechanical verify steps).
  enrichedPrompt = `${enrichedPrompt}\n\n## Responding to reviewer issues

For each reviewer issue, your output should include either:
  (a) A code change that addresses the issue, OR
  (b) A structured response (see schema below) explaining why the issue is wrong / not-applicable / better-handled-differently.

If you do neither for a CRITICAL/MAJOR issue, the reviewer will re-raise it on the next iteration and the loop continues.

Write structured responses to /tmp/fix_responses_${taskId}.json as a JSON array of these objects:

  {
    "issue_id": "<copy the exact BP-... id shown with the reviewer issue>",
    "issue_summary": "<first 80 chars of the issue text>",
    "source_iteration": <int — which review iteration raised this issue>,
    "response_type": "disputed" | "partial" | "alternative",
    "claim": "<1–3 sentences: what you did, or why the issue is wrong>",
    "verify": [
      "<concrete check the reviewer can run>",
      "<another concrete check>"
    ]
  }

Copy the BP-... issue id exactly. Do not hash, recompute, or invent an id.

Verify steps must be MECHANICAL — runnable commands, greppable assertions, references to specific files/lines.  "I tested it" / "I'm confident" / "trust me" are rejected at parse time.  If you cannot produce mechanical verify steps, you cannot dispute — either fix the issue or accept the flag.

When to use which type:
  - disputed: reviewer is factually wrong.  Verify steps demonstrate the underlying claim is false.
  - partial: you fixed part; the rest is intentionally deferred.  Verify steps show deferral is correct (e.g. "out of cluster scope; sibling task X owns the rest" with verify pointing to the sibling task ID).
  - alternative: you took a different approach than the reviewer suggested.  Verify steps show your approach achieves the same goal.

Maximum 5 responses per iteration.  Skip writing the file entirely if every issue was addressed by a code change.\n`;

  // Capture test baseline for medium/large scope fix tasks before running Claude.
  // This lets testFixLoop distinguish pre-existing failures from newly-broken tests,
  // matching the same logic used in the create handler.
  const fixScope = (task.context as Record<string, unknown> | undefined)?.scope as string | undefined;
  if (fixReason === 'ci_failure') {
    sendLog(ws, '[Baseline] Skipped — this is a CI-failure fix; all existing test failures on the PR are in scope.\n');
  } else if (fixScope === 'medium' || fixScope === 'large') {
    // Baseline is captured for the side-effect of warming up the test runner
    // and surfacing pre-existing failures in the agent log; the captured set
    // is no longer consumed by the wrapper (Claude does its own ./check test).
    await captureTestBaseline(ws, statusRef, fixBuildConfig, fixRelatedFiles);
  }

  if (fixReason === 'ci_failure') {
    if (ciFailureSelectors.length > 0) {
      sendLog(ws, `CI failure selectors extracted: ${ciFailureSelectors.slice(0, 8).join(', ')}${ciFailureSelectors.length > 8 ? ` (+${ciFailureSelectors.length - 8} more)` : ''}\n`);
      const repro = await testFixLoop(
        ws, statusRef, model, effort, fixBuildConfig,
        undefined, fixRelatedFiles, undefined,
        {
          targetPytestSelectors: ciFailureSelectors,
          maxFixAttempts: 0,
          phaseLabel: 'CI failure repro',
        },
      );
      // ./check now VERIFIES these exact selectors locally (sanctioned
      // harness, targeted run) and ./pr open gates on ./check — so the push
      // is blocked until the CI-failing tests pass. Tell the agent so the
      // gate failure is expected, not a surprise.
      const ciGateNote =
        `These selector(s) are now part of your ./check gate: ./check runs them via the sanctioned ` +
        `test harness and ./pr open will NOT push until they pass locally. Iterate edit → ./check until ` +
        `the targeted CI selector(s) are green, then ./pr open.\n`;
      if (repro.failureSummary) {
        enrichedPrompt =
          `## Local targeted CI repro\n` +
          `The CI-failing pytest selector(s) were reproduced locally before edits. Use this focused output as the primary failure evidence:\n\n` +
          `${repro.failureSummary.slice(-5000)}\n\n` +
          ciGateNote + `\n` +
          enrichedPrompt;
      } else if (repro.passed) {
        enrichedPrompt =
          `## Local targeted CI repro\n` +
          `The CI-failing pytest selector(s) passed locally before edits. Re-check whether the CI failure is stale, environment-specific, or covered by another failing check before changing code.\n\n` +
          ciGateNote + `\n` +
          enrichedPrompt;
      } else {
        // Repro neither produced a failure summary nor cleanly passed (e.g.
        // an infra error in the pre-edit repro). The ./check gate still runs
        // the selectors, so the agent must know it's there.
        enrichedPrompt = `## CI failure selectors\n` + ciGateNote + `\n` + enrichedPrompt;
      }
    } else {
      sendLog(ws, 'No pytest selectors could be extracted from CI failure logs — skipping targeted repro and relying on CI context plus impact tests.\n');
    }
  }

  // Fix tasks assemble their prompt independently of the standard handler.
  // Keep the backend-supplied suffix last so manual retry guidance is actually
  // delivered and can explicitly correct a stale original fix premise.
  if (payload.prompt_suffix) {
    enrichedPrompt =
      `${enrichedPrompt}\n\nAdditional instructions:\n${strVal(payload.prompt_suffix)}`;
  }

  // Run Claude with the review feedback as the prompt
  const fixTelemetry = buildPromptSizeTelemetry(enrichedPrompt, [
    ['claude_md', fixClaudeMd],
    ['playbook', fixPlaybookSection],
    ['retry_context', fixRetryContext],
    ['domain_overview', fixDomainOverview],
    ['repo_overview', fixRepoOverview],
    ['strategic_intent', fixStrategicIntent],
    ['project_vision', fixProjectVision],
    ['rejection_patterns', fixRejectionPatterns],
    ['core_prompt', basePrompt],
    ['guidance', `${fixFirstTaskSection}${buildCodeChangeHelperGuidance({ buildConfig: fixBuildConfig, owner, repo })}${fixSrFocusBlock}${PLAYBOOK_GUIDANCE}\nNote: If the review feedback revealed a recurring pattern or non-obvious codebase rule, save it to the playbook.\n${assembleFailureGuidanceBlocks()}${parallelFixBlock}`],
    ['fetch_and_tools', `\n\n${fixFetchGuidance}${DEFAULT_TOOL_GUIDANCE}`],
  ]);
  setPromptSizeTelemetry(fixTelemetry.prompt_size_bytes, fixTelemetry.prompt_section_count, fixTelemetry.prompt_section_sizes);
  const promptFingerprint = computePromptFingerprint('fix', fixBuildConfig, {
    includeParallelFixGuidance: parallelFixInjected,
    promotionTarget,
    codeChangeGuidanceVariant: codeChangeGuidanceVariant({ buildConfig: fixBuildConfig, owner, repo }),
    emittedFailureGuidance: assembleFailureGuidanceBlocks(),
  });
  statusRef.value = 'Addressing review feedback...';
  setCurrentPromptFingerprint(promptFingerprint);
  // Emit once before the fix session starts so fast fix/rebase tasks still
  // persist the stable code-change fingerprint on the backend.
  sendHeartbeatSync(taskId, statusRef.value, { prompt_fingerprint: promptFingerprint });
  // Telemetry: persist parallelization marker so orchestrator can attribute outcomes.
  if (parallelFixInjected) {
    try {
      const parallelMarker = JSON.stringify({ fix_was_parallelized: true, task_id: taskId });
      fs.writeFileSync(`/tmp/fix_parallelized_${taskId}.json`, parallelMarker, 'utf-8');
    } catch { /* non-fatal */ }
  }
  const fixBaseCommit = git(['rev-parse', 'HEAD']);
  // 2026-05-04: post-Claude lint/test/push pipeline replaced with
  // runCodeChangeTask. Claude calls ./check + ./pr open (which pushes
  // updates to the EXISTING PR via existingPrNumber) + ./done from
  // inside its own session. See agent/src/handlers/code-change.ts.
  await runCodeChangeTask({
    taskId,
    prompt: enrichedPrompt,
    taskPrompt: basePrompt,
    ws,
    statusRef,
    workBranch: branch,
    baseBranch,
    owner,
    repo,
    existingPrNumber: prNumber,
    testsOnMainBranch: baseBranch === (promotionTarget ?? resolveBaseBranch({}, task)),
    baseCommit: fixBaseCommit,
    model,
    effort,
    disallowedTools: disallowedToolsFix,
    noChangesMessage: 'No file changes after review-fix iteration.',
    buildConfig: fixBuildConfig,
    taskCapability: task.task_capability as Partial<Record<'agent_mode' | 'run_shape' | 'output_kind', unknown>> | undefined,
    // Close the ci_failure fix loop: thread the exact CI-failing selectors
    // into ./check so ./pr open can't push until they pass locally. Empty for
    // non-ci_failure fixes (ciFailureSelectors is [] unless fixReason ===
    // 'ci_failure'), so review-feedback fixes keep the existing gate untouched.
    ciFailureSelectors,
    // Conflict-resolution closed-loop verify: when this fix integrated a moving
    // base and resolved conflicts (or is an explicit merge_conflict fix), ./check
    // runs the affected backend test files under Postgres so a bad resolution
    // fails the gate before ./pr open can push (see buildMergeVerifyBlock).
    mergeVerify: conflictResolutionOccurred || fixReason === 'merge_conflict',
  });

}
