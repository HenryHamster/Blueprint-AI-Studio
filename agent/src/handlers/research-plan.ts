import fs from 'fs';
import { execFileSync } from 'child_process';
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
  extractJsonObjectDetailed,
} from '../json-extract.js';
import {
  PLAYBOOK_GUIDANCE, FIRST_TASK_GUIDANCE, DEFAULT_TOOL_GUIDANCE, READ_ONLY_BASH_WRITE_NOTE, readClaudeMd, buildContextPrefix, buildFileTree, isGreenfieldRepo, verifyCloneDelivered, configureAgentPermissions,
  writePlaybookHelper, writeMessagesHelper, writeDesignDocHelper, formatPlaybook, buildRetryContext, type PlaybookEntry,
} from '../prompt.js';
import { parseBackendTimestamp } from '../utils/time.js';
import { reconcileSystemEntry, reconcileMissingCapabilities } from '../utils/design-doc-presence.js';
import {
  differentiationHintFor,
  formatCochangeHints,
  formatDecompositionOutcomes,
} from './research-direction-context.js';
import { renderDirection } from './direction-renderer.js';
import {
  writeFetchHelper, buildFetchGuidance, RESEARCH_FETCH_VERBS, PLAN_FETCH_VERBS,
} from './strategic-helpers.js';
import { fetchPriorKnowledge, buildPriorKnowledgeSection } from './prior-knowledge.js';
import { repairStructuredOutput } from './structured-output-repair.js';

export const PR_INELIGIBLE_TASK_TYPES = [
  'research', 'investigate', 'audit', 'memo',
  'web_research', 'design', 'compute_experiment',
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

export function buildStrategicCompletionLogs(
  allOutput: string,
  lastResultText: string | null | undefined,
): { logs: string; warning?: string; missReason?: import('../json-extract.js').JsonExtractionMissReason } {
  const fallbackLogs = sanitize(lastResultText || allOutput.slice(-50_000));
  const hasDirectionOperations = allOutput.includes('"direction_operations"');
  const hasProblemModel = allOutput.includes('"problem_model"');
  const hasVision = allOutput.includes('"vision"');

  // Current strategic cycles apply state through ./strategic commands. A final
  // inline strategic JSON object is a legacy artifact, so its absence should
  // not warn after a command-based run succeeds.
  if (!hasDirectionOperations || (!hasProblemModel && !hasVision)) {
    return { logs: fallbackLogs };
  }

  const requiredKeys = hasProblemModel
    ? ['problem_model', 'direction_operations']
    : ['vision', 'direction_operations'];

  try {
    const stratObj = extractJsonObjectDetailed(allOutput, ...requiredKeys);
    if (stratObj.ok) {
      return { logs: sanitize(JSON.stringify(stratObj.value)) };
    }
    return {
      logs: fallbackLogs,
      warning: `Could not extract strategic JSON: ${describeJsonExtractionResult(stratObj)}`,
      missReason: stratObj.reason,
    };
  } catch (err: unknown) {
    return {
      logs: fallbackLogs,
      warning: `Could not extract strategic JSON: ${err instanceof Error ? err.message : String(err)}`,
    };
  }
}

/** Maps raw agent-reported failure_category to a high-level failure class. */
export function classifyFailureCategory(rawCat: string): string {
  const CODE_ERROR_CATS: Record<string, true | undefined> = {
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

  // Prior-cycles failure recap: surface 2-3 code-error titles from cycles before
  // the last one so agents can avoid re-proposing the same failed findings.
  let priorFailuresLine = '';
  if (history.length >= 2) {
    const priorEntries = history.slice(-3, -1);
    const priorItems: Array<{title: string; reason: string; cls: string; cycle: number}> = [];
    priorEntries.forEach((h, idx) => {
      const details = Array.isArray(h.failed_task_details)
        ? h.failed_task_details as Array<{title: string; reason: string; category: string}>
        : [];
      for (const fd of details) {
        const cls = classifyFailureCategory(fd.category);
        if (cls === 'CODE_ERROR') {
          priorItems.push({ title: fd.title || '?', reason: fd.reason || '', cls, cycle: priorEntries.length - idx });
        }
      }
    });
    if (priorItems.length > 0) {
      const snippets = priorItems.slice(0, 3).map(it => {
        const t = it.title.slice(0, 60);
        const r = it.reason ? `: ${it.reason.slice(0, 50)}` : '';
        return `"${t}"${r} (${it.cycle} cycle${it.cycle === 1 ? '' : 's'} ago)`;
      });
      priorFailuresLine = `\n  Prior CODE_ERRORs to avoid repeating: ${snippets.join('; ')}`;
    }
  }

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

  // Scope success from last 3 cycles: aggregate scope_breakdown to show merged/created per scope,
  // and surface distinguishing characteristics (complexity, expansion ratio) + calibration flow
  const recent = history.slice(-3);
  type ScopeAgg = {
    created: number; merged: number;
    cplxMSum: number; cplxMN: number;
    cplxFSum: number; cplxFN: number;
    expSum: number; expN: number;
    calIn: number; calOut: number;
  };
  const aggScope: Record<string, ScopeAgg | undefined> = {};
  for (const h of recent) {
    const sb = h.scope_breakdown as Record<string, Record<string, number> | undefined> | undefined;
    if (!sb || typeof sb !== 'object') continue;
    for (const [scope, counts] of Object.entries(sb)) {
      if (!counts) continue;
      const a = aggScope[scope] ?? (aggScope[scope] = {
        created: 0, merged: 0, cplxMSum: 0, cplxMN: 0, cplxFSum: 0, cplxFN: 0, expSum: 0, expN: 0, calIn: 0, calOut: 0,
      });
      a.created += (counts.created) || 0;
      a.merged += (counts.merged) || 0;
      if (typeof counts.avg_complexity_merged === 'number') { a.cplxMSum += counts.avg_complexity_merged; a.cplxMN += 1; }
      if (typeof counts.avg_complexity_failed === 'number') { a.cplxFSum += counts.avg_complexity_failed; a.cplxFN += 1; }
      if (typeof counts.avg_expansion_ratio === 'number') { a.expSum += counts.avg_expansion_ratio; a.expN += 1; }
      a.calIn += counts.calibrated_in || 0;
      a.calOut += counts.calibrated_out || 0;
    }
  }
  const scopeOrder = ['small', 'medium', 'large'];
  const orderedScopes = [...scopeOrder, ...Object.keys(aggScope).filter(s => !scopeOrder.includes(s))];
  const scopeParts: string[] = [];
  const profileParts: string[] = [];
  const calParts: string[] = [];
  for (const scope of orderedScopes) {
    const a = aggScope[scope];
    if (!a) continue;
    if (a.created > 0) scopeParts.push(`${scope} ${a.merged}/${a.created}`);
    const mBits: string[] = [];
    if (a.cplxMN > 0) mBits.push(`cplx ${(a.cplxMSum / a.cplxMN).toFixed(2)}`);
    if (a.expN > 0) mBits.push(`expand ${(a.expSum / a.expN).toFixed(1)}x`);
    const fBits = a.cplxFN > 0 ? [`cplx ${(a.cplxFSum / a.cplxFN).toFixed(2)}`] : [];
    const sides: string[] = [];
    if (mBits.length) sides.push(`merged ${mBits.join('/')}`);
    if (fBits.length) sides.push(`failed ${fBits.join('/')}`);
    if (sides.length) profileParts.push(`${scope}(${sides.join(' vs ')})`);
    if (a.calIn > 0 || a.calOut > 0) {
      const c: string[] = [];
      if (a.calIn > 0) c.push(`in=${a.calIn}`);
      if (a.calOut > 0) c.push(`out=${a.calOut}`);
      calParts.push(`${scope} ${c.join(' ')}`);
    }
  }
  const cycleLabel = recent.length === 1 ? '1 cycle' : `${recent.length} cycles`;
  const line3 = scopeParts.length > 0
    ? `\n  Scope success (last ${cycleLabel}): ${scopeParts.join(', ')}`
    : '';
  const line4 = profileParts.length > 0
    ? `\n  Per-scope profile: ${profileParts.join('; ')}`
    : '';
  const line5 = calParts.length > 0
    ? `\n  Calibration (in=narrowed from larger, out=auto-escalated up): ${calParts.join(', ')}`
    : '';

  // Scope accuracy: per-scope avg_expansion_ratio (actual files / declared related_files).
  // Flag overflow (>2.0x) per scope and emit a summary warning so research agents
  // learn when their file-count estimates chronically underestimate reality.
  const expansionParts: string[] = [];
  let maxRatio = 0;
  for (const scope of orderedScopes) {
    const a = aggScope[scope];
    if (!a || a.expN === 0) continue;
    const avg = a.expSum / a.expN;
    if (avg > maxRatio) maxRatio = avg;
    const flag = avg > 2.0 ? ' ⚠ scope overflow' : '';
    expansionParts.push(`${scope} ${avg.toFixed(1)}x${flag}`);
  }
  const line6 = expansionParts.length > 0
    ? `\n  Scope accuracy (actual files / declared, last ${cycleLabel}): ${expansionParts.join(', ')}`
    : '';
  const line7 = maxRatio > 2.0
    ? `\n  ⚠ Expansion ratio exceeds 2.0x — halve your file count estimates or prefer smaller scope`
    : '';

  if (!line1 && !line2 && !line3 && !line4 && !line5 && !line6 && !line7 && !priorFailuresLine) return '';
  return line1 + line2 + line3 + line4 + line5 + line6 + line7 + priorFailuresLine;
}

/**
 * Renders a compact "Recently filtered (avoid)" section from the last few
 * research_history entries. Flattens entries' filtered_findings lists
 * (most recent cycle last), dedups by title, and caps at 5 items so the
 * prompt does not bloat. Returns an empty string when no entries carry
 * filtered_findings so existing prompts are unchanged.
 */
export function buildFilteredFindingsSection(history: Array<Record<string, unknown>>): string {
  if (history.length === 0) return '';
  const recent = history.slice(-3);
  const seen = new Set<string>();
  const items: Array<{title: string; reason: string}> = [];
  for (let i = recent.length - 1; i >= 0; i--) {
    const entry = recent[i];
    const raw = Array.isArray(entry.filtered_findings)
      ? entry.filtered_findings as Array<Record<string, unknown>>
      : [];
    for (const rec of raw) {
      const title = typeof rec.title === 'string' ? rec.title : '';
      const reason = typeof rec.reason === 'string' ? rec.reason : '';
      if (!title) continue;
      const key = title.toLowerCase();
      if (seen.has(key)) continue;
      seen.add(key);
      items.push({ title, reason });
      if (items.length >= 5) break;
    }
    if (items.length >= 5) break;
  }
  if (items.length === 0) return '';
  const lines = items.map(it => {
    const hint = differentiationHintFor(it.reason);
    if (it.reason && hint) return `  * "${it.title}" — ${it.reason} [hint: ${hint}]`;
    if (it.reason) return `  * "${it.title}" — ${it.reason}`;
    return `  * "${it.title}"`;
  });
  return `\n  Recently filtered (avoid — propose different approaches):\n${lines.join('\n')}`;
}

export function buildRejectedProposalsSection(
  rejectedProposals: Array<Record<string, unknown>>
): string {
  if (!Array.isArray(rejectedProposals) || rejectedProposals.length === 0) return '';
  const lines = rejectedProposals.slice(0, 10).map(p => {
    const text = strVal(p.text).slice(0, 300);
    const when = strVal(p.rejected_at).slice(0, 10) || '?';
    const rationale = strVal(p.rationale).slice(0, 200);
    const tail = rationale ? ` — ${rationale}` : '';
    return `- (${when}) ${text}${tail}`;
  });
  return `\n## Recently Rejected Proposals (last 30 days)\nThe user explicitly rejected these direction proposals. Do NOT re-propose similar work — find a different angle or move on.\n${lines.join('\n')}\n`;
}

export function buildOperatorOverridesSection(
  overrides: Array<Record<string, unknown>>
): string {
  if (!Array.isArray(overrides) || overrides.length === 0) return '';
  const lines = overrides.slice(0, 8).map(o => {
    const document = strVal(o.document) || 'document';
    const field = strVal(o.field);
    const when = strVal(o.at).slice(0, 10) || '?';
    const agentValue = strVal(o.agent_value).slice(0, 240);
    const operatorValue = strVal(o.operator_value).slice(0, 240);
    const fieldTail = field ? ` \`${field}\`` : '';
    const head = `- (${when}) ${document}${fieldTail}`;
    if (document === 'running_update') {
      // Running-update overrides carry no scalar operator value — the operator's
      // winning version IS the current running_update body in this payload.
      return `${head}: you proposed "${agentValue}"; the operator kept their own running_update edit (see the Running update above).`;
    }
    return `${head}: you proposed "${agentValue}"; the operator kept "${operatorValue}".`;
  });
  return `\n## Operator Overrides Since Last Cycle\nThe operator edited these documents mid-cycle and their value won the reconciliation — your previous proposal was overridden. Reconcile with the operator's intent: do NOT simply re-propose the overridden value. If you still believe a change is warranted, route it as a new proposal that respects what the operator chose.\n${lines.join('\n')}\n`;
}

export function buildRunningUpdateSupersededSection(
  signal: Record<string, unknown> | null | undefined
): string {
  if (!signal || typeof signal !== 'object') return '';
  const count = Number(signal.count) || 0;
  if (count <= 0) return '';
  const when = strVal(signal.most_recent_at).slice(0, 10);
  const whenTail = when ? ` (most recent: ${when})` : '';
  return `\n_Last cycle the operator preserved their own running_update over your proposed rewrite${whenTail}. The operator is actively editing this document — treat it as operator-authoritative. Refine it; do NOT rewrite it from a blank slate or re-impose your superseded version._\n`;
}

export function buildUserPausedSection(
  directionsSummary: Array<Record<string, unknown>>
): string {
  if (!Array.isArray(directionsSummary) || directionsSummary.length === 0) return '';
  const userPaused = directionsSummary.filter(
    d => strVal(d.status) === 'paused' && d.auto_paused !== true
  );
  if (userPaused.length === 0) return '';
  const lines = userPaused.map(d => {
    const reason = strVal(d.paused_reason);
    const tail = reason ? ` — reason: ${reason}` : '';
    return `- ${strVal(d.text)} (id: ${strVal(d.id)})${tail}`;
  });
  return `\n## User-Paused Directions\nThese directions were paused by the user (not auto-paused by the system). Treat as an explicit user constraint — do NOT unpause, retire, or duplicate them without a strong reason backed by user intent.\n${lines.join('\n')}\n`;
}

const QUALIFICATION_SCORE_KEYS = ['audit', 'test_erosion', 'smoketest'] as const;
const QUALIFICATION_SEVERITY_ORDER: Record<string, number> = {
  blocker: 0,
  critical: 1,
  high: 2,
  medium: 3,
  moderate: 3,
  warning: 4,
  low: 5,
  info: 6,
};

function formatBlockerProvenance(qualification: Record<string, unknown>, blocker: string): string | null {
  // Surface the blocker's provenance when available. For CI blockers, include
  // the resolution state/detail. For audit blockers, include the missing reason.
  if (blocker.startsWith('ci_')) {
    const ciResolution = qualification.ci_resolution;
    if (ciResolution && typeof ciResolution === 'object' && !Array.isArray(ciResolution)) {
      const ciRes = ciResolution as Record<string, unknown>;
      const state = typeof ciRes.state === 'string' ? ciRes.state : null;
      const detail = typeof ciRes.detail === 'string' ? ciRes.detail : null;
      if (state || detail) {
        const parts: string[] = [];
        if (state) parts.push(`state: ${state}`);
        if (detail) parts.push(detail);
        return parts.join(', ');
      }
    }
  } else if (blocker === 'audit_missing') {
    const reason = typeof qualification.audit_missing_reason === 'string'
      ? qualification.audit_missing_reason
      : null;
    if (reason) return `reason: ${reason}`;
  }
  return null;
}

function formatQualificationDetails(qualification: Record<string, unknown>): string[] {
  const lines: string[] = [];

  // When the smoketest subsystem was disabled for the qualifying run it spawned
  // no smoketest child, so its sub-score is an unconditional 0.0 placeholder —
  // NOT a real low score. Mirror the backend (`smoketest_enabled` defaults True
  // for legacy records that predate the flag) and exclude the dimension from
  // both the rendered sub-scores and the binding determination when disabled.
  const smoketestEnabled = qualification.smoketest_enabled !== false;
  // A hard blocker (CI states, deleted test, missing audit, smoketest startup
  // failure) forces the composite to zero — the named blocker, not a numeric
  // minimum over placeholder scores, is the true constraint. Some blockers (CI
  // states) map to no sub-score at all.
  const blocker = typeof qualification.blocker === 'string' && qualification.blocker
    ? qualification.blocker
    : null;

  const subScoresRaw = qualification.sub_scores;
  if (subScoresRaw && typeof subScoresRaw === 'object' && !Array.isArray(subScoresRaw)) {
    const subScores = subScoresRaw as Record<string, unknown>;
    const scoreBits: string[] = [];
    let bindingKey: string | null = null;
    let bindingValue: number | null = null;
    for (const key of QUALIFICATION_SCORE_KEYS) {
      if (key === 'smoketest' && !smoketestEnabled) continue;
      const value = typeof subScores[key] === 'number' ? subScores[key] : null;
      if (value === null) continue;
      scoreBits.push(`${key}=${value.toFixed(2)}`);
      if (bindingValue === null || value < bindingValue) {
        bindingKey = key;
        bindingValue = value;
      }
    }
    if (scoreBits.length > 0) {
      let bindingClause: string | null = null;
      if (blocker) {
        // A hard blocker is the constraint regardless of the numeric minimum.
        // Surface the blocker's provenance when available.
        const provenance = formatBlockerProvenance(qualification, blocker);
        bindingClause = provenance ? `binding=${blocker} (${provenance})` : `binding=${blocker}`;
      } else if (bindingKey !== null && bindingValue !== null) {
        bindingClause = `binding=${bindingKey}=${bindingValue.toFixed(2)}`;
      }
      const suffix = bindingClause ? ` (${bindingClause})` : '';
      lines.push(`  Qualification details: sub-scores: ${scoreBits.join(', ')}${suffix}`);
    }
  }

  // Audit-score caveat — the audit sub-score reflects only *verified* findings;
  // higher-severity findings lacking a substantive verification note were
  // excluded from the score (their deductions never applied). Surface how many
  // were excluded, and any material model-reported-vs-findings-derived score
  // divergence the verdict already recorded, so the agent does not read an
  // inflated audit sub-score as a clean audit.
  const reconRaw = qualification.audit_reconciliation;
  if (reconRaw && typeof reconRaw === 'object' && !Array.isArray(reconRaw)) {
    const recon = reconRaw as Record<string, unknown>;
    const excludedCount = typeof recon.excluded_findings_count === 'number'
      ? recon.excluded_findings_count : 0;
    if (excludedCount > 0) {
      const bits: string[] = [
        `audit sub-score reflects only verified findings; ${excludedCount} higher-severity finding${excludedCount === 1 ? '' : 's'} excluded for lacking verification`,
      ];
      const modelScore = typeof recon.model_reported_score === 'number' ? recon.model_reported_score : null;
      const derivedScore = typeof recon.findings_derived_score === 'number' ? recon.findings_derived_score : null;
      const delta = typeof recon.score_delta === 'number' ? recon.score_delta : null;
      if (modelScore !== null && derivedScore !== null && delta !== null && delta >= 0.10) {
        bits.push(`model-reported audit ${modelScore.toFixed(2)} vs findings-derived ${derivedScore.toFixed(2)} (Δ${delta.toFixed(2)})`);
      }
      lines.push(`  Audit score caveat: ${bits.join('; ')}`);
    }
  }

  const findingsRaw = qualification.audit_findings;
  if (Array.isArray(findingsRaw) && findingsRaw.length > 0) {
    const findings = findingsRaw
      .map((finding, idx) => {
        if (!finding || typeof finding !== 'object' || Array.isArray(finding)) return null;
        const rec = finding as Record<string, unknown>;
        const severity = typeof rec.severity === 'string' ? rec.severity.trim() : '';
        const summary = typeof rec.summary === 'string' ? rec.summary.trim() : '';
        if (!severity || !summary) return null;
        return {
          severity,
          summary,
          order: QUALIFICATION_SEVERITY_ORDER[severity.toLowerCase()] ?? 99,
          idx,
        };
      })
      .filter((finding): finding is { severity: string; summary: string; order: number; idx: number } => finding !== null)
      .sort((a, b) => (a.order - b.order) || (a.idx - b.idx))
      .slice(0, 3);

    if (findings.length > 0) {
      const moreCount = findingsRaw.length - findings.length;
      const findingBits = findings.map(f => `${f.severity}: ${f.summary}`);
      const tail = moreCount > 0 ? ` (+${moreCount} more)` : '';
      lines.push(`  Audit findings: ${findingBits.join('; ')}${tail}`);
    }
  }

  // Excluded audit findings — higher-severity findings the verification gate
  // dropped from scoring (and from `audit_findings`). Rendered explicitly as
  // UNVERIFIED so the agent can investigate the omitted risk behind an inflated
  // audit sub-score rather than assume the audit was clean. These are NOT
  // confirmed blockers.
  const excludedRaw = qualification.excluded_audit_findings;
  if (Array.isArray(excludedRaw) && excludedRaw.length > 0) {
    const excluded = excludedRaw
      .map((finding, idx) => {
        if (!finding || typeof finding !== 'object' || Array.isArray(finding)) return null;
        const rec = finding as Record<string, unknown>;
        const severity = typeof rec.severity === 'string' ? rec.severity.trim() : '';
        const summary = typeof rec.summary === 'string' ? rec.summary.trim() : '';
        if (!severity || !summary) return null;
        return {
          severity,
          summary,
          order: QUALIFICATION_SEVERITY_ORDER[severity.toLowerCase()] ?? 99,
          idx,
        };
      })
      .filter((finding): finding is { severity: string; summary: string; order: number; idx: number } => finding !== null)
      .sort((a, b) => (a.order - b.order) || (a.idx - b.idx))
      .slice(0, 3);

    if (excluded.length > 0) {
      const moreCount = excludedRaw.length - excluded.length;
      const excludedBits = excluded.map(f => `${f.severity}: ${f.summary}`);
      const tail = moreCount > 0 ? ` (+${moreCount} more)` : '';
      lines.push(`  Excluded audit findings (UNVERIFIED — not confirmed blockers): ${excludedBits.join('; ')}${tail}`);
    }
  }

  // Test-erosion detail — the specific deleted test files / negative deltas
  // behind a low (or hard-blocking) test-erosion sub-score, so the agent can
  // scope "restore file X" / "recover N assertions" instead of blind cleanup.
  const erosionRaw = qualification.test_erosion_detail;
  if (erosionRaw && typeof erosionRaw === 'object' && !Array.isArray(erosionRaw)) {
    const erosion = erosionRaw as Record<string, unknown>;
    const bits: string[] = [];
    const deleted = Array.isArray(erosion.deleted_test_files)
      ? erosion.deleted_test_files.filter((p): p is string => typeof p === 'string' && p.trim() !== '')
      : [];
    if (deleted.length > 0) {
      const moreDeleted = typeof erosion.deleted_test_files_truncated === 'number'
        ? erosion.deleted_test_files_truncated : 0;
      const tail = moreDeleted > 0 ? ` (+${moreDeleted} more)` : '';
      bits.push(`deleted ${deleted.join(', ')}${tail}`);
    }
    if (typeof erosion.test_count_delta === 'number' && erosion.test_count_delta < 0) {
      bits.push(`${erosion.test_count_delta} tests`);
    }
    if (typeof erosion.assertion_delta === 'number' && erosion.assertion_delta < 0) {
      bits.push(`${erosion.assertion_delta} assertions`);
    }
    if (bits.length > 0) {
      lines.push(`  Test erosion: ${bits.join('; ')}`);
    }
  }

  // Smoketest detail — which suite / named tests failed behind a low (or
  // hard-blocking) smoketest sub-score, so the agent can target the failing
  // startup suite or the named tests instead of generic stabilize work.
  const smokeRaw = qualification.smoketest_detail;
  if (smokeRaw && typeof smokeRaw === 'object' && !Array.isArray(smokeRaw)) {
    const smoke = smokeRaw as Record<string, unknown>;
    const bits: string[] = [];
    if (smoke.startup_failed === true) {
      bits.push('startup suite failed');
    } else if (typeof smoke.failed_suite === 'string' && smoke.failed_suite.trim()) {
      bits.push(`suite ${smoke.failed_suite.trim()} failed`);
    }
    const failedTests = Array.isArray(smoke.failed_tests)
      ? smoke.failed_tests.filter((t): t is string => typeof t === 'string' && t.trim() !== '')
      : [];
    if (failedTests.length > 0) {
      const moreFailed = typeof smoke.failed_tests_truncated === 'number'
        ? smoke.failed_tests_truncated : 0;
      const tail = moreFailed > 0 ? ` (+${moreFailed} more)` : '';
      bits.push(`failed: ${failedTests.join(', ')}${tail}`);
    }
    if (bits.length > 0) {
      lines.push(`  Smoketest: ${bits.join('; ')}`);
    }
  }

  return lines;
}

/**
 * Builds the version-phase status block for the strategic thinker prompt.
 * Explains the current phase, what each phase means, and instructs the agent
 * to emit version_phase_decision when it decides to flip.
 */
export function buildVersionPhaseSection(versionPhase: Record<string, unknown> | undefined): string {
  if (!versionPhase || typeof versionPhase !== 'object') return '';
  const versionNumber = typeof versionPhase.version_number === 'number' ? versionPhase.version_number : null;
  const phase = typeof versionPhase.phase === 'string' ? versionPhase.phase : null;
  if (versionNumber === null || (phase !== 'build' && phase !== 'stabilize')) return '';

  const phaseChangedAt = typeof versionPhase.phase_changed_at === 'string' ? versionPhase.phase_changed_at : null;
  const tasksMergedThisVersion = typeof versionPhase.tasks_merged_this_version === 'number'
    ? versionPhase.tasks_merged_this_version : null;
  const goal = typeof versionPhase.goal === 'string' && versionPhase.goal.trim()
    ? versionPhase.goal.trim() : null;
  // The backend always sends last_qualification as an object (the structured
  // qualification record: {status, candidate_sha, confidence, blocker, ...}) or
  // null — never a string. Render a compact summary line so the agent can see
  // when a qualification is pending / finalized.
  const lastQualRaw = versionPhase.last_qualification;
  const lastQualification = lastQualRaw && typeof lastQualRaw === 'object' && !Array.isArray(lastQualRaw)
    ? lastQualRaw as Record<string, unknown>
    : null;
  const qualStatus = lastQualification && typeof lastQualification.status === 'string'
    ? lastQualification.status
    : null;
  const qualificationPending = qualStatus === 'pending';
  // A finalized conclusive verdict (dry_run, not_promoted, or promoted) means
  // the candidate has been fully evaluated and the outcome is settled. In the
  // shipped-dark default (promotion_enabled=False) a passing candidate records
  // status='dry_run' and the version stays open + stabilize. Re-emitting
  // `qualify_request` for the SAME candidate just re-spawns two agent VMs for
  // an identical, already-scored commit — the orchestrator's cached_verdict
  // guard ignores it. Only a candidate change (new work merged onto the
  // integration branch) makes a fresh qualification meaningful.
  const qualificationDryRun = qualStatus === 'dry_run';

  const parts: string[] = [];
  parts.push(`\n## Version Phase`);
  parts.push(`Current: **v${versionNumber} — ${phase.toUpperCase()}**`);
  if (goal) {
    parts.push(`Goal: ${goal}`);
  }

  if (phaseChangedAt) {
    parts.push(`Phase entered: ${phaseChangedAt}`);
  }
  if (tasksMergedThisVersion !== null) {
    parts.push(`Tasks merged this version: ${tasksMergedThisVersion}`);
  }
  if (lastQualification) {
    // Compact one-line summary: status, then confidence / candidate_sha /
    // blocker when present. Keeps the prompt small while surfacing whether a
    // qualification is already running (status='pending').
    const qualBits: string[] = [];
    qualBits.push(`status=${qualStatus ?? 'unknown'}`);
    if (typeof lastQualification.confidence === 'number') {
      qualBits.push(`confidence=${lastQualification.confidence}`);
    }
    if (typeof lastQualification.candidate_sha === 'string' && lastQualification.candidate_sha) {
      qualBits.push(`candidate_sha=${lastQualification.candidate_sha.slice(0, 12)}`);
    }
    if (typeof lastQualification.blocker === 'string' && lastQualification.blocker) {
      qualBits.push(`blocker=${lastQualification.blocker}`);
    }
    if (qualificationDryRun && lastQualification.would_promote === true) {
      qualBits.push(`would_promote=true`);
    }
    parts.push(`Last qualification: ${qualBits.join(', ')}`);
    parts.push(...formatQualificationDetails(lastQualification));
    if (qualificationPending) {
      parts.push(
        `A qualification is currently **PENDING** (running now). Do NOT emit ` +
        `\`qualify_request\` this cycle — wait for it to finalize.`,
      );
    } else if (qualificationDryRun) {
      const dryRunSha = typeof lastQualification.candidate_sha === 'string' && lastQualification.candidate_sha
        ? ` (commit ${lastQualification.candidate_sha.slice(0, 12)})`
        : '';
      parts.push(
        `The last qualification was a **DRY RUN**${dryRunSha} — it already ` +
        `fully evaluated that candidate. Do NOT emit \`qualify_request\` ` +
        `again UNLESS new work has since merged onto the integration branch ` +
        `(i.e. the candidate commit has changed). Re-qualifying the same ` +
        `already-scored candidate just re-spawns agent VMs and is ignored.`,
      );
    }
  }

  // Promotion-readiness trend (cross-version aggregation).
  const promotionReadinessRaw = versionPhase.promotion_readiness;
  if (promotionReadinessRaw && typeof promotionReadinessRaw === 'object' && !Array.isArray(promotionReadinessRaw)) {
    const pr = promotionReadinessRaw as Record<string, unknown>;
    const prReady = pr.ready === true ? 'YES' : 'NO';
    const prRate = typeof pr.would_promote_rate === 'number' ? pr.would_promote_rate : null;
    const prStreak = typeof pr.consecutive_clean_passes === 'number' ? pr.consecutive_clean_passes : null;
    const prEvaluated = typeof pr.verdicts_evaluated === 'number' ? pr.verdicts_evaluated : null;
    const prReason = typeof pr.reason === 'string' ? pr.reason : null;
    const prConf = pr.confidence && typeof pr.confidence === 'object' && !Array.isArray(pr.confidence)
      ? pr.confidence as Record<string, unknown> : null;
    const prRecurring = pr.recurring_blockers && typeof pr.recurring_blockers === 'object' && !Array.isArray(pr.recurring_blockers)
      ? pr.recurring_blockers as Record<string, unknown> : null;

    const prBits: string[] = [`ready=${prReady}`];
    if (prRate !== null) prBits.push(`would_promote_rate=${prRate}`);
    if (prStreak !== null) prBits.push(`consecutive_clean_passes=${prStreak}`);
    if (prEvaluated !== null) prBits.push(`verdicts_evaluated=${prEvaluated}`);
    if (prConf) {
      const confMean = typeof prConf.mean === 'number' ? prConf.mean : null;
      const confStdev = typeof prConf.stdev === 'number' ? prConf.stdev : null;
      const confStable = prConf.stable === true ? 'stable' : (prConf.stable === false ? 'volatile' : null);
      if (confMean !== null) prBits.push(`confidence_mean=${confMean}`);
      if (confStdev !== null) prBits.push(`confidence_stdev=${confStdev}`);
      if (confStable !== null) prBits.push(`confidence=${confStable}`);
    }
    parts.push(`Promotion readiness: ${prBits.join(', ')}`);
    if (prReason) parts.push(`  Readiness reason: ${prReason}`);
    if (prRecurring && Object.keys(prRecurring).length > 0) {
      const blockerStr = Object.entries(prRecurring).map(([code, n]) => `${code}x${String(n)}`).join(', ');
      parts.push(`  Recurring blockers: ${blockerStr}`);
    }
  }

  // Recent closed versions (newest-first, up to 3).
  const recentVersionsRaw = versionPhase.recent_versions;
  if (Array.isArray(recentVersionsRaw) && recentVersionsRaw.length > 0) {
    const rvLines: string[] = ['Recent versions (newest-first):'];
    for (const rv of recentVersionsRaw) {
      if (!rv || typeof rv !== 'object' || Array.isArray(rv)) continue;
      const rvRec = rv as Record<string, unknown>;
      const rvNum = typeof rvRec.version_number === 'number' ? rvRec.version_number : '?';
      const rvStatus = typeof rvRec.status === 'string' ? rvRec.status : '?';
      const lq = rvRec.last_qualification;
      const lqRec = lq && typeof lq === 'object' && !Array.isArray(lq) ? lq as Record<string, unknown> : null;
      const lqStatus = lqRec && typeof lqRec.status === 'string' ? lqRec.status : null;
      const lqConf = lqRec && typeof lqRec.confidence === 'number' ? lqRec.confidence : null;
      const lqBlocker = lqRec && typeof lqRec.blocker === 'string' && lqRec.blocker ? lqRec.blocker : null;
      const lqBits: string[] = [];
      if (lqStatus) lqBits.push(`qual=${lqStatus}`);
      if (lqConf !== null) lqBits.push(`confidence=${lqConf}`);
      if (lqBlocker) lqBits.push(`blocker=${lqBlocker}`);
      const lqSuffix = lqBits.length > 0 ? ` [${lqBits.join(', ')}]` : '';
      rvLines.push(`  - v${rvNum} (${rvStatus})${lqSuffix}`);
      if (lqRec) {
        rvLines.push(...formatQualificationDetails(lqRec).map(line => `    ${line.trimStart()}`));
      }
    }
    if (rvLines.length > 1) parts.push(rvLines.join('\n'));
  }

  parts.push(`
**Phase meanings:**
- **build** — pursue the version's feature and improvement work at full pace. The version is actively developing; continue creating and executing directions that advance the intent.
- **stabilize** — shift the work-mix toward audit, cleanup, and hardening. The version is approaching a release-qualifiable state; prioritize directions that reduce risk, fix known issues, and improve test coverage over net-new feature work. New feature directions should be deferred to the next version unless they directly unblock stabilization.

**Your judgment on phase:**
Assess whether the current phase still fits the project's state. When a **Goal** is shown above, treat it as what this version is *for* — weigh phase and qualification decisions against whether that goal is substantially met. You are not bound to a schedule or a fixed rule — phase transitions are YOUR call, based on what you observe.

- Flip from **build → stabilize** when: enough feature work has landed that further additions would outpace the project's ability to qualify them, or when there are enough known issues, technical debt, or stability concerns that they should be addressed before continuing to build.
- Flip from **stabilize → build** when: the version has been sufficiently hardened, qualification criteria look achievable, and continued stabilization work has diminishing returns.

If you decide to flip the phase, emit a \`version_phase_decision\` field in your Section 6 problem_model update (as a top-level JSON field alongside the model):
\`\`\`json
{
  "version_phase_decision": {
    "phase": "build" | "stabilize",
    "reasoning": "One or two sentences explaining why you're flipping now."
  }
}
\`\`\`

If you are staying in the current phase, do NOT emit \`version_phase_decision\` — omitting it means "no change."

**Requesting qualification (stabilize only):**
When the version is in **stabilize** and you judge it ready for release — hardening is sufficient, known issues are addressed, and the qualification criteria look achievable — request qualification. The orchestrator then snapshots the integration branch and runs the automated release qualifier (CI, test-erosion, audit, smoketest). Emit a \`qualify_request\` field in your Section 6 problem_model update (a top-level JSON field alongside the model):
\`\`\`json
{
  "qualify_request": {
    "reason": "One sentence explaining why the version is ready to qualify now."
  }
}
\`\`\`
Only request qualification from the **stabilize** phase — it is ignored in **build**. If the version is not yet release-ready, do NOT emit \`qualify_request\` — omitting it means "not ready." \`version_phase_decision\` and \`qualify_request\` are independent: you may emit either, both, or neither.

**Do NOT re-request while a qualification is in flight.** If the "Last qualification" line above shows \`status=pending\`, a qualification is already running for this version — the orchestrator has snapshotted a candidate and spawned the audit + smoketest. Do NOT emit \`qualify_request\` again; wait for the in-flight run to finalize (its status will change to \`promoted\`, \`not_promoted\`, \`dry_run\`, or an error). Re-requesting while \`status=pending\` is wasted work and is ignored.

**Do NOT re-request after a dry-run of the same candidate.** If the "Last qualification" line shows \`status=dry_run\`, the qualifier already ran end-to-end (CI, test-erosion, audit, smoketest) on that candidate commit and recorded the result — a dry-run means the candidate would promote but promotion currently ships dark, so the version stays open in \`stabilize\`. Re-emitting \`qualify_request\` for the SAME candidate re-spawns two agent VMs to re-score an identical, already-evaluated commit; the orchestrator's already-dry-run guard ignores it. Only emit \`qualify_request\` again once new work has merged onto the integration branch since that dry-run — i.e. the candidate commit has actually changed. If nothing has merged since the \`dry_run\` line above, do NOT re-request.`);

  return parts.join('\n');
}

/**
 * Builds the memo-citations block for the strategic thinker prompt.
 * Memos are terminal reasoning artifacts spawned from research findings the
 * packer dropped under budget pressure; the backend surfaces a digest of the
 * recent ones so the strategic reviewer can cite them by short handle. Each memo
 * is assigned a stable short handle (M0, M1, M2, ...) alongside its real task_id
 * for traceability. Returns '' when there are no memos for this cycle window.
 */
export function buildMemoCitationsSection(memoCitations: unknown): string {
  if (!Array.isArray(memoCitations) || memoCitations.length === 0) return '';
  const lines: string[] = ['\n## Memo Citations (cross-cycle institutional memory)'];
  lines.push(
    'Memos are durable cross-cycle institutional memory: reasoning artifacts from ' +
    'prior cycles that capture why findings were set aside, why directions were ' +
    'retired, or why patterns emerged. Each memo is a completed terminal reasoning ' +
    'task — genuinely useful context that transcends a single cycle.\n' +
    'As you reason about gaps and directions below, actively check whether any memo ' +
    'is relevant to your thinking. If a memo genuinely informs a decision or shapes ' +
    'your understanding of a pattern, cite it by its short handle (e.g., "memo M0", ' +
    '"memo M1"). The handle is stable and maps reliably back to the full task_id for ' +
    'traceability. Cite only when a memo authentically informs your reasoning — ' +
    'valuable citations strengthen the record; performative ones do not.',
  );
  for (let i = 0; i < memoCitations.length; i++) {
    // eslint-disable-next-line @typescript-eslint/no-unsafe-assignment
    const m = memoCitations[i];
    if (!m || typeof m !== 'object') continue;
    const rec = m as Record<string, unknown>;
    const compactText = (value: string) => value.replace(/\s+/g, ' ').trim();
    const taskId = typeof rec.task_id === 'string' && rec.task_id ? rec.task_id : '?';
    const excerpt = typeof rec.summary_excerpt === 'string' ? rec.summary_excerpt.trim() : '';
    const handle = `M${i}`;
    const metaParts: string[] = [`task_id: ${taskId}`];
    if (typeof rec.reasoning_kind === 'string' && rec.reasoning_kind) {
      metaParts.push(`kind: ${rec.reasoning_kind}`);
    }
    if (typeof rec.direction_id === 'string' && rec.direction_id) {
      metaParts.push(`direction: ${rec.direction_id}`);
    }
    const tags = Array.isArray(rec.tags) && rec.tags.length > 0
      ? (rec.tags as unknown[]).filter((t) => typeof t === 'string').join(', ')
      : '';
    if (tags) metaParts.push(`tags: ${tags}`);
    lines.push(`- ${handle} (${metaParts.join('; ')}): ${excerpt || '(no summary)'}`);
    const citationRefs = typeof rec.citation_refs === 'string' ? rec.citation_refs.trim() : '';
    if (citationRefs) {
      lines.push(`  citation_refs: ${citationRefs}`);
    }
    if (typeof rec.success_evidence === 'string' && rec.success_evidence) {
      lines.push(`  success_evidence: ${rec.success_evidence}`);
    }
    if (typeof rec.retirement_condition === 'string' && rec.retirement_condition) {
      lines.push(`  retirement_condition: ${rec.retirement_condition}`);
    }
    const finalMetricsText = typeof rec.final_metrics_excerpt === 'string'
      ? rec.final_metrics_excerpt.trim()
      : (rec.final_metrics && typeof rec.final_metrics === 'object'
        ? Object.entries(rec.final_metrics as Record<string, unknown>)
          .map(([key, value]) => `${key}=${String(value)}`)
          .join(', ')
        : '');
    if (finalMetricsText) {
      lines.push(`  final_metrics: ${finalMetricsText}`);
    }
    const rationaleExcerpt = typeof rec.rationale_excerpt === 'string' && rec.rationale_excerpt
      ? compactText(rec.rationale_excerpt)
      : typeof rec.rationale === 'string'
        ? compactText(rec.rationale).slice(0, 200)
        : '';
    if (rationaleExcerpt) {
      lines.push(`  rationale: ${rationaleExcerpt}`);
    }
    const reasoningExcerpt = typeof rec.reasoning_excerpt === 'string' && rec.reasoning_excerpt
      ? compactText(rec.reasoning_excerpt)
      : typeof rec.reasoning === 'string'
        ? compactText(rec.reasoning).slice(0, 200)
        : '';
    if (reasoningExcerpt) {
      lines.push(`  reasoning: ${reasoningExcerpt}`);
    }
  }
  return lines.join('\n');
}

/**
 * Resolves memo handle references (e.g., "M0", "M1") back to their real task_ids.
 * Scans analysisText for handle patterns and returns the corresponding task_ids
 * in the order they appear. Handles are format like M0, M1, M2, etc.
 * Used to make memo citation capture robust to UUID reproduction fragility.
 */
export function resolveMemoHandles(
  analysisText: string,
  memoCitations: Array<Record<string, unknown> | null | undefined>
): string[] {
  if (memoCitations.length === 0) return [];
  const matches: Array<{ taskId: string; position: number }> = [];

  for (let i = 0; i < memoCitations.length; i++) {
    const m = memoCitations[i];
    if (!m || typeof m !== 'object') continue;
    const taskId = m.task_id;
    if (typeof taskId !== 'string' || !taskId) continue;

    // Look for the handle pattern M{i} (e.g., M0, M1, M2...). The word
    // boundaries keep "M01" from matching handle "M0" and also cover
    // phrase contexts like "memo M0" and "(M0)".
    const handlePattern = new RegExp(`\\bM${i}\\b`);
    const match = handlePattern.exec(analysisText);
    if (match) {
      matches.push({ taskId, position: match.index });
    }
  }

  // Contract (see docstring): return task_ids in the order their handles
  // appear in analysisText, sorted by first-match position.
  matches.sort((a, b) => a.position - b.position);
  return matches.map((entry) => entry.taskId);
}

/**
 * Builds the efficiency-axes section for the strategic thinker prompt.
 * Shows per-axis current value, baseline, and drift_pct so the strategic
 * agent can track whether pipeline efficiency is improving or regressing.
 * Returns a 'data unavailable' marker (not empty string) when absent so the
 * agent is never left guessing whether efficiency was intentionally omitted.
 */
export function buildEfficiencySnapshotSection(efficiencySnapshot: unknown): string {
  const AXIS_LABELS: Record<string, string> = {
    cost_per_merged_pr: 'Cost/merged PR (USD)',
    llm_cost_per_merged_pr: 'LLM cost/merged PR (USD)',
    agent_timeout_rate: 'Agent timeout rate',
    vm_cold_start_p50_ms: 'VM cold-start p50 (ms)',
    vm_reuse_rate: 'VM reuse rate',
    median_api_calls_per_task: 'Median API calls/task',
    median_review_iterations: 'Median review iterations',
    fleet_utilization: 'Fleet slot utilization',
  };

  if (!efficiencySnapshot || typeof efficiencySnapshot !== 'object') {
    return '\n## Pipeline Efficiency Axes\n_data unavailable — no snapshot recorded yet_\n';
  }
  const snap = efficiencySnapshot as Record<string, unknown>;
  const axes = snap.axes && typeof snap.axes === 'object' ? snap.axes as Record<string, unknown> : {};
  const axisKeys = Object.keys(axes);
  if (axisKeys.length === 0) {
    return '\n## Pipeline Efficiency Axes\n_data unavailable — no snapshot recorded yet_\n';
  }

  const fmt = (v: unknown): string => {
    if (v === null || v === undefined) return 'n/a';
    if (typeof v === 'number') return v.toString();
    if (typeof v === 'string' || typeof v === 'boolean') return String(v);
    return 'n/a';
  };
  const fmtDrift = (v: unknown): string => {
    if (v === null || v === undefined) return 'n/a';
    if (typeof v !== 'number') return typeof v === 'string' ? v : 'n/a';
    const sign = v > 0 ? '+' : '';
    return `${sign}${v.toFixed(1)}%`;
  };

  const lines: string[] = ['\n## Pipeline Efficiency Axes'];
  const lastAt = typeof snap.last_snapshot_at === 'string' ? snap.last_snapshot_at.slice(0, 10) : 'unknown';
  const baseAt = typeof snap.baseline_captured_at === 'string' ? snap.baseline_captured_at.slice(0, 10) : 'unknown';
  lines.push(`Baseline: ${baseAt}  |  Last snapshot: ${lastAt}`);
  lines.push('');
  lines.push('axis | current | baseline | drift');
  lines.push('--- | --- | --- | ---');
  for (const key of axisKeys) {
    const data = axes[key] as Record<string, unknown> | undefined;
    if (!data || typeof data !== 'object') continue;
    const label = AXIS_LABELS[key] ?? key;
    const cur = fmt(data.current);
    const base = fmt(data.baseline);
    const drift = fmtDrift(data.drift_pct);
    lines.push(`${label} | ${cur} | ${base} | ${drift}`);
  }
  if ('fleet_utilization' in axes) {
    lines.push('');
    lines.push(
      '_Fleet slot utilization is the share of max-plan account slots in use (now + short trailing average) across the whole fleet. Chronically low utilization WITH verified non-clashing backlog means throughput is being left on the table — parallelize existing work or open more non-clashing directions. But distinguish idle headroom from cooled accounts (shown only when present): cooldowns are a rate-limit problem, not a reason to manufacture work. Near capacity is healthy, not a deficit._',
    );
  }

  const fc = snap.format_compliance && typeof snap.format_compliance === 'object'
    ? snap.format_compliance as Record<string, unknown>
    : null;
  if (fc) {
    const fmtRate = (key: string): string => {
      const r = fc[key] as Record<string, unknown> | undefined;
      if (!r || typeof r !== 'object') return 'n/a';
      return `${fmt(r.current)} (baseline ${fmt(r.baseline)}, drift ${fmtDrift(r.drift_pct)})`;
    };
    lines.push('');
    lines.push('**Format compliance:**');
    lines.push(`  format_compliance_rate: ${fmtRate('format_compliance_rate')}`);
    lines.push(`  self_repair_success_rate: ${fmtRate('self_repair_success_rate')}`);
  }

  const atr = snap.agent_timeout_rate && typeof snap.agent_timeout_rate === 'object'
    ? snap.agent_timeout_rate as Record<string, unknown>
    : null;
  if (atr) {
    const overall = atr.overall && typeof atr.overall === 'object'
      ? atr.overall as Record<string, unknown>
      : null;
    const overallStr = overall
      ? `${fmt(overall.current)} (baseline ${fmt(overall.baseline)}, drift ${fmtDrift(overall.drift_pct)})`
      : 'n/a';
    const rawSubtype = atr.dominant_subtype;
    const dominantSubtype = typeof rawSubtype === 'string' || typeof rawSubtype === 'number' ? String(rawSubtype) : null;
    const rawShare = atr.dominant_share;
    const dominantShare = typeof rawShare === 'string' || typeof rawShare === 'number' ? String(rawShare) : null;
    const dominantStr = dominantSubtype
      ? ` — dominant: ${dominantSubtype}${dominantShare != null ? ` (${dominantShare})` : ''}`
      : '';
    lines.push('');
    lines.push(`**Agent timeout rate:** ${overallStr}${dominantStr}`);
  }

  return lines.join('\n') + '\n';
}

// Portfolio-level view computed from per-direction data — surfaces imbalances
// (category skew, system coverage gaps, concentration, age) as explicit
// decision inputs for the strategic thinker.
export function buildPortfolioComposition(
  activeDirections: Array<Record<string, unknown>>,
  systems: Array<Record<string, unknown>>,
  activeByScope: Record<string, number> | undefined,
  authoritativeActiveTotal?: number,
  authoritativeSystemCounts?: Record<string, number>,
): string {
  const shipped = activeDirections.length;
  // Anchor the empty-state guard and percentage denominator on the trim-proof
  // authoritative active total when present; fall back to the (possibly trimmed)
  // shipped row count for old payloads/tests that omit it. Never render
  // "portfolio is empty" while the authoritative count says directions are active.
  const total = (typeof authoritativeActiveTotal === 'number' && authoritativeActiveTotal >= 0)
    ? authoritativeActiveTotal
    : shipped;
  if (total === 0) return 'No active directions — portfolio is empty.';
  if (shipped === 0) {
    return `Portfolio has ${total} active direction(s), but their per-direction detail was not shipped this cycle (trimmed/dropped under the byte budget) — category mix unavailable. Fetch with \`./fetch direction <id>\`.`;
  }

  const catCounts: Record<string, number> = { product: 0, structural: 0, mixed: 0, uncategorized: 0 };
  const systemCounts: Record<string, number> = {};
  const ageFlags: string[] = [];
  for (const d of activeDirections) {
    const rawCat = typeof d.category === 'string' ? (d.category) : '';
    const cat = ['product', 'structural', 'mixed'].includes(rawCat) ? rawCat : 'uncategorized';
    catCounts[cat] = (catCounts[cat] || 0) + 1;
    const sys = typeof d.system_id === 'string' ? (d.system_id) : '';
    if (sys) systemCounts[sys] = (systemCounts[sys] || 0) + 1;
    const cycles = typeof d.total_cycles === 'number' ? (d.total_cycles) : 0;
    if (cycles >= 10) {
      const id = typeof d.id === 'string' ? (d.id) : '?';
      const text = typeof d.text === 'string' ? (d.text).slice(0, 60) : '';
      ageFlags.push(`${id} (${cycles} cycles): ${text}`);
    }
  }

  const pct = (n: number) => total > 0 ? Math.round((n / total) * 100) : 0;
  const productPct = pct(catCounts.product);
  const lines: string[] = [];
  // When the shipped rows are fewer than the authoritative total, the category
  // mix is computed from the shipped subset but percentages divide by the true
  // total — flag that so the reader does not mistake a partial mix for the whole.
  if (shipped < total) {
    lines.push(`_Category mix below computed from ${shipped} of ${total} active directions shipped this cycle (${total - shipped} trimmed for budget — fetch with \`./fetch direction <id>\`); percentages use the authoritative total of ${total}._`);
  }
  const catParts = (['product', 'structural', 'mixed', 'uncategorized'] as const)
    .filter(c => catCounts[c] > 0)
    .map(c => `${c}=${catCounts[c]} (${pct(catCounts[c])}%)`);
  const productFlag = productPct < 30 ? ' [LOW PRODUCT RATIO — <30%]' : '';
  lines.push(`Category mix: ${catParts.join(', ')}${productFlag}`);

  // Per-system signals (coverage gaps + concentration risk). Prefer the
  // authoritative per-system active map when present — it is computed from the
  // FULL direction set before trimming, so a system whose only direction was
  // trimmed out of the shipped rows is never falsely reported uncovered, and a
  // trimmed direction still counts toward concentration. Fall back to the
  // shipped-row map for older payloads/tests that omit it.
  const hasAuthSystemCounts = authoritativeSystemCounts != null
    && typeof authoritativeSystemCounts === 'object';
  const effectiveSystemCounts: Record<string, number> = hasAuthSystemCounts
    ? authoritativeSystemCounts
    : systemCounts;
  // The shipped detail list is incomplete when fewer active rows shipped than
  // the authoritative total — i.e. some active directions were trimmed.
  const listIncomplete = shipped < total;

  const allSystems = systems.filter(s => typeof s.id === 'string').map(s => ({
    id: s.id as string,
    name: typeof s.name === 'string' ? (s.name) : (s.id as string),
  }));
  if (allSystems.length > 0) {
    const mapped = allSystems.filter(s => (effectiveSystemCounts[s.id] || 0) > 0);
    if (hasAuthSystemCounts || !listIncomplete) {
      // Authoritative map present, OR the shipped rows are complete — uncovered
      // claims are trustworthy.
      const uncovered = allSystems.filter(s => (effectiveSystemCounts[s.id] || 0) === 0);
      lines.push(`System coverage: ${mapped.length}/${allSystems.length} vision systems have directions`
        + (uncovered.length > 0 ? ` — gaps: ${uncovered.map(s => `${s.name} (${s.id})`).join(', ')}` : ''));
    } else {
      // Map absent AND shipped rows incomplete — cannot prove a system is
      // uncovered (its only direction may have been trimmed for budget).
      // Suppress the false gap list; report only the verifiable lower bound.
      lines.push(`System coverage: at least ${mapped.length}/${allSystems.length} vision systems have directions (coverage gaps not shown — per-direction detail trimmed for budget, so an unmapped system may still be covered; fetch with \`./fetch direction <id>\`)`);
    }
  }

  const concentrated = Object.entries(effectiveSystemCounts).filter(([, n]) => n >= 3);
  if (concentrated.length > 0) {
    const parts = concentrated.map(([sid, n]) => `${sid}=${n}`);
    // When derived from the shipped subset only (no authoritative map, list
    // trimmed), the count may under-report directions that were trimmed.
    const partialNote = (!hasAuthSystemCounts && listIncomplete)
      ? ' [from shipped subset only — may under-count trimmed directions]'
      : '';
    lines.push(`CONCENTRATION RISK: 3+ directions on same system: ${parts.join(', ')}${partialNote}`);
  }

  if (activeByScope) {
    const scopeParts: string[] = [];
    for (const s of ['small', 'medium', 'large', 'unknown']) {
      const n = activeByScope[s];
      if (typeof n === 'number' && n > 0) scopeParts.push(`${s}=${n}`);
    }
    if (scopeParts.length > 0) lines.push(`In-flight scope mix: ${scopeParts.join(', ')}`);
  }

  if (ageFlags.length > 0) {
    lines.push(`Long-running (10+ cycles without completion): ${ageFlags.join('; ')}`);
  }

  return lines.join('\n');
}

/**
 * Bootstrap-from-goal research guidance, gated on the unified greenfield
 * predicate (`isGreenfieldRepo`) so the research bootstrap gate and the plan
 * handler's GREENFIELD-MODE gate consult the identical signal and can never
 * disagree. The predicate is greenfield when the durable onboarding signal
 * (`task.repo_is_empty`, persisted as `build_config.repo_is_empty`) is truthy
 * OR the live full-clone `detectGreenfield()` scan says so.
 *
 * Returns the "nearly empty codebase → bootstrap the foundation" paragraph ONLY
 * when the repo is greenfield. Mature-repo agents get `''`, so the guidance
 * stops appearing as noise in every research prompt. The leading blank line
 * keeps spacing when the paragraph is interpolated between other mission
 * bullets.
 */
export function buildEmptyRepoGuidance(task: Record<string, unknown>): string {
  if (!isGreenfieldRepo(task)) return '';
  return `\n**If the codebase is nearly empty** (few source files, mostly config/scaffolding): this is
normal for new projects. Use the design doc specs and the project's config files to create
foundational structure. Output a large-scope finding that bootstraps the target system.\n`;
}

/**
 * Renders the CAPABILITY VERIFICATION section for the tactical research prompt,
 * mirroring the inline SYSTEM VERIFICATION block. The backend's research-side
 * verification loop historically classified only design-doc `systems/` entries;
 * design-doc CAPABILITIES claimed working/partial were never verified for the
 * research agent. So a capability whose backing code is absent on the
 * integration branch slipped through, and the agent reasoned about — and
 * proposed improvements to — behavior that does not exist.
 *
 * The backend now emits a parallel `capability_verification` map (same shape as
 * `system_verification`: per-capability `status` of claimed_absent/partial/
 * verified plus `claimed_status`/`present`/`missing`). This helper surfaces it
 * as a prominent prompt section the agent cannot miss. The classification is
 * conservative by construction (computed backend-side the same way systems are):
 * a capability is flagged claimed_absent only when EVERY referenced backing path
 * is absent; a present path makes it `partial`; a capability with no path
 * pointer is never classified and so never appears here. Returns '' when no
 * verification map is provided, so existing prompts are unchanged.
 */
export function buildCapabilityVerificationSection(
  capVerif: Record<string, Record<string, unknown>> | undefined,
): string {
  if (!capVerif || typeof capVerif !== 'object' || Object.keys(capVerif).length === 0) return '';
  const _pm = capVerif as Record<string, unknown>;
  const computedAt = typeof _pm['_computed_at'] === 'string' ? _pm['_computed_at'] : null;
  const branchSha = typeof _pm['_branch_sha'] === 'string' ? _pm['_branch_sha'] : null;
  const absent: string[] = [];
  const partial: string[] = [];
  const verified: string[] = [];
  for (const capName of Object.keys(capVerif).sort()) {
    if (capName.startsWith('_')) continue;
    const v = capVerif[capName];
    const status = strVal(v.status as string);
    const missing = Array.isArray(v.missing) ? v.missing as string[] : [];
    const present = Array.isArray(v.present) ? v.present as string[] : [];
    const claimed = strVal(v.claimed_status as string);
    const claimedNote = claimed ? ` (design-doc status: ${claimed})` : '';
    if (status === 'claimed_absent') {
      absent.push(`- **${capName}**${claimedNote}: CLAIMED-BUT-ABSENT — all ${missing.length} declared backing path(s) missing: ${missing.join(', ')}`);
    } else if (status === 'partial') {
      partial.push(`- **${capName}**${claimedNote}: PARTIAL — missing: ${missing.join(', ')}; present: ${present.join(', ')}`);
    } else if (status === 'verified') {
      verified.push(`- **${capName}**: verified — ${present.length} declared path(s) exist`);
    }
  }
  if (absent.length === 0 && partial.length === 0 && verified.length === 0) return '';
  const vlines: string[] = ['# CAPABILITY VERIFICATION (code existence vs. design-doc claims)'];
  if (computedAt) {
    const shaNote = branchSha ? ` against ${branchSha.slice(0, 12)}` : '';
    vlines.push(`_Verdicts computed ${computedAt}${shaNote}. If this task was retried after a delay, the branch may have changed — treat older stamps as advisory._`);
  } else {
    vlines.push('_⚠️ STAMP MISSING — verdicts may be from an earlier run and could reflect a stale branch state._');
  }
  vlines.push('The backend verified each design-doc capability\'s declared backing paths against the integration-branch file tree. Do NOT trust a capability\'s status over this evidence — if a capability is flagged below, treat its claimed status (working/partial) as unreliable, design from what actually exists, and do NOT propose improvements to behavior that is not built.');
  if (absent.length > 0) {
    vlines.push('\n## ⚠️ Claimed-but-absent (effectively missing — backing code does not exist)');
    vlines.push(...absent);
  }
  if (partial.length > 0) {
    vlines.push('\n## ⚠️ Partial (some declared backing paths missing)');
    vlines.push(...partial);
  }
  if (verified.length > 0) {
    vlines.push('\n## Verified (all declared backing paths present)');
    vlines.push(...verified);
  }
  return vlines.join('\n') + '\n\n';
}

/**
 * Renders the shared "AVAILABLE CAPABILITIES" vocabulary section from the
 * backend-supplied `available_capabilities` payload field. This is the SINGLE
 * shared namespace both producers (a `provides_capability` stamp) and consumers
 * (a `prerequisite` capability ref) draw from, so they converge on the same
 * string instead of inventing divergent free prose that the EXACT (non-fuzzy)
 * matcher can never bridge.
 *
 * Each entry is `{name, status?}` where `name` is a canonical kebab-case
 * capability name from `problem_model.capabilities` and `status` is its declared
 * working/partial/planned state. Rendered uniformly across research, plan,
 * strategic, and design prompts. Returns '' when the field is absent/empty so
 * existing prompts are byte-for-byte unchanged.
 */
export function buildAvailableCapabilitiesSection(caps: unknown): string {
  if (!Array.isArray(caps) || caps.length === 0) return '';
  const lines: string[] = [];
  let anyProofStatus = false;
  for (const c of caps) {
    if (!c || typeof c !== 'object' || Array.isArray(c)) continue;
    const rec = c as Record<string, unknown>;
    const name = strVal(rec.name);
    if (!name) continue;
    const status = strVal(rec.status);
    // proof_status is the SECOND, durable axis (proven / unproven) the strategist
    // records at merge time — independent of the declared status. Render it
    // alongside status when present so a working-but-unproven capability reads
    // differently from a proven one. Absent-field callers render exactly as
    // before (back-compat: `(working)` / `(planned)` / bare name).
    const proofStatus = strVal(rec.proof_status);
    if (proofStatus) anyProofStatus = true;
    const qualifiers = [status, proofStatus].filter(Boolean);
    lines.push(qualifiers.length ? `- \`${name}\` (${qualifiers.join(', ')})` : `- \`${name}\``);
  }
  if (lines.length === 0) return '';
  const legend = anyProofStatus
    ? 'Each entry shows its declared `status` (working / partial / planned) and, when known, its durable `proof_status` (proven / unproven). `proven` means evidence-grounded proof records exist; `working, unproven` means the registry declares it working but no proof has been recorded yet — prefer proven capabilities when a prerequisite must be reliable, and treat unproven ones as claims still awaiting verification.'
    : null;
  return [
    '# AVAILABLE CAPABILITIES (shared vocabulary)',
    'These are the project\'s canonical capability names — the strategist-curated registry (`problem_model.capabilities`). It is the SHARED VOCABULARY for cross-lane prerequisites: when you declare a `prerequisite` capability ref OR a `provides_capability` stamp, DRAW THE NAME FROM THIS LIST so producers and consumers converge on the SAME string (the matcher is EXACT, never fuzzy). Propose a NEW kebab-case name (same convention) ONLY for a genuinely new capability not already listed here.',
    ...(legend ? [legend] : []),
    ...lines,
  ].join('\n') + '\n\n';
}

// Shared, uniform documentation of the capability-declaration framework, embedded
// identically in the research, plan, strategic, and design prompts so EVERY task
// type participates in the same vocabulary (not research special-casing). Frames
// both the producer stamp and the consumer ref as drawn from AVAILABLE CAPABILITIES.
export const CAPABILITY_DECLARATION_DOC = [
  '**Shared capability vocabulary (applies to this and every other task type):** you may declare a `provides_capability` stamp (a capability your work ADVERTISES) and/or a `prerequisite` capability ref (a capability your work DEPENDS ON). BOTH must be drawn from the AVAILABLE CAPABILITIES list above when one fits — the registry is the shared namespace, and the resolver matches EXACTLY (no fuzzy matching), so a free-prose name a different lane will never reproduce leaves the dependency stranded. Use the SAME kebab-case convention and propose a NEW name ONLY for a genuinely new capability absent from the list. An off-registry ref is still accepted (never rejected) and flagged advisory for the strategist to fold into the registry.',
].join('\n');

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
  const promptFingerprint = computeResearchPlanPromptFingerprint('plan');
  const setPromptFingerprint = (indexMod as { setCurrentPromptFingerprint?: (fp: string) => void }).setCurrentPromptFingerprint;
  if (setPromptFingerprint) setPromptFingerprint(promptFingerprint);
  const sendHeartbeat = (indexMod as { sendHeartbeatSync?: (taskId: string, status: string, extra?: Record<string, unknown>) => void }).sendHeartbeatSync;
  if (sendHeartbeat) sendHeartbeat(taskId, statusRef.value, { prompt_fingerprint: promptFingerprint });

  const contextPrefix = buildContextPrefix(task);

  const claudeMd = readClaudeMd();
  const claudeMdPrefix = claudeMd ? `## Codebase Instructions\n${claudeMd}\n\n` : '';
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
  "no_op_reason": "Specific, verified reason (at least 30 chars) naming exactly what is already present / why no code subtasks are needed — cite the concrete files, symbols, or sibling work you checked, e.g. 'app/config.py already loads repo layout from settings.REPO_LAYOUT and shared_state.py:get_repo_root() derives all paths from it; no hardcoded layout remains to de-hardcode'."
}

The no_op_reason MUST be at least 30 characters and name the concrete evidence you verified. This is the ONLY clean way for a plan to complete with zero subtasks — an empty "subtasks" array WITHOUT a specific no_op_reason is treated as a failed plan. This is NOT a give-up path: only use it when you have actually VERIFIED the work is already done or genuinely needs no code change. If you simply could not figure out how to decompose the task, that is a real plan failure — let the agent exit non-zero rather than emitting a bogus no_op_reason.${isGreenfield ? `

GREENFIELD CAVEAT: on this empty/near-empty repo, "there is no source code to change" or "the codebase is empty" is NOT a valid no_op_reason. Absent source is exactly the signal to bootstrap — decompose "build the foundation for the goal" into ordered subtasks driven by the Project Vision / goal and any design-doc specs. Only emit a no-op here if the foundation the goal requires already exists on this branch.` : ''}

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
- BUILD-ON ordering (critical): if a subtask CONSUMES code or artifacts another subtask produces — it imports a module/file the other creates, calls a new function/class it adds, or builds a feature on top of its output — set depends_on_indices so the consumer comes AFTER the producer, EVEN IF the two touch DIFFERENT files. Sibling subtasks run concurrently against a clone of the same base; if the producer has not merged, the consumer's clone is missing the new code and it will fail to import it or re-create a conflicting stub. Genuinely independent subtasks (including small additive edits to shared infra) MUST stay parallel — only order true producer→consumer relationships, and make the consumer's related_files/prompt name the producer's file so the ordering is explicit.
- Aim for 1-4 subtasks. Prefer 1-2 large subtasks over 4-6 small ones. If the task can be one PR, output a single subtask.
- Do NOT include review, testing, or deployment subtasks — those are handled automatically by the pipeline
- Use effort=high only for complex architectural subtasks. Leave null for simple additive changes — the orchestrator resolves the model from provider routing.
- scope: "small" for changes under ~100 lines across 1-3 files with clear, narrow scope, "medium" for changes spanning 3-8 files or 100-300 lines, or touching shared infrastructure, "large" for cross-component architectural changes, 300+ lines, or changes to auth/data models/core orchestration
- The "type" field controls how the subtask is executed: "create" (default) for new features/additions, "refactor" for code quality improvements with no behavior change, "migrate" for database schema changes (Alembic migrations), "qa" for test-only PRs. The type field must be EXACTLY one of those values (or a knowledge-prerequisite type below). "fix" is NOT a valid subtask type — a bug-fixing subtask is type "create" even when its TITLE uses the "fix:" conventional-commit prefix (titles and types are independent; a plan child always starts from a fresh branch, which is what "create" means).
- EMPIRICAL PREREQUISITE (optional, additive): besides the implementation subtypes above, a plan MAY emit a measurement prerequisite subtask of type="compute_experiment" when a later subtask should "measure this first, then act on the verdict". It does NOT produce a PR — it launches a budgeted, auto-torn-down compute job and persists a durable MEASURED VERDICT as knowledge. It carries a launch contract: "command" (the experiment to run), a positive "spend_cap_usd", a positive "deadline_seconds", and an OPTIONAL "success_criterion": {"metric", "operator", "threshold"}. A later implementation subtask consumes the verdict by naming this subtask's index in its depends_on_indices — it is handed the experiment's knowledge at launch. A MEASURED NEGATIVE result (success_criterion honestly missed) is still consumable evidence: the dependent still runs and acts on the verdict. Only a BROKEN run that produced no usable verdict cancels the dependent follow-on work. REQUIRED: every compute_experiment subtask must include a "smoke_sample" field proving the command was tested on this VM before writing the plan. Run: BPRF=$(mktemp); timeout 30 bash -c "BLUEPRINT_RESULT_FILE=\\"$BPRF\\" <your command>" 2>/tmp/ce_smoke_err.txt; CE_EXIT=$?; then record {"exit_code":CE_EXIT,"startup_ok":true/false,"metric_captured":true/false,"timed_out":(CE_EXIT==124),"error_excerpt":"<stderr snippet>"}. Nothing beyond the repo itself is guaranteed; your smoke is proof. If startup_ok=false, fix or drop the subtask. If genuinely un-smokable (special hardware required), set smoke_sample={"unsmokable":true,"unsmokable_reason":"<why>"} for explicit operator review.
- acceptance_evidence (OPTIONAL, additive — omit when you have nothing substantive to declare): the concrete evidence that will prove THIS subtask's claim true, carried forward to the implementing agent and the evaluation lane. Exactly one of four kinds: (1) {"kind": "named_test", "test_path": "tests/test_x.py", "test_id": "test_fn"} — a test exercising the changed seam (test_path must name a real test file; test_id optional, or embed inline as path::test_fn); (2) {"kind": "observable_behavior", "description": "..."} — a substantive (≥10 char, non-placeholder) description of a behavior that should be exercised; (3) {"kind": "downstream_artifact", "artifact_id": "..."} — an artifact a later task consumes; (4) {"kind": "visual", "component": "<UI component under test>", "viewport_width": <px, 1-4096>, "assertion_type": "<optional, e.g. overflow|text_truncation>"} — for FRONTEND-TOUCHING work (admin-ui components), proven by an uploaded viewport screenshot of that component captured at that width on a successful run. Declare it only for subtasks where the success condition is concrete; never invent a placeholder/always-true declaration (those are rejected).
- prerequisite (OPTIONAL, default null): a FOUNDATION this subtask depends on that is built OUTSIDE this plan (another lane / an already-built capability). Shape: \`{"kind": "task", "ref": "<task-id or finding-hash>"}\` or \`{"kind": "capability", "ref": "<capability-name>"}\`. For a capability ref, DRAW THE NAME from the AVAILABLE CAPABILITIES vocabulary above so it resolves to whichever task advertises it (the matcher is EXACT). Use depends_on_indices — NOT prerequisite — to order subtasks WITHIN this plan; prerequisite is only for cross-lane/cross-plan foundations.
- provides_capability (OPTIONAL, default null): a short kebab-case capability name THIS subtask advertises so another lane's prerequisite \`{"kind": "capability", "ref": "<name>"}\` can depend on it. DRAW THE NAME from the AVAILABLE CAPABILITIES vocabulary above when one fits; propose a NEW kebab-case name (same convention) ONLY for a genuinely new capability. ${CAPABILITY_DECLARATION_DOC}
- ATOMIC SCHEMA RULE: a database schema change is ONE subtask, never split across dependent subtasks. If a subtask modifies SQLAlchemy/SQLModel models (backend/app/models.py) or adds/changes tables or columns, that SAME subtask must also generate the Alembic migration and bump the schema revision. Do NOT emit a separate dependent type="migrate" subtask for the migration of a model change in another subtask — splitting the model edit from its migration manufactures dead serialization and ships a model PR with no migration (which reviewers reject). Set the subtask's type="migrate" and include in its related_files the model file(s) (backend/app/models.py), the new Alembic file (backend/alembic/versions/...), and backend/app/main.py for EXPECTED_SCHEMA_REVISION. The migration agent will handle Alembic generation and the revision bump within that single subtask/PR. Only create a separate subtask when the other work is genuinely independent of the schema change — never to carry the migration for it.
- If any subtask adds new user-facing functionality, consider whether a test subtask is needed. New API endpoints should have corresponding integration tests. New agent handlers should have at least one smoke test. Add a test subtask with depends_on_indices pointing to the implementation subtask.
- If any subtask changes a public API interface (endpoint signatures, request/response shapes, TypeScript types exported to frontend), include explicit notes in the prompt about backward compatibility requirements.
- IMPORTANT: Write your plan JSON to /tmp/plan_output_${taskId}.json ${READ_ONLY_BASH_WRITE_NOTE} (NOT in the repository). The file should contain ONLY the JSON object, no markdown wrapping.
- ALSO write /tmp/plan_progress_${taskId}.md ${READ_ONLY_BASH_WRITE_NOTE} — a concise markdown summary (under ~3000 chars) of your decomposition: which areas of the codebase you explored, the rationale for how you split the work, the file targets and key constraints per subtask, and any risks or open questions. This artifact is surfaced to retry agents if this task fails, so they can pick up without re-investigating.${planPromptSuffix}${planFirstTaskSection}${PLAYBOOK_GUIDANCE}

${planFetchGuidance}${DEFAULT_TOOL_GUIDANCE}`;

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
  if (!logs) {
    const repair = await repairStructuredOutput<Record<string, unknown>>({
      mode: 'object',
      requiredKeys: ['subtasks'],
      schemaLabel: 'research_plan',
      first: { exitCode, allOutput, lastResultText },
      reopenPrompt: `You did not emit the plan JSON in the required schema. Output ONLY the JSON object now — no markdown, no explanation. Required field: subtasks (array). Schema:\n\n{"subtasks":[{"title":"...","prompt":"...","type":"create","scope":"medium","related_files":["path/to/file.ts"],"depends_on_indices":[]}]}\n\nOutput ONLY the JSON object.`,
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
  const planProgress = readProgressArtifact(taskId, 'plan');
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
  const researchMode = strVal(payload.research_mode as string, 'research');
  const disallowedTools = configureAgentPermissions(researchMode);
  const promptFingerprint = computeResearchPlanPromptFingerprint('research');
  const setPromptFingerprint = (indexMod as { setCurrentPromptFingerprint?: (fp: string) => void }).setCurrentPromptFingerprint;
  if (setPromptFingerprint) setPromptFingerprint(promptFingerprint);
  const sendHeartbeat = (indexMod as { sendHeartbeatSync?: (taskId: string, status: string, extra?: Record<string, unknown>) => void }).sendHeartbeatSync;
  if (sendHeartbeat) sendHeartbeat(taskId, statusRef.value, { prompt_fingerprint: promptFingerprint });

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
  const priorDocs = await fetchPriorKnowledge(userPrompt || currentDirectionText);
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

  const contextPrefix = buildContextPrefix(task);

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
    const dirVerif = dirDesignCtx.verification as Record<string, unknown> | undefined;
    if (dirVerif) {
      const vstatus = strVal(dirVerif.status as string);
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

  // Design doc: only include key listing + playbook. Full sections available via ./design-doc get <key>.
  let designDocSection = '';
  if (tacticalDesignDoc && Object.keys(tacticalDesignDoc).length > 0) {
    const docKeys = Array.isArray(tacticalDesignDoc._keys)
      ? tacticalDesignDoc._keys as string[]
      : Object.keys(tacticalDesignDoc).filter(k => !k.startsWith('_') && !k.startsWith('playbook/'));
    const playbookEntries = Object.entries(tacticalDesignDoc)
      .filter(([k]) => k.startsWith('playbook/'))
      .map(([k, v]) => `- **${k.replace('playbook/', '')}**: ${typeof v === 'string' ? v : JSON.stringify(v)}`)
      .join('\n');
    designDocSection = `# ARCHITECTURE (Design Document)
Available sections (use \`./design-doc get <key>\` to read): ${docKeys.join(', ')}

${playbookEntries ? `## Operational Playbook\n${playbookEntries}\n` : ''}
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
      const status = strVal(v.status as string);
      const missing = Array.isArray(v.missing) ? v.missing as string[] : [];
      const present = Array.isArray(v.present) ? v.present as string[] : [];
      const claimed = strVal(v.claimed_status as string);
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

  const researchRetryContext = buildRetryContext(task);
  const researchFirstTaskSection = task.first_task_on_repo ? FIRST_TASK_GUIDANCE : '';
  const researchEmptyRepoGuidance = buildEmptyRepoGuidance(task);
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

${codebaseSummary}${fileTreeSection}${gitHistorySection}
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

Review the DEDUP list and Project Context above. For each finding you're about to propose,
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
    — to make the result machine-checkable. To make a declared metric available, the command must
    write {metric: value} JSON to the \`$BLUEPRINT_RESULT_FILE\` path OR print a JSON object
    containing it to stdout (e.g. \`echo "{\\"accuracy\\": 0.93}" > "$BLUEPRINT_RESULT_FILE"\`); the
    criterion reads THAT value. A measured-but-missed goal is recorded as honest negative evidence
    (the dependent still runs), but a declared metric the run never produces a numeric value for
    fails loud (compute_unmeasured_criterion) — a job that ran cannot pass as a hypothesis that was
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
    be held explicitly for operator review rather than dispatching silently.

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
  "related_files": ["path/to/file.py"],
  "depends_on_indices": [],
  "prerequisite": null,
  "provides_capability": null,
  "direction_id": "d-xxx or null",
  "effort": "high|medium|low|null",
  "compute_experiment": {"command": "command run inside a clone of this repo at cwd, e.g. 'python3 -m pkg.eval --episodes 1000'", "spend_cap_usd": 5.0, "deadline_seconds": 1800, "success_criterion": {"metric": "accuracy", "operator": ">=", "threshold": 0.9}, "accelerator": "T4"},
  "smoke_sample": {"exit_code": 0, "startup_ok": true, "metric_captured": true, "timed_out": false, "error_excerpt": ""},
  "claims": ["short factual assertion backed by your investigation (omit when synthesizing)"],
  "sources": [{"type": "file|pr|log|message", "ref": "path/to/file.py or pr#123 or msg_id", "excerpt": "optional short quote"}],
  "acceptance_evidence": {"kind": "test", "descriptor": "named test exercising the new seam end-to-end", "selector": "testing/test_foo.py::test_bar"}
}]
\`\`\`

**claims** (optional): Short factual assertions that back this finding — one sentence each, verifiable (e.g. 'backend/app/task_completion.py:_handle_research_completion stores findings in task.plan_output'). Omit the field when synthesizing across many observations with no single traceable origin.

**sources** (optional, provenance only): Cite a specific origin when you have one. type 'file': a file you read, 'pr': a merged PR number, 'log': a pipeline log line, 'message': a user message. Omit the field when synthesizing. excerpt is optional — include only a directly relevant short quote.

**prerequisite** (optional, default null): Declare a FOUNDATION this finding depends on that is being (or was) built OUTSIDE this batch — an earlier or concurrently-in-flight cycle, or a named capability. Shape: \`{"kind": "task", "ref": "<task-id or finding-hash>"}\` to gate on a specific in-flight/queued task, or \`{"kind": "capability", "ref": "<capability-name>"}\` to gate on whichever task advertises that capability. For a capability ref, DRAW THE NAME from the AVAILABLE CAPABILITIES vocabulary above so it resolves to the registry's canonical string (the matcher is EXACT, never fuzzy). When it resolves to a real foundation the dependent is structurally gated (depends_on edge) so it does not run against a base missing the foundation or duplicate it; when it does not resolve the dependent enters a BLOCKED state with the unmet prerequisite recorded as a machine-resolvable need, and is resumed when the foundation appears or escalated to the operator after a timeout. Use ONLY for cross-cycle / cross-batch foundations or named capabilities — for ordering findings WITHIN this batch, use depends_on_indices instead (NOT prerequisite). Omit (or null) when there is no external foundation.

**compute_experiment** (optional, default null): Present ONLY on a compute_experiment/measurement/benchmark finding — the launch contract for the budgeted compute job. Required keys \`command\`, \`spend_cap_usd\` (positive), \`deadline_seconds\` (positive); optional \`success_criterion\` (numeric metric/operator/threshold goal) and benchmark metadata (\`machine_type\`, \`accelerator\`, \`experiment\`, \`benchmark\`, \`metric\`, \`cwd\`, \`env\`). The \`accelerator\` field (one of \`T4\`, \`L4\`, \`V100\`) is **sufficient on its own** to request a single GPU — you do NOT need to specify \`machine_type\` when requesting a single GPU by accelerator name; the system resolves the correct GPU machine type automatically. Omit entirely on non-experiment findings.

**provides_capability** (optional, default null): A short kebab-case capability name this finding's task advertises (e.g. "payment-core"), so a sibling finding (or another lane) declaring \`{"kind": "capability", "ref": "payment-core"}\` can depend on it. DRAW THE NAME from the AVAILABLE CAPABILITIES vocabulary above when one fits; propose a NEW kebab-case name (same convention) ONLY for a genuinely new capability not already listed. Omit unless another task needs to gate on this one by capability. ${CAPABILITY_DECLARATION_DOC}

**acceptance_evidence** (optional): Declare the proof that will show this finding's claim is true. Shape: \`{"kind": "test"|"behavior"|"artifact"|"visual", "descriptor": "short human-readable proof", "selector": "optional test path/selector, artifact ref, or viewport width"}\`. kind=test = a named test/selector exercising the seam end-to-end; behavior = an observable runtime behavior; artifact = an artifact a downstream task consumes; visual = a viewport screenshot of a FRONTEND/admin-ui component (descriptor = the component name, selector = the viewport width in px as a string), proven by an uploaded screenshot of that component at that width on a successful run. Omit the whole object when you are not declaring a proof — omitting it preserves today's behavior exactly. Do NOT invent other kinds.

**actionability_grade rubric** (required on every finding):
- HIGH: problem statement is clear, desired outcome is clear, related files identified, no major unknowns blocking implementation.
- MEDIUM: problem clear but solution path uncertain, OR small unknowns the implementer can resolve with brief exploration.
- LOW: vague or exploratory, or requires sub-research before code can land. Consider emitting a research sub-finding instead.

**verification_basis rubric** (OPTIONAL — defaults to pattern_inferred when omitted): how you established the underlying claim, i.e. the STRENGTH OF EVIDENCE behind it. This is ORTHOGONAL to actionability_grade — actionability_grade is about how implementable the finding is, verification_basis is about how well-evidenced the claim is. A finding can be HIGH actionability but only pattern_inferred, or LOW actionability but code_verified. Do not conflate them.
- code_verified: you read the actual source and confirmed the claim directly.
- path_verified: you confirmed the relevant file paths are present/absent via the file tree, but did not read their contents.
- pattern_inferred: structural inference from naming/architecture patterns, with no direct inspection. This is the default — use it (or omit the field) when uncertain.
- deduced: derived from adjacent evidence (e.g. another finding, a log line) without directly inspecting the subject.

**acceptance_evidence** (OPTIONAL, additive — default null): the FORWARD evidence that will prove this finding's claim true once implemented, carried onto the spawned task. This is the OPPOSITE direction from verification_basis: verification_basis is BACKWARD ('how well-evidenced is the claim I am making now'), acceptance_evidence is FORWARD ('what concrete observation will demonstrate the fix worked'). Exactly one of four kinds: (1) {"kind": "named_test", "test_path": "tests/test_x.py", "test_id": "test_fn"} — a test exercising the changed seam (test_path must name a real test file; test_id optional, or inline as path::test_fn); (2) {"kind": "observable_behavior", "description": "..."} — a substantive (≥10 char, non-placeholder) description of a behavior to exercise; (3) {"kind": "downstream_artifact", "artifact_id": "..."} — an artifact a later task consumes; (4) {"kind": "visual", "component": "<UI component under test>", "viewport_width": <px, 1-4096>, "assertion_type": "<optional, e.g. overflow|text_truncation>"} — for FRONTEND-TOUCHING work (admin-ui components), proven by an uploaded viewport screenshot of that component captured at that width on a successful run. Omit (or null) when you cannot name a concrete, non-trivial proof — never invent a placeholder/always-true declaration (those are rejected).

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
  statusRef.value = 'Running codebase research...';
  const { exitCode, allOutput, lastResultText } = await runClaudeCode(researchPrompt + researchPromptSuffix + researchFirstTaskSection + PLAYBOOK_GUIDANCE + '\nNote: If your research uncovered operational knowledge (build patterns, module relationships, common failure modes), save the most reusable findings to the playbook.\n' + `\n\n${researchFetchGuidance}${DEFAULT_TOOL_GUIDANCE}`, ws, statusRef, model, effort, disallowedTools);

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
