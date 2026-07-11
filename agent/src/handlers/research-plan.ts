import fs from 'fs';
import { execFileSync } from 'child_process';
/**
 * Research and planning handlers — exploratory analysis and implementation planning.
 *
 * - **handleResearch**: Analyze a codebase area (specified by focus, root, or file
 *   list) and produce research findings. Clones repo, runs Claude Code analysis,
 *   captures findings, and stores them as a ResearchFinding artifact.
 *
 * - **handlePlan**: Generate an implementation plan for a feature or refactor.
 *   Clones repo, analyzes code structure, produces a structured plan with
 *   milestones and file changes, stores as PlanFinding artifact. Does not edit code.
 *
 * These handlers are reconnaissance tasks: they gather information and produce
 * structured analysis artifacts that downstream create/fix/refactor tasks consume.
 * Research feeds strategic cycles; plans guide implementation tasks.
 */

import type WebSocket from 'ws';
import {
  WORKSPACE, strVal, sanitize, sendLog, readHandlerOutput,
  persistAndPostStatus, runClaudeCode,
  getProviderRunner,
  resolveBaseBranch,
} from '../index.js';
import * as indexMod from '../index.js';
import { prepareWorkspaceForRole } from '../workspacePrep.js';
import { DEFAULT_CLONE_DEPTH } from '../constants.js';
import {
  describeJsonExtractionResult,
  extractJsonArrayDetailed,
} from '../json-extract.js';
import {
  PLAYBOOK_GUIDANCE, FIRST_TASK_GUIDANCE, DEFAULT_TOOL_GUIDANCE, READ_ONLY_BASH_WRITE_NOTE, readClaudeMd, buildContextPrefix, buildFileTree, isGreenfieldRepo, verifyCloneDelivered, configureAgentPermissions,
  writePlaybookHelper, writeMessagesHelper, writeDesignDocHelper, formatPlaybook, buildRetryContext, type PlaybookEntry,
} from '../prompt.js';
import { parseBackendTimestamp } from '../utils/time.js';
import { reconcileSystemEntry, reconcileMissingCapabilities } from '../utils/design-doc-presence.js';
import {
  formatCochangeHints,
  formatDecompositionOutcomes,
} from './research-direction-context.js';
import { renderDirection } from './direction-renderer.js';
import {
  writeFetchHelper, buildFetchGuidance, RESEARCH_FETCH_VERBS, PLAN_FETCH_VERBS,
} from './strategic-helpers.js';
import { fetchPriorKnowledge, buildPriorKnowledgeSection, filterKnowledgeDuplicates } from './prior-knowledge.js';
import { buildAvailableCapabilitiesSection, buildCapabilityVerificationSection, buildEmptyRepoGuidance } from './research-plan-sections.js';
import { repairStructuredOutput } from './structured-output-repair.js';
import { resolveBuildConfig } from '../build-config.js';
import { providerAutoInjectsClaudeMd } from '../providers/runners.js';
import { resolveMigrationLayout } from './specialized.js';
import { setPromptSizeTelemetry, buildPromptSizeTelemetry } from '../telemetry.js';

export const PR_INELIGIBLE_TASK_TYPES = [
  'compute_experiment', 'debug', 'design', 'design_doc_update',
  'investigate', 'memo', 'panel_review', 'perf', 'plan',
  'release_audit', 'release_audit_challenge', 'release_merge_grade',
  'repo_bootstrap', 'research', 'review', 'security_audit',
  'self_audit', 'smoketest', 'strategic_review', 'verify_tests',
  'web_research',
] as const;
export {
  formatCochangeHints,
  formatDecompositionOutcomes,
  formatPreviousDecisionOutcomes,
  formatPastRetirementCritiques,
  formatRetiredDirectionsSummary,
} from './research-direction-context.js';
import {
  computeResearchPlanPromptFingerprint,
} from '../prompts/thinker.js';
export { buildThinkerPrompt, runThinkerWithGate, type ThinkerPromptParams } from '../prompts/thinker.js';
import { ACCEPTANCE_EVIDENCE_RUBRIC, CAPABILITY_DECLARATION_DOC } from './research-plan-prompt-blocks.js';
export { CAPABILITY_DECLARATION_DOC } from './research-plan-prompt-blocks.js';

// ── Pure data transformation helpers (exported for testing) ─────────────────

export function calcMessageAge(m: Record<string, unknown>): string {
  if (typeof m.created_at !== 'string') return '?';
  const d = parseBackendTimestamp(m.created_at);
  if (!d) return '?';
  const ms = Date.now() - d.getTime();
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

  // Conversion rate from last 3 cycles: findings that became merged PRs.
  // Exclude task types that can never open a PR (knowledge/compute outputs)
  // from the denominator so the rate reflects PR-eligible tasks only.
  // Older entries without type_breakdown contribute 0 ineligible (safe default).
  const prIneligibleTypes: ReadonlySet<string> = new Set(PR_INELIGIBLE_TASK_TYPES);
  const recent = history.slice(-3);
  const recentTasksCreated = recent.reduce((s, h) => s + ((h.tasks_created as number) || 0), 0);
  const recentIneligible = recent.reduce((s, h) => {
    const tb = h.type_breakdown as Record<string, {created?: number} | undefined> | undefined;
    if (!tb || typeof tb !== 'object') return s;
    let n = 0;
    for (const [k, v] of Object.entries(tb)) {
      if (prIneligibleTypes.has(k) && v && typeof v.created === 'number') n += v.created;
    }
    return s + n;
  }, 0);
  const recentEligible = recentTasksCreated - recentIneligible;
  if (recentEligible > 0) {
    const recentMerged = recent.reduce((s, h) => s + ((h.prs_merged as number) || 0), 0);
    const pct = Math.round((recentMerged / recentEligible) * 100);
    const cycleLabel = recent.length === 1 ? '1 cycle' : `${recent.length} cycles`;
    parts.push(`conversion: ${pct}% (${recentMerged}/${recentEligible} findings merged last ${cycleLabel})`);
  }

  // Dedup/filter losses across last 3 cycles — lets the agent see how many
  // proposed findings were filtered and by which mechanism, so it can steer
  // toward more novel titles, different files, or new approaches.
  const filterKeys = ['hash_dedup', 'word_overlap_dedup', 'file_overlap_dedup', 'type_filtered', 'empty_prompt'] as const;
  const filterAgg: Record<string, number> = {};
  for (const h of recent) {
    const fb = h.filter_breakdown as Record<string, number> | undefined;
    if (!fb || typeof fb !== 'object') continue;
    for (const k of filterKeys) {
      const v = (fb[k]) || 0;
      if (v > 0) filterAgg[k] = (filterAgg[k] || 0) + v;
    }
  }
  const filterTotal = Object.values(filterAgg).reduce((s, v) => s + v, 0);
  if (filterTotal > 0) {
    const shortKey: Record<string, string> = {
      hash_dedup: 'hash', word_overlap_dedup: 'word-overlap',
      file_overlap_dedup: 'file-overlap', type_filtered: 'type', empty_prompt: 'empty',
    };
    const segs = filterKeys.filter(k => filterAgg[k]).map(k => `${shortKey[k]} ${filterAgg[k]}`);
    parts.push(`filtered: ${filterTotal} (${segs.join(', ')})`);
  }

  // Salvage activity across last 3 cycles — surfaced separately from filters
  // because salvage captures findings for consolidated action rather than
  // discarding them. This tells the agent that findings dropped by dedup/decay
  // gates were durably captured and bundled.
  let salvageCount = 0;
  let salvageCycles = 0;
  for (const h of recent) {
    const s = h.salvage as { salvaged_count?: number } | undefined;
    if (s && typeof s.salvaged_count === 'number' && s.salvaged_count > 0) {
      salvageCount += s.salvaged_count;
      salvageCycles++;
    }
  }
  if (salvageCount > 0) {
    const cycleLabel = salvageCycles === 1 ? '1 cycle' : `${salvageCycles} cycles`;
    parts.push(`${salvageCount} findings salvaged across ${cycleLabel} — dropped by dedup/decay gates and bundled for consolidated action`);
  }

  // Per-scope (finding-type) success rates across last 3 cycles — surfaces
  // whether certain scopes/types land more reliably than others.
  const typeAgg: Record<string, {created: number; merged: number}> = {};
  for (const h of recent) {
    const sb = h.scope_breakdown as Record<string, {created?: number; merged?: number} | undefined> | undefined;
    if (!sb || typeof sb !== 'object') continue;
    for (const [k, v] of Object.entries(sb)) {
      if (!v || typeof v !== 'object') continue;
      const agg = typeAgg[k] ?? (typeAgg[k] = { created: 0, merged: 0 });
      agg.created += (v.created ?? 0);
      agg.merged += (v.merged ?? 0);
    }
  }
  const typeKeys = Object.keys(typeAgg).filter(k => typeAgg[k].created > 0);
  if (typeKeys.length > 1) {
    const order = ['small', 'medium', 'large'];
    typeKeys.sort((a, b) => {
      const ai = order.indexOf(a); const bi = order.indexOf(b);
      if (ai !== -1 && bi !== -1) return ai - bi;
      if (ai !== -1) return -1;
      if (bi !== -1) return 1;
      return a.localeCompare(b);
    });
    const segs = typeKeys.map(k => {
      const { merged, created } = typeAgg[k];
      return `${k} ${Math.round((merged / created) * 100)}% (${merged}/${created})`;
    });
    parts.push(`by type: ${segs.join(', ')}`);
  }

  return `Stats: ${parts.join(', ')}`;
}


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

// Mirrors the backend's plan-child allowlist: PLAN_SUBTASK_TYPES (the
// PR-producing implementation subtypes) ∪ PLAN_KNOWLEDGE_SUBTASK_TYPES (the
// knowledge-prerequisite subtypes) in backend/app/task_capabilities.py. Keep in
// sync when registering a new plan-spawnable type.
const PLAN_CHILD_TYPE_VOCABULARY = new Set([
  'create', 'refactor', 'migrate', 'qa',
  'web_research', 'design', 'compute_experiment',
]);

// The plan prompt asks for conventional-commit TITLES ("fix: ...") while the
// backend's plan-child registry has no "fix" TYPE (a plan child cannot carry
// the pre-existing PR context a fix task needs) — models conflate the two, and
// a single off-vocabulary type used to FAIL the whole plan downstream when a
// later subtask depended on the skipped producer (plan_completion's
// unsatisfiable-dependency check). Coerce loudly to "create" — the documented
// default and the exact semantics of a fresh plan child — instead of shipping
// a plan the backend must reject. Non-JSON or schema-less input passes through
// untouched (the raw-output fallback path; the backend owns parsing it).
export function normalizePlanSubtaskTypes(planJson: string, log?: (msg: string) => void): string {
  let parsed: unknown;
  try {
    parsed = JSON.parse(planJson);
  } catch {
    return planJson;
  }
  if (!parsed || typeof parsed !== 'object' || !Array.isArray((parsed as Record<string, unknown>).subtasks)) {
    return planJson;
  }
  const plan = parsed as Record<string, unknown> & { subtasks: unknown[] };
  let changed = false;
  for (const sub of plan.subtasks) {
    if (!sub || typeof sub !== 'object' || Array.isArray(sub)) continue;
    const subtask = sub as Record<string, unknown>;
    const rawType = subtask.type;
    if (rawType === undefined || rawType === null) continue; // backend defaults to "create"
    if (typeof rawType === 'string' && PLAN_CHILD_TYPE_VOCABULARY.has(rawType)) continue;
    const titleStr = typeof subtask.title === 'string' ? subtask.title : '';
    log?.(`[PLAN] Subtask ${JSON.stringify(titleStr)} requested off-vocabulary type ${JSON.stringify(rawType)} — coercing to "create" so the plan stays spawnable\n`);
    subtask.type = 'create';
    changed = true;
  }
  return changed ? JSON.stringify(plan) : planJson;
}

// ── Handler: Plan (decompose complex task into subtasks) ───────────────────
export async function handlePlan(
  taskId: string, payload: Record<string, unknown>, task: Record<string, unknown>,
  ghToken: string, owner: string, repo: string, ws: WebSocket | null, statusRef: { value: string },
  model?: string, effort?: string
) {
  const prompt = strVal(payload.prompt) || strVal(payload.description) || strVal(task.description);
  const baseBranch = resolveBaseBranch(payload, task);
  const planPromptSuffix = payload.prompt_suffix ? `\n\nAdditional instructions:\n${strVal(payload.prompt_suffix)}` : '';

  if (!prompt) throw new Error('Plan task missing prompt');

  // Clone repo — planner needs codebase context for good decomposition
  statusRef.value = `Cloning ${owner}/${repo} for planning...`;
  sendLog(ws, `Planning task decomposition for: ${prompt.slice(0, 200)}\n`);
  const cloneUrl = `https://x-access-token:${ghToken}@github.com/${owner}/${repo}.git`;
  await prepareWorkspaceForRole({
    workspace: WORKSPACE, repoUrl: cloneUrl, githubToken: ghToken,
    branch: baseBranch, baseBranch, depth: DEFAULT_CLONE_DEPTH,
  });
  // Positively confirm the clone delivered the expected tree before any
  // greenfield conclusion is drawn below — a successful-but-empty clone of a
  // repo the durable signal says is NOT empty must fail loudly, not be
  // misread as a greenfield bootstrap.
  verifyCloneDelivered(task, baseBranch);
  writePlaybookHelper(taskId);
  writeMessagesHelper(taskId);
  // The plan prompt advertises `./design-doc get <key>` for architectural
  // grounding; provision the read-only helper so that contract is honest.
  writeDesignDocHelper(taskId, WORKSPACE, { readOnly: true });
  // Provision the read-only ./fetch helper (direction|section|playbook) so the
  // planner can resolve refs trimmed from its launch snapshot. project_id is set
  // for all task types by the backend; if it is missing we skip BOTH the helper
  // and its prompt advertisement so we never promise a helper we didn't write.
  const planProjectId = ((task.project_metadata as Record<string, unknown> | undefined)?.project_id as string | undefined) ?? '';
  if (planProjectId) writeFetchHelper(taskId, planProjectId, WORKSPACE, { verbs: PLAN_FETCH_VERBS });
  const planFetchGuidance = planProjectId ? buildFetchGuidance(PLAN_FETCH_VERBS) : '';
  const disallowedTools = configureAgentPermissions('plan');

  // ATOMIC SCHEMA RULE guidance — when this project configures a migration
  // layout (build_config.migration, or any python directory), name the resolved
  // model/migration/schema-revision paths so the planner keeps the model edit
  // and its migration in ONE subtask. With no migration config we fall back to
  // a stack-agnostic statement (don't hardcode any one project's paths).
  const planBuildConfig = resolveBuildConfig(task);
  const planHasMigrationLayout =
    !!planBuildConfig?.migration ||
    (planBuildConfig?.directories ?? []).some(d => d.type === 'python');
  let atomicSchemaPathsNote = '';
  if (planHasMigrationLayout) {
    const planMigrationLayout = resolveMigrationLayout(planBuildConfig);
    const revisionTarget = planMigrationLayout.schemaRevisionFile
      ? ` and ${planMigrationLayout.schemaRevisionFile} for ${planMigrationLayout.schemaRevisionConst}`
      : '';
    atomicSchemaPathsNote = ` Set the subtask's type="migrate" and include in its related_files the model file(s) under ${planMigrationLayout.modelsDir}, the new migration file under ${planMigrationLayout.versionsDir}${revisionTarget}.`;
  }
  const promptFingerprint = computeResearchPlanPromptFingerprint('plan');
  const setPromptFingerprint = (indexMod as { setCurrentPromptFingerprint?: (fp: string) => void }).setCurrentPromptFingerprint;
  if (setPromptFingerprint) setPromptFingerprint(promptFingerprint);
  const sendHeartbeat = (indexMod as { sendHeartbeatSync?: (taskId: string, status: string, extra?: Record<string, unknown>) => void }).sendHeartbeatSync;

  const contextPrefix = buildContextPrefix(task);

  const claudeMd = readClaudeMd();
  const claudeMdPrefix = (claudeMd && !providerAutoInjectsClaudeMd()) ? `## Codebase Instructions\n${claudeMd}\n\n` : '';
  const planPlaybookSection = formatPlaybook(task.playbook as Record<string, PlaybookEntry | string> | undefined);
  const planRetryContext = buildRetryContext(task);
  const planFirstTaskSection = task.first_task_on_repo ? FIRST_TASK_GUIDANCE : '';
  const cochangeHintsSection = formatCochangeHints(task.direction_context as Record<string, unknown> | undefined);
  const decompositionOutcomesSection = formatDecompositionOutcomes(task.direction_context as Record<string, unknown> | undefined);
  // Shared capability vocabulary — the canonical registry names a subtask draws
  // its prerequisite / provides_capability refs from (additive; '' when absent).
  const planAvailableCapabilitiesSection = buildAvailableCapabilitiesSection(
    payload.available_capabilities,
  );

  // Greenfield mode: when the cloned tree has effectively no source (only
  // config/scaffolding), the plan agent must NOT treat absent code as "nothing
  // to decompose". Mirror the research agent's "if the codebase is nearly empty"
  // branch and decompose "build the foundation for <goal>" instead.
  const isGreenfield = isGreenfieldRepo(task);
  const greenfieldBranch = isGreenfield ? `
**GREENFIELD MODE — this repository is empty or near-empty (only config/scaffolding, no real source).**
Do NOT treat absent source as "nothing to decompose". This is a first-class bootstrap case: your job is to decompose **"build the foundation for the project goal"** into ordered implementation subtasks. Drive the foundation from the **Project Vision / goal above** and any design-doc specs — pick the structure, entry points, and core modules the goal requires, and order them so later subtasks build on earlier ones. An empty repo with a stated goal is NOT a valid no-op.

` : '';

  const planPrompt = `${claudeMdPrefix}${planPlaybookSection}${planRetryContext}${contextPrefix}${cochangeHintsSection}${decompositionOutcomesSection}You are a planning agent. Your job is to analyze this codebase and decompose the following task into concrete, ordered subtasks that can each be implemented as a single pull request.

Task to decompose:
${prompt}
${greenfieldBranch}
${planAvailableCapabilitiesSection}Instructions:
1. Explore the codebase thoroughly. Read key files, understand the architecture, find relevant patterns.${isGreenfield ? ' (On a greenfield repo there is little to read — lean on the project goal and design-doc specs to decide the foundation.)' : ''}
2. Decompose into substantial subtasks. Prefer fewer, larger subtasks over many tiny ones. A single subtask that modifies 200+ lines across 5+ files is fine — the implementing agent is capable. Only split when subtasks are genuinely independent or have different risk profiles.
3. Identify dependencies between subtasks — set depends_on_indices so that dependencies flow forward. See BUILD-ON ordering rule below for detailed guidance on when to add (and when to omit) ordering edges.
4. Write clear, self-contained prompts for each subtask. Describe the PROBLEM and desired outcome — do not prescribe exact line numbers. The implementing agent has full repo access and will figure out the details.

Output EXACTLY one JSON block as the VERY LAST thing in your response:

{
  "subtasks": [
    {
      "title": "Short title (under 60 chars, conventional commit style: feat:, fix:, refactor:, etc.)",
      "prompt": "Self-contained implementation prompt. Describe WHAT to achieve and key constraints. The agent has full repo access and will figure out implementation details.",
      "depends_on_indices": [],
      "related_files": ["src/main.py", "src/utils.py"],
      "effort": "low | medium | high | null (inherit from parent)",
      "scope": "medium",
      "risk": "low",
      "type": "create",
      "approval_reason": "Only set when risk=high — brief reason why human approval is needed (omit otherwise)",
      "acceptance_evidence": { "kind": "named_test", "test_path": "testing/test_foo.py", "test_id": "test_bar" },
      "prerequisite": null,
      "provides_capability": null
    },
    {
      "title": "feat: second subtask",
      "prompt": "...",
      "depends_on_indices": [0],
      "related_files": ["src/main.py"],
      "effort": null,
      "scope": "large",
      "risk": "medium",
      "type": "create"
    }
  ]
}

Affirmative no-op (nothing to decompose):
If — and ONLY if — your exploration shows there is genuinely nothing to build (the requested work is ALREADY DONE on this branch, or the task is not actionable as code subtasks), do NOT emit a silent empty plan and do NOT invent filler subtasks. Instead emit this affirmative no-op sentinel as the LAST JSON block:

{
  "subtasks": [],
  "no_op_reason": "Specific, verified reason (at least 30 chars) naming exactly what is already present / why no code subtasks are needed — name the concrete files, symbols, or sibling work you verified already implement the requested change."
}

The no_op_reason MUST be at least 30 characters and name the concrete evidence you verified. This is the ONLY clean way for a plan to complete with zero subtasks — an empty "subtasks" array WITHOUT a specific no_op_reason is treated as a failed plan. This is NOT a give-up path: only use it when you have actually VERIFIED the work is already done or genuinely needs no code change. If you simply could not figure out how to decompose the task, that is a real plan failure — let the agent exit non-zero rather than emitting a bogus no_op_reason.${isGreenfield ? `

GREENFIELD CAVEAT: on this empty/near-empty repo, "there is no source code to change" or "the codebase is empty" is NOT a valid no_op_reason. Absent source is exactly the signal to bootstrap — decompose "build the foundation for the goal" into ordered subtasks driven by the Project Vision / goal and any design-doc specs. Only emit a no-op here if the foundation the goal requires already exists on this branch.` : ''}

Risk guidance:
- risk=high: changes to auth/secrets handling, data-model or schema changes, database migrations, core control/orchestration loops, infrastructure provisioning, any change that if wrong could cause data loss or a security breach
- risk=medium: new or changed public API surfaces (endpoints, request/response shapes), frontend state management, cross-process or protocol changes
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
- BUILD-ON ordering (critical): if a subtask CONSUMES code or artifacts another subtask produces — it imports a module/file the other creates, calls a new function/class it adds, or builds a feature on top of its output — set depends_on_indices so the consumer comes AFTER the producer, EVEN IF the two touch DIFFERENT files. Sibling subtasks run concurrently against a clone of the same base; if the producer has not merged, the consumer's clone is missing the new code and it will fail to import it or re-create a conflicting stub. Genuinely independent subtasks (including small additive edits to shared infra) MUST stay parallel — only order true producer→consumer relationships, and make the consumer's related_files/prompt name the producer's file so the ordering is explicit.
- Aim for 1-4 subtasks. Prefer 1-2 large subtasks over 4-6 small ones. If the task can be one PR, output a single subtask.
- Do NOT include review, testing, or deployment subtasks — those are handled automatically by the pipeline
- Use effort=high only for complex architectural subtasks. Leave null for simple additive changes — the orchestrator resolves the model from provider routing.
- scope: "small" for changes under ~100 lines across 1-3 files with clear, narrow scope, "medium" for changes spanning 3-8 files or 100-300 lines, or touching shared infrastructure, "large" for cross-component architectural changes, 300+ lines, or changes to auth/data models/core orchestration
- The "type" field controls how the subtask is executed: "create" (default) for new features/additions, "refactor" for code quality improvements with no behavior change, "migrate" for database schema changes (Alembic migrations), "qa" for test-only PRs. The type field must be EXACTLY one of those values (or a knowledge-prerequisite type below). "fix" is NOT a valid subtask type — a bug-fixing subtask is type "create" even when its TITLE uses the "fix:" conventional-commit prefix (titles and types are independent; a plan child always starts from a fresh branch, which is what "create" means).
- EMPIRICAL PREREQUISITE (optional, additive): besides the implementation subtypes above, a plan MAY emit a measurement prerequisite subtask of type="compute_experiment" when a later subtask should "measure this first, then act on the verdict". It does NOT produce a PR — it launches a budgeted, auto-torn-down compute job and persists a durable MEASURED VERDICT as knowledge. It carries a launch contract: "command" (the experiment to run), a positive "spend_cap_usd", a positive "deadline_seconds", and an OPTIONAL "success_criterion": {"metric", "operator", "threshold"}. A later implementation subtask consumes the verdict by naming this subtask's index in its depends_on_indices — it is handed the experiment's knowledge at launch. A MEASURED NEGATIVE result (success_criterion honestly missed) is still consumable evidence: the dependent still runs and acts on the verdict. Only a BROKEN run that produced no usable verdict cancels the dependent follow-on work. REQUIRED: every compute_experiment subtask must include a "smoke_sample" field proving the command was tested on this VM before writing the plan. Run: BPRF=$(mktemp); timeout 30 bash -c "BLUEPRINT_RESULT_FILE=\\"$BPRF\\" <your command>" 2>/tmp/ce_smoke_err.txt; CE_EXIT=$?; then record {"exit_code":CE_EXIT,"startup_ok":true/false,"metric_captured":true/false,"timed_out":(CE_EXIT==124),"error_excerpt":"<stderr snippet>"}. Nothing beyond the repo itself is guaranteed; your smoke is proof. If startup_ok=false, fix or drop the subtask. If genuinely un-smokable (special hardware required), set smoke_sample={"unsmokable":true,"unsmokable_reason":"<why>"} for explicit operator review.
- acceptance_evidence (OPTIONAL, additive — omit when you have nothing substantive to declare): the concrete evidence that will prove THIS subtask's claim true, carried forward to the implementing agent and the evaluation lane. ${ACCEPTANCE_EVIDENCE_RUBRIC} Declare it only for subtasks where the success condition is concrete; never invent a placeholder/always-true declaration (those are rejected).
- prerequisite (OPTIONAL, default null): a FOUNDATION this subtask depends on that is built OUTSIDE this plan (another lane / an already-built capability). Shape: \`{"kind": "task", "ref": "<task-id or finding-hash>"}\` or \`{"kind": "capability", "ref": "<capability-name>"}\`. For a capability ref, DRAW THE NAME from the AVAILABLE CAPABILITIES vocabulary above so it resolves to whichever task advertises it (the matcher is EXACT). Use depends_on_indices — NOT prerequisite — to order subtasks WITHIN this plan; prerequisite is only for cross-lane/cross-plan foundations.
- provides_capability (OPTIONAL, default null): a short kebab-case capability name THIS subtask advertises so another lane's prerequisite \`{"kind": "capability", "ref": "<name>"}\` can depend on it. DRAW THE NAME from the AVAILABLE CAPABILITIES vocabulary above when one fits; propose a NEW kebab-case name (same convention) ONLY for a genuinely new capability. ${CAPABILITY_DECLARATION_DOC}
- ATOMIC SCHEMA RULE: a database schema change is ONE subtask, never split across dependent subtasks. If a subtask edits the data model (adds/changes tables, columns, or model definitions), that SAME subtask must also generate the matching migration and bump the schema revision. Do NOT emit a separate dependent type="migrate" subtask for the migration of a model change in another subtask — splitting the model edit from its migration manufactures dead serialization and ships a model PR with no migration (which reviewers reject).${atomicSchemaPathsNote} The migration agent will handle migration generation and the revision bump within that single subtask/PR. Only create a separate subtask when the other work is genuinely independent of the schema change — never to carry the migration for it.
- If any subtask adds new user-facing functionality, consider whether a test subtask is needed. New API endpoints should have corresponding integration tests. New agent handlers should have at least one smoke test. Add a test subtask with depends_on_indices pointing to the implementation subtask.
- If any subtask changes a public API interface (endpoint signatures, request/response shapes, TypeScript types exported to frontend), include explicit notes in the prompt about backward compatibility requirements.
- IMPORTANT: Write your plan JSON to /tmp/plan_output_${taskId}.json ${READ_ONLY_BASH_WRITE_NOTE} (NOT in the repository). The file should contain ONLY the JSON object, no markdown wrapping.
- ALSO write /tmp/plan_progress_${taskId}.md ${READ_ONLY_BASH_WRITE_NOTE} — a concise markdown summary (under ~3000 chars) of your decomposition: which areas of the codebase you explored, the rationale for how you split the work, the file targets and key constraints per subtask, and any risks or open questions. This artifact is surfaced to retry agents if this task fails, so they can pick up without re-investigating.${planPromptSuffix}${planFirstTaskSection}${PLAYBOOK_GUIDANCE}

${planFetchGuidance}${DEFAULT_TOOL_GUIDANCE}`;
  const planPromptTelemetry = buildPromptSizeTelemetry(planPrompt, [
    ['playbook', planPlaybookSection],
    ['retry_context', planRetryContext],
    ['context_prefix', contextPrefix],
    ['cochange_hints', cochangeHintsSection],
    ['decomposition_outcomes', decompositionOutcomesSection],
    ['claude_md_prefix', claudeMdPrefix],
    ['available_capabilities', planAvailableCapabilitiesSection],
    ['greenfield', greenfieldBranch],
  ]);
  setPromptSizeTelemetry(
    planPromptTelemetry.prompt_size_bytes,
    planPromptTelemetry.prompt_section_count,
    planPromptTelemetry.prompt_section_sizes,
  );
  if (sendHeartbeat) sendHeartbeat(taskId, statusRef.value, { prompt_fingerprint: promptFingerprint });

  statusRef.value = `Running ${getProviderRunner().displayName} for planning...`;
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
  const planProgress = readProgressArtifact(taskId, 'plan');
  if (!logs) {
    const priorAnalysisSection = planProgress ? `${planProgress.slice(-8000)}\n\n` : '';
    const repair = await repairStructuredOutput<Record<string, unknown>>({
      mode: 'object',
      requiredKeys: ['subtasks'],
      schemaLabel: 'research_plan',
      first: { exitCode, allOutput, lastResultText },
      reopenPrompt: `${priorAnalysisSection}You did not emit the plan JSON in the required schema. Output ONLY the JSON object now — no markdown, no explanation. Required field: subtasks (array). Schema:\n\n{"subtasks":[{"title":"...","prompt":"...","type":"create","scope":"medium","related_files":["path/to/file.ts"],"depends_on_indices":[]}]}\n\nOutput ONLY the JSON object.`,
      ws,
      statusRef,
      model,
      effort,
      disallowedTools,
      retryStatus: 'Retrying plan JSON extraction...',
    });
    if (repair.value !== null) {
      logs = sanitize(JSON.stringify(repair.value));
    } else {
      logs = sanitize(lastResultText || allOutput.slice(-50_000));
    }
  }

  // Coerce off-vocabulary subtask types (notably "fix") to "create" before the
  // backend parses the plan — see normalizePlanSubtaskTypes for why.
  logs = normalizePlanSubtaskTypes(logs, (m) => { sendLog(ws, m); });

  // Backend's _handle_plan_completion parses `logs` on COMPLETED and creates
  // children with the correct plan branch. No agent-side spawn is needed —
  // that path returned empty task_ids by design (deferred to post-branch
  // creation) and misreading it as failure silently dropped valid plans.

  const finalStatus = exitCode === 0 ? 'COMPLETED' : 'FAILED';
  try {
    await persistAndPostStatus(taskId, {
      task_id: taskId,
      status: finalStatus,
      ...(finalStatus === 'FAILED' ? { artifact_type: 'ERROR_LOG' } : {}),
      logs,
      ...(finalStatus === 'FAILED' ? { failure_category: 'claude_code_failed' } : {}),
      ...(planProgress ? { analysis_plan: planProgress.slice(0, 5000) } : {}),
    });
  } catch (e) {
    sendLog(ws, `[ERROR] Failed to post plan completion status: ${String(e)}\n`);
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
  const baseBranch = resolveBaseBranch(payload, task);
  const userPrompt = strVal(payload.prompt) || strVal(payload.description) || strVal(task.description);

  // Full clone — research needs history context
  statusRef.value = `Cloning ${owner}/${repo} for research...`;
  sendLog(ws, `Starting codebase research...\n`);
  const cloneUrl = `https://x-access-token:${ghToken}@github.com/${owner}/${repo}.git`;
  // research uses a FULL clone (no --depth) so it can mine git history.
  // We pass depth=0 to signal full-clone semantics in the cold path; the
  // helper interprets a non-positive depth as "omit --depth".
  await prepareWorkspaceForRole({
    workspace: WORKSPACE, repoUrl: cloneUrl, githubToken: ghToken,
    branch: baseBranch, baseBranch, depth: 0,
  });
  // Positively confirm the clone delivered the expected tree before the
  // greenfield/empty-repo guidance gate runs — a successful-but-empty clone of
  // a repo the durable signal says is NOT empty must fail loudly, not be
  // misread as greenfield.
  verifyCloneDelivered(task, baseBranch);
  writePlaybookHelper(taskId);
  writeMessagesHelper(taskId);
  // The research prompt advertises `./design-doc get <key>` for architectural
  // grounding; provision the read-only helper so that contract is honest.
  writeDesignDocHelper(taskId, WORKSPACE, { readOnly: true });
  // Provision the read-only ./fetch helper (direction|problem-model|gap) so the
  // research agent can resolve refs trimmed from its launch snapshot. project_id
  // is set for all task types by the backend; if missing we skip BOTH the helper
  // and its prompt advertisement so we never promise a helper we didn't write.
  const researchProjectId = ((task.project_metadata as Record<string, unknown> | undefined)?.project_id as string | undefined) ?? '';
  if (researchProjectId) writeFetchHelper(taskId, researchProjectId, WORKSPACE, { verbs: RESEARCH_FETCH_VERBS });
  const researchFetchGuidance = researchProjectId ? buildFetchGuidance(RESEARCH_FETCH_VERBS) : '';
  const researchMode = strVal(payload.research_mode, 'research');
  const disallowedTools = configureAgentPermissions(researchMode);
  const promptFingerprint = computeResearchPlanPromptFingerprint('research');
  const setPromptFingerprint = (indexMod as { setCurrentPromptFingerprint?: (fp: string) => void }).setCurrentPromptFingerprint;
  if (setPromptFingerprint) setPromptFingerprint(promptFingerprint);
  const sendHeartbeat = (indexMod as { sendHeartbeatSync?: (taskId: string, status: string, extra?: Record<string, unknown>) => void }).sendHeartbeatSync;

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
  const gitHistorySection = (gitLogOutput || gitDiffStatOutput) ? [
    gitLogOutput ? `## Recent Git History (last 30 commits)\n${gitLogOutput}` : '',
    gitShortlogOutput ? `## Top Contributors (last 20, no merges)\n${gitShortlogOutput}` : '',
    gitDiffStatOutput ? `## Recent Changes Summary (last 5 commits diff stat)\n${gitDiffStatOutput}` : '',
    hotspotsSection,
    '',
  ].filter(Boolean).join('\n\n') + '\n' : '';

  // Build codebase summary to reduce exploration overhead

  const claudeMd = readClaudeMd();
  const codebaseSummary = (claudeMd && !providerAutoInjectsClaudeMd())
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
    ? directions.map((d, i) => renderDirection(d, i, currentDirectionId, activeTasks, queuedTasks)).join('\n\n')
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

  // Pull the most relevant existing knowledge documents so research builds on
  // accumulated knowledge instead of re-deriving the same findings from scratch.
  // Query with the explicit research focus when present, else the current
  // direction's text. Fail-soft: an empty list yields no section and no failure.
  const currentDirectionText = directions.length > 0
    ? strVal((directions.find(d => d.id === currentDirectionId) ?? directions[0]).text)
    : '';
  const knowledgeIndexForFilter = Array.isArray(payload.knowledge_index)
    ? (payload.knowledge_index as Array<Record<string, unknown>>)
    : [];
  const knownKnowledgeIds = new Set<string>(
    knowledgeIndexForFilter.map(e => strVal(e.id)).filter(Boolean),
  );
  const priorDocs = filterKnowledgeDuplicates(
    await fetchPriorKnowledge(userPrompt || currentDirectionText),
    knownKnowledgeIds,
  );
  if (priorDocs.length > 0) {
    sendLog(ws, `Injecting ${priorDocs.length} prior knowledge document(s) into the research prompt\n`);
  }
  const priorKnowledgeSection = buildPriorKnowledgeSection(priorDocs);

  // Pipeline-wide scope conversion rates — rendered from pipeline_health.scope_conversion_rates
  // so measurement directions can read the number straight from the prompt instead
  // of hitting an admin endpoint they aren't authenticated for.
  let pipelineScopeRatesSection = '';
  const pipelineHealthForScope = projectCtx?.pipeline_health as Record<string, unknown> | null | undefined;
  const scopeConversionRates = pipelineHealthForScope?.scope_conversion_rates as
    | Record<string, { current_rate: number | null; prev_rate: number | null; delta: number | null; trend: string | null } | undefined>
    | null
    | undefined;
  if (scopeConversionRates && Object.keys(scopeConversionRates).length > 0) {
    const fmtRate = (r: number | null | undefined) => (typeof r === 'number' ? `${Math.round(r * 100)}%` : '—');
    const scopeOrder = ['small', 'medium', 'large'];
    const orderedKeys = [
      ...scopeOrder.filter(k => k in scopeConversionRates),
      ...Object.keys(scopeConversionRates).filter(k => !scopeOrder.includes(k)),
    ];
    const parts = orderedKeys.map(k => {
      const r = scopeConversionRates[k];
      const trend = r?.trend ? ` (${r.trend})` : '';
      return `${k} ${fmtRate(r?.current_rate)}/${fmtRate(r?.prev_rate)}${trend}`;
    });
    pipelineScopeRatesSection = `\nPipeline-wide scope conversion rates (current / prev 24h window): ${parts.join(', ')}\n`;
  }

  // Medium-scope pre/post investment measurement — rendered so measurement directions
  // can read the headline before/after numbers straight from the prompt.
  const mediumScopeMeasurement = pipelineHealthForScope?.medium_scope_measurement as
    | {
        medium_scope_conversion_rate: number | null;
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
      const headline = `${fmtPct(mediumScopeMeasurement.medium_scope_conversion_rate)} overall (${mediumScopeMeasurement.total_merged ?? 0}/${mediumScopeMeasurement.total_created ?? 0})`;
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

  const contextPrefix = buildContextPrefix(task, { suppressTaskLists: true });

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
    // Reconcile the claimed system status against the cloned file tree before
    // formatting — a present-status system whose backing modules are all gone
    // is downgraded to `missing` so the MISSION's "IMPROVE if partial/functional/
    // mature" branch never fires on code that does not exist.
    const sys = reconcileSystemEntry(dirDesignCtx.system as Record<string, unknown> | undefined, WORKSPACE);
    if (sys) {
      lines.push(`System: ${strVal(sys.name)} (${strVal(sys.id)}) — status: **${strVal(sys.status)}**`);
      if (sys.description) lines.push(`Description: ${strVal(sys.description)}`);
      if (sys.spec) {
        const spec = sys.spec as Record<string, unknown>;
        lines.push('Architectural spec (use this as your blueprint):');
        if (spec.approach) lines.push(`  Approach: ${strVal(spec.approach)}`);
        if (Array.isArray(spec.technology)) lines.push(`  Technology: ${(spec.technology as string[]).join(', ')}`);
        if (Array.isArray(spec.interfaces)) lines.push(`  Interfaces: ${(spec.interfaces as string[]).join('; ')}`);
        if (Array.isArray(spec.integration_points)) lines.push(`  Integration: ${(spec.integration_points as string[]).join('; ')}`);
        if (spec.notes) lines.push(`  Notes: ${strVal(spec.notes)}`);
      }
    } else {
      lines.push(`System ${strVal(dirDesignCtx.system_id)}: not yet documented in design doc`);
    }
    const dirVerif = dirDesignCtx.verification as Record<string, unknown> | undefined;
    if (dirVerif) {
      const vstatus = strVal(dirVerif.status);
      const missing = Array.isArray(dirVerif.missing) ? dirVerif.missing as string[] : [];
      const present = Array.isArray(dirVerif.present) ? dirVerif.present as string[] : [];
      if (vstatus === 'claimed_absent') {
        lines.push(`⚠️ CODE VERIFICATION: this system is CLAIMED-BUT-ABSENT — every declared module is missing from the integration branch. Treat its status as effectively missing/unverified and design from what actually exists. Missing: ${missing.join(', ')}`);
      } else if (vstatus === 'partial') {
        lines.push(`⚠️ CODE VERIFICATION: this system is PARTIAL — some declared modules are missing. Verify before relying on the claimed status. Missing: ${missing.join(', ')}; Present: ${present.join(', ')}`);
      } else if (vstatus === 'verified') {
        lines.push(`CODE VERIFICATION: all ${present.length} declared module(s) exist on the integration branch (status verified).`);
      }
    }
    const missingCaps = reconcileMissingCapabilities(
      dirDesignCtx.missing_capabilities as Record<string, Record<string, string>> | undefined,
      WORKSPACE,
    );
    if (missingCaps && Object.keys(missingCaps).length > 0) {
      lines.push('Missing/partial capabilities relevant to your direction:');
      for (const [name, cap] of Object.entries(missingCaps)) {
        lines.push(`  - ${name}: ${cap.status}${cap.notes ? ` (${cap.notes})` : ''}`);
      }
    }
    lines.push('');
    designDocHighlight = lines.join('\n') + '\n';
  }

  // Design doc: only include key listing + playbook keys index. Full sections available via ./design-doc get <key>.
  // Playbook content is already in researchPlaybookSection (task.playbook — capped, ranked selection).
  // Rendering it again from design_doc would double the context on every agentic turn.
  let designDocSection = '';
  if (tacticalDesignDoc && Object.keys(tacticalDesignDoc).length > 0) {
    const docKeys = Array.isArray(tacticalDesignDoc._keys)
      ? tacticalDesignDoc._keys as string[]
      : Object.keys(tacticalDesignDoc).filter(k => !k.startsWith('_') && !k.startsWith('playbook/'));
    const playbookKeys = Array.isArray(tacticalDesignDoc._playbook_keys)
      ? tacticalDesignDoc._playbook_keys as string[]
      : [];
    designDocSection = `# ARCHITECTURE (Design Document)
Available sections (use \`./design-doc get <key>\` to read): ${docKeys.join(', ')}

${playbookKeys.length > 0 ? `Playbook entries available (use \`./playbook get <key>\` to read full content): ${playbookKeys.join(', ')}\n` : ''}
`;
  }

  // System verification: backend checked each design-doc system's declared
  // module paths against the integration-branch file tree. Surface flagged
  // (claimed-but-absent / partial) systems prominently so tactical research
  // reasons about what actually exists instead of trusting a stale status.
  let systemVerificationSection = '';
  const sysVerif = payload.system_verification as Record<string, Record<string, unknown>> | undefined;
  if (sysVerif && Object.keys(sysVerif).length > 0) {
    const _syspm = sysVerif as Record<string, unknown>;
    const sysComputedAt = typeof _syspm['_computed_at'] === 'string' ? _syspm['_computed_at'] : null;
    const sysBranchSha = typeof _syspm['_branch_sha'] === 'string' ? _syspm['_branch_sha'] : null;
    const absent: string[] = [];
    const partial: string[] = [];
    const verified: string[] = [];
    for (const sysId of Object.keys(sysVerif).sort()) {
      if (sysId.startsWith('_')) continue;
      const v = sysVerif[sysId];
      const status = strVal(v.status);
      const missing = Array.isArray(v.missing) ? v.missing as string[] : [];
      const present = Array.isArray(v.present) ? v.present as string[] : [];
      const claimed = strVal(v.claimed_status);
      const claimedNote = claimed ? ` (design-doc status: ${claimed})` : '';
      if (status === 'claimed_absent') {
        absent.push(`- **${sysId}**${claimedNote}: CLAIMED-BUT-ABSENT — all ${missing.length} declared module(s) missing: ${missing.join(', ')}`);
      } else if (status === 'partial') {
        partial.push(`- **${sysId}**${claimedNote}: PARTIAL — missing: ${missing.join(', ')}; present: ${present.join(', ')}`);
      } else if (status === 'verified') {
        verified.push(`- **${sysId}**: verified — ${present.length} declared module(s) exist`);
      }
    }
    if (absent.length > 0 || partial.length > 0 || verified.length > 0) {
      const vlines: string[] = ['# SYSTEM VERIFICATION (code existence vs. design-doc claims)'];
      if (sysComputedAt) {
        const shaNote = sysBranchSha ? ` against ${sysBranchSha.slice(0, 12)}` : '';
        vlines.push(`_Verdicts computed ${sysComputedAt}${shaNote}. If this task was retried after a delay, the branch may have changed — treat older stamps as advisory._`);
      } else {
        vlines.push('_⚠️ STAMP MISSING — verdicts may be from an earlier run and could reflect a stale branch state._');
      }
      vlines.push('The backend verified each design-doc system\'s declared module paths against the integration-branch file tree. Do NOT trust a system\'s status over this evidence — if a system is flagged below, treat its claimed status as unreliable and design from what actually exists.');
      if (absent.length > 0) {
        vlines.push('\n## ⚠️ Claimed-but-absent (effectively missing — backing code does not exist)');
        vlines.push(...absent);
      }
      if (partial.length > 0) {
        vlines.push('\n## ⚠️ Partial (some declared modules missing)');
        vlines.push(...partial);
      }
      if (verified.length > 0) {
        vlines.push('\n## Verified (all declared modules present)');
        vlines.push(...verified);
      }
      systemVerificationSection = vlines.join('\n') + '\n\n';
    }
  }

  // Capability verification: the design-doc-system check above covers only
  // `systems/` entries. The backend now emits a parallel verdict map for
  // design-doc CAPABILITIES claimed working/partial — surface it the same way so
  // the research agent never proposes improving a capability whose backing code
  // is absent on the integration branch.
  const capabilityVerificationSection = buildCapabilityVerificationSection(
    payload.capability_verification as Record<string, Record<string, unknown>> | undefined,
  );
  // Shared capability vocabulary — the canonical registry names a finding draws
  // its prerequisite / provides_capability refs from (additive; '' when absent).
  const researchAvailableCapabilitiesSection = buildAvailableCapabilitiesSection(
    payload.available_capabilities,
  );

  // Local-GPU-runner availability — thread the project's live operator-owned
  // runner status into compute_experiment authoring so the model doesn't burn
  // billable cloud GPU when the operator's machine is offline. The backend ships
  // `local_runner_status` (top-level on the strategic payload, same shape as the
  // other payload fields read above) ONLY when the project has ever registered a
  // runner; when absent there is no local hardware → append nothing (author for
  // cloud normally).
  let localRunnerNote = '';
  const localRunnerStatus = payload.local_runner_status as
    { ever_registered?: boolean; active_total?: number; active_gpu?: number } | undefined;
  if (localRunnerStatus && typeof localRunnerStatus === 'object' && !Array.isArray(localRunnerStatus)) {
    const activeGpu = typeof localRunnerStatus.active_gpu === 'number' ? localRunnerStatus.active_gpu : 0;
    if (activeGpu > 0) {
      localRunnerNote = `\n    Local GPU available: ${activeGpu} live GPU-capable runner(s) registered for this project. GPU compute_experiments will run on operator-owned hardware (free) rather than billable cloud GPU — prefer authoring GPU experiments now while a runner is live.`;
    } else if (localRunnerStatus.ever_registered) {
      localRunnerNote = `\n    This project relies on operator-owned local GPU hardware which is CURRENTLY OFFLINE (0 live GPU runners). Do NOT author GPU compute_experiment findings right now — with no live runner they fall back to BILLABLE cloud GPU. Defer GPU experiments until a runner is live (or author CPU-only work).`;
    }
  }

  const researchRetryContext = buildRetryContext(task);
  const researchFirstTaskSection = task.first_task_on_repo ? FIRST_TASK_GUIDANCE : '';
  const researchEmptyRepoGuidance = buildEmptyRepoGuidance(task);
  const researchPrompt = `${codebaseSummary}${contextPrefix}${researchPlaybookSection}${researchRetryContext}You are a codebase research agent inside an autonomous AI coding system. Your findings spawn implementing agents, get reviewed, and merge as PRs.

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

${researchEmptyRepoGuidance}
**If a direction targets something EXISTING** (the design document shows it as partial, functional,
or mature): your job is to IMPROVE it. Find specific, high-impact changes — missing capabilities,
broken assumptions, architectural bottlenecks, or small fixes that prevent cascade failures.
Quality over quantity — 2 real findings beats 10 incremental ones.
(Note: any entry tagged with a "[reconciled: …]" / "presence_warning" annotation has been
downgraded to "missing" because its referenced module files are absent on this branch — treat it
as MISSING to DESIGN, never as something to IMPROVE, and do not confabulate its behavior.)

**Sequencing matters.** If your findings have dependencies (e.g., "build cache infrastructure"
must come before "add caching to user API"), use depends_on_indices to order them. The pipeline
respects these — dependent tasks won't start until prerequisites complete.

**Sequence wisely.** If foundational infrastructure is genuinely missing (check research context for gaps),
build it. But if foundations exist, prioritize user-visible features and product improvements over
additional structural refinement. Don't re-propose foundations that are already confirmed.

# CODEBASE CONTEXT

${fileTreeSection}${gitHistorySection}
${designDocHighlight}${designDocSection}${systemVerificationSection}${capabilityVerificationSection}${researchAvailableCapabilitiesSection}# DIRECTIONS
${userPrompt ? `Research focus: ${userPrompt}\n\n` : ''}${directionsText}${crossDirSection}
${priorKnowledgeSection}${pipelineScopeRatesSection}${anomalySection}
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

Review the DEDUP list above. For each finding you're about to propose,
ask: "is this genuinely new work, or does an active/completed/queued task already cover it?"
Different approaches to the same area ARE valid (e.g. "add rate limiting to webhooks" vs
"add error handling to webhooks"). Only skip a finding if a prior task solves the same problem.
When in doubt, keep the finding — false negatives (missing good work) are worse than duplicates.

# OUTPUT FORMAT

Write findings to /tmp/research_findings_${taskId}.json ${READ_ONLY_BASH_WRITE_NOTE}. Output the JSON array as the last thing in your response.

ALSO write /tmp/research_progress_${taskId}.md ${READ_ONLY_BASH_WRITE_NOTE} — a concise markdown summary (under ~3000 chars) of your investigation: which areas/directories you explored, which files you read, key observations and patterns discovered, and any remaining gaps or open questions. This is surfaced to retry agents if this task fails, so they can resume instead of re-investigating from scratch.

## Finding types

**Implementation findings** — these are your primary output. Each one spawns an agent that writes code:
  feature, improvement, bug, refactor, perf, security_audit, qa, deps, docs, migrate, test

**Compute experiments** — spawns a budgeted, auto-torn-down compute job (NOT a code PR):
  compute_experiment (aliases: measurement, benchmark) — emit when a MEASURABLE or EMPIRICAL
    question can only be answered by RUNNING CODE or a BENCHMARK, not by more reading: a
    perf claim to confirm (throughput/latency), a before-vs-after comparison, an accuracy or
    cost measurement. Do NOT use it for questions a code read settles, or for open-ended
    exploration. The finding must carry a launch contract describing how to run it:
    \`command\` (a single shell command that runs the experiment end-to-end. It executes inside a
    fresh, shallow clone of THIS repository at the current working directory — the repo root is
    also on PYTHONPATH — so import the project's code directly: \`from <pkg> import ...\`,
    \`python3 -m <pkg>.<module>\`, \`./run.sh\`. Do NOT clone the repo yourself or assume network
    git access; the checkout is throwaway scratch on a torn-down VM and writes are not persisted),
    \`spend_cap_usd\` (a positive USD cap — keep it conservative), and
    \`deadline_seconds\` (a positive wall-clock timeout). Optionally add \`success_criterion\`
    — a numeric goal of {"metric": "<name>", "operator": ">=|<=|>|<|==|!=", "threshold": <number>}
    — to make the result machine-checkable. Metric capture is MANDATORY: the command MUST
    write {metric: value} JSON to the file at \`$BLUEPRINT_RESULT_FILE\` as its final step OR print a
    JSON object containing it as the LAST line of stdout — e.g.
    \`... && python -c "import json,os; json.dump({'<metric>': <value>}, open(os.environ['BLUEPRINT_RESULT_FILE'],'w'))"\`
    or \`echo "{\\"<metric>\\": <value>}" > "$BLUEPRINT_RESULT_FILE"\` (use your real declared-metric
    key in place of \`<metric>\`); the criterion reads THAT value. A measured-but-missed goal is
    recorded as honest negative evidence (the dependent still runs), but a declared metric the run
    never produces a numeric value for is silently lost — the criterion is then evaluated UNMEASURED
    and fails loud (compute_unmeasured_criterion) — a job that ran cannot pass as a hypothesis that was
    measured. Put the launch fields either flat on the finding or nested under a
    \`compute_experiment\` object. Above a configured spend threshold the launch holds for operator
    approval; below it, it runs autonomously.
    \`smoke_sample\` (REQUIRED on every compute_experiment finding): BEFORE writing this
    finding to the output file, smoke-test the command using Bash to confirm it works on this VM.
    Run a short/capped variant (≤30 seconds), for example:
      BPRF=$(mktemp)
      timeout 30 bash -c "BLUEPRINT_RESULT_FILE=\\"$BPRF\\" <your command>" 2>/tmp/ce_smoke_err.txt
      CE_EXIT=$?
    Check startup — did it start without ImportError/ModuleNotFoundError/FileNotFoundError?
    Check metric — does $BPRF contain a JSON object with at least one numeric field?
    Record the result as a \`smoke_sample\` field on the finding:
      {"exit_code": CE_EXIT, "startup_ok": true/false, "metric_captured": true/false,
       "timed_out": (CE_EXIT==124), "error_excerpt": "<first 200 chars of stderr if any>"}
    If startup_ok=false: FIX the command or DROP the finding — nothing beyond the repo itself is guaranteed (no external services, network, secrets, or prior state); your smoke is proof.
    A timeout (exit_code=124) with startup_ok=true is acceptable — slow first iteration is fine.
    If the command genuinely cannot be smoked at all (requires hardware or resources unavailable
    here): set smoke_sample={"unsmokable":true,"unsmokable_reason":"<why>"} and the finding will
    be held explicitly for operator review rather than dispatching silently.${localRunnerNote}

**Sub-research** — spawns another research cycle (use sparingly):
  research

**Knowledge findings** — these spawn a knowledge agent that produces a durable, queryable
knowledge document (NOT a code PR). These are HIGH-ROI and chronically under-used: one good
knowledge document de-risks every later implementer that touches the area, and the index of
existing documents is queryable by all future agents. Emit one whenever it would genuinely
raise the success rate of downstream work — err toward emitting:
  web_research — emit when the work touches FACTS or BEST-PRACTICES that live OUTSIDE this
    repo. Concrete triggers: integrating or upgrading an external API, library, SDK, or
    provider; pricing/quota/limit facts; a security/standards reference; how mature OSS
    projects solve a problem this repo is about to solve; a comparison of approaches you
    cannot settle from the codebase alone. The deliverable is sourced knowledge the
    implementer needs before code can be written correctly.
  design — emit when implementation quality hinges on structure decided up front: the change
    is cross-cutting, architecturally risky, or has 2+ plausible shapes an implementer
    starting straight from a prompt would likely get wrong. The deliverable is a
    FILE-GROUNDED design document, grounded in the actual repo files, not external facts.
  A knowledge finding does NOT replace the implementation finding — emit both and declare the
  implementation finding's dependency on it via depends_on_indices, so the implementer launches
  with the document delivered into its context. Skip the knowledge finding only when the area is
  already covered (see "Existing knowledge documents" in your context) or the work is genuinely
  routine. Use web_research vs design by SOURCE OF TRUTH: outside the repo → web_research;
  inside the repo → design.

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
  "actionability_grade": "HIGH|MEDIUM|LOW",
  "verification_basis": "code_verified|path_verified|pattern_inferred|deduced",
  "acceptance_evidence": null,
  "related_files": ["path/to/file.<ext>"],
  "depends_on_indices": [],
  "prerequisite": null,
  "provides_capability": null,
  "direction_id": "d-xxx or null",
  "effort": "high|medium|low|null",
  "compute_experiment": {"command": "command run inside a clone of this repo at cwd, e.g. 'python3 -m pkg.eval --episodes 1000'", "spend_cap_usd": 5.0, "deadline_seconds": 1800, "success_criterion": {"metric": "accuracy", "operator": ">=", "threshold": 0.9}, "accelerator": "T4", "container_image": "optional — e.g. 'nvcr.io/nvidia/tensorflow:25.01'; runs command INSIDE this docker image (needed for GPU/CUDA stacks the repo can't pip-install natively)", "setup_step": "optional — host-level prep run BEFORE/OUTSIDE the container; put container-dependent installs in command, NOT here"},
  "smoke_sample": {"exit_code": 0, "startup_ok": true, "metric_captured": true, "timed_out": false, "error_excerpt": ""},
  "claims": ["short factual assertion backed by your investigation (omit when synthesizing)"],
  "sources": [{"type": "file|pr|log|message", "ref": "path/to/file.<ext> or pr#123 or msg_id", "excerpt": "optional short quote"}]
}]
\`\`\`

**claims** (optional): Short factual assertions that back this finding — one sentence each, verifiable (e.g. '<module>:<function> does X at <path>:<line>'). Omit the field when synthesizing across many observations with no single traceable origin.

**sources** (optional, provenance only): Cite a specific origin when you have one. type 'file': a file you read, 'pr': a merged PR number, 'log': a pipeline log line, 'message': a user message. Omit the field when synthesizing. excerpt is optional — include only a directly relevant short quote.

**prerequisite** (optional, default null): Declare a FOUNDATION this finding depends on that is being (or was) built OUTSIDE this batch — an earlier or concurrently-in-flight cycle, or a named capability. Shape: \`{"kind": "task", "ref": "<task-id or finding-hash>"}\` to gate on a specific in-flight/queued task, or \`{"kind": "capability", "ref": "<capability-name>"}\` to gate on whichever task advertises that capability. For a capability ref, DRAW THE NAME from the AVAILABLE CAPABILITIES vocabulary above so it resolves to the registry's canonical string (the matcher is EXACT, never fuzzy). When it resolves to a real foundation the dependent is structurally gated (depends_on edge) so it does not run against a base missing the foundation or duplicate it; when it does not resolve the dependent enters a BLOCKED state with the unmet prerequisite recorded as a machine-resolvable need, and is resumed when the foundation appears or escalated to the operator after a timeout. Use ONLY for cross-cycle / cross-batch foundations or named capabilities — for ordering findings WITHIN this batch, use depends_on_indices instead (NOT prerequisite). Omit (or null) when there is no external foundation.

**compute_experiment** (optional, default null): Present ONLY on a compute_experiment/measurement/benchmark finding — the launch contract for the budgeted compute job. Required keys \`command\`, \`spend_cap_usd\` (positive), \`deadline_seconds\` (positive); optional \`success_criterion\` (numeric metric/operator/threshold goal) and benchmark metadata (\`machine_type\`, \`accelerator\`, \`experiment\`, \`benchmark\`, \`metric\`, \`cwd\`, \`env\`). The \`accelerator\` field (one of \`T4\`, \`L4\`, \`V100\`) is **sufficient on its own** to request a single GPU — you do NOT need to specify \`machine_type\` when requesting a single GPU by accelerator name; the system resolves the correct GPU machine type automatically. Optional \`container_image\`: runs \`command\` INSIDE the given docker image (e.g. an NVIDIA NGC image such as \`nvcr.io/nvidia/tensorflow:25.01\`) — REQUIRED when the experiment needs a runtime the repo cannot \`pip install\` natively, e.g. a CUDA/cuDNN/framework stack; without it the command runs BARE and dies with ModuleNotFoundError. Optional \`setup_step\`: runs as bare \`bash -lc\` on the HOST, BEFORE and OUTSIDE the container — use it ONLY for host-level pre-container prep. Anything that needs the container's environment (pip installs into the experiment's interpreter, importing the container's libraries) MUST go in \`command\`, NOT \`setup_step\`. Omit entirely on non-experiment findings.

**provides_capability** (optional, default null): A short kebab-case capability name this finding's task advertises (e.g. "<feature>-core"), so a sibling finding (or another lane) declaring \`{"kind": "capability", "ref": "<feature>-core"}\` can depend on it. DRAW THE NAME from the AVAILABLE CAPABILITIES vocabulary above when one fits; propose a NEW kebab-case name (same convention) ONLY for a genuinely new capability not already listed. Omit unless another task needs to gate on this one by capability. ${CAPABILITY_DECLARATION_DOC}

**actionability_grade rubric** (required on every finding):
- HIGH: problem statement is clear, desired outcome is clear, related files identified, no major unknowns blocking implementation.
- MEDIUM: problem clear but solution path uncertain, OR small unknowns the implementer can resolve with brief exploration.
- LOW: vague or exploratory, or requires sub-research before code can land. Consider emitting a research sub-finding instead.

**verification_basis rubric** (OPTIONAL — defaults to pattern_inferred when omitted): how you established the underlying claim, i.e. the STRENGTH OF EVIDENCE behind it. This is ORTHOGONAL to actionability_grade — actionability_grade is about how implementable the finding is, verification_basis is about how well-evidenced the claim is. A finding can be HIGH actionability but only pattern_inferred, or LOW actionability but code_verified. Do not conflate them.
- code_verified: you read the actual source and confirmed the claim directly.
- path_verified: you confirmed the relevant file paths are present/absent via the file tree, but did not read their contents.
- pattern_inferred: structural inference from naming/architecture patterns, with no direct inspection. This is the default — use it (or omit the field) when uncertain.
- deduced: derived from adjacent evidence (e.g. another finding, a log line) without directly inspecting the subject.

**acceptance_evidence** (OPTIONAL, additive — default null): the FORWARD evidence that will prove this finding's claim true once implemented, carried onto the spawned task. This is the OPPOSITE direction from verification_basis: verification_basis is BACKWARD ('how well-evidenced is the claim I am making now'), acceptance_evidence is FORWARD ('what concrete observation will demonstrate the fix worked'). ${ACCEPTANCE_EVIDENCE_RUBRIC} Omit (or null) when you cannot name a concrete, non-trivial proof — never invent a placeholder/always-true declaration (those are rejected).

Scope: small (<50 lines, 1 file), medium (2-5 files, 50-200 lines), large (cross-component, 200+ lines).
Risk: low (cosmetic), medium (logic/endpoints), high (core logic, auth, data models).
Routing: large scope → plan agent (decomposition), medium → create agent (single PR), research → sub-research, web_research/design → knowledge agent (produces a knowledge document, not a PR).

- Use scope=large for cross-component work — the pipeline has a plan agent for safe decomposition. But most product features are scope=small or scope=medium.
- direction_update: suggest rewording/reprioritizing a direction (fields: direction_id, suggested_text, suggested_priority, suggested_paths, description). Max 1 per cycle.
- direction_note: add context to a direction (fields: direction_id, description max 2000 chars). Be honest about progress.
- direction_proposal: propose a new standing direction (max 2 per cycle, held for user approval).
- design_doc_observation: flag an architectural observation for the design document (fields: description, section). Use when you discover something about the system's architecture — module patterns, missing capabilities, structural risks. These are reviewed by the architecture agent, not acted on directly. Max 3 per cycle.
- Optional: /tmp/research_insights_${taskId}.json ${READ_ONLY_BASH_WRITE_NOTE} with [{"summary": "<200 chars", "area": "path"}] for observations not worth implementing.`;

  const researchPromptSuffix = payload.prompt_suffix ? `\n\nAdditional instructions:\n${strVal(payload.prompt_suffix)}` : '';
  const researchPromptFull = researchPrompt + researchPromptSuffix + researchFirstTaskSection + PLAYBOOK_GUIDANCE + '\nNote: If your research uncovered operational knowledge (build patterns, module relationships, common failure modes), save the most reusable findings to the playbook.\n' + `\n\n${researchFetchGuidance}${DEFAULT_TOOL_GUIDANCE}`;
  const researchPromptTelemetry = buildPromptSizeTelemetry(researchPromptFull, [
    ['context_prefix', contextPrefix],
    ['playbook', researchPlaybookSection],
    ['retry_context', researchRetryContext],
    ['codebase_instructions', codebaseSummary],
    ['file_tree', fileTreeSection],
    ['git_history', gitHistorySection],
    ['design_doc_highlight', designDocHighlight],
    ['design_doc', designDocSection],
    ['system_verification', systemVerificationSection],
    ['capability_verification', capabilityVerificationSection],
    ['available_capabilities', researchAvailableCapabilitiesSection],
    ['directions', directionsText],
    ['cross_direction', crossDirSection],
    ['prior_knowledge', priorKnowledgeSection],
    ['pipeline_scope_rates', pipelineScopeRatesSection],
    ['anomaly', anomalySection],
  ]);
  setPromptSizeTelemetry(
    researchPromptTelemetry.prompt_size_bytes,
    researchPromptTelemetry.prompt_section_count,
    researchPromptTelemetry.prompt_section_sizes,
  );
  if (sendHeartbeat) sendHeartbeat(taskId, statusRef.value, { prompt_fingerprint: promptFingerprint });
  statusRef.value = 'Running codebase research...';
  const { exitCode, allOutput, lastResultText } = await runClaudeCode(researchPromptFull, ws, statusRef, model, effort, disallowedTools);

  // Prefer reading findings from /tmp/ file (avoids output truncation), fall back to output parsing
  const researchFindings = readHandlerOutput(taskId, 'research_findings', ws);
  let logs = researchFindings !== null ? sanitize(JSON.stringify(researchFindings)) : '';
  if (!logs) {
    if (lastResultText && lastResultText.includes('"prompt"')) {
      logs = sanitize(lastResultText);
    } else {
      const findingsArr = extractJsonArrayDetailed(allOutput, 'prompt');
      if (!findingsArr.ok) {
        sendLog(ws, `[WARNING] Could not extract research findings JSON: ${describeJsonExtractionResult(findingsArr)}\n`);
      }
      logs = sanitize(findingsArr.ok ? JSON.stringify(findingsArr.value) : (lastResultText || allOutput.slice(-50_000)));
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
    sendLog(ws, `[ERROR] Failed to post research completion status: ${String(e)}\n`);
  }
}

// ── Re-export all section builders from the extracted file ─────────────────
export {
  buildStrategicCompletionLogs,
  classifyFailureCategory,
  buildScopeCalibrationSignal,
  buildFilteredFindingsSection,
  buildRejectedProposalsSection,
  buildOperatorOverridesSection,
  buildRunningUpdateSupersededSection,
  buildUserPausedSection,
  QUALIFICATION_STATE_VOCAB,
  buildVersionPhaseSection,
  buildMemoCitationsSection,
  resolveMemoHandles,
  buildEfficiencySnapshotSection,
  buildPortfolioComposition,
  buildEmptyRepoGuidance,
  buildCapabilityVerificationSection,
  buildAvailableCapabilitiesSection,
} from './research-plan-sections.js';
