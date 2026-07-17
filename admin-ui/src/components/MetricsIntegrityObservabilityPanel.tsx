import { useCallback, useState } from 'react';
import { ChevronDown, ChevronRight } from 'lucide-react';
import {
  fetchMetricsIntegrityObservability,
  type MetricsIntegrityObservabilityResponse,
  type MetricsIntegrityFlag,
} from '../api';
import { usePoller, DEFAULT_POLL_MS } from '../hooks/usePoller';

export function MetricsIntegrityObservabilityPanel({
  sweepMode,
  onSweepModeChange,
}: {
  sweepMode?: string;
  onSweepModeChange?: (mode: string) => Promise<void>;
} = {}) {
  const [data, setData] = useState<MetricsIntegrityObservabilityResponse | null>(null);
  const [fetchError, setFetchError] = useState<string | null>(null);
  const [collapsed, setCollapsed] = useState(false);

  const load = useCallback(async (signal?: AbortSignal) => {
    setFetchError(null);
    try {
      const result = await fetchMetricsIntegrityObservability(signal);
      if (signal?.aborted) return;
      setData(result);
    } catch (e) {
      if (signal?.aborted) return;
      setFetchError(e instanceof Error ? e.message : 'Failed to load');
    }
  }, []);

  usePoller(load, { intervalMs: DEFAULT_POLL_MS, enabled: true });

  const recentFlags: MetricsIntegrityFlag[] = data?.recent_flags ?? [];

  return (
    <div className="border border-neutral-800 rounded-lg overflow-hidden" data-testid="metrics-integrity-observability-panel">
      <button
        className="w-full flex items-center justify-between px-4 py-3 bg-neutral-900 hover:bg-neutral-800/60 transition-colors"
        onClick={() => setCollapsed(v => !v)}
      >
        <div className="flex items-center gap-2">
          {collapsed ? <ChevronRight className="w-4 h-4 text-neutral-500" /> : <ChevronDown className="w-4 h-4 text-neutral-500" />}
          <span className="text-sm font-medium text-neutral-200">Metrics Integrity Observability</span>
          {data != null && (
            <span className="text-xs text-neutral-500 ml-1">
              {data.ticks} ticks · {data.flags_total} flags
            </span>
          )}
        </div>
      </button>

      {!collapsed && (
        <div className="px-4 py-3 space-y-4">
          {sweepMode != null && onSweepModeChange != null && (
            <div>
              <label className="block text-xs font-medium text-neutral-400 mb-1.5">Mode</label>
              <div className="flex gap-1 bg-neutral-950 border border-neutral-700 rounded-lg p-1 w-fit">
                {(['off', 'observe', 'enforce'] as const).map(mode => (
                  <button
                    key={mode}
                    onClick={() => void onSweepModeChange(mode)}
                    className={`px-3 py-1.5 text-xs rounded-md transition-colors capitalize ${
                      sweepMode === mode
                        ? 'bg-neutral-700 text-neutral-100'
                        : 'text-neutral-500 hover:text-neutral-300'
                    }`}
                  >
                    {mode}
                  </button>
                ))}
              </div>
            </div>
          )}

          {fetchError && (
            <p className="text-xs text-red-400">{fetchError}</p>
          )}

          {data == null && !fetchError && (
            <p className="text-xs text-neutral-500">Loading…</p>
          )}

          {data != null && (
            <>
              <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                <div className="bg-neutral-900 rounded-lg px-3 py-2 border border-neutral-800">
                  <p className="text-ui-sm text-neutral-500 uppercase tracking-wider mb-1">Ticks</p>
                  <p className="text-lg font-mono text-neutral-100">{data.ticks}</p>
                </div>
                <div className="bg-neutral-900 rounded-lg px-3 py-2 border border-neutral-800">
                  <p className="text-ui-sm text-neutral-500 uppercase tracking-wider mb-1">Checks Run</p>
                  <p className="text-lg font-mono text-neutral-100">{data.checks_run}</p>
                </div>
                <div className="bg-neutral-900 rounded-lg px-3 py-2 border border-neutral-800">
                  <p className="text-ui-sm text-neutral-500 uppercase tracking-wider mb-1">Flags Total</p>
                  <p className={`text-lg font-mono ${data.flags_total > 0 ? 'text-yellow-400' : 'text-neutral-100'}`}>{data.flags_total}</p>
                </div>
                <div className="bg-neutral-900 rounded-lg px-3 py-2 border border-neutral-800">
                  <p className="text-ui-sm text-neutral-500 uppercase tracking-wider mb-1">Errors Total</p>
                  <p className={`text-lg font-mono ${data.errors_total > 0 ? 'text-red-400' : 'text-neutral-100'}`}>{data.errors_total}</p>
                </div>
                <div className="bg-neutral-900 rounded-lg px-3 py-2 border border-neutral-800">
                  <p className="text-ui-sm text-neutral-500 uppercase tracking-wider mb-1">Auto-Resolved</p>
                  <p className={`text-lg font-mono ${(data.auto_resolved_total ?? 0) > 0 ? 'text-green-400' : 'text-neutral-100'}`}>{data.auto_resolved_total ?? 0}</p>
                </div>
                <div className="bg-neutral-900 rounded-lg px-3 py-2 border border-neutral-800">
                  <p className="text-ui-sm text-neutral-500 uppercase tracking-wider mb-1">Backing Path</p>
                  <p className="text-lg font-mono text-neutral-100">{data.auto_resolved_backing_path_total ?? 0}</p>
                </div>
                <div className="bg-neutral-900 rounded-lg px-3 py-2 border border-neutral-800">
                  <p className="text-ui-sm text-neutral-500 uppercase tracking-wider mb-1">Named Test</p>
                  <p className="text-lg font-mono text-neutral-100">{data.auto_resolved_named_test_present_total ?? 0}</p>
                </div>
              </div>

              {data.last_tick_at != null && (
                <p className="text-xs text-neutral-600">
                  Last tick: <span className="font-mono">{data.last_tick_at}</span>
                </p>
              )}

              {recentFlags.length > 0 && (
                <div>
                  <p className="text-xs font-semibold text-neutral-400 uppercase tracking-wider mb-2">Recent Flags</p>
                  <div className="overflow-x-auto">
                    <table className="w-full text-xs">
                      <thead>
                        <tr className="text-left text-ui-sm text-neutral-500 border-b border-neutral-800">
                          <th className="pb-1 pr-3 font-medium">Check</th>
                          <th className="pb-1 pr-3 font-medium">Subject</th>
                          <th className="pb-1 pr-3 font-medium">Self-Reported</th>
                          <th className="pb-1 pr-3 font-medium">Ground Truth</th>
                          <th className="pb-1 pr-3 font-medium">Delta</th>
                          <th className="pb-1 font-medium">Detail</th>
                        </tr>
                      </thead>
                      <tbody>
                        {recentFlags.slice().reverse().map((f, i) => (
                          <tr key={i} className="border-b border-neutral-800/50 last:border-0">
                            <td className="py-1 pr-3 font-mono text-neutral-300">{f.check ?? '—'}</td>
                            <td className="py-1 pr-3 text-neutral-400 truncate max-w-24">{f.subject ?? '—'}</td>
                            <td className="py-1 pr-3 font-mono text-neutral-500">{f.self_reported != null ? String(f.self_reported) : '—'}</td>
                            <td className="py-1 pr-3 font-mono text-neutral-300">{f.ground_truth != null ? String(f.ground_truth) : '—'}</td>
                            <td className="py-1 pr-3 font-mono text-yellow-400">{f.delta != null ? String(f.delta) : '—'}</td>
                            <td className="py-1 text-neutral-400 truncate max-w-48">{f.detail || '—'}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              )}

              {recentFlags.length === 0 && data.ticks > 0 && (
                <p className="text-xs text-neutral-600">No drift flags recorded in recent ticks.</p>
              )}

              {data.ticks === 0 && (
                <p className="text-xs text-neutral-600">No metrics integrity sweep ticks recorded yet.</p>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
}
