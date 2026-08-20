import React, { useState } from 'react';
import { AlertTriangle, ChevronRight, ExternalLink, EyeOff, RefreshCw, Wand2 } from 'lucide-react';
import { timeAgo } from '../utils/time';
import { isSafeUrl } from './TaskCard';
import type { Task } from '../types/task';

export interface OpenPRTask {
  task_id: string;
  task_status: string;
  task_type: string;
  task_title: string | null;
}

export interface OpenPR {
  pr_number: number;
  pr_url: string;
  pr_title: string;
  pr_branch: string;
  pr_base_branch?: string;
  pr_created_at: string;
  tasks: OpenPRTask[];
  is_orphaned: boolean;
}

const TASK_TYPE_COLORS: Record<string, string> = {
  create: 'bg-emerald-500/20 text-emerald-400',
  fix: 'bg-red-500/20 text-red-400',
  review: 'bg-amber-500/20 text-amber-400',
  research: 'bg-blue-500/20 text-blue-400',
  plan: 'bg-purple-500/20 text-purple-400',
  refactor: 'bg-cyan-500/20 text-cyan-400',
};

const PR_STATUS_COLORS: Record<string, string> = {
  QUEUED: 'bg-blue-500/20 text-blue-400',
  PROVISIONING: 'bg-blue-500/20 text-blue-400',
  IN_PROGRESS: 'bg-emerald-500/20 text-emerald-400',
  EVALUATING: 'bg-amber-500/20 text-amber-400',
  COMPLETED: 'bg-workbench-control/20 text-neutral-400',
  FAILED: 'bg-red-500/20 text-red-400',
  CANCELLED: 'bg-workbench-control/20 text-neutral-500',
};

function SkeletonRow() {
  return <div className="animate-pulse bg-workbench-panel/60 rounded h-10 w-full" />;
}

export function PRMonitorSection({ openPRs, loading, refreshing, onRefresh, hasProject, tasks, onViewTask, integrationBranch, promotionTarget, onSpawnFixTask, stale, degraded }: { openPRs: OpenPR[]; loading: boolean; refreshing: boolean; onRefresh: () => void; hasProject: boolean; tasks: Task[]; onViewTask: (task: Task) => void; integrationBranch?: string | null; promotionTarget?: string | null; onSpawnFixTask?: (pr: OpenPR) => void; stale?: boolean; degraded?: boolean }) {
  const [expanded, setExpanded] = useState(true);
  const [dismissedPrs, setDismissedPrs] = useState<Set<number>>(() => new Set());
  const tasksById = React.useMemo(() => new Map(tasks.map(t => [t.id, t])), [tasks]);
  const busy = loading || refreshing;
  const visiblePRs = React.useMemo(() => openPRs.filter(pr => !dismissedPrs.has(pr.pr_number)), [openPRs, dismissedPrs]);
  const hasOutage = Boolean(degraded || stale);
  const staleBadgeLabel = degraded || (stale && openPRs.length === 0) ? 'GitHub unreachable' : 'cached';
  const staleBadgeTitle = degraded || (stale && openPRs.length === 0)
    ? 'GitHub is unreachable — showing outage state'
    : 'Last PR refresh failed — showing cached data';

  return (
    <section className="mt-4">
      <div className="flex items-center gap-1.5 mb-2">
        <button
          onClick={() => setExpanded(!expanded)}
          className="flex items-center justify-center gap-1.5 group min-h-11 px-2"
        >
          <h2 className="text-ui-md font-bold text-neutral-400 uppercase tracking-widest">Open PRs</h2>
          <span className="text-ui-sm text-neutral-500 font-mono">{visiblePRs.length}</span>
          <ChevronRight className={`w-3 h-3 text-neutral-500 transition-transform ${expanded ? 'rotate-90' : ''}`} />
        </button>
        {hasProject && (
          <button
            onClick={onRefresh}
            disabled={busy}
            title={refreshing ? 'Refreshing…' : 'Refresh open PRs'}
            className="ml-1 text-neutral-500 hover:text-neutral-300 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >
            <RefreshCw className={`w-3 h-3 ${busy ? 'animate-spin' : ''}`} />
          </button>
        )}
        {hasOutage && (
          <span
            data-testid="open-prs-stale-badge"
            title={staleBadgeTitle}
            className="flex items-center gap-1 text-ui-sm text-amber-400"
          >
            <AlertTriangle className="w-3 h-3" />
            {staleBadgeLabel}
          </span>
        )}
      </div>
      {expanded && (
        !hasProject ? (
          <div className="bg-workbench-base/50 border border-neutral-800/50 rounded-lg p-4 text-neutral-500 text-xs">Select a project</div>
        ) : loading ? (
          <div className="space-y-2"><SkeletonRow /><SkeletonRow /></div>
        ) : visiblePRs.length === 0 ? (
          <div className="bg-workbench-base/50 border border-neutral-800/50 rounded-lg p-4 text-neutral-500 text-xs">
            {hasOutage ? 'GitHub unreachable' : 'No open PRs'}
          </div>
        ) : (
          <div className="space-y-1.5 max-h-[400px] overflow-y-auto custom-scrollbar">
            {visiblePRs.map(pr => (
              <div key={pr.pr_number} className="bg-workbench-base/50 border border-neutral-800/50 rounded-lg p-2.5">
                <div className="flex items-start gap-2">
                  <div className={`w-1.5 h-1.5 rounded-full mt-1.5 shrink-0 ${pr.is_orphaned ? 'bg-red-400' : 'bg-emerald-400'}`} />
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-1.5">
                      <span className="text-xs text-neutral-300 font-mono">#{pr.pr_number}</span>
                      <p className="text-xs text-neutral-200 truncate">{pr.pr_title}</p>
                      <span className="text-ui-sm text-neutral-500 ml-auto">{timeAgo(pr.pr_created_at)}</span>
                    </div>
                    <div className="flex items-center gap-1 mt-0.5 flex-wrap">
                      <span className="text-ui-xs text-neutral-600 font-mono truncate">{pr.pr_branch}</span>
                      {pr.pr_base_branch && (
                        <>
                          <span className="text-ui-xs text-neutral-700">→</span>
                          <span className="text-ui-xs text-neutral-600 font-mono truncate">{pr.pr_base_branch}</span>
                        </>
                      )}
                      {pr.pr_base_branch && integrationBranch && pr.pr_base_branch !== integrationBranch && pr.pr_base_branch !== (promotionTarget ?? '') && (
                        <span className="text-ui-xs text-amber-400 border border-amber-500/30 rounded px-1 py-0 ml-0.5">drift</span>
                      )}
                    </div>
                    {pr.is_orphaned ? (
                      <div className="mt-1.5 text-ui-xs text-red-400 font-medium">No active task</div>
                    ) : (
                      <div className="mt-1.5 space-y-1">
                        {pr.tasks.map(t => {
                          const typeCls = TASK_TYPE_COLORS[t.task_type] ?? 'bg-workbench-control/20 text-neutral-400';
                          const statusCls = PR_STATUS_COLORS[t.task_status] ?? 'bg-workbench-control/20 text-neutral-500';
                          const fullTask = tasksById.get(t.task_id);
                          const row = (
                            <div className="flex items-center gap-1.5 flex-wrap">
                              <span className={`px-1.5 py-0.5 rounded text-ui-xs font-medium ${typeCls}`}>{t.task_type}</span>
                              <span className={`px-1.5 py-0.5 rounded text-ui-xs font-medium ${statusCls}`}>{t.task_status}</span>
                              {t.task_title && (
                                <span className="text-ui-sm text-neutral-400 truncate min-w-0 flex-1">{t.task_title}</span>
                              )}
                            </div>
                          );
                          return fullTask ? (
                            // eslint-disable-next-line viewport/min-touch-target -- pre-existing, dense UI; audit per rule introduction
                            <button
                              key={t.task_id}
                              onClick={() => onViewTask(fullTask)}
                              className="w-full text-left hover:bg-workbench-hover/40 rounded px-1 -mx-1 py-0.5 transition-colors"
                            >
                              {row}
                            </button>
                          ) : (
                            <div key={t.task_id} className="px-1 py-0.5">{row}</div>
                          );
                        })}
                      </div>
                    )}
                  </div>
                  <div className="flex items-center gap-1.5 shrink-0">
                    {pr.is_orphaned && onSpawnFixTask && (
                      <button
                        type="button"
                        onClick={() => onSpawnFixTask(pr)}
                        title="Spawn fix task"
                        className="text-neutral-500 hover:text-neutral-300"
                      >
                        <Wand2 className="w-3.5 h-3.5" />
                      </button>
                    )}
                    {pr.is_orphaned && (
                      <button
                        type="button"
                        onClick={() => setDismissedPrs(prev => { const next = new Set(prev); next.add(pr.pr_number); return next; })}
                        title="Dismiss until refresh"
                        className="text-neutral-500 hover:text-neutral-300"
                      >
                        <EyeOff className="w-3.5 h-3.5" />
                      </button>
                    )}
                    {pr.pr_url && isSafeUrl(pr.pr_url) && (
                      <a href={pr.pr_url} target="_blank" rel="noopener noreferrer" className="text-neutral-500 hover:text-neutral-300">
                        <ExternalLink className="w-3.5 h-3.5" />
                      </a>
                    )}
                  </div>
                </div>
              </div>
            ))}
          </div>
        )
      )}
    </section>
  );
}
