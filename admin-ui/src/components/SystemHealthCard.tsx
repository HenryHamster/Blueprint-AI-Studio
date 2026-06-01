import { AlertTriangle, CheckCircle } from 'lucide-react';
import { Skeleton } from './DashboardMetricCards';
import type { Task } from '../types/task';

export interface HealthOverviewIssue {
  severity: 'critical' | 'warning' | 'info';
  title: string;
  hint: string;
  task_ids: string[];
  category: string;
}

export interface HealthOverviewResponse {
  overall_status: 'healthy' | 'degraded' | 'unhealthy';
  success_rate: { current: number | null; trend: string | null; window_hours: number };
  queue_depth: { queued: number; active: number; evaluating: number };
  actionable_issues: HealthOverviewIssue[];
}

const _HEALTH_SEV_BORDER: Record<string, string> = { critical: 'border-l-red-500', warning: 'border-l-amber-500', info: 'border-l-blue-500' };
const _HEALTH_SEV_ICON: Record<string, string> = { critical: 'text-red-400', warning: 'text-amber-400', info: 'text-blue-400' };
const _HEALTH_STATUS_STYLE: Record<string, { badge: string; label: string }> = {
  healthy: { badge: 'bg-emerald-500/10 border-emerald-500/30 text-emerald-400', label: 'Healthy' },
  degraded: { badge: 'bg-amber-500/10 border-amber-500/30 text-amber-400', label: 'Degraded' },
  unhealthy: { badge: 'bg-red-500/10 border-red-500/30 text-red-400', label: 'Unhealthy' },
};

export function SystemHealthCard({
  data,
  loading,
  tasks,
  onViewTask,
}: {
  data: HealthOverviewResponse | null;
  loading: boolean;
  tasks: Task[];
  onViewTask: (task: Task) => void;
}) {
  const status = data?.overall_status ?? 'healthy';
  const statusStyle = _HEALTH_STATUS_STYLE[status] ?? _HEALTH_STATUS_STYLE.healthy;
  const sr = data?.success_rate;
  const qd = data?.queue_depth;
  const issues = data?.actionable_issues ?? [];
  const trendColor = sr?.trend === 'improving' ? 'text-emerald-400' : sr?.trend === 'declining' ? 'text-red-400' : 'text-neutral-400';
  const trendIcon = sr?.trend === 'improving' ? '↑' : sr?.trend === 'declining' ? '↓' : '→';

  return (
    <section>
      <div className="flex items-center gap-2 mb-2">
        <h2 className="text-[11px] font-bold text-neutral-400 uppercase tracking-widest">System Health</h2>
        {!loading && data && (
          <span className={`text-[10px] font-semibold px-2 py-0.5 rounded border ${statusStyle.badge}`}>
            {statusStyle.label}
          </span>
        )}
      </div>
      {loading ? (
        <div className="space-y-2"><Skeleton className="h-10 w-full" /><Skeleton className="h-14 w-full" /></div>
      ) : (
        <>
          {data && (sr?.current != null || qd) && (
            <div className="flex flex-wrap gap-2 mb-2">
              {sr?.current != null && (
                <div className="bg-neutral-900/50 border border-neutral-800/50 rounded px-2.5 py-1.5 text-xs flex items-center gap-1.5">
                  <span className="text-neutral-500">Success</span>
                  <span className="font-mono font-bold text-neutral-200">{Math.round(sr.current * 100)}%</span>
                  {sr.trend && <span className={`text-[10px] ${trendColor}`}>{trendIcon}</span>}
                </div>
              )}
              {qd && (
                <div className="bg-neutral-900/50 border border-neutral-800/50 rounded px-2.5 py-1.5 text-xs flex items-center gap-2">
                  <span className="text-neutral-500">Queue</span>
                  <span className="font-mono text-neutral-300" title="queued">{qd.queued}q</span>
                  <span className="font-mono text-neutral-300" title="active">{qd.active}a</span>
                  <span className="font-mono text-neutral-300" title="evaluating">{qd.evaluating}e</span>
                </div>
              )}
            </div>
          )}
          {issues.length === 0 ? (
            <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg p-4 flex items-center gap-2 text-emerald-400 text-xs">
              <CheckCircle className="w-4 h-4" />
              All systems healthy
            </div>
          ) : (
            <div className="space-y-1.5">
              {issues.map((issue, i) => {
                const borderCls = _HEALTH_SEV_BORDER[issue.severity] ?? 'border-l-neutral-500';
                const iconCls = _HEALTH_SEV_ICON[issue.severity] ?? 'text-neutral-400';
                const linkedTasks = issue.task_ids.slice(0, 3)
                  .map(id => tasks.find(t => t.id === id))
                  .filter((t): t is Task => t != null);
                return (
                  <div key={i} className={`bg-neutral-900/50 border border-neutral-800/50 border-l-2 ${borderCls} rounded-lg p-3`}>
                    <div className="flex items-start gap-2">
                      <AlertTriangle className={`w-4 h-4 mt-0.5 shrink-0 ${iconCls}`} />
                      <div className="min-w-0 flex-1">
                        <p className="text-xs text-neutral-200">{issue.title}</p>
                        {issue.hint && <p className="text-[10px] text-neutral-500 mt-0.5">{issue.hint}</p>}
                        {linkedTasks.length > 0 && (
                          <div className="flex flex-wrap gap-1 mt-1.5">
                            {linkedTasks.map(t => (
                              <button
                                key={t.id}
                                onClick={() => onViewTask(t)}
                                className="text-[10px] px-1.5 py-0.5 rounded bg-neutral-800/60 text-neutral-300 hover:text-neutral-100 hover:bg-neutral-700/60 transition-colors truncate max-w-[200px] min-h-[44px] flex items-center"
                              >
                                {(t.title ?? t.id).slice(0, 40)}
                              </button>
                            ))}
                          </div>
                        )}
                      </div>
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </>
      )}
    </section>
  );
}
