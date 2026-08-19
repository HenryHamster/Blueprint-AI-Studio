import { useCallback, useState } from 'react';
import { ChevronDown, ChevronRight } from 'lucide-react';
import {
  fetchEvergreenBacklog,
  fetchEvergreenBacklogSummary,
  type EvergreenBacklogItem,
  type EvergreenBacklogSummaryResponse,
} from '../api';
import { usePoller, DEFAULT_POLL_MS } from '../hooks/usePoller';
import { formatAbsolute } from '../utils/time';

interface EvergreenBacklogPanelProps {
  projectId: string | null;
}

function statusBadgeClasses(status: string): string {
  switch (status) {
    case 'enrolled': return 'border-blue-500/30 bg-blue-500/15 text-blue-300';
    case 'spawned': return 'border-amber-500/30 bg-amber-500/15 text-amber-300';
    case 'done': return 'border-emerald-500/30 bg-emerald-500/15 text-emerald-300';
    case 'retired': return 'border-neutral-600 bg-workbench-panel text-neutral-500';
    default: return 'border-neutral-700 bg-workbench-panel/50 text-neutral-400';
  }
}

export function EvergreenBacklogPanel({ projectId }: EvergreenBacklogPanelProps) {
  const [items, setItems] = useState<EvergreenBacklogItem[]>([]);
  const [summary, setSummary] = useState<EvergreenBacklogSummaryResponse | null>(null);
  const [fetchError, setFetchError] = useState<string | null>(null);
  const [collapsed, setCollapsed] = useState(false);

  const load = useCallback(async (signal?: AbortSignal) => {
    if (!projectId) return;
    setFetchError(null);
    try {
      const [backlogData, summaryData] = await Promise.all([
        fetchEvergreenBacklog(projectId, signal),
        fetchEvergreenBacklogSummary(projectId, signal),
      ]);
      if (signal?.aborted) return;
      setItems(backlogData.items ?? []);
      setSummary(summaryData);
    } catch (e) {
      if (signal?.aborted) return;
      setFetchError(e instanceof Error ? e.message : 'Failed to load');
    }
  }, [projectId]);

  usePoller(load, { intervalMs: DEFAULT_POLL_MS, enabled: !!projectId, deps: [projectId] });

  const totalItems = items.length;

  return (
    <div className="border border-neutral-800 rounded-lg overflow-hidden" data-testid="evergreen-backlog-panel">
      <button
        className="w-full flex items-center justify-between px-4 py-3 bg-workbench-base hover:bg-workbench-hover/60 transition-colors"
        onClick={() => setCollapsed(v => !v)}
      >
        <div className="flex items-center gap-2">
          {collapsed ? <ChevronRight className="w-4 h-4 text-neutral-500" /> : <ChevronDown className="w-4 h-4 text-neutral-500" />}
          <span className="text-sm font-medium text-neutral-200">Evergreen Backlog</span>
          {totalItems > 0 && (
            <span className="text-xs text-neutral-500 ml-1">{totalItems} item{totalItems !== 1 ? 's' : ''}</span>
          )}
        </div>
      </button>

      {!collapsed && (
        <div className="px-4 py-3 space-y-4">
          {!projectId && (
            <p className="text-xs text-neutral-500">Select a project to view its backlog.</p>
          )}

          {fetchError && (
            <p className="text-xs text-red-400">{fetchError}</p>
          )}

          {projectId && summary != null && (
            <div className="flex flex-wrap gap-3">
              {Object.entries(summary).map(([status, count]) => (
                <div key={status} className="flex items-center gap-1.5">
                  <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-ui-sm font-medium border ${statusBadgeClasses(status)}`}>
                    {status}
                  </span>
                  <span className="text-xs font-mono text-neutral-400">{count}</span>
                </div>
              ))}
            </div>
          )}

          {projectId && items.length === 0 && !fetchError && (
            <p className="text-xs text-neutral-600">No backlog items.</p>
          )}

          {items.length > 0 && (
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr className="text-left text-ui-sm text-neutral-500 border-b border-neutral-800">
                    <th className="pb-1 pr-3 font-medium">Title</th>
                    <th className="pb-1 pr-3 font-medium">Status</th>
                    <th className="pb-1 pr-3 font-medium">Provenance</th>
                    <th className="pb-1 pr-3 font-medium">Attempts</th>
                    <th className="pb-1 pr-3 font-medium">Task</th>
                    <th className="pb-1 font-medium">Updated</th>
                  </tr>
                </thead>
                <tbody>
                  {items.map(item => (
                    <tr key={item.id} className="border-b border-neutral-800/50 last:border-0 hover:bg-workbench-hover/20">
                      <td className="py-1.5 pr-3 max-w-[200px]">
                        <span className="text-neutral-200 truncate block" title={item.title}>{item.title}</span>
                        <span className="text-ui-sm text-neutral-600 font-mono">{item.finding_key}</span>
                      </td>
                      <td className="py-1.5 pr-3">
                        <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-ui-sm font-medium border ${statusBadgeClasses(item.status)}`}>
                          {item.status}
                        </span>
                      </td>
                      <td className="py-1.5 pr-3 text-neutral-500">{item.provenance}</td>
                      <td className="py-1.5 pr-3 font-mono text-neutral-400">{item.attempts}</td>
                      <td className="py-1.5 pr-3">
                        {item.spawned_task_id ? (
                          <span className="font-mono text-ui-sm text-neutral-400" title={item.spawned_task_id}>
                            {item.spawned_task_id.slice(0, 8)}…
                          </span>
                        ) : (
                          <span className="text-neutral-700">—</span>
                        )}
                      </td>
                      <td className="py-1.5 text-neutral-600">
                        {item.updated_at ? formatAbsolute(item.updated_at) : '—'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
