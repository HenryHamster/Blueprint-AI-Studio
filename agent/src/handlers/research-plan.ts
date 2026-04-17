import fs from 'fs';
import { execFileSync } from 'child_process';
import type WebSocket from 'ws';
import {
  WORKSPACE, strVal, sanitize, sendLog, readHandlerOutput,
  apiPost, persistAndPostStatus, cloneWithRetry, runClaudeCode,
} from '../index.js';
import { DEFAULT_CLONE_DEPTH } from '../constants.js';
import { extractJsonObject, extractJsonArray } from '../json-extract.js';
import {
  PLAYBOOK_GUIDANCE, FIRST_TASK_GUIDANCE, DEFAULT_TOOL_GUIDANCE, readClaudeMd, buildContextPrefix, buildFileTree, configureAgentPermissions,
  writePlaybookHelper, writeMessagesHelper, formatPlaybook, excludeAgentHelpers, buildRetryContext, type PlaybookEntry,
} from '../prompt.js';

// ── Pure data transformation helpers (exported for testing) ─────────────────

export function calcMessageAge(m: Record<string, unknown>): string {
  if (!m.created_at) return '?';
  const ms = Date.now() - new Date(String(m.created_at)).getTime();
  if (isNaN(ms)) return '?';
  const h = Math.floor(ms / 3600000);
  return h < 24 ? `${h}h` : `${Math.floor(h / 24)}d`;
}

export function parseResearchContext(rawCtx: string): { established: string[]; failed: string[]; open: string[]; eviction: string } {
  const sections: { established: string[]; failed: string[]; open: string[]; eviction: string } = { established: [], failed: [], open: [], eviction: '' };
  let currentSection: 'established' | 'failed' | 'open' | 'eviction' | '' = '';
  const evictionLines: string[] = [];
  for (const line of rawCtx.split('\n')) {
    const trimmed = line.trim();
    if (trimmed.startsWith('## Established')) { currentSection = 'established'; continue; }
    if (trimmed.startsWith('## Failed')) { currentSection = 'failed'; continue; }
    if (trimmed.startsWith('## Open')) { currentSection = 'open'; continue; }
    if (trimmed.startsWith('## Eviction Summary')) { currentSection = 'eviction'; continue; }
    if (currentSection === 'eviction' && trimmed.length > 0) {
      evictionLines.push(trimmed);
    } else if (trimmed.startsWith('- ') && currentSection && currentSection !== 'eviction') {
      sections[currentSection].push(trimmed);
    }
  }
  sections.eviction = evictionLines.join(' ');
  return sections;
}

export function aggregateDirectionStats(
  history: Array<Record<string, unknown>>,
  aggregate: Record<string, number | string | null>
): string {
  const totalCycles = history.length + ((aggregate.total_cycles as number) || 0);
  if (totalCycles === 0) return '';
  const totalTasks = history.reduce((s, h) => s + ((h.tasks_created as number) || 0), 0) + ((aggregate.total_tasks_created as number) || 0);
  const totalMerged = history.reduce((s, h) => s + ((h.prs_merged as number) || 0), 0) + ((aggregate.total_prs_merged as number) || 0);
  const totalFailed = history.reduce((s, h) => s + ((h.tasks_failed as number) || 0), 0) + ((aggregate.total_tasks_failed as number) || 0);
  const parts = [`${totalCycles} cycles`, `${totalTasks} tasks`];
  if (totalMerged > 0) parts.push(`${totalMerged} merged`);
  if (totalFailed > 0) parts.push(`${totalFailed} failed`);

  // Conversion rate from last 3 cycles: findings that became merged PRs
  const recent = history.slice(-3);
  const recentTasksCreated = recent.reduce((s, h) => s + ((h.tasks_created as number) || 0), 0);
  if (recentTasksCreated > 0) {
    const recentMerged = recent.reduce((s, h) => s + ((h.prs_merged as number) || 0), 0);
    const pct = Math.round((recentMerged / recentTasksCreated) * 100);
    const cycleLabel = recent.length === 1 ? '1 cycle' : `${recent.length} cycles`;
    parts.push(`conversion: ${pct}% (${recentMerged}/${recentTasksCreated} findings merged last ${cycleLabel})`);
  }

  return `Stats: ${parts.join(', ')}`;
}

/** Maps raw agent-reported failure_category to a high-level failure class. */
export function classifyFailureCategory(rawCat: string): string {
  const CODE_ERROR_CATS: Record<string, true> = {
    build_failed: true, test_failed: true, lint_failed: true,
    claude_code_failed: true, parse_failed: true, fatal: true,
  };
  if (CODE_ERROR_CATS[rawCat]) return 'CODE_ERROR';
  if (rawCat === 'clone_failed') return 'PROVISION_FAILURE';
  if (rawCat === 'rate_limited') return 'RATE_LIMIT';
  if (rawCat === 'pr_creation_failed') return 'EXTERNAL_API';
  // Pass through if already a high-level class name or unknown
  return rawCat || 'unknown';
}

const _INFRA_CLASSES = new Set(['PROVISION_FAILURE', 'RATE_LIMIT', 'AGENT_TIMEOUT', 'EXTERNAL_API']);

// Reads a progress artifact written by the agent (research/plan handlers).
// Returns content prefixed with "## Pre-Implementation Analysis" so retry
// context rendering (buildRetryContext in prompt.ts) treats it like the
// standard handler's analysis_plan. Returns undefined if file is missing/empty.
export function readProgressArtifact(taskId: string, kind: 'plan' | 'research'): string | undefined {
  const path = `/tmp/${kind}_progress_${taskId}.md`;
  try {
    if (fs.existsSync(path)) {
      const content = fs.readFileSync(path, 'utf-8').trim();
      if (content) return `## Pre-Implementation Analysis\n\n${content}`;
    }
  } catch { /* ignore */ }
  return undefined;
}

/**
 * Builds a compact (0-2 line) failure-calibration signal from direction history.
 * Line 1: last cycle's failures with class and actionability hint.
 * Line 2 (optional): recurring CODE_ERROR trend → urge smaller scope.
 */
export function buildScopeCalibrationSignal(history: Array<Record<string, unknown>>): string {
  if (history.length === 0) return '';
  const lastEntry = history[history.length - 1];
  const failedDetails = Array.isArray(lastEntry.failed_task_details)
    ? lastEntry.failed_task_details as Array<{title: string; reason: string; category: string}>
    : [];

  const classified = failedDetails.map(fd => ({ ...fd, cls: classifyFailureCategory(fd.category) }));
  const codeErrors = classified.filter(fd => fd.cls === 'CODE_ERROR');
  const infraErrors = classified.filter(fd => _INFRA_CLASSES.has(fd.cls));

  const parts: string[] = [];
  if (codeErrors.length > 0) {
    const names = codeErrors.slice(0, 2).map(fd => `"${fd.title.slice(0, 40)}"`).join(', ');
    parts.push(`${codeErrors.length} CODE_ERROR (${names}) → over-scoped/under-specified`);
  }
  if (infraErrors.length > 0) {
    parts.push(`${infraErrors.length} infra failure(s) → retry ok`);
  }
  const other = classified.length - codeErrors.length - infraErrors.length;
  if (other > 0) {
    parts.push(`${other} other`);
  }

  const line1 = parts.length > 0 ? `\n  Last cycle failures: ${parts.join('; ')}` : '';

  // Scope trend: CODE_ERRORs recurring in prior cycles too → strongly urge smaller scope
  let line2 = '';
  if (codeErrors.length > 0 && history.length >= 2) {
    const prevEntries = history.slice(-3, -1);
    let prevCodeErrors = 0;
    for (const h of prevEntries) {
      const prevDetails = Array.isArray(h.failed_task_details)
        ? h.failed_task_details as Array<{category: string}>
        : [];
      prevCodeErrors += prevDetails.filter(fd => classifyFailureCategory(fd.category) === 'CODE_ERROR').length;
    }
    if (prevCodeErrors > 0) {
      line2 = `\n  Scope trend: CODE_ERRORs in ${Math.min(history.length, 3)} consecutive cycles → strongly prefer smaller, narrower findings`;
    }
  }

  // Scope success from last 3 cycles: aggregate scope_breakdown to show merged/created per scope
  const recent = history.slice(-3);
  const aggScope: Record<string, {created: number; merged: number}> = {};
  for (const h of recent) {
    const sb = h.scope_breakdown as Record<string, {created: number; merged: number; failed: number}> | undefined;
    if (!sb || typeof sb !== 'object') continue;
    for (const [scope, counts] of Object.entries(sb)) {
      if (!aggScope[scope]) aggScope[scope] = { created: 0, merged: 0 };
      aggScope[scope].created += (counts.created as number) || 0;
      aggScope[scope].merged += (counts.merged as number) || 0;
    }
  }
  const scopeOrder = ['small', 'medium', 'large'];
  const scopeParts: string[] = [];
  for (const scope of scopeOrder) {
    if (aggScope[scope] && aggScope[scope].created > 0) {
      scopeParts.push(`${scope} ${aggScope[scope].merged}/${aggScope[scope].created}`);
    }
  }
  // Include any non-standard scopes
  for (const scope of Object.keys(aggScope)) {
    if (!scopeOrder.includes(scope) && aggScope[scope].created > 0) {
      scopeParts.push(`${scope} ${aggScope[scope].merged}/${aggScope[scope].created}`);
    }
  }
  const cycleLabel = recent.length === 1 ? '1 cycle' : `${recent.length} cycles`;
  const line3 = scopeParts.length > 0
    ? `\n  Scope success (last ${cycleLabel}): ${scopeParts.join(', ')}`
    : '';

  if (!line1 && !line2 && !line3) return '';
  return line1 + line2 + line3;
}

export function formatCochangeHints(directionCtx: Record<string, unknown> | undefined): string {
  if (!directionCtx) return '';
  const rawHints = directionCtx.cochange_hints;
  if (!rawHints || typeof rawHints !== 'object') return '';
  const seen = new Set<string>();
  const pairs: Array<[string, string, number]> = [];
  for (const [fileA, affinities] of Object.entries(rawHints as Record<string, unknown>)) {
    if (!affinities || typeof affinities !== 'object') continue;
    for (const [fileB, scoreRaw] of Object.entries(affinities as Record<string, unknown>)) {
      const score = typeof scoreRaw === 'number' ? scoreRaw : Number(scoreRaw);
      if (!Number.isFinite(score)) continue;
      const key = [fileA, fileB].sort().join('|');
      if (seen.has(key)) continue;
      seen.add(key);
      pairs.push([fileA, fileB, score]);
    }
  }
  if (pairs.length === 0) return '';
  pairs.sort((a, b) => b[2] - a[2]);
  const lines = pairs.map(([a, b, s]) => `- ${a} \u2194 ${b} (affinity: ${s})`);
  return `## Co-Change Affinity Hints\n\nBased on prior merged PRs, these files in your task's scope have historically changed together. Prefer to keep high-affinity pairs in the same subtask; files without listed affinity can safely be split into independent subtasks.\n\n${lines.join('\n')}\n\n`;
}

export function formatDecompositionOutcomes(directionCtx: Record<string, unknown> | undefined): string {
  if (!directionCtx) return '';
  const raw = directionCtx.decomposition_outcomes;
  if (!raw || typeof raw !== 'object') return '';
  const o = raw as Record<string, unknown>;
  const count = typeof o.prior_decomposition_count === 'number' ? o.prior_decomposition_count : 0;
  if (count <= 0) return '';

  const lines: string[] = [`- Prior decompositions analyzed: ${count}`];
  if (typeof o.avg_subtask_merge_rate === 'number') {
    lines.push(`- Avg subtask merge rate: ${Math.round(o.avg_subtask_merge_rate * 100)}%`);
  }
  if (typeof o.avg_files_per_subtask === 'number') {
    lines.push(`- Avg files per subtask: ${o.avg_files_per_subtask}`);
  }
  if (typeof o.max_files_in_subtask === 'number') {
    lines.push(`- Max files in a single subtask: ${o.max_files_in_subtask}`);
  }
  if (typeof o.avg_dependency_depth === 'number') {
    lines.push(`- Avg dependency depth: ${o.avg_dependency_depth}`);
  }
  if (typeof o.max_dependency_depth === 'number') {
    lines.push(`- Max dependency depth: ${o.max_dependency_depth}`);
  }

  const hints: string[] = [];
  const avgFiles = typeof o.avg_files_per_subtask === 'number' ? o.avg_files_per_subtask : 0;
  const maxDep = typeof o.max_dependency_depth === 'number' ? o.max_dependency_depth : 0;
  const mergeRate = typeof o.avg_subtask_merge_rate === 'number' ? o.avg_subtask_merge_rate : null;
  const xmodCorr = o.high_cross_module_correlates_with_low_merge === true;
  if (avgFiles > 5 && (mergeRate === null || mergeRate < 0.6)) {
    hints.push('Prior subtasks averaged many files and had middling merge rates — prefer smaller subtasks.');
  }
  if (maxDep > 3) {
    hints.push('Prior decompositions had deep dependency chains — flatten the dependency graph.');
  }
  if (xmodCorr) {
    hints.push('Cross-module subtasks (>=2 top-level dirs) correlate with lower merge rates — keep each subtask scoped to one module where possible.');
  }

  const hintSection = hints.length > 0
    ? `\n\nActionable guidance:\n${hints.map(h => `- ${h}`).join('\n')}`
    : '';

  return `## Prior Decomposition Outcomes\n\nMetrics from recent plan decompositions in this direction:\n${lines.join('\n')}${hintSection}\n\n`;
}

export function mapSubtaskToSpawnPayload(st: Record<string, unknown>, baseBranch: string): Record<string, unknown> {
  return {
    title: strVal(st.title, 'Untitled subtask'),
    description: strVal(st.prompt),
    prompt: strVal(st.prompt),
    type: strVal(st.type, 'create'),
    scope: strVal(st.scope, 'small'),
    risk: strVal(st.risk, 'low'),
    base_branch: baseBranch,
    depends_on_indices: st.depends_on_indices ?? [],
    related_files: st.related_files ?? [],
    ...(st.model ? { model: strVal(st.model) } : {}),
    ...(st.effort ? { effort: strVal(st.effort) } : {}),
    ...(st.approval_reason ? { approval_reason: strVal(st.approval_reason) } : {}),
  };
}

// ── Handler: Plan (decompose complex task into subtasks) ───────────────────
export async function handlePlan(
  taskId: string, payload: Record<string, unknown>, task: Record<string, unknown>,
  ghToken: string, owner: string, repo: string, ws: WebSocket | null, statusRef: { value: string },
  model?: string, effort?: string
) {
  const prompt = strVal(payload.prompt) || strVal(payload.description) || strVal(task.description);
  const baseBranch = strVal(payload.base_branch, 'main');
  const planPromptSuffix = payload.prompt_suffix ? `\n\nAdditional instructions:\n${strVal(payload.prompt_suffix)}` : '';

  if (!prompt) throw new Error('Plan task missing prompt');

  // Clone repo — planner needs codebase context for good decomposition
  statusRef.value = `Cloning ${owner}/${repo} for planning...`;
  sendLog(ws, `Planning task decomposition for: ${prompt.slice(0, 200)}\n`);
  execFileSync('rm', ['-rf', WORKSPACE], { encoding: 'utf-8' });
  const cloneUrl = `https://x-access-token:${ghToken}@github.com/${owner}/${repo}.git`;
  await cloneWithRetry(['clone', '--depth', String(DEFAULT_CLONE_DEPTH), '--branch', baseBranch, cloneUrl, WORKSPACE]);
  writePlaybookHelper(taskId);
  writeMessagesHelper(taskId);
  const disallowedTools = configureAgentPermissions('plan');

  const contextPrefix = buildContextPrefix(task);

  const claudeMd = readClaudeMd();
  const claudeMdPrefix = claudeMd ? `## Codebase Instructions\n${claudeMd}\n\n` : '';
  const planPlaybookSection = formatPlaybook(task.playbook as Record<string, PlaybookEntry | string> | undefined);
  const planRetryContext = buildRetryContext(task);
  const planFirstTaskSection = task.first_task_on_repo ? FIRST_TASK_GUIDANCE : '';
  const cochangeHintsSection = formatCochangeHints(task.direction_context as Record<string, unknown> | undefined);
  const decompositionOutcomesSection = formatDecompositionOutcomes(task.direction_context as Record<string, unknown> | undefined);

  const planPrompt = `${claudeMdPrefix}${planPlaybookSection}${planRetryContext}${contextPrefix}${cochangeHintsSection}${decompositionOutcomesSection}You are a planning agent. Your job is to analyze this codebase and decompose the following task into concrete, ordered subtasks that can each be implemented as a single pull request.

Task to decompose:
${prompt}

Instructions:
1. Explore the codebase thoroughly. Read key files, understand the architecture, find relevant patterns.
2. Decompose into substantial subtasks. Prefer fewer, larger subtasks over many tiny ones. A single subtask that modifies 200+ lines across 5+ files is fine — the implementing agent is capable. Only split when subtasks are genuinely independent or have different risk profiles.
3. Identify dependencies between subtasks — which must complete before others can start.
4. Write clear, self-contained prompts for each subtask. Describe the PROBLEM and desired outcome — do not prescribe exact line numbers. The implementing agent has full repo access and will figure out the details.

Output EXACTLY one JSON block as the VERY LAST thing in your response:

{
  "subtasks": [
    {
      "title": "Short title (under 60 chars, conventional commit style: feat:, fix:, refactor:, etc.)",
      "prompt": "Self-contained implementation prompt. Describe WHAT to achieve and key constraints. The agent has full repo access and will figure out implementation details.",
      "depends_on_indices": [],
      "related_files": ["src/main.py", "src/utils.py"],
      "model": "claude-opus-4-7 | null (inherit from parent)",
      "effort": "low | medium | high | null (inherit from parent)",
      "scope": "medium",
      "risk": "low",
      "type": "create",
      "approval_reason": "Only set when risk=high — brief reason why human approval is needed (omit otherwise)"
    },
    {
      "title": "feat: second subtask",
      "prompt": "...",
      "depends_on_indices": [0],
      "related_files": ["src/main.py"],
      "model": null,
      "effort": null,
      "scope": "large",
      "risk": "medium",
      "type": "create"
    }
  ]
}

Risk guidance:
- risk=high: changes to auth flows, JWT/token handling, database models (models.py), Alembic migrations, core orchestration (main.py worker loops), VM provisioning (compute.py), any change that if wrong could cause data loss or security breach
- risk=medium: new API endpoints, changes to existing API request/response shapes, new agent handler types, frontend state management, WebSocket protocol changes
- risk=low: UI-only changes, new utility functions, log/error message changes, documentation, tests that don't touch production code
When risk=high for a subtask, the task will require human approval before executing. Set this judiciously — only when a mistake would be hard to roll back. When risk=high, also set "approval_reason" to a brief explanation of why this needs approval.

Rules:
- Every subtask must describe a concrete change to implement — code, config, migrations, tests. Investigation and research are how YOU prepare the plan; they are not subtasks. If you need to understand the codebase, do that now before writing the plan.
- depends_on_indices uses 0-based array positions (e.g., [0, 1] means this subtask depends on the first and second subtasks)
- related_files: list the key files each subtask will create or modify
- Each subtask prompt must be self-contained — don't reference other subtasks by index
- Keep prompts concise — describe what to achieve, not step-by-step how
- Order subtasks so that dependencies flow forward (a subtask can only depend on earlier subtasks)
- If subtasks involve structural refactors of the same files (renames, interface changes, restructuring), set depends_on_indices between them. Small additive changes to shared files can stay parallel.
- Aim for 1-4 subtasks. Prefer 1-2 large subtasks over 4-6 small ones. If the task can be one PR, output a single subtask.
- Do NOT include review, testing, or deployment subtasks — those are handled automatically by the pipeline
- Use model=claude-opus-4-7 and effort=high only for complex architectural subtasks. Leave null for simple additive changes.
- scope: "small" for changes under ~100 lines across 1-3 files with clear, narrow scope, "medium" for changes spanning 3-8 files or 100-300 lines, or touching shared infrastructure, "large" for cross-component architectural changes, 300+ lines, or changes to auth/data models/core orchestration
- The "type" field controls how the subtask is executed: "create" (default) for new features/additions, "refactor" for code quality improvements with no behavior change, "migrate" for database schema changes (Alembic migrations), "qa" for test-only PRs.
- If any subtask modifies SQLAlchemy/SQLModel models (backend/app/models.py) or adds new database tables/columns, include a separate subtask with set type="migrate" (e.g., title "feat: add index for user lookup", type "migrate"). This subtask should depend on the model change subtask. The migration agent will handle Alembic generation.
- If any subtask adds new user-facing functionality, consider whether a test subtask is needed. New API endpoints should have corresponding integration tests. New agent handlers should have at least one smoke test. Add a test subtask with depends_on_indices pointing to the implementation subtask.
- If any subtask changes a public API interface (endpoint signatures, request/response shapes, TypeScript types exported to frontend), include explicit notes in the prompt about backward compatibility requirements.
- IMPORTANT: Write your plan JSON to /tmp/plan_output_${taskId}.json (NOT in the repository). The file should contain ONLY the JSON object, no markdown wrapping.
- ALSO write /tmp/plan_progress_${taskId}.md — a concise markdown summary (under ~3000 chars) of your decomposition: which areas of the codebase you explored, the rationale for how you split the work, the file targets and key constraints per subtask, and any risks or open questions. This artifact is surfaced to retry agents if this task fails, so they can pick up without re-investigating.${planPromptSuffix}${planFirstTaskSection}${PLAYBOOK_GUIDANCE}

${DEFAULT_TOOL_GUIDANCE}`;

  statusRef.value = 'Running Claude Code for planning...';
  const { exitCode, allOutput, lastResultText } = await runClaudeCode(planPrompt, ws, statusRef, model, effort, disallowedTools);

  // Prefer reading plan from /tmp/ file (avoids output truncation), fall back to output parsing
  let logs = '';

  const planPath = `/tmp/plan_output_${taskId}.json`;
  try {
    if (fs.existsSync(planPath)) {
      logs = sanitize(fs.readFileSync(planPath, 'utf-8'));
      sendLog(ws, `Read plan output from ${planPath}\n`);
    }
  } catch {
    sendLog(ws, `Failed to read ${planPath}, falling back to output parsing\n`);
  }
  if (!logs) {
    if (lastResultText && lastResultText.includes('"subtasks"')) {
      logs = sanitize(lastResultText);
    } else {
      const planObj = extractJsonObject(allOutput, 'subtasks');
      logs = sanitize(planObj ? JSON.stringify(planObj) : (lastResultText || allOutput.slice(-50_000)));
    }
  }

  // Try to parse plan JSON and spawn subtasks directly from the agent
  let spawnFailed = false;
  if (exitCode === 0 && logs) {
    try {
      let planData: Record<string, unknown> | null = null;
      try {
        planData = JSON.parse(logs) as Record<string, unknown>;
      } catch {
        planData = extractJsonObject(logs, 'subtasks');
      }

      const subtasks = planData?.subtasks;
      if (Array.isArray(subtasks) && subtasks.length > 0 && subtasks.length <= 10) {
        sendLog(ws, `Parsed ${subtasks.length} subtasks from plan output\n`);

        const spawnPayloads = (subtasks as Record<string, unknown>[]).map((st) => mapSubtaskToSpawnPayload(st, baseBranch));

        statusRef.value = 'Spawning subtasks...';
        const spawnResult = await apiPost('/api/agent/spawn', {
          task_id: taskId,
          payloads: spawnPayloads,
          requires_approval: false,
          context: { agent_spawned: true },
        }) as Record<string, unknown>;

        const spawnedIds = Array.isArray(spawnResult.task_ids) ? spawnResult.task_ids as string[] : [];
        if (spawnedIds.length === 0) {
          spawnFailed = true;
          sendLog(ws, `Spawn returned no task IDs — reporting FAILED\n`);
        } else {
          sendLog(ws, `Spawned ${spawnedIds.length} subtasks: ${spawnedIds.join(', ')}\n`);
          // Include full plan data with spawn info for backend enrichment
          logs = JSON.stringify({
            ...planData,
            agent_spawned: true,
            spawned_task_ids: spawnedIds,
          });
        }
      }
    } catch (err) {
      spawnFailed = true;
      sendLog(ws, `Agent-side spawn failed: ${err instanceof Error ? err.message : String(err)}\n`);
    }
  }

  const finalStatus = spawnFailed ? 'FAILED' : (exitCode === 0 ? 'COMPLETED' : 'FAILED');
  const planProgress = readProgressArtifact(taskId, 'plan');
  try {
    await persistAndPostStatus(taskId, {
      task_id: taskId,
      status: finalStatus,
      ...(finalStatus === 'FAILED' ? { artifact_type: 'ERROR_LOG' } : {}),
      logs: spawnFailed ? `Spawn failed — subtasks not created. Plan output: ${logs}` : logs,
      ...(finalStatus === 'FAILED' ? { failure_category: 'claude_code_failed' } : {}),
      ...(planProgress ? { analysis_plan: planProgress.slice(0, 5000) } : {}),
    });
  } catch (e) {
    sendLog(ws, `[ERROR] Failed to post plan completion status: ${e}\n`);
  }
}

// ── Handler: Research (analyze codebase, output improvement findings) ──────
export async function handleResearch(
  taskId: string, payload: Record<string, unknown>, task: Record<string, unknown>,
  ghToken: string, owner: string, repo: string, ws: WebSocket | null, statusRef: { value: string },
  model?: string, effort?: string
) {
  const directions = Array.isArray(payload.directions) ? payload.directions as Array<Record<string, unknown>> : [];
  const currentDirectionId = payload.current_direction_id as string | undefined;
  const baseBranch = strVal(payload.base_branch, 'main');
  const userPrompt = strVal(payload.prompt) || strVal(payload.description) || strVal(task.description);

  // Full clone — research needs history context
  statusRef.value = `Cloning ${owner}/${repo} for research...`;
  sendLog(ws, `Starting codebase research...\n`);
  execFileSync('rm', ['-rf', WORKSPACE], { encoding: 'utf-8' });
  const cloneUrl = `https://x-access-token:${ghToken}@github.com/${owner}/${repo}.git`;
  await cloneWithRetry(['clone', '--branch', baseBranch, cloneUrl, WORKSPACE]);
  writePlaybookHelper(taskId);
  writeMessagesHelper(taskId);
  const disallowedTools = configureAgentPermissions(strVal(payload.research_mode as string, 'research'));

  let gitLogOutput = '';
  let gitDiffStatOutput = '';
  try {
    gitLogOutput = execFileSync('git', ['-C', WORKSPACE, 'log', '--oneline', '-30'], { encoding: 'utf-8' }).trim();
  } catch (e) {
    sendLog(ws, `Warning: git log failed: ${String(e)}\n`);
  }
  let gitShortlogOutput = '';
  try {
    gitShortlogOutput = execFileSync('git', ['-C', WORKSPACE, 'shortlog', '-sn', '--no-merges', '-20'], { encoding: 'utf-8' }).trim();
  } catch (e) {
    sendLog(ws, `Warning: git shortlog failed: ${String(e)}\n`);
  }
  try {
    gitDiffStatOutput = execFileSync('git', ['-C', WORKSPACE, 'diff', '--stat', 'HEAD~5..HEAD'], { encoding: 'utf-8' }).trim();
  } catch (e) {
    sendLog(ws, `Warning: git diff --stat failed: ${String(e)}\n`);
  }
  let hotspotsSection = '';
  try {
    const hotspots = execFileSync('git', ['log', '--pretty=format:', '--name-only', '-50'], {cwd: WORKSPACE, encoding: 'utf-8'})
      .split('\n').filter(Boolean);
    const fileCounts = hotspots.reduce<Record<string, number>>((acc, f) => ({...acc, [f]: (acc[f] ?? 0)+1}), {});
    const topFiles = Object.entries(fileCounts).sort((a,b) => b[1]-a[1]).slice(0, 10);
    hotspotsSection = `### Most Changed Files (last 50 commits)\n${topFiles.map(([f,c]) => `- ${f} (${c} changes)`).join('\n')}`;
  } catch (e) {
    sendLog(ws, `Warning: git hotspots failed: ${String(e)}\n`);
  }
  const gitHistorySection = (gitLogOutput || gitDiffStatOutput) ? `## Recent Git History (last 30 commits)
${gitLogOutput || '(unavailable)'}

## Top Contributors (last 20, no merges)
${gitShortlogOutput || '(unavailable)'}

## Recent Changes Summary (last 5 commits diff stat)
${gitDiffStatOutput || '(unavailable)'}

${hotspotsSection}

` : '';

  // Build codebase summary to reduce exploration overhead

  const claudeMd = readClaudeMd();
  const codebaseSummary = claudeMd
    ? `## Codebase Instructions\n${claudeMd}\n\n`
    : '';
  const researchPlaybookSection = formatPlaybook(task.playbook as Record<string, PlaybookEntry | string> | undefined);

  const fileTreeSection = buildFileTree(ws);

  const projectCtx = task.project_context as Record<string, unknown> | undefined;
  const rawActiveTasks = projectCtx?.active_tasks;
  const activeTasks: Array<Record<string, unknown>> = Array.isArray(rawActiveTasks) ? rawActiveTasks as Array<Record<string, unknown>> : [];
  const rawQueuedTasks = projectCtx?.queued_tasks;
  const queuedTasks: Array<Record<string, unknown>> = Array.isArray(rawQueuedTasks) ? rawQueuedTasks as Array<Record<string, unknown>> : [];

  const directionsText = directions.length > 0
    ? directions.map((d, i) => {
        const pathScope = Array.isArray(d.paths) && (d.paths as string[]).length > 0
          ? ` [scope: ${(d.paths as string[]).join(', ')}]`
          : '';
        const history = Array.isArray(d.research_history) ? d.research_history as Array<Record<string, unknown>> : [];
        const agg = (d.history_aggregate || {}) as Record<string, number | string | null>;
        const statsResult = aggregateDirectionStats(history, agg);
        const statsLine = statsResult ? `\n  ${statsResult}` : '';
        const calibrationSignal = buildScopeCalibrationSignal(history);

        // Research context — restructure to avoid anchoring on small past findings.
        // Show failed approaches and open questions in full (high-signal steering).
        // Summarize established findings as a count to inform without anchoring scope.
        let researchCtx = '';
        if (typeof d.research_context === 'string' && (d.research_context as string).length > 0) {
          const sections = parseResearchContext(d.research_context as string);
          const ctxParts: string[] = [];
          if (sections.established.length > 0) {
            ctxParts.push(`  ${sections.established.length} findings already completed for this direction (do not re-propose similar scope work).`);
          }
          if (sections.failed.length > 0) {
            ctxParts.push(`  Failed approaches (avoid these):\n${sections.failed.join('\n')}`);
          }
          if (sections.open.length > 0) {
            ctxParts.push(`  Open questions:\n${sections.open.join('\n')}`);
          }
          if (sections.eviction) {
            ctxParts.push(`  \u26a0 Context trimmed: ${sections.eviction}. Earlier findings were evicted — check git history if you suspect overlap with prior work.`);
          }
          if (ctxParts.length > 0) {
            researchCtx = `\n  Context:\n${ctxParts.join('\n')}`;
          }
        }

        // History titles: purely for dedup — framed as "already done", not as examples
        let historySection = '';
        if (history.length > 0) {
          const historySlice = history.slice(-5);
          const allTitles = historySlice.flatMap(h =>
            Array.isArray(h.finding_titles) ? h.finding_titles as string[] : []
          );
          if (allTitles.length > 0) {
            historySection = `\n  Already completed (dedup only — do NOT use as scope examples): ${allTitles.join(', ')}`;
          }
        }

        // Notes: show all notes in full (user-written guidance, highest signal)
        const notes = Array.isArray(d.notes) ? d.notes as Array<{text: string; added_at: string}> : [];
        let notesSection = '';
        if (notes.length > 0) {
          const rendered = notes.map(n => `  * ${n.text}`).join('\n');
          notesSection = `\n  Notes:\n${rendered}`;
        }

        const saturationNote = d.possibly_saturated
          ? '\n  \u26a0 SATURATED: Consider a direction_update or direction_note instead of more findings.'
          : '';

        // Active tasks for this direction (avoid duplicating)
        let activeForDirNote = '';
        if (d.id === currentDirectionId) {
          const dirTasks = [...activeTasks, ...queuedTasks].filter(t => t['direction_id'] === d.id);
          if (dirTasks.length > 0) {
            activeForDirNote = `\n  In-flight: ${dirTasks.map(t => strVal(t['title'])).join(', ')}`;
          }
        }

        return `${i + 1}. [${strVal(d.priority, 'medium')}] ${strVal(d.text)}${pathScope} (id: ${strVal(d.id)})${statsLine}${calibrationSignal}${researchCtx}${historySection}${notesSection}${saturationNote}${activeForDirNote}`;
      }).join('\n\n')
    : 'No specific directions provided. Identify general improvements.';

  // Build cross-direction context from OTHER active directions
  const otherDirections = currentDirectionId
    ? directions.filter(d => d.id !== currentDirectionId && d.status === 'active')
    : [];
  let crossDirSection = '';
  if (otherDirections.length > 0) {
    const snippets = otherDirections
      .filter(d => Array.isArray(d.research_history) && (d.research_history as unknown[]).length > 0)
      .map(d => {
        const history = d.research_history as Array<Record<string, unknown>>;
        const lastCycle = history[history.length - 1];
        const titles = (Array.isArray(lastCycle.finding_titles) ? lastCycle.finding_titles as string[] : []).slice(0, 3).join(', ');
        return `- "${strVal(d.text).slice(0, 80)}": ${String(lastCycle.tasks_created)} tasks (${titles || 'none'})`;
      })
      .slice(0, 3);  // max 3 other directions
    if (snippets.length > 0) {
      crossDirSection = `\n\nOther active directions (recent activity — avoid duplicating):\n${snippets.join('\n')}`;
    }
  }

  // Pipeline-wide scope merge rates — rendered from pipeline_health.scope_merge_rates
  // so measurement directions can read the number straight from the prompt instead
  // of hitting an admin endpoint they aren't authenticated for.
  let pipelineScopeRatesSection = '';
  const pipelineHealthForScope = projectCtx?.pipeline_health as Record<string, unknown> | null | undefined;
  const scopeMergeRates = pipelineHealthForScope?.scope_merge_rates as
    | Record<string, { current_rate: number | null; prev_rate: number | null; delta: number | null; trend: string | null }>
    | null
    | undefined;
  if (scopeMergeRates && Object.keys(scopeMergeRates).length > 0) {
    const fmtRate = (r: number | null | undefined) => (typeof r === 'number' ? `${Math.round(r * 100)}%` : '—');
    const scopeOrder = ['small', 'medium', 'large'];
    const orderedKeys = [
      ...scopeOrder.filter(k => k in scopeMergeRates),
      ...Object.keys(scopeMergeRates).filter(k => !scopeOrder.includes(k)),
    ];
    const parts = orderedKeys.map(k => {
      const r = scopeMergeRates[k];
      const trend = r?.trend ? ` (${r.trend})` : '';
      return `${k} ${fmtRate(r?.current_rate)}/${fmtRate(r?.prev_rate)}${trend}`;
    });
    pipelineScopeRatesSection = `\nPipeline-wide scope merge rates (current / prev 24h window): ${parts.join(', ')}\n`;
  }

  // Medium-scope pre/post investment measurement — rendered so measurement directions
  // can read the headline before/after numbers straight from the prompt.
  const mediumScopeMeasurement = pipelineHealthForScope?.medium_scope_measurement as
    | {
        medium_scope_merge_rate: number | null;
        total_created: number | null;
        total_merged: number | null;
        pre_investment: { merge_rate: number | null; created: number | null; merged: number | null } | null;
        post_investment: { merge_rate: number | null; created: number | null; merged: number | null } | null;
        recent_30d?: { merge_rate: number | null; created: number | null; merged: number | null } | null;
        recent_7d?: { merge_rate: number | null; created: number | null; merged: number | null } | null;
      }
    | null
    | undefined;
  if (mediumScopeMeasurement) {
    const fmtPct = (r: number | null | undefined) => (typeof r === 'number' ? `${Math.round(r * 100)}%` : '—');
    const pre = mediumScopeMeasurement.pre_investment;
    const post = mediumScopeMeasurement.post_investment;
    const recent30 = mediumScopeMeasurement.recent_30d;
    const recent7 = mediumScopeMeasurement.recent_7d;
    if (pre || post) {
      const preStr = pre ? `pre-investment ${fmtPct(pre.merge_rate)} (${pre.created ?? 0} tasks)` : 'pre-investment —';
      const postStr = post ? `post-investment ${fmtPct(post.merge_rate)} (${post.created ?? 0} tasks)` : 'post-investment —';
      const headline = `${fmtPct(mediumScopeMeasurement.medium_scope_merge_rate)} overall (${mediumScopeMeasurement.total_merged ?? 0}/${mediumScopeMeasurement.total_created ?? 0})`;
      const recentParts: string[] = [];
      if (recent30) recentParts.push(`recent-30d ${fmtPct(recent30.merge_rate)} (${recent30.created ?? 0} tasks)`);
      if (recent7) recentParts.push(`recent-7d ${fmtPct(recent7.merge_rate)} (${recent7.created ?? 0} tasks)`);
      const recentStr = recentParts.length ? `; ${recentParts.join(', ')}` : '';
      pipelineScopeRatesSection += `Medium-scope before/after: ${preStr}, ${postStr}${recentStr}; ${headline}\n`;
    }
  }

  // Build anomaly signals section for undirected research cycles
  let anomalySection = '';
  if (!currentDirectionId) {
    const rawAnomalies = payload.anomaly_signals;
    if (Array.isArray(rawAnomalies) && rawAnomalies.length > 0) {
      const anomalyLines = (rawAnomalies as Array<{signal: string; severity: string; details: string}>)
        .map(a => `- [${strVal(a.severity)}] ${strVal(a.signal)}. ${strVal(a.details)}`)
        .join('\n');
      anomalySection = `\n## Suggested Investigation Areas\nBased on pipeline performance analysis:\n${anomalyLines}\n`;
    }
  }

  const contextPrefix = buildContextPrefix(task);

  // ── Strategic mode: product-level vision review ────────────────────────
  if (payload.research_mode === 'strategic') {
    const vision = payload.vision as Record<string, unknown> | undefined;
    const directionsSummary = Array.isArray(payload.directions_summary)
      ? payload.directions_summary as Array<Record<string, unknown>>
      : [];
    const projectName = strVal(payload.project_name, 'Unknown Project');

    const openMessages = Array.isArray(payload.open_messages)
      ? payload.open_messages as Array<Record<string, unknown>>
      : [];

    const visionJson = vision && Object.keys(vision).length > 0
      ? JSON.stringify(vision, null, 2)
      : '';

    // Build outcome evaluation section
    const outcomeEval = payload.outcome_evaluation as Record<string, unknown> | undefined;
    let outcomeSection = '';
    if (outcomeEval && typeof outcomeEval === 'object') {
      const window = outcomeEval.window as Record<string, unknown> | undefined;
      const overall = outcomeEval.overall as Record<string, unknown> | undefined;
      const systems = Array.isArray(outcomeEval.systems) ? outcomeEval.systems as Array<Record<string, unknown>> : [];
      const dirOutcomes = Array.isArray(outcomeEval.directions) ? outcomeEval.directions as Array<Record<string, unknown>> : [];

      const lines: string[] = [];
      if (window) {
        lines.push(`# Outcome evaluation (since vision rev ${window.vision_revision || 0})`);
        lines.push(`Window: ${strVal(window.from as string, '?')} to ${strVal(window.to as string, '?')}`);
      }
      if (overall) {
        const sr = typeof overall.success_rate === 'number' ? Math.round(overall.success_rate * 100) : '?';
        const productRatioStr = typeof overall.product_ratio === 'number' ? `, product ratio: ${Math.round((overall.product_ratio as number) * 100)}%` : '';
        lines.push(`Overall: ${overall.cycles || 0} cycles, ${overall.merged || 0} merged, ${overall.failed || 0} failed (${sr}% success rate, trend: ${strVal(overall.trend as string, 'stable')}${productRatioStr})`);
      }

      if (systems.length > 0) {
        lines.push('');
        lines.push('## System progress');
        lines.push('Each vision system compared against actual direction outcomes:');
        for (const sys of systems) {
          const gap = (sys.directions_count as number) === 0 ? ' [NO DIRECTIONS MAPPED]' : '';
          lines.push(`- ${strVal(sys.name as string)} (${strVal(sys.id as string)}): planned=${strVal(sys.planned_status as string)}, ${sys.directions_count || 0} directions, ${sys.merged || 0} merged, ${sys.failed || 0} failed${gap}`);
        }
      }

      if (dirOutcomes.length > 0) {
        lines.push('');
        lines.push('## Direction outcomes');
        lines.push('What each direction actually produced. Use confirmed findings to judge real impact:');
        for (const d of dirOutcomes) {
          const sysTag = d.system_id ? ` (system: ${strVal(d.system_id as string)})` : '';
          lines.push(`\n### ${strVal(d.text as string)} (${strVal(d.id as string)})${sysTag} [${strVal(d.status as string)}]`);
          lines.push(`Stats: ${d.cycles || 0} cycles, ${d.merged || 0} merged, ${d.failed || 0} failed`);

          const typeDist = d.type_distribution as Record<string, number> | undefined;
          if (typeDist && Object.keys(typeDist).length > 0) {
            const typeParts = Object.entries(typeDist).sort((a, b) => b[1] - a[1]).map(([t, c]) => `${c} ${t}`);
            const productRatio = typeof d.product_ratio === 'number' ? ` (${Math.round((d.product_ratio as number) * 100)}% product)` : '';
            lines.push(`Type mix: ${typeParts.join(', ')}${productRatio}`);
          }

          const lifetime = d.lifetime as Record<string, number> | undefined;
          if (lifetime) {
            lines.push(`Lifetime: ${lifetime.cycles || 0} total cycles, ${lifetime.merged || 0} total merged`);
          }

          const confirmed = Array.isArray(d.confirmed) ? d.confirmed as string[] : [];
          if (confirmed.length > 0) {
            lines.push(`Confirmed (shipped): ${confirmed.join('; ')}`);
          }
          const proposed = Array.isArray(d.proposed) ? d.proposed as string[] : [];
          if (proposed.length > 0) {
            lines.push(`Proposed (in-flight): ${proposed.join('; ')}`);
          }
          const failedApproaches = Array.isArray(d.failed_approaches) ? d.failed_approaches as string[] : [];
          if (failedApproaches.length > 0) {
            lines.push(`Failed approaches: ${failedApproaches.slice(0, 5).join('; ')}`);
          }
        }
      }

      if (lines.length > 0) {
        outcomeSection = '\n' + lines.join('\n') + '\n';
      }
    }

    // Recent interventions — time-series view of what shipped + per-week rate deltas.
    // Given with explicit non-causal framing so the agent doesn't over-attribute.
    const recentInterventions = payload.recent_interventions as Record<string, unknown> | undefined;
    let clockHeader = '';
    let recentWorkSection = '';
    if (recentInterventions && typeof recentInterventions === 'object') {
      const merges = Array.isArray(recentInterventions.merges)
        ? recentInterventions.merges as Array<Record<string, unknown>>
        : [];
      const rollingRates = recentInterventions.rolling_rates as Record<string, Record<string, unknown>> | undefined;
      const perDirectionDelta = Array.isArray(recentInterventions.per_direction_delta)
        ? recentInterventions.per_direction_delta as Array<Record<string, unknown>>
        : [];
      const hoursSinceLast = typeof recentInterventions.hours_since_last_strategic === 'number'
        ? recentInterventions.hours_since_last_strategic as number
        : null;

      // Clock header: answer "since I last ran, what's happened?" before anything else.
      // Forces the agent to notice when a cycle elapsed without meaningful throughput
      // instead of silently re-running its last playbook.
      if (hoursSinceLast !== null && rollingRates) {
        const sinceLast = rollingRates.since_last_strategic || {};
        const mergedSince = typeof sinceLast.merged === 'number' ? sinceLast.merged : 0;
        const createdSince = typeof sinceLast.created === 'number' ? sinceLast.created : 0;
        const rateSince = typeof sinceLast.merge_rate === 'number' ? `${Math.round((sinceLast.merge_rate as number) * 100)}%` : 'n/a';
        clockHeader = `\n# Clock\n`
          + `${hoursSinceLast}h since your last strategic cycle. In that window: `
          + `${mergedSince}/${createdSince} merged (${rateSince}).\n`
          + `If that's a low-value ${hoursSinceLast}h relative to the ambition you set last time, that's information — ask why before doubling down on the same plays.\n`;
      }

      if (merges.length > 0 || rollingRates || perDirectionDelta.length > 0) {
        const lines: string[] = ['\n# Recent Work & Rolling Outcomes'];
        lines.push('IMPORTANT — read this carefully before acting:');
        lines.push('- Correlation is NOT causation. Rate swings can reflect task difficulty shifts, account health, rate limits, CI outages, or small-sample noise.');
        lines.push('- Weekly buckets were replaced with rolling windows (last_24h / last_3d / last_7d / since_last_strategic) because weekly is too coarse for this pipeline\'s velocity.');
        lines.push('- Only act on patterns visible across 2+ of the overlapping windows. A spike in last_24h that does not show in last_3d is probably noise.');
        lines.push('- Prefer "investigate whether X helps" over "revert X".');

        if (rollingRates) {
          lines.push('\nRolling merge-rate windows:');
          const order = ['since_last_strategic', 'last_24h', 'last_3d', 'last_7d'];
          for (const w of order) {
            const r = rollingRates[w];
            if (!r) continue;
            const created = typeof r.created === 'number' ? r.created : 0;
            const merged = typeof r.merged === 'number' ? r.merged : 0;
            const rate = typeof r.merge_rate === 'number' ? `${Math.round((r.merge_rate as number) * 100)}%` : 'n/a';
            lines.push(`  ${w}: ${merged}/${created} merged (${rate})`);
          }
        }

        if (perDirectionDelta.length > 0) {
          lines.push('\nPer-direction outcomes in the rolling windows:');
          for (const d of perDirectionDelta) {
            const dirText = strVal(d.direction_text, '').slice(0, 100);
            lines.push(`\n- ${dirText} (${strVal(d.direction_id)})`);
            const sinceLast = d.since_last_strategic as Record<string, unknown> | undefined;
            const last24h = d.last_24h as Record<string, unknown> | undefined;
            const last3d = d.last_3d as Record<string, unknown> | undefined;
            const fmt = (r: Record<string, unknown> | undefined): string => {
              if (!r) return '0/0';
              const c = typeof r.created === 'number' ? r.created : 0;
              const m = typeof r.merged === 'number' ? r.merged : 0;
              return `${m}/${c}`;
            };
            const parts: string[] = [];
            if (sinceLast) parts.push(`since_last_strategic: ${fmt(sinceLast)}`);
            parts.push(`last_24h: ${fmt(last24h)}`, `last_3d: ${fmt(last3d)}`);
            lines.push(`  ${parts.join(', ')}`);
            const recentTitles = Array.isArray(d.recent_merged_titles) ? d.recent_merged_titles as string[] : [];
            if (recentTitles.length > 0) {
              lines.push(`  Recently shipped: ${recentTitles.map(t => `"${t}"`).join('; ')}`);
            }
          }
        }

        if (merges.length > 0) {
          lines.push('\nRecent merges (newest first, with body when informative):');
          for (const m of merges) {
            const title = strVal(m.pr_title, '?').slice(0, 120);
            const pr = m.pr_number ? `#${m.pr_number}` : '';
            const hoursAgo = typeof m.hours_ago === 'number' ? `${m.hours_ago}h ago` : '?';
            const dirText = strVal(m.direction_text, '').slice(0, 80);
            const dirTag = dirText ? ` [direction: ${dirText}]` : ' [no direction]';
            lines.push(`  - ${title} ${pr} (${hoursAgo})${dirTag}`);
            const body = strVal(m.body, '').trim();
            if (body) {
              const indented = body.split('\n').map(l => `    ${l}`).join('\n');
              lines.push(indented);
            }
          }
        }
        recentWorkSection = lines.join('\n') + '\n';
      }
    }

    // Unified conversation thread — chronological, shows back-and-forth
    const conversationThread = Array.isArray(payload.conversation_thread)
      ? payload.conversation_thread as Array<Record<string, unknown>>
      : [];
    const threadSection = conversationThread.length > 0
      ? '\n## Conversation History\n' +
        conversationThread.map(m => {
          const age = calcMessageAge(m);
          const status = strVal(m.status);
          const from = strVal(m.from);
          const tag = status === 'resolved' ? `${from}, ${age}, RESOLVED` : `${from}, ${age}, OPEN`;
          let line = `[${tag}] [${strVal(m.type)}] ${strVal(m.content).slice(0, 5000)}`;
          if (status === 'resolved' && m.resolution) {
            line += `\n  -> ${strVal(m.resolution).slice(0, 500)}`;
          }
          if (status === 'open') {
            line += ` (id: ${strVal(m.id)})`;
          }
          return line;
        }).join('\n') + '\n'
      : '';

    // Extract open FEEDBACK messages — these are direct user requests
    const feedbackMessages = openMessages.filter(m => strVal(m.type) === 'FEEDBACK');
    const feedbackSection = feedbackMessages.length > 0
      ? (() => {
          const feedbackLines = feedbackMessages.map(m => {
            const age = calcMessageAge(m);
            return `- (id: ${strVal(m.id)}, from: ${strVal(m.from)}, open ${age}) ${strVal(m.content).slice(0, 5000)}`;
          }).join('\n');
          return `\n## User Feature Requests\nThese FEEDBACK messages are direct user requests. Strongly consider creating a direction for each unaddressed request:\n${feedbackLines}\n`;
        })()
      : '';

    // Flat open messages list for executor resolve operations
    const openMessagesText = openMessages.length > 0
      ? openMessages.map(m => {
          const age = calcMessageAge(m);
          return `- [${strVal(m.type)}] (id: ${strVal(m.id)}, from: ${strVal(m.from)}, open ${age}) ${strVal(m.content).slice(0, 5000)}`;
        }).join('\n')
      : '';

    const dirSummaryText = directionsSummary.length > 0
      ? directionsSummary.map((d, i) => {
          const parts = [`${d.total_cycles || 0} cycles`];
          if (d.prs_merged) parts.push(`${d.prs_merged} merged`);
          if (d.tasks_failed) {
            const infraFailed = (d.tasks_failed_infra as number) || 0;
            parts.push(infraFailed > 0 ? `${d.tasks_failed} failed (${infraFailed} infra)` : `${d.tasks_failed} failed`);
          }
          const satNote = d.possibly_saturated ? ' [SATURATED]' : '';
          const catNote = d.category ? ` [${strVal(d.category)}]` : '';
          const sysNote = d.system_id ? ` (system: ${strVal(d.system_id)})` : '';

          // Research context: show established findings count, failed approaches, open questions
          let ctxSection = '';
          if (typeof d.research_context === 'string' && (d.research_context as string).length > 0) {
            const sections = parseResearchContext(d.research_context as string);
            const ctxParts: string[] = [];
            if (sections.established.length > 0) {
              const confirmed = sections.established.filter(s => s.includes('(confirmed)'));
              const proposed = sections.established.filter(s => s.includes('(proposed)'));
              ctxParts.push(`   ${confirmed.length} confirmed findings, ${proposed.length} proposed`);
            }
            if (sections.failed.length > 0) {
              ctxParts.push(`   Failed approaches: ${sections.failed.slice(0, 3).join('; ')}`);
            }
            if (sections.open.length > 0) {
              ctxParts.push(`   Open questions: ${sections.open.slice(0, 3).join('; ')}`);
            }
            if (sections.eviction) {
              ctxParts.push(`   \u26a0 Context trimmed: ${sections.eviction}. Earlier findings were evicted — check git history if you suspect overlap with prior work.`);
            }
            if (ctxParts.length > 0) ctxSection = '\n' + ctxParts.join('\n');
          }

          // Recent findings: what was actually discovered/built
          let findingsSection = '';
          const findings = Array.isArray(d.recent_findings) ? d.recent_findings as string[] : [];
          if (findings.length > 0) {
            findingsSection = `\n   Recent work: ${findings.slice(0, 5).join('; ')}`;
          }

          // Recent failures: why things failed (with category for infra vs code distinction)
          let failuresSection = '';
          const failures = Array.isArray(d.recent_failures) ? d.recent_failures as Array<Record<string, string>> : [];
          if (failures.length > 0) {
            const failLines = failures.slice(0, 3).map(f => {
              const cat = f.category ? ` [${f.category}]` : '';
              return `${f.title || '?'}${cat}: ${f.reason || 'unknown'}`;
            });
            failuresSection = `\n   Recent failures: ${failLines.join('; ')}`;
          }

          return `${i + 1}. [${strVal(d.priority, 'medium')}]${catNote} ${strVal(d.text)} (id: ${strVal(d.id)})${satNote}${sysNote}\n   Stats: ${parts.join(', ')}${ctxSection}${findingsSection}${failuresSection}`;
        }).join('\n\n')
      : 'No directions exist yet.';

    // Build design doc section — this is the primary input for Phase 2
    const designDoc = payload.design_doc as Record<string, unknown> | undefined;
    const designDocJson = designDoc && Object.keys(designDoc).length > 0
      ? JSON.stringify(designDoc, null, 2)
      : '';

    // Write the strategic helper script (same pattern as design-doc)
    // Uses printf + pipe to avoid shell expansion of JSON in curl -d
    const strategicHelper = `#!/bin/bash
# Strategic review tool — updates vision, directions, and messages via API
# Reads ORCHESTRATOR_URL and AGENT_API_TOKEN from env
OP="\${1:?Usage: strategic set-vision|create-direction|update-direction|retire-direction|create-message|resolve-message|spawn-task <json>}"
shift
API="\${ORCHESTRATOR_URL:?ORCHESTRATOR_URL not set}"
TOKEN="\${AGENT_API_TOKEN:?AGENT_API_TOKEN not set}"
TASK="${taskId}"
DATA="\$1"

_call() {
  local attempt=0
  while [ \$attempt -lt 3 ]; do
    local response
    response=\$(printf '{"task_id":"%s","op":"%s","data":%s}' "\$TASK" "\$1" "\$DATA" | \\
      curl -sS --max-time 30 --connect-timeout 10 \\
        -X POST "\$API/api/agent/strategic" \\
        -H "Content-Type: application/json" \\
        -H "X-Agent-Token: \$TOKEN" \\
        --data-binary @- \\
        -w '\n%{http_code}')
    local http_code
    http_code=\$(printf '%s' "\$response" | tail -n1)
    local body
    body=\$(printf '%s' "\$response" | head -n -1)
    if [ "\$http_code" -ge 200 ] && [ "\$http_code" -lt 300 ]; then
      printf '%s' "\$body"
      return 0
    fi
    if [ "\$http_code" -eq 409 ]; then
      printf '%s' "\$body"
      echo "[CONSTRAINT] \$1 rejected (HTTP 409)" >&2
      return 0
    fi
    attempt=\$((attempt + 1))
    [ \$attempt -lt 3 ] && echo "[RETRY \$attempt/3] \$1 failed (HTTP \$http_code), retrying in 5s..." >&2 && sleep 5
  done
  echo "[ERROR] \$1 failed after 3 attempts" >&2
  return 1
}

case "\$OP" in
  set-vision)        _call "set_vision" ;;
  create-direction)  _call "create_direction" ;;
  update-direction)  _call "update_direction" ;;
  retire-direction)  _call "retire_direction" ;;
  create-message)    _call "create_message" ;;
  resolve-message)   _call "resolve_message" ;;
  spawn-task)        _call "spawn_task" ;;
  *) echo "Unknown op: \$OP" >&2; exit 1 ;;
esac
echo ""
`;
    fs.writeFileSync(`${WORKSPACE}/strategic`, strategicHelper, { mode: 0o755 });
    excludeAgentHelpers();

    // Also write design-doc helper so strategic agent can read/write sections
    const designDocHelper = `#!/bin/bash
API="\${ORCHESTRATOR_URL:?ORCHESTRATOR_URL not set}"
TOKEN="\${AGENT_API_TOKEN:?AGENT_API_TOKEN not set}"
OP="\${1:?Usage: design-doc get|set|delete|list <key> [json-value]}"
KEY="\$2"
VALUE="\$3"
if [ "\$OP" = "list" ]; then
  curl -f -X POST "\$API/api/agent/design-doc" -H "Content-Type: application/json" -H "X-Agent-Token: \$TOKEN" -d "{\\"task_id\\": \\"${taskId}\\", \\"op\\": \\"list\\"}"
elif [ "\$OP" = "get" ]; then
  curl -f -X POST "\$API/api/agent/design-doc" -H "Content-Type: application/json" -H "X-Agent-Token: \$TOKEN" -d "{\\"task_id\\": \\"${taskId}\\", \\"op\\": \\"get\\", \\"key\\": \\"\$KEY\\"}"
elif [ "\$OP" = "set" ]; then
  curl -f -X POST "\$API/api/agent/design-doc" -H "Content-Type: application/json" -H "X-Agent-Token: \$TOKEN" -d "{\\"task_id\\": \\"${taskId}\\", \\"op\\": \\"set\\", \\"key\\": \\"\$KEY\\", \\"value\\": \$VALUE}"
elif [ "\$OP" = "delete" ]; then
  curl -f -X POST "\$API/api/agent/design-doc" -H "Content-Type: application/json" -H "X-Agent-Token: \$TOKEN" -d "{\\"task_id\\": \\"${taskId}\\", \\"op\\": \\"delete\\", \\"key\\": \\"\$KEY\\"}"
fi
echo ""
`;
    fs.writeFileSync(`${WORKSPACE}/design-doc`, designDocHelper, { mode: 0o755 });
    excludeAgentHelpers();

    // ── Compute direction health summary for thinker prompt ──
    const maxActiveDirections = typeof payload.max_active_directions === 'number'
      ? payload.max_active_directions
      : 10;
    const activeDirections = directionsSummary.filter(d => strVal(d.status, 'active') === 'active');
    const activeCount = activeDirections.length;
    const saturatedCount = activeDirections.filter(d => d.possibly_saturated === true).length;
    const productCount = activeDirections.filter(d => strVal(d.category) === 'product').length;
    const availableSlots = maxActiveDirections - activeCount;
    const gaps: string[] = [];
    if (productCount === 0) gaps.push('NO_PRODUCT_DIRECTIONS');
    if (saturatedCount === activeCount && activeCount > 0) gaps.push('ALL_SATURATED');
    if (activeCount < 3) gaps.push('BELOW_MINIMUM');
    const dirHealthSummary = [
      `Active: ${activeCount}/${maxActiveDirections} slots used (${availableSlots} available). Saturated: ${saturatedCount}/${activeCount}. Product directions: ${productCount}.`,
      gaps.length > 0 ? `GAPS DETECTED: ${gaps.join(', ')}` : 'No critical gaps detected.',
    ].join('\n');

    // ── Running context from prior cycles (design doc notes/strategic-context) ──
    const strategicContext = designDoc
      ? strVal((designDoc as Record<string, unknown>)['notes/strategic-context'])
      : '';

    // ── Pass 1: Thinker — analyze freely, write prose ──
    // Let the agent do what it's good at: read code, reason about architecture,
    // write a strategic analysis. No API constraints, no JSON formatting.
    const thinkerPrompt = `${contextPrefix}You own this project's strategic direction.

# Project: ${projectName}
${clockHeader}${strategicContext ? `\n## Running Context (from prior cycles)\n${strategicContext}\n` : ''}
## Architecture (design document — current state)
${designDocJson || 'No design document yet.'}

## Vision (current)
${visionJson || 'No vision set yet.'}
${outcomeSection}
## Current research directions
${dirSummaryText}

## Direction Health
${dirHealthSummary}
${recentWorkSection}${feedbackSection}${threadSection}
# Your task

Read the vision's purpose and context carefully. They tell you what kind of project this is and what the user values. Calibrate every recommendation to what the user actually needs, not to a generic "best practices" standard. Different projects have radically different priorities — don't assume yours.

If the vision doesn't make the project's priorities clear (speed vs correctness, features vs polish, etc.), ask the user once and save their answer in the vision context so you don't need to ask again.

You have everything above: what exists (design doc), where we're going (vision), what's actually shipping (outcomes), what we're working on (directions), and user feedback. Do NOT explore the codebase — the architecture agent handles that.

Start from the end state. What is this project meant to become? Read the vision's purpose — that's the destination. Now look at where we actually are (outcomes, directions, design doc). What's the gap?

Think about what bold moves close that gap:
- What would make a user say "I can't work without this"? Is anyone building it?
- Are we stuck in a local optimum — lots of small improvements instead of one big leap?
- Look at the outcome data honestly — are directions producing real value, or busywork?
- Are we defaulting to infrastructure when product features would matter more?
- What should we STOP doing? Which directions are dead weight?
- What are we avoiding that we shouldn't be?

Think big. If a direction would fundamentally change the product's trajectory, it's worth more than five safe incremental ones. Don't optimize the current plan — question whether it's the right plan.

## Read the metrics through the vision's lens

You're being shown merge-rate, success-rate, cycle counts, per-direction throughput, weekly deltas. These are proxies — useful but partial. The vision is the authoritative measure of what "better" means here. For every metric in front of you:

- **Translate the metric into vision-progress.** "Merge-rate up 5pp" isn't a win on its own — it's a win only if the merged work is the kind of work the vision calls for. Before acting on a metric trend, name how it maps to the vision's purpose. If you can't, the metric probably isn't what you should be optimizing for.

- **When evaluating a direction, ask "what would push us furthest toward the vision?" — not "what would move this number most?"** Those often align, but when they diverge the vision wins. A direction that hits its numeric target while leaving the vision no closer is finished, not successful. Retire or reframe it.

- **Watch for the metric-proxy drift.** If recent directions moved a number (merge-rate, review-pass rate, cycle count) without visibly moving the user's experience or the vision's stated outcomes, that's a signal the metric has detached from what it's meant to proxy. Spot it early — propose a direction that either re-anchors the metric or replaces it with something closer to the vision.

This isn't about distrusting numbers or being adversarial about every measurement. It's about remembering that metrics exist to serve the vision, not the other way around. When they agree, act on them. When they drift apart, trust the vision.

Some task failures are caused by the pipeline itself — marked [PROVISION_FAILURE], [RATE_LIMIT], [AGENT_TIMEOUT], or [EXTERNAL_API] in the failure details. These say nothing about the direction's quality. If you see a pattern of infra failures, flag it to the user as a pipeline issue (\`./messages send 'observation' NOTE\`) rather than penalizing the direction.

Think about the WHAT, not the HOW. Direction text should describe outcomes ("users can X", "the system handles Y"), not implementation steps. Leave the implementation planning to research agents.

If you have questions for the user — about priorities, intent, or things you can't resolve from the data — ask them now using \`./messages send 'question' QUESTION\`. Better to ask during analysis than to guess wrong during execution. If there are open messages, respond to them.

Don't worry about formatting, JSON, or direction operations. Just think and write. A follow-up step will translate your analysis into concrete actions.

Before finishing, ALSO write /tmp/research_progress_${taskId}.md — a concise markdown summary (under ~3000 chars) of your analysis: the gap you identified between vision and current state, the bold moves you considered, and the rationale for your recommendations. This is surfaced to retry agents if this task fails.`;

    statusRef.value = 'Running strategic analysis (pass 1: thinking)...';
    const thinkerResult = await runClaudeCode(thinkerPrompt, ws, statusRef, model, effort, disallowedTools);
    const rawAnalysis = thinkerResult.lastResultText || thinkerResult.allOutput.slice(-80_000);
    const analysisText = rawAnalysis;

    // ── Pass 2: Executor — translate analysis into API calls ──
    // Short, focused pass. The analysis is done. This just calls the tools.
    fs.writeFileSync(`${WORKSPACE}/CLAUDE.md`,
      `# Executor Agent\nYour ONLY job is to call ./strategic and ./design-doc commands.\nDo NOT analyze code. Do NOT write prose. Just run the bash commands.\n`
    );

    const executorPrompt = `You are a strategic executor. Your job is to translate the analysis below into direction operations, but with JUDGMENT — not blind execution. You decide how the analysis maps to directions.

## Ownership
You own this project's strategic direction. The analysis above is your reasoning — now execute it. If the data clearly supports a change, make it. Don't hedge, don't soften, don't ask permission for things the outcome data already answers.

Only escalate (\`./messages send 'question' QUESTION\`) when you literally cannot proceed without the user:
- Missing credentials, API keys, deploy targets, or external access
- Ambiguous user intent where the wrong call wastes cycles
- External blockers outside the system's control

If a direction is dead, retire it. If there's an obvious gap, fill it. If a user asked for something, create a direction for it.

Do NOT analyze code. Do NOT explore the codebase. Think briefly about each operation, then run the commands.

# Commands available

\`\`\`bash
# Set the vision (REQUIRED)
# "purpose" = stable product identity (what the product IS — change rarely)
# "focus" = current strategic bets (what we're building NOW — update every cycle, can have multiple parallel goals)
./strategic set-vision '{"purpose": "...", "focus": ["current goal 1", "current goal 2"], "success_criteria": ["..."], "systems": [{"id": "sys-x", "name": "...", "description": "...", "status": "planned|in_progress|functional|mature", "depends_on": []}], "priorities": ["sys-x"], "context": "..."}'

# Create research directions
# conflicts_with: list direction IDs that touch overlapping code/systems — they won't run in parallel
./strategic create-direction '{"text": "...", "priority": "high|medium|low", "category": "product|structural", "system_id": "sys-x", "rationale": "...", "conflicts_with": ["d-xxx"]}'

# Update existing directions (can set conflicts_with to control parallel execution)
./strategic update-direction '{"direction_id": "d-xxx", "changes": {"priority": "medium", "text": "...", "conflicts_with": ["d-yyy"]}, "rationale": "..."}'

# Retire directions
./strategic retire-direction '{"direction_id": "d-xxx", "rationale": "..."}'

# Spawn a targeted investigation task (research or plan only) when something from
# the Recent Work section needs investigation NOW instead of waiting for the next
# autonomous cycle. The spawned task runs at high priority (ahead of routine work).
# USE THIS FOR: measuring whether a shipped feature correlates with a rate change,
# auditing a suspicious pattern, investigating a user message that's blocking progress.
# DO NOT USE AS: a way to skip direction review, or to directly spawn implementation
# (create/fix/refactor are explicitly rejected — those must go through normal flow).
./strategic spawn-task '{"type": "research", "title": "Measure whether X shipped in week Y correlates with the merge-rate drop", "prompt": "Full investigation prompt...", "direction_id": "d-xxx"}'

# Save notes
./design-doc set "notes/strategic-review" '"..."'

# Respond to or resolve user messages
./messages resolve <message-id> 'How it was addressed'
./messages send 'Response or note to the user'
\`\`\`

# Existing directions (for update/retire operations)
${dirSummaryText}

${feedbackSection}${openMessagesText ? `# Open Messages (resolve addressed ones, respond to questions)\n${openMessagesText}\n` : ''}
# Strategic analysis to execute

${analysisText.slice(0, 50_000)}

# Instructions

1. Run ./strategic set-vision with the vision from the analysis. The "purpose" is the stable product identity — what the product IS at its core. Only change it if the product's fundamental nature has shifted. The "focus" is a list of current strategic bets — what you're actively building NOW. These change every cycle as goals complete or priorities shift. Multiple parallel goals are normal.

2. **Before creating or updating any directions, plan your operations.** For each idea in the analysis:
   - Does an existing direction already cover this? → UPDATE it (merge the ideas), don't create a duplicate.
   - Can two similar ideas be combined into one direction? → Create one direction, not two.
   - Does this depend on another direction being done first? → PAUSE it (status: "paused") or don't create it yet.
   Direction text should describe the desired OUTCOME ("users can X", "the system handles Y"), not implementation steps. Keep implementation details in the rationale field.

3. **Priority is a scarce resource.** The system researches one direction per cycle. Having 5+ high-priority directions means random selection. Be deliberate:
   - high: 1-2 directions max — the most important work RIGHT NOW
   - medium: the next tier — important but can wait for high-priority work
   - low: nice to have, opportunistic

4. Run the direction operations (create/update/retire).

5. Run ./design-doc set "notes/strategic-review" with key observations.

6. **Leave feedback for next cycle.** Run ./design-doc set "notes/strategic-feedback" with honest observations about what's working and what isn't. Examples:
   - "Direction X has had 3 cycles with zero merges — research keeps proposing the same thing"
   - "The thinker keeps creating overlapping directions around Y"
   - "Priority spread is good, directions are well-scoped"
   - "Research agents are ignoring the dependency ordering"
   This persists in the design doc and will be visible to the thinker on the next strategic cycle.

7. Review open messages — resolve addressed ones, respond to questions. Check the conversation history for your own prior QUESTION messages — if a user message answers one, resolve it: \`./messages resolve <id> 'Answered — [summary]'\`.

8. **Update running context.** Save a concise summary of this cycle's key takeaways:
   ./design-doc set "notes/strategic-context" '"Decisions: [what changed and why] | User preferences: [anything expressed in messages] | Pending questions: [open questions awaiting user input] | Blockers: [external constraints]"'
   This carries forward to future cycles. Overwrite the previous version — this is a living summary, not a log.

9. Guardrails: ensure at least 3 active directions remain and at least one has category="product".

10. **Post ONE summary message** explaining what changed and why:
    ./messages send 'Strategic review complete: [1-2 sentence summary]. Key changes: [list major direction/vision changes].' NOTE

11. Done. Do not write anything else.`;

    statusRef.value = 'Running strategic execution (pass 2: applying decisions)...';
    let { exitCode, allOutput, lastResultText } = await runClaudeCode(executorPrompt, ws, statusRef, model, effort, disallowedTools);

    // Post-check: verify the executor actually called ./strategic set-vision.
    // Check the output for the set_vision response.
    // Matches the design-doc.ts post-check + retry pattern (lines 185-205).
    try {
      // Check: look for set_vision success response in executor output
      const visionSetInOutput = /"op"\s*:\s*"set_vision"/.test(allOutput);

      if (exitCode === 0 && !visionSetInOutput) {
        sendLog(ws, '\n[WARNING] Strategic executor completed but ./strategic set-vision was not called. Retrying...\n');
        const retryResult = await runClaudeCode(
          'Your previous attempt did not call ./strategic set-vision. This is REQUIRED.\n\n' +
          'Run this command NOW with the vision from your analysis:\n' +
          './strategic set-vision \'{"purpose": "...", "focus": ["..."], "success_criteria": [...], "systems": [...], "priorities": [...]}\'\n' +
          'Extract the vision from the analysis and call the command. Nothing else.',
          ws, statusRef, model, effort, disallowedTools
        );
        exitCode = retryResult.exitCode;
        allOutput = allOutput + '\n' + retryResult.allOutput;
        lastResultText = retryResult.lastResultText || lastResultText;
      }
    } catch (e) {
      sendLog(ws, `[WARNING] Could not verify strategic set-vision call: ${e}\n`);
    }

    // Post-check: verify direction operations were executed if thinker recommended them
    try {
      const directionCreatedInOutput = /"status"\s*:\s*"ok"[\s\S]{0,100}"op"\s*:\s*"create_direction"/.test(allOutput)
        || /"op"\s*:\s*"create_direction"[\s\S]{0,100}"status"\s*:\s*"ok"/.test(allOutput);
      const directionUpdatedInOutput = /"status"\s*:\s*"ok"[\s\S]{0,100}"op"\s*:\s*"(update_direction|retire_direction)"/.test(allOutput)
        || /"op"\s*:\s*"(update_direction|retire_direction)"[\s\S]{0,100}"status"\s*:\s*"ok"/.test(allOutput);
      const anyDirectionOp = directionCreatedInOutput || directionUpdatedInOutput;

      // Detect constraint rejections (cap/dedup) so we don't retry wastefully
      const directionRejectedInOutput = /\[CONSTRAINT\].*create_direction/.test(allOutput) || /"status"\s*:\s*"skipped"/.test(allOutput);

      // Check if thinker's structured output contained direction recommendations
      const structuredSection = rawAnalysis.match(/## Structured Output[\s\S]*$/i)?.[0] || '';
      const thinkerRecommendedCreates = /create[_-]direction|"op"\s*:\s*"create"/i.test(structuredSection);

      if (directionRejectedInOutput && !anyDirectionOp) {
        sendLog(ws, '\n[INFO] Direction creation was attempted but rejected by constraints (cap/duplicate). Not retrying.\n');
      } else if (exitCode === 0 && thinkerRecommendedCreates && !anyDirectionOp) {
        sendLog(ws, '\n[WARNING] Strategic executor completed but no direction operations were executed. Retrying...\n');
        const retryResult = await runClaudeCode(
          'Your previous attempt did not execute any ./strategic create-direction commands.\n\n' +
          'The strategic analysis recommended creating new directions. Run the create-direction commands NOW:\n\n' +
          'For each new direction in the analysis, run:\n' +
          './strategic create-direction \'{"text": "...", "priority": "high|medium|low", "category": "product|structural", "system_id": "sys-x", "rationale": "..."}\' \n\n' +
          'Extract the directions from the analysis and call the commands. Nothing else.',
          ws, statusRef, model, effort, disallowedTools
        );
        exitCode = retryResult.exitCode;
        allOutput = allOutput + '\n' + retryResult.allOutput;
        lastResultText = retryResult.lastResultText || lastResultText;
      }
    } catch (e) {
      sendLog(ws, `[WARNING] Could not verify strategic direction ops: ${e}\n`);
    }

    // Legacy: try to read /tmp output file (backward compat)
    const strategicOutput = readHandlerOutput(taskId, 'strategic_output', ws);
    let logs = strategicOutput !== null ? sanitize(JSON.stringify(strategicOutput)) : '';
    if (!logs) {
      const stratObj = extractJsonObject(allOutput, 'vision', 'direction_operations');
      logs = sanitize(stratObj ? JSON.stringify(stratObj) : (lastResultText || allOutput.slice(-50_000)));
    }

    const strategicProgress = readProgressArtifact(taskId, 'research');
    try {
      await persistAndPostStatus(taskId, {
        task_id: taskId,
        status: exitCode === 0 ? 'COMPLETED' : 'FAILED',
        ...(exitCode !== 0 ? { artifact_type: 'ERROR_LOG' } : { artifact_type: 'RESEARCH_FINDINGS' }),
        logs,
        ...(exitCode !== 0 ? { failure_category: 'claude_code_failed' } : {}),
        ...(strategicProgress ? { analysis_plan: strategicProgress.slice(0, 5000) } : {}),
      });
    } catch (e) {
      sendLog(ws, `[ERROR] Failed to post strategic completion status: ${e}\n`);
    }
    return;
  }

  // ── Tactical research mode (default) ──────────────────────────────────
  const recentlyCompleted = Array.isArray(projectCtx?.recently_completed_tasks)
    ? (projectCtx.recently_completed_tasks as Array<{title: string, task_type: string, status?: string}>)
    : [];

  // Build design doc section for tactical research
  const tacticalDesignDoc = payload.design_doc as Record<string, unknown> | undefined;
  const dirDesignCtx = payload.direction_design_context as Record<string, unknown> | undefined;

  // Direction-specific design doc highlight — surfaces relevant system + spec upfront
  let designDocHighlight = '';
  if (dirDesignCtx) {
    const lines: string[] = ['# YOUR DIRECTION\'S ARCHITECTURE CONTEXT'];
    const sys = dirDesignCtx.system as Record<string, unknown> | undefined;
    if (sys) {
      lines.push(`System: ${strVal(sys.name as string)} (${strVal(sys.id as string)}) — status: **${strVal(sys.status as string)}**`);
      if (sys.description) lines.push(`Description: ${strVal(sys.description as string)}`);
      if (sys.spec) {
        const spec = sys.spec as Record<string, unknown>;
        lines.push('Architectural spec (use this as your blueprint):');
        if (spec.approach) lines.push(`  Approach: ${strVal(spec.approach as string)}`);
        if (Array.isArray(spec.technology)) lines.push(`  Technology: ${(spec.technology as string[]).join(', ')}`);
        if (Array.isArray(spec.interfaces)) lines.push(`  Interfaces: ${(spec.interfaces as string[]).join('; ')}`);
        if (Array.isArray(spec.integration_points)) lines.push(`  Integration: ${(spec.integration_points as string[]).join('; ')}`);
        if (spec.notes) lines.push(`  Notes: ${strVal(spec.notes as string)}`);
      }
    } else {
      lines.push(`System ${strVal(dirDesignCtx.system_id as string)}: not yet documented in design doc`);
    }
    const missingCaps = dirDesignCtx.missing_capabilities as Record<string, Record<string, string>> | undefined;
    if (missingCaps && Object.keys(missingCaps).length > 0) {
      lines.push('Missing/partial capabilities relevant to your direction:');
      for (const [name, cap] of Object.entries(missingCaps)) {
        lines.push(`  - ${name}: ${cap.status}${cap.notes ? ` (${cap.notes})` : ''}`);
      }
    }
    lines.push('');
    designDocHighlight = lines.join('\n') + '\n';
  }

  // Design doc: only include key listing + playbook. Full sections available via ./design-doc get <key>.
  let designDocSection = '';
  if (tacticalDesignDoc && Object.keys(tacticalDesignDoc).length > 0) {
    const docKeys = (tacticalDesignDoc._keys as string[]) || Object.keys(tacticalDesignDoc).filter(k => !k.startsWith('_') && !k.startsWith('playbook/'));
    const playbookEntries = Object.entries(tacticalDesignDoc)
      .filter(([k]) => k.startsWith('playbook/'))
      .map(([k, v]) => `- **${k.replace('playbook/', '')}**: ${typeof v === 'string' ? v : JSON.stringify(v)}`)
      .join('\n');
    designDocSection = `# ARCHITECTURE (Design Document)
Available sections (use \`./design-doc get <key>\` to read): ${docKeys.join(', ')}

${playbookEntries ? `## Operational Playbook\n${playbookEntries}\n` : ''}
`;
  }

  const researchRetryContext = buildRetryContext(task);
  const researchFirstTaskSection = task.first_task_on_repo ? FIRST_TASK_GUIDANCE : '';
  const researchPrompt = `${contextPrefix}${researchPlaybookSection}${researchRetryContext}You are a codebase research agent inside Blueprint, an autonomous AI coding system. Your findings spawn implementing agents, get reviewed, and merge as PRs.

# MISSION

Your job is to advance the system toward its directions. HOW you do that depends on what exists:

**If a direction targets something MISSING** (the design document shows it as missing, stub, or
nonexistent): your job is to DESIGN it. Output a single large-scope finding that builds the
foundation. The plan agent will decompose it into ordered implementation steps. Don't output
5 small findings when the system needs 1 foundational one.

**If the design document has a spec for the target system** (look for a "spec" field on the
system entry): use it as your architectural blueprint. The architecture agent analyzed the
full codebase and wrote that spec to fit the existing stack. Follow its approach, technology
choices, and integration points. You should still explore the codebase to verify the spec
makes sense, but don't redesign from scratch when a spec exists.

**If the codebase is nearly empty** (few source files, mostly config/scaffolding): this is
normal for new projects. Use the design doc specs and the project's config files to create
foundational structure. Output a large-scope finding that bootstraps the target system.

**If a direction targets something EXISTING** (the design document shows it as partial, functional,
or mature): your job is to IMPROVE it. Find specific, high-impact changes — missing capabilities,
broken assumptions, architectural bottlenecks, or small fixes that prevent cascade failures.
Quality over quantity — 2 real findings beats 10 incremental ones.

**Sequencing matters.** If your findings have dependencies (e.g., "build cache infrastructure"
must come before "add caching to user API"), use depends_on_indices to order them. The pipeline
respects these — dependent tasks won't start until prerequisites complete.

**Sequence wisely.** If foundational infrastructure is genuinely missing (check research context for gaps),
build it. But if foundations exist, prioritize user-visible features and product improvements over
additional structural refinement. Don't re-propose foundations that are already confirmed.

# CODEBASE CONTEXT

${codebaseSummary}${fileTreeSection}${gitHistorySection}
${designDocHighlight}${designDocSection}# DIRECTIONS
${userPrompt ? `Research focus: ${userPrompt}\n\n` : ''}${directionsText}${crossDirSection}
${pipelineScopeRatesSection}${anomalySection}
# DEDUP — do not re-propose these
${(() => {
  const lines: string[] = [];
  if (activeTasks.length > 0) {
    lines.push('## Active (IN_PROGRESS / EVALUATING)');
    for (const t of activeTasks) {
      const dirId = strVal(t['direction_id']);
      const dirAttr = dirId ? ` [direction: ${dirId}]` : '';
      lines.push(`- [${strVal(t['task_type'])}] ${strVal(t['title'])} (active)${dirAttr}`);
    }
  }
  if (queuedTasks.length > 0) {
    lines.push('## Queued (awaiting execution)');
    for (const t of queuedTasks) {
      const dirId = strVal(t['direction_id']);
      const dirAttr = dirId ? ` [direction: ${dirId}]` : '';
      lines.push(`- [${strVal(t['task_type'])}] ${strVal(t['title'])} (queued)${dirAttr}`);
    }
  }
  if (recentlyCompleted.length > 0) {
    lines.push('## Recently completed / failed / merged');
    for (const t of recentlyCompleted) {
      const status = t.status ?? 'completed';
      const label = status === 'failed' ? '(FAILED)' : status === 'cancelled' ? '(cancelled)' : '(merged)';
      lines.push(`- [${t.task_type}] ${t.title} ${label}`);
    }
  }
  return lines.length > 0 ? lines.join('\n') : '(none yet)';
})()}

# HOW TO RESEARCH

1. Explore aggressively — spawn subagents for parallel exploration, use WebSearch for best practices, use TodoWrite to track coverage.
2. For each direction, ask: "what would it take for this to be DONE?" Work backward from the goal.
3. Check the design document — if the direction targets something missing, look for a spec on that system entry first. If a spec exists, use it as your design foundation. If no spec, design from scratch. If the direction targets something existing, improve it.
4. Before writing findings, verify you explored all major directories.
5. Finding prompts must describe the PROBLEM and desired outcome — NOT the implementation.
   Do not write line numbers, specific function signatures, or step-by-step code changes.
   The implementing agent reads the full codebase and decides how to build it. Over-specified
   prompts waste time and anchor the agent on a suboptimal approach.
   For foundational findings (scope=large), include: what the system needs and why, recommended
   approach and technology choices, key architectural decisions, and how it fits with existing systems.
   For smaller findings, state what's wrong and what "done" looks like — nothing more.

# SCOPE GUIDANCE

- **Building something new?** Use scope=large. Write the finding prompt as a design brief.
  The plan agent will decompose this into ordered implementation steps.
- **Improving something existing?** Use scope=small or medium as appropriate.
- **Adding user-facing features?** Product features, UI improvements, and API enhancements are equally valid findings. Don't deprioritize them in favor of structural work.
- **Building features on a foundation?** If research context shows a foundation was confirmed
  (e.g., "(confirmed)" entries), output findings that USE it. These are typically scope=medium.

Output ALL meaningful findings you discover — don't stop at one.
A direction may need both foundational work AND incremental improvements.
If you find 5 things worth doing, output 5 findings. Be ambitious — propose
infrastructure, new systems, and architectural changes when the direction calls
for it. Use scope=large when the change genuinely requires cross-component coordination. Prefer scope=small or scope=medium for focused product features and targeted improvements.

# BEFORE YOU WRITE FINDINGS — dedup check

Review the DEDUP list and Project Context above. For each finding you're about to propose,
ask: "is this genuinely new work, or does an active/completed/queued task already cover it?"
Different approaches to the same area ARE valid (e.g. "add rate limiting to webhooks" vs
"add error handling to webhooks"). Only skip a finding if a prior task solves the same problem.
When in doubt, keep the finding — false negatives (missing good work) are worse than duplicates.

# OUTPUT FORMAT

Write findings to /tmp/research_findings_${taskId}.json. Output the JSON array as the last thing in your response.

ALSO write /tmp/research_progress_${taskId}.md — a concise markdown summary (under ~3000 chars) of your investigation: which areas/directories you explored, which files you read, key observations and patterns discovered, and any remaining gaps or open questions. This is surfaced to retry agents if this task fails, so they can resume instead of re-investigating from scratch.

## Finding types

**Implementation findings** — these are your primary output. Each one spawns an agent that writes code:
  feature, improvement, bug, refactor, perf, security_audit, qa, deps, docs, migrate, test

**Sub-research** — spawns another research cycle (use sparingly):
  research

**Metadata** — these update the system's knowledge but do not produce code changes:
  direction_note, direction_update, direction_proposal, design_doc_observation

Metadata is useful context for the system, but it is not work product. A research cycle that
outputs only metadata has not found anything to build — keep looking. Your goal is to produce
implementation findings. Add metadata alongside them when relevant.

\`\`\`json
[{
  "type": "<see finding types above>",
  "title": "feat: short title (under 60 chars, conventional commit style)",
  "description": "1-3 sentences: what and why",
  "prompt": "Self-contained prompt for the implementing agent. Describe the problem and desired outcome — NOT the implementation. No line numbers, no function signatures, no step-by-step instructions.",
  "scope": "small|medium|large",
  "risk": "low|medium|high",
  "priority": "high|medium|low",
  "related_files": ["path/to/file.py"],
  "depends_on_indices": [],
  "direction_id": "d-xxx or null",
  "model": "claude-opus-4-7|null",
  "effort": "high|medium|low|null"
}]
\`\`\`

Scope: small (<50 lines, 1 file), medium (2-5 files, 50-200 lines), large (cross-component, 200+ lines).
Risk: low (cosmetic), medium (logic/endpoints), high (core logic, auth, data models).
Routing: large scope → plan agent (decomposition), medium → create agent (single PR), research → sub-research.

- Use scope=large for cross-component work — the pipeline has a plan agent for safe decomposition. But most product features are scope=small or scope=medium.
- direction_update: suggest rewording/reprioritizing a direction (fields: direction_id, suggested_text, suggested_priority, suggested_paths, description). Max 1 per cycle.
- direction_note: add context to a direction (fields: direction_id, description max 2000 chars). Be honest about progress.
- direction_proposal: propose a new standing direction (max 2 per cycle, held for user approval).
- design_doc_observation: flag an architectural observation for the design document (fields: description, section). Use when you discover something about the system's architecture — module patterns, missing capabilities, structural risks. These are reviewed by the architecture agent, not acted on directly. Max 3 per cycle.
- Optional: /tmp/research_insights_${taskId}.json with [{"summary": "<200 chars", "area": "path"}] for observations not worth implementing.`;

  const researchPromptSuffix = payload.prompt_suffix ? `\n\nAdditional instructions:\n${strVal(payload.prompt_suffix)}` : '';
  statusRef.value = 'Running codebase research...';
  const { exitCode, allOutput, lastResultText } = await runClaudeCode(researchPrompt + researchPromptSuffix + researchFirstTaskSection + PLAYBOOK_GUIDANCE + '\nNote: If your research uncovered operational knowledge (build patterns, module relationships, common failure modes), save the most reusable findings to the playbook.\n' + `\n\n${DEFAULT_TOOL_GUIDANCE}`, ws, statusRef, model, effort, disallowedTools);

  // Prefer reading findings from /tmp/ file (avoids output truncation), fall back to output parsing
  const researchFindings = readHandlerOutput(taskId, 'research_findings', ws);
  let logs = researchFindings !== null ? sanitize(JSON.stringify(researchFindings)) : '';
  if (!logs) {
    if (lastResultText && lastResultText.includes('"prompt"')) {
      logs = sanitize(lastResultText);
    } else {
      const findingsArr = extractJsonArray(allOutput, 'prompt');
      logs = sanitize(findingsArr ? JSON.stringify(findingsArr) : (lastResultText || allOutput.slice(-50_000)));
    }
  }

  const researchProgress = readProgressArtifact(taskId, 'research');
  try {
    await persistAndPostStatus(taskId, {
      task_id: taskId,
      status: exitCode === 0 ? 'COMPLETED' : 'FAILED',
      ...(exitCode !== 0 ? { artifact_type: 'ERROR_LOG' } : { artifact_type: 'RESEARCH_FINDINGS' }),
      logs,
      ...(exitCode !== 0 ? { failure_category: 'claude_code_failed' } : {}),
      ...(researchProgress ? { analysis_plan: researchProgress.slice(0, 5000) } : {}),
    });
  } catch (e) {
    sendLog(ws, `[ERROR] Failed to post research completion status: ${e}\n`);
  }
}
