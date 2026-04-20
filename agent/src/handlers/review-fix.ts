import fs from 'fs';
import { execFileSync } from 'child_process';
import type WebSocket from 'ws';
import {
  WORKSPACE, strVal, sanitize, sendLog, validateBranchName,
  persistAndPostStatus, apiGet, git, gitAsync, gitCommit, cloneWithRetry,
  runClaudeCode, runProjectLintChecks, lintFixLoop, testFixLoop,
  captureTestBaseline, sendHeartbeatSync,
} from '../index.js';
import type { BuildConfig } from '../index.js';
import { DEFAULT_CLONE_DEPTH, SCOPE_OVERFLOW_THRESHOLDS, PUSH_FORCE_WITH_LEASE_MAX_RETRIES, PUSH_FORCE_WITH_LEASE_RETRY_DELAY, CONFLICT_CHECK_TIMEOUT } from '../constants.js';
import {
  getTaskTypeHints, PLAYBOOK_GUIDANCE, FIRST_TASK_GUIDANCE, DEFAULT_TOOL_GUIDANCE, readClaudeMd, formatProjectContext, configureAgentPermissions,
  writePlaybookHelper, writeMessagesHelper, formatPlaybook, buildRetryContext, discoverRelatedTests, formatSelfReviewBlindSpots, type PlaybookEntry,
} from '../prompt.js';
import { resolveBuildConfig } from '../build-config.js';

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
    if (!errMsg.includes('stale info')) throw e;
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
      if (!errMsg.includes('stale info')) throw e;
      lastErr = e;
      if (attempt < maxRetries) {
        sendLog(ws, `Retry ${attempt}/${maxRetries} also rejected (stale info) — another concurrent push, will retry.\n`);
      }
    }
  }
  throw lastErr;
}

function fmtIssueLoc(i: { file?: string; line?: number }): string {
  if (i.file && typeof i.line === 'number') return ` [${i.file}:${i.line}]`;
  if (i.file) return ` [${i.file}]`;
  return '';
}

type ReviewHistoryIssue = { severity: string; description: string; file?: string; line?: number };
type ReviewHistoryEntry = { iteration: number; feedback: string; issues?: ReviewHistoryIssue[] };
type AnnotatedIssue = ReviewHistoryIssue & { resolvedInIteration?: number };
type AnnotatedEntry = { iteration: number; feedback: string; issues?: AnnotatedIssue[] };

// Jaccard token-overlap threshold for treating a rephrased issue as the SAME
// concept. Reviewers often paraphrase between iterations ("missing null check"
// → "null check not present"); exact-match alone marks those as resolved and
// the next reviewer skips a real bug. False positives here are cheap (reviewer
// re-verifies an unrelated issue once); false negatives are expensive
// (persistent bug ships), so the threshold leans permissive.
export const ISSUE_SIMILARITY_THRESHOLD = 0.4;

const ISSUE_TOKEN_STOPWORDS = new Set([
  'the', 'a', 'an', 'is', 'in', 'on', 'at', 'of', 'to', 'for', 'and', 'or',
  'this', 'that', 'it', 'as', 'be', 'are', 'was', 'were', 'has', 'have', 'had',
  'with', 'by', 'from', 'into', 'when', 'if',
]);

function tokenizeIssue(s: string): Set<string> {
  return new Set(
    s.toLowerCase()
      .split(/[^a-z0-9]+/)
      .filter(t => t.length >= 2 && !ISSUE_TOKEN_STOPWORDS.has(t))
  );
}

function jaccardSimilarity(a: Set<string>, b: Set<string>): number {
  if (a.size === 0 || b.size === 0) return 0;
  let intersection = 0;
  for (const t of a) if (b.has(t)) intersection++;
  const union = a.size + b.size - intersection;
  return union === 0 ? 0 : intersection / union;
}

// Annotate each iteration's issues with whether they reappeared in the next
// iteration. Issues that don't reappear are likely resolved — surfacing that
// signal prevents the next reviewer from re-flagging already-fixed problems or
// wasting effort re-verifying them. Matches both exact descriptions (after
// whitespace/case normalization) and rephrasings (token overlap above
// ISSUE_SIMILARITY_THRESHOLD), so a paraphrased-but-persisting issue is not
// silently marked resolved. The most recent iteration is never annotated (no
// "next" to compare against).
export function annotateResolvedIssues(history: ReviewHistoryEntry[]): AnnotatedEntry[] {
  const sorted = [...history].sort((a, b) => a.iteration - b.iteration);
  const norm = (s: string) => s.toLowerCase().replace(/\s+/g, ' ').trim();
  return sorted.map((entry, idx) => {
    const next = sorted[idx + 1];
    if (!next || !entry.issues || entry.issues.length === 0) return entry;
    const nextIssues = next.issues || [];
    const nextDescs = new Set(nextIssues.map(i => norm(i.description)));
    const nextTokenSets = nextIssues.map(i => tokenizeIssue(i.description));
    const annotated: AnnotatedIssue[] = entry.issues.map(i => {
      if (nextDescs.has(norm(i.description))) return i;
      const currentTokens = tokenizeIssue(i.description);
      if (currentTokens.size > 0) {
        for (const nextTokens of nextTokenSets) {
          if (jaccardSimilarity(currentTokens, nextTokens) >= ISSUE_SIMILARITY_THRESHOLD) {
            return i;
          }
        }
      }
      return { ...i, resolvedInIteration: next.iteration };
    });
    return { ...entry, issues: annotated };
  });
}

function fmtAnnotatedIssue(label: string, i: AnnotatedIssue): string {
  const base = `  [${label}]${fmtIssueLoc(i)} ${i.description}`;
  return i.resolvedInIteration
    ? `${base} (likely resolved — not flagged in iteration ${i.resolvedInIteration})`
    : base;
}

// Truncation budget for raw review-feedback text in the per-iteration history
// section. Used identically in the reviewer and fixer prompt builders below —
// if they diverge, a fix agent may address feedback that the next reviewer
// cannot see, causing false "not addressed" flags. Preferring structured
// `issues` (which have no truncation) happens before this cap kicks in.
export const REVIEW_FEEDBACK_HISTORY_CHARS = 1500;

// Mirrors backend _max_review_iterations (review_pipeline.py): ci_failure=8,
// small=10, medium=12, else=16. Keep in sync — if this drifts, the agent will
// claim "FINAL attempt" at the wrong iteration and collapse targeted fixes
// into one risky rewrite. Backend defaults missing scope to 'medium', so we
// do the same here (a bare task with no context still maps to 12, not 16).
export function computeMaxFixIterations(task: Record<string, unknown>): number {
  const ctx = (task.context as Record<string, unknown> | undefined) || {};
  const scope = (ctx.scope as string | undefined) || 'medium';
  const reviewLoop = ctx.review_loop as Record<string, unknown> | undefined;
  // Backend may pass an effective cap (reduced for low-quality directions). Prefer
  // it so the agent's "iteration N of M" and FINAL-attempt warning match reality.
  const backendMax = reviewLoop?.max_iterations;
  if (typeof backendMax === 'number' && Number.isFinite(backendMax) && backendMax > 0) {
    return Math.floor(backendMax);
  }
  const fixReason = reviewLoop?.fix_reason as string | undefined;
  if (fixReason === 'ci_failure') return 8;
  if (scope === 'small') return 10;
  if (scope === 'medium') return 12;
  return 16;
}

// Medium-scope priority hint based on which subsystem the PR touches. The
// reviewer otherwise weights every checklist section equally, accumulating
// off-target findings (e.g. SQL injection notes on agent-only TypeScript
// changes) that drive extra fix iterations. Returns '' for mixed-subsystem
// PRs so they keep full scrutiny.
export function detectMediumScopeSubsystemHint(changedFiles: string): string {
  const paths = changedFiles
    .split('\n')
    .map(l => l.trim())
    .filter(l => l && !l.startsWith('('))
    .map(l => l.split(/\s+/).pop() || '')
    .filter(Boolean);
  if (paths.length === 0) return '';
  if (paths.every(p => p.startsWith('agent/'))) {
    return 'This is a medium-scope change confined to agent/ (TypeScript agent code). The agent does not touch the database, so deprioritize SQL injection and database query checks. Focus on Correctness & Logic, Build & Type Safety, and TypeScript-specific concerns.\n\n';
  }
  if (paths.every(p => p.startsWith('admin-ui/'))) {
    return 'This is a medium-scope change confined to admin-ui/ (frontend). Deprioritize backend-specific checks like SQL injection, server-side authorization, and database query review. Focus on Correctness & Logic, Build & Type Safety, XSS risks, and frontend-specific concerns.\n\n';
  }
  if (paths.every(p => p.startsWith('backend/'))) {
    if (paths.every(p => p.startsWith('backend/testing/') || /(^|\/)test_[^/]+\.py$|(^|\/)conftest\.py$/.test(p))) {
      return 'This is a medium-scope change confined to backend test files (backend/testing/). Deprioritize SQL injection, authorization, and input-validation checks — these tests do not handle real user input or production database connections. Focus on test quality: correct assertions, test isolation, fixture cleanup, and coverage of the intended behavior.\n\n';
    }
    return 'This is a medium-scope change confined to backend/. Apply full security scrutiny — review SQL injection, authorization checks, input validation, and server-side concerns thoroughly.\n\n';
  }
  const topDirs = new Set(paths.map(p => p.split('/')[0]).filter(Boolean));
  const subsystems = new Set<string>();
  for (const d of topDirs) {
    if (d === 'backend' || d === 'agent' || d === 'admin-ui') subsystems.add(d);
  }
  if (subsystems.size >= 2) {
    const list = Array.from(subsystems).sort().join(', ');
    return `This is a medium-scope cross-system change touching multiple subsystems (${list}). Prioritize interface consistency between subsystems over single-file style concerns: shared types, API contracts, payload shapes, and field names must match across boundaries. Verify both sides of every integration point are updated together — e.g., a backend response field has a corresponding frontend/agent reader, a new payload key is produced and consumed, schema changes are reflected in serializers and TypeScript types. Cross-file inconsistencies (mismatched names, missing updates on one side, divergent assumptions about shape/nullability) are higher severity than local style issues.\n\n`;
  }
  return '';
}

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
  const reviewHistory = (reviewLoop?.review_history as Array<{ iteration: number; feedback: string; issues?: Array<{severity: string; description: string; file?: string; line?: number}> }> | undefined) || [];
  const taskScope = (taskContext?.scope as string | undefined) || 'medium';
  const origTaskType = strVal(payload.original_task_type || '');

  // Clone the full repo so Claude can explore everything, then checkout the PR branch
  statusRef.value = `Cloning ${owner}/${repo} (PR #${prNumber})...`;
  sendLog(ws, `Reviewing PR #${prNumber} on branch ${branch} (iteration ${iteration})\n`);
  execFileSync('rm', ['-rf', WORKSPACE], { encoding: 'utf-8' });
  const cloneUrl = `https://x-access-token:${ghToken}@github.com/${owner}/${repo}.git`;
  // Shallow clone of the PR branch directly — avoids fetch+checkout issues with --single-branch
  await cloneWithRetry(['clone', '--depth', String(DEFAULT_CLONE_DEPTH), '--branch', branch, cloneUrl, WORKSPACE]);
  // Fetch the base branch into the shallow clone so `git diff origin/${baseBranch}...HEAD`
  // and `git merge-base --is-ancestor ... origin/${baseBranch}` actually work.  Without
  // this, the changed-files list and every verification instruction in the review prompt
  // silently fail — the reviewer then hallucinates verdicts from commit-title patterns
  // (notably flagging valid rollup PRs as "empty" because it pattern-matched "(#NNNN)"
  // without a real diff to look at).
  try {
    git(['fetch', 'origin', `+refs/heads/${baseBranch}:refs/remotes/origin/${baseBranch}`, '--depth', String(DEFAULT_CLONE_DEPTH)]);
  } catch (e) {
    sendLog(ws, `WARNING: Failed to fetch origin/${baseBranch} for diff comparison: ${String(e)}\n`);
  }
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

  // Shortstat gives the reviewer objective line-change volume alongside the
  // file list. A "3 files changed" PR that touches 300 lines reads very
  // differently from one that touches 10 — surfacing both prevents scope-label
  // mismatches and gives rollup reviewers proof the PR has real changes.
  let diffShortstat = '';
  try {
    diffShortstat = git(['diff', '--shortstat', `origin/${baseBranch}...HEAD`]).trim();
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
    ? `NOTE: This PR has ${changedFileCount} changed files, which exceeds the typical range for a ${taskScope}-scope change (threshold: ${scopeThreshold}). Treat this as a higher-risk change and review ALL checklist sections thoroughly — Security, Code Quality, and Test Coverage are NOT deprioritized here regardless of the original ${taskScope}-scope label.\n\n`
    : taskScope === 'small'
      ? 'This is a small-scope change. Focus your review on Correctness & Logic and Build & Type Safety. Security and performance sections are lower priority unless the change touches auth, user input handling, or database queries.\n\n'
      : taskScope === 'large'
        ? 'This is a large-scope change with higher risk. Review ALL checklist sections thoroughly. Pay special attention to backwards compatibility, data model safety, and security implications.\n\n'
        : taskScope === 'medium'
          ? detectMediumScopeSubsystemHint(changedFiles)
          : '';

  // Read CLAUDE.md for codebase instructions
  const reviewClaudeMd = readClaudeMd();
  const reviewClaudeMdPrefix = reviewClaudeMd
    ? `## Codebase Instructions\n${reviewClaudeMd}\n\nCheck that the changes respect the File Restrictions listed above. Changes to restricted files should result in REQUEST_CHANGES.\n\n`
    : '';

  // Read project_context for active/sibling task awareness
  const reviewProjectCtx = task.project_context as Record<string, unknown> | undefined;
  const reviewDirectionId = (taskContext?.direction_id as string | undefined) || '';
  const reviewProjectCtxSection = reviewProjectCtx ? formatProjectContext(reviewProjectCtx, 'review', taskScope, reviewDirectionId) + '\n\n' : '';

  // Build prior review history section (for iterations > 1)
  const annotatedReviewHistory = annotateResolvedIssues(reviewHistory);
  const reviewHistorySection = annotatedReviewHistory.length > 0 && iteration > 1
    ? `Issues from previous iterations (verify each was addressed — issues marked "(likely resolved ...)" were not re-flagged in the following iteration and are historical context, not active blockers):\n${annotatedReviewHistory.map(e => {
        if (e.issues && e.issues.length > 0) {
          const critical = e.issues.filter(i => i.severity === 'critical').map(i => fmtAnnotatedIssue('CRITICAL', i));
          const major = e.issues.filter(i => i.severity === 'major').map(i => fmtAnnotatedIssue('MAJOR', i));
          const minor = e.issues.filter(i => i.severity === 'minor').map(i => fmtAnnotatedIssue('MINOR', i));
          return `- Iteration ${e.iteration}:\n` + [...critical, ...major, ...minor].join('\n');
        }
        return `- Iteration ${e.iteration}: ` + e.feedback.slice(0, REVIEW_FEEDBACK_HISTORY_CHARS);
      }).join('\n')}\n\n`
    : '';

  // Build iteration preamble with graduated budget guidance
  const reviewMaxIterations = computeMaxFixIterations(task);
  const reviewMidBudget = iteration * 2 >= reviewMaxIterations && iteration < reviewMaxIterations;
  const reviewBudgetHint = reviewMidBudget
    ? `\nBudget-awareness: you are at or past 50% of the ${reviewMaxIterations}-iteration budget. Do NOT flag MINOR-severity issues — every iteration now costs a meaningful fraction of remaining budget and cosmetic nits are not worth a round trip. Focus only on CRITICAL and MAJOR findings. If only MINOR issues remain, APPROVE and note them as non-blocking polish in \`feedback\`.`
    : '';
  const smallScopeEarlyHint = iteration === 1 && taskScope === 'small' && !isScopeOverflow
    ? `\nSmall-scope early-iteration guidance: MINOR-severity issues (cosmetic nits, naming preferences, non-critical style) are NON-BLOCKING for small-scope changes. Do NOT include them in the \`issues\` array — mention them in \`feedback\` as optional polish. Only CRITICAL and MAJOR issues should drive a REQUEST_CHANGES verdict. A small-scope PR with only MINOR findings should APPROVE.`
    : '';
  const iterationPreamble = `This is review iteration ${iteration} of ${reviewMaxIterations} in the automated review-fix cycle.${iteration > 1 ? '\nPrevious iterations have already addressed some issues. Focus on what remains unresolved.' : ''}${reviewBudgetHint}${smallScopeEarlyHint}`;

  // Rollup PRs (head branch research/* or plan/*) contain N subtask squash-merge
  // commits whose titles end in "(#NNNN)". The `(#NNNN)` marker means a PR was
  // squash-merged — NOT that it landed on origin/main. Subtask PRs merge INTO
  // the rollup branch. We've seen reviewers misread these as "already-on-main"
  // and flag perfectly valid rollup PRs as empty, looping the fix agent on
  // nothing. Make the verification steps explicit.
  const isRollupHead = branch.startsWith('research/') || branch.startsWith('plan/');
  const rollupGuidance = isRollupHead
    ? `
**Rollup-PR verification (IMPORTANT — head branch "${branch}" looks like a rollup):**
A rollup PR aggregates subtask squash-merges into one branch. Commits titled like
"Some feature (#1234)" are subtask merges INTO this rollup branch, not into
${baseBranch}. Before concluding this PR is "empty" or "already merged", verify
objectively:
- Run \`git fetch origin ${baseBranch}\` then \`git merge-base --is-ancestor <sha> origin/${baseBranch}\`
  for any commit SHA you suspect is already on ${baseBranch}. Exit code 0 means
  it IS an ancestor; exit code 1 means it is NOT.
- Run \`git diff --stat origin/${baseBranch}...HEAD\` — a non-empty stat means
  the PR has real changes relative to ${baseBranch}, regardless of how commit
  titles look.
- \`(#NNNN)\` in a commit message is just GitHub's squash-merge format. It does
  NOT imply the commit is on ${baseBranch}.
Only claim "empty PR" / "already merged" if BOTH the merge-base check AND the
diff --stat confirm it. Otherwise treat this as a normal rollup and review the
actual changes.
`
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
        ? `\nFiles changed but NOT in the original plan:\n${unplannedNonTest.map(f => `  - ${f}`).join('\n')}\n\nUnplanned non-test file changes may indicate scope creep — flag them as MAJOR unless they are directly necessary to fix a listed review issue.\n\n`
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
    reviewScopeBudgetSection =
      `## Scope Budget\n` +
      `This PR touches ${rsNonTest} of ${rsBudget} allowed production files (${rsPct}% utilized). ` +
      `When requesting changes, consider whether the fix would require touching additional files that could push the PR over its scope budget. ` +
      `Prefer feedback that can be addressed within existing files.\n\n`;
  }

  // Build review prompt — Claude has full repo access, not just a diff blob
  const reviewPlaybookSection = formatPlaybook(task.playbook as Record<string, PlaybookEntry | string> | undefined);
  const reviewPrompt = `${reviewClaudeMdPrefix}${reviewPlaybookSection}${reviewProjectCtxSection}${reviewHistorySection}You are reviewing PR #${prNumber} on this repository. The PR branch is "${branch}" targeting "${baseBranch}".

${iterationPreamble}
${rollupGuidance}
${originalPrompt ? `The original task was: ${originalPrompt}\n\n` : ''}${prTitle ? `PR title: ${prTitle}\n` : ''}${prBody ? `PR description:\n${prBody}\n\n` : ''}Files changed in this PR:
${changedFiles}${diffShortstat ? `\nDiff volume: ${diffShortstat}\n` : ''}

${selfReviewBlindSpotsSection}
${plannedFilesSection}

You have full access to the codebase. Read any files you need to understand the changes in context. Use \`git diff origin/${baseBranch}...HEAD\` to see the full diff, or \`git diff origin/${baseBranch}...HEAD -- <file>\` for specific files.

Review the changes thoroughly using this checklist:

${reviewScopeBudgetSection}${scopePreamble}${origTaskType === 'qa' ? 'This is a test-only change. The Test Coverage section is less relevant (this IS the test). Focus on whether tests are meaningful and correctly assert behavior.\n\n' : origTaskType === 'migrate' ? 'This is a database migration. Focus on: reversibility (downgrade path), data preservation, and index impact on existing queries.\n\n' : ''}**Correctness & Logic:**
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

After your review, output EXACTLY ONE of the following JSON blocks as the last line of your response. There are only two valid verdicts — no third option exists.

**APPROVE** — the change is ready to merge as-is. Use this when the code is correct, the tests cover the behavior, and nothing blocks shipping. Optional polish notes belong in \`feedback\` as non-blocking observations; they do NOT disqualify APPROVE.
{"verdict": "APPROVE", "feedback": "What you verified and why it's ready to merge. Include any non-blocking polish notes here."}

**REQUEST_CHANGES** — something actionable must change before this merges. Use this for any bug, missing test, broken invariant, merge conflict, failing CI signal, or behavior that would surprise a reader. If you are uncertain between APPROVE and REQUEST_CHANGES, pick REQUEST_CHANGES — another round is cheap; a missed regression is expensive.
{"verdict": "REQUEST_CHANGES", "feedback": "What needs fixing and why it blocks merge.", "issues": [{"severity": "critical|major|minor", "description": "...", "file": "path/to/file.py", "line": 42}]}

Do NOT emit "COMMENT", "NEUTRAL", "ADVISORY", or any other verdict — they are not accepted and will be normalized to REQUEST_CHANGES. There is no informational-only path: if the change is acceptable → APPROVE (put observations in \`feedback\`); if anything needs changing → REQUEST_CHANGES.

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
  const reviewHistory = (reviewLoop?.review_history as Array<{ iteration: number; feedback: string; issues?: Array<{severity: string; description: string; file?: string; line?: number}> }> | undefined) || [];
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
    const latestIssues = reviewHistory[reviewHistory.length - 1]?.issues || [];
    const issueFiles = latestIssues.map(i => i.file).filter((f): f is string => typeof f === 'string' && f.length > 0);
    const originalScopeFiles = includeOriginalScope
      ? [
          ...((fixCtx._planned_files as string[] | undefined) || []),
          ...((fixCtx.related_files as string[] | undefined) || []),
          ...((payload.related_files as string[] | undefined) || []),
        ].filter((f): f is string => typeof f === 'string' && f.length > 0)
      : [];
    const allowedFiles = Array.from(new Set([...issueFiles, ...originalScopeFiles]));
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
  let basePrompt = originalPromptSection + iterationHeader + prompt;
  if (reviewHistory.length > 0) {
    const annotatedHistory = annotateResolvedIssues(reviewHistory);
    const historyLines = annotatedHistory
      .map(e => {
        const issues = e.issues;
        if (issues && issues.length > 0) {
          const critical = issues.filter(i => i.severity === 'critical').map(i => fmtAnnotatedIssue('CRITICAL', i));
          const major = issues.filter(i => i.severity === 'major').map(i => fmtAnnotatedIssue('MAJOR', i));
          const minor = issues.filter(i => i.severity === 'minor').map(i => fmtAnnotatedIssue('MINOR', i));
          return `- Iteration ${e.iteration}:\n${[...critical, ...major, ...minor].join('\n')}`;
        } else {
          return `- Iteration ${e.iteration}: ${e.feedback.slice(0, REVIEW_FEEDBACK_HISTORY_CHARS)}`;
        }
      })
      .join('\n');
    basePrompt = `${originalPromptSection}${iterationHeader}Previous review iterations (issues marked "(likely resolved ...)" were not re-flagged in the following iteration — treat as historical context, focus on the current feedback):\n${historyLines}\n\nCurrent review feedback to address:\n${prompt}`;
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
      const markerCheck = execFileSync('git', ['-C', WORKSPACE, 'grep', '-rl', '<' + '<<<<<<', '--', '*.ts', '*.tsx', '*.py', '*.js', '*.jsx', '*.json'], { encoding: 'utf-8', timeout: CONFLICT_CHECK_TIMEOUT }).trim();
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

  // Prepend CLAUDE.md instructions + playbook
  const fixClaudeMd = readClaudeMd();
  const fixPlaybookSection = formatPlaybook(task.playbook as Record<string, PlaybookEntry | string> | undefined);
  const fixRetryContext = buildRetryContext(task);
  const fixFirstTaskSection = task.first_task_on_repo ? FIRST_TASK_GUIDANCE : '';
  if (fixClaudeMd) {
    enrichedPrompt = `## Codebase Instructions\n${fixClaudeMd}\n\n${fixPlaybookSection}${fixRetryContext}${enrichedPrompt}`;
  } else if (fixPlaybookSection || fixRetryContext) {
    enrichedPrompt = `${fixPlaybookSection}${fixRetryContext}${enrichedPrompt}`;
  }

  // Append fix-specific task type hint and playbook guidance
  const fixTypeHints = getTaskTypeHints(fixBuildConfig);
  if (fixTypeHints['fix']) {
    enrichedPrompt = `${enrichedPrompt}\n\n${fixTypeHints['fix']}`;
  }
  enrichedPrompt = `${enrichedPrompt}${fixFirstTaskSection}${PLAYBOOK_GUIDANCE}\nNote: If the review feedback revealed a recurring pattern or non-obvious codebase rule, save it to the playbook.\n\n${DEFAULT_TOOL_GUIDANCE}`;

  // Capture test baseline for medium/large scope fix tasks before running Claude.
  // This lets testFixLoop distinguish pre-existing failures from newly-broken tests,
  // matching the same logic used in the create handler.
  const fixScope = (task.context as Record<string, unknown> | undefined)?.scope as string | undefined;
  let fixBaselineFailures: Set<string> | undefined;
  if (fixScope === 'medium' || fixScope === 'large') {
    const captured = await captureTestBaseline(ws, statusRef, fixBuildConfig, fixRelatedFiles);
    fixBaselineFailures = captured ?? undefined;
  }

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
    const lintPassed = await lintFixLoop(changedPaths, ws, statusRef, 'the fix you just applied', model, effort, fixBuildConfig);
    if (!lintPassed) {
      sendLog(ws, 'Lint checks still failing after max attempts — failing review-fix cycle early.\n');
      await persistAndPostStatus(taskId, {
        task_id: taskId, status: 'FAILED', artifact_type: 'ERROR_LOG',
        logs: 'Lint/build checks failed after max fix attempts during review-fix cycle.',
        failure_category: 'lint_failed',
        changed_files: changedPaths.slice(0, 50),
      });
      return;
    }

    const { testFailuresDetected: fixTestFailures, passed: fixTestsPassed, failureSummary: fixTestFailureSummary } = await testFixLoop(ws, statusRef, model, effort, buildConfig, fixBaselineFailures);

    // Match standard handler behaviour: if tests still fail after the fix loop,
    // fail the task rather than pushing known-broken code into the review pipeline.
    if (fixTestFailures && !fixTestsPassed) {
      sendLog(ws, 'Tests failed after review-fix — marking task as FAILED instead of pushing known-broken code.\n');
      const failedLogs = fixTestFailureSummary
        ? `Test suite failed after review-fix attempts. Failing tests:\n${fixTestFailureSummary}`
        : `Test suite failed after review-fix attempts. Last output:\n${sanitize(allOutput.slice(-10_000))}`;
      await persistAndPostStatus(taskId, {
        task_id: taskId, status: 'FAILED', artifact_type: 'ERROR_LOG',
        logs: failedLogs,
        failure_category: 'test_failed',
        ...(fixTestFailureSummary ? { test_failure_summary: fixTestFailureSummary.slice(0, 5000) } : {}),
      });
      return;
    }

    git(['add', '-A']);

    // Parse restricted paths from CLAUDE.md and unstage any violations before commit.
    {
      const claudeMdContent = readClaudeMd();
      if (claudeMdContent) {
        const restrictedPaths: string[] = [];
        for (const line of claudeMdContent.split('\n')) {
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

    const shortId = taskId.replace(/-/g, '').slice(0, 8);
    const testNote = fixTestFailures
      ? `\n\nNote: test failures detected, manual review recommended${fixTestFailureSummary ? `:\n${fixTestFailureSummary.slice(0, 500)}` : ''}`
      : '';
    // Only create a new commit if there are uncommitted changes (Claude may have already committed).
    // Two-step check: working tree has any change → stage it all → inspect the
    // staged diff. If nothing ends up staged (e.g. the only "changes" were
    // untracked ignored files, or pre-commit hooks in Claude's prior runs
    // cleaned up), skip the commit instead of hitting "nothing to commit" —
    // which, with pre-commit hooks firing on commit, previously surfaced as
    // an opaque "Command failed: git commit" and burned all 3 retries without
    // progress.
    const fixStaged = git(['status', '--porcelain']);
    if (fixStaged.length > 0) {
      // Make sure everything the agent modified is actually staged before we
      // attempt the commit. Without this, Edit-tool changes that weren't
      // `git add`ed by Claude would leave the index empty.
      try { git(['add', '-A']); } catch { /* pre-commit add-failures are rare; fall through */ }
      let hasStagedDiff = true;
      try {
        execFileSync('git', ['-C', WORKSPACE, 'diff', '--cached', '--quiet'], { encoding: 'utf-8' });
        // Exit 0 = no staged diff; exit 1 = has staged diff (throws)
        hasStagedDiff = false;
      } catch {
        hasStagedDiff = true;
      }
      if (!hasStagedDiff) {
        sendLog(ws, 'REVIEW_FIX_NO_CHANGES: working tree has changes but nothing staged for commit — skipping (likely untracked-only or reverted by hooks)\n');
      } else {
        const committed = gitCommit(`blueprint: address review feedback\n\nAutomated by Blueprint Agent (task: ${shortId})${testNote}`);
        if (!committed) sendLog(ws, 'Nothing to commit after review fix (pre-commit hook may have reverted changes)\n');
      }
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
        // Both rebase and merge failed — the working tree is fundamentally
        // conflicted. Fail fast so the backend retry mechanism can spawn a
        // fresh attempt with a clean clone, rather than burning a Claude call
        // on post-push in-place repair of complex conflicts.
        try { git(['merge', '--abort']); } catch { /* ignore */ }
        sendLog(ws, `Rebase and merge both failed (${conflictList || 'unknown files'}) — failing task for fresh retry\n`);
        await persistAndPostStatus(taskId, {
          task_id: taskId, status: 'FAILED', artifact_type: 'ERROR_LOG',
          logs: `Rebase onto ${baseBranch} failed and merge fallback also failed. Conflicting files: ${conflictList || 'unknown'}. Failing task so retry spawns a fresh clone.`,
          failure_category: 'rebase_failed',
        });
        return;
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
        gitCommit('blueprint: resolve remaining conflict markers');
        // Final check — if markers persist after retry, fail this cycle so the
        // retry mechanism can spawn a fresh attempt. Pushing conflict markers
        // guarantees a CI failure and burns a full review-fix iteration.
        try {
          const stillConflicting = execFileSync('git', ['-C', WORKSPACE, 'grep', '-rl', '<' + '<<<<<<', '--', '*.ts', '*.tsx', '*.py', '*.js', '*.jsx', '*.json'], { encoding: 'utf-8', timeout: 10_000 }).trim();
          if (stillConflicting) {
            sendLog(ws, `Conflict markers persist after retry in:\n${stillConflicting}\nFailing this cycle — will not push known-broken code.\n`);
            await persistAndPostStatus(taskId, {
              task_id: taskId, status: 'FAILED', artifact_type: 'ERROR_LOG',
              logs: `Conflict markers still present after retry, refusing to push:\n${stillConflicting}`,
              failure_category: 'unresolved_conflicts',
            });
            return;
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
        gitCommit(`blueprint: fix post-rebase lint errors\n\nAutomated by Blueprint Agent (task: ${shortId})`);
      }
      // Re-validate that the fix actually resolved lint. Without this, a partial
      // fix gets pushed and only surfaces as a CI failure — burning a full review
      // cycle. Recompute paths in case Claude touched new files during the fix.
      const revalidatePaths = git(['diff', '--name-only', `origin/${baseBranch}..HEAD`]).split('\n').filter(Boolean);
      try {
        runProjectLintChecks(revalidatePaths, ws, fixBuildConfig);
      } catch (revalidateErr: unknown) {
        const revalidateMsg = revalidateErr instanceof Error ? revalidateErr.message : String(revalidateErr);
        sendLog(ws, `Post-rebase lint still failing after fix attempt — failing this cycle, will not push known-broken code:\n${revalidateMsg.slice(0, 2000)}\n`);
        await persistAndPostStatus(taskId, {
          task_id: taskId, status: 'FAILED', artifact_type: 'ERROR_LOG',
          logs: `Post-rebase lint still failing after fix attempt:\n${revalidateMsg.slice(0, 5000)}`,
          failure_category: 'lint_failed',
        });
        return;
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
      await pushForceWithLeaseRetrying(branch, ws);
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
