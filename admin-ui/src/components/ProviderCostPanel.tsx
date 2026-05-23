import { useState, useEffect } from 'react';
import { apiFetch } from '../api';
import { useEfficiencySnapshot, fmtEffSecs, fmtEffRate, fmtEffIter } from '../hooks/useEfficiencySnapshot';

interface ProviderCostRow {
  provider: string;
  account_id: string | null;
  account_name: string;
  total_tasks: number;
  success_count: number;
  merged_count: number;
  total_cost_usd: number;
  success_rate: number | null;
  cost_per_merged_pr: number | null;
}

interface ProviderCostSummary {
  window_hours: number;
  rows: ProviderCostRow[];
}

export function ProviderCostPanel({
  selectedProjectId,
  windowHours,
}: {
  selectedProjectId: string | null;
  windowHours: number;
}) {
  const [data, setData] = useState<ProviderCostSummary | null>(null);
  const efficiency = useEfficiencySnapshot(selectedProjectId);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const params = new URLSearchParams({ hours: String(windowHours) });
        if (selectedProjectId) params.set('project_id', selectedProjectId);
        const res = await apiFetch(`/api/admin/provider-cost-summary?${params}`);
        if (!cancelled && res.ok) setData(await res.json());
      } catch { /* silent */ }
    };
    load();
    const id = setInterval(load, 30_000);
    return () => { cancelled = true; clearInterval(id); };
  }, [selectedProjectId, windowHours]);

  if (!data || data.rows.length === 0) return null;

  const w24 = efficiency?.window_24h;
  const w7 = efficiency?.window_7d;

  return (
    <section>
      <h2 className="text-[11px] font-bold text-neutral-400 uppercase tracking-widest mb-2">Provider Cost &amp; Success</h2>
      {efficiency && (
        <div className="mb-2 flex flex-wrap gap-x-4 gap-y-1 text-[10px] text-neutral-400 px-1">
          <span className="text-neutral-500 font-bold uppercase tracking-widest w-full">Pipeline Efficiency</span>
          <EffMetric label="cold-start p50" value={fmtEffSecs(w24?.setup_p50_secs)} delta={w24?.vs_prior_setup_p50_delta} lowerIsBetter />
          <EffMetric label="reuse rate" value={fmtEffRate(w24?.warm_reuse_rate)} delta={w24?.vs_prior_warm_reuse_rate_delta} lowerIsBetter={false} />
          <EffMetric label="review cycles" value={fmtEffIter(w7?.review_iter_median)} delta={w7?.vs_prior_review_iter_median_delta} lowerIsBetter />
        </div>
      )}
      <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg overflow-hidden">
        {/* Mobile: stacked card layout */}
        <div className="sm:hidden divide-y divide-neutral-800/30">
          {data.rows.map((row, i) => {
            const srColor = row.success_rate == null ? 'text-neutral-500'
              : row.success_rate >= 0.8 ? 'text-emerald-400'
              : row.success_rate >= 0.5 ? 'text-amber-400'
              : 'text-red-400';
            const cpmColor = row.cost_per_merged_pr == null ? 'text-neutral-500'
              : row.cost_per_merged_pr <= 5 ? 'text-emerald-400'
              : row.cost_per_merged_pr <= 15 ? 'text-amber-400'
              : 'text-red-400';
            return (
              <div key={i} className="px-3 py-2 text-[10px]">
                <div className="flex items-start justify-between gap-2 mb-1">
                  <span className="font-medium text-neutral-200 break-words">{row.account_name}</span>
                  <span className="text-neutral-500 shrink-0">{row.provider}</span>
                </div>
                <div className="grid grid-cols-2 gap-x-4 gap-y-0.5 text-neutral-500">
                  <span>Tasks: <span className="text-neutral-300 tabular-nums">{row.total_tasks}</span></span>
                  <span>Success: <span className={`tabular-nums ${srColor}`}>{row.success_rate != null ? `${Math.round(row.success_rate * 100)}%` : '—'}</span></span>
                  <span>Spend: <span className="text-neutral-300 font-mono tabular-nums">${row.total_cost_usd.toFixed(2)}</span></span>
                  <span>$/Merge: <span className={`font-mono tabular-nums ${cpmColor}`}>{row.cost_per_merged_pr != null ? `$${row.cost_per_merged_pr.toFixed(2)}` : '—'}</span></span>
                </div>
              </div>
            );
          })}
        </div>
        {/* Desktop: table layout */}
        <table className="hidden sm:table w-full text-xs">
          <thead>
            <tr className="text-[10px] text-neutral-500 border-b border-neutral-800/50">
              <th className="text-left px-3 py-2 font-medium">Provider / Account</th>
              <th className="text-right px-3 py-2 font-medium">Tasks</th>
              <th className="text-right px-3 py-2 font-medium">Success</th>
              <th className="text-right px-3 py-2 font-medium">Spend</th>
              <th className="text-right px-3 py-2 font-medium">$/Merge</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-neutral-800/30">
            {data.rows.map((row, i) => {
              const srColor = row.success_rate == null ? 'text-neutral-500'
                : row.success_rate >= 0.8 ? 'text-emerald-400'
                : row.success_rate >= 0.5 ? 'text-amber-400'
                : 'text-red-400';
              const cpmColor = row.cost_per_merged_pr == null ? 'text-neutral-500'
                : row.cost_per_merged_pr <= 5 ? 'text-emerald-400'
                : row.cost_per_merged_pr <= 15 ? 'text-amber-400'
                : 'text-red-400';
              return (
                <tr key={i}>
                  <td className="px-3 py-2">
                    <span className="font-medium text-neutral-200">{row.account_name}</span>
                    <span className="text-neutral-500 ml-1.5 text-[10px]">{row.provider}</span>
                  </td>
                  <td className="text-right px-3 py-2 text-neutral-300 tabular-nums">{row.total_tasks}</td>
                  <td className={`text-right px-3 py-2 tabular-nums ${srColor}`}>
                    {row.success_rate != null ? `${Math.round(row.success_rate * 100)}%` : '—'}
                  </td>
                  <td className="text-right px-3 py-2 text-neutral-300 font-mono tabular-nums">
                    ${row.total_cost_usd.toFixed(2)}
                  </td>
                  <td className={`text-right px-3 py-2 font-mono tabular-nums ${cpmColor}`}>
                    {row.cost_per_merged_pr != null ? `$${row.cost_per_merged_pr.toFixed(2)}` : '—'}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
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
