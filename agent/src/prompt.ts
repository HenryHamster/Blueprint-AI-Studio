import fs from 'fs';
import { execFileSync } from 'child_process';
import { WORKSPACE } from './index.js';
import type { BuildConfig } from './index.js';
import { formatProjectContext } from './prompts/project-context.js';
import { isReadOnlyTaskType } from './taskCapabilities.js';
import { parseBackendTimestamp } from './utils/time.js';

// ── Re-exports from sub-modules (facade) ────────────────────────────────────
// The bulky helpers have been split out into prompts/* modules so that small
// prompt edits don't keep reshaping a 2k-line file (which causes painful merge
// conflicts in the self-improving pipeline). Callers continue to import from
// './prompt.js' unchanged.
export {
  buildFileTree,
  extractTestConventions,
  discoverRelatedTests,
  discoverModuleInterfaces,
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
  'perf': 'Profile before optimizing. Identify the specific bottleneck with measurements before writing any fix.',
  'create': 'Before finishing: edit the files you need to change, run `./check` to validate lint/build, fix any failures, then `./pr open --title "feat/fix/etc: ..." --body "Summary of changes"` to ship and `./done` to mark complete. The `./pr` helper gates on `./check` so broken code can\'t reach a PR. If a library is unfamiliar, fetch its docs first. If you determine the task can\'t be completed (scope wrong, conflicting requirements, blocker found), call `./fail "<reason>"` instead of leaving silently.',
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
  'plan',             // Uses research-plan.ts bespoke prompt assembly
  'research',         // Uses research-plan.ts bespoke prompt assembly
  'strategic_review', // Reuses research handler with research_mode branch; uses bespoke assembly
  'smoketest',        // Uses specialized.ts bespoke prompt assembly
  'design_doc_update', // Uses design-doc.ts bespoke prompt assembly
]);

// ── Dynamic Task Type Hints (based on project build configuration) ───────────
export function getTaskTypeHints(buildConfig?: BuildConfig): Record<string, string> {
  const dirs = buildConfig?.directories ?? [];
  if (dirs.length === 0) return TASK_TYPE_HINTS;

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
    hints['create'] = `Before finishing: edit the files you need, run \`./check\` to validate lint/build (which uses ${cmdsText} under the hood), fix any failures, then \`./pr open --title "feat/fix/etc: ..." --body "Summary"\` and \`./done\`. The \`./pr\` helper gates on \`./check\`. Use \`./fail "<reason>"\` if the task can't be completed.`;
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

  return hints;
}

export const DEFAULT_TOOL_GUIDANCE = 'Tool guidance: Use Grep/Glob for code search (not Bash grep/find), Read for file contents, Agent tool for parallel exploration of multiple areas. Use TodoWrite to track multi-step work. Use WebFetch to look up API documentation, library references, or framework guides when working with unfamiliar code. Use WebSearch to find solutions for specific error messages or unusual patterns. If you discover bugs, architectural issues, or improvement ideas, use ./note to leave annotations: ./note comment "msg" --page <page>, ./note issue "msg" --severity high, ./note vision "msg". If you hit a barrier, have a question for the project owner, or want to flag something important, use ./messages send \'your message\' (types: NOTE, QUESTION, FEEDBACK, BUG_REPORT, BLOCKER).';

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

Specifically:
1. Read the failing assertion. It will tell you the file path, current
   line count, and the limit it exceeded.
2. Edit \`backend/testing/module_size_limits.json\` (for backend files) or
   \`admin-ui/src/...\` size config (for frontend files). Bump the limit
   for the offending file by enough headroom (current_size + 50-100 lines).
3. Commit both the source change AND the JSON edit in the same PR.

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

// ── Git exclude for agent helper scripts ────────────────────────────────────
// Agent helper scripts (playbook, messages, note, strategic, design-doc) are
// written to WORKSPACE for Claude Code to use. Exclude them from git so they
// don't get committed into PRs via `git add -A`.
export const AGENT_HELPER_NAMES = ['playbook', 'messages', 'note', 'strategic', 'design-doc', 'check', 'pr', 'done', 'fail'];

export function excludeAgentHelpers(): void {
  const excludePath = `${WORKSPACE}/.git/info/exclude`;
  try {
    const existing = fs.existsSync(excludePath) ? fs.readFileSync(excludePath, 'utf-8') : '';
    const lines = existing.split('\n');
    const missing = AGENT_HELPER_NAMES.filter(name => !lines.includes(`/${name}`));
    if (missing.length > 0) {
      const append = missing.map(name => `/${name}`).join('\n') + '\n';
      fs.appendFileSync(excludePath, append);
    }
  } catch { /* .git/info may not exist in shallow clones */ }
}

// ── Playbook helpers ─────────────────────────────────────────────────────────

export function writePlaybookHelper(taskId: string): void {
  const helperScript = `#!/bin/bash
OP="\${1:?Usage: playbook get|set|list|delete <key> [content]}"
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
  jq -n --arg tid "$TASK" --arg k "$KEY" --arg v "$VALUE" \\
    '{task_id: $tid, op: "set", key: $k, value: $v}' | \\
    curl -f -sS --max-time 30 -X POST "$API/api/agent/playbook" \\
      -H "Content-Type: application/json" -H "X-Agent-Token: $TOKEN" --data-binary @-
elif [ "$OP" = "delete" ]; then
  jq -n --arg tid "$TASK" --arg k "$KEY" \\
    '{task_id: $tid, op: "delete", key: $k}' | \\
    curl -f -sS --max-time 30 -X POST "$API/api/agent/playbook" \\
      -H "Content-Type: application/json" -H "X-Agent-Token: $TOKEN" --data-binary @-
elif [ "$OP" = "list" ]; then
  printf '{"task_id":"%s","op":"list"}' "$TASK" | \\
    curl -f -sS --max-time 30 -X POST "$API/api/agent/playbook" \\
      -H "Content-Type: application/json" -H "X-Agent-Token: $TOKEN" --data-binary @-
fi
echo ""
`;
  fs.writeFileSync(`${WORKSPACE}/playbook`, helperScript, { mode: 0o755 });
  excludeAgentHelpers();
}

export function writeMessagesHelper(taskId: string): void {
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
  fs.writeFileSync(`${WORKSPACE}/messages`, helperScript, { mode: 0o755 });
  excludeAgentHelpers();
}

// ── Code-change task helpers (./check, ./pr, ./done, ./fail) ────────────────
//
// These four helpers replace the wrapper-driven lint/test/push/PR pipeline
// for tasks that produce code changes. Claude owns its own loop:
//
//   edit → ./check → see errors → edit → ./check → ... (one Claude session,
//   full task context, intelligent pivots) → ./pr open --title --body
//   → ./done (mark COMPLETED with PR URL)  OR  ./fail "reason"
//
// The wrapper just runs Claude, watches for ./done|./fail marker files,
// and posts status accordingly. No more wrapper-driven lintFixLoop /
// testFixLoop / pushAndCreatePR for these task types.

interface CodeChangeHelperConfig {
  taskId: string;
  /** Working branch name, e.g. ``blueprint/task-abc12345``. */
  workBranch: string;
  baseBranch: string;
  owner: string;
  repo: string;
  /** Existing PR number (review-fix path). When set, ./pr open just pushes
   *  to the existing PR instead of creating a new one. */
  existingPrNumber?: number;
  /** When true, ./check test runs the Docker backend test suite for any
   *  task whose changes touch ``backend/``. Set when base_branch === 'main'. */
  testsOnMainBranch: boolean;
}

/** Write ./check, ./pr, ./done, ./fail to WORKSPACE. Idempotent. */
export function writeCodeChangeHelpers(cfg: CodeChangeHelperConfig): void {
  writeCheckHelper(cfg);
  writePrHelper(cfg);
  writeDoneHelper(cfg);
  writeFailHelper(cfg);
  excludeAgentHelpers();
}

function writeCheckHelper(cfg: CodeChangeHelperConfig): void {
  // Auto-detects which stacks were touched (admin-ui / agent / backend) by
  // inspecting `git diff --name-only` against the base branch + uncommitted.
  // Runs the corresponding lint/build commands. Optional `./check test` also
  // runs the Docker backend test suite when backend/ was touched and
  // tests-on-main is enabled.
  const script = `#!/bin/bash
# ./check — run lint/build (and optionally tests) for changed paths.
# Usage:
#   ./check        # lint+build only — fast, run before each iteration
#   ./check test   # also run tests (slow; mainly before ./pr open)
# Exits 0 on green, non-zero on any failure. Output is the actual error tail
# so you can act on it. Re-run after edits.

set -uo pipefail
WORKSPACE="\${WORKSPACE:-${WORKSPACE}}"
cd "$WORKSPACE" || { echo "Error: cannot cd to $WORKSPACE" >&2; exit 2; }

# Sanity: make sure we're in a git repo. Without this, the diff commands
# below silently swallow errors and CHANGED becomes empty -> all stack
# blocks skipped -> "PASS" with zero actual checks.
git rev-parse --git-dir >/dev/null 2>&1 || {
  echo "Error: $WORKSPACE is not a git repo (missing .git)" >&2
  exit 2
}

WITH_TESTS=0
if [ "\${1:-}" = "test" ] || [ "\${1:-}" = "--tests" ]; then
  WITH_TESTS=1
fi

# Determine changed paths: committed since base + uncommitted. Verify at
# least one diff source actually worked — silently-empty CHANGED used to
# fall through to "PASS — all checks green" without running anything.
BASE="${cfg.baseBranch}"
COMMITTED=""
if git rev-parse --verify "origin/$BASE" >/dev/null 2>&1; then
  COMMITTED=$(git diff --name-only "origin/$BASE...HEAD" 2>/dev/null || true)
elif git rev-parse --verify "$BASE" >/dev/null 2>&1; then
  COMMITTED=$(git diff --name-only "$BASE...HEAD" 2>/dev/null || true)
else
  echo "[check] WARNING: neither origin/$BASE nor $BASE exists locally — falling back to working-tree diff only" >&2
fi
UNCOMMITTED=$(git diff --name-only HEAD 2>/dev/null; git ls-files --others --exclude-standard 2>/dev/null)
CHANGED=$(printf '%s\\n%s\\n' "$COMMITTED" "$UNCOMMITTED" | sort -u | grep -v '^$' || true)

touches() { printf '%s\\n' "$CHANGED" | grep -q "^$1"; }

FAILED=()

run() {
  local label="$1"; shift
  echo "[check] $label..."
  if "$@"; then
    echo "[check] $label OK"
  else
    echo "[check] $label FAILED"
    FAILED+=("$label")
  fi
}

if touches admin-ui/; then
  ( cd admin-ui && [ -d node_modules ] || npm ci ) >/tmp/check_npm_ci.log 2>&1 || {
    echo "[check] admin-ui npm ci FAILED — see /tmp/check_npm_ci.log"
    tail -30 /tmp/check_npm_ci.log
    FAILED+=("admin-ui npm ci")
  }
  run "admin-ui eslint"  bash -c 'cd admin-ui && npx eslint . 2>&1'
  run "admin-ui build"   bash -c 'cd admin-ui && VITE_API_URL=http://localhost:8080 VITE_FIREBASE_API_KEY=ci VITE_FIREBASE_AUTH_DOMAIN=ci.firebaseapp.com VITE_FIREBASE_PROJECT_ID=ci npm run build 2>&1'
fi

if touches agent/; then
  ( cd agent && [ -d node_modules ] || npm ci ) >/tmp/check_agent_npm_ci.log 2>&1 || {
    echo "[check] agent npm ci FAILED — see /tmp/check_agent_npm_ci.log"
    tail -30 /tmp/check_agent_npm_ci.log
    FAILED+=("agent npm ci")
  }
  run "agent tsc"        bash -c 'cd agent && npx tsc --noEmit 2>&1'
  run "agent eslint"     bash -c 'cd agent && npx eslint . 2>&1'
fi

if touches backend/; then
  if command -v ruff >/dev/null 2>&1 || python3 -m ruff --version >/dev/null 2>&1; then
    run "backend ruff"    bash -c 'python3 -m ruff check backend/ --select E,F,W --ignore E501 2>&1'
    run "backend ruff fmt" bash -c 'python3 -m ruff format --check backend/ 2>&1'
  else
    echo "[check] ruff not installed, skipping backend lint"
  fi
  if [ "$WITH_TESTS" = "1" ] && [ "${cfg.testsOnMainBranch ? '1' : '0'}" = "1" ]; then
    # --standalone --no-postgres: agent VM is e2-standard-2 (8 GB) and
    # cannot fit Docker build + Postgres sidecar + full pytest suite +
    # agent + Claude subprocess in memory. The full suite OOMs (exit
    # 137) during the mandatory pre-PR gate (53ef061b, b150b4d6, b15b6813
    # all died this way 2026-05-05). Standalone runs the 3 invariant
    # files (security, pipeline, invariants) which catch file-size
    # breaches, security regressions, and basic pipeline assertions —
    # the rest is CI's job.
    run "backend pytest" bash -c 'bash scripts/test-backend.sh --standalone --no-postgres 2>&1 | tail -200'
  fi
fi

if [ \${#FAILED[@]} -eq 0 ]; then
  echo ""
  echo "[check] PASS — all checks green"
  exit 0
else
  echo ""
  echo "[check] FAIL — \${#FAILED[@]} check(s) failed: \${FAILED[*]}"
  exit 1
fi
`;
  fs.writeFileSync(`${WORKSPACE}/check`, script, { mode: 0o755 });
}

function writePrHelper(cfg: CodeChangeHelperConfig): void {
  // Idempotent push + PR creation/update. Gates on ./check first.
  // Writes /tmp/pr_url_<task_id> on success so the wrapper + ./done can
  // pick up the URL without re-querying GitHub.
  const script = `#!/bin/bash
# ./pr open --title "..." --body "..."  — ship the diff via PR
#
# Gates on ./check (must pass). Then commits any pending changes, pushes
# to ${cfg.workBranch}, and creates a PR (or pushes an update to an
# existing PR for review-fix tasks).
#
# Exits 0 on success (PR URL written to /tmp/pr_url_${cfg.taskId}).
# Non-zero on any failure — error tail printed; fix and retry.

set -uo pipefail
WORKSPACE="\${WORKSPACE:-${WORKSPACE}}"
cd "$WORKSPACE" || { echo "Error: cannot cd to $WORKSPACE" >&2; exit 2; }

OP="\${1:-}"
shift || true

# ── ./pr close --superseded-by <PR#> ─────────────────────────────────────────
# Use when post-rebase conflict resolution leaves an empty diff because an
# equivalent sibling PR already merged the same changes.  Closes the current PR
# cleanly, posts a traceability comment, and writes /tmp/pr_url_<task_id> so
# ./done can mark the task COMPLETED-via-supersession.
if [ "$OP" = "close" ]; then
  SUPERSEDED_BY=""
  PR_NUMBER="${cfg.existingPrNumber !== undefined ? String(cfg.existingPrNumber) : ""}"
  while [ $# -gt 0 ]; do
    case "$1" in
      --superseded-by) SUPERSEDED_BY="$2"; shift 2 ;;
      --pr-number)     PR_NUMBER="$2";     shift 2 ;;
      *) echo "Unknown arg: $1" >&2; exit 2 ;;
    esac
  done

  # Fall back to PR URL written by a prior ./pr open in this session
  if [ -z "$PR_NUMBER" ]; then
    PR_URL_FILE="/tmp/pr_url_${cfg.taskId}"
    if [ -s "$PR_URL_FILE" ]; then
      PR_NUMBER=$(grep -oE '[0-9]+$' "$PR_URL_FILE" || true)
    fi
  fi
  if [ -z "$PR_NUMBER" ]; then
    echo "[pr close] No PR number available. Pass --pr-number <N> or run ./pr open first." >&2
    exit 1
  fi

  API="\${ORCHESTRATOR_URL:?ORCHESTRATOR_URL not set}"
  TOKEN="\${AGENT_API_TOKEN:?AGENT_API_TOKEN not set}"
  TASK="${cfg.taskId}"

  # Refresh GitHub token
  TOKEN_RESP=$(curl -f -sS --max-time 30 \\
    -H "X-Agent-Token: $TOKEN" \\
    "$API/api/agent/github/token?task_id=$TASK") || {
    echo "[pr close] Failed to fetch GitHub token from orchestrator" >&2
    exit 1
  }
  GH_TOKEN=$(printf '%s' "$TOKEN_RESP" | jq -r '.data.token // empty')
  if [ -z "$GH_TOKEN" ]; then
    echo "[pr close] orchestrator returned no token: $TOKEN_RESP" >&2
    exit 1
  fi

  # Close the PR via GitHub API
  CLOSE_HTTP=$(curl -sS -o /tmp/pr_close_resp_${cfg.taskId} -w '%{http_code}' --max-time 30 \\
    -X PATCH "https://api.github.com/repos/${cfg.owner}/${cfg.repo}/pulls/$PR_NUMBER" \\
    -H "Authorization: token $GH_TOKEN" \\
    -H "Accept: application/vnd.github+json" \\
    -d '{"state":"closed"}') || CLOSE_HTTP="000"
  if [ "$CLOSE_HTTP" != "200" ]; then
    echo "[pr close] GitHub PATCH /pulls/$PR_NUMBER returned HTTP=$CLOSE_HTTP:" >&2
    cat /tmp/pr_close_resp_${cfg.taskId} >&2
    rm -f /tmp/pr_close_resp_${cfg.taskId}
    exit 1
  fi
  rm -f /tmp/pr_close_resp_${cfg.taskId}

  # Post supersession comment for traceability
  if [ -n "$SUPERSEDED_BY" ]; then
    COMMENT_BODY=$(jq -n --arg msg "Closing: equivalent changes already merged via PR #$SUPERSEDED_BY. No further action needed on this branch." '{body: $msg}')
    curl -sS --max-time 30 \\
      -X POST "https://api.github.com/repos/${cfg.owner}/${cfg.repo}/issues/$PR_NUMBER/comments" \\
      -H "Authorization: token $GH_TOKEN" \\
      -H "Accept: application/vnd.github+json" \\
      -d "$COMMENT_BODY" >/dev/null 2>&1 || true
  fi
  unset GH_TOKEN

  PR_URL="https://github.com/${cfg.owner}/${cfg.repo}/pull/$PR_NUMBER"
  echo "$PR_URL" > /tmp/pr_url_${cfg.taskId}
  echo ""
  echo "[pr] Closed PR #$PR_NUMBER\${SUPERSEDED_BY:+ (superseded by #$SUPERSEDED_BY)}"
  echo "[pr] Next: ./done"
  exit 0
fi

[ "$OP" != "open" ] && { echo "Usage: ./pr open --title <title> --body <body> [--with-tests]  |  ./pr close [--superseded-by <PR#>]"; exit 2; }

TITLE=""
BODY=""
WITH_TESTS=0
while [ $# -gt 0 ]; do
  case "$1" in
    --title) TITLE="$2"; shift 2 ;;
    --body) BODY="$2"; shift 2 ;;
    --with-tests) WITH_TESTS=1; shift ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

[ -z "$TITLE" ] && { echo "Error: --title is required" >&2; exit 2; }

${cfg.testsOnMainBranch ? `# Mandatory test gate: force WITH_TESTS=1 when the diff touches backend/.
# The agent must not be able to skip backend tests on main-branch tasks.
_BASE_GATE="${cfg.baseBranch}"
_CMT_GATE=""
if git rev-parse --verify "origin/$_BASE_GATE" >/dev/null 2>&1; then
  _CMT_GATE=$(git diff --name-only "origin/$_BASE_GATE...HEAD" 2>/dev/null || true)
elif git rev-parse --verify "$_BASE_GATE" >/dev/null 2>&1; then
  _CMT_GATE=$(git diff --name-only "$_BASE_GATE...HEAD" 2>/dev/null || true)
fi
_UNC_GATE=$(git diff --name-only HEAD 2>/dev/null; git ls-files --others --exclude-standard 2>/dev/null)
_CHG_GATE=$(printf '%s\\n%s\\n' "$_CMT_GATE" "$_UNC_GATE" | sort -u | grep -v '^$' || true)
if printf '%s\\n' "$_CHG_GATE" | grep -q '^backend/'; then
  echo "[pr] backend/ touched on main — forcing WITH_TESTS=1 (mandatory test gate)"
  WITH_TESTS=1
fi
` : ''}
# 1. Run ./check as gate (lint always; tests on main branch when --with-tests
#    OR on review-fix path that touches backend changes).
# Branch on WITH_TESTS instead of expanding an array. Older bash + set -u
# aborts on an empty array expansion, so just call ./check directly.
if [ "$WITH_TESTS" = "1" ]; then
  if ! ./check test; then
    echo ""
    echo "[pr] ./check test failed — fix lint/build/tests then re-run ./pr open."
    exit 1
  fi
else
  if ! ./check; then
    echo ""
    echo "[pr] ./check failed — fix lint/build then re-run ./pr open."
    exit 1
  fi
fi

# 2. Stage and commit any pending changes
if [ -n "$(git status --porcelain)" ]; then
  git add -A
  git -c user.email=blueprint-agent@automated.dev -c user.name='Blueprint Agent' \\
    commit -m "$TITLE" >/dev/null || {
    echo "[pr] git commit failed" >&2
    git status --short
    exit 1
  }
fi

# Anything to push?
COMMITS_AHEAD=$(git rev-list --count "origin/${cfg.baseBranch}..HEAD" 2>/dev/null || echo 0)
if [ "$COMMITS_AHEAD" = "0" ]; then
  echo "[pr] No commits ahead of ${cfg.baseBranch} — nothing to push."
  echo "[pr] If this branch was superseded (a sibling PR already merged the same changes),"
  echo "[pr] run:  ./pr close --superseded-by <winning-PR-number>"
  echo "[pr] Otherwise make your changes and re-run ./pr open."
  exit 1
fi

API="\${ORCHESTRATOR_URL:?ORCHESTRATOR_URL not set}"
TOKEN="\${AGENT_API_TOKEN:?AGENT_API_TOKEN not set}"
TASK="${cfg.taskId}"

# 3. Refresh GitHub token + remote URL right before push. The clone-time token
#    is per-task ephemeral and may be near expiry by the time Claude ships.
TOKEN_RESP=$(curl -f -sS --max-time 30 \\
  -H "X-Agent-Token: $TOKEN" \\
  "$API/api/agent/github/token?task_id=$TASK") || {
  echo "[pr] Failed to fetch fresh GitHub token from orchestrator" >&2
  exit 1
}
GH_PUSH_TOKEN=$(printf '%s' "$TOKEN_RESP" | jq -r '.data.token // empty')
if [ -z "$GH_PUSH_TOKEN" ]; then
  echo "[pr] orchestrator returned no token: $TOKEN_RESP" >&2
  exit 1
fi
git remote set-url origin "https://x-access-token:$GH_PUSH_TOKEN@github.com/${cfg.owner}/${cfg.repo}.git"
unset GH_PUSH_TOKEN  # only needed for the remote URL above

# 4. Push (with retry — concurrent-push handling)
PUSH_OK=0
for attempt in 1 2 3; do
  if git push --force-with-lease origin "${cfg.workBranch}" 2>/tmp/pr_push.log; then
    PUSH_OK=1
    break
  fi
  if grep -q 'stale info' /tmp/pr_push.log; then
    echo "[pr] push attempt $attempt: stale lease, fetching + retrying..."
    git fetch origin "${cfg.workBranch}" 2>/dev/null || true
    sleep 2
  else
    echo "[pr] push attempt $attempt failed:"; cat /tmp/pr_push.log; sleep 2
  fi
done
if [ "$PUSH_OK" = "0" ]; then
  echo "[pr] push failed after 3 attempts" >&2
  exit 1
fi

# 5. Create or update PR via orchestrator-mediated endpoint.
#    Why the orchestrator endpoint instead of \`gh pr create\`:
#    - gh CLI is not installed on agent VMs, and GH_TOKEN isn't in Claude's env.
#    - The endpoint already handles idempotency (returns existing PR if open).
#    - It writes a PULL_REQUEST Artifact row immediately on creation so the
#      reaper can route reaped mid-run tasks to EVALUATING instead of
#      retrying-from-scratch (orphan-PR recovery).
${cfg.existingPrNumber !== undefined ? `
# Existing PR (#${cfg.existingPrNumber}) — review-fix path. Just pushed
# updates; no creation needed. The PULL_REQUEST artifact already exists from
# the original create task, so we don't need to call the endpoint here.
PR_URL="https://github.com/${cfg.owner}/${cfg.repo}/pull/${cfg.existingPrNumber}"
echo "[pr] Pushed updates to existing PR #${cfg.existingPrNumber}"
` : `
PR_PAYLOAD=$(jq -n --arg tid "$TASK" --arg owner "${cfg.owner}" --arg repo "${cfg.repo}" \\
  --arg title "$TITLE" --arg head "${cfg.workBranch}" --arg base "${cfg.baseBranch}" --arg body "$BODY" \\
  '{task_id: $tid, owner: $owner, repo: $repo, title: $title, head: $head, base: $base, body: $body}')
PR_RESP=""
PR_OK=0
for attempt in 1 2 3 4 5; do
  PR_HTTP=$(printf '%s' "$PR_PAYLOAD" | curl -sS -o /tmp/pr_create_resp_${cfg.taskId} -w '%{http_code}' --max-time 90 \\
    -X POST "$API/api/agent/github/pull-requests" \\
    -H "Content-Type: application/json" -H "X-Agent-Token: $TOKEN" --data-binary @-) || PR_HTTP="000"
  if [ "$PR_HTTP" = "200" ] || [ "$PR_HTTP" = "201" ] || [ "$PR_HTTP" = "202" ]; then
    PR_RESP=$(cat /tmp/pr_create_resp_${cfg.taskId})
    PR_OK=1; break
  fi
  if [ "$PR_HTTP" = "429" ] || [ "\${PR_HTTP:0:1}" = "5" ] || [ "$PR_HTTP" = "000" ]; then
    BACKOFF=$((attempt * 3))
    echo "[pr] PR creation attempt $attempt got HTTP=$PR_HTTP — retrying in \${BACKOFF}s..." >&2
    sleep "$BACKOFF"; continue
  fi
  echo "[pr] PR creation attempt $attempt got HTTP=$PR_HTTP (permanent failure):" >&2
  cat /tmp/pr_create_resp_${cfg.taskId} >&2
  rm -f /tmp/pr_create_resp_${cfg.taskId}
  exit 1
done
rm -f /tmp/pr_create_resp_${cfg.taskId}
if [ "$PR_OK" = "0" ]; then
  echo "[pr] PR creation failed after 5 retries — orchestrator unreachable" >&2
  exit 1
fi
PR_URL=$(printf '%s' "$PR_RESP" | jq -r '.data.html_url // empty')
if [ -z "$PR_URL" ]; then
  echo "[pr] orchestrator response had no html_url: $PR_RESP" >&2
  exit 1
fi
`}

# 6. Persist PR URL for ./done + wrapper detection
echo "$PR_URL" > /tmp/pr_url_${cfg.taskId}
echo ""
echo "[pr] OK — $PR_URL"
echo "[pr] Next: ./done   (or call ./pr again if you push more commits)"
`;
  fs.writeFileSync(`${WORKSPACE}/pr`, script, { mode: 0o755 });
}

function writeDoneHelper(cfg: CodeChangeHelperConfig): void {
  // Posts COMPLETED to the orchestrator with the PR URL from /tmp/pr_url_<task_id>.
  // Also writes /tmp/done_<task_id> as a marker for the wrapper to detect on
  // exit and skip its fallback push.
  const script = `#!/bin/bash
# ./done — mark task COMPLETED. Requires ./pr open to have run first
# (writes /tmp/pr_url_${cfg.taskId} which this reads). Posts the COMPLETED
# status to the orchestrator with the PR URL, then writes a marker so the
# wrapper exits cleanly without re-doing commit/push.

set -uo pipefail
PR_FILE="/tmp/pr_url_${cfg.taskId}"
if [ ! -s "$PR_FILE" ]; then
  echo "Error: $PR_FILE missing or empty — run ./pr open first." >&2
  exit 2
fi
PR_URL=$(cat "$PR_FILE")

API="\${ORCHESTRATOR_URL:?ORCHESTRATOR_URL not set}"
TOKEN="\${AGENT_API_TOKEN:?AGENT_API_TOKEN not set}"
TASK="${cfg.taskId}"

# Idempotency: if marker already exists, this is a re-run — no-op cleanly.
if [ -f /tmp/done_${cfg.taskId} ]; then
  echo "[done] already marked — no-op (PR: $PR_URL)"
  exit 0
fi

# Optional enrichment: if Claude wrote /tmp/fix_responses_<task_id>.json
# (review-fix dialog-system response), include it in the payload so the
# orchestrator's verify-mode reviewer can chain-of-custody the fix
# rationale forward (review_pipeline.py:1664-1681).
FIX_RESPONSES_FILE="/tmp/fix_responses_${cfg.taskId}.json"
FIX_RESPONSES_JSON=""
if [ -s "$FIX_RESPONSES_FILE" ]; then
  if jq empty "$FIX_RESPONSES_FILE" >/dev/null 2>&1; then
    FIX_RESPONSES_JSON=$(cat "$FIX_RESPONSES_FILE")
  else
    echo "[done] /tmp/fix_responses_${cfg.taskId}.json exists but is not valid JSON — skipping enrichment" >&2
  fi
fi

# Soft-fallback self-review outcome. Claude should write a structured
# self-review JSON to /tmp/self_review_<task_id>.json before calling
# ./done; we forward it as self_review_outcome on the payload so
# downstream consumers (review_pipeline retry context, admin_digest
# catch-rate metrics, reaper retry pairing) get the signal. If missing
# or invalid, we emit a minimal fallback envelope so the gap is visible
# in metrics but the task doesn't block.
SELF_REVIEW_FILE="/tmp/self_review_${cfg.taskId}.json"
SELF_REVIEW_JSON=""
if [ -s "$SELF_REVIEW_FILE" ]; then
  if jq empty "$SELF_REVIEW_FILE" >/dev/null 2>&1; then
    SELF_REVIEW_JSON=$(cat "$SELF_REVIEW_FILE")
  else
    echo "[done] /tmp/self_review_${cfg.taskId}.json exists but is not valid JSON — using fallback envelope" >&2
    SELF_REVIEW_JSON='{"verdict":"unknown","check_results":{},"blind_spots":["self-review JSON parsing failed"]}'
  fi
else
  echo "[done] /tmp/self_review_${cfg.taskId}.json not found — using minimal fallback envelope" >&2
  SELF_REVIEW_JSON='{"verdict":"unknown","check_results":{},"blind_spots":["self-review skipped — investigate"]}'
fi

# Optional enrichment: changed_files from git diff — review-pipeline scope
# analysis, direction credit, rollup management all read this from
# task.context._pr_changed_files. Without it those signals degrade.
CHANGED_FILES_JSON=""
if BASE_COMMIT_FILE="/tmp/base_commit_${cfg.taskId}" && [ -s "$BASE_COMMIT_FILE" ]; then
  BASE_COMMIT=$(cat "$BASE_COMMIT_FILE")
  CHANGED_FILES=$(git -C "${WORKSPACE}" diff --name-only "$BASE_COMMIT..HEAD" 2>/dev/null | head -50 | jq -R . | jq -s .)
  if [ -n "$CHANGED_FILES" ] && [ "$CHANGED_FILES" != "[]" ]; then
    CHANGED_FILES_JSON="$CHANGED_FILES"
  fi
fi

# Retry on 429/5xx/network — orchestrator returns 429 on FOR-UPDATE
# contention with Retry-After: 5. Without retry, contended completions
# never reach the orchestrator and the task stays IN_PROGRESS until the
# reaper kicks in minutes later.
PAYLOAD=$(jq -n --arg tid "$TASK" --arg pr "$PR_URL" \\
  --argjson fix_responses "\${FIX_RESPONSES_JSON:-null}" \\
  --argjson self_review "$SELF_REVIEW_JSON" \\
  --argjson changed_files "\${CHANGED_FILES_JSON:-null}" \\
  '{task_id: $tid, status: "COMPLETED", artifact_type: "PULL_REQUEST", pr_url: $pr, logs: ("Task complete. PR: " + $pr)}
   + (if $fix_responses then {fix_agent_responses: $fix_responses} else {} end)
   + {self_review_outcome: $self_review}
   + (if $changed_files then {changed_files: $changed_files} else {} end)')

# Stale-result envelope: persist BEFORE attempting the POST so that if
# the VM is killed mid-retry, the next agent boot can scan for the
# envelope and replay the COMPLETED post. Mirrors the TS-side behaviour
# in persistAndPostStatus (index.ts:567). Without this, a kill during
# the bash 45s retry window leaves the task IN_PROGRESS until the reaper
# sweeps it as orphan, then re-runs from scratch — duplicate work + retry
# budget burn.
ENVELOPE="/tmp/task_${cfg.taskId}_result.json"
printf '%s' "$PAYLOAD" > "$ENVELOPE" 2>/dev/null || \
  echo "[done] WARNING: failed to persist stale-result envelope to $ENVELOPE" >&2

POSTED=0
for attempt in 1 2 3 4 5; do
  HTTP=$(printf '%s' "$PAYLOAD" | curl -sS -o /tmp/done_resp_${cfg.taskId} -w '%{http_code}' --max-time 30 \\
    -X POST "$API/api/agent/status" \\
    -H "Content-Type: application/json" -H "X-Agent-Token: $TOKEN" --data-binary @-) || HTTP="000"
  if [ "$HTTP" = "200" ] || [ "$HTTP" = "202" ]; then
    POSTED=1
    break
  fi
  if [ "$HTTP" = "429" ] || [ "\${HTTP:0:1}" = "5" ] || [ "$HTTP" = "000" ]; then
    BACKOFF=$((attempt * 3))
    echo "[done] POST attempt $attempt got HTTP=$HTTP — retrying in \${BACKOFF}s..." >&2
    sleep "$BACKOFF"
    continue
  fi
  # 4xx other than 429: don't retry — likely a permanent error.
  echo "[done] POST attempt $attempt got HTTP=$HTTP (permanent failure):" >&2
  cat /tmp/done_resp_${cfg.taskId} >&2
  rm -f /tmp/done_resp_${cfg.taskId}
  exit 1
done
rm -f /tmp/done_resp_${cfg.taskId}
if [ "$POSTED" = "0" ]; then
  echo "[done] POST failed after 5 retries — orchestrator unreachable. Marker NOT written; wrapper will fall back to its own status post." >&2
  exit 1
fi

# Marker is written ONLY after a successful POST. The wrapper inspects it
# to decide whether to skip its fallback completion path.
touch /tmp/done_${cfg.taskId}
# Envelope replayed successfully — remove it so the next agent boot doesn't
# resend a duplicate COMPLETED post for this task ID.
rm -f "$ENVELOPE"
echo ""
echo "[done] OK — wrapper will exit cleanly. PR: $PR_URL"
`;
  fs.writeFileSync(`${WORKSPACE}/done`, script, { mode: 0o755 });
}

function writeFailHelper(cfg: CodeChangeHelperConfig): void {
  // Explicit failure signal — Claude calls this when it determines the task
  // can't be completed (e.g. scope is wrong, blocker found, conflicting
  // requirements). Posts FAILED with the reason. Wrapper sees the marker
  // and exits cleanly without trying its own fallback push.
  const script = `#!/bin/bash
# ./fail "reason" — mark task FAILED with an operator-readable reason.
# Use when the task can't be completed cleanly: scope is wrong, blocker
# found, conflicting requirements, etc. Don't use for transient lint
# failures — keep iterating with ./check instead.

set -uo pipefail
REASON="\${1:?Usage: ./fail \\"<reason>\\"}"

API="\${ORCHESTRATOR_URL:?ORCHESTRATOR_URL not set}"
TOKEN="\${AGENT_API_TOKEN:?AGENT_API_TOKEN not set}"
TASK="${cfg.taskId}"

if [ -f /tmp/fail_${cfg.taskId} ]; then
  echo "[fail] already marked — no-op"
  exit 0
fi

PAYLOAD=$(jq -n --arg tid "$TASK" --arg r "$REASON" \\
  '{task_id: $tid, status: "FAILED", artifact_type: "ERROR_LOG", logs: $r, failure_category: "agent_self_failed"}')
POSTED=0
for attempt in 1 2 3 4 5; do
  HTTP=$(printf '%s' "$PAYLOAD" | curl -sS -o /tmp/fail_resp_${cfg.taskId} -w '%{http_code}' --max-time 30 \\
    -X POST "$API/api/agent/status" \\
    -H "Content-Type: application/json" -H "X-Agent-Token: $TOKEN" --data-binary @-) || HTTP="000"
  if [ "$HTTP" = "200" ] || [ "$HTTP" = "202" ]; then
    POSTED=1; break
  fi
  if [ "$HTTP" = "429" ] || [ "\${HTTP:0:1}" = "5" ] || [ "$HTTP" = "000" ]; then
    BACKOFF=$((attempt * 3))
    echo "[fail] POST attempt $attempt got HTTP=$HTTP — retrying in \${BACKOFF}s..." >&2
    sleep "$BACKOFF"; continue
  fi
  echo "[fail] POST attempt $attempt got HTTP=$HTTP (permanent failure):" >&2
  cat /tmp/fail_resp_${cfg.taskId} >&2
  rm -f /tmp/fail_resp_${cfg.taskId}
  exit 1
done
rm -f /tmp/fail_resp_${cfg.taskId}
if [ "$POSTED" = "0" ]; then
  echo "[fail] POST failed after 5 retries — orchestrator unreachable. Marker NOT written." >&2
  exit 1
fi

touch /tmp/fail_${cfg.taskId}
echo ""
echo "[fail] Posted FAILED status. Reason: $REASON"
`;
  fs.writeFileSync(`${WORKSPACE}/fail`, script, { mode: 0o755 });
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
        content = value.content;
        if (value.stale || value.effectiveness_score < 0.3) {
          caveat = '> ⚠ Possibly outdated or low confidence — verify before applying.\n\n';
        }
        caveat = `${relevanceLine}${caveat}`;
      }
      return `### ${key}\n${caveat}${content}`;
    })
    .join('\n\n');
  return `## Operational Playbook (active collective memory)\n\nThese notes are the project's living shared memory — every agent reads and writes them. Treat them as a useful starting point, not gospel: if an entry contradicts what you actually observe in the code, update or delete it (\`./playbook set\` to overwrite, \`./playbook delete <key>\` to remove). The playbook is only as accurate as the latest agent who looked.\n\n${lines}\n\n`;
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
export const SELF_REVIEW_PROMPT_VERSION = "1";

// Task-type-specific self-review focus blocks. Each block overlays the generic
// CODE_CHANGE_HELPER_GUIDANCE with concrete, type-specific scrutiny targeting
// the dominant miss patterns observed per task type in sr_catch_rate_trends.
// Handlers compose these into the prompt via getTaskTypeFocusBlock(taskType).

export const MIGRATE_TASK_FOCUS = `
## Self-review focus: migrate tasks

Migrate tasks carry the highest regression_risk because schema or data changes
ripple into existing tests. Apply these additional checks before \`./done\`:

1. **Down-revision chain** — verify the new migration's \`down_revision\` matches
   the current alembic head (\`alembic heads\`). A broken chain silently skips
   the migration in CI.
2. **Affected tests** — scan for tests that exercise the changed models or tables.
   Name them explicitly; record them in the \`regression_risk\` issue field even
   when they still pass (e.g. \`"issue": "affected_tests: [test_foo, test_bar] — all still pass"\`).
3. **EXPECTED_SCHEMA_REVISION** — if \`backend/app/main.py\` declares this constant,
   bump it to match the new alembic head or the invariant test will fail.
`;

export const REFACTOR_TASK_FOCUS = `
## Self-review focus: refactor tasks

Refactor tasks must guarantee NO new behavior is introduced. The dominant miss
is accidental scope-drift or subtle behavioral changes that bypass tests.

1. **Scope-drift (mandatory)** — run \`git diff --stat HEAD\` and confirm every
   changed file is in scope. If ANY externally-observable behavior changed (return
   values, side effects, error messages, API shape), record it under \`correctness\`
   with explicit before/after: \`"issue": "behavior changed: foo() returned X, now returns Y"\`.
2. **Caller inventory** — grep for every caller of renamed or restructured exports.
   Verify each call site is updated and behavior-preserving.
3. **Test delta** — a pure refactor should need no new tests; if you added tests,
   explain why in \`blind_spots\`.
`;

export const QA_TASK_FOCUS = `
## Self-review focus: qa tasks

QA tasks must produce meaningful coverage — the dominant miss is trivial tests
that don't exercise edge cases or new behaviors.

1. **Behavior enumeration** — list every new behavior or code path under test. In
   \`test-coverage\`, explicitly attest that each has a test: \`"issue": "behaviors
   covered: [X, Y, Z] — each has a dedicated test"\`.
2. **Edge cases** — for each behavior, name at least one non-happy-path scenario
   (empty input, error branch, boundary value) and confirm a test exercises it.
3. **Assertion strength** — tests that only check a function runs without error are
   insufficient. Verify assertions check actual return values or side effects.
`;

// Returns the task-type-specific self-review focus block to append after
// CODE_CHANGE_HELPER_GUIDANCE. Returns '' for task types without a custom block.
export function getTaskTypeFocusBlock(taskType: string): string {
  switch (taskType) {
    case 'migrate': return MIGRATE_TASK_FOCUS;
    case 'refactor': return REFACTOR_TASK_FOCUS;
    case 'qa': return QA_TASK_FOCUS;
    default: return '';
  }
}

export const CODE_CHANGE_HELPER_GUIDANCE = `
## Task lifecycle — you own it end-to-end

This task ends only when YOU explicitly mark it done or failed. The wrapper
does NOT auto-commit or auto-push your work. Use these helpers (already in
the workspace, executable):

- \`./check\` — runs lint/build for the stacks your changes touched
  (admin-ui eslint+build, agent tsc+eslint, backend ruff). Fast. Exits
  non-zero on failure with the actual error tail; fix and re-run until
  green. If your changes don't touch admin-ui/, agent/, or backend/,
  \`./check\` returns PASS without running anything — that's expected
  for pure-docs tasks.
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
- \`./pr open --title "<title>" --body "<body>"\` — gates on \`./check\`,
  commits any pending changes, pushes to the work branch (force-with-lease
  retry built in), creates a PR (or pushes updates to an existing one for
  fix tasks) via the orchestrator endpoint. On success writes
  /tmp/pr_url_<task_id> with the URL. Pass \`--with-tests\` to run
  \`./check test\` instead of just \`./check\` as the gate. Exits non-zero
  with a clear message on failure — fix and call again.
- \`./done\` — marks the task COMPLETED with the PR URL. Requires
  \`./pr open\` to have run first. Idempotent (safe to re-run).
- \`./fail "<reason>"\` — marks the task FAILED. Use ONLY when the task
  fundamentally can't be completed: scope is wrong, blocker found,
  conflicting requirements. Don't use for transient lint/test failures —
  keep iterating with \`./check\` instead.

Required exit pattern (every code-change task):
  edit -> ./check -> (if green) ./pr open --title ... --body ... -> ./done

ALWAYS call \`./done\` or \`./fail\` explicitly. The wrapper has only one
fallback: if you opened a PR via \`./pr open\` but didn't call \`./done\`,
it posts COMPLETED on your behalf using the PR URL marker. Otherwise it
posts FAILED with no useful reason — which is bad for the operator and
bad for downstream investigation. Don't rely on the fallback.

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
<!-- vocabulary mirrored in backend/testing/test_invariants.py (TestPromptVocabularyParity) — keep parity -->
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

**Scope-drift check (required for code-change tasks):**
Run \`git diff --stat HEAD\` (or \`git diff --stat <base>..HEAD\` if you know the base) and count
total changed lines (insertions + deletions) and the number of files touched.
Compare against the task's declared scope using these thresholds:

| Scope  | Max lines | Max files |
|--------|-----------|-----------|
| small  | 50        | 2         |
| medium | 200       | 5         |
| large  | 800       | 15        |

If the diff exceeds the threshold for the declared scope, record \`scope-drift\` in
\`check_results\` with \`passed: false\` and an \`issue\` field explaining the overage (e.g.
"small task shipped 312 changed lines across 6 files — threshold is 50 lines / 2 files").
If within bounds, record \`scope-drift\` with \`passed: true\`. The check does NOT block
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
  omit \`regression_risk\` from \`check_results\` entirely.

Like \`scope-drift\`, this check does NOT block \`./done\` — it is informational and feeds
the catch-rate dashboard.

Schema:

\`\`\`json
{
  "verdict": "pass" | "fail",
  "fixes_applied": true | false,
  "prompt_version": "<string from SELF_REVIEW_PROMPT_VERSION — include verbatim>",
  "check_results": {
    "<category>": { "passed": true | false, "issue": "..." }
  },
  "blind_spots": ["...", "..."],
  "truncated_files_read": ["path/a.ts", "path/b.ts"],
  "categories": ["incomplete_review"]
}
\`\`\`

\`fixes_applied: true\` means YOU caught something during self-review and
fixed it before pushing. A degraded \`{"verdict":"unknown","check_results":{},"blind_spots":["..."]}\`
envelope is acceptable when you have nothing useful to record, but the
file MUST exist for code-change tasks.

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
self-attestations in \`check_results\`. The final JSON shape is:
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

export const PLAYBOOK_GUIDANCE = `
## Operational Playbook — Active Collective Memory
The playbook is shared, mutable knowledge across all agents in this project — more like a forum than an append-only log. Use \`./playbook set <key> <value>\` to save reusable knowledge OR overwrite an existing entry, \`./playbook get\` to review, and \`./playbook delete <key>\` to remove an entry that turned out to be wrong, stale, or contradicted by the code you just read. If an existing entry led you astray, fixing or removing it helps the next agent more than adding a new one alongside it.

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

// ── Shared prompt assembly helpers ──────────────────────────────────────

// Detect test files across Python/JS/TS conventions used in this repo.
function isTestFilePath(f: string): boolean {
  return /(?:^|\/)(?:tests?|testing|__tests__)\//i.test(f)
    || /(?:^|\/)test_[^/]+$/i.test(f)
    || /(?:^|\/)[^/]+\.(?:test|spec)\.[a-z]+$/i.test(f)
    || /_test\.[a-z]+$/i.test(f);
}

// Synthesize concise tactical guidance from scope_success_rates and
// direction_{prior,successful}_analysis. Returns up to 2 sentences.
export function synthesizeSuccessPatterns(
  dirCtx: Record<string, unknown>,
  taskScope: string,
): string[] {
  const sentences: string[] = [];

  const scopeRates = dirCtx.scope_success_rates as Record<string, Record<string, unknown>> | undefined;
  if (scopeRates && typeof scopeRates === 'object' && taskScope) {
    const rateFor = (scope: string) => {
      const s = scopeRates[scope] as Record<string, unknown> | undefined;
      if (!s) return null;
      const created = Number(s.created ?? 0);
      const merged = Number(s.merged ?? 0);
      if (created < 1) return null;
      return { merged, created, pct: Math.round((merged / created) * 100) };
    };
    const current = rateFor(taskScope);
    if (current && current.pct < 50) {
      let best: { scope: string; pct: number; merged: number; created: number } | null = null;
      for (const [scope, counts] of Object.entries(scopeRates)) {
        if (scope === taskScope) continue;
        const created = Number(counts.created ?? 0);
        const merged = Number(counts.merged ?? 0);
        if (created < 2) continue;
        const pct = Math.round((merged / created) * 100);
        if (!best || pct > best.pct) best = { scope, pct, merged, created };
      }
      if (best && best.pct - current.pct >= 20) {
        sentences.push(
          `${taskScope}-scope tasks in this direction merge at ${current.pct}% (${current.merged}/${current.created}) while ${best.scope}-scope tasks merge at ${best.pct}% (${best.merged}/${best.created}) — consider constraining your changes to the smallest viable surface.`,
        );
      }
    }
  }

  const successful = Array.isArray(dirCtx.direction_successful_analysis)
    ? dirCtx.direction_successful_analysis as Array<Record<string, unknown>>
    : [];
  const failed = Array.isArray(dirCtx.direction_prior_analysis)
    ? dirCtx.direction_prior_analysis as Array<Record<string, unknown>>
    : [];
  const entryHasTest = (e: Record<string, unknown>): boolean => {
    const files = Array.isArray(e.pr_changed_files) ? e.pr_changed_files as unknown[] : [];
    return files.some(f => typeof f === 'string' && isTestFilePath(f));
  };
  if (successful.length >= 2 && failed.length >= 1) {
    const succWithTests = successful.filter(entryHasTest).length;
    const failWithTests = failed.filter(entryHasTest).length;
    const succRatio = succWithTests / successful.length;
    const failRatio = failWithTests / failed.length;
    if (succRatio >= 0.75 && failRatio <= 0.25) {
      sentences.push(
        'Successful tasks in this direction consistently touch test files while failed tasks typically did not — add or update tests for the behavior you change.',
      );
    }
  }

  return sentences;
}

// Aggregate pr_changed_files across failed prior-analysis entries to find
// files appearing in 2+ failures. Skips test files. Returns up to top 5.
export function synthesizeFailureHotspots(
  dirCtx: Record<string, unknown>,
): Array<{ file: string; count: number }> {
  const failed = Array.isArray(dirCtx.direction_prior_analysis)
    ? dirCtx.direction_prior_analysis as Array<Record<string, unknown>>
    : [];
  if (failed.length < 2) return [];
  const counts = new Map<string, number>();
  for (const entry of failed) {
    const files = Array.isArray(entry.pr_changed_files) ? entry.pr_changed_files as unknown[] : [];
    const seen = new Set<string>();
    for (const f of files) {
      const path = typeof f === 'string' ? f : '';
      if (!path || isTestFilePath(path) || seen.has(path)) continue;
      seen.add(path);
      counts.set(path, (counts.get(path) ?? 0) + 1);
    }
  }
  return Array.from(counts.entries())
    .filter(([, c]) => c >= 2)
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .slice(0, 5)
    .map(([file, count]) => ({ file, count }));
}

export function buildContextPrefix(task: Record<string, unknown>): string {
  const projectCtx = task.project_context as Record<string, unknown> | undefined;
  const payloads = Array.isArray(task.payloads) ? task.payloads as Record<string, unknown>[] : [];
  const payload = payloads[0] as Record<string, unknown> | undefined;
  const taskType = payload ? strVal(payload.type) : '';
  const taskContext = task.context as Record<string, unknown> | undefined;
  const taskScope = taskContext ? strVal(taskContext.scope) : '';

  let prefix = '';

  // Project Vision — strategic "why" set by the strategic research agent.
  // Surfaced first so tactical agents can align their work to it.
  const vision = task.vision as Record<string, unknown> | undefined;
  if (vision && typeof vision === 'object') {
    const vLines: string[] = ['## Project Vision'];
    const purpose = strVal(vision.purpose);
    if (purpose) vLines.push(`**Purpose:** ${purpose}`);
    const successCriteria = vision.success_criteria;
    if (typeof successCriteria === 'string' && successCriteria) {
      vLines.push(`**Success criteria:** ${successCriteria}`);
    } else if (Array.isArray(successCriteria) && successCriteria.length > 0) {
      vLines.push('**Success criteria:**');
      for (const sc of successCriteria) {
        const s = strVal(sc);
        if (s) vLines.push(`- ${s}`);
      }
    }
    const priorities = Array.isArray(vision.priorities) ? vision.priorities as unknown[] : [];
    if (priorities.length > 0) {
      vLines.push('**Priorities:**');
      for (const p of priorities) {
        const s = strVal(p);
        if (s) vLines.push(`- ${s}`);
      }
    }
    const systems = Array.isArray(vision.systems) ? vision.systems as Array<Record<string, unknown>> : [];
    if (systems.length > 0) {
      vLines.push('**Systems:**');
      for (const sys of systems) {
        const name = strVal(sys.name);
        if (!name) continue;
        const status = strVal(sys.status);
        const desc = strVal(sys.description);
        const statusSuffix = status ? ` (${status})` : '';
        const descSuffix = desc ? ` — ${desc}` : '';
        vLines.push(`- **${name}**${statusSuffix}${descSuffix}`);
      }
    }
    const context = strVal(vision.context);
    if (context) vLines.push(`**Context:** ${context}`);
    if (vLines.length > 1) {
      vLines.push('');
      prefix += vLines.join('\n') + '\n';
    }
  }

  if (projectCtx) {
    const hasData = Object.keys(projectCtx).some(k => {
      const v = projectCtx[k]; return Array.isArray(v) && v.length > 0;
    });
    if (hasData) {
      const currentDirectionId = taskContext ? strVal(taskContext.direction_id) : '';
      const relatedFiles =
        (payload?.related_files as string[] | undefined) ||
        (taskContext?.related_files as string[] | undefined) ||
        [];
      prefix = formatProjectContext(projectCtx, taskType, taskScope, currentDirectionId, relatedFiles.length) + '\n\n';
    }
  }

  // Append direction context section when present (title, priority, failed approaches)
  const dirCtx = task.direction_context as Record<string, unknown> | undefined;
  if (dirCtx) {
    const dcLines: string[] = ['## Direction Context'];
    const title = strVal(dirCtx.title);
    if (title) dcLines.push(`**Goal:** ${title}`);
    const priority = strVal(dirCtx.priority);
    if (priority) dcLines.push(`**Priority:** ${priority}`);
    const narrative = dirCtx.narrative as Record<string, unknown> | undefined;
    if (narrative && typeof narrative === 'object') {
      const established = Array.isArray(narrative.established_findings)
        ? (narrative.established_findings as unknown[]).map(s => strVal(s)).filter(Boolean)
        : [];
      if (established.length > 0) {
        dcLines.push('**Established findings (what we know):**');
        for (const f of established) dcLines.push(`- ${f}`);
      }
      const openQuestions = Array.isArray(narrative.open_questions)
        ? (narrative.open_questions as unknown[]).map(s => strVal(s)).filter(Boolean)
        : [];
      if (openQuestions.length > 0) {
        dcLines.push('**Open questions (still investigating):**');
        for (const q of openQuestions) dcLines.push(`- ${q}`);
      }
      const lastCycle = narrative.last_cycle as Record<string, unknown> | undefined;
      if (lastCycle && typeof lastCycle === 'object') {
        const tc = Number(lastCycle.tasks_created ?? 0);
        const pm = Number(lastCycle.prs_merged ?? 0);
        const pr = Number(lastCycle.prs_rejected ?? 0);
        if (tc > 0) {
          dcLines.push(`**Last productive cycle:** ${pm}/${tc} merged${pr > 0 ? `, ${pr} rejected` : ''}`);
        }
      }
    }
    const failedApproaches = Array.isArray(dirCtx.failed_approaches) ? dirCtx.failed_approaches as unknown[] : [];
    if (failedApproaches.length > 0) {
      dcLines.push('**Avoid (already tried and failed):**');
      for (const fa of failedApproaches) {
        if (typeof fa === 'string') {
          dcLines.push(`- ${fa}`);
        } else if (fa && typeof fa === 'object') {
          const obj = fa as Record<string, unknown>;
          const text = strVal(obj.text);
          const ftype = strVal(obj.failure_type);
          const failedAt = strVal(obj.failed_at);
          const stage = strVal(obj.failure_stage);
          if (text) {
            const headParts = [`- ${text}`];
            if (ftype) headParts.push(`[${ftype}]`);
            const qualifiers: string[] = [];
            const rel = relativeTimeFromIso(failedAt);
            if (rel) qualifiers.push(rel);
            if (stage) qualifiers.push(`failed at ${stage}`);
            if (qualifiers.length > 0) headParts.push(`(${qualifiers.join(', ')})`);
            dcLines.push(headParts.join(' '));
          }
        }
      }
    }
    const recentOutcomes = Array.isArray(dirCtx.recent_outcomes) ? dirCtx.recent_outcomes as Array<Record<string, unknown>> : [];
    if (recentOutcomes.length > 0) {
      const parts = recentOutcomes.map(o => `${Number(o.prs_merged ?? 0)}/${Number(o.tasks_created ?? 0)} merged`);
      dcLines.push(`Recent cycles: ${parts.join(', ')}`);
    }
    const failureStagePattern = dirCtx.failure_stage_pattern as Record<string, unknown> | undefined;
    if (failureStagePattern) {
      const msg = strVal(failureStagePattern.message);
      if (msg) {
        const rate = Number(failureStagePattern.rate);
        const header = Number.isFinite(rate) && rate > 0
          ? `**Failure pattern (${Math.round(rate * 100)}% of recent failures):**`
          : `**Failure pattern:**`;
        dcLines.push(`${header} ${msg}`);
      }
    }
    const scopeRates = dirCtx.scope_success_rates as Record<string, Record<string, unknown>> | undefined;
    if (scopeRates && typeof scopeRates === 'object') {
      const scopeOrder = ['small', 'medium', 'large', 'ci_failure'];
      const entries = Object.entries(scopeRates).sort(([a], [b]) => {
        const ai = scopeOrder.indexOf(a);
        const bi = scopeOrder.indexOf(b);
        return (ai === -1 ? 99 : ai) - (bi === -1 ? 99 : bi);
      });
      const parts: string[] = [];
      for (const [scope, counts] of entries) {
        const created = Number(counts.created ?? 0);
        const merged = Number(counts.merged ?? 0);
        if (created < 1) continue;
        const pct = Math.round((merged / created) * 100);
        const failed = Number(counts.failed ?? 0);
        const suffix = failed > 0 ? `, ${failed} failed` : '';
        parts.push(`${scope} ${merged}/${created} (${pct}%${suffix})`);
      }
      if (parts.length > 0) {
        dcLines.push(`Scope merge rates: ${parts.join(', ')}`);
      }
      const expansionParts: string[] = [];
      for (const [scope, counts] of entries) {
        const ratio = Number(counts.avg_expansion_ratio);
        if (!Number.isFinite(ratio) || ratio <= 0) continue;
        expansionParts.push(`${scope}-scope averages ${ratio.toFixed(1)}x file expansion`);
      }
      if (expansionParts.length > 0) {
        dcLines.push(
          `Direction expansion: ${expansionParts.join('; ')} — budget headroom for related files beyond what you declare upfront.`,
        );
      }
      const crossParts: string[] = [];
      for (const [scope, counts] of entries) {
        const merged = Number(counts.merged ?? 0);
        const crossMerged = Number(counts.merged_cross_module ?? 0);
        if (merged < 1 || crossMerged < 1) continue;
        crossParts.push(`${crossMerged} of ${merged} ${scope} merges touched 2+ directories`);
      }
      if (crossParts.length > 0) {
        dcLines.push(`Cross-directory: ${crossParts.join('; ')} — multi-directory changes are proven to merge in this direction.`);
      }
    }
    const rollingMedianIter = typeof dirCtx.review_iteration_rolling_median === 'number'
      ? dirCtx.review_iteration_rolling_median
      : null;
    if (rollingMedianIter !== null) {
      const lastCycleIter = typeof dirCtx.review_iteration_last_cycle === 'number'
        ? dirCtx.review_iteration_last_cycle
        : null;
      const lastCycleSuffix = lastCycleIter !== null && lastCycleIter !== rollingMedianIter
        ? ` (last cycle: ${lastCycleIter.toFixed(1)})`
        : '';
      dcLines.push(
        `Recent PRs on this direction averaged ${rollingMedianIter.toFixed(1)} review iterations${lastCycleSuffix}; tight scope merges faster.`,
      );
    }
    const priorAnalysis = Array.isArray(dirCtx.direction_prior_analysis)
      ? dirCtx.direction_prior_analysis as Array<Record<string, unknown>>
      : [];
    if (priorAnalysis.length > 0) {
      dcLines.push('**Prior analysis from failed attempts:**');
      for (const entry of priorAnalysis) {
        const t = strVal(entry.title);
        const cat = strVal(entry.failure_category);
        const scope = strVal(entry.scope);
        const tags = [cat, scope].filter(Boolean).join(', ');
        const header = tags ? `- ${t} [${tags}]` : `- ${t}`;
        dcLines.push(header);
        const plan = strVal(entry.analysis_plan);
        if (plan) dcLines.push(`  - Analysis plan: ${plan}`);
        const tfs = strVal(entry.test_failure_summary);
        if (tfs) dcLines.push(`  - Test failure: ${tfs}`);
        const statusMsg = strVal(entry.latest_status_message);
        if (statusMsg) dcLines.push(`  - Error: ${statusMsg}`);
        const sro = entry.self_review_outcome;
        if (sro && typeof sro === 'object') {
          const sroObj = sro as Record<string, unknown>;
          const sroParts: string[] = [];
          const verdict = strVal(sroObj.verdict);
          if (verdict) sroParts.push(`verdict=${verdict}`);
          const failedChecks = Array.isArray(sroObj.failed_checks)
            ? (sroObj.failed_checks as unknown[]).map(f => strVal(f)).filter(Boolean)
            : [];
          if (failedChecks.length > 0) sroParts.push(`failed=${failedChecks.join(',')}`);
          const notes = strVal(sroObj.notes);
          if (notes) sroParts.push(`notes=${notes}`);
          if (sroParts.length > 0) dcLines.push(`  - Self-review: ${sroParts.join('; ')}`);
        }
        const diffSummary = Array.isArray(entry.pr_diff_summary)
          ? entry.pr_diff_summary as Array<Record<string, unknown>>
          : [];
        if (diffSummary.length > 0) {
          const parts = diffSummary.map(d => {
            const file = strVal(d.file);
            const added = typeof d.added === 'number' ? d.added : null;
            const removed = typeof d.removed === 'number' ? d.removed : null;
            if (added !== null || removed !== null) {
              return `${file} (+${added ?? 0}/-${removed ?? 0})`;
            }
            return file;
          }).filter(Boolean);
          if (parts.length > 0) dcLines.push(`  - Files changed: ${parts.join(', ')}`);
        } else {
          const changedFiles = Array.isArray(entry.pr_changed_files)
            ? (entry.pr_changed_files as unknown[]).map(f => strVal(f)).filter(Boolean)
            : [];
          if (changedFiles.length > 0) {
            dcLines.push(`  - Files changed: ${changedFiles.join(', ')}`);
          }
        }
        const fp = strVal(entry.previous_error_fingerprint);
        if (fp) dcLines.push(`  - Previous error fingerprint: ${fp}`);
        if (typeof entry.review_iterations === 'number') {
          const iter = entry.review_iterations;
          const label = iter === 0
            ? 'Review iterations: 0 (failed before review)'
            : `Review iterations: ${iter} (${iter === 1 ? '1 cycle' : `${iter} cycles`} before failure)`;
          dcLines.push(`  - ${label}`);
        }
        const reviewFeedback = entry.review_feedback;
        if (Array.isArray(reviewFeedback) && reviewFeedback.length > 0) {
          const parts = reviewFeedback.map(item => {
            if (!item || typeof item !== 'object') return '';
            const obj = item as Record<string, unknown>;
            const sev = strVal(obj.severity);
            const desc = strVal(obj.description);
            if (!desc) return '';
            const file = strVal(obj.file);
            const line = typeof obj.line === 'number' ? obj.line : null;
            const loc = file ? ` (${file}${line !== null ? `:${line}` : ''})` : '';
            return sev ? `[${sev}] ${desc}${loc}` : `${desc}${loc}`;
          }).filter(Boolean);
          if (parts.length > 0) dcLines.push(`  - Review feedback: ${parts.join(', ')}`);
        } else if (typeof reviewFeedback === 'string' && reviewFeedback) {
          dcLines.push(`  - Review feedback: ${reviewFeedback}`);
        }
      }
    }
    const failureHotspots = synthesizeFailureHotspots(dirCtx);
    if (failureHotspots.length > 0) {
      const parts = failureHotspots.map(h => `${h.file} (${h.count}x)`).join(', ');
      dcLines.push(`Files appearing in multiple recent failures: ${parts} — changes to these files need extra care.`);
    }
    const successfulAnalysis = Array.isArray(dirCtx.direction_successful_analysis)
      ? dirCtx.direction_successful_analysis as Array<Record<string, unknown>>
      : [];
    if (successfulAnalysis.length > 0) {
      dcLines.push('**Approaches that worked:**');
      for (const entry of successfulAnalysis) {
        const t = strVal(entry.title);
        const scope = strVal(entry.scope);
        const header = scope ? `- ${t} [${scope}]` : `- ${t}`;
        dcLines.push(header);
        const plan = strVal(entry.analysis_plan);
        if (plan) dcLines.push(`  - Analysis plan: ${plan}`);
        const changedFiles = Array.isArray(entry.pr_changed_files)
          ? (entry.pr_changed_files as unknown[]).map(f => strVal(f)).filter(Boolean)
          : [];
        if (changedFiles.length > 0) {
          dcLines.push(`  - Files changed: ${changedFiles.join(', ')}`);
        }
        if (typeof entry.review_iterations === 'number') {
          const iter = entry.review_iterations;
          const label = iter <= 1
            ? '1st-pass success'
            : `${iter} review cycles`;
          dcLines.push(`  - ${label}`);
        }
      }
    }
    const patterns = synthesizeSuccessPatterns(dirCtx, taskScope);
    if (patterns.length > 0) {
      dcLines.push('**What tends to work:**');
      for (const s of patterns) dcLines.push(`- ${s}`);
    }
    type DirectionFirstReviewIssue = { description: string; count: number; category: string };
    const dirTopIssues = Array.isArray(dirCtx.top_first_review_issues)
      ? dirCtx.top_first_review_issues as DirectionFirstReviewIssue[]
      : [];
    if (dirTopIssues.length > 0) {
      dcLines.push('**Recurring review issues in this direction (address before submitting):**');
      for (const issue of dirTopIssues) {
        dcLines.push(`- "${issue.description}" (seen ${issue.count}x, category: ${issue.category})`);
      }
    }
    const promptQualityPatterns = Array.isArray(dirCtx.prompt_quality_patterns)
      ? (dirCtx.prompt_quality_patterns as unknown[]).filter((s): s is string => typeof s === 'string')
      : [];
    if (promptQualityPatterns.length > 0) {
      dcLines.push('**Prompt quality patterns (apply when writing finding prompts):**');
      for (const note of promptQualityPatterns) dcLines.push(`- ${note}`);
    }
    type ReviewEffectivenessCategory = { trend?: string };
    const reviewEff = dirCtx.review_effectiveness as
      | { categories?: Record<string, ReviewEffectivenessCategory> }
      | null
      | undefined;
    const reviewEffCats = reviewEff?.categories;
    if (reviewEffCats && Object.keys(reviewEffCats).length > 0) {
      dcLines.push('**Review effectiveness trends (per category):**');
      for (const [cat, info] of Object.entries(reviewEffCats)) {
        const trend = info.trend;
        const trendIndicator = trend === 'decreasing' ? 'trending down'
          : trend === 'increasing' ? 'trending up ⚠'
          : trend === 'stable' ? 'persistent'
          : trend ?? 'unknown';
        dcLines.push(`- **${cat}**: ${trendIndicator}`);
      }
    }
    type ReviewQualityFeedback = {
      reviews_dismissed?: number;
      reviews_overridden?: number;
      user_issues_caught?: number;
      note?: string;
    };
    const rqf = dirCtx.review_quality_feedback as ReviewQualityFeedback | null | undefined;
    if (rqf && typeof rqf === 'object') {
      const rqfLines: string[] = [];
      if (typeof rqf.note === 'string' && rqf.note) {
        rqfLines.push(`- ${rqf.note}`);
      }
      const counts: string[] = [];
      if (typeof rqf.reviews_dismissed === 'number') counts.push(`dismissed: ${rqf.reviews_dismissed}`);
      if (typeof rqf.reviews_overridden === 'number') counts.push(`overridden: ${rqf.reviews_overridden}`);
      if (typeof rqf.user_issues_caught === 'number') counts.push(`user-caught: ${rqf.user_issues_caught}`);
      if (counts.length > 0) rqfLines.push(`- self-review calibration — ${counts.join(', ')}`);
      if (rqfLines.length > 0) {
        dcLines.push('**Self-review calibration:**');
        dcLines.push(...rqfLines);
      }
    }
    dcLines.push('');
    prefix += dcLines.join('\n') + '\n';
  }

  // Append architecture context section when present
  const archCtx = projectCtx?.architecture_context as Array<Record<string, unknown>> | undefined;
  if (archCtx && archCtx.length > 0) {
    const archLines: string[] = ['## Architecture Context'];
    for (const sys of archCtx) {
      const name = strVal(sys.name);
      const status = strVal(sys.status);
      const ready = sys.ready_for_features === true ? 'ready' : 'not ready';
      const desc = strVal(sys.description);
      const line = desc
        ? `- **${name}**: ${status}, ${ready} for features — ${desc}`
        : `- **${name}**: ${status}, ${ready} for features`;
      archLines.push(line);
    }
    archLines.push('');
    prefix += archLines.join('\n') + '\n';
  }

  // Append task scope section for create/fix/refactor task types
  const scopeApplicable = ['create', 'fix', 'refactor'].includes(taskType);
  if (taskScope && scopeApplicable) {
    const scopeDescriptions: Record<string, string> = {
      small: 'expect <50 lines across 1-3 files. Keep changes minimal and focused.',
      medium: 'expect 50-200 lines across 2-5 files. Moderate cross-cutting changes. Plan your changes before executing — identify files, dependencies, and order.',
      large: 'cross-component, 200+ lines — plan before coding.',
    };
    const scopeDesc = scopeDescriptions[taskScope] ?? '';
    const effort = payload ? strVal(payload.effort) : '';
    const calibratedFrom = taskContext ? strVal(taskContext.scope_calibrated_from) : '';
    const scopeLines: string[] = ['## Task Scope'];
    const scopeLine = scopeDesc ? `Scope: ${taskScope} — ${scopeDesc}` : `Scope: ${taskScope}`;
    scopeLines.push(scopeLine);
    if (effort) scopeLines.push(`Effort: ${effort}`);
    if (calibratedFrom && calibratedFrom !== taskScope) {
      const order = ['small', 'medium', 'large'];
      const calibratedFromBase = calibratedFrom.split(':')[0];
      const fromIdx = order.indexOf(calibratedFromBase);
      const toIdx = order.indexOf(taskScope);
      const isUpgrade = fromIdx >= 0 && toIdx > fromIdx;
      const scopeRatesForCal = dirCtx?.scope_success_rates as
        | Record<string, Record<string, unknown>>
        | undefined;
      const formatRate = (scope: string): string | null => {
        const entry = scopeRatesForCal?.[scope];
        if (!entry) return null;
        const created = Number(entry.created) || 0;
        const merged = Number(entry.merged) || 0;
        if (created <= 0) return null;
        const pct = Math.round((merged / created) * 100);
        return `${scope}-scope tasks merge at ${pct}% (${merged}/${created})`;
      };
      const fromRate = formatRate(calibratedFromBase);
      const toRate = formatRate(taskScope);
      let ratesSuffix = '';
      if (fromRate && toRate) {
        ratesSuffix = ` Direction merge rates: ${toRate} vs ${fromRate}.`;
      } else if (fromRate) {
        ratesSuffix = ` Direction merge rate: ${fromRate}.`;
      } else if (toRate) {
        ratesSuffix = ` Direction merge rate: ${toRate}.`;
      }
      if (isUpgrade) {
        scopeLines.push(
          `Note: This task was originally scoped as ${calibratedFrom} but calibrated up to ${taskScope} because ${calibratedFrom}-scope tasks have struggled for this direction while smaller scopes have succeeded.${ratesSuffix} Decompose the work into independent smaller pieces — each piece should be small enough to succeed on its own.`,
        );
      } else {
        scopeLines.push(
          `Note: This task was originally scoped as ${calibratedFrom} but calibrated down to ${taskScope} because ${calibratedFrom}-scope tasks have struggled for this direction.${ratesSuffix} Stay tightly focused — the scope reduction is intentional to avoid the scope creep that caused prior ${calibratedFrom}-scope failures.`,
        );
      }
    }
    scopeLines.push('');
    prefix += scopeLines.join('\n') + '\n';
  }

  // Append hidden-signal section surfacing backend-computed quality signals
  // stored in task.context that would otherwise never reach the agent.
  if (taskContext) {
    const signalLines: string[] = [];
    const preventive = taskContext.preventive_context as Record<string, unknown> | undefined;
    if (preventive && typeof preventive === 'object') {
      const warning = strVal(preventive.warning);
      if (warning) {
        const overlapFiles = Array.isArray(preventive.overlap_files)
          ? (preventive.overlap_files as unknown[]).map(f => strVal(f)).filter(Boolean)
          : [];
        const fingerprint = strVal(preventive.fingerprint);
        const anomalyKey = strVal(preventive.anomaly_key);
        signalLines.push('**Preventive warning:** ' + warning);
        if (overlapFiles.length > 0) {
          signalLines.push(`- Overlap files: ${overlapFiles.join(', ')}`);
        }
        if (fingerprint) signalLines.push(`- Failure fingerprint: ${fingerprint}`);
        if (anomalyKey) signalLines.push(`- Anomaly key: ${anomalyKey}`);
      }
    }
    const budgetWarning = strVal(taskContext._file_budget_warning);
    if (budgetWarning) {
      signalLines.push(`**File budget pressure:** ${budgetWarning}`);
    }
    const lowConfRate = taskContext.low_confidence_spawn_rate;
    if (typeof lowConfRate === 'number' && Number.isFinite(lowConfRate)) {
      const pct = Math.round(lowConfRate * 100);
      signalLines.push(
        `**Low-confidence spawn:** predicted merge probability is ${pct}% — prefer the smallest viable change and avoid speculative scope expansion.`,
      );
    }
    const complexityScore = taskContext.complexity_score;
    if (typeof complexityScore === 'number' && Number.isFinite(complexityScore) && complexityScore < 0.5) {
      const pct = Math.round(complexityScore * 100);
      signalLines.push(`**Complexity score:** ${pct}/100 (low merge-probability estimate).`);
      const signals = taskContext.complexity_signals as Record<string, unknown> | undefined;
      if (signals && typeof signals === 'object') {
        const files = typeof signals.files === 'number' ? signals.files : null;
        const modules = typeof signals.modules === 'number' ? signals.modules : null;
        const expansionRatio = typeof signals.expansion_ratio === 'number' ? signals.expansion_ratio : null;
        const neighborRatio = typeof signals.neighbor_ratio === 'number' ? signals.neighbor_ratio : null;
        const factors = (signals.factors && typeof signals.factors === 'object')
          ? signals.factors as Record<string, unknown>
          : {};
        const f = (k: string): number | null =>
          typeof factors[k] === 'number' ? factors[k] : null;

        const sentences: string[] = [];
        if (files !== null && modules !== null) {
          sentences.push(`This task spans ${files} file${files === 1 ? '' : 's'} across ${modules} module${modules === 1 ? '' : 's'}.`);
        } else if (files !== null) {
          sentences.push(`This task spans ${files} file${files === 1 ? '' : 's'}.`);
        }
        const fRatio = f('ratio');
        if (fRatio !== null && fRatio < 0.8 && expansionRatio !== null) {
          sentences.push(`Historical expansion ratio of ${expansionRatio}x means agents working on similar tasks touched ${expansionRatio}x the listed files — constrain your initial changes aggressively.`);
        }
        const fNeighbors = f('neighbors');
        if (fNeighbors !== null && fNeighbors < 0.8 && neighborRatio !== null) {
          sentences.push(`High co-change neighbor ratio (${neighborRatio}) suggests unlisted files will likely need changes too — plan for scope pressure.`);
        }
        const fModules = f('modules');
        if (fModules !== null && fModules < 0.8 && modules !== null && modules > 1) {
          sentences.push(`Changes spanning ${modules} modules increase cross-system risk — verify each module's contract independently.`);
        }
        const fFiles = f('files');
        if (fFiles !== null && fFiles < 0.8 && files !== null) {
          sentences.push(`File count (${files}) is above the high-merge band — prefer the smallest viable subset.`);
        }
        if (sentences.length > 0) {
          signalLines.push(`- ${sentences.join(' ')}`);
        }
      }
    }
    if (signalLines.length > 0) {
      prefix += ['## Quality Signals', ...signalLines, ''].join('\n') + '\n';
    }
  }

  // Append remediation context when this task was spawned from an investigation.
  // The payload prompt already contains the root cause and recommended fix — this
  // section adds framing so the agent treats it as a diagnosed fix.
  if (taskContext && taskContext.diagnosis_task_id) {
    const remLines: string[] = ['## Remediation Context'];
    remLines.push(
      'This task is a diagnosed fix spawned from a prior investigation. The payload contains the root cause and recommended fix — validate that your changes resolve that specific root cause, not adjacent issues.',
    );
    const severity = strVal(taskContext.severity);
    if (severity) remLines.push(`- Severity: ${severity}`);
    const confidence = strVal(taskContext.diagnosis_confidence);
    if (confidence) remLines.push(`- Diagnosis confidence: ${confidence}`);
    const attemptsRaw = taskContext.remediation_attempts;
    let attempts: number | null = null;
    if (typeof attemptsRaw === 'number' && Number.isFinite(attemptsRaw)) {
      attempts = attemptsRaw;
    } else if (dirCtx && typeof dirCtx.remediation_attempts === 'number') {
      attempts = dirCtx.remediation_attempts;
    }
    if (attempts !== null && attempts > 0) {
      remLines.push(`- Prior remediation attempts on this direction: ${attempts} — if a previous remediation failed to hold, favor a different angle over repeating the prior fix shape.`);
    }
    if (confidence === 'low') {
      remLines.push('- Confidence is low: treat the recommended fix as a hypothesis, verify the root cause before applying, and prefer the smallest reversible change.');
    }
    remLines.push('');
    prefix += remLines.join('\n') + '\n';
  }

  return prefix;
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
    const selfReviewStatus = fixesApplied
      ? 'Self-review found issues and applied fixes (but the task still failed after review)'
      : 'Self-review ran and found no critical issues';
    lines.push(`Self-review outcome: ${selfReviewStatus}`);
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
