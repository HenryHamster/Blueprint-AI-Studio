/* eslint-disable react-hooks/set-state-in-effect */
import { useCallback, useEffect, useRef, useState } from 'react';
import { WorkbenchPanel } from './WorkbenchLayout';
import {
  type CompletionDebtEntry,
  dismissCompletionDebt,
  fetchCompletionDebts,
  resolveCompletionDebt,
} from '../api';

interface CompletionDebtPanelProps {
  projectId: string;
  addToast: (message: string, type: 'success' | 'error' | 'info') => void;
}

type StatusFilter = 'open' | 'dismissed' | 'resolved' | 'all';

const STATUS_FILTERS: { value: StatusFilter; label: string }[] = [
  { value: 'all', label: 'All' },
  { value: 'open', label: 'Open' },
  { value: 'dismissed', label: 'Dismissed' },
  { value: 'resolved', label: 'Resolved' },
];

function statusPillClass(status: string): string {
  if (status === 'open') return 'bg-amber-900/40 text-amber-300 border-amber-700/50';
  if (status === 'dismissed') return 'bg-workbench-panel/60 text-neutral-400 border-neutral-700/50';
  return 'bg-emerald-900/40 text-emerald-300 border-emerald-700/50';
}

function relativeAge(isoDate: string): string {
  const diffMs = Date.now() - new Date(isoDate).getTime();
  const days = Math.floor(diffMs / 86400000);
  if (days === 0) return 'today';
  if (days === 1) return '1d ago';
  if (days < 30) return `${days}d ago`;
  const weeks = Math.floor(days / 7);
  if (weeks < 8) return `${weeks}w ago`;
  return `${Math.floor(days / 30)}mo ago`;
}

export function CompletionDebtPanel({ projectId, addToast }: CompletionDebtPanelProps) {
  const [entries, setEntries] = useState<CompletionDebtEntry[]>([]);
  const [totalCount, setTotalCount] = useState<number>(0);
  const [loading, setLoading] = useState(false);
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('open');
  const [dismissingId, setDismissingId] = useState<string | null>(null);
  const [dismissReason, setDismissReason] = useState('');
  const [pendingAction, setPendingAction] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  const load = useCallback(async () => {
    abortRef.current?.abort();
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    setLoading(true);
    try {
      const result = await fetchCompletionDebts(
        projectId,
        statusFilter === 'all' ? {} : { status: statusFilter },
      );
      if (!ctrl.signal.aborted) {
        setEntries(result.entries);
        setTotalCount(result.total_count);
      }
    } catch {
      if (!ctrl.signal.aborted) {
        addToast('Failed to load completion debts', 'error');
      }
    } finally {
      if (!ctrl.signal.aborted) setLoading(false);
    }
  }, [projectId, statusFilter, addToast]);

  useEffect(() => {
    void load();
    return () => abortRef.current?.abort();
  }, [load]);

  const handleDismiss = async (id: string) => {
    const reason = dismissReason.trim();
    if (!reason) {
      addToast('Dismiss reason is required', 'error');
      return;
    }
    setPendingAction(id);
    try {
      await dismissCompletionDebt(projectId, id, reason);
      addToast('Entry dismissed', 'success');
      setDismissingId(null);
      setDismissReason('');
      await load();
    } catch (err) {
      addToast(err instanceof Error ? err.message : 'Failed to dismiss', 'error');
    } finally {
      setPendingAction(null);
    }
  };

  const handleResolve = async (id: string) => {
    setPendingAction(id);
    try {
      await resolveCompletionDebt(projectId, id);
      addToast('Entry resolved', 'success');
      await load();
    } catch (err) {
      addToast(err instanceof Error ? err.message : 'Failed to resolve', 'error');
    } finally {
      setPendingAction(null);
    }
  };

  return (
    <WorkbenchPanel
      title="Completion Debt"
      meta={statusFilter === 'open' ? (totalCount > 0 ? `${totalCount} open` : 'none open') : undefined}
    >
      <div className="flex flex-col gap-2">
        <div className="flex flex-wrap gap-1">
          {STATUS_FILTERS.map(f => (
            // eslint-disable-next-line viewport/min-touch-target -- dense filter controls, consistent with adjacent panels
            <button
              key={f.value}
              type="button"
              onClick={() => setStatusFilter(f.value)}
              className={`rounded border px-2 py-0.5 text-ui-sm font-medium transition-colors ${
                statusFilter === f.value
                  ? 'border-neutral-500 bg-workbench-layer text-neutral-100'
                  : 'border-neutral-700 bg-workbench-base text-neutral-400 hover:border-neutral-600 hover:text-neutral-300'
              }`}
            >
              {f.label}
            </button>
          ))}
          {/* eslint-disable-next-line viewport/min-touch-target -- dense filter controls, consistent with adjacent panels */}
          <button
            type="button"
            onClick={() => void load()}
            className="ml-auto rounded border border-neutral-700 bg-workbench-base px-2 py-0.5 text-ui-sm text-neutral-400 hover:border-neutral-600 hover:text-neutral-300"
            disabled={loading}
          >
            {loading ? '…' : 'Refresh'}
          </button>
        </div>

        {entries.length > 0 && entries.length < totalCount && (
          <p className="text-ui-xs text-neutral-500">
            Showing {entries.length} of {totalCount}
          </p>
        )}

        {entries.length === 0 ? (
          <p className="py-4 text-center text-ui-sm text-neutral-500">
            {loading ? 'Loading…' : 'No entries'}
          </p>
        ) : (
          <div className="flex flex-col gap-1.5">
            {entries.map(entry => (
              <div
                key={entry.id}
                className="workbench-card rounded border border-neutral-700/60 bg-workbench-base/60 px-3 py-2"
              >
                <div className="flex min-w-0 flex-wrap items-start gap-2">
                  <span
                    className={`shrink-0 rounded border px-1.5 py-0.5 font-mono text-ui-xs uppercase tracking-wide ${statusPillClass(entry.status)}`}
                  >
                    {entry.status}
                  </span>
                  <span className="shrink-0 rounded border border-neutral-700/60 bg-workbench-panel/60 px-1.5 py-0.5 font-mono text-ui-xs text-neutral-400">
                    {entry.kind}
                  </span>
                  <span className="min-w-0 flex-1 truncate text-ui-sm text-neutral-200" title={entry.title}>
                    {entry.title}
                  </span>
                  <span className="shrink-0 text-ui-xs text-neutral-500">{relativeAge(entry.created_at)}</span>
                </div>
                {entry.detail && (
                  <p className="mt-1 line-clamp-2 text-ui-sm text-neutral-400">{entry.detail}</p>
                )}
                {entry.dismiss_reason && (
                  <p className="mt-1 text-ui-sm italic text-neutral-500">
                    Dismissed: {entry.dismiss_reason}
                  </p>
                )}
                {entry.status === 'open' && (
                  <div className="mt-2 flex flex-wrap gap-1.5">
                    {dismissingId === entry.id ? (
                      <>
                        <input
                          type="text"
                          value={dismissReason}
                          onChange={e => setDismissReason(e.target.value)}
                          placeholder="Dismiss reason…"
                          className="min-w-0 flex-1 rounded border border-neutral-600 bg-workbench-panel px-2 py-1 text-ui-sm text-neutral-200 placeholder-neutral-600 workbench-focus"
                          onKeyDown={e => {
                            if (e.key === 'Enter') void handleDismiss(entry.id);
                            if (e.key === 'Escape') { setDismissingId(null); setDismissReason(''); }
                          }}
                          autoFocus
                        />
                        {/* eslint-disable-next-line viewport/min-touch-target -- inline action inside dense card row */}
                        <button
                          type="button"
                          onClick={() => void handleDismiss(entry.id)}
                          disabled={pendingAction === entry.id}
                          className="rounded border border-amber-700/50 bg-amber-900/30 px-2 py-0.5 text-ui-sm text-amber-300 hover:bg-amber-900/50 disabled:opacity-50"
                        >
                          {pendingAction === entry.id ? '…' : 'Confirm'}
                        </button>
                        {/* eslint-disable-next-line viewport/min-touch-target -- inline action inside dense card row */}
                        <button
                          type="button"
                          onClick={() => { setDismissingId(null); setDismissReason(''); }}
                          className="rounded border border-neutral-700 px-2 py-0.5 text-ui-sm text-neutral-400 hover:text-neutral-300"
                        >
                          Cancel
                        </button>
                      </>
                    ) : (
                      <>
                        {/* eslint-disable-next-line viewport/min-touch-target -- inline action inside dense card row */}
                        <button
                          type="button"
                          onClick={() => setDismissingId(entry.id)}
                          className="rounded border border-neutral-700 bg-workbench-panel px-2 py-0.5 text-ui-sm text-neutral-400 hover:border-neutral-600 hover:text-neutral-300"
                        >
                          Dismiss
                        </button>
                        {/* eslint-disable-next-line viewport/min-touch-target -- inline action inside dense card row */}
                        <button
                          type="button"
                          onClick={() => void handleResolve(entry.id)}
                          disabled={pendingAction === entry.id}
                          className="rounded border border-emerald-700/50 bg-emerald-900/20 px-2 py-0.5 text-ui-sm text-emerald-400 hover:bg-emerald-900/40 disabled:opacity-50"
                        >
                          {pendingAction === entry.id ? '…' : 'Resolve'}
                        </button>
                      </>
                    )}
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </WorkbenchPanel>
  );
}
