import fs from 'fs';
import type WebSocket from 'ws';
import {
  WORKSPACE, strVal, sanitize, sendLog, readHandlerOutput,
  persistAndPostStatus, runClaudeCode,
  apiPost,
  resolveBaseBranch,
} from '../index.js';
import * as indexMod from '../index.js';
import { prepareWorkspaceForRole } from '../workspacePrep.js';
import {
  buildContextPrefix, configureAgentPermissions,
  writePlaybookHelper, writeMessagesHelper,
} from '../prompt.js';
import { writeStrategicHelpers } from './strategic-helpers.js';
import { parseBackendTimestamp } from '../utils/time.js';
import { reconcileDesignDocPresence } from '../utils/design-doc-presence.js';
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
  buildUserPausedSection,
  buildVersionPhaseSection,
  buildMemoCitationsSection,
  resolveMemoHandles,
  buildEfficiencySnapshotSection,
  buildPortfolioComposition,
} from './research-plan.js';
import {
  buildSystemMaturitySection,
  buildCapabilityVerificationSection,
  buildDriftSection,
  buildOutcomeSection,
  buildClockAndRecentWorkSection,
  buildConversationThreadSection,
  buildFeedbackSection,
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
    const openMessagesText = buildOpenMessagesText(openMessages);

    const dirSummaryText = buildDirSummaryText(directionsSummary);

    // Reconcile the design doc's systems/* and capabilities/* statuses against
    // the cloned file tree BEFORE serializing into the thinker prompt: any
    // entry marked present (mature/functional/partial / working) whose backing
    // module files are all absent on the integration branch is downgraded to
    // `missing` + annotated, so the strategic agent never reasons on a status
    // the code does not back up.
    const designDoc = reconcileDesignDocPresence(
      payload.design_doc as Record<string, unknown> | undefined,
      WORKSPACE,
    );
    const designDocJson = designDoc && Object.keys(designDoc).length > 0
      ? JSON.stringify(designDoc, null, 2)
      : '';

    // Write strategic helper scripts (strategic, fetch, design-doc)
    const projectMetadataForFetch = task.project_metadata as Record<string, unknown> | undefined;
    const fetchProjectId = (projectMetadataForFetch?.project_id as string | undefined) ?? '';
    writeStrategicHelpers(taskId, fetchProjectId, WORKSPACE);

    // ── Compute direction health summary for thinker prompt ──
    const maxActiveDirections = typeof payload.max_active_directions === 'number'
      ? payload.max_active_directions
      : 10;
    const activeDirections = directionsSummary.filter(d => strVal(d.status, 'active') === 'active');
    const dirHealthSummary = buildDirHealthSummary(directionsSummary, maxActiveDirections);

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
    );

    // ── Running context from prior cycles (design doc notes/strategic-context) ──
    const strategicContext = designDoc
      ? strVal((designDoc)['notes/strategic-context'])
      : '';

    // ── Problem model + running update + self-reflection rendering ──
    // These are the new strategic substrate — persistent durable state the
    // agent maintains across cycles. They replace vision as source of truth.
    const problemModel = payload.problem_model as Record<string, unknown> | undefined;
    const problemModelJson = problemModel && Object.keys(problemModel).length > 0
      ? JSON.stringify(problemModel, null, 2)
      : '';

    const runningUpdateDoc = typeof payload.running_update === 'string'
      ? payload.running_update
      : '';
    const runningUpdateAckAt = strVal(payload.running_update_acknowledged_at);
    const userLastActiveAt = strVal(payload.user_last_active_at);
    const selfReflectionEntries = Array.isArray(payload.self_reflection_log)
      ? payload.self_reflection_log as Array<Record<string, unknown>>
      : [];
    const isSelfRepo = payload.enable_self_improvement === true;

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
        '  - `./fetch direction <id>` — full direction record (research_history capped at 20 cycles)',
        '  - `./fetch playbook <key>` — single playbook entry (e.g. `./fetch playbook frontend-build`)',
        '  - `./fetch gap <id>` — single gap with full history/past_moves/evidence',
      );
      return lines.join('\n');
    };
    const packerMetaSection = renderPackerMeta(packerMeta);

    const problemModelSection = problemModelJson
      ? `## Your current understanding (problem_model)\n\`\`\`json\n${problemModelJson}\n\`\`\``
      : `## Your current understanding (problem_model)\n_(empty — this is the first cycle. Bootstrap from the other sections below.)_`;
    const verificationFailuresSection = buildCapabilityVerificationSection(problemModel);

    const runningUpdateSection = `## Running update (living document — rewrite, don't append)\n${renderRunningUpdateDoc(runningUpdateDoc)}\n\n_This is the document the user currently sees. Refine it this cycle — remove stale content, integrate what's new, keep it coherent and short. It's a current snapshot, not a log._`;

    const reflectionSection = `## What you previously said was hard\n${renderReflectionList(selfReflectionEntries)}`;

    const pastRetirementCritiquesSection = formatPastRetirementCritiques(payload.past_retirement_critiques);

    const selfImprovementBlock = payload.enable_self_improvement === true ? `

# Self-improvement

This project's codebase includes your own prompts, schemas, tools, and
orchestration. When you notice a recurring limitation in yourself — missing
context, unclear instruction, schema friction — it's a gap. Name it.

Look at "What you previously said was hard" above. If the same theme
appears in 2+ reflections, open a gap tagged \`self_improvement\` and
propose a direction that modifies the code governing your behavior.
Cite the reflection entries via \`source.type=self_reflection\`.

You don't need user permission to improve yourself — that's the whole
point of self-repo. Just do it and tell the user in whats_changed.` : '';

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
      verificationFailuresSection,
      reflectionSection,
      selfImprovementBlock,
      runningUpdateSection,
      designDocJson,
      visionJson,
      outcomeSection,
      previousDecisionOutcomes: formatPreviousDecisionOutcomes(payload.previous_decision_outcomes),
      pastRetirementCritiquesSection,
      retiredDirectionsSummarySection: formatRetiredDirectionsSummary(payload.retired_directions_summary),
      dirSummaryText,
      dirHealthSummary,
      userPausedSection: buildUserPausedSection(directionsSummary),
      rejectedProposalsSection: buildRejectedProposalsSection(Array.isArray(payload.rejected_proposals) ? payload.rejected_proposals as Array<Record<string, unknown>> : []),
      portfolioCompositionText,
      systemMaturitySection,
      driftSection,
      recentWorkSection,
      feedbackSection,
      threadSection,
      taskId,
      versionPhaseSection: buildVersionPhaseSection(versionPhase),
      memoCitationsSection: buildMemoCitationsSection(payload.memo_citations),
      efficiencySnapshotSection,
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

    // ── Pass 2: Executor — translate analysis into API calls ──
    // Short, focused pass. The analysis is done. This just calls the tools.
    // Note: do NOT overwrite CLAUDE.md here. The executor needs to inherit the
    // real project CLAUDE.md so the strategic state it writes is grounded in
    // the managed project. When CLAUDE.md was stripped to a bare "Executor
    // Agent" role blurb, the executor hallucinated problem_model content
    // describing its own role instead of the project.

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
./strategic update-direction '{"direction_id": "d-xxx", "changes": {"priority": "...", "text": "...", "conflicts_with": [...], "recommended_mode": "audit"}, "rationale": "..."}'
./strategic retire-direction '{"direction_id": "d-xxx", "rationale": "..."}'

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

# Spawn a targeted investigation task (research or plan only) when something
# from the Recent Work section needs investigation NOW instead of waiting for
# the next autonomous cycle.
./strategic spawn-task '{"type": "research", "title": "...", "prompt": "Full investigation prompt...", "direction_id": "d-xxx"}'

# Pull full detail for items the strategic packer trimmed (see "Context not
# shipped this cycle" section in your prompt). Use only when you actually
# need the detail — extra context hurts reasoning quality.
./fetch direction <direction_id>   # full direction record
./fetch playbook <key>             # single playbook entry
./fetch gap <gap_id>               # single problem_model gap

# Respond to or resolve user messages
./messages resolve <message-id> 'How it was addressed'
./messages send 'Observation or note to the user'
\`\`\`

# Existing directions (for update/retire operations)
${dirSummaryText}

${feedbackSection}${openMessagesText ? `# Open Messages (resolve addressed ones, respond to questions)\n${openMessagesText}\n` : ''}
# Strategic analysis to execute

${analysisText.slice(0, 50_000)}

# Instructions

1. **Problem model (REQUIRED).** First do step 5 below: if the Thinker emitted \`version_phase_decision\` and/or \`qualify_request\`, edit \`/tmp/problem_model_${taskId}.json\` IN PLACE to add those top-level sibling fields. Then pass \`"$(cat /tmp/problem_model_${taskId}.json)"\` to \`./strategic set-problem-model\` — the \`cat\` re-reads the file, so the sibling fields you added in step 5 are included. Don't summarize or re-emit the model's own content — read-and-pass; the only edit permitted is step 5's sibling fields.

   Fallback (use judgment): if the file is missing, empty, or the JSON is malformed, you may construct a corrected problem_model yourself — but ground it STRICTLY in the thinker's analysis below (Sections 1-6) and in the project described by CLAUDE.md. Verify findings against the analysis before writing. If even the analysis is insufficient, call \`./strategic pending-confirm\` with type "major_decision" to surface the gap rather than ship bad state. NEVER describe this execution environment, this prompt, your role, or the ./strategic / ./design-doc commands — the problem_model describes the managed project from CLAUDE.md.

   This is the canonical source of truth — EVERY cycle must emit it so the revision bumps and the cycle is auditable.

2. **Running update (REQUIRED).** Primary path: run \`./strategic set-running-update /tmp/running_update_${taskId}.md\`. The helper reads the file and wraps it as a JSON string for the API — don't try to pass markdown as argv.

   Fallback (use judgment): if the file is missing or empty, you may write the markdown yourself to that path before running the command — but ground it STRICTLY in the thinker's Section 4 and in the project described by CLAUDE.md. Describe the MANAGED PROJECT as a current snapshot (what's going on, what you're working on, blockers, next). Never describe this execution environment, this prompt, your role, or the ./strategic / ./design-doc commands.

   This doc is the user's primary view of the project — never skip it.

3. **Direction ops.** From Section 3, retire first, then create/update. For each create, set \`gap_id\` pointing to the gap_id defined in the problem_model. Parallel moves are fine — file the gap_id and let the orchestrator handle concurrency.

   - Does an existing direction already cover this? → UPDATE it; don't duplicate.
   - Does this depend on another direction being done first? → Keep it paused or don't create yet.
   - Direction text describes OUTCOME ("users can X", "the system handles Y"), not implementation. Implementation details go in rationale.

   Priority is scarce:
   - high: 1-2 directions max — top work RIGHT NOW
   - medium: the next tier
   - low: opportunistic

4. **Retired-direction critique (REQUIRED).** From Section 3.5, call \`./strategic set-retirement-critiques\` with a JSON array of critique objects. If the Thinker wrote "No eligible retirements", pass \`'{"no_eligible_retirements": true}'\`. Never skip this call.

   *Optional companion:* If the Thinker's Section 3.6 recorded \`evidence_progress\` verdicts for long-running active directions, persist them with \`./strategic set-evidence-progress '[{"direction_id":"d-xxx","status":"advancing|stalled|met","rationale":"..."}]'\`. Skip entirely when Section 3.6 is empty — this is an optional precision refinement, never a required call.

5. **Version phase / qualify siblings (conditional — do this BEFORE step 1's \`set-problem-model\` call).** If the Thinker's analysis includes a \`version_phase_decision\` field (look in Section 6 and throughout the analysis), edit \`/tmp/problem_model_${taskId}.json\` IN PLACE to add it as a top-level sibling key, so the file becomes:
   \`{"version_phase_decision": {"phase": "build" or "stabilize", "reasoning": "..."}, ...rest_of_problem_model}\`
   If the Thinker did NOT emit \`version_phase_decision\`, SKIP — do not fabricate one, and do not add the field. Absence means "no phase change this cycle."

   Likewise, if the Thinker's analysis includes a \`qualify_request\` field (look in Section 6 and throughout the analysis), edit \`/tmp/problem_model_${taskId}.json\` IN PLACE to add it as a top-level sibling key too:
   \`{"qualify_request": {"reason": "..."}, ...rest_of_problem_model}\`
   If the Thinker did NOT emit \`qualify_request\`, SKIP — do not fabricate one, and do not add the field. Absence means "do not qualify this cycle." \`version_phase_decision\` and \`qualify_request\` are independent siblings; add whichever the Thinker emitted (either, both, or neither).

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
          !problemModelSetInOutput ? `- ./strategic set-problem-model "$(cat /tmp/problem_model_${taskId}.json)"  (if file missing: construct from analysis grounded in the managed project — NEVER self-describe)` : '',
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
      sendLog(ws, `[WARNING] Could not verify strategic required calls: ${String(e)}\n`);
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
      const completionLogs = buildStrategicCompletionLogs(allOutput, lastResultText);
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
