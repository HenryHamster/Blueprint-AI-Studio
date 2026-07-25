import fs from 'fs';
import type WebSocket from 'ws';
import {
  WORKSPACE, strVal, sanitize, sendLog, readHandlerOutput,
  persistAndPostStatus, runClaudeCode,
  apiGet, apiPost,
  resolveBaseBranch,
} from '../index.js';
import { VmConfigMissingError } from '../providers/runners.js';
import * as indexMod from '../index.js';
import { prepareWorkspaceForRole } from '../workspacePrep.js';
import {
  writeGitTokenFile, setupGitCredentialHelper,
} from '../git-credential.js';
import {
  buildContextPrefix, configureAgentPermissions,
  writePlaybookHelper, writeMessagesHelper,
} from '../prompt.js';
import {
  writeStrategicHelpers,
  runStrategicChallenge,
  buildStrategicChallengeExecutorSection,
  blockedChallengeRecord,
  renderStrategicFetchVerbHelpLines,
} from './strategic-helpers.js';
import { parseBackendTimestamp } from '../utils/time.js';
import { reconcileDesignDocIndex } from '../utils/design-doc-presence.js';
import {
  formatPreviousDecisionOutcomes,
  formatPastRetirementCritiques,
  formatRetiredDirectionsSummary,
} from './research-direction-context.js';
import {
  computeStrategicPromptFingerprint,
  buildThinkerPrompt,
  runThinkerWithGate,
} from '../prompts/thinker.js';
import { buildPromptSizeTelemetry, setPromptSizeTelemetry } from '../telemetry.js';
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
  buildHeldDirectionsSection,
  buildOutcomeSection,
  buildClockAndRecentWorkSection,
  buildConversationThreadSection,
  buildFeedbackSection,
  buildQuestionSection,
  buildOpenMessagesText,
  buildDirSummaryText,
  buildDirectionPortfolioSection,
  buildSweepComparisonSection,
  buildCiComplianceSection,
  buildCompletionDebtSection,
  buildPendingFollowupsSection,
  buildPayloadStalenessSection,
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

export function resolveStrategicPurpose(
  payload: Record<string, unknown>,
  problemModel: Record<string, unknown> | undefined,
): string | undefined {
  const intent = problemModel?.intent as Record<string, unknown> | undefined;
  const vision = payload.vision as Record<string, unknown> | undefined;
  const candidates = [
    payload.effective_purpose,
    intent?.user_purpose,
    intent?.purpose,
    vision?.user_purpose,
    vision?.purpose,
  ];

  for (const candidate of candidates) {
    if (typeof candidate !== 'string') continue;
    const normalized = candidate.trim();
    if (normalized && normalized !== '...' && !/^<[^>]+>$/.test(normalized)) return normalized;
  }
  return undefined;
}

/**
 * Render an ISO timestamp as a deterministic recency string relative to a
 * caller-supplied reference epoch (`refMs`) — NOT the VM wall clock. Passing the
 * same `(iso, refMs)` always yields the same bytes, so a strategic prompt built
 * twice from an identical payload stays byte-identical regardless of the current
 * time. This is the v24 prompt-cache cost lever (direction d-27685510): Anthropic
 * prefix-cache reads bill at ~10% of base input, and a single wall-clock byte in
 * the prompt would bust the automatic prefix match for everything after it.
 *
 * `refMs` should be the backend-baked payload-generation epoch. When it is not a
 * finite number (the backend supplied no timestamp) the recency signal is still
 * preserved but rendered as the absolute UTC date, which is likewise clock-free —
 * never the live wall clock. Returns '' for empty/unparseable input.
 */
export function formatRelativeDays(iso: string, refMs: number): string {
  if (!iso) return '';
  try {
    const d = parseBackendTimestamp(iso);
    if (!d) return '';
    if (!Number.isFinite(refMs)) {
      // No deterministic reference — fall back to the absolute UTC date so the
      // text never reads the VM wall clock (which would bust the cache prefix).
      return `on ${d.toISOString().slice(0, 10)}`;
    }
    const days = Math.max(0, Math.floor((refMs - d.getTime()) / 86400000));
    if (days === 0) return 'today';
    if (days === 1) return '1 day ago';
    return `${days} days ago`;
  } catch { return ''; }
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
 * Render the backend-supplied `local_runner_status` block as a short prompt note
 * so the strategist factors LIVE self-hosted GPU availability into whether to
 * author GPU `compute_experiment` tasks. The backend ships this object only when
 * the project has registered a self-hosted runner OR declares the enforce-local
 * policy (require_local_runner). Shape:
 * `{ever_registered, active_total, active_gpu, require_local_runner}` plus an
 * optional bounded `devices` list (one entry per live runner: `{runner_name,
 * gpu_count, gpu_models, gpu_vram_mib?, container_ok, gpu_in_container_ok,
 * active_tasks, active_compute_jobs}`) rendered so the strategist can size
 * experiments to actual VRAM/device counts and container capability.
 *
 * When no GPU runner is live the strategist should AVOID authoring GPU
 * experiments: with require_local_runner they are held then terminally fail;
 * without it they fall back to billable cloud GPU. Returns '' when absent or
 * malformed so the section is omitted on the common path. Defined here (not in
 * strategic-sections.ts) so it does not have to be threaded through that module's
 * exhaustive vi.mock in strategic.test.ts.
 */
export function buildLocalRunnerSection(status: unknown): string {
  if (!status || typeof status !== 'object' || Array.isArray(status)) return '';
  const s = status as Record<string, unknown>;
  const activeTotal = typeof s.active_total === 'number' ? s.active_total : 0;
  const activeGpu = typeof s.active_gpu === 'number' ? s.active_gpu : 0;
  const everRegistered = s.ever_registered === true;
  const requireLocal = s.require_local_runner === true;
  const bakedImageRef = typeof s.baked_image_ref === 'string' && s.baked_image_ref ? s.baked_image_ref : null;
  // Backend gates inclusion on (ever_registered || require_local_runner || baked_image_ref);
  // re-check defensively so a stray payload never renders an empty/misleading note.
  if (!everRegistered && !requireLocal && !bakedImageRef) return '';

  const lines: string[] = ['## Self-hosted compute (local runner availability)'];
  if (bakedImageRef) {
    lines.push(
      `- Project-baked compute image: \`${bakedImageRef}\`. `
      + 'PREFER to omit `container_image` in compute_experiments — the platform defaults to this digest-pinned image automatically. '
      + 'Only set `container_image` explicitly when you need a DIFFERENT image (e.g. a specific NGC stack the baked image does not cover).',
    );
  }
  if (everRegistered || requireLocal) {
    lines.push(
      `- Live runners: ${activeTotal} total, ${activeGpu} GPU-capable`
      + (requireLocal
        ? ' — this project ENFORCES local-only compute (require_local_runner=true), so a compute_experiment never bills cloud.'
        : '.'),
    );
  }
  if (everRegistered || requireLocal) {
    if (activeGpu > 0) {
      lines.push(
        '- GPU hardware is ONLINE: a GPU `compute_experiment` will run on the operator\'s self-hosted runner. Authoring one is fine.',
      );
    } else if (requireLocal) {
      lines.push(
        '- GPU hardware is OFFLINE and this project is local-only: a GPU `compute_experiment` authored now is HELD (never billed to cloud) and terminally fails after the hold window. Do NOT author GPU experiments until a GPU runner is live — defer the hypothesis or take a non-GPU approach.',
      );
    } else {
      lines.push(
        '- GPU hardware is OFFLINE: a GPU `compute_experiment` authored now would FALL BACK to billable cloud GPU. Prefer to defer GPU experiments until the operator\'s runner is live, unless the cloud spend is justified.',
      );
    }
  }
  // Project-scoped runner ids make the existing compute_experiment
  // runner_target contract usable without exposing the global fleet. This is
  // advisory telemetry: eligibility is rechecked by dispatch when the job is
  // claimed, so a host can change state after this prompt was built.
  const runners: unknown[] = Array.isArray(s.runners) ? s.runners : [];
  if (runners.length > 0) {
    const formatBytes = (value: unknown): string | null => (
      typeof value === 'number' && value >= 0
        ? `${(value / (1024 ** 3)).toFixed(1)} GiB`
        : null
    );
    lines.push('- Project runner inventory (use the exact UUID only when a host preference is useful):');
    for (const entry of runners.slice(0, 8)) {
      if (!entry || typeof entry !== 'object') continue;
      const r = entry as Record<string, unknown>;
      const runnerId = typeof r.runner_id === 'string' ? r.runner_id : '';
      if (!runnerId) continue;
      const hostname = typeof r.hostname === 'string' && r.hostname ? r.hostname : `runner-${runnerId.slice(0, 8)}`;
      const persistedStatus = typeof r.status === 'string' ? r.status : 'unknown';
      // A row can remain status=active after its heartbeat has expired. Never
      // present that stale persisted state as evidence that the host is usable.
      const state = r.live === true ? 'LIVE' : `STALE/OFFLINE (persisted status: ${persistedStatus})`;
      const heartbeat = typeof r.last_heartbeat_at === 'string' ? `; last heartbeat ${r.last_heartbeat_at}` : '';
      const gpuCount = typeof r.gpu_count === 'number' ? r.gpu_count : 0;
      const container = r.container_ok === true
        ? (r.gpu_in_container_ok === true ? 'containers+GPU OK' : 'containers OK, GPU passthrough unverified')
        : 'no container runtime';
      const active = typeof r.active_compute_jobs === 'number' ? r.active_compute_jobs : 0;
      const max = typeof r.max_concurrent_compute === 'number' ? r.max_concurrent_compute : null;
      const workspace = formatBytes(r.workspace_free_bytes) ?? formatBytes(r.disk_free_bytes);
      const imageStore = formatBytes(r.container_image_store_free_bytes);
      const storage = [
        workspace ? `workspace/disk ${workspace}` : null,
        imageStore ? `image store ${imageStore}` : null,
      ].filter((part): part is string => part !== null).join(', ');
      lines.push(
        `  - ${hostname} [${runnerId}]: ${state}${heartbeat}; ${gpuCount} GPU(s); ${container}; `
        + `compute ${active}${max !== null ? `/${max}` : ''}`
        + (storage ? `; ${storage}` : ''),
      );
    }
    lines.push(
      '  To prefer one listed host, add `"runner_target": {"runner_id": "<UUID>", "strict": false}` to a compute_experiment spawn. Soft is the default: dispatch falls back when that host is busy/offline. Use `strict: true` ONLY when the experiment genuinely cannot run on another allowed runner; it intentionally waits for that exact host.',
    );
  }
  // Per-device hardware detail (backend-bounded per-runner AND in total under
  // the experiment-authoring shared budget) so the strategist can reason about
  // VRAM fit, device counts, and container capability when sizing experiments.
  const devices: unknown[] = Array.isArray(s.devices) ? s.devices : [];
  if (devices.length > 0) {
    lines.push('- Devices (live runners — size experiments to fit this hardware):');
    for (const entry of devices.slice(0, 8)) {
      if (!entry || typeof entry !== 'object') continue;
      const d = entry as Record<string, unknown>;
      const name = typeof d.runner_name === 'string' && d.runner_name ? d.runner_name : 'runner';
      const gpuCount = typeof d.gpu_count === 'number' ? d.gpu_count : 0;
      const models = Array.isArray(d.gpu_models) ? d.gpu_models.filter((m) => typeof m === 'string').join(', ') : '';
      const vramVals = Array.isArray(d.gpu_vram_mib)
        ? (d.gpu_vram_mib as unknown[]).filter((v): v is number => typeof v === 'number' && v > 0)
        : [];
      const vram = vramVals.length > 0 ? `, ${Math.min(...vramVals)} MiB VRAM/GPU` : '';
      const gpuBit = gpuCount > 0 ? `${gpuCount}x GPU${models ? ` (${models})` : ''}${vram}` : 'no GPU';
      const containerBit = d.container_ok === true
        ? (d.gpu_in_container_ok === true ? 'containers OK (GPU-in-container OK)' : 'containers OK (no GPU passthrough)')
        : 'NO container runtime (container_image would be discarded)';
      const tasks = typeof d.active_tasks === 'number' ? d.active_tasks : 0;
      const jobs = typeof d.active_compute_jobs === 'number' ? d.active_compute_jobs : 0;
      lines.push(`  - ${name}: ${gpuBit}; ${containerBit}; load ${tasks} task(s) + ${jobs} compute job(s)`);
    }
    lines.push(
      '  When per-GPU VRAM is listed, author experiments whose model + batch fit within it (a job needing more VRAM than any device has can NEVER run locally); target `container_image` only at devices with containers OK.',
    );
  }
  return lines.join('\n');
}

/**
 * Build a prompt section listing gaps whose history entries warrant compaction.
 * Takes the backend's ``compaction_hints`` list (each item has gap_id, name,
 * history_entry_count, past_moves_count, total_entries) and renders a compact
 * advisory block so the strategist can apply targeted ``_compactions``
 * watermarks. Returns '' when the list is absent or empty.
 */
function buildCompactionHintsSection(
  hints: Array<Record<string, unknown>> | undefined,
): string {
  if (!hints || !Array.isArray(hints) || hints.length === 0) return '';

  const lines: string[] = [
    '## Gap compaction candidates',
    'The following gaps have accumulated enough history entries to warrant `_compactions` watermark compaction. Run `./fetch problem-model` (or `./fetch gap <gap_id>`) to review the full durable history before emitting compaction directives.',
  ];
  for (const h of hints) {
    const id = strVal(h.gap_id, '');
    const name = strVal(h.name, '');
    const hist = Number(h.history_entry_count ?? 0);
    const pm = Number(h.past_moves_count ?? 0);
    lines.push(`- ${id}: "${name}" (${hist} history entries, ${pm} past_moves)`);
  }
  lines.push(
    '',
    'Emit `_compactions` directives in your problem-model update to permanently compact the history you have verified. See the "Model-size hygiene" section for the compaction contract.',
  );
  return lines.join('\n');
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

  // ── Git credential helper setup ──────────────────────────────────────────
  // Write the initial token then switch the remote URL from the baked-in
  // token-embedded form to a plain HTTPS URL backed by a credential helper that
  // reads a refreshed token file. This prevents git operations from failing
  // after the 60-minute GitHub App installation token TTL.
  writeGitTokenFile(ghToken);
  try {
    setupGitCredentialHelper(owner, repo, WORKSPACE);
    sendLog(ws, '[strategic] git credential helper configured (push-capable, auto-refresh)\n');
  } catch (e) {
    sendLog(ws, `[strategic] WARNING: git credential helper setup failed (git push may expire): ${String(e)}\n`);
  }

  writePlaybookHelper(taskId);
  writeMessagesHelper(taskId);
  const researchMode = strVal(payload.research_mode, 'research');
  const disallowedTools = configureAgentPermissions(researchMode);

  // Deterministic reference epoch for every relative-time string rendered into
  // this prompt (failed-approach "Nd ago" in contextPrefix, userStateLine
  // "N days ago"). Sourced from the backend-baked payload timestamp — NOT the VM
  // wall clock — so two builds of an identical payload produce a byte-identical
  // cacheable prefix (v24 cost lever, direction d-27685510: prefix-cache reads
  // bill at ~10% of base input). NaN when the backend supplied no timestamp, in
  // which case the relative-time renderers degrade to a clock-free absolute date.
  const payloadStaleness = payload.payload_staleness && typeof payload.payload_staleness === 'object'
    ? payload.payload_staleness as Record<string, unknown>
    : undefined;
  const payloadClockRefIso =
    strVal(payloadStaleness?.generated_at) || strVal(payloadStaleness?.checked_at);
  const payloadClockRefDate = payloadClockRefIso ? parseBackendTimestamp(payloadClockRefIso) : null;
  const payloadClockRefMs = payloadClockRefDate ? payloadClockRefDate.getTime() : NaN;

  const contextPrefix = buildContextPrefix(task, { nowMs: payloadClockRefMs });

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
    // Trim-proof inbox depth: shipped at critical tier so it survives even when
    // open_messages itself is dropped under byte budget pressure.
    const openMessageCount = typeof payload.open_message_count === 'number'
      ? payload.open_message_count
      : openMessages.length;

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

    const heldDirections = Array.isArray(payload.held_directions)
      ? payload.held_directions as Array<Record<string, unknown>>
      : [];

    const systemMaturitySection = buildSystemMaturitySection(systemMaturity);
    const driftSection = buildDriftSection(driftSignals);
    const heldDirectionsSection = buildHeldDirectionsSection(heldDirections);

    const efficiencySnapshotSection = buildEfficiencySnapshotSection(payload.efficiency_snapshot);
    const ciComplianceSection = buildCiComplianceSection(payload.ci_compliance);
    const completionDebtSection = buildCompletionDebtSection(payload.open_completion_debt);
    const pendingFollowupsSection = buildPendingFollowupsSection(payload.pending_followups);
    const compactionHintsSection = buildCompactionHintsSection(
      payload.compaction_hints as Array<Record<string, unknown>> | undefined,
    );

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

    // ── Compute direction portfolio section for thinker prompt ──
    const maxActiveDirections = typeof payload.max_active_directions === 'number'
      ? payload.max_active_directions
      : 10;
    const activeDirections = directionsSummary.filter(d => strVal(d.status, 'active') === 'active');
    const outcomeSystems = outcomeEval && Array.isArray(outcomeEval.systems)
      ? outcomeEval.systems as Array<Record<string, unknown>>
      : (vision && Array.isArray(vision.systems) ? vision.systems as Array<Record<string, unknown>> : []);
    const activeByScopeForPortfolio = (payload.recent_interventions as Record<string, unknown> | undefined)
      ?.active_by_scope as Record<string, number> | undefined;
    const directionPortfolioSection = buildDirectionPortfolioSection(
      directionsSummary,
      maxActiveDirections,
      outcomeSystems,
      activeByScopeForPortfolio,
      authoritativeActive,
      authoritativePaused,
      authoritativeSystemCounts,
    );

    // Portfolio composition: still computed for the strategic challenge pass
    // (runStrategicChallenge receives dirSummaryText and portfolioCompositionText).
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
    // Use the backend-resolved value first, with legacy payload fallbacks.
    const currentPurpose = resolveStrategicPurpose(payload, problemModel);

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

    // formatRelativeDays is a clock-free module-scope helper: it renders relative
    // to the deterministic payload epoch (payloadClockRefMs), so userStateLine
    // stays byte-identical across two builds of an identical payload.
    const userActiveRel = formatRelativeDays(userLastActiveAt, payloadClockRefMs);
    const ackRel = formatRelativeDays(runningUpdateAckAt, payloadClockRefMs);
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
    // Sections whose recovery path is the generic section-fetch endpoint carry a
    // `fetch_verb` of "sections" in the packer meta. Render the command with the
    // section name so the strategist gets an actionable `./fetch sections <name>`.
    const SECTION_FETCH_VERB = 'sections';
    const sectionFetchHint = (sectionName: string): string =>
      `./fetch sections ${sectionName}`;
    // Build the "; run ... " recovery clause for a packer-meta record. When the
    // fetch verb is the generic section verb, emit the exact `./fetch sections <name>`
    // CLI command — 'sections' is a real FetchVerb granted to strategic agents.
    const recoveryClause = (fetchVerb: string, key: string, tail: string): string => {
      if (!fetchVerb) return '';
      if (fetchVerb === SECTION_FETCH_VERB) {
        return `; run \`./fetch sections ${key}\` ${tail}`;
      }
      return `; run \`./fetch ${fetchVerb}\` ${tail}`;
    };
    // Maps each strategic payload key to the ./fetch command that recovers it.
    const PAYLOAD_KEY_FETCH_VERB: Record<string, string> = {
      'problem_model':              './fetch problem-model',
      'directions_summary':         './fetch direction',
      'staged_directions':          './fetch direction',
      'open_messages':              './fetch message',
      'knowledge_index':            './fetch knowledge',
      'version_phase':              './fetch version-phase',
      'design_doc':                 './fetch section <key>',
      'capability_trial_verdicts':  './fetch capability-trials',
      'compute_experiment_sweeps':  './fetch compute-sweeps',
      'efficiency_snapshot':        './fetch efficiency-snapshot',
      'ci_compliance':              './fetch ci-compliance',
      'open_completion_debt':       './fetch completion-debt',
      // sections-type keys: served by /api/agent/projects/{id}/sections/{name}.
      // retirement_deferrals is intentionally omitted: it has no recovery path
      // (SECTION_FETCH_INVENTORY maps it to None), so advertising any fetch
      // would mislead.
      'self_reflection_log':        sectionFetchHint('self_reflection_log'),
      'anomaly_summary':            sectionFetchHint('anomaly_summary'),
      'playbook_summary':           sectionFetchHint('playbook_summary'),
      'retired_directions_summary': sectionFetchHint('retired_directions_summary'),
      'rejected_proposals':         sectionFetchHint('rejected_proposals'),
      'past_retirement_critiques':  sectionFetchHint('past_retirement_critiques'),
      'previous_decision_outcomes': sectionFetchHint('previous_decision_outcomes'),
      'outcome_evaluation':         sectionFetchHint('outcome_evaluation'),
    };
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
          const bytesOrig = typeof t.bytes_original === 'number'
            ? ` (${t.bytes_original}→${typeof t.bytes_trimmed === 'number' ? t.bytes_trimmed : '?'} bytes)` : '';
          const fetchVerb = strVal(t.fetch_verb);
          const recovery = recoveryClause(fetchVerb, k, 'for full detail');
          if (kind === 'list_trimmed') {
            const shown = (t.shown as number | undefined) ?? '?';
            const total = (t.total as number | undefined) ?? '?';
            lines.push(`  - ${k}: showing ${shown} of ${total} entries${bytesOrig}${recovery}`);
          } else {
            lines.push(`  - ${k}: shipped in ${kind} form${bytesOrig}${recovery}`);
          }
          const fetchHint = PAYLOAD_KEY_FETCH_VERB[k];
          if (fetchHint) lines.push(`    → ${fetchHint}`);
        }
      }
      if (dropped.length > 0) {
        lines.push('', 'Dropped entirely (not in this payload):');
        for (const d of dropped) {
          const k = strVal(d.key, '?');
          const reason = strVal(d.reason, 'budget exhausted');
          const bytes = typeof d.bytes === 'number' ? ` (${d.bytes} bytes)` : '';
          const fetchVerb = strVal(d.fetch_verb);
          const recovery = recoveryClause(fetchVerb, k, 'to recover');
          lines.push(`  - ${k}${bytes}: ${reason}${recovery}`);
          const fetchHint = PAYLOAD_KEY_FETCH_VERB[k];
          if (fetchHint) lines.push(`    → ${fetchHint}`);
        }
      }
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
    // payloadStaleness (extracted above for the deterministic clock reference) is
    // reused here for its rendered section.
    const payloadStalenessSection = buildPayloadStalenessSection(payloadStaleness);
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
    const thinkerParams = {
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
      directionPortfolioSection,
      stagedDirectionsSection,
      // Live self-hosted local-runner availability note (M7) — built from the
      // backend's `local_runner_status` payload field so the strategist avoids
      // authoring GPU compute_experiments while the operator's hardware is offline.
      localRunnerSection: buildLocalRunnerSection(payload.local_runner_status),
      userPausedSection: buildUserPausedSection(directionsSummary),
      rejectedProposalsSection: buildRejectedProposalsSection(Array.isArray(payload.rejected_proposals) ? payload.rejected_proposals as Array<Record<string, unknown>> : []),
      operatorOverridesSection: buildOperatorOverridesSection(Array.isArray(payload.operator_overrides) ? payload.operator_overrides as Array<Record<string, unknown>> : []),
      modelHealthSection,
      pruneCandidatesSection,
      systemMaturitySection,
      driftSection,
      heldDirectionsSection,
      recentWorkSection,
      feedbackSection,
      questionSection,
      threadSection,
      taskId,
      versionPhaseSection: buildVersionPhaseSection(versionPhase),
      memoCitationsSection: buildMemoCitationsSection(payload.memo_citations),
      efficiencySnapshotSection,
      ciComplianceSection,
      completionDebtSection,
      pendingFollowupsSection,
      compactionHintsSection,
      // Shared capability vocabulary + uniform provides_capability / prerequisite
      // declaration guidance, so the strategist draws cross-lane capability refs
      // from the same registry namespace every other task type uses.
      availableCapabilitiesSection: buildStrategicAvailableCapabilitiesSection(
        payload.available_capabilities,
      ),
      // Fully-verdicted multi-arm sweep synthesis: every completed sweep demands
      // exactly one follow-on move from the thinker so no sweep is silently skipped.
      sweepComparisonSection: buildSweepComparisonSection(
        payload.compute_experiment_sweeps as Record<string, Array<Record<string, unknown>>> | undefined,
      ),
      payloadStalenessSection,
    };

    const thinkerPrompt = buildThinkerPrompt(thinkerParams);
    const promptSuffix = renderStrategicPromptSuffix(payload);
    const promptFingerprint = computeStrategicPromptFingerprint(
      thinkerParams,
      promptSuffix,
    );
    const setPromptFingerprint = (indexMod as { setCurrentPromptFingerprint?: (fp: string) => void }).setCurrentPromptFingerprint;
    if (setPromptFingerprint) setPromptFingerprint(promptFingerprint);
    const sendHeartbeat = (indexMod as { sendHeartbeatSync?: (taskId: string, status: string, extra?: Record<string, unknown>) => void }).sendHeartbeatSync;
    if (sendHeartbeat) sendHeartbeat(taskId, statusRef.value, { prompt_fingerprint: promptFingerprint });

    // Append the backend-supplied prompt_suffix as "Additional instructions"
    // (same convention as every other task type). This guidance is reasoning
    // input — deployment framing, stabilize audit-findings hints, stall nudges,
    // mode hints — so it belongs in the analysis pass that produces decisions.
    const thinkerPromptWithSuffix = thinkerPrompt + promptSuffix;

    // ── Prompt section telemetry ──
    // Build per-section byte attribution from the major thinker-prompt blocks so
    // strategic-turn cost is visible in the per-section cost breakdown. Sections
    // map 1:1 to thinkerParams fields; buildPromptSizeTelemetry sorts descending
    // and caps at the top 3 for heartbeat payload size.
    const thinkerPromptSectionTuples: Array<[string, string]> = [
      ['context_prefix', thinkerParams.contextPrefix],
      ['packer_meta', thinkerParams.packerMetaSection],
      ['strategic_context', thinkerParams.strategicContext],
      ['problem_model', thinkerParams.problemModelSection],
      ['cycle_start_reconciliation', thinkerParams.cycleStartReconciliationSection || ''],
      ['reflection', thinkerParams.reflectionSection],
      ['self_improvement', thinkerParams.selfImprovementBlock],
      ['running_update', thinkerParams.runningUpdateSection],
      ['running_update_superseded', thinkerParams.runningUpdateSupersededSection || ''],
      ['design_doc', thinkerParams.designDocJson],
      ['vision', thinkerParams.visionJson],
      ['outcome', thinkerParams.outcomeSection],
      ['decision_outcomes', thinkerParams.previousDecisionOutcomes],
      ['retirement_critiques', thinkerParams.pastRetirementCritiquesSection],
      ['retired_directions', thinkerParams.retiredDirectionsSummarySection],
      ['direction_portfolio', thinkerParams.directionPortfolioSection],
      ['staged_directions', thinkerParams.stagedDirectionsSection || ''],
      ['user_paused', thinkerParams.userPausedSection],
      ['rejected_proposals', thinkerParams.rejectedProposalsSection],
      ['operator_overrides', thinkerParams.operatorOverridesSection],
      ['available_capabilities', thinkerParams.availableCapabilitiesSection || ''],
      ['model_health', thinkerParams.modelHealthSection],
      ['prune_candidates', thinkerParams.pruneCandidatesSection || ''],
      ['system_maturity', thinkerParams.systemMaturitySection],
      ['drift', thinkerParams.driftSection],
      ['held_directions', thinkerParams.heldDirectionsSection || ''],
      ['recent_work', thinkerParams.recentWorkSection],
      ['feedback', thinkerParams.feedbackSection],
      ['question', thinkerParams.questionSection],
      ['thread', thinkerParams.threadSection],
      ['version_phase', thinkerParams.versionPhaseSection],
      ['memo_citations', thinkerParams.memoCitationsSection],
      ['efficiency_snapshot', thinkerParams.efficiencySnapshotSection],
      ['ci_compliance', thinkerParams.ciComplianceSection || ''],
      ['completion_debt', thinkerParams.completionDebtSection || ''],
      ['pending_followups', thinkerParams.pendingFollowupsSection || ''],
      ['sweep_comparison', thinkerParams.sweepComparisonSection || ''],
      ['local_runner', thinkerParams.localRunnerSection || ''],
      ['compaction_hints', thinkerParams.compactionHintsSection || ''],
      ['payload_staleness', thinkerParams.payloadStalenessSection || ''],
    ];
    const thinkerTelemetry = buildPromptSizeTelemetry(thinkerPromptWithSuffix, thinkerPromptSectionTuples);
    setPromptSizeTelemetry(
      thinkerTelemetry.prompt_size_bytes,
      thinkerTelemetry.prompt_section_count,
      thinkerTelemetry.prompt_section_sizes,
    );

    const thinkerGateResult = await runThinkerWithGate(
      thinkerPromptWithSuffix, taskId, ws, statusRef, model, effort, disallowedTools,
      currentPurpose,
    );
    if (!thinkerGateResult.ok) return;
    const analysisText = thinkerGateResult.analysisText;

    // If the thinker emitted a version goal (Section 7 file), pre-merge it into
    // the problem_model JSON now so the executor's set-problem-model call carries
    // it without needing to search prose. Non-fatal: if the merge fails
    // (malformed JSON, write error) the executor can still proceed; the goal
    // stays unset this cycle and the thinker can retry next cycle.
    const { newVersionGoal } = thinkerGateResult;
    if (newVersionGoal) {
      const pmPath = `/tmp/problem_model_${taskId}.json`;
      try {
        const pmRaw = fs.readFileSync(pmPath, 'utf8');
        const pm = JSON.parse(pmRaw) as Record<string, unknown>;
        pm.new_version_goal = newVersionGoal;
        fs.writeFileSync(pmPath, `${JSON.stringify(pm, null, 2)}\n`);
        sendLog(ws, `[INFO] Pre-merged new_version_goal into problem_model JSON\n`);
      } catch (e) {
        sendLog(ws, `[WARNING] Failed to pre-merge version goal into problem_model JSON (executor will handle it): ${String(e)}\n`);
      }
    }

    let proposedCriteriaBlock = '';
    const proposedCriteriaPath = `/tmp/proposed_criteria_${taskId}.json`;
    if (fs.existsSync(proposedCriteriaPath)) {
      try {
        const raw = fs.readFileSync(proposedCriteriaPath, 'utf8').trim();
        if (raw) proposedCriteriaBlock = `# Proposed acceptance criteria\n\`\`\`json\n${raw}\n\`\`\`\n`;
      } catch (e) {
        sendLog(ws, `[WARNING] Failed to read proposed_criteria file: ${String(e)}\n`);
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

Use \`./strategic pending-confirm\` for those — it's the structured channel for blocking asks. Use \`./messages send\` for general conversation or observations, and \`./messages send --stdin\` for quote-heavy or multiline replies. NEVER \`./messages send\` to ask "should I do X?" for coding decisions — those you make.

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
#   OMIT conflicts_with (or pass []) when no real overlap exists — directions without
#   conflicts run concurrently, which is the default and preferred dispatch mode.
#   "Overlap" means conflicting file rewrites, incompatible architecture shifts, or a
#   shared resource that cannot be used concurrently — NOT "both are big" or "both touch
#   the backend." When in doubt, omit: extra conflicts serialize work unnecessarily.
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
./strategic approve-direction '{"direction_id": "d-xxx", "rationale": "..."}'
./strategic reject-direction '{"direction_id": "d-xxx", "rationale": "..."}'
# Reactivate a staged or user-paused direction back to active.
./strategic activate-direction '{"direction_id": "d-xxx", "rationale": "why reactivating now"}'

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

# CONDITIONAL: Submit acceptance criteria proposed by the Thinker, only when the
# /tmp/proposed_criteria_${taskId}.json file exists and is non-empty.
./strategic set-acceptance-criteria "$(cat /tmp/proposed_criteria_${taskId}.json)"

# Spawn a targeted investigation/knowledge task when something from the Recent
# Work section needs attention NOW instead of waiting for the next autonomous
# cycle. Choose the type by what you need:
#   research          — codebase investigation that yields implementation findings
#   plan              — decompose a large change into implementation subtasks
#   web_research      — external / web knowledge (best practices, unfamiliar
#                       protocols/APIs/vendors); produces a citation-grounded doc
#   design            — codebase-grounded design exploration for a subsystem before
#                       implementation; produces a design document
#   compute_experiment — run a command and measure a declared metric to resolve a
#                       falsifiable hypothesis; requires command, spend_cap_usd,
#                       deadline_seconds; optional success_criterion {metric,
#                       operator (<|>|<=|>=), threshold}, accelerator (T4|L4|V100
#                       — requests ONE GPU, no machine_type needed), container_image
#                       (run command INSIDE a docker image — if a project-baked image
#                       is listed in the Self-hosted compute section above, OMIT this
#                       field and the platform defaults to that digest-pinned image;
#                       only set it when you need a DIFFERENT NGC/CUDA stack the baked
#                       image does not cover; for projects with no baked image it is
#                       REQUIRED for CUDA/NGC stacks the repo cannot pip-install
#                       natively, else the command runs BARE and dies
#                       ModuleNotFoundError), container_registry_auth
#                       {server,username,password} (private/gated registries only),
#                       setup_step (host-level prep run as bare bash -lc OUTSIDE the
#                       container; container-dependent installs go in command, NOT
#                       here). The command MUST write the declared metric as JSON to
#                       $BLUEPRINT_RESULT_FILE (or print it as the last stdout line)
#                       — a run that exits 0 without it loses the metric and the
#                       criterion fails unmeasured (compute_unmeasured_criterion).
#                       Reliability conventions (experiments die from env drift and
#                       lost metrics, not wrong hypotheses): PIN dependency versions
#                       in the command (e.g. jax==0.6.2 — an unpinned brax install
#                       once pulled an incompatible jax and killed the run); write
#                       the metric EARLY (provisional value at first measurement)
#                       and overwrite with the final value so a timeout still
#                       measures; structure long training as short gated stages
#                       (bounded first stage gates a longer follow-on) instead of
#                       one deadline-sized run; keep the artifact dir small (~25 MiB
#                       upload cap); prefer a repo harness/experiment entrypoint
#                       when one exists over inlining a bespoke pipeline.
# Durable outputs (models, checkpoints, trained weights, generated datasets,
# or any result the request says to retain): author the compute_experiment with
# require_durable_artifact: true AND have its ordinary command copy the selected
# outputs into '$BLUEPRINT_ARTIFACT_DIR' before exiting. Put a small manifest.json
# there naming the retained files, their purpose, and the load/reproduction
# command. Training-like work should normally do this; write checkpoints directly
# there or copy the final selected checkpoint there. The platform already collects
# this directory, and require_durable_artifact makes a missing/empty delivery a
# loud failed result. Do not invent a second training or upload path; keep only
# the useful, bounded files (artifact upload cap is about 25 MiB).
# Project compute-image environment (only when a workload actually needs a
# container-only runtime such as CUDA/framework libraries): run
# './compute-image status' before authoring the launch. It returns one of four
# actionable states:
#   - ready: a digest-pinned project image is available; OMIT container_image so
#     the platform selects it automatically.
#   - missing_but_dockerfile_candidate: spawn a one-time create/refactor task to
#     add the returned 'manifest_template' to '.blueprint/tools.json'; do not
#     improvise a Docker build in a compute command.
#   - declared_needs_build: the recipe is already in the repo; run
#     './compute-image build' ONCE. It returns accepted/reused promptly; do NOT
#     wait or foreground-poll it. Re-check status in a later resident turn
#     before launching. 'build_status: requested' means the request was saved,
#     not that a worker is definitely still alive. A 'last_build_outcome' is
#     shown only when it belongs to the current recipe inputs; resolve its
#     deferred/error cause before one later request if needed, never retry in a
#     loop.
#   - no_candidate: no credible repository Dockerfile was found. Bare compute is
#     still valid; do NOT block a normal command or manufacture an image solely
#     to satisfy this resolver.
# web_research and design are HIGH-ROI and chronically under-used: one durable
# knowledge document de-risks every later task in the area and compounds across
# cycles. Spawn them PROACTIVELY, not only reactively — signals that warrant one:
#   - a direction hinges on external facts (APIs, vendors, pricing/quotas,
#     standards, how mature OSS solves the same problem) nobody has documented
#   - an area keeps producing failed/churning implementation tasks (structure
#     should be settled by a design doc instead of re-derived per task)
#   - the "Existing knowledge documents" index is empty or thin for a direction
#     you are actively advancing
# (Implementation/PR types — create/fix/refactor/migrate/qa — are intentionally
# NOT spawnable here; they flow through the normal direction → research → plan →
# create path. compute_experiment is the exception: it is empirical measurement,
# not implementation, so it IS spawnable directly from the strategist.)
./strategic spawn-task '{"type": "research", "title": "...", "prompt": "Full investigation prompt...", "direction_id": "d-xxx"}'
./strategic spawn-task '{"type": "web_research", "title": "...", "prompt": "What external knowledge to gather...", "direction_id": "d-xxx"}'
./strategic spawn-task '{"type": "design", "title": "...", "prompt": "Design question grounded in the codebase...", "direction_id": "d-xxx"}'
./strategic spawn-task '{"type": "compute_experiment", "title": "...", "hypothesis": "<falsifiable claim with a concrete threshold>", "command": "<command that writes the declared metric as JSON to $BLUEPRINT_RESULT_FILE>", "spend_cap_usd": 2.50, "deadline_seconds": 1800, "success_criterion": {"metric": "<metric_key>", "operator": ">=", "threshold": 0}, "direction_id": "d-xxx"}'
# GPU / containerized variant — accelerator requests one GPU; container_image runs
# the command INSIDE a CUDA/NGC image. If a project-baked compute image is listed in
# the "Self-hosted compute" section above, OMIT container_image — the platform
# defaults to that digest-pinned image automatically. Only set container_image when
# you need a DIFFERENT stack the baked image does not cover, or for projects with no
# baked image where the runtime is not pip-installable (else the command runs bare
# and dies ModuleNotFoundError). Add container_registry_auth only for a private
# registry; setup_step is host-level prep run OUTSIDE the container.
./strategic spawn-task '{"type": "compute_experiment", "title": "...", "hypothesis": "<falsifiable GPU claim with a concrete threshold>", "command": "<command — installs+run go HERE, inside the image; must write the metric JSON to $BLUEPRINT_RESULT_FILE>", "accelerator": "T4", "container_image": "nvcr.io/nvidia/tensorflow:25.01", "spend_cap_usd": 5.00, "deadline_seconds": 3600, "success_criterion": {"metric": "<metric_key>", "operator": ">=", "threshold": 0}, "direction_id": "d-xxx"}'
# Multi-arm variant: use "arms" + "declared_metric" instead of a single "command".
# Each arm writes its declared_metric key to $BLUEPRINT_RESULT_FILE as usual; the
# platform isolates each arm in its own result path and assembles the comparison
# block — arm commands need not self-assemble it.  One arm failing does not suppress
# the others.  "declared_metric" (required with arms) is the exact metric key each
# arm writes to $BLUEPRINT_RESULT_FILE.
./strategic spawn-task '{"type": "compute_experiment", "title": "...", "hypothesis": "<arm A outperforms arm B on the declared metric>", "arms": [{"name": "a", "command": "<command for arm a>"}, {"name": "b", "command": "<command for arm b>"}], "declared_metric": "<metric_key>", "spend_cap_usd": 5.00, "deadline_seconds": 3600, "direction_id": "d-xxx"}'
# Optional fields on any spawn-task call:
#   criterion_ids — list of criterion keys (from acceptance criteria) to link this
#     task's merge evidence to coverage. When direction_id is also set, these are
#     MERGED with the direction's criterion_keys (both filtered to active keys).
#     Use this for direction-free proving spawns that should count toward coverage.
#   acceptance_evidence — declare HOW the spawned task proves its claim; the named_test
#     kind is the strongest signal: when the declared test runs green, the merge is
#     credited as proven and contributes to acceptance_declaration_coverage.
#     Without this field, a proven merge still records evidence but is not pinned to
#     a specific test, so the coverage credit is weaker.
# Direction-free proving spawn that contributes to criterion coverage:
./strategic spawn-task '{"type": "research", "title": "...", "prompt": "...", "criterion_ids": ["c-key-1", "c-key-2"], "acceptance_evidence": {"kind": "named_test", "test_path": "testing/test_foo.py", "test_id": "test_bar"}}'
# Direction spawn with explicit extra criterion coverage (merged with direction's own keys):
./strategic spawn-task '{"type": "research", "title": "...", "prompt": "...", "direction_id": "d-xxx", "criterion_ids": ["c-key-extra"], "acceptance_evidence": {"kind": "named_test", "test_path": "testing/test_foo.py"}}'

# One-time fix: spawn a PR-producing task with NO direction (create/refactor/qa).
# Fires once and lands a PR; no direction lifecycle.
./strategic spawn-fix '{"type": "create|refactor|qa", "title": "...", "prompt": "Full implementation prompt..."}'

# Dispose an open completion-debt ledger entry.
# disposition: "resolved" (requires evidence) or "dismissed" (requires reason).
./strategic dispose-completion-debt '{"debt_id": "...", "disposition": "resolved|dismissed", "reason": "evidence or reason"}'

# Pull full detail for items the strategic packer trimmed (see "Context not
# shipped this cycle" section in your prompt). Use only when you actually
# need the detail — extra context hurts reasoning quality.
${renderStrategicFetchVerbHelpLines().join('\n')}

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
./messages send --stdin <<'EOF'
Observation or note to the user
EOF
\`\`\`

# Existing directions (for update/retire operations)
${dirSummaryText}

${questionSection}${feedbackSection}${openMessagesText ? `# Open Messages (resolve addressed ones, respond to questions)\nA message tagged \`⚠ STALE\` is a BUG_REPORT/BLOCKER citing code that no longer exists on HEAD — treat it as discounted/unproven: do NOT spawn a fix or investigation to chase it. Resolve/close it as already-fixed, or re-verify the defect still reproduces on HEAD before acting.\n${openMessagesText}\n` : openMessageCount > 0 ? `# Open Messages (inbox not shipped — ${openMessageCount} message${openMessageCount === 1 ? '' : 's'} available)\nThe open-message inbox was dropped under byte-budget pressure this cycle. Run \`./fetch message\` to retrieve the ${openMessageCount} unresolved message${openMessageCount === 1 ? '' : 's'} before responding to or resolving any operator requests.\n` : ''}
# Strategic analysis to execute

${analysisText.slice(0, 50_000)}

${strategicChallengeSection}

${proposedCriteriaBlock}
# Instructions

1. **Problem model (REQUIRED).** First do step 5 below: if the Thinker emitted \`version_phase_decision\`, \`qualify_request\`, and/or \`new_version_goal\`, edit \`/tmp/problem_model_${taskId}.json\` IN PLACE to add those top-level sibling fields. Then pass \`"$(cat /tmp/problem_model_${taskId}.json)"\` to \`./strategic set-problem-model\` — the \`cat\` re-reads the file, so the sibling fields you added in step 5 are included. Don't summarize or re-emit the model's own content — read-and-pass; the only edit permitted is step 5's sibling fields.

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

4.5. **Acceptance criteria (conditional).** If the proposed criteria block above is non-empty, call \`./strategic set-acceptance-criteria "$(cat /tmp/proposed_criteria_${taskId}.json)"\`. If the file is absent or empty, SKIP — do not fabricate a criteria list.

   The payload may contain both a \`criteria\` additions array and an optional \`retract\` array. A \`retract\` item retracts or supersedes an existing **active** criterion that has become permanently uncoverable: \`{"criterion_key":"c-xxx","reason":"...","superseded_by_key":"c-yyy"}\` (\`superseded_by_key\` is optional; omit when retraction stands alone). Retractions are processed before additions. **Never fabricate retractions** — only retract when the Thinker's analysis shows the criterion is demonstrably uncoverable given the current direction portfolio.

5. **Version phase / qualify siblings (conditional — do this BEFORE step 1's \`set-problem-model\` call).** If the Thinker's analysis includes a \`version_phase_decision\` field (look in Section 6 and throughout the analysis), edit \`/tmp/problem_model_${taskId}.json\` IN PLACE to add it as a top-level sibling key, so the file becomes:
   \`{"version_phase_decision": {"phase": "build" or "stabilize", "reasoning": "..."}, ...rest_of_problem_model}\`
   If the Thinker did NOT emit \`version_phase_decision\`, SKIP — do not fabricate one, and do not add the field. Absence means "no phase change this cycle."

   Likewise, if the Thinker's analysis includes a \`qualify_request\` field (look in Section 6 and throughout the analysis), edit \`/tmp/problem_model_${taskId}.json\` IN PLACE to add it as a top-level sibling key too:
   \`{"qualify_request": {"reason": "..."}, ...rest_of_problem_model}\`
   If the Thinker did NOT emit \`qualify_request\`, SKIP — do not fabricate one, and do not add the field. Absence means "do not qualify this cycle."

   Likewise, if \`new_version_goal\` is already present as a top-level key in \`/tmp/problem_model_${taskId}.json\` (pre-merged from the Thinker's Section 7 output file before this executor pass), it is already set — the \`cat\` in step 1 will carry it through automatically. Do NOT overwrite or re-add it. If it is absent from the file, the Thinker did not set a version goal this cycle; do NOT fabricate one.

   You may also emit \`new_acceptance_criteria\` as a top-level sibling array to author acceptance criteria for the open version:
   \`{"new_acceptance_criteria": ["All unit tests pass on automain", "No P0 bugs in open state"], ...rest_of_problem_model}\`
   Each string becomes a new VersionAcceptanceCriterion (duplicates are skipped). Omit the key entirely when no new criteria are needed — absence is the default and means "no new criteria this cycle."

   \`version_phase_decision\`, \`qualify_request\`, \`new_version_goal\`, and \`new_acceptance_criteria\` are independent siblings; add whichever the Thinker emitted (any, all, or none).

   Because step 1 passes \`"$(cat /tmp/problem_model_${taskId}.json)"\` to \`./strategic set-problem-model\`, editing the file here is what carries these siblings through — step 1 will re-read the file and the fields will be included. If the file is missing/malformed and you took step 1's fallback (constructing the model inline), include these siblings in that inline JSON instead.

6. **Self-reflection (optional).** If Section 5 has non-empty content, emit via \`./strategic reflect\`. If the Thinker wrote nothing (or wrote "none" / "N/A" / empty), SKIP — do not fabricate.

7. **Pending confirmations (optional).** For each external blocker the Thinker identified in Section 4's blockers field (credentials, access, major pivots), emit one \`./strategic pending-confirm\`. Do NOT create these for coding questions.

8. **Messages.** Resolve any user messages the analysis addressed: \`./messages resolve <id> 'Answered — [summary]'\`. Check the conversation for your prior QUESTION messages — if a user reply answers one, resolve it. Do not post a cycle-summary message — the running_update is the voice now.

9. **Guardrails.** Ensure at least 3 active directions remain after retires.

10. Done. Do not write anything else.`;

    // ── Git token refresh before executor ───────────────────────────────────
    // Refresh the token file before the executor runs so any git operations
    // (branch creation, pushes) authenticate with a fresh credential rather
    // than the potentially-expired role-start token.
    try {
      const refreshResult = await apiGet(`/api/agent/github/token?task_id=${encodeURIComponent(taskId)}&force_refresh=true`) as { data?: { token?: string } } | null;
      const newToken = refreshResult?.data?.token;
      if (newToken) {
        writeGitTokenFile(newToken);
        sendLog(ws, '[strategic] git token refreshed before executor\n');
      }
    } catch (e) {
      sendLog(ws, `[strategic] git token refresh before executor failed (keeping existing token): ${String(e)}\n`);
    }

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
      const acceptanceCriteriaSetInOutput =
        /"op"\s*:\s*"set_acceptance_criteria"/.test(allOutput)
        || /\.\/strategic\s+set-acceptance-criteria\b/.test(allOutput)
        || /Acceptance criteria recorded/i.test(allOutput);
      const proposedCriteriaRequired = proposedCriteriaBlock.length > 0;

      if (exitCode === 0 && (!problemModelSetInOutput || !runningUpdateSetInOutput || !retirementCritiquesSetInOutput || (proposedCriteriaRequired && !acceptanceCriteriaSetInOutput))) {
        const missing: string[] = [];
        if (!problemModelSetInOutput) missing.push('./strategic set-problem-model');
        if (!runningUpdateSetInOutput) missing.push('./strategic set-running-update');
        if (!retirementCritiquesSetInOutput) missing.push('./strategic set-retirement-critiques');
        if (proposedCriteriaRequired && !acceptanceCriteriaSetInOutput) missing.push('./strategic set-acceptance-criteria');
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
          proposedCriteriaRequired && !acceptanceCriteriaSetInOutput ? `- ./strategic set-acceptance-criteria "$(cat /tmp/proposed_criteria_${taskId}.json)"` : '',
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
      const directionUpdatedInOutput = /"status"\s*:\s*"ok"[\s\S]{0,100}"op"\s*:\s*"(update_direction|retire_direction|stage_direction|approve_direction|reject_direction)"/.test(allOutput)
        || /"op"\s*:\s*"(update_direction|retire_direction|stage_direction|approve_direction|reject_direction)"[\s\S]{0,100}"status"\s*:\s*"ok"/.test(allOutput);
      const anyDirectionOp = directionCreatedInOutput || directionUpdatedInOutput;

      // Detect constraint rejections (cap/dedup) so we don't retry wastefully
      const directionRejectedInOutput = /\[CONSTRAINT\].*(create_direction|approve_direction|reject_direction)/.test(allOutput) || /"status"\s*:\s*"skipped"/.test(allOutput);

      // Check if thinker's structured output contained direction recommendations
      const structuredSection = analysisText.match(/## Structured Output[\s\S]*$/i)?.[0] ?? '';
      const thinkerRecommendedCreates = /create[_-]direction|"op"\s*:\s*"create"/i.test(structuredSection);
      const thinkerRecommendedOtherOps = /retire[_-]direction|update[_-]direction|stage[_-]direction|approve[_-]direction|reject[_-]direction/i.test(structuredSection);

      if (directionRejectedInOutput && !anyDirectionOp) {
        sendLog(ws, '\n[INFO] Direction creation was attempted but rejected by constraints (cap/duplicate). Not retrying.\n');
      } else if (exitCode === 0 && thinkerRecommendedCreates && !anyDirectionOp) {
        sendLog(ws, '\n[WARNING] Strategic executor completed but no direction operations were executed. Retrying...\n');
        const retryResult = await runClaudeCode(
          'Your previous attempt did not execute any ./strategic create-direction commands.\n\n' +
          'The strategic analysis recommended creating new directions. Run the create-direction commands NOW:\n\n' +
          'For each new direction in the analysis, run:\n' +
          './strategic create-direction \'{"text": "...", "priority": "high|medium|low", "category": "product|structural", "gap_id": "g-xxx", "rationale": "...", "success_evidence": "observable outcome that proves this direction is done (required)", "retirement_condition": "measurable condition that triggers retirement (required)", "dedup_reasoning": {"closest_directions": ["d-xxx"], "distinction": "..."}}\' \n\n' +
          'success_evidence and retirement_condition are required fields — the backend will reject the call with HTTP 400 if either is missing.\n\n' +
          'Extract the directions from the analysis and call the commands. Nothing else.',
          ws, statusRef, model, effort, disallowedTools
        );
        exitCode = retryResult.exitCode;
        allOutput = allOutput + '\n' + retryResult.allOutput;
        lastResultText = retryResult.lastResultText || lastResultText;
      } else if (exitCode === 0 && thinkerRecommendedOtherOps && !anyDirectionOp) {
        sendLog(ws, '\n[WARNING] Strategic executor completed but recommended direction operations were not executed. Retrying...\n');
        const retryResult = await runClaudeCode(
          'Your previous attempt did not execute the recommended direction operation commands.\n\n' +
          'The strategic analysis recommended one or more of the following operations. Run the relevant commands NOW:\n\n' +
          './strategic retire-direction \'{"direction_id": "d-xxx", "rationale": "..."}\'\n' +
          './strategic update-direction \'{"direction_id": "d-xxx", "changes": {"priority": "...", "text": "...", "success_evidence": "...", "retirement_condition": "..."}, "rationale": "..."}\'\n' +
          './strategic stage-direction \'{"direction_id": "d-xxx", "rationale": "why this belongs to the next version"}\'\n' +
          './strategic approve-direction \'{"direction_id": "d-xxx", "rationale": "..."}\'\n' +
          './strategic reject-direction \'{"direction_id": "d-xxx", "rationale": "..."}\'\n\n' +
          'Extract the direction operation from the analysis and call the appropriate command. Nothing else.',
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
      logs = buildStrategicCompletionLogs(allOutput, lastResultText).logs;
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
