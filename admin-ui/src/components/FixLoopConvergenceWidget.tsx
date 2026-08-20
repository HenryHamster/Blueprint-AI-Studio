import { useState, useEffect, useCallback, useRef } from 'react';
import { ResponsiveContainer, AreaChart, Area, Tooltip } from 'recharts';
import { apiFetch } from '../api';
import { EXIT_REASON_COLORS } from '../utils/fixLoopColors';
import { FIX_LOOP_LAYERS, LOOP_LABELS } from '../utils/fixLoopLayers';
import { usePoller } from '../hooks/usePoller';

interface LoopAggregate {
  task_count: number;
  avg_iterations: number | null;
  convergence_rate: number | null;
  exit_reason_counts: Record<string, number>;
  iteration_distribution: Record<string, number>;
}

interface ConvergenceData {
  window_hours: number;
  tasks_scanned: number;
  tasks_with_fix_loops: number;
  lint: LoopAggregate;
  test: LoopAggregate;
  check: LoopAggregate;
}

const LOOP_COLORS: Record<string, string> = {
  lint: 'text-amber-400',
  test: 'text-blue-400',
  check: 'text-purple-400',
};

interface DayLoopStats {
  task_count: number;
  convergence_rate: number | null;
  divergence_rate: number | null;
}

interface TrendBucket {
  date: string;
  lint: DayLoopStats;
  test: DayLoopStats;
  check: DayLoopStats;
}

interface TrendData {
  days: number;
  buckets: TrendBucket[];
}

interface DivergedTask {
  task_id: string;
  title: string;
  task_type: string;
  scope: string | null;
  project_id: string;
  status: string;
  loop: string;
  exit_reason: string;
  iteration_count: number;
  error_tail: string | null;
}

interface DrillDownFilter {
  loop: string;
  exit_reason: string;
}

interface FixLoopConvergenceWidgetProps {
  trendDays?: 7 | 14 | 30;
  projectId?: string;
  onViewTask?: (taskId: string) => void;
}

const POLL_MS = 60_000;
const DIST_KEYS = ['1', '2', '3', '4+'] as const;

const TOOLTIP_CONTENT_STYLE = { background: '#1a1a1a', border: '1px solid #404040', borderRadius: 4, padding: '4px 8px', fontSize: 10 };
const TOOLTIP_LABEL_STYLE = { color: '#a3a3a3' };
const TOOLTIP_ITEM_STYLE = { color: '#e5e5e5' };
const tooltipFormatter = (value: unknown, name: unknown): [string, string] => [value == null ? '—' : `${value}%`, name === 'conv' ? 'converged' : 'diverged'];
const tooltipLabelFormatter = (label: unknown): string => String(label);

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
            title={`${k} iter: ${count} (${Math.round(pct)}%)`}
            className={`${colors[i]} rounded shrink-0`}
            style={{ width: `${Math.max(pct, 2)}%`, height: `${Math.max(pct / 100 * 16, 2)}px` }}
          />
        );
      })}
    </div>
  );
}

function LoopCard({ name, data, onChipClick }: { name: string; data: LoopAggregate; onChipClick: (loop: string, reason: string) => void }) {
  if (data.task_count === 0) {
    return (
      <div className="bg-workbench-base/60 rounded-lg p-3 border border-neutral-800 flex flex-col gap-1 min-w-0">
        <span className={`text-xs font-semibold uppercase tracking-wide ${LOOP_COLORS[name]}`}>{LOOP_LABELS[name]}</span>
        <span className="text-xs text-neutral-600">no data</span>
      </div>
    );
  }
  const convPct = data.convergence_rate != null ? Math.round(data.convergence_rate * 100) : null;
  const convColor = convPct == null ? 'text-neutral-500' : convPct >= 80 ? 'text-emerald-400' : convPct >= 50 ? 'text-yellow-400' : 'text-red-400';

  const topReasons = Object.entries(data.exit_reason_counts)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 4);

  return (
    <div className="bg-workbench-base/60 rounded-lg p-3 border border-neutral-800 flex flex-col gap-2 min-w-0">
      <div className="flex items-center justify-between gap-2">
        <span className={`text-xs font-semibold uppercase tracking-wide ${LOOP_COLORS[name]}`}>{LOOP_LABELS[name]}</span>
        <span className="text-xs text-neutral-500">{data.task_count} task{data.task_count !== 1 ? 's' : ''}</span>
      </div>

      <div className="flex items-baseline gap-3">
        {data.avg_iterations != null && (
          <div className="flex flex-col">
            <span className="text-lg font-bold text-neutral-100 leading-none">{data.avg_iterations.toFixed(1)}</span>
            <span className="text-ui-sm text-neutral-500">avg iter</span>
          </div>
        )}
        {convPct != null && (
          <div className="flex flex-col">
            <span className={`text-lg font-bold leading-none ${convColor}`}>{convPct}%</span>
            <span className="text-ui-sm text-neutral-500">converged</span>
          </div>
        )}
      </div>

      <IterDistBar dist={data.iteration_distribution} />

      <div className="flex flex-wrap gap-1">
        {topReasons.map(([reason, count]) => {
          const cls = EXIT_REASON_COLORS[reason] ?? 'bg-workbench-control/20 text-neutral-400 border-neutral-500/30';
          return (
            <button
              data-viewport-allow-small-target
              key={reason}
              onClick={() => onChipClick(name, reason)}
              className={`inline-flex items-center gap-1 text-ui-sm px-1.5 py-0.5 rounded border cursor-pointer hover:brightness-125 transition-all ${cls}`}
              title={`Show tasks: ${reason}`}
            >
              {reason.replace(/_/g, ' ')}
              <span className="font-bold">{count}</span>
            </button>
          );
        })}
      </div>
    </div>
  );
}

const DIVERGENCE_PATTERNS = [
  'divergence_oscillation',
  'divergence_similar_error',
  'divergence_diff_revert',
] as const;

function DrillDownPanel({
  filter,
  hours,
  projectId,
  exitReasonCounts,
  onViewTask,
  onClose,
}: {
  filter: DrillDownFilter;
  hours: number;
  projectId?: string;
  exitReasonCounts: Record<string, number>;
  onViewTask?: (taskId: string) => void;
  onClose: () => void;
}) {
  const [tasks, setTasks] = useState<DivergedTask[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [activePattern, setActivePattern] = useState<string | null>(null);
  const panelMountedRef = useRef(true);

  const isDivergenceFilter = filter.exit_reason.includes('divergence');
  const effectiveReason = activePattern ?? filter.exit_reason;
  const patternCounts: Record<string, number> = isDivergenceFilter
    ? Object.fromEntries(DIVERGENCE_PATTERNS.map(p => [p, exitReasonCounts[p] ?? 0]))
    : {};

  const fetchDiverged = useCallback(async () => {
    setLoading(true);
    setError(null);
    const params = new URLSearchParams({
      hours: String(hours),
      loop: filter.loop,
      exit_reason: effectiveReason,
      limit: '50',
    });
    if (projectId) params.set('project_id', projectId);
    try {
      const r = await apiFetch(`/api/admin/fix-loop-diverged-tasks?${params}`);
      if (!panelMountedRef.current) return;
      if (!r.ok) { setError(`HTTP ${r.status}`); setLoading(false); return; }
      const body = (await r.json()) as { tasks: DivergedTask[]; total: number };
      if (!panelMountedRef.current) return;
      setTasks(body.tasks);
      setTotal(body.total);
      setLoading(false);
    } catch (e) {
      if (!panelMountedRef.current) return;
      setError(e instanceof Error ? e.message : String(e));
      setLoading(false);
    }
  }, [filter.loop, effectiveReason, hours, projectId]);

  useEffect(() => {
    panelMountedRef.current = true;
    // eslint-disable-next-line react-hooks/set-state-in-effect -- async fetch sets state after await, intentional
    fetchDiverged();
    return () => { panelMountedRef.current = false; };
  }, [fetchDiverged]);

  const reasonCls = EXIT_REASON_COLORS[effectiveReason] ?? 'bg-workbench-control/20 text-neutral-400 border-neutral-500/30';

  return (
    <div className="mt-2 bg-workbench-base/80 rounded-lg border border-neutral-700 p-3">
      <div className="flex items-center justify-between gap-2 mb-2">
        <div className="flex items-center gap-2 flex-wrap">
          <span className={`text-ui-sm font-semibold uppercase tracking-wide ${LOOP_COLORS[filter.loop]}`}>{LOOP_LABELS[filter.loop]}</span>
          <span className={`inline-flex items-center text-ui-sm px-1.5 py-0.5 rounded border ${reasonCls}`}>
            {effectiveReason.replace(/_/g, ' ')}
          </span>
          {!loading && <span className="text-ui-sm text-neutral-500">{total} task{total !== 1 ? 's' : ''}</span>}
        </div>
        <button data-viewport-allow-small-target onClick={onClose} className="text-neutral-500 hover:text-neutral-300 text-xs leading-none px-1" aria-label="Close">✕</button>
      </div>
      {isDivergenceFilter && DIVERGENCE_PATTERNS.some(p => (patternCounts[p] ?? 0) > 0) && (
        <div className="flex flex-wrap gap-1 mb-2">
          {DIVERGENCE_PATTERNS.map(p => {
            const count = patternCounts[p] ?? 0;
            if (count === 0) return null;
            const cls = EXIT_REASON_COLORS[p] ?? 'bg-workbench-control/20 text-neutral-400 border-neutral-500/30';
            const isActive = activePattern === p;
            return (
              <button
                data-viewport-allow-small-target
                key={p}
                onClick={() => setActivePattern(prev => prev === p ? null : p)}
                className={`inline-flex items-center gap-1 text-ui-sm px-1.5 py-0.5 rounded border transition-all ${cls} ${isActive ? 'ring-1 ring-white/30' : 'opacity-60 hover:opacity-100'}`}
                title={isActive ? 'Click to show all patterns' : `Filter by ${p.replace(/_/g, ' ')}`}
              >
                {p.replace(/divergence_/g, '').replace(/_/g, ' ')}
                <span className="font-bold">{count}</span>
              </button>
            );
          })}
          {activePattern && (
            <button data-viewport-allow-small-target onClick={() => setActivePattern(null)} className="text-ui-sm text-neutral-400 hover:text-neutral-200 px-1">
              show all
            </button>
          )}
        </div>
      )}

      {loading && <p className="text-ui-sm text-neutral-500">Loading…</p>}
      {error && <p className="text-ui-sm text-red-400">{error}</p>}
      {!loading && !error && tasks.length === 0 && (
        <p className="text-ui-sm text-neutral-600">No tasks found for this filter.</p>
      )}
      {!loading && !error && tasks.length > 0 && (
        <div className="flex flex-col gap-1.5 max-h-64 overflow-y-auto pr-1">
          {tasks.map(t => (
            <div key={t.task_id} className="bg-workbench-panel/60 rounded p-2 flex flex-col gap-0.5 min-w-0">
              <div className="flex items-start justify-between gap-2">
                <span className="text-ui-md text-neutral-200 font-medium leading-tight truncate flex-1" title={t.title}>{t.title}</span>
                {onViewTask && (
                  <button
                    data-viewport-allow-small-target
                    onClick={() => onViewTask(t.task_id)}
                    className="text-ui-sm text-neutral-400 hover:text-neutral-100 shrink-0 ml-1 underline underline-offset-2"
                  >
                    View
                  </button>
                )}
              </div>
              <div className="flex items-center gap-2 flex-wrap">
                <span className="text-ui-sm text-neutral-500">{t.task_type}</span>
                {t.scope && <span className="text-ui-sm text-neutral-600">{t.scope}</span>}
                <span className="text-ui-sm text-neutral-600">{t.iteration_count} iter</span>
                <span className={`text-ui-sm px-1 py-0.5 rounded border ${EXIT_REASON_COLORS[t.exit_reason] ?? 'bg-workbench-control/20 text-neutral-400 border-neutral-500/30'}`}>
                  {t.exit_reason.replace(/_/g, ' ')}
                </span>
              </div>
              {t.error_tail && (
                <pre className="text-ui-xs text-neutral-500 font-mono mt-0.5 truncate max-w-full" title={t.error_tail}>{t.error_tail.slice(0, 200)}</pre>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

const LOOP_SPARK_COLORS: Record<string, { conv: string; div: string }> = {
  lint:  { conv: '#f59e0b', div: '#f97316' },
  test:  { conv: '#60a5fa', div: '#f97316' },
  check: { conv: '#a78bfa', div: '#f97316' },
};

function TrendSparkline({ loop, buckets }: { loop: string; buckets: TrendBucket[] }) {
  const colors = LOOP_SPARK_COLORS[loop];
  const data = buckets.map(b => {
    const stats = b[loop as keyof TrendBucket] as DayLoopStats | null | undefined;
    return {
      date: b.date,
      conv: stats != null && stats.convergence_rate != null
        ? Math.round(stats.convergence_rate * 100)
        : null,
      div: stats != null && stats.divergence_rate != null
        ? Math.round(stats.divergence_rate * 100)
        : null,
    };
  });
  const hasData = data.some(d => d.conv != null);
  if (!hasData) return <span className="text-neutral-600 text-xs">no data</span>;

  return (
    <div className="flex flex-col gap-0.5 min-w-0">
      <span className={`text-ui-sm font-semibold uppercase tracking-wide ${LOOP_COLORS[loop]}`}>{LOOP_LABELS[loop]}</span>
      <div className="h-10">
        <ResponsiveContainer width="100%" height="100%" minWidth={1} minHeight={1}>
          <AreaChart data={data} margin={{ top: 1, right: 0, left: 0, bottom: 1 }}>
            <defs>
              <linearGradient id={`tconv-${loop}`} x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor={colors.conv} stopOpacity={0.3} />
                <stop offset="100%" stopColor={colors.conv} stopOpacity={0} />
              </linearGradient>
              <linearGradient id={`tdiv-${loop}`} x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor={colors.div} stopOpacity={0.3} />
                <stop offset="100%" stopColor={colors.div} stopOpacity={0} />
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
            <Area type="monotone" dataKey="conv" stroke={colors.conv} fill={`url(#tconv-${loop})`} strokeWidth={1.5} dot={false} isAnimationActive={false} connectNulls />
            <Area type="monotone" dataKey="div" stroke={colors.div} fill={`url(#tdiv-${loop})`} strokeWidth={1} dot={false} isAnimationActive={false} connectNulls />
          </AreaChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}

export function FixLoopConvergenceWidget({ trendDays = 7, projectId, onViewTask }: FixLoopConvergenceWidgetProps) {
  const [data, setData] = useState<ConvergenceData | null>(null);
  const [trendData, setTrendData] = useState<TrendData | null>(null);
  const [loading, setLoading] = useState(true);
  const [fetchError, setFetchError] = useState<string | null>(null);
  const [drillDown, setDrillDown] = useState<DrillDownFilter | null>(null);
  const mountedRef = useRef(true);

  const fetchData = useCallback(async () => {
    try {
      const convergenceParams = new URLSearchParams({ hours: '24' });
      const trendParams = new URLSearchParams({ days: String(trendDays) });
      if (projectId) {
        convergenceParams.set('project_id', projectId);
        trendParams.set('project_id', projectId);
      }
      const [res, trendRes] = await Promise.all([
        apiFetch(`/api/admin/fix-loop-convergence?${convergenceParams}`),
        apiFetch(`/api/admin/fix-loop-convergence-trend?${trendParams}`),
      ]);
      if (!mountedRef.current) return;
      if (!res.ok) {
        setFetchError(`HTTP ${res.status}`);
        setLoading(false);
        return;
      }
      const body = (await res.json()) as ConvergenceData;
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
  }, [projectId, trendDays]);

  useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; };
  }, []);

  // Shared, visibility-aware poll clock. The old inline
  // `document.visibilityState === 'visible'` gate is now centralized in
  // usePoller (pause-when-hidden + catch-up on return).
  usePoller(fetchData, { intervalMs: POLL_MS, deps: [fetchData] });

  const handleChipClick = useCallback((loop: string, exit_reason: string) => {
    setDrillDown(prev =>
      prev?.loop === loop && prev?.exit_reason === exit_reason ? null : { loop, exit_reason }
    );
  }, []);

  if (loading) return null;
  if (fetchError) return null;
  if (!data || data.tasks_with_fix_loops === 0) return null;

  return (
    <section>
      <div className="flex items-center gap-2 mb-2">
        <p className="text-ui-sm text-neutral-500 uppercase tracking-wide">Fix-Loop Convergence</p>
        <span className="text-ui-sm text-neutral-600">{data.tasks_with_fix_loops} tasks · 24h</span>
      </div>
      <div className="grid grid-cols-3 gap-2">
        <LoopCard name="lint" data={data.lint} onChipClick={handleChipClick} />
        <LoopCard name="test" data={data.test} onChipClick={handleChipClick} />
        <LoopCard name="check" data={data.check} onChipClick={handleChipClick} />
      </div>
      <div className="flex gap-3 mt-1.5 items-center">
        {DIST_KEYS.map((k, i) => {
          const colors = ['text-emerald-400', 'text-yellow-400', 'text-orange-400', 'text-red-400'];
          return (
            <span key={k} className={`text-ui-sm ${colors[i]}`}>
              <span className="font-bold">{k}</span> iter
            </span>
          );
        })}
      </div>
      {drillDown && (
        <DrillDownPanel
          filter={drillDown}
          hours={24}
          projectId={projectId}
          exitReasonCounts={data[drillDown.loop as 'lint' | 'test' | 'check']?.exit_reason_counts ?? {}}
          onViewTask={onViewTask}
          onClose={() => setDrillDown(null)}
        />
      )}
      {trendData && trendData.buckets.length >= 2 && (
        <div className="mt-3">
          <div className="flex items-center gap-2 mb-1.5">
            <p className="text-ui-sm text-neutral-500 uppercase tracking-wide">{trendDays}-day trend</p>
            <span className="flex items-center gap-1 text-ui-sm text-neutral-600">
              <span className="inline-block w-3 border-t border-amber-400" /> converged
              <span className="inline-block w-3 border-t border-orange-500 ml-1" /> diverged
            </span>
          </div>
          <div className="grid grid-cols-3 gap-2">
            {FIX_LOOP_LAYERS.map(loop => (
              <TrendSparkline key={loop} loop={loop} buckets={trendData.buckets} />
            ))}
          </div>
        </div>
      )}
    </section>
  );
}
