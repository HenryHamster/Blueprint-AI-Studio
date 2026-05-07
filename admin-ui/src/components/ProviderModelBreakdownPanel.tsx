import { useState, useEffect } from 'react';
import { apiFetch } from '../api';
import type { AccountHealthEntry } from './PoolAccountHealthPanel';

interface QuarantineStatus {
  kind: string;
  started_at: string | null;
  account_rate: number | null;
  project_median: number | null;
  sample_size: number | null;
}

interface AccountBreakdown {
  account_id: string;
  total_tasks: number;
  success_count: number;
  success_rate: number;
  below_median: boolean;
  quarantine_status: QuarantineStatus | null;
}

interface BreakdownRow {
  provider: string;
  model: string;
  total_tasks: number;
  success_count: number;
  success_rate: number;
  distinct_accounts: number;
  project_median_success_rate: number;
  accounts?: AccountBreakdown[];
}

function srColor(rate: number | null) {
  if (rate == null) return 'text-neutral-500';
  if (rate >= 0.8) return 'text-emerald-400';
  if (rate >= 0.5) return 'text-amber-400';
  return 'text-red-400';
}

function fmtPct(rate: number | null) {
  return rate != null ? `${Math.round(rate * 100)}%` : '—';
}

function fmtDate(iso: string | null) {
  if (!iso) return '—';
  try {
    return new Date(iso).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
  } catch {
    return iso;
  }
}

function QuarantineBadge({
  qs,
  accountId,
  provider,
  model,
  onCleared,
}: {
  qs: QuarantineStatus;
  accountId: string;
  provider: string;
  model: string;
  onCleared: () => void;
}) {
  const [confirming, setConfirming] = useState(false);
  const [clearing, setClearing] = useState(false);

  const doClear = async () => {
    setClearing(true);
    try {
      const params = new URLSearchParams({ provider, model });
      await apiFetch(
        `/api/admin/max-plan-accounts/${accountId}/regression-quarantine/clear?${params}`,
        { method: 'POST' },
      );
      onCleared();
    } finally {
      setClearing(false);
      setConfirming(false);
    }
  };

  return (
    <div className="flex items-center gap-1.5 flex-wrap">
      <span className="text-[9px] font-semibold px-1.5 py-0.5 rounded border bg-orange-500/20 text-orange-400 border-orange-500/30 uppercase">
        quarantine:{qs.kind}
      </span>
      {qs.started_at && (
        <span className="text-[9px] text-neutral-500">{fmtDate(qs.started_at)}</span>
      )}
      {qs.account_rate != null && qs.project_median != null && (
        <span className="text-[9px] text-neutral-500 font-mono">
          {fmtPct(qs.account_rate)} vs {fmtPct(qs.project_median)} median
        </span>
      )}
      {qs.kind === 'regression' && (
        confirming ? (
          <span className="flex items-center gap-1">
            {/* eslint-disable-next-line viewport/min-touch-target -- pre-existing, dense UI; audit per rule introduction */}
            <button
              onClick={doClear}
              disabled={clearing}
              className="text-[9px] px-1.5 py-0.5 rounded bg-orange-500/30 text-orange-300 hover:bg-orange-500/50 transition-colors disabled:opacity-50"
            >
              {clearing ? 'Clearing…' : 'Confirm clear'}
            </button>
            {/* eslint-disable-next-line viewport/min-touch-target -- pre-existing, dense UI; audit per rule introduction */}
            <button
              onClick={() => setConfirming(false)}
              className="text-[9px] px-1.5 py-0.5 rounded bg-neutral-700/50 text-neutral-400 hover:bg-neutral-700 transition-colors"
            >
              Cancel
            </button>
          </span>
        ) : (
          // eslint-disable-next-line viewport/min-touch-target -- pre-existing, dense UI; audit per rule introduction
          <button
            onClick={() => setConfirming(true)}
            className="text-[9px] px-1.5 py-0.5 rounded bg-neutral-700/50 text-neutral-400 hover:text-orange-300 hover:bg-orange-500/20 transition-colors"
          >
            Clear quarantine
          </button>
        )
      )}
    </div>
  );
}

export function ProviderModelBreakdownPanel({
  selectedProjectId,
  accounts: healthAccounts,
}: {
  selectedProjectId: string | null;
  accounts?: AccountHealthEntry[] | null;
}) {
  const [rows, setRows] = useState<BreakdownRow[]>([]);
  const [expandedKey, setExpandedKey] = useState<string | null>(null);
  const [hideSingleAccount, setHideSingleAccount] = useState(false);
  const [loading, setLoading] = useState(true);

  const labelFor = (accountId: string) =>
    healthAccounts?.find(a => a.account_id === accountId)?.label ?? accountId.slice(0, 12);

  const load = async () => {
    try {
      const params = new URLSearchParams({ expand_accounts: 'true' });
      if (selectedProjectId) params.set('project_id', selectedProjectId);
      const res = await apiFetch(`/api/admin/max-plan-accounts/provider-model-breakdown?${params}`);
      if (res.ok) {
        const json = await res.json();
        setRows(json.rows ?? []);
      }
    } catch { /* silent */ } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    let cancelled = false;
    const safeLoad = async () => { if (!cancelled) await load(); };
    safeLoad();
    const id = setInterval(safeLoad, 30_000);
    return () => { cancelled = true; clearInterval(id); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedProjectId]);

  if (loading || rows.length === 0) return null;

  const visible = hideSingleAccount ? rows.filter(r => r.distinct_accounts >= 2) : rows;
  if (visible.length === 0) return null;

  return (
    <section>
      <div className="flex items-center justify-between mb-2">
        <h2 className="text-[11px] font-bold text-neutral-400 uppercase tracking-widest">
          Provider / Model Breakdown
        </h2>
        {/* eslint-disable-next-line viewport/min-touch-target -- pre-existing, dense UI; audit per rule introduction */}
        <button
          onClick={() => setHideSingleAccount(v => !v)}
          className={`text-[9px] px-1.5 py-0.5 rounded border transition-colors ${
            hideSingleAccount
              ? 'bg-neutral-700/50 text-neutral-300 border-neutral-600/50'
              : 'text-neutral-500 border-neutral-800/50 hover:text-neutral-300'
          }`}
        >
          {hideSingleAccount ? 'Show all' : 'Hide single-account'}
        </button>
      </div>
      <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg overflow-hidden">
        <table className="w-full text-xs">
          <thead>
            <tr className="text-[10px] text-neutral-500 border-b border-neutral-800/50">
              <th className="text-left px-3 py-2 font-medium">Provider / Model</th>
              <th className="text-right px-3 py-2 font-medium">Tasks</th>
              <th className="text-right px-3 py-2 font-medium">Rate</th>
              <th className="text-right px-3 py-2 font-medium">Median</th>
              <th className="text-right px-3 py-2 font-medium">Accts</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-neutral-800/30">
            {visible.map(row => {
              const key = `${row.provider}::${row.model}`;
              const expanded = expandedKey === key;
              return [
                <tr
                  key={key}
                  className="cursor-pointer hover:bg-neutral-800/20 transition-colors"
                  onClick={() => setExpandedKey(expanded ? null : key)}
                >
                  <td className="px-3 py-2">
                    <span className="font-medium text-neutral-200">{row.model}</span>
                    <span className="text-neutral-500 ml-1.5 text-[10px]">{row.provider}</span>
                  </td>
                  <td className="text-right px-3 py-2 text-neutral-300 tabular-nums">{row.total_tasks}</td>
                  <td className={`text-right px-3 py-2 tabular-nums ${srColor(row.success_rate)}`}>
                    {fmtPct(row.success_rate)}
                  </td>
                  <td className={`text-right px-3 py-2 tabular-nums ${srColor(row.project_median_success_rate)}`}>
                    {fmtPct(row.project_median_success_rate)}
                  </td>
                  <td className="text-right px-3 py-2 text-neutral-400 tabular-nums">{row.distinct_accounts}</td>
                </tr>,
                expanded && row.accounts && row.accounts.length > 0 && (
                  <tr key={`${key}__expand`}>
                    <td colSpan={5} className="px-3 pb-3 pt-1 bg-neutral-950/40">
                      <div className="space-y-1.5">
                        {row.accounts.map(acct => (
                          <div key={acct.account_id} className="rounded border border-neutral-800/50 bg-neutral-900/60 px-2.5 py-1.5">
                            <div className="flex items-center gap-2 flex-wrap">
                              <span className="text-[10px] font-medium text-neutral-300">
                                {labelFor(acct.account_id)}
                              </span>
                              <span className="text-[9px] text-neutral-500 font-mono tabular-nums">
                                {acct.total_tasks} tasks
                              </span>
                              <span className={`text-[10px] font-mono font-bold tabular-nums ${srColor(acct.success_rate)}`}>
                                {fmtPct(acct.success_rate)}
                              </span>
                              {acct.below_median && (
                                <span className="text-[9px] font-semibold px-1.5 py-0.5 rounded border bg-amber-500/20 text-amber-400 border-amber-500/30 uppercase">
                                  below median
                                </span>
                              )}
                            </div>
                            {acct.quarantine_status && (
                              <div className="mt-1">
                                <QuarantineBadge
                                  qs={acct.quarantine_status}
                                  accountId={acct.account_id}
                                  provider={row.provider}
                                  model={row.model}
                                  onCleared={load}
                                />
                              </div>
                            )}
                          </div>
                        ))}
                      </div>
                    </td>
                  </tr>
                ),
              ];
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}
