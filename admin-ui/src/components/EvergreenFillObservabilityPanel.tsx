import { useCallback, useState } from 'react';
import { ChevronDown, ChevronRight } from 'lucide-react';
import {
  fetchEvergreenFillObservability,
  type EvergreenFillObservabilityResponse,
} from '../api';
import { usePoller, DEFAULT_POLL_MS } from '../hooks/usePoller';

export function EvergreenFillObservabilityPanel() {
  const [data, setData] = useState<EvergreenFillObservabilityResponse | null>(null);
  const [fetchError, setFetchError] = useState<string | null>(null);
  const [collapsed, setCollapsed] = useState(false);

  const load = useCallback(async (signal?: AbortSignal) => {
    setFetchError(null);
    try {
      const result = await fetchEvergreenFillObservability(signal);
      if (signal?.aborted) return;
      setData(result);
    } catch (e) {
      if (signal?.aborted) return;
      setFetchError(e instanceof Error ? e.message : 'Failed to load');
    }
  }, []);

  usePoller(load, { intervalMs: DEFAULT_POLL_MS, enabled: true });

  const reasonEntries = Object.entries(data?.reason_counts ?? {}).sort((a, b) => b[1] - a[1]);
  const triggerEntries = Object.entries(data?.trigger_counts ?? {}).sort((a, b) => b[1] - a[1]);
  const recent = data?.recent ?? [];

  return (
    <div className="border border-neutral-800 rounded-lg overflow-hidden" data-testid="evergreen-fill-observability-panel">
      <button
        className="w-full flex items-center justify-between px-4 py-3 bg-neutral-900 hover:bg-neutral-800/60 transition-colors"
        onClick={() => setCollapsed(v => !v)}
      >
        <div className="flex items-center gap-2">
          {collapsed ? <ChevronRight className="w-4 h-4 text-neutral-500" /> : <ChevronDown className="w-4 h-4 text-neutral-500" />}
          <span className="text-sm font-medium text-neutral-200">Fill Observability</span>
          {data != null && (
            <span className="text-xs text-neutral-500 ml-1">
              {data.created_count} created
            </span>
          )}
        </div>
      </button>

      {!collapsed && (
        <div className="px-4 py-3 space-y-4">
          {fetchError && (
            <p className="text-xs text-red-400">{fetchError}</p>
          )}

          {data == null && !fetchError && (
            <p className="text-xs text-neutral-500">Loading…</p>
          )}

          {data != null && (
            <>
              <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
                <div className="bg-neutral-900 rounded-lg px-3 py-2 border border-neutral-800">
                  <p className="text-[10px] text-neutral-500 uppercase tracking-wider mb-1">Items Created</p>
                  <p className="text-lg font-mono text-neutral-100">{data.created_count}</p>
                </div>
                <div className="bg-neutral-900 rounded-lg px-3 py-2 border border-neutral-800">
                  <p className="text-[10px] text-neutral-500 uppercase tracking-wider mb-1">Reason Types</p>
                  <p className="text-lg font-mono text-neutral-100">{reasonEntries.length}</p>
                </div>
                <div className="bg-neutral-900 rounded-lg px-3 py-2 border border-neutral-800">
                  <p className="text-[10px] text-neutral-500 uppercase tracking-wider mb-1">Trigger Types</p>
                  <p className="text-lg font-mono text-neutral-100">{triggerEntries.length}</p>
                </div>
              </div>

              {reasonEntries.length > 0 && (
                <div>
                  <p className="text-xs font-semibold text-neutral-400 uppercase tracking-wider mb-2">Reason Counts</p>
                  <div className="space-y-1">
                    {reasonEntries.map(([reason, count]) => (
                      <div key={reason} className="flex items-center justify-between text-xs">
                        <span className="text-neutral-300 font-mono">{reason}</span>
                        <span className="text-neutral-500 font-mono">{count}</span>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {triggerEntries.length > 0 && (
                <div>
                  <p className="text-xs font-semibold text-neutral-400 uppercase tracking-wider mb-2">Trigger Counts</p>
                  <div className="space-y-1">
                    {triggerEntries.map(([trigger, count]) => (
                      <div key={trigger} className="flex items-center justify-between text-xs">
                        <span className="text-neutral-300 font-mono">{trigger}</span>
                        <span className="text-neutral-500 font-mono">{count}</span>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {recent.length > 0 && (
                <div>
                  <p className="text-xs font-semibold text-neutral-400 uppercase tracking-wider mb-2">Recent Decisions</p>
                  <div className="overflow-x-auto">
                    <table className="w-full text-xs">
                      <thead>
                        <tr className="text-left text-[10px] text-neutral-500 border-b border-neutral-800">
                          <th className="pb-1 pr-3 font-medium">Action</th>
                          <th className="pb-1 pr-3 font-medium">Reason</th>
                          <th className="pb-1 pr-3 font-medium">Trigger</th>
                          <th className="pb-1 font-medium">Project</th>
                        </tr>
                      </thead>
                      <tbody>
                        {recent.slice().reverse().map((d, i) => (
                          <tr key={i} className="border-b border-neutral-800/50 last:border-0">
                            <td className="py-1 pr-3 font-mono text-neutral-300">{d.action}</td>
                            <td className="py-1 pr-3 text-neutral-400">{d.reason}</td>
                            <td className="py-1 pr-3 text-neutral-500">{d.trigger ?? '—'}</td>
                            <td className="py-1 text-neutral-600 font-mono text-[10px]">
                              {d.project_id ? d.project_id.slice(0, 8) + '…' : '—'}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              )}

              {recent.length === 0 && reasonEntries.length === 0 && (
                <p className="text-xs text-neutral-600">No fill decisions recorded yet.</p>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
}
