import { ChevronDown, ChevronUp, Loader2, Compass, DollarSign } from 'lucide-react';
import { timeAgo } from '../utils/time';
import { TaskTypeBadge } from './TaskCard';
import type { TaskType } from './TaskCard';

export interface ActiveTask {
  task_id: string;
  title: string;
  status: string;
  task_type: string;
  started_at: string | null;
  running_cost_usd?: number | null;
  direction_text?: string | null;
  scope?: string | null;
  review_iteration?: number | null;
  review_cap?: number | null;
}

export interface CatchUpInProgressSectionProps {
  activeCount: number;
  activeTasks: ActiveTask[];
  expanded: boolean;
  onToggle: () => void;
  onSelectTask: (task: { id: string }) => void;
}

export function CatchUpInProgressSection({
  activeCount,
  activeTasks,
  expanded,
  onToggle,
  onSelectTask,
}: CatchUpInProgressSectionProps) {
  if (activeCount <= 0) return null;

  const tasks = activeTasks ?? [];

  return (
    <div>
      <button
        onClick={onToggle}
        aria-expanded={expanded}
        className="flex items-center gap-1.5 text-[11px] font-bold text-neutral-400 uppercase tracking-widest hover:text-neutral-300 transition-colors mb-1.5"
      >
        {expanded ? <ChevronUp className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />}
        <Loader2 className="w-3 h-3 text-neutral-500" />
        In progress ({activeCount})
      </button>
      {expanded && (
        <div className="flex flex-col gap-1.5">
          {tasks.map(t => (
            <div
              key={t.task_id}
              role="button"
              tabIndex={0}
              className="flex flex-wrap items-center gap-2 text-xs bg-neutral-950 border border-neutral-800/80 px-2 py-1.5 rounded cursor-pointer hover:bg-neutral-800/50 min-h-[44px]"
              onClick={() => onSelectTask({ id: t.task_id })}
              onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onSelectTask({ id: t.task_id }); } }}
            >
              <span className="min-w-0 flex-1 truncate text-neutral-400">{t.title}</span>
              {t.direction_text && (
                <span
                  className="shrink-0 inline-flex items-center gap-0.5 text-[9px] px-1.5 py-0.5 rounded bg-neutral-700/30 text-neutral-400 border border-neutral-700/40 max-w-[180px] truncate"
                  title={t.direction_text}
                >
                  <Compass className="w-2.5 h-2.5 shrink-0" />
                  <span className="truncate">{t.direction_text.slice(0, 30)}{t.direction_text.length > 30 ? '…' : ''}</span>
                </span>
              )}
              {t.task_type && (
                <TaskTypeBadge taskType={t.task_type as TaskType} />
              )}
              {t.scope && (
                <span
                  className="shrink-0 text-[9px] uppercase tracking-wider font-semibold px-1.5 py-0.5 rounded bg-indigo-500/10 text-indigo-400 border border-indigo-500/20"
                  title={`scope: ${t.scope}`}
                >
                  {t.scope}
                </span>
              )}
              {t.review_iteration != null && t.review_iteration > 0 && t.review_cap != null && t.review_cap > 0 && (() => {
                const reviewIter = t.review_iteration;
                const reviewCap = t.review_cap;
                if (reviewIter == null || reviewCap == null) return null;
                const ratio = reviewIter / reviewCap;
                const colorCls = ratio > 0.9
                  ? "bg-red-500/10 text-red-400 border-red-500/20"
                  : ratio > 0.7
                    ? "bg-amber-500/10 text-amber-400 border-amber-500/20"
                    : "bg-neutral-700/20 text-neutral-400 border-neutral-700/30";
                return (
                  <span
                    className={`shrink-0 text-[9px] font-mono px-1.5 py-0.5 rounded border ${colorCls}`}
                    title={`review iterations: ${t.review_iteration}/${t.review_cap}`}
                  >
                    {t.review_iteration}/{t.review_cap} iter
                  </span>
                );
              })()}
              {t.running_cost_usd != null && t.running_cost_usd > 0 && (
                <span className="shrink-0 inline-flex items-center gap-0.5 text-[9px] font-mono px-1.5 py-0.5 rounded bg-emerald-500/10 text-emerald-400 border border-emerald-500/20">
                  <DollarSign className="w-2.5 h-2.5" />
                  {t.running_cost_usd.toFixed(2)}
                </span>
              )}
              <span className="shrink-0 text-[9px] uppercase tracking-wider font-semibold px-1.5 py-0.5 rounded bg-neutral-700/30 text-neutral-500">
                {t.status}
              </span>
              {t.started_at && (
                <span className="shrink-0 text-[10px] text-neutral-600 font-mono">{timeAgo(t.started_at)}</span>
              )}
            </div>
          ))}
          {activeCount > tasks.length && (
            <div className="text-[10px] text-neutral-600 px-2">
              +{activeCount - tasks.length} more
            </div>
          )}
        </div>
      )}
    </div>
  );
}
