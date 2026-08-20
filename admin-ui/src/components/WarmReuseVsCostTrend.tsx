/* eslint-disable react-hooks/set-state-in-effect */
import { useEffect, useState } from 'react';
import {
  ResponsiveContainer,
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ReferenceLine,
  Legend,
} from 'recharts';
import { fetchEfficiencyAxes, type EfficiencyAxesResponse } from '../api';
import { WarmStartOriginRow } from './WarmStartOriginRow';

const TICK_STYLE = { fill: '#737373', fontSize: 10 };
const GRID_COLOR = '#262626';
const TOOLTIP_STYLE = {
  contentStyle: { background: '#171717', border: '1px solid #404040', borderRadius: 4 },
  itemStyle: { color: '#f5f5f5' },
  labelStyle: { color: '#a3a3a3' },
};

const REUSE_COLOR = '#10b981';
const COST_COLOR = '#f59e0b';
// Idle-inclusive cost line: a hotter hue + dashed stroke so it reads as the
// "true cost" companion to the solid headline cost line on the same axis.
const COST_INCL_COLOR = '#ef4444';

// Keys on the efficiency-axes response. cost_per_merged_pr is the idle-EXCLUSIVE
// headline axis. The idle-INCLUSIVE companion lives on a dedicated top-level key
// (cost_per_merged_pr_incl_pool_idle_axis) rather than inside `axes`, because the
// backend keeps it out of the immutable baseline axis set on purpose.
const REUSE_KEY = 'vm_reuse_rate';
const COST_KEY = 'cost_per_merged_pr';

interface MergedPoint {
  captured_at: string;
  reuse: number | null;
  cost: number | null;
  costIncl: number | null;
}

// Zip the reuse, headline-cost, and idle-inclusive-cost history arrays onto one
// shared captured_at time axis.
function mergeHistory(data: EfficiencyAxesResponse): MergedPoint[] {
  const byTime = new Map<string, MergedPoint>();
  const reuseHistory = data.axes[REUSE_KEY]?.history ?? [];
  const costHistory = data.axes[COST_KEY]?.history ?? [];
  const costInclHistory = data.cost_per_merged_pr_incl_pool_idle_axis?.history ?? [];

  const ensure = (captured_at: string): MergedPoint => {
    let point = byTime.get(captured_at);
    if (!point) {
      point = { captured_at, reuse: null, cost: null, costIncl: null };
      byTime.set(captured_at, point);
    }
    return point;
  };

  for (const h of reuseHistory) {
    if (!h.captured_at) continue;
    ensure(h.captured_at).reuse = h.value;
  }
  for (const h of costHistory) {
    if (!h.captured_at) continue;
    ensure(h.captured_at).cost = h.value;
  }
  for (const h of costInclHistory) {
    if (!h.captured_at) continue;
    ensure(h.captured_at).costIncl = h.value;
  }

  return Array.from(byTime.values()).sort((a, b) => a.captured_at.localeCompare(b.captured_at));
}

function formatDay(captured_at: string): string {
  const d = new Date(captured_at);
  if (Number.isNaN(d.getTime())) return captured_at;
  const month = (d.getMonth() + 1).toString().padStart(2, '0');
  const day = d.getDate().toString().padStart(2, '0');
  return `${month}/${day}`;
}

const COST_LABEL = 'Cost / merged PR';
const COST_INCL_LABEL = 'Cost / merged PR (incl. pool idle)';

function tooltipFormatter(value: unknown, name: unknown): [string, string] {
  const label = String(name ?? '');
  if (value === null || value === undefined) return ['—', label];
  if (label === COST_LABEL || label === COST_INCL_LABEL) {
    return [`$${(value as number).toFixed(2)}`, label];
  }
  return [`${(value as number).toFixed(1)}%`, label];
}

export function WarmReuseVsCostTrend({ projectId }: { projectId: string | null }) {
  const [data, setData] = useState<EfficiencyAxesResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!projectId) {
      setLoading(false);
      return;
    }
    let mounted = true;
    setLoading(true);
    setError(null);
    (async () => {
      try {
        const result = await fetchEfficiencyAxes(projectId);
        if (mounted) {
          setData(result);
          setError(null);
        }
      } catch (err) {
        if (mounted) setError(err instanceof Error ? err.message : 'Unknown error');
      } finally {
        if (mounted) setLoading(false);
      }
    })();
    return () => { mounted = false; };
  }, [projectId]);

  if (!projectId) return null;

  const title = (
    <h2 className="text-ui-md font-bold text-neutral-400 uppercase tracking-widest mb-2">
      Warm Reuse vs Cost / Merged PR
    </h2>
  );

  if (loading) {
    return (
      <section>
        {title}
        <div className="flex items-center justify-center h-24 text-neutral-500 text-xs">Loading trend…</div>
      </section>
    );
  }

  if (error) {
    return (
      <section>
        {title}
        <div className="flex items-center justify-center h-24 text-red-400 text-xs break-all px-2">{error}</div>
      </section>
    );
  }

  const merged = data ? mergeHistory(data) : [];
  const reuseBaseline = data?.axes[REUSE_KEY]?.baseline ?? null;
  const costInclAxis = data?.cost_per_merged_pr_incl_pool_idle_axis ?? null;
  const costBaseline = costInclAxis?.baseline ?? null;
  const costInclDrift = costInclAxis?.drift_pct ?? null;
  const hasCostIncl = merged.some((p) => p.costIncl !== null);

  if (merged.length < 2) {
    return (
      <section>
        {title}
        <div className="flex items-center justify-center h-24 text-neutral-500 text-xs text-center px-2">
          Baseline pending (waiting for the daily efficiency snapshot history to fill)
        </div>
      </section>
    );
  }

  const driftReadout =
    hasCostIncl && costInclDrift !== null ? (
      <span
        data-testid="cost-incl-drift"
        className={`ml-2 text-ui-sm font-semibold normal-case tracking-normal ${
          costInclDrift > 0 ? 'text-red-400' : 'text-emerald-400'
        }`}
      >
        idle-incl cost {costInclDrift > 0 ? '+' : ''}
        {costInclDrift.toFixed(1)}% vs baseline
      </span>
    ) : null;

  return (
    <section>
      <h2 className="text-ui-md font-bold text-neutral-400 uppercase tracking-widest mb-2">
        Warm Reuse vs Cost / Merged PR
        {driftReadout}
      </h2>
      <div className="bg-workbench-base border border-neutral-800 rounded-lg p-3">
        <ResponsiveContainer width="100%" height={200}>
          <LineChart data={merged} margin={{ top: 4, right: 4, left: 0, bottom: 0 }}>
            <CartesianGrid stroke={GRID_COLOR} vertical={false} />
            <XAxis dataKey="captured_at" tickFormatter={formatDay} tick={TICK_STYLE} interval="preserveStartEnd" />
            <YAxis
              yAxisId="reuse"
              tick={TICK_STYLE}
              domain={[0, 100]}
              tickFormatter={(v) => `${v}%`}
              width={36}
            />
            <YAxis
              yAxisId="cost"
              orientation="right"
              tick={TICK_STYLE}
              tickFormatter={(v) => `$${v}`}
              width={40}
            />
            <Tooltip {...TOOLTIP_STYLE} formatter={tooltipFormatter} labelFormatter={(v) => formatDay(String(v))} />
            <Legend wrapperStyle={{ fontSize: 11, color: '#a3a3a3' }} />
            {reuseBaseline !== null && (
              <ReferenceLine
                yAxisId="reuse"
                y={reuseBaseline}
                stroke={REUSE_COLOR}
                strokeDasharray="4 3"
                strokeOpacity={0.5}
                label={{ value: 'reuse baseline', position: 'insideTopLeft', fill: '#737373', fontSize: 9 }}
              />
            )}
            <Line
              yAxisId="reuse"
              dataKey="reuse"
              stroke={REUSE_COLOR}
              dot={false}
              strokeWidth={1.5}
              name="Warm reuse rate"
              connectNulls
            />
            {hasCostIncl && costBaseline !== null && (
              <ReferenceLine
                yAxisId="cost"
                y={costBaseline}
                stroke={COST_INCL_COLOR}
                strokeDasharray="4 3"
                strokeOpacity={0.5}
                label={{ value: 'cost baseline', position: 'insideBottomRight', fill: '#737373', fontSize: 9 }}
              />
            )}
            <Line
              yAxisId="cost"
              dataKey="cost"
              stroke={COST_COLOR}
              dot={false}
              strokeWidth={1.5}
              name={COST_LABEL}
              connectNulls
            />
            {hasCostIncl && (
              <Line
                yAxisId="cost"
                dataKey="costIncl"
                stroke={COST_INCL_COLOR}
                strokeDasharray="5 3"
                dot={false}
                strokeWidth={1.5}
                name={COST_INCL_LABEL}
                connectNulls
              />
            )}
          </LineChart>
        </ResponsiveContainer>
        {/* Truthful per-origin companion to the contaminated aggregate reuse
            line: buckets warm starts into pool/backlog/chain + pool-origin
            share so the headline reuse trend is never read in isolation.
            Renders nothing when the backend omits the breakdown. */}
        <div className="mt-2 pt-2 border-t border-neutral-800 text-ui-sm flex">
          <WarmStartOriginRow breakdown={data?.warm_start_origin_breakdown} />
        </div>
      </div>
    </section>
  );
}
