import type { DigestData } from '../types/digest';
import type { DirectionProgress } from './DirectionProgressSection';
import type { TimeWindow } from './catchupHelpers';

export interface CatchUpNarrativeSummaryProps {
  digest: DigestData<DirectionProgress>;
  activeWindow: TimeWindow;
}

// The backend bakes a review/fix convergence verdict into narrative_summary,
// already gated on churn-signal sufficiency (see
// digest_metrics.compute_narrative_summary). The sentence always starts with
// "Review/fix churn is" and runs to the next period. We lift it out of the
// prose so the headline churn verdict reads as its own line instead of being
// buried mid-paragraph; the rest of the narrative renders unchanged. On thin
// windows the backend emits no such sentence, so nothing extra is surfaced.
const CONVERGENCE_VERDICT_RE = /Review\/fix churn is[^.]*\./;

/**
 * Narrative summary paragraph plus the trend pill. Renders nothing when the
 * digest has neither a narrative_summary nor a trend (matching the inline
 * guard it was extracted from). When the narrative carries a review/fix
 * convergence verdict, that verdict is surfaced as its own emphasized line.
 */
export function CatchUpNarrativeSummary({ digest, activeWindow }: CatchUpNarrativeSummaryProps) {
  if (!digest.narrative_summary && !digest.trend) return null;

  const narrative = digest.narrative_summary ?? '';
  const convergenceVerdict = narrative.match(CONVERGENCE_VERDICT_RE)?.[0] ?? null;
  const prose = convergenceVerdict
    ? narrative.replace(CONVERGENCE_VERDICT_RE, '').replace(/\s{2,}/g, ' ').trim()
    : narrative;

  return (
    <div className="flex flex-col gap-1.5 mb-3">
      <div className="flex items-start gap-2">
        {prose && (
          <p className="text-sm text-neutral-300 leading-relaxed flex-1">
            {activeWindow === 'last_visit' && digest.window_hours && (
              <span className="text-neutral-400">Since you last checked ({digest.window_hours}h ago): </span>
            )}
            {prose}
          </p>
        )}
        {digest.trend && (
          <span
            className={`shrink-0 text-[10px] font-semibold px-2 py-0.5 rounded-full border ${
              digest.trend === 'improving'
                ? 'bg-emerald-500/15 text-emerald-400 border-emerald-500/20'
                : digest.trend === 'declining'
                  ? 'bg-red-500/15 text-red-400 border-red-500/20'
                  : 'bg-neutral-700/30 text-neutral-500 border-neutral-700/30'
            }`}
            aria-label={`Trend: ${digest.trend === 'improving' ? 'Improving' : digest.trend === 'declining' ? 'Declining' : 'Stable'}`}
          >
            {digest.trend === 'improving' ? 'Improving' : digest.trend === 'declining' ? 'Declining' : 'Stable'}
          </span>
        )}
      </div>
      {convergenceVerdict && (
        <p
          className="text-xs font-medium text-amber-300/90 leading-relaxed"
          aria-label="Review/fix convergence verdict"
        >
          {convergenceVerdict}
        </p>
      )}
    </div>
  );
}
