import { useMemo, useState, useEffect } from 'react';
import { parseTimestamp } from '../utils/time';
import { downloadText, escapeCsv } from '../utils/taskCsv';
import type { Task } from './TaskCard';
import { apiFetch } from '../api';
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
  metricsWindow: '15m' | '30m' | '1h' | '2h' | '3h' | '6h' | '12h' | '24h' | 'all';
  onWindowChange: (w: '15m' | '30m' | '1h' | '2h' | '3h' | '6h' | '12h' | '24h' | 'all') => void;
  projectId?: string;
}

const WINDOW_MS: Record<'15m' | '30m' | '1h' | '2h' | '3h' | '6h' | '12h' | '24h' | 'all', number> = { '15m': 900000, '30m': 1800000, '1h': 3600000, '2h': 7200000, '3h': 10800000, '6h': 21600000, '12h': 43200000, '24h': 86400000, 'all': Number.MAX_SAFE_INTEGER };

const TICK_STYLE = { fill: '#737373', fontSize: 10 };
const GRID_COLOR = '#262626';
const TOOLTIP_STYLE = {
  contentStyle: { background: '#171717', border: '1px solid #404040', borderRadius: 4 },
  itemStyle: { color: '#f5f5f5' },
  labelStyle: { color: '#a3a3a3' },
};

const formatCostPerHr = (value: unknown): [string, string] => [(value as number).toFixed(2) + ' $/hr', 'VM Cost/Hr'];
const formatActiveVMs = (value: unknown): [string, string] => [String(value) + ' VMs', 'Active VMs'];
const formatFailureRate = (value: unknown): [string, string] => [(value as number).toFixed(1) + '%', 'Failure Rate'];
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
  const filtered = points.filter((p) => p.timestamp >= now - windowMs);
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
  const filtered = points.filter((p) => p.timestamp >= now - windowMs);
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
  const filtered = points.filter((p) => p.timestamp >= now - windowMs);
  const labelFmt = useMemo(() => makeLabelFormatter(windowMs), [windowMs]);
  if (filtered.length < 2) return <NoDataMessage />;

  const baselineCompleted = tasks.filter((t) => t.status === 'COMPLETED').length;
  const baselineFailed = tasks.filter((t) => t.status === 'FAILED').length;

  const totalDeltaCompleted = filtered.reduce((s, p) => s + p.completedCount, 0);
  const totalDeltaFailed = filtered.reduce((s, p) => s + p.failedCount, 0);

  const offsetCompleted = Math.max(0, baselineCompleted - totalDeltaCompleted);
  const offsetFailed = Math.max(0, baselineFailed - totalDeltaFailed);

  const cumPoints = filtered.reduce<{ timestamp: number; cumCompleted: number; cumFailed: number }[]>(
    (acc, p) => {
      const prev = acc[acc.length - 1];
      const cumCompleted = Math.max(0, (prev?.cumCompleted ?? offsetCompleted) + p.completedCount);
      const cumFailed = Math.max(0, (prev?.cumFailed ?? offsetFailed) + p.failedCount);
      return [...acc, { timestamp: p.timestamp, cumCompleted, cumFailed }];
    },
    [],
  );

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
  const filtered = points.filter((p) => p.timestamp >= now - windowMs);
  const labelFmt = useMemo(() => makeLabelFormatter(windowMs), [windowMs]);
  if (filtered.length < 2) return <NoDataMessage />;

  const ratePoints = filtered.map((p) => {
    const total = p.completedCount + p.failedCount;
    const failureRate = total > 0 ? (p.failedCount / total) * 100 : 0;
    return { timestamp: p.timestamp, failureRate };
  });

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
}

// Backend returns history[] per axis; api.ts EfficiencyAxis adds this field in cluster 4/4.
interface EfficiencyAxisLocal {
  current: number | null;
  baseline: number | null;
  drift_pct: number | null;
  history?: Array<{ captured_at: string; value: number | null }>;
}

interface EfficiencyAxesLocal {
  axes: Record<string, EfficiencyAxisLocal>;
  baseline_captured_at: string | null;
  last_snapshot_at: string | null;
  vm_cold_start_breakdown?: Record<string, number | null> | null;
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
      <div className="text-[10px] text-neutral-500 mb-1 uppercase tracking-wide">Cold-start stage breakdown (p50)</div>
      <div className="grid grid-cols-2 gap-x-4 gap-y-0.5">
        {COLD_START_STAGE_LABELS.map(([key, label, isQueueWait]) => {
          const val = breakdown[key];
          return (
            <div key={key} className="flex justify-between items-center text-[10px]">
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

function AxisSparkline({ history, favorableDirection }: {
  history: Array<{ captured_at: string; value: number | null }>;
  favorableDirection: 'lower' | 'higher';
}) {
  const points = history
    .filter((h): h is { captured_at: string; value: number } => h.value !== null)
    .map(h => ({ v: h.value }));
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
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

const EFFICIENCY_AXES: EfficiencyAxisDef[] = [
  { key: 'cost_per_merged_pr', label: 'Cost per merged PR', unit: '$', favorableDirection: 'lower' },
  { key: 'vm_cold_start_p50_ms', label: 'VM cold-start p50', unit: 'ms', favorableDirection: 'lower' },
  { key: 'vm_reuse_rate', label: 'VM reuse rate', unit: '%', favorableDirection: 'higher' },
  { key: 'median_api_calls_per_task', label: 'Median API calls per task', unit: '', favorableDirection: 'lower' },
  { key: 'median_review_iterations', label: 'Median review iterations', unit: '', favorableDirection: 'lower' },
];

function getDriftColor(driftPct: number | null, favorableDirection: 'lower' | 'higher'): string {
  if (driftPct === null) return 'text-neutral-500';
  const isImproving = (favorableDirection === 'lower' && driftPct < 0) || (favorableDirection === 'higher' && driftPct > 0);
  return isImproving ? 'text-emerald-400' : 'text-red-400';
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
        const { current, baseline, drift_pct } = axisData;
        const driftColor = getDriftColor(drift_pct, axisDef.favorableDirection);
        const driftArrow = drift_pct === null ? '—' : (drift_pct < 0 ? '↓' : '↑');
        const driftDisplay = drift_pct === null ? '—' : `${driftArrow} ${Math.abs(drift_pct).toFixed(1)}%`;
        return (
          <div key={axisDef.key} className="py-1.5 px-3 rounded bg-neutral-800/50 text-xs">
            <div className="flex justify-between items-center">
              <span className="text-neutral-400">{axisDef.label}</span>
              <div className="flex gap-4 items-center text-neutral-300 font-mono">
                <span>{current !== null ? `${current.toFixed(1)}${axisDef.unit}` : '—'}</span>
                <span className="text-neutral-500">baseline: {baseline !== null ? `${baseline.toFixed(1)}${axisDef.unit}` : '—'}</span>
                <span className={`${driftColor} min-w-12 text-right`}>{driftDisplay}</span>
              </div>
            </div>
            {axisData.history && axisData.history.length >= 2 && (
              <AxisSparkline history={axisData.history} favorableDirection={axisDef.favorableDirection} />
            )}
            {axisDef.key === 'vm_cold_start_p50_ms' && data.vm_cold_start_breakdown && (
              <ColdStartBreakdown breakdown={data.vm_cold_start_breakdown} />
            )}
          </div>
        );
      })}
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
  const filteredMetrics = metricsHistory.filter((p) => p.timestamp >= now - windowMs);
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
          <div className="text-[11px] font-bold text-neutral-400 uppercase tracking-widest mb-2">
            Total VM-Hours
          </div>
          <div className="text-lg font-semibold text-neutral-100 font-mono">
            {cumulativeVmHours.toFixed(2)}
          </div>
          <div className="text-[10px] text-neutral-500 mt-1">
            Cumulative VM Cost: <span className="text-neutral-300 font-mono">${cumulativeCost.toFixed(2)}</span>
          </div>
          <div className="text-[10px] text-neutral-500">since session start</div>
        </div>

        {/* Task Throughput card */}
        <div className={cardClass}>
          <div className="text-[11px] font-bold text-neutral-400 uppercase tracking-widest mb-2">
            Tasks Completed
          </div>
          <div className="grid grid-cols-3 gap-2 text-center">
            <div><div className="text-lg font-semibold text-neutral-100 font-mono">{count1h}</div><div className="text-[10px] text-neutral-500">1h</div></div>
            <div><div className="text-lg font-semibold text-neutral-100 font-mono">{count6h}</div><div className="text-[10px] text-neutral-500">6h</div></div>
            <div><div className="text-lg font-semibold text-neutral-100 font-mono">{count24h}</div><div className="text-[10px] text-neutral-500">24h</div></div>
          </div>
        </div>

        {/* Success Rate card */}
        <div className={cardClass}>
          <div className="text-[11px] font-bold text-neutral-400 uppercase tracking-widest mb-2">
            Success Rate
          </div>
          <div className="grid grid-cols-3 gap-2 text-center">
            <div>
              <div className={`text-lg font-semibold font-mono ${successRateColor(count1h, failed1h)}`}>{successRate(count1h, failed1h)}</div>
              <div className="text-[10px] text-neutral-500">1h</div>
            </div>
            <div>
              <div className={`text-lg font-semibold font-mono ${successRateColor(count6h, failed6h)}`}>{successRate(count6h, failed6h)}</div>
              <div className="text-[10px] text-neutral-500">6h</div>
            </div>
            <div>
              <div className={`text-lg font-semibold font-mono ${successRateColor(count24h, failed24h)}`}>{successRate(count24h, failed24h)}</div>
              <div className="text-[10px] text-neutral-500">24h</div>
            </div>
          </div>
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

        {/* Efficiency axes widget — full width */}
        {projectId && (
          <div className={`${cardClass} sm:col-span-2`}>
            <div className="text-xs font-semibold text-neutral-400 uppercase tracking-wide mb-3">Efficiency Axes</div>
            <EfficiencyWidget projectId={projectId} />
          </div>
        )}
      </div>
    </div>
  );
}
