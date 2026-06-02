import { useState, useCallback, useEffect } from 'react';
import { ChevronDown, ChevronUp, Server, Flame, AlertTriangle, Clock } from 'lucide-react';
import { API_BASE_URL, apiFetch, authGet, enableWarmPoolForProject, disableWarmPoolForProject, fetchWarmPoolTargets, setWarmPoolTargetForProject, fetchEfficiencyAxes } from '../api';
import type { PoolPayoffVerdict, PoolPayoffState } from '../api';
import { parseTimestamp } from '../utils/time';
import type { WarmVmStateResponse, WarmVmRow, ChainContinuityFunnelSnapshot, SpawnPinFunnelSnapshot, BacklogReuseFunnelSnapshot, PoolMetrics, PoolHitRateEstimate, WarmPoolTargets } from '../types/warmVm';
import type { Project } from '../types/project';
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

// Per-project warm-pool enable/disable control. Lets an operator scope the pool
// to ONE project (enable), see the currently-scoped set, and remove a project
// (disable) — the safe one-project-at-a-time rollout lever. Reads the current
// scope from pool_metrics.project_allowlist (empty = fleet-wide). Renders only
// when the parent passes a projects list.
function WarmPoolScopeRow({ projects, allowlist, busy, onEnable, onDisable }: {
  projects: Project[];
  allowlist: string[];
  busy: boolean;
  onEnable: (projectId: string, projectName: string) => void;
  onDisable: (projectId: string, projectName: string) => void;
}) {
  const [selected, setSelected] = useState('');
  const scopedIds = new Set(allowlist);
  const nameFor = (id: string) => projects.find((p) => p.id === id)?.name ?? id;
  // Candidates for enable: projects not already scoped.
  const candidates = projects.filter((p) => !scopedIds.has(p.id));
  // Show the scoped set straight from the allowlist so an id with no matching
  // project (e.g. a project not visible to this user) still renders + is removable.
  return (
    <div
      className="mb-3 text-[10px] text-neutral-400 pt-2 border-t border-neutral-800/60"
      data-testid="warm-pool-scope-row"
    >
      <span className="text-neutral-500 font-bold uppercase tracking-widest block mb-1">
        Warm Pool Scope
      </span>
      <div className="flex flex-wrap items-center gap-2 mb-1">
        <select
          value={selected}
          onChange={(e) => setSelected(e.target.value)}
          disabled={busy || candidates.length === 0}
          data-testid="warm-pool-scope-select"
          className="bg-neutral-800 border border-neutral-700 rounded px-1.5 py-0.5 text-[10px] text-neutral-200 max-w-[180px] disabled:opacity-50"
        >
          <option value="">{candidates.length === 0 ? 'all projects scoped' : 'Select project…'}</option>
          {candidates.map((p) => (
            <option key={p.id} value={p.id}>{p.name}</option>
          ))}
        </select>
        <button
          onClick={() => { if (selected) onEnable(selected, nameFor(selected)); }}
          disabled={busy || !selected}
          data-testid="warm-pool-enable-btn"
          className={
            'bg-emerald-500/20 border border-emerald-500/40 text-emerald-400 hover:bg-emerald-500/30' +
            ' text-[10px] font-bold px-1.5 py-0.5 rounded leading-none transition-colors' +
            (busy || !selected ? ' opacity-50 cursor-not-allowed' : ' cursor-pointer')
          }
        >
          Enable
        </button>
      </div>
      {allowlist.length === 0 ? (
        <span className="text-neutral-600 italic">
          No projects scoped — enabling the pool scopes it to your chosen project.
        </span>
      ) : (
        <div className="space-y-0.5">
          {allowlist.map((id) => (
            <div
              key={id}
              className="flex items-center gap-2"
              data-testid={`warm-pool-scoped-${id}`}
            >
              <span className="font-mono text-violet-300 break-all min-w-0">{nameFor(id)}</span>
              <button
                onClick={() => onDisable(id, nameFor(id))}
                disabled={busy}
                data-testid={`warm-pool-disable-${id}`}
                className={
                  'bg-neutral-700/40 border border-neutral-600/40 text-neutral-300 hover:bg-neutral-700/60' +
                  ' text-[10px] font-bold px-1.5 py-0.5 rounded leading-none transition-colors' +
                  (busy ? ' opacity-50 cursor-not-allowed' : ' cursor-pointer')
                }
              >
                Disable
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// Per-project warm-pool PAYOFF VERDICT. Fetches the efficiency-axes response
// for each scoped project (pool.project_allowlist — normally one) and renders
// the synthesized verdict the backend folds from the LIFT signals (pool-origin
// warm-start share + dispatch-funnel hit rate) and the NO-regression signal
// (idle-inclusive cost-per-merged-PR drift). Answers the bounded-enable
// question — "did enabling the pool produce a measured reuse lift WITHOUT a
// cost regression?" — in one place, next to the enable/disable/scope controls.
// Degrades silently: a project whose backend omits `pool_payoff_verdict`
// (older snapshot) simply contributes no badge.
const PAYOFF_BADGE: Record<PoolPayoffState, { label: string; cls: string }> = {
  PROVEN: { label: 'PROVEN', cls: 'text-emerald-400 border-emerald-500/40 bg-emerald-500/10' },
  COST_REGRESSION: { label: 'COST REGRESSION', cls: 'text-red-400 border-red-500/40 bg-red-500/10' },
  NO_LIFT: { label: 'NO LIFT', cls: 'text-amber-400 border-amber-500/40 bg-amber-500/10' },
  INSUFFICIENT_DATA: { label: 'INSUFFICIENT DATA', cls: 'text-neutral-400 border-neutral-600/40 bg-neutral-700/20' },
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
      className="mb-3 text-[10px] text-neutral-400 pt-2 border-t border-neutral-800/60"
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

function fmtEstRate(v: number | null | undefined): string {
  if (v === null || v === undefined) return '—';
  return `${Math.round(v * 100)}%`;
}

// Pre-enable counterfactual: lets an operator pick a project + candidate pool
// target size and see the estimated warm-pool hit rate over recent dispatches
// BEFORE enabling the pool (and paying real idle cost). Read-only — it GETs the
// offline estimate endpoint and is re-runnable to tune the target size. The
// project selector is populated from the active-VM dispatch mix (deduped
// project_ids); when no VM carries a project_id there is nothing to estimate so
// the control hides itself.
function PoolHitRateEstimateRow({ data, targets, onTargetSet }: {
  data: WarmVmStateResponse;
  // Current per-project target map + global fallback (null while loading or on
  // an older backend that omits the settings fields). Used to show whether the
  // scoped project's target is explicit or inherited.
  targets: WarmPoolTargets | null;
  // Refresh callback fired after a successful set/clear so the panel re-reads
  // both the warm-vm state and the target map.
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
      className="mb-3 text-[10px] text-neutral-400 pt-2 border-t border-neutral-800/60"
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
          className="bg-neutral-800 border border-neutral-700 rounded px-1 py-0.5 text-neutral-200 font-mono max-w-[160px]"
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
            className="w-12 bg-neutral-800 border border-neutral-700 rounded px-1 py-0.5 text-neutral-200 font-mono"
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
              'bg-neutral-700/40 border border-neutral-600/40 text-neutral-300 hover:bg-neutral-700/60 ' +
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
