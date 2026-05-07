import { useState, useEffect } from 'react';
import { apiFetch } from '../api';

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

  return (
    <section>
      <h2 className="text-[11px] font-bold text-neutral-400 uppercase tracking-widest mb-2">Provider Cost &amp; Success</h2>
      <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg overflow-x-auto" data-viewport-allow-overflow>
        <table className="w-full text-xs">
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
