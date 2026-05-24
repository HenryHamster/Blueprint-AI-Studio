import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';
import { createHash } from 'crypto';
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
  'release_audit': 'You are running on the LTS branch (main) and reviewing the incoming diff against main\'s standards. Run `git diff <diff_range>` to obtain the diff. Check for test erosion (deleted test files, net assertion/test-count delta). Score findings into audit_score [0,1]. Do not modify any files — output is a JSON release audit report only.',
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
  'plan',             // Uses research-plan.ts bespoke prompt assembly
  'research',         // Uses research-plan.ts bespoke prompt assembly
  'strategic_review', // Reuses research handler with research_mode branch; uses bespoke assembly
  'smoketest',        // Uses specialized.ts bespoke prompt assembly
  'design_doc_update', // Uses design-doc.ts bespoke prompt assembly
  'memo',             // Uses memo.ts bespoke prompt assembly (no-clone reasoning artifact)
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
export function computePromptFingerprint(taskType: string, buildConfig?: BuildConfig): string {
  const hints = getTaskTypeHints(buildConfig);
  const taskHint = hints[taskType] ?? '';
  const content = [
    taskHint,
    DEFAULT_TOOL_GUIDANCE,
    assembleFailureGuidanceBlocks(),
  ].join('\n\n');
  return createHash('sha256').update(content).digest('hex').slice(0, 12);
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
export const AGENT_HELPER_NAMES = ['playbook', 'messages', 'note', 'strategic', 'design-doc', 'check', 'pr', 'done', 'fail', 'fetch', 'blocked'];

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
  /** SR categories whose miss-rate is regressing for this task type.
   *  When set, ./done validates that each has a non-trivial `reasoning` field
   *  in check_results before posting COMPLETED. */
  regressingCategories?: string[];
  /** Resolved project build configuration. When it carries per-directory
   *  commands, the generated ./check runs those instead of the hardcoded
   *  Blueprint admin-ui/agent/backend blocks (the self-repo fallback used
   *  only when no build_config is present). */
  buildConfig?: BuildConfig;
}

/** Write ./check, ./pr, ./done, ./fail, ./blocked to WORKSPACE. Idempotent. */
export function writeCodeChangeHelpers(cfg: CodeChangeHelperConfig): void {
  writeCheckHelper(cfg);
  writePrHelper(cfg);
  writeDoneHelper(cfg);
  writeFailHelper(cfg);
  writeBlockedHelper(cfg);
  excludeAgentHelpers();
}

function writeCheckHelper(cfg: CodeChangeHelperConfig): void {
  validateCfgShellSafety(cfg);
  // Auto-detects which stacks were touched (admin-ui / agent / backend) by
  // inspecting `git diff --name-only` against the base branch + uncommitted.
  // Runs the corresponding lint/build commands plus the admin-ui browser-mode
  // viewport suite. Optional `./check test` also runs the Docker backend test
  // suite when backend/ was touched and tests-on-main is enabled.
  const header = `#!/bin/bash
# ./check — run lint/build (and optionally tests) for changed paths.
# Usage:
#   ./check         # lint+build only — fast, run before each iteration
#   ./check test    # also run tests (slow; mainly before ./pr open)
#   ./check full    # run repo-wide lint (eslint .) + full viewport suite
#                   # WITHOUT scoping. Skips the baseline recheck on
#                   # failure (because the result IS the new baseline).
#                   # Use sparingly — when you're confident your edits
#                   # affect a shared rule / config / global selector
#                   # whose effect a scoped check can't surface, OR
#                   # when a baseline-warned WARN is hiding a real
#                   # regression you want to confirm. Combine with
#                   # 'test' (./check full test) to also run tests.
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
FULL_SCOPE=0
for arg in "$@"; do
  case "$arg" in
    test|--tests) WITH_TESTS=1 ;;
    full|--full)  FULL_SCOPE=1  ;;
  esac
done

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

admin_ui_runtime_touched() {
  printf '%s\\n' "$CHANGED" | grep -Eq '^(admin-ui/src/|admin-ui/(package\\.json|package-lock\\.json|vite\\.config\\.ts|vitest(\\.viewport)?\\.config\\.ts|eslint\\.config\\.js|eslint-rules/))'
}

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
`;
  const blueprintStackBlocks = `
if admin_ui_runtime_touched; then
  ( cd admin-ui && [ -d node_modules ] || npm ci ) >/tmp/check_npm_ci.log 2>&1 || {
    echo "[check] admin-ui npm ci FAILED — see /tmp/check_npm_ci.log"
    tail -30 /tmp/check_npm_ci.log
    FAILED+=("admin-ui npm ci")
  }
  # Scope eslint to changed files. PR gates shouldn't be blocked by
  # pre-existing errors in untouched files (same rationale as ruff). We
  # do NOT baseline-warn eslint: errors in files the agent edited are
  # the agent's responsibility to fix (or carve out scope for) per the
  # "don't leave preexisting issues" principle. --full forces repo-wide.
  if [ "$FULL_SCOPE" = "1" ]; then
    run "admin-ui eslint (full, --full)" bash -c 'cd admin-ui && npx eslint . 2>&1'
  else
    : > /tmp/check_admin_ui_lint.txt
    printf '%s\\n' "$CHANGED" | grep -E '^admin-ui/.*\\.(ts|tsx|js|jsx|cjs|mjs)$' | while IFS= read -r f; do
      [ -f "$f" ] && printf '%s\\0' "\${f#admin-ui/}" >> /tmp/check_admin_ui_lint.txt
    done
    if [ -s /tmp/check_admin_ui_lint.txt ]; then
      run "admin-ui eslint (changed)" bash -c 'cd admin-ui && xargs -0 -a /tmp/check_admin_ui_lint.txt npx eslint 2>&1'
    else
      echo "[check] no admin-ui JS/TS files changed, skipping eslint"
    fi
  fi

  # Viewport gate: scoped run + baseline recheck.
  #
  # 1) SCOPING: by default, run only the viewport tests whose component
  #    .tsx (or .viewport.test.tsx) was touched. Repo-wide viewport stays
  #    a CI/main concern; on PRs we don't want pre-existing overflow or
  #    import drift in untouched components to block unrelated fixes.
  #    If a non-component admin-ui file changed (shared types, App.tsx,
  #    package.json, src/test/*), fall back to the full suite — that
  #    edit could break any component.
  #
  # 2) BASELINE: if the (scoped or full) run fails, re-run the SAME
  #    subset against origin/$BASE in a detached worktree. Both fail →
  #    pre-existing → warn-pass (mirrors frontend CI's BROKEN/OK
  #    pattern at .github/workflows/ci.yml line 111-143). PR-only fail →
  #    real regression → block. Worktree path keeps the agent's working
  #    tree untouched.
  ADMIN_UI_CHANGED=$(printf '%s\\n' "$CHANGED" | grep -E '^admin-ui/' || true)
  NON_COMPONENT=$(printf '%s\\n' "$ADMIN_UI_CHANGED" | grep -vE '^admin-ui/src/components/[^/]+\\.(viewport\\.test\\.tsx|tsx)$' || true)

  # Check for new admin-ui components without viewport tests.
  # Uses process substitution (\`done < <(...)\`) so the loop runs in the
  # current shell — a piped \`while\` would run in a subshell and any
  # FAILED+= or flag set inside would be lost, leaving the gate
  # advisory-only.
  if [ "$FULL_SCOPE" = "0" ] && [ -n "$ADMIN_UI_CHANGED" ]; then
    NEW_COMPONENTS=$(printf '%s\\n' "$ADMIN_UI_CHANGED" | grep -E '^admin-ui/src/components/[^/]+\\.tsx$' | grep -vE '\\.test\\.tsx$|\\.viewport\\.test\\.tsx$' || true)
    if [ -n "$NEW_COMPONENTS" ]; then
      MISSING_VIEWPORT_TESTS=()
      while IFS= read -r comp; do
        [ -z "$comp" ] && continue
        [ -f "\${comp%.tsx}.viewport.test.tsx" ] && continue
        basename "$comp" .tsx | grep -qE '^[a-z]' && continue
        grep -q '@viewport-test-exempt' "$comp" 2>/dev/null && continue
        MISSING_VIEWPORT_TESTS+=("$comp")
      done < <(printf '%s\\n' "$NEW_COMPONENTS")
      if [ \${#MISSING_VIEWPORT_TESTS[@]} -gt 0 ]; then
        echo "[check] ERROR: New component(s) without viewport test:" >&2
        for comp in "\${MISSING_VIEWPORT_TESTS[@]}"; do
          echo "  $comp" >&2
          echo "  Expected test file: \${comp%.tsx}.viewport.test.tsx" >&2
        done
        echo "  See admin-ui/src/test/viewport.ts for examples" >&2
        FAILED+=("admin-ui new component viewport test")
      fi
    fi
  fi

  : > /tmp/check_viewport_tests.txt
  if [ "$FULL_SCOPE" = "0" ] && [ -z "$NON_COMPONENT" ]; then
    printf '%s\\n' "$ADMIN_UI_CHANGED" | grep -E '^admin-ui/src/components/[^/]+\\.viewport\\.test\\.tsx$' | while IFS= read -r f; do
      [ -f "$f" ] && printf '%s\\n' "\${f#admin-ui/}" >> /tmp/check_viewport_tests.txt
    done
    printf '%s\\n' "$ADMIN_UI_CHANGED" | grep -E '^admin-ui/src/components/[^/]+\\.tsx$' | grep -v '\\.viewport\\.test\\.tsx$' | while IFS= read -r f; do
      vt="\${f%.tsx}.viewport.test.tsx"
      [ -f "$vt" ] && printf '%s\\n' "\${vt#admin-ui/}" >> /tmp/check_viewport_tests.txt
    done
  fi
  VIEWPORT_FILTER=$(sort -u /tmp/check_viewport_tests.txt 2>/dev/null | tr '\\n' ' ' | sed 's/ *$//')
  VIEWPORT_RUN=1
  if [ "$FULL_SCOPE" = "0" ] && [ -z "$NON_COMPONENT" ] && [ -z "$VIEWPORT_FILTER" ]; then
    echo "[check] no viewport test matches changed components, skipping viewport suite"
    VIEWPORT_RUN=0
  fi

  if [ "$VIEWPORT_RUN" = "1" ]; then
    # Bootstrap Playwright Chromium binary before the viewport gate.
    # The baked agent VM already includes the system libs (libatk, libnss,
    # etc.) — and may already have the browser cached, so we cache-check
    # first and fall through to the bare install, with --with-deps as a
    # last-resort fallback for fresh local machines.
    # See admin-ui/CONTRIBUTING.md for developer-machine setup.
    run "admin-ui playwright chromium" bash -c '
      cd admin-ui || exit 1
      if [ -d "$HOME/.cache/ms-playwright" ] && ls "$HOME/.cache/ms-playwright"/chromium-* >/dev/null 2>&1; then
        exit 0
      fi
      if npx playwright install chromium 2>&1; then
        exit 0
      fi
      if npx playwright install --with-deps chromium 2>&1; then
        exit 0
      fi
      echo "[check] Retrying inside Docker container..." >&2
      docker run --rm -v "$PWD:/work" -w /work mcr.microsoft.com/playwright:v1.59.1-jammy npx playwright install chromium 2>&1 || exit 1
    ' 2>&1
  fi

  if [ "$FULL_SCOPE" = "1" ]; then
    VIEWPORT_FILTER=""
    VIEWPORT_LABEL="admin-ui viewport (full, --full)"
  elif [ -n "$NON_COMPONENT" ]; then
    VIEWPORT_LABEL="admin-ui viewport (full)"
  elif [ -n "$VIEWPORT_FILTER" ]; then
    VIEWPORT_LABEL="admin-ui viewport (changed: $(echo "$VIEWPORT_FILTER" | wc -w | tr -d ' ') file(s))"
  fi

  if [ "$VIEWPORT_RUN" = "1" ]; then
    echo "[check] $VIEWPORT_LABEL..."
    PR_VP_LOG=/tmp/check_viewport_pr.log
    export VIEWPORT_FILTER
    if ( cd admin-ui && VITE_API_URL=http://localhost:8080 VITE_FIREBASE_API_KEY=ci VITE_FIREBASE_AUTH_DOMAIN=ci.firebaseapp.com VITE_FIREBASE_PROJECT_ID=ci npm run test:viewport -- $VIEWPORT_FILTER 2>&1 ) > "$PR_VP_LOG" 2>&1; then
      tail -20 "$PR_VP_LOG"
      echo "[check] $VIEWPORT_LABEL OK"
    elif [ "$FULL_SCOPE" = "1" ]; then
      # --full mode: skip baseline recheck. The result IS the new baseline.
      echo "[check] $VIEWPORT_LABEL FAILED (--full, no baseline recheck)"
      tail -100 "$PR_VP_LOG" || true
      FAILED+=("$VIEWPORT_LABEL")
    else
      echo "[check] $VIEWPORT_LABEL FAILED on PR — checking baseline on origin/$BASE..."
      BASELINE_FAILED=0
      BASELINE_REASON=""
      BASE_REF="origin/$BASE"
      if ! git rev-parse --verify "$BASE_REF" >/dev/null 2>&1; then
        BASE_REF="$BASE"
        git rev-parse --verify "$BASE_REF" >/dev/null 2>&1 || BASELINE_REASON="base ref unavailable"
      fi
      if [ -z "$BASELINE_REASON" ]; then
        WT=/tmp/check_viewport_baseline_wt
        # Stale worktree from a prior run can wedge \`worktree add\`.
        # \`worktree remove --force\` is the safe cleanup (no rm -rf).
        git worktree remove --force "$WT" >/dev/null 2>&1 || true
        if ! git worktree add --detach "$WT" "$BASE_REF" >/tmp/check_viewport_wt.log 2>&1; then
          BASELINE_REASON="worktree add failed (see /tmp/check_viewport_wt.log)"
        else
          MAIN_VP_LOG=/tmp/check_viewport_main.log
          # Baseline run uses the SAME scoped subset — VIEWPORT_FILTER
          # is exported above. If the test files don't exist on main
          # (newly added in this PR), vitest exits 0 with "no tests"
          # and we treat baseline as PASSING (so PR-side failure is
          # real). Acceptable: new test files should pass when added.
          if ( cd "$WT/admin-ui" && npm ci >/tmp/check_viewport_main_npmci.log 2>&1 && { [ -d "$HOME/.cache/ms-playwright" ] && ls "$HOME/.cache/ms-playwright"/chromium-* >/dev/null 2>&1 || npx playwright install chromium || npx playwright install --with-deps chromium; } >/tmp/check_viewport_main_pw.log 2>&1 && VITE_API_URL=http://localhost:8080 VITE_FIREBASE_API_KEY=ci VITE_FIREBASE_AUTH_DOMAIN=ci.firebaseapp.com VITE_FIREBASE_PROJECT_ID=ci npm run test:viewport -- $VIEWPORT_FILTER 2>&1 ) > "$MAIN_VP_LOG" 2>&1; then
            BASELINE_FAILED=0
          else
            BASELINE_FAILED=1
          fi
          git worktree remove --force "$WT" >/dev/null 2>&1 || true
        fi
      fi
      if [ "$BASELINE_FAILED" = "1" ]; then
        echo "[check] $VIEWPORT_LABEL WARN — also broken on $BASE_REF; not blocking PR"
        echo "[check] PR-side tail:"
        tail -30 "$PR_VP_LOG" || true
        echo "[check] Baseline-side tail:"
        tail -30 /tmp/check_viewport_main.log || true
      else
        echo "[check] $VIEWPORT_LABEL FAILED — passes on $BASE_REF, fails on PR. Real regression."
        [ -n "$BASELINE_REASON" ] && echo "[check] (baseline check skipped: $BASELINE_REASON — treating PR failure as real)"
        tail -100 "$PR_VP_LOG" || true
        FAILED+=("$VIEWPORT_LABEL")
      fi
    fi
  fi

  run "admin-ui build"   bash -c 'cd admin-ui && VITE_API_URL=http://localhost:8080 VITE_FIREBASE_API_KEY=ci VITE_FIREBASE_AUTH_DOMAIN=ci.firebaseapp.com VITE_FIREBASE_PROJECT_ID=ci npm run build 2>&1'
fi

if touches agent/; then
  ( cd agent && [ -d node_modules ] || npm ci ) >/tmp/check_agent_npm_ci.log 2>&1 || {
    echo "[check] agent npm ci FAILED — see /tmp/check_agent_npm_ci.log"
    tail -30 /tmp/check_agent_npm_ci.log
    FAILED+=("agent npm ci")
  }
  run "agent tsc"        bash -c 'cd agent && npx tsc --noEmit 2>&1'

  # Scope agent eslint to changed files. Errors in files the agent
  # edited are the agent's responsibility ("don't leave preexisting
  # issues" principle). --full forces repo-wide.
  if [ "$FULL_SCOPE" = "1" ]; then
    run "agent eslint (full, --full)" bash -c 'cd agent && npx eslint . 2>&1'
  else
    : > /tmp/check_agent_lint.txt
    printf '%s\\n' "$CHANGED" | grep -E '^agent/.*\\.(ts|tsx|js|jsx|cjs|mjs)$' | while IFS= read -r f; do
      [ -f "$f" ] && printf '%s\\0' "\${f#agent/}" >> /tmp/check_agent_lint.txt
    done
    if [ -s /tmp/check_agent_lint.txt ]; then
      run "agent eslint (changed)" bash -c 'cd agent && xargs -0 -a /tmp/check_agent_lint.txt npx eslint 2>&1'
    else
      echo "[check] no agent JS/TS files changed, skipping eslint"
    fi
  fi
fi

if touches backend/; then
  if command -v ruff >/dev/null 2>&1 || python3 -m ruff --version >/dev/null 2>&1; then
    # Scope ruff to changed *.py files only. Repo-wide ruff is a CI/main
    # concern; on PRs we don't want pre-existing baseline errors to block
    # unrelated fixes. CHANGED already includes both committed-since-base
    # and uncommitted/untracked. Filter to existing files so deletions
    # don't blow up ruff with ENOENT. Write null-delimited entries so xargs
    # -0 doesn't split on whitespace (path-with-space safe).
    : > /tmp/check_backend_py.txt
    printf '%s\\n' "$CHANGED" | grep -E '^backend/.*\\.py$' | while IFS= read -r f; do
      [ -f "$f" ] && printf '%s\\0' "$f" >> /tmp/check_backend_py.txt
    done
    if [ -s /tmp/check_backend_py.txt ]; then
      run "backend ruff (changed)"     bash -c 'xargs -0 -a /tmp/check_backend_py.txt python3 -m ruff check --select E,F,W --ignore E501 2>&1'
      run "backend ruff fmt (changed)" bash -c 'xargs -0 -a /tmp/check_backend_py.txt python3 -m ruff format --check 2>&1'
    else
      echo "[check] no backend/*.py files changed, skipping ruff"
    fi
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
  # Fast migration-chain guard: no DB, no Docker, sub-second. Runs only
  # when at least one alembic/versions/ file was added or changed.
  if printf '%s\\n' "$CHANGED" | grep -q '^backend/alembic/versions/.*\\.py$'; then
    run "backend migration chain" bash -c 'python3 backend/app/migration_chain.py 2>&1'
  fi
fi
`;
  // When the project supplies a build_config with directories, generate
  // per-directory check blocks from it. Otherwise fall back to the hardcoded
  // Blueprint blocks above (self-repo behavior is byte-for-byte unchanged).
  const stackBlocks = cfg.buildConfig?.directories?.length
    ? buildConfigDrivenStackBlocks(cfg.buildConfig)
    : blueprintStackBlocks;
  const footer = `
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
  const script = header + stackBlocks + footer;
  fs.writeFileSync(`${WORKSPACE}/check`, script, { mode: 0o755 });
}

/** Generate config-driven per-directory ./check blocks from a project's
 *  build_config. Mirrors the per-directory loop in runProjectLintChecks
 *  (index.ts): for each configured directory whose files changed, run its
 *  install/lint/build commands; test_cmd runs only under `./check test`
 *  (WITH_TESTS). env_vars are exported so all commands inherit them.
 *
 *  All interpolated values (paths, commands, env names/values) are validated
 *  by validateCfgShellSafety against a conservative allowlist that rejects
 *  shell metacharacters (including single quotes), so emitting them as
 *  single-quoted bash literals here is injection-safe — see CLAUDE.md on
 *  generating one language from another. */
function buildConfigDrivenStackBlocks(buildConfig: BuildConfig): string {
  const dirs = buildConfig.directories ?? [];
  const envLines = Object.entries(buildConfig.env_vars ?? {}).map(
    ([k, v]) => `export ${k}='${v}'`,
  );
  const sections: string[] = [];
  for (const dir of dirs) {
    const dirPrefix = dir.path.replace(/\/$/, '');
    const inner: string[] = [];
    if (dir.install_cmd) inner.push(`  run "${dirPrefix} install" bash -c 'cd "${dirPrefix}" && ${dir.install_cmd}'`);
    if (dir.lint_cmd) inner.push(`  run "${dirPrefix} lint" bash -c 'cd "${dirPrefix}" && ${dir.lint_cmd}'`);
    if (dir.build_cmd) inner.push(`  run "${dirPrefix} build" bash -c 'cd "${dirPrefix}" && ${dir.build_cmd}'`);
    if (dir.test_cmd) {
      inner.push(`  if [ "$WITH_TESTS" = "1" ]; then`);
      inner.push(`    run "${dirPrefix} test" bash -c 'cd "${dirPrefix}" && ${dir.test_cmd}'`);
      inner.push(`  fi`);
    }
    if (inner.length === 0) continue;
    sections.push(`if touches "${dirPrefix}/"; then\n${inner.join('\n')}\nfi`);
  }
  const comment = `
# Config-driven checks: project supplied a build_config, so run its
# per-directory install/lint/build commands (tests under \`./check test\`)
# for changed directories. Mirrors runProjectLintChecks in index.ts.`;
  const envBlock = envLines.length ? `\n${envLines.join('\n')}` : '';
  const body = sections.length ? `\n${sections.join('\n\n')}` : '';
  return `${comment}${envBlock}${body}\n`;
}

// Load the canonical sr-pr-section.js renderer from disk and base64-encode
// it for safe bash-level injection into the generated ./pr script. Per
// CLAUDE.md "Generating Code for One Language From Another": the embedded
// script lives as a real file (lintable, testable) and the generator reads
// it rather than mirroring its body in a string constant. Lazy-loaded so
// that test suites which mock fs do not crash on module import.
const _SR_SECTION_SCRIPT_PATH = path.join(
  path.dirname(fileURLToPath(import.meta.url)),
  '..',
  'sr-pr-section.js',
);
function loadSrSectionScriptB64(): string {
  try {
    return Buffer.from(
      fs.readFileSync(_SR_SECTION_SCRIPT_PATH, 'utf8'),
    ).toString('base64');
  } catch {
    return '';
  }
}

const BRANCH_RE = /^[A-Za-z0-9._/-]+$/;
const OWNER_REPO_RE = /^[A-Za-z0-9._-]+$/;
const TASK_ID_RE = /^[A-Za-z0-9-]+$/;
// build_config paths/commands/env values are emitted as single-quoted bash
// literals; this allowlist rejects single quotes and every shell metacharacter
// ($ ` ; & | < > ( ) newline ...), so no value can break out of its quoting.
const SHELL_CMD_RE = /^[A-Za-z0-9 _./@:=,+-]+$/;
const ENV_NAME_RE = /^[A-Za-z_][A-Za-z0-9_]*$/;

function validateCfgShellSafety(cfg: CodeChangeHelperConfig): void {
  if (!TASK_ID_RE.test(cfg.taskId)) {
    throw new Error(`taskId contains unsafe characters for shell interpolation: ${cfg.taskId}`);
  }
  if (!BRANCH_RE.test(cfg.workBranch)) {
    throw new Error(`workBranch contains unsafe characters for shell interpolation: ${cfg.workBranch}`);
  }
  if (!BRANCH_RE.test(cfg.baseBranch)) {
    throw new Error(`baseBranch contains unsafe characters for shell interpolation: ${cfg.baseBranch}`);
  }
  if (!OWNER_REPO_RE.test(cfg.owner)) {
    throw new Error(`owner contains unsafe characters for shell interpolation: ${cfg.owner}`);
  }
  if (!OWNER_REPO_RE.test(cfg.repo)) {
    throw new Error(`repo contains unsafe characters for shell interpolation: ${cfg.repo}`);
  }
  validateBuildConfigShellSafety(cfg.buildConfig);
}

/** Validate every build_config value that gets interpolated into the generated
 *  ./check bash. Throws on any shell metacharacter so the config-driven blocks
 *  stay injection-safe (see CLAUDE.md on generating one language from another). */
function validateBuildConfigShellSafety(buildConfig: BuildConfig | undefined): void {
  if (!buildConfig) return;
  for (const dir of buildConfig.directories ?? []) {
    if (!SHELL_CMD_RE.test(dir.path)) {
      throw new Error(`build_config directory path contains unsafe characters for shell interpolation: ${dir.path}`);
    }
    const cmds: Array<[string, string | undefined]> = [
      ['install_cmd', dir.install_cmd],
      ['lint_cmd', dir.lint_cmd],
      ['build_cmd', dir.build_cmd],
      ['test_cmd', dir.test_cmd],
    ];
    for (const [field, val] of cmds) {
      if (val !== undefined && val !== '' && !SHELL_CMD_RE.test(val)) {
        throw new Error(`build_config ${field} for ${dir.path} contains unsafe characters for shell interpolation: ${val}`);
      }
    }
  }
  for (const [k, v] of Object.entries(buildConfig.env_vars ?? {})) {
    if (!ENV_NAME_RE.test(k)) {
      throw new Error(`build_config env_var name contains unsafe characters for shell interpolation: ${k}`);
    }
    if (!SHELL_CMD_RE.test(v)) {
      throw new Error(`build_config env_var value for ${k} contains unsafe characters for shell interpolation: ${v}`);
    }
  }
}

function writePrHelper(cfg: CodeChangeHelperConfig): void {
  validateCfgShellSafety(cfg);
  // Idempotent push + PR creation/update. Gates on ./check first.
  // Writes /tmp/pr_url_<task_id> on success so the wrapper + ./done can
  // pick up the URL without re-querying GitHub.
  const _SR_SECTION_SCRIPT_B64 = loadSrSectionScriptB64();
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

# ── Self-review section injection ─────────────────────────────────────────────
# Reads /tmp/self_review_<taskId>.json if present, renders the
# <!-- blueprint:self-review --> block, strips any prior block from BODY
# (idempotent), and appends the fresh one.  No-op when JSON is absent.
SR_FILE="/tmp/self_review_${cfg.taskId}.json"
SR_SKIP_FILE="/tmp/self_review_skip_reason_${cfg.taskId}.txt"
SR_SECTION=""
if [ -f "$SR_FILE" ]; then
  printf '%s' '${_SR_SECTION_SCRIPT_B64}' | base64 -d > /tmp/sr_section_${cfg.taskId}.js
  SR_SECTION=$(node /tmp/sr_section_${cfg.taskId}.js "$SR_FILE" "$SR_SKIP_FILE" 2>/dev/null || true)
  rm -f /tmp/sr_section_${cfg.taskId}.js
fi
if [ -n "$SR_SECTION" ]; then
  BODY=$(printf '%s' "$BODY" | sed '/<!-- blueprint:self-review -->/,/<!-- \\/blueprint:self-review -->/d')
  BODY=$(printf '%s\\n\\n%s' "$BODY" "$SR_SECTION")
fi

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
# Update existing PR body with fresh SR section (best-effort — log on failure)
if [ -n "$SR_SECTION" ]; then
  _PTRESP=$(curl -f -sS --max-time 30 \\
    -H "X-Agent-Token: $TOKEN" \\
    "$API/api/agent/github/token?task_id=$TASK" 2>/dev/null) || _PTRESP=""
  _PTOKEN=$(printf '%s' "$_PTRESP" | jq -r '.data.token // empty' 2>/dev/null || true)
  if [ -n "$_PTOKEN" ]; then
    # Fetch current PR body. Use -f so non-2xx GET responses (auth/rate-limit/
    # 5xx/404) fail fast rather than yielding an empty body that would let the
    # subsequent PATCH wipe the live PR description with just the SR section.
    _GETBODY_FILE=/tmp/pr_get_body_${cfg.taskId}.json
    if curl -f -sS --max-time 30 \\
        "https://api.github.com/repos/${cfg.owner}/${cfg.repo}/pulls/${cfg.existingPrNumber}" \\
        -H "Authorization: token $_PTOKEN" -H "Accept: application/vnd.github+json" \\
        -o "$_GETBODY_FILE" 2>/dev/null; then
      _CURBODY=$(jq -r '.body // ""' "$_GETBODY_FILE" 2>/dev/null || printf '')
      _UPDBODY=$(printf '%s' "$_CURBODY" | sed '/<!-- blueprint:self-review -->/,/<!-- \\/blueprint:self-review -->/d')
      _UPDBODY=$(printf '%s\\n\\n%s' "$_UPDBODY" "$SR_SECTION")
      _PHTTP=$(jq -n --arg b "$_UPDBODY" '{body: $b}' | curl -sS -o /dev/null -w '%{http_code}' \\
        --max-time 30 -X PATCH \\
        "https://api.github.com/repos/${cfg.owner}/${cfg.repo}/pulls/${cfg.existingPrNumber}" \\
        -H "Authorization: token $_PTOKEN" -H "Accept: application/vnd.github+json" \\
        -H "Content-Type: application/json" --data-binary @- 2>/dev/null) || _PHTTP="000"
      [ "$_PHTTP" = "200" ] || echo "[pr] Warning: SR body update returned HTTP $_PHTTP — continuing" >&2
    else
      echo "[pr] Warning: GET PR body failed — skipping SR section update to avoid wiping description" >&2
    fi
    rm -f "$_GETBODY_FILE"
    unset _PTOKEN
  fi
fi
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

# 7. Post-create mergeable check.
#
# GitHub computes mergeable status asynchronously (a few seconds after
# the head commit lands). Poll briefly, then if the PR is in a real
# conflict state (mergeable=false + state=dirty) tell the agent so
# they can rebase and resolve while the workspace is still warm. This
# is dramatically cheaper than letting the orchestrator spawn a fresh
# fix-task from a cold VM that has to re-derive the change intent.
#
# State semantics (from GitHub):
#   clean / has_hooks  → mergeable, will merge / fast-forward fine
#   blocked / unstable → CI gating; auto-merge handles it
#   behind             → out-of-date with base; GitHub or auto-rebase fixes
#   dirty              → real merge conflict; needs rebase + manual resolve
#   draft              → marked draft; no action
#
# Only \`dirty\` triggers the warning + exit 4. Everything else exits 0.
PR_NUMBER=$(printf '%s' "$PR_URL" | grep -oE '[0-9]+$' || true)
if [ -n "$PR_NUMBER" ]; then
  TOKEN_RESP=$(curl -f -sS --max-time 30 \\
    -H "X-Agent-Token: $TOKEN" \\
    "$API/api/agent/github/token?task_id=$TASK" 2>/dev/null) || TOKEN_RESP=""
  GH_TOKEN_PR=$(printf '%s' "$TOKEN_RESP" | jq -r '.data.token // empty' 2>/dev/null || true)
  if [ -n "$GH_TOKEN_PR" ]; then
    PR_MERGEABLE="null"
    PR_STATE="unknown"
    for pr_attempt in 1 2 3 4 5; do
      _PR_DETAIL=$(curl -sS --max-time 20 \\
        -H "Authorization: token $GH_TOKEN_PR" \\
        -H "Accept: application/vnd.github+json" \\
        "https://api.github.com/repos/${cfg.owner}/${cfg.repo}/pulls/$PR_NUMBER" 2>/dev/null || echo "")
      PR_MERGEABLE=$(printf '%s' "$_PR_DETAIL" | jq -r '.mergeable // "null"' 2>/dev/null || echo "null")
      PR_STATE=$(printf '%s' "$_PR_DETAIL" | jq -r '.mergeable_state // "unknown"' 2>/dev/null || echo "unknown")
      [ "$PR_MERGEABLE" != "null" ] && break
      sleep 3
    done
    unset GH_TOKEN_PR
    if [ "$PR_MERGEABLE" = "false" ] && [ "$PR_STATE" = "dirty" ]; then
      echo ""
      echo "[pr] WARNING: PR #$PR_NUMBER has merge conflicts with origin/${cfg.baseBranch}"
      echo "[pr]   mergeable=false, mergeable_state=dirty"
      echo "[pr]"
      echo "[pr] You're still in the workspace — resolve here, much cheaper than"
      echo "[pr] spawning a fix-task from a fresh VM that has to re-derive intent."
      echo "[pr]"
      echo "[pr] Resolution flow:"
      echo "[pr]   git fetch origin ${cfg.baseBranch}"
      echo "[pr]   git rebase origin/${cfg.baseBranch}"
      echo "[pr]   # Resolve conflicts in the listed files (use the Edit tool)."
      echo "[pr]   # If a conflict is in a file YOUR PR did not intend to touch,"
      echo "[pr]   # the right move is usually \\\`git checkout --theirs <file>\\\` to"
      echo "[pr]   # take the base branch's version, then re-apply your edits to"
      echo "[pr]   # the files you OWN. Don't accidentally revert sibling work."
      echo "[pr]   git rebase --continue"
      echo "[pr]   ./check       # re-verify before pushing"
      echo "[pr]   ./pr open --title \\"...\\" --body \\"...\\"   # re-push (force-with-lease)"
      echo "[pr]"
      echo "[pr] Limit: ~2 attempts. If you can't resolve cleanly (the conflict is"
      echo "[pr] structural, not just a textual overlap, or it's in code your task"
      echo "[pr] should not be modifying), call \\\`./blocked \\"<reason>\\"\\\` instead."
      echo "[pr] The orchestrator will spawn a fix-task as backstop."
      exit 4
    fi
    if [ "$PR_STATE" = "behind" ]; then
      echo "[pr] note: PR #$PR_NUMBER is behind origin/${cfg.baseBranch}; auto-merge will rebase when CI clears"
    fi
  fi
fi

echo ""
echo "[pr] OK — $PR_URL"
echo "[pr] Next: ./done   (or call ./pr again if you push more commits)"
`;
  fs.writeFileSync(`${WORKSPACE}/pr`, script, { mode: 0o755 });
}

function writeDoneHelper(cfg: CodeChangeHelperConfig): void {
  validateCfgShellSafety(cfg);
  // Posts COMPLETED to the orchestrator with the PR URL from /tmp/pr_url_<task_id>.
  // Also writes /tmp/done_<task_id> as a marker for the wrapper to detect on
  // exit and skip its fallback push.
  const script = `#!/bin/bash
# ./done — mark task COMPLETED. Requires ./pr open to have run first
# (writes /tmp/pr_url_${cfg.taskId} which this reads). Posts the COMPLETED
# status to the orchestrator with the PR URL, then writes a marker so the
# wrapper exits cleanly without re-doing commit/push.
#
# Usage:
#   ./done                                     # standard: ./check must pass
#   ./done --warn-out-of-scope "<reason>"      # judgment-call escape hatch:
#       ship despite ./check failures when those failures are PRE-EXISTING
#       on origin/${cfg.baseBranch} and out of scope for this PR. Posts a FEEDBACK
#       project message describing the pre-existing issues + your reason
#       so strategic/operators can spawn cleanup work later. The reason
#       text MUST include the specific gates/files that failed and why
#       fixing them is out of scope (e.g., "admin-ui eslint reports 4
#       errors in DashboardHome.tsx that pre-date this PR; fixing them
#       requires refactoring the digest-card layout which is not in
#       scope for this auth-flow change").
#
#       USE ./blocked INSTEAD when the failure requires introducing a
#       new bug or precondition (missing package, infra gap, agent
#       can't even start the work). --warn-out-of-scope is for issues
#       that already existed on main BEFORE this PR.

set -uo pipefail

WARN_OUT_OF_SCOPE_REASON=""
SKIP_NEXT=0
for i in "$@"; do
  if [ "$SKIP_NEXT" = "1" ]; then
    WARN_OUT_OF_SCOPE_REASON="$i"
    SKIP_NEXT=0
    continue
  fi
  case "$i" in
    --warn-out-of-scope)
      SKIP_NEXT=1
      ;;
    --warn-out-of-scope=*)
      WARN_OUT_OF_SCOPE_REASON="\${i#--warn-out-of-scope=}"
      ;;
  esac
done
if [ "$SKIP_NEXT" = "1" ] && [ -z "$WARN_OUT_OF_SCOPE_REASON" ]; then
  echo "Error: --warn-out-of-scope requires a reason argument" >&2
  echo "Usage: ./done --warn-out-of-scope \\"<reason>\\"" >&2
  exit 2
fi
if [ -n "$WARN_OUT_OF_SCOPE_REASON" ] && [ \${#WARN_OUT_OF_SCOPE_REASON} -lt 30 ]; then
  echo "Error: --warn-out-of-scope reason must be at least 30 chars (you gave \${#WARN_OUT_OF_SCOPE_REASON})." >&2
  echo "       Be specific: which gates failed, which files, why fixing is out of scope." >&2
  exit 2
fi

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
    SELF_REVIEW_JSON='{"verdict":"unknown","check_results":{},"blind_spots":["self-review JSON parsing failed"],"skip_reason":"json_parse_failed"}'
  fi
else
  echo "[done] /tmp/self_review_${cfg.taskId}.json not found — using minimal fallback envelope" >&2
  SKIP_REASON="json_missing"; MARKER="/tmp/self_review_skip_reason_${cfg.taskId}.txt"; [ -s "$MARKER" ] && SKIP_REASON=$(head -c 64 "$MARKER" | tr -dc 'a-z_')
  SELF_REVIEW_JSON='{"verdict":"unknown","check_results":{},"blind_spots":["self-review skipped — investigate"],"skip_reason":"'"$SKIP_REASON"'"}'
fi

# Regressing-category reasoning gate: require non-trivial reasoning for categories
# that have above-floor miss rates for this task type (embedded at write time).
# If any are missing, print the gap and exit so Claude can fix the SR JSON first.
${cfg.regressingCategories && cfg.regressingCategories.length > 0
  ? `REGRESSING_CATS=(${cfg.regressingCategories.map((c) => `"${c}"`).join(' ')})
MISSING_REASONING=()
for _CAT in "\${REGRESSING_CATS[@]}"; do
  _R=$(printf '%s' "$SELF_REVIEW_JSON" | jq -r --arg c "$_CAT" '.check_results[$c].reasoning // ""' 2>/dev/null || echo "")
  if [ "\${#_R}" -lt 10 ]; then
    # Short reasoning passes if it contains a file:line evidence anchor
    _HAS_ANCHOR=0
    if printf '%s' "$_R" | grep -qE '[a-zA-Z0-9._/-]+[.][a-z]+:[0-9]+' 2>/dev/null; then
      _HAS_ANCHOR=1
    fi
    if [ "\${_HAS_ANCHOR}" = "0" ]; then
      MISSING_REASONING+=("$_CAT")
    fi
  fi
done
if [ "\${#MISSING_REASONING[@]}" -gt 0 ]; then
  echo "[done] Regressing categories need a reasoning field (>=10 chars, or a file:line anchor) in check_results:" >&2
  for _CAT in "\${MISSING_REASONING[@]}"; do echo "  - $_CAT" >&2; done
  echo "Update /tmp/self_review_${cfg.taskId}.json and re-run ./done." >&2
  exit 1
fi`
  : '# No regressing categories for this task type — reasoning check skipped.'}

# High-stakes-category evidence-anchor gate: require concrete evidence anchors for PASS verdicts
# on correctness, error-handling, security, regression_risk, and scope-drift. Anchors must be
# file:line, diff stats reference, or file-list reference. If any are missing, print the gap and
# exit so Claude can fix the SR JSON.
HIGH_STAKES_CATS=("correctness" "error-handling" "security" "regression_risk" "scope-drift")
MISSING_EVIDENCE=()
for _CAT in "\${HIGH_STAKES_CATS[@]}"; do
  _PASSED=$(printf '%s' "$SELF_REVIEW_JSON" | jq -r --arg c "$_CAT" '.check_results[$c].passed // false' 2>/dev/null || echo "false")
  _REASONING=$(printf '%s' "$SELF_REVIEW_JSON" | jq -r --arg c "$_CAT" '.check_results[$c].reasoning // ""' 2>/dev/null || echo "")

  if [ "$_PASSED" = "true" ]; then
    # Check if reasoning has evidence anchor: file:line format, diff stats, or file list reference
    _HAS_ANCHOR=0
    # File:line reference (e.g., path/to/file.ts:42)
    if printf '%s' "$_REASONING" | grep -E '[a-zA-Z0-9._/-]+[.][a-z]+:[0-9]+' > /dev/null 2>&1; then
      _HAS_ANCHOR=1
    # Diff-stat reference (e.g., "lines / files" or "diff:")
    elif printf '%s' "$_REASONING" | grep -iE 'diff.*lines|files|threshold' > /dev/null 2>&1; then
      _HAS_ANCHOR=1
    # File-list reference (e.g., "related_files" or list of files)
    elif printf '%s' "$_REASONING" | grep -iE 'related_files|modified files|checked.*files?' > /dev/null 2>&1; then
      _HAS_ANCHOR=1
    # Substantial reasoning (20+ chars)
    elif printf '%s' "$_REASONING" | grep '.' > /dev/null 2>&1 && [ "\${#_REASONING}" -ge 20 ]; then
      _HAS_ANCHOR=1
    fi
    if [ "\${_HAS_ANCHOR}" = "0" ]; then
      MISSING_EVIDENCE+=("$_CAT")
    fi
  fi
done
if [ "\${#MISSING_EVIDENCE[@]}" -gt 0 ]; then
  echo "[done] PASS verdicts on high-stakes categories must include evidence anchors (file:line, diff stats, or file list):" >&2
  for _CAT in "\${MISSING_EVIDENCE[@]}"; do echo "  - $_CAT" >&2; done
  echo "Update /tmp/self_review_${cfg.taskId}.json with evidence anchors in reasoning and re-run ./done." >&2
  exit 1
fi

# scope-drift overage gate: if the diff materially exceeds the "large" scope threshold
# (800 lines / 15 files), do not allow scope-drift PASS — the agent must mark it FAIL.
# This catches cases where the diff is unexpectedly huge despite a smaller scope declaration.
_SCOPE_DRIFT_PASSED=$(printf '%s' "$SELF_REVIEW_JSON" | jq -r '.check_results["scope-drift"].passed // false' 2>/dev/null || echo "false")
if [ "$_SCOPE_DRIFT_PASSED" = "true" ]; then
  _DIFF_STAT=$(git -C "${WORKSPACE}" diff --stat HEAD 2>/dev/null | tail -1 || echo "")
  if [ -n "$_DIFF_STAT" ]; then
    # Parse "X files changed, Y insertions(+), Z deletions(-)" — extract files and total lines
    _FILES=$(printf '%s' "$_DIFF_STAT" | grep -oE '^[[:space:]]*[0-9]+' | xargs)
    _LINES=$(printf '%s' "$_DIFF_STAT" | grep -oE '[0-9]+[[:space:]]*(insertion|deletion)' | grep -oE '[0-9]+' | paste -sd+ | bc 2>/dev/null || echo "0")
    # If diff exceeds large threshold (800 lines, 15 files), block PASS
    if [ "$_FILES" -gt 15 ] || [ "$_LINES" -gt 800 ]; then
      echo "[done] scope-drift cannot be PASS when diff exceeds large threshold (800 lines / 15 files):" >&2
      echo "       Detected: $_LINES lines / $_FILES files" >&2
      echo "       Mark scope-drift as FAIL with the overage details, or revise your scope claim." >&2
      echo "       Update /tmp/self_review_${cfg.taskId}.json and re-run ./done." >&2
      exit 1
    fi
  fi
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
# --warn-out-of-scope: post a FEEDBACK project message BEFORE the
# COMPLETED post so the warning is visible to strategic even if the
# COMPLETED post stalls or fails. Best-effort: a message-post failure
# does not block ./done — the warn_out_of_scope_reason field on the
# COMPLETED payload is the durable record.
if [ -n "$WARN_OUT_OF_SCOPE_REASON" ]; then
  WARN_MSG="[shipped via ./done --warn-out-of-scope by task ${cfg.taskId}]
PR: $PR_URL

The agent shipped this diff despite ./check failures, asserting that
the failures are pre-existing on origin/${cfg.baseBranch} and out of
scope for this PR. Reason given:

$WARN_OUT_OF_SCOPE_REASON

Strategic / operators: this is a signal to spawn cleanup work for the
referenced files/gates. The PR itself is intended to merge as-is."
  jq -n --arg tid "$TASK" --arg c "$WARN_MSG" \\
    '{task_id: $tid, op: "create_message", data: {type: "FEEDBACK", content: $c}}' | \\
    curl -sS --max-time 30 --connect-timeout 10 -X POST "$API/api/agent/strategic" \\
      -H "Content-Type: application/json" -H "X-Agent-Token: $TOKEN" --data-binary @- \\
    > /tmp/done_warn_msg_${cfg.taskId}.log 2>&1 || \\
    echo "[done] warn-out-of-scope FEEDBACK message post failed (best-effort) — see /tmp/done_warn_msg_${cfg.taskId}.log" >&2
fi

PAYLOAD=$(jq -n --arg tid "$TASK" --arg pr "$PR_URL" \\
  --argjson fix_responses "\${FIX_RESPONSES_JSON:-null}" \\
  --argjson self_review "$SELF_REVIEW_JSON" \\
  --argjson changed_files "\${CHANGED_FILES_JSON:-null}" \\
  --arg warn_reason "\${WARN_OUT_OF_SCOPE_REASON:-}" \\
  '{task_id: $tid, status: "COMPLETED", artifact_type: "PULL_REQUEST", pr_url: $pr, logs: ("Task complete. PR: " + $pr)}
   + (if $fix_responses then {fix_agent_responses: $fix_responses} else {} end)
   + {self_review_outcome: $self_review}
   + (if $changed_files then {changed_files: $changed_files} else {} end)
   + (if ($warn_reason | length) > 0 then {warn_out_of_scope_reason: $warn_reason} else {} end)')

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
  validateCfgShellSafety(cfg);
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

function writeBlockedHelper(cfg: CodeChangeHelperConfig): void {
  validateCfgShellSafety(cfg);
  // Explicit precondition-blocked signal — Claude calls this when a real
  // code change has been made and ./check still fails identically (or the
  // failure is at the toolchain/system-library layer, where no code edit
  // can move the needle). Routes to BLOCKED_PRECONDITION (an INFRA class)
  // rather than CODE_ERROR — review-fix iterations don't burn against the
  // chain, and the strategic agent gets a ProjectMessage so it can decide
  // whether to spawn a precondition-fix direction.
  const script = `#!/bin/bash
# ./blocked "<reason>" — mark task BLOCKED_PRECONDITION (no retry burn).
#
# Use when:
#   - You've made a real code edit and ./check fails identically to the
#     previous attempt — the failure isn't moving and another iteration
#     won't help.
#   - The failure is at the toolchain / system-library layer (e.g.
#     "error while loading shared libraries: libatk-1.0.so.0",
#     "No module named 'ruff'", "playwright is not installed",
#     "command not found: <tool>"). No code edit can fix this.
#   - A repo-wide gate fails on files your PR did not touch (baseline
#     debt — pre-existing lint/test failures unrelated to your change).
#
# Don't use for ordinary lint/test failures Claude can fix — keep
# iterating with ./check instead. Don't use for "I don't know how to fix
# this" — that's ./fail. ./blocked is specifically for "no code edit
# CAN fix this on my own."
#
# Effect: marks the task FAILED with failure_category=BLOCKED_PRECONDITION
# (INFRA class — doesn't count toward the review-fix iteration cap), and
# the orchestrator notifies the strategic agent so it can decide whether
# to spawn a precondition-fix direction.

set -uo pipefail
REASON="\${1:?Usage: ./blocked \\"<reason>\\"}"

API="\${ORCHESTRATOR_URL:?ORCHESTRATOR_URL not set}"
TOKEN="\${AGENT_API_TOKEN:?AGENT_API_TOKEN not set}"
TASK="${cfg.taskId}"

if [ -f /tmp/blocked_${cfg.taskId} ]; then
  echo "[blocked] already marked — no-op"
  exit 0
fi

# "BLOCKER:" prefix is the canonical orchestrator-side signal for the
# BLOCKED_PRECONDITION class (see backend/app/failure_classification.py).
# We send both the explicit failure_category AND the prefixed reason so
# the routing is unambiguous regardless of which path the orchestrator
# takes (normalize_agent_failure_category vs the BLOCKER:-prefix
# fast-path in _classify_failure_class).
PAYLOAD=$(jq -n --arg tid "$TASK" --arg r "BLOCKER: $REASON" \\
  '{task_id: $tid, status: "FAILED", artifact_type: "ERROR_LOG", logs: $r, failure_category: "BLOCKED_PRECONDITION"}')
POSTED=0
for attempt in 1 2 3 4 5; do
  HTTP=$(printf '%s' "$PAYLOAD" | curl -sS -o /tmp/blocked_resp_${cfg.taskId} -w '%{http_code}' --max-time 30 \\
    -X POST "$API/api/agent/status" \\
    -H "Content-Type: application/json" -H "X-Agent-Token: $TOKEN" --data-binary @-) || HTTP="000"
  if [ "$HTTP" = "200" ] || [ "$HTTP" = "202" ]; then
    POSTED=1; break
  fi
  if [ "$HTTP" = "429" ] || [ "\${HTTP:0:1}" = "5" ] || [ "$HTTP" = "000" ]; then
    BACKOFF=$((attempt * 3))
    echo "[blocked] POST attempt $attempt got HTTP=$HTTP — retrying in \${BACKOFF}s..." >&2
    sleep "$BACKOFF"; continue
  fi
  echo "[blocked] POST attempt $attempt got HTTP=$HTTP (permanent failure):" >&2
  cat /tmp/blocked_resp_${cfg.taskId} >&2
  rm -f /tmp/blocked_resp_${cfg.taskId}
  exit 1
done
rm -f /tmp/blocked_resp_${cfg.taskId}
if [ "$POSTED" = "0" ]; then
  echo "[blocked] POST failed after 5 retries — orchestrator unreachable. Marker NOT written." >&2
  exit 1
fi

touch /tmp/blocked_${cfg.taskId}
echo ""
echo "[blocked] Posted BLOCKED_PRECONDITION status. Reason: $REASON"
echo "[blocked] Strategic agent will be notified — it may spawn a precondition-fix direction."
`;
  fs.writeFileSync(`${WORKSPACE}/blocked`, script, { mode: 0o755 });
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

type SRCatchRateSource = {
  categories?: Record<string, { miss_rate?: number; weighted_miss_rate?: number; current_hit_rate?: number; hit_rate?: number; below_floor?: boolean }>;
  by_task_type?: Record<string, Record<string, { miss_rate?: number; weighted_miss_rate?: number; current_hit_rate?: number; hit_rate?: number; below_floor?: boolean }>>;
  for_task_type?: Record<string, { miss_rate?: number; weighted_miss_rate?: number; current_hit_rate?: number; hit_rate?: number; below_floor?: boolean }>;
  prompt_version_changed?: boolean;
  // True when the measurement window spans multiple SR prompt versions. The
  // calibration surface is still surfaced (unlike prompt_version_changed which
  // fully suppresses it) but a caveat is prepended so the agent knows the
  // percentages may conflate regimes and should not be treated as a clean signal.
  mixed_prompt_versions?: boolean;
};

export function formatSelfReviewCalibrationSection(source: SRCatchRateSource | undefined, taskType = ''): string {
  if (!source || source.prompt_version_changed) return '';
  const categories = taskType ? (source.by_task_type?.[taskType] ?? source.for_task_type) : source.categories;
  if (!categories) return '';
  const rows = Object.entries(categories)
    .map(([category, data]) => ({ category, data, miss: typeof data.miss_rate === 'number' ? data.miss_rate : typeof data.hit_rate === 'number' ? 1 - data.hit_rate : typeof data.current_hit_rate === 'number' ? 1 - data.current_hit_rate : -1 }))
    .filter(({ data, miss }) => miss >= 0.5 && (taskType ? data.below_floor !== false : true))
    .sort((a, b) => (b.data.weighted_miss_rate ?? b.miss) - (a.data.weighted_miss_rate ?? a.miss))
    .slice(0, 2);
  if (rows.length === 0) return '';
  const caveat = source.mixed_prompt_versions
    ? '> Note: this calibration window spans multiple SR prompt versions — percentages may conflate regimes and are directional only.\n'
    : '';
  return `## Self-review calibration\n${caveat}Historical pattern for this task type:\n${rows.map(({ category, miss }) => `- Historical pattern: ${category} is missing ${Math.round(miss * 100)}% of issues a reviewer catches on this project. Scrutinize this category extra carefully.`).join('\n')}\n\n`;
}

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

1. **Scope-drift (mandatory)** — run BOTH \`git diff --stat HEAD\` AND
   \`git diff --name-only HEAD\`. Both triggers apply: the diff-size threshold
   (Trigger A) and the file-set check against the declared \`related_files\`
   (Trigger B, modulo the incidental allowlist — lockfiles, generated outputs,
   sibling test files). If ANY externally-observable behavior changed (return
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

export const CREATE_TASK_FOCUS = `
## Self-review focus: create tasks

Create tasks most commonly miss test coverage for new behavior and drift outside
the declared scope. Apply these additional checks before \`./done\`:

1. **Test coverage** — for every new function, class, or behavior you shipped,
   confirm there is at least one test. In \`test-coverage\`, attest explicitly:
   \`"issue": "new behavior X has a test in test_foo.py"\` (or note the gap).
2. **Scope-drift** — run BOTH \`git diff --stat HEAD\` AND \`git diff --name-only HEAD\`.
   Trigger A: diff size against the declared scope threshold. Trigger B: every
   modified file must be in the declared \`related_files\` set OR in the incidental
   allowlist (lockfiles like \`package-lock.json\`/\`poetry.lock\`/\`Cargo.lock\`/\`go.sum\`,
   generated outputs, \`.gitignore\`, sibling \`*.test.*\`/\`*.spec.*\` files). Any
   modified file outside \`related_files\` and outside the allowlist is off-scope —
   record under \`scope-drift\` with \`passed: false\` and list the off-scope files
   (e.g. "modified files outside declared scope: backend/app/foo.py — declared related_files: [agent/src/baz.ts]").
3. **Cross-file consistency** — when the diff touches multiple files, identify the dominant
   pattern introduced or modified (e.g., 'added null guard before .map calls', 'switched
   timestamps to UTC-aware', 'added try/except around external HTTP call'). Enumerate every
   touched file and attest that the pattern is applied consistently. If the pattern is
   inconsistently applied across files, record under \`correctness\` with \`passed: false\`
   and the issue: "pattern X inconsistently applied: present in [files], missing/divergent in [files]".
4. **Caller compatibility** — grep for callers of any function or export you
   added or modified; confirm their call sites still compile and behave correctly.
`;

export const FIX_TASK_FOCUS = `
## Self-review focus: fix tasks

Fix tasks most commonly introduce regression risk on the surface that was already
failing, and scope creep into unrelated files. Apply these additional checks:

1. **Regression surface** — identify the specific code path that was failing before
   your fix. Confirm the fix targets ONLY that path and does not silently change
   adjacent behavior. Record in \`regression_risk\` even when passing:
   \`"issue": "fixed path: foo() — adjacent callers bar(), baz() verified unchanged"\`.
2. **Scope-drift** — run BOTH \`git diff --stat HEAD\` AND \`git diff --name-only HEAD\`.
   Fixes should be surgical, so both triggers apply: diff-size threshold (Trigger A)
   AND the file-set check against \`related_files\` (Trigger B). Any modified file
   outside \`related_files\` (after the incidental allowlist — lockfiles, generated
   outputs, sibling test files like \`foo.test.ts\` next to \`foo.ts\`) is off-scope.
   Record unjustified additions under \`scope-drift\` with \`passed: false\` and list
   the off-scope file paths.
3. **Root-cause vs. symptom** — confirm the fix addresses the root cause, not just
   the observable symptom. If you only masked the symptom, record it under
   \`correctness\` with \`passed: false\`.
4. **Fresh verdict — do not reuse the original create task's SR** — your self-review
   must reflect THIS iteration's diff only. The original task's self-review (propagated
   as \`_self_review_outcome\` in context) is reference material, not a template to copy.
   Write to \`/tmp/self_review_<YOUR_task_id>.json\` using the CURRENT task ID from the
   ORCHESTRATOR env — never a prior task's ID.
5. **Cross-file consistency** — when the fix touches multiple files, confirm the fix pattern
   is applied consistently across all affected files. Identify the primary fix pattern and
   verify it is applied in all relevant locations. If the fix is incomplete across files,
   record under \`correctness\` with \`passed: false\` and the issue: "fix pattern inconsistency:
   [description of where pattern is/isn't applied]".
`;

export const DOCS_TASK_FOCUS = `
## Self-review focus: docs tasks

Docs tasks most commonly ship stale links or descriptions that diverge from
current code behavior. Apply these additional checks before \`./done\`:

1. **Link rot** — for every URL or cross-reference you added or kept, verify it
   resolves. Dead links in published docs are high-visibility failures.
2. **Code-comment vs. behavior** — for every code snippet or function description
   you wrote, re-read the actual implementation and confirm it matches. Divergence
   between docs and code is worse than no docs.
3. **Production-code untouched** — confirm \`git diff --name-only HEAD\` shows only
   documentation files (README, docs/, docstrings). Any production-code change
   that is not in \`related_files\` (and not an incidental allowlisted file like a
   lockfile) belongs in a separate task; record under \`scope-drift\` with
   \`passed: false\` and list the off-scope files.
`;

export const DEPS_TASK_FOCUS = `
## Self-review focus: deps tasks

Deps tasks most commonly leave lockfile/manifest drift or miss transitive breakage.
Apply these additional checks before \`./done\`:

1. **Lockfile/manifest parity** — confirm every file that declares a dependency
   (package.json, requirements.txt, pyproject.toml, Cargo.toml, go.mod) has a
   corresponding updated lockfile (package-lock.json, poetry.lock, Cargo.lock,
   go.sum). A manifest update without a lockfile update is a broken state.
2. **Transitive impact** — check whether bumped packages pulled in new transitive
   deps or dropped old ones. Run the build to confirm no implicit peer-dep errors.
3. **Major-version guard** — confirm no package crossed a major version boundary.
   Record any that did under \`correctness\` with \`passed: false\`.
`;

// Returns the task-type-specific self-review focus block to append after
// CODE_CHANGE_HELPER_GUIDANCE. Returns '' for task types without a custom block.
export function getTaskTypeFocusBlock(taskType: string): string {
  switch (taskType) {
    case 'migrate': return MIGRATE_TASK_FOCUS;
    case 'refactor': return REFACTOR_TASK_FOCUS;
    case 'qa': return QA_TASK_FOCUS;
    case 'create': return CREATE_TASK_FOCUS;
    case 'fix': return FIX_TASK_FOCUS;
    case 'docs': return DOCS_TASK_FOCUS;
    case 'deps': return DEPS_TASK_FOCUS;
    default: return '';
  }
}

export const CODE_CHANGE_HELPER_GUIDANCE = `
## Task lifecycle — you own it end-to-end

This task ends only when YOU explicitly mark it done or failed. The wrapper
does NOT auto-commit or auto-push your work. Use these helpers (already in
the workspace, executable):

- \`./check\` — runs lint/build for the stacks your changes touched
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
- \`./pr open --title "<title>" --body "<body>"\` — gates on \`./check\`,
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
      if (['create', 'fix', 'refactor', 'migrate'].includes(taskType)) {
        prefix += formatSelfReviewCalibrationSection(
          (projectCtx.self_review_catch_rate as SRCatchRateSource | undefined) ||
          (projectCtx.sr_catch_rate_trends as SRCatchRateSource | undefined),
          taskType,
        );
      }
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
    // recent_findings_evidence: structured claims+sources for the most recent findings.
    // Canonical evidence view — use this for cross-referencing claims and sources.
    const recentFindingsEvidence = Array.isArray(dirCtx.recent_findings_evidence)
      ? dirCtx.recent_findings_evidence as Array<Record<string, unknown>>
      : [];
    if (recentFindingsEvidence.length > 0) {
      dcLines.push('**Recent findings evidence (canonical — full claims and sources):**');
      for (const entry of recentFindingsEvidence) {
        const ftitle = strVal(entry.title);
        if (!ftitle) continue;
        dcLines.push(`- "${ftitle}"`);
        const claims = Array.isArray(entry.claims)
          ? (entry.claims as unknown[]).map(c => strVal(c)).filter(Boolean)
          : [];
        if (claims.length > 0) {
          dcLines.push(`  Claims: ${claims.join('; ')}`);
        }
        const sourceRefs = Array.isArray(entry.source_refs)
          ? entry.source_refs as Array<Record<string, unknown>>
          : [];
        if (sourceRefs.length > 0) {
          const parts = sourceRefs.map(s => {
            const ref = strVal(s.ref);
            const excerpt = strVal(s.excerpt);
            return excerpt ? `${ref} ("${excerpt}")` : ref;
          }).filter(Boolean);
          if (parts.length > 0) dcLines.push(`  Sources: ${parts.join('; ')}`);
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
