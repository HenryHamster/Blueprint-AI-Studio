import { useCallback, useState } from 'react';
import { Check, Clock, FileText, Pencil, ShieldCheck, X } from 'lucide-react';
import { apiFetch } from '../api';
import { timeAgo } from '../utils/time';
import { MarkdownContent } from './MarkdownContent';
import type { RunningUpdateReconciliation } from '../types';

interface RunningUpdatePanelProps {
  readOnly?: boolean;
  projectId: string;
  document: string | null;
  acknowledgedAt: string | null;
  userLastActiveAt?: string | null;
  reconciliation?: RunningUpdateReconciliation | null;
  addToast: (message: string, type: 'success' | 'error' | 'info') => void;
  onAcknowledged: () => void;
  onEdited?: (newBody: string) => void;
}

export default function RunningUpdatePanel({
  readOnly = true,
  projectId,
  document,
  acknowledgedAt,
  userLastActiveAt,
  reconciliation,
  addToast,
  onAcknowledged,
  onEdited,
}: RunningUpdatePanelProps) {
  const [acknowledging, setAcknowledging] = useState(false);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState('');
  const [saving, setSaving] = useState(false);

  const hasContent = !!(document && document.trim());
  const operatorWins = reconciliation?.operator_wins_count ?? 0;
  const showReconciledBadge = operatorWins > 0;

  function startEditing() {
    if (readOnly) return;
    setDraft(document ?? '');
    setEditing(true);
  }

  function cancelEditing() {
    setEditing(false);
    setDraft('');
  }

  const handleSave = useCallback(async () => {
    if (readOnly || saving) return;
    setSaving(true);
    try {
      const res = await apiFetch(
        `/api/admin/projects/${projectId}/running-update`,
        {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ body: draft, base: document ?? '' }),
        },
      );
      if (res.status === 409) {
        addToast(
          'Running update changed since you started editing — reload and retry',
          'error',
        );
        return;
      }
      if (!res.ok) {
        throw new Error(`Save failed: HTTP ${res.status}`);
      }
      await res.json();
      addToast('Saved running update', 'success');
      onEdited?.(draft);
      setEditing(false);
      setDraft('');
    } catch (err) {
      console.error('Save running update failed:', err);
      addToast('Failed to save running update', 'error');
    } finally {
      setSaving(false);
    }
  }, [projectId, draft, document, readOnly, saving, addToast, onEdited]);

  const handleAcknowledge = useCallback(async () => {
    if (readOnly || acknowledging) return;
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
  }, [projectId, readOnly, acknowledging, addToast, onAcknowledged]);

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
          {showReconciledBadge && (
            <div
              className="mt-2 inline-flex items-center gap-1.5 rounded-md border border-emerald-600/40 bg-emerald-600/10 px-2 py-1 text-xs text-emerald-300"
              title="A mid-cycle edit you made was kept when the strategic cycle completed; the agent's competing rewrite was recorded as superseded."
            >
              <ShieldCheck size={12} />
              <span>
                Your edit preserved — {operatorWins} agent{' '}
                {operatorWins === 1 ? 'proposal' : 'proposals'} superseded
                {reconciliation?.last_operator_win_at
                  ? ` ${timeAgo(reconciliation.last_operator_win_at)}`
                  : ''}
              </span>
            </div>
          )}
        </div>
        {!readOnly && <div className="shrink-0 flex items-center gap-2">
          {!editing && (
            <button
              type="button"
              onClick={startEditing}
              className="inline-flex items-center gap-1.5 px-3 py-3 min-h-11 text-xs font-medium rounded-md border border-neutral-700 bg-neutral-800/60 text-neutral-300 hover:bg-neutral-700/60 transition"
            >
              <Pencil size={12} />
              Edit
            </button>
          )}
          <button
            type="button"
            onClick={handleAcknowledge}
            disabled={!hasContent || acknowledging || editing}
            className="inline-flex items-center gap-1.5 px-3 py-3 min-h-11 text-xs font-medium rounded-md border border-emerald-600/40 bg-emerald-600/10 text-emerald-300 hover:bg-emerald-600/20 disabled:opacity-40 disabled:cursor-not-allowed transition"
          >
            <Check size={12} />
            {acknowledging ? 'Acknowledging…' : 'Acknowledge'}
          </button>
        </div>}
      </div>

      {/* Document */}
      <div className="flex-1 overflow-y-auto custom-scrollbar px-4 py-3">
        {!readOnly && editing && (
          <div className="flex flex-col gap-3">
            <textarea
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              rows={16}
              className="w-full rounded-md border border-neutral-700 bg-neutral-900 px-3 py-2 text-sm text-neutral-100 font-mono focus:outline-none focus:border-neutral-500"
              placeholder="Edit the running update document…"
            />
            <div className="flex items-center gap-2">
              <button
                type="button"
                onClick={handleSave}
                disabled={saving}
                className="inline-flex items-center gap-1.5 px-3 py-3 min-h-11 text-xs font-medium rounded-md border border-emerald-600/40 bg-emerald-600/10 text-emerald-300 hover:bg-emerald-600/20 disabled:opacity-40 disabled:cursor-not-allowed transition"
              >
                <Check size={12} />
                {saving ? 'Saving…' : 'Save'}
              </button>
              <button
                type="button"
                onClick={cancelEditing}
                disabled={saving}
                className="inline-flex items-center gap-1.5 px-3 py-3 min-h-11 text-xs font-medium rounded-md border border-neutral-700 bg-neutral-800/60 text-neutral-300 hover:bg-neutral-700/60 disabled:opacity-40 disabled:cursor-not-allowed transition"
              >
                <X size={12} />
                Cancel
              </button>
            </div>
          </div>
        )}
        {(!editing || readOnly) && !hasContent && (
          <div className="text-center py-12 text-neutral-500 text-sm">
            <p>No running update yet.</p>
            <p className="text-xs mt-1 text-neutral-600">
              The next strategic cycle will populate this snapshot.
            </p>
          </div>
        )}
        {(!editing || readOnly) && hasContent && document && (
          <div className="prose-sm max-w-none">
            <MarkdownContent content={document} />
          </div>
        )}
      </div>
    </div>
  );
}
