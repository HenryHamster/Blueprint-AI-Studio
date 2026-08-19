/* eslint-disable react-hooks/set-state-in-effect */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { RefreshCw } from 'lucide-react';
import type { Task } from './TaskCard';
import type { OrchestratorHealth, ProviderApisHealth } from '../types/settings';
import type { FailingTaskTypeEntry } from '../types/diagnostics';
import { timeAgo, parseTimestamp, formatDurationSec } from '../utils/time';
import { apiFetch } from '../api';

interface CircuitBreakerState {
  state: 'CLOSED' | 'OPEN' | 'HALF_OPEN';
  failure_count: number;
  opened_at: string | null;
}

interface GithubQuota {
  remaining: number | null;
  limit: number | null;
  reset_at: number | null;
  last_updated: string | null;
  status: 'ok' | 'degraded' | 'critical' | 'unknown';
}

export interface DetailedHealth {
  circuit_breaker: { projects: Record<string, CircuitBreakerState> };
  max_plan_pool: { total: number; available: number; by_provider?: Record<string, { total: number; available: number }> };
  autonomous: { enabled: boolean; consecutive_failures: number };
  provider_apis: ProviderApisHealth;
  github_quota: GithubQuota;
}

export interface PipelineThrottle {
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
  gatedTaskTypes?: FailingTaskTypeEntry[];
  costSectionAvailable?: boolean;
}

const DEP_LABELS: Record<string, string> = {
  github_api: 'GitHub',
  db: 'DB',
  gcp: 'GCP',
  dispatch_api: 'Dispatch',
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

const COST_AMBER_THRESHOLD = 0.6;
const COST_RED_THRESHOLD = 0.9;

// Slow-cycle (degraded) threshold, in seconds. Mirrors the backend contract in
// backend/app/main.py (`slow_cycle = cycle_age >= 120` in both /health and
// /api/health/detailed) so operator surfaces never disagree: a 60-119s
// progressing cycle is healthy on every surface, and degraded begins at 120s.
const SLOW_CYCLE_THRESHOLD_SEC = 120;

export function OrchestratorHealthBar({ health, detailedHealth, apiHealthy, wsConnected, lastFetchTime, tasks, pipelineThrottle, gatedTaskTypes, costSectionAvailable = false }: OrchestratorHealthBarProps) {
  const [now, setNow] = useState(() => Date.now());
  const [circuitExpanded, setCircuitExpanded] = useState(false);
  const [refreshingQuota, setRefreshingQuota] = useState(false);
  const [localQuota, setLocalQuota] = useState<GithubQuota | null>(null);
  const [dailyCostUsd, setDailyCostUsd] = useState<number | null>(null);
  const [dailyCostCeilingUsd, setDailyCostCeilingUsd] = useState<number>(0);
  const [digestPollFailed, setDigestPollFailed] = useState(false);
  const [digestLastSuccessAt, setDigestLastSuccessAt] = useState<number | null>(null);
  // Fleet-scoped stale-heartbeat count from the same digest fetch as the cost
  // pill. `null` until the digest has returned the field; until then the warning
  // falls back to the current `tasks` page (clearly labelled as page-only).
  const [fleetStaleHeartbeatCount, setFleetStaleHeartbeatCount] = useState<number | null>(null);
  const costFetchRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);

  // Clear the local override once the parent's polled data catches up —
  // detected by the server-reported last_updated reaching (or exceeding)
  // the timestamp we stored from our manual refresh response.
  useEffect(() => {
    if (!localQuota || !detailedHealth?.github_quota) return;
    const serverTs = detailedHealth.github_quota.last_updated;
    const localTs = localQuota.last_updated;
    if (serverTs && localTs && serverTs >= localTs) setLocalQuota(null);
  }, [detailedHealth?.github_quota, localQuota]);

  const fetchCostData = useCallback(async () => {
    try {
      const res = await apiFetch('/api/admin/digest?hours=24');
      if (res.ok) {
        const data = await res.json();
        if (data?.daily_cost_usd != null) setDailyCostUsd(data.daily_cost_usd);
        if (data?.daily_cost_ceiling_usd != null) setDailyCostCeilingUsd(data.daily_cost_ceiling_usd);
        if (typeof data?.stale_heartbeat_count === 'number') setFleetStaleHeartbeatCount(data.stale_heartbeat_count);
        // Treat served_stale as equivalent to a failed poll for display purposes
        if (data?.served_stale === true) {
          setDigestPollFailed(true);
        } else {
          setDigestLastSuccessAt(Date.now());
          setDigestPollFailed(false);
        }
      } else {
        setDigestPollFailed(true);
      }
    } catch {
      setDigestPollFailed(true);
    }
  }, []);

  useEffect(() => {
    fetchCostData();
    const schedule = () => { costFetchRef.current = setTimeout(() => { fetchCostData().then(schedule); }, 60_000); };
    schedule();
    return () => { if (costFetchRef.current) clearTimeout(costFetchRef.current); };
  }, [fetchCostData]);

  const refreshQuota = async () => {
    if (refreshingQuota) return;
    setRefreshingQuota(true);
    try {
      const res = await apiFetch('/api/admin/github-quota/refresh', { method: 'POST' });
      if (res.ok) {
        const data = await res.json();
        if (data?.github_quota) setLocalQuota(data.github_quota as GithubQuota);
      }
    } catch {
      // Ignored — the pill will keep showing last-known values.
    } finally {
      setRefreshingQuota(false);
    }
  };

  // Page-derived stale-heartbeat count over the current `tasks` slice. Used only
  // as a fallback until the fleet-scoped digest count is available.
  const pageStaleHeartbeatCount = useMemo(() => {
    const fiveMinutesAgo = now - 5 * 60 * 1000;
    return tasks.filter(t =>
      t.status === 'IN_PROGRESS' &&
      t.last_heartbeat &&
      parseTimestamp(t.last_heartbeat).getTime() < fiveMinutesAgo
    ).length;
  }, [tasks, now]);

  // Prefer the fleet-wide digest count so the warning reflects every stuck task,
  // not just those on the current paginated page. Fall back to the page count
  // (labelled page-only in the tooltip) until the digest field has loaded.
  const staleIsPageOnly = fleetStaleHeartbeatCount == null;
  const staleHeartbeatIsDigestStale = digestPollFailed && fleetStaleHeartbeatCount != null;
  const staleHeartbeatCount = fleetStaleHeartbeatCount ?? pageStaleHeartbeatCount;
  const digestAsOfLabel =
    digestPollFailed && digestLastSuccessAt != null
      ? timeAgo(digestLastSuccessAt)
      : null;
  const digestStaleSuffix = digestAsOfLabel
    ? `Last digest poll failed; showing values as of ${digestAsOfLabel}.`
    : null;

  // Cycle-COMPLETION age is now only a secondary, informational slow-cycle
  // signal. The PRIMARY health verdict comes from the backend's progress-based
  // liveness (the same contract /health and the lifecycle watchdog use): a
  // long-but-progressing cycle is healthy, and only a genuine wedge is "down".
  const cycleAge = health?.last_cycle_seconds_ago ?? null;

  // Liveness fields are present on current payloads; older orchestrator
  // payloads omit them, in which case we fall back to the legacy cycle-age
  // thresholds so nothing regresses.
  const hasLiveness =
    health != null &&
    (health.orchestrator_alive !== undefined || health.orchestrator_hung !== undefined);
  const orchestratorAlive = health?.orchestrator_alive ?? true;
  const orchestratorHung = health?.orchestrator_hung ?? false;
  const progressAge = health?.orchestrator_progress_age_seconds ?? null;
  const livenessReason = health?.orchestrator_liveness_reason ?? null;

  // Slow cycle is purely informational (amber hint) and never the down verdict.
  // The threshold matches the backend's slow-cycle contract (>=120s) so a
  // 60-119s progressing cycle never shows contradictory states across surfaces.
  const cycleSlow = cycleAge !== null && cycleAge >= SLOW_CYCLE_THRESHOLD_SEC;
  // cold-start: alive=false but stalled=false means the first cycle hasn't
  // completed yet — not a genuine hang. Show green until stalled fires.
  const cycleDown = hasLiveness
    ? orchestratorHung
    : cycleAge !== null && cycleAge >= SLOW_CYCLE_THRESHOLD_SEC;
  // Legacy fallback (payload predates progress liveness) keeps the original
  // cycle-age-only band: degraded at 60-119s, down at >=120s. That older
  // contract is internally consistent with an old orchestrator and only fires
  // when liveness fields are absent, so it is intentionally left unchanged.
  const cycleDegraded = hasLiveness
    ? !cycleDown && cycleSlow
    : cycleAge !== null && cycleAge >= 60 && cycleAge < SLOW_CYCLE_THRESHOLD_SEC;

  const cycleTitle = (() => {
    // Cold-start: alive=false, stalled=false, no cycle data yet — must come
    // before the null-data early return so boot doesn't show "No cycle data".
    if (hasLiveness && !orchestratorAlive && !orchestratorHung) {
      return `Orchestrator starting — no cycle recorded yet`;
    }
    if (cycleAge === null && progressAge === null) return 'No cycle data';
    if (hasLiveness) {
      const progressStr =
        progressAge !== null ? `progress ${Math.round(progressAge)}s ago` : 'progress age unknown';
      const cycleStr =
        cycleAge !== null ? `last cycle ${Math.round(cycleAge)}s ago` : 'cycle age unknown';
      if (cycleDown) {
        return `Orchestrator not progressing — ${livenessReason ?? 'no progress heartbeat'} (${progressStr}, ${cycleStr})`;
      }
      if (cycleDegraded) {
        return `Slow cycle (${cycleStr}) but orchestrator is still progressing (${progressStr}) — healthy`;
      }
      return `Orchestrator progressing (${progressStr}, ${cycleStr}) — healthy`;
    }
    // Legacy cycle-age-only fallback (payload predates progress liveness).
    const ageStr = `Last cycle ${Math.round(cycleAge as number)}s ago`;
    if (cycleDown) return `${ageStr} — down (threshold: ≥120s)`;
    if (cycleDegraded) return `${ageStr} — degraded (threshold: ≥60s)`;
    return `${ageStr} — healthy`;
  })();

  const deployState = health?.deploy_state ?? null;
  const showDeployInfo = deployState && (
    deployState.status === 'MONITORING' ||
    deployState.status === 'ROLLED_BACK' ||
    deployState.status === 'HALTED' ||
    deployState.status === 'PAUSED' ||
    (deployState.rollback_count > 0)
  );

  const externalHealth = health?.deploy_monitoring?.external_health ?? {};
  const orderedDeps = (['github_api', 'db', 'gcp'] as const).filter(k => k in externalHealth);
  const providerApis = detailedHealth?.provider_apis ?? null;

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
    <div data-testid="orchestrator-health-bar" className={`mb-3 flex min-h-8 shrink-0 flex-wrap items-center justify-between gap-x-3 gap-y-1 rounded-md border border-neutral-800 bg-workbench-layer px-2.5 py-1.5 text-xs${apiHealthy ? '' : ' bg-red-950/20'}`}>
      {/* Left: API + WS status */}
      <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1">
        <div className="flex items-center gap-1.5 rounded border border-neutral-800 bg-workbench-card px-2 py-1" title={apiHealthy ? undefined : 'API connection error — data may be stale'}>
          <span className={`w-1.5 h-1.5 rounded-full ${apiHealthy ? 'bg-emerald-500' : 'bg-red-500 animate-pulse'}`} />
          <span className={apiHealthy ? 'text-neutral-400' : 'text-red-400'}>
            {apiHealthy ? 'Connected' : 'API Error'}
          </span>
        </div>
        <div className="flex items-center gap-1.5 rounded border border-neutral-800 bg-workbench-card px-2 py-1">
          <span className={`w-1.5 h-1.5 rounded-full ${wsConnected ? 'bg-blue-500' : 'bg-workbench-card'}`} />
          <span className={wsConnected ? 'text-blue-400' : 'text-neutral-500'}>
            WS
          </span>
        </div>
      </div>

      {/* Center: Orchestrator cycle + task counts + deps + detailed health */}
      <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1">
        {health && (
          <>
            <div
              className="flex items-center gap-1.5 rounded border border-neutral-800 bg-workbench-card px-2 py-1"
              title={cycleTitle}
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

            <div className="w-px h-3 bg-workbench-panel shrink-0" />

            <div className="flex items-center gap-2 rounded border border-neutral-800 bg-workbench-card px-2 py-1 text-neutral-400">
              <span><span className="text-neutral-300 font-medium">{Math.max(0, health.queued_tasks)}</span> queued</span>
              <span><span className="text-neutral-300 font-medium">{Math.max(0, health.active_tasks)}</span> active</span>
            </div>

            {showDeployInfo && (
              <>
                <div className="w-px h-3 bg-workbench-panel shrink-0" />
                <div
                  className="flex items-center gap-1.5 rounded border border-neutral-800 bg-workbench-card px-2 py-1"
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
                    deployState?.status === 'PAUSED' ? 'bg-amber-400' :
                    deployState?.status === 'MONITORING' ? 'bg-blue-400 animate-pulse' :
                    'bg-workbench-control'
                  }`} />
                  <span className={`${
                    deployState?.status === 'HALTED' ? 'text-red-400' :
                    deployState?.status === 'ROLLED_BACK' ? 'text-amber-400' :
                    deployState?.status === 'PAUSED' ? 'text-amber-400' :
                    deployState?.status === 'MONITORING' ? 'text-blue-400' :
                    'text-neutral-400'
                  }`}>
                    {deployState?.status === 'MONITORING' && deployState?.monitoring_since
                      ? `Monitoring ${monitoringDuration(deployState?.monitoring_since)}`
                      : deployState?.status === 'ROLLED_BACK'
                      ? 'Rolled back'
                      : deployState?.status === 'PAUSED'
                      ? 'Paused'
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
                <div className="w-px h-3 bg-workbench-panel shrink-0" />
                <div className="flex items-center gap-2 rounded border border-neutral-800 bg-workbench-card px-2 py-1">
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
            <div className="w-px h-3 bg-workbench-panel shrink-0" />
            <div className="relative flex items-center">
              <button
                className="flex min-h-[44px] cursor-pointer items-center gap-1.5 rounded border border-neutral-800 bg-workbench-card px-2 py-1 sm:min-h-0"
                title={`Circuit breakers open for: ${openProjects.join(', ')}`}
                onClick={() => setCircuitExpanded(v => !v)}
              >
                <span className="w-1.5 h-1.5 rounded-full bg-amber-400 animate-pulse shrink-0" />
                <span className="text-amber-400">{openProjects.length} circuit{openProjects.length !== 1 ? 's' : ''} open</span>
              </button>
              {circuitExpanded && (
                <div className="absolute top-5 left-0 z-10 bg-workbench-panel border border-neutral-700 rounded px-2 py-1.5 text-neutral-300 whitespace-nowrap shadow-lg">
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
            <div className="w-px h-3 bg-workbench-panel shrink-0" />
            <div className="flex items-center gap-1.5 rounded border border-amber-500/20 bg-amber-500/10 px-2 py-1" title="No Max Plan accounts available">
              <span className="w-1.5 h-1.5 rounded-full bg-amber-400 animate-pulse shrink-0" />
              <span className="text-amber-400">Pool empty</span>
            </div>
          </>
        )}

        {providerApis && providerApis.required_providers.length > 0 && (
          <>
            <div className="w-px h-3 bg-workbench-panel shrink-0" />
            <div className="flex items-center gap-2 rounded border border-neutral-800 bg-workbench-card px-2 py-1" title={`Provider health: ${providerApis.status}`}>
              <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${
                providerApis.status === 'down' ? 'bg-red-500 animate-pulse' :
                providerApis.status === 'degraded' ? 'bg-amber-400' :
                'bg-emerald-500'
              }`} />
              <span className={providerApis.status === 'down' ? 'text-red-400' : providerApis.status === 'degraded' ? 'text-amber-400' : 'text-neutral-400'}>
                LLMs
              </span>
              <div className="flex items-center gap-1.5">
                {providerApis.required_providers.map(provider => {
                  const snapshot = providerApis.providers?.[provider];
                  const status = snapshot?.status ?? 'unknown';
                  const color =
                    status === 'down' ? 'bg-red-500' :
                    status === 'degraded' ? 'bg-amber-400' :
                    status === 'ok' ? 'bg-emerald-500' :
                    'bg-workbench-card';
                  const label = provider.slice(0, 3).toUpperCase();
                  const title = snapshot
                    ? `${provider}: ${status}${snapshot.reason ? ` — ${snapshot.reason}` : ''}`
                    : `${provider}: unknown`;
                  return (
                    <div key={provider} className="flex items-center gap-1" title={title}>
                      <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${color}`} />
                      <span className="text-neutral-500">{label}</span>
                    </div>
                  );
                })}
              </div>
              {providerApis.fallback_enabled && (
                <span className="text-ui-sm text-blue-400 border border-blue-900/50 bg-blue-950/20 rounded px-1.5 py-0.5">
                  fallback
                </span>
              )}
            </div>
          </>
        )}

        {autonomousFailures >= 2 && (
          <>
            <div className="w-px h-3 bg-workbench-panel shrink-0" />
            <div className="flex items-center gap-1.5 rounded border border-amber-500/20 bg-amber-500/10 px-2 py-1" title={`Autonomous mode: ${autonomousFailures} consecutive failures`}>
              <span className="w-1.5 h-1.5 rounded-full bg-amber-400 animate-pulse shrink-0" />
              <span className="text-amber-400">{autonomousFailures} auto-fail</span>
            </div>
          </>
        )}

        {/* GitHub quota — always shown so the manual refresh button is accessible.
            Text stays low-intensity when healthy; amber/red when degraded/critical. */}
        {detailedHealth?.github_quota && (
          <>
            <div className="w-px h-3 bg-workbench-panel shrink-0" />
            {(() => {
              const q = localQuota ?? detailedHealth.github_quota;
              const dotColor =
                q.status === 'critical' ? 'bg-red-500 animate-pulse' :
                q.status === 'degraded' ? 'bg-amber-400' :
                q.status === 'ok' ? 'bg-emerald-500' :
                'bg-workbench-card';
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
                ? 'GitHub quota: no recent observation (click ↻ to probe)'
                : `GitHub API quota: ${remainingDisplay}/${limitDisplay} remaining${resetIn != null ? `, resets in ~${resetIn}min` : ''} (click ↻ to refresh)`;
              return (
                <div className="flex items-center gap-1.5 rounded border border-neutral-800 bg-workbench-card px-2 py-1" title={title}>
                  <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${dotColor}`} />
                  <span className={textColor}>
                    GH: {remainingDisplay}{q.status === 'critical' ? ' ⚠' : ''}
                    {resetIn != null && resetIn > 0 && ` (${resetIn}m)`}
                  </span>
                  <button
                    type="button"
                    onClick={refreshQuota}
                    disabled={refreshingQuota}
                    className="min-h-[44px] min-w-[44px] sm:min-h-0 sm:min-w-0 flex items-center justify-center text-neutral-500 hover:text-neutral-300 disabled:opacity-50 disabled:cursor-not-allowed"
                    title="Probe GitHub rate_limit endpoint now"
                  >
                    <RefreshCw className={`w-3 h-3 ${refreshingQuota ? 'animate-spin' : ''}`} />
                  </button>
                </div>
              );
            })()}
          </>
        )}

        {pipelineThrottle && (
          <>
            <div className="w-px h-3 bg-workbench-panel shrink-0" />
            <span className="inline-flex max-w-full min-w-0 items-center gap-1 rounded border border-amber-500/20 bg-amber-500/10 px-2 py-1 text-xs text-amber-300">
              <span className="w-1.5 h-1.5 rounded-full bg-amber-400 animate-pulse" />
              <span className="min-w-0 truncate">Throttled: {pipelineThrottle.detail}</span>
              {pipelineThrottle.recovery_seconds != null && ` (~${pipelineThrottle.recovery_seconds}s)`}
            </span>
          </>
        )}

        {gatedTaskTypes && gatedTaskTypes.length > 0 && (
          <>
            <div className="w-px h-3 bg-workbench-panel shrink-0" />
            <div
              className="inline-flex max-w-full min-w-0 flex-wrap items-center gap-1 rounded border border-amber-500/20 bg-amber-500/10 px-2 py-1 text-xs"
              title={`Per-type dispatch gates active: ${gatedTaskTypes.map(t => `${t.task_type} (${(t.failure_rate * 100).toFixed(0)}% failure rate, n=${t.sample_size})`).join('; ')}`}
            >
              <span className="w-1.5 h-1.5 rounded-full bg-amber-400 animate-pulse shrink-0" />
              <span className="text-amber-300 shrink-0">Gated:</span>
              {gatedTaskTypes.map(t => (
                <span
                  key={t.task_type}
                  className="font-mono text-amber-200 bg-amber-900/30 rounded px-1"
                >
                  {t.task_type}
                </span>
              ))}
            </div>
          </>
        )}

        {dailyCostUsd != null && (
          <>
            <div className="w-px h-3 bg-workbench-panel shrink-0" />
            {(() => {
              const hasCeiling = dailyCostCeilingUsd > 0;
              const ratio = hasCeiling ? dailyCostUsd / dailyCostCeilingUsd : 0;
              const isRed = hasCeiling && ratio >= COST_RED_THRESHOLD;
              const isAmber = hasCeiling && !isRed && ratio >= COST_AMBER_THRESHOLD;
              const dotColor = isRed ? 'bg-red-500 animate-pulse' : isAmber ? 'bg-amber-400' : 'bg-workbench-control';
              const textColor = isRed ? 'text-red-400' : isAmber ? 'text-amber-400' : 'text-neutral-400';
              const label = hasCeiling
                ? `$${dailyCostUsd.toFixed(2)} / $${dailyCostCeilingUsd.toFixed(2)}`
                : `$${dailyCostUsd.toFixed(2)}`;
              const titleText = hasCeiling
                ? `Daily cost: $${dailyCostUsd.toFixed(2)} / $${dailyCostCeilingUsd.toFixed(2)} ceiling (${Math.round(ratio * 100)}%).${costSectionAvailable ? ' Click to view cost section.' : ''}`
                : `Daily cost: $${dailyCostUsd.toFixed(2)}.${costSectionAvailable ? ' Click to view cost section.' : ''}`;
              return (
                <button
                  type="button"
                  className={`inline-flex min-h-[44px] cursor-pointer items-center gap-1 rounded border border-neutral-800 bg-workbench-card px-2 py-1 text-xs sm:min-h-0 ${isRed ? 'bg-red-900/40' : isAmber ? 'bg-amber-900/40' : ''} ${digestPollFailed ? 'border-amber-500/30 opacity-75' : ''}`}
                  title={digestStaleSuffix ? `${titleText} ${digestStaleSuffix}` : titleText}
                  onClick={() => { if (costSectionAvailable) document.getElementById('cost-section')?.scrollIntoView({ behavior: 'smooth', block: 'nearest' }); }}
                >
                  <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${dotColor}`} />
                  <span className={textColor}>{label}{isRed && ratio >= 1 ? ' ⚠' : ''}</span>
                  {digestStaleSuffix && (
                    <span className="text-ui-sm text-amber-300">as of {digestAsOfLabel}</span>
                  )}
                </button>
              );
            })()}
          </>
        )}
      </div>

      {/* Right: stale heartbeats + last update */}
      <div className="flex items-center gap-2">
        {staleHeartbeatCount > 0 && (
          <span
            className={`rounded border border-amber-500/20 bg-amber-500/10 px-2 py-1 text-amber-300 ${staleHeartbeatIsDigestStale ? 'opacity-75' : ''}`}
            title={`${staleHeartbeatCount} IN_PROGRESS task${staleHeartbeatCount !== 1 ? 's' : ''} with no heartbeat in the last 5 minutes${staleIsPageOnly ? ' (current page only — fleet count unavailable)' : ' (fleet-wide)'}${digestStaleSuffix ? ` — ${digestStaleSuffix}` : ''}`}
          >
            ⚠ {staleHeartbeatCount} stale
            {staleHeartbeatIsDigestStale && digestAsOfLabel && (
              <span className="ml-1 text-ui-sm text-amber-300">as of {digestAsOfLabel}</span>
            )}
          </span>
        )}
        <span className="rounded border border-neutral-800 bg-workbench-card px-2 py-1 text-neutral-500">Updated {lastFetchTime ? timeAgo(lastFetchTime) : '—'}</span>
      </div>
    </div>
  );
}
