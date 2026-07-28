import { createHash } from 'crypto';
import type { BuildConfig } from '../index.js';
import {
  PARALLEL_REVIEW_GUIDANCE, DEFAULT_TOOL_GUIDANCE, READ_ONLY_BASH_WRITE_NOTE,
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

export const REVIEW_HISTORY_INTRO = 'Issues from previous iterations (verify each was addressed — issues marked "(likely resolved ...)" were not re-flagged in the following iteration and are historical context, not active blockers):';
export const REVIEW_ITERATION_PRIOR_NOTE = '\nPrevious iterations have already addressed some issues. Focus on what remains unresolved.';

// Delta-focus guidance for iteration ≥2. Round 1 keeps the full-PR-diff,
// full-rigor review (this fragment is NOT emitted then). From round 2 onward the
// reviewer would otherwise re-scan the entire PR every round and re-derive the
// same already-handed-off issues — "verification-only" drift that produces
// another REQUEST_CHANGES with no new code defect. This fragment scopes later
// rounds to the changes the fix agent just made plus the unresolved set, without
// lowering the bar: a genuinely-unresolved prior blocker or a NEW blocker the
// delta introduced still blocks merge.
export const REVIEW_DELTA_FOCUS_NOTE = (
  `\nDelta-focus (iteration ≥2): the prior issues were handed to a fixer. Concentrate on the changes since the last fix commit — run \`git log --oneline -5\` and \`git diff HEAD~1...HEAD\` (or the range from the last fix commit to HEAD) to see what the fixer just touched. Verify each prior blocker is resolved in that delta, and only raise a NEW blocker if the delta introduced one OR a prior blocker is genuinely unresolved. Do NOT re-litigate already-addressed issues, out-of-scope concerns, or pre-existing code the fixer did not touch. The full-PR re-scan already happened in iteration 1; repeating it every round burns the iteration budget without finding new defects.`
);

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
  const deltaFocus = iteration > 1 ? REVIEW_DELTA_FOCUS_NOTE : '';
  return `This is review iteration ${iteration} of ${maxIters} in the automated review-fix cycle.${priorNote}${deltaFocus}${budgetHint}${smallHint}`;
}

export const REVIEW_QA_FOCUS_HINT = 'This is a test-only change. The Test Coverage section is less relevant (this IS the test). Focus on whether tests are meaningful and correctly assert behavior.\n\n';
export const REVIEW_MIGRATE_FOCUS_HINT = 'This is a database migration. Focus on: reversibility (downgrade path), data preservation, and index impact on existing queries.\n\n';

export const REVIEW_SMALL_SCOPE_PREAMBLE = 'This is a small-scope change. Focus your review on Correctness & Logic. Security and performance sections are lower priority unless the change touches auth, user input handling, or database queries.\n\n';
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
// CI — a separate, authoritative gate — already runs the project's build,
// type-check, eslint, and the full test suite; merge requires `ci_passed AND
// review_approved` as independent inputs, so the reviewer re-running build/tests
// would be redundant with CI and would block PRs that CI already proved green
// whenever the reviewer's sandbox can't run them. The reviewer's retained,
// reliably-runnable verification is scoped lint on the changed files (the one
// check CI does not run), framed as an optional code-quality signal — never an
// aggregate build gate. Scoped-lint one-liners are emitted via the shared
// buildScopedLintOneliner helper so they restrict to changed files only.
const CI_AUTHORITY_PREAMBLE = `CI is the authoritative gate for this project's build, type-check, eslint, and the full test suite. Merge requires CI to pass independently of your review, so do NOT re-run the build, type-check, or tests yourself, and do NOT issue REQUEST_CHANGES because one failed or because you could not run one — CI owns that signal.`;

export function buildTypeCheckInstructions(buildConfig?: BuildConfig, baseBranch?: string): string {
  if (!buildConfig || !buildConfig.directories || buildConfig.directories.length === 0) {
    return `**Build, Tests & Lint — who verifies what:**
- ${CI_AUTHORITY_PREAMBLE}
- The ONE quality check CI does not run is the project's lint. As an OPTIONAL code-quality signal you MAY run the lint scoped to the changed files (Python: \`python -m ruff check <changed .py files>\`; JS/TS: \`npx eslint <changed files>\`) and surface only NET-NEW style/lint violations as code-quality issues. This is a scoped quality check, not a build gate — skip it freely if your sandbox can't run it.
- You MAY run targeted commands in your already-cloned workspace to SUBSTANTIATE a specific concern (reproduce a suspected bug, confirm a claim). Use the result to inform your judgment.
- A command you CANNOT run — tool missing, install/build failure, API 401/403, sandbox limit — is an environment condition, never a code defect. It is NOT grounds for REQUEST_CHANGES.`;
  }
  const base = baseBranch ?? 'main';
  // Reviewer-runnable checks = the project's LINT, scoped to the changed files
  // only. CI already runs the build/type-check/tests as the authoritative merge
  // gate, so we deliberately do NOT emit build_cmd invocations here — re-running
  // the aggregate build would be redundant with CI and would block green PRs
  // whenever the reviewer's sandbox can't run it. A directory configured with
  // only a build_cmd (no lint_cmd) contributes no reviewer check.
  const checks = buildConfig.directories
    .filter(d => d.lint_cmd)
    .map(d => {
      const installPrefix = d.type === 'node'
        ? 'npm ci && '
        : d.install_cmd ? `${d.install_cmd} && ` : '';
      // Delegate scoping to the shared helper so it stays in sync with the
      // ./check fix path (buildScopedLintOneliner in prompt.ts).
      const lintCmd = d.lint_cmd ?? '';
      const scopedOneliner = buildScopedLintOneliner(lintCmd, d.path, base, 'git-diff');
      if (scopedOneliner) {
        const kind = detectLinterKind(lintCmd);
        const fileDesc = kind === 'eslint' ? 'JS/TS' : '.py';
        return `- For changes in \`${d.path}/\`: run \`cd ${d.path} && ${scopedOneliner}\` — report only NET-NEW issues (scoped to changed ${fileDesc} files)`;
      }
      return `- For changes in \`${d.path}/\`: run \`cd ${d.path} && ${installPrefix}${lintCmd}\` — report only NET-NEW issues`;
    });
  if (checks.length === 0) {
    return `**Build, Tests & Lint — who verifies what:**
- ${CI_AUTHORITY_PREAMBLE}
- No reviewer-runnable lint is configured for this project, so there is no scoped quality check to run; review the code manually for correctness.
- A command you CANNOT run (tool missing, install/build failure, API 401/403, sandbox limit) is an environment condition, never a code defect, and is NOT grounds for REQUEST_CHANGES.`;
  }
  return `**Build, Tests & Lint — who verifies what:**
- ${CI_AUTHORITY_PREAMBLE}
- The ONE quality check CI does not run is the project's lint. As an OPTIONAL code-quality signal you MAY run the scoped lint below (already restricted to the changed files) and surface only NET-NEW style/lint violations as code-quality issues. This is a scoped quality check, not a build gate — skip it freely if your sandbox can't run it.
${checks.join('\n')}
- You MAY also run targeted commands in your already-cloned workspace to SUBSTANTIATE a specific concern (reproduce a suspected bug, confirm a claim) — use the result to inform your judgment.
- A command you CANNOT run (tool missing, install/build failure, API 401/403, sandbox limit) is an environment condition, never a code defect, and is NOT grounds for REQUEST_CHANGES.`;
}

// Reviewer criterion for catching unjustified default-off/observe/fail-open hedges.
// Exported so the self-review test file can assert the reviewer checks the same patterns
// the agent self-review focus blocks name.
export const REVIEW_ANTI_HEDGE_CRITERION =
  '- Does the diff introduce a new flag, gate, or mode that defaults to `observe`, ' +
  'disabled, or `fail-open` without documented rationale? ' +
  'New behavior must default ON/committed — flag unjustified hedges as a correctness issue.';

const LENS_CHECKLIST_ITEMS: Record<string, string> = {
  correctness: `**Correctness & Logic:**
- Does the code correctly implement what the original task asked for?
- Are there edge cases, null/undefined handling, or off-by-one errors?
- Are async operations properly awaited? Are errors properly handled?
- Do any database queries have N+1 problems or missing indexes?
${REVIEW_ANTI_HEDGE_CRITERION}`,
  security: `**Security:**
- Are user inputs validated and sanitized before use?
- Are there SQL injection, XSS, path traversal, or command injection risks?
- Are secrets/tokens exposed in logs, responses, or error messages?
- Are authorization checks present on new endpoints?`,
  code_quality: `**Code Quality:**
- Is the code readable and following existing patterns in the codebase?
- Are there obvious performance issues (unnecessary loops, redundant DB calls)?
- Does the change break any existing interfaces or contracts?
- Are type annotations correct (TypeScript/Python)?
- **Anti-hedging**: Does this diff introduce a new default-off flag, observe mode, or fail-open behavior WITHOUT an explicit written justification (a code comment, PR description note, or inline rationale)? If yes, flag as MAJOR — new features must ship enabled by default; unjustified observe/off/fail-open patterns are forbidden.`,
  test_coverage: `**Test Coverage:**
- Are there tests for the new/changed behavior? (A genuinely missing test for new behavior is a blockable defect.)
- Do the changes look likely to break existing tests by reasoning about the diff? (CI runs the full suite as the authoritative gate — do not re-run it yourself or block on a test run you couldn't perform; flag only a specific case the diff clearly breaks.)`,
};

export function buildReviewChecklistSection(buildConfig?: BuildConfig, baseBranch?: string): string {
  return `Review the changes thoroughly using this checklist:

${LENS_CHECKLIST_ITEMS.correctness}

${LENS_CHECKLIST_ITEMS.security}

${LENS_CHECKLIST_ITEMS.code_quality}

${LENS_CHECKLIST_ITEMS.test_coverage}

${buildTypeCheckInstructions(buildConfig, baseBranch)}`;
}


export function buildReviewSeverityGuideSection(): string {
  return `**Severity Classification Guide:**
Use these definitions when assigning severity to issues. Severity affects how the review appears in GitHub (critical = failure annotation, major = warning, minor = notice).

- **CRITICAL** — Issues that could cause security vulnerabilities, data loss, crashes, or correctness bugs in production. Examples:
  - SQL/command injection from unsanitized user input passed directly to a query or shell
  - Missing auth check on a new endpoint that exposes sensitive data
  - Off-by-one in a loop that silently skips or double-processes records in a DB migration

- **MAJOR** — Logic errors, missing error handling, race conditions, missing tests for new behavior, violations of the codebase's established patterns, or hedging that leaves new behavior dark. Examples:
  - Async function whose rejection is silently swallowed (missing try/catch or .catch())
  - New API endpoint with no test coverage at all
  - New code that bypasses an existing invariant (e.g., writing to a field that has a single designated writer elsewhere in the codebase)
  - New feature hidden behind a default-off flag, fail-open fallback, or observe-mode wrapper when the task called for the feature to be on

- **MINOR** — Style inconsistencies, naming suggestions, documentation gaps, or optional improvements that do not affect correctness. Examples:
  - Variable named \`data\` where \`userRecord\` would be clearer
  - Missing docstring on a public function
  - Redundant else after a return statement`;
}

export function buildReviewVerdictInstructions(taskId: string, opts?: { repairContext?: boolean }): string {
  const opening = opts?.repairContext
    ? `Your review analysis is complete above. Based on that analysis, output EXACTLY ONE of the following JSON blocks as the last line of your response. There are only two valid verdicts — no third option exists.`
    : `After your review, output EXACTLY ONE of the following JSON blocks as the last line of your response. There are only two valid verdicts — no third option exists.`;
  return `${opening}

**APPROVE** — the change is ready to merge as-is. Use this when the code is correct, the tests cover the behavior, and nothing blocks shipping. Optional polish notes belong in \`feedback\` as non-blocking observations; they do NOT disqualify APPROVE.
{"verdict": "APPROVE", "feedback": "What you verified and why it's ready to merge. Include any non-blocking polish notes here."}

**REQUEST_CHANGES** — block only on a concrete, substantiated CODE defect: a logic/correctness bug, a security hole, a missing test for new behavior, a broken invariant, a design flaw, or a merge conflict in the diff. Build/type-check/test pass is CI's job, not yours — do not block on it. Uncertainty rooted in something you could NOT verify in your sandbox (a command you couldn't run, a tool that was missing, an API that 401'd) is NOT grounds to block: judge the code on its merits and trust CI for build/test pass. If your concern is a real, substantiated defect, list it; if it is only a "couldn't confirm", do not REQUEST_CHANGES.
{"verdict": "REQUEST_CHANGES", "feedback": "What needs fixing and why it blocks merge.", "issues": [{"severity": "critical|major|minor", "description": "...", "file": "path/to/file.py", "line": 42}], "requires_rereview": true}

Every REQUEST_CHANGES verdict MUST include the boolean \`requires_rereview\`. Explicitly choose:
- \`true\` — an independent follow-up review is required after the fixer responds. Use this whenever the correctness of the resulting repair still needs independent judgment.
- \`false\` — you are comfortable delegating closure of this specific repair to the fixer's self-check and the normal CI/merge gates. This is not mechanical proof that the repair is correct; it is your judgment that an independent follow-up review is unnecessary.

\`APPROVE\` ignores \`requires_rereview\`; omit it from APPROVE verdicts.

**Exhaustiveness — list every blocking change this PR needs, not just the most prominent one.** When you choose REQUEST_CHANGES, the \`issues\` array must enumerate every blocker you can identify in this pass. The fix agent will address each item; if you surface them one-per-iteration, the PR burns N review-fix cycles for what could have converged in one. Concretely:
- Before finalizing your verdict, re-scan the diff and the touched call sites for additional blockers in adjacent files / sister call sites / invariants the touched functions used to enforce. A single conceptual change often spans multiple files — flag every place that needs to change, not just the first one you noticed.
- If the cited issue is one instance of a pattern (e.g., a missing check at one call site), list every other call site that needs the same fix.
- It is far better to over-list (the fix agent can dedupe) than to surface a new MAJOR each iteration. The goal is convergence, not paced disclosure.

Do NOT emit "COMMENT", "NEUTRAL", "ADVISORY", or any other verdict — they are not accepted and will be normalized to REQUEST_CHANGES. There is no informational-only path: if the change is acceptable → APPROVE (put observations in \`feedback\`); if anything needs changing → REQUEST_CHANGES.

For each issue in the \`issues\` array:
- \`file\`: repo-relative path to the file where the issue is located (optional — omit if not tied to a specific file)
- \`line\`: line number in the file (optional — omit if not applicable)
- Issues without \`file\`/\`line\` are still valid and will be included in the review body summary

IMPORTANT: Also write your verdict JSON to /tmp/review_verdict_${taskId}.json ${READ_ONLY_BASH_WRITE_NOTE} (NOT in the repository). The file should contain ONLY the JSON object, no markdown wrapping.`;
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
