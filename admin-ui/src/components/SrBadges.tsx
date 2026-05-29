import { AlertTriangle } from 'lucide-react';
import type { Task } from '../types/task';

interface SelfReviewCheckResult {
  passed: boolean;
  issue?: string;
}

interface SelfReviewOutcome {
  verdict?: string;
  fixes_applied?: boolean;
  check_results?: Record<string, SelfReviewCheckResult>;
  blind_spots?: string[];
  skip_reason?: string;
  _filtered_blind_spots_count?: number;
}

// Human-readable labels for disallowed blind-spot categories.
// Cross-reference: backend/app/sr_blind_spots.py DISALLOWED_BLIND_SPOT_EXAMPLES
const DISALLOWED_BLIND_SPOT_LABELS = [
  'full backend test suite',
  'docker pytest',
  'viewport tests',
];

interface SrBadgeState {
  label: string;
  tooltip: string;
  variant: 'pass' | 'pass-fixed' | 'fail' | 'miss' | 'skip';
}

const SR_SKIP_REASON_LABELS: Record<string, string> = {
  diff_too_small: 'Diff too small',
  json_parse_failed: 'JSON parsing failed',
  json_missing: 'JSON missing',
  reviewer_session_failed: 'Reviewer session failed',
  reviewer_no_findings_parse: 'Reviewer findings parse failed',
  auto_remediation: 'Auto-remediation active',
  no_baseline: 'No baseline available',
};

function getSrSkipReason(outcome: SelfReviewOutcome): string | null {
  if (outcome.skip_reason) return outcome.skip_reason;

  const blindSpots = outcome.blind_spots ?? [];
  for (const spot of blindSpots) {
    if (spot.includes('self-review skipped')) return 'auto_remediation';
    if (spot.includes('self-review JSON parsing failed')) return 'json_parse_failed';
  }
  return null;
}

function getSrBadgeState(task: Task): SrBadgeState | null {
  const ctx = task.context as Record<string, unknown> | undefined;
  const outcome = ctx?._self_review_outcome as SelfReviewOutcome | undefined;
  if (!outcome) return null;

  const verdict = outcome.verdict ?? 'unknown';
  const checkResults = outcome.check_results ?? {};
  const isSoftFallback = verdict === 'unknown' && Object.keys(checkResults).length === 0;

  if (isSoftFallback) {
    const skipReason = getSrSkipReason(outcome);
    if (skipReason && skipReason !== 'diff_too_small') {
      const label = SR_SKIP_REASON_LABELS[skipReason] ?? skipReason;
      return {
        variant: 'skip',
        label: 'SR skipped',
        tooltip: `Self-review skipped — ${label}`,
      };
    }
    return null;
  }

  const humanCategories = ctx?._first_attempt_rejection_categories as Record<string, unknown> | undefined;
  const failedCategories = Object.entries(checkResults).filter(([, r]) => !(r as SelfReviewCheckResult).passed);

  if (humanCategories && Object.keys(humanCategories).length > 0) {
    const allCats = Array.from(new Set([...Object.keys(checkResults), ...Object.keys(humanCategories)]));
    const missedCount = allCats.filter(cat => cat in humanCategories && !(checkResults[cat] && !(checkResults[cat] as SelfReviewCheckResult).passed)).length;
    if (missedCount > 0) {
      return {
        variant: 'miss',
        label: `SR miss ${missedCount}`,
        tooltip: `${missedCount} human-review categor${missedCount !== 1 ? 'ies' : 'y'} not caught by self-review`,
      };
    }
  }

  if (verdict === 'fail') {
    const n = failedCategories.length;
    return {
      variant: 'fail',
      label: 'SR ✗',
      tooltip: `Self-review verdict: fail${n > 0 ? ` (${n} failed check${n !== 1 ? 's' : ''})` : ''}`,
    };
  }

  if (verdict === 'pass') {
    if (outcome.fixes_applied) {
      const n = failedCategories.length;
      return {
        variant: 'pass-fixed',
        label: n > 0 ? `SR ✓ +${n}` : 'SR ✓ +',
        tooltip: `Self-review passed; agent caught and applied fixes${n > 0 ? ` (${n} flagged check${n !== 1 ? 's' : ''})` : ''}`,
      };
    }
    return {
      variant: 'pass',
      label: 'SR ✓',
      tooltip: 'Self-review passed with no issues caught',
    };
  }

  return null;
}

export function SrBadge({ task, onClick }: { task: Task; onClick?: () => void }) {
  const state = getSrBadgeState(task);
  if (!state) return null;

  const colorClass =
    state.variant === 'pass' ? 'bg-emerald-500/10 border-emerald-500/20 text-emerald-400' :
    state.variant === 'pass-fixed' ? 'bg-teal-500/10 border-teal-500/20 text-teal-400' :
    state.variant === 'fail' ? 'bg-red-500/10 border-red-500/20 text-red-400' :
    state.variant === 'skip' ? 'bg-slate-500/10 border-slate-500/20 text-slate-400' :
    'bg-amber-500/10 border-amber-500/20 text-amber-400';

  if (onClick) {
    return (
      <button
        type="button"
        title={state.tooltip}
        onClick={(e) => { e.stopPropagation(); onClick(); }}
        className={`inline-flex items-center text-[10px] font-medium border px-1.5 py-0.5 rounded min-h-[44px] min-w-[44px] sm:min-h-auto sm:min-w-auto ${colorClass} hover:opacity-80 transition-opacity`}
      >
        {state.label}
      </button>
    );
  }

  return (
    <span
      title={state.tooltip}
      className={`inline-flex items-center text-[10px] font-medium border px-1.5 py-0.5 rounded ${colorClass}`}
    >
      {state.label}
    </span>
  );
}

export function SrDisallowedBadge({ task }: { task: Task }) {
  const ctx = task.context as Record<string, unknown> | undefined;
  const outcome = ctx?._self_review_outcome as SelfReviewOutcome | undefined;
  const count = outcome?._filtered_blind_spots_count ?? 0;
  if (count === 0) return null;

  const tooltip = `Agent emitted ${count} disallowed blind-spot pattern${count !== 1 ? 's' : ''} (filtered before aggregation)\nDisallowed categories: ${DISALLOWED_BLIND_SPOT_LABELS.join(', ')}`;
  return (
    <span
      title={tooltip}
      className="inline-flex items-center gap-0.5 text-[10px] font-medium border px-1.5 py-0.5 rounded bg-amber-500/10 border-amber-500/20 text-amber-400"
    >
      <AlertTriangle className="w-3 h-3 shrink-0" />
      {count} disallowed
    </span>
  );
}

interface SrDivergenceState {
  variant: 'missed' | 'over-flagged';
  category: string;
  allDivergences: { variant: 'missed' | 'over-flagged'; category: string }[];
}

function getSrDivergenceBadgeState(task: Task): SrDivergenceState | null {
  const ctx = task.context as Record<string, unknown> | undefined;
  const outcome = ctx?._self_review_outcome as SelfReviewOutcome | undefined;
  const humanCategories = ctx?._first_attempt_rejection_categories as Record<string, unknown> | undefined;

  if (!outcome || !humanCategories) return null;
  const checkResults = outcome.check_results ?? {};

  const divergences: SrDivergenceState['allDivergences'] = [];

  // Find SR missed categories (reviewer flagged, SR passed or didn't check)
  for (const cat of Object.keys(humanCategories)) {
    const srResult = checkResults[cat] as SelfReviewCheckResult | undefined;
    if (!srResult || srResult.passed) {
      divergences.push({ variant: 'missed', category: cat });
    }
  }

  // Find over-flagged categories (SR failed, reviewer didn't flag)
  for (const cat of Object.keys(checkResults)) {
    const srResult = checkResults[cat] as SelfReviewCheckResult;
    if (!srResult.passed && !(cat in humanCategories)) {
      divergences.push({ variant: 'over-flagged', category: cat });
    }
  }

  if (divergences.length === 0) return null;

  // Sort: misses first (higher severity), then lexically
  const sorted = divergences.sort((a, b) => {
    if (a.variant !== b.variant) return a.variant === 'missed' ? -1 : 1;
    return a.category.localeCompare(b.category);
  });

  return {
    variant: sorted[0].variant,
    category: sorted[0].category,
    allDivergences: sorted,
  };
}

export function SrDivergenceBadge({ task, onClick }: { task: Task; onClick?: () => void }) {
  const state = getSrDivergenceBadgeState(task);
  if (!state) return null;

  const isMissed = state.variant === 'missed';
  const colorClass = isMissed ? 'bg-red-500/10 border-red-500/20 text-red-400' : 'bg-amber-500/10 border-amber-500/20 text-amber-400';
  const label = isMissed ? `SR missed ${state.category}` : `SR over-flagged ${state.category}`;
  const allLabels = state.allDivergences.map(d => `${d.variant === 'missed' ? 'missed' : 'over-flagged'} ${d.category}`).join(', ');
  const tooltip = state.allDivergences.length > 1
    ? `${label}; all divergences: ${allLabels}`
    : label;

  if (onClick) {
    return (
      <button
        type="button"
        title={tooltip}
        onClick={(e) => { e.stopPropagation(); onClick(); }}
        className={`inline-flex items-center text-[10px] font-medium border px-1.5 py-0.5 rounded min-h-[44px] min-w-[44px] sm:min-h-auto sm:min-w-auto ${colorClass} hover:opacity-80 transition-opacity`}
      >
        {label}
      </button>
    );
  }

  return (
    <span
      title={tooltip}
      className={`inline-flex items-center text-[10px] font-medium border px-1.5 py-0.5 rounded ${colorClass}`}
    >
      {label}
    </span>
  );
}
