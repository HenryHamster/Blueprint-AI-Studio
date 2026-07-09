/* eslint-disable react-hooks/set-state-in-effect */
import { useMemo, useState, useEffect, useCallback, useRef } from 'react';
import { parseTimestamp } from '../utils/time';
import { downloadText, escapeCsv } from '../utils/taskCsv';
import type { Task } from './TaskCard';
import { apiFetch, type EfficiencyAxis, type EfficiencyAxesResponse, type EfficiencyHistoryPoint, type FormatComplianceByTypeEntry, type FormatComplianceBySchemaEntry, type ChallengerSampleRateRecommendation, type LlmCostAttributionWindowDelta } from '../api';
import { LaneDiagPanel } from './LaneDiagPanel';
import { WarmStartOriginRow } from './WarmStartOriginRow';
import { usePoller } from '../hooks/usePoller';
import type { WarmStartOriginBreakdown } from '../hooks/useEfficiencySnapshot';
import {
  ResponsiveContainer,
  AreaChart,
  Area,
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ReferenceLine,
  Legend,
} from 'recharts';

type MetricsWindow = '15m' | '30m' | '1h' | '2h' | '3h' | '6h' | '12h' | '24h' | 'all';

interface MetricsPoint {
  timestamp: number;
  activeVMs: number;
  costPerHr: number;
  completedCount: number;
  failedCount: number;
}

interface MetricsDashboardProps {
  metricsHistory: MetricsPoint[];
  cumulativeCost: number;
  cumulativeVmHours: number;
  maxConcurrentVMs: number;
  tasks: Task[];
  metricsWindow: MetricsWindow;
  onWindowChange: (w: MetricsWindow) => void;
  projectId?: string;
}

const WINDOW_MS: Record<MetricsWindow, number> = { '15m': 900000, '30m': 1800000, '1h': 3600000, '2h': 7200000, '3h': 10800000, '6h': 21600000, '12h': 43200000, '24h': 86400000, 'all': Number.MAX_SAFE_INTEGER };

const TICK_STYLE = { fill: '#737373', fontSize: 10 };
const GRID_COLOR = '#262626';
const TOOLTIP_STYLE = {
  contentStyle: { background: '#171717', border: '1px solid #404040', borderRadius: 4 },
  itemStyle: { color: '#f5f5f5' },
  labelStyle: { color: '#a3a3a3' },
};

const formatCostPerHr = (value: unknown): [string, string] => {
  if (value === null || value === undefined) return ['—', 'VM Cost/Hr'];
  return [(value as number).toFixed(2) + ' $/hr', 'VM Cost/Hr'];
};
const formatActiveVMs = (value: unknown): [string, string] => [String(value) + ' VMs', 'Active VMs'];
const formatFailureRate = (value: unknown): [string, string] => {
  if (value === null || value === undefined) return ['—', 'Failure Rate'];
  return [(value as number).toFixed(1) + '%', 'Failure Rate'];
};
const makeLabelFormatter = (windowMs: number) => (ts: unknown) => formatTime(Number(ts), windowMs);
const METRICS_EXPORT_FIELDS = ['timestamp', 'activeVMs', 'costPerHr', 'completedCount', 'failedCount'] as const;

function formatTime(ts: number, windowMs?: number): string {
  const d = new Date(ts);
  if (windowMs !== undefined && windowMs >= 43200000) {
    const month = (d.getMonth() + 1).toString().padStart(2, '0');
    const day = d.getDate().toString().padStart(2, '0');
    const time = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    return `${month}/${day} ${time}`;
  }
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function NoDataMessage() {
  return (
    <div className="flex items-center justify-center" style={{ height: 160 }}>
      <span className="text-neutral-500 text-xs">Collecting data…</span>
    </div>
  );
}

function CostChart({ points, windowMs }: { points: MetricsPoint[]; windowMs: number }) {
  const [now] = useState(Date.now);
  const filtered = useMemo(() => points.filter((p) => p.timestamp >= now - windowMs), [points, now, windowMs]);
  const labelFmt = useMemo(() => makeLabelFormatter(windowMs), [windowMs]);
  if (filtered.length < 2) return <NoDataMessage />;

  return (
    <ResponsiveContainer width="100%" height={160}>
      <AreaChart data={filtered}>
        <CartesianGrid stroke={GRID_COLOR} vertical={false} />
        <XAxis dataKey="timestamp" tickFormatter={(ts) => formatTime(Number(ts), windowMs)} tick={TICK_STYLE} interval="preserveStartEnd" />
        <YAxis tick={TICK_STYLE} />
        <Tooltip
          {...TOOLTIP_STYLE}
          formatter={formatCostPerHr}
          labelFormatter={labelFmt}
        />
        <Area dataKey="costPerHr" stroke="#10b981" fill="#10b981" fillOpacity={0.15} dot={false} />
      </AreaChart>
    </ResponsiveContainer>
  );
}

function VMChart({ points, maxConcurrentVMs, windowMs }: { points: MetricsPoint[]; maxConcurrentVMs: number; windowMs: number }) {
  const [now] = useState(Date.now);
  const filtered = useMemo(() => points.filter((p) => p.timestamp >= now - windowMs), [points, now, windowMs]);
  const labelFmt = useMemo(() => makeLabelFormatter(windowMs), [windowMs]);
  if (filtered.length < 2) return <NoDataMessage />;

  return (
    <ResponsiveContainer width="100%" height={160}>
      <AreaChart data={filtered}>
        <CartesianGrid stroke={GRID_COLOR} vertical={false} />
        <XAxis dataKey="timestamp" tickFormatter={(ts) => formatTime(Number(ts), windowMs)} tick={TICK_STYLE} interval="preserveStartEnd" />
        <YAxis tick={TICK_STYLE} />
        <Tooltip
          {...TOOLTIP_STYLE}
          formatter={formatActiveVMs}
          labelFormatter={labelFmt}
        />
        <ReferenceLine y={maxConcurrentVMs} stroke="#3b82f6" strokeDasharray="4 3" strokeOpacity={0.5} />
        <Area dataKey="activeVMs" stroke="#3b82f6" fill="#3b82f6" fillOpacity={0.15} dot={false} />
      </AreaChart>
    </ResponsiveContainer>
  );
}

function TaskOutcomesChart({ points, tasks, windowMs }: { points: MetricsPoint[]; tasks: Task[]; windowMs: number }) {
  const [now] = useState(Date.now);
  const filtered = useMemo(() => points.filter((p) => p.timestamp >= now - windowMs), [points, now, windowMs]);
  const labelFmt = useMemo(() => makeLabelFormatter(windowMs), [windowMs]);
  const baselineCompleted = useMemo(() => tasks.filter((t) => t.status === 'COMPLETED').length, [tasks]);
  const baselineFailed = useMemo(() => tasks.filter((t) => t.status === 'FAILED').length, [tasks]);
  const totalDeltaCompleted = useMemo(() => filtered.reduce((s, p) => s + p.completedCount, 0), [filtered]);
  const totalDeltaFailed = useMemo(() => filtered.reduce((s, p) => s + p.failedCount, 0), [filtered]);
  const offsetCompleted = useMemo(() => Math.max(0, baselineCompleted - totalDeltaCompleted), [baselineCompleted, totalDeltaCompleted]);
  const offsetFailed = useMemo(() => Math.max(0, baselineFailed - totalDeltaFailed), [baselineFailed, totalDeltaFailed]);
  const cumPoints = useMemo(() => filtered.reduce<{ timestamp: number; cumCompleted: number; cumFailed: number }[]>(
    (acc, p) => {
      const prev = acc[acc.length - 1];
      const cumCompleted = Math.max(0, (prev?.cumCompleted ?? offsetCompleted) + p.completedCount);
      const cumFailed = Math.max(0, (prev?.cumFailed ?? offsetFailed) + p.failedCount);
      acc.push({ timestamp: p.timestamp, cumCompleted, cumFailed });
      return acc;
    },
    [],
  ), [filtered, offsetCompleted, offsetFailed]);
  if (filtered.length < 2) return <NoDataMessage />;

  return (
    <ResponsiveContainer width="100%" height={160}>
      <LineChart data={cumPoints}>
        <CartesianGrid stroke={GRID_COLOR} vertical={false} />
        <XAxis dataKey="timestamp" tickFormatter={(ts) => formatTime(Number(ts), windowMs)} tick={TICK_STYLE} interval="preserveStartEnd" />
        <YAxis tick={TICK_STYLE} />
        <Tooltip
          {...TOOLTIP_STYLE}
          labelFormatter={labelFmt}
        />
        <Legend wrapperStyle={{ fontSize: 11, color: '#a3a3a3' }} />
        <Line dataKey="cumCompleted" stroke="#10b981" dot={false} name="Completed" />
        <Line dataKey="cumFailed" stroke="#ef4444" dot={false} name="Failed" />
      </LineChart>
    </ResponsiveContainer>
  );
}

function FailureRateChart({ points, windowMs }: { points: MetricsPoint[]; windowMs: number }) {
  const [now] = useState(Date.now);
  const { filtered, ratePoints } = useMemo(() => {
    const f = points.filter((p) => p.timestamp >= now - windowMs);
    return {
      filtered: f,
      ratePoints: f.map((p) => {
        const total = p.completedCount + p.failedCount;
        const failureRate = total > 0 ? (p.failedCount / total) * 100 : 0;
        return { timestamp: p.timestamp, failureRate };
      }),
    };
  }, [points, now, windowMs]);
  const labelFmt = useMemo(() => makeLabelFormatter(windowMs), [windowMs]);
  if (filtered.length < 2) return <NoDataMessage />;

  return (
    <ResponsiveContainer width="100%" height={160}>
      <LineChart data={ratePoints}>
        <CartesianGrid stroke={GRID_COLOR} vertical={false} />
        <XAxis dataKey="timestamp" tickFormatter={(ts) => formatTime(Number(ts), windowMs)} tick={TICK_STYLE} interval="preserveStartEnd" />
        <YAxis tick={TICK_STYLE} domain={[0, 100]} tickFormatter={(v) => `${v}%`} />
        <Tooltip
          {...TOOLTIP_STYLE}
          formatter={formatFailureRate}
          labelFormatter={labelFmt}
        />
        <Line dataKey="failureRate" stroke="#ef4444" dot={false} name="Failure Rate" />
      </LineChart>
    </ResponsiveContainer>
  );
}

function getTaskCompletedTime(task: Task): number | null {
  if (task.status !== 'COMPLETED') return null;
  if (task.history && task.history.length > 0) {
    const last = task.history[task.history.length - 1];
    const ts = parseTimestamp(last.timestamp).getTime();
    if (!isNaN(ts)) return ts;
  }
  const ts = parseTimestamp(task.created_at).getTime();
  return isNaN(ts) ? null : ts;
}

function getTaskFailedTime(task: Task): number | null {
  if (task.status !== 'FAILED') return null;
  if (task.history && task.history.length > 0) {
    const last = task.history[task.history.length - 1];
    const ts = parseTimestamp(last.timestamp).getTime();
    if (!isNaN(ts)) return ts;
  }
  const ts = parseTimestamp(task.created_at).getTime();
  return isNaN(ts) ? null : ts;
}

const WINDOW_OPTIONS: Array<'15m' | '30m' | '1h' | '2h' | '3h' | '6h' | '12h' | '24h' | 'all'> = ['15m', '30m', '1h', '2h', '3h', '6h', '12h', '24h', 'all'];

interface EfficiencyAxisDef {
  key: string;
  label: string;
  unit: string;
  favorableDirection: 'lower' | 'higher';
  percent?: boolean;
}

interface EfficiencyAxisLocal extends EfficiencyAxis {
  baseline_seeded_at?: string | null;
  baseline_seed_snapshot_at?: string | null;
}

interface EfficiencyAxesLocal extends EfficiencyAxesResponse {
  axes: Record<string, EfficiencyAxisLocal>;
  vm_cold_start_breakdown?: Record<string, number | null> | null;
  // Per-origin warm-start split (pool/backlog/chain) + pool-origin share,
  // rendered beneath the aggregate vm_reuse_rate axis so the contaminated
  // aggregate is never the sole reuse number. Optional/nullable — older
  // backends omit it; null when there were no warm starts to attribute.
  warm_start_origin_breakdown?: WarmStartOriginBreakdown | null;
}

interface TaskCostBreakdownEntry {
  task_type: string;
  task_count: number;
  estimated_cost_usd: number;
}

interface DirectionCostBreakdownEntry {
  direction_id: string | null;
  direction_text: string | null;
  task_count: number;
  estimated_cost_usd: number;
}

interface TopCostTask {
  task_id: string;
  task_title: string;
  task_type: string;
  direction_id: string | null;
  completed_at: string;
  estimated_cost_usd: number;
}

interface TaskCostBreakdownResponse {
  project_id: string | null;
  window: MetricsWindow;
  window_ms: number | null;
  window_start: string | null;
  task_count: number;
  total_estimated_cost_usd: number;
  task_type_breakdown: TaskCostBreakdownEntry[];
  direction_breakdown: DirectionCostBreakdownEntry[];
  top_tasks: TopCostTask[];
}

interface SpendBurnRateResponse {
  project_id: string | null;
  window_days: number;
  window_cost_usd: number;
  task_count: number;
  daily_spend_usd: number;
  monthly_forecast_usd: number;
}

interface DigestLatencyStats {
  latency_percentiles: {
    p50_seconds: number | null;
    p95_seconds: number | null;
    sample_n: number;
  };
  tier_counts: {
    l1_precompute: number;
    l2_shared_cache: number;
    l3_on_demand: number;
  };
  total_served: number;
  instance_note: string;
}

const COLD_START_STAGE_LABELS: [string, string, boolean][] = [
  ['dispatch_to_slot_ms', 'Queue wait (slot)', true],
  ['dispatch_to_creds_ms', 'Dispatch → creds', false],
  ['creds_to_env_ms', 'Creds → env', false],
  ['env_to_payload_ms', 'Env → payload', false],
  ['payload_to_systemctl_ms', 'Payload → systemctl', false],
  ['systemctl_to_startup_ms', 'Systemctl → startup', false],
  ['startup_to_claim_ms', 'Startup → claim', false],
];

function ColdStartBreakdown({ breakdown }: { breakdown: Record<string, number | null> }) {
  const hasAny = COLD_START_STAGE_LABELS.some(([key]) => breakdown[key] != null);
  if (!hasAny) return null;
  return (
    <div className="mt-2 pt-2 border-t border-neutral-700/50">
      <div className="text-ui-sm text-neutral-500 mb-1 uppercase tracking-wide">Cold-start stage breakdown (p50)</div>
      <div className="grid grid-cols-2 gap-x-4 gap-y-0.5">
        {COLD_START_STAGE_LABELS.map(([key, label, isQueueWait]) => {
          const val = breakdown[key];
          return (
            <div key={key} className="flex justify-between items-center text-ui-sm">
              <span className={isQueueWait ? 'text-amber-400/80' : 'text-neutral-500'}>{label}</span>
              <span className={`font-mono ${isQueueWait ? 'text-amber-400' : 'text-neutral-400'}`}>
                {val != null ? `${val.toFixed(0)}ms` : '—'}
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function formatSeconds(value: number | null): string {
  if (value === null) return '—';
  return `${value.toFixed(2)}s`;
}

function formatShare(count: number, total: number): string {
  if (total === 0) return '—';
  return `${Math.round((count / total) * 100)}%`;
}

function DigestLatencyWidget() {
  const [data, setData] = useState<DigestLatencyStats | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const hasLoadedRef = useRef(false);

  const fetchDigestLatency = useCallback(async (signal: AbortSignal) => {
    if (!hasLoadedRef.current) setLoading(true);
    try {
      const res = await apiFetch('/api/admin/digest-latency-stats', undefined, 2, 30000, signal);
      if (!res.ok) throw new Error(`Failed to fetch digest latency stats: ${res.status}`);
      const json = await res.json() as DigestLatencyStats;
      if (!signal.aborted) {
        setData(json);
        hasLoadedRef.current = true;
        setError(null);
      }
    } catch (err) {
      if (signal.aborted) return;
      setError(err instanceof Error ? err.message : 'Unknown error');
    } finally {
      if (!signal.aborted) setLoading(false);
    }
  }, []);

  usePoller(fetchDigestLatency, { intervalMs: 60_000, deps: [fetchDigestLatency] });

  if (loading && !data) {
    return <div className="flex items-center justify-center h-20 text-neutral-500 text-xs">Loading digest latency...</div>;
  }

  if (error) {
    return <div className="flex items-center justify-center h-20 text-red-400 text-xs">{error}</div>;
  }

  if (!data) {
    return <div className="flex items-center justify-center h-20 text-neutral-500 text-xs">No digest latency samples yet</div>;
  }

  const tiers = [
    { key: 'l1_precompute', label: 'L1 precompute', count: data.tier_counts.l1_precompute },
    { key: 'l2_shared_cache', label: 'L2 shared cache', count: data.tier_counts.l2_shared_cache },
    { key: 'l3_on_demand', label: 'L3 on-demand', count: data.tier_counts.l3_on_demand },
  ] as const;

  return (
    <div className="space-y-2 text-xs">
      <div className="grid grid-cols-3 gap-2">
        <div className="rounded bg-neutral-800/50 px-3 py-2">
          <div className="text-ui-sm uppercase tracking-wide text-neutral-500">p50</div>
          <div className="font-mono text-neutral-100">{formatSeconds(data.latency_percentiles.p50_seconds)}</div>
        </div>
        <div className="rounded bg-neutral-800/50 px-3 py-2">
          <div className="text-ui-sm uppercase tracking-wide text-neutral-500">p95</div>
          <div className="font-mono text-neutral-100">{formatSeconds(data.latency_percentiles.p95_seconds)}</div>
        </div>
        <div className="rounded bg-neutral-800/50 px-3 py-2">
          <div className="text-ui-sm uppercase tracking-wide text-neutral-500">Samples</div>
          <div className="font-mono text-neutral-100">{data.latency_percentiles.sample_n}</div>
        </div>
      </div>
      <div className="rounded bg-neutral-800/50 px-3 py-2">
        <div className="mb-1 text-ui-sm uppercase tracking-wide text-neutral-500">Serve mix</div>
        <div className="space-y-1">
          {tiers.map((tier) => (
            <div key={tier.key} className="flex items-center justify-between gap-3">
              <span className="text-neutral-300">{tier.label}</span>
              <span className="font-mono text-neutral-400 tabular-nums">
                {tier.count} | {formatShare(tier.count, data.total_served)}
              </span>
            </div>
          ))}
        </div>
      </div>
      <div data-testid="digest-latency-caveat" className="text-ui-sm text-amber-400/80">
        {data.instance_note}
      </div>
    </div>
  );
}

function AxisSparkline({ history, favorableDirection }: {
  history: EfficiencyHistoryPoint[];
  favorableDirection: 'lower' | 'higher';
}) {
  // Track index positions where the metric definition version changes so we
  // can render a vertical boundary in the sparkline at those discontinuities.
  const defVersionBoundaries: number[] = [];
  let prevVersion: string | null | undefined = undefined;
  const points: Array<{ v: number; defChanged?: boolean }> = [];
  for (const h of history) {
    if (h.value === null) continue;
    const changed = prevVersion !== undefined && h.def_version !== prevVersion;
    points.push({ v: h.value, defChanged: changed });
    if (changed) defVersionBoundaries.push(points.length - 1);
    prevVersion = h.def_version;
  }
  if (points.length < 2) return null;
  const first = points[0].v;
  const last = points[points.length - 1].v;
  const improved = favorableDirection === 'lower' ? last <= first : last >= first;
  const color = improved ? '#10b981' : '#ef4444';
  return (
    <div style={{ height: 24 }} className="mt-1">
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={points} margin={{ top: 2, right: 0, left: 0, bottom: 2 }}>
          <Line dataKey="v" stroke={color} dot={false} strokeWidth={1.5} isAnimationActive={false} />
          {defVersionBoundaries.map(idx => (
            <ReferenceLine key={idx} x={idx} stroke="#f59e0b" strokeDasharray="3 2" strokeWidth={1} strokeOpacity={0.8} />
          ))}
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

const EFFICIENCY_AXES: EfficiencyAxisDef[] = [
  { key: 'cost_per_merged_pr', label: 'Cost per autonomous merge', unit: '$', favorableDirection: 'lower' },
  { key: 'vm_cold_start_p50_ms', label: 'VM cold-start p50', unit: 'ms', favorableDirection: 'lower' },
  { key: 'vm_reuse_rate', label: 'VM reuse rate', unit: '%', favorableDirection: 'higher' },
  { key: 'median_api_calls_per_task', label: 'Median API calls per task', unit: '', favorableDirection: 'lower' },
  { key: 'median_review_iterations', label: 'Median review iterations', unit: '', favorableDirection: 'lower' },
  { key: 'llm_cost_per_merged_pr', label: 'LLM token cost per merged PR', unit: '$', favorableDirection: 'lower' },
  { key: 'cost_per_verified_task', label: 'Cost per verified task', unit: '$', favorableDirection: 'lower' },
  { key: 'llm_cost_per_verified_task', label: 'LLM cost per verified task', unit: '$', favorableDirection: 'lower' },
  { key: 'total_cost_per_verified_task', label: 'Total cost per verified task', unit: '$', favorableDirection: 'lower' },
  { key: 'llm_cache_hit_rate', label: 'LLM cache hit rate', unit: '%', favorableDirection: 'higher', percent: true },
  { key: 'rework_cost_share', label: 'Rework cost share', unit: '%', favorableDirection: 'lower', percent: true },
  { key: 'lane_cost_per_merged_pr', label: 'Lane cost per merged PR', unit: '$', favorableDirection: 'lower' },
  { key: 'lane_warm_resume_rate', label: 'Lane warm resume rate', unit: '', favorableDirection: 'higher' },
  { key: 'checkpoint_loss_rate', label: 'Checkpoint loss rate', unit: '', favorableDirection: 'lower' },
  { key: 'prompt_static_bytes', label: 'Static prompt size', unit: ' B', favorableDirection: 'lower' },
];

function getDriftColor(driftPct: number | null, favorableDirection: 'lower' | 'higher'): string {
  if (driftPct === null) return 'text-neutral-500';
  const isImproving = (favorableDirection === 'lower' && driftPct < 0) || (favorableDirection === 'higher' && driftPct > 0);
  return isImproving ? 'text-emerald-400' : 'text-red-400';
}

function formatAxisDisplay(axis: Pick<EfficiencyAxis, 'current' | 'baseline' | 'drift_pct'>, unit: string, favorableDirection: 'lower' | 'higher', percent = false) {
  const driftColor = getDriftColor(axis.drift_pct, favorableDirection);
  const driftArrow = axis.drift_pct === null ? '—' : (axis.drift_pct < 0 ? '↓' : '↑');
  const toDisplayValue = (value: number | null) => {
    if (value === null) return '—';
    const numeric = percent ? value * 100 : value;
    return `${numeric.toFixed(1)}${unit}`;
  };
  return {
    current: toDisplayValue(axis.current),
    baseline: toDisplayValue(axis.baseline),
    driftColor,
    driftDisplay: axis.drift_pct === null ? '—' : `${driftArrow} ${Math.abs(axis.drift_pct).toFixed(1)}%`,
  };
}

function NestedAxisLine({ label, axis, unit, favorableDirection, percent = false }: { label: string; axis: Pick<EfficiencyAxis, 'current' | 'baseline' | 'drift_pct'>; unit: string; favorableDirection: 'lower' | 'higher'; percent?: boolean; }) {
  const { current, baseline, driftColor, driftDisplay } = formatAxisDisplay(axis, unit, favorableDirection, percent);
  return (
    <div className="flex items-center justify-between gap-3 text-ui-sm">
      <span className="text-neutral-500">{label}</span>
      <div className="flex items-center gap-3 text-neutral-300 font-mono">
        <span>{current}</span>
        <span className="text-neutral-500">baseline: {baseline}</span>
        <span className={`${driftColor} min-w-12 text-right`}>{driftDisplay}</span>
      </div>
    </div>
  );
}

function ChallengerSampleRateRow({ rec }: { rec: ChallengerSampleRateRecommendation }) {
  const statusColor = rec.status === 'reduce' ? 'text-amber-400' : rec.status === 'gate_earning' ? 'text-emerald-400' : 'text-neutral-500';
  const rateLabel = rec.recommended_sample_rate != null ? rec.recommended_sample_rate.toFixed(2) : '—';
  return (
    <div data-testid="challenger-sample-rate-recommendation" className="mt-1.5 text-ui-sm border-t border-neutral-700/50 pt-1.5">
      <div className="flex items-center justify-between gap-2">
        <span className="text-neutral-400">recommended sample_rate</span>
        <span className={`font-mono font-semibold ${statusColor}`}>{rateLabel}</span>
      </div>
      <p className={`mt-0.5 ${statusColor} leading-tight`}>{rec.rationale}</p>
    </div>
  );
}

function EfficiencyWidget({ projectId }: { projectId: string }) {
  const [data, setData] = useState<EfficiencyAxesLocal | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let mounted = true;
    (async () => {
      try {
        const result = await apiFetch(`/api/admin/projects/${encodeURIComponent(projectId)}/efficiency-axes`);
        if (!result.ok) throw new Error(`Failed to fetch efficiency data: ${result.status}`);
        if (mounted) {
          setData(await result.json());
          setError(null);
        }
      } catch (err) {
        if (mounted) {
          setError(err instanceof Error ? err.message : 'Unknown error');
        }
      } finally {
        if (mounted) setLoading(false);
      }
    })();
    return () => { mounted = false; };
  }, [projectId]);

  if (loading) {
    return <div className="flex items-center justify-center h-24 text-neutral-500 text-xs">Loading efficiency data…</div>;
  }

  if (error) {
    return <div className="flex items-center justify-center h-24 text-red-400 text-xs">{error}</div>;
  }

  if (!data || !data.axes || Object.keys(data.axes).length === 0) {
    return <div className="flex items-center justify-center h-24 text-neutral-500 text-xs">Baseline pending (waiting for first weekly snapshot)</div>;
  }

  const hasBaseline = Object.values(data.axes).some(axis => axis.baseline !== null);
  if (!hasBaseline) {
    return <div className="flex items-center justify-center h-24 text-neutral-500 text-xs">Baseline pending (waiting for first weekly snapshot)</div>;
  }

  return (
    <div className="space-y-2">
      {EFFICIENCY_AXES.map((axisDef) => {
        const axisData = data.axes[axisDef.key];
        if (!axisData) return null;
        const { current, baseline } = axisData;
        const { driftColor, driftDisplay } = formatAxisDisplay(axisData, axisDef.unit, axisDef.favorableDirection);
        const { baseline_seeded_at, baseline_seed_snapshot_at } = axisData;
        const reseedDate = baseline_seeded_at && baseline_seeded_at !== data.baseline_captured_at
          ? baseline_seeded_at
          : null;
        return (
          <div key={axisDef.key} className="py-1.5 px-3 rounded bg-neutral-800/50 text-xs">
            <div className="flex justify-between items-center">
              <span className="text-neutral-400">{axisDef.label}</span>
              <div className="flex gap-4 items-center text-neutral-300 font-mono">
                <span>{current !== null ? `${(axisDef.percent ? current * 100 : current).toFixed(1)}${axisDef.unit}` : '—'}</span>
                <span className="text-neutral-500">baseline: {baseline !== null ? `${(axisDef.percent ? baseline * 100 : baseline).toFixed(1)}${axisDef.unit}` : '—'}</span>
                <span className={`${driftColor} min-w-12 text-right`}>{driftDisplay}</span>
              </div>
            </div>
            {reseedDate && (
              <div data-testid={`axis-reseed-provenance-${axisDef.key}`} className="mt-0.5 text-ui-sm text-neutral-500">
                baseline re-seeded {new Date(reseedDate).toLocaleDateString()}
                {baseline_seed_snapshot_at && (
                  <span className="ml-1">(snapshot: {new Date(baseline_seed_snapshot_at).toLocaleDateString()})</span>
                )}
              </div>
            )}
            {axisData.history && axisData.history.length >= 2 && (
              <AxisSparkline history={axisData.history} favorableDirection={axisDef.favorableDirection} />
            )}
            {axisDef.key === 'vm_cold_start_p50_ms' && data.vm_cold_start_breakdown && (
              <ColdStartBreakdown breakdown={data.vm_cold_start_breakdown} />
            )}
            {axisDef.key === 'vm_reuse_rate' && data.warm_start_origin_breakdown && (
              <div className="mt-2 pt-2 border-t border-neutral-700/50 text-ui-sm">
                <div data-testid="vm-reuse-aggregate-caveat" className="text-amber-400/80 mb-1">
                  Aggregate conflates warm-pool, backlog-reuse, and chain-continuity starts — read the per-origin split (esp. pool share) below, not the headline rate.
                </div>
                <WarmStartOriginRow breakdown={data.warm_start_origin_breakdown} />
              </div>
            )}
          </div>
        );
      })}
      {data.panel_review_effectiveness && (
        <div data-testid="panel-review-effectiveness-block" className="py-1.5 px-3 rounded bg-neutral-800/50 text-xs">
          <div className="flex justify-between items-center gap-4">
            <span className="text-neutral-400">Panel review effectiveness</span>
            <div className="space-y-1 min-w-0">
              <NestedAxisLine
                label="overturn_rate"
                axis={{
                  current: data.panel_review_effectiveness.overturn_rate,
                  baseline: null,
                  drift_pct: null,
                }}
                unit="%"
                favorableDirection="higher"
                percent
              />
            </div>
          </div>
          <div className="mt-1 text-ui-sm text-neutral-500 flex flex-wrap gap-x-3 gap-y-1">
            <span>challenged confirmed: {data.panel_review_effectiveness.challenged_confirmed ?? '—'}</span>
            <span>revised: {data.panel_review_effectiveness.challenged_revised ?? '—'}</span>
            <span>rejected: {data.panel_review_effectiveness.challenged_rejected ?? '—'}</span>
            <span>total: {data.panel_review_effectiveness.total_challenged ?? '—'}</span>
            {data.panel_review_effectiveness.cost_per_challenge_usd != null && (
              <span>cost/challenge: ${data.panel_review_effectiveness.cost_per_challenge_usd.toFixed(4)}</span>
            )}
          </div>
          {data.panel_review_recommended_sample_rate && (
            <ChallengerSampleRateRow rec={data.panel_review_recommended_sample_rate} />
          )}
        </div>
      )}
      {data.vm_cost_attribution && data.vm_cost_attribution.by_task_type.length > 0 && (
        <div data-testid="vm-cost-attribution-block" className="py-1.5 px-3 rounded bg-neutral-800/50 text-xs">
          <div className="flex justify-between items-center gap-4 mb-2">
            <span className="text-neutral-400">VM cost by task type</span>
            <span className="text-neutral-500 font-mono">${data.vm_cost_attribution.total_vm_usd.toFixed(2)} total</span>
          </div>
          {data.vm_cost_attribution_trend && data.vm_cost_attribution_trend.length >= 2 && (() => {
            const trendPoints = data.vm_cost_attribution_trend
              .filter((e): e is typeof e & { total_vm_usd: number } => e.total_vm_usd != null)
              .map(e => ({ v: e.total_vm_usd }));
            if (trendPoints.length < 2) return null;
            const first = trendPoints[0].v;
            const last = trendPoints[trendPoints.length - 1].v;
            const color = last <= first ? '#10b981' : '#ef4444';
            return (
              <div style={{ height: 24 }} className="mb-2">
                <ResponsiveContainer width="100%" height="100%">
                  <AreaChart data={trendPoints} margin={{ top: 2, right: 0, left: 0, bottom: 2 }}>
                    <Area dataKey="v" stroke={color} fill={color} fillOpacity={0.15} dot={false} strokeWidth={1.5} isAnimationActive={false} />
                  </AreaChart>
                </ResponsiveContainer>
              </div>
            );
          })()}
          <div className="space-y-1">
            {data.vm_cost_attribution.by_task_type.map((row) => (
              <div key={row.task_type} className="flex items-center justify-between gap-3 text-ui-sm">
                <span className="text-neutral-500">{row.task_type}</span>
                <div className="flex items-center gap-2 text-neutral-400 font-mono">
                  <span>${row.total_cost_usd.toFixed(2)}</span>
                  <span className="text-neutral-600">· {row.task_count}</span>
                  <span className="text-neutral-600">· {row.merged_count}↓</span>
                  {row.cost_per_merge != null && (
                    <span className="text-neutral-600">· ${row.cost_per_merge.toFixed(4)}/merge</span>
                  )}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
      {data.llm_cost_attribution && data.llm_cost_attribution.by_failure_class.length > 0 && (
        <div data-testid="llm-cost-by-outcome-block" className="py-1.5 px-3 rounded bg-neutral-800/50 text-xs">
          <div className="flex justify-between items-center gap-4 mb-2">
            <span className="text-neutral-400">LLM cost by terminal outcome</span>
            <span className="text-neutral-500 font-mono">${data.llm_cost_attribution.total_llm_usd.toFixed(2)} total</span>
          </div>
          <div className="space-y-1">
            {data.llm_cost_attribution.by_failure_class.map((row) => (
              <div key={row.failure_class} className="flex items-center justify-between gap-3 text-ui-sm">
                <span className={row.failure_class === 'MERGED' ? 'text-emerald-400/80' : 'text-neutral-500'}>{row.failure_class}</span>
                <div className="flex items-center gap-2 font-mono">
                  <span className={row.failure_class === 'MERGED' ? 'text-emerald-400' : 'text-neutral-400'}>${row.total_cost_usd.toFixed(2)}</span>
                  <span className="text-neutral-600">· {row.task_count}</span>
                </div>
              </div>
            ))}
          </div>
          {(() => {
            const attr = data.llm_cost_attribution;
            const dominantModel = attr.by_model?.length
              ? [...attr.by_model].sort((a, b) => b.cost_usd - a.cost_usd)[0]
              : null;
            const dominantTaskType = attr.by_task_type?.length
              ? [...attr.by_task_type].sort((a, b) => b.cost_usd - a.cost_usd)[0]
              : null;
            const hasDecomp = dominantModel != null || dominantTaskType != null
              || attr.rework_share != null || attr.self_repair_burn_share != null;
            if (!hasDecomp) return null;
            return (
              <div data-testid="llm-cost-decomposition" className="mt-2 pt-2 border-t border-neutral-700/50 text-ui-sm text-neutral-500 flex flex-wrap gap-x-3 gap-y-1">
                {dominantModel && (
                  <span>top model: <span className="text-neutral-400 font-mono">{dominantModel.model}</span> ${dominantModel.cost_usd.toFixed(2)}</span>
                )}
                {dominantTaskType && (
                  <span>top type: <span className="text-neutral-400 font-mono">{dominantTaskType.task_type}</span> ${dominantTaskType.cost_usd.toFixed(2)}</span>
                )}
                <span>rework: {attr.rework_share != null ? `${(attr.rework_share * 100).toFixed(0)}%` : '—'}</span>
                <span>self-repair burn: {attr.self_repair_burn_share != null ? `${(attr.self_repair_burn_share * 100).toFixed(0)}%` : '—'}</span>
              </div>
            );
          })()}
        </div>
      )}
      {data.llm_cost_attribution_window_delta && (() => {
        const wd: LlmCostAttributionWindowDelta = data.llm_cost_attribution_window_delta;
        const meta = wd.window_meta;
        const topModel = wd.by_model_delta?.length ? wd.by_model_delta[0] : null;
        const topType = wd.by_task_type_delta?.length ? wd.by_task_type_delta[0] : null;
        const totalDelta = wd.total_llm_usd;
        const reworkDelta = wd.rework_share;
        const repairDelta = wd.self_repair_burn_share;
        const hasContent = topModel != null || topType != null || totalDelta != null || reworkDelta != null;
        if (!hasContent) return null;
        const fmtPct = (v: number | null | undefined) => v != null ? `${v > 0 ? '+' : ''}${(v * 100).toFixed(1)}%` : '—';
        const fmtUsd = (v: number | null | undefined) => v != null ? `${v >= 0 ? '+' : ''}$${Math.abs(v).toFixed(2)}` : '—';
        const windowLabel = meta
          ? `recent ${meta.recent_n} vs prior ${meta.prior_count}`
          : 'recent vs prior';
        return (
          <div data-testid="llm-cost-window-delta-block" className="py-1.5 px-3 rounded bg-neutral-800/50 text-xs">
            <div className="flex justify-between items-center gap-4 mb-1.5">
              <span className="text-neutral-400">LLM cost-mix shift <span className="text-neutral-600">({windowLabel} snapshots)</span></span>
              {totalDelta?.abs_delta != null && (
                <span className={`font-mono ${totalDelta.abs_delta > 0 ? 'text-amber-400' : 'text-emerald-400'}`}>
                  {fmtUsd(totalDelta.abs_delta)} avg/snapshot
                </span>
              )}
            </div>
            <div className="text-ui-sm text-neutral-500 flex flex-wrap gap-x-3 gap-y-1">
              {topModel && (
                <span>top model mover: <span className="text-neutral-400 font-mono">{topModel.model}</span> {fmtUsd(topModel.cost_delta_usd)}{topModel.share_delta != null ? ` (${fmtPct(topModel.share_delta)} share)` : ''}</span>
              )}
              {topType && (
                <span>top type mover: <span className="text-neutral-400 font-mono">{topType.task_type}</span> {fmtUsd(topType.cost_delta_usd)}</span>
              )}
              {reworkDelta?.abs_delta != null && (
                <span>rework: {fmtPct(reworkDelta.abs_delta)}</span>
              )}
              {repairDelta?.abs_delta != null && (
                <span>self-repair burn: {fmtPct(repairDelta.abs_delta)}</span>
              )}
            </div>
          </div>
        );
      })()}
      {data.format_compliance && (
        <div data-testid="format-compliance-block" className="py-1.5 px-3 rounded bg-neutral-800/50 text-xs">
          <div className="flex justify-between items-center gap-4">
            <span className="text-neutral-400">Format compliance</span>
            <div className="space-y-1 min-w-0">
              <NestedAxisLine
                label="format_compliance_rate"
                axis={data.format_compliance.format_compliance_rate}
                unit="%"
                favorableDirection="higher"
                percent
              />
              <NestedAxisLine
                label="self_repair_success_rate"
                axis={data.format_compliance.self_repair_success_rate}
                unit="%"
                favorableDirection="higher"
                percent
              />
            </div>
          </div>
          <div className="mt-1 text-ui-sm text-neutral-500 flex flex-wrap gap-x-3 gap-y-1">
            <span>tasks measured: {data.format_compliance.tasks_measured ?? '—'}</span>
            <span>well formed: {data.format_compliance.well_formed_tasks ?? '—'}</span>
            <span>format errors: {data.format_compliance.format_error_total ?? '—'}</span>
            <span>repairs: {data.format_compliance.repair_attempts ?? '—'}</span>
            <span>successes: {data.format_compliance.repair_successes ?? '—'}</span>
          </div>
          {data.format_compliance_by_type && Object.keys(data.format_compliance_by_type).length > 0 && (
            <div data-testid="format-compliance-by-type-block" className="mt-2 pt-2 border-t border-neutral-700/50">
              <div className="text-ui-sm text-neutral-500 mb-1 uppercase tracking-wide">By task type</div>
              <div className="space-y-0.5">
                {Object.entries(data.format_compliance_by_type).sort(([a], [b]) => a.localeCompare(b)).map(([taskType, entry]: [string, FormatComplianceByTypeEntry]) => (
                  <div key={taskType} className="flex items-center justify-between gap-3 text-ui-sm">
                    <span className="text-neutral-500">{taskType}</span>
                    <div className="flex items-center gap-2 text-neutral-400 font-mono">
                      <span>{entry.format_compliance_rate != null ? `${(entry.format_compliance_rate * 100).toFixed(0)}%` : '—'}</span>
                      <span className="text-neutral-600">· {entry.tasks_measured ?? '—'} tasks</span>
                      {(entry.format_error_total ?? 0) > 0 && (
                        <span className="text-red-400/70">· {entry.format_error_total} err</span>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}
          {data.format_compliance?.by_schema && Object.keys(data.format_compliance.by_schema).length > 0 && (
            <div data-testid="format-compliance-by-schema-block" className="mt-2 pt-2 border-t border-neutral-700/50">
              <div className="text-ui-sm text-neutral-500 mb-1 uppercase tracking-wide">By schema</div>
              <div className="space-y-0.5">
                {Object.entries(data.format_compliance.by_schema).sort(([, a]: [string, FormatComplianceBySchemaEntry], [, b]: [string, FormatComplianceBySchemaEntry]) => (b.format_miss ?? 0) - (a.format_miss ?? 0)).map(([schema, entry]: [string, FormatComplianceBySchemaEntry]) => (
                  <div key={schema} className="flex items-center justify-between gap-3 text-ui-sm">
                    <span className="text-neutral-500">{schema}</span>
                    <div className="flex items-center gap-2 text-neutral-400 font-mono">
                      {entry.format_miss > 0 && (
                        <span className="text-red-400/70">{entry.format_miss} miss</span>
                      )}
                      <span className="text-neutral-600">· {entry.repair_attempts} repair</span>
                      {entry.self_repair_success_rate != null && (
                        <span>{(entry.self_repair_success_rate * 100).toFixed(0)}% ok</span>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
      {data.agent_timeout_rate && (
        <div data-testid="agent-timeout-rate-block" className="py-1.5 px-3 rounded bg-neutral-800/50 text-xs">
          <div className="flex justify-between items-center gap-4">
            <span className="text-neutral-400">Agent timeout rate</span>
            <div className="space-y-1 min-w-0">
              <NestedAxisLine
                label="overall"
                axis={data.agent_timeout_rate.overall}
                unit="%"
                favorableDirection="lower"
                percent
              />
              <div className="text-ui-sm text-neutral-500 text-right font-mono">
                dominant subtype: {data.agent_timeout_rate.dominant_subtype ?? '—'}
              </div>
            </div>
          </div>
          <div className="mt-1 text-ui-sm text-neutral-500 flex flex-wrap gap-x-3 gap-y-1">
            <span>share: {data.agent_timeout_rate.dominant_share != null ? `${(data.agent_timeout_rate.dominant_share * 100).toFixed(0)}%` : '—'}</span>
            <span>timeouts: {data.agent_timeout_rate.timeout_count ?? '—'}</span>
            <span>denominator: {data.agent_timeout_rate.denominator ?? '—'}</span>
          </div>
        </div>
      )}
    </div>
  );
}

function SpendBurnRateWidget({ projectId }: { projectId?: string }) {
  const [data, setData] = useState<SpendBurnRateResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let mounted = true;
    setLoading(true);
    setError(null);
    setData(null);

    (async () => {
      try {
        const params = new URLSearchParams({ window_days: '7' });
        if (projectId) params.set('project_id', projectId);
        const result = await apiFetch(`/api/admin/spend-burn-rate?${params}`);
        if (!result.ok) throw new Error(`Failed to fetch spend burn rate: ${result.status}`);
        if (mounted) setData(await result.json());
      } catch (err) {
        if (mounted) setError(err instanceof Error ? err.message : 'Unknown error');
      } finally {
        if (mounted) setLoading(false);
      }
    })();

    return () => { mounted = false; };
  }, [projectId]);

  if (loading) return <div className="flex items-center justify-center h-16 text-neutral-500 text-xs">Loading burn rate…</div>;
  if (error) return <div className="flex items-center justify-center h-16 text-red-400 text-xs">{error}</div>;
  if (!data) return null;

  return (
    <div data-testid="spend-burn-rate-widget" className="flex flex-wrap gap-6 items-end">
      <div>
        <div className="text-ui-sm text-neutral-500 uppercase tracking-wide mb-1">Daily spend (7d avg)</div>
        <div className="text-2xl font-mono text-neutral-100">${data.daily_spend_usd.toFixed(2)}<span className="text-sm text-neutral-400">/day</span></div>
      </div>
      <div>
        <div className="text-ui-sm text-neutral-500 uppercase tracking-wide mb-1">Monthly forecast</div>
        <div className="text-2xl font-mono text-emerald-400">${data.monthly_forecast_usd.toFixed(2)}<span className="text-sm text-neutral-400">/mo</span></div>
      </div>
      <div className="text-ui-sm text-neutral-500 font-mono ml-auto self-end">
        {data.task_count} tasks · ${data.window_cost_usd.toFixed(2)} over {data.window_days}d
      </div>
    </div>
  );
}

function TaskCostBreakdownWidget({ projectId, window }: { projectId: string; window: MetricsWindow }) {
  const [data, setData] = useState<TaskCostBreakdownResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let mounted = true;
    setLoading(true);
    setError(null);
    setData(null);

    (async () => {
      try {
        const params = new URLSearchParams({
          project_id: projectId,
          window,
          top_n: '5',
        });
        const result = await apiFetch(`/api/admin/task-cost-breakdown?${params}`);
        if (!result.ok) throw new Error(`Failed to fetch task cost breakdown: ${result.status}`);
        if (mounted) {
          setData(await result.json());
        }
      } catch (err) {
        if (mounted) {
          setError(err instanceof Error ? err.message : 'Unknown error');
        }
      } finally {
        if (mounted) setLoading(false);
      }
    })();

    return () => { mounted = false; };
  }, [projectId, window]);

  if (loading) {
    return <div className="flex items-center justify-center h-24 text-neutral-500 text-xs">Loading spend breakdown…</div>;
  }

  if (error) {
    return <div className="flex items-center justify-center h-24 text-red-400 text-xs">{error}</div>;
  }

  if (!data || data.task_count === 0) {
    return <div className="flex items-center justify-center h-24 text-neutral-500 text-xs">No completed cost data in this window</div>;
  }

  const sectionCard = 'rounded-lg bg-neutral-800/50 border border-neutral-700/50 p-3';

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="text-ui-md text-neutral-400 uppercase tracking-wide">Selected project spend</div>
        <div className="text-xs font-mono text-neutral-300">${data.total_estimated_cost_usd.toFixed(2)} across {data.task_count} tasks</div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
        <div className={sectionCard}>
          <div className="text-ui-sm text-neutral-500 uppercase tracking-wide mb-2">By task type</div>
          <div className="space-y-1">
            {data.task_type_breakdown.map((row) => (
              <div key={row.task_type} className="flex items-center justify-between gap-3 text-xs min-w-0">
                <span className="text-neutral-300 break-all min-w-0">{row.task_type}</span>
                <span className="text-neutral-400 font-mono shrink-0">${row.estimated_cost_usd.toFixed(2)} · {row.task_count}</span>
              </div>
            ))}
          </div>
        </div>

        <div className={sectionCard}>
          <div className="text-ui-sm text-neutral-500 uppercase tracking-wide mb-2">By research direction</div>
          <div className="space-y-1">
            {data.direction_breakdown.map((row) => (
              <div key={row.direction_id ?? 'unassigned'} className="flex items-center justify-between gap-3 text-xs min-w-0">
                <span className="text-neutral-300 break-all min-w-0">{row.direction_text ?? row.direction_id ?? 'No direction'}</span>
                <span className="text-neutral-400 font-mono shrink-0">${row.estimated_cost_usd.toFixed(2)} · {row.task_count}</span>
              </div>
            ))}
          </div>
        </div>
      </div>

      <div className={sectionCard}>
        <div className="text-ui-sm text-neutral-500 uppercase tracking-wide mb-2">Top expensive tasks</div>
        <div className="space-y-2">
          {data.top_tasks.map((row) => (
            <div key={row.task_id} className="grid grid-cols-[minmax(0,1fr)_auto] gap-3 items-start text-xs">
              <div className="min-w-0">
                <div className="text-neutral-200 break-all">{row.task_title}</div>
                <div className="mt-0.5 text-ui-sm text-neutral-500 break-all">
                  <span className="font-mono">{row.task_type}</span>
                  {' · '}
                  <span className="font-mono">{row.direction_id ?? 'No direction'}</span>
                  {' · '}
                  {formatTime(parseTimestamp(row.completed_at).getTime(), window === 'all' ? WINDOW_MS['24h'] : WINDOW_MS[window])}
                </div>
              </div>
              <div className="font-mono text-neutral-300 whitespace-nowrap">${row.estimated_cost_usd.toFixed(2)}</div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

export default function MetricsDashboard({
  metricsHistory,
  cumulativeCost,
  cumulativeVmHours,
  maxConcurrentVMs,
  tasks,
  metricsWindow,
  onWindowChange,
  projectId,
}: MetricsDashboardProps) {
  const windowMs = WINDOW_MS[metricsWindow];
  const [now] = useState(Date.now);

  const completedTasks = useMemo(
    () =>
      tasks
        .map((t) => ({ task: t, completedAt: getTaskCompletedTime(t) }))
        .filter((t): t is { task: Task; completedAt: number } => t.completedAt !== null),
    [tasks],
  );

  const { count1h, count6h, count24h } = useMemo(
    () => ({
      count1h: completedTasks.filter((t) => now - t.completedAt <= 3_600_000).length,
      count6h: completedTasks.filter((t) => now - t.completedAt <= 21_600_000).length,
      count24h: completedTasks.filter((t) => now - t.completedAt <= 86_400_000).length,
    }),
    [completedTasks, now],
  );

  const failedTasks = useMemo(
    () =>
      tasks
        .map((t) => ({ task: t, failedAt: getTaskFailedTime(t) }))
        .filter((t): t is { task: Task; failedAt: number } => t.failedAt !== null),
    [tasks],
  );

  const { failed1h, failed6h, failed24h } = useMemo(
    () => ({
      failed1h: failedTasks.filter((t) => now - t.failedAt <= 3_600_000).length,
      failed6h: failedTasks.filter((t) => now - t.failedAt <= 21_600_000).length,
      failed24h: failedTasks.filter((t) => now - t.failedAt <= 86_400_000).length,
    }),
    [failedTasks, now],
  );

  function successRate(completed: number, failed: number): string {
    const total = completed + failed;
    if (total === 0) return '—';
    return ((completed / total) * 100).toFixed(1) + '%';
  }

  function successRateColor(completed: number, failed: number): string {
    const total = completed + failed;
    if (total === 0) return 'text-neutral-500';
    const pct = (completed / total) * 100;
    if (pct >= 90) return 'text-emerald-400';
    if (pct >= 70) return 'text-amber-400';
    return 'text-red-400';
  }

  const cardClass = 'bg-neutral-900/50 border border-neutral-800/50 rounded-xl p-4';
  const filteredMetrics = useMemo(() => metricsHistory.filter((p) => p.timestamp >= now - windowMs), [metricsHistory, now, windowMs]);
  const exportMetrics = (format: 'csv' | 'json') => {
    const rows = filteredMetrics.map((p) => ({
      timestamp: new Date(p.timestamp).toISOString(),
      activeVMs: p.activeVMs,
      costPerHr: p.costPerHr,
      completedCount: p.completedCount,
      failedCount: p.failedCount,
    }));
    const stamp = new Date(now).toISOString().replace(/[:.]/g, '-');
    if (format === 'json') {
      downloadText(`metrics-${metricsWindow}-${stamp}.json`, 'application/json', JSON.stringify(rows, null, 2));
      return;
    }
    const csv = [METRICS_EXPORT_FIELDS.join(','), ...rows.map((row) => METRICS_EXPORT_FIELDS.map((field) => escapeCsv(row[field])).join(','))].join('\n');
    downloadText(`metrics-${metricsWindow}-${stamp}.csv`, 'text/csv', csv);
  };

  return (
    <div className="mb-6">
      {/* Window selector */}
      <div className="flex flex-wrap items-center justify-between gap-2 mb-3">
        <div className="flex flex-wrap items-center gap-1">
        {WINDOW_OPTIONS.map((w) => (
          // reason: time-window filter pill in compact metrics toolbar; py-1 is operator-density convention
          <button
            data-viewport-allow-small-target
            key={w}
            onClick={() => onWindowChange(w)}
            className={`px-3 py-1 rounded-full text-xs font-medium border transition-colors ${
              metricsWindow === w
                ? 'bg-neutral-700 border-neutral-600 text-neutral-100'
                : 'bg-neutral-900/50 border-neutral-800/50 text-neutral-400 hover:border-neutral-700 hover:text-neutral-300'
            }`}
          >
            {w}
          </button>
        ))}
        </div>
        <div className="flex items-center gap-1">
          <button data-viewport-allow-small-target onClick={() => exportMetrics('csv')} className="px-3 py-1 rounded-full text-xs font-medium border bg-neutral-900/50 border-neutral-800/50 text-neutral-400 hover:border-neutral-700 hover:text-neutral-300">CSV</button>
          <button data-viewport-allow-small-target onClick={() => exportMetrics('json')} className="px-3 py-1 rounded-full text-xs font-medium border bg-neutral-900/50 border-neutral-800/50 text-neutral-400 hover:border-neutral-700 hover:text-neutral-300">JSON</button>
        </div>
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
        {/* Cost Over Time chart */}
        <div className={cardClass}>
          <div className="text-xs font-semibold text-neutral-400 uppercase tracking-wide mb-2">VM Cost/Hr (e2-standard-2)</div>
          <CostChart points={metricsHistory} windowMs={windowMs} />
        </div>

        {/* VM Utilization chart */}
        <div className={cardClass}>
          <div className="text-xs font-semibold text-neutral-400 uppercase tracking-wide mb-2">VM Utilization</div>
          <VMChart points={metricsHistory} maxConcurrentVMs={maxConcurrentVMs} windowMs={windowMs} />
        </div>

        {/* VM-Hours card */}
        <div className={cardClass}>
          <div className="text-ui-md font-bold text-neutral-400 uppercase tracking-widest mb-2">
            Total VM-Hours
          </div>
          <div className="text-lg font-semibold text-neutral-100 font-mono">
            {cumulativeVmHours.toFixed(2)}
          </div>
          <div className="text-ui-sm text-neutral-500 mt-1">
            Cumulative VM Cost: <span className="text-neutral-300 font-mono">${cumulativeCost.toFixed(2)}</span>
          </div>
          <div className="text-ui-sm text-neutral-500">since session start</div>
        </div>

        {/* Task Throughput card */}
        <div className={cardClass}>
          <div className="text-ui-md font-bold text-neutral-400 uppercase tracking-widest mb-2">
            Tasks Completed
          </div>
          <div className="grid grid-cols-3 gap-2 text-center">
            <div><div className="text-lg font-semibold text-neutral-100 font-mono">{count1h}</div><div className="text-ui-sm text-neutral-500">1h</div></div>
            <div><div className="text-lg font-semibold text-neutral-100 font-mono">{count6h}</div><div className="text-ui-sm text-neutral-500">6h</div></div>
            <div><div className="text-lg font-semibold text-neutral-100 font-mono">{count24h}</div><div className="text-ui-sm text-neutral-500">24h</div></div>
          </div>
        </div>

        {/* Success Rate card */}
        <div className={cardClass}>
          <div className="text-ui-md font-bold text-neutral-400 uppercase tracking-widest mb-2">
            Success Rate
          </div>
          <div className="grid grid-cols-3 gap-2 text-center">
            <div>
              <div className={`text-lg font-semibold font-mono ${successRateColor(count1h, failed1h)}`}>{successRate(count1h, failed1h)}</div>
              <div className="text-ui-sm text-neutral-500">1h</div>
            </div>
            <div>
              <div className={`text-lg font-semibold font-mono ${successRateColor(count6h, failed6h)}`}>{successRate(count6h, failed6h)}</div>
              <div className="text-ui-sm text-neutral-500">6h</div>
            </div>
            <div>
              <div className={`text-lg font-semibold font-mono ${successRateColor(count24h, failed24h)}`}>{successRate(count24h, failed24h)}</div>
              <div className="text-ui-sm text-neutral-500">24h</div>
            </div>
          </div>
        </div>

        {/* Digest latency card */}
        <div className={`${cardClass} sm:col-span-2`}>
          <div className="flex flex-wrap items-center justify-between gap-2 mb-3">
            <div className="text-xs font-semibold text-neutral-400 uppercase tracking-wide">Digest Latency</div>
            <div className="text-ui-sm text-neutral-500">Shared visibility-aware poll, 60s cadence</div>
          </div>
          <DigestLatencyWidget />
        </div>

        {/* Task Outcomes chart — full width */}
        <div className={`${cardClass} sm:col-span-2`}>
          <div className="text-xs font-semibold text-neutral-400 uppercase tracking-wide mb-2">Task Outcomes</div>
          <TaskOutcomesChart points={metricsHistory} tasks={tasks} windowMs={windowMs} />
        </div>

        {/* Failure Rate chart */}
        <div className={cardClass}>
          <div className="text-xs font-semibold text-neutral-400 uppercase tracking-wide mb-2">Failure Rate</div>
          <FailureRateChart points={metricsHistory} windowMs={windowMs} />
        </div>

        {/* Spend burn rate widget — full width */}
        <div className={`${cardClass} sm:col-span-2`}>
          <div className="text-xs font-semibold text-neutral-400 uppercase tracking-wide mb-3">Spend Burn Rate</div>
          <SpendBurnRateWidget projectId={projectId} />
        </div>

        {/* Cost breakdown widget — full width */}
        {projectId && (
          <div className={`${cardClass} sm:col-span-2`}>
            <div className="text-xs font-semibold text-neutral-400 uppercase tracking-wide mb-3">Cost Breakdown</div>
            <TaskCostBreakdownWidget projectId={projectId} window={metricsWindow} />
          </div>
        )}

        {/* Efficiency axes widget — full width */}
        {projectId && (
          <div className={`${cardClass} sm:col-span-2`}>
            <div className="text-xs font-semibold text-neutral-400 uppercase tracking-wide mb-3">Efficiency Axes</div>
            <EfficiencyWidget projectId={projectId} />
          </div>
        )}

        {/* Lane diagnostics panel — full width */}
        {projectId && (
          <div className={`${cardClass} sm:col-span-2`}>
            <div className="text-xs font-semibold text-neutral-400 uppercase tracking-wide mb-3">Lane Diagnostics</div>
            <LaneDiagPanel projectId={projectId} />
          </div>
        )}
      </div>
    </div>
  );
}
