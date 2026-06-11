import { useCallback, useEffect, useState } from 'react';
import { Activity, AlertTriangle, ArrowDownRight, ArrowUpRight, ChevronDown, ChevronRight } from 'lucide-react';
import { fetchPipelineRegressions, resolveViewportFailureDownloadUrl, type PipelineRegression } from '../api';
import { usePoller, DEFAULT_POLL_MS } from '../hooks/usePoller';
import { formatAbsolute, parseTimestamp } from '../utils/time';

const STORAGE_KEY = 'blueprint-admin:pipelineRegressionsPanelCollapsed';
const LEGACY_STORAGE_KEY = 'blueprint-admin:pipelineRegressionsCollapsed';

interface PipelineRegressionsPanelProps {
  projectId: string | null;
}

const SEVERITY_RANK: Record<string, number> = { critical: 3, warning: 2, info: 1 };

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

function worstSeverity(regressions: PipelineRegression[]): string | null {
  let worst: string | null = null;
  let rank = 0;
  for (const regression of regressions) {
    const r = SEVERITY_RANK[regression.severity.toLowerCase()] ?? 0;
    if (r > rank) {
      rank = r;
      worst = regression.severity;
    }
  }
  return worst;
}

function isViewportRegression(regression: PipelineRegression): boolean {
  return regression.regression_class.startsWith('viewport_regression:')
    || regression.viewport_component != null
    || regression.viewport_width != null;
}

function viewportMetadata(regression: PipelineRegression): { component: string; width: number } | null {
  const component = regression.viewport_component;
  const width = regression.viewport_width;
  if (typeof component !== 'string' || component.trim().length === 0) return null;
  if (typeof width !== 'number' || !Number.isFinite(width) || width <= 0) return null;
  return { component, width };
}

interface ScreenshotResolution {
  key: string;
  url: string | null;
  message: string | null;
}

function ViewportRegressionScreenshot({
  projectId,
  regression,
}: {
  projectId: string;
  regression: PipelineRegression;
}) {
  const metadata = viewportMetadata(regression);
  const component = metadata?.component ?? null;
  const width = metadata?.width ?? null;
  const requestKey = component && width != null ? `${component}:${width}` : null;
  const [resolution, setResolution] = useState<ScreenshotResolution | null>(null);

  useEffect(() => {
    if (!component || width == null || !requestKey) return;
    const controller = new AbortController();
    resolveViewportFailureDownloadUrl(projectId, component, width, controller.signal)
      .then((resolvedUrl) => {
        if (controller.signal.aborted) return;
        setResolution({ key: requestKey, url: resolvedUrl, message: null });
      })
      .catch((error) => {
        if (controller.signal.aborted) return;
        const detail = error instanceof Error ? error.message : 'resolver failed';
        setResolution({ key: requestKey, url: null, message: `Screenshot unavailable: ${detail}` });
      });
    return () => controller.abort();
  }, [component, projectId, requestKey, width]);

  if (!metadata) {
    return (
      <div className="border-t border-neutral-800/80 px-3 py-2 text-[11px] text-amber-300">
        Screenshot unavailable: missing viewport metadata
      </div>
    );
  }

  const currentResolution = resolution?.key === requestKey ? resolution : null;
  const url = currentResolution?.url ?? null;
  const message = currentResolution ? currentResolution.message : 'Loading screenshot…';

  return (
    <div className="border-t border-neutral-800/80 px-3 py-2">
      <div className="mb-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-[10px] uppercase tracking-wide text-neutral-500">
        <span>Viewport screenshot</span>
        <span className="font-mono normal-case tracking-normal text-neutral-400">
          {metadata.component} · {metadata.width}px
        </span>
      </div>
      {message && (
        <div className="rounded border border-amber-500/20 bg-amber-500/10 px-2 py-1.5 text-[11px] text-amber-300">
          {message}
        </div>
      )}
      {url && (
        <img
          src={url}
          alt={`${metadata.component} viewport failure at ${metadata.width}px`}
          className="max-h-96 w-full rounded border border-neutral-800 bg-neutral-950 object-contain"
          onError={() => {
            setResolution({
              key: requestKey ?? '',
              url: null,
              message: 'Screenshot unavailable: image failed to load',
            });
          }}
        />
      )}
    </div>
  );
}

export function PipelineRegressionsPanel({ projectId }: PipelineRegressionsPanelProps) {
  const [regressions, setRegressions] = useState<PipelineRegression[]>([]);
  const [loading, setLoading] = useState(false);
  const [fetchError, setFetchError] = useState<string | null>(null);
  // null = no explicit operator preference yet; fall back to a health-derived default.
  const [collapsedPref, setCollapsedPref] = useState<boolean | null>(() => {
    const stored = localStorage.getItem(STORAGE_KEY) ?? localStorage.getItem(LEGACY_STORAGE_KEY);
    return stored === null ? null : stored === 'true';
  });
  const [expandedRows, setExpandedRows] = useState<Set<string>>(new Set());

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

  const toggleRow = useCallback((key: string) => {
    setExpandedRows((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }, []);

  if (!projectId) return null;

  const healthy = !loading && !fetchError && regressions.length === 0;
  const hasActive = !loading && !fetchError && regressions.length > 0;
  // Default: collapsed when healthy, expanded when there are active regressions.
  const collapsed = collapsedPref ?? healthy;
  const worst = hasActive ? worstSeverity(regressions) : null;

  const togglePanel = () => {
    setCollapsedPref((prev) => {
      const next = !(prev ?? healthy);
      try {
        localStorage.setItem(STORAGE_KEY, String(next));
      } catch { /* localStorage unavailable — keep in-memory state */ }
      return next;
    });
  };

  return (
    <div className="mb-3 shrink-0">
      <button
        type="button"
        onClick={togglePanel}
        data-testid="pipeline-regressions-toggle"
        aria-expanded={!collapsed}
        className="flex min-h-[44px] w-full items-center gap-2 px-1 py-1 text-[11px] font-bold uppercase tracking-widest text-neutral-400 transition-colors hover:text-neutral-300"
      >
        {collapsed ? <ChevronRight className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
        <Activity className="h-3 w-3" />
        Pipeline regressions
        {hasActive && (
          <span className="ml-1 rounded-full border border-red-500/30 bg-red-500/15 px-1.5 py-0.5 text-[10px] font-bold leading-none text-red-300">
            {regressions.length} active
          </span>
        )}
        {collapsed && worst && (
          <span
            data-testid="pipeline-regression-worst-severity"
            className={`rounded border px-1.5 py-0.5 text-[10px] font-bold uppercase leading-none ${severityClasses(worst)}`}
          >
            {worst}
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

          {hasActive && (
            <div className="space-y-1.5">
              {regressions.map((regression) => {
                const key = `${regression.regression_class}:${regression.signal_name}`;
                const isOpen = expandedRows.has(key);
                const detectedAt = regression.detected_at
                  ? formatAbsolute(parseTimestamp(regression.detected_at).getTime())
                  : '—';
                return (
                  <div
                    key={key}
                    data-testid="pipeline-regression-row"
                    className={`rounded-lg border bg-neutral-950/60 ${rowBorderClasses(regression.severity)}`}
                  >
                    <button
                      type="button"
                      onClick={() => toggleRow(key)}
                      aria-expanded={isOpen}
                      className="flex min-h-[44px] w-full items-center gap-2 px-3 py-2 text-left"
                    >
                      {isOpen ? (
                        <ChevronDown className="h-3 w-3 shrink-0 text-neutral-500" />
                      ) : (
                        <ChevronRight className="h-3 w-3 shrink-0 text-neutral-500" />
                      )}
                      <span className={`shrink-0 rounded border px-1.5 py-0.5 text-[10px] font-bold uppercase leading-none ${severityClasses(regression.severity)}`}>
                        {regression.severity}
                      </span>
                      <span className="min-w-0 flex-1">
                        <span className="block truncate font-medium text-neutral-200">
                          {regression.signal_name}
                        </span>
                        <span className="block truncate font-mono text-[10px] text-neutral-500">
                          {regression.regression_class}
                        </span>
                      </span>
                      <span className="shrink-0 inline-flex items-center gap-1 text-neutral-300">
                        <ArrowDownRight className="h-3 w-3 shrink-0 text-neutral-500" />
                        {formatRateChange(regression)}
                      </span>
                      <span className="hidden shrink-0 text-[10px] text-neutral-500 sm:block">
                        {detectedAt}
                      </span>
                    </button>

                    {isOpen && (
                      <>
                        <div className="grid grid-cols-1 gap-2 border-t border-neutral-800/80 px-3 py-2 sm:grid-cols-3">
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
                        {isViewportRegression(regression) && (
                          <ViewportRegressionScreenshot projectId={projectId} regression={regression} />
                        )}
                      </>
                    )}
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
