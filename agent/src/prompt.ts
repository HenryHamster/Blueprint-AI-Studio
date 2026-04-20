import fs from 'fs';
import { execFileSync } from 'child_process';
import { WORKSPACE } from './index.js';
import type { BuildConfig } from './index.js';
import { formatProjectContext } from './prompts/project-context.js';

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
  getMandatoryChecks,
} from './prompts/analysis.js';
export {
  buildSelfReviewPrompt,
  formatSelfReviewBlindSpots,
} from './prompts/self-review.js';

// ── Agent Permission Enforcement ────────────────────────────────────────
// Write .claude/settings.json in the workspace to restrict what the inner
// Claude Code agent can do.  Each task type gets appropriate permissions.

const READ_ONLY_TYPES = new Set([
  'research', 'plan', 'review', 'security_audit', 'smoketest',
  'debug', 'perf', 'investigate', 'design_doc_update', 'strategic_review',
]);

// Write types get no restrictions (default behavior). Listed for documentation:
// create, fix, refactor, docs, qa, deps, migrate

/**
 * Configure Claude Code permissions for the current task type.
 * Returns a list of disallowed tools to pass to runClaudeCode's --disallowedTools flag.
 * Also installs git hooks to block commit/push for read-only agents.
 * Call this AFTER cloning the repo but BEFORE running Claude Code.
 */
export function configureAgentPermissions(taskType: string): string[] {
  if (READ_ONLY_TYPES.has(taskType)) {
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
  const then = new Date(iso);
  if (Number.isNaN(then.getTime())) return '';
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
  'perf': 'Profile before optimizing. Identify the specific bottleneck with measurements before writing any fix.',
  'create': 'Before finishing: Run the relevant lint, build, and test commands for every area you modified (check the Codebase Instructions for commands). Fix any failures. Do not leave broken tests, lint errors, build failures, or conflict markers. If the task involves libraries or frameworks you\'re uncertain about, fetch their official documentation before writing code. IMPORTANT: Do NOT run git commit, git rebase, or git push — the automation framework handles committing, rebasing, and pushing after you exit.',
  'fix': 'You are fixing issues from a code review or a pipeline failure (merge conflict or failing CI). Read ALL feedback carefully and address every point. IMPORTANT: the spawn prompt can be stale — between when this task was queued and when you run, a concurrent rollup may have resolved conflicts, or a new commit may have broken something new. If the prompt includes a "stale-premise notice", trust the latest state over the original description. If the error involves a third-party library, fetch its docs or search for the specific error message. Before declaring done, verify that local lint/type/test suites pass on the changed files. Run the relevant test suite to confirm your fix does not introduce regressions. IMPORTANT: Do NOT run git commit, git rebase, or git push — the automation framework handles committing, rebasing, and pushing after you exit.',
  'docs': 'Read the existing README and documentation files first. Understand the current documentation structure before making changes.',
  'deps': 'Check for breaking changes in major version bumps. Run the full test suite after updating to verify compatibility.',
  'refactor': 'Run the full test suite BEFORE and AFTER changes. Zero test regressions allowed. Do not change external behavior. If the refactor involves libraries or frameworks you\'re uncertain about, fetch their official documentation before writing code. IMPORTANT: Do NOT run git commit, git rebase, or git push — the automation framework handles committing, rebasing, and pushing after you exit.',
  'investigate': 'This is a read-only investigation. Do NOT modify any files. Search for known issues related to error patterns you discover. Write your findings to the output JSON only.',
  'debug': 'Search for known issues related to error patterns you discover. Use WebSearch to look up specific error messages or stack traces when local context is insufficient.',
};

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
    hints['create'] = `Before finishing: Run the relevant lint, build, and test commands for every area you modified (${cmdsText}). Fix any failures. Do not leave broken tests, lint errors, build failures, or conflict markers. If the task involves libraries or frameworks you're uncertain about, fetch their official documentation before writing code. IMPORTANT: Do NOT run git commit, git rebase, or git push — the automation framework handles committing, rebasing, and pushing after you exit.`;
    hints['refactor'] = `Run the full test suite BEFORE and AFTER changes (${cmdsText}). Zero test regressions allowed. Do not change external behavior. If the refactor involves libraries or frameworks you're uncertain about, fetch their official documentation before writing code. IMPORTANT: Do NOT run git commit, git rebase, or git push — the automation framework handles committing, rebasing, and pushing after you exit.`;
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

// ── Git exclude for agent helper scripts ────────────────────────────────────
// Agent helper scripts (playbook, messages, note, strategic, design-doc) are
// written to WORKSPACE for Claude Code to use. Exclude them from git so they
// don't get committed into PRs via `git add -A`.
const AGENT_HELPER_NAMES = ['playbook', 'messages', 'note', 'strategic', 'design-doc'];

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

export interface PlaybookEntry {
  content: string;
  effectiveness_score: number;
  confirmed_count: number;
  age_days: number;
  stale: boolean;
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
        content = value.content;
        if (value.stale || value.effectiveness_score < 0.3) {
          caveat = '> ⚠ Possibly outdated or low confidence — verify before applying.\n\n';
        }
      }
      return `### ${key}\n${caveat}${content}`;
    })
    .join('\n\n');
  return `## Operational Playbook (knowledge from past agents)\n\nThese notes were left by previous agents working on this codebase. Trust and follow them — they represent hard-won operational knowledge.\n\n${lines}\n\n`;
}

export const PLAYBOOK_GUIDANCE = `
## Operational Playbook — Save What You Learn
The playbook persists operational knowledge across all agent tasks in this project. Use \`./playbook set <key> <value>\` to save reusable knowledge. Use \`./playbook get\` to review existing entries.

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
      const s = scopeRates[scope];
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
        const created = Number(counts?.created ?? 0);
        const merged = Number(counts?.merged ?? 0);
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
  if (projectCtx) {
    const hasData = Object.keys(projectCtx).some(k => {
      const v = projectCtx[k]; return Array.isArray(v) && v.length > 0;
    });
    if (hasData) {
      const currentDirectionId = taskContext ? strVal(taskContext.direction_id) : '';
      prefix = formatProjectContext(projectCtx, taskType, taskScope, currentDirectionId) + '\n\n';
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
      const parts = recentOutcomes.map(o => `${o.prs_merged}/${o.tasks_created} merged`);
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
        const created = Number(counts?.created ?? 0);
        const merged = Number(counts?.merged ?? 0);
        if (created < 1) continue;
        const pct = Math.round((merged / created) * 100);
        const failed = Number(counts?.failed ?? 0);
        const suffix = failed > 0 ? `, ${failed} failed` : '';
        parts.push(`${scope} ${merged}/${created} (${pct}%${suffix})`);
      }
      if (parts.length > 0) {
        dcLines.push(`Scope merge rates: ${parts.join(', ')}`);
      }
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
        const parts: string[] = [];
        for (const [k, v] of Object.entries(signals)) {
          if (v === null || v === undefined) continue;
          if (typeof v === 'object') {
            parts.push(`${k}=${JSON.stringify(v)}`);
          } else {
            parts.push(`${k}=${String(v)}`);
          }
        }
        if (parts.length > 0) {
          signalLines.push(`- Signals: ${parts.join(', ')}`);
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
      attempts = dirCtx.remediation_attempts as number;
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
  const previousError = strVal(ctx._previous_error as unknown);
  const previousFailureClass = strVal(ctx._previous_failure_class as unknown);
  const previousPrUrl = strVal(ctx._previous_pr_url as unknown);
  const previousChangedFiles = Array.isArray(ctx._previous_changed_files)
    ? (ctx._previous_changed_files as unknown[]).map(f => strVal(f)).filter(Boolean)
    : [];
  const previousFailureStage = strVal(ctx._previous_failure_stage as unknown);
  const previousErrorSummary = strVal(ctx._previous_error_summary as unknown);
  const previousIsRecurring = ctx._previous_is_recurring === true;
  const failureClassHistory = Array.isArray(ctx._failure_class_history)
    ? (ctx._failure_class_history as unknown[]).map(f => strVal(f)).filter(Boolean)
    : [];
  const previousDiffSummary = Array.isArray(ctx._previous_diff_summary)
    ? (ctx._previous_diff_summary as unknown[]).filter(
        (e): e is Record<string, unknown> => typeof e === 'object' && e !== null,
      )
    : [];
  const previousTestFailureSummary = strVal(ctx._previous_test_failure_summary as unknown);
  const previousSelfReviewOutcome = ctx._previous_self_review_outcome as Record<string, unknown> | undefined;
  const previousAnalysisPlan = strVal(ctx._previous_analysis_plan as unknown);
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
      const file = strVal(e.file as unknown);
      const additions = typeof e.additions === 'number' ? e.additions : 0;
      const deletions = typeof e.deletions === 'number' ? e.deletions : 0;
      const changeType = strVal(e.change_type as unknown) || 'modified';
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
