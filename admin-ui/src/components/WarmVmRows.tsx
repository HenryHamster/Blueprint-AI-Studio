import { useState } from 'react';
import { AlertTriangle, Clock, Flame } from 'lucide-react';
import { formatTimeOnly } from '../utils/time';
import type { WarmVmRow, ChainContinuityFunnelSnapshot, SpawnPinFunnelSnapshot, BacklogReuseFunnelSnapshot, PoolMetrics, WarmVmStateResponse } from '../types/warmVm';
import type { Project } from '../types/project';
import { ageSecs, fmtAge, fmtSetupSecs, shortVm, fmtUsd } from './WarmVmUtils';

// ── Pool cost-guard banner ────────────────────────────────────────────────────

export function PoolCostGuardBanner({ guard }: { guard: NonNullable<PoolMetrics['cost_guard']> }) {
  const hitRate = guard.hit_rate;
  return (
    <div
      className="mb-3 px-2 py-1.5 rounded border border-amber-500/40 bg-amber-500/10 text-amber-300 text-ui-md flex items-start gap-2"
      data-testid="pool-cost-guard-banner"
    >
      <AlertTriangle className="w-3 h-3 shrink-0 mt-0.5" />
      <div className="min-w-0">
        <div className="font-bold uppercase tracking-wide text-ui-sm mb-0.5">
          Warm-pool cost guard engaged — draining idle members, provisioning paused
        </div>
        {guard.reason && <div className="text-amber-200/90 mb-0.5">{guard.reason}</div>}
        <div className="flex flex-wrap gap-x-3 gap-y-0.5 font-mono text-ui-sm text-amber-200/80">
          <span data-testid="pool-cost-guard-hit-rate">
            hit rate {hitRate === null || hitRate === undefined ? '—' : `${Math.round(hitRate * 100)}%`}
            {' '}/ floor {Math.round(guard.min_hit_rate * 100)}%
          </span>
          <span data-testid="pool-cost-guard-idle-cost">
            idle spend {fmtUsd(guard.idle_cost_usd)}{guard.window_hours != null ? ` (${guard.window_hours}h window)` : ''} / floor {fmtUsd(guard.min_idle_cost_usd)}
          </span>
          <span>dispatches {guard.total_dispatches}</span>
        </div>
      </div>
    </div>
  );
}

// ── Pool metrics row ──────────────────────────────────────────────────────────

export function PoolMetricsRow({ pool }: { pool: PoolMetrics }) {
  const funnel = pool.funnel;
  const hitRate = funnel?.pool_hit_rate;
  return (
    <div className="mb-3 flex flex-wrap gap-x-4 gap-y-1 text-ui-sm text-neutral-400 pt-2 border-t border-neutral-800/60" data-testid="pool-metrics-row">
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
          {(funnel.lease_miss_total ?? 0) > 0 && (
            <span data-testid="pool-lease-misses">
              <span className="text-neutral-500">misses</span>:{' '}
              <span className="font-mono text-rose-400">{funnel.lease_miss_total}</span>
              <span className="text-neutral-600">
                {' '}({Object.entries(funnel.lease_miss_by_reason ?? {})
                  .sort((a, b) => b[1] - a[1])
                  .map(([reason, n]) => `${reason} ${n}`)
                  .join(', ')})
              </span>
            </span>
          )}
        </>
      )}
    </div>
  );
}

// ── Breaker callouts ──────────────────────────────────────────────────────────

export function BreakerCallout({ name, suppressedUntil, failures, threshold, testid }: {
  name: string;
  suppressedUntil: string | null | undefined;
  failures: number | undefined;
  threshold: number | undefined;
  testid: string;
}) {
  return (
    <div
      data-testid={testid}
      className="px-2 py-1.5 rounded border border-red-500/40 bg-red-500/10 text-red-400 text-ui-md flex flex-wrap items-center gap-x-2 gap-y-1"
    >
      <AlertTriangle className="w-3 h-3 shrink-0" />
      <span className="font-bold uppercase tracking-wide">{name} breaker</span>
      <span className="bg-red-500/20 border border-red-500/40 text-red-300 text-ui-sm font-bold px-1.5 py-0.5 rounded leading-none">
        OPEN
      </span>
      {suppressedUntil && (
        <span className="flex items-center gap-1 text-red-300/90">
          <Clock className="w-2.5 h-2.5 shrink-0" />
          until {formatTimeOnly(suppressedUntil)}
        </span>
      )}
      <span className="font-mono text-red-300/90">
        {failures ?? 0}/{threshold ?? 0} failures
      </span>
    </div>
  );
}

export function WarmBreakerCallouts({ metrics }: { metrics: WarmVmStateResponse['warm_pin_metrics'] }) {
  const poolOpen = metrics.pool_circuit_breaker_suppressed ?? false;
  const continuityOpen = metrics.circuit_breaker_suppressed ?? false;
  const backlogOpen = metrics.backlog_reuse_circuit_breaker_suppressed ?? false;
  if (!poolOpen && !continuityOpen && !backlogOpen) return null;
  return (
    <div className="mb-3 space-y-1.5 pt-2 border-t border-neutral-800/60" data-testid="warm-breaker-callouts">
      {poolOpen && (
        <BreakerCallout
          name="Pool claim"
          suppressedUntil={metrics.pool_suppressed_until}
          failures={metrics.pool_claim_failures_last_hour}
          threshold={metrics.pool_failure_rate_threshold}
          testid="pool-breaker-callout"
        />
      )}
      {continuityOpen && (
        <BreakerCallout
          name="Chain continuity"
          suppressedUntil={metrics.suppressed_until}
          failures={metrics.clears_last_hour}
          threshold={metrics.clear_rate_threshold}
          testid="continuity-breaker-callout"
        />
      )}
      {backlogOpen && (
        <BreakerCallout
          name="Backlog reuse"
          suppressedUntil={metrics.backlog_reuse_suppressed_until}
          failures={metrics.backlog_reuse_failures_last_hour}
          threshold={metrics.backlog_reuse_failure_rate_threshold}
          testid="backlog-breaker-callout"
        />
      )}
    </div>
  );
}

// ── Account retarget callout ──────────────────────────────────────────────────

export function AccountRetargetCallout({ pool, poolOn, accountRetargetEnabled, flipping, onEnable }: {
  pool: PoolMetrics | undefined;
  poolOn: boolean;
  accountRetargetEnabled: boolean;
  flipping: boolean;
  onEnable: () => void;
}) {
  if (!poolOn) return null;
  if (accountRetargetEnabled) return null;
  const hitRate = pool?.funnel?.pool_hit_rate;
  if (hitRate !== null && hitRate !== undefined && hitRate >= 0.5) return null;
  const noCompatMisses = pool?.funnel?.lease_miss_by_reason?.['no_compatible_member'] ?? 0;
  if (noCompatMisses <= 0) return null;

  return (
    <div
      data-testid="account-retarget-callout"
      className="mb-3 px-2 py-1.5 rounded border border-amber-500/40 bg-amber-500/10 text-amber-300 text-ui-md flex flex-wrap items-center gap-x-2 gap-y-1"
    >
      <AlertTriangle className="w-3 h-3 shrink-0 text-amber-400" />
      <span>
        Idle pool members exist but are pinned to the wrong account/model for incoming tasks
        ({noCompatMisses} miss{noCompatMisses !== 1 ? 'es' : ''}).
        Enable <span className="font-bold">Account Retarget</span> so any idle member can serve any account.
      </span>
      <button
        onClick={onEnable}
        disabled={flipping}
        data-testid="account-retarget-enable-btn"
        className={
          'bg-violet-500/20 border border-violet-500/40 text-violet-300 hover:bg-violet-500/30 ' +
          'text-ui-sm font-bold px-1.5 py-0.5 rounded leading-none transition-colors shrink-0' +
          (flipping ? ' opacity-50 cursor-wait' : ' cursor-pointer')
        }
      >
        {flipping ? '…' : 'Enable Account Retarget'}
      </button>
    </div>
  );
}

// ── Warm pool scope row ───────────────────────────────────────────────────────

export function WarmPoolScopeRow({ projects, allowlist, busy, onEnable, onDisable }: {
  projects: Project[];
  allowlist: string[];
  busy: boolean;
  onEnable: (projectId: string, projectName: string) => void;
  onDisable: (projectId: string, projectName: string) => void;
}) {
  const [selected, setSelected] = useState('');
  const scopedIds = new Set(allowlist);
  const nameFor = (id: string) => projects.find((p) => p.id === id)?.name ?? id;
  const candidates = projects.filter((p) => !scopedIds.has(p.id));
  return (
    <div
      className="mb-3 text-ui-sm text-neutral-400 pt-2 border-t border-neutral-800/60"
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
          className="bg-workbench-panel border border-neutral-700 rounded px-1.5 py-0.5 text-ui-sm text-neutral-200 max-w-[180px] disabled:opacity-50"
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
            ' text-ui-sm font-bold px-1.5 py-0.5 rounded leading-none transition-colors' +
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
                  'bg-workbench-layer/40 border border-neutral-600/40 text-neutral-300 hover:bg-workbench-hover/60' +
                  ' text-ui-sm font-bold px-1.5 py-0.5 rounded leading-none transition-colors' +
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

// ── Pool account mix row ──────────────────────────────────────────────────────

interface PoolAccountAmortization {
  pool_idle_cost_usd: number;
  pool_lease_hits: number;
  cold_dispatches: number;
  total_dispatches: number;
  pool_hit_rate: number | null;
  cost_per_pool_hit_usd: number | null;
}

export function PoolAccountMixRow({ pool }: { pool: PoolMetrics }) {
  const byAccount = (pool as PoolMetrics & {
    cost_amortization_by_account?: Record<string, PoolAccountAmortization>;
  }).cost_amortization_by_account ?? {};
  const missByAccount = pool.funnel?.by_account ?? {};
  const entries = Object.entries(byAccount).sort(
    (a, b) => b[1].total_dispatches - a[1].total_dispatches,
  );
  if (entries.length === 0) return null;
  return (
    <div
      className="mb-3 text-ui-sm text-neutral-400 pt-2 border-t border-neutral-800/60"
      data-testid="pool-account-mix-row"
    >
      <span className="text-neutral-500 font-bold uppercase tracking-widest block mb-1">
        Warm Pool by Account
      </span>
      <div className="space-y-1">
        {entries.map(([account, a]) => {
          const hr = a.pool_hit_rate;
          const miss = missByAccount[account];
          const missTotal = miss?.lease_miss_total ?? 0;
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
              {missTotal > 0 && (
                <span data-testid={`pool-account-misses-${account}`}>
                  <span className="text-neutral-500">misses</span>:{' '}
                  <span className="font-mono text-rose-400">{missTotal}</span>
                  <span className="text-neutral-600">
                    {' '}({Object.entries(miss?.lease_miss_by_reason ?? {})
                      .sort((x, y) => y[1] - x[1])
                      .map(([reason, n]) => `${reason} ${n}`)
                      .join(', ')})
                  </span>
                </span>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ── Funnel rows ───────────────────────────────────────────────────────────────

export function SpawnPinFunnelRow({ funnel }: { funnel: SpawnPinFunnelSnapshot }) {
  const skippedDisabled = funnel.skipped_by_reason?.disabled ?? 0;
  const skippedBreaker = funnel.skipped_by_reason?.breaker_tripped ?? 0;
  const skippedSourceCold = funnel.skipped_by_reason?.source_cold ?? 0;
  const skippedStale = funnel.skipped_by_reason?.stale_source ?? 0;
  const skippedSuppressed = funnel.skipped_by_reason?.caller_suppressed ?? 0;
  return (
    <div className="mb-3 flex flex-wrap gap-x-4 gap-y-1 text-ui-sm text-neutral-400 pt-2 border-t border-neutral-800/60">
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

export function BacklogReuseFunnelRow({ funnel }: { funnel: BacklogReuseFunnelSnapshot }) {
  const skipReasons = Object.entries(funnel.claim_skipped_by_reason ?? {})
    .filter(([, count]) => count > 0);
  const scanDep = funnel.scan_polls_with_depends_on_skip ?? 0;
  const scanPin = funnel.scan_polls_with_pinned_vm_skip ?? 0;
  const scanFile = funnel.scan_polls_with_file_conflict_skip ?? 0;
  const scanSat = funnel.scan_polls_saturated ?? 0;
  return (
    <div className="mb-3 flex flex-wrap gap-x-4 gap-y-1 text-ui-sm text-neutral-400 pt-2 border-t border-neutral-800/60">
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

export function ChainFunnelRow({ funnel }: { funnel: ChainContinuityFunnelSnapshot }) {
  const skippedDisabled = funnel.skipped_by_reason?.disabled ?? 0;
  const skippedBreaker = funnel.skipped_by_reason?.breaker_tripped ?? 0;
  return (
    <div className="mb-3 flex flex-wrap gap-x-4 gap-y-1 text-ui-sm text-neutral-400 pt-2 border-t border-neutral-800/60">
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

// ── VM card / row ─────────────────────────────────────────────────────────────

export function VmCard({ vm, onViewTask }: { vm: WarmVmRow; onViewTask?: (taskId: string) => void }) {
  const hbAge = ageSecs(vm.last_heartbeat);
  const aliveDot = vm.is_alive ? 'bg-emerald-400' : 'bg-workbench-card';
  const cur = vm.current_task;

  return (
    <div className="py-2 text-ui-md">
      <div className="flex items-center justify-between gap-2 mb-1">
        <span className="flex items-center gap-1.5 font-mono text-neutral-300" title={vm.vm_instance_id}>
          <span className={`inline-block w-1.5 h-1.5 rounded-full shrink-0 ${aliveDot}`} />
          {shortVm(vm.vm_instance_id)}
        </span>
        <span className="font-mono text-neutral-400 text-ui-sm">
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
            {cur.warm_start && <Flame aria-label="Warm start" className="w-2.5 h-2.5 text-cyan-300 shrink-0"><title>Warm start</title></Flame>}
            <span className="text-neutral-200 truncate" title={cur.title}>{cur.title || '(untitled)'}</span>
          </div>
          <div className="text-ui-sm text-neutral-500 font-mono mt-0.5">
            {cur.task_type}
            {cur.pr_number !== null && <> · PR #{cur.pr_number}</>}
            {cur.warm_start && cur.warm_claim_origin === 'backlog_reuse' && (
              <> · <span className="text-sky-400">backlog</span></>
            )}
            {cur.warm_start && cur.warm_claim_origin === 'warm_pool' && (
              <> · <span className="text-emerald-400">pool</span></>
            )}
            {cur.warm_start && cur.warm_claim_origin !== 'backlog_reuse' && cur.warm_claim_origin !== 'warm_pool' && cur.prior_role_vm_id && (
              <> · <span className="text-cyan-300">chain</span></>
            )}
            {cur.warm_start && cur.prior_role_vm_id && (
              <> · from <span title={cur.prior_role_vm_id}>{shortVm(cur.prior_role_vm_id)}</span></>
            )}
          </div>
        </button>
      ) : (
        <div className="text-neutral-600 italic mb-1">idle</div>
      )}
      <div className="grid grid-cols-2 gap-x-3 text-ui-sm text-neutral-500">
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

export function VmRow({ vm, onViewTask }: { vm: WarmVmRow; onViewTask?: (taskId: string) => void }) {
  const hbAge = ageSecs(vm.last_heartbeat);
  const aliveDot = vm.is_alive ? 'bg-emerald-400' : 'bg-workbench-card';
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
              {cur.warm_start && <Flame aria-label="Warm start" className="w-2.5 h-2.5 text-cyan-300 shrink-0"><title>Warm start</title></Flame>}
              <span className="text-neutral-200 truncate max-w-[280px]" title={cur.title}>{cur.title || '(untitled)'}</span>
            </div>
            <div className="text-ui-sm text-neutral-500 font-mono mt-0.5">
              {cur.task_type}
              {cur.pr_number !== null && <> · PR #{cur.pr_number}</>}
              {cur.warm_start && cur.warm_claim_origin === 'backlog_reuse' && (
                <> · <span className="text-sky-400">backlog</span></>
              )}
              {cur.warm_start && cur.warm_claim_origin === 'warm_pool' && (
                <> · <span className="text-emerald-400">pool</span></>
              )}
              {cur.warm_start && cur.warm_claim_origin !== 'backlog_reuse' && cur.warm_claim_origin !== 'warm_pool' && cur.prior_role_vm_id && (
                <> · <span className="text-cyan-300">chain</span></>
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
              // the deadline has already passed.
              const dlAge = ageSecs(p.warm_handoff_deadline);
              const expired = dlAge !== null && dlAge > 0;
              return (
                <button
                  key={p.id}
                  onClick={() => onViewTask?.(p.id)}
                  className="block text-left hover:text-blue-400 transition-colors text-ui-sm font-mono"
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
          <div className="flex flex-wrap gap-1 text-ui-sm font-mono">
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

// ── Efficiency metric ─────────────────────────────────────────────────────────

export function EffMetric({ label, value, delta, lowerIsBetter }: {
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
