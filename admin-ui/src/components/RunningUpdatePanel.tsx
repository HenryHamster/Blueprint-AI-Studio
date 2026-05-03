import { useState } from 'react';
import { Check, Clock, FileText } from 'lucide-react';
import { apiFetch } from '../api';
import { timeAgo } from '../utils/time';
import { MarkdownContent } from './MarkdownContent';

interface RunningUpdatePanelProps {
  projectId: string;
  document: string | null;
  acknowledgedAt: string | null;
  userLastActiveAt?: string | null;
  addToast: (message: string, type: 'success' | 'error' | 'info') => void;
  onAcknowledged: () => void;
}

export default function RunningUpdatePanel({
  projectId,
  document,
  acknowledgedAt,
  userLastActiveAt,
  addToast,
  onAcknowledged,
}: RunningUpdatePanelProps) {
  const [acknowledging, setAcknowledging] = useState(false);

  const hasContent = !!(document && document.trim());

  async function handleAcknowledge() {
    if (acknowledging) return;
    setAcknowledging(true);
    try {
      const res = await apiFetch(
        `/api/admin/projects/${projectId}/running-update/acknowledge`,
        { method: 'POST' },
      );
      if (!res.ok) {
        throw new Error(`Acknowledge failed: HTTP ${res.status}`);
      }
      await res.json();
      addToast('Marked running update as read', 'success');
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
            The strategic agent's current-state snapshot of this project. Rewritten each cycle; acknowledge to mark as read.
          </p>
          <div className="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-xs text-neutral-500">
            <span className="flex items-center gap-1">
              <Clock size={11} />
              {hasContent ? 'current snapshot' : 'no snapshot yet'}
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
          disabled={!hasContent || acknowledging}
          className="shrink-0 inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded-md border border-emerald-600/40 bg-emerald-600/10 text-emerald-300 hover:bg-emerald-600/20 disabled:opacity-40 disabled:cursor-not-allowed transition"
        >
          <Check size={12} />
          {acknowledging ? 'Acknowledging…' : 'Acknowledge'}
        </button>
      </div>

      {/* Document */}
      <div className="flex-1 overflow-y-auto custom-scrollbar px-4 py-3">
        {!hasContent && (
          <div className="text-center py-12 text-neutral-500 text-sm">
            <p>No running update yet.</p>
            <p className="text-xs mt-1 text-neutral-600">
              The next strategic cycle will populate this snapshot.
            </p>
          </div>
        )}
        {hasContent && document && (
          <div className="prose-sm max-w-none">
            <MarkdownContent content={document} />
          </div>
        )}
      </div>
    </div>
  );
}
