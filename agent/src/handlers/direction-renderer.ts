import { strVal } from '../index.js';
import {
  aggregateDirectionStats,
  buildScopeCalibrationSignal,
  parseResearchContext,
  buildFilteredFindingsSection,
} from './research-plan.js';

/**
 * Renders a single direction entry for the research prompt's DIRECTIONS section.
 * Composes stats, calibration signal, research context, history dedup, filtered
 * findings, notes, saturation warning, and in-flight task list into a compact
 * multi-line string ready for prompt assembly.
 */
export function renderDirection(
  d: Record<string, unknown>,
  i: number,
  currentDirectionId: string | undefined,
  activeTasks: Array<Record<string, unknown>>,
  queuedTasks: Array<Record<string, unknown>>,
): string {
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
  // For the CURRENT direction, skip: ## Direction Context from buildContextPrefix
  // already carries this narrative authoritatively, so re-emitting it here doubles tokens.
  const isCurrentDirection = !!currentDirectionId && d.id === currentDirectionId;
  let researchCtx = '';
  if (!isCurrentDirection && typeof d.research_context === 'string' && (d.research_context).length > 0) {
    const sections = parseResearchContext(d.research_context);
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
      ctxParts.push(`  ⚠ Context trimmed: ${sections.eviction}. Earlier findings were evicted — check git history if you suspect overlap with prior work.`);
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
    ? '\n  ⚠ SATURATED: Consider a direction_update or direction_note instead of more findings.'
    : '';

  // Active tasks for this direction (avoid duplicating in-flight work)
  let activeForDirNote = '';
  if (d.id === currentDirectionId) {
    const dirTasks = [...activeTasks, ...queuedTasks].filter(t => t['direction_id'] === d.id);
    if (dirTasks.length > 0) {
      activeForDirNote = `\n  In-flight: ${dirTasks.map(t => strVal(t['title'])).join(', ')}`;
    }
  }

  return `${i + 1}. [${strVal(d.priority, 'medium')}] ${strVal(d.text)}${pathScope} (id: ${strVal(d.id)})${statsLine}${calibrationSignal}${researchCtx}${historySection}${filteredSection}${notesSection}${saturationNote}${activeForDirNote}`;
}
