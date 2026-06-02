import { useState, useCallback } from 'react';
import { ChevronDown, ChevronUp, Server, Flame, AlertTriangle, Clock } from 'lucide-react';
import { API_BASE_URL, apiFetch, authGet } from '../api';
import { parseTimestamp } from '../utils/time';
import type { WarmVmStateResponse, WarmVmRow, ChainContinuityFunnelSnapshot, SpawnPinFunnelSnapshot, BacklogReuseFunnelSnapshot, PoolMetrics } from '../types/warmVm';
import { useEfficiencySnapshot, fmtEffSecs, fmtEffRate, fmtEffIter } from '../hooks/useEfficiencySnapshot';
import type { WarmStartOriginBreakdown } from '../hooks/useEfficiencySnapshot';
import { usePoller } from '../hooks/usePoller';

const POLL_INTERVAL_MS = 10_000;
const STORAGE_KEY = 'blueprint-admin:warmVmPanelCollapsed';

function ageSecs(iso: string | null): number | null {
  if (!iso) return null;
  // Backend emits naive UTC datetimes (project-wide convention — see
  // project_timestamptz_migration). parseTimestamp() appends 'Z' so the
  // value is interpreted as UTC, not local time.
  const t = parseTimestamp(iso).getTime();
  if (Number.isNaN(t)) return null;
  return Math.max(0, Math.round((Date.now() - t) / 1000));
}

function fmtAge(secs: number | null): string {
  if (secs === null) return '—';
  if (secs < 60) return `${secs}s`;
  if (secs < 3600) return `${Math.round(secs / 60)}m`;
  return `${Math.round(secs / 3600)}h`;
}

function fmtSetupSecs(s: number | null): string {
  if (s === null || s === undefined) return '—';
  if (s < 1) return `<1s`;
  if (s < 60) return `${s.toFixed(1)}s`;
  return `${Math.round(s / 60)}m`;
}

function shortVm(id: string): string {
  // Match other panels' truncation style — show last 8 chars of UUID-ish suffix.
  const tail = id.split('-').slice(-1)[0] || id;
  return tail.length > 12 ? tail.slice(0, 12) : tail;
}

interface Props {
  onViewTask?: (taskId: string) => void;
}

export function WarmVmPanel({ onViewTask }: Props) {
  const [collapsed, setCollapsed] = useState(() => localStorage.getItem(STORAGE_KEY) === 'true');
  const [data, setData] = useState<WarmVmStateResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [flipping, setFlipping] = useState(false);
  const efficiency = useEfficiencySnapshot();

  const load = useCallback(async () => {
    try {
      const res = await authGet(`${API_BASE_URL}/api/admin/warm-vm-state`);
      if (!res.ok) {
        setError(`HTTP ${res.status}`);
        return;
      }
      setData(await res.json() as WarmVmStateResponse);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  usePoller(load, { intervalMs: POLL_INTERVAL_MS, deps: [load] });

  const toggle = () => {
    const next = !collapsed;
    setCollapsed(next);
    localStorage.setItem(STORAGE_KEY, String(next));
  };

  const flipFlag = useCallback(async (
    configKey: 'warm_continuity_enabled' | 'warm_backlog_reuse_enabled' | 'warm_pool_enabled',
    label: string,
    currentValue: boolean,
  ) => {
    const target = !currentValue;
    const verb = target ? 'ENABLE' : 'DISABLE';
    const ok = window.confirm(
      `${verb} ${label} globally?\n\n` +
      `Current: ${currentValue ? 'enabled' : 'disabled'}\n` +
      `New:     ${target ? 'enabled' : 'disabled'}\n\n` +
      `Effect propagates within ~15s via runtime-config sync. ` +
      `Existing roles finish on whichever path they started.`
    );
    if (!ok) return;
    setFlipping(true);
    try {
      const res = await apiFetch(`${API_BASE_URL}/api/admin/settings`, {
        method: 'PATCH',
        body: JSON.stringify({ [configKey]: target }),
      });
      if (!res.ok) {
        const detail = await res.text().catch(() => '');
        setError(`Toggle failed: HTTP ${res.status} ${detail.slice(0, 200)}`);
      } else {
        await load();
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setFlipping(false);
    }
  }, [load]);

  const vmCount = data?.active_vms.length ?? 0;
  const warmChains = data?.warm_pin_metrics.active_warm_chains ?? 0;
  const clearsHour = data?.warm_pin_metrics.clears_last_hour ?? 0;
  const pinnedQueued = data?.warm_pin_metrics.active_pinned_queued_total ?? 0;
  const flagOn = data?.warm_continuity_enabled ?? false;
  const backlogReuseOn = data?.warm_backlog_reuse_enabled ?? false;
  const backlogClaims = data?.warm_pin_metrics.active_backlog_reuse_claims ?? 0;
  const poolClaims = data?.warm_pin_metrics.active_pool_origin_claims ?? 0;
  const poolShare = data?.warm_pin_metrics.pool_origin_share ?? null;
  const chainClaims = data?.warm_pin_metrics.active_chain_continuity_claims ?? 0;
  const pool = data?.pool_metrics;
  const poolOn = pool?.enabled ?? false;
  const overThreshold = clearsHour > 5;

  return (
    <div className="mb-3 shrink-0">
      <div className="flex items-center gap-2 flex-wrap">
        <button
          onClick={toggle}
          className="flex items-center gap-2 text-[11px] font-bold text-neutral-400 uppercase tracking-widest hover:text-neutral-300 transition-colors"
        >
          {collapsed ? <ChevronDown className="w-3 h-3" /> : <ChevronUp className="w-3 h-3" />}
          <Server className="w-3 h-3" />
          VM Fleet
        </button>
        <button
          onClick={() => { void flipFlag('warm_continuity_enabled', 'warm-VM continuity', flagOn); }}
          disabled={flipping || !data}
          className={
            (flagOn
              ? 'bg-emerald-500/20 border border-emerald-500/40 text-emerald-400 hover:bg-emerald-500/30'
              : 'bg-neutral-700/40 border border-neutral-600/40 text-neutral-400 hover:bg-neutral-700/60'
            ) +
            ' text-[10px] font-bold px-1.5 py-0.5 rounded leading-none transition-colors' +
            (flipping ? ' opacity-50 cursor-wait' : ' cursor-pointer') +
            (!data ? ' opacity-50 cursor-not-allowed' : '')
          }
          title={
            flipping
              ? 'flipping…'
              : (flagOn
                  ? 'Click to DISABLE warm-VM continuity (will be cold-only)'
                  : 'Click to ENABLE warm-VM continuity globally')
          }
        >
          {flipping ? '…' : (flagOn ? 'WARM' : 'COLD')}
        </button>
        <button
          onClick={() => { void flipFlag('warm_backlog_reuse_enabled', 'backlog VM reuse', backlogReuseOn); }}
          disabled={flipping || !data}
          className={
            (backlogReuseOn
              ? 'bg-sky-500/20 border border-sky-500/40 text-sky-400 hover:bg-sky-500/30'
              : 'bg-neutral-700/40 border border-neutral-600/40 text-neutral-400 hover:bg-neutral-700/60'
            ) +
            ' text-[10px] font-bold px-1.5 py-0.5 rounded leading-none transition-colors' +
            (flipping ? ' opacity-50 cursor-wait' : ' cursor-pointer') +
            (!data ? ' opacity-50 cursor-not-allowed' : '')
          }
          title={
            flipping
              ? 'flipping…'
              : (backlogReuseOn
                  ? 'Click to DISABLE same-project backlog VM reuse'
                  : 'Click to ENABLE same-project backlog VM reuse globally')
          }
        >
          {flipping ? '…' : (backlogReuseOn ? 'REUSE' : 'NO-REUSE')}
        </button>
        <button
          onClick={() => { void flipFlag('warm_pool_enabled', 'pre-warmed VM pool', poolOn); }}
          disabled={flipping || !data}
          className={
            (poolOn
              ? 'bg-violet-500/20 border border-violet-500/40 text-violet-400 hover:bg-violet-500/30'
              : 'bg-neutral-700/40 border border-neutral-600/40 text-neutral-400 hover:bg-neutral-700/60'
            ) +
            ' text-[10px] font-bold px-1.5 py-0.5 rounded leading-none transition-colors' +
            (flipping ? ' opacity-50 cursor-wait' : ' cursor-pointer') +
            (!data ? ' opacity-50 cursor-not-allowed' : '')
          }
          title={
            flipping
              ? 'flipping…'
              : (poolOn
                  ? 'Click to DISABLE the pre-warmed VM pool'
                  : 'Click to ENABLE the pre-warmed VM pool globally')
          }
        >
          {flipping ? '…' : (poolOn ? 'POOL' : 'NO-POOL')}
        </button>
        <button
          onClick={toggle}
          className="flex items-center gap-2 text-[11px] font-bold text-neutral-400 uppercase tracking-widest hover:text-neutral-300 transition-colors"
        >
          {warmChains > 0 && (
            <span className="bg-orange-500/20 border border-orange-500/40 text-orange-400 text-[10px] font-bold px-1.5 py-0.5 rounded-full leading-none flex items-center gap-1">
              <Flame className="w-2.5 h-2.5" />
              {warmChains} chain{warmChains !== 1 ? 's' : ''}
            </span>
          )}
          {overThreshold && (
            <span className="bg-red-500/20 border border-red-500/40 text-red-400 text-[10px] font-bold px-1.5 py-0.5 rounded-full leading-none flex items-center gap-1">
              <AlertTriangle className="w-2.5 h-2.5" />
              {clearsHour} clears/hr
            </span>
          )}
          {collapsed && data && (
            <span className="text-[10px] text-neutral-500 font-mono ml-1 normal-case tracking-normal font-normal">
              {vmCount} VM{vmCount !== 1 ? 's' : ''}
              {pinnedQueued > 0 && <> · {pinnedQueued} pinned</>}
            </span>
          )}
        </button>
      </div>

      {!collapsed && (
        <div className="mt-2 bg-neutral-900 border border-neutral-800 rounded-lg p-3 text-xs">
          {error && (
            <div className="text-red-400 mb-2">Failed to load warm-VM state: {error}</div>
          )}
          {!error && data && (
            <>
              <div className="flex flex-wrap gap-x-4 gap-y-1 mb-3 text-[10px] text-neutral-400">
                <span><span className="text-neutral-500">VMs</span>: <span className="font-mono text-neutral-300">{vmCount}</span></span>
                <span><span className="text-neutral-500">warm chains</span>: <span className="font-mono text-neutral-300">{warmChains}</span></span>
                <span><span className="text-neutral-500">backlog reuse</span>: <span className="font-mono text-sky-400">{backlogClaims}</span></span>
                <span data-testid="pool-origin-claims"><span className="text-neutral-500">pool origin</span>: <span className="font-mono text-emerald-400">{poolClaims}</span>{poolShare !== null && <span className="text-neutral-500"> ({Math.round(poolShare * 100)}%)</span>}</span>
                <span><span className="text-neutral-500">chain claims</span>: <span className="font-mono text-orange-400">{chainClaims}</span></span>
                <span><span className="text-neutral-500">pinned queued</span>: <span className="font-mono text-neutral-300">{pinnedQueued}</span></span>
                <span><span className="text-neutral-500">clears/hr</span>: <span className={`font-mono ${overThreshold ? 'text-red-400' : 'text-neutral-300'}`}>{clearsHour}</span></span>
                <span><span className="text-neutral-500">clears/24h</span>: <span className="font-mono text-neutral-300">{data.warm_pin_metrics.clears_last_24h}</span></span>
                {Object.entries(data.warm_pin_metrics.clears_by_reason_last_hour ?? {})
                  .filter(([, count]) => count > 0)
                  .map(([reason, count]) => (
                    <span key={reason} data-testid={`clears-by-reason-${reason}`}>
                      <span className="text-neutral-500">{reason}</span>
                      :{' '}
                      <span
                        className={`font-mono ${reason === 'vm_unresponsive' ? 'text-red-400' : 'text-neutral-300'}`}
                      >
                        {count}
                      </span>
                    </span>
                  ))}
                <span><span className="text-neutral-500">poll timeout</span>: <span className="font-mono text-neutral-300">{data.next_role_poll_timeout_secs}s</span></span>
              </div>

              {efficiency && (
                <div className="mb-3 flex flex-wrap gap-x-4 gap-y-1 text-[10px] text-neutral-400 pt-2 border-t border-neutral-800/60">
                  <span className="text-neutral-500 font-bold uppercase tracking-widest w-full">Pipeline Efficiency</span>
                  <EffMetric label="cold-start p50" value={fmtEffSecs(efficiency.window_24h?.setup_p50_secs)} delta={efficiency.window_24h?.vs_prior_setup_p50_delta} lowerIsBetter />
                  <EffMetric label="reuse rate" value={fmtEffRate(efficiency.window_24h?.warm_reuse_rate)} delta={efficiency.window_24h?.vs_prior_warm_reuse_rate_delta} lowerIsBetter={false} />
                  <EffMetric label="review cycles" value={fmtEffIter(efficiency.window_7d?.review_iter_median)} delta={efficiency.window_7d?.vs_prior_review_iter_median_delta} lowerIsBetter />
                  <WarmStartOriginRow breakdown={efficiency.window_24h?.warm_start_origin_breakdown} />
                </div>
              )}

              {pool && <PoolMetricsRow pool={pool} />}

              {pool && <PoolAccountMixRow pool={pool} />}

              {data.spawn_pin_funnel && (
                <SpawnPinFunnelRow funnel={data.spawn_pin_funnel} />
              )}

              {data.backlog_reuse_funnel && data.backlog_reuse_funnel.window_hours && (
                <BacklogReuseFunnelRow funnel={data.backlog_reuse_funnel} />
              )}

              {data.chain_continuity_funnel && (
                <ChainFunnelRow funnel={data.chain_continuity_funnel} />
              )}

              {overThreshold && (
                <div className="mb-3 px-2 py-1.5 rounded border border-red-500/40 bg-red-500/10 text-red-400 text-[11px] flex items-center gap-2">
                  <AlertTriangle className="w-3 h-3 shrink-0" />
                  <span>
                    Warm-pin clear-rate is {clearsHour}/hr — investigate. Sustained high
                    clear-rate means warm VMs are dying mid-poll faster than expected.
                  </span>
                </div>
              )}

              {vmCount === 0 ? (
                <div className="text-neutral-500 italic py-2">No active VMs. Cold-only operation.</div>
              ) : (
                <>
                  {/* Mobile: stacked card layout */}
                  <div className="sm:hidden divide-y divide-neutral-800/30">
                    {data.active_vms.map((vm) => (
                      <VmCard key={vm.vm_instance_id} vm={vm} onViewTask={onViewTask} />
                    ))}
                  </div>
                  {/* Desktop: table layout */}
                  <table className="hidden sm:table w-full text-[11px] border-collapse">
                    <thead>
                      <tr className="text-neutral-500 text-[10px] uppercase tracking-wide">
                        <th className="text-left font-medium pb-1.5 pr-3">VM</th>
                        <th className="text-left font-medium pb-1.5 pr-3">Heartbeat</th>
                        <th className="text-left font-medium pb-1.5 pr-3">Current Task</th>
                        <th className="text-left font-medium pb-1.5 pr-3">Setup</th>
                        <th className="text-left font-medium pb-1.5 pr-3">Pinned</th>
                        <th className="text-left font-medium pb-1.5">Recent</th>
                      </tr>
                    </thead>
                    <tbody>
                      {data.active_vms.map((vm) => (
                        <VmRow key={vm.vm_instance_id} vm={vm} onViewTask={onViewTask} />
                      ))}
                    </tbody>
                  </table>
                </>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
}

function fmtUsd(v: number | undefined): string {
  if (v === undefined || v === null || Number.isNaN(v)) return '—';
  if (v === 0) return '$0';
  if (v < 0.01) return `$${v.toFixed(4)}`;
  return `$${v.toFixed(2)}`;
}

function PoolMetricsRow({ pool }: { pool: PoolMetrics }) {
  const funnel = pool.funnel;
  const hitRate = funnel?.pool_hit_rate;
  return (
    <div className="mb-3 flex flex-wrap gap-x-4 gap-y-1 text-[10px] text-neutral-400 pt-2 border-t border-neutral-800/60" data-testid="pool-metrics-row">
      <span className="text-neutral-500 font-bold uppercase tracking-widest w-full">
        Warm Pool {pool.enabled ? '(enabled)' : '(disabled)'}
      </span>
      <span><span className="text-neutral-500">size</span>: <span className="font-mono text-neutral-300">{pool.current_size ?? 0}</span><span className="text-neutral-600">/{pool.target_size ?? 0}</span></span>
      <span><span className="text-neutral-500">idle</span>: <span className="font-mono text-violet-400">{pool.idle_count ?? 0}</span></span>
      <span><span className="text-neutral-500">idle cost</span>: <span className="font-mono text-amber-400" data-testid="pool-idle-cost">{fmtUsd(pool.projected_idle_cost_usd)}</span></span>
      {pool.idle_ttl_secs !== undefined && (
        <span><span className="text-neutral-500">idle TTL</span>: <span className="font-mono text-neutral-300">{pool.idle_ttl_secs}s</span></span>
      )}
      {funnel && (
        <>
          <span><span className="text-neutral-500">hits</span>: <span className="font-mono text-emerald-400">{funnel.pool_lease_hits}</span></span>
          <span><span className="text-neutral-500">cold</span>: <span className="font-mono text-neutral-300">{funnel.cold_dispatches}</span></span>
          <span><span className="text-neutral-500">hit rate</span>: <span className="font-mono text-neutral-300">{hitRate === null || hitRate === undefined ? '—' : `${Math.round(hitRate * 100)}%`}</span></span>
        </>
      )}
    </div>
  );
}

// Per-account warm-pool cost-amortization, as joined server-side by
// admin_health._get_pool_metrics -> cost_amortization_by_account. Defined
// locally because the shared PoolMetrics type (admin-ui/src/types/warmVm.ts)
// is owned by a sibling cluster; the field is read here via a widening cast so
// this component stays within its single declared file. Older backends omit the
// field entirely (cast yields undefined) and the row simply does not render.
interface PoolAccountAmortization {
  pool_idle_cost_usd: number;
  pool_lease_hits: number;
  cold_dispatches: number;
  total_dispatches: number;
  pool_hit_rate: number | null;
  cost_per_pool_hit_usd: number | null;
}

function PoolAccountMixRow({ pool }: { pool: PoolMetrics }) {
  const byAccount = (pool as PoolMetrics & {
    cost_amortization_by_account?: Record<string, PoolAccountAmortization>;
  }).cost_amortization_by_account ?? {};
  // Busiest account first so the operator reads the heaviest dispatch mix on top.
  const entries = Object.entries(byAccount).sort(
    (a, b) => b[1].total_dispatches - a[1].total_dispatches,
  );
  if (entries.length === 0) return null;
  return (
    <div
      className="mb-3 text-[10px] text-neutral-400 pt-2 border-t border-neutral-800/60"
      data-testid="pool-account-mix-row"
    >
      <span className="text-neutral-500 font-bold uppercase tracking-widest block mb-1">
        Warm Pool by Account
      </span>
      <div className="space-y-1">
        {entries.map(([account, a]) => {
          const hr = a.pool_hit_rate;
          return (
            <div
              key={account}
              className="flex flex-wrap items-baseline gap-x-3 gap-y-0.5"
              data-testid={`pool-account-${account}`}
            >
              <span className="font-mono text-violet-300 break-all min-w-0">{account}</span>
              <span><span className="text-neutral-500">hit rate</span>: <span className="font-mono text-neutral-300">{hr === null || hr === undefined ? '—' : `${Math.round(hr * 100)}%`}</span></span>
              <span><span className="text-neutral-500">warm</span>: <span className="font-mono text-emerald-400">{a.pool_lease_hits}</span><span className="text-neutral-600">/{a.total_dispatches}</span></span>
              <span><span className="text-neutral-500">idle cost</span>: <span className="font-mono text-amber-400">{fmtUsd(a.pool_idle_cost_usd)}</span></span>
              <span><span className="text-neutral-500">$/warm</span>: <span className="font-mono text-neutral-300">{fmtUsd(a.cost_per_pool_hit_usd ?? undefined)}</span></span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function SpawnPinFunnelRow({ funnel }: { funnel: SpawnPinFunnelSnapshot }) {
  const skippedDisabled = funnel.skipped_by_reason?.disabled ?? 0;
  const skippedBreaker = funnel.skipped_by_reason?.breaker_tripped ?? 0;
  const skippedSourceCold = funnel.skipped_by_reason?.source_cold ?? 0;
  const skippedStale = funnel.skipped_by_reason?.stale_source ?? 0;
  const skippedSuppressed = funnel.skipped_by_reason?.caller_suppressed ?? 0;
  return (
    <div className="mb-3 flex flex-wrap gap-x-4 gap-y-1 text-[10px] text-neutral-400 pt-2 border-t border-neutral-800/60">
      <span className="text-neutral-500 font-bold uppercase tracking-widest w-full">
        Spawn Pin Funnel ({funnel.window_hours}h)
      </span>
      <span><span className="text-neutral-500">attempts</span>: <span className="font-mono text-neutral-300">{funnel.spawn_attempts}</span></span>
      <span><span className="text-neutral-500">pin set</span>: <span className="font-mono text-emerald-400">{funnel.pin_set}</span></span>
      {skippedDisabled > 0 && (
        <span><span className="text-neutral-500">skip disabled</span>: <span className="font-mono text-amber-400">{skippedDisabled}</span></span>
      )}
      {skippedBreaker > 0 && (
        <span><span className="text-neutral-500">skip breaker</span>: <span className="font-mono text-red-400">{skippedBreaker}</span></span>
      )}
      {skippedSourceCold > 0 && (
        <span><span className="text-neutral-500">skip source cold</span>: <span className="font-mono text-neutral-300">{skippedSourceCold}</span></span>
      )}
      {skippedStale > 0 && (
        <span><span className="text-neutral-500">skip stale source</span>: <span className="font-mono text-neutral-300">{skippedStale}</span></span>
      )}
      {skippedSuppressed > 0 && (
        <span><span className="text-neutral-500">skip suppressed</span>: <span className="font-mono text-neutral-300">{skippedSuppressed}</span></span>
      )}
    </div>
  );
}

function BacklogReuseFunnelRow({ funnel }: { funnel: BacklogReuseFunnelSnapshot }) {
  const skipReasons = Object.entries(funnel.claim_skipped_by_reason ?? {})
    .filter(([, count]) => count > 0);
  const scanDep = funnel.scan_polls_with_depends_on_skip ?? 0;
  const scanPin = funnel.scan_polls_with_pinned_vm_skip ?? 0;
  const scanFile = funnel.scan_polls_with_file_conflict_skip ?? 0;
  const scanSat = funnel.scan_polls_saturated ?? 0;
  return (
    <div className="mb-3 flex flex-wrap gap-x-4 gap-y-1 text-[10px] text-neutral-400 pt-2 border-t border-neutral-800/60">
      <span className="text-neutral-500 font-bold uppercase tracking-widest w-full">
        Backlog Reuse Funnel ({funnel.window_hours}h)
      </span>
      <span><span className="text-neutral-500">eligible</span>: <span className="font-mono text-neutral-300">{funnel.completed_vms_with_eligible_backlog}</span></span>
      <span><span className="text-neutral-500">defers</span>: <span className="font-mono text-neutral-300">{funnel.teardown_defers_scheduled}</span></span>
      <span><span className="text-neutral-500">polls</span>: <span className="font-mono text-neutral-300">{funnel.next_role_polls_during_defer}</span></span>
      <span><span className="text-neutral-500">claimed</span>: <span className="font-mono text-emerald-400">{funnel.warm_claims_succeeded}</span></span>
      {skipReasons.map(([reason, count]) => (
        <span key={reason} data-testid={`backlog-reuse-skip-${reason}`}>
          <span className="text-neutral-500">skip {reason}</span>
          :{' '}
          <span className={`font-mono ${reason === 'breaker_tripped' ? 'text-red-400' : reason === 'disabled' ? 'text-amber-400' : 'text-neutral-300'}`}>
            {count}
          </span>
        </span>
      ))}
      {scanDep > 0 && (
        <span data-testid="backlog-reuse-scan-depends_on">
          <span className="text-neutral-500">skip dep</span>:{' '}
          <span className="font-mono text-neutral-300">{scanDep}</span>
        </span>
      )}
      {scanPin > 0 && (
        <span data-testid="backlog-reuse-scan-pinned_vm">
          <span className="text-neutral-500">skip pin</span>:{' '}
          <span className="font-mono text-neutral-300">{scanPin}</span>
        </span>
      )}
      {scanFile > 0 && (
        <span data-testid="backlog-reuse-scan-file_conflict">
          <span className="text-neutral-500">skip file</span>:{' '}
          <span className="font-mono text-neutral-300">{scanFile}</span>
        </span>
      )}
      {scanSat > 0 && (
        <span data-testid="backlog-reuse-scan-saturated">
          <span className="text-neutral-500">saturated</span>:{' '}
          <span className="font-mono text-amber-400">{scanSat}</span>
        </span>
      )}
    </div>
  );
}

function ChainFunnelRow({ funnel }: { funnel: ChainContinuityFunnelSnapshot }) {
  const skippedDisabled = funnel.skipped_by_reason?.disabled ?? 0;
  const skippedBreaker = funnel.skipped_by_reason?.breaker_tripped ?? 0;
  return (
    <div className="mb-3 flex flex-wrap gap-x-4 gap-y-1 text-[10px] text-neutral-400 pt-2 border-t border-neutral-800/60">
      <span className="text-neutral-500 font-bold uppercase tracking-widest w-full">
        Chain Continuity Funnel ({funnel.window_hours}h)
      </span>
      <span><span className="text-neutral-500">polls</span>: <span className="font-mono text-neutral-300">{funnel.poll_arrivals}</span></span>
      <span><span className="text-neutral-500">claimed</span>: <span className="font-mono text-emerald-400">{funnel.chain_claimed}</span></span>
      <span><span className="text-neutral-500">no pinned</span>: <span className="font-mono text-neutral-300">{funnel.no_pinned_task}</span></span>
      <span><span className="text-neutral-500">other VM</span>: <span className="font-mono text-neutral-300">{funnel.another_vm_owns_pr}</span></span>
      <span><span className="text-neutral-500">doomed</span>: <span className="font-mono text-neutral-300">{funnel.doomed_vm}</span></span>
      {skippedDisabled > 0 && (
        <span><span className="text-neutral-500">skip disabled</span>: <span className="font-mono text-amber-400">{skippedDisabled}</span></span>
      )}
      {skippedBreaker > 0 && (
        <span><span className="text-neutral-500">skip breaker</span>: <span className="font-mono text-red-400">{skippedBreaker}</span></span>
      )}
    </div>
  );
}

function VmCard({ vm, onViewTask }: { vm: WarmVmRow; onViewTask?: (taskId: string) => void }) {
  const hbAge = ageSecs(vm.last_heartbeat);
  const aliveDot = vm.is_alive ? 'bg-emerald-400' : 'bg-neutral-600';
  const cur = vm.current_task;

  return (
    <div className="py-2 text-[11px]">
      <div className="flex items-center justify-between gap-2 mb-1">
        <span className="flex items-center gap-1.5 font-mono text-neutral-300" title={vm.vm_instance_id}>
          <span className={`inline-block w-1.5 h-1.5 rounded-full shrink-0 ${aliveDot}`} />
          {shortVm(vm.vm_instance_id)}
        </span>
        <span className="font-mono text-neutral-400 text-[10px]">
          {hbAge === null ? <span className="text-neutral-600">no hb</span> : (
            <span className={vm.is_alive ? 'text-neutral-400' : 'text-amber-400'}>
              <Clock className="w-2.5 h-2.5 inline mr-0.5" />{fmtAge(hbAge)}
            </span>
          )}
        </span>
      </div>
      {cur ? (
        <button
          onClick={() => onViewTask?.(cur.id)}
          className="w-full text-left hover:text-blue-400 transition-colors mb-1"
        >
          <div className="flex items-center gap-1.5">
            {cur.warm_start && <Flame aria-label="Warm start" className="w-2.5 h-2.5 text-orange-400 shrink-0"><title>Warm start</title></Flame>}
            <span className="text-neutral-200 truncate" title={cur.title}>{cur.title || '(untitled)'}</span>
          </div>
          <div className="text-[10px] text-neutral-500 font-mono mt-0.5">
            {cur.task_type}
            {cur.pr_number !== null && <> · PR #{cur.pr_number}</>}
            {cur.warm_start && cur.warm_claim_origin === 'backlog_reuse' && (
              <> · <span className="text-sky-400">backlog</span></>
            )}
            {cur.warm_start && cur.warm_claim_origin === 'warm_pool' && (
              <> · <span className="text-emerald-400">pool</span></>
            )}
            {cur.warm_start && cur.warm_claim_origin !== 'backlog_reuse' && cur.warm_claim_origin !== 'warm_pool' && cur.prior_role_vm_id && (
              <> · <span className="text-orange-400">chain</span></>
            )}
            {cur.warm_start && cur.prior_role_vm_id && (
              <> · from <span title={cur.prior_role_vm_id}>{shortVm(cur.prior_role_vm_id)}</span></>
            )}
          </div>
        </button>
      ) : (
        <div className="text-neutral-600 italic mb-1">idle</div>
      )}
      <div className="grid grid-cols-2 gap-x-3 text-[10px] text-neutral-500">
        <span>Setup: <span className="font-mono text-neutral-400">{cur ? fmtSetupSecs(cur.setup_secs) : '—'}</span></span>
        <span>Pinned: <span className="font-mono text-neutral-400">{vm.warm_pinned_queued.length || '—'}</span></span>
        {vm.recent_completions.length > 0 && (
          <span className="col-span-2 mt-0.5">
            Recent:{' '}
            <span className="font-mono">
              {vm.recent_completions.slice(0, 5).map((rc, i) => (
                <button
                  key={rc.id}
                  onClick={() => onViewTask?.(rc.id)}
                  className={`hover:text-blue-400 transition-colors ${rc.status === 'FAILED' ? 'text-red-400' : 'text-neutral-400'}`}
                  title={`${rc.task_type} · ${rc.status}`}
                >
                  {i > 0 && <span className="text-neutral-600"> · </span>}{rc.task_type}
                </button>
              ))}
            </span>
          </span>
        )}
      </div>
    </div>
  );
}

function VmRow({ vm, onViewTask }: { vm: WarmVmRow; onViewTask?: (taskId: string) => void }) {
  const hbAge = ageSecs(vm.last_heartbeat);
  const aliveDot = vm.is_alive ? 'bg-emerald-400' : 'bg-neutral-600';
  const cur = vm.current_task;

  return (
    <tr className="border-t border-neutral-800/60 align-top">
      <td className="py-1.5 pr-3 font-mono text-neutral-300" title={vm.vm_instance_id}>
        <span className="flex items-center gap-1.5">
          <span className={`inline-block w-1.5 h-1.5 rounded-full ${aliveDot}`} />
          {shortVm(vm.vm_instance_id)}
        </span>
      </td>
      <td className="py-1.5 pr-3 font-mono text-neutral-400 whitespace-nowrap">
        {hbAge === null ? <span className="text-neutral-600">never</span> : (
          <span className={vm.is_alive ? 'text-neutral-400' : 'text-amber-400'}>
            <Clock className="w-2.5 h-2.5 inline mr-0.5" />{fmtAge(hbAge)}
          </span>
        )}
      </td>
      <td className="py-1.5 pr-3">
        {cur ? (
          <button
            onClick={() => onViewTask?.(cur.id)}
            className="text-left hover:text-blue-400 transition-colors"
          >
            <div className="flex items-center gap-1.5">
              {cur.warm_start && <Flame aria-label="Warm start" className="w-2.5 h-2.5 text-orange-400 shrink-0"><title>Warm start</title></Flame>}
              <span className="text-neutral-200 truncate max-w-[280px]" title={cur.title}>{cur.title || '(untitled)'}</span>
            </div>
            <div className="text-[10px] text-neutral-500 font-mono mt-0.5">
              {cur.task_type}
              {cur.pr_number !== null && <> · PR #{cur.pr_number}</>}
              {cur.warm_start && cur.warm_claim_origin === 'backlog_reuse' && (
                <> · <span className="text-sky-400">backlog</span></>
              )}
              {cur.warm_start && cur.warm_claim_origin === 'warm_pool' && (
                <> · <span className="text-emerald-400">pool</span></>
              )}
              {cur.warm_start && cur.warm_claim_origin !== 'backlog_reuse' && cur.warm_claim_origin !== 'warm_pool' && cur.prior_role_vm_id && (
                <> · <span className="text-orange-400">chain</span></>
              )}
              {cur.warm_start && cur.prior_role_vm_id && (
                <> · from <span title={cur.prior_role_vm_id}>{shortVm(cur.prior_role_vm_id)}</span></>
              )}
            </div>
          </button>
        ) : (
          <span className="text-neutral-600 italic">idle</span>
        )}
      </td>
      <td className="py-1.5 pr-3 font-mono text-neutral-400 whitespace-nowrap">
        {cur ? fmtSetupSecs(cur.setup_secs) : '—'}
      </td>
      <td className="py-1.5 pr-3">
        {vm.warm_pinned_queued.length === 0 ? (
          <span className="text-neutral-600">—</span>
        ) : (
          <div className="space-y-0.5">
            {vm.warm_pinned_queued.map((p) => {
              // ageSecs is Math.max(0, now - deadline): a positive value means
              // the deadline has already passed.  Avoid an inline Date.now()
              // call here — the lint rule blocks impure functions in render,
              // and we already encode the comparison via the helper.
              const dlAge = ageSecs(p.warm_handoff_deadline);
              const expired = dlAge !== null && dlAge > 0;
              return (
                <button
                  key={p.id}
                  onClick={() => onViewTask?.(p.id)}
                  className="block text-left hover:text-blue-400 transition-colors text-[10px] font-mono"
                >
                  <span className="text-neutral-300">{p.task_type}</span>
                  {p.warm_target_pr !== null && <span className="text-neutral-500"> #{p.warm_target_pr}</span>}
                  {expired && <span className="text-red-400"> · expired</span>}
                </button>
              );
            })}
          </div>
        )}
      </td>
      <td className="py-1.5">
        {vm.recent_completions.length === 0 ? (
          <span className="text-neutral-600">—</span>
        ) : (
          <div className="flex flex-wrap gap-1 text-[10px] font-mono">
            {vm.recent_completions.slice(0, 5).map((rc) => (
              <button
                key={rc.id}
                onClick={() => onViewTask?.(rc.id)}
                className={`hover:text-blue-400 transition-colors ${rc.status === 'FAILED' ? 'text-red-400' : 'text-neutral-400'}`}
                title={`${rc.task_type} · ${rc.status}`}
              >
                {rc.task_type}
              </button>
            ))}
          </div>
        )}
      </td>
    </tr>
  );
}

// Pool-vs-chain-vs-backlog split of warm starts over the 24h window, plus the
// pool-origin share. Renders nothing when the backend omits the field (older
// deploy) or there were no warm starts to attribute (null). Colors match the
// in-flight claim conventions used elsewhere in the panel: pool=emerald,
// backlog=sky, chain=orange.
function WarmStartOriginRow({ breakdown }: { breakdown?: WarmStartOriginBreakdown | null }) {
  if (!breakdown) return null;
  const share = Math.round(breakdown.pool_origin_share * 100);
  return (
    <span data-testid="warm-start-origin-breakdown" className="w-full flex flex-wrap gap-x-3 gap-y-0.5">
      <span className="text-neutral-500">warm by origin</span>:
      <span><span className="text-neutral-500">pool</span> <span className="font-mono text-emerald-400">{breakdown.warm_pool}</span></span>
      <span><span className="text-neutral-500">backlog</span> <span className="font-mono text-sky-400">{breakdown.backlog_reuse}</span></span>
      <span><span className="text-neutral-500">chain</span> <span className="font-mono text-orange-400">{breakdown.chain_continuity}</span></span>
      <span data-testid="warm-start-pool-share"><span className="text-neutral-500">pool share</span> <span className="font-mono text-emerald-400">{share}%</span></span>
    </span>
  );
}

function EffMetric({ label, value, delta, lowerIsBetter }: {
  label: string; value: string; delta: number | null | undefined; lowerIsBetter: boolean;
}) {
  const arrow = delta === null || delta === undefined || delta === 0 ? null : delta > 0 ? '↑' : '↓';
  const improving = delta === null || delta === undefined ? null : lowerIsBetter ? delta < 0 : delta > 0;
  const cls = improving === null ? 'text-neutral-500' : improving ? 'text-emerald-400' : 'text-red-400';
  return (
    <span>
      <span className="text-neutral-500">{label}</span>:{' '}
      <span className="font-mono text-neutral-300">{value}</span>
      {arrow && <span className={`ml-0.5 ${cls}`}>{arrow}</span>}
    </span>
  );
}
