import { useCallback, useState } from 'react';
import { ChevronDown, ChevronRight } from 'lucide-react';
import {
  fetchStructuralHealthObservability,
  type StructuralHealthObservabilityResponse,
} from '../api';
import { usePoller, DEFAULT_POLL_MS } from '../hooks/usePoller';

function fmtBytes(b: number): string {
  if (b >= 1_048_576) return `${(b / 1_048_576).toFixed(1)} MiB`;
  if (b >= 1_024) return `${(b / 1_024).toFixed(1)} KiB`;
  return `${b} B`;
}

export function StructuralHealthObservabilityPanel() {
  const [data, setData] = useState<StructuralHealthObservabilityResponse | null>(null);
  const [fetchError, setFetchError] = useState<string | null>(null);
  const [collapsed, setCollapsed] = useState(false);

  const load = useCallback(async (signal?: AbortSignal) => {
    setFetchError(null);
    try {
      const result = await fetchStructuralHealthObservability(signal);
      if (signal?.aborted) return;
      setData(result);
    } catch (e) {
      if (signal?.aborted) return;
      setFetchError(e instanceof Error ? e.message : 'Failed to load');
    }
  }, []);

  usePoller(load, { intervalMs: DEFAULT_POLL_MS, enabled: true });

  const largestRows = data?.largest_rows ?? [];
  const recent = data?.recent ?? [];

  return (
    <div className="border border-neutral-800 rounded-lg overflow-hidden" data-testid="structural-health-observability-panel">
      <button
        className="w-full flex items-center justify-between px-4 py-3 bg-neutral-900 hover:bg-neutral-800/60 transition-colors"
        onClick={() => setCollapsed(v => !v)}
      >
        <div className="flex items-center gap-2">
          {collapsed ? <ChevronRight className="w-4 h-4 text-neutral-500" /> : <ChevronDown className="w-4 h-4 text-neutral-500" />}
          <span className="text-sm font-medium text-neutral-200">Structural Health Observability</span>
          {data != null && (
            <span className="text-xs text-neutral-500 ml-1">
              {data.cycles} cycles · {data.enrolled_total} enrolled
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
                  <p className="text-[10px] text-neutral-500 uppercase tracking-wider mb-1">Audit Cycles</p>
                  <p className="text-lg font-mono text-neutral-100">{data.cycles}</p>
                </div>
                <div className="bg-neutral-900 rounded-lg px-3 py-2 border border-neutral-800">
                  <p className="text-[10px] text-neutral-500 uppercase tracking-wider mb-1">Enrolled Total</p>
                  <p className="text-lg font-mono text-neutral-100">{data.enrolled_total}</p>
                </div>
                <div className="bg-neutral-900 rounded-lg px-3 py-2 border border-neutral-800">
                  <p className="text-[10px] text-neutral-500 uppercase tracking-wider mb-1">Largest Rows</p>
                  <p className="text-lg font-mono text-neutral-100">{largestRows.length}</p>
                </div>
              </div>

              {largestRows.length > 0 && (
                <div>
                  <p className="text-xs font-semibold text-neutral-400 uppercase tracking-wider mb-2">Largest task.context Rows</p>
                  <div className="overflow-x-auto">
                    <table className="w-full text-xs">
                      <thead>
                        <tr className="text-left text-[10px] text-neutral-500 border-b border-neutral-800">
                          <th className="pb-1 pr-3 font-medium">Size</th>
                          <th className="pb-1 pr-3 font-medium">Task ID</th>
                          <th className="pb-1 font-medium">Project</th>
                        </tr>
                      </thead>
                      <tbody>
                        {largestRows.map((r, i) => (
                          <tr key={i} className="border-b border-neutral-800/50 last:border-0">
                            <td className="py-1 pr-3 font-mono text-neutral-300">{fmtBytes(r.bytes)}</td>
                            <td className="py-1 pr-3 text-neutral-400 font-mono text-[10px]">{r.task_id.slice(0, 8)}…</td>
                            <td className="py-1 text-neutral-600 font-mono text-[10px]">{r.project_id.slice(0, 8)}…</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              )}

              {recent.length > 0 && (
                <div>
                  <p className="text-xs font-semibold text-neutral-400 uppercase tracking-wider mb-2">Recent Audit Decisions</p>
                  <div className="overflow-x-auto">
                    <table className="w-full text-xs">
                      <thead>
                        <tr className="text-left text-[10px] text-neutral-500 border-b border-neutral-800">
                          <th className="pb-1 pr-3 font-medium">Action</th>
                          <th className="pb-1 pr-3 font-medium">Check</th>
                          <th className="pb-1 font-medium">Enrolled</th>
                        </tr>
                      </thead>
                      <tbody>
                        {recent.slice().reverse().map((d, i) => (
                          <tr key={i} className="border-b border-neutral-800/50 last:border-0">
                            <td className="py-1 pr-3 font-mono text-neutral-300">{String(d.action ?? '—')}</td>
                            <td className="py-1 pr-3 text-neutral-400">{String(d.check ?? 'fat_row')}</td>
                            <td className="py-1 text-neutral-500 font-mono">{String(d.enrolled ?? 0)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              )}

              {recent.length === 0 && largestRows.length === 0 && (
                <p className="text-xs text-neutral-600">No structural health audit cycles recorded yet.</p>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
}
