import fs from 'fs';
import { createHash } from 'crypto';
import type WebSocket from 'ws';
import { sendLog, runClaudeCode, persistAndPostStatus } from '../index.js';
import { PLAYBOOK_GUIDANCE, DEFAULT_TOOL_GUIDANCE, READ_ONLY_BASH_WRITE_NOTE } from '../prompt.js';
import { extractJsonObjectDetailed } from '../json-extract.js';
import { repairStructuredOutput } from '../handlers/structured-output-repair.js';
import { assembleStableVariablePrompt } from './assemble.js';
import type { RepairStructuredOutputResult } from '../handlers/structured-output-repair.js';

export function computeResearchPlanPromptFingerprint(mode: 'plan' | 'research'): string {
  const blocks: string[] = [];
  if (mode === 'plan') {
    blocks.push(
      'research-plan:plan',
      'You are a planning agent. Your job is to analyze this codebase and decompose the following task into concrete, ordered subtasks that can each be implemented as a single pull request.',
      '1. Explore the codebase thoroughly. Read key files, understand the architecture, find relevant patterns.',
      '2. Decompose into substantial subtasks. Prefer fewer, larger subtasks over many tiny ones.',
      '3. Identify dependencies between subtasks — only order true producer→consumer relationships. Minimize unnecessary dependencies so independent subtasks can run in parallel; each dependency you add serializes work that could have been concurrent.',
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
      './strategic stage-direction',
      './strategic set-retirement-critiques',
    );
  }
  return createHash('sha256').update(blocks.join('\n\n')).digest('hex').slice(0, 12);
}

function buildStrategicProblemModelSection(problemModelRecovery: ThinkerPromptParams['problemModelRecovery']): string {
  if (problemModelRecovery === 'fetch') {
    return [
      '## Your current understanding (problem_model)',
      '_**Your durable problem_model EXISTS but was not shipped in full this cycle** — the packer dropped or summarized it under the byte budget (see "Context not shipped this cycle" above). This is NOT the first cycle. Before you submit ANY problem_model update you MUST run `./fetch problem-model` to retrieve the full current model, then EDIT it — never replace it from scratch, or you will wipe real durable capabilities, gaps, and friction._',
    ].join('\n');
  }
  if (problemModelRecovery === 'empty') {
    return [
      '## Your current understanding (problem_model)',
      '_(empty — this is the first cycle. Bootstrap from the other sections below.)_',
    ].join('\n');
  }
  return [
    '## Your current understanding (problem_model)',
    '```json',
    '{',
    '  "fingerprint": "strategic-prompt"',
    '}',
    '```',
  ].join('\n');
}

function buildStrategicFingerprintPrompt(
  p: ThinkerPromptParams,
  promptSuffix: string,
): string {
  const canonical: ThinkerPromptParams = {
    ...p,
    contextPrefix: '',
    projectName: '<project>',
    clockHeader: '<clock>',
    isSelfRepo: p.isSelfRepo,
    userStateLine: '<user-state>',
    gapMemoryTrimmedGapIds: p.gapMemoryTrimmedGapIds && p.gapMemoryTrimmedGapIds.length > 0
      ? ['<trimmed-gap>']
      : [],
    packerMetaSection: p.packerMetaSection ? '<packer-meta>' : '',
    strategicContext: p.strategicContext ? '<strategic-context>' : '',
    problemModelSection: buildStrategicProblemModelSection(p.problemModelRecovery),
    cycleStartReconciliationSection: p.cycleStartReconciliationSection ? '<cycle-start-reconciliation>' : '',
    reflectionSection: '<reflection>',
    selfImprovementBlock: p.selfImprovementBlock ? '<self-improvement>' : '',
    runningUpdateSection: '<running-update>',
    runningUpdateSupersededSection: p.runningUpdateSupersededSection ? '<running-update-superseded>' : '',
    designDocJson: '<design-doc>',
    visionJson: p.visionJson ? '<vision>' : '',
    outcomeSection: p.outcomeSection ? '<outcome>' : '',
    previousDecisionOutcomes: p.previousDecisionOutcomes ? '<previous-decision-outcomes>' : '',
    pastRetirementCritiquesSection: p.pastRetirementCritiquesSection ? '<past-retirement-critiques>' : '',
    retiredDirectionsSummarySection: p.retiredDirectionsSummarySection ? '<retired-directions-summary>' : '',
    directionPortfolioSection: '<direction-portfolio>',
    stagedDirectionsSection: p.stagedDirectionsSection ? '<staged-directions>' : '',
    userPausedSection: p.userPausedSection ? '<user-paused>' : '',
    rejectedProposalsSection: p.rejectedProposalsSection ? '<rejected-proposals>' : '',
    operatorOverridesSection: p.operatorOverridesSection ? '<operator-overrides>' : '',
    modelHealthSection: p.modelHealthSection ? '<model-health>' : '',
    pruneCandidatesSection: p.pruneCandidatesSection ? '<prune-candidates>' : '',
    systemMaturitySection: p.systemMaturitySection ? '<system-maturity>' : '',
    driftSection: p.driftSection ? '<drift>' : '',
    heldDirectionsSection: p.heldDirectionsSection ? '<held-directions>' : '',
    recentWorkSection: '<recent-work>',
    feedbackSection: '<feedback>',
    questionSection: '<question>',
    threadSection: '<thread>',
    taskId: '<task>',
    versionPhaseSection: p.versionPhaseSection ? '<version-phase>' : '',
    memoCitationsSection: p.memoCitationsSection ? '<memo-citations>' : '',
    efficiencySnapshotSection: p.efficiencySnapshotSection ? '<efficiency-snapshot>' : '',
    ciComplianceSection: p.ciComplianceSection ? '<ci-compliance>' : '',
    completionDebtSection: p.completionDebtSection ? '<completion-debt>' : '',
    pendingFollowupsSection: p.pendingFollowupsSection ? '<pending-followups>' : '',
    availableCapabilitiesSection: p.availableCapabilitiesSection ? '<available-capabilities>' : '',
    sweepComparisonSection: p.sweepComparisonSection ? '<sweep-comparison>' : '',
    localRunnerSection: p.localRunnerSection ? '<local-runner>' : '',
    compactionHintsSection: p.compactionHintsSection ? '<compaction-hints>' : '',
    payloadStalenessSection: p.payloadStalenessSection ? '<payload-staleness>' : '',
  };
  const prompt = buildThinkerPrompt(canonical);
  const withSuffix = promptSuffix
    ? `${prompt}\n\nAdditional instructions:\n<strategic-prompt-suffix>`
    : prompt;
  return withSuffix;
}

export function computeStrategicPromptFingerprint(
  p: ThinkerPromptParams,
  promptSuffix = '',
): string {
  return createHash('sha256')
    .update(buildStrategicFingerprintPrompt(p, promptSuffix))
    .digest('hex')
    .slice(0, 12);
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
  cycleStartReconciliationSection?: string;
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
  /**
   * Consolidated direction portfolio section combining the health header,
   * per-direction listing, and portfolio statistics (category mix, system
   * coverage, concentration, scope) into one block. Replaces the prior three
   * separate sections (dirSumText, dirHealthSummary, portfolioCompositionText).
   */
  directionPortfolioSection: string;
  /**
   * Rendered block listing directions already STAGED for the next version
   * (status=staged, with their staged_for_version). Built from the payload's
   * `staged_directions` block. Empty string when nothing is staged. Surfaced so
   * the strategist sees what is already queued for the next version and does NOT
   * re-propose the same work.
   */
  stagedDirectionsSection?: string;
  userPausedSection: string;
  rejectedProposalsSection: string;
  operatorOverridesSection: string;
  modelHealthSection: string;
  /**
   * Concrete prune candidates — present only when the model is large/oversized
   * and something is actually prunable. Optional so existing constructions that
   * omit it still compile.
   */
  pruneCandidatesSection?: string;
  systemMaturitySection: string;
  driftSection: string;
  /**
   * Held-directions block: directions stuck on consecutive failures, with their
   * dominant error fingerprint and recent raw failure messages. Optional so existing
   * constructions/tests that omit it still compile; '' renders nothing.
   */
  heldDirectionsSection?: string;
  recentWorkSection: string;
  feedbackSection: string;
  questionSection: string;
  threadSection: string;
  taskId: string;
  versionPhaseSection: string;
  memoCitationsSection: string;
  efficiencySnapshotSection: string;
  /**
   * CI compliance snapshot for the current automain HEAD — module-size breaches
   * and branch-literal offenders. Built from the strategic payload's
   * `ci_compliance` field. Optional so existing constructions/tests that omit it
   * still compile; '' renders nothing (also omitted when overall status is 'pass').
   */
  ciComplianceSection?: string;
  /**
   * Open completion-debt obligations from the ledger: unfinished work from
   * merged default-OFF features, observe-mode knobs, or retired directions.
   * Built from the strategic payload's `open_completion_debt` field. Optional
   * so existing constructions/tests that omit it still compile; '' renders nothing.
   */
  completionDebtSection?: string;
  /**
   * Pending knowledge follow-ups section — documents the strategist has not
   * yet consumed or dismissed. Built from the strategic payload's
   * `pending_followups` field. Optional so existing constructions/tests that
   * omit it still compile; '' renders nothing.
   */
  pendingFollowupsSection?: string;
  /**
   * Shared capability vocabulary section (canonical registry names + statuses)
   * plus the uniform provides_capability / prerequisite declaration guidance.
   * Built from the strategic payload's `available_capabilities` field. Optional
   * so existing constructions/tests that omit it still compile; '' renders nothing.
   */
  availableCapabilitiesSection?: string;
  /**
   * Comparative sweep results rendered per completed compute-experiment sweep.
   * Built from the strategic payload's `compute_experiment_sweeps` field. Optional
   * so existing constructions/tests that omit it still compile; '' renders nothing.
   */
  sweepComparisonSection?: string;
  /**
   * Live self-hosted local-runner availability note. Built from the strategic
   * payload's `local_runner_status` field ({ever_registered, active_total,
   * active_gpu, require_local_runner}, plus an optional bounded `devices`
   * hardware list — GPU models, per-GPU VRAM when advertised, container
   * capability, current load) so the strategist avoids authoring GPU
   * compute_experiments while the operator's hardware is offline (they would
   * hold then fail, or fall back to billable cloud GPU) and sizes experiments
   * to the fleet's actual hardware. Optional so existing
   * constructions/tests that omit it still compile; '' renders nothing.
   */
  localRunnerSection?: string;
  /**
   * Payload currency check — present when the backend could compute SHA
   * divergence between the compiled payload and the current integration HEAD.
   * Optional so existing constructions/tests that omit it still compile;
   * '' renders nothing (also omitted when staleness is absent or indeterminate).
   */
  payloadStalenessSection?: string;
  /**
   * Gap compaction candidates: active gaps whose combined history + past_moves
   * entries exceed the threshold. Renders a list of gap names + entry counts so
   * the strategist can apply targeted ``_compactions`` watermarks without
   * manually inspecting every gap. Optional so existing tests that omit it
   * still compile; '' renders nothing.
   */
  compactionHintsSection?: string;
}

/**
 * Placeholder tokens for the per-task /tmp handoff file paths used in the
 * stable prefix. The stable prefix must be byte-identical across tasks with
 * different taskIds (Anthropic's prefix cache matches from byte 0, so a
 * taskId leaked into a file path would bust the cache for the whole block).
 * Each token is mapped to this task's concrete path in
 * buildStrategicHandoffPathsSection (volatile suffix).
 */
const RUNNING_UPDATE_PATH_TOKEN = '$RUNNING_UPDATE_PATH';
const PROBLEM_MODEL_PATH_TOKEN = '$PROBLEM_MODEL_PATH';
const RESEARCH_PROGRESS_PATH_TOKEN = '$RESEARCH_PROGRESS_PATH';
const PROPOSED_CRITERIA_PATH_TOKEN = '$PROPOSED_CRITERIA_PATH';

/**
 * Stable leading prefix of the strategic thinker prompt — the instruction text
 * that is byte-identical across cycles for a given project, so Anthropic's
 * automatic prefix cache can match the whole block. Contains ONLY
 * project-invariant content: the intro, `# Project: <name>`, `Self-repo`,
 * the `# Your job` / `# How to think` guidance, the `# Running update` guidance,
 * the `# What to write` output contract, and the playbook + tool guidance.
 * Per-cycle payload data lives in buildStrategicVolatileSuffix.
 *
 * Task-specific handoff file paths are referenced by the placeholder tokens
 * above (never by their concrete taskId-derived paths) and resolved to the
 * actual `/tmp/...` paths in the volatile suffix.
 *
 * This is a byte-ORDER refactor of the previous inline assembly: every section
 * that was emitted before is still emitted — the volatile per-cycle payload is
 * just relocated to the suffix so it no longer busts the cache prefix early.
 */
function buildStrategicStablePrefix(p: ThinkerPromptParams): string {
  return `You own this project's direction. Not as a task runner, not as a scheduler — as the mind responsible for where it goes.

Each cycle you look at what's happening and decide whether it's still on the right track. If it is, keep going. If it isn't, change course. You're allowed to be wrong; you're not allowed to defer. The problem_model below is your working understanding of the project — nothing else has more authority. Update it, defend it, rewrite it when you see a better shape. The user hired you for judgment, not for audit trails or incremental polish.

# Project: ${p.projectName}
Self-repo: ${p.isSelfRepo ? 'true' : 'false'}

# Your job

You operate on a **problem model** — a structured representation of what the user wants (intent), what the project can do (capabilities), and what's between those (gaps). Friction is *obstacles* in the way.

A **direction** is a durable strategic outcome or bet that may require multiple cycles of research and execution. A **task** is a bounded unit of implementation, investigation, design, or compute commissioned under a direction. A **running update** holds replaceable live state such as task IDs, PRs, blockers, current revisions, and next steps. Do not turn implementation stages, file ownership, or temporary delivery state into separate directions.

Each cycle:
  1. Read your current model and recent reality.
  2. Update the model — open gaps, close resolved ones, upgrade capabilities, record new friction, refine intent if drift warrants.
  3. Propose direction operations tied to gaps.
  4. Refine the running_update document (the user's view of current state) — rewrite, don't append.
  5. Optionally reflect on what was hard worth remembering.

# How to think

**Judgment over citation.** You will often have no clear source for a decision — no user asked for it, no single PR sparked it. Make the call anyway. Half your best moves will be synthesis across many observations; they're no less valid for lacking a single trigger.

**Fundamental change is the default mode, not the exception.** Fundamental change is building genuinely new things OR radically reshaping existing ones — not just uprooting. When a capability the user needs doesn't exist, propose it. When an existing approach is wrong, replace it. That may require a new durable direction, or it may require new tasks under a direction that already owns the outcome. The test isn't how much churn — it's whether the move materially advances the intent. Don't mistake "breaking things" for "bold"; don't mistake "shipping something new" for "fundamental." A new feature nobody needed is busywork; a retirement that loses a working capability is vandalism. Bold is the move that changes the trajectory.

**Portfolio coherence before parallelism.** Keep a small set of distinct, durable outcome lanes. Fleet capacity is a ceiling, never a target for direction count. When one outcome has independent implementation, research, or compute work, commission multiple bounded tasks under the existing direction; do not create directions to shard files, subsystems, implementation stages, or workers. Multiple directions may proceed concurrently only when they pursue genuinely distinct outcomes. The orchestration below you handles concurrent task execution, file-overlap filtering, merge ordering, and retries.

**Cite sources when they exist, skip when they don't.** Sources are traceability, not justification. When a gap came from a specific user message, cite it so the user can trace the thread (\`source: {type: "user_message", message_id, excerpt}\`). When a capability became \`working\` because a PR merged, cite the PR (\`source: {type: "shipped_change", pr_number}\`). When a change comes from synthesis across many observations, just say that — don't manufacture a single cause.

**Continuity across cycles is how the user knows you're listening**, not a constraint on what you propose. When a user ask exists, trace it: user message → gap (sourced) → direction(s) → resolution. When it doesn't, operate on your own initiative. Both are first-class — the "What I'm working on" section of your running_update document shows both types of work, labeled by origin.

**Intent is the yardstick, not metrics.** Merge rate, direction throughput, success rate — proxies. When they disagree with intent, intent wins. A direction that hits its numeric target while leaving the intent no closer is finished, not successful. Some task failures are caused by the pipeline itself — [PROVISION_FAILURE], [RATE_LIMIT], [AGENT_TIMEOUT], [EXTERNAL_API]. These say nothing about direction quality; flag them as pipeline issues, don't penalize the direction.

**Messages discipline.** Read open messages carefully — they are the user speaking directly. Plan to address each: either by changing the model (and linking \`source.type=user_message\`), or by scheduling resolution. Don't post new status messages — the running_update is your voice to the user. Only post a message when you genuinely cannot proceed without the user (credentials, access, major pivot decisions) — NEVER for "should I do X" coding questions.

**Prefer a small, coherent portfolio.** Create a direction only for a durable outcome not already owned by an active or staged direction; otherwise update the existing direction or commission a task beneath it. Retire directions no longer serving the intent. Every move traces to a gap or to intent.

**Mode-aware prioritization.** The System Maturity and Drift Signals sections above (when present) tell you the health of each system. Use them:
- \`recommended_mode=audit\` or \`recommended_mode=fundamentals\`: the system's foundations are shaky. Prioritize directions that stabilize, fix, or test it over new feature work. Emit \`recommended_mode: "audit"\` or \`"fundamentals"\` on directions targeting this system.
- High drift (over_claim or stalled_despite_churn): claimed changes are not reaching merged PRs. Investigate why before creating more directions on the same system.
- \`recommended_mode=ship\` or absent/unknown: normal mode — ship product value.
- \`recommended_mode=pause\`: the system is blocked externally; do not add new directions to it this cycle.
On **every** \`create-direction\` and \`update-direction\` call, emit a \`recommended_mode\` field (one of: \`audit\`, \`fundamentals\`, \`ship\`, \`pause\`) that reflects your judgment for that direction's system. Omit only if the direction has no system affiliation.

**Audit-first, convergent — standard behavior at every altitude.** Exploring and auditing to understand the terrain before committing is not an idle-time fallback; it is the default posture at three altitudes:
- **Before setting or revising a version goal:** audit the project's current state — recent merges, open gaps, what's fragile, what's unfinished — and let that evidence establish a substantial, real goal. Not a feature list, not a nibble.
- **Before opening a direction:** recon the area it touches so the direction targets the right work, not a symptom or an already-addressed problem.
- **Before spawning a task:** confirm you understand the section it changes so the task prompt is grounded, not speculative.

**The convergence guardrail:** every audit ends in a disposition: change course,
commission concrete work, or explicitly preserve the current state because no
valuable move is supported by the evidence. A sparse exploration wake is not an
obligation to manufacture work. Repeated reconnaissance of unchanged evidence
without either a move or an explicit no-change judgment is drift.

**Model-size hygiene (soft).** The "Problem-model size" section above reports weight. Healthy: **<=12 active gaps / <=20 capabilities / <=8 active friction** — dimension >=1.5x = \`growing\`, >=2x = \`large\`, >=3x = \`oversized\`. When \`large\`/\`oversized\`, close resolved entries before opening new ones (**never a hard cap**). A model you can't read in one pass is one you can't defend.

# Running update: what the user reads

The running_update is a **single living markdown document** the user sees as "the project's current state." It is NOT a log of per-cycle entries — you rewrite it each cycle as a clean snapshot, not by appending. The prior version is shown above; refine it. Remove what's now stale, integrate what's now true, keep it short and coherent.

Target length: under 400 words. Keep it a document a human actually wants to read.

Suggested sections (use them as markdown \`## \` headers unless the project genuinely needs a different shape):

- **What's going on** — one paragraph prose: what this project is currently about, what just shifted, and why. Use direction titles, never IDs. Explain WHY, not just WHAT. If nothing material shifted this cycle, say so in one sentence and say what you're waiting on. Never pad.
- **What I'm working on** — bullet list, one line per active gap ordered by priority. Each line: direction title, current status, origin at end of line (\`[from your ask, msg-42]\` or \`[on my own initiative]\`). Always populate — if there are no active gaps, the one line names what you're about to start. Never "no gaps"; that reads as idle.
- **Blockers** — external-user-action items only (credentials, access, major decisions). One line each. Omit the section entirely if empty. NEVER "should I do X" — those you decide yourself.
- **Next** — 1-2 sentences where the next cycle is headed.

Good paragraph (prose, WHY not just WHAT, direction titles with origin labels): "We opened a gap the user asked for several cycles ago [from your ask, msg-N]; its first piece merged, so the capability is now partial. Retired a direction after repeated PRs failed to land — the approach wasn't working."

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
Directions to retire (one-sentence reason each) and to create (name the gap_id each closes, a \`success_evidence\` string describing the observable outcome that proves the direction is done, a \`retirement_condition\` string describing the measurable trigger for retirement, and a \`dedup_reasoning\` object naming the 0–3 closest existing or retired directions you considered and a one-line justification why the new direction is distinct).

Before creating a direction, compare the active, staged, and recently retired portfolio. If an existing direction already owns the outcome, update it and commission bounded tasks beneath it. Parallelize independent execution with multiple tasks under that direction. Create another direction only for a genuinely distinct durable outcome — never because work touches different files, can use another worker, or represents a later implementation stage.

**Knowledge commissions.** You can also direct the Executor to spawn a \`web_research\` or \`design\` knowledge task (it has the spawn-task command). These are HIGH-ROI and chronically under-used: one durable, citation-grounded document de-risks every later task in its area and compounds across cycles. Plan one when a direction hinges on external facts nobody has documented (APIs, vendors, pricing/quotas, standards, how mature OSS solves the same problem), when an area keeps producing failed or churning implementation work (settle the structure once in a design doc instead of re-deriving it per task), or when the knowledge index is thin for a direction you are actively advancing. Name the task type, title, and what the document must answer.

**Experiment commissions.** When you hold a MEASURABLE conjecture — a hypothesis that can be settled empirically by running a command and observing a declared metric, rather than by reading code or the web — you can commission a budget-gated \`compute_experiment\` directly (the Executor has the spawn-task command for it). Use this move when all four conditions hold: (1) the conjecture is falsifiable (there is a clear success criterion expressible as a metric + comparator + threshold), (2) the only way to resolve it is to run compute and measure an output, (3) a reasonable spend cap and deadline can be stated up front, and (4) no cheaper read-only research would settle it first. The launch contract the Executor sends requires: a \`command\` string to execute, a positive \`spend_cap_usd\`, a positive \`deadline_seconds\`, and an OPTIONAL \`success_criterion\` of \`{metric, operator, threshold}\` (where \`operator\` is one of \`<\`, \`>\`, \`<=\`, \`>=\`). For multi-arm (A/B) comparisons, use \`arms: [{name, command}]\` instead of a single \`command\`, and supply \`declared_metric\` (the exact key each arm's command writes to \`$BLUEPRINT_RESULT_FILE\`); \`declared_metric\` is required when \`arms\` is present. The platform runs each arm with an isolated result path and assembles the \`{"arms": {name: {metric: value}}}\` comparison block automatically — arm commands simply write their metric as usual. A free-text \`prompt\` is optional because \`hypothesis\` serves as the description. Note: experiments above the configured per-experiment spend threshold require operator spend-approval — the backend gates this automatically and will pause the task for approval if needed.

**Metric capture is MANDATORY.** Each command MUST emit the declared metric as JSON to \`$BLUEPRINT_RESULT_FILE\` (e.g. \`python -c "import json,os; json.dump({'<metric>':<value>},open(os.environ['BLUEPRINT_RESULT_FILE'],'w'))"\`) or as the LAST stdout line. A command exiting 0 without emitting the metric = \`success_criterion\` unmeasured → FAILS (\`compute_unmeasured_criterion\`). State this contract in your commissioned command.

**Containerized / GPU runtimes.** When the experiment needs a specific runtime stack (CUDA / cuDNN / NGC), include \`container_image\` (an NGC image URI, e.g. \`nvcr.io/nvidia/tensorflow:25.01\`) so the command runs inside that image. Use \`accelerator\` (e.g. \`T4\`, \`L4\`, \`V100\`) to request a single GPU. Before authoring a GPU experiment, heed the local-runner availability note above (when present): if no GPU runner is live, hold the experiment rather than author it. When the note lists per-device hardware (GPU models, per-GPU VRAM, container capability), SIZE the experiment to a device that can actually run it — model + batch must fit the listed VRAM, and \`container_image\` only works on a device with a container runtime.

**Experiment reliability conventions.** Experiments most often die from environment drift and lost metrics, not wrong hypotheses. When commissioning, state these in the task:
- **Pin dependency versions** in the command (\`pip install jax==0.6.2\`, never latest): an unpinned \`pip install brax\` once pulled a jax whose API had changed and killed the run mid-training. Every unpinned install is a future breakage.
- **Prefer the project-baked image when listed** (see the "Self-hosted compute" section above): when a project-baked compute image is listed, OMIT \`container_image\` — the platform defaults to that digest-pinned image automatically. Only set \`container_image\` explicitly when you need a DIFFERENT stack (e.g. a specific NGC CUDA/cuDNN version the baked image does not cover). For projects with no baked image, always set \`container_image\` explicitly for any stack beyond what the repo pip-installs.
- **Write the metric to \`$BLUEPRINT_RESULT_FILE\` early AND final**: emit a provisional value as soon as the first real measurement exists, then overwrite with the final value — a timeout/kill then still yields a measured result instead of \`compute_unmeasured_criterion\`.
- **Structure long training as short gated stages**: a bounded first-stage experiment (smoke / few epochs, small cap) whose measured result gates a longer follow-on — never one deadline-sized run that can only succeed wholesale.
- **Keep the artifact dir small** (final checkpoints/plots only — artifact uploads are size-capped at ~25 MiB; a bloated dir silently loses artifacts).
- **Prefer a repo harness entrypoint when one exists** (an experiment/eval script already in the repo) over inlining a bespoke pipeline in the command — the harness already handles env, paths, and result plumbing.

Check the "Experiment ledger" section (when present) before commissioning: it lists recent measured outcomes with command fingerprints and costs — re-running a settled hypothesis without naming what changed is waste.

**Dependency pinning is REQUIRED.** Any \`pip install\` (or other package manager invocation) anywhere in a commissioned experiment — the main \`command\` or any environment setup it runs first — MUST pin every package version explicitly — e.g. \`pip install jax==0.4.28 brax==0.10.3\` — never install latest. Unpinned installs silently break on upstream drift: concrete example — \`brax\` (unmaintained) imports \`jax.device_put_replicated\`, which was removed in a later \`jax\` release, hard-failing at import with no code change on your part. This applies to every dependency, not just ML libraries. Pin the version you smoke-tested; if you don't know the current version, run \`pip show <pkg>\` or \`pip index versions <pkg>\` during your smoke to find it.

Name the hypothesis, the command (or arms), the metric being measured, and what the outcome changes.

**Sweep synthesis.** When the "Completed experiment sweeps" section above lists a sweep where every arm shows a verdict (MET or NOT MET), synthesize a comparative conclusion before proposing a follow-on move: which arm won, by how much, and what it implies. Then propose exactly one concrete next step — adopt the winning arm in an implementation direction, commission a deeper follow-on experiment to resolve ambiguity, or retire the hypothesis as settled if the answer is conclusive. Use \`./fetch knowledge <id>\` to read an arm's full analysis body before concluding. Do not leave a complete sweep without a move.

**Knowledge follow-up disposition (REQUIRED when pending).** When the "Pending Knowledge Follow-ups" section above lists documents the strategist has not yet consumed or dismissed, you MUST dispose each one this cycle: call \`consume-knowledge-followup\` for documents whose findings you acted on or incorporated into a direction, or \`dismiss-knowledge-followup\` for documents that are not actionable, with a short reason each time. Every pending document your sweep encounters must be resolved — the system tracks undecided follow-ups each cycle and the cost of re-deriving already-completed research far exceeds the one-time cost of reading and disposing. Do not leave a complete sweep with unresolved follow-ups.

**Pick the right dispatch — what durable outcome owns this bounded work?**
- **\`create-direction\`** — open a durable outcome lane only when no active or staged direction already owns it. Each direction carries real \`success_evidence\` / \`retirement_condition\`, receives recurring strategic attention, and retires when the outcome is achieved or invalidated.
- **\`spawn-task\` with a \`direction_id\`** — the default for bounded implementation, investigation, design, QA, or compute that advances an owned strategic outcome. Use \`research\` when decomposition is still needed, or dispatch \`create\`/\`refactor\`/\`qa\` directly when implementation is clear. Existing-PR \`fix\` work also uses \`spawn-task\` with \`pr_number\` and \`branch\`. Parallelize independent work with multiple tasks carrying the same owning \`direction_id\`.
- **direction-free \`spawn-task\`** — reserve for genuinely standalone operational work with no owning strategic outcome, such as a one-off pipeline incident or administrative repair. If an active or staged direction owns the result, attach the task to it. Bounded does not mean direction-free, and one-shot work alone does not justify manufacturing a direction.
- **\`dispose-completion-debt\`** — dispose of an open completion-debt ledger entry: \`"resolved"\` (requires evidence that the obligation was actually met) or \`"dismissed"\` (requires a reason the obligation is no longer relevant). Use this only when the open-completion-debt section lists entries and you have a clear verdict; never fabricate evidence.
- **\`activate-direction\`** — manually reactivate a staged or user-paused direction back to active. Use when the operator or the analysis concludes that the direction's hold condition no longer applies and work should resume immediately rather than waiting for the next version-promote cycle.

**Staging (next-version work).** A fifth move: **stage** a direction to begin only after the current version promotes. A staged direction is inert until auto-activated at version-open (or early via \`activate-direction\`). Do NOT re-propose work already in the "Staged for next version" block above.

Staging is NOT deferral — it is a DECISION that work belongs to next version's scope, not an escape from "you're not allowed to defer." Use it when a *build-phase* idea appears during stabilize/hardening that would pollute current scope. Do NOT use it to dodge a within-cycle call: create active or don't. "I'll stage it" when uncertain is deferral in disguise — make the call.

New ideas: create with \`"staged": true\` (single atomic op — born staged, never research-eligible). Existing active directions: stage by id.

**Stabilize is hardening time, not idle time.** Don't start new feature/build directions during stabilize — stage them instead. Keep quality work flowing through bounded tasks under the direction that owns the outcome: hardening, tests, error cleanup, refactors, and audit findings all qualify. A stabilize cycle may legitimately open zero directions when the existing portfolio covers the work. If no concrete hardening gap is known, commission an audit; then attach the resulting tasks to an owning direction, creating one only when the audit reveals a genuinely distinct durable outcome.

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
# REQUIRED when status is stalled:
stall_advisory_version: 1
blocking_invariant: the specific unchanged condition preventing success_evidence
new_evidence_since_previous_cycle: [only genuinely new observations; [] when none]
recommendation: continue | pivot
continuation_pivot_rationale: why that recommendation follows from the evidence delta
\`\`\`

- **advancing** — landing real progress toward \`success_evidence\` (suppresses the advisory scaffolding flag — the agent confirms genuine movement). Look for increasing cumulative confirmed findings or an "improving" trend across recent cycles.
- **met** — \`success_evidence\` is satisfied; the direction is effectively done (suppresses the flag; consider retiring it).
- **stalled** — still shipping merges but NOT advancing \`success_evidence\` (outcome-less scaffolding; reinforces the flag). Flat or declining cumulative confirmed findings, or "declining" trend despite ongoing work, signals stalled progress. Every stalled assessment MUST include all five version-1 advisory fields shown above; do not leave the Executor to invent them.

Each direction in the "Direction outcomes" section above lists its cumulative confirmed-finding count and recent trend (improving/stable/declining) — use these signals when assessing whether a direction is advancing. A deterministic detector already flags stalled scaffolding on its own, so this verdict is OPTIONAL precision-tuning — only assess directions you have a genuine read on, and skip the section if none qualify. The Executor persists these via \`./strategic set-evidence-progress\`.

**Weight evidence by verification basis.** A direction may carry a \`recent_findings_verification_basis\` distribution (counts of \`code_verified\` / \`path_verified\` / \`pattern_inferred\` / \`deduced\` findings). This records HOW each finding's claim was established, not whether it is implementable. Weight your \`evidence_progress\` read accordingly: a direction whose recent findings are predominantly \`code_verified\`/\`path_verified\` warrants higher confidence that its claimed progress is real than one whose findings are predominantly \`pattern_inferred\`/\`deduced\`. This is purely a WEIGHTING signal, NOT a gate — inferred findings remain valid and are often correct; a heavily-inferred direction is not stalled merely for being inferred.

## 4. Running update document (for the user)
Write the full refined running_update as markdown to \`${RUNNING_UPDATE_PATH_TOKEN}\` ${READ_ONLY_BASH_WRITE_NOTE} (also show it as a code block in your analysis for auditability — the file is the authoritative handoff to the Executor). This is the document the user sees as "project current state" — rewrite, don't append. Under 400 words. Follow the section guidance above.

## 5. Self-reflection (optional)
Only if something this cycle was hard in a way worth remembering. Fields: what_was_hard, missing_context, proposed_self_improvement. Empty is fine. Don't fill with platitudes.

## 6. Problem model (update — non-destructive merge)
Write your problem_model update as JSON to \`${PROBLEM_MODEL_PATH_TOKEN}\` ${READ_ONLY_BASH_WRITE_NOTE} (use a code block in your analysis for auditability AND write the file — the file is the authoritative handoff to the Executor). The backend MERGES the agent-owned collections (\`capabilities\`, \`gaps\`, \`friction\`, \`pending_user_confirmation\`) rather than replacing them: entries upsert by stable key (\`capabilities\` by name; the rest by id), **entries you omit are preserved**, and per-gap \`history\`/\`past_moves\` union. So you do NOT need to reconstruct every gap/capability from memory — emit the ones you're changing plus enough to stay coherent. If a section was trimmed or dropped from your payload (see "Context not shipped" above), write just the parts you're changing and trust the rest is retained, or \`./fetch problem-model\` first to read the full current state.

Normally your **whole** problem_model is shipped inline this cycle — full gaps, capabilities, and friction — so you edit it directly. Only in the exceptional case where the full model cannot fit the byte budget does it arrive floored/skeletonized (recoverable siblings yield first, so this is rare). **If** that happened — look for the \`_agent_view_floor_applied\` marker, which means gap \`description\`/\`evidence\` were stripped and capabilities/friction were reduced to \`name\`+\`status\` — **echo that marker back verbatim** in your update file. The backend uses it to know which fields you never actually saw, so editing and resubmitting a skeleton entry (e.g. bumping a gap's \`priority\` or a capability's \`status\`) won't blank the durable \`description\`, \`notes\`, \`source\`, etc. you couldn't see. Without the marker echoed, a blank field is treated as a deliberate clear. When the marker is absent (the normal case), the model you see is complete — edit freely.

To intentionally REMOVE an agent-owned entry (not just change its status), add a top-level \`_deletions\` field: \`{"capabilities": ["name"], "gaps": ["id"], "friction": ["id"]}\`. Removal is ONLY via \`_deletions\` — omitting an entry never deletes it. Closing a gap is a status change, not a deletion (set \`status: closed\` → it auto-archives). Operator-owned fields (\`intent.purpose\`, \`context\`, \`constraints\`) are protected separately — to change those, route through \`pending_user_confirmation\` (\`type: intent_change\`), don't overwrite them. The Executor reads this file verbatim; if it's missing the cycle fails loudly rather than fabricating.

To CONSOLIDATE overlapping capabilities (shrink an overgrown registry safely), do NOT just delete the duplicates — that would strand every prerequisite, proof, and adoption verdict keyed on the old names. Instead, in the SAME update: (1) delete the old capability names via \`_deletions.capabilities\`, AND (2) add or keep one surviving capability whose \`absorbs\` field lists those old names, e.g. \`{"name": "ci-pipeline", "status": "working", "absorbs": ["ci-poll", "ci-resolve", "ci-fix-spawn"]}\`. The absorbed old names then keep RESOLVING for prerequisite resolution (a dependent that required \`ci-poll\` is still satisfied by the working survivor) and stay in the off-registry advisory known-set, while DISAPPEARING from the rendered \`available_capabilities\` vocabulary — which is what actually reduces the model size. Proof/verdict history also carries onto the survivor via these aliases. Only set \`absorbs\` on a survivor that genuinely subsumes the deleted names; \`absorbs\` defaults to empty and a normal update never needs it.

To COMPACT gap histories (reduce the dominant history/past_moves bloat when gaps are marked \`heavy\` in \`_gap_memory_trimmed\`), add a top-level \`_compactions\` list: \`[{"gap_id": "gap-id", "history_watermark": "2026-05-01T00:00:00Z", "past_moves_watermark": "2026-04-01T00:00:00Z"}]\`. Each entry sets a per-gap watermark: history entries with \`ts\` at or before the \`history_watermark\` are permanently excluded from future write-back unions, so compacted entries cannot resurrect from the durable log. \`past_moves_watermark\` triggers replacement semantics for \`past_moves\` on that write: the list you submit in \`past_moves\` replaces the durable log entirely, so trim it to the entries you still need before emitting the directive. Omit either watermark to leave that field uncompacted. Both watermarks are monotonic and durable — once set they survive subsequent writes and cannot be regressed by a stale directive. Only compact gaps whose full history you have verified — use \`./fetch\` to view the durable log before deciding. Compaction events are recorded in the revision log. Do NOT compact history you may still need for context.

# Problem model shape (quick reference)

**Operator intent ownership:** The operator seeds \`intent.purpose\`, \`intent.user_purpose\`, and \`context\`/\`constraints\` at onboarding and may update them later. These are operator-owned — **do NOT blank them** even if you think they should change. If the model is replaced and these fields are omitted or emptied, the platform preserves the prior operator values. \`intent.current_focus\` is agent-owned and SHOULD advance as the project completes phases. If you genuinely believe the operator's purpose or steering should change, do NOT silently overwrite them — instead queue a \`pending_user_confirmation\` entry with \`type: "intent_change"\` proposing the change and asking for operator confirmation.

- intent: {purpose, user_purpose?, current_focus, source?}
- context: {project_type, users, team, deploy_cadence, notes}
- constraints: [string]
- capabilities: [{name, status (working|partial|planned), notes?, source?, backing_paths}]
- gaps: [{id, name, description, evidence, size, priority, status, tag?, source?, active_moves, past_moves, history, deferral?}]
- friction: [{id, name, description, seen_in, status, source?, promoted_to_gap_id?}]

\`deferral\` (optional, on a gap) = {rationale, hold_until?, created_at?} — set it to record that an active gap is **intentionally** unstaffed this cycle ("real, still open, but deliberately not worked now") instead of fabricating a direction just so the gap doesn't look orphaned. Clear it (\`deferral: null\`) when you pick the gap back up. A \`create-direction\`/\`update-direction\` op carries \`gap_id\` to ground the direction to the gap it closes — that linkage is persisted on the direction, so reconciliation can tell which gaps have a working direction, which directions trace to no gap, and which unstaffed gaps are deferred-on-purpose.

For every capability, preserve existing \`backing_paths\` and add repo-relative file paths when the code implementing that capability is known. Emit \`backing_paths: []\` instead of dropping the field when the implementation is still unknown.

Source = {type (user_message|friction_promotion|agent_observation|self_reflection|shipped_change|direction_outcome), message_id?, excerpt?, pr_number?, direction_id?, friction_id?}

When describing changes in prose, be precise: "opening gap g-xxx (from user_message msg-N)", "retiring direction d-xxx (gap g-yyy closed by merged PR #N)".

Before finishing, write ${RESEARCH_PROGRESS_PATH_TOKEN} ${READ_ONLY_BASH_WRITE_NOTE} — a concise markdown summary (≤3000 chars) capturing the model changes proposed, gaps opening/closing, and moves. This is a breadcrumb for retry agents if this task fails — lets a retry pick up from your analysis rather than redoing it from scratch.

## 7. Version goal (author or revise when warranted)

**Three distinct concepts — keep them separate:**
- \`intent.purpose\` = enduring PROJECT north star, operator-owned, spans all versions. Never touch this.
- \`version.goal\` = THIS version's current objective — a derivative of purpose that you may correct when evidence shows it is infeasible, circular, stale, or badly scoped.
- \`intent.current_focus\` = live "working on now" status — NOT the goal, advances across cycles.

Include a \`new_version_goal\` key at the **top level** of your problem_model JSON (a peer of \`intent\`, \`context\`, \`constraints\`, etc.) when the goal is unset OR when current delivery capabilities/evidence show the existing goal must be revised, split, or replaced. Single line, under 500 characters. The backend lifts this field before ProblemModel validation and applies it — it does not persist in the stored model.

The goal must be SUBSTANTIVE and DIRECTIONAL — a concrete deliverable statement that orients a new contributor reading it cold. It is the scoring target for the version until evidence warrants a correction.

Bad: "Fix CI, improve quality, pass audit." — feature list / status echo, rejected by the contentless-goal guard.
Good: a concrete deliverable paired with a measurable success bar a new contributor could verify cold — outcome-framed, not a feature list or status echo.

Omit \`new_version_goal\` when the existing goal remains sound. If it demands evidence the project cannot currently produce (for example promotion is disabled or no serving/deploy identity is available), do not loop on impossible work: revise/split the goal to an achievable candidate outcome, or park/replace the owning direction using your normal portfolio authority. Use normal \`spawn-task\` operations for work you judge useful; there is no special release-repair lane or fixed task count.

## 8. Acceptance criteria (conditional — author only when warranted)

Acceptance criteria are an authored RUBRIC — a small set of aspirational, goal-derived outcomes the qualifier grades at qualify time. They are NOT a coverage checklist to burn down during build/stabilize. Author them from the version GOAL, not from any coverage count.

Author criteria only when the version needs a root acceptance contract:
- When the version has a non-empty \`Goal:\` line but no acceptance criteria have been authored yet for this version — derive the rubric from that goal. Treat a version with a non-empty \`Goal:\` and no acceptance criteria listed as this no-criteria case.
- OR when active directions created this cycle have no criterion mappings and need to be tied to version-level outcomes.

Skip entirely if criteria already exist and are reasonably mapped. Do NOT fabricate criteria when the version goal is unset — Section 7 handles that case first.

When criteria are warranted, write \`${PROPOSED_CRITERIA_PATH_TOKEN}\` ${READ_ONLY_BASH_WRITE_NOTE} as the backend payload shape:

\`\`\`json
{"criteria":[{"text":"Users can complete the key workflow without manual operator intervention.","direction_keys":["d-xxx"]}],"retract":[{"criterion_key":"c-old-key","reason":"All directions that could produce covering evidence have been retired; this criterion is permanently uncoverable.","superseded_by_key":"c-new-key"}]}
\`\`\`

Each \`criteria\` item has:
- \`text\`: a short, concrete, independently verifiable criterion statement for the VERSION, not a direction description.
- \`direction_keys\`: the \`d-xxx\` identifiers of directions that contribute to that criterion; use an empty array only when no active direction maps cleanly yet.

The optional top-level \`retract\` array lets you retract or supersede an existing **active** criterion that has become permanently uncoverable — for example, when every direction that could produce covering evidence has been retired and no realistic path to covering the criterion remains. Each retraction item has:
- \`criterion_key\` (required): the \`c-xxx\` key of the active criterion to retract.
- \`reason\` (required): a short explanation of why the criterion is permanently uncoverable.
- \`superseded_by_key\` (optional): the \`c-xxx\` key of a different active criterion that replaces this one. Omit when retraction stands alone.

Retractions are processed before additions this cycle. Omit the \`retract\` key entirely when no active criterion needs retraction — absence is the default. **Never fabricate retractions.** Only retract a criterion when it is demonstrably uncoverable given the current direction portfolio (not merely hard or slow to cover).

Keep the total to 5-7 criteria maximum. Frame text as outcomes ("Users can X", "System handles Y"), not implementation steps ("Implement Z"). Ground every criterion in the version goal and active gap descriptions. If no criteria are needed this cycle, do not write the file at all.

${PLAYBOOK_GUIDANCE}

${DEFAULT_TOOL_GUIDANCE}
`;
}

/**
 * Resolve the stable-prefix path tokens to this task's concrete /tmp handoff
 * paths. This is the ONLY place the taskId-derived paths appear; because the
 * block lives in the volatile suffix, the cacheable prefix above stays
 * byte-identical across tasks with different taskIds.
 */
function buildStrategicHandoffPathsSection(p: ThinkerPromptParams): string {
  return [
    '# Handoff file paths (this task)',
    '',
    'The output sections above reference task-specific handoff files by the',
    'placeholder tokens below. Write each file to its concrete path — the',
    'Executor reads these exact files verbatim:',
    `- \`${RUNNING_UPDATE_PATH_TOKEN}\` → \`/tmp/running_update_${p.taskId}.md\``,
    `- \`${PROBLEM_MODEL_PATH_TOKEN}\` → \`/tmp/problem_model_${p.taskId}.json\``,
    `- \`${RESEARCH_PROGRESS_PATH_TOKEN}\` → \`/tmp/research_progress_${p.taskId}.md\``,
    `- \`${PROPOSED_CRITERIA_PATH_TOKEN}\` → \`/tmp/proposed_criteria_${p.taskId}.json\``,
    '',
  ].join('\n');
}

/**
 * Volatile suffix of the strategic thinker prompt — every per-cycle payload
 * section, emitted after the stable prefix so it cannot invalidate the
 * cacheable region. Everything that varies per call (clock, context prefix,
 * problem model, portfolio, messages) lives here; nothing stable may appear in
 * this function, and nothing volatile may be added to buildStrategicStablePrefix.
 */
function buildStrategicVolatileSuffix(p: ThinkerPromptParams): string {
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

  return `${buildStrategicHandoffPathsSection(p)}
${p.selfImprovementBlock}
${p.clockHeader}
${p.contextPrefix}
${p.packerMetaSection ? `\n${p.packerMetaSection}\n` : ''}
${p.payloadStalenessSection ? `\n${p.payloadStalenessSection}\n` : ''}
${p.strategicContext ? `\n## Running Context (from prior cycles)\n${p.strategicContext}\n` : ''}
${p.problemModelSection}
${p.cycleStartReconciliationSection ? `\n${p.cycleStartReconciliationSection}\n` : ''}

${p.reflectionSection}

${p.runningUpdateSection}
${p.runningUpdateSupersededSection || ''}
## Architecture (design document — overview index)
${p.designDocJson || 'No design document yet.'}
${p.designDocJson ? '_The above is an OVERVIEW INDEX of the design document — which systems and capabilities exist, their reconciled presence status, and declared backing module paths. `repo_overview` additionally carries inline bootstrap context (tech_stack, directory_layout, first_steps) for immediate orientation; the full section remains fetchable. It is NOT the full design doc. To read any section\'s FULL prose/specs, run `./design-doc get <key>` (e.g. `./design-doc get systems/auth`); run `./design-doc list` to enumerate every section key. Fetch the full text whenever you need a section\'s detailed prose or specs to reason or decide._' : ''}

${p.visionJson ? `## Vision (legacy artifact — use problem_model instead; retained for bootstrap reference only)\n${p.visionJson}\n` : ''}
${p.outcomeSection}${p.previousDecisionOutcomes}${p.pastRetirementCritiquesSection}${p.retiredDirectionsSummarySection}
${p.directionPortfolioSection}
${p.stagedDirectionsSection ? `\n${p.stagedDirectionsSection}\n` : ''}${p.userPausedSection}${p.rejectedProposalsSection}${p.operatorOverridesSection}
${p.availableCapabilitiesSection ? `\n${p.availableCapabilitiesSection}` : ''}${p.modelHealthSection}${p.pruneCandidatesSection ? `\n${p.pruneCandidatesSection}\n` : ''}${p.systemMaturitySection}${p.driftSection}${p.heldDirectionsSection ? `\n${p.heldDirectionsSection}` : ''}${p.efficiencySnapshotSection}${p.ciComplianceSection ? `\n${p.ciComplianceSection}` : ''}${p.completionDebtSection ? `\n${p.completionDebtSection}` : ''}${p.pendingFollowupsSection ? `\n${p.pendingFollowupsSection}` : ''}${p.sweepComparisonSection ? `\n${p.sweepComparisonSection}` : ''}${p.localRunnerSection ? `\n${p.localRunnerSection}\n` : ''}${p.compactionHintsSection ? `\n${p.compactionHintsSection}\n` : ''}
${p.recentWorkSection}${p.versionPhaseSection}${p.memoCitationsSection}

${bootstrapOrRecoverSection}
${gapMemoryTrimmedSection ? `\n${gapMemoryTrimmedSection}\n` : ''}
# Current user activity & open messages

The following reflects the live wall clock (relative-time strings such as "N days ago" / message ages). It is deliberately placed AFTER the stable guidance above so those clock-derived strings do not change the cacheable prompt prefix each turn — identical underlying payloads then render an identical prefix regardless of the current time.

${p.userStateLine}
${p.questionSection}${p.feedbackSection}${p.threadSection}
`;
}

/**
 * Assemble the strategic thinker prompt split at the cache boundary. The
 * byte-identical stable prefix leads; the volatile per-cycle payload follows.
 * buildThinkerPrompt returns only the final prompt for callers that do not
 * need the split.
 */
export function buildThinkerPromptParts(p: ThinkerPromptParams): {
  stablePrefix: string;
  variableBody: string;
  finalPrompt: string;
} {
  const stablePrefix = buildStrategicStablePrefix(p);
  const variableBody = buildStrategicVolatileSuffix(p);
  return assembleStableVariablePrompt(stablePrefix, variableBody);
}

export function buildThinkerPrompt(p: ThinkerPromptParams): string {
  return buildThinkerPromptParts(p).finalPrompt;
}

// ── Pass-1 gate: run thinker, verify artifacts, retry once if missing ────────
// Exported for unit testing. Returns { ok: true, analysisText, newVersionGoal? }
// when both /tmp/problem_model_<taskId>.json and
// /tmp/running_update_<taskId>.md exist after the thinker (or its focused retry)
// completes. Also reads the optional /tmp/new_version_goal_<taskId>.txt (Section 7
// output) and surfaces its content so the caller can pre-merge it into the
// problem_model JSON before the executor runs. On any failure path it calls
// persistAndPostStatus(FAILED, failure_category=strategic_thinker_artifacts_missing)
// and returns { ok: false } so the caller can simply return.
function realPurpose(value: unknown): string | undefined {
  if (typeof value !== 'string') return undefined;
  const purpose = value.trim();
  if (purpose === '' || purpose === '...' || /^<[^>]+>$/.test(purpose)) return undefined;
  return purpose;
}

type PurposeState =
  | { ok: true; changed: false; rejected: string; purpose?: string }
  | { ok: true; changed: true; rejected: string; purpose: string }
  | { ok: false; changed: false; rejected: string };

function materializeOperatorPurpose(
  model: Record<string, unknown>,
  currentPurpose?: string,
): PurposeState {
  const intent = model.intent as Record<string, unknown> | undefined;
  // Partial problem_model updates may omit intent entirely; the backend's
  // non-destructive merge preserves the durable operator-owned fields.
  if (intent == null) return { ok: true, changed: false, rejected: '' };

  const raw = typeof intent.purpose === 'string' ? intent.purpose.trim() : '';
  const existing = realPurpose(raw);
  if (existing) {
    return { ok: true, changed: false, rejected: raw, purpose: existing };
  }

  // The strategist cannot author operator intent. If it accidentally emits a
  // schema placeholder, restore the known durable value rather than spending
  // another LLM turn asking it to copy an operator-owned string. user_purpose
  // travels with the artifact when preserved; currentPurpose is independently
  // derived from the live project payload by the caller.
  const authoritative = realPurpose(intent.user_purpose) || realPurpose(currentPurpose);
  if (!authoritative) return { ok: false, changed: false, rejected: raw };
  intent.purpose = authoritative;
  return { ok: true, changed: true, rejected: raw, purpose: authoritative };
}

export async function runThinkerWithGate(
  thinkerPrompt: string,
  taskId: string,
  ws: WebSocket | null,
  statusRef: { value: string },
  model: string | undefined,
  effort: string | undefined,
  disallowedTools: string[],
  currentPurpose?: string,
): Promise<{ ok: true; analysisText: string; newVersionGoal?: string } | { ok: false }> {
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

  let problemModelParseable = false;

  // First, check if the file was already written to disk (authoritative source).
  if (fs.existsSync(problemModelPath)) {
    try {
      const fileContent = fs.readFileSync(problemModelPath, 'utf8');
      // eslint-disable-next-line @typescript-eslint/no-unsafe-assignment
      const parsed = JSON.parse(fileContent);
      // Validate the problem_model structure: reject schema-template placeholders
      // and ensure it's a valid object (not null, array, or primitive).
      if (parsed != null && typeof parsed === 'object' && !Array.isArray(parsed)) {
        const model = parsed as Record<string, unknown>;
        const purposeState = materializeOperatorPurpose(model, currentPurpose);
        if (!purposeState.ok) {
          sendLog(
            ws,
            `[WARNING] Problem model file exists but intent.purpose is a schema placeholder: "${purposeState.rejected}"; triggering repair.\n`,
          );
          problemModelParseable = false;
        } else {
          let purposePersisted = true;
          if (purposeState.changed) {
            try {
              fs.writeFileSync(problemModelPath, `${JSON.stringify(model, null, 2)}\n`);
              sendLog(
                ws,
                `[INFO] Restored operator-owned intent.purpose before thinker handoff validation.\n`,
              );
            } catch (e) {
              sendLog(
                ws,
                `[WARNING] Restored intent.purpose but failed to write ${problemModelPath}: ${String(e)}; triggering repair.\n`,
              );
              purposePersisted = false;
            }
          }
          problemModelParseable = purposePersisted;
          if (purposePersisted) {
            sendLog(ws, `[INFO] Problem model file exists on disk and is valid; skipping repair reopen.\n`);
          }
        }
      } else {
        sendLog(ws, `[WARNING] Problem model file exists but is not a valid object structure.\n`);
        problemModelParseable = false;
      }
    } catch (e) {
      sendLog(ws, `[WARNING] Problem model file exists but is invalid JSON: ${String(e)}\n`);
      problemModelParseable = false;
    }
  }

  // If file doesn't exist or is invalid, try parsing from stdout.
  if (!problemModelParseable) {
    try {
      const problemModelProbe = extractJsonObjectDetailed(analysisText, 'intent');
      if (problemModelProbe.ok) {
        const probeValue = problemModelProbe.value;
        const purposeState = materializeOperatorPurpose(probeValue, currentPurpose);
        if (purposeState.ok) {
          problemModelParseable = true;
          try {
            fs.writeFileSync(problemModelPath, `${JSON.stringify(probeValue, null, 2)}\n`);
            if (purposeState.changed) {
              sendLog(ws, `[INFO] Restored operator-owned intent.purpose in parsed thinker output.\n`);
            }
          } catch (e) {
            sendLog(ws, `[WARNING] Parsed strategic problem-model JSON but failed to write ${problemModelPath}: ${String(e)}\n`);
          }
        }
      }
    } catch {
      problemModelParseable = false;
    }
  }

  // Command-mode signal: thinker applied state via ./strategic commands rather
  // than writing inline JSON. Skip repair to avoid false-positive format misses
  // under the 'thinker' schema label. The filesPresent() check below still runs
  // and fires the focused retry if the handoff files were not written to disk.
  const isCommandMode = thinkerResult.allOutput.includes('./strategic ');

  let problemModelRepair: RepairStructuredOutputResult<Record<string, unknown>> | null = null;
  if (!problemModelParseable && !isCommandMode) {
    // Embed the thinker's prior analysis in the repair prompt so the fresh
    // session can extract the real problem_model JSON rather than echoing
    // schema-template placeholders (incident: a repair with no prior context
    // persisted intent.purpose = "Provide a parseable problem_model JSON
    // object..." and wiped constraints — PR #7435 / BUG_REPORT 8154c2d3).
    const progressPath = `/tmp/research_progress_${taskId}.md`;
    const progressBreadcrumb = fs.existsSync(progressPath)
      ? `\n\n===== RESEARCH PROGRESS BREADCRUMB =====\n${fs.readFileSync(progressPath, 'utf8').slice(0, 10_000)}\n===== END BREADCRUMB =====`
      : '';
    const priorAnalysis = thinkerResult.allOutput.slice(-60_000);

    problemModelRepair = await repairStructuredOutput<Record<string, unknown>>({
      mode: 'object',
      requiredKeys: [],
      schemaLabel: 'thinker',
      // Reject schema-template placeholder echoes (BUG_REPORT 8154c2d3 / PR #7435):
      // a repair session with insufficient context may echo back the schema reminder
      // text as actual values (e.g. purpose="<from analysis>" or purpose="..." or
      // empty string). Routing the guard here ensures recordFormatRepairSuccess only
      // fires when the value is actually accepted, not merely parseable.
      // Accept partial problem_model updates that omit intent (entries omitted are
      // preserved by non-destructive merge); validate intent.purpose when present.
      validateParsedValue: (value) => {
        const intent = (value as Record<string, unknown>).intent as Record<string, unknown> | undefined;
        // Partial updates may omit intent entirely — that's legitimate. Only validate
        // when intent is present.
        if (intent == null) {
          return { ok: true };
        }
        const purpose = typeof intent.purpose === 'string' ? intent.purpose.trim() : '';
        // Reject empty-string, '...', or a full XML/schema-tag value like
        // '<from analysis>'. A legitimate purpose starting with '<' but not
        // ending with '>' won't match the full-tag regex, so real values are safe.
        const isSchemaPlaceholder = realPurpose(purpose) == null;
        if (isSchemaPlaceholder) {
          return {
            ok: false,
            reason: 'parsed_object_schema_mismatch',
            detail: `intent.purpose is a schema-template placeholder: "${purpose}"`,
          };
        }
        return { ok: true };
      },
      first: {
        exitCode: thinkerResult.exitCode,
        allOutput: thinkerResult.allOutput,
        lastResultText: thinkerResult.lastResultText,
      },
      reopenPrompt: [
        ...(currentPurpose
          ? [
              `CRITICAL: The operator-set durable intent.purpose for this project is: "${currentPurpose}". In your problem_model JSON output, set intent.purpose to exactly this string — do NOT substitute it with "...", "&lt;...&gt;", or an empty string. This value is authoritative; the prior analysis below may omit it, but your output MUST include it.`,
              '',
            ]
          : []),
        'Your strategic run completed cleanly, but the required problem_model JSON was not parseable.',
        'IMPORTANT: this is a FRESH session — you do NOT have the previous conversation.',
        'Your previous analysis output is reproduced verbatim below.',
        'Find the problem_model JSON in it and output ONLY that JSON object now — no markdown, no explanation, no code fences.',
        'Do NOT use placeholder or template values. Extract the real content from the analysis below.',
        'CRITICAL: Do NOT blank operator-owned fields. If intent.purpose, intent.user_purpose, or constraints appear in the prior analysis with real content, preserve them exactly — do not substitute schema template text, "...", or empty values.',
        `Write the JSON for ${problemModelPath}.`,
        'Schema reminder (use this for structure only — the EXAMPLE values below show the shape; fill with REAL values extracted from the analysis above):',
        '{"intent":{"purpose":"Build an automated billing system for SaaS subscriptions","current_focus":"Implementing the invoice generation pipeline","user_purpose":"Reduce manual billing overhead and prevent revenue leakage"},"context":{},"constraints":[],"capabilities":[],"gaps":[],"archived_gaps":[],"friction":[],"pending_user_confirmation":[],"revision_log":[],"rejected_agent_edits":[]}',
        '',
        '===== YOUR PREVIOUS ANALYSIS OUTPUT (verbatim) =====',
        priorAnalysis,
        '===== END PREVIOUS ANALYSIS OUTPUT =====',
        progressBreadcrumb,
      ].join('\n'),
      ws,
      statusRef,
      model,
      effort,
      disallowedTools,
      retryStatus: 'Repairing strategic problem-model JSON...',
    });
    if (problemModelRepair.value !== null) {
      try {
        fs.writeFileSync(problemModelPath, `${JSON.stringify(problemModelRepair.value, null, 2)}\n`);
        sendLog(ws, `[INFO] Recovered strategic problem-model JSON via bounded repair and wrote ${problemModelPath}\n`);
      } catch (e) {
        sendLog(ws, `[WARNING] Recovered strategic problem-model JSON but failed to write ${problemModelPath}: ${String(e)}\n`);
      }
    }
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
    const priorOutput = (problemModelRepair?.retry?.allOutput || thinkerResult.allOutput).slice(-80_000);
    const retryPrompt = [
      ...(currentPurpose
        ? [
            `CRITICAL: The operator-set durable intent.purpose for this project is: "${currentPurpose}". In your problem_model JSON output, set intent.purpose to exactly this string — do NOT substitute it with "...", "&lt;...&gt;", or an empty string. This value is authoritative; the prior analysis below may omit it, but your output MUST include it.`,
            '',
          ]
        : []),
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

  // Validate the final problem_model file content: ensure it's not just valid JSON
  // but also a valid problem_model structure (not a schema placeholder or invalid object).
  if (fs.existsSync(problemModelPath)) {
    try {
      const finalContent = fs.readFileSync(problemModelPath, 'utf8');
      // eslint-disable-next-line @typescript-eslint/no-unsafe-assignment
      const finalModel = JSON.parse(finalContent);
      if (finalModel != null && typeof finalModel === 'object' && !Array.isArray(finalModel)) {
        const intent = (finalModel as Record<string, unknown>).intent as Record<string, unknown> | undefined;
        // If intent is present, it must have a valid non-placeholder purpose.
        if (intent != null) {
          const purposeState = materializeOperatorPurpose(
            finalModel as Record<string, unknown>,
            currentPurpose,
          );
          if (!purposeState.ok) {
            const purpose = purposeState.rejected;
            sendLog(
              ws,
              `\n[ERROR] Final problem_model file has invalid intent.purpose after all retry attempts: "${purpose}"\n`,
            );
            try {
              await persistAndPostStatus(taskId, {
                task_id: taskId,
                status: 'FAILED',
                artifact_type: 'ERROR_LOG',
                logs: `Problem model validation failed: intent.purpose is a schema placeholder "${purpose}"`,
                failure_category: 'strategic_thinker_artifacts_missing',
              });
            } catch (e) {
              sendLog(ws, `[ERROR] Failed to post validation failure: ${String(e)}\n`);
            }
            return { ok: false };
          }
          if (purposeState.changed) {
            if (intent.source == null && realPurpose(intent.user_purpose)) {
              intent.source = { type: 'user_message', excerpt: purposeState.purpose.slice(0, 120) };
            }
            try {
              fs.writeFileSync(problemModelPath, `${JSON.stringify(finalModel, null, 2)}\n`);
              sendLog(
                ws,
                `\n[INFO] Restored operator-owned intent.purpose: "${purposeState.purpose.slice(0, 80)}"\n`,
              );
            } catch (writeErr) {
              sendLog(
                ws,
                `\n[WARNING] Restored intent.purpose but failed to write file: ${String(writeErr)}\n`,
              );
              try {
                await persistAndPostStatus(taskId, {
                    task_id: taskId,
                    status: 'FAILED',
                    artifact_type: 'ERROR_LOG',
                  logs: 'Problem model validation failed: restored intent.purpose could not be persisted',
                    failure_category: 'strategic_thinker_artifacts_missing',
                  });
                } catch (e) {
                  sendLog(ws, `[ERROR] Failed to post validation failure: ${String(e)}\n`);
                }
                return { ok: false };
              }
            }
        }
      } else {
        sendLog(ws, `\n[ERROR] Final problem_model file is not a valid object structure.\n`);
        try {
          await persistAndPostStatus(taskId, {
            task_id: taskId,
            status: 'FAILED',
            artifact_type: 'ERROR_LOG',
            logs: 'Problem model validation failed: not a valid object structure',
            failure_category: 'strategic_thinker_artifacts_missing',
          });
        } catch (e) {
          sendLog(ws, `[ERROR] Failed to post validation failure: ${String(e)}\n`);
        }
        return { ok: false };
      }
    } catch (e) {
      sendLog(ws, `\n[ERROR] Final problem_model file is invalid JSON: ${String(e)}\n`);
      try {
        await persistAndPostStatus(taskId, {
          task_id: taskId,
          status: 'FAILED',
          artifact_type: 'ERROR_LOG',
          logs: `Problem model validation failed: invalid JSON - ${String(e)}`,
          failure_category: 'strategic_thinker_artifacts_missing',
        });
      } catch (e2) {
        sendLog(ws, `[ERROR] Failed to post validation failure: ${String(e2)}\n`);
      }
      return { ok: false };
    }
  }

  // Extract the optional new_version_goal — primary: first-class field in the
  // problem_model JSON (the authoritative handoff file the gate already validated);
  // fallback: legacy tmp file written by older thinker agents for backward compat.
  let newVersionGoal: string | undefined;
  try {
    const pmRaw = fs.readFileSync(problemModelPath, 'utf8');
    const pm = JSON.parse(pmRaw) as Record<string, unknown>;
    if (typeof pm.new_version_goal === 'string') {
      const trimmed = pm.new_version_goal.trim();
      if (trimmed) newVersionGoal = trimmed;
    }
  } catch {
    // problem_model JSON unreadable at this point — fall through to tmp-file fallback.
  }
  if (!newVersionGoal) {
    const newVersionGoalPath = `/tmp/new_version_goal_${taskId}.txt`;
    if (fs.existsSync(newVersionGoalPath)) {
      try {
        const raw = fs.readFileSync(newVersionGoalPath, 'utf8').trim();
        if (raw) newVersionGoal = raw;
      } catch (e) {
        sendLog(ws, `[WARNING] Failed to read ${newVersionGoalPath}: ${String(e)}\n`);
      }
    }
  }

  return { ok: true, analysisText, newVersionGoal };
}
