import { useState } from 'react';
import { parseTimestamp } from '../utils/time';
import type { Task } from './TaskCard';
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
}

const WINDOW_MS: Record<'15m' | '30m' | '1h' | '2h' | '3h' | '6h' | '12h' | '24h' | 'all', number> = { '15m': 900000, '30m': 1800000, '1h': 3600000, '2h': 7200000, '3h': 10800000, '6h': 21600000, '12h': 43200000, '24h': 86400000, 'all': Number.MAX_SAFE_INTEGER };

const TICK_STYLE = { fill: '#737373', fontSize: 10 };
const GRID_COLOR = '#262626';
const TOOLTIP_STYLE = {
  contentStyle: { background: '#171717', border: '1px solid #404040', borderRadius: 4 },
  itemStyle: { color: '#f5f5f5' },
  labelStyle: { color: '#a3a3a3' },
};

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
  if (filtered.length < 2) return <NoDataMessage />;

  return (
    <ResponsiveContainer width="100%" height={160}>
      <AreaChart data={filtered}>
        <CartesianGrid stroke={GRID_COLOR} vertical={false} />
        <XAxis dataKey="timestamp" tickFormatter={(ts) => formatTime(Number(ts), windowMs)} tick={TICK_STYLE} interval="preserveStartEnd" />
        <YAxis tick={TICK_STYLE} />
        <Tooltip
          {...TOOLTIP_STYLE}
          formatter={(value: unknown) => [(value as number).toFixed(2) + ' $/hr', 'VM Cost/Hr']}
          labelFormatter={(ts) => formatTime(Number(ts), windowMs)}
        />
        <Area dataKey="costPerHr" stroke="#10b981" fill="#10b981" fillOpacity={0.15} dot={false} />
      </AreaChart>
    </ResponsiveContainer>
  );
}

function VMChart({ points, maxConcurrentVMs, windowMs }: { points: MetricsPoint[]; maxConcurrentVMs: number; windowMs: number }) {
  const [now] = useState(Date.now);
  const filtered = points.filter((p) => p.timestamp >= now - windowMs);
  if (filtered.length < 2) return <NoDataMessage />;

  return (
    <ResponsiveContainer width="100%" height={160}>
      <AreaChart data={filtered}>
        <CartesianGrid stroke={GRID_COLOR} vertical={false} />
        <XAxis dataKey="timestamp" tickFormatter={(ts) => formatTime(Number(ts), windowMs)} tick={TICK_STYLE} interval="preserveStartEnd" />
        <YAxis tick={TICK_STYLE} />
        <Tooltip
          {...TOOLTIP_STYLE}
          formatter={(value: unknown) => [String(value) + ' VMs', 'Active VMs']}
          labelFormatter={(ts) => formatTime(Number(ts), windowMs)}
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
          labelFormatter={(ts) => formatTime(Number(ts), windowMs)}
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
          formatter={(value: unknown) => [(value as number).toFixed(1) + '%', 'Failure Rate']}
          labelFormatter={(ts) => formatTime(Number(ts), windowMs)}
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

const WINDOW_OPTIONS: Array<'15m' | '30m' | '1h' | '2h' | '3h' | '6h' | '12h' | '24h' | 'all'> = ['15m', '30m', '1h', '2h', '3h', '6h', '12h', '24h', 'all'];

export default function MetricsDashboard({
  metricsHistory,
  cumulativeCost,
  cumulativeVmHours,
  maxConcurrentVMs,
  tasks,
  metricsWindow,
  onWindowChange,
}: MetricsDashboardProps) {
  const windowMs = WINDOW_MS[metricsWindow];
  const [now] = useState(Date.now);

  const completedTasks = tasks
    .map((t) => ({ task: t, completedAt: getTaskCompletedTime(t) }))
    .filter((t): t is { task: Task; completedAt: number } => t.completedAt !== null);

  const count1h = completedTasks.filter((t) => now - t.completedAt <= 3_600_000).length;
  const count6h = completedTasks.filter((t) => now - t.completedAt <= 21_600_000).length;
  const count24h = completedTasks.filter((t) => now - t.completedAt <= 86_400_000).length;

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

  const failedTasks = tasks
    .map((t) => ({ task: t, failedAt: getTaskFailedTime(t) }))
    .filter((t): t is { task: Task; failedAt: number } => t.failedAt !== null);

  const failed1h = failedTasks.filter((t) => now - t.failedAt <= 3_600_000).length;
  const failed6h = failedTasks.filter((t) => now - t.failedAt <= 21_600_000).length;
  const failed24h = failedTasks.filter((t) => now - t.failedAt <= 86_400_000).length;

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

  return (
    <div className="mb-6">
      {/* Window selector */}
      <div className="flex items-center gap-1 mb-3">
        {WINDOW_OPTIONS.map((w) => (
          <button
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
      </div>
    </div>
  );
}
