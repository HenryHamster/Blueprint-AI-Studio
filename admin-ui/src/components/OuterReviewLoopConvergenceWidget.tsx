import { useState, useCallback, useEffect, useRef } from 'react';
import { ResponsiveContainer, AreaChart, Area, Tooltip } from 'recharts';
import {
  fetchOuterLoopConvergence,
  fetchOuterLoopConvergenceTrend,
  fetchOuterLoopChains,
  type OuterLoopConvergenceSummary,
  type OuterLoopConvergenceTrendBucket,
  type OuterLoopConvergenceTrendResponse,
  type OuterLoopChain,
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
// Human labels for the candidate non-convergence causes ranked by the backend
// dominant-blocker attribution (keys mirror digest_metrics.blocker_cause_counts).
const BLOCKER_CAUSE_LABELS: Record<string, string> = {
  churn: 'issue-churn',
  drift: 'file drift',
  stuck: 'stuck cycles',
  standoff: 'dispute standoff',
  repeat_fingerprint_halt: 'repeat-fingerprint halt',
  cap_exhausted: 'review-cap exhaustion',
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

// Valid drill-down `reason` values accepted by GET /api/admin/outer-loop-chains
// (mirrors _OUTER_LOOP_DRILLDOWN_REASONS in admin_pipeline_observability.py).
// Terminal-outcome keys map 1:1; incidence/escalation keys map onto their
// terminal-reason analog. Chips whose key has no mapping stay non-interactive.
const DRILLDOWN_REASONS = new Set([
  'rejected',
  'cap_exhausted',
  'stuck',
  'standoff',
  'repeat_fingerprint_halt',
  'merged',
]);
const INCIDENCE_DRILLDOWN_REASON: Record<string, string | undefined> = {
  stuck: 'stuck',
  standoff: 'standoff',
  repeat_fingerprint: 'repeat_fingerprint_halt',
};
const ESCALATION_DRILLDOWN_REASON: Record<string, string | undefined> = {
  stuck: 'stuck',
  standoff: 'standoff',
  'repeat-fingerprint': 'repeat_fingerprint_halt',
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
    <div className="bg-workbench-base/60 rounded-lg p-3 border border-neutral-800 flex flex-col gap-1 min-w-0">
      <span className={`text-lg font-bold leading-none ${color ?? 'text-neutral-100'}`}>{value}</span>
      <span className="text-ui-sm text-neutral-500 truncate">{label}</span>
    </div>
  );
}

function MetricChip({
  label,
  value,
  tone,
  title,
  onClick,
  active,
}: {
  label: string;
  value: string;
  tone?: string;
  title?: string;
  onClick?: () => void;
  active?: boolean;
}) {
  const toneCls = tone ?? 'bg-workbench-panel text-neutral-300 border-neutral-700';
  const content = (
    <>
      {label}
      <span className="font-bold">{value}</span>
    </>
  );
  if (onClick) {
    return (
      <button
        data-viewport-allow-small-target
        type="button"
        onClick={onClick}
        className={`inline-flex items-center gap-1 text-ui-sm px-1.5 py-0.5 rounded border cursor-pointer hover:brightness-125 transition-all ${toneCls} ${active ? 'ring-1 ring-white/40' : ''}`}
        title={title}
      >
        {content}
      </button>
    );
  }
  return (
    <span
      className={`inline-flex items-center gap-1 text-ui-sm px-1.5 py-0.5 rounded border ${toneCls}`}
      title={title}
    >
      {content}
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
            className={`${colors[i]} rounded`}
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

function getBucketChurn(bucket: OuterLoopConvergenceTrendBucket): number {
  return getOptionalCount(bucket.incidence_counts?.churn) ?? bucket.churn_total;
}

type ChurnDirection = 'falling' | 'rising' | 'flat';

// Back-compat FALLBACK only. The canonical churn direction is computed by the
// backend (digest_metrics.compute_churn_direction) and delivered on the trend
// response as `churn_direction`; the widget prefers that value (see the
// consumption site below) so its glyph matches the narrative's "Review/fix churn
// is X." verdict exactly. This local function is a byte-for-byte port of the same
// first-half-vs-second-half average algorithm, used ONLY when an older backend
// omits the field — both paths therefore yield identical directions and can
// never diverge from the narrative.
function getChurnDirection(buckets: OuterLoopConvergenceTrendBucket[]): ChurnDirection {
  if (buckets.length < 2) return 'flat';

  // Split into two halves and compare average churn per half
  const midpoint = Math.ceil(buckets.length / 2);
  const firstHalf = buckets.slice(0, midpoint);
  const secondHalf = buckets.slice(midpoint);

  const avgFirstHalf = firstHalf.reduce((sum, b) => sum + getBucketChurn(b), 0) / firstHalf.length;
  const avgSecondHalf = secondHalf.reduce((sum, b) => sum + getBucketChurn(b), 0) / secondHalf.length;

  // Small threshold (1%) to avoid flip-flopping on near-equal halves
  const threshold = 0.01 * Math.max(avgFirstHalf, avgSecondHalf, 1);
  const difference = avgSecondHalf - avgFirstHalf;

  if (difference < -threshold) return 'falling';
  if (difference > threshold) return 'rising';
  return 'flat';
}

const CHURN_DIRECTION_META: Record<ChurnDirection, { glyph: string; label: string; tone: string }> = {
  // Falling churn is the desirable direction, so it reads green.
  falling: { glyph: '▼', label: 'falling', tone: 'text-emerald-400' },
  rising: { glyph: '▲', label: 'rising', tone: 'text-red-400' },
  flat: { glyph: '▬', label: 'flat', tone: 'text-neutral-400' },
};

function ChurnTrend({ buckets }: { buckets: OuterLoopConvergenceTrendBucket[] }) {
  const data = buckets.map(b => ({ date: b.date, churn: getBucketChurn(b) }));
  const hasData = data.some(d => d.churn > 0);
  if (!hasData) return <span className="text-neutral-600 text-xs">no data</span>;
  return (
    <div className="h-12">
      <ResponsiveContainer width="100%" height="100%" minWidth={1} minHeight={1}>
        <AreaChart data={data} margin={{ top: 1, right: 0, left: 0, bottom: 1 }}>
          <defs>
            <linearGradient id="outer-churn" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="#fb923c" stopOpacity={0.3} />
              <stop offset="100%" stopColor="#fb923c" stopOpacity={0} />
            </linearGradient>
          </defs>
          <Tooltip
            contentStyle={TOOLTIP_CONTENT_STYLE}
            labelStyle={TOOLTIP_LABEL_STYLE}
            itemStyle={TOOLTIP_ITEM_STYLE}
            content={({ active, payload, label }) => {
              if (!active || !payload?.length) return null;
              const point = payload[0]?.payload as { churn: number } | undefined;
              if (!point) return null;
              return (
                <div style={TOOLTIP_CONTENT_STYLE}>
                  <div style={TOOLTIP_LABEL_STYLE}>{String(label)}</div>
                  <div style={TOOLTIP_ITEM_STYLE}>churn: {point.churn}</div>
                </div>
              );
            }}
            isAnimationActive={false}
          />
          <Area type="monotone" dataKey="churn" stroke="#fb923c" fill="url(#outer-churn)" strokeWidth={1.5} dot={false} isAnimationActive={false} connectNulls />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}

function ChainDrillDownPanel({
  reason,
  hours,
  projectId,
  onClose,
}: {
  reason: string;
  hours: number;
  projectId?: string;
  onClose: () => void;
}) {
  const [chains, setChains] = useState<OuterLoopChain[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const panelMountedRef = useRef(true);

  const fetchChains = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const body = await fetchOuterLoopChains(hours, projectId, reason, 50);
      if (!panelMountedRef.current) return;
      setChains(body.chains);
      setTotal(body.total);
      setLoading(false);
    } catch (e) {
      if (!panelMountedRef.current) return;
      if (isAbortError(e)) return;
      setError(e instanceof Error ? e.message : String(e));
      setLoading(false);
    }
  }, [reason, hours, projectId]);

  useEffect(() => {
    panelMountedRef.current = true;
    // eslint-disable-next-line react-hooks/set-state-in-effect -- async fetch sets state after await, intentional
    fetchChains();
    return () => { panelMountedRef.current = false; };
  }, [fetchChains]);

  const reasonCls = TERMINAL_OUTCOME_TONES[reason] ?? INCIDENCE_TONES[reason] ?? 'bg-workbench-control/20 text-neutral-400 border-neutral-500/30';
  const reasonLabel = TERMINAL_OUTCOME_LABELS[reason] ?? reason.replace(/_/g, ' ');

  return (
    <div className="mt-2 bg-workbench-base/80 rounded-lg border border-neutral-700 p-3">
      <div className="flex items-center justify-between gap-2 mb-2">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-ui-sm font-semibold uppercase tracking-wide text-neutral-400">PR chains</span>
          <span className={`inline-flex items-center text-ui-sm px-1.5 py-0.5 rounded border ${reasonCls}`}>
            {reasonLabel}
          </span>
          {!loading && <span className="text-ui-sm text-neutral-500">{total} chain{total !== 1 ? 's' : ''}</span>}
        </div>
        <button data-viewport-allow-small-target type="button" onClick={onClose} className="text-neutral-500 hover:text-neutral-300 text-xs leading-none px-1" aria-label="Close">✕</button>
      </div>

      {loading && <p className="text-ui-sm text-neutral-500">Loading…</p>}
      {error && <p className="text-ui-sm text-red-400">{error}</p>}
      {!loading && !error && chains.length === 0 && (
        <p className="text-ui-sm text-neutral-600">No PR chains found for this outcome.</p>
      )}
      {!loading && !error && chains.length > 0 && (
        <div className="flex flex-col gap-1.5 max-h-64 overflow-y-auto pr-1">
          {chains.map((c, i) => (
            <div key={c.pr_number ?? `chain-${i}`} className="bg-workbench-panel/60 rounded p-2 flex flex-col gap-0.5 min-w-0">
              <div className="flex items-center gap-2 flex-wrap">
                <span className="text-ui-md text-neutral-200 font-medium">
                  {c.pr_number != null ? `PR #${c.pr_number}` : 'no PR'}
                </span>
                {c.terminal_reason && (
                  <span className={`text-ui-sm px-1 py-0.5 rounded border ${TERMINAL_OUTCOME_TONES[c.terminal_reason] ?? 'bg-workbench-control/20 text-neutral-400 border-neutral-500/30'}`}>
                    {TERMINAL_OUTCOME_LABELS[c.terminal_reason] ?? c.terminal_reason.replace(/_/g, ' ')}
                  </span>
                )}
              </div>
              <div className="flex items-center gap-2 flex-wrap">
                <span className="text-ui-sm text-neutral-600">{c.max_iteration} iter</span>
                <span className="text-ui-sm text-neutral-600">churn {c.churn_count}</span>
                <span className="text-ui-sm text-neutral-500 truncate min-w-0" title={c.task_ids.join(', ')}>
                  {c.task_ids.length} task{c.task_ids.length !== 1 ? 's' : ''}
                </span>
              </div>
            </div>
          ))}
        </div>
      )}
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
  const [drillDown, setDrillDown] = useState<string | null>(null);
  const mountedRef = useRef(true);

  const handleChipClick = useCallback((reason: string) => {
    setDrillDown(prev => (prev === reason ? null : reason));
  }, []);

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
  const trendBuckets = trendData?.buckets ?? [];
  const showTrend = trendBuckets.length >= 2;
  // Prefer the backend-computed canonical direction; fall back to the local
  // (identical-algorithm) port only when an older backend omits the field.
  const churnDirection: ChurnDirection =
    trendData?.churn_direction ?? getChurnDirection(trendBuckets);
  const churnDirMeta = CHURN_DIRECTION_META[churnDirection];

  return (
    <section>
      <div className="flex items-center gap-2 mb-2">
        <p className="text-ui-sm text-neutral-500 uppercase tracking-wide">Outer Review↔Fix Convergence</p>
        <span className="text-ui-sm text-neutral-600">{data.prs_with_review_loop} PR cycles · {formatWindowHours(data.window_hours || windowHours)}</span>
      </div>

      <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
        <StatCard value={String(data.prs_with_review_loop)} label="PR cycles" />
        <StatCard value={convPct != null ? `${convPct}%` : '—'} label="converged" color={convColor} />
        <StatCard value={data.iteration_median != null ? String(data.iteration_median) : '—'} label="median cycles" />
        <StatCard value={data.iteration_p95 != null ? String(data.iteration_p95) : '—'} label="p95 cycles" />
      </div>

      {data.dominant_blocker && (
        <div className="mt-2 rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2">
          <p className="text-ui-sm uppercase tracking-wide text-amber-300/80">dominant non-convergence blocker</p>
          <p className="mt-0.5 text-xs text-amber-100">
            <span className="font-bold">
              {BLOCKER_CAUSE_LABELS[data.dominant_blocker.cause] ?? data.dominant_blocker.cause.replace(/_/g, ' ')}
            </span>
            {` — ${data.dominant_blocker.count} of ${data.dominant_blocker.total} non-converged PR chains (${formatPercent(data.dominant_blocker.share)})`}
          </p>
        </div>
      )}

      <div className="mt-2 flex flex-col gap-1">
        <span className="text-ui-sm text-neutral-500">cycles-to-terminal distribution</span>
        <IterDistBar dist={data.iteration_distribution} />
      </div>

      <div className="mt-2 flex flex-col gap-1">
        <span className="text-ui-sm text-neutral-500">terminal outcomes</span>
        <div className="flex flex-wrap gap-1">
          {terminalOutcomes.map(({ key, count }) => {
            const canDrill = DRILLDOWN_REASONS.has(key);
            return (
              <MetricChip
                key={key}
                label={TERMINAL_OUTCOME_LABELS[key] ?? key.replace(/_/g, ' ')}
                value={String(count)}
                tone={count > 0 ? TERMINAL_OUTCOME_TONES[key] : undefined}
                title={canDrill ? `Show ${key.replace(/_/g, ' ')} PR chains` : undefined}
                onClick={canDrill ? () => handleChipClick(key) : undefined}
                active={drillDown === key}
              />
            );
          })}
          <MetricChip
            label="drift"
            value={String(data.drift_pr_count)}
            tone={data.drift_pr_count > 0 ? TERMINAL_OUTCOME_TONES.drift : undefined}
            title="PR chains whose drift signal showed files entering or leaving the loop"
          />
        </div>
      </div>

      <div className="mt-2 flex flex-col gap-1">
        <span className="text-ui-sm text-neutral-500">incidence</span>
        <div className="flex flex-wrap gap-1">
          {incidenceEntries.map(({ key, count, rate }) => {
            const reason = INCIDENCE_DRILLDOWN_REASON[key];
            return (
              <MetricChip
                key={key}
                label={INCIDENCE_LABELS[key] ?? key.replace(/_/g, ' ')}
                value={`${count} · ${formatPercent(rate)}`}
                tone={count > 0 ? INCIDENCE_TONES[key] : undefined}
                title={reason ? `Show ${key.replace(/_/g, ' ')} PR chains` : undefined}
                onClick={reason ? () => handleChipClick(reason) : undefined}
                active={reason != null && drillDown === reason}
              />
            );
          })}
          <MetricChip
            label="churn events"
            value={String(data.churn_total)}
            tone={data.churn_total > 0 ? INCIDENCE_TONES.churn : undefined}
          />
        </div>
      </div>

      {escReasons.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-1">
          {escReasons.map(([reason, count]) => {
            const drillReason = ESCALATION_DRILLDOWN_REASON[reason];
            return (
              <MetricChip
                key={reason}
                label={ESCALATION_LABELS[reason] ?? reason}
                value={String(count)}
                tone="bg-red-500/20 text-red-400 border-red-500/30"
                title={drillReason ? `Show ${reason} PR chains` : `Escalation alerts tagged ${reason}`}
                onClick={drillReason ? () => handleChipClick(drillReason) : undefined}
                active={drillReason != null && drillDown === drillReason}
              />
            );
          })}
        </div>
      )}

      {drillDown && (
        <ChainDrillDownPanel
          reason={drillDown}
          hours={data.window_hours || windowHours}
          projectId={projectId}
          onClose={() => setDrillDown(null)}
        />
      )}

      {showTrend && (
        <div className="mt-3 grid grid-cols-1 sm:grid-cols-2 gap-3">
          <div className="min-w-0">
            <div className="flex items-center gap-2 mb-1.5">
              <p className="text-ui-sm text-neutral-500 uppercase tracking-wide">{trendDays}-day convergence trend</p>
              <span className="flex items-center gap-1 text-ui-sm text-neutral-600">
                <span className="inline-block w-3 border-t border-emerald-400" /> converged %
              </span>
            </div>
            <ConvergenceTrend buckets={trendBuckets} />
          </div>
          <div className="min-w-0">
            <div className="flex items-center gap-2 mb-1.5 flex-wrap">
              <p className="text-ui-sm text-neutral-500 uppercase tracking-wide">{trendDays}-day churn trend</p>
              <span className={`inline-flex items-center gap-0.5 text-ui-sm font-semibold ${churnDirMeta.tone}`} title={`Churn is ${churnDirMeta.label} across the window`}>
                <span aria-hidden>{churnDirMeta.glyph}</span> {churnDirMeta.label}
              </span>
            </div>
            <ChurnTrend buckets={trendBuckets} />
          </div>
          {latestBucket && (
            <p className="mt-1 text-ui-sm text-neutral-600 sm:col-span-2">{getTrendBucketSummary(latestBucket)}</p>
          )}
        </div>
      )}
    </section>
  );
}
