import { timeAgo } from '../utils/time';
import type { DigestData } from '../types/digest';
import type { DirectionProgress } from './DirectionProgressSection';

export interface CatchUpMemoPathwaySectionProps {
  digest: DigestData<DirectionProgress>;
}

/**
 * "Memo pathway" metrics: pathway exits, ever-cited badge, and per-cycle
 * memos-cited/available chips. Renders nothing unless there is at least one
 * memo pathway exit (matching the inline guard it was extracted from).
 */
export function CatchUpMemoPathwaySection({ digest }: CatchUpMemoPathwaySectionProps) {
  if ((digest.memo_pathway_exits ?? 0) <= 0) return null;

  return (
    <div className="flex flex-wrap items-center gap-2 text-[11px]">
      <span className="text-[10px] text-neutral-500 uppercase tracking-wide">Memo pathway</span>
      <span className="text-[10px] font-semibold text-violet-400 px-1.5 py-0.5 rounded border border-violet-500/30 bg-violet-500/10">
        {digest.memo_pathway_exits} exit{digest.memo_pathway_exits !== 1 ? 's' : ''}
      </span>
      {digest.memo_ever_cited?.ever_cited ? (
        <span
          className="text-[10px] font-semibold text-emerald-400 px-1.5 py-0.5 rounded border border-emerald-500/30 bg-emerald-500/10"
          title={digest.memo_ever_cited.first_cited_at ? `first cited ${timeAgo(digest.memo_ever_cited.first_cited_at)}` : undefined}
        >
          ever cited ✓
        </span>
      ) : digest.memo_ever_cited != null ? (
        <span className="text-[10px] text-neutral-500 px-1.5 py-0.5 rounded border border-neutral-700/50">
          not yet cited
        </span>
      ) : null}
      {(digest.recent_cycle_memo_stats ?? []).length > 0 && (
        <div className="flex flex-wrap gap-1">
          {(digest.recent_cycle_memo_stats ?? []).slice(0, 5).map((s, i) => (
            <span
              key={s.cycle_id ?? i}
              className="text-[10px] text-neutral-400 px-1.5 py-0.5 rounded border border-neutral-700/50 bg-neutral-800/50"
              title={s.started_at ? `cycle ${timeAgo(s.started_at)}` : undefined}
            >
              {s.memos_cited}/{s.memos_available}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}
