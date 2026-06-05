import fs from 'fs';
import { createHash } from 'crypto';
import { execFileSync } from 'child_process';
import { WORKSPACE } from './index.js';
import type { BuildConfig } from './index.js';
import { isReadOnlyTaskType } from './taskCapabilities.js';
import { parseBackendTimestamp } from './utils/time.js';

// ── Re-exports from sub-modules (facade) ────────────────────────────────────
// The bulky helpers have been split out into prompts/* modules so that small
// prompt edits don't keep reshaping a 2k-line file (which causes painful merge
// conflicts in the self-improving pipeline). Callers continue to import from
// './prompt.js' unchanged.
export {
  buildFileTree,
  countSourceFiles,
  detectGreenfield,
  isGreenfieldRepo,
  verifyCloneDelivered,
  extractTestConventions,
  discoverRelatedTests,
  extractModuleInterfaces,
  discoverAffectedConsumers,
} from './prompts/discovery.js';
export { formatProjectContext } from './prompts/project-context.js';
export {
  buildAnalysisPrompt,
  parseAnalysisPlanFiles,
  parseAnalysisPlanTestFiles,
  parseAnalysisPlanVerifyFiles,
} from './prompts/analysis.js';
export {
  formatSelfReviewBlindSpots,
  formatProjectBlindSpotSummary,
} from './prompts/self-review.js';
export {
  buildContextPrefix,
  formatStrategicIntent,
  formatSelfReviewCalibrationSection,
  synthesizeSuccessPatterns,
  synthesizeFailureHotspots,
} from './prompts/context-prefix.js';
export {
  MIGRATE_TASK_FOCUS,
  REFACTOR_TASK_FOCUS,
  QA_TASK_FOCUS,
  CREATE_TASK_FOCUS,
  FIX_TASK_FOCUS,
  DOCS_TASK_FOCUS,
  DEPS_TASK_FOCUS,
  getTaskTypeFocusBlock,
} from './prompts/task-type-focus.js';
export {
  AGENT_HELPER_NAMES,
  writeCodeChangeHelpers,
  detectLinterKind,
  buildScopedLintOneliner,
} from './prompts/code-change-helpers.js';
import { excludeAgentHelpers, isBlueprintSelfRepo } from './prompts/code-change-helpers.js';
export { excludeAgentHelpers };

// ── Agent Permission Enforcement ────────────────────────────────────────
// Write .claude/settings.json in the workspace to restrict what the inner
// Claude Code agent can do.  Each task type gets appropriate permissions.

// Write types get no restrictions (default behavior). Listed for documentation:
// create, fix, refactor, docs, qa, deps, migrate

/**
 * Configure Claude Code permissions for the current task type.
 * Returns a list of disallowed tools to pass to runClaudeCode's --disallowedTools flag.
 * Also installs git hooks to block commit/push for read-only agents.
 * Call this AFTER cloning the repo but BEFORE running Claude Code.
 */
export function configureAgentPermissions(taskType: string): string[] {
  if (isReadOnlyTaskType(taskType)) {
    // Strip GitHub token from git remote so the agent can't push
    try {
      execFileSync('git', ['remote', 'set-url', 'origin', 'https://github.com/readonly/readonly.git'], { cwd: WORKSPACE });
    } catch { /* git dir may not exist yet */ }

    // Block git commit via pre-commit hook (backup enforcement)
    const hooksDir = `${WORKSPACE}/.git/hooks`;
    try {
      fs.mkdirSync(hooksDir, { recursive: true });
      const blockScript = '#!/bin/sh\necho "ERROR: This is a read-only analysis task. Do NOT commit or push." >&2\nexit 1\n';
      fs.writeFileSync(`${hooksDir}/pre-commit`, blockScript, { mode: 0o755 });
      fs.writeFileSync(`${hooksDir}/pre-push`, blockScript, { mode: 0o755 });
    } catch { /* hooks dir may not exist before clone completes */ }

    return ['Write', 'Edit', 'NotebookEdit'];
  }
  // Write types: no restrictions
  return [];
}

export function strVal(v: unknown, fallback = ''): string {
  return typeof v === 'string' ? v : fallback;
}

// Render an ISO date/datetime as a compact relative time suffix ("3h ago",
// "2d ago", "1w ago"). Returns '' when the input can't be parsed.
export function relativeTimeFromIso(iso: string, now: Date = new Date()): string {
  if (!iso) return '';
  const then = parseBackendTimestamp(iso);
  if (!then) return '';
  const diffMs = now.getTime() - then.getTime();
  if (diffMs < 0) return 'just now';
  const minutes = Math.floor(diffMs / 60000);
  if (minutes < 1) return 'just now';
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days < 7) return `${days}d ago`;
  const weeks = Math.floor(days / 7);
  return `${weeks}w ago`;
}

// ── Task Type Hints (appended to prompts for known task types) ───────────
export const TASK_TYPE_HINTS: Record<string, string> = {
  'qa': 'Focus on writing tests. Run the existing test suite first to understand patterns. Do not modify production code unless fixing a test setup issue.',
  'migrate': 'This is a database migration task. Check existing Alembic migrations in backend/alembic/versions/ for naming conventions. Use `alembic revision --autogenerate` as a starting point only — always review and clean up generated migrations.',
  'security_audit': 'Focus on identifying security vulnerabilities. Do not implement fixes — create a detailed findings report as your output.',
  'self_audit': 'Score system maturity on four signals: test_coverage, friction_density, claim_consistency, failure_mix. Each component is 0-100. Pick recommended_mode (audit|fundamentals|ship|pause) from the worst component. Do not modify any files — output is a JSON findings report only.',
  'release_audit': 'You are running on the LTS / promotion branch and reviewing the incoming diff against that branch\'s standards. Run `git diff <diff_range>` to obtain the diff. Check for test erosion (deleted test files, net assertion/test-count delta). Score findings into audit_score [0,1]. Do not modify any files — output is a JSON release audit report only.',
  'perf': 'Profile before optimizing. Identify the specific bottleneck with measurements before writing any fix.',
  'create': 'Before finishing: edit the files you need to change, run `./check` to validate lint/build, fix any failures, then `./pr open --title "feat/fix/etc: ..." --body "Summary of changes"` to ship and `./done` to mark complete. The `./pr` helper gates on `./check` so broken code can\'t reach a PR. If admin-ui viewport bootstrapping only fails because Chromium deps are missing, `./check` will retry in the Playwright container when Docker is available and otherwise warn without blocking the task. If a library is unfamiliar, fetch its docs first. If you determine the task can\'t be completed (scope wrong, conflicting requirements, blocker found), call `./fail "<reason>"` instead of leaving silently.',
  'fix': 'You are fixing issues from a code review or a pipeline failure. Read ALL feedback carefully and address every point. IMPORTANT: the spawn prompt can be stale — between when this task was queued and when you run, a concurrent rollup may have resolved conflicts, or a new commit may have broken something new. If the prompt includes a "stale-premise notice", trust the latest state. After fixing, run `./check` to verify, then `./pr open --title "fix: ..." --body "..."` to push (it will update the existing PR if there is one), then `./done`. Use `./fail "<reason>"` only if you can\'t reasonably resolve the feedback.',
  'docs': 'Read the existing README and documentation files first. Understand the current documentation structure before making changes.',
  'deps': 'Check for breaking changes in major version bumps. Run the full test suite after updating to verify compatibility.',
  'refactor': 'Run the full test suite BEFORE and AFTER changes via `./check test`. Zero test regressions allowed. Do not change external behavior. After refactoring + verifying via `./check`, run `./pr open --title "refactor: ..." --body "..."` and `./done`. Use `./fail` if the refactor proves not feasible.',
  'investigate': 'This is a read-only investigation. Do NOT modify any files. Search for known issues related to error patterns you discover. Write your findings to the output JSON only.',
  'debug': 'Search for known issues related to error patterns you discover. Use WebSearch to look up specific error messages or stack traces when local context is insufficient.',
};

// Task types that don't get generic TASK_TYPE_HINTS entries because they assemble
// bespoke prompts in their handlers and don't append the generic hint dict. This
// allowlist prevents regressions: new dispatch entries must either add a hint to
// TASK_TYPE_HINTS or be explicitly opted-out here with justification.
export const TASK_TYPES_WITHOUT_HINT: ReadonlySet<string> = new Set([
  'review',           // Uses review-fix.ts bespoke prompt assembly
  'panel_review',     // Uses panel-review.ts bespoke fixed-lens review assembly
  'plan',             // Uses research-plan.ts bespoke prompt assembly
  'research',         // Uses research-plan.ts bespoke prompt assembly
  'strategic_review', // Reuses research handler with research_mode branch; uses bespoke assembly
  'smoketest',        // Uses specialized.ts bespoke prompt assembly
  'design_doc_update', // Uses design-doc.ts bespoke prompt assembly
  'memo',             // Uses memo.ts bespoke prompt assembly (no-clone reasoning artifact)
  'verify_tests',     // Uses verify-tests.ts; runs the detected suite directly, no Claude prompt
  'compute_experiment', // Uses compute-experiment.ts; launches+polls a compute job, no Claude prompt
  'web_research',     // Uses web-research.ts bespoke prompt assembly (web-first knowledge doc)
  'design',           // Uses design.ts bespoke prompt assembly (codebase-grounded design doc)
]);

// ── Dynamic Task Type Hints (based on project build configuration) ───────────
export function getTaskTypeHints(buildConfig?: BuildConfig, promotionTarget?: string): Record<string, string> {
  const dirs = buildConfig?.directories ?? [];
  if (dirs.length === 0 && !promotionTarget) return TASK_TYPE_HINTS;

  const pythonDirs = dirs.filter(d => d.type === 'python');
  const nodeDirs = dirs.filter(d => d.type === 'node');
  const hints: Record<string, string> = { ...TASK_TYPE_HINTS };

  // 'migrate' hint: detect migration framework from directory types
  const pyDir = pythonDirs.length > 0 ? pythonDirs[0].path.replace(/\/$/, '') : null;
  const nodeDir = nodeDirs.length > 0 ? nodeDirs[0].path.replace(/\/$/, '') : null;
  if (pyDir) {
    hints['migrate'] = `This is a database migration task. Check existing Alembic migrations in ${pyDir}/alembic/versions/ for naming conventions. Use \`alembic revision --autogenerate\` as a starting point only — always review and clean up generated migrations.`;
  } else if (nodeDir) {
    hints['migrate'] = `This is a database migration task. Check existing migration files in ${nodeDir}/ for naming conventions. For Prisma: use \`prisma migrate dev\` to generate migrations — always review generated output.`;
  }

  // 'create'/'refactor' hints: reference actual build/test commands
  const testCmds: string[] = [];
  for (const dir of dirs) {
    if (dir.test_cmd) testCmds.push(`${dir.path}: \`${dir.test_cmd}\``);
    else if (dir.build_cmd) testCmds.push(`${dir.path}: \`${dir.build_cmd}\``);
  }
  if (testCmds.length > 0) {
    const cmdsText = testCmds.join(', ');
    hints['create'] = `Before finishing: edit the files you need, run \`./check\` to validate lint/build (which uses ${cmdsText} under the hood), fix any failures, then \`./pr open --title "feat/fix/etc: ..." --body "Summary"\` and \`./done\`. The \`./pr\` helper gates on \`./check\`. If admin-ui viewport bootstrapping only fails because Chromium deps are missing, \`./check\` will retry in the Playwright container when Docker is available and otherwise warn without blocking the task. Use \`./fail "<reason>"\` if the task can't be completed.`;
    hints['refactor'] = `Use \`./check test\` to run the full test suite BEFORE and AFTER changes (${cmdsText}). Zero test regressions allowed. After refactoring + verifying, \`./pr open --title "refactor: ..." --body "..."\` then \`./done\`. \`./fail\` if not feasible.`;
  }

  // 'deps' hint: list actual package managers detected
  const pkgManagers: string[] = [];
  for (const dir of dirs) {
    if (dir.type === 'python') pkgManagers.push('pip/poetry');
    else if (dir.type === 'node') pkgManagers.push('npm/yarn');
    else if (dir.type === 'go') pkgManagers.push('go mod');
    else if (dir.type === 'rust') pkgManagers.push('cargo');
  }
  if (pkgManagers.length > 0) {
    hints['deps'] = `Check for breaking changes in major version bumps. This project uses ${pkgManagers.join(', ')}. Run the full test suite after updating to verify compatibility.`;
  }

  if (promotionTarget) {
    const lts = promotionTarget;
    hints['release_audit'] = `You are running on the LTS branch (${lts}) and reviewing the incoming diff against ${lts}'s standards. Run \`git diff <diff_range>\` to obtain the diff. Check for test erosion (deleted test files, net assertion/test-count delta). Score findings into audit_score [0,1]. Do not modify any files — output is a JSON release audit report only.`;
  }

  return hints;
}

export const DEFAULT_TOOL_GUIDANCE = 'Tool guidance: Use Grep/Glob for code search (not Bash grep/find), Read for file contents, Agent tool for parallel exploration of multiple areas. Use TodoWrite to track multi-step work. Use WebFetch to look up API documentation, library references, or framework guides when working with unfamiliar code. Use WebSearch to find solutions for specific error messages or unusual patterns. If you discover bugs, architectural issues, or improvement ideas, use ./note to leave annotations: ./note comment "msg" --page <page>, ./note issue "msg" --severity high, ./note vision "msg". If you hit a barrier, have a question for the project owner, or want to flag something important, use ./messages send \'your message\' (types: NOTE, QUESTION, FEEDBACK, BUG_REPORT, BLOCKER).';

// Adaptive parallel-subagent guidance for rollup PRs touching multiple
// modules. Only inject when shouldFanOutReview() AND the per-project
// enable_parallel_review flag both allow it — the fingerprint moves when
// present so review telemetry can A/B by fingerprint. Trivial diffs MUST
// skip fan-out: the block itself reminds the agent of that even when present.
export const PARALLEL_REVIEW_GUIDANCE = `
## Parallel review fan-out (rollup PRs touching multiple modules)

This PR touches multiple module groups (build_config directories). To keep wall-clock close to the slowest module review instead of the sum, fan out the per-file checklist work (Correctness, Security, Code Quality, Test Coverage) using parallel investigation subagents. Self-check before fanning out: if the diff is actually trivial (a few files in one module) skip fan-out and review serially — subagent overhead dominates on small diffs.

Fan-out plan:
1. Group the changed files by build_config directory (one group per module).
2. Spawn one investigation subagent per module group with the Task/Agent tool. Each subagent's prompt: read the diff for its assigned files only, apply the per-file checklist (Correctness, Security, Code Quality, Test Coverage), and return a short structured list of findings (severity, file, line, description). Keep each subagent prompt small — pass only the file list and the checklist, not the whole PR context.
3. After all subagents return, summarize each finding to a single line before recompositing into your working set. This protects context budget.
4. Run your OWN cross-module coherence pass over the recomposited findings AND the diff: shared types, interface contracts, naming consistency, layering, cross-file invariants. Subagents cannot see across module boundaries — this pass is mandatory and is yours alone.
5. Emit the SINGLE verdict JSON yourself. Subagents are investigation-only — no subagent writes /tmp/review_verdict_*, makes edits, or emits a verdict.

Fail-soft: if context is tight after recompositing subagent output, drop fan-out and finish serially. A complete serial review beats an incomplete parallel one.
`;

// Adaptive parallel-subagent guidance for fix iterations on rollups touching
// multiple modules or carrying many reviewer issues. Only inject when
// shouldFanOutFix() AND the per-project enable_parallel_fix flag both allow
// it. Atomicity contract: ONLY the parent agent may Write/Edit/commit —
// subagents return investigation reports only.
export const PARALLEL_FIX_GUIDANCE = `
## Parallel fix investigation (rollup PRs with multi-module diffs or many reviewer issues)

This fix iteration has enough scope that parallel investigation subagents can reduce wall-clock. Use the Task/Agent tool to fan out root-cause investigation. **Read the atomicity contract below before delegating anything.**

**ATOMICITY CONTRACT — non-negotiable:**
Only the parent agent (you) may Write, Edit, NotebookEdit, or run Bash (git/check/pr) commands that change files. Subagents return investigation reports ONLY. A subagent that writes or edits files is a bug — remind yourself of this before spawning each one.

**Slicing axes — choose the one that fits:**
- **Module axis**: one subagent per build_config directory group. Each subagent reads the diff for its assigned files, identifies root causes for the reviewer's cited issues in those files, and returns: file, line, proposed fix location, estimated risk, suggested verify steps.
- **Issue-cluster axis**: when reviewer issues are high (≥6) and span ≥2 files, group related issues into clusters and assign one subagent per cluster. Each subagent returns the same investigation report shape.
Pick the axis that maps more naturally onto the actual issues — you may use a mix.

**Fan-out plan:**
1. Before spawning, summarize the review feedback to a compact form — fix prompts are large. Pass only the relevant issue subset and file list to each subagent, not the whole review history.
2. Spawn subagents in parallel using the Task/Agent tool. Each subagent prompt: "You are an investigation-only subagent. Read these files and these reviewer issues. Return a structured report: {file, line, root_cause, proposed_fix_location, risk, verify_steps}. Do NOT write or edit any files."
3. After all reports return, summarize each finding to a single line before merging. This protects your context budget.
4. Merge and deduplicate: if two subagents propose overlapping fixes, resolve the conflict yourself (pick the more conservative one or ask yourself which is correct). Do NOT apply conflicting fixes from two subagents — that is when serial fallback is safer.
5. Batch-read all affected files ONCE before applying any edits. Then apply changes sequentially yourself.
6. Run your own cross-module coherence pass after edits: shared types, interface contracts, naming consistency. Subagents cannot see across module boundaries — this is yours alone.

**Fail-soft:** If subagents disagree on root cause or their reports conflict, fall back to serial investigation for the conflicting files rather than guessing. Incomplete parallel fix is worse than complete serial fix.

**Self-check before delegating each slice:** "Am I asking this subagent to apply the fix?" If yes, revise the prompt to ask for investigation only.
`;

// Adaptive parallel-subagent guidance for read-only audit/investigation
// handlers. Inject when the per-project enable_parallel_review flag is set.
// Output schema is unchanged — only investigation strategy changes.
export const PARALLEL_AUDIT_GUIDANCE = `
## Parallel audit fan-out (investigation-only handlers)

These handlers never Edit or Write source files — atomicity is automatic. Fan out investigation work using parallel subagents when the scope spans multiple concern axes (e.g. one subagent per OWASP category for security audits, one per system for self-audits, one per file-cluster for investigate/debug/perf/release_audit).

**Fan-out plan:**
1. Identify 2-4 natural concern axes for the current task (OWASP category, system boundary, file cluster, failure mode, etc.).
2. Spawn one investigation subagent per axis using the Task/Agent tool. Each subagent's prompt: read its assigned files/scope only, return a short structured findings list (severity, location, description). Keep each prompt small — pass only the relevant scope, not the full task context.
3. After all subagents return, summarize each finding to a single line before merging. This protects context budget.
4. Run your OWN coherence pass over the recomposited findings: deduplication, consistent severity assignment, cross-axis invariants that individual subagents cannot see.
5. Emit the SINGLE output JSON yourself (the file written to /tmp/...). Subagents are investigation-only — no subagent writes the output file or emits a findings list as the final result.

**Output schema unchanged:** only HOW the investigation is done changes, not WHAT is emitted. The final /tmp/ output file must match the handler's documented schema exactly.

**Self-check:** if the task scope is narrow (one file, one system, one failure), skip fan-out — subagent overhead dominates on small scopes. Fan out only when parallelism meaningfully reduces wall-clock.

Fail-soft: if context is tight after recompositing subagent output, drop fan-out and finish serially. A complete serial audit beats an incomplete parallel one.
`;

// Generalized override of the trained "lint failed → refactor source"
// reflex for project-specific assertion messages that encode policy. The
// 2026-05-03 dogfood run lost a full strategic cycle because investigation
// agents read the size_breach assertion message (which TELLS the agent to
// bump the JSON), correctly diagnosed the size-cap as root cause, but
// prescribed an extract-helpers refactor anyway because Claude's training
// bias dominated the assertion's literal instructions. The same pattern
// will recur on any project-specific convention encoded in assertion text:
// alembic migration patterns, release-relock pattern, playbook system, etc.
export const READ_THE_FAILURE_MESSAGE = `
## Read The Failure Message Literally

When a test, lint, or build check fails, **read the full assertion or
error message before deciding what to change**. Project-specific
assertion messages often encode operational policy that overrides the
generic "fix the source" reflex.

If the failure message includes a remediation procedure — e.g.
\`POST /api/agent/...\`, \`edit file X\`, \`follow the Y protocol\`,
\`bump the limit in <config>\`, \`run script Z\` — **follow it literally**.
The procedure is what the project owner wants you to do; ignoring it to
"fix the source" creates churn and burns retry attempts.

Examples of remediation procedures encoded in assertions:
- Size guardrails (\`module_size_limits.json\`): bump the limit, do NOT refactor
- Migration conflicts: chain a new migration, do NOT modify existing ones
- Playbook patterns: use \`./playbook set\` to record discoveries
- Schema revision drift: bump \`EXPECTED_SCHEMA_REVISION\` to match alembic head

Default reflex is "fix the source." Override that reflex when the
assertion is telling you to fix the test config, the migration chain,
or any other piece of project infrastructure.
`;

export const PRIOR_ATTEMPT_DIVERGENCE = `
## If You're Retrying — Try A Different Angle

If your task context indicates this is a fix iteration or a remediation
of a previously-failed attempt, **the same approach won't work twice**.

Before writing code: ask yourself what the previous attempt assumed
about the root cause. If it tried "edit source X to make test Y pass"
and failed, consider:
- Maybe Y is meant to fail unless the test config is bumped (size guardrail, etc.)
- Maybe X isn't the right file — the test is actually validating Z
- Maybe the failure is in a place the previous attempt never read

Common wrong-direction patterns to avoid on retry:
1. **Re-running the same lint fix that already failed** — output won't change
2. **Adding more code when the previous attempt's lint failure was "file too long"** — the answer is bump the limit, not condense the diff
3. **Refactoring the source when the test is asking you to update test config** — read the assertion message, follow its remediation literally
4. **Making the test pass by weakening assertions** — that's not allowed; either the source needs to be different or the test/policy needs to change

The diagnosis system fingerprints failures and stores them as
\`ResidualRemediationCase\`. If your task includes prior_diagnosis or
diagnosis_root_cause in its context, that is your hint about what
**previously didn't work** — pivot away from that, do not repeat it.
`;

// Per-task SIZE-specific guidance — kept distinct from READ_THE_FAILURE_MESSAGE
// because it includes an explicit step-by-step procedure.
export const SIZE_GUARDRAIL_GUIDANCE = `
## Size Guardrails Are Advisory — Bump, Don't Refactor

If a test fails with a message containing \`module_size_limits.json\` or
\`size guardrail\`, the correct fix is **almost always** to bump the limit
in that JSON file — NOT to refactor the source. The assertion message
itself walks you through the bump procedure; follow it literally.

The fast local gate runs the guardrail too: \`./check\` invokes
\`python3 backend/testing/check_size_limits.py --sections <touched stacks>\`,
so a breach now surfaces BEFORE you open a PR — not only in CI.

This covers BOTH size guardrails:
- a breach of an EXISTING cap — \`has <N> lines (limit: <M>)\` /
  \`(cap: <M>)\`; and
- the complementary coverage failure for a NEW oversized file that has
  no cap at all — \`has <N> lines (threshold: 1500)\` / \`NO cap entry\`
  (\`test_*_no_uncapped_large_file\`). The fix is to ADD a cap entry, NOT
  to extract or split the file.

Specifically:
1. Read the failing assertion (or the \`./check\` "size guardrail" failure).
   It names the file path, current line count, and either the cap it
   exceeded or the threshold it crossed with no cap entry.
2. Run the single deterministic fix command from the repo root:
   \`python3 backend/testing/check_size_limits.py --fix\`
   This rewrites ONLY the breached caps AND adds a cap entry for any
   uncapped over-threshold file in
   \`backend/testing/module_size_limits.json\` using the SAME canonical bump
   policy the server's size-breach endpoint applies
   (\`max(current+200, current*1.15)\` rounded up to 50). Do NOT hand-edit
   the cap to an ad-hoc number, and do NOT refactor — the one command keeps
   it policy-consistent so the guardrail still catches runaway growth.
3. Commit both the source change AND the regenerated JSON in the same PR.

**Do NOT** refactor or extract code unless your task explicitly says to.
Refactoring during a create/fix task is a strategic decision that belongs
in its own PR; doing it inline causes import cycles, breaks unrelated
tests, and burns retry attempts.

The only time you should NOT bump is when the diff is genuinely bundling
unrelated code that should be split — e.g. you accidentally edited
something outside the task's scope. In that case, shrink the diff to only
what the task asked for.
`;

// Encourage agents to surface (not absorb) latent debt they discover beyond
// their task scope. Without this, agents that notice "actually there are 300
// pre-existing lint errors here" tend to either (a) stuff their head in the
// sand and ignore the signal, or (b) try to fix all of them inline, blow the
// retry budget on side-quests, and fail the original task too. The right
// move is: report what was found via ./messages, finish in-scope work, let
// strategic agent decide whether to spawn a dedicated cleanup task.
export const BEYOND_SCOPE_REPORTING = `
## When You Find Bigger Problems Than Your Task

Surfacing latent debt is valuable — keep doing it. But trying to fix
everything inline often blows the retry budget and the original task
fails too. Decision rule:

1. **My task can't succeed without this fix?** (lint gate fails on
   pre-existing errors in files I'm changing; migration in another
   module conflicts with mine; function I depend on is genuinely broken)
   → Fix it inline. Strategic agent runs on a slow cycle (~30 min) and
   won't unblock you in time. Then send a NOTE so the bigger pattern is
   visible to whoever reviews.

2. **Out-of-scope and my task can ship without it?** → **Report and
   skip.** Don't side-quest. Use \`./messages send "<content>" NOTE\`
   (types: \`NOTE\` | \`BUG_REPORT\` | \`BLOCKER\`). Include: file paths,
   rough count, effort estimate, whether anything blocks you.

3. **Incidental, in files I'm already touching, <50 LOC?** → Just fix it.

**Heroics that fail are worse than focused work that ships.** A
300-error sweep in a task scoped for 50 LOC times out the VM and leaves
both the feature and the cleanup unshipped. Report-and-skip lets the
feature land AND makes the debt visible for proper prioritization.
`;

// ── Edges-considered structured self-review field ────────────────────────────
// Exported type used by sibling-cluster code (handlers, backend schemas) to
// stay consistent with the prompt-defined shape.
export interface EdgeConsidered {
  case: string;
  handled: boolean;
  evidence?: string;
  blind_spot?: string;
  origin?: 'author' | 'reviewer';
}

// Self-review prompt block requesting enumerated edge-case analysis.
// Included in code-change task prompts so the self-review JSON captures
// which paths the agent thought through (and which it did not).
export const EDGES_CONSIDERED_GUIDANCE = `
## Edges-Considered Field in Self-Review

When writing your self-review JSON (\`/tmp/self_review_<task_id>.json\`),
include an optional \`edges_considered\` array. Each entry names one specific
edge case the diff touches and declares whether it is handled:

\`\`\`json
"edges_considered": [
  {"case": "empty input list", "handled": true, "evidence": "early return at L42"},
  {"case": "concurrent calls to same key", "handled": false, "blind_spot": "no lock — relying on caller serialization"},
  {"case": "null/undefined payload field", "handled": true, "evidence": "optional chaining at L87"}
]
\`\`\`

Guidelines:
- **medium/large scope tasks**: enumerate at least 3 edges. Small tasks may have fewer.
- \`evidence\`: cite file + line where the edge is handled (e.g. \`"early return at L42"\`).
- \`blind_spot\`: when \`handled: false\`, explain why it is deferred or acceptable.
- Unhandled edges (\`handled: false\`) are advisory — they do NOT fail the self-review,
  but they feed the catch-rate dashboard and downstream reviewer focus.
- Omitting the field entirely is acceptable; it is never required.
`;

// Canonical concatenation of the cross-cutting failure-guidance blocks
// shared by code-change handlers (standard.ts, review-fix.ts fix path).
// Centralized so block order, inclusion, and future additions only need
// to change in one place.
export function assembleFailureGuidanceBlocks(): string {
  return READ_THE_FAILURE_MESSAGE + SIZE_GUARDRAIL_GUIDANCE + PRIOR_ATTEMPT_DIVERGENCE + BEYOND_SCOPE_REPORTING;
}

// Compute a stable 12-hex-char fingerprint of the static prompt guidance for
// a given task type. Used to correlate production task outcomes back to prompt
// revisions cycle-over-cycle. Only hashes static/deterministic content —
// task IDs, file paths, and payload-specific text are intentionally excluded.
export function computePromptFingerprint(
  taskType: string,
  buildConfig?: BuildConfig,
  options?: { includeParallelFixGuidance?: boolean; promotionTarget?: string; codeChangeGuidanceVariant?: string },
): string {
  const hints = getTaskTypeHints(buildConfig, options?.promotionTarget);
  const taskHint = hints[taskType] ?? '';
  const sections = [taskHint, DEFAULT_TOOL_GUIDANCE, assembleFailureGuidanceBlocks()];
  if (options?.includeParallelFixGuidance) sections.push(PARALLEL_FIX_GUIDANCE);
  // The code-change lifecycle/helper guidance now varies by project (self-repo
  // vs. build_config-driven vs. generic no-config). Fold the chosen variant into
  // the fingerprint so telemetry diverges when the emitted ./check prose diverges
  // — without it, two tasks with identical task hints but different guidance would
  // collide. Optional + appended-only, so existing fingerprints are unchanged.
  if (options?.codeChangeGuidanceVariant) {
    sections.push(`code-change-guidance:${options.codeChangeGuidanceVariant}`);
  }
  return createHash('sha256').update(sections.join('\n\n')).digest('hex').slice(0, 12);
}

// Within-task fix-iteration history. Each retry attempt of the within-VM
// lint/test fix loop currently spawns a fresh Claude subprocess with no
// memory of what prior iterations tried. When the first attempt picks the
// wrong remediation (e.g. refactor source instead of bumping the size cap
// JSON), the next iterations repeat it and the task fails after exhausting
// the retry budget on the same wrong choice. This history mechanism gives
// the next iteration's prompt visibility into what already didn't work.
export interface FixAttempt {
  attemptNumber: number;
  diffStat: string;
  errorTail: string;
}

const FIX_HISTORY_MAX_CHARS = 2000;
const FIX_HISTORY_ERROR_TAIL_CHARS = 400;

export function formatFixIterationHistory(attempts: FixAttempt[]): string {
  if (attempts.length === 0) return '';

  const renderEntry = (a: FixAttempt): string => {
    const diff = a.diffStat.trim() || '(no file changes detected)';
    const tail = a.errorTail.trim().slice(0, FIX_HISTORY_ERROR_TAIL_CHARS);
    return `### Fix iteration ${a.attemptNumber}\nChanges made (cumulative from task start):\n${diff}\n\nFailure that persisted after this iteration:\n${tail}`;
  };

  const header = `## Previous Fix Attempts\n\nEach iteration below tried to fix the same failure but the failure persisted (or shifted). Don't repeat the same approach — if it didn't work, the root cause is somewhere else. Read the failure message literally; project-specific assertions often encode the remediation procedure.\n`;

  // Drop oldest attempts until total fits in the budget — the most recent
  // attempts are the most informative for "don't repeat what just failed".
  let kept = [...attempts];
  while (kept.length > 0) {
    const body = kept.map(renderEntry).join('\n\n');
    const out = `${header}\n${body}\n`;
    if (out.length <= FIX_HISTORY_MAX_CHARS) return out;
    kept = kept.slice(1);
  }
  return '';
}

// ── Playbook helpers ─────────────────────────────────────────────────────────

// Neutralize the four characters that retain meaning inside a double-quoted
// bash string — backslash, double-quote, backtick, and dollar — so an id baked
// into `TASK="<value>"` is a literal assignment even if it contains `$(...)`,
// backticks, or quote-breakout attempts. ids are server-generated, so this is
// defense-in-depth, not a primary trust boundary. The character class escapes
// the backslash too, and `$&` re-emits the matched char prefixed with one
// backslash, so each special char is neutralized exactly once.
export function shellEscapeDoubleQuoted(value: string): string {
  return value.replace(/[\\"`$]/g, '\\$&');
}

export function writePlaybookHelper(taskIdRaw: string, workspace: string = WORKSPACE): void {
  const taskId = shellEscapeDoubleQuoted(taskIdRaw);
  const helperScript = `#!/bin/bash
OP="\${1:?Usage: playbook get|set|list [key] [content]}"
KEY="$2"
VALUE="$3"
API="\${ORCHESTRATOR_URL:?ORCHESTRATOR_URL not set}"
TOKEN="\${AGENT_API_TOKEN:?AGENT_API_TOKEN not set}"
TASK="${taskId}"

if [ "$OP" = "get" ]; then
  printf '{"task_id":"%s","op":"get"}' "$TASK" | \\
    curl -f -sS --max-time 30 -X POST "$API/api/agent/playbook" \\
      -H "Content-Type: application/json" -H "X-Agent-Token: $TOKEN" --data-binary @-
elif [ "$OP" = "set" ]; then
  [ -z "$KEY" ] || [ -z "$VALUE" ] && { echo "Error: set requires <key> and <content>" >&2; exit 1; }
  # Capture branch/HEAD provenance at write time so the backend can stamp it on
  # the entry. Absent (non-git workspace) => omitted => backend keeps it legacy.
  BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || true)
  HEAD_SHA=$(git rev-parse --short HEAD 2>/dev/null || true)
  jq -n --arg tid "$TASK" --arg k "$KEY" --arg v "$VALUE" --arg br "$BRANCH" --arg sha "$HEAD_SHA" \\
    '{task_id: $tid, op: "set", key: $k, value: $v}
       + (if $br == "" then {} else {branch: $br} end)
       + (if $sha == "" then {} else {head_sha: $sha} end)' | \\
    curl -f -sS --max-time 30 -X POST "$API/api/agent/playbook" \\
      -H "Content-Type: application/json" -H "X-Agent-Token: $TOKEN" --data-binary @-
elif [ "$OP" = "list" ]; then
  printf '{"task_id":"%s","op":"list"}' "$TASK" | \\
    curl -f -sS --max-time 30 -X POST "$API/api/agent/playbook" \\
      -H "Content-Type: application/json" -H "X-Agent-Token: $TOKEN" --data-binary @-
else
  echo "Error: unsupported playbook operation: $OP" >&2
  exit 1
fi
echo ""
`;
  fs.writeFileSync(`${workspace}/playbook`, helperScript, { mode: 0o755 });
  excludeAgentHelpers();
}

export function writeMessagesHelper(taskIdRaw: string, workspace: string = WORKSPACE): void {
  const taskId = shellEscapeDoubleQuoted(taskIdRaw);
  const helperScript = `#!/bin/bash
# Message tool — send messages to the project owner or resolve open messages
# Usage:
#   ./messages send '<content>'                    # send a NOTE message
#   ./messages send '<content>' QUESTION           # send with specific type (NOTE|QUESTION|FEEDBACK|BUG_REPORT|BLOCKER)
#   ./messages resolve <message-id> '<resolution>' # resolve an open message

OP="\${1:?Usage: messages send|resolve ...}"
API="\${ORCHESTRATOR_URL:?ORCHESTRATOR_URL not set}"
TOKEN="\${AGENT_API_TOKEN:?AGENT_API_TOKEN not set}"
TASK="${taskId}"
CURL_OPTS="-f -sS --max-time 30 --connect-timeout 10"

if [ "$OP" = "send" ]; then
  CONTENT="\${2:?send requires '<content>'}"
  MSG_TYPE="\${3:-NOTE}"
  jq -n --arg tid "$TASK" --arg t "$MSG_TYPE" --arg c "$CONTENT" \\
    '{task_id: $tid, op: "create_message", data: {type: $t, content: $c}}' | \\
    curl $CURL_OPTS -X POST "$API/api/agent/strategic" \\
      -H "Content-Type: application/json" -H "X-Agent-Token: $TOKEN" --data-binary @-
elif [ "$OP" = "resolve" ]; then
  MSG_ID="\${2:?resolve requires <message-id>}"
  RESOLUTION="\${3:-Addressed}"
  jq -n --arg tid "$TASK" --arg mid "$MSG_ID" --arg r "$RESOLUTION" \\
    '{task_id: $tid, op: "resolve_message", data: {message_id: $mid, resolution: $r}}' | \\
    curl $CURL_OPTS -X POST "$API/api/agent/strategic" \\
      -H "Content-Type: application/json" -H "X-Agent-Token: $TOKEN" --data-binary @-
else
  echo "Unknown op: $OP. Use send or resolve." >&2; exit 1
fi
echo ""
`;
  fs.writeFileSync(`${workspace}/messages`, helperScript, { mode: 0o755 });
  excludeAgentHelpers();
}

// ── Design-doc helper ─────────────────────────────────────────────────────────
// Single canonical generator for the ./design-doc CLI, shared by the
// design_doc_update handler (design-doc.ts) and the strategic_review handler
// (strategic.ts) so both task types get identical, durable write behavior.
//
// READ ops (list/get) are fail-fast: a single curl, no retry.
// WRITE ops (set/delete) are durable under contention: the backend serializes
// concurrent design-doc writers with a NON-BLOCKING project-row lock and
// returns HTTP 429 + Retry-After on contention (and transient 5xx under load),
// expecting the client to retry (see backend/app/task_locking.py). A single
// fail-fast curl would drop the page write silently, so set/delete retry a
// bounded number of times, honoring Retry-After, before surfacing a real error.
export function writeDesignDocHelper(taskIdRaw: string, workspace: string = WORKSPACE): void {
  const taskId = shellEscapeDoubleQuoted(taskIdRaw);
  const helperScript = `#!/bin/bash
# Design doc wiki tool — updates pages incrementally via API
# Reads ORCHESTRATOR_URL and AGENT_API_TOKEN from env (no secrets on disk)
# Usage:
#   ./design-doc get <key>                — read a section
#   ./design-doc set <key> '<json-value>' — write a section (durable: retries on 429/5xx)
#   ./design-doc delete <key>             — remove a section (durable: retries on 429/5xx)
#   ./design-doc list                     — list all section keys

OP="\${1:?Usage: design-doc get|set|delete|list <key> [json-value]}"
KEY="$2"
VALUE="$3"
API="\${ORCHESTRATOR_URL:?ORCHESTRATOR_URL not set}"
TOKEN="\${AGENT_API_TOKEN:?AGENT_API_TOKEN not set}"
TASK="${taskId}"

# Fail-fast POST for read operations (list/get): single curl, no retry.
_post_once() {
  curl -f -sS --max-time 30 --connect-timeout 10 \\
    -X POST "$API/api/agent/design-doc" \\
    -H "Content-Type: application/json" \\
    -H "X-Agent-Token: $TOKEN" \\
    --data-binary @-
}

# Durable POST for write operations (set/delete). Retries up to 3 times on
# 429 (lock contention / rate limit) and 5xx (transient), honoring the
# Retry-After header when present and falling back to linear backoff.
# Non-retryable responses (other 4xx) surface immediately.
_post_retry() {
  local body="$1"
  local attempt=0 max=3 delay http_code response dump
  while [ $attempt -lt $max ]; do
    dump=$(mktemp)
    response=$(printf '%s' "$body" | \\
      curl -sS --max-time 30 --connect-timeout 10 \\
        -X POST "$API/api/agent/design-doc" \\
        -H "Content-Type: application/json" \\
        -H "X-Agent-Token: $TOKEN" \\
        --data-binary @- \\
        -D "$dump" \\
        -w '\\n%{http_code}')
    http_code=$(printf '%s' "$response" | tail -n1)
    delay=$(grep -i '^retry-after:' "$dump" 2>/dev/null | tail -n1 | tr -dc '0-9')
    rm -f "$dump"
    if [ "$http_code" -ge 200 ] && [ "$http_code" -lt 300 ]; then
      printf '%s' "$response" | head -n -1
      return 0
    fi
    attempt=$((attempt + 1))
    if [ $attempt -ge $max ]; then break; fi
    if [ "$http_code" = "429" ] || { [ "$http_code" -ge 500 ] && [ "$http_code" -lt 600 ]; }; then
      [ -z "$delay" ] && delay=$((attempt * 2))
      echo "[RETRY $attempt/$((max - 1))] design-doc $OP failed (HTTP $http_code), retrying in \${delay}s..." >&2
      sleep "$delay"
    else
      printf '%s' "$response" | head -n -1 >&2
      echo "[ERROR] design-doc $OP failed (HTTP $http_code, non-retryable)" >&2
      return 1
    fi
  done
  echo "[ERROR] design-doc $OP failed after $max attempts" >&2
  return 1
}

if [ "$OP" = "list" ]; then
  printf '{"task_id":"%s","op":"list"}' "$TASK" | _post_once
elif [ "$OP" = "get" ]; then
  if [ -z "$KEY" ]; then
    echo "Error: get requires <key>" >&2; exit 1
  fi
  printf '{"task_id":"%s","op":"get","key":"%s"}' "$TASK" "$KEY" | _post_once
elif [ "$OP" = "set" ]; then
  if [ -z "$KEY" ] || [ -z "$VALUE" ]; then
    echo "Error: set requires <key> and <json-value>" >&2; exit 1
  fi
  _post_retry "$(printf '{"task_id":"%s","op":"set","key":"%s","value":%s}' "$TASK" "$KEY" "$VALUE")"
elif [ "$OP" = "delete" ]; then
  if [ -z "$KEY" ]; then
    echo "Error: delete requires <key>" >&2; exit 1
  fi
  _post_retry "$(printf '{"task_id":"%s","op":"delete","key":"%s"}' "$TASK" "$KEY")"
else
  echo "Unknown op: $OP. Use get, set, delete, or list." >&2; exit 1
fi
# Propagate the helper's exit status so the model sees a real failure after
# retries are exhausted (the trailing newline must not mask a non-zero rc).
rc=$?
echo ""
exit $rc
`;
  fs.writeFileSync(`${workspace}/design-doc`, helperScript, { mode: 0o755 });
  excludeAgentHelpers();
}

export interface PlaybookEntry {
  content: string;
  effectiveness_score: number;
  confirmed_count: number;
  age_days: number;
  stale: boolean;
  relevance?: {
    reasons?: string[];
    local_match?: boolean;
  };
  // Branch/HEAD the entry was authored against, stamped at write time. Absent
  // for legacy entries written before provenance capture. ``diverged`` is set
  // by the backend when the origin branch differs from the reader's
  // integration branch — drives the possibly-outdated caveat below.
  provenance?: {
    branch?: string;
    head_sha?: string;
    diverged?: boolean;
  };
}

export function formatPlaybook(
  playbook: Record<string, PlaybookEntry | string> | undefined
): string {
  if (!playbook || Object.keys(playbook).length === 0) return '';
  const lines = Object.entries(playbook)
    .map(([key, value]) => {
      let content: string;
      let caveat = '';
      if (typeof value === 'string') {
        content = value;
      } else {
        const reasons = Array.isArray(value.relevance?.reasons)
          ? value.relevance.reasons.filter((r): r is string => typeof r === 'string' && r.length > 0)
          : [];
        const relevanceLine = reasons.length > 0
          ? `> Relevance: ${reasons.join('; ')}\n\n`
          : '';
        const prov = value.provenance;
        const provenanceLine = prov && (prov.branch || prov.head_sha)
          ? `> Recorded on ${prov.branch || 'unknown branch'}${prov.head_sha ? ` @ ${prov.head_sha}` : ''}${prov.diverged ? ' (⚠ different branch than yours — code may not exist on your branch)' : ''}\n\n`
          : '';
        content = value.content;
        if (value.stale || value.provenance?.diverged || value.effectiveness_score < 0.3) {
          caveat = '> ⚠ Possibly outdated or low confidence — verify before applying.\n\n';
        }
        caveat = `${relevanceLine}${provenanceLine}${caveat}`;
      }
      return `### ${key}\n${caveat}${content}`;
    })
    .join('\n\n');
  return `## Operational Playbook (active collective memory)\n\nThese notes are the project's living shared memory — every agent reads and writes them. Treat them as a useful starting point, not gospel: if an entry contradicts what you actually observe in the code, overwrite it with \`./playbook set\`. The playbook is only as accurate as the latest agent who looked.\n\n${lines}\n\n`;
}

// 2026-05-04: appended to every code-change task prompt (create, refactor,
// docs, qa, deps, migrate, fix). Tells Claude about the four end-to-end
// helpers and the *required* exit ritual. Without this, Claude has no way
// to know the helpers exist and falls through to the wrapper's "FAILED:
// incomplete" path on every task.

// Bump this stamp whenever the self-review prompt guidance changes
// (category list, scope thresholds, schema shape, etc.) so that
// compute_sr_catch_rate_trends can distinguish prompt changes from
// genuine catch-rate movement. Kept as a string so the typed shape
// across the agent → backend (schemas.SelfReviewOutcome) → frontend
// (SrTrendData.prompt_version) chain is consistent and string-safe
// for frontend operations like `.slice(...)`.
export const SELF_REVIEW_PROMPT_VERSION = "6";

// Human-readable examples of disallowed blind_spot entries — mirrored in
// backend/app/sr_blind_spots.py DISALLOWED_BLIND_SPOT_EXAMPLES for contract-test parity.
// Includes the truncated theme form ("did not run the full backend") and a
// no-test-noun variant ("did not run the full backend suite") so the disallow
// filter covers every phrasing of this intended-design pipeline step.
export const DISALLOWED_BLIND_SPOT_EXAMPLES: readonly string[] = [
  "did not run the full backend test suite",
  "did not run the full backend",
  "did not run the full backend suite",
  "did not run docker pytest",
  "did not run viewport tests",
] as const;

// The code-change lifecycle/helper guidance is assembled from three pieces:
//   SHARED_LIFECYCLE_PREAMBLE  — repo-agnostic "## Task lifecycle" intro
//   <check-intro>              — STACK-SPECIFIC ./check + ./check test bullets
//   SHARED_LIFECYCLE_REMAINDER — repo-agnostic ./pr/./done/./fail/./blocked +
//                                the self-review schema + scope-drift /
//                                regression-risk / cross-system-contract checks
// Only the middle <check-intro> describes what `./check` runs, which is the only
// stack-specific part. buildCodeChangeHelperGuidance() picks the right intro for
// the project (self-repo vs. build_config-driven vs. generic no-config), mirroring
// the same three-way branch in prompts/check-helper.ts that generates ./check.
const SHARED_LIFECYCLE_PREAMBLE = `
## Task lifecycle — you own it end-to-end

This task ends only when YOU explicitly mark it done or failed. The wrapper
does NOT auto-commit or auto-push your work. Use these helpers (already in
the workspace, executable):
`;

// Blueprint self-repo ./check description — the exact wording shipped today,
// preserved byte-for-byte (the 8 GB / OOM / scripts/test-backend.sh paragraph).
const BLUEPRINT_CHECK_INTRO = `- \`./check\` — runs lint/build for the stacks your changes touched
  (admin-ui eslint+viewport+build, agent tsc+eslint, backend ruff). Fast. Exits
  non-zero on failure with the actual error tail; fix and re-run until
  green. If your changes don't touch admin-ui/, agent/, or backend/,
  \`./check\` returns PASS without running anything — that's expected
  for pure-docs tasks. For \`admin-ui/\` changes this includes the
  browser-mode \`npm run test:viewport\` gate, not the jsdom-only
  \`npm run test:run\` suite.
- \`./check test\` — same plus a **lightweight standalone backend test
  pass** (3 invariant files, ~30s). This VM is 8 GB; the FULL Docker
  + Postgres pytest suite OOMs here (production 2026-05-05: tasks
  53ef061b, b150b4d6, b15b6813 all died at exit 137 during this gate).
  CI runs the full suite on the PR after \`./pr open\`, so this local
  gate is just a smoke check — file-size guardrails, security regs,
  pipeline invariants. **For full suite runs use
  \`bash scripts/test-backend.sh --standalone --no-postgres\`** — calling
  \`scripts/test-backend.sh\` with no test-file argument runs the full
  suite and OOMs. Specific test files are fine with default Postgres:
  \`bash scripts/test-backend.sh testing/test_<module>.py\` fits in
  headroom (the memory guard auto-exempts single-file runs).
`;

const SHARED_LIFECYCLE_REMAINDER = `- \`./pr open --title "<title>" --body "<body>"\` — gates on \`./check\`,
  commits any pending changes, pushes to the work branch (force-with-lease
  retry built in), creates a PR (or pushes updates to an existing one for
  fix tasks) via the orchestrator endpoint. On success writes
  /tmp/pr_url_<task_id> with the URL. Pass \`--with-tests\` to run
  \`./check test\` instead of just \`./check\` as the gate. Exits non-zero
  with a clear message on failure — fix and call again.
  - **Exit 4 = PR created but conflicts with base.** \`./pr\` polls GitHub
    for mergeable status post-create; if the result is \`mergeable=false\`
    + \`mergeable_state=dirty\` (real conflict), exit 4 is returned with
    explicit resolution steps. You're still in the workspace — rebase
    onto origin/<base>, resolve the conflicts (typically via Edit or
    \`git checkout --theirs <file>\` for files outside your task's scope),
    re-run \`./check\`, then \`./pr open\` again to push the rebase. Cap
    yourself at ~2 conflict-resolution attempts; if it's not converging
    call \`./blocked\` and the orchestrator will spawn a fix-task from a
    fresh VM. Resolving locally is much cheaper than that fallback.
- \`./done\` — marks the task COMPLETED with the PR URL. Requires
  \`./pr open\` to have run first. Idempotent (safe to re-run).
  - \`./done --already-done "<reason>"\` — no-op / already-satisfied exit.
    Use ONLY when you have VERIFIED that the task's deliverable ALREADY
    EXISTS on the base branch and producing a diff would be a genuine no-op
    (or would break an invariant — e.g. a sibling PR already added the
    migration/model/endpoint you were asked to add, and a duplicate would
    branch the Alembic revision chain). Marks the task COMPLETED with NO PR
    and records your reason so operators see why there's no PR. Does NOT
    require \`./pr open\`. The reason MUST name the concrete artifact you
    verified and where it lives (≥30 chars), e.g. "0133_poolvm_registry.py +
    PoolVm model + EXPECTED_SCHEMA_REVISION=0133 already on base via sibling
    PR #6978; a new migration would break chain-linearity". This is NOT a
    give-up verb — if you couldn't complete the work use \`./fail\`; if an
    upstream precondition blocks you use \`./blocked\`. A bare exit with no
    diff and no \`./done\`/\`./fail\`/\`./blocked\` is treated as INCOMPLETE
    (it fails) — so if the work is genuinely already done you MUST call
    \`./done --already-done\` to land it COMPLETED.
  - \`./done --warn-out-of-scope "<reason>"\` — judgment-call escape hatch.
    Use when \`./check\` fails but the failures are in **files you edited**
    and are **pre-existing on the base branch (origin/<BASE>)** AND
    fixing them is materially out of scope for this PR (e.g., a refactor of an
    unrelated layout, a sweeping rename, a new dep introduction). Posts a
    FEEDBACK project message naming the gates/files/reason so strategic
    can spawn cleanup work. The reason MUST be specific (≥30 chars):
    which gates failed, which files, why the fix is out of scope.
    DO try to fix related preexisting issues first ("don't leave
    preexisting issues" principle). DO NOT use this for issues a single
    edit could resolve.
    NOTE: if the failure requires a new bug to be introduced (missing
    package, infra gap, agent can't even start), use \`./blocked\` instead.
    \`./blocked\` is for "I cannot start until X is fixed upstream";
    \`./done --warn-out-of-scope\` is for "I shipped a real change but
    pre-existing issues in touched files remain — flag for follow-up".
- \`./fail "<reason>"\` — marks the task FAILED. Use ONLY when the task
  fundamentally can't be completed: scope is wrong, conflicting
  requirements, you genuinely don't know how to make the change. Don't
  use for transient lint/test failures — keep iterating with \`./check\`
  instead.
- \`./blocked "<reason>"\` — marks the task BLOCKED_PRECONDITION (an
  INFRA class — does NOT burn iteration cap). Use when **no code edit
  on your part can fix the failure**, specifically:
    1. You made a real code change and \`./check\` still fails *identically*
       to the previous attempt. The failure isn't moving — another
       iteration won't help.
    2. The failure is at the toolchain or system-library layer (e.g.
       \`error while loading shared libraries: libatk-1.0.so.0\`,
       \`No module named 'ruff'\`, \`playwright is not installed\`,
       \`command not found: <tool>\`). No code edit can install a system
       package on the agent VM.
    3. A repo-wide gate fails on files your PR did not touch (e.g. a
       lint baseline of pre-existing errors elsewhere in the tree).
  This routes to the strategic agent — it can spawn a direction to fix
  the precondition (update the packer image, scope the gate to
  changed files, etc.) if it judges that worthwhile.
  \`./blocked\` is **not** a "give up" verb — it's a "this needs an
  upstream fix, not a code edit on this PR" verb. \`./fail\` is the
  give-up verb.

Required exit pattern (every code-change task):
  edit -> ./check -> (if green) ./pr open --title ... --body ... -> ./done

ALWAYS call \`./done\`, \`./fail\`, or \`./blocked\` explicitly. The wrapper has only one
fallback: if you opened a PR via \`./pr open\` but didn't call \`./done\`,
it posts COMPLETED on your behalf using the PR URL marker. Otherwise it
posts FAILED with no useful reason — which is bad for the operator and
bad for downstream investigation. Don't rely on the fallback.
There is NO "exit with no diff = success" fallback: if the deliverable is
already present and you produced no diff, you MUST call
\`./done --already-done "<reason>"\` — a bare no-diff exit is failed as
incomplete, never silently completed.

DO use \`git status\`, \`git diff\` to inspect your work.
DON'T directly call \`git commit\`, \`git push\`, or \`gh pr create\` — use
\`./pr open\` so the lint/build gate fires and the orchestrator records
the PR atomically.

## Required: self-review before \`./done\` (code-change tasks)

For create / fix / refactor / migrate tasks, write a structured self-review
JSON to \`/tmp/self_review_<task_id>.json\` before calling \`./done\` (your
task ID is in the ORCHESTRATOR env or the prompt context). \`./done\`
includes it in the COMPLETED payload as \`self_review_outcome\` —
downstream consumers use it for retry context, dashboard catch-rate
metrics, and reviewer-iteration priming.

Use these canonical category IDs in \`check_results\` (drawn from the
backend review vocabulary — must match exactly):
<!-- vocabulary mirrored in backend/testing/test_invariants.py as AGENT_SELF_REVIEW_CATEGORIES frozenset — keep parity with both the frozenset and backend/app/playbook_learning.py (_REVIEW_CATEGORIES) -->
<!-- SR_CATEGORIES_START -->
- \`correctness\` — logic bugs, off-by-one errors, edge cases, race conditions
- \`error-handling\` — unhandled exceptions, missing null checks, async errors
- \`security\` — injection, XSS, CSRF, exposed secrets, unvalidated inputs
- \`style-conventions\` — naming, formatting, lint, code style consistency
- \`type-safety\` — TypeScript errors, missing type annotations, unsafe casts
- \`documentation\` — missing docstrings, inline comments, undocumented exports
- \`performance\` — N+1 queries, missing indexes, memory leaks, slow paths
- \`architecture\` — coupling, abstraction boundaries, pattern violations
- \`test-coverage\` — missing tests for new behavior, untested edge cases
- \`scope-drift\` — diff materially exceeds the declared task scope (see thresholds below)
- \`regression_risk\` — previously-passing test now fails after changes (omit if no baseline available)
- \`cross-system-contract\` — URL path, DTO field, or shared type referenced on one side has no matching definition on the other (see check below)
<!-- SR_CATEGORIES_END -->

**Scope-drift check (required for code-change tasks):**
The \`scope-drift\` category has TWO triggers that both feed the SAME \`scope-drift\` entry
(do not introduce a new category id). If EITHER trigger fires, record \`scope-drift\` with
\`passed: false\` and an \`issue\` field that names which trigger fired and the specifics.
Only when BOTH triggers are clean do you record \`passed: true\`.

*Trigger A — diff size exceeds declared-scope threshold.*
Run \`git diff --stat HEAD\` (or \`git diff --stat <base>..HEAD\` if you know the base) and count
total changed lines (insertions + deletions) and the number of files touched.
Compare against the task's declared scope using these thresholds:

| Scope  | Max lines | Max files |
|--------|-----------|-----------|
| small  | 50        | 2         |
| medium | 200       | 5         |
| large  | 800       | 15        |

If the diff exceeds the threshold for the declared scope, Trigger A fires. The \`issue\`
field should explain the overage (e.g. "small task shipped 312 changed lines across 6
files — threshold is 50 lines / 2 files").

*Trigger B — modified files outside the declared \`related_files\` set.*
Run \`git diff --name-only HEAD\` (or \`git diff --name-only <base>..HEAD\`) to enumerate
every modified file. Compare that set against the task's declared \`related_files\` (from
the task prompt / payload). Files in the diff that are NOT in \`related_files\` are
off-scope UNLESS they fall in the incidental-files allowlist:

- Lockfiles updated as a side-effect of a manifest change: \`package-lock.json\`,
  \`poetry.lock\`, \`Cargo.lock\`, \`go.sum\`, \`yarn.lock\`, \`pnpm-lock.yaml\`.
- Generated outputs that the build emits automatically (e.g. compiled bundles,
  protobuf-generated code, schema dumps the build regenerates).
- \`.gitignore\` updates that accompany new generated paths.
- Sibling test files that mirror a declared source file — e.g. if \`agent/src/foo.ts\` is
  in \`related_files\`, then \`agent/src/foo.test.ts\` and \`agent/src/foo.spec.ts\` are
  considered in-scope tests for that file. The same applies to \`test_foo.py\` next to
  \`foo.py\` and \`__tests__/foo.test.tsx\` next to \`foo.tsx\`.

If any non-allowlisted modified file is outside \`related_files\`, Trigger B fires. The
\`issue\` field should explicitly enumerate the off-scope files and the declared scope, e.g.
\`"issue": "modified files outside declared scope: backend/app/foo.py, agent/src/bar.ts — declared related_files: [agent/src/baz.ts]"\`.
If \`related_files\` is unspecified or empty, Trigger B is informational only — record what
you observed but do not flip \`passed: false\` solely on missing scope metadata.

If both triggers fire, mention both in the same \`issue\` string. The check does NOT block
\`./done\` — it is informational and feeds the catch-rate dashboard.

**Regression-risk check (conditional on baseline availability):**
Before calling \`./done\`, check whether \`/tmp/test_baseline_\${TASK_ID}.json\` exists
(where TASK_ID is the value from the ORCHESTRATOR env, also printed at the top of
your session context). If the file exists, it contains a JSON array of test names that
were already failing BEFORE your changes — these are pre-existing failures, not
regressions. Compare this list against any test failures surfaced by your most recent
\`./check test\` or \`./check\` run:

- If a test is now failing AND its name does NOT appear in the baseline array, that is a
  regression introduced by your changes. Record \`regression_risk\` in \`check_results\`
  with \`passed: false\` and an \`issue\` naming the affected test(s) and your assessment
  of whether the failure is caused by your changes or is pre-existing/unrelated.
- If all current failures appear in the baseline (or there are no failures), record
  \`regression_risk\` with \`passed: true\`.
- If the baseline file does not exist (fix tasks, non-main branches, auto-remediation),
  compute a diff-based heuristic instead of omitting:
  1. Run \`git diff <base>..HEAD --unified=0\`. Classify changed lines by file: test files
     match \`*.test.ts\`, \`*.spec.*\`, \`test_*.py\`, or paths containing \`__tests__/\`.
  2. **Signal A — source-to-test ratio**: if ≥80 source lines changed and 0 test lines, flag as risky.
  3. **Signal B — removed exports**: count removed lines matching
     \`export (default )?(async )?(function|class|const|let|var|type|interface|enum)\`.
  Record \`regression_risk\` with \`passed: false\` if any signal fires, \`passed: true\` if none.
  **Always append \`[heuristic-derived]\`** to the issue text so downstream measurement can
  distinguish heuristic from baseline-validated results.

Like \`scope-drift\`, this check does NOT block \`./done\` — it is informational and feeds
the catch-rate dashboard.

**Cross-system-contract check (fires when diff is cross-system):**
This check is REQUIRED when the diff touches ≥2 of {admin-ui, backend, agent} OR when it adds or
changes HTTP route paths, FastAPI router decorators (\`@router.get\`, \`@router.post\`, etc.), or
fetch/axios call sites.

Steps:
1. Enumerate every new or changed URL path string, DTO field name, and shared TypeScript/Python
   type name in the diff (look for string literals like \`'/api/...'\`, decorator paths like
   \`@router.get('/...')\`, and \`fetch('/...')\` / \`axios.get('/...')\` calls).
2. For each URL path found on one side, grep the *other* side's tree for a matching route
   definition or call site (e.g., if admin-ui calls \`/api/admin/foo\`, grep backend for \`/foo\`
   in router files; if backend adds \`@router.post('/bar')\`, grep admin-ui for \`/bar\`).
3. For each DTO field name or shared type name, grep the other side for its definition or usage.
4. Record \`passed: false\` with \`severity: "major"\` for URL/route mismatches (these break
   runtime), or \`severity: "minor"\` for type-name drift where runtime is unaffected.
   Include the path string and the side that is missing the definition in the \`issue\` field.
5. If the diff is not cross-system and contains no HTTP path changes, record \`passed: true\`
   with \`issue: "diff does not cross system boundaries"\`.

This check does NOT block \`./done\` — it is informational and feeds the catch-rate dashboard.

Schema:

\`\`\`json
{
  "verdict": "pass" | "fail",
  "fixes_applied": true | false,
  "prompt_version": "<string from SELF_REVIEW_PROMPT_VERSION — include verbatim>",
  "check_results": {
    "<category>": { "passed": true | false, "issue": "...", "reasoning": "..." }
  },
  "blind_spots": ["...", "..."],
  "truncated_files_read": ["path/a.ts", "path/b.ts"],
  "categories": ["incomplete_review"]
}
\`\`\`

**Regressing-category reasoning (required for categories in the Self-Review Blind Spots list):**
If the project context injected a "Self-Review Blind Spots" section naming specific categories
for this task type, those categories are *regressing* — the dashboard shows increasing miss rates
for them. For every regressing category in \`check_results\`, add a \`reasoning\` field (~1 sentence)
describing exactly what you inspected and what evidence supports the pass/fail call. This is
required for BOTH pass and fail outcomes. Non-regressing categories keep the existing optional
schema (no \`reasoning\` required). If \`./done\` exits with a "Regressing categories need a
reasoning field" error, update the SR JSON and re-run \`./done\`.

**Evidence anchors for PASS verdicts on high-stakes categories:**
When you mark any of these five categories as \`"passed": true\` — \`correctness\`, \`error-handling\`, \`security\`, \`regression_risk\`, or \`scope-drift\` — your \`reasoning\` field MUST include a concrete evidence anchor. Valid anchors depend on the category:

For \`correctness\`, \`error-handling\`, \`security\`, \`regression_risk\`:
- A file reference with line number: \`path/to/file.ts:42\`
- A backtick-quoted code snippet ≥10 characters from the diff: \`\`\`const result = validate(...)\`\`\`
- A named test file: \`testing/test_security.py\` with the assertion that proved the claim

For \`scope-drift\` PASS verdicts specifically:
- Trigger A evidence (diff size vs. threshold): reference the diff-stat numbers and how they compare to the scope threshold, e.g. \`"diff: 42 lines / 2 files — small scope threshold: 50 lines / 2 files"\`
- Trigger B evidence (files vs. related_files): reference the file-set check you performed, e.g. \`"checked modified files against related_files: [agent/src/prompt.ts, agent/src/prompt.test.ts]"\`

Example for correctness: \`"reasoning": "Checked \`path/to/foo.ts:89\` — the null guard handles undefined payload fields, verified in test_agent.py:156"\`.
Example for scope-drift: \`"reasoning": "Trigger A: diff 45 lines / 2 files vs. small threshold 50/2 (within bounds). Trigger B: all modified files in related_files set."\`

PASS without an anchor on these high-stakes categories is unsubstantiated — downgrade to FAIL or \`unknown\` instead. This requirement does not apply to lower-stakes categories (style-conventions, documentation, performance, architecture).

\`fixes_applied: true\` means YOU caught something during self-review and
fixed it before pushing. A degraded \`{"verdict":"unknown","check_results":{},"blind_spots":["..."]}\`
envelope is acceptable when you have nothing useful to record, but the
file MUST exist for code-change tasks.

**\`blind_spots\` semantics:** A blind spot is a *specific, unverified risk inside the diff you wrote* — an untested code path, an unhandled edge case, an assumption that may not hold. It is scoped to your change.

What is NOT a blind spot, by design: after \`./pr open\` CI runs the full backend / docker / viewport / playwright suite and gates the merge. CI running the full suite is a *guarantee*, not a gap — your job is the targeted \`./check\` on the files you touched, and CI closes the loop. So "I did not run the full / complete / docker / integration test suite" is never a blind spot, in any phrasing; neither are intentional scope decisions you made deliberately.

Valid blind_spots are uncertainties CI cannot resolve for you: "did not re-read c.ts after diff truncated", "unsure whether the retry path handles concurrent calls", "no test for the empty-list edge case I added handling for". If you have nothing of that kind, leave the array empty.

**Optional verification-coverage fields (advisory — do NOT block \`./done\`):**
- \`truncated_files_read\`: list the changed file paths you actually re-opened/re-read during self-review for verification. Only include files you re-inspected — it is fine for this list to be a subset of the diff. Reviewers use it to focus on unverified files.
- \`categories: ["incomplete_review"]\`: set this marker when the diff was truncated and you could not verify all changed files even after retrying the read. \`categories\` is a top-level array (separate from \`check_results\`) reserved for coverage markers like this.

Example of partial verification (verified two of three changed files, diff was truncated):
\`\`\`json
{
  "verdict": "pass",
  "fixes_applied": false,
  "check_results": { "correctness": { "passed": true } },
  "blind_spots": ["did not re-read c.ts after retry"],
  "truncated_files_read": ["a.ts", "b.ts"],
  "categories": ["incomplete_review"]
}
\`\`\`

**Fresh-reviewer pass (when enabled):** After your session ends, a second
read-only Claude session may run and augment the self-review JSON with a
\`reviewer_findings\` array. Reviewer-flagged categories override passing
self-attestations in \`check_results\`. Additionally, if \`categories\` contains
\`"incomplete_review"\`, a recovery fresh-reviewer pass fires automatically even
when the project has not opted into the fresh-reviewer feature — recovery findings
carry \`origin: "reviewer_recovery_after_incomplete_sr"\` in their \`check_results\`
entries and a top-level \`reviewer_recovery_origin\` field on the JSON. The final JSON shape is:
\`\`\`json
{
  "verdict": "pass" | "fail",
  "fixes_applied": true | false,
  "check_results": { "<category>": { "passed": true | false, "issue": "..." } },
  "blind_spots": ["..."],
  "reviewer_findings": [{ "category": "<id>", "severity": "CRITICAL|HIGH|MEDIUM|LOW", "description": "..." }]
}
\`\`\`
If you see CRITICAL entries in \`reviewer_findings\` when you re-read the file
before \`./done\`, triage them: fix what's valid, record false positives in
\`blind_spots\`.
`;

/** Config-driven ./check description for a project that supplied a build_config
 *  with per-directory commands. Describes the project's OWN configured
 *  directories/lint/build/test commands — never Blueprint's admin-ui/agent/
 *  backend stacks or scripts/test-backend.sh. Mirrors the per-directory blocks
 *  emitted by buildConfigDrivenStackBlocks in prompts/check-helper.ts. */
function buildConfigCheckIntro(buildConfig: BuildConfig): string {
  const dirs = buildConfig.directories ?? [];
  const dirParts = dirs.map(d => {
    const path = d.path.replace(/\/$/, '');
    const cmds = [d.lint_cmd, d.build_cmd].filter((c): c is string => !!c);
    return cmds.length
      ? `\`${path}/\` (${cmds.map(c => `\`${c}\``).join(', ')})`
      : `\`${path}/\``;
  });
  const dirList = dirParts.length ? dirParts.join(', ') : 'the configured project directories';
  const testParts = dirs
    .filter(d => d.test_cmd)
    .map(d => `\`${d.path.replace(/\/$/, '')}/\` (\`${d.test_cmd}\`)`);
  const testList = testParts.length ? testParts.join(', ') : 'any configured test commands';
  return `- \`./check\` — runs this project's configured lint/build commands for the
  directories your changes touched: ${dirList}. Fast. Exits non-zero on
  failure with the actual error tail; fix and re-run until green. If your
  changes don't touch a configured directory, \`./check\` returns PASS
  without running anything — that's expected for pure-docs tasks.
- \`./check test\` — same plus this project's configured test commands
  (${testList}). This local gate is a smoke check; CI runs the full suite
  on the PR after \`./pr open\`.
`;
}

/** Generic ./check description for a non-Blueprint repo that supplied no
 *  build_config. Mirrors the no-op fallback in genericNoConfigStackBlock
 *  (prompts/check-helper.ts): ./check runs no project-specific checks (the
 *  Blueprint admin-ui/agent/backend commands would be wrong for an unknown
 *  layout) and CI is the backstop. */
function genericNoConfigCheckIntro(): string {
  return `- \`./check\` — runs lint/build for the stacks your changes touched. This
  project has no configured build_config and is not the Blueprint self-repo,
  so \`./check\` runs no project-specific checks and returns PASS without
  running anything — CI is the backstop. (To get scoped lint/build/test
  gating, configure the project's build_config with per-directory
  lint/build/test commands.)
- \`./check test\` — behaves the same; no standalone local test pass is
  configured here. CI runs the full suite on the PR after \`./pr open\`.
`;
}

/** Discriminator for which ./check intro buildCodeChangeHelperGuidance emits.
 *  Threaded into computePromptFingerprint so telemetry diverges when the prose
 *  diverges. Mirrors the branch order in buildCodeChangeHelperGuidance. */
export function codeChangeGuidanceVariant(opts: {
  buildConfig?: BuildConfig;
  owner: string;
  repo: string;
}): string {
  if (opts.buildConfig?.directories?.length) return 'build-config';
  if (isBlueprintSelfRepo(opts.owner, opts.repo)) return 'self-repo';
  return 'generic';
}

/** Project-aware code-change lifecycle/helper guidance. The repo-agnostic
 *  preamble + remainder (./pr/./done/./fail/./blocked, self-review schema,
 *  scope-drift / regression-risk / cross-system-contract) are always identical;
 *  only the ./check intro varies by project, following the same three-way branch
 *  as prompts/check-helper.ts:
 *    1. build_config with directories → describe the configured commands.
 *    2. Blueprint self-repo, no build_config → today's wording byte-for-byte.
 *    3. non-Blueprint repo, no build_config → generic no-op ./check fallback.
 *  When buildConfig/owner/repo match the self-repo no-config case, the return
 *  value equals CODE_CHANGE_HELPER_GUIDANCE byte-for-byte. */
export function buildCodeChangeHelperGuidance(opts: {
  buildConfig?: BuildConfig;
  owner: string;
  repo: string;
}): string {
  let checkIntro: string;
  if (opts.buildConfig?.directories?.length) {
    checkIntro = buildConfigCheckIntro(opts.buildConfig);
  } else if (isBlueprintSelfRepo(opts.owner, opts.repo)) {
    checkIntro = BLUEPRINT_CHECK_INTRO;
  } else {
    checkIntro = genericNoConfigCheckIntro();
  }
  return SHARED_LIFECYCLE_PREAMBLE + checkIntro + SHARED_LIFECYCLE_REMAINDER;
}

// Backward-compatible self-repo guidance constant — exactly the string shipped
// today (preamble + Blueprint ./check intro + remainder). Still imported by the
// handlers' structural tests and prompts/task-type-focus. New call sites should
// prefer buildCodeChangeHelperGuidance({ buildConfig, owner, repo }).
export const CODE_CHANGE_HELPER_GUIDANCE =
  SHARED_LIFECYCLE_PREAMBLE + BLUEPRINT_CHECK_INTRO + SHARED_LIFECYCLE_REMAINDER;

export const PLAYBOOK_GUIDANCE = `
## Operational Playbook — Active Collective Memory
The playbook is shared, mutable knowledge across all agents in this project — more like a forum than an append-only log. Use \`./playbook set <key> <value>\` to save reusable knowledge OR overwrite an existing entry, and \`./playbook get\` to review. If an existing entry led you astray, overwrite it with corrected guidance so the next agent benefits from the latest version.

**When to save (trigger conditions):**
- After fixing a lint, build, or test error: save the root cause pattern and fix
- After discovering a non-obvious configuration, path, or workaround: save it
- After wasting time on a wrong approach: save what didn't work and why
- After finding an undocumented dependency between modules: save the relationship
- After learning that a test requires special setup: save the setup steps
- After successfully running build/test commands: save the exact commands that work, especially non-obvious ones (custom scripts, required flags, env vars)
- After setting up a new repo environment: save version requirements, virtualenv steps, database setup, or prerequisites not documented in README/CLAUDE.md
- After learning repo-specific test patterns: save fixture conventions, mock helpers, or test utilities unique to this codebase
- After identifying key architectural patterns: save the pattern (e.g., "all API handlers use X decorator", "DB models follow Y convention") so future agents write consistent code
- After discovering undocumented linting or formatting rules: save the rule and how to fix violations

**What NOT to save:**
- Anything already in CLAUDE.md (check first)
- Task-specific details (PR numbers, branch names, specific bug descriptions)
- Obvious patterns that any developer would know

**Before completing your task**, reflect on repo-level knowledge you gained: Did you discover build/test commands, environment setup steps, architectural patterns, or conventions specific to this repo? If so, save them now — the next agent starts cold, and one good entry compounds across hundreds of tasks.

\`\`\`bash
./playbook set "key-name" "Concise operational knowledge here."
\`\`\`
`;

export const FIRST_TASK_GUIDANCE = `
## IMPORTANT: First Task on This Repository — Capture What You Learn

This is the first agent task on this repository. There are no existing playbook entries.
Everything you discover through trial and error is NEW knowledge that will benefit every future agent.

**Before you finish this task, use \`./playbook set\` to record:**
- The exact build and test commands that worked (e.g. \`npm run build\`, \`pytest\`, \`make test\`)
- Any linting or type-check quirks (config files, flags, common false positives)
- Dependency installation steps or version constraints discovered
- Non-obvious directory structure or naming conventions
- Any workarounds required to get things running

These are high-value entries — saving even 2–3 facts now will save future agents hours of rediscovery.

\`\`\`bash
./playbook set "test-commands" "Run tests with: <command>"
./playbook set "lint-setup" "Lint runs via: <command>"
\`\`\`
`;

// ── Shared helper: read CLAUDE.md from workspace ────────────────────────────
export function readClaudeMd(): string {
  const claudeMdPath = `${WORKSPACE}/CLAUDE.md`;
  if (fs.existsSync(claudeMdPath)) {
    return fs.readFileSync(claudeMdPath, 'utf-8').slice(0, 2000);
  }
  return '';
}

// ── Retry context injection ──────────────────────────────────────────────────
/**
 * Builds a section describing the previous failed attempt, to be inserted
 * before the raw task prompt so the agent knows what went wrong and can avoid
 * the same mistake.  Returns an empty string for first-attempt tasks.
 */
export function buildRetryContext(task: Record<string, unknown>): string {
  const ctx = (task.context as Record<string, unknown> | undefined) || {};
  const attemptNumber = typeof ctx._attempt_number === 'number' ? ctx._attempt_number : 1;
  if (attemptNumber <= 1) return '';

  const maxAttempts = typeof ctx._max_attempts === 'number' && ctx._max_attempts >= attemptNumber
    ? ctx._max_attempts
    : 0;
  const previousError = strVal(ctx._previous_error);
  const previousFailureClass = strVal(ctx._previous_failure_class);
  const previousPrUrl = strVal(ctx._previous_pr_url);
  const previousChangedFiles = Array.isArray(ctx._previous_changed_files)
    ? (ctx._previous_changed_files as unknown[]).map(f => strVal(f)).filter(Boolean)
    : [];
  const previousFailureStage = strVal(ctx._previous_failure_stage);
  const previousErrorSummary = strVal(ctx._previous_error_summary);
  const previousIsRecurring = ctx._previous_is_recurring === true;
  const failureClassHistory = Array.isArray(ctx._failure_class_history)
    ? (ctx._failure_class_history as unknown[]).map(f => strVal(f)).filter(Boolean)
    : [];
  const previousDiffSummary = Array.isArray(ctx._previous_diff_summary)
    ? (ctx._previous_diff_summary as unknown[]).filter(
        (e): e is Record<string, unknown> => typeof e === 'object' && e !== null,
      )
    : [];
  const previousTestFailureSummary = strVal(ctx._previous_test_failure_summary);
  const previousSelfReviewOutcome = ctx._previous_self_review_outcome as Record<string, unknown> | undefined;
  const firstAttemptRejectionCategories = (ctx._first_attempt_rejection_categories as Record<string, number> | undefined) || {};
  const previousAnalysisPlan = strVal(ctx._previous_analysis_plan);
  const prevAttempt = attemptNumber - 1;

  const failureTypeLabel = previousFailureClass || 'UNKNOWN';
  const headerSuffix = maxAttempts > 0 ? ` of ${maxAttempts}` : '';
  const lines: string[] = [
    `## Previous Attempt (attempt #${prevAttempt}${headerSuffix} failed)`,
    previousFailureStage
      ? `Failure type: ${failureTypeLabel} (at ${previousFailureStage} stage)`
      : `Failure type: ${failureTypeLabel}`,
  ];

  if (maxAttempts > 0) {
    const remainingAfter = Math.max(0, maxAttempts - attemptNumber);
    if (remainingAfter === 0) {
      lines.push(
        `**Final attempt (${attemptNumber} of ${maxAttempts}) — no retries remain after this one.** Adopt a conservative strategy: prefer the smallest viable change that addresses the root cause. Fewer files, simpler approach, avoid speculative refactors or scope expansion.`,
      );
    } else if (remainingAfter === 1) {
      lines.push(
        `Retry budget: attempt ${attemptNumber} of ${maxAttempts} (1 retry remaining after this one). **Be conservative** — narrow scope where possible and avoid speculative changes.`,
      );
    } else {
      lines.push(
        `Retry budget: attempt ${attemptNumber} of ${maxAttempts} (${remainingAfter} retries remaining after this one).`,
      );
    }
  }

  if (previousIsRecurring) {
    lines.push(
      '**This is a recurring failure pattern** — this same failure type has been seen multiple times. Consider a fundamentally different approach.',
    );
  }

  if (failureClassHistory.length > 1) {
    lines.push(
      `Failure trajectory across retries: ${failureClassHistory.join(' → ')}. Consider whether the sequence reveals a root cause (e.g., CODE_ERROR → AGENT_TIMEOUT may indicate an infinite loop; repeated EXTERNAL_API suggests environment instability).`,
    );
  }

  if (previousPrUrl) {
    lines.push(`PR from previous attempt: ${previousPrUrl}`);
  }

  // Prefer enriched diff summary over plain file list when available
  if (previousDiffSummary.length > 0) {
    const fileLines = previousDiffSummary.map(e => {
      const file = strVal(e.file);
      const additions = typeof e.additions === 'number' ? e.additions : 0;
      const deletions = typeof e.deletions === 'number' ? e.deletions : 0;
      const changeType = strVal(e.change_type) || 'modified';
      return `  - ${file} (${changeType}, +${additions}/-${deletions} lines)`;
    });
    lines.push(`Files changed in previous attempt:\n${fileLines.join('\n')}`);
  } else if (previousChangedFiles.length > 0) {
    lines.push(`Files changed in previous attempt:\n${previousChangedFiles.map(f => `  - ${f}`).join('\n')}`);
  }

  if (previousTestFailureSummary) {
    lines.push(`Test failures from previous attempt:\n${previousTestFailureSummary}`);
  }

  if (previousSelfReviewOutcome) {
    const fixesApplied = Boolean(previousSelfReviewOutcome.fixes_applied);
    const checkResults = (previousSelfReviewOutcome.check_results as Record<string, Record<string, unknown>> | undefined) || {};
    const verdict = strVal(previousSelfReviewOutcome.verdict);

    // Detect SR-missed categories: where SR said passed/absent but reviewer found issues
    const isSoftFallback = verdict === 'unknown' && Object.keys(checkResults).length === 0;
    const srMissedCategories: Array<[string, number]> = [];

    if (!isSoftFallback && Object.keys(firstAttemptRejectionCategories).length > 0) {
      for (const [category, count] of Object.entries(firstAttemptRejectionCategories)) {
        // SR missed this category if it was reviewer-originated, absent from
        // check_results, or marked as passed.
        if (category in checkResults) {
          const srResult = checkResults[category];
          if (srResult.origin === 'reviewer' || srResult.passed) {
            srMissedCategories.push([category, count]);
          }
        } else {
          // Category not in check_results, so SR missed it
          srMissedCategories.push([category, count]);
        }
      }
    }

    if (srMissedCategories.length > 0) {
      // Enumerate missed categories with issue counts
      const missedLines = srMissedCategories
        .map(([cat, count]) => `   - ${cat} (reviewer found ${count} ${count === 1 ? 'issue' : 'issues'})`)
        .join('\n');
      lines.push(
        `Previous attempt's self-review missed these categories (your SR claimed they passed, but the reviewer flagged issues):\n${missedLines}\nOn THIS retry, scrutinize these categories carefully — re-read the changed files and verify each.`,
      );
    } else {
      const selfReviewStatus = fixesApplied
        ? 'Self-review found issues and applied fixes (but the task still failed after review)'
        : 'Self-review ran and found no critical issues';
      lines.push(`Self-review outcome: ${selfReviewStatus}`);
    }
  }

  if (previousAnalysisPlan) {
    lines.push(`Pre-implementation analysis from previous attempt (skip re-analysis — use this plan directly):\n${previousAnalysisPlan}`);
  }

  // For CODE_ERROR failures, show the focused error summary; otherwise show the full error.
  if (previousErrorSummary) {
    lines.push(`Error summary: ${previousErrorSummary}`);
  } else if (previousError) {
    lines.push(`Error: ${previousError}`);
  }

  const hasChangedFiles = previousDiffSummary.length > 0 || previousChangedFiles.length > 0;
  if (hasChangedFiles && previousTestFailureSummary) {
    lines.push(
      'Focus your retry on fixing the failing tests shown above. Preserve the working changes in other files and avoid re-doing changes that were not related to the test failure.',
    );
  } else if (hasChangedFiles) {
    lines.push(
      'Review the files listed above — your previous attempt modified them. Check whether those changes were correct or need to be reworked. Do not blindly repeat the same approach if it failed.',
    );
  } else {
    lines.push(
      'Analyze the error above and avoid the same mistake. If it was a test failure, check the specific test first. If it was a lint error, run the linter before committing.',
    );
  }
  lines.push('');
  return lines.join('\n');
}
