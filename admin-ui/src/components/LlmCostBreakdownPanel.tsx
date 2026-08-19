import { useState } from 'react';
import { fetchLlmCostBreakdown, type LlmCostBreakdownResponse, type LlmCostTaskTypeRow, type ReworkPrEntry } from '../api';
import { usePoller, DEFAULT_POLL_MS } from '../hooks/usePoller';

function fmtTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n);
}

function fmtShare(share: number | null): string {
  return share != null ? `${Math.round(share * 100)}%` : '—';
}

function fmtMergeRate(rate: number | null): string {
  return rate != null ? `${Math.round(rate * 100)}%` : '—';
}

// Cost-per-merge is null for unpriced (understated cost) or zero-merge buckets;
// render an em-dash there rather than a confident-but-misleading dollar figure.
function fmtCostPerMerge(cost: number | null): string {
  return cost != null ? `$${cost.toFixed(2)}` : '—';
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
  // Track which project's last poll failed so we can show a stale/error affordance.
  const [pollErrorProjectId, setPollErrorProjectId] = useState<string | null>(null);

  usePoller(async (signal) => {
    if (!selectedProjectId) return;
    try {
      const res = await fetchLlmCostBreakdown(selectedProjectId, windowDays);
      if (!signal.aborted) {
        setState({ projectId: selectedProjectId, data: res });
        setPollErrorProjectId(null);
      }
    } catch {
      if (!signal.aborted) setPollErrorProjectId(selectedProjectId);
    }
  }, { intervalMs: DEFAULT_POLL_MS, enabled: !!selectedProjectId, deps: [selectedProjectId, windowDays] });

  const pollError = pollErrorProjectId === selectedProjectId;

  if (!selectedProjectId) return null;

  if (state?.projectId !== selectedProjectId) {
    if (pollError) {
      return (
        <section>
          <h2 className="text-ui-md font-bold text-neutral-400 uppercase tracking-widest mb-2">
            LLM Cost by Model
          </h2>
          <div className="text-xs text-red-400 px-1">Cost data unavailable — fetch failed.</div>
        </section>
      );
    }
    return null;
  }

  const data = state.data;

  const { by_model, by_task_type, totals, unpriced_combos, approximate_priced_combos } = data;
  const coverage = totals.unpriced_coverage_fraction;
  const fullCoverage = coverage == null || coverage >= 1;

  const Header = (
    <h2 className="text-ui-md font-bold text-neutral-400 uppercase tracking-widest mb-2">
      LLM Cost by Model <span className="text-neutral-600 normal-case font-normal">· {data.window_days}d</span>
    </h2>
  );

  const StaleBanner = pollError && (
    <div className="mb-2 rounded border border-amber-500/30 bg-amber-500/10 px-2.5 py-1.5 text-ui-sm text-amber-300">
      Data may be stale — last poll failed.
    </div>
  );

  if (by_model.length === 0) {
    return (
      <section>
        {Header}
        {StaleBanner}
        <div className="bg-workbench-base/50 border border-neutral-800/50 rounded-lg px-3 py-4 text-ui-md text-neutral-500">
          No priced LLM token spend in the last {data.window_days} days for this project.
        </div>
      </section>
    );
  }

  const allUnpriced = totals.total_cost_usd <= 0 && totals.unpriced_task_count > 0;
  const coverageWarning = (allUnpriced || !fullCoverage) && (
    <div className="mb-2 rounded border border-amber-500/30 bg-amber-500/10 px-2.5 py-1.5 text-ui-sm text-amber-300">
      <span className="font-semibold">
        {allUnpriced ? 'Cost data unavailable' : `Pricing coverage ${fmtShare(coverage)}`}
      </span>
      {' — '}
      {allUnpriced
        ? `All ${totals.unpriced_task_count} token-bearing tasks have no pricing entry, so actual cost is unknown.`
        : `${totals.unpriced_task_count} of ${totals.priced_task_count + totals.unpriced_task_count} priceable tasks have no pricing entry, so reported cost is understated.`
      }
      {unpriced_combos.length > 0 && (
        <div className="mt-1 text-amber-400/80">
          Unpriced: <span className="font-mono">{unpriced_combos.join(', ')}</span>
        </div>
      )}
    </div>
  );

  const fallbackWarning = totals.fallback_task_count > 0 && (
    <div className="mb-2 rounded border border-amber-500/30 bg-amber-500/10 px-2.5 py-1.5 text-ui-sm text-amber-300">
      <span className="font-semibold">
        {totals.fallback_task_count} fallback {totals.fallback_task_count === 1 ? 'task' : 'tasks'}
      </span>
      {' — '}ran on a different provider/model than the intended routing (${totals.fallback_cost_usd.toFixed(2)} of spend), so by-model attribution folds these into the resolved model rather than the configured one.
    </div>
  );

  const approximateWarning = approximate_priced_combos.length > 0 && (
    <div className="mb-2 rounded border border-blue-500/30 bg-blue-500/10 px-2.5 py-1.5 text-ui-sm text-blue-300">
      <span className="font-semibold">Approximate pricing</span>
      {' — '}some combos are priced at a proxy rate (closest GA-tier equivalent) as no separately published API rate exists.
      <div className="mt-1 text-blue-400/80">
        <span className="font-mono">{approximate_priced_combos.join(', ')}</span>
      </div>
    </div>
  );

  const { rework_attribution } = data;
  const ReworkSection = rework_attribution && (
    <section className="mt-4">
      <h2 className="text-ui-md font-bold text-neutral-400 uppercase tracking-widest mb-2">
        LLM Cost Rework Attribution <span className="text-neutral-600 normal-case font-normal">· {data.window_days}d</span>
      </h2>
      <div className="bg-workbench-base/50 border border-neutral-800/50 rounded-lg px-3 py-3">
        <div className="flex flex-wrap gap-x-6 gap-y-1 text-ui-md mb-3">
          <span className="text-neutral-400">
            Rework share:{' '}
            <span className="text-neutral-200 font-semibold tabular-nums">
              {rework_attribution.rework_share != null
                ? `${Math.round(rework_attribution.rework_share * 100)}%`
                : '—'}
            </span>
          </span>
          <span className="text-neutral-400">
            First-pass:{' '}
            <span className="text-neutral-200 font-mono tabular-nums">
              ${rework_attribution.first_pass_llm_cost_usd.toFixed(2)}
            </span>
          </span>
          <span className="text-neutral-400">
            Rework:{' '}
            <span className="text-neutral-200 font-mono tabular-nums">
              ${rework_attribution.rework_llm_cost_usd.toFixed(2)}
            </span>
          </span>
        </div>
        {rework_attribution.top_rework_prs.length > 0 && (
          <>
            <div className="text-ui-sm text-neutral-500 mb-1 uppercase tracking-wider">Top rework PRs</div>
            <div className="divide-y divide-neutral-800/30">
              {rework_attribution.top_rework_prs.map((pr: ReworkPrEntry) => (
                <div key={pr.pr_number} className="flex items-center justify-between py-1 text-ui-md">
                  <span className="text-neutral-400 font-mono">#{pr.pr_number}</span>
                  <div className="flex gap-x-4 text-neutral-500">
                    <span>LLM: <span className="text-neutral-300 font-mono tabular-nums">${pr.rework_llm_cost_usd.toFixed(2)}</span></span>
                    <span>VM: <span className="text-neutral-300 font-mono tabular-nums">${pr.rework_vm_cost_usd.toFixed(2)}</span></span>
                  </div>
                </div>
              ))}
            </div>
          </>
        )}
      </div>
    </section>
  );

  const TaskTypeSection = by_task_type.length > 0 && (
    <section className="mt-4">
      <h2 className="text-ui-md font-bold text-neutral-400 uppercase tracking-widest mb-2">
        LLM Cost by Task Type <span className="text-neutral-600 normal-case font-normal">· {data.window_days}d</span>
      </h2>
      <div className="bg-workbench-base/50 border border-neutral-800/50 rounded-lg overflow-hidden">
        {/* Mobile: stacked card layout */}
        <div className="sm:hidden divide-y divide-neutral-800/30">
          {by_task_type.map((row: LlmCostTaskTypeRow, i: number) => (
            <div key={i} className="px-3 py-2 text-ui-sm">
              <div className="flex items-start justify-between gap-2 mb-1">
                <span className="font-medium text-neutral-200 break-words font-mono">{row.task_type}</span>
                <span className="text-neutral-500 shrink-0 tabular-nums">{fmtShare(row.share_of_total_cost)}</span>
              </div>
              <div className="grid grid-cols-2 gap-x-4 gap-y-0.5 text-neutral-500">
                <span>Tokens: <span className="text-neutral-300 tabular-nums">{fmtTokens(row.total_input_tokens + row.total_output_tokens)}</span></span>
                <span>Cost: <span className="text-neutral-300 font-mono tabular-nums">${row.total_cost_usd.toFixed(2)}</span></span>
                <span>Tasks: <span className="text-neutral-300 tabular-nums">{row.task_count}</span></span>
                <span>Merged: <span className="text-neutral-300 tabular-nums">{row.merged_count}/{row.task_count} ({fmtMergeRate(row.merge_rate)})</span></span>
                <span>$/Verified Merge: <span className="text-neutral-300 font-mono tabular-nums">{fmtCostPerMerge(row.cost_per_merge)}</span></span>
              </div>
            </div>
          ))}
        </div>
        {/* Desktop: table layout */}
        <table className="hidden sm:table w-full text-xs">
          <thead>
            <tr className="text-ui-sm text-neutral-500 border-b border-neutral-800/50">
              <th className="text-left px-3 py-2 font-medium">Task Type</th>
              <th className="text-right px-3 py-2 font-medium">In / Out Tokens</th>
              <th className="text-right px-3 py-2 font-medium">Cost</th>
              <th className="text-right px-3 py-2 font-medium">Tasks</th>
              <th className="text-right px-3 py-2 font-medium">Merged</th>
              <th className="text-right px-3 py-2 font-medium">Verified</th>
              <th className="text-right px-3 py-2 font-medium">$/Verified Merge</th>
              <th className="text-right px-3 py-2 font-medium">Share</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-neutral-800/30">
            {by_task_type.map((row: LlmCostTaskTypeRow, i: number) => (
              <tr key={i}>
                <td className="px-3 py-2 font-mono text-neutral-200">{row.task_type}</td>
                <td className="text-right px-3 py-2 text-neutral-300 tabular-nums">
                  {fmtTokens(row.total_input_tokens)} / {fmtTokens(row.total_output_tokens)}
                </td>
                <td className="text-right px-3 py-2 text-neutral-300 font-mono tabular-nums">
                  ${row.total_cost_usd.toFixed(2)}
                </td>
                <td className="text-right px-3 py-2 text-neutral-300 tabular-nums">
                  {row.task_count}
                </td>
                <td className="text-right px-3 py-2 text-neutral-300 tabular-nums">
                  {row.merged_count}/{row.task_count} ({fmtMergeRate(row.merge_rate)})
                </td>
                <td className="text-right px-3 py-2 text-neutral-300 tabular-nums">
                  {row.verified_merged_count}/{row.task_count} ({fmtMergeRate(row.verified_merge_rate)})
                </td>
                <td className="text-right px-3 py-2 text-neutral-300 font-mono tabular-nums">
                  {fmtCostPerMerge(row.cost_per_merge)}
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

  return (
    <section>
      {Header}
      {StaleBanner}
      {coverageWarning}
      {fallbackWarning}
      {approximateWarning}
      <div className="bg-workbench-base/50 border border-neutral-800/50 rounded-lg overflow-hidden">
        {/* Mobile: stacked card layout */}
        <div className="sm:hidden divide-y divide-neutral-800/30">
          {by_model.map((row, i) => (
            <div key={i} className="px-3 py-2 text-ui-sm">
              <div className="flex items-start justify-between gap-2 mb-1">
                <span className="font-medium text-neutral-200 break-words">
                  {row.model}
                  {!row.priced && <span className="ml-1 text-amber-400">(unpriced)</span>}
                  {row.approximate_priced && <span className="ml-1 text-blue-400">(approx)</span>}
                </span>
                <span className="text-neutral-500 shrink-0">{row.provider}</span>
              </div>
              <div className="grid grid-cols-2 gap-x-4 gap-y-0.5 text-neutral-500">
                <span>Tokens: <span className="text-neutral-300 tabular-nums">{fmtTokens(row.total_input_tokens + row.total_output_tokens)}</span></span>
                <span>Share: <span className="text-neutral-300 tabular-nums">{fmtShare(row.share_of_total_cost)}</span></span>
                <span>Cost: <span className="text-neutral-300 font-mono tabular-nums">${row.total_cost_usd.toFixed(2)}</span></span>
                <span>$/Task: <span className="text-neutral-300 font-mono tabular-nums">${row.mean_cost_per_task.toFixed(3)}</span></span>
                <span>Merged: <span className="text-neutral-300 tabular-nums">{row.merged_count}/{row.task_count} ({fmtMergeRate(row.merge_rate)})</span></span>
                <span>Verified: <span className="text-neutral-300 tabular-nums">{row.verified_merged_count}/{row.task_count} ({fmtMergeRate(row.verified_merge_rate)})</span></span>
                <span>$/Verified Merge: <span className="text-neutral-300 font-mono tabular-nums">{fmtCostPerMerge(row.cost_per_merge)}</span></span>
              </div>
            </div>
          ))}
        </div>
        {/* Desktop: table layout */}
        <table className="hidden sm:table w-full text-xs">
          <thead>
            <tr className="text-ui-sm text-neutral-500 border-b border-neutral-800/50">
              <th className="text-left px-3 py-2 font-medium">Provider / Model</th>
              <th className="text-right px-3 py-2 font-medium">In / Out Tokens</th>
              <th className="text-right px-3 py-2 font-medium">Cost</th>
              <th className="text-right px-3 py-2 font-medium">$/Task</th>
              <th className="text-right px-3 py-2 font-medium">Merged</th>
              <th className="text-right px-3 py-2 font-medium">Verified</th>
              <th className="text-right px-3 py-2 font-medium">$/Verified Merge</th>
              <th className="text-right px-3 py-2 font-medium">Share</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-neutral-800/30">
            {by_model.map((row, i) => (
              <tr key={i}>
                <td className="px-3 py-2">
                  <span className="font-medium text-neutral-200">{row.model}</span>
                  <span className="text-neutral-500 ml-1.5 text-ui-sm">{row.provider}</span>
                  {!row.priced && <span className="ml-1.5 text-ui-sm text-amber-400">unpriced</span>}
                  {row.approximate_priced && <span className="ml-1.5 text-ui-sm text-blue-400">approx</span>}
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
                  {row.merged_count}/{row.task_count} ({fmtMergeRate(row.merge_rate)})
                </td>
                <td className="text-right px-3 py-2 text-neutral-300 tabular-nums">
                  {row.verified_merged_count}/{row.task_count} ({fmtMergeRate(row.verified_merge_rate)})
                </td>
                <td className="text-right px-3 py-2 text-neutral-300 font-mono tabular-nums">
                  {fmtCostPerMerge(row.cost_per_merge)}
                </td>
                <td className="text-right px-3 py-2 text-neutral-300 tabular-nums">
                  {fmtShare(row.share_of_total_cost)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {TaskTypeSection}
      {ReworkSection}
    </section>
  );
}
