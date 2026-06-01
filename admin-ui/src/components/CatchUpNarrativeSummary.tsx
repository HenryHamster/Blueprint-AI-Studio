import type { DigestData } from '../types/digest';
import type { DirectionProgress } from './DirectionProgressSection';
import type { TimeWindow } from './catchupHelpers';

export interface CatchUpNarrativeSummaryProps {
  digest: DigestData<DirectionProgress>;
  activeWindow: TimeWindow;
}

/**
 * Narrative summary paragraph plus the trend pill. Renders nothing when the
 * digest has neither a narrative_summary nor a trend (matching the inline
 * guard it was extracted from).
 */
export function CatchUpNarrativeSummary({ digest, activeWindow }: CatchUpNarrativeSummaryProps) {
  if (!digest.narrative_summary && !digest.trend) return null;

  return (
    <div className="flex items-start gap-2 mb-3">
      {digest.narrative_summary && (
        <p className="text-sm text-neutral-300 leading-relaxed flex-1">
          {activeWindow === 'last_visit' && digest.window_hours && (
            <span className="text-neutral-400">Since you last checked ({digest.window_hours}h ago): </span>
          )}
          {digest.narrative_summary}
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
  );
}
