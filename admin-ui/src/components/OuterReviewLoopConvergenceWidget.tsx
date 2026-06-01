import { useState, useCallback, useRef } from 'react';
import { ResponsiveContainer, AreaChart, Area, Tooltip } from 'recharts';
import { apiFetch } from '../api';
import { usePoller } from '../hooks/usePoller';

interface Outcomes {
  merged: number;
  rejected: number;
  cap_exhausted: number;
  repeat_fingerprint_halt: number;
}

interface OuterLoopData {
  window_hours: number;
  prs_with_review_loop: number;
  iteration_median: number | null;
  iteration_p95: number | null;
  iteration_distribution: Record<string, number>;
  churn_total: number;
  churn_pr_count: number;
  drift_pr_count: number;
  outcomes: Outcomes;
  escalation_total: number;
  escalation_by_reason: Record<string, number>;
  convergence_rate: number | null;
}

interface TrendBucket {
  date: string;
  pr_count: number;
  convergence_rate: number | null;
  escalation_total: number;
  churn_total: number;
}

interface TrendData {
  days: number;
  buckets: TrendBucket[];
}

interface OuterReviewLoopConvergenceWidgetProps {
  trendDays?: 7 | 14 | 30;
  projectId?: string;
}

const POLL_MS = 60_000;
const DIST_KEYS = ['1', '2', '3', '4+'] as const;

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
const tooltipFormatter = (value: unknown): [string, string] => [value == null ? '—' : `${value}%`, 'converged'];
const tooltipLabelFormatter = (label: unknown): string => String(label);

function StatCard({ value, label, color }: { value: string; label: string; color?: string }) {
  return (
    <div className="bg-neutral-900/60 rounded-lg p-3 border border-neutral-800 flex flex-col gap-1 min-w-0">
      <span className={`text-lg font-bold leading-none ${color ?? 'text-neutral-100'}`}>{value}</span>
      <span className="text-[10px] text-neutral-500 truncate">{label}</span>
    </div>
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
            className={`${colors[i]} rounded-sm shrink-0`}
            style={{ width: `${Math.max(pct, 2)}%`, height: `${Math.max((pct / 100) * 16, 2)}px` }}
          />
        );
      })}
    </div>
  );
}

function ConvergenceTrend({ buckets }: { buckets: TrendBucket[] }) {
  const data = buckets.map(b => ({
    date: b.date,
    conv: b.convergence_rate != null ? Math.round(b.convergence_rate * 100) : null,
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
            formatter={tooltipFormatter}
            labelFormatter={tooltipLabelFormatter}
            isAnimationActive={false}
          />
          <Area type="monotone" dataKey="conv" stroke="#34d399" fill="url(#outer-conv)" strokeWidth={1.5} dot={false} isAnimationActive={false} connectNulls />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}

export function OuterReviewLoopConvergenceWidget({ trendDays = 7, projectId }: OuterReviewLoopConvergenceWidgetProps) {
  const [data, setData] = useState<OuterLoopData | null>(null);
  const [trendData, setTrendData] = useState<TrendData | null>(null);
  const [loading, setLoading] = useState(true);
  const [fetchError, setFetchError] = useState<string | null>(null);
  const mountedRef = useRef(true);

  const fetchData = useCallback(async () => {
    const proj = projectId ? `&project_id=${encodeURIComponent(projectId)}` : '';
    const projTrend = projectId ? `&project_id=${encodeURIComponent(projectId)}` : '';
    try {
      const [res, trendRes] = await Promise.all([
        apiFetch(`/api/admin/outer-loop-convergence?hours=24${proj}`),
        apiFetch(`/api/admin/outer-loop-convergence-trend?days=${trendDays}${projTrend}`),
      ]);
      if (!mountedRef.current) return;
      if (!res.ok) {
        setFetchError(`HTTP ${res.status}`);
        setLoading(false);
        return;
      }
      const body = (await res.json()) as OuterLoopData;
      if (!mountedRef.current) return;
      setData(body);
      setFetchError(null);
      setLoading(false);
      if (trendRes.ok) {
        const trendBody = (await trendRes.json()) as TrendData;
        if (mountedRef.current) setTrendData(trendBody);
      }
    } catch (e) {
      if (!mountedRef.current) return;
      setFetchError(e instanceof Error ? e.message : 'network error');
      setLoading(false);
    }
  }, [trendDays, projectId]);

  usePoller(fetchData, { intervalMs: POLL_MS, deps: [fetchData] });

  if (loading) return null;
  if (fetchError) return null;
  if (!data || data.prs_with_review_loop === 0) return null;

  const convPct = data.convergence_rate != null ? Math.round(data.convergence_rate * 100) : null;
  const convColor = convPct == null ? 'text-neutral-500' : convPct >= 80 ? 'text-emerald-400' : convPct >= 50 ? 'text-yellow-400' : 'text-red-400';
  const escReasons = Object.entries(data.escalation_by_reason).filter(([, c]) => c > 0);

  return (
    <section>
      <div className="flex items-center gap-2 mb-2">
        <p className="text-[10px] text-neutral-500 uppercase tracking-wide">Outer Review↔Fix Convergence</p>
        <span className="text-[10px] text-neutral-600">{data.prs_with_review_loop} PRs · 24h</span>
      </div>

      <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
        <StatCard value={convPct != null ? `${convPct}%` : '—'} label="converged (merged)" color={convColor} />
        <StatCard value={data.iteration_median != null ? String(data.iteration_median) : '—'} label="median cycles" />
        <StatCard value={String(data.churn_pr_count)} label={`PRs w/ churn (${data.churn_total})`} color={data.churn_pr_count > 0 ? 'text-orange-400' : undefined} />
        <StatCard value={String(data.escalation_total)} label="escalations" color={data.escalation_total > 0 ? 'text-red-400' : undefined} />
      </div>

      <div className="mt-2 flex flex-col gap-1">
        <span className="text-[10px] text-neutral-500">cycles-to-merge distribution</span>
        <IterDistBar dist={data.iteration_distribution} />
      </div>

      <div className="mt-2 flex flex-wrap gap-2 items-center text-[10px]">
        <span className="text-emerald-400">merged <span className="font-bold">{data.outcomes.merged}</span></span>
        <span className="text-yellow-400">rejected <span className="font-bold">{data.outcomes.rejected}</span></span>
        <span className="text-red-400">cap-exhausted <span className="font-bold">{data.outcomes.cap_exhausted}</span></span>
        <span className="text-rose-400">repeat-fp halt <span className="font-bold">{data.outcomes.repeat_fingerprint_halt}</span></span>
        {data.drift_pr_count > 0 && <span className="text-orange-300">drift <span className="font-bold">{data.drift_pr_count}</span></span>}
      </div>

      {escReasons.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-1">
          {escReasons.map(([reason, count]) => (
            <span
              key={reason}
              className="inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded border bg-red-500/20 text-red-400 border-red-500/30"
              title={`Escalation: ${reason}`}
            >
              {ESCALATION_LABELS[reason] ?? reason}
              <span className="font-bold">{count}</span>
            </span>
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
        </div>
      )}
    </section>
  );
}
