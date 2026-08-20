import { useState, useEffect, useCallback } from 'react';
import {
  Activity,
  AlertTriangle,
  HardDrive,
  MemoryStick,
  Monitor,
  RefreshCw,
  Cpu,
  Server,
} from 'lucide-react';
import { fetchFleetHealthSummary } from '../api';
import type { FleetHealthSummaryResponse, FleetRunner } from '../api';

const AUTO_REFRESH_INTERVAL_MS = 30_000;

function statusColor(status: string | undefined): string {
  if (status === 'connected') return 'text-emerald-400';
  if (status === 'degraded') return 'text-amber-400';
  if (status === 'stale') return 'text-amber-400/70';
  if (status === 'offline') return 'text-neutral-500';
  return 'text-neutral-500';
}

function statusBorderColor(status: string | undefined): string {
  if (status === 'connected') return 'border-emerald-800/40';
  if (status === 'degraded') return 'border-amber-700/50';
  if (status === 'stale') return 'border-amber-800/30';
  if (status === 'offline') return 'border-neutral-700/40';
  return 'border-neutral-700/40';
}

function statusLabel(status: string | undefined): string {
  if (status === 'connected') return 'Online';
  if (status === 'degraded') return 'Failing';
  if (status === 'stale') return 'Stale';
  if (status === 'offline') return 'Offline';
  return status ?? 'Unknown';
}

function formatBytes(bytes: number | null | undefined): string {
  if (bytes == null) return '—';
  if (bytes >= 1_073_741_824) return `${(bytes / 1_073_741_824).toFixed(1)} GB`;
  if (bytes >= 1_048_576) return `${(bytes / 1_048_576).toFixed(0)} MB`;
  return `${(bytes / 1024).toFixed(0)} KB`;
}

function formatDuration(seconds: number | null | undefined): string {
  if (seconds == null) return 'never';
  if (seconds < 60) return `${Math.round(seconds)}s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  return `${Math.round(seconds / 3600)}h ago`;
}

function FleetSummaryCard({ data }: { data: FleetHealthSummaryResponse }) {
  const lowMemory = data.low_memory ?? 0;
  const hasIssues = data.degraded > 0 || data.offline > 0 || data.stale > 0 || data.low_disk > 0 || lowMemory > 0;
  const hasCritical = data.offline > 0 || data.low_disk > 0 || lowMemory > 0;

  return (
    <div className="bg-workbench-base/60 border border-neutral-800/60 rounded-lg p-4">
      <div className="flex items-center gap-2 mb-3">
        {hasCritical ? (
          <AlertTriangle className="w-4 h-4 text-amber-400 shrink-0" />
        ) : hasIssues ? (
          <Monitor className="w-4 h-4 text-amber-400 shrink-0" />
        ) : (
          <Activity className="w-4 h-4 text-emerald-400 shrink-0" />
        )}
        <h2 className="text-sm font-bold text-neutral-300 uppercase tracking-widest">
          Fleet Health
        </h2>
        <span className="text-xs text-neutral-500">
          {data.total} runner{data.total !== 1 ? 's' : ''} across{' '}
          {data.project_count} project{data.project_count !== 1 ? 's' : ''}
        </span>
      </div>

      <div className="flex flex-wrap gap-4">
        <div className="flex flex-col items-center">
          <span className="text-2xl font-mono font-bold text-emerald-400">{data.connected}</span>
          <span className="text-xs text-neutral-500">online</span>
        </div>
        {data.degraded > 0 && (
          <div className="flex flex-col items-center">
            <span className="text-2xl font-mono font-bold text-amber-400">{data.degraded}</span>
            <span className="text-xs text-neutral-500">failing</span>
          </div>
        )}
        {data.stale > 0 && (
          <div className="flex flex-col items-center">
            <span className="text-2xl font-mono font-bold text-amber-400/70">{data.stale}</span>
            <span className="text-xs text-neutral-500">stale</span>
          </div>
        )}
        {data.offline > 0 && (
          <div className="flex flex-col items-center">
            <span className="text-2xl font-mono font-bold text-neutral-500">{data.offline}</span>
            <span className="text-xs text-neutral-500">offline</span>
          </div>
        )}
        {data.low_disk > 0 && (
          <div className="flex flex-col items-center">
            <span className="text-2xl font-mono font-bold text-red-400">{data.low_disk}</span>
            <span className="text-xs text-neutral-500 flex items-center gap-0.5">
              <HardDrive className="w-3 h-3" /> low disk
            </span>
          </div>
        )}
        {lowMemory > 0 && (
          <div className="flex flex-col items-center">
            <span className="text-2xl font-mono font-bold text-red-400">{lowMemory}</span>
            <span className="text-xs text-neutral-500 flex items-center gap-0.5">
              <MemoryStick className="w-3 h-3" /> low mem
            </span>
          </div>
        )}
      </div>
    </div>
  );
}

function RunnerCard({ runner }: { runner: FleetRunner }) {
  const health = runner.health;
  const status = health?.device_status;
  const caps = runner.capabilities ?? {};

  const diskFree = health?.disk_free_bytes ?? caps.disk_free_bytes;
  const memFree = health?.memory_free_bytes ?? caps.memory_free_bytes;
  const cpuLoad = health?.cpu_load_avg ?? caps.cpu_load_avg;
  const gpuPct = health?.gpu_utilization_pct ?? caps.gpu_utilization_pct;

  return (
    <div
      className={`bg-workbench-base/50 border rounded-lg p-3 flex flex-col gap-2 ${statusBorderColor(status)}`}
    >
      {/* Header row */}
      <div className="flex items-start gap-2">
        <Server className={`w-3.5 h-3.5 mt-0.5 shrink-0 ${statusColor(status)}`} />
        <div className="min-w-0 flex-1">
          <div className="text-sm font-mono text-neutral-200 truncate">
            {runner.hostname ?? runner.id.slice(0, 12)}
          </div>
          {runner.version && (
            <div className="text-xs text-neutral-600 truncate">v{runner.version}</div>
          )}
        </div>
        <span className={`text-xs font-semibold shrink-0 ${statusColor(status)}`}>
          {statusLabel(status)}
        </span>
      </div>

      {/* Liveness */}
      <div className="text-xs text-neutral-500">
        {health?.seconds_since_last_seen != null
          ? `Last seen ${formatDuration(health.seconds_since_last_seen)}`
          : 'Last seen: unknown'}
      </div>

      {/* Active work */}
      {(health?.active_task_count != null || health?.active_compute_job_count != null) && (
        <div className="text-xs text-neutral-400">
          {health.active_task_count ?? 0} task{(health.active_task_count ?? 0) !== 1 ? 's' : ''}
          {health.active_compute_job_count != null && ` · ${health.active_compute_job_count} compute`}
          {health.available_slots != null && (
            <span className="text-neutral-600"> · {health.available_slots} free</span>
          )}
        </div>
      )}

      {/* Resource telemetry */}
      <div className="flex flex-wrap gap-x-3 gap-y-0.5 text-xs">
        {cpuLoad != null && (
          <span className="flex items-center gap-1 text-neutral-400">
            <Cpu className="w-3 h-3 shrink-0" />
            {cpuLoad.toFixed(2)}
          </span>
        )}
        {memFree != null && (
          <span className={`flex items-center gap-1 ${health?.memory_low ? 'text-red-400' : 'text-neutral-400'}`}>
            <MemoryStick className="w-3 h-3 shrink-0" />
            {formatBytes(memFree)} free
          </span>
        )}
        {diskFree != null && (
          <span className={`flex items-center gap-1 ${health?.disk_low ? 'text-red-400' : 'text-neutral-400'}`}>
            <HardDrive className="w-3 h-3 shrink-0" />
            {formatBytes(diskFree)} free
          </span>
        )}
        {gpuPct != null && (
          <span className="flex items-center gap-1 text-neutral-400">
            GPU {gpuPct.toFixed(0)}%
          </span>
        )}
      </div>

      {/* Alerts */}
      {(health?.consecutive_job_failures ?? 0) > 0 && (
        <div className="text-xs text-amber-400">
          {health?.consecutive_job_failures ?? 0} consecutive failure{(health?.consecutive_job_failures ?? 0) !== 1 ? 's' : ''}
        </div>
      )}
    </div>
  );
}

export function FleetRunnersDashboard() {
  const [data, setData] = useState<FleetHealthSummaryResponse | null>(null);
  // Start as true so the skeleton renders immediately on mount without a flash.
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [lastRefreshed, setLastRefreshed] = useState<Date | null>(null);
  // Incrementing this triggers a fresh fetch cycle.
  const [refreshKey, setRefreshKey] = useState(0);

  const refresh = useCallback(() => {
    setRefreshKey(k => k + 1);
  }, []);

  useEffect(() => {
    let cancelled = false;

    async function fetchData() {
      setError(null);
      try {
        const result = await fetchFleetHealthSummary();
        if (!cancelled) {
          setData(result);
          setLastRefreshed(new Date());
        }
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : 'Failed to load fleet data');
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    }

    fetchData();
    const interval = setInterval(fetchData, AUTO_REFRESH_INTERVAL_MS);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, [refreshKey]);

  return (
    <div className="flex flex-col h-full overflow-hidden bg-workbench-shell">
      {/* Page header */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-neutral-800/60 shrink-0">
        <div className="flex items-center gap-2">
          <Monitor className="w-4 h-4 text-neutral-400" />
          <h1 className="text-sm font-bold text-neutral-300 uppercase tracking-widest">
            Runner Fleet
          </h1>
        </div>
        <div className="flex items-center gap-3">
          {lastRefreshed && (
            <span className="text-xs text-neutral-600">
              Updated {lastRefreshed.toLocaleTimeString()}
            </span>
          )}
          <button
            type="button"
            onClick={refresh}
            disabled={loading}
            className="flex items-center gap-1.5 px-2 py-1 min-h-[44px] text-xs text-neutral-400 hover:text-neutral-200 disabled:opacity-50 transition-colors"
          >
            <RefreshCw className={`w-3 h-3 ${loading ? 'animate-spin' : ''}`} />
            Refresh
          </button>
        </div>
      </div>

      {/* Body */}
      <div className="flex-1 overflow-y-auto p-4 space-y-6">
        {error && (
          <div className="bg-red-900/20 border border-red-800/40 rounded-lg p-3 text-sm text-red-400">
            {error}
          </div>
        )}

        {loading && !data ? (
          <div className="space-y-4">
            <div className="h-24 bg-workbench-base/50 border border-neutral-800/40 rounded-lg animate-pulse" />
            <div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-3">
              {[...Array(4)].map((_, i) => (
                <div
                  key={i}
                  className="h-32 bg-workbench-base/50 border border-neutral-800/40 rounded-lg animate-pulse"
                />
              ))}
            </div>
          </div>
        ) : data ? (
          <>
            <FleetSummaryCard data={data} />

            {data.total === 0 ? (
              <div className="text-center py-12 text-neutral-500 text-sm">
                No runners registered yet.
              </div>
            ) : (
              <div className="space-y-4">
                {data.per_project.map(project => (
                  <div key={project.project_id}>
                    <div className="flex items-center gap-2 mb-2">
                      <span className="text-xs font-mono text-neutral-500 uppercase tracking-widest">
                        Project {project.project_id.slice(0, 8)}
                      </span>
                      <span className="text-xs text-neutral-600">
                        {project.total} runner{project.total !== 1 ? 's' : ''}
                      </span>
                      {project.degraded > 0 && (
                        <span className="text-xs text-amber-400">{project.degraded} failing</span>
                      )}
                      {project.offline > 0 && (
                        <span className="text-xs text-neutral-500">{project.offline} offline</span>
                      )}
                    </div>
                    <div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-3">
                      {project.runners.map(runner => (
                        <RunnerCard key={runner.id} runner={runner} />
                      ))}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </>
        ) : null}
      </div>
    </div>
  );
}
