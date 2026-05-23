import { useEffect, useState, useCallback } from 'react';
import { ChevronDown, ChevronUp, Server, Flame, AlertTriangle, Clock } from 'lucide-react';
import { API_BASE_URL, apiFetch, authGet } from '../api';
import { parseTimestamp } from '../utils/time';
import type { WarmVmStateResponse, WarmVmRow } from '../types/warmVm';
import { useEfficiencySnapshot, fmtEffSecs, fmtEffRate, fmtEffIter } from '../hooks/useEfficiencySnapshot';

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

  useEffect(() => {
    let cancelled = false;
    const wrapped = async () => { if (!cancelled) await load(); };
    void wrapped();
    const id = setInterval(wrapped, POLL_INTERVAL_MS);
    return () => { cancelled = true; clearInterval(id); };
  }, [load]);

  const toggle = () => {
    const next = !collapsed;
    setCollapsed(next);
    localStorage.setItem(STORAGE_KEY, String(next));
  };

  const flipFlag = useCallback(async (
    configKey: 'warm_continuity_enabled' | 'warm_backlog_reuse_enabled',
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
  const chainClaims = data?.warm_pin_metrics.active_chain_continuity_claims ?? 0;
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
                <span><span className="text-neutral-500">chain claims</span>: <span className="font-mono text-orange-400">{chainClaims}</span></span>
                <span><span className="text-neutral-500">pinned queued</span>: <span className="font-mono text-neutral-300">{pinnedQueued}</span></span>
                <span><span className="text-neutral-500">clears/hr</span>: <span className={`font-mono ${overThreshold ? 'text-red-400' : 'text-neutral-300'}`}>{clearsHour}</span></span>
                <span><span className="text-neutral-500">clears/24h</span>: <span className="font-mono text-neutral-300">{data.warm_pin_metrics.clears_last_24h}</span></span>
                <span><span className="text-neutral-500">poll timeout</span>: <span className="font-mono text-neutral-300">{data.next_role_poll_timeout_secs}s</span></span>
              </div>

              {efficiency && (
                <div className="mb-3 flex flex-wrap gap-x-4 gap-y-1 text-[10px] text-neutral-400 pt-2 border-t border-neutral-800/60">
                  <span className="text-neutral-500 font-bold uppercase tracking-widest w-full">Pipeline Efficiency</span>
                  <EffMetric label="cold-start p50" value={fmtEffSecs(efficiency.window_24h?.setup_p50_secs)} delta={efficiency.window_24h?.vs_prior_setup_p50_delta} lowerIsBetter />
                  <EffMetric label="reuse rate" value={fmtEffRate(efficiency.window_24h?.warm_reuse_rate)} delta={efficiency.window_24h?.vs_prior_warm_reuse_rate_delta} lowerIsBetter={false} />
                  <EffMetric label="review cycles" value={fmtEffIter(efficiency.window_7d?.review_iter_median)} delta={efficiency.window_7d?.vs_prior_review_iter_median_delta} lowerIsBetter />
                </div>
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
            {cur.warm_start && cur.warm_claim_origin !== 'backlog_reuse' && cur.prior_role_vm_id && (
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
              {cur.warm_start && cur.warm_claim_origin !== 'backlog_reuse' && cur.prior_role_vm_id && (
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
