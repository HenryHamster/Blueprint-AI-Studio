// Shared helpers for the generalized task-outcome breakdown that the backend
// now emits alongside the legacy PR-merge metrics (see
// backend/app/direction_metrics.py and task_outcomes.py). Every direction
// surface — portfolio cards, catch-up direction rows, and the directions panel
// cards — renders the same outcome-kind vocabulary, so the labels, ordering,
// and summarization live here rather than being re-derived per component.

export interface OutcomeKindCounts {
  success: number;
  neutral: number;
  failure: number;
}

// Keyed by the backend outcome-kind constant (task_outcomes.py KIND_*).
export type OutcomeKindBreakdown = Record<string, OutcomeKindCounts>;

// Human-facing short labels, mirroring the backend KIND_* values. `code_merge`
// is the legacy code-to-PR lane; the rest are the generalized lanes.
export const OUTCOME_KIND_LABELS: Record<string, string> = {
  code_merge: 'code',
  research_quality: 'research',
  audit_findings: 'audit',
  memo_artifact: 'memo',
  verification_verdict: 'verify',
  read_only_unclassified: 'read-only',
  unknown: 'other',
};

// Stable display order so chips don't reshuffle between renders. Unknown keys
// sort last (after this list) but are still rendered.
const OUTCOME_KIND_ORDER = [
  'code_merge',
  'research_quality',
  'audit_findings',
  'memo_artifact',
  'verification_verdict',
  'read_only_unclassified',
  'unknown',
];

export function outcomeKindLabel(kind: string): string {
  return OUTCOME_KIND_LABELS[kind] ?? kind;
}

function normalizeCounts(counts: Partial<OutcomeKindCounts> | undefined): OutcomeKindCounts {
  return {
    success: Math.max(0, Math.trunc(counts?.success ?? 0)),
    neutral: Math.max(0, Math.trunc(counts?.neutral ?? 0)),
    failure: Math.max(0, Math.trunc(counts?.failure ?? 0)),
  };
}

export interface OutcomeKindSummaryRow extends OutcomeKindCounts {
  kind: string;
  label: string;
  terminal: number;
}

/**
 * Flatten a breakdown into a sorted, display-ready list. Kinds with no terminal
 * outcomes (success+neutral+failure === 0) are dropped so empty lanes never
 * render a chip. Returns [] for null/undefined/empty input — callers should
 * treat an empty result as "nothing to surface beyond PR metrics".
 */
export function summarizeOutcomeKinds(
  breakdown: OutcomeKindBreakdown | null | undefined,
): OutcomeKindSummaryRow[] {
  if (!breakdown) return [];
  const rows: OutcomeKindSummaryRow[] = [];
  for (const [kind, rawCounts] of Object.entries(breakdown)) {
    const counts = normalizeCounts(rawCounts);
    const terminal = counts.success + counts.neutral + counts.failure;
    if (terminal === 0) continue;
    rows.push({ kind, label: outcomeKindLabel(kind), terminal, ...counts });
  }
  rows.sort((a, b) => {
    const ai = OUTCOME_KIND_ORDER.indexOf(a.kind);
    const bi = OUTCOME_KIND_ORDER.indexOf(b.kind);
    const aRank = ai === -1 ? OUTCOME_KIND_ORDER.length : ai;
    const bRank = bi === -1 ? OUTCOME_KIND_ORDER.length : bi;
    if (aRank !== bRank) return aRank - bRank;
    return b.terminal - a.terminal;
  });
  return rows;
}

/**
 * True when the breakdown carries any generalized (non-code-merge) lane with
 * terminal outcomes. This is the honesty gate: when only `code_merge` is
 * present, the generalized success count is just the PR-merge count, so we keep
 * the UI showing only the existing PR metrics rather than double-labeling them.
 */
export function hasNonCodeMergeOutcomes(
  breakdown: OutcomeKindBreakdown | null | undefined,
): boolean {
  return summarizeOutcomeKinds(breakdown).some(row => row.kind !== 'code_merge');
}

/** Merge several per-direction breakdowns into one aggregate breakdown. */
export function mergeOutcomeKindBreakdowns(
  breakdowns: Array<OutcomeKindBreakdown | null | undefined>,
): OutcomeKindBreakdown {
  const merged: OutcomeKindBreakdown = {};
  for (const breakdown of breakdowns) {
    if (!breakdown) continue;
    for (const [kind, rawCounts] of Object.entries(breakdown)) {
      const counts = normalizeCounts(rawCounts);
      const bucket = merged[kind] ?? { success: 0, neutral: 0, failure: 0 };
      bucket.success += counts.success;
      bucket.neutral += counts.neutral;
      bucket.failure += counts.failure;
      merged[kind] = bucket;
    }
  }
  return merged;
}

/** Tailwind classes for a health-rate value (0–1), matching existing thresholds. */
export function healthRateColorClass(rate: number | null | undefined): string {
  if (rate == null) return 'text-neutral-500';
  if (rate >= 0.6) return 'text-emerald-400';
  if (rate >= 0.3) return 'text-amber-400';
  return 'text-red-400';
}
