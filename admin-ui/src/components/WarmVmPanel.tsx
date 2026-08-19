/* eslint-disable react-hooks/preserve-manual-memoization */
import { useState, useCallback, useEffect } from 'react';
import { ChevronDown, ChevronUp, Server, Flame, AlertTriangle } from 'lucide-react';
import { API_BASE_URL, apiFetch, authGet, enableWarmPoolForProject, disableWarmPoolForProject, fetchWarmPoolTargets, setWarmPoolTargetForProject, fetchEfficiencyAxes } from '../api';
import type { PoolPayoffVerdict, PoolPayoffState } from '../api';
import { snapshotFreshness } from '../utils/time';
import type { WarmVmStateResponse, WarmPoolTargets, WarmReuseDropoutDiagnostic, WarmReuseDropoutTrendBucket, PoolHitRateEstimate } from '../types/warmVm';
import type { Project } from '../types/project';
import { useEfficiencySnapshot, fmtEffSecs, fmtEffRate, fmtEffIter } from '../hooks/useEfficiencySnapshot';
import { WarmStartOriginRow } from './WarmStartOriginRow';
import { usePoller } from '../hooks/usePoller';
import { shortVm } from './WarmVmUtils';
import {
  PoolCostGuardBanner,
  PoolMetricsRow,
  WarmBreakerCallouts,
  AccountRetargetCallout,
  WarmPoolScopeRow,
  PoolAccountMixRow,
  SpawnPinFunnelRow,
  BacklogReuseFunnelRow,
  ChainFunnelRow,
  VmCard,
  VmRow,
  EffMetric,
} from './WarmVmRows';

const POLL_INTERVAL_MS = 10_000;
const STORAGE_KEY = 'blueprint-admin:warmVmPanelCollapsed';
const SNAPSHOT_STALE_AFTER_SECONDS = 120;
const STALE_CLASSES = 'opacity-60 saturate-50';

interface Props {
  onViewTask?: (taskId: string) => void;
  // Optional so existing mounts/tests keep working. When provided, the panel
  // renders a per-project warm-pool enable/disable scope control.
  projects?: Project[];
}

export function WarmVmPanel({ onViewTask, projects = [] }: Props) {
  const [collapsed, setCollapsed] = useState(() => localStorage.getItem(STORAGE_KEY) === 'true');
  const [data, setData] = useState<WarmVmStateResponse | null>(null);
  const [targets, setTargets] = useState<WarmPoolTargets | null>(null);
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
    // Per-project target map is a separate, best-effort read (settings endpoint).
    // A failure here must not blank the panel — leave the last-known targets so
    // the set-target control degrades gracefully on an older/erroring backend.
    try {
      setTargets(await fetchWarmPoolTargets());
    } catch {
      // ignore — targets stay at their previous value (or null)
    }
  }, []);

  usePoller(load, { intervalMs: POLL_INTERVAL_MS, deps: [load] });

  const toggle = () => {
    const next = !collapsed;
    setCollapsed(next);
    localStorage.setItem(STORAGE_KEY, String(next));
  };

  const flipFlag = useCallback(async (
    configKey:
      | 'warm_continuity_enabled'
      | 'warm_backlog_reuse_enabled'
      | 'warm_pool_enabled'
      | 'warm_pool_account_retarget_enabled'
      | 'warm_pool_cross_provider_retarget_enabled',
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

  const enableProject = useCallback(async (projectId: string, projectName: string) => {
    const ok = window.confirm(
      `ENABLE the warm pool for "${projectName}"?\n\n` +
      `Scopes the pool to this project, turns the master switch on, and warms ` +
      `the first members. Other scoped projects are unaffected.`
    );
    if (!ok) return;
    setFlipping(true);
    try {
      await enableWarmPoolForProject(projectId);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setFlipping(false);
    }
  }, [load]);

  const disableProject = useCallback(async (projectId: string, projectName: string) => {
    const ok = window.confirm(
      `DISABLE the warm pool for "${projectName}"?\n\n` +
      `Drains this project's pool members and removes it from scope. Other ` +
      `enabled projects keep their pool — the master switch only turns off if ` +
      `this is the last scoped project.`
    );
    if (!ok) return;
    setFlipping(true);
    try {
      await disableWarmPoolForProject(projectId);
      await load();
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
  const accountRetargetOn = data?.warm_pool_account_retarget_enabled ?? data?.account_retarget_enabled ?? false;
  const crossProviderRetargetOn = data?.warm_pool_cross_provider_retarget_enabled ?? false;
  const overThreshold = clearsHour > 5;
  const { asOfLabel, stale: freshnessStale } = snapshotFreshness(
    data?.precomputed_at,
    data?.precompute_age_seconds,
    SNAPSHOT_STALE_AFTER_SECONDS,
  );
  const stale = freshnessStale || data?.served_stale === true;

  return (
    <div className="mb-3 shrink-0">
      <div className="flex items-center gap-2 flex-wrap">
        <button
          onClick={toggle}
          className="flex items-center gap-2 text-ui-md font-bold text-neutral-400 uppercase tracking-widest hover:text-neutral-300 transition-colors"
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
              : 'bg-workbench-layer/40 border border-neutral-600/40 text-neutral-400 hover:bg-workbench-hover/60'
            ) +
            ' text-ui-sm font-bold px-1.5 py-0.5 rounded leading-none transition-colors' +
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
              : 'bg-workbench-layer/40 border border-neutral-600/40 text-neutral-400 hover:bg-workbench-hover/60'
            ) +
            ' text-ui-sm font-bold px-1.5 py-0.5 rounded leading-none transition-colors' +
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
              : 'bg-workbench-layer/40 border border-neutral-600/40 text-neutral-400 hover:bg-workbench-hover/60'
            ) +
            ' text-ui-sm font-bold px-1.5 py-0.5 rounded leading-none transition-colors' +
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
          onClick={() => { void flipFlag('warm_pool_account_retarget_enabled', 'warm-pool account retarget', accountRetargetOn); }}
          disabled={flipping || !data}
          data-testid="warm-pool-account-retarget-toggle"
          className={
            (accountRetargetOn
              ? 'bg-violet-500/20 border border-violet-500/40 text-violet-400 hover:bg-violet-500/30'
              : 'bg-workbench-layer/40 border border-neutral-600/40 text-neutral-400 hover:bg-workbench-hover/60'
            ) +
            ' text-ui-sm font-bold px-1.5 py-0.5 rounded leading-none transition-colors' +
            (flipping ? ' opacity-50 cursor-wait' : ' cursor-pointer') +
            (!data ? ' opacity-50 cursor-not-allowed' : '')
          }
          title={
            flipping
              ? 'flipping…'
              : (accountRetargetOn
                  ? 'Click to DISABLE warm-pool account retarget (strict per-account match)'
                  : 'Click to ENABLE warm-pool account retarget (an idle member can serve a different account)')
          }
        >
          {flipping ? '…' : (accountRetargetOn ? 'RETARGET' : 'NO-RETARGET')}
        </button>
        <button
          onClick={() => { void flipFlag('warm_pool_cross_provider_retarget_enabled', 'warm-pool cross-provider retarget', crossProviderRetargetOn); }}
          disabled={flipping || !data}
          data-testid="warm-pool-cross-provider-retarget-toggle"
          className={
            (crossProviderRetargetOn
              ? 'bg-violet-500/20 border border-violet-500/40 text-violet-400 hover:bg-violet-500/30'
              : 'bg-workbench-layer/40 border border-neutral-600/40 text-neutral-400 hover:bg-workbench-hover/60'
            ) +
            ' text-ui-sm font-bold px-1.5 py-0.5 rounded leading-none transition-colors' +
            (flipping ? ' opacity-50 cursor-wait' : ' cursor-pointer') +
            (!data ? ' opacity-50 cursor-not-allowed' : '')
          }
          title={
            flipping
              ? 'flipping…'
              : (crossProviderRetargetOn
                  ? 'Click to DISABLE warm-pool cross-provider retarget (same-provider only)'
                  : 'Click to ENABLE warm-pool cross-provider retarget (an idle member can serve a different provider)')
          }
        >
          {flipping ? '…' : (crossProviderRetargetOn ? 'X-PROV' : 'SAME-PROV')}
        </button>
        <button
          onClick={toggle}
          className="flex items-center gap-2 text-ui-md font-bold text-neutral-400 uppercase tracking-widest hover:text-neutral-300 transition-colors"
        >
          {warmChains > 0 && (
            <span className="bg-cyan-500/15 border border-cyan-500/35 text-cyan-300 text-ui-sm font-bold px-1.5 py-0.5 rounded-full leading-none flex items-center gap-1">
              <Flame className="w-2.5 h-2.5" />
              {warmChains} chain{warmChains !== 1 ? 's' : ''}
            </span>
          )}
          {overThreshold && (
            <span className="bg-red-500/20 border border-red-500/40 text-red-400 text-ui-sm font-bold px-1.5 py-0.5 rounded-full leading-none flex items-center gap-1">
              <AlertTriangle className="w-2.5 h-2.5" />
              {clearsHour} clears/hr
            </span>
          )}
          {collapsed && data && (
            <span className="text-ui-sm text-neutral-500 font-mono ml-1 normal-case tracking-normal font-normal">
              {vmCount} VM{vmCount !== 1 ? 's' : ''}
              {pinnedQueued > 0 && <> · {pinnedQueued} pinned</>}
            </span>
          )}
        </button>
        {data && asOfLabel && (
          <span
            className={`text-ui-sm ${stale ? 'text-amber-400 font-semibold' : 'text-neutral-500'}`}
            title={stale ? 'Cached snapshot is stale — may not reflect current VM fleet state' : 'When this VM fleet snapshot was computed'}
          >
            {stale && '⚠ '}as of {asOfLabel}{stale && ' · stale'}
          </span>
        )}
      </div>

      {!collapsed && (
        <div className={`mt-2 bg-workbench-base border border-neutral-800 rounded-lg p-3 text-xs ${stale && data && !error ? STALE_CLASSES : ''}`}>
          {error && (
            <div className="text-red-400 mb-2">Failed to load warm-VM state: {error}</div>
          )}
          {!error && data && (
            <>
              <div className="flex flex-wrap gap-x-4 gap-y-1 mb-3 text-ui-sm text-neutral-400">
                <span><span className="text-neutral-500">VMs</span>: <span className="font-mono text-neutral-300">{vmCount}</span></span>
                <span><span className="text-neutral-500">warm chains</span>: <span className="font-mono text-neutral-300">{warmChains}</span></span>
                <span><span className="text-neutral-500">backlog reuse</span>: <span className="font-mono text-sky-400">{backlogClaims}</span></span>
                <span data-testid="pool-origin-claims"><span className="text-neutral-500">pool origin</span>: <span className="font-mono text-emerald-400">{poolClaims}</span>{poolShare !== null && <span className="text-neutral-500"> ({Math.round(poolShare * 100)}%)</span>}</span>
                <span><span className="text-neutral-500">chain claims</span>: <span className="font-mono text-cyan-300">{chainClaims}</span></span>
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
                <div className="mb-3 flex flex-wrap gap-x-4 gap-y-1 text-ui-sm text-neutral-400 pt-2 border-t border-neutral-800/60">
                  <span className="text-neutral-500 font-bold uppercase tracking-widest w-full">Pipeline Efficiency</span>
                  <EffMetric label="cold-start p50" value={fmtEffSecs(efficiency.window_24h?.setup_p50_secs)} delta={efficiency.window_24h?.vs_prior_setup_p50_delta} lowerIsBetter />
                  <EffMetric label="reuse rate" value={fmtEffRate(efficiency.window_24h?.warm_reuse_rate)} delta={efficiency.window_24h?.vs_prior_warm_reuse_rate_delta} lowerIsBetter={false} />
                  <EffMetric label="review cycles" value={fmtEffIter(efficiency.window_7d?.review_iter_median)} delta={efficiency.window_7d?.vs_prior_review_iter_median_delta} lowerIsBetter />
                  <WarmStartOriginRow breakdown={efficiency.window_24h?.warm_start_origin_breakdown} />
                </div>
              )}

              {pool?.cost_guard?.engaged && (
                <PoolCostGuardBanner guard={pool.cost_guard} />
              )}

              <WarmBreakerCallouts metrics={data.warm_pin_metrics} />

              <AccountRetargetCallout
                pool={pool}
                poolOn={poolOn}
                accountRetargetEnabled={accountRetargetOn}
                flipping={flipping}
                onEnable={() => { void flipFlag('warm_pool_account_retarget_enabled', 'warm-pool account retarget', accountRetargetOn); }}
              />

              {pool && <PoolMetricsRow pool={pool} />}

              {projects.length > 0 && (
                <WarmPoolScopeRow
                  projects={projects}
                  allowlist={pool?.project_allowlist ?? []}
                  busy={flipping}
                  onEnable={enableProject}
                  onDisable={disableProject}
                />
              )}

              {(pool?.project_allowlist?.length ?? 0) > 0 && (
                <PoolPayoffVerdictRow
                  projects={projects}
                  allowlist={pool?.project_allowlist ?? []}
                />
              )}

              {pool && <PoolAccountMixRow pool={pool} />}

              <PoolHitRateEstimateRow data={data} targets={targets} onTargetSet={load} />

              {data.spawn_pin_funnel && (
                <SpawnPinFunnelRow funnel={data.spawn_pin_funnel} />
              )}

              {data.backlog_reuse_funnel && data.backlog_reuse_funnel.window_hours && (
                <BacklogReuseFunnelRow funnel={data.backlog_reuse_funnel} />
              )}

              {pool?.project_allowlist && pool.project_allowlist.length > 0 && (
                <BacklogDropoutRow projectId={pool.project_allowlist[0]} />
              )}

              {data.chain_continuity_funnel && (
                <ChainFunnelRow funnel={data.chain_continuity_funnel} />
              )}

              {overThreshold && (
                <div className="mb-3 px-2 py-1.5 rounded border border-red-500/40 bg-red-500/10 text-red-400 text-ui-md flex items-center gap-2">
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
                  <table className="hidden sm:table w-full text-ui-md border-collapse">
                    <thead>
                      <tr className="text-neutral-500 text-ui-sm uppercase tracking-wide">
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

// ── Pool payoff verdict row ───────────────────────────────────────────────────

const PAYOFF_BADGE: Record<PoolPayoffState, { label: string; cls: string }> = {
  PROVEN: { label: 'PROVEN', cls: 'text-emerald-400 border-emerald-500/40 bg-emerald-500/10' },
  COST_REGRESSION: { label: 'COST REGRESSION', cls: 'text-red-400 border-red-500/40 bg-red-500/10' },
  NO_LIFT: { label: 'NO LIFT', cls: 'text-amber-400 border-amber-500/40 bg-amber-500/10' },
  INSUFFICIENT_DATA: { label: 'INSUFFICIENT DATA', cls: 'text-neutral-400 border-neutral-600/40 bg-workbench-layer/20' },
};

function fmtPct(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  return `${Math.round(v * 100)}%`;
}

function fmtDrift(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  const sign = v > 0 ? '+' : '';
  return `${sign}${v.toFixed(1)}%`;
}

function PoolPayoffVerdictRow({ projects, allowlist }: {
  projects: Project[];
  allowlist: string[];
}) {
  const [verdicts, setVerdicts] = useState<Record<string, PoolPayoffVerdict>>({});
  const nameFor = (id: string) => projects.find((p) => p.id === id)?.name ?? id;
  // Stable dependency key so the effect only refetches when the scoped set
  // actually changes (not on every parent re-render that recreates the array).
  const scopeKey = allowlist.join(',');

  useEffect(() => {
    let cancelled = false;
    const ids = scopeKey ? scopeKey.split(',') : [];
    void (async () => {
      const next: Record<string, PoolPayoffVerdict> = {};
      await Promise.all(
        ids.map(async (id) => {
          try {
            const axes = await fetchEfficiencyAxes(id);
            // Older backends omit the field — skip silently so the row degrades.
            if (axes.pool_payoff_verdict) next[id] = axes.pool_payoff_verdict;
          } catch {
            // A per-project fetch failure must not blank the whole row.
          }
        }),
      );
      if (!cancelled) setVerdicts(next);
    })();
    return () => { cancelled = true; };
  }, [scopeKey]);

  const entries = allowlist.filter((id) => verdicts[id]);
  if (entries.length === 0) return null;

  return (
    <div
      className="mb-3 text-ui-sm text-neutral-400 pt-2 border-t border-neutral-800/60"
      data-testid="pool-payoff-verdict-row"
    >
      <span className="text-neutral-500 font-bold uppercase tracking-widest block mb-1">
        Warm Pool Payoff
      </span>
      <div className="space-y-1.5">
        {entries.map((id) => {
          const v = verdicts[id];
          const badge = PAYOFF_BADGE[v.state] ?? PAYOFF_BADGE.INSUFFICIENT_DATA;
          const s = v.supporting;
          return (
            <div key={id} data-testid={`pool-payoff-${id}`} className="space-y-0.5">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-mono text-violet-300 break-all min-w-0">{nameFor(id)}</span>
                <span
                  data-testid={`pool-payoff-badge-${id}`}
                  className={`font-bold px-1.5 py-0.5 rounded border leading-none ${badge.cls}`}
                >
                  {badge.label}
                </span>
              </div>
              <div className="flex flex-wrap gap-x-4 gap-y-0.5" data-testid={`pool-payoff-numbers-${id}`}>
                <span><span className="text-neutral-500">pool hit rate</span>: <span className="font-mono text-neutral-300">{fmtPct(s.pool_hit_rate)}</span></span>
                <span><span className="text-neutral-500">pool-origin share</span>: <span className="font-mono text-neutral-300">{fmtPct(s.pool_origin_share)}</span></span>
                <span><span className="text-neutral-500">cost drift</span>: <span className="font-mono text-neutral-300">{fmtDrift(s.cost_drift_pct)}</span></span>
                <span><span className="text-neutral-500">pool dispatches</span>: <span className="font-mono text-neutral-300">{s.total_pool_dispatches ?? '—'}</span></span>
              </div>
              <div className="text-neutral-500 italic" data-testid={`pool-payoff-action-${id}`}>
                {v.recommended_action}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ── Pool hit-rate estimate row ────────────────────────────────────────────────

function fmtEstRate(v: number | null | undefined): string {
  if (v === null || v === undefined) return '—';
  return `${Math.round(v * 100)}%`;
}

function PoolHitRateEstimateRow({ data, targets, onTargetSet }: {
  data: WarmVmStateResponse;
  targets: WarmPoolTargets | null;
  onTargetSet: () => void | Promise<void>;
}) {
  const projectIds = Array.from(
    new Set(
      data.active_vms
        .map((vm) => vm.current_task?.project_id)
        .filter((pid): pid is string => !!pid),
    ),
  );
  const [projectId, setProjectId] = useState<string>('');
  const [targetSize, setTargetSize] = useState<number>(2);
  const [estimate, setEstimate] = useState<PoolHitRateEstimate | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [settingTarget, setSettingTarget] = useState(false);
  const [targetErr, setTargetErr] = useState<string | null>(null);

  const effectiveProject = projectId || projectIds[0] || '';

  const run = useCallback(async () => {
    if (!effectiveProject) return;
    setLoading(true);
    setErr(null);
    try {
      const res = await authGet(
        `${API_BASE_URL}/api/admin/projects/${encodeURIComponent(effectiveProject)}` +
          `/pool-hit-rate-estimate?target_size=${Math.max(0, Math.round(targetSize))}`,
      );
      if (!res.ok) {
        setErr(`HTTP ${res.status}`);
        setEstimate(null);
        return;
      }
      setEstimate((await res.json()) as PoolHitRateEstimate);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
      setEstimate(null);
    } finally {
      setLoading(false);
    }
  }, [effectiveProject, targetSize]);

  // Set this project's explicit target to the tuned N (or clear back to the
  // global default when `clear` is true). Refreshes the panel on success.
  const applyTarget = useCallback(async (clear: boolean) => {
    if (!effectiveProject) return;
    const value = clear ? null : Math.max(0, Math.round(targetSize));
    setSettingTarget(true);
    setTargetErr(null);
    try {
      await setWarmPoolTargetForProject(effectiveProject, value);
      await onTargetSet();
    } catch (e) {
      setTargetErr(e instanceof Error ? e.message : String(e));
    } finally {
      setSettingTarget(false);
    }
  }, [effectiveProject, targetSize, onTargetSet]);

  if (projectIds.length === 0) return null;

  // Resolve the current target for the scoped project: an explicit per-project
  // entry takes precedence; otherwise it inherits the global fallback.
  const explicitTarget = targets?.warm_pool_target_size_by_project?.[effectiveProject];
  const hasExplicit = explicitTarget !== undefined;
  const globalTarget = targets?.warm_pool_target_size;
  const currentTarget = hasExplicit ? explicitTarget : globalTarget;

  return (
    <div
      className="mb-3 text-ui-sm text-neutral-400 pt-2 border-t border-neutral-800/60"
      data-testid="pool-hit-rate-estimate-row"
    >
      <span className="text-neutral-500 font-bold uppercase tracking-widest block mb-1">
        Pre-enable Pool Hit-Rate Estimate
      </span>
      <div className="flex flex-wrap items-center gap-2 mb-1">
        <select
          aria-label="Project"
          data-testid="pool-estimate-project"
          value={effectiveProject}
          onChange={(e) => { setProjectId(e.target.value); setEstimate(null); }}
          className="bg-workbench-panel border border-neutral-700 rounded px-1 py-0.5 text-neutral-200 font-mono max-w-[160px]"
        >
          {projectIds.map((pid) => (
            <option key={pid} value={pid}>{shortVm(pid)}</option>
          ))}
        </select>
        <label className="flex items-center gap-1">
          <span className="text-neutral-500">target N</span>
          <input
            type="number"
            min={0}
            max={100}
            aria-label="Target size"
            data-testid="pool-estimate-target"
            value={targetSize}
            onChange={(e) => setTargetSize(Number(e.target.value))}
            className="w-12 bg-workbench-panel border border-neutral-700 rounded px-1 py-0.5 text-neutral-200 font-mono"
          />
        </label>
        <button
          onClick={() => { void run(); }}
          disabled={loading || !effectiveProject}
          data-testid="pool-estimate-run"
          className={
            'bg-violet-500/20 border border-violet-500/40 text-violet-300 hover:bg-violet-500/30 ' +
            'font-bold px-1.5 py-0.5 rounded leading-none transition-colors' +
            (loading ? ' opacity-50 cursor-wait' : ' cursor-pointer')
          }
        >
          {loading ? '…' : 'Estimate'}
        </button>
      </div>
      {err && <div className="text-red-400">Estimate failed: {err}</div>}
      {estimate && (
        <div className="flex flex-wrap gap-x-4 gap-y-1" data-testid="pool-estimate-result">
          <span title={estimate.assumptions}>
            <span className="text-neutral-500">at N={estimate.target_size}, est. hit rate</span>:{' '}
            <span className="font-mono text-emerald-400" data-testid="pool-estimate-hit-rate">
              {fmtEstRate(estimate.estimated_hit_rate)}
            </span>
          </span>
          <span><span className="text-neutral-500">warm/total</span>: <span className="font-mono text-neutral-300">{estimate.estimated_warm_hits}/{estimate.total_dispatches}</span></span>
          <span><span className="text-neutral-500">retarget</span>: <span className="font-mono text-violet-300">{fmtEstRate(estimate.account_retarget_scenario.estimated_hit_rate)}</span></span>
          <span><span className="text-neutral-500">window</span>: <span className="font-mono text-neutral-300">{estimate.window_days}d</span></span>
          <span className="text-neutral-600 italic w-full">estimate only — replays recent dispatches, provisions nothing</span>
        </div>
      )}
      <div
        className="flex flex-wrap items-center gap-2 mt-1 pt-1 border-t border-neutral-800/40"
        data-testid="pool-target-control"
      >
        <span className="text-neutral-500">
          target:{' '}
          {targets === null ? (
            <span className="font-mono text-neutral-300" data-testid="pool-target-current">—</span>
          ) : hasExplicit ? (
            <span className="font-mono text-violet-300" data-testid="pool-target-current">
              explicit {currentTarget}
            </span>
          ) : (
            <span className="font-mono text-neutral-300" data-testid="pool-target-current">
              inherited (global {globalTarget ?? 0})
            </span>
          )}
        </span>
        <button
          onClick={() => { void applyTarget(false); }}
          disabled={settingTarget || !effectiveProject}
          data-testid="pool-target-set"
          title={`Persist N=${Math.max(0, Math.round(targetSize))} as this project's warm-pool target`}
          className={
            'bg-violet-500/20 border border-violet-500/40 text-violet-300 hover:bg-violet-500/30 ' +
            'font-bold px-1.5 py-0.5 rounded leading-none transition-colors' +
            (settingTarget || !effectiveProject ? ' opacity-50 cursor-not-allowed' : ' cursor-pointer')
          }
        >
          {settingTarget ? '…' : `Set target = ${Math.max(0, Math.round(targetSize))}`}
        </button>
        {hasExplicit && (
          <button
            onClick={() => { void applyTarget(true); }}
            disabled={settingTarget}
            data-testid="pool-target-clear"
            title="Clear this project's explicit target so it inherits the global default"
            className={
              'bg-workbench-layer/40 border border-neutral-600/40 text-neutral-300 hover:bg-workbench-hover/60 ' +
              'font-bold px-1.5 py-0.5 rounded leading-none transition-colors' +
              (settingTarget ? ' opacity-50 cursor-not-allowed' : ' cursor-pointer')
            }
          >
            Clear
          </button>
        )}
      </div>
      {targetErr && <div className="text-red-400">Set target failed: {targetErr}</div>}
    </div>
  );
}

// ── Backlog dropout row ───────────────────────────────────────────────────────

function BacklogDropoutRow({ projectId }: { projectId: string }) {
  const [diagnostic, setDiagnostic] = useState<WarmReuseDropoutDiagnostic | null>(null);
  const [trend, setTrend] = useState<WarmReuseDropoutTrendBucket[]>([]);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const base = `${API_BASE_URL}/api/admin/projects/${encodeURIComponent(projectId)}`;
        const [diagRes, trendRes] = await Promise.all([
          authGet(`${base}/warm-reuse-dropout`),
          authGet(`${base}/warm-reuse-dropout-trend`),
        ]);
        if (!diagRes.ok || !trendRes.ok) return;
        const [diagData, trendData] = await Promise.all([diagRes.json(), trendRes.json()]);
        if (!cancelled) {
          setDiagnostic(diagData as WarmReuseDropoutDiagnostic);
          setTrend(((trendData as { buckets?: WarmReuseDropoutTrendBucket[] }).buckets ?? []));
        }
      } catch {
        // Degrade silently — dropout is observability only
      }
    })();
    return () => { cancelled = true; };
  }, [projectId]);

  if (!diagnostic) return null;

  const dominant = diagnostic.dominant_dropout;
  const recentBuckets = trend.slice(-3);

  return (
    <div
      className="mb-3 text-ui-sm text-neutral-400 pt-2 border-t border-neutral-800/60"
      data-testid="backlog-dropout-row"
    >
      <span className="text-neutral-500 font-bold uppercase tracking-widest block mb-1">
        Warm Reuse Dropout
      </span>
      <div className="flex flex-wrap gap-x-4 gap-y-1">
        <span>
          <span className="text-neutral-500">dominant</span>:{' '}
          <span className="font-mono text-neutral-300" data-testid="backlog-dropout-reason">
            {dominant ? dominant.reason : '—'}
          </span>
        </span>
        {dominant && (
          <span>
            <span className="text-neutral-500">share</span>:{' '}
            <span className="font-mono text-rose-400" data-testid="backlog-dropout-share">
              {Math.round(dominant.share * 100)}%
            </span>
          </span>
        )}
        {dominant && (
          <span>
            <span className="text-neutral-500">count</span>:{' '}
            <span className="font-mono text-neutral-300">{dominant.count}</span>
            <span className="text-neutral-600">/{dominant.total}</span>
          </span>
        )}
        {recentBuckets.length > 0 && (
          <span className="flex items-center gap-1">
            <span className="text-neutral-500">trend</span>:{' '}
            {recentBuckets.map((b, i) => {
              const share = b.dominant_share;
              const prevShare = i > 0 ? recentBuckets[i - 1].dominant_share : null;
              const arrow = share !== null && prevShare !== null
                ? (share > prevShare ? '↑' : share < prevShare ? '↓' : null)
                : null;
              const arrowCls = arrow === '↑' ? 'text-red-400' : 'text-emerald-400';
              return (
                <span key={i} className="font-mono text-neutral-500">
                  {i > 0 && <span className="text-neutral-700 mr-1">·</span>}
                  {share !== null ? `${Math.round(share * 100)}%` : '—'}
                  {arrow && <span className={arrowCls}>{arrow}</span>}
                </span>
              );
            })}
          </span>
        )}
      </div>
    </div>
  );
}
