import { AlertTriangle, CheckCircle } from 'lucide-react';
import { Skeleton } from './DashboardMetricCardsPrimitives';
import { timeAgo } from '../utils/time';
import { splitConvergenceVerdict } from '../utils/convergenceVerdict';
import { StrategicChallengeChip } from './StrategicChallengeChip';
import type { DigestData } from './DashboardHome';

export interface LatestCycle {
  cycle_id: string;
  started_at: string;
  completed_at: string | null;
  outcome: string;
  directions_created: number;
  directions_retired: number;
  vision_updated: boolean;
  summary_text?: string | null;
  memos_available?: number | null;
  memos_cited?: number | null;
}

interface SystemStatusCardProps {
  digest: DigestData | null;
  loading: boolean;
}

export function SystemStatusCard({ digest, loading }: SystemStatusCardProps) {
  const cycle = digest?.latest_cycle ?? null;
  const memoExits = digest?.memo_pathway_exits ?? 0;
  const memoEverCited = digest?.memo_ever_cited ?? null;
  const recentCycleMemoStats = digest?.recent_cycle_memo_stats ?? [];
  const hasContent = digest?.narrative_summary || cycle;

  // Lift the convergence verdict out of the prose via the shared helper so this
  // card and CatchUpNarrativeSummary surface it identically.
  const { verdict: convergenceVerdict, prose } = splitConvergenceVerdict(digest?.narrative_summary);

  return (
    <section>
      <h2 className="text-ui-md font-bold text-neutral-400 uppercase tracking-widest mb-2">System Status</h2>
      <div className="bg-workbench-base/50 border border-neutral-800/50 rounded-lg p-4">
        {loading ? (
          <Skeleton className="h-16 w-full" />
        ) : !hasContent ? (
          <p className="text-neutral-500 text-xs">No recent activity</p>
        ) : (
          <>
            {prose && (
              <p className="text-sm text-neutral-300 leading-relaxed">{prose}</p>
            )}
            {convergenceVerdict && (
              <p
                className="text-xs font-medium text-amber-300/90 leading-relaxed mt-1.5"
                aria-label="Review/fix convergence verdict"
              >
                {convergenceVerdict}
              </p>
            )}
            {cycle && (
              <>
                <div className="text-ui-sm text-neutral-500 mt-2 flex items-center gap-2">
                  {cycle.outcome === 'completed' ? (
                    <CheckCircle className="w-3 h-3 text-emerald-400 shrink-0" />
                  ) : (
                    <AlertTriangle className="w-3 h-3 text-red-400 shrink-0" />
                  )}
                  <span>
                    Last cycle: {timeAgo(cycle.completed_at ?? cycle.started_at)} &mdash;{' '}
                    {cycle.directions_created} created, {cycle.directions_retired} retired
                  </span>
                  {(cycle.memos_available != null || cycle.memos_cited != null) && (
                    <span className="text-violet-400" title="memos available / cited in last cycle">
                      {cycle.memos_available ?? 0} memo{(cycle.memos_available ?? 0) !== 1 ? 's' : ''} avail, {cycle.memos_cited ?? 0} cited
                    </span>
                  )}
                  {cycle.strategic_judgment_challenge && (
                    <StrategicChallengeChip summary={cycle.strategic_judgment_challenge} />
                  )}
                </div>
                {cycle.summary_text && (
                  <p className="text-ui-sm text-neutral-500 mt-1">{cycle.summary_text}</p>
                )}
              </>
            )}
            {memoExits > 0 && (
              <div className="mt-2 pt-2 border-t border-neutral-800/50">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-ui-sm text-neutral-500 uppercase tracking-wide">Memo pathway</span>
                  <span className="text-ui-sm font-semibold text-violet-400 px-1.5 py-0.5 rounded border border-violet-500/30 bg-violet-500/10">
                    {memoExits} exit{memoExits !== 1 ? 's' : ''}
                  </span>
                  {memoEverCited?.ever_cited ? (
                    <span
                      className="text-ui-sm font-semibold text-emerald-400 px-1.5 py-0.5 rounded border border-emerald-500/30 bg-emerald-500/10"
                      title={memoEverCited.first_cited_at ? `first cited ${timeAgo(memoEverCited.first_cited_at)}` : undefined}
                    >
                      ever cited ✓
                    </span>
                  ) : memoEverCited != null ? (
                    <span className="text-ui-sm text-neutral-500 px-1.5 py-0.5 rounded border border-neutral-700/50">
                      not yet cited
                    </span>
                  ) : null}
                  {recentCycleMemoStats.length > 0 && (
                    <div className="flex flex-wrap gap-1">
                      {recentCycleMemoStats.slice(0, 5).map((s: typeof recentCycleMemoStats[0], i: number) => (
                        <span
                          key={s.cycle_id ?? i}
                          className="text-ui-sm text-neutral-400 px-1.5 py-0.5 rounded border border-neutral-700/50 bg-workbench-panel/50"
                          title={s.started_at ? `cycle ${timeAgo(s.started_at)}` : undefined}
                        >
                          {s.memos_cited}/{s.memos_available}
                        </span>
                      ))}
                    </div>
                  )}
                </div>
              </div>
            )}
          </>
        )}
      </div>
    </section>
  );
}
