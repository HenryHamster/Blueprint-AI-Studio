import { useState } from 'react';
import { Check, Clock, FileText } from 'lucide-react';
import { apiFetch } from '../api';
import { timeAgo } from '../utils/time';
import { MarkdownContent } from './MarkdownContent';
import type { RunningUpdateEntry } from '../types';

interface RunningUpdatePanelProps {
  projectId: string;
  entries: RunningUpdateEntry[];
  acknowledgedAt: string | null;
  userLastActiveAt?: string | null;
  addToast: (message: string, type: 'success' | 'error' | 'info') => void;
  onAcknowledged: () => void;
}

// Cap on what we show when an entry's prose fields are long — prevents a
// single oversized entry from dominating the panel. Full content is
// available in the underlying data if we ever want a detail view.
const SECTION_SOFT_CAP = 1200;

function truncate(s: string, limit: number): string {
  if (!s) return '';
  if (s.length <= limit) return s;
  return s.slice(0, limit) + '…';
}

export default function RunningUpdatePanel({
  projectId,
  entries,
  acknowledgedAt,
  userLastActiveAt,
  addToast,
  onAcknowledged,
}: RunningUpdatePanelProps) {
  const [acknowledging, setAcknowledging] = useState(false);

  const hasEntries = entries.length > 0;
  // Newest first — the strategic cycle appends chronologically, reverse for display
  const ordered = [...entries].reverse();

  async function handleAcknowledge() {
    if (acknowledging || !hasEntries) return;
    setAcknowledging(true);
    try {
      const res = await apiFetch(
        `/api/admin/projects/${projectId}/running-update/acknowledge`,
        { method: 'POST' },
      );
      if (!res.ok) {
        throw new Error(`Acknowledge failed: HTTP ${res.status}`);
      }
      const data = await res.json() as { cleared_entries: number };
      addToast(`Acknowledged ${data.cleared_entries} update${data.cleared_entries === 1 ? '' : 's'}`, 'success');
      onAcknowledged();
    } catch (err) {
      console.error('Acknowledge failed:', err);
      addToast('Failed to acknowledge running update', 'error');
    } finally {
      setAcknowledging(false);
    }
  }

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="border-b border-neutral-800 px-4 py-3 flex items-start justify-between gap-3">
        <div className="flex-1 min-w-0">
          <h2 className="text-sm font-semibold text-neutral-100 flex items-center gap-2">
            <FileText size={14} className="text-neutral-400" />
            Running Update
          </h2>
          <p className="text-xs text-neutral-500 mt-0.5">
            The strategic agent's journal of what has changed. Acknowledge to clear the list.
          </p>
          <div className="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-xs text-neutral-500">
            <span className="flex items-center gap-1">
              <Clock size={11} />
              {hasEntries
                ? <>{entries.length} {entries.length === 1 ? 'entry' : 'entries'} pending</>
                : <>no pending entries</>}
            </span>
            {acknowledgedAt && (
              <span className="text-neutral-600">
                last acknowledged {timeAgo(acknowledgedAt)}
              </span>
            )}
            {userLastActiveAt && (
              <span className="text-neutral-600">
                you were last active {timeAgo(userLastActiveAt)}
              </span>
            )}
          </div>
        </div>
        <button
          type="button"
          onClick={handleAcknowledge}
          disabled={!hasEntries || acknowledging}
          className="shrink-0 inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded-md border border-emerald-600/40 bg-emerald-600/10 text-emerald-300 hover:bg-emerald-600/20 disabled:opacity-40 disabled:cursor-not-allowed transition"
        >
          <Check size={12} />
          {acknowledging ? 'Acknowledging…' : 'Acknowledge'}
        </button>
      </div>

      {/* Entries */}
      <div className="flex-1 overflow-y-auto custom-scrollbar px-4 py-3 space-y-4">
        {!hasEntries && (
          <div className="text-center py-12 text-neutral-500 text-sm">
            <p>No updates since your last check-in.</p>
            <p className="text-xs mt-1 text-neutral-600">
              The agent will append one entry per strategic cycle.
            </p>
          </div>
        )}

        {ordered.map((entry, idx) => {
          const whatsChanged = truncate(entry.whats_changed || '', SECTION_SOFT_CAP);
          const whatsWorkingOn = truncate(entry.whats_working_on || '', SECTION_SOFT_CAP);
          const blockers = truncate(entry.blockers || '', SECTION_SOFT_CAP);
          const nextUp = truncate(entry.next_up || '', SECTION_SOFT_CAP);

          return (
            <div
              key={`${entry.ts}-${idx}`}
              className="border border-neutral-800 rounded-lg bg-neutral-900/40 overflow-hidden"
            >
              <div className="flex items-center justify-between px-3 py-2 border-b border-neutral-800/70 text-xs">
                <span className="text-neutral-300 font-medium">
                  {timeAgo(entry.ts)}
                </span>
                <span className="text-neutral-600 font-mono">
                  {new Date(entry.ts).toISOString().slice(0, 16).replace('T', ' ')}
                </span>
              </div>
              <div className="p-3 space-y-3">
                {whatsChanged && (
                  <div>
                    <h3 className="text-xs font-semibold text-neutral-400 uppercase tracking-wide mb-1">
                      What's changed
                    </h3>
                    <div className="prose-sm">
                      <MarkdownContent content={whatsChanged} />
                    </div>
                  </div>
                )}
                {whatsWorkingOn && (
                  <div>
                    <h3 className="text-xs font-semibold text-neutral-400 uppercase tracking-wide mb-1">
                      What I'm working on
                    </h3>
                    <div className="prose-sm">
                      <MarkdownContent content={whatsWorkingOn} />
                    </div>
                  </div>
                )}
                {blockers && (
                  <div>
                    <h3 className="text-xs font-semibold text-amber-400 uppercase tracking-wide mb-1">
                      Blockers
                    </h3>
                    <div className="prose-sm">
                      <MarkdownContent content={blockers} />
                    </div>
                  </div>
                )}
                {nextUp && (
                  <div>
                    <h3 className="text-xs font-semibold text-neutral-400 uppercase tracking-wide mb-1">
                      Next
                    </h3>
                    <div className="prose-sm">
                      <MarkdownContent content={nextUp} />
                    </div>
                  </div>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
