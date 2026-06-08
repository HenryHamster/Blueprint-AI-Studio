import fs from 'fs';
import { WORKSPACE } from '../index.js';
import type { BuildConfig } from '../index.js';
import { SHELL_CMD_RE } from '../shell-safety.js';
import { writeCheckHelper } from './check-helper.js';
import { writeDoneHelper } from './done-helper.js';
import { writePrHelper } from './pr-helper.js';

// Re-exported so callers importing writeCheckHelper from this module keep working.
export { writeCheckHelper } from './check-helper.js';

// ── Blueprint self-repo identity ────────────────────────────────────────────
// Canonical coordinates of the Blueprint repo the pipeline self-improves on.
// Used to gate Blueprint-stack-specific behavior (e.g. the hardcoded
// admin-ui/agent/backend ./check blocks) so it applies ONLY to the self-repo,
// not to an arbitrary onboarded target repo. Env-overridable for forks/tests.
// (We intentionally do NOT key off project_metadata.is_self_repo — that flag is
// being phased out; the repo coordinates are the durable signal.)
export const BLUEPRINT_SELF_REPO = {
  owner: process.env.BLUEPRINT_SELF_REPO_OWNER || 'HenryHamster',
  repo: process.env.BLUEPRINT_SELF_REPO_NAME || 'BlueprintAIStudio',
} as const;

/** True when owner/repo identify the Blueprint self-repo (case-insensitive —
 *  GitHub owner/repo names are case-insensitive). */
export function isBlueprintSelfRepo(owner: string, repo: string): boolean {
  return (
    owner.toLowerCase() === BLUEPRINT_SELF_REPO.owner.toLowerCase() &&
    repo.toLowerCase() === BLUEPRINT_SELF_REPO.repo.toLowerCase()
  );
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

export interface CodeChangeHelperConfig {
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
   *  task whose changes touch ``backend/``. Set when baseBranch === promotionTarget
   *  (or === 'main' when no promotionTarget is known). */
  testsOnMainBranch: boolean;
  /** Per-project promotion target branch (e.g. 'automain', 'production', 'release/v2').
   *  When set, the test gate fires when baseBranch === promotionTarget, overriding
   *  the testsOnMainBranch boolean. Handlers that read promotion_target from the
   *  task payload should pass it here. */
  promotionTarget?: string;
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

// Detects the canonical linter kind from a lint_cmd string.
// Returns null for unrecognized commands — those run verbatim (never silently
// rewrite something we don't understand).
export function detectLinterKind(lintCmd: string): 'ruff' | 'eslint' | 'mypy' | 'flake8' | 'pyright' | null {
  const base = lintCmd.replace(/^(python3?\s+-m\s+|npx\s+|pnpm\s+exec\s+)/, '');
  if (/^ruff\b/.test(base)) return 'ruff';
  if (/^eslint\b/.test(base)) return 'eslint';
  if (/^mypy\b/.test(base)) return 'mypy';
  if (/^flake8\b/.test(base)) return 'flake8';
  if (/^pyright\b/.test(base)) return 'pyright';
  return null;
}

/**
 * Builds the scoped lint bash one-liner for a recognized linter.
 *
 * source='check-tmpfile': returns the xargs invocation that reads null-delimited
 *   paths from /tmp/check_<slug>_lint.txt (populated upstream by grep over
 *   $CHANGED). Used inside the ./check generator (buildLintInner).
 *
 * source='git-diff': returns a git diff pipeline that filters changed files in
 *   dirPath and strips the directory prefix before passing to the linter.
 *   Used inside review prompt instruction strings (buildTypeCheckInstructions).
 *
 * Canonical behaviors shared across both call sites:
 * - Strips trailing '.' / './' token (replaced by the scoped file list)
 * - Recognizes python3 -m / npx / pnpm exec prefixes
 * - Hardcodes ruff to --select E,F,W --ignore E501 with python3 -m
 * - Returns null for unrecognized linters — callers must fall back to verbatim
 */
export function buildScopedLintOneliner(
  lintCmd: string,
  dirPath: string,
  baseBranch: string,
  source: 'check-tmpfile' | 'git-diff',
): string | null {
  const kind = detectLinterKind(lintCmd);
  if (!kind) return null;

  const toks = lintCmd.trim().split(/\s+/);
  while (toks.length > 1 && (toks[toks.length - 1] === '.' || toks[toks.length - 1] === './')) toks.pop();
  const execCmd = kind === 'ruff'
    ? 'python3 -m ruff check --select E,F,W --ignore E501'
    : toks.join(' ');

  const dir = dirPath.replace(/\/$/, '');
  const isPy = kind !== 'eslint';
  const extPat = isPy ? '\\.py' : '\\.(ts|tsx|js|jsx|cjs|mjs)';

  if (source === 'check-tmpfile') {
    const slug = dir.replace(/\//g, '_');
    return `xargs -0 -a /tmp/check_${slug}_lint.txt ${execCmd} 2>&1`;
  }
  return `git diff origin/${baseBranch}...HEAD --name-only | grep -E '${extPat}$' | sed 's|${dir}/||' | xargs -r ${execCmd}`;
}

const BRANCH_RE = /^[A-Za-z0-9._/-]+$/;
const OWNER_REPO_RE = /^[A-Za-z0-9._-]+$/;
const TASK_ID_RE = /^[A-Za-z0-9-]+$/;
const ENV_NAME_RE = /^[A-Za-z_][A-Za-z0-9_]*$/;

export function validateCfgShellSafety(cfg: CodeChangeHelperConfig): void {
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
    // NOTE: per-directory commands (install/lint/build/test) are intentionally
    // NOT validated here. resolveBuildConfig retains operator-configured commands
    // with quotes/shell operators verbatim so the backend's tests_gated decision
    // and the agent's no-CI gate agree on whether a project has runnable tests.
    // Those commands run safely via a `bash -c` argv in the no-CI gate (execFile,
    // no interpolation), and buildConfigDrivenStackBlocks SKIPS any command that
    // fails SHELL_CMD_RE rather than interpolating it — so throwing here would
    // needlessly abort ./check generation for an otherwise-runnable config.
    // path and env_var values below ARE still validated: they are interpolated
    // with no skip fallback.
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

# Optional structured prerequisite flags. When given, the blocker carries a
# normalized machine-resolvable need ({kind, ref}) alongside the free-text
# reason. Without any flag the payload is byte-for-byte the legacy shape.
REASON=""
NEED_KIND=""
NEED_REF=""
while [ $# -gt 0 ]; do
  case "$1" in
    --needs-task) NEED_KIND="task"; NEED_REF="\${2:-}"; shift 2;;
    --needs-pr) NEED_KIND="pr"; NEED_REF="\${2:-}"; shift 2;;
    --needs-capability) NEED_KIND="capability"; NEED_REF="\${2:-}"; shift 2;;
    *) if [ -z "$REASON" ]; then REASON="$1"; fi; shift;;
  esac
done
if [ -z "$REASON" ]; then
  echo "Usage: ./blocked \\"<reason>\\" [--needs-task <id>|--needs-pr <n>|--needs-capability \\"<desc>\\"]" >&2
  exit 1
fi

# Shared exact-task-ref policy (mirrors backend app.task_blocked.is_exact_task_ref):
# --needs-task is for a REAL task identifier only — a task UUID or a 16-char
# finding hash. A title-like ref (free prose naming a desired foundation) names
# no resolvable task, so it would only become stranded "missing task" demand.
# Record it as a capability need instead; named foundations belong to
# --needs-capability, not --needs-task.
if [ "$NEED_KIND" = "task" ] && [ -n "$NEED_REF" ]; then
  if ! printf '%s' "$NEED_REF" | grep -qiE '^([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{16})$'; then
    echo "[blocked] --needs-task ref '$NEED_REF' is not a task id or finding hash; recording it as a capability need (use --needs-capability for named foundations)." >&2
    NEED_KIND="capability"
  fi
fi

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
if [ -n "$NEED_KIND" ] && [ -n "$NEED_REF" ]; then
  PAYLOAD=$(jq -n --arg tid "$TASK" --arg r "BLOCKER: $REASON" \\
    --arg nk "$NEED_KIND" --arg nr "$NEED_REF" \\
    '{task_id: $tid, status: "FAILED", artifact_type: "ERROR_LOG", logs: $r, failure_category: "BLOCKED_PRECONDITION", blocked_need: {kind: $nk, ref: $nr}}')
else
  PAYLOAD=$(jq -n --arg tid "$TASK" --arg r "BLOCKER: $REASON" \\
    '{task_id: $tid, status: "FAILED", artifact_type: "ERROR_LOG", logs: $r, failure_category: "BLOCKED_PRECONDITION"}')
fi
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
