import { useEffect, useMemo, useState } from 'react';
import type { Task } from './TaskCard';

interface OrchestratorHealth {
  last_cycle_seconds_ago: number | null;
  queued_tasks: number;
  active_tasks: number;
  pool_info: Record<string, unknown>;
  deploy_monitoring?: {
    external_health?: Record<string, boolean | string | string[]>;
  };
}

interface OrchestratorHealthBarProps {
  health: OrchestratorHealth | null;
  apiHealthy: boolean;
  wsConnected: boolean;
  lastFetchTime: number;
  tasks: Task[];
}

const DEP_LABELS: Record<string, string> = {
  github_api: 'GitHub',
  db: 'DB',
  gcp: 'GCP',
};

function statusColor(val: boolean | string): string {
  if (typeof val === 'boolean') return val ? 'bg-emerald-500' : 'bg-red-500';
  const s = val.toLowerCase();
  if (s === 'ok' || s === 'healthy') return 'bg-emerald-500';
  if (s === 'degraded' || s === 'slow') return 'bg-amber-400';
  return 'bg-red-500';
}

function statusDotTitle(key: string, val: boolean | string): string {
  const label = DEP_LABELS[key] ?? key;
  if (typeof val === 'boolean') return `${label}: ${val ? 'ok' : 'down'}`;
  return `${label}: ${val}`;
}

function relativeTime(ts: number, now: number): string {
  if (!ts) return '—';
  const sec = Math.floor((now - ts) / 1000);
  if (sec < 10) return 'just now';
  if (sec < 60) return `${sec}s ago`;
  return `${Math.floor(sec / 60)}m ago`;
}

export function OrchestratorHealthBar({ health, apiHealthy, wsConnected, lastFetchTime, tasks }: OrchestratorHealthBarProps) {
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);

  const staleHeartbeatCount = useMemo(() => {
    const fiveMinutesAgo = now - 5 * 60 * 1000;
    return tasks.filter(t =>
      t.status === 'IN_PROGRESS' &&
      t.last_heartbeat &&
      new Date(t.last_heartbeat).getTime() < fiveMinutesAgo
    ).length;
  }, [tasks, now]);

  const cycleAge = health?.last_cycle_seconds_ago ?? null;
  const cycleStale = cycleAge !== null && cycleAge > 120;

  const externalHealth = health?.deploy_monitoring?.external_health ?? {};
  const orderedDeps = (['github_api', 'db', 'gcp'] as const).filter(k => k in externalHealth);

  return (
    <div className={`h-7 flex items-center justify-between px-3 bg-neutral-900 border border-neutral-800 rounded-lg text-xs mb-3 shrink-0${apiHealthy ? '' : ' bg-red-950/20'}`}>
      {/* Left: API + WS status */}
      <div className="flex items-center gap-3">
        <div className="flex items-center gap-1.5" title={apiHealthy ? undefined : 'API connection error — data may be stale'}>
          <span className={`w-1.5 h-1.5 rounded-full ${apiHealthy ? 'bg-emerald-500' : 'bg-red-500 animate-pulse'}`} />
          <span className={apiHealthy ? 'text-neutral-400' : 'text-red-400'}>
            {apiHealthy ? 'Connected' : 'API Error'}
          </span>
        </div>
        <div className="flex items-center gap-1.5">
          <span className={`w-1.5 h-1.5 rounded-full ${wsConnected ? 'bg-blue-500' : 'bg-neutral-600'}`} />
          <span className={wsConnected ? 'text-blue-400' : 'text-neutral-500'}>
            WS
          </span>
        </div>
      </div>

      {/* Center: Orchestrator cycle + task counts + deps */}
      <div className="flex items-center gap-3">
        {health && (
          <>
            <div className="flex items-center gap-1.5" title="Time since last orchestrator cycle">
              <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${cycleStale ? 'bg-amber-400 animate-pulse' : 'bg-emerald-500'}`} />
              <span className={cycleStale ? 'text-amber-400' : 'text-neutral-400'}>
                Cycle:{' '}
                {cycleAge === null
                  ? '—'
                  : cycleAge < 60
                  ? `${Math.round(cycleAge)}s`
                  : `${Math.round(cycleAge / 60)}m`}
                {cycleStale && ' ⚠'}
              </span>
            </div>

            <div className="w-px h-3 bg-neutral-800 shrink-0" />

            <div className="flex items-center gap-2 text-neutral-400">
              <span><span className="text-neutral-300 font-medium">{health.queued_tasks}</span> queued</span>
              <span><span className="text-neutral-300 font-medium">{health.active_tasks}</span> active</span>
            </div>

            {orderedDeps.length > 0 && (
              <>
                <div className="w-px h-3 bg-neutral-800 shrink-0" />
                <div className="flex items-center gap-2">
                  {orderedDeps.map(key => {
                    const val = externalHealth[key] as boolean | string;
                    return (
                      <div key={key} className="flex items-center gap-1" title={statusDotTitle(key, val)}>
                        <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${statusColor(val)}`} />
                        <span className="text-neutral-500">{DEP_LABELS[key] ?? key}</span>
                      </div>
                    );
                  })}
                </div>
              </>
            )}
          </>
        )}
        {!health && (
          <span className="text-neutral-500">Orchestrator status unavailable</span>
        )}
      </div>

      {/* Right: stale heartbeats + last update */}
      <div className="flex items-center gap-3">
        {staleHeartbeatCount > 0 && (
          <span className="text-amber-400" title={`${staleHeartbeatCount} IN_PROGRESS task${staleHeartbeatCount !== 1 ? 's' : ''} with no heartbeat in the last 5 minutes`}>
            ⚠ {staleHeartbeatCount} stale
          </span>
        )}
        <span className="text-neutral-500">Updated {relativeTime(lastFetchTime, now)}</span>
      </div>
    </div>
  );
}
