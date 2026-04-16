import fs from 'fs';
import { execFileSync } from 'child_process';
import type WebSocket from 'ws';
import { sendLog, WORKSPACE } from './index.js';
import type { BuildConfig } from './index.js';

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

function strVal(v: unknown, fallback = ''): string {
  return typeof v === 'string' ? v : fallback;
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

export function formatPlaybook(playbook: Record<string, string> | undefined): string {
  if (!playbook || Object.keys(playbook).length === 0) return '';
  const lines = Object.entries(playbook)
    .map(([key, content]) => `### ${key}\n${content}`)
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
      prefix = formatProjectContext(projectCtx, taskType, taskScope) + '\n\n';
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
    const failedApproaches = Array.isArray(dirCtx.failed_approaches) ? dirCtx.failed_approaches as string[] : [];
    if (failedApproaches.length > 0) {
      dcLines.push('**Avoid (already tried and failed):**');
      for (const fa of failedApproaches) {
        dcLines.push(`- ${fa}`);
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
      if (msg) dcLines.push(`**Failure pattern:** ${msg}`);
    }
    dcLines.push('');
    prefix += dcLines.join('\n') + '\n';
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
    const scopeLines: string[] = ['## Task Scope'];
    const scopeLine = scopeDesc ? `Scope: ${taskScope} — ${scopeDesc}` : `Scope: ${taskScope}`;
    scopeLines.push(scopeLine);
    if (effort) scopeLines.push(`Effort: ${effort}`);
    scopeLines.push('');
    prefix += scopeLines.join('\n') + '\n';
  }

  return prefix;
}

export function buildFileTree(ws: WebSocket | null): string {
  try {
    // Get files ranked by recent git activity
    let recentFiles: string[] = [];
    try {
      const gitLog = execFileSync('git', ['log', '--pretty=format:', '--name-only', '-100'],
        { cwd: WORKSPACE, encoding: 'utf-8' });
      const counts: Record<string, number> = {};
      for (const f of gitLog.split('\n').filter(Boolean)) {
        counts[f] = (counts[f] ?? 0) + 1;
      }
      recentFiles = Object.entries(counts)
        .sort((a, b) => b[1] - a[1])
        .map(([f]) => f);
    } catch { /* fallback to unsorted */ }

    // Get all tracked files
    const allFiles = execFileSync('git', ['ls-files'],
      { cwd: WORKSPACE, encoding: 'utf-8' })
      .split('\n')
      .filter(f => f && !f.includes('node_modules/') && !f.includes('dist/'));

    // Merge: recent files first, then remaining alphabetically
    const recentSet = new Set(recentFiles);
    const remaining = allFiles.filter(f => !recentSet.has(f)).sort();
    const sorted = [...recentFiles.filter(f => allFiles.includes(f)), ...remaining];
    const tree = sorted.slice(0, 150).map(f => './' + f).join('\n');

    return tree
      ? `## Repository File Tree (first 150 files, sorted by recent activity)\n\`\`\`\n${tree}\n\`\`\`\n\n`
      : '';
  } catch (e) {
    sendLog(ws, `Warning: file tree generation failed: ${String(e)}\n`);
    return '';
  }
}

// ── Test Convention Extraction ───────────────────────────────────────────────
// Cache: dirKey → convention summary string. Populated once per task invocation.
const _testConventionCache = new Map<string, string>();

/**
 * Scan test files in the given directory and extract conventions:
 * fixtures, mock libraries, setup patterns, assertion style.
 * Returns a compact summary (<=500 chars). Cached per dirKey.
 */
export function extractTestConventions(dirKey: string, testFiles: string[]): string {
  if (_testConventionCache.has(dirKey)) return _testConventionCache.get(dirKey)!;

  const fixtures = new Set<string>();
  const mockImports = new Set<string>();
  const setupPatterns = new Set<string>();
  const assertionStyles = new Set<string>();

  for (const tf of testFiles.slice(0, 6)) {
    const full = `${WORKSPACE}/${tf}`;
    if (!fs.existsSync(full)) continue;
    try {
      const lines = fs.readFileSync(full, 'utf-8').split('\n').slice(0, 120);
      for (const line of lines) {
        // Python fixture decorators (names extracted below via full-content regex)
        // Python mock imports
        if (line.match(/from\s+unittest\.mock\s+import/)) {
          const imported = line.match(/import\s+(.+)/)?.[1].trim();
          if (imported) mockImports.add(`unittest.mock: ${imported.slice(0, 50)}`);
        }
        if (line.match(/from\s+pytest_mock/)) mockImports.add('pytest_mock');
        // JS/TS mock imports
        if (line.match(/vi\.fn\(\)|vi\.mock\(|vitest/)) mockImports.add('vitest (vi.fn/vi.mock)');
        if (line.match(/jest\.fn\(\)|jest\.mock\(/)) mockImports.add('jest');
        // Setup patterns
        if (line.match(/beforeEach\s*\(/)) setupPatterns.add('beforeEach');
        if (line.match(/afterEach\s*\(/)) setupPatterns.add('afterEach');
        if (line.match(/beforeAll\s*\(/)) setupPatterns.add('beforeAll');
        if (line.match(/def\s+setup(?:_method)?\s*\(/)) setupPatterns.add('setUp');
        if (line.match(/@pytest\.fixture/)) setupPatterns.add('@pytest.fixture');
        if (line.match(/@pytest_asyncio\.fixture/)) setupPatterns.add('@pytest_asyncio.fixture');
        // Assertion styles
        if (line.match(/expect\s*\(.*\)\.(toBe|toEqual|toHaveBeenCalled)/)) assertionStyles.add('expect().toBe/toEqual');
        if (line.match(/\bassert\s+/)) assertionStyles.add('assert');
        if (line.match(/pytest\.raises\s*\(/)) assertionStyles.add('pytest.raises');
        if (line.match(/assert_called(?:_once)?_with/)) assertionStyles.add('mock.assert_called_with');
        if (line.match(/AsyncMock\s*\(/)) mockImports.add('AsyncMock');
        if (line.match(/MagicMock\s*\(/)) mockImports.add('MagicMock');
      }

      // Fixture names: scan for @pytest.fixture + next def
      const fullContent = lines.join('\n');
      const fixturePattern = /@(?:pytest_asyncio\.fixture|pytest\.fixture)[^\n]*\n(?:async )?def\s+(\w+)/g;
      let fm: RegExpExecArray | null;
      while ((fm = fixturePattern.exec(fullContent)) !== null) {
        fixtures.add(fm[1]);
      }
    } catch { /* skip */ }
  }

  const parts: string[] = [];
  if (fixtures.size > 0) parts.push(`Fixtures: ${[...fixtures].slice(0, 6).join(', ')}`);
  if (mockImports.size > 0) parts.push(`Mocks: ${[...mockImports].slice(0, 4).join(', ')}`);
  if (setupPatterns.size > 0) parts.push(`Setup: ${[...setupPatterns].join(', ')}`);
  if (assertionStyles.size > 0) parts.push(`Assertions: ${[...assertionStyles].join(', ')}`);

  const summary = parts.length > 0 ? parts.join(' | ') : '';
  const result = summary.slice(0, 500);
  _testConventionCache.set(dirKey, result);
  return result;
}

export function discoverRelatedTests(ws: WebSocket | null, relatedFiles: string[], buildConfig?: BuildConfig): string {
  if (relatedFiles.length === 0) return '';

  try {
    const testEntries: string[] = [];
    const seen = new Set<string>();
    // Track test files per directory key for convention extraction
    const testFilesByDir = new Map<string, string[]>();

    const extractTestNames = (content: string): string[] => {
      const names: string[] = [];
      for (const line of content.split('\n')) {
        const m = line.match(/^\s*(?:def (test_\w+)|it\(['"`]([^'"`]+)['"`]|test\(['"`]([^'"`]+)['"`]|describe\(['"`]([^'"`]+)['"`])/);
        if (m) names.push(m[1] || m[2] || m[3] || m[4]);
      }
      return names;
    };

    const processTestFile = (testPath: string) => {
      const full = `${WORKSPACE}/${testPath}`;
      if (seen.has(testPath) || !fs.existsSync(full)) return;
      seen.add(testPath);
      try {
        const content = fs.readFileSync(full, 'utf-8').split('\n').slice(0, 80).join('\n');
        const names = extractTestNames(content);
        if (names.length > 0) {
          testEntries.push(`- ${testPath}: ${names.slice(0, 10).join(', ')}`);
        } else {
          testEntries.push(`- ${testPath}`);
        }
        // Track by directory key for convention extraction
        const dirKey = testPath.startsWith('backend/') ? 'backend'
          : testPath.startsWith('agent/') ? 'agent'
          : testPath.startsWith('admin-ui/') ? 'admin-ui'
          : testPath.split('/')[0];
        if (!testFilesByDir.has(dirKey)) testFilesByDir.set(dirKey, []);
        testFilesByDir.get(dirKey)!.push(testPath);
      } catch { /* skip unreadable */ }
    };

    // Build a lookup from directory prefix → directory type using buildConfig if provided
    const dirConfigs = buildConfig?.directories ?? [];


    for (const relFile of relatedFiles) {
      // Convention-based discovery: use buildConfig.directories if available, else hardcoded Blueprint paths
      let discoveredByConvention = false;

      if (dirConfigs.length > 0) {
        for (const dir of dirConfigs) {
          const dirPrefix = dir.path.replace(/\/$/, '');
          if (!relFile.startsWith(dirPrefix + '/')) continue;
          discoveredByConvention = true;

          if (dir.type === 'python' && relFile.endsWith('.py')) {
            // Look for test_<module>.py in common test directories
            const base = relFile.replace(new RegExp(`^${dirPrefix}/`), '').replace(/\.py$/, '');
            const moduleName = base.split('/').pop() || base;
            for (const testDir of [`${dirPrefix}/testing`, `${dirPrefix}/tests`, `${dirPrefix}/test`]) {
              processTestFile(`${testDir}/test_${moduleName}.py`);
            }
          } else if ((dir.type === 'node') && relFile.match(/\.(tsx?|jsx?)$/) && !relFile.includes('.test.')) {
            const testPath = relFile.replace(/\.(tsx?|jsx?)$/, (ext) => `.test${ext}`);
            processTestFile(testPath);
            // Also search for any test files in this directory
            try {
              const nodeTestFiles = execFileSync('git', ['ls-files', dirPrefix + '/'],
                { cwd: WORKSPACE, encoding: 'utf-8' })
                .split('\n')
                .filter(f => f.includes('.test.') || f.includes('.spec.'));
              for (const tf of nodeTestFiles) processTestFile(tf);
            } catch { /* skip */ }
          } else {
            // Unknown type: fall through to grep-based discovery
            discoveredByConvention = false;
          }
        }
      } else {
        // Hardcoded Blueprint conventions
        if (relFile.startsWith('backend/') && relFile.endsWith('.py')) {
          const base = relFile.replace(/^backend\/app\//, '').replace(/\.py$/, '');
          const candidate = `backend/testing/test_${base.split('/').pop()}.py`;
          processTestFile(candidate);
          discoveredByConvention = true;
        } else if (relFile.match(/^admin-ui\/.*\.(tsx?|jsx?)$/) && !relFile.includes('.test.')) {
          const testPath = relFile.replace(/\.(tsx?|jsx?)$/, (ext) => `.test${ext}`);
          processTestFile(testPath);
          discoveredByConvention = true;
        } else if (relFile.startsWith('agent/')) {
          // Search for any test files in agent/
          try {
            const agentTestFiles = execFileSync('git', ['ls-files', 'agent/'],
              { cwd: WORKSPACE, encoding: 'utf-8' })
              .split('\n')
              .filter(f => f.includes('.test.') || f.includes('test_') || f.includes('/tests/'));
            for (const tf of agentTestFiles) processTestFile(tf);
          } catch { /* skip */ }
          discoveredByConvention = true;
        }
      }

      // Grep-based discovery: find test files importing from this module (always run)
      try {
        const moduleName = relFile.split('/').pop()?.replace(/\.(tsx?|jsx?|py)$/, '') || '';
        if (moduleName) {
          const grepResult = execFileSync('git', ['grep', '-l', moduleName, '--', '*test*'],
            { cwd: WORKSPACE, encoding: 'utf-8' });
          for (const tf of grepResult.split('\n').filter(Boolean)) {
            processTestFile(tf);
          }
        }
      } catch { /* no matches */ }

      void discoveredByConvention; // used for control flow, not needed after loop
    }

    if (testEntries.length === 0) return '';

    // Extract test conventions per directory type
    const conventionLines: string[] = [];
    for (const [dirKey, files] of testFilesByDir) {
      const conventions = extractTestConventions(dirKey, files);
      if (conventions) conventionLines.push(`${dirKey}: ${conventions}`);
    }
    const conventionsSection = conventionLines.length > 0
      ? `## Test Conventions\n${conventionLines.join('\n')}\n\n`
      : '';

    const output = `## Related Tests\nThese tests cover files you'll be modifying:\n${testEntries.join('\n')}\n\n${conventionsSection}`;
    return output.slice(0, 3000);
  } catch (e) {
    sendLog(ws, `Warning: related test discovery failed: ${String(e)}\n`);
    return '';
  }
}

function parseResearchSummary(raw: string): string {
  try {
    const parsed = JSON.parse(raw) as Record<string, unknown>;
    const items = Array.isArray(parsed) ? parsed : (Array.isArray(parsed.logs) ? parsed.logs : []);
    if (Array.isArray(items) && items.length > 0) {
      return (items as Record<string, unknown>[]).slice(0, 3)
        .map((f) => `${strVal(f.title, '?')}: ${(strVal(f.description) || strVal(f.prompt)).slice(0, 120)}`)
        .join(' | ');
    }
  } catch { /* ignore */ }
  return raw.slice(0, 400);
}

// ── Shared: Format project_context into a markdown section ────────────────
export function formatProjectContext(ctx: Record<string, unknown>, taskType = '', taskScope = ''): string {
  const CONTEXT_BUDGET = 8000;

  const header = [
    '## Project Context',
    'The following tasks are already active or completed in this project. Avoid duplicating their work.',
    '',
  ];

  // Build active tasks section (always included in full)
  const activeLines: string[] = [];
  const activeTasks = ctx.active_tasks as Array<Record<string, unknown>> | undefined;
  if (activeTasks && activeTasks.length > 0) {
    activeLines.push('### Active Tasks');
    for (const t of activeTasks) {
      const files = (t.related_files as string[] | undefined);
      const filesSuffix = files && files.length > 0 ? ` (files: ${files.join(', ')})` : '';
      activeLines.push(`- [${strVal(t.task_type)}] **${strVal(t.title)}** (${strVal(t.status)}) — ${strVal(t.prompt).slice(0, 400)}${filesSuffix}`);
    }
    activeLines.push('');
  }

  // Build open PRs section (always included in full)
  const prLines: string[] = [];
  const openPRs = ctx.open_prs as string[] | undefined;
  if (openPRs && openPRs.length > 0) {
    prLines.push('### Open PRs');
    for (const pr of openPRs) {
      prLines.push(`- ${pr}`);
    }
    prLines.push('');
  }

  // Build sibling tasks section (always included in full)
  const siblingLines: string[] = [];
  const siblingTasks = ctx.sibling_tasks as Array<Record<string, unknown>> | undefined;
  if (siblingTasks && siblingTasks.length > 0) {
    siblingLines.push('## Sibling Tasks (parallel tasks from the same parent — avoid duplicating their work)');
    for (const t of siblingTasks) {
      const files = (t.related_files as string[] | undefined);
      const filesSuffix = files && files.length > 0 ? ` (files: ${files.join(', ')})` : '';
      siblingLines.push(`- [${strVal(t.status)}] ${strVal(t.title)}${filesSuffix}`);
    }
    siblingLines.push('');
  }

  // Build co-change affinity section — always included for create/fix/refactor (concise, actionable)
  const cochangeFiles = ctx.cochange_files as string[] | undefined;
  const cochangeLines: string[] = [];
  if (cochangeFiles && cochangeFiles.length > 0) {
    cochangeLines.push('## Frequently Co-Changed Files');
    cochangeLines.push('Based on merged PRs in this project, these files are commonly modified together with your task\'s related files — review and update them if relevant:');
    for (const f of cochangeFiles) {
      cochangeLines.push(`- ${f}`);
    }
    cochangeLines.push('');
  }

  const fixedContent = [...header, ...activeLines, ...prLines, ...siblingLines, ...cochangeLines].join('\n');
  let budget = CONTEXT_BUDGET - fixedContent.length;

  // Build lessons section first — lessons directly prevent mistakes and take priority over informational sections
  const lessonsData = ctx.lessons as Array<Record<string, unknown>> | undefined;
  let lessonsLines: string[] = [];
  if (lessonsData && lessonsData.length > 0) {
    const candidateLines: string[] = ['### Lessons from Past Reviews'];
    for (const l of lessonsData) {
      const fcSuffix = l.failure_class ? ` [${strVal(l.failure_class)}]` : '';
      candidateLines.push(`- **${strVal(l.task_title)}** (${String(l.iterations)} cycles)${fcSuffix}: ${strVal(l.last_feedback)}`);
    }
    candidateLines.push('');
    const candidateContent = candidateLines.join('\n');
    if (candidateContent.length <= budget) {
      lessonsLines = candidateLines;
      budget -= candidateContent.length;
    }
  }

  // Build queued tasks section — one-liners only to preserve budget for higher-priority content
  const queuedTasks = ctx.queued_tasks as Array<Record<string, unknown>> | undefined;
  let queuedLines: string[] = [];
  if (queuedTasks && queuedTasks.length > 0) {
    const shortLines: string[] = ['### Queued Tasks'];
    for (const t of queuedTasks) {
      shortLines.push(`- [${strVal(t.task_type)}] **${strVal(t.title)}**`);
    }
    shortLines.push('');
    const shortContent = shortLines.join('\n');
    if (shortContent.length <= budget) {
      queuedLines = shortLines;
      budget -= shortContent.length;
    }
  }

  // Build recently completed section — drop oldest entries to fit budget
  const recentTasks = ctx.recently_completed_tasks as Array<Record<string, unknown>> | undefined;
  let recentLines: string[] = [];
  let recentDropped = 0;
  if (recentTasks && recentTasks.length > 0) {
    const allEntries: string[] = [];
    for (const t of recentTasks) {
      const pr = t.pr_url ? ` (${strVal(t.pr_url)})` : '';
      const summary = t.research_findings_summary ? ` — ${parseResearchSummary(strVal(t.research_findings_summary))}` : '';
      const failureClass = t.failure_class ? ` [${strVal(t.failure_class)}]` : '';
      const failureSuffix = t.failure_reason ? ` (FAILED${failureClass}: ${strVal(t.failure_reason)})` : '';
      const reviewSuffix = (typeof t.review_iterations === 'number' && t.review_iterations > 1) ? ` (${t.review_iterations} review cycles)` : '';
      allEntries.push(`- [${strVal(t.task_type)}] **${strVal(t.title)}**${pr}${summary}${failureSuffix}${reviewSuffix}`);
    }

    // Keep most recent entries that fit within budget
    const headerLine = '### Recently Completed Tasks';
    const kept = allEntries.slice(); // most recent assumed to be last
    while (kept.length > 0) {
      const candidate = [headerLine, ...kept, ''].join('\n');
      if (candidate.length <= budget) break;
      kept.shift(); // drop oldest (first entry)
      recentDropped++;
    }
    if (kept.length > 0) {
      recentLines = [headerLine, ...kept, ''];
      budget -= recentLines.join('\n').length;
    } else {
      recentDropped = allEntries.length;
    }
  }

  // Build pipeline health section — only if budget allows, capped at 400 chars
  const pipelineHealth = ctx.pipeline_health as Record<string, unknown> | null | undefined;
  let healthLines: string[] = [];
  if (pipelineHealth) {
    const parts: string[] = [];

    const successRate = pipelineHealth.success_rate;
    const trend = pipelineHealth.trend as string | undefined;
    if (successRate !== null && successRate !== undefined) {
      const pct = Math.round((successRate as number) * 100);
      const trendStr = trend ? ` (${trend})` : '';
      parts.push(`Success rate: ${pct}%${trendStr}.`);
    }

    const fasr = pipelineHealth.first_attempt_success_rate as Record<string, unknown> | undefined;
    if (fasr && fasr.overall !== null && fasr.overall !== undefined && (fasr.sample_size as number) > 0) {
      const faPct = Math.round((fasr.overall as number) * 100);
      const faTrend = fasr.trend as string | undefined;
      const faTrendStr = faTrend ? ` (${faTrend})` : '';
      const byType = fasr.by_type as Record<string, number> | undefined;
      let perTypeSuffix = '';
      if (taskType && byType && byType[taskType] !== undefined) {
        const typePct = Math.round(byType[taskType] * 100);
        perTypeSuffix = ` Your task type (${taskType}): ${typePct}%.`;
      }
      const byScope = fasr.by_scope as Record<string, number> | undefined;
      let perScopeSuffix = '';
      if (taskScope && byScope && byScope[taskScope] !== undefined) {
        const scopePct = Math.round(byScope[taskScope] * 100);
        perScopeSuffix = ` Your scope (${taskScope}): ${scopePct}%.`;
      }
      parts.push(`First-attempt success: ${faPct}%${faTrendStr}, sample: ${String(fasr.sample_size)}.${perTypeSuffix}${perScopeSuffix}`);
    }

    const failureDist = pipelineHealth.failure_distribution as Record<string, number> | undefined;
    if (failureDist && Object.keys(failureDist).length > 0) {
      const failStr = Object.entries(failureDist)
        .map(([cls, cnt]) => `${cls} (${cnt})`)
        .join(', ');
      parts.push(`Top failures: ${failStr}.`);
    }

    const dirStats = pipelineHealth.direction_stats as Array<Record<string, unknown>> | undefined;
    if (dirStats && dirStats.length > 0) {
      const dirStr = dirStats
        .map((d) => {
          const did = strVal(d.direction_id).slice(-8);
          const sat = d.possibly_saturated ? ', saturated' : '';
          return `d-${did} merge rate ${String(d.merge_rate)} (${String(d.cycles)} cycles${sat})`;
        })
        .join(', ');
      parts.push(`Direction ROI: ${dirStr}.`);
    }

    if (taskType === 'review') {
      const reviewAccuracy = pipelineHealth.review_accuracy as Record<string, unknown> | null | undefined;
      if (reviewAccuracy) {
        const precision = reviewAccuracy.precision as number;
        const overrides = reviewAccuracy.overrides as number;
        const precPct = Math.round(precision * 100);
        const recall = reviewAccuracy.recall as number | null | undefined;
        const misses = reviewAccuracy.misses as number;
        let accuracyStr = `Review accuracy: ${precPct}% precision (${overrides} overrides)`;
        if (recall !== null && recall !== undefined) {
          const recPct = Math.round(recall * 100);
          accuracyStr += `, ${recPct}% recall (${misses} user-caught issues)`;
        }
        accuracyStr += '.';
        parts.push(accuracyStr);
      }
    }

    if (parts.length > 0) {
      const body = parts.join(' ');
      const candidate = `### Pipeline Health (last 24h)\n${body}\n`;
      if (candidate.length <= 400 && candidate.length <= budget) {
        healthLines = ['### Pipeline Health (last 24h)', body, ''];
        budget -= candidate.length;
      }
    }
  }

  // Build review issue distribution section — only if budget allows
  const reviewIssueDist = ctx.review_issue_distribution as Record<string, number> | null | undefined;
  // First-attempt-specific breakdown from pipeline health (more actionable than all-iterations data)
  const fasrForReview = pipelineHealth?.first_attempt_success_rate as Record<string, unknown> | undefined;
  const firstAttemptByCat = fasrForReview?.first_attempt_by_review_category as Record<string, number> | null | undefined;
  let reviewIssueLines: string[] = [];
  if (reviewIssueDist && Object.keys(reviewIssueDist).length > 0) {
    const hasFirstAttemptCats = firstAttemptByCat && Object.keys(firstAttemptByCat).length > 0;
    const descLine = hasFirstAttemptCats
      ? 'Issues flagged in this project\'s review cycles — first-attempt rejection rate shown in parentheses (prioritize these before submitting):'
      : 'Issues flagged most often in this project\'s review cycles (prioritize these before submitting):';
    const candidateLines = ['### Common Review Issues', descLine];
    const entries = Object.entries(reviewIssueDist).sort((a, b) => b[1] - a[1]);
    for (const [cat, pct] of entries) {
      const faPct = hasFirstAttemptCats && firstAttemptByCat![cat] !== undefined
        ? ` (${Math.round(firstAttemptByCat![cat] * 100)}% of first-attempt rejections)`
        : '';
      candidateLines.push(`- **${cat}**: ${Math.round(pct * 100)}% of review feedback${faPct}`);
    }
    // Append specific recurring patterns if available
    type FirstReviewIssue = { description: string; count: number; category: string };
    const topIssues = ctx.top_first_review_issues as FirstReviewIssue[] | null | undefined;
    if (topIssues && topIssues.length > 0) {
      candidateLines.push('**Specific patterns to avoid:**');
      for (const issue of topIssues) {
        candidateLines.push(`- "${issue.description}" (seen ${issue.count}x, category: ${issue.category})`);
      }
    }
    candidateLines.push('');
    const candidate = candidateLines.join('\n');
    if (candidate.length <= budget) {
      reviewIssueLines = candidateLines;
      budget -= candidate.length;
    }
  }

  // Build per-module review feedback warnings — only if budget allows
  type ModuleFeedbackWarning = { file: string; top_categories: Record<string, number>; sample_size: number };
  const moduleFeedbackWarnings = ctx.module_feedback_warnings as ModuleFeedbackWarning[] | null | undefined;
  let moduleFeedbackLines: string[] = [];
  if (moduleFeedbackWarnings && moduleFeedbackWarnings.length > 0) {
    const candidateLines: string[] = ['### Module-Specific Review History'];
    for (const w of moduleFeedbackWarnings) {
      const catParts = Object.entries(w.top_categories)
        .sort((a, b) => b[1] - a[1])
        .map(([cat, pct]) => `${Math.round(pct * 100)}% ${cat}`)
        .join(', ');
      candidateLines.push(`- When modifying \`${w.file}\`, review history shows: ${catParts} (${w.sample_size} issues)`);
    }
    candidateLines.push('');
    const candidate = candidateLines.join('\n');
    if (candidate.length <= budget) {
      moduleFeedbackLines = candidateLines;
      budget -= candidate.length;
    }
  }

  // Build direction quality warning — only if budget allows, placed after Pipeline Health
  const directionQuality = ctx.direction_quality as Record<string, unknown> | null | undefined;
  let dirQualityLines: string[] = [];
  if (directionQuality) {
    const rate = directionQuality.acceptance_rate as number;
    const merged = directionQuality.prs_merged as number;
    const rejected = directionQuality.prs_rejected as number;
    const avgTtm = directionQuality.avg_time_to_merge_hours as number | null | undefined;
    const rejCtx = directionQuality.rejection_context as Array<Record<string, unknown>> | undefined;

    const qualityParts: string[] = [
      `Operator acceptance rate for this direction: ${Math.round(rate * 100)}% (${merged} merged, ${rejected} rejected)`,
    ];
    if (avgTtm !== null && avgTtm !== undefined) {
      qualityParts.push(`Average time for operators to merge PRs in this direction: ${avgTtm}h`);
    }
    if (rejCtx && rejCtx.length > 0) {
      qualityParts.push('Recent rejections:');
      for (const r of rejCtx) {
        qualityParts.push(`- "${strVal(r.title)}" — ${strVal(r.reason)}`);
      }
    }
    qualityParts.push('Pay extra attention to code quality, test coverage, and alignment with project conventions.');

    const candidateLines = ['### Direction Quality Warning', ...qualityParts, ''];
    const candidate = candidateLines.join('\n');
    if (candidate.length <= budget) {
      dirQualityLines = candidateLines;
      budget -= candidate.length;
    }
  }

  // Append footer if anything was omitted
  const footerLines: string[] = [];
  const omittedCount = recentDropped;
  if (omittedCount > 0) {
    footerLines.push(`_(${omittedCount} more tasks not shown)_`);
    footerLines.push('');
  }

  const allLines = [...header, ...activeLines, ...lessonsLines, ...queuedLines, ...recentLines, ...healthLines, ...reviewIssueLines, ...moduleFeedbackLines, ...dirQualityLines, ...prLines, ...siblingLines, ...cochangeLines, ...footerLines];
  return allLines.join('\n');
}

// ── Module interface extraction ──────────────────────────────────────────────

/**
 * Extracts top-level exported symbols from a Python source file.
 * Prefers __all__ when present; otherwise scans for top-level def/class.
 */
function extractPythonExports(content: string): string[] {
  // If __all__ is defined, use it as the authoritative public surface
  const allMatch = content.match(/__all__\s*=\s*\[([\s\S]*?)\]/);
  if (allMatch) {
    return [...allMatch[1].matchAll(/['"](\w+)['"]/g)].map(m => m[1]).slice(0, 15);
  }

  const results: string[] = [];
  for (const line of content.split('\n')) {
    // Top-level functions (no leading indentation)
    const funcMatch = line.match(/^def (\w+)\(([^)]*)\)/);
    const classMatch = line.match(/^class (\w+)/);
    if (funcMatch) {
      const params = funcMatch[2].length > 50 ? funcMatch[2].slice(0, 47) + '...' : funcMatch[2];
      results.push(`def ${funcMatch[1]}(${params})`);
    } else if (classMatch) {
      results.push(`class ${classMatch[1]}`);
    }
  }
  return results;
}

/**
 * Extracts export declarations from a TypeScript/JavaScript source file.
 */
function extractTypeScriptExports(content: string): string[] {
  const results: string[] = [];
  for (const line of content.split('\n')) {
    let m: RegExpMatchArray | null;
    if ((m = line.match(/^export\s+(?:default\s+)?(?:async\s+)?function\s+(\w+)\s*(?:<[^>]*>)?\s*\(([^)]*)\)/))) {
      const params = m[2].length > 50 ? m[2].slice(0, 47) + '...' : m[2];
      results.push(`function ${m[1]}(${params})`);
    } else if ((m = line.match(/^export\s+(?:abstract\s+)?class\s+(\w+)/))) {
      results.push(`class ${m[1]}`);
    } else if ((m = line.match(/^export\s+interface\s+(\w+)/))) {
      results.push(`interface ${m[1]}`);
    } else if ((m = line.match(/^export\s+type\s+(\w+)/))) {
      results.push(`type ${m[1]}`);
    } else if ((m = line.match(/^export\s+(?:const|let)\s+(\w+)/))) {
      results.push(`const ${m[1]}`);
    }
  }
  return results;
}

/**
 * Scans each file in relatedFiles to extract its public API surface (exported
 * functions/classes/types) and which other files import from it.  Returns a
 * concise markdown section for injection into the agent prompt, capped at ~2KB.
 *
 * Only runs for create/refactor tasks with scope=medium or scope=large so that
 * small tasks are not burdened with irrelevant context.
 */
export function extractModuleInterfaces(ws: WebSocket | null, relatedFiles: string[]): string {
  if (relatedFiles.length === 0) return '';

  try {
    const CAP = 2000;
    const entries: string[] = [];

    for (const relFile of relatedFiles) {
      // Skip test files — their exports are not caller-facing APIs
      if (relFile.includes('.test.') || relFile.includes('/test_') || relFile.includes('/tests/')) continue;

      const isPython = relFile.endsWith('.py');
      const isTS = /\.(ts|tsx|js|jsx)$/.test(relFile);
      if (!isPython && !isTS) continue;

      let content: string;
      try {
        const fullPath = `${WORKSPACE}/${relFile}`;
        if (!fs.existsSync(fullPath)) continue;
        content = fs.readFileSync(fullPath, 'utf-8');
      } catch { continue; }

      const exports = isPython ? extractPythonExports(content) : extractTypeScriptExports(content);
      if (exports.length === 0) continue;

      // Find files that import this module via git grep
      const moduleName = relFile.split('/').pop()?.replace(/\.(tsx?|jsx?|py)$/, '') || '';
      let callers: string[] = [];
      if (moduleName) {
        try {
          const grepOut = execFileSync(
            'git', ['grep', '-rl', moduleName, '--', '*.ts', '*.tsx', '*.py', '*.js', '*.jsx'],
            { cwd: WORKSPACE, encoding: 'utf-8', timeout: 5000 }
          );
          callers = grepOut.split('\n')
            .filter(f => f && f !== relFile && !f.includes('.test.') && !f.includes('/test_'))
            .slice(0, 5);
        } catch { /* no matches — not an error */ }
      }

      const exportStr = exports.slice(0, 12).join(', ');
      let entry = `**${relFile}** exports: ${exportStr}`;
      if (callers.length > 0) {
        entry += `\n  Imported by: ${callers.join(', ')}`;
      }
      entries.push(entry);
    }

    if (entries.length === 0) return '';

    const body = `## Module Interfaces\nPublic API and callers for files you are modifying. Update callers when changing signatures.\n\n${entries.join('\n\n')}\n\n`;
    return body.slice(0, CAP);
  } catch (e) {
    sendLog(ws, `Warning: module interface extraction failed: ${String(e)}\n`);
    return '';
  }
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

  const previousError = strVal(ctx._previous_error as unknown);
  const previousFailureClass = strVal(ctx._previous_failure_class as unknown);
  const previousPrUrl = strVal(ctx._previous_pr_url as unknown);
  const previousChangedFiles = Array.isArray(ctx._previous_changed_files)
    ? (ctx._previous_changed_files as unknown[]).map(f => strVal(f)).filter(Boolean)
    : [];
  const previousFailureStage = strVal(ctx._previous_failure_stage as unknown);
  const previousErrorSummary = strVal(ctx._previous_error_summary as unknown);
  const previousIsRecurring = ctx._previous_is_recurring === true;
  const previousDiffSummary = Array.isArray(ctx._previous_diff_summary)
    ? (ctx._previous_diff_summary as unknown[]).filter(
        (e): e is Record<string, unknown> => typeof e === 'object' && e !== null,
      )
    : [];
  const previousTestFailureSummary = strVal(ctx._previous_test_failure_summary as unknown);
  const previousSelfReviewOutcome = ctx._previous_self_review_outcome as Record<string, unknown> | undefined;
  const prevAttempt = attemptNumber - 1;

  const failureTypeLabel = previousFailureClass || 'UNKNOWN';
  const lines: string[] = [
    `## Previous Attempt (attempt #${prevAttempt} failed)`,
    previousFailureStage
      ? `Failure type: ${failureTypeLabel} (at ${previousFailureStage} stage)`
      : `Failure type: ${failureTypeLabel}`,
  ];

  if (previousIsRecurring) {
    lines.push(
      '**This is a recurring failure pattern** — this same failure type has been seen multiple times. Consider a fundamentally different approach.',
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

// ── Self-review helpers ───────────────────────────────────────────────────────
function buildSelfReviewChecklist(taskScope?: string): string {
  if (taskScope === 'small') {
    return `Check for:
1. **Correctness** — Off-by-one errors, null/undefined handling, edge cases in the changed logic?
2. **Missing test coverage** — Is there new behavior with no corresponding test?`;
  }
  if (taskScope === 'large') {
    return `Check for:
1. **Missing test coverage** — Is there new behavior with no corresponding test?
2. **Type safety violations** — TypeScript errors, incorrect type assertions, or missing type guards?
3. **Scope violations** — Are there changes unrelated to the task requirements?
4. **Security issues** — Command injection, SQL injection, XSS, exposed secrets or tokens?
5. **Pattern violations** — Does the code violate the patterns listed in Review Failure Patterns above?
6. **Cross-file consistency** — Do interfaces, types, and exports stay consistent across all changed files?
7. **Backwards compatibility** — Are any existing callers broken by signature or behavior changes?
8. **Data model safety** — Are schema changes (DB, JSONB, API response shapes) backwards-compatible?
9. **Import/dependency verification** — Are new imports available and correctly referenced?`;
  }
  // medium or unspecified: 8-item checklist (includes cross-file and import checks for multi-file changes)
  return `This is a multi-file change — verify that types, imports, and function signatures are consistent across all changed files.

Check for:
1. **Missing test coverage** — Is there new behavior with no corresponding test?
2. **Type safety violations** — TypeScript errors, incorrect type assertions, or missing type guards?
3. **Scope violations** — Are there changes unrelated to the task requirements?
4. **Security issues** — Command injection, SQL injection, XSS, exposed secrets or tokens?
5. **Pattern violations** — Does the code violate the patterns listed in Review Failure Patterns above?
6. **Cross-file consistency** — Do interfaces, types, and exports stay consistent across all changed files?
7. **Backwards compatibility** — Are any existing callers broken by signature or behavior changes?
8. **Import/dependency verification** — Are new imports available and correctly referenced?`;
}

// ── Self-review prompt builder ───────────────────────────────────────────────
/**
 * Builds a focused self-review prompt for the agent to check its own diff
 * before committing.  Concentrates only on CRITICAL and MAJOR issues.
 */
export function buildSelfReviewPrompt(opts: {
  rawPrompt: string;
  taskTitle: string;
  diff: string;
  reviewPlaybook: Record<string, string>;
  claudeMd: string;
  buildConfig?: BuildConfig;
  changedFiles?: string[];
  reviewIssueDistribution?: Record<string, number>;
  taskScope?: string;
  scopeOverflowWarning?: string;
}): string {
  const restrictionsSection = opts.claudeMd
    ? `\n## File Restrictions (from CLAUDE.md)\n${opts.claudeMd.slice(0, 500)}\n`
    : '';

  const issueDistSection = opts.reviewIssueDistribution && Object.keys(opts.reviewIssueDistribution).length > 0
    ? (() => {
        const sorted = Object.entries(opts.reviewIssueDistribution!)
          .sort(([, a], [, b]) => b - a)
          .slice(0, 5)
          .map(([cat, count]) => `${cat} (${Math.round(count * 100)}%)`)
          .join(', ');
        return `\n## Project-Specific Review Patterns\nIn this project, the most common review issues are: ${sorted}. Prioritize these checks.\n`;
      })()
    : '';

  const playbookSection = Object.keys(opts.reviewPlaybook).length > 0
    ? `\n## Review Failure Patterns\nAvoid these known patterns:\n\n${
        Object.entries(opts.reviewPlaybook)
          .map(([k, v]) => `### ${k}\n${v}`)
          .join('\n\n')
      }\n`
    : '';

  const truncationNotice = opts.changedFiles && opts.changedFiles.length > 0
    ? `\n\n⚠️ WARNING: The diff above was truncated. The following files were changed but may not be fully shown:\n${opts.changedFiles.map(f => `- ${f}`).join('\n')}\nRead any truncated files directly with cat/head before declaring SELF-REVIEW PASSED.`
    : '';
  const diffSection = opts.diff
    ? `\n## Diff (changes since base branch)\n\`\`\`diff\n${opts.diff}\n\`\`\`${truncationNotice}\n`
    : '\n## Diff\nNo diff available — review the changed files directly.\n';

  const buildConfig = opts.buildConfig;
  let buildVerificationSection: string;
  if (buildConfig && buildConfig.directories && buildConfig.directories.length > 0) {
    const checks = buildConfig.directories
      .filter(d => d.build_cmd || d.lint_cmd)
      .map(d => `- \`${d.build_cmd || d.lint_cmd}\` (in \`${d.path}/\`)`);
    if (checks.length > 0) {
      buildVerificationSection = `\n## Build Verification\nRun these build commands. If any fail, fix the errors before proceeding with the review checklist.\n\n${checks.join('\n')}\n`;
    } else {
      buildVerificationSection = '';
    }
  } else {
    buildVerificationSection = `\n## Build Verification\nRun these build commands. If any fail, fix the errors before proceeding with the review checklist.\n\n- For frontend changes (admin-ui/): \`cd admin-ui && npm run build\`\n- For agent changes (agent/): \`cd agent && npx tsc --noEmit\`\n- For backend changes (backend/): \`cd backend && python -m py_compile app/main.py\`\n`;
  }

  const scopeOverflowSection = opts.scopeOverflowWarning
    ? `\n## ⚠️ Scope Overflow Warning\n${opts.scopeOverflowWarning}\nFor each file listed, confirm it is necessary for this task. Revert any files that are not essential.\n`
    : '';

  return `You are performing a self-review of the changes you just made for this task.

## Task Requirements
${opts.taskTitle ? `**Title:** ${opts.taskTitle}\n\n` : ''}${opts.rawPrompt.slice(0, 1500)}
${restrictionsSection}${issueDistSection}${playbookSection}${scopeOverflowSection}${diffSection}${buildVerificationSection}
## Self-Review Instructions

Review ONLY for CRITICAL and MAJOR issues. Skip minor style or naming feedback.

${buildSelfReviewChecklist(opts.taskScope)}

If you find CRITICAL or MAJOR issues, fix them and stage the changes with \`git add\`.
If everything looks good, output exactly: SELF-REVIEW PASSED`;
}
