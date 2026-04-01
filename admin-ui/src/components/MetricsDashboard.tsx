import { useState } from 'react';
import type { Task } from './TaskCard';

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
  maxConcurrentVMs: number;
  tasks: Task[];
}

const SVG_W = 300;
const SVG_H = 120;
const PAD = { top: 16, right: 12, bottom: 20, left: 36 };
const PLOT_W = SVG_W - PAD.left - PAD.right;
const PLOT_H = SVG_H - PAD.top - PAD.bottom;
const TWO_HOURS = 2 * 60 * 60 * 1000;

function formatTime(ts: number): string {
  const d = new Date(ts);
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function buildPolyline(
  points: MetricsPoint[],
  getValue: (p: MetricsPoint) => number,
  yMin: number,
  yMax: number,
  tMin: number,
  tMax: number,
): string {
  const tRange = tMax - tMin || 1;
  const yRange = yMax - yMin || 1;
  return points
    .map((p) => {
      const x = PAD.left + ((p.timestamp - tMin) / tRange) * PLOT_W;
      const y = PAD.top + PLOT_H - ((getValue(p) - yMin) / yRange) * PLOT_H;
      return `${x},${y}`;
    })
    .join(' ');
}

function NoDataMessage() {
  return (
    <svg viewBox={`0 0 ${SVG_W} ${SVG_H}`} className="w-full" preserveAspectRatio="xMidYMid meet">
      <text x={SVG_W / 2} y={SVG_H / 2} textAnchor="middle" dominantBaseline="middle" fill="#737373" fontSize="12">
        Collecting data…
      </text>
    </svg>
  );
}

interface HoverState {
  svgX: number;
  tooltipX: number;
  tooltipY: number;
  label: string;
}

function ChartSvg({
  points,
  getValue,
  strokeColor,
  refLine,
  unit,
}: {
  points: MetricsPoint[];
  getValue: (p: MetricsPoint) => number;
  strokeColor: string;
  refLine?: number;
  unit?: string;
}) {
  const [hover, setHover] = useState<HoverState | null>(null);

  const now = new Date().getTime();
  const tMin = now - TWO_HOURS;
  const tMax = now;
  const filtered = points.filter((p) => p.timestamp >= tMin);

  if (filtered.length < 2) return <NoDataMessage />;

  const values = filtered.map(getValue);
  let yMin = Math.min(...values);
  let yMax = Math.max(...values);
  if (refLine !== undefined) {
    yMax = Math.max(yMax, refLine);
  }
  // Add a little padding so lines aren't clipped at edges
  if (yMin === yMax) {
    yMin = Math.max(0, yMin - 1);
    yMax = yMax + 1;
  }

  const polyline = buildPolyline(filtered, getValue, yMin, yMax, tMin, tMax);

  // Grid lines (3 horizontal)
  const gridLines = [0.25, 0.5, 0.75].map((frac) => PAD.top + PLOT_H * (1 - frac));

  // Time labels (start, mid, end)
  const timeTicks = [tMin, tMin + TWO_HOURS / 2, tMax];

  const tRange = tMax - tMin || 1;

  function handleMouseMove(e: React.MouseEvent<SVGRectElement>) {
    const svgEl = (e.currentTarget as SVGElement).closest('svg') as SVGSVGElement;
    const svgRect = svgEl.getBoundingClientRect();

    // Map clientX to SVG viewBox x, then normalize within plot area
    const relX = e.clientX - svgRect.left;
    const svgX = (relX / svgRect.width) * SVG_W;
    const normX = Math.max(0, Math.min(1, (svgX - PAD.left) / PLOT_W));

    // Find nearest data point by time
    const targetTime = tMin + normX * tRange;
    const nearest = filtered.reduce((a, b) =>
      Math.abs(a.timestamp - targetTime) < Math.abs(b.timestamp - targetTime) ? a : b,
    );

    const value = getValue(nearest);
    const timeStr = formatTime(nearest.timestamp);
    const formatted = value % 1 === 0 ? String(value) : value.toFixed(2);
    const label = unit ? `${formatted} ${unit} @ ${timeStr}` : `${formatted} @ ${timeStr}`;

    // Tooltip position relative to the wrapping div
    const containerEl = svgEl.parentElement as HTMLElement;
    const containerRect = containerEl.getBoundingClientRect();

    setHover({
      svgX: PAD.left + normX * PLOT_W,
      tooltipX: e.clientX - containerRect.left,
      tooltipY: e.clientY - containerRect.top,
      label,
    });
  }

  function handleMouseLeave() {
    setHover(null);
  }

  return (
    <div className="relative">
      <svg viewBox={`0 0 ${SVG_W} ${SVG_H}`} className="w-full" preserveAspectRatio="xMidYMid meet">
        {/* Grid lines */}
        {gridLines.map((y) => (
          <line key={y} x1={PAD.left} x2={SVG_W - PAD.right} y1={y} y2={y} stroke="#262626" strokeWidth={1} />
        ))}

        {/* Y-axis labels */}
        <text x={PAD.left - 4} y={PAD.top + 4} textAnchor="end" fill="#737373" fontSize="8">
          {yMax % 1 === 0 ? yMax : yMax.toFixed(2)}
        </text>
        <text x={PAD.left - 4} y={PAD.top + PLOT_H + 2} textAnchor="end" fill="#737373" fontSize="8">
          {yMin % 1 === 0 ? yMin : yMin.toFixed(2)}
        </text>

        {/* X-axis labels */}
        {timeTicks.map((ts, i) => (
          <text
            key={ts}
            x={PAD.left + (i / (timeTicks.length - 1)) * PLOT_W}
            y={SVG_H - 2}
            textAnchor="middle"
            fill="#737373"
            fontSize="8"
          >
            {formatTime(ts)}
          </text>
        ))}

        {/* Reference line */}
        {refLine !== undefined && (
          <line
            x1={PAD.left}
            x2={SVG_W - PAD.right}
            y1={PAD.top + PLOT_H - ((refLine - yMin) / ((yMax - yMin) || 1)) * PLOT_H}
            y2={PAD.top + PLOT_H - ((refLine - yMin) / ((yMax - yMin) || 1)) * PLOT_H}
            stroke="#3b82f6"
            strokeWidth={1}
            strokeDasharray="4 3"
            opacity={0.5}
          />
        )}

        {/* Data line */}
        <polyline points={polyline} fill="none" stroke={strokeColor} strokeWidth={1.5} strokeLinejoin="round" />

        {/* Vertical hover indicator line */}
        {hover !== null && (
          <line
            x1={hover.svgX}
            x2={hover.svgX}
            y1={PAD.top}
            y2={PAD.top + PLOT_H}
            stroke="rgba(255,255,255,0.3)"
            strokeWidth={1}
            pointerEvents="none"
          />
        )}

        {/* Invisible overlay rect for mouse events */}
        <rect
          x={PAD.left}
          y={PAD.top}
          width={PLOT_W}
          height={PLOT_H}
          fill="transparent"
          onMouseMove={handleMouseMove}
          onMouseLeave={handleMouseLeave}
        />
      </svg>

      {/* Tooltip */}
      {hover !== null && (
        <div
          className="absolute bg-neutral-800 border border-neutral-700 rounded px-2 py-1 text-xs text-neutral-100 pointer-events-none z-10 whitespace-nowrap"
          style={{ left: hover.tooltipX + 8, top: hover.tooltipY - 28 }}
        >
          {hover.label}
        </div>
      )}
    </div>
  );
}

function TaskOutcomesChart({ points }: { points: MetricsPoint[] }) {
  const now = new Date().getTime();
  const tMin = now - TWO_HOURS;
  const tMax = now;
  const filtered = points.filter((p) => p.timestamp >= tMin);

  if (filtered.length < 2) return <NoDataMessage />;

  // Build cumulative counts over the window
  const cumPoints = filtered.reduce<{ timestamp: number; cumCompleted: number; cumFailed: number }[]>(
    (acc, p) => {
      const prev = acc[acc.length - 1];
      const cumCompleted = (prev?.cumCompleted ?? 0) + p.completedCount;
      const cumFailed = (prev?.cumFailed ?? 0) + p.failedCount;
      return [...acc, { timestamp: p.timestamp, cumCompleted, cumFailed }];
    },
    [],
  );

  const allValues = cumPoints.flatMap((p) => [p.cumCompleted, p.cumFailed]);
  let yMin = Math.min(...allValues);
  let yMax = Math.max(...allValues);
  if (yMin === yMax) {
    yMin = Math.max(0, yMin - 1);
    yMax = yMax + 1;
  }

  const tRange = tMax - tMin || 1;
  const yRange = yMax - yMin || 1;
  const toCoord = (ts: number, val: number) =>
    `${PAD.left + ((ts - tMin) / tRange) * PLOT_W},${PAD.top + PLOT_H - ((val - yMin) / yRange) * PLOT_H}`;

  const completedPoly = cumPoints.map((p) => toCoord(p.timestamp, p.cumCompleted)).join(' ');
  const failedPoly = cumPoints.map((p) => toCoord(p.timestamp, p.cumFailed)).join(' ');

  const gridLines = [0.25, 0.5, 0.75].map((frac) => PAD.top + PLOT_H * (1 - frac));
  const timeTicks = [tMin, tMin + TWO_HOURS / 2, tMax];

  return (
    <svg viewBox={`0 0 ${SVG_W} ${SVG_H}`} className="w-full" preserveAspectRatio="xMidYMid meet">
      {gridLines.map((y) => (
        <line key={y} x1={PAD.left} x2={SVG_W - PAD.right} y1={y} y2={y} stroke="#262626" strokeWidth={1} />
      ))}
      <text x={PAD.left - 4} y={PAD.top + 4} textAnchor="end" fill="#737373" fontSize="8">
        {yMax}
      </text>
      <text x={PAD.left - 4} y={PAD.top + PLOT_H + 2} textAnchor="end" fill="#737373" fontSize="8">
        {yMin}
      </text>
      {timeTicks.map((ts, i) => (
        <text
          key={ts}
          x={PAD.left + (i / (timeTicks.length - 1)) * PLOT_W}
          y={SVG_H - 2}
          textAnchor="middle"
          fill="#737373"
          fontSize="8"
        >
          {formatTime(ts)}
        </text>
      ))}
      <polyline points={completedPoly} fill="none" stroke="#10b981" strokeWidth={1.5} strokeLinejoin="round" />
      <polyline points={failedPoly} fill="none" stroke="#ef4444" strokeWidth={1.5} strokeLinejoin="round" />
      {/* Legend */}
      <rect x={PAD.left} y={PAD.top - 2} width={6} height={6} fill="#10b981" rx={1} />
      <text x={PAD.left + 9} y={PAD.top + 4} fill="#a3a3a3" fontSize="8">Completed</text>
      <rect x={PAD.left + 66} y={PAD.top - 2} width={6} height={6} fill="#ef4444" rx={1} />
      <text x={PAD.left + 75} y={PAD.top + 4} fill="#a3a3a3" fontSize="8">Failed</text>
    </svg>
  );
}

function getTaskCompletedTime(task: Task): number | null {
  if (task.status !== 'COMPLETED') return null;
  // Use last history entry timestamp if available
  if (task.history && task.history.length > 0) {
    const last = task.history[task.history.length - 1];
    const ts = new Date(last.timestamp).getTime();
    if (!isNaN(ts)) return ts;
  }
  // Fallback to created_at
  const ts = new Date(task.created_at).getTime();
  return isNaN(ts) ? null : ts;
}

export default function MetricsDashboard({
  metricsHistory,
  cumulativeCost,
  maxConcurrentVMs,
  tasks,
}: MetricsDashboardProps) {
  // Task throughput calculation
  const now = new Date().getTime();
  const completedTasks = tasks
    .map((t) => ({ task: t, completedAt: getTaskCompletedTime(t) }))
    .filter((t): t is { task: Task; completedAt: number } => t.completedAt !== null);

  const count1h = completedTasks.filter((t) => now - t.completedAt <= 3_600_000).length;
  const count6h = completedTasks.filter((t) => now - t.completedAt <= 21_600_000).length;
  const count24h = completedTasks.filter((t) => now - t.completedAt <= 86_400_000).length;

  const cardClass = 'bg-neutral-900/50 border border-neutral-800/50 rounded-xl p-4';

  return (
    <div className="grid grid-cols-2 gap-4 mb-6">
      {/* Cost Over Time chart */}
      <div className={cardClass}>
        <div className="text-xs text-neutral-400 mb-1">Cost / Hour</div>
        <ChartSvg
          points={metricsHistory}
          getValue={(p) => p.costPerHr}
          strokeColor="#10b981"
          unit="$/hr"
        />
      </div>

      {/* VM Utilization chart */}
      <div className={cardClass}>
        <div className="text-xs text-neutral-400 mb-1">VM Utilization</div>
        <ChartSvg
          points={metricsHistory}
          getValue={(p) => p.activeVMs}
          strokeColor="#3b82f6"
          refLine={maxConcurrentVMs}
          unit="VMs"
        />
      </div>

      {/* Session Totals card */}
      <div className={cardClass}>
        <div className="text-[11px] font-bold text-neutral-400 uppercase tracking-widest mb-2">
          Est. Cost Since Page Load
        </div>
        <div className="text-lg font-semibold text-neutral-100 font-mono">
          ${cumulativeCost.toFixed(2)}
        </div>
      </div>

      {/* Task Throughput card */}
      <div className={cardClass}>
        <div className="text-[11px] font-bold text-neutral-400 uppercase tracking-widest mb-2">
          Tasks Completed
        </div>
        <div className="text-sm font-medium text-neutral-100 font-mono">
          1h: {count1h} | 6h: {count6h} | 24h: {count24h}
        </div>
      </div>

      {/* Task Outcomes chart — full width */}
      <div className={`${cardClass} col-span-2`}>
        <div className="text-xs text-neutral-400 mb-1">Task Outcomes</div>
        <TaskOutcomesChart points={metricsHistory} />
      </div>
    </div>
  );
}
