import { useState } from 'react';
import { AlertTriangle, BarChart2, CheckCircle2, Clock, ExternalLink, Layers, PlusCircle, WifiOff, Zap } from 'lucide-react';
import { useDashboardDigest } from '../hooks/useDashboardDigest';
import { usePoller, DEFAULT_POLL_MS } from '../hooks/usePoller';
import { fetchFleetUtilization, fetchTask, triggerSelfAudit } from '../api';
import { MetricCard, SuccessRateCard, CostCard } from './DashboardMetricCards';
import { Skeleton, FAILURE_CLASS_COLORS } from './DashboardMetricCardsPrimitives';
import { FleetCapacityCard, type FleetUtilizationResponse } from './FleetCapacityCard';
import { SystemStatusCard } from './SystemStatusCard';
import { timeAgo } from '../utils/time';
import type { NeedsAttentionItem, RecentCompletion } from '../types/digest';
import type { Task } from '../types/task';

// Per-reason presentation for a Needs-Attention row. `failed` is an error the
// operator must investigate; `approval_required` is a decision blocking the
// pipeline. Both get a distinct severity colour, a human label, and an
// explicit "what to inspect next" hint so the row is actionable on its own.
function attentionMeta(item: NeedsAttentionItem): {
  label: string;
  dotClass: string;
  reasonClass: string;
  inspectHint: string;
} {
  if (item.reason === 'approval_required') {
    return {
      label: 'Awaiting your approval',
      dotClass: 'bg-amber-400',
      reasonClass: 'text-amber-400/90',
      inspectHint:
        item.spend_cap_usd != null || item.deadline_seconds != null
          ? 'Open to approve, adjust the cap, or reject'
          : 'Open to approve or reject',
    };
  }
  // Default / `failed`: a task failed and needs investigation.
  return {
    label: 'Failed — needs investigation',
    dotClass: 'bg-red-500',
    reasonClass: 'text-red-400/90',
    inspectHint: item.pr_url
      ? 'Open to inspect the error and PR diff'
      : 'Open to inspect the error and retry',
  };
}

interface ConsoleHomeProps {
  selectedProjectId: string | null;
  scopedActiveCount?: number | null;
  scopedQueuedCount?: number | null;
  globalActiveCount?: number;
  globalQueuedCount?: number;
  tasks: Task[];
  onViewTask: (task: Task) => void;
  onShowAnalytics: () => void;
  onOpenFleet?: () => void;
  onCreateTask?: () => void;
  isAdmin?: boolean;
}

function NeedsAttentionSection({
  items,
  loading,
  ready,
  onViewTask,
}: {
  items: NeedsAttentionItem[] | null | undefined;
  loading: boolean;
  // True once a digest has actually been received, so an empty list reads as a
  // genuine "all clear" rather than "not loaded yet".
  ready: boolean;
  onViewTask: (id: string) => void;
}) {
  if (loading) return <Skeleton className="h-20 w-full" />;

  const list = items ?? [];
  const header = (
    <h2 className="text-ui-md font-bold text-neutral-400 uppercase tracking-widest mb-2 flex items-center gap-1.5">
      <AlertTriangle className="w-3 h-3 text-amber-400" />
      Needs Attention
      {list.length > 0 && (
        <span className="text-amber-400 font-mono">{list.length}</span>
      )}
    </h2>
  );

  // Explicit empty state: only show "all clear" once data has arrived, so the
  // operator never confuses an unloaded panel with a quiet, healthy queue.
  if (list.length === 0) {
    if (!ready) return null;
    return (
      <section>
        {header}
        <div className="flex items-center gap-2 px-3 py-2 bg-neutral-900/50 border border-neutral-800/50 rounded-lg text-ui-sm text-neutral-500">
          <CheckCircle2 className="w-3.5 h-3.5 text-emerald-400/80 shrink-0" />
          Nothing needs your attention — no failed tasks or pending approvals.
        </div>
      </section>
    );
  }

  return (
    <section>
      {header}
      <div className="space-y-1.5">
        {list.slice(0, 8).map(item => {
          const meta = attentionMeta(item);
          const failClass = item.reason === 'failed' ? item.failure_class : null;
          return (
            <button
              key={item.task_id}
              onClick={() => onViewTask(item.task_id)}
              className="w-full text-left flex items-start gap-2 px-3 py-2 bg-neutral-900/50 border border-neutral-800/50 rounded-lg hover:bg-neutral-800/50 transition-colors"
            >
              <span className={`mt-1 w-1.5 h-1.5 rounded-full shrink-0 ${meta.dotClass}`} aria-hidden="true" />
              <div className="flex-1 min-w-0">
                <p className="text-xs text-neutral-300 truncate">{item.title ?? item.task_id}</p>
                <div className="flex flex-wrap items-center gap-1.5 mt-0.5">
                  <span className={`text-ui-sm ${meta.reasonClass}`}>{meta.label}</span>
                  {failClass && (
                    <span className={`text-ui-xs px-1.5 py-0.5 rounded font-medium ${FAILURE_CLASS_COLORS[failClass] ?? 'bg-neutral-500/20 text-neutral-400'}`}>
                      {failClass.toLowerCase()}
                    </span>
                  )}
                  {item.scope && (
                    <span className="text-ui-xs px-1.5 py-0.5 rounded border border-neutral-700/60 text-neutral-400">
                      {item.scope}
                    </span>
                  )}
                </div>
                {item.error_snippet && (
                  <p className="text-ui-sm text-neutral-500 mt-0.5 truncate font-mono">{item.error_snippet}</p>
                )}
                {(item.spend_cap_usd != null || item.deadline_seconds != null) && (
                  <p className="text-ui-sm text-neutral-400 mt-0.5">
                    {item.spend_cap_usd != null && <span>Cap: ${item.spend_cap_usd.toFixed(2)}</span>}
                    {item.spend_cap_usd != null && item.deadline_seconds != null && <span className="mx-1">·</span>}
                    {item.deadline_seconds != null && (
                      <span>
                        Deadline: {item.deadline_seconds < 3600
                          ? `${Math.round(item.deadline_seconds / 60)}m`
                          : `${Math.round(item.deadline_seconds / 3600)}h`}
                      </span>
                    )}
                  </p>
                )}
                <p className="text-ui-sm text-neutral-600 mt-0.5">{meta.inspectHint}</p>
              </div>
              {item.waiting_hours != null && item.waiting_hours > 0 && (
                <span className="shrink-0 flex items-center gap-1 text-ui-sm text-neutral-500">
                  <Clock className="w-3 h-3" />
                  {item.waiting_hours < 24 ? `${item.waiting_hours}h` : `${Math.floor(item.waiting_hours / 24)}d`}
                </span>
              )}
            </button>
          );
        })}
      </div>
    </section>
  );
}

// "What changed" — the most recent merges/completions, so the operator can see
// at a glance what shipped (and what fell open unverified) without leaving Home.
function RecentlyShippedSection({
  items,
  ready,
  onViewTask,
}: {
  items: RecentCompletion[] | null | undefined;
  ready: boolean;
  onViewTask: (id: string) => void;
}) {
  const list = items ?? [];
  if (!ready || list.length === 0) return null;

  return (
    <section>
      <h2 className="text-ui-md font-bold text-neutral-400 uppercase tracking-widest mb-2 flex items-center gap-1.5">
        <CheckCircle2 className="w-3 h-3 text-emerald-400" />
        Recently Shipped
      </h2>
      <div className="space-y-1.5">
        {list.slice(0, 5).map(item => (
          <button
            key={item.task_id}
            onClick={() => onViewTask(item.task_id)}
            className="w-full text-left flex items-start gap-2 px-3 py-2 bg-neutral-900/50 border border-neutral-800/50 rounded-lg hover:bg-neutral-800/50 transition-colors"
          >
            <CheckCircle2 className="w-3.5 h-3.5 text-emerald-400/80 shrink-0 mt-0.5" aria-hidden="true" />
            <div className="flex-1 min-w-0">
              <p className="text-xs text-neutral-300 truncate">{item.title}</p>
              <div className="flex flex-wrap items-center gap-1.5 mt-0.5 text-ui-sm text-neutral-500">
                <span className="text-neutral-400">{item.task_type}</span>
                {item.unverified_merge_reason && (
                  <span
                    className="text-ui-xs px-1.5 py-0.5 rounded border border-amber-500/30 bg-amber-500/10 text-amber-400"
                    title="Merged without a verifying test suite"
                  >
                    unverified
                  </span>
                )}
                {item.pr_url && (
                  <span className="inline-flex items-center gap-0.5 text-blue-400">
                    PR <ExternalLink className="w-2.5 h-2.5" />
                  </span>
                )}
              </div>
            </div>
            {item.completed_at && (
              <span className="shrink-0 text-ui-sm text-neutral-500">{timeAgo(item.completed_at)}</span>
            )}
          </button>
        ))}
      </div>
    </section>
  );
}

export function ConsoleHome({
  selectedProjectId,
  scopedActiveCount,
  scopedQueuedCount,
  globalActiveCount,
  globalQueuedCount,
  tasks,
  onViewTask,
  onShowAnalytics,
  onOpenFleet,
  onCreateTask,
  isAdmin = false,
}: ConsoleHomeProps) {
  const pageActiveCount = tasks.filter(t => t.status === 'IN_PROGRESS').length;
  const pageQueuedCount = tasks.filter(t => t.status === 'QUEUED').length;
  const activeCount = selectedProjectId && scopedActiveCount != null ? scopedActiveCount : (globalActiveCount ?? pageActiveCount);
  const queuedCount = selectedProjectId && scopedQueuedCount != null ? scopedQueuedCount : (globalQueuedCount ?? pageQueuedCount);

  const { digest, digestStale, loading, lastFetchError, sparkData } = useDashboardDigest(selectedProjectId, 168, activeCount, queuedCount, isAdmin);
  // True once a digest has actually arrived — lets empty sections distinguish
  // "all clear" from "not loaded / fetch failed".
  const digestReady = digest != null;
  // The live refresh is failing or the server is replaying a last-good cache.
  // Surface this so zeros below aren't mistaken for a healthy, quiet system.
  const dataStale = digestStale || digest?.served_stale === true;

  const [fleetData, setFleetData] = useState<FleetUtilizationResponse | null>(null);
  const [fleetLoading, setFleetLoading] = useState(true);
  usePoller(async (signal) => {
    try {
      const data = await fetchFleetUtilization();
      if (!signal.aborted) { setFleetData(data); setFleetLoading(false); }
    } catch {
      if (!signal.aborted) setFleetLoading(false);
    }
  }, { intervalMs: DEFAULT_POLL_MS, enabled: isAdmin, deps: [isAdmin] });

  const successRate = digest?.success_rate;
  const totalCost = digest?.spot_cost_usd;
  const trendHistory = digest?.trend_history ?? [];

  const [fetchError, setFetchError] = useState<string | null>(null);
  const [auditLoading, setAuditLoading] = useState(false);
  const [auditTaskId, setAuditTaskId] = useState<string | null>(null);
  const [auditError, setAuditError] = useState<string | null>(null);

  const handleRunSelfAudit = async () => {
    if (!selectedProjectId) {
      setAuditError('No project selected');
      return;
    }
    setAuditLoading(true);
    setAuditError(null);
    try {
      const result = await triggerSelfAudit(selectedProjectId);
      setAuditTaskId(result.task_id);
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Failed to start self-audit';
      setAuditError(message);
    } finally {
      setAuditLoading(false);
    }
  };

  const handleViewTaskById = (taskId: string) => {
    const task = tasks.find(t => t.id === taskId);
    if (task) {
      setFetchError(null);
      onViewTask(task);
      return;
    }
    fetchTask(taskId).then(t => {
      setFetchError(null);
      onViewTask(t);
    }).catch(() => {
      setFetchError(`Could not load task ${taskId}.`);
    });
  };

  return (
    <div className="flex-1 overflow-auto custom-scrollbar px-1 py-2 space-y-4">
      {/* Data trust banner: never let a failed/stale fetch read as healthy zeros */}
      {!loading && lastFetchError ? (
        <div className="flex items-center gap-2 px-3 py-2 bg-red-500/10 border border-red-500/30 rounded-lg text-xs text-red-300">
          <WifiOff className="w-3.5 h-3.5 shrink-0" />
          Can't reach the orchestrator — health metrics may be unavailable. Showing what we have.
        </div>
      ) : !loading && dataStale ? (
        <div className="flex items-center gap-2 px-3 py-2 bg-amber-500/10 border border-amber-500/30 rounded-lg text-xs text-amber-300">
          <AlertTriangle className="w-3.5 h-3.5 shrink-0" />
          Live refresh failed — showing last-known data. Counts below may be out of date.
        </div>
      ) : null}

      {/* 4 metric cards */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        <MetricCard label="In progress" value={activeCount} color="emerald" spark={sparkData} dataKey="active" loading={loading} />
        <MetricCard label="Queued" value={queuedCount} color="blue" spark={sparkData} dataKey="queued" loading={loading} />
        <SuccessRateCard
          rate={successRate}
          trendHistory={trendHistory.map(p => ({ success_rate: p.success_rate ?? null, completed: 0, failed: 0 }))}
          loading={loading}
        />
        <div className="cursor-pointer" onClick={onShowAnalytics} title="View Detailed Analytics">
          <CostCard
            totalCost={totalCost}
            trendHistory={trendHistory}
            loading={loading}
          />
        </div>
      </div>

      {/* System status */}
      <SystemStatusCard digest={digest} loading={loading} />

      {/* Task fetch error */}
      {fetchError && (
        <p className="text-xs text-red-400 px-1">{fetchError}</p>
      )}

      {/* Empty-state CTA: shown when nothing is in progress or queued */}
      {!loading && activeCount === 0 && queuedCount === 0 && onCreateTask && (
        <button
          onClick={onCreateTask}
          className="w-full flex items-center gap-3 px-4 py-4 bg-neutral-900/50 border border-dashed border-neutral-700 rounded-xl hover:border-neutral-500 hover:bg-neutral-800/50 transition-colors text-left"
        >
          <PlusCircle className="w-5 h-5 text-neutral-500 shrink-0" />
          <div>
            <p className="text-sm font-medium text-neutral-300">Create your first task</p>
            <p className="text-xs text-neutral-500 mt-0.5">Nothing is in progress yet — launch a task to get started</p>
          </div>
        </button>
      )}

      {/* Needs attention */}
      <NeedsAttentionSection
        items={digest?.needs_attention}
        loading={loading}
        ready={digestReady}
        onViewTask={handleViewTaskById}
      />

      {/* What changed — recent merges/completions */}
      <RecentlyShippedSection
        items={digest?.recent_completions}
        ready={digestReady}
        onViewTask={handleViewTaskById}
      />

      {/* Fleet summary strip — click to open full fleet detail drawer */}
      <div className="relative group">
        <FleetCapacityCard data={fleetData} loading={fleetLoading} />
        {onOpenFleet && (
          // eslint-disable-next-line viewport/min-touch-target
          <button
            onClick={onOpenFleet}
            className="absolute top-2 right-2 flex items-center gap-1 text-ui-sm text-neutral-500 hover:text-neutral-300 transition-colors px-2 py-1 rounded bg-neutral-900/80 border border-neutral-800/60 opacity-0 group-hover:opacity-100"
            title="View full fleet details"
          >
            <Layers className="w-3 h-3" />
            Details
          </button>
        )}
      </div>

      {/* Self-audit section */}
      <div className="space-y-2">
        <button
          onClick={handleRunSelfAudit}
          disabled={auditLoading}
          className="w-full flex items-center gap-2 min-h-11 px-3 py-2 bg-neutral-900/50 border border-neutral-800/50 rounded-lg hover:bg-neutral-800/50 disabled:opacity-50 disabled:cursor-not-allowed transition-colors text-left"
        >
          <Zap className="w-4 h-4 text-neutral-400 shrink-0" />
          <span className="text-xs font-medium text-neutral-300">
            {auditLoading ? 'Running Self-Audit...' : 'Run Self-Audit'}
          </span>
        </button>
        {auditTaskId && (
          <p className="text-xs text-emerald-400 px-1">
            ✓ Self-audit task: <span className="font-mono">{auditTaskId.slice(0, 8)}</span>
          </p>
        )}
        {auditError && (
          <p className="text-xs text-red-400 px-1">{auditError}</p>
        )}
      </div>

      {/* Analytics link */}
      <div className="flex justify-end pt-1">
        <button
          onClick={onShowAnalytics}
          className="flex items-center gap-1.5 min-h-11 text-xs text-neutral-500 hover:text-neutral-300 transition-colors"
        >
          <BarChart2 className="w-3.5 h-3.5" />
          Detailed Analytics
          <ExternalLink className="w-3 h-3" />
        </button>
      </div>
    </div>
  );
}
