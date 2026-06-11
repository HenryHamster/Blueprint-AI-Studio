import { useCallback, useEffect, useState } from 'react';
import { Activity, AlertTriangle, ArrowDownRight, ArrowUpRight, ChevronDown, ChevronUp } from 'lucide-react';
import { fetchPipelineRegressions, type PipelineRegression } from '../api';
import { usePoller, DEFAULT_POLL_MS } from '../hooks/usePoller';
import { formatAbsolute, parseTimestamp } from '../utils/time';

const STORAGE_KEY = 'blueprint-admin:pipelineRegressionsPanelCollapsed';

interface PipelineRegressionsPanelProps {
  projectId: string | null;
}

function formatRate(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return '—';
  return `${(value * 100).toFixed(1)}%`;
}

function formatRateChange(regression: PipelineRegression): string {
  if (regression.current_rate == null || regression.prior_rate == null) return '—';
  const delta = regression.current_rate - regression.prior_rate;
  const sign = delta > 0 ? '+' : '';
  return `${sign}${(delta * 100).toFixed(1)} pts`;
}

function severityClasses(severity: string): string {
  switch (severity.toLowerCase()) {
    case 'critical':
      return 'bg-red-500/15 text-red-300 border-red-500/30';
    case 'warning':
      return 'bg-amber-500/15 text-amber-300 border-amber-500/30';
    case 'info':
      return 'bg-sky-500/15 text-sky-300 border-sky-500/30';
    default:
      return 'bg-neutral-800 text-neutral-300 border-neutral-700';
  }
}

function rowBorderClasses(severity: string): string {
  switch (severity.toLowerCase()) {
    case 'critical':
      return 'border-red-500/20';
    case 'warning':
      return 'border-amber-500/20';
    case 'info':
      return 'border-sky-500/20';
    default:
      return 'border-neutral-800/80';
  }
}

export function PipelineRegressionsPanel({ projectId }: PipelineRegressionsPanelProps) {
  const [regressions, setRegressions] = useState<PipelineRegression[]>([]);
  const [loading, setLoading] = useState(false);
  const [fetchError, setFetchError] = useState<string | null>(null);
  const [collapsed, setCollapsed] = useState(() => localStorage.getItem(STORAGE_KEY) === 'true');

  const toggle = () => {
    const next = !collapsed;
    setCollapsed(next);
    localStorage.setItem(STORAGE_KEY, String(next));
  };

  const load = useCallback(async (signal?: AbortSignal) => {
    if (!projectId) return;
    setLoading(true);
    setFetchError(null);
    try {
      const data = await fetchPipelineRegressions(projectId, signal);
      if (signal?.aborted) return;
      setRegressions(data.regressions);
    } catch (e) {
      if (e instanceof Error && e.name === 'AbortError') return;
      setRegressions([]);
      setFetchError(e instanceof Error ? e.message : 'Failed to load pipeline regressions');
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    setRegressions([]);
    setFetchError(null);
    setLoading(Boolean(projectId));
  }, [projectId]);

  usePoller((signal) => load(signal), {
    intervalMs: DEFAULT_POLL_MS,
    enabled: !!projectId,
    deps: [projectId],
  });

  if (!projectId) return null;

  const healthy = !loading && !fetchError && regressions.length === 0;

  return (
    <div className="mb-3 shrink-0">
      <button
        onClick={toggle}
        data-testid="pipeline-regressions-toggle"
        className="flex min-h-[44px] items-center gap-2 px-1 py-1 text-[11px] font-bold uppercase tracking-widest text-neutral-400 transition-colors hover:text-neutral-300"
      >
        {collapsed ? <ChevronDown className="h-3 w-3" /> : <ChevronUp className="h-3 w-3" />}
        <Activity className="h-3 w-3" />
        Pipeline regressions
        {!loading && !fetchError && regressions.length > 0 && (
          <span className="ml-1 rounded-full border border-red-500/30 bg-red-500/15 px-1.5 py-0.5 text-[10px] font-bold leading-none text-red-300">
            {regressions.length} active
          </span>
        )}
        {healthy && (
          <span className="ml-1 rounded-full border border-emerald-500/30 bg-emerald-500/15 px-1.5 py-0.5 text-[10px] font-bold leading-none text-emerald-300">
            Healthy
          </span>
        )}
        {collapsed && fetchError && (
          <span className="ml-1 rounded-full border border-red-500/30 bg-red-500/15 px-1.5 py-0.5 text-[10px] font-bold leading-none text-red-300">
            Error
          </span>
        )}
      </button>

      {!collapsed && (
      <div className="mt-1 rounded-lg border border-neutral-800 bg-neutral-900 p-3 text-xs">
        {fetchError && (
          <div className="flex items-start gap-2 rounded border border-red-500/20 bg-red-500/10 px-3 py-2 text-red-300">
            <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" />
            <span className="min-w-0 break-words">{fetchError}</span>
          </div>
        )}

        {loading && !fetchError && regressions.length === 0 && (
          <div className="px-1 py-2 text-neutral-500">Loading pipeline regressions…</div>
        )}

        {healthy && (
          <div className="flex items-center gap-2 rounded border border-emerald-500/20 bg-emerald-500/10 px-3 py-2 text-emerald-300">
            <span className="h-2 w-2 rounded-full bg-emerald-400" />
            <span>No active pipeline regressions</span>
          </div>
        )}

        {!loading && !fetchError && regressions.length > 0 && (
          <div className="space-y-2">
            {regressions.map((regression) => {
              const detectedAt = regression.detected_at ? formatAbsolute(parseTimestamp(regression.detected_at).getTime()) : '—';
              return (
                <div
                  key={`${regression.regression_class}:${regression.signal_name}`}
                  data-testid="pipeline-regression-row"
                  className={`rounded-lg border bg-neutral-950/60 px-3 py-2 ${rowBorderClasses(regression.severity)}`}
                >
                  <div className="flex items-start gap-2">
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-start gap-2">
                        <div className="min-w-0 flex-1">
                          <div className="break-words font-medium text-neutral-200">
                            {regression.signal_name}
                          </div>
                          <div className="mt-0.5 break-words font-mono text-[10px] text-neutral-500">
                            {regression.regression_class}
                          </div>
                        </div>
                        <span className={`shrink-0 rounded border px-1.5 py-0.5 text-[10px] font-bold uppercase leading-none ${severityClasses(regression.severity)}`}>
                          {regression.severity}
                        </span>
                      </div>

                      <div className="mt-2 grid grid-cols-1 gap-2 sm:grid-cols-3">
                        <div>
                          <div className="text-[10px] uppercase tracking-wide text-neutral-500">Detected</div>
                          <div className="mt-0.5 break-words text-neutral-300">{detectedAt}</div>
                        </div>
                        <div>
                          <div className="text-[10px] uppercase tracking-wide text-neutral-500">Current</div>
                          <div className="mt-0.5 flex items-center gap-1.5 text-neutral-300">
                            <ArrowUpRight className="h-3 w-3 shrink-0 text-sky-400" />
                            <span>{formatRate(regression.current_rate)}</span>
                          </div>
                        </div>
                        <div>
                          <div className="text-[10px] uppercase tracking-wide text-neutral-500">Prior / change</div>
                          <div className="mt-0.5 flex flex-wrap items-center gap-x-1.5 gap-y-0.5 text-neutral-300">
                            <span>{formatRate(regression.prior_rate)}</span>
                            <span className="text-neutral-600">·</span>
                            <span className="inline-flex items-center gap-1">
                              <ArrowDownRight className="h-3 w-3 shrink-0 text-neutral-500" />
                              {formatRateChange(regression)}
                            </span>
                          </div>
                        </div>
                      </div>
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>
      )}
    </div>
  );
}

export default PipelineRegressionsPanel;
