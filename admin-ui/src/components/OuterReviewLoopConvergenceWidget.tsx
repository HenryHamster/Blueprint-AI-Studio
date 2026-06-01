import { useState, useCallback, useEffect, useRef } from 'react';
import { ResponsiveContainer, AreaChart, Area, Tooltip } from 'recharts';
import {
  fetchOuterLoopConvergence,
  fetchOuterLoopConvergenceTrend,
  type OuterLoopConvergenceSummary,
  type OuterLoopConvergenceTrendBucket,
  type OuterLoopConvergenceTrendResponse,
} from '../api';
import { usePoller } from '../hooks/usePoller';

interface OuterReviewLoopConvergenceWidgetProps {
  trendDays?: 7 | 14 | 30;
  projectId?: string;
  windowHours?: number;
}

const POLL_MS = 60_000;
const DIST_KEYS = ['1', '2', '3', '4+'] as const;
const TERMINAL_OUTCOME_ORDER = [
  'merged',
  'rejected',
  'cap_exhausted',
  'stuck',
  'standoff',
  'repeat_fingerprint_halt',
] as const;
const INCIDENCE_ORDER = [
  'churn',
  'stuck',
  'standoff',
  'repeat_fingerprint',
  'overall_escalations',
] as const;

// Human labels for the three early-escalation reasons emitted by the backend
// (keys mirror digest_metrics._ESCALATION_LABEL_TO_REASON values).
const ESCALATION_LABELS: Record<string, string> = {
  stuck: 'stuck',
  standoff: 'standoff',
  'repeat-fingerprint': 'repeat-fp',
};

const TOOLTIP_CONTENT_STYLE = { background: '#1a1a1a', border: '1px solid #404040', borderRadius: 4, padding: '4px 8px', fontSize: 10 };
const TOOLTIP_LABEL_STYLE = { color: '#a3a3a3' };
const TOOLTIP_ITEM_STYLE = { color: '#e5e5e5' };
const TERMINAL_OUTCOME_LABELS: Record<string, string> = {
  merged: 'merged',
  rejected: 'rejected',
  cap_exhausted: 'cap-exhausted',
  stuck: 'stuck',
  standoff: 'standoff',
  repeat_fingerprint_halt: 'repeat-fp halt',
};
const TERMINAL_OUTCOME_TONES: Record<string, string> = {
  merged: 'bg-emerald-500/20 text-emerald-400 border-emerald-500/30',
  rejected: 'bg-yellow-500/20 text-yellow-400 border-yellow-500/30',
  cap_exhausted: 'bg-red-500/20 text-red-400 border-red-500/30',
  stuck: 'bg-rose-500/20 text-rose-400 border-rose-500/30',
  standoff: 'bg-fuchsia-500/20 text-fuchsia-400 border-fuchsia-500/30',
  repeat_fingerprint_halt: 'bg-orange-500/20 text-orange-300 border-orange-500/30',
  drift: 'bg-amber-500/20 text-amber-300 border-amber-500/30',
};
const INCIDENCE_LABELS: Record<string, string> = {
  churn: 'churn',
  stuck: 'stuck',
  standoff: 'standoff',
  repeat_fingerprint: 'repeat-fp',
  overall_escalations: 'escalations',
};
const INCIDENCE_TONES: Record<string, string> = {
  churn: 'bg-orange-500/20 text-orange-300 border-orange-500/30',
  stuck: 'bg-rose-500/20 text-rose-400 border-rose-500/30',
  standoff: 'bg-fuchsia-500/20 text-fuchsia-400 border-fuchsia-500/30',
  repeat_fingerprint: 'bg-amber-500/20 text-amber-300 border-amber-500/30',
  overall_escalations: 'bg-red-500/20 text-red-400 border-red-500/30',
};

function formatPercent(rate: number | null | undefined): string {
  return rate == null ? '—' : `${Math.round(rate * 100)}%`;
}

function formatWindowHours(hours: number): string {
  if (hours >= 168 && hours % 24 === 0) return `${hours / 24}d`;
  return `${hours}h`;
}

function getCount(value: unknown): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : 0;
}

function getOptionalCount(value: unknown): number | undefined {
  return typeof value === 'number' && Number.isFinite(value) ? value : undefined;
}

function getRate(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function isAbortError(error: unknown): boolean {
  return error instanceof Error && error.name === 'AbortError';
}

function getLegacyOutcomeCount(data: OuterLoopConvergenceSummary, key: string): number | undefined {
  const outcomes = data.outcomes;
  if (!outcomes) return undefined;
  if (key === 'rejected') {
    const rejected = getOptionalCount(outcomes.rejected);
    if (rejected == null) return undefined;
    const capExhausted = getOptionalCount(outcomes.cap_exhausted) ?? 0;
    return Math.max(rejected - capExhausted, 0);
  }
  return getOptionalCount(outcomes[key]);
}

function getTerminalOutcomeEntries(data: OuterLoopConvergenceSummary): Array<{ key: string; count: number }> {
  const histogram = data.terminal_outcome_histogram ?? {};
  const ordered = TERMINAL_OUTCOME_ORDER.map((key) => ({
    key,
    count: getOptionalCount(histogram[key]) ?? getLegacyOutcomeCount(data, key) ?? 0,
  }));
  const extras = Object.entries(histogram)
    .filter(([key]) => !TERMINAL_OUTCOME_ORDER.includes(key as typeof TERMINAL_OUTCOME_ORDER[number]))
    .map(([key, count]) => ({ key, count: getCount(count) }));
  return [...ordered, ...extras];
}

function getIncidenceEntries(data: OuterLoopConvergenceSummary): Array<{ key: string; count: number; rate: number | null }> {
  const counts = data.incidence_counts ?? {};
  const rates = data.incidence_rates ?? {};
  const denominator = data.prs_with_review_loop;

  const fallbackCounts: Record<string, number> = {
    churn: data.churn_pr_count,
    stuck: getOptionalCount(data.terminal_outcome_histogram?.stuck) ?? 0,
    standoff: getOptionalCount(data.terminal_outcome_histogram?.standoff) ?? 0,
    repeat_fingerprint: getOptionalCount(data.terminal_outcome_histogram?.repeat_fingerprint_halt) ?? 0,
    overall_escalations: data.escalation_total,
  };

  return INCIDENCE_ORDER.map((key) => {
    const count = getOptionalCount(counts[key]) ?? fallbackCounts[key] ?? 0;
    const rate = getRate(rates[key]) ?? (denominator > 0 ? count / denominator : null);
    return { key, count, rate };
  });
}

function getTrendBucketSummary(bucket: OuterLoopConvergenceTrendBucket): string {
  const churnCount = getOptionalCount(bucket.incidence_counts?.churn) ?? bucket.churn_total;
  const escalationCount = getOptionalCount(bucket.incidence_counts?.overall_escalations) ?? bucket.escalation_total;
  return `${bucket.date} · ${bucket.pr_count} PRs · churn ${churnCount} · escalations ${escalationCount}`;
}

function StatCard({ value, label, color }: { value: string; label: string; color?: string }) {
  return (
    <div className="bg-neutral-900/60 rounded-lg p-3 border border-neutral-800 flex flex-col gap-1 min-w-0">
      <span className={`text-lg font-bold leading-none ${color ?? 'text-neutral-100'}`}>{value}</span>
      <span className="text-[10px] text-neutral-500 truncate">{label}</span>
    </div>
  );
}

function MetricChip({
  label,
  value,
  tone,
  title,
}: {
  label: string;
  value: string;
  tone?: string;
  title?: string;
}) {
  return (
    <span
      className={`inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded border ${tone ?? 'bg-neutral-800 text-neutral-300 border-neutral-700'}`}
      title={title}
    >
      {label}
      <span className="font-bold">{value}</span>
    </span>
  );
}

function IterDistBar({ dist }: { dist: Record<string, number> }) {
  const total = DIST_KEYS.reduce((s, k) => s + (dist[k] ?? 0), 0);
  if (total === 0) return <span className="text-neutral-600 text-xs">no data</span>;
  const colors = ['bg-emerald-600', 'bg-yellow-600', 'bg-orange-500', 'bg-red-500'];
  return (
    <div className="flex gap-0.5 items-end h-4 w-full">
      {DIST_KEYS.map((k, i) => {
        const count = dist[k] ?? 0;
        const pct = total > 0 ? (count / total) * 100 : 0;
        return (
          <div
            key={k}
            title={`${k} cycle${k === '1' ? '' : 's'}: ${count} (${Math.round(pct)}%)`}
            // No `shrink-0`: the bar widths sum to 100% and the inter-bar
            // `gap-0.5` (6px total) would otherwise overflow `w-full` by 6px at
            // narrow viewports. Allowing flex-shrink lets the bars absorb the
            // gap so the row fits exactly without clipping.
            className={`${colors[i]} rounded-sm`}
            style={{ width: `${Math.max(pct, 2)}%`, height: `${Math.max((pct / 100) * 16, 2)}px` }}
          />
        );
      })}
    </div>
  );
}

function ConvergenceTrend({ buckets }: { buckets: OuterLoopConvergenceTrendBucket[] }) {
  const data = buckets.map(b => ({
    date: b.date,
    conv: b.convergence_rate != null ? Math.round(b.convergence_rate * 100) : null,
    prCount: b.pr_count,
    churnCount: getOptionalCount(b.incidence_counts?.churn) ?? b.churn_total,
    escalationCount: getOptionalCount(b.incidence_counts?.overall_escalations) ?? b.escalation_total,
  }));
  const hasData = data.some(d => d.conv != null);
  if (!hasData) return <span className="text-neutral-600 text-xs">no data</span>;
  return (
    <div className="h-12">
      <ResponsiveContainer width="100%" height="100%" minWidth={1} minHeight={1}>
        <AreaChart data={data} margin={{ top: 1, right: 0, left: 0, bottom: 1 }}>
          <defs>
            <linearGradient id="outer-conv" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="#34d399" stopOpacity={0.3} />
              <stop offset="100%" stopColor="#34d399" stopOpacity={0} />
            </linearGradient>
          </defs>
          <Tooltip
            contentStyle={TOOLTIP_CONTENT_STYLE}
            labelStyle={TOOLTIP_LABEL_STYLE}
            itemStyle={TOOLTIP_ITEM_STYLE}
            formatter={(value: unknown): [string, string] => [value == null ? '—' : `${value}%`, 'converged']}
            labelFormatter={(label: unknown): string => String(label)}
            content={({ active, payload, label }) => {
              if (!active || !payload?.length) return null;
              const point = payload[0]?.payload as { conv: number | null; prCount: number; churnCount: number; escalationCount: number } | undefined;
              if (!point) return null;
              return (
                <div style={TOOLTIP_CONTENT_STYLE}>
                  <div style={TOOLTIP_LABEL_STYLE}>{String(label)}</div>
                  <div style={TOOLTIP_ITEM_STYLE}>converged: {point.conv == null ? '—' : `${point.conv}%`}</div>
                  <div style={TOOLTIP_ITEM_STYLE}>PRs: {point.prCount}</div>
                  <div style={TOOLTIP_ITEM_STYLE}>churn: {point.churnCount}</div>
                  <div style={TOOLTIP_ITEM_STYLE}>escalations: {point.escalationCount}</div>
                </div>
              );
            }}
            isAnimationActive={false}
          />
          <Area type="monotone" dataKey="conv" stroke="#34d399" fill="url(#outer-conv)" strokeWidth={1.5} dot={false} isAnimationActive={false} connectNulls />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}

export function OuterReviewLoopConvergenceWidget({
  trendDays = 7,
  projectId,
  windowHours = 24,
}: OuterReviewLoopConvergenceWidgetProps) {
  const [data, setData] = useState<OuterLoopConvergenceSummary | null>(null);
  const [trendData, setTrendData] = useState<OuterLoopConvergenceTrendResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [fetchError, setFetchError] = useState<string | null>(null);
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const fetchData = useCallback(async (signal: AbortSignal) => {
    const [summaryResult, trendResult] = await Promise.allSettled([
      fetchOuterLoopConvergence(windowHours, projectId, signal),
      fetchOuterLoopConvergenceTrend(trendDays, projectId, signal),
    ]);

    if (!mountedRef.current || signal.aborted) return;
    if (summaryResult.status === 'rejected') {
      const error = summaryResult.reason;
      if (!mountedRef.current || signal.aborted || isAbortError(error)) return;
      setFetchError(error instanceof Error ? error.message : 'network error');
      setLoading(false);
      return;
    }

    if (signal.aborted) return;
    setData(summaryResult.value);
    setFetchError(null);
    setLoading(false);
    setTrendData(trendResult.status === 'fulfilled' ? trendResult.value : null);
  }, [projectId, trendDays, windowHours]);

  usePoller(fetchData, { intervalMs: POLL_MS, deps: [fetchData] });

  if (loading) return null;
  if (fetchError) return null;
  if (!data || data.prs_with_review_loop === 0) return null;

  const convPct = data.convergence_rate != null ? Math.round(data.convergence_rate * 100) : null;
  const convColor = convPct == null ? 'text-neutral-500' : convPct >= 80 ? 'text-emerald-400' : convPct >= 50 ? 'text-yellow-400' : 'text-red-400';
  const escReasons = Object.entries(data.escalation_by_reason).filter(([, c]) => c > 0);
  const terminalOutcomes = getTerminalOutcomeEntries(data);
  const incidenceEntries = getIncidenceEntries(data);
  const latestBucket = trendData?.buckets.at(-1) ?? null;

  return (
    <section>
      <div className="flex items-center gap-2 mb-2">
        <p className="text-[10px] text-neutral-500 uppercase tracking-wide">Outer Review↔Fix Convergence</p>
        <span className="text-[10px] text-neutral-600">{data.prs_with_review_loop} PR cycles · {formatWindowHours(data.window_hours || windowHours)}</span>
      </div>

      <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
        <StatCard value={String(data.prs_with_review_loop)} label="PR cycles" />
        <StatCard value={convPct != null ? `${convPct}%` : '—'} label="converged" color={convColor} />
        <StatCard value={data.iteration_median != null ? String(data.iteration_median) : '—'} label="median cycles" />
        <StatCard value={data.iteration_p95 != null ? String(data.iteration_p95) : '—'} label="p95 cycles" />
      </div>

      <div className="mt-2 flex flex-col gap-1">
        <span className="text-[10px] text-neutral-500">cycles-to-terminal distribution</span>
        <IterDistBar dist={data.iteration_distribution} />
      </div>

      <div className="mt-2 flex flex-col gap-1">
        <span className="text-[10px] text-neutral-500">terminal outcomes</span>
        <div className="flex flex-wrap gap-1">
          {terminalOutcomes.map(({ key, count }) => (
            <MetricChip
              key={key}
              label={TERMINAL_OUTCOME_LABELS[key] ?? key.replace(/_/g, ' ')}
              value={String(count)}
              tone={count > 0 ? TERMINAL_OUTCOME_TONES[key] : undefined}
            />
          ))}
          <MetricChip
            label="drift"
            value={String(data.drift_pr_count)}
            tone={data.drift_pr_count > 0 ? TERMINAL_OUTCOME_TONES.drift : undefined}
            title="PR chains whose drift signal showed files entering or leaving the loop"
          />
        </div>
      </div>

      <div className="mt-2 flex flex-col gap-1">
        <span className="text-[10px] text-neutral-500">incidence</span>
        <div className="flex flex-wrap gap-1">
          {incidenceEntries.map(({ key, count, rate }) => (
            <MetricChip
              key={key}
              label={INCIDENCE_LABELS[key] ?? key.replace(/_/g, ' ')}
              value={`${count} · ${formatPercent(rate)}`}
              tone={count > 0 ? INCIDENCE_TONES[key] : undefined}
            />
          ))}
          <MetricChip
            label="churn events"
            value={String(data.churn_total)}
            tone={data.churn_total > 0 ? INCIDENCE_TONES.churn : undefined}
          />
        </div>
      </div>

      {escReasons.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-1">
          {escReasons.map(([reason, count]) => (
            <MetricChip
              key={reason}
              label={ESCALATION_LABELS[reason] ?? reason}
              value={String(count)}
              tone="bg-red-500/20 text-red-400 border-red-500/30"
              title={`Escalation alerts tagged ${reason}`}
            />
          ))}
        </div>
      )}

      {trendData && trendData.buckets.length >= 2 && (
        <div className="mt-3">
          <div className="flex items-center gap-2 mb-1.5">
            <p className="text-[10px] text-neutral-500 uppercase tracking-wide">{trendDays}-day convergence trend</p>
            <span className="flex items-center gap-1 text-[10px] text-neutral-600">
              <span className="inline-block w-3 border-t border-emerald-400" /> converged %
            </span>
          </div>
          <ConvergenceTrend buckets={trendData.buckets} />
          {latestBucket && (
            <p className="mt-1 text-[10px] text-neutral-600">{getTrendBucketSummary(latestBucket)}</p>
          )}
        </div>
      )}
    </section>
  );
}
