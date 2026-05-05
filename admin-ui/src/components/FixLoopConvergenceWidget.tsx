import { useState, useEffect, useCallback, useRef } from 'react';
import { apiFetch } from '../api';

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

const LOOP_LABELS: Record<string, string> = { lint: 'Lint', test: 'Test', check: 'Check' };
const LOOP_COLORS: Record<string, string> = {
  lint: 'text-amber-400',
  test: 'text-blue-400',
  check: 'text-purple-400',
};
const EXIT_REASON_COLORS: Record<string, string> = {
  passed: 'bg-emerald-500/20 text-emerald-400 border-emerald-500/30',
  early_unchanged: 'bg-yellow-500/20 text-yellow-400 border-yellow-500/30',
  max_attempts: 'bg-red-500/20 text-red-400 border-red-500/30',
  divergence_detected: 'bg-orange-500/20 text-orange-400 border-orange-500/30',
  divergence_oscillation: 'bg-orange-400/20 text-orange-300 border-orange-400/30',
  divergence_similar_error: 'bg-amber-500/20 text-amber-400 border-amber-500/30',
  divergence_diff_revert: 'bg-rose-500/20 text-rose-400 border-rose-500/30',
  runaway_detected: 'bg-red-600/20 text-red-500 border-red-600/30',
};

const POLL_MS = 60_000;
const DIST_KEYS = ['1', '2', '3', '4+'] as const;

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
            className={`${colors[i]} rounded-sm shrink-0`}
            style={{ width: `${Math.max(pct, 2)}%`, height: `${Math.max(pct / 100 * 16, 2)}px` }}
          />
        );
      })}
    </div>
  );
}

function LoopCard({ name, data }: { name: string; data: LoopAggregate }) {
  if (data.task_count === 0) {
    return (
      <div className="bg-neutral-900/60 rounded-lg p-3 border border-neutral-800 flex flex-col gap-1 min-w-0">
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
    <div className="bg-neutral-900/60 rounded-lg p-3 border border-neutral-800 flex flex-col gap-2 min-w-0">
      <div className="flex items-center justify-between gap-2">
        <span className={`text-xs font-semibold uppercase tracking-wide ${LOOP_COLORS[name]}`}>{LOOP_LABELS[name]}</span>
        <span className="text-xs text-neutral-500">{data.task_count} task{data.task_count !== 1 ? 's' : ''}</span>
      </div>

      <div className="flex items-baseline gap-3">
        {data.avg_iterations != null && (
          <div className="flex flex-col">
            <span className="text-lg font-bold text-neutral-100 leading-none">{data.avg_iterations.toFixed(1)}</span>
            <span className="text-[10px] text-neutral-500">avg iter</span>
          </div>
        )}
        {convPct != null && (
          <div className="flex flex-col">
            <span className={`text-lg font-bold leading-none ${convColor}`}>{convPct}%</span>
            <span className="text-[10px] text-neutral-500">converged</span>
          </div>
        )}
      </div>

      <IterDistBar dist={data.iteration_distribution} />

      <div className="flex flex-wrap gap-1">
        {topReasons.map(([reason, count]) => {
          const cls = EXIT_REASON_COLORS[reason] ?? 'bg-neutral-500/20 text-neutral-400 border-neutral-500/30';
          return (
            <span key={reason} className={`inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded border ${cls}`}>
              {reason.replace(/_/g, ' ')}
              <span className="font-bold">{count}</span>
            </span>
          );
        })}
      </div>
    </div>
  );
}

export function FixLoopConvergenceWidget() {
  const [data, setData] = useState<ConvergenceData | null>(null);
  const [loading, setLoading] = useState(true);
  const [fetchError, setFetchError] = useState<string | null>(null);
  const mountedRef = useRef(true);

  const fetchData = useCallback(async () => {
    try {
      const res = await apiFetch('/api/admin/fix-loop-convergence?hours=24');
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
    } catch (e) {
      if (!mountedRef.current) return;
      setFetchError(e instanceof Error ? e.message : 'network error');
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    // eslint-disable-next-line react-hooks/set-state-in-effect -- async fetch sets state after await, intentional
    fetchData();
    const id = setInterval(() => {
      if (document.visibilityState === 'visible') fetchData();
    }, POLL_MS);
    return () => {
      mountedRef.current = false;
      clearInterval(id);
    };
  }, [fetchData]);

  if (loading) return null;
  if (fetchError) return null;
  if (!data || data.tasks_with_fix_loops === 0) return null;

  return (
    <section>
      <div className="flex items-center gap-2 mb-2">
        <p className="text-[10px] text-neutral-500 uppercase tracking-wide">Fix-Loop Convergence</p>
        <span className="text-[10px] text-neutral-600">{data.tasks_with_fix_loops} tasks · 24h</span>
      </div>
      <div className="grid grid-cols-3 gap-2">
        <LoopCard name="lint" data={data.lint} />
        <LoopCard name="test" data={data.test} />
        <LoopCard name="check" data={data.check} />
      </div>
      <div className="flex gap-3 mt-1.5 items-center">
        {DIST_KEYS.map((k, i) => {
          const colors = ['text-emerald-400', 'text-yellow-400', 'text-orange-400', 'text-red-400'];
          return (
            <span key={k} className={`text-[10px] ${colors[i]}`}>
              <span className="font-bold">{k}</span> iter
            </span>
          );
        })}
      </div>
    </section>
  );
}
