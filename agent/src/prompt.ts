import fs from 'fs';
import path from 'path';
import { createHash } from 'crypto';
import { execFileSync } from 'child_process';
import { WORKSPACE } from './index.js';
import type { BuildConfig } from './index.js';
import { isReadOnlyTaskType } from './taskCapabilities.js';
import { parseBackendTimestamp } from './utils/time.js';
import { agentTokenShellFn, jsonStrShellFn } from './token-file.js';

// ── Re-exports from sub-modules (facade) ────────────────────────────────────
// The bulky helpers have been split out into prompts/* modules so that small
// prompt edits don't keep reshaping a 2k-line file (which causes painful merge
// conflicts in the self-improving pipeline). Callers continue to import from
// './prompt.js' unchanged.
export {
  buildFileTree,
  buildRepoMap,
  countSourceFiles,
  detectGreenfield,
  isGreenfieldRepo,
  buildGreenfieldCitationGuidance,
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
  formatRejectionPatternsSection,
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
import { isPytestCmd } from './prompts/check-helper.js';
import { effectiveTestCmd, effectiveBuildCmd, effectiveLintCmd } from './utils/test-runners.js';
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
// Marker embedded in the read-only block hooks. Used to WRITE the hooks (read-only
// path) and to RECOGNISE+REMOVE our own hooks when a warm VM is reused for a write
// task (write path). Single source of truth so the two paths can't drift apart.
const READ_ONLY_BLOCK_MARKER = 'This is a read-only analysis task. Do NOT commit or push.';
const READ_ONLY_BLOCK_HOOK = `#!/bin/sh\necho "ERROR: ${READ_ONLY_BLOCK_MARKER}" >&2\nexit 1\n`;
const READ_ONLY_BLOCKED_HOOKS = ['pre-commit', 'pre-push'] as const;

function installReadOnlyBlockHooks(hooksDir: string): void {
  try {
    fs.mkdirSync(hooksDir, { recursive: true });
    for (const hook of READ_ONLY_BLOCKED_HOOKS) {
      fs.writeFileSync(`${hooksDir}/${hook}`, READ_ONLY_BLOCK_HOOK, { mode: 0o755 });
    }
  } catch { /* hooks dir may not exist before clone completes */ }
}

export function configureAgentPermissions(taskType: string): string[] {
  const hooksDir = `${WORKSPACE}/.git/hooks`;
  if (isReadOnlyTaskType(taskType)) {
    // Strip GitHub token from git remote so the agent can't push
    try {
      execFileSync('git', ['remote', 'set-url', 'origin', 'https://github.com/readonly/readonly.git'], { cwd: WORKSPACE });
    } catch { /* git dir may not exist yet */ }

    // Block git commit/push via hooks (backup enforcement).
    installReadOnlyBlockHooks(hooksDir);

    return ['Write', 'Edit', 'NotebookEdit'];
  }

  // Write types: no Claude restrictions — but warm VMs reuse the workspace (and
  // its .git) across sequential roles, and the read-only branch above only ever
  // ADDS the block hooks, never removes them. A write task (fix/create/migrate/…)
  // that lands on a VM whose prior role was read-only would otherwise inherit the
  // `exit 1` pre-commit/pre-push hooks and be physically unable to commit or push
  // its work — the authenticated remote is restored at push time, but these hooks
  // are not. (Real incident: a "Fix PR" task resolved a merge conflict, ./check
  // green, but ./pr open failed at push for 3 attempts on the leftover hooks,
  // wedging the whole research strand for ~10h.) Idempotently remove OUR block
  // hooks so the VM matches the current write role. Guard on the marker so a
  // legitimate repo-provided hook (should one ever live here) is left untouched.
  for (const hook of READ_ONLY_BLOCKED_HOOKS) {
    const hookPath = `${hooksDir}/${hook}`;
    try {
      if (fs.existsSync(hookPath)
          && fs.readFileSync(hookPath, 'utf-8').includes(READ_ONLY_BLOCK_MARKER)) {
        fs.unlinkSync(hookPath);
      }
    } catch { /* nothing to clear / hooks dir absent */ }
  }
  return [];
}

/**
 * Configure the Workday Resident's explicit read-only posture.
 *
 * The session uses a live credential helper to keep `git fetch origin`
 * working, so only the inner-agent write blockers and commit/push hooks are
 * installed here. The remote URL itself stays untouched.
 */
export function configureResidentAgentPermissions(taskType: string): string[] {
  if (!isReadOnlyTaskType(taskType)) {
    throw new Error(`resident permissions require a read-only task type, got "${taskType}"`);
  }
  installReadOnlyBlockHooks(`${WORKSPACE}/.git/hooks`);
  return ['Write', 'Edit', 'NotebookEdit'];
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
// Blueprint-self-repo viewport before/after screenshot pilot (Phase 0). Appended
// to the create/fix/refactor hints. The instruction is self-limiting: it fires
// only when the diff touches `admin-ui/src/components/`, which exists only in the
// Blueprint repo — foreign repos never match the guard, so no separate self-repo
// signature is needed. `scripts/viewport-snapshot.sh` is informational and never
// blocks `./pr`. Keep wording identical across every insertion point.
export const VIEWPORT_SNAPSHOT_HINT =
  ' VISUAL CHECK (admin-ui only): if your diff touches `admin-ui/src/components/`, after `./check` passes run `./viewport-snapshot`, then Read each before/after PNG pair the manifest marks `changed` or `new` and confirm (a) the intended visual change is present, (b) no unintended layout drift in other captured components, (c) no blank or error-boundary render. For `changed` pairs the manifest also includes a `change_pct` field (percent of pixels that differ) and a `diff` field with a path to a highlighted diff PNG — Read the diff image to see exactly where the component changed instead of eyeballing before/after side by side. Fix what looks wrong and re-snapshot. Keep it to ≤6 changed/new pairs at 375px. The script is informational — it never blocks `./pr`. To inspect any component without making a change, use `bash scripts/viewport-snapshot.sh --current ComponentName` — skips the BEFORE pass (no git worktree, no npm ci on base), writes AFTER PNGs under /tmp/viewport_shots/, and exits in under 60s on a warmed node_modules.';

// Live-capture hint (Blueprint self-repo only). Unlike the before/after
// snapshot, `./viewport-live` builds the SPA against the LIVE backend (using the
// firebase.ts token bypass) and screenshots the rendered dashboard, so agents
// and operators can see real backend state without an interactive OAuth login.
export const VIEWPORT_LIVE_HINT =
  ' LIVE ADMIN-UI CAPTURE: when `ORCHESTRATOR_URL` and `ADMIN_API_KEY` are set in the environment, run `./viewport-live` to build the admin-UI SPA against the live backend (via the Firebase token bypass) and capture a full-page screenshot of the rendered dashboard with real data — no interactive Google OAuth login needed. PNGs are written under `/tmp/viewport_live/`, and with the baked-in `--upload --task-id` they are uploaded so they can be reviewed. The capture is informational and never blocks `./pr`. It exits 75 with `VIEWPORT_GATE_INFRA_UNAVAILABLE` when Chromium cannot launch on this VM — that is an infrastructure shortfall, not a code defect.';

export const VIEWPORT_FAILURE_SCREENSHOT_HINT =
  ' VIEWPORT REGRESSION FAILURES: if `./check` fails on viewport tests, the failure screenshots are automatically uploaded and logged in the output as `[check] viewport screenshot uploaded: <gs_uri> (<component>@<width>px) — local copy: <local_path>`. BEFORE attempting to fix the visual regression, open and read the screenshot at the logged local path (e.g., `/tmp/viewport_failure_<task_id>_<component>_<width>.png`) to see the broken render. The screenshot shows you exactly what is wrong with the layout so you can diagnose the issue instead of fixing blind.';

/**
 * Build a prompt section pointing an agent at viewport screenshots that were
 * already downloaded to the local workspace (paths under /tmp/viewport_*). The
 * paths follow the `<component>-<width>-(before|after|diff).png` naming the
 * implementer uploads, so we group them back into before/after/diff sets per
 * component+width key rather than emitting a flat list. Returns '' when no
 * screenshots were downloaded, so non-visual PRs are unaffected.
 *
 * Shared by the review and fix prompt builders so a downstream
 * agent receives the same screenshot brief regardless of which lane forwarded
 * the PNGs.
 */
export function buildViewportScreenshotSection(downloadedShotPaths: string[]): string {
  if (downloadedShotPaths.length === 0) return '';
  const pairs = new Map<string, { before?: string; after?: string; diff?: string }>();
  for (const p of downloadedShotPaths) {
    const basename = p.replace(/^.*\//, '').replace(/\.png$/, '');
    const key = basename.replace(/-(?:before|after|diff)$/, '');
    if (!pairs.has(key)) pairs.set(key, {});
    const entry = pairs.get(key);
    if (!entry) continue;
    if (basename.endsWith('-before')) entry.before = p;
    else if (basename.endsWith('-after')) entry.after = p;
    else if (basename.endsWith('-diff')) entry.diff = p;
  }
  const lines = ['## Viewport Screenshots (before/after/diff)'];
  lines.push('The implementing agent captured these admin-ui viewport screenshots. Use the Read tool to inspect each PNG and check for visual regressions.');
  for (const [key, entry] of pairs) {
    lines.push(`\n**${key}**`);
    if (entry.before) lines.push(`  - before: ${entry.before}`);
    if (entry.after) lines.push(`  - after:  ${entry.after}`);
    if (entry.diff) lines.push(`  - diff:   ${entry.diff}`);
  }
  return lines.join('\n') + '\n\n';
}

// ── Fix-hint sub-sections ────────────────────────────────────────────────
// Broken out so each concern (staleness, code-review flow, defect repro) is
// clearly separated and independently maintainable.

const FIX_STALENESS_NOTICE =
  'You are fixing issues from a code review or a pipeline failure. Read ALL feedback carefully and address every point. IMPORTANT: the spawn prompt can be stale — a concurrent rollup may have resolved conflicts or a new commit may have broken something new between when this task was queued and now. If the prompt includes a "stale-premise notice", trust the latest state.';

const FIX_CODE_REVIEW_PATH =
  'Code-review fix (common case): After fixing, run `./check` to verify, then `./pr open --title "fix: ..." --body "..."` to push (updates the existing PR if there is one), then `./done`. Use `./fail "<reason>"` only if you can\'t reasonably resolve the feedback.';

const FIX_DEFECT_REPRODUCTION_PATH =
  'Defect reproduction (finding-derived fix): if this task asks you to fix a reported defect from a finding, REPRODUCE FIRST — confirm it still reproduces on current HEAD via a failing test, an observable symptom, or the cited pattern still present at the cited code/symbol. If it does NOT reproduce (cited code absent, or your reproduction attempt demonstrably no longer fails), do NOT fabricate a speculative change: use `./done --already-done "<reason naming the cited location and what you checked>"` — that path reports the structured `already_resolved` reproduce-on-HEAD verdict so the outcome scores as a verified no-op, not a false fix. Three canonical reproduce-on-HEAD outcomes: reproduced → normal fix and finish with `./done --reproduced "<what reproduced on HEAD and what you fixed>"`; already-resolved → `./done --already-done` off-ramp; indeterminate → keep the bar high, proceed with the normal fix, and if still unproven at ship time use `./done --indeterminate "<why reproduce-on-HEAD remained ambiguous>"`. Keep the not-reproducible bar high — ambiguity or a still-present cited pattern means proceed with the normal fix.';

export const TASK_TYPE_HINTS: Record<string, string> = {
  'qa': 'Focus on writing tests. Run the existing test suite first to understand patterns. Do not modify production code unless fixing a test setup issue.',
  'migrate': 'This is a database migration task. Check existing Alembic migrations in backend/alembic/versions/ for naming conventions. Use `alembic revision --autogenerate` as a starting point only — always review and clean up generated migrations.',
  'security_audit': 'Focus on identifying security vulnerabilities. Do not implement fixes — create a detailed findings report as your output.',
  'self_audit': 'Score system maturity on four signals: test_coverage, friction_density, claim_consistency, failure_mix. Each component is 0-100. Pick recommended_mode (audit|fundamentals|ship|pause) from the worst component. Do not modify any files — output is a JSON findings report only.',
  'release_audit': 'You are running on the LTS / promotion branch and reviewing the incoming diff against that branch\'s standards. Run `git diff <diff_range>` to obtain the diff. Check for test erosion (deleted test files, net assertion/test-count delta). Score findings into audit_score [0,1]. Do not modify any files — output is a JSON release audit report only.',
  'perf': 'Profile before optimizing. Identify the specific bottleneck with measurements before writing any fix.',
  'create': 'Before finishing: edit the files you need to change, run `./check` to validate lint/build, fix any failures, then `./pr open --title "feat/fix/etc: ..." --body "Summary of changes"` to ship and `./done` to mark complete. The `./pr` helper gates on `./check` so broken code can\'t reach a PR. If admin-ui viewport bootstrapping only fails because Chromium deps are missing, `./check` will retry in the Playwright container when Docker is available and otherwise warn without blocking the task. If a library is unfamiliar, fetch its docs first. If you determine the task can\'t be completed (scope wrong, conflicting requirements, blocker found), call `./fail "<reason>"` instead of leaving silently.' + VIEWPORT_FAILURE_SCREENSHOT_HINT + VIEWPORT_SNAPSHOT_HINT + VIEWPORT_LIVE_HINT,
  'fix': FIX_STALENESS_NOTICE + '\n\n' + FIX_CODE_REVIEW_PATH + '\n\n' + FIX_DEFECT_REPRODUCTION_PATH + VIEWPORT_FAILURE_SCREENSHOT_HINT + VIEWPORT_SNAPSHOT_HINT + VIEWPORT_LIVE_HINT,
  'docs': 'Read the existing README and documentation files first. Understand the current documentation structure before making changes.',
  'deps': 'Check for breaking changes in major version bumps. Run the full test suite after updating to verify compatibility.',
  'refactor': 'Run the full test suite BEFORE and AFTER changes via `./check test`. Zero test regressions allowed. Do not change external behavior. After refactoring + verifying via `./check`, run `./pr open --title "refactor: ..." --body "..."` and `./done`. Use `./fail` if the refactor proves not feasible.' + VIEWPORT_SNAPSHOT_HINT,
  'investigate': 'This is a read-only investigation. Do NOT modify any files. Search for known issues related to error patterns you discover. Write your findings to the output JSON only. If you are investigating a reported defect from a finding, classify it against the canonical reproduce-on-HEAD verdict — reproduced, non-reproducing/already-resolved, or indeterminate — and state which outcome holds (and the evidence) in your findings, rather than reporting free-text rationale alone.',
  'debug': 'Search for known issues related to error patterns you discover. Use WebSearch to look up specific error messages or stack traces when local context is insufficient.',
};

// Task types that don't get generic TASK_TYPE_HINTS entries because they assemble
// bespoke prompts in their handlers and don't append the generic hint dict. This
// allowlist prevents regressions: new dispatch entries must either add a hint to
// TASK_TYPE_HINTS or be explicitly opted-out here with justification.
export const TASK_TYPES_WITHOUT_HINT: ReadonlySet<string> = new Set([
  'review',           // Uses review-fix.ts bespoke prompt assembly
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
  'release_audit_challenge', // Uses audit-challenge.ts bespoke prompt assembly (advisory second opinion)
  'release_merge_grade', // Uses merge-grade.ts bespoke prompt assembly (blind goal-coverage grader)
  'repo_bootstrap',          // Uses specialized.ts bespoke prompt assembly (external-repo first-cycle bootstrap)
  'salvage_bundle',          // Uses standard.ts handleSalvageBundle bespoke prompt assembly (consolidated fix batch)
  'capability_proof_trial',  // Uses capability-proof-trial.ts; runs the targeted covering tests directly, no Claude prompt
  'criterion_coverage_trial', // Uses criterion-coverage-trial.ts; runs discovered covering tests directly, no Claude prompt
  'walkthrough_e2e',        // Uses specialized.ts bespoke prompt assembly (live-stack walkthrough harness)
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
    const testCmd = effectiveTestCmd(dir);
    const buildCmd = effectiveBuildCmd(dir);
    if (testCmd) testCmds.push(`${dir.path}: \`${testCmd}\``);
    else if (buildCmd) testCmds.push(`${dir.path}: \`${buildCmd}\``);
  }
  if (testCmds.length > 0) {
    const cmdsText = testCmds.join(', ');
    hints['create'] = `Before finishing: edit the files you need, run \`./check\` to validate lint/build (which uses ${cmdsText} under the hood), fix any failures, then \`./pr open --title "feat/fix/etc: ..." --body "Summary"\` and \`./done\`. The \`./pr\` helper gates on \`./check\`. If admin-ui viewport bootstrapping only fails because Chromium deps are missing, \`./check\` will retry in the Playwright container when Docker is available and otherwise warn without blocking the task. Use \`./fail "<reason>"\` if the task can't be completed.` + VIEWPORT_SNAPSHOT_HINT + VIEWPORT_LIVE_HINT;
    hints['refactor'] = `Use \`./check test\` to run the full test suite BEFORE and AFTER changes (${cmdsText}). Zero test regressions allowed. After refactoring + verifying, \`./pr open --title "refactor: ..." --body "..."\` then \`./done\`. \`./fail\` if not feasible.` + VIEWPORT_SNAPSHOT_HINT + VIEWPORT_LIVE_HINT;
  }

  // 'deps' hint: list actual package managers detected
  const pkgManagers: string[] = [];
  for (const dir of dirs) {
    if (dir.type === 'python') pkgManagers.push('pip/poetry');
    else if (dir.type === 'node') pkgManagers.push('npm/yarn');
    else if (dir.type === 'go') pkgManagers.push('go mod');
    else if (dir.type === 'rust') pkgManagers.push('cargo');
    else if (dir.type === 'other') pkgManagers.push('language-specific package manager');
  }
  if (pkgManagers.length > 0) {
    hints['deps'] = `Check for breaking changes in major version bumps. This project uses ${pkgManagers.join(', ')}. Run the full test suite after updating to verify compatibility.`;
  }

  if (promotionTarget) {
    const target = promotionTarget;
    hints['release_audit'] = `You are running on the promotion/integration branch (${target}) and reviewing the incoming diff against ${target}'s standards. Run \`git diff <diff_range>\` to obtain the diff. Check for test erosion (deleted test files, net assertion/test-count delta). Score findings into audit_score [0,1]. Do not modify any files — output is a JSON release audit report only.`;
  }

  return hints;
}

export const DEFAULT_TOOL_GUIDANCE = 'Tool guidance: Use Grep/Glob for code search (not Bash grep/find), Read for file contents, Agent tool for parallel exploration of multiple areas. Use TodoWrite to track multi-step work. Use WebFetch to look up API documentation, library references, or framework guides when working with unfamiliar code. Use WebSearch to find solutions for specific error messages or unusual patterns. Use ./symbols <name> to find where a function or class is defined (returns file path, line, and kind). If you discover bugs, architectural issues, or improvement ideas, use ./note to leave annotations: ./note comment "msg" --page <page>, ./note issue "msg" --severity high, ./note vision "msg". If you hit a barrier, have a question for the project owner, or want to flag something important, use ./messages send \'your message\' (types: NOTE, QUESTION, FEEDBACK, BUG_REPORT, BLOCKER) or ./messages send --stdin for quote-safe multiline text.';

// Shared parenthetical for every read-only handler that instructs the agent to
// write a /tmp output file. The Write/Edit tools are stripped for read-only
// tasks (configureAgentPermissions returns them in the disallowed list), so the
// agent must use Bash (e.g. `cat << 'EOF' > /tmp/...` or `echo '...' > /tmp/...`)
// to create the file. Without this note the agent reaches for the disabled Write
// tool, the call silently no-ops, no output file lands, and the unparseable
// final message is booked as a format miss and wasted repair reopen.
export const READ_ONLY_BASH_WRITE_NOTE = '(use Bash to write the file — the Write/Edit tools are disabled for this read-only task)';

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

If your task context indicates this is a fix iteration, **the same approach won't work twice**. Before writing code, ask yourself what the previous attempt assumed about the root cause.

Common wrong-direction patterns to avoid on retry:
1. **Don't repeat the same fix** — it won't change the outcome. If a lint fix failed, re-running it produces the same failure. If "file too long" was the error, bump the limit—don't condense the diff.
2. **Read the assertion message and follow it literally** — if it asks you to update test config, a migration, or infrastructure, do exactly that. Don't refactor the source to make the test pass; either the source needs changing or the policy needs to.

The diagnosis system fingerprints failures as \`ResidualRemediationCase\`. If your task includes prior_diagnosis or diagnosis_root_cause in context, that indicates what **previously didn't work** — pivot away from it.
`;

// Per-task SIZE-specific guidance — kept distinct from READ_THE_FAILURE_MESSAGE
// because it includes an explicit step-by-step procedure.
export const SIZE_GUARDRAIL_GUIDANCE = `
## Size Guardrails Are Advisory — Bump, Don't Refactor

**Core policy: Bump the limit, do NOT refactor.** If a test fails with \`module_size_limits.json\` or \`size guardrail\`, update the JSON cap — not the source. The assertion message walks you through the procedure.

**Two guardrail cases:**
1. Existing cap breach (\`has <N> lines (limit: <M>)\`) — bump the cap.
2. New uncapped file over 1500 lines — add a cap entry (not a code split).

**Fix procedure:**
\`python3 backend/testing/check_size_limits.py --fix\`
This rewrites caps and adds entries using the canonical bump policy (\`max(current+200, current*1.15)\`). Commit both source and regenerated JSON.

**Why not refactor?** Refactoring during create/fix tasks is out-of-scope, causes import cycles, breaks unrelated tests, and burns retries. Size decisions belong in separate PRs. Exception: if you bundled unrelated code by accident, shrink the diff back to scope.
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
   or \`./messages send --stdin NOTE\` for quote-heavy prose
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

// Anti-hedging instinct: code-writing agents must ship the feature on, not
// wrapped in a default-off flag, fail-open fallback, or observe-mode shim.
export const ANTI_HEDGING_GUIDANCE = `
## Anti-Hedging: Ship It On

When implementing a feature or fix, do NOT introduce:
- Default-off feature flags, observe-mode wrappers, or "gate_enabled=False" guards
  that leave the new behavior dark on the first deploy
- Fail-open fallbacks that silently skip the new logic when an input is missing or unexpected
- "Log-only" / observe mode that defers enforcement to a follow-up task

These patterns re-introduce the hedging that every clean-up cycle has to remove later.
If the change is correct, ship it on. If it is unsafe without a flag, the task scope is
too broad — narrow the scope and ship the safe portion, then file a follow-up.
`;

// Decision discipline: spend cheap self-critique tokens BEFORE proposing so the
// first answer is the simplest thing that works, not a plausible draft that needs
// rounds of correction. Front-loads the decider's real intent + selection criteria
// and an adversarial self-pushback pass. Bias is against UNJUSTIFIED building —
// a genuinely earned system is the simplest-that-works and should be built.
export const BUILD_WITH_INTENT_GUIDANCE = `
## Build With Intent: Decide Before You Draft

Before proposing a solution, design, or fix — especially one that adds a system,
tool, gate, or detector — spend cheap self-critique tokens so your FIRST answer is
the simplest thing that actually works, not a plausible draft that needs rounds of
correction. Every round of pushback you would get is usually a criterion you already
knew and failed to apply; apply it yourself first.

1. **Name the target before drafting.** State the real intent behind the request
   (not the literal ask) and the known selection criteria — simpler wins, don't add
   a system without cause, don't make a human the dependency, fix at root not just
   here — then optimize against them from draft one.
2. **Be your own pushback.** Write the obvious answer, then attack it before anyone
   sees it: What is the simplest thing that could actually work? What can I REMOVE
   instead of add? What would the decider reject, and why? Present only the survivor.
3. **Defaults are filters, not suggestions:**
   - **Subtractive first, not subtractive-only.** Prefer changing behavior or
     removing a system over adding one, and make any new system earn its place by
     naming its maintenance and human-attention cost out loud — you do not feel
     complexity cost the way a human does. Cut unjustified additions; but a
     genuinely justified system IS the simplest-that-works, so build it without
     flinching. The bias is against unjustified building, not against building.
   - **Un-gameable over policing.** One objective check that cannot be gamed beats
     a detector that pattern-matches surface smells and just gets routed around.
   - **Never route through the human.** A solution that reintroduces the operator as
     a dependency or bottleneck is a non-answer — design that out.

Three critique passes is discipline; ten is token-burn in a costume. Stop when the
answer stops changing.
`;

// Build-order discipline: ground the design skeleton first, write tests that
// capture INTENDED behavior against that skeleton, THEN implement that behavior
// (tests confirm it — green is evidence, not the target). Designed out: tests
// written against already-finished code (which mirror the implementation and catch
// nothing) and the "edit the test until it passes" reflex that silently neuters the
// assertion that was catching the defect.
export const BUILD_ORDER_GUIDANCE = `
## Build Order: Design → Tests-Against-Design → Implement

Build in this order — it is what makes the tests catch real defects instead of
echoing whatever the code already does:

1. **Ground the design first** — lay down the skeleton (interfaces, signatures,
   contracts, the shape of the thing) before filling in the bodies. Stubs that throw
   or return a placeholder are fine at this step; you are fixing the SHAPE, not the
   behavior.
2. **Write tests against that grounded design** — assert what it SHOULD do (intended
   inputs, outputs, error cases), not how today's code happens to do it. Tests
   written against finished code mirror the implementation and catch nothing; tests
   written against the design capture intent.
3. **Then implement the intended behavior** — the design is the target; the tests
   only confirm you hit it. Build the real behavior, not the minimum that turns the
   tests green: code that passes only the exact asserted cases has overfit the check,
   not implemented the design.

**Test-edit rule:** you MAY change a test after writing it — the design legitimately
shifts as you learn. But before you do, be able to name the concrete, real defect the
edited test still catches. If your only reason for the edit is "to make it pass,"
stop: silently weakening, neutering, or deleting an assertion to go green is the exact
failure mode this ordering exists to prevent. Change the code, or change the test
because the intended behavior genuinely changed — never to dodge a red.
`;

// Canonical concatenation of the cross-cutting failure-guidance blocks
// shared by code-change handlers (standard.ts, review-fix.ts fix path).
// Centralized so block order, inclusion, and future additions only need
// to change in one place.
export function assembleFailureGuidanceBlocks(includeRetryGuidance = true): string {
  return READ_THE_FAILURE_MESSAGE + SIZE_GUARDRAIL_GUIDANCE + (includeRetryGuidance ? PRIOR_ATTEMPT_DIVERGENCE : '') + BEYOND_SCOPE_REPORTING + EDGES_CONSIDERED_GUIDANCE + ANTI_HEDGING_GUIDANCE + BUILD_WITH_INTENT_GUIDANCE + BUILD_ORDER_GUIDANCE;
}

// Compute a stable 12-hex-char fingerprint of the static prompt guidance for
// a given task type. Used to correlate production task outcomes back to prompt
// revisions cycle-over-cycle. Only hashes static/deterministic content —
// task IDs, file paths, and payload-specific text are intentionally excluded.
export function computePromptFingerprint(
  taskType: string,
  buildConfig?: BuildConfig,
  options?: {
    includeParallelFixGuidance?: boolean;
    promotionTarget?: string;
    codeChangeGuidanceVariant?: string;
    emittedFailureGuidance?: string;
  },
): string {
  const hints = getTaskTypeHints(buildConfig, options?.promotionTarget);
  const taskHint = hints[taskType] ?? '';
  const sections = [taskHint, DEFAULT_TOOL_GUIDANCE];
  if (options?.emittedFailureGuidance) {
    sections.push(options.emittedFailureGuidance);
  }
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

// Returns the shared bash _post_with_retry function embedded by all write-capable
// helper scripts (messages send/resolve, playbook set, design-doc set/delete, note).
// Provides bounded retry on 429/5xx: 4 max attempts, 5s/15s/30s backoff, Retry-After
// honored, token re-read each attempt. Requires _load_agent_token (from agentTokenShellFn()).
// Usage in bash: echo "$JSON" | _post_with_retry <url> <op-label>
export function retryPostShellFn(): string {
  return `
_post_with_retry() {
  local url="$1" op_label="$2"
  local payload attempt=0 max=4 delay http_code body response dump saw_auth=0
  payload=$(cat)
  while [ $attempt -lt $max ]; do
    _load_agent_token || return 1
    dump=$(mktemp)
    response=$(printf '%s' "$payload" | curl -sS --max-time 30 --connect-timeout 10 \\
      -X POST "$url" \\
      -H "Content-Type: application/json" -H "X-Agent-Token: $TOKEN" \\
      --data-binary @- \\
      -D "$dump" \\
      -w '\\n%{http_code}')
    http_code=$(printf '%s' "$response" | tail -n1)
    body=$(printf '%s' "$response" | head -n -1)
    delay=$(grep -i '^retry-after:' "$dump" 2>/dev/null | tail -n1 | tr -dc '0-9')
    rm -f "$dump"
    if [ "$http_code" -ge 200 ] && [ "$http_code" -lt 300 ]; then
      printf '%s' "$body"
      return 0
    fi
    attempt=$((attempt + 1))
    if [ $attempt -ge $max ]; then break; fi
    # curl emits http_code "000" on a transport-level failure (connect timeout,
    # DNS failure, TLS handshake, dropped connection) — no HTTP response at all.
    # Treat it as a transient failure and retry alongside 429/5xx so a network
    # blip (or a token-budget hiccup) is not booked as a permanent failure.
    if [ "$http_code" = "000" ] || [ "$http_code" = "429" ] || { [ "$http_code" -ge 500 ] && [ "$http_code" -lt 600 ]; }; then
      if [ -z "$delay" ]; then
        if [ $attempt -eq 1 ]; then delay=5
        elif [ $attempt -eq 2 ]; then delay=15
        else delay=30; fi
      fi
      echo "[RETRY $attempt/$((max - 1))] $op_label failed (HTTP $http_code), retrying in \${delay}s..." >&2
      sleep "$delay"
    elif [ "$http_code" -eq 401 ] || [ "$http_code" -eq 403 ]; then
      saw_auth=1
      echo "[AUTH] $op_label rejected (HTTP $http_code) — re-reading rotated agent token and retrying" >&2
      sleep 1
    else
      if [ -n "$body" ]; then printf '%s\\n' "$body" >&2; fi
      echo "[ERROR] $op_label failed (HTTP $http_code, non-retryable)" >&2
      return 1
    fi
  done
  if [ "$saw_auth" -eq 1 ] && { [ "$http_code" -eq 401 ] || [ "$http_code" -eq 403 ]; }; then
    echo "[ERROR] $op_label failed: agent token expired — restart the session." >&2
  else
    if [ -n "$body" ]; then printf '%s\\n' "$body" >&2; fi
    echo "[ERROR] $op_label failed after $max attempts (HTTP $http_code)" >&2
  fi
  return 1
}`;
}

/** Bash helper that reads only a stable regular file, never a symlink/FIFO, within a byte cap. */
export function boundedRegularReadShellFn(): string {
  return `
_read_regular_bounded() {
  "$NODE_BIN" -e 'const fs=require("fs"),p=process.argv[1],max=Number(process.argv[2]); let fd=null,ok=false; try { const before=fs.lstatSync(p); if(!before.isFile()) throw 0; fd=fs.openSync(p,fs.constants.O_RDONLY|(fs.constants.O_NOFOLLOW||0)); const opened=fs.fstatSync(fd); if(!opened.isFile()||opened.dev!==before.dev||opened.ino!==before.ino||opened.size>max) throw 0; const b=Buffer.alloc(opened.size+1); let n=0; while(n<b.length){const r=fs.readSync(fd,b,n,b.length-n,null);if(r===0)break;n+=r;} const after=fs.fstatSync(fd); if(n!==opened.size||after.size!==opened.size) throw 0; process.stdout.write(b.subarray(0,n)); ok=true; } catch {} finally { if(fd!==null){try{fs.closeSync(fd)}catch{ok=false}} } if(!ok)process.exit(2);' "$1" "$2"
}`;
}

export function writePlaybookHelper(taskIdRaw: string, workspace: string = WORKSPACE): void {
  const taskId = shellEscapeDoubleQuoted(taskIdRaw);
  const helperScript = `#!/bin/bash
OP="\${1:?Usage: playbook get|set|list [key] [content]}"
KEY="$2"
VALUE="$3"
API="\${ORCHESTRATOR_URL:?ORCHESTRATOR_URL not set}"
${agentTokenShellFn()}
${jsonStrShellFn()}
_load_agent_token || exit 1
TASK="${taskId}"
${retryPostShellFn()}
if [ "$OP" = "get" ]; then
  if [ -n "$KEY" ]; then
    PAYLOAD=$(printf '{"task_id":%s,"op":"get","key":%s}' \\
      "$(_json_str "$TASK")" "$(_json_str "$KEY")")
    printf '%s' "$PAYLOAD" | \\
      curl -f -sS --max-time 30 -X POST "$API/api/agent/playbook" \\
        -H "Content-Type: application/json" -H "X-Agent-Token: $TOKEN" --data-binary @-
  else
    printf '{"task_id":"%s","op":"get"}' "$TASK" | \\
      curl -f -sS --max-time 30 -X POST "$API/api/agent/playbook" \\
        -H "Content-Type: application/json" -H "X-Agent-Token: $TOKEN" --data-binary @-
  fi
elif [ "$OP" = "set" ]; then
  [ -z "$KEY" ] || [ -z "$VALUE" ] && { echo "Error: set requires <key> and <content>" >&2; exit 1; }
  # Capture branch/HEAD provenance at write time so the backend can stamp it on
  # the entry. Absent (non-git workspace) => omitted => backend keeps it legacy.
  BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || true)
  HEAD_SHA=$(git rev-parse --short HEAD 2>/dev/null || true)
  PAYLOAD=$(printf '{"task_id":%s,"op":"set","key":%s,"value":%s' \\
    "$(_json_str "$TASK")" "$(_json_str "$KEY")" "$(_json_str "$VALUE")")
  if [ -n "$BRANCH" ]; then
    PAYLOAD="\${PAYLOAD},\\"branch\\":$(_json_str "$BRANCH")"
  fi
  if [ -n "$HEAD_SHA" ]; then
    PAYLOAD="\${PAYLOAD},\\"head_sha\\":$(_json_str "$HEAD_SHA")"
  fi
  PAYLOAD="\${PAYLOAD}}"
  printf '%s' "$PAYLOAD" | _post_with_retry "$API/api/agent/playbook" "playbook set"
elif [ "$OP" = "list" ]; then
  printf '{"task_id":"%s","op":"list"}' "$TASK" | \\
    curl -f -sS --max-time 30 -X POST "$API/api/agent/playbook" \\
      -H "Content-Type: application/json" -H "X-Agent-Token: $TOKEN" --data-binary @-
else
  echo "Error: unsupported playbook operation: $OP" >&2
  exit 1
fi
rc=$?
echo ""
exit $rc
`;
  fs.writeFileSync(`${workspace}/playbook`, helperScript, { mode: 0o755 });
  excludeAgentHelpers();
}

export function writeMessagesHelper(
  taskIdRaw: string,
  workspace: string = WORKSPACE,
  options: {
    residentReplyOnly?: boolean;
    replyKeyPath?: string;
    replyReceiptPath?: string;
    stagedReplyPath?: string;
  } = {},
): void {
  const taskId = shellEscapeDoubleQuoted(taskIdRaw);
  const residentReplyOnly = options.residentReplyOnly === true;
  const replyKeyPath = shellEscapeDoubleQuoted(
    (options.replyKeyPath
      ?? path.resolve(workspace, '.resident-reply-idempotency-key')).replace(/\\/g, '/'),
  );
  const replyReceiptPath = shellEscapeDoubleQuoted(
    (options.replyReceiptPath
      ?? path.resolve(workspace, '.resident-reply-receipt.json')).replace(/\\/g, '/'),
  );
  const stagedReplyPath = shellEscapeDoubleQuoted(
    (options.stagedReplyPath
      ?? path.resolve(workspace, '.resident-reply-staged-message.json')).replace(/\\/g, '/'),
  );
  const helperScript = `#!/bin/bash
# Message tool — send messages to the project owner or resolve open messages
# Usage:
#   ./messages send '<content>'                    # send a NOTE message (inline form)
#   ./messages send --stdin [type]                 # quote-safe form; read content from stdin (pipe or heredoc), defaults to NOTE
#   ./messages send '<content>' QUESTION           # send with specific type (NOTE|QUESTION|FEEDBACK|BUG_REPORT|BLOCKER)
#   ./messages resolve <ref> '<resolution>'        # resolve an open message; <ref> may be a
#                                                  #   short ref (first 8+ hex chars of the
#                                                  #   message UUID, as shown in the admin UI)
#                                                  #   or a full UUID — the backend resolves it,
#                                                  #   so no need to recover the full UUID.
# Note: send reads content from stdin so apostrophes, quotes, and newlines are safe.

OP="\${1:?Usage: messages send|resolve ...}"
API="\${ORCHESTRATOR_URL:?ORCHESTRATOR_URL not set}"
${agentTokenShellFn()}
${jsonStrShellFn()}
_load_agent_token || exit 1
TASK="${taskId}"
RESIDENT_REPLY_ONLY="${residentReplyOnly ? '1' : '0'}"
REPLY_KEY_PATH="${replyKeyPath}"
REPLY_RECEIPT_PATH="${replyReceiptPath}"
STAGED_REPLY_PATH="${stagedReplyPath}"
NODE_BIN="\${NODE_BINARY:-node}"
${boundedRegularReadShellFn()}
${retryPostShellFn()}
if [ "$OP" = "send" ]; then
  if [ "\${2:-}" = "--stdin" ]; then
    MSG_TYPE="\${3:-NOTE}"
    CONTENT="$(cat)"
  else
    CONTENT="\${2:?send requires '<content>'}"
    MSG_TYPE="\${3:-NOTE}"
  fi
  [ -n "$CONTENT" ] || { echo "Error: send requires message content" >&2; exit 1; }
  IDEM_KEY=""
  COVERED_IDS=""
  if [ -e "$REPLY_RECEIPT_PATH" ] || [ -L "$REPLY_RECEIPT_PATH" ]; then
    if [ "$RESIDENT_REPLY_ONLY" = "1" ]; then
      echo "[ERROR] send: this is not the Workday operator reply tool; call reply_to_operator instead" >&2
      exit 2
    fi
    RECEIPT=$(_read_regular_bounded "$REPLY_RECEIPT_PATH" 16384) || {
      echo "[ERROR] send: ambient Resident reply receipt is not a safe regular file" >&2; exit 1
    }
    RECEIPT_FIELDS=$("$NODE_BIN" -e 'const r=JSON.parse(process.argv[1]); if(!r || typeof r.idempotency_key!=="string" || !r.idempotency_key || !Array.isArray(r.covered_message_ids) || r.covered_message_ids.length===0 || r.covered_message_ids.some(x=>typeof x!=="string" || !x)) process.exit(2); process.stdout.write(r.idempotency_key+"\\n"+JSON.stringify(r.covered_message_ids))' "$RECEIPT") || {
      echo "[ERROR] send: ambient Resident reply receipt is invalid" >&2; exit 1
    }
    IDEM_KEY=$(printf '%s' "$RECEIPT_FIELDS" | head -n1)
    COVERED_IDS=$(printf '%s' "$RECEIPT_FIELDS" | tail -n +2)
  elif [ -e "$REPLY_KEY_PATH" ] || [ -L "$REPLY_KEY_PATH" ]; then
    echo "[ERROR] send: ambient Resident reply key exists without its receipt" >&2
    exit 1
  fi
  if [ -z "$IDEM_KEY" ]; then
    IDEM_KEY=$(printf '%s' "\${TASK}:\${MSG_TYPE}:\${CONTENT}" | sha256sum | awk '{print $1}')
  fi
  if [ -n "$COVERED_IDS" ]; then
    PAYLOAD=$(printf '{"task_id":%s,"op":"create_message","data":{"type":%s,"content":%s,"idempotency_key":%s,"covered_message_ids":%s}}' \\
      "$(_json_str "$TASK")" "$(_json_str "$MSG_TYPE")" "$(_json_str "$CONTENT")" "$(_json_str "$IDEM_KEY")" "$COVERED_IDS")
    STAGED_DATA=$(printf '{"type":%s,"content":%s,"idempotency_key":%s,"covered_message_ids":%s}' \\
      "$(_json_str "$MSG_TYPE")" "$(_json_str "$CONTENT")" "$(_json_str "$IDEM_KEY")" "$COVERED_IDS")
    [ -n "$STAGED_DATA" ] || { echo "[ERROR] send: failed to serialize staged Resident reply" >&2; exit 1; }
    "$NODE_BIN" -e 'const fs=require("fs"); const p=process.argv[1]; const d=JSON.parse(process.argv[2]); const t=p+".tmp."+process.pid; try { fs.writeFileSync(t,JSON.stringify(d),{encoding:"utf8",flag:"wx",mode:0o600}); fs.linkSync(t,p); } finally { try { fs.unlinkSync(t); } catch {} }' "$STAGED_REPLY_PATH" "$STAGED_DATA" || {
      echo "[ERROR] send: failed to stage Resident reply" >&2; exit 1
    }
    printf '{"status":"staged","op":"create_message"}'
    echo ""
    exit 0
  else
    PAYLOAD=$(printf '{"task_id":%s,"op":"create_message","data":{"type":%s,"content":%s,"idempotency_key":%s}}' \\
      "$(_json_str "$TASK")" "$(_json_str "$MSG_TYPE")" "$(_json_str "$CONTENT")" "$(_json_str "$IDEM_KEY")")
  fi
  [ -n "$PAYLOAD" ] || {
    echo "[ERROR] send: failed to serialize message payload" >&2; exit 1
  }
  printf '%s' "$PAYLOAD" | _post_with_retry "$API/api/agent/strategic" "messages $OP"
elif [ "$OP" = "resolve" ]; then
  if [ "$RESIDENT_REPLY_ONLY" = "1" ]; then
    echo "[ERROR] resolve is disabled for Resident sessions; use ./messages send so the runtime can commit the reply and mark the operator message handled atomically" >&2
    exit 2
  fi
  MSG_ID="\${2:?resolve requires <message-id>}"
  RESOLUTION="\${3:-Addressed}"
  PAYLOAD=$(printf '{"task_id":%s,"op":"resolve_message","data":{"message_id":%s,"resolution":%s}}' \\
    "$(_json_str "$TASK")" "$(_json_str "$MSG_ID")" "$(_json_str "$RESOLUTION")")
  printf '%s' "$PAYLOAD" | _post_with_retry "$API/api/agent/strategic" "messages $OP"
else
  echo "Unknown op: $OP. Use send or resolve." >&2; exit 1
fi
rc=$?
echo ""
exit $rc
`;
  fs.writeFileSync(`${workspace}/messages`, helperScript, { mode: 0o755 });
  excludeAgentHelpers();
}

// ── Symbol navigation helper ──────────────────────────────────────────────────
// Reads the ctags JSON index built at workspace-setup time (standard handler).
// Gracefully handles missing index — falls back with an informational message.
export function writeSymbolsHelper(taskIdRaw: string, workspace: string = WORKSPACE): void {
  const taskId = shellEscapeDoubleQuoted(taskIdRaw);
  const helperScript = `#!/bin/bash
# Symbol navigation — look up function/class/variable definitions via ctags index.
# Usage: ./symbols <name>         — find all definitions matching the given name
#        ./symbols <name> <kind>  — filter by kind (function, class, variable, etc.)
# Returns: file path, line number, and symbol kind (up to 10 results).
# Falls back gracefully when no ctags index is available for this task.

NAME="\${1:?Usage: ./symbols <name> [kind]}"
KIND="\${2:-}"
INDEX="/tmp/symbols_${taskId}.json"

if [ ! -f "$INDEX" ]; then
  echo "[symbols] No symbol index available (ctags index not found at $INDEX)."
  echo "[symbols] Use Grep/Glob to locate symbols manually."
  exit 0
fi

if [ -n "$KIND" ]; then
  jq -r --arg n "$NAME" --arg k "$KIND" \\
    'select(.name == $n and .kind == $k) | "\\(.path):\\(.line)  \\(.kind)  \\(.name)"' \\
    "$INDEX" | head -10
else
  jq -r --arg n "$NAME" \\
    'select(.name == $n) | "\\(.path):\\(.line)  \\(.kind)  \\(.name)"' \\
    "$INDEX" | head -10
fi
`;
  fs.writeFileSync(`${workspace}/symbols`, helperScript, { mode: 0o755 });
  excludeAgentHelpers();
}

// ── Viewport snapshot upload helper ──────────────────────────────────────────
// Writes a `./viewport-snapshot` wrapper that bakes in the task ID and passes
// --upload so the manifest is written to /tmp/viewport_manifest_${TASK_ID}.json,
// which pr-helper.ts reads to render the <!-- blueprint:screenshots --> block.
// Only provisioned for Blueprint self-repo write tasks (create/fix/refactor).
export function writeViewportSnapshotHelper(taskIdRaw: string, workspace: string = WORKSPACE): void {
  const taskId = shellEscapeDoubleQuoted(taskIdRaw);
  const helperScript = `#!/bin/bash
# Capture before/after viewport screenshots and upload them so the PR body
# renders the <!-- blueprint:screenshots --> section.
# Delegates to scripts/viewport-snapshot.sh with the task ID baked in.
TASK_ID="${taskId}"
exec bash scripts/viewport-snapshot.sh --upload --task-id "$TASK_ID" "$@"
`;
  fs.writeFileSync(`${workspace}/viewport-snapshot`, helperScript, { mode: 0o755 });
  excludeAgentHelpers();
}

// ── Live viewport capture helper ─────────────────────────────────────────────
// Writes a `./viewport-live` wrapper that bakes in the task ID and passes
// --upload so scripts/viewport-live.sh builds the SPA against the live backend
// (via the firebase.ts token bypass) and uploads the rendered screenshots under
// /tmp/viewport_live/. Only provisioned for Blueprint self-repo write tasks.
export function writeViewportLiveHelper(taskIdRaw: string, workspace: string = WORKSPACE): void {
  const taskId = shellEscapeDoubleQuoted(taskIdRaw);
  const helperScript = `#!/bin/bash
# Build the admin-UI SPA against the LIVE backend and capture full-page
# screenshots of the rendered dashboard with real data (no interactive OAuth).
# Delegates to scripts/viewport-live.sh with the task ID baked in.
TASK_ID="${taskId}"
exec bash scripts/viewport-live.sh --upload --task-id "$TASK_ID" "$@"
`;
  fs.writeFileSync(`${workspace}/viewport-live`, helperScript, { mode: 0o755 });
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
export function writeDesignDocHelper(
  taskIdRaw: string,
  workspace: string = WORKSPACE,
  opts: { readOnly?: boolean } = {},
): void {
  const taskId = shellEscapeDoubleQuoted(taskIdRaw);
  // Read-only mode (research / plan workspaces): the prompt only promises
  // `./design-doc get|list`, and these agents must never mutate durable
  // strategic state. We provision the read subset and reject set/delete with a
  // clear message instead of silently shipping a write path they shouldn't use.
  const readOnly = opts.readOnly === true;
  const usageComment = readOnly
    ? `#   ./design-doc get <key>                — read a section
#   ./design-doc list                     — list all section keys`
    : `#   ./design-doc get <key>                — read a section
#   ./design-doc set <key> '<json-value>' — write a section (durable: retries on 429/5xx)
#   ./design-doc delete <key>             — remove a section (durable: retries on 429/5xx)
#   ./design-doc list                     — list all section keys`;
  const usageLine = readOnly ? 'get|list' : 'get|set|delete|list';

  // Durable POST for write operations (set/delete). Routes through the shared
  // _post_with_retry wrapper (4 attempts, 5s/15s/30s backoff, Retry-After honored)
  // so 429 (lock contention) and 5xx (transient) are never silently dropped.
  // Non-retryable 4xx responses surface immediately.
  const retryFn = readOnly ? '' : retryPostShellFn();
  // Payload builders use python3 with sys.argv to pass all values — no printf
  // format-string substitution, no shell-quoting escape layers. Any character
  // (double-quote, backslash, newline, %) in KEY or VALUE reaches python3 as a
  // plain sys.argv element and is serialised correctly by json.dumps / json.loads.
  const writeBranches = readOnly ? `elif [ "$OP" = "set" ] || [ "$OP" = "delete" ]; then
  echo "Error: ./design-doc is read-only in this workspace ($OP not permitted)." >&2; exit 1` : `elif [ "$OP" = "set" ]; then
  if [ -z "$KEY" ] || [ -z "$VALUE" ]; then
    echo "Error: set requires <key> and <json-value>" >&2; exit 1
  fi
  python3 -c 'import json,sys;t,k,v=sys.argv[1],sys.argv[2],sys.argv[3];sys.stdout.write(json.dumps({"task_id":t,"op":"set","key":k,"value":json.loads(v)}))' "$TASK" "$KEY" "$VALUE" | \\
    _post_with_retry "$API/api/agent/design-doc" "design-doc set"
elif [ "$OP" = "delete" ]; then
  if [ -z "$KEY" ]; then
    echo "Error: delete requires <key>" >&2; exit 1
  fi
  python3 -c 'import json,sys;t,k=sys.argv[1],sys.argv[2];sys.stdout.write(json.dumps({"task_id":t,"op":"delete","key":k}))' "$TASK" "$KEY" | \\
    _post_with_retry "$API/api/agent/design-doc" "design-doc delete"`;
  const unknownUsage = readOnly ? 'Use get or list.' : 'Use get, set, delete, or list.';
  const helperScript = `#!/bin/bash
# Design doc wiki tool — updates pages incrementally via API
# Reads ORCHESTRATOR_URL and AGENT_API_TOKEN from env (no secrets on disk)
# Usage:
${usageComment}

OP="\${1:?Usage: design-doc ${usageLine} <key> [json-value]}"
KEY="$2"
VALUE="$3"
API="\${ORCHESTRATOR_URL:?ORCHESTRATOR_URL not set}"
${agentTokenShellFn()}
_load_agent_token || exit 1
TASK="${taskId}"
command -v python3 >/dev/null 2>&1 || { echo "[ERROR] design-doc requires python3" >&2; exit 1; }

# Fail-fast POST for read operations (list/get): single curl, no retry.
_post_once() {
  _load_agent_token || return 1
  curl -f -sS --max-time 30 --connect-timeout 10 \\
    -X POST "$API/api/agent/design-doc" \\
    -H "Content-Type: application/json" \\
    -H "X-Agent-Token: $TOKEN" \\
    --data-binary @-
}
${retryFn}
if [ "$OP" = "list" ]; then
  python3 -c 'import json,sys;sys.stdout.write(json.dumps({"task_id":sys.argv[1],"op":"list"}))' "$TASK" | _post_once
elif [ "$OP" = "get" ]; then
  if [ -z "$KEY" ]; then
    echo "Error: get requires <key>" >&2; exit 1
  fi
  python3 -c 'import json,sys;t,k=sys.argv[1],sys.argv[2];sys.stdout.write(json.dumps({"task_id":t,"op":"get","key":k}))' "$TASK" "$KEY" | _post_once
${writeBranches}
else
  echo "Unknown op: $OP. ${unknownUsage}" >&2; exit 1
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

// ── Skill helpers (project-declared from .blueprint/tools.json) ──────────────

/**
 * A project-declared skill entry from `.blueprint/tools.json`.
 * Each skill describes a shell-command procedure the project's agent can invoke.
 */
export interface TaskSkill {
  slug: string;
  title: string;
  command: string;
  description?: string;
}

/** Slug validation: only lowercase alphanumeric + hyphens, 1-64 chars, no leading hyphen. */
const SKILL_SLUG_RE = /^[a-z0-9][a-z0-9-]{0,63}$/;

/**
 * Render a `## Project Skills` markdown block from the task's skills array.
 * Each skill is shown as a compact reference: title, description (if any),
 * and how to invoke it. Returns '' when the input is undefined/empty so
 * the prompt stays unchanged for tasks with no skills declared.
 */
export function formatSkills(skills: TaskSkill[] | undefined): string {
  if (!skills || skills.length === 0) return '';
  const lines = skills.map(skill => {
    const slug = typeof skill.slug === 'string' ? skill.slug : '';
    const title = typeof skill.title === 'string' ? skill.title : slug;
    const command = typeof skill.command === 'string' ? skill.command : '';
    const description = typeof skill.description === 'string' ? skill.description.trim() : '';
    const parts = [`### ${title}`];
    if (description) parts.push(description);
    parts.push(`- Run: \`skill-${slug}\` (or directly: \`${command}\`)`);
    return parts.join('\n\n');
  }).join('\n\n');
  return `## Project Skills\n\nThese are project-declared shell procedures available in this workspace as executable scripts. Use them for project-specific operations (deploy, test, lint, format, etc.).\n\n${lines}\n\n`;
}

export interface TaskToolContent {
  slug: string;
  title: string;
  kind: string;
  text: string;
}

/**
 * Render a `## Project Tool Content` markdown block from the task's
 * project_tools array — the content of the project's built-in tools
 * (notes, whiteboard) that the operator wrote in the workspace UI.
 * Each tool becomes a titled subsection with its serialized text.
 * Returns '' when the input is undefined/empty so the prompt stays
 * unchanged for tasks whose project has no tool content.
 */
export function formatProjectTools(tools: TaskToolContent[] | undefined): string {
  if (!tools || tools.length === 0) return '';
  const blocks = tools.map(tool => {
    const slug = typeof tool.slug === 'string' ? tool.slug : '';
    const title = typeof tool.title === 'string' ? tool.title : slug;
    const kind = typeof tool.kind === 'string' ? tool.kind : '';
    const text = typeof tool.text === 'string' ? tool.text.trim() : '';
    if (!text) return '';
    const heading = kind ? `### ${title} (${kind})` : `### ${title}`;
    return `${heading}\n\n${text}`;
  }).filter(block => block.length > 0);
  if (blocks.length === 0) return '';
  return `## Project Tool Content\n\nThe operator has written the following in this project's built-in tools. Treat it as project-specific context (notes, hypotheses, sketches) that may inform your work.\n\n${blocks.join('\n\n')}\n\n`;
}

/**
 * Write each project skill as an executable ``skill-<slug>`` bash script in the
 * workspace. Validates that each skill's slug is a safe shell token (alphanumeric
 * + hyphens) before writing. Skips invalid or missing entries silently.
 */
export function writeSkillHelpers(
  skills: Record<string, unknown>[] | undefined,
  workspace: string = WORKSPACE,
): void {
  if (!skills || skills.length === 0) return;
  const addedPatterns: string[] = [];
  for (const raw of skills) {
    const slug = typeof raw.slug === 'string' ? raw.slug : '';
    const command = typeof raw.command === 'string' ? raw.command : '';
    if (!slug || !SKILL_SLUG_RE.test(slug)) continue;
    if (!command) continue;
    const title = typeof raw.title === 'string' ? raw.title : slug;
    const script = `#!/bin/bash\n# Project skill: ${title}\nset -e\n${command}\n`;
    const filePath = `${workspace}/skill-${slug}`;
    try {
      fs.writeFileSync(filePath, script, { mode: 0o755 });
      addedPatterns.push(`/skill-${slug}`);
    } catch { /* skip — workspace may not be writable yet */ }
  }
  // Exclude all skill-* scripts from git so they don't get committed.
  if (addedPatterns.length > 0) {
    const excludePath = `${workspace}/.git/info/exclude`;
    try {
      const existing = fs.existsSync(excludePath) ? fs.readFileSync(excludePath, 'utf-8') : '';
      const linesSet = new Set(existing.split('\n'));
      // Also add a glob pattern so future skill scripts are auto-excluded.
      if (!linesSet.has('/skill-*')) {
        fs.appendFileSync(excludePath, '/skill-*\n');
      }
      const missing = addedPatterns.filter(p => !linesSet.has(p));
      if (missing.length > 0) {
        fs.appendFileSync(excludePath, missing.join('\n') + '\n');
      }
    } catch { /* .git/info may not exist */ }
  }
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
export const SELF_REVIEW_PROMPT_VERSION = "8";

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
  for pure-docs tasks. For \`admin-ui/\` changes this includes both
  the happy-dom unit suite via \`npx vitest run\` (equivalently
  \`npm run test:run\`) on every invocation AND the browser-mode
  \`npm run test:viewport\` gate (scoped to changed components).
- \`./check test\` — same plus a **lightweight standalone backend test
  pass** (~10 invariant/contract files, ~30s). This VM is 8 GB; the FULL Docker
  + Postgres pytest suite OOMs (exit 137) here. CI runs the full suite
  on the PR after \`./pr open\`, so this local gate is just a smoke
  check — file-size guardrails, security regs, pipeline invariants.
  **For full suite runs use
  \`bash scripts/test-backend.sh --standalone --no-postgres\`** — calling
  \`scripts/test-backend.sh\` with no test-file argument runs the full
  suite and OOMs. Specific test files are fine with default Postgres:
  \`bash scripts/test-backend.sh testing/test_<module>.py\` fits in
  headroom (the memory guard auto-exempts single-file runs).
`;

export const SHARED_LIFECYCLE_REMAINDER = `- \`./pr open --title "<title>" --body "<body>"\` — gates on \`./check\`, commits
  changes, pushes branch, creates PR. Writes /tmp/pr_url_<task_id> on success.
  Pass \`--with-tests\` to run \`./check test\` instead. **Exit 4 = conflicts.**
  Rebase onto origin/<base>, resolve via Edit or \`git checkout --theirs\`,
  \`./check\`, then \`./pr open\` again. If unresolved after ~2 attempts, \`./blocked\`.
- \`./done\` — marks COMPLETED with the PR URL. Requires \`./pr open\` first.
  - \`./done --already-done "<reason>"\` — Deliverable EXISTS on base branch.
    Reason MUST name concrete artifact (≥30 chars), e.g.
    "migration 0133 + EXPECTED_SCHEMA_REVISION on base via sibling PR #6978".
    Does NOT require \`./pr open\`.
  - \`./done --warn-out-of-scope "<reason>"\` — \`./check\` fails on
    pre-existing issues in your edited files (not your changes). Reason MUST be
    specific (≥30 chars): gates, files, why unfixable. Posts FEEDBACK message.
- \`./fail "<reason>"\` — Task fundamentally can't complete (wrong scope,
  conflicting requirements, unknown approach). NOT for transient lint/test
  failures — iterate with \`./check\`.
- For \`GATE-ENVIRONMENT\`/local time or tool limits, do NOT call \`./blocked\`:
  run cheapest faithful checks, \`./pr open\`, \`./done\`; CI is authoritative.
  A real assertion/lint/build/test failure still blocks publication and must be fixed.
- \`./blocked "<reason>"\` — Only when no code edit can fix it; routes to strategic.

Exit: edit -> ./check -> ./pr open -> ./done

## You MUST end every run with a terminal verb

ALWAYS call exactly ONE: \`./done\`, \`./done --already-done\`, \`./fail\`, or
\`./blocked\`. There is NO external monitor that will resume this session —
run ends when you emit a final message. Phrases like "I'll wait for the monitor"
cause the task to be marked not-shipped. DO \`git status\`/\`git diff\`.
DON'T \`git commit\`/\`git push\`/\`gh pr create\`.

## Required: self-review before \`./done\` (code-change tasks)

Write structured self-review JSON to \`/tmp/self_review_<task_id>.json\`.
Fields: \`check_results\` (category → {passed, reasoning}), \`blind_spots\`,
\`fixes_applied\`, \`edges_considered\`, \`truncated_files_read\`.

Categories:
<!-- SR_CATEGORIES_START -->
- \`correctness\`
- \`error-handling\`
- \`security\`
- \`style-conventions\`
- \`type-safety\`
- \`documentation\`
- \`performance\`
- \`architecture\`
- \`test-coverage\`
- \`scope-drift\`
- \`regression_risk\`
- \`cross-system-contract\`
<!-- SR_CATEGORIES_END -->

**Scope-drift (required):** TWO triggers → FAIL if EITHER. Trigger A: \`git diff --stat HEAD\` (small≤50/2, medium≤200/5, large≤800/15 lines/files). Trigger B: \`git diff --name-only HEAD\` vs related_files. Allow: lockfiles (\`package-lock.json\`, \`poetry.lock\`, \`Cargo.lock\`, \`go.sum\`, \`yarn.lock\`, \`pnpm-lock.yaml\`) and sibling test files (\`.test.ts\`, \`.spec.ts\`, \`test_*.py\`). Format: "modified files outside declared scope: <file> — declared related_files: [...]".

**Regression-risk (conditional):** If \`/tmp/test_baseline_<TASK_ID>.json\` exists, failures not in it are regressions. If absent, omit the baseline check; flag risky when ≥80 source lines + 0 test lines, or removed exports. \`[heuristic-derived]\`.

**Cross-system-contract (required when diff ≥2 of {admin-ui, backend, agent} OR HTTP path changes):** Enumerate URL paths, DTO fields, shared types. grep other sides for matching defs. \`"major"\` for URL/route mismatch, \`"minor"\` for type drift. Else: "diff does not cross system boundaries".

**Reasoning:** Every regressing (Self-Review Blind Spots) category needs a reasoning field ("reasoning") describing what you inspected and the evidence. Required for BOTH pass and fail. \`./done\` enforces.

**Evidence anchors:** Required for high-stakes PASS (\`correctness\`, \`error-handling\`, \`security\`, \`regression_risk\`, \`scope-drift\`). Each PASS needs an evidence anchor: file:line, code snippet (≥10 chars), or named test. \`scope-drift\`: cite A+B results. No anchor → FAIL. lower-stakes need no anchors.

\`fixes_applied: true\` = caught/fixed issues. File MUST exist (may degrade to \`{"verdict":"unknown"}\`).

**\`blind_spots\`:** Unverified risks (NOT CI/scope decisions). Empty if none.

**Optional:** \`truncated_files_read\` = subset re-read. \`["incomplete_review"]\` when diff truncated.

**Fresh-reviewer pass:** May add \`reviewer_findings\` (overrides passing). Auto-fires for \`"incomplete_review"\`.
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
    const cmds = [effectiveLintCmd(d), effectiveBuildCmd(d)].filter((c): c is string => !!c);
    return cmds.length
      ? `\`${path}/\` (${cmds.map(c => `\`${c}\``).join(', ')})`
      : `\`${path}/\``;
  });
  const dirList = dirParts.length ? dirParts.join(', ') : 'the configured project directories';
  const testParts = dirs
    .map(d => ({ path: d.path, cmd: effectiveTestCmd(d) }))
    .filter((x): x is { path: string; cmd: string } => !!x.cmd)
    .map(x => `\`${x.path.replace(/\/$/, '')}/\` (\`${x.cmd}\`)`);
  const testList = testParts.length ? testParts.join(', ') : 'any configured test commands';
  // Mirrors buildPytestGateInner in prompts/check-helper.ts: a pytest-style
  // test command gets the honest gate (sanctioned-harness preference +
  // GATE-ENVIRONMENT fallback), so tell the agent what that note means.
  const effectiveTestCmds = dirs.map(d => effectiveTestCmd(d)).filter((c): c is string => !!c);
  const hasPytestGate = effectiveTestCmds.some(c => isPytestCmd(c));
  const pytestGateNote = hasPytestGate
    ? `  A pytest-style test command runs honestly: \`./check test\` prefers the
  project's Docker test harness (\`scripts/test-<dir>.sh\`) when Docker can
  run it; if this VM can faithfully run neither the harness nor a
  Postgres-backed pytest, the gate logs a GATE-ENVIRONMENT note instead of
  failing on pre-existing environment-only failures. When you see that
  note, record your own faithful test evidence in the PR body — do NOT
  treat the environment artifact as a blocker for validated work.
`
    : '';
  return `- \`./check\` — runs this project's configured lint/build commands for the
  directories your changes touched: ${dirList}. Fast. Exits non-zero on
  failure with the actual error tail; fix and re-run until green. If your
  changes don't touch a configured directory, \`./check\` returns PASS
  without running anything — that's expected for pure-docs tasks.
- \`./check test\` — same plus this project's configured test commands
  (${testList}). This local gate is a smoke check; CI runs the full suite
  on the PR after \`./pr open\`.
${pytestGateNote}`;
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
The playbook is shared, mutable knowledge across all agents — more like a forum than an append-only log. Use \`./playbook set <key> <value>\` to save reusable knowledge or overwrite an existing entry, and \`./playbook get\` to review. If an entry led you astray, overwrite it.

**When to save:** after fixing a build/test error, discovering non-obvious configuration, running successful commands, or finding undocumented patterns. Check \`./playbook get\` first to avoid duplicates.

**Before completing your task:** reflect on repo-level knowledge you gained and save it — the next agent starts cold, and one good entry compounds across hundreds of tasks.

\`\`\`bash
./playbook set "key-name" "Concise operational knowledge here."
\`\`\`
`;

export const FIRST_TASK_GUIDANCE = `
## IMPORTANT: First Task on This Repository — Capture What You Learn

This is the first agent task on this repository. There are no existing playbook entries.
Everything you discover through trial and error is NEW knowledge that will benefit every future agent, and verified build/test commands now also flow back into the backend automatically when you run them successfully.

**Before you finish this task, use \`./playbook set\` to record:**
- The exact build and test commands that worked (e.g. \`npm run build\`, \`pytest\`, \`make test\`)
- Any linting or type-check quirks (config files, flags, common false positives)
- Dependency installation steps or version constraints discovered
- Non-obvious directory structure or naming conventions
- Any workarounds required to get things running

These are still high-value entries — the structured backend write is the durable path, and saving even 2–3 facts now will save future agents hours of rediscovery.

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

/**
 * Render a "## Stale-premise notice" provenance block from a finding-derived
 * task's `_provenance_warnings`. Research findings spawn implementing tasks
 * (create/fix/refactor/docs/qa/deps and review→fix) that often run long after
 * the finding was generated — by which time the cited code may have moved, the
 * citations may have been broken/ungrounded at ingestion, or the integration
 * branch may have advanced. This block names which signals fired and instructs
 * the agent to REPRODUCE-FIRST against the current checkout before implementing.
 *
 * Returns '' when `_provenance_warnings` is absent or empty, so the prompt (and
 * its fingerprint) is byte-unchanged for tasks that carry no warnings. Reads
 * every field defensively — the dict is optional and duck-typed, and every key
 * inside it is optional.
 */
export function formatProvenanceWarnings(
  context: Record<string, unknown> | undefined,
): string {
  const warnings = context?._provenance_warnings;
  if (!warnings || typeof warnings !== 'object' || Array.isArray(warnings)) return '';
  const w = warnings as Record<string, unknown>;

  const signals: string[] = [];

  if (w.cited_files_changed_since_generation === true) {
    signals.push(
      '- **Cited files changed since the finding was generated.** The integration branch ' +
      'has advanced — the code this finding points at may have moved, been refactored, or ' +
      'already been fixed.',
    );
  }

  const brokenCount = typeof w.broken_source_ref_count === 'number' ? w.broken_source_ref_count : 0;
  const brokenRefs = Array.isArray(w.broken_refs)
    ? (w.broken_refs as unknown[]).map(r => strVal(r)).filter(Boolean)
    : [];
  if (brokenCount > 0 || brokenRefs.length > 0) {
    const n = brokenCount || brokenRefs.length;
    const refList = brokenRefs.length > 0 ? `: ${brokenRefs.slice(0, 20).join(', ')}` : '';
    signals.push(
      `- **${n} broken/missing citation(s)${refList}.** ` +
      'These references did not resolve to real code when the research was ingested.',
    );
  }

  const ungroundedCount = typeof w.ungrounded_claim_count === 'number' ? w.ungrounded_claim_count : 0;
  const ungroundedRefs = Array.isArray(w.ungrounded_refs)
    ? (w.ungrounded_refs as unknown[]).map(r => strVal(r)).filter(Boolean)
    : [];
  if (ungroundedCount > 0 || ungroundedRefs.length > 0) {
    const n = ungroundedCount || ungroundedRefs.length;
    const refList = ungroundedRefs.length > 0 ? `: ${ungroundedRefs.slice(0, 20).join(', ')}` : '';
    signals.push(
      `- **${n} ungrounded claim(s)${refList}.** ` +
      'These assertions were not tied to verifiable source when the research was ingested.',
    );
  }

  if (signals.length === 0) return '';

  return (
    `## Stale-premise notice\n` +
    `This task was derived from a research finding whose provenance was flagged. ` +
    `The finding's premise may no longer hold against the current checkout:\n\n` +
    `${signals.join('\n')}\n\n` +
    `**REPRODUCE-FIRST against the current HEAD before implementing.** Verify the cited code ` +
    `still exists and that the finding's problem actually reproduces now. If it does, implement ` +
    `the fix as described. If the premise no longer holds — the cited code is gone, already ` +
    `fixed, or the problem cannot be reproduced — do NOT fabricate a change to match the finding: ` +
    `cleanly no-op with \`./done --already-done "<what you verified>"\`, or pivot to the real ` +
    `current issue if one is evident. A stale finding is a signal to investigate, not a mandate ` +
    `to invent a diff.\n\n`
  );
}

/**
 * Render a citation-provenance note for an INVESTIGATE task spawned from a
 * bug-report message. The originating message's reproduce-on-HEAD grounding
 * signal is carried onto the task's `context._provenance_warnings` as
 * `{citation_state, broken_refs, ungrounded_refs}`. The strategist inbox, the
 * strategist prompt, and the admin message UI already surface this signal; this
 * brings the investigating agent to the same standard so it can see exactly
 * which cited refs did not resolve on HEAD at the decision-critical moment.
 *
 * Two framings, by `citation_state`:
 *  - `absent` (every cited ref gone): a STRONGER discount note — the defect may
 *    already be fixed/refactored away; verify it still reproduces before
 *    investing and discount actionability.
 *  - non-absent with broken/ungrounded refs (partially grounded): a SOFTER note
 *    — the defect is NOT presumed resolved and remains actionable; verify these
 *    specific refs during reproduce-first and treat them as stale.
 *
 * Returns '' when no signal is present (no `_provenance_warnings`, or a clean
 * verdict with no stale refs), so the static investigate prompt fingerprint is
 * unaffected. Reads every field defensively — the dict and its keys are
 * optional and duck-typed. This is a read-only task (the agent writes a JSON
 * report, not a diff), so there is no `./done --already-done` off-ramp.
 */
export function formatInvestigateCitationProvenance(
  context: Record<string, unknown> | undefined,
): string {
  const warnings = context?._provenance_warnings;
  if (!warnings || typeof warnings !== 'object' || Array.isArray(warnings)) return '';
  const w = warnings as Record<string, unknown>;

  const brokenRefs = Array.isArray(w.broken_refs)
    ? (w.broken_refs as unknown[]).map(r => strVal(r)).filter(Boolean)
    : [];
  const ungroundedRefs = Array.isArray(w.ungrounded_refs)
    ? (w.ungrounded_refs as unknown[]).map(r => strVal(r)).filter(Boolean)
    : [];
  const staleRefs = [...brokenRefs, ...ungroundedRefs];

  if (w.citation_state === 'absent') {
    const refList = staleRefs.length > 0 ? ` (${staleRefs.slice(0, 20).join(', ')})` : '';
    return (
      `\n\n## Citation-provenance notice\n` +
      `The originating bug report cites code that is ABSENT on the current ` +
      `integration branch${refList}. The defect may already be fixed or ` +
      `refactored away — verify it still reproduces on HEAD before investing, ` +
      `and discount its actionability accordingly.\n`
    );
  }

  if (staleRefs.length > 0) {
    const refList = staleRefs.slice(0, 20).join(', ');
    return (
      `\n\n## Citation-provenance notice\n` +
      `The originating bug report is only PARTIALLY grounded on the current ` +
      `integration branch: some cited references are stale (${refList}). The ` +
      `defect itself is NOT presumed resolved — it remains actionable. Verify ` +
      `these specific references during reproduce-first and treat them as ` +
      `outdated, but do not discount the defect on their account.\n`
    );
  }

  return '';
}

const ACCEPTANCE_TEST_DIR_SEGMENTS = new Set([
  'tests',
  'testing',
  'test',
  '__tests__',
  '__test__',
  'spec',
]);

const ACCEPTANCE_TEST_FILE_BASENAME_RE = /^(?:test_.*\.py|.*_test\.py|.*\.test\.[cm]?[jt]sx?|.*\.spec\.[cm]?[jt]sx?|.*_test\.go|.*_test\.exs?|.*_spec\.rb|test_.*\.rb)$/i;

function looksLikeAcceptanceTestFile(path: string): boolean {
  const candidate = path.trim();
  if (!candidate) return false;
  const segments = candidate.replace(/\\/g, '/').split('/').filter(Boolean);
  if (segments.length === 0) return false;
  const basename = segments[segments.length - 1];
  if (!basename.includes('.')) return false;
  const inTestDir = segments.slice(0, -1).some(seg => ACCEPTANCE_TEST_DIR_SEGMENTS.has(seg.toLowerCase()));
  return inTestDir || ACCEPTANCE_TEST_FILE_BASENAME_RE.test(basename.toLowerCase());
}

function formatAcceptanceNamedTest(evidence: Record<string, unknown>): string {
  const rawTestPath = typeof evidence.test_path === 'string' ? evidence.test_path.trim() : '';
  if (!rawTestPath) return '';
  const [declaredPath, inlineTestId = ''] = rawTestPath.split('::', 2);
  if (!looksLikeAcceptanceTestFile(declaredPath)) return '';

  const explicitTestId = typeof evidence.test_id === 'string' ? evidence.test_id.trim() : '';
  const testId = explicitTestId || inlineTestId.trim();
  const target = testId ? ` and target \`${testId}\`` : '';

  return (
    `- named_test: add or modify \`${declaredPath}\`${target}. ` +
    `The test must run green under a real merge-test verdict, and the PR must also ` +
    `change non-test production code that the test exercises. A test-only diff fails ` +
    `the gate; an always-true assertion in isolation fails.`
  );
}

/**
 * Render a concrete acceptance-evidence obligation block from the first payload.
 *
 * Returns '' when the field is absent or malformed so the prompt stays byte-for-
 * byte identical for tasks that do not declare acceptance evidence.
 */
export function formatAcceptanceEvidence(
  firstPayload: Record<string, unknown> | undefined,
): string {
  if (!firstPayload || typeof firstPayload !== 'object' || Array.isArray(firstPayload)) return '';
  const rawEvidence = firstPayload.acceptance_evidence;
  if (!rawEvidence || typeof rawEvidence !== 'object' || Array.isArray(rawEvidence)) return '';

  const evidence = rawEvidence as Record<string, unknown>;
  const kind = typeof evidence.kind === 'string' ? evidence.kind.trim() : '';
  if (!kind) return '';

  if (kind === 'named_test') {
    const namedTest = formatAcceptanceNamedTest(evidence);
    if (!namedTest) return '';
    return `## Acceptance Evidence\nThis task declared a plan-time acceptance requirement. Carry it through explicitly:\n${namedTest}\n\n`;
  }

  if (kind === 'observable_behavior') {
    const description = typeof evidence.description === 'string' ? evidence.description.trim() : '';
    if (description.length < 10) return '';
    return (
      `## Acceptance Evidence\nThis task declared a plan-time acceptance requirement. Carry it through explicitly:\n` +
      `- observable_behavior: deliver the described behavior: \`${description}\`. The PR must ` +
      `ship a real, non-empty diff.\n\n`
    );
  }

  if (kind === 'downstream_artifact') {
    const artifactId = typeof evidence.artifact_id === 'string' ? evidence.artifact_id.trim() : '';
    if (!artifactId) return '';
    return (
      `## Acceptance Evidence\nThis task declared a plan-time acceptance requirement. Carry it through explicitly:\n` +
      `- downstream_artifact: produce or deliver \`${artifactId}\`. The PR must ship a real, non-empty diff.\n\n`
    );
  }

  return '';
}
