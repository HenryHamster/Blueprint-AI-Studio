import { useState } from 'react';
import { Activity, ChevronDown, ChevronRight, Monitor, AlertTriangle, HardDrive, MemoryStick } from 'lucide-react';
import type { FleetHealthSummaryResponse } from '../api';

function statusColor(status: string): string {
  if (status === 'connected') return 'text-emerald-400';
  if (status === 'degraded') return 'text-amber-400';
  if (status === 'offline') return 'text-neutral-500';
  if (status === 'stale') return 'text-amber-400';
  return 'text-neutral-500';
}

function statusLabel(status: string): string {
  if (status === 'connected') return 'Online';
  if (status === 'degraded') return 'Failing';
  if (status === 'offline') return 'Offline';
  if (status === 'stale') return 'Stale';
  return status;
}

export function FleetHealthCard({
  data,
  loading,
}: {
  data: FleetHealthSummaryResponse | null;
  loading: boolean;
}) {
  const [expandedProject, setExpandedProject] = useState<string | null>(null);

  if (loading) {
    return (
      <div className="bg-workbench-base/50 border border-neutral-800/50 rounded-lg p-3">
        <div className="flex items-center gap-2 mb-2">
          <Activity className="w-3.5 h-3.5 text-neutral-500 shrink-0" />
          <h3 className="text-ui-md font-bold text-neutral-500 uppercase tracking-widest">Fleet Health</h3>
        </div>
        <div className="animate-pulse space-y-2">
          <div className="h-8 bg-workbench-panel/50 rounded w-1/2" />
          <div className="h-4 bg-workbench-panel/50 rounded w-3/4" />
        </div>
      </div>
    );
  }

  if (!data || data.total === 0) {
    return (
      <div className="bg-workbench-base/50 border border-neutral-800/50 rounded-lg p-3">
        <div className="flex items-center gap-2 mb-2">
          <Activity className="w-3.5 h-3.5 text-neutral-500 shrink-0" />
          <h3 className="text-ui-md font-bold text-neutral-500 uppercase tracking-widest">Fleet Health</h3>
        </div>
        <div className="text-ui-sm text-neutral-500">
          {!data ? 'Fleet health data unavailable' : 'No runners registered'}
        </div>
      </div>
    );
  }

  const lowMemory = data.low_memory ?? 0;
  const hasIssues =
    data.degraded > 0 || data.offline > 0 || data.stale > 0 || data.low_disk > 0 || lowMemory > 0;
  const hasCritical = data.offline > 0 || data.low_disk > 0 || lowMemory > 0;

  return (
    <div className="bg-workbench-base/50 border border-neutral-800/50 rounded-lg p-3">
      <div className="flex items-center gap-2 mb-2">
        {hasCritical ? (
          <AlertTriangle className="w-3.5 h-3.5 text-amber-400 shrink-0" />
        ) : hasIssues ? (
          <Monitor className="w-3.5 h-3.5 text-amber-400 shrink-0" />
        ) : (
          <Activity className="w-3.5 h-3.5 text-emerald-400 shrink-0" />
        )}
        <h3 className="text-ui-md font-bold text-neutral-400 uppercase tracking-widest">Fleet Health</h3>
        <span className="text-ui-sm text-neutral-500">
          {data.total} runner{data.total === 1 ? '' : 's'} · {data.project_count} project{data.project_count === 1 ? '' : 's'}
        </span>
      </div>

      {/* Status breakdown */}
      <div className="flex flex-wrap gap-x-3 gap-y-1 text-ui-sm">
        <span className={statusColor('connected')}>
          <span className="font-mono font-bold">{data.connected}</span> online
        </span>
        {data.degraded > 0 && (
          <span className={statusColor('degraded')}>
            <span className="font-mono font-bold">{data.degraded}</span> failing
          </span>
        )}
        {data.stale > 0 && (
          <span className={statusColor('stale')}>
            <span className="font-mono font-bold">{data.stale}</span> stale
          </span>
        )}
        {data.offline > 0 && (
          <span className={statusColor('offline')}>
            <span className="font-mono font-bold">{data.offline}</span> offline
          </span>
        )}
        {data.low_disk > 0 && (
          <span className="text-red-400 inline-flex items-center gap-1">
            <HardDrive className="w-3 h-3" />
            <span className="font-mono font-bold">{data.low_disk}</span> low disk
          </span>
        )}
        {lowMemory > 0 && (
          <span className="text-red-400 inline-flex items-center gap-1">
            <MemoryStick className="w-3 h-3" />
            <span className="font-mono font-bold">{lowMemory}</span> low memory
          </span>
        )}
      </div>

      {/* Per-project drill-down */}
      {data.per_project.length > 0 && (
        <div className="mt-2 border-t border-neutral-800/70 pt-2">
          <div className="text-ui-sm font-bold text-neutral-500 uppercase tracking-widest mb-1.5">By Project</div>
          <div className="space-y-1">
            {data.per_project.map(proj => (
              <div key={proj.project_id}>
                <button
                  onClick={() => setExpandedProject(expandedProject === proj.project_id ? null : proj.project_id)}
                  className="flex items-center gap-1.5 w-full text-left text-ui-sm hover:bg-workbench-hover/50 rounded px-1.5 py-0.5 transition-colors min-h-[44px]"
                >
                  {expandedProject === proj.project_id ? (
                    <ChevronDown className="w-3 h-3 shrink-0 text-neutral-400" />
                  ) : (
                    <ChevronRight className="w-3 h-3 shrink-0 text-neutral-400" />
                  )}
                  <span className="font-mono text-neutral-300 truncate">{proj.project_id.slice(0, 8)}</span>
                  <span className="text-neutral-500">
                    {proj.total} runner{proj.total === 1 ? '' : 's'}
                  </span>
                  <div className="flex gap-1.5 ml-auto text-ui-sm">
                    <span className={statusColor('connected')}>{proj.connected}</span>
                    {proj.degraded > 0 && <span className={statusColor('degraded')}>{proj.degraded}</span>}
                    {proj.offline > 0 && <span className={statusColor('offline')}>{proj.offline}</span>}
                  </div>
                </button>
                {expandedProject === proj.project_id && (
                  <div className="ml-6 mt-0.5 space-y-0.5">
                    {proj.runners.map(runner => {
                      const deviceStatus = runner.health?.device_status ?? 'unknown';
                      return (
                        <div
                          key={runner.id}
                          className="flex items-center gap-2 text-ui-sm rounded px-1.5 py-0.5"
                        >
                          <span className={statusColor(deviceStatus)}>●</span>
                          <span className="text-neutral-300 truncate flex-1">
                            {runner.hostname ?? runner.id.slice(0, 8)}
                          </span>
                          <span className={`text-ui-sm ${statusColor(deviceStatus)}`}>
                            {statusLabel(deviceStatus)}
                          </span>
                          {runner.health?.disk_low && (
                            <HardDrive className="w-3 h-3 text-red-400 shrink-0" aria-label="Low disk" />
                          )}
                          {runner.health?.memory_low && (
                            <MemoryStick className="w-3 h-3 text-red-400 shrink-0" aria-label="Low memory" />
                          )}
                          {runner.health?.consecutive_job_failures != null && runner.health.consecutive_job_failures > 0 && (
                            <span className="text-amber-400/80">
                              {runner.health.consecutive_job_failures} fail
                            </span>
                          )}
                        </div>
                      );
                    })}
                  </div>
                )}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
