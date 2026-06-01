import { useState } from 'react';
import { fetchLlmCostBreakdown, type LlmCostBreakdownResponse } from '../api';
import { usePoller, DEFAULT_POLL_MS } from '../hooks/usePoller';

function fmtTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n);
}

function fmtShare(share: number | null): string {
  return share != null ? `${Math.round(share * 100)}%` : '—';
}

/**
 * Per-model LLM token-spend breakdown for the selected project, over a rolling
 * window (default 7 days). Surfaces which model drives token cost and warns when
 * some provider:model combos are unpriced (and therefore understating cost).
 */
export function LlmCostBreakdownPanel({
  selectedProjectId,
  windowDays = 7,
}: {
  selectedProjectId: string | null;
  windowDays?: number;
}) {
  // Tag loaded data with the project it belongs to so a project switch never
  // briefly renders the previous project's spend before the next load resolves.
  const [state, setState] = useState<{ projectId: string; data: LlmCostBreakdownResponse } | null>(null);

  usePoller(async (signal) => {
    if (!selectedProjectId) return;
    try {
      const res = await fetchLlmCostBreakdown(selectedProjectId, windowDays);
      if (!signal.aborted) setState({ projectId: selectedProjectId, data: res });
    } catch { /* silent */ }
  }, { intervalMs: DEFAULT_POLL_MS, enabled: !!selectedProjectId, deps: [selectedProjectId, windowDays] });

  if (!selectedProjectId || state?.projectId !== selectedProjectId) return null;
  const data = state.data;

  const { by_model, totals, unpriced_combos } = data;
  const coverage = totals.unpriced_coverage_fraction;
  const fullCoverage = coverage == null || coverage >= 1;

  const Header = (
    <h2 className="text-[11px] font-bold text-neutral-400 uppercase tracking-widest mb-2">
      LLM Cost by Model <span className="text-neutral-600 normal-case font-normal">· {data.window_days}d</span>
    </h2>
  );

  if (totals.total_cost_usd <= 0 || by_model.length === 0) {
    return (
      <section>
        {Header}
        <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg px-3 py-4 text-[11px] text-neutral-500">
          No priced LLM token spend in the last {data.window_days} days for this project.
        </div>
      </section>
    );
  }

  const coverageWarning = !fullCoverage && (
    <div className="mb-2 rounded border border-amber-500/30 bg-amber-500/10 px-2.5 py-1.5 text-[10px] text-amber-300">
      <span className="font-semibold">Pricing coverage {fmtShare(coverage)}</span>
      {' — '}{totals.unpriced_task_count} of {totals.priced_task_count + totals.unpriced_task_count} priceable tasks have no pricing entry, so reported cost is understated.
      {unpriced_combos.length > 0 && (
        <div className="mt-1 text-amber-400/80">
          Unpriced: <span className="font-mono">{unpriced_combos.join(', ')}</span>
        </div>
      )}
    </div>
  );

  return (
    <section>
      {Header}
      {coverageWarning}
      <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg overflow-hidden">
        {/* Mobile: stacked card layout */}
        <div className="sm:hidden divide-y divide-neutral-800/30">
          {by_model.map((row, i) => (
            <div key={i} className="px-3 py-2 text-[10px]">
              <div className="flex items-start justify-between gap-2 mb-1">
                <span className="font-medium text-neutral-200 break-words">
                  {row.model}
                  {!row.priced && <span className="ml-1 text-amber-400">(unpriced)</span>}
                </span>
                <span className="text-neutral-500 shrink-0">{row.provider}</span>
              </div>
              <div className="grid grid-cols-2 gap-x-4 gap-y-0.5 text-neutral-500">
                <span>Tokens: <span className="text-neutral-300 tabular-nums">{fmtTokens(row.total_input_tokens + row.total_output_tokens)}</span></span>
                <span>Share: <span className="text-neutral-300 tabular-nums">{fmtShare(row.share_of_total_cost)}</span></span>
                <span>Cost: <span className="text-neutral-300 font-mono tabular-nums">${row.total_cost_usd.toFixed(2)}</span></span>
                <span>$/Task: <span className="text-neutral-300 font-mono tabular-nums">${row.mean_cost_per_task.toFixed(3)}</span></span>
              </div>
            </div>
          ))}
        </div>
        {/* Desktop: table layout */}
        <table className="hidden sm:table w-full text-xs">
          <thead>
            <tr className="text-[10px] text-neutral-500 border-b border-neutral-800/50">
              <th className="text-left px-3 py-2 font-medium">Provider / Model</th>
              <th className="text-right px-3 py-2 font-medium">In / Out Tokens</th>
              <th className="text-right px-3 py-2 font-medium">Cost</th>
              <th className="text-right px-3 py-2 font-medium">$/Task</th>
              <th className="text-right px-3 py-2 font-medium">Share</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-neutral-800/30">
            {by_model.map((row, i) => (
              <tr key={i}>
                <td className="px-3 py-2">
                  <span className="font-medium text-neutral-200">{row.model}</span>
                  <span className="text-neutral-500 ml-1.5 text-[10px]">{row.provider}</span>
                  {!row.priced && <span className="ml-1.5 text-[10px] text-amber-400">unpriced</span>}
                </td>
                <td className="text-right px-3 py-2 text-neutral-300 tabular-nums">
                  {fmtTokens(row.total_input_tokens)} / {fmtTokens(row.total_output_tokens)}
                </td>
                <td className="text-right px-3 py-2 text-neutral-300 font-mono tabular-nums">
                  ${row.total_cost_usd.toFixed(2)}
                </td>
                <td className="text-right px-3 py-2 text-neutral-300 font-mono tabular-nums">
                  ${row.mean_cost_per_task.toFixed(3)}
                </td>
                <td className="text-right px-3 py-2 text-neutral-300 tabular-nums">
                  {fmtShare(row.share_of_total_cost)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
