import fs from 'fs';
import { execFileSync } from 'child_process';
import { createHash } from 'crypto';
import type WebSocket from 'ws';
import {
  WORKSPACE, strVal, sanitize, sendLog, validateBranchName,
  persistAndPostStatus, git, gitAsync,
  runClaudeCode, sendHeartbeatSync, setCurrentPromptFingerprint,
  captureTestBaseline, testFixLoop, extractPytestSelectorsFromOutput,
  snapshotToolTrace, extractInvestigatedFiles,
} from '../index.js';
import { runCodeChangeTask } from './code-change.js';
import { runFreshReviewerPass } from './standard.js';
import { prepareWorkspaceForRole } from '../workspacePrep.js';
import type { BuildConfig } from '../index.js';
import { DEFAULT_CLONE_DEPTH, SCOPE_OVERFLOW_THRESHOLDS, PUSH_FORCE_WITH_LEASE_MAX_RETRIES, PUSH_FORCE_WITH_LEASE_RETRY_DELAY, CONFLICT_CHECK_TIMEOUT, FRONTEND_NPM_TIMEOUT } from '../constants.js';
import {
  getTaskTypeHints, PLAYBOOK_GUIDANCE, FIRST_TASK_GUIDANCE, DEFAULT_TOOL_GUIDANCE, CODE_CHANGE_HELPER_GUIDANCE,
  FIX_TASK_FOCUS, getTaskTypeFocusBlock,
  assembleFailureGuidanceBlocks,
  readClaudeMd, formatProjectContext, configureAgentPermissions, computePromptFingerprint,
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
type ReviewHistoryEntry = {
  iteration: number;
  feedback: string;
  issues?: ReviewHistoryIssue[];
  // Backend writes "APPROVE" | "REQUEST_CHANGES" starting with the convergence
  // dialog change. Older entries may lack the field; renderers fall back.
  verdict?: 'APPROVE' | 'REQUEST_CHANGES';
  // Files in the diff at this iteration boundary, capped at 30 by the backend.
  // Used by the trajectory + drift section in fix prompts.
  changed_files?: string[];
};
type AnnotatedIssue = ReviewHistoryIssue & { resolvedInIteration?: number };
type AnnotatedEntry = { iteration: number; feedback: string; issues?: AnnotatedIssue[] };

type DriftInEntry = {
  file: string;
  first_appeared_iteration?: number;
  flag_count?: number;
};
type DriftSignal = { drift_in?: DriftInEntry[]; drift_out?: string[] };

type ReviewPromptFingerprintOptions = {
  branch?: string;
  baseBranch?: string;
  includePlannedFilesClassificationGuidance?: boolean;
};

// ── Static review-guidance fragments (shared with prompt fingerprint) ────────
// These constants and helpers are consumed by both `handleReview()`'s launched
// prompt and `buildReviewPromptFingerprintContent()`. Edits to any fragment
// move the fingerprint, so review telemetry tracks real prompt changes and
// cannot silently drift while claiming the same version.
const REVIEW_CLAUDEMD_PROMPT_HEADER = '## Codebase Instructions\n';
const REVIEW_CLAUDEMD_PROMPT_FOOTER = '\n\nCheck that the changes respect the File Restrictions listed above. Changes to restricted files should result in REQUEST_CHANGES.\n\n';
const REVIEW_FILE_RESTRICTIONS_NOTE = 'Check that the changes respect the File Restrictions listed above. Changes to restricted files should result in REQUEST_CHANGES.';

const REVIEW_HISTORY_INTRO = 'Issues from previous iterations (verify each was addressed — issues marked "(likely resolved ...)" were not re-flagged in the following iteration and are historical context, not active blockers):';
const REVIEW_ITERATION_PRIOR_NOTE = '\nPrevious iterations have already addressed some issues. Focus on what remains unresolved.';

function buildReviewBudgetMidHint(maxIters: number): string {
  return `\nBudget-awareness: you are at or past 50% of the ${maxIters}-iteration budget. Do NOT flag MINOR-severity issues — every iteration now costs a meaningful fraction of remaining budget and cosmetic nits are not worth a round trip. Focus only on CRITICAL and MAJOR findings. If only MINOR issues remain, APPROVE and note them as non-blocking polish in \`feedback\`.`;
}
const REVIEW_SMALL_SCOPE_EARLY_HINT = `\nSmall-scope early-iteration guidance: MINOR-severity issues (cosmetic nits, naming preferences, non-critical style) are NON-BLOCKING for small-scope changes. Do NOT include them in the \`issues\` array — mention them in \`feedback\` as optional polish. Only CRITICAL and MAJOR issues should drive a REQUEST_CHANGES verdict. A small-scope PR with only MINOR findings should APPROVE.`;

export function buildReviewIterationPreamble(
  iteration: number,
  maxIters: number,
  midBudget: boolean,
  smallScopeEarly: boolean,
): string {
  const budgetHint = midBudget ? buildReviewBudgetMidHint(maxIters) : '';
  const smallHint = smallScopeEarly ? REVIEW_SMALL_SCOPE_EARLY_HINT : '';
  const priorNote = iteration > 1 ? REVIEW_ITERATION_PRIOR_NOTE : '';
  return `This is review iteration ${iteration} of ${maxIters} in the automated review-fix cycle.${priorNote}${budgetHint}${smallHint}`;
}

const REVIEW_QA_FOCUS_HINT = 'This is a test-only change. The Test Coverage section is less relevant (this IS the test). Focus on whether tests are meaningful and correctly assert behavior.\n\n';
const REVIEW_MIGRATE_FOCUS_HINT = 'This is a database migration. Focus on: reversibility (downgrade path), data preservation, and index impact on existing queries.\n\n';

const REVIEW_SMALL_SCOPE_PREAMBLE = 'This is a small-scope change. Focus your review on Correctness & Logic and Build & Type Safety. Security and performance sections are lower priority unless the change touches auth, user input handling, or database queries.\n\n';
const REVIEW_LARGE_SCOPE_PREAMBLE = 'This is a large-scope change with higher risk. Review ALL checklist sections thoroughly. Pay special attention to backwards compatibility, data model safety, and security implications.\n\n';

function buildReviewScopeOverflowPreamble(taskScope: string, changedFileCount: number, scopeThreshold: number): string {
  return `NOTE: This PR has ${changedFileCount} changed files, which exceeds the typical range for a ${taskScope}-scope change (threshold: ${scopeThreshold}). Treat this as a higher-risk change and review ALL checklist sections thoroughly — Security, Code Quality, and Test Coverage are NOT deprioritized here regardless of the original ${taskScope}-scope label.\n\n`;
}

function buildReviewScopeBudgetSection(nonTest: number, budget: number, pct: number): string {
  return (
    `## Scope Budget\n` +
    `This PR touches ${nonTest} of ${budget} allowed production files (${pct}% utilized). ` +
    `When requesting changes, consider whether the fix would require touching additional files that could push the PR over its scope budget. ` +
    `Prefer feedback that can be addressed within existing files.\n\n`
  );
}

const REVIEW_FIX_RESPONSES_HEADER = '## Responses from prior fix iteration\n\n';
function buildReviewFixResponsesIntro(n: number): string {
  return (
    `The fix agent recorded structured rationale for ${n} issue(s) it left unaddressed. ` +
    `Treat these as CLAIMS TO VERIFY, never as conclusions to accept.\n\n`
  );
}
const REVIEW_FIX_RESPONSES_INSTRUCTIONS = (
  `For each disputed/alternative/partial response below:\n` +
  `  1. Run every step in \`verify steps\`. Quote the actual command output in your feedback.\n` +
  `  2. If all verify steps confirm the claim → DROP the original issue (do not re-raise).\n` +
  `  3. If any verify step fails or is non-actionable ("trust me", "I tested it") → re-raise the original issue with severity major AND quote the specific verify step that failed so the next fix iteration knows what to address.\n` +
  `  4. If you cannot run a verify step (e.g. it requires data you don't have) → flag as "unverifiable" with severity major; do not silently accept.\n\n` +
  `**Sycophancy guard**: a plausible-sounding claim with no verify steps, or with verify steps that boil down to "the agent says so", MUST be rejected. Real disagreements have concrete checks.\n\n`
);

function buildReviewMasterPreamble(prNumber: number | string, branch: string, baseBranch: string): string {
  return `You are reviewing PR #${prNumber} on this repository. The PR branch is "${branch}" targeting "${baseBranch}".`;
}
function buildReviewDiffAccessHint(baseBranch: string): string {
  return `You have full access to the codebase. Read any files you need to understand the changes in context. Use \`git diff origin/${baseBranch}...HEAD\` to see the full diff, or \`git diff origin/${baseBranch}...HEAD -- <file>\` for specific files.`;
}

function buildReviewChecklistSection(buildConfig?: BuildConfig): string {
  return `Review the changes thoroughly using this checklist:

**Correctness & Logic:**
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

${buildTypeCheckInstructions(buildConfig)}`;
}

function buildReviewSeverityGuideSection(): string {
  return `**Severity Classification Guide:**
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
  - Redundant else after a return statement`;
}

function buildReviewVerdictInstructions(taskId: string): string {
  return `After your review, output EXACTLY ONE of the following JSON blocks as the last line of your response. There are only two valid verdicts — no third option exists.

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
}

function buildRollupReviewGuidance(branch: string, baseBranch: string): string {
  const isRollupHead = branch.startsWith('research/') || branch.startsWith('plan/');
  return isRollupHead
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
}

function buildPlannedFilesClassificationGuidance(): string {
  return `Classify each unplanned non-test file before flagging. Apply skepticism — do not assume a stated reason is correct, but do not flag categories that are valid even when unstated:
  - scope-creep: file has no causal link to anything in the plan or prior reviews. → MAJOR.
  - caller-impact: a planned file's API change forced consumer updates here. Verify the consumer reference exists (grep the planned file for the new/changed API and confirm this file uses it). → not a violation.
  - discovered-dependency: planned change required a missing import / helper / flag-flip that lives here. Verify by reading the planned file and confirming the dependency. → not a violation.
  - cluster-boundary-correction: file logically belongs in this cluster but was misallocated to a sibling task. Verify the PR description explicitly explains the correction. → not a violation IF acknowledged in PR description; flag MAJOR if not acknowledged.
If unsure on edge cases, do not flag — let the operator decide.`;
}

function buildReviewCoreGuidance(buildConfig: BuildConfig | undefined, taskId: string): string {
  return `${buildReviewChecklistSection(buildConfig)}

${buildReviewSeverityGuideSection()}

${buildReviewVerdictInstructions(taskId)}`;
}

function buildReviewPromptFingerprintContent(
  buildConfig?: BuildConfig,
  options: ReviewPromptFingerprintOptions = {},
): string {
  // Concatenation of every static review-guidance fragment that handleReview()
  // can inject into the launched prompt. Conditional branches are forced ON
  // here (e.g. iteration > 1, mid-budget, small-scope-early) so an edit to any
  // template fragment moves the fingerprint regardless of whether a specific
  // task happens to trigger that branch. Per-task data is filled with stable
  // placeholders so the fingerprint stays invariant across tasks.
  const sections: string[] = [
    REVIEW_CLAUDEMD_PROMPT_HEADER,
    REVIEW_CLAUDEMD_PROMPT_FOOTER,
    REVIEW_HISTORY_INTRO,
    buildReviewIterationPreamble(2, 0, true, true),
    REVIEW_QA_FOCUS_HINT,
    REVIEW_MIGRATE_FOCUS_HINT,
    REVIEW_SMALL_SCOPE_PREAMBLE,
    REVIEW_LARGE_SCOPE_PREAMBLE,
    buildReviewScopeOverflowPreamble('<SCOPE>', 0, 0),
    buildReviewScopeBudgetSection(0, 0, 0),
    REVIEW_FIX_RESPONSES_HEADER,
    buildReviewFixResponsesIntro(0),
    REVIEW_FIX_RESPONSES_INSTRUCTIONS,
    buildReviewMasterPreamble('<PR>', '<BRANCH>', '<BASE>'),
    buildReviewDiffAccessHint('<BASE>'),
    REVIEW_FILE_RESTRICTIONS_NOTE,
    buildReviewCoreGuidance(buildConfig, 'TASK_ID'),
  ];
  const rollupGuidance = buildRollupReviewGuidance(options.branch || '', options.baseBranch || 'main');
  if (rollupGuidance) sections.push(rollupGuidance.trim());
  if (options.includePlannedFilesClassificationGuidance) {
    sections.push(buildPlannedFilesClassificationGuidance());
  }
  sections.push(DEFAULT_TOOL_GUIDANCE);
  return sections.join('\n\n');
}

export function computeReviewPromptFingerprint(
  buildConfig?: BuildConfig,
  options: ReviewPromptFingerprintOptions = {},
): string {
  return createHash('sha256')
    .update(buildReviewPromptFingerprintContent(buildConfig, options))
    .digest('hex')
    .slice(0, 12);
}

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
    const next = sorted.at(idx + 1);
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

// Skepticism gate: a structured fix-agent response is only kept if it has at
// least one mechanical verify step. "Trust me", "I'm confident", "I tested
// it" are sycophancy patterns that turn the reviewer into a judge of rhetoric
// rather than a verifier. Mechanical steps reference commands, files, or
// greppable assertions. Heuristic — false positives here are cheap (one
// extra reviewer round trip), false negatives are expensive (sycophancy slips
// through), so the heuristic leans permissive on what counts as mechanical.
const NON_MECHANICAL_VERIFY_PATTERNS = [
  /\btrust me\b/i,
  /\bi tested\b/i,
  /\bi'?m confident\b/i,
  /\bbelieve me\b/i,
  /\btake my word\b/i,
];
export function verifyStepsOk(entry: Record<string, unknown>): boolean {
  const claim = typeof entry.claim === 'string' ? entry.claim.trim() : '';
  if (claim.length === 0) return false;
  const verify = Array.isArray(entry.verify) ? entry.verify : [];
  if (verify.length === 0) return false;
  const stringSteps = verify.filter((s): s is string => typeof s === 'string' && s.trim().length > 0);
  if (stringSteps.length === 0) return false;
  // Reject if EVERY verify step is non-mechanical. A mix is OK — the reviewer
  // can pick the actionable ones. This way honest agents who pad with
  // "I also re-read the code" still get their verifiable steps respected.
  const allNonMechanical = stringSteps.every(step =>
    NON_MECHANICAL_VERIFY_PATTERNS.some(p => p.test(step)),
  );
  return !allNonMechanical;
}

// Render a trajectory section showing each iteration's verdict and changed-file
// signature. Empty when no entries carry verdict info (older backends that
// hadn't shipped the trajectory plumbing). Surfaces approved iterations
// alongside REQUEST_CHANGES — the load-bearing signal for cases like PR #3435
// where iter 1 was approved but the test changes got lost in iter 2's rebase.
export function formatTrajectorySection(history: ReviewHistoryEntry[]): string {
  if (history.length === 0) return '';
  const hasVerdicts = history.some(e => e.verdict === 'APPROVE' || e.verdict === 'REQUEST_CHANGES');
  if (!hasVerdicts) return '';
  const sorted = [...history].sort((a, b) => a.iteration - b.iteration);
  const lines = sorted.map(e => {
    const verdict = e.verdict || 'REQUEST_CHANGES';
    const filesPart = e.changed_files && e.changed_files.length > 0
      ? ` — diff: ${e.changed_files.slice(0, 8).join(', ')}${e.changed_files.length > 8 ? `, +${e.changed_files.length - 8} more` : ''}`
      : '';
    return `  iter ${e.iteration}: ${verdict}${filesPart}`;
  });
  return `## PR trajectory\n${lines.join('\n')}\n\n`;
}

// Render the drift signal section using fields computed by the orchestrator
// at fix-spawn time (review_pipeline.compute_drift_signal). Informational —
// the agent decides whether to revert, justify, or expand scope.
export function formatDriftSection(drift: DriftSignal | undefined): string {
  if (!drift) return '';
  const driftIn = drift.drift_in || [];
  const driftOut = drift.drift_out || [];
  if (driftIn.length === 0 && driftOut.length === 0) return '';
  const inLines = driftIn.map(d => {
    const meta: string[] = [];
    if (typeof d.first_appeared_iteration === 'number') {
      meta.push(`first appeared iter ${d.first_appeared_iteration}`);
    }
    if (typeof d.flag_count === 'number' && d.flag_count > 0) {
      meta.push(`flagged ${d.flag_count}x`);
    }
    const metaStr = meta.length > 0 ? `    (${meta.join('; ')})` : '';
    return `  - ${d.file}${metaStr}`;
  });
  const outLines = driftOut.map(f => `  - ${f}`);
  let body = '## Drift signal (informational — not a directive)\n';
  if (inLines.length > 0) {
    body += `Files in current diff but NOT in {planned ∪ any-approved-iteration}:\n${inLines.join('\n')}\n\n`;
  }
  if (outLines.length > 0) {
    body += `Files expected (planned or approved before) but missing from current diff:\n${outLines.join('\n')}\n\n`;
  }
  body +=
    'Reading the trajectory: a file that drifted in during a rebase or earlier fix, ' +
    "has been flagged for removal repeatedly, and isn't justified by caller-impact / " +
    'discovered-dep / cluster-boundary correction → reverting via `git checkout origin/<base> -- <file>` ' +
    '(or `git rm` if newly added) usually breaks the loop.\n\n' +
    'But scope expansion IS sometimes the right call. If the planned files are genuinely insufficient ' +
    "and a sibling cluster's dependency or a discovered caller forces a wider edit, keep the change " +
    'AND record a structured response (see "Responding to reviewer issues" below). The reviewer accepts ' +
    'caller-impact / discovered-dependency / cluster-boundary corrections — your job is to distinguish ' +
    'those from leftover drift, not to reflexively revert.\n\n';
  return body;
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

// Detect migration-file conflicts that go beyond text-level conflict markers:
// when the PR introduces a new alembic migration and the base branch has
// gained additional migrations with equal-or-higher revision numbers since the
// PR branched, the PR's revision id and down_revision are stale and must be
// renumbered even if git produced no conflict markers. Pure-function form so
// it can be unit-tested without touching the filesystem.
const ALEMBIC_VERSIONS_DIR = 'backend/alembic/versions/';
export type MigrationConflict = {
  detected: boolean;
  prMigrations: string[];
  baseNewMigrations: string[];
};
export function detectMigrationConflict(
  prFiles: string[],
  baseBranchFiles: string[],
): MigrationConflict {
  const isMigration = (f: string) =>
    f.startsWith(ALEMBIC_VERSIONS_DIR) && f.endsWith('.py') && !f.endsWith('__init__.py');
  const revNum = (f: string): number => {
    const base = f.slice(ALEMBIC_VERSIONS_DIR.length);
    const m = base.match(/^(\d+)_/);
    return m ? parseInt(m[1], 10) : -1;
  };
  const prMigrations = prFiles.filter(isMigration);
  if (prMigrations.length === 0) return { detected: false, prMigrations: [], baseNewMigrations: [] };
  const prRevs = prMigrations.map(revNum).filter(n => n >= 0);
  if (prRevs.length === 0) return { detected: false, prMigrations, baseNewMigrations: [] };
  const minPrRev = Math.min(...prRevs);
  const prSet = new Set(prMigrations);
  const baseNewMigrations = baseBranchFiles
    .filter(isMigration)
    .filter(f => !prSet.has(f) && revNum(f) >= minPrRev);
  return { detected: baseNewMigrations.length > 0, prMigrations, baseNewMigrations };
}

export function buildMigrationConflictGuidance(c: MigrationConflict): string {
  if (!c.detected) return '';
  const latestBase = [...c.baseNewMigrations].sort().pop() || '';
  const latestBaseBase = latestBase.slice(ALEMBIC_VERSIONS_DIR.length);
  const latestRevMatch = latestBaseBase.match(/^(\d+)_/);
  const latestRev = latestRevMatch ? latestRevMatch[1] : '';
  const latestRevNum = latestRev ? parseInt(latestRev, 10) : NaN;
  const padWidth = Math.max(latestRev.length, 4);
  const nextRev = Number.isFinite(latestRevNum)
    ? String(latestRevNum + 1).padStart(padWidth, '0')
    : '';
  const prFile = c.prMigrations[0] || '';
  const prBase = prFile.slice(ALEMBIC_VERSIONS_DIR.length);
  const prDescMatch = prBase.match(/^\d+_(.+)\.py$/);
  const prDesc = prDescMatch ? prDescMatch[1] : '<description>';
  const renamedFile = nextRev
    ? `${ALEMBIC_VERSIONS_DIR}${nextRev}_${prDesc}.py`
    : '';
  const concrete = nextRev && latestRev;
  return (
    `## Migration revision conflict\n` +
    `This PR adds alembic migration file(s):\n${c.prMigrations.map(f => `  - ${f}`).join('\n')}\n` +
    `But the base branch has gained migration file(s) with equal-or-higher revision numbers that this PR does not yet incorporate:\n` +
    `${c.baseNewMigrations.map(f => `  - ${f}`).join('\n')}\n\n` +
    `Resolution steps (migrations are immutable once applied — do NOT edit existing files in backend/alembic/versions/):\n` +
    (concrete
      ? `1. Latest base-branch migration is ${latestBase} (revision="${latestRev}"). Open it to confirm its \`revision\` string matches "${latestRev}".\n` +
        `2. Rename this PR's migration to ${renamedFile} and set \`revision = "${nextRev}"\` inside it.\n` +
        `3. Set this PR's migration \`down_revision = "${latestRev}"\` so the chain is linear.\n` +
        `4. Set \`EXPECTED_SCHEMA_REVISION = "${nextRev}"\` in backend/app/main.py to match the renumbered revision.\n`
      : `1. Read the latest migration on the base branch (${latestBase}) to obtain its revision id.\n` +
        `2. Rename this PR's migration file to the next sequential numeric prefix AFTER the base branch's latest, and update its \`revision\` string to a fresh unique id.\n` +
        `3. Set this PR's migration \`down_revision\` to the base branch's latest revision id so the chain is linear.\n` +
        `4. Bump \`EXPECTED_SCHEMA_REVISION\` in backend/app/main.py to match the renumbered revision id.\n`) +
    `5. Re-run the backend tests (\`bash scripts/test-backend.sh\`) to confirm the chain applies cleanly.\n\n`
  );
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

// Pre-install node_modules in every node-typed directory in the reviewer's
// workspace. Best-effort: any individual install failure is logged and
// swallowed so the review can still proceed — the prompt also instructs
// reviewers to install before building, this is the belt to that suspenders.
//
// For repos with a configured BuildConfig, iterates buildConfig.directories.
// For Blueprint's self-repo (is_self_repo=true → undefined buildConfig), uses
// the same hardcoded admin-ui/agent/root list as the fallback prompt.
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

// ── Build instruction generator for review prompts ───────────────────────────
// Build commands are emitted with `npm ci &&` (or equivalent install step)
// prefixed for node-typed directories so the reviewer's cold-clone workspace
// always has dependencies available — without the prefix, every reviewer on a
// fresh VM hits "cannot resolve <package>" and flags it as a build-gate
// failure even when the underlying PR is fine.
function buildTypeCheckInstructions(buildConfig?: BuildConfig): string {
  if (!buildConfig || !buildConfig.directories || buildConfig.directories.length === 0) {
    return `**Build & Type Safety (MUST run these — do not skip):**
- For TypeScript/JavaScript changes: find the package root for each changed file (the nearest parent directory with a package.json), then run \`npm ci && npm run build\` (or \`npx tsc --noEmit\` for TypeScript-only packages) — report any errors
- For Python changes: run \`python -m ruff check <changed_files>\` or \`python -m py_compile <changed_files>\` to verify syntax/style — report any errors
- For Go changes: run \`go build ./...\` — report any errors; for Rust: run \`cargo check\`
- Only run checks for the subsystems touched by the changed files. Do not invent additional build invocations.
- For ANY code changes: run the relevant command above and include results in your verdict. A build failure is an automatic REQUEST_CHANGES — but FIRST verify your workspace is fully installed: a missing-package error like "cannot resolve <foo>" is almost always a stale workspace, not a real defect. Re-run with the install step prefix above before flagging.`;
  }
  const checks = buildConfig.directories
    .filter(d => d.build_cmd || d.lint_cmd)
    .map(d => {
      const cmd = d.build_cmd || d.lint_cmd;
      const installPrefix = d.type === 'node'
        ? 'npm ci && '
        : d.install_cmd ? `${d.install_cmd} && ` : '';
      return `- For changes in \`${d.path}/\`: run \`cd ${d.path} && ${installPrefix}${cmd}\` — report any errors`;
    });
  if (checks.length === 0) {
    return '**Build & Type Safety:** No build commands configured — review code manually for type correctness.';
  }
  return `**Build & Type Safety (MUST run these — do not skip):**
${checks.join('\n')}
- Only run the build commands listed above. Do not invent additional build invocations against directories without configured commands.
- For ANY code changes: run the relevant command above and include results in your verdict. A build failure is an automatic REQUEST_CHANGES — but FIRST verify your workspace is fully installed: a missing-package error like "cannot resolve <foo>" is almost always a stale workspace, not a real defect.`;
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
  const cloneUrl = `https://x-access-token:${ghToken}@github.com/${owner}/${repo}.git`;
  // Shallow clone of the PR branch directly — avoids fetch+checkout issues with --single-branch
  await prepareWorkspaceForRole({
    workspace: WORKSPACE, repoUrl: cloneUrl, githubToken: ghToken,
    branch, baseBranch, depth: DEFAULT_CLONE_DEPTH,
  });
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

  // Pre-install node_modules in every node-typed directory the reviewer might
  // build. Without this, every cold-clone reviewer hits "cannot resolve <pkg>"
  // when running `npm run build`, mistakes the empty workspace for a real
  // build defect, and flags it as a blocker — burning review-fix iterations on
  // a phantom env failure (PR #3884 saga, 2026-05-06).
  preinstallNodeDeps(WORKSPACE, reviewBuildConfig, ws, taskId);

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
    ? buildReviewScopeOverflowPreamble(taskScope, changedFileCount, scopeThreshold)
    : taskScope === 'small'
      ? REVIEW_SMALL_SCOPE_PREAMBLE
      : taskScope === 'large'
        ? REVIEW_LARGE_SCOPE_PREAMBLE
        : taskScope === 'medium'
          ? detectMediumScopeSubsystemHint(changedFiles)
          : '';

  // Read CLAUDE.md for codebase instructions
  const reviewClaudeMd = readClaudeMd();
  const reviewClaudeMdPrefix = reviewClaudeMd
    ? `${REVIEW_CLAUDEMD_PROMPT_HEADER}${reviewClaudeMd}${REVIEW_CLAUDEMD_PROMPT_FOOTER}`
    : '';

  // Read project_context for active/sibling task awareness
  const reviewProjectCtx = task.project_context as Record<string, unknown> | undefined;
  const reviewDirectionId = (taskContext?.direction_id as string | undefined) || '';
  const reviewProjectCtxSection = reviewProjectCtx ? formatProjectContext(reviewProjectCtx, 'review', taskScope, reviewDirectionId) + '\n\n' : '';

  // Build prior review history section (for iterations > 1)
  const annotatedReviewHistory = annotateResolvedIssues(reviewHistory);
  const reviewHistorySection = annotatedReviewHistory.length > 0 && iteration > 1
    ? `${REVIEW_HISTORY_INTRO}\n${annotatedReviewHistory.map(e => {
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
  const smallScopeEarly = iteration === 1 && taskScope === 'small' && !isScopeOverflow;
  const iterationPreamble = buildReviewIterationPreamble(
    iteration, reviewMaxIterations, reviewMidBudget, smallScopeEarly,
  );

  // Rollup PRs (head branch research/* or plan/*) contain N subtask squash-merge
  // commits whose titles end in "(#NNNN)". The `(#NNNN)` marker means a PR was
  // squash-merged — NOT that it landed on origin/main. Subtask PRs merge INTO
  // the rollup branch. We've seen reviewers misread these as "already-on-main"
  // and flag perfectly valid rollup PRs as empty, looping the fix agent on
  // nothing. Make the verification steps explicit.
  const rollupGuidance = buildRollupReviewGuidance(branch, baseBranch);

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
  const reviewPrompt = `${reviewClaudeMdPrefix}${reviewPlaybookSection}${reviewProjectCtxSection}${reviewHistorySection}${fixResponsesSection}${buildReviewMasterPreamble(prNumber, branch, baseBranch)}

${iterationPreamble}
${rollupGuidance}
${originalPrompt ? `The original task was: ${originalPrompt}\n\n` : ''}${prTitle ? `PR title: ${prTitle}\n` : ''}${prBody ? `PR description:\n${prBody}\n\n` : ''}Files changed in this PR:
${changedFiles}${diffShortstat ? `\nDiff volume: ${diffShortstat}\n` : ''}

${selfReviewBlindSpotsSection}
${plannedFilesSection}

${buildReviewDiffAccessHint(baseBranch)}
${reviewScopeBudgetSection}${scopePreamble}${taskTypeFocusHint}${buildReviewCoreGuidance(reviewBuildConfig, taskId)}`;

  const launchedReviewPrompt = reviewPrompt + `\n\n${DEFAULT_TOOL_GUIDANCE}`;
  const promptFingerprint = computeReviewPromptFingerprint(reviewBuildConfig, {
    branch,
    baseBranch,
    includePlannedFilesClassificationGuidance: plannedFiles.length > 0 && plannedFilesSection.includes('Files changed but NOT in the original plan:'),
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
  try {
    if (fs.existsSync(`/tmp/review_verdict_${taskId}.json`)) {
      const verdictJson = fs.readFileSync(`/tmp/review_verdict_${taskId}.json`, 'utf-8').trim();
      // Validate it's actual verdict JSON
      const parsed = JSON.parse(verdictJson) as Record<string, unknown>;
      if (parsed.verdict) {
        // Inject investigated_files alongside the agent-emitted verdict
        // fields, then re-serialize. Capped to keep verdict_json under
        // schemas.StatusUpdate.verdict_json max_length=10000.
        if (investigatedFiles.length > 0 && !parsed.investigated_files) {
          parsed.investigated_files = investigatedFiles.slice(0, 25);
        }
        const augmented = JSON.stringify(parsed);
        verdictJsonField = augmented;
        // Also append to logs for backwards compatibility
        const reviewText = sanitize(lastResultText || allOutput.slice(-50_000));
        logs = reviewText + '\n\n' + augmented;
        sendLog(ws, `Verdict read from file: ${typeof parsed.verdict === 'string' ? parsed.verdict : JSON.stringify(parsed.verdict)}\n`);
      }
    }
  } catch { /* fall through to output parsing */ }

  if (!logs) {
    logs = sanitize(lastResultText || allOutput.slice(-50_000));
  }

  // Surface the PR's current changed-file list to the orchestrator so the
  // next fix iteration's drift detector has a fresh snapshot. Without this,
  // drift falls back to the original create task's _pr_changed_files (which
  // is stale once a rebase-fix or new commits land), leaving drift_in/drift_out
  // empty for exactly the cases we need them to fire (PR #3435-style rebase
  // damage). Cap at 50 to match AgentStatusPayload.changed_files limit.
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
  const baseBranch = validateBranchName(strVal(payload.base_branch, 'main'), 'base_branch');
  const prompt = strVal(payload.prompt);
  const prNumber = Number(payload.pr_number);
  if (!Number.isInteger(prNumber) || prNumber <= 0) throw new Error(`Invalid pr_number: ${String(payload.pr_number)}`);
  const originalPrompt = strVal(payload.original_prompt);
  const fixOrigTaskType = strVal(payload.original_task_type || '');

  if (!prompt) throw new Error('Fix task missing prompt');

  // Clone the existing PR branch (don't create a new one)
  statusRef.value = `Cloning ${owner}/${repo} (fixing PR #${prNumber})...`;
  sendLog(ws, `Fixing PR #${prNumber} on branch ${branch}\n`);
  const cloneUrl = `https://x-access-token:${ghToken}@github.com/${owner}/${repo}.git`;
  // Shallow clone of the PR branch directly — avoids fetch+checkout issues with --single-branch
  await prepareWorkspaceForRole({
    workspace: WORKSPACE, repoUrl: cloneUrl, githubToken: ghToken,
    branch, baseBranch, depth: DEFAULT_CLONE_DEPTH,
  });
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
  const fixReason = reviewLoop?.fix_reason as string | undefined;
  const taskContext = (task.context as Record<string, unknown> | undefined) || {};
  const ciFailureLogs = [
    strVal(taskContext.ci_failure_logs),
    strVal(taskContext._ci_failure_logs),
    strVal(taskContext._test_failure_summary),
    fixReason === 'ci_failure' ? prompt : '',
  ].map(s => s.trim()).filter(Boolean).join('\n\n');
  const ciFailureSelectors = fixReason === 'ci_failure'
    ? extractPytestSelectorsFromOutput(ciFailureLogs)
    : [];
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
  // Trajectory + drift sections — render before the existing "Previous review
  // iterations" block so the agent reads the iteration arc (verdicts, files
  // per iteration, drift) before diving into per-iteration issue lists.
  const trajectorySection = formatTrajectorySection(reviewHistory);
  const driftSignal = (reviewLoop?.drift_signal as DriftSignal | undefined);
  const driftSection = formatDriftSection(driftSignal);
  let basePrompt = originalPromptSection + trajectorySection + driftSection + iterationHeader + prompt;
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
    basePrompt = `${originalPromptSection}${trajectorySection}${driftSection}${iterationHeader}Previous review iterations (issues marked "(likely resolved ...)" were not re-flagged in the following iteration — treat as historical context, focus on the current feedback):\n${historyLines}\n\nCurrent review feedback to address:\n${prompt}`;
  }

  // Surface self-review blind-spots (truncated_files_read, incomplete_review,
  // reviewer_findings, and author-flagged blind_spots) propagated from the
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
    const prFiles = git(['diff', '--name-only', `origin/${baseBranch}...HEAD`])
      .split('\n').map(s => s.trim()).filter(Boolean);
    const baseFiles = git(['ls-tree', '-r', '--name-only', `origin/${baseBranch}`, ALEMBIC_VERSIONS_DIR])
      .split('\n').map(s => s.trim()).filter(Boolean);
    const migrationConflict = detectMigrationConflict(prFiles, baseFiles);
    if (migrationConflict.detected) {
      const guidance = buildMigrationConflictGuidance(migrationConflict);
      enrichedPrompt = `${guidance}${enrichedPrompt}`;
      sendLog(ws, `Migration revision conflict detected: PR=${migrationConflict.prMigrations.join(',')} base-new=${migrationConflict.baseNewMigrations.join(',')}\n`);
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
  enrichedPrompt = `${enrichedPrompt}${fixFirstTaskSection}${CODE_CHANGE_HELPER_GUIDANCE}${fixSrFocusBlock}${PLAYBOOK_GUIDANCE}\nNote: If the review feedback revealed a recurring pattern or non-obvious codebase rule, save it to the playbook.\n${assembleFailureGuidanceBlocks()}\n\n${DEFAULT_TOOL_GUIDANCE}`;

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
    "issue_id": "<sha1 of the issue's normalized description (lowercased, whitespace-collapsed)>",
    "issue_summary": "<first 80 chars of the issue text>",
    "source_iteration": <int — which review iteration raised this issue>,
    "response_type": "disputed" | "partial" | "alternative",
    "claim": "<1–3 sentences: what you did, or why the issue is wrong>",
    "verify": [
      "<concrete check the reviewer can run>",
      "<another concrete check>"
    ]
  }

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
      if (repro.failureSummary) {
        enrichedPrompt =
          `## Local targeted CI repro\n` +
          `The CI-failing pytest selector(s) were reproduced locally before edits. Use this focused output as the primary failure evidence:\n\n` +
          `${repro.failureSummary.slice(-5000)}\n\n` +
          enrichedPrompt;
      } else if (repro.passed) {
        enrichedPrompt =
          `## Local targeted CI repro\n` +
          `The CI-failing pytest selector(s) passed locally before edits. Re-check whether the CI failure is stale, environment-specific, or covered by another failing check before changing code.\n\n` +
          enrichedPrompt;
      }
    } else {
      sendLog(ws, 'No pytest selectors could be extracted from CI failure logs — skipping targeted repro and relying on CI context plus impact tests.\n');
    }
  }

  // Run Claude with the review feedback as the prompt
  const promptFingerprint = computePromptFingerprint('fix', fixBuildConfig);
  statusRef.value = 'Addressing review feedback...';
  setCurrentPromptFingerprint(promptFingerprint);
  // Emit once before the fix session starts so fast fix/rebase tasks still
  // persist the stable code-change fingerprint on the backend.
  sendHeartbeatSync(taskId, statusRef.value, { prompt_fingerprint: promptFingerprint });
  const fixBaseCommit = git(['rev-parse', 'HEAD']);
  // 2026-05-04: post-Claude lint/test/push pipeline replaced with
  // runCodeChangeTask. Claude calls ./check + ./pr open (which pushes
  // updates to the EXISTING PR via existingPrNumber) + ./done from
  // inside its own session. See agent/src/handlers/code-change.ts.
  await runCodeChangeTask({
    taskId,
    prompt: enrichedPrompt,
    ws,
    statusRef,
    workBranch: branch,
    baseBranch,
    owner,
    repo,
    existingPrNumber: prNumber,
    testsOnMainBranch: baseBranch === 'main',
    baseCommit: fixBaseCommit,
    model,
    effort,
    disallowedTools: disallowedToolsFix,
    noChangesMessage: 'No file changes after review-fix iteration.',
    buildConfig: fixBuildConfig,
  });

  // Fresh reviewer pass: read-only second session to catch issues the fix
  // agent missed. Enabled per-project via project_metadata.enable_fresh_reviewer.
  const fixProjectMeta = task.project_metadata as Record<string, unknown> | undefined;
  const freshReviewerEnabled =
    fixProjectMeta?.enable_fresh_reviewer === true;
  if (freshReviewerEnabled) {
    const fixTaskTitle = strVal(task.title) || originalPrompt.slice(0, 80) || 'Fix task';
    await runFreshReviewerPass(
      taskId,
      fixTaskTitle,
      originalPrompt || prompt,
      fixRelatedFiles,
      ws,
      statusRef,
      model,
      task.project_context as Record<string, unknown> | undefined,
    );
  }
}
