import { useMemo } from 'react';
import { AlertTriangle, CheckCircle } from 'lucide-react';
import { Skeleton } from './DashboardMetricCardsPrimitives';
import { snapshotFreshness } from '../utils/time';
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
  direction_portfolio?: {
    possibly_complete_count: number;
    missing_criteria_count: number;
    [key: string]: number;
  };
  // Precompute provenance, stamped by the backend when the overview is served
  // from the leader-precomputed cross-instance cache (absent on the on-demand
  // live-compute path, which is fresh-by-definition). Used to surface when the
  // snapshot was computed and to degrade the card once it goes stale.
  precomputed?: boolean;
  precomputed_at?: string | null;
  precompute_age_seconds?: number | null;
  // True when the backend served the LAST GOOD payload because the live health
  // compute failed (e.g. the scan exceeded its statement timeout). The reading
  // may be recent in wall-clock terms yet is NOT a live verdict, so the card
  // must dim it even when precompute_age_seconds is under the stale threshold.
  served_stale?: boolean;
}

// A cached snapshot older than this many seconds is presented as stale: the
// card dims/desaturates and its as-of label is marked, mirroring the
// per-project status-badge stale pattern so a cached reading is never shown as
// a live, authoritative one. The dashboard re-polls every ~30s, so a snapshot
// this old is meaningfully behind real project state.
const SNAPSHOT_STALE_AFTER_SECONDS = 120;

// Tailwind classes applied to the card body when its snapshot is stale —
// desaturate + dim (same affordance as projectStatus.ts STALE_CLASSES).
const STALE_CLASSES = 'opacity-60 saturate-50';

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
  onOpenDirections,
}: {
  data: HealthOverviewResponse | null;
  loading: boolean;
  tasks: Task[];
  onViewTask: (task: Task) => void;
  onOpenDirections?: () => void;
}) {
  const taskById = useMemo(() => new Map(tasks.map(t => [t.id, t])), [tasks]);
  const status = data?.overall_status ?? 'healthy';
  const statusStyle = _HEALTH_STATUS_STYLE[status] ?? _HEALTH_STATUS_STYLE.healthy;
  const sr = data?.success_rate;
  const qd = data?.queue_depth;
  const issues = data?.actionable_issues ?? [];
  const directionAttention = data?.direction_portfolio ?? null;
  const possiblyCompleteCount = directionAttention?.possibly_complete_count ?? 0;
  const missingCriteriaCount = directionAttention?.missing_criteria_count ?? 0;
  const hasDirectionAttention = directionAttention != null;
  const trendColor = sr?.trend === 'improving' ? 'text-emerald-400' : sr?.trend === 'declining' ? 'text-red-400' : 'text-neutral-400';
  const trendIcon = sr?.trend === 'improving' ? '↑' : sr?.trend === 'declining' ? '↓' : '→';
  const { asOfLabel, stale: freshnessStale } = snapshotFreshness(
    data?.precomputed_at,
    data?.precompute_age_seconds,
    SNAPSHOT_STALE_AFTER_SECONDS,
  );
  // A served-stale reading (compute currently failing, last-good returned) is
  // never live regardless of its age — OR it into the staleness decision so the
  // verdict dims/marks even inside the freshness window.
  const stale = freshnessStale || data?.served_stale === true;

  return (
    <section>
      <div className="flex items-center gap-2 mb-2">
        <h2 className="text-ui-md font-bold text-neutral-400 uppercase tracking-widest">System Health</h2>
        {!loading && data && (
          <span className={`text-ui-sm font-semibold px-2 py-0.5 rounded border ${statusStyle.badge}`}>
            {statusStyle.label}
          </span>
        )}
        {!loading && data && asOfLabel && (
          <span
            className={`text-ui-sm ${stale ? 'text-amber-400 font-semibold' : 'text-neutral-500'}`}
            title={stale ? 'Cached snapshot is stale — may not reflect current project state' : 'When this health snapshot was computed'}
          >
            {stale && '⚠ '}as of {asOfLabel}{stale && ' · stale'}
          </span>
        )}
      </div>
      {loading ? (
        <div className="space-y-2"><Skeleton className="h-10 w-full" /><Skeleton className="h-14 w-full" /></div>
      ) : !data ? (
        <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg p-4 flex items-center gap-2 text-neutral-400 text-xs">
          <AlertTriangle className="w-4 h-4" />
          Health data unavailable
        </div>
      ) : (
        <div className={stale ? STALE_CLASSES : undefined}>
          {data && (sr?.current != null || qd) && (
            <div className="flex flex-wrap gap-2 mb-2">
              {sr?.current != null && (
                <div className="bg-neutral-900/50 border border-neutral-800/50 rounded px-2.5 py-1.5 text-xs flex items-center gap-1.5">
                  <span className="text-neutral-500">Success</span>
                  <span className="font-mono font-bold text-neutral-200">{Math.round(sr.current * 100)}%</span>
                  {sr.trend && <span className={`text-ui-sm ${trendColor}`}>{trendIcon}</span>}
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
          {hasDirectionAttention && (
            onOpenDirections ? (
              <button
                type="button"
                onClick={onOpenDirections}
                aria-label="Open directions view"
                className="w-full mb-2 rounded-lg border border-neutral-800/50 bg-neutral-900/40 px-2.5 py-2 text-left text-xs transition-colors hover:border-neutral-700 hover:bg-neutral-900/70 focus:outline-none focus:ring-1 focus:ring-neutral-600"
              >
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-neutral-500 uppercase tracking-wide text-ui-sm font-semibold">Directions</span>
                  <span className={`rounded-full border px-2 py-0.5 font-mono text-ui-sm ${possiblyCompleteCount > 0 ? 'border-amber-500/30 bg-amber-500/10 text-amber-300' : 'border-neutral-800/70 bg-neutral-950/40 text-neutral-500'}`}>
                    {possiblyCompleteCount} possibly complete
                  </span>
                  <span className={`rounded-full border px-2 py-0.5 font-mono text-ui-sm ${missingCriteriaCount > 0 ? 'border-amber-500/30 bg-amber-500/10 text-amber-300' : 'border-neutral-800/70 bg-neutral-950/40 text-neutral-500'}`}>
                    {missingCriteriaCount} missing criteria
                  </span>
                </div>
              </button>
            ) : (
              <div className="mb-2 rounded-lg border border-neutral-800/50 bg-neutral-900/40 px-2.5 py-2 text-xs">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-neutral-500 uppercase tracking-wide text-ui-sm font-semibold">Directions</span>
                  <span className={`rounded-full border px-2 py-0.5 font-mono text-ui-sm ${possiblyCompleteCount > 0 ? 'border-amber-500/30 bg-amber-500/10 text-amber-300' : 'border-neutral-800/70 bg-neutral-950/40 text-neutral-500'}`}>
                    {possiblyCompleteCount} possibly complete
                  </span>
                  <span className={`rounded-full border px-2 py-0.5 font-mono text-ui-sm ${missingCriteriaCount > 0 ? 'border-amber-500/30 bg-amber-500/10 text-amber-300' : 'border-neutral-800/70 bg-neutral-950/40 text-neutral-500'}`}>
                    {missingCriteriaCount} missing criteria
                  </span>
                </div>
              </div>
            )
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
                  .map(id => taskById.get(id))
                  .filter((t): t is Task => t != null);
                return (
                  <div key={i} className={`bg-neutral-900/50 border border-neutral-800/50 border-l-2 ${borderCls} rounded-lg p-3`}>
                    <div className="flex items-start gap-2">
                      <AlertTriangle className={`w-4 h-4 mt-0.5 shrink-0 ${iconCls}`} />
                      <div className="min-w-0 flex-1">
                        <p className="text-xs text-neutral-200">{issue.title}</p>
                        {issue.hint && <p className="text-ui-sm text-neutral-500 mt-0.5">{issue.hint}</p>}
                        {linkedTasks.length > 0 && (
                          <div className="flex flex-wrap gap-1 mt-1.5">
                            {linkedTasks.map(t => (
                              <button
                                key={t.id}
                                onClick={() => onViewTask(t)}
                                className="text-ui-sm px-1.5 py-0.5 rounded bg-neutral-800/60 text-neutral-300 hover:text-neutral-100 hover:bg-neutral-700/60 transition-colors truncate max-w-[200px] min-h-[44px] flex items-center"
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
        </div>
      )}
    </section>
  );
}
