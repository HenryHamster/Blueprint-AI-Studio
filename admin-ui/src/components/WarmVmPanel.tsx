import { useEffect, useState } from 'react';
import { ChevronDown, ChevronUp, Server, Flame, AlertTriangle, Clock } from 'lucide-react';
import { API_BASE_URL, authGet } from '../api';
import type { WarmVmStateResponse, WarmVmRow } from '../types/warmVm';

const POLL_INTERVAL_MS = 10_000;
const STORAGE_KEY = 'blueprint-admin:warmVmPanelCollapsed';

function ageSecs(iso: string | null): number | null {
  if (!iso) return null;
  const t = new Date(iso).getTime();
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

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const res = await authGet(`${API_BASE_URL}/api/admin/warm-vm-state`);
        if (cancelled) return;
        if (!res.ok) {
          setError(`HTTP ${res.status}`);
          return;
        }
        setData(await res.json() as WarmVmStateResponse);
        setError(null);
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      }
    };
    void load();
    const id = setInterval(load, POLL_INTERVAL_MS);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  const toggle = () => {
    const next = !collapsed;
    setCollapsed(next);
    localStorage.setItem(STORAGE_KEY, String(next));
  };

  const vmCount = data?.active_vms.length ?? 0;
  const warmChains = data?.warm_pin_metrics.active_warm_chains ?? 0;
  const clearsHour = data?.warm_pin_metrics.clears_last_hour ?? 0;
  const pinnedQueued = data?.warm_pin_metrics.active_pinned_queued_total ?? 0;
  const flagOn = data?.warm_continuity_enabled ?? false;
  const overThreshold = clearsHour > 5;

  return (
    <div className="mb-3 shrink-0">
      <div className="flex items-center gap-2">
        <button
          onClick={toggle}
          className="flex items-center gap-2 text-[11px] font-bold text-neutral-400 uppercase tracking-widest hover:text-neutral-300 transition-colors"
        >
          {collapsed ? <ChevronDown className="w-3 h-3" /> : <ChevronUp className="w-3 h-3" />}
          <Server className="w-3 h-3" />
          VM Fleet
          <span
            className={
              flagOn
                ? 'ml-1 bg-emerald-500/20 border border-emerald-500/40 text-emerald-400 text-[10px] font-bold px-1.5 py-0.5 rounded leading-none'
                : 'ml-1 bg-neutral-700/40 border border-neutral-600/40 text-neutral-400 text-[10px] font-bold px-1.5 py-0.5 rounded leading-none'
            }
            title={flagOn ? 'warm_continuity_enabled = true' : 'warm_continuity_enabled = false (cold-only)'}
          >
            {flagOn ? 'WARM' : 'COLD'}
          </span>
          {warmChains > 0 && (
            <span className="ml-1 bg-orange-500/20 border border-orange-500/40 text-orange-400 text-[10px] font-bold px-1.5 py-0.5 rounded-full leading-none flex items-center gap-1">
              <Flame className="w-2.5 h-2.5" />
              {warmChains} chain{warmChains !== 1 ? 's' : ''}
            </span>
          )}
          {overThreshold && (
            <span className="ml-1 bg-red-500/20 border border-red-500/40 text-red-400 text-[10px] font-bold px-1.5 py-0.5 rounded-full leading-none flex items-center gap-1">
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
                <span><span className="text-neutral-500">pinned queued</span>: <span className="font-mono text-neutral-300">{pinnedQueued}</span></span>
                <span><span className="text-neutral-500">clears/hr</span>: <span className={`font-mono ${overThreshold ? 'text-red-400' : 'text-neutral-300'}`}>{clearsHour}</span></span>
                <span><span className="text-neutral-500">clears/24h</span>: <span className="font-mono text-neutral-300">{data.warm_pin_metrics.clears_last_24h}</span></span>
                <span><span className="text-neutral-500">poll timeout</span>: <span className="font-mono text-neutral-300">{data.next_role_poll_timeout_secs}s</span></span>
              </div>

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
                <div className="overflow-x-auto">
                  <table className="w-full text-[11px] border-collapse">
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
                </div>
              )}
            </>
          )}
        </div>
      )}
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
