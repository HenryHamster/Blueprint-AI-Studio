import { strVal } from '../index.js';

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
  const lines = pairs.map(([a, b, s]) => `- ${a} ↔ ${b} (affinity: ${s})`);
  return `## Co-Change Affinity Hints\n\nBased on prior merged PRs, these files in your task's scope have historically changed together. Prefer to keep high-affinity pairs in the same subtask. Absence of a listed affinity is NOT evidence of independence — contract coupling (shared APIs, schemas, invariants) does not show up in co-change history, so judge splits by whether the pieces share a contract, not by this table alone.\n\n${lines.join('\n')}\n\n`;
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

  // NOTE: these metrics are PROXIES, not goals. Per-subtask merge rate is
  // blind to integration cost — N small file-disjoint subtasks can each merge
  // cleanly while breaking each other's contracts, pushing the real cost into
  // review/fix churn on the rollup. Do not derive "make subtasks smaller /
  // flatter / single-module" imperatives from this table (a prior version did,
  // and it trained the pipeline to shred coherent work). Decompose along
  // contract boundaries at the size the work actually needs.
  return `## Prior Decomposition Outcomes\n\nMetrics from recent plan decompositions in this direction (context only — these are proxies; per-subtask merge rate ignores integration cost across siblings, so do NOT shrink or flatten a decomposition just to chase it):\n${lines.join('\n')}\n\n`;
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

export function formatPastRetirementCritiques(raw: unknown): string {
  if (!Array.isArray(raw) || raw.length === 0) return '';
  const items = raw as Array<unknown>;
  const lines: string[] = ['## Lessons from past retired directions (do not repeat these patterns)'];
  let count = 0;
  for (const item of items) {
    if (!item || typeof item !== 'object') continue;
    const c = item as Record<string, unknown>;
    if (c.no_eligible_retirements) {
      lines.push('_(no eligible retired directions were recorded in a prior cycle)_');
      count++;
      continue;
    }
    const id = typeof c.direction_id === 'string' ? c.direction_id : '?';
    const what = typeof c.what_it_addressed === 'string' ? c.what_it_addressed : '';
    const pattern = typeof c.pattern_that_produced_it === 'string' ? c.pattern_that_produced_it : '';
    const lesson = typeof c.lesson === 'string' ? c.lesson : '';
    if (!what && !pattern && !lesson) continue;
    lines.push(`\n**${id}**`);
    if (what) lines.push(`  - Addressed: ${what}`);
    if (pattern) lines.push(`  - Pattern: ${pattern}`);
    if (lesson) lines.push(`  - Lesson: ${lesson}`);
    count++;
  }
  if (count === 0) return '';
  return '\n' + lines.join('\n') + '\n';
}

export function formatRetiredDirectionsSummary(raw: unknown): string {
  if (!Array.isArray(raw) || raw.length === 0) return '';
  const items = raw as Array<Record<string, unknown>>;
  const lines: string[] = ['## Recently retired directions (last 90d — avoid re-proposing overlapping work)'];
  for (const d of items) {
    const id = typeof d.id === 'string' ? d.id : '?';
    const text = typeof d.text === 'string' ? d.text : '';
    const status = typeof d.status === 'string' ? d.status : '';
    const cycles = typeof d.cycles === 'number' ? d.cycles : 0;
    const merged = typeof d.prs_merged === 'number' ? d.prs_merged : 0;
    const reason = typeof d.retirement_reason === 'string' ? d.retirement_reason
      : typeof d.auto_completed_reason === 'string' ? d.auto_completed_reason : '';
    const outcome = typeof d.retirement_outcome === 'string' ? d.retirement_outcome : '';
    const tax = typeof d.dominant_taxonomy === 'string' ? d.dominant_taxonomy : '';
    const cpm = typeof d.cost_per_merge === 'number' ? `, $${d.cost_per_merge}/merge` : '';
    const failed = Array.isArray(d.failed_approaches) ? d.failed_approaches as string[] : [];

    let line = `- **${id}** [${status}]: "${text.slice(0, 120)}" — ${cycles} cycles, ${merged} merged${cpm}`;
    if (tax) line += `, dominant: ${tax}`;
    if (reason) line += `; reason: ${reason.slice(0, 120)}`;
    if (outcome) line += `; outcome: ${outcome.slice(0, 120)}`;
    lines.push(line);
    if (failed.length > 0) {
      lines.push(`  Failed approaches: ${failed.slice(0, 3).join('; ')}`);
    }
  }
  return '\n' + lines.join('\n') + '\n';
}

/**
 * Render the `retired_direction_grounding` object an active direction carries
 * when it was created as a continuation/re-staffing of a retired one. Surfaces
 * how the prior work ended (classification + metrics) so the strategist can tell
 * an active direction picks up abandoned/landed work rather than being novel.
 * Returns '' when no usable grounding is present.
 */
export function formatRetiredDirectionGrounding(grounding: unknown): string {
  if (!grounding || typeof grounding !== 'object' || Array.isArray(grounding)) return '';
  const g = grounding as Record<string, unknown>;
  const classification = typeof g.classification === 'string' ? g.classification : '';
  const priorId = typeof g.direction_id === 'string' ? g.direction_id : '';
  const reason = typeof g.reason === 'string' ? g.reason : '';
  if (!classification && !priorId && !reason) return '';

  const metricBits: string[] = [];
  const metrics = g.metrics;
  if (metrics && typeof metrics === 'object' && !Array.isArray(metrics)) {
    for (const [k, v] of Object.entries(metrics as Record<string, unknown>)) {
      if (typeof v === 'number' || typeof v === 'boolean') metricBits.push(`${k}=${v}`);
    }
  }

  let line = 'Re-staffing of retired work';
  if (priorId) line += ` ${priorId}`;
  if (classification) line += ` [${classification}]`;
  if (metricBits.length > 0) line += ` (${metricBits.join(', ')})`;
  if (reason) line += `; prior reason: ${reason.slice(0, 200)}`;
  return line;
}

// re-export differentiationHintFor for use in research-plan.ts
export { differentiationHintFor };
