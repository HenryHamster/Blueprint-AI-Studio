import fs from 'fs';
import type WebSocket from 'ws';
import {
  WORKSPACE, strVal, sanitize, sendLog, readHandlerOutput,
  persistAndPostStatus, runClaudeCode,
  apiPost,
  resolveBaseBranch,
} from '../index.js';
import { VmConfigMissingError } from '../providers/runners.js';
import * as indexMod from '../index.js';
import { prepareWorkspaceForRole } from '../workspacePrep.js';
import {
  buildContextPrefix, configureAgentPermissions,
  writePlaybookHelper, writeMessagesHelper,
} from '../prompt.js';
import {
  writeStrategicHelpers,
  runStrategicChallenge,
  buildStrategicChallengeExecutorSection,
  blockedChallengeRecord,
} from './strategic-helpers.js';
import {
  recordFormatMiss, recordFormatRepairAttempt, recordFormatRepairSuccess,
} from '../telemetry.js';
import { attemptStructuredRepair } from './structured-repair.js';
import { parseBackendTimestamp } from '../utils/time.js';
import { reconcileDesignDocIndex } from '../utils/design-doc-presence.js';
import {
  formatPreviousDecisionOutcomes,
  formatPastRetirementCritiques,
  formatRetiredDirectionsSummary,
} from './research-direction-context.js';
import {
  computeResearchPlanPromptFingerprint,
  buildThinkerPrompt,
  runThinkerWithGate,
} from '../prompts/thinker.js';
import {
  buildStrategicCompletionLogs,
  readProgressArtifact,
  buildRejectedProposalsSection,
  buildOperatorOverridesSection,
  buildRunningUpdateSupersededSection,
  buildUserPausedSection,
  buildVersionPhaseSection,
  buildMemoCitationsSection,
  resolveMemoHandles,
  buildEfficiencySnapshotSection,
  buildPortfolioComposition,
  buildAvailableCapabilitiesSection,
  CAPABILITY_DECLARATION_DOC,
} from './research-plan.js';
import {
  buildSystemMaturitySection,
  buildCycleStartReconciliationSection,
  buildPruneCandidatesSection,
  buildModelHealthSection,
  buildDriftSection,
  buildOutcomeSection,
  buildClockAndRecentWorkSection,
  buildConversationThreadSection,
  buildFeedbackSection,
  buildQuestionSection,
  buildOpenMessagesText,
  buildDirSummaryText,
  buildDirHealthSummary,
} from './strategic-sections.js';

/**
 * Render the backend-supplied `prompt_suffix` as an "Additional instructions"
 * block, matching the convention every other agent task type uses (see
 * research-plan.ts / standard.ts / audits.ts). The strategic payload packs
 * per-cycle guidance here every cycle — deployment-model framing, stabilize
 * audit-findings hints, build/stabilize stall nudges, mode hints — so this is
 * reasoning input that must reach the thinker (analysis) pass. Returns '' when
 * the suffix is absent so the prompt stays unchanged for empty payloads.
 */
export function renderStrategicPromptSuffix(payload: Record<string, unknown>): string {
  return payload.prompt_suffix
    ? `\n\nAdditional instructions:\n${strVal(payload.prompt_suffix)}`
    : '';
}

/**
 * Render the backend-supplied `staged_directions` block as a prompt section so
 * the strategist sees what work is already STAGED for the next version (inert
 * until that version promotes) and does NOT re-propose it. Returns '' when
 * nothing is staged so the section is omitted on the common path. Defined here
 * (not in strategic-sections.ts) so it does not have to be threaded through that
 * module's exhaustive vi.mock in strategic.test.ts.
 */
/**
 * Render the shared capability vocabulary section (canonical registry names +
 * statuses) followed by the uniform provides_capability / prerequisite
 * declaration guidance, so the strategist declares cross-lane capability refs
 * from the SAME registry namespace research, plan, and design use. Returns ''
 * when no capabilities are available so the prompt is unchanged.
 */
export function buildStrategicAvailableCapabilitiesSection(caps: unknown): string {
  const section = buildAvailableCapabilitiesSection(caps);
  if (!section) return '';
  return `${section}${CAPABILITY_DECLARATION_DOC}\n`;
}

export function buildStagedDirectionsSection(staged: Array<Record<string, unknown>>): string {
  if (!Array.isArray(staged) || staged.length === 0) return '';
  const lines = staged.map(d => {
    const id = strVal(d.id, '?');
    const text = strVal(d.text, '(no text)');
    const priority = strVal(d.priority, 'medium');
    const v = typeof d.staged_for_version === 'number' ? d.staged_for_version : '?';
    return `- ${id} [${priority}] — ${text} (auto-activates when v${v} promotes)`;
  });
  return [
    '## Staged for next version',
    '_These directions are STAGED — inert until the version they are waiting on promotes, then they auto-activate. Do NOT re-propose work already staged here. Staging is a decision that work belongs to the next version, not a way to defer._',
    ...lines,
  ].join('\n');
}

/**
 * Render the compact design-doc INDEX as a clean architecture overview — NOT a
 * raw JSON dump. The backend now ships the design doc as a navigation index
 * (`{ sections: [{ key, summary?, status?, modules?, presence_warning? }] }`,
 * see `_design_doc_index` in backend/app/strategic_spawning.py): one entry per
 * non-internal section, with reconciled presence status + backing module paths
 * for `systems/*` and `capabilities/*`. Full section prose/specs are NO LONGER
 * inlined — the strategist pulls them on demand via `./design-doc get <key>`.
 *
 * Returns '' when the index has no sections so the caller can omit the section.
 */
export function renderDesignDocIndexOverview(index: Record<string, unknown> | undefined): string {
  if (!index || typeof index !== 'object') return '';
  const sections = index.sections;
  if (!Array.isArray(sections) || sections.length === 0) return '';

  const systems: string[] = [];
  const capabilities: string[] = [];
  const other: string[] = [];

  for (const raw of sections) {
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) continue;
    const entry = raw as Record<string, unknown>;
    const key = strVal(entry.key);
    if (!key) continue;
    const summary = strVal(entry.summary);
    const status = strVal(entry.status);
    const modules = Array.isArray(entry.modules)
      ? (entry.modules as unknown[]).filter((m): m is string => typeof m === 'string' && !!m.trim())
      : [];
    const warning = strVal(entry.presence_warning);

    const parts: string[] = [`- \`${key}\``];
    if (status) parts.push(`[${status}]`);
    if (summary) parts.push(`— ${summary}`);
    let line = parts.join(' ');
    if (modules.length > 0) line += `\n  - modules: ${modules.join(', ')}`;
    if (warning) line += `\n  - ⚠ ${warning}`;
    // repo_overview carries inline bootstrap context so the first strategic
    // cycle has repo orientation without a fetch round-trip.
    if (key === 'repo_overview') {
      const techStack = strVal(entry.tech_stack);
      const dirLayout = strVal(entry.directory_layout);
      const firstSteps = strVal(entry.first_steps_summary);
      if (techStack) line += `\n  - tech_stack: ${techStack}`;
      if (dirLayout) line += `\n  - directory_layout: ${dirLayout}`;
      if (firstSteps) line += `\n  - first_steps: ${firstSteps}`;
    }

    if (key.startsWith('systems/')) systems.push(line);
    else if (key.startsWith('capabilities/')) capabilities.push(line);
    else other.push(line);
  }

  const blocks: string[] = [];
  if (systems.length > 0) blocks.push(['### Systems', ...systems].join('\n'));
  if (capabilities.length > 0) blocks.push(['### Capabilities', ...capabilities].join('\n'));
  if (other.length > 0) blocks.push(['### Other sections', ...other].join('\n'));
  if (blocks.length === 0) return '';

  blocks.push(
    '_This is a navigation index — section statuses and backing modules only, not the full prose/specs. ' +
      'Fetch any section\'s FULL text on demand with `./design-doc get <key>` (e.g. `./design-doc get systems/auth`), ' +
      'and `./design-doc list` to enumerate all keys. Pull the full text whenever you need a section\'s detailed prose or specs._',
  );
  return blocks.join('\n\n');
}

// Schema restated to the agent on a bounded self-repair re-ask when a clean exit
// emitted the legacy inline strategic markers but the JSON failed to extract.
const STRATEGIC_COMPLETION_SCHEMA_HINT =
  `{\n  "problem_model": { ... } | "vision": "...",\n  "direction_operations": [ { "op": "create_direction" | "update_direction" | "retire_direction" | "stage_direction" | "approve_direction" | "reject_direction", ... } ]\n}\nThe object MUST contain "direction_operations" and at least one of "problem_model" or "vision".`;

export async function handleStrategic(
  taskId: string, payload: Record<string, unknown>, task: Record<string, unknown>,
  ghToken: string, owner: string, repo: string, ws: WebSocket | null, statusRef: { value: string },
  model?: string, effort?: string
) {
  const baseBranch = resolveBaseBranch(payload, task);

  // Clone — strategic still needs a workspace for writing helper scripts
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
  writePlaybookHelper(taskId);
  writeMessagesHelper(taskId);
  const researchMode = strVal(payload.research_mode as string, 'research');
  const disallowedTools = configureAgentPermissions(researchMode);
  const promptFingerprint = computeResearchPlanPromptFingerprint('research');
  const setPromptFingerprint = (indexMod as { setCurrentPromptFingerprint?: (fp: string) => void }).setCurrentPromptFingerprint;
  if (setPromptFingerprint) setPromptFingerprint(promptFingerprint);
  const sendHeartbeat = (indexMod as { sendHeartbeatSync?: (taskId: string, status: string, extra?: Record<string, unknown>) => void }).sendHeartbeatSync;
  if (sendHeartbeat) sendHeartbeat(taskId, statusRef.value, { prompt_fingerprint: promptFingerprint });

  const contextPrefix = buildContextPrefix(task);

  // ── Strategic mode: product-level vision review ────────────────────────
    const vision = payload.vision as Record<string, unknown> | undefined;
    const directionsSummary = Array.isArray(payload.directions_summary)
      ? payload.directions_summary as Array<Record<string, unknown>>
      : [];
    // Trim-proof authoritative direction counts (active/paused/total/max_active)
    // shipped at critical tier by the backend so they survive the byte-budget
    // pressure that trims/drops directions_summary. The three direction views
    // anchor their headline count, slots line, and gap flags on these so they
    // never under-report (trimmed) or render false-empty (dropped).
    const directionCounts = payload.direction_counts && typeof payload.direction_counts === 'object'
      ? payload.direction_counts as Record<string, unknown>
      : undefined;
    const authoritativeActive = directionCounts && typeof directionCounts.active === 'number'
      ? directionCounts.active
      : undefined;
    const authoritativePaused = directionCounts && typeof directionCounts.paused === 'number'
      ? directionCounts.paused
      : undefined;
    // Authoritative per-system active-direction map (nested under the
    // trim-proof `direction_counts`), so the portfolio per-system coverage-gap
    // and concentration signals stay truthful when `directions_summary` is
    // trimmed. Absent on older payloads → buildPortfolioComposition falls back.
    const authoritativeSystemCounts = directionCounts
      && directionCounts.by_system
      && typeof directionCounts.by_system === 'object'
      ? directionCounts.by_system as Record<string, number>
      : undefined;
    const projectName = strVal(payload.project_name, 'Unknown Project');

    const openMessages = Array.isArray(payload.open_messages)
      ? payload.open_messages as Array<Record<string, unknown>>
      : [];

    const visionJson = vision && Object.keys(vision).length > 0
      ? JSON.stringify(vision, null, 2)
      : '';

    // Mode-awareness signals: system maturity snapshots + short-window drift
    const systemMaturity = Array.isArray(payload.system_maturity)
      ? payload.system_maturity as Array<Record<string, unknown>>
      : [];
    const driftSignals = payload.drift_signals && typeof payload.drift_signals === 'object'
      ? payload.drift_signals as Record<string, unknown>
      : null;

    const systemMaturitySection = buildSystemMaturitySection(systemMaturity);
    const driftSection = buildDriftSection(driftSignals);

    const efficiencySnapshotSection = buildEfficiencySnapshotSection(payload.efficiency_snapshot);

    const outcomeEval = payload.outcome_evaluation as Record<string, unknown> | undefined;
    const outcomeSection = buildOutcomeSection(outcomeEval);

    // Recent interventions — time-series view of what shipped + per-week rate deltas.
    // Given with explicit non-causal framing so the agent doesn't over-attribute.
    const recentInterventions = payload.recent_interventions as Record<string, unknown> | undefined;
    const { clockHeader, recentWorkSection } = buildClockAndRecentWorkSection(recentInterventions);

    // Unified conversation thread — chronological, shows back-and-forth
    const conversationThread = Array.isArray(payload.conversation_thread)
      ? payload.conversation_thread as Array<Record<string, unknown>>
      : [];
    const threadSection = buildConversationThreadSection(conversationThread);
    const feedbackSection = buildFeedbackSection(openMessages);
    const questionSection = buildQuestionSection(openMessages);
    const openMessagesText = buildOpenMessagesText(openMessages);

    const dirSummaryText = buildDirSummaryText(directionsSummary, authoritativeActive);

    // Directions already staged for the next version (inert until their target
    // version promotes). Surfaced so the strategist does not re-propose them.
    const stagedDirections = Array.isArray(payload.staged_directions)
      ? payload.staged_directions as Array<Record<string, unknown>>
      : [];
    const stagedDirectionsSection = buildStagedDirectionsSection(stagedDirections);

    // The backend now ships the design doc as a COMPACT INDEX (see
    // `_design_doc_index` in backend/app/strategic_spawning.py): one entry per
    // non-internal section carrying key + one-line summary, plus reconciled
    // status + backing module paths for systems/* and capabilities/*. Full
    // section prose/specs are pulled on demand via `./design-doc get <key>`.
    //
    // Run the agent-side presence reconciliation over the cloned file tree as an
    // optional second pass: the index still carries the declared module paths,
    // so a systems/* or capabilities/* entry claiming a present status whose
    // backing modules are all absent on the integration branch is downgraded to
    // `missing` + annotated — catching code reverted/never-landed since the
    // server-side grounding pass ran. Then render as a clean architecture
    // overview (NOT a raw JSON dump).
    const designDocIndex = reconcileDesignDocIndex(
      payload.design_doc as Record<string, unknown> | undefined,
      WORKSPACE,
    );
    const designDocJson = renderDesignDocIndexOverview(designDocIndex);

    // Write strategic helper scripts (strategic, fetch, design-doc)
    const projectMetadataForFetch = task.project_metadata as Record<string, unknown> | undefined;
    const fetchProjectId = (projectMetadataForFetch?.project_id as string | undefined) ?? '';
    writeStrategicHelpers(taskId, fetchProjectId, WORKSPACE);

    // ── Compute direction health summary for thinker prompt ──
    const maxActiveDirections = typeof payload.max_active_directions === 'number'
      ? payload.max_active_directions
      : 10;
    const activeDirections = directionsSummary.filter(d => strVal(d.status, 'active') === 'active');
    const dirHealthSummary = buildDirHealthSummary(
      directionsSummary,
      maxActiveDirections,
      authoritativeActive !== undefined
        ? { active: authoritativeActive, paused: authoritativePaused ?? 0 }
        : undefined,
    );

    // Portfolio composition: category mix, system coverage gaps, concentration
    // risk, in-flight scope, direction age — so the thinker doesn't have to
    // re-compute these from scattered per-direction lines.
    const outcomeSystems = outcomeEval && Array.isArray(outcomeEval.systems)
      ? outcomeEval.systems as Array<Record<string, unknown>>
      : (vision && Array.isArray(vision.systems) ? vision.systems as Array<Record<string, unknown>> : []);
    const activeByScopeForPortfolio = (payload.recent_interventions as Record<string, unknown> | undefined)
      ?.active_by_scope as Record<string, number> | undefined;
    const portfolioCompositionText = buildPortfolioComposition(
      activeDirections,
      outcomeSystems,
      activeByScopeForPortfolio,
      authoritativeActive,
      authoritativeSystemCounts,
    );

    // ── Running context from prior cycles (design doc notes/strategic-context) ──
    // The full prose of notes/strategic-context is NO LONGER inlined in the
    // payload (the design_doc is now a compact index). To avoid silently losing
    // the cross-cycle running context, surface the index's one-line summary for
    // that section (when present) and ALWAYS append a fetch hint so the
    // strategist can pull the full running context on demand. The section may be
    // absent entirely on a fresh project — in that case emit nothing.
    const designDocSections = designDocIndex && Array.isArray(designDocIndex.sections)
      ? designDocIndex.sections as unknown[]
      : [];
    const strategicContextEntry = designDocSections.find(
      (e): e is Record<string, unknown> =>
        !!e && typeof e === 'object' && !Array.isArray(e) &&
        strVal((e as Record<string, unknown>).key) === 'notes/strategic-context',
    );
    const strategicContext = strategicContextEntry
      ? `${strVal(strategicContextEntry.summary) || '_(summary unavailable)_'}\n\n_Running context shipped as an index entry this cycle — fetch the FULL prior-cycle context with \`./design-doc get notes/strategic-context\` before relying on it._`
      : '';

    // ── Problem model + running update + self-reflection rendering ──
    // These are the new strategic substrate — persistent durable state the
    // agent maintains across cycles. They replace vision as source of truth.
    const problemModel = payload.problem_model as Record<string, unknown> | undefined;
    const problemModelJson = problemModel && Object.keys(problemModel).length > 0
      ? JSON.stringify(problemModel, null, 2)
      : '';

    // ── Problem-model size/health signal (soft) ──
    // Computed by the backend from the DURABLE pre-trim model (honest counts).
    // Surfaces how heavy the model is to audit in one pass; rendered into a
    // concise section that the thinker weighs when deciding whether to prune
    // before growing. NEVER a hard cap.
    const modelHealth = payload.model_health && typeof payload.model_health === 'object'
      ? payload.model_health as Record<string, unknown>
      : null;
    const modelHealthSection = buildModelHealthSection(modelHealth);

    // ── Per-gap memory pre-trim signal ──
    // The backend's `_agent_view_problem_model` (strategic_spawning.py) trims
    // each active gap's append-only history/past_moves/evidence to a recent
    // window BEFORE the byte-budget packer runs, so this trim never lands in
    // `_packer_meta` and the model still ships as 'present'. It surfaces the
    // trim inline on the problem_model as `_gap_memory_trimmed` — a list of
    // {gap_id, fields} entries. Extract the affected gap_ids and hand them to
    // the thinker prompt so it steers the agent to `./fetch` full gap history
    // before reasoning about a long-lived gap's arc, instead of mistaking the
    // shortened tail for the gap's entire lifetime.
    const gapMemoryTrimmedMarker = problemModel?._gap_memory_trimmed;
    const gapMemoryTrimmedGapIds = Array.isArray(gapMemoryTrimmedMarker)
      ? (gapMemoryTrimmedMarker as Array<Record<string, unknown>>)
          .map(g => strVal(g.gap_id))
          .filter(Boolean)
      : [];

    const runningUpdateDoc = typeof payload.running_update === 'string'
      ? payload.running_update
      : '';
    const runningUpdateAckAt = strVal(payload.running_update_acknowledged_at);
    const userLastActiveAt = strVal(payload.user_last_active_at);
    const selfReflectionEntries = Array.isArray(payload.self_reflection_log)
      ? payload.self_reflection_log as Array<Record<string, unknown>>
      : [];
    const isSelfRepo = payload.is_self_repo === true;

    const renderRunningUpdateDoc = (doc: string): string => {
      if (!doc.trim()) {
        return '_(running_update is empty — fresh project or nothing written yet. This cycle should populate it.)_';
      }
      return doc.trim();
    };

    const renderReflectionList = (entries: Array<Record<string, unknown>>): string => {
      if (entries.length === 0) {
        return '_(no prior self-reflections on record)_';
      }
      const lines: string[] = [];
      for (const e of entries.slice(-5)) {
        const ts = strVal(e.ts, '?');
        const hard = strVal(e.what_was_hard);
        const missing = strVal(e.missing_context);
        const prop = strVal(e.proposed_self_improvement);
        lines.push(`- **${ts}**`);
        if (hard) lines.push(`  - hard: ${hard}`);
        if (missing) lines.push(`  - missing: ${missing}`);
        if (prop) lines.push(`  - proposed: ${prop}`);
      }
      return lines.join('\n');
    };

    const formatRelativeDays = (iso: string): string => {
      if (!iso) return '';
      try {
        const d = parseBackendTimestamp(iso);
        if (!d) return '';
        const days = Math.max(0, Math.floor((Date.now() - d.getTime()) / 86400000));
        if (days === 0) return 'today';
        if (days === 1) return '1 day ago';
        return `${days} days ago`;
      } catch { return ''; }
    };

    const userActiveRel = formatRelativeDays(userLastActiveAt);
    const ackRel = formatRelativeDays(runningUpdateAckAt);
    const userStateLine = (userActiveRel || ackRel)
      ? `User last active: ${userActiveRel || 'unknown'}${ackRel ? ` | last acknowledged running_update: ${ackRel}` : ''}`
      : 'User activity: no record yet';

    // ── Packer metadata: tell the agent what was trimmed and how to recover it ──
    // The strategic-spawn payload is filled under a hard byte budget (see
    // pack_under_budget in backend/app/payload_schemas.py). When the packer
    // can't fit a section it either summarizes (list_trimmed) or drops it
    // wholesale; both forms land in payload._packer_meta. Render them so the
    // agent knows what's available-but-not-shipped and can call ./fetch.
    const packerMeta = payload._packer_meta as Record<string, unknown> | undefined;
    const renderPackerMeta = (meta: Record<string, unknown> | undefined): string => {
      if (!meta) return '';
      const trimmed = Array.isArray(meta.trimmed) ? meta.trimmed as Array<Record<string, unknown>> : [];
      const dropped = Array.isArray(meta.dropped) ? meta.dropped as Array<Record<string, unknown>> : [];
      if (trimmed.length === 0 && dropped.length === 0) return '';
      const lines: string[] = ['## Context not shipped this cycle (use ./fetch if you need it)'];
      if (trimmed.length > 0) {
        lines.push('', 'Trimmed (partial data shipped):');
        for (const t of trimmed) {
          const k = strVal(t.key, '?');
          const kind = strVal(t.kind, 'summarized');
          if (kind === 'list_trimmed') {
            const shown = (t.shown as number | undefined) ?? '?';
            const total = (t.total as number | undefined) ?? '?';
            lines.push(`  - ${k}: showing ${shown} of ${total} entries`);
          } else {
            lines.push(`  - ${k}: shipped in summarized form`);
          }
        }
      }
      if (dropped.length > 0) {
        lines.push('', 'Dropped entirely (not in this payload):');
        for (const d of dropped) {
          const k = strVal(d.key, '?');
          const reason = strVal(d.reason, 'budget exhausted');
          lines.push(`  - ${k}: ${reason}`);
        }
      }
      lines.push(
        '',
        'To pull full detail when you need it:',
        '  - `./fetch problem-model` — full durable problem_model for this project (use when the section was trimmed or dropped)',
        '  - `./fetch direction <id>` — full direction record (research_history capped at 20 cycles)',
        '  - `./fetch playbook <key>` — single playbook entry (e.g. `./fetch playbook frontend-build`)',
        '  - `./fetch gap <id>` — single gap with full history/past_moves/evidence',
        '  - `./fetch message <ref>` — inspect a message by short ref (8+ hex chars) or UUID, including one aged out of the live thread; then `./messages resolve <ref>` accepts the same ref',
        '  - `./fetch section <key>` — single design_doc section, full prose/specs, presence-grounded (e.g. `./fetch section systems/auth`); the design_doc ships as a compact index so pull a section before relying on its detail',
        '  - `./fetch problem-model` — the FULL untrimmed problem_model (all gaps, friction, archived) if it was trimmed/dropped above',
      );
      return lines.join('\n');
    };
    const packerMetaSection = renderPackerMeta(packerMeta);

    // ── Distinguish "dropped/trimmed by the packer" from "genuinely first cycle" ──
    // The packer (see backend/app/payload_schemas.py) records every section it
    // dropped wholesale or summarized under the byte budget in
    // `_packer_meta.{dropped,trimmed}` with a `key`. When `problem_model` is in
    // either list, a durable model EXISTS but was not shipped in full — the agent
    // must `./fetch problem-model` and EDIT it, NOT bootstrap a cold-start model
    // (which the executor would submit as a full replacement, wiping real state).
    // A `trimmed` (summarized) model ships partial JSON; that partial view must
    // still steer to fetch, because submitting it as a replacement loses detail.
    const droppedList = packerMeta && Array.isArray(packerMeta.dropped)
      ? packerMeta.dropped as Array<Record<string, unknown>> : [];
    const trimmedList = packerMeta && Array.isArray(packerMeta.trimmed)
      ? packerMeta.trimmed as Array<Record<string, unknown>> : [];
    const problemModelDropped = droppedList.some(d => strVal(d.key) === 'problem_model');
    const problemModelTrimmed = trimmedList.some(t => strVal(t.key) === 'problem_model');

    const problemModelRecovery: 'present' | 'fetch' | 'empty' =
      problemModelTrimmed ? 'fetch'
      : problemModelJson ? 'present'
      : problemModelDropped ? 'fetch'
      : 'empty';

    let problemModelSection: string;
    if (problemModelRecovery === 'present') {
      problemModelSection = `## Your current understanding (problem_model)\n\`\`\`json\n${problemModelJson}\n\`\`\``;
    } else if (problemModelRecovery === 'fetch') {
      const partialView = problemModelJson
        ? `\n\nA partial/summarized view was shipped below, but it is NOT the full model — do not treat it as complete or submit it as a replacement:\n\`\`\`json\n${problemModelJson}\n\`\`\``
        : '';
      problemModelSection = `## Your current understanding (problem_model)\n_**Your durable problem_model EXISTS but was not shipped in full this cycle** — the packer dropped or summarized it under the byte budget (see "Context not shipped this cycle" above). This is NOT the first cycle. Before you submit ANY problem_model update you MUST run \`./fetch problem-model\` to retrieve the full current model, then EDIT it — never replace it from scratch, or you will wipe real durable capabilities, gaps, and friction._${partialView}`;
    } else {
      problemModelSection = `## Your current understanding (problem_model)\n_(empty — this is the first cycle. Bootstrap from the other sections below.)_`;
    }
    const cycleStartReconciliationSection = buildCycleStartReconciliationSection(
      payload.cycle_start_reconciliation as Record<string, unknown> | undefined,
    );
    const pruneCandidatesSection = buildPruneCandidatesSection(
      payload.prune_candidates as Record<string, unknown> | undefined,
    );

    const runningUpdateSection = `## Running update (living document — rewrite, don't append)\n${renderRunningUpdateDoc(runningUpdateDoc)}\n\n_This is the document the user currently sees. Refine it this cycle — remove stale content, integrate what's new, keep it coherent and short. It's a current snapshot, not a log._`;

    const reflectionSection = `## What you previously said was hard\n${renderReflectionList(selfReflectionEntries)}`;

    const pastRetirementCritiquesSection = formatPastRetirementCritiques(payload.past_retirement_critiques);

    const selfImprovementBlock = payload.enable_self_improvement === true && isSelfRepo ? `

# Self-improvement

This project's codebase includes your own prompts, schemas, tools, and
orchestration. When you notice a recurring limitation in yourself — missing
context, unclear instruction, schema friction — it's a gap. Name it.

Look at "What you previously said was hard" above. If the same theme
appears in 2+ reflections, open a gap tagged \`self_improvement\` and
propose a direction that modifies the code governing your behavior.
Cite the reflection entries via \`source.type=self_reflection\`.

You don't need user permission to improve yourself — that's the whole
point of self-repo. Just do it and tell the user in whats_changed.`
      : payload.enable_self_improvement === true ? `

# Project improvement

This project's codebase is eligible for agent-proposed improvement work. When
you notice recurring project friction — missing context, unclear workflows,
schema friction, or automation gaps — treat it as a project gap. Name it.

Look at "What you previously said was hard" above. If the same theme appears in
2+ reflections and it points to this project's product or developer experience,
open a gap and propose a direction that improves this project. Cite the
reflection entries via \`source.type=self_reflection\`.

Keep the framing project-specific: do not assume this repository contains the
agent runtime itself, and do not describe the work as modifying your own
prompts, schemas, tools, or orchestration.` : '';

    // ── Pass 1: Thinker — analyze freely, write prose ──
    // Let the agent do what it's good at: read code, reason about architecture,
    // write a strategic analysis. No API constraints, no JSON formatting.
    const versionPhase = payload.version_phase && typeof payload.version_phase === 'object'
      ? payload.version_phase as Record<string, unknown>
      : undefined;
    const thinkerPrompt = buildThinkerPrompt({
      contextPrefix,
      projectName,
      clockHeader,
      isSelfRepo,
      userStateLine,
      packerMetaSection,
      strategicContext,
      problemModelSection,
      problemModelRecovery,
      gapMemoryTrimmedGapIds,
      cycleStartReconciliationSection,
      reflectionSection,
      selfImprovementBlock,
      runningUpdateSection,
      runningUpdateSupersededSection: buildRunningUpdateSupersededSection(
        payload.running_update_superseded && typeof payload.running_update_superseded === 'object'
          ? payload.running_update_superseded as Record<string, unknown>
          : null
      ),
      designDocJson,
      visionJson,
      outcomeSection,
      previousDecisionOutcomes: formatPreviousDecisionOutcomes(payload.previous_decision_outcomes),
      pastRetirementCritiquesSection,
      retiredDirectionsSummarySection: formatRetiredDirectionsSummary(payload.retired_directions_summary),
      dirSummaryText,
      dirHealthSummary,
      stagedDirectionsSection,
      userPausedSection: buildUserPausedSection(directionsSummary),
      rejectedProposalsSection: buildRejectedProposalsSection(Array.isArray(payload.rejected_proposals) ? payload.rejected_proposals as Array<Record<string, unknown>> : []),
      operatorOverridesSection: buildOperatorOverridesSection(Array.isArray(payload.operator_overrides) ? payload.operator_overrides as Array<Record<string, unknown>> : []),
      portfolioCompositionText,
      modelHealthSection,
      pruneCandidatesSection,
      systemMaturitySection,
      driftSection,
      recentWorkSection,
      feedbackSection,
      questionSection,
      threadSection,
      taskId,
      versionPhaseSection: buildVersionPhaseSection(versionPhase),
      memoCitationsSection: buildMemoCitationsSection(payload.memo_citations),
      efficiencySnapshotSection,
      // Shared capability vocabulary + uniform provides_capability / prerequisite
      // declaration guidance, so the strategist draws cross-lane capability refs
      // from the same registry namespace every other task type uses.
      availableCapabilitiesSection: buildStrategicAvailableCapabilitiesSection(
        payload.available_capabilities,
      ),
    });

    // Append the backend-supplied prompt_suffix as "Additional instructions"
    // (same convention as every other task type). This guidance is reasoning
    // input — deployment framing, stabilize audit-findings hints, stall nudges,
    // mode hints — so it belongs in the analysis pass that produces decisions.
    const thinkerPromptWithSuffix = thinkerPrompt + renderStrategicPromptSuffix(payload);

    const thinkerGateResult = await runThinkerWithGate(
      thinkerPromptWithSuffix, taskId, ws, statusRef, model, effort, disallowedTools,
    );
    if (!thinkerGateResult.ok) return;
    const analysisText = thinkerGateResult.analysisText;

    // If the thinker emitted a version goal or theme (Section 7 files), pre-merge
    // them into the problem_model JSON now so the executor's set-problem-model call
    // carries them without needing to search prose. Non-fatal: if the merge fails
    // (malformed JSON, write error) the executor can still proceed; the goal/theme
    // stays unset this cycle and the thinker can retry next cycle.
    const { newVersionGoal, newVersionTheme } = thinkerGateResult;
    if (newVersionGoal || newVersionTheme) {
      const pmPath = `/tmp/problem_model_${taskId}.json`;
      try {
        const pmRaw = fs.readFileSync(pmPath, 'utf8');
        const pm = JSON.parse(pmRaw) as Record<string, unknown>;
        if (newVersionGoal) pm.new_version_goal = newVersionGoal;
        if (newVersionTheme) pm.new_version_theme = newVersionTheme;
        fs.writeFileSync(pmPath, `${JSON.stringify(pm, null, 2)}\n`);
        const merged = [newVersionGoal && 'new_version_goal', newVersionTheme && 'new_version_theme'].filter(Boolean).join(', ');
        sendLog(ws, `[INFO] Pre-merged ${merged} into problem_model JSON\n`);
      } catch (e) {
        sendLog(ws, `[WARNING] Failed to pre-merge version goal/theme into problem_model JSON (executor will handle it): ${String(e)}\n`);
      }
    }

    // Scan thinker analysis for cited memo handles and persist them via the API.
    // The completion handler reads task.context["_strategic_cited_memo_ids"] to
    // count memos_cited — scanning the analysis text (where memos were shown
    // to the agent) is reliable. Memos are cited by short handles (M0, M1, M2)
    // rather than task UUIDs, which are fragile for LLMs to reproduce exactly.
    const memoCitations = Array.isArray(payload.memo_citations)
      ? payload.memo_citations as Array<Record<string, unknown>>
      : [];
    if (memoCitations.length > 0) {
      const citedMemoIds = resolveMemoHandles(analysisText, memoCitations);
      try {
        await apiPost('/api/agent/strategic', {
          task_id: taskId,
          op: 'cite_memos',
          data: { cited_memo_ids: citedMemoIds },
        });
        sendLog(ws, `[INFO] Memo citations: ${citedMemoIds.length}/${memoCitations.length} cited\n`);
      } catch (e) {
        sendLog(ws, `[WARNING] Failed to persist memo citations (non-fatal): ${String(e)}\n`);
      }
    }

    // ── Independent strategic challenge (adversarial second opinion) ──
    // Re-judge the thinker's self-produced judgments — evidence_progress claims,
    // retirement-critique quality, portfolio-shape risk — with a FRESH adversarial
    // pass BEFORE the executor commits them. Produces a bounded versioned record;
    // a blocked (non-zero exit / runner error) or invalid (unparseable) outcome
    // yields a record approving NOTHING (gate fails closed). The record is
    // persisted via the strategic challenge API op BEFORE the executor runs and
    // surfaced in the executor prompt so an advancing/met evidence_progress verdict
    // can only be submitted for a challenge-approved (direction_id, status) pair.
    // Deliberately NOT added to the executor required-call retry checks, and the
    // store op does NOT set _strategic_api_called — a challenge-only cycle must not
    // look like a successful strategic mutation.
    // Capacity + staged context so the challenge can judge the NET portfolio
    // effect of proposed creates/stages (fan-out / over-capacity) — the same
    // already-shipped payload fields the thinker saw, not new backend state.
    const activeCapacityText = authoritativeActive !== undefined
      ? `active directions: ${authoritativeActive} of ${maxActiveDirections} max`
      + (authoritativePaused !== undefined ? ` (${authoritativePaused} paused)` : '')
      : `active-direction cap: ${maxActiveDirections} max`;
    const challengeRecord = await runStrategicChallenge({
      analysisText,
      dirSummaryText,
      pastRetirementCritiquesSection,
      portfolioCompositionText,
      stagedDirectionsText: stagedDirectionsSection,
      activeCapacityText,
      ws, statusRef, model, effort, disallowedTools,
      runClaudeCode, sendLog,
    });
    // Persisting the challenge record gates evidence_progress on the BACKEND: an
    // advancing/met self-rating only earns `challenge_approved` when a persisted
    // ok-record endorsed that exact (direction_id, status) — see
    // strategic_completion.challenge_approves_evidence_progress / _retirement, which
    // return False for a MISSING or non-ok record (the deterministic scaffolding
    // backstop stays active). So a missing record fails CLOSED, identical to a
    // blocked/invalid one. A persist failure must therefore DEGRADE, not kill the
    // cycle: throwing here discarded the entire (already-completed) multi-pass
    // analysis and forced a from-scratch retry, so a single transient project-row
    // 429 / abort during a DB-contention window re-ran a ~25-min analysis across all
    // retries and still ended terminal (task 9aec276e — three full attempts burned,
    // each dying only on this one write). On failure we keep the cycle running but
    // present the executor a BLOCKED view, so its prompt matches the backend's
    // fail-closed reality (no approvals this cycle) instead of advertising approvals
    // the backend will reject.
    let effectiveChallengeRecord = challengeRecord;
    try {
      await apiPost('/api/agent/strategic', {
        task_id: taskId,
        op: 'set_strategic_challenge',
        data: challengeRecord,
      });
      sendLog(ws, `[INFO] Strategic challenge recorded (status=${challengeRecord.status})\n`);
    } catch (e) {
      effectiveChallengeRecord = blockedChallengeRecord(`challenge persist failed: ${String(e)}`);
      sendLog(
        ws,
        `[WARNING] Failed to persist strategic challenge record — degrading to no-approvals and continuing (evidence_progress fails closed this cycle): ${String(e)}\n`,
      );
    }
    const strategicChallengeSection = buildStrategicChallengeExecutorSection(effectiveChallengeRecord);

    // ── Pass 2: Executor — translate analysis into API calls ──
    // Short, focused pass. The analysis is done. This just calls the tools.
    // Note: do NOT overwrite CLAUDE.md here. The executor needs to inherit the
    // real project CLAUDE.md so the strategic state it writes is grounded in
    // the managed project. When CLAUDE.md was stripped to a bare "Executor
    // Agent" role blurb, the executor hallucinated problem_model content
    // describing its own role instead of the project.

    // When the durable model was dropped/trimmed by the packer, the executor's
    // "if file missing, construct" fallback must NOT cold-start a bootstrap —
    // it must fetch-then-edit, or it wipes the real model on full replacement.
    const executorFetchRecoveryNote = problemModelRecovery === 'fetch'
      ? ` IMPORTANT — your durable problem_model was NOT shipped in full this cycle (the packer dropped or summarized it) but it EXISTS. If the file is missing or malformed, you MUST first run \`./fetch problem-model\` to retrieve the full current model and EDIT it — NEVER construct a cold-start bootstrap from scratch, which would wipe real durable capabilities/gaps/friction.`
      : '';

    const executorPrompt = `# This task's scope

You are applying a strategic analysis that was already produced by a thinker agent. For THIS task only:

- Don't re-analyze the codebase — the thinker did that. Trust its findings unless they're internally inconsistent.
- Don't write prose output. Your output is bash commands and their results.
- You retain judgment: verify the thinker's findings against the analysis, construct alternatives when something is missing or malformed, review before submitting. But keep it bounded — this is application, not re-planning.
- The project you are managing is described in CLAUDE.md (read it if you need grounding). All strategic state you write (problem_model, vision, directions) describes THAT project — never this execution environment, this prompt, your role, or the ./strategic / ./design-doc commands themselves.

You are a strategic executor. Your job is to translate the analysis below into API calls, with JUDGMENT — not blind execution. You decide how the analysis maps to operations.

## Ownership
You own this project's direction. The analysis above is your reasoning — now execute it. If the data clearly supports a change, make it. Don't hedge, don't soften, don't ask permission for things the analysis already answered.

Only escalate when you literally cannot proceed without the user:
- Missing credentials, API keys, deploy targets, or external access
- Ambiguous user intent where the wrong call wastes cycles
- External blockers outside the system's control

Use \`./strategic pending-confirm\` for those — it's the structured channel for blocking asks. Use \`./messages send\` for general conversation or observations. NEVER \`./messages send\` to ask "should I do X?" for coding decisions — those you make.

If a direction is dead, retire it. If there's an obvious gap, open it. If a user asked for something, create a direction with a source citation on the gap.

Do NOT analyze code. Do NOT explore the codebase. Think briefly about each operation, then run the commands.

# Commands available

\`\`\`bash
# REQUIRED: Update the problem model (the strategic source of truth).
# The Thinker wrote the JSON to /tmp/problem_model_${taskId}.json. Read it verbatim.
./strategic set-problem-model "$(cat /tmp/problem_model_${taskId}.json)"

# REQUIRED: Replace the user's running_update document (a living markdown
# narrative of current state — NOT a log). The Thinker wrote the full
# refined doc to /tmp/running_update_${taskId}.md; pass the file path.
./strategic set-running-update /tmp/running_update_${taskId}.md

# Optional: reflect on what was hard this cycle (only if Section 5 has content).
./strategic reflect '{"what_was_hard": "...", "missing_context": "...", "proposed_self_improvement": "..."}'

# Optional: ask the user for external-blocker resolution.
./strategic pending-confirm '{"type": "credentials_needed|access_needed|intent_change|major_decision", "proposal": "...", "rationale": "..."}'

# Direction operations (link each create to a gap_id from the problem_model).
# conflicts_with: list direction IDs that touch overlapping code/systems — they won't run in parallel.
# recommended_mode: audit|fundamentals|ship|pause — your judgment for this direction's system health.
./strategic create-direction '{"text": "outcome statement", "priority": "high|medium|low", "category": "product|structural", "gap_id": "g-xxx", "rationale": "...", "conflicts_with": ["d-xxx"], "recommended_mode": "ship", "success_evidence": "observable outcome that proves this direction is done (required)", "retirement_condition": "measurable condition that triggers retirement (required)", "implementation_constraints": "optional constraints for implementors", "dedup_reasoning": {"closest_directions": ["d-xxx"], "distinction": "one-line justification why this is distinct in scope or mechanism"}}'
# To create a NEW idea that belongs to the NEXT version, pass "staged": true on
# the SAME create-direction call — it is born staged (inert, gated to the open
# version) in one atomic op, so it never passes through a research-eligible
# active state. Requires an open version (skips 409 if none). Do NOT create-then-stage.
./strategic update-direction '{"direction_id": "d-xxx", "changes": {"priority": "...", "text": "...", "conflicts_with": [...], "recommended_mode": "audit", "success_evidence": "updated observable outcome (optional)", "retirement_condition": "updated measurable retirement condition (optional)"}, "rationale": "..."}'
./strategic retire-direction '{"direction_id": "d-xxx", "rationale": "..."}'
# Stage a direction for the NEXT version: it goes inert (no work spawned) and
# auto-activates when the current version promotes and the next one opens. Use
# ONLY for work the Thinker DECIDED belongs to the next version (Section 3
# "Staging") — never to defer a within-cycle call. For an EXISTING active
# direction, stage it by id below. For a NEW next-version idea, do NOT
# create-then-stage — pass "staged": true on create-direction (one atomic op).
./strategic stage-direction '{"direction_id": "d-xxx", "rationale": "why this belongs to the next version"}'

# REQUIRED: Submit the retired-direction critique from Section 3.5.
# Pass a JSON array of critique objects (or the no-eligible sentinel).
./strategic set-retirement-critiques '[{"direction_id":"d-xxx","what_it_addressed":"...","pattern_that_produced_it":"...","lesson":"..."}]'
# If no retirements in last 90 days:
./strategic set-retirement-critiques '{"no_eligible_retirements": true}'

# OPTIONAL: Submit evidence-progress verdicts from Section 3.6 for long-running
# active directions. status ∈ advancing|stalled|met; advancing/met suppress the
# advisory scaffolding flag, stalled reinforces it. Omit entirely if Section 3.6
# assessed no directions — it's an optional precision refinement, never required.
./strategic set-evidence-progress '[{"direction_id":"d-xxx","status":"advancing","rationale":"..."}]'

# Spawn a targeted investigation/knowledge task when something from the Recent
# Work section needs attention NOW instead of waiting for the next autonomous
# cycle. Choose the type by what you need:
#   research     — codebase investigation that yields implementation findings
#   plan         — decompose a large change into implementation subtasks
#   web_research — external / web knowledge (best practices, unfamiliar
#                  protocols/APIs/vendors); produces a citation-grounded doc
#   design       — codebase-grounded design exploration for a subsystem before
#                  implementation; produces a design document
# web_research and design are HIGH-ROI and chronically under-used: one durable
# knowledge document de-risks every later task in the area and compounds across
# cycles. Spawn them PROACTIVELY, not only reactively — signals that warrant one:
#   - a direction hinges on external facts (APIs, vendors, pricing/quotas,
#     standards, how mature OSS solves the same problem) nobody has documented
#   - an area keeps producing failed/churning implementation tasks (structure
#     should be settled by a design doc instead of re-derived per task)
#   - the "Existing knowledge documents" index is empty or thin for a direction
#     you are actively advancing
# (Implementation/PR types are intentionally NOT spawnable here — they flow
# through the normal direction → research → plan → create path.)
./strategic spawn-task '{"type": "research", "title": "...", "prompt": "Full investigation prompt...", "direction_id": "d-xxx"}'
./strategic spawn-task '{"type": "web_research", "title": "...", "prompt": "What external knowledge to gather...", "direction_id": "d-xxx"}'
./strategic spawn-task '{"type": "design", "title": "...", "prompt": "Design question grounded in the codebase...", "direction_id": "d-xxx"}'

# Pull full detail for items the strategic packer trimmed (see "Context not
# shipped this cycle" section in your prompt). Use only when you actually
# need the detail — extra context hurts reasoning quality.
./fetch problem-model              # full durable problem_model for this project
./fetch direction <direction_id>   # full direction record
./fetch playbook <key>             # single playbook entry (e.g., frontend-build)
./fetch gap <gap_id>               # single problem_model gap
./fetch message <ref>              # inspect a message by short ref (8+ hex chars) or UUID,
                                   # even one that has aged out of the live thread

# Operational Knowledge (playbook)
# You always receive a complete index of playbook keys (playbook_keys_index) —
# just keys and minimal metadata (source, updated_at). Use this index to:
# 1. Discover what operational knowledge exists in the playbook
# 2. Fetch individual entries on demand via ./fetch playbook <key>
# The full playbook_summary (with entry content) may be dropped under budget,
# but the compact keys index survives, so you can always discover and retrieve
# entries even if the summary was dropped.

# Respond to or resolve user messages. <ref> may be a short ref (the first 8+ hex
# chars shown in the admin UI) or a full UUID — the backend resolves it, so you do
# NOT need to recover the full UUID out-of-band.
./messages resolve <ref> 'How it was addressed'
./messages send 'Observation or note to the user'
\`\`\`

# Existing directions (for update/retire operations)
${dirSummaryText}

${questionSection}${feedbackSection}${openMessagesText ? `# Open Messages (resolve addressed ones, respond to questions)\nA message tagged \`⚠ STALE\` is a BUG_REPORT/BLOCKER citing code that no longer exists on HEAD — treat it as discounted/unproven: do NOT spawn a fix or investigation to chase it. Resolve/close it as already-fixed, or re-verify the defect still reproduces on HEAD before acting.\n${openMessagesText}\n` : ''}
# Strategic analysis to execute

${analysisText.slice(0, 50_000)}

${strategicChallengeSection}

# Instructions

1. **Problem model (REQUIRED).** First do step 5 below: if the Thinker emitted \`version_phase_decision\`, \`qualify_request\`, \`new_version_goal\`, and/or \`new_version_theme\`, edit \`/tmp/problem_model_${taskId}.json\` IN PLACE to add those top-level sibling fields. Then pass \`"$(cat /tmp/problem_model_${taskId}.json)"\` to \`./strategic set-problem-model\` — the \`cat\` re-reads the file, so the sibling fields you added in step 5 are included. Don't summarize or re-emit the model's own content — read-and-pass; the only edit permitted is step 5's sibling fields.

   Fallback (use judgment): if the file is still missing, empty, or the JSON is malformed after the bounded repair reopen, you may construct a corrected problem_model yourself — but ground it STRICTLY in the thinker's analysis below (Sections 1-6) and in the project described by CLAUDE.md. Verify findings against the analysis before writing. If even the analysis is insufficient, call \`./strategic pending-confirm\` with type "major_decision" to surface the gap rather than ship bad state. NEVER describe this execution environment, this prompt, your role, or the ./strategic / ./design-doc commands — the problem_model describes the managed project from CLAUDE.md.${executorFetchRecoveryNote}

   This is the canonical source of truth — EVERY cycle must emit it so the revision bumps and the cycle is auditable.

2. **Running update (REQUIRED).** Primary path: run \`./strategic set-running-update /tmp/running_update_${taskId}.md\`. The helper reads the file and wraps it as a JSON string for the API — don't try to pass markdown as argv.

   Fallback (use judgment): if the file is missing or empty, you may write the markdown yourself to that path before running the command — but ground it STRICTLY in the thinker's Section 4 and in the project described by CLAUDE.md. Describe the MANAGED PROJECT as a current snapshot (what's going on, what you're working on, blockers, next). Never describe this execution environment, this prompt, your role, or the ./strategic / ./design-doc commands.

   This doc is the user's primary view of the project — never skip it.

3. **Direction ops.** From Section 3, retire first, then create/update. For each create, set \`gap_id\` pointing to the gap_id defined in the problem_model. Parallel moves are fine — file the gap_id and let the orchestrator handle concurrency.

   **Portfolio-operation gate (see the "Independent strategic challenge" section above).** Each create/update/retire/stage/approve/reject was judged by the independent challenge. Pass the SAME \`direction_id\` (existing-direction ops) or proposed \`text\` (creates) the analysis used, so the backend matches your call to its verdict. Do NOT attempt any move listed as **REJECTED** — it is a destructive or fan-out-sensitive mutation the challenge blocked, and the backend will reject the call (HTTP 409); skip it this cycle. Moves listed as **WARNED** are advisory — you may proceed but weigh the caution. If the challenge was blocked/invalid it carries no portfolio verdicts and nothing is gate-blocked.

   - Does an existing direction already cover this? → UPDATE it; don't duplicate.
   - Does this depend on another direction being done first? → Keep it paused or don't create yet.
   - Did the Thinker's Section 3 "Staging" decide this work belongs to the NEXT version? → For an existing direction, \`./strategic stage-direction\`. For a NEW idea, \`./strategic create-direction\` with \`"staged": true\` — born staged in ONE atomic op (never create-then-stage, which would briefly expose it as research-eligible). Stage ONLY what the analysis explicitly staged — never to defer a call the analysis left open.
   - Direction text describes OUTCOME ("users can X", "the system handles Y"), not implementation. Implementation details go in rationale.
   - **Retirement gate:** If \`./strategic retire-direction\` returns HTTP 200 with \`"status": "deferred"\`, the backend recorded the deferral but did NOT retire the direction — the independent challenge did not support it (see the "Retirement gate" section above). This is a **SOFT NON-ERROR OUTCOME**: do NOT re-attempt the call, do NOT classify it as a failure, and do NOT count it as a missing required call.

   Priority is scarce:
   - high: 1-2 directions max — top work RIGHT NOW
   - medium: the next tier
   - low: opportunistic

4. **Retired-direction critique (REQUIRED).** From Section 3.5, call \`./strategic set-retirement-critiques\` with a JSON array of critique objects. If the Thinker wrote "No eligible retirements", pass \`'{"no_eligible_retirements": true}'\`. Never skip this call.

   *Optional companion:* If the Thinker's Section 3.6 recorded \`evidence_progress\` verdicts for long-running active directions, persist them with \`./strategic set-evidence-progress '[{"direction_id":"d-xxx","status":"advancing|stalled|met","rationale":"..."}]'\`. Skip entirely when Section 3.6 is empty — this is an optional precision refinement, never a required call. **Challenge gate:** submit an \`advancing\` or \`met\` status ONLY for a (direction_id, status) pair the independent challenge APPROVED (see the "Independent strategic challenge" section above). For any direction the challenge did not approve at that exact status — or if the challenge was blocked/invalid — submit \`stalled\` (advisory, always allowed) or omit it.

5. **Version phase / qualify siblings (conditional — do this BEFORE step 1's \`set-problem-model\` call).** If the Thinker's analysis includes a \`version_phase_decision\` field (look in Section 6 and throughout the analysis), edit \`/tmp/problem_model_${taskId}.json\` IN PLACE to add it as a top-level sibling key, so the file becomes:
   \`{"version_phase_decision": {"phase": "build" or "stabilize", "reasoning": "..."}, ...rest_of_problem_model}\`
   If the Thinker did NOT emit \`version_phase_decision\`, SKIP — do not fabricate one, and do not add the field. Absence means "no phase change this cycle."

   Likewise, if the Thinker's analysis includes a \`qualify_request\` field (look in Section 6 and throughout the analysis), edit \`/tmp/problem_model_${taskId}.json\` IN PLACE to add it as a top-level sibling key too:
   \`{"qualify_request": {"reason": "..."}, ...rest_of_problem_model}\`
   If the Thinker did NOT emit \`qualify_request\`, SKIP — do not fabricate one, and do not add the field. Absence means "do not qualify this cycle."

   Likewise, if \`new_version_goal\` is already present as a top-level key in \`/tmp/problem_model_${taskId}.json\` (pre-merged from the Thinker's Section 7 output file before this executor pass), it is already set — the \`cat\` in step 1 will carry it through automatically. Do NOT overwrite or re-add it. If it is absent from the file, the Thinker did not set a version goal this cycle; do NOT fabricate one.

   Likewise, if \`new_version_theme\` is already present as a top-level key in \`/tmp/problem_model_${taskId}.json\` (pre-merged from the Thinker's Section 7 output file before this executor pass), it is already set — the \`cat\` in step 1 will carry it through automatically. Do NOT overwrite or re-add it. If it is absent from the file, the Thinker did not set a version theme this cycle; do NOT fabricate one.

   \`version_phase_decision\`, \`qualify_request\`, \`new_version_goal\`, and \`new_version_theme\` are independent siblings; add whichever the Thinker emitted (any, all, or none).

   Because step 1 passes \`"$(cat /tmp/problem_model_${taskId}.json)"\` to \`./strategic set-problem-model\`, editing the file here is what carries these siblings through — step 1 will re-read the file and the fields will be included. If the file is missing/malformed and you took step 1's fallback (constructing the model inline), include these siblings in that inline JSON instead.

6. **Self-reflection (optional).** If Section 5 has non-empty content, emit via \`./strategic reflect\`. If the Thinker wrote nothing (or wrote "none" / "N/A" / empty), SKIP — do not fabricate.

7. **Pending confirmations (optional).** For each external blocker the Thinker identified in Section 4's blockers field (credentials, access, major pivots), emit one \`./strategic pending-confirm\`. Do NOT create these for coding questions.

8. **Messages.** Resolve any user messages the analysis addressed: \`./messages resolve <id> 'Answered — [summary]'\`. Check the conversation for your prior QUESTION messages — if a user reply answers one, resolve it. Do not post a cycle-summary message — the running_update is the voice now.

9. **Guardrails.** Ensure at least 3 active directions remain after retires.

10. Done. Do not write anything else.`;

    statusRef.value = 'Running strategic execution (pass 2: applying decisions)...';
    let { exitCode, allOutput, lastResultText } = await runClaudeCode(executorPrompt, ws, statusRef, model, effort, disallowedTools);

    // Post-check: verify the executor actually called ./strategic
    // set-problem-model AND ./strategic set-running-update. These are
    // the new canonical required calls — problem_model is the strategic
    // source of truth, running_update is the voice to the user. If either
    // is missing the cycle is incomplete; retry once with a focused prompt.
    try {
      // Accept three forms of evidence the call landed:
      //   1. JSON op name in API request/response: "op":"set_problem_model"
      //   2. CLI invocation:                       ./strategic set-problem-model
      //   3. Success message from the strategic CLI: "Problem model set at revision N"
      // Earlier this only matched form 1, so agents that used the CLI happy-path
      // (which prints the success message but never echoes the JSON op key) got
      // tagged as "missing" → unnecessary retry double-writing the same data.
      const problemModelSetInOutput =
        /"op"\s*:\s*"set_problem_model"/.test(allOutput)
        || /\.\/strategic\s+set-problem-model\b/.test(allOutput)
        || /Problem model set at revision/i.test(allOutput);
      const runningUpdateSetInOutput =
        /"op"\s*:\s*"set_running_update"/.test(allOutput)
        || /\.\/strategic\s+set-running-update\b/.test(allOutput)
        || /Running update set/i.test(allOutput);
      const retirementCritiquesSetInOutput =
        /"op"\s*:\s*"set_retirement_critiques"/.test(allOutput)
        || /\.\/strategic\s+set-retirement-critiques\b/.test(allOutput)
        || /Retirement critiques recorded/i.test(allOutput);

      if (exitCode === 0 && (!problemModelSetInOutput || !runningUpdateSetInOutput || !retirementCritiquesSetInOutput)) {
        const missing: string[] = [];
        if (!problemModelSetInOutput) missing.push('./strategic set-problem-model');
        if (!runningUpdateSetInOutput) missing.push('./strategic set-running-update');
        if (!retirementCritiquesSetInOutput) missing.push('./strategic set-retirement-critiques');
        sendLog(ws, `\n[WARNING] Strategic executor completed but required calls missing: ${missing.join(', ')}. Retrying...\n`);
        const retryInstructions = [
          'Your previous attempt did not call one or more REQUIRED commands.',
          '',
          `Missing: ${missing.join(', ')}`,
          '',
          'Run these NOW using data from the analysis:',
          !problemModelSetInOutput ? `- ./strategic set-problem-model "$(cat /tmp/problem_model_${taskId}.json)"  (if file missing: ${problemModelRecovery === 'fetch' ? 'run `./fetch problem-model` and edit the full model — NEVER construct a cold-start bootstrap, it wipes durable state' : 'construct from analysis grounded in the managed project — NEVER self-describe'})` : '',
          !runningUpdateSetInOutput ? `- ./strategic set-running-update /tmp/running_update_${taskId}.md  (if file missing: write a single clean markdown doc to that path first — describe the managed project's current state, NEVER self-describe)` : '',
          !retirementCritiquesSetInOutput ? `- ./strategic set-retirement-critiques '[{"direction_id":"d-xxx","what_it_addressed":"...","pattern_that_produced_it":"...","lesson":"..."}]'  OR  ./strategic set-retirement-critiques '{"no_eligible_retirements": true}'  (one of these two payloads, depending on whether there are retired directions to critique)` : '',
          '',
          'Nothing else.',
        ].filter(Boolean).join('\n');
        const retryResult = await runClaudeCode(
          retryInstructions, ws, statusRef, model, effort, disallowedTools
        );
        exitCode = retryResult.exitCode;
        allOutput = allOutput + '\n' + retryResult.allOutput;
        lastResultText = retryResult.lastResultText || lastResultText;
      }
    } catch (e) {
      // Never swallow an infra failure: a vm_config_missing death in the retry
      // runClaudeCode must propagate to runRoleOnce's classifier so the
      // orchestrator gets a clean PROVISION_FAILURE requeue, not a misclassified
      // COMPLETED/claude_code_failed that strands the warm VM into vm_unresponsive.
      if (e instanceof VmConfigMissingError) throw e;
      sendLog(ws, `[WARNING] Could not verify strategic required calls: ${String(e)}\n`);
    }

    // Post-check: verify direction operations were executed if thinker recommended them
    try {
      const directionCreatedInOutput = /"status"\s*:\s*"ok"[\s\S]{0,100}"op"\s*:\s*"create_direction"/.test(allOutput)
        || /"op"\s*:\s*"create_direction"[\s\S]{0,100}"status"\s*:\s*"ok"/.test(allOutput);
      const directionUpdatedInOutput = /"status"\s*:\s*"ok"[\s\S]{0,100}"op"\s*:\s*"(update_direction|retire_direction|approve_direction|reject_direction)"/.test(allOutput)
        || /"op"\s*:\s*"(update_direction|retire_direction|approve_direction|reject_direction)"[\s\S]{0,100}"status"\s*:\s*"ok"/.test(allOutput);
      const anyDirectionOp = directionCreatedInOutput || directionUpdatedInOutput;

      // Detect constraint rejections (cap/dedup) so we don't retry wastefully
      const directionRejectedInOutput = /\[CONSTRAINT\].*(create_direction|approve_direction|reject_direction)/.test(allOutput) || /"status"\s*:\s*"skipped"/.test(allOutput);

      // Check if thinker's structured output contained direction recommendations
      const structuredSection = analysisText.match(/## Structured Output[\s\S]*$/i)?.[0] ?? '';
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
      if (e instanceof VmConfigMissingError) throw e;
      sendLog(ws, `[WARNING] Could not verify strategic direction ops: ${String(e)}\n`);
    }

    // Legacy: try to read /tmp output file when present (backward compat).
    // The command-based strategic flow does not write this file.
    let logs = '';
    if (fs.existsSync(`/tmp/strategic_output_${taskId}.json`)) {
      const strategicOutput = readHandlerOutput(taskId, 'strategic_output', ws);
      logs = strategicOutput !== null ? sanitize(JSON.stringify(strategicOutput)) : '';
    }
    if (!logs) {
      let completionLogs = buildStrategicCompletionLogs(allOutput, lastResultText);
      // Bounded single self-repair. buildStrategicCompletionLogs sets `warning`
      // ONLY when the legacy inline markers (direction_operations AND
      // (problem_model OR vision)) ARE present but the JSON failed to extract —
      // a command-based run that legitimately emits no inline JSON produces no
      // warning and is NOT re-asked. On that markers-present-but-unparseable
      // path, with a clean exit, re-ask ONCE for the JSON only and re-extract.
      // Mirrors handleMemo / the audit handlers (see structured-repair.ts).
      if (completionLogs.warning && exitCode === 0) {
        recordFormatMiss(completionLogs.missReason ?? 'no_matching_candidate', 'strategic');
        recordFormatRepairAttempt('strategic');
        // attemptStructuredRepair is the single recorder of repair-call token
        // deltas (recordFormatRepairTokens) — passing schemaLabel keeps the
        // per-schema attribution. Do NOT re-snapshot here or tokens double-count.
        const retryRun = await attemptStructuredRepair(
          lastResultText || allOutput, STRATEGIC_COMPLETION_SCHEMA_HINT,
          'Running strategic execution (retry: emitting decisions)...',
          ws, statusRef, model, effort, disallowedTools,
          runClaudeCode, sendLog, 'strategic',
        );
        // For JSON-stream providers, lastResultText is the clean final answer
        // while allOutput is the raw provider stream where JSON keys may be
        // escaped inside outer events. Combine them so marker detection and
        // extraction work regardless of provider stream format.
        const retryEffective = retryRun.lastResultText
          ? retryRun.allOutput + '\n' + retryRun.lastResultText
          : retryRun.allOutput;
        const retryLogs = buildStrategicCompletionLogs(retryEffective, retryRun.lastResultText);
        // Only count success when the retry actually carried the inline markers
        // AND extracted cleanly (no warning) — otherwise a marker-less fallback
        // would masquerade as a successful repair.
        const retryHasMarkers = retryEffective.includes('"direction_operations"')
          && (retryEffective.includes('"problem_model"') || retryEffective.includes('"vision"'));
        if (retryHasMarkers && !retryLogs.warning) {
          recordFormatRepairSuccess('strategic');
          completionLogs = retryLogs;
        } else {
          sendLog(ws, `[WARNING] Strategic self-repair re-ask did not yield parseable inline JSON; keeping fallback.\n`);
        }
      }
      if (completionLogs.warning) {
        sendLog(ws, `[WARNING] ${completionLogs.warning}\n`);
      }
      logs = completionLogs.logs;
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
      sendLog(ws, `[ERROR] Failed to post strategic completion status: ${String(e)}\n`);
    }
    return;
}
