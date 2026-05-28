import { createHash } from 'crypto';
import type { BuildConfig } from '../index.js';
import {
  PARALLEL_REVIEW_GUIDANCE, DEFAULT_TOOL_GUIDANCE,
  buildScopedLintOneliner, detectLinterKind,
} from '../prompt.js';

export type ReviewPromptFingerprintOptions = {
  branch?: string;
  baseBranch?: string;
  includePlannedFilesClassificationGuidance?: boolean;
  // When the parent's enable_parallel_review flag is on AND the helper says
  // fan-out is worth it, handleReview injects PARALLEL_REVIEW_GUIDANCE. The
  // fingerprint MUST move so reviewer telemetry can A/B the two modes — same
  // fingerprint for both would silently mix parallel and serial outcomes.
  includeParallelReviewGuidance?: boolean;
};

// ── Static review-guidance fragments (shared with prompt fingerprint) ────────
// These constants and helpers are consumed by both `handleReview()`'s launched
// prompt and `buildReviewPromptFingerprintContent()`. Edits to any fragment
// move the fingerprint, so review telemetry tracks real prompt changes and
// cannot silently drift while claiming the same version.
export const REVIEW_CLAUDEMD_PROMPT_HEADER = '## Codebase Instructions\n';
export const REVIEW_CLAUDEMD_PROMPT_FOOTER = '\n\nCheck that the changes respect the File Restrictions listed above. Changes to restricted files should result in REQUEST_CHANGES.\n\n';
export const REVIEW_FILE_RESTRICTIONS_NOTE = 'Check that the changes respect the File Restrictions listed above. Changes to restricted files should result in REQUEST_CHANGES.';

export const REVIEW_HISTORY_INTRO = 'Issues from previous iterations (verify each was addressed — issues marked "(likely resolved ...)" were not re-flagged in the following iteration and are historical context, not active blockers):';
export const REVIEW_ITERATION_PRIOR_NOTE = '\nPrevious iterations have already addressed some issues. Focus on what remains unresolved.';

function buildReviewBudgetMidHint(maxIters: number): string {
  return `\nBudget-awareness: you are at or past 50% of the ${maxIters}-iteration budget. Do NOT flag MINOR-severity issues — every iteration now costs a meaningful fraction of remaining budget and cosmetic nits are not worth a round trip. Focus only on CRITICAL and MAJOR findings. If only MINOR issues remain, APPROVE and note them as non-blocking polish in \`feedback\`.`;
}
export const REVIEW_SMALL_SCOPE_EARLY_HINT = `\nSmall-scope early-iteration guidance: MINOR-severity issues (cosmetic nits, naming preferences, non-critical style) are NON-BLOCKING for small-scope changes. Do NOT include them in the \`issues\` array — mention them in \`feedback\` as optional polish. Only CRITICAL and MAJOR issues should drive a REQUEST_CHANGES verdict. A small-scope PR with only MINOR findings should APPROVE.`;

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

export const REVIEW_QA_FOCUS_HINT = 'This is a test-only change. The Test Coverage section is less relevant (this IS the test). Focus on whether tests are meaningful and correctly assert behavior.\n\n';
export const REVIEW_MIGRATE_FOCUS_HINT = 'This is a database migration. Focus on: reversibility (downgrade path), data preservation, and index impact on existing queries.\n\n';

export const REVIEW_SMALL_SCOPE_PREAMBLE = 'This is a small-scope change. Focus your review on Correctness & Logic and Build & Type Safety. Security and performance sections are lower priority unless the change touches auth, user input handling, or database queries.\n\n';
export const REVIEW_LARGE_SCOPE_PREAMBLE = 'This is a large-scope change with higher risk. Review ALL checklist sections thoroughly. Pay special attention to backwards compatibility, data model safety, and security implications.\n\n';

export function buildReviewScopeOverflowPreamble(taskScope: string, changedFileCount: number, scopeThreshold: number): string {
  return `NOTE: This PR has ${changedFileCount} changed files, which exceeds the typical range for a ${taskScope}-scope change (threshold: ${scopeThreshold}). Treat this as a higher-risk change and review ALL checklist sections thoroughly — Security, Code Quality, and Test Coverage are NOT deprioritized here regardless of the original ${taskScope}-scope label.\n\n`;
}

export function buildReviewScopeBudgetSection(nonTest: number, budget: number, pct: number): string {
  return (
    `## Scope Budget\n` +
    `This PR touches ${nonTest} of ${budget} allowed production files (${pct}% utilized). ` +
    `When requesting changes, consider whether the fix would require touching additional files that could push the PR over its scope budget. ` +
    `Prefer feedback that can be addressed within existing files.\n\n`
  );
}

export const REVIEW_FIX_RESPONSES_HEADER = '## Responses from prior fix iteration\n\n';
export function buildReviewFixResponsesIntro(n: number): string {
  return (
    `The fix agent recorded structured rationale for ${n} issue(s) it left unaddressed. ` +
    `Treat these as CLAIMS TO VERIFY, never as conclusions to accept.\n\n`
  );
}
export const REVIEW_FIX_RESPONSES_INSTRUCTIONS = (
  `For each disputed/alternative/partial response below:\n` +
  `  1. Run every step in \`verify steps\`. Quote the actual command output in your feedback.\n` +
  `  2. If all verify steps confirm the claim → DROP the original issue (do not re-raise).\n` +
  `  3. If any verify step fails or is non-actionable ("trust me", "I tested it") → re-raise the original issue with severity major AND quote the specific verify step that failed so the next fix iteration knows what to address.\n` +
  `  4. If you cannot run a verify step (e.g. it requires data you don't have) → flag as "unverifiable" with severity major; do not silently accept.\n\n` +
  `**Sycophancy guard**: a plausible-sounding claim with no verify steps, or with verify steps that boil down to "the agent says so", MUST be rejected. Real disagreements have concrete checks.\n\n`
);

export function buildReviewMasterPreamble(prNumber: number | string, branch: string, baseBranch: string): string {
  return `You are reviewing PR #${prNumber} on this repository. The PR branch is "${branch}" targeting "${baseBranch}".`;
}
export function buildReviewDiffAccessHint(baseBranch: string): string {
  return `You have full access to the codebase. Read any files you need to understand the changes in context. Use \`git diff origin/${baseBranch}...HEAD\` to see the full diff, or \`git diff origin/${baseBranch}...HEAD -- <file>\` for specific files.`;
}

// ── Build instruction generator for review prompts ───────────────────────────
// Build commands are emitted with `npm ci &&` (or equivalent install step)
// prefixed for node-typed directories so the reviewer's cold-clone workspace
// always has dependencies available — without the prefix, every reviewer on a
// fresh VM hits "cannot resolve <package>" and flags it as a build-gate
// failure even when the underlying PR is fine.
export function buildTypeCheckInstructions(buildConfig?: BuildConfig, baseBranch?: string): string {
  if (!buildConfig || !buildConfig.directories || buildConfig.directories.length === 0) {
    return `**Build & Type Safety (MUST run these — do not skip):**
- For TypeScript/JavaScript changes: find the package root for each changed file (the nearest parent directory with a package.json), then run \`npm ci && npm run build\` (or \`npx tsc --noEmit\` for TypeScript-only packages) — report any errors
- For Python changes: run \`python -m ruff check <changed_files>\` or \`python -m py_compile <changed_files>\` to verify syntax/style — report any errors
- For Go changes: run \`go build ./...\` — report any errors; for Rust: run \`cargo check\`
- Only run checks for the subsystems touched by the changed files. Do not invent additional build invocations.
- For ANY code changes: run the relevant command above and include results in your verdict. A build failure is an automatic REQUEST_CHANGES — but FIRST verify your workspace is fully installed: a missing-package error like "cannot resolve <foo>" is almost always a stale workspace, not a real defect. Re-run with the install step prefix above before flagging.`;
  }
  const base = baseBranch ?? 'main';
  const checks = buildConfig.directories
    .filter(d => d.build_cmd || d.lint_cmd)
    .map(d => {
      const installPrefix = d.type === 'node'
        ? 'npm ci && '
        : d.install_cmd ? `${d.install_cmd} && ` : '';
      if (d.build_cmd) {
        return `- For changes in \`${d.path}/\`: run \`cd ${d.path} && ${installPrefix}${d.build_cmd}\` — report any errors`;
      }
      // lint_cmd only: delegate to shared helper so scoping logic stays in sync
      // with the ./check fix path (buildScopedLintOneliner in prompt.ts).
      const lintCmd = d.lint_cmd ?? '';
      const scopedOneliner = buildScopedLintOneliner(lintCmd, d.path, base, 'git-diff');
      if (scopedOneliner) {
        const kind = detectLinterKind(lintCmd);
        const fileDesc = kind === 'eslint' ? 'JS/TS' : '.py';
        return `- For changes in \`${d.path}/\`: run \`cd ${d.path} && ${scopedOneliner}\` — report any errors (scoped to changed ${fileDesc} files)`;
      }
      return `- For changes in \`${d.path}/\`: run \`cd ${d.path} && ${installPrefix}${lintCmd}\` — report any errors`;
    });
  if (checks.length === 0) {
    return '**Build & Type Safety:** No build commands configured — review code manually for type correctness.';
  }
  return `**Build & Type Safety (MUST run these — do not skip):**
${checks.join('\n')}
- Only run the build commands listed above. Do not invent additional build invocations against directories without configured commands.
- For ANY code changes: run the relevant command above and include results in your verdict. A build failure is an automatic REQUEST_CHANGES — but FIRST verify your workspace is fully installed: a missing-package error like "cannot resolve <foo>" is almost always a stale workspace, not a real defect.`;
}

export function buildReviewChecklistSection(buildConfig?: BuildConfig, baseBranch?: string): string {
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

${buildTypeCheckInstructions(buildConfig, baseBranch)}`;
}

export function buildReviewSeverityGuideSection(): string {
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

export function buildReviewVerdictInstructions(taskId: string): string {
  return `After your review, output EXACTLY ONE of the following JSON blocks as the last line of your response. There are only two valid verdicts — no third option exists.

**APPROVE** — the change is ready to merge as-is. Use this when the code is correct, the tests cover the behavior, and nothing blocks shipping. Optional polish notes belong in \`feedback\` as non-blocking observations; they do NOT disqualify APPROVE.
{"verdict": "APPROVE", "feedback": "What you verified and why it's ready to merge. Include any non-blocking polish notes here."}

**REQUEST_CHANGES** — something actionable must change before this merges. Use this for any bug, missing test, broken invariant, merge conflict, failing CI signal, or behavior that would surprise a reader. If you are uncertain between APPROVE and REQUEST_CHANGES, pick REQUEST_CHANGES — another round is cheap; a missed regression is expensive.
{"verdict": "REQUEST_CHANGES", "feedback": "What needs fixing and why it blocks merge.", "issues": [{"severity": "critical|major|minor", "description": "...", "file": "path/to/file.py", "line": 42}]}

**Exhaustiveness — list every blocking change this PR needs, not just the most prominent one.** When you choose REQUEST_CHANGES, the \`issues\` array must enumerate every blocker you can identify in this pass. The fix agent will address each item; if you surface them one-per-iteration, the PR burns N review-fix cycles for what could have converged in one. Concretely:
- Before finalizing your verdict, re-scan the diff and the touched call sites for additional blockers in adjacent files / sister call sites / invariants the touched functions used to enforce. A single conceptual change often spans multiple files — flag every place that needs to change, not just the first one you noticed.
- If the cited issue is one instance of a pattern (e.g., a missing check at one call site), list every other call site that needs the same fix.
- It is far better to over-list (the fix agent can dedupe) than to surface a new MAJOR each iteration. The goal is convergence, not paced disclosure.

Do NOT emit "COMMENT", "NEUTRAL", "ADVISORY", or any other verdict — they are not accepted and will be normalized to REQUEST_CHANGES. There is no informational-only path: if the change is acceptable → APPROVE (put observations in \`feedback\`); if anything needs changing → REQUEST_CHANGES.

For each issue in the \`issues\` array:
- \`file\`: repo-relative path to the file where the issue is located (optional — omit if not tied to a specific file)
- \`line\`: line number in the file (optional — omit if not applicable)
- Issues without \`file\`/\`line\` are still valid and will be included in the review body summary

IMPORTANT: Also write your verdict JSON to /tmp/review_verdict_${taskId}.json (NOT in the repository). The file should contain ONLY the JSON object, no markdown wrapping.`;
}

export function buildRollupReviewGuidance(
  branch: string,
  baseBranch: string,
  stagingPrefixes: string[] = ['research/', 'plan/'],
): string {
  const isRollupHead = stagingPrefixes.some(p => branch.startsWith(p));
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

export function buildPlannedFilesClassificationGuidance(): string {
  return `Classify each unplanned non-test file before flagging. Apply skepticism — do not assume a stated reason is correct, but do not flag categories that are valid even when unstated:
  - scope-creep: file has no causal link to anything in the plan or prior reviews. → MAJOR.
  - caller-impact: a planned file's API change forced consumer updates here. Verify the consumer reference exists (grep the planned file for the new/changed API and confirm this file uses it). → not a violation.
  - discovered-dependency: planned change required a missing import / helper / flag-flip that lives here. Verify by reading the planned file and confirming the dependency. → not a violation.
  - cluster-boundary-correction: file logically belongs in this cluster but was misallocated to a sibling task. Verify the PR description explicitly explains the correction. → not a violation IF acknowledged in PR description; flag MAJOR if not acknowledged.
If unsure on edge cases, do not flag — let the operator decide.`;
}

export function buildReviewCoreGuidance(buildConfig: BuildConfig | undefined, taskId: string, baseBranch?: string): string {
  return `${buildReviewChecklistSection(buildConfig, baseBranch)}

${buildReviewSeverityGuideSection()}

${buildReviewVerdictInstructions(taskId)}`;
}

export function buildReviewPromptFingerprintContent(
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
  if (options.includeParallelReviewGuidance) {
    sections.push(PARALLEL_REVIEW_GUIDANCE);
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
