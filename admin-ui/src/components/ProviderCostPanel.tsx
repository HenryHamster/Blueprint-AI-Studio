import { useState } from 'react';
import { apiFetch } from '../api';
import { usePoller, DEFAULT_POLL_MS } from '../hooks/usePoller';
import { useEfficiencySnapshot, fmtEffSecs, fmtEffRate, fmtEffIter } from '../hooks/useEfficiencySnapshot';
import { WarmStartOriginRow } from './WarmStartOriginRow';

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

function spendPressureForRow(row: ProviderCostRow): { label: string; cls: string; severity: number } {
  if (row.total_cost_usd > 0 && row.merged_count === 0) {
    return { label: 'No merges for spend', cls: 'text-red-300', severity: 3 };
  }
  if (row.cost_per_merged_pr != null && row.cost_per_merged_pr > 15) {
    return { label: 'High $/merge', cls: 'text-red-300', severity: 3 };
  }
  if (row.success_rate != null && row.success_rate < 0.5) {
    return { label: 'Low success', cls: 'text-amber-300', severity: 2 };
  }
  if (row.cost_per_merged_pr != null && row.cost_per_merged_pr > 5) {
    return { label: 'Watch cost', cls: 'text-amber-300', severity: 1 };
  }
  return { label: 'Efficient', cls: 'text-emerald-300', severity: 0 };
}

function spendPressureBannerClasses(severity: number): string {
  if (severity >= 3) return 'border-red-500/20 bg-red-500/10 text-red-200';
  if (severity >= 1) return 'border-amber-500/20 bg-amber-500/10 text-amber-200';
  return 'border-emerald-500/20 bg-emerald-500/10 text-emerald-200';
}

export function ProviderCostPanel({
  selectedProjectId,
  windowHours,
}: {
  selectedProjectId: string | null;
  windowHours: number;
}) {
  const [data, setData] = useState<ProviderCostSummary | null>(null);
  const [pollError, setPollError] = useState(false);
  const efficiency = useEfficiencySnapshot(selectedProjectId);

  usePoller(async (signal) => {
    try {
      const params = new URLSearchParams({ hours: String(windowHours) });
      if (selectedProjectId) params.set('project_id', selectedProjectId);
      const res = await apiFetch(`/api/admin/provider-cost-summary?${params}`);
      if (!signal.aborted) {
        if (res.ok) {
          setData(await res.json());
          setPollError(false);
        } else {
          setPollError(true);
        }
      }
    } catch {
      if (!signal.aborted) setPollError(true);
    }
  }, { intervalMs: DEFAULT_POLL_MS, deps: [selectedProjectId, windowHours] });

  if (!data || data.rows.length === 0) {
    if (pollError) {
      return (
        <section>
          <h2 className="text-ui-md font-bold text-neutral-400 uppercase tracking-widest mb-2">Provider Cost &amp; Success</h2>
          <div className="text-xs text-red-400 px-1">Cost data unavailable — fetch failed.</div>
        </section>
      );
    }
    return null;
  }

  const w24 = efficiency?.window_24h;
  const w7 = efficiency?.window_7d;
  const totalTasks = data.rows.reduce((sum, row) => sum + row.total_tasks, 0);
  const successCount = data.rows.reduce((sum, row) => sum + row.success_count, 0);
  const mergedCount = data.rows.reduce((sum, row) => sum + row.merged_count, 0);
  const totalSpend = data.rows.reduce((sum, row) => sum + row.total_cost_usd, 0);
  const aggregateSuccessRate = totalTasks > 0 ? successCount / totalTasks : null;
  const aggregateCostPerMerged = mergedCount > 0 ? totalSpend / mergedCount : null;
  const rowPressures = data.rows.map(row => ({ row, pressure: spendPressureForRow(row) }));
  const worstPressure = rowPressures.reduce((worst, current) => (
    current.pressure.severity > worst.pressure.severity ? current : worst
  ));
  const spendAction = worstPressure.pressure.severity >= 3
    ? `Reroute or inspect ${worstPressure.row.account_name}: ${worstPressure.pressure.label.toLowerCase()}.`
    : worstPressure.pressure.severity >= 1
      ? `Watch ${worstPressure.row.account_name}: ${worstPressure.pressure.label.toLowerCase()}.`
      : 'Spend is within the current success envelope.';

  return (
    <section>
      <h2 className="text-ui-md font-bold text-neutral-400 uppercase tracking-widest mb-2">Provider Cost &amp; Success</h2>
      {pollError && (
        <div className="mb-2 rounded border border-amber-500/30 bg-amber-500/10 px-2.5 py-1.5 text-ui-sm text-amber-300">
          Data may be stale — last poll failed.
        </div>
      )}
      {efficiency && (
        <div className="mb-2 flex flex-wrap gap-x-4 gap-y-1 text-ui-sm text-neutral-400 px-1">
          <span className="text-neutral-500 font-bold uppercase tracking-widest w-full">Pipeline Efficiency</span>
          <EffMetric label="cold-start p50" value={fmtEffSecs(w24?.setup_p50_secs)} delta={w24?.vs_prior_setup_p50_delta} lowerIsBetter />
          <EffMetric label="reuse rate" value={fmtEffRate(w24?.warm_reuse_rate)} delta={w24?.vs_prior_warm_reuse_rate_delta} lowerIsBetter={false} />
          <WarmStartOriginRow breakdown={w24?.warm_start_origin_breakdown} />
          <EffMetric label="review cycles" value={fmtEffIter(w7?.review_iter_median)} delta={w7?.vs_prior_review_iter_median_delta} lowerIsBetter />
        </div>
      )}
      <div
        data-testid="provider-cost-summary"
        className="mb-2 grid grid-cols-2 overflow-hidden rounded-md border border-neutral-800/60 bg-workbench-shell/30 sm:grid-cols-4"
      >
        <div className="border-r border-b border-neutral-800/60 px-2.5 py-2 sm:border-b-0">
          <div className="text-ui-sm uppercase tracking-wide text-neutral-500">Window</div>
          <div className="mt-0.5 font-mono text-xs font-semibold text-neutral-200">{data.window_hours}h</div>
          <div className="mt-0.5 truncate text-ui-sm text-neutral-600">{selectedProjectId ? 'project' : 'fleet'} scope</div>
        </div>
        <div className="border-r-0 border-b border-neutral-800/60 px-2.5 py-2 sm:border-r sm:border-b-0">
          <div className="text-ui-sm uppercase tracking-wide text-neutral-500">Tasks</div>
          <div className="mt-0.5 font-mono text-xs font-semibold text-neutral-200">{totalTasks}</div>
          <div className="mt-0.5 truncate text-ui-sm text-neutral-600">{mergedCount} merged</div>
        </div>
        <div className="border-r border-neutral-800/60 px-2.5 py-2">
          <div className="text-ui-sm uppercase tracking-wide text-neutral-500">Success</div>
          <div className="mt-0.5 font-mono text-xs font-semibold text-emerald-300">
            {aggregateSuccessRate == null ? '—' : `${Math.round(aggregateSuccessRate * 100)}%`}
          </div>
          <div className="mt-0.5 truncate text-ui-sm text-neutral-600">{successCount} succeeded</div>
        </div>
        <div className="px-2.5 py-2">
          <div className="text-ui-sm uppercase tracking-wide text-neutral-500">Spend</div>
          <div className="mt-0.5 font-mono text-xs font-semibold text-neutral-200">${totalSpend.toFixed(2)}</div>
          <div className="mt-0.5 truncate text-ui-sm text-neutral-600">
            {aggregateCostPerMerged == null ? 'no merges' : `$${aggregateCostPerMerged.toFixed(2)}/merge`}
          </div>
        </div>
      </div>
      <div
        className={`mb-2 rounded-md border px-2.5 py-1.5 text-ui-sm ${spendPressureBannerClasses(worstPressure.pressure.severity)}`}
        data-testid="provider-cost-spend-pressure"
      >
        <span className="font-semibold">Spend pressure:</span> {spendAction}
      </div>
      <div className="bg-workbench-base/50 border border-neutral-800/50 rounded-lg overflow-hidden">
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
            const pressure = spendPressureForRow(row);
            return (
              <div key={i} className="px-3 py-2 text-ui-sm">
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
                <div className={`mt-1 ${pressure.cls}`}>Next: {pressure.label}</div>
              </div>
            );
          })}
        </div>
        {/* Desktop: table layout */}
        <table className="hidden sm:table w-full text-xs">
          <thead>
            <tr className="text-ui-sm text-neutral-500 border-b border-neutral-800/50">
              <th className="text-left px-3 py-2 font-medium">Provider / Account</th>
              <th className="text-right px-3 py-2 font-medium">Tasks</th>
              <th className="text-right px-3 py-2 font-medium">Success</th>
              <th className="text-right px-3 py-2 font-medium">Spend</th>
              <th className="text-right px-3 py-2 font-medium">$/Merge</th>
              <th className="text-right px-3 py-2 font-medium">Action</th>
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
              const pressure = spendPressureForRow(row);
              return (
                <tr key={i}>
                  <td className="px-3 py-2">
                    <span className="font-medium text-neutral-200">{row.account_name}</span>
                    <span className="text-neutral-500 ml-1.5 text-ui-sm">{row.provider}</span>
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
                  <td className={`text-right px-3 py-2 ${pressure.cls}`}>
                    {pressure.label}
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
