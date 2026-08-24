/* eslint-disable react-hooks/set-state-in-effect */
import { useState, useEffect, useCallback } from 'react';
import {
  apiFetch,
  fetchLaneBaselines,
  type LaneDiagResponse,
  type LaneWasteTaxonomy,
  type LaneBaselineTrend,
  type LaneBaselineTrendBlock,
  type PerPrPhaseRow,
} from '../api';

const WASTE_CATEGORIES: { key: keyof LaneWasteTaxonomy; label: string; color: string }[] = [
  { key: 'infra', label: 'Infra', color: 'bg-red-500' },
  { key: 'merge_conflict', label: 'Merge Conflict', color: 'bg-orange-500' },
  { key: 'ci_churn', label: 'CI Churn', color: 'bg-yellow-500' },
  { key: 'review_ambiguity', label: 'Review Ambiguity', color: 'bg-blue-500' },
  { key: 'lost_reasoning', label: 'Lost Reasoning', color: 'bg-purple-500' },
  { key: 'unknown', label: 'Unknown', color: 'bg-workbench-control' },
];

function fmt(s: number): string {
  if (s >= 3600) return `${(s / 3600).toFixed(1)}h`;
  if (s >= 60) return `${Math.round(s / 60)}m`;
  return `${Math.round(s)}s`;
}

function classificationBadge(c: LaneBaselineTrendBlock['classification']): string {
  if (c === 'improving') return 'text-green-400';
  if (c === 'declining') return 'text-red-400';
  if (c === 'stable') return 'text-neutral-400';
  return 'text-neutral-600';
}

function classificationLabel(c: LaneBaselineTrendBlock['classification']): string {
  if (c === 'improving') return '↓ improving';
  if (c === 'declining') return '↑ declining';
  if (c === 'stable') return '→ stable';
  return '—';
}

function WasteTaxonomyTable({ taxonomy }: { taxonomy: LaneWasteTaxonomy }) {
  const total = Object.values(taxonomy).reduce((a, b) => a + b, 0);
  if (total === 0) return <p className="text-xs text-neutral-500">No waste data available.</p>;

  return (
    <div className="space-y-1">
      {WASTE_CATEGORIES.map(({ key, label, color }) => {
        const count = taxonomy[key] ?? 0;
        const pct = total > 0 ? (count / total) * 100 : 0;
        return (
          <div key={key} className="flex items-center gap-2 text-xs">
            <span className="w-32 text-neutral-300 shrink-0">{label}</span>
            <div className="flex-1 bg-workbench-panel rounded h-2 overflow-hidden">
              <div className={`${color} h-full rounded`} style={{ width: `${pct}%` }} />
            </div>
            <span className="w-8 text-right text-neutral-400">{count}</span>
            <span className="w-10 text-right text-neutral-500">{pct.toFixed(0)}%</span>
          </div>
        );
      })}
      <p className="text-xs text-neutral-500 pt-1">Total waste tasks: {total}</p>
    </div>
  );
}

function RetargetConvergenceTable({
  laneWaste,
}: {
  laneWaste: LaneDiagResponse['lane_waste'];
}) {
  const affected = laneWaste.rollup_supersede_affected_reviews_to_converge
    ?? laneWaste.supersede_affected_reviews_to_converge;
  const unaffected = laneWaste.rollup_supersede_unaffected_reviews_to_converge;
  const capDirections = laneWaste.cap_exhausted_directions ?? [];
  return (
    <table className="w-full text-xs">
      <thead>
        <tr className="text-neutral-500 border-b border-neutral-800">
          <th className="text-left py-1 font-normal">Metric</th>
          <th className="text-right py-1 font-normal">Count</th>
        </tr>
      </thead>
      <tbody>
        <tr className="border-b border-neutral-800/50">
          <td className="py-1 text-neutral-300">Reviews to converge</td>
          <td className="py-1 text-right text-neutral-200">
            {laneWaste.reviews_to_converge?.count ?? 0}{' '}
            <span className="text-neutral-500">
              (median {laneWaste.reviews_to_converge?.median ?? '—'}, P95 {laneWaste.reviews_to_converge?.p95 ?? '—'})
            </span>
          </td>
        </tr>
        <tr className="border-b border-neutral-800/50">
          <td className="py-1 text-neutral-300">Superseded-rollup reviews to converge</td>
          <td className="py-1 text-right text-neutral-200">
            {affected?.median ?? '—'}{' '}
            <span className="text-neutral-500">
              (median of {affected?.count ?? 0} merged of{' '}
              {laneWaste.rollup_supersede_affected_chain_count
                ?? laneWaste.supersede_affected_chain_count
                ?? 0} superseded rollup chains, P95 {affected?.p95 ?? '—'})
            </span>
          </td>
        </tr>
        <tr className="border-b border-neutral-800/50">
          <td className="py-1 text-neutral-300">Rollup control reviews to converge</td>
          <td className="py-1 text-right text-neutral-200">
            {unaffected?.median ?? '—'}{' '}
            <span className="text-neutral-500">
              (median of {unaffected?.count ?? 0} merged of{' '}
              {laneWaste.rollup_supersede_unaffected_chain_count ?? 0} unaffected rollup chains, P95{' '}
              {unaffected?.p95 ?? '—'})
            </span>
          </td>
        </tr>
        <tr className="border-b border-neutral-800/50">
          <td className="py-1 text-neutral-300">Directions lost to the review cap</td>
          <td className="py-1 text-right text-neutral-200">
            {laneWaste.cap_exhausted_direction_count ?? 0}{' '}
            <span className="text-neutral-500">
              ({laneWaste.cap_exhausted_chain_count ?? 0} chains)
            </span>
          </td>
        </tr>
        {capDirections.length > 0 && (
          <tr className="border-b border-neutral-800/50">
            <td className="py-1 text-neutral-300">Cap-loss directions</td>
            <td className="py-1 text-right text-neutral-500">
              {capDirections.map((direction) => direction.direction_text || direction.direction_id).join(', ')}
            </td>
          </tr>
        )}
        <tr className="border-b border-neutral-800/50">
          <td className="py-1 text-neutral-300">Delta-scope retargets</td>
          <td className="py-1 text-right text-neutral-200">{laneWaste.retarget_review_scope_counts?.delta ?? 0}</td>
        </tr>
        <tr className="border-b border-neutral-800/50">
          <td className="py-1 text-neutral-300">Full-scope retargets</td>
          <td className="py-1 text-right text-neutral-200">{laneWaste.retarget_review_scope_counts?.full ?? 0}</td>
        </tr>
        <tr className="border-b border-neutral-800/50">
          <td className="py-1 text-neutral-300">Stale-head discards</td>
          <td className="py-1 text-right text-neutral-200">{laneWaste.stale_head_discard_count ?? 0}</td>
        </tr>
      </tbody>
    </table>
  );
}

function CheckpointRecoveryWasteTable({
  crw,
}: {
  crw: NonNullable<LaneDiagResponse['checkpoint_recovery_waste']>;
}) {
  if (crw.total === 0) {
    return <p className="text-xs text-neutral-500">No checkpoint-recovery waste recorded.</p>;
  }

  const entries = Object.entries(crw.by_reason).filter(([, v]) => v > 0);
  return (
    <div className="space-y-1">
      <table className="w-full text-xs">
        <thead>
          <tr className="text-neutral-500 border-b border-neutral-800">
            <th className="text-left py-1 font-normal">Recovery Reason</th>
            <th className="text-right py-1 font-normal">Count</th>
            <th className="text-right py-1 font-normal">Share</th>
          </tr>
        </thead>
        <tbody>
          {entries.sort(([, a], [, b]) => b - a).map(([reason, count]) => {
            const pct = crw.total > 0 ? ((count / crw.total) * 100).toFixed(0) : '0';
            return (
              <tr key={reason} className="border-b border-neutral-800/50">
                <td className="py-1 text-neutral-300 font-mono">{reason}</td>
                <td className="py-1 text-right text-neutral-200">{count}</td>
                <td className="py-1 text-right text-neutral-500">{pct}%</td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <p className="text-xs text-neutral-500 pt-1">Total recovery tasks: {crw.total}</p>
    </div>
  );
}

function RelayCountsTable({ counts }: { counts: Record<string, number> }) {
  const entries = Object.entries(counts).filter(([, v]) => v > 0);
  if (entries.length === 0) return <p className="text-xs text-neutral-500">No relay data available.</p>;

  return (
    <table className="w-full text-xs">
      <thead>
        <tr className="text-neutral-500 border-b border-neutral-800">
          <th className="text-left py-1 font-normal">Lane</th>
          <th className="text-right py-1 font-normal">Fix Relays</th>
        </tr>
      </thead>
      <tbody>
        {entries.sort(([, a], [, b]) => b - a).map(([lane, count]) => (
          <tr key={lane} className="border-b border-neutral-800/50">
            <td className="py-1 text-neutral-300 font-mono">{lane}</td>
            <td className="py-1 text-right text-neutral-200">{count}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function PhaseBreakdownTable({ rows, truncated }: { rows: PerPrPhaseRow[]; truncated: boolean }) {
  if (rows.length === 0) return <p className="text-xs text-neutral-500">No phase breakdown data available.</p>;

  return (
    <div className="overflow-x-auto" data-viewport-allow-overflow data-viewport-allow-truncation>
      {/* eslint-disable-next-line viewport/no-fixed-width */}
      <table className="w-full text-xs min-w-[640px]">
        <thead>
          <tr className="text-neutral-500 border-b border-neutral-800">
            <th className="text-left py-1 font-normal">PR</th>
            <th className="text-right py-1 font-normal">Tasks</th>
            <th className="text-right py-1 font-normal">Review</th>
            <th className="text-right py-1 font-normal">Fix</th>
            <th className="text-right py-1 font-normal">Queue</th>
            <th className="text-right py-1 font-normal">Provision</th>
            <th className="text-right py-1 font-normal">Gap</th>
            <th className="text-right py-1 font-normal">Total</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.pr_number} className="border-b border-neutral-800/50 hover:bg-workbench-hover/30">
              <td className="py-1 text-neutral-300 font-mono">#{row.pr_number}</td>
              <td className="py-1 text-right text-neutral-400">{row.task_count}</td>
              <td className="py-1 text-right text-neutral-300">{fmt(row.review_seconds)}</td>
              <td className="py-1 text-right text-neutral-300">{fmt(row.fix_seconds)}</td>
              <td className="py-1 text-right text-neutral-400">{fmt(row.queue_seconds)}</td>
              <td className="py-1 text-right text-neutral-400">{fmt(row.provision_seconds)}</td>
              <td className="py-1 text-right text-neutral-400">{fmt(row.gap_seconds)}</td>
              <td className="py-1 text-right text-neutral-200 font-medium">{fmt(row.total_seconds)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {truncated && (
        <p className="text-xs text-neutral-500 pt-1">Showing top {rows.length} PRs by total time.</p>
      )}
    </div>
  );
}

function TrendRow({
  label,
  block,
  fmt: fmtVal,
}: {
  label: string;
  // Optional: a backend payload missing a trend block must render as "—"
  // rather than throwing and blanking the whole panel.
  block: LaneBaselineTrendBlock | undefined;
  fmt?: (v: number) => string;
}) {
  const display = (v: number | null | undefined) =>
    v == null ? '—' : fmtVal ? fmtVal(v) : v.toFixed(1);

  return (
    <tr className="border-b border-neutral-800/50">
      <td className="py-1 text-neutral-300">{label}</td>
      <td className="py-1 text-right text-neutral-400">{display(block?.prior)}</td>
      <td className="py-1 text-right text-neutral-200">{display(block?.latest)}</td>
      <td className={`py-1 text-right font-medium ${classificationBadge(block?.classification ?? null)}`}>
        {classificationLabel(block?.classification ?? null)}
      </td>
    </tr>
  );
}

function CrossVersionTrendPanel({ trend }: { trend: LaneBaselineTrend }) {
  return (
    <div className="space-y-4" data-testid="cross-version-trend">
      <p className="text-xs text-neutral-500">
        v{trend.prior_version} → v{trend.latest_version}
      </p>

      <div>
        <p className="text-xs font-semibold text-neutral-400 uppercase tracking-wide mb-1">
          Relay Medians
        </p>
        <table className="w-full text-xs">
          <thead>
            <tr className="text-neutral-500 border-b border-neutral-800">
              <th className="text-left py-1 font-normal">Metric</th>
              <th className="text-right py-1 font-normal">Prior</th>
              <th className="text-right py-1 font-normal">Latest</th>
              <th className="text-right py-1 font-normal">Trend</th>
            </tr>
          </thead>
          <tbody>
            <TrendRow label="Wall clock" block={trend.relay_medians.wall_clock_seconds} fmt={fmt} />
            <TrendRow label="Active" block={trend.relay_medians.active_seconds} fmt={fmt} />
            <TrendRow label="Gap" block={trend.relay_medians.gap_seconds} fmt={fmt} />
            <TrendRow label="Tasks/PR" block={trend.relay_medians.task_count} />
          </tbody>
        </table>
      </div>

      <div>
        <p className="text-xs font-semibold text-neutral-400 uppercase tracking-wide mb-1">
          Lane Iterations
        </p>
        <table className="w-full text-xs">
          <thead>
            <tr className="text-neutral-500 border-b border-neutral-800">
              <th className="text-left py-1 font-normal">Metric</th>
              <th className="text-right py-1 font-normal">Prior</th>
              <th className="text-right py-1 font-normal">Latest</th>
              <th className="text-right py-1 font-normal">Trend</th>
            </tr>
          </thead>
          <tbody>
            <TrendRow label="Conflict p50" block={trend.lane_iterations.conflict.p50} />
            <TrendRow label="Conflict p90" block={trend.lane_iterations.conflict.p90} />
            <TrendRow label="CI p50" block={trend.lane_iterations.ci.p50} />
            <TrendRow label="CI p90" block={trend.lane_iterations.ci.p90} />
            <TrendRow
              label="Review convergence p50"
              block={trend.lane_iterations.review_convergence.p50}
            />
            <TrendRow
              label="Review convergence p90"
              block={trend.lane_iterations.review_convergence.p90}
            />
          </tbody>
        </table>
      </div>

      <div>
        <p className="text-xs font-semibold text-neutral-400 uppercase tracking-wide mb-1">
          Waste Taxonomy Shares
        </p>
        <table className="w-full text-xs">
          <thead>
            <tr className="text-neutral-500 border-b border-neutral-800">
              <th className="text-left py-1 font-normal">Category</th>
              <th className="text-right py-1 font-normal">Prior</th>
              <th className="text-right py-1 font-normal">Latest</th>
              <th className="text-right py-1 font-normal">Trend</th>
            </tr>
          </thead>
          <tbody>
            {WASTE_CATEGORIES.map(({ key, label }) => {
              const block = trend.waste_taxonomy_shares[key];
              if (!block) return null;
              const fmtPct = (v: number) => `${(v * 100).toFixed(0)}%`;
              return <TrendRow key={key} label={label} block={block} fmt={fmtPct} />;
            })}
          </tbody>
        </table>
      </div>

      <div>
        <p className="text-xs font-semibold text-neutral-400 uppercase tracking-wide mb-1">
          Checkpoint Recovery Waste
        </p>
        <table className="w-full text-xs">
          <thead>
            <tr className="text-neutral-500 border-b border-neutral-800">
              <th className="text-left py-1 font-normal">Metric</th>
              <th className="text-right py-1 font-normal">Prior</th>
              <th className="text-right py-1 font-normal">Latest</th>
              <th className="text-right py-1 font-normal">Trend</th>
            </tr>
          </thead>
          <tbody>
            <TrendRow label="Recovery tasks" block={trend.checkpoint_recovery_waste_total} />
          </tbody>
        </table>
      </div>

      <div>
        <p className="text-xs font-semibold text-neutral-400 uppercase tracking-wide mb-1">
          Outer-Loop Convergence
        </p>
        <table className="w-full text-xs">
          <thead>
            <tr className="text-neutral-500 border-b border-neutral-800">
              <th className="text-left py-1 font-normal">Metric</th>
              <th className="text-right py-1 font-normal">Prior</th>
              <th className="text-right py-1 font-normal">Latest</th>
              <th className="text-right py-1 font-normal">Trend</th>
            </tr>
          </thead>
          <tbody>
            <TrendRow
              label="Median cycles"
              block={trend.outer_loop_convergence?.iteration_median}
            />
            <TrendRow label="P95 cycles" block={trend.outer_loop_convergence?.iteration_p95} />
            <TrendRow
              label="Convergence rate"
              block={trend.outer_loop_convergence?.convergence_rate}
              fmt={(v) => `${(v * 100).toFixed(0)}%`}
            />
            <TrendRow
              label="Cap-exhausted chains"
              block={trend.outer_loop_convergence?.cap_exhausted_chain_count}
            />
            <TrendRow
              label="Directions lost to cap"
              block={trend.outer_loop_convergence?.cap_exhausted_direction_count}
            />
            <TrendRow
              label="Superseded reviews to converge (median)"
              block={trend.outer_loop_convergence?.supersede_affected_reviews_to_converge_median}
            />
            <TrendRow
              label="Superseded reviews to converge (P95)"
              block={trend.outer_loop_convergence?.supersede_affected_reviews_to_converge_p95}
            />
            <TrendRow
              label="Rollup affected reviews (median)"
              block={trend.outer_loop_convergence?.rollup_supersede_affected_reviews_to_converge_median}
            />
            <TrendRow
              label="Rollup control reviews (median)"
              block={trend.outer_loop_convergence?.rollup_supersede_unaffected_reviews_to_converge_median}
            />
          </tbody>
        </table>
      </div>

      <div>
        <p className="text-xs font-semibold text-neutral-400 uppercase tracking-wide mb-1">
          Retarget Convergence
        </p>
        <table className="w-full text-xs">
          <thead>
            <tr className="text-neutral-500 border-b border-neutral-800">
              <th className="text-left py-1 font-normal">Metric</th>
              <th className="text-right py-1 font-normal">Prior</th>
              <th className="text-right py-1 font-normal">Latest</th>
              <th className="text-right py-1 font-normal">Trend</th>
            </tr>
          </thead>
          <tbody>
            <TrendRow
              label="Converged chains (sample size)"
              block={trend.retarget_convergence?.reviews_to_converge.count}
            />
            <TrendRow
              label="Reviews to converge (median)"
              block={trend.retarget_convergence?.reviews_to_converge.median}
            />
            <TrendRow
              label="Reviews to converge (P95)"
              block={trend.retarget_convergence?.reviews_to_converge.p95}
            />
            <TrendRow
              label="Delta-scope retargets"
              block={trend.retarget_convergence?.retarget_review_scope_counts.delta}
            />
            <TrendRow
              label="Full-scope retargets"
              block={trend.retarget_convergence?.retarget_review_scope_counts.full}
            />
            <TrendRow
              label="Stale-head discards"
              block={trend.retarget_convergence?.stale_head_discard_count}
            />
          </tbody>
        </table>
      </div>
    </div>
  );
}

interface LaneDiagPanelProps {
  projectId?: string;
}

export function LaneDiagPanel({ projectId }: LaneDiagPanelProps) {
  const [data, setData] = useState<LaneDiagResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [trend, setTrend] = useState<LaneBaselineTrend | null>(null);

  const fetchData = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const params = new URLSearchParams();
      if (projectId) params.set('project_id', projectId);
      const query = params.toString() ? `?${params.toString()}` : '';
      const res = await apiFetch(`/api/admin/lane-diag${query}`, undefined, 2, 30000);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const json = await res.json() as LaneDiagResponse;
      setData(json);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load lane diagnostics');
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  const fetchTrend = useCallback(async () => {
    if (!projectId) return;
    try {
      const baselines = await fetchLaneBaselines(projectId);
      setTrend(baselines.trend);
    } catch {
      // Trend is supplementary — silently skip on error.
    }
  }, [projectId]);

  useEffect(() => {
    fetchData();
    fetchTrend();
  }, [fetchData, fetchTrend]);

  if (loading) {
    return <p className="text-xs text-neutral-500">Loading lane diagnostics…</p>;
  }

  if (error) {
    return <p className="text-xs text-red-400">Error: {error}</p>;
  }

  if (!data) return null;

  const windowLabel = data.version
    ? `v${data.version.version_number}`
    : data.window_days != null
      ? `${data.window_days}d window`
      : 'rolling window';

  return (
    <div className="space-y-6">
      <div className="flex items-center gap-2 text-xs text-neutral-500">
        <span>{data.pr_count} PRs · {data.task_count} tasks · {windowLabel}</span>
        {data.truncated && <span className="text-amber-400">(truncated)</span>}
      </div>

      <div>
        <h4 className="text-xs font-semibold text-neutral-400 uppercase tracking-wide mb-2">
          Waste Taxonomy
        </h4>
        <WasteTaxonomyTable taxonomy={data.lane_waste.waste_taxonomy} />
      </div>

      <div>
        <h4 className="text-xs font-semibold text-neutral-400 uppercase tracking-wide mb-2">
          Retarget Convergence
        </h4>
        <RetargetConvergenceTable laneWaste={data.lane_waste} />
      </div>

      <div>
        <h4 className="text-xs font-semibold text-neutral-400 uppercase tracking-wide mb-2">
          Checkpoint Recovery Waste
        </h4>
        {data.checkpoint_recovery_waste != null ? (
          <CheckpointRecoveryWasteTable crw={data.checkpoint_recovery_waste} />
        ) : (
          <p className="text-xs text-neutral-500">No checkpoint-recovery waste recorded.</p>
        )}
      </div>

      <div>
        <h4 className="text-xs font-semibold text-neutral-400 uppercase tracking-wide mb-2">
          Relay Counts by Lane
        </h4>
        <RelayCountsTable counts={data.relay_counts_by_lane} />
      </div>

      <div>
        <h4 className="text-xs font-semibold text-neutral-400 uppercase tracking-wide mb-2">
          Per-PR Phase Breakdown
        </h4>
        <PhaseBreakdownTable
          rows={data.per_pr_phase_breakdown}
          truncated={data.pr_breakdown_truncated}
        />
      </div>

      {trend && (
        <div>
          <h4 className="text-xs font-semibold text-neutral-400 uppercase tracking-wide mb-2">
            Cross-Version Trend
          </h4>
          <CrossVersionTrendPanel trend={trend} />
        </div>
      )}
    </div>
  );
}
