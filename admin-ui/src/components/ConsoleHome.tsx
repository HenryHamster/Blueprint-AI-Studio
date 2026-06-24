import { useState } from 'react';
import { AlertTriangle, BarChart2, Clock, ExternalLink, Layers } from 'lucide-react';
import { useDashboardDigest } from '../hooks/useDashboardDigest';
import { usePoller, DEFAULT_POLL_MS } from '../hooks/usePoller';
import { fetchFleetUtilization, fetchTask } from '../api';
import { MetricCard, SuccessRateCard, CostCard, Skeleton } from './DashboardMetricCards';
import { FleetCapacityCard, type FleetUtilizationResponse } from './FleetCapacityCard';
import { SystemStatusCard } from './SystemStatusCard';
import type { NeedsAttentionItem } from '../types/digest';
import type { Task } from '../types/task';

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
}

function NeedsAttentionSection({
  items,
  loading,
  onViewTask,
}: {
  items: NeedsAttentionItem[] | null | undefined;
  loading: boolean;
  onViewTask: (id: string) => void;
}) {
  if (loading) return <Skeleton className="h-20 w-full" />;
  if (!items || items.length === 0) return null;

  return (
    <section>
      <h2 className="text-ui-md font-bold text-neutral-400 uppercase tracking-widest mb-2 flex items-center gap-1.5">
        <AlertTriangle className="w-3 h-3 text-amber-400" />
        Needs Attention
      </h2>
      <div className="space-y-1.5">
        {items.slice(0, 8).map(item => (
          <button
            key={item.task_id}
            onClick={() => onViewTask(item.task_id)}
            className="w-full text-left flex items-start gap-2 px-3 py-2 bg-neutral-900/50 border border-neutral-800/50 rounded-lg hover:bg-neutral-800/50 transition-colors"
          >
            <div className="flex-1 min-w-0">
              <p className="text-xs text-neutral-300 truncate">{item.title ?? item.task_id}</p>
              <p className="text-ui-sm text-amber-400/80 mt-0.5 truncate">{item.reason}</p>
              {item.error_snippet && (
                <p className="text-ui-sm text-neutral-500 mt-0.5 truncate font-mono">{item.error_snippet}</p>
              )}
            </div>
            {item.waiting_hours != null && item.waiting_hours > 0 && (
              <span className="shrink-0 flex items-center gap-1 text-ui-sm text-neutral-500">
                <Clock className="w-3 h-3" />
                {item.waiting_hours < 24 ? `${item.waiting_hours}h` : `${Math.floor(item.waiting_hours / 24)}d`}
              </span>
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
}: ConsoleHomeProps) {
  const pageActiveCount = tasks.filter(t => t.status === 'IN_PROGRESS' || t.status === 'PROVISIONING').length;
  const pageQueuedCount = tasks.filter(t => t.status === 'QUEUED').length;
  const activeCount = selectedProjectId && scopedActiveCount != null ? scopedActiveCount : (globalActiveCount ?? pageActiveCount);
  const queuedCount = selectedProjectId && scopedQueuedCount != null ? scopedQueuedCount : (globalQueuedCount ?? pageQueuedCount);

  const { digest, loading, sparkData } = useDashboardDigest(selectedProjectId, 168, activeCount, queuedCount);

  const [fleetData, setFleetData] = useState<FleetUtilizationResponse | null>(null);
  const [fleetLoading, setFleetLoading] = useState(true);
  usePoller(async (signal) => {
    try {
      const data = await fetchFleetUtilization();
      if (!signal.aborted) { setFleetData(data); setFleetLoading(false); }
    } catch {
      if (!signal.aborted) setFleetLoading(false);
    }
  }, { intervalMs: DEFAULT_POLL_MS, enabled: true, deps: [] });

  const successRate = digest?.success_rate;
  const totalCost = digest?.spot_cost_usd;
  const trendHistory = digest?.trend_history ?? [];

  const [fetchError, setFetchError] = useState<string | null>(null);

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
      {/* 4 metric cards */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        <MetricCard label="Active" value={activeCount} color="emerald" spark={sparkData} dataKey="active" loading={loading} />
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

      {/* Needs attention */}
      <NeedsAttentionSection
        items={digest?.needs_attention}
        loading={loading}
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
