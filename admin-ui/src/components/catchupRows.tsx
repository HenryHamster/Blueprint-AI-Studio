import { isSafeUrl } from './TaskCard';
import { timeAgo } from '../utils/time';
import { formatDuration } from './catchupHelpers';
import type { RecentCompletion } from './catchupHelpers';

export function WaitingBadge({ hours }: { hours: number | undefined }) {
  if (hours === undefined || hours === null) return null;
  const label = hours < 24 ? `${hours}h` : `${Math.floor(hours / 24)}d`;
  const color = hours > 24
    ? 'bg-red-500/15 text-red-400 border-red-500/20'
    : hours >= 6
      ? 'bg-amber-500/15 text-amber-400 border-amber-500/20'
      : 'bg-neutral-700/30 text-neutral-500 border-neutral-700/30';
  return (
    <span className={`shrink-0 text-[9px] font-mono px-1.5 py-0.5 rounded border ${color}`}>
      {label}
    </span>
  );
}

export function CompletionRow({ c, onSelectTask }: { c: RecentCompletion; onSelectTask: (task: { id: string }) => void }) {
  const prNum = c.pr_url?.match(/\/(\d+)\/?$/)?.[1];
  return (
    // eslint-disable-next-line viewport/min-touch-target -- pre-existing, dense UI; audit per rule introduction
    <div
      role="button"
      tabIndex={0}
      className="flex items-center gap-2 text-xs bg-neutral-950 border border-neutral-800/80 px-2 py-1.5 rounded cursor-pointer hover:bg-neutral-800/50"
      onClick={() => onSelectTask({ id: c.task_id })}
      onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onSelectTask({ id: c.task_id }); } }}
    >
      <span className="flex-1 truncate text-neutral-400">{c.title}</span>
      <div className="shrink-0 flex items-center gap-2">
        {c.direction_text && (
          <span
            className="shrink-0 text-[9px] px-1.5 py-0.5 rounded bg-neutral-700/30 text-neutral-400 border border-neutral-700/40 truncate max-w-[12rem]"
            title={c.direction_text}
          >
            {c.direction_text.length > 40 ? c.direction_text.slice(0, 40) + '…' : c.direction_text}
          </span>
        )}
        {c.scope && (
          <span
            className="shrink-0 text-[9px] uppercase tracking-wider font-semibold px-1.5 py-0.5 rounded bg-indigo-500/10 text-indigo-400 border border-indigo-500/20"
            title={`scope: ${c.scope}`}
          >
            {c.scope}
          </span>
        )}
        {typeof c.duration_seconds === 'number' && c.duration_seconds > 0 && (
          <span className="text-[9px] text-neutral-500 font-mono" title="duration">
            {formatDuration(c.duration_seconds)}
          </span>
        )}
        {typeof c.review_iterations === 'number' && c.review_iterations > 1 && (
          <span
            className="shrink-0 text-[9px] px-1.5 py-0.5 rounded bg-amber-500/10 text-amber-400 border border-amber-500/20"
            title="review iterations"
          >
            {c.review_iterations} cycles
          </span>
        )}
        {c.pr_url && isSafeUrl(c.pr_url) && (
          <a
            href={c.pr_url}
            target="_blank"
            rel="noopener noreferrer"
            onClick={e => e.stopPropagation()}
            className="text-[9px] text-emerald-400 hover:text-emerald-300 font-medium"
          >
            {prNum ? `PR #${prNum}` : 'PR'}
          </a>
        )}
        {c.completed_at && (
          <span className="text-[10px] text-neutral-600 font-mono">{timeAgo(c.completed_at)}</span>
        )}
      </div>
    </div>
  );
}

export function SkeletonRow() {
  return (
    <div className="flex items-center gap-2 px-2 py-1.5 rounded bg-neutral-950 border border-neutral-800/60 animate-pulse">
      <div className="h-2.5 bg-neutral-700/50 rounded flex-1" />
      <div className="h-2.5 bg-neutral-700/50 rounded w-12" />
    </div>
  );
}
