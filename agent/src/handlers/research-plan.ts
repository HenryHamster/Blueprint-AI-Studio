import fs from 'fs';
import { execFileSync } from 'child_process';
import type WebSocket from 'ws';
import {
  WORKSPACE, strVal, sanitize, sendLog, readHandlerOutput,
  persistAndPostStatus, cloneWithRetry, runClaudeCode,
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

  // Dedup/filter losses across last 3 cycles — lets the agent see how many
  // proposed findings were filtered and by which mechanism, so it can steer
  // toward more novel titles, different files, or new approaches.
  const filterKeys = ['hash_dedup', 'word_overlap_dedup', 'file_overlap_dedup', 'type_filtered', 'empty_prompt'] as const;
  const filterAgg: Record<string, number> = {};
  for (const h of recent) {
    const fb = h.filter_breakdown as Record<string, number> | undefined;
    if (!fb || typeof fb !== 'object') continue;
    for (const k of filterKeys) {
      const v = (fb[k] as number) || 0;
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
    const sb = h.scope_breakdown as Record<string, {created?: number; merged?: number}> | undefined;
    if (!sb || typeof sb !== 'object') continue;
    for (const [k, v] of Object.entries(sb)) {
      if (!v || typeof v !== 'object') continue;
      if (!typeAgg[k]) typeAgg[k] = { created: 0, merged: 0 };
      typeAgg[k].created += (v.created as number) || 0;
      typeAgg[k].merged += (v.merged as number) || 0;
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
  const aggScope: Record<string, ScopeAgg> = {};
  for (const h of recent) {
    const sb = h.scope_breakdown as Record<string, Record<string, number>> | undefined;
    if (!sb || typeof sb !== 'object') continue;
    for (const [scope, counts] of Object.entries(sb)) {
      if (!aggScope[scope]) aggScope[scope] = {
        created: 0, merged: 0, cplxMSum: 0, cplxMN: 0, cplxFSum: 0, cplxFN: 0, expSum: 0, expN: 0, calIn: 0, calOut: 0,
      };
      const a = aggScope[scope];
      a.created += (counts.created as number) || 0;
      a.merged += (counts.merged as number) || 0;
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

function differentiationHintFor(reason: string): string {
  const r = reason.toLowerCase();
  if (r.includes('hash match')) {
    return 'fundamentally different scope or approach needed';
  }
  if (r.includes('word overlap')) {
    return 'target different files or subsystem, or frame the problem from a different angle';
  }
  if (r.includes('file overlap')) {
    return "address a different aspect of these files or propose complementary work that doesn't overlap";
  }
  return '';
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

export function formatPreviousDecisionOutcomes(raw: unknown): string {
  if (!raw || typeof raw !== 'object') return '';
  const o = raw as Record<string, unknown>;
  const created = Array.isArray(o.created) ? o.created as Array<Record<string, unknown>> : [];
  const retired = Array.isArray(o.retired) ? o.retired as Array<Record<string, unknown>> : [];
  if (created.length === 0 && retired.length === 0) return '';

  const lines: string[] = ['## Decision Feedback'];
  const cycleId = strVal(o.cycle_id);
  const completedAt = strVal(o.phase2_completed_at);
  const headerParts: string[] = [];
  if (cycleId) headerParts.push(`cycle ${cycleId}`);
  if (completedAt) headerParts.push(`completed ${completedAt}`);
  lines.push(
    `Outcomes of your prior strategic cycle's decisions${headerParts.length ? ` (${headerParts.join(', ')})` : ''}.`
  );
  const summaryText = strVal(o.summary_text);
  if (summaryText) lines.push(`Prior summary: ${summaryText.slice(0, 500)}`);

  const fmtEntry = (d: Record<string, unknown>): string => {
    const id = strVal(d.direction_id);
    const status = strVal(d.status, 'unknown');
    const text = strVal(d.text).slice(0, 120);
    const textTag = text ? ` "${text}"` : '';
    if (status === 'missing') {
      return `- ${id}${textTag}: fully removed from direction list`;
    }
    const cycles = typeof d.cycles_active === 'number' ? d.cycles_active : 0;
    const merged = typeof d.prs_merged === 'number' ? d.prs_merged : 0;
    const quality = typeof d.quality_score === 'number' ? d.quality_score : null;
    const qualityStr = quality !== null ? `, quality ${quality}` : '';
    const pauseFlags: string[] = [];
    if (d.auto_paused === true) pauseFlags.push('AUTO_PAUSED');
    if (d.user_paused === true) pauseFlags.push('USER_PAUSED');
    const pauseStr = pauseFlags.length ? ` [${pauseFlags.join(', ')}]` : '';
    return `- ${id}${textTag}: status=${status}, ${cycles} cycles active, ${merged} PRs merged${qualityStr}${pauseStr}`;
  };

  if (created.length > 0) {
    lines.push('\nDirections you CREATED in the prior cycle:');
    for (const d of created) lines.push(fmtEntry(d));
  }
  if (retired.length > 0) {
    lines.push('\nDirections you RETIRED in the prior cycle:');
    for (const d of retired) lines.push(fmtEntry(d));
  }
  lines.push(
    '\nReflect: did the directions you created produce merges, or stall? Were any auto-paused? Use this feedback before creating similar directions again.'
  );
  return '\n' + lines.join('\n') + '\n';
}

// Portfolio-level view computed from per-direction data — surfaces imbalances
// (category skew, system coverage gaps, concentration, age) as explicit
// decision inputs for the strategic thinker.
export function buildPortfolioComposition(
  activeDirections: Array<Record<string, unknown>>,
  systems: Array<Record<string, unknown>>,
  activeByScope: Record<string, number> | undefined,
): string {
  const total = activeDirections.length;
  if (total === 0) return 'No active directions — portfolio is empty.';

  const catCounts: Record<string, number> = { product: 0, structural: 0, mixed: 0, uncategorized: 0 };
  const systemCounts: Record<string, number> = {};
  const ageFlags: string[] = [];
  for (const d of activeDirections) {
    const rawCat = typeof d.category === 'string' ? (d.category as string) : '';
    const cat = ['product', 'structural', 'mixed'].includes(rawCat) ? rawCat : 'uncategorized';
    catCounts[cat] = (catCounts[cat] || 0) + 1;
    const sys = typeof d.system_id === 'string' ? (d.system_id as string) : '';
    if (sys) systemCounts[sys] = (systemCounts[sys] || 0) + 1;
    const cycles = typeof d.total_cycles === 'number' ? (d.total_cycles as number) : 0;
    if (cycles >= 10) {
      const id = typeof d.id === 'string' ? (d.id as string) : '?';
      const text = typeof d.text === 'string' ? (d.text as string).slice(0, 60) : '';
      ageFlags.push(`${id} (${cycles} cycles): ${text}`);
    }
  }

  const pct = (n: number) => total > 0 ? Math.round((n / total) * 100) : 0;
  const productPct = pct(catCounts.product);
  const lines: string[] = [];
  const catParts = (['product', 'structural', 'mixed', 'uncategorized'] as const)
    .filter(c => catCounts[c] > 0)
    .map(c => `${c}=${catCounts[c]} (${pct(catCounts[c])}%)`);
  const productFlag = productPct < 30 ? ' [LOW PRODUCT RATIO — <30%]' : '';
  lines.push(`Category mix: ${catParts.join(', ')}${productFlag}`);

  const mappedSystems = new Set(Object.keys(systemCounts));
  const allSystems = systems.filter(s => typeof s.id === 'string').map(s => ({
    id: s.id as string,
    name: typeof s.name === 'string' ? (s.name as string) : (s.id as string),
  }));
  const uncovered = allSystems.filter(s => !mappedSystems.has(s.id));
  if (allSystems.length > 0) {
    lines.push(`System coverage: ${mappedSystems.size}/${allSystems.length} vision systems have directions`
      + (uncovered.length > 0 ? ` — gaps: ${uncovered.map(s => `${s.name} (${s.id})`).join(', ')}` : ''));
  }

  const concentrated = Object.entries(systemCounts).filter(([, n]) => n >= 3);
  if (concentrated.length > 0) {
    const parts = concentrated.map(([sid, n]) => `${sid}=${n}`);
    lines.push(`CONCENTRATION RISK: 3+ directions on same system: ${parts.join(', ')}`);
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
            const merged = sections.established.filter(s => /\(merged\)/i.test(s));
            const failed = sections.established.filter(s => /\(failed[:)]/i.test(s));
            const breakdown: string[] = [];
            if (merged.length > 0) breakdown.push(`${merged.length} merged`);
            if (failed.length > 0) breakdown.push(`${failed.length} failed`);
            const breakdownStr = breakdown.length > 0 ? ` (${breakdown.join(', ')})` : '';
            ctxParts.push(`  ${sections.established.length} findings already completed for this direction${breakdownStr} (do not re-propose similar scope work; prefer approaches that shipped over ones that failed).`);
            if (failed.length > 0) {
              ctxParts.push(`  Failed findings (do not repeat):\n${failed.slice(0, 5).join('\n')}`);
            }
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

        // Filter decisions from the dedup pipeline: specific titles + reasons
        // for findings rejected in recent cycles. Lets the agent propose a
        // different angle instead of repeating work the backend will reject.
        const filteredSection = buildFilteredFindingsSection(history);

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

        return `${i + 1}. [${strVal(d.priority, 'medium')}] ${strVal(d.text)}${pathScope} (id: ${strVal(d.id)})${statsLine}${calibrationSignal}${researchCtx}${historySection}${filteredSection}${notesSection}${saturationNote}${activeForDirNote}`;
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

  // Pipeline-wide scope conversion rates — rendered from pipeline_health.scope_conversion_rates
  // so measurement directions can read the number straight from the prompt instead
  // of hitting an admin endpoint they aren't authenticated for.
  let pipelineScopeRatesSection = '';
  const pipelineHealthForScope = projectCtx?.pipeline_health as Record<string, unknown> | null | undefined;
  const scopeConversionRates = pipelineHealthForScope?.scope_conversion_rates as
    | Record<string, { current_rate: number | null; prev_rate: number | null; delta: number | null; trend: string | null }>
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
        const finishedSince = typeof sinceLast.finished === 'number' ? sinceLast.finished
          : typeof sinceLast.created === 'number' ? sinceLast.created : 0;
        const spawnedSince = typeof sinceLast.spawned === 'number' ? sinceLast.spawned : null;
        const rateSince = typeof sinceLast.merge_rate === 'number' ? `${Math.round((sinceLast.merge_rate as number) * 100)}%` : 'n/a';
        const spawnNote = spawnedSince != null && spawnedSince !== finishedSince
          ? ` (${spawnedSince} spawned, some still in flight)`
          : '';
        clockHeader = `\n# Clock\n`
          + `${hoursSinceLast}h since your last strategic cycle. In that window: `
          + `${mergedSince} of ${finishedSince} finished tasks merged (${rateSince})${spawnNote}.\n`
          + `If that's a low-value ${hoursSinceLast}h relative to the ambition you set last time, that's information — ask why before doubling down on the same plays.\n`;
      }

      // Active in-flight counts per scope: preserves visibility of
      // still-running work now that merge rates are finished-only.
      const activeByScope = recentInterventions.active_by_scope as Record<string, number> | undefined;
      if (activeByScope) {
        const parts: string[] = [];
        for (const s of ['small', 'medium', 'large', 'unknown']) {
          const n = activeByScope[s];
          if (typeof n === 'number' && n > 0) parts.push(`${s}=${n}`);
        }
        if (parts.length > 0) {
          clockHeader += `Currently in flight: ${parts.join(', ')} (not in the merge-rate numbers above — those only count finished tasks).\n`;
        }
      }

      if (merges.length > 0 || rollingRates || perDirectionDelta.length > 0) {
        const lines: string[] = ['\n# Recent Work & Rolling Outcomes'];
        lines.push('IMPORTANT — read this carefully before acting:');
        lines.push('- Rate numerator/denominator semantics: merge_rate = merged/finished, where "finished" means the task reached a terminal state inside the window. Tasks spawned mid-window that have not finished yet do NOT count in the denominator — so short-window rates are not biased downward by in-flight slower-scope tasks.');
        lines.push('- "spawned" = tasks created in the window (throughput signal). "finished" = reached terminal state in the window (quality denominator). Divergence between the two is normal for slow scopes.');
        lines.push('- Correlation is NOT causation. Rate swings can reflect task difficulty shifts, account health, rate limits, CI outages, or small-sample noise.');
        lines.push('- Weekly buckets were replaced with rolling windows (last_24h / last_3d / last_7d / since_last_strategic) because weekly is too coarse for this pipeline\'s velocity.');
        lines.push('- Only act on patterns visible across 2+ of the overlapping windows. A spike in last_24h that does not show in last_3d is probably noise.');
        lines.push('- Prefer "investigate whether X helps" over "revert X".');

        if (rollingRates) {
          lines.push('\nRolling merge-rate windows (merged / finished):');
          const order = ['since_last_strategic', 'last_24h', 'last_3d', 'last_7d'];
          for (const w of order) {
            const r = rollingRates[w];
            if (!r) continue;
            const finished = typeof r.finished === 'number' ? r.finished
              : typeof r.created === 'number' ? r.created : 0;
            const merged = typeof r.merged === 'number' ? r.merged : 0;
            const spawned = typeof r.spawned === 'number' ? r.spawned : null;
            const rate = typeof r.merge_rate === 'number' ? `${Math.round((r.merge_rate as number) * 100)}%` : 'n/a';
            const spawnTail = spawned != null && spawned !== finished ? `, ${spawned} spawned` : '';
            lines.push(`  ${w}: ${merged}/${finished} merged (${rate}${spawnTail})`);
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
          const dStatus = strVal(d.status, 'active');
          let statusNote = '';
          if (dStatus !== 'active') {
            if (dStatus === 'paused') {
              const tag = d.auto_paused === true ? 'AUTO_PAUSED' : 'USER_PAUSED';
              const reason = strVal(d.paused_reason).slice(0, 80);
              statusNote = reason ? ` [${tag}: ${reason}]` : ` [${tag}]`;
            } else {
              statusNote = ` [${dStatus.toUpperCase()}]`;
            }
          }

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

          return `${i + 1}. [${strVal(d.priority, 'medium')}]${catNote} ${strVal(d.text)} (id: ${strVal(d.id)})${statusNote}${satNote}${sysNote}\n   Stats: ${parts.join(', ')}${ctxSection}${findingsSection}${failuresSection}`;
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
# Strategic review tool — updates problem_model, running_update, directions,
# messages via API. Reads ORCHESTRATOR_URL and AGENT_API_TOKEN from env.
OP="\${1:?Usage: strategic set-problem-model|set-running-update|reflect|pending-confirm|set-vision|create-direction|update-direction|retire-direction|create-message|resolve-message|spawn-task <json-or-file>}"
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
  set-problem-model)    _call "set_problem_model" ;;
  set-running-update)
    # DATA is a path to the markdown file written by the Thinker. Wrap
    # the file contents as {markdown: "..."} before the usual body build.
    FILE="\$DATA"
    [ -f "\$FILE" ] || { echo "[ERROR] set-running-update: file not found: \$FILE" >&2; exit 1; }
    DATA=\$(jq -Rs '{markdown: .}' < "\$FILE")
    _call "set_running_update"
    ;;
  reflect)              _call "reflect" ;;
  pending-confirm)      _call "pending_confirm" ;;
  set-vision)           _call "set_vision" ;;
  create-direction)     _call "create_direction" ;;
  update-direction)     _call "update_direction" ;;
  retire-direction)     _call "retire_direction" ;;
  create-message)       _call "create_message" ;;
  resolve-message)      _call "resolve_message" ;;
  spawn-task)           _call "spawn_task" ;;
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
    const pausedDirections = directionsSummary.filter(d => strVal(d.status) === 'paused');
    const autoPausedCount = pausedDirections.filter(d => d.auto_paused === true).length;
    const userPausedCount = pausedDirections.length - autoPausedCount;
    const saturatedCount = activeDirections.filter(d => d.possibly_saturated === true).length;
    const productCount = activeDirections.filter(d => strVal(d.category) === 'product').length;
    const availableSlots = maxActiveDirections - activeCount;
    const gaps: string[] = [];
    if (productCount === 0) gaps.push('NO_PRODUCT_DIRECTIONS');
    if (saturatedCount === activeCount && activeCount > 0) gaps.push('ALL_SATURATED');
    if (activeCount < 3) gaps.push('BELOW_MINIMUM');
    const pausedNote = pausedDirections.length > 0
      ? ` Paused: ${pausedDirections.length} (${autoPausedCount} auto, ${userPausedCount} user) — listed below but not consuming active slots.`
      : '';
    const dirHealthSummary = [
      `Active: ${activeCount}/${maxActiveDirections} slots used (${availableSlots} available). Saturated: ${saturatedCount}/${activeCount}. Product directions: ${productCount}.${pausedNote}`,
      gaps.length > 0 ? `GAPS DETECTED: ${gaps.join(', ')}` : 'No critical gaps detected.',
    ].join('\n');

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
      ? strVal((designDoc as Record<string, unknown>)['notes/strategic-context'])
      : '';

    // ── Problem model + running update + self-reflection rendering ──
    // These are the new strategic substrate — persistent durable state the
    // agent maintains across cycles. They replace vision as source of truth.
    const problemModel = payload.problem_model as Record<string, unknown> | undefined;
    const problemModelJson = problemModel && Object.keys(problemModel).length > 0
      ? JSON.stringify(problemModel, null, 2)
      : '';

    const runningUpdateDoc = typeof payload.running_update === 'string'
      ? payload.running_update as string
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
        const ts = Date.parse(iso);
        if (Number.isNaN(ts)) return '';
        const days = Math.max(0, Math.floor((Date.now() - ts) / 86400000));
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

    const problemModelSection = problemModelJson
      ? `## Your current understanding (problem_model)\n\`\`\`json\n${problemModelJson}\n\`\`\``
      : `## Your current understanding (problem_model)\n_(empty — this is the first cycle. Bootstrap from the other sections below.)_`;

    const runningUpdateSection = `## Running update (living document — rewrite, don't append)\n${renderRunningUpdateDoc(runningUpdateDoc)}\n\n_This is the document the user currently sees. Refine it this cycle — remove stale content, integrate what's new, keep it coherent and short. It's a current snapshot, not a log._`;

    const reflectionSection = `## What you previously said was hard\n${renderReflectionList(selfReflectionEntries)}`;

    const selfImprovementBlock = isSelfRepo ? `

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
    const thinkerPrompt = `${contextPrefix}You own this project's direction. Not as a task runner, not as a scheduler — as the mind responsible for where it goes.

Each cycle you look at what's happening and decide whether it's still on the right track. If it is, keep going. If it isn't, change course. You're allowed to be wrong; you're not allowed to defer. The problem_model below is your working understanding of the project — nothing else has more authority. Update it, defend it, rewrite it when you see a better shape. The user hired you for judgment, not for audit trails or incremental polish.

# Project: ${projectName}
${clockHeader}
Self-repo: ${isSelfRepo ? 'true' : 'false'}
${userStateLine}
${strategicContext ? `\n## Running Context (from prior cycles)\n${strategicContext}\n` : ''}
${problemModelSection}

${reflectionSection}

${runningUpdateSection}

## Architecture (design document — current state)
${designDocJson || 'No design document yet.'}

${visionJson ? `## Vision (legacy artifact — use problem_model instead; retained for bootstrap reference only)\n${visionJson}\n` : ''}
${outcomeSection}${formatPreviousDecisionOutcomes(payload.previous_decision_outcomes)}
## Current research directions
${dirSummaryText}

## Direction Health
${dirHealthSummary}
${buildUserPausedSection(directionsSummary)}${buildRejectedProposalsSection(Array.isArray(payload.rejected_proposals) ? payload.rejected_proposals as Array<Record<string, unknown>> : [])}
## Portfolio Composition
${portfolioCompositionText}
${recentWorkSection}${feedbackSection}${threadSection}
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

${selfImprovementBlock}

# Bootstrap (when the problem_model is empty)

Populate intent from: the existing vision field (if any), the conversation_thread (the user's own words), and rejected_proposals (they tell you what the user does NOT want). Populate capabilities from: what shipped work and active directions observably demonstrate the project does. Open initial gaps for: anything in conversation_thread the user explicitly asked for that isn't yet a capability; anything in the vision's stated direction not reflected in shipped work. Bootstrapped models are imperfect by design — subsequent cycles refine. Don't fail to populate because you're uncertain.

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
Directions to retire (one-sentence reason each) and to create (name the gap_id each closes + a success condition that would retire it). Parallel moves are fine and encouraged when non-clashing.

## 4. Running update document (for the user)
Write the full refined running_update as markdown to \`/tmp/running_update_${taskId}.md\` (also show it as a code block in your analysis for auditability — the file is the authoritative handoff to the Executor). This is the document the user sees as "project current state" — rewrite, don't append. Under 400 words. Follow the section guidance above.

## 5. Self-reflection (optional)
Only if something this cycle was hard in a way worth remembering. Fields: what_was_hard, missing_context, proposed_self_improvement. Empty is fine. Don't fill with platitudes.

## 6. Problem model (full replacement)
Write the full updated problem_model as JSON to \`/tmp/problem_model_${taskId}.json\` (use a code block in your analysis for auditability AND write the file — the file is the authoritative handoff to the Executor). This is the entire model, not a delta — you're replacing the prior state with a new consistent one. The Executor reads this file verbatim; if it's missing the cycle fails loudly rather than fabricating.

# Problem model shape (quick reference)

- intent: {purpose, current_focus, source?}
- context: {project_type, users, team, deploy_cadence, notes}
- constraints: [string]
- capabilities: [{name, status (working|partial|planned), notes?, source?}]
- gaps: [{id, name, description, evidence, size, priority, status, tag?, source?, active_moves, past_moves, history}]
- friction: [{id, name, description, seen_in, status, source?, promoted_to_gap_id?}]

Source = {type (user_message|friction_promotion|agent_observation|self_reflection|shipped_change|direction_outcome), message_id?, excerpt?, pr_number?, direction_id?, friction_id?}

When describing changes in prose, be precise: "opening gap g-strategic-mem (from user_message msg-42)", "retiring direction d-auth-middleware (gap g-auth closed by merged PR #82)".

Before finishing, write /tmp/research_progress_${taskId}.md — a concise markdown summary (≤3000 chars) capturing the model changes proposed, gaps opening/closing, and moves. This is a breadcrumb for retry agents if this task fails — lets a retry pick up from your analysis rather than redoing it from scratch.`;

    statusRef.value = 'Running strategic analysis (pass 1: thinking)...';
    const thinkerResult = await runClaudeCode(thinkerPrompt, ws, statusRef, model, effort, disallowedTools);
    const rawAnalysis = thinkerResult.lastResultText || thinkerResult.allOutput.slice(-80_000);
    const analysisText = rawAnalysis;

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
./strategic create-direction '{"text": "outcome statement", "priority": "high|medium|low", "category": "product|structural", "gap_id": "g-xxx", "rationale": "...", "conflicts_with": ["d-xxx"]}'
./strategic update-direction '{"direction_id": "d-xxx", "changes": {"priority": "...", "text": "...", "conflicts_with": [...]}, "rationale": "..."}'
./strategic retire-direction '{"direction_id": "d-xxx", "rationale": "..."}'

# Spawn a targeted investigation task (research or plan only) when something
# from the Recent Work section needs investigation NOW instead of waiting for
# the next autonomous cycle.
./strategic spawn-task '{"type": "research", "title": "...", "prompt": "Full investigation prompt...", "direction_id": "d-xxx"}'

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

1. **Problem model (REQUIRED).** Primary path: pass \`"$(cat /tmp/problem_model_${taskId}.json)"\` to \`./strategic set-problem-model\`. Don't summarize or re-emit — read-and-pass.

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

4. **Self-reflection (optional).** If Section 5 has non-empty content, emit via \`./strategic reflect\`. If the Thinker wrote nothing (or wrote "none" / "N/A" / empty), SKIP — do not fabricate.

5. **Pending confirmations (optional).** For each external blocker the Thinker identified in Section 4's blockers field (credentials, access, major pivots), emit one \`./strategic pending-confirm\`. Do NOT create these for coding questions.

6. **Messages.** Resolve any user messages the analysis addressed: \`./messages resolve <id> 'Answered — [summary]'\`. Check the conversation for your prior QUESTION messages — if a user reply answers one, resolve it. Do not post a cycle-summary message — the running_update is the voice now.

7. **Guardrails.** Ensure at least 3 active directions remain after retires.

8. Done. Do not write anything else.`;

    statusRef.value = 'Running strategic execution (pass 2: applying decisions)...';
    let { exitCode, allOutput, lastResultText } = await runClaudeCode(executorPrompt, ws, statusRef, model, effort, disallowedTools);

    // Post-check: verify the executor actually called ./strategic
    // set-problem-model AND ./strategic set-running-update. These are
    // the new canonical required calls — problem_model is the strategic
    // source of truth, running_update is the voice to the user. If either
    // is missing the cycle is incomplete; retry once with a focused prompt.
    try {
      const problemModelSetInOutput = /"op"\s*:\s*"set_problem_model"/.test(allOutput);
      const runningUpdateSetInOutput = /"op"\s*:\s*"set_running_update"/.test(allOutput);

      if (exitCode === 0 && (!problemModelSetInOutput || !runningUpdateSetInOutput)) {
        const missing: string[] = [];
        if (!problemModelSetInOutput) missing.push('./strategic set-problem-model');
        if (!runningUpdateSetInOutput) missing.push('./strategic set-running-update');
        sendLog(ws, `\n[WARNING] Strategic executor completed but required calls missing: ${missing.join(', ')}. Retrying...\n`);
        const retryInstructions = [
          'Your previous attempt did not call one or more REQUIRED commands.',
          '',
          `Missing: ${missing.join(', ')}`,
          '',
          'Run these NOW using data from the analysis:',
          !problemModelSetInOutput ? `- ./strategic set-problem-model "$(cat /tmp/problem_model_${taskId}.json)"  (if file missing: construct from analysis grounded in the managed project — NEVER self-describe)` : '',
          !runningUpdateSetInOutput ? `- ./strategic set-running-update /tmp/running_update_${taskId}.md  (if file missing: write a single clean markdown doc to that path first — describe the managed project's current state, NEVER self-describe)` : '',
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
      sendLog(ws, `[WARNING] Could not verify strategic required calls: ${e}\n`);
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
