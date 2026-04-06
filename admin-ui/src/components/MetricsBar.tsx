import type { RefObject } from 'react';
import { RefreshCw } from 'lucide-react';
import { AutonomousStatus } from './AutonomousStatus';
import { COST_PER_VM_HR } from '../api';

export interface MetricsBarProps {
  globalActiveCount: number;
  maxConcurrentVMs: number;
  globalEvaluatingCount: number;
  queuedTasksLength: number;
  lastRefresh: Date | null;
  onRefresh: () => void;
  showMetrics: boolean;
  onToggleMetrics: () => void;
  wsRef: RefObject<WebSocket | null>;
  onRefreshTasks: () => void;
  metricsHistory: { timestamp: number; completedCount: number }[];
  pauseReason?: string | null;
}

function formatDrainTime(minutes: number): string {
  if (minutes < 60) return `${Math.round(minutes)}m`;
  const h = Math.floor(minutes / 60);
  const m = Math.round(minutes % 60);
  return m > 0 ? `${h}h ${m}m` : `${h}h`;
}

function computeDrainMinutes(
  metricsHistory: { timestamp: number; completedCount: number }[],
  queued: number,
): number | null {
  if (queued <= 0 || metricsHistory.length < 2) return null;
  const now = Date.now();
  const oneHourAgo = now - 3600000;
  const recent = metricsHistory.filter(p => p.timestamp >= oneHourAgo);
  if (recent.length < 2) return null;
  const oldest = recent[0];
  const newest = recent[recent.length - 1];
  const elapsedHr = (newest.timestamp - oldest.timestamp) / 3600000;
  if (elapsedHr <= 0) return null;
  const completedDelta = newest.completedCount - oldest.completedCount;
  if (completedDelta <= 0) return null;
  const ratePerHour = completedDelta / elapsedHr;
  return (queued / ratePerHour) * 60;
}

function formatTimeAgo(date: Date): string {
  const seconds = Math.floor((Date.now() - date.getTime()) / 1000);
  if (seconds < 10) return 'just now';
  if (seconds < 60) return `${Math.floor(seconds / 10) * 10}s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  return `${Math.floor(minutes / 60)}h ago`;
}

export function MetricsBar({
  globalActiveCount,
  maxConcurrentVMs,
  globalEvaluatingCount,
  queuedTasksLength,
  lastRefresh,
  onRefresh,
  showMetrics,
  onToggleMetrics,
  wsRef,
  onRefreshTasks,
  metricsHistory,
  pauseReason,
}: MetricsBarProps) {
  const drainMinutes = computeDrainMinutes(metricsHistory, queuedTasksLength);
  return (
    <div
      role="button"
      tabIndex={0}
      onClick={onToggleMetrics}
      onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onToggleMetrics(); } }}
      aria-expanded={showMetrics}
      className="flex items-center gap-8 px-6 py-2 bg-neutral-900/30 rounded-lg border border-neutral-800/50 cursor-pointer hover:bg-neutral-900/50 transition-colors"
      title="Toggle metrics dashboard"
    >
      <div className="flex flex-col items-center" title={`${globalActiveCount} VMs active out of ${maxConcurrentVMs} configured maximum`}>
        <span className="text-[10px] text-neutral-500 font-semibold uppercase tracking-wider mb-1">Active VMs</span>
        <span className="text-xl font-bold text-emerald-400 font-mono leading-none flex items-center gap-1.5">
          {globalActiveCount > 0 && <span className="w-2 h-2 rounded-full bg-emerald-500 animate-pulse"></span>}
          {globalActiveCount}<span className="text-xs text-neutral-600 font-sans">/{maxConcurrentVMs}</span>
        </span>
      </div>
      <div className="w-px h-8 bg-neutral-800"></div>
      <div
        className="flex flex-col items-center"
        title={`e2-standard-2 VM cost: $${COST_PER_VM_HR}/hr per VM`}
      >
        <span className="text-[10px] text-neutral-500 font-semibold uppercase tracking-wider mb-1">VM Cost/hr</span>
        <span className="text-xl font-bold text-neutral-200 font-mono leading-none">
          ${(globalActiveCount * COST_PER_VM_HR).toFixed(2)}<span className="text-sm text-neutral-400 font-sans">/hr</span>
        </span>
      </div>
      <div className="w-px h-8 bg-neutral-800"></div>
      <div className="flex flex-col items-center">
        <span className="text-[10px] text-neutral-500 font-semibold uppercase tracking-wider mb-1">Evaluating</span>
        <span className="text-xl font-bold text-amber-400 font-mono leading-none">{globalEvaluatingCount}</span>
      </div>
      <div className="w-px h-8 bg-neutral-800"></div>
      <div className="flex flex-col items-center">
        <span className="text-[10px] text-neutral-500 font-semibold uppercase tracking-wider mb-1">Queue</span>
        <span className="text-xl font-bold text-neutral-200 font-mono leading-none">{queuedTasksLength}</span>
        {drainMinutes !== null && (
          <span className="text-xs text-neutral-500 font-mono mt-0.5">Est. drain: {formatDrainTime(drainMinutes)}</span>
        )}
      </div>
      <div className="w-px h-8 bg-neutral-800"></div>
      <button className="flex flex-col items-center group" onClick={(e) => { e.stopPropagation(); onRefresh(); }} title="Click to refresh">
        <span className="text-[10px] text-neutral-500 font-semibold uppercase tracking-wider mb-1">Refreshed</span>
        <span className="text-xs font-medium text-neutral-400 font-mono group-hover:text-neutral-200 transition-colors flex items-center gap-1.5">
          <RefreshCw className="w-3 h-3" />
          {lastRefresh ? formatTimeAgo(lastRefresh) : '...'}
        </span>
      </button>
      <div className="w-px h-8 bg-neutral-800"></div>
      <span onClick={(e) => e.stopPropagation()}>
        <AutonomousStatus onRefreshTasks={onRefreshTasks} wsRef={wsRef} pauseReason={pauseReason} />
      </span>
      <div className="w-px h-8 bg-neutral-800"></div>
      <span className="text-neutral-500 text-xs transition-transform" style={{ transform: showMetrics ? 'rotate(180deg)' : 'rotate(0deg)' }}>▾</span>
    </div>
  );
}
