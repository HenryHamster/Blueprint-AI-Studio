import { useEffect, useMemo, useState } from 'react';
import type { Task } from './TaskCard';
import { timeAgo, parseTimestamp, formatDurationSec } from '../utils/time';

interface OrchestratorHealth {
  last_cycle_seconds_ago: number | null;
  queued_tasks: number;
  active_tasks: number;
  pool_info: Record<string, unknown>;
  deploy_state?: {
    status: string;
    rollback_count: number;
    rollback_reason: string | null;
    pause_reason: string | null;
    monitoring_since: string | null;
    recent_outcomes: { created_at: string; status: string; failure_class: string | null }[];
  } | null;
  deploy_monitoring?: {
    external_health?: Record<string, boolean | string | string[]>;
  };
}

interface CircuitBreakerState {
  state: 'CLOSED' | 'OPEN' | 'HALF_OPEN';
  failure_count: number;
  opened_at: string | null;
}

interface DetailedHealth {
  circuit_breaker: { projects: Record<string, CircuitBreakerState> };
  max_plan_pool: { total: number; available: number };
  autonomous: { enabled: boolean; consecutive_failures: number };
  github_quota: {
    remaining: number | null;
    limit: number | null;
    reset_at: number | null;
    last_updated: string | null;
    status: 'ok' | 'degraded' | 'critical' | 'unknown';
  };
}

interface PipelineThrottle {
  reason: string;
  detail: string;
  recovery_seconds: number | null;
}

interface OrchestratorHealthBarProps {
  health: OrchestratorHealth | null;
  detailedHealth: DetailedHealth | null;
  apiHealthy: boolean;
  wsConnected: boolean;
  lastFetchTime: number;
  tasks: Task[];
  pipelineThrottle?: PipelineThrottle | null;
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

function monitoringDuration(since: string): string {
  return formatDurationSec(Math.floor((Date.now() - parseTimestamp(since).getTime()) / 1000));
}

export function OrchestratorHealthBar({ health, detailedHealth, apiHealthy, wsConnected, lastFetchTime, tasks, pipelineThrottle }: OrchestratorHealthBarProps) {
  const [now, setNow] = useState(() => Date.now());
  const [circuitExpanded, setCircuitExpanded] = useState(false);

  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);

  const staleHeartbeatCount = useMemo(() => {
    const fiveMinutesAgo = now - 5 * 60 * 1000;
    return tasks.filter(t =>
      t.status === 'IN_PROGRESS' &&
      t.last_heartbeat &&
      parseTimestamp(t.last_heartbeat).getTime() < fiveMinutesAgo
    ).length;
  }, [tasks, now]);

  const cycleAge = health?.last_cycle_seconds_ago ?? null;
  const cycleDegraded = cycleAge !== null && cycleAge >= 60 && cycleAge < 120;
  const cycleDown = cycleAge !== null && cycleAge >= 120;

  const deployState = health?.deploy_state ?? null;
  const showDeployInfo = deployState && (
    deployState.status === 'MONITORING' ||
    deployState.status === 'ROLLED_BACK' ||
    deployState.status === 'HALTED' ||
    (deployState.rollback_count > 0)
  );

  const externalHealth = health?.deploy_monitoring?.external_health ?? {};
  const orderedDeps = (['github_api', 'db', 'gcp'] as const).filter(k => k in externalHealth);

  // Circuit breaker derived state
  const openProjects = useMemo(() => {
    if (!detailedHealth) return [];
    return Object.entries(detailedHealth.circuit_breaker.projects)
      .filter(([, b]) => b.state === 'OPEN')
      .map(([id]) => id);
  }, [detailedHealth]);

  const poolExhausted = detailedHealth
    ? detailedHealth.max_plan_pool.total > 0 && detailedHealth.max_plan_pool.available === 0
    : false;

  const autonomousFailures = detailedHealth?.autonomous.consecutive_failures ?? 0;

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

      {/* Center: Orchestrator cycle + task counts + deps + detailed health */}
      <div className="flex items-center gap-3">
        {health && (
          <>
            <div
              className="flex items-center gap-1.5"
              title={
                cycleAge === null
                  ? 'No cycle data'
                  : cycleDown
                  ? `Last cycle ${Math.round(cycleAge)}s ago — down (threshold: ≥120s)`
                  : cycleDegraded
                  ? `Last cycle ${Math.round(cycleAge)}s ago — degraded (threshold: ≥60s)`
                  : `Last cycle ${Math.round(cycleAge)}s ago — healthy`
              }
            >
              <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${cycleDown ? 'bg-red-500 animate-pulse' : cycleDegraded ? 'bg-amber-400 animate-pulse' : 'bg-emerald-500'}`} />
              <span className={cycleDown ? 'text-red-400' : cycleDegraded ? 'text-amber-400' : 'text-neutral-400'}>
                Cycle:{' '}
                {cycleAge === null
                  ? '—'
                  : cycleAge < 60
                  ? `${Math.round(cycleAge)}s`
                  : `${Math.round(cycleAge / 60)}m`}
                {cycleDown && ' ⚠'}
                {cycleDegraded && ' ~'}
              </span>
            </div>

            <div className="w-px h-3 bg-neutral-800 shrink-0" />

            <div className="flex items-center gap-2 text-neutral-400">
              <span><span className="text-neutral-300 font-medium">{Math.max(0, health.queued_tasks)}</span> queued</span>
              <span><span className="text-neutral-300 font-medium">{Math.max(0, health.active_tasks)}</span> active</span>
            </div>

            {showDeployInfo && (
              <>
                <div className="w-px h-3 bg-neutral-800 shrink-0" />
                <div
                  className="flex items-center gap-1.5"
                  title={[
                    (deployState?.rollback_count ?? 0) > 0 ? `${deployState?.rollback_count ?? 0} rollback${(deployState?.rollback_count ?? 0) !== 1 ? 's' : ''}` : null,
                    deployState?.rollback_reason ? `Reason: ${deployState?.rollback_reason}` : null,
                    deployState?.pause_reason ? `Paused: ${deployState?.pause_reason}` : null,
                    deployState?.monitoring_since ? `Monitoring for ${monitoringDuration(deployState?.monitoring_since)}` : null,
                  ].filter(Boolean).join(' · ') || undefined}
                >
                  <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${
                    deployState?.status === 'HALTED' ? 'bg-red-500 animate-pulse' :
                    deployState?.status === 'ROLLED_BACK' ? 'bg-amber-400' :
                    deployState?.status === 'MONITORING' ? 'bg-blue-400 animate-pulse' :
                    'bg-neutral-500'
                  }`} />
                  <span className={`${
                    deployState?.status === 'HALTED' ? 'text-red-400' :
                    deployState?.status === 'ROLLED_BACK' ? 'text-amber-400' :
                    deployState?.status === 'MONITORING' ? 'text-blue-400' :
                    'text-neutral-400'
                  }`}>
                    {deployState?.status === 'MONITORING' && deployState?.monitoring_since
                      ? `Monitoring ${monitoringDuration(deployState?.monitoring_since)}`
                      : deployState?.status === 'ROLLED_BACK'
                      ? 'Rolled back'
                      : deployState?.status === 'HALTED'
                      ? 'Halted'
                      : deployState?.status}
                    {(deployState?.rollback_count ?? 0) > 0 && ` ×${deployState?.rollback_count ?? 0}`}
                  </span>
                  {deployState?.pause_reason && (
                    <span className="text-neutral-500 max-w-[160px] truncate" title={deployState?.pause_reason}>
                      — {deployState?.pause_reason}
                    </span>
                  )}
                </div>
              </>
            )}

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

        {/* Detailed health indicators */}
        {openProjects.length > 0 && (
          <>
            <div className="w-px h-3 bg-neutral-800 shrink-0" />
            <div className="relative flex items-center">
              <button
                className="flex items-center gap-1.5 cursor-pointer"
                title={`Circuit breakers open for: ${openProjects.join(', ')}`}
                onClick={() => setCircuitExpanded(v => !v)}
              >
                <span className="w-1.5 h-1.5 rounded-full bg-amber-400 animate-pulse shrink-0" />
                <span className="text-amber-400">{openProjects.length} circuit{openProjects.length !== 1 ? 's' : ''} open</span>
              </button>
              {circuitExpanded && (
                <div className="absolute top-5 left-0 z-10 bg-neutral-800 border border-neutral-700 rounded px-2 py-1.5 text-neutral-300 whitespace-nowrap shadow-lg">
                  {openProjects.map(id => (
                    <div key={id} className="flex items-center gap-1.5 py-0.5">
                      <span className="w-1.5 h-1.5 rounded-full bg-amber-400 shrink-0" />
                      <span>{id}</span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </>
        )}

        {poolExhausted && (
          <>
            <div className="w-px h-3 bg-neutral-800 shrink-0" />
            <div className="flex items-center gap-1.5" title="No Max Plan accounts available">
              <span className="w-1.5 h-1.5 rounded-full bg-amber-400 animate-pulse shrink-0" />
              <span className="text-amber-400">Pool empty</span>
            </div>
          </>
        )}

        {autonomousFailures >= 2 && (
          <>
            <div className="w-px h-3 bg-neutral-800 shrink-0" />
            <div className="flex items-center gap-1.5" title={`Autonomous mode: ${autonomousFailures} consecutive failures`}>
              <span className="w-1.5 h-1.5 rounded-full bg-amber-400 animate-pulse shrink-0" />
              <span className="text-amber-400">{autonomousFailures} auto-fail</span>
            </div>
          </>
        )}

        {/* GitHub quota — only show when degraded/critical or unknown-with-recent-fetch.
            Hidden when healthy to avoid noise; visible the moment it matters. */}
        {detailedHealth?.github_quota && detailedHealth.github_quota.status !== 'ok' && (
          <>
            <div className="w-px h-3 bg-neutral-800 shrink-0" />
            {(() => {
              const q = detailedHealth.github_quota;
              const dotColor =
                q.status === 'critical' ? 'bg-red-500 animate-pulse' :
                q.status === 'degraded' ? 'bg-amber-400' :
                'bg-neutral-600';
              const textColor =
                q.status === 'critical' ? 'text-red-400' :
                q.status === 'degraded' ? 'text-amber-400' :
                'text-neutral-500';
              const resetIn = q.reset_at
                ? Math.max(0, Math.round((q.reset_at * 1000 - now) / 60000))
                : null;
              const remainingDisplay = q.remaining == null ? '?' : q.remaining;
              const limitDisplay = q.limit ?? 5000;
              const title = q.remaining == null
                ? 'GitHub quota: no recent observation'
                : `GitHub API quota: ${remainingDisplay}/${limitDisplay} remaining${resetIn != null ? `, resets in ~${resetIn}min` : ''}`;
              return (
                <div className="flex items-center gap-1.5" title={title}>
                  <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${dotColor}`} />
                  <span className={textColor}>
                    GH: {remainingDisplay}{q.status === 'critical' ? ' ⚠' : ''}
                    {resetIn != null && resetIn > 0 && ` (${resetIn}m)`}
                  </span>
                </div>
              );
            })()}
          </>
        )}

        {pipelineThrottle && (
          <>
            <div className="w-px h-3 bg-neutral-800 shrink-0" />
            <span className="inline-flex items-center gap-1 px-2 py-0.5 bg-amber-900/40 text-amber-400 text-xs rounded">
              <span className="w-1.5 h-1.5 rounded-full bg-amber-400 animate-pulse" />
              Throttled: {pipelineThrottle.detail}
              {pipelineThrottle.recovery_seconds != null && ` (~${pipelineThrottle.recovery_seconds}s)`}
            </span>
          </>
        )}
      </div>

      {/* Right: stale heartbeats + last update */}
      <div className="flex items-center gap-3">
        {staleHeartbeatCount > 0 && (
          <span className="text-amber-400" title={`${staleHeartbeatCount} IN_PROGRESS task${staleHeartbeatCount !== 1 ? 's' : ''} with no heartbeat in the last 5 minutes`}>
            ⚠ {staleHeartbeatCount} stale
          </span>
        )}
        <span className="text-neutral-500">Updated {lastFetchTime ? timeAgo(lastFetchTime) : '—'}</span>
      </div>
    </div>
  );
}
