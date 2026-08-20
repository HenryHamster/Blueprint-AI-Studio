import { ExternalLink, GitMerge } from 'lucide-react';
import { Skeleton } from './DashboardMetricCardsPrimitives';
import { timeAgo, formatDurationSec } from '../utils/time';
import { isSafeUrl } from './TaskCard';
import { humanizeUnverifiedReason } from '../utils/unverifiedReasonLabels';
import type { RecentCompletion } from '../types/digest';

const TASK_TYPE_COLORS: Record<string, string> = {
  create: 'bg-emerald-500/20 text-emerald-400',
  fix: 'bg-red-500/20 text-red-400',
  review: 'bg-amber-500/20 text-amber-400',
  research: 'bg-blue-500/20 text-blue-400',
  plan: 'bg-purple-500/20 text-purple-400',
  refactor: 'bg-cyan-500/20 text-cyan-400',
};

export function RecentMergesTimeline({
  mergeEvents,
  loading,
}: {
  mergeEvents: RecentCompletion[];
  loading: boolean;
}) {
  return (
    <>
      <h2 className="text-ui-md font-bold text-neutral-400 uppercase tracking-widest mb-2">Recent Merges</h2>
      {loading ? (
        <div className="space-y-3"><Skeleton className="h-12 w-full" /><Skeleton className="h-12 w-full" /><Skeleton className="h-12 w-full" /></div>
      ) : mergeEvents.length === 0 ? (
        <div className="bg-workbench-base/50 border border-neutral-800/50 rounded-lg p-4 text-neutral-500 text-xs">No recent merges</div>
      ) : (
        <div className="relative pl-4 space-y-0">
          {/* vertical line */}
          <div className="absolute left-1.5 top-1 bottom-1 w-px bg-workbench-panel" />
          {mergeEvents.map(ev => {
            const dotColor = (ev.task_type === 'fix' || ev.task_type === 'review') ? 'bg-amber-500' : 'bg-emerald-500';
            const typeCls = TASK_TYPE_COLORS[ev.task_type ?? ''] ?? 'bg-workbench-control/20 text-neutral-400';
            const dirLabel = ev.direction_text ? ev.direction_text.slice(0, 30) + (ev.direction_text.length > 30 ? '…' : '') : null;
            const unverifiedReason = ev.unverified_merge_reason ? ev.unverified_merge_reason.trim() : '';
            return (
              <div key={ev.task_id} className="relative pb-3">
                <div className={`absolute -left-2.5 top-1.5 w-2 h-2 rounded-full ${dotColor} ring-2 ring-neutral-950`} />
                <div className="bg-workbench-base/50 border border-neutral-800/50 rounded-lg p-2.5">
                  <div className="flex items-start gap-2">
                    <GitMerge className="w-3.5 h-3.5 text-neutral-500 mt-0.5 shrink-0" />
                    <div className="flex-1 min-w-0">
                      <p className="text-xs text-neutral-200 truncate">{ev.title}</p>
                      {dirLabel && <p className="text-ui-xs text-neutral-500 truncate mt-0.5">{dirLabel}</p>}
                      <div className="flex items-center gap-2 mt-1 text-ui-sm text-neutral-500">
                        {ev.task_type && <span className={`px-1.5 py-0.5 rounded text-ui-xs font-medium ${typeCls}`}>{ev.task_type}</span>}
                        <span>{timeAgo(ev.completed_at ?? '')}</span>
                        {ev.duration_seconds != null && <span>{formatDurationSec(ev.duration_seconds)}</span>}
                      </div>
                      {unverifiedReason && (
                        <div className="flex items-center gap-1.5 mt-1">
                          <span className="px-1.5 py-0.5 rounded text-ui-xs font-medium bg-amber-500/20 text-amber-400">UNVERIFIED</span>
                          <span className="text-ui-xs text-amber-400/80 truncate">{humanizeUnverifiedReason(unverifiedReason)}</span>
                        </div>
                      )}
                    </div>
                    {ev.pr_url && isSafeUrl(ev.pr_url) && (
                      <a href={ev.pr_url} target="_blank" rel="noopener noreferrer" className="text-neutral-500 hover:text-neutral-300 shrink-0" data-viewport-allow-small-target title="View PR on GitHub">
                        <ExternalLink className="w-3.5 h-3.5" />
                      </a>
                    )}
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </>
  );
}
