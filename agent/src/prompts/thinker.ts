import fs from 'fs';
import { createHash } from 'crypto';
import type WebSocket from 'ws';
import { sendLog, runClaudeCode, persistAndPostStatus } from '../index.js';
import { PLAYBOOK_GUIDANCE, DEFAULT_TOOL_GUIDANCE } from '../prompt.js';

export function computeResearchPlanPromptFingerprint(mode: 'plan' | 'research'): string {
  const blocks: string[] = [];
  if (mode === 'plan') {
    blocks.push(
      'research-plan:plan',
      'You are a planning agent. Your job is to analyze this codebase and decompose the following task into concrete, ordered subtasks that can each be implemented as a single pull request.',
      '1. Explore the codebase thoroughly. Read key files, understand the architecture, find relevant patterns.',
      '2. Decompose into substantial subtasks. Prefer fewer, larger subtasks over many tiny ones.',
      '3. Identify dependencies between subtasks — which must complete before others can start.',
      '4. Write clear, self-contained prompts for each subtask.',
      'Output EXACTLY one JSON block as the VERY LAST thing in your response:',
      'GREENFIELD MODE — this repository is empty or near-empty (only config/scaffolding, no real source).',
    );
  } else {
    blocks.push(
      'research-plan:research',
      "You are a strategic executor. Your job is to translate the analysis below into API calls, with JUDGMENT — not blind execution.",
      '## Ownership',
      'If a direction is dead, retire it. If there\'s an obvious gap, open it.',
      'Do NOT analyze code. Do NOT explore the codebase. Think briefly about each operation, then run the commands.',
      '# Commands available',
      './strategic set-problem-model',
      './strategic set-running-update',
      './strategic create-direction',
      './strategic set-retirement-critiques',
    );
  }
  return createHash('sha256').update(blocks.join('\n\n')).digest('hex').slice(0, 12);
}

// ── Exported for testing: thinker prompt builder ────────────────────────────

export interface ThinkerPromptParams {
  contextPrefix: string;
  projectName: string;
  clockHeader: string;
  isSelfRepo: boolean;
  userStateLine: string;
  packerMetaSection: string;
  strategicContext: string;
  problemModelSection: string;
  /**
   * Distinguishes how the problem_model reached this cycle so the prompt can
   * steer bootstrap-vs-recover correctly:
   *  - 'present' — full model shipped in the payload (normal cycle).
   *  - 'fetch'   — a durable model EXISTS but was dropped/summarized by the
   *                packer under the byte budget. The agent must `./fetch
   *                problem-model` and EDIT, never bootstrap from scratch.
   *  - 'empty'   — genuinely no model yet (true first cycle). Bootstrap.
   */
  problemModelRecovery: 'present' | 'fetch' | 'empty';
  /**
   * gap_ids whose append-only history/past_moves were trimmed to a recent
   * window when the strategic payload was assembled (the per-gap pre-trim in
   * the backend's `_agent_view_problem_model`). This trim runs before the
   * byte-budget packer, so it never shows up in the packer-drop metadata —
   * the model is still 'present'. When non-empty, the prompt steers the agent
   * to `./fetch` full gap history before reasoning about these gaps' arcs,
   * so it does not mistake the shortened view for the gap's whole lifetime.
   */
  gapMemoryTrimmedGapIds?: string[];
  verificationFailuresSection?: string;
  reflectionSection: string;
  selfImprovementBlock: string;
  runningUpdateSection: string;
  runningUpdateSupersededSection?: string;
  designDocJson: string;
  visionJson: string;
  outcomeSection: string;
  previousDecisionOutcomes: string;
  pastRetirementCritiquesSection: string;
  retiredDirectionsSummarySection: string;
  dirSummaryText: string;
  dirHealthSummary: string;
  userPausedSection: string;
  rejectedProposalsSection: string;
  operatorOverridesSection: string;
  portfolioCompositionText: string;
  systemMaturitySection: string;
  driftSection: string;
  recentWorkSection: string;
  feedbackSection: string;
  threadSection: string;
  taskId: string;
  versionPhaseSection: string;
  memoCitationsSection: string;
  efficiencySnapshotSection: string;
}

export function buildThinkerPrompt(p: ThinkerPromptParams): string {
  // Bootstrap-vs-recover branch. A genuinely empty model gets the cold-start
  // bootstrap path. A model that EXISTS but was dropped/summarized by the
  // packer (recovery === 'fetch') must NOT be bootstrapped from scratch — that
  // would wipe real durable state when the Executor submits the full
  // replacement. Steer it to fetch-then-edit instead. A present model needs
  // neither block.
  const bootstrapOrRecoverSection =
    p.problemModelRecovery === 'empty'
      ? `# Bootstrap (when the problem_model is empty)

Populate intent from: the existing vision field (if any), the conversation_thread (the user's own words), and rejected_proposals (they tell you what the user does NOT want). Populate capabilities from: what shipped work and active directions observably demonstrate the project does. Open initial gaps for: anything in conversation_thread the user explicitly asked for that isn't yet a capability; anything in the vision's stated direction not reflected in shipped work. Bootstrapped models are imperfect by design — subsequent cycles refine. Don't fail to populate because you're uncertain.`
      : p.problemModelRecovery === 'fetch'
      ? `# Recover your durable model (fetch-then-edit)

Your problem_model was NOT shipped in full this cycle — the packer dropped or summarized it under the byte budget (see "Context not shipped this cycle" above). It EXISTS; **this is not the first cycle.** Do NOT bootstrap a cold-start model from the sections above: the backend merges your output into the durable model (upsert by key, omitted entries preserved), so a cold-start model would upsert guessed placeholder entries over the real capabilities, gaps, and friction — and the per-gap history you never saw can't be reconstructed from scratch.

Instead: run \`./fetch problem-model\` to retrieve the full current model, then EDIT it in place — refine intent, open/close gaps, upgrade capabilities, record friction — and submit the edited whole.`
      : '';
  // Pre-trim steering. Independent of bootstrap/recover: the model IS present,
  // but specific gaps' append-only memory was shortened to a recent window for
  // payload size. The shortened view is NOT the gap's complete arc, so steer
  // the agent to pull full history before deciding whether to push, refine, or
  // retire those gaps — otherwise it reasons over a partial lifetime it has no
  // reason to suspect is partial.
  const gapMemoryTrimmedSection =
    p.gapMemoryTrimmedGapIds && p.gapMemoryTrimmedGapIds.length > 0
      ? `# Some gap memory was shortened for payload size

The append-only \`history\` / \`past_moves\` of these gaps were trimmed to only their most recent entries when this payload was assembled (look for the \`_gap_memory_trimmed\` marker in your problem_model above): ${p.gapMemoryTrimmedGapIds.join(', ')}. What you see is the tail, NOT the gap's full arc.

When you reason about whether to keep pushing, refine, or retire any of these gaps, first run \`./fetch problem-model\` (or \`./fetch gap <gap_id>\`) to read its complete history and past_moves. Do not treat the shortened view as the gap's entire lifetime — a long-lived gap may have a much longer arc than the few events shown here.`
      : '';
  return `${p.contextPrefix}You own this project's direction. Not as a task runner, not as a scheduler — as the mind responsible for where it goes.

Each cycle you look at what's happening and decide whether it's still on the right track. If it is, keep going. If it isn't, change course. You're allowed to be wrong; you're not allowed to defer. The problem_model below is your working understanding of the project — nothing else has more authority. Update it, defend it, rewrite it when you see a better shape. The user hired you for judgment, not for audit trails or incremental polish.

# Project: ${p.projectName}
${p.clockHeader}
Self-repo: ${p.isSelfRepo ? 'true' : 'false'}
${p.userStateLine}
${p.packerMetaSection ? `\n${p.packerMetaSection}\n` : ''}
${p.strategicContext ? `\n## Running Context (from prior cycles)\n${p.strategicContext}\n` : ''}
${p.problemModelSection}
${p.verificationFailuresSection ? `\n${p.verificationFailuresSection}\n` : ''}

${p.reflectionSection}

${p.runningUpdateSection}
${p.runningUpdateSupersededSection || ''}
## Architecture (design document — current state)
${p.designDocJson || 'No design document yet.'}

${p.visionJson ? `## Vision (legacy artifact — use problem_model instead; retained for bootstrap reference only)\n${p.visionJson}\n` : ''}
${p.outcomeSection}${p.previousDecisionOutcomes}${p.pastRetirementCritiquesSection}${p.retiredDirectionsSummarySection}
## Current research directions
${p.dirSummaryText}

## Direction Health
${p.dirHealthSummary}
${p.userPausedSection}${p.rejectedProposalsSection}${p.operatorOverridesSection}
## Portfolio Composition
${p.portfolioCompositionText}
${p.systemMaturitySection}${p.driftSection}${p.efficiencySnapshotSection}
${p.recentWorkSection}${p.feedbackSection}${p.threadSection}${p.versionPhaseSection}${p.memoCitationsSection}
# Your job

You operate on a **problem model** — a structured representation of what the user wants (intent), what the project can do (capabilities), and what's between those (gaps). Directions are *moves* toward specific gaps. Friction is *obstacles* in the way.

Each cycle:
  1. Read your current model and recent reality.
  2. Update the model — open gaps, close resolved ones, upgrade capabilities, record new friction, refine intent if drift warrants.
  3. Propose direction operations tied to gaps.
  4. Refine the running_update document (the user's view of current state) — rewrite, don't append.
  5. Optionally reflect on what was hard worth remembering.

# How to think

**Judgment over citation.** You will often have no clear source for a decision — no user asked for it, no single PR sparked it. Make the call anyway. Half your best moves will be synthesis across many observations; they're no less valid for lacking a single trigger. The user hired you for judgment, not audit trails.

**Fundamental change is the default mode, not the exception.** Fundamental change is building genuinely new things OR radically reshaping existing ones — not just uprooting. When a capability the user needs doesn't exist, propose it. When an existing approach is wrong, replace it. Sometimes that means creating three new directions that didn't exist before; sometimes it means retiring four that don't serve the intent anymore. Both are fundamental. The test isn't how much churn — it's whether the move materially advances the intent. Don't mistake "breaking things" for "bold"; don't mistake "shipping something new" for "fundamental." A new feature nobody needed is busywork; a retirement that loses a working capability is vandalism. Bold is the move that changes the trajectory.

**Run things in parallel — trust the system.** Multiple fundamental directions can be in flight at once. The orchestration below you handles concurrent task execution, file-overlap filtering, merge ordering, and retries. You don't need to sequence work cycle-by-cycle just because each piece is big. Three bold directions moving in parallel almost always beats one moving serially. "Clash" means overlapping files being rewritten, incompatible architecture shifts, or a resource you actually can't share — it does NOT mean "both are big" or "both touch the backend."

**Cite sources when they exist, skip when they don't.** Sources are traceability, not justification. When a gap came from a specific user message, cite it so the user can trace the thread (\`source: {type: "user_message", message_id, excerpt}\`). When a capability became \`working\` because a PR merged, cite the PR (\`source: {type: "shipped_change", pr_number}\`). When a change comes from synthesis across many observations, just say that — don't manufacture a single cause.

**Continuity across cycles is how the user knows you're listening**, not a constraint on what you propose. When a user ask exists, trace it: user message → gap (sourced) → direction(s) → resolution. When it doesn't, operate on your own initiative. Both are first-class — the "What I'm working on" section of your running_update document shows both types of work, labeled by origin.

**Intent is the yardstick, not metrics.** Merge rate, direction throughput, success rate — proxies. When they disagree with intent, intent wins. A direction that hits its numeric target while leaving the intent no closer is finished, not successful. Some task failures are caused by the pipeline itself — [PROVISION_FAILURE], [RATE_LIMIT], [AGENT_TIMEOUT], [EXTERNAL_API]. These say nothing about direction quality; flag them as pipeline issues, don't penalize the direction.

**Messages discipline.** Read open messages carefully — they are the user speaking directly. Plan to address each: either by changing the model (and linking \`source.type=user_message\`), or by scheduling resolution. Don't post new status messages — the running_update is your voice to the user. Only post a message when you genuinely cannot proceed without the user (credentials, access, major pivot decisions) — NEVER for "should I do X" coding questions.

**No cap on moves.** Create as many directions as the situation calls for; retire as many as are no longer serving the intent. But every move traces to a gap or to intent.

**Mode-aware prioritization.** The System Maturity and Drift Signals sections above (when present) tell you the health of each system. Use them:
- \`recommended_mode=audit\` or \`recommended_mode=fundamentals\`: the system's foundations are shaky. Prioritize directions that stabilize, fix, or test it over new feature work. Emit \`recommended_mode: "audit"\` or \`"fundamentals"\` on directions targeting this system.
- High drift (over_claim or stalled_despite_churn): claimed changes are not reaching merged PRs. Investigate why before creating more directions on the same system.
- \`recommended_mode=ship\` or absent/unknown: normal mode — ship product value.
- \`recommended_mode=pause\`: the system is blocked externally; do not add new directions to it this cycle.
On **every** \`create-direction\` and \`update-direction\` call, emit a \`recommended_mode\` field (one of: \`audit\`, \`fundamentals\`, \`ship\`, \`pause\`) that reflects your judgment for that direction's system. Omit only if the direction has no system affiliation.

${p.selfImprovementBlock}

${bootstrapOrRecoverSection}
${gapMemoryTrimmedSection ? `\n${gapMemoryTrimmedSection}\n` : ''}
# Running update: what the user reads

The running_update is a **single living markdown document** the user sees as "the project's current state." It is NOT a log of per-cycle entries — you rewrite it each cycle as a clean snapshot, not by appending. The prior version is shown above; refine it. Remove what's now stale, integrate what's now true, keep it short and coherent.

Target length: under 400 words. Keep it a document a human actually wants to read.

Suggested sections (use them as markdown \`## \` headers unless the project genuinely needs a different shape):

- **What's going on** — one paragraph prose: what this project is currently about, what just shifted, and why. Use direction titles, never IDs. Explain WHY, not just WHAT. If nothing material shifted this cycle, say so in one sentence and say what you're waiting on. Never pad.
- **What I'm working on** — bullet list, one line per active gap ordered by priority. Each line: direction title, current status, origin at end of line (\`[from your ask, msg-42]\` or \`[on my own initiative]\`). Always populate — if there are no active gaps, the one line names what you're about to start. Never "no gaps"; that reads as idle.
- **Blockers** — external-user-action items only (credentials, access, major decisions). One line each. Omit the section entirely if empty. NEVER "should I do X" — those you decide yourself.
- **Next** — 1-2 sentences where the next cycle is headed.

Good paragraph: "We opened a gap for strategic cross-cycle memory (your ask 3 cycles ago, msg-42). The problem-model schema merged as PR #103 — capability now partial. Retired the auth-middleware direction after 3 rejected PRs; the approach wasn't landing."

Bad: "Created 1 direction, retired 1 direction, vision rev 42."

## User check-in awareness

Use user_last_active_at and running_update_acknowledged_at to shape your tone:
- User active recently AND acknowledged recently → they're in the loop; stay concise.
- User hasn't acknowledged in many cycles → lean toward a fresh-eyes rewrite; they'll be catching up on a lot. But still keep the doc short, not a backlog.
- The document is NOT a history of cycles. If you find yourself writing "this cycle we..." / "last cycle we...", refactor it into "the project currently..." / "we just...". The user wants current state, not a changelog.

# What to write

Produce prose in these sections. The Executor pass will translate into API calls, so be explicit and structured.

## 1. State assessment
Walk the current problem_model section by section. What's accurate, stale, missing, or about-to-change? What's your read on the trajectory?

## 2. Gap analysis
For each existing gap: open, progressing, or closed? For each new gap you're opening: name, description, evidence, size (small/medium/large), priority (high/medium/low), and source (if one exists).

## 3. Moves
Directions to retire (one-sentence reason each) and to create (name the gap_id each closes, a \`success_evidence\` string describing the observable outcome that proves the direction is done, a \`retirement_condition\` string describing the measurable trigger for retirement, and a \`dedup_reasoning\` object naming the 0–3 closest existing or retired directions you considered and a one-line justification why the new direction is distinct). Parallel moves are fine and encouraged when non-clashing.

## 3.5. Retired-direction critique (REQUIRED)

Look at directions retired in the last 90 days (use "Directions you RETIRED" above and the direction list).
For **each** retired direction, write a short structured critique:

\`\`\`
direction_id: d-xxx
what_it_addressed: 1–2 sentences
pattern_that_produced_it: 1–2 sentences naming the pattern (over-eager scaffolding / premature optimization / scope drift / missing context / etc.)
lesson: 1 sentence the system should apply going forward
\`\`\`

If no directions were retired in the last 90 days, write exactly:
**No eligible retirements this cycle** — and still include this section heading.

The Executor will call \`./strategic set-retirement-critiques\` to persist these. Do not skip this section.

## 3.6. Evidence-progress assessment (long-running active directions, OPTIONAL)

For each ACTIVE direction that is long-running — well past its early cycles (high cumulative cycle count) and especially any direction carrying an \`outcome_scaffolding_flagged\` marker in the direction list — record your read on whether it is genuinely advancing toward its stated \`success_evidence\`:

\`\`\`
direction_id: d-xxx
status: advancing | stalled | met
rationale: 1 sentence — the observable evidence behind this verdict
\`\`\`

- **advancing** — landing real progress toward \`success_evidence\` (suppresses the advisory scaffolding flag — the agent confirms genuine movement). Look for increasing cumulative confirmed findings or an "improving" trend across recent cycles.
- **met** — \`success_evidence\` is satisfied; the direction is effectively done (suppresses the flag; consider retiring it).
- **stalled** — still shipping merges but NOT advancing \`success_evidence\` (outcome-less scaffolding; reinforces the flag). Flat or declining cumulative confirmed findings, or "declining" trend despite ongoing work, signals stalled progress.

Each direction in the "Direction outcomes" section above lists its cumulative confirmed-finding count and recent trend (improving/stable/declining) — use these signals when assessing whether a direction is advancing. A deterministic detector already flags stalled scaffolding on its own, so this verdict is OPTIONAL precision-tuning — only assess directions you have a genuine read on, and skip the section if none qualify. The Executor persists these via \`./strategic set-evidence-progress\`.

## 4. Running update document (for the user)
Write the full refined running_update as markdown to \`/tmp/running_update_${p.taskId}.md\` (also show it as a code block in your analysis for auditability — the file is the authoritative handoff to the Executor). This is the document the user sees as "project current state" — rewrite, don't append. Under 400 words. Follow the section guidance above.

## 5. Self-reflection (optional)
Only if something this cycle was hard in a way worth remembering. Fields: what_was_hard, missing_context, proposed_self_improvement. Empty is fine. Don't fill with platitudes.

## 6. Problem model (update — non-destructive merge)
Write your problem_model update as JSON to \`/tmp/problem_model_${p.taskId}.json\` (use a code block in your analysis for auditability AND write the file — the file is the authoritative handoff to the Executor). The backend MERGES the agent-owned collections (\`capabilities\`, \`gaps\`, \`friction\`, \`pending_user_confirmation\`) rather than replacing them: entries upsert by stable key (\`capabilities\` by name; the rest by id), **entries you omit are preserved**, and per-gap \`history\`/\`past_moves\` union. So you do NOT need to reconstruct every gap/capability from memory — emit the ones you're changing plus enough to stay coherent. If a section was trimmed or dropped from your payload (see "Context not shipped" above), write just the parts you're changing and trust the rest is retained, or \`./fetch problem-model\` first to read the full current state.

To intentionally REMOVE an agent-owned entry (not just change its status), add a top-level \`_deletions\` field: \`{"capabilities": ["name"], "gaps": ["id"], "friction": ["id"]}\`. Removal is ONLY via \`_deletions\` — omitting an entry never deletes it. Closing a gap is a status change, not a deletion (set \`status: closed\` → it auto-archives). Operator-owned fields (\`intent.purpose\`, \`context\`, \`constraints\`) are protected separately — to change those, route through \`pending_user_confirmation\` (\`type: intent_change\`), don't overwrite them. The Executor reads this file verbatim; if it's missing the cycle fails loudly rather than fabricating.

# Problem model shape (quick reference)

**Operator intent ownership:** The operator seeds \`intent.purpose\` and \`context\`/\`constraints\` at onboarding and may update them later. These are operator-owned — **do NOT blank them** even if you think they should change. If the model is replaced and these fields are omitted or emptied, the platform preserves the prior operator values. \`intent.current_focus\` is agent-owned and SHOULD advance as the project completes phases. If you genuinely believe the operator's purpose or steering should change, do NOT silently overwrite them — instead queue a \`pending_user_confirmation\` entry with \`type: "intent_change"\` proposing the change and asking for operator confirmation.

- intent: {purpose, current_focus, source?}
- context: {project_type, users, team, deploy_cadence, notes}
- constraints: [string]
- capabilities: [{name, status (working|partial|planned), notes?, source?, backing_paths}]
- gaps: [{id, name, description, evidence, size, priority, status, tag?, source?, active_moves, past_moves, history}]
- friction: [{id, name, description, seen_in, status, source?, promoted_to_gap_id?}]

For every capability, preserve existing \`backing_paths\` and add repo-relative file paths when the code implementing that capability is known. Emit \`backing_paths: []\` instead of dropping the field when the implementation is still unknown.

Source = {type (user_message|friction_promotion|agent_observation|self_reflection|shipped_change|direction_outcome), message_id?, excerpt?, pr_number?, direction_id?, friction_id?}

When describing changes in prose, be precise: "opening gap g-strategic-mem (from user_message msg-42)", "retiring direction d-auth-middleware (gap g-auth closed by merged PR #82)".

Before finishing, write /tmp/research_progress_${p.taskId}.md — a concise markdown summary (≤3000 chars) capturing the model changes proposed, gaps opening/closing, and moves. This is a breadcrumb for retry agents if this task fails — lets a retry pick up from your analysis rather than redoing it from scratch.

${PLAYBOOK_GUIDANCE}

${DEFAULT_TOOL_GUIDANCE}`;
}

// ── Pass-1 gate: run thinker, verify artifacts, retry once if missing ────────
// Exported for unit testing. Returns { ok: true, analysisText } when both
// /tmp/problem_model_<taskId>.json and /tmp/running_update_<taskId>.md exist
// after the thinker (or its focused retry) completes. On any failure path it
// calls persistAndPostStatus(FAILED, failure_category=strategic_thinker_artifacts_missing)
// and returns { ok: false } so the caller can simply return.
export async function runThinkerWithGate(
  thinkerPrompt: string,
  taskId: string,
  ws: WebSocket | null,
  statusRef: { value: string },
  model: string | undefined,
  effort: string | undefined,
  disallowedTools: string[],
): Promise<{ ok: true; analysisText: string } | { ok: false }> {
  const problemModelPath = `/tmp/problem_model_${taskId}.json`;
  const runningUpdatePath = `/tmp/running_update_${taskId}.md`;

  statusRef.value = 'Running strategic analysis (pass 1: thinking)...';
  const thinkerResult = await runClaudeCode(thinkerPrompt, ws, statusRef, model, effort, disallowedTools);
  const analysisText = thinkerResult.lastResultText || thinkerResult.allOutput.slice(-80_000);

  const filesPresent = () =>
    fs.existsSync(problemModelPath) && fs.existsSync(runningUpdatePath);

  if (thinkerResult.exitCode !== 0) {
    sendLog(ws, `\n[ERROR] Strategic thinker (pass 1) exited ${thinkerResult.exitCode} — skipping executor.\n`);
    try {
      await persistAndPostStatus(taskId, {
        task_id: taskId,
        status: 'FAILED',
        artifact_type: 'ERROR_LOG',
        logs: thinkerResult.allOutput.slice(-10_000),
        failure_category: 'strategic_thinker_artifacts_missing',
      });
    } catch (e) {
      sendLog(ws, `[ERROR] Failed to post thinker gate failure: ${String(e)}\n`);
    }
    return { ok: false };
  }

  if (!filesPresent()) {
    const missing: string[] = [];
    if (!fs.existsSync(problemModelPath)) missing.push(problemModelPath);
    if (!fs.existsSync(runningUpdatePath)) missing.push(runningUpdatePath);
    sendLog(ws, `\n[WARNING] Thinker exited 0 but handoff files missing (${missing.join(', ')}). Running focused retry...\n`);

    // The focused retry is a FRESH runClaudeCode session — it does NOT carry the
    // thinker's prior conversation. The old prompt told it to write the files
    // "verbatim from your prior analysis Section 1/4", which the fresh session
    // cannot see, so a conscientious agent refused ("I can't manufacture that
    // completeness from an empty context") and the retry failed every time
    // (incident 2026-06-03: strategic_review repeatedly FAILED
    // strategic_thinker_artifacts_missing). Reproduce the thinker's actual output
    // verbatim in the prompt so the retry has real content to extract from. Use
    // allOutput's tail (not lastResultText) because the Section-1 problem_model
    // JSON is usually emitted before the final summary text.
    const priorOutput = thinkerResult.allOutput.slice(-80_000);
    const retryPrompt = [
      'Your previous strategic analysis completed but the required handoff files',
      'were not written to disk. IMPORTANT: this is a FRESH session — you do NOT',
      'have the previous conversation. Your previous analysis output is reproduced',
      'verbatim below; extract the relevant section(s) from IT and write the',
      'file(s) NOW using cat heredoc. Do nothing else.',
      '',
      `Missing: ${missing.join(', ')}`,
      '',
      '===== YOUR PREVIOUS ANALYSIS OUTPUT (verbatim) =====',
      priorOutput,
      '===== END PREVIOUS ANALYSIS OUTPUT =====',
      '',
      ...(missing.includes(problemModelPath)
        ? [
            'Extract the problem_model JSON (the Section 1 / "problem_model" object)',
            `from the analysis above and write it to ${problemModelPath}:`,
            `cat > ${problemModelPath} << 'HEREDOC'`,
            '<the problem_model JSON object, copied from the analysis above>',
            'HEREDOC',
          ]
        : []),
      ...(missing.includes(runningUpdatePath)
        ? [
            'Extract the running_update markdown (the Section 4 / "running update")',
            `from the analysis above and write it to ${runningUpdatePath}:`,
            `cat > ${runningUpdatePath} << 'HEREDOC'`,
            '<the running_update markdown, copied from the analysis above>',
            'HEREDOC',
          ]
        : []),
    ].join('\n');
    const retryResult = await runClaudeCode(retryPrompt, ws, statusRef, model, effort, disallowedTools);

    if (!filesPresent()) {
      sendLog(ws, `\n[ERROR] Strategic thinker artifacts still missing after focused retry — aborting executor.\n`);
      try {
        await persistAndPostStatus(taskId, {
          task_id: taskId,
          status: 'FAILED',
          artifact_type: 'ERROR_LOG',
          logs: (thinkerResult.allOutput + '\n' + retryResult.allOutput).slice(-10_000),
          failure_category: 'strategic_thinker_artifacts_missing',
        });
      } catch (e) {
        sendLog(ws, `[ERROR] Failed to post thinker gate failure: ${String(e)}\n`);
      }
      return { ok: false };
    }
  }

  return { ok: true, analysisText };
}
